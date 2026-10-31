#!/usr/bin/env bash
# If .env exists, set PORT only (default 8001). Do not modify other keys.
set -euo pipefail

ENV_FILE="${1:?env file path}"
PORT="${2:-8001}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  log "no env file at $ENV_FILE (skip port patch)"
  exit 0
fi

tmp="$(mktemp)"
if grep -q '^PORT=' "$ENV_FILE"; then
  sed "s/^PORT=.*/PORT=${PORT}/" "$ENV_FILE" >"$tmp"
else
  cp "$ENV_FILE" "$tmp"
  printf '\nPORT=%s\n' "$PORT" >>"$tmp"
fi
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
log "set PORT=${PORT} in existing env file (other keys unchanged)"
