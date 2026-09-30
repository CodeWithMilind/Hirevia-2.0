"""CLI wiring tests: key resolution, notifier setup and report rendering.

The pipeline itself is stubbed - these tests only check that the CLI builds its
parts correctly, degrades gracefully when credentials are missing, and prints
every ScanStats field the pipeline reports.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from hirevia import cli as cli_module
from hirevia.cli import _build_notifier, _print_stats, _resolve_nvidia_key, cli
from hirevia.config import Config
from hirevia.core.models import ApplicationStatus, Job, ScanStats
from hirevia.notifications.telegram import TelegramNotifier
from hirevia.sources.telegram_config import TelegramChannel
from hirevia.sources.telegram_extract import TgMessage
from hirevia.sources.telegram_source import TelegramSource


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("NVIDIA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HIREVIA_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


def make_stats():
    return ScanStats(
        sources_scanned=2, messages_checked=120, jobs_detected=30, jobs_normalized=9,
        jobs_found=9, jobs_new=5, jobs_duplicate=2, jobs_filtered=2,
        jobs_ai_matched=4, jobs_ai_rejected=1, ai_failures=1,
        jobs_verified=3, jobs_unverified=1, jobs_closed=1, jobs_link_rejected=2,
        jobs_notified=2, jobs_skipped_notification=3, jobs_failed=1,
        errors=["telegram: RuntimeError: boom"], duration_seconds=12.5,
    )


# --- API key + notifier resolution -------------------------------------------

def test_nvidia_key_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "  nv-abc  ")
    assert _resolve_nvidia_key() == "nv-abc"


def test_nvidia_key_is_never_prompted_for(monkeypatch):
    monkeypatch.setattr(cli_module, "get_nvidia_api_key", lambda: pytest.fail("must not prompt"), raising=False)
    assert _resolve_nvidia_key() is None


def test_provider_configuration_can_enable_groq_without_nvidia(monkeypatch):
    seen = {}

    class FakeMatcher:
        def __init__(self, config, api_key):
            seen["api_key"] = api_key

    monkeypatch.setattr(cli_module, "provider_status", lambda config: [
        {"name": "nvidia", "configured": False},
        {"name": "groq", "configured": True},
    ])
    monkeypatch.setattr(cli_module, "AIMatcher", FakeMatcher)

    matcher = cli_module._build_ai_matcher(Config())

    assert isinstance(matcher, FakeMatcher)
    assert seen["api_key"] is None


def test_notifier_is_built_from_environment_with_retry_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    config = Config(notification_retry_attempts=5, notification_retry_delay_seconds=30)

    notifier = _build_notifier(config, None, None)

    assert isinstance(notifier, TelegramNotifier)
    assert notifier.chat_id == "42" and notifier.bot_token == "123:token"
    assert notifier._retry_attempts == 5 and notifier._retry_delay == 30


def test_notifier_is_skipped_without_credentials(monkeypatch):
    monkeypatch.setattr(cli_module, "get_telegram_token", lambda: "")
    monkeypatch.setattr(cli_module.click, "prompt",
                        lambda *a, **k: (_ for _ in ()).throw(EOFError))

    assert _build_notifier(Config(), None, None) is None


def test_explicit_flags_win_over_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-id")
    notifier = _build_notifier(Config(), "flag:token", "flag-id")
    assert notifier.bot_token == "flag:token" and notifier.chat_id == "flag-id"


# --- commands ------------------------------------------------------------------

def test_scan_reports_compact_summary_and_real_counts(monkeypatch, tmp_path):
    seen = {}

    async def fake_scan(config, repo, ai_matcher=None, notifier=None):
        seen["notifier"] = notifier
        seen["ai"] = ai_matcher
        return make_stats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "cli.db")]
    )

    assert result.exit_code == 0, result.output
    assert seen["notifier"] is None and seen["ai"] is None
    for label in ("Hirevia Scan", "Messages scanned", "Opportunities", "Noise filtered",
                  "Profile matched", "High-quality", "AI provider", "AI degraded",
                  "Scan completed", "Errors requiring attention"):
        assert label in result.output
    for diagnostic in ("AI Matched", "Jobs Reaching Matching", "Stage 5", "Excluded keywords"):
        assert diagnostic not in result.output


def test_scan_closes_the_notifier_it_created(monkeypatch, tmp_path):
    closed = []

    class SpyNotifier(TelegramNotifier):
        async def close(self):
            closed.append(True)
            await self.client.aclose()

    async def fake_scan(config, repo, ai_matcher=None, notifier=None):
        assert isinstance(notifier, SpyNotifier), "pipeline must receive the notifier"
        return ScanStats(jobs_notified=1)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(cli_module, "TelegramNotifier", SpyNotifier)
    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--notify", "--db-path", str(tmp_path / "cli.db")]
    )
    assert result.exit_code == 0, result.output
    assert closed == [True]


def test_status_shows_freshness_and_link_verdict(monkeypatch, tmp_path):
    job = Job(
        source="telegram", source_job_id="alpha-1", title="Backend Developer",
        company="Acme", location="Pune", description="Python",
        url="https://t.me/alpha/1", application_url="https://jobs.lever.co/acme/1",
        application_status=ApplicationStatus.OPEN,
        posted_at=datetime.utcnow() - timedelta(hours=2),
        raw_data={"freshness_label": "fresh", "application_url_quality": "ats"},
        ai_decision={"match_score": 88},
    )
    monkeypatch.setattr(cli_module.JobRepository, "get_recent_jobs", lambda self, **k: [job])

    result = CliRunner().invoke(cli, ["status", "--db-path", str(tmp_path / "none.db")])

    assert result.exit_code == 0, result.output
    assert "Fresh" in result.output and "Link" in result.output
    assert "fresh" in result.output and "ats" in result.output
    assert "88%" in result.output


def test_print_stats_stays_quiet_on_a_healthy_scan(capsys):
    _print_stats(ScanStats(
        jobs_found=1, jobs_new=1,
        top_opportunities=[{
            "title": "Python Developer", "company": "Example", "match_score": 88,
            "priority": "P1", "location": "Pune", "application_url": "https://example.com/apply",
        }],
    ))
    out = capsys.readouterr().out
    assert "AI Failures" not in out  # only shown when something degraded
    assert "Scan completed" in out
    assert "Python Developer" in out


def test_telegram_message_details_are_debug_not_normal_info(caplog):
    caplog.set_level(logging.INFO)
    source = TelegramSource()
    source._normalize_messages(
        TelegramChannel(username="jobs_alpha"),
        [TgMessage("jobs_alpha", 1, datetime.utcnow(), "Hiring Python Developer. Apply: https://jobs.lever.co/acme/1")],
    )

    assert "New message detected" not in caplog.text
    assert "[Filter] ACCEPTED" not in caplog.text

    caplog.clear()
    caplog.set_level(logging.DEBUG)
    source._normalize_messages(
        TelegramChannel(username="jobs_alpha"),
        [TgMessage("jobs_alpha", 2, datetime.utcnow(), "Hiring Python Developer. Apply: https://jobs.lever.co/acme/2")],
    )
    assert "New message detected" in caplog.text
    assert "[Filter] ACCEPTED" in caplog.text


def test_logging_configuration_is_idempotent_and_verbose_is_debug(monkeypatch):
    root = logging.getLogger()
    telethon_logger = logging.getLogger("telethon")
    original_handlers = root.handlers
    original_level = root.level
    original_telethon_level = telethon_logger.level
    root.handlers = []
    try:
        monkeypatch.delenv("HIREVIA_LOG_LEVEL", raising=False)
        cli_module.configure_logging()
        assert telethon_logger.level == logging.WARNING
        first_count = len(root.handlers)
        cli_module.configure_logging()
        assert first_count == len(root.handlers) == 1
        cli_module.configure_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert root.handlers[0].level == logging.DEBUG
        assert telethon_logger.level == logging.DEBUG
        monkeypatch.setenv("HIREVIA_LOG_LEVEL", "DEBUG")
        cli_module.configure_logging()
        assert root.level == logging.DEBUG
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)
        telethon_logger.setLevel(original_telethon_level)


def test_scan_verbose_option_enables_debug_logs(monkeypatch, tmp_path):
    seen = {}

    async def fake_scan(config, repo, ai_matcher=None, notifier=None):
        seen["level"] = logging.getLogger().level
        return ScanStats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    result = CliRunner().invoke(
        cli, ["scan", "--verbose", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "verbose.db")]
    )

    assert result.exit_code == 0, result.output
    assert seen["level"] == logging.DEBUG


def test_ai_status_reports_configuration_without_secrets(monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    monkeypatch.setenv("GROQ_API_KEY", "groq-CANARY-do-not-print")

    result = CliRunner().invoke(cli, ["ai", "status"])

    assert result.exit_code == 0, result.output
    assert "Hirevia AI Providers" in result.output
    assert "NVIDIA (primary)" in result.output
    assert "GROQ" in result.output and "configured" in result.output
    assert "groq-CANARY-do-not-print" not in result.output


def test_start_loop_survives_errors_and_closes_the_notifier(monkeypatch, tmp_path):
    """One broken scan must not kill the daemon, and Ctrl-C must release it."""
    closed = []

    class SpyNotifier(TelegramNotifier):
        async def close(self):
            closed.append(True)
            await self.client.aclose()

    calls = {"n": 0}

    async def fake_scan(config, repo, ai_matcher=None, notifier=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("source exploded")
        raise KeyboardInterrupt  # user pressed Ctrl-C

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(cli_module, "TelegramNotifier", SpyNotifier)
    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    monkeypatch.setattr(cli_module, "asyncio", SimpleNamespace(sleep=no_sleep, run=asyncio.run))

    result = CliRunner().invoke(
        cli, ["start", "--interval", "0", "--no-ai", "--notify",
              "--db-path", str(tmp_path / "loop.db")]
    )

    assert result.exit_code == 0, result.output
    assert calls["n"] == 2          # the error did not abort the loop
    assert closed == [True]         # ... but stopping always cleans up
    assert "Stopping" in result.output
