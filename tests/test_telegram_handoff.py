"""Worker-owned Telegram session handoff regression tests."""

import asyncio
import os
import telethon
from types import SimpleNamespace
import pytest

from hirevia.config import Config
from hirevia.storage.state import JobRepository
from hirevia.web import state as web_state
from hirevia.worker import WorkerService
from hirevia.worker_runtime import WorkerRuntime
from hirevia.sources.telegram_config import TelegramCredentials
from hirevia.sources.telegram_session_lock import TelegramSessionLock


class FakeWorkerState:
    def __init__(self, repo):
        self.repo = repo
        self.config = Config()
        self.worker_runtime = WorkerRuntime(self)
        self.external_worker = False
        self.telegram_authenticated = True
        self.telegram_account = {"name": "Hirevia User", "username": "hirevia_user"}
        self.telegram_source_validation = {"authenticated": True, "validation_complete": True}
        self.telegram_validation_complete = True
        self.last_scan = None
        self.monitor_start_error = None
        self.closed_clients = 0
        self.activities = []

    async def restore_telegram_auth_for_startup(self):
        self.telegram_authenticated = True
        self.telegram_account = {"name": "Hirevia User", "username": "hirevia_user"}
        self.telegram_source_validation = {
            "authenticated": True,
            "validation_complete": True,
        }
        self.telegram_validation_complete = True

    async def _close_telegram_clients(self):
        self.closed_clients += 1

    async def run_cleanup_once(self):
        return {}

    def add_activity(self, message, level="info"):
        self.activities.append((message, level))


def configure_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setenv("TELEGRAM_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("TELEGRAM_SESSION_NAME", "worker-owner")
    return TelegramCredentials(
        api_id=12345,
        api_hash="test-api-hash",
        session_dir=str(tmp_path / "sessions"),
        session_name="worker-owner",
    )


def test_worker_owns_telegram_session_and_dashboard_lock_is_excluded(
    monkeypatch, tmp_path
):
    credentials = configure_credentials(monkeypatch, tmp_path)
    worker_lock = TelegramSessionLock(credentials.session_path)
    dashboard_lock = TelegramSessionLock(credentials.session_path)

    assert worker_lock.try_acquire() is True
    assert dashboard_lock.try_acquire() is False
    worker_lock.release()
    assert dashboard_lock.try_acquire() is True
    dashboard_lock.release()


def test_worker_releases_for_dashboard_then_reacquires_after_auth(tmp_path, monkeypatch):
    credentials = configure_credentials(monkeypatch, tmp_path)
    repo = JobRepository(tmp_path / "handoff.db")
    pid = os.getpid()
    assert repo.claim_worker_lease(pid) is True
    repo.set_source_state("system", "monitor_desired", "running")

    worker_state = FakeWorkerState(repo)
    service = WorkerService(worker_state)
    dashboard_state = web_state.WebUIState(
        config=Config(), repo=repo, ai_matcher=None
    )
    dashboard_state.external_worker = True

    async def send_worker_command(command):
        command_id = repo.enqueue_worker_command(command)
        await service._process_commands()
        response = repo.get_worker_command(command_id)
        if response["status"] == "ERROR":
            raise RuntimeError(response["error"])
        return response["result"]

    dashboard_state._send_worker_command = send_worker_command

    async def handoff():
        assert await worker_state.worker_runtime.acquire_telegram_session()
        assert await dashboard_state._acquire_telegram_auth_session(credentials)
        assert worker_state.worker_runtime.owns_telegram_session() is False
        assert dashboard_state.telegram_session_lock.acquired is True
        assert repo.get_source_state("system", "monitor_desired") == "running"
        assert repo.get_source_state("system", "telegram_auth_status") == "AUTHENTICATING"

        await dashboard_state._release_telegram_auth_session()

        assert dashboard_state.telegram_session_lock is None
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert repo.get_source_state("system", "telegram_auth_status") == "CONNECTED"
        assert repo.get_source_state("system", "monitor_desired") == "running"

    try:
        asyncio.run(handoff())
    finally:
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


class FakeDashboardTelegramClient:
    def __init__(self, *, otp_error=None, authorized=False):
        self.connected = False
        self.authorized = authorized
        self.otp_error = otp_error
        self.sign_in_calls = []
        self.logged_out = False

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="safe-test-code-hash", timeout=0)

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        if self.otp_error:
            raise type(self.otp_error, (Exception,), {})()
        self.authorized = True

    async def get_me(self):
        return SimpleNamespace(
            first_name="Hirevia",
            last_name="User",
            username="hirevia_user",
            phone="12025550123",
        )

    async def is_user_authorized(self):
        return self.authorized

    async def log_out(self):
        self.authorized = False
        self.logged_out = True


class BlockingDashboardTelegramClient(FakeDashboardTelegramClient):
    def __init__(self, blocked_phase):
        super().__init__()
        self.blocked_phase = blocked_phase
        self.blocked = asyncio.Event()
        self.disconnect_calls = 0

    async def connect(self):
        self.connected = True
        if self.blocked_phase == "connect":
            self.blocked.set()
            await asyncio.Future()

    async def send_code_request(self, phone):
        if self.blocked_phase == "send_code_request":
            self.blocked.set()
            await asyncio.Future()
        return await super().send_code_request(phone)

    async def disconnect(self):
        self.disconnect_calls += 1
        await super().disconnect()


def prepare_external_handoff(monkeypatch, tmp_path, *, desired="running"):
    credentials = configure_credentials(monkeypatch, tmp_path)
    repo = JobRepository(tmp_path / "external-handoff.db")
    worker_pid = os.getpid()
    assert repo.claim_worker_lease(worker_pid) is True
    repo.set_source_state("system", "monitor_desired", desired)
    worker_state = FakeWorkerState(repo)
    service = WorkerService(worker_state)
    dashboard_state = web_state.WebUIState(config=Config(), repo=repo)
    dashboard_state.external_worker = True

    async def validated_sources(**_kwargs):
        return _validated_sources()

    async def send_worker_command(command, arguments=None):
        command_id = repo.enqueue_worker_command(command, arguments)
        await service._process_commands()
        response = repo.get_worker_command(command_id)
        if response["status"] == "ERROR":
            raise RuntimeError(response["error"])
        return response["result"]

    dashboard_state._send_worker_command = send_worker_command
    dashboard_state.validate_telegram_sources = validated_sources
    return credentials, repo, worker_pid, worker_state, service, dashboard_state


def _validated_sources():
    return {
        "authenticated": True,
        "validation_complete": True,
        "sources": [],
        "account": {"name": "Hirevia User", "username": "hirevia_user"},
    }


def monitor_start_spy(worker_state):
    started = []
    runtime = worker_state.worker_runtime

    async def start_scanner(*, persist_desired=True):
        started.append(persist_desired)
        runtime.is_running = True
        runtime.monitor_state = "RUNNING"
        return True

    runtime.start_scanner = start_scanner
    return started


def test_external_phone_otp_handoff_reports_connected_only_after_worker_verify(
    monkeypatch, tmp_path
):
    client = FakeDashboardTelegramClient()
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )

    async def authenticate():
        assert await worker_state.worker_runtime.acquire_telegram_session()
        requested = await dashboard_state.begin_telegram_phone_login("+12025550123")
        assert requested["success"] is True
        assert worker_state.worker_runtime.owns_telegram_session() is False
        assert dashboard_state.telegram_session_lock.acquired is True
        verified = await dashboard_state.verify_telegram_otp("123456")
        return requested, verified

    try:
        requested, verified = asyncio.run(authenticate())

        assert requested["login_state"] == "code_sent"
        assert verified["success"] is True
        assert verified["login_state"] == "connected"
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert dashboard_state.telegram_session_lock is None
        assert repo.get_source_state("system", "telegram_auth_status") == "CONNECTED"
        assert repo.get_source_state("system", "monitor_desired") == "running"
        assert client.sign_in_calls[0]["code"] == "123456"
        status = asyncio.run(dashboard_state.telegram_status())
        assert status["connected"] is True
        assert status["auth_status"] == "CONNECTED"
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_external_two_factor_auth_uses_same_serialized_handoff(monkeypatch, tmp_path):
    class TwoFactorClient(FakeDashboardTelegramClient):
        async def sign_in(self, **kwargs):
            self.sign_in_calls.append(kwargs)
            if "code" in kwargs:
                raise type("SessionPasswordNeededError", (Exception,), {})()
            self.authorized = True

    client = TwoFactorClient()
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )

    async def authenticate():
        await worker_state.worker_runtime.acquire_telegram_session()
        await dashboard_state.begin_telegram_phone_login("+12025550123")
        required = await dashboard_state.verify_telegram_otp("123456")
        accepted = await dashboard_state.verify_telegram_password("private-password")
        return required, accepted

    try:
        required, accepted = asyncio.run(authenticate())
        assert required["login_state"] == "password_required"
        assert accepted["success"] is True
        assert accepted["login_state"] == "connected"
        assert "private-password" not in str((required, accepted))
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert repo.get_source_state("system", "telegram_auth_status") == "CONNECTED"
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_expired_session_status_is_auth_required_and_transient_is_reconnecting(
    monkeypatch, tmp_path
):
    configure_credentials(monkeypatch, tmp_path)
    repo = JobRepository(tmp_path / "auth-status.db")
    worker_state = FakeWorkerState(repo)
    worker_state.telegram_authenticated = False
    worker_state.telegram_login_state = "expired"
    service = WorkerService(worker_state)

    class CompletedTask:
        def __init__(self, exception=None):
            self.error = exception

        def cancelled(self):
            return False

        def exception(self):
            return self.error

    service._auth_restore_finished(CompletedTask())
    assert repo.get_source_state("system", "telegram_auth_status") == "AUTH_REQUIRED"

    worker_state.telegram_login_state = "reconnecting"
    service._auth_restore_finished(CompletedTask())
    assert repo.get_source_state("system", "telegram_auth_status") == "RECONNECTING"


def test_simultaneous_external_phone_requests_are_serialized(monkeypatch, tmp_path):
    client = FakeDashboardTelegramClient()
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )

    async def start_requests():
        await worker_state.worker_runtime.acquire_telegram_session()
        return await asyncio.gather(
            dashboard_state.begin_telegram_phone_login("+12025550123"),
            dashboard_state.begin_telegram_phone_login("+12025550123"),
        )

    try:
        first, second = asyncio.run(start_requests())
        assert first["success"] is True
        assert second["success"] is False
        assert dashboard_state.telegram_login_client is client
        assert dashboard_state.telegram_session_lock.acquired is True
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        if dashboard_state.telegram_session_lock is not None:
            asyncio.run(dashboard_state._release_telegram_auth_session())
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


@pytest.mark.parametrize("phase", ["connect", "send_code_request"])
@pytest.mark.parametrize("desired", ["running", "stopped"])
def test_external_phone_login_cancellation_releases_and_restores_worker(
    monkeypatch, tmp_path, phase, desired
):
    client = BlockingDashboardTelegramClient(phase)
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, service, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path, desired=desired
    )
    starts = monitor_start_spy(worker_state)

    async def cancel_login():
        assert await worker_state.worker_runtime.acquire_telegram_session()
        request = asyncio.create_task(
            dashboard_state.begin_telegram_phone_login("+12025550123")
        )
        await client.blocked.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert dashboard_state.telegram_session_lock is None
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert client.disconnect_calls == 1
        assert repo.get_source_state("system", "monitor_desired") == desired
        assert repo.get_source_state("system", "telegram_auth_status") == "CONNECTED"
        await service._reconcile_desired_state()
        assert starts == ([False] if desired == "running" else [])
        assert worker_state.worker_runtime.is_running is (desired == "running")

    try:
        asyncio.run(cancel_login())
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.is_running = False
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_cancelled_login_does_not_report_connected_without_worker_auth(
    monkeypatch, tmp_path
):
    client = BlockingDashboardTelegramClient("connect")
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )

    async def restore_requires_auth():
        worker_state.telegram_authenticated = False
        worker_state.telegram_account = None

    worker_state.restore_telegram_auth_for_startup = restore_requires_auth

    async def cancel_login():
        assert await worker_state.worker_runtime.acquire_telegram_session()
        request = asyncio.create_task(
            dashboard_state.begin_telegram_phone_login("+12025550123")
        )
        await client.blocked.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert repo.get_source_state("system", "telegram_auth_status") == "AUTH_REQUIRED"
        assert dashboard_state.telegram_authenticated is False
        assert dashboard_state.telegram_login_state == "auth_required"
        assert dashboard_state.telegram_session_lock is None
        assert worker_state.worker_runtime.owns_telegram_session() is True

    try:
        asyncio.run(cancel_login())
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


@pytest.mark.parametrize("desired", ["running", "stopped"])
def test_authentication_timeout_cancels_connect_and_releases_ownership(
    monkeypatch, tmp_path, desired
):
    client = BlockingDashboardTelegramClient("connect")
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, service, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path, desired=desired
    )
    starts = monitor_start_spy(worker_state)
    expire = asyncio.Event()

    async def wait_for_expiry(_seconds):
        await expire.wait()

    monkeypatch.setattr(web_state.asyncio, "sleep", wait_for_expiry)

    async def expire_login():
        assert await worker_state.worker_runtime.acquire_telegram_session()
        request = asyncio.create_task(
            dashboard_state.begin_telegram_phone_login("+12025550123")
        )
        await client.blocked.wait()
        timeout_task = dashboard_state.telegram_auth_timeout_task
        expire.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await timeout_task

        assert dashboard_state.telegram_login_client is None
        assert dashboard_state.telegram_login_in_progress is False
        assert client.sign_in_calls == []
        assert client.disconnect_calls == 1
        assert dashboard_state.telegram_session_lock is None
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert repo.get_source_state("system", "monitor_desired") == desired
        await service._reconcile_desired_state()
        assert starts == ([False] if desired == "running" else [])

    try:
        asyncio.run(expire_login())
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.is_running = False
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_external_auth_does_not_restart_a_previously_stopped_monitor(
    monkeypatch, tmp_path
):
    client = FakeDashboardTelegramClient()
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path, desired="stopped"
    )

    async def authenticate():
        await worker_state.worker_runtime.acquire_telegram_session()
        await dashboard_state.begin_telegram_phone_login("+12025550123")
        return await dashboard_state.verify_telegram_otp("123456")

    try:
        result = asyncio.run(authenticate())
        assert result["success"] is True
        assert repo.get_source_state("system", "monitor_desired") == "stopped"
        assert worker_state.worker_runtime.is_running is False
        assert repo.get_source_state("system", "telegram_auth_status") == "CONNECTED"
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_external_invalid_otp_preserves_session_and_keeps_worker_paused(
    monkeypatch, tmp_path
):
    client = FakeDashboardTelegramClient(otp_error="PhoneCodeInvalidError")
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )
    session_file = credentials.session_path.with_suffix(".session")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"existing-session")

    async def reject_code():
        await worker_state.worker_runtime.acquire_telegram_session()
        await dashboard_state.begin_telegram_phone_login("+12025550123")
        return await dashboard_state.verify_telegram_otp("000000")

    try:
        result = asyncio.run(reject_code())
        assert result["success"] is False
        assert result["login_state"] == "code_invalid"
        assert dashboard_state.telegram_session_lock.acquired is True
        assert worker_state.worker_runtime.owns_telegram_session() is False
        assert session_file.read_bytes() == b"existing-session"
        asyncio.run(dashboard_state._release_telegram_auth_session())
    finally:
        dashboard_state._cancel_telegram_auth_timeout()
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None


def test_external_logout_uses_lock_and_worker_reports_disconnected(
    monkeypatch, tmp_path
):
    client = FakeDashboardTelegramClient(authorized=True)
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args: client)
    credentials, repo, pid, worker_state, _, dashboard_state = prepare_external_handoff(
        monkeypatch, tmp_path
    )
    session_file = credentials.session_path.with_suffix(".session")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"authorized-session")

    try:
        assert asyncio.run(worker_state.worker_runtime.acquire_telegram_session())
        result = asyncio.run(dashboard_state.logout_telegram())

        assert result["success"] is True
        assert client.logged_out is True
        assert not session_file.exists()
        assert worker_state.worker_runtime.owns_telegram_session() is True
        assert repo.get_source_state("system", "monitor_desired") == "stopped"
        assert repo.get_source_state("system", "telegram_auth_status") == "DISCONNECTED"
    finally:
        worker_state.worker_runtime.release_telegram_session()
        repo.release_worker_lease(pid)
        web_state.WebUIState._instance = None
        web_state._web_state = None
