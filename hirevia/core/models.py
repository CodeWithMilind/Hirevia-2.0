"""Core data models for Hirevia"""

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

    @classmethod
    def from_json(cls, data: dict) -> "AIResponse":
        priority_map = {"P1": Priority.P1, "P2": Priority.P2, "P3": Priority.P3}
        return cls(
            relevant=data.get("relevant", False),
            priority=priority_map.get(data.get("priority", "P3"), Priority.P3),
            match_score=max(0, min(100, data.get("match_score", 0))),
            reasons=data.get("reasons", []),
        )


class FilterResult(BaseModel):
    passed: bool
    matched: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ScanStats(BaseModel):
    sources_scanned: int = 0
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_filtered: int = 0
    jobs_ai_matched: int = 0
    jobs_verified: int = 0
    jobs_notified: int = 0
    jobs_expired: int = 0
    errors: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0