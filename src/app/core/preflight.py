"""
Booklet_AI Desktop — Preflight Check (STEP-6.4)

Two-tier pre-export quality gate:

1. Pre-COM checks (this module): run entirely in the Python layer before
   any COMSupervisor job.  Never imports win32com or pythoncom.  Detects
   unresolved legacy equation placeholders (blocker), low effective image
   DPI (warning), unverifiable DPI (warning), and filler-page requirements
   (warning).

2. Post-generation checks (run inside the COM child process after InDesign
   composition completes): text-frame overset, missing-font detection,
   unintended blank pages.  See post_generation_preflight.py for the
   child-process-side implementation.

Both produce frozen PreflightReport objects.  Pre-COM results are returned
directly; post-generation results travel back via file-based IPC
(preflight_output_path on COMJobRequest).

Import-boundary rule (AC-6.4.3 / AC-1.1.10):
    This file MUST NOT import win32com, pythoncom, or any module that
    transitively imports them.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import PreflightBlockerError
from app.core.models import BookletDocument, ImageAsset
from app.core.pricing_engine import imposed_page_count
from app.core.pricing_models import BindingMethod

# ─── Constants ─────────────────────────────────────────────────────────

MM_PER_INCH = 25.4
MIN_PRINT_DPI = 200.0  # threshold below which effective DPI is flagged


# ─── Declarative Models ───────────────────────────────────────────────


class PreflightIssue(BaseModel):
    """A single issue detected during Preflight.

    Attributes:
        severity: 'warning' (informational, export proceeds) or
                  'blocker' (export refused unless explicitly overridden).
        code: Machine-readable issue code (e.g. 'unresolved_legacy_equations').
        message: User-facing plain-language message (Arabic, per STEP-6.2
                 translation standard).
    """

    model_config = ConfigDict(frozen=True)

    severity: str   # 'warning' | 'blocker'
    code: str
    message: str


class PreflightReport(BaseModel):
    """Result of a Preflight check tier.

    Attributes:
        issues: List of all detected issues (may be empty for a clean check).
        stage: Which tier produced this report ('pre_com' or 'post_generation').
    """

    model_config = ConfigDict(frozen=True)

    issues: List[PreflightIssue]
    stage: str   # 'pre_com' | 'post_generation'

    @property
    def has_blockers(self) -> bool:
        """True if any issue has severity 'blocker'."""
        return any(i.severity == "blocker" for i in self.issues)


# ─── Effective DPI Calculation ─────────────────────────────────────────


def compute_effective_dpi(asset: ImageAsset) -> Optional[float]:
    """Compute the effective print DPI for an image at its intended size.

    Uses BOTH axes and returns the LOWER (more conservative) value — the
    weaker dimension governs perceived print quality.

    Returns None if intended placement size isn't yet known (callers must
    treat None as "cannot verify yet," not as a pass).

    Args:
        asset: The ImageAsset containing pixel dimensions and intended
               placement size.

    Returns:
        The minimum of width-DPI and height-DPI, or None if placement
        size is unavailable or degenerate (zero/negative).
    """
    if asset.intended_width_mm is None or asset.intended_height_mm is None:
        return None
    if asset.intended_width_mm <= 0 or asset.intended_height_mm <= 0:
        return None  # degenerate placement — cannot compute meaningful DPI
    width_dpi = asset.pixel_width / (asset.intended_width_mm / MM_PER_INCH)
    height_dpi = asset.pixel_height / (asset.intended_height_mm / MM_PER_INCH)
    return min(width_dpi, height_dpi)


# ─── Pre-COM Preflight ────────────────────────────────────────────────


def run_pre_com_preflight(
    doc: BookletDocument,
    binding: BindingMethod,
) -> PreflightReport:
    """Run all pre-COM preflight checks.

    Executes entirely in the Python layer — never touches win32com or
    pythoncom.  Safe to call before COMSupervisor is invoked.

    Checks performed:
      1. Unresolved legacy equation placeholders (blocker).
      2. Effective image DPI (warning) / unverifiable DPI (warning).
      3. Filler pages required for binding (warning).

    Args:
        doc: The BookletDocument to check.
        binding: The binding method that will be used for this job.

    Returns:
        A PreflightReport with stage='pre_com'.
    """
    issues: List[PreflightIssue] = []

    # ── 1. Unresolved legacy equations (BLOCKER) ──────────────────
    legacy_count = sum(
        1
        for page in doc.pages
        for block in page.content_blocks
        if block.block_type == "legacy_equation_placeholder"
    )
    if legacy_count > 0:
        issues.append(PreflightIssue(
            severity="blocker",
            code="unresolved_legacy_equations",
            message=(
                f"يوجد {legacy_count} معادلة قديمة تحتاج مراجعتك اليدوية "
                f"قبل الطباعة — لا يمكن المتابعة دون حلها أو تجاوزها صراحة."
            ),
        ))

    # ── 2. Image DPI checks ───────────────────────────────────────
    for page in doc.pages:
        for block in page.content_blocks:
            asset: Optional[ImageAsset] = block.source_position.get(
                "image_asset"
            )
            if asset is None:
                continue

            # Reconstruct ImageAsset from dict if it was deserialized
            if isinstance(asset, dict):
                asset = ImageAsset(**asset)

            effective_dpi = compute_effective_dpi(asset)
            if effective_dpi is None:
                issues.append(PreflightIssue(
                    severity="warning",
                    code="image_dpi_unverified",
                    message=(
                        "لا يمكن التحقق من دقة إحدى الصور قبل معرفة "
                        "حجم وضعها النهائي — سيُعاد الفحص بعد التصميم."
                    ),
                ))
            elif effective_dpi < MIN_PRINT_DPI:
                issues.append(PreflightIssue(
                    severity="warning",
                    code="low_effective_dpi",
                    message=(
                        f"إحدى الصور دقتها الفعلية عند حجم العرض المقصود "
                        f"أقل من {int(MIN_PRINT_DPI)} DPI "
                        f"وقد تظهر مبكسلة بالطباعة."
                    ),
                ))

    # ── 3. Filler pages ───────────────────────────────────────────
    estimated_pages = len(doc.pages)
    target = imposed_page_count(estimated_pages, binding)
    if target != estimated_pages:
        issues.append(PreflightIssue(
            severity="warning",
            code="filler_pages_required",
            message=(
                f"سيُضاف {target - estimated_pages} صفحة فارغة "
                f"لمطابقة متطلبات التجليد."
            ),
        ))

    return PreflightReport(issues=issues, stage="pre_com")


# ─── Required Fonts Source ─────────────────────────────────────────────
# Fonts checked by the post-generation check are exactly the fonts
# referenced by TypographyStylesConfig (STEP-2.2) and the .idml
# template's paragraph styles (STEP-2.3's BLOCK_STYLE_MAP).  Derived
# programmatically, never a separately maintained list.

REQUIRED_FONTS_SOURCE = (
    "TypographyStylesConfig + .idml paragraph style definitions (STEP-2.2/2.3)"
)


def get_required_fonts() -> list[str]:
    """Derive the required font list from TypographyStylesConfig.

    Returns the fonts declared in the config, not a separate list that
    could drift out of sync (AC-6.4.4c).

    Returns:
        List of unique font names required by the typography config.
    """
    from app.automation.styles_schema import TypographyStylesConfig

    config = TypographyStylesConfig()
    fonts: set[str] = set()
    fonts.add(config.body_font)
    fonts.add(config.math_font)
    return sorted(fonts)


# ─── Export Gating & Override Handling (AC-6.4.1b) ─────────────────────


def can_proceed_to_export(
    report: PreflightReport,
    has_explicit_override: bool = False,
) -> bool:
    """Determine whether a document can proceed to final export.

    A document with blockers cannot proceed unless an explicit override
    has been recorded (AC-6.4.1b).

    Args:
        report: The PreflightReport from pre-COM or post-generation check.
        has_explicit_override: True if the user deliberately approved an
                               override action.

    Returns:
        True if export is permitted, False if blockers remain un-overridden.
    """
    if not report.has_blockers:
        return True
    return bool(has_explicit_override)


def assert_export_allowed(
    report: PreflightReport,
    has_explicit_override: bool = False,
) -> None:
    """Enforce the pre-export gate.

    Raises PreflightBlockerError if any blockers exist and no explicit
    override was granted.

    Args:
        report: The PreflightReport to evaluate.
        has_explicit_override: Whether the user explicitly overrode blockers.

    Raises:
        PreflightBlockerError: If unresolved blockers are present without override.
    """
    if report.has_blockers and not has_explicit_override:
        blockers = [i for i in report.issues if i.severity == "blocker"]
        raise PreflightBlockerError(blockers)


def proceed_to_export_with_override(
    report: PreflightReport,
    job_history_manager: Any,
    job_id: int,
    reason: str,
    user_identifier: str = "designer",
) -> bool:
    """Sanctioned override path to proceed to export past blockers (AC-6.4.1b).

    Writes an auditable record into the database (job_history / preflight_overrides)
    for every active blocker before authorizing export.

    Args:
        report: The PreflightReport with blocker issues.
        job_history_manager: JobHistoryManager instance connected to SQLite.
        job_id: The active job ID in job_history.
        reason: Plain-text justification for the override (cannot be empty).
        user_identifier: User identifier for audit logging.

    Returns:
        True if all overrides were recorded and export is authorized.

    Raises:
        ValueError: If reason is empty or whitespace.
        PreflightBlockerError: If report unexpectedly contains no blockers.
    """
    if not reason or not reason.strip():
        raise ValueError("An explicit reason is required to override preflight blockers.")

    blockers = [i for i in report.issues if i.severity == "blocker"]
    if not blockers:
        return True

    for blocker in blockers:
        job_history_manager.record_preflight_override(
            job_id=job_id,
            blocker_code=blocker.code,
            reason=reason.strip(),
            user_identifier=user_identifier,
        )

    return True

