"""Main pipeline orchestration.

Stage order (each stage logs one line so a scan is auditable end to end):

    1. expire stale jobs
    2. collect from enabled sources    (per-source failures are isolated)
    3. deduplicate                      (fingerprint, then source id)
    4. deterministic filters            (filters.yaml - hard vetoes)
    5. matching                         (5a cheap link gate, 5b hybrid match:
                                         deterministic profile evidence +
                                         AI judgement -> one MatchResult)
    6. application link verification, storage, notification

Failure policy: one bad job never stops a scan and one dead source never stops
the others. Job-level failures land in ScanStats.jobs_failed / errors. An AI
that is switched off or completely down costs confidence, never a scan: the
deterministic half still scores every job (see hirevia.core.matching).
"""

import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from hirevia.ai.matcher import AIMatcher
from hirevia.config import Config
from hirevia.core.dedup import compute_fingerprint
from hirevia.core.filtering import apply_filters
from hirevia.core.freshness import freshness_label, sort_by_freshness
from hirevia.core.matching import analyze, combine
from hirevia.core.models import AIResponse, ApplicationStatus, Job, MatchResult, Priority, ScanStats
from hirevia.core.verification import UrlQuality, classify_application_url, verify_application
from hirevia.notifications.base import Notifier
from hirevia.profile.profile import load_profile, validate_profile
from hirevia.sources.registry import SourceRegistry
from hirevia.storage.state import JobRepository

logger = logging.getLogger(__name__)
PREFIX = "[Pipeline]"

# Priorities that always earn an alert; P3 needs a strong score instead.
ALERT_PRIORITIES = {Priority.P1, Priority.P2}

# Link qualities worth interrupting the user for. A job whose only link is a
# Telegram/WhatsApp channel, a shortener or an aggregator is stored for history
# but never alerted on: the candidate cannot apply through it.
ALERTABLE_QUALITIES = {"ats", "company", "job_board", "generic"}


def _accepts_evidence(matcher) -> bool:
    """Does this matcher accept the deterministic evidence as a third argument?

    Phase 6 matchers (and test doubles) only take (job, profile). Probing the
    signature once keeps those working instead of raising TypeError mid-scan.
    """
    if matcher is None:
        return False
    try:
        params = inspect.signature(matcher.match).parameters
    except (TypeError, ValueError):  # builtins / exotic callables
        return False
    if any(p.kind is p.VAR_POSITIONAL or p.kind is p.VAR_KEYWORD for p in params.values()):
        return True
    positional = [p for p in params.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 3


class Pipeline:
    def __init__(
        self,
        config: Config,
        repo: JobRepository,
        ai_matcher: Optional[AIMatcher] = None,
        notifier: Optional[Notifier] = None,
        diagnostic_output: bool = False,
        ai_progress_callback: Optional[Callable[..., None]] = None,
    ):
        self.config = config
        self.repo = repo
        self.ai_matcher = ai_matcher
        self.notifier = notifier
        self.diagnostic_output = diagnostic_output
        self.ai_progress_callback = ai_progress_callback
        self.registry = SourceRegistry(config)
        self.profile = load_profile(self.config.profile_path, self.config.filters)
        self.stats = ScanStats()
        self.stats.ai_provider = getattr(ai_matcher, "provider_name", "disabled") if ai_matcher else "disabled"
        self._matcher_takes_evidence = _accepts_evidence(ai_matcher)
        for warning in validate_profile(self.profile):
            logger.warning("%s profile: %s", PREFIX, warning)
        logger.debug(
            "%s profile '%s': %d skill(s), %d role(s), %d yr experience, remote=%s%s",
            PREFIX, self.profile.name, len(self.profile.skills), len(self.profile.preferred_roles),
            self.profile.experience_years, self.profile.remote_preference,
            "" if self.ai_matcher else " | AI disabled - deterministic scoring only",
        )

    # -- scan loop ----------------------------------------------------------

    async def run_scan(self) -> ScanStats:
        """Run one complete scan cycle. Never raises: returns stats + errors."""
        start_time = datetime.utcnow()
        self.stats = ScanStats(
            ai_provider=getattr(self.ai_matcher, "provider_name", "disabled")
            if self.ai_matcher else "disabled"
        )

        try:
            self.stats.jobs_expired = self.repo.expire_jobs()
            logger.debug("%s Stage 1: expired %d stale job(s)", PREFIX, self.stats.jobs_expired)

            sources = self.registry.get_enabled_sources()
            if self.diagnostic_output:
                for source in sources:
                    if source.name == "telegram":
                        source.diagnostic_output = True
            self.stats.sources_scanned = len(sources)
            logger.debug(
                "%s Stage 2: scanning %d enabled source(s): %s",
                PREFIX, len(sources), ", ".join(s.name for s in sources) or "none",
            )

            all_jobs: List[Job] = []
            for source in sources:
                all_jobs.extend(await self._collect(source))

            all_jobs = sort_by_freshness(all_jobs)
            logger.info(
                "%s Stage 2 done: %d message(s), %d detected, %d normalized, %d to process",
                PREFIX, self.stats.messages_checked, self.stats.jobs_detected,
                self.stats.jobs_normalized, len(all_jobs),
            )

            total = len(all_jobs)
            if self.ai_progress_callback:
                self._emit_ai_progress("start", total=total)
            for index, job in enumerate(all_jobs, start=1):
                self._job_progress_state = {"status": "AI SKIPPED", "score": None}
                if self.ai_progress_callback:
                    self._emit_ai_progress(
                        "processing", index=index, total=total, job=job
                    )
                try:
                    await self._process_job(job, index, total)
                except Exception as e:  # noqa: BLE001 - a job must not kill the scan
                    self.stats.jobs_failed += 1
                    self._record_error(f"{job.source}:{job.source_job_id} {type(e).__name__}: {e}")
                    logger.error(
                        "%s [%d/%d] job %r (%s) failed: %s",
                        PREFIX, index, total, job.title, job.source, e, exc_info=True,
                    )
                finally:
                    if self.ai_progress_callback:
                        self._emit_ai_progress(
                            "job_complete",
                            index=index,
                            total=total,
                            job=job,
                            **self._job_progress_state,
                        )
        except Exception as e:  # noqa: BLE001
            logger.error("%s scan failed: %s", PREFIX, e, exc_info=True)
            self._record_error(f"Pipeline: {type(e).__name__}: {e}")

        self.stats.duration_seconds = (datetime.utcnow() - start_time).total_seconds()
        logger.info("%s Scan finished in %.1fs: %s", PREFIX, self.stats.duration_seconds, self._summary_line())
        return self.stats

    def _summary_line(self) -> str:
        s = self.stats
        return (
            f"found={s.jobs_found} new={s.jobs_new} dup={s.jobs_duplicate} "
            f"promo_rejected={s.promotional_rejected} education_rejected={s.educational_rejected} "
            f"info_rejected={s.informational_rejected} spam_rejected={s.spam_rejected} "
            f"selection_rejected={s.selection_rejected} aggregator_rejected={s.aggregator_rejected} "
            f"ambiguous={s.intent_ambiguous} "
            f"filtered={s.jobs_filtered} hard_passed={s.jobs_hard_filter_passed} "
            f"hard_unknown={s.jobs_hard_filter_unknown} rule_scored={s.jobs_rule_scored} "
            f"rule_strong={s.jobs_rule_strong} ai_skipped={s.jobs_ai_skipped} "
            f"matched={s.jobs_matched} below_match={s.jobs_below_match} "
            f"matching_attempted={s.jobs_matching_attempted} "
            f"ai_ok={s.jobs_ai_matched} ai_no={s.jobs_ai_rejected} "
            f"ai_errors={s.ai_failures} verified={s.jobs_verified} "
            f"verification_attempted={s.jobs_verification_attempted} unknown={s.jobs_unverified} "
            f"closed={s.jobs_closed} no_apply_link={s.jobs_link_rejected} "
            f"notify_eligible={s.jobs_notification_eligible} notified={s.jobs_notified} "
            f"alerts_skipped={s.jobs_skipped_notification} "
            f"failed={s.jobs_failed}"
        )

    def _record_error(self, message: str) -> None:
        """Keep the error list useful without letting it grow unbounded."""
        if len(self.stats.errors) < self.config.max_reported_errors:
            self.stats.errors.append(str(message)[:300])

    async def _collect(self, source) -> List[Job]:
        """Collect from one source; a failure here costs only that source."""
        binder = getattr(source, "bind_state", None)
        if callable(binder):
            try:
                binder(self.repo)  # lets collectors persist their cursors
            except Exception as e:  # noqa: BLE001
                logger.debug("%s %s: state store not bound (%s)", PREFIX, source.name, e)

        try:
            jobs = await source.collect()
        except Exception as e:  # noqa: BLE001
            logger.error("%s Source %s failed: %s", PREFIX, source.name, e)
            self._record_error(f"{source.name}: {type(e).__name__}: {e}")
            return []

        self.stats.jobs_found += len(jobs)
        counters = getattr(source, "last_stats", None) or {}
        self.stats.messages_checked += int(counters.get("messages_checked", 0) or 0)
        for counter in (
            "promotional_rejected", "educational_rejected", "informational_rejected",
            "spam_rejected", "selection_rejected", "aggregator_rejected", "intent_ambiguous",
        ):
            setattr(self.stats, counter, getattr(self.stats, counter) + int(counters.get(counter, 0) or 0))
        self.stats.jobs_detected += int(counters.get("jobs_detected", 0) or 0)
        self.stats.jobs_normalized += int(counters.get("jobs_normalized", 0) or 0)
        self.stats.telegram_channels_scanned += int(counters.get("channels_scanned", 0) or 0)
        logger.debug("%s Source %s collected %d job(s)", PREFIX, source.name, len(jobs))
        return jobs

    # -- one job, stage by stage -------------------------------------------

    async def _process_job(self, job: Job, index: int = 1, total: int = 1) -> None:
        tag = f"[{index}/{total}]"

        # Stage 3: dedup. Fingerprint catches the same posting reached through
        # another source; source id catches a Telegram repost under a new title.
        job.fingerprint = compute_fingerprint(job)
        existing = (
            self.repo.get_by_fingerprint(job.fingerprint)
            or self.repo.get_by_source_id(job.source, job.source_job_id)
            or (
                self.repo.get_by_application_url(str(job.application_url))
                if job.application_url else None
            )
        )
        if existing is not None:
            self._merge_repost(existing, job)
            self.stats.jobs_duplicate += 1
            existing_verdict = existing.verdict_model()
            if existing_verdict is not None:
                self._job_progress_state = {
                    "status": "AI SKIPPED",
                    "score": existing_verdict.match_score,
                }
            logger.debug("%s Stage 3: duplicate of %s - %r", PREFIX, existing.id, job.title)
            # A repost proves the opening is still live: if an earlier alert
            # never got out (Telegram was down), this is our retry.
            await self._retry_failed_alert(existing)
            return

        # Stage 4: deterministic filters (filters.yaml)
        filters = apply_filters(job, self.config.filters, self.profile)
        if not filters.passed:
            self.stats.jobs_filtered += 1
            logger.debug(
                "%s [HARD FILTER] %s | %s | %s",
                PREFIX, filters.status, job.title, filters.reason,
            )
            return
        self.stats.jobs_hard_filter_passed += 1
        if filters.status == "UNKNOWN":
            self.stats.jobs_hard_filter_unknown += 1
        logger.debug(
            "%s [HARD FILTER] %s | %s | %s",
            PREFIX, filters.status, job.title, filters.reason,
        )

        evidence = analyze(job, self.profile)
        self._job_progress_state = {"status": "AI SKIPPED", "score": evidence.score}
        self.stats.jobs_rule_scored += 1
        self.stats.jobs_rule_strong += int(evidence.score >= self.config.match_min_score)
        if evidence.hard_reject:
            self.stats.jobs_filtered += 1
            logger.debug(
                "%s [HARD FILTER] REJECT | %s | %s",
                PREFIX, job.title, evidence.reject_reasons[0],
            )
            return

        # Stage 5a: cheap link gate BEFORE paying for an AI call. A post whose
        # only link is a channel/shortener/aggregator can never be applied to.
        quality = self._application_quality(job)
        self._stamp(job, quality)
        if not quality.usable:
            self.stats.jobs_link_rejected += 1
            job.application_status = ApplicationStatus.UNKNOWN
            if quality.quality == "none" and job.application_url:
                job.raw_data["invalid_application_url"] = str(job.application_url)[:500]
                job.application_url = None
            if self.repo.upsert_job(job):
                self.stats.jobs_new += 1
            logger.debug(
                "%s Stage 5: %r has no usable apply link (%s) - stored, never alerted",
                PREFIX, job.title, quality.reason,
            )
            return

        # Stage 5b: hybrid matching. Deterministic profile evidence first
        # (free, auditable), then the model's semantic judgement, then one
        # verdict that storage, notifications and the dashboard all read.
        if self.ai_matcher is not None and not _should_call_ai(evidence, self.config):
            self.stats.jobs_ai_skipped += 1
            verdict = combine(evidence, self.config)
            self._job_progress_state = {"status": "AI SKIPPED", "score": evidence.score}
            job.match_result = verdict.model_dump(mode="json")
            job.raw_data["deterministic_score"] = verdict.deterministic_score
            job.raw_data["ai_status"] = verdict.ai_status
            if self.ai_progress_callback:
                self._emit_ai_progress(
                    "skipped",
                    job=job,
                    rule_score=evidence.score,
                )
            if not verdict.match:
                self.stats.jobs_below_match += 1
                return
        else:
            self.stats.jobs_matching_attempted += 1
            if self.ai_matcher is not None and self.ai_progress_callback:
                self._emit_ai_progress("sending_ai", job=job)
            verdict = await self._match(job, tag, evidence)
            if self.ai_progress_callback:
                if self.ai_matcher is None:
                    self._job_progress_state = {"status": "AI SKIPPED", "score": evidence.score}
                    self._emit_ai_progress(
                        "skipped",
                        job=job,
                        rule_score=evidence.score,
                    )
                else:
                    is_ai_error = verdict.ai_status == "failed"
                    self._job_progress_state = {
                        "status": "AI ERROR" if is_ai_error else (
                            "MATCHED" if verdict.match else "NOT MATCHED"
                        ),
                        "score": verdict.match_score,
                    }
                    self._emit_ai_progress(
                        "ai_error" if is_ai_error else "ai_response",
                        job=job,
                        verdict=verdict,
                    )
        if not verdict.match:
            self.stats.jobs_below_match += 1
            logger.debug(
                "%s %s Stage 5: no match for %r (%d%%, deterministic %d%%) - %s",
                PREFIX, tag, job.title, verdict.match_score, verdict.deterministic_score,
                verdict.reason[:160] or "below the match threshold",
            )
            return
        self.stats.jobs_matched += 1

        # Stage 6: light-touch availability check on the application page
        self.stats.jobs_verification_attempted += 1
        status, quality = await self._verify(job, tag)
        self._stamp(job, quality)
        job.application_status = status
        if status is ApplicationStatus.OPEN:
            self.stats.jobs_verified += 1
        elif status is ApplicationStatus.CLOSED:
            self.stats.jobs_closed += 1
        else:
            self.stats.jobs_unverified += 1

        # Stage 7: store, then decide about alerting
        is_new = self.repo.upsert_job(job)
        if is_new:
            self.stats.jobs_new += 1

        blocked = self._alert_block(job, verdict, quality, is_new)
        if blocked:
            if self.notifier is None and blocked == "notifications disabled":
                if self._alert_block(job, verdict, quality, is_new, ignore_notification_disabled=True) is None:
                    self.stats.jobs_notification_eligible += 1
                    self._record_top_opportunity(job, verdict)
            self.stats.jobs_skipped_notification += 1
            logger.debug("%s %s Stage 7: no alert for %r - %s", PREFIX, tag, job.title, blocked)
            return

        self.stats.jobs_notification_eligible += 1
        self._record_top_opportunity(job, verdict)
        await self._notify(job, verdict)

    def _application_quality(self, job: Job):
        quality = classify_application_url(str(job.application_url) if job.application_url else None)
        if not quality.usable and job.raw_data.get("manual_application_action"):
            return UrlQuality(
                "generic", True, 65,
                "explicit walk-in schedule/location provides an application action",
            )
        return quality

    def _emit_ai_progress(self, event: str, **payload) -> None:
        try:
            self.ai_progress_callback(event, **payload)
        except Exception:
            logger.debug("%s AI progress callback failed", PREFIX, exc_info=True)

    def _record_top_opportunity(self, job: Job, verdict: MatchResult) -> None:
        item = {
            "title": job.title,
            "company": job.company,
            "match_score": verdict.match_score,
            "priority": verdict.priority.value,
            "location": job.location,
            "application_url": str(job.application_url) if job.application_url else str(job.url),
        }
        self.stats.top_opportunities.append(item)
        self.stats.top_opportunities.sort(key=lambda opportunity: opportunity["match_score"], reverse=True)
        del self.stats.top_opportunities[5:]

    def _stamp(self, job: Job, quality) -> None:
        """Persist the freshness + link-quality verdicts on the job itself."""
        job.raw_data["freshness_label"] = freshness_label(job.posted_at or job.detected_at)
        job.raw_data["application_url_quality"] = quality.quality
        job.raw_data["application_url_usable"] = quality.usable
        job.raw_data["application_url_confidence"] = quality.confidence
        job.raw_data["application_url_reason"] = quality.reason

    def _merge_repost(self, existing: Job, job: Job) -> None:
        """Keep ONE record per opening: accumulate source channels, refresh the
        link if the repost finally carries a better one, bump last_seen."""
        channels = [c for c in (existing.raw_data.get("source_channels") or []) if c]
        incoming = job.raw_data.get("source_channels") or [job.raw_data.get("source_channel")]
        merged = channels + [c for c in incoming if c and c not in channels]
        if merged:
            existing.raw_data["source_channels"] = merged
        if job.application_url and not existing.application_url:
            existing.application_url = str(job.application_url)
        existing.last_seen = datetime.utcnow()
        try:
            self.repo.upsert_job(existing)
        except Exception as e:  # noqa: BLE001 - duplicate bookkeeping is not fatal
            logger.warning("%s could not refresh duplicate %s: %s", PREFIX, existing.id, e)

    async def _retry_failed_alert(self, existing: Job) -> None:
        """Re-send an alert that never got out on a previous scan.

        A duplicate means the posting is live again, the job already has a
        stored verdict, and `notified_at` is still empty because the send
        failed - so this reuses the saved match (no new AI spend) instead of
        waiting for a scan that never re-sees the message.
        """
        if self.notifier is None or existing.notified_at:
            return
        verdict = existing.verdict_model()
        if verdict is None:
            return
        quality = self._application_quality(existing)
        blocked = self._alert_block(existing, verdict, quality, is_new=False, allow_known=True)
        if blocked:
            return
        logger.debug("%s Stage 3: retrying the alert that never landed for %r", PREFIX, existing.title)
        await self._notify(existing, verdict)



    # -- stage helpers ------------------------------------------------------

    async def _match(self, job: Job, tag: str, evidence=None) -> MatchResult:
        """Stage 5b: one hybrid verdict, always available.

        The deterministic half always runs: it is free, it is auditable, and it
        means every job still carries a score when the AI is switched off or the
        provider is down. The model then supplies the judgement a regex cannot
        make (seniority, role intent, transferability), grounded on the evidence
        block so it cannot invent skills.
        """
        evidence = evidence or analyze(job, self.profile)

        decision: Optional[AIResponse] = None
        if self.ai_matcher is None:
            logger.debug("%s Stage 5: AI disabled - scoring on profile evidence only", PREFIX)
        else:
            args = (job, self.profile, evidence) if self._matcher_takes_evidence else (job, self.profile)
            decision = await self.ai_matcher.match(*args)  # never raises
            self.stats.ai_provider = getattr(self.ai_matcher, "provider_name", self.stats.ai_provider)
            job.ai_decision = decision.model_dump()
            if decision.degraded:
                self.stats.ai_failures += 1
                self._record_error(f"AI degraded ({job.source}/{job.source_job_id}): {decision.error}")
                logger.debug(
                    "%s Stage 5: AI unavailable for %r (%s) - scored on evidence alone",
                    PREFIX, job.title, decision.error,
                )
            elif decision.relevant:
                self.stats.jobs_ai_matched += 1
            else:
                self.stats.jobs_ai_rejected += 1

        verdict = combine(evidence, self.config, decision, getattr(self.ai_matcher, "model", None))
        job.match_result = verdict.model_dump(mode="json")
        # Mirrors for the dashboard's existing flat readers.
        job.raw_data["deterministic_score"] = verdict.deterministic_score
        job.raw_data["ai_status"] = verdict.ai_status
        logger.debug(
            "%s Stage 5: %r -> %d%% (%s) = deterministic %d%% + ai %s | %s",
            PREFIX, job.title, verdict.match_score, verdict.priority.value,
            verdict.deterministic_score,
            "n/a" if verdict.ai_score is None else f"{verdict.ai_score}%",
            verdict.reason[:140] or "no reason given",
        )
        return verdict

    async def _verify(self, job: Job, tag: str):
        """Stage 6: reachability of the application page (best effort)."""
        if job.raw_data.get("manual_application_action"):
            return ApplicationStatus.UNKNOWN, self._application_quality(job)
        try:
            return await verify_application(
                job,
                check_live=self.config.verify_application_urls,
                timeout=self.config.verify_timeout_seconds,
            )
        except Exception as e:  # noqa: BLE001 - a dead link checker must not lose a job
            logger.warning(
                "%s Stage 6: link check failed for %r (%s) - status stays unknown",
                PREFIX, job.title, e,
            )
            return ApplicationStatus.UNKNOWN, self._application_quality(job)

    def _alert_block(
        self,
        job: Job,
        verdict: MatchResult,
        quality,
        is_new: bool,
        allow_known: bool = False,
        ignore_notification_disabled: bool = False,
    ) -> Optional[str]:
        """Return why this job must NOT interrupt the user (None = alert it).

        `allow_known` is used by the alert-retry path: the job is already in the
        database, we are re-sending something that never got delivered.
        """
        if job.raw_data.get("intent_category") in {"unknown", "ambiguous"}:
            return "job intent is ambiguous; no actionable opening confirmed"
        if self.notifier is None and not ignore_notification_disabled:
            return "notifications disabled"
        if not is_new and not allow_known:
            return "already known before this scan"
        if job.notified_at:
            return f"already notified via {job.notification_channel}"
        if verdict.hard_reject:
            return "canonical verdict contains a hard rejection"
        if not verdict.match:
            return "canonical verdict is not a match"
        if job.application_status in (ApplicationStatus.CLOSED, ApplicationStatus.EXPIRED):
            return f"application is {job.application_status.value}"
        if not quality.usable:
            return f"application link is unusable ({quality.reason})"
        if not job.title.strip():
            return "job title is missing"
        if verdict.ai_status == "skipped":
            return "no AI ranking (run with AI enabled to receive alerts)"
        if verdict.degraded:
            return f"AI unavailable ({verdict.ai_error})"
        if (
            verdict.priority not in ALERT_PRIORITIES
            and verdict.match_score < self.config.notify_min_match_score
        ):
            return (
                f"{verdict.priority.value}/{verdict.match_score}% below alert threshold "
                f"(P3 needs >= {self.config.notify_min_match_score}%)"
            )
        if quality.quality not in ALERTABLE_QUALITIES:
            return f"application link is not directly usable ({quality.reason})"
        max_age = self.config.notify_max_age_hours
        reference = job.posted_at or job.detected_at
        if max_age and reference:
            if reference.tzinfo is not None:
                reference = reference.astimezone(timezone.utc).replace(tzinfo=None)
            if (datetime.utcnow() - reference) > timedelta(hours=max_age):
                return f"posted over {max_age}h ago (freshness: {job.raw_data.get('freshness_label')})"
        return None

    async def _notify(self, job: Job, verdict: MatchResult) -> None:
        """Stage 7: deliver one alert.

        The notifier owns transport-level retries. If it still fails we leave
        notified_at empty, so the next scan - the real retry, one scan interval
        later - picks the job up again instead of blocking this one.
        """
        try:
            sent = await self.notifier.send(job, verdict)
        except Exception as e:  # noqa: BLE001
            logger.error("%s Stage 7: notification crashed for %r: %s", PREFIX, job.title, e)
            self._record_error(f"notify {job.source_job_id}: {type(e).__name__}: {e}")
            return

        if not sent:
            self._record_error(f"notification failed for {job.source_job_id} - retrying next scan")
            logger.warning("%s Stage 7: alert failed for %r - will retry next scan", PREFIX, job.title)
            return

        self.stats.jobs_notified += 1
        job.notified_at = datetime.utcnow()
        job.notification_channel = self.notifier.name
        try:
            self.repo.mark_notified(job.id, self.notifier.name)
        except Exception as e:  # noqa: BLE001 - alert is already out; do not fail the scan
            logger.warning("%s could not record notification for %s: %s", PREFIX, job.id, e)
        logger.info(
            "%s Stage 7: alerted %r @ %s (%s, %d%% / det %d%%) via %s",
            PREFIX, job.title, job.company, verdict.priority.value, verdict.match_score,
            verdict.deterministic_score, self.notifier.name,
        )


async def run_single_scan(
    config: Config,
    repo: JobRepository,
    ai_matcher: Optional[AIMatcher] = None,
    notifier: Optional[Notifier] = None,
    diagnostic_output: bool = False,
    ai_progress_callback: Optional[Callable[..., None]] = None,
) -> ScanStats:
    """Run a single scan - convenience function."""
    pipeline = Pipeline(
        config, repo, ai_matcher, notifier, diagnostic_output, ai_progress_callback
    )
    return await pipeline.run_scan()


def _should_call_ai(evidence, config: Config) -> bool:
    """Keep strong and uncertain candidates for semantic judgement; skip clear misses."""
    if evidence.hard_reject:
        return False
    if evidence.role_match is True:
        return True
    if evidence.score >= config.match_min_score:
        return True
    if evidence.role_match is None:
        return True
    return False



