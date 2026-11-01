#!/usr/bin/env bash
# While v0.1 is tested by the owner only, run the API without sign-up.
# Adds HELI_OPEN_ACCESS=1 only when the key is absent; set it to 0 in the env file to turn sign-up back on.
set -euo pipefail

ENV_FILE="${1:?env file path}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  log "no env file at $ENV_FILE (skip open access patch)"
  exit 0
fi

if grep -q '^HELI_OPEN_ACCESS=' "$ENV_FILE"; then
  log "HELI_OPEN_ACCESS already set, leaving it as is"
  exit 0
fi

printf '\nHELI_OPEN_ACCESS=1\n' >>"$ENV_FILE"
log "set HELI_OPEN_ACCESS=1 (sign-up off for testing)"
