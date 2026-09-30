"""Hybrid profile matching engine (Phase 7).

Two halves, one verdict:

  1. deterministic evidence - what the posting provably contains, scored
     against the candidate profile with a fixed, auditable weight table;
  2. AI semantic judgement - what the posting *means* (seniority, role intent,
     transferability), supplied separately by hirevia.ai.matcher.

`analyze()` runs half 1 and never touches the network. `match_job()` merges
both halves into one MatchResult. Keeping them separate means a scan still
produces explainable scores when the model is down, and every number shown to
the user can be traced back to a line in the posting.

Weight table (deterministic half, sums to 100):
    skills 45 | role 18 | location 12 | experience 10 | remote 8 | employment 4 | company 3

A dimension the candidate never expressed an opinion on is scored NEUTRAL
(60% of its weight) rather than 0 - silence is not a rejection.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from hirevia.core.models import (
    AIResponse,
    CandidateProfile,
    EmploymentType,
    Job,
    MatchResult,
    Priority,
)
from hirevia.core.skills import (
    canonical_set,
    find_skills,
    normalize_skill,
    skill_in_text,
    transferable_matches,
)

logger = logging.getLogger(__name__)

WEIGHTS: Dict[str, int] = {
    "skills": 45,
    "role": 18,
    "location": 12,
    "experience": 10,
    "remote": 8,
    "employment": 4,
    "company": 3,
}

NEUTRAL = 0.6          # share of a weight awarded when no preference was declared
TRANSFERABLE_CREDIT = 0.15   # per transferable skill, as a share of the skill weight
MAX_SKILLS_SHOWN = 12
REJECTED_SCORE_CAP = 40      # a hard veto can never present a score above this

# Profile "role families" -> words that make a job title belong to that family.
ROLE_FAMILY_KEYWORDS: Dict[str, List[str]] = {
    "python_backend": ["backend", "back end", "back-end", "python", "fastapi", "flask", "django", "api"],
    "ai_ml": ["machine learning", "ml", "ai ", "artificial intelligence", "data scientist",
              "genai", "generative ai", "llm", "nlp", "deep learning", "ai/ml"],
    "data": ["data analyst", "data science", "data engineer", "analytics", "business intelligence", "bi "],
    "full_stack": ["full stack", "fullstack", "full-stack", "frontend", "front-end", "web developer"],
    "devops": ["devops", "sre", "site reliability", "platform engineer", "cloud engineer", "infrastructure"],
    "qa": ["qa", "quality", "test engineer", "testing"],
}

CITY_ALIASES: Dict[str, List[str]] = {
    "bengaluru": ["bangalore"],
    "mumbai": ["bombay"],
    "new delhi": ["delhi"],
    "delhi ncr": ["ncr", "gurgaon", "gurugram", "noida"],
    "hyderabad": ["hyd"],
    "pune": [],
    "chennai": ["madras"],
}

# Only encode containment for cities already named in CITY_ALIASES; nearby
# places not listed there remain distinct rather than being guessed equivalent.
CITY_REGIONS: Dict[str, set[str]] = {
    "bengaluru": {"karnataka", "india"},
    "mumbai": {"maharashtra", "india"},
    "new delhi": {"india"},
    "delhi ncr": {"india"},
    "hyderabad": {"telangana", "india"},
    "pune": {"maharashtra", "india"},
    "chennai": {"tamil nadu", "india"},
}

# A preferred location that covers a whole country/region rather than a place.
# Matching one of these is real but weaker evidence than matching a named city,
# so it scores below an exact hit instead of tying with it.
BROAD_LOCATIONS = {
    "india", "indian", "maharashtra", "karnataka", "telangana", "tamil nadu",
    "kerala", "gujarat", "rajasthan", "west bengal", "punjab", "haryana",
    "uttar pradesh", "odisha", "jharkhand", "chhattisgarh", "assam", "asia",
}
_ANYWHERE_PREFERENCES = {"remote", "anywhere", "worldwide", "global", "any",
                         "any location", "work from home", "wfh"}
_UNKNOWN_LOCATIONS = {
    "unknown", "n/a", "na", "not specified", "not provided", "not disclosed",
    "location not specified", "location unavailable", "tbd", "to be determined",
    "various", "multiple locations", "undisclosed",
}


def _normalize_location(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _contains_location(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text))


def _is_broad_location(token: str) -> bool:
    """Does this preferred location cover an area rather than a specific place?

    Note a city that merely has aliases (bengaluru/bangalore) is still
    specific - the alias list exists to catch spelling variants, not to widen
    the match.
    """
    token = (token or "").strip().casefold()
    if not token:
        return False
    if any(word in token for word in ("remote", "anywhere", "worldwide", "global")):
        return True
    return token in BROAD_LOCATIONS

_REMOTE_WORDS = ("remote", "work from home", "wfh", "anywhere", "location independent", "home based", "home-based")
_HYBRID_WORDS = ("hybrid", "2 days", "3 days", "flexible location")
_ONSITE_WORDS = ("onsite", "on-site", "on site", "office based", "in office")

# --------------------------------------------------------------------------
# Seniority
#
# A title's seniority band is a first-class deterministic signal. The spec
# treats Senior/Lead/Principal/Staff/Manager as STRONG negatives, and filters.yaml
# is optional - with a blank filter file its exclude_keywords are empty, so
# without this a "Senior Python Developer" used to score 94/100 for a fresher.
#
# Matching is whole-word: "Lead" must not veto "leading a team" or
# "leadership" (the same lesson filtering.py learned in Phase 6).
# --------------------------------------------------------------------------

SENIORITY_LADDER: Dict[str, int] = {
    "intern": 0, "trainee": 0, "fresher": 0, "graduate": 0,
    "junior": 1, "entry": 1,
    "mid": 2, "intermediate": 2,
    "senior": 3, "sr": 3,
    "staff": 4, "lead": 4, "principal": 4, "manager": 4, "architect": 4,
    "head": 5, "director": 5, "vp": 5, "chief": 5,
}
SENIORITY_RANK: Dict[str, int] = {name: rank for name, rank in SENIORITY_LADDER.items()}

_SENIORITY_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(SENIORITY_LADDER, key=len, reverse=True)) + r")(?:s)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# "experience preferred but not mandatory" softens a stated range.
_SOFT_EXPERIENCE_RE = re.compile(
    r"(preferred\s+but\s+not\s+(?:mandatory|required)|not\s+mandatory|"
    r"preferred\s*\)?\s*(?:but|ideally|preferred)|good\s+to\s+have|"
    r"nice\s+to\s+have|bonus|plus)",
    re.IGNORECASE,
)

_EXPERIENCE_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|to|–|~)\s*(\d{1,2})\s*(?:\+)?\s*(?:years?|yrs?|y\.o\.e\.?)",
    re.IGNORECASE,
)
_EXPERIENCE_SINGLE = re.compile(
    r"(\d{1,2})\s*(?:\+)?\s*(?:years?|yrs?|y\.o\.e\.?)",
    re.IGNORECASE,
)
_EXPERIENCE_ANY = re.compile(
    r"(fresher|freshers|internship|intern\b|interns\b|no experience|"
    r"fresh\s*grad(?:uate)?s?|graduates?|entry[\s-]?level|entry\s*level|"
    r"(?<!\d)0\s*(?:\+|-|to|–)\s*\d{1,2}\s*(?:years?|yrs?)|(?<!\d)0\s*(?:\+)?\s*(?:years?|yrs?)|"
    r"campus\s+hiring|campus\s+recruit|"
    r"recent\s*graduates?|no\s+prior\s+experience)",
    re.IGNORECASE,
)

# A bare "N years" is only a REQUIREMENT when it sits next to requirement
# language. Without this guard "this company has been hiring for 10 years" or
# "raised funding 3 years ago" parsed as a 10-year / 3-year requirement, which
# hard-rejected a perfectly good fresher job.
_EXPERIENCE_REQUIREMENT_CUE = re.compile(
    r"(experience|exp\b|work\s*exp|exposure|background|knowledge|proficiency|"
    r"hands[\s-]on|familiarity|with\s+at\s+least|at\s+least|minimum|min\b|"
    r"min\.?\s*\d|up\s+to|around|about|require[sd]?|requiring|qualif|"
    r"strong|good|excellent|solid|proven|ability|able\s+to|good\s+hands)",
    re.IGNORECASE,
)
# Contexts that make a number explicitly NOT a job requirement.
_EXPERIENCE_NON_REQUIREMENT_CUE = re.compile(
    r"(hiring\s+for|we(?:'ve| have)?\s+been|has\s+been|have\s+been|been\s+(?:hiring|serving|operating|working)|"
    r"founded|in\s+business|since\s+\d{4}|established|old\s+of|years\s+old|"
    r"raised|funding|seed\s+round|series\s+[a-c]\b|valuation|ipo|start(?:ed|up)|"
    r"anniversary|crore|lakh|turnover|revenue|clients?|customers?|"
    r"since\s+its\s+inception|celebrat)",
    re.IGNORECASE,
)


def _requirement_context(text: str, start: int, end: int, radius: int = 70) -> Optional[bool]:
    """Is the "N years" at [start:end) a requirement, a non-requirement, or unknown?

    Returns True/False when the surrounding prose decides it, else None - and
    "unknown" is treated as a requirement, so a bare "2 years" in a terse post
    still counts.
    """
    window = text[max(0, start - radius):end + radius]
    if _EXPERIENCE_NON_REQUIREMENT_CUE.search(window):
        return False
    if _EXPERIENCE_REQUIREMENT_CUE.search(window):
        return True
    return None


def job_signal_text(job: Job) -> str:
    """Everything the posting says, in one lower-case haystack."""
    parts = [job.title, job.company, job.location, job.experience or "", job.description or ""]
    if job.employment_type:
        parts.append(job.employment_type.value.replace("_", " "))
    parts.extend(job.skills or [])
    return "\n".join(str(p) for p in parts if p).casefold()


def job_skills(job: Job, limit: int = 18) -> List[str]:
    """Canonical skills the posting mentions (used for prompts + missing-skill notes)."""
    text = job_signal_text(job)
    declared = job.skills or []
    found = find_skills(text, list(canonical_set(declared)), cap=limit)
    remaining = limit - len(found)
    if remaining > 0:
        from hirevia.core.skills import SKILL_ALIASES  # local import: keeps module import cheap

        extra = find_skills(text, [s for s in SKILL_ALIASES if s not in found], cap=remaining)
        found.extend(extra)
    return found


def parse_experience(text: str) -> Optional[Tuple[int, int]]:
    """(min, max) years the posting asks for, or None when it never says.

    Only requirement-context numbers count. A company that "has been hiring for
    10 years" or "raised funding 3 years ago" is describing itself, not the
    job, and must not be read as a 10-year bar on a fresher.
    """
    window = text[:2500]

    ranges = []
    for match in _EXPERIENCE_RANGE.finditer(window):
        low, high = int(match.group(1)), int(match.group(2))
        if high < low or high > 30:
            continue
        if _requirement_context(window, match.start(), match.end()) is False:
            continue
        ranges.append((low, high))
    if ranges:
        return min(r[0] for r in ranges), max(r[1] for r in ranges)

    singles = []
    for match in _EXPERIENCE_SINGLE.finditer(window):
        years = int(match.group(1))
        if not 0 <= years <= 30:
            continue
        if _requirement_context(window, match.start(), match.end()) is False:
            continue
        singles.append(years)
    if singles:
        low = min(singles)
        return low, max(singles) if len(singles) > 1 else min(low + 1, 30)

    if _EXPERIENCE_ANY.search(window):
        return 0, 1
    return None


def is_soft_experience(text: str) -> bool:
    """True when the range is a preference rather than a hard bar.

    "1-2 years, experience preferred but not mandatory" is explicitly listed as
    potentially acceptable, so it must not be scored like a strict requirement.
    """
    return bool(_SOFT_EXPERIENCE_RE.search((text or "")[:2500]))


def job_seniority(title: str) -> Optional[str]:
    """Highest seniority band stated in a job title, or None if it states none.

    Whole-word only, so "Lead Backend Developer" is lead while "leading a
    team" is not.
    """
    found = {match.group(1).lower() for match in _SENIORITY_RE.finditer(title or "")}
    if not found:
        return None
    return max(found, key=lambda name: SENIORITY_RANK.get(name, 0))


def seniority_ceiling(profile: CandidateProfile) -> int:
    """The highest band the candidate will consider.

    An explicit max_seniority wins. Otherwise it is derived, so a profile
    never has to restate what candidate_type already says.
    """
    explicit = (profile.max_seniority or "").strip().lower()
    if explicit:
        for name, rank in SENIORITY_RANK.items():
            if explicit.startswith(name) or name.startswith(explicit):
                return rank
    if profile.experience_years >= 6 or profile.candidate_type.casefold() == "experienced":
        return SENIORITY_RANK["senior"]
    if profile.experience_years >= 3:
        return SENIORITY_RANK["mid"]
    return SENIORITY_RANK["junior"]


def _contains_any(text: str, words) -> bool:
    return any(word in text for word in words)


def remote_mode(text: str) -> Optional[str]:
    """'remote' | 'hybrid' | 'onsite' | None when the posting never says."""
    if _contains_any(text, _REMOTE_WORDS):
        return "remote"
    if _contains_any(text, _HYBRID_WORDS):
        return "hybrid"
    if _contains_any(text, _ONSITE_WORDS):
        return "onsite"
    return None


def job_remote_mode(job: Job) -> Optional[str]:
    """Location is authoritative; only fall back to the description when the
    location alone does not say (a posting can mention 'remote team' in prose)."""
    mode = remote_mode((job.location or "").casefold())
    if mode:
        return mode
    employment = (job.employment_type.value.replace("_", " ") if job.employment_type else "")
    mode = remote_mode(f"{employment} {job.experience or ''}".casefold())
    return mode or remote_mode(job_signal_text(job))

@dataclass
class MatchEvidence:
    """The deterministic half of a verdict: signals, weights, and vetoes."""

    signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    text: str = ""                       # the haystack every signal was judged on
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    transferable: List[Tuple[str, str]] = field(default_factory=list)
    job_skills: List[str] = field(default_factory=list)
    experience_match: Optional[bool] = None
    location_match: Optional[bool] = None
    role_match: Optional[bool] = None
    education_match: Optional[bool] = None
    employment_match: Optional[bool] = None
    remote_mode: Optional[str] = None
    reject_reasons: List[str] = field(default_factory=list)

    @property
    def hard_reject(self) -> bool:
        return bool(self.reject_reasons)

    def add(self, name: str, share: float, note: str) -> None:
        weight = WEIGHTS.get(name, 0)
        self.signals[name] = {
            "weight": weight,
            "score": round(weight * max(0.0, min(1.0, share)), 1),
            "note": note,
        }

    @property
    def score(self) -> int:
        total = int(round(sum(signal["score"] for signal in self.signals.values())))
        return max(0, min(100, total))

    @property
    def strongest_note(self) -> str:
        if not self.signals:
            return ""
        return max(self.signals.values(), key=lambda s: s["score"])["note"]

    def reason(self) -> str:
        if self.reject_reasons:
            return "; ".join(self.reject_reasons[:2])
        return self.strongest_note


def analyze(job: Job, profile: CandidateProfile) -> MatchEvidence:
    """Deterministic evidence for one job. Pure function, no I/O."""
    evidence = MatchEvidence()
    text = job_signal_text(job)
    evidence.text = text
    evidence.job_skills = job_skills(job)

    _score_skills(profile, text, evidence)
    _score_role(job, profile, evidence)
    _score_experience(profile, text, evidence)
    _score_location(job, profile, evidence)
    _score_remote(job, profile, evidence)
    _score_employment(job, profile, evidence)
    _score_company(job, profile, evidence)
    _education_signal(profile, text, evidence)
    return evidence


# --------------------------------------------------------------------------
# Individual signals
# --------------------------------------------------------------------------

def _profile_skills(profile: CandidateProfile) -> List[str]:
    seen, out = set(), []
    for skill in profile.skills:
        canonical = normalize_skill(skill)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def _score_skills(profile: CandidateProfile, text: str, evidence: MatchEvidence) -> None:
    wanted = _profile_skills(profile)
    if not wanted:
        evidence.add("skills", NEUTRAL, "profile declares no skills - skill relevance is neutral")
        return

    matched = find_skills(text, wanted)
    missing = [skill for skill in wanted if skill not in matched]
    evidence.matched_skills = matched
    evidence.missing_skills = missing[:MAX_SKILLS_SHOWN]

    # Transferable evidence: the posting wants something in a family the
    # candidate already works in (job wants Django, candidate has FastAPI).
    pairs = transferable_matches(missing, evidence.job_skills + matched)
    evidence.transferable = pairs[:6]

    if not matched:
        share = min(0.35, TRANSFERABLE_CREDIT * len(pairs)) if pairs else 0.0
        note = (
            "no profile skill mentioned"
            if not pairs
            else "no direct hit, but " + ", ".join(f"{have} ~ {wants}" for wants, have in pairs[:2])
        )
        evidence.add("skills", share, note)
        return

    ratio = len(matched) / len(wanted)
    strength = max(ratio, min(1.0, len(matched) / 3))  # 3 real hits = full credit
    bonus = TRANSFERABLE_CREDIT * min(1.0, len(pairs) / 2)
    evidence.add(
        "skills",
        min(1.0, strength + bonus),
        f"{len(matched)}/{len(wanted)} profile skills mentioned: {', '.join(matched[:6])}"
        + (f" (+{len(pairs)} transferable)" if pairs else ""),
    )


def _score_role(job: Job, profile: CandidateProfile, evidence: MatchEvidence) -> None:
    title = (job.title or "").casefold()
    haystack = f"{title} {job.company or ''}".casefold()

    for avoided in profile.avoid_roles or []:
        token = str(avoided).strip().casefold()
        if token and token in title:
            evidence.reject_reasons.append(f"'{avoided}' is a role you avoid")

    # Seniority is part of role fit, so it is scored inside this signal rather
    # than as a new weight: the table keeps summing to 100.
    seniority_penalty, seniority_note = _seniority_adjustment(job, profile, evidence)
    share = 1.0 + seniority_penalty

    roles = [str(role).strip().casefold() for role in profile.preferred_roles or [] if str(role).strip()]
    if not roles:
        evidence.role_match = None
        evidence.add("role", min(1.0, share) if seniority_penalty else NEUTRAL,
                     seniority_note or "profile declares no preferred roles")
        return

    if any(role in title for role in roles):
        evidence.role_match = True
        evidence.add("role", min(1.0, share), seniority_note or f"'{job.title}' is a role you target")
        return

    families = {str(f).strip().casefold() for f in profile.role_families or [] if str(f).strip()}
    keywords: List[str] = []
    for family in sorted(families):
        keywords.extend(ROLE_FAMILY_KEYWORDS.get(family, [family.replace("_", " ")]))
    hit = next((word for word in keywords if word and word in haystack), None)
    if hit:
        evidence.role_match = True
        track = ", ".join(sorted(families)) if families else "target"
        evidence.add("role", min(1.0, 0.65 + seniority_penalty),
                     seniority_note or f"title belongs to your {track} track ('{hit.strip()}')")
        return

    evidence.role_match = False
    evidence.add("role", min(1.0, 0.1 + seniority_penalty),
                 seniority_note or f"'{job.title}' is outside the roles you target")


def _seniority_adjustment(
    job: Job, profile: CandidateProfile, evidence: MatchEvidence
) -> Tuple[float, str]:
    """Score adjustment (and note) for a title above/below the candidate's band.

    Above the ceiling is a veto, not a discount: "Senior Python Developer" must
    never be presented as a near-match to someone targeting junior roles, and
    filters.yaml - the layer that used to catch this - is optional.
    """
    band = job_seniority(job.title or "")
    if band is None:
        return 0.0, ""

    level = SENIORITY_RANK.get(band, 0)
    ceiling = seniority_ceiling(profile)

    if level > ceiling:
        evidence.reject_reasons.append(
            f"'{band}' role, you target up to {profile.candidate_type or 'entry'} level"
        )
        return -0.6, f"'{job.title}' is a {band} role, above the level you target"
    if level == ceiling:
        return 0.0, f"'{job.title}' matches the seniority you target"
    return 0.0, f"'{job.title}' is a {band} role, within your level"



def _score_experience(profile: CandidateProfile, text: str, evidence: MatchEvidence) -> None:
    asked = parse_experience(text)
    if asked is None:
        evidence.experience_match = None
        evidence.add("experience", NEUTRAL, "posting does not state an experience range")
        return

    low, high = asked
    have = max(0, profile.experience_years)
    ceiling = max(profile.target_max_experience_years, have)
    # "1-2 years, experience preferred but not mandatory" is a preference, so it
    # is never treated as the hard bar a strict range would be.
    soft = is_soft_experience(text)

    if low <= have:
        evidence.experience_match = True
        if high <= ceiling + 1:
            evidence.add("experience", 1.0, f"{low}-{high} yrs fits your {have} yr(s) of experience")
        else:
            evidence.add("experience", 0.75, f"{low}-{high} yrs, above the {ceiling} yr band you target")
        return

    if low <= 1 and profile.accept_fresher:
        evidence.experience_match = True
        evidence.add("experience", 0.8, f"open to freshers ({low}-{high} yrs asked)")
        return

    # A stated-but-soft range just above the bar is a near miss, not a wall.
    if soft and low <= ceiling + 1:
        evidence.experience_match = True
        evidence.add("experience", 0.55,
                     f"{low}-{high} yrs preferred but not mandatory - still open to you")
        return

    if low > ceiling + 3:
        evidence.experience_match = False
        evidence.reject_reasons.append(f"asks {low}+ years, you target up to {ceiling}")
        evidence.add("experience", 0.0, f"asks {low}-{high} years - far beyond your target")
        return

    evidence.experience_match = False
    evidence.add(
        "experience",
        0.2 if low - have <= 2 else 0.05,
        f"asks {low}-{high} years, you have {have}",
    )


def _expand_location(token: str) -> List[str]:
    token = _normalize_location(token)
    for city, aliases in CITY_ALIASES.items():
        if token == city or token in aliases:
            return [city, *[alias for alias in aliases if alias != city]]
    return [token]


def _has_named_city(location: str) -> bool:
    return any(
        _contains_location(location, token)
        for city, aliases in CITY_ALIASES.items()
        for token in (city, *aliases)
    )


def _city_regions(preference: str) -> set[str]:
    expanded = _expand_location(preference)
    city = next((name for name in CITY_REGIONS if name in expanded), None)
    return CITY_REGIONS.get(city, set())


def _score_location(job: Job, profile: CandidateProfile, evidence: MatchEvidence) -> None:
    evidence.remote_mode = job_remote_mode(job)
    location = _normalize_location(job.location)
    prefs = [_normalize_location(p) for p in profile.preferred_locations or [] if _normalize_location(p)]

    if not prefs:
        evidence.location_match = None
        evidence.add("location", NEUTRAL,
                     f"profile declares no preferred locations ({job.location or 'unknown'})")
        return

    open_world = any(pref in _ANYWHERE_PREFERENCES for pref in prefs)

    # An exact preferred place is a strong signal; a job that only matches a
    # BROAD preference (a country or "remote") is weaker. Without this split,
    # a profile listing "India" scored Pune, Bangalore and Mumbai identically -
    # the location signal became a constant and could not discriminate.
    broad_hit: Optional[str] = None
    for pref in prefs:
        aliases = [a for a in _expand_location(pref) if a]
        if not any(_contains_location(location, alias) for alias in aliases):
            continue
        if _is_broad_location(pref):
            broad_hit = broad_hit or pref
            continue
        evidence.location_match = True
        evidence.add("location", 1.0, f"'{job.location}' is on your location list")
        return

    if evidence.remote_mode == "remote":
        evidence.location_match = None
        evidence.add("location", 0.9 if open_world else 0.55,
                     f"remote role{'' if open_world else ' - not one of your cities'}")
        return

    if evidence.remote_mode == "hybrid":
        location = re.sub(r"\bhybrid\b", " ", location)
        location = re.sub(r"\s+", " ", location).strip(" ,/-")

    if not location or location in _UNKNOWN_LOCATIONS or not re.search(r"[a-z0-9]", location):
        evidence.location_match = None
        evidence.add("location", NEUTRAL, "posting does not state a location")
        return

    if broad_hit is None and not _has_named_city(location):
        for pref in prefs:
            contained_regions = _city_regions(pref)
            broad_hit = next(
                (region for region in sorted(contained_regions)
                 if _contains_location(location, region)),
                None,
            )
            if broad_hit:
                break

    if broad_hit:
        evidence.location_match = None
        evidence.add("location", 0.7,
                     f"'{job.location}' is inside {broad_hit}, but not one of your specific cities")
        return

    if open_world:
        evidence.location_match = None
        evidence.add("location", 0.75, f"'{job.location}' is not listed, but you accept remote/anywhere")
        return

    evidence.location_match = False
    evidence.add("location", 0.05, f"'{job.location}' is outside {', '.join(prefs[:4])}")



def _score_remote(job: Job, profile: CandidateProfile, evidence: MatchEvidence) -> None:
    mode = evidence.remote_mode or job_remote_mode(job)
    evidence.remote_mode = mode
    preference = (profile.remote_preference or "any").strip().casefold()

    if mode is None:
        evidence.add("remote", NEUTRAL, "posting does not say remote/hybrid/onsite")
        return
    if preference == "only":
        if mode == "remote":
            evidence.add("remote", 1.0, "remote, exactly what you want")
        elif mode == "hybrid":
            evidence.add("remote", 0.35, "hybrid, you only want remote")
        else:
            evidence.reject_reasons.append("onsite role, your remote preference is 'only'")
            evidence.add("remote", 0.0, f"{mode} role, you want remote only")
        return
    if preference == "preferred":
        share = {"remote": 1.0, "hybrid": 0.7}.get(mode, 0.35)
        evidence.add("remote", share, "remote, as preferred" if mode == "remote" else f"{mode} role (remote preferred)")
        return
    evidence.add("remote", 1.0 if mode != "onsite" else 0.8, f"{mode} role (no remote preference)")


def _normalize_company(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", str(name or "").casefold())
    stop = {"pvt", "private", "ltd", "limited", "llp", "inc", "technologies", "technology", "solutions", "the"}
    return " ".join(word for word in text.split() if word and word not in stop)


def _score_employment(job: Job, profile: CandidateProfile, evidence: MatchEvidence) -> None:
    allowed = {str(e).strip().casefold().replace(" ", "_").replace("-", "_")
               for e in profile.employment_types or [] if str(e).strip()}
    if not allowed:
        evidence.employment_match = None
        evidence.add("employment", NEUTRAL, "profile declares no employment-type preference")
        return

    job_type = job.employment_type.value if job.employment_type else None
    if job_type is None and "internship" in job_signal_text(job):
        job_type = EmploymentType.INTERNSHIP.value
    if job_type is None:
        evidence.employment_match = None
        evidence.add("employment", NEUTRAL, "posting does not state an employment type")
        return

    if job_type in allowed:
        evidence.employment_match = True
        evidence.add("employment", 1.0, f"{job_type.replace('_', ' ')} is what you want")
        return
    if job_type == EmploymentType.INTERNSHIP.value and profile.internship_preference:
        evidence.employment_match = True
        evidence.add("employment", 0.9, "internship, and you are open to internships")
        return

    evidence.employment_match = False
    evidence.add("employment", 0.0, f"{job_type.replace('_', ' ')} is not in your employment preferences")


def _score_company(job: Job, profile: CandidateProfile, evidence: MatchEvidence) -> None:
    company = _normalize_company(job.company)
    preferred = [c for c in (_normalize_company(x) for x in profile.preferred_companies or []) if c]
    avoided = [c for c in (_normalize_company(x) for x in profile.avoid_companies or []) if c]

    for bad in avoided:
        if bad in company or company in bad:
            evidence.reject_reasons.append(f"{job.company} is a company you avoid")
            evidence.add("company", 0.0, f"{job.company} is on your avoid list")
            return

    if not preferred:
        evidence.add("company", NEUTRAL, "profile declares no target companies")
        return

    if any(pref in company or company in pref for pref in preferred):
        evidence.add("company", 1.0, f"{job.company} is a target company")
        return

    evidence.add("company", 0.25, f"{job.company} is not one of your target companies")


_DEGREE_WORDS = ("b.e", "b.tech", "btech", "b.sc", "bsc", "bca", "bcom", "m.tech", "mtech",
                 "mca", "graduate", "graduation", "bachelor", "engineering", "degree holder")


def _education_signal(profile: CandidateProfile, text: str, evidence: MatchEvidence) -> None:
    """Informational only - never scored, but it belongs on the verdict."""
    if not (profile.degree or profile.education):
        evidence.education_match = None
    elif any(word in text for word in _DEGREE_WORDS):
        evidence.education_match = True
    elif "qualification" in text or "education" in text:
        evidence.education_match = False
    else:
        evidence.education_match = None
# --------------------------------------------------------------------------
# Merging the two halves
# --------------------------------------------------------------------------

def priority_for(score: int, config=None) -> Priority:
    p1 = getattr(config, "priority_p1_min_score", 80)
    p2 = getattr(config, "priority_p2_min_score", 62)
    if score >= p1:
        return Priority.P1
    if score >= p2:
        return Priority.P2
    return Priority.P3


def combine(
    evidence: MatchEvidence,
    config=None,
    ai: Optional[AIResponse] = None,
    ai_model: Optional[str] = None,
) -> MatchResult:
    """Deterministic evidence + (optional) AI answer -> the one canonical verdict."""
    deterministic = evidence.score
    ai_weight = min(max(float(getattr(config, "ai_match_weight", 0.4)), 0.0), 1.0)
    min_score = int(getattr(config, "match_min_score", 55))
    failed_cap = int(getattr(config, "ai_failed_score_cap", 70))

    if ai is None:
        status = "skipped"
    elif ai.degraded:
        status = "failed"
    else:
        status = "ok"

    ai_score = ai.match_score if (ai is not None and status == "ok") else None
    notes: List[str] = []

    if evidence.hard_reject:
        # A stated veto outranks any score: a rejected job is never presented as
        # a near-match, and the AI cannot talk its way past it.
        score = min(deterministic, REJECTED_SCORE_CAP)
        match = False
        notes.append("rejected: " + "; ".join(evidence.reject_reasons[:2]))
    elif status == "ok":
        blended = deterministic * (1 - ai_weight) + ai_score * ai_weight
        if not ai.relevant and blended >= min_score:
            blended -= 12  # the model explicitly said "not worth your time"
            notes.append("AI judged this not relevant")
        score = int(min(100, max(0, round(blended))))
        match = score >= min_score
    elif status == "failed":
        # No AI judgement: keep the deterministic result, but never present a
        # degraded verdict as high confidence.
        score = min(int(round(deterministic * 0.9)), failed_cap)
        match = score >= min_score
        notes.append(f"AI unavailable - deterministic evidence only ({ai.error or 'unknown error'})")
    else:
        score = deterministic
        match = score >= min_score

    priority = Priority.P3 if evidence.hard_reject else priority_for(score, config)
    if status != "ok" and priority is Priority.P1:
        priority = Priority.P2  # no model confirmation -> never the top banner
    elif status == "ok":
        # The model's own tier acts as a ceiling, never a floor: maths may say
        # P1, but if the model called it P3 we do not escalate it to P1.
        rank = {Priority.P1: 1, Priority.P2: 2, Priority.P3: 3}
        if rank[ai.priority] > rank[priority]:
            priority = ai.priority

    matched, missing = _reconcile_skills(evidence, ai if status == "ok" else None)

    reason = evidence.reason()
    if status == "ok" and ai.reason:
        reason = ai.reason
    if notes:
        reason = " | ".join([reason, *notes]) if reason else " | ".join(notes)

    raw_model = (ai.raw_data or {}).get("model") if ai is not None else None
    model = ai_model or (str(raw_model) if raw_model else None)
    signals: Dict[str, Any] = dict(evidence.signals)
    signals["total_deterministic"] = deterministic
    signals["weighting"] = {"ai": round(ai_weight, 2), "deterministic": round(1 - ai_weight, 2)}
    signals["transferable"] = [f"{have} ~ {wants}" for wants, have in evidence.transferable]
    signals["ai"] = {
        "status": status,
        "model": model,
        "score": ai_score,
        "priority": ai.priority.value if (ai is not None and status == "ok") else None,
        "relevant": ai.relevant if (ai is not None and status == "ok") else None,
        "error": ai.error if (ai is not None and status == "failed") else None,
    }

    return MatchResult(
        match=match,
        match_score=score,
        priority=priority,
        reason=reason[:500],
        matched_skills=matched,
        missing_skills=missing,
        experience_match=evidence.experience_match,
        location_match=evidence.location_match,
        role_match=evidence.role_match,
        education_match=evidence.education_match,
        employment_match=evidence.employment_match,
        ai_status=status,
        ai_model=model,
        ai_score=ai_score,
        ai_priority=ai.priority if (ai is not None and status == "ok") else None,
        ai_error=ai.error if (ai is not None and status == "failed") else None,
        deterministic_score=deterministic,
        hard_reject=evidence.hard_reject,
        reject_reasons=list(evidence.reject_reasons),
        signals=signals,
    )


def _reconcile_skills(evidence: MatchEvidence, ai: Optional[AIResponse]) -> Tuple[List[str], List[str]]:
    """Deterministic skills are the source of truth.

    The AI may only ADD a matched skill when the posting really contains it -
    checked against the posting text - so a hallucinated "Kubernetes match"
    cannot inflate the evidence shown to the user.
    """
    matched = list(evidence.matched_skills)
    known = set(matched) | set(evidence.missing_skills)

    if ai is not None:
        for claim in ai.matched_skills:
            canonical = normalize_skill(claim)
            if canonical and canonical not in matched and canonical in known:
                if skill_in_text(canonical, evidence.text):
                    matched.append(canonical)

    missing = [skill for skill in evidence.missing_skills if skill not in matched]
    return matched[:MAX_SKILLS_SHOWN], missing[:MAX_SKILLS_SHOWN]


def match_job(
    job: Job,
    profile: CandidateProfile,
    config=None,
    ai: Optional[AIResponse] = None,
    ai_model: Optional[str] = None,
) -> MatchResult:
    """One-call helper for tests, CLI previews and re-scoring stored jobs."""
    return combine(analyze(job, profile), config, ai, ai_model)


def evidence_prompt_block(evidence: MatchEvidence) -> str:
    """Deterministic findings handed to the model, so it judges meaning instead
    of redoing arithmetic it cannot see. Grounding the AI on verifiable facts
    measurably cuts hallucinated skill claims."""
    location_match = {True: "match", False: "mismatch", None: "unstated"}.get(
        evidence.location_match, "unstated"
    )
    location_score = evidence.signals.get("location", {}).get("score", 0)
    return "\n".join([
        f"deterministic_score: {evidence.score}/100",
        f"location_match: {location_match}",
        f"location_score: {location_score}/{WEIGHTS['location']}",
        f"your_skills_mentioned_in_posting: {', '.join(evidence.matched_skills) or 'none'}",
        f"your_skills_not_mentioned: {', '.join(evidence.missing_skills[:10]) or 'none'}",
        f"skills_named_in_posting: {', '.join(evidence.job_skills[:12]) or 'not detectable'}",
        f"work_mode: {evidence.remote_mode or 'unstated'}",
        "transferable_evidence: "
        + (", ".join(f"{have} instead of {wants}" for wants, have in evidence.transferable) or "none"),
    ])
