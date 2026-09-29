"""Tests for TelegramNotifier delivery behaviour.

The HTTP client is replaced with a fake, so nothing talks to Telegram. What is
under test is the retry policy: transient failures back off (respecting
Telegram's retry_after), permanent client errors fail fast, and the alert text
carries everything a candidate needs to apply.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from hirevia.core.models import AIResponse, Job, Priority
from hirevia.notifications import telegram as tg
from hirevia.notifications.telegram import TelegramNotifier, _retry_after


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": status_code == 200}
        self.text = text or str(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    """Replays a queue of responses / exceptions for successive POSTs."""

    def __init__(self, responses):
        self.queue = list(responses)
        self.calls = []
        self.closed = False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        item = self.queue.pop(0) if self.queue else FakeResponse(200)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True


def make_job(**kwargs):
    defaults = dict(
        source="telegram",
        source_job_id="jobs_alpha-1",
        title="Backend Developer",
        company="Acme <Corp>",
        location="Pune",
        description="Python FastAPI, 1-3 years",
        url="https://t.me/jobs_alpha/1",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
        posted_at=datetime.utcnow() - timedelta(minutes=40),
        raw_data={"source_channels": ["jobs_alpha"], "salary": "₹8-12 LPA"},
    )
    defaults.update(kwargs)
    return Job(**defaults)


DECISION = AIResponse(relevant=True, priority=Priority.P1, match_score=91,
                      reasons=["Python + FastAPI overlap", "Pune location"])


@pytest.fixture
def sleeps(monkeypatch):
    """Capture backoff waits without ever really sleeping."""
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(tg, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return recorded


def build(monkeypatch, responses, **kwargs):
    kwargs.setdefault("retry_attempts", 3)
    kwargs.setdefault("retry_delay", 1.0)
    notifier = TelegramNotifier("123:token", "42", **kwargs)
    notifier.client = FakeClient(responses)
    return notifier


def send(notifier):
    return asyncio.run(notifier.send(make_job(), DECISION))


# --- delivery -----------------------------------------------------------------

def test_successful_send_posts_once(monkeypatch, sleeps):
    notifier = build(monkeypatch, [FakeResponse(200)])
    assert send(notifier) is True
    assert len(notifier.client.calls) == 1
    assert sleeps == []

    payload = notifier.client.calls[0]["json"]
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"
    assert notifier.client.calls[0]["url"].endswith("/sendMessage")


def test_transient_server_errors_are_retried_with_backoff(monkeypatch, sleeps):
    notifier = build(monkeypatch, [FakeResponse(500), FakeResponse(502), FakeResponse(200)])
    assert send(notifier) is True
    assert len(notifier.client.calls) == 3
    assert sleeps == [1.0, 2.0]  # retry_delay * attempt number


def test_network_exception_is_retried_then_gives_up(monkeypatch, sleeps):
    notifier = build(monkeypatch, [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    assert send(notifier) is False
    assert len(notifier.client.calls) == 3
    assert len(sleeps) == 2


def test_attempts_and_delay_are_configurable(monkeypatch, sleeps):
    notifier = build(monkeypatch, [FakeResponse(500), FakeResponse(500), FakeResponse(500)],
                     retry_attempts=1)
    assert send(notifier) is False
    assert len(notifier.client.calls) == 1  # no retry budget left
    assert sleeps == []


# --- permanent vs rate-limited --------------------------------------------------

@pytest.mark.parametrize("status", [400, 403, 404])
def test_permanent_client_errors_fail_fast(monkeypatch, sleeps, status):
    """Bad chat id / blocked bot: retrying wastes the whole scan window."""
    notifier = build(monkeypatch, [FakeResponse(status), FakeResponse(200), FakeResponse(200)])
    assert send(notifier) is False
    assert len(notifier.client.calls) == 1  # never retried
    assert sleeps == []


def test_429_respects_telegram_retry_after(monkeypatch, sleeps):
    limited = FakeResponse(429, payload={"ok": False, "parameters": {"retry_after": 7}})
    notifier = build(monkeypatch, [limited, FakeResponse(200)], retry_delay=1.0)
    assert send(notifier) is True
    assert sleeps == [7.0]  # Telegram's hint beats the local backoff


def test_429_retry_after_is_capped(monkeypatch, sleeps):
    limited = FakeResponse(429, payload={"ok": False, "parameters": {"retry_after": 600}})
    notifier = build(monkeypatch, [limited, FakeResponse(200)])
    assert send(notifier) is True
    assert sleeps == [30.0]  # a scan must never stall on one rate limit


def test_retry_after_falls_back_to_header_and_bad_payload():
    assert _retry_after(FakeResponse(429, payload={}, headers={"retry-after": "5"})) == 5.0
    assert _retry_after(FakeResponse(429, payload="not json", headers={})) == 0.0


def test_close_releases_the_client(monkeypatch):
    notifier = build(monkeypatch, [])
    asyncio.run(notifier.close())
    assert notifier.client.closed is True


# --- alert rendering --------------------------------------------------------------

def test_alert_text_is_apply_ready_and_escaped(monkeypatch):
    notifier = build(monkeypatch, [FakeResponse(200)])
    send(notifier)
    text = notifier.client.calls[0]["json"]["text"]

    assert "P1 - Job Match (91%)" in text
    assert "Role: Backend Developer" in text
    assert "Acme &lt;Corp&gt;" in text          # HTML injection is escaped
    assert "Apply: https://boards.greenhouse.io/acme/jobs/1" in text
    assert "Compensation: ₹8-12 LPA" in text
    assert "Source: jobs_alpha" in text
    assert "Python + FastAPI overlap" in text
    assert "fresh" in text.lower()

