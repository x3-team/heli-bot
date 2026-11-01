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

log "host: $(nproc 2>/dev/null || echo '?') CPU"
free -m 2>/dev/null || true

# Heli's own browser needs ~1.2 GB free. On a host with less, keep it off so getheli and Fili are not squeezed.
CLOUD_MIN_FREE_MB="${CLOUD_MIN_FREE_MB:-1200}"
FREE_MB="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
log "host memory available: ${FREE_MB} MB (cloud browser needs ${CLOUD_MIN_FREE_MB} MB)"
if grep -Eq '^CLOUD_BROWSER_ENABLED=(1|true|yes)' "$ENV_FILE" && [ "$FREE_MB" -lt "$CLOUD_MIN_FREE_MB" ]; then
  log "CLOUD_BROWSER_SKIPPED_LOW_MEMORY: setting CLOUD_BROWSER_ENABLED=0 and keeping the 256 MB cap"
  sed -i 's/^CLOUD_BROWSER_ENABLED=.*/CLOUD_BROWSER_ENABLED=0/' "$ENV_FILE"
fi
CLOUD_ON=0
if grep -Eq '^CLOUD_BROWSER_ENABLED=(1|true|yes)' "$ENV_FILE"; then CLOUD_ON=1; fi

# Heli's own browser on this host (CLOUD_BROWSER_ENABLED=1 in .env): Playwright + Chromium, no root.
if [ "$CLOUD_ON" = 1 ]; then
  log "cloud browser: installing Playwright Chromium"
  python -m pip install -e "$APP_DIR/server[cloud]"
  export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/var/ms-playwright"
  python -m playwright install chromium-headless-shell || python -m playwright install chromium
  # No root on this host: fetch the system libraries Chromium lacks as .deb files and unpack them
  # into var/chromium-libs. The app passes that folder to Chromium as LD_LIBRARY_PATH.
  LIB_ROOT="$APP_DIR/var/chromium-libs"
  CHROME_BIN="$(find "$PLAYWRIGHT_BROWSERS_PATH" -type f \( -name headless_shell -o -name chrome-headless-shell -o -name chrome \) | head -n 1 || true)"
  missing_libs() {
    LD_LIBRARY_PATH="$LIB_ROOT/usr/lib/x86_64-linux-gnu:$LIB_ROOT/lib/x86_64-linux-gnu" ldd "$CHROME_BIN" 2>/dev/null | awk '/not found/ {print $1}' || true
  }
  if [ -n "$CHROME_BIN" ] && [ -n "$(missing_libs)" ]; then
    # Playwright's list misses a few the headless shell links to on Ubuntu 24.04 (libxi6).
    PKGS="libxi6 $(python -m playwright install-deps --dry-run chromium 2>/dev/null | grep -o 'apt-get install.*' | tail -n 1 | sed -e 's/apt-get install//' -e 's/--no-install-recommends//' -e 's/-y//' | tr ' ' '\n' | grep -E '^[a-z0-9][a-z0-9.+-]+$' | sort -u | tr '\n' ' ' || true)"
    log "cloud browser: unpacking libraries without root: $PKGS"
    DEB_DIR="$(mktemp -d)"
    for round in 1 2; do
      if [ -z "$(missing_libs)" ]; then break; fi
      (cd "$DEB_DIR" && for pkg in $PKGS; do apt-get download "$pkg" >/dev/null 2>&1 || log "apt-get download $pkg failed"; done)
      for deb in "$DEB_DIR"/*.deb; do
        [ -f "$deb" ] && dpkg-deb -x "$deb" "$LIB_ROOT"
      done
      # Second round: packages that provide what is still missing, found through apt-cache.
      PKGS=""
      for lib in $(missing_libs); do
        # libXi.so.6 -> libxi6: package names are lower case, soname major appended.
        base="$(echo "$lib" | sed -E 's/^lib//; s/\.so.*//' | tr 'A-Z' 'a-z')"
        cand="$(apt-cache pkgnames 2>/dev/null | grep -E "^lib${base}[0-9.t-]*$" | sort | head -n 1 || true)"
        [ -n "$cand" ] && PKGS="$PKGS $cand"
      done
    done
    rm -rf "$DEB_DIR"
  fi
  if [ -n "$CHROME_BIN" ]; then
    STILL="$(missing_libs)"
    if [ -n "$STILL" ]; then
      log "CLOUD_BROWSER_LIBS_MISSING (ask the host admin: sudo $APP_DIR/venv/bin/python -m playwright install-deps chromium):"
      echo "$STILL"
    else
      log "cloud browser: Chromium has every library it needs"
    fi
    # Start Chromium once, the way the API will, and print why if it does not start.
    (cd "$APP_DIR/server" && CLOUD_BROWSER_ENABLED=1 CLOUD_BROWSER_LIBS="$LIB_ROOT" VAULT_MASTER_KEY= \
      timeout 90 python -c 'from app import cloud_browser as c; ok = c.probe(c.pool()); print("[heli-deploy] cloud browser probe:", "OK" if ok else c.last_error())' \
      || log "cloud browser probe could not run")
  else
    log "cloud browser: Chromium binary not found under $PLAYWRIGHT_BROWSERS_PATH"
  fi
else
  python -m pip install -e "$APP_DIR/server"
fi

set +u
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
set -u

export DATABASE_URL="${DATABASE_URL:-sqlite:///$APP_DIR/var/heli.db}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8001}"
export APP_ENV="${APP_ENV:-production}"

log "DATABASE_URL=$DATABASE_URL HOST=$HOST PORT=$PORT"

cd "$APP_DIR/server"
alembic upgrade head

install -m 0644 "$APP_DIR/infra/deploy/systemd-user/helibot.slice" "$APP_DIR/.config/systemd/user/helibot.slice"
if [ "$CLOUD_ON" != 1 ]; then
  # No cloud browser: the API alone keeps the old 256 MB cap.
  sed -i -e 's/^MemoryMax=.*/MemoryMax=256M/' -e '/^CPUQuota=/d' "$APP_DIR/.config/systemd/user/helibot.slice"
fi
log "slice: $(grep -E '^(MemoryMax|CPUQuota)=' "$APP_DIR/.config/systemd/user/helibot.slice" | tr '\n' ' ')"
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
