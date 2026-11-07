#!/usr/bin/env bash
# bot.getheli.ru web cabinet on pinvds. Runs on the host as the deploy user (helibot), started by
# .github/workflows/deploy-bot-web.yml.
#
#   web.sh diff                 show what is served now (release, nginx block, DNS, HTTPS), change nothing
#   web.sh deploy <tarball>     unpack the build into releases/<stamp>, switch `current` to it, check
#                               https://bot.getheli.ru, switch back if the new build is not served
#
# Needs the one-time root setup first (bot-web-setup.sh: web root, nginx block, certificate).
# Never touches nginx, certificates, /opt/heli (getheli.ru) or the API.
set -euo pipefail

MODE="${1:?mode: diff|deploy}"
TARBALL="${2:-}"
DOMAIN="${DOMAIN:-bot.getheli.ru}"
WEB_ROOT="${WEB_ROOT:-/var/www/bot.getheli.ru}"
KEEP="${KEEP_RELEASES:-5}"
URL="https://$DOMAIN/"
SETUP_HINT="as root, once: sudo bash /opt/heli-bot/infra/deploy/pinvds/bot-web-setup.sh"

log() { echo "[bot-web] $*"; }
current_release() { readlink "$WEB_ROOT/current" 2>/dev/null || true; }
served_revision() { curl -fsS --max-time 10 "${URL}REVISION" 2>/dev/null | head -n 1 || true; }
switch_to() {
  ln -sfn "$1" "$WEB_ROOT/current.new"
  mv -Tf "$WEB_ROOT/current.new" "$WEB_ROOT/current"
}

# Keep the setup script next to the other deploy files, so root can run it from a known path.
if [ -f /tmp/bot-web-setup.sh ] && [ -d /opt/heli-bot ] && [ -w /opt/heli-bot ]; then
  install -d -m 755 /opt/heli-bot/infra/deploy/pinvds
  install -m 755 /tmp/bot-web-setup.sh /opt/heli-bot/infra/deploy/pinvds/bot-web-setup.sh
fi

log "user $(id -un), web root $WEB_ROOT"
log "DNS $DOMAIN -> $(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
if grep -rqsE "server_name[^;]*[[:space:]]${DOMAIN//./\\.}([[:space:];]|$)" /etc/nginx; then
  log "nginx: server block for $DOMAIN found"
else
  log "nginx: no server block for $DOMAIN (or /etc/nginx not readable)"
fi
log "current release: $(current_release || echo none)"
log "served REVISION: $(served_revision || echo none)"
log "$URL -> HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" || true)"

case "$MODE" in
  diff)
    if [ ! -d "$WEB_ROOT/releases" ]; then log "web root is not set up yet; $SETUP_HINT"; fi
    ls -1t "$WEB_ROOT/releases" 2>/dev/null | head -n "$KEEP" | sed 's/^/[bot-web]   release /' || true
    exit 0
    ;;
  deploy) ;;
  *) log "unknown mode $MODE"; exit 2 ;;
esac

[ -n "$TARBALL" ] && [ -f "$TARBALL" ] || { log "no build tarball"; exit 1; }
if [ ! -d "$WEB_ROOT/releases" ] || [ ! -w "$WEB_ROOT/releases" ]; then
  log "$WEB_ROOT/releases is missing or not writable for $(id -un); $SETUP_HINT"
  exit 1
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
release="$WEB_ROOT/releases/$stamp"
mkdir -p "$release"
tar -xzf "$TARBALL" -C "$release"
[ -f "$release/index.html" ] || { log "build has no index.html, nothing switched"; rm -rf "$release"; exit 1; }
chmod -R a+rX "$release"
expected="$(head -n 1 "$release/REVISION" 2>/dev/null || true)"

previous="$(current_release)"
switch_to "$release"
log "current -> $release (was ${previous:-none})"

# nginx serves `current` directly, so the new build is live without a reload. Check it really is.
ok=0
for _ in 1 2 3 4 5; do
  if [ "$(served_revision)" = "$expected" ] \
    && curl -fsS --max-time 10 "$URL" | grep -q 'id="root"'; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" || true)"
  if [ "$code" = 000 ] || ! getent ahostsv4 "$DOMAIN" >/dev/null; then
    log "WARNING: $URL does not answer yet (DNS or nginx/TLS not set up); the build is in place. $SETUP_HINT"
  else
    log "$URL answers $code but does not serve the new build; switching back"
    if [ -n "$previous" ]; then switch_to "$previous"; fi
    rm -rf "$release"
    exit 1
  fi
else
  log "$URL serves $expected"
fi

# Old releases: keep the newest $KEEP (never the one in use).
in_use="$(current_release)"
ls -1t "$WEB_ROOT/releases" | tail -n +"$((KEEP + 1))" | while read -r old; do
  [ "$WEB_ROOT/releases/$old" = "$in_use" ] || rm -rf "${WEB_ROOT:?}/releases/$old"
done
log "done"
