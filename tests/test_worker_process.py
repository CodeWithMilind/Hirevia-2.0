"""Process-level tests for the independent worker foundation."""

import asyncio
import os
import subprocess

from hirevia.core.models import Job
from hirevia.storage.state import JobRepository
from hirevia.worker import WorkerService
from hirevia.worker_process import WorkerProcessController, WorkerProcessError


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

            stopped = await owner.shutdown_worker(timeout=20)
            assert stopped == {"stopped": True, "already_stopped": False}
            assert owner._spawned_process.poll() == 0
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

    async def restart_after_crash():
        try:
            first_runtime = await controller.ensure_running()
            first_process = controller._spawned_process
            assert first_runtime["alive"] is True

            first_process.kill()
            await asyncio.to_thread(first_process.wait, 10)
            assert repo.get_worker_runtime()["alive"] is False

            restarted = await controller.ensure_running()
            assert restarted["alive"] is True
            assert restarted["pid"] == controller._spawned_process.pid
            assert restarted["status"] == "RUNNING"
            await controller.shutdown_worker(timeout=20)
        finally:
            await stop_test_children([controller])

    asyncio.run(restart_after_crash())
    assert repo.get_worker_runtime()["status"] == "STOPPED"


def test_graceful_shutdown_waits_for_in_progress_scan(tmp_path):
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

    async def drain_scan():
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if repo.get_worker_runtime()["status"] == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert repo.get_worker_command(shutdown_id)["status"] == "PENDING"
        assert state.stop_calls == 0

        state.is_scanning = False
        assert await asyncio.wait_for(task, timeout=5) == 0

    asyncio.run(drain_scan())

    assert state.stop_calls == 1
    assert state.close_calls == 1
    assert repo.get_worker_command(shutdown_id)["status"] == "COMPLETED"
    assert repo.get_worker_runtime()["status"] == "STOPPED"
