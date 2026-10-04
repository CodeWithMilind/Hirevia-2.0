# Ubuntu VPS Deployment

Hirevia runs as two independently supervised services. The FastAPI dashboard sends durable commands through SQLite; `hirevia-worker.service` owns the Telethon client, collector, queue processing, AI matching, and job persistence. Restarting the web service does not stop the worker. The worker uses a database PID lease to reject duplicate processes, and desired monitor state remains in SQLite. A first install starts STOPPED; previously running monitoring resumes after reboot.

## Install

Create a dedicated account and install the project and virtual environment:

```bash
sudo useradd --system --create-home --home-dir /opt/hirevia --shell /usr/sbin/nologin hirevia
sudo install -d -o hirevia -g hirevia -m 0700 /etc/hirevia /var/lib/hirevia /var/lib/hirevia/sessions /var/log/hirevia
sudo -u hirevia python3 -m venv /opt/hirevia/.venv
sudo -u hirevia /opt/hirevia/.venv/bin/pip install -r /opt/hirevia/requirements.txt
sudo -u hirevia /opt/hirevia/.venv/bin/pip install -e /opt/hirevia
```

Copy the non-secret YAML configuration files (`telegram.yaml`, `profile.yaml`, `filters.yaml`, `sources.yaml`, and `ai.yaml` if used) to `/etc/hirevia`. Keep `.env` or `hirevia.env` server-side and owner-readable only. Create `/etc/hirevia/hirevia.env` with at least:

```dotenv
TELEGRAM_API_ID=your_telegram_api_id
TELEGRAM_API_HASH=your_telegram_api_hash
TELEGRAM_SESSION_DIR=/var/lib/hirevia/sessions
HIREVIA_ENV_FILE=/etc/hirevia/hirevia.env
HIREVIA_ADMIN_EMAIL=admin@example.com
HIREVIA_ADMIN_PASSWORD=choose-a-strong-one-time-password
HIREVIA_AUTH_DB_PATH=/var/lib/hirevia/auth.db
HIREVIA_SESSION_SECRET=
HIREVIA_ENV=production
HIREVIA_COOKIE_SECURE=true
HIREVIA_ALLOWED_HOSTS=hirevia.example.com
```

The first web startup hashes the initial password, stores the admin in the separate auth database, generates a session secret if missing, and removes the plaintext password setting from this file. Later changes to the bootstrap email or password do not replace the existing database admin. Keep the auth database under `/var/lib/hirevia`; it is separate from the jobs database. Add the active model's API key and provider/model settings to this file when needed. Do not put OTPs, 2FA passwords, or Telegram session contents in it. Restrict the file:

```bash
sudo chown hirevia:hirevia /etc/hirevia/hirevia.env
sudo chmod 600 /etc/hirevia/hirevia.env
```

Install the service and start it:

```bash
sudo install -o root -g root -m 644 /opt/hirevia/deploy/hirevia-worker.service /etc/systemd/system/hirevia-worker.service
sudo install -o root -g root -m 644 /opt/hirevia/deploy/hirevia-web.service /etc/systemd/system/hirevia-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now hirevia-worker.service
sudo systemctl enable --now hirevia-web.service
sudo systemctl status hirevia-worker.service hirevia-web.service
```

The web service binds to `127.0.0.1:8000`. Reach it through an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 hirevia-vps
```

Then open `http://127.0.0.1:8000`. For public access, retain the loopback bind and configure a TLS reverse proxy to `127.0.0.1:8000`. Do not expose port 8000 directly. The login page uses a server-validated HttpOnly session cookie; the reverse proxy must preserve the public `Host` header and forward HTTPS requests. `HIREVIA_ALLOWED_HOSTS` must include the public hostname.

## Monitor controls and recovery

On a new install the monitor is stopped. Log into Telegram in the dashboard, wait for channel validation to finish, then select **Start Monitor**. Select **Stop Monitor** to stop collection without disconnecting Telegram. The desired state is stored in the shared jobs database; restarting either service or rebooting the VPS resumes a previously running monitor, but leaves a manually stopped monitor stopped. Dashboard sign-out only revokes the web session; it does not stop the worker or disconnect Telegram.

Application log files are stored and rotated under `/var/log/hirevia`; they are not exposed through the dashboard or API. Database and Telegram session files remain under `/var/lib/hirevia` and are separate.

Operational commands:

```bash
sudo systemctl restart hirevia-web.service
sudo systemctl restart hirevia-worker.service
sudo systemctl stop hirevia-worker.service
sudo systemctl start hirevia-worker.service
sudo journalctl -u hirevia-worker.service -n 100 --no-pager
```

The journal contains concise service output. For diagnostics, use `journalctl` or inspect the current and rotated application logs directly on the VPS.

## Update

Back up the database and configuration before replacing application code. Keep `/var/lib/hirevia`, `/etc/hirevia`, and `/var/log/hirevia` intact. After updating the checkout under `/opt/hirevia`:

```bash
sudo -u hirevia /opt/hirevia/.venv/bin/pip install -r /opt/hirevia/requirements.txt
sudo -u hirevia /opt/hirevia/.venv/bin/pip install -e /opt/hirevia
sudo systemctl restart hirevia-worker.service hirevia-web.service
```

The worker checkpoints its durable queue before shutdown. A worker crash returns in-flight queue claims to QUEUED on restart; Telegram cursors replay updates that were not committed.
