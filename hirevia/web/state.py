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
from hirevia.notifications.telegram import PriorityTelegramNotifier, TelegramNotifier
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


def _match_fields(job: Job) -> Dict[str, Any]:
    """The match-related fields the dashboard renders.

    One helper so the list and detail endpoints cannot disagree about which
    score is the real one. Empty/neutral when a job was never matched.
    """
    result = job.match_result or {}
    verdict = job.verdict()
    return {
        "ai_decision": job.ai_decision,
        "match_result": job.match_result,
        "match_score": verdict.get("match_score"),
        "match_priority": verdict.get("priority"),
        "deterministic_score": result.get("deterministic_score"),
        "ai_score": result.get("ai_score"),
        "ai_status": result.get("ai_status"),
        "matched_skills": result.get("matched_skills") or [],
        "missing_skills": result.get("missing_skills") or [],
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
        self.notifier: Optional[TelegramNotifier] = None
        self.pipeline: Optional[Pipeline] = None
        
        # Scanner state
        self.scanner_task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        self.is_scanning: bool = False
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

    @staticmethod
    def _telegram_session_file(credentials) -> Path:
        return Path(f"{credentials.session_path}.session")

    @staticmethod
    def _remove_telegram_session_file(credentials) -> None:
        session_file = WebUIState._telegram_session_file(credentials)
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{session_file}{suffix}").unlink(missing_ok=True)

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
                if client.is_connected():
                    await client.disconnect()
            except Exception as error:
                logger.warning("Telegram client shutdown failed (%s)", type(error).__name__)
        telegram_source._shared_client = None
        self.telegram_login_client = None

    async def reset_telegram_auth_for_startup(self) -> None:
        """Require a fresh web login for each normal Hirevia service start."""
        from hirevia.sources.telegram_config import load_telegram_credentials

        if self.is_running:
            await self.stop_scanner()
        await self._close_telegram_clients()
        credentials = load_telegram_credentials()
        if credentials is not None:
            self._remove_telegram_session_file(credentials)
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
        self.add_activity("Telegram login required for this Hirevia session")
    
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
        nvidia_key: Optional[str] = None,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ):
        """Start the background scanner"""
        if self.is_running:
            self.add_activity("Scanner already running", "warning")
            return False

        from hirevia.sources.telegram_config import load_telegram_settings

        telegram_config = self.config.sources.sources.get("telegram")
        if (
            telegram_config is not None
            and telegram_config.enabled
            and load_telegram_settings().enabled
        ):
            validation = await self.validate_telegram_sources(force=True)
            if not validation["authenticated"]:
                self.monitor_start_error = "Connect a Telegram account before starting monitoring"
                self.add_activity(self.monitor_start_error, "warning")
                return False
            if not validation["validation_complete"]:
                self.monitor_start_error = "Refresh Telegram channel validation before starting monitoring"
                self.add_activity(self.monitor_start_error, "warning")
                return False
        self.monitor_start_error = None
        
        # Provider credentials are read from environment variables only.
        if self.ai_matcher is None and any(
            provider["configured"] for provider in provider_status(self.config)
        ):
            self.ai_matcher = AIMatcher(self.config, os.getenv("NVIDIA_API_KEY"))
        
        # Initialize Telegram notifier if credentials provided
        telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        from hirevia.sources.telegram_config import load_telegram_settings

        destinations = load_telegram_settings().community_destinations
        if telegram_token and (telegram_chat_id or destinations):
            self.notifier = (
                PriorityTelegramNotifier(telegram_token, destinations)
                if destinations else TelegramNotifier(telegram_token, telegram_chat_id)
            )
        self.pipeline = Pipeline(
            self.config, self.repo, self.ai_matcher, self.notifier
        )
        
        self.is_running = True
        self.add_activity("Scanner started")
        
        # Start background task
        self.scanner_task = asyncio.create_task(self._run_scanner_loop())
        return True
    
    async def stop_scanner(self):
        """Stop the background scanner"""
        if not self.is_running:
            self.add_activity("Scanner not running", "warning")
            return False
        
        self.is_running = False
        
        if self.scanner_task:
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            self.scanner_task = None
        
        if self.notifier:
            await self.notifier.close()
            self.notifier = None
        self.pipeline = Pipeline(self.config, self.repo, self.ai_matcher)
        
        self.add_activity("Scanner stopped")
        return True
    
    async def trigger_scan(self) -> ScanStats:
        """Trigger an immediate scan"""
        if self.is_scanning:
            self.add_activity("Scan already in progress", "warning")
            raise RuntimeError("Scan already in progress")
        
        from hirevia.sources.telegram_config import load_telegram_settings
        
        telegram_config = self.config.sources.sources.get("telegram")
        if (
            telegram_config is not None
            and telegram_config.enabled
            and load_telegram_settings().enabled
        ):
            validation = await self.validate_telegram_sources(force=True)
            if not validation["authenticated"]:
                raise RuntimeError("Connect a Telegram account before scanning")
            if not validation["validation_complete"]:
                raise RuntimeError("Refresh Telegram channel validation before scanning")
        
        self.is_scanning = True
        self.add_activity("Manual scan triggered")
        
        try:
            if self.pipeline is None:
                self.pipeline = Pipeline(
                    self.config, self.repo, self.ai_matcher, self.notifier
                )
            stats = await self.pipeline.run_scan()
            
            # Update state
            self.last_scan = datetime.utcnow()
            self.last_stats = stats
            self.last_scan_duration = stats.duration_seconds
            self.next_scan = datetime.utcnow() + timedelta(minutes=self.scan_interval_minutes)
            
            self.add_activity(
                f"Scan completed: {stats.jobs_found} found, "
                f"{stats.jobs_new} new, {stats.jobs_duplicate} duplicates, "
                f"{stats.jobs_filtered} filtered",
                "info" if not stats.errors else "warning"
            )
            
            return stats
        except Exception as e:
            logger.exception("Web-triggered scan failed")
            self.add_activity(f"Scan failed: {e}", "error")
            raise
        finally:
            self.is_scanning = False
    
    async def _run_scanner_loop(self):
        """Background scanner loop"""
        while self.is_running:
            try:
                if not self.is_scanning:
                    self.add_activity("Starting scheduled scan")
                    await self.trigger_scan()
                
                # Wait for next interval
                wait_seconds = self.scan_interval_minutes * 60
                for _ in range(wait_seconds):
                    if not self.is_running:
                        break
                    await asyncio.sleep(1)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Web monitor loop failed")
                self.add_activity(f"Scanner error: {e}", "error")
                await asyncio.sleep(60)  # Wait before retry
    
    def get_status(self) -> Dict[str, Any]:
        """Get current system status"""
        from hirevia.web.api.models import SystemStatus  # see module note

        if self.is_running and self.scanner_task is not None and self.scanner_task.done():
            worker_error = None
            if not self.scanner_task.cancelled():
                worker_error = self.scanner_task.exception()
            self.is_running = False
            self.is_scanning = False
            logger.error(
                "Web monitor worker exited unexpectedly%s",
                f" ({type(worker_error).__name__})" if worker_error else "",
                exc_info=(type(worker_error), worker_error, worker_error.__traceback__)
                if worker_error else None,
            )
            self.add_activity("Monitor worker exited unexpectedly", "error")

        return {
            "status": (
                SystemStatus.SCANNING.value if self.is_scanning
                else SystemStatus.RUNNING.value if self.is_running
                else SystemStatus.STOPPED.value
            ),
            "current_operation": "Scanning" if self.is_scanning else ("Idle" if self.is_running else "Stopped"),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "next_scan": self.next_scan.isoformat() if self.next_scan else None,
            "scan_duration": self.last_scan_duration,
            "scan_interval_minutes": self.scan_interval_minutes,
            "is_scanning": self.is_scanning,
            "is_running": self.is_running,
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
            jobs = [j for j in jobs if str(j.verdict().get("priority", "")).upper() == priority]
        
        if min_match is not None:
            jobs = [j for j in jobs if (j.verdict().get("match_score") or 0) >= min_match]
        
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
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "notified_at": job.notified_at.isoformat() if job.notified_at else None,
            "notification_channel": job.notification_channel,
            "freshness_minutes": freshness,
            **_match_fields(job),
        }

    def get_job_counts(self) -> Dict[str, int]:
        counts = {"ALL": 0, "P1": 0, "P2": 0, "P3": 0}
        for job in self.repo.get_active_jobs(hours=24, limit=1000):
            counts["ALL"] += 1
            priority = str(job.verdict().get("priority", "")).upper()
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
            self.add_activity("Job removed from active feed")
        return changed
    
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

    async def reload_configuration(self) -> None:
        self.config = load_config()
        self.scan_interval_minutes = self.config.scan_interval_minutes
        self.telegram_source_validation = None
        self.telegram_source_validation_at = None
        self.telegram_validation_complete = False
        self.ai_matcher = (
            AIMatcher(self.config, os.getenv("NVIDIA_API_KEY"))
            if any(provider["configured"] for provider in provider_status(self.config))
            else None
        )
        if self.is_running:
            from hirevia.sources.telegram_config import load_telegram_settings

            previous = self.notifier
            token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
            chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
            destinations = load_telegram_settings().community_destinations
            self.notifier = (
                PriorityTelegramNotifier(token, destinations)
                if token and destinations else TelegramNotifier(token, chat_id)
                if token and chat_id else None
            )
            if previous:
                await previous.close()
        self.pipeline = Pipeline(self.config, self.repo, self.ai_matcher, self.notifier)
        self.add_activity("Configuration reloaded")

    async def test_ai_provider(self) -> Dict[str, Any]:
        if not any(provider["configured"] for provider in provider_status(self.config)):
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
            logger.exception("AI provider connection test raised an exception")
            self.add_activity("AI provider connection test failed", "error")
            return {"success": False, "message": "Provider request failed"}
        success = not result.degraded and result.status == "ok"
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
    
    async def test_telegram(self, message: str) -> Dict[str, Any]:
        """Send a test Telegram message"""
        if not self.notifier:
            return {"success": False, "message": "Telegram not configured"}
        
        try:
            # Create a dummy job and AI response for formatting
            from hirevia.core.models import Job as JobModel, AIResponse, Priority
            
            test_job = JobModel(
                source="test",
                source_job_id="test",
                title="Test Job",
                company="Test Company",
                location="Test Location",
                description="This is a test notification from Hirevia Web UI",
                url="https://example.com",
            )
            
            test_ai = AIResponse(
                relevant=True,
                priority=Priority.P1,
                match_score=100,
                reasons=["Test message"],
            )
            
            success = await self.notifier.send(test_job, test_ai)
            return {"success": success, "message": "Test message sent" if success else "Failed to send"}
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}

    async def validate_telegram_sources(self, force: bool = False) -> Dict[str, Any]:
        from hirevia.sources.telegram_config import load_telegram_settings
        from hirevia.sources.telegram_source import close_shared_client, ensure_client

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
        if self.telegram_authenticated:
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
        from hirevia.sources.telegram_config import mask_phone

        return {
            "connected": self.telegram_authenticated and self.telegram_account is not None,
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
            "community_destinations": settings.community_destinations,
        }

    async def begin_telegram_phone_login(self, phone_number: str) -> Dict[str, Any]:
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

        self.telegram_login_in_progress = True
        client = None
        try:
            await self._close_telegram_clients()
            self._remove_telegram_session_file(credentials)
            credentials.session_path.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(credentials.session_path), credentials.api_id, credentials.api_hash
            )
            await client.connect()
            sent_code = await client.send_code_request(phone)
            self.telegram_login_client = client
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
        except Exception as error:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self.telegram_login_state = "error"
            self.telegram_auth_error = _telegram_login_error(error)
            self.add_activity(f"Telegram code request failed ({type(error).__name__})", "warning")
            return {"success": False, "login_state": "error", "message": _telegram_login_error(error)}
        finally:
            self.telegram_login_in_progress = False

    async def resend_telegram_otp(self) -> Dict[str, Any]:
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
            self.telegram_login_state = "connected"
            self.telegram_auth_error = None
            self.telegram_phone_code_hash = None
            self.telegram_resend_after = 0
            self.telegram_account = {
                "name": " ".join(filter(None, (user.first_name, user.last_name))) or "Telegram user",
                "username": user.username,
            }
            validation = await self.validate_telegram_sources(force=True)
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
        from hirevia.sources.telegram_config import load_telegram_credentials

        if self.is_running:
            await self.stop_scanner()
        await self._close_telegram_clients()
        credentials = load_telegram_credentials()
        if credentials is not None:
            self._remove_telegram_session_file(credentials)
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

    async def telegram_dialogs(self) -> Dict[str, Any]:
        from telethon.utils import get_peer_id
        from hirevia.sources.telegram_source import ensure_client

        try:
            client = await ensure_client()
            dialogs = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                is_channel = bool(getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False))
                is_group = entity.__class__.__name__ == "Chat"
                if not (is_channel or is_group):
                    continue
                dialogs.append({
                    "id": str(get_peer_id(entity)),
                    "title": dialog.title,
                    "username": getattr(entity, "username", None),
                    "type": "channel" if is_channel else "group",
                })
            return {"dialogs": dialogs}
        except Exception:
            return {"dialogs": [], "error": "Connect a Telegram user account to list destinations"}

    async def test_community_destination(self, priority: str) -> Dict[str, Any]:
        from hirevia.core.models import AIResponse, Priority
        from hirevia.notifications.telegram import TelegramNotifier
        from hirevia.sources.telegram_config import load_telegram_settings

        destination = load_telegram_settings().community_destinations.get(priority)
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        if not destination or not token:
            return {"success": False, "message": "Configure a bot token and destination first"}
        notifier = TelegramNotifier(token, destination)
        test_job = Job(
            source="health-check",
            source_job_id=f"web-community-{priority}",
            title=f"Hirevia {priority} destination test",
            company="Hirevia",
            location="—",
            description="Telegram community delivery test",
            url="https://example.com/",
        )
        decision = AIResponse(
            relevant=True,
            priority=Priority(priority),
            match_score=90 if priority == "P1" else 70 if priority == "P2" else 40,
            reasons=["Telegram destination test"],
        )
        try:
            sent = await notifier.send(test_job, decision)
            self.add_activity(
                f"Telegram {priority} destination test {'succeeded' if sent else 'failed'}",
                "info" if sent else "warning",
            )
            return {"success": sent, "message": "Test message sent" if sent else "Telegram rejected the test message"}
        finally:
            await notifier.close()


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