<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.4] Preflight Check

#### 1. Rationale

STEP-1.2 already detects `legacy_equation_placeholder` blocks that require manual designer intervention, but nothing in Phases 1–5 surfaces this (or other pre-export risk conditions) as a single, explicit checklist before a job proceeds to final export. Preflight Check closes that gap: a deliberate, named "ready to print?" gate.

**Post-review correction:** the original draft had four confirmed defects: (1) unresolved legacy equations were classified as a `warning`, allowing export to proceed with a known-incomplete document, contradicting STEP-1.2's own statement that these blocks require manual resolution; (2) "source-image resolution checked directly from the extracted file" is not a meaningful check on its own — print-quality resolution is a function of *effective* DPI (pixel dimensions ÷ intended physical placement size), not raw pixel count, and `ContentBlock`/`DocumentStore` did not even store a reference to the source image file needed to check this; (3) the post-generation check's results had no defined path back to the UI — STEP-1.1's IPC channel is limited to primitive heartbeat/PID values and cannot carry a structured `PreflightReport`; (4) "missing-font detection" did not specify which fonts to check against. All four are corrected below.

#### 2. Architectural Placement — Two-Tier Check, Not a Single COM Round-Trip

Preflight Check is split across two distinct phases, matching where each piece of information actually becomes available in the existing pipeline — it is not one function and not one COM call:

- **Pre-COM checks (Python layer, before `COMSupervisor` is ever invoked):** unresolved `legacy_equation_placeholder` block count (now a **blocker**, see below), effective source-image DPI (using the corrected `ImageAsset` model below), and the page-count-vs-imposition check already computed by STEP-5.1's `imposed_page_count()`. These require no InDesign round-trip at all and can run immediately after classification (STEP-1.4) completes.
- **Post-generation checks (require the actual InDesign document, run inside the same `COMSupervisor` child-process job that performs generation, per STEP-1.1's process-isolation rule):** text frame overset (`.overflows`, the same property STEP-1.1's `ensureFrameCapacity()` already checks), missing-font detection against an explicit required-font list (see below), and unintended blank pages. These checks cannot run before generation because they depend on how InDesign actually laid out the content.
- **The explicit gap this creates, and how it's guarded:** because Preflight is split this way, it is possible for the pre-COM check to pass while the post-generation check later fails (e.g. a font looked present on the system but InDesign's specific instance can't resolve it) — this is not a bug to eliminate but a known characteristic of the two-tier design, and the UI must present pre-COM Preflight results as "no known blockers so far" rather than "guaranteed clean," with the post-generation check remaining the authoritative final gate before export.

#### 3. Corrected Image Metadata Model

`ContentBlock` and `DocumentStore` as originally specified store only text and `TableData` — there is no reference to a source image file for `image_prompt` blocks, so no DPI check could actually run against real data. This is closed by an explicit `ImageAsset` reference captured at extraction time (STEP-1.2/1.3), not invented later at Preflight time.

**Post-follow-up-review correction — confirmed wiring gap:** three independent reviewers confirmed that defining `ImageAsset` here, without also requiring STEP-1.2's DOCX extractor and STEP-1.3's PDF extractor to actually construct and populate it, leaves the model permanently unused — the Preflight loop below would iterate over zero `image_prompt` blocks carrying an asset, silently passing every document regardless of actual image quality. This is now an explicit, mandatory addition to STEP-1.2 and STEP-1.3's own extraction responsibilities, not an optional Phase 6 nicety:

- **STEP-1.2 (DOCX) extends its extraction strategy:** `w:drawing`/`w:pict` elements encountered during the `w:p` traversal already described in STEP-1.2 §2 are extracted alongside `w:object`/`v:shape` handling — the embedded image binary is unzipped from the `.docx` package's media parts (a `.docx` is a zip archive; embedded images live under `word/media/`), written to a job-scoped temporary directory, and its pixel dimensions are read (e.g. via Pillow, added as a new dependency for this purpose) to construct an `ImageAsset`. `intended_width_mm`/`intended_height_mm` are populated from the drawing element's `wp:extent` (EMU units, convertible to mm) when present — this is usually available directly in the DOCX XML, unlike the PDF case below.
- **STEP-1.3 (PDF) extends its extraction strategy:** `page.get_images()` (PyMuPDF's own image-enumeration API) is used to locate embedded raster images per page; the binary is extracted via `page.extract_image()`, written to the same job-scoped temporary directory, and pixel dimensions are read from the extracted binary. `intended_width_mm`/`intended_height_mm` are populated from the image's placement `fitz.Rect` on the page when PyMuPDF can resolve it (i.e. the image is drawn at a known, fixed size in the source PDF) — this is not always resolvable purely from the source in the PDF case, in which case the fields remain `None` and Preflight correctly reports `image_dpi_unverified` (AC-6.4.2c) rather than fabricating a placement size.
- **Both extractors attach the resulting `ImageAsset` to the corresponding `image_prompt` `ContentBlock.source_position["image_asset"]`** at the point that block is constructed — this is additive to each extractor's existing per-block construction step, not a separate post-processing pass that could be skipped.

```python
# app/core/models.py (Phase 6 addition — extends the Phase 1 model)
from pydantic import BaseModel, ConfigDict
from typing import Optional

class ImageAsset(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_path: str            # path to the extracted/referenced image file
    pixel_width: int
    pixel_height: int
    # Intended physical placement size, if already known at extraction
    # time (from wp:extent in DOCX, or a resolvable placement Rect in
    # PDF — see the extractor wiring above); None if placement is only
    # resolved later during InDesign composition — in which case the
    # effective-DPI check is necessarily deferred, and Preflight must
    # label it as unverified rather than presenting a guaranteed pass/fail.
    intended_width_mm: Optional[float] = None
    intended_height_mm: Optional[float] = None

# image_prompt ContentBlocks gain an optional link to their asset:
#   ContentBlock.source_position["image_asset"] references an ImageAsset,
#   populated during STEP-1.2/1.3 extraction per the wiring above — this
#   is additive to the existing frozen ContentBlock contract, not a
#   breaking change to it.
```

#### 4. Declarative Definitions

```python
# app/core/preflight.py
from pydantic import BaseModel, ConfigDict
from app.core.models import BookletDocument, ImageAsset
from app.core.pricing_engine import imposed_page_count
from app.core.pricing_models import BindingMethod

MM_PER_INCH = 25.4
MIN_PRINT_DPI = 200.0  # threshold below which effective DPI is flagged (see severity note below)

class PreflightIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    severity: str  # 'warning' | 'blocker'
    code: str
    message: str   # plain-language, same translation standard as STEP-6.2

class PreflightReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    issues: list[PreflightIssue]
    stage: str  # 'pre_com' | 'post_generation'

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == "blocker" for i in self.issues)

def compute_effective_dpi(asset: ImageAsset) -> float | None:
    """
    Post-follow-up-review correction: the prior version used only
    pixel_width / intended_width_mm, silently ignoring height entirely —
    two reviewers confirmed a non-square placement (e.g. an image
    stretched disproportionately) could report a misleadingly high DPI
    based on the narrower dimension alone. This version computes BOTH
    axes and returns the LOWER (more conservative) of the two — the
    weaker dimension governs perceived print quality, since a viewer
    notices the blurrier axis regardless of what the other axis reports.

    Returns None if intended placement size isn't yet known (see
    ImageAsset docstring above) — callers must treat None as "cannot
    verify yet," not as a pass.
    """
    if asset.intended_width_mm is None or asset.intended_height_mm is None:
        return None
    if asset.intended_width_mm <= 0 or asset.intended_height_mm <= 0:
        return None  # degenerate placement size — cannot compute a meaningful DPI
    width_dpi = asset.pixel_width / (asset.intended_width_mm / MM_PER_INCH)
    height_dpi = asset.pixel_height / (asset.intended_height_mm / MM_PER_INCH)
    return min(width_dpi, height_dpi)

def run_pre_com_preflight(doc: BookletDocument, binding: BindingMethod) -> PreflightReport:
    """
    Runs entirely in the Python layer, before any COMSupervisor job is
    submitted. Never touches win32com/pythoncom.
    """
    issues: list[PreflightIssue] = []

    legacy_count = sum(
        1 for page in doc.pages for block in page.content_blocks
        if block.block_type == "legacy_equation_placeholder"
    )
    if legacy_count > 0:
        # CORRECTED: this is a blocker, not a warning. STEP-1.2 already
        # states these blocks require manual designer intervention;
        # allowing export to proceed past a warning would let a booklet
        # with unresolved equations reach print. An explicit, logged user
        # override is the only sanctioned path past this blocker (see
        # Preconditions) — silent proceed-past-warning is not.
        issues.append(PreflightIssue(
            severity="blocker",
            code="unresolved_legacy_equations",
            message=f"يوجد {legacy_count} معادلة قديمة تحتاج مراجعتك اليدوية قبل الطباعة — لا يمكن المتابعة دون حلها أو تجاوزها صراحة.",
        ))

    for page in doc.pages:
        for block in page.content_blocks:
            asset: ImageAsset | None = block.source_position.get("image_asset")
            if asset is None:
                continue
            effective_dpi = compute_effective_dpi(asset)
            if effective_dpi is None:
                issues.append(PreflightIssue(
                    severity="warning",
                    code="image_dpi_unverified",
                    message="لا يمكن التحقق من دقة إحدى الصور قبل معرفة حجم وضعها النهائي — سيُعاد الفحص بعد التصميم.",
                ))
            elif effective_dpi < MIN_PRINT_DPI:
                # Post-follow-up-review note: reviewers flagged that the
                # original comment on MIN_PRINT_DPI said "blocker" while
                # this code emitted "warning" — an actual inconsistency,
                # not a documentation slip to wave away. Resolved
                # explicitly here: low_effective_dpi remains a WARNING,
                # deliberately distinct from unresolved_legacy_equations'
                # BLOCKER severity. Rationale: a DPI check is inherently
                # an estimate (see intended_width_mm/height_mm's own
                # "may be unresolved until composition" caveat above),
                # whereas an unresolved legacy equation is a definite,
                # unconditional content-completeness gap with no
                # estimation involved. Treating an estimate-based warning
                # as an unbypassable blocker would force manual overrides
                # on legitimate low-severity cases (e.g. a decorative
                # background image where DPI genuinely doesn't matter).
                issues.append(PreflightIssue(
                    severity="warning",
                    code="low_effective_dpi",
                    message=f"إحدى الصور دقتها الفعلية عند حجم العرض المقصود أقل من {int(MIN_PRINT_DPI)} DPI وقد تظهر مبكسلة بالطباعة.",
                ))

    estimated_pages = len(doc.pages)
    target = imposed_page_count(estimated_pages, binding)
    if target != estimated_pages:
        issues.append(PreflightIssue(
            severity="warning",
            code="filler_pages_required",
            message=f"سيُضاف {target - estimated_pages} صفحة فارغة لمطابقة متطلبات التجليد.",
        ))

    return PreflightReport(issues=issues, stage="pre_com")

# Fonts checked by the post-generation check are not open-ended — they
# are exactly the fonts referenced by TypographyStylesConfig (STEP-2.2)
# and the .idml template's paragraph styles (STEP-2.3's BLOCK_STYLE_MAP
# preconditions): body_font, math_font, and any per-language Latin body
# font. This list is derived from those existing configs, not maintained
# as a separate, potentially-drifting list.
REQUIRED_FONTS_SOURCE = "TypographyStylesConfig + .idml paragraph style definitions (STEP-2.2/2.3)"
```

```python
# app/automation/com_supervisor.py (Phase 6 extension)
"""
Post-review correction: the original IPC channel (multiprocessing.Value
floats/ints for heartbeat and PID only) is physically incapable of
carrying a structured PreflightReport back to the parent process. The
channel is extended, not replaced, with a dedicated payload path:
"""

class COMJobRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    job_command: Literal["generate_booklet", "add_filler_pages", "verify_indesign"]
    payload_file_path: str
    # New: where the child process writes its PreflightReport (as JSON)
    # after post-generation checks complete, for the supervisor to read
    # once the child process joins. A file-based handoff is used rather
    # than adding a second multiprocessing.Queue, keeping the "primitive
    # values only across the process boundary" rule (STEP-1.1) intact —
    # the file path is a string, the payload itself never crosses the
    # boundary as a live Python object.
    preflight_output_path: str

# The child process's generate_booklet job command writes a JSON-
# serialized PreflightReport to preflight_output_path after composition
# completes and before the child process exits. COMSupervisor.run_job()
# reads and parses this file after the child process joins successfully
# — this is the same "file-based IPC pattern" STEP-1.1 already documents
# for the outbound payload, applied symmetrically to the return path.
```

#### 5. Preconditions

- Pre-COM Preflight must not import or call any `win32com`/`pythoncom` symbol — this is the same import-boundary lint rule already required by AC-1.1.10 for the `COMSupervisor` pattern, extended to cover this new module.
- The UI must visibly distinguish pre-COM Preflight results from post-generation Preflight results — they are not interchangeable and must not be presented as a single unified "all clear" state.
- An `unresolved_legacy_equations` blocker may only be bypassed via an explicit, logged user override action (distinct from simply dismissing a warning dialog) — the override itself is recorded in `job_history` (STEP-6.3) so it is auditable after the fact.
- The post-generation required-font list is derived programmatically from `TypographyStylesConfig` and the `.idml` template's paragraph style font assignments — it is never a separately hand-maintained list that could silently drift out of sync with the actual template.
- `Pillow` is added to `requirements.txt` (STEP-1.1's dependency list) specifically to support `ImageAsset` pixel-dimension reading in STEP-1.2's extractor — this is a new, required dependency introduced by this correction, not an optional convenience.
- STEP-1.2 and STEP-1.3's extractors are not considered complete implementations of their own acceptance criteria until they populate `ContentBlock.source_position["image_asset"]` for every embedded image they encounter — this is a mandatory extension of those STEPs' existing scope, not an independent Phase 6 feature that could be skipped without affecting Phase 1's own completeness.

#### 6. Acceptance Criteria

- **[AC-6.4.1]** `run_pre_com_preflight()` correctly reports `unresolved_legacy_equations` matching the exact count of `legacy_equation_placeholder` blocks in the document, with `severity="blocker"`.
- **[AC-6.4.1b]** A document with `unresolved_legacy_equations` present cannot proceed to final export without an explicit, separately-logged override action — verified by confirming the normal export path refuses to proceed and that only the override path (which writes an audit record) allows it through.
- **[AC-6.4.2]** `run_pre_com_preflight()` correctly reports `filler_pages_required` using the same `imposed_page_count()` function STEP-5.1/STEP-2.1 already use — not an independently recomputed rounding rule.
- **[AC-6.4.2b]** `compute_effective_dpi()` returns a lower value for the same image at a larger `intended_width_mm` than at a smaller one — verified directly against the documented 1200×1200px example (600 PPI at 2×2in vs. 150 PPI at 8×8in) — and additionally, for a non-square placement (e.g. `intended_width_mm` far larger than `intended_height_mm`), the function returns the DPI computed from the weaker (lower-DPI) axis, not the stronger one — verified against a fixture where width-DPI and height-DPI deliberately differ, closing the width-only defect from the prior draft.
- **[AC-6.4.2c]** An `image_prompt` block whose `ImageAsset.intended_width_mm`/`height_mm` is not yet known produces an `image_dpi_unverified` warning, never a false pass — verified by confirming `compute_effective_dpi()` returning `None` results in this specific issue code, not silence.
- **[AC-6.4.2d]** Running the DOCX (STEP-1.2) and PDF (STEP-1.3) extractors against a fixture file containing at least one embedded image produces a `ContentBlock` whose `source_position["image_asset"]` is a populated, non-`None` `ImageAsset` with correct `pixel_width`/`pixel_height` read from the actual embedded binary — verified end-to-end against real extracted image data, not merely that the `ImageAsset` Pydantic model can be constructed in isolation. This directly closes the confirmed "orphaned model" gap: without this test passing, `run_pre_com_preflight()`'s image-DPI loop is guaranteed to iterate over zero populated assets regardless of how correct `compute_effective_dpi()` itself is.
- **[AC-6.4.3]** No function in `app/core/preflight.py`'s pre-COM path imports `win32com` or `pythoncom`, directly or transitively — verified by the same import-boundary lint rule as AC-1.1.10.
- **[AC-6.4.4]** Post-generation overset detection reuses the same `.overflows` check STEP-1.1's `ensureFrameCapacity()` already performs, rather than a separately-implemented overflow check that could disagree with it.
- **[AC-6.4.4b]** A `PreflightReport` produced by post-generation checks inside the `COMSupervisor` child process is successfully read and parsed by the parent process after the child joins — verified end-to-end via `preflight_output_path`, not assumed from the existing heartbeat channel's capabilities.
- **[AC-6.4.4c]** The post-generation font check's required-font list matches exactly the fonts declared in `TypographyStylesConfig` and the `.idml` template's paragraph styles for the document being generated — verified by confirming the list is derived programmatically, not hardcoded separately.
- **[AC-6.4.5]** The UI presentation of a pre-COM Preflight pass with zero issues is verified to never claim the document is confirmed print-ready — that claim is only made after the post-generation check also passes.

---
