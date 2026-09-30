"""YAML-based deterministic filtering"""

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from hirevia.core.matching import analyze, job_remote_mode, parse_experience, remote_mode
from hirevia.core.models import CandidateProfile, Job, FilterResult
from hirevia.config import FilterConfig
from hirevia.core.freshness import is_expired


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


def apply_filters(
    job: Job, config: FilterConfig, profile: Optional[CandidateProfile] = None
) -> FilterResult:
    """Apply local hard vetoes; missing evidence stays UNKNOWN and passes."""
    from hirevia.core.matching import job_seniority

    intent = str((job.raw_data or {}).get("intent_category") or "").casefold()
    rejected_intents = {
        "promotional": "promotional_content",
        "educational": "educational_content",
        "informational": "non_job_content",
        "spam": "spam_content",
        "selection_announcement": "no_open_opportunity",
        "aggregator_list": "non_job_content",
    }
    if intent in rejected_intents:
        return _reject(rejected_intents[intent], "telegram.intent_category", {"category": intent})

    if not (job.title or "").strip():
        return _reject("invalid_opportunity", "job.title")
    if is_expired(job) or job.application_status.value in {"closed", "expired"}:
        return _reject("expired_opportunity", "job.expiry")
    if job.deadline:
        deadline = job.deadline
        if deadline.tzinfo is not None:
            deadline = deadline.astimezone(timezone.utc).replace(tzinfo=None)
        if deadline < datetime.utcnow():
            return _reject("expired_opportunity", "job.deadline")

    supplied_profile = profile is not None
    profile = profile or CandidateProfile()
    unknown: List[str] = []
    matched: Dict[str, object] = {}
    text = " ".join((job.title or "", job.experience or "", job.description or ""))

    roles_passed, roles_matched = check_roles(job, config.roles)
    if roles_matched:
        matched["roles"] = roles_matched
    locations_passed, locations_matched = check_locations(job, config.locations)
    if locations_matched:
        matched["locations"] = locations_matched
    _, skills_matched = check_skills(job, config.skills)
    if skills_matched:
        matched["skills"] = skills_matched

    experience = config.experience or {}
    configured_max = _optional_number(experience.get("max_years"))
    maximum = configured_max
    maximum_rule = "experience.max_years"
    if maximum is None and supplied_profile:
        maximum = max(profile.target_max_experience_years, profile.experience_years)
        maximum_rule = "profile.target_max_experience_years"
    required = parse_experience(text)
    if maximum is not None and required is None:
        unknown.append("experience")
    elif maximum is not None:
        required_min, required_max = required
        matched["experience"] = {
            "detected_years": required_min,
            "minimum": required_min,
            "maximum": required_max,
            "max_allowed": maximum,
            "fresher_ok": required_min <= 1,
        }
        if required_min > maximum:
            return _reject(
                "experience_exceeds_maximum", maximum_rule, matched,
            )

    allowed_employment = {_normalize_employment(item) for item in config.employment if str(item).strip()}
    if allowed_employment:
        job_employment = _job_employment(job, text)
        if job_employment is None:
            unknown.append("employment")
        elif job_employment not in allowed_employment:
            return _reject(
                "incompatible_employment_type", "employment",
                {"required": job_employment, "allowed": sorted(allowed_employment)},
            )

    title = (job.title or "").casefold()
    excluded = [
        keyword for keyword in config.exclude_keywords
        if keyword.strip() and _whole_word(keyword).search(title)
    ]
    if excluded:
        return _reject("excluded_role", "exclude_keywords", {"matched": excluded})

    seniority = job_seniority(job.title)
    if seniority and _seniority_is_excluded(seniority, config.exclude_keywords):
        return _reject("excluded_role", "exclude_keywords", {"seniority": seniority})
    if seniority and profile.max_seniority and _seniority_rank(profile.max_seniority) < _seniority_rank(seniority):
        return _reject("seniority_exceeds_maximum", "profile.candidate_type", {"seniority": seniority})
    if supplied_profile and seniority:
        from hirevia.core.matching import seniority_ceiling

        if seniority_ceiling(profile) < _seniority_rank(seniority):
            return _reject("seniority_exceeds_maximum", "profile.candidate_type", {"seniority": seniority})

    avoided_roles = [role for role in profile.avoid_roles if _phrase_in_text(role, title)]
    if avoided_roles:
        return _reject("excluded_role", "profile.avoid_roles", {"matched": avoided_roles})

    excluded_company = next(
        (company for company in profile.avoid_companies if _company_matches(job.company, company)),
        None,
    )
    if excluded_company:
        return _reject("excluded_company", "profile.avoid_companies", {"matched": excluded_company})

    if config.locations:
        mode = remote_mode(job.location)
        remote_allowed = any(_normalize_location(item) in {
            "remote", "work from home", "wfh", "anywhere", "global", "worldwide", "any"
        } for item in config.locations)
        if mode == "remote" and not remote_allowed:
            return _reject("incompatible_location", "locations", {"mode": "remote"})
        city_preferences = [
            item for item in config.locations
            if _normalize_location(item) not in {
                "remote", "work from home", "wfh", "anywhere", "global", "worldwide", "any"
            }
        ]
        if not (job.location or "").strip() and mode is None:
            unknown.append("location")
        elif mode in {"onsite", "hybrid"} and remote_allowed and not city_preferences:
            return _reject("incompatible_location", "locations", {"mode": mode})
        elif mode == "remote":
            pass
        elif not city_preferences:
            unknown.append("location")
        else:
            location_profile = CandidateProfile(
                preferred_locations=city_preferences, remote_preference="any"
            )
            location_only_job = job.model_copy(update={
                "title": "",
                "company": "",
                "description": "",
                "experience": None,
                "employment_type": None,
                "skills": [],
            })
            location_evidence = analyze(location_only_job, location_profile)
            if location_evidence.location_match is False:
                return _reject(
                    "incompatible_location", "locations", {"location": job.location or "unknown"}
                )

    if profile.remote_preference.strip().casefold() == "only":
        mode = job_remote_mode(job)
        if mode in {"onsite", "hybrid"}:
            return _reject("incompatible_location", "profile.remote_preference", {"mode": mode})
        if mode is None:
            unknown.append("location")

    if unknown:
        return FilterResult(
            passed=True,
            status="UNKNOWN",
            reason=f"insufficient_{unknown[0]}_information",
            rule=None,
            matched=matched,
            details={"unknown": unknown},
        )
    return FilterResult(
        passed=True,
        status="PASS",
        reason="passed_hard_filters",
        rule=None,
        matched=matched,
        details={},
    )


def _reject(reason: str, rule: str, details: Optional[dict] = None) -> FilterResult:
    return FilterResult(
        passed=False, status="REJECT", reason=reason, rule=rule,
        matched=details or {}, details=details or {},
    )


def _optional_number(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _whole_word(value: str):
    return _exclude_pattern(value)


def _phrase_in_text(phrase: str, text: str) -> bool:
    return bool(phrase.strip() and re.search(
        r"(?<!\w)" + re.escape(phrase.strip().casefold()) + r"(?!\w)", text
    ))


def _normalize_company(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (value or "").casefold()).split())


def _company_matches(actual: str, excluded: str) -> bool:
    actual, excluded = _normalize_company(actual), _normalize_company(excluded)
    return bool(actual and excluded and (actual == excluded or excluded in actual))


def _normalize_location(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _normalize_employment(value: str) -> str:
    text = re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())
    aliases = {
        "fulltime": "full_time", "permanent": "full_time", "permanent_full_time": "full_time",
        "parttime": "part_time", "intern": "internship", "apprentice": "apprenticeship",
        "temp": "temporary",
    }
    return aliases.get(text, text)


def _job_employment(job: Job, text: str) -> Optional[str]:
    if job.employment_type:
        return _normalize_employment(job.employment_type.value)
    patterns = (
        ("internship", r"\b(?:internships?|intern)\b"),
        ("apprenticeship", r"\bapprenticeships?\b"),
        ("trainee", r"\btrainees?\b"),
        ("part_time", r"\bpart[\s-]?time\b"),
        ("contract", r"\bcontract(?:ual)?\b"),
        ("temporary", r"\btemporary\b"),
        ("full_time", r"\bfull[\s-]?time\b"),
    )
    return next((kind for kind, pattern in patterns if re.search(pattern, text, re.I)), None)


def _seniority_rank(value: str) -> int:
    from hirevia.core.matching import SENIORITY_RANK

    token = str(value or "").strip().casefold()
    return next((rank for name, rank in SENIORITY_RANK.items() if token.startswith(name)), 0)


def _seniority_is_excluded(value: str, keywords: List[str]) -> bool:
    return any(keyword.strip().casefold() == value.casefold() for keyword in keywords)