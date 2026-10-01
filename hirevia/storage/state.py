"""SQLite-based job state storage"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from hirevia.core.dedup import normalize_url
from hirevia.core.models import Job, ApplicationStatus, ScanStats


# Canonical persisted column order. INSERT/UPDATE columns, placeholders and
# values are ALL generated from this list - a manual VALUES list drifted once
# ("25 values for 24 columns") and this structure makes that impossible.
JOB_COLUMNS = [
    "id", "source", "source_job_id", "title", "company", "location",
    "description", "url", "application_url", "posted_at", "detected_at",
    "deadline", "employment_type", "experience", "skills", "raw_data",
    "application_status", "fingerprint", "first_seen", "last_seen",
    "expires_at", "notified_at", "notification_channel", "ai_decision",
    "match_result", "saved", "deleted_at",
]

# Columns never overwritten on the UPDATE path (identity + lifecycle anchors).
UPSERT_IMMUTABLE_COLUMNS = {"id", "source", "source_job_id", "first_seen", "saved", "deleted_at"}


def _load_json(raw, expect: type):
    """Decode a JSON column defensively.

    NULL, unreadable text and a payload of the wrong shape all come back as None
    rather than crashing the read of an otherwise perfectly good job - a type
    mismatch would also break pydantic's field validation on Job.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, expect) else None

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
    match_result TEXT,
    saved INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_source_id ON jobs(source, source_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs(expires_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(application_status);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);

-- Small key/value store so collectors can resume where they stopped even
-- after a restart (e.g. telegram last message id per channel).
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key)
);
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
            self._migrate_schema(conn)
            conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection):
        """Lightweight additive migration: CREATE TABLE IF NOT EXISTS never
        touches a pre-existing table, so an old DB missing newer columns
        (e.g. ai_decision, match_result) would break INSERTs. Add anything
        missing."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for column in JOB_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")

        # Fail fast on any remaining model/schema drift.
        final = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        missing = [c for c in JOB_COLUMNS if c not in final]
        if missing:
            raise RuntimeError(f"jobs table missing required columns: {missing}")

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        def json_col(name: str, expect: type):
            """Read a JSON column, tolerating one the SELECT never returned
            (a row written before that column existed)."""
            try:
                return _load_json(row[name], expect)
            except (IndexError, KeyError):
                return None

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
            skills=json_col("skills", list) or [],
            raw_data=json_col("raw_data", dict) or {},
            application_status=ApplicationStatus(row["application_status"]),
            fingerprint=row["fingerprint"],
            first_seen=datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None,
            last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            notified_at=datetime.fromisoformat(row["notified_at"]) if row["notified_at"] else None,
            notification_channel=row["notification_channel"],
            saved=bool(row["saved"]) if "saved" in row.keys() else False,
            deleted_at=datetime.fromisoformat(row["deleted_at"]) if "deleted_at" in row.keys() and row["deleted_at"] else None,
            ai_decision=json_col("ai_decision", dict),
            match_result=json_col("match_result", dict),
        )

    def _job_to_dict(self, job: Job) -> Dict[str, Any]:
        """Single canonical Job -> column mapping. Every persisted column is
        keyed by name here, so INSERT/UPDATE arity can never drift again."""
        now = datetime.utcnow().isoformat()
        return {
            "id": job.id,
            "source": job.source,
            "source_job_id": job.source_job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description,
            "url": str(job.url),
            "application_url": str(job.application_url) if job.application_url else None,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "detected_at": job.detected_at.isoformat() if job.detected_at else now,
            "deadline": job.deadline.isoformat() if job.deadline else None,
            "employment_type": job.employment_type.value if job.employment_type else None,
            "experience": job.experience,
            "skills": json.dumps(job.skills),
            "raw_data": json.dumps(job.raw_data),
            "application_status": job.application_status.value,
            "fingerprint": job.fingerprint,
            "first_seen": job.first_seen.isoformat() if job.first_seen else now,
            "last_seen": job.last_seen.isoformat() if job.last_seen else now,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "notified_at": job.notified_at.isoformat() if job.notified_at else None,
            "notification_channel": job.notification_channel,
            "saved": int(job.saved),
            "deleted_at": job.deleted_at.isoformat() if job.deleted_at else None,
            "ai_decision": json.dumps(job.ai_decision) if job.ai_decision else None,
            # Phase 7: the canonical hybrid verdict (see MatchResult).
            "match_result": json.dumps(job.match_result) if job.match_result else None,
        }

    def _job_to_row(self, job: Job) -> Tuple:
        values = self._job_to_dict(job)
        row = tuple(values[col] for col in JOB_COLUMNS)
        if len(row) != len(JOB_COLUMNS):  # defensive - cannot happen by construction
            raise ValueError(f"Job row arity mismatch: {len(row)} values / {len(JOB_COLUMNS)} columns")
        return row

    def upsert_job(self, job: Job) -> bool:
        """Insert or update job. Returns True if newly inserted.

        Columns, placeholders and values are all derived from JOB_COLUMNS so
        the old '25 values for 24 columns' class of bug is structurally
        impossible now.
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT id, first_seen, match_result FROM jobs WHERE source = ? AND source_job_id = ?",
                (job.source, job.source_job_id),
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing
                job.id = existing["id"]
                # Nullable in the schema, and rows written by early code left it
                # empty - treat "unknown" as "now" rather than crashing the scan.
                job.first_seen = (
                    datetime.fromisoformat(existing["first_seen"])
                    if existing["first_seen"]
                    else datetime.utcnow()
                )
                job.last_seen = datetime.utcnow()
                if job.expires_at is None:
                    from datetime import timedelta
                    job.expires_at = job.first_seen + timedelta(hours=24)

                values = self._job_to_dict(job)
                if not values["match_result"] and existing["match_result"]:
                    # A pass with no verdict (a collector re-writing the row, or
                    # a duplicate seen before matching runs) must not erase the
                    # stored score that the dashboard and alert retry read.
                    stored = _load_json(existing["match_result"], dict)
                    if stored is not None:
                        job.match_result = stored
                        values["match_result"] = existing["match_result"]
                update_cols = [c for c in JOB_COLUMNS if c not in UPSERT_IMMUTABLE_COLUMNS]
                sql = (
                    f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in update_cols)} WHERE id = ?"
                )
                params = tuple(values[c] for c in update_cols) + (job.id,)
                if len(params) != len(update_cols) + 1:  # defensive
                    raise ValueError("Job update arity mismatch")
                conn.execute(sql, params)
                conn.commit()
                return False
            else:
                # New job
                job.first_seen = datetime.utcnow()
                job.last_seen = job.first_seen
                from datetime import timedelta
                job.expires_at = job.first_seen + timedelta(hours=24)

                sql = (
                    f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in JOB_COLUMNS)})"
                )
                conn.execute(sql, self._job_to_row(job))
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

    def get_by_application_url(self, application_url: str) -> Optional[Job]:
        """Find a stored job by canonical application URL, across sources."""
        normalized = normalize_url(str(application_url))
        host = urlparse(str(application_url)).netloc.lower().replace("www.", "", 1)
        if not normalized or not host:
            return None

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE application_url IS NOT NULL "
                "AND instr(lower(replace(application_url, 'www.', '')), ?) > 0 "
                "ORDER BY detected_at DESC",
                (host,),
            ).fetchall()
        for row in rows:
            job = self._row_to_job(row)
            if job.application_url and normalize_url(str(job.application_url)) == normalized:
                return job
        return None

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

    def set_saved(self, job_id: str, saved: bool = True) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET saved = ? WHERE id = ? AND deleted_at IS NULL",
                (int(saved), job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def soft_delete_job(self, job_id: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (datetime.utcnow().isoformat(), job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def expire_jobs(self) -> int:
        """Mark jobs past expires_at as expired. Returns count."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET application_status = ?
                WHERE expires_at IS NOT NULL
                AND julianday(expires_at) < julianday('now')
                AND application_status IN (?, ?)
                """,
                (
                    ApplicationStatus.EXPIRED.value,
                    ApplicationStatus.OPEN.value,
                    ApplicationStatus.UNKNOWN.value,
                ),
            )
            conn.commit()
            return cursor.rowcount

    def get_active_jobs(self, limit: int = 100, hours: Optional[int] = None) -> List[Job]:
        age_clause = ""
        params: Tuple[Any, ...] = ()
        if hours is not None:
            age_clause = "AND julianday(detected_at) >= julianday('now', ?)"
            params = (f"-{hours} hours",)
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE application_status IN ('open', 'unknown')
                AND deleted_at IS NULL
                AND (expires_at IS NULL OR julianday(expires_at) >= julianday('now'))
                {age_clause}
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (*params, limit),
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

    def get_source_state(self, source: str, key: str, default: Any = None) -> Any:
        """Read a persisted collector cursor/value."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM source_state WHERE source = ? AND key = ?",
                (source, key),
            ).fetchone()
            return row["value"] if row else default

    def set_source_state(self, source: str, key: str, value: Any) -> None:
        """Persist a collector cursor/value (idempotent)."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO source_state (source, key, value, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(source, key) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (source, key, None if value is None else str(value)),
            )
            conn.commit()

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