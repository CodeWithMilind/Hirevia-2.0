"""Cross-platform process supervision and SQLite command transport for the worker."""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class WorkerProcessError(RuntimeError):
    pass


class WorkerAlreadyRunning(WorkerProcessError):
    pass


class WorkerProcessController:
    def __init__(
        self,
        repo,
        *,
        popen=None,
        startup_timeout: float = 12.0,
        spawn_enabled: Optional[bool] = None,
    ):
        self.repo = repo
        self._popen = popen or subprocess.Popen
        self.startup_timeout = startup_timeout
        self.spawn_enabled = (
            self._local_supervision_enabled()
            if spawn_enabled is None
            else spawn_enabled
        )
        self._spawned_process = None
        self._spawned_db_path: Optional[Path] = None

    @staticmethod
    def _local_supervision_enabled() -> bool:
        mode = os.getenv("HIREVIA_WORKER_MODE", "").strip().lower()
        if mode:
            if mode == "local":
                return True
            if mode in {"systemd", "external"}:
                return False
            raise WorkerProcessError(
                "HIREVIA_WORKER_MODE must be 'local' or 'systemd'"
            )
        managed = os.getenv("HIREVIA_WORKER_MANAGED", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return not managed

    async def ensure_running(
        self, *, spawn_if_missing: Optional[bool] = None
    ) -> Dict[str, Any]:
        may_spawn = self.spawn_enabled and spawn_if_missing is not False
        runtime = self.repo.get_worker_runtime()
        if (
            not runtime.get("alive")
            and self._spawned_process is not None
            and self._spawned_process.poll() is not None
        ):
            await asyncio.to_thread(self._spawned_process.wait)
            self._spawned_process = None
        if not runtime.get("alive"):
            if may_spawn and self._spawned_process is None:
                self._spawn_worker()

        deadline = asyncio.get_running_loop().time() + self.startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            runtime = self.repo.get_worker_runtime()
            if runtime.get("alive"):
                if runtime.get("status") == "ERROR":
                    raise WorkerProcessError(
                        runtime.get("last_error") or "Worker failed during startup"
                    )
                if runtime.get("status") == "RUNNING":
                    child = self._spawned_process
                    if child is None:
                        return {**runtime, "already_running": True}
                    if runtime.get("pid") == child.pid:
                        return {**runtime, "already_running": False}
                    if child.poll() is None:
                        await self._stop_unleased_child(child)
                    current = self.repo.get_worker_runtime()
                    if (
                        current.get("alive")
                        and current.get("status") == "RUNNING"
                        and current.get("pid") != child.pid
                    ):
                        return {**current, "already_running": True}
                    if current.get("alive") and current.get("pid") == child.pid:
                        return {**current, "already_running": False}
                    if child.poll() is not None and child.returncode == 2:
                        await asyncio.sleep(0.1)
                        continue
                    if child.poll() is not None:
                        raise WorkerProcessError(
                            "Competing worker exited while the active lease changed"
                        )
                    else:
                        await asyncio.sleep(0.1)
                        continue
            child = self._spawned_process
            if child is not None and child.poll() is not None:
                exit_code = child.returncode
                if exit_code != 2:
                    raise WorkerProcessError(
                        "Worker process exited before becoming ready "
                        f"(exit code {exit_code})"
                    )
            await asyncio.sleep(0.1)
        if self._spawned_process is not None and self._spawned_process.returncode == 2:
            raise WorkerAlreadyRunning(
                "Another worker owns the database lease, but its process is not visible"
            )
        raise WorkerProcessError(
            "Worker process did not become ready with an active database lease"
        )

    async def supervise(self, stop_event, *, retry_seconds: float = 2.0) -> None:
        """Locally supervise the worker while the web service remains alive."""
        if not self.spawn_enabled:
            return
        while not stop_event.is_set():
            try:
                await self.ensure_running(spawn_if_missing=True)
            except Exception as error:
                logger.error("Local worker unavailable (%s)", type(error).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), retry_seconds)
            except asyncio.TimeoutError:
                pass

    def _spawn_worker(self):
        database_path = Path(self.repo.db_path).expanduser().resolve()
        child_environment = os.environ.copy()
        child_environment["HIREVIA_DB_PATH"] = str(database_path)
        command = [sys.executable, "-m", "hirevia", "worker"]
        if os.name == "nt":
            base_executable = getattr(sys, "_base_executable", sys.executable)
            if Path(base_executable).resolve() != Path(sys.executable).resolve():
                import site

                site_paths = site.getsitepackages()
                bootstrap = (
                    "import runpy, site, sys; "
                    f"sys.prefix = sys.exec_prefix = {sys.prefix!r}; "
                    + "; ".join(
                        f"site.addsitedir({path!r})" for path in site_paths
                    )
                    + "; sys.argv = ['hirevia', 'worker']; "
                    + "runpy.run_module('hirevia', run_name='__main__', alter_sys=True)"
                )
                command = [
                    base_executable,
                    "-c",
                    bootstrap,
                    "-m",
                    "hirevia",
                    "worker",
                ]
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path.cwd()),
            "close_fds": True,
            "env": child_environment,
        }
        if os.name == "nt":
            options["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            options["start_new_session"] = True
        self._spawned_process = self._popen(
            command, **options
        )
        self._spawned_db_path = database_path

    async def shutdown_worker(
        self, *, timeout: float = 30.0, force_timeout: float = 5.0
    ) -> Dict[str, Any]:
        child = self._spawned_process
        if child is not None and child.poll() is not None:
            await asyncio.to_thread(child.wait)
            return {"stopped": True, "already_stopped": True}

        runtime = self.repo.get_worker_runtime()
        if child is None and not runtime.get("alive"):
            return {"stopped": True, "already_stopped": True}
        if (
            child is not None
            and runtime.get("alive")
            and runtime.get("pid") != child.pid
            and runtime.get("status") == "RUNNING"
        ):
            await self._stop_unleased_child(child)
            return {
                "stopped": False,
                "already_stopped": False,
                "child_stopped": child.poll() is not None,
                "worker_pid": runtime["pid"],
            }

        command_id = None
        deadline = asyncio.get_running_loop().time() + timeout
        shutdown_completed = False
        while asyncio.get_running_loop().time() < deadline:
            current = self.repo.get_worker_runtime()
            if child is not None:
                if child.poll() is not None:
                    await asyncio.to_thread(child.wait)
                    if command_id is not None and not shutdown_completed:
                        command = self.repo.get_worker_command(command_id)
                        if not command or command["status"] != "COMPLETED":
                            raise WorkerProcessError(
                                "Worker exited before acknowledging graceful shutdown"
                            )
                    return {"stopped": True, "already_stopped": False}
                if current.get("alive") and current.get("pid") != child.pid:
                    if current.get("status") == "RUNNING":
                        await self._stop_unleased_child(child)
                        return {
                            "stopped": False,
                            "already_stopped": False,
                            "child_stopped": child.poll() is not None,
                            "worker_pid": current["pid"],
                        }
                elif current.get("alive") and current.get("pid") == child.pid:
                    if command_id is None:
                        command_id = self.repo.enqueue_worker_command("SHUTDOWN")
            elif current.get("alive") and command_id is None:
                command_id = self.repo.enqueue_worker_command("SHUTDOWN")

            if command_id is not None:
                command = self.repo.get_worker_command(command_id)
                if command and command["status"] == "ERROR":
                    raise WorkerProcessError(command["error"] or "Worker shutdown failed")
                if command and command["status"] == "COMPLETED":
                    shutdown_completed = True
                    if child is None and not current.get("alive"):
                        return {"stopped": True, "already_stopped": False}
                    if child is not None and child.poll() is not None:
                        await asyncio.to_thread(child.wait)
                        return {"stopped": True, "already_stopped": False}
            await asyncio.sleep(0.1)

        if child is None:
            if not shutdown_completed:
                raise WorkerProcessError(
                    f"Worker did not stop gracefully within {timeout:g} seconds"
                )
            raise WorkerProcessError(
                "Worker acknowledged shutdown but its process is still alive"
            )

        if shutdown_completed:
            await self._terminate_and_reap(child, force_timeout)
            return {"stopped": True, "already_stopped": False, "forced": True}

        await self._terminate_and_reap(child, force_timeout)
        raise WorkerProcessError(
            "Worker did not acquire a lease or stop before the shutdown timeout; "
            "its owned child was terminated and reaped"
        )

    async def _stop_unleased_child(self, child) -> None:
        self._verify_tracked_worker_child(child)
        if child.poll() is not None:
            await asyncio.to_thread(child.wait)
            return

        try:
            await asyncio.to_thread(child.wait, 1.0)
            return
        except subprocess.TimeoutExpired:
            pass

        runtime = self.repo.get_worker_runtime()
        if runtime.get("alive") and runtime.get("pid") == child.pid:
            return
        await self._terminate_and_reap(child, 5.0)

    @staticmethod
    def _is_hirevia_worker_command(arguments) -> bool:
        normalized = [str(argument).lower() for argument in arguments]
        return any(
            normalized[index:index + 3] == ["-m", "hirevia", "worker"]
            for index in range(max(0, len(normalized) - 2))
        )

    def _verify_tracked_worker_child(self, child) -> None:
        if child is not self._spawned_process:
            raise WorkerProcessError(
                "Refusing to stop a process not spawned by this controller"
            )
        child_args = getattr(child, "args", None)
        if child_args is not None and not self._is_hirevia_worker_command(child_args):
            raise WorkerProcessError(
                "Refusing to stop a process that is not a Hirevia worker"
            )
        if self._spawned_db_path is not None:
            expected_db = Path(self.repo.db_path).expanduser().resolve()
            if self._spawned_db_path != expected_db:
                raise WorkerProcessError(
                    "Refusing to stop a worker for a different database"
                )

    async def _terminate_and_reap(self, child, force_timeout: float) -> None:
        self._verify_tracked_worker_child(child)
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, force_timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait)
        else:
            await asyncio.to_thread(child.wait)
        if child.poll() is None:
            raise WorkerProcessError("Worker child could not be reaped")
        return {"stopped": True, "already_stopped": False}

    async def send_command(
        self,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 20.0,
    ) -> Dict[str, Any]:
        await self.ensure_running(spawn_if_missing=False)
        command_id = self.repo.enqueue_worker_command(command, arguments)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = self.repo.get_worker_command(command_id)
            if result and result["status"] in {"COMPLETED", "ERROR"}:
                if result["status"] == "ERROR":
                    raise WorkerProcessError(result["error"] or "Worker command failed")
                return result.get("result") or {"success": True}
            runtime = self.repo.get_worker_runtime()
            if not runtime.get("alive"):
                raise WorkerProcessError("Worker process exited before completing the command")
            await asyncio.sleep(0.1)
        raise WorkerProcessError(f"Worker did not complete {command} within {timeout:g} seconds")
