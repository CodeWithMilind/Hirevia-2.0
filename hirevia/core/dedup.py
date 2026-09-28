"""Deduplication logic for jobs"""

import re
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse

from hirevia.core.models import Job


def normalize_url(url: str) -> str:
    """Normalize URL for comparison."""
    parsed = urlparse(url.lower())
    # Remove www, trailing slashes, query params, fragments
    netloc = parsed.netloc.replace("www.", "")
    path = parsed.path.rstrip("/")
    return f"{netloc}{path}"


def normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    text = text.lower().strip()
    # Remove common suffixes/prefixes
    text = re.sub(r"\b(sr|junior|jr|lead|principal|staff)\b\.?", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compute_fingerprint(job: Job) -> str:
    """Compute deterministic fingerprint for deduplication."""
    parts = []

    # 1. Source + source_job_id (most reliable)
    if job.source_job_id:
        parts.append(f"sid:{job.source}:{job.source_job_id}")

    # 2. Canonical application URL
    if job.application_url:
        parts.append(f"app:{normalize_url(str(job.application_url))}")

    # 3. Company + normalized title + normalized location
    company_norm = normalize_text(job.company)
    title_norm = normalize_text(job.title)
    location_norm = normalize_text(job.location)
    parts.append(f"ctl:{company_norm}|{title_norm}|{location_norm}")

    # 4. Company + URL domain
    if job.url:
        domain = urlparse(str(job.url)).netloc.replace("www.", "")
        parts.append(f"cd:{company_norm}|{domain}")

    # Join with separator, hash for consistent length
    import hashlib
    fingerprint_str = "||".join(sorted(set(parts)))
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:32]


def is_duplicate(job: Job, existing_job: Job, threshold: float = 0.85) -> bool:
    """Check if two jobs are duplicates using multiple signals."""
    # Signal 1: Exact source_job_id match
    if job.source_job_id and existing_job.source_job_id:
        if job.source == existing_job.source and job.source_job_id == existing_job.source_job_id:
            return True

    # Signal 2: Application URL match
    if job.application_url and existing_job.application_url:
        if normalize_url(str(job.application_url)) == normalize_url(str(existing_job.application_url)):
            return True

    # Signal 3: Fingerprint match
    if job.fingerprint and existing_job.fingerprint:
        if job.fingerprint == existing_job.fingerprint:
            return True

    # Signal 4: Company + title + location similarity
    company_sim = SequenceMatcher(None, normalize_text(job.company), normalize_text(existing_job.company)).ratio()
    title_sim = SequenceMatcher(None, normalize_text(job.title), normalize_text(existing_job.title)).ratio()
    location_sim = SequenceMatcher(None, normalize_text(job.location), normalize_text(existing_job.location)).ratio()

    if company_sim > 0.9 and title_sim > threshold and location_sim > 0.8:
        return True

    # Signal 5: Description similarity (for same company)
    if company_sim > 0.95:
        desc_sim = SequenceMatcher(None, job.description[:500], existing_job.description[:500]).ratio()
        if desc_sim > threshold:
            return True

    return False


def find_duplicate(job: Job, existing_jobs: list[Job]) -> Optional[Job]:
    """Find duplicate job in existing list."""
    for existing in existing_jobs:
        if is_duplicate(job, existing):
            return existing
    return None