"""
Booklet_AI Desktop — Batch Processing Sequential Queue (STEP-6.7)

Orchestrates sequential batch execution of multiple booklet jobs.
Guarantees strict file-to-file isolation, token-budget chunking race closure,
traceable ministry question occurrence recording, and per-job state isolation.

Key Guarantees:
- Sequential Job Queue: Jobs are processed one at a time, never concurrently.
- No Context Merging: Content from two files never coexists in DocumentStore,
  classify_blocks(), or COMSupervisor.
- Audited Graceful Degradation: A failure in one job records status='failed' in
  job_history and does not halt the queue.
- Job-Scoped State Isolation: Temp dirs, caches, placeholder maps, counters,
  tokens, and document stores are freshly created per job (AC-6.7.5).
- Cancellation Support: UI can cancel the remaining queue between jobs without
  forcibly killing an in-flight job.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from PySide6.QtCore import QObject, QThread, Signal

from app.automation.job_retry import run_job_with_retry
from app.core.models import BookletDocument, ContentBlock
from app.state.document_store import DocumentStore
from app.state.job_history import JobHistoryManager, compute_source_file_sha256
from app.ui.error_translation import translate_error
from app.ui.problem_report import ProblemReport, build_problem_report
from app.utils.auto_recovery import SupervisorRetryState

logger = logging.getLogger("booklet_ai.batch_queue")


@dataclass
class JobScopedState:
    """Container for job-scoped resources requiring complete isolation per job.

    Guarantees no state leakage between sequential jobs in BatchQueue (AC-6.7.5).
    Enumerated state inventory from STEP-6.7 §2:
    1. temporary directory (temp_dir)
    2. prompt/response cache (cache)
    3. placeholder map (placeholder_map)
    4. classification confidence/fallback counters (fallback_counter)
    5. error/retry counters (retry_counter)
    6. output path (output_path)
    7. cancellation token (cancellation_token)
    8. document store with its own document revision (document_store)
    9. COMSupervisor child-process handle (com_supervisor)
    """

    temp_dir: Path
    cache: Dict[str, Any]
    placeholder_map: Dict[str, Any]
    fallback_counter: Dict[str, int]
    retry_counter: SupervisorRetryState
    output_path: Path
    cancellation_token: threading.Event
    document_store: DocumentStore
    com_supervisor: Any


def create_job_scoped_state(
    source_file_path: Union[str, Path],
    base_output_dir: Optional[Union[str, Path]] = None,
    com_supervisor_factory: Optional[Callable[[], Any]] = None,
) -> JobScopedState:
    """Factory creating freshly initialized job-scoped resources.

    Ensures every listed state object has a distinct object identity
    for job N+1 relative to job N (AC-6.7.5).
    """
    job_uuid = uuid.uuid4().hex[:8]
    temp_dir = Path(tempfile.mkdtemp(prefix=f"booklet_job_{job_uuid}_"))

    file_p = Path(source_file_path)
    stem = file_p.stem if file_p.name else "job"
    if base_output_dir is not None:
        out_dir = Path(base_output_dir)
    else:
        out_dir = temp_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{stem}_{job_uuid}.indd"

    com_handle = (
        com_supervisor_factory()
        if com_supervisor_factory is not None
        else f"com_handle_{job_uuid}"
    )

    return JobScopedState(
        temp_dir=temp_dir,
        cache={},
        placeholder_map={},
        fallback_counter={"fallback_count": 0, "total_calls": 0},
        retry_counter=SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
        ),
        output_path=output_path,
        cancellation_token=threading.Event(),
        document_store=DocumentStore(),
        com_supervisor=com_handle,
    )


def orchestrate_classification(
    document_store: DocumentStore,
    ai_client: Any,
    doc_id: Optional[str] = None,
) -> bool:
    """Orchestration-layer classification runner with document revision verification.

    Snapshots document_revision before calling classify_blocks.
    Re-validates that revision has not advanced under DocumentStore's lock
    before applying the merged result. If revision has advanced (e.g. user edit
    or undo/redo while classification in-flight), the merged result is discarded
    (AC-6.7.1c).

    Args:
        document_store: The DocumentStore instance.
        ai_client: The AI client (e.g. OpenRouterClient).
        doc_id: Optional document ID.

    Returns:
        True if classification was applied, False if discarded due to revision advance.
    """
    with document_store.lock:
        target_id = doc_id or document_store.active_doc_id
        doc = document_store.get_document(target_id)
        if doc is None:
            return False
        initial_revision = document_store.revision
        all_blocks = [b for page in doc.pages for b in page.content_blocks]

    # classify_blocks runs outside the lock across potential network requests
    classified = ai_client.classify_blocks(
        all_blocks, document_revision=initial_revision
    )

    # Re-validate revision under DocumentStore's lock before applying
    with document_store.lock:
        if document_store.revision != initial_revision:
            logger.warning(
                "Document revision advanced (was %d, now %d). Discarding merged classification result.",
                initial_revision,
                document_store.revision,
            )
            return False

        return document_store.apply_classified_blocks(
            classified, expected_revision=initial_revision, doc_id=target_id
        )


@dataclass
class BatchJobResult:
    """Immutable result summary for an individual queued job."""

    file_path: str
    job_id: Optional[int] = None
    status: str = "pending"  # pending, in_progress, success, failed, cancelled
    error_summary: Optional[str] = None
    exception: Optional[Exception] = None
    problem_report: Optional[ProblemReport] = None
    page_count: Optional[int] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class BatchQueueSummary:
    """Overall summary of a batch processing queue run."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    results: List[BatchJobResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize summary to primitive dict for Qt Signals (AC-1.1.4)."""
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "results": [
                {
                    "file_path": r.file_path,
                    "job_id": r.job_id,
                    "status": r.status,
                    "error_summary": r.error_summary,
                    "page_count": r.page_count,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "has_problem_report": r.problem_report is not None,
                }
                for r in self.results
            ],
        }


class BatchQueue(QThread):
    """Batch Processing Sequential Queue Worker.

    Runs on a dedicated worker thread (QThread) off the Qt main thread.
    Processes pending file paths one at a time via run_job_with_retry().
    Emits Qt Signals with primitive data types for live UI progress updates.
    """

    job_started = Signal(str, int, int)       # (file_path, current_index_1_based, total_jobs)
    job_progress = Signal(str, float, str)    # (file_path, fraction_0_to_1, step_description)
    job_succeeded = Signal(str, int)          # (file_path, job_id)
    job_failed = Signal(str, str, int)        # (file_path, error_summary, job_id)
    queue_progress = Signal(int, int)         # (completed_count, total_jobs)
    queue_finished = Signal(dict)             # summary dictionary
    queue_cancelled = Signal(int, int)        # (completed_count, remaining_count)

    def __init__(
        self,
        file_paths: List[str],
        db_conn: sqlite3.Connection,
        job_config: Optional[Any] = None,
        generation_fn: Optional[Callable[..., Any]] = None,
        extraction_fn: Optional[Callable[..., Any]] = None,
        state_factory: Optional[Callable[[str], JobScopedState]] = None,
        parent: Optional[QObject] = None,
        job_configs: Optional[Union[Dict[str, Any], Callable[[str], Any]]] = None,
    ) -> None:
        """Initialize BatchQueue.

        Args:
            file_paths: Ordered list of source file paths to process.
            db_conn: SQLite database connection for job history and question indexer.
            job_config: Optional default job configuration parameters.
            generation_fn: Optional generation callable for run_job_with_retry.
            extraction_fn: Optional extraction callable for run_job_with_retry.
            state_factory: Optional factory for JobScopedState.
            parent: Optional Qt parent QObject.
            job_configs: Optional per-file configuration mapping or callable (AC-6.8.3).
        """
        super().__init__(parent)
        self._file_paths = list(file_paths)
        self._db_conn = db_conn
        self._job_history = JobHistoryManager(db_conn)
        self._job_config = job_config
        self._job_configs = job_configs
        self._generation_fn = generation_fn
        self._extraction_fn = extraction_fn
        self._state_factory = state_factory or create_job_scoped_state
        self._cancel_requested = False
        self._summary = BatchQueueSummary(total=len(self._file_paths))

    def _get_config_for_file(self, file_path: str) -> Any:
        """Resolve per-document configuration for a queued file (AC-6.8.3)."""
        if self._job_configs is not None:
            if callable(self._job_configs):
                return self._job_configs(file_path)
            if isinstance(self._job_configs, dict) and file_path in self._job_configs:
                return self._job_configs[file_path]
        return self._job_config

    def cancel_queue(self) -> None:
        """Request queue cancellation between jobs.

        Does not forcibly abort an in-flight job, but refuses to start the next one.
        """
        self._cancel_requested = True
        logger.info("BatchQueue cancellation requested.")

    def get_summary(self) -> BatchQueueSummary:
        """Return the current queue summary."""
        return self._summary

    def run(self) -> None:
        """Execute the sequential queue on the worker thread."""
        total_jobs = len(self._file_paths)
        logger.info("BatchQueue started with %d queued files.", total_jobs)

        for idx, file_path in enumerate(self._file_paths):
            if self._cancel_requested:
                remaining = self._file_paths[idx:]
                logger.info(
                    "BatchQueue cancelled between jobs. Skipping %d remaining files.",
                    len(remaining),
                )
                for rem_path in remaining:
                    self._summary.cancelled += 1
                    self._summary.results.append(
                        BatchJobResult(file_path=rem_path, status="cancelled")
                    )
                self.queue_cancelled.emit(idx, len(remaining))
                break

            # 1. Fresh job-scoped state isolation for each file (AC-6.7.5)
            job_state = self._state_factory(file_path)

            # 2. Source file hash computation
            p = Path(file_path)
            if p.exists() and p.is_file():
                file_sha = compute_source_file_sha256(p)
            else:
                file_sha = hashlib.sha256(file_path.encode("utf-8")).hexdigest()

            # 3. Record job start in job_history (STEP-6.3)
            job_id = self._job_history.start_job(
                source_file_path=file_path,
                source_file_sha256=file_sha,
            )
            result_item = BatchJobResult(
                file_path=file_path,
                job_id=job_id,
                status="in_progress",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self.job_started.emit(file_path, idx + 1, total_jobs)

            try:
                # 4. Resolve per-document config and execute through run_job_with_retry() (AC-6.8.3)
                file_config = self._get_config_for_file(file_path)
                gen_result = run_job_with_retry(
                    source_file_path=file_path,
                    job_config=file_config,
                    document_store=job_state.document_store,
                    generation_fn=self._generation_fn,
                    extraction_fn=self._extraction_fn,
                )

                page_count = (
                    getattr(gen_result, "page_count", None)
                    if gen_result is not None
                    else None
                )
                self._job_history.complete_job(job_id=job_id, page_count=page_count)
                result_item.status = "success"
                result_item.page_count = page_count
                result_item.completed_at = datetime.now(timezone.utc).isoformat()
                self._summary.succeeded += 1
                self.job_succeeded.emit(file_path, job_id)

            except Exception as exc:
                # 5. Audited Graceful Degradation: Record failure, do not halt queue (AC-6.7.2)
                translated = translate_error(exc)
                self._job_history.fail_job(job_id=job_id, error=exc)

                report = build_problem_report(
                    exc=exc,
                    app_version="1.0.0",
                    correlation_id=str(uuid.uuid4()),
                    recent_log_lines=[f"Batch processing error for {file_path}: {exc}"],
                )

                result_item.status = "failed"
                result_item.error_summary = translated.translated_message
                result_item.exception = exc
                result_item.problem_report = report
                result_item.completed_at = datetime.now(timezone.utc).isoformat()
                self._summary.failed += 1
                self.job_failed.emit(file_path, translated.translated_message, job_id)

            self._summary.results.append(result_item)
            self.queue_progress.emit(len(self._summary.results), total_jobs)

        # 6. Emit overall queue completion summary
        logger.info(
            "BatchQueue finished: %d succeeded, %d failed, %d cancelled (total: %d)",
            self._summary.succeeded,
            self._summary.failed,
            self._summary.cancelled,
            self._summary.total,
        )
        self.queue_finished.emit(self._summary.to_dict())
