#!/usr/bin/env bash
# Create /opt/heli-bot/.env from CI-injected env vars when the file is absent. Never echo secrets.
set -euo pipefail

ENV_FILE="${1:?env file path}"
APP_DIR="${2:?app dir}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ -f "$ENV_FILE" ]; then
  log ".env already exists at $ENV_FILE (not overwritten)"
  exit 0
fi

missing=""
for name in HELI_DEPLOY_JWT_SECRET HELI_DEPLOY_TRAJECTORY_HASH_PEPPER HELI_API_KEY; do
  if [ -z "${!name:-}" ]; then
    missing="$missing $name"
  fi
done
if [ -n "$missing" ]; then
  log "cannot create .env: missing GitHub secrets:$missing"
  exit 1
fi

mkdir -p "$(dirname "$ENV_FILE")" "$APP_DIR/var"

set +x
umask 077
cat > "$ENV_FILE" <<EOF
APP_ENV=production
HOST=127.0.0.1
PORT=8001
DATABASE_URL=sqlite:///${APP_DIR}/var/heli.db
JWT_SECRET=${HELI_DEPLOY_JWT_SECRET}
TRAJECTORY_HASH_PEPPER=${HELI_DEPLOY_TRAJECTORY_HASH_PEPPER}
HELI_API_KEY=${HELI_API_KEY}
HELI_BASE_URL=${HELI_BASE_URL:-https://getheli.ru/v1}
DAILY_LIMIT_RUB=${DAILY_LIMIT_RUB:-1000}
DEFAULT_MONTHLY_LIMIT_RUB=${DEFAULT_MONTHLY_LIMIT_RUB:-1000}
EOF
chmod 600 "$ENV_FILE"
log "created $ENV_FILE (mode 600)"
