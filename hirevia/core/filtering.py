"""YAML-based deterministic filtering"""

import re
from typing import Dict, List, Optional

from hirevia.core.models import Job, FilterResult
from hirevia.config import FilterConfig


def extract_years_experience(text: str) -> Optional[int]:
    """Extract years of experience from text."""
    patterns = [
        r"(\d+)\+?\s*years?\s*(?:of\s*)?experience",
        r"(\d+)\+?\s*yrs?\s*(?:of\s*)?exp",
        r"experience\s*:?\s*(\d+)\+?\s*years?",
        r"minimum\s*(\d+)\+?\s*years?",
        r"at\s*least\s*(\d+)\+?\s*years?",
    ]
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            return int(match.group(1))
    return None


def check_roles(job: Job, roles: List[str]) -> tuple[bool, List[str]]:
    """Check if job title matches any desired role.

    An empty list means "no role preference configured", which must pass:
    otherwise a missing/blank filters.yaml silently drops every single job.
    """
    if not roles:
        return True, []
    title_lower = job.title.lower()
    matched = []
    for role in roles:
        if role.lower() in title_lower:
            matched.append(role)
    return len(matched) > 0, matched


def check_locations(job: Job, locations: List[str]) -> tuple[bool, List[str]]:
    """Check if job location matches any desired location (empty = any)."""
    if not locations:
        return True, []
    location_lower = job.location.lower()
    matched = []
    for loc in locations:
        if loc.lower() in location_lower:
            matched.append(loc)
    # Also check description for remote mentions
    if "remote" in location_lower or "work from home" in location_lower:
        if "remote" in [l.lower() for l in locations]:
            matched.append("remote")
    return len(matched) > 0, matched


def check_experience(job: Job, exp_config: Dict) -> tuple[bool, Dict]:
    """Check if job experience requirements match."""
    max_years = exp_config.get("max_years", 99)
    fresher_allowed = exp_config.get("fresher", False)

    # Try to extract from job
    years = extract_years_experience(job.description)
    if years is None:
        years = extract_years_experience(job.title)

    if years is None:
        # If we can't determine, be permissive for fresher-friendly
        if fresher_allowed:
            return True, {"detected_years": None, "max_allowed": max_years, "fresher_ok": True}
        return True, {"detected_years": None, "max_allowed": max_years, "fresher_ok": fresher_allowed}

    if years <= max_years:
        return True, {"detected_years": years, "max_allowed": max_years, "fresher_ok": fresher_allowed}

    # If over max but fresher allowed and job says "fresher" or "entry"
    if fresher_allowed and years <= max_years + 1:
        text = (job.title + " " + job.description).lower()
        if any(kw in text for kw in ["fresher", "entry", "junior", "0-1", "0 to 1"]):
            return True, {"detected_years": years, "max_allowed": max_years, "fresher_ok": True}

    return False, {"detected_years": years, "max_allowed": max_years, "fresher_ok": fresher_allowed}


def check_skills(job: Job, skills: List[str]) -> tuple[bool, List[str]]:
    """Check if job description contains desired skills (empty = any)."""
    if not skills:
        return True, []
    text = (job.title + " " + job.description + " " + " ".join(job.skills)).lower()
    matched = []
    for skill in skills:
        if skill.lower() in text:
            matched.append(skill)
    return len(matched) > 0, matched


def check_employment(job: Job, employment_types: List[str]) -> tuple[bool, List[str]]:
    """Check if job employment type matches."""
    if not employment_types:
        return True, []

    # Safely get employment type value
    if job.employment_type:
        if hasattr(job.employment_type, 'value'):
            job_type = job.employment_type.value.lower()
        else:
            job_type = str(job.employment_type).lower()
    else:
        job_type = ""
    text = (job.title + " " + job.description).lower()

    matched = []
    for emp in employment_types:
        emp_lower = emp.lower()
        if emp_lower in job_type or emp_lower in text:
            matched.append(emp)

    if matched:
        return True, matched
    if not job_type:
        # The post never stated a contract type (typical for a Telegram alert).
        # Silence is not a mismatch - vetoing here would drop real jobs simply
        # because the collector could not read "Full time" out of the message.
        return True, []
    return False, []


_EXCLUDE_CACHE: Dict[str, "re.Pattern"] = {}


def _exclude_pattern(keyword: str) -> "re.Pattern":
    """Whole-word matcher so 'lead' cannot veto 'leading' or 'leadership'.

    A trailing 's' is allowed, so 'senior' still matches 'seniors'. Matching is
    deliberately whole-word: substring matching made filters like 'lead' veto
    every posting that merely mentioned leading or blacklead.
    """
    key = keyword.lower().strip()
    if key not in _EXCLUDE_CACHE:
        _EXCLUDE_CACHE[key] = re.compile(r"\b" + re.escape(key) + r"s?\b")
    return _EXCLUDE_CACHE[key]


def check_excludes(job: Job, exclude_keywords: List[str]) -> tuple[bool, List[str]]:
    """Check if job contains excluded keywords (whole-word, case-insensitive)."""
    text = (job.title + " " + job.description).lower()
    matched = []
    for kw in exclude_keywords:
        if _exclude_pattern(kw).search(text):
            matched.append(kw)
    return len(matched) == 0, matched


def apply_filters(job: Job, config: FilterConfig) -> FilterResult:
    """Apply all deterministic filters."""
    all_matched = {}

    # Check excludes first (fast fail)
    exclude_passed, exclude_matched = check_excludes(job, config.exclude_keywords)
    if not exclude_passed:
        return FilterResult(
            passed=False,
            matched={"excluded": exclude_matched},
            reason=f"Excluded keywords found: {', '.join(exclude_matched)}",
        )

    # Positive signals: what makes this job one the candidate cares about.
    signals = []

    # Check roles
    roles_passed, roles_matched = check_roles(job, config.roles)
    if roles_matched:
        all_matched["roles"] = roles_matched
        signals.append(f"Role match: {', '.join(roles_matched)}")

    # Check locations
    loc_passed, loc_matched = check_locations(job, config.locations)
    if loc_matched:
        all_matched["locations"] = loc_matched
        signals.append(f"Location match: {', '.join(loc_matched)}")

    # Check skills
    skills_passed, skills_matched = check_skills(job, config.skills)
    if skills_matched:
        all_matched["skills"] = skills_matched
        signals.append(
            f"Skills match: {', '.join(skills_matched[:5])}{'...' if len(skills_matched) > 5 else ''}"
        )

    # Qualification checks: these can only veto, they never signal interest.
    details = []

    # Check experience
    exp_passed, exp_info = check_experience(job, config.experience)
    all_matched["experience"] = exp_info
    if exp_passed and exp_info.get("detected_years") is not None:
        details.append(f"Experience: {exp_info['detected_years']} years (max {exp_info['max_allowed']})")
    elif exp_passed and exp_info.get("fresher_ok"):
        details.append("Fresher/entry-level eligible")

    # Check employment
    emp_passed, emp_matched = check_employment(job, config.employment)
    if emp_matched:
        all_matched["employment"] = emp_matched
        details.append(f"Employment type: {', '.join(emp_matched)}")

    # At least one *configured* positive criterion must fire (OR between roles,
    # locations and skills, as before). A criterion with no entries is "any":
    # it neither signals interest nor vetoes, so a blank filters.yaml accepts
    # everything instead of silently dropping every job.
    configured = [
        ok for ok, wanted in (
            (roles_passed, config.roles),
            (loc_passed, config.locations),
            (skills_passed, config.skills),
        ) if wanted
    ]
    positive_signal = any(configured) if configured else True
    passed = positive_signal and exp_passed and emp_passed

    if signals:
        reason = "; ".join(signals + details)
    elif not configured:
        reason = "No filter criteria configured - accepting everything" + (
            f" ({'; '.join(details)})" if details else ""
        )
    else:
        reason = "No matching role, location or skill"

    return FilterResult(
        passed=passed,
        matched=all_matched,
        reason=reason,
    )