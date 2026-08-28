FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color

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
      # --- build tooling so users can compile anything they need ---
      build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

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
      "  zenith desktop start    chạy desktop GUI + noVNC" \
      "  Mọi cổng nội bộ mở qua  /p/<PORT>/" \
      "" \
      > /etc/zenith-motd

EXPOSE 10000

CMD ["/app/start.sh"]
