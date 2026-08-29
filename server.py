"""ZenithCore Console - Debian 13 browser console with unrestricted port exposure.

Features
--------
* Real PTY terminal over WebSocket (multi-session).
* Reverse proxy: every service listening on 127.0.0.1:<port> inside the
  container is reachable at /p/<port>/  (HTTP + WebSocket + streaming).
* Raw TCP bridge: /ws/tcp?port=<port> tunnels an arbitrary TCP stream over
  WebSocket, so a SOCKS5/HTTP proxy running inside the container can be used
  from your local machine through the single public HTTPS port.
* Job runner: long-running installs (proxies, web UIs, desktop) execute in the
  background and stream their log to the console UI.
* App catalog + proxy manager APIs so everything is one click, no typing.
* Live port scanner, process list and system metrics for the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

import httpx
import websockets
from fastapi import FastAPI, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# POSIX-only modules. Importing on Windows must not explode: the console only
# runs on Linux, but the test-suite and static analysis run anywhere.
# --------------------------------------------------------------------------- #

POSIX = os.name == "posix"

if POSIX:  # pragma: no cover - platform dependent
    import fcntl
    import pty
    import select
    import termios
else:  # pragma: no cover - platform dependent
    fcntl = pty = select = termios = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONSOLE_TOKEN = os.environ.get("CONSOLE_TOKEN", "")
SESSION_COOKIE = "zenith_session"
SESSION_MAX_AGE = 7 * 24 * 3600
BOOT_TIME = time.time()
STATE_DIR = Path(os.environ.get("ZENITH_STATE", "/tmp/zenith"))
ZENITH_BIN = shutil.which("zenith") or "/usr/local/bin/zenith"

_http_client: Optional[httpx.AsyncClient] = None


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=10.0),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    try:
        yield
    finally:
        await JOBS.shutdown()
        if _http_client is not None:
            await _http_client.aclose()


app = FastAPI(title="ZenithCore Console", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def token_ok(candidate: str) -> bool:
    return bool(CONSOLE_TOKEN) and hmac.compare_digest(candidate, CONSOLE_TOKEN)


def sign_session(issued: int) -> str:
    mac = hmac.new(CONSOLE_TOKEN.encode(), str(issued).encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


def session_ok(cookie: Optional[str]) -> bool:
    if not cookie or not CONSOLE_TOKEN or "." not in cookie:
        return False
    issued_raw, mac = cookie.split(".", 1)
    try:
        issued = int(issued_raw)
    except ValueError:
        return False
    if time.time() - issued > SESSION_MAX_AGE:
        return False
    return hmac.compare_digest(sign_session(issued), cookie)


def authorized(request: Request) -> bool:
    if session_ok(request.cookies.get(SESSION_COOKIE)):
        return True
    header = request.headers.get("x-console-token", "")
    if header and token_ok(header):
        return True
    qp = request.query_params.get("token", "")
    return bool(qp) and token_ok(qp)


def ws_authorized(websocket: WebSocket, token: str = "") -> bool:
    if session_ok(websocket.cookies.get(SESSION_COOKIE)):
        return True
    return bool(token) and token_ok(token)


def deny() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def read_json(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body is just an empty dict
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Background job runner
# --------------------------------------------------------------------------- #

MAX_JOB_LINES = 4000


class Job:
    __slots__ = ("id", "label", "command", "lines", "started", "ended", "code",
                 "process", "task", "dropped")

    def __init__(self, job_id: int, label: str, command: str) -> None:
        self.id = job_id
        self.label = label
        self.command = command
        self.lines: List[str] = []
        self.started = time.time()
        self.ended: Optional[float] = None
        self.code: Optional[int] = None
        self.dropped = 0
        self.process: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.ended is None

    def append(self, text: str) -> None:
        self.lines.append(text)
        overflow = len(self.lines) - MAX_JOB_LINES
        if overflow > 0:
            del self.lines[:overflow]
            self.dropped += overflow

    def brief(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "command": self.command,
            "running": self.running,
            "code": self.code,
            "started": int(self.started),
            "elapsed": round((self.ended or time.time()) - self.started, 1),
            "lines": len(self.lines) + self.dropped,
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[int, Job] = {}
        self._counter = 0

    def get(self, job_id: int) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        return [j.brief() for j in sorted(self._jobs.values(), key=lambda x: -x.id)][:40]

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if not j.running]
        if len(finished) <= 30:
            return
        finished.sort(key=lambda j: j.ended or 0)
        for job in finished[: len(finished) - 30]:
            self._jobs.pop(job.id, None)

    async def spawn(self, command: str, label: str = "") -> Job:
        self._counter += 1
        job = Job(self._counter, label or command, command)
        self._jobs[job.id] = job
        self._prune()

        env = dict(os.environ)
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        env.setdefault("TERM", "dumb")

        try:
            job.process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd="/" if POSIX else None,
                env=env,
                start_new_session=POSIX,
            )
        except Exception as exc:  # noqa: BLE001
            job.append(f"không chạy được lệnh: {exc}")
            job.ended = time.time()
            job.code = -1
            return job

        job.task = asyncio.create_task(self._pump(job))
        return job

    async def _pump(self, job: Job) -> None:
        assert job.process is not None and job.process.stdout is not None
        stream = job.process.stdout
        try:
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                job.append(chunk.decode("utf-8", errors="replace").rstrip("\r\n"))
        except Exception as exc:  # noqa: BLE001
            job.append(f"[lỗi đọc output: {exc}]")
        finally:
            with contextlib.suppress(Exception):
                job.code = await job.process.wait()
            job.ended = time.time()
            job.append(f"[kết thúc, mã thoát {job.code}]")

    async def stop(self, job: Job) -> bool:
        if not job.running or job.process is None:
            return False
        with contextlib.suppress(Exception):
            if POSIX:
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            else:  # pragma: no cover - platform dependent
                job.process.terminate()
        return True

    async def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            if job.running:
                await self.stop(job)
            if job.task:
                job.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job.task


JOBS = JobRegistry()


# --------------------------------------------------------------------------- #
# System introspection
# --------------------------------------------------------------------------- #

_TCP_STATE_LISTEN = "0A"


def _hex_to_addr(raw: str) -> Tuple[str, int]:
    host_hex, port_hex = raw.split(":")
    port = int(port_hex, 16)
    if len(host_hex) == 8:
        packed = bytes.fromhex(host_hex)[::-1]
        return socket.inet_ntop(socket.AF_INET, packed), port
    chunks = [host_hex[i:i + 8] for i in range(0, 32, 8)]
    packed = b"".join(bytes.fromhex(c)[::-1] for c in chunks)
    return socket.inet_ntop(socket.AF_INET6, packed), port


def _inode_owner_map() -> Dict[str, Tuple[int, str]]:
    owners: Dict[str, Tuple[int, str]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return owners
    try:
        entries = list(proc.iterdir())
    except OSError:
        return owners
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError, OSError):
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            comm = "?"
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                owners[target[8:-1]] = (pid, comm)
    return owners


def listening_ports() -> List[Dict[str, Any]]:
    owners = _inode_owner_map()
    seen: Dict[int, Dict[str, Any]] = {}
    for proc_file, family in (("/proc/net/tcp", "ipv4"), ("/proc/net/tcp6", "ipv6")):
        path = Path(proc_file)
        if not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != _TCP_STATE_LISTEN:
                continue
            try:
                addr, port = _hex_to_addr(parts[1])
            except (ValueError, OSError):
                continue
            inode = parts[9]
            pid, comm = owners.get(inode, (0, "unknown"))
            record = seen.setdefault(port, {
                "port": port,
                "address": addr,
                "family": family,
                "pid": pid,
                "process": comm,
                "self": False,
            })
            if pid and not record["pid"]:
                record["pid"] = pid
                record["process"] = comm
    own_port = int(os.environ.get("PORT", "10000"))
    known = {a["port"]: a["id"] for a in APP_CATALOG if a.get("port")}
    for port, record in seen.items():
        record["self"] = port == own_port
        record["app"] = known.get(port, "")
    return sorted(seen.values(), key=lambda r: r["port"])


def port_is_listening(port: int) -> bool:
    return any(p["port"] == port for p in listening_ports())


def uname_info() -> Dict[str, str]:
    """os.uname() is Linux-only; fall back to platform for portability."""
    try:
        u = os.uname()  # type: ignore[attr-defined]
        return {"nodename": u.nodename, "release": u.release, "machine": u.machine}
    except AttributeError:
        import platform
        return {
            "nodename": platform.node(),
            "release": platform.release(),
            "machine": platform.machine(),
        }


def _read_meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            num = rest.strip().split(" ")[0]
            if num.isdigit():
                values[key] = int(num) * 1024
    except OSError:
        pass
    return values


def _cpu_times() -> Optional[Tuple[int, int]]:
    try:
        first = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    except (OSError, IndexError):
        return None
    nums = [int(x) for x in first if x.isdigit()]
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def _net_totals() -> Tuple[int, int]:
    rx = tx = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            cols = rest.split()
            if len(cols) >= 9:
                rx += int(cols[0])
                tx += int(cols[8])
    except (OSError, ValueError):
        pass
    return rx, tx


_prev_cpu: Optional[Tuple[int, int]] = None
_prev_net: Optional[Tuple[float, int, int]] = None


def system_metrics() -> Dict[str, Any]:
    global _prev_cpu, _prev_net
    mem = _read_meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)

    cpu_percent = None
    now = _cpu_times()
    if now and _prev_cpu:
        d_total = now[0] - _prev_cpu[0]
        d_idle = now[1] - _prev_cpu[1]
        if d_total > 0:
            cpu_percent = round(max(0.0, (1 - d_idle / d_total)) * 100, 1)
    if now:
        _prev_cpu = now

    rx, tx = _net_totals()
    rx_rate = tx_rate = 0.0
    stamp = time.time()
    if _prev_net:
        gap = stamp - _prev_net[0]
        if gap > 0.2:
            rx_rate = max(0.0, (rx - _prev_net[1]) / gap)
            tx_rate = max(0.0, (tx - _prev_net[2]) / gap)
            _prev_net = (stamp, rx, tx)
    else:
        _prev_net = (stamp, rx, tx)

    try:
        disk = shutil.disk_usage("/")
        disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_info = {"total": 0, "used": 0, "free": 0}

    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (0.0, 0.0, 0.0)

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": os.cpu_count() or 1,
        "load": [round(x, 2) for x in load],
        "mem_total": total,
        "mem_used": max(0, total - available),
        "swap_total": mem.get("SwapTotal", 0),
        "swap_used": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        "disk": disk_info,
        "net": {"rx": rx, "tx": tx, "rx_rate": round(rx_rate), "tx_rate": round(tx_rate)},
        "uptime": int(time.time() - BOOT_TIME),
    }


def process_list(limit: int = 60) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    try:
        page = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
    except (ValueError, AttributeError, OSError):
        page = 4096
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            close = stat.rfind(")")
            fields = stat[close + 2:].split()
            rss = int(fields[21]) * page
            utime, stime = int(fields[11]), int(fields[12])
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace").strip()
            comm = stat[stat.find("(") + 1:close]
        except (OSError, IndexError, ValueError):
            continue
        rows.append({
            "pid": int(entry.name),
            "name": comm,
            "cmd": cmdline or comm,
            "rss": rss,
            "cpu_ticks": utime + stime,
            "state": fields[0] if fields else "?",
        })
    rows.sort(key=lambda r: -r["rss"])
    return rows[:limit]


# --------------------------------------------------------------------------- #
# App catalog - one click web UIs
# --------------------------------------------------------------------------- #

APP_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "code-server",
        "name": "code-server",
        "tag": "IDE",
        "port": 8080,
        "path": "/",
        "web": True,
        "desc": "VS Code đầy đủ chạy trong trình duyệt, mở thẳng cây thư mục container.",
        "size": "~180 MB",
    },
    {
        "id": "filebrowser",
        "name": "File Browser",
        "tag": "Files",
        "port": 8081,
        "path": "/",
        "web": True,
        "desc": "Trình quản lý file có upload, sửa, giải nén và chia sẻ link.",
        "size": "~25 MB",
    },
    {
        "id": "ttyd",
        "name": "ttyd",
        "tag": "Shell",
        "port": 8082,
        "path": "/",
        "web": True,
        "desc": "Terminal web độc lập, hữu ích khi cần shell song song console.",
        "size": "~5 MB",
    },
    {
        "id": "dufs",
        "name": "dufs",
        "tag": "Files",
        "port": 8083,
        "path": "/",
        "web": True,
        "desc": "HTTP file server tĩnh siêu nhẹ, hỗ trợ WebDAV và upload.",
        "size": "~8 MB",
    },
    {
        "id": "jupyter",
        "name": "JupyterLab",
        "tag": "Data",
        "port": 8888,
        "path": "/lab",
        "web": True,
        "desc": "Notebook Python cho phân tích dữ liệu và thử nghiệm nhanh.",
        "size": "~350 MB",
    },
    {
        "id": "desktop",
        "name": "Desktop XFCE",
        "tag": "GUI",
        "port": 6080,
        "path": "/vnc.html?autoconnect=1&resize=remote",
        "web": True,
        "desc": "Môi trường đồ hoạ đầy đủ qua noVNC, chạy được trình duyệt thật.",
        "size": "~700 MB",
    },
    {
        "id": "desktop-light",
        "name": "Desktop nhẹ",
        "tag": "GUI",
        "port": 6080,
        "path": "/vnc.html?autoconnect=1&resize=remote",
        "web": True,
        "desc": "Openbox thay cho XFCE, hợp với gói RAM thấp.",
        "size": "~250 MB",
    },
]

APP_IDS = {a["id"] for a in APP_CATALOG}

PROXY_KINDS = {
    "socks5": {"name": "SOCKS5 (MicroSocks)", "port": 1080, "scheme": "socks5"},
    "http": {"name": "HTTP (Tinyproxy)", "port": 8888, "scheme": "http"},
    "squid": {"name": "Squid", "port": 3128, "scheme": "http"},
    "privoxy": {"name": "Privoxy", "port": 8118, "scheme": "http"},
}


def zenith_available() -> bool:
    return Path(ZENITH_BIN).exists()


def _svc_pid(name: str) -> Optional[int]:
    """Read a zenith-managed service pidfile and check if it is still alive."""
    pidfile = STATE_DIR / "svc" / f"{name}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    except OSError:
        return None
    return pid


def _state_json(name: str) -> Dict[str, Any]:
    path = STATE_DIR / name
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def valid_port(value: Any) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


_CRED_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def valid_cred(value: str) -> bool:
    return bool(_CRED_RE.match(value or ""))


# --------------------------------------------------------------------------- #
# Basic routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    body = await read_json(request)
    candidate = str(body.get("token", ""))
    if not token_ok(candidate):
        await asyncio.sleep(0.4)  # slow down brute force attempts
        return JSONResponse({"ok": False, "error": "Invalid console token"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(int(time.time())),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/status")
def status(request: Request) -> JSONResponse:
    debian = "unknown"
    debian_file = Path("/etc/debian_version")
    if debian_file.exists():
        try:
            debian = debian_file.read_text().strip()
        except OSError:
            pass
    u = uname_info()
    return JSONResponse({
        "online": True,
        "authenticated": authorized(request),
        "debian": debian,
        "hostname": u["nodename"],
        "kernel": u["release"],
        "arch": u["machine"],
        "shell": "/bin/bash",
        "systemctl": shutil.which("systemctl") is not None,
        "zenith": zenith_available(),
        "posix": POSIX,
        "python": sys.version.split()[0],
        "public_port": int(os.environ.get("PORT", "10000")),
    })


@app.get("/api/system")
def api_system(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    return JSONResponse(system_metrics())


@app.get("/api/ports")
def api_ports(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    return JSONResponse({"ports": listening_ports()})


@app.get("/api/processes")
def api_processes(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    return JSONResponse({"processes": process_list()})


@app.post("/api/kill")
async def api_kill(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    body = await read_json(request)
    try:
        pid = int(body.get("pid", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "pid không hợp lệ"}, status_code=400)
    if pid <= 1 or pid == os.getpid():
        return JSONResponse({"error": "Không được phép dừng tiến trình này"}, status_code=400)
    sig = signal.SIGKILL if body.get("force") and POSIX else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return JSONResponse({"error": "Tiến trình không tồn tại"}, status_code=404)
    except (PermissionError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "pid": pid})


@app.get("/api/probe")
async def api_probe(request: Request, port: int = Query(...)) -> JSONResponse:
    """Check whether a local port speaks HTTP, so the UI can offer 'Open UI'."""
    if not authorized(request):
        return deny()
    checked = valid_port(port)
    if checked is None:
        return JSONResponse({"error": "Port không hợp lệ"}, status_code=400)
    assert _http_client is not None
    result: Dict[str, Any] = {"port": checked, "open": False, "http": False}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", checked), timeout=2.5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        result["open"] = True
        del reader
    except Exception:  # noqa: BLE001
        return JSONResponse(result)
    try:
        head = await _http_client.get(
            f"http://127.0.0.1:{checked}/", timeout=httpx.Timeout(4.0))
        result["http"] = True
        result["status"] = head.status_code
        result["server"] = head.headers.get("server", "")
        result["content_type"] = head.headers.get("content-type", "")
        title = re.search(rb"<title[^>]*>(.{0,120}?)</title>",
                          head.content[:20000], re.I | re.S)
        if title:
            result["title"] = title.group(1).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        result["http"] = False
    return JSONResponse(result)


# --------------------------------------------------------------------------- #
# Jobs API
# --------------------------------------------------------------------------- #

@app.get("/api/jobs")
def api_jobs(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    return JSONResponse({"jobs": JOBS.list()})


@app.get("/api/jobs/{job_id}")
def api_job(request: Request, job_id: int, offset: int = 0) -> JSONResponse:
    if not authorized(request):
        return deny()
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "job không tồn tại"}, status_code=404)
    start = max(0, min(offset, len(job.lines)))
    payload = job.brief()
    payload["offset"] = start + len(job.lines[start:])
    payload["log"] = job.lines[start:]
    return JSONResponse(payload)


@app.post("/api/jobs/{job_id}/stop")
async def api_job_stop(request: Request, job_id: int) -> JSONResponse:
    if not authorized(request):
        return deny()
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "job không tồn tại"}, status_code=404)
    stopped = await JOBS.stop(job)
    return JSONResponse({"ok": stopped, "job": job.brief()})


@app.post("/api/exec")
async def api_exec(request: Request) -> JSONResponse:
    """Run an arbitrary shell command as a background job.

    The console is already a root shell, so this endpoint adds no privilege -
    it only exists so UI buttons can run the same commands the terminal can.
    """
    if not authorized(request):
        return deny()
    body = await read_json(request)
    command = str(body.get("command", "")).strip()
    if not command:
        return JSONResponse({"error": "Thiếu lệnh"}, status_code=400)
    if len(command) > 8000:
        return JSONResponse({"error": "Lệnh quá dài"}, status_code=400)
    job = await JOBS.spawn(command, str(body.get("label", "")) or command[:60])
    return JSONResponse({"ok": True, "job": job.brief()})


# --------------------------------------------------------------------------- #
# Proxy manager API
# --------------------------------------------------------------------------- #

@app.get("/api/proxy")
def api_proxy_state(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    saved = _state_json("proxy.json")
    running = {p["port"]: p for p in listening_ports()}
    items = []
    for kind, meta in PROXY_KINDS.items():
        entry = saved.get(kind, {}) if isinstance(saved.get(kind), dict) else {}
        port = valid_port(entry.get("port")) or meta["port"]
        items.append({
            "kind": kind,
            "name": meta["name"],
            "scheme": meta["scheme"],
            "port": port,
            "running": port in running,
            "auth": bool(entry.get("auth")),
            "user": entry.get("user", ""),
        })
    return JSONResponse({
        "proxies": items,
        "outbound": _state_json("outbound.json"),
        "zenith": zenith_available(),
    })


@app.post("/api/proxy/start")
async def api_proxy_start(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    body = await read_json(request)
    kind = str(body.get("kind", ""))
    if kind not in PROXY_KINDS:
        return JSONResponse({"error": "Loại proxy không hỗ trợ"}, status_code=400)
    port = valid_port(body.get("port")) or PROXY_KINDS[kind]["port"]

    args = [ZENITH_BIN, "proxy", kind, str(port)]
    user = str(body.get("user", "")).strip()
    password = str(body.get("password", "")).strip()
    if user or password:
        if not (valid_cred(user) and valid_cred(password)):
            return JSONResponse(
                {"error": "User/mật khẩu chỉ cho phép chữ, số và . _ @ -"}, status_code=400)
        args += ["--auth", f"{user}:{password}"]

    command = " ".join(shlex.quote(a) for a in args)
    job = await JOBS.spawn(command, f"Khởi động {PROXY_KINDS[kind]['name']} :{port}")
    return JSONResponse({"ok": True, "job": job.brief()})


@app.post("/api/proxy/stop")
async def api_proxy_stop(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    body = await read_json(request)
    kind = str(body.get("kind", "")) or "all"
    if kind != "all" and kind not in PROXY_KINDS:
        return JSONResponse({"error": "Loại proxy không hỗ trợ"}, status_code=400)
    args = [ZENITH_BIN, "proxy", "stop"] + ([kind] if kind != "all" else [])
    command = " ".join(shlex.quote(a) for a in args)
    job = await JOBS.spawn(command, f"Dừng proxy {kind}")
    return JSONResponse({"ok": True, "job": job.brief()})


@app.post("/api/proxy/outbound")
async def api_proxy_outbound(request: Request) -> JSONResponse:
    """Route the container's own traffic through an upstream proxy."""
    if not authorized(request):
        return deny()
    body = await read_json(request)
    action = str(body.get("action", "set"))
    if action == "clear":
        command = " ".join(shlex.quote(a) for a in [ZENITH_BIN, "proxy", "out", "clear"])
        job = await JOBS.spawn(command, "Xoá proxy đi ra")
        return JSONResponse({"ok": True, "job": job.brief()})

    url = str(body.get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "Thiếu URL proxy"}, status_code=400)
    if not re.match(r"^(https?|socks4|socks4a|socks5|socks5h)://[^\s\"']{3,300}$", url, re.I):
        return JSONResponse(
            {"error": "URL phải dạng scheme://[user:pass@]host:port"}, status_code=400)
    command = " ".join(shlex.quote(a) for a in [ZENITH_BIN, "proxy", "out", "set", url])
    job = await JOBS.spawn(command, "Đặt proxy đi ra")
    return JSONResponse({"ok": True, "job": job.brief()})


# --------------------------------------------------------------------------- #
# Public tunnel API (Cloudflare Tunnel via the zenith CLI)
# --------------------------------------------------------------------------- #

_TUNNEL_URL_RE = re.compile(r"^https?://[^\s\"']{3,300}$", re.I)


@app.get("/api/tunnel")
def api_tunnel_state(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    saved = _state_json("tunnel.json")
    running = _svc_pid("cloudflared") is not None
    return JSONResponse({
        "available": shutil.which("cloudflared") is not None,
        "zenith": zenith_available(),
        "running": running,
        "mode": saved.get("mode") if running else None,
        "url": saved.get("url") if running else None,
        "token_configured": bool(os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")),
        "public_port": int(os.environ.get("PORT", "10000")),
    })


@app.post("/api/tunnel/start")
async def api_tunnel_start(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    body = await read_json(request)
    target = str(body.get("target", "")).strip()
    if target and not _TUNNEL_URL_RE.match(target):
        return JSONResponse(
            {"error": "Target phải dạng http(s)://host:port"}, status_code=400)

    args = [ZENITH_BIN, "tunnel", "start"]
    if target:
        args.append(target)
    command = " ".join(shlex.quote(a) for a in args)
    job = await JOBS.spawn(command, "Khởi động public tunnel")
    return JSONResponse({"ok": True, "job": job.brief()})


@app.post("/api/tunnel/stop")
async def api_tunnel_stop(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    command = " ".join(shlex.quote(a) for a in [ZENITH_BIN, "tunnel", "stop"])
    job = await JOBS.spawn(command, "Dừng public tunnel")
    return JSONResponse({"ok": True, "job": job.brief()})


# --------------------------------------------------------------------------- #
# App manager API
# --------------------------------------------------------------------------- #

@app.get("/api/apps")
def api_apps(request: Request) -> JSONResponse:
    if not authorized(request):
        return deny()
    saved = _state_json("apps.json")
    running_ports = {p["port"] for p in listening_ports()}
    items = []
    for meta in APP_CATALOG:
        entry = saved.get(meta["id"], {}) if isinstance(saved.get(meta["id"]), dict) else {}
        port = valid_port(entry.get("port")) or meta["port"]
        items.append({
            **meta,
            "port": port,
            "installed": bool(entry.get("installed")),
            "running": port in running_ports,
            "url": f"/p/{port}{meta['path']}",
            "extra": entry.get("extra", {}),
        })
    return JSONResponse({"apps": items, "zenith": zenith_available()})


@app.post("/api/apps/{action}")
async def api_apps_action(request: Request, action: str) -> JSONResponse:
    if not authorized(request):
        return deny()
    if action not in {"install", "start", "stop", "remove"}:
        return JSONResponse({"error": "Hành động không hợp lệ"}, status_code=400)
    body = await read_json(request)
    app_id = str(body.get("id", ""))
    if app_id not in APP_IDS:
        return JSONResponse({"error": "Ứng dụng không có trong danh mục"}, status_code=400)

    args = [ZENITH_BIN, "app", action, app_id]
    port = valid_port(body.get("port"))
    if port:
        args += ["--port", str(port)]
    password = str(body.get("password", "")).strip()
    if password:
        if not valid_cred(password):
            return JSONResponse(
                {"error": "Mật khẩu chỉ cho phép chữ, số và . _ @ -"}, status_code=400)
        args += ["--password", password]

    labels = {"install": "Cài", "start": "Chạy", "stop": "Dừng", "remove": "Gỡ"}
    command = " ".join(shlex.quote(a) for a in args)
    job = await JOBS.spawn(command, f"{labels[action]} {app_id}")
    return JSONResponse({"ok": True, "job": job.brief()})


@app.post("/api/pkg")
async def api_pkg(request: Request) -> JSONResponse:
    """Install arbitrary apt packages - no allow-list, this box is yours."""
    if not authorized(request):
        return deny()
    body = await read_json(request)
    raw = str(body.get("packages", "")).strip()
    if not raw:
        return JSONResponse({"error": "Thiếu tên gói"}, status_code=400)
    names = [n for n in re.split(r"[\s,]+", raw) if n]
    bad = [n for n in names if not re.match(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,80}$", n)]
    if bad:
        return JSONResponse({"error": f"Tên gói không hợp lệ: {', '.join(bad[:4])}"},
                            status_code=400)
    if len(names) > 40:
        return JSONResponse({"error": "Tối đa 40 gói mỗi lần"}, status_code=400)
    quoted = " ".join(shlex.quote(n) for n in names)
    command = (
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
        f"apt-get install -y --no-install-recommends {quoted}"
    )
    job = await JOBS.spawn(command, f"apt install {' '.join(names[:3])}"
                                    + (" …" if len(names) > 3 else ""))
    return JSONResponse({"ok": True, "job": job.brief()})


# --------------------------------------------------------------------------- #
# Terminal (PTY over WebSocket)
# --------------------------------------------------------------------------- #

def set_winsize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 400))
    cols = max(1, min(int(cols), 800))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def child_shell(cwd: str = "/root") -> None:  # pragma: no cover - runs in child
    os.environ.setdefault("TERM", "xterm-256color")
    os.environ.setdefault("LANG", "C.UTF-8")
    os.environ.setdefault("LC_ALL", "C.UTF-8")
    os.environ["HOME"] = "/root"
    os.environ["PS1"] = "\\[\\e[38;5;73m\\]\\u@zenith\\[\\e[0m\\]:\\[\\e[38;5;110m\\]\\w\\[\\e[0m\\]\\$ "
    try:
        os.chdir(cwd)
    except OSError:
        os.chdir("/")
    os.execv("/bin/bash", ["/bin/bash", "--login"])


@app.websocket("/ws/terminal")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        auth_data = json.loads(raw_auth)
    except Exception:  # noqa: BLE001
        await websocket.close(code=1008, reason="Authentication failed")
        return

    if not ws_authorized(websocket, str(auth_data.get("token", ""))):
        await websocket.close(code=1008, reason="Invalid console token")
        return

    if not POSIX:  # pragma: no cover - platform dependent
        await websocket.send_text(json.dumps({
            "type": "fatal",
            "message": "PTY chỉ khả dụng trên Linux.",
        }))
        await websocket.close(code=1011, reason="PTY unavailable")
        return

    rows = int(auth_data.get("rows") or 30)
    cols = int(auth_data.get("cols") or 120)
    cwd = str(auth_data.get("cwd") or "/root")

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        child_shell(cwd)
        return

    try:
        set_winsize(fd, rows, cols)

        async def browser_to_pty() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"type": "input", "data": raw}
                kind = data.get("type")
                if kind == "input":
                    payload = str(data.get("data", "")).encode("utf-8", errors="ignore")
                    if payload:
                        os.write(fd, payload)
                elif kind == "resize":
                    try:
                        set_winsize(fd, int(data.get("rows", rows)), int(data.get("cols", cols)))
                    except (TypeError, ValueError, OSError):
                        pass
                elif kind == "signal":
                    try:
                        os.killpg(os.getpgid(pid), int(data.get("value", signal.SIGINT)))
                    except (OSError, ValueError):
                        pass

        async def pty_to_browser() -> None:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    ready, _, _ = await loop.run_in_executor(
                        None, select.select, [fd], [], [], 0.4)
                    if not ready:
                        try:
                            waited, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            waited = pid
                        if waited == pid:
                            break
                        continue
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    await websocket.send_text(json.dumps({
                        "type": "output",
                        "data": chunk.decode("utf-8", errors="replace"),
                    }))
                except OSError:
                    break

        await websocket.send_text(json.dumps({"type": "ready", "pid": pid, "message": ""}))

        sender = asyncio.create_task(browser_to_pty())
        receiver = asyncio.create_task(pty_to_browser())
        _, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        for action in (
            lambda: os.kill(pid, signal.SIGHUP),
            lambda: os.close(fd),
            lambda: os.waitpid(pid, os.WNOHANG),
        ):
            try:
                action()
            except (OSError, ChildProcessError):
                pass
        with contextlib.suppress(Exception):
            await websocket.close()


# --------------------------------------------------------------------------- #
# Raw TCP bridge  (use container proxies from your local machine)
# --------------------------------------------------------------------------- #

@app.websocket("/ws/tcp")
async def tcp_bridge(
    websocket: WebSocket,
    port: int = Query(...),
    host: str = Query("127.0.0.1"),
    token: str = Query(""),
) -> None:
    await websocket.accept()
    if not ws_authorized(websocket, token):
        await websocket.close(code=1008, reason="Unauthorized")
        return
    if valid_port(port) is None:
        await websocket.close(code=1008, reason="Invalid port")
        return

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=15
        )
    except Exception as exc:  # noqa: BLE001
        await websocket.close(code=1011, reason=f"Connect failed: {exc}"[:110])
        return

    async def ws_to_tcp() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload is None:
                text = message.get("text")
                if text is None:
                    continue
                payload = text.encode("utf-8", errors="ignore")
            writer.write(payload)
            await writer.drain()

    async def tcp_to_ws() -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            await websocket.send_bytes(chunk)

    up = asyncio.create_task(ws_to_tcp())
    down = asyncio.create_task(tcp_to_ws())
    try:
        _, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()


# --------------------------------------------------------------------------- #
# Reverse proxy for any local port:  /p/<port>/<path>
# --------------------------------------------------------------------------- #

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length", "host",
}
PROXY_PORT_COOKIE = "zenith_proxy_port"
_ABS_URL_ATTR = re.compile(rb'((?:href|src|action|poster|data-src|srcset)\s*=\s*["\'])/(?!/)', re.I)
_ABS_URL_CSS = re.compile(rb'(url\(\s*["\']?)/(?!/)', re.I)
_REWRITE_TYPES = ("text/html", "application/xhtml")
_CSS_TYPES = ("text/css",)
# Bodies larger than this are streamed untouched instead of being buffered.
MAX_REWRITE_BYTES = 8 * 1024 * 1024


def _filter_headers(items: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    return {k: v for k, v in items if k.lower() not in HOP_BY_HOP}


def _rewrite_css(body: bytes, prefix: str) -> bytes:
    return _ABS_URL_CSS.sub(rb"\1" + prefix.encode() + b"/", body)


def _rewrite_html(body: bytes, prefix: str) -> bytes:
    marker = prefix.encode()
    body = _ABS_URL_ATTR.sub(rb"\1" + marker + b"/", body)
    body = _ABS_URL_CSS.sub(rb"\1" + marker + b"/", body)
    inject = (
        b"<script>(function(){var P='" + marker + b"';"
        b"function fix(u){return (typeof u==='string'&&u.charAt(0)==='/'"
        b"&&u.substr(0,2)!=='//'&&u.indexOf(P)!==0)?P+u:u;}"
        b"var of=window.fetch;if(of){window.fetch=function(u,o){"
        b"if(typeof u==='string')u=fix(u);return of.call(this,u,o);};}"
        b"var ox=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){"
        b"if(typeof u==='string'){arguments[1]=fix(u);}return ox.apply(this,arguments);};"
        b"var OW=window.WebSocket;window.WebSocket=function(u,p){"
        b"try{var a=new URL(u,location.href);if(a.host===location.host"
        b"&&a.pathname.indexOf(P)!==0){a.pathname=P+a.pathname;u=a.toString();}}catch(e){}"
        b"return p?new OW(u,p):new OW(u);};window.WebSocket.prototype=OW.prototype;"
        b"try{var ps=history.pushState,rs=history.replaceState;"
        b"history.pushState=function(a,b,u){return ps.call(this,a,b,u?fix(u):u);};"
        b"history.replaceState=function(a,b,u){return rs.call(this,a,b,u?fix(u):u);};}catch(e){}"
        b"})();</script>"
    )
    lowered = body.lower()
    idx = lowered.find(b"</head>")
    if idx == -1:
        idx = lowered.find(b"<body")
        if idx != -1:
            end = lowered.find(b">", idx)
            idx = end + 1 if end != -1 else -1
    if idx != -1:
        return body[:idx] + inject + body[idx:]
    return inject + body


def _should_rewrite(content_type: str) -> str:
    lowered = content_type.lower()
    if any(t in lowered for t in _REWRITE_TYPES):
        return "html"
    if any(t in lowered for t in _CSS_TYPES):
        return "css"
    return ""


async def _proxy_http(request: Request, port: int, path: str) -> Response:
    assert _http_client is not None
    prefix = f"/p/{port}"
    query = request.url.query
    target = f"http://127.0.0.1:{port}/{path}" + (f"?{query}" if query else "")

    headers = _filter_headers(request.headers.items())
    headers["host"] = f"127.0.0.1:{port}"
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-prefix"] = prefix
    headers["accept-encoding"] = "identity"

    body = await request.body()

    upstream_request = _http_client.build_request(
        request.method, target, headers=headers, content=body or None
    )
    try:
        upstream = await _http_client.send(upstream_request, stream=True)
    except httpx.ConnectError:
        return JSONResponse(
            {"error": f"Không có service nào lắng nghe trên 127.0.0.1:{port}"},
            status_code=502,
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"Upstream error: {exc}"}, status_code=502)

    out_headers = _filter_headers(upstream.headers.items())
    location = upstream.headers.get("location")
    if location and location.startswith("/") and not location.startswith("//"):
        out_headers["location"] = prefix + location

    content_type = upstream.headers.get("content-type", "")
    mode = _should_rewrite(content_type)

    declared = upstream.headers.get("content-length")
    try:
        declared_size = int(declared) if declared is not None else None
    except ValueError:
        declared_size = None
    too_big = declared_size is not None and declared_size > MAX_REWRITE_BYTES

    if mode and not too_big:
        try:
            payload = await upstream.aread()
        finally:
            await upstream.aclose()
        if len(payload) <= MAX_REWRITE_BYTES:
            payload = (_rewrite_html(payload, prefix) if mode == "html"
                       else _rewrite_css(payload, prefix))
        response: Response = Response(
            content=payload,
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=content_type or None,
        )
    else:
        async def body_stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        if declared_size is not None:
            out_headers["content-length"] = str(declared_size)
        response = StreamingResponse(
            body_stream(),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=content_type or None,
        )

    response.set_cookie(PROXY_PORT_COOKIE, str(port), path="/", samesite="lax")
    return response


@app.api_route("/p/{port:int}", methods=["GET", "HEAD"])
async def proxy_root(request: Request, port: int) -> Response:
    if not authorized(request):
        return deny()
    return Response(status_code=307, headers={"location": f"/p/{port}/"})


@app.api_route(
    "/p/{port:int}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_any(request: Request, port: int, path: str) -> Response:
    if not authorized(request):
        return deny()
    if valid_port(port) is None:
        return JSONResponse({"error": "Port không hợp lệ"}, status_code=400)
    return await _proxy_http(request, port, path)


@app.websocket("/p/{port}/{path:path}")
async def proxy_ws(websocket: WebSocket, port: int, path: str) -> None:
    if not session_ok(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=1008)
        return
    if valid_port(port) is None:
        await websocket.close(code=1008, reason="Invalid port")
        return
    await websocket.accept()
    query = websocket.url.query
    target = f"ws://127.0.0.1:{port}/{path}" + (f"?{query}" if query else "")
    try:
        upstream = await websockets.connect(target, open_timeout=15, max_size=None)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Upstream websocket unreachable")
        return

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    up = asyncio.create_task(client_to_upstream())
    down = asyncio.create_task(upstream_to_client())
    try:
        _, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()


# --------------------------------------------------------------------------- #
# Fallback: absolute-root requests coming from a proxied panel
# --------------------------------------------------------------------------- #

_RESERVED_PREFIXES = ("api/", "static/", "ws/", "p/")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_fallback(request: Request, path: str) -> Response:
    if path.startswith(_RESERVED_PREFIXES):
        return JSONResponse({"error": "not found"}, status_code=404)
    cookie_port = request.cookies.get(PROXY_PORT_COOKIE)
    if cookie_port and cookie_port.isdigit() and authorized(request):
        port = valid_port(cookie_port)
        if port is not None:
            return await _proxy_http(request, port, path)
    if not path:
        return FileResponse(STATIC_DIR / "index.html")
    return JSONResponse({"error": "not found"}, status_code=404)
