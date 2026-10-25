"""
Booklet_AI Desktop — Dashboard Aggregator & Metrics Service (STEP-6.9)

Provides read-only aggregation over the STEP-6.3 `job_history` table for
shop-level visibility (booklets processed, average processing time, error rate).

Key Guarantees & Constraints:
- Read-Only: Introduces no new write path and no new data beyond what
  `job_history` already records (STEP-6.9 §2).
- SQL Aggregation: All aggregate figures (processed count, average time, error rate)
  are computed directly via SQL aggregate queries (`COUNT`, `AVG`) executed
  against SQLite — never loading rows into Python (Precondition §4).
- No DocumentStore / RLock: Never imports or acquires `DocumentStore`'s `threading.RLock`
  — data path touches exclusively the SQLite connection (AC-6.9.3, AC-6.3.5).
- Single Logical Job Counting: Retries within `run_job_with_retry()` are executions
  of a single logical job and are counted as exactly one booklet or one failure,
  never two (AC-6.9.1b).
- Queue Wait Time Exclusion: Average processing time uses each job's own `started_at`
  (when execution began), never its batch enqueue time (AC-6.9.4).
- Freshness: Error rate and metrics are recomputed on every call, never cached in a
  way that could drift from the underlying table (AC-6.9.2).
- High Performance: Queries leverage the indexes defined in the job history migration
  (`idx_job_history_started_at`, `idx_job_history_status`, `idx_job_history_status_started_at`) (AC-6.9.5).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("booklet_ai.dashboard")


class TimePeriod(str, Enum):
    """Supported time filter periods for Dashboard aggregation."""

    ALL_TIME = "all_time"
    TODAY = "today"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    CUSTOM = "custom"


class DashboardMetrics(BaseModel):
    """Immutable aggregate visibility metrics for the print-shop owner (STEP-6.9).

    Attributes:
        booklets_processed: Count of logical jobs successfully completed (status='success')
            in the selected period (AC-6.9.1).
        failed_jobs: Count of logical jobs with status='failed' in the selected period.
        total_completed: Total completed logical jobs (success + failed) in the period.
            Excludes in_progress and cancelled jobs.
        error_rate: Float between 0.0 and 1.0 representing failed / total_completed (AC-6.9.2).
        error_rate_percentage: Float between 0.0 and 100.0 representing error_rate * 100.
        average_processing_time_seconds: Average processing duration in seconds
            (completed_at - started_at) for status='success' jobs only (AC-6.9.4).
        period: The selected time period identifier.
        start_time: ISO-8601 string for the beginning of the period filter, if any.
        end_time: ISO-8601 string for the end of the period filter, if any.
    """

    model_config = ConfigDict(frozen=True)

    booklets_processed: int = Field(default=0, ge=0)
    failed_jobs: int = Field(default=0, ge=0)
    total_completed: int = Field(default=0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    error_rate_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    average_processing_time_seconds: float = Field(default=0.0, ge=0.0)
    period: str = "all_time"
    start_time: Optional[str] = None
    end_time: Optional[str] = None


def compute_time_range(
    period: Union[TimePeriod, str],
    reference_time: Optional[datetime] = None,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Compute the ISO-8601 [start_time, end_time] bounds for a given time period.

    Args:
        period: TimePeriod enum value or string representation.
        reference_time: Optional datetime reference (defaults to utcnow).
            Useful for deterministic testing.
        custom_start: Optional ISO string for custom start bound.
        custom_end: Optional ISO string for custom end bound.

    Returns:
        Tuple of (start_iso, end_iso), either of which may be None for unbounded.
    """
    period_str = period.value if isinstance(period, TimePeriod) else str(period).lower()
    ref = reference_time or datetime.now(timezone.utc)

    if period_str == TimePeriod.ALL_TIME.value:
        return None, None

    if period_str == TimePeriod.TODAY.value:
        start_today = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_today.isoformat(), ref.isoformat()

    if period_str == TimePeriod.LAST_7_DAYS.value:
        start_7d = ref - timedelta(days=7)
        return start_7d.isoformat(), ref.isoformat()

    if period_str == TimePeriod.LAST_30_DAYS.value:
        start_30d = ref - timedelta(days=30)
        return start_30d.isoformat(), ref.isoformat()

    if period_str == TimePeriod.CUSTOM.value:
        return custom_start, custom_end

    logger.warning("Unrecognized time period '%s', defaulting to all_time", period)
    return None, None


class DashboardAggregator:
    """Read-only aggregation engine over the SQLite `job_history` table.

    Computes shop-level usage statistics entirely via SQL aggregate queries
    (COUNT, AVG) without loading job rows into Python application memory.

    Concurrency & Thread Safety:
    - Never imports or acquires DocumentStore's threading.RLock (AC-6.9.3).
    - Concurrency placement is consistent with STEP-6.3 (AC-6.3.5): pure SQLite reads.
    """

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        """Initialize the DashboardAggregator.

        Args:
            db_conn: Open sqlite3.Connection to the Booklet_AI database.
        """
        self._db_conn = db_conn

    def get_metrics(
        self,
        period: Union[TimePeriod, str] = TimePeriod.ALL_TIME,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        reference_time: Optional[datetime] = None,
    ) -> DashboardMetrics:
        """Compute aggregate dashboard metrics for the selected time period.

        Recomputed directly from `job_history` on each call to prevent stale
        drift from the underlying table (AC-6.9.2).

        Args:
            period: TimePeriod enum or identifier.
            start_time: Optional explicit start time ISO string (overrides period if provided).
            end_time: Optional explicit end time ISO string.
            reference_time: Optional reference datetime for deterministic testing.

        Returns:
            Immutable DashboardMetrics model.
        """
        period_str = period.value if isinstance(period, TimePeriod) else str(period)

        if start_time is None and end_time is None and period_str != TimePeriod.ALL_TIME.value:
            s_iso, e_iso = compute_time_range(period, reference_time=reference_time)
        else:
            s_iso, e_iso = start_time, end_time

        # Build SQL aggregate query with optional date filters
        conditions: List[str] = []
        params: List[Any] = []

        if s_iso is not None:
            conditions.append("started_at >= ?")
            params.append(s_iso)
        if e_iso is not None:
            conditions.append("started_at <= ?")
            params.append(e_iso)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Precondition §4: Compute via SQL aggregate queries directly in SQLite
        query = f"""
            SELECT
                COUNT(CASE WHEN status = 'success' THEN 1 END) AS booklets_processed,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed_jobs,
                COUNT(CASE WHEN status IN ('success', 'failed') THEN 1 END) AS total_completed,
                COALESCE(ROUND(AVG(CASE WHEN status = 'success' AND completed_at IS NOT NULL THEN
                    ((julianday(completed_at) - julianday(started_at)) * 86400.0)
                END), 2), 0.0) AS avg_duration_seconds
            FROM job_history
            {where_clause}
        """

        try:
            cursor = self._db_conn.execute(query, params)
            row = cursor.fetchone()
            if row is None:
                return DashboardMetrics(period=period_str, start_time=s_iso, end_time=e_iso)

            processed = int(row[0] or 0)
            failed = int(row[1] or 0)
            completed = int(row[2] or 0)
            avg_time = float(row[3] or 0.0)

            # AC-6.9.2: error_rate matches failed / (failed + success);
            # in_progress and cancelled rows are strictly excluded.
            if completed > 0:
                err_rate = round(failed / completed, 4)
            else:
                err_rate = 0.0

            err_pct = round(err_rate * 100.0, 2)

            return DashboardMetrics(
                booklets_processed=processed,
                failed_jobs=failed,
                total_completed=completed,
                error_rate=err_rate,
                error_rate_percentage=err_pct,
                average_processing_time_seconds=avg_time,
                period=period_str,
                start_time=s_iso,
                end_time=e_iso,
            )
        except sqlite3.Error as exc:
            logger.error("Failed to execute Dashboard aggregate query: %s", exc)
            raise

    def get_booklets_processed(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> int:
        """Count successful logical booklet jobs in the given time period (AC-6.9.1).

        Uses `idx_job_history_status` or `idx_job_history_status_started_at`
        for covering index execution.

        Args:
            start_time: Optional ISO start timestamp.
            end_time: Optional ISO end timestamp.

        Returns:
            Count of rows with status='success'.
        """
        conditions = ["status = 'success'"]
        params: List[Any] = []

        if start_time is not None:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("started_at <= ?")
            params.append(end_time)

        where_clause = "WHERE " + " AND ".join(conditions)
        query = f"SELECT COUNT(*) FROM job_history {where_clause}"

        cursor = self._db_conn.execute(query, params)
        row = cursor.fetchone()
        return int(row[0] if row else 0)

    def get_error_rate(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> float:
        """Compute error rate as failed / (failed + success) (AC-6.9.2).

        Excludes in_progress and cancelled jobs from both numerator and denominator.

        Args:
            start_time: Optional ISO start timestamp.
            end_time: Optional ISO end timestamp.

        Returns:
            Float error rate in [0.0, 1.0].
        """
        conditions: List[str] = []
        params: List[Any] = []

        if start_time is not None:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("started_at <= ?")
            params.append(end_time)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT
                COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed_count,
                COUNT(CASE WHEN status IN ('success', 'failed') THEN 1 END) AS total_count
            FROM job_history
            {where_clause}
        """

        cursor = self._db_conn.execute(query, params)
        row = cursor.fetchone()
        if not row:
            return 0.0

        failed = int(row[0] or 0)
        total = int(row[1] or 0)
        return round(failed / total, 4) if total > 0 else 0.0

    def get_average_processing_time(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> float:
        """Compute average processing time (completed_at - started_at) for success jobs (AC-6.9.4).

        Excludes batch queue wait time by utilizing job_history.started_at
        (when the job's execution began).

        Args:
            start_time: Optional ISO start timestamp.
            end_time: Optional ISO end timestamp.

        Returns:
            Average duration in seconds, or 0.0 if no successful jobs exist.
        """
        conditions = ["status = 'success'", "completed_at IS NOT NULL"]
        params: List[Any] = []

        if start_time is not None:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("started_at <= ?")
            params.append(end_time)

        where_clause = "WHERE " + " AND ".join(conditions)

        # Uses idx_job_history_status or idx_job_history_status_started_at
        query = f"""
            SELECT COALESCE(ROUND(AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400.0
            ), 2), 0.0)
            FROM job_history
            {where_clause}
        """

        cursor = self._db_conn.execute(query, params)
        row = cursor.fetchone()
        return float(row[0] if row else 0.0)

    def get_recent_jobs(
        self,
        limit: int = 50,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent jobs for detailed inspection in the Dashboard UI.

        Args:
            limit: Maximum rows to return.
            status: Optional status filter.
            start_time: Optional start timestamp filter.
            end_time: Optional end timestamp filter.

        Returns:
            List of job records as dictionaries.
        """
        conditions: List[str] = []
        params: List[Any] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if start_time is not None:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("started_at <= ?")
            params.append(end_time)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM job_history {where_clause} ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        cursor = self._db_conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
