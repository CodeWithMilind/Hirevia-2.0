"""Cross-platform process supervision and SQLite command transport for the worker."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


class WorkerProcessError(RuntimeError):
    pass


class WorkerAlreadyRunning(WorkerProcessError):
    pass


class WorkerProcessController:
    def __init__(self, repo, *, popen=None, startup_timeout: float = 12.0):
        self.repo = repo
        self._popen = popen or subprocess.Popen
        self.startup_timeout = startup_timeout
        self._spawned_process = None

    async def ensure_running(self) -> Dict[str, Any]:
        runtime = self.repo.get_worker_runtime()
        if (
            not runtime.get("alive")
            and self._spawned_process is not None
            and self._spawned_process.poll() is not None
        ):
            await asyncio.to_thread(self._spawned_process.wait)
            self._spawned_process = None
        if not runtime.get("alive"):
            managed_externally = os.getenv("HIREVIA_WORKER_MANAGED", "").strip().lower() in {
                "1", "true", "yes", "on",
            }
            if not managed_externally and self._spawned_process is None:
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

    def _spawn_worker(self):
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path.cwd()),
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            options["start_new_session"] = True
        self._spawned_process = self._popen(
            [sys.executable, "-m", "hirevia", "worker"], **options
        )

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
                        await self._stop_unleased_child(child, current["pid"])
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
    async def _terminate_and_reap(child, force_timeout: float) -> None:
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
        await self.ensure_running()
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
