"""YAML-driven keyword matching, independent of candidate eligibility and AI."""

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hirevia.core.classification import (
    DEFAULT_P1_THRESHOLD,
    DEFAULT_P2_THRESHOLD,
    DEFAULT_P3_THRESHOLD,
    classify_score,
)
from hirevia.core.models import Job
from hirevia.core.skills import extract_jd_skills, normalize_skill


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

    p1_threshold: int = Field(default=DEFAULT_P1_THRESHOLD, ge=1, le=100)
    p2_threshold: int = Field(default=DEFAULT_P2_THRESHOLD, ge=1, le=99)
    p3_threshold: int = Field(default=DEFAULT_P3_THRESHOLD, ge=0, le=98)
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
    weighted_match_percentage: Optional[float] = None
    mandatory_keywords: List[str] = Field(default_factory=list)
    optional_keywords: List[str] = Field(default_factory=list)
    missing_mandatory_keywords: List[str] = Field(default_factory=list)
    missing_optional_keywords: List[str] = Field(default_factory=list)
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
            _normalize_phrase(keyword), normalize_skill(keyword)
        )
        for keyword in configuration.candidate.keywords
        if _normalize_phrase(keyword)
    }
    keywords: List[str] = []
    seen = set()
    for keyword in configuration.candidate.keywords:
        key = _normalize_phrase(keyword)
        canonical = canonical_by_key.get(key, normalize_skill(keyword))
        canonical_key = _normalize_phrase(canonical)
        if canonical_key and canonical_key not in seen:
            seen.add(canonical_key)
            keywords.append(canonical)
    return keywords


def classify_keyword_priority(
    percentage: float, configuration: KeywordConfiguration
) -> KeywordPriority:
    """Classify a percentage using the configured, non-overlapping cutoffs."""
    thresholds = configuration.matching
    return classify_score(
        percentage,
        thresholds.p1_threshold,
        thresholds.p2_threshold,
        thresholds.p3_threshold,
    )



def match_keywords(
    job: Job,
    configuration: KeywordConfiguration,
    candidate_skills: Optional[Sequence[str]] = None,
) -> KeywordMatchResult:
    """Score the candidate against distinct technical skills mentioned in the JD."""
    candidate_keywords = list(candidate_skills) if candidate_skills is not None else _canonical_keywords(configuration)
    candidate_set = {normalize_skill(keyword) for keyword in candidate_keywords if normalize_skill(keyword)}

    content_parts: List[str] = []
    for field_name in configuration.matching.fields_to_match:
        value = getattr(job, field_name, None)
        if field_name == "skills":
            content_parts.extend(str(item) for item in value or [] if str(item).strip())
        elif isinstance(value, str) and value.strip():
            content_parts.append(value)
    content = "\n".join(content_parts)
    requirements = extract_jd_skills(content, getattr(job, "title", ""))
    extracted_keys = {normalize_skill(skill) for skill, _ in requirements}
    normalized_content = _normalize_phrase(content)
    for canonical, aliases in configuration.matching.keyword_aliases.items():
        skill = normalize_skill(canonical)
        if skill in extracted_keys or skill not in candidate_set:
            continue
        forms = [canonical, *aliases]
        if any(
            re.search(
                rf"(?<![a-z0-9+#]){re.escape(_normalize_phrase(form))}(?![a-z0-9+#])",
                normalized_content,
            )
            for form in forms
            if _normalize_phrase(form)
        ):
            requirements.append((skill, True))
            extracted_keys.add(skill)
    if not requirements:
        return KeywordMatchResult(
            match_percentage=None,
            priority=None,
            matched_keywords=[],
            unmatched_keywords=[],
            matched_keyword_count=0,
            total_keyword_count=0,
            status="unscored",
        )
    matched = [skill for skill, _ in requirements if skill in candidate_set]
    missing = [skill for skill, _ in requirements if skill not in candidate_set]
    mandatory = [skill for skill, required in requirements if required]
    optional = [skill for skill, required in requirements if not required]
    missing_mandatory = [skill for skill in mandatory if skill in missing]
    missing_optional = [skill for skill in optional if skill in missing]
    percentage = len(matched) / len(requirements) * 100
    total_weight = sum(2 if required else 1 for _, required in requirements)
    matched_weight = sum(2 if required else 1 for skill, required in requirements if skill in candidate_set)
    weighted_percentage = matched_weight / total_weight * 100
    return KeywordMatchResult(
        match_percentage=percentage,
        priority=classify_keyword_priority(percentage, configuration),
        matched_keywords=matched,
        unmatched_keywords=missing,
        matched_keyword_count=len(matched),
        total_keyword_count=len(requirements),
        weighted_match_percentage=weighted_percentage,
        mandatory_keywords=mandatory,
        optional_keywords=optional,
        missing_mandatory_keywords=missing_mandatory,
        missing_optional_keywords=missing_optional,
    )