FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
    ZENITH_AUTOSTART_DESKTOP=1 \
    ZENITH_DESKTOP_GEOMETRY=1440x900 \
    ZENITH_DESKTOP_LIGHT=1

WORKDIR /app

# --- base system, networking, build and proxy tooling ---------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      bash sudo ca-certificates locales tzdata \
      curl wget git openssh-client rsync \
      nano vim less htop tree ncdu file jq \
      unzip zip tar gzip bzip2 xz-utils \
      lsof procps psmisc \
      iproute2 iputils-ping net-tools dnsutils traceroute \
      socat netcat-openbsd \
      gnupg openssl cron logrotate \
      pciutils usbutils kmod \
      systemd systemd-sysv dbus dbus-user-session \
      # --- proxy stack ---
      microsocks tinyproxy privoxy squid proxychains4 redsocks \
      # --- GUI desktop stack, baked in so the desktop is ready at boot ---
      # (same idea as railway-ubuntu-novnc / docker-ubuntu-vnc-desktop templates:
      #  X11 + VNC + noVNC packaged into the image, no on-demand apt-get needed)
      openbox tigervnc-standalone-server tigervnc-common \
      novnc websockify xterm x11-xserver-utils dbus-x11 fonts-dejavu-core \
      # --- build tooling so users can compile anything they need ---
      build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

# cloudflared: free, tokenless "quick tunnel" (trycloudflare.com) or a named
# tunnel (with CLOUDFLARE_TUNNEL_TOKEN) to get a real public HTTPS/TCP URL,
# independent from the platform's own public domain.
RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) cf_url=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 ;; \
      arm64) cf_url=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 ;; \
      *)     cf_url="" ;; \
    esac; \
    if [ -n "$cf_url" ]; then \
      curl -fsSL "$cf_url" -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared; \
    fi

# Proxies must not autostart; the zenith CLI manages them on demand.
RUN for svc in microsocks tinyproxy privoxy squid redsocks; do \
      [ -x "/etc/init.d/$svc" ] && update-rc.d -f "$svc" remove || true; \
    done; \
    systemctl disable microsocks tinyproxy privoxy squid redsocks 2>/dev/null || true

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY server.py /app/server.py
COPY start.sh  /app/start.sh
COPY zenith    /usr/local/bin/zenith
COPY static    /app/static

RUN chmod +x /app/start.sh /usr/local/bin/zenith && \
    mkdir -p /root /var/lib/zenith /var/log/zenith && \
    printf '%s\n' \
      "export PATH=\$PATH:/opt/venv/bin" \
      "alias ll='ls -alF --color=auto'" \
      "alias ports='zenith ports'" \
      "[ -z \"\$ZENITH_MOTD\" ] && export ZENITH_MOTD=1 && cat /etc/zenith-motd 2>/dev/null" \
      >> /root/.bashrc && \
    printf '%s\n' \
      "" \
      "  ZenithCore  ·  Debian 13" \
      "  zenith ports            cổng đang lắng nghe" \
      "  zenith proxy socks5     dựng SOCKS5 trên 1080" \
      "  zenith desktop start    chạy desktop GUI + noVNC (đã tự bật, xem /p/6080/vnc.html)" \
      "  zenith tunnel start     mở URL public bằng Cloudflare Tunnel (miễn phí)" \
      "  Mọi cổng nội bộ mở qua  /p/<PORT>/" \
      "" \
      > /etc/zenith-motd

EXPOSE 10000

CMD ["/app/start.sh"]
