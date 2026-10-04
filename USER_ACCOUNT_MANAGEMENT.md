# Hirevia Account Management

## Current account model

Hirevia currently supports one dashboard administrator. The admin email and Argon2id password hash are stored in the separate auth database (`HIREVIA_AUTH_DB_PATH`), not in the jobs database. The first web startup can create this record from `HIREVIA_ADMIN_EMAIL` and `HIREVIA_ADMIN_PASSWORD`. The password is hashed and the one-time plaintext setting is removed from the configured env file.

There is no public registration flow, user directory, admin user-management page, or user-deletion command in the current implementation. Email OTP verification and password-reset emails are not implemented. There can be only one admin; changing `.env` or restarting the service does not change existing credentials. Use the CLI reset command below to set a new admin password.

## Create the initial admin

For automatic setup, put `HIREVIA_ADMIN_EMAIL` and `HIREVIA_ADMIN_PASSWORD` in the private environment file before the first web startup. The app hashes the password, stores the admin row in the auth database, removes the plaintext password variable from the env file, and ignores subsequent bootstrap credential changes.

Alternatively, run the manual command in a trusted terminal. It prompts for the admin email and password and writes the Argon2id hash directly to the auth database. It refuses to change credentials if an admin already exists.

Windows PowerShell, using the project `.env`:

```powershell
.\.venv\Scripts\python.exe -m hirevia auth create-admin
```

Ubuntu VPS, using the web service environment file:

```bash
sudo -u hirevia /opt/hirevia/.venv/bin/python -m hirevia auth create-admin \
  --env-file /etc/hirevia/hirevia.env
sudo systemctl restart hirevia-web.service
```

The command is first-admin setup only. It cannot reset or replace an existing admin. The auth database persists across web-service restarts and must be backed up along with deployment configuration. It is separate from the jobs database.

## Reset the existing admin password

Run this command from the web service's working directory. It prompts twice with hidden input, enforces a 12-1024 character password, updates only the existing admin's Argon2id hash, and invalidates existing dashboard sessions. It does not change the admin email or write credentials to `.env`.

```powershell
.\.venv\Scripts\python.exe -m hirevia auth reset-password
```

When the web service uses a non-default environment file, pass it with `--env-file PATH`. The command uses the configured auth database and does not require a service restart.

Keep `.env` or `/etc/hirevia/hirevia.env` out of source control. On Ubuntu, keep the file owner-readable only:

```bash
sudo chown hirevia:hirevia /etc/hirevia/hirevia.env
sudo chmod 600 /etc/hirevia/hirevia.env
```

## Disable access

There is no admin-deletion command. To take the dashboard offline without affecting monitoring, stop only the web service:

```bash
sudo systemctl stop hirevia-web.service
```

Keep the auth database and the jobs database. Deleting either database is not an account-revocation method. To revoke every active browser session while keeping the admin, rotate `HIREVIA_SESSION_SECRET` to a new random value and restart the web service. The current code does not support permanently deleting the stored admin; that requires an explicit future account-management operation.

## Data and service impact

Admin login and logout do not start or stop the independent Hirevia monitoring worker. Removing admin access does not delete jobs, Telegram sessions, or worker state. The current implementation has no per-user ownership or account deletion operation; multi-user support needs an additive database migration and explicit access rules before it is enabled.