"""Freshness and expiry logic for jobs"""

from datetime import datetime, timedelta
from typing import Optional

from hirevia.core.models import Job, ApplicationStatus

# Canonical freshness ladder - single source of truth for every stage
# (collector stamps it into raw_data["freshness_label"], notification shows it).
FRESHNESS_LABELS = ("very_fresh", "fresh", "recent", "older", "low_freshness")

# Upper bound of each bucket in hours (low_freshness is the overflow bucket).
FRESHNESS_BUCKETS_HOURS = (1, 6, 24, 72)


def calculate_expiry(first_seen: datetime, hours: int = 24) -> datetime:
    """Calculate expiry time from first_seen."""
    return first_seen + timedelta(hours=hours)


def freshness_label(posted_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Label a post's age: very_fresh / fresh / recent / older / low_freshness.

    Accepts naive-UTC or aware datetimes (aware inputs are normalised to naive
    UTC first, matching how Job.posted_at is stored).
    """
    if posted_at is None:
        return "low_freshness"
    if now is None:
        now = datetime.utcnow()
    if posted_at.tzinfo is not None:
        from datetime import timezone

        posted_at = posted_at.astimezone(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        from datetime import timezone

        now = now.astimezone(timezone.utc).replace(tzinfo=None)

    age_hours = (now - posted_at).total_seconds() / 3600
    if age_hours < 0:  # clock skew / future-dated message
        return "very_fresh"
    for label, bound in zip(FRESHNESS_LABELS, FRESHNESS_BUCKETS_HOURS):
        if age_hours < bound:
            return label
    if age_hours <= FRESHNESS_BUCKETS_HOURS[-1]:
        return "older"
    return FRESHNESS_LABELS[-1]



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