# Shared-server production deploy (venv + user systemd)

Heli bot API for user **`helibot`** at **`/opt/heli-bot`**: **no Docker**, **no root**. The API binds **`127.0.0.1`** only (reverse proxy forwards public HTTPS). Does **not** modify Fili or Heli (getheli).

## Layout on the host

| Path | Purpose |
|---|---|
| `/opt/heli-bot/server` | FastAPI app (synced from git) |
| `/opt/heli-bot/venv` | Python 3.12 venv |
| `/opt/heli-bot/var/heli.db` | Default SQLite database |
| `/opt/heli-bot/.env` | Secrets (`env.venv.example`) |
| `~/.config/systemd/user/heli-bot-api.service` | User unit |
| `~/.config/systemd/user/helibot.slice` | **256 MB** memory cap |

Legacy Docker Compose files remain for reference only; the GitHub deploy workflow uses **`remote-install.sh`**.

## GitHub Actions

Workflow: `.github/workflows/deploy-shared-server.yml`

- **Pull requests:** validate scripts + server tests.
- **Manual deploy:** `workflow_dispatch` → SSH + venv + `alembic` + `systemctl --user`.

### Repository secrets

| Secret | Purpose |
|---|---|
| `HELI_DEPLOY_SSH_HOST` | Hostname or IP |
| `HELI_DEPLOY_SSH_USER` | `helibot` |
| `HELI_DEPLOY_SSH_PRIVATE_KEY` | Deploy key (PEM) |
| `HELI_DEPLOY_SSH_PORT` | Optional; default `22` |
| `HELI_DEPLOY_APP_DIR` | Optional; default `/opt/heli-bot` |
| `HELI_DEPLOY_ENV_FILE` | Optional; default `/opt/heli-bot/.env` |

### Repository variables (desktop CI)

| Variable | Purpose |
|---|---|
| `HELI_PUBLIC_API_BASE` | Public HTTPS API origin for `VITE_HELI_API_BASE` in installers |

Create `/opt/heli-bot/.env` from `env.venv.example` before the first deploy.

If **`systemctl --user`** fails (no linger), the job exits **2** and logs **`systemd-system/`** templates for an admin.
