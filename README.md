# Hirevia 2.0

A web-first job discovery agent. The browser, CLI and background services use the same Hirevia pipeline, SQLite repository, configuration and Telegram integrations.

## Architecture

```
Discover → Extract → Normalize → Deduplicate → Freshness → Filter → AI Match → Verify → Notify → Track → Expire
```

## Features

- **Web command center** - Browse and manage active opportunities, monitoring, AI providers and Telegram from a local browser
- **YAML-based filtering** - User-editable filters for roles, locations, skills, experience
- **Multi-source** - Company careers, ATS (Greenhouse, Lever, Ashby), job boards
- **AI-powered relevance** - llama.cpp is the default primary provider; NVIDIA, Groq, Ollama and other configured OpenAI-compatible providers remain available
- **Deduplication** - Multi-signal fingerprinting across sources
- **24-hour active window** - Jobs auto-expire after 24 hours
- **SQLite state** - Lightweight local storage
- **Telegram notifications** - Real-time job alerts

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

### 2. Sources (`sources.yaml`)

Enable/disable job sources:

```yaml
sources:
  company_careers:
    enabled: true
  greenhouse:
    enabled: true
  lever:
    enabled: true
  # ... more sources
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

Open `http://127.0.0.1:8000`. Monitoring starts stopped; start it from the Web UI. The compatible `python -m hirevia web` command is also available.

### Run a single scan

```bash
python -m hirevia scan
```

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
| `start` | Run continuous scanning with notifications |
| `scan` | Run a single scan cycle |
| `stats` | Show database statistics |
| `status` | Show recent jobs and their status |
| `sources` | List configured sources |
| `monitor` | Continuously process Telegram channel messages |
| `web` | Run the Web UI for administration or debugging |
| `ai status`, `ai test`, `ai set-primary` | Inspect and manage AI providers |
| `test` | Run self-tests |
| `version` | Show version |

## Security

- The web server binds to localhost by default. Non-local binding requires `HIREVIA_ADMIN_TOKEN`; API calls use bearer authentication and the browser prompts for the token.
- Put the token in the server's `.env` file, keep it private, and put remote access behind a TLS reverse proxy. Do not expose an unauthenticated administrative interface directly to the internet.
- API keys and Telegram API/session credentials are not returned by the web API. Keep `.env` and `sessions/` private and out of source control.
- Set `HIREVIA_HOST` and `HIREVIA_PORT` to configure the bind address and port. For example, a reverse proxy may connect to a loopback-bound Hirevia server on the VPS.

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