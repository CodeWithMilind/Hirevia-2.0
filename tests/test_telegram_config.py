"""Tests for Telegram collector configuration (telegram.yaml + .env credentials).

Contract under test: config problems degrade gracefully (channel skipped,
Telegram inactive) and must never crash the scan pipeline. Credentials come
from env only and never leak through repr/str.
"""

import pytest

from hirevia.sources import telegram_config
from hirevia.sources.telegram_config import (
    TelegramChannel,
    load_telegram_credentials,
    load_telegram_settings,
    mask_phone,
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Run each test from an empty dir with a no-op dotenv loader so the
    developer's real .env / telegram.yaml never leak into tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(telegram_config, "load_dotenv", lambda *a, **k: False)
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_NAME", "TELEGRAM_SESSION_DIR"):
        monkeypatch.delenv(var, raising=False)


def write_yaml(tmp_path, body):
    (tmp_path / "telegram.yaml").write_text(body, encoding="utf-8")


VALID_YAML = """
enabled: true
channels:
  - name: "Python Internships"
    username: pythoninternships
    enabled: true
    lookback_hours: 24
    max_messages: 30
  - username: offtopicchannel
    enabled: false
"""


def test_missing_file_means_no_channels(tmp_path):
    settings = load_telegram_settings()
    assert settings.get_enabled_channels() == []


def test_loads_channels_and_env_flags(tmp_path):
    write_yaml(tmp_path, VALID_YAML)
    settings = load_telegram_settings()
    assert settings.enabled is True
    enabled = settings.get_enabled_channels()
    assert [c.username for c in enabled] == ["pythoninternships"]
    assert enabled[0].lookback_hours == 24
    assert enabled[0].max_messages == 30


def test_channel_defaults(tmp_path):
    write_yaml(tmp_path, "enabled: true\nchannels:\n  - username: jobs_abc\n")
    channel = load_telegram_settings().get_enabled_channels()[0]
    assert channel.lookback_hours == 24
    assert channel.max_messages == 100
    assert channel.enabled is True


def test_invalid_channel_skipped_not_fatal(tmp_path):
    write_yaml(tmp_path, "enabled: true\nchannels:\n  - username: 'has spaces!'\n  - username: jobs_good\n")
    settings = load_telegram_settings()
    assert [c.username for c in settings.get_enabled_channels()] == ["jobs_good"]


def test_username_forms_normalized():
    assert TelegramChannel(username="@pythoninternships").username == "pythoninternships"
    assert TelegramChannel(username="t.me/pythoninternships").username == "pythoninternships"
    assert TelegramChannel(username="https://t.me/pythoninternships/").username == "pythoninternships"


def test_username_too_short_rejected():
    with pytest.raises(Exception):
        TelegramChannel(username="ab")


def test_malformed_entry_skipped(tmp_path):
    write_yaml(tmp_path, "enabled: true\nchannels:\n  - just a string\n  - username: jobs_good\n")
    assert [c.username for c in load_telegram_settings().get_enabled_channels()] == ["jobs_good"]


def test_disabled_flag_respected(tmp_path):
    write_yaml(tmp_path, "enabled: false\nchannels:\n  - username: jobs_good\n")
    settings = load_telegram_settings()
    assert settings.enabled is False


def test_credentials_missing(monkeypatch):
    assert load_telegram_credentials() is None


def test_credentials_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("TELEGRAM_SESSION_NAME", "hirevia")
    monkeypatch.setenv("TELEGRAM_SESSION_DIR", str(tmp_path / "tgstate"))
    creds = load_telegram_credentials()
    assert creds is not None
    assert creds.api_id == 123456
    assert creds.api_hash == "0123456789abcdef0123456789abcdef"
    assert creds.session_path == tmp_path / "tgstate" / "hirevia"


def test_credentials_reject_non_numeric_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "not-a-number")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    assert load_telegram_credentials() is None  # inactive, never crash


def test_credentials_never_leak_hash(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
    creds = load_telegram_credentials()
    assert "0123456789abcdef" not in repr(creds)
    assert "0123456789abcdef" not in str(creds)


def test_mask_phone():
    assert mask_phone("+919876543210") == "+********3210"
    assert mask_phone("") == ""

