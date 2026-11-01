#!/usr/bin/env bash
# Paid beta: every tester has an own account, so the shared test account is off.
# Sets HELI_OPEN_ACCESS=0 and, when the deploy passes HELI_DEPLOY_ADMIN_EMAILS, writes HELI_ADMIN_EMAILS.
# HELI_INVITE_ONLY is left as is (default on: new accounts need an invite from the admin).
set -euo pipefail

ENV_FILE="${1:?env file path}"
ADMIN_EMAILS="${HELI_DEPLOY_ADMIN_EMAILS:-}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  log "no env file at $ENV_FILE (skip auth settings)"
  exit 0
fi

set_key() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -q "^${key}=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'index($0, k "=") == 1 { print k "=" v; next } { print }' "$ENV_FILE" >"$tmp"
  else
    cp "$ENV_FILE" "$tmp"
    printf '\n%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

set_key HELI_OPEN_ACCESS 0
log "set HELI_OPEN_ACCESS=0 (own accounts, sign-up by invite)"

if [ -n "$ADMIN_EMAILS" ]; then
  if ! printf '%s' "$ADMIN_EMAILS" | grep -Eq '^[A-Za-z0-9._%+@, -]+$'; then
    log "admin emails contain unexpected characters, not written"
    exit 1
  fi
  set_key HELI_ADMIN_EMAILS "$ADMIN_EMAILS"
  log "set HELI_ADMIN_EMAILS"
elif ! grep -Eq '^HELI_ADMIN_EMAILS=.+' "$ENV_FILE"; then
  log "WARNING: HELI_ADMIN_EMAILS is empty, nobody can create invites (run the deploy with admin_emails)"
fi
