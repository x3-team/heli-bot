#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
PROFILE_DIR="${PROFILE_DIR:-/profile}"
mkdir -p "$PROFILE_DIR"

Xvfb "$DISPLAY" -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
sleep 0.4
x11vnc -display "$DISPLAY" -nopw -listen 127.0.0.1 -rfbport 5900 -forever -shared >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &

CHROME="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
if [[ -z "$CHROME" ]]; then
  CHROME="$(find /ms-playwright -type f -name chrome -path '*chrome-linux*' | head -n 1 || true)"
fi
if [[ -z "$CHROME" ]]; then
  echo "chromium binary not found" >&2
  exit 1
fi

exec "$CHROME" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --window-size=1280,800 \
  about:blank
