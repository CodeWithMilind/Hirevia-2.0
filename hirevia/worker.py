"""Independent OS worker owning Telegram collection and the processing pipeline."""

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

from hirevia.config import load_config
from hirevia.storage.state import JobRepository
from hirevia.web.state import initialize_web_state

logger = logging.getLogger(__name__)


class WorkerService:
    """Long-lived worker loop controlled through SQLite desired state/commands."""

    def __init__(self, state, *, poll_seconds: float = 0.5):
        self.state = state
        self.runtime = getattr(state, "worker_runtime", state)
        self.repo = state.repo
        self.poll_seconds = poll_seconds
        self.pid = os.getpid()
        self.stop_requested = asyncio.Event()
        self.retry_after = 0.0
        self.retry_delay = 2.0
        self._last_auth_retry = 0.0
        self._next_cleanup = 0.0
        self._shutdown_command_id = None
        self._auth_restore_task = None

    async def run(self) -> int:
        if not self.repo.claim_worker_lease(self.pid):
            logger.error("A Hirevia worker already owns the persistent worker lease")
            return 2

        exit_status = 0
        self.repo.recover_processing_queue()
        self.repo.recover_worker_commands()
        self._install_signal_handlers()
        self.repo.update_worker_runtime(self.pid, "STARTING")
        try:
            self._start_auth_restore()
            self.repo.update_worker_runtime(self.pid, "RUNNING")
            while not self.stop_requested.is_set():
                await self._process_commands()
                if self.stop_requested.is_set():
                    break
                await self._reconcile_desired_state()
                now = asyncio.get_running_loop().time()
                if now >= self._next_cleanup:
                    await self.state.run_cleanup_once()
                    self._next_cleanup = now + 60
                self._publish_runtime()
                try:
                    await asyncio.wait_for(self.stop_requested.wait(), self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                if self.stop_requested.is_set():
                    break
        except asyncio.CancelledError:
            self.repo.update_worker_runtime(self.pid, "STOPPING")
            raise
        except Exception as error:
            logger.exception("Hirevia worker service failed")
            exit_status = 1
            self.repo.update_worker_runtime(
                self.pid, "ERROR", last_error=f"{type(error).__name__}: {error}"
            )
        finally:
            if self._shutdown_command_id is not None:
                self.repo.update_worker_runtime(self.pid, "STOPPING")
            try:
                if self._auth_restore_task is not None and not self._auth_restore_task.done():
                    self._auth_restore_task.cancel()
                    await asyncio.gather(
                        self._auth_restore_task, return_exceptions=True
                    )
                if self.runtime.is_running:
                    await self.runtime.stop_scanner(persist_desired=False)
                await self.state._close_telegram_clients()
            except Exception as error:
                exit_status = 1
                self.repo.update_worker_runtime(
                    self.pid, "ERROR", last_error=f"{type(error).__name__}: {error}"
                )
                logger.exception("Hirevia worker cleanup failed")
            else:
                if self._shutdown_command_id is not None and exit_status == 0:
                    self.repo.complete_worker_command(
                        self._shutdown_command_id, result={"stopped": True}
                    )
            self.repo.release_worker_lease(
                self.pid, "ERROR" if exit_status else "STOPPED"
            )
        return exit_status

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.stop_requested.set)
            except (NotImplementedError, RuntimeError):
                try:
                    signal.signal(signum, lambda *_: self.stop_requested.set())
                except (ValueError, OSError):
                    pass

    async def _process_commands(self) -> None:
        if self.runtime.is_scanning:
            return
        for command in self.repo.claim_worker_commands(limit=1):
            name = command["command"].upper()
            command_id = command["id"]
            arguments = command.get("arguments") or {}
            try:
                if name == "START":
                    self.repo.set_source_state("system", "monitor_desired", "running")
                    await self._reconcile_desired_state()
                    if not self.runtime.is_running:
                        raise RuntimeError(
                            self.state.monitor_start_error or "Monitor did not start"
                        )
                    result = {"running": True}
                elif name == "STOP":
                    self.repo.set_source_state("system", "monitor_desired", "stopped")
                    await self._reconcile_desired_state()
                    if self.runtime.is_running:
                        raise RuntimeError("Monitor did not stop")
                    result = {"stopped": True}
                elif name == "SCAN_24H":
                    stats = await self.runtime.trigger_scan(
                        scan_24h=bool(arguments.get("scan_24h", True))
                    )
                    result = {
                        "jobs_found": stats.jobs_found,
                        "jobs_new": stats.jobs_new,
                        "jobs_duplicate": stats.jobs_duplicate,
                        "jobs_failed": stats.jobs_failed,
                    }
                elif name == "REFRESH_AUTH":
                    await self._refresh_auth()
                    if not self.state.telegram_authenticated:
                        raise RuntimeError("Telegram session could not be verified")
                    self._publish_runtime()
                    result = {"connected": True}
                elif name == "LOGOUT":
                    self.repo.set_source_state("system", "monitor_desired", "stopped")
                    if self.runtime.is_running:
                        await self.runtime.stop_scanner(persist_desired=False)
                    await self.state._close_telegram_clients()
                    self.state.telegram_authenticated = False
                    self.state.telegram_account = None
                    self.state.telegram_source_validation = None
                    self.state.telegram_validation_complete = False
                    self.repo.clear_worker_account(self.pid)
                    result = {"disconnected": True}
                elif name == "SHUTDOWN":
                    self._shutdown_command_id = command_id
                    self.repo.update_worker_runtime(self.pid, "STOPPING")
                    if self.runtime.is_running:
                        await self.runtime.stop_scanner(persist_desired=False)
                    self.stop_requested.set()
                    return
                elif name == "STATUS":
                    runtime = self.repo.get_worker_runtime()
                    available = {
                        "pid": runtime.get("pid"),
                        "status": runtime.get("status"),
                        "monitor_status": runtime.get("monitor_status"),
                        "desired_monitoring": runtime.get("desired_monitoring"),
                    }
                    fields = arguments.get("fields", list(available))
                    result = {
                        key: available[key] for key in fields if key in available
                    }
                else:
                    raise ValueError("Unknown worker command")
                self.repo.complete_worker_command(command_id, result=result)
            except Exception as error:
                self.repo.complete_worker_command(
                    command_id, error=type(error).__name__
                )
                logger.warning(
                    "Worker command %s failed (%s)", name, type(error).__name__
                )

    async def _reconcile_desired_state(self) -> None:
        desired = self.repo.get_source_state("system", "monitor_desired", "stopped")
        if desired != "running":
            if self.runtime.is_running:
                await self.runtime.stop_scanner(persist_desired=False)
            elif self.state.telegram_authenticated:
                await self.state._close_telegram_clients()
            self.retry_delay = 2.0
            self.retry_after = 0.0
            return

        now = asyncio.get_running_loop().time()
        if not self.state.telegram_authenticated and now - self._last_auth_retry >= 5:
            self._start_auth_restore()
        if self.runtime.is_running or now < self.retry_after:
            return

        started = await self.runtime.start_scanner(persist_desired=False)
        if started:
            self.retry_delay = 2.0
            self.retry_after = 0.0
            return

        self.retry_after = now + self.retry_delay
        self.retry_delay = min(self.retry_delay * 2, 60.0)
        if self.runtime.monitor_state in {"AUTH_REQUIRED", "ERROR"}:
            self.repo.update_worker_runtime(
                self.pid,
                "RUNNING",
                monitor_status=self.runtime.monitor_state,
                last_error=self.state.monitor_start_error,
            )

    def _publish_runtime(self) -> None:
        self.repo.update_worker_runtime(
            self.pid,
            "RUNNING",
            monitor_status=(
                "SCANNING" if self.runtime.is_scanning else self.runtime.monitor_state
            ),
            account=self.state.telegram_account if self.state.telegram_authenticated else None,
            validation=self.state.telegram_source_validation,
            last_scan_at=self.state.last_scan,
        )

    def _start_auth_restore(self) -> None:
        if self._auth_restore_task is not None and not self._auth_restore_task.done():
            return
        self._last_auth_retry = asyncio.get_running_loop().time()
        self._auth_restore_task = asyncio.create_task(
            self.state.restore_telegram_auth_for_startup()
        )
        self._auth_restore_task.add_done_callback(self._auth_restore_finished)

    def _auth_restore_finished(self, task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.exception(
                "Worker Telegram session restoration failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _refresh_auth(self) -> None:
        self._start_auth_restore()
        if self._auth_restore_task is not None:
            await self._auth_restore_task


async def run_worker() -> int:
    config = load_config()
    database_path = Path(os.getenv("HIREVIA_DB_PATH", "hirevia.db"))
    repo = JobRepository(database_path)
    from hirevia.cli import _build_ai_matcher

    state = initialize_web_state(config, repo, _build_ai_matcher(config))
    return await WorkerService(state).run()
