"""Worker-owned pipeline and monitoring lifecycle, independent of HTTP routes."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from hirevia.core.models import ScanStats
from hirevia.core.pipeline import Pipeline
from hirevia.sources.telegram_config import (
    load_telegram_credentials,
    load_telegram_settings,
)

logger = logging.getLogger(__name__)


class WorkerRuntime:
    """Own the one pipeline instance and background collection task."""

    def __init__(self, state, pipeline_factory: Optional[Callable] = None):
        self.state = state
        self.pipeline_factory = pipeline_factory or (
            lambda config, repo, matcher: Pipeline(config, repo, matcher, None)
        )
        self.pipeline: Optional[Pipeline] = None
        self.scanner_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.is_scanning = False
        self.monitor_state = "STOPPED"
        self.scanner_started: Optional[asyncio.Event] = None
        self.scanner_stop: Optional[asyncio.Event] = None
        self.continuous_telegram_source = None
        self.telegram_session_lock = None

    async def acquire_telegram_session(self, *, timeout: float = 0.0) -> bool:
        credentials = load_telegram_credentials()
        if credentials is None:
            return True
        if self.telegram_session_lock is None:
            from hirevia.sources.telegram_session_lock import TelegramSessionLock

            self.telegram_session_lock = TelegramSessionLock(credentials.session_path)
        return await self.telegram_session_lock.acquire(timeout=timeout)

    def release_telegram_session(self) -> None:
        if self.telegram_session_lock is not None:
            self.telegram_session_lock.release()
            self.telegram_session_lock = None

    def owns_telegram_session(self) -> bool:
        if load_telegram_credentials() is None:
            return True
        return bool(self.telegram_session_lock and self.telegram_session_lock.acquired)

    async def reload_configuration(self, config, ai_matcher) -> None:
        state = self.state
        was_running = self.is_running
        if was_running:
            stopped = await self.stop_scanner(
                persist_desired=False, rebuild_pipeline=False
            )
            if not stopped:
                raise RuntimeError("Monitor could not pause for configuration reload")

        state.config = config
        state.ai_matcher = ai_matcher
        state.scan_interval_minutes = config.scan_interval_minutes
        state.notifier = None
        state.telegram_source_validation = None
        state.telegram_source_validation_at = None
        state.telegram_validation_complete = False

        if was_running:
            if not await self.start_scanner(persist_desired=False):
                raise RuntimeError(
                    state.monitor_start_error or "Monitor could not resume after configuration reload"
                )
        else:
            self.pipeline = self.pipeline_factory(config, state.repo, ai_matcher)

    async def start_scanner(self, persist_desired: bool = True) -> bool:
        state = self.state
        if self.is_running or self.monitor_state == "STARTING":
            state.add_activity("Scanner already running", "warning")
            return False

        self.monitor_state = "STARTING"
        try:
            state.repo.get_stats()
        except Exception as error:
            self.monitor_state = "ERROR"
            state.monitor_start_error = "Database is unavailable"
            state.add_activity(
                f"Monitor startup database check failed ({type(error).__name__})", "error"
            )
            return False

        if state.config.ai.enabled and state.config.ai.primary not in state.config.ai.providers:
            self.monitor_state = "ERROR"
            state.monitor_start_error = "The active AI provider is not configured"
            state.add_activity(state.monitor_start_error, "error")
            return False

        if not await self.acquire_telegram_session():
            self.monitor_state = "AUTHENTICATING"
            state.monitor_start_error = "Telegram session is owned by authentication"
            return False

        telegram_config = state.config.sources.sources.get("telegram")
        telegram_enabled = bool(
            telegram_config
            and telegram_config.enabled
            and load_telegram_settings().enabled
        )
        if telegram_enabled:
            validation = await state.validate_telegram_sources(force=True)
            if not validation.get("authenticated"):
                self.monitor_state = "AUTH_REQUIRED"
                state.monitor_start_error = (
                    "Connect a Telegram account before starting monitoring"
                )
                state.add_activity(state.monitor_start_error, "warning")
                return False
            if not validation.get("validation_complete"):
                self.monitor_state = "ERROR"
                state.monitor_start_error = (
                    "Refresh Telegram channel validation before starting monitoring"
                )
                state.add_activity(state.monitor_start_error, "warning")
                return False

        state.monitor_start_error = None
        state._ensure_ai_matcher()

        self.pipeline = self.pipeline_factory(
            state.config, state.repo, state.ai_matcher
        )
        self.continuous_telegram_source = None
        if telegram_enabled:
            registry = getattr(self.pipeline, "registry", None)
            source = registry.get_source("telegram") if registry else None
            if source is None:
                self.monitor_state = "ERROR"
                state.monitor_start_error = "Telegram collector could not be initialized"
                return False
            try:
                channel_count = await source.start_continuous_monitoring()
            except Exception as error:
                self.monitor_state = "ERROR"
                state.monitor_start_error = "Telegram listener could not be started"
                state.add_activity(
                    f"Telegram monitor startup failed ({type(error).__name__})", "error"
                )
                return False
            if isinstance(channel_count, int):
                logger.info("Telegram connected (%d channels)", channel_count)
            else:
                logger.info("Telegram connected")
            self.continuous_telegram_source = source

        self.scanner_started = asyncio.Event()
        self.scanner_stop = asyncio.Event()
        self.is_running = True
        self.scanner_task = asyncio.create_task(self._run_scanner_loop())
        try:
            await asyncio.wait_for(self.scanner_started.wait(), timeout=5)
        except Exception:
            self.is_running = False
            self.scanner_stop.set()
            self.scanner_task.cancel()
            await asyncio.gather(self.scanner_task, return_exceptions=True)
            self.scanner_task = None
            self.monitor_state = "ERROR"
            state.monitor_start_error = "Monitor worker did not become ready"
            if persist_desired:
                state.repo.set_source_state("system", "monitor_desired", "stopped")
            return False

        self.monitor_state = "RUNNING"
        if persist_desired:
            state.repo.set_source_state("system", "monitor_desired", "running")
        state.add_activity("Scanner started")
        logger.info("Monitor started")
        return True

    async def stop_scanner(
        self, persist_desired: bool = True, *, rebuild_pipeline: bool = True
    ) -> bool:
        state = self.state
        if persist_desired:
            state.repo.set_source_state("system", "monitor_desired", "stopped")
        if not self.is_running:
            self.monitor_state = "STOPPED"
            state.add_activity("Scanner not running", "warning")
            return False

        self.monitor_state = "STOPPING"
        self.is_running = False
        if self.scanner_stop:
            self.scanner_stop.set()
        if self.continuous_telegram_source is not None:
            await self.continuous_telegram_source.stop_continuous_monitoring()
            self.continuous_telegram_source = None
        if self.scanner_task:
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                logger.warning("Monitor worker was cancelled during shutdown")
            self.scanner_task = None

        if rebuild_pipeline:
            self.pipeline = self.pipeline_factory(
                state.config, state.repo, state.ai_matcher
            )
        self.monitor_state = "STOPPED"
        state.add_activity("Scanner stopped")
        return True

    async def trigger_scan(self, scan_24h: bool = False) -> ScanStats:
        state = self.state
        if self.is_scanning:
            state.add_activity("Scan already in progress", "warning")
            raise RuntimeError("Scan already in progress")

        telegram_config = state.config.sources.sources.get("telegram")
        if (
            telegram_config
            and telegram_config.enabled
            and load_telegram_settings().enabled
        ):
            if not await self.acquire_telegram_session():
                raise RuntimeError("Telegram session is owned by authentication")
            validation = await state.validate_telegram_sources(force=False)
            if not validation["authenticated"]:
                raise RuntimeError("Connect a Telegram account before scanning")
            if not validation["validation_complete"]:
                raise RuntimeError("Refresh Telegram channel validation before scanning")

        self.is_scanning = True
        state.add_activity("Manual scan triggered")
        telegram_source = None
        prior_lookback = False
        try:
            if self.pipeline is None:
                self.pipeline = self.pipeline_factory(
                    state.config, state.repo, state.ai_matcher
                )
            registry = getattr(self.pipeline, "registry", None)
            telegram_source = registry.get_source("telegram") if registry else None
            prior_lookback = getattr(telegram_source, "force_24h_lookback", False)
            if telegram_source is not None:
                telegram_source.force_24h_lookback = scan_24h
            stats = await self.pipeline.run_scan()

            state.last_scan = datetime.utcnow()
            state.last_stats = stats
            state.last_scan_duration = stats.duration_seconds
            state.next_scan = datetime.utcnow() + timedelta(
                minutes=state.scan_interval_minutes
            )
            state.add_activity(
                f"Scan completed: {stats.jobs_found} found, "
                f"{stats.jobs_new} new, {stats.jobs_duplicate} duplicates, "
                f"{stats.jobs_filtered} filtered",
                "info" if not stats.errors else "warning",
            )
            matched_summary = (
                f" | Matched: {stats.jobs_ai_matched}"
                if stats.jobs_ai_matched else ""
            )
            logger.info(
                "Scan complete | Found: %d | New: %d%s",
                stats.jobs_found, stats.jobs_new, matched_summary,
            )
            return stats
        except Exception as error:
            logger.exception("Worker scan failed")
            state.add_activity(f"Scan failed: {error}", "error")
            raise
        finally:
            if telegram_source is not None:
                telegram_source.force_24h_lookback = prior_lookback
            self.is_scanning = False

    async def _run_scanner_loop(self) -> None:
        if self.scanner_started:
            self.scanner_started.set()
        self.monitor_state = "RUNNING"
        source = self.continuous_telegram_source
        retry_delay = 2
        if source is not None:
            try:
                while self.is_running:
                    stats = await self.trigger_scan()
                    if source.last_collection_failed or stats.jobs_failed:
                        self.monitor_state = "RECONNECTING"
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 60)
                    elif source.has_more_backfill:
                        continue
                    else:
                        break
                if self.is_running:
                    source.enable_event_mode()
                    retry_delay = 2
                    while self.is_running:
                        stats = await self.trigger_scan()
                        if source.retry_uncommitted_event():
                            self.monitor_state = "RECONNECTING"
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, 60)
                        else:
                            retry_delay = 2
                            self.monitor_state = "RUNNING"
                        await asyncio.sleep(0.1)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Telegram monitor worker failed")
                self.monitor_state = "ERROR"
                self.state.add_activity(
                    f"Telegram monitor worker failed ({type(error).__name__})", "error"
                )
                self.is_running = False
                return

        while self.is_running:
            try:
                if not self.is_scanning:
                    self.state.add_activity("Starting scheduled scan")
                    await self.trigger_scan()
                if self.scanner_stop is None:
                    return
                try:
                    await asyncio.wait_for(
                        self.scanner_stop.wait(),
                        timeout=self.state.scan_interval_minutes * 60,
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.exception("Web monitor loop failed")
                self.state.add_activity(f"Scanner error: {error}", "error")
                self.monitor_state = (
                    "AUTH_REQUIRED"
                    if "Connect a Telegram account" in str(error)
                    else "RECONNECTING"
                )
                if self.scanner_stop is None:
                    return
                try:
                    await asyncio.wait_for(self.scanner_stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    if self.is_running:
                        self.monitor_state = "RUNNING"
        if self.monitor_state not in {"STOPPING", "STOPPED"}:
            self.monitor_state = "ERROR"

    def _nvidia_key(self) -> Optional[str]:
        import os

        return os.getenv("NVIDIA_API_KEY")
