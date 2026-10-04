"""The web adapter must use the shared Hirevia core objects."""

from hirevia.config import Config
from hirevia import cli as cli_module
from hirevia.storage.state import JobRepository
from hirevia.web import state as web_state
from hirevia.web import main as web_main
import asyncio
import logging
import os
from click.testing import CliRunner
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hirevia.core.models import AIResponse, ApplicationStatus, Job, Priority
from hirevia.core.models import ScanStats
from hirevia.notifications.telegram import PriorityTelegramNotifier
from hirevia.sources.telegram_config import load_telegram_settings
from hirevia.web.api import routes
from hirevia.web.api.models import AISettingsRequest, ConfigFileRequest, SaveJobRequest, TelegramSourceUpdate
from hirevia.web.auth import hash_password
from hirevia.web.main import app


def configure_web_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("HIREVIA_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD_HASH", hash_password("test-password-strong"))
    monkeypatch.setenv("HIREVIA_SESSION_SECRET", "web-test-session-secret-long-enough-for-hmac")
    monkeypatch.setenv("HIREVIA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "false")


def login_web_admin(client):
    response = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "test-password-strong"},
    )
    assert response.status_code == 200, response.text


def test_web_state_uses_injected_config_repository_and_matcher(tmp_path):
    config = Config(scan_interval_minutes=17)
    repo = JobRepository(tmp_path / "shared.db")
    matcher = object()
    web_state._web_state = None
    web_state.WebUIState._instance = None

    try:
        state = web_state.initialize_web_state(config, repo, matcher)

        assert state is web_state.get_web_state()
        assert state.config is config
        assert state.repo is repo
        assert state.ai_matcher is matcher
        assert state.scan_interval_minutes == 17
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_web_scans_reuse_one_pipeline_instance(monkeypatch, tmp_path):
    created = []

    class FakePipeline:
        def __init__(self, config, repo, matcher, notifier=None):
            self.scans = 0
            created.append(self)

        async def run_scan(self):
            self.scans += 1
            return ScanStats()

    monkeypatch.setattr(web_state, "Pipeline", FakePipeline)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    config = Config()
    config.sources.sources["telegram"].enabled = False
    state = web_state.initialize_web_state(
        config, JobRepository(tmp_path / "shared-pipeline.db")
    )

    try:
        asyncio.run(state.trigger_scan())
        asyncio.run(state.trigger_scan())

        assert len(created) == 1
        assert created[0] is state.pipeline
        assert created[0].scans == 2
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_no_argument_cli_runs_service_launcher(monkeypatch):
    called = []
    monkeypatch.setattr(cli_module, "run_service", lambda: called.append(True))

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code == 0, result.output
    assert called == [True]


def test_worker_cli_command_runs_independent_service(monkeypatch):
    from hirevia import worker as worker_module

    calls = []

    async def fake_worker():
        calls.append("worker")
        return 0

    monkeypatch.setattr(cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "run_worker", fake_worker)
    result = CliRunner().invoke(cli_module.cli, ["worker"])

    assert result.exit_code == 0, result.output
    assert calls == ["worker"]


def test_port_owner_treats_connection_refused_as_available(monkeypatch):
    with cli_module.socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    assert cli_module._port_owner("127.0.0.1", port) is None


def test_port_owner_recognizes_an_existing_hirevia_server(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"service":"hirevia-web"}'

    listener = cli_module.socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setattr(cli_module, "urlopen", lambda *args, **kwargs: Response())
    try:
        assert cli_module._port_owner("127.0.0.1", listener.getsockname()[1]) == "hirevia-web"
    finally:
        listener.close()


def test_service_startup_failure_is_logged_with_traceback(monkeypatch, caplog):
    def fail_loading_config():
        raise RuntimeError("configuration unavailable")

    monkeypatch.setattr(cli_module, "load_config", fail_loading_config)

    with caplog.at_level(logging.INFO, logger="hirevia.cli"):
        with pytest.raises(RuntimeError, match="configuration unavailable"):
            cli_module.run_service("127.0.0.1", 8123)

    failure = next(record for record in caplog.records if "startup or web server failed" in record.message)
    assert failure.exc_info is not None
    assert "Hirevia launcher stopped" in caplog.text


def test_nonlocal_service_requires_admin_authentication(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIREVIA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    for name in (
        "HIREVIA_ADMIN_EMAIL",
        "HIREVIA_ADMIN_PASSWORD",
        "HIREVIA_ADMIN_PASSWORD_HASH",
        "HIREVIA_SESSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "true")
    monkeypatch.setattr(cli_module, "load_config", lambda: Config())

    result = CliRunner().invoke(cli_module.cli, ["web", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "requires configured admin authentication" in result.output


def test_admin_session_protects_web_api_without_exposing_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    configure_web_admin(monkeypatch, tmp_path)
    web_state._web_state = None
    web_state.WebUIState._instance = None

    try:
        with TestClient(app) as client:
            denied = client.get("/api/config")
            login_web_admin(client)
            allowed = client.get("/api/config")

        assert denied.status_code == 401
        assert allowed.status_code == 200
        assert "test-password-strong" not in allowed.text
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_nonlocal_service_accepts_secure_initial_admin_bootstrap(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIREVIA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HIREVIA_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD", "bootstrap-password")
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "true")
    monkeypatch.setattr(cli_module, "load_config", lambda: Config())
    monkeypatch.setattr(cli_module, "_port_owner", lambda *_args: "other-service")

    result = CliRunner().invoke(cli_module.cli, ["web", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "configured admin authentication" not in result.output
    assert "already occupied" in result.output


def test_status_api_exposes_monitor_lifecycle_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    configure_web_admin(monkeypatch, tmp_path)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    web_state.initialize_web_state(Config(), JobRepository(tmp_path / "status.db"))

    try:
        with TestClient(app) as client:
            login_web_admin(client)
            response = client.get("/api/status")

        assert response.status_code == 200
        assert response.json()["status"] == "STOPPED"
        assert response.json()["is_running"] is False
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_stats_api_exposes_matching_and_priority_diagnostics(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    configure_web_admin(monkeypatch, tmp_path)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(Config(), JobRepository(tmp_path / "stats.db"))
    state.last_stats = ScanStats(
        jobs_matching_attempted=3,
        ai_failures=1,
        jobs_matched=2,
        jobs_new=1,
        jobs_notified=1,
        priority_distribution={"P1": 1, "P2": 1, "P3": 0},
    )

    try:
        with TestClient(app) as client:
            login_web_admin(client)
            response = client.get("/api/stats")

        assert response.status_code == 200
        result = response.json()
        assert result["jobs_matching_attempted"] == 3
        assert result["ai_failures"] == 1
        assert result["jobs_matched"] == 2
        assert result["priority_distribution"] == {"P1": 1, "P2": 1, "P3": 0}
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_status_api_detects_a_stopped_monitor_worker(monkeypatch, tmp_path, caplog):
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "dead-worker.db")
    )

    async def finished_worker():
        return None

    async def prepare_worker():
        state.scanner_task = asyncio.create_task(finished_worker())
        await state.scanner_task

    asyncio.run(prepare_worker())
    state.is_running = True

    try:
        with caplog.at_level(logging.ERROR, logger="hirevia.web.state"):
            status = state.get_status()

        assert status["status"] == "ERROR"
        assert status["is_running"] is False
        assert "Web monitor worker exited unexpectedly" in caplog.text
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_stopping_an_already_stopped_monitor_is_logged(monkeypatch, tmp_path, caplog):
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "stopped-monitor.db")
    )
    monkeypatch.setattr(routes, "_state", lambda: state)

    with caplog.at_level(logging.WARNING, logger="hirevia.web.state"):
        result = asyncio.run(routes.stop_scanner())

    assert result.success is True
    assert result.status.value == "STOPPED"
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_external_stop_reports_unavailable_worker_without_queueing(monkeypatch, tmp_path):
    repo = JobRepository(tmp_path / "unavailable-worker.db")
    stale_pid = 2_000_000_000
    assert repo.claim_worker_lease(stale_pid)
    repo.update_worker_runtime(stale_pid, "RUNNING", monitor_status="SCANNING")
    repo.set_source_state("system", "monitor_desired", "stopped")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=Config(), repo=repo)
    state.external_worker = True
    monkeypatch.setattr(routes, "_state", lambda: state)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.stop_scanner())

    assert error.value.status_code == 503
    assert "worker is unavailable" in error.value.detail.casefold()
    assert repo.get_worker_runtime()["status"] == "ERROR"
    with repo._get_conn() as connection:
        assert connection.execute(
            "SELECT count(*) FROM worker_commands WHERE command = 'STOP'"
        ).fetchone()[0] == 0
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_external_stop_returns_stopped_only_after_worker_confirmation(
    monkeypatch, tmp_path
):
    repo = JobRepository(tmp_path / "confirmed-stop.db")
    pid = os.getpid()
    assert repo.claim_worker_lease(pid)
    repo.update_worker_runtime(pid, "RUNNING", monitor_status="SCANNING")
    repo.set_source_state("system", "monitor_desired", "running")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=Config(), repo=repo)
    state.external_worker = True

    async def acknowledge_stop(command, *_args, **_kwargs):
        assert command == "STOP"
        repo.set_source_state("system", "monitor_desired", "stopped")
        repo.update_worker_runtime(pid, "RUNNING", monitor_status="STOPPED")
        return {"stopped": True}

    state._send_worker_command = acknowledge_stop
    monkeypatch.setattr(routes, "_state", lambda: state)
    try:
        result = asyncio.run(routes.stop_scanner())

        assert result.success is True
        assert result.status.value == "STOPPED"
        assert result.message == "Monitor stopped"
        assert state.get_status()["status"] == "STOPPED"
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


@pytest.mark.parametrize(
    ("worker_error", "expected_message"),
    [
        ("Worker did not complete STOP within 0.01 seconds", "timeout"),
        ("RuntimeError", "rejected STOP"),
    ],
)
def test_external_stop_reports_unacknowledged_command(monkeypatch, tmp_path, worker_error, expected_message):
    from hirevia.worker_process import WorkerProcessError

    repo = JobRepository(tmp_path / "stop-command-error.db")
    pid = os.getpid()
    assert repo.claim_worker_lease(pid)
    repo.update_worker_runtime(pid, "RUNNING", monitor_status="SCANNING")
    repo.set_source_state("system", "monitor_desired", "running")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=Config(), repo=repo)
    state.external_worker = True

    async def fail_command(*_args, **_kwargs):
        raise WorkerProcessError(worker_error)

    state._send_worker_command = fail_command
    monkeypatch.setattr(routes, "_state", lambda: state)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.stop_scanner())

    assert error.value.status_code == 503
    assert expected_message.casefold() in error.value.detail.casefold()
    assert repo.get_source_state("system", "monitor_desired") == "stopped"
    assert state.get_status()["status"] == "SCANNING"


def test_dashboard_reports_unavailable_stale_scanner_as_error(monkeypatch, tmp_path):
    repo = JobRepository(tmp_path / "stale-scanner-status.db")
    stale_pid = 2_000_000_000
    assert repo.claim_worker_lease(stale_pid)
    repo.update_worker_runtime(stale_pid, "RUNNING", monitor_status="SCANNING")
    repo.set_source_state("system", "monitor_desired", "stopped")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=Config(), repo=repo)
    state.external_worker = True
    monkeypatch.setattr(routes, "_state", lambda: state)

    try:
        status = asyncio.run(routes.get_status())

        assert status.status.value == "ERROR"
        assert status.is_running is False
        assert status.is_scanning is False
        assert status.worker_alive is False
        assert status.monitor_desired == "stopped"
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_service_launcher_injects_one_runtime_and_starts_web_server(monkeypatch, tmp_path, capsys, caplog):
    config = Config(scan_interval_minutes=19)
    repo = JobRepository(tmp_path / "service.db")
    monkeypatch.setattr(cli_module, "_port_owner", lambda host, port: None)
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(cli_module, "JobRepository", lambda path: repo)
    monkeypatch.setattr(cli_module, "_build_ai_matcher", lambda loaded: None)
    launched = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: launched.append((app, kwargs)))
    web_state._web_state = None
    web_state.WebUIState._instance = None

    try:
        cli_module.run_service("127.0.0.1", 8123)

        state = web_state.get_web_state()
        assert state.config is config
        assert state.repo is repo
        assert state.ai_matcher is None
        assert launched[0][1] == {
            "host": "127.0.0.1", "port": 8123, "log_level": "info", "log_config": None,
        }
        assert launched[0][0].state.auto_start_monitoring is False
        assert "Agent      STOPPED" in capsys.readouterr().out
        assert "Hirevia command center startup requested" in caplog.text
        assert "Starting web server at http://127.0.0.1:8123" in caplog.text
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_systemd_web_lifespan_never_spawns_or_supervises_worker(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("HIREVIA_WORKER_MODE", raising=False)
    monkeypatch.setenv("HIREVIA_WORKER_MANAGED", "1")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "systemd-web.db")
    )
    state.external_worker = True
    calls = []

    class ManagedController:
        def __init__(self, repo):
            self.spawn_enabled = False
            calls.append(("init", repo))

        async def ensure_running(self, **kwargs):
            calls.append(("ensure", kwargs))
            raise AssertionError("systemd mode must not ensure or spawn a worker")

        async def supervise(self, stop_event):
            calls.append(("supervise", stop_event))
            raise AssertionError("systemd mode must not start a web supervisor")

    monkeypatch.setattr("hirevia.worker_process.WorkerProcessController", ManagedController)
    monkeypatch.setattr(web_main, "get_web_state", lambda: state)

    async def run_lifespan():
        async with web_main.lifespan(web_main.app):
            assert state.worker_controller is None
            assert calls == []

    try:
        asyncio.run(run_lifespan())
        assert calls == []
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_local_web_lifespan_does_not_start_or_restart_worker(monkeypatch, tmp_path):
    monkeypatch.delenv("HIREVIA_WORKER_MODE", raising=False)
    monkeypatch.delenv("HIREVIA_WORKER_MANAGED", raising=False)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "local-web.db")
    )
    state.external_worker = True
    worker = {"alive": True, "spawn_count": 1}

    class ForbiddenController:
        def __init__(self, repo):
            raise AssertionError("web lifespan must not create a worker controller")

    monkeypatch.setattr("hirevia.worker_process.WorkerProcessController", ForbiddenController)
    monkeypatch.setattr(web_main, "get_web_state", lambda: state)

    async def run_web_lifespans():
        for _ in range(2):
            async with web_main.lifespan(web_main.app):
                await asyncio.sleep(0)
                assert state.worker_controller is None
                assert worker["alive"] is True

    try:
        asyncio.run(run_web_lifespans())
        assert worker == {"alive": True, "spawn_count": 1}
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_terminal_log_redaction_masks_configured_secrets_and_assignments(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:bot-secret")

    message = cli_module._redact_log_text(
        "sending 123456:bot-secret API_KEY=api-secret password=hunter2"
    )

    assert "123456:bot-secret" not in message
    assert "API_KEY=api-secret" not in message
    assert "password=hunter2" not in message
    assert message.count("[REDACTED]") == 3


def test_web_feed_filters_active_24h_jobs_by_stored_priority_and_preserves_actions(tmp_path):
    config = Config()
    repo = JobRepository(tmp_path / "feed.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None

    def store_job(priority, title):
        job = Job(
            source="test",
            source_job_id=title,
            title=title,
            company="Example",
            location="Remote",
            description="Python role",
            url=f"https://example.com/{title}",
            application_status=ApplicationStatus.OPEN,
            match_result={"match": True, "match_score": 80, "priority": priority},
        )
        repo.upsert_job(job)
        return repo.get_job(job.id)

    try:
        p1 = store_job("P1", "one")
        store_job("P2", "two")
        store_job("P3", "three")
        old = store_job("P1", "old")
        with repo._get_conn() as connection:
            connection.execute(
                "UPDATE jobs SET detected_at = ?, expires_at = ?, application_status = 'expired' WHERE id = ?",
                ((datetime.utcnow() - timedelta(hours=25)).isoformat(),
                 (datetime.utcnow() - timedelta(hours=1)).isoformat(), old.id),
            )
            connection.commit()

        state = web_state.initialize_web_state(config, repo)
        assert state.get_job_counts() == {
            "ALL": 3, "P1": 1, "P2": 1, "P3": 1, "UNMATCHED": 0,
        }
        assert state.get_jobs(priority="P2")["jobs"][0]["match_priority"] == "P2"

        assert state.save_job(p1.id) is True
        assert state.get_job(p1.id)["saved"] is True
        assert state.delete_job(p1.id) is True
        assert state.get_job(p1.id) is None
        assert state.get_job_counts()["ALL"] == 2
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_unmatched_p3_below_match_threshold_is_visible_in_jobs_and_filter(tmp_path, monkeypatch):
    config = Config()
    repo = JobRepository(tmp_path / "unmatched-feed.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    job = Job(
        source="telegram",
        source_job_id="jobs_alpha-below-threshold",
        title="Python Developer",
        company="Example",
        location="Remote",
        description="Python role",
        url="https://t.me/jobs_alpha/42",
        ai_decision={
            "matched": False,
            "priority": "P3",
            "match_score": 46,
        },
        match_result={
            "matched": False,
            "match_score": 46,
            "priority": "P3",
            "reason": "Below the configured match threshold",
            "ai_status": "ok",
            "ai_score": 46,
        },
    )
    repo.upsert_job(job)

    try:
        state = web_state.initialize_web_state(config, repo)
        all_jobs = state.get_jobs()
        p3_jobs = state.get_jobs(priority="P3")
        unmatched_jobs = state.get_jobs(match_status="unmatched")

        assert all_jobs["total"] == 1
        assert all_jobs["jobs"][0]["match_status"] == "unmatched"
        assert p3_jobs["jobs"][0]["match_score"] == 46
        assert unmatched_jobs["jobs"][0]["match_priority"] == "P3"
        assert unmatched_jobs["jobs"][0]["match_status"] == "unmatched"
        monkeypatch.setattr(routes, "_state", lambda: state)
        api_response = asyncio.run(routes.get_jobs(
            search=None, source=None, status=None, priority="P3", min_match=None,
            match_status="unmatched", location=None, limit=50, offset=0,
        ))
        assert api_response.total == 1
        assert api_response.jobs[0].match_status == "unmatched"
        assert api_response.jobs[0].match_score == 46
        assert state.get_job_counts() == {
            "ALL": 1, "P1": 0, "P2": 0, "P3": 1, "UNMATCHED": 1,
        }
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


@pytest.mark.parametrize("assessment_status", ["pending", "retrying", "failed"])
def test_dashboard_distinguishes_incomplete_assessments_from_unmatched(
    tmp_path, assessment_status
):
    repo = JobRepository(tmp_path / f"assessment-{assessment_status}.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    job = Job(
        source="telegram",
        source_job_id=assessment_status,
        title="Backend Developer",
        company="Example",
        location="Remote",
        description="Python role",
        url=f"https://t.me/jobs_alpha/{assessment_status}",
        match_result={
            "assessment_status": assessment_status,
            "match": None,
            "match_score": None,
            "priority": "P3",
            "ai_status": "failed" if assessment_status == "failed" else "skipped",
            "ai_score": None,
            "deterministic_score": 31,
        },
    )
    repo.upsert_job(job)

    try:
        state = web_state.initialize_web_state(Config(), repo)
        response = state.get_jobs()
        assert response["jobs"][0]["assessment_status"] == assessment_status
        assert response["jobs"][0]["match_status"] == assessment_status
        assert response["jobs"][0]["match_score"] is None
        assert response["jobs"][0]["deterministic_score"] == 31
        assert state.get_jobs(match_status="unmatched")["total"] == 0
        assert state.get_job_counts() == {
            "ALL": 1, "P1": 0, "P2": 0, "P3": 1, "UNMATCHED": 0,
        }
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


@pytest.mark.parametrize(
    ("raw_data", "expected_status"),
    [({"filter_status": "FILTERED"}, "not_required"), ({}, "unscored")],
)
def test_dashboard_does_not_infer_pending_without_an_explicit_assessment(
    tmp_path, raw_data, expected_status
):
    repo = JobRepository(tmp_path / f"assessment-fallback-{expected_status}.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    job = Job(
        source="telegram",
        source_job_id=expected_status,
        title="Backend Developer",
        company="Example",
        location="Remote",
        description="Python role",
        url="https://t.me/jobs_alpha/legacy",
        raw_data=raw_data,
    )
    repo.upsert_job(job)

    try:
        state = web_state.initialize_web_state(Config(), repo)
        displayed = state.get_jobs()["jobs"][0]
        assert displayed["assessment_status"] == expected_status
        assert displayed["match_status"] == (
            "unmatched" if expected_status == "not_required" else "unscored"
        )
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_yaml_validation_rejects_invalid_yaml():
    with pytest.raises(HTTPException) as error:
        routes._validate_yaml("filters.yaml", "roles: [unterminated")
    assert error.value.status_code == 422


def test_valid_yaml_is_atomically_persisted_and_reloaded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    filters_path = tmp_path / "filters.yaml"
    filters_path.write_text("roles: []\n", encoding="utf-8")
    state = web_state.initialize_web_state(Config(), JobRepository(tmp_path / "jobs.db"))
    monkeypatch.setattr(routes, "_state", lambda: state)
    content = "roles:\n  - Backend Developer\nlocations:\n  - Pune\n"

    response = asyncio.run(routes.save_config_file("filters.yaml", ConfigFileRequest(content=content)))

    assert response == {"success": True, "name": "filters.yaml"}
    assert filters_path.read_text(encoding="utf-8") == content
    assert state.config.filters.roles == ["Backend Developer"]
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_yaml_save_waits_for_running_worker_configuration_ack(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "filters.yaml").write_text("roles: []\n", encoding="utf-8")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "worker-config.db")
    )
    state.external_worker = True
    state.is_running = True
    commands = []

    async def send_worker_command(command, arguments=None, *, timeout=20.0):
        commands.append((command, arguments, timeout))
        return {"reloaded": True}

    state._send_worker_command = send_worker_command
    monkeypatch.setattr(routes, "_state", lambda: state)

    response = asyncio.run(routes.save_config_file(
        "filters.yaml", ConfigFileRequest(content="roles:\n  - Updated Role\n")
    ))

    assert response == {"success": True, "name": "filters.yaml"}
    assert commands == [
        ("RELOAD_CONFIGURATION", {"env_keys": []}, 45.0)
    ]
    assert state.config.filters.roles == ["Updated Role"]
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_config_save_reports_worker_reload_failure_without_leaking_error(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "filters.yaml").write_text("roles: []\n", encoding="utf-8")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "worker-config-failure.db")
    )
    state.external_worker = True

    async def fail_worker_command(command, arguments=None, *, timeout=20.0):
        raise TimeoutError("private-key-canary")

    state._send_worker_command = fail_worker_command
    monkeypatch.setattr(routes, "_state", lambda: state)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.save_config_file(
            "filters.yaml", ConfigFileRequest(content="roles:\n  - Updated Role\n")
        ))

    assert error.value.status_code == 503
    assert "private-key-canary" not in error.value.detail
    assert "worker did not apply" in error.value.detail
    assert "private-key-canary" not in repr(state.get_activity())
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_ai_web_settings_update_the_shared_configuration_without_returning_keys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hirevia.config._ensure_dotenv", lambda: None)
    for variable in (
        "AI_PRIMARY_PROVIDER", "AI_FALLBACK_PROVIDERS", "AI_TIMEOUT_SECONDS",
        "LLAMACPP_MODEL", "LLAMACPP_BASE_URL", "NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "masked-secret-CANARY")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "jobs.db")
    )
    monkeypatch.setattr(routes, "_state", lambda: state)

    result = asyncio.run(routes.update_ai_config(AISettingsRequest(
        primary="llamacpp",
        model="web-selected-model",
        base_url="http://127.0.0.1:8080/v1",
        timeout_seconds=60,
        p1_threshold=90,
        p2_threshold=70,
        p3_threshold=45,
        api_key="local-secret-CANARY",
    )))
    public_config = asyncio.run(routes.get_ai_config())

    assert result == {"success": True, "primary": "llamacpp", "model": "web-selected-model"}
    assert (tmp_path / ".env").read_text(encoding="utf-8").find("NVIDIA_API_KEY=masked-secret-CANARY") == -1
    assert state.config.ai.primary == "llamacpp"
    assert state.config.ai.providers["llamacpp"].model == "web-selected-model"
    assert state.config.priority_p1_min_score == 90
    assert state.config.priority_p2_min_score == 70
    assert state.config.priority_p3_min_score == 45
    assert state.config.ai.timeout_seconds == 60
    assert "LLAMACPP_API_KEY=local-secret-CANARY" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "masked-secret-CANARY" not in repr(public_config)
    assert "local-secret-CANARY" not in repr(public_config)
    assert "fallback" not in public_config
    assert public_config["matching"] == {
        "p1_threshold": 90, "p2_threshold": 70, "p3_threshold": 45
    }
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_ai_config_reload_sends_only_environment_key_names(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hirevia.config._ensure_dotenv", lambda: None)
    monkeypatch.setenv("NVIDIA_API_KEY", "api-key-command-canary")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "worker-ai-config.db")
    )
    state.external_worker = True
    commands = []

    async def send_worker_command(command, arguments=None, *, timeout=20.0):
        commands.append((command, arguments, timeout))
        return {"reloaded": True}

    state._send_worker_command = send_worker_command
    monkeypatch.setattr(routes, "_state", lambda: state)

    result = asyncio.run(routes.update_ai_config(AISettingsRequest(
        primary="llamacpp",
        model="worker-selected-model",
        base_url="http://127.0.0.1:8080/v1",
        timeout_seconds=60,
        p1_threshold=90,
        p2_threshold=70,
        p3_threshold=45,
        api_key="api-key-command-canary",
    )))

    command, arguments, timeout = commands[0]
    assert result["success"] is True
    assert command == "RELOAD_CONFIGURATION"
    assert set(arguments["env_keys"]) == {
        "AI_PRIMARY_PROVIDER", "LLAMACPP_MODEL", "AI_TIMEOUT_SECONDS",
        "MATCHING_P1_THRESHOLD", "MATCHING_P2_THRESHOLD",
        "MATCHING_P3_THRESHOLD", "LLAMACPP_BASE_URL", "LLAMACPP_API_KEY",
    }
    assert timeout == 45.0
    assert "api-key-command-canary" not in repr(arguments)
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_telegram_first_launch_without_session_blocks_monitor_start(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramAuthError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("TELEGRAM_SESSION_NAME", "web-state-test")
    web_state._web_state = None
    web_state.WebUIState._instance = None

    async def no_session():
        raise TelegramAuthError("missing test session")

    monkeypatch.setattr(telegram_source, "ensure_client", no_session)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "first-launch.db")
    )
    monkeypatch.setattr(routes, "_state", lambda: state)

    try:
        with pytest.raises(RuntimeError, match="Connect a Telegram account"):
            asyncio.run(state.trigger_scan())
        started = asyncio.run(routes.start_scanner())

        assert started.success is False
        assert "Connect a Telegram account" in started.message
        assert state.is_running is False
        assert state.scanner_task is None
        assert state.monitor_start_error == "Connect a Telegram account before starting monitoring"
    finally:
        state.worker_runtime.release_telegram_session()
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_monitor_starts_after_telegram_authentication_and_validation(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("TELEGRAM_SESSION_NAME", "web-state-test")
    (tmp_path / "telegram.yaml").write_text(
        "telegram:\n  channels:\n    - username: jobs_alpha\n", encoding="utf-8"
    )
    web_state._web_state = None
    web_state.WebUIState._instance = None

    class AccountClient:
        async def get_me(self):
            return SimpleNamespace(first_name="Hirevia", last_name=None, username="hirevia_user")

        async def get_entity(self, username):
            return SimpleNamespace(username=username)

        async def __call__(self, request):
            return SimpleNamespace()

    async def existing_session():
        return AccountClient()

    class FakeTelegramSource:
        last_collection_failed = False
        has_more_backfill = False

        def __init__(self):
            self.stopped = asyncio.Event()
            self.event_mode = False

        async def start_continuous_monitoring(self):
            return 1

        def enable_event_mode(self):
            self.event_mode = True

        def retry_uncommitted_event(self):
            return False

        async def stop_continuous_monitoring(self):
            self.stopped.set()

    source = FakeTelegramSource()

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            self.registry = SimpleNamespace(get_source=lambda name: source if name == "telegram" else None)
            self.scan_calls = 0

        async def run_scan(self):
            self.scan_calls += 1
            if self.scan_calls == 1:
                return ScanStats()
            await source.stopped.wait()
            return ScanStats()

    monkeypatch.setattr(telegram_source, "ensure_client", existing_session)
    monkeypatch.setattr(web_state, "Pipeline", FakePipeline)
    config = Config()
    state = web_state.initialize_web_state(
        config, JobRepository(tmp_path / "validated-monitor.db")
    )
    state.telegram_authenticated = True

    async def start_then_stop():
        started = await state.start_scanner()
        assert started is True
        assert state.is_running is True
        assert state.repo.get_source_state("system", "monitor_desired") == "running"
        assert source.event_mode is True
        await state.stop_scanner()
        assert state.repo.get_source_state("system", "monitor_desired") == "stopped"

    try:
        asyncio.run(start_then_stop())
        assert state.is_running is False
    finally:
        state.worker_runtime.release_telegram_session()
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_monitor_desired_state_defaults_stopped_and_recovers_only_when_requested(monkeypatch, tmp_path):
    config = Config()
    config.sources.sources["telegram"].enabled = False
    state = web_state.initialize_web_state(
        config, JobRepository(tmp_path / "monitor-recovery.db")
    )
    start_calls = []

    async def fake_start(persist_desired=True):
        start_calls.append(persist_desired)
        return True

    monkeypatch.setattr(state, "start_scanner", fake_start)

    async def recover():
        stopped = await state.recover_monitoring_for_startup()
        state.repo.set_source_state("system", "monitor_desired", "running")
        running = await state.recover_monitoring_for_startup()
        return stopped, running

    try:
        stopped, running = asyncio.run(recover())
        assert stopped is False
        assert running is True
        assert state.monitor_state == "STOPPED"
        assert start_calls == [False]
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


@pytest.mark.parametrize("monitor_status", ["RUNNING", "SCANNING", "RECONNECTING"])
def test_external_monitor_start_keeps_desired_state_for_active_worker_status(
    monkeypatch, tmp_path, monitor_status
):
    config = Config()
    config.sources.sources["telegram"].enabled = False
    state = web_state.initialize_web_state(
        config, JobRepository(tmp_path / f"monitor-start-{monitor_status}.db")
    )
    state.external_worker = True
    runtime = {
        "alive": True,
        "status": "RUNNING",
        "monitor_status": "STOPPED",
    }

    def sync_worker_status():
        state.worker_alive = runtime["alive"]
        state.monitor_state = runtime["monitor_status"]
        state.is_running = runtime["monitor_status"] in {
            "RUNNING", "SCANNING", "RECONNECTING"
        }
        state.is_scanning = runtime["monitor_status"] == "SCANNING"
        return runtime

    async def acknowledge_start(command, arguments=None, *, timeout=20.0):
        assert command == "START"
        runtime["monitor_status"] = monitor_status
        return {"running": True}

    monkeypatch.setattr(state, "_sync_external_worker_status", sync_worker_status)
    monkeypatch.setattr(state, "_send_worker_command", acknowledge_start)

    async def start_and_advance_past_timeout():
        loop = asyncio.get_running_loop()
        clock = [loop.time()]
        started_at = clock[0]
        monkeypatch.setattr(loop, "time", lambda: clock[0])

        async def advance_clock(delay):
            clock[0] += delay

        monkeypatch.setattr(web_state.asyncio, "sleep", advance_clock)
        started = await state.start_scanner()
        clock[0] += 13

        assert started is True
        assert runtime["monitor_status"] == monitor_status
        assert clock[0] - started_at > 12
        assert state.repo.get_source_state("system", "monitor_desired") == "running"

    try:
        asyncio.run(start_and_advance_past_timeout())
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_existing_telegram_session_is_restored_on_startup(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramCredentials

    monkeypatch.chdir(tmp_path)
    credentials = TelegramCredentials(
        api_id=12345, api_hash="private-api-hash", session_dir=str(tmp_path / "sessions")
    )
    monkeypatch.setattr("hirevia.sources.telegram_config.load_telegram_credentials", lambda: credentials)
    session_file = Path(f"{credentials.session_path}.session")
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b"old-authorized-session")
    web_state._web_state = None
    web_state.WebUIState._instance = None

    class OldClient:
        def is_connected(self):
            return True

        async def disconnect(self):
            self.closed = True

    old_client = OldClient()
    telegram_source._shared_client = old_client
    restored_client = OldClient()

    async def existing_session():
        return restored_client

    async def get_me():
        return SimpleNamespace(
            first_name="Hirevia", last_name="User", username="hirevia_user", phone="12025550123"
        )

    restored_client.get_me = get_me

    monkeypatch.setattr(telegram_source, "ensure_client", existing_session)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "existing-session.db")
    )

    try:
        asyncio.run(state.restore_telegram_auth_for_startup())
        status = asyncio.run(state.telegram_status())

        assert status["connected"] is True
        assert status["login_state"] == "connected"
        assert status["account"] == {"name": "Hirevia User", "username": "hirevia_user"}
        assert session_file.exists()
        assert old_client.closed is True
        assert telegram_source._shared_client is restored_client
    finally:
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_telegram_source_validation_reports_membership_access_and_disabled(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    monkeypatch.chdir(tmp_path)
    (tmp_path / "telegram.yaml").write_text(
        "telegram:\n  enabled: true\n  channels:\n"
        "    - username: jobs_alpha\n"
        "    - username: jobs_beta\n"
        "    - username: jobs_private\n"
        "    - username: jobs_off\n      enabled: false\n",
        encoding="utf-8",
    )
    web_state._web_state = None
    web_state.WebUIState._instance = None

    class TelegramTestClient:
        async def get_me(self):
            return SimpleNamespace(first_name="Hirevia", last_name=None, username="hirevia_user")

        async def get_entity(self, username):
            if username == "jobs_private":
                raise type("ChannelPrivateError", (Exception,), {})()
            return SimpleNamespace(username=username)

        async def __call__(self, request):
            if request.channel.username == "jobs_beta":
                raise type("UserNotParticipantError", (Exception,), {})()
            return SimpleNamespace()

    client = TelegramTestClient()

    async def use_client():
        return client

    monkeypatch.setattr(telegram_source, "ensure_client", use_client)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "channel-validation.db")
    )
    state.telegram_authenticated = True

    try:
        result = asyncio.run(state.validate_telegram_sources(force=True))
        sources = {item["username"]: item for item in result["sources"]}

        assert result["authenticated"] is True
        assert result["validation_complete"] is True
        assert sources["jobs_alpha"]["membership"] == "joined"
        assert sources["jobs_beta"]["membership"] == "not_joined"
        assert sources["jobs_beta"]["can_join"] is True
        assert sources["jobs_private"]["access"] == "inaccessible"
        assert sources["jobs_off"]["membership"] == "disabled"
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_expired_telegram_session_is_reported_after_web_auth(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramAuthError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr("hirevia.sources.telegram_config.load_dotenv", lambda *args, **kwargs: False)
    web_state._web_state = None
    web_state.WebUIState._instance = None

    async def expired_session():
        raise TelegramAuthError("test session is no longer authorized")

    monkeypatch.setattr(telegram_source, "ensure_client", expired_session)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "expired-session.db")
    )
    state.telegram_authenticated = True

    try:
        status = asyncio.run(state.telegram_status())

        assert status["connected"] is False
        assert status["login_state"] == "expired"
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def _make_fake_telegram_client(*, code_error=None, password_error=None):
    class FakeClient:
        def __init__(self):
            self.connected = False
            self.authorized = False
            self.sign_in_calls = []
            self.send_calls = []
            self.password_error = password_error

        async def connect(self):
            self.connected = True

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

        async def send_code_request(self, phone):
            self.send_calls.append(phone)
            return SimpleNamespace(phone_code_hash="mock-code-hash", timeout=0)

        async def resend_code(self, phone, phone_code_hash):
            self.send_calls.append((phone, phone_code_hash))
            return SimpleNamespace(phone_code_hash="new-mock-code-hash", timeout=0)

        async def sign_in(self, **kwargs):
            self.sign_in_calls.append(kwargs)
            if "code" in kwargs and code_error:
                raise type(code_error, (Exception,), {})()
            if "password" in kwargs and self.password_error:
                raise type(self.password_error, (Exception,), {})()
            self.authorized = True

        async def is_user_authorized(self):
            return self.authorized

        async def get_me(self):
            return SimpleNamespace(first_name="Hirevia", last_name="User", username="hirevia_user")

    return FakeClient()


def _configure_fake_telegram_login(monkeypatch, tmp_path, client):
    import telethon
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramCredentials

    monkeypatch.chdir(tmp_path)
    credentials = TelegramCredentials(
        api_id=12345, api_hash="private-api-hash", session_dir=str(tmp_path / "sessions")
    )
    monkeypatch.setattr("hirevia.sources.telegram_config.load_telegram_credentials", lambda: credentials)
    monkeypatch.setattr(telethon, "TelegramClient", lambda *args: client)

    async def use_client():
        return client

    monkeypatch.setattr(telegram_source, "ensure_client", use_client)
    telegram_source._shared_client = None
    web_state._web_state = None
    web_state.WebUIState._instance = None
    return credentials


def test_dashboard_reuses_authorized_session_without_requesting_otp(
    monkeypatch, tmp_path
):
    from hirevia.sources import telegram_source

    client = _make_fake_telegram_client()
    client.authorized = True
    credentials = _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    session_file = Path(f"{credentials.session_path}.session")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"authorized-session-preserved")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "authorized-session.db")
    )

    async def validated_sources(**_kwargs):
        return {
            "authenticated": True,
            "validation_complete": True,
            "sources": [],
            "account": {"name": "Hirevia User", "username": "hirevia_user"},
        }

    state.validate_telegram_sources = validated_sources

    try:
        result = asyncio.run(state.begin_telegram_phone_login("+12025550123"))

        assert result["success"] is True
        assert result["login_state"] == "connected"
        assert client.send_calls == []
        assert client.sign_in_calls == []
        assert state.telegram_authenticated is True
        assert session_file.read_bytes() == b"authorized-session-preserved"
    finally:
        asyncio.run(state._release_telegram_auth_session(resume_worker=False))
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_phone_otp_authentication_uses_hash_and_verifies_account(monkeypatch, tmp_path, caplog):
    client = _make_fake_telegram_client()
    credentials = _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "phone-login.db")
    )

    try:
        with caplog.at_level(logging.INFO, logger="hirevia.web.state"):
            requested = asyncio.run(state.begin_telegram_phone_login("+1 (202) 555-0123"))
            duplicate = asyncio.run(state.begin_telegram_phone_login("+12025550123"))
            verified = asyncio.run(state.verify_telegram_otp("123456"))
            status = asyncio.run(state.telegram_status())

        assert requested["success"] is True
        assert requested["phone_number"] == "+*******0123"
        assert duplicate["success"] is False
        assert client.send_calls == ["+12025550123"]
        assert client.sign_in_calls[0] == {
            "phone": "+12025550123",
            "code": "123456",
            "phone_code_hash": "mock-code-hash",
        }
        assert verified["success"] is True
        assert state.telegram_authenticated is True
        assert state.telegram_account == {"name": "Hirevia User", "username": "hirevia_user"}
        assert state.telegram_source_validation["validation_complete"] is True
        assert status["connected"] is True
        assert status["phone_number"] == "+*******0123"
        assert "123456" not in caplog.text
        assert "private-api-hash" not in caplog.text
        assert "mock-code-hash" not in str((requested, duplicate, verified, status))
        assert "private-api-hash" not in str((requested, duplicate, verified, status))
        assert credentials.session_path.parent.exists()
    finally:
        from hirevia.sources import telegram_source
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


@pytest.mark.parametrize(
    ("error_name", "expected_state"),
    [("PhoneCodeInvalidError", "code_invalid"), ("PhoneCodeExpiredError", "code_expired")],
)
def test_invalid_and_expired_otp_are_handled_safely(monkeypatch, tmp_path, error_name, expected_state):
    client = _make_fake_telegram_client(code_error=error_name)
    credentials = _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    session_file = Path(f"{credentials.session_path}.session")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"existing-session-must-survive")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / f"{expected_state}.db")
    )

    try:
        asyncio.run(state.begin_telegram_phone_login("+12025550123"))
        result = asyncio.run(state.verify_telegram_otp("000000"))

        assert result["success"] is False
        assert result["login_state"] == expected_state
        assert state.telegram_authenticated is False
        assert session_file.read_bytes() == b"existing-session-must-survive"
        if expected_state == "code_expired":
            assert state.telegram_phone_code_hash is None
            assert client.sign_in_calls
    finally:
        from hirevia.sources import telegram_source
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_otp_resend_obeys_cooldown_and_replaces_code_hash(monkeypatch, tmp_path):
    client = _make_fake_telegram_client()
    _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "resend.db")
    )

    try:
        asyncio.run(state.begin_telegram_phone_login("+12025550123"))
        early = asyncio.run(state.resend_telegram_otp())
        state.telegram_resend_after = 0
        resent = asyncio.run(state.resend_telegram_otp())

        assert early["success"] is False
        assert resent["success"] is True
        assert state.telegram_phone_code_hash == "new-mock-code-hash"
        assert client.send_calls[-1] == ("+12025550123", "mock-code-hash")
    finally:
        from hirevia.sources import telegram_source
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_two_factor_required_and_password_redacted(monkeypatch, tmp_path, caplog):
    client = _make_fake_telegram_client(password_error="PasswordHashInvalidError")
    original_sign_in = client.sign_in

    async def require_password(**kwargs):
        if "code" in kwargs:
            client.sign_in_calls.append(kwargs)
            raise type("SessionPasswordNeededError", (Exception,), {})()
        await original_sign_in(**kwargs)

    client.sign_in = require_password
    _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "two-factor.db")
    )
    password = "private-test-password"

    try:
        with caplog.at_level(logging.INFO, logger="hirevia.web.state"):
            asyncio.run(state.begin_telegram_phone_login("+12025550123"))
            required = asyncio.run(state.verify_telegram_otp("654321"))
            rejected = asyncio.run(state.verify_telegram_password(password))
            client.password_error = None
            accepted = asyncio.run(state.verify_telegram_password(password))

        assert required["login_state"] == "password_required"
        assert rejected["success"] is False
        assert "Incorrect" in rejected["message"]
        assert accepted["success"] is True
        assert state.telegram_authenticated is True
        assert password not in caplog.text
        assert "654321" not in caplog.text
        assert password not in str(required)
        assert password not in str(rejected)
        assert password not in str(accepted)
    finally:
        from hirevia.sources import telegram_source
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_logout_closes_client_clears_auth_and_removes_only_telegram_session(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    client = _make_fake_telegram_client()
    credentials = _configure_fake_telegram_login(monkeypatch, tmp_path, client)
    credentials.session_path.parent.mkdir(parents=True, exist_ok=True)
    session_file = Path(f"{credentials.session_path}.session")
    session_file.write_bytes(b"authenticated-session")
    unrelated_file = tmp_path / "keep-this.db"
    unrelated_file.write_bytes(b"application-data")
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "logout.db")
    )
    client.connected = True
    telegram_source._shared_client = client
    state.telegram_authenticated = True
    state.telegram_login_state = "connected"
    state.telegram_account = {"name": "Hirevia User", "username": "hirevia_user"}

    try:
        result = asyncio.run(state.logout_telegram())

        assert result["success"] is True
        assert client.connected is False
        assert telegram_source._shared_client is None
        assert not session_file.exists()
        assert unrelated_file.read_bytes() == b"application-data"
        assert state.telegram_authenticated is False
        assert state.telegram_login_state == "disconnected"
        assert state.telegram_account is None
    finally:
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_deleting_jobs_database_does_not_delete_telegram_session_file(tmp_path):
    from hirevia.sources.telegram_config import TelegramCredentials

    credentials = TelegramCredentials(
        api_id=12345, api_hash="private-api-hash", session_dir=str(tmp_path / "sessions")
    )
    session_file = credentials.session_path.with_suffix(".session")
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b"separate-session-data")
    database_path = tmp_path / "jobs.db"
    JobRepository(database_path)

    database_path.unlink()

    assert session_file.exists()


def test_service_startup_preserves_session_and_closes_clients(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramCredentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "private-api-hash")
    monkeypatch.setattr("hirevia.sources.telegram_config.load_dotenv", lambda *args, **kwargs: False)
    credentials = TelegramCredentials(
        api_id=12345, api_hash="private-api-hash", session_dir=str(tmp_path / "sessions")
    )
    session_file = Path(f"{credentials.session_path}.session")
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b"old-authorized-session")
    closed = []

    class OldClient:
        def is_connected(self):
            return True

        async def disconnect(self):
            closed.append(True)

    class RestoredClient(OldClient):
        async def get_me(self):
            return SimpleNamespace(
                first_name="Hirevia", last_name="User", username="hirevia_user", phone="12025550123"
            )

    restored_client = RestoredClient()

    async def restore_client():
        return restored_client

    telegram_source._shared_client = OldClient()
    monkeypatch.setattr(telegram_source, "ensure_client", restore_client)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.initialize_web_state(
        Config(), JobRepository(tmp_path / "startup-session.db")
    )
    app.state.auto_start_monitoring = False
    configure_web_admin(monkeypatch, tmp_path)

    try:
        with TestClient(app) as client:
            login_web_admin(client)
            status = client.get("/api/telegram/config").json()

        assert closed == [True, True]
        assert session_file.exists()
        assert status["connected"] is True
        assert status["login_state"] == "connected"
        assert state.is_running is False
    finally:
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_telegram_source_toggle_preserves_channel_settings_and_reloads(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "telegram.yaml"
    config_file.write_text(
        "telegram:\n  channels:\n    - username: dev_jobs\n      enabled: true\n      lookback_hours: 12\n",
        encoding="utf-8",
    )
    state = web_state.initialize_web_state(Config(), JobRepository(tmp_path / "jobs.db"))
    monkeypatch.setattr(routes, "_state", lambda: state)

    result = asyncio.run(routes.update_telegram_source(
        "dev_jobs", TelegramSourceUpdate(enabled=False)
    ))

    assert result == {"success": True, "username": "dev_jobs", "enabled": False}
    assert load_telegram_settings().channels[0].enabled is False
    assert load_telegram_settings().channels[0].lookback_hours == 12
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_telegram_source_membership_uses_account_client(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    requests = []

    class FakeClient:
        async def get_entity(self, username):
            return username

        async def __call__(self, request):
            requests.append(type(request).__name__)

    client = FakeClient()

    async def fake_ensure_client():
        return client

    monkeypatch.setattr(telegram_source, "ensure_client", fake_ensure_client)
    state = web_state.WebUIState(Config(), JobRepository(tmp_path / "membership.db"))
    validation_results = iter([
        {"authenticated": True, "validation_complete": True, "sources": [
            {"username": "dev_jobs", "enabled": True, "membership": "not_joined", "can_join": True},
        ]},
        {"authenticated": True, "validation_complete": True, "sources": [
            {"username": "dev_jobs", "enabled": True, "membership": "joined", "can_join": False},
        ]},
        {"authenticated": True, "validation_complete": True, "sources": [
            {"username": "dev_jobs", "enabled": True, "membership": "joined", "can_join": False},
        ]},
        {"authenticated": True, "validation_complete": True, "sources": [
            {"username": "dev_jobs", "enabled": True, "membership": "not_joined", "can_join": True},
        ]},
    ])

    async def validated_state(force=False):
        return next(validation_results)

    state.validate_telegram_sources = validated_state

    joined = asyncio.run(state.set_telegram_source_membership("dev_jobs", join=True))
    left = asyncio.run(state.set_telegram_source_membership("dev_jobs", join=False))

    assert joined["status"] == "joined"
    assert left["status"] == "left"
    assert requests == ["JoinChannelRequest", "LeaveChannelRequest"]


def test_inaccessible_telegram_source_is_not_joined(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    async def must_not_connect():
        pytest.fail("inaccessible channel must not receive a join request")

    monkeypatch.setattr(telegram_source, "ensure_client", must_not_connect)
    state = web_state.WebUIState(Config(), JobRepository(tmp_path / "inaccessible.db"))

    async def inaccessible(force=False):
        return {"authenticated": True, "validation_complete": True, "sources": [
            {"username": "jobs_private", "enabled": True, "membership": "inaccessible", "can_join": False},
        ]}

    state.validate_telegram_sources = inaccessible

    result = asyncio.run(state.set_telegram_source_membership("jobs_private", join=True))

    assert result["success"] is False
    assert result["status"] == "inaccessible"


def test_bulk_join_only_joins_validated_eligible_channels(monkeypatch, tmp_path):
    from hirevia.sources import telegram_source

    monkeypatch.chdir(tmp_path)
    (tmp_path / "telegram.yaml").write_text(
        "telegram:\n  channels:\n"
        "    - username: jobs_alpha\n"
        "    - username: jobs_private\n"
        "    - username: jobs_joined\n"
        "    - username: jobs_off\n      enabled: false\n",
        encoding="utf-8",
    )
    requests = []

    class FakeClient:
        async def get_entity(self, username):
            return SimpleNamespace(username=username)

        async def get_input_entity(self, entity):
            return entity

        async def __call__(self, request):
            requests.append(type(request).__name__)
            if type(request).__name__ == "GetDialogFiltersRequest":
                return []

    client = FakeClient()

    async def ensure_client():
        return client

    monkeypatch.setattr(telegram_source, "ensure_client", ensure_client)
    state = web_state.WebUIState(Config(), JobRepository(tmp_path / "bulk-join.db"))
    source_states = [
        {"username": "jobs_alpha", "enabled": True, "membership": "not_joined", "can_join": True},
        {"username": "jobs_private", "enabled": True, "membership": "inaccessible", "can_join": False},
        {"username": "jobs_joined", "enabled": True, "membership": "joined", "can_join": False},
        {"username": "jobs_off", "enabled": False, "membership": "disabled", "can_join": False},
    ]

    async def validation(force=False):
        return {"authenticated": True, "validation_complete": True, "sources": source_states}

    state.validate_telegram_sources = validation

    result = asyncio.run(state.join_telegram_sources())
    statuses = {source["username"]: source["status"] for source in result["sources"]}

    assert result["success"] is True
    assert statuses == {
        "jobs_alpha": "joined",
        "jobs_private": "inaccessible",
        "jobs_joined": "already_joined",
        "jobs_off": "disabled",
    }
    assert requests.count("JoinChannelRequest") == 1


def test_priority_telegram_notifier_routes_using_stored_verdict(monkeypatch):
    deliveries = []

    class FakeNotifier:
        def __init__(self, token, chat_id, **options):
            self.chat_id = chat_id

        async def send(self, job, decision):
            deliveries.append(self.chat_id)
            return True

        async def close(self):
            return None

    monkeypatch.setattr("hirevia.notifications.telegram.TelegramNotifier", FakeNotifier)
    notifier = PriorityTelegramNotifier(
        "bot-token-not-returned", {"P1": "p1-chat", "P2": "p2-chat", "P3": "p3-chat"}
    )
    job = Job(
        source="test", source_job_id="tier", title="Role", company="Example",
        location="Remote", description="Test", url="https://example.com/",
        match_result={"priority": "P2", "match_score": 70},
    )
    decision = AIResponse(relevant=True, priority=Priority.P1, match_score=95)

    assert asyncio.run(notifier.send(job, decision)) is True
    assert deliveries == ["p2-chat"]


def test_web_control_routes_call_the_injected_monitor_and_scan_methods(monkeypatch):
    calls = []

    class SharedState:
        is_running = False
        is_scanning = False

        async def start_scanner(self, **kwargs):
            calls.append("start")
            self.is_running = True
            return True

        async def stop_scanner(self):
            calls.append("stop")
            self.is_running = False
            return True

        async def trigger_scan(self, scan_24h=False):
            calls.append(("scan", scan_24h))
            return ScanStats()

        def get_status(self):
            return {
                "status": "RUNNING" if self.is_running else "STOPPED",
                "current_operation": "Idle",
                "last_scan": None,
                "next_scan": None,
                "scan_duration": None,
                "scan_interval_minutes": 30,
                "is_scanning": False,
            }

        def get_stats(self):
            return ScanStats().model_dump()

    shared_state = SharedState()
    monkeypatch.setattr(routes, "_state", lambda: shared_state)

    started = asyncio.run(routes.start_scanner())
    scanned = asyncio.run(routes.trigger_scan())
    stopped = asyncio.run(routes.stop_scanner())

    assert started.success and scanned.success and stopped.success
    assert calls == ["start", ("scan", True), "stop"]


def test_web_monitor_scan_and_job_actions_write_backend_logs(monkeypatch, tmp_path, caplog):
    class FakePipeline:
        def __init__(self, config, repo, matcher, notifier=None):
            pass

        async def run_scan(self):
            return ScanStats(jobs_found=1, jobs_new=1)

    monkeypatch.setattr(web_state, "Pipeline", FakePipeline)
    monkeypatch.setattr(web_state, "provider_status", lambda config: [])
    monkeypatch.setattr("hirevia.worker_runtime.load_telegram_credentials", lambda: None)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = Config()
    config.sources.sources["telegram"].enabled = False
    repo = JobRepository(tmp_path / "web-action-logs.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    job = Job(
        source="test", source_job_id="web-log-job", title="Backend Developer",
        company="Example", location="Remote", description="Role", url="https://example.com/job",
    )
    repo.upsert_job(job)
    state = web_state.initialize_web_state(config, repo)
    monkeypatch.setattr(routes, "_state", lambda: state)

    async def run_actions():
        started = await routes.start_scanner()
        scanned = await routes.trigger_scan()
        saved = await routes.save_job(job.id, SaveJobRequest())
        deleted = await routes.delete_job(job.id)
        stopped = await routes.stop_scanner()
        assert started.success and scanned.success and saved["success"]
        assert deleted["success"] and stopped.success

    try:
        with caplog.at_level(logging.INFO, logger="hirevia.web.state"):
            asyncio.run(run_actions())

        for message in (
            "Web action: Scanner started",
            "Web action: Manual scan triggered",
            "Web action: Scan completed",
            "Web action: Job saved",
            "Web action: Job permanently deleted",
            "Web action: Scanner stopped",
        ):
            assert message in caplog.text
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_log_file_endpoints_are_not_exposed():
    paths = {route.path for route in routes.router.routes}

    assert "/logs" not in paths
    assert "/logs/{filename}" not in paths


def test_delete_all_jobs_route_keeps_telegram_history(monkeypatch, tmp_path):
    state = web_state.initialize_web_state(Config(), JobRepository(tmp_path / "bulk-delete.db"))
    job = Job(
        source="telegram", source_job_id="bulk-delete-1", title="Role", company="Example",
        location="Remote", description="Test", url="https://t.me/jobs_alpha/1",
        raw_data={"source_channel": "jobs_alpha", "source_message_id": 1},
    )
    state.repo.upsert_job(job)
    state.repo.record_telegram_message("jobs_alpha", "1")
    state.repo.mark_telegram_message_processed("jobs_alpha", "1")
    monkeypatch.setattr(routes, "_state", lambda: state)

    result = asyncio.run(routes.delete_all_jobs())

    assert result == {"success": True, "deleted": 1}
    assert state.repo.get_job(job.id) is None
    assert state.repo.record_telegram_message("jobs_alpha", "1") is False
    web_state._web_state = None
    web_state.WebUIState._instance = None