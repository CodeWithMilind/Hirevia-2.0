"""API request/response models for Web UI"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, SecretStr, model_validator


class SystemStatus(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    SCANNING = "SCANNING"
    STARTING = "STARTING"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    AUTH_REQUIRED = "AUTH_REQUIRED"
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
    is_running: bool
    telegram_connected: bool = False
    ai_provider: str = ""
    ai_provider_configured: bool = False
    ai_health_state: str = "untested"
    ai_health_checked_at: Optional[str] = None
    queue_size: int = 0
    queue_processing: int = 0
    queue_oldest_pending_at: Optional[str] = None
    last_cleanup_at: Optional[str] = None
    worker_pid: Optional[int] = None
    worker_alive: bool = False
    worker_status: Optional[str] = None
    worker_started_at: Optional[str] = None
    worker_heartbeat_at: Optional[str] = None
    worker_last_error: Optional[str] = None
    monitor_desired: Optional[str] = None
    sources: List[SourceInfo]


class ScanStatsResponse(BaseModel):
    sources_scanned: int
    jobs_found: int
    jobs_new: int
    jobs_duplicate: int
    jobs_filtered: int
    jobs_hard_filter_passed: int = 0
    jobs_link_rejected: int = 0
    jobs_matching_attempted: int = 0
    jobs_ai_rejected: int = 0
    jobs_ai_matched: int
    ai_failures: int = 0
    jobs_matched: int = 0
    jobs_below_match: int = 0
    jobs_verified: int
    jobs_verification_attempted: int = 0
    jobs_unverified: int = 0
    jobs_closed: int = 0
    jobs_notification_eligible: int = 0
    jobs_notified: int
    jobs_skipped_notification: int = 0
    jobs_failed: int = 0
    jobs_expired: int
    priority_distribution: Dict[str, int] = Field(
        default_factory=lambda: {"P1": 0, "P2": 0, "P3": 0}
    )
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
    saved: bool = False
    fingerprint: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    saved_at: Optional[str] = None
    expires_at: Optional[str] = None
    notified_at: Optional[str] = None
    notification_channel: Optional[str] = None
    ai_decision: Optional[Dict[str, Any]] = None
    freshness_minutes: Optional[int] = None
    match_status: Optional[str] = None
    match_score: Optional[float] = None
    match_priority: Optional[str] = None
    # Phase 7 hybrid verdict (deterministic evidence + AI). Declared here so
    # JobResponse does not silently drop them: pydantic ignores undeclared
    # fields, and the dashboard would lose the evidence behind the score.
    match_result: Optional[Dict[str, Any]] = None
    keyword_match_result: Optional[Dict[str, Any]] = None
    keyword_match_percentage: Optional[float] = None
    keyword_priority: Optional[str] = None
    matched_keywords: List[str] = Field(default_factory=list)
    unmatched_keywords: List[str] = Field(default_factory=list)
    matched_keyword_count: int = 0
    total_keyword_count: int = 0
    matching_method: Optional[str] = None
    deterministic_score: Optional[int] = None
    ai_score: Optional[int] = None
    ai_status: Optional[str] = None
    assessment_status: Optional[str] = None
    ai_error: Optional[str] = None
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    fresher_eligible: Optional[bool] = None
    ai_rejection_reasons: List[str] = []
    match_reason: str = ""
    hard_reject: bool = False
    degraded: bool = False


class JobsResponse(BaseModel):
    jobs: List[JobResponse]
    total: int
    limit: int
    offset: int


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


class SaveJobRequest(BaseModel):
    saved: bool = True


class ConfigFileRequest(BaseModel):
    content: str


class AISettingsRequest(BaseModel):
    primary: str
    model: str
    base_url: Optional[str] = None
    timeout_seconds: int = Field(default=20, ge=1, le=300)
    p1_threshold: int = Field(default=85, ge=0, le=100)
    p2_threshold: int = Field(default=65, ge=0, le=100)
    p3_threshold: int = Field(default=40, ge=0, le=100)
    api_key: Optional[SecretStr] = None

    @model_validator(mode="after")
    def validate_threshold_order(self):
        if not self.p1_threshold > self.p2_threshold > self.p3_threshold:
            raise ValueError("matching thresholds must satisfy P1 > P2 > P3")
        return self


class TelegramPhoneRequest(BaseModel):
    phone_number: str = Field(min_length=7, max_length=24)


class TelegramOTPRequest(BaseModel):
    code: SecretStr


class Telegram2FARequest(BaseModel):
    password: SecretStr


class TelegramSourceUpdate(BaseModel):
    enabled: bool