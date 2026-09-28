"""Tests for YAML filtering"""

import pytest
from hirevia.core.filtering import apply_filters, extract_years_experience
from hirevia.core.models import Job, FilterResult
from hirevia.config import FilterConfig


def make_job(**kwargs):
    defaults = {
        "source": "test",
        "source_job_id": "1",
        "title": "Python Developer",
        "company": "Test Corp",
        "location": "Pune",
        "description": "We need a Python developer with FastAPI and SQL experience. 1 year experience required. Full time position.",
        "url": "https://example.com/job/1",
        "employment_type": "full_time",
    }
    defaults.update(kwargs)
    return Job(**defaults)


def make_config(**kwargs):
    defaults = {
        "roles": ["Python Developer", "Backend Developer"],
        "locations": ["Pune", "Remote"],
        "experience": {"max_years": 2, "fresher": True},
        "skills": ["Python", "FastAPI", "SQL"],
        "employment": ["full_time"],
        "exclude_keywords": ["senior", "lead", "manager"],
    }
    defaults.update(kwargs)
    return FilterConfig(**defaults)


def test_extract_years_experience():
    assert extract_years_experience("3 years experience") == 3
    assert extract_years_experience("5+ years of experience") == 5
    assert extract_years_experience("minimum 2 years") == 2
    assert extract_years_experience("fresher") is None
    assert extract_years_experience("no experience mentioned") is None


def test_role_match():
    job = make_job(title="Python Developer")
    config = make_config(roles=["Python Developer"])
    result = apply_filters(job, config)
    assert result.passed
    assert "Python Developer" in result.matched.get("roles", [])


def test_role_no_match():
    job = make_job(title="Java Developer")
    config = make_config(roles=["Python Developer"])
    result = apply_filters(job, config)
    # Should still pass if other criteria match (location/skills)
    # But roles won't be in matched


def test_location_match():
    job = make_job(location="Pune, Maharashtra")
    config = make_config(locations=["Pune"])
    result = apply_filters(job, config)
    assert result.passed
    assert "Pune" in result.matched.get("locations", [])


def test_location_remote():
    job = make_job(location="Remote", description="Work from home position")
    config = make_config(locations=["Remote"])
    result = apply_filters(job, config)
    assert result.passed


def test_experience_within_limit():
    job = make_job(description="1 year experience required")
    config = make_config(experience={"max_years": 2, "fresher": True})
    result = apply_filters(job, config)
    assert result.passed
    assert result.matched["experience"]["detected_years"] == 1


def test_experience_over_limit():
    job = make_job(description="5 years experience required")
    config = make_config(experience={"max_years": 2, "fresher": True})
    result = apply_filters(job, config)
    assert not result.passed


def test_experience_fresher_ok():
    job = make_job(description="Fresher friendly, 0-1 years experience")
    config = make_config(experience={"max_years": 1, "fresher": True})
    result = apply_filters(job, config)
    assert result.passed
    assert result.matched["experience"]["fresher_ok"] is True


def test_skills_match():
    job = make_job(description="Python FastAPI SQL")
    config = make_config(skills=["Python", "FastAPI", "SQL"])
    result = apply_filters(job, config)
    assert result.passed
    assert set(result.matched.get("skills", [])) >= {"Python", "FastAPI", "SQL"}


def test_exclude_keywords():
    job = make_job(title="Senior Python Developer")
    config = make_config(exclude_keywords=["senior"])
    result = apply_filters(job, config)
    assert not result.passed
    assert "senior" in result.matched.get("excluded", [])


def test_employment_match():
    job = make_job(employment_type="full_time")
    config = make_config(employment=["full_time"])
    result = apply_filters(job, config)
    assert result.passed


def test_all_filters_combined():
    job = make_job(
        title="Backend Developer",
        location="Pune",
        description="Python FastAPI SQL, 1 year experience, full time",
    )
    config = make_config()
    result = apply_filters(job, config)
    assert result.passed
    assert result.matched.get("roles")
    assert result.matched.get("locations")
    assert result.matched.get("skills")
    assert result.matched.get("experience")


def test_no_positive_signal_fails():
    """Job with no matching role, location, or skill should fail."""
    job = make_job(
        title="Java Architect",
        location="Delhi",
        description="Java Spring Hibernate, 5 years experience",
    )
    config = make_config()
    result = apply_filters(job, config)
    assert not result.passed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])