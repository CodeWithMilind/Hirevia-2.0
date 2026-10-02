"""Tests for YAML-driven keyword relevance scoring."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from hirevia.config import Config
from hirevia.core.keyword_matching import (
    KeywordConfiguration,
    load_keyword_configuration,
    match_keywords,
)
from hirevia.core.models import AIResponse, EmploymentType, Job, Priority
from hirevia.core.pipeline import Pipeline
from hirevia.storage.state import JobRepository
from hirevia.web.state import WebUIState, _match_fields


KEYWORDS = ["Python", "FastAPI", "Flask", "Java", "JavaScript", "SQL", "Git", "REST API"]


def job(title="", description="", skills=None):
    return Job(
        source="test",
        source_job_id="1",
        title=title,
        company="Unknown",
        location="",
        description=description,
        url="https://example.com/job/1",
        skills=skills or [],
    )


def configuration(**overrides):
    data = {
        "candidate": {"keywords": KEYWORDS},
        "matching": {
            "p1_threshold": 70,
            "p2_threshold": 40,
            "p3_threshold": 1,
            "fields_to_match": ["title", "description", "skills"],
            "use_ai_for_scoring": False,
        },
    }
    data.update(overrides)
    return KeywordConfiguration.model_validate(data)


def test_strong_match_uses_keyword_percentage_and_priority():
    result = match_keywords(
        job(
            "Software Engineer",
            "Required skills: Python, FastAPI, Flask, SQL, RESTful API, PostgreSQL, Docker, Git, Redis, GraphQL.",
        ),
        configuration(candidate={"keywords": [
            "Python", "FastAPI", "Flask", "SQL", "REST API", "Postgres", "Docker", "Git"
        ]}),
    )

    assert result.match_percentage == 80
    assert result.priority == "P1"
    assert result.matched_keyword_count == 8
    assert result.total_keyword_count == 10
    assert result.matching_method == "yaml_keyword"


def test_four_of_five_jd_skills_is_eighty_percent():
    result = match_keywords(
        job("Software Engineer", "Python, SQL, Docker, Git, and Redis required."),
        configuration(candidate={"keywords": ["Python", "SQL", "Docker", "Git"]}),
    )

    assert result.matched_keyword_count == 4
    assert result.total_keyword_count == 5
    assert result.match_percentage == 80
    assert result.priority == "P1"


def test_python_backend_jd_counts_redis_not_unmentioned_mongodb():
    jd_skills = [
        "Python", "FastAPI", "Flask", "REST API", "SQL", "MySQL",
        "Git", "Docker", "PostgreSQL", "Redis",
    ]
    candidate_skills = [*jd_skills[:-1], "MongoDB"]
    result = match_keywords(
        job(
            "TEST JOB P1 - Python Backend Developer",
            "Required Skills: " + ", ".join(jd_skills),
        ),
        configuration(candidate={"keywords": candidate_skills}),
        candidate_skills=candidate_skills,
    )

    assert result.matched_keyword_count == 9
    assert result.total_keyword_count == 10
    assert result.match_percentage == 90
    assert result.matched_keywords.count("MongoDB") == 0
    assert result.unmatched_keywords == ["Redis"]


def test_zero_of_ten_jd_skills_is_unmatched():
    result = match_keywords(
        job("Software Engineer", "Java JavaScript SQL Python Docker AWS React Pandas NumPy Statistics"),
        configuration(candidate={"keywords": []}),
        candidate_skills=[],
    )

    assert result.match_percentage == 0
    assert result.priority == "Unmatched"
    assert result.matched_keyword_count == 0
    assert result.total_keyword_count == 10


def test_unrelated_candidate_skills_do_not_reduce_jd_coverage():
    relevant = ["Python", "FastAPI", "Flask", "SQL", "REST API", "Postgres", "Docker", "Git"]
    unrelated = [f"unrelated skill {index}" for index in range(100)]
    result = match_keywords(
        job("Software Engineer", "Python FastAPI Flask SQL REST API PostgreSQL Docker Git Redis GraphQL"),
        configuration(candidate={"keywords": relevant + unrelated}),
    )

    assert result.match_percentage == 80
    assert result.matched_keyword_count == 8
    assert result.total_keyword_count == 10


def test_unrelated_role_is_zero_and_unmatched():
    result = match_keywords(
        job("Mechanical Design Engineer", "AutoCAD SolidWorks CATIA"), configuration()
    )

    assert result.match_percentage is None
    assert result.priority is None
    assert result.status == "unscored"


def test_repeated_keyword_counts_once():
    result = match_keywords(job(description=" ".join(["Python"] * 15)), configuration())

    assert result.matched_keywords == ["Python"]
    assert result.matched_keyword_count == 1


def test_case_and_punctuation_variants_match_canonically():
    config = configuration(
        candidate={"keywords": [
            "Python", "Postgres", "REST API", "Machine Learning", "Generative AI", "RAG"
        ]},
        matching={
            "keyword_aliases": {
                "Postgres": ["PostgreSQL"],
                "REST API": ["RESTful APIs"],
                "Machine Learning": ["ML models"],
            }
        },
    )
    result = match_keywords(
        job(
            "PYTHON",
            "postgres RESTful APIs machine-learning GenAI Generative AI retrieval-augmented generation RAG",
        ),
        config,
    )

    assert result.match_percentage == 100
    assert result.matched_keywords == [
        "Python", "REST API", "Postgres", "Machine Learning", "Generative AI", "RAG"
    ]
    assert result.total_keyword_count == 6


def test_required_optional_skills_have_different_weights():
    job_posting = job(
        "Data Analyst",
        "Required: SQL, Excel. Nice to have: Power BI.",
    )
    misses_optional = match_keywords(
        job_posting,
        configuration(candidate={"keywords": ["SQL", "Excel"]}),
    )
    misses_mandatory = match_keywords(
        job_posting,
        configuration(candidate={"keywords": ["SQL", "Power BI"]}),
    )

    assert misses_optional.match_percentage == pytest.approx(2 / 3 * 100)
    assert misses_optional.weighted_match_percentage == 80
    assert misses_optional.missing_optional_keywords == ["Power BI"]
    assert misses_mandatory.weighted_match_percentage == 60
    assert misses_mandatory.missing_mandatory_keywords == ["Excel"]


def test_generic_soft_skills_do_not_inflate_technical_coverage():
    result = match_keywords(
        job("Software Engineer", "Communication, teamwork, and problem-solving are essential."),
        configuration(candidate={"keywords": ["Communication", "Teamwork", "Problem Solving"]}),
    )

    assert result.status == "unscored"
    assert result.total_keyword_count == 0


def test_role_aware_extraction_uses_distinct_backend_and_data_science_pools():
    description = (
        "Python, FastAPI, REST APIs, SQL, databases, Docker, Pandas, NumPy, "
        "statistics, machine learning, and model evaluation."
    )
    backend = match_keywords(
        job("Python Backend Developer", description), configuration(candidate={"keywords": []})
    )
    scientist = match_keywords(
        job("Data Scientist", description), configuration(candidate={"keywords": []})
    )

    assert "FastAPI" in backend.unmatched_keywords
    assert "FastAPI" not in scientist.unmatched_keywords
    assert "Statistics" in scientist.unmatched_keywords
    assert "Statistics" not in backend.unmatched_keywords


def test_java_does_not_match_javascript():
    config = configuration(candidate={"keywords": ["Java"]})
    result = match_keywords(job(description="JavaScript developer"), config)

    assert result.match_percentage == 0
    assert result.priority == "Unmatched"


def test_missing_fields_only_match_available_title():
    config = configuration(candidate={"keywords": ["Python", "SQL"]})
    result = match_keywords(job(title="Python Developer"), config)

    assert result.match_percentage == 100
    assert result.matched_keywords == ["Python"]
    assert result.unmatched_keywords == []


def test_empty_keyword_list_is_unscored_not_unmatched():
    result = match_keywords(job(title="Python Developer"), configuration(candidate={"keywords": []}))

    assert result.match_percentage == 0
    assert result.priority == "Unmatched"
    assert result.status == "scored"
    assert result.total_keyword_count == 1


def test_nested_yaml_configuration_loads(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "candidate": {
                    "experience_level": "fresher",
                    "max_experience_years": 1,
                    "preferred_roles": ["Python Developer"],
                    "role_categories": ["software_development"],
                    "preferred_locations": ["Pune", "Remote"],
                    "keywords": ["Python", "SQL"],
                },
                "matching": {
                    "p1_threshold": 80,
                    "p2_threshold": 50,
                    "p3_threshold": 1,
                    "fields_to_match": ["title"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = load_keyword_configuration(path)

    assert loaded.candidate.experience_level == "fresher"
    assert loaded.candidate.preferred_roles == ["Python Developer"]
    assert loaded.candidate.role_categories == ["software_development"]
    assert loaded.matching.p1_threshold == 80
    assert loaded.matching.fields_to_match == ["title"]


def test_thresholds_are_loaded_and_control_priority():
    config = configuration(
        candidate={"keywords": ["Python"]},
        matching={"p1_threshold": 70, "p2_threshold": 40, "p3_threshold": 1},
    )
    job_posting = job(title="Software Engineer", description="Python and SQL")

    assert match_keywords(job_posting, config).priority == "P2"
    config.matching.p1_threshold = 40
    assert match_keywords(job_posting, config).priority == "P1"


def test_invalid_thresholds_or_ai_scoring_are_rejected():
    with pytest.raises(ValidationError, match="thresholds"):
        configuration(matching={"p1_threshold": 40, "p2_threshold": 40, "p3_threshold": 1})
    with pytest.raises(ValidationError, match="must be false"):
        configuration(matching={"use_ai_for_scoring": True})


def test_alias_target_must_be_a_configured_keyword(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        "candidate:\n  keywords: [Python]\nmatching:\n  keyword_aliases:\n    SQL: [Structured Query Language]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not in candidate.keywords"):
        load_keyword_configuration(path)


def test_custom_keyword_aliases_are_included_in_jd_extraction():
    config = configuration(
        candidate={"keywords": ["Cloud Platform"]},
        matching={"keyword_aliases": {"Cloud Platform": ["CloudOps"]}},
    )

    result = match_keywords(job("Software Engineer", "CloudOps required."), config)

    assert result.matched_keywords == ["Cloud Platform"]
    assert result.total_keyword_count == 1
    assert result.match_percentage == 100


def test_pipeline_keyword_result_is_independent_of_legacy_ai_verdict(tmp_path, caplog):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "candidate:\n"
        "  experience_level: fresher\n"
        "  max_experience_years: 1\n"
        "  preferred_roles: [Python Developer]\n"
        "  preferred_locations: [Pune]\n"
        "  employment_types: [Full-time]\n"
        "  keywords: [Python, SQL]\n"
        "matching:\n  p1_threshold: 70\n  p2_threshold: 40\n  p3_threshold: 1\n",
        encoding="utf-8",
    )
    candidate = job(
        title="Python Developer",
        description="Python SQL, 0-1 years experience, full-time role.",
        skills=["Python", "SQL"],
    )
    candidate.source = "telegram"
    candidate.source_job_id = "source-1"
    candidate.location = "Pune"
    candidate.experience = "0-1 years"
    candidate.employment_type = EmploymentType.FULL_TIME
    candidate.raw_data = {"source_channel": "jobs", "source_message_id": 1}

    class Source:
        name = "telegram"
        last_stats = {}

        def bind_state(self, _repo):
            pass

        async def collect(self):
            return [candidate]

    class Registry:
        def get_enabled_sources(self):
            return [Source()]

    class DisagreeingMatcher:
        provider_name = "test"
        model = "test-model"

        async def match(self, _job, _profile):
            return AIResponse(
                relevant=False,
                priority=Priority.P3,
                match_score=0,
                reason="test disagreement",
            )

    config = Config(profile_path=str(profile_path), verify_application_urls=False)
    repo = JobRepository(tmp_path / "keyword-pipeline.db")
    pipeline = Pipeline(config, repo, DisagreeingMatcher())
    pipeline.registry = Registry()

    with caplog.at_level(logging.DEBUG, logger="hirevia.core.pipeline"):
        asyncio.run(pipeline.run_scan())

    stored = repo.get_by_source_id("telegram", "source-1")
    keyword_result = stored.raw_data["keyword_match"]
    assert keyword_result["match_percentage"] == 100
    assert keyword_result["priority"] == "P1"
    assert stored.ai_decision["relevant"] is False
    assert stored.ai_decision["match_score"] == 0
    fields = _match_fields(stored)
    assert fields["match_score"] == stored.match_result["match_score"] == 46
    assert fields["match_priority"] == stored.match_result["priority"] == "P2"
    assert fields["matching_method"] == "yaml_keyword"
    diagnostics = "\n".join(record.message for record in caplog.records)
    assert "matched_keyword_count=2" in diagnostics
    assert "total_keyword_count=2" in diagnostics
    assert "keyword_match_percentage=100.0" in diagnostics
    assert "role_match=True" in diagnostics
    assert "location_match=True" in diagnostics
    assert "experience_compatibility=True" in diagnostics
    assert "employment_type_match=True" in diagnostics
    assert "jd_skill_coverage=100.0" in diagnostics
    assert "jd_coverage_classification=P1" in diagnostics


def test_keyword_result_persists_when_ai_matcher_is_unavailable(tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "candidate:\n  keywords: [Python, SQL]\n"
        "matching:\n  p1_threshold: 70\n  p2_threshold: 40\n  p3_threshold: 1\n",
        encoding="utf-8",
    )
    candidate = job("Python Developer", "Python SQL")
    candidate.source = "telegram"
    candidate.source_job_id = "no-ai-source"
    candidate.raw_data = {"source_channel": "jobs", "source_message_id": 2}

    class Source:
        name = "telegram"
        last_stats = {}

        def bind_state(self, _repo):
            pass

        async def collect(self):
            return [candidate]

    class Registry:
        def get_enabled_sources(self):
            return [Source()]

    repo = JobRepository(tmp_path / "no-ai-pipeline.db")
    pipeline = Pipeline(
        Config(profile_path=str(profile_path), verify_application_urls=False),
        repo,
        ai_matcher=None,
    )
    pipeline.registry = Registry()

    stats = asyncio.run(pipeline.run_scan())

    stored = repo.get_by_source_id("telegram", "no-ai-source")
    keyword_result = stored.raw_data["keyword_match"]
    assert keyword_result["match_percentage"] == 100
    assert keyword_result["priority"] == "P1"
    assert stored.match_result["assessment_status"] == "retrying"
    assert stats.jobs_failed == 1


def test_config_file_validation_accepts_nested_profile_and_rejects_bad_thresholds():
    from hirevia.web.api.routes import _validate_yaml

    assert _validate_yaml(
        "profile.yaml",
        "candidate:\n  keywords: [Python]\nmatching:\n  p1_threshold: 70\n  p2_threshold: 40\n  p3_threshold: 1\n",
    ) == []
    with pytest.raises(Exception, match="thresholds"):
        _validate_yaml(
            "profile.yaml",
            "candidate:\n  keywords: [Python]\nmatching:\n  p1_threshold: 40\n  p2_threshold: 40\n  p3_threshold: 1\n",
        )


def test_dashboard_filters_and_counts_use_final_verdict(tmp_path):
    repo = JobRepository(tmp_path / "dashboard-keywords.db")
    p1 = job(title="Python Developer", description="Python and SQL")
    p1.source_job_id = "dashboard-p1"
    p1.match_result = {"match": False, "match_score": 0, "priority": "P3"}
    p1.ai_decision = {"relevant": False, "match_score": 0, "priority": "P3"}
    p1.raw_data["keyword_match"] = {
        "matching_method": "yaml_keyword",
        "match_percentage": 100,
        "priority": "P1",
        "status": "scored",
    }
    unmatched = job(title="Mechanical Engineer", description="AutoCAD")
    unmatched.source_job_id = "dashboard-unmatched"
    unmatched.match_result = {"match": True, "match_score": 99, "priority": "P1"}
    unmatched.raw_data["keyword_match"] = {
        "matching_method": "yaml_keyword",
        "match_percentage": 0,
        "priority": "Unmatched",
        "status": "scored",
    }
    repo.upsert_job(p1)
    repo.upsert_job(unmatched)
    state = SimpleNamespace(repo=repo)

    counts = WebUIState.get_job_counts(state)
    p1_jobs = WebUIState.get_jobs(state, priority="P1")
    unmatched_jobs = WebUIState.get_jobs(state, match_status="unmatched")

    assert counts == {"ALL": 2, "P1": 1, "P2": 0, "P3": 1, "UNMATCHED": 1}
    assert [item["source_job_id"] for item in p1_jobs["jobs"]] == ["dashboard-unmatched"]
    assert [item["source_job_id"] for item in unmatched_jobs["jobs"]] == ["dashboard-p1"]