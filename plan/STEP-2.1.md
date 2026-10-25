<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-2.1] IDML Master Spreads & Document Geometry Engine

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Master Pages Engine | InDesign MasterSpread (Parent Pages via `.count()`) | Replaces fragile per-page manual text frame injection with native InDesign master spreads, eliminating frame overlap and guaranteeing clean separation between header, footer, and body story. |
| Running Headers | InDesign Text Variables (Running Header / Section Marker) | Dynamically binds chapter titles to the top ribbon across all pages automatically without manual string passing or script-level page loops. |
| Pagination Engine | Native `SpecialCharacters.AUTO_PAGE_NUMBER` | Native InDesign page number markers embedded in Master Spreads, fully respecting Arabic numbering and facing-page layouts. |
| Geometry & Margin Calculator | Spine-Aware Margin Matrix (Python → JSX) | Calculates binding offsets (`insideMargin` vs. `outsideMargin`) based on total page count and booklet spine thickness for print-shop binding (سيم / كبس / غراء). |

#### 2. Preconditions & Architectural Design

- **Mandatory Template Precondition:** The base `.idml` template in `assets/` must be pre-configured as a Facing Pages document (`documentPreferences.facingPages = true`). If a single-sided layout is supplied, the margin calculator automatically falls back to uniform symmetric margins across all pages.
- **Minimum Top Margin Constraint:** `top_mm` must be ≥ 12.0mm (≈34pt) to safely host the header ribbon and graphic divider without negative-coordinate clipping (`Math.max` safeguard applied in JSX, computed from the same `MM_TO_PT` constant used throughout — not a hardcoded point value).
- **The Stale-Reference Root Cause (why this replaces manual framing):** Generating headers/footers by iterating over `doc.pages` and adding text frames per-page created hundreds of detached COM/DOM objects, triggering memory leaks and invalid bounds errors (`Object is invalid`) — this is the same failure class documented in STEP-1.1's InDesign lifecycle rules, now solved structurally rather than defensively.
- **Native Master Spread Pipeline:** Script configures `MasterSpread("Booklet_Master")` with two text frames per spread page (Header Ribbon, Footer Ribbon with `AUTO_PAGE_NUMBER`). The Header frame embeds a Running Header Text Variable linked to the `Booklet_Title` paragraph style. Every new page added during text overflow automatically inherits `Booklet_Master` without executing any ExtendScript loops on the page object itself. The document story flows strictly through the Primary Text Frame within page margins.

#### 3. Declarative Definitions

```javascript
// assets/scripts/build_master_spread.jsx
/**
 * Master Spread & Text Variable Generator
 * Executes once during document initialization.
 */
function setupMasterSpread(doc, docCfg, colors, styles) {
    var MM_TO_PT = 2.83465;
    var MIN_TOP_MARGIN_MM = 12.0;
    var master = doc.masterSpreads.item(0);
    master.name = "Booklet_Master";

    // 1. Define Running Header Text Variable (linked to Chapter Title)
    var headerVar = doc.textVariables.itemByName("Booklet_Header_Var");
    if (!headerVar.isValid) {
        headerVar = doc.textVariables.add({
            name: "Booklet_Header_Var",
            variableType: VariableTypes.MATCH_PARAGRAPH_STYLE_TYPE
        });
        headerVar.variableOptions.appliedParagraphStyle = styles.titleStyle;
        headerVar.variableOptions.searchStrategy = SearchStrategies.FIRST_ON_PAGE;
    }

    // 2. Setup Master Pages (ExtendScript Collection uses .count(), never .length)
    var pageCount = master.pages.count();
    for (var i = 0; i < pageCount; i++) {
        var mPage = master.pages.item(i);
        var pBounds = mPage.bounds;
        var pWidth = pBounds[3] - pBounds[1];
        var pHeight = pBounds[2] - pBounds[0];

        // CORRECTION (round 1): an earlier draft read docCfg.margins.top /
        // .inside / .outside, but Python's MarginConfig defines top_mm /
        // inside_spine_mm / outside_mm — a field-name mismatch masked by
        // `|| 20`-style fallbacks (mismatch silently substituted a
        // hardcoded default instead of failing).
        //
        // CORRECTION (round 2): fixing the field names alone was not
        // enough — all four independent reviewers flagged that the `||`
        // fallback pattern ITSELF is the real defect, not just this one
        // instance of it. Any FUTURE field-name typo or serialization
        // change would silently repeat the exact same bug. The fallback
        // pattern is removed entirely; missing/invalid fields now fail
        // loudly at generation start, before any page geometry is built.
        function requireMarginField(marginsObj, fieldName) {
            if (!marginsObj || typeof marginsObj[fieldName] !== "number") {
                throw new Error(
                    "MarginConfigError: required field '" + fieldName +
                    "' is missing or not a number in the margins payload. " +
                    "This indicates a Python\u2194JSX serialization mismatch " +
                    "or an incomplete payload — fix the mismatch, do not " +
                    "add a fallback default here."
                );
            }
            return marginsObj[fieldName];
        }

        var mTop = Math.max(
            requireMarginField(docCfg.margins, "top_mm") * MM_TO_PT,
            MIN_TOP_MARGIN_MM * MM_TO_PT
        );
        var mBottom = requireMarginField(docCfg.margins, "bottom_mm") * MM_TO_PT;
        var mInside = requireMarginField(docCfg.margins, "inside_spine_mm") * MM_TO_PT;
        var mOutside = requireMarginField(docCfg.margins, "outside_mm") * MM_TO_PT;

        // CORRECTION: an earlier draft's side-detection only handled
        // LEFT_HAND / RIGHT_HAND, silently applying asymmetric spine
        // margins even to SINGLE_SIDED (non-facing-pages) documents —
        // contradicting this plan's own stated fallback ("single-sided
        // layout... falls back to uniform symmetric margins"). Three
        // independent reviewers confirmed PageSideOptions.SINGLE_SIDED is
        // a real, documented third value that is neither LEFT_HAND nor
        // RIGHT_HAND, so treating "not LEFT_HAND" as "must be RIGHT_HAND"
        // is incorrect. Single-sided pages now get explicit symmetric
        // treatment instead of falling through the left/right branch.
        var isSingleSided = (mPage.side === PageSideOptions.SINGLE_SIDED);
        var isLeft = !isSingleSided && (mPage.side === PageSideOptions.LEFT_HAND);

        var left, right;
        if (isSingleSided) {
            // Symmetric margins — no inside/outside spine distinction
            // applies without a facing page to bind against.
            var symmetricMargin = mOutside; // use outside_mm as the uniform value
            left = symmetricMargin;
            right = pWidth - symmetricMargin;
        } else {
            left = isLeft ? mOutside : mInside;
            right = isLeft ? (pWidth - mInside) : (pWidth - mOutside);
        }

        // Header Frame
        var hFrame = mPage.textFrames.add({
            geometricBounds: [mTop - 18, left, mTop - 4, right]
        });
        hFrame.textFramePreferences.verticalJustification = VerticalJustification.BOTTOM_ALIGN;
        hFrame.insertionPoints[0].contents = SpecialCharacters.SECTION_MARKER;
        hFrame.insertionPoints[-1].contents = "   |   ";
        hFrame.insertionPoints[-1].textVariableInstances.add({associatedTextVariable: headerVar});
        hFrame.paragraphs.everyItem().applyParagraphStyle(styles.runningHeaderStyle, false);

        // Header Divider Line
        var hLine = mPage.graphicLines.add();
        hLine.geometricBounds = [mTop - 2, left, mTop - 2, right];
        hLine.strokeColor = colors.borderColor;
        hLine.strokeWeight = 0.5;

        // Footer Frame (Auto Page Number)
        var fFrame = mPage.textFrames.add({
            geometricBounds: [pHeight - mBottom + 4, left, pHeight - mBottom + 18, right]
        });
        fFrame.textFramePreferences.verticalJustification = VerticalJustification.TOP_ALIGN;
        fFrame.insertionPoints[0].contents = "Booklet_AI   •   صفحة ";
        fFrame.insertionPoints[-1].contents = SpecialCharacters.AUTO_PAGE_NUMBER;
        fFrame.paragraphs.everyItem().applyParagraphStyle(styles.runningFooterStyle, false);

        // Body Frame (Primary Story Frame) — CORRECTION (round 1): an
        // earlier version of this function created only header/footer
        // frames, while STEP-1.1's ensureFrameCapacity() depends on a
        // labeled body frame that never existed on the master, guaranteeing
        // MasterFrameNotFoundError on the very first page overflow. This
        // frame sits between the header divider and the footer, and is
        // labeled per-side so findMasterFrameByLabelOnSide() (STEP-1.1)
        // can retrieve the correct left/right geometry.
        //
        // CORRECTION (round 2): all reviewers confirmed MarginConfig has
        // no upper bound or cross-field validation — top_mm and bottom_mm
        // are unconstrained floats, so a pathological but structurally
        // valid config (e.g. top_mm = 300, bottom_mm = 300) can make
        // bodyBottom <= bodyTop, producing zero/negative frame height.
        // InDesign's textFrames.add() with inverted geometricBounds either
        // throws a COM exception or produces invalid geometry — neither
        // is acceptable, so this is validated explicitly and loudly before
        // attempting frame creation, rather than letting InDesign fail
        // with an opaque COM error deep in master construction.
        var bodyTop = mTop + 6;
        var bodyBottom = pHeight - mBottom - 6;
        if (bodyBottom <= bodyTop) {
            throw new Error(
                "InvalidBodyFrameGeometryError: computed body frame height is " +
                "zero or negative (top=" + bodyTop + ", bottom=" + bodyBottom + "). " +
                "top_mm + bottom_mm leaves no usable body area for this page size — " +
                "reduce margins or increase page height."
            );
        }

        var bFrame = mPage.textFrames.add({
            geometricBounds: [bodyTop, left, bodyBottom, right]
        });
        bFrame.label = "Booklet_Body_Story";
    }
}

/**
 * Adds blank filler pages after all content generation is complete, to
 * bring the document's total page count up to the binding method's
 * required multiple (see STEP-5.1's imposed_page_count()).
 *
 * CORRECTION (round 1): an earlier draft asserted (via AC-5.1.6) that
 * pricing's imposed_page_count() and the actual InDesign document's page
 * count were kept in sync — but no code implementing that sync was ever
 * written. Without this function, a 17-page saddle-stitch booklet would be
 * priced for 20 pages while the actual InDesign document had only 17.
 *
 * CORRECTION (round 2): the round-1 fix conflated two entirely different
 * situations under one error. If real content (font substitution, Arabic
 * shaping, table breaks, inline math expansion) causes the document to
 * naturally require MORE pages than the naive pre-layout page-count
 * estimate assumed, doc.pages.count() can already exceed targetPageCount
 * when this function runs — the while loop correctly does nothing, but
 * the old equality check then threw "FillerPageMismatchError" as if a
 * filler-insertion operation had failed, when nothing about filler
 * insertion was actually wrong; the real cause was that the PRICING
 * ESTIMATE was made before layout and the actual content simply needed
 * more room. These are different failure classes needing different
 * handling: a genuine shortfall is fixed by this function; an overrun
 * means the quote itself is stale and must be recalculated, not "fixed"
 * by deleting real content pages.
 */
function addBlankFillerPages(doc, targetPageCount, masterName) {
    var currentCount = doc.pages.count();

    if (currentCount > targetPageCount) {
        // Content genuinely overran the pre-layout estimate — this is a
        // pricing/estimation staleness issue, not a filler-page defect.
        // Recompute imposed_page_count() in Python against the ACTUAL
        // count and re-quote, rather than silently truncating real pages
        // or misreporting this as a filler-insertion failure.
        throw new ContentExceedsEstimateError(
            "Actual content requires " + currentCount + " pages, exceeding " +
            "the pre-layout estimate of " + targetPageCount + ". This is not " +
            "a filler-page error — the price quote must be recalculated " +
            "against the actual page count via pricing_engine.imposed_page_count(" +
            currentCount + ", binding) before proceeding."
        );
    }

    while (currentCount < targetPageCount) {
        var lastPage = doc.pages.item(-1);
        var newPage = doc.pages.add(LocationOptions.AFTER, lastPage);
        newPage.appliedMaster = doc.masterSpreads.itemByName(masterName);
        // Filler pages carry no body-frame override — no injected body
        // content appears. Note this is NOT the same as "visually blank":
        // the header ribbon, footer ribbon, and AUTO_PAGE_NUMBER are
        // inherited automatically from the master (InDesign renders
        // un-overridden master items on every page that uses that
        // master) and WILL appear on these pages, consistent with every
        // other page in the booklet. These are correctly described as
        // "imposition filler pages with normal running header/footer,
        // no body content" — not "blank pages" in the literal sense.
        currentCount++;
    }

    if (doc.pages.count() !== targetPageCount) {
        throw new Error(
            "FillerPageMismatchError: expected " + targetPageCount +
            " pages after filler insertion, got " + doc.pages.count() +
            " — filler insertion did not reach the target count " +
            "(this indicates a bug in the insertion loop itself, distinct " +
            "from the pre-layout overrun case handled above)."
        );
    }
}
```

```python
# app/automation/pagination_sync.py — the previously-missing wiring
# connecting pricing_engine.imposed_page_count() to the actual JSX call.
# CORRECTION: three independent reviewers noted that both this function's
# existence and STEP-5.1's imposed_page_count() were shown, but the actual
# orchestration connecting them (Python computing the target, then invoking
# the JSX function with it) was never written — this closes that gap.
from app.core.pricing_engine import imposed_page_count
from app.core.pricing_models import BindingMethod

class ContentExceedsEstimateError(BookletError):
    """
    Raised (mirrored from the JSX side) when actual generated content
    exceeds the pre-layout page-count estimate. Signals that pricing must
    be recalculated against the actual count, not that filler-page
    insertion failed.
    """

def sync_filler_pages(com_bridge, doc_handle, estimated_page_count: int, binding: BindingMethod) -> int:
    """
    Called once after the main content-injection loop completes and
    before export. Computes the imposed (rounded) target from the
    PRE-layout estimate, invokes the JSX filler-page function, and
    surfaces ContentExceedsEstimateError distinctly from a genuine
    filler-insertion bug so the caller can decide to re-quote rather
    than treat this as a fatal generation failure.

    Returns the final synced page count on success.
    """
    target = imposed_page_count(estimated_page_count, binding)
    try:
        com_bridge.call_jsx_function(
            "addBlankFillerPages",
            doc_handle,
            target,
            "Booklet_Master",
        )
        return target
    except ContentExceedsEstimateError:
        # Re-derive the target from the actual page count and re-quote —
        # this is an expected, handleable case, not a hard failure.
        actual_count = com_bridge.call_jsx_function("getPageCount", doc_handle)
        new_target = imposed_page_count(actual_count, binding)
        com_bridge.call_jsx_function(
            "addBlankFillerPages", doc_handle, new_target, "Booklet_Master"
        )
        return new_target  # caller is responsible for re-quoting STEP-5.1's pricing using new_target
```

```python
# app/automation/geometry.py
from pydantic import BaseModel, ConfigDict, model_validator

class MarginConfig(BaseModel):
    """
    IMPORTANT — serialization contract: when this model is serialized to
    the JSON payload consumed by build_master_spread.jsx (via
    model_dump()), field names are passed through UNCHANGED. The JSX
    reads docCfg.margins.top_mm / .bottom_mm / .inside_spine_mm /
    .outside_mm — matching these field names exactly, not a renamed or
    abbreviated form. An earlier draft of this plan had the JSX reading
    differently-named fields (.top / .inside / .outside), silently masked
    by `|| default`-style fallbacks; both the field-name mismatch and the
    fallback pattern itself were removed (see build_master_spread.jsx,
    which now fails loudly on any missing/invalid margin field instead of
    silently substituting a default).
    """
    model_config = ConfigDict(frozen=True)
    top_mm: float = 20.0
    bottom_mm: float = 20.0
    inside_spine_mm: float = 22.0  # Safe spine offset for booklet binding (كعب)
    outside_mm: float = 15.0
    column_count: int = 1
    column_gutter_mm: float = 5.0

    @model_validator(mode="after")
    def validate_margins_nonnegative(self) -> "MarginConfig":
        """
        Defense-in-depth: catches the confirmed bug class (unbounded
        margin values producing zero/negative body-frame height) at the
        Python layer, before a bad config ever reaches JSX — the JSX-side
        InvalidBodyFrameGeometryError check (STEP-2.1) remains as a second
        independent guard, since this validator cannot know the target
        page's actual height (that's resolved later against PageGeometry).
        This validator only catches the always-true-regardless-of-page-size
        case: negative margins, which are never valid.
        """
        for field_name in ("top_mm", "bottom_mm", "inside_spine_mm", "outside_mm"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative, got {getattr(self, field_name)}")
        return self

class PageGeometry(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_size: str = "A4"
    width_pt: float = 595.28
    height_pt: float = 841.89
    facing_pages: bool = True
    bleed_mm: float = 3.0
    margins: MarginConfig = MarginConfig()
```

#### 4. Acceptance Criteria

- **[AC-2.1.1]** Document creation initializes zero body text frames on individual pages manually; all header/footer ribbons and dividing lines reside strictly inside `MasterSpread("Booklet_Master")`.
- **[AC-2.1.2]** Automatic page numbers update dynamically across any number of generated pages via `SpecialCharacters.AUTO_PAGE_NUMBER` without requiring script-level page indexing.
- **[AC-2.1.3]** The dynamic Running Header variable updates instantly on pages where a new `chapter_header` or `Booklet_Title` appears.
- **[AC-2.1.4]** For a document with `facing_pages = True`, the `inside_spine_mm` offset on the left-hand margin equals `outside_mm` on right-hand pages and vice versa — verified programmatically by comparing `hFrame.geometricBounds[1]` (the x0 coordinate) across generated left vs. right pages.
- **[AC-2.1.5]** Both the left and right master pages carry a text frame labeled `"Booklet_Body_Story"` after `setupMasterSpread()` runs — verified before any generation job starts, not discovered on first overflow.
- **[AC-2.1.6]** A generation job whose content overflows onto a new right-hand page uses the right-hand master page's body frame geometry (verified via `override()`'d frame bounds matching the right-page margins), and a new left-hand page uses the left-hand master's geometry — not the first-found frame regardless of side.
- **[AC-2.1.7]** Changing `MarginConfig.top_mm` (or any other margin field) in Python and regenerating a document produces a measurably different `mTop`/`hFrame` geometry in the resulting InDesign document — verified end-to-end (not just that the JSX code compiles), catching the class of bug where a Python-side config change silently has no effect due to a field-name mismatch with the JSX payload reader.
- **[AC-2.1.8]** A `MarginConfig` with `top_mm = 300, bottom_mm = 300` on a standard A4 page raises `InvalidBodyFrameGeometryError` (JSX) or a `ValidationError` (Python, for negative values) before any document content is generated — never produces a zero/negative-height body frame passed silently to `textFrames.add()`.
- **[AC-2.1.9]** A document with `facing_pages = False` (single-sided) generates successfully with symmetric left/right margins on every page, and `findMasterFrameByLabelOnSide()` correctly resolves the body frame for `PageSideOptions.SINGLE_SIDED` pages — verified by a dedicated single-sided test fixture, not only the facing-pages case.
- **[AC-2.1.10]** A payload deliberately missing the `outside_mm` field (simulating a future serialization mismatch) causes `build_master_spread.jsx` to throw `MarginConfigError` immediately, before any page geometry is computed — never silently substitutes a hardcoded fallback value.

---
