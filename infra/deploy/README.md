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
| `HELI_DEPLOY_JWT_SECRET` | JWT signing secret (bootstrap when `.env` is absent) |
| `HELI_DEPLOY_TRAJECTORY_HASH_PEPPER` | Pepper for login codes and password hashing |
| `HELI_API_KEY` | getheli upstream key (CI tests + `.env` bootstrap) |

### Repository variables (desktop CI)

| Variable | Purpose |
|---|---|
| `HELI_PUBLIC_API_BASE` | Public HTTPS API origin for `VITE_HELI_API_BASE` in installers |

If `/opt/heli-bot/.env` is missing, deploy runs `write-env-if-missing.sh` (mode **600**, never overwritten). Otherwise copy from `env.venv.example` manually.

If `python3 -m venv` fails (no `python3-venv` package), `remote-install.sh` bootstraps pip via `--without-pip` + get-pip.py (no root required).

If **`systemctl --user`** fails (no linger), the job exits **2** and logs **`systemd-system/`** templates for an admin.
