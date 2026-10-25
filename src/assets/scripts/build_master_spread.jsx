/*
    build_master_spread.jsx — Master Spread & Document Geometry Engine
    STEP-2.1

    Configures MasterSpread("Booklet_Master") with:
    - Running Header Text Variable (Booklet_Header_Var)
    - Header ribbon frame (section marker + text variable)
    - Header divider graphic line
    - Footer ribbon frame (AUTO_PAGE_NUMBER)
    - Labeled body story frame (Booklet_Body_Story)

    Handles PageSideOptions: LEFT_HAND, RIGHT_HAND, and SINGLE_SIDED.

    Also provides addBlankFillerPages() for binding-imposed page padding,
    with distinct error paths for content-overrun vs. insertion-bug.

    Usage (from Python COM bridge):
        app.DoScript(File("build_master_spread.jsx"), ScriptLanguage.JAVASCRIPT);

    Or via payload_loader pattern:
        #include "lib/json2.jsx"
        #include "payload_loader.jsx"
        var payload = loadPayloadFromFile(app.scriptArgs.getValue("payloadFile"));
        setupMasterSpread(app.activeDocument, payload.geometry, payload.colors, payload.styles);

    Dependencies:
        - lib/json2.jsx (JSON polyfill for ExtendScript)
        - payload_loader.jsx (file-based IPC)

    STEP-1.1 reference: InDesign lifecycle rules, COM threading, modal suppression
*/

// ─── Margin Field Validator ───────────────────────────────────────────

/**
 * Validate that a required margin field exists and is a number.
 *
 * Replaces the old || fallback pattern that silently substituted
 * hardcoded defaults on field-name mismatches. Any missing or
 * non-numeric field now fails LOUDLY at generation start, before
 * any page geometry is built.
 *
 * @param {Object} marginsObj - The margins sub-object from docCfg.
 * @param {string} fieldName - The exact field name to validate
 *     (must match MarginConfig's Python field names: top_mm,
 *     bottom_mm, inside_spine_mm, outside_mm).
 * @returns {number} The validated field value.
 * @throws {Error} MarginConfigError if field is missing or non-numeric.
 */
function requireMarginField(marginsObj, fieldName) {
    if (!marginsObj || typeof marginsObj[fieldName] !== "number") {
        throw new Error(
            "MarginConfigError: required field '" + fieldName +
            "' is missing or not a number in the margins payload. " +
            "This indicates a Python\u2194JSX serialization mismatch " +
            "or an incomplete payload \u2014 fix the mismatch, do not " +
            "add a fallback default here."
        );
    }
    return marginsObj[fieldName];
}


// ─── Master Spread Setup ──────────────────────────────────────────────

/**
 * Configure the document's first master spread as "Booklet_Master".
 *
 * Creates header ribbon, footer ribbon (with AUTO_PAGE_NUMBER),
 * header divider line, and labeled body story frames on every page
 * of the master spread. Handles facing-page (left/right) and
 * single-sided layouts correctly.
 *
 * Executes once during document initialization — every new page
 * added during text overflow automatically inherits Booklet_Master
 * without executing any ExtendScript loops on the page object itself.
 *
 * @param {Document} doc - The active InDesign document.
 * @param {Object} docCfg - Document configuration object. Must contain
 *     a 'margins' sub-object with fields: top_mm, bottom_mm,
 *     inside_spine_mm, outside_mm (matching MarginConfig exactly).
 * @param {Object} colors - Color palette. Must contain:
 *     borderColor: a Swatch or Color object for the header divider.
 * @param {Object} styles - Paragraph style references. Must contain:
 *     titleStyle: ParagraphStyle used by the Running Header variable
 *         to match chapter titles (applied via MATCH_PARAGRAPH_STYLE_TYPE).
 *     runningHeaderStyle: ParagraphStyle applied to the header frame text.
 *     runningFooterStyle: ParagraphStyle applied to the footer frame text.
 */
function setupMasterSpread(doc, docCfg, colors, styles) {
    var MM_TO_PT = 2.83465;
    var MIN_TOP_MARGIN_MM = 12.0;

    // ── 0. Acquire the first master spread and configure prefix/baseName ──
    // MasterSpread.name is read-only in InDesign (composed of namePrefix + "-" + baseName)
    var master = doc.masterSpreads.item(0);
    master.namePrefix = "B";
    master.baseName = "Booklet_Master";

    // ── 1. Define Running Header Text Variable ──
    // Links to the chapter title paragraph style so the header
    // updates automatically when a new chapter appears on a page.
    var headerVar;
    try {
        headerVar = doc.textVariables.itemByName("Booklet_Header_Var");
        // Force resolution — itemByName returns a specifier that may
        // not be valid until accessed. Accessing .name triggers the
        // validity check.
        var _testName = headerVar.name;
    } catch (e) {
        // Variable doesn't exist yet — create it
        headerVar = null;
    }

    if (headerVar === null) {
        headerVar = doc.textVariables.add({
            name: "Booklet_Header_Var",
            variableType: VariableTypes.MATCH_PARAGRAPH_STYLE_TYPE
        });
        headerVar.variableOptions.appliedParagraphStyle = styles.titleStyle;
        headerVar.variableOptions.searchStrategy = SearchStrategies.FIRST_ON_PAGE;
    }

    // ── 2. Configure each page of the master spread ──
    // Uses .count() — NEVER .length — per STEP-1.1 ExtendScript
    // collection rules.
    var pageCount = master.pages.count();
    for (var i = 0; i < pageCount; i++) {
        var mPage = master.pages.item(i);
        var pBounds = mPage.bounds; // [y0, x0, y1, x1]
        var pWidth = pBounds[3] - pBounds[1];
        var pHeight = pBounds[2] - pBounds[0];

        // ── 2a. Validate and compute margins ──
        // All four fields are required — no || fallback defaults.
        var mTop = Math.max(
            requireMarginField(docCfg.margins, "top_mm") * MM_TO_PT,
            MIN_TOP_MARGIN_MM * MM_TO_PT
        );
        var mBottom = requireMarginField(docCfg.margins, "bottom_mm") * MM_TO_PT;
        var mInside = requireMarginField(docCfg.margins, "inside_spine_mm") * MM_TO_PT;
        var mOutside = requireMarginField(docCfg.margins, "outside_mm") * MM_TO_PT;

        // ── 2b. Determine page side and compute left/right margins ──
        // Three-way check: SINGLE_SIDED, LEFT_HAND, RIGHT_HAND.
        // An earlier draft's two-way check (LEFT_HAND vs. "everything
        // else is RIGHT_HAND") silently applied asymmetric margins to
        // single-sided documents — now explicitly handled.
        var isSingleSided = (mPage.side === PageSideOptions.SINGLE_SIDED);
        var isLeft = !isSingleSided && (mPage.side === PageSideOptions.LEFT_HAND);

        var left, right;
        if (isSingleSided) {
            // Symmetric margins — no inside/outside spine distinction
            // applies without a facing page to bind against.
            var symmetricMargin = mOutside; // use outside_mm as uniform value
            left = symmetricMargin;
            right = pWidth - symmetricMargin;
        } else {
            // Facing pages: inside margin is on the spine side.
            // LEFT_HAND page: spine is on the RIGHT edge → left=outside, right boundary=pWidth-inside
            // RIGHT_HAND page: spine is on the LEFT edge → left=inside, right boundary=pWidth-outside
            left = isLeft ? mOutside : mInside;
            right = isLeft ? (pWidth - mInside) : (pWidth - mOutside);
        }

        // ── 2c. Header Ribbon Frame ──
        // Sits above the top margin line, vertically bottom-aligned.
        // Contains: Running Header Variable (SECTION_MARKER removed per STEP-7.1e)
        var hFrame = mPage.textFrames.add({
            geometricBounds: [mTop - 18, left, mTop - 4, right]
        });
        hFrame.textFramePreferences.verticalJustification =
            VerticalJustification.BOTTOM_ALIGN;
        hFrame.insertionPoints[0].textVariableInstances.add({
            associatedTextVariable: headerVar
        });
        hFrame.paragraphs.everyItem().applyParagraphStyle(
            styles.runningHeaderStyle, false
        );

        // ── 2d. Header Divider Line ──
        // Thin horizontal rule between header ribbon and body area.
        var hLine = mPage.graphicLines.add();
        hLine.geometricBounds = [mTop - 2, left, mTop - 2, right];
        hLine.strokeColor = colors.borderColor;
        hLine.strokeWeight = 0.5;

        // ── 2e. Footer Ribbon Frame (Auto Page Number) ──
        // Sits below the bottom margin line, vertically top-aligned.
        // Contains: configurable footer_text (default empty = only page number).
        // Hardcoded "Booklet_AI •" removed per STEP-7.1e.
        var fFrame = mPage.textFrames.add({
            geometricBounds: [
                pHeight - mBottom + 4,
                left,
                pHeight - mBottom + 18,
                right
            ]
        });
        fFrame.textFramePreferences.verticalJustification =
            VerticalJustification.TOP_ALIGN;

        var footerText = docCfg.footer_text || (docCfg.geometry && docCfg.geometry.footer_text) || "";
        if (footerText && footerText.length > 0) {
            fFrame.insertionPoints[0].contents = footerText + "   |   ";
            fFrame.insertionPoints[-1].contents = SpecialCharacters.AUTO_PAGE_NUMBER;
        } else {
            fFrame.insertionPoints[0].contents = SpecialCharacters.AUTO_PAGE_NUMBER;
        }
        fFrame.paragraphs.everyItem().applyParagraphStyle(
            styles.runningFooterStyle, false
        );

        // ── 2e.1 Page Numbering Digit Shaping (STEP-6.8) ──
        // Sets InDesign's native digit-shaping display property (DigitsTypeOptions.HINDI_DIGITS vs
        // DigitsTypeOptions.ARABIC_DIGITS) on the AUTO_PAGE_NUMBER marker, leaving the
        // automatic-numbering mechanism intact (AC-6.8.1, AC-6.8.2).
        // Scoped strictly to the footer frame marker, never applied globally to ordinary
        // document text, question numbers, or dates (AC-6.8.4).
        var numStyle = docCfg.numbering_style || docCfg.page_numbering_style || "arabic_indic";
        var isWestern = (numStyle === "western_arabic" || numStyle === "arabic");
        var targetDigits = null;
        if (typeof DigitsTypeOptions !== "undefined") {
            targetDigits = isWestern ? DigitsTypeOptions.ARABIC_DIGITS : DigitsTypeOptions.HINDI_DIGITS;
        } else {
            // Raw numeric enum values: ARABIC_DIGITS=1684627826, HINDI_DIGITS=1684629609
            targetDigits = isWestern ? 1684627826 : 1684629609;
        }

        if (targetDigits !== null) {
            try {
                // Primary: apply digit shaping directly to the AUTO_PAGE_NUMBER marker (last character)
                var pageNumMarker = fFrame.characters.item(-1);
                if (pageNumMarker && pageNumMarker.isValid) {
                    pageNumMarker.digitsType = targetDigits;
                }
            } catch (eMarker) {
                // Defensive fallback: apply to footer frame paragraphs if character-level indexing differs
                try {
                    fFrame.paragraphs.everyItem().digitsType = targetDigits;
                } catch (eFrame) {
                    // Non-ME edition without digitsType support
                }
            }
        }

        // ── 2f. Body Story Frame ──
        // The primary text frame for content injection. Labeled per-side
        // so findMasterFrameByLabelOnSide() (STEP-1.1) can retrieve the
        // correct left/right geometry during overflow.
        //
        // Validate that margins don't consume the entire page height
        // (pathological config guard — e.g. top_mm=300, bottom_mm=300
        // on A4 would produce zero/negative body height).
        var bodyTop = mTop + 6;
        var bodyBottom = pHeight - mBottom - 6;

        if (bodyBottom <= bodyTop) {
            throw new Error(
                "InvalidBodyFrameGeometryError: computed body frame height is " +
                "zero or negative (top=" + bodyTop + ", bottom=" + bodyBottom + "). " +
                "top_mm + bottom_mm leaves no usable body area for this page size \u2014 " +
                "reduce margins or increase page height."
            );
        }

        var bFrame = mPage.textFrames.add({
            geometricBounds: [bodyTop, left, bodyBottom, right]
        });
        bFrame.label = "Booklet_Body_Story";

        // ── 2g. Margin Guides (STEP-7.1e) ──
        // Set master page marginPreferences to match body frame margins
        // (inside/outside according to page side) so InDesign's guides equal real margins.
        var topMargin = bodyTop;
        var bottomMargin = mBottom + 6;
        var leftMargin = isSingleSided ? mOutside : (isLeft ? mOutside : mInside);
        var rightMargin = isSingleSided ? mOutside : (isLeft ? mInside : mOutside);

        try {
            mPage.marginPreferences.properties = {
                top: topMargin,
                bottom: bottomMargin,
                left: leftMargin,
                right: rightMargin,
                columnCount: 1,
                columnGutter: 0
            };
        } catch (eMarg) {
            try {
                mPage.marginPreferences.top = topMargin;
                mPage.marginPreferences.bottom = bottomMargin;
                mPage.marginPreferences.left = leftMargin;
                mPage.marginPreferences.right = rightMargin;
                mPage.marginPreferences.columnCount = 1;
            } catch (eMarg2) {}
        }
    }
}


// ─── Master Spread Resolver ───────────────────────────────────────────

/**
 * Retrieve the master spread whose baseName === "Booklet_Master".
 *
 * In InDesign, MasterSpread.name is read-only and composed of namePrefix + "-" + baseName
 * (e.g. "B-Booklet_Master"). itemByName resolution must NOT be used because it
 * fails or is unreliable across InDesign versions.
 *
 * @param {Document} doc - The active InDesign document.
 * @param {string} [targetBaseName] - Optional baseName to match (defaults to "Booklet_Master").
 * @returns {MasterSpread} The master spread whose baseName matches.
 * @throws {Error} MasterNotFoundError if no matching master spread exists.
 */
function getBookletMaster(doc, targetBaseName) {
    var expected = targetBaseName || "Booklet_Master";
    if (expected.indexOf("-") !== -1) {
        var parts = expected.split("-");
        expected = parts.slice(1).join("-");
    }
    var count = (typeof doc.masterSpreads.count === "function") ?
        doc.masterSpreads.count() :
        (doc.masterSpreads.length || 0);
    for (var i = 0; i < count; i++) {
        var ms = doc.masterSpreads.item(i);
        if (ms && ms.baseName === expected) {
            return ms;
        }
    }
    throw new Error(
        "MasterNotFoundError: master spread with baseName '" + expected +
        "' not found in document."
    );
}


// ─── Filler Page Insertion ────────────────────────────────────────────

/**
 * Add blank filler pages to bring the document's total page count up
 * to the binding method's required multiple.
 *
 * Called by pagination_sync.py after all content generation is complete.
 *
 * Distinguishes two failure modes:
 * 1. Content overrun: actual pages > targetPageCount → the price quote
 *    is stale and must be recalculated (ContentExceedsEstimateError).
 * 2. Insertion bug: the while loop completed but page count doesn't
 *    match target (FillerPageMismatchError — a genuine code bug).
 *
 * Filler pages inherit the Booklet_Master's header/footer/page-number
 * but carry no body-frame content override — they are "imposition filler
 * pages with normal running header/footer, no body content."
 *
 * @param {Document} doc - The active InDesign document.
 * @param {number} targetPageCount - The imposed page count target.
 * @param {string} masterName - Name of the master spread to apply
 *     (kept for backward compatibility; resolved via getBookletMaster by baseName).
 * @throws {Error} ContentExceedsEstimateError if actual > target.
 * @throws {Error} FillerPageMismatchError if insertion loop fails.
 */
function addBlankFillerPages(doc, targetPageCount, masterName) {
    var currentCount = doc.pages.count();

    if (currentCount > targetPageCount) {
        // Content genuinely overran the pre-layout estimate — this is a
        // pricing/estimation staleness issue, not a filler-page defect.
        throw new Error(
            "ContentExceedsEstimateError: Actual content requires " +
            currentCount + " pages, exceeding the pre-layout estimate of " +
            targetPageCount + ". This is not a filler-page error \u2014 the price " +
            "quote must be recalculated against the actual page count via " +
            "pricing_engine.imposed_page_count(" + currentCount +
            ", binding) before proceeding."
        );
    }

    // Insert filler pages until we reach the target
    while (currentCount < targetPageCount) {
        var lastPage = doc.pages.item(-1);
        var newPage = doc.pages.add(LocationOptions.AFTER, lastPage);
        newPage.appliedMaster = getBookletMaster(doc);

        // Apply marginPreferences to filler page matching master body frame (STEP-7.1e)
        try {
            var mSpread = newPage.appliedMaster;
            var mBodyFrame = findMasterFrameByLabelOnSide(mSpread, "Booklet_Body_Story", newPage.side);
            var mgb = mBodyFrame.geometricBounds;
            var pHeightPt = newPage.bounds[2] - newPage.bounds[0];
            var pWidthPt = newPage.bounds[3] - newPage.bounds[1];
            newPage.marginPreferences.properties = {
                top: mgb[0],
                bottom: pHeightPt - mgb[2],
                left: mgb[1],
                right: pWidthPt - mgb[3],
                columnCount: 1,
                columnGutter: 0
            };
        } catch (eFMP) {}

        // Filler pages carry no body-frame override — no injected body
        // content appears. The header ribbon, footer ribbon, and
        // AUTO_PAGE_NUMBER are inherited automatically from the master
        // and WILL appear on these pages, consistent with every other
        // page in the booklet.
        currentCount++;
    }

    // Post-condition: verify the insertion loop actually reached target
    var finalCount = doc.pages.count();
    if (finalCount !== targetPageCount) {
        throw new Error(
            "FillerPageMismatchError: expected " + targetPageCount +
            " pages after filler insertion, got " + finalCount +
            " \u2014 filler insertion did not reach the target count " +
            "(this indicates a bug in the insertion loop itself, distinct " +
            "from the pre-layout overrun case handled above)."
        );
    }
}


/**
 * Helper: return the current page count of the document.
 * Called by pagination_sync.py's re-quote flow to get the actual count.
 *
 * @param {Document} doc - The active InDesign document.
 * @returns {number} The current number of pages.
 */
function getPageCount(doc) {
    return doc.pages.count();
}
