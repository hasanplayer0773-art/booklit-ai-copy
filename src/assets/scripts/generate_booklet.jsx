/*
    generate_booklet.jsx — Real Booklet Generation Script
    STEP-7.1

    Converts a BookletDocument JSON payload (blocks, tables, PageGeometry,
    styles config) into a real InDesign document and PDF.

    Workflow:
    1. #include json2.jsx via $.fileName-relative path
    2. Read payload from file
    3. Set UserInteractionLevels.NEVER_INTERACT (prevent modal deadlock)
    4. Create document from PageGeometry (facing pages, page size, bleeds)
    5. Programmatically create the 8 paragraph styles from BLOCK_STYLE_MAP
       (plus helpers) if missing (v1 does not require a pre-existing .idml)
    6. Set up Master Spread ("Booklet_Master") with header/footer ribbons
       and labeled body frames ("Booklet_Body_Story")
    7. Override first page body frame and thread story
    8. Inject blocks atomically with insert-style-release:
       - injectMathFormulaBlock (enforced LTR)
       - injectRTLTable (proportional columns, alternating rows, RTL direction)
       - resolveAndApplyStyle (maps block_type to style, applies ministry callout)
       - ensureFrameCapacity (MAX_PAGES_SAFETY = 500, side-matching body frame override)
    9. Call addBlankFillerPages to meet signature imposition requirement
    10. Save .indd and export high-quality PDF
    11. Write result JSON (page_count, warnings, status)
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
        newPage.appliedMaster = doc.masterSpreads.itemByName(masterName);

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
                    // Ignore unsupported properties on specific InDesign builds
                }
            }
        }
    }
    return style;
}


/**
 * Create or ensure all 8 BLOCK_STYLE_MAP styles (plus supporting styles) exist.
 *
 * Configures Arabic fonts (with fallbacks), RTL paragraph direction,
 * and strict LTR direction for math/chemistry/latin styles.
 */
function ensureAllStyles(doc, stylesCfg) {
    var bodyFont = stylesCfg.body_font || "Tajawal";
    var mathFont = stylesCfg.math_font || "Cambria Math";

    var rtlDirection = ParagraphDirectionOptions.RIGHT_TO_LEFT_DIRECTION;
    var ltrDirection = ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;

    // Helper to safely assign font
    function applyFontSafe(style, fontName, fallback) {
        try {
            style.appliedFont = app.fonts.itemByName(fontName);
        } catch (e1) {
            try {
                style.appliedFont = fontName;
            } catch (e2) {
                if (fallback) {
                    try {
                        style.appliedFont = fallback;
                    } catch (e3) {}
                }
            }
        }
    }

    // 1. ChapterHeaderStyle
    var chStyle = getOrCreateParagraphStyle(doc, "ChapterHeaderStyle", {
        pointSize: 16.0,
        leading: 22.0,
        paragraphDirection: rtlDirection,
        spaceBefore: 12.0,
        spaceAfter: 8.0
    });
    applyFontSafe(chStyle, bodyFont, "Arial");

    // 2. BodyArabicStyle
    var baStyle = getOrCreateParagraphStyle(doc, "BodyArabicStyle", {
        pointSize: stylesCfg.body_size_pt || 11.5,
        leading: stylesCfg.body_leading_pt || 17.0,
        paragraphDirection: rtlDirection,
        spaceAfter: 6.0
    });
    applyFontSafe(baStyle, bodyFont, "Traditional Arabic");

    // 3. BodyLatinStyle
    var blStyle = getOrCreateParagraphStyle(doc, "BodyLatinStyle", {
        pointSize: 11.0,
        leading: 16.0,
        paragraphDirection: ltrDirection,
        spaceAfter: 6.0
    });
    applyFontSafe(blStyle, "Arial", "Times New Roman");

    // 4. MinistryQuestionStyle
    var mqStyle = getOrCreateParagraphStyle(doc, "MinistryQuestionStyle", {
        pointSize: 12.0,
        leading: 18.0,
        paragraphDirection: rtlDirection,
        spaceBefore: 8.0,
        spaceAfter: 8.0
    });
    applyFontSafe(mqStyle, bodyFont, "Traditional Arabic");

    // 5. MathFormulaStyle (Forced LTR)
    var mfStyle = getOrCreateParagraphStyle(doc, "MathFormulaStyle", {
        pointSize: stylesCfg.math_size_pt || 12.0,
        leading: stylesCfg.math_leading_pt || 18.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 6.0,
        spaceAfter: 6.0
    });
    applyFontSafe(mfStyle, mathFont, "Times New Roman");

    // 6. ChemistryFormulaStyle (Forced LTR)
    var cfStyle = getOrCreateParagraphStyle(doc, "ChemistryFormulaStyle", {
        pointSize: 12.0,
        leading: 18.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 6.0,
        spaceAfter: 6.0
    });
    applyFontSafe(cfStyle, mathFont, "Arial");

    // 7. ImagePlaceholderStyle
    var ipStyle = getOrCreateParagraphStyle(doc, "ImagePlaceholderStyle", {
        pointSize: 10.0,
        leading: 14.0,
        paragraphDirection: rtlDirection,
        spaceBefore: 4.0,
        spaceAfter: 4.0
    });
    applyFontSafe(ipStyle, bodyFont, "Arial");

    // 8. ListItemStyle
    var liStyle = getOrCreateParagraphStyle(doc, "ListItemStyle", {
        pointSize: 11.5,
        leading: 17.0,
        paragraphDirection: rtlDirection,
        leftIndent: 12.0,
        spaceAfter: 4.0
    });
    applyFontSafe(liStyle, bodyFont, "Traditional Arabic");

    // 9. LegacyEquationPlaceholderStyle
    var legStyle = getOrCreateParagraphStyle(doc, "LegacyEquationPlaceholderStyle", {
        pointSize: 10.0,
        leading: 14.0,
        paragraphDirection: ltrDirection,
        spaceBefore: 4.0,
        spaceAfter: 4.0
    });
    applyFontSafe(legStyle, "Courier New", "Arial");

    // Supporting styles for headers, footers, tables
    var rhStyle = getOrCreateParagraphStyle(doc, "RunningHeaderStyle", {
        pointSize: 9.0,
        leading: 12.0,
        paragraphDirection: rtlDirection
    });
    applyFontSafe(rhStyle, bodyFont, "Arial");

    var rfStyle = getOrCreateParagraphStyle(doc, "RunningFooterStyle", {
        pointSize: 9.0,
        leading: 12.0,
        paragraphDirection: rtlDirection
    });
    applyFontSafe(rfStyle, bodyFont, "Arial");

    var thStyle = getOrCreateParagraphStyle(doc, "TableHeaderStyle", {
        pointSize: stylesCfg.table_header_size_pt || 10.5,
        leading: 14.0,
        paragraphDirection: rtlDirection
    });
    applyFontSafe(thStyle, bodyFont, "Arial");

    var tcStyle = getOrCreateParagraphStyle(doc, "TableCellStyle", {
        pointSize: stylesCfg.table_cell_size_pt || 10.0,
        leading: 13.0,
        paragraphDirection: rtlDirection
    });
    applyFontSafe(tcStyle, bodyFont, "Traditional Arabic");

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
        tableCellStyle: tcStyle
    };
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

        with (doc.documentPreferences) {
            pageHeight = geometry.height_pt + "pt";
            pageWidth = geometry.width_pt + "pt";
            facingPages = (geometry.facing_pages !== false);
            pagesPerDocument = 1;
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

        // ── 6. Create Paragraph Styles ──────────────────────────────────
        var styles = ensureAllStyles(doc, stylesCfg);

        // ── 7. Configure Master Spread ──────────────────────────────────
        setupMasterSpread(doc, geometry, colors, styles);

        // ── 8. Initialize First Page Body Frame ─────────────────────────
        var firstPage = doc.pages.item(0);
        firstPage.appliedMaster = doc.masterSpreads.itemByName("Booklet_Master");

        var masterBodyFrame = findMasterFrameByLabelOnSide(
            firstPage.appliedMaster, "Booklet_Body_Story", firstPage.side
        );
        var currentFrame = masterBodyFrame.override(firstPage);
        var story = currentFrame.parentStory;

        // Calculate available body frame width for tables
        var pBounds = firstPage.bounds;
        var pWidth = pBounds[3] - pBounds[1];
        var mInside = (geometry.margins.inside_spine_mm || 20.0) * 2.83465;
        var mOutside = (geometry.margins.outside_mm || 15.0) * 2.83465;
        var bodyWidthPt = pWidth - mInside - mOutside;
        if (bodyWidthPt <= 0) {
            bodyWidthPt = 400.0;
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
                // RTL Table Injection
                injectRTLTable(story, item, bodyWidthPt, colors, styles);
            } else if (item.block_type === "math_formula") {
                // Math Formula Injection (strict LTR)
                injectMathFormulaBlock(story, item.raw_content || "", styles);
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

        // ── 10. Filler Page Imposition Sync ─────────────────────────────
        var currentCount = doc.pages.count();
        var targetCount = payload.target_page_count;
        var bindingMethod = payload.binding || "saddle_stitch";
        var multiple = (bindingMethod === "saddle_stitch") ? 4 : ((bindingMethod === "perfect_bind" || bindingMethod === "wire") ? 2 : 1);

        if (!targetCount) {
            targetCount = Math.ceil(currentCount / multiple) * multiple;
        }

        try {
            addBlankFillerPages(doc, targetCount, "Booklet_Master");
        } catch (eFiller) {
            if (eFiller.message && eFiller.message.indexOf("ContentExceedsEstimateError") !== -1) {
                warnings.push(eFiller.message);
                var actualCount = doc.pages.count();
                var newTarget = Math.ceil(actualCount / multiple) * multiple;
                addBlankFillerPages(doc, newTarget, "Booklet_Master");
            } else {
                throw eFiller;
            }
        }

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

        // ── 12. Write Successful Result JSON ────────────────────────────
        var resultData = {
            status: "ok",
            page_count: doc.pages.count(),
            output_indd: inddPath,
            output_pdf: pdfPath,
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
            warnings: warnings
        };
        if (resultPath) {
            try {
                writeResultToFile(resultPath, errorResult);
            } catch (eWrite) {}
        }
        throw err;
    }
}

// Execute main entry point
generateBookletMain();
