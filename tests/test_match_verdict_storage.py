"""Persistence and presentation of the Phase 7 verdict.

The pipeline produces one canonical MatchResult; this file pins the two things
that make it useful afterwards:

  * storage writes it as one JSON column, reads it back intact, migrates an
    older database that never had the column, and never lets a verdict-less pass
    erase a score the dashboard and the alert-retry path depend on;
  * Job.verdict()/verdict_model() and the alert text agree on which number is
    "the" score, and unreadable payloads degrade to "no verdict" instead of
    raising, because one bad row must not break a whole scan.
"""

import asyncio
import ctypes
import errno
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from hirevia.core.models import AIResponse, Job, MatchResult, Priority
from hirevia.notifications.base import Notifier
from hirevia.storage import state as storage_state
from hirevia.storage.state import JOB_COLUMNS, JobRepository


# --- helpers ------------------------------------------------------------------


def make_job(**overrides) -> Job:
    defaults = dict(
        source="telegram",
        source_job_id="jobs_alpha-1",
        title="Python Developer",
        company="Acme Corp",
        location="Pune",
        description="Python and FastAPI. Fresher friendly, 0-1 years.",
        url="https://t.me/jobs_alpha/1",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def hybrid_result(**overrides) -> MatchResult:
    defaults = dict(
        match=True,
        match_score=86,
        priority=Priority.P2,
        reason="Python Developer role wants 2 of your 4 skills",
        matched_skills=["Python", "FastAPI"],
        missing_skills=["Docker"],
        experience_match=True,
        ai_status="ok",
        ai_model="nvidia/mistral",
        ai_score=90,
        deterministic_score=74,
    )
    defaults.update(overrides)
    return MatchResult(**defaults)


def stored(result: MatchResult):
    """The dict shape Job.match_result holds (what a JSON round trip gives back)."""
    return result.model_dump(mode="json")


class RecordingNotifier(Notifier):
    """Concrete notifier with no transport, so only the rendering is tested."""

    name = "recording"

    def __init__(self):
        self.sent = []

    async def send(self, job: Job, decision: AIResponse) -> bool:
        self.sent.append(job.title)
        return True


def render(job: Job, decision: AIResponse) -> str:
    return RecordingNotifier().format_message(job, decision)


# --- storage -------------------------------------------------------------------


def test_match_result_survives_a_storage_round_trip(tmp_path):
    repo = JobRepository(tmp_path / "hirevia.db")
    job = make_job()
    job.match_result = stored(hybrid_result())

    assert repo.upsert_job(job) is True
    loaded = repo.get_by_source_id(job.source, job.source_job_id)

    assert loaded.match_result["match_score"] == 86
    verdict = loaded.verdict_model()
    assert verdict.match and verdict.match_score == 86
    assert verdict.priority is Priority.P2
    assert verdict.matched_skills == ["Python", "FastAPI"]
    assert verdict.deterministic_score == 74 and verdict.ai_score == 90
    assert verdict.experience_match is True


def test_telegram_processing_queue_survives_restart_and_preserves_history(tmp_path):
    database_path = tmp_path / "queue.db"
    repo = JobRepository(database_path)
    job = make_job(
        raw_data={"source_channel": "jobs_alpha", "source_message_id": 42},
        posted_at=datetime.utcnow(),
    )

    assert repo.enqueue_telegram_job(job) is True
    identity = "telegram:jobs_alpha:42"
    claimed = repo.claim_telegram_jobs(limit=1)
    assert len(claimed) == 1
    assert claimed[0][0] == identity
    assert claimed[0][1].title == job.title
    assert repo.claim_telegram_jobs(limit=1) == []

    restarted = JobRepository(database_path)
    assert restarted.recover_processing_queue() == 1
    recovered = restarted.claim_telegram_jobs(limit=1)
    assert len(recovered) == 1
    restarted.complete_telegram_job(identity)
    assert restarted.record_telegram_message("jobs_alpha", "42") is False
    assert restarted.get_telegram_queue_status()["queued"] == 0
    with restarted._get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM telegram_message_history WHERE message_identity = ?",
            (identity,),
        ).fetchone()
    assert row["status"] == "PROCESSED"


def test_expired_telegram_queue_payload_is_removed_but_history_remains(tmp_path):
    repo = JobRepository(tmp_path / "expired-queue.db")
    old_job = make_job(
        raw_data={"source_channel": "jobs_alpha", "source_message_id": 43},
        posted_at=datetime.utcnow() - timedelta(hours=25),
    )
    identity = "telegram:jobs_alpha:43"

    assert repo.enqueue_telegram_job(old_job) is True
    assert repo.expire_pending_telegram_jobs() == 1
    assert repo.claim_telegram_jobs() == []
    assert repo.record_telegram_message("jobs_alpha", "43") is False
    with repo._get_conn() as conn:
        queue_row = conn.execute(
            "SELECT 1 FROM processing_queue WHERE message_identity = ?", (identity,)
        ).fetchone()
        history = conn.execute(
            "SELECT status FROM telegram_message_history WHERE message_identity = ?",
            (identity,),
        ).fetchone()
    assert queue_row is None
    assert history["status"] == "EXPIRED"


def test_expiration_permanently_deletes_job_but_keeps_message_history(tmp_path):
    repo = JobRepository(tmp_path / "job-expiration.db")
    job = make_job(raw_data={"source_channel": "jobs_alpha", "source_message_id": 44})
    repo.upsert_job(job)
    repo.record_telegram_message("jobs_alpha", "44")
    repo.mark_telegram_message_processed("jobs_alpha", "44")
    with repo._get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET expires_at = datetime('now', '-1 second') WHERE id = ?",
            (job.id,),
        )
        conn.commit()

    assert repo.expire_jobs() == 1
    assert repo.get_job(job.id) is None
    assert repo.record_telegram_message("jobs_alpha", "44") is False
    with repo._get_conn() as conn:
        history = conn.execute(
            "SELECT status FROM telegram_message_history WHERE channel_id = ? AND message_id = ?",
            ("jobs_alpha", "44"),
        ).fetchone()
    assert history["status"] == "PROCESSED"


def test_delete_all_jobs_keeps_message_history(tmp_path):
    repo = JobRepository(tmp_path / "delete-all.db")
    for message_id in (45, 46):
        job = make_job(
            source_job_id=f"job-{message_id}",
            raw_data={"source_channel": "jobs_alpha", "source_message_id": message_id},
        )
        repo.upsert_job(job)
        repo.record_telegram_message("jobs_alpha", str(message_id))
        repo.mark_telegram_message_processed("jobs_alpha", str(message_id))

    assert repo.delete_all_jobs() == 2
    assert repo.get_active_jobs() == []
    assert repo.record_telegram_message("jobs_alpha", "45") is False
    with repo._get_conn() as conn:
        history_count = conn.execute(
            "SELECT COUNT(*) FROM telegram_message_history"
        ).fetchone()[0]
    assert history_count == 2


def test_worker_lease_is_single_owner_across_repository_instances(tmp_path, monkeypatch):
    database_path = tmp_path / "worker-lease.db"
    first = JobRepository(database_path)
    second = JobRepository(database_path)
    monkeypatch.setattr(first, "_pid_is_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(second, "_pid_is_alive", lambda pid: pid == 12345)

    assert first.claim_worker_lease(12345) is True
    assert second.claim_worker_lease(67890) is False
    assert second.get_worker_runtime()["pid"] == 12345
    first.release_worker_lease(12345)
    assert second.claim_worker_lease(67890) is True
    assert second.get_worker_runtime()["pid"] == 67890


def test_pid_is_alive_for_current_process():
    assert JobRepository._pid_is_alive(os.getpid()) is True


def test_pid_is_not_alive_for_nonexistent_process():
    assert JobRepository._pid_is_alive(2**31 - 1) is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process probe")
def test_linux_posix_probe_recognizes_current_process():
    assert JobRepository._posix_pid_is_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [None, 0, -1, True, 1.5, "not-a-pid", 10**100])
def test_invalid_pids_are_not_alive(pid):
    assert JobRepository._pid_is_alive(pid) is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProcessLookupError(errno.ESRCH, "missing"), False),
        (PermissionError(errno.EPERM, "inaccessible"), True),
    ],
)
def test_posix_pid_detection_distinguishes_missing_and_inaccessible(
    monkeypatch, error, expected
):
    calls = []

    def fake_kill(pid, signal_number):
        calls.append((pid, signal_number))
        raise error

    monkeypatch.setattr(storage_state.os, "kill", fake_kill)

    assert JobRepository._posix_pid_is_alive(12345) is expected
    assert calls == [(12345, 0)]


def test_windows_pid_detection_treats_access_denied_as_alive(monkeypatch):
    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class FakeKernel32:
        OpenProcess = FakeFunction(lambda *_: None)
        GetExitCodeProcess = FakeFunction(lambda *_: 0)
        CloseHandle = FakeFunction(lambda *_: 1)

    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    assert JobRepository._windows_pid_is_alive(12345) is True


@pytest.mark.parametrize(
    ("platform", "expected_helper"),
    [("nt", "windows"), ("posix", "posix")],
)
def test_pid_detection_dispatches_to_platform_helper(
    monkeypatch, platform, expected_helper
):
    calls = []
    monkeypatch.setattr(storage_state.os, "name", platform)
    monkeypatch.setattr(
        JobRepository,
        "_windows_pid_is_alive",
        staticmethod(lambda pid: calls.append(("windows", pid)) or True),
    )
    monkeypatch.setattr(
        JobRepository,
        "_posix_pid_is_alive",
        staticmethod(lambda pid: calls.append(("posix", pid)) or True),
    )

    assert JobRepository._pid_is_alive(42) is True
    assert calls == [(expected_helper, 42)]


def test_worker_commands_are_durable_and_claimed_once(tmp_path):
    database_path = tmp_path / "worker-commands.db"
    first = JobRepository(database_path)
    second = JobRepository(database_path)
    command_id = first.enqueue_worker_command("SCAN_24H", {"source": "dashboard"})

    claimed = second.claim_worker_commands()

    assert claimed == [{
        "id": command_id,
        "command": "SCAN_24H",
        "arguments": {"source": "dashboard"},
    }]
    assert first.claim_worker_commands() == []
    second.complete_worker_command(command_id, result={"success": True})
    assert first.get_worker_command(command_id) == {
        "status": "COMPLETED", "result": {"success": True}, "error": None,
    }


def test_interrupted_worker_commands_recover_without_replaying_shutdown(tmp_path):
    repo = JobRepository(tmp_path / "worker-command-recovery.db")
    retry_id = repo.enqueue_worker_command("START", {"source": "dashboard"})
    shutdown_id = repo.enqueue_worker_command("SHUTDOWN")
    repo.claim_worker_commands(limit=2)

    assert repo.recover_worker_commands() == 1
    assert repo.get_worker_command(retry_id)["status"] == "PENDING"
    assert repo.get_worker_command(shutdown_id) == {
        "status": "ERROR",
        "result": None,
        "error": "Worker exited before shutdown completed",
    }


def test_veto_and_degraded_error_fields_survive_a_storage_round_trip(tmp_path):
    repo = JobRepository(tmp_path / "hirevia.db")
    result = hybrid_result(
        match=False,
        match_score=40,
        priority=Priority.P3,
        reason="rejected: asks 8+ years",
        hard_reject=True,
        reject_reasons=["asks 8+ years, candidate targets 2"],
        ai_status="failed",
        ai_score=None,
        ai_priority=None,
        ai_error="timeout",
    )
    job = make_job(match_result=stored(result))

    repo.upsert_job(job)
    restored = repo.get_by_source_id(job.source, job.source_job_id).verdict_model()

    assert restored.match is False and restored.priority is Priority.P3
    assert restored.match_score == 40 and restored.deterministic_score == 74
    assert restored.ai_score is None and restored.degraded is True
    assert restored.ai_error == "timeout"
    assert restored.hard_reject is True
    assert restored.reject_reasons == ["asks 8+ years, candidate targets 2"]
    assert restored.matched_skills == ["Python", "FastAPI"]
    assert restored.missing_skills == ["Docker"]
    assert restored.reason == "rejected: asks 8+ years"


def test_an_old_database_gains_the_match_result_column(tmp_path):
    """A pre-Phase-7 DB has no match_result column at all. Reading, migrating and
    writing must all work - CREATE TABLE IF NOT EXISTS never touches it."""
    db = tmp_path / "legacy.db"
    legacy_cols = [c for c in JOB_COLUMNS if c != "match_result"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE jobs ("
            + ", ".join(f"{c} TEXT" for c in legacy_cols)
            + ", created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs (id, source, source_job_id, title, company, location,"
            " description, url, detected_at, skills, raw_data, application_status,"
            " ai_decision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-1", "telegram", "jobs_alpha-1", "Python Developer", "Acme Corp",
                "Pune", "Python FastAPI", "https://t.me/jobs_alpha/1",
                datetime.utcnow().isoformat(), "[]", "{}", "unknown",
                '{"relevant": true, "priority": "P2", "match_score": 77, "reasons": ["decent"]}',
            ),
        )

    repo = JobRepository(db)  # runs the additive migration

    loaded = repo.get_job("legacy-1")
    assert loaded.match_result is None
    assert loaded.verdict()["match_score"] == 77  # falls back to ai_decision

    repo.upsert_job(loaded)  # must not fail on the newly added column
    assert repo.get_job("legacy-1").match_result is None


def test_a_verdict_less_pass_does_not_erase_the_stored_one(tmp_path):
    """Collectors re-write rows the pipeline already scored. Losing the score
    would silently blind the dashboard and kill the alert retry."""
    repo = JobRepository(tmp_path / "hirevia.db")
    job = make_job()
    job.match_result = stored(hybrid_result())
    repo.upsert_job(job)

    re_seen = make_job()  # same source/source_job_id, no verdict this time
    assert repo.upsert_job(re_seen) is False

    assert re_seen.match_result["match_score"] == 86  # the object is honest too
    assert repo.get_job(job.id).match_result["match_score"] == 86


def test_a_newer_verdict_does_overwrite_the_stored_one(tmp_path):
    repo = JobRepository(tmp_path / "hirevia.db")
    job = make_job()
    job.match_result = stored(hybrid_result())
    repo.upsert_job(job)

    rescored = make_job()
    rescored.match_result = stored(hybrid_result(match_score=31, match=False, priority=Priority.P3))
    repo.upsert_job(rescored)

    assert repo.get_job(job.id).match_result["match_score"] == 31


def test_unreadable_payloads_are_no_verdict_not_a_crash(tmp_path):
    """Garbage in a JSON column is a missing verdict, never a broken scan - and
    never a type pydantic would reject on the Job model."""
    repo = JobRepository(tmp_path / "hirevia.db")
    job = make_job()
    repo.upsert_job(job)

    with sqlite3.connect(tmp_path / "hirevia.db") as conn:
        conn.execute("UPDATE jobs SET match_result = 'not json', skills = 'null' WHERE id = ?",
                     (job.id,))

    loaded = repo.get_job(job.id)
    assert loaded.match_result is None
    assert loaded.verdict_model() is None
    assert loaded.skills == []  # a null payload is treated as "no skills"
    assert repo.upsert_job(loaded) is False  # and the row is still writable


# --- which score is the real one ----------------------------------------------


def test_verdict_prefers_the_hybrid_result_over_the_raw_ai_answer():
    job = make_job()
    job.ai_decision = AIResponse(relevant=True, priority=Priority.P1, match_score=95,
                                 reasons=["the model was enthusiastic"]).model_dump(mode="json")
    job.match_result = stored(hybrid_result())

    assert job.verdict()["match_score"] == 86  # evidence-checked, not raw AI
    assert job.verdict_model().match_score == 86


def test_verdict_is_empty_when_a_job_was_never_matched():
    job = make_job()
    assert job.verdict() == {}
    assert job.verdict_model() is None


def test_verdict_model_upgrades_a_legacy_ai_decision():
    """Rows written before Phase 7 only carry the AI's own answer; the alert
    retry must still be able to replay them without re-spending on the model."""
    job = make_job()
    job.ai_decision = AIResponse(relevant=True, priority=Priority.P2, match_score=77,
                                 reasons=["decent overlap"]).model_dump(mode="json")

    verdict = job.verdict_model()
    assert verdict.match and verdict.match_score == 77
    assert verdict.priority is Priority.P2
    assert verdict.reason == "decent overlap"
    assert verdict.ai_status == "ok" and verdict.degraded is False
    # The stored number was the model's, so both halves honestly show it.
    assert verdict.deterministic_score == 77 and verdict.ai_score == 77

    replay = verdict.to_ai_response()
    assert replay.relevant and replay.match_score == 77 and replay.priority is Priority.P2


def test_unreadable_verdict_payloads_read_as_no_verdict():
    job = make_job(match_result={"match_score": "huge"})  # not coercible to int
    assert job.verdict_model() is None

    job = make_job(ai_decision={"relevant": True, "priority": "P9", "match_score": 50})
    assert job.verdict_model() is None  # a tier the model never issues


# --- alert rendering ----------------------------------------------------------


def test_alert_text_reports_both_halves_of_a_hybrid_score():
    job = make_job(match_result=stored(hybrid_result()))
    text = render(job, hybrid_result().to_ai_response())

    assert "P2 - Job Match (86%)" in text          # the blended number, not AI 90
    assert "Matched: Python, FastAPI" in text
    assert "Missing: Docker" in text
    assert "Evidence 74% + AI 90%" in text


def test_alert_text_says_so_when_the_ai_was_unavailable():
    degraded = hybrid_result(match_score=61, ai_status="failed", ai_score=None,
                             ai_error="timeout")
    job = make_job(match_result=stored(degraded))
    text = render(job, degraded.to_ai_response())

    assert "P2 - Job Match (61%)" in text
    assert "AI unavailable" in text
    assert "Evidence" not in text  # only one half produced a score, so say so


def test_alert_text_without_a_verdict_keeps_the_ai_reasons():
    """The Phase 6 contract still works for callers that never ran matching."""
    job = make_job()
    decision = AIResponse(relevant=True, priority=Priority.P1, match_score=91,
                          reasons=["Python + FastAPI overlap", "Pune location"])
    text = render(job, decision)

    assert "P1 - Job Match (91%)" in text
    assert "- Python + FastAPI overlap" in text
    assert "- Pune location" in text
    assert "Evidence" not in text


def test_alert_text_with_nothing_to_say_still_renders():
    job = make_job()
    text = render(job, AIResponse(relevant=True, priority=Priority.P3, match_score=60))
    assert "- Matched your profile preferences" in text


def test_notifier_send_contract_is_unchanged():
    """Phase 7 changed what the alert says, not how the pipeline calls it."""
    notifier = RecordingNotifier()
    job = make_job(match_result=stored(hybrid_result()))

    assert asyncio.run(notifier.send(job, hybrid_result().to_ai_response())) is True
    assert notifier.sent == ["Python Developer"]


# --- dashboard payload --------------------------------------------------------


def test_dashboard_fields_survive_the_api_response_model():
    """Whatever the state layer writes must be declared on JobResponse: pydantic
    silently drops undeclared keys, so the UI would lose the evidence behind the
    score without any error anywhere."""
    from hirevia.web.api.models import JobResponse
    from hirevia.web.state import _match_fields

    payload = _match_fields(make_job(match_result=stored(hybrid_result())))
    assert set(payload) <= set(JobResponse.model_fields), set(payload) - set(JobResponse.model_fields)

    response = JobResponse(
        id="1", source="telegram", source_job_id="1", title="Python Developer",
        company="Acme Corp", location="Pune", description="d", url="https://t.me/x/1",
        detected_at="2026-09-29T00:00:00", application_status="open", **payload,
    )
    assert response.match_score == 86 and response.match_priority == "P2"
    assert response.deterministic_score == 74 and response.ai_score == 90
    assert response.matched_skills == ["Python", "FastAPI"]
    assert response.missing_skills == ["Docker"]
    assert response.hard_reject is False and response.degraded is False


def test_dashboard_marks_a_degraded_verdict_as_degraded():
    """A score produced without the model must not render as a normal match."""
    from hirevia.web.state import _match_fields

    degraded = _match_fields(make_job(match_result=stored(
        hybrid_result(ai_status="failed", ai_score=None, ai_error="timeout")
    )))
    assert degraded["degraded"] is True and degraded["ai_score"] is None
    assert degraded["ai_error"] == "timeout"

    vetoed = _match_fields(make_job(match_result=stored(
        hybrid_result(hard_reject=True, reject_reasons=["asks 5+ years"])
    )))
    assert vetoed["hard_reject"] is True

    never_matched = _match_fields(make_job())
    assert never_matched["match_score"] is None
    assert never_matched["matched_skills"] == [] and never_matched["match_reason"] == ""
