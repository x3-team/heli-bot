#!/usr/bin/env bash
# Deploy Heli bot API on pinvds (5.188.88.202). Runs on the host, started by
# .github/workflows/deploy-pinvds.yml after it copied the release tarball.
#
#   deploy.sh diff   <tarball>   show what differs from the running copy, change nothing
#   deploy.sh deploy <tarball>   back up, sync, pip, alembic, restart, health check, roll back on failure
#
# Touches only /opt/heli-bot and the heli-bot-api unit. getheli.ru (/opt/heli), nginx and certificates
# are never touched. The .env, var/ (database, browser profiles, files) and the venv are kept.
set -euo pipefail

MODE="${1:?mode: diff|deploy}"
TARBALL="${2:?release tarball}"
APP_DIR="${APP_DIR:-/opt/heli-bot}"
APP_USER="${APP_USER:-helibot}"
UNIT="${UNIT:-heli-bot-api}"
PORT="${PORT:-8001}"
PUBLIC_HEALTH="${PUBLIC_HEALTH:-https://bot-api.getheli.ru/health}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"

log() { echo "[pinvds-deploy] $*"; }
SUDO=""
if [ "$(id -u)" != 0 ]; then SUDO="sudo -n"; fi
# Run as the app user. With env_file: with the variables of .env (which may be readable by root only).
as_app() { $SUDO runuser -u "$APP_USER" -- "$@"; }
as_app_with_env() {
  $SUDO bash -c 'set -a; . "$0"; set +a; shift; exec runuser -u "$1" -- "${@:2}"' "$APP_DIR/.env" _ "$APP_USER" "$@"
}

[ -d "$APP_DIR/server" ] || { log "no $APP_DIR/server on this host"; exit 1; }
[ -f "$APP_DIR/.env" ] || { log "no $APP_DIR/.env on this host"; exit 1; }

STAGE="$(mktemp -d /tmp/heli-release.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$TARBALL" -C "$STAGE"
REV="$(cat "$STAGE/server/REVISION" 2>/dev/null || echo unknown)"
log "release $REV"

RSYNC_EXCLUDES=(--exclude '__pycache__/' --exclude '*.pyc' --exclude '*.egg-info/' --exclude '.pytest_cache/')

if [ "$MODE" = diff ]; then
  log "running copy: $(cat "$APP_DIR/server/REVISION" 2>/dev/null || echo 'no REVISION (copied by hand)')"
  log "files that would change (rsync dry run):"
  $SUDO rsync -rcn --delete --itemize-changes "${RSYNC_EXCLUDES[@]}" "$STAGE/server/" "$APP_DIR/server/" | sed 's/^/  /' || true
  log "content diff (first 400 lines):"
  diff -ru -x '__pycache__' -x '*.pyc' -x '*.egg-info' "$APP_DIR/server" "$STAGE/server" | head -n 400 || true
  exit 0
fi

[ "$MODE" = deploy ] || { log "unknown mode $MODE"; exit 1; }

# 1. Back up the running copy (the first one was copied by hand and is not in git).
BACKUPS="$APP_DIR/backups"
$SUDO mkdir -p "$BACKUPS"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUPS/server-$STAMP.tgz"
$SUDO tar -czf "$BACKUP" -C "$APP_DIR" --exclude '__pycache__' server
log "backup: $BACKUP"
if [ -f "$APP_DIR/var/heli.db" ]; then
  $SUDO cp -p "$APP_DIR/var/heli.db" "$BACKUPS/heli-$STAMP.db"
  log "sqlite backup: $BACKUPS/heli-$STAMP.db"
fi
# Keep the last KEEP_BACKUPS of each kind.
for pattern in 'server-*.tgz' 'heli-*.db'; do
  # shellcheck disable=SC2012
  ls -1t "$BACKUPS"/$pattern 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r $SUDO rm -f
done

rollback() {
  log "ROLLBACK to $BACKUP"
  $SUDO rm -rf "$APP_DIR/server.failed"
  $SUDO mv "$APP_DIR/server" "$APP_DIR/server.failed"
  $SUDO mkdir -p "$APP_DIR/server"
  $SUDO tar -xzf "$BACKUP" -C "$APP_DIR"
  $SUDO chown -R "$APP_USER:$APP_USER" "$APP_DIR/server"
  as_app "$APP_DIR/venv/bin/python" -m pip install -q -e "$APP_DIR/server" || true
  $SUDO systemctl restart "$UNIT" || true
  log "rolled back; the failed tree is in $APP_DIR/server.failed"
}

# 2. Sync the code. .env, var/, venv/ live outside server/ and are not touched.
$SUDO rsync -rc --delete "${RSYNC_EXCLUDES[@]}" "$STAGE/server/" "$APP_DIR/server/"
$SUDO chown -R "$APP_USER:$APP_USER" "$APP_DIR/server"

# 3. Dependencies (with Playwright when the cloud browser is on) and migrations.
EXTRA=""
if $SUDO grep -Eq '^CLOUD_BROWSER_ENABLED=(1|true|yes)' "$APP_DIR/.env"; then EXTRA="[cloud]"; fi
if ! as_app "$APP_DIR/venv/bin/python" -m pip install -q -e "$APP_DIR/server$EXTRA"; then
  rollback; exit 1
fi
if ! as_app_with_env bash -c "cd '$APP_DIR/server' && exec '$APP_DIR/venv/bin/alembic' upgrade head"; then
  log "alembic failed"; rollback; exit 1
fi

# 4. Restart and check.
$SUDO systemctl restart "$UNIT"
ok=0
for attempt in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  log "local health check failed"
  $SUDO journalctl -u "$UNIT" -n 60 --no-pager || true
  rollback; exit 1
fi
log "local health OK"
if curl -fsS --max-time 15 "$PUBLIC_HEALTH" >/dev/null 2>&1; then
  log "public health OK: $PUBLIC_HEALTH"
else
  log "WARNING: $PUBLIC_HEALTH did not answer (nginx/TLS are not touched by this deploy)"
fi
curl -fsS "http://127.0.0.1:$PORT/health" || true
echo
log "deployed $REV"
