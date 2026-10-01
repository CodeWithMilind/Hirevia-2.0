"""Configuration loading for Hirevia"""

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

try:  # .env is loaded once, here, so every env var (NVIDIA_MODEL included)
    from dotenv import load_dotenv  # is present before load_config() runs
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    def load_dotenv(*args, **kwargs):
        return False


def _ensure_dotenv() -> None:
    """Load .env into the environment without overriding real env vars.

    Collectors used to do this lazily, which meant load_config() could read
    NVIDIA_MODEL before anything had loaded .env and silently fall back to the
    built-in default model.
    """
    try:
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    except Exception:  # noqa: BLE001 - a broken .env must not stop a scan
        pass



class SourceConfig(BaseModel):
    enabled: bool = False


DEFAULT_SOURCES = {
    "company_careers": SourceConfig(enabled=False),
    "greenhouse": SourceConfig(enabled=False),
    "lever": SourceConfig(enabled=False),
    "ashby": SourceConfig(enabled=False),
    "smartrecruiters": SourceConfig(enabled=False),
    "naukri": SourceConfig(enabled=False),
    "linkedin": SourceConfig(enabled=False),
    "indeed": SourceConfig(enabled=False),
    "internshala": SourceConfig(enabled=False),
    "cutshort": SourceConfig(enabled=False),
    "hirist": SourceConfig(enabled=False),
    "instahyre": SourceConfig(enabled=False),
    "wellfound": SourceConfig(enabled=False),
    "unstop": SourceConfig(enabled=False),
    "telegram": SourceConfig(enabled=True),
    "whatsapp": SourceConfig(enabled=False),
}


class SourcesConfig(BaseModel):
    sources: Dict[str, SourceConfig] = Field(default_factory=lambda: {name: config.model_copy() for name, config in DEFAULT_SOURCES.items()})

    def get_enabled_sources(self) -> list[str]:
        return [name for name, config in self.sources.items() if config.enabled]


class FilterConfig(BaseModel):
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    experience: Dict[str, Any] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    employment: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class AIProviderConfig(BaseModel):
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    requires_api_key: bool = True


def _default_ai_providers() -> Dict[str, AIProviderConfig]:
    return {
        "nvidia": AIProviderConfig(
            base_url="https://integrate.api.nvidia.com/v1",
            model="openai/gpt-oss-20b",
            api_key_env="NVIDIA_API_KEY",
        ),
        "groq": AIProviderConfig(
            base_url="https://api.groq.com/openai/v1",
            model="llama-3.3-70b-versatile",
            api_key_env="GROQ_API_KEY",
        ),
        "ollama": AIProviderConfig(
            base_url="http://localhost:11434/v1",
            api_key_env="OLLAMA_API_KEY",
            requires_api_key=False,
        ),
        "llamacpp": AIProviderConfig(
            base_url="http://localhost:8080/v1",
            api_key_env="LLAMACPP_API_KEY",
            requires_api_key=False,
        ),
    }


class AIConfig(BaseModel):
    enabled: bool = True
    primary: str = "llamacpp"
    fallback: List[str] = Field(default_factory=lambda: ["nvidia", "groq", "ollama", "llamacpp"])
    timeout_seconds: int = Field(default=20, ge=1, le=300)
    max_retries: int = Field(default=1, ge=0, le=3)
    cooldown_seconds: int = Field(default=120, ge=1, le=300)
    providers: Dict[str, AIProviderConfig] = Field(default_factory=_default_ai_providers)


class Config(BaseModel):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "openai/gpt-oss-20b"
    scan_interval_minutes: int = 30
    notification_retry_attempts: int = 3
    notification_retry_delay_seconds: int = 60

    # --- Phase 6: pipeline robustness -------------------------------------
    # Hard per-request timeout for the AI provider. Without it a hung request
    # stalls the whole scan.
    ai_timeout_seconds: int = 45
    # Light-touch reachability check on application links (HEAD then GET).
    # Set VERIFY_APPLICATION_URLS=false for offline runs/tests.
    verify_application_urls: bool = True
    verify_timeout_seconds: float = 8.0
    # Alerts go out for P1/P2 always; P3 only above this score.
    notify_min_match_score: int = 80
    # Do not re-alert for jobs older than this (0 disables the guard).
    notify_max_age_hours: int = 72
    # Cap stored error strings so a failing source cannot bloat the report.
    max_reported_errors: int = 25

    # --- Phase 7: profile-driven job matching --------------------------------
    # Canonical candidate profile (PROFILE_PATH env / --profile-path). When the
    # file is missing, load_profile() falls back to filters.yaml instead of
    # handing the matcher an empty profile.
    profile_path: Optional[str] = None
    # A job counts as a match (recorded + alert-eligible) at or above this score.
    # Deliberately lower than notify_min_match_score: relevant != interrupt-worthy.
    match_min_score: int = 55
    # Deterministic priority thresholds (clear, no arbitrary buckets).
    priority_p1_min_score: int = Field(default=85, ge=0, le=100)
    priority_p2_min_score: int = Field(default=65, ge=0, le=100)
    priority_p3_min_score: int = Field(default=40, ge=0, le=100)
    # Weight of the AI verdict in the final score; the rest is deterministic
    # evidence. AI_MATCH_WEIGHT=0 makes matching purely deterministic.
    ai_match_weight: float = 0.40
    # When the model could not be consulted, the score is capped here: a job is
    # never presented as a strong match just because the AI was unavailable.
    ai_failed_score_cap: int = 70

    @model_validator(mode="after")
    def validate_priority_thresholds(self):
        if not (
            self.priority_p1_min_score > self.priority_p2_min_score
            > self.priority_p3_min_score
        ):
            raise ValueError("matching thresholds must satisfy P1 > P2 > P3")
        return self



def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    """int() that refuses to take down a scan on a malformed .env line."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default



def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(
    base_dir: Optional[Path] = None,
    sources_path: Optional[Path] = None,
    filters_path: Optional[Path] = None,
    ai_path: Optional[Path] = None,
) -> Config:
    _ensure_dotenv()  # NVIDIA_MODEL / NVIDIA_API_KEY live in .env for local runs
    if base_dir is None:
        base_dir = Path.cwd()

    if sources_path is None:
        sources_path = base_dir / "sources.yaml"
    if filters_path is None:
        filters_path = base_dir / "filters.yaml"
    if ai_path is None:
        ai_path = base_dir / "ai.yaml"

    sources_data = load_yaml(sources_path)
    filters_data = load_yaml(filters_path)
    ai_document = load_yaml(ai_path)
    ai_data = ai_document
    ai_data = ai_data.get("ai", ai_data) or {}
    matching_data = ai_document.get("matching", {}) if isinstance(ai_document, dict) else {}
    if not isinstance(matching_data, dict):
        matching_data = {}

    provider_data = {
        name: provider.model_dump()
        for name, provider in _default_ai_providers().items()
    }
    for name, values in (ai_data.get("providers") or {}).items():
        provider_data.setdefault(name, {})
        provider_data[name].update(values or {})
    for name, values in provider_data.items():
        base_url = os.getenv(f"{name.upper()}_BASE_URL")
        model = os.getenv(f"{name.upper()}_MODEL")
        if base_url is not None:
            values["base_url"] = base_url
        if model is not None:
            values["model"] = model
    nvidia = provider_data["nvidia"]
    nvidia["base_url"] = os.getenv("NVIDIA_BASE_URL", nvidia["base_url"])
    nvidia["model"] = os.getenv("NVIDIA_MODEL", nvidia["model"])
    ai_data["primary"] = os.getenv("AI_PRIMARY_PROVIDER", ai_data.get("primary", "llamacpp"))
    fallback_env = os.getenv("AI_FALLBACK_PROVIDERS")
    if fallback_env is not None:
        ai_data["fallback"] = [
            name.strip().lower()
            for name in fallback_env.split(",")
            if name.strip()
        ]
    ai_data["providers"] = provider_data
    if os.getenv("AI_TIMEOUT_SECONDS"):
        ai_data["timeout_seconds"] = _env_int("AI_TIMEOUT_SECONDS", 20)
    ai_settings = AIConfig(**ai_data)

    sources_config = SourcesConfig(**sources_data)
    filters_config = FilterConfig(**filters_data)

    return Config(
        sources=sources_config,
        filters=filters_config,
        ai=ai_settings,
        nvidia_base_url=nvidia["base_url"],
        nvidia_model=nvidia["model"],
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "30")),
        ai_timeout_seconds=ai_settings.timeout_seconds,
        verify_application_urls=_env_bool("VERIFY_APPLICATION_URLS", True),
        verify_timeout_seconds=float(os.getenv("VERIFY_TIMEOUT_SECONDS", "8")),
        notify_min_match_score=int(os.getenv("NOTIFY_MIN_MATCH_SCORE", "80")),
        notify_max_age_hours=int(os.getenv("NOTIFY_MAX_AGE_HOURS", "72")),
        priority_p1_min_score=_env_int(
            "MATCHING_P1_THRESHOLD", int(matching_data.get("p1_threshold", 85))
        ),
        priority_p2_min_score=_env_int(
            "MATCHING_P2_THRESHOLD", int(matching_data.get("p2_threshold", 65))
        ),
        priority_p3_min_score=_env_int(
            "MATCHING_P3_THRESHOLD", int(matching_data.get("p3_threshold", 40))
        ),
    )



def get_nvidia_api_key() -> str:
    """Prompt user for NVIDIA API key at runtime."""
    import getpass
    return getpass.getpass("NVIDIA API key: ")


def get_telegram_token() -> str:
    """Prompt user for Telegram bot token at runtime."""
    import getpass
    return getpass.getpass("Telegram bot token: ")