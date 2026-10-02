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
            "Python Developer",
            "Python FastAPI Flask SQL REST API Git",
        ),
        configuration(),
    )

    assert result.match_percentage == pytest.approx(6 / 8 * 100)
    assert result.priority == "P1"
    assert result.matched_keywords == ["Python", "FastAPI", "Flask", "SQL", "Git", "REST API"]
    assert result.unmatched_keywords == ["Java", "JavaScript"]
    assert result.matching_method == "yaml_keyword"


def test_partial_match_counts_actual_unique_configured_keywords():
    result = match_keywords(job("Java Developer", "Java, SQL and Git"), configuration())

    assert result.matched_keyword_count == 3
    assert result.total_keyword_count == 8
    assert result.match_percentage == pytest.approx(3 / 8 * 100)
    assert result.priority == "P3"


def test_unrelated_role_is_zero_and_unmatched():
    result = match_keywords(
        job("Mechanical Design Engineer", "AutoCAD SolidWorks CATIA"),
        configuration(),
    )

    assert result.match_percentage == 0
    assert result.priority == "Unmatched"
    assert result.matched_keywords == []
    assert result.unmatched_keywords == KEYWORDS


NINE_PERSISTED_JOB_CASES = [
    (
        "Customer Support Executive",
        "Excellent English communication, Customer Relationship Management, Email and Chat Support, "
        "Problem Resolution, MS Office, CRM Software, Customer Data Management. No programming required.",
        [],
        [],
    ),
    (
        "Graduate Mechanical Engineer",
        "AutoCAD, SolidWorks, CATIA, Mechanical Design, Manufacturing Processes, Thermodynamics, "
        "Quality Control, CNC Operations.",
        [],
        [],
    ),
    (
        "Software Tester / QA Engineer",
        "Manual Testing, Automation Testing, Selenium WebDriver, Python, Test Cases, Bug Reporting, "
        "API Testing, Postman.",
        ["Python", "Selenium"],
        ["Python", "Selenium", "Testing"],
    ),
    (
        "Java Developer",
        "Core Java, OOP, Collections Framework, Exception Handling, JDBC, Spring Boot basics, "
        "MySQL, Data Structures.",
        ["MySQL", "Java"],
        ["Java", "MySQL"],
    ),
    (
        "Job Opening: Frontend Developer",
        "HTML5, CSS3, JavaScript, React.js, Next.js, Responsive Web Design, REST API Integration, Git.",
        ["Git", "REST API", "JavaScript", "React"],
        ["JavaScript", "React", "REST API", "Git"],
    ),
    (
        "Junior Data Analyst",
        "SQL, Advanced Excel, Power BI, Data Visualization, Dashboard Development, Data Cleaning, "
        "Reporting and Analytics.",
        ["SQL", "Power BI"],
        ["SQL", "Power BI", "Excel"],
    ),
    (
        "AI/ML Engineer",
        "Python, Machine Learning, Deep Learning, TensorFlow / Keras, NumPy and Pandas, "
        "Scikit-learn, model training, basic NLP.",
        ["Python", "Machine Learning", "Deep Learning", "TensorFlow", "Scikit-learn", "Pandas", "NumPy", "NLP"],
        ["Python", "Machine Learning", "Deep Learning", "Pandas", "NumPy", "TensorFlow"],
    ),
    (
        "Backend Developer",
        "Python, FastAPI, REST API development, SQL and PostgreSQL, database design, Git, Docker.",
        ["Python", "FastAPI", "SQL", "PostgreSQL", "Docker", "Git", "REST API"],
        ["Python", "FastAPI", "SQL", "PostgreSQL", "REST API", "Git", "Docker"],
    ),
    (
        "Python Developer (Fresher)",
        "Python programming, basic SQL and MySQL, Flask or Django, REST API fundamentals, Git and GitHub.",
        ["Python", "Flask", "Django", "SQL", "MySQL", "Git", "REST API"],
        ["Python", "Flask", "Django", "SQL", "MySQL", "REST API", "Git"],
    ),
]


@pytest.mark.parametrize(
    "title,description,skills,expected_matched",
    NINE_PERSISTED_JOB_CASES,
    ids=[case[0] for case in NINE_PERSISTED_JOB_CASES],
)
def test_all_nine_dashboard_job_samples_score_from_configured_keywords(
    title, description, skills, expected_matched
):
    profile_path = Path(__file__).resolve().parent.parent / "profile.yaml"
    result = match_keywords(
        job(title=title, description=description, skills=skills),
        load_keyword_configuration(profile_path),
    )

    assert result.total_keyword_count == 30
    assert result.matched_keywords == expected_matched
    assert result.priority == ("Unmatched" if not result.matched_keywords else "P3")
    assert result.match_percentage == pytest.approx(
        len(result.matched_keywords) / 30 * 100
    )


def test_repeated_keyword_counts_once():
    result = match_keywords(job(description=" ".join(["Python"] * 15)), configuration())

    assert result.matched_keywords == ["Python"]
    assert result.matched_keyword_count == 1


def test_case_and_punctuation_variants_match_canonically():
    config = configuration(
        candidate={"keywords": ["Python", "PostgreSQL", "REST API", "Machine Learning"]},
        matching={
            "keyword_aliases": {
                "PostgreSQL": ["Postgres"],
                "REST API": ["RESTful APIs"],
                "Machine Learning": ["ML models"],
            }
        },
    )
    result = match_keywords(
        job("PYTHON", "postgres RESTful APIs machine-learning"), config
    )

    assert result.match_percentage == 100
    assert result.matched_keywords == ["Python", "PostgreSQL", "REST API", "Machine Learning"]
    assert result.total_keyword_count == 4


def test_java_does_not_match_javascript():
    config = configuration(candidate={"keywords": ["Java"]})
    result = match_keywords(job(description="JavaScript developer"), config)

    assert result.match_percentage == 0
    assert result.priority == "Unmatched"


def test_missing_fields_only_match_available_title():
    config = configuration(candidate={"keywords": ["Python", "SQL"]})
    result = match_keywords(job(title="Python Developer"), config)

    assert result.match_percentage == 50
    assert result.matched_keywords == ["Python"]
    assert result.unmatched_keywords == ["SQL"]


def test_empty_keyword_list_is_unscored_not_unmatched():
    result = match_keywords(job(title="Python Developer"), configuration(candidate={"keywords": []}))

    assert result.match_percentage is None
    assert result.priority is None
    assert result.status == "unscored"
    assert result.total_keyword_count == 0


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
        candidate={"keywords": ["Python", "SQL"]},
        matching={"p1_threshold": 50, "p2_threshold": 25, "p3_threshold": 1},
    )

    assert match_keywords(job(title="Python"), config).priority == "P1"
    assert match_keywords(job(title="Python"), configuration(
        candidate={"keywords": ["Python", "SQL"]},
        matching={"p1_threshold": 80, "p2_threshold": 40, "p3_threshold": 1},
    )).priority == "P2"


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
    assert fields["match_score"] == 100
    assert fields["match_priority"] == "P1"
    assert fields["matching_method"] == "yaml_keyword"
    diagnostics = "\n".join(record.message for record in caplog.records)
    assert "matched_keyword_count=2" in diagnostics
    assert "total_keyword_count=2" in diagnostics
    assert "keyword_match_percentage=100.0" in diagnostics
    assert "role_match=True" in diagnostics
    assert "location_match=True" in diagnostics
    assert "experience_compatibility=True" in diagnostics
    assert "employment_type_match=True" in diagnostics
    assert "final_score=100.0" in diagnostics
    assert "final_classification=P1" in diagnostics


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


def test_dashboard_filters_and_counts_use_keyword_priority(tmp_path):
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

    assert counts == {"ALL": 2, "P1": 1, "P2": 0, "P3": 0, "UNMATCHED": 1}
    assert [item["source_job_id"] for item in p1_jobs["jobs"]] == ["dashboard-p1"]
    assert [item["source_job_id"] for item in unmatched_jobs["jobs"]] == ["dashboard-unmatched"]