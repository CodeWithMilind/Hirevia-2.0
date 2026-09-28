"""Configuration loading for Hirevia"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    enabled: bool = False


class SourcesConfig(BaseModel):
    sources: Dict[str, SourceConfig] = Field(default_factory=dict)

    def get_enabled_sources(self) -> list[str]:
        return [name for name, config in self.sources.items() if config.enabled]


class FilterConfig(BaseModel):
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    experience: Dict[str, Any] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    employment: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class Config(BaseModel):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "meta/llama-3.1-70b-instruct"
    scan_interval_minutes: int = 30
    notification_retry_attempts: int = 3
    notification_retry_delay_seconds: int = 60


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(
    base_dir: Optional[Path] = None,
    sources_path: Optional[Path] = None,
    filters_path: Optional[Path] = None,
) -> Config:
    if base_dir is None:
        base_dir = Path.cwd()

    if sources_path is None:
        sources_path = base_dir / "sources.yaml"
    if filters_path is None:
        filters_path = base_dir / "filters.yaml"

    sources_data = load_yaml(sources_path)
    filters_data = load_yaml(filters_path)

    sources_config = SourcesConfig(**sources_data)
    filters_config = FilterConfig(**filters_data)

    return Config(
        sources=sources_config,
        filters=filters_config,
        nvidia_base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        nvidia_model=os.getenv("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct"),
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "30")),
    )


def get_nvidia_api_key() -> str:
    """Prompt user for NVIDIA API key at runtime."""
    import getpass
    return getpass.getpass("NVIDIA API key: ")


def get_telegram_token() -> str:
    """Prompt user for Telegram bot token at runtime."""
    import getpass
    return getpass.getpass("Telegram bot token: ")