"""Telegram source configuration.

Two separate concerns, kept strictly apart:

* Credentials  -> environment variables only (TELEGRAM_API_ID, TELEGRAM_API_HASH,
                  TELEGRAM_SESSION_NAME). Never stored in YAML, never logged.
* Channels     -> telegram.yaml (safe to review/edit, no secrets).
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

try:  # module-level so the whole project picks up .env; patchable in tests
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

logger = logging.getLogger(__name__)
_APPLICATION_ROOT = Path(__file__).resolve().parents[2]

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class TelegramAuthError(RuntimeError):
    """Raised when the Telegram account cannot be used (missing/invalid credentials or session)."""


def mask_phone(phone: str) -> str:
    """Mask a phone number for safe logging, keeping only the last 4 digits."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) <= 4:
        return "*" * len(digits)
    return "+" + "*" * (len(digits) - 4) + digits[-4:]


class TelegramCredentials(BaseModel):
    """MTProto API credentials for the dedicated Hirevia Telegram account."""

    api_id: int
    api_hash: str = Field(repr=False)  # never appear in repr/logs
    session_name: str = "hirevia"
    session_dir: str = "sessions"

    @property
    def session_path(self) -> Path:
        session_dir = Path(self.session_dir).expanduser()
        if not session_dir.is_absolute():
            session_dir = _APPLICATION_ROOT / session_dir
        return (session_dir / self.session_name).resolve()

    def __str__(self) -> str:  # defensive: even str() must not leak the hash
        return f"TelegramCredentials(api_id={self.api_id}, session={self.session_name})"


def load_telegram_credentials() -> Optional[TelegramCredentials]:
    """Read Telegram API credentials from the environment.

    Returns None (never raises) when credentials are absent or malformed, so the
    rest of Hirevia keeps working with Telegram simply inactive.
    """
    load_dotenv()  # idempotent; does not override real environment variables

    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    session_name = (os.getenv("TELEGRAM_SESSION_NAME") or "").strip() or "hirevia"

    if not api_id or not api_hash:
        return None

    if not api_id.isdigit():
        logger.error("[Telegram] TELEGRAM_API_ID must be a numeric Telegram API ID")
        return None

    session_dir = (os.getenv("TELEGRAM_SESSION_DIR") or "").strip() or "sessions"
    return TelegramCredentials(
        api_id=int(api_id),
        api_hash=api_hash,
        session_name=session_name,
        session_dir=session_dir,
    )


class TelegramChannel(BaseModel):
    """One monitored public Telegram channel/group."""

    username: str
    enabled: bool = True
    # Only used on the very first visit (no in-process cursor yet):
    # fetch messages newer than this window so a new channel never floods the DB.
    lookback_hours: int = 24
    # Hard cap of messages inspected per channel per scan.
    max_messages: int = 100

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        v = (v or "").strip()
        # Accept "@channel", "t.me/channel" and "https://t.me/channel" forms.
        v = re.sub(r"^https?://", "", v)
        v = re.sub(r"^(www\.)?(telegram\.me|t\.me)/", "", v)
        v = v.strip("/").lstrip("@")
        if not USERNAME_RE.match(v):
            raise ValueError(f"invalid public channel username: {v!r}")
        return v


class TelegramSettings(BaseModel):
    """telegram.yaml contents: which public channels to monitor."""

    enabled: bool = True
    channels: List[TelegramChannel] = Field(default_factory=list)

    def get_enabled_channels(self) -> List[TelegramChannel]:
        return [c for c in self.channels if c.enabled]


def load_telegram_settings(path: Optional[Path] = None) -> TelegramSettings:
    """Load telegram.yaml. Missing/broken config degrades to "no channels",
    it must never crash the scan pipeline."""
    if path is None:
        path = Path.cwd() / "telegram.yaml"

    if not path.exists():
        logger.warning(
            "[Telegram] No telegram.yaml found at %s - no channels will be monitored",
            path,
        )
        return TelegramSettings()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("[Telegram] Failed to read %s: %s", path, e)
        return TelegramSettings()

    section = data.get("telegram", data) or {}
    raw_channels = section.get("channels") or []
    raw_defaults = section.get("defaults") or {}
    supported_defaults = ("enabled", "lookback_hours", "max_messages")
    defaults = (
        {key: raw_defaults[key] for key in supported_defaults if key in raw_defaults}
        if isinstance(raw_defaults, dict)
        else {}
    )

    channels: List[TelegramChannel] = []
    for item in raw_channels:
        if not isinstance(item, dict):
            logger.warning("[Telegram] Skipping malformed channel entry: %r", item)
            continue
        try:
            channels.append(TelegramChannel(**{**defaults, **item}))
        except Exception as e:
            logger.warning("[Telegram] Skipping invalid channel %r: %s", item, e)

    return TelegramSettings(
        enabled=bool(section.get("enabled", True)),
        channels=channels,
    )
