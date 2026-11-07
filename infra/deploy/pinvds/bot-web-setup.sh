#!/usr/bin/env bash
# One-time setup of bot.getheli.ru (the web cabinet) on pinvds. Run as root:
#
#   sudo bash /opt/heli-bot/infra/deploy/pinvds/bot-web-setup.sh
#
# What it does, and nothing else:
#   1. checks that bot.getheli.ru already points at this host (A record), otherwise stops;
#   2. creates /var/www/bot.getheli.ru owned by helibot (the deploy user writes builds there, no sudo);
#   3. adds ONE nginx server block for bot.getheli.ru only, checks `nginx -t`, then `systemctl reload nginx`
#      (never restart: getheli.ru runs in the same nginx);
#   4. gets a Let's Encrypt certificate for bot.getheli.ru with certbot and turns on the HTTPS redirect.
# getheli.ru (/opt/heli), bot-api.getheli.ru and other nginx files are not changed. Safe to run again.
set -euo pipefail

DOMAIN="${DOMAIN:-bot.getheli.ru}"
WEB_ROOT="${WEB_ROOT:-/var/www/bot.getheli.ru}"
APP_USER="${APP_USER:-helibot}"

log() { echo "[bot-web-setup] $*"; }
die() { log "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: sudo bash $0"
command -v nginx >/dev/null 2>&1 || die "nginx is not installed"
id "$APP_USER" >/dev/null 2>&1 || die "user $APP_USER does not exist"

# 1. DNS must already point here, otherwise Let's Encrypt cannot check the domain.
resolved="$(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | tr '\n' ' ')"
[ -n "$resolved" ] || die "$DOMAIN does not resolve yet. Add an A record $DOMAIN -> this server's IP and run again."
mine="$(hostname -I 2>/dev/null || true) $(curl -fsS -4 --max-time 5 https://ifconfig.me 2>/dev/null || true)"
ok=0
for ip in $resolved; do case " $mine " in *" $ip "*) ok=1 ;; esac; done
[ "$ok" = 1 ] || die "$DOMAIN resolves to $resolved, but this server is $mine. Fix the A record and run again."

# Where this nginx keeps server blocks.
if [ -d /etc/nginx/sites-available ] && [ -d /etc/nginx/sites-enabled ]; then
  CONF="/etc/nginx/sites-available/$DOMAIN"
  LINK="/etc/nginx/sites-enabled/$DOMAIN"
else
  CONF="/etc/nginx/conf.d/$DOMAIN.conf"
  LINK=""
fi

# Another file already serving this name would clash; leave it to a human.
domain_re="${DOMAIN//./\\.}"
other="$(grep -rlE "server_name[^;]*[[:space:]]$domain_re([[:space:];]|$)" /etc/nginx 2>/dev/null \
  | grep -vxF "$CONF" | grep -vxF "${LINK:-/nonexistent}" || true)"
[ -z "$other" ] || die "$DOMAIN is already in another nginx file: $other. Nothing changed."

# 2. Web root: releases/<stamp> + current -> releases/<stamp>, written by the deploy user.
install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$WEB_ROOT" "$WEB_ROOT/releases"
if [ ! -e "$WEB_ROOT/current" ]; then
  first="$WEB_ROOT/releases/placeholder"
  install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$first"
  printf '<!doctype html><meta charset="utf-8"><title>Heli bot</title><p>Кабинет скоро откроется.</p>\n' > "$first/index.html"
  chown "$APP_USER:$APP_USER" "$first/index.html"
  ln -sfn "$first" "$WEB_ROOT/current"
  chown -h "$APP_USER:$APP_USER" "$WEB_ROOT/current"
fi
log "web root $WEB_ROOT (owner $APP_USER)"

# 3. nginx server block (HTTP first; certbot adds 443 and the redirect below).
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<NGINX
# bot.getheli.ru: Heli bot web cabinet (static build). Written by bot-web-setup.sh.
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    root $WEB_ROOT/current;
    index index.html;

    # Hashed build files never change.
    location /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable";
        add_header X-Content-Type-Options "nosniff";
        try_files \$uri =404;
    }

    # The app itself: always revalidate, so a new build shows up at once.
    location / {
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options "nosniff";
        add_header X-Frame-Options "DENY";
        add_header Referrer-Policy "strict-origin-when-cross-origin";
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX
  [ -z "$LINK" ] || ln -sfn "$CONF" "$LINK"
  if ! nginx -t; then
    rm -f "$CONF" ${LINK:+"$LINK"}
    die "nginx -t failed with the new block; it was removed, nginx was not reloaded."
  fi
  systemctl reload nginx
  log "nginx: server block $CONF added and nginx reloaded"
else
  log "nginx: $CONF already exists, left as is"
fi

# 4. Certificate.
if ! command -v certbot >/dev/null 2>&1; then
  die "certbot is not installed. HTTP works now; install certbot (apt-get install certbot python3-certbot-nginx) and run again for HTTPS."
fi
args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect --keep-until-expiring)
if [ -z "$(ls -A /etc/letsencrypt/accounts 2>/dev/null || true)" ]; then
  args+=(--register-unsafely-without-email)
fi
certbot "${args[@]}"
nginx -t
systemctl reload nginx

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$DOMAIN/" || true)"
log "https://$DOMAIN/ -> HTTP $code"
[ "$code" = 200 ] || die "HTTPS does not answer 200 yet; check 'nginx -t' and 'certbot certificates'."
log "done. Builds are now delivered by Actions -> deploy-bot-web -> deploy."
