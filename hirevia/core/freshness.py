"""Freshness and expiry logic for jobs"""

from datetime import datetime, timedelta
from typing import Optional

from hirevia.core.models import Job, ApplicationStatus


def calculate_expiry(first_seen: datetime, hours: int = 24) -> datetime:
    """Calculate expiry time from first_seen."""
    return first_seen + timedelta(hours=hours)


def is_expired(job: Job, now: Optional[datetime] = None) -> bool:
    """Check if job is expired."""
    if now is None:
        now = datetime.utcnow()
    if job.expires_at is None:
        return False
    return job.expires_at < now


def is_fresh(job: Job, max_age_hours: int = 168, now: Optional[datetime] = None) -> bool:
    """Check if job is fresh (within max_age_hours)."""
    if now is None:
        now = datetime.utcnow()
    if job.posted_at:
        age = now - job.posted_at
        return age <= timedelta(hours=max_age_hours)
    # If no posted_at, use detected_at but with lower confidence
    age = now - job.detected_at
    return age <= timedelta(hours=max_age_hours)


def get_freshness_score(job: Job, now: Optional[datetime] = None) -> float:
    """
    Get freshness score 0-1.
    1.0 = posted just now
    0.0 = at or past max age
    """
    if now is None:
        now = datetime.utcnow()

    reference_time = job.posted_at if job.posted_at else job.detected_at
    age_hours = (now - reference_time).total_seconds() / 3600
    max_age_hours = 168  # 1 week

    if age_hours <= 0:
        return 1.0
    if age_hours >= max_age_hours:
        return 0.0

    return 1.0 - (age_hours / max_age_hours)


def sort_by_freshness(jobs: list[Job], now: Optional[datetime] = None) -> list[Job]:
    """Sort jobs by freshness (newest first)."""
    if now is None:
        now = datetime.utcnow()

    def sort_key(job: Job):
        # Primary: posted_at (known dates first)
        # Secondary: detected_at
        posted = job.posted_at if job.posted_at else datetime.min
        detected = job.detected_at
        return (posted, detected)

    return sorted(jobs, key=sort_key, reverse=True)


def mark_expired(job: Job, now: Optional[datetime] = None) -> bool:
    """Mark job as expired if past expiry. Returns True if status changed."""
    if now is None:
        now = datetime.utcnow()

    if job.application_status in (ApplicationStatus.OPEN, ApplicationStatus.UNKNOWN):
        if is_expired(job, now):
            job.application_status = ApplicationStatus.EXPIRED
            return True
    return False


def get_time_until_expiry(job: Job, now: Optional[datetime] = None) -> Optional[timedelta]:
    """Get time remaining until expiry."""
    if now is None:
        now = datetime.utcnow()
    if job.expires_at is None:
        return None
    remaining = job.expires_at - now
    return remaining if remaining.total_seconds() > 0 else timedelta(0)