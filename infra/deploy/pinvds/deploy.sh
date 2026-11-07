#!/usr/bin/env bash
# Deploy Heli bot API on pinvds (5.188.88.202). Runs on the host, started by
# .github/workflows/deploy-pinvds.yml after it copied the release tarball.
#
#   deploy.sh diff   <tarball>   show what differs from the running copy, change nothing
#   deploy.sh deploy <tarball>   back up, sync, pip, alembic, restart, health check, roll back on failure
#   deploy.sh learning-check <tarball>
#                                read-only: run journal, SFT/DPO export and /admin/quality as counts
#                                (code from the tarball, data from the running copy; nothing changes)
#
# Touches only /opt/heli-bot and the heli-bot-api unit. getheli.ru (/opt/heli), nginx and certificates
# are never touched. The .env, var/ (database, browser profiles, files) and the venv are kept.
set -euo pipefail

MODE="${1:?mode: diff|deploy|learning-check}"
TARBALL="${2:?release tarball}"
APP_DIR="${APP_DIR:-/opt/heli-bot}"
APP_USER="${APP_USER:-helibot}"
UNIT="${UNIT:-heli-bot-api}"
PORT="${PORT:-8001}"
PUBLIC_HEALTH="${PUBLIC_HEALTH:-https://bot-api.getheli.ru/health}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"

log() { echo "[pinvds-deploy] $*"; }
# Three ways in: root; the app user itself (then only the restart needs a sudoers rule); or another
# user with passwordless sudo.
SUDO=""
SVC="systemctl"
JOURNAL="journalctl"
ME="$(id -un)"
if [ "$(id -u)" = 0 ]; then
  :
elif [ "$ME" = "$APP_USER" ]; then
  SVC="sudo -n systemctl"
  JOURNAL="sudo -n journalctl"
else
  SUDO="sudo -n"
  SVC="sudo -n systemctl"
  JOURNAL="sudo -n journalctl"
fi
log "deploy user: $ME"
command -v rsync >/dev/null 2>&1 || { log "rsync is not installed on the host (apt-get install rsync)"; exit 1; }
command -v ss >/dev/null 2>&1 || { log "ss is not installed on the host (apt-get install iproute2)"; exit 1; }
as_app() {
  if [ "$ME" = "$APP_USER" ]; then "$@"; else $SUDO runuser -u "$APP_USER" -- "$@"; fi
}
as_app_with_env() {
  if [ "$ME" = "$APP_USER" ]; then
    bash -c 'set -a; . "$0"; set +a; exec "$@"' "$APP_DIR/.env" "$@"
  else
    $SUDO bash -c 'set -a; . "$0"; set +a; shift; exec runuser -u "$1" -- "${@:2}"' "$APP_DIR/.env" _ "$APP_USER" "$@"
  fi
}
if [ "$MODE" = deploy ] && ! $SVC --version >/dev/null 2>&1; then
  log "this user cannot run '$SVC'. Add a sudoers rule, e.g. /etc/sudoers.d/heli-bot-deploy:"
  log "  $ME ALL=(root) NOPASSWD: /bin/systemctl restart $UNIT, /bin/systemctl --version, /bin/journalctl -u $UNIT *"
  exit 1
fi

# The API must run only under the unit. A process started by hand (python -m app.main) that still holds
# the port would keep serving the old code and make the health check lie, so it is stopped first.
unit_owns() { grep -q "/$UNIT.service" "/proc/$1/cgroup" 2>/dev/null; }
port_pids() { ss -Hltnp "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
port_busy() { [ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]; }
orphan_pids() { for pid in $(port_pids); do unit_owns "$pid" || echo "$pid"; done; }
describe_pid() { ps -o pid=,user=,etime=,args= -p "$1" 2>/dev/null | sed 's/^ *//' || echo "$1"; }
stop_orphans() {
  local pids pid
  pids="$(orphan_pids)"
  [ -n "$pids" ] || return 0
  for pid in $pids; do
    log "stopping process outside $UNIT on port $PORT: $(describe_pid "$pid")"
    kill "$pid" 2>/dev/null || $SUDO kill "$pid" 2>/dev/null || {
      log "cannot stop pid $pid as $ME (it belongs to $(ps -o user= -p "$pid" 2>/dev/null))"; return 1; }
  done
  for _ in $(seq 1 10); do [ -z "$(orphan_pids)" ] && return 0; sleep 1; done
  for pid in $(orphan_pids); do kill -9 "$pid" 2>/dev/null || $SUDO kill -9 "$pid" 2>/dev/null || true; done
  sleep 1
  [ -z "$(orphan_pids)" ]
}

[ -d "$APP_DIR/server" ] || { log "no $APP_DIR/server on this host"; exit 1; }
[ -f "$APP_DIR/.env" ] || { log "no $APP_DIR/.env on this host"; exit 1; }

STAGE="$(mktemp -d /tmp/heli-release.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$TARBALL" -C "$STAGE"
REV="$(cat "$STAGE/server/REVISION" 2>/dev/null || echo unknown)"
log "release $REV"

# server/var and any .env inside server/ belong to the host (database, profiles, files): never synced or deleted.
RSYNC_EXCLUDES=(--exclude '__pycache__/' --exclude '*.pyc' --exclude '*.egg-info/' --exclude '.pytest_cache/'
  --exclude '/var/' --exclude '/.env' --exclude '/backups/')

if [ "$MODE" = diff ]; then
  log "unit $UNIT: $(systemctl is-active "$UNIT" 2>/dev/null || true)"
  for pid in $(port_pids); do
    if unit_owns "$pid"; then log "port $PORT: $(describe_pid "$pid") (in $UNIT)";
    else log "port $PORT: $(describe_pid "$pid") OUTSIDE $UNIT, deploy will stop it"; fi
  done
  if port_busy && [ -z "$(port_pids)" ]; then log "port $PORT is busy, owner not visible to $ME"; fi
  log "running copy: $(cat "$APP_DIR/server/REVISION" 2>/dev/null || echo 'no REVISION (copied by hand)')"
  log "files that would change (rsync dry run):"
  $SUDO rsync -rcn --delete --itemize-changes "${RSYNC_EXCLUDES[@]}" "$STAGE/server/" "$APP_DIR/server/" | sed 's/^/  /' || true
  log "content diff (first 400 lines):"
  $SUDO diff -ru -x '__pycache__' -x '*.pyc' -x '*.egg-info' -x var -x .env "$APP_DIR/server" "$STAGE/server" | head -n 400 || true
  exit 0
fi

if [ "$MODE" = learning-check ]; then
  # Read-only. The release code is put first on sys.path, the working directory and .env are the running
  # copy's, so it reads the live database. Prints only counts and the quality summary.
  log "running copy: $(cat "$APP_DIR/server/REVISION" 2>/dev/null || echo unknown)"
  chmod -R a+rX "$STAGE"
  as_app_with_env bash -c "cd '$APP_DIR/server' && exec '$APP_DIR/venv/bin/python' -c \"import sys; sys.path.insert(0, '$STAGE/server'); sys.argv = ['heli-server', 'learning-check', '--api', 'http://127.0.0.1:$PORT']; from app.cli import main; main()\""
  exit $?
fi

[ "$MODE" = deploy ] || { log "unknown mode $MODE"; exit 1; }

# 1. Back up the running copy (the first one was copied by hand and is not in git).
BACKUPS="$APP_DIR/backups"
$SUDO mkdir -p "$BACKUPS" 2>/dev/null || mkdir -p "$BACKUPS"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUPS/server-$STAMP.tgz"
$SUDO tar -czf "$BACKUP" -C "$APP_DIR" --exclude '__pycache__' --exclude 'server/var' server
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
  $SUDO mkdir -p "$APP_DIR/server.failed"
  $SUDO rsync -a "${RSYNC_EXCLUDES[@]}" "$APP_DIR/server/" "$APP_DIR/server.failed/"
  RESTORE="$(mktemp -d /tmp/heli-restore.XXXXXX)"
  $SUDO tar -xzf "$BACKUP" -C "$RESTORE"
  $SUDO rsync -rc --delete "${RSYNC_EXCLUDES[@]}" "$RESTORE/server/" "$APP_DIR/server/"
  $SUDO rm -rf "$RESTORE"
  if [ "$ME" != "$APP_USER" ]; then $SUDO chown -R "$APP_USER:$APP_USER" "$APP_DIR/server" || true; fi
  as_app "$APP_DIR/venv/bin/python" -m pip install -q -e "$APP_DIR/server" || true
  $SVC restart "$UNIT" || true
  log "rolled back; the failed tree is in $APP_DIR/server.failed"
}

# 2. Sync the code. .env, var/, venv/ live outside server/ and are not touched.
$SUDO rsync -rc --delete "${RSYNC_EXCLUDES[@]}" "$STAGE/server/" "$APP_DIR/server/"
if [ "$ME" != "$APP_USER" ]; then $SUDO chown -R "$APP_USER:$APP_USER" "$APP_DIR/server"; fi

# 3. Dependencies (with Playwright when the cloud browser is on) and migrations.
EXTRA=""
if $SUDO grep -Eq '^CLOUD_BROWSER_ENABLED=(1|true|yes)' "$APP_DIR/.env"; then EXTRA="[cloud]"; fi
if ! as_app "$APP_DIR/venv/bin/python" -m pip install -q -e "$APP_DIR/server$EXTRA"; then
  rollback; exit 1
fi
if ! as_app_with_env bash -c "cd '$APP_DIR/server' && exec '$APP_DIR/venv/bin/alembic' upgrade head"; then
  log "alembic failed"; rollback; exit 1
fi

# 4. Restart under the unit and check that the unit, not a stray process, answers.
if ! stop_orphans; then log "a process outside $UNIT still holds port $PORT"; rollback; exit 1; fi
$SVC restart "$UNIT"
ok=0
for attempt in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    if [ -n "$(port_pids)" ] && [ -z "$(orphan_pids)" ] && systemctl is-active --quiet "$UNIT"; then ok=1; break; fi
  fi
  sleep 2
done
log "unit $UNIT: $(systemctl is-active "$UNIT" 2>/dev/null || true); port $PORT: $(for p in $(port_pids); do describe_pid "$p"; done | tr '\n' ';')"
if [ "$ok" != 1 ]; then
  log "local health check failed"
  $JOURNAL -u "$UNIT" -n 60 --no-pager || true
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
