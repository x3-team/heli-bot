#!/usr/bin/env bash
# Saved sign-ins of the cloud browser and the credentials vault are sealed with VAULT_MASTER_KEY.
# Without it nothing is kept. Generates a random 32-byte key once; an existing non-empty key is never changed
# (changing it would make everything sealed with the old one unreadable).
set -euo pipefail

ENV_FILE="${1:?env file path}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  log "no env file at $ENV_FILE (skip vault key)"
  exit 0
fi

if grep -Eq '^VAULT_MASTER_KEY=.+' "$ENV_FILE"; then
  log "VAULT_MASTER_KEY already set, leaving it as is"
  exit 0
fi

KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
if grep -q '^VAULT_MASTER_KEY=' "$ENV_FILE"; then
  sed -i "s|^VAULT_MASTER_KEY=.*|VAULT_MASTER_KEY=$KEY|" "$ENV_FILE"
else
  printf '\nVAULT_MASTER_KEY=%s\n' "$KEY" >>"$ENV_FILE"
fi
log "generated VAULT_MASTER_KEY (saved sign-ins and the credentials vault are on)"
