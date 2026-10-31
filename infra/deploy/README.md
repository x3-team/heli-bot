# Shared-server production deploy

Heli bot runs in **its own Docker Compose project** on Vitalii's existing shared host: dedicated API and Postgres containers, CPU/RAM limits, **no host port bindings**, and a **separate subdomain** on the reverse proxy that is already there.

Other containers on the host are not modified by this stack; only a new external network attachment and proxy route are required.

## Layout

| Piece | Purpose |
|---|---|
| `docker-compose.prod.yml` | `heli-bot` project, internal network + `HELI_PROXY_NETWORK` |
| `.env.example` | Variables for the server-side env file |
| `reverse-proxy.example.md` | Snippet for the existing proxy (subdomain not fixed here) |
| `backup-db.sh` | `pg_dump` for moving to a dedicated VPS later |

## GitHub Actions

Workflow: `.github/workflows/deploy-shared-server.yml`

- **Pull requests:** validate Compose and run server tests.
- **Manual deploy:** `workflow_dispatch` only (after secrets exist), SSH sync + `docker compose up --build`.

### Repository variables (desktop CI)

| Variable | Purpose |
|---|---|
| `HELI_PUBLIC_API_BASE` | Public HTTPS origin for the Heli API (no trailing slash), baked into the Windows/macOS installer as `VITE_HELI_API_BASE`. Example: `https://your-subdomain.example.com` |

### Repository secrets (coordinator / Vitalii — values not in repo)

| Secret | Purpose |
|---|---|
| `HELI_DEPLOY_SSH_HOST` | Shared server hostname or IP |
| `HELI_DEPLOY_SSH_USER` | SSH user |
| `HELI_DEPLOY_SSH_PRIVATE_KEY` | Private key (PEM), no passphrase recommended for CI |
| `HELI_DEPLOY_SSH_PORT` | Optional; default `22` if unset |
| `HELI_DEPLOY_APP_DIR` | Remote directory for this project (e.g. `/opt/heli-bot`) |
| `HELI_DEPLOY_ENV_FILE` | Remote path to the production `.env` file passed to `docker compose --env-file` (same keys as `.env.example`) |

Server-side `.env` is created on the host from `.env.example` (includes `HELI_PROXY_NETWORK`, `POSTGRES_PASSWORD`, `JWT_SECRET`, `HELI_API_KEY`, etc.). The workflow does **not** print env contents or keys.

### First-time server steps (manual)

1. Create `HELI_DEPLOY_APP_DIR` and copy `.env` from `.env.example`.
2. Ensure `HELI_PROXY_NETWORK` matches the Docker network your reverse proxy uses.
3. Add the subdomain route pointing at `heli-bot-api:8000` (see `reverse-proxy.example.md`).
4. Run the deploy workflow once secrets are set.

## Moving to a dedicated VPS later

1. On the old host: `./backup-db.sh`.
2. On the new host: install Docker, copy `infra/deploy`, `.env`, and the SQL archive.
3. `docker compose -f docker-compose.prod.yml --env-file .env up -d --build`
4. Restore: `gunzip -c backup.sql.gz | docker compose ... exec -T postgres psql -U heli -d heli`
