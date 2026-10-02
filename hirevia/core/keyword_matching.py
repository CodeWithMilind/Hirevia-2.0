"""YAML-driven keyword matching, independent of candidate eligibility and AI."""

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hirevia.core.models import Job


MatchField = Literal["title", "description", "skills"]
KeywordPriority = Literal["P1", "P2", "P3", "Unmatched"]


class CandidateKeywordPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    experience_level: str = "fresher"
    max_experience_years: Optional[float] = Field(default=1, ge=0)
    preferred_roles: List[str] = Field(default_factory=list)
    role_categories: List[str] = Field(default_factory=list)
    preferred_locations: List[str] = Field(default_factory=list)
    employment_types: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)


class KeywordMatchingPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    p1_threshold: int = Field(default=70, ge=1, le=100)
    p2_threshold: int = Field(default=40, ge=1, le=99)
    p3_threshold: int = Field(default=1, ge=0, le=98)
    fields_to_match: List[MatchField] = Field(
        default_factory=lambda: ["title", "description", "skills"]
    )
    keyword_aliases: Dict[str, List[str]] = Field(default_factory=dict)
    use_ai_for_scoring: bool = False

    @model_validator(mode="after")
    def validate_thresholds(self):
        if not self.p1_threshold > self.p2_threshold > self.p3_threshold:
            raise ValueError("matching thresholds must satisfy P1 > P2 > P3")
        if self.use_ai_for_scoring:
            raise ValueError("matching.use_ai_for_scoring must be false")
        return self


class KeywordConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate: CandidateKeywordPreferences = Field(
        default_factory=CandidateKeywordPreferences
    )
    matching: KeywordMatchingPreferences = Field(
        default_factory=KeywordMatchingPreferences
    )


class KeywordMatchResult(BaseModel):
    match_percentage: Optional[float]
    priority: Optional[KeywordPriority]
    matched_keywords: List[str] = Field(default_factory=list)
    unmatched_keywords: List[str] = Field(default_factory=list)
    matched_keyword_count: int
    total_keyword_count: int
    matching_method: Literal["yaml_keyword"] = "yaml_keyword"
    status: Literal["scored", "unscored"] = "scored"


def _normalize_phrase(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return " ".join(text.split())


def _legacy_profile_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt the older flat profile shape without consulting filters.yaml."""
    if "candidate" in data:
        return data

    candidate = {
        "experience_level": data.get("candidate_type", "fresher"),
        "max_experience_years": data.get("target_max_experience_years"),
        "preferred_roles": data.get("preferred_roles", []),
        "role_categories": data.get("role_categories", data.get("role_families", [])),
        "preferred_locations": data.get("preferred_locations", []),
        "employment_types": data.get("employment_types", []),
        "keywords": data.get("keywords", data.get("skills", [])),
    }
    return {"candidate": candidate, "matching": data.get("matching", {})}


def load_keyword_configuration(
    profile_path: Optional[str | Path] = None,
) -> KeywordConfiguration:
    """Load candidate and matching preferences from profile.yaml."""
    if profile_path is not None:
        path = Path(profile_path)
    else:
        path = Path(os.getenv("PROFILE_PATH") or Path.cwd() / "profile.yaml")

    if not path.exists():
        return KeywordConfiguration()
    try:
        with path.open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in {path.name}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must contain a YAML mapping")

    return parse_keyword_configuration(document)


def parse_keyword_configuration(document: Any) -> KeywordConfiguration:
    """Validate a parsed profile document, including alias targets."""
    if not isinstance(document, dict):
        raise ValueError("profile.yaml must contain a YAML mapping")
    configuration = KeywordConfiguration.model_validate(
        _legacy_profile_document(document)
    )
    configured = {
        _normalize_phrase(keyword)
        for keyword in configuration.candidate.keywords
        if _normalize_phrase(keyword)
    }
    for canonical, aliases in configuration.matching.keyword_aliases.items():
        if _normalize_phrase(canonical) not in configured:
            raise ValueError(
                f"keyword alias target {canonical!r} is not in candidate.keywords"
            )
        if any(not _normalize_phrase(alias) for alias in aliases):
            raise ValueError(f"keyword aliases for {canonical!r} cannot be empty")
    _canonical_keywords(configuration)
    return configuration


def _canonical_keywords(configuration: KeywordConfiguration) -> List[str]:
    alias_to_canonical: Dict[str, str] = {}
    for canonical, aliases in configuration.matching.keyword_aliases.items():
        for alias in aliases:
            key = _normalize_phrase(alias)
            existing = alias_to_canonical.get(key)
            if existing is not None and _normalize_phrase(existing) != _normalize_phrase(canonical):
                raise ValueError(f"keyword alias {alias!r} maps to multiple keywords")
            alias_to_canonical[key] = canonical.strip()

    canonical_by_key = {
        _normalize_phrase(keyword): alias_to_canonical.get(
            _normalize_phrase(keyword), keyword.strip()
        )
        for keyword in configuration.candidate.keywords
        if _normalize_phrase(keyword)
    }
    keywords: List[str] = []
    seen = set()
    for keyword in configuration.candidate.keywords:
        key = _normalize_phrase(keyword)
        canonical = canonical_by_key.get(key, keyword.strip())
        canonical_key = _normalize_phrase(canonical)
        if canonical_key and canonical_key not in seen:
            seen.add(canonical_key)
            keywords.append(canonical)
    return keywords


def _keyword_forms(keyword: str, configuration: KeywordConfiguration) -> List[str]:
    forms = [keyword]
    for canonical, aliases in configuration.matching.keyword_aliases.items():
        if _normalize_phrase(canonical) == _normalize_phrase(keyword):
            forms.extend(aliases)
    unique = []
    seen = set()
    for form in forms:
        normalized = _normalize_phrase(form)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _contains_keyword(text: str, forms: List[str]) -> bool:
    for form in forms:
        pattern = re.compile(
            rf"(?<![a-z0-9+#]){re.escape(form)}(?![a-z0-9+#])",
            re.IGNORECASE,
        )
        if pattern.search(text):
            return True
    return False


def classify_keyword_priority(
    percentage: float, configuration: KeywordConfiguration
) -> KeywordPriority:
    """Classify a percentage using the configured, non-overlapping cutoffs."""
    if percentage <= 0:
        return "Unmatched"
    thresholds = configuration.matching
    if percentage >= thresholds.p1_threshold:
        return "P1"
    if percentage >= thresholds.p2_threshold:
        return "P2"
    if percentage >= thresholds.p3_threshold:
        return "P3"
    return "Unmatched"



# Role-specific skill pools prevent unrelated candidate skills from diluting
# the percentage. A pool is intersected with candidate.keywords, so only
# skills the candidate actually listed can contribute to the denominator.
_ROLE_FAMILIES = {
    "python": ("python developer", "python engineer"),
    "backend": ("backend developer", "back end developer", "backend engineer", "back end engineer"),
    "ai_ml": ("ai engineer", "ai ml", "artificial intelligence", "machine learning", "ml engineer", "deep learning", "data scientist"),
    "data_analyst": ("data analyst", "business intelligence analyst", "bi analyst"),
    "frontend": ("frontend developer", "front end developer", "react developer", "ui developer"),
    "java": ("java developer", "java engineer"),
    "cloud": ("cloud engineer", "devops engineer", "site reliability engineer", "sre"),
    "qa": ("qa engineer", "quality assurance", "software tester", "test engineer"),
}

_ROLE_SKILL_POOLS = {
    "python": {"python", "django", "flask", "fastapi", "rest api", "sql", "mysql"},
    "backend": {"python", "fastapi", "flask", "django", "rest api", "sql", "mysql", "postgresql", "mongodb", "docker"},
    "ai_ml": {"python", "machine learning", "deep learning", "pandas", "numpy", "tensorflow", "pytorch"},
    "data_analyst": {"python", "sql", "pandas", "numpy", "excel", "power bi", "statistics"},
    "frontend": {"javascript", "react", "html", "css", "rest api", "git"},
    "java": {"java", "spring boot", "sql", "mysql", "rest api", "git"},
    "cloud": {"aws", "azure", "docker", "linux", "python", "ci cd"},
    "qa": {"selenium", "testing", "python", "java", "sql", "api testing"},
}


def _role_family(value: str) -> Optional[str]:
    """Map a job/profile role label to a broad, stable role family."""
    normalized = f" {_normalize_phrase(value)} "
    for family, labels in _ROLE_FAMILIES.items():
        for label in labels:
            if f" {_normalize_phrase(label)} " in normalized:
                return family
    return None


def _role_relevant_keywords(
    job: Job,
    configuration: KeywordConfiguration,
    keywords: List[str],
) -> List[str]:
    """Use a role-specific denominator only when the role is a preferred role.

    Unknown or non-preferred roles retain the full candidate keyword set. This
    avoids inflating scores for unrelated jobs while preventing unrelated
    skills from diluting a role the candidate explicitly wants.
    """
    job_family = _role_family(getattr(job, "title", "") or "")
    if not job_family:
        return keywords

    preferred_families = {
        family
        for role in configuration.candidate.preferred_roles
        if (family := _role_family(role))
    }
    if job_family not in preferred_families:
        return keywords

    allowed = _ROLE_SKILL_POOLS.get(job_family, set())
    relevant = [
        keyword for keyword in keywords
        if _normalize_phrase(keyword) in allowed
    ]
    return relevant or keywords

def match_keywords(job: Job, configuration: KeywordConfiguration) -> KeywordMatchResult:
    """Match role-relevant canonical keywords once against the selected job fields."""
    all_keywords = _canonical_keywords(configuration)
    keywords = _role_relevant_keywords(job, configuration, all_keywords)
    if not keywords:
        return KeywordMatchResult(
            match_percentage=None,
            priority=None,
            matched_keywords=[],
            unmatched_keywords=[],
            matched_keyword_count=0,
            total_keyword_count=0,
            status="unscored",
        )

    content_parts: List[str] = []
    for field_name in configuration.matching.fields_to_match:
        value = getattr(job, field_name, None)
        if field_name == "skills":
            content_parts.extend(str(item) for item in value or [] if str(item).strip())
        elif isinstance(value, str) and value.strip():
            content_parts.append(value)
    content = _normalize_phrase(" ".join(content_parts))

    matched = [
        keyword
        for keyword in keywords
        if _contains_keyword(content, _keyword_forms(keyword, configuration))
    ]
    matched_keys = {_normalize_phrase(keyword) for keyword in matched}
    unmatched = [
        keyword for keyword in keywords if _normalize_phrase(keyword) not in matched_keys
    ]
    percentage = len(matched) / len(keywords) * 100
    return KeywordMatchResult(
        match_percentage=percentage,
        priority=classify_keyword_priority(percentage, configuration),
        matched_keywords=matched,
        unmatched_keywords=unmatched,
        matched_keyword_count=len(matched),
        total_keyword_count=len(keywords),
    )