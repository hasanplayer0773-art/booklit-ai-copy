<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-2.2] Arabic RTL Tables & LTR Math Injection Engine

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Native RTL Tables | InDesign Table Engine (`TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION`) | Reverses native column ordering so Column 0 starts visually on the right-hand margin, matching Arabic reading flow without manual column swapping. |
| Cell Inset & Padding Matrix | Native cell geometric formatting (`topInset`/`bottomInset`/`leftInset`/`rightInset`) | Enforces professional typography padding and vertical centering (`VerticalJustification.CENTER_ALIGN`) instead of cramped default cell borders. |
| Math BiDi Isolation | Explicit LTR paragraph/character direction, enforced both in the template style and in code | Overrides the document's global RTL composer for math blocks, so equations, brackets, exponents, and factorials (`2!` vs `!2`) render strictly left-to-right without transposition. |
| Multi-Page Table Splitting | Native header row repetition (`headerRowCount = 1`) | Automatically duplicates table headers at the top of the next text frame when a table flows across a page boundary. |

#### 2. Technical Challenges & Architectural Solutions

- **BiDi Collision in Mixed Layouts:** Under InDesign's World-Ready Paragraph Composer in RTL mode, raw math expressions like `(1 + x)^n = 1 + nx + ... + n(n-1)x^2 / 2!` get structurally scrambled by Unicode bidirectional heuristics. The fix is an isolated LTR math style: paragraph and character direction forced to LTR, centered, bound to a dedicated math font (Cambria Math / Consolas).
- **Defense in Depth for LTR Enforcement:** Direction is set on the `mathFormulaStyle` Paragraph Style *inside* the `.idml` template as the primary mechanism — but the injection code also applies `paragraphDirection` explicitly at insertion time as a second, independent safeguard. This matters because if the template style is ever edited or replaced later without this setting, silent BiDi corruption in every subsequent math block would otherwise go undetected until print. The template-level setting is therefore documented as a mandatory **precondition** on `assets/*.idml`, not just a convenience default.
- **Proportional Column Width Distribution:** Dividing columns equally makes short columns (e.g. "نعم") excessively wide while descriptive columns (e.g. "اسم الشخصية") overflow. Column widths are calculated from the maximum character length per column before converting to points. **Known limitation:** character count is a rough proxy for Arabic visual width (glyph shaping/ligatures differ from Latin at equal character count), so Arabic-heavy columns may render slightly wider than strictly necessary — acceptable for v1, flagged for a possible future glyph-width-aware pass.
- **Scope of the "Zero Batching" Rule (STEP-1.1 clarification):** STEP-1.1's atomic insert-style-release rule exists specifically because `insertionPoints`/character-range offsets are invalidated by every subsequent insertion into the same story. Table cells (`table.rows.item(x).cells.item(y)`) do **not** share this failure mode — cell references remain stable once the table structure is created, since populating one cell's contents does not shift the character offsets of other cells. Table population may therefore safely use a "build structure, then populate in loops" pattern; this is a deliberate, scoped exception to STEP-1.1's rule, not a violation of it.
- **Fail-Fast on Unsupported RTL Tables:** If the running InDesign installation does not support `TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION` (e.g. a non-Middle-East localized build), the table must **not** silently render left-to-right — an Arabic booklet with reversed tables reaching print is exactly the class of error this project's Fail-Fast policy exists to prevent. This raises `RTLTableUnsupportedError` immediately rather than degrading silently.
- **Atomic Table Insertion:** Tables are added at `story.insertionPoints[-1]`, fully formatted, then followed by a paragraph break (`\r`) before any subsequent block is inserted — keeping the table itself atomic relative to the surrounding story flow, even though internal cell population uses the loop pattern described above.

#### 3. Declarative Definitions

```javascript
// assets/scripts/inject_tables_and_math.jsx
/**
 * Table and Math Injection Subroutines
 */

function injectMathFormulaBlock(story, mathText, styles) {
    var start = story.insertionPoints[-1].index;
    story.insertionPoints[-1].contents = mathText + "\r";
    var end = story.insertionPoints[-1].index;

    var targetRange = story.characters.itemByRange(start, Math.max(start, end - 1));
    targetRange.applyParagraphStyle(styles.mathFormulaStyle, false);

    // Explicit LTR enforcement (defense in depth — do not rely solely on
    // mathFormulaStyle's template definition; the template setting is a
    // mandatory precondition, this is the code-level safeguard).
    targetRange.paragraphs.everyItem().paragraphDirection = ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
}

function injectRTLTable(story, tableData, frameWidthPt, colors, styles) {
    var headers = tableData.headers || [];
    var rows = tableData.rows || [];
    if (headers.length === 0 && rows.length === 0) return;

    var colCount = headers.length > 0 ? headers.length : (rows[0] ? rows[0].length : 1);

    // 1. Insert Table Instance (atomic relative to surrounding story flow)
    var indTable = story.insertionPoints[-1].tables.add({
        headerRowCount: headers.length > 0 ? 1 : 0,
        bodyRowCount: rows.length,
        columnCount: colCount
    });

    // 2. Set Right-to-Left Table Direction — Fail-Fast, not silent fallback.
    //    A reversed-direction Arabic table reaching print is a critical
    //    layout defect, not a degradable condition.
    if (typeof TableDirectionOptions === "undefined" ||
        typeof TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION === "undefined") {
        throw new Error("RTLTableUnsupportedError: This InDesign installation does not support RIGHT_TO_LEFT_DIRECTION tables. Verify Middle-East / World-Ready localized build.");
    }
    indTable.tableDirection = TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION;

    // 3. Proportional Column Width Calculation (character-count proxy —
    //    see "Known limitation" note above for Arabic glyph-width caveat)
    var colCharWeights = [];
    for (var c = 0; c < colCount; c++) {
        var maxLen = headers[c] ? headers[c].length : 2;
        for (var r = 0; r < rows.length; r++) {
            if (rows[r][c] && rows[r][c].length > maxLen) {
                maxLen = rows[r][c].length;
            }
        }
        colCharWeights.push(Math.max(maxLen, 4));
    }

    var totalWeight = 0;
    for (var w = 0; w < colCharWeights.length; w++) {
        totalWeight += colCharWeights[w];
    }

    for (var colIdx = 0; colIdx < colCount; colIdx++) {
        var assignedWidth = (colCharWeights[colIdx] / totalWeight) * frameWidthPt;
        indTable.columns.item(colIdx).width = assignedWidth;
    }

    // 4. Stroke & Border Configuration
    indTable.topBorderStrokeColor = colors.primaryColor;
    indTable.topBorderStrokeWeight = 1.0;
    indTable.bottomBorderStrokeColor = colors.primaryColor;
    indTable.bottomBorderStrokeWeight = 1.0;
    indTable.leftBorderStrokeColor = colors.tableBorderColor;
    indTable.leftBorderStrokeWeight = 0.5;
    indTable.rightBorderStrokeColor = colors.tableBorderColor;
    indTable.rightBorderStrokeWeight = 0.5;

    // 5. Populate Header Row (safe as a loop — see Zero Batching scope note)
    if (headers.length > 0) {
        for (var hIdx = 0; hIdx < colCount; hIdx++) {
            var hCell = indTable.rows.item(0).cells.item(hIdx);
            hCell.contents = headers[hIdx] || "";
            hCell.fillColor = colors.primaryColor;
            hCell.verticalJustification = VerticalJustification.CENTER_ALIGN;
            hCell.topInset = 4.0;
            hCell.bottomInset = 4.0;
            hCell.leftInset = 6.0;
            hCell.rightInset = 6.0;
            hCell.paragraphs.everyItem().applyParagraphStyle(styles.tableHeaderStyle, false);
        }
    }

    // 6. Populate Body Rows with Alternating Fills (safe as a loop)
    var rOffset = headers.length > 0 ? 1 : 0;
    for (var rowIdx = 0; rowIdx < rows.length; rowIdx++) {
        var rowData = rows[rowIdx];
        var targetRow = indTable.rows.item(rowIdx + rOffset);
        var isAlt = (rowIdx % 2 === 1);

        for (var cellIdx = 0; cellIdx < colCount; cellIdx++) {
            var bCell = targetRow.cells.item(cellIdx);
            bCell.contents = (rowData && rowData[cellIdx]) ? rowData[cellIdx] : "";
            bCell.fillColor = isAlt ? colors.tableAltColor : colors.paperColor;
            bCell.verticalJustification = VerticalJustification.CENTER_ALIGN;
            bCell.topInset = 3.5;
            bCell.bottomInset = 3.5;
            bCell.leftInset = 5.0;
            bCell.rightInset = 5.0;
            bCell.bottomEdgeStrokeColor = colors.tableBorderColor;
            bCell.bottomEdgeStrokeWeight = 0.5;
            bCell.paragraphs.everyItem().applyParagraphStyle(styles.tableCellStyle, false);
        }
    }

    // Mandatory newline following table insertion
    story.insertionPoints[-1].contents = "\r";
}
```

```python
# app/automation/styles_schema.py
from pydantic import BaseModel, ConfigDict

class TypographyStylesConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Global / Body
    body_font: str = "Tajawal"
    body_size_pt: float = 11.5
    body_leading_pt: float = 17.0

    # Math & Formulas (Strict LTR — enforced both in template style and
    # explicitly in code; see "Defense in Depth" note above)
    math_font: str = "Cambria Math"
    math_size_pt: float = 12.0
    math_leading_pt: float = 18.0
    math_direction: str = "left_to_right"

    # Tables
    table_header_size_pt: float = 10.5
    table_cell_size_pt: float = 10.0
    table_direction: str = "right_to_left"
```

#### 4. Preconditions

- **Mandatory Template Precondition:** `styles.mathFormulaStyle` in the base `.idml` must have its paragraph direction set to LTR at the template level, as the primary defense; the code-level `paragraphDirection` assignment is a secondary safeguard, not a substitute.
- **Localization Precondition:** The InDesign installation running the automation must support `TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION` (Middle-East / World-Ready composer build). This should be verified once at application startup (not per-table) and surfaced to the user clearly if unsupported, rather than discovered mid-generation.

#### 5. Acceptance Criteria

- **[AC-2.2.1]** Tables injected into InDesign have `tableDirection` set to `RIGHT_TO_LEFT_DIRECTION`, with Column 0 positioned on the right-hand margin. If unsupported by the InDesign build, `RTLTableUnsupportedError` is raised immediately — no silent LTR fallback.
- **[AC-2.2.2]** When a table extends beyond the bottom margin and flows into the next threaded text frame/page, the header row repeats automatically at the top of the next frame (`headerRowCount = 1`).
- **[AC-2.2.3]** Injected `math_formula` blocks render with strictly left-to-right character and paragraph direction, preserving original operator order (`(1+x)^n`, `2!`, exponents) without bidirectional inversion — verified independently of template state, since direction is enforced explicitly at insertion time.
- **[AC-2.2.4]** Table column widths dynamically scale to fit the total text frame width (`frameWidthPt`), with wider text columns receiving proportionally larger widths than single-word status columns.

---
