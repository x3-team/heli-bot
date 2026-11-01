#!/usr/bin/env bash
# Heli's own browser on this host is the agent's computer in v0.1.
# Adds CLOUD_BROWSER_ENABLED=1 and CLOUD_BROWSER_MAX=2 only when absent; set ENABLED=0 in the env file to turn it off.
set -euo pipefail

ENV_FILE="${1:?env file path}"

log() {
  echo "[heli-deploy] $*" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  log "no env file at $ENV_FILE (skip cloud browser patch)"
  exit 0
fi

if ! grep -q '^CLOUD_BROWSER_ENABLED=' "$ENV_FILE"; then
  printf '\nCLOUD_BROWSER_ENABLED=1\n' >>"$ENV_FILE"
  log "set CLOUD_BROWSER_ENABLED=1"
fi
if ! grep -q '^CLOUD_BROWSER_MAX=' "$ENV_FILE"; then
  printf 'CLOUD_BROWSER_MAX=2\n' >>"$ENV_FILE"
  log "set CLOUD_BROWSER_MAX=2"
fi
