# Dashboard Authentication

Hirevia supports one dashboard administrator. The admin record and browser sessions are stored in the separate SQLite database configured by `HIREVIA_AUTH_DB_PATH`; this is not the jobs database. Do not delete or replace either database to change dashboard access.

## First Admin

For automatic setup, set `HIREVIA_ADMIN_EMAIL` and `HIREVIA_ADMIN_PASSWORD` in the private environment file before the first web startup. The app creates the admin only if the auth database has no admin row, hashes the password with Argon2id, and removes the one-time plaintext password from both the process environment and environment file. A blank `HIREVIA_SESSION_SECRET` is generated once and persisted to that environment file.

Startup is safe to repeat. Existing admin email and password hashes are never updated from environment values, including when those values change. Changing the bootstrap variables is not a password-reset mechanism. The manual `auth create-admin` command also refuses to replace an existing admin.

The default auth database is `hirevia_auth.db` in the working directory. Set an absolute path for a service deployment so restarts use the same protected database. Keep it separate from `HIREVIA_DB_PATH` and preserve it across application updates.

## Windows Setup

Run these commands from the project root in PowerShell. The copy is conditional so an existing `.env` is never overwritten:

```powershell
if (-not (Test-Path .\.env)) { Copy-Item .\.env.example .\.env }
notepad .\.env
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set `HIREVIA_ADMIN_EMAIL` and a strong, one-time `HIREVIA_ADMIN_PASSWORD` in `.env`. For local loopback development, `HIREVIA_COOKIE_SECURE=false` is appropriate. Leave `HIREVIA_SESSION_SECRET` blank to generate and persist it at startup. Keep the default local host on `127.0.0.1`.

Start the web app from the same project directory:

```powershell
.\.venv\Scripts\python.exe -m hirevia
```

Open `http://127.0.0.1:8000` and sign in with the initial email and password. Hirevia removes the password setting after first startup. The worker is a separate process; start it with `python -m hirevia worker start` when monitoring is needed. Admin login, logout, and auth database operations do not start or stop the worker, alter Telegram login state, or modify existing job records.

If an admin already exists, do not expect changed `.env` credentials to replace it. Preserve the auth database and use only an explicitly supported account operation; there is no password-reset or admin-deletion command in this implementation.

## Ubuntu VPS

The supplied systemd units run as `hirevia`, load `/etc/hirevia/hirevia.env` using `EnvironmentFile`, and set the auth database to `/var/lib/hirevia/auth.db`. The web unit also sets `HIREVIA_ENV_FILE` to that environment file and allows writes to `/etc/hirevia`, so first startup can persist the generated session secret and remove the bootstrap password. Keep the file owned by `hirevia` with mode `600`.

These commands assume the repository is already deployed at `/opt/hirevia` and the `hirevia` system account exists:

```bash
sudo install -d -o hirevia -g hirevia -m 0700 /etc/hirevia /var/lib/hirevia /var/lib/hirevia/sessions /var/log/hirevia
sudo -u hirevia python3 -m venv /opt/hirevia/.venv
sudo -u hirevia /opt/hirevia/.venv/bin/python -m pip install -r /opt/hirevia/requirements.txt
if [ ! -e /etc/hirevia/hirevia.env ]; then
	sudo install -o hirevia -g hirevia -m 600 /dev/null /etc/hirevia/hirevia.env
fi
sudoedit /etc/hirevia/hirevia.env
```

Set these values in `/etc/hirevia/hirevia.env`, along with the deployment's Telegram and optional AI settings. Use a private strong bootstrap password; do not put actual values in Git:

```dotenv
PYTHONPATH=/opt/hirevia
HIREVIA_ADMIN_EMAIL=admin@example.com
HIREVIA_ADMIN_PASSWORD=replace-with-a-strong-one-time-password
HIREVIA_AUTH_DB_PATH=/var/lib/hirevia/auth.db
HIREVIA_SESSION_SECRET=
HIREVIA_ENV_FILE=/etc/hirevia/hirevia.env
HIREVIA_ENV=production
HIREVIA_COOKIE_SECURE=true
HIREVIA_ALLOWED_HOSTS=hirevia.example.com
TELEGRAM_SESSION_DIR=/var/lib/hirevia/sessions
```

The units set `HIREVIA_DB_PATH=/var/lib/hirevia/hirevia.db`, so job data remains separate. `PYTHONPATH=/opt/hirevia` is needed with the supplied `/etc/hirevia` working directory because this checkout has no packaging metadata. The web service binds to loopback; expose it only through an HTTPS reverse proxy or SSH tunnel, and set `HIREVIA_ALLOWED_HOSTS` to the public hostname for a proxy deployment.

Set ownership, verify the import path, install the unit files, and start services in this order:

```bash
sudo chown hirevia:hirevia /etc/hirevia/hirevia.env
sudo chmod 600 /etc/hirevia/hirevia.env
sudo -u hirevia env PYTHONPATH=/opt/hirevia /opt/hirevia/.venv/bin/python -c 'import hirevia; print(hirevia.__file__)'
sudo install -o root -g root -m 644 /opt/hirevia/deploy/hirevia-worker.service /etc/systemd/system/hirevia-worker.service
sudo install -o root -g root -m 644 /opt/hirevia/deploy/hirevia-web.service /etc/systemd/system/hirevia-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now hirevia-worker.service
sudo systemctl enable --now hirevia-web.service
sudo systemctl status hirevia-worker.service hirevia-web.service
```

The first web-service startup creates the admin if needed, persists the session secret, and removes the plaintext bootstrap password from `hirevia.env`. Verify the dashboard over the configured HTTPS endpoint. Restarting the service reloads the environment file but does not replace the admin. Back up `/var/lib/hirevia/auth.db` and `/var/lib/hirevia/hirevia.db` as distinct databases.

## Session Security

Passwords are Argon2id hashes. Successful login issues a random opaque session token in an `HttpOnly`, `SameSite=Lax` cookie; only an HMAC-SHA256 digest of the token is stored in the auth database. Sessions expire after eight hours by default (`HIREVIA_SESSION_TTL_SECONDS`) and logout revokes the current token. Login failures are rate-limited. Unsafe API requests must have a same-origin `Origin` header.

Production mode (`HIREVIA_ENV=production`) requires `HIREVIA_COOKIE_SECURE=true`; use it only behind HTTPS. The session secret must be at least 32 bytes and must remain stable across restarts. Rotating it invalidates existing sessions. Keep `.env`, `.env.lock`, VPS environment files, auth databases, and backups private. `.env.example` contains placeholders only; it is not a live environment file.

There is no public registration, multi-admin user manager, password reset, or admin deletion operation. See [USER_ACCOUNT_MANAGEMENT.md](USER_ACCOUNT_MANAGEMENT.md) for the current account-management limits and service impact.