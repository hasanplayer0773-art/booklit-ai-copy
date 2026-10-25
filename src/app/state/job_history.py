"""
Booklet_AI Desktop — Job History Manager (STEP-6.3)

Records completed and failed generation jobs into the job_history table
in the STEP-3.1 SQLite database. Provides query methods for the
Dashboard (STEP-6.9) and the preview/history panel.

Lock discipline:
- Job History read/write operations NEVER acquire DocumentStore's
  threading.RLock (AC-6.3.5). They interact ONLY with the SQLite
  connection — these are pure database operations with no
  DocumentStore interaction.

Error translation:
- On failure, error_summary is populated using STEP-6.2's
  translate_error(), never a raw exception string (AC-6.3.2).
"""

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.ui.error_translation import translate_error

logger = logging.getLogger("booklet_ai.job_history")


def compute_source_file_sha256(file_path: Union[str, Path]) -> str:
    """Compute SHA-256 of raw source file bytes.

    This is distinct from STEP-1.1's compute_canonical_hash() which
    operates on NFC-normalized extracted TEXT. This function operates
    on the raw file bytes on disk, identifying WHICH FILE a job was
    run against.

    Args:
        file_path: Absolute path to the source file.

    Returns:
        Lowercase hex SHA-256 digest of the file bytes.
    """
    hasher = hashlib.sha256()
    path = Path(file_path)
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


class JobHistoryManager:
    """Manages job_history records in the STEP-3.1 SQLite database.

    Thread safety:
    - All operations are serialized through SQLite's own locking.
    - This class NEVER acquires DocumentStore's RLock (AC-6.3.5).

    Usage:
        manager = JobHistoryManager(db_conn)
        job_id = manager.start_job("/path/to/source.docx", "abc123...")
        # ... run generation pipeline ...
        manager.complete_job(job_id, page_count=12)
        # or on failure:
        manager.fail_job(job_id, exception)
    """

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        """Initialize the JobHistoryManager.

        Args:
            db_conn: Open sqlite3.Connection to the STEP-3.1 database
                     with the 002_job_history_and_undo migration applied.
        """
        self._db_conn = db_conn

    def start_job(
        self,
        source_file_path: str,
        source_file_sha256: str,
        document_content_hash: Optional[str] = None,
    ) -> int:
        """Record a new job as 'in_progress'.

        Called at the start of a generation pipeline run.

        Args:
            source_file_path: Absolute path to the source file.
            source_file_sha256: SHA-256 of the raw source file bytes.
            document_content_hash: Optional STEP-1.1 content hash of
                                   the extracted text tree.

        Returns:
            The auto-generated job_history.id for this new job.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            cursor = self._db_conn.execute(
                "INSERT INTO job_history "
                "(source_file_path, source_file_sha256, document_content_hash, "
                "status, started_at) "
                "VALUES (?, ?, ?, 'in_progress', ?)",
                (
                    source_file_path,
                    source_file_sha256,
                    document_content_hash,
                    now_iso,
                ),
            )
            self._db_conn.commit()
            job_id = cursor.lastrowid
            logger.info(
                "Started job %d for file: %s", job_id, source_file_path
            )
            return job_id
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception("Failed to record job start")
            raise

    def complete_job(
        self,
        job_id: int,
        page_count: Optional[int] = None,
        document_content_hash: Optional[str] = None,
    ) -> None:
        """Mark a job as successfully completed.

        Args:
            job_id: The job_history.id to update.
            page_count: Final page count of the generated booklet.
            document_content_hash: Final content hash, if updated.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._db_conn.execute(
                "UPDATE job_history SET "
                "status = 'success', "
                "page_count = ?, "
                "document_content_hash = COALESCE(?, document_content_hash), "
                "completed_at = ? "
                "WHERE id = ?",
                (page_count, document_content_hash, now_iso, job_id),
            )
            self._db_conn.commit()
            logger.info("Completed job %d successfully", job_id)
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception("Failed to record job completion for %d", job_id)
            raise

    def fail_job(self, job_id: int, error: Exception) -> None:
        """Mark a job as failed with a translated error summary.

        The error_summary is populated using STEP-6.2's translate_error()
        — never a raw exception string (AC-6.3.2).

        Args:
            job_id: The job_history.id to update.
            error: The exception that caused the failure.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        user_facing = translate_error(error)
        try:
            self._db_conn.execute(
                "UPDATE job_history SET "
                "status = 'failed', "
                "error_summary = ?, "
                "completed_at = ? "
                "WHERE id = ?",
                (user_facing.translated_message, now_iso, job_id),
            )
            self._db_conn.commit()
            logger.info(
                "Marked job %d as failed: %s",
                job_id,
                user_facing.translated_message,
            )
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception("Failed to record job failure for %d", job_id)
            raise

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve a single job by ID.

        Args:
            job_id: The job_history.id to look up.

        Returns:
            Dict of job fields, or None if not found.
        """
        cursor = self._db_conn.execute(
            "SELECT * FROM job_history WHERE id = ?", (job_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_recent_jobs(
        self, limit: int = 50, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve recent jobs, optionally filtered by status.

        Args:
            limit: Maximum number of jobs to return.
            status: Optional filter: 'success', 'failed', or 'in_progress'.

        Returns:
            List of job dicts, most recent first.
        """
        if status is not None:
            cursor = self._db_conn.execute(
                "SELECT * FROM job_history "
                "WHERE status = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = self._db_conn.execute(
                "SELECT * FROM job_history "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def record_preflight_override(
        self,
        job_id: int,
        blocker_code: str,
        reason: str,
        user_identifier: str = "designer",
    ) -> int:
        """Record an explicit user override for a preflight blocker (AC-6.4.1b).

        Auditable log entry ensuring legacy equations cannot be bypassed
        silently without an audit trail.

        Args:
            job_id: The job_history.id associated with this override.
            blocker_code: The code of the blocker being overridden.
            reason: Designer's explicit reason / note for overriding.
            user_identifier: Identifier for the operator/designer performing the override.

        Returns:
            The auto-generated preflight_overrides.id.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            cursor = self._db_conn.execute(
                "INSERT INTO preflight_overrides "
                "(job_id, blocker_code, reason, overridden_at, user_identifier) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, blocker_code, reason, now_iso, user_identifier),
            )
            self._db_conn.commit()
            override_id = cursor.lastrowid
            logger.info(
                "Recorded preflight override %d for job %d: blocker '%s'",
                override_id,
                job_id,
                blocker_code,
            )
            return override_id
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception("Failed to record preflight override for job %d", job_id)
            raise

    def get_preflight_overrides(
        self, job_id: int
    ) -> List[Dict[str, Any]]:
        """Retrieve all preflight override audit records for a job.

        Args:
            job_id: The job_history.id to look up.

        Returns:
            List of override dicts.
        """
        cursor = self._db_conn.execute(
            "SELECT * FROM preflight_overrides WHERE job_id = ? ORDER BY overridden_at ASC",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_jobs_for_file(
        self, source_file_sha256: str
    ) -> List[Dict[str, Any]]:
        """Retrieve all jobs for a specific source file by its SHA-256.

        Args:
            source_file_sha256: SHA-256 of the raw source file bytes.

        Returns:
            List of job dicts, most recent first.
        """
        cursor = self._db_conn.execute(
            "SELECT * FROM job_history "
            "WHERE source_file_sha256 = ? "
            "ORDER BY started_at DESC",
            (source_file_sha256,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def find_session_for_file(
        self, source_file_sha256: str
    ) -> Optional[str]:
        """Find the most recent document_session_id for a file hash.

        Used during document-reopen to find an existing edit session
        for replay (STEP-6.3 §4 recovery sequence).

        Args:
            source_file_sha256: SHA-256 of the raw source file bytes.

        Returns:
            document_session_id if found, None otherwise.
        """
        # Look for edit sessions tied to jobs with this file hash
        cursor = self._db_conn.execute(
            "SELECT DISTINCT beh.document_session_id "
            "FROM block_edit_history beh "
            "JOIN job_history jh ON beh.job_id = jh.id "
            "WHERE jh.source_file_sha256 = ? "
            "ORDER BY beh.edited_at DESC LIMIT 1",
            (source_file_sha256,),
        )
        row = cursor.fetchone()
        if row:
            return row["document_session_id"]

        # Also check sessions not yet tied to a job (pre-submission edits)
        # by looking at edit_cursor entries that match sessions with edits
        cursor = self._db_conn.execute(
            "SELECT DISTINCT document_session_id "
            "FROM block_edit_history "
            "ORDER BY edited_at DESC",
        )
        # Without a direct link, we can't match orphan sessions to files.
        # This path requires the caller to provide the session_id explicitly.
        return None

    def get_dashboard_metrics(
        self,
        period: Any = "all_time",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Any:
        """Retrieve aggregated dashboard metrics (STEP-6.9).

        Delegates to DashboardAggregator to compute metrics directly via SQL.
        """
        from app.state.dashboard import DashboardAggregator

        aggregator = DashboardAggregator(self._db_conn)
        return aggregator.get_metrics(
            period=period,
            start_time=start_time,
            end_time=end_time,
        )

    def get_booklets_processed(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> int:
        """Count successful booklet jobs in the given period (AC-6.9.1)."""
        from app.state.dashboard import DashboardAggregator

        aggregator = DashboardAggregator(self._db_conn)
        return aggregator.get_booklets_processed(start_time=start_time, end_time=end_time)

    def get_error_rate(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> float:
        """Compute error rate as failed / (failed + success) (AC-6.9.2)."""
        from app.state.dashboard import DashboardAggregator

        aggregator = DashboardAggregator(self._db_conn)
        return aggregator.get_error_rate(start_time=start_time, end_time=end_time)

    def get_average_processing_time(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> float:
        """Compute average processing time for successful jobs in seconds (AC-6.9.4)."""
        from app.state.dashboard import DashboardAggregator

        aggregator = DashboardAggregator(self._db_conn)
        return aggregator.get_average_processing_time(
            start_time=start_time, end_time=end_time
        )

