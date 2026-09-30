"""Tests for the Telegram collector (TelegramSource) with a mocked client.

No Telethon connection is ever made: _ensure_client / _fetch_channel_messages
are replaced with fakes, and one test drives the real fetch logic through a
fake Telethon client to verify cursor + lookback behaviour.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hirevia.core.models import Job
from hirevia.sources import telegram_source
from hirevia.sources.telegram_config import TelegramAuthError, TelegramChannel
from hirevia.sources.telegram_extract import TgMessage
from hirevia.sources.telegram_source import TelegramSource

NOW = datetime.now(timezone.utc)

JOB_TEXT = (
    "Hiring: Backend Developer\n"
    "Company: Acme Corp\n"
    "Location: Pune\n"
    "Apply: https://boards.greenhouse.io/acme-corp/jobs/12345"
)

JOB_TEXT_B = (
    "We are hiring: Data Analyst\n"
    "Company: Beta Labs\n"
    "Location: Remote\n"
    "Apply: https://jobs.lever.co/beta-labs/analyst-99"
)


def tg_msg(channel, message_id, text=JOB_TEXT, date=None):
    return TgMessage(channel=channel, message_id=message_id, date=date or NOW, text=text, links=[])


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    telegram_source._last_seen_ids.clear()
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(var, raising=False)
    yield
    telegram_source._last_seen_ids.clear()


def write_yaml(body):
    from pathlib import Path

    (Path.cwd() / "telegram.yaml").write_text(body, encoding="utf-8")


def two_channel_yaml(enabled=True):
    write_yaml(
        f"enabled: {'true' if enabled else 'false'}\n"
        "channels:\n"
        "  - username: jobs_alpha\n"
        "  - username: jobs_beta\n"
    )


def make_source(per_channel, diagnostic_output=False):
    src = TelegramSource(diagnostic_output=diagnostic_output)

    async def fake_ensure():
        return object()

    async def fake_fetch(client, channel):
        result = per_channel[channel.username]
        if isinstance(result, Exception):
            raise result
        return result

    src._ensure_client = fake_ensure
    src._fetch_channel_messages = fake_fetch
    return src


def test_collect_normalizes_new_messages():
    two_channel_yaml()
    src = make_source({
        "jobs_alpha": [tg_msg("jobs_alpha", 1), tg_msg("jobs_alpha", 2, text="just chatting here")],
        "jobs_beta": [tg_msg("jobs_beta", 7, text=JOB_TEXT_B)],
    })
    jobs = asyncio.run(src.collect())
    assert len(jobs) == 2
    assert {j.source for j in jobs} == {"telegram"}
    assert {j.raw_data["source_channel"] for j in jobs} == {"jobs_alpha", "jobs_beta"}


def test_diagnostic_output_groups_normalized_jobs_and_summary(capsys):
    two_channel_yaml()
    source = make_source(
        {
            "jobs_alpha": [
                tg_msg("jobs_alpha", 1),
                tg_msg("jobs_alpha", 2),
                tg_msg("jobs_alpha", 3, text="Join our Python course"),
            ],
            "jobs_beta": [tg_msg("jobs_beta", 7, text=JOB_TEXT_B)],
        },
        diagnostic_output=True,
    )

    asyncio.run(source.collect())
    output = capsys.readouterr().out

    alpha = output.index("📢 CHANNEL: jobs_alpha")
    first_job = output.index("Job Role: Backend Developer")
    beta = output.index("📢 CHANNEL: jobs_beta")
    second_job = output.index("Job Role: We are hiring: Data Analyst")
    summary = output.index("SCAN SUMMARY")
    assert alpha < first_job < beta < second_job < summary
    assert output.count("Job Role: Backend Developer") == 1
    assert "Join our Python course" not in output
    assert "Company Name: Acme Corp" in output
    assert "Location: Remote" in output
    assert "Apply Link: https://jobs.lever.co/beta-labs/analyst-99" in output
    assert "Channels scanned: 2" in output
    assert "Messages checked: 4" in output
    assert "Jobs detected: 3" in output
    assert "Jobs normalized: 2" in output
    assert "Jobs printed: 2" in output
    assert "Duplicates removed: 1" in output
    assert "Promotional rejected: 1" in output


def test_diagnostic_output_uses_placeholders_for_missing_fields(capsys):
    job = Job(
        source="telegram",
        source_job_id="missing-fields",
        title="Backend Developer",
        company="Unknown",
        location="Not specified",
        description="Hiring Backend Developer",
        url="https://t.me/jobs_alpha/1",
    )

    TelegramSource._print_diagnostic_job(job)
    output = capsys.readouterr().out

    assert "Company Name: Not specified" in output
    assert "Location: Not specified" in output
    assert "Apply Link: Not available" in output
    assert "Experience Level: Not specified" in output


def test_collect_counts_intent_noise_before_job_normalization():
    two_channel_yaml()
    messages = [
        tg_msg("jobs_alpha", 1, "Join our Python course"),
        tg_msg("jobs_alpha", 2, "20+ Job Portals. One Curated List. Fresh Jobs Every Day."),
        tg_msg("jobs_alpha", 3, "Selected, Congratulations..."),
        tg_msg("jobs_alpha", 4, "Career tips for data analysts"),
        tg_msg("jobs_alpha", 5, JOB_TEXT),
    ]
    source = make_source({"jobs_alpha": messages, "jobs_beta": []})

    jobs = asyncio.run(source.collect())

    assert len(jobs) == 1
    assert source.last_stats["messages_checked"] == 5
    assert source.last_stats["promotional_rejected"] == 1
    assert source.last_stats["aggregator_rejected"] == 1
    assert source.last_stats["selection_rejected"] == 1
    assert source.last_stats["informational_rejected"] == 1
    assert source.last_stats["jobs_normalized"] == 1


def test_in_scan_reposts_collapse():
    two_channel_yaml()
    src = make_source({
        "jobs_alpha": [tg_msg("jobs_alpha", 1)],
        "jobs_beta": [tg_msg("jobs_beta", 9, text="BUMP " + JOB_TEXT)],  # repost, same app URL
    })
    jobs = asyncio.run(src.collect())
    assert len(jobs) == 1


def test_disabled_globally_does_nothing():
    two_channel_yaml(enabled=False)

    class Boom:
        pass

    src = TelegramSource()

    async def must_not_run():
        raise AssertionError("client must not be created when disabled")

    src._ensure_client = must_not_run
    assert asyncio.run(src.collect()) == []


def test_channel_failure_isolated():
    two_channel_yaml()
    src = make_source({
        "jobs_alpha": [tg_msg("jobs_alpha", 1)],
        "jobs_beta": ValueError("Cannot find any entity corresponding to jobs_beta"),
    })
    jobs = asyncio.run(src.collect())
    assert [j.raw_data["source_channel"] for j in jobs] == ["jobs_alpha"]


def test_flood_wait_does_not_crash_scan():
    two_channel_yaml()
    flood = telegram_source.FloodWaitError.__new__(telegram_source.FloodWaitError)
    flood.seconds = 0
    src = make_source({
        "jobs_alpha": flood,
        "jobs_beta": [tg_msg("jobs_beta", 3)],
    })
    jobs = asyncio.run(src.collect())
    assert [j.raw_data["source_channel"] for j in jobs] == ["jobs_beta"]


def test_missing_credentials_inactive_not_crash(monkeypatch):
    from hirevia.sources import telegram_config

    monkeypatch.setattr(telegram_config, "load_dotenv", lambda *a, **k: False)
    two_channel_yaml()
    src = TelegramSource()  # real _ensure_client -> raises TelegramAuthError (no creds)
    assert asyncio.run(src.collect()) == []


# --- real fetch logic against a fake Telethon client -------------------------

class FakeMessage:
    def __init__(self, message_id, date, text):
        self.id = message_id
        self.date = date
        self.raw_text = text
        self.message = text
        self.entities = []


class _Ent:
    def __init__(self, url):
        self.url = url


class FakeClient:
    """Mimics the read-only slice of Telethon the collector uses (newest-first)."""

    def __init__(self, messages):
        self.messages = messages

    async def get_entity(self, name):
        return name

    def iter_messages(self, entity, offset_id=None, limit=None):
        async def gen():
            for m in self.messages:
                if offset_id is not None and m.id <= offset_id:
                    continue
                yield m
        return gen()


def test_first_scan_respects_lookback_and_updates_cursor():
    channel = TelegramChannel(username="jobs_alpha", lookback_hours=24)
    client = FakeClient([
        FakeMessage(100, NOW, JOB_TEXT),
        FakeMessage(95, NOW - timedelta(hours=2), "hello everyone"),
        FakeMessage(50, NOW - timedelta(hours=100), JOB_TEXT),  # outside lookback
    ])
    collected = asyncio.run(TelegramSource()._fetch_channel_messages(client, channel))
    assert [m.message_id for m in collected] == [100, 95]
    assert telegram_source._last_seen_ids["jobs_alpha"] == 100


def test_second_scan_only_fetches_newer_than_cursor():
    telegram_source._last_seen_ids["jobs_alpha"] = 100
    channel = TelegramChannel(username="jobs_alpha")
    client = FakeClient([
        FakeMessage(110, NOW, "new post"),
        FakeMessage(100, NOW, "already seen"),
    ])
    collected = asyncio.run(TelegramSource()._fetch_channel_messages(client, channel))
    assert [m.message_id for m in collected] == [110]
    assert telegram_source._last_seen_ids["jobs_alpha"] == 110


def test_message_links_from_entities_and_text():
    message = FakeMessage(1, NOW, "Apply: https://example.com/a")
    message.entities = [_Ent("https://hidden-button.example/b")]
    links = telegram_source.message_links(message)
    assert links[0] == "https://hidden-button.example/b"  # entity links first
    assert "https://example.com/a" in links


def test_ensure_client_without_credentials_raises(monkeypatch):
    from hirevia.sources import telegram_config

    monkeypatch.setattr(telegram_config, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(TelegramAuthError):
        asyncio.run(telegram_source.ensure_client())

