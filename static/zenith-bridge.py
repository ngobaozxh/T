#!/usr/bin/env python3
"""ZenithCore TCP bridge.

Exposes a TCP port on your local machine and forwards every byte over a
WebSocket to a port inside the ZenithCore container. Use it to consume a
SOCKS5 / HTTP proxy (or any TCP service) that only listens on loopback
inside the container.

Usage
-----
    pip install websockets
    python3 zenith-bridge.py --local 1080 \
        --url wss://your-service.example.com/ws/tcp?port=1080 \
        --token YOUR_CONSOLE_TOKEN

Then point applications at socks5://127.0.0.1:1080
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run:  pip install websockets")


def build_url(url: str, port: int | None, token: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    if port is not None:
        query.setdefault("port", str(port))
    if token:
        query["token"] = token
    return urlunparse(parsed._replace(query=urlencode(query)))


async def pump_tcp_to_ws(reader: asyncio.StreamReader, ws) -> None:
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        await ws.send(chunk)


async def pump_ws_to_tcp(ws, writer: asyncio.StreamWriter) -> None:
    async for message in ws:
        if isinstance(message, str):
            message = message.encode()
        writer.write(message)
        await writer.drain()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, url: str, verbose: bool) -> None:
    peer = writer.get_extra_info("peername")
    try:
        async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
            if verbose:
                print(f"[+] {peer} connected")
            up = asyncio.create_task(pump_tcp_to_ws(reader, ws))
            down = asyncio.create_task(pump_ws_to_tcp(ws, writer))
            _, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] {peer} error: {exc}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if verbose:
            print(f"[-] {peer} closed")


async def main() -> None:
    ap = argparse.ArgumentParser(description="ZenithCore TCP-over-WebSocket bridge")
    ap.add_argument("--local", type=int, required=True, help="local TCP port to listen on")
    ap.add_argument("--url", required=True, help="wss://<host>/ws/tcp?port=<remote port>")
    ap.add_argument("--token", default="", help="CONSOLE_TOKEN")
    ap.add_argument("--remote", type=int, default=None, help="remote port (if not in --url)")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    url = build_url(args.url, args.remote, args.token)
    verbose = not args.quiet

    server = await asyncio.start_server(
        lambda r, w: handle(r, w, url, verbose), args.bind, args.local
    )
    shown = url.split("token=")[0].rstrip("&?")
    print(f"ZenithCore bridge  {args.bind}:{args.local}  ->  {shown}")
    print("Ctrl+C để dừng.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
