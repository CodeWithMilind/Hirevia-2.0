"""Integration tests for the scan pipeline.

Everything external is faked: sources, the AI matcher and the notifier. The
repository is real (tmp SQLite) because dedup + notification bookkeeping is
exactly what these tests are about. verify_application_urls is off, so no
network access happens.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hirevia.config import Config
from hirevia.core.matching import analyze
from hirevia.core.models import AIResponse, ApplicationStatus, CandidateProfile, Job, MatchResult, Priority
from hirevia.core.pipeline import Pipeline
from hirevia.core.verification import UrlQuality
from hirevia.storage.state import JobRepository


# --- doubles -----------------------------------------------------------------

class FakeSource:
    """Stand-in for a collector: returns canned jobs, records the state store."""

    name = "telegram"

    def __init__(self, jobs=(), error=None, counters=None):
        self.jobs = list(jobs)
        self.error = error
        self.bound_to = None
        self.collect_calls = 0
        self.last_stats = counters or {}

    def bind_state(self, repo):
        self.bound_to = repo

    async def collect(self):
        self.collect_calls += 1
        if self.error:
            raise self.error
        return list(self.jobs)


class BrokenSource(FakeSource):
    name = "rss"


class FakeRegistry:
    def __init__(self, *sources):
        self._sources = list(sources)

    def get_enabled_sources(self):
        return self._sources


class FakeMatcher:
    """Canned AI verdicts keyed by job title."""

    def __init__(self, decisions=None, default=None):
        self.decisions = decisions or {}
        self.default = default or AIResponse(
            relevant=True, priority=Priority.P1, match_score=92, reasons=["test verdict"]
        )
        self.calls = []

    async def match(self, job, profile):
        self.calls.append(job.title)
        return self.decisions.get(job.title, self.default)


class FakeNotifier:
    name = "telegram"

    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send(self, job, decision):
        self.sent.append(job.title)
        return self.ok

    async def close(self):
        pass


def make_job(title="Backend Developer", company="Acme Corp", apply=None, channel="jobs_alpha",
             message_id=1, posted_at=None):
    return Job(
        source="telegram",
        source_job_id=f"{channel}-{message_id}",
        title=title,
        company=company,
        location="Remote",
        description=f"{title} at {company}. Python, FastAPI, 1-3 years.",
        url=f"https://t.me/{channel}/{message_id}",
        application_url=apply,
        posted_at=posted_at,
        raw_data={"source_channel": channel, "source_channels": [channel],
                  "source_message_id": message_id},
    )


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Keep cwd-based config/profile lookups out of the repo."""
    monkeypatch.chdir(tmp_path)
    for var in ("VERIFY_APPLICATION_URLS", "NOTIFY_MIN_MATCH_SCORE", "NOTIFY_MAX_AGE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def repo(tmp_path):
    return JobRepository(tmp_path / "pipeline.db")


def build_pipeline(sources, repo, matcher=None, notifier=None, **overrides):
    config = Config()
    config.verify_application_urls = False
    for key, value in overrides.items():
        setattr(config, key, value)
    pipeline = Pipeline(config, repo, matcher, notifier)
    pipeline.registry = FakeRegistry(*sources)
    return pipeline


def run(pipeline):
    return asyncio.run(pipeline.run_scan())


# --- happy path + assessment eligibility ------------------------------------

def test_scan_alerts_only_the_apply_ready_job(repo):
    good = make_job("Backend Developer", apply="https://boards.greenhouse.io/acme/jobs/1")
    channel_only = make_job("DevOps Engineer", apply="https://t.me/acme_hiring", message_id=2)
    irrelevant = make_job("Sales Executive", apply="https://jobs.lever.co/acme/sales",
                          message_id=3, channel="jobs_beta")
    source = FakeSource([good, channel_only, irrelevant],
                        counters={"messages_checked": 40, "jobs_detected": 12,
                                  "jobs_normalized": 3})
    matcher = FakeMatcher(decisions={
        "Sales Executive": AIResponse(relevant=False, priority=Priority.P3, match_score=20,
                                      reasons=["no sales experience"]),
    })
    notifier = FakeNotifier()
    stats = run(build_pipeline([source], repo, matcher, notifier))

    assert notifier.sent == ["Backend Developer"]
    assert stats.jobs_found == 3
    assert (stats.messages_checked, stats.jobs_detected, stats.jobs_normalized) == (40, 12, 3)
    assert stats.jobs_new == 3  # alerted, unusable-link, and unmatched jobs
    assert stats.jobs_ai_matched == 2
    assert stats.jobs_ai_rejected == 1
    assert stats.jobs_link_rejected == 1
    assert stats.jobs_notified == 1
    assert stats.jobs_failed == 0
    assert stats.errors == []

    # Application-link quality controls alerting, not model assessment.
    assert set(matcher.calls) == {
        "Backend Developer", "DevOps Engineer", "Sales Executive"
    }

    # The unusable-link job is history, not a lead: stored, never notified.
    stored = repo.get_by_source_id("telegram", "jobs_alpha-2")
    assert stored is not None and stored.notified_at is None
    assert stored.raw_data["application_url_quality"] == "social"
    assert stored.raw_data["application_url_usable"] is False
    assert stored.match_result["ai_status"] == "ok"

    unmatched = repo.get_by_source_id("telegram", "jobs_beta-3")
    assert unmatched is not None and unmatched.notified_at is None
    assert unmatched.match_result["match"] is False
    assert unmatched.ai_decision["relevant"] is False

    # The alerted job remembers it was alerted, so the next scan stays quiet.
    alerted = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert alerted.notified_at is not None and alerted.notification_channel == "telegram"
    assert alerted.raw_data["freshness_label"]
    assert str(alerted.url).startswith("https://t.me/")
    assert str(alerted.application_url).startswith("https://boards.greenhouse.io/")


def test_ai_rejected_job_is_persisted_without_becoming_notification_eligible(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/unmatched")
    matcher = FakeMatcher(default=AIResponse(
        relevant=False, priority=Priority.P3, match_score=32,
        reasons=["the role does not fit the candidate"],
    ))
    notifier = FakeNotifier()

    stats = run(build_pipeline(
        [FakeSource([job])], repo, matcher, notifier, ai_match_weight=1.0
    ))

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored is not None
    assert stored.ai_decision["relevant"] is False
    assert stored.match_result["match"] is False
    assert stored.match_result["match_score"] == 32
    assert stored.match_result["priority"] == "P3"
    assert stored.match_result["ai_score"] == 32
    assert stats.jobs_new == 1 and stats.jobs_below_match == 1
    assert stats.jobs_notification_eligible == 0
    assert notifier.sent == []


def test_completed_assessment_persists_structured_ai_evidence(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/structured")
    matcher = FakeMatcher(default=AIResponse(
        relevant=True,
        priority=Priority.P2,
        match_score=82,
        reason="The backend responsibilities fit the candidate profile.",
        matched_skills=["Python", "FastAPI"],
        missing_skills=["SQL"],
        experience_match=True,
        role_match=True,
        location_match=True,
        employment_match=True,
        fresher_eligible=True,
    ))
    pipeline = build_pipeline([FakeSource([job])], repo, matcher, ai_match_weight=1.0)
    pipeline.profile = CandidateProfile(
        candidate_type="fresher",
        skills=["Python", "FastAPI", "SQL"],
        preferred_roles=["Backend Developer"],
        preferred_locations=["Remote"],
    )

    run(pipeline)

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored.match_result["assessment_status"] == "completed"
    assert stored.match_result["ai_score"] == 82
    assert stored.match_result["deterministic_score"] == analyze(job, pipeline.profile).score
    assert stored.match_result["matched_skills"] == ["Python", "FastAPI"]
    assert stored.match_result["missing_skills"] == []
    assert stored.match_result["fresher_eligible"] is True
    assert "responsibilities fit" in stored.match_result["reason"]


def test_retry_after_persistence_reuses_the_same_telegram_job_record(repo):
    class RetryOnceRepository(JobRepository):
        fail_completion = True

        def complete_telegram_job(self, message_identity):
            if self.fail_completion:
                self.fail_completion = False
                raise RuntimeError("simulate interruption after persistence")
            return super().complete_telegram_job(message_identity)

    retry_repo = RetryOnceRepository(repo.db_path)
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/retry")
    source = FakeSource([job])
    matcher = FakeMatcher(default=AIResponse(
        relevant=False, priority=Priority.P3, match_score=32,
        reasons=["not a fit"],
    ))

    first = run(build_pipeline([source], retry_repo, matcher, FakeNotifier(), ai_match_weight=1.0))
    assert first.jobs_new == 1 and first.jobs_failed == 1
    with retry_repo._get_conn() as connection:
        connection.execute(
            "UPDATE processing_queue SET available_at = datetime('now', '-1 second')"
        )
        connection.commit()

    second = run(build_pipeline([source], retry_repo, matcher, FakeNotifier(), ai_match_weight=1.0))

    assert second.jobs_new == 0
    assert retry_repo.get_by_source_id(job.source, job.source_job_id).match_result["match"] is False
    with retry_repo._get_conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_scan_logs_collection_matching_priorities_and_notification(repo, caplog):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")
    source = FakeSource([job], counters={"messages_checked": 4, "jobs_detected": 1})
    notifier = FakeNotifier()
    pipeline = build_pipeline([source], repo, FakeMatcher(), notifier)

    with caplog.at_level(logging.INFO, logger="hirevia.core.pipeline"):
        stats = run(pipeline)

    assert stats.jobs_matched == 1
    assert stats.jobs_notified == 1
    assert "Source collection started: 1 source(s): telegram" in caplog.text
    assert "Source telegram collection started" in caplog.text
    assert "Source telegram collected 1 job(s)" in caplog.text
    assert "AI request started for 'Backend Developer'" in caplog.text
    assert "Matching completed for 'Backend Developer'" in caplog.text
    assert "Priority distribution: P1=0 P2=1 P3=0" in caplog.text
    assert stats.priority_distribution == {"P1": 0, "P2": 1, "P3": 0}
    assert "Stage 7: alerted 'Backend Developer'" in caplog.text
    assert "Scan finished" in caplog.text


def test_scan_logs_collector_failures_with_tracebacks(repo, caplog):
    source = FakeSource(error=RuntimeError("collector unavailable"))
    pipeline = build_pipeline([source], repo)

    with caplog.at_level(logging.INFO, logger="hirevia.core.pipeline"):
        stats = run(pipeline)

    failure = next(record for record in caplog.records if "Source telegram failed" in record.message)
    assert stats.jobs_failed == 0
    assert failure.exc_info is not None


def test_ai_provider_failures_are_visible_in_scan_logs(repo, caplog):
    class FailedMatcher:
        provider_name = "mock-provider"
        model = "mock-model"

        async def match(self, job, profile):
            return AIResponse.failure("mock provider timeout")

    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")
    pipeline = build_pipeline([FakeSource([job])], repo, FailedMatcher())

    with caplog.at_level(logging.WARNING, logger="hirevia.core.pipeline"):
        stats = run(pipeline)

    assert stats.ai_failures == 1
    assert "AI provider failed for 'Backend Developer' (mock provider timeout)" in caplog.text


def test_missing_application_url_is_assessed_but_never_alerted(repo):
    job = make_job(apply=None)
    matcher = FakeMatcher()
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job])], repo, matcher, notifier))

    assert stats.jobs_link_rejected == 1
    assert matcher.calls == [job.title]
    assert notifier.sent == []
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored is not None
    assert stored.raw_data["application_url_usable"] is False
    assert stored.match_result["ai_status"] == "ok"
    assert stored.ai_decision["relevant"] is True


def test_unconfigured_ai_job_is_persisted_as_retrying(repo):
    job = make_job(apply="https://jobs.lever.co/acme/backend")
    pipeline = build_pipeline([FakeSource([job])], repo)
    events = []
    pipeline.ai_progress_callback = lambda event, **details: events.append((event, details))

    stats = run(pipeline)

    assert [event for event, _ in events] == ["start", "processing", "job_complete"]
    assert events[1][1]["job"].source_job_id == job.source_job_id
    assert events[2][1]["status"] == "AI RETRYING"
    assert events[2][1]["score"] is None
    assert stats.jobs_rule_scored == 1
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored.match_result["assessment_status"] == "retrying"
    assert stored.match_result["deterministic_score"] == analyze(job, pipeline.profile).score
    assert stored.match_result["match"] is None and stored.match_result["ai_score"] is None


def test_collector_cursor_commits_after_successful_pipeline_run(repo):
    class CommitSource(FakeSource):
        def __init__(self, jobs):
            super().__init__(jobs)
            self.commits = 0

        def commit_processed_messages(self):
            self.commits += 1
            return True

    source = CommitSource([
        make_job(apply="https://jobs.lever.co/acme/backend")
    ])

    stats = run(build_pipeline([source], repo, FakeMatcher()))

    assert stats.jobs_failed == 0
    assert source.commits == 1


def test_collector_cursor_is_not_committed_after_job_failure(repo):
    class CommitSource(FakeSource):
        def __init__(self, jobs):
            super().__init__(jobs)
            self.commits = 0

        def commit_processed_messages(self):
            self.commits += 1
            return True

    source = CommitSource([
        make_job(apply="https://jobs.lever.co/acme/backend")
    ])
    pipeline = build_pipeline([source], repo)

    async def fail_job(*args, **kwargs):
        raise RuntimeError("simulated processing failure")

    pipeline._process_job = fail_job

    stats = run(pipeline)

    assert stats.jobs_failed == 1
    assert source.commits == 0


def test_event_messages_use_pipeline_job_dedup_and_notification_protection(repo, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramChannel
    from hirevia.sources.telegram_source import TelegramSource

    telegram_source._last_seen_ids.pop("jobs_alpha", None)
    Path("telegram.yaml").write_text(
        "telegram:\n  enabled: true\n  channels:\n    - username: jobs_alpha\n",
        encoding="utf-8",
    )
    source = TelegramSource()
    source.bind_state(repo)
    source._event_mode = True
    source._event_channels = {"jobs_alpha": TelegramChannel(username="jobs_alpha")}
    source._event_queue = asyncio.Queue()
    posted_text = (
        "Hiring: Backend Developer\n"
        "Company: Acme Corp\n"
        "Location: Pune\n"
        "Experience: 1-3 years\n"
        "Apply: https://jobs.lever.co/acme/backend"
    )
    for message_id in (105, 106):
        source._event_queue.put_nowait((
            "jobs_alpha",
            SimpleNamespace(
                id=message_id,
                date=datetime.now(timezone.utc),
                raw_text=posted_text,
                message=posted_text,
                entities=[],
            ),
        ))

    notifier = FakeNotifier()
    pipeline = build_pipeline(
        [source], repo, FakeMatcher(), notifier, notify_min_match_score=0
    )

    first = run(pipeline)
    second = run(pipeline)

    assert first.jobs_new == 1
    assert second.jobs_duplicate == 1
    assert notifier.sent == ["Backend Developer"]
    assert repo.get_source_state(
        "telegram", telegram_source.CURSOR_KEY.format("jobs_alpha")
    ) == "106"


def test_telegram_event_queue_receives_updates_while_ai_is_processing(repo, tmp_path):
    from hirevia.sources import telegram_source
    from hirevia.sources.telegram_config import TelegramChannel
    from hirevia.sources.telegram_source import TelegramSource

    telegram_source._last_seen_ids.pop("jobs_alpha", None)
    Path("telegram.yaml").write_text(
        "telegram:\n  enabled: true\n  channels:\n    - username: jobs_alpha\n",
        encoding="utf-8",
    )
    source = TelegramSource()
    source.bind_state(repo)
    source._event_mode = True
    source._event_channels = {"jobs_alpha": TelegramChannel(username="jobs_alpha")}
    source._event_queue = asyncio.Queue(maxsize=2)

    def telegram_message(message_id, text):
        return SimpleNamespace(
            id=message_id,
            date=datetime.now(timezone.utc),
            raw_text=text,
            message=text,
            entities=[],
        )

    first_text = (
        "Hiring: Backend Developer\nCompany: Acme Corp\nLocation: Pune\n"
        "Experience: 1-3 years\nApply: https://jobs.lever.co/acme/backend"
    )
    second_text = (
        "Hiring: Data Analyst\nCompany: Beta Labs\nLocation: Remote\n"
        "Experience: 1-3 years\nApply: https://jobs.lever.co/beta/analyst"
    )
    source._event_queue.put_nowait(("jobs_alpha", telegram_message(201, first_text)))

    class WaitingMatcher:
        def __init__(self):
            self.calls = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def match(self, job, profile):
            self.calls.append(job.title)
            if len(self.calls) == 1:
                self.started.set()
                await self.release.wait()
            return AIResponse(
                relevant=True, priority=Priority.P1, match_score=92,
                reasons=["test verdict"],
            )

    matcher = WaitingMatcher()
    pipeline = build_pipeline([source], repo, matcher)

    async def run_with_pending_update():
        first_scan = asyncio.create_task(pipeline.run_scan())
        await matcher.started.wait()
        await source._on_new_message(SimpleNamespace(
            chat=SimpleNamespace(username="jobs_alpha"),
            message=telegram_message(202, second_text),
        ))
        assert source._event_queue.qsize() == 1
        matcher.release.set()
        await first_scan
        await pipeline.run_scan()

    asyncio.run(run_with_pending_update())

    assert matcher.calls == ["Backend Developer", "Data Analyst"]
    assert telegram_source._cursor_for("jobs_alpha") == 202


def test_ai_progress_is_printable_before_matcher_wait_and_completes_after_pipeline(repo):
    events = []

    class ObservedMatcher(FakeMatcher):
        async def match(self, job, profile):
            events.append(("matcher_started", job.title))
            return self.default

    job = make_job(apply="https://jobs.lever.co/acme/backend")
    next_job = make_job(
        title="Data Analyst", company="Beta Labs",
        apply="https://jobs.lever.co/beta/analyst", message_id=2,
    )
    pipeline = build_pipeline([FakeSource([job, next_job])], repo, ObservedMatcher())
    pipeline.ai_progress_callback = lambda event, **details: events.append((event, details))

    run(pipeline)

    event_names = [event for event, _ in events]
    assert event_names.index("processing") < event_names.index("sending_ai")
    assert event_names.index("sending_ai") < event_names.index("matcher_started")
    assert event_names.index("matcher_started") < event_names.index("ai_response")
    assert event_names.index("ai_response") < event_names.index("job_complete")
    complete = events[event_names.index("job_complete")][1]
    assert complete["status"] == "MATCHED"
    assert complete["score"] > 0
    first_complete = next(
        index for index, (event, details) in enumerate(events)
        if event == "job_complete" and details["index"] == 1
    )
    second_processing = next(
        index for index, (event, details) in enumerate(events)
        if event == "processing" and details["index"] == 2
    )
    assert first_complete < second_processing


def test_profile_hard_reject_and_clear_role_miss_still_reach_ai(repo):
    senior = make_job(
        "Senior Java Architect", apply="https://jobs.lever.co/acme/senior",
        message_id=21,
    )
    senior.description = "Java architecture role requiring 8+ years."
    strong = make_job(
        "Junior Python Backend Developer", apply="https://jobs.lever.co/acme/python",
        message_id=22,
    )
    strong.description = "Python, FastAPI, 0-2 years, full-time."
    ambiguous = make_job(
        "Software Engineer - AI Platform", apply="https://jobs.lever.co/acme/ai",
        message_id=23,
    )
    ambiguous.description = "Build an AI platform."
    irrelevant = make_job(
        "Sales Executive", apply="https://jobs.lever.co/acme/sales", message_id=24,
    )
    irrelevant.description = "Cold calling, sales quotas, and customer acquisition."
    jobs = [senior, strong, ambiguous, irrelevant]
    matcher = FakeMatcher()
    pipeline = build_pipeline([FakeSource(jobs)], repo, matcher)
    pipeline.profile = CandidateProfile(
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        skills=["Python", "FastAPI"],
        preferred_roles=["Python Backend Developer", "Software Engineer"],
        role_families=["python_backend", "ai_ml"],
        preferred_locations=["Pune", "Remote"],
        remote_preference="preferred",
        employment_types=["full_time", "internship"],
    )

    stats = run(pipeline)

    assert set(matcher.calls) == {job.title for job in jobs}
    assert stats.jobs_filtered == 0
    assert stats.jobs_hard_filter_passed == 4
    assert stats.jobs_rule_scored == 4
    assert stats.jobs_matching_attempted == 4
    assert stats.jobs_ai_skipped == 0
    assert len(jobs) == 4


@pytest.mark.parametrize("intent_category", ["unknown", "ambiguous"])
def test_ambiguous_telegram_intent_is_never_notification_eligible(repo, intent_category):
    job = make_job(apply="https://jobs.lever.co/acme/backend")
    job.raw_data["intent_category"] = intent_category
    matcher = FakeMatcher()
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job])], repo, matcher, notifier))

    assert matcher.calls == [job.title]
    assert stats.jobs_matching_attempted == 1
    assert stats.jobs_verification_attempted == 1
    assert stats.jobs_notification_eligible == 0
    assert stats.jobs_skipped_notification == 1
    assert notifier.sent == []


def test_scheduled_walk_in_without_external_url_remains_actionable(repo):
    job = make_job(apply=None)
    job.raw_data.update({
        "intent_category": "hiring_drive",
        "manual_application_action": True,
    })
    matcher = FakeMatcher()
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job])], repo, matcher, notifier))

    assert stats.jobs_link_rejected == 0
    assert stats.jobs_verification_attempted == 1
    assert stats.jobs_unverified == 1
    assert stats.jobs_notification_eligible == 1
    assert notifier.sent == [job.title]
    assert str(job.url) == "https://t.me/jobs_alpha/1"


@pytest.mark.parametrize(
    "application_url, expected_quality",
    [
        ("https://careers.acme.com/jobs/backend", "company"),
        ("https://indeed.com/viewjob?jk=123", "job_board"),
    ],
)
def test_company_and_job_board_links_remain_alertable(repo, application_url, expected_quality):
    job = make_job(apply=application_url)
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job])], repo, FakeMatcher(), notifier))

    assert stats.jobs_notified == 1
    assert notifier.sent == [job.title]
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored.raw_data["application_url_quality"] == expected_quality


def test_titleless_posting_is_not_alerted_even_with_a_valid_apply_link(repo):
    job = make_job(title="", apply="https://jobs.lever.co/acme/backend")
    notifier = FakeNotifier()
    matcher = FakeMatcher()
    stats = run(build_pipeline([FakeSource([job])], repo, matcher, notifier))

    assert stats.jobs_filtered == 1
    assert stats.jobs_matching_attempted == 0
    assert matcher.calls == []
    assert notifier.sent == []


def test_second_scan_is_duplicate_and_stays_silent(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")
    source = FakeSource([job])
    notifier = FakeNotifier()
    first = run(build_pipeline([source], repo, FakeMatcher(), notifier))
    second = run(build_pipeline([source], repo, FakeMatcher(), notifier))

    assert first.jobs_notified == 1 and first.jobs_new == 1
    assert second.jobs_duplicate == 1 and second.jobs_new == 0
    assert second.jobs_notified == 0 and notifier.sent == ["Backend Developer"]
    # Dedup keeps one record and stamps the repost time.
    assert len(repo.get_recent_jobs(hours=48)) == 1


def test_same_canonical_application_url_is_not_alerted_twice(repo):
    first = make_job(apply="https://boards.greenhouse.io/acme/jobs/44?source=feed")
    second = make_job(
        apply="https://www.boards.greenhouse.io/acme/jobs/44/?source=repost",
        channel="jobs_beta",
        message_id=2,
    )
    notifier = FakeNotifier()

    first_stats = run(build_pipeline([FakeSource([first])], repo, FakeMatcher(), notifier))
    second_stats = run(build_pipeline([FakeSource([second])], repo, FakeMatcher(), notifier))

    assert first_stats.jobs_notified == 1
    assert second_stats.jobs_duplicate == 1
    assert second_stats.jobs_notified == 0
    assert notifier.sent == ["Backend Developer"]


# --- robustness ---------------------------------------------------------------

def test_dead_source_only_costs_itself(repo):
    broken = BrokenSource(error=RuntimeError("feed gone"))
    healthy = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/2")])
    stats = run(build_pipeline([broken, healthy], repo, FakeMatcher(), FakeNotifier()))

    assert stats.sources_scanned == 2
    assert stats.jobs_new == 1
    assert "rss: RuntimeError: feed gone" in stats.errors


def test_source_without_bind_state_is_supported(repo):
    class PlainSource:
        name = "apify"

        async def collect(self):
            return [make_job(apply="https://boards.greenhouse.io/acme/jobs/3")]

    assert not hasattr(PlainSource(), "bind_state")
    stats = run(build_pipeline([PlainSource()], repo, FakeMatcher(), FakeNotifier()))
    assert stats.jobs_new == 1 and stats.errors == []


def test_bind_state_receives_the_repository(repo):
    source = FakeSource([])
    run(build_pipeline([source], repo))
    assert source.bound_to is repo


def test_broken_job_does_not_stop_the_scan(repo):
    class ExplodingRepo(JobRepository):
        def upsert_job(self, job):
            if job.title == "Bomb":
                raise RuntimeError("disk on fire")
            return super().upsert_job(job)

    exploding = ExplodingRepo(repo.db_path)
    source = FakeSource([
        make_job("Bomb", apply="https://boards.greenhouse.io/acme/jobs/4"),
        make_job("Backend Developer", apply="https://boards.greenhouse.io/acme/jobs/5",
                 message_id=2),
    ])
    stats = run(build_pipeline([source], exploding, FakeMatcher(), FakeNotifier()))

    assert stats.jobs_failed == 1
    assert stats.jobs_new == 1
    assert stats.jobs_notified == 1
    assert any("disk on fire" in err for err in stats.errors)



# --- notification policy ------------------------------------------------------

def test_ai_outage_keeps_telegram_job_queued_for_retry(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/6")])
    degraded = FakeMatcher(default=AIResponse.failure(RuntimeError("model down")))
    notifier = FakeNotifier()
    pipeline = build_pipeline([source], repo, degraded, notifier)
    pipeline.profile = CandidateProfile(
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        skills=["Python", "FastAPI"],
        preferred_roles=["Backend Developer"],
        preferred_locations=["Remote"],
        remote_preference="preferred",
    )
    stats = run(pipeline)

    assert stats.ai_failures == 1
    assert stats.jobs_failed == 1
    assert notifier.sent == []
    stored = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert stored is not None
    assert stored.match_result["assessment_status"] == "retrying"
    assert stored.match_result["match"] is None and stored.match_result["ai_score"] is None
    assert repo.get_telegram_queue_status()["queued"] == 1
    assert any("AI degraded" in err for err in stats.errors)


def test_assessment_retry_exhaustion_is_failed_not_unmatched(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/retry-limit")
    source = FakeSource([job])
    matcher = FakeMatcher(default=AIResponse.failure(RuntimeError("provider down")))
    pipeline = build_pipeline([source], repo, matcher)
    pipeline.config.ai.assessment_max_attempts = 2

    first = run(pipeline)
    assert first.jobs_failed == 1
    with repo._get_conn() as connection:
        connection.execute("UPDATE processing_queue SET available_at = datetime('now', '-1 second')")
        connection.commit()

    second = run(pipeline)

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert second.jobs_failed == 1
    assert stored.match_result["assessment_status"] == "failed"
    assert stored.match_result["match"] is None
    assert stored.match_result["match_score"] is None
    assert stored.match_result["ai_score"] is None
    assert repo.get_telegram_queue_status()["queued"] == 0
    with repo._get_conn() as connection:
        assert connection.execute("SELECT status FROM processing_queue").fetchone()[0] == "FAILED"
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_pending_assessment_resumes_after_worker_restart_without_duplicate(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/restart")
    source = FakeSource([job])
    failed = FakeMatcher(default=AIResponse.failure(RuntimeError("temporary outage")))
    first = run(build_pipeline([source], repo, failed))
    assert first.jobs_failed == 1

    with repo._get_conn() as connection:
        connection.execute("UPDATE processing_queue SET status = 'PROCESSING'")
        connection.execute(
            "UPDATE telegram_message_history SET status = 'PROCESSING'"
        )
        connection.commit()
    assert repo.recover_processing_queue() == 1

    recovered = FakeMatcher()
    second = run(build_pipeline([source], repo, recovered))

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert second.jobs_failed == 0
    assert recovered.calls == [job.title]
    assert stored.match_result["assessment_status"] == "completed"
    with repo._get_conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_legacy_skipped_telegram_job_is_backfilled_without_duplicate(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/legacy")
    job.match_result = {
        "match": False,
        "match_score": 31,
        "priority": "P3",
        "ai_status": "skipped",
        "ai_score": None,
        "deterministic_score": 31,
    }
    repo.upsert_job(job)
    repo.record_telegram_message("jobs_alpha", "1")
    repo.mark_telegram_message_processed("jobs_alpha", "1")
    matcher = FakeMatcher()
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([])], repo, matcher, notifier))

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert matcher.calls == [job.title]
    assert stored.match_result["assessment_status"] == "completed"
    assert stored.match_result["ai_score"] == 92
    assert stats.jobs_new == 0
    assert notifier.sent == [job.title]
    with repo._get_conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_legacy_assessment_backfill_queues_only_eligible_jobs_once(repo):
    legacy = make_job(message_id="legacy-no-link")
    legacy.match_result = {"deterministic_score": 31}
    filtered = make_job(message_id="legacy-filtered")
    filtered.raw_data["filter_status"] = "FILTERED"
    completed = make_job(message_id="legacy-completed")
    completed.match_result = {"ai_status": "ok", "match_score": 82}
    for job in (legacy, filtered, completed):
        repo.upsert_job(job)

    matcher = FakeMatcher()
    pipeline = build_pipeline([FakeSource([])], repo, matcher)
    run(pipeline)
    run(pipeline)

    assert matcher.calls == [legacy.title]
    assert repo.get_telegram_queue_status()["queued"] == 0
    stored_legacy = repo.get_by_source_id(legacy.source, legacy.source_job_id)
    stored_filtered = repo.get_by_source_id(filtered.source, filtered.source_job_id)
    assert stored_legacy.match_result["assessment_status"] == "completed"
    assert stored_filtered.match_result is None


def test_p3_below_threshold_is_not_alerted(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/7")])
    marginal = FakeMatcher(default=AIResponse(relevant=True, priority=Priority.P3, match_score=61,
                                             reasons=["partial skill overlap"]))
    notifier = FakeNotifier()
    stats = run(build_pipeline(
        [source], repo, marginal, notifier, notify_min_match_score=80,
        priority_p1_min_score=95, priority_p2_min_score=90, priority_p3_min_score=40,
        match_min_score=45,
    ))

    assert notifier.sent == []
    assert stats.jobs_skipped_notification == 1 and stats.jobs_notified == 0


def test_p3_above_threshold_is_alerted(repo):
    """P3 job with hybrid score above notify_min_match_score should alert.
    
    Uses ai_match_weight=1.0 so hybrid score == AI score, isolating the
    notification threshold logic from deterministic blending.
    """
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/8")])
    strong = FakeMatcher(default=AIResponse(relevant=True, priority=Priority.P3, match_score=88,
                                           reasons=["strong score"]))
    notifier = FakeNotifier()
    stats = run(build_pipeline(
        [source], repo, strong, notifier, notify_min_match_score=80,
        ai_match_weight=1.0, priority_p1_min_score=95,
        priority_p2_min_score=90, priority_p3_min_score=40,
    ))

    assert notifier.sent == ["Backend Developer"] and stats.jobs_notified == 1


def test_p2_with_a_canonical_match_is_alerted(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/80")])
    p2 = FakeMatcher(default=AIResponse(
        relevant=True, priority=Priority.P2, match_score=75, reasons=["good fit"]
    ))
    notifier = FakeNotifier()

    stats = run(build_pipeline([source], repo, p2, notifier, ai_match_weight=1.0))

    assert stats.jobs_notified == 1
    assert notifier.sent == ["Backend Developer"]


def test_profile_hard_veto_is_assessed_but_still_controls_final_decision(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/81")
    job.description = "Python FastAPI. Requires 8+ years of experience."
    matcher = FakeMatcher(default=AIResponse(
        relevant=True, priority=Priority.P1, match_score=100, reasons=["strong fit"]
    ))
    notifier = FakeNotifier()
    pipeline = build_pipeline([FakeSource([job])], repo, matcher, notifier, ai_match_weight=1.0)
    pipeline.profile = CandidateProfile(
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        skills=["Python", "FastAPI"],
        preferred_roles=["Backend Developer"],
        preferred_locations=["Remote"],
        remote_preference="preferred",
    )
    stats = run(pipeline)

    assert stats.jobs_filtered == 0
    assert stats.jobs_matching_attempted == 1
    assert matcher.calls == [job.title]
    assert notifier.sent == []
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored is not None
    assert stored.match_result["hard_reject"] is True
    assert stored.match_result["ai_status"] == "ok"


def test_future_timezone_aware_posting_does_not_crash_eligibility(repo):
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    job = make_job(
        apply="https://boards.greenhouse.io/acme/jobs/82",
        posted_at=future,
    )
    naive_job = make_job(
        apply="https://boards.greenhouse.io/acme/jobs/83",
        channel="jobs_beta",
        message_id=2,
    )
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job, naive_job])], repo, FakeMatcher(), notifier))

    assert stats.jobs_failed == 0
    assert stats.jobs_notified == 2
    assert notifier.sent == [job.title, naive_job.title]


def test_stale_job_is_recorded_but_not_alerted(repo):
    old = make_job(apply="https://boards.greenhouse.io/acme/jobs/9",
                   posted_at=datetime.utcnow() - timedelta(hours=120))
    notifier = FakeNotifier()
    stats = run(build_pipeline([FakeSource([old])], repo, FakeMatcher(), notifier,
                               notify_max_age_hours=72))

    assert notifier.sent == []
    assert stats.jobs_new == 0
    assert repo.get_by_source_id("telegram", old.source_job_id) is None
    assert repo.get_telegram_queue_status()["queued"] == 0
    with repo._get_conn() as conn:
        history = conn.execute(
            "SELECT status FROM telegram_message_history"
        ).fetchone()
    assert history["status"] == "EXPIRED"


def test_without_notifier_jobs_are_stored_and_nothing_is_alerted(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/10")])
    stats = run(build_pipeline([source], repo, FakeMatcher(), None))

    assert stats.jobs_notified == 0
    assert stats.jobs_skipped_notification == 1
    assert stats.jobs_new == 1


def test_processed_telegram_message_is_not_reprocessed_on_later_scans(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/11")])
    failing = FakeNotifier(ok=False)
    first = run(build_pipeline([source], repo, FakeMatcher(), failing))

    assert first.jobs_notified == 0
    assert any("notification failed" in err for err in first.errors)
    assert repo.get_by_source_id("telegram", "jobs_alpha-1").notified_at is None

    # Terminal message history prevents reprocessing the same Telegram post.
    working = FakeNotifier()
    second = run(build_pipeline([source], repo, FakeMatcher(), working))
    assert second.jobs_duplicate == 1
    assert second.jobs_notified == 0 and working.sent == []
    stored = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert stored.notified_at is None


def test_ai_disabled_stores_without_alerting(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/12")])
    notifier = FakeNotifier()
    stats = run(build_pipeline([source], repo, None, notifier))

    assert stats.jobs_notified == 0 and stats.jobs_new == 1
    assert notifier.sent == [] and stats.jobs_failed == 1
    stored = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert stored.match_result["assessment_status"] == "retrying"
    assert stored.match_result["match"] is None


@pytest.mark.parametrize(
    "status,usable,expected_sent",
    [
        (ApplicationStatus.CLOSED, False, []),
        (ApplicationStatus.UNKNOWN, True, ["Backend Developer"]),
    ],
)
def test_verification_status_controls_alertability_without_live_http(
    repo, monkeypatch, status, usable, expected_sent
):
    job = make_job(apply="https://jobs.lever.co/acme/backend")
    notifier = FakeNotifier()

    async def fake_verify(*args, **kwargs):
        return status, UrlQuality("ats", usable, 90 if usable else 0, "fake result")

    monkeypatch.setattr("hirevia.core.pipeline.verify_application", fake_verify)
    pipeline = build_pipeline([FakeSource([job])], repo, FakeMatcher(), notifier,
                              verify_application_urls=True)
    stats = run(pipeline)

    assert notifier.sent == expected_sent
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored.application_status is status
    if status is ApplicationStatus.CLOSED:
        assert stats.jobs_closed == 1
        assert stats.jobs_skipped_notification == 1
    else:
        assert stats.jobs_unverified == 1


@pytest.mark.parametrize("status", [ApplicationStatus.CLOSED, ApplicationStatus.EXPIRED])
def test_closed_or_expired_job_is_not_retried_after_a_failed_alert(repo, status):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")
    job.application_status = status
    job.match_result = MatchResult(
        match=True,
        match_score=92,
        priority=Priority.P1,
        ai_status="ok",
        ai_score=92,
        deterministic_score=85,
    ).model_dump(mode="json")
    repo.upsert_job(job)
    notifier = FakeNotifier()
    repost = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")

    stats = run(build_pipeline([FakeSource([repost])], repo, FakeMatcher(), notifier))

    assert stats.jobs_duplicate == 1
    assert notifier.sent == []


def test_failed_alert_retry_respects_the_canonical_nonmatch_verdict(repo):
    job = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")
    job.ai_decision = AIResponse(
        relevant=False, priority=Priority.P1, match_score=95
    ).model_dump(mode="json")
    repo.upsert_job(job)
    notifier = FakeNotifier()
    repost = make_job(apply="https://boards.greenhouse.io/acme/jobs/1")

    run(build_pipeline([FakeSource([repost])], repo, FakeMatcher(), notifier))

    assert notifier.sent == []

