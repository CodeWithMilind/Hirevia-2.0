"""End-to-end Phase 7 flow with fake source, AI, verifier, and Telegram transport."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hirevia.ai.client import AITimeoutError
from hirevia.ai.matcher import AIMatcher
from hirevia.config import Config
from hirevia.core.models import ApplicationStatus, CandidateProfile, Priority
from hirevia.core.pipeline import Pipeline
from hirevia.core.verification import UrlQuality
from hirevia.notifications.telegram import TelegramNotifier
from hirevia.sources.telegram_extract import TgMessage, message_to_job
from hirevia.storage.state import JobRepository


APPLICATION_URL = "https://boards.greenhouse.io/acme/jobs/phase7"
JOB_TEXT = (
    "Hiring: Backend Developer\n"
    "Company: Acme Corp\n"
    "Location: Pune\n"
    "Experience: 0-2 years\n"
    "Skills: Python, FastAPI, SQL\n"
    f"Apply: {APPLICATION_URL}\n"
    "Join our group: https://t.me/acme_jobs"
)
AI_PAYLOAD = {
    "match": True,
    "match_score": 92,
    "priority": "P1",
    "reason": "Python and FastAPI match the candidate's target backend role.",
    "matched_skills": ["Python", "FastAPI"],
    "missing_skills": ["Docker"],
    "experience_match": True,
    "location_match": True,
    "role_match": True,
}


class FakeSource:
    name = "telegram"

    def __init__(self, jobs):
        self.jobs = jobs

    async def collect(self):
        return list(self.jobs)


class FakeRegistry:
    def __init__(self, source):
        self.source = source

    def get_enabled_sources(self):
        return [self.source]


class FakeNVIDIAClient:
    model = "nvidia/fake-model"

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def structured_completion(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeTelegramResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = "fake response"
        self.headers = {}

    def json(self):
        return self.payload


class FakeTelegramClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        response = self.responses.pop(0) if self.responses else FakeTelegramResponse(200, {"ok": True})
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self):
        pass


class FakeVerifier:
    def __init__(self, status=ApplicationStatus.OPEN, usable=True):
        self.status = status
        self.usable = usable
        self.calls = []

    async def __call__(self, job, **kwargs):
        self.calls.append(str(job.application_url))
        quality = UrlQuality(
            "ats", self.usable, 90 if self.usable else 0, "fake verifier result"
        )
        return self.status, quality


def extracted_job(*, channel="acme_jobs", message_id=7, date=None, text=JOB_TEXT):
    message = TgMessage(
        channel=channel,
        message_id=message_id,
        date=date or datetime.now(timezone.utc),
        text=text,
    )
    job = message_to_job(message)
    assert job is not None
    return job


def candidate_profile():
    return CandidateProfile(
        name="Test Candidate",
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        accept_fresher=True,
        skills=["Python", "FastAPI", "SQL", "Docker"],
        preferred_roles=["Backend Developer"],
        preferred_locations=["Pune"],
        remote_preference="preferred",
        employment_types=["full_time", "internship"],
    )


def build_pipeline(monkeypatch, tmp_path, job, ai_outcome=AI_PAYLOAD,
                   telegram_responses=None, verifier_status=ApplicationStatus.OPEN,
                   verifier_usable=True, max_age_hours=72):
    config = Config()
    config.ai.fallback = []
    config.verify_application_urls = True
    config.notify_max_age_hours = max_age_hours

    fake_ai = FakeNVIDIAClient(ai_outcome)
    monkeypatch.setattr("hirevia.ai.matcher.NVIDIAClient", lambda *_: fake_ai)
    matcher = AIMatcher(config, "fake-nvidia-key")

    notifier = TelegramNotifier("fake-telegram-token", "test-chat", retry_attempts=1)
    fake_telegram = FakeTelegramClient(
        telegram_responses or [FakeTelegramResponse(200, {"ok": True})]
    )
    notifier.client = fake_telegram

    verifier = FakeVerifier(verifier_status, verifier_usable)
    monkeypatch.setattr("hirevia.core.pipeline.verify_application", verifier)

    source = FakeSource([job])
    repo = JobRepository(tmp_path / "phase7-integration.db")
    pipeline = Pipeline(config, repo, matcher, notifier)
    pipeline.registry = FakeRegistry(source)
    pipeline.profile = candidate_profile()
    return pipeline, repo, source, fake_ai, fake_telegram, verifier


def run(pipeline):
    return asyncio.run(pipeline.run_scan())


def test_fresh_telegram_job_completes_pipeline_and_deduplicates(monkeypatch, tmp_path):
    job = extracted_job()
    pipeline, repo, source, fake_ai, telegram, verifier = build_pipeline(
        monkeypatch, tmp_path, job
    )

    first = run(pipeline)

    assert first.jobs_notified == 1
    assert first.jobs_verified == 1
    assert fake_ai.calls and len(fake_ai.calls) == 1
    assert verifier.calls == [APPLICATION_URL]
    assert str(job.url).startswith("https://t.me/")
    assert str(job.application_url) == APPLICATION_URL

    payload = telegram.calls[0]["json"]
    assert payload["parse_mode"] == "HTML"
    assert "P1 - Job Match" in payload["text"]
    assert "Role: Backend Developer" in payload["text"]
    assert "Company: Acme Corp" in payload["text"]
    assert "Matched: Python, FastAPI" in payload["text"]
    assert "Missing: Docker" in payload["text"]
    assert "Python and FastAPI match" in payload["text"]
    assert f"Apply: {APPLICATION_URL}" in payload["text"]
    assert "Source: acme_jobs" in payload["text"]

    stored = repo.get_by_source_id(job.source, job.source_job_id)
    verdict = stored.verdict_model()
    assert stored.notified_at is not None
    assert stored.notification_channel == "telegram"
    assert stored.application_status is ApplicationStatus.OPEN
    assert verdict.match and verdict.ai_status == "ok"
    assert verdict.deterministic_score > 0 and verdict.ai_score == 92
    assert verdict.match_score == stored.match_result["match_score"]

    second = run(pipeline)
    assert second.jobs_duplicate == 1
    assert second.jobs_notified == 0
    assert len(fake_ai.calls) == 1
    assert len(telegram.calls) == 1

    cross_source = job.model_copy(update={
        "source": "greenhouse",
        "source_job_id": "greenhouse-duplicate-7",
        "url": "https://boards.greenhouse.io/acme/jobs/phase7",
    })
    source.jobs = [cross_source]
    third = run(pipeline)
    assert third.jobs_duplicate == 1
    assert third.jobs_notified == 0
    assert len(telegram.calls) == 1


def test_failed_telegram_delivery_retries_from_the_stored_verdict(monkeypatch, tmp_path):
    job = extracted_job(message_id=8)
    pipeline, repo, _, fake_ai, telegram, _ = build_pipeline(
        monkeypatch,
        tmp_path,
        job,
        telegram_responses=[
            FakeTelegramResponse(500, {"ok": False}),
            FakeTelegramResponse(200, {"ok": True}),
        ],
    )
    pipeline.notifier._retry_attempts = 1

    first = run(pipeline)
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert first.jobs_notified == 0
    assert stored.notified_at is None
    assert stored.verdict_model().match is True

    second = run(pipeline)
    stored = repo.get_by_source_id(job.source, job.source_job_id)
    assert second.jobs_duplicate == 1
    assert second.jobs_notified == 1
    assert len(fake_ai.calls) == 1
    assert stored.notified_at is not None
    assert stored.notification_channel == "telegram"
    assert len(telegram.calls) == 2


@pytest.mark.parametrize(
    "case, expected_status",
    [
        ("missing-link", ApplicationStatus.UNKNOWN),
        ("malformed-link", ApplicationStatus.UNKNOWN),
        ("closed", ApplicationStatus.CLOSED),
        ("stale", ApplicationStatus.OPEN),
        ("hard-veto", ApplicationStatus.UNKNOWN),
        ("ai-failure", ApplicationStatus.OPEN),
        ("malformed-ai", ApplicationStatus.OPEN),
        ("unknown-verification", ApplicationStatus.UNKNOWN),
    ],
)
def test_actionability_and_failure_cases_complete_without_false_alerts(
    monkeypatch, tmp_path, case, expected_status
):
    text = JOB_TEXT
    date = datetime.now(timezone.utc)
    ai_outcome = AI_PAYLOAD
    verify_status = ApplicationStatus.OPEN
    verify_usable = True
    max_age = 72

    if case == "missing-link":
        text = text.replace(f"Apply: {APPLICATION_URL}\n", "")
    elif case == "closed":
        verify_status = ApplicationStatus.CLOSED
        verify_usable = False
    elif case == "stale":
        date -= timedelta(days=5)
    elif case == "hard-veto":
        text = text.replace("Experience: 0-2 years", "Experience: 8+ years required")
    elif case == "ai-failure":
        ai_outcome = AITimeoutError("fake NVIDIA timeout")
    elif case == "malformed-ai":
        ai_outcome = {
            "match": "maybe",
            "match_score": "very high",
            "priority": "P9",
            "reason": None,
        }
    elif case == "unknown-verification":
        verify_status = ApplicationStatus.UNKNOWN

    job = extracted_job(message_id=20, date=date, text=text)
    if case == "malformed-link":
        job.application_url = "not a URL"
    pipeline, repo, _, fake_ai, telegram, verifier = build_pipeline(
        monkeypatch,
        tmp_path,
        job,
        ai_outcome=ai_outcome,
        verifier_status=verify_status,
        verifier_usable=verify_usable,
        max_age_hours=max_age,
    )

    stats = run(pipeline)

    assert stats.jobs_failed == 0
    if case == "unknown-verification":
        assert stats.jobs_notified == 1
        assert len(telegram.calls) == 1
        assert verifier.calls == [APPLICATION_URL]
    else:
        assert stats.jobs_notified == 0

    if case == "missing-link":
        assert job.application_url is None
        assert fake_ai.calls == []
        assert verifier.calls == []
    elif case == "malformed-link":
        assert fake_ai.calls == []
        assert verifier.calls == []
        stored = repo.get_by_source_id(job.source, job.source_job_id)
        assert stored is not None
        assert stored.application_url is None
        assert stored.raw_data["invalid_application_url"] == "not a URL"
        assert stored.raw_data["application_url_usable"] is False
    elif case == "hard-veto":
        assert fake_ai.calls == []
        assert stats.jobs_filtered == 1
        assert verifier.calls == []
    elif case == "ai-failure":
        stored = repo.get_by_source_id(job.source, job.source_job_id)
        assert stored is not None
        verdict = stored.verdict_model()
        assert verdict.degraded is True
        assert verdict.ai_error == "fake NVIDIA timeout"
        assert verdict.deterministic_score > 0
        assert telegram.calls == []
    elif case == "malformed-ai":
        assert fake_ai.calls
        assert stats.jobs_failed == 0
        assert telegram.calls == []
    elif case == "closed":
        assert verifier.calls == [APPLICATION_URL]
        stored = repo.get_by_source_id(job.source, job.source_job_id)
        assert stored.application_status is expected_status
    elif case == "stale":
        assert verifier.calls == [APPLICATION_URL]
        assert stats.jobs_skipped_notification == 1
    else:
        assert verifier.calls == [APPLICATION_URL]
