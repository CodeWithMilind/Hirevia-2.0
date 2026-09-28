# Hirevia 2.0

A CLI-first autonomous job discovery agent that finds fresh, relevant job opportunities and delivers them to you via messaging channels.

## Architecture

```
Discover → Extract → Normalize → Deduplicate → Freshness → Filter → AI Match → Verify → Notify → Track → Expire
```

## Features

- **CLI-first** - No web dashboard, everything runs from terminal
- **YAML-based filtering** - User-editable filters for roles, locations, skills, experience
- **Multi-source** - Company careers, ATS (Greenhouse, Lever, Ashby), job boards
- **AI-powered relevance** - NVIDIA-hosted LLM for semantic job matching
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

### Start the agent

```bash
python -m hirevia start
```

You'll be prompted for your NVIDIA API key (never stored, only in memory).

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
| `test` | Run self-tests |
| `version` | Show version |

## Security

- **NVIDIA API key**: Prompted at runtime, never written to disk
- **Telegram token**: Prompted at runtime, never written to disk
- **No secrets in logs**: API keys and tokens are redacted
- **Local-only**: SQLite database stays on your machine

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