"""Main pipeline orchestration"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from hirevia.config import Config, load_config
from hirevia.core.models import Job, ScanStats, AIResponse, Priority, ApplicationStatus
from hirevia.core.dedup import compute_fingerprint, find_duplicate
from hirevia.core.freshness import sort_by_freshness, calculate_expiry
from hirevia.core.filtering import apply_filters
from hirevia.storage.state import JobRepository
from hirevia.sources.registry import SourceRegistry
from hirevia.ai.matcher import AIMatcher
from hirevia.profile.profile import load_profile

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: Config, repo: JobRepository, ai_matcher: Optional[AIMatcher] = None):
        self.config = config
        self.repo = repo
        self.ai_matcher = ai_matcher
        self.registry = SourceRegistry(config)
        self.profile = load_profile()
        self.stats = ScanStats()

    async def run_scan(self) -> ScanStats:
        """Run a complete scan cycle."""
        start_time = datetime.utcnow()
        self.stats = ScanStats()

        try:
            # Expire old jobs
            expired_count = self.repo.expire_jobs()
            self.stats.jobs_expired = expired_count
            logger.info(f"Expired {expired_count} jobs")

            # Collect from all enabled sources
            sources = self.registry.get_enabled_sources()
            self.stats.sources_scanned = len(sources)

            all_jobs: List[Job] = []
            for source in sources:
                try:
                    jobs = await source.collect()
                    self.stats.jobs_found += len(jobs)
                    all_jobs.extend(jobs)
                    logger.info(f"Source {source.name}: collected {len(jobs)} jobs")
                except Exception as e:
                    logger.error(f"Source {source.name} failed: {e}")
                    self.stats.errors.append(f"{source.name}: {e}")

            # Sort by freshness (newest first)
            all_jobs = sort_by_freshness(all_jobs)

            # Process each job
            for job in all_jobs:
                await self._process_job(job)

            self.stats.duration_seconds = (datetime.utcnow() - start_time).total_seconds()
            logger.info(f"Scan complete: {self.stats}")

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self.stats.errors.append(f"Pipeline: {e}")

        return self.stats

    async def _process_job(self, job: Job):
        """Process a single job through the pipeline."""
        # Compute fingerprint
        job.fingerprint = compute_fingerprint(job)

        # Check deduplication against database
        existing = self.repo.get_by_fingerprint(job.fingerprint)
        if existing:
            # Update last_seen on existing
            existing.last_seen = datetime.utcnow()
            self.repo.upsert_job(existing)
            self.stats.jobs_duplicate += 1
            logger.debug(f"Duplicate: {job.title} at {job.company}")
            return

        # Also check by source_job_id
        existing = self.repo.get_by_source_id(job.source, job.source_job_id)
        if existing:
            existing.last_seen = datetime.utcnow()
            self.repo.upsert_job(existing)
            self.stats.jobs_duplicate += 1
            logger.debug(f"Duplicate (source_id): {job.title} at {job.company}")
            return

        # Apply deterministic filters
        filter_result = apply_filters(job, self.config.filters)
        if not filter_result.passed:
            self.stats.jobs_filtered += 1
            logger.debug(f"Filtered: {job.title} - {filter_result.reason}")
            return

        # AI matching
        if self.ai_matcher:
            try:
                ai_response = await self.ai_matcher.match(job, self.profile)
                job.ai_decision = ai_response.model_dump()

                if not ai_response.relevant:
                    logger.debug(f"AI rejected: {job.title} - {ai_response.reasons}")
                    return

                self.stats.jobs_ai_matched += 1
                logger.info(f"AI matched: {job.title} (priority: {ai_response.priority}, score: {ai_response.match_score})")
            except Exception as e:
                logger.error(f"AI matching failed for {job.title}: {e}")
                # Fail-open: continue without AI decision
                job.ai_decision = {"relevant": True, "priority": "P3", "match_score": 50, "reasons": ["AI unavailable"]}

        # Verify application URL
        verified = await self._verify_application(job)
        if not verified:
            job.application_status = ApplicationStatus.CLOSED
            logger.debug(f"Application closed: {job.title}")
            # Still store for dedup
            is_new = self.repo.upsert_job(job)
            if is_new:
                self.stats.jobs_new += 1
            return

        job.application_status = ApplicationStatus.OPEN
        self.stats.jobs_verified += 1

        # Store job
        is_new = self.repo.upsert_job(job)
        if is_new:
            self.stats.jobs_new += 1

        # Notify (will be implemented in notifications phase)
        # await self._notify(job, ai_response)

        self.stats.jobs_notified += 1
        logger.info(f"Job ready for notification: {job.title}")

    async def _verify_application(self, job: Job) -> bool:
        """Verify application URL is accessible."""
        # For now, basic URL validation
        if not job.application_url:
            return True  # Can't verify, assume open

        # Could add HTTP HEAD request here
        # For now, return True
        return True


async def run_single_scan(config: Config, repo: JobRepository, ai_matcher: Optional[AIMatcher] = None) -> ScanStats:
    """Run a single scan - convenience function."""
    pipeline = Pipeline(config, repo, ai_matcher)
    return await pipeline.run_scan()