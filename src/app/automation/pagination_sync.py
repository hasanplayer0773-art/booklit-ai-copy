"""
Booklet_AI Desktop — Pagination Sync (STEP-2.1)

Connects the pricing engine's imposed_page_count() to the actual JSX
filler-page insertion call. This is the previously-missing wiring that
three independent reviewers confirmed was specified but never written.

The flow:
1. After all content injection completes, Python computes the imposed
   (rounded) target page count from the pre-layout estimate.
2. Python invokes the JSX addBlankFillerPages() via the COM bridge.
3. If actual content already exceeds the estimate (content overrun),
   the JSX raises ContentExceedsEstimateError — this module catches it,
   re-derives the target from the actual count, and re-quotes.

STEP-5.1 STUBS: BindingMethod and imposed_page_count() are forward-
declared here with minimal implementations. They will be replaced by
the full STEP-5.1 pricing engine module when that step is implemented.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Literal, Protocol

from app.core.exceptions import BookletError, ContentExceedsEstimateError

logger = logging.getLogger(__name__)


# ─── STEP-5.1 Pricing Engine Integration ──────────────────────────────
# Implemented in STEP-5.1: imports from app.core.pricing_models and
# app.core.pricing_engine, closing the loop with sync_filler_pages() [AC-5.1.6].

from app.core.pricing_models import BindingMethod
from app.core.pricing_engine import imposed_page_count


class COMBridge(Protocol):
    """Protocol for the COM bridge used to invoke JSX functions.

    This is a structural typing contract — any object with a
    call_jsx_function method matching this signature is accepted.
    The actual implementation is in COMSupervisor / the COM worker.
    """

    def call_jsx_function(self, function_name: str, *args: Any) -> Any:
        ...


def sync_filler_pages(
    com_bridge: COMBridge,
    doc_handle: Any,
    estimated_page_count: int,
    binding: BindingMethod,
) -> int:
    """Sync the InDesign document's page count to the binding requirement.

    Called once after the main content-injection loop completes and
    before export. Computes the imposed (rounded) target from the
    PRE-layout estimate, invokes the JSX filler-page function, and
    surfaces ContentExceedsEstimateError distinctly from a genuine
    filler-insertion bug so the caller can decide to re-quote rather
    than treat this as a fatal generation failure.

    Args:
        com_bridge: Object implementing the COMBridge protocol for
                    invoking JSX functions via InDesign COM.
        doc_handle: InDesign document handle (passed through to JSX).
        estimated_page_count: The pre-layout page count estimate from
                              the extraction/classification pipeline.
        binding: The binding method (determines signature multiple).

    Returns:
        The final synced page count on success. The caller is
        responsible for using this value to update STEP-5.1's
        pricing if it differs from the original estimate.

    Raises:
        ContentExceedsEstimateError: If actual content exceeds the
            estimate AND the re-derived target also fails (should
            not happen in practice — included for completeness).
        BookletError: If the JSX filler-page insertion itself fails
            for reasons other than content overrun.
    """
    target = imposed_page_count(estimated_page_count, binding)
    logger.info(
        "sync_filler_pages: estimated=%d, imposed_target=%d, binding=%s",
        estimated_page_count,
        target,
        binding,
    )

    try:
        com_bridge.call_jsx_function(
            "addBlankFillerPages",
            doc_handle,
            target,
            "Booklet_Master",
        )
        logger.info("sync_filler_pages: filler insertion complete, target=%d", target)
        return target

    except ContentExceedsEstimateError:
        # Content genuinely overran the pre-layout estimate — this is a
        # pricing staleness issue, not a filler-page defect.
        # Re-derive the target from the actual page count and re-quote.
        logger.warning(
            "sync_filler_pages: content exceeded estimate (%d pages). "
            "Re-deriving target from actual page count.",
            target,
        )
        actual_count: int = com_bridge.call_jsx_function(
            "getPageCount", doc_handle
        )
        new_target = imposed_page_count(actual_count, binding)
        logger.info(
            "sync_filler_pages: actual=%d, new_imposed_target=%d",
            actual_count,
            new_target,
        )

        com_bridge.call_jsx_function(
            "addBlankFillerPages",
            doc_handle,
            new_target,
            "Booklet_Master",
        )
        # Caller is responsible for re-quoting STEP-5.1's pricing
        # using new_target instead of the original estimate.
        return new_target
