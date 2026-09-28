"""Tests for deduplication"""

import pytest
from hirevia.core.dedup import compute_fingerprint, is_duplicate, find_duplicate
from hirevia.core.models import Job


def test_fingerprint_deterministic():
    """Fingerprint should be deterministic for same job."""
    job = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    fp1 = compute_fingerprint(job)
    fp2 = compute_fingerprint(job)
    assert fp1 == fp2


def test_fingerprint_different_jobs():
    """Different jobs should have different fingerprints."""
    job1 = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    job2 = Job(
        source="test",
        source_job_id="456",
        title="Java Developer",
        company="Test Corp",
        location="Pune",
        description="Java Spring role",
        url="https://example.com/job/456",
    )
    assert compute_fingerprint(job1) != compute_fingerprint(job2)


def test_duplicate_same_source_id():
    """Jobs with same source_job_id should be duplicates."""
    job1 = Job(
        source="greenhouse",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    job2 = Job(
        source="greenhouse",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    assert is_duplicate(job1, job2)


def test_duplicate_same_application_url():
    """Jobs with same application URL should be duplicates."""
    job1 = Job(
        source="greenhouse",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
        application_url="https://apply.com/job/123",
    )
    job2 = Job(
        source="lever",
        source_job_id="456",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://other.com/job/456",
        application_url="https://apply.com/job/123",
    )
    assert is_duplicate(job1, job2)


def test_not_duplicate_different_companies():
    """Jobs at different companies should not be duplicates."""
    job1 = Job(
        source="greenhouse",
        source_job_id="123",
        title="Python Developer",
        company="Company A",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    job2 = Job(
        source="greenhouse",
        source_job_id="456",
        title="Python Developer",
        company="Company B",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/456",
    )
    assert not is_duplicate(job1, job2)


def test_find_duplicate():
    """find_duplicate should return matching job."""
    existing = [
        Job(
            source="greenhouse",
            source_job_id="123",
            title="Python Developer",
            company="Test Corp",
            location="Pune",
            description="Python FastAPI role",
            url="https://example.com/job/123",
        )
    ]
    new_job = Job(
        source="lever",
        source_job_id="456",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://other.com/job/456",
        application_url="https://example.com/job/123",
    )
    dup = find_duplicate(new_job, existing)
    assert dup is not None
    assert dup.source_job_id == "123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])