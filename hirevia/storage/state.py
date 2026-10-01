"""SQLite-based job state storage"""

import errno
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

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
    "saved_at", "expires_at", "notified_at", "notification_channel", "ai_decision",
    "match_result", "saved", "deleted_at",
]

# Columns never overwritten on the UPDATE path (identity + lifecycle anchors).
UPSERT_IMMUTABLE_COLUMNS = {
    "id", "source", "source_job_id", "first_seen", "saved_at", "saved", "deleted_at"
}


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
    saved_at TEXT,
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

CREATE TABLE IF NOT EXISTS telegram_message_history (
    message_identity TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    first_discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_timestamp TEXT,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    last_processed_at TEXT,
    last_error TEXT,
    UNIQUE(channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS processing_queue (
    message_identity TEXT PRIMARY KEY
        REFERENCES telegram_message_history(message_identity) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL DEFAULT (datetime('now')),
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_timestamp TEXT,
    processing_started_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_processing_queue_ready
    ON processing_queue(status, available_at, source_timestamp);

CREATE TABLE IF NOT EXISTS worker_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    pid INTEGER,
    status TEXT NOT NULL DEFAULT 'STOPPED',
    monitor_status TEXT NOT NULL DEFAULT 'STOPPED',
    heartbeat_at TEXT,
    started_at TEXT,
    last_error TEXT,
    account_json TEXT,
    validation_json TEXT,
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS worker_commands (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_commands_pending
    ON worker_commands(status, created_at);
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
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
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

        runtime_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(worker_runtime)")
        }
        if "validation_json" not in runtime_columns:
            conn.execute("ALTER TABLE worker_runtime ADD COLUMN validation_json TEXT")
        if "monitor_status" not in runtime_columns:
            conn.execute(
                "ALTER TABLE worker_runtime ADD COLUMN monitor_status TEXT NOT NULL DEFAULT 'STOPPED'"
            )

        conn.execute(
            "UPDATE jobs SET saved_at = COALESCE(first_seen, detected_at, created_at, datetime('now')) "
            "WHERE saved_at IS NULL"
        )
        conn.execute(
            "UPDATE jobs SET expires_at = datetime(saved_at, '+24 hours') "
            "WHERE expires_at IS NULL AND saved_at IS NOT NULL"
        )

        # Fail fast on any remaining model/schema drift.
        final = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        missing = [c for c in JOB_COLUMNS if c not in final]
        if missing:
            raise RuntimeError(f"jobs table missing required columns: {missing}")

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
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
            saved_at=datetime.fromisoformat(row["saved_at"]) if row["saved_at"] else None,
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
            "saved_at": job.saved_at.isoformat() if job.saved_at else now,
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
                "SELECT id, first_seen, saved_at, match_result FROM jobs WHERE source = ? AND source_job_id = ?",
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
                job.saved_at = (
                    datetime.fromisoformat(existing["saved_at"])
                    if existing["saved_at"] else job.first_seen
                )
                if job.expires_at is None:
                    from datetime import timedelta
                    job.expires_at = job.saved_at + timedelta(hours=24)

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
                job.saved_at = job.first_seen
                job.expires_at = job.saved_at + timedelta(hours=24)

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
                "DELETE FROM jobs WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_all_jobs(self) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM jobs")
            conn.commit()
            return cursor.rowcount

    def expire_jobs(self) -> int:
        """Permanently delete jobs past their individual expiration timestamp."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE expires_at IS NOT NULL
                AND julianday(expires_at) < julianday('now')
                """,
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

    @staticmethod
    def _pid_is_alive(pid: Optional[int]) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            return JobRepository._windows_pid_is_alive(pid)
        return JobRepository._posix_pid_is_alive(pid)

    @staticmethod
    def _posix_pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OverflowError, ValueError):
            return False
        except OSError as error:
            if isinstance(error, PermissionError) or error.errno == errno.EPERM:
                return True
            if isinstance(error, ProcessLookupError) or error.errno == errno.ESRCH:
                return False
            return False

    @staticmethod
    def _windows_pid_is_alive(pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        if pid > 0xFFFFFFFF:
            return False

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return ctypes.get_last_error() == error_access_denied

        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ctypes.get_last_error() == error_access_denied
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    def claim_worker_lease(self, pid: int) -> bool:
        """Atomically acquire the singleton worker lease for this OS process."""
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT pid, heartbeat_at FROM worker_runtime WHERE singleton = 1"
            ).fetchone()
            if row and row["pid"] != pid and self._pid_is_alive(row["pid"]):
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO worker_runtime
                    (singleton, pid, status, monitor_status, heartbeat_at, started_at, last_error)
                VALUES (1, ?, 'STARTING', 'STOPPED', datetime('now'), datetime('now'), NULL)
                ON CONFLICT(singleton) DO UPDATE SET
                    pid = excluded.pid,
                    status = 'STARTING',
                    monitor_status = 'STOPPED',
                    heartbeat_at = datetime('now'),
                    started_at = datetime('now'),
                    last_error = NULL
                """,
                (pid,),
            )
            conn.commit()
        return True

    def update_worker_runtime(
        self,
        pid: int,
        status: str,
        *,
        last_error: Optional[str] = None,
        monitor_status: Optional[str] = None,
        account: Optional[Dict[str, Any]] = None,
        validation: Optional[Dict[str, Any]] = None,
        last_scan_at: Optional[datetime] = None,
    ) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE worker_runtime
                SET status = ?, monitor_status = COALESCE(?, monitor_status),
                    heartbeat_at = datetime('now'), last_error = COALESCE(?, last_error),
                    account_json = COALESCE(?, account_json),
                    validation_json = COALESCE(?, validation_json),
                    last_scan_at = COALESCE(?, last_scan_at)
                WHERE singleton = 1 AND pid = ?
                """,
                (
                    status,
                    monitor_status,
                    str(last_error)[:300] if last_error else None,
                    json.dumps(account) if account is not None else None,
                    json.dumps(validation) if validation is not None else None,
                    last_scan_at.isoformat() if last_scan_at else None,
                    pid,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_worker_lease(self, pid: int, status: str = "STOPPED") -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE worker_runtime
                SET status = ?, monitor_status = 'STOPPED',
                    heartbeat_at = datetime('now'), pid = NULL
                WHERE singleton = 1 AND pid = ?
                """,
                (status, pid),
            )
            conn.commit()

    def clear_worker_account(self, pid: int) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE worker_runtime SET account_json = NULL, validation_json = NULL,
                    monitor_status = 'STOPPED', heartbeat_at = datetime('now')
                WHERE singleton = 1 AND pid = ?
                """,
                (pid,),
            )
            conn.commit()

    def get_worker_runtime(self) -> Dict[str, Any]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM worker_runtime WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return {
                "pid": None,
                "status": "STOPPED",
                "monitor_status": "STOPPED",
                "alive": False,
                "desired_monitoring": self.get_source_state(
                    "system", "monitor_desired", "stopped"
                ),
            }
        try:
            account = json.loads(row["account_json"]) if row["account_json"] else None
        except (ValueError, TypeError):
            account = None
        try:
            validation = json.loads(row["validation_json"]) if row["validation_json"] else None
        except (ValueError, TypeError):
            validation = None
        pid = row["pid"]
        alive = self._pid_is_alive(pid)
        status = row["status"]
        if pid is not None and not alive and status in {"STARTING", "RUNNING", "STOPPING"}:
            status = "ERROR"
        return {
            "pid": pid,
            "alive": alive,
            "status": status,
            "monitor_status": row["monitor_status"] or "STOPPED",
            "heartbeat_at": row["heartbeat_at"],
            "started_at": row["started_at"],
            "last_error": row["last_error"],
            "account": account,
            "validation": validation,
            "last_scan_at": row["last_scan_at"],
            "desired_monitoring": self.get_source_state(
                "system", "monitor_desired", "stopped"
            ),
        }

    def enqueue_worker_command(self, command: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        command_id = str(uuid4())
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO worker_commands (id, command, arguments_json) VALUES (?, ?, ?)",
                (command_id, command, json.dumps(arguments or {})),
            )
            conn.commit()
        return command_id

    def claim_worker_commands(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM worker_commands WHERE status = 'PENDING' "
                "ORDER BY created_at LIMIT ?",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    "UPDATE worker_commands SET status = 'RUNNING', updated_at = datetime('now') "
                    "WHERE id = ? AND status = 'PENDING'",
                    (row["id"],),
                )
                claimed.append({
                    "id": row["id"],
                    "command": row["command"],
                    "arguments": json.loads(row["arguments_json"] or "{}"),
                })
            conn.commit()
        return claimed

    def recover_worker_commands(self) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE worker_commands SET status = 'PENDING', updated_at = datetime('now') "
                "WHERE status = 'RUNNING' AND command != 'SHUTDOWN'"
            )
            conn.execute(
                "UPDATE worker_commands SET status = 'ERROR', "
                "updated_at = datetime('now'), error = 'Worker exited before shutdown completed' "
                "WHERE status = 'RUNNING' AND command = 'SHUTDOWN'"
            )
            conn.commit()
            return cursor.rowcount

    def complete_worker_command(
        self, command_id: str, *, result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE worker_commands SET status = ?, updated_at = datetime('now'),
                    result_json = ?, error = ? WHERE id = ?
                """,
                (
                    "ERROR" if error else "COMPLETED",
                    json.dumps(result) if result is not None else None,
                    str(error)[:300] if error else None,
                    command_id,
                ),
            )
            conn.commit()

    def get_worker_command(self, command_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM worker_commands WHERE id = ?", (command_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except (ValueError, TypeError):
            result = None
        return {"status": row["status"], "result": result, "error": row["error"]}

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

    @staticmethod
    def _telegram_message_identity(channel_id: str, message_id: str) -> str:
        return f"telegram:{channel_id}:{message_id}"

    def record_telegram_message(
        self, channel_id: str, message_id: str, source_timestamp: Optional[datetime] = None
    ) -> bool:
        """Record a lightweight identity; return false for terminal history rows."""
        identity = self._telegram_message_identity(str(channel_id), str(message_id))
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_message_history
                    (message_identity, channel_id, message_id, source_timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (
                    identity,
                    str(channel_id),
                    str(message_id),
                    source_timestamp.isoformat() if source_timestamp else None,
                ),
            )
            row = conn.execute(
                "SELECT status FROM telegram_message_history WHERE message_identity = ?",
                (identity,),
            ).fetchone()
            conn.commit()
        return bool(row and row["status"] not in {"PROCESSED", "EXPIRED"})

    def mark_telegram_message_processed(self, channel_id: str, message_id: str) -> None:
        identity = self._telegram_message_identity(str(channel_id), str(message_id))
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE telegram_message_history
                SET status = 'PROCESSED', last_processed_at = datetime('now'), last_error = NULL
                WHERE message_identity = ? AND status != 'EXPIRED'
                """,
                (identity,),
            )
            conn.commit()

    def enqueue_telegram_job(self, job: Job) -> bool:
        """Persist a normalized Telegram candidate once, keyed by its source message."""
        if job.application_url:
            application_url = str(job.application_url)
            parsed_url = urlparse(application_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                job.raw_data["invalid_application_url"] = application_url[:500]
                job.application_url = None
        channel_id = str(job.raw_data.get("source_channel") or "unknown")
        message_id = str(job.raw_data.get("source_message_id") or job.source_job_id)
        identity = self._telegram_message_identity(channel_id, message_id)
        source_timestamp = job.posted_at.isoformat() if job.posted_at else None
        payload = job.model_dump_json()
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_message_history
                    (message_identity, channel_id, message_id, source_timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (identity, channel_id, message_id, source_timestamp),
            )
            history = conn.execute(
                "SELECT status FROM telegram_message_history WHERE message_identity = ?",
                (identity,),
            ).fetchone()
            if history is None or history["status"] in {"PROCESSED", "EXPIRED"}:
                conn.commit()
                return False
            existing = conn.execute(
                "SELECT status FROM processing_queue WHERE message_identity = ?",
                (identity,),
            ).fetchone()
            if existing:
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO processing_queue
                    (message_identity, payload, status, source_timestamp)
                VALUES (?, ?, 'QUEUED', ?)
                """,
                (identity, payload, source_timestamp),
            )
            conn.execute(
                "UPDATE telegram_message_history SET status = 'QUEUED' WHERE message_identity = ?",
                (identity,),
            )
            conn.commit()
        return True

    def recover_processing_queue(self) -> int:
        """Return interrupted claims to the queue after an application restart."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE processing_queue
                SET status = 'QUEUED', processing_started_at = NULL,
                    available_at = datetime('now')
                WHERE status = 'PROCESSING'
                """
            )
            conn.execute(
                """
                UPDATE telegram_message_history
                SET status = 'QUEUED'
                WHERE status = 'PROCESSING'
                """
            )
            conn.commit()
            return cursor.rowcount

    def claim_telegram_jobs(self, limit: int = 25) -> List[Tuple[str, Job]]:
        """Atomically claim ready queue entries so only one worker processes each."""
        limit = max(1, min(int(limit), 100))
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT message_identity, payload FROM processing_queue
                WHERE status = 'QUEUED'
                  AND julianday(available_at) <= julianday('now')
                  AND julianday(COALESCE(source_timestamp, first_seen_at))
                      > julianday('now', '-24 hours')
                ORDER BY COALESCE(source_timestamp, first_seen_at), message_identity
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            claimed = []
            for row in rows:
                identity = row["message_identity"]
                updated = conn.execute(
                    """
                    UPDATE processing_queue
                    SET status = 'PROCESSING', attempts = attempts + 1,
                        processing_started_at = datetime('now')
                    WHERE message_identity = ? AND status = 'QUEUED'
                    """,
                    (identity,),
                )
                if updated.rowcount:
                    conn.execute(
                        "UPDATE telegram_message_history SET status = 'PROCESSING' WHERE message_identity = ?",
                        (identity,),
                    )
                    claimed.append((identity, Job.model_validate_json(row["payload"])))
            conn.commit()
        return claimed

    def complete_telegram_job(self, message_identity: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE telegram_message_history
                SET status = 'PROCESSED', last_processed_at = datetime('now'), last_error = NULL
                WHERE message_identity = ? AND status != 'EXPIRED'
                """,
                (message_identity,),
            )
            conn.execute(
                "DELETE FROM processing_queue WHERE message_identity = ?",
                (message_identity,),
            )
            conn.commit()

    def retry_telegram_job(self, message_identity: str, error: str) -> int:
        """Requeue a failed item with a capped exponential delay."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT attempts FROM processing_queue WHERE message_identity = ?",
                (message_identity,),
            ).fetchone()
            if row is None:
                return 0
            delay = min(2 ** min(max(row["attempts"] - 1, 0), 8), 300)
            conn.execute(
                """
                UPDATE processing_queue
                SET status = 'QUEUED', processing_started_at = NULL,
                    available_at = datetime('now', ?), last_error = ?
                WHERE message_identity = ?
                """,
                (f"+{delay} seconds", str(error)[:300], message_identity),
            )
            conn.execute(
                """
                UPDATE telegram_message_history
                SET status = 'FAILED', last_error = ?
                WHERE message_identity = ? AND status != 'EXPIRED'
                """,
                (str(error)[:300], message_identity),
            )
            conn.commit()
            return delay

    def expire_pending_telegram_jobs(self) -> int:
        """Expire queue payloads after 24h while retaining identity history."""
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT message_identity FROM processing_queue
                WHERE julianday(COALESCE(source_timestamp, first_seen_at))
                    <= julianday('now', '-24 hours')
                """
            ).fetchall()
            identities = [row["message_identity"] for row in rows]
            for identity in identities:
                conn.execute(
                    "UPDATE telegram_message_history SET status = 'EXPIRED', last_error = NULL WHERE message_identity = ?",
                    (identity,),
                )
                conn.execute(
                    "DELETE FROM processing_queue WHERE message_identity = ?",
                    (identity,),
                )
            conn.commit()
        return len(identities)

    def get_telegram_queue_status(self) -> Dict[str, Any]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM processing_queue GROUP BY status"
            ).fetchall()
            oldest = conn.execute(
                "SELECT MIN(COALESCE(source_timestamp, first_seen_at)) AS oldest FROM processing_queue"
            ).fetchone()["oldest"]
        counts = {row["status"].lower(): row["count"] for row in rows}
        return {
            "queued": counts.get("queued", 0),
            "processing": counts.get("processing", 0),
            "oldest_pending_at": oldest,
        }

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