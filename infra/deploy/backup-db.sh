#!/bin/sh
# Run on the server from the deploy directory to export the dedicated Postgres volume.
# Keeps migration to another VPS simple: copy this archive + infra/deploy + .env.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"
ENV_FILE="${HELI_ENV_FILE:-.env}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${1:-./backups/heli-bot-pg-${STAMP}.sql.gz}"
mkdir -p "$(dirname "$OUT")"
docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE" exec -T postgres \
  pg_dump -U heli -d heli --no-owner --no-acl | gzip -c > "$OUT"
echo "Wrote $OUT"
