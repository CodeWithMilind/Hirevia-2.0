"""API request/response models for Web UI"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class SystemStatus(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    SCANNING = "SCANNING"
    ERROR = "ERROR"


class SourceStatus(str, Enum):
    ACTIVE = "active"
    WARNING = "warning"
    ERROR = "error"
    DISABLED = "disabled"


class JobStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class ActivityEntry(BaseModel):
    timestamp: str
    message: str
    level: str = "info"  # info, warning, error


class SourceInfo(BaseModel):
    name: str
    status: SourceStatus
    jobs_collected: int
    last_error: Optional[str] = None
    enabled: bool


class SystemStatusResponse(BaseModel):
    status: SystemStatus
    current_operation: str
    last_scan: Optional[str] = None
    next_scan: Optional[str] = None
    scan_duration: Optional[float] = None
    scan_interval_minutes: int
    is_scanning: bool
    sources: List[SourceInfo]


class ScanStatsResponse(BaseModel):
    sources_scanned: int
    jobs_found: int
    jobs_new: int
    jobs_duplicate: int
    jobs_filtered: int
    jobs_ai_matched: int
    jobs_verified: int
    jobs_notified: int
    jobs_expired: int
    duration_seconds: float
    errors: List[str] = []


class JobListParams(BaseModel):
    search: Optional[str] = None
    source: Optional[str] = None
    status: Optional[str] = None
    min_match: Optional[int] = None
    location: Optional[str] = None
    limit: int = 50
    offset: int = 0


class JobResponse(BaseModel):
    id: str
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    application_url: Optional[str] = None
    posted_at: Optional[str] = None
    detected_at: str
    deadline: Optional[str] = None
    employment_type: Optional[str] = None
    experience: Optional[str] = None
    skills: List[str] = []
    application_status: JobStatus
    fingerprint: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    expires_at: Optional[str] = None
    notified_at: Optional[str] = None
    notification_channel: Optional[str] = None
    ai_decision: Optional[Dict[str, Any]] = None
    freshness_minutes: Optional[int] = None
    match_score: Optional[int] = None
    match_priority: Optional[str] = None
    # Phase 7 hybrid verdict (deterministic evidence + AI). Declared here so
    # JobResponse does not silently drop them: pydantic ignores undeclared
    # fields, and the dashboard would lose the evidence behind the score.
    match_result: Optional[Dict[str, Any]] = None
    deterministic_score: Optional[int] = None
    ai_score: Optional[int] = None
    ai_status: Optional[str] = None
    ai_error: Optional[str] = None
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    match_reason: str = ""
    hard_reject: bool = False
    degraded: bool = False


class JobsResponse(BaseModel):
    jobs: List[JobResponse]
    total: int
    limit: int
    offset: int


class TelegramTestRequest(BaseModel):
    message: str = "Test message from Hirevia Web UI"


class TelegramTestResponse(BaseModel):
    success: bool
    message: str


class ScanTriggerResponse(BaseModel):
    success: bool
    message: str
    stats: Optional[ScanStatsResponse] = None


class StartStopResponse(BaseModel):
    success: bool
    message: str
    status: SystemStatus


class ConfigResponse(BaseModel):
    scan_interval_minutes: int
    enabled_sources: List[str]
    filters: Dict[str, Any]