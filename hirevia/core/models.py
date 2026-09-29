"""Core data models for Hirevia"""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl


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

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if not isinstance(other, Job):
            return False
        return self.id == other.id


class CandidateProfile(BaseModel):
    name: str
    summary: str
    skills: List[str] = Field(default_factory=list)
    experience_years: int = 0
    preferred_roles: List[str] = Field(default_factory=list)
    preferred_locations: List[str] = Field(default_factory=list)
    preferred_companies: List[str] = Field(default_factory=list)
    avoid_companies: List[str] = Field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    notice_period_days: Optional[int] = None


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

        return cls(relevant=relevant, priority=priority, match_score=score, reasons=reasons)

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


class ScanStats(BaseModel):
    sources_scanned: int = 0
    messages_checked: int = 0
    jobs_detected: int = 0
    jobs_normalized: int = 0
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_filtered: int = 0
    jobs_ai_matched: int = 0
    jobs_ai_rejected: int = 0
    # Jobs where the AI call failed and the pipeline used the degraded fallback.
    ai_failures: int = 0
    jobs_verified: int = 0
    jobs_unverified: int = 0
    jobs_closed: int = 0
    # Jobs dropped because the only link was social/aggregator/shortener noise.
    jobs_link_rejected: int = 0
    jobs_notified: int = 0
    jobs_skipped_notification: int = 0
    jobs_expired: int = 0
    jobs_failed: int = 0
    errors: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
