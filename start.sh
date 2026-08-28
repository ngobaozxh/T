#!/bin/bash
set -e

PORT="${PORT:-10000}"
CONSOLE_TOKEN="${CONSOLE_TOKEN:-}"
TOKEN_FILE=/var/lib/zenith/console_token

mkdir -p /run/sshd /var/lib/zenith /var/log/zenith /root

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

echo "ZenithCore Console starting on 0.0.0.0:$PORT"

exec /opt/venv/bin/uvicorn server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --ws websockets \
  --timeout-keep-alive 75 \
  --no-access-log \
  --proxy-headers \
  --forwarded-allow-ips '*'
