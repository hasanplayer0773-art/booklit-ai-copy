"""
Booklet_AI Desktop — Job Retry Orchestration (STEP-6.2)

The previously-missing orchestration layer connecting is_auto_retryable()
to an actual retry loop.

Lives in the orchestration layer (the caller of COMSupervisor.run_job()
and of the extraction/classification pipeline) — never inside
COMSupervisor itself, and never in the UI layer.

Post-review correction: the prior draft retried the ENTIRE pipeline
(re-extraction, re-classification, re-generation) on every retryable
failure. This was flagged as a genuine defect: extraction and
classification are pure-Python stages that never touch COM, so a
COMOperationTimeout indicates a generation-stage failure, not an
ingestion-stage one. Blindly re-running extraction on a COM-only
failure discards every with_user_edit() mutation the operator made
before submitting the job, destroying real user work.

The corrected behavior retries ONLY the COM/generation step on a
COMOperationTimeout, reusing the already-extracted-and-classified
document state (including any user edits) exactly as it stood when
the job was submitted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.utils.auto_recovery import (
    MAX_AUTO_RETRIES,
    SupervisorRetryState,
    is_auto_retryable,
)

if TYPE_CHECKING:
    from app.state.document_store import DocumentStore

logger = logging.getLogger(__name__)


def _extract_and_classify(
    source_file_path: str,
    job_config: Any,
    document_store: Any,
) -> Any:
    """Extract content and classify blocks from the source file.

    Runs ONCE per job submission, never inside the retry loop.
    Returns the processed document that will be passed to the
    generation stage.

    This is a placeholder that will be connected to the full
    extraction (STEP-1.2/1.3) and AI classification (STEP-1.4)
    pipeline. For now, returns the document_store as-is, since
    extraction and classification are assumed to have been completed
    before reaching this orchestration function (the DocumentStore
    already contains the processed, classified document).

    Args:
        source_file_path: Path to the source Word/PDF file.
        job_config: Job configuration parameters.
        document_store: The document state container.

    Returns:
        The processed document (currently the document_store itself).
    """
    logger.info(
        "Extraction and classification for '%s' (single pass)",
        source_file_path,
    )
    return document_store


def _run_generation_once(
    document: Any,
    job_config: Any,
    attempt_index: int = 0,
) -> Any:
    """Run the COM/generation step once.

    This is a placeholder that will invoke COMSupervisor.run_job()
    with the appropriate COMJobRequest in the full implementation.
    The document parameter is the already-extracted-and-classified
    document state, reused across retry attempts.

    Args:
        document: The processed document state.
        job_config: Job configuration parameters.
        attempt_index: Zero-based attempt counter for logging.

    Returns:
        The generation result.

    Raises:
        COMOperationTimeout: If InDesign becomes unresponsive.
        NotImplementedError: In the current placeholder state.
    """
    raise NotImplementedError(
        "_run_generation_once must be connected to COMSupervisor.run_job() "
        "— this is a placeholder in the STEP-6.2 orchestration skeleton."
    )


def run_job_with_retry(
    source_file_path: str,
    job_config: Any,
    document_store: Any,
    *,
    generation_fn: Optional[Callable[..., Any]] = None,
    extraction_fn: Optional[Callable[..., Any]] = None,
) -> Any:
    """Orchestration-layer job execution with auto-retry.

    Extraction and classification run exactly ONCE per job submission,
    never inside the retry loop. Only the COM/generation step is
    retried on a COMOperationTimeout, reusing the already-extracted-
    and-classified DocumentStore state (including any user edits)
    exactly as it stood when the job was submitted.

    This is the only sanctioned entry point for auto-retry; no other
    caller may re-invoke COMSupervisor.run_job() or the extraction/
    classification pipeline directly in response to a caught exception.

    Args:
        source_file_path: Path to the source Word/PDF file.
        job_config: Job configuration parameters.
        document_store: The document state container (with any
            user edits already applied).
        generation_fn: Optional callable for the generation step.
            If None, uses the default _run_generation_once stub.
            Signature: (document, job_config, attempt_index) -> result.
        extraction_fn: Optional callable for the extraction step.
            If None, uses the default _extract_and_classify stub.
            Signature: (source_file_path, job_config, document_store)
            -> document.

    Returns:
        The generation result from the successful attempt.

    Raises:
        COMOperationTimeout: If retry is exhausted or not permitted.
        Any other exception: Propagated immediately (not retried).
    """
    _gen_fn = generation_fn or _run_generation_once
    _ext_fn = extraction_fn or _extract_and_classify

    attempt = 0
    last_exc: Optional[Exception] = None

    # Extraction and classification run exactly ONCE per job submission,
    # never inside the retry loop — retrying only ever re-invokes the
    # COM/generation step against this same, already-built document.
    document = _ext_fn(source_file_path, job_config, document_store)

    while attempt <= MAX_AUTO_RETRIES:
        try:
            return _gen_fn(document, job_config, attempt_index=attempt)
        except Exception as exc:
            last_exc = exc
            supervisor_state: Optional[SupervisorRetryState] = getattr(
                exc, "supervisor_state", None
            )
            if attempt >= MAX_AUTO_RETRIES or not is_auto_retryable(
                exc, supervisor_state
            ):
                raise
            attempt += 1
            logger.warning(
                "Auto-retrying generation after %s (attempt %d/%d)",
                type(exc).__name__,
                attempt,
                MAX_AUTO_RETRIES,
            )

    # Unreachable in practice; satisfies static analysis.
    assert last_exc is not None
    raise last_exc
