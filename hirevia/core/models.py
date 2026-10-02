"""Core data models for Hirevia"""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ApplicationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class Priority(str, Enum):
    P1 = "P1"  # High match
    P2 = "P2"  # Medium match
    P3 = "P3"  # Low match


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: HttpUrl
    application_url: Optional[HttpUrl] = None
    posted_at: Optional[datetime] = None
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None
    employment_type: Optional[EmploymentType] = None
    experience: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    raw_data: Dict[str, Any] = Field(default_factory=dict)
    application_status: ApplicationStatus = ApplicationStatus.UNKNOWN
    fingerprint: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    saved_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None
    notification_channel: Optional[str] = None
    saved: bool = False
    deleted_at: Optional[datetime] = None
    ai_decision: Optional[Dict[str, Any]] = None
    # Phase 7: the combined deterministic+AI match verdict (see MatchResult).
    # Stored as a plain dict so the schema stays a single JSON column.
    match_result: Optional[Dict[str, Any]] = None

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if not isinstance(other, Job):
            return False
        return self.id == other.id

    # -- verdict access ------------------------------------------------------
    # Every reader (CLI table, dashboard API, notifier, alert retry) goes
    # through these, so nobody re-invents the "which score is the real one"
    # rule: Phase 7's match_result wins, the older AI-only answer is the
    # fallback, and "never matched" stays distinguishable from "scored 0".

    def verdict(self) -> Dict[str, Any]:
        """The score/priority to display, in dict form."""
        verdict = dict(self.match_result or self.ai_decision or {})
        for payload in (self.match_result, self.ai_decision):
            if not isinstance(payload, dict):
                continue
            match = next((
                payload[key]
                for key in ("match", "matched", "relevant")
                if isinstance(payload.get(key), bool)
            ), None)
            if match is not None:
                verdict["match"] = match
                break
        return verdict

    def verdict_model(self) -> Optional["MatchResult"]:
        """The canonical MatchResult for a stored job, rebuilt from whichever
        payload exists. None when this job was never matched."""
        if self.match_result:
            try:
                return MatchResult(**self.match_result)
            except Exception:  # noqa: BLE001 - unreadable payload == no verdict
                pass
        if not self.ai_decision:
            return None
        try:
            decision = AIResponse(**self.ai_decision)
        except Exception:  # noqa: BLE001 - unreadable payload == no verdict
            return None
        return MatchResult(
            match=decision.relevant,
            match_score=decision.match_score,
            priority=decision.priority,
            reason=decision.reason or (decision.reasons[0] if decision.reasons else ""),
            matched_skills=decision.matched_skills,
            missing_skills=decision.missing_skills,
            experience_match=decision.experience_match,
            location_match=decision.location_match,
            role_match=decision.role_match,
            education_match=decision.education_match,
            employment_match=decision.employment_match,
            ai_status="failed" if decision.degraded else "ok",
            ai_model=(decision.raw_data or {}).get("model"),
            ai_score=decision.match_score,
            ai_error=decision.error,
            deterministic_score=decision.match_score,
        )


# --------------------------------------------------------------------------
# Canonical candidate profile (Phase 7)
#
# ONE representation of the candidate, loaded from profile.yaml. Nothing here
# is duplicated elsewhere: filters.yaml stays the *hard veto* layer, the
# profile is the *relevance* layer.
# --------------------------------------------------------------------------

class CandidateProfile(BaseModel):
    # Identity / education
    name: str = "Candidate"
    summary: str = ""
    education: str = ""                      # "B.E. Computer Engineering"
    degree: str = ""                         # "B.E/B.Tech" - used for education match
    graduation_year: Optional[int] = None

    # Where the candidate stands
    candidate_type: str = "fresher"          # fresher | entry_level | experienced
    experience_years: int = 0
    target_max_experience_years: int = 2     # postings above this are a weaker fit
    accept_fresher: bool = True
    internship_preference: bool = True       # internships are welcome
    # Highest job seniority the candidate will consider ("junior", "mid",
    # "senior", ...). Empty = derived from candidate_type + experience_years.
    # A posting titled above this is a veto, not a low score.
    max_seniority: str = ""

    # Skills. `skill_groups` keeps profile.yaml readable (AI/ML, backend, tools
    # ...); the groups are flattened into `skills` so there is exactly one
    # canonical skill list for matching, prompts and storage.
    skills: List[str] = Field(default_factory=list)
    skill_groups: Dict[str, List[str]] = Field(default_factory=dict)
    tools: List[str] = Field(default_factory=list)
    projects: List[str] = Field(default_factory=list)

    # Preferences
    preferred_roles: List[str] = Field(default_factory=list)
    role_families: List[str] = Field(default_factory=list)  # e.g. python_backend, ai_ml
    preferred_locations: List[str] = Field(default_factory=list)
    remote_preference: str = "any"           # any | preferred | only
    employment_types: List[str] = Field(default_factory=list)  # full_time, internship...
    preferred_companies: List[str] = Field(default_factory=list)
    avoid_companies: List[str] = Field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    notice_period_days: Optional[int] = None

    # Anything the candidate never wants to see, beyond filters.yaml.
    avoid_roles: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _flatten_skills(self) -> "CandidateProfile":
        """Merge skill_groups + tools into one canonical `skills` list."""
        merged: List[str] = []
        seen = set()
        for item in list(self.skills) + [
            s for group in self.skill_groups.values() for s in group
        ] + list(self.tools):
            text = str(item).strip()
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                merged.append(text)
        self.skills = merged
        return self

    @property
    def is_fresher_target(self) -> bool:
        return self.candidate_type.casefold() in ("fresher", "entry", "entry_level", "entry-level")


class AIResponse(BaseModel):
    relevant: Optional[bool] = None
    priority: Optional[Priority] = None
    match_score: Optional[int] = Field(default=None, ge=0, le=100)
    reasons: List[str] = Field(default_factory=list)
    # Which model produced this decision + the raw payload, for traceability.
    raw_data: Dict[str, Any] = Field(default_factory=dict)
    # True when the model could not be consulted (API/model failure) and the
    # pipeline continued on the documented fallback instead of a real decision.
    degraded: bool = False
    error: Optional[str] = None

    # --- Phase 7: the structured verdict (never rely on free-form text) -----
    reason: str = ""
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    experience_match: Optional[bool] = None
    location_match: Optional[bool] = None
    role_match: Optional[bool] = None
    education_match: Optional[bool] = None
    employment_match: Optional[bool] = None
    fresher_eligible: Optional[bool] = None
    rejection_reasons: List[str] = Field(default_factory=list)

    @property
    def status(self) -> str:
        """"failed" when the model was unreachable/unreadable, else "ok"."""
        return "failed" if self.degraded else "ok"

    @classmethod
    def from_json(cls, data: dict) -> "AIResponse":
        """Tolerant mapping of a model payload onto AIResponse.

        Models phrase the same verdict differently ("match" vs "relevant",
        "score" vs "match_score", "reason" vs "reasons", "high" vs "P1",
        "yes" vs true). Anything unusable degrades to a safe default instead of
        raising, so a malformed AI answer can never break a scan.
        """
        if not isinstance(data, dict):
            raise ValueError("AI response must be a JSON object")

        raw_relevant = next((data[key] for key in ("match", "relevant", "is_match", "is_relevant") if key in data), None)
        if not isinstance(raw_relevant, bool):
            raise ValueError("AI response is missing a boolean match decision")
        raw_score = next((data[key] for key in ("match_score", "score", "similarity") if key in data), None)
        if isinstance(raw_score, bool) or not isinstance(raw_score, int) or not 0 <= raw_score <= 100:
            raise ValueError("AI response must contain an integer match score from 0 to 100")
        raw_priority = data.get("priority", data.get("confidence_level"))
        if not isinstance(raw_priority, str) or raw_priority.strip().lower() not in _PRIORITY_ALIASES:
            raise ValueError("AI response must contain a valid priority")
        priority = _PRIORITY_ALIASES[raw_priority.strip().lower()]
        raw_reason = data.get("reason", data.get("explanation", data.get("summary")))
        if raw_reason is None:
            raw_reason = data.get("reasons", data.get("why"))
            if isinstance(raw_reason, list):
                raw_reason = next((item for item in raw_reason if isinstance(item, str) and item.strip()), None)
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            raise ValueError("AI response must contain a non-empty explanation")

        raw_reasons = data.get("reasons", raw_reason)
        if not isinstance(raw_reasons, str) and not (
            isinstance(raw_reasons, list)
            and all(isinstance(item, str) for item in raw_reasons)
        ):
            raise ValueError("AI response field reasons must be a string or an array of strings")
        reasons = _reasons_from(raw_reasons)
        matched_skills = data.get("matched_skills", data.get("skills_matched", []))
        missing_skills = data.get("missing_skills", data.get("skills_missing", []))
        rejection_reasons = data.get("rejection_reasons", [])
        for field_name, value in (
            ("matched_skills", matched_skills),
            ("missing_skills", missing_skills),
            ("rejection_reasons", rejection_reasons),
        ):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"AI response field {field_name} must be an array of strings")

        optional_bools = {}
        for field_name in (
            "experience_match", "location_match", "role_match", "education_match",
            "employment_match", "fresher_eligible",
        ):
            value = data.get(field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"AI response field {field_name} must be a boolean")
            optional_bools[field_name] = value

        return cls(
            relevant=raw_relevant,
            priority=priority,
            match_score=raw_score,
            reasons=reasons,
            reason=raw_reason.strip()[:400],
            matched_skills=_skill_list_from(matched_skills),
            missing_skills=_skill_list_from(missing_skills),
            rejection_reasons=rejection_reasons,
            **optional_bools,
        )

    @classmethod
    def failure(cls, error: object) -> "AIResponse":
        """The documented fallback when AI matching is unavailable.

        A failure carries no model decision or score; the job is retained for
        retry/manual review without manufacturing an AI result.
        """
        message = str(error).replace("\n", " ")[:180] if error is not None else "unknown error"
        return cls(
            relevant=None,
            priority=None,
            match_score=None,
            reasons=[],
            degraded=True,
            error=message,
        )


_PRIORITY_ALIASES = {
    "p1": Priority.P1, "high": Priority.P1, "urgent": Priority.P1, "strong": Priority.P1,
    "1": Priority.P1, "top": Priority.P1,
    "p2": Priority.P2, "medium": Priority.P2, "med": Priority.P2, "moderate": Priority.P2,
    "2": Priority.P2, "ok": Priority.P2,
    "p3": Priority.P3, "low": Priority.P3, "weak": Priority.P3, "maybe": Priority.P3,
    "3": Priority.P3, "none": Priority.P3,
}


def _priority_from(*values) -> Priority:
    for value in values:
        if isinstance(value, Priority):
            return value
        if isinstance(value, str):
            mapped = _PRIORITY_ALIASES.get(value.strip().lower())
            if mapped:
                return mapped
    return Priority.P3


def _score_from(value) -> int:
    try:
        if isinstance(value, bool):
            return 0
        if isinstance(value, str):
            digits = re.search(r"-?\d+(?:\.\d+)?", value)
            value = digits.group() if digits else 0
        score = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 < score <= 1:  # some models answer on a 0-1 scale
        score *= 100
    return max(0, min(100, int(round(score))))


def _reasons_from(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip(" -•\t") for p in re.split(r"[;\n]|\.\s+", value)]
        return [p for p in parts if p][:5] or [value.strip()[:300]]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip()[:300])
            elif isinstance(item, dict):
                text = item.get("reason") or item.get("text") or item.get("point")
                if text:
                    out.append(str(text).strip()[:300])
        return out[:5]
    return [str(value)[:300]]


def _skill_list_from(value) -> List[str]:
    """Normalise whatever the model produced for matched/missing skills."""
    if value is None:
        return []
    items: List[str] = []
    if isinstance(value, str):
        items = re.split(r"[;,\n]", value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict):
                text = item.get("skill") or item.get("name") or item.get("text")
                if text:
                    items.append(str(text))
    else:
        items = [str(value)]

    out, seen = [], set()
    for item in items:
        text = str(item).strip(" \t-•*")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text[:60])
    return out[:15]


def _bool_from(value):
    """Tri-state bool: None means 'the model never answered this'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value >= 1 if value > 1 else bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1", "match", "matched", "relevant", "high"):
            return True
        if text in ("false", "no", "n", "0", "not relevant", "irrelevant", "none"):
            return False
    return None



class FilterResult(BaseModel):
    passed: bool
    matched: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    status: str = "PASS"
    rule: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Phase 7: the job-to-profile verdict
#
# This is the single canonical answer to "is this job relevant, and should we
# surface it?". It merges deterministic evidence (role, skills, experience,
# location, education, employment) with the AI's semantic judgement, and it is
# what storage, notifications and the future dashboard all read from.
# --------------------------------------------------------------------------

class MatchResult(BaseModel):
    match: Optional[bool] = None
    match_score: Optional[int] = Field(default=None, ge=0, le=100)
    priority: Priority = Priority.P3
    assessment_status: str = "completed"
    reason: str = ""
    # A hard rule vetoed this job (see MatchEvidence.reject_reasons). The score
    # is already capped for those, but the flag keeps the *why* visible to the
    # alert and the dashboard instead of burying it in the reason sentence.
    hard_reject: bool = False
    reject_reasons: List[str] = Field(default_factory=list)

    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)

    # Tri-state: None means "the posting never said", which is neutral - not a
    # rejection. Only True/False are evidence.
    experience_match: Optional[bool] = None
    location_match: Optional[bool] = None
    role_match: Optional[bool] = None
    education_match: Optional[bool] = None
    employment_match: Optional[bool] = None
    fresher_eligible: Optional[bool] = None
    ai_rejection_reasons: List[str] = Field(default_factory=list)

    # Traceability: which model answered, and what it answered with.
    ai_status: str = "skipped"            # compatibility: ok | failed | skipped
    ai_model: Optional[str] = None
    ai_score: Optional[int] = Field(default=None, ge=0, le=100)
    ai_priority: Optional[Priority] = None
    ai_error: Optional[str] = None
    deterministic_score: int = Field(default=0, ge=0, le=100)
    # Per-signal breakdown (0-1 scores + notes) for explainability/dashboard.
    signals: Dict[str, Any] = Field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when the verdict was reached without a usable AI answer."""
        return self.ai_status == "failed"

    def to_ai_response(self) -> "AIResponse":
        """Bridge to the Phase 6 verdict shape used by storage + notifier.

        Kept so ai_decision keeps its documented meaning (the AI's own answer)
        and so a stored match can still be replayed by the alert-retry path.
        """
        reasons = []
        if self.reason:
            reasons.append(self.reason)
        for label, value in (
            ("Matched", ", ".join(self.matched_skills[:6])),
            ("Missing", ", ".join(self.missing_skills[:6])),
        ):
            if value:
                reasons.append(f"{label}: {value}")
        if self.ai_error:
            reasons.append(f"AI unavailable ({self.ai_error})")
        return AIResponse(
            relevant=self.match,
            priority=self.priority,
            match_score=self.match_score,
            reasons=reasons[:5],
            raw_data={
                "model": self.ai_model,
                "ai_status": self.ai_status,
                "ai_score": self.ai_score,
                "deterministic_score": self.deterministic_score,
                "matched_skills": self.matched_skills,
                "missing_skills": self.missing_skills,
            },
            degraded=self.degraded,
            error=self.ai_error,
            reason=self.reason,
            matched_skills=self.matched_skills,
            missing_skills=self.missing_skills,
            experience_match=self.experience_match,
            location_match=self.location_match,
            role_match=self.role_match,
            education_match=self.education_match,
            employment_match=self.employment_match,
            fresher_eligible=self.fresher_eligible,
            rejection_reasons=self.ai_rejection_reasons,
        )



class ScanStats(BaseModel):
    sources_scanned: int = 0
    telegram_channels_scanned: int = 0
    messages_checked: int = 0
    promotional_rejected: int = 0
    educational_rejected: int = 0
    informational_rejected: int = 0
    spam_rejected: int = 0
    selection_rejected: int = 0
    aggregator_rejected: int = 0
    intent_ambiguous: int = 0
    jobs_detected: int = 0
    jobs_normalized: int = 0
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_filtered: int = 0
    jobs_hard_filter_passed: int = 0
    jobs_hard_filter_unknown: int = 0
    jobs_rule_scored: int = 0
    jobs_rule_strong: int = 0
    jobs_ai_skipped: int = 0
    jobs_ai_matched: int = 0
    jobs_ai_rejected: int = 0
    jobs_matching_attempted: int = 0
    # Jobs where the AI call failed and the pipeline used the degraded fallback.
    ai_failures: int = 0
    # Phase 7: hybrid profile-match outcomes (deterministic + AI together).
    jobs_matched: int = 0
    jobs_below_match: int = 0
    jobs_verified: int = 0
    jobs_verification_attempted: int = 0
    jobs_unverified: int = 0
    jobs_closed: int = 0
    # Jobs dropped because the only link was social/aggregator/shortener noise.
    jobs_link_rejected: int = 0
    jobs_notified: int = 0
    jobs_notification_eligible: int = 0
    ai_provider: str = "disabled"
    top_opportunities: List[Dict[str, Any]] = Field(default_factory=list)
    jobs_skipped_notification: int = 0
    jobs_expired: int = 0
    jobs_failed: int = 0
    priority_distribution: Dict[str, int] = Field(
        default_factory=lambda: {"P1": 0, "P2": 0, "P3": 0}
    )
    errors: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
