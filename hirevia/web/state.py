"""Web UI State singleton - manages background scanner and shared state"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from hirevia.config import Config, load_config
from hirevia.core.models import Job, ScanStats, ApplicationStatus
from hirevia.core.pipeline import Pipeline, run_single_scan
from hirevia.storage.state import JobRepository
from hirevia.ai.matcher import AIMatcher
from hirevia.notifications.telegram import TelegramNotifier
# NOTE: hirevia.web.api.models is imported inside the methods that need it.
# hirevia.web.api's __init__ imports routes, which imports this module - a
# module-level import here would deadlock that cycle and make
# `import hirevia.web.state` fail unless the API package happened to load first.
from pathlib import Path

logger = logging.getLogger(__name__)


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
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        # Core components
        self.config: Config = load_config()
        self.repo: JobRepository = JobRepository(Path("hirevia.db"))
        self.ai_matcher: Optional[AIMatcher] = None
        self.notifier: Optional[TelegramNotifier] = None
        
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
    
    def add_activity(self, message: str, level: str = "info"):
        """Add an activity log entry"""
        entry = ActivityEntry(message, level)
        self.activity_log.append(entry)
        logger.info(f"Activity: {message}")
    
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
        
        # Initialize AI matcher if key provided
        if nvidia_key:
            self.ai_matcher = AIMatcher(self.config, nvidia_key)
        
        # Initialize Telegram notifier if credentials provided
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
        
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
        
        self.add_activity("Scanner stopped")
        return True
    
    async def trigger_scan(self) -> ScanStats:
        """Trigger an immediate scan"""
        if self.is_scanning:
            self.add_activity("Scan already in progress", "warning")
            raise RuntimeError("Scan already in progress")
        
        self.is_scanning = True
        self.add_activity("Manual scan triggered")
        
        try:
            stats = await run_single_scan(self.config, self.repo, self.ai_matcher)
            
            # Update state
            self.last_scan = datetime.utcnow()
            self.last_stats = stats
            self.last_scan_duration = stats.duration_seconds
            self.next_scan = datetime.utcnow() + timedelta(minutes=self.scan_interval_minutes)
            
            # Send notifications for new jobs
            if self.notifier and stats.jobs_notified > 0:
                # Fetch newly added jobs and notify
                await self._send_notifications()
            
            self.add_activity(
                f"Scan completed: {stats.jobs_found} found, "
                f"{stats.jobs_new} new, {stats.jobs_duplicate} duplicates, "
                f"{stats.jobs_filtered} filtered",
                "info" if not stats.errors else "warning"
            )
            
            return stats
        except Exception as e:
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
                logger.error(f"Scanner loop error: {e}")
                self.add_activity(f"Scanner error: {e}", "error")
                await asyncio.sleep(60)  # Wait before retry
    
    async def _send_notifications(self):
        """Send Telegram notifications for jobs that passed AI matching"""
        if not self.notifier or not self.ai_matcher:
            return
        
        # Get recently added open jobs
        jobs = self.repo.get_active_jobs(limit=100)
        for job in jobs:
            if job.notified_at:
                continue
            # Phase 7: the stored hybrid verdict is canonical, and to_ai_response
            # keeps the notifier interface intact instead of faking an object.
            verdict = job.verdict_model()
            if verdict is None or not verdict.match or verdict.degraded:
                continue
            try:
                await self.notifier.send(job, verdict.to_ai_response())
                self.repo.mark_notified(job.id, "telegram")
            except Exception as e:
                logger.error(f"Failed to send notification for {job.id}: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current system status"""
        from hirevia.web.api.models import SystemStatus  # see module note

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
                "jobs_ai_matched": self.last_stats.jobs_ai_matched,
                "jobs_verified": self.last_stats.jobs_verified,
                "jobs_notified": self.last_stats.jobs_notified,
                "jobs_expired": self.last_stats.jobs_expired,
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
            "jobs_ai_matched": 0,
            "jobs_verified": stats.jobs_verified,
            "jobs_notified": stats.jobs_notified,
            "jobs_expired": stats.jobs_expired,
            "duration_seconds": 0,
            "errors": [],
        }
    
    def get_jobs(
        self,
        search: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        min_match: Optional[int] = None,
        location: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Get filtered jobs list"""
        jobs = self.repo.get_recent_jobs(hours=168 * 4, limit=1000)  # 4 weeks
        
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
        if not job:
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
            "fingerprint": job.fingerprint,
            "first_seen": job.first_seen.isoformat() if job.first_seen else None,
            "last_seen": job.last_seen.isoformat() if job.last_seen else None,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "notified_at": job.notified_at.isoformat() if job.notified_at else None,
            "notification_channel": job.notification_channel,
            "freshness_minutes": freshness,
            **_match_fields(job),
        }
    
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


# Global instance
_web_state: Optional[WebUIState] = None


def get_web_state() -> WebUIState:
    global _web_state
    if _web_state is None:
        _web_state = WebUIState()
    return _web_state