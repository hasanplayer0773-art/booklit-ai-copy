/*
    generate_booklet.jsx — Real Booklet Generation Script
    STEP-7.1 / STEP-7.1b

    Converts a BookletDocument JSON payload (blocks, tables, PageGeometry,
    styles config) into a real InDesign document and PDF.

    Workflow:
    1. #include json2.jsx via $.fileName-relative path
    2. Read payload from file
    3. Set UserInteractionLevels.NEVER_INTERACT (prevent modal deadlock)
    4. Create document from PageGeometry (facing pages, page size, bleeds)
    5. Set doc.viewPreferences.rulerOrigin = RulerOrigin.PAGE_ORIGIN
       (page-relative frame coordinates across left/right pages)
    6. Programmatically create paragraph styles from BLOCK_STYLE_MAP with
       strict checks:
       - Adobe World-Ready Paragraph Composer required for all Arabic styles (throws on failure)
       - MissingFontError thrown if font is not found (no silent Arial substitution)
       - StyleConfigError thrown on direction/composer/font failure
    7. Set up Master Spread ("Booklet_Master") with header/footer ribbons
       and labeled body frames ("Booklet_Body_Story")
    8. Override first page body frame and thread story
    9. Inject blocks atomically with insert-style-release:
       - injectMathFormulaBlock (enforced LTR)
       - injectRTLTable (proportional columns, alternating rows, RTL direction)
       - resolveAndApplyStyle (maps block_type to style, applies ministry callout)
       - ensureFrameCapacity (MAX_PAGES_SAFETY = 500, side-matching body frame override)
    10. Compute imposed target from ACTUAL doc.pages.count() after injection
        (using IMPOSITION_MULTIPLE unified with pricing_engine.py) and call addBlankFillerPages
    11. Save .indd and export high-quality PDF
    12. Close document via doc.close(SaveOptions.NO) in finally block
    13. Write result JSON (actual_page_count, imposed_page_count, page_count, composer_ok, warnings)
*/

#include "lib/json2.jsx"
#include "payload_loader.jsx"
#include "build_master_spread.jsx"
#include "inject_tables_and_math.jsx"
#include "apply_question_callout.jsx"

// ─── Ensure Libraries Loaded at Runtime ────────────────────────────────
if (typeof JSON === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _json2 = File(_scriptDir + "/lib/json2.jsx");
    if (_json2.exists) {
        $.evalFile(_json2);
    }
}
if (typeof loadPayloadFromFile === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _loader = File(_scriptDir + "/payload_loader.jsx");
    if (_loader.exists) {
        $.evalFile(_loader);
    }
}
if (typeof setupMasterSpread === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _ms = File(_scriptDir + "/build_master_spread.jsx");
    if (_ms.exists) {
        $.evalFile(_ms);
    }
}
if (typeof requireMarginField === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _msMargin = File(_scriptDir + "/build_master_spread.jsx");
    if (_msMargin.exists) {
        $.evalFile(_msMargin);
    }
}
if (typeof injectMathFormulaBlock === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _itm = File(_scriptDir + "/inject_tables_and_math.jsx");
    if (_itm.exists) {
        $.evalFile(_itm);
    }
}
if (typeof resolveAndApplyStyle === "undefined") {
    var _scriptDir = File($.fileName).parent;
    var _aqc = File(_scriptDir + "/apply_question_callout.jsx");
    if (_aqc.exists) {
        $.evalFile(_aqc);
    }
}


// ─── STEP-7.1g Glyph Coverage Constants ─────────────────────────────────
var GREEK_MATH_GREP_PATTERN = "[\\x{0370}-\\x{03FF}\\x{2070}-\\x{209F}\\x{2200}-\\x{22FF}\\x{2190}-\\x{21FF}]";
var GREEK_MATH_JS_REGEX = /[\u0370-\u03FF\u2070-\u209F\u2200-\u22FF\u2190-\u21FF]/g;


// ─── Imposition Multiples (Unified with pricing_engine.IMPOSITION_MULTIPLE) ──

var IMPOSITION_MULTIPLE_MAP = {
    "saddle_stitch": 4,
    "perfect_bound": 1,
    "spiral":        1
};

function getImpositionMultiple(binding) {
    if (IMPOSITION_MULTIPLE_MAP.hasOwnProperty(binding)) {
        return IMPOSITION_MULTIPLE_MAP[binding];
    }
    throw new Error("InvalidBindingError: unknown binding method '" + binding + "'");
}


// ─── Plan STEP-1.1 Frame Capacity & Side Matching Implementations ──────

/**
 * Locate the labeled master body frame on the matching side of the master spread.
 *
 * Implemented EXACTLY as in plan STEP-1.1 lines 60-70.
 *
 * @param {MasterSpread} masterSpread - The master spread to search.
 * @param {string} labelName - Label identifier ("Booklet_Body_Story").
 * @param {PageSideOptions} side - Page side (LEFT_HAND, RIGHT_HAND, SINGLE_SIDED).
 * @returns {TextFrame} The matching master text frame.
 * @throws {Error} MasterFrameNotFoundError if not found.
 */
function findMasterFrameByLabelOnSide(masterSpread, labelName, side) {
    for (var i = 0; i < masterSpread.pages.count(); i++) {
        var mPage = masterSpread.pages.item(i);
        if (mPage.side !== side) continue;
        var frames = mPage.textFrames;
        for (var f = 0; f < frames.count(); f++) {
            if (frames.item(f).label === labelName) return frames.item(f);
        }
    }
    throw new Error(
        "MasterFrameNotFoundError: no frame labeled '" + labelName +
        "' on the " + side + " master page."
    );
}

var MAX_PAGES_SAFETY = 500; // guard against runaway loops on malformed input

/**
 * Ensure the body story frame has enough capacity to hold injected text,
 * adding new pages and threading frames as needed.
 *
 * Implemented EXACTLY as in plan STEP-1.1 lines 74-100.
 *
 * @param {Document} doc - The active InDesign document.
 * @param {TextFrame} currentFrame - The current page's body frame.
 * @param {string} masterName - Applied master spread name ("Booklet_Master").
 * @returns {TextFrame} The new active body text frame.
 * @throws {Error} PageOverflowSafetyError if page count exceeds MAX_PAGES_SAFETY.
 */
function ensureFrameCapacity(doc, currentFrame, masterName) {
    var guard = 0;
    while (currentFrame.overflows && guard < MAX_PAGES_SAFETY) {
        var newPage = doc.pages.add(LocationOptions.AFTER, currentFrame.parentPage);
        newPage.appliedMaster = getBookletMaster(doc);

        // Locate the labeled master body frame ON THE SAME SIDE as the
        // new page, then override it to get an editable, page-level
        // frame object — item(0) is unsafe both because master items
        // are locked and because ordinal position does not guarantee
        // "the body frame" (header/footer frames exist on the same
        // master); side-matching is required because left/right master
        // pages have different margins.
        var masterBodyFrame = findMasterFrameByLabelOnSide(
            newPage.appliedMaster, "Booklet_Body_Story", newPage.side
        );

        var newFrame = masterBodyFrame.override(newPage);
        newFrame.label = "Booklet_Body_Story";

        currentFrame.nextTextFrame = newFrame;
        currentFrame = newFrame;
        guard++;
    }
    if (guard >= MAX_PAGES_SAFETY) {
        throw new Error(
            "PageOverflowSafetyError: exceeded " + MAX_PAGES_SAFETY +
            " pages — likely malformed input or infinite overflow loop."
        );
    }
    return currentFrame;
}


// ─── Color & Swatch Helpers ───────────────────────────────────────────

function getOrCreateColor(doc, colorName, rgbValues) {
    var color = doc.colors.itemByName(colorName);
    if (!color.isValid) {
        color = doc.colors.add({
            name: colorName,
            model: ColorModel.PROCESS,
            space: ColorSpace.RGB,
            colorValue: rgbValues
        });
    }
    return color;
}


// ─── Style Definition Helpers ─────────────────────────────────────────

/**
 * Create or retrieve a paragraph style with strict error handling.
 *
 * Only swallows errors for purely cosmetic properties (spaceBefore, spaceAfter, etc.).
 * Critical properties (paragraphDirection, composer, appliedFont) MUST throw StyleConfigError.
 */
function getOrCreateParagraphStyle(doc, styleName, properties) {
    var style = doc.paragraphStyles.itemByName(styleName);
    if (!style.isValid) {
        style = doc.paragraphStyles.add({ name: styleName });
    }
    if (properties) {
        for (var prop in properties) {
            if (properties.hasOwnProperty(prop)) {
                try {
                    style[prop] = properties[prop];
                } catch (eProp) {
                    // Critical non-cosmetic properties MUST NOT fail silently (STEP-7.1b)
                    if (prop === "paragraphDirection" || prop === "composer" || prop === "appliedFont") {
                        throw new Error(
                            "StyleConfigError: " + styleName + "." + prop +
                            " failed: " + eProp.message
                        );
                    }
                    // Swallow only purely cosmetic properties (spaceBefore, spaceAfter, etc.)
                }
            }
        }
    }
    return style;
}

/**
 * Apply font to paragraph style without silent substitution.
 *
 * Plan STEP-1.1 / STEP-2.3 explicitly forbids silent font fallback (e.g. falling back to Arial).
 * If the font is missing from InDesign, throws MissingFontError immediately.
 */
function applyFontSafe(style, fontName) {
    if (!fontName) {
        throw new Error(
            "MissingFontError: fontName is undefined or empty for style '" + style.name + "'."
        );
    }
    try {
        var f = app.fonts.itemByName(fontName);
        if (!f.isValid) {
            throw new Error("Font not found in app.fonts");
        }
        style.appliedFont = f;
    } catch (e1) {
        try {
            style.appliedFont = fontName;
        } catch (e2) {
            throw new Error(
                "MissingFontError: required font '" + fontName +
                "' is not installed or available in InDesign for style '" +
                style.name + "': " + e2.message
            );
        }
    }
}


/**
 * Create or ensure all 8 BLOCK_STYLE_MAP styles (plus supporting styles) exist.
 *
 * Configures Arabic fonts (fails fast if missing), RTL paragraph direction,
 * Adobe World-Ready Paragraph Composer on all Arabic/RTL styles,
 * and strict LTR direction for math/chemistry/latin styles.
 */
function ensureAllStyles(doc, stylesCfg) {
    var bodyFont = stylesCfg.body_font || "Tajawal";
    var mathFont = stylesCfg.math_font || "Cambria Math";

    var rtlDirection = ParagraphDirectionOptions.RIGHT_TO_LEFT_DIRECTION;
    var ltrDirection = ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
    var wrComposer = "Adobe World-Ready Paragraph Composer";

    // 1. ChapterHeaderStyle (Arabic/RTL)
    var chStyle = getOrCreateParagraphStyle(doc, "ChapterHeaderStyle", {
        pointSize: 16.0,
        leading: 22.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        spaceBefore: 12.0,
        spaceAfter: 8.0
    });
    applyFontSafe(chStyle, bodyFont);

    // 2. BodyArabicStyle (Arabic/RTL)
    var baStyle = getOrCreateParagraphStyle(doc, "BodyArabicStyle", {
        pointSize: stylesCfg.body_size_pt || 11.5,
        leading: stylesCfg.body_leading_pt || 17.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        spaceAfter: 6.0
    });
    applyFontSafe(baStyle, bodyFont);

    // 3. BodyLatinStyle (Latin/LTR)
    var blStyle = getOrCreateParagraphStyle(doc, "BodyLatinStyle", {
        pointSize: 11.0,
        leading: 16.0,
        paragraphDirection: ltrDirection,
        spaceAfter: 6.0
    });
    applyFontSafe(blStyle, "Arial");

    // 4. MinistryQuestionStyle (Arabic/RTL)
    var mqStyle = getOrCreateParagraphStyle(doc, "MinistryQuestionStyle", {
        pointSize: 12.0,
        leading: 18.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        spaceBefore: 8.0,
        spaceAfter: 8.0
    });
    applyFontSafe(mqStyle, bodyFont);

    // 5. MathFormulaStyle (Forced LTR)
    var mfStyle = getOrCreateParagraphStyle(doc, "MathFormulaStyle", {
        pointSize: stylesCfg.math_size_pt || 12.0,
        leading: stylesCfg.math_leading_pt || 18.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 6.0,
        spaceAfter: 6.0
    });
    applyFontSafe(mfStyle, mathFont);

    // 6. ChemistryFormulaStyle (Forced LTR)
    var cfStyle = getOrCreateParagraphStyle(doc, "ChemistryFormulaStyle", {
        pointSize: 12.0,
        leading: 18.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 6.0,
        spaceAfter: 6.0
    });
    applyFontSafe(cfStyle, mathFont);

    // 7. ImagePlaceholderStyle (Arabic/RTL)
    var ipStyle = getOrCreateParagraphStyle(doc, "ImagePlaceholderStyle", {
        pointSize: 10.0,
        leading: 14.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        spaceBefore: 4.0,
        spaceAfter: 4.0
    });
    applyFontSafe(ipStyle, bodyFont);

    // 8. ListItemStyle (Arabic/RTL)
    var liStyle = getOrCreateParagraphStyle(doc, "ListItemStyle", {
        pointSize: 11.5,
        leading: 17.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        leftIndent: 12.0,
        spaceAfter: 4.0
    });
    applyFontSafe(liStyle, bodyFont);

    // 9. LegacyEquationPlaceholderStyle
    var legStyle = getOrCreateParagraphStyle(doc, "LegacyEquationPlaceholderStyle", {
        pointSize: 10.0,
        leading: 14.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 4.0,
        spaceAfter: 4.0
    });
    applyFontSafe(legStyle, "Courier New");

    // Supporting styles for headers, footers, tables (Arabic/RTL)
    var rhStyle = getOrCreateParagraphStyle(doc, "RunningHeaderStyle", {
        pointSize: 9.0,
        leading: 12.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer
    });
    applyFontSafe(rhStyle, bodyFont);

    var rfStyle = getOrCreateParagraphStyle(doc, "RunningFooterStyle", {
        pointSize: 9.0,
        leading: 12.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer
    });
    applyFontSafe(rfStyle, bodyFont);

    var paperColor = doc.swatches.itemByName("Paper");
    var thStyle = getOrCreateParagraphStyle(doc, "TableHeaderStyle", {
        pointSize: stylesCfg.table_header_size_pt || 10.5,
        leading: 14.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer,
        fillColor: (paperColor && paperColor.isValid) ? paperColor : "Paper"
    });
    applyFontSafe(thStyle, bodyFont);

    var tcStyle = getOrCreateParagraphStyle(doc, "TableCellStyle", {
        pointSize: stylesCfg.table_cell_size_pt || 10.0,
        leading: 13.0,
        paragraphDirection: rtlDirection,
        composer: wrComposer
    });
    applyFontSafe(tcStyle, bodyFont);

    // 10. Greek / Math Character Style & GREP Styles (STEP-7.1e)
    // Tajawal has no Greek glyphs, so θ etc. render as missing-glyph boxes.
    // Create character style "GreekMathChar" and add nested GREP style to Arabic styles.
    var greekCharStyle = doc.characterStyles.itemByName("GreekMathChar");
    if (!greekCharStyle.isValid) {
        greekCharStyle = doc.characterStyles.add({ name: "GreekMathChar" });
    }
    applyFontSafe(greekCharStyle, mathFont);

    var arabicStylesForGrep = [baStyle, chStyle, mqStyle, liStyle, thStyle, tcStyle];
    for (var gIdx = 0; gIdx < arabicStylesForGrep.length; gIdx++) {
        var pStyle = arabicStylesForGrep[gIdx];
        var grepStyle = pStyle.nestedGrepStyles.add();
        grepStyle.appliedCharacterStyle = greekCharStyle;
        grepStyle.grepExpression = GREEK_MATH_GREP_PATTERN;
    }

    return {
        titleStyle: chStyle,
        chapterHeaderStyle: chStyle,
        bodyArabicStyle: baStyle,
        bodyLatinStyle: blStyle,
        ministryQuestionStyle: mqStyle,
        mathFormulaStyle: mfStyle,
        chemistryFormulaStyle: cfStyle,
        imagePlaceholderStyle: ipStyle,
        listItemStyle: liStyle,
        legacyStyle: legStyle,
        runningHeaderStyle: rhStyle,
        runningFooterStyle: rfStyle,
        tableHeaderStyle: thStyle,
        tableCellStyle: tcStyle,
        greekMathCharStyle: greekCharStyle
    };
}


// ─── Usable Frame Width Helper ────────────────────────────────────────

/**
 * Compute the usable text width of a text frame.
 *
 * Implemented per STEP-7.1e: computes actual width from frame's geometricBounds
 * (x1 - x0) minus its text frame inset spacings.
 *
 * @param {TextFrame} frame - The text frame to measure.
 * @returns {number} Usable width in points.
 */
function getFrameUsableWidth(frame) {
    var gb = frame.geometricBounds; // [y0, x0, y1, x1]
    var frameWidth = gb[3] - gb[1];
    var leftInset = 0;
    var rightInset = 0;
    if (frame.textFramePreferences) {
        var insets = frame.textFramePreferences.insetSpacing;
        if (typeof insets === "number") {
            leftInset = insets;
            rightInset = insets;
        } else if (insets instanceof Array || (insets && typeof insets.length === "number")) {
            if (insets.length === 4) {
                leftInset = insets[1];
                rightInset = insets[3];
            } else if (insets.length === 1) {
                leftInset = insets[0];
                rightInset = insets[0];
            }
        }
    }
    var usable = frameWidth - leftInset - rightInset;
    return (usable > 0) ? usable : frameWidth;
}


// ─── Main Generation Function ─────────────────────────────────────────

function generateBookletMain() {
    var warnings = [];
    var resultPath = null;
    var doc = null;

    try {
        // ── 1. Modal Dialog Deadlock Prevention ─────────────────────────
        app.scriptPreferences.userInteractionLevel =
            UserInteractionLevels.NEVER_INTERACT;

        // ── 2. Resolve Payload File Path ────────────────────────────────
        var payloadPath = null;
        if (typeof arguments !== "undefined" && arguments.length > 0 && arguments[0]) {
            payloadPath = arguments[0];
        }
        if (!payloadPath && app.scriptArgs.isDefined("payloadFile")) {
            payloadPath = app.scriptArgs.getValue("payloadFile");
        }
        if (!payloadPath && app.scriptArgs.isDefined("payload_file_path")) {
            payloadPath = app.scriptArgs.getValue("payload_file_path");
        }
        if (!payloadPath) {
            throw new Error("GenerateBookletError: No payload file path provided in script arguments.");
        }

        // ── 3. Read Payload ─────────────────────────────────────────────
        var payload = loadPayloadFromFile(payloadPath);
        resultPath = payload.result_file_path || (payload.output_dir + "/" + (payload.doc_id || "booklet") + "_result.json");

        var geometry = payload.geometry;
        var stylesCfg = payload.styles || {};

        // ── 4. Create Document from PageGeometry ────────────────────────
        doc = app.documents.add();
        doc.viewPreferences.horizontalMeasurementUnits = MeasurementUnits.POINTS;
        doc.viewPreferences.verticalMeasurementUnits = MeasurementUnits.POINTS;

        // Set rulerOrigin to RulerOrigin.PAGE_ORIGIN right after creating the document
        // and BEFORE setupMasterSpread. By default in facing-page spreads, InDesign's ruler origin
        // is SPREAD_ORIGIN (zero at the left page's top-left corner), which causes coordinate bounds
        // on right-hand pages to be offset horizontally by the page width. PAGE_ORIGIN ensures
        // [y0, x0, y1, x1] bounds are strictly page-relative on both left and right master pages.
        doc.viewPreferences.rulerOrigin = RulerOrigin.PAGE_ORIGIN;

        with (doc.documentPreferences) {
            pageHeight = geometry.height_pt + "pt";
            pageWidth = geometry.width_pt + "pt";
            facingPages = (geometry.facing_pages !== false);
            pagesPerDocument = 1;
            pageBinding = PageBindingOptions.RIGHT_TO_LEFT;
            if (typeof geometry.bleed_mm === "number" && geometry.bleed_mm > 0) {
                var bleedPt = geometry.bleed_mm * 2.83465;
                documentBleedTopOffset = bleedPt + "pt";
                documentBleedBottomOffset = bleedPt + "pt";
                documentBleedInsideOrLeftOffset = bleedPt + "pt";
                documentBleedOutsideOrRightOffset = bleedPt + "pt";
            }
        }

        // ── 5. Setup Palette Colors ─────────────────────────────────────
        var colors = {
            primaryColor: getOrCreateColor(doc, "Booklet_Primary", [24, 76, 120]),
            borderColor: getOrCreateColor(doc, "Booklet_Border", [180, 190, 200]),
            tableBorderColor: getOrCreateColor(doc, "Booklet_TableBorder", [200, 205, 210]),
            tableAltColor: getOrCreateColor(doc, "Booklet_TableAlt", [245, 247, 250]),
            paperColor: doc.swatches.itemByName("Paper")
        };

        // ── 6. Create Paragraph Styles (World-Ready Composer + Fonts) ───
        var styles = ensureAllStyles(doc, stylesCfg);

        // ── 7. Configure Master Spread ──────────────────────────────────
        geometry.footer_text = payload.footer_text || (payload.pages && payload.pages[0] && payload.pages[0].footer_text) || geometry.footer_text || "";
        setupMasterSpread(doc, geometry, colors, styles);

        // ── 8. Initialize First Page Body Frame ─────────────────────────
        var firstPage = doc.pages.item(0);
        firstPage.appliedMaster = getBookletMaster(doc);

        var masterBodyFrame = findMasterFrameByLabelOnSide(
            firstPage.appliedMaster, "Booklet_Body_Story", firstPage.side
        );

        var currentFrame = masterBodyFrame.override(firstPage);
        currentFrame.label = "Booklet_Body_Story";
        var story = currentFrame.parentStory;

        // Calculate available body frame width for tables using requireMarginField (no fallbacks)
        var pBounds = firstPage.bounds;
        var pWidth = pBounds[3] - pBounds[1];
        var mInside = requireMarginField(geometry.margins, "inside_spine_mm") * 2.83465;
        var mOutside = requireMarginField(geometry.margins, "outside_mm") * 2.83465;
        var bodyWidthPt = pWidth - mInside - mOutside;
        if (bodyWidthPt <= 0) {
            throw new Error(
                "InvalidBodyFrameGeometryError: computed body frame width is zero or negative (" +
                bodyWidthPt + "pt) — inside_spine_mm + outside_mm leaves no usable area."
            );
        }

        // ── 9. Inject Content Blocks & Tables ───────────────────────────
        // Build ordered list of items
        var items = [];
        if (payload.items && payload.items.length > 0) {
            items = payload.items;
        } else if (payload.pages && payload.pages.length > 0) {
            for (var p = 0; p < payload.pages.length; p++) {
                var pg = payload.pages[p];
                if (pg.blocks) {
                    for (var b = 0; b < pg.blocks.length; b++) {
                        items.push(pg.blocks[b]);
                    }
                }
                if (pg.tables) {
                    for (var t = 0; t < pg.tables.length; t++) {
                        var tblItem = pg.tables[t];
                        tblItem.item_type = "table";
                        items.push(tblItem);
                    }
                }
            }
        } else if (payload.blocks && payload.blocks.length > 0) {
            items = payload.blocks;
            if (payload.tables) {
                for (var tb = 0; tb < payload.tables.length; tb++) {
                    var tbObj = payload.tables[tb];
                    tbObj.item_type = "table";
                    items.push(tbObj);
                }
            }
        }

        for (var idx = 0; idx < items.length; idx++) {
            var item = items[idx];
            var isTable = (item.item_type === "table" || (!item.block_type && (item.headers || item.rows)));

            if (isTable) {
                // RTL Table Injection: compute width from actual body frame (STEP-7.1e)
                var currentTableWidth = getFrameUsableWidth(currentFrame);
                injectRTLTable(story, item, currentTableWidth, colors, styles);
            } else if (item.block_type === "math_formula") {
                // Math Formula Injection (strict LTR - SVG image or placeholder)
                injectMathFormulaBlock(story, item, styles);
            } else {
                // Atomic insert-style-release for standard blocks
                var blockText = item.raw_content || "";
                var start = story.insertionPoints[-1].index;
                story.insertionPoints[-1].contents = blockText + "\r";
                var end = story.insertionPoints[-1].index;

                resolveAndApplyStyle(
                    story,
                    start,
                    end,
                    item.block_type || "arabic_text",
                    colors
                );
            }

            // Ensure frame capacity after every inserted block/table
            currentFrame = ensureFrameCapacity(doc, currentFrame, "Booklet_Master");
            story = currentFrame.parentStory;
        }

        // ── 9b. Deterministic Greek/Math Glyph Styling via changeGrep (STEP-7.1g) ──
        var GreekMathChar = styles.greekMathCharStyle || doc.characterStyles.itemByName("GreekMathChar");
        if (typeof NothingEnum === "undefined") {
            var NothingEnum = { NOTHING: null };
        }
        app.findGrepPreferences = NothingEnum.NOTHING;
        app.changeGrepPreferences = NothingEnum.NOTHING;
        app.findGrepPreferences.findWhat = GREEK_MATH_GREP_PATTERN;
        app.changeGrepPreferences.appliedCharacterStyle = GreekMathChar;
        var changedGrepResults = doc.changeGrep();
        var greekCharsStyled = (changedGrepResults && typeof changedGrepResults.length === "number") ? changedGrepResults.length : 0;
        app.findGrepPreferences = NothingEnum.NOTHING;
        app.changeGrepPreferences = NothingEnum.NOTHING;

        // Count expected Greek/Math characters from payload items
        var greekCharsExpected = 0;
        for (var itIdx = 0; itIdx < items.length; itIdx++) {
            var it = items[itIdx];
            if (it.raw_content && typeof it.raw_content === "string" && it.render_mode !== "image") {
                var m = it.raw_content.match(GREEK_MATH_JS_REGEX);
                if (m) greekCharsExpected += m.length;
            }
            if (it.headers && it.headers.length) {
                for (var hi = 0; hi < it.headers.length; hi++) {
                    var mh = String(it.headers[hi]).match(GREEK_MATH_JS_REGEX);
                    if (mh) greekCharsExpected += mh.length;
                }
            }
            if (it.rows && it.rows.length) {
                for (var ri = 0; ri < it.rows.length; ri++) {
                    var rw = it.rows[ri];
                    if (rw && rw.length) {
                        for (var ci = 0; ci < rw.length; ci++) {
                            var mr = String(rw[ci]).match(GREEK_MATH_JS_REGEX);
                            if (mr) greekCharsExpected += mr.length;
                        }
                    }
                }
            }
        }

        if (greekCharsExpected > 0 && greekCharsStyled === 0) {
            throw new Error(
                "GlyphCoverageError: expected " + greekCharsExpected +
                " Greek/math characters to be styled, but changeGrep styled 0."
            );
        }

        var assignedFontName = "";
        if (GreekMathChar && GreekMathChar.appliedFont) {
            if (typeof GreekMathChar.appliedFont.name !== "undefined") {
                assignedFontName = GreekMathChar.appliedFont.name;
            } else {
                assignedFontName = String(GreekMathChar.appliedFont);
            }
        }

        // ── 10. Dynamic Imposition from ACTUAL InDesign Page Count ──────
        var actualPageCount = doc.pages.count();
        var bindingMethod = payload.binding || "saddle_stitch";
        var multiple = getImpositionMultiple(bindingMethod);
        var imposedPageCount = Math.ceil(actualPageCount / multiple) * multiple;

        addBlankFillerPages(doc, imposedPageCount, "Booklet_Master");

        // ── 11. Save .INDD and Export PDF ────────────────────────────────
        var inddPath = payload.output_indd || (payload.output_dir + "/" + (payload.doc_id || "booklet") + ".indd");
        var pdfPath = payload.output_pdf || (payload.output_dir + "/" + (payload.doc_id || "booklet") + ".pdf");

        // Save INDD
        var inddFile = new File(inddPath);
        doc.save(inddFile);

        // Export PDF
        var pdfFile = new File(pdfPath);
        var pdfPreset = null;
        try {
            pdfPreset = app.pdfExportPresets.itemByName("[High Quality Print]");
            if (!pdfPreset.isValid) {
                pdfPreset = app.pdfExportPresets.item(0);
            }
        } catch (ePreset) {
            pdfPreset = null;
        }

        if (pdfPreset && pdfPreset.isValid) {
            doc.exportFile(ExportFormat.PDF_TYPE, pdfFile, false, pdfPreset);
        } else {
            doc.exportFile(ExportFormat.PDF_TYPE, pdfFile, false);
        }

        // ── 12. Collect Page Layout Diagnostics (STEP-7.1e) ────────────
        var pageDiagnostics = [];
        for (var p = 0; p < doc.pages.count(); p++) {
            var pg = doc.pages.item(p);
            var pSideStr = "";
            try {
                pSideStr = pg.side.toString();
            } catch (eSide) {
                pSideStr = String(pg.side);
            }

            var bBounds = null;
            var pageTableWidths = [];

            for (var tfIdx = 0; tfIdx < pg.textFrames.count(); tfIdx++) {
                var tf = pg.textFrames.item(tfIdx);
                if (tf.label === "Booklet_Body_Story") {
                    bBounds = tf.geometricBounds;
                }
                if (tf.tables && tf.tables.count() > 0) {
                    for (var tbIdx = 0; tbIdx < tf.tables.count(); tbIdx++) {
                        pageTableWidths.push(tf.tables.item(tbIdx).width);
                    }
                }
            }

            // Fallback for filler pages without body frame override
            if (!bBounds && pg.appliedMaster && pg.appliedMaster.isValid) {
                try {
                    var mbf = findMasterFrameByLabelOnSide(pg.appliedMaster, "Booklet_Body_Story", pg.side);
                    if (mbf && mbf.isValid) {
                        bBounds = mbf.geometricBounds;
                    }
                } catch (eMbf) {
                    bBounds = null;
                }
            }

            var pageNum = p + 1;
            if (pg.name) {
                var parsed = parseInt(pg.name, 10);
                if (!isNaN(parsed)) pageNum = parsed;
            }

            pageDiagnostics.push({
                page_number: pageNum,
                side: pSideStr,
                geometric_bounds: bBounds,
                geometricBounds: bBounds,
                table_width: (pageTableWidths.length > 0) ? pageTableWidths[0] : null,
                table_widths: pageTableWidths
            });
        }

        // ── 13. Write Successful Result JSON ────────────────────────────
        var resultData = {
            status: "ok",
            page_count: doc.pages.count(),
            actual_page_count: actualPageCount,
            imposed_page_count: imposedPageCount,
            composer_ok: true,
            greek_chars_styled: greekCharsStyled,
            greek_chars_expected: greekCharsExpected,
            greek_char_font: assignedFontName,
            output_indd: inddPath,
            output_pdf: pdfPath,
            pages: pageDiagnostics,
            warnings: warnings
        };

        if (resultPath) {
            writeResultToFile(resultPath, resultData);
        }

        return resultData;

    } catch (err) {
        var errorResult = {
            status: "error",
            error: err.message || String(err),
            page_count: (doc && doc.isValid) ? doc.pages.count() : 0,
            actual_page_count: (doc && doc.isValid) ? doc.pages.count() : 0,
            imposed_page_count: 0,
            composer_ok: false,
            warnings: warnings
        };
        if (resultPath) {
            try {
                writeResultToFile(resultPath, errorResult);
            } catch (eWrite) {
                warnings.push("Failed to write error result: " + eWrite.message);
            }
        }
        throw err;
    } finally {
        // Close document to avoid accumulating open documents across batch jobs
        if (doc !== null && doc.isValid) {
            try {
                doc.close(SaveOptions.NO);
            } catch (eClose) {
                warnings.push("Failed to close document: " + eClose.message);
            }
        }
    }
}

// Execute main entry point
generateBookletMain();
