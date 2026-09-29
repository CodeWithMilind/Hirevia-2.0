"""Main pipeline orchestration.

Stage order (each stage logs one line so a scan is auditable end to end):

    1. expire stale jobs
    2. collect from enabled sources    (per-source failures are isolated)
    3. deduplicate                      (fingerprint, then source id)
    4. deterministic filters            (filters.yaml)
    5. AI relevance / priority / score  (never fatal - see AIMatcher)
    6. application link verification, storage, notification

Failure policy: one bad job never stops a scan and one dead source never stops
the others. Job-level failures land in ScanStats.jobs_failed / errors.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from hirevia.ai.matcher import AIMatcher
from hirevia.config import Config
from hirevia.core.dedup import compute_fingerprint
from hirevia.core.filtering import apply_filters
from hirevia.core.freshness import freshness_label, sort_by_freshness
from hirevia.core.models import AIResponse, ApplicationStatus, Job, Priority, ScanStats
from hirevia.core.verification import classify_application_url, verify_application
from hirevia.notifications.base import Notifier
from hirevia.profile.profile import load_profile
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


class Pipeline:
    def __init__(
        self,
        config: Config,
        repo: JobRepository,
        ai_matcher: Optional[AIMatcher] = None,
        notifier: Optional[Notifier] = None,
    ):
        self.config = config
        self.repo = repo
        self.ai_matcher = ai_matcher
        self.notifier = notifier
        self.registry = SourceRegistry(config)
        self.profile = load_profile()
        self.stats = ScanStats()

    # -- scan loop ----------------------------------------------------------

    async def run_scan(self) -> ScanStats:
        """Run one complete scan cycle. Never raises: returns stats + errors."""
        start_time = datetime.utcnow()
        self.stats = ScanStats()

        try:
            self.stats.jobs_expired = self.repo.expire_jobs()
            logger.info("%s Stage 1: expired %d stale job(s)", PREFIX, self.stats.jobs_expired)

            sources = self.registry.get_enabled_sources()
            self.stats.sources_scanned = len(sources)
            logger.info(
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
            for index, job in enumerate(all_jobs, start=1):
                try:
                    await self._process_job(job, index, total)
                except Exception as e:  # noqa: BLE001 - a job must not kill the scan
                    self.stats.jobs_failed += 1
                    self._record_error(f"{job.source}:{job.source_job_id} {type(e).__name__}: {e}")
                    logger.error(
                        "%s [%d/%d] job %r (%s) failed: %s",
                        PREFIX, index, total, job.title, job.source, e, exc_info=True,
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
            f"filtered={s.jobs_filtered} ai_ok={s.jobs_ai_matched} ai_no={s.jobs_ai_rejected} "
            f"ai_errors={s.ai_failures} verified={s.jobs_verified} unknown={s.jobs_unverified} "
            f"closed={s.jobs_closed} no_apply_link={s.jobs_link_rejected} "
            f"notified={s.jobs_notified} alerts_skipped={s.jobs_skipped_notification} "
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
        self.stats.jobs_detected += int(counters.get("jobs_detected", 0) or 0)
        self.stats.jobs_normalized += int(counters.get("jobs_normalized", 0) or 0)
        logger.info("%s Source %s collected %d job(s)", PREFIX, source.name, len(jobs))
        return jobs

    # -- one job, stage by stage -------------------------------------------

    async def _process_job(self, job: Job, index: int = 1, total: int = 1) -> None:
        tag = f"[{index}/{total}]"

        # Stage 3: dedup. Fingerprint catches the same posting reached through
        # another source; source id catches a Telegram repost under a new title.
        job.fingerprint = compute_fingerprint(job)
        existing = self.repo.get_by_fingerprint(job.fingerprint) or self.repo.get_by_source_id(
            job.source, job.source_job_id
        )
        if existing is not None:
            self._merge_repost(existing, job)
            self.stats.jobs_duplicate += 1
            logger.debug("%s Stage 3: duplicate of %s - %r", PREFIX, existing.id, job.title)
            # A repost proves the opening is still live: if an earlier alert
            # never got out (Telegram was down), this is our retry.
            await self._retry_failed_alert(existing)
            return

        # Stage 4: deterministic filters (filters.yaml)
        filters = apply_filters(job, self.config.filters)
        if not filters.passed:
            self.stats.jobs_filtered += 1
            logger.info("%s %s Stage 4: filtered %r (%s)", PREFIX, tag, job.title, filters.reason)
            return

        # Stage 5a: cheap link gate BEFORE paying for an AI call. A post whose
        # only link is a channel/shortener/aggregator can never be applied to.
        quality = classify_application_url(str(job.application_url) if job.application_url else None)
        self._stamp(job, quality)
        if not quality.usable:
            self.stats.jobs_link_rejected += 1
            job.application_status = ApplicationStatus.UNKNOWN
            if self.repo.upsert_job(job):
                self.stats.jobs_new += 1
            logger.info(
                "%s Stage 5: %r has no usable apply link (%s) - stored, never alerted",
                PREFIX, job.title, quality.reason,
            )
            return

        # Stage 5b: AI relevance
        decision = await self._match(job, tag)
        if decision is not None and not decision.relevant:
            self.stats.jobs_ai_rejected += 1
            logger.info(
                "%s Stage 5: AI rejected %r (%d%%) - %s",
                PREFIX, job.title, decision.match_score, "; ".join(decision.reasons[:2]) or "no reason",
            )
            return
        if decision is not None:
            self.stats.jobs_ai_matched += 1

        # Stage 6: light-touch availability check on the application page
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

        blocked = self._alert_block(job, decision, quality, is_new)
        if blocked:
            self.stats.jobs_skipped_notification += 1
            logger.info("%s %s Stage 7: no alert for %r - %s", PREFIX, tag, job.title, blocked)
            return

        await self._notify(job, decision)

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
        failed - so this reuses the saved AI decision (no new AI spend) instead
        of waiting for a scan that never re-sees the message.
        """
        if self.notifier is None or existing.notified_at or not existing.ai_decision:
            return
        try:
            decision = AIResponse(**existing.ai_decision)
        except Exception as e:  # noqa: BLE001 - a stale payload shape is not fatal
            logger.debug("%s duplicate %s has an unreadable AI decision (%s)", PREFIX, existing.id, e)
            return
        quality = classify_application_url(
            str(existing.application_url) if existing.application_url else None
        )
        blocked = self._alert_block(existing, decision, quality, is_new=False, allow_known=True)
        if blocked:
            return
        logger.info("%s Stage 3: retrying the alert that never landed for %r", PREFIX, existing.title)
        await self._notify(existing, decision)



    # -- stage helpers ------------------------------------------------------

    async def _match(self, job: Job, tag: str) -> Optional[AIResponse]:
        """Stage 5: ask the model. Returns None when AI is switched off."""
        if self.ai_matcher is None:
            logger.debug("%s Stage 5: AI disabled - unranked pass-through", PREFIX)
            return None

        decision = await self.ai_matcher.match(job, self.profile)  # never raises
        job.ai_decision = decision.model_dump()
        if decision.degraded:
            self.stats.ai_failures += 1
            self._record_error(f"AI degraded ({job.source}/{job.source_job_id}): {decision.error}")
            logger.warning(
                "%s Stage 5: AI unavailable for %r (%s) - kept for manual review, no alert",
                PREFIX, job.title, decision.error,
            )
        else:
            logger.info(
                "%s Stage 5: AI matched %r - %s, %d%% (%s)",
                PREFIX, job.title, decision.priority.value, decision.match_score,
                "; ".join(decision.reasons[:2]) or "no reason",
            )
        return decision

    async def _verify(self, job: Job, tag: str):
        """Stage 6: reachability of the application page (best effort)."""
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
            return ApplicationStatus.UNKNOWN, classify_application_url(
                str(job.application_url) if job.application_url else None
            )

    def _alert_block(
        self,
        job: Job,
        decision: Optional[AIResponse],
        quality,
        is_new: bool,
        allow_known: bool = False,
    ) -> Optional[str]:
        """Return why this job must NOT interrupt the user (None = alert it).

        `allow_known` is used by the alert-retry path: the job is already in the
        database, we are re-sending something that never got delivered.
        """
        if self.notifier is None:
            return "notifications disabled"
        if not is_new and not allow_known:
            return "already known before this scan"
        if job.notified_at:
            return f"already notified via {job.notification_channel}"
        if decision is None:
            return "no AI ranking (run with AI enabled to receive alerts)"
        if decision.degraded:
            return f"AI unavailable ({decision.error})"
        if (
            decision.priority not in ALERT_PRIORITIES
            and decision.match_score < self.config.notify_min_match_score
        ):
            return (
                f"{decision.priority.value}/{decision.match_score}% below alert threshold "
                f"(P3 needs >= {self.config.notify_min_match_score}%)"
            )
        if quality.quality not in ALERTABLE_QUALITIES:
            return f"application link is not directly usable ({quality.reason})"
        max_age = self.config.notify_max_age_hours
        reference = job.posted_at or job.detected_at
        if max_age and reference and (datetime.utcnow() - reference) > timedelta(hours=max_age):
            return f"posted over {max_age}h ago (freshness: {job.raw_data.get('freshness_label')})"
        return None

    async def _notify(self, job: Job, decision: AIResponse) -> None:
        """Stage 7: deliver one alert.

        The notifier owns transport-level retries. If it still fails we leave
        notified_at empty, so the next scan - the real retry, one scan interval
        later - picks the job up again instead of blocking this one.
        """
        try:
            sent = await self.notifier.send(job, decision)
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
            "%s Stage 7: alerted %r @ %s (%s, %d%%) via %s",
            PREFIX, job.title, job.company, decision.priority.value, decision.match_score,
            self.notifier.name,
        )


async def run_single_scan(
    config: Config,
    repo: JobRepository,
    ai_matcher: Optional[AIMatcher] = None,
    notifier: Optional[Notifier] = None,
) -> ScanStats:
    """Run a single scan - convenience function."""
    pipeline = Pipeline(config, repo, ai_matcher, notifier)
    return await pipeline.run_scan()



