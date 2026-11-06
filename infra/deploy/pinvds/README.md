# Deploy to pinvds (bot-api.getheli.ru)

Host 5.188.88.202, Ubuntu 20.04. Heli bot API: `/opt/heli-bot/server`, venv `/opt/heli-bot/venv`,
system unit `heli-bot-api` (user `helibot`), `127.0.0.1:8001`, nginx + Let's Encrypt in front.
getheli.ru on the same host (`/opt/heli`) is **not** touched by this deploy.

Workflow: **Actions → deploy-pinvds → Run workflow**.

1. `mode = diff` first: shows what differs between the running copy (made by hand, not from git) and the
   chosen branch. Nothing changes on the host.
2. `mode = deploy`: server tests → backup of `server/` (and `var/heli.db` if SQLite) to `/opt/heli-bot/backups`
   (last 5 kept) → rsync of `server/` → `pip install -e server[cloud]` → `alembic upgrade head` →
   `systemctl restart heli-bot-api` → health check on 127.0.0.1:8001 and https://bot-api.getheli.ru/health.
   If the API does not come up, the previous copy is restored and restarted.

`.env`, `var/` (database, browser profiles, files) and the venv are kept as they are.

The API must run only under the `heli-bot-api` unit. If another process (for example `python -m app.main`
started by hand) holds port 8001, `deploy` stops it before the restart and then checks that the process
answering on the port belongs to the unit; `diff` shows who holds the port.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `PINVDS_SSH_HOST` | `5.188.88.202` |
| `PINVDS_SSH_USER` | `helibot` (simplest: owns /opt/heli-bot, needs only the sudoers rule below), `root`, or a user with passwordless `sudo` |
| `PINVDS_SSH_KEY` | private key (OpenSSH/PEM) of that user, public half in its `~/.ssh/authorized_keys` |
| `PINVDS_SSH_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 5.188.88.202` (pins the host key) |
| `PINVDS_SSH_PORT` | optional, default `22` |
| `HELI_API_KEY` | already present, used by the tests |

For `helibot` as the deploy user, as root once:

```
echo 'helibot ALL=(root) NOPASSWD: /bin/systemctl restart heli-bot-api, /bin/systemctl --version, /bin/journalctl -u heli-bot-api *' > /etc/sudoers.d/heli-bot-deploy
chmod 440 /etc/sudoers.d/heli-bot-deploy && visudo -c
```

`server/var` and `.env` on the host are never synced over or deleted.

The job runs in the GitHub environment `pinvds`: add required reviewers there to make every deploy wait
for a click.
