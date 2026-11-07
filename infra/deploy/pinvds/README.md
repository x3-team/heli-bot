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

`mode = learning-check`: read-only check of the self-learning signals on the live database: schema (0011),
run journal (finished_at/stopped/duration, tasks never closed), feedback, signals, trajectories, the raw/sft/dpo
export (line counts, no forbidden keys or account emails) and `/v1/admin/quality` + export called on the running
API as an admin. Prints counts only: no task texts, emails, ids or tokens. Nothing on the host changes.

Input `admin_emails` (with `mode = deploy`): writes `HELI_ADMIN_EMAILS` into `.env` (replaces the list; `.env` is backed up first). A listed address signs up in the app with a password and no invite, and gets admin rights (`/v1/admin/*`).

Input `docs_crawl` (with `mode = deploy`): `1` writes `HELI_DOCS_CRAWL=1`, the API then indexes the official docs sites (`OFFICIAL_SOURCES`) two minutes after start and once a day (robots.txt, 1 request/s per site, 100 pages per site per run); `0` turns it off. Empty leaves `.env` as is.

`.env`, `var/` (database, browser profiles, files) and the venv are kept as they are.

## Which build is on prod

- `GET https://bot-api.getheli.ru/health` → `{"status": "ok", "revision": "<git sha>"}`: the commit that
  `deploy-pinvds` packed into `server/REVISION`. `null` means a copy that did not come from the workflow.
- `mode = diff` prints the running `REVISION`, the rsync dry run for `server/`, and since 07.11 also:
  the top-level layout of `/opt/heli-bot` (names only), files outside `server/` that are not in git
  (`.env`, `var`, `backups`, `venv` are skipped), the diff of `/opt/heli-bot/infra/deploy` against the
  release, and any `issue-cert*` script with long values masked (so its steps can be kept in this README).
- `mode = deploy` also refreshes `/opt/heli-bot/infra/deploy` from the release **without deleting**
  anything that exists only on the host, after the API is up. The old copy is in the backup tarball.
  Reason: the hand-copied `ensure-open-access-in-env.sh` / `env.venv.example` there still suggested
  `HELI_OPEN_ACCESS=1`; a `.env` restored from them would have opened the shared account.

Reconciled 07.11.2026: `server/` on the host equals main `89d2263` file for file (diff run 42380315746:
only `REVISION` differs).

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

## Training-stand keys in .env

`deploy-pinvds` → Run workflow has two optional inputs: `amo_sandbox_hosts` → `HELI_AMO_SANDBOX_HOSTS`
(training amoCRM accounts, `xxx.amocrm.ru`, comma-separated) and `ga4_sandbox_ids` → `HELI_GA4_SANDBOX_IDS`
(training GA4 Measurement IDs, `G-…`). Empty means «leave as is». `mode=diff` prints the current values;
`mode=deploy` backs up `.env` to `backups/env-<stamp>` (mode 600), writes only these keys and restores the
backup on rollback. Nothing else in `.env` is changed, and values in any other format are refused.

## Web cabinet bot.getheli.ru

Static build of `apps/web`, served by nginx from `/var/www/bot.getheli.ru/current`
(`releases/<stamp>`, the newest 5 kept). It talks to `https://bot-api.getheli.ru` (CORS is open on the API).

1. **DNS:** A record `bot.getheli.ru → 5.188.88.202` (reg.ru).
2. **Once, as root** (the deploy user `helibot` has no rights on nginx or certbot):
   `sudo bash /opt/heli-bot/infra/deploy/pinvds/bot-web-setup.sh`. It checks DNS, creates the web root owned by
   `helibot`, adds one nginx server block for `bot.getheli.ru` only, runs `nginx -t` and then
   `systemctl reload nginx` (never restart: getheli.ru shares this nginx), and gets the certificate with
   `certbot --nginx -d bot.getheli.ru --redirect`. Safe to run again. Any run of `deploy-bot-web` puts the
   current copy of the script at that path.
3. **Every release:** Actions → `deploy-bot-web` → `deploy`. Builds with
   `VITE_HELI_API_BASE=https://bot-api.getheli.ru`, unpacks a new release, switches `current` and checks that
   `https://bot.getheli.ru/REVISION` shows the new commit; if the site answers but serves something else, it
   switches back. `diff` only reports what is served.
