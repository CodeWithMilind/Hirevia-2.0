# Hirevia 2.0

A web-first Telegram job-discovery agent. The dashboard and monitor use the existing Hirevia pipeline, SQLite repository, configuration, and Telethon user session. Monitoring runs in the backend and does not depend on an open browser.

## Architecture

```
Collect → Queue → Normalize → Deduplicate → Filter → Match → Persist → Expire
```

## Features

- **Web command center** - Browse and manage active opportunities, monitoring, AI providers and Telegram from a local browser
- **YAML-based filtering** - User-editable filters for roles, locations, skills, experience
- **Telegram collection** - Configured channels through the existing Telethon user client
- **AI-powered relevance** - llama.cpp is the default primary provider; NVIDIA, Groq, Ollama and other configured OpenAI-compatible providers remain available
- **Deduplication** - Multi-signal fingerprinting across sources
- **24-hour active window** - Jobs auto-expire after 24 hours
- **SQLite state** - Lightweight local storage
- **Durable processing** - SQLite queue, message history, monitor desired state, and 24-hour retention

## Installation

```bash
# Clone and enter directory
cd hirevia2

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

### 1. Filters (`filters.yaml`)

Edit `filters.yaml` to match your preferences:

```yaml
roles:
  - Python Developer
  - Backend Developer
  - AI Engineer
  - ML Engineer
  - Data Scientist

locations:
  - Pune
  - Mumbai
  - Remote
  - India

experience:
  max_years: 1
  fresher: true

skills:
  - Python
  - FastAPI
  - Flask`
  - SQL
  - Machine Learning
  - Deep Learning
  - Selenium

employment:
  - full_time
  - internship

exclude_keywords:
  - senior
  - lead
  - manager
  - principal
```

### 2. Telegram sources

Enable Telegram in `sources.yaml` and list public channels in `telegram.yaml`:

```yaml
sources:
  telegram:
    enabled: true
telegram:
  enabled: true
  channels:
    - username: pythoninternships
      enabled: true
```

### 3. Candidate Profile (`profile.yaml`)

Copy `profile.yaml.example` to `profile.yaml` and fill in your details:

```yaml
name: "Your Name"
summary: "Python backend developer with 1 year experience in FastAPI, SQL, and ML basics"
skills:
  - Python
  - FastAPI
  - SQL
  - Machine Learning
experience_years: 1
preferred_roles:
  - Python Developer
  - Backend Developer
preferred_locations:
  - Pune
  - Remote
```

## Matching Scores

Skill coverage is calculated from unique, role-relevant technical skills found in each job description:

```text
JD skill coverage = matched JD skills / total relevant JD skills * 100
```

Additional skills in a candidate profile do not enter the denominator. Aliases such as `ML`/`Machine Learning`, `GenAI`/`Generative AI`, `RESTful API`/`REST API`, `PostgreSQL`/`Postgres`, and retrieval-augmented generation/`RAG` count once. Recognized soft skills do not count as technical coverage. Explicitly optional or preferred skills have half the skill weight of mandatory requirements.

Role-aware extraction uses distinct skill pools for backend engineering, data science, data analysis, and AI engineering. The JD coverage percentage is shown separately from overall compatibility; it does not independently decide whether a job matches. The deterministic score combines skills (45 points), role (18), location (12), experience (10), work mode (8), employment (4), and company (3). When AI assessment is available, the final score is `round(deterministic * (1 - ai_match_weight) + ai_score * ai_match_weight)`; an AI `relevant=false` assessment incurs the existing 12-point penalty, and hard vetoes remain decisive. `match_min_score` independently gates the boolean match decision, and alert eligibility still checks application quality, freshness, and notification score thresholds.

The shared configurable score cutoffs default to P1 at 70+, P2 at 40-69, P3 above 0 and below 40, and Unmatched at 0. They classify both JD coverage and the final overall score. Configure them in `ai.yaml` under `matching` or through `MATCHING_P1_THRESHOLD`, `MATCHING_P2_THRESHOLD`, and `MATCHING_P3_THRESHOLD`. The final priority is calculated from the final overall score, not the AI-provided tier. Alerts, the dashboard, and CLI priority summaries use the final overall verdict; JD coverage is an additional signal.

## Running

### Start Hirevia locally

```powershell
.\.venv\Scripts\python.exe -m hirevia worker start
.\.venv\Scripts\python.exe -m hirevia worker status
.\.venv\Scripts\python.exe -m hirevia web
```

The worker and web API are separate processes. Start the worker explicitly before opening the dashboard so Telegram authentication and monitor controls have a worker to communicate with. The API binds to `127.0.0.1:8000`; open `http://127.0.0.1:8000`. Starting the worker does not start the API, and starting the API does not create or restart a worker.

Stop the API with Ctrl+C in its terminal. Stop the independent worker with:

```powershell
.\.venv\Scripts\python.exe -m hirevia worker stop
```

Use `.\.venv\Scripts\python.exe -m hirevia worker restart` for a graceful restart, or `.\.venv\Scripts\python.exe -m hirevia worker` to run it attached to the current terminal. Closing the API terminal leaves an explicitly started background worker running; check it with `worker status` and stop it explicitly when desired. `.\.venv\Scripts\python.exe -m hirevia doctor` reports database integrity, the worker lease and process executable, and whether the configured API port responds.

### Database location

The default remains `hirevia.db` in the project directory for backward compatibility. `HIREVIA_DB_PATH` can be set in the environment or project `.env`; all repository-backed commands resolve it to one absolute path. The current checkout is under OneDrive, where synchronizing a live SQLite database and its WAL sidecars can interfere with file locking and cross-device consistency. A local, non-synchronized location such as `%LOCALAPPDATA%\Hirevia\hirevia.db` is recommended.

Hirevia does not move the database automatically. To migrate, stop the API and worker cleanly, make a verified SQLite backup, copy the backup to the chosen local directory, run `PRAGMA quick_check` and compare job counts on both copies, then configure `HIREVIA_DB_PATH` and verify `worker status`, `doctor`, and dashboard data. Keep the original database and backup unchanged until the new location is verified. Do not copy or delete active `-wal` or `-shm` files by hand.

### Scan Telegram history

```bash
python -m hirevia scan --no-ai
```

The dashboard's **Scan 24h** control runs the shared pipeline against recent Telegram channel history and skips identities already processed.

### View statistics

```bash
python -m hirevia stats
```

### Check status

```bash
python -m hirevia status
```

## Commands

| Command | Description |
|---------|-------------|
| `start` | Run the legacy continuous scan loop without notifications |
| `scan` | Run a single Telegram scan cycle |
| `stats` | Show database statistics |
| `status` | Show recent jobs and their status |
| `sources` | List configured sources |
| `monitor` | Continuously process Telegram channel messages without notifications |
| `web` | Run the Web UI for administration or debugging |
| `worker` | Run the worker attached to the current terminal |
| `worker start/status/stop/restart` | Explicitly manage the independent background worker |
| `doctor` | Check database integrity, worker lease, and API port |
| `ai status`, `ai test`, `ai set-primary` | Inspect and manage AI providers |
| `test` | Run self-tests |
| `version` | Show version |

## Runtime files

- SQLite is deliberately configured for WAL mode. `hirevia.db-wal` and `hirevia.db-shm` may appear while connections are active and disappear after checkpointing/connection close; do not delete them manually.
- Application logs rotate by size into `hirevia.log.1`, `hirevia.log.2`, and subsequent backups.
- Configuration edits use temporary `.tmp` files and atomic replacement; the API keeps `.bak` copies. A failed write removes its temporary file.
- Telegram logout explicitly removes the Telegram `.session` file and its SQLite sidecars. Ordinary worker or API shutdown preserves the session.
- Python may create `__pycache__` bytecode files during imports. OneDrive may also create transient sync metadata; this investigation did not capture a historical file event, so a specific past creator cannot be attributed from the current filesystem alone.

## Security

- The web server binds to localhost by default. Non-local binding requires `HIREVIA_ADMIN_TOKEN`; API calls use bearer authentication and the browser prompts for the token.
- Put the token in the server's `.env` file, keep it private, and put remote access behind a TLS reverse proxy. Do not expose an unauthenticated administrative interface directly to the internet.
- API keys and Telegram API/session credentials are not returned by the web API. Keep `.env` and `sessions/` private and out of source control.
- OTPs and 2FA passwords are submitted only to the login endpoints and are never written to configuration files. SQLite is configured for WAL mode and busy timeouts.
- Set `HIREVIA_HOST` and `HIREVIA_PORT` to configure the bind address and port. For example, a reverse proxy may connect to a loopback-bound Hirevia server on the VPS.
- Ubuntu systemd installation, data paths, TLS proxy guidance, and restart behavior are documented in [docs/VPS.md](docs/VPS.md).

## Project Structure

```
hirevia/
├── __init__.py
├── __main__.py
├── cli.py              # CLI commands
├── config.py           # Configuration loading
├── core/
│   ├── models.py       # Job, CandidateProfile, AIResponse
│   ├── pipeline.py     # Main pipeline orchestration
│   ├── dedup.py        # Deduplication logic
│   ├── freshness.py    # Freshness & 24h expiry
│   └── filtering.py    # YAML-based filtering
├── sources/
│   ├── base.py         # Source interface
│   ├── registry.py     # Source registry
│   └── wellfound.py    # First real source
├── ai/
│   ├── client.py       # NVIDIA/OpenAI-compatible client
│   └── matcher.py      # AI relevance matching
├── profile/
│   └── profile.py      # Candidate profile
├── notifications/
│   ├── base.py         # Notifier interface
│   └── telegram.py     # Telegram bot
└── storage/
    └── state.py        # SQLite repository
```

## Testing

```bash
python -m pytest tests/ -v
```

## Development

### Adding a new source

1. Create `hirevia/sources/newsource.py` implementing `JobSource`
2. Register in `hirevia/sources/registry.py`
3. Add to `sources.yaml`

### Adding a notification channel

1. Create `hirevia/notifications/newchannel.py` implementing `Notifier`
2. Register in pipeline

## License

MIT