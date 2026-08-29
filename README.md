# ZenithCore Console

Console Debian 13 chạy trong trình duyệt: terminal PTY thật, reverse proxy cho **mọi** cổng nội bộ,
bridge TCP-over-WebSocket để dùng proxy trong container từ máy cá nhân, và desktop GUI tuỳ chọn.

---

## Có gì trong bản này

| Thành phần | Mô tả |
|---|---|
| **Terminal đa phiên** | Nhiều tab bash độc lập, mỗi tab là một PTY riêng. `Ctrl+Shift+T` mở tab mới. |
| **Reverse proxy** | Mọi service ở `127.0.0.1:PORT` mở được tại `/p/PORT/`. Hỗ trợ HTTP, WebSocket, tự viết lại đường dẫn tuyệt đối. |
| **TCP bridge** | `/ws/tcp?port=N` bắc cầu TCP thật qua WebSocket — dùng SOCKS5/HTTP proxy trong container từ máy bạn. |
| **Desktop GUI** | Openbox/XFCE + TigerVNC + noVNC đã **bake sẵn trong image** và **tự khởi động khi container start** (giống pattern `railway-ubuntu-novnc` / `docker-ubuntu-vnc-desktop`) — xem `/p/6080/vnc.html` ngay không cần gõ lệnh. |
| **Public tunnel** | `zenith tunnel start` dùng Cloudflare Tunnel (miễn phí) để có thêm một URL HTTPS công khai, độc lập với domain của nền tảng host. |
| **Proxy stack** | Cài sẵn microsocks, tinyproxy, privoxy, squid, proxychains4, redsocks, socat. |
| **Giám sát** | CPU / RAM / disk / uptime / danh sách cổng đang lắng nghe, đọc trực tiếp từ `/proc`. |

Giao diện dùng tông tối trung tính, font mono, viền mảnh 1px — không glassmorphism, không gradient nhiều màu.

---

## Triển khai

Tạo **Web Service** kiểu **Docker**, trỏ vào repo này.

Biến môi trường:

| Biến | Bắt buộc | Ghi chú |
|---|---|---|
| `CONSOLE_TOKEN` | Nên đặt | Chuỗi bí mật dài. Nếu bỏ trống, container tự sinh token và in ra log khi khởi động. |
| `PORT` | Không | Nền tảng host tự cấp. Mặc định là `8888` nếu không có biến môi trường. |
| `ZENITH_AUTOSTART_DESKTOP` | Không | Mặc định `1` — tự bật desktop GUI (VNC+noVNC) ngay khi container start. Đặt `0` để tắt và bật thủ công bằng `zenith desktop start`. |
| `ZENITH_DESKTOP_GEOMETRY` | Không | Độ phân giải desktop khi autostart, mặc định `1440x900`. |
| `ZENITH_DESKTOP_LIGHT` | Không | Mặc định `1` — dùng Openbox (nhẹ RAM) thay vì XFCE khi autostart. Đặt `0` để dùng XFCE đầy đủ. |
| `ZENITH_AUTOSTART_TUNNEL` | Không | Đặt `1` để tự bật Cloudflare Tunnel ngay khi container start. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Không | Token của named tunnel (Cloudflare Zero Trust) để có domain public cố định thay vì URL ngẫu nhiên `*.trycloudflare.com`. |

### Chạy thử local

```bash
docker build -t zenithcore .
docker run --rm -p 10000:10000 -e CONSOLE_TOKEN='mot-chuoi-that-dai' zenithcore
```

Mở `http://localhost:10000`.

---

## Mở web UI của bất kỳ service nào

Không cần cấu hình gì thêm. Chạy service trong container rồi mở đường dẫn tương ứng:

```bash
# ví dụ: code-server
apt-get install -y curl
curl -fsSL https://code-server.dev/install.sh | sh
code-server --bind-addr 127.0.0.1:8080 --auth none
```

Vào tab **Ports & Web UI** trong console, port 8080 sẽ hiện ra — bấm **Mở UI**, hoặc truy cập thẳng:

```
https://<domain-cua-ban>/p/8080/
```

Reverse proxy tự làm các việc sau:

- chuyển tiếp mọi method HTTP, header, body và streaming
- nâng cấp WebSocket (`/p/8080/ws` → `ws://127.0.0.1:8080/ws`)
- viết lại `href` / `src` / `action` / `url()` bắt đầu bằng `/`
- vá `fetch`, `XMLHttpRequest` và `WebSocket` trong trang để tự thêm prefix
- sửa header `Location` khi service redirect về đường dẫn gốc

Nếu panel có sẵn tuỳ chọn base path thì nên dùng, kết quả chuẩn hơn:

```bash
code-server --abs-proxy-base-path /p/8080
jupyter lab --ServerApp.base_url=/p/8888
```

---

## Dùng proxy trong container từ máy của bạn

Nền tảng host chỉ mở một cổng HTTP công khai, nên proxy TCP không thể phơi trực tiếp.
Console giải quyết bằng bridge TCP-over-WebSocket.

### 1. Dựng proxy trong container

```bash
zenith proxy socks5 1080      # MicroSocks, SOCKS5, không auth
zenith proxy http   8888      # Tinyproxy, HTTP/HTTPS CONNECT
zenith proxy squid  3128      # Squid
zenith proxy status           # xem cái gì đang chạy
zenith proxy stop             # dừng hết
```

### 2. Bắc cầu ra máy bạn

Tải script bridge từ chính console rồi chạy:

```bash
# Python (cần: pip install websockets)
curl -O https://<domain>/static/zenith-bridge.py
python3 zenith-bridge.py \
  --local 1080 \
  --url  wss://<domain>/ws/tcp?port=1080 \
  --token <CONSOLE_TOKEN>
```

```bash
# hoặc Node.js 21+ (không cần cài gì thêm)
curl -O https://<domain>/static/zenith-bridge.js
node zenith-bridge.js 1080 "wss://<domain>/ws/tcp?port=1080" "<CONSOLE_TOKEN>"
```

### 3. Trỏ ứng dụng vào proxy

```bash
curl -x socks5h://127.0.0.1:1080 https://ifconfig.me
export all_proxy=socks5://127.0.0.1:1080
```

Trình duyệt, Telegram, SSH (`ProxyCommand nc -X 5 -x 127.0.0.1:1080 %h %p`)… đều dùng được như proxy bình thường.

Bridge không giới hạn ở proxy — nó chuyển tiếp TCP thuần, nên dùng được cho MySQL, Postgres, Redis, SSH… miễn là service đang nghe trong container.

### Cho container đi qua proxy bên ngoài

```bash
export http_proxy=http://user:pass@host:port
export https_proxy=$http_proxy
export all_proxy=socks5://user:pass@host:port

# hoặc dùng proxychains cho chương trình không hỗ trợ biến môi trường
vi /etc/proxychains4.conf
proxychains4 curl https://ifconfig.me
```

---

## Desktop GUI

Đã bake sẵn trong image và **tự khởi động khi container start** (biến `ZENITH_AUTOSTART_DESKTOP=1`,
mặc định bật) — không cần cài gì thêm hay gõ lệnh, mở domain Railway lên rồi vào:

```
https://<domain>/p/6080/vnc.html?autoconnect=1&resize=remote
```

hoặc bấm tab **Desktop GUI** trong console. VNC nghe ở `5901`, noVNC ở `6080`.

Điều khiển thủ công nếu cần:

```bash
zenith desktop start                 # XFCE, 1440x900
zenith desktop start 1920x1080       # đổi độ phân giải
zenith desktop start --light         # openbox, nhẹ RAM hơn nhiều (mặc định khi autostart)
zenith desktop status
zenith desktop stop
```

Trên gói hosting RAM thấp nên giữ `--light` (openbox). Nếu muốn XFCE đầy đủ khi autostart, đặt
`ZENITH_DESKTOP_LIGHT=0`.

---

## Mở IP/URL public — tất cả các cách

Nền tảng như Railway chỉ cấp **một** domain HTTPS công khai cho container (không có IP tĩnh
riêng cho TCP thuần). Dự án này gộp đủ 4 cách để "mở ra ngoài", dùng cách nào tuỳ nhu cầu:

### Cách 1 — Domain có sẵn của nền tảng + reverse proxy `/p/<port>/`

Không cần cấu hình gì. Mọi service nghe ở `127.0.0.1:<port>` trong container tự động truy cập
được tại `https://<domain>/p/<port>/` (xem mục "Mở web UI của bất kỳ service nào" bên dưới).

### Cách 2 — TCP bridge qua WebSocket (`/ws/tcp?port=`)

Dùng cho giao thức TCP thuần không phải HTTP (SOCKS5, Postgres, Redis, SSH…) đi qua domain
HTTPS có sẵn, không cần Railway mở thêm cổng nào (xem mục "Dùng proxy trong container..." bên dưới).

### Cách 3 — Cloudflare Tunnel (miễn phí, thêm 1 URL public độc lập)

```bash
zenith tunnel start          # quick tunnel: URL ngẫu nhiên *.trycloudflare.com, không cần đăng ký
zenith tunnel status
zenith tunnel stop
```

Muốn domain cố định thay vì URL ngẫu nhiên: tạo **named tunnel** trong
[Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) → lấy token → đặt biến môi
trường `CLOUDFLARE_TUNNEL_TOKEN` trong Railway → `zenith tunnel start` sẽ tự dùng token đó.
Đặt `ZENITH_AUTOSTART_TUNNEL=1` để tunnel tự chạy ngay khi container start.

Ưu điểm so với cách 1/2: tunnel hỗ trợ TCP thật (không cần bridge), domain riêng, không phụ
thuộc giới hạn 1-port của nền tảng.

### Cách 4 — Railway TCP Proxy (tính năng trả phí của Railway, IP:port thật)

Nếu cần một địa chỉ `IP:port` TCP thật do chính Railway cấp (ví dụ để trỏ DNS riêng, dùng cho
client không hỗ trợ WebSocket/HTTP), bật trực tiếp trong Railway dashboard:

1. Vào service → tab **Settings** → mục **Networking**
2. Chọn **TCP Proxy** → nhập port nội bộ trong container (ví dụ `1080` cho SOCKS5, `5901` cho VNC)
3. Railway cấp một `IP:port` public trỏ thẳng vào cổng đó — tính năng này nằm ngoài phạm vi
   Dockerfile/CLI của repo, cấu hình hoàn toàn ở dashboard.

> Ghi chú: TCP Proxy là tính năng riêng của Railway (không phải mọi nền tảng Docker hosting đều
> có). Nếu nền tảng bạn dùng không hỗ trợ, dùng cách 1–3 ở trên.

---

## Lệnh `zenith`

```
zenith ports                  liệt kê cổng đang lắng nghe
zenith expose <port>          in đường dẫn reverse proxy
zenith proxy   <socks5|http|squid|status|stop> [port]
zenith desktop <start|stop|status|install> [WxH] [--light]
zenith tunnel  <start|stop|status> [url nội bộ]
```

---

## Bảo mật

- Đăng nhập một lần bằng `CONSOLE_TOKEN`, sau đó dùng cookie phiên ký HMAC-SHA256 (hạn 7 ngày).
- Mọi đường dẫn `/p/...`, `/api/...`, `/ws/...` đều yêu cầu phiên hợp lệ.
- So sánh token bằng `hmac.compare_digest` để tránh timing attack.
- Cookie `HttpOnly`, `SameSite=Lax`.

> Bất kỳ ai có token đều có quyền root trong container **và** dùng được reverse proxy + TCP bridge.
> Hãy dùng token dài, ngẫu nhiên và không chia sẻ.

---

## Giới hạn thực tế

Trong container bạn có toàn quyền root: cài gói, chạy service, mở cổng nội bộ tuỳ ý.
Console đã gỡ giới hạn "chỉ một cổng public" của nền tảng host.

Những thứ vẫn **không** làm được vì thuộc về kernel/hạ tầng của host:

- nạp kernel module, sửa kernel
- KVM / nested virtualization / Docker-in-Docker đặc quyền
- mở thêm cổng công khai ngoài cổng được cấp
- systemd hoạt động đầy đủ như trên VPS thật

Hệ thống file thường bị reset sau mỗi lần deploy — lưu dữ liệu quan trọng ra ngoài.

---

## Cấu trúc

```
server.py                  FastAPI: PTY, reverse proxy, TCP bridge, API hệ thống
zenith                     CLI trong container: proxy, desktop, ports
start.sh                   entrypoint, tự sinh token nếu thiếu
Dockerfile                 Debian 13 + proxy stack + build tools
static/index.html          giao diện console
static/app.css             hệ thiết kế
static/app.js              logic front-end
static/zenith-bridge.py    bridge TCP-over-WS (Python)
static/zenith-bridge.js    bridge TCP-over-WS (Node.js)
```
