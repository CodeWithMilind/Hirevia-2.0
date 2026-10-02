"""Tests for the canonical candidate profile (Phase 7).

profile.yaml is the ONE place the candidate lives. These tests cover three
things:

  * loading   - explicit path > PROFILE_PATH env > ./profile.yaml, and the
                never-raises fallbacks (missing / empty / non-mapping / broken
                YAML must degrade to a usable profile, never kill a scan);
  * validation - every problem the user should be told about;
  * preferences - tolerant coercion of the hand-edited YAML, plus the
                guarantee that skill_groups + tools collapse into ONE canonical
                skill list (no duplication across files or groups).

The last test pins the shipped profile.yaml.example, so the template cannot
rot into something that fails to load or misses a required field.
"""

import pytest
import yaml

from hirevia.core.matching import analyze
from hirevia.core.models import CandidateProfile, Job
from hirevia.core.skills import SKILL_ALIASES
from hirevia.profile.profile import (
    DEFAULT_FILENAME,
    PROFILE_ENV,
    build_profile,
    load_profile,
    profile_prompt_block,
    resolve_profile_path,
    validate_profile,
)


# --- helpers ------------------------------------------------------------------


def write_profile(tmp_path, data, name=DEFAULT_FILENAME):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


MINIMAL = {
    "name": "Ada",
    "candidate_type": "fresher",
    "experience_years": 0,
    "skills": ["Python", "FastAPI"],
    "preferred_roles": ["Python Developer"],
    "preferred_locations": ["Pune"],
}


# --- loading ------------------------------------------------------------------


def test_loads_from_explicit_path(tmp_path):
    path = write_profile(tmp_path, MINIMAL)
    profile = load_profile(path)

    assert profile.name == "Ada"
    assert profile.preferred_roles == ["Python Developer"]
    assert profile.candidate_type == "fresher"


def test_profiles_path_env_is_used_when_no_argument(tmp_path, monkeypatch):
    path = write_profile(tmp_path, {"name": "FromEnv"}, name="custom.yaml")
    monkeypatch.setenv(PROFILE_ENV, str(path))
    monkeypatch.chdir(tmp_path)

    assert resolve_profile_path() == path
    assert load_profile().name == "FromEnv"


def test_explicit_argument_beats_the_env(tmp_path, monkeypatch):
    wanted = write_profile(tmp_path, {"name": "Explicit"}, name="wanted.yaml")
    other = write_profile(tmp_path, {"name": "Env"}, name="env.yaml")
    monkeypatch.setenv(PROFILE_ENV, str(other))

    assert resolve_profile_path(wanted) == wanted
    assert load_profile(wanted).name == "Explicit"


def test_defaults_to_cwd_profile_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert resolve_profile_path() == tmp_path / DEFAULT_FILENAME


def test_missing_file_falls_back_to_a_usable_profile(tmp_path):
    profile = load_profile(tmp_path / "does-not-exist.yaml")

    # Never raises, and yields something the matcher can run with.
    assert isinstance(profile, CandidateProfile)
    assert profile.candidate_type == "fresher"
    assert profile.accept_fresher is True


def test_empty_file_falls_back(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")

    assert load_profile(path).candidate_type == "fresher"


def test_non_mapping_file_falls_back(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    assert load_profile(path).candidate_type == "fresher"


def test_broken_yaml_falls_back_without_raising(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n  bad: : :\n", encoding="utf-8")

    profile = load_profile(path)
    assert isinstance(profile, CandidateProfile)
    assert profile.candidate_type == "fresher"


def test_profile_preferences_are_not_inherited_from_filters(tmp_path):
    """filters.yaml is not a fallback source for candidate preferences."""
    from hirevia.config import FilterConfig

    filters = FilterConfig(
        roles=["Python Developer"],
        locations=["Pune"],
        employment=["full_time"],
        skills=["Python", "SQL"],
        experience={"max_years": 3, "fresher": True},
    )
    profile = load_profile(tmp_path / "missing.yaml", filters)

    assert profile.preferred_roles == []
    assert profile.preferred_locations == []
    assert profile.employment_types == []
    assert profile.skills == []
    assert profile.target_max_experience_years == 2


def test_nested_candidate_preferences_load_for_existing_profile_consumers(tmp_path):
    path = write_profile(
        tmp_path,
        {
            "candidate": {
                "experience_level": "fresher",
                "max_experience_years": 1,
                "preferred_roles": ["Python Developer"],
                "preferred_locations": ["Pune"],
                "keywords": ["Python", "SQL"],
            },
            "matching": {"p1_threshold": 70},
        },
    )

    profile = load_profile(path)

    assert profile.candidate_type == "fresher"
    assert profile.target_max_experience_years == 1
    assert profile.preferred_roles == ["Python Developer"]
    assert profile.preferred_locations == ["Pune"]
    assert profile.skills == ["Python", "SQL"]


# --- validation ---------------------------------------------------------------


def test_a_complete_profile_reports_no_problems():
    assert validate_profile(build_profile(MINIMAL)) == []


@pytest.mark.parametrize("data,fragment", [
    ({"name": "A"}, "no skills configured"),
    ({"name": "A", "skills": ["Python"]}, "no preferred_roles configured"),
    ({"name": "A", "skills": ["Python"], "preferred_roles": ["Dev"]},
     "no preferred_locations configured"),
    ({"name": "A", "experience_years": -3}, "experience_years is negative"),
    ({"name": "A", "target_max_experience_years": -1}, "target_max_experience_years is negative"),
    ({"name": "A", "remote_preference": "sometimes-maybe"}, "unknown remote_preference"),
    ({"name": "A", "candidate_type": "wizard"}, "unknown candidate_type"),
    ({"name": "A", "salary_min": 900000, "salary_max": 100000}, "salary_min is above salary_max"),
])
def test_validation_reports_each_problem(data, fragment):
    problems = validate_profile(build_profile(data))
    assert any(fragment in problem for problem in problems), problems


def test_validation_never_raises_on_a_half_filled_profile():
    """A partial profile must still match jobs; the user just gets warnings."""
    problems = validate_profile(build_profile({"name": "OnlyAName"}))
    assert len(problems) >= 3


# --- tolerant coercion --------------------------------------------------------


def test_comma_separated_string_becomes_a_list():
    profile = build_profile({**MINIMAL, "skills": "Python, FastAPI ,SQL"})
    assert profile.skills == ["Python", "FastAPI", "SQL"]


def test_scalar_becomes_a_single_item_list():
    assert build_profile({**MINIMAL, "skills": "Python"}).skills == ["Python"]


def test_mapping_uses_its_keys_as_items():
    profile = build_profile({**MINIMAL, "preferred_roles": {"Python Developer": 1, "AI Engineer": 1}})
    assert profile.preferred_roles == ["Python Developer", "AI Engineer"]


def test_list_of_dicts_reads_their_name_key():
    profile = build_profile({**MINIMAL, "preferred_roles": [{"name": "Data Scientist"}]})
    assert profile.preferred_roles == ["Data Scientist"]


def test_duplicates_are_removed_case_insensitively():
    profile = build_profile({**MINIMAL, "skills": ["Python", "python", "PYTHON", "SQL"]})
    assert profile.skills == ["Python", "SQL"]


@pytest.mark.parametrize("value,expected", [
    ("yes", True), ("true", True), ("1", True), ("on", True),
    ("no", False), ("false", False), ("0", False), ("off", False),
    (True, True), (False, False),
])
def test_boolean_coercion(value, expected):
    assert build_profile({**MINIMAL, "accept_fresher": value}).accept_fresher is expected


def test_unparseable_boolean_falls_back_to_the_default():
    assert build_profile({**MINIMAL, "accept_fresher": "maybe"}).accept_fresher is True


def test_integers_are_parsed_from_strings():
    profile = build_profile({**MINIMAL, "experience_years": "2", "graduation_year": "2026"})
    assert profile.experience_years == 2
    assert profile.graduation_year == 2026


def test_matching_never_treats_the_candidate_as_more_senior_than_the_ceiling():
    """target_max_experience_years is a target, not a cap on reality.

    A 4-year candidate keeps the baseline target_max of 2, so the field alone
    would understate them. The matching layer guards this with
    max(target_max, experience_years), so a 4-5 year role still fits and only
    a genuinely distant one is penalised.
    """
    profile = build_profile({"name": "A", "experience_years": 4})
    assert profile.target_max_experience_years < profile.experience_years  # the quirk

    def job(text):
        return Job(source="t", source_job_id="1", title="Python Developer",
                   company="Acme", location="Pune", description=text,
                   url="https://example.com/1")

    within = analyze(job("Python FastAPI. 4-5 years."), profile)
    beyond = analyze(job("Python FastAPI. 6+ years."), profile)

    assert within.experience_match is True
    assert beyond.experience_match is False


def test_explicit_target_max_wins_over_the_default():
    assert build_profile({**MINIMAL, "target_max_experience_years": 1}).target_max_experience_years == 1


def test_explicit_profile_value_beats_the_filters_inheritance(tmp_path):
    from hirevia.config import FilterConfig

    filters = FilterConfig(roles=["Java Developer"], locations=["Mumbai"])
    profile = build_profile({**MINIMAL, "preferred_roles": ["Python Developer"]}, filters)
    assert profile.preferred_roles == ["Python Developer"]


# --- skills: one canonical list ------------------------------------------------


def test_skill_groups_are_flattened_into_one_canonical_list():
    profile = build_profile({
        **MINIMAL,
        "skills": [],
        "skill_groups": {"languages": ["Python"], "web": ["FastAPI"]},
    })
    assert set(profile.skills) == {"Python", "FastAPI"}


def test_tools_are_merged_into_the_canonical_skill_list():
    profile = build_profile({**MINIMAL, "skills": ["Python"], "tools": ["Git", "Docker"]})
    assert set(profile.skills) == {"Python", "Git", "Docker"}


def test_a_skill_in_two_groups_is_stored_once():
    """Groups are a readability device - they must never duplicate a skill."""
    profile = build_profile({
        **MINIMAL,
        "skills": [],
        "skill_groups": {
            "ai_ml": ["Python", "Pandas"],
            "data_science": ["Pandas", "NumPy"],
        },
        "tools": ["Pandas"],
    })
    assert profile.skills.count("Pandas") == 1
    assert set(profile.skills) == {"Python", "Pandas", "NumPy"}


def test_malformed_skill_groups_are_ignored_not_fatal():
    profile = build_profile({**MINIMAL, "skills": ["Python"], "skill_groups": "not-a-mapping"})
    assert profile.skills == ["Python"]
    assert profile.skill_groups == {}


def test_profile_keeps_the_spelling_the_candidate_wrote():
    """The profile is a human file: it is stored as written, not rewritten.

    Canonicalisation belongs to the matcher, so a user reading profile.yaml
    sees exactly what they typed. Alias resolution happens at match time.
    """
    profile = build_profile({**MINIMAL, "skills": ["ML", "k8s", "postgres"]})
    assert profile.skills == ["ML", "k8s", "postgres"]


def test_matcher_resolves_skill_aliases_and_dedupes_them():
    """'ML' and 'Machine Learning' are the same skill and count once.

    The profile may legitimately list both spellings; the canonical view the
    matcher compares against must collapse them to a single entry, otherwise
    the profile would double-count one skill and understate the overlap.
    """
    from hirevia.core.matching import _profile_skills

    profile = build_profile({**MINIMAL, "skills": ["ML", "Machine Learning", "NLP", "k8s"]})

    assert _profile_skills(profile) == ["Machine Learning", "Natural Language Processing", "Kubernetes"]
    for skill in _profile_skills(profile):
        assert skill in SKILL_ALIASES


# --- prompt rendering ----------------------------------------------------------


def test_prompt_block_omits_fields_the_candidate_never_set():
    """Undeclared fields are omitted so the model never assumes them."""
    block = profile_prompt_block(build_profile({"name": "Ada"}))

    assert "Name: Ada" in block
    assert "Salary expectation" not in block
    assert "Preferred roles" not in block


def test_prompt_block_includes_the_declared_preferences():
    block = profile_prompt_block(build_profile(MINIMAL))

    assert "Preferred roles: Python Developer" in block
    assert "Preferred locations: Pune" in block
    assert "Open to internships: yes" in block


def test_prompt_block_renders_salary_as_a_range():
    profile = build_profile({**MINIMAL, "salary_min": 600000, "salary_max": 1200000})
    assert "Salary expectation (annual INR): 600000-1200000" in profile_prompt_block(profile)


# --- the shipped template ------------------------------------------------------


def test_shipped_example_profile_is_valid():
    """profile.yaml.example must stay loadable and complete.

    Guards the template against rot: a field rename or a YAML typo would make
    every new user's first scan silently fall back to the baseline.
    """
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "profile.yaml.example"
    assert example.exists(), "profile.yaml.example is missing"

    profile = load_profile(example)
    assert validate_profile(profile) == []


def test_shipped_example_covers_every_required_field():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "profile.yaml.example"
    data = yaml.safe_load(example.read_text(encoding="utf-8")) or {}

    for field in (
        "candidate_type", "education", "degree", "skill_groups", "tools",
        "projects", "preferred_roles", "role_families", "preferred_locations",
        "remote_preference", "experience_years", "internship_preference",
    ):
        assert field in data, f"profile.yaml.example is missing '{field}'"

    # The spec calls out these skill categories explicitly.
    groups = data["skill_groups"]
    for category in ("languages", "frameworks", "ai_ml", "data_science", "backend"):
        assert groups.get(category), f"skill_groups is missing '{category}'"


def test_shipped_example_declares_only_real_role_families():
    """A typo'd family key silently disables that whole role track."""
    from hirevia.core.matching import ROLE_FAMILY_KEYWORDS
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "profile.yaml.example"
    families = (yaml.safe_load(example.read_text(encoding="utf-8")) or {}).get("role_families") or []

    assert families
    for family in families:
        assert family in ROLE_FAMILY_KEYWORDS, f"unknown role family '{family}'"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
