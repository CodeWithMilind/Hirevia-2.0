"""Tests for freshness and expiry"""

import pytest
from datetime import datetime, timedelta
from hirevia.core.freshness import (
    calculate_expiry,
    is_expired,
    is_fresh,
    get_freshness_score,
    sort_by_freshness,
    mark_expired,
)
from hirevia.core.models import Job, ApplicationStatus


def test_calculate_expiry():
    """Expiry should be 24 hours from first_seen."""
    first_seen = datetime(2024, 1, 1, 12, 0, 0)
    expiry = calculate_expiry(first_seen, hours=24)
    expected = datetime(2024, 1, 2, 12, 0, 0)
    assert expiry == expected


def test_is_expired():
    """Job past expiry should be expired."""
    job = Job(
        source="test",
        source_job_id="1",
        title="Test",
        company="Test",
        location="Test",
        description="Test",
        url="https://example.com",
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    assert is_expired(job)

    job.expires_at = datetime.utcnow() + timedelta(hours=1)
    assert not is_expired(job)


def test_is_fresh_with_posted_at():
    """Freshness should use posted_at when available."""
    job = Job(
        source="test",
        source_job_id="1",
        title="Test",
        company="Test",
        location="Test",
        description="Test",
        url="https://example.com",
        posted_at=datetime.utcnow() - timedelta(hours=12),
    )
    assert is_fresh(job, max_age_hours=24)

    job.posted_at = datetime.utcnow() - timedelta(hours=48)
    assert not is_fresh(job, max_age_hours=24)


def test_is_fresh_without_posted_at():
    """Freshness should use detected_at when posted_at missing."""
    job = Job(
        source="test",
        source_job_id="1",
        title="Test",
        company="Test",
        location="Test",
        description="Test",
        url="https://example.com",
        detected_at=datetime.utcnow() - timedelta(hours=12),
    )
    assert is_fresh(job, max_age_hours=24)


def test_freshness_score():
    """Freshness score should be 0-1."""
    now = datetime.utcnow()
    job = Job(
        source="test",
        source_job_id="1",
        title="Test",
        company="Test",
        location="Test",
        description="Test",
        url="https://example.com",
        posted_at=now,
    )
    score = get_freshness_score(job, now)
    assert score == 1.0

    job.posted_at = now - timedelta(hours=84)  # Half of 168
    score = get_freshness_score(job, now)
    assert 0.4 < score < 0.6

    job.posted_at = now - timedelta(hours=168)
    score = get_freshness_score(job, now)
    assert score == 0.0


def test_sort_by_freshness():
    """Jobs should sort newest first."""
    now = datetime.utcnow()
    jobs = [
        Job(source="t", source_job_id="1", title="Old", company="C", location="L", description="D", url="https://example.com/1",
            posted_at=now - timedelta(days=2)),
        Job(source="t", source_job_id="2", title="New", company="C", location="L", description="D", url="https://example.com/2",
            posted_at=now - timedelta(hours=1)),
        Job(source="t", source_job_id="3", title="Medium", company="C", location="L", description="D", url="https://example.com/3",
            posted_at=now - timedelta(days=1)),
    ]
    sorted_jobs = sort_by_freshness(jobs, now)
    assert sorted_jobs[0].source_job_id == "2"  # Newest first
    assert sorted_jobs[1].source_job_id == "3"
    assert sorted_jobs[2].source_job_id == "1"


def test_mark_expired():
    """mark_expired should update status."""
    job = Job(
        source="test",
        source_job_id="1",
        title="Test",
        company="Test",
        location="Test",
        description="Test",
        url="https://example.com",
        application_status=ApplicationStatus.OPEN,
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    changed = mark_expired(job)
    assert changed
    assert job.application_status == ApplicationStatus.EXPIRED

    # Already expired
    job.application_status = ApplicationStatus.EXPIRED
    changed = mark_expired(job)
    assert not changed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])