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

## Running

### Start the command center

```bash
python -m hirevia
```

Open `http://127.0.0.1:8000`. On first setup, authenticate Telegram and wait for channel validation, then explicitly start monitoring. The monitor continues when the browser closes. A previously running monitor is restored after an ordinary service restart; Telegram sessions are preserved except on logout or invalidation. The compatible `python -m hirevia web` command is also available.

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
| `ai status`, `ai test`, `ai set-primary` | Inspect and manage AI providers |
| `test` | Run self-tests |
| `version` | Show version |

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