"""CLI wiring tests: key resolution, notifier setup and report rendering.

The pipeline itself is stubbed - these tests only check that the CLI builds its
parts correctly, degrades gracefully when credentials are missing, and prints
every ScanStats field the pipeline reports.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from hirevia import cli as cli_module
from hirevia.cli import _build_notifier, _print_stats, _resolve_nvidia_key, cli
from hirevia.config import Config
from hirevia.core.models import ApplicationStatus, Job, ScanStats
from hirevia.notifications.telegram import TelegramNotifier


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("NVIDIA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
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


def test_nvidia_key_falls_back_to_prompt(monkeypatch):
    monkeypatch.setattr(cli_module, "get_nvidia_api_key", lambda: "nv-prompted")
    assert _resolve_nvidia_key() == "nv-prompted"


def test_nvidia_key_is_optional_when_prompting_is_impossible(monkeypatch):
    def boom():
        raise EOFError

    monkeypatch.setattr(cli_module, "get_nvidia_api_key", boom)
    assert _resolve_nvidia_key() is None


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

def test_scan_reports_every_pipeline_metric(monkeypatch, tmp_path):
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
    for label in ("Messages Checked", "Jobs Detected", "AI Failures (degraded)",
                  "Link Unusable", "Application Closed", "Alerts Suppressed",
                  "Failed Jobs", "Errors:"):
        assert label in result.output


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
    _print_stats(ScanStats(jobs_found=1, jobs_new=1))
    out = capsys.readouterr().out
    assert "AI Failures" not in out  # only shown when something degraded
    assert "Duration" in out


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
