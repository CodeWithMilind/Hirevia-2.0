"""Deterministic matching signals (Phase 7, Stage 3).

Covers each signal in isolation - role, role families, skills, aliases,
experience/fresher, seniority, location, remote, employment, company - so a
regression points at one signal instead of "the score changed".

Two invariants are asserted throughout:

  * BOUNDED - every signal stays within its weight and the total never leaves
    0-100, so no combination of postings can manufacture an impossible score;
  * SILENCE IS NOT REJECTION - a dimension the candidate never expressed an
    opinion about scores NEUTRAL, never zero, and never vetoes.

No AI is involved anywhere in this file: `analyze()` is pure and offline.
"""

import pytest

from hirevia.core.matching import (
    NEUTRAL,
    SENIORITY_RANK,
    WEIGHTS,
    analyze,
    is_soft_experience,
    job_remote_mode,
    job_seniority,
    parse_experience,
    seniority_ceiling,
    transferable_matches,
)
from hirevia.core.models import CandidateProfile, EmploymentType, Job
from hirevia.core.skills import (
    SKILL_ALIASES,
    SKILL_FAMILIES,
    find_skills,
    normalize_skill,
    related_skill,
    same_skill,
    skill_in_text,
)

PYTHON_PROFILE = {
    "name": "Python Dev",
    "candidate_type": "fresher",
    "experience_years": 0,
    "target_max_experience_years": 2,
    "accept_fresher": True,
    "skills": ["Python", "FastAPI", "SQL", "PostgreSQL", "Machine Learning"],
    "preferred_roles": ["Python Developer", "Backend Developer"],
    "role_families": ["python_backend"],
    "preferred_locations": ["Pune"],
    "remote_preference": "preferred",
    "employment_types": ["full_time", "internship"],
}


def profile(**overrides) -> CandidateProfile:
    return CandidateProfile(**{**PYTHON_PROFILE, **overrides})


def job(title="Python Developer", description="Python and FastAPI with SQL.",
        location="Pune", company="Acme Corp", **overrides) -> Job:
    return Job(
        source="telegram", source_job_id="1", title=title, company=company,
        location=location, description=description,
        url="https://example.com/job/1", **overrides,
    )


# ===========================================================================
# The weighting contract
# ===========================================================================

def test_weight_table_is_a_budget_of_exactly_100():
    assert sum(WEIGHTS.values()) == 100
    assert set(WEIGHTS) == {"skills", "role", "location", "experience",
                            "remote", "employment", "company"}


def test_every_signal_stays_inside_its_own_weight():
    """A single signal can never contribute more than its budget."""
    evidence = analyze(job(description="Python FastAPI SQL PostgreSQL Machine Learning. 0-1 years."),
                       profile())
    for name, signal in evidence.signals.items():
        assert 0 <= signal["score"] <= WEIGHTS[name], name


@pytest.mark.parametrize("title,description,location", [
    ("Python Developer", "Python FastAPI SQL PostgreSQL machine learning. 0-1 years.", "Pune"),
    ("Data Analyst", "Excel dashboards and reporting.", "Mumbai, India"),
    ("Sales Executive", "Cold outreach and targets.", "Remote"),
    ("", "", ""),
    ("Python Developer", "x" * 4000, "Pune"),
])
def test_total_score_stays_inside_0_100(title, description, location):
    evidence = analyze(job(title=title, description=description, location=location), profile())
    assert 0 <= evidence.score <= 100


def test_signals_carry_an_explainable_note():
    """Every scored dimension must say why it scored what it scored."""
    evidence = analyze(job(), profile())
    for name, signal in evidence.signals.items():
        assert signal["note"], f"{name} has no explanation"
        assert isinstance(signal["weight"], int) and isinstance(signal["score"], float)


# ===========================================================================
# Strong matches
# ===========================================================================

def test_strong_python_backend_match():
    evidence = analyze(
        job(title="Backend Developer",
            description="Build services with Python, FastAPI and PostgreSQL. SQL. 0-1 years."),
        profile(),
    )

    assert evidence.score >= 80
    assert evidence.role_match is True
    assert set(evidence.matched_skills) >= {"Python", "FastAPI", "PostgreSQL"}
    assert evidence.experience_match is True
    assert evidence.location_match is True
    assert not evidence.hard_reject


def test_ai_ml_match():
    p = profile(skills=["Python", "Machine Learning", "Deep Learning", "TensorFlow", "Scikit-learn"],
                preferred_roles=["AI Engineer", "ML Engineer"],
                role_families=["ai_ml"])
    evidence = analyze(
        job(title="AI Engineer",
            description="Train machine learning and deep learning models with TensorFlow and "
                        "scikit-learn in Python. Fresher-friendly, 0-1 years."),
        p,
    )

    assert evidence.score >= 80
    assert evidence.role_match is True
    assert set(evidence.matched_skills) >= {"Machine Learning", "Deep Learning", "TensorFlow"}
    assert not evidence.hard_reject


def test_data_science_match():
    p = profile(skills=["Python", "Pandas", "NumPy", "Statistics", "SQL", "Data Analysis"],
                preferred_roles=["Data Scientist"],
                role_families=["data"])
    evidence = analyze(
        job(title="Data Scientist",
            description="Statistical analysis and data analysis with Pandas, NumPy and SQL. 0-2 years."),
        p,
    )

    assert evidence.role_match is True
    assert set(evidence.matched_skills) >= {"Pandas", "NumPy", "Statistics", "SQL"}
    assert evidence.score >= 75


def test_transferable_evidence_earns_partial_credit_not_a_match():
    """FastAPI and Django are different tools in the same family."""
    p = profile(skills=["FastAPI"])
    evidence = analyze(job(title="Python Developer",
                           description="You will use Django and Python. 0-1 years."), p)

    assert "FastAPI" not in evidence.matched_skills     # never reported as a match
    assert "FastAPI" in evidence.missing_skills
    assert evidence.transferable, "a same-family substitute should be recorded"
    assert "Django" in {offered for _, offered in evidence.transferable}
    # Partial credit only: below a real hit, above nothing.
    assert 0 < evidence.signals["skills"]["score"] < WEIGHTS["skills"]


# ===========================================================================
# Unrelated roles
# ===========================================================================

def test_unrelated_role_scores_low_and_never_matches_the_target():
    evidence = analyze(job(title="Sales Executive",
                           description="Cold outreach, pipeline and targets."), profile())

    assert evidence.role_match is False
    assert evidence.matched_skills == []
    assert evidence.score < 40


def test_one_shared_keyword_does_not_make_an_unrelated_role_a_match():
    """"Data Analyst" is not a backend role just because 'data' appears."""
    p = profile(preferred_roles=["Python Developer"], role_families=[])
    evidence = analyze(job(title="Data Analyst",
                           description="Reporting and dashboards. 0-1 years."), p)

    assert evidence.role_match is not True
    assert evidence.score < 60


def test_role_family_grants_credit_without_claiming_an_exact_match():
    evidence = analyze(job(title="Python Backend Engineer",
                           description="Python and FastAPI. 0-1 years."), profile())

    assert evidence.role_match is True
    assert 0.5 * WEIGHTS["role"] < evidence.signals["role"]["score"] < WEIGHTS["role"]


# ===========================================================================
# Fresher / senior / experience
# ===========================================================================

@pytest.mark.parametrize("text", [
    "Fresher role, 0-1 years experience.",
    "Freshers welcome, no prior experience needed.",
    "Entry-level position for recent graduates.",
    "Campus hiring for the 2026 batch.",
    "0-2 years experience required.",
])
def test_fresher_and_graduate_signals_are_accepted(text):
    evidence = analyze(job(title="Python Developer", description=f"Python and SQL. {text}"), profile())

    assert evidence.experience_match is True
    assert not evidence.hard_reject
    assert evidence.signals["experience"]["score"] >= 0.8 * WEIGHTS["experience"]


def test_graduate_trainee_and_intern_are_fresher_friendly():
    evidence = analyze(job(title="Python Intern", description="Python and SQL internship."), profile())
    assert evidence.experience_match is True
    assert not evidence.hard_reject


@pytest.mark.parametrize("title", [
    "Senior Python Developer", "Lead Backend Developer", "Staff AI Engineer",
    "Principal Engineer", "Python Developer (Manager)", "Head of Engineering",
    "Python Developer - Team Lead",
])
def test_senior_titles_are_a_hard_reject_for_a_fresher(title):
    """filters.yaml used to be the only thing catching these, and it is optional."""
    evidence = analyze(job(title=title, description="Python, FastAPI and SQL. 0-1 years."), profile())

    assert evidence.hard_reject, f"{title} should be vetoed for a fresher"
    assert any("role" in reason for reason in evidence.reject_reasons)


def test_lead_word_matches_whole_words_only():
    """"leading a team" / "leadership" must not read as a Lead role."""
    assert job_seniority("Backend Developer (leading a small team)") is None
    assert job_seniority("Team Lead Backend Developer") == "lead"
    assert job_seniority("Backend Developer, leadership skills required") is None


def test_senior_role_is_still_fine_for_an_experienced_candidate():
    p = profile(candidate_type="experienced", experience_years=7, max_seniority="senior")
    evidence = analyze(job(title="Senior Python Developer",
                           description="Python, FastAPI and SQL. 5+ years."), p)

    assert not evidence.hard_reject
    assert evidence.role_match is True


def test_explicit_max_seniority_overrides_the_derived_ceiling():
    p = profile(candidate_type="fresher", experience_years=0, max_seniority="senior")
    assert seniority_ceiling(p) == SENIORITY_RANK["senior"]

    q = profile(candidate_type="fresher", experience_years=0)
    assert seniority_ceiling(q) == SENIORITY_RANK["junior"]


def test_junior_title_is_not_penalised():
    evidence = analyze(job(title="Junior Python Developer",
                           description="Python, FastAPI and SQL. 0-1 years."), profile())
    assert not evidence.hard_reject
    assert evidence.signals["role"]["score"] == pytest.approx(WEIGHTS["role"])


@pytest.mark.parametrize("text", [
    "Python and SQL role. Nothing about years here.",
    "Python and SQL role. We are building things.",
])
def test_missing_experience_is_neutral_not_a_rejection(text):
    evidence = analyze(job(title="Python Developer", description=text), profile())

    assert evidence.experience_match is None
    assert not evidence.hard_reject
    assert evidence.signals["experience"]["score"] == pytest.approx(NEUTRAL * WEIGHTS["experience"])


@pytest.mark.parametrize("text", [
    "This company has been hiring for 10 years.",
    "Founded 3 years ago, raised a seed round last year.",
    "Celebrating 20 years in business.",
])
def test_company_age_is_not_read_as_a_job_requirement(text):
    """These used to hard-reject a fresher against a perfect job."""
    evidence = analyze(job(title="Python Developer",
                           description=f"Python, FastAPI and SQL. {text}"), profile())

    assert not evidence.hard_reject
    assert parse_experience(text) is None


def test_a_real_requirement_is_still_enforced():
    evidence = analyze(job(title="Senior Python Developer",
                           description="Requires 7+ years of experience."), profile())
    assert evidence.hard_reject


def test_ten_years_is_not_mistaken_for_a_fresher_role():
    """"0 years" used to match inside "10 years", inverting seniority."""
    assert parse_experience("Requires 10+ years of experience.") == (10, 11)
    assert parse_experience("Requires 20 years of experience.") == (20, 21)


def test_soft_experience_is_acceptable_but_scores_lower():
    """'Preferred but not mandatory' is a preference, not a hard bar.

    A 1-year candidate asking 2-3 years is still a realistic near-miss when the
    range is soft, and a genuine miss when it is strict.
    """
    p = profile(experience_years=1, target_max_experience_years=2)

    soft = analyze(job(description="Python and SQL. 2-3 years, experience preferred but not mandatory."), p)
    strict = analyze(job(description="Python and SQL. 2-3 years of experience required."), p)

    assert is_soft_experience("experience preferred but not mandatory")
    assert soft.experience_match is True
    assert not soft.hard_reject
    assert soft.signals["experience"]["score"] > strict.signals["experience"]["score"]


def test_soft_experience_still_does_not_license_a_far_out_of_range_role():
    p = profile(experience_years=1, target_max_experience_years=2)
    evidence = analyze(job(description="Python and SQL. 7+ years of experience preferred."), p)

    assert evidence.hard_reject
    assert evidence.signals["experience"]["score"] == 0


def test_experience_far_beyond_the_ceiling_is_a_reject():
    evidence = analyze(job(title="Python Developer",
                           description="Python and SQL. Requires 7+ years of experience."), profile())
    assert evidence.hard_reject
    assert any("7+" in reason for reason in evidence.reject_reasons)


# ===========================================================================
# Location
# ===========================================================================

def test_exact_preferred_city_is_a_full_signal():
    evidence = analyze(job(location="Pune, Maharashtra"), profile())
    assert evidence.location_match is True
    assert evidence.signals["location"]["score"] == pytest.approx(WEIGHTS["location"])


def test_city_alias_is_still_an_exact_match():
    p = profile(preferred_locations=["Bengaluru"])
    assert analyze(job(location="Bangalore, Karnataka"), p).location_match is True


@pytest.mark.parametrize("location", ["Mumbai, India", "Bangalore, Karnataka, India"])
def test_a_different_city_beats_nothing_but_loses_to_the_exact_one(location):
    """A broad 'India' match must not tie with a named city."""
    p = profile(preferred_locations=["Pune", "India"])

    near = analyze(job(location=location), p)
    exact = analyze(job(location="Pune, Maharashtra, India"), p)

    assert near.location_match is None                     # in-country, not your city
    assert near.signals["location"]["score"] < exact.signals["location"]["score"]
    assert near.signals["location"]["score"] > 0.5 * WEIGHTS["location"]


def test_missing_location_is_neutral_not_a_rejection():
    for empty in ("", "Not specified", "unknown"):
        evidence = analyze(job(location=empty), profile())
        assert evidence.location_match is None
        assert not evidence.hard_reject
        assert evidence.signals["location"]["score"] == pytest.approx(NEUTRAL * WEIGHTS["location"])


def test_a_different_country_scores_low():
    p = profile(preferred_locations=["Pune"])
    evidence = analyze(job(location="Berlin, Germany"), p)

    assert evidence.location_match is False
    assert evidence.signals["location"]["score"] < 0.2 * WEIGHTS["location"]


def test_a_broad_location_alone_still_matches():
    """A profile that only accepts India must not reject Indian cities."""
    p = profile(preferred_locations=["India"])
    evidence = analyze(job(location="Bangalore, Karnataka, India"), p)

    assert evidence.signals["location"]["score"] > 0.5 * WEIGHTS["location"]


# ===========================================================================
# Remote / work mode
# ===========================================================================

def test_remote_is_strong_when_preferred():
    evidence = analyze(job(location="Remote", description="Work from home, Python and SQL."), profile())

    assert job_remote_mode(job(location="Remote")) == "remote"
    assert evidence.signals["remote"]["score"] == pytest.approx(WEIGHTS["remote"])


def test_hybrid_sits_between_remote_and_onsite():
    hybrid = analyze(job(location="Pune", description="Hybrid, 2 days in office. Python, SQL."), profile())
    assert hybrid.signals["remote"]["score"] == pytest.approx(0.7 * WEIGHTS["remote"])


def test_onsite_is_penalised_when_remote_is_only_acceptable():
    p = profile(remote_preference="only")
    evidence = analyze(job(location="Pune, Maharashtra", description="On-site role. Python, SQL."), p)

    assert evidence.hard_reject
    assert any("remote" in reason for reason in evidence.reject_reasons)


def test_onsite_is_acceptable_when_remote_is_only_preferred():
    evidence = analyze(job(location="Pune, Maharashtra", description="On-site role. Python, SQL."), profile())
    assert not evidence.hard_reject
    assert evidence.signals["remote"]["score"] > 0


def test_unstated_work_mode_is_neutral():
    evidence = analyze(job(location="Pune", description="Python and SQL role."), profile())
    assert evidence.signals["remote"]["score"] == pytest.approx(NEUTRAL * WEIGHTS["remote"])


@pytest.mark.parametrize("location,expected", [
    ("Remote", "remote"), ("Work From Home", "remote"), ("wfh", "remote"),
    ("Hybrid - Pune", "hybrid"), ("On-site, Bengaluru", "onsite"),
    ("Pune, Maharashtra", None),
])
def test_work_mode_detection(location, expected):
    assert job_remote_mode(job(location=location)) == expected


# ===========================================================================
# Employment type
# ===========================================================================

def test_preferred_employment_type_scores_full():
    evidence = analyze(job(employment_type=EmploymentType.FULL_TIME), profile())
    assert evidence.employment_match is True
    assert evidence.signals["employment"]["score"] == pytest.approx(WEIGHTS["employment"])


def test_internship_is_accepted_when_wanted():
    evidence = analyze(job(employment_type=EmploymentType.INTERNSHIP), profile())
    assert evidence.employment_match is True
    assert not evidence.hard_reject


def test_internship_can_be_turned_off():
    p = profile(internship_preference=False, employment_types=["full_time"])
    evidence = analyze(job(employment_type=EmploymentType.INTERNSHIP), p)

    assert evidence.employment_match is not True
    assert evidence.signals["employment"]["score"] == 0


def test_undeclared_employment_type_is_neutral_not_a_veto():
    evidence = analyze(job(employment_type=None), profile())
    assert evidence.employment_match is None
    assert not evidence.hard_reject
    assert evidence.signals["employment"]["score"] == pytest.approx(NEUTRAL * WEIGHTS["employment"])


# ===========================================================================
# Company preferences
# ===========================================================================

def test_preferred_company_scores_high():
    p = profile(preferred_companies=["Acme"])
    evidence = analyze(job(company="Acme Technologies"), p)
    assert evidence.signals["company"]["score"] == pytest.approx(WEIGHTS["company"])


def test_company_name_normalises_suffixes_and_plurals():
    p = profile(preferred_companies=["Infosys"])
    for name in ("Infosys Limited", "Infosys Ltd", "Infosys Private Limited", "INFOSYS"):
        assert analyze(job(company=name), p).signals["company"]["score"] == pytest.approx(
            WEIGHTS["company"]), name


def test_non_preferred_company_is_scored_low_without_vetoing():
    p = profile(preferred_companies=["Infosys"])
    evidence = analyze(job(company="Some Other Startup"), p)

    assert evidence.signals["company"]["score"] < 0.5 * WEIGHTS["company"]
    assert not evidence.hard_reject


def test_avoided_company_is_a_hard_reject():
    p = profile(avoid_companies=["Acme"])
    evidence = analyze(job(company="Acme Corp"), p)

    assert evidence.hard_reject
    assert any("avoid" in reason for reason in evidence.reject_reasons)


def test_avoided_company_matches_across_suffixes():
    p = profile(avoid_companies=["Acme Corp"])
    assert analyze(job(company="Acme"), p).hard_reject is True


def test_avoided_role_is_a_hard_reject():
    p = profile(avoid_roles=["Sales Executive"])
    evidence = analyze(job(title="Sales Executive", description="Python and SQL."), p)

    assert evidence.hard_reject
    assert any("Sales Executive" in reason for reason in evidence.reject_reasons)


def test_avoided_role_does_not_veto_a_clean_title():
    p = profile(avoid_roles=["Sales Executive"])
    evidence = analyze(job(title="Python Developer", description="Python and SQL."), p)
    assert not evidence.hard_reject


# ===========================================================================
# Undeclared preferences stay neutral
# ===========================================================================

def test_an_empty_profile_neutralises_every_optional_signal():
    evidence = analyze(job(), CandidateProfile())

    for name in ("skills", "role", "location", "employment", "company"):
        assert evidence.signals[name]["score"] == pytest.approx(NEUTRAL * WEIGHTS[name], abs=0.01), name
    assert not evidence.hard_reject
    assert evidence.role_match is None
    assert evidence.location_match is None
    assert evidence.employment_match is None


def test_undeclared_preferences_never_reject():
    """The strongest guarantee: silence can only cost score, never the job."""
    p = CandidateProfile()
    for title in ("Sales Executive", "Security Guard", "Marketing Intern"):
        assert not analyze(job(title=title, description="Completely unrelated."), p).hard_reject


# ===========================================================================
# Skill aliases / canonicalisation
# ===========================================================================

@pytest.mark.parametrize("spelling,canonical", [
    ("ML", "Machine Learning"),
    ("ml models", "Machine Learning"),
    ("NLP", "Natural Language Processing"),
    ("natural language processing", "Natural Language Processing"),
    ("k8s", "Kubernetes"),
    ("postgres", "PostgreSQL"),
    ("sklearn", "Scikit-learn"),
    ("DL", "Deep Learning"),
    ("FastAPI", "FastAPI"),
    ("fast api", "FastAPI"),
])
def test_aliases_resolve_to_one_canonical_name(spelling, canonical):
    assert normalize_skill(spelling) == canonical


def test_every_alias_maps_back_to_its_own_canonical_skill():
    """A typo in the table would make a skill unmatchable - fail loudly."""
    for canonical, aliases in SKILL_ALIASES.items():
        for alias in aliases:
            assert normalize_skill(alias) == canonical, f"{alias!r} -> {normalize_skill(alias)!r}"


def test_every_family_member_is_a_known_canonical_skill():
    """Otherwise family membership silently does nothing."""
    for family, members in SKILL_FAMILIES.items():
        for member in members:
            assert member in SKILL_ALIASES, f"{family}: {member!r} has no canonical entry"


@pytest.mark.parametrize("posting_skill,profile_skill", [
    ("ML", "Machine Learning"),
    ("NLP", "Natural Language Processing"),
    ("k8s", "Kubernetes"),
    ("Postgres", "PostgreSQL"),
])
def test_alias_spelling_in_a_posting_matches_the_canonical_profile_skill(posting_skill, profile_skill):
    p = profile(skills=[profile_skill])
    evidence = analyze(job(description=f"You will use {posting_skill} in this role."), p)
    assert evidence.matched_skills == [profile_skill]


def test_several_aliases_of_one_skill_count_once():
    """"ML models", "machine learning" and "predictive modelling" are one skill."""
    text = "Work on ML models and machine learning. Predictive modelling with python."
    found = find_skills(text, ["Machine Learning"])

    assert found == ["Machine Learning"]
    assert normalize_skill("ML models") == normalize_skill("machine learning")


def test_a_skill_repeated_many_times_does_not_inflate_the_score():
    once = analyze(job(description="Python role."), profile(skills=["Python", "SQL"]))
    many = analyze(job(description="Python. Python. Python. Python. python PYTHON."), profile(skills=["Python", "SQL"]))

    assert once.signals["skills"]["score"] == many.signals["skills"]["score"]
    assert len(many.matched_skills) == 1


@pytest.mark.parametrize("posting_skill,profile_skill", [
    ("Playwright", "Selenium"),
    ("httpx", "Requests"),
    ("Jenkins", "CI/CD"),
    ("Laravel", "PHP"),
    ("MEAN", "MERN"),
    ("Tableau", "Power BI"),
])
def test_related_technologies_are_transferable_not_equal(posting_skill, profile_skill):
    """"Do not blindly equate unrelated technologies"."""
    assert not same_skill(posting_skill, profile_skill)
    assert related_skill(posting_skill, profile_skill)

    p = profile(skills=[profile_skill])
    evidence = analyze(job(description=f"The role uses {posting_skill} daily."), p)

    assert profile_skill not in evidence.matched_skills
    assert evidence.matched_skills == []            # never reported as a direct hit


@pytest.mark.parametrize("first,second", [
    ("Python", "Java"),
    ("React", "Vue"),
    ("Docker", "TensorFlow"),
    ("Excel", "FastAPI"),
])
def test_unrelated_technologies_share_no_family(first, second):
    assert not related_skill(first, second)


def test_sibling_frameworks_stay_transferable_not_equal():
    """TensorFlow and PyTorch are different tools doing the same job."""
    assert related_skill("TensorFlow", "PyTorch")
    assert not same_skill("TensorFlow", "PyTorch")


def test_sibling_databases_stay_transferable_not_equal():
    """SQL and Redis are both databases, but one is not evidence of the other."""
    assert related_skill("SQL", "Redis")
    assert not same_skill("SQL", "Redis")


def test_skill_matching_is_whole_word():
    """'R' must not match 'React'; 'Java' must not match 'JavaScript'."""
    assert not skill_in_text("React", "Strong written communication required")
    assert not skill_in_text("Java", "JavaScript and TypeScript only")
    assert skill_in_text("Java", "Core Java and JUnit")


def test_transferable_matches_are_reported_as_pairs():
    pairs = transferable_matches(["FastAPI"], ["Django"])
    assert pairs == [("FastAPI", "Django")]


def test_case_variants_of_a_skill_still_match():
    p = profile(skills=["Machine Learning"])
    for text in ("MACHINE LEARNING", "machine learning", "Machine Learning"):
        assert analyze(job(description=text), p).matched_skills == ["Machine Learning"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
