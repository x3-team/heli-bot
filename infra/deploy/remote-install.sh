#!/usr/bin/env bash
# Run on the shared host as helibot (no root, no docker). APP_DIR defaults to $HOME.
set -euo pipefail
set -x
exec 2>&1

log() {
  echo "[heli-deploy] $*" >&2
}

on_err() {
  log "FAILED at line $1 (exit $2)"
}
trap 'on_err $LINENO $?' ERR

APP_DIR="${1:-$HOME}"
ENV_FILE="${2:-$APP_DIR/.env}"

log "APP_DIR=$APP_DIR ENV_FILE=$ENV_FILE"

if [ ! -d "$APP_DIR/server" ]; then
  log "missing $APP_DIR/server (sync server/ to APP_DIR first)"
  exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  log "missing env file: $ENV_FILE (copy infra/deploy/env.venv.example to .env on the host)"
  exit 1
fi

cd "$APP_DIR"
mkdir -p "$APP_DIR/var" "$APP_DIR/.config/systemd/user"

if ! command -v python3 >/dev/null 2>&1; then
  log "python3 is required on the host"
  exit 1
fi

ensure_venv() {
  if [ -x "$APP_DIR/venv/bin/python" ]; then
    log "reusing venv at $APP_DIR/venv"
    return 0
  fi
  log "creating venv at $APP_DIR/venv"
  if python3 -m venv "$APP_DIR/venv" 2>&1; then
    return 0
  fi
  log "python3 -m venv failed (often missing python3-venv); bootstrapping with --without-pip"
  python3 -m venv --without-pip "$APP_DIR/venv"
  # shellcheck disable=SC1091
  source "$APP_DIR/venv/bin/activate"
  curl -fsSL https://bootstrap.pypa.io/pip/get-pip.py -o /tmp/heli-get-pip.py
  python /tmp/heli-get-pip.py
  rm -f /tmp/heli-get-pip.py
}

ensure_venv
# shellcheck disable=SC1091
source "$APP_DIR/venv/bin/activate"

export PIP_NO_CACHE_DIR=1
python -m pip install -U pip wheel
python -m pip install -e "$APP_DIR/server"

set +u
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
set -u

export DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/var/heli.db}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export APP_ENV="${APP_ENV:-production}"

log "DATABASE_URL=$DATABASE_URL HOST=$HOST PORT=$PORT"

cd "$APP_DIR/server"
alembic upgrade head

install -m 0644 "$APP_DIR/infra/deploy/systemd-user/helibot.slice" "$APP_DIR/.config/systemd/user/helibot.slice"
install -m 0644 "$APP_DIR/infra/deploy/systemd-user/heli-bot-api.service" "$APP_DIR/.config/systemd/user/heli-bot-api.service"

if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  log "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
fi

if ! systemctl --user daemon-reload 2>&1; then
  log "USER_SYSTEMD_UNAVAILABLE: enable linger for helibot or install infra/deploy/systemd-system/ as root"
  exit 2
fi

systemctl --user enable heli-bot-api.service
systemctl --user restart heli-bot-api.service
systemctl --user --no-pager status heli-bot-api.service || true

for attempt in 1 2 3 4 5; do
  if curl -fsS "http://${HOST}:${PORT}/health" >/dev/null; then
    log "Heli bot API is up on http://${HOST}:${PORT}"
    exit 0
  fi
  log "health check attempt $attempt failed; retrying..."
  sleep 2
done

log "health check failed after restart"
journalctl --user -u heli-bot-api.service -n 30 --no-pager 2>&1 || true
exit 1
