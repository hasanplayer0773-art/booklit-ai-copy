"""
Booklet_AI Desktop — Auto-Recovery (STEP-6.2)

Narrowly-scoped auto-retry mechanism for transient/environmental errors.

Design principles:
- Explicit ALLOW-LIST, not a deny-list: only error classes confirmed to
  be transient/environmental are eligible for auto-retry. Defaulting to
  "retry unless listed as unsafe" would eventually auto-retry a class of
  error added later without anyone deciding that was safe.
- Membership in AUTO_RETRYABLE_EXCEPTIONS is NECESSARY but not SUFFICIENT
  for COMOperationTimeout — it is additionally gated by
  confirms_clean_retryable_state() at the moment of the retry decision.
- MAX_AUTO_RETRIES = 1: a single silent retry, never an unbounded loop.

Post-review corrections applied:
- SupervisorRetryState is a concrete, required contract — not an optional
  enrichment. COMSupervisor MUST attach one to every COMOperationTimeout.
- confirms_clean_retryable_state() is a plain, deterministic check over
  fields populated before raising — not a placeholder for future logic.
"""

import logging
from typing import Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import (
    COMOperationTimeout,
    MathExtractionError,
    TextIntegrityError,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# SupervisorRetryState — frozen snapshot of cleanup state
# ═══════════════════════════════════════════════════════════════════════


class SupervisorRetryState(BaseModel):
    """Frozen snapshot of COMSupervisor's cleanup state at exception time.

    COMSupervisor MUST construct and attach one of these to every
    COMOperationTimeout it raises (at the _terminate_and_cleanup()
    call site). This is a required contract, not an optional enrichment.

    The supervisor computes these facts during _terminate_and_cleanup()
    and previously discarded them after logging — this model captures
    them for the retry decision.
    """

    model_config = ConfigDict(frozen=True)

    child_process_exited: bool
    owned_indesign_pid: int  # 0 if this attempt never launched/owned an instance
    owned_indesign_confirmed_terminated: bool
    attached_to_pre_existing_instance: bool  # True → NEVER auto-retryable
    partial_output_path: Optional[str] = None  # path of partially-written artifact

    def confirms_clean_retryable_state(self) -> bool:
        """All conditions must hold for a retry to be permitted.

        This is a plain, deterministic check over fields COMSupervisor
        is required to populate before raising — not a placeholder for
        future logic.

        Returns:
            True only if the state is confirmed clean for retry.
        """
        # STEP-1.1: pre-existing instance is NEVER auto-retryable
        if self.attached_to_pre_existing_instance:
            return False
        # Child process must have exited
        if not self.child_process_exited:
            return False
        # If we owned an InDesign instance, it must be confirmed terminated
        if self.owned_indesign_pid != 0 and not self.owned_indesign_confirmed_terminated:
            return False
        # No partial output artifact from the failed attempt
        if self.partial_output_path is not None:
            return False
        return True


# ═══════════════════════════════════════════════════════════════════════
# Allow-list and deny-list
# ═══════════════════════════════════════════════════════════════════════

# Explicit allow-list — only these exceptions are eligible for auto-retry.
AUTO_RETRYABLE_EXCEPTIONS: Tuple[Type[Exception], ...] = (
    COMOperationTimeout,
    # Network/timeout-class exceptions from OpenRouterClient are NOT listed
    # here because STEP-1.4 already defines their handling: an AI-call
    # failure falls back to RuleBasedClassifier within the same call,
    # rather than being retried at this layer.
)

# Never auto-retried, regardless of future additions to the codebase:
# retrying implies "try the same thing again and hope it works" — for
# these, the same input will deterministically fail again, so a retry
# only delays the same Fail-Fast halt while masking the cause.
NEVER_AUTO_RETRY: Tuple[Type[Exception], ...] = (
    TextIntegrityError,   # hash mismatch will mismatch again
    MathExtractionError,  # missing/broken XSLT stylesheet doesn't fix itself
)

MAX_AUTO_RETRIES: int = 1  # a single silent retry; never an unbounded loop


# ═══════════════════════════════════════════════════════════════════════
# is_auto_retryable() — the single retry eligibility decision
# ═══════════════════════════════════════════════════════════════════════


def is_auto_retryable(
    exc: Exception,
    supervisor_state: Optional[SupervisorRetryState] = None,
) -> bool:
    """Determine whether an exception is eligible for auto-retry.

    COMOperationTimeout requires a SECOND, more specific condition beyond
    allow-list membership: the supervisor must be able to prove the prior
    attempt's state is fully clean before a retry is permitted. Retrying
    blind risks re-attaching to the same hung pre-existing InDesign
    instance or re-running generation over a partially-written output.

    Args:
        exc: The exception to evaluate.
        supervisor_state: The SupervisorRetryState attached to the
            exception by COMSupervisor. Required for COMOperationTimeout;
            if None, the retry is refused (fail-safe).

    Returns:
        True if the exception is eligible for a single auto-retry.
    """
    # Deny-list takes absolute precedence
    if isinstance(exc, NEVER_AUTO_RETRY):
        return False

    # Must be on the allow-list
    if not isinstance(exc, AUTO_RETRYABLE_EXCEPTIONS):
        return False

    # COMOperationTimeout has an additional gate
    if isinstance(exc, COMOperationTimeout):
        if supervisor_state is None:
            # Cannot prove clean state → refuse retry, fail safe
            return False
        return supervisor_state.confirms_clean_retryable_state()

    return True
