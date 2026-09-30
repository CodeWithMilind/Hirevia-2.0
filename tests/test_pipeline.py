"""Integration tests for the scan pipeline.

Everything external is faked: sources, the AI matcher and the notifier. The
repository is real (tmp SQLite) because dedup + notification bookkeeping is
exactly what these tests are about. verify_application_urls is off, so no
network access happens.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hirevia.config import Config
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


# --- happy path + cost gate ---------------------------------------------------

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
    assert stats.jobs_new == 2  # alerted job + stored-but-unusable one
    assert stats.jobs_ai_matched == 1
    assert stats.jobs_ai_rejected == 1
    assert stats.jobs_link_rejected == 1
    assert stats.jobs_notified == 1
    assert stats.jobs_failed == 0
    assert stats.errors == []

    # The channel-only post never reached the model: the link gate saves money.
    assert "DevOps Engineer" not in matcher.calls
    assert {"Backend Developer", "Sales Executive"} == set(matcher.calls)

    # The unusable-link job is history, not a lead: stored, never notified.
    stored = repo.get_by_source_id("telegram", "jobs_alpha-2")
    assert stored is not None and stored.notified_at is None
    assert stored.raw_data["application_url_quality"] == "social"
    assert stored.raw_data["application_url_usable"] is False

    # The alerted job remembers it was alerted, so the next scan stays quiet.
    alerted = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert alerted.notified_at is not None and alerted.notification_channel == "telegram"
    assert alerted.raw_data["freshness_label"]
    assert str(alerted.url).startswith("https://t.me/")
    assert str(alerted.application_url).startswith("https://boards.greenhouse.io/")


def test_missing_application_url_is_stored_but_never_matched_or_alerted(repo):
    job = make_job(apply=None)
    matcher = FakeMatcher()
    notifier = FakeNotifier()

    stats = run(build_pipeline([FakeSource([job])], repo, matcher, notifier))

    assert stats.jobs_link_rejected == 1
    assert matcher.calls == []
    assert notifier.sent == []
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert stored is not None
    assert stored.raw_data["application_url_usable"] is False


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
    stats = run(build_pipeline([FakeSource([job])], repo, FakeMatcher(), notifier))

    assert stats.jobs_matched == 1
    assert stats.jobs_skipped_notification == 1
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

def test_ai_outage_keeps_the_job_but_never_alerts(repo):
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
    assert notifier.sent == []
    assert stats.jobs_skipped_notification == 1 and stats.jobs_notified == 0
    stored = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert stored is not None
    verdict = stored.verdict_model()
    assert verdict.match is True and verdict.degraded is True
    assert verdict.ai_error == "model down"
    assert verdict.ai_score is None and verdict.deterministic_score > 0
    assert any("AI degraded" in err for err in stats.errors)


def test_p3_below_threshold_is_not_alerted(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/7")])
    marginal = FakeMatcher(default=AIResponse(relevant=True, priority=Priority.P3, match_score=61,
                                             reasons=["partial skill overlap"]))
    notifier = FakeNotifier()
    stats = run(build_pipeline([source], repo, marginal, notifier, notify_min_match_score=80))

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
    stats = run(build_pipeline([source], repo, strong, notifier,
                               notify_min_match_score=80, ai_match_weight=1.0))

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


def test_high_ai_and_deterministic_scores_cannot_override_a_hard_veto(repo):
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
    original_match = pipeline._match
    observed_verdicts = []

    async def capture_verdict(job, tag):
        verdict = await original_match(job, tag)
        observed_verdicts.append(verdict)
        return verdict

    pipeline._match = capture_verdict

    stats = run(pipeline)

    assert stats.jobs_below_match == 1
    assert notifier.sent == []
    verdict = observed_verdicts[0]
    assert verdict.hard_reject is True
    assert "8+ years" in " ".join(verdict.reject_reasons)
    assert verdict.ai_score == 100
    assert verdict.deterministic_score >= 75


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
    assert stats.jobs_skipped_notification == 1
    assert stats.jobs_new == 1  # still recorded as history


def test_without_notifier_jobs_are_stored_and_nothing_is_alerted(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/10")])
    stats = run(build_pipeline([source], repo, FakeMatcher(), None))

    assert stats.jobs_notified == 0
    assert stats.jobs_skipped_notification == 1
    assert stats.jobs_new == 1


def test_failed_alert_is_retried_on_the_next_scan(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/11")])
    failing = FakeNotifier(ok=False)
    first = run(build_pipeline([source], repo, FakeMatcher(), failing))

    assert first.jobs_notified == 0
    assert any("notification failed" in err for err in first.errors)
    assert repo.get_by_source_id("telegram", "jobs_alpha-1").notified_at is None

    # Same job later, now with a working notifier: it is still a fresh lead.
    working = FakeNotifier()
    second = run(build_pipeline([source], repo, FakeMatcher(), working))
    assert second.jobs_notified == 1 and working.sent == ["Backend Developer"]
    stored = repo.get_by_source_id("telegram", "jobs_alpha-1")
    assert stored.notified_at is not None
    assert stored.notification_channel == "telegram"


def test_ai_disabled_stores_without_alerting(repo):
    source = FakeSource([make_job(apply="https://boards.greenhouse.io/acme/jobs/12")])
    notifier = FakeNotifier()
    stats = run(build_pipeline([source], repo, None, notifier))

    # No ranking means no ranking-based decision to alert on.
    assert stats.jobs_notified == 0 and stats.jobs_new == 1
    assert notifier.sent == [] and stats.jobs_skipped_notification == 1


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

