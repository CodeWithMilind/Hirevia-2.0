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
    expires_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None
    notification_channel: Optional[str] = None
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
        return self.match_result or self.ai_decision or {}

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
    relevant: bool
    priority: Priority
    match_score: int = Field(ge=0, le=100)
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
        data = data if isinstance(data, dict) else {}

        priority = _priority_from(data.get("priority"), data.get("confidence_level"))
        score = _score_from(data.get("match_score", data.get("score", data.get("similarity"))))
        reasons = _reasons_from(data.get("reasons", data.get("reason", data.get("why"))))

        relevant = _bool_from(
            data.get("relevant", data.get("match", data.get("is_match", data.get("is_relevant"))))
        )
        if relevant is None:  # no explicit verdict -> infer from the score
            relevant = score >= 60

        explanation = data.get("explanation") or data.get("summary") or ""
        reason = str(data.get("reason") or "").strip() if isinstance(data.get("reason"), str) else ""

        return cls(
            relevant=relevant,
            priority=priority,
            match_score=score,
            reasons=reasons,
            reason=(reason or str(explanation)).strip()[:400],
            matched_skills=_skill_list_from(data.get("matched_skills", data.get("skills_matched"))),
            missing_skills=_skill_list_from(data.get("missing_skills", data.get("skills_missing"))),
            experience_match=_bool_from(data.get("experience_match")),
            location_match=_bool_from(data.get("location_match")),
            role_match=_bool_from(data.get("role_match")),
            education_match=_bool_from(data.get("education_match")),
            employment_match=_bool_from(data.get("employment_match")),
        )

    @classmethod
    def failure(cls, error: object) -> "AIResponse":
        """The documented fallback when AI matching is unavailable.

        Fail-open but LOW priority: the job is kept for review/storage, it is
        never escalated to a P1 alert just because the AI could not answer.
        """
        message = str(error).replace("\n", " ")[:180] if error is not None else "unknown error"
        return cls(
            relevant=True,
            priority=Priority.P3,
            match_score=50,
            reasons=[f"AI unavailable - kept for manual review ({message})"],
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


# --------------------------------------------------------------------------
# Phase 7: the job-to-profile verdict
#
# This is the single canonical answer to "is this job relevant, and should we
# surface it?". It merges deterministic evidence (role, skills, experience,
# location, education, employment) with the AI's semantic judgement, and it is
# what storage, notifications and the future dashboard all read from.
# --------------------------------------------------------------------------

class MatchResult(BaseModel):
    match: bool = False
    match_score: int = Field(default=0, ge=0, le=100)
    priority: Priority = Priority.P3
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

    # Traceability: which model answered, and what it answered with.
    ai_status: str = "skipped"            # ok | failed | skipped
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
        )



class ScanStats(BaseModel):
    sources_scanned: int = 0
    messages_checked: int = 0
    promotional_rejected: int = 0
    educational_rejected: int = 0
    informational_rejected: int = 0
    spam_rejected: int = 0
    intent_ambiguous: int = 0
    jobs_detected: int = 0
    jobs_normalized: int = 0
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_filtered: int = 0
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
    jobs_skipped_notification: int = 0
    jobs_expired: int = 0
    jobs_failed: int = 0
    errors: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
