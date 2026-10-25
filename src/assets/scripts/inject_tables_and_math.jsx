/*
    inject_tables_and_math.jsx — Table and Math Injection Subroutines
    STEP-2.2

    Provides two core injection functions:
    1. injectMathFormulaBlock() — Inserts math/formula text with strict
       LTR paragraph direction ("Defense in Depth" — direction is enforced
       both in the template's mathFormulaStyle AND explicitly at insertion
       time, so a stale template cannot silently corrupt math rendering).
    2. injectRTLTable() — Inserts RTL-direction tables with proportional
       column widths, header row repetition, alternating row fills, and
       professional cell padding.

    These functions are subroutines called by the main content injection
    loop (STEP-2.3), not standalone entry points. They expect an active
    document with resolved color swatches and paragraph styles.

    Dependencies:
        - lib/json2.jsx (JSON polyfill for ExtendScript)
        - payload_loader.jsx (file-based IPC, if invoked via payload)
        - build_master_spread.jsx (provides the body story frame)

    Key design decisions:
    - Table cell references are stable once the table structure is
      created — populating one cell does not shift character offsets
      of other cells. This is a deliberate, scoped exception to
      STEP-1.1's atomic insert-style-release rule.
    - Tables are added at story.insertionPoints[-1] and followed by
      a paragraph break (\r), keeping the table atomic relative to
      surrounding story flow.
    - Fail-Fast on unsupported RTL tables: if the InDesign build does
      not support TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION,
      RTLTableUnsupportedError is raised immediately.

    STEP-1.1 reference: Zero-Mutation, COM threading, modal suppression
*/


// ─── Math Formula Injection ───────────────────────────────────────────

/**
 * Inject a math/formula text block into the story with strict LTR direction.
 *
 * Appends the math text at the current end of the story, applies the
 * mathFormulaStyle paragraph style, then EXPLICITLY sets paragraph
 * direction to LTR as a secondary safeguard (Defense in Depth).
 *
 * The template-level mathFormulaStyle MUST already have its paragraph
 * direction set to LTR as the primary defense mechanism. This code-level
 * assignment is an independent safeguard against template edits that
 * might remove the direction setting — without this, silent BiDi
 * corruption would go undetected until print.
 *
 * @param {Story} story - The InDesign story object to inject into.
 * @param {string} mathText - The math/formula text content to inject.
 *     Examples: "(1 + x)^n = 1 + nx + ...", "2!", "x^2 + y^2 = r^2"
 * @param {Object} styles - Style references object. Must contain:
 *     mathFormulaStyle: ParagraphStyle — the LTR math paragraph style
 *         from the template (mandatory precondition: its paragraph
 *         direction must be LTR at the template level).
 */
function injectMathFormulaBlock(story, mathText, styles) {
    // Record the insertion point index BEFORE inserting content
    var start = story.insertionPoints[-1].index;

    // Insert math text followed by a paragraph break
    story.insertionPoints[-1].contents = mathText + "\r";

    // Record the insertion point index AFTER inserting content
    var end = story.insertionPoints[-1].index;

    // Apply mathFormulaStyle to the inserted text range.
    // Math.max guards against zero-length edge case (empty mathText
    // with only the \r — still needs styling for consistency).
    var targetRange = story.characters.itemByRange(
        start, Math.max(start, end - 1)
    );
    targetRange.applyParagraphStyle(styles.mathFormulaStyle, false);

    // Defense in Depth: Explicit LTR enforcement at the code level.
    // Do NOT rely solely on mathFormulaStyle's template definition —
    // the template setting is a mandatory precondition on assets/*.idml,
    // this is the code-level safeguard against template drift.
    targetRange.paragraphs.everyItem().paragraphDirection =
        ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
}


// ─── RTL Table Injection ──────────────────────────────────────────────

/**
 * Inject an RTL (right-to-left) table into the story with proportional
 * column widths, header row repetition, alternating row fills, and
 * professional cell padding.
 *
 * The table is inserted atomically at story.insertionPoints[-1],
 * fully formatted, then followed by a paragraph break (\r) before
 * any subsequent block is inserted.
 *
 * Cell references are stable once the table structure is created
 * (populating one cell does not shift character offsets of other cells),
 * so the "build structure, then populate in loops" pattern is safe.
 * This is a deliberate, scoped exception to STEP-1.1's atomic
 * insert-style-release rule — see STEP-2.2 §2 scope clarification.
 *
 * Column widths are calculated from maximum character count per column
 * as a proportional proxy. Known limitation: character count is a rough
 * proxy for Arabic visual width (glyph shaping/ligatures differ from
 * Latin at equal character count), acceptable for v1.
 *
 * @param {Story} story - The InDesign story object to inject into.
 * @param {Object} tableData - Table data object. Must contain:
 *     headers: {Array<string>} Column header strings (may be empty).
 *     rows: {Array<Array<string>>} 2D array of cell content strings.
 * @param {number} frameWidthPt - Total width of the text frame in
 *     points, used for proportional column width distribution.
 * @param {Object} colors - Color palette object. Must contain:
 *     primaryColor: Swatch for header cell fill and top/bottom borders.
 *     tableBorderColor: Swatch for side borders and row separators.
 *     tableAltColor: Swatch for alternating (odd-indexed) row fills.
 *     paperColor: Swatch for even-indexed row fills ("Paper" or white).
 * @param {Object} styles - Style references object. Must contain:
 *     tableHeaderStyle: ParagraphStyle for header cell text.
 *     tableCellStyle: ParagraphStyle for body cell text.
 * @throws {Error} RTLTableUnsupportedError if this InDesign build does
 *     not support RIGHT_TO_LEFT_DIRECTION tables.
 */
function injectRTLTable(story, tableData, frameWidthPt, colors, styles) {
    var headers = tableData.headers || [];
    var rows = tableData.rows || [];

    // Guard: nothing to inject
    if (headers.length === 0 && rows.length === 0) return;

    var colCount = headers.length > 0
        ? headers.length
        : (rows[0] ? rows[0].length : 1);

    // ── 1. Insert Table Instance ──────────────────────────────────
    // Atomic relative to surrounding story flow — inserted at the
    // current end of the story.
    var indTable = story.insertionPoints[-1].tables.add({
        headerRowCount: headers.length > 0 ? 1 : 0,
        bodyRowCount: rows.length,
        columnCount: colCount
    });

    // ── 2. Set Right-to-Left Table Direction ──────────────────────
    // Fail-Fast: A reversed-direction Arabic table reaching print
    // is a critical layout defect, not a degradable condition.
    // If the InDesign build does not support RTL tables (non-Middle-
    // East localized build), raise immediately rather than silently
    // rendering left-to-right.
    if (typeof TableDirectionOptions === "undefined" ||
        typeof TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION === "undefined") {
        throw new Error(
            "RTLTableUnsupportedError: This InDesign installation " +
            "does not support RIGHT_TO_LEFT_DIRECTION tables. " +
            "Verify Middle-East / World-Ready localized build."
        );
    }
    indTable.tableDirection =
        TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION;

    // ── 3. Proportional Column Width Calculation ──────────────────
    // Uses maximum character count per column as a proportional
    // weight. Wider text columns receive proportionally larger widths
    // than single-word status columns.
    //
    // Known limitation: character count is a rough proxy for Arabic
    // visual width (glyph shaping/ligatures differ from Latin at
    // equal character count) — acceptable for v1.
    var colCharWeights = [];
    for (var c = 0; c < colCount; c++) {
        var maxLen = headers[c] ? headers[c].length : 2;
        for (var r = 0; r < rows.length; r++) {
            if (rows[r][c] && rows[r][c].length > maxLen) {
                maxLen = rows[r][c].length;
            }
        }
        // Minimum weight of 4 prevents zero-width columns for
        // very short content (e.g. single-digit numbers).
        colCharWeights.push(Math.max(maxLen, 4));
    }

    var totalWeight = 0;
    for (var w = 0; w < colCharWeights.length; w++) {
        totalWeight += colCharWeights[w];
    }

    var sumAssigned = 0;
    for (var colIdx = 0; colIdx < colCount; colIdx++) {
        var assignedWidth =
            (colCharWeights[colIdx] / totalWeight) * frameWidthPt;
        indTable.columns.item(colIdx).width = assignedWidth;
        sumAssigned += assignedWidth;
    }
    // Anchor edge-to-edge: adjust last column for any sub-point rounding remainder
    if (colCount > 0 && Math.abs(frameWidthPt - sumAssigned) > 0.001) {
        indTable.columns.item(colCount - 1).width += (frameWidthPt - sumAssigned);
    }

    // Anchor the table so it spans the frame edge to edge:
    // Clear left/right/firstLine indent on the containing paragraph so the table
    // is not inset by paragraph styles.
    var parentPara = indTable.storyOffset.paragraphs.item(0);
    if (parentPara && parentPara.isValid) {
        parentPara.leftIndent = 0;
        parentPara.rightIndent = 0;
        parentPara.firstLineIndent = 0;
    }

    // ── 4. Stroke & Border Configuration ─────────────────────────
    // Top/bottom borders use the primary color at 1pt weight.
    // Left/right borders use the table border color at 0.5pt weight.
    indTable.topBorderStrokeColor = colors.primaryColor;
    indTable.topBorderStrokeWeight = 1.0;
    indTable.bottomBorderStrokeColor = colors.primaryColor;
    indTable.bottomBorderStrokeWeight = 1.0;
    indTable.leftBorderStrokeColor = colors.tableBorderColor;
    indTable.leftBorderStrokeWeight = 0.5;
    indTable.rightBorderStrokeColor = colors.tableBorderColor;
    indTable.rightBorderStrokeWeight = 0.5;

    // ── 5. Populate Header Row ───────────────────────────────────
    // Safe as a loop: cell references are stable once the table
    // structure is created — see STEP-2.2 §2 "Zero Batching" scope.
    if (headers.length > 0) {
        for (var hIdx = 0; hIdx < colCount; hIdx++) {
            var hCell = indTable.rows.item(0).cells.item(hIdx);
            var hText = headers[hIdx] || "";
            hCell.contents = hText;
            hCell.fillColor = colors.primaryColor;
            hCell.verticalJustification =
                VerticalJustification.CENTER_ALIGN;
            hCell.topInset = 4.0;
            hCell.bottomInset = 4.0;
            hCell.leftInset = 6.0;
            hCell.rightInset = 6.0;
            hCell.paragraphs.everyItem().applyParagraphStyle(
                styles.tableHeaderStyle, false
            );

            // BiDi IN TABLE CELLS (STEP-7.1e):
            // In injectRTLTable, for every cell whose text contains NO Arabic letter ([\u0600-\u06FF]),
            // set paragraphDirection = LEFT_TO_RIGHT and justification = CENTER_ALIGN.
            // Known limitation: Cells that mix Arabic and Latin/math keep RTL.
            // Do not insert any control characters.
            var hasArabicH = /[\u0600-\u06FF]/.test(hText);
            if (!hasArabicH) {
                var hParas = hCell.paragraphs.everyItem();
                hParas.paragraphDirection =
                    ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
                hParas.justification =
                    Justification.CENTER_ALIGN;
            }
        }
    }

    // ── 6. Populate Body Rows with Alternating Fills ─────────────
    // Safe as a loop: same cell-reference stability rationale as §5.
    // Alternating fills improve readability for dense data tables.
    var rOffset = headers.length > 0 ? 1 : 0;
    for (var rowIdx = 0; rowIdx < rows.length; rowIdx++) {
        var rowData = rows[rowIdx];
        var targetRow = indTable.rows.item(rowIdx + rOffset);
        var isAlt = (rowIdx % 2 === 1);

        for (var cellIdx = 0; cellIdx < colCount; cellIdx++) {
            var bCell = targetRow.cells.item(cellIdx);
            var cText = (rowData && rowData[cellIdx]) ? rowData[cellIdx] : "";
            bCell.contents = cText;
            bCell.fillColor =
                isAlt ? colors.tableAltColor : colors.paperColor;
            bCell.verticalJustification =
                VerticalJustification.CENTER_ALIGN;
            bCell.topInset = 3.5;
            bCell.bottomInset = 3.5;
            bCell.leftInset = 5.0;
            bCell.rightInset = 5.0;
            bCell.bottomEdgeStrokeColor = colors.tableBorderColor;
            bCell.bottomEdgeStrokeWeight = 0.5;
            bCell.paragraphs.everyItem().applyParagraphStyle(
                styles.tableCellStyle, false
            );

            // BiDi IN TABLE CELLS (STEP-7.1e):
            // In injectRTLTable, for every cell whose text contains NO Arabic letter ([\u0600-\u06FF]),
            // set paragraphDirection = LEFT_TO_RIGHT and justification = CENTER_ALIGN.
            // Known limitation: Cells that mix Arabic and Latin/math keep RTL.
            // Do not insert any control characters.
            var hasArabicB = /[\u0600-\u06FF]/.test(cText);
            if (!hasArabicB) {
                var bParas = bCell.paragraphs.everyItem();
                bParas.paragraphDirection =
                    ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
                bParas.justification =
                    Justification.CENTER_ALIGN;
            }
        }
    }

    // ── 7. Mandatory Newline After Table ──────────────────────────
    // Keeps the table atomic relative to the surrounding story flow.
    // Any subsequent block inserted at insertionPoints[-1] will
    // appear AFTER this separator, not inside the table's last cell.
    story.insertionPoints[-1].contents = "\r";
}
