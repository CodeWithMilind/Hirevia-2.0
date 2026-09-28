"""SQLite-based job state storage"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hirevia.core.models import Job, ApplicationStatus, ScanStats


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    url TEXT NOT NULL,
    application_url TEXT,
    posted_at TEXT,
    detected_at TEXT NOT NULL,
    deadline TEXT,
    employment_type TEXT,
    experience TEXT,
    skills TEXT NOT NULL DEFAULT '[]',
    raw_data TEXT NOT NULL DEFAULT '{}',
    application_status TEXT NOT NULL DEFAULT 'unknown',
    fingerprint TEXT,
    first_seen TEXT,
    last_seen TEXT,
    expires_at TEXT,
    notified_at TEXT,
    notification_channel TEXT,
    ai_decision TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_source_id ON jobs(source, source_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs(expires_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(application_status);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
"""

# Triggers for updated_at
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS update_jobs_timestamp
AFTER UPDATE ON jobs
BEGIN
    UPDATE jobs SET updated_at = datetime('now') WHERE id = NEW.id;
END;
"""


class JobRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)
            conn.executescript(TRIGGERS)
            conn.commit()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            source=row["source"],
            source_job_id=row["source_job_id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            description=row["description"],
            url=row["url"],
            application_url=row["application_url"],
            posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
            detected_at=datetime.fromisoformat(row["detected_at"]) if row["detected_at"] else datetime.utcnow(),
            deadline=datetime.fromisoformat(row["deadline"]) if row["deadline"] else None,
            employment_type=row["employment_type"],
            experience=row["experience"],
            skills=json.loads(row["skills"]) if row["skills"] else [],
            raw_data=json.loads(row["raw_data"]) if row["raw_data"] else {},
            application_status=ApplicationStatus(row["application_status"]),
            fingerprint=row["fingerprint"],
            first_seen=datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None,
            last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            notified_at=datetime.fromisoformat(row["notified_at"]) if row["notified_at"] else None,
            notification_channel=row["notification_channel"],
            ai_decision=json.loads(row["ai_decision"]) if row["ai_decision"] else None,
        )

    def _job_to_row(self, job: Job) -> Tuple:
        now = datetime.utcnow().isoformat()
        return (
            job.id,
            job.source,
            job.source_job_id,
            job.title,
            job.company,
            job.location,
            job.description,
            str(job.url),
            str(job.application_url) if job.application_url else None,
            job.posted_at.isoformat() if job.posted_at else None,
            job.detected_at.isoformat() if job.detected_at else now,
            job.deadline.isoformat() if job.deadline else None,
            job.employment_type.value if job.employment_type else None,
            job.experience,
            json.dumps(job.skills),
            json.dumps(job.raw_data),
            job.application_status.value,
            job.fingerprint,
            job.first_seen.isoformat() if job.first_seen else now,
            job.last_seen.isoformat() if job.last_seen else now,
            job.expires_at.isoformat() if job.expires_at else None,
            job.notified_at.isoformat() if job.notified_at else None,
            job.notification_channel,
            json.dumps(job.ai_decision) if job.ai_decision else None,
        )

    def upsert_job(self, job: Job) -> bool:
        """Insert or update job. Returns True if newly inserted."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT id, first_seen FROM jobs WHERE source = ? AND source_job_id = ?",
                (job.source, job.source_job_id),
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing
                job.id = existing["id"]
                job.first_seen = datetime.fromisoformat(existing["first_seen"])
                job.last_seen = datetime.utcnow()
                if job.expires_at is None:
                    job.expires_at = job.first_seen.replace(hour=23, minute=59, second=59)
                    # Add 24 hours from first_seen
                    from datetime import timedelta
                    job.expires_at = job.first_seen + timedelta(hours=24)

                conn.execute(
                    """
                    UPDATE jobs SET
                        title = ?, company = ?, location = ?, description = ?,
                        url = ?, application_url = ?, posted_at = ?, detected_at = ?,
                        deadline = ?, employment_type = ?, experience = ?,
                        skills = ?, raw_data = ?, application_status = ?,
                        fingerprint = ?, last_seen = ?, expires_at = ?,
                        notified_at = ?, notification_channel = ?, ai_decision = ?
                    WHERE id = ?
                    """,
                    self._job_to_row(job)[3:18] + self._job_to_row(job)[19:] + (job.id,),
                )
                conn.commit()
                return False
            else:
                # New job
                job.first_seen = datetime.utcnow()
                job.last_seen = job.first_seen
                from datetime import timedelta
                job.expires_at = job.first_seen + timedelta(hours=24)

                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, source, source_job_id, title, company, location, description,
                        url, application_url, posted_at, detected_at, deadline,
                        employment_type, experience, skills, raw_data,
                        application_status, fingerprint, first_seen, last_seen,
                        expires_at, notified_at, notification_channel, ai_decision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._job_to_row(job),
                )
                conn.commit()
                return True

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return self._row_to_job(row) if row else None

    def get_by_fingerprint(self, fingerprint: str) -> Optional[Job]:
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,))
            row = cursor.fetchone()
            return self._row_to_job(row) if row else None

    def get_by_source_id(self, source: str, source_job_id: str) -> Optional[Job]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                (source, source_job_id),
            )
            row = cursor.fetchone()
            return self._row_to_job(row) if row else None

    def mark_notified(self, job_id: str, channel: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET notified_at = ?, notification_channel = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), channel, job_id),
            )
            conn.commit()

    def mark_application_closed(self, job_id: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET application_status = ? WHERE id = ?",
                (ApplicationStatus.CLOSED.value, job_id),
            )
            conn.commit()

    def mark_application_open(self, job_id: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET application_status = ? WHERE id = ?",
                (ApplicationStatus.OPEN.value, job_id),
            )
            conn.commit()

    def expire_jobs(self) -> int:
        """Mark jobs past expires_at as expired. Returns count."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET application_status = ?
                WHERE expires_at IS NOT NULL
                AND expires_at < datetime('now')
                AND application_status = ?
                """,
                (ApplicationStatus.EXPIRED.value, ApplicationStatus.OPEN.value),
            )
            conn.commit()
            return cursor.rowcount

    def get_active_jobs(self, limit: int = 100) -> List[Job]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs
                WHERE application_status IN ('open', 'unknown')
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_job(row) for row in cursor.fetchall()]

    def get_recent_jobs(self, hours: int = 24, limit: int = 50) -> List[Job]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs
                WHERE detected_at >= datetime('now', ?)
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (f"-{hours} hours", limit),
            )
            return [self._row_to_job(row) for row in cursor.fetchall()]

    def get_stats(self) -> ScanStats:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN application_status = 'open' THEN 1 ELSE 0 END) as open,
                    SUM(CASE WHEN application_status = 'closed' THEN 1 ELSE 0 END) as closed,
                    SUM(CASE WHEN application_status = 'expired' THEN 1 ELSE 0 END) as expired,
                    SUM(CASE WHEN application_status = 'unknown' THEN 1 ELSE 0 END) as unknown,
                    SUM(CASE WHEN notified_at IS NOT NULL THEN 1 ELSE 0 END) as notified
                FROM jobs
                """
            )
            row = cursor.fetchone()

            return ScanStats(
                jobs_found=row["total"] or 0,
                jobs_verified=row["open"] or 0,
                jobs_expired=row["expired"] or 0,
                jobs_notified=row["notified"] or 0,
            )

    def count_by_status(self) -> Dict[str, int]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT application_status, COUNT(*) as cnt FROM jobs GROUP BY application_status"
            )
            return {row["application_status"]: row["cnt"] for row in cursor.fetchall()}

    def count_by_source(self) -> Dict[str, int]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source"
            )
            return {row["source"]: row["cnt"] for row in cursor.fetchall()}

    def cleanup_old_jobs(self, days: int = 30) -> int:
        """Remove jobs older than N days that are expired/closed."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE (application_status = 'expired' OR application_status = 'closed')
                AND updated_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            conn.commit()
            return cursor.rowcount