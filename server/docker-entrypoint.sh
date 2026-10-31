#!/bin/sh
set -eu
if [ -n "${DATABASE_URL:-}" ] && echo "$DATABASE_URL" | grep -q '^postgresql'; then
  alembic upgrade head
fi
exec python -m app.main
