"""
Booklet_AI Desktop — Custom Exception Hierarchy

All application-specific exceptions derive from BookletError.
This module is the single source of truth for exception types
referenced across extraction, classification, and automation layers.
"""

from typing import Optional


class BookletError(Exception):
    """Base exception for Booklet_AI core.

    All domain-specific exceptions must inherit from this class
    to enable centralized error handling and translation (STEP-6.2).
    """


class TextIntegrityError(BookletError):
    """Raised when canonical text hash verification fails.

    Indicates an unauthorized mutation of source text content —
    the SHA-256 hash of the canonicalized text does not match
    the expected value. This triggers an immediate halt of the
    generation cycle (Fail-Fast policy).
    """


class MathExtractionError(BookletError):
    """Raised when OMML/MathML/LaTeX conversion or XSLT transformation fails.

    Covers failures in:
    - OMML → MathML conversion via XSLT (STEP-1.2)
    - MathType/OLE equation detection
    - Chemistry formula extraction
    """


class COMOperationTimeout(BookletError):
    """Raised when the COM worker process's heartbeat goes stale.

    Indicates the InDesign automation process has become unresponsive,
    likely due to a blocked modal dialog or hung InDesign instance.
    The supervisor will terminate the worker and owned InDesign process.
    """


class InDesignUnavailableError(BookletError):
    """Raised when InDesign is not available via COM.

    Checked at application startup (STEP-4.1) — if InDesign is not
    installed, not licensed, or not responding to COM activation,
    the application cannot perform its primary function.
    """


class InvalidBodyFrameGeometryError(BookletError):
    """Raised when computed body frame dimensions are invalid.

    Occurs when margin values overlap or produce a zero/negative
    text area — prevents passing impossible geometry to InDesign.
    """


class ContentExceedsEstimateError(BookletError):
    """Raised when actual generated content exceeds the pre-layout page-count estimate.

    This is a pricing/estimation staleness issue, NOT a filler-page
    insertion failure. The correct response is to recalculate the
    price quote against the actual page count via
    pricing_engine.imposed_page_count(actual_count, binding),
    not to silently truncate real content pages.

    Mirrored from the JSX side (addBlankFillerPages) — the Python
    pagination_sync module catches the JSX error and raises this
    Python exception with the same semantics.
    """


class MarginConfigError(BookletError):
    """Raised when a required margin field is missing or invalid in the JSX payload.

    Mirrors the JSX-side requireMarginField() validation — indicates
    a Python↔JSX serialization mismatch or an incomplete payload.
    The fix is to correct the mismatch, never to add a fallback default.
    """


class StyleMappingError(BookletError):
    """Raised when a BlockType has no mapped paragraph style.

    Every block type must have an explicit entry in the style map;
    silent fallback to a default style is not permitted (STEP-2.3).
    """


class StyleConfigError(BookletError):
    """Raised when a non-cosmetic style property (direction, composer, font) fails to apply.

    Plan STEP-1.1/2.3 forbids silent failure or silent degradation for
    paragraphDirection, composer, and appliedFont.
    """


class MissingFontError(BookletError):
    """Raised when a required font is not installed or available in InDesign.

    Plan STEP-1.1/2.3 forbids silent font substitution (e.g. falling back to Arial).
    """


class GlyphCoverageError(BookletError):
    """Raised when expected Greek/mathematical glyphs fail to receive styling in InDesign."""


class MasterNotFoundError(BookletError):
    """Raised when a required master spread cannot be found in the InDesign document.

    Replaces fragile itemByName lookup with baseName resolution.
    """


class RTLTableUnsupportedError(BookletError):
    """Raised when the InDesign installation does not support RTL tables.

    The InDesign build must support
    TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION (Middle-East /
    World-Ready composer build) for Arabic booklet table injection.

    An Arabic table rendered left-to-right (Column 0 on the left margin)
    is a critical layout defect — exactly the class of error this
    project's Fail-Fast policy exists to prevent. This exception is
    raised immediately at table injection time rather than allowing
    silent LTR degradation.

    Should ideally be detected once at application startup (STEP-4.1)
    and surfaced to the user clearly, rather than discovered per-table
    mid-generation.
    """


class QuestionIndexError(BookletError):
    """Raised on unrecoverable database write failure for the question bank."""


class PreflightBlockerError(BookletError):
    """Raised when export is attempted while preflight blockers exist without override (STEP-6.4)."""

    def __init__(self, blockers: list = None):
        self.blockers = blockers or []
        codes = [getattr(b, "code", str(b)) for b in self.blockers]
        super().__init__(
            f"Cannot proceed to export: {len(self.blockers)} unresolved "
            f"preflight blocker(s): {codes}"
        )


class MathRenderError(BookletError):
    """Raised when MathML rendering to SVG vector image fails (STEP-7.4)."""


class MathPlaceError(BookletError):
    """Raised when placing an SVG math formula image into InDesign fails (STEP-7.4)."""


class ImageAssetMissingError(BookletError):
    """Raised when an image asset referenced by a content block is missing on disk.

    Plan STEP-7.7a forbids silent skipping or placeholder substitution for missing
    image assets during payload construction.
    """

    def __init__(
        self,
        block_id_or_message: str = "",
        path: str = "",
        block_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        if path:
            self.block_id = block_id if block_id is not None else block_id_or_message
            self.path = str(path)
            msg = message or f"Image asset for block '{self.block_id}' missing on disk: {self.path}"
        else:
            self.block_id = block_id or ""
            self.path = path
            msg = message or block_id_or_message or "Image asset missing on disk"
        super().__init__(msg)


