#!/bin/bash
set -e

PORT="${PORT:-8888}"
CONSOLE_TOKEN="${CONSOLE_TOKEN:-}"
STATE_DIR="${ZENITH_STATE:-${HOME:-/tmp}/.zenith}"
LOG_DIR="${ZENITH_LOG:-${STATE_DIR}/log}"
TOKEN_FILE="$STATE_DIR/console_token"

mkdir -p /run/sshd /root 2>/dev/null || true
if ! mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null; then
  STATE_DIR="/tmp/zenith"
  LOG_DIR="/tmp/zenith/log"
  mkdir -p "$STATE_DIR" "$LOG_DIR" || true
fi

if [ -z "$CONSOLE_TOKEN" ] || [ "$CONSOLE_TOKEN" = "change-me" ]; then
  if [ -s "$TOKEN_FILE" ]; then
    CONSOLE_TOKEN="$(cat "$TOKEN_FILE")"
  else
    CONSOLE_TOKEN="$(openssl rand -hex 24)"
    printf '%s' "$CONSOLE_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
  fi
  echo "=============================================================="
  echo " CONSOLE_TOKEN chưa được đặt — đã sinh token tạm thời:"
  echo ""
  echo "     $CONSOLE_TOKEN"
  echo ""
  echo " Token này sẽ đổi khi deploy lại. Hãy đặt biến môi trường"
  echo " CONSOLE_TOKEN trong dashboard để cố định."
  echo "=============================================================="
fi

export CONSOLE_TOKEN

# --- auto-start GUI desktop (XFCE/Openbox + TigerVNC + noVNC) ---------------
# Mirrors the railway-ubuntu-novnc / docker-ubuntu-vnc-desktop pattern: the
# desktop is already baked into the image, so it comes up by itself and is
# reachable at /p/6080/vnc.html the moment the container is healthy.
if [ "${ZENITH_AUTOSTART_DESKTOP:-1}" = "1" ]; then
  (
    sleep 1
    args=(desktop start "${ZENITH_DESKTOP_GEOMETRY:-1440x900}")
    [ "${ZENITH_DESKTOP_LIGHT:-1}" = "1" ] && args+=(--light)
    /usr/local/bin/zenith "${args[@]}" >>"$LOG_DIR/autostart-desktop.log" 2>&1 \
      || echo "[autostart] desktop start thất bại, xem $LOG_DIR/autostart-desktop.log" >&2
  ) &
fi

# --- auto-start public tunnel (Cloudflare) -----------------------------------
# Optional: gives a second, independent public URL besides the platform's own
# domain. Set ZENITH_AUTOSTART_TUNNEL=1 (+ optionally CLOUDFLARE_TUNNEL_TOKEN
# for a named/persistent tunnel) to enable.
if [ "${ZENITH_AUTOSTART_TUNNEL:-0}" = "1" ]; then
  (
    sleep 2
    /usr/local/bin/zenith tunnel start >>"$LOG_DIR/autostart-tunnel.log" 2>&1 \
      || echo "[autostart] tunnel start thất bại, xem $LOG_DIR/autostart-tunnel.log" >&2
  ) &
fi

echo "ZenithCore Console starting on 0.0.0.0:$PORT"

exec /opt/venv/bin/uvicorn server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --ws websockets \
  --timeout-keep-alive 75 \
  --no-access-log \
  --proxy-headers \
  --forwarded-allow-ips '*'
