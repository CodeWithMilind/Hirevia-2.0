"""Process-level tests for the independent worker foundation."""

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from hirevia.config import Config
from hirevia.core.models import ScanStats
from hirevia.core.models import Job
from hirevia.storage.state import JobRepository
from hirevia.web import state as web_state
from hirevia.worker import WorkerService
from hirevia.worker_process import (
    WorkerProcessController,
    WorkerProcessError,
    get_process_executable_path,
)


class FakeChildProcess:
    def __init__(self, pid, *, exits_on_wait=False):
        self.pid = pid
        self.returncode = None
        self.exits_on_wait = exits_on_wait
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None and self.exits_on_wait:
            self.returncode = 2
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake worker", timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


def test_process_executable_lookup_identifies_current_python():
    executable = get_process_executable_path(os.getpid())

    assert executable is not None
    assert Path(executable).name.casefold().startswith("python")


def test_posix_pid_liveness_treats_zombie_as_exited():
    if os.name == "nt" or not Path("/proc").is_dir():
        return

    child_pid = os.fork()
    if child_pid == 0:
        os._exit(0)
    try:
        os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOWAIT)
        assert JobRepository._posix_pid_is_alive(child_pid) is False
    finally:
        os.waitpid(child_pid, 0)


def test_repository_connections_close_and_wal_checkpoint_cleanly(tmp_path):
    repo = JobRepository(tmp_path / "wal.db")

    with repo._get_conn() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        retained_connection = connection

    try:
        retained_connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("Repository connection remained open after context exit")

    checkpoint = sqlite3.connect(repo.db_path, timeout=5)
    try:
        result = checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result[0] == 0
        assert checkpoint.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        checkpoint.close()


def test_concurrent_controllers_accept_one_verified_lease_owner():
    class RacingRepository:
        def __init__(self):
            self.reads = 0
            self.runtime = {
                "pid": None,
                "status": "STOPPED",
                "alive": False,
            }

        def get_worker_runtime(self):
            self.reads += 1
            if self.reads >= 4:
                self.runtime = {
                    "pid": 111,
                    "status": "RUNNING",
                    "alive": True,
                    "monitor_status": "STOPPED",
                    "heartbeat_at": "heartbeat",
                }
            return dict(self.runtime)

    async def start_both():
        repo = RacingRepository()
        owner_child = FakeChildProcess(111)
        competing_child = FakeChildProcess(222, exits_on_wait=True)
        controllers = [
            WorkerProcessController(repo, startup_timeout=2),
            WorkerProcessController(repo, startup_timeout=2),
        ]
        controllers[0]._spawn_worker = lambda: setattr(
            controllers[0], "_spawned_process", owner_child
        )
        controllers[1]._spawn_worker = lambda: setattr(
            controllers[1], "_spawned_process", competing_child
        )

        results = await asyncio.gather(
            *(controller.ensure_running() for controller in controllers)
        )

        assert [result["pid"] for result in results] == [111, 111]
        assert [result["status"] for result in results] == ["RUNNING", "RUNNING"]
        assert results[0]["already_running"] is False
        assert results[1]["already_running"] is True
        assert competing_child.poll() == 2
        assert competing_child.terminate_calls == 0
        assert competing_child.kill_calls == 0
        assert owner_child.poll() is None
        assert owner_child.terminate_calls == 0
        assert owner_child.kill_calls == 0

    asyncio.run(start_both())


def test_shutdown_does_not_kill_foreign_lease_owner():
    class ForeignLeaseRepository:
        def get_worker_runtime(self):
            return {"pid": 111, "status": "RUNNING", "alive": True}

    child = FakeChildProcess(222, exits_on_wait=True)
    controller = WorkerProcessController(ForeignLeaseRepository())
    controller._spawned_process = child

    result = asyncio.run(controller.shutdown_worker(timeout=1))

    assert result["stopped"] is False
    assert result["worker_pid"] == 111
    assert child.poll() == 2
    assert child.terminate_calls == 0
    assert child.kill_calls == 0


def test_shutdown_with_missing_lease_still_reaps_live_tracked_child():
    class MissingLeaseRepository:
        def get_worker_runtime(self):
            return {"pid": None, "status": "STOPPED", "alive": False}

    child = FakeChildProcess(333)
    controller = WorkerProcessController(MissingLeaseRepository())
    controller._spawned_process = child

    try:
        asyncio.run(controller.shutdown_worker(timeout=0.01, force_timeout=0.1))
    except WorkerProcessError as error:
        assert "terminated and reaped" in str(error)
    else:
        raise AssertionError("An unleased live child was reported as already stopped")

    assert child.wait_calls >= 1
    assert child.terminate_calls == 1
    assert child.kill_calls == 0
    assert child.poll() == -15


def test_shutdown_kills_only_after_terminate_times_out():
    class StubbornChild(FakeChildProcess):
        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

    class MissingLeaseRepository:
        def get_worker_runtime(self):
            return {"pid": None, "status": "STOPPED", "alive": False}

    child = StubbornChild(444)
    controller = WorkerProcessController(MissingLeaseRepository())
    controller._spawned_process = child

    try:
        asyncio.run(controller.shutdown_worker(timeout=0.01, force_timeout=0.01))
    except WorkerProcessError:
        pass
    else:
        raise AssertionError("An unleased child without a shutdown command was accepted")

    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert child.poll() == -9


def test_reload_command_timeout_is_reported_not_completed():
    class PendingCommandRepository:
        def __init__(self):
            self.command = None

        def get_worker_runtime(self):
            return {"pid": 123, "status": "RUNNING", "alive": True}

        def enqueue_worker_command(self, command, arguments=None):
            self.command = (command, arguments)
            return "pending-reload"

        def get_worker_command(self, command_id):
            return {"status": "PENDING", "result": None, "error": None}

    repo = PendingCommandRepository()
    controller = WorkerProcessController(repo, spawn_enabled=False)

    try:
        asyncio.run(controller.send_command(
            "RELOAD_CONFIGURATION", {"env_keys": ["LLAMACPP_MODEL"]}, timeout=0.01
        ))
    except WorkerProcessError as error:
        assert "did not complete RELOAD_CONFIGURATION" in str(error)
    else:
        raise AssertionError("A pending reload was reported as successful")

    assert repo.command == (
        "RELOAD_CONFIGURATION", {"env_keys": ["LLAMACPP_MODEL"]}
    )


def test_systemd_mode_controller_never_spawns_missing_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("HIREVIA_WORKER_MODE", "systemd")
    monkeypatch.delenv("HIREVIA_WORKER_MANAGED", raising=False)

    class MissingWorkerRepository:
        db_path = tmp_path / "managed.db"

        def get_worker_runtime(self):
            return {"pid": None, "status": "STOPPED", "alive": False}

    spawned = []
    controller = WorkerProcessController(
        MissingWorkerRepository(),
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        startup_timeout=0.01,
    )

    try:
        asyncio.run(controller.ensure_running(spawn_if_missing=True))
    except WorkerProcessError:
        pass
    else:
        raise AssertionError("systemd mode reported an absent worker as running")


def test_managed_worker_environment_never_spawns_local_worker(monkeypatch, tmp_path):
    monkeypatch.delenv("HIREVIA_WORKER_MODE", raising=False)
    monkeypatch.setenv("HIREVIA_WORKER_MANAGED", "1")

    class MissingWorkerRepository:
        db_path = tmp_path / "managed-env.db"

        def get_worker_runtime(self):
            return {"pid": None, "status": "STOPPED", "alive": False}

    spawned = []
    controller = WorkerProcessController(
        MissingWorkerRepository(),
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    assert controller.spawn_enabled is False
    assert spawned == []


def configure_worker_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("HIREVIA_WORKER_MANAGED", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("HIREVIA_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setenv("HIREVIA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TELEGRAM_SESSION_DIR", str(tmp_path / "sessions"))


async def stop_test_children(controllers):
    for controller in controllers:
        process = controller._spawned_process
        if process is None or process.poll() is not None:
            continue
        try:
            await controller.shutdown_worker(timeout=5, force_timeout=2)
        except Exception:
            process.kill()
            await asyncio.to_thread(process.wait, 10)


def test_independent_child_start_command_and_graceful_shutdown(
    monkeypatch, tmp_path
):
    configure_worker_environment(monkeypatch, tmp_path)
    database_path = tmp_path / "worker.db"
    repo = JobRepository(database_path)
    session_directory = tmp_path / "sessions"
    session_directory.mkdir()
    session_file = session_directory / "user.session"
    session_file.write_bytes(b"preserve this session")
    job = Job(
        source="telegram",
        source_job_id="queued-1",
        title="Python Developer",
        company="Acme",
        location="Remote",
        description="Python role",
        url="https://t.me/example/123",
        raw_data={"source_channel": "example", "source_message_id": 123},
    )
    assert repo.enqueue_telegram_job(job)

    controllers = [
        WorkerProcessController(repo, startup_timeout=30),
        WorkerProcessController(repo, startup_timeout=30),
    ]

    async def exercise_processes():
        try:
            results = await asyncio.gather(
                *(controller.ensure_running() for controller in controllers),
                return_exceptions=True,
            )
            assert all(isinstance(result, dict) for result in results), results
            runtime = repo.get_worker_runtime()
            assert runtime["alive"] is True
            assert runtime["status"] == "RUNNING"
            assert runtime["pid"] in {
                controller._spawned_process.pid for controller in controllers
            }
            assert runtime["started_at"]
            assert runtime["heartbeat_at"]

            owner = next(
                controller
                for controller in controllers
                if controller._spawned_process.pid == runtime["pid"]
            )
            loser_processes = [
                controller._spawned_process
                for controller in controllers
                if controller is not owner
            ]
            assert all(process.poll() == 2 for process in loser_processes)

            command_result = await owner.send_command(
                "STATUS", {"fields": ["pid", "status"]}
            )
            assert command_result == {"pid": runtime["pid"], "status": "RUNNING"}

            prior_heartbeat = runtime["heartbeat_at"]
            await asyncio.sleep(1.2)
            assert repo.get_worker_runtime()["heartbeat_at"] != prior_heartbeat

            stopper = WorkerProcessController(repo, spawn_enabled=False)
            stopped = await stopper.shutdown_worker(timeout=20)
            assert stopped == {"stopped": True, "already_stopped": False}
            assert await asyncio.to_thread(owner._spawned_process.wait, 5) == 0
        finally:
            await stop_test_children(controllers)

    asyncio.run(exercise_processes())

    assert all(controller._spawned_process.poll() is not None for controller in controllers)
    runtime = repo.get_worker_runtime()
    assert runtime["alive"] is False
    assert runtime["status"] == "STOPPED"
    assert runtime["pid"] is None
    assert repo.get_source_state("system", "monitor_desired", "stopped") == "stopped"
    assert repo.get_telegram_queue_status()["queued"] == 1
    assert session_file.read_bytes() == b"preserve this session"


def test_worker_crash_releases_stale_lease_and_can_restart(monkeypatch, tmp_path):
    configure_worker_environment(monkeypatch, tmp_path)
    repo = JobRepository(tmp_path / "worker.db")
    controller = WorkerProcessController(repo, startup_timeout=30)
    stop_supervisor = asyncio.Event()
    supervisor_task = None

    async def restart_after_crash():
        nonlocal supervisor_task
        try:
            supervisor_task = asyncio.create_task(
                controller.supervise(stop_supervisor, retry_seconds=0.1)
            )
            first_runtime = None
            for _ in range(300):
                first_runtime = repo.get_worker_runtime()
                if first_runtime.get("alive") and first_runtime.get("status") == "RUNNING":
                    break
                await asyncio.sleep(0.1)
            assert first_runtime["alive"] is True
            first_process = controller._spawned_process
            first_pid = first_runtime["pid"]

            first_process.kill()
            await asyncio.to_thread(first_process.wait, 10)
            restarted = None
            for _ in range(300):
                restarted = repo.get_worker_runtime()
                if (
                    restarted.get("alive")
                    and restarted.get("status") == "RUNNING"
                    and restarted.get("pid") != first_pid
                ):
                    break
                await asyncio.sleep(0.1)
            assert restarted["alive"] is True
            assert restarted["pid"] == controller._spawned_process.pid
            assert restarted["status"] == "RUNNING"
            stop_supervisor.set()
            await asyncio.wait_for(supervisor_task, timeout=5)
            assert repo.get_worker_runtime()["alive"] is True
            assert controller._spawned_process.poll() is None
            await controller.shutdown_worker(timeout=20)
        finally:
            stop_supervisor.set()
            if supervisor_task is not None and not supervisor_task.done():
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)
            await stop_test_children([controller])

    asyncio.run(restart_after_crash())
    assert repo.get_worker_runtime()["status"] == "STOPPED"


def test_start_and_stop_commands_are_acknowledged_during_in_progress_scan(monkeypatch, tmp_path):
    configure_worker_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_SESSION_NAME", raising=False)
    config = Config()
    config.sources.sources["telegram"].enabled = False
    repo = JobRepository(tmp_path / "stop-during-scan.db")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=config, repo=repo)
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    scan_cancelled = asyncio.Event()

    class BlockingPipeline:
        registry = SimpleNamespace(get_source=lambda _name: None)

        async def run_scan(self):
            scan_started.set()
            try:
                await release_scan.wait()
                return ScanStats()
            finally:
                scan_cancelled.set()

    state.worker_runtime.pipeline_factory = lambda *_args: BlockingPipeline()
    service = WorkerService(state, poll_seconds=0.01)

    async def stop_during_scan():
        try:
            assert repo.claim_worker_lease(service.pid)
            start_id = repo.enqueue_worker_command("START")
            await service._process_commands()
            start_command = repo.get_worker_command(start_id)
            assert start_command["status"] == "COMPLETED"
            assert start_command["result"] == {"running": True}

            await asyncio.wait_for(scan_started.wait(), timeout=1)
            assert state.worker_runtime.is_scanning
            assert repo.get_worker_runtime()["monitor_status"] == "SCANNING"
            job = Job(
                source="telegram",
                source_job_id="interrupted-1",
                title="Python Developer",
                company="Example",
                location="Remote",
                description="Test-only queued item",
                url="https://t.me/example/1",
            )
            assert repo.enqueue_telegram_job(job)
            assert len(repo.claim_telegram_jobs(limit=1)) == 1

            scan_id = repo.enqueue_worker_command("SCAN_24H")
            repo.set_source_state("system", "monitor_desired", "stopped")
            stop_id = repo.enqueue_worker_command("STOP")
            await asyncio.wait_for(service._process_commands(), timeout=1)

            stop_command = repo.get_worker_command(stop_id)
            assert stop_command["status"] == "COMPLETED"
            assert stop_command["result"] == {"stopped": True}
            assert repo.get_worker_command(scan_id)["status"] == "PENDING"
            assert scan_cancelled.is_set()
            assert state.worker_runtime.is_running is False
            assert state.worker_runtime.is_scanning is False
            assert state.worker_runtime.monitor_state == "STOPPED"
            assert repo.get_source_state("system", "monitor_desired") == "stopped"
            assert repo.get_worker_runtime()["monitor_status"] == "STOPPED"
            with repo._get_conn() as connection:
                queue_status = connection.execute(
                    "SELECT status FROM processing_queue"
                ).fetchone()[0]
            assert queue_status == "QUEUED"
        finally:
            release_scan.set()
            if state.worker_runtime.scanner_task is not None:
                await state.worker_runtime.stop_scanner(persist_desired=False)
            repo.release_worker_lease(service.pid)
            web_state._web_state = None
            web_state.WebUIState._instance = None

    asyncio.run(stop_during_scan())


def test_graceful_shutdown_interrupts_in_progress_scan(tmp_path):
    repo = JobRepository(tmp_path / "drain.db")
    repo.set_source_state("system", "monitor_desired", "running")
    shutdown_id = repo.enqueue_worker_command("SHUTDOWN")

    class ActiveScanState:
        def __init__(self):
            self.repo = repo
            self.is_running = True
            self.is_scanning = True
            self.monitor_state = "RUNNING"
            self.telegram_authenticated = True
            self.telegram_account = {"name": "Test"}
            self.telegram_source_validation = {"validation_complete": True}
            self.last_scan = None
            self.stop_calls = 0
            self.close_calls = 0

        async def restore_telegram_auth_for_startup(self):
            return None

        async def run_cleanup_once(self):
            return {}

        async def stop_scanner(self, persist_desired=True):
            self.stop_calls += 1
            self.is_running = False
            self.is_scanning = False
            self.monitor_state = "STOPPED"
            return True

        async def _close_telegram_clients(self):
            self.close_calls += 1

    state = ActiveScanState()
    service = WorkerService(state, poll_seconds=0.01)

    async def stop_worker():
        task = asyncio.create_task(service.run())
        for _ in range(200):
            command = repo.get_worker_command(shutdown_id)
            if command and command["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.01)
        assert await asyncio.wait_for(task, timeout=5) == 0
        assert repo.get_worker_command(shutdown_id)["status"] == "COMPLETED"
        assert state.stop_calls == 1
        assert state.is_scanning is False

    asyncio.run(stop_worker())

    assert state.close_calls == 1
    assert repo.get_worker_runtime()["status"] == "STOPPED"


def test_worker_restart_keeps_persisted_stopped_state(monkeypatch, tmp_path):
    configure_worker_environment(monkeypatch, tmp_path)
    config = Config()
    config.sources.sources["telegram"].enabled = False
    repo = JobRepository(tmp_path / "restart-stopped.db")
    repo.set_source_state("system", "monitor_desired", "stopped")
    stale_pid = 2_000_000_000
    assert repo.claim_worker_lease(stale_pid)
    repo.release_worker_lease(stale_pid, "STOPPED")

    web_state._web_state = None
    web_state.WebUIState._instance = None
    state = web_state.WebUIState(config=config, repo=repo)
    pipeline_calls = []

    def unexpected_pipeline(*_args):
        pipeline_calls.append(True)
        raise AssertionError("Stopped worker restart must not create a scan pipeline")

    state.worker_runtime.pipeline_factory = unexpected_pipeline
    service = WorkerService(state, poll_seconds=0.01)

    async def restart_without_monitoring():
        task = asyncio.create_task(service.run())
        for _ in range(100):
            runtime = repo.get_worker_runtime()
            if runtime["alive"] and runtime["status"] == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert runtime["alive"] is True
        assert runtime["monitor_status"] == "STOPPED"
        assert runtime["desired_monitoring"] == "stopped"
        assert state.worker_runtime.is_running is False
        assert pipeline_calls == []

        shutdown_id = repo.enqueue_worker_command("SHUTDOWN")
        assert await asyncio.wait_for(task, timeout=2) == 0
        assert repo.get_worker_command(shutdown_id)["status"] == "COMPLETED"

    try:
        asyncio.run(restart_without_monitoring())
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None
    runtime = repo.get_worker_runtime()
    assert runtime["status"] == "STOPPED"
    assert runtime["monitor_status"] == "STOPPED"
    assert runtime["desired_monitoring"] == "stopped"
    assert pipeline_calls == []


def test_worker_reload_applies_latest_yaml_while_stopped(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hirevia.web.state.provider_status", lambda config: [])
    for key in ("AI_PRIMARY_PROVIDER", "LLAMACPP_MODEL", "LLAMACPP_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  telegram:\n    enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "filters.yaml").write_text("roles: []\n", encoding="utf-8")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    repo = JobRepository(tmp_path / "config-reload-stopped.db")
    state = web_state.WebUIState(config=Config(), repo=repo)
    created = []

    class FakePipeline:
        def __init__(self, config, current_repo, matcher):
            self.config = config
            self.registry = SimpleNamespace(get_source=lambda name: None)
            created.append(self)

        async def run_scan(self):
            return ScanStats()

    state.worker_runtime.pipeline_factory = FakePipeline
    service = WorkerService(state)
    command_id = repo.enqueue_worker_command("RELOAD_CONFIGURATION")

    async def reload():
        await service._process_commands()

    asyncio.run(reload())

    (tmp_path / "filters.yaml").write_text(
        "roles:\n  - Latest Worker Role\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "AI_PRIMARY_PROVIDER=llamacpp\n"
        "LLAMACPP_MODEL=latest-worker-model\n"
        "LLAMACPP_API_KEY=worker-only-secret\n",
        encoding="utf-8",
    )
    # A second command verifies the worker reads the current files, not startup state.
    command_id = repo.enqueue_worker_command(
        "RELOAD_CONFIGURATION",
        {"env_keys": ["AI_PRIMARY_PROVIDER", "LLAMACPP_MODEL", "LLAMACPP_API_KEY"]},
    )
    asyncio.run(reload())

    assert repo.get_worker_command(command_id)["status"] == "COMPLETED"
    assert state.config.filters.roles == ["Latest Worker Role"]
    assert state.pipeline.config.filters.roles == ["Latest Worker Role"]
    assert state.config.ai.providers["llamacpp"].model == "latest-worker-model"
    assert os.environ["LLAMACPP_API_KEY"] == "worker-only-secret"
    assert len(created) == 2
    assert state.is_running is False
    web_state._web_state = None
    web_state.WebUIState._instance = None


def test_worker_defers_active_reload_until_scan_boundary_and_reuses_runtime(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hirevia.web.state.provider_status", lambda config: [])
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  telegram:\n    enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "filters.yaml").write_text("roles: []\n", encoding="utf-8")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    repo = JobRepository(tmp_path / "config-reload-active.db")
    config = Config()
    config.sources.sources["telegram"].enabled = False
    state = web_state.WebUIState(config=config, repo=repo)
    scan_gate = asyncio.Event()
    created = []

    class FakePipeline:
        def __init__(self, current_config, current_repo, matcher):
            self.config = current_config
            self.registry = SimpleNamespace(get_source=lambda name: None)
            self.wait_for_gate = not created
            created.append(self)

        async def run_scan(self):
            if self.wait_for_gate:
                await scan_gate.wait()
            return ScanStats()

    state.worker_runtime.pipeline_factory = FakePipeline
    state._ensure_ai_matcher = lambda: None
    service = WorkerService(state, poll_seconds=0.01)
    worker_pid = service.pid

    async def reload_during_monitoring():
        assert await state.worker_runtime.start_scanner(persist_desired=False)
        for _ in range(100):
            if state.is_scanning:
                break
            await asyncio.sleep(0.01)
        assert state.is_scanning is True

        (tmp_path / "filters.yaml").write_text(
            "roles:\n  - Active Update Role\n", encoding="utf-8"
        )
        command_id = repo.enqueue_worker_command("RELOAD_CONFIGURATION")
        await service._process_commands()
        assert repo.get_worker_command(command_id)["status"] == "PENDING"
        assert len(created) == 1

        scan_gate.set()
        for _ in range(100):
            if not state.is_scanning:
                break
            await asyncio.sleep(0.01)
        await service._process_commands()
        assert repo.get_worker_command(command_id)["status"] == "COMPLETED"

        assert state.is_running is True
        assert state.config.filters.roles == ["Active Update Role"]
        assert state.pipeline.config.filters.roles == ["Active Update Role"]
        assert len(created) == 2
        assert state.worker_runtime is service.runtime
        assert service.pid == worker_pid
        await state.worker_runtime.stop_scanner(persist_desired=False)

    try:
        asyncio.run(reload_during_monitoring())
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_active_configuration_reload_never_overlaps_telegram_clients(
    monkeypatch, tmp_path
):
    from hirevia.sources import telegram_source

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hirevia.web.state.provider_status", lambda config: [])
    monkeypatch.setattr(
        "hirevia.worker_runtime.load_telegram_settings",
        lambda: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        "hirevia.worker_runtime.load_telegram_credentials", lambda: None
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  telegram:\n    enabled: true\n", encoding="utf-8"
    )
    (tmp_path / "filters.yaml").write_text("roles: []\n", encoding="utf-8")
    web_state._web_state = None
    web_state.WebUIState._instance = None
    repo = JobRepository(tmp_path / "config-reload-telegram.db")
    state = web_state.WebUIState(config=Config(), repo=repo)
    state._ensure_ai_matcher = lambda: None
    state.validate_telegram_sources = lambda **kwargs: asyncio.sleep(
        0, result={"authenticated": True, "validation_complete": True}
    )
    client_counts = {"active": 0, "maximum": 0, "created": 0, "closed": 0}
    sources = []
    pipelines = []

    class FakeTelegramSource:
        last_collection_failed = False
        has_more_backfill = False

        async def start_continuous_monitoring(self):
            assert client_counts["active"] == 0
            client_counts["active"] += 1
            client_counts["created"] += 1
            client_counts["maximum"] = max(
                client_counts["maximum"], client_counts["active"]
            )

        async def stop_continuous_monitoring(self):
            client_counts["active"] -= 1
            client_counts["closed"] += 1

        def enable_event_mode(self):
            return None

        def retry_uncommitted_event(self):
            return False

    class FakePipeline:
        def __init__(self, config, current_repo, matcher):
            source = FakeTelegramSource()
            sources.append(source)
            self.registry = SimpleNamespace(get_source=lambda name: source)
            self.config = config
            pipelines.append(self)

        async def run_scan(self):
            return ScanStats()

    state.worker_runtime.pipeline_factory = FakePipeline
    service = WorkerService(state, poll_seconds=0.01)

    async def reload():
        assert await state.worker_runtime.start_scanner(persist_desired=False)
        for _ in range(100):
            if sources and state.worker_runtime.continuous_telegram_source is sources[0]:
                break
            await asyncio.sleep(0.01)
        command_id = repo.enqueue_worker_command("RELOAD_CONFIGURATION")
        for _ in range(200):
            await service._process_commands()
            command = repo.get_worker_command(command_id)
            if command["status"] in {"COMPLETED", "ERROR"}:
                break
            await asyncio.sleep(0.01)

        assert repo.get_worker_command(command_id)["status"] == "COMPLETED"
        assert state.is_running is True
        assert client_counts == {"active": 1, "maximum": 1, "created": 2, "closed": 1}
        assert len(pipelines) == 2
        await state.worker_runtime.stop_scanner(persist_desired=False)
        assert client_counts["active"] == 0

    try:
        asyncio.run(reload())
    finally:
        telegram_source._shared_client = None
        web_state._web_state = None
        web_state.WebUIState._instance = None
