<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-2.3] Typography & Ministerial Question Callout Matrix

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Style Dispatch Table | `BlockType → ParagraphStyle` static mapping | Deterministic, single source of truth linking each `ContentBlock.block_type` to its InDesign paragraph style — no conditional branching scattered across injection code. |
| Question Callout Frame | Side-border colored rule via `paragraphBorders` / anchored rule above/below | Ministerial questions (`ministry_question`) get a distinct left-side colored bar and light background tint, matching the reference booklet's visual hierarchy. |
| Font Fallback Chain | Explicit per-language font binding (Arabic / Latin / Math) | Prevents InDesign silently substituting a default font (e.g. missing glyph boxes) when a block's script doesn't match the primary body font. |

#### 2. Technical Notes

- **Style Dispatch is data, not logic:** `BLOCK_STYLE_MAP` is a plain dict keyed by `BlockType`, resolved once per block at injection time — adding a new `block_type` later means adding one map entry, not new branching code.
- **Ministerial question callout is layered, not embedded:** the colored side-bar is applied via the paragraph border-and-shading feature (`borderRightWeight`/`borderRightColor`) plus a matching `rightIndent`, not a literal drawn rectangle — this keeps the callout reflow-safe (it moves with the text automatically) rather than needing manual repositioning like the STEP-2.1 stale-header problem. **Note on RTL indent direction:** in Arabic (RTL) paragraphs, the leading/indented edge is the *right* edge, not the left — `leftIndent` would push text away from the line's trailing (left) edge while leaving the reading-start (right) edge flush against the margin, which is backwards for a leading side-bar. `rightIndent` combined with `borderRightWeight`/`borderRightColor` is the correct pairing.
- **Fail-Fast on unmapped block types:** if `compute_target_style()` receives a `block_type` with no entry in `BLOCK_STYLE_MAP`, it raises `StyleMappingError` rather than silently falling back to `body_style` — an unmapped block silently defaulting to body text is a layout defect that should surface immediately, consistent with this project's Fail-Fast policy (STEP-1.1, STEP-2.2).
- **Font fallback chain lives in the style definitions themselves** (Arabic body → Tajawal, English/Latin → body_font_latin, math → Cambria Math per STEP-2.2) — enforced at template level as a precondition, same pattern as STEP-2.2's math direction.

#### 3. Declarative Definitions

```python
# app/automation/style_dispatch.py
from app.core.models import BlockType
from app.core.exceptions import BookletError

class StyleMappingError(BookletError):
    """Raised when a block_type has no registered paragraph style mapping."""

BLOCK_STYLE_MAP: dict[BlockType, str] = {
    "chapter_header": "ChapterHeaderStyle",
    "arabic_text": "BodyArabicStyle",
    "english_text": "BodyLatinStyle",
    "ministry_question": "MinistryQuestionStyle",
    "math_formula": "MathFormulaStyle",
    "chemistry_formula": "ChemistryFormulaStyle",
    "image_prompt": "ImagePlaceholderStyle",
    "list_item": "ListItemStyle",
    # CROSS-STEP AMENDMENT: STEP-1.2 emits this type; without an entry it would hit StyleMappingError.
    "legacy_equation_placeholder": "LegacyEquationPlaceholderStyle",
}

def resolve_style_name(block_type: BlockType) -> str:
    if block_type not in BLOCK_STYLE_MAP:
        raise StyleMappingError(f"No paragraph style mapped for block_type: {block_type}")
    return BLOCK_STYLE_MAP[block_type]
```

```javascript
// assets/scripts/apply_question_callout.jsx
/**
 * Applies the ministerial question side-bar callout to a paragraph style
 * range. Reflow-safe: uses paragraph border-and-shading + indent, not a
 * drawn shape. Uses rightIndent + borderRightWeight/Color — the correct
 * pairing for an RTL leading-edge side-bar (right edge is the reading-start
 * edge in Arabic paragraphs; leftIndent/a left-side rule would be reversed).
 */
function applyQuestionCallout(targetRange, colors, styles) {
    targetRange.paragraphs.everyItem().applyParagraphStyle(styles.ministryQuestionStyle, false);

    var pIter = targetRange.paragraphs.everyItem().getElements();
    for (var i = 0; i < pIter.length; i++) {
        var p = pIter[i];
        p.rightIndent = 14.0;
        p.paragraphBorderAndShadingSettings.borderRightWeight = 3.0;
        p.paragraphBorderAndShadingSettings.borderRightColor = colors.primaryColor;
    }
}

function resolveAndApplyStyle(story, start, end, blockType, styleMap, colors) {
    var styleName = styleMap[blockType];
    if (!styleName) {
        throw new Error("StyleMappingError: no InDesign style registered for block_type '" + blockType + "'");
    }
    var targetRange = story.characters.itemByRange(start, Math.max(start, end - 1));
    var pStyle = app.activeDocument.paragraphStyles.itemByName(styleName);
    if (!pStyle.isValid) {
        throw new Error("StyleMappingError: paragraph style '" + styleName + "' not found in template.");
    }
    targetRange.applyParagraphStyle(pStyle, false);

    if (blockType === "ministry_question") {
        applyQuestionCallout(targetRange, colors, {ministryQuestionStyle: pStyle});
    }
}
```

#### 4. Preconditions

- All nine `BLOCK_STYLE_MAP` style names must exist in the base `.idml` template's paragraph styles panel before generation — verified once at document-load time, not discovered mid-injection.

#### 5. Acceptance Criteria

- **[AC-2.3.1]** Every `ContentBlock` injected resolves to exactly one paragraph style via `resolve_style_name()`/`resolveAndApplyStyle()`; no block is left with a default/unstyled paragraph.
- **[AC-2.3.2]** An unmapped or missing `block_type` raises `StyleMappingError` immediately (Python) or a thrown `Error` (JSX) — never silently falls back to body style.
- **[AC-2.3.3]** `ministry_question` blocks receive the right-side callout treatment (`rightIndent` + `borderRightWeight`/`borderRightColor`) and reflow correctly when preceding content changes length (no manual repositioning required).
- **[AC-2.3.4]** A missing paragraph style in the template (name mismatch between `BLOCK_STYLE_MAP` and the `.idml` file) is detected at document-load precondition check, not mid-generation.
- **[AC-2.3.5]** On a rendered `ministry_question` paragraph, the colored border renders on the right edge of the text frame and the text is indented away from that same right edge — verified by comparing `rightIndent > 0` and a non-zero `borderRightWeight`, with `leftIndent` and `borderLeftWeight` remaining at their style defaults.
- **[AC-2.3.6]** A `legacy_equation_placeholder` block resolves to `LegacyEquationPlaceholderStyle` (a visibly distinct style, e.g. tinted/boxed, so the designer cannot miss the gap) and never raises `StyleMappingError` — the style exists in the template precondition check (AC-2.3.4).

---

## PHASE 2 STATUS: CLOSED (STEP-2.1 → STEP-2.3)

---

## PHASE 3: Ministerial Question Database & Retrieval Engine
