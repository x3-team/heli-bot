# Shared-server production deploy (venv + user systemd)

Heli bot API for Vitalii's shared host: user **`helibot`**, home **`/opt/heli-bot`**, **no Docker**, **no root**. The API binds **`127.0.0.1`** only (reverse proxy on the host forwards a public HTTPS URL). Does **not** modify Fili or Heli (getheli) services.

## Layout on the host

| Path | Purpose |
|---|---|
| `/opt/heli-bot/server` | FastAPI app (synced from git) |
| `/opt/heli-bot/venv` | Python 3.12 venv |
| `/opt/heli-bot/var/heli.db` | Default SQLite database |
| `/opt/heli-bot/.env` | Secrets (`env.venv.example`) |
| `~/.config/systemd/user/heli-bot-api.service` | User unit (`systemctl --user`) |
| `~/.config/systemd/user/helibot.slice` | **256 MB** memory cap |

## GitHub Actions

Workflow: `.github/workflows/deploy-shared-server.yml`

- **Pull requests:** validate scripts + run server tests.
- **Manual deploy:** `workflow_dispatch` → SSH sync + `infra/deploy/remote-install.sh`.

### Repository secrets

| Secret | Purpose |
|---|---|
| `HELI_DEPLOY_SSH_HOST` | Hostname or IP |
| `HELI_DEPLOY_SSH_USER` | `helibot` |
| `HELI_DEPLOY_SSH_PRIVATE_KEY` | Deploy key (PEM) |
| `HELI_DEPLOY_SSH_PORT` | Optional; default `22` |
| `HELI_DEPLOY_APP_DIR` | Optional; default `/opt/heli-bot` |
| `HELI_DEPLOY_ENV_FILE` | Optional; default `/opt/heli-bot/.env` |

Create `/opt/heli-bot/.env` from `env.venv.example` on the server before the first deploy.

### User systemd vs system unit

Deploy tries **`systemctl --user`** (requires **linger** for `helibot`: `loginctl enable-linger helibot` once by an admin).

If user systemd is unavailable, the workflow fails with exit code **2** and prints commented templates from `systemd-system/` for an administrator to install under `/etc/systemd/system/`.

## Desktop clients

Set repository variable **`HELI_PUBLIC_API_BASE`** to the public HTTPS API origin (no trailing slash) so Windows/macOS builds call the same URL as the reverse proxy.
