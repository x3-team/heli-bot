#!/usr/bin/env bash
# Run on the shared host as helibot (no root, no docker). APP_DIR defaults to $HOME.
set -eu

APP_DIR="${1:-$HOME}"
ENV_FILE="${2:-$APP_DIR/.env}"

cd "$APP_DIR"
test -d server
test -f "$ENV_FILE"

mkdir -p "$APP_DIR/var" "$APP_DIR/.config/systemd/user"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required on the host" >&2
  exit 1
fi

if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
# shellcheck disable=SC1091
source "$APP_DIR/venv/bin/activate"
python -m pip install -U pip wheel
python -m pip install -e "$APP_DIR/server"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/var/heli.db}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export APP_ENV="${APP_ENV:-production}"

cd "$APP_DIR/server"
alembic upgrade head

install -m 0644 "$APP_DIR/infra/deploy/systemd-user/helibot.slice" "$APP_DIR/.config/systemd/user/helibot.slice"
install -m 0644 "$APP_DIR/infra/deploy/systemd-user/heli-bot-api.service" "$APP_DIR/.config/systemd/user/heli-bot-api.service"

if ! systemctl --user daemon-reload; then
  echo "USER_SYSTEMD_UNAVAILABLE: enable linger for helibot or install the system unit from infra/deploy/systemd-system/" >&2
  exit 2
fi

systemctl --user enable helibot.slice heli-bot-api.service
systemctl --user restart heli-bot-api.service
systemctl --user --no-pager status heli-bot-api.service || true

curl -fsS "http://${HOST}:${PORT}/health" >/dev/null
echo "Heli bot API is up on ${HOST}:${PORT}"
