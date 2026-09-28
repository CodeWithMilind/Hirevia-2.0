"""Tests for core models"""

import pytest
from datetime import datetime
from hirevia.core.models import (
    Job,
    CandidateProfile,
    AIResponse,
    FilterResult,
    ScanStats,
    ApplicationStatus,
    EmploymentType,
    Priority,
)


def test_job_creation():
    job = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python role",
        url="https://example.com/job/123",
    )
    assert job.id is not None
    assert job.source == "test"
    assert job.application_status == ApplicationStatus.UNKNOWN
    assert job.detected_at is not None


def test_job_equality():
    job1 = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python role",
        url="https://example.com/job/123",
    )
    job2 = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python role",
        url="https://example.com/job/123",
    )
    # Different IDs
    assert job1 != job2
    assert job1.id != job2.id

    # Same ID
    job2.id = job1.id
    assert job1 == job2


def test_candidate_profile():
    profile = CandidateProfile(
        name="Test User",
        summary="Python developer",
        skills=["Python", "FastAPI"],
        experience_years=2,
        preferred_roles=["Python Developer"],
        preferred_locations=["Pune"],
    )
    assert profile.name == "Test User"
    assert len(profile.skills) == 2


def test_ai_response_from_json():
    data = {
        "relevant": True,
        "priority": "P1",
        "match_score": 90,
        "reasons": ["Python match", "Location match"],
    }
    response = AIResponse.from_json(data)
    assert response.relevant is True
    assert response.priority == Priority.P1
    assert response.match_score == 90
    assert len(response.reasons) == 2


def test_ai_response_invalid_priority():
    data = {
        "relevant": True,
        "priority": "INVALID",
        "match_score": 50,
        "reasons": [],
    }
    response = AIResponse.from_json(data)
    assert response.priority == Priority.P3  # Default


def test_ai_response_score_clamping():
    data = {
        "relevant": True,
        "priority": "P1",
        "match_score": 150,
        "reasons": [],
    }
    response = AIResponse.from_json(data)
    assert response.match_score == 100

    data["match_score"] = -10
    response = AIResponse.from_json(data)
    assert response.match_score == 0


def test_filter_result():
    result = FilterResult(
        passed=True,
        matched={"roles": ["Python Developer"]},
        reason="Role match",
    )
    assert result.passed
    assert "Python Developer" in result.matched["roles"]


def test_scan_stats():
    stats = ScanStats(
        sources_scanned=3,
        jobs_found=100,
        jobs_new=10,
        jobs_duplicate=5,
        jobs_filtered=80,
        jobs_ai_matched=8,
        jobs_verified=5,
        jobs_notified=5,
        jobs_expired=2,
        duration_seconds=45.5,
    )
    assert stats.sources_scanned == 3
    assert stats.jobs_found == 100


def test_enums():
    assert ApplicationStatus.OPEN.value == "open"
    assert EmploymentType.FULL_TIME.value == "full_time"
    assert Priority.P1.value == "P1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])