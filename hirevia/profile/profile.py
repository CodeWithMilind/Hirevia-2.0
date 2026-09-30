"""Candidate profile loading and validation (Phase 7).

profile.yaml is the ONE canonical place the candidate lives. Neither the
matching engine nor the AI prompt keeps its own copy of skills, roles or
locations: both read this model, so updating the candidate is a one-file change.

Location precedence: explicit argument > PROFILE_PATH env > ./profile.yaml

A missing or unreadable profile never breaks a scan: load_profile() falls back
to the baseline candidate below, enriched with whatever filters.yaml already
declares (roles / locations / employment / experience ceiling), and logs one
warning telling the user how to fix it.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from hirevia.core.models import CandidateProfile

logger = logging.getLogger(__name__)

PROFILE_ENV = "PROFILE_PATH"
DEFAULT_FILENAME = "profile.yaml"

VALID_REMOTE_PREFERENCES = ("any", "preferred", "only", "none", "exclude")
VALID_CANDIDATE_TYPES = ("fresher", "entry", "entry_level", "entry-level", "experienced")

# Defaults for a candidate that has no profile.yaml yet: an entry-level seeker.
BASELINE = {
    "name": "Candidate",
    "summary": "",
    "candidate_type": "fresher",
    "experience_years": 0,
    "target_max_experience_years": 2,
    "accept_fresher": True,
    "internship_preference": True,
    "remote_preference": "any",
}


def resolve_profile_path(profile_path: Optional[Union[str, Path]] = None) -> Path:
    """Where profile.yaml lives - argument, then env, then working directory."""
    if profile_path:
        return Path(profile_path)
    env = (os.getenv(PROFILE_ENV) or "").strip()
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_FILENAME


def load_profile(
    profile_path: Optional[Union[str, Path]] = None,
    filters=None,
) -> CandidateProfile:
    """Load the canonical candidate profile. Never raises."""
    path = resolve_profile_path(profile_path)

    if not path.exists():
        profile = _baseline_profile(filters)
        logger.warning(
            "[Profile] %s not found - baseline profile in use (%d skills, %d roles). "
            "Copy profile.yaml.example to profile.yaml to personalise matching.",
            path, len(profile.skills), len(profile.preferred_roles),
        )
        return profile

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 - a broken profile must not stop a scan
        logger.error("[Profile] could not read %s (%s) - using baseline profile", path, e)
        return _baseline_profile(filters)

    if not isinstance(data, dict):
        logger.error("[Profile] %s is not a mapping - using baseline profile", path)
        return _baseline_profile(filters)
    if not data:
        logger.warning("[Profile] %s is empty - using baseline profile", path)
        return _baseline_profile(filters)

    profile = _build_profile(data, filters)
    for warning in validate_profile(profile):
        logger.warning("[Profile] %s: %s", path.name, warning)
    logger.debug(
        "[Profile] loaded %s (%d skills, %d roles, %d locations, type=%s)",
        path, len(profile.skills), len(profile.preferred_roles),
        len(profile.preferred_locations), profile.candidate_type,
    )
    return profile


def build_profile(data: Dict[str, Any], filters=None) -> CandidateProfile:
    """Public entry point used by tests and tooling."""
    return _build_profile(data, filters)

# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_profile(profile: CandidateProfile) -> List[str]:
    """Return human-readable problems. An empty list means the profile is usable.

    Nothing here raises: a half-filled profile should still match jobs, the user
    just deserves to know which part of it is missing.
    """
    problems: List[str] = []

    if not profile.skills:
        problems.append("no skills configured - skill relevance will be neutral for every job")
    if not profile.preferred_roles:
        problems.append("no preferred_roles configured - role relevance falls back to generic tech roles")
    if not profile.preferred_locations:
        problems.append("no preferred_locations configured - every location scores neutral")
    if profile.experience_years < 0:
        problems.append("experience_years is negative")
    if profile.target_max_experience_years < 0:
        problems.append("target_max_experience_years is negative")
    if profile.remote_preference.casefold() not in VALID_REMOTE_PREFERENCES:
        problems.append(
            "unknown remote_preference %r (use one of %s)"
            % (profile.remote_preference, ", ".join(VALID_REMOTE_PREFERENCES))
        )
    if profile.candidate_type.casefold() not in VALID_CANDIDATE_TYPES:
        problems.append("unknown candidate_type %r" % profile.candidate_type)
    if profile.max_seniority:
        from hirevia.core.matching import SENIORITY_RANK

        token = profile.max_seniority.strip().casefold()
        if not any(token.startswith(name) or name.startswith(token) for name in SENIORITY_RANK):
            problems.append(
                "unknown max_seniority %r (use one of: %s)"
                % (profile.max_seniority, ", ".join(sorted(set(SENIORITY_RANK))))
            )
    if profile.salary_min and profile.salary_max and profile.salary_min > profile.salary_max:
        problems.append("salary_min is above salary_max")
    return problems


# --------------------------------------------------------------------------
# Tolerant coercion - profile.yaml is hand edited, so it is never trusted
# --------------------------------------------------------------------------

def _baseline_profile(filters=None) -> CandidateProfile:
    data = dict(BASELINE)
    _inherit_from_filters(data, filters)
    return _build_profile(data, filters=None)


def _build_profile(data: Dict[str, Any], filters=None) -> CandidateProfile:
    payload: Dict[str, Any] = dict(BASELINE)
    payload.update({k: v for k, v in data.items() if v is not None})

    _inherit_from_filters(payload, filters)

    for key in ("name", "summary", "education", "degree", "candidate_type",
                "remote_preference", "max_seniority"):
        payload[key] = _text(payload.get(key)) or str(BASELINE.get(key, ""))

    payload["experience_years"] = _int(payload.get("experience_years"), 0)
    payload["target_max_experience_years"] = _int(
        payload.get("target_max_experience_years"), max(2, _int(payload.get("experience_years"), 0))
    )
    payload["graduation_year"] = _optional_int(payload.get("graduation_year"))
    payload["salary_min"] = _optional_int(payload.get("salary_min"))
    payload["salary_max"] = _optional_int(payload.get("salary_max"))
    payload["notice_period_days"] = _optional_int(payload.get("notice_period_days"))
    payload["accept_fresher"] = _bool(payload.get("accept_fresher"), True)
    payload["internship_preference"] = _bool(payload.get("internship_preference"), True)

    for key in (
        "skills", "tools", "projects", "preferred_roles", "role_families",
        "preferred_locations", "employment_types", "preferred_companies",
        "avoid_companies", "avoid_roles",
    ):
        payload[key] = _string_list(payload.get(key))

    groups = payload.get("skill_groups")
    payload["skill_groups"] = (
        {str(name): _string_list(values) for name, values in groups.items()}
        if isinstance(groups, dict) else {}
    )

    return CandidateProfile(**payload)


def _inherit_from_filters(payload: Dict[str, Any], filters) -> None:
    """Fill profile gaps from filters.yaml so one missing file cannot leave the
    matcher blind. Anything the profile already declares always wins."""
    if filters is None:
        return

    def wanted(key: str, value) -> None:
        current = payload.get(key)
        if (current in (None, "", [], {}) or current == BASELINE.get(key)) and value:
            payload[key] = list(value)

    wanted("preferred_roles", getattr(filters, "roles", None))
    wanted("preferred_locations", getattr(filters, "locations", None))
    wanted("employment_types", getattr(filters, "employment", None))
    wanted("skills", getattr(filters, "skills", None))

    experience = getattr(filters, "experience", None) or {}
    if isinstance(experience, dict):
        if experience.get("max_years") is not None:
            payload["target_max_experience_years"] = _int(
                experience.get("max_years"), payload.get("target_max_experience_years")
            )
        if experience.get("fresher") is not None:
            payload["accept_fresher"] = _bool(experience.get("fresher"), True)

def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item)
    return str(value).strip()


def _string_list(value) -> List[str]:
    """Accept lists, comma separated strings, dicts and scalars; drop empties."""
    if value is None:
        return []
    if isinstance(value, str):
        items: List[Any] = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, dict):
        items = list(value.keys())
    else:
        items = [value]

    out, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("skill") or item.get("role")
        text = _text(item)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def _int(value, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


# --------------------------------------------------------------------------
# Prompt rendering - the AI sees exactly this, nothing invented
# --------------------------------------------------------------------------

def profile_prompt_block(profile: CandidateProfile) -> str:
    """Render the profile for the AI prompt. Undeclared fields are omitted so
    the model is never tempted to assume something the candidate never said."""

    def line(label: str, value) -> Optional[str]:
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v)
        text = _text(value)
        return f"{label}: {text}" if text else None

    lines = [
        line("Name", profile.name),
        line("Summary", profile.summary),
        line("Candidate type", profile.candidate_type),
        line("Education", profile.education),
        line("Degree", profile.degree),
        line("Graduation year", profile.graduation_year),
        line("Years of experience", profile.experience_years),
        line("Comfortable with roles asking up to", f"{profile.target_max_experience_years} years"),
        line("Fresher friendly", "yes" if profile.accept_fresher else "no"),
        line("Open to internships", "yes" if profile.internship_preference else "no"),
        line("Will consider roles up to", profile.max_seniority or "their entry/junior level"),
        line("Skills", profile.skills),
        line("Projects", profile.projects),
        line("Preferred roles", profile.preferred_roles),
        line("Preferred locations", profile.preferred_locations),
        line("Remote preference", profile.remote_preference),
        line("Employment types", profile.employment_types),
        line("Preferred companies", profile.preferred_companies),
        line("Companies to avoid", profile.avoid_companies),
        line("Roles to avoid", profile.avoid_roles),
        line("Salary expectation (annual INR)", _salary_text(profile)),
    ]
    return "\n".join(item for item in lines if item)


def _salary_text(profile: CandidateProfile) -> str:
    if not profile.salary_min and not profile.salary_max:
        return ""
    if profile.salary_min and profile.salary_max:
        return f"{profile.salary_min}-{profile.salary_max}"
    return str(profile.salary_min or profile.salary_max)
