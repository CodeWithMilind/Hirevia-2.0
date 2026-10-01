"""Tests for the Telegram collector (TelegramSource) with a mocked client.

No Telethon connection is ever made: _ensure_client / _fetch_channel_messages
are replaced with fakes, and one test drives the real fetch logic through a
fake Telethon client to verify cursor + lookback behaviour.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hirevia.core.models import Job
from hirevia.sources import telegram_source
from hirevia.sources.telegram_config import TelegramAuthError, TelegramChannel
from hirevia.sources.telegram_extract import TgMessage
from hirevia.sources.telegram_source import TelegramSource
from hirevia.storage.state import JobRepository

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
    telegram_source._state_store = None
    telegram_source._shared_client = None
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(var, raising=False)
    yield
    telegram_source._last_seen_ids.clear()
    telegram_source._state_store = None
    telegram_source._shared_client = None


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


def test_channel_collection_exception_is_logged_with_traceback(caplog):
    two_channel_yaml()
    src = make_source({
        "jobs_alpha": RuntimeError("mock channel failure"),
        "jobs_beta": [],
    })

    with caplog.at_level(logging.ERROR, logger="hirevia.sources.telegram_source"):
        asyncio.run(src.collect())

    failure = next(record for record in caplog.records if "Channel jobs_alpha failed" in record.message)
    assert failure.exc_info is not None


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

    def iter_messages(self, entity, offset_id=None, limit=None, min_id=0, reverse=False):
        async def gen():
            messages = [m for m in self.messages if m.id > min_id]
            if offset_id is not None:
                messages = [m for m in messages if m.id > offset_id]
            messages.sort(key=lambda message: message.id, reverse=not reverse)
            for m in messages[:limit]:
                yield m
        return gen()


def test_first_scan_respects_lookback_and_updates_cursor():
    channel = TelegramChannel(username="jobs_alpha", lookback_hours=24)
    client = FakeClient([
        FakeMessage(100, NOW, JOB_TEXT),
        FakeMessage(95, NOW - timedelta(hours=2), "hello everyone"),
        FakeMessage(50, NOW - timedelta(hours=100), JOB_TEXT),  # outside lookback
    ])
    source = TelegramSource()
    collected = asyncio.run(source._fetch_channel_messages(client, channel))
    assert [m.message_id for m in collected] == [100, 95]
    assert source._pending_message_ids["jobs_alpha"] == 100
    assert "jobs_alpha" not in telegram_source._last_seen_ids
    assert source.commit_processed_messages() is True
    assert telegram_source._last_seen_ids["jobs_alpha"] == 100


def test_second_scan_only_fetches_newer_than_cursor():
    telegram_source._last_seen_ids["jobs_alpha"] = 100
    channel = TelegramChannel(username="jobs_alpha")
    client = FakeClient([
        FakeMessage(110, NOW, "new post"),
        FakeMessage(100, NOW, "already seen"),
    ])
    source = TelegramSource()
    collected = asyncio.run(source._fetch_channel_messages(client, channel))
    assert [m.message_id for m in collected] == [110]
    assert telegram_source._last_seen_ids["jobs_alpha"] == 100
    assert source.commit_processed_messages() is True
    assert telegram_source._last_seen_ids["jobs_alpha"] == 110


def test_cursor_backfill_pages_new_messages_without_skipping_ids(tmp_path):
    write_yaml(
        "telegram:\n"
        "  channels:\n"
        "    - username: jobs_alpha\n"
        "      max_messages: 2\n"
    )
    repo = JobRepository(tmp_path / "paged-state.db")
    telegram_source.bind_state_store(repo)
    repo.set_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_alpha"), 100)
    channel = TelegramChannel(username="jobs_alpha", max_messages=2)
    client = FakeClient([
        FakeMessage(105, NOW, JOB_TEXT),
        FakeMessage(104, NOW, JOB_TEXT),
        FakeMessage(103, NOW, JOB_TEXT),
        FakeMessage(102, NOW, JOB_TEXT),
        FakeMessage(101, NOW, JOB_TEXT),
    ])
    source = TelegramSource()

    first_page = asyncio.run(source._fetch_channel_messages(client, channel))
    assert [message.message_id for message in first_page] == [101, 102]
    source.commit_processed_messages()
    assert telegram_source._cursor_for("jobs_alpha") == 102

    second_page = asyncio.run(source._fetch_channel_messages(client, channel))
    assert [message.message_id for message in second_page] == [103, 104]
    source.commit_processed_messages()
    assert telegram_source._cursor_for("jobs_alpha") == 104

    final_page = asyncio.run(source._fetch_channel_messages(client, channel))
    assert [message.message_id for message in final_page] == [105]
    source.commit_processed_messages()
    assert telegram_source._cursor_for("jobs_alpha") == 105


def test_event_messages_persist_per_channel_and_duplicates_are_ignored(tmp_path):
    two_channel_yaml()
    repo = JobRepository(tmp_path / "telegram-state.db")
    source = TelegramSource()
    source.bind_state(repo)
    source._event_mode = True
    source._event_queue = asyncio.Queue()
    source._event_channels = {
        "jobs_alpha": TelegramChannel(username="jobs_alpha"),
        "jobs_beta": TelegramChannel(username="jobs_beta"),
    }

    async def enqueue_and_collect(username, message_id):
        await source._event_queue.put((username, FakeMessage(message_id, NOW, JOB_TEXT)))
        return await source.collect()

    alpha_jobs = asyncio.run(enqueue_and_collect("jobs_alpha", 105))
    assert len(alpha_jobs) == 1
    assert repo.get_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_alpha")) is None
    assert source.commit_processed_messages() is True

    beta_jobs = asyncio.run(enqueue_and_collect("jobs_beta", 25))
    assert len(beta_jobs) == 1
    assert source.commit_processed_messages() is True
    assert repo.get_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_alpha")) == "105"
    assert repo.get_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_beta")) == "25"

    telegram_source._last_seen_ids.clear()
    restarted = TelegramSource()
    restarted.bind_state(repo)
    restarted._event_mode = True
    restarted._event_queue = asyncio.Queue()
    restarted._event_channels = source._event_channels

    async def read_seen_message():
        await restarted._event_queue.put(("jobs_alpha", FakeMessage(105, NOW, JOB_TEXT)))
        return await restarted.collect()

    assert asyncio.run(read_seen_message()) == []
    assert repo.get_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_beta")) == "25"


def test_event_non_job_is_acknowledged_without_a_job(tmp_path):
    two_channel_yaml()
    repo = JobRepository(tmp_path / "non-job-state.db")
    source = TelegramSource()
    source.bind_state(repo)
    source._event_mode = True
    source._event_queue = asyncio.Queue()
    source._event_channels = {"jobs_alpha": TelegramChannel(username="jobs_alpha")}

    async def collect_non_job():
        await source._event_queue.put((
            "jobs_alpha", FakeMessage(108, NOW, "Join our Python course")
        ))
        return await source.collect()

    assert asyncio.run(collect_non_job()) == []
    assert source.commit_processed_messages() is True
    assert repo.get_source_state("telegram", telegram_source.CURSOR_KEY.format("jobs_alpha")) == "108"


def test_uncommitted_event_retries_before_later_queued_messages():
    two_channel_yaml()
    source = TelegramSource()
    source._event_mode = True
    source._event_queue = asyncio.Queue()
    source._event_channels = {"jobs_alpha": TelegramChannel(username="jobs_alpha")}
    failed_message = FakeMessage(109, NOW, JOB_TEXT)
    later_message = FakeMessage(110, NOW, JOB_TEXT_B)
    source._active_event = ("jobs_alpha", failed_message)
    source._pending_message_ids["jobs_alpha"] = 109
    source._event_queue.put_nowait(("jobs_alpha", later_message))

    assert source.retry_uncommitted_event() is True

    async def collect_retry_then_later():
        retry_jobs = await source.collect()
        assert retry_jobs[0].raw_data["source_message_id"] == 109
        assert source._event_queue.qsize() == 1
        assert source.commit_processed_messages() is True
        later_jobs = await source.collect()
        assert later_jobs[0].raw_data["source_message_id"] == 110

    asyncio.run(collect_retry_then_later())


def test_new_message_handler_queues_unseen_updates_by_channel():
    source = TelegramSource()
    source._event_queue = asyncio.Queue(maxsize=5)
    source._event_channels = {
        "jobs_alpha": TelegramChannel(username="jobs_alpha"),
        "jobs_beta": TelegramChannel(username="jobs_beta"),
    }
    telegram_source._last_seen_ids["jobs_alpha"] = 105

    async def receive_updates():
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_alpha"),
            message=FakeMessage(105, NOW, JOB_TEXT),
        ))
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_alpha"),
            message=FakeMessage(106, NOW, JOB_TEXT),
        ))
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_beta"),
            message=FakeMessage(5, NOW, JOB_TEXT),
        ))

    asyncio.run(receive_updates())

    assert source._event_queue.qsize() == 2
    assert source._event_queue.get_nowait()[0] == "jobs_alpha"
    assert source._event_queue.get_nowait()[0] == "jobs_beta"


def test_full_event_queue_recovers_dropped_updates_from_channel_history():
    source = TelegramSource()
    source._event_queue = asyncio.Queue(maxsize=1)
    source._event_channels = {
        "jobs_alpha": TelegramChannel(username="jobs_alpha", max_messages=2)
    }
    source._stop_connection_watch = asyncio.Event()

    class RecoveryClient(FakeClient):
        def is_connected(self):
            return True

    source._listener_client = RecoveryClient([
        FakeMessage(202, NOW, JOB_TEXT),
        FakeMessage(201, NOW, JOB_TEXT),
        FakeMessage(200, NOW, JOB_TEXT),
    ])

    async def collect_recovered_ids():
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_alpha"),
            message=FakeMessage(200, NOW, JOB_TEXT),
        ))
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_alpha"),
            message=FakeMessage(201, NOW, JOB_TEXT),
        ))
        recovered = []
        for _ in range(30):
            await asyncio.sleep(0)
            while not source._event_queue.empty():
                _, message = source._event_queue.get_nowait()
                recovered.append(message.id)
            if source._recovery_task.done() and source._event_queue.empty():
                break
        await source.stop_continuous_monitoring()
        return recovered

    message_ids = asyncio.run(collect_recovered_ids())

    assert message_ids == [200, 201, 202]
    assert not source._overflow_channels


def test_continuous_monitor_registers_every_enabled_channel_and_stops():
    write_yaml(
        "telegram:\n"
        "  channels:\n"
        "    - username: jobs_alpha\n"
        "    - username: jobs_beta\n"
        "      enabled: false\n"
        "    - username: jobs_gamma\n"
    )
    source = TelegramSource()
    registered = []

    class FakeClient:
        def is_connected(self):
            return True

    client = FakeClient()

    async def fake_ensure_client():
        return client

    async def fake_register_event_handler(client_arg, channels):
        registered.extend(channel.username for channel in channels)
        source._listener_client = client_arg

    source._ensure_client = fake_ensure_client
    source._register_event_handler = fake_register_event_handler

    async def start_and_stop():
        count = await source.start_continuous_monitoring()
        assert count == 2
        assert source._event_queue.maxsize == 1000
        await source.stop_continuous_monitoring()

    asyncio.run(start_and_stop())

    assert registered == ["jobs_alpha", "jobs_gamma"]
    assert source._connection_task is None


def test_scan_24h_fetches_history_while_event_mode_is_active(tmp_path):
    from hirevia.storage.state import JobRepository

    write_yaml("telegram:\n  channels:\n    - username: jobs_alpha\n")
    source = TelegramSource()
    source._event_mode = True
    source.force_24h_lookback = True
    source.bind_state(JobRepository(tmp_path / "scan-24h.db"))
    fetched = []

    async def use_client():
        return object()

    async def fetch_history(client, channel):
        fetched.append(channel.username)
        return [TgMessage(channel.username, 45, NOW, JOB_TEXT, [])]

    async def fail_if_event_mode_is_used(channels):
        raise AssertionError("Scan 24h must fetch channel history")

    source._ensure_client = use_client
    source._fetch_channel_messages = fetch_history
    source._collect_queued_message = fail_if_event_mode_is_used

    jobs = asyncio.run(source.collect())

    assert fetched == ["jobs_alpha"]
    assert len(jobs) == 1


def test_reconnect_reattaches_handler_without_resetting_channel_cursor():
    source = TelegramSource()
    channel = TelegramChannel(username="jobs_alpha")
    old_client = object()
    new_client = object()
    registrations = []
    source._listener_client = old_client
    telegram_source._last_seen_ids["jobs_alpha"] = 105

    async def fake_ensure_client():
        return new_client

    async def fake_register_event_handler(client, channels):
        registrations.append((client, channels))

    source._ensure_client = fake_ensure_client
    source._register_event_handler = fake_register_event_handler

    asyncio.run(source._reconnect_listener([channel]))

    assert source._listener_client is new_client
    assert registrations == [(new_client, [channel])]
    assert telegram_source._last_seen_ids["jobs_alpha"] == 105


def test_reconnect_requests_telethon_update_catch_up():
    source = TelegramSource()
    channel = TelegramChannel(username="jobs_alpha")

    class FakeClient:
        def __init__(self):
            self.catch_up_calls = 0

        async def catch_up(self):
            self.catch_up_calls += 1

    client = FakeClient()
    source._listener_client = client
    source._ensure_client = lambda: asyncio.sleep(0, result=client)

    asyncio.run(source._reconnect_listener([channel]))

    assert client.catch_up_calls == 1


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

