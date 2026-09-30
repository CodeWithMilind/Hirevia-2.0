"""Tests for YAML filtering"""

import pytest
import yaml
from hirevia.core.filtering import apply_filters, extract_years_experience
from hirevia.core.models import CandidateProfile, Job, FilterResult
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
    assert "senior" in result.matched.get("matched", [])
    assert result.status == "REJECT"
    assert result.reason == "excluded_role"


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
    """A relevance miss is not a hard veto; the matcher can judge it later."""
    job = make_job(
        title="Java Architect",
        location="Pune",
        description="Java Spring Hibernate, 0-2 years experience",
    )
    config = make_config()
    result = apply_filters(job, config)
    assert result.passed
    assert result.status == "PASS"


def test_empty_criteria_accepts_everything():
    """No filters.yaml (all lists blank) must NOT swallow the whole pipeline."""
    job = make_job(title="Java Architect", location="Delhi",
                   description="Java Spring Hibernate, 0-2 years experience")
    result = apply_filters(job, FilterConfig())
    assert result.passed
    assert result.status == "PASS"
    assert result.reason == "passed_hard_filters"


def test_role_allowlist_is_relevance_not_a_hard_veto():
    job = make_job(title="Java Architect", location="Delhi",
                   description="Java Spring Hibernate, 0-2 years experience")
    result = apply_filters(job, FilterConfig(roles=["Python Developer"]))
    assert result.passed
    assert result.status == "PASS"
def test_employment_type_unknown_is_not_a_veto():
    """Telegram posts rarely say \"Full time\" - silence must not drop the job."""
    job = make_job(employment_type=None,
                   description="Python FastAPI SQL, 1 year experience required")
    result = apply_filters(job, make_config(employment=["full_time"]))
    assert result.passed


def test_employment_type_known_and_unwanted_still_vetoes():
    job = make_job(title="Java Developer", location="Pune",
                   description="Java Spring Hibernate, 1 year experience",
                   employment_type="part_time")
    result = apply_filters(job, make_config(employment=["full_time"]))
    assert not result.passed


def test_exclude_keywords_match_whole_words_only():
    """\"lead\" vetoes the word \"lead\", not \"leading\"/\"leadership\"/\"blacklead\"."""
    job = make_job(title="Backend Developer",
                   description="Python FastAPI, you will be leading a small team and show "
                               "leadership, 1 year experience")
    result = apply_filters(job, make_config(exclude_keywords=["lead", "senior"]))
    assert result.passed


def test_exclude_keywords_still_catch_the_real_thing():
    """A bare exclude keyword in the title still vetoes the job."""
    job = make_job(title="Lead Backend Developer",
                   description="Python FastAPI, 1 year experience")
    result = apply_filters(job, make_config(exclude_keywords=["lead", "senior"]))
    assert not result.passed
    assert "lead" in result.matched["matched"]


def test_experience_unknown_is_not_rejected():
    job = make_job(description="Python backend opportunity; experience not specified.")
    result = apply_filters(job, make_config())
    assert result.passed
    assert result.status == "UNKNOWN"
    assert result.reason == "insufficient_experience_information"


def test_range_experience_is_parsed_without_overrejecting_entry_paths():
    config = make_config(experience={"max_years": 2, "fresher": True})
    for description in (
        "Fresher position, 0-2 years.",
        "Entry level, 0-3 years.",
        "Minimum 2 years experience.",
    ):
        assert apply_filters(make_job(description=description), config).passed
    assert not apply_filters(make_job(description="3-5 years required."), config).passed
    assert not apply_filters(make_job(description="5+ years required."), config).passed


def test_unknown_employment_type_is_unknown_not_rejected():
    job = make_job(employment_type=None, description="Python backend role.")
    result = apply_filters(job, make_config(employment=["full_time"]))
    assert result.passed
    assert result.status == "UNKNOWN"


def test_explicit_incompatible_employment_type_rejects():
    job = make_job(employment_type="part_time")
    result = apply_filters(job, make_config(employment=["full_time", "internship"]))
    assert not result.passed
    assert result.reason == "incompatible_employment_type"


def test_explicit_company_exclusion_rejects():
    job = make_job(company="Acme Technologies Private Limited")
    profile = CandidateProfile(avoid_companies=["Acme"])
    result = apply_filters(job, make_config(), profile)
    assert not result.passed
    assert result.reason == "excluded_company"


def test_remote_job_passes_when_remote_is_configured():
    job = make_job(location="Remote")
    result = apply_filters(job, make_config(locations=["Remote", "Pune"]))
    assert result.passed


def test_explicit_incompatible_location_rejects_but_unknown_passes():
    config = make_config(locations=["Pune", "Remote"])
    assert not apply_filters(make_job(location="Mumbai"), config).passed
    unknown = apply_filters(make_job(location=""), config)
    assert unknown.passed and unknown.status == "UNKNOWN"


def test_description_remote_word_does_not_override_explicit_city():
    job = make_job(location="Mumbai", description="Join our remote-first engineering team.")
    result = apply_filters(job, make_config(locations=["Pune", "Remote"]))
    assert not result.passed
    assert result.reason == "incompatible_location"


def test_location_filter_keeps_pune_and_pcmc_distinct():
    config = make_config(locations=["Pune"])
    assert apply_filters(make_job(location="Pune, Maharashtra"), config).passed
    assert not apply_filters(make_job(location="Pimpri-Chinchwad / PCMC"), config).passed


@pytest.mark.parametrize("intent,reason", [
    ("promotional", "promotional_content"),
    ("educational", "educational_content"),
])
def test_non_opportunity_intent_rejects_before_matching(intent, reason):
    job = make_job(raw_data={"intent_category": intent})
    result = apply_filters(job, make_config())
    assert not result.passed
    assert result.reason == reason


def test_skills_are_not_a_hard_requirement():
    job = make_job(title="AI Engineer", description="Build AI services.")
    result = apply_filters(job, make_config(skills=["Python"]))
    assert result.passed


def test_junior_python_backend_role_passes():
    job = make_job(
        title="Junior Python Backend Developer",
        description="Python, FastAPI, entry level, 0-2 years.",
    )
    profile = CandidateProfile(
        candidate_type="fresher", experience_years=0,
        target_max_experience_years=2,
    )
    result = apply_filters(job, make_config(), profile)
    assert result.passed
    assert result.status == "PASS"


def test_missing_optional_filter_fields_are_supported():
    result = apply_filters(make_job(), FilterConfig())
    assert result.passed
    assert result.status == "PASS"


def test_yaml_exclusion_changes_filter_decision_without_code_change():
    job = make_job(title="Python Developer")
    allow = FilterConfig(**yaml.safe_load("exclude_keywords: []"))
    reject = FilterConfig(**yaml.safe_load("exclude_keywords:\n  - python developer\n"))

    assert apply_filters(job, allow).passed
    result = apply_filters(job, reject)
    assert not result.passed
    assert result.reason == "excluded_role"






if __name__ == "__main__":
    pytest.main([__file__, "-v"])