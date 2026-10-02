"""Tests for the hybrid profile matching engine (Phase 7).

`analyze()` is the deterministic half (pure, no network); `combine()` is where
deterministic evidence and the AI verdict are merged into one MatchResult.

The tests here pin the scoring CONTRACT, not incidental numbers:
  * a dimension the candidate never expressed an opinion on scores NEUTRAL,
    never 0 - silence is not a rejection;
  * a stated veto outranks any score, and the model cannot talk past it;
  * the AI contributes a bounded share of the final score, so an
    over-confident model can never manufacture relevance on its own;
    * final tiers follow the configured score thresholds, while hard vetoes win.
"""

import pytest
from pydantic import ValidationError

from hirevia.config import Config
from hirevia.core.matching import MatchEvidence, WEIGHTS, analyze, combine, match_job, parse_experience
from hirevia.core.models import (
    AIResponse,
    CandidateProfile,
    EmploymentType,
    Job,
    MatchResult,
    Priority,
)
from hirevia.core.skills import skill_overlap


# --- helpers ------------------------------------------------------------------


def test_priority_threshold_defaults_and_order_validation():
    config = Config()

    assert config.priority_p1_min_score == 70
    assert config.priority_p2_min_score == 40
    assert config.priority_p3_min_score == 1
    with pytest.raises(ValidationError):
        Config(priority_p1_min_score=60, priority_p2_min_score=70, priority_p3_min_score=40)
    with pytest.raises(ValidationError):
        Config(priority_p1_min_score=85, priority_p2_min_score=65, priority_p3_min_score=65)


def make_profile(**overrides) -> CandidateProfile:
    defaults = dict(
        name="Test User",
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        accept_fresher=True,
        skills=["Python", "FastAPI", "SQL", "Machine Learning"],
        preferred_roles=["Python Developer", "Backend Developer"],
        preferred_locations=["Pune"],
        remote_preference="preferred",
        employment_types=["full_time", "internship"],
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def make_job(**overrides) -> Job:
    defaults = dict(
        source="test",
        source_job_id="1",
        title="Python Developer",
        company="Acme Corp",
        location="Pune, Maharashtra",
        description="Python and FastAPI role. SQL queries. Fresher friendly, 0-1 years.",
        url="https://example.com/job/1",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_weights_sum_to_one_hundred():
    """The weight table is a budget: it must keep summing to 100."""
    assert sum(WEIGHTS.values()) == 100


def test_skill_overlap_uses_jd_skills_as_the_denominator():
    jd_skills = ["Python", "FastAPI", "Flask", "SQL", "REST API", "Postgres", "Docker", "Git", "Redis", "GraphQL"]
    candidate_skills = jd_skills[:8] + [f"unrelated {index}" for index in range(100)]

    assert skill_overlap(jd_skills, candidate_skills) == 0.8


# --- neutral, not rejected ----------------------------------------------------


def test_missing_experience_is_neutral_not_a_rejection():
    """A posting that never states experience must not be penalised."""
    profile = make_profile()
    job = make_job(description="Python developer role. Nothing about years here.")
    evidence = analyze(job, profile)

    assert evidence.experience_match is None          # tri-state: no evidence
    assert evidence.reject_reasons == []              # silence != rejection
    assert "experience" in evidence.signals


def test_missing_location_is_neutral_not_a_rejection():
    profile = make_profile()
    job = make_job(location="Not specified", description="Python FastAPI, 0-1 years.")
    evidence = analyze(job, profile)

    assert evidence.location_match is None
    assert evidence.reject_reasons == []


def test_empty_profile_scores_neutral_everywhere():
    """Unstated preferences stay neutral; stated JD skills are uncovered."""
    profile = CandidateProfile()
    evidence = analyze(make_job(), profile)

    for name in ("skills", "role", "location", "employment", "company"):
        if name == "skills":
            assert evidence.signals[name]["score"] == 0
            assert evidence.jd_skill_coverage == 0
            continue
        assert evidence.signals[name]["score"] == pytest.approx(
            WEIGHTS[name] * 0.6, abs=0.01
        ), f"{name} should be neutral, not zero"
    assert evidence.location_match is None
    assert not evidence.hard_reject


# --- skills -------------------------------------------------------------------


def test_strong_skill_match_scores_well():
    profile = make_profile()
    job = make_job(description="Python, FastAPI, and SQL. 0-1 years.")
    evidence = analyze(job, profile)

    assert evidence.matched_skills == ["Python", "FastAPI", "SQL"]
    assert evidence.jd_skill_coverage == 100
    assert evidence.signals["skills"]["score"] == pytest.approx(WEIGHTS["skills"], abs=1.0)
    assert evidence.missing_skills == []


def test_python_backend_reports_redis_missing_and_mongodb_transferable_only():
    profile = make_profile(skills=[
        "Python", "FastAPI", "Flask", "REST API", "SQL", "MySQL",
        "Git", "Docker", "PostgreSQL", "MongoDB",
    ])
    job = make_job(
        title="TEST JOB P1 - Python Backend Developer",
        description=(
            "Required Skills: Python, FastAPI, Flask, REST API, SQL, MySQL, "
            "Git, Docker, PostgreSQL, Redis."
        ),
    )
    evidence = analyze(job, profile)

    assert evidence.jd_skill_total_count == 10
    assert evidence.jd_skill_matched_count == 9
    assert evidence.jd_skill_coverage == 90
    assert evidence.missing_skills == ["Redis"]
    assert "Redis" not in evidence.matched_skills
    assert ("Redis", "MongoDB") in evidence.transferable


def test_weak_skill_overlap_scores_low():
    profile = make_profile()
    job = make_job(title="Sales Executive",
                   description="Java, Kubernetes, and AWS required.")
    evidence = analyze(job, profile)

    assert evidence.matched_skills == []
    assert evidence.signals["skills"]["score"] < WEIGHTS["skills"] * 0.4


def test_skill_aliases_normalise_to_the_canonical_name():
    """'ML' in a posting must match a profile asking for Machine Learning."""
    profile = make_profile(skills=["Machine Learning"])
    job = make_job(title="AI Engineer", description="Work on ML models. 0-1 years.")
    evidence = analyze(job, profile)

    assert evidence.matched_skills == ["Machine Learning"]


def test_nlp_alias_resolves_to_canonical_name():
    profile = make_profile(skills=["Natural Language Processing"])
    job = make_job(title="AI Engineer", description="NLP research work. 0-1 years.")
    evidence = analyze(job, profile)

    assert evidence.matched_skills == ["Natural Language Processing"]


def test_unrelated_skill_is_not_matched_by_shared_substring():
    """'R' must never match 'React'; 'Java' must not match 'JavaScript'."""
    profile = make_profile(skills=["React"])
    job = make_job(description="We need Rust and JavaScript. 0-1 years.")
    evidence = analyze(job, profile)

    assert "React" not in evidence.matched_skills


# --- role families ------------------------------------------------------------


def test_targeted_role_scores_full_credit():
    profile = make_profile()
    evidence = analyze(make_job(title="Backend Developer"), profile)

    assert evidence.role_match is True
    assert evidence.signals["role"]["score"] == WEIGHTS["role"]


def test_unrelated_role_scores_low_and_is_flagged():
    profile = make_profile()
    evidence = analyze(make_job(title="Sales Executive"), profile)

    assert evidence.role_match is False
    assert evidence.signals["role"]["score"] < WEIGHTS["role"] * 0.3


def test_single_shared_keyword_is_not_enough_to_match_a_role():
    """'Data Analyst' must not pass as a Python/backend role on one word."""
    profile = make_profile()
    job = make_job(title="Data Analyst",
                   description="Excel, dashboards and SQL reporting. 0-1 years.")
    evidence = analyze(job, profile)

    # The role itself is outside the target set, and the posting shares no
    # real technical evidence with the profile beyond the word 'SQL'.
    assert evidence.role_match is not True
    assert evidence.signals["skills"]["score"] < WEIGHTS["skills"]


def test_role_family_catches_related_titles():
    profile = make_profile(role_families=["python_backend"])
    evidence = analyze(make_job(title="Python Backend Engineer"), profile)

    assert evidence.role_match is True
    assert 0 < evidence.signals["role"]["score"] < WEIGHTS["role"]


# --- experience ---------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Fresher role, 0-1 years", (0, 1)),
    ("1-2 years experience", (1, 2)),
    ("5+ years of experience", (5, 6)),
    ("Nothing relevant here", None),
])
def test_parse_experience_ranges(text, expected):
    assert parse_experience(text) == expected


def test_senior_role_is_a_hard_reject_for_a_fresher():
    profile = make_profile(experience_years=0, target_max_experience_years=2)
    evidence = analyze(make_job(description="Python FastAPI. Requires 7+ years."), profile)

    assert evidence.hard_reject
    assert any("years" in reason for reason in evidence.reject_reasons)


def test_fresher_role_is_accepted():
    profile = make_profile()
    evidence = analyze(make_job(description="Python FastAPI. Fresher welcome, 0-1 years."), profile)

    assert evidence.experience_match is True
    assert not evidence.hard_reject


# --- location -----------------------------------------------------------------


def test_preferred_city_is_a_strong_signal():
    profile = make_profile(preferred_locations=["Pune"])
    evidence = analyze(make_job(location="Pune, Maharashtra"), profile)

    assert evidence.location_match is True
    assert evidence.signals["location"]["score"] == WEIGHTS["location"]


def test_different_city_scores_much_lower():
    profile = make_profile(preferred_locations=["Pune"])
    evidence = analyze(make_job(location="Mumbai, Maharashtra"), profile)

    assert evidence.location_match is False
    assert evidence.signals["location"]["score"] < WEIGHTS["location"] * 0.2


def test_ai_location_claim_does_not_override_deterministic_location_evidence():
    evidence = analyze(make_job(location="Mumbai"), make_profile(preferred_locations=["Pune"]))
    ai = AIResponse(
        relevant=True, priority=Priority.P2, match_score=70, location_match=True
    )

    verdict = combine(evidence, None, ai)

    assert evidence.location_match is False
    assert verdict.location_match is False


def test_remote_is_a_strong_signal_when_preferred():
    profile = make_profile(preferred_locations=["Pune"], remote_preference="preferred")
    evidence = analyze(make_job(location="Remote"), profile)

    assert evidence.remote_mode == "remote"
    assert evidence.signals["remote"]["score"] == WEIGHTS["remote"]


# --- combine(): the hybrid contract -------------------------------------------


def test_ai_contributes_only_a_bounded_share():
    """final = det*(1-w) + ai*w, with w <= 1 - the AI cannot take over."""
    profile = make_profile()
    evidence = analyze(make_job(), profile)
    config = type("Cfg", (), {"ai_match_weight": 0.4, "match_min_score": 55,
                              "priority_p1_min_score": 80, "priority_p2_min_score": 62,
                              "ai_failed_score_cap": 70})()
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="great")

    result = combine(evidence, config, ai)

    assert result.ai_status == "ok" and result.ai_score == 100
    assert result.deterministic_score < result.match_score <= 100
    expected = round(evidence.score * 0.6 + 100 * 0.4)
    assert result.match_score == expected


def test_strong_deterministic_match_is_reduced_by_a_weak_ai_verdict():
    evidence = analyze(make_job(), make_profile())
    ai = AIResponse(relevant=False, priority=Priority.P3, match_score=20, reason="weak fit")

    result = combine(evidence, None, ai)

    expected = round(evidence.score * 0.6 + 20 * 0.4) - 12
    assert evidence.score >= 80
    assert result.match_score == expected
    assert result.match is False
    assert result.priority is Priority.P2


def test_weak_deterministic_match_is_not_rescued_by_a_strong_ai_verdict():
    evidence = analyze(
        make_job(title="Sales Executive", location="Mumbai", description="Sales role."),
        make_profile(),
    )
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="strong fit")

    evidence = analyze(
        make_job(title="Sales Executive", location="Mumbai", description="Java Kubernetes AWS"),
        make_profile(),
    )
    result = combine(evidence, None, ai)

    assert evidence.score < 30
    assert result.match_score == round(evidence.score * 0.6 + 100 * 0.4)
    assert result.match is False
    assert result.priority is Priority.P2


@pytest.mark.parametrize(
    "job_overrides",
    [
        {"description": "Python FastAPI. Requires 7+ years of experience."},
        {"title": "Senior Python Developer"},
    ],
    ids=["experience", "seniority"],
)
def test_hard_veto_overrides_a_perfect_ai_verdict(job_overrides):
    evidence = analyze(make_job(**job_overrides), make_profile())
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="perfect fit")

    result = combine(evidence, None, ai)

    assert result.hard_reject is True
    assert result.reject_reasons == evidence.reject_reasons
    assert result.match is False
    assert result.match_score <= 40
    assert result.priority is Priority.P3


def test_deterministic_and_hybrid_scores_are_bounded():
    evidence = analyze(make_job(), make_profile())
    evidence.signals["external"] = {"score": 150, "note": "synthetic oversized signal"}
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="strong fit")

    result = combine(evidence, None, ai)

    assert evidence.score == 100
    assert result.deterministic_score == 100
    assert result.ai_score == 100
    assert 0 <= result.match_score <= 100


@pytest.mark.parametrize(
    "score_fields",
    [{"ai_score": 101}, {"ai_score": -1}, {"deterministic_score": 101},
     {"deterministic_score": -1}],
)
def test_match_result_rejects_out_of_range_source_scores(score_fields):
    with pytest.raises(ValidationError):
        MatchResult(**score_fields)


def test_missing_skills_remain_visible_without_becoming_a_hard_veto():
    profile = make_profile(skills=["Python", "FastAPI", "SQL"])
    profile = make_profile(skills=["Python"])
    evidence = analyze(make_job(description="Python, FastAPI and SQL role. 0-1 years."), profile)

    result = combine(evidence, None, AIResponse(
        relevant=True, priority=Priority.P2, match_score=70, reason="partial fit"
    ))

    assert "FastAPI" in result.missing_skills
    assert "SQL" in result.missing_skills
    assert result.hard_reject is False


def test_over_confident_model_cannot_manufacture_relevance():
    """Weak evidence + perfect model still cannot reach a top-tier score."""
    profile = CandidateProfile()          # nothing configured => neutral evidence
    evidence = analyze(make_job(), profile)
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="looks great")

    result = combine(evidence, None, ai)

    assert result.match_score < 100
    assert result.priority is not Priority.P1 or result.match_score < 80


def test_ai_priority_does_not_override_the_shared_score_classification():
    """A model-provided tier cannot contradict the configured score bucket."""
    profile = CandidateProfile()
    evidence = analyze(make_job(), profile)
    config = type("Cfg", (), {"ai_match_weight": 1.0, "match_min_score": 55,
                              "priority_p1_min_score": 80, "priority_p2_min_score": 62,
                              "ai_failed_score_cap": 70})()
    ai = AIResponse(relevant=True, priority=Priority.P3, match_score=95, reason="borderline")

    result = combine(evidence, config, ai)

    assert result.match_score == 95       # pure AI score at weight 1.0
    assert result.priority is Priority.P1


def test_final_priority_uses_blended_score_not_ai_priority():
    evidence = MatchEvidence()
    evidence.signals["skills"] = {"weight": 45, "score": 99, "note": "fixture"}
    config = type("Cfg", (), {
        "ai_match_weight": 0.4,
        "match_min_score": 55,
        "priority_p1_min_score": 85,
        "priority_p2_min_score": 65,
        "priority_p3_min_score": 40,
    })()
    ai = AIResponse(
        relevant=True, priority=Priority.P2, match_score=90, reason="good fit"
    )

    result = combine(evidence, config, ai)

    assert result.deterministic_score == 99
    assert result.ai_score == 90
    assert result.ai_priority is Priority.P2
    assert result.match_score == 95
    assert result.priority is Priority.P1


def test_hard_reject_outranks_the_ai():
    """A stated veto beats even a perfect, relevant AI verdict."""
    profile = make_profile(experience_years=0, target_max_experience_years=2)
    evidence = analyze(make_job(title="Sales Executive",
                                description="Python FastAPI. Requires 7+ years."), profile)
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=100, reason="perfect")

    result = combine(evidence, None, ai)

    assert result.match is False
    assert result.priority is Priority.P3
    assert result.match_score <= 40
    assert "rejected" in result.reason.lower()


def test_ai_failure_is_scoreless_and_flagged_degraded():
    """Provider down => preserve evidence without inventing a final decision."""
    profile = make_profile()
    evidence = analyze(make_job(), profile)
    ai = AIResponse.failure(RuntimeError("model down"))

    result = combine(evidence, None, ai)

    assert result.ai_status == "failed"
    assert result.degraded
    assert result.match_score is None
    assert result.match is None
    assert result.priority is not Priority.P1
    assert "AI unavailable" in result.reason


def test_ai_pending_has_no_final_match_or_score():
    profile = make_profile()
    evidence = analyze(make_job(), profile)

    result = combine(evidence, None, None)

    assert result.ai_status == "skipped"
    assert result.assessment_status == "pending"
    assert result.match_score is None
    assert result.match is None
    assert result.priority is not Priority.P1


def test_model_cannot_invent_a_skill_the_posting_never_mentions():
    """Deterministic skills are the source of truth for what 'matched' means."""
    profile = make_profile()
    evidence = analyze(make_job(description="Python and SQL only. 0-1 years."), profile)
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=80, reason="ok",
                    matched_skills=["Kubernetes", "FastAPI"])

    result = combine(evidence, None, ai)

    # Kubernetes is absent from the posting text, so the claim is discarded.
    assert "Kubernetes" not in result.matched_skills


def test_match_result_bridges_to_the_legacy_ai_response():
    profile = make_profile()
    result = match_job(make_job(), profile)

    bridged = result.to_ai_response()
    assert isinstance(bridged, AIResponse)
    assert bridged.match_score == result.match_score
    assert bridged.priority is result.priority
    assert bridged.relevant is result.match


def test_priority_thresholds_are_configurable():
    profile = make_profile()
    evidence = analyze(make_job(), profile)
    ai = AIResponse(relevant=True, priority=Priority.P1, match_score=90, reason="ok")
    lenient = type("Cfg", (), {"ai_match_weight": 1.0, "match_min_score": 55,
                               "priority_p1_min_score": 85, "priority_p2_min_score": 60,
                               "ai_failed_score_cap": 70})()

    assert combine(evidence, lenient, ai).priority is Priority.P1


# --- education (informational, never scored) ---------------------------------


def test_education_signal_is_informational_only():
    """Education never moves the score; it is context on the verdict."""
    profile = make_profile(degree="B.Tech")
    without = analyze(make_job(), profile)
    with_degree = analyze(
        make_job(description="Python FastAPI SQL. B.Tech required. 0-1 years."), profile
    )

    assert with_degree.signals.keys() == without.signals.keys()
    assert with_degree.education_match is True
    assert with_degree.score == pytest.approx(without.score, abs=0.01)


# --- employment ---------------------------------------------------------------


def test_interview_internship_is_accepted_when_preference_is_set():
    profile = make_profile(internship_preference=True)
    evidence = analyze(make_job(employment_type=EmploymentType.INTERNSHIP), profile)

    assert evidence.employment_match is True
    assert evidence.signals["employment"]["score"] == pytest.approx(WEIGHTS["employment"])


def test_undeclared_employment_type_is_neutral():
    profile = make_profile()
    evidence = analyze(make_job(employment_type=None), profile)

    assert evidence.employment_match is None
    assert not evidence.hard_reject


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
