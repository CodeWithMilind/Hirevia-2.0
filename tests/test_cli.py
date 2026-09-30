"""CLI wiring tests: key resolution, notifier setup and report rendering.

The pipeline itself is stubbed - these tests only check that the CLI builds its
parts correctly, degrades gracefully when credentials are missing, and prints
every ScanStats field the pipeline reports.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from hirevia.ai.client import AIInvalidResponseError
from hirevia.ai.matcher import MATCH_RESPONSE_SCHEMA
from hirevia import cli as cli_module
from hirevia.cli import _build_notifier, _print_stats, _resolve_nvidia_key, cli
from hirevia.config import Config
from hirevia.core.models import ApplicationStatus, EmploymentType, Job, ScanStats
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

    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
        seen["notifier"] = notifier
        seen["ai"] = ai_matcher
        seen["diagnostic_output"] = diagnostic_output
        seen["ai_progress_callback"] = ai_progress_callback
        return make_stats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    monkeypatch.setattr(
        cli_module,
        "_build_notifier",
        lambda *a, **k: pytest.fail("diagnostic mode must not create a notifier"),
    )

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--notify", "--diagnostic", "--db-path", str(tmp_path / "cli.db")]
    )

    assert result.exit_code == 0, result.output
    assert seen["notifier"] is None and seen["ai"] is None
    assert seen["diagnostic_output"] is True
    assert seen["ai_progress_callback"] is None
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

    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
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


def test_scan_prints_jobs_sequentially_after_processing(monkeypatch, tmp_path):
    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
        assert diagnostic_output is False
        assert ai_progress_callback is not None
        jobs = [
            (SimpleNamespace(
                title="Software Engineer", company="Amazon",
                application_url="https://jobs.example.com/amazon",
                experience="0-1 years", location="Pune, Maharashtra",
                work_mode="ignored", employment_type=EmploymentType.FULL_TIME,
                posted_at=datetime(2026, 9, 30),
                raw_data={"source_channel": "Freshershunt", "work_mode": "Hybrid"},
            ), "MATCHED", 87, "ai_response"),
            (SimpleNamespace(
                title="Customer Support Executive", company="ABC",
                application_url="https://jobs.example.com/abc",
                experience="Fresher", location="Pune", work_mode="WFO",
                employment_type=EmploymentType.INTERNSHIP,
                posted_at=datetime(2026, 9, 30), raw_data={},
            ), "NOT MATCHED", 31, "ai_response"),
            (SimpleNamespace(
                title="Data Scientist", company="XYZ",
                application_url="https://jobs.example.com/xyz",
                experience=None, location=None, employment_type=None,
                posted_at=None, raw_data={"source_name": "LinkedIn"},
            ), "AI ERROR", 62, "ai_error"),
            (SimpleNamespace(
                source="telegram", title="Software Developer", company="XYZ",
                application_url=None,
                experience=None, location=None, employment_type=None,
                posted_at=None, raw_data={},
            ), "AI SKIPPED", 58, "skipped"),
        ]
        ai_progress_callback("start", total=len(jobs))
        for index, (job, status, score, outcome) in enumerate(jobs, 1):
            ai_progress_callback("processing", index=index, total=len(jobs), job=job)
            if outcome == "ai_error":
                ai_progress_callback("sending_ai", job=job)
                ai_progress_callback(
                    "ai_error",
                    job=job,
                    verdict=SimpleNamespace(ai_error="secret APITimeoutError: timeout"),
                )
            elif outcome == "ai_response":
                ai_progress_callback("sending_ai", job=job)
                ai_progress_callback("ai_response", job=job)
            else:
                ai_progress_callback("skipped", job=job)
            ai_progress_callback(
                "job_complete", index=index, total=len(jobs), job=job,
                status=status, score=score,
            )
        return ScanStats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "progress.db")]
    )

    assert result.exit_code == 0, result.output
    output = result.output
    first_processing = output.index("[1/4] Software Engineer — Amazon")
    first_send = output.index("→ Sending job to AI matcher...")
    first_response = output.index("→ AI response received")
    first_result = output.index("Status      : MATCHED")
    second_processing = output.index("[2/4] Customer Support Executive — ABC")
    assert first_processing < first_send < first_response < first_result < second_processing
    assert "→ AI ERROR | timeout" in output
    assert "HIREVIA JOB PROCESSING" in output
    assert "[2/4] Customer Support Executive — ABC" in output
    assert "[3/4] Data Scientist — XYZ" in output
    assert "[4/4] Software Developer — XYZ" in output
    assert "DONE" in output
    assert "Company     : Amazon" in result.output
    assert "Experience  : 0–1 years" in result.output
    assert "Location    : Pune, Maharashtra" in result.output
    assert "Work Mode   : Hybrid" in result.output
    assert "Employment  : Full-time" in result.output
    assert "Posted      : 30 Sep 2026" in result.output
    assert "Source      : Freshershunt" in result.output
    assert "Apply       : https://jobs.example.com/amazon" in result.output
    assert "Status      : MATCHED" in result.output
    assert "Score       : 87" in result.output
    assert "Priority    : P1" in result.output
    assert "Status      : NOT MATCHED" in result.output
    assert "Priority    : P3" in result.output
    assert "Status      : AI ERROR" in result.output
    assert "Source      : LinkedIn" in result.output
    assert "Experience  : Not specified" in result.output
    assert "Location    : Not specified" in result.output
    assert "Work Mode   : Unknown" in result.output
    assert "Employment  : Not specified" in result.output
    assert "Posted      : Unknown" in result.output
    assert "Source      : Telegram" in result.output
    assert "Apply       : Not available" in result.output
    assert "Status      : AI SKIPPED" in result.output
    assert "Score       : 58" in result.output
    assert "Priority    : P2" in result.output
    assert "secret" not in result.output
    assert "APITimeoutError" not in result.output
    assert "nvidia/test-model" not in result.output
    assert "NVIDIA" not in result.output
    assert "HIREVIA SCAN COMPLETE" in result.output
    assert "Processed       : 4" in result.output
    assert "Matched         : 1" in result.output
    assert "Not Matched     : 1" in result.output
    assert "AI Skipped      : 1" in result.output
    assert "AI Errors       : 1" in result.output


@pytest.mark.parametrize(
    ("score", "expected"),
    [(81, "P1"), (80, "P2"), (51, "P2"), (50, "P3"), (21, "P3"), (20, "No Priority")],
)
def test_ai_progress_priority_boundaries(capsys, score, expected):
    from hirevia.cli import _make_ai_progress_reporter

    report = _make_ai_progress_reporter()
    report("start", total=1)
    job = SimpleNamespace(title="Engineer", company="Example", application_url=None,
                          experience=None, location=None, employment_type=None,
                          posted_at=None, raw_data={}, source="test")
    report("processing", index=1, total=1, job=job)
    report(
        "job_complete", index=1, total=1, job=job,
        status="NOT MATCHED", score=score,
    )
    report("complete")

    assert f"Priority    : {expected}" in capsys.readouterr().out


def test_ai_review_empty_scan_prints_zero_summary(capsys):
    from hirevia.cli import _make_ai_progress_reporter

    report = _make_ai_progress_reporter()
    report("start", total=0)
    report("complete")

    output = capsys.readouterr().out
    assert "Processed       : 0" in output
    assert "HIREVIA SCAN COMPLETE" in output


def test_scan_prints_final_scan_summary_without_notifications(monkeypatch, tmp_path):
    seen = {}

    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
        seen["notifier"] = notifier
        seen["diagnostic_output"] = diagnostic_output
        return ScanStats(
            jobs_notification_eligible=1,
            top_opportunities=[{
                "title": "Software Engineer",
                "company": "Amazon",
                "match_score": 87,
                "priority": "P1",
                "location": "Pune",
                "application_url": "https://jobs.example.com/amazon/123",
            }],
        )

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "final.db")]
    )

    assert result.exit_code == 0, result.output
    assert seen == {"notifier": None, "diagnostic_output": False}
    assert "HIREVIA SCAN COMPLETE" in result.output
    assert "Processed       : 0" in result.output
    assert "Telegram notifications: DISABLED (--no-notify)" in result.output


def test_scan_prints_empty_final_results(monkeypatch, tmp_path):
    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
        return ScanStats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())

    result = CliRunner().invoke(
        cli, ["scan", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "empty.db")]
    )

    assert result.exit_code == 0, result.output
    assert "Processed       : 0" in result.output


def test_monitor_uses_telegram_only_and_no_notify_still_starts(monkeypatch, tmp_path):
    config = Config()
    config.sources.sources["greenhouse"].enabled = True
    source_calls = []
    pipeline_configs = []

    class FakeTelegramSource:
        last_collection_failed = False
        has_more_backfill = False
        retry_count = 0

        async def start_continuous_monitoring(self):
            source_calls.append("started")
            return 7

        def enable_event_mode(self):
            source_calls.append("event_mode")

        def retry_uncommitted_event(self):
            return False

        async def stop_continuous_monitoring(self):
            source_calls.append("stopped")

    source = FakeTelegramSource()

    class FakePipeline:
        def __init__(self, monitor_config, repo, ai_matcher, notifier, **kwargs):
            pipeline_configs.append(monitor_config)
            self.registry = SimpleNamespace(get_source=lambda name: source if name == "telegram" else None)
            self.scan_calls = 0

        async def run_scan(self):
            self.scan_calls += 1
            if self.scan_calls == 1:
                return ScanStats()
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli_module, "_build_ai_matcher", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_module,
        "_build_notifier",
        lambda *a, **k: pytest.fail("--no-notify must not create a notifier"),
    )

    result = CliRunner().invoke(
        cli, ["monitor", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "monitor.db")]
    )

    assert result.exit_code == 0, result.output
    assert source_calls == ["started", "event_mode", "stopped"]
    assert pipeline_configs[0].sources.get_enabled_sources() == ["telegram"]
    assert config.sources.get_enabled_sources() == ["greenhouse", "telegram"]
    assert "✓ Telegram monitoring started" in result.output
    assert "✓ Monitoring 7 channels" in result.output
    assert "✓ Persistent message tracking enabled" in result.output
    assert "Waiting for new opportunities..." in result.output
    assert "Telegram notifications: DISABLED (--no-notify)" in result.output


def test_monitor_normal_output_is_concise_and_keeps_job_card(monkeypatch, tmp_path):
    config = Config()
    source_calls = []

    class FakeTelegramSource:
        last_collection_failed = False
        has_more_backfill = False
        retry_count = 0

        async def start_continuous_monitoring(self):
            return 7

        def enable_event_mode(self):
            source_calls.append("event_mode")

        def retry_uncommitted_event(self):
            self.retry_count += 1
            return self.retry_count == 2

        async def stop_continuous_monitoring(self):
            source_calls.append("stopped")

    source = FakeTelegramSource()
    job = Job(
        source="telegram",
        source_job_id="job-1",
        title="Python Developer Intern",
        company="TestCompany",
        location="Pune",
        description="Internship for freshers",
        url="https://t.me/jobs/101",
        application_url="https://example.com/job",
        experience="Freshers",
        employment_type=EmploymentType.INTERNSHIP,
    )

    class FakePipeline:
        def __init__(self, monitor_config, repo, ai_matcher, notifier, **kwargs):
            self.registry = SimpleNamespace(get_source=lambda name: source)
            self.report_progress = kwargs["ai_progress_callback"]
            self.scan_calls = 0

        async def run_scan(self):
            self.scan_calls += 1
            if self.scan_calls == 1:
                return ScanStats()
            if self.scan_calls == 2:
                telegram_logger = logging.getLogger("hirevia.sources.telegram_source")
                telegram_logger.info("[Telegram] New message: channel=jobs_alpha message_id=101")
                telegram_logger.debug("[Telegram] [Filter] REJECTED promotional: course content")
                telegram_logger.info("[Telegram] New message: channel=jobs_alpha message_id=102")
                telegram_logger.debug("[Telegram] New message detected in jobs_alpha #102")
                self.report_progress("processing", index=1, total=1, job=job)
                self.report_progress("sending_ai")
                self.report_progress("ai_response")
                self.report_progress("job_complete", index=1, total=1, job=job, status="MATCHED", score=68)
                telegram_logger.info("[Telegram] New message: channel=jobs_alpha message_id=103")
                logging.getLogger("hirevia.core.pipeline").debug(
                    "[Pipeline] Stage 3: duplicate of database-id - 'Python Developer Intern'"
                )
                telegram_logger.warning("[Telegram] Connection lost; reconnecting")
                telegram_logger.info("[Telegram] Reconnected")
                logging.getLogger("hirevia.core.pipeline").info(
                    "[Pipeline] Stage 2 done: 3 message(s), 2 detected, 2 normalized, 2 to process"
                )
                logging.getLogger("hirevia.core.pipeline").info(
                    "[Pipeline] Scan finished in 0.6s: found=2 new=1 dup=1 promo_rejected=1"
                )
                logging.getLogger("hirevia.ai.client").error(
                    "provider response API_KEY-CANARY " + "provider details " * 100
                )
                return ScanStats(messages_checked=3, jobs_detected=2, jobs_duplicate=1)
            if self.scan_calls == 3:
                logging.getLogger("hirevia.core.pipeline").error(
                    "provider error API_KEY-CANARY " + "stack trace details " * 100
                )
                return ScanStats(messages_checked=1, jobs_failed=1)
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli_module, "_build_ai_matcher", lambda *a, **k: None)

    async def no_sleep(*_args):
        return None

    monkeypatch.setattr(cli_module.asyncio, "sleep", no_sleep)

    result = CliRunner().invoke(
        cli,
        ["monitor", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "monitor.db")],
    )

    assert result.exit_code == 0, result.output
    for event in (
        "[Telegram] New message received",
        "[Telegram] New message ignored — not a job",
        "[Telegram] New job detected",
        "[Telegram] Duplicate ignored",
        "[Telegram] Processing failed — retrying",
        "[Telegram] Connection lost — reconnecting",
        "[Telegram] Reconnected",
        "[Telegram] Processing completed",
    ):
        assert event in result.output
    for card_line in (
        "[1/1] Python Developer Intern — TestCompany",
        "    Processing...",
        "    → Sending job to AI matcher...",
        "    → AI response received",
        "Company     : TestCompany",
        "Experience  : Freshers",
        "Location    : Pune",
        "Work Mode   : Unknown",
        "Employment  : Internship",
        "Apply       : https://example.com/job",
        "Status      : MATCHED",
        "Score       : 68",
        "Priority    : P2",
    ):
        assert card_line in result.output
    for internal in (
        "message_id", "database-id", "Stage 2 done", "Scan finished",
        "promo_rejected", "API_KEY-CANARY", "provider details", "stack trace details",
    ):
        assert internal not in result.output


def test_monitor_verbose_keeps_pipeline_diagnostics(monkeypatch, tmp_path, caplog):
    config = Config()

    class FakeTelegramSource:
        last_collection_failed = False
        has_more_backfill = False

        async def start_continuous_monitoring(self):
            return 2

        def enable_event_mode(self):
            pass

        def retry_uncommitted_event(self):
            return False

        async def stop_continuous_monitoring(self):
            pass

    source = FakeTelegramSource()

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            self.registry = SimpleNamespace(get_source=lambda name: source)
            self.scan_calls = 0

        async def run_scan(self):
            self.scan_calls += 1
            logging.getLogger("hirevia.core.pipeline").info(
                "[Pipeline] Stage 2 done: 1 message(s), 0 detected, 0 normalized, 0 to process"
            )
            if self.scan_calls == 1:
                return ScanStats()
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli_module, "_build_ai_matcher", lambda *a, **k: None)
    caplog.set_level(logging.INFO, logger="hirevia.core.pipeline")

    result = CliRunner().invoke(
        cli,
        ["monitor", "--verbose", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "verbose.db")],
    )

    assert result.exit_code == 0, result.output
    assert "[Pipeline] Stage 2 done: 1 message(s)" in caplog.text
    assert "HIREVIA SCAN COMPLETE" in result.output


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
    httpx_logger = logging.getLogger("httpx")
    original_handlers = root.handlers
    original_level = root.level
    original_telethon_level = telethon_logger.level
    original_httpx_level = httpx_logger.level
    root.handlers = []
    try:
        monkeypatch.delenv("HIREVIA_LOG_LEVEL", raising=False)
        cli_module.configure_logging()
        assert telethon_logger.level == logging.WARNING
        assert httpx_logger.level == logging.WARNING
        first_count = len(root.handlers)
        cli_module.configure_logging()
        assert first_count == len(root.handlers) == 1
        cli_module.configure_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert root.handlers[0].level == logging.DEBUG
        assert telethon_logger.level == logging.DEBUG
        assert httpx_logger.level == logging.DEBUG
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
        httpx_logger.setLevel(original_httpx_level)


def test_scan_verbose_option_enables_debug_logs(monkeypatch, tmp_path):
    seen = {}

    async def fake_scan(
        config, repo, ai_matcher=None, notifier=None, diagnostic_output=False,
        ai_progress_callback=None,
    ):
        seen["level"] = logging.getLogger().level
        return ScanStats()

    monkeypatch.setattr(cli_module, "run_single_scan", fake_scan)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    result = CliRunner().invoke(
        cli, ["scan", "--verbose", "--no-ai", "--no-notify", "--db-path", str(tmp_path / "verbose.db")]
    )

    assert result.exit_code == 0, result.output
    assert seen["level"] == logging.DEBUG


def test_unimplemented_sources_are_disabled_by_default():
    config = Config()
    for name in ("naukri", "indeed", "internshala", "cutshort", "unstop"):
        assert name in config.sources.sources
        assert config.sources.sources[name].enabled is False
    assert config.sources.sources["telegram"].enabled is True


def test_ai_status_reports_configuration_without_secrets(monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: Config())
    monkeypatch.setenv("GROQ_API_KEY", "groq-CANARY-do-not-print")

    result = CliRunner().invoke(cli, ["ai", "status"])

    assert result.exit_code == 0, result.output
    assert "Hirevia AI Providers" in result.output
    assert "NVIDIA (primary)" in result.output
    assert "GROQ" in result.output and "configured" in result.output
    assert "groq-CANARY-do-not-print" not in result.output


def test_httpx2_logging_is_quiet_at_info_and_visible_at_debug():
    logger = logging.getLogger("httpx2")
    previous_level = logger.level
    try:
        cli_module.configure_logging(verbose=False)
        assert logger.level == logging.WARNING
        assert not logger.isEnabledFor(logging.INFO)

        cli_module.configure_logging(verbose=True)
        assert logger.level == logging.DEBUG
        assert logger.isEnabledFor(logging.INFO)
    finally:
        logger.setLevel(previous_level)


def test_ai_set_primary_persists_configured_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("LLAMACPP_API_KEY", "llama-secret-CANARY")
    monkeypatch.setenv("LLAMACPP_MODEL", "hirevia-qwen3-1.7b")
    monkeypatch.setattr(cli_module, "load_config", lambda: Config(ai={"primary": "nvidia"}))

    result = CliRunner().invoke(cli, ["ai", "set-primary", "llamacpp"])

    assert result.exit_code == 0, result.output
    assert "Previous: nvidia" in result.output
    assert "Primary : llamacpp" in result.output
    assert "Model   : hirevia-qwen3-1.7b" in result.output
    assert "llama-secret-CANARY" not in result.output
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == "AI_PRIMARY_PROVIDER=llamacpp"


def test_ai_set_primary_nvidia_and_status_show_selection(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-secret-CANARY")
    monkeypatch.setattr(cli_module, "load_config", lambda: Config(ai={"primary": "llamacpp"}))

    result = CliRunner().invoke(cli, ["ai", "set-primary", "nvidia"])
    monkeypatch.setattr(cli_module, "load_config", lambda: Config(ai={"primary": "nvidia"}))
    status = CliRunner().invoke(cli, ["ai", "status"])

    assert result.exit_code == 0, result.output
    assert "nvidia-secret-CANARY" not in result.output
    assert status.exit_code == 0, status.output
    assert "NVIDIA (primary)" in status.output
    assert "nvidia-secret-CANARY" not in status.output


def test_ai_set_primary_unknown_or_unconfigured_leaves_config_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PRIMARY_PROVIDER", "nvidia")
    monkeypatch.setattr(cli_module, "load_config", lambda: Config(ai={"primary": "nvidia"}))

    unknown = CliRunner().invoke(cli, ["ai", "set-primary", "unknown"])
    unconfigured = CliRunner().invoke(cli, ["ai", "set-primary", "groq"])

    assert unknown.exit_code != 0
    assert "Unknown AI provider" in unknown.output
    assert unconfigured.exit_code != 0
    assert "Status: not configured" in unconfigured.output
    assert not (tmp_path / ".env").exists()
    assert os.environ["AI_PRIMARY_PROVIDER"] == "nvidia"


def test_ai_test_calls_only_selected_primary_provider(monkeypatch):
    called = []

    class FakeProvider:
        model = "hirevia-qwen3-1.7b"

        async def structured_completion(self, **kwargs):
            called.append(kwargs)
            return {
                "match": True,
                "match_score": 72,
                "priority": "P2",
                "reason": "Python backend skills fit the role.",
                "matched_skills": ["Python", "SQL"],
            }

    config = Config(ai={"primary": "llamacpp"})
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(cli_module, "provider_status", lambda config: [
        {"name": "llamacpp", "model": "hirevia-qwen3-1.7b", "configured": True}
    ])
    monkeypatch.setattr(cli_module, "build_ai_provider_router", lambda config: SimpleNamespace(
        primary="llamacpp", providers={"llamacpp": FakeProvider()}
    ))

    result = CliRunner().invoke(cli, ["ai", "test"])

    assert result.exit_code == 0, result.output
    assert "Provider: llamacpp" in result.output
    assert "Model: hirevia-qwen3-1.7b" in result.output
    assert "✓ Connection successful" in result.output
    assert "✓ Model responded" in result.output
    assert len(called) == 1
    assert called[0]["schema"] == MATCH_RESPONSE_SCHEMA
    assert called[0]["temperature"] == 0.1
    assert called[0]["max_tokens"] == 1200
    assert called[0]["messages"][0]["role"] == "system"
    assert called[0]["messages"][1]["role"] == "user"
    assert "Junior Python Backend Developer" in called[0]["messages"][1]["content"]


def test_ai_test_rejects_unusable_matching_response(monkeypatch):
    class FakeProvider:
        model = "openai/gpt-oss-20b"

        async def structured_completion(self, **kwargs):
            raise AIInvalidResponseError("model returned unusable JSON: Expecting value")

    config = Config(ai={"primary": "nvidia"})
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(cli_module, "provider_status", lambda config: [
        {"name": "nvidia", "model": "openai/gpt-oss-20b", "configured": True}
    ])
    monkeypatch.setattr(cli_module, "build_ai_provider_router", lambda config: SimpleNamespace(
        primary="nvidia", providers={"nvidia": FakeProvider()}
    ))

    result = CliRunner().invoke(cli, ["ai", "test"])

    assert result.exit_code == 1
    assert "Provider test failed" in result.output
    assert "AI provider is ready" not in result.output


def test_router_removes_primary_from_fallback_and_keeps_other_providers():
    from hirevia.ai.providers import AIProviderRouter

    router = AIProviderRouter({}, "llamacpp", ["nvidia", "groq", "ollama", "llamacpp"])

    assert router.fallback == ["nvidia", "groq", "ollama"]


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
