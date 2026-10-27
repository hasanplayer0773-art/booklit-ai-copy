/*
    apply_question_callout.jsx — Typography & Ministerial Question Callout
    STEP-2.3

    Provides two core functions:
    1. resolveAndApplyStyle() — Maps a blockType string to its InDesign
       paragraph style and applies it to a text range. Fail-Fast on
       unmapped types or missing template styles (AC-2.3.2).
    2. applyQuestionCallout() — Applies the ministerial question side-bar
       callout to a paragraph range. Uses paragraph border-and-shading +
       rightIndent (reflow-safe), not a drawn shape (AC-2.3.3).

    RTL Indent Direction Note:
    In Arabic (RTL) paragraphs, the leading/indented edge is the RIGHT
    edge, not the left. rightIndent combined with borderRightWeight/Color
    is the correct pairing for a leading-edge side-bar. leftIndent would
    push text away from the trailing (left) edge while leaving the
    reading-start (right) edge flush against the margin — backwards.

    Dependencies:
        - lib/json2.jsx (JSON polyfill for ExtendScript)
        - payload_loader.jsx (file-based IPC, if invoked via payload)
        - build_master_spread.jsx (provides the body story frame)
        - inject_tables_and_math.jsx (math/table injection subroutines)

    STEP-1.1 reference: Zero-Mutation, COM threading, modal suppression
*/


// ─── Style Map (JSX-side mirror of BLOCK_STYLE_MAP) ──────────────────
// Must be kept in sync with app/automation/style_dispatch.py.
// Any addition to the Python BLOCK_STYLE_MAP must be reflected here.

var BLOCK_STYLE_MAP_JSX = {
    "chapter_header":               "ChapterHeaderStyle",
    "arabic_text":                  "BodyArabicStyle",
    "english_text":                 "BodyLatinStyle",
    "ministry_question":            "MinistryQuestionStyle",
    "math_formula":                 "MathFormulaStyle",
    "chemistry_formula":            "ChemistryFormulaStyle",
    "image_prompt":                 "ImagePlaceholderStyle",
    "list_item":                    "ListItemStyle",
    "legacy_equation_placeholder":  "LegacyEquationPlaceholderStyle"
};


// ─── Ministerial Question Callout ─────────────────────────────────────

/**
 * Apply the ministerial question side-bar callout to a paragraph range.
 *
 * Uses paragraph border-and-shading + rightIndent — reflow-safe: the
 * callout moves with the text automatically when preceding content
 * changes length, unlike a drawn rectangle that needs manual
 * repositioning (AC-2.3.3).
 *
 * Uses rightIndent + borderRightWeight/Color — the correct pairing for
 * an RTL leading-edge side-bar (right edge is the reading-start edge
 * in Arabic paragraphs; leftIndent/a left-side rule would be reversed)
 * (AC-2.3.5).
 *
 * @param {TextObject} targetRange - The text range to apply the callout to.
 * @param {Object} colors - Color palette object. Must contain:
 *     primaryColor: Swatch for the colored side-bar border.
 * @param {Object} styles - Style references object. Must contain:
 *     ministryQuestionStyle: ParagraphStyle for ministry question text.
 */
function applyQuestionCallout(targetRange, colors, styles) {
    // Apply the ministry question paragraph style first
    targetRange.paragraphs.everyItem().applyParagraphStyle(
        styles.ministryQuestionStyle, false
    );

    // Padding offsets for both paragraph shading and border
    var TOP_OFFSET = 4.0;
    var BOTTOM_OFFSET = 4.0;
    var LEFT_OFFSET = 6.0;
    var RIGHT_OFFSET = 6.0;

    // Apply callout formatting to each paragraph individually
    var pElements = targetRange.paragraphs.everyItem().getElements();
    for (var i = 0; i < pElements.length; i++) {
        var p = pElements[i];

        // Right-side indent — pushes text away from the right (leading)
        // edge to make room for the colored border bar.
        p.rightIndent = 14.0;

        // Colored side-bar on the right (reading-start) edge.
        // Uses paragraph border-and-shading feature — reflow-safe,
        // unlike an anchored/drawn rectangle.
        p.paragraphBorderOn = true;
        p.paragraphBorderTopLineWeight = 0;
        p.paragraphBorderBottomLineWeight = 0;
        p.paragraphBorderLeftLineWeight = 0;
        p.paragraphBorderRightLineWeight = 3.0;
        p.paragraphBorderColor = colors.primaryColor;

        // Border offsets so the shaded area has padding and the text does not touch the edges
        p.paragraphBorderTopOffset = TOP_OFFSET;
        p.paragraphBorderBottomOffset = BOTTOM_OFFSET;
        p.paragraphBorderLeftOffset = LEFT_OFFSET;
        p.paragraphBorderRightOffset = RIGHT_OFFSET;

        // Light background tint for visual hierarchy
        p.paragraphShadingOn = true;
        p.paragraphShadingColor = colors.primaryColor;
        p.paragraphShadingTint = 8.0;

        // Padding inside the shading box
        p.paragraphShadingTopOffset = TOP_OFFSET;
        p.paragraphShadingBottomOffset = BOTTOM_OFFSET;
        p.paragraphShadingLeftOffset = LEFT_OFFSET;
        p.paragraphShadingRightOffset = RIGHT_OFFSET;
    }
}


// ─── Style Resolution & Application ──────────────────────────────────

/**
 * Resolve a blockType to its InDesign paragraph style and apply it
 * to a text range within the story. Fail-Fast on unmapped block types
 * or missing template styles (AC-2.3.1, AC-2.3.2, AC-2.3.4).
 *
 * For ministry_question blocks, additionally applies the colored
 * side-bar callout treatment (AC-2.3.3, AC-2.3.5).
 *
 * @param {Story} story - The InDesign story containing the text.
 * @param {number} start - Character index of the range start.
 * @param {number} end - Character index of the range end (exclusive).
 * @param {string} blockType - The block_type string (must be a key
 *     in BLOCK_STYLE_MAP_JSX).
 * @param {Object} colors - Color palette object (passed through to
 *     applyQuestionCallout for ministry_question blocks).
 * @throws {Error} StyleMappingError if blockType has no entry in
 *     BLOCK_STYLE_MAP_JSX.
 * @throws {Error} StyleMappingError if the resolved paragraph style
 *     does not exist in the active document's template.
 */
function resolveAndApplyStyle(story, start, end, blockType, colors) {
    // 1. Resolve blockType → style name via the static map
    var styleName = BLOCK_STYLE_MAP_JSX[blockType];
    if (!styleName) {
        throw new Error(
            "StyleMappingError: no InDesign style registered for " +
            "block_type '" + blockType + "'. Every block_type must " +
            "have an explicit entry in BLOCK_STYLE_MAP — add a " +
            "mapping, do not use a fallback."
        );
    }

    // 2. Retrieve the paragraph style object from the document
    var pStyle = app.activeDocument.paragraphStyles.itemByName(styleName);
    if (!pStyle.isValid) {
        throw new Error(
            "StyleMappingError: paragraph style '" + styleName +
            "' not found in template. This style must exist in the " +
            ".idml template's paragraph styles panel before generation " +
            "— detected at document-load time (AC-2.3.4), not " +
            "mid-injection."
        );
    }

    // 3. Build the character range — Math.max guards against empty range
    var targetRange = story.characters.itemByRange(
        start, Math.max(start, end - 1)
    );

    // 4. Apply the paragraph style
    targetRange.applyParagraphStyle(pStyle, false);

    // 5. For ministry_question blocks, apply the callout treatment
    if (blockType === "ministry_question") {
        applyQuestionCallout(
            targetRange,
            colors,
            { ministryQuestionStyle: pStyle }
        );
    }
}


// ─── Template Preflight ──────────────────────────────────────────────

/**
 * Verify that all required paragraph styles exist in the active
 * document. Called once at document-load time (AC-2.3.4).
 *
 * @param {Array<string>} requiredStyles - List of paragraph style
 *     names that must exist in the template.
 * @returns {Object} Result object with 'status' and optionally
 *     'missing_styles' keys.
 */
function preflightTemplateStyles(requiredStyles) {
    var doc = app.activeDocument;
    var missing = [];

    for (var i = 0; i < requiredStyles.length; i++) {
        var styleName = requiredStyles[i];
        var style = doc.paragraphStyles.itemByName(styleName);
        if (!style.isValid) {
            missing.push(styleName);
        }
    }

    if (missing.length > 0) {
        return {
            status: "error",
            missing_styles: missing
        };
    }

    return { status: "ok" };
}
