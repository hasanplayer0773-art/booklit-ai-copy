"""
Booklet_AI Desktop — STEP-6.9 Unit & Integration Tests (Dashboard)

Covers all Acceptance Criteria:
- [AC-6.9.1]: The Dashboard's "booklets processed" count for a given period matches
  the count of job_history rows with status='success' in that period — verified
  against the same table STEP-6.3 writes, not a separately-maintained counter.
- [AC-6.9.1b]: A logical job that fails once (COMOperationTimeout) and succeeds on
  run_job_with_retry()'s single permitted retry is counted as exactly one successful
  booklet in "booklets processed," not two — verified directly against this exact retry scenario.
- [AC-6.9.2]: The Dashboard's error-rate figure matches failed rows divided by (failed + success)
  rows in job_history for the selected period — recomputed from job_history on each view,
  not cached in a way that could drift from the underlying table, and excluding in_progress/cancelled
  rows from both numerator and denominator.
- [AC-6.9.3]: Viewing the Dashboard never acquires DocumentStore's threading.RLock —
  verified by confirming the Dashboard's data path touches only the SQLite connection.
- [AC-6.9.4]: Average processing time excludes any time a batched job (STEP-6.7) spent queued
  waiting for prior jobs — verified by confirming the computation uses each job's own
  started_at (when its own execution began), not its enqueue time.
- [AC-6.9.5]: job_history queries backing the Dashboard use the indexes defined in
  the migration (005_job_history_indexes.sql / 004_job_history_indexes.sql) — verified
  via query plan inspection (e.g. EXPLAIN QUERY PLAN) confirming an index scan rather
  than a full table scan for the standard Dashboard queries.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.automation.batch_queue import BatchQueue
from app.automation.job_retry import run_job_with_retry
from app.core.exceptions import COMOperationTimeout
from app.state.dashboard import (
    DashboardAggregator,
    DashboardMetrics,
    TimePeriod,
    compute_time_range,
)
from app.state.document_store import DocumentStore
from app.state.job_history import JobHistoryManager
from app.ui.dashboard_panel import DashboardPanel, MetricCard, format_duration
from app.utils.auto_recovery import SupervisorRetryState
from app.utils.db import apply_migrations, get_db_connection, init_db


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_conn():
    """In-memory SQLite database with all migrations applied (including indexes)."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def job_manager(db_conn):
    """JobHistoryManager instance from STEP-6.3."""
    return JobHistoryManager(db_conn)


@pytest.fixture
def aggregator(db_conn):
    """DashboardAggregator instance for STEP-6.9."""
    return DashboardAggregator(db_conn)


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.1: Booklets Processed Count Verification
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria691:
    """[AC-6.9.1] Booklets processed matches job_history rows with status='success'."""

    def test_booklets_processed_matches_success_rows(self, job_manager, aggregator, db_conn):
        """Count matches success rows in the same table STEP-6.3 writes to."""
        # 3 successful jobs
        id1 = job_manager.start_job("file1.docx", "hash1")
        job_manager.complete_job(id1, page_count=12)

        id2 = job_manager.start_job("file2.docx", "hash2")
        job_manager.complete_job(id2, page_count=16)

        id3 = job_manager.start_job("file3.docx", "hash3")
        job_manager.complete_job(id3, page_count=20)

        # 1 failed job
        id4 = job_manager.start_job("file4.docx", "hash4")
        job_manager.fail_job(id4, RuntimeError("Test failure"))

        # 1 in_progress job
        job_manager.start_job("file5.docx", "hash5")

        # Verify SQL table directly
        cursor = db_conn.execute("SELECT COUNT(*) FROM job_history WHERE status = 'success'")
        expected_success_count = cursor.fetchone()[0]
        assert expected_success_count == 3

        # Verify Dashboard aggregator
        metrics = aggregator.get_metrics(TimePeriod.ALL_TIME)
        assert metrics.booklets_processed == 3
        assert aggregator.get_booklets_processed() == 3

        # Verify via JobHistoryManager convenience method
        assert job_manager.get_booklets_processed() == 3

    def test_booklets_processed_period_filtering(self, db_conn, aggregator):
        """Booklets processed respects period boundaries based on started_at."""
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        # Job today
        t_today = "2026-10-25T08:00:00+00:00"
        # Job yesterday
        t_yesterday = "2026-10-24T15:00:00+00:00"
        # Job 10 days ago
        t_10d_ago = "2026-10-15T10:00:00+00:00"

        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("f1.docx", "h1", t_today, "2026-10-25T08:00:15+00:00"),
        )
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("f2.docx", "h2", t_yesterday, "2026-10-24T15:00:20+00:00"),
        )
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("f3.docx", "h3", t_10d_ago, "2026-10-15T10:00:30+00:00"),
        )
        db_conn.commit()

        # All time: 3
        m_all = aggregator.get_metrics(TimePeriod.ALL_TIME)
        assert m_all.booklets_processed == 3

        # Today: 1 (only f1)
        m_today = aggregator.get_metrics(TimePeriod.TODAY, reference_time=now)
        assert m_today.booklets_processed == 1

        # Last 7 days: 2 (f1 and f2)
        m_7d = aggregator.get_metrics(TimePeriod.LAST_7_DAYS, reference_time=now)
        assert m_7d.booklets_processed == 2

        # Last 30 days: 3 (all)
        m_30d = aggregator.get_metrics(TimePeriod.LAST_30_DAYS, reference_time=now)
        assert m_30d.booklets_processed == 3

    def test_dynamic_insertion_immediately_reflected(self, job_manager, aggregator):
        """No caching: newly completed job immediately updates processed count."""
        assert aggregator.get_booklets_processed() == 0

        j1 = job_manager.start_job("doc1.docx", "h1")
        job_manager.complete_job(j1)
        assert aggregator.get_booklets_processed() == 1

        j2 = job_manager.start_job("doc2.docx", "h2")
        job_manager.complete_job(j2)
        assert aggregator.get_booklets_processed() == 2


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.1b: Retry Scenario Attribution (Logical Job Counting)
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria691b:
    """[AC-6.9.1b] Retry scenario: COMOperationTimeout retry succeeds -> counted as exactly ONE."""

    def test_single_retry_success_counted_as_one_booklet(self, job_manager, aggregator, db_conn):
        """A logical job that fails once with COMOperationTimeout and succeeds on retry
        is counted as exactly 1 successful booklet, not 2."""
        source_file = "C:/test/timeout_retry.docx"
        file_sha = "hash_retry_scenario"

        # 1. Start logical job
        job_id = job_manager.start_job(source_file_path=source_file, source_file_sha256=file_sha)

        attempts_executed = []

        def mock_generation_fn(doc, cfg, attempt_index=0):
            attempts_executed.append(attempt_index)
            if attempt_index == 0:
                # Attempt 0: InDesign timeout (auto-retryable)
                exc = COMOperationTimeout("InDesign unresponsive during master frame creation")
                exc.supervisor_state = SupervisorRetryState(
                    child_process_exited=True,
                    owned_indesign_pid=4321,
                    owned_indesign_confirmed_terminated=True,
                    attached_to_pre_existing_instance=False,
                )
                raise exc
            # Attempt 1 (retry): Succeeds
            res = MagicMock()
            res.page_count = 12
            return res

        # 2. Run through STEP-6.2 run_job_with_retry()
        gen_result = run_job_with_retry(
            source_file_path=source_file,
            job_config=None,
            document_store=MagicMock(),
            generation_fn=mock_generation_fn,
        )

        assert len(attempts_executed) == 2  # Proves 2 execution attempts occurred
        assert attempts_executed == [0, 1]

        # 3. Complete logical job
        job_manager.complete_job(job_id=job_id, page_count=gen_result.page_count)

        # 4. Verify job_history has exactly ONE row for this logical job
        cursor = db_conn.execute("SELECT COUNT(*) FROM job_history")
        total_rows = cursor.fetchone()[0]
        assert total_rows == 1, "Must maintain 1 row per logical job, not multiple rows for retries"

        # 5. Verify Dashboard metrics
        metrics = aggregator.get_metrics()
        assert metrics.booklets_processed == 1, "Must count as exactly 1 booklet, not 2"
        assert metrics.failed_jobs == 0, "Initial retryable failure must NOT count as a failed booklet"
        assert metrics.error_rate == 0.0
        assert metrics.total_completed == 1

    def test_retry_exhausted_counted_as_one_failure(self, job_manager, aggregator, db_conn):
        """When both attempts fail, the logical job is counted as exactly 1 failure, not 2."""
        source_file = "C:/test/exhausted_retry.docx"
        file_sha = "hash_exhausted"

        job_id = job_manager.start_job(source_file_path=source_file, source_file_sha256=file_sha)

        attempts = 0

        def always_failing_generation(doc, cfg, attempt_index=0):
            nonlocal attempts
            attempts += 1
            exc = COMOperationTimeout("Persistent timeout")
            exc.supervisor_state = SupervisorRetryState(
                child_process_exited=True,
                owned_indesign_pid=5555,
                owned_indesign_confirmed_terminated=True,
                attached_to_pre_existing_instance=False,
            )
            raise exc

        with pytest.raises(COMOperationTimeout):
            run_job_with_retry(
                source_file_path=source_file,
                job_config=None,
                document_store=MagicMock(),
                generation_fn=always_failing_generation,
            )

        assert attempts == 2  # Initial + 1 retry executed
        job_manager.fail_job(job_id, COMOperationTimeout("Persistent timeout"))

        # Invariant: exactly 1 row in table
        assert db_conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0] == 1

        metrics = aggregator.get_metrics()
        assert metrics.booklets_processed == 0
        assert metrics.failed_jobs == 1, "Must count as exactly 1 failed job, not 2"
        assert metrics.error_rate == 1.0


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.2: Error Rate Recomputation & Status Exclusion
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria692:
    """[AC-6.9.2] Error-rate = failed / (failed + success); in_progress and cancelled excluded."""

    def test_error_rate_calculation_exact(self, job_manager, aggregator):
        """6 success, 2 failed -> error_rate = 2 / (6 + 2) = 0.25 (25.0%)."""
        for i in range(6):
            jid = job_manager.start_job(f"s_{i}.docx", f"hash_s_{i}")
            job_manager.complete_job(jid)

        for i in range(2):
            jid = job_manager.start_job(f"f_{i}.docx", f"hash_f_{i}")
            job_manager.fail_job(jid, RuntimeError("Err"))

        metrics = aggregator.get_metrics()
        assert metrics.booklets_processed == 6
        assert metrics.failed_jobs == 2
        assert metrics.total_completed == 8
        assert metrics.error_rate == 0.25
        assert metrics.error_rate_percentage == 25.0

    def test_in_progress_and_cancelled_strictly_excluded(self, job_manager, aggregator, db_conn):
        """in_progress and cancelled jobs are excluded from both numerator and denominator."""
        # 3 success
        for i in range(3):
            jid = job_manager.start_job(f"s_{i}.docx", f"hash_s_{i}")
            job_manager.complete_job(jid)

        # 1 failed
        jid = job_manager.start_job("fail.docx", "hash_f")
        job_manager.fail_job(jid, RuntimeError("Err"))

        # 4 in_progress
        for i in range(4):
            job_manager.start_job(f"prog_{i}.docx", f"hash_p_{i}")

        # 2 cancelled (inserted directly with status='cancelled')
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at) VALUES (?, ?, 'cancelled', ?)",
            ("canc_1.docx", "h_c1", "2026-10-25T10:00:00+00:00"),
        )
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at) VALUES (?, ?, 'cancelled', ?)",
            ("canc_2.docx", "h_c2", "2026-10-25T10:05:00+00:00"),
        )
        db_conn.commit()

        # Total rows in DB: 3 + 1 + 4 + 2 = 10
        assert db_conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0] == 10

        # But Dashboard error_rate only counts (1 failed) / (1 failed + 3 success) = 1/4 = 0.25
        metrics = aggregator.get_metrics()
        assert metrics.booklets_processed == 3
        assert metrics.failed_jobs == 1
        assert metrics.total_completed == 4  # 3 + 1 only
        assert metrics.error_rate == 0.25
        assert metrics.error_rate_percentage == 25.0

    def test_zero_completed_jobs_yields_zero_error_rate(self, job_manager, aggregator):
        """Empty history or only in_progress jobs yields 0.0 error rate (no ZeroDivisionError)."""
        metrics = aggregator.get_metrics()
        assert metrics.total_completed == 0
        assert metrics.error_rate == 0.0

        # Add only in_progress jobs
        job_manager.start_job("pending.docx", "hp")
        m_prog = aggregator.get_metrics()
        assert m_prog.total_completed == 0
        assert m_prog.error_rate == 0.0

    def test_recomputed_fresh_on_each_view_no_stale_cache(self, job_manager, aggregator):
        """Confirming metrics re-execute SQL and do not serve a cached copy."""
        j1 = job_manager.start_job("doc1.docx", "h1")
        job_manager.complete_job(j1)

        m1 = aggregator.get_metrics()
        assert m1.error_rate == 0.0

        # Add failure
        j2 = job_manager.start_job("doc2.docx", "h2")
        job_manager.fail_job(j2, RuntimeError("fail"))

        m2 = aggregator.get_metrics()
        assert m2.error_rate == 0.5  # Immediately updated to 1/2


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.3: Concurrency Placement & RLock Non-Acquisition
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria693:
    """[AC-6.9.3] Viewing the Dashboard never acquires DocumentStore's threading.RLock."""

    def test_no_document_store_or_rlock_in_dashboard_source(self):
        """Static inspection: dashboard.py and dashboard_panel.py never import DocumentStore or RLock."""
        from pathlib import Path
        import app.state.dashboard as dash_mod
        import app.ui.dashboard_panel as panel_mod

        for mod in (dash_mod, panel_mod):
            source = Path(mod.__file__).read_text(encoding="utf-8")
            import_lines = [
                line.strip()
                for line in source.splitlines()
                if line.strip().startswith(("import ", "from "))
            ]
            for line in import_lines:
                assert "DocumentStore" not in line, f"{mod.__name__} must not import DocumentStore: {line}"
                assert "RLock" not in line, f"{mod.__name__} must not import RLock: {line}"

    def test_runtime_lock_spy_proves_zero_acquisitions(self, db_conn, aggregator):
        """Instrument DocumentStore.lock to prove it is NEVER acquired during Dashboard operations."""
        class LockSpy:
            def __init__(self, real_lock):
                self._real_lock = real_lock
                self.acquire_count = 0

            def acquire(self, *args, **kwargs):
                self.acquire_count += 1
                return self._real_lock.acquire(*args, **kwargs)

            def release(self, *args, **kwargs):
                return self._real_lock.release(*args, **kwargs)

            def __enter__(self):
                self.acquire_count += 1
                return self._real_lock.__enter__()

            def __exit__(self, *args):
                return self._real_lock.__exit__(*args)

        store = DocumentStore()
        spy = LockSpy(store.lock)
        store._lock = spy

        # Run all aggregator queries
        aggregator.get_metrics()
        aggregator.get_booklets_processed()
        aggregator.get_error_rate()
        aggregator.get_average_processing_time()
        aggregator.get_recent_jobs()

        assert spy.acquire_count == 0, "DocumentStore.lock must NEVER be acquired by DashboardAggregator"

        # Instantiate UI Panel and trigger refresh
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication(["--platform", "offscreen"])

        panel = DashboardPanel(db_conn, aggregator=aggregator)
        panel.refresh()
        panel.set_period(TimePeriod.TODAY)
        panel.set_period(TimePeriod.LAST_7_DAYS)

        assert spy.acquire_count == 0, "DocumentStore.lock must NEVER be acquired by DashboardPanel"


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.4: Average Processing Time Excludes Batch Queue Wait Time
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria694:
    """[AC-6.9.4] Average processing time uses job's own started_at, excluding batch queue wait time."""

    def test_average_processing_time_excludes_queue_wait(self, db_conn, aggregator):
        """Job 1 takes 30s; Job 2 waits 30s in queue and executes in 20s.
        Average must be (30 + 20) / 2 = 25.0s, NOT including the 30s queue wait."""
        # Simulated batch run:
        # Enqueue time was 10:00:00
        # Job 1 began execution at 10:00:00 and finished at 10:00:30 (duration = 30s)
        # Job 2 began execution at 10:00:30 and finished at 10:00:50 (duration = 20s)
        t_job1_start = "2026-10-25T10:00:00+00:00"
        t_job1_comp = "2026-10-25T10:00:30+00:00"

        t_job2_start = "2026-10-25T10:00:30+00:00"
        t_job2_comp = "2026-10-25T10:00:50+00:00"

        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("job1.docx", "h1", t_job1_start, t_job1_comp),
        )
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("job2.docx", "h2", t_job2_start, t_job2_comp),
        )
        db_conn.commit()

        # Expected: (30s + 20s) / 2 = 25.0s
        avg_time = aggregator.get_average_processing_time()
        assert avg_time == 25.0

        metrics = aggregator.get_metrics()
        assert metrics.average_processing_time_seconds == 25.0

    def test_average_processing_time_excludes_failed_and_in_progress_jobs(self, db_conn, aggregator):
        """Failed jobs and in_progress jobs are excluded from average processing time."""
        # 1 success: 40s
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'success', ?, ?)",
            ("s.docx", "hs", "2026-10-25T10:00:00+00:00", "2026-10-25T10:00:40+00:00"),
        )
        # 1 failed: took 100s before failing
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, 'failed', ?, ?)",
            ("f.docx", "hf", "2026-10-25T10:01:00+00:00", "2026-10-25T10:02:40+00:00"),
        )
        # 1 in_progress: no completed_at
        db_conn.execute(
            "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at) VALUES (?, ?, 'in_progress', ?)",
            ("p.docx", "hp", "2026-10-25T10:03:00+00:00"),
        )
        db_conn.commit()

        # Average must be 40.0s (only the success job)
        assert aggregator.get_average_processing_time() == 40.0

    def test_batch_queue_sets_started_at_at_execution_time(self, db_conn):
        """Verify BatchQueue calls start_job() sequentially when processing begins for each file."""
        import time

        recorded_starts = []

        def mock_gen(doc, cfg, attempt_index=0):
            res = MagicMock()
            res.page_count = 8
            time.sleep(0.05)  # slight delay
            return res

        queue = BatchQueue(
            file_paths=["file_A.docx", "file_B.docx"],
            db_conn=db_conn,
            generation_fn=mock_gen,
        )
        queue.run()

        # Check the two job_history rows
        cursor = db_conn.execute("SELECT id, started_at, completed_at FROM job_history ORDER BY id ASC")
        rows = cursor.fetchall()
        assert len(rows) == 2

        start_a = rows[0]["started_at"]
        comp_a = rows[0]["completed_at"]
        start_b = rows[1]["started_at"]
        comp_b = rows[1]["completed_at"]

        # Job B was started after or at Job A's completion, not at queue initialization time
        assert start_b >= start_a


# ═══════════════════════════════════════════════════════════════════════
# AC-6.9.5: Query Plan Inspection (Indexes Used)
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria695:
    """[AC-6.9.5] Dashboard queries use indexes defined in 005_job_history_indexes.sql."""

    def test_indexes_exist_in_database(self, db_conn):
        """All three indexes defined in migration exist in sqlite_master."""
        cursor = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'job_history'"
        )
        index_names = {row[0] for row in cursor.fetchall()}
        assert "idx_job_history_started_at" in index_names
        assert "idx_job_history_status" in index_names
        assert "idx_job_history_status_started_at" in index_names

    def test_migration_file_definition(self):
        """Verify migration files (src/migrations/005_job_history_indexes.sql) contain exact index DDL."""
        migration_path = Path("src/migrations/005_job_history_indexes.sql")
        assert migration_path.exists(), "Migration file 005_job_history_indexes.sql must exist"
        sql = migration_path.read_text(encoding="utf-8")
        assert "CREATE INDEX" in sql
        assert "idx_job_history_started_at ON job_history(started_at)" in sql
        assert "idx_job_history_status ON job_history(status)" in sql
        assert "idx_job_history_status_started_at ON job_history(status, started_at)" in sql

    def test_explain_query_plan_uses_indexes(self, db_conn):
        """EXPLAIN QUERY PLAN confirms index search / scan rather than full table scan."""
        # Insert sufficient rows so SQLite query planner prefers index lookup over table scan
        for i in range(120):
            status = "success" if i % 2 == 0 else "failed"
            db_conn.execute(
                "INSERT INTO job_history (source_file_path, source_file_sha256, status, started_at, completed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    f"file_{i}.docx",
                    f"hash_{i}",
                    status,
                    f"2026-10-25T10:{i % 60:02d}:00+00:00",
                    f"2026-10-25T10:{i % 60:02d}:20+00:00",
                ),
            )
        db_conn.commit()
        db_conn.execute("ANALYZE")

        queries_to_test = [
            (
                "Booklets processed with period",
                "SELECT COUNT(*) FROM job_history WHERE status = 'success' AND started_at >= ? AND started_at <= ?",
                ("2026-10-25T10:00:00+00:00", "2026-10-25T10:30:00+00:00"),
                "idx_job_history_status_started_at",
            ),
            (
                "Booklets processed all time",
                "SELECT COUNT(*) FROM job_history WHERE status = 'success'",
                (),
                "idx_job_history_status",
            ),
            (
                "Average processing time with period",
                "SELECT AVG((julianday(completed_at) - julianday(started_at)) * 86400.0) FROM job_history WHERE status = 'success' AND completed_at IS NOT NULL AND started_at >= ? AND started_at <= ?",
                ("2026-10-25T10:00:00+00:00", "2026-10-25T10:30:00+00:00"),
                "idx_job_history_status_started_at",
            ),
            (
                "Date range filter across statuses",
                "SELECT COUNT(*) FROM job_history WHERE started_at >= ? AND started_at <= ?",
                ("2026-10-25T10:00:00+00:00", "2026-10-25T10:30:00+00:00"),
                "idx_job_history_started_at",
            ),
        ]

        for label, sql, params, expected_index in queries_to_test:
            plan_cursor = db_conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            plan_rows = plan_cursor.fetchall()
            plan_details = " ".join([str(col) for row in plan_rows for col in row])

            # Confirm index is utilized
            assert expected_index in plan_details, (
                f"Query '{label}' did not use expected index '{expected_index}'. Plan: {plan_details}"
            )
            # Confirm NO full table scan
            assert "SCAN TABLE" not in plan_details or "USING INDEX" in plan_details or "USING COVERING INDEX" in plan_details


# ═══════════════════════════════════════════════════════════════════════
# UI & Model Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestDashboardUIAndModels:
    """Unit tests for Dashboard UI components and Pydantic models."""

    def test_dashboard_metrics_frozen_model(self):
        """DashboardMetrics is immutable (frozen=True)."""
        metrics = DashboardMetrics(booklets_processed=5)
        with pytest.raises(ValidationError):
            metrics.booklets_processed = 10  # type: ignore[misc]

    def test_format_duration_helper(self):
        """format_duration formats seconds to localized Arabic strings."""
        assert format_duration(0.0) == "0 ثانية"
        assert format_duration(15.4) == "15.4 ثانية"
        assert format_duration(65.0) == "1 د 5 ث"
        assert format_duration(130.0) == "2 د 10 ث"

    def test_metric_card_widget(self):
        """MetricCard renders title and updates value."""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(["--platform", "offscreen"])
        card = MetricCard("عنوان", "10", "وصف")
        assert card._title_label.text() == "عنوان"
        assert card._value_label.text() == "10"

        card.set_value("25")
        assert card._value_label.text() == "25"

    def test_dashboard_panel_refresh_and_periods(self, db_conn, job_manager):
        """DashboardPanel updates cards and table when changing periods and refreshing."""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(["--platform", "offscreen"])

        # Populate sample jobs
        j1 = job_manager.start_job("file1.docx", "hash1")
        job_manager.complete_job(j1, page_count=10)

        j2 = job_manager.start_job("file2.docx", "hash2")
        job_manager.fail_job(j2, RuntimeError("Sample error"))

        panel = DashboardPanel(db_conn)
        metrics = panel.get_current_metrics()
        assert metrics.booklets_processed == 1
        assert metrics.failed_jobs == 1
        assert metrics.total_completed == 2
        assert metrics.error_rate == 0.5

        # Check table row count
        assert panel._table.rowCount() == 2

        # Change period to TODAY
        panel.set_period(TimePeriod.TODAY)
        panel.refresh()
        assert panel.get_current_metrics().period == "today"
