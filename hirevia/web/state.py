"""Web UI State singleton - manages background scanner and shared state"""

import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, List, Optional

from hirevia.config import Config, load_config
from hirevia.core.models import Job, ScanStats, ApplicationStatus
from hirevia.core.pipeline import Pipeline
from hirevia.storage.state import JobRepository
from hirevia.ai.matcher import AIMatcher
from hirevia.ai.providers import provider_status
# NOTE: hirevia.web.api.models is imported inside the methods that need it.
# hirevia.web.api's __init__ imports routes, which imports this module - a
# module-level import here would deadlock that cycle and make
# `import hirevia.web.state` fail unless the API package happened to load first.
from pathlib import Path

logger = logging.getLogger(__name__)


def _telegram_login_error(error: Exception) -> str:
    name = type(error).__name__
    if name == "PhoneNumberInvalidError":
        return "Telegram rejected the phone number. Include the country code and try again"
    if name == "PhoneNumberBannedError":
        return "Telegram cannot authenticate this phone number"
    if name == "FloodWaitError":
        seconds = max(1, int(getattr(error, "seconds", 1) or 1))
        return f"Telegram requires a cooldown of {seconds} seconds before trying again"
    return f"Telegram authentication failed ({name})"


def _keyword_result(job: Job) -> Optional[Dict[str, Any]]:
    result = (job.raw_data or {}).get("keyword_match")
    if isinstance(result, dict) and result.get("matching_method") == "yaml_keyword":
        return result
    return None


def _display_priority(job: Job):
    return job.verdict().get("priority")


def _display_score(job: Job):
    return job.verdict().get("match_score")


def _match_fields(job: Job) -> Dict[str, Any]:
    """The match-related fields the dashboard renders.

    One helper so the list and detail endpoints cannot disagree about which
    score is the real one. Empty/neutral when a job was never matched.
    """
    result = job.match_result or {}
    verdict = job.verdict()
    keyword_result = _keyword_result(job)
    has_keyword_result = keyword_result is not None
    matched = verdict.get("match", verdict.get("relevant"))
    assessment_status = result.get("assessment_status") or job.raw_data.get("assessment_status")
    if assessment_status is None:
        if job.raw_data.get("filter_status"):
            assessment_status = "not_required"
        else:
            assessment_status = {
                "ok": "completed",
                "failed": "failed",
            }.get(result.get("ai_status"), "unscored")
    if matched is True:
        match_status = "matched"
    elif matched is False:
        match_status = "unmatched"
    elif assessment_status in {"pending", "retrying", "failed"}:
        match_status = assessment_status
    elif has_keyword_result and keyword_result.get("status") == "unscored":
        match_status = "unscored"
    elif assessment_status == "not_required":
        match_status = "unmatched"
    else:
        match_status = "unscored"
    return {
        "ai_decision": job.ai_decision,
        "match_result": job.match_result,
        "match_status": match_status,
        "match_score": verdict.get("match_score"),
        "match_priority": verdict.get("priority"),
        "keyword_match_result": keyword_result if has_keyword_result else None,
        "keyword_match_percentage": keyword_result.get("match_percentage") if has_keyword_result else None,
        "keyword_weighted_match_percentage": keyword_result.get("weighted_match_percentage") if has_keyword_result else None,
        "keyword_priority": keyword_result.get("priority") if has_keyword_result else None,
        "matched_keywords": keyword_result.get("matched_keywords", []) if has_keyword_result else [],
        "unmatched_keywords": keyword_result.get("unmatched_keywords", []) if has_keyword_result else [],
        "matched_keyword_count": keyword_result.get("matched_keyword_count", 0) if has_keyword_result else 0,
        "total_keyword_count": keyword_result.get("total_keyword_count", 0) if has_keyword_result else 0,
        "missing_mandatory_keywords": keyword_result.get("missing_mandatory_keywords", []) if has_keyword_result else [],
        "missing_optional_keywords": keyword_result.get("missing_optional_keywords", []) if has_keyword_result else [],
        "matching_method": keyword_result.get("matching_method") if has_keyword_result else None,
        "deterministic_score": result.get("deterministic_score"),
        "ai_score": result.get("ai_score"),
        "ai_status": result.get("ai_status"),
        "assessment_status": assessment_status,
        "matched_skills": result.get("matched_skills") or [],
        "missing_skills": result.get("missing_skills") or [],
        "fresher_eligible": result.get("fresher_eligible"),
        "ai_rejection_reasons": result.get("ai_rejection_reasons") or [],
        "match_reason": result.get("reason") or "",
        # The UI must be able to tell "the model was down" and "a hard rule
        # vetoed this" apart from an ordinary low score - all three look the
        # same if you only show the number.
        "hard_reject": bool(result.get("hard_reject", False)),
        "degraded": result.get("ai_status") == "failed",
        "ai_error": result.get("ai_error"),
    }


class ActivityEntry:
    def __init__(self, message: str, level: str = "info"):
        self.timestamp = datetime.utcnow().isoformat()
        self.message = message
        self.level = level

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "message": self.message,
            "level": self.level,
        }


class WebUIState:
    """Singleton state for web UI - manages background scanner and provides status"""
    
    _instance: Optional["WebUIState"] = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(
        self,
        config: Optional[Config] = None,
        repo: Optional[JobRepository] = None,
        ai_matcher: Optional[AIMatcher] = None,
    ):
        if self._initialized:
            return
        self._initialized = True
        
        # Core components
        self.config: Config = config or load_config()
        self.repo: JobRepository = repo or JobRepository(Path("hirevia.db"))
        self.ai_matcher: Optional[AIMatcher] = ai_matcher
        self.notifier = None

        from hirevia.worker_runtime import WorkerRuntime

        self.worker_runtime = WorkerRuntime(
            self,
            pipeline_factory=lambda current_config, current_repo, matcher: Pipeline(
                current_config, current_repo, matcher, None
            ),
        )
        self.external_worker = False
        self.worker_controller = None
        self.worker_pid: Optional[int] = None
        self.worker_alive = False
        self.scan_interval_minutes: int = self.config.scan_interval_minutes
        
        # Scan tracking
        self.last_scan: Optional[datetime] = None
        self.next_scan: Optional[datetime] = None
        self.last_stats: Optional[ScanStats] = None
        self.last_scan_duration: Optional[float] = None
        
        # Activity log (ring buffer, max 100 entries)
        self.activity_log: deque = deque(maxlen=100)
        
        # Source status cache
        self._source_status_cache: Dict[str, Dict[str, Any]] = {}
        self.telegram_login_client = None
        self.telegram_session_lock = None
        self.telegram_auth_timeout_task: Optional[asyncio.Task] = None
        self.telegram_auth_request_task: Optional[asyncio.Task] = None
        self.telegram_auth_operation_lock = asyncio.Lock()
        self.telegram_login_in_progress = False
        self.telegram_login_state = "disconnected"
        self.telegram_login_phone: Optional[str] = None
        self.telegram_phone_code_hash: Optional[str] = None
        self.telegram_resend_after = 0.0
        self.telegram_auth_error: Optional[str] = None
        self.telegram_authenticated = False
        self.telegram_account: Optional[Dict[str, Any]] = None
        self.telegram_source_validation: Optional[Dict[str, Any]] = None
        self.telegram_source_validation_at: Optional[datetime] = None
        self.telegram_validation_complete = False
        self.monitor_start_error: Optional[str] = None
        self.last_cleanup_at: Optional[datetime] = None
        self.cleanup_error: Optional[str] = None
        self.ai_health_state = "untested"
        self.ai_health_checked_at: Optional[datetime] = None

    @property
    def pipeline(self) -> Optional[Pipeline]:
        return self.worker_runtime.pipeline

    @pipeline.setter
    def pipeline(self, value: Optional[Pipeline]) -> None:
        self.worker_runtime.pipeline = value

    @property
    def scanner_task(self) -> Optional[asyncio.Task]:
        return self.worker_runtime.scanner_task

    @scanner_task.setter
    def scanner_task(self, value: Optional[asyncio.Task]) -> None:
        self.worker_runtime.scanner_task = value

    @property
    def is_running(self) -> bool:
        return self.worker_runtime.is_running

    @is_running.setter
    def is_running(self, value: bool) -> None:
        self.worker_runtime.is_running = value

    @property
    def is_scanning(self) -> bool:
        return self.worker_runtime.is_scanning

    @is_scanning.setter
    def is_scanning(self, value: bool) -> None:
        self.worker_runtime.is_scanning = value

    @property
    def monitor_state(self) -> str:
        return self.worker_runtime.monitor_state

    @monitor_state.setter
    def monitor_state(self, value: str) -> None:
        self.worker_runtime.monitor_state = value

    @property
    def _scanner_started(self) -> Optional[asyncio.Event]:
        return self.worker_runtime.scanner_started

    @_scanner_started.setter
    def _scanner_started(self, value: Optional[asyncio.Event]) -> None:
        self.worker_runtime.scanner_started = value

    @property
    def _scanner_stop(self) -> Optional[asyncio.Event]:
        return self.worker_runtime.scanner_stop

    @_scanner_stop.setter
    def _scanner_stop(self, value: Optional[asyncio.Event]) -> None:
        self.worker_runtime.scanner_stop = value

    @property
    def _continuous_telegram_source(self):
        return self.worker_runtime.continuous_telegram_source

    @_continuous_telegram_source.setter
    def _continuous_telegram_source(self, value) -> None:
        self.worker_runtime.continuous_telegram_source = value

    def _ensure_ai_matcher(self) -> None:
        if self.ai_matcher is None and any(
            provider["configured"] for provider in provider_status(self.config)
        ):
            self.ai_matcher = AIMatcher(self.config, os.getenv("NVIDIA_API_KEY"))

    @staticmethod
    def _telegram_session_file(credentials) -> Path:
        return Path(f"{credentials.session_path}.session")

    @staticmethod
    def _remove_telegram_session_file(credentials) -> None:
        session_file = WebUIState._telegram_session_file(credentials)
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{session_file}{suffix}").unlink(missing_ok=True)

    async def _acquire_telegram_auth_session(self, credentials) -> bool:
        from hirevia.sources.telegram_session_lock import TelegramSessionLock

        if self.external_worker:
            result = await self._send_worker_command("AUTH_RELEASE")
            if not result.get("released"):
                return False

        self.telegram_session_lock = TelegramSessionLock(credentials.session_path)
        acquired = await self.telegram_session_lock.acquire(timeout=10)
        if acquired:
            return True

        self.telegram_session_lock = None
        if self.external_worker:
            try:
                await self._send_worker_command("AUTH_RESUME")
            except Exception:
                logger.warning("Worker did not resume after session handoff timeout")
        return False

    async def _release_telegram_auth_session(
        self, *, resume_worker: bool = True, delete_session=None
    ) -> None:
        lock = self.telegram_session_lock
        cancelled = False
        cleanup_error = None
        try:
            await self._close_telegram_clients()
            if delete_session is not None:
                self._remove_telegram_session_file(delete_session)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            cleanup_error = error
        finally:
            self.telegram_session_lock = None
            if lock is not None:
                lock.release()

        if self.external_worker and resume_worker:
            try:
                result = await self._send_worker_command("AUTH_RESUME")
            except asyncio.CancelledError:
                cancelled = True
                self.repo.set_source_state(
                    "system", "telegram_auth_status", "RECONNECTING"
                )
                self.repo.set_source_state(
                    "system", "telegram_auth_handoff_started_at", None
                )
                self.telegram_authenticated = False
            except Exception as error:
                logger.warning(
                    "Telegram session returned; worker reconnection pending (%s)",
                    type(error).__name__,
                )
                self.repo.set_source_state(
                    "system", "telegram_auth_status", "RECONNECTING"
                )
                self.repo.set_source_state(
                    "system", "telegram_auth_handoff_started_at", None
                )
                self.telegram_authenticated = False
            else:
                auth_status = result.get("auth_status", "AUTH_REQUIRED")
                self.repo.set_source_state("system", "telegram_auth_status", auth_status)
                self._sync_external_worker_status()

        if cleanup_error is not None:
            raise cleanup_error
        if cancelled:
            raise asyncio.CancelledError

    def _schedule_telegram_auth_timeout(self) -> None:
        from hirevia.sources.telegram_session_lock import AUTH_HANDOFF_TIMEOUT_SECONDS

        if self.telegram_auth_timeout_task is not None:
            self.telegram_auth_timeout_task.cancel()

        async def expire_authentication():
            try:
                await asyncio.sleep(AUTH_HANDOFF_TIMEOUT_SECONDS)
                if self.telegram_session_lock is None:
                    return
                self.telegram_login_state = "expired"
                self.telegram_auth_error = "Telegram login timed out; request a new code"
                self.telegram_authenticated = False
                self.telegram_auth_timeout_task = None
                request_task = self.telegram_auth_request_task
                if (
                    request_task is not None
                    and request_task is not asyncio.current_task()
                    and not request_task.done()
                ):
                    request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                if self.telegram_session_lock is not None:
                    await self._release_telegram_auth_session()
            except asyncio.CancelledError:
                raise

        self.telegram_auth_timeout_task = asyncio.create_task(expire_authentication())

    def _cancel_telegram_auth_timeout(self) -> None:
        task = self.telegram_auth_timeout_task
        self.telegram_auth_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _close_telegram_clients(self) -> None:
        from hirevia.sources import telegram_source

        login_client = self.telegram_login_client
        shared_client = telegram_source._shared_client
        disconnected = set()
        for client in (login_client, shared_client):
            if client is None or id(client) in disconnected:
                continue
            disconnected.add(id(client))
            try:
                if client is login_client or client.is_connected():
                    await client.disconnect()
            except Exception as error:
                logger.warning("Telegram client shutdown failed (%s)", type(error).__name__)
        telegram_source._shared_client = None
        self.telegram_login_client = None

    async def restore_telegram_auth_for_startup(self) -> None:
        """Reconnect an existing Telethon session without deleting it."""
        from hirevia.sources import telegram_source
        from hirevia.sources.telegram_config import load_telegram_credentials

        if self.external_worker:
            self._sync_external_worker_status()
            return

        await self._close_telegram_clients()
        self.telegram_login_in_progress = False
        self.telegram_login_state = "disconnected"
        self.telegram_login_phone = None
        self.telegram_phone_code_hash = None
        self.telegram_resend_after = 0.0
        self.telegram_auth_error = None
        self.telegram_authenticated = False
        self.telegram_account = None
        self.telegram_source_validation = None
        self.telegram_source_validation_at = None
        self.telegram_validation_complete = False
        if load_telegram_credentials() is None:
            self.add_activity("Telegram credentials are not configured", "warning")
            return

        try:
            from hirevia.sources.telegram_source import ensure_client

            client = await ensure_client()
            user = await client.get_me()
        except Exception as error:
            transient = isinstance(error, (TimeoutError, ConnectionError, OSError)) or (
                "connection failed" in str(error).casefold()
            )
            self.telegram_login_state = "reconnecting" if transient else "expired"
            self.telegram_auth_error = (
                "Telegram connection is temporarily unavailable; retrying"
                if transient
                else "Telegram session is unavailable; log in again"
            )
            await self._close_telegram_clients()
            self.add_activity(
                f"Telegram session restore requires login ({type(error).__name__})", "warning"
            )
            return

        telegram_source._shared_client = client
        self.telegram_authenticated = True
        self.telegram_login_state = "connected"
        self.telegram_login_phone = getattr(user, "phone", None)
        self.telegram_account = {
            "name": " ".join(filter(None, (user.first_name, user.last_name))) or "Telegram user",
            "username": user.username,
        }
        validation = await self.validate_telegram_sources(force=True)
        self.add_activity(
            "Telegram session restored; channel validation "
            + ("complete" if validation["validation_complete"] else "needs attention")
        )

    async def recover_monitoring_for_startup(self) -> bool:
        """Resume monitoring only when the persisted desired state requests it."""
        if self.external_worker:
            self._sync_external_worker_status()
            return self.is_running
        desired = self.repo.get_source_state("system", "monitor_desired", "stopped")
        if desired != "running":
            self.monitor_state = "STOPPED"
            return False
        from hirevia.sources.telegram_config import load_telegram_settings

        telegram_config = self.config.sources.sources.get("telegram")
        telegram_required = bool(
            telegram_config and telegram_config.enabled and load_telegram_settings().enabled
        )
        if telegram_required and not self.telegram_authenticated:
            self.monitor_state = "AUTH_REQUIRED"
            self.add_activity("Monitoring recovery requires Telegram authentication", "warning")
            return False
        return await self.start_scanner(persist_desired=False)

    def _sync_external_worker_status(self) -> Dict[str, Any]:
        runtime = self.repo.get_worker_runtime()
        self.worker_pid = runtime.get("pid")
        self.worker_alive = bool(runtime.get("alive"))
        desired = self.repo.get_source_state("system", "monitor_desired", "stopped")
        auth_status = self.repo.get_source_state(
            "system", "telegram_auth_status", "DISCONNECTED"
        )
        if self.worker_alive:
            worker_status = runtime.get("status", "ERROR")
            self.monitor_state = runtime.get("monitor_status", "STOPPED")
            if worker_status == "STOPPING":
                self.monitor_state = "STOPPING"
            self.telegram_account = runtime.get("account")
            self.telegram_authenticated = self.telegram_account is not None
            self.telegram_source_validation = runtime.get("validation")
            self.telegram_validation_complete = bool(
                self.telegram_source_validation
                and self.telegram_source_validation.get("validation_complete")
            )
            self.is_running = self.monitor_state in {"RUNNING", "SCANNING", "RECONNECTING"}
            self.is_scanning = self.monitor_state == "SCANNING"
            if self.telegram_authenticated:
                self.telegram_login_state = "connected"
            elif auth_status == "AUTHENTICATING":
                if self.telegram_login_client is None:
                    self.telegram_login_state = "authenticating"
            elif auth_status == "RECONNECTING":
                if self.telegram_login_client is None:
                    self.telegram_login_state = "reconnecting"
            elif auth_status == "ERROR":
                self.telegram_login_state = "error"
            elif auth_status == "AUTH_REQUIRED" or desired == "running":
                self.telegram_login_state = "auth_required"
            else:
                self.telegram_login_state = "disconnected"
        else:
            self.is_running = False
            self.is_scanning = False
            self.monitor_state = (
                "ERROR"
                if runtime.get("status") == "ERROR" or desired == "running"
                else "STOPPED"
            )
        return runtime

    async def _send_worker_command(
        self, command: str, arguments=None, *, timeout: float = 20.0
    ) -> Dict[str, Any]:
        from hirevia.worker_process import WorkerProcessController

        controller = self.worker_controller or WorkerProcessController(
            self.repo, spawn_enabled=False
        )
        return await controller.send_command(command, arguments, timeout=timeout)

    async def run_cleanup_once(self) -> Dict[str, Any]:
        try:
            expired_jobs = self.repo.expire_jobs()
            expired_messages = self.repo.expire_pending_telegram_jobs()
            self.last_cleanup_at = datetime.utcnow()
            self.cleanup_error = None
            if expired_jobs or expired_messages:
                self.add_activity(
                    f"Retention cleanup removed {expired_jobs} job(s) and "
                    f"expired {expired_messages} queued message(s)"
                )
            return {"expired_jobs": expired_jobs, "expired_messages": expired_messages}
        except Exception as error:
            self.cleanup_error = type(error).__name__
            logger.exception("Retention cleanup failed")
            return {"expired_jobs": 0, "expired_messages": 0, "error": self.cleanup_error}

    async def cleanup_loop(self) -> None:
        while True:
            await self.run_cleanup_once()
            await asyncio.sleep(60)

    def add_activity(self, message: str, level: str = "info"):
        """Add an activity log entry"""
        entry = ActivityEntry(message, level)
        self.activity_log.append(entry)
        log = getattr(logger, level.lower(), logger.info)
        log("Web action: %s", message)
    
    def get_activity(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent activity entries"""
        return [e.to_dict() for e in list(self.activity_log)[-limit:]]
    
    async def start_scanner(
        self,
        persist_desired: bool = True,
    ):
        """Start the background scanner"""
        if not self.external_worker:
            return await self.worker_runtime.start_scanner(persist_desired)

        self._sync_external_worker_status()
        if self.is_running or self.monitor_state == "STARTING":
            self.add_activity("Scanner already running", "warning")
            return False

        self.monitor_state = "STARTING"
        try:
            self.repo.get_stats()
        except Exception as error:
            self.monitor_state = "ERROR"
            self.monitor_start_error = "Database is unavailable"
            self.add_activity(f"Monitor startup database check failed ({type(error).__name__})", "error")
            return False

        if self.config.ai.enabled and self.config.ai.primary not in self.config.ai.providers:
            self.monitor_state = "ERROR"
            self.monitor_start_error = "The active AI provider is not configured"
            self.add_activity(self.monitor_start_error, "error")
            return False

        from hirevia.sources.telegram_config import load_telegram_settings

        telegram_config = self.config.sources.sources.get("telegram")
        if (
            telegram_config is not None
            and telegram_config.enabled
            and load_telegram_settings().enabled
        ):
            runtime = self._sync_external_worker_status()
            validation = runtime.get("validation") or {}
            if not runtime.get("alive"):
                validation = {}
            if not validation.get("authenticated"):
                self.monitor_state = "AUTH_REQUIRED"
                self.monitor_start_error = "Connect a Telegram account before starting monitoring"
                self.add_activity(self.monitor_start_error, "warning")
                return False
            if not validation.get("validation_complete"):
                self.monitor_state = "ERROR"
                self.monitor_start_error = "Refresh Telegram channel validation before starting monitoring"
                self.add_activity(self.monitor_start_error, "warning")
                return False
        self.monitor_start_error = None

        await self._close_telegram_clients()
        self.repo.set_source_state("system", "monitor_desired", "running")
        try:
            await self._send_worker_command("START")
        except Exception as error:
            self.repo.set_source_state("system", "monitor_desired", "stopped")
            self.monitor_state = "ERROR"
            self.monitor_start_error = "Independent monitor worker did not start"
            self.add_activity(
                f"Monitor worker start failed ({type(error).__name__})", "error"
            )
            return False

        deadline = asyncio.get_running_loop().time() + 12
        while asyncio.get_running_loop().time() < deadline:
            runtime = self._sync_external_worker_status()
            if runtime.get("monitor_status") in {"RUNNING", "SCANNING", "RECONNECTING"}:
                self.add_activity("Independent monitor worker started")
                return True
            if runtime.get("monitor_status") in {"AUTH_REQUIRED", "ERROR"}:
                self.repo.set_source_state("system", "monitor_desired", "stopped")
                self.monitor_start_error = (
                    "Connect and validate Telegram before starting monitoring"
                    if runtime.get("monitor_status") == "AUTH_REQUIRED"
                    else "Monitor worker failed to initialize"
                )
                return False
            await asyncio.sleep(0.1)
        self.repo.set_source_state("system", "monitor_desired", "stopped")
        self.monitor_state = "ERROR"
        self.monitor_start_error = "Monitor worker did not report an active state"
        return False
    
    async def stop_scanner(self, persist_desired: bool = True):
        """Stop the background scanner"""
        if not self.external_worker:
            return await self.worker_runtime.stop_scanner(persist_desired)

        self._sync_external_worker_status()
        if persist_desired:
            self.repo.set_source_state("system", "monitor_desired", "stopped")

        runtime = self._sync_external_worker_status()
        if not runtime.get("alive"):
            return False
        try:
            await self._send_worker_command("STOP")
        except Exception as error:
            self.monitor_state = "ERROR"
            self.add_activity(
                f"Monitor worker stop failed ({type(error).__name__})", "error"
            )
            return False
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            runtime = self._sync_external_worker_status()
            if runtime.get("monitor_status") == "STOPPED":
                self.add_activity("Independent monitor worker stopped")
                return True
            await asyncio.sleep(0.1)
        self.monitor_state = "ERROR"
        self.add_activity("Monitor worker did not stop before timeout", "error")
        return False
    
    async def trigger_scan(self, scan_24h: bool = False) -> ScanStats:
        """Trigger an immediate scan"""
        if self.external_worker:
            if self.is_scanning:
                raise RuntimeError("Scan already in progress")
            result = await self._send_worker_command("SCAN_24H")
            self.last_scan = datetime.utcnow()
            self.last_stats = ScanStats(
                jobs_found=result.get("jobs_found", 0),
                jobs_new=result.get("jobs_new", 0),
                jobs_duplicate=result.get("jobs_duplicate", 0),
                jobs_failed=result.get("jobs_failed", 0),
            )
            return self.last_stats
        return await self.worker_runtime.trigger_scan(scan_24h)
    
    async def _run_scanner_loop(self):
        """Compatibility entry point delegated to the worker runtime."""
        await self.worker_runtime._run_scanner_loop()
    
    def get_status(self) -> Dict[str, Any]:
        """Get current system status"""
        from hirevia.web.api.models import SystemStatus  # see module note

        worker = self._sync_external_worker_status() if self.external_worker else None
        if not self.external_worker and self.is_running and self.scanner_task is not None and self.scanner_task.done():
            worker_error = None
            if not self.scanner_task.cancelled():
                worker_error = self.scanner_task.exception()
            self.is_running = False
            self.is_scanning = False
            self.monitor_state = "ERROR"
            logger.error(
                "Web monitor worker exited unexpectedly%s",
                f" ({type(worker_error).__name__})" if worker_error else "",
                exc_info=(type(worker_error), worker_error, worker_error.__traceback__)
                if worker_error else None,
            )
            self.add_activity("Monitor worker exited unexpectedly", "error")

        ai_status = next(
            (item for item in provider_status(self.config) if item["name"] == self.config.ai.primary),
            {"configured": False},
        )
        queue_status = self.repo.get_telegram_queue_status()
        status_value = self.monitor_state
        if self.external_worker and (not worker or not worker.get("alive")):
            status_value = "ERROR" if self.repo.get_source_state("system", "monitor_desired", "stopped") == "running" else "STOPPED"
        return {
            "status": (
                SystemStatus.SCANNING.value if self.is_scanning
                else getattr(SystemStatus, status_value, SystemStatus.STOPPED).value
            ),
            "current_operation": "Scanning" if self.is_scanning else self.monitor_state.title(),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "next_scan": self.next_scan.isoformat() if self.next_scan else None,
            "scan_duration": self.last_scan_duration,
            "scan_interval_minutes": self.scan_interval_minutes,
            "is_scanning": self.is_scanning,
            "is_running": self.is_running,
            "telegram_connected": self.telegram_authenticated,
            "ai_provider": self.config.ai.primary,
            "ai_provider_configured": ai_status["configured"],
            "ai_health_state": self.ai_health_state,
            "ai_health_checked_at": self.ai_health_checked_at.isoformat() if self.ai_health_checked_at else None,
            "queue_size": queue_status["queued"],
            "queue_processing": queue_status["processing"],
            "queue_oldest_pending_at": queue_status["oldest_pending_at"],
            "last_cleanup_at": self.last_cleanup_at.isoformat() if self.last_cleanup_at else None,
            "worker_pid": self.worker_pid,
            "worker_alive": self.worker_alive,
            "worker_status": worker.get("status") if worker else None,
            "worker_started_at": worker.get("started_at") if worker else None,
            "worker_last_error": worker.get("last_error") if worker else None,
            "monitor_desired": worker.get("desired_monitoring") if worker else None,
            "worker_heartbeat_at": worker.get("heartbeat_at") if worker else None,
        }
    
    def get_source_status(self) -> List[Dict[str, Any]]:
        """Get status of all sources"""
        from hirevia.web.api.models import SourceStatus  # see module note

        sources = []
        for source_name in self.config.sources.get_enabled_sources():
            count = 0
            # Count jobs from this source
            try:
                with self.repo._get_conn() as conn:
                    cursor = conn.execute(
                        "SELECT COUNT(*) as cnt FROM jobs WHERE source = ?",
                        (source_name,)
                    )
                    row = cursor.fetchone()
                    count = row["cnt"] if row else 0
            except Exception:
                pass
            
            error = self._source_status_cache.get(source_name, {}).get("error")
            status = SourceStatus.ERROR.value if error else (
                SourceStatus.ACTIVE.value if count > 0 else SourceStatus.WARNING.value
            )
            
            sources.append({
                "name": source_name,
                "status": status,
                "jobs_collected": count,
                "last_error": error,
                "enabled": True,
            })
        return sources
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        if self.last_stats:
            return {
                "sources_scanned": self.last_stats.sources_scanned,
                "jobs_found": self.last_stats.jobs_found,
                "jobs_new": self.last_stats.jobs_new,
                "jobs_duplicate": self.last_stats.jobs_duplicate,
                "jobs_filtered": self.last_stats.jobs_filtered,
                "jobs_hard_filter_passed": self.last_stats.jobs_hard_filter_passed,
                "jobs_link_rejected": self.last_stats.jobs_link_rejected,
                "jobs_matching_attempted": self.last_stats.jobs_matching_attempted,
                "jobs_ai_rejected": self.last_stats.jobs_ai_rejected,
                "jobs_ai_matched": self.last_stats.jobs_ai_matched,
                "ai_failures": self.last_stats.ai_failures,
                "jobs_matched": self.last_stats.jobs_matched,
                "jobs_below_match": self.last_stats.jobs_below_match,
                "jobs_verified": self.last_stats.jobs_verified,
                "jobs_verification_attempted": self.last_stats.jobs_verification_attempted,
                "jobs_unverified": self.last_stats.jobs_unverified,
                "jobs_closed": self.last_stats.jobs_closed,
                "jobs_notification_eligible": self.last_stats.jobs_notification_eligible,
                "jobs_notified": self.last_stats.jobs_notified,
                "jobs_skipped_notification": self.last_stats.jobs_skipped_notification,
                "jobs_failed": self.last_stats.jobs_failed,
                "jobs_expired": self.last_stats.jobs_expired,
                "priority_distribution": self.last_stats.priority_distribution,
                "duration_seconds": self.last_stats.duration_seconds,
                "errors": self.last_stats.errors,
            }
        # Fallback to database stats
        stats = self.repo.get_stats()
        return {
            "sources_scanned": 0,
            "jobs_found": stats.jobs_found,
            "jobs_new": 0,
            "jobs_duplicate": 0,
            "jobs_filtered": 0,
            "jobs_hard_filter_passed": 0,
            "jobs_link_rejected": 0,
            "jobs_matching_attempted": 0,
            "jobs_ai_rejected": 0,
            "jobs_ai_matched": 0,
            "ai_failures": 0,
            "jobs_matched": 0,
            "jobs_below_match": 0,
            "jobs_verified": stats.jobs_verified,
            "jobs_verification_attempted": 0,
            "jobs_unverified": 0,
            "jobs_closed": 0,
            "jobs_notification_eligible": 0,
            "jobs_notified": stats.jobs_notified,
            "jobs_skipped_notification": 0,
            "jobs_failed": 0,
            "jobs_expired": stats.jobs_expired,
            "priority_distribution": {"P1": 0, "P2": 0, "P3": 0},
            "duration_seconds": 0,
            "errors": [],
        }
    
    def get_jobs(
        self,
        search: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        min_match: Optional[int] = None,
        location: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        match_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get filtered jobs list"""
        jobs = self.repo.get_active_jobs(hours=24, limit=1000)
        
        # Apply filters
        if search:
            search_lower = search.lower()
            jobs = [j for j in jobs if (
                search_lower in j.title.lower() or
                search_lower in j.company.lower() or
                search_lower in j.location.lower() or
                any(search_lower in s.lower() for s in j.skills)
            )]
        
        if source:
            jobs = [j for j in jobs if j.source == source]
        
        if status:
            jobs = [j for j in jobs if j.application_status.value == status]

        if priority:
            priority = priority.upper()
            jobs = [j for j in jobs if str(_display_priority(j) or "").upper() == priority]

        if match_status == "unmatched":
            jobs = [j for j in jobs if j.verdict().get("match", j.verdict().get("relevant")) is False]
        
        if min_match is not None:
            jobs = [j for j in jobs if (_display_score(j) or 0) >= min_match]
        
        if location:
            loc_lower = location.lower()
            jobs = [j for j in jobs if loc_lower in j.location.lower()]
        
        total = len(jobs)
        jobs = jobs[offset:offset + limit]
        
        # Convert to response format
        job_responses = []
        for job in jobs:
            freshness = None
            if job.detected_at:
                delta = datetime.utcnow() - job.detected_at
                freshness = int(delta.total_seconds() / 60)
            
            job_responses.append({
                "id": job.id,
                "source": job.source,
                "source_job_id": job.source_job_id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "description": job.description,
                "url": str(job.url),
                "application_url": str(job.application_url) if job.application_url else None,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "detected_at": job.detected_at.isoformat() if job.detected_at else None,
                "deadline": job.deadline.isoformat() if job.deadline else None,
                "employment_type": job.employment_type.value if job.employment_type else None,
                "experience": job.experience,
                "skills": job.skills,
                "application_status": job.application_status.value,
                "saved": job.saved,
                "fingerprint": job.fingerprint,
                "first_seen": job.first_seen.isoformat() if job.first_seen else None,
                "last_seen": job.last_seen.isoformat() if job.last_seen else None,
                "saved_at": job.saved_at.isoformat() if job.saved_at else None,
                "expires_at": job.expires_at.isoformat() if job.expires_at else None,
                "notified_at": job.notified_at.isoformat() if job.notified_at else None,
                "notification_channel": job.notification_channel,
                "freshness_minutes": freshness,
                **_match_fields(job),
            })
        
        return {
            "jobs": job_responses,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get single job by ID"""
        job = self.repo.get_job(job_id)
        if not job or job.deleted_at:
            return None
        
        freshness = None
        if job.detected_at:
            delta = datetime.utcnow() - job.detected_at
            freshness = int(delta.total_seconds() / 60)
        
        return {
            "id": job.id,
            "source": job.source,
            "source_job_id": job.source_job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description,
            "url": str(job.url),
            "application_url": str(job.application_url) if job.application_url else None,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "detected_at": job.detected_at.isoformat() if job.detected_at else None,
            "deadline": job.deadline.isoformat() if job.deadline else None,
            "employment_type": job.employment_type.value if job.employment_type else None,
            "experience": job.experience,
            "skills": job.skills,
            "application_status": job.application_status.value,
            "saved": job.saved,
            "fingerprint": job.fingerprint,
            "first_seen": job.first_seen.isoformat() if job.first_seen else None,
            "last_seen": job.last_seen.isoformat() if job.last_seen else None,
            "saved_at": job.saved_at.isoformat() if job.saved_at else None,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "notified_at": job.notified_at.isoformat() if job.notified_at else None,
            "notification_channel": job.notification_channel,
            "freshness_minutes": freshness,
            **_match_fields(job),
        }

    def get_job_counts(self) -> Dict[str, int]:
        counts = {"ALL": 0, "P1": 0, "P2": 0, "P3": 0, "UNMATCHED": 0}
        for job in self.repo.get_active_jobs(hours=24, limit=1000):
            counts["ALL"] += 1
            verdict = job.verdict()
            if verdict.get("match", verdict.get("relevant")) is False:
                counts["UNMATCHED"] += 1
            priority = str(verdict.get("priority", "")).upper()
            if priority in counts:
                counts[priority] += 1
        return counts

    def save_job(self, job_id: str, saved: bool = True) -> bool:
        changed = self.repo.set_saved(job_id, saved)
        if changed:
            self.add_activity("Job saved" if saved else "Job removed from saved")
        return changed

    def delete_job(self, job_id: str) -> bool:
        changed = self.repo.soft_delete_job(job_id)
        if changed:
            self.add_activity("Job permanently deleted")
        return changed

    def delete_all_jobs(self) -> int:
        deleted = self.repo.delete_all_jobs()
        self.add_activity(f"All jobs permanently deleted ({deleted})")
        return deleted
    
    def get_config(self) -> Dict[str, Any]:
        """Get current configuration"""
        return {
            "scan_interval_minutes": self.scan_interval_minutes,
            "enabled_sources": self.config.sources.get_enabled_sources(),
            "filters": {
                "roles": self.config.filters.roles,
                "locations": self.config.filters.locations,
                "experience": self.config.filters.experience,
                "skills": self.config.filters.skills,
                "employment": self.config.filters.employment,
                "exclude_keywords": self.config.filters.exclude_keywords,
            },
        }

    async def reload_configuration(
        self, *, env_keys=(), propagate_to_worker: bool = True
    ) -> None:
        config = load_config()
        ai_matcher = (
            AIMatcher(config, os.getenv("NVIDIA_API_KEY"))
            if any(provider["configured"] for provider in provider_status(config))
            else None
        )
        if self.external_worker and propagate_to_worker:
            await self._send_worker_command(
                "RELOAD_CONFIGURATION",
                {"env_keys": list(env_keys)},
                timeout=45.0,
            )
            self.config = config
            self.ai_matcher = ai_matcher
            self.scan_interval_minutes = config.scan_interval_minutes
            self.telegram_source_validation = None
            self.telegram_source_validation_at = None
            self.telegram_validation_complete = False
            self.notifier = None
        else:
            await self.worker_runtime.reload_configuration(config, ai_matcher)
        self.add_activity("Configuration reloaded")

    async def test_ai_provider(self) -> Dict[str, Any]:
        if not any(provider["configured"] for provider in provider_status(self.config)):
            self.ai_health_state = "unconfigured"
            self.ai_health_checked_at = datetime.utcnow()
            self.add_activity("AI provider test skipped: no provider configured", "warning")
            return {"success": False, "message": "No configured AI provider"}

        from hirevia.core.matching import analyze
        from hirevia.core.models import CandidateProfile

        job = Job(
            source="health-check",
            source_job_id="web-ai-test",
            title="Junior Python Developer",
            company="Example",
            location="Remote",
            description="Entry-level Python backend role",
            url="https://example.com/jobs/health-check",
            application_url="https://example.com/jobs/health-check/apply",
        )
        profile = CandidateProfile(skills=["Python"], preferred_roles=["Python Developer"])
        matcher = AIMatcher(self.config, os.getenv("NVIDIA_API_KEY"))
        try:
            result = await matcher.match(job, profile, analyze(job, profile))
        except Exception:
            self.ai_health_state = "unavailable"
            self.ai_health_checked_at = datetime.utcnow()
            logger.exception("AI provider connection test raised an exception")
            self.add_activity("AI provider connection test failed", "error")
            return {"success": False, "message": "Provider request failed"}
        success = not result.degraded and result.status == "ok"
        self.ai_health_state = "available" if success else "unavailable"
        self.ai_health_checked_at = datetime.utcnow()
        self.add_activity(
            "AI provider connection test succeeded" if success else "AI provider connection test failed",
            "info" if success else "warning",
        )
        return {
            "success": success,
            "message": "Provider responded" if success else "Provider request failed",
            "provider": self.config.ai.primary,
            "model": self.config.ai.providers[self.config.ai.primary].model,
        }
    
    async def validate_telegram_sources(
        self, force: bool = False, *, session_owned: bool = False
    ) -> Dict[str, Any]:
        from hirevia.sources.telegram_config import load_telegram_settings
        from hirevia.sources.telegram_source import close_shared_client, ensure_client

        if self.external_worker and not session_owned:
            auth_status = self.repo.get_source_state(
                "system", "telegram_auth_status", "DISCONNECTED"
            )
            if force and auth_status != "AUTHENTICATING":
                try:
                    return await self._send_worker_command(
                        "VALIDATE_TELEGRAM_SOURCES"
                    )
                except Exception as error:
                    logger.warning(
                        "Worker Telegram validation failed (%s)",
                        type(error).__name__,
                    )
            runtime = self.repo.get_worker_runtime()
            return runtime.get("validation") or {
                "authenticated": auth_status == "CONNECTED",
                "validation_complete": False,
                "account": runtime.get("account"),
                "sources": [],
                "error": auth_status,
            }

        if (
            not force
            and self.telegram_authenticated
            and self.telegram_source_validation is not None
            and self.telegram_source_validation_at is not None
            and datetime.utcnow() - self.telegram_source_validation_at < timedelta(minutes=2)
        ):
            return self.telegram_source_validation

        settings = load_telegram_settings()
        sources = []
        for channel in settings.channels:
            enabled = settings.enabled and channel.enabled
            sources.append({
                "username": channel.username,
                "enabled": enabled,
                "membership": "disabled" if not enabled else "unchecked",
                "access": "disabled" if not enabled else "unchecked",
                "can_join": False,
            })
        
        if not self.telegram_authenticated:
            self.telegram_account = None
            self.telegram_validation_complete = False
            self.telegram_source_validation = {
                "authenticated": False,
                "validation_complete": False,
                "account": None,
                "sources": sources,
                "error": "WebLoginRequired",
            }
            self.telegram_source_validation_at = datetime.utcnow()
            return self.telegram_source_validation

        try:
            client = await ensure_client()
            user = await client.get_me()
        except Exception as error:
            self.telegram_account = None
            self.telegram_validation_complete = False
            await close_shared_client()
            self.telegram_source_validation = {
                "authenticated": False,
                "validation_complete": False,
                "account": None,
                "sources": sources,
                "error": type(error).__name__,
            }
            self.telegram_source_validation_at = datetime.utcnow()
            return self.telegram_source_validation

        self.telegram_account = {
            "name": " ".join(filter(None, (user.first_name, user.last_name))) or "Telegram user",
            "username": user.username,
        }

        validation_complete = True
        if not settings.enabled:
            for item in sources:
                item.update({"enabled": False, "membership": "disabled", "access": "disabled"})
        for item in sources:
            if not item["enabled"]:
                continue
            try:
                entity = await client.get_entity(item["username"])
            except Exception as error:
                error_name = type(error).__name__
                item.update({"membership": "inaccessible", "access": "inaccessible", "error": error_name})
                if error_name not in {"UsernameNotOccupiedError", "ChannelPrivateError", "ChatAdminRequiredError"}:
                    validation_complete = False
                continue

            try:
                from telethon.tl.functions.channels import GetParticipantRequest
                from telethon.tl.types import InputUserSelf

                await client(GetParticipantRequest(entity, InputUserSelf()))
                item.update({"membership": "joined", "access": "accessible"})
            except Exception as error:
                error_name = type(error).__name__
                if error_name in {"UserNotParticipantError", "ChannelParticipantNotFoundError"}:
                    item.update({
                        "membership": "not_joined", "access": "accessible", "can_join": True,
                    })
                elif error_name in {"ChannelPrivateError", "ChatAdminRequiredError", "UsernameNotOccupiedError"}:
                    item.update({"membership": "inaccessible", "access": "inaccessible", "error": error_name})
                else:
                    item.update({"membership": "error", "access": "unknown", "error": error_name})
                    validation_complete = False

        self.telegram_validation_complete = validation_complete
        self.telegram_source_validation = {
            "authenticated": True,
            "validation_complete": validation_complete,
            "account": self.telegram_account,
            "sources": sources,
            "error": None,
        }
        self.telegram_source_validation_at = datetime.utcnow()
        self.add_activity(
            f"Telegram channel validation {'completed' if validation_complete else 'incomplete'}: "
            f"{sum(item['membership'] == 'joined' for item in sources)} joined, "
            f"{sum(item['membership'] == 'not_joined' for item in sources)} not joined, "
            f"{sum(item['membership'] == 'inaccessible' for item in sources)} inaccessible",
            "info" if validation_complete else "warning",
        )
        return self.telegram_source_validation

    async def telegram_status(self) -> Dict[str, Any]:
        from hirevia.sources.telegram_config import load_telegram_credentials, load_telegram_settings

        settings = load_telegram_settings()
        if self.external_worker:
            self._sync_external_worker_status()
        elif self.telegram_authenticated:
            try:
                from hirevia.sources.telegram_source import ensure_client

                client = await ensure_client()
                user = await client.get_me()
                self.telegram_account = {
                    "name": " ".join(filter(None, (user.first_name, user.last_name))) or "Telegram user",
                    "username": user.username,
                }
            except Exception as error:
                logger.warning("Telegram account verification failed (%s)", type(error).__name__)
                self.telegram_authenticated = False
                self.telegram_login_state = "expired"
                self.telegram_account = None
                self.telegram_source_validation = None
                self.telegram_source_validation_at = None
                self.telegram_validation_complete = False
                await self._close_telegram_clients()
        validation = self.telegram_source_validation or {
            "authenticated": False,
            "validation_complete": False,
            "sources": [],
        }
        auth_status = self.repo.get_source_state(
            "system", "telegram_auth_status", "DISCONNECTED"
        )
        from hirevia.sources.telegram_config import mask_phone

        return {
            "connected": self.telegram_authenticated and self.telegram_account is not None,
            "auth_status": auth_status,
            "account": self.telegram_account if self.telegram_authenticated else None,
            "phone_number": mask_phone(self.telegram_login_phone or "") if self.telegram_authenticated else None,
            "login_state": self.telegram_login_state,
            "resend_after_seconds": max(0, int(self.telegram_resend_after - monotonic())),
            "auth_error": self.telegram_auth_error,
            "telegram_enabled": bool(
                self.config.sources.sources.get("telegram")
                and self.config.sources.sources["telegram"].enabled
                and settings.enabled
            ),
            "validation_complete": validation.get("validation_complete", False),
            "source_validation": validation.get("sources", []),
            "sources": [
                {"username": channel.username, "enabled": channel.enabled}
                for channel in settings.channels
            ],
        }

    async def begin_telegram_phone_login(self, phone_number: str) -> Dict[str, Any]:
        async with self.telegram_auth_operation_lock:
            return await self._begin_telegram_phone_login(phone_number)

    async def _begin_telegram_phone_login(self, phone_number: str) -> Dict[str, Any]:
        from hirevia.sources.telegram_config import load_telegram_credentials, mask_phone
        from telethon import TelegramClient

        phone = re.sub(r"[\s().-]", "", phone_number or "")
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            return {"success": False, "login_state": self.telegram_login_state, "message": "Enter a valid phone number with country code"}
        if self.telegram_login_in_progress:
            return {"success": False, "login_state": self.telegram_login_state, "message": "A Telegram authentication request is already in progress"}
        if self.telegram_authenticated:
            return {"success": False, "login_state": "connected", "message": "Disconnect the current Telegram account before logging in again"}
        if self.telegram_login_state == "code_sent" and self.telegram_login_client is not None:
            return {"success": False, "login_state": "code_sent", "message": "Verify the code already sent or wait before requesting another"}
        credentials = load_telegram_credentials()
        if credentials is None:
            return {"success": False, "login_state": "disconnected", "message": "Set Telegram API credentials in the server environment first"}

        try:
            if not await self._acquire_telegram_auth_session(credentials):
                self.telegram_login_state = "authenticating"
                return {
                    "success": False,
                    "login_state": self.telegram_login_state,
                    "message": "Telegram session is currently in use; retry shortly",
                }
        except Exception as error:
            logger.warning("Telegram session handoff failed (%s)", type(error).__name__)
            return {
                "success": False,
                "login_state": "error",
                "message": "Could not pause the Telegram worker safely",
            }

        self.telegram_login_in_progress = True
        client = None
        request_task = asyncio.current_task()
        self.telegram_auth_request_task = request_task
        self._schedule_telegram_auth_timeout()
        try:
            await self._close_telegram_clients()
            credentials.session_path.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(credentials.session_path), credentials.api_id, credentials.api_hash
            )
            self.telegram_login_client = client
            await client.connect()
            sent_code = await client.send_code_request(phone)
            self.telegram_login_phone = phone
            self.telegram_phone_code_hash = sent_code.phone_code_hash
            timeout = getattr(sent_code, "timeout", None)
            if timeout is None:
                timeout = getattr(getattr(sent_code, "type", None), "timeout", None)
            self.telegram_resend_after = monotonic() + max(30, int(timeout or 0))
            self.telegram_auth_error = None
            self.telegram_login_state = "code_sent"
            self.add_activity("Telegram verification code requested")
            return {
                "success": True,
                "login_state": self.telegram_login_state,
                "phone_number": mask_phone(phone),
                "resend_after_seconds": max(0, int(self.telegram_resend_after - monotonic())),
                "message": "Verification code sent by Telegram",
            }
        except asyncio.CancelledError:
            timed_out = self.telegram_login_state == "expired"
            if not timed_out:
                self._cancel_telegram_auth_timeout()
                self.telegram_login_state = "error"
                self.telegram_auth_error = "Telegram login was cancelled"
            self.telegram_authenticated = False
            self.telegram_account = None
            self.telegram_source_validation = None
            self.telegram_validation_complete = False
            await self._release_telegram_auth_session()
            raise
        except Exception as error:
            self._cancel_telegram_auth_timeout()
            await self._release_telegram_auth_session()
            self.telegram_login_state = "error"
            self.telegram_auth_error = _telegram_login_error(error)
            self.add_activity(f"Telegram code request failed ({type(error).__name__})", "warning")
            return {"success": False, "login_state": "error", "message": _telegram_login_error(error)}
        finally:
            self.telegram_login_in_progress = False
            if self.telegram_auth_request_task is request_task:
                self.telegram_auth_request_task = None

    async def resend_telegram_otp(self) -> Dict[str, Any]:
        async with self.telegram_auth_operation_lock:
            return await self._resend_telegram_otp()

    async def _resend_telegram_otp(self) -> Dict[str, Any]:
        if self.telegram_login_in_progress:
            return {"success": False, "login_state": self.telegram_login_state, "message": "A Telegram authentication request is already in progress"}
        if self.telegram_login_client is None or not self.telegram_login_phone:
            return {"success": False, "login_state": "disconnected", "message": "Request a new verification code first"}
        remaining = max(0, int(self.telegram_resend_after - monotonic()))
        if remaining:
            return {"success": False, "login_state": "code_sent", "resend_after_seconds": remaining, "message": f"Wait {remaining} seconds before requesting another code"}

        self.telegram_login_in_progress = True
        try:
            if self.telegram_phone_code_hash:
                try:
                    sent_code = await self.telegram_login_client.resend_code(
                        self.telegram_login_phone, self.telegram_phone_code_hash
                    )
                except (AttributeError, TypeError):
                    sent_code = await self.telegram_login_client.send_code_request(self.telegram_login_phone)
            else:
                sent_code = await self.telegram_login_client.send_code_request(self.telegram_login_phone)
            self.telegram_phone_code_hash = sent_code.phone_code_hash
            timeout = getattr(sent_code, "timeout", None)
            if timeout is None:
                timeout = getattr(getattr(sent_code, "type", None), "timeout", None)
            self.telegram_resend_after = monotonic() + max(30, int(timeout or 0))
            self.telegram_auth_error = None
            self.telegram_login_state = "code_sent"
            self._schedule_telegram_auth_timeout()
            self.add_activity("Telegram verification code resent")
            return {"success": True, "login_state": "code_sent", "resend_after_seconds": max(0, int(self.telegram_resend_after - monotonic())), "message": "A new verification code was sent"}
        except Exception as error:
            name = type(error).__name__
            self.telegram_auth_error = _telegram_login_error(error)
            self.telegram_login_state = "code_expired" if "Expired" in name else "error"
            self.add_activity(f"Telegram code resend failed ({name})", "warning")
            return {"success": False, "login_state": self.telegram_login_state, "message": _telegram_login_error(error)}
        finally:
            self.telegram_login_in_progress = False

    async def verify_telegram_otp(self, code: str) -> Dict[str, Any]:
        async with self.telegram_auth_operation_lock:
            return await self._verify_telegram_otp(code)

    async def _verify_telegram_otp(self, code: str) -> Dict[str, Any]:
        if self.telegram_login_in_progress:
            return {"success": False, "login_state": self.telegram_login_state, "message": "A Telegram authentication request is already in progress"}
        client = self.telegram_login_client
        if client is None or not self.telegram_phone_code_hash or not self.telegram_login_phone:
            return {"success": False, "login_state": "disconnected", "message": "Request a verification code first"}
        otp = re.sub(r"\s", "", code or "")
        if not otp or not otp.isdigit():
            return {"success": False, "login_state": "code_invalid", "message": "Enter the numeric Telegram verification code"}

        self.telegram_login_in_progress = True
        try:
            await client.sign_in(
                phone=self.telegram_login_phone,
                code=otp,
                phone_code_hash=self.telegram_phone_code_hash,
            )
        except Exception as error:
            name = type(error).__name__
            if name == "SessionPasswordNeededError":
                self.telegram_login_state = "password_required"
                self.telegram_auth_error = None
                self._schedule_telegram_auth_timeout()
                self.add_activity("Telegram requested two-step verification")
                return {"success": True, "login_state": "password_required", "message": "Enter your Telegram two-step verification password"}
            if name in {"PhoneCodeInvalidError", "PhoneCodeEmptyError"}:
                self.telegram_login_state = "code_invalid"
                self.telegram_auth_error = "Telegram rejected that code. Check it and try again"
                self.add_activity("Telegram verification code was rejected", "warning")
                return {"success": False, "login_state": "code_invalid", "message": "Telegram rejected that code. Check it and try again"}
            if name in {"PhoneCodeExpiredError", "PhoneCodeHashExpiredError"}:
                self.telegram_login_state = "code_expired"
                self.telegram_phone_code_hash = None
                self.telegram_auth_error = "That code expired. Request a new code"
                self.telegram_resend_after = 0
                self.add_activity("Telegram verification code expired", "warning")
                return {"success": False, "login_state": "code_expired", "message": "That code expired. Request a new code"}
            self.telegram_auth_error = _telegram_login_error(error)
            self.add_activity(f"Telegram OTP verification failed ({name})", "warning")
            return {"success": False, "login_state": self.telegram_login_state, "message": _telegram_login_error(error)}
        finally:
            self.telegram_login_in_progress = False

        return await self._complete_telegram_authentication(client)

    async def verify_telegram_password(self, password: str) -> Dict[str, Any]:
        async with self.telegram_auth_operation_lock:
            return await self._verify_telegram_password(password)

    async def _verify_telegram_password(self, password: str) -> Dict[str, Any]:
        if self.telegram_login_in_progress:
            return {"success": False, "login_state": self.telegram_login_state, "message": "A Telegram authentication request is already in progress"}
        client = self.telegram_login_client
        if client is None or self.telegram_login_state != "password_required":
            return {"success": False, "login_state": self.telegram_login_state, "message": "Telegram is not requesting a two-step verification password"}
        if not password:
            return {"success": False, "login_state": "password_required", "message": "Enter your two-step verification password"}

        self.telegram_login_in_progress = True
        try:
            await client.sign_in(password=password)
        except Exception as error:
            name = type(error).__name__
            self.telegram_auth_error = _telegram_login_error(error)
            self.add_activity("Telegram two-step verification failed", "warning")
            message = "Incorrect Telegram two-step verification password" if name == "PasswordHashInvalidError" else _telegram_login_error(error)
            return {"success": False, "login_state": "password_required", "message": message}
        finally:
            self.telegram_login_in_progress = False

        return await self._complete_telegram_authentication(client)

    async def _complete_telegram_authentication(self, client) -> Dict[str, Any]:
        try:
            user = await client.get_me()
            from hirevia.sources import telegram_source

            telegram_source._shared_client = client
            self.telegram_login_client = None
            self.telegram_authenticated = True
            self.telegram_login_state = (
                "reconnecting" if self.external_worker else "connected"
            )
            self.telegram_auth_error = None
            self.telegram_phone_code_hash = None
            self.telegram_resend_after = 0
            self.telegram_account = {
                "name": " ".join(filter(None, (user.first_name, user.last_name))) or "Telegram user",
                "username": user.username,
            }
            validation = await self.validate_telegram_sources(
                force=True, session_owned=True
            )
            if self.external_worker:
                self._cancel_telegram_auth_timeout()
                await self._release_telegram_auth_session()
                auth_status = self.repo.get_source_state(
                    "system", "telegram_auth_status", "AUTH_REQUIRED"
                )
                if auth_status != "CONNECTED":
                    self.telegram_authenticated = False
                    self.telegram_login_state = (
                        "reconnecting" if auth_status == "RECONNECTING" else "auth_required"
                    )
                    return {
                        "success": False,
                        "login_state": self.telegram_login_state,
                        "validation_complete": False,
                        "message": "Telegram authentication succeeded, but the worker has not verified the session yet",
                    }
            else:
                self._cancel_telegram_auth_timeout()
            self.add_activity(
                "Telegram login succeeded; channel validation "
                + ("complete" if validation["validation_complete"] else "needs attention")
            )
            return {
                "success": True,
                "login_state": "connected",
                "account": self.telegram_account,
                "validation_complete": validation["validation_complete"],
                "message": "Telegram account connected",
            }
        except Exception as error:
            self.telegram_authenticated = False
            self.telegram_login_state = "error"
            self.telegram_auth_error = "Telegram account verification failed"
            self.add_activity(f"Telegram account verification failed ({type(error).__name__})", "error")
            return {"success": False, "login_state": "error", "message": "Telegram account verification failed"}

    async def logout_telegram(self) -> Dict[str, Any]:
        async with self.telegram_auth_operation_lock:
            return await self._logout_telegram()

    async def _logout_telegram(self) -> Dict[str, Any]:
        from hirevia.sources.telegram_config import load_telegram_credentials

        credentials = load_telegram_credentials()
        if self.external_worker:
            self.repo.set_source_state("system", "monitor_desired", "stopped")
            if credentials is None:
                try:
                    await self._send_worker_command("AUTH_RELEASE")
                    await self._send_worker_command("AUTH_RESUME")
                except Exception as error:
                    logger.warning(
                        "Telegram logout handoff failed (%s)", type(error).__name__
                    )
                    return {
                        "success": False,
                        "login_state": "error",
                        "message": "Telegram worker could not release its session",
                    }
                self.repo.set_source_state(
                    "system", "telegram_auth_status", "DISCONNECTED"
                )
            else:
                try:
                    if not await self._acquire_telegram_auth_session(credentials):
                        return {
                            "success": False,
                            "login_state": "authenticating",
                            "message": "Telegram session is currently in use; retry logout shortly",
                        }
                    from telethon import TelegramClient

                    self.telegram_login_client = TelegramClient(
                        str(credentials.session_path),
                        credentials.api_id,
                        credentials.api_hash,
                    )
                    await self.telegram_login_client.connect()
                    if await self.telegram_login_client.is_user_authorized():
                        await self.telegram_login_client.log_out()
                    await self.telegram_login_client.disconnect()
                    self.telegram_login_client = None
                except Exception as error:
                    logger.warning("Telegram logout failed (%s)", type(error).__name__)
                    await self._release_telegram_auth_session()
                    return {
                        "success": False,
                        "login_state": "error",
                        "message": "Telegram logout could not be completed; the existing session was preserved",
                    }

                self._cancel_telegram_auth_timeout()
                await self._release_telegram_auth_session(
                    delete_session=credentials
                )
                self.repo.set_source_state(
                    "system", "telegram_auth_status", "DISCONNECTED"
                )
        else:
            self.repo.set_source_state("system", "monitor_desired", "stopped")
            if self.external_worker:
                self._sync_external_worker_status()
        if self.is_running:
            await self.stop_scanner()
        await self._close_telegram_clients()
        if not self.external_worker and credentials is not None:
            self._remove_telegram_session_file(credentials)
        self._cancel_telegram_auth_timeout()
        self.telegram_authenticated = False
        self.telegram_login_state = "disconnected"
        self.telegram_login_phone = None
        self.telegram_phone_code_hash = None
        self.telegram_resend_after = 0
        self.telegram_auth_error = None
        self.telegram_account = None
        self.telegram_source_validation = None
        self.telegram_source_validation_at = None
        self.telegram_validation_complete = False
        self.add_activity("Telegram account disconnected")
        return {"success": True, "login_state": "disconnected", "message": "Telegram disconnected"}

    async def join_telegram_sources(self) -> Dict[str, Any]:
        if self.external_worker:
            try:
                return await self._send_worker_command("JOIN_TELEGRAM_SOURCES")
            except Exception as error:
                logger.warning("Worker Telegram join failed (%s)", type(error).__name__)
                return {
                    "success": False,
                    "message": "Telegram worker is unavailable",
                    "sources": [],
                }

        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
        from telethon.tl.types import DialogFilter, TextWithEntities
        from hirevia.sources.telegram_config import load_telegram_settings
        from hirevia.sources.telegram_source import ensure_client

        settings = load_telegram_settings()
        validation = await self.validate_telegram_sources(force=True)
        if not validation["authenticated"] or not validation["validation_complete"]:
            return {
                "success": False,
                "message": "Authenticate Telegram and complete channel validation first",
                "sources": [],
            }
        try:
            client = await ensure_client()
        except Exception:
            return {"success": False, "message": "Connect a Telegram user account first", "sources": []}

        checked_sources = {
            item["username"].casefold(): item
            for item in validation["sources"]
        }
        joined = []
        peers = []
        for channel in settings.channels:
            checked = checked_sources.get(channel.username.casefold(), {})
            if not settings.enabled or not channel.enabled:
                joined.append({"username": channel.username, "status": "disabled"})
                continue
            if checked.get("membership") == "joined":
                try:
                    entity = await client.get_entity(channel.username)
                    peers.append(await client.get_input_entity(entity))
                    joined.append({"username": channel.username, "status": "already_joined"})
                except Exception as error:
                    joined.append({"username": channel.username, "status": "unavailable", "error": type(error).__name__})
                continue
            if not checked.get("can_join"):
                joined.append({
                    "username": channel.username,
                    "status": checked.get("membership", "unavailable"),
                    **({"error": checked["error"]} if checked.get("error") else {}),
                })
                continue
            try:
                entity = await client.get_entity(channel.username)
                try:
                    await client(JoinChannelRequest(entity))
                    status = "joined"
                except Exception as error:
                    if type(error).__name__ == "UserAlreadyParticipantError":
                        status = "already_joined"
                    else:
                        raise
                peers.append(await client.get_input_entity(entity))
                joined.append({"username": channel.username, "status": status})
            except Exception as error:
                joined.append({"username": channel.username, "status": "unavailable", "error": type(error).__name__})

        try:
            filters = await client(GetDialogFiltersRequest())
            current = next(
                (item for item in filters if getattr(getattr(item, "title", None), "text", None) == "Hirevia Jobs"),
                None,
            )
            filter_id = current.id if current else max(
                (getattr(item, "id", 0) for item in filters), default=1
            ) + 1
            dialog_filter = DialogFilter(
                id=filter_id,
                title=TextWithEntities(text="Hirevia Jobs", entities=[]),
                pinned_peers=[],
                include_peers=peers,
                exclude_peers=[],
                groups=False,
                broadcasts=False,
                bots=False,
                exclude_muted=False,
                exclude_read=False,
                exclude_archived=False,
            )
            await client(UpdateDialogFilterRequest(id=filter_id, filter=dialog_filter))
            folder = "created"
        except Exception as error:
            folder = "unsupported"
            logger.info("Telegram folder update unavailable (%s)", type(error).__name__)
        joined_count = sum(item["status"] in {"joined", "already_joined"} for item in joined)
        self.add_activity(
            f"Telegram source join completed: {joined_count}/{len(joined)} joined; folder {folder}",
            "info" if joined_count == len(joined) else "warning",
        )
        await self.validate_telegram_sources(force=True)
        return {"success": True, "folder": folder, "sources": joined}

    async def set_telegram_source_membership(self, username: str, join: bool) -> Dict[str, Any]:
        if self.external_worker:
            try:
                return await self._send_worker_command(
                    "SET_TELEGRAM_SOURCE_MEMBERSHIP",
                    {"username": username, "join": join},
                )
            except Exception as error:
                logger.warning(
                    "Worker Telegram membership update failed (%s)",
                    type(error).__name__,
                )
                return {
                    "success": False,
                    "status": "unavailable",
                    "message": "Telegram worker is unavailable",
                }

        from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
        from hirevia.sources.telegram_source import ensure_client

        validation = await self.validate_telegram_sources(force=True)
        if not validation["authenticated"] or not validation["validation_complete"]:
            return {"success": False, "status": "unavailable", "message": "Authenticate Telegram and validate channels first"}
        checked = next(
            (item for item in validation["sources"] if item["username"].casefold() == username.casefold()),
            None,
        )
        if checked is None or not checked["enabled"]:
            return {"success": False, "status": "disabled", "message": "Channel is not an enabled configured source"}
        if join and checked["membership"] == "joined":
            return {"success": True, "status": "already_joined", "message": "Already joined"}
        if join and not checked.get("can_join"):
            return {"success": False, "status": checked["membership"], "message": "Channel is not an accessible public source"}
        if not join and checked["membership"] != "joined":
            return {"success": True, "status": "not_joined", "message": "Not currently joined"}

        try:
            client = await ensure_client()
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity) if join else LeaveChannelRequest(entity))
            status = "joined" if join else "left"
            await self.validate_telegram_sources(force=True)
            self.add_activity(f"Telegram source @{username} {status}")
            return {"success": True, "status": status, "message": f"Channel {status}"}
        except Exception as error:
            error_name = type(error).__name__
            if join and error_name == "UserAlreadyParticipantError":
                self.add_activity(f"Telegram source @{username} already joined")
                return {"success": True, "status": "already_joined", "message": "Already joined"}
            if not join and error_name == "UserNotParticipantError":
                self.add_activity(f"Telegram source @{username} was not joined")
                return {"success": True, "status": "not_joined", "message": "Not currently joined"}
            if error_name == "TelegramAuthError":
                self.add_activity("Telegram channel operation requires an authenticated account", "warning")
                return {"success": False, "status": "unavailable", "message": "Connect a Telegram user account first"}
            logger.warning("Telegram channel operation failed (%s)", error_name, exc_info=True)
            self.add_activity(f"Telegram source {'join' if join else 'leave'} failed ({error_name})", "error")
            return {"success": False, "status": "unavailable", "message": f"Could not {('join' if join else 'leave')} channel ({error_name})"}

# Global instance
_web_state: Optional[WebUIState] = None


def get_web_state() -> WebUIState:
    global _web_state
    if _web_state is None:
        _web_state = WebUIState()
    return _web_state


def initialize_web_state(
    config: Config,
    repo: JobRepository,
    ai_matcher: Optional[AIMatcher] = None,
) -> WebUIState:
    """Bind the web adapter to the already initialized Hirevia core objects."""
    global _web_state
    if _web_state is None:
        _web_state = WebUIState(config=config, repo=repo, ai_matcher=ai_matcher)
    else:
        _web_state.config = config
        _web_state.repo = repo
        _web_state.ai_matcher = ai_matcher
        _web_state.scan_interval_minutes = config.scan_interval_minutes
    return _web_state