<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-1.2] Word (DOCX) Extraction Engine — Math & Chemistry Formulas

#### 1. Tooling & Rationale

- **`python-docx` + `lxml` (direct OpenXML tree parsing):** Standard `python-docx` reads paragraph text (`w:p`) but drops or ignores math blocks (`m:oMath`) entirely. `lxml` allows inspecting raw XML elements to extract text and equations in their exact linear document order.
- **OMML → MathML/LaTeX via XSLT (`OMML2MML.XSL`):** Word stores equations in OMML (Office Math Markup Language). Microsoft's official `OMML2MML.XSL` stylesheet converts OMML to standard MathML without loss of symbols, fractions, or integrals — from which LaTeX conversion or direct MathML injection into InDesign is straightforward. **Precondition:** the exact redistribution terms of this specific stylesheet file must be confirmed before bundling it inside a distributed `.exe` (as opposed to requiring it be sourced from a local Office installation at runtime) — this is a one-time licensing verification task, not an architectural blocker.
- **Positional Chemical Formula Parser:** Word chemistry content uses `w:vertAlign val="subscript"/"superscript"` runs and reaction arrows (`->`, `\rightleftharpoons`). These patterns are detected within `w:r` runs and assembled into formatted `chemistry_formula` blocks preserving valence numbers and charges.

#### 2. Extraction Strategy

**In-line Sequential Traversal:** Walk `w:p` paragraph elements and their children:
- Text with subscript/superscript chemistry markers → classified as `chemistry_formula`.
- `m:oMath` elements → passed through the XSLT engine, converted to MathML/LaTeX, classified as `math_formula`.
- `w:tbl` tables → extracted with full structure (cells, rows, merged text) and stored as `TableData`.
- `w:object` / `v:shape` elements (legacy MathType / Microsoft Equation 3.0 OLE objects) → **detected explicitly and classified as `legacy_equation_placeholder`**, never silently skipped. Older Iraqi curriculum booklets frequently predate the modern `m:oMath` format and embed equations as OLE objects or embedded images instead — a purely `m:oMath`-based extractor would drop these equations from the output with no error and no trace, which is a Zero-Mutation violation by omission (a form of content loss the hash-integrity system cannot catch, since a *missing* block produces no hash mismatch). The placeholder block records the OLE object's position and, where extractable, a rendered preview image reference, so the block appears in the document flow as a visible gap the designer must manually resolve, rather than vanishing.

**Known limitation, documented rather than solved in this phase:** automatic conversion of legacy OLE/MathType equations to MathML is out of scope for v1 — the goal here is detection and visibility, not conversion. A `legacy_equation_placeholder` always requires manual designer intervention in InDesign.

#### 3. Declarative Definitions

```python
# app/core/docx_extractor.py
import os
import lxml.etree as ET
from app.core.exceptions import MathExtractionError

class DocxMathExtractor:
    """
    Extracts OMML math elements from Word XML and converts them to
    standard MathML. Applies strict Fail-Fast on conversion failure
    or missing XSLT stylesheet.
    """
    def __init__(self, xslt_path: str = "assets/xslt/OMML2MML.XSL"):
        if not os.path.exists(xslt_path):
            raise MathExtractionError(f"Math conversion stylesheet missing: {xslt_path}")

        try:
            self.xslt_tree = ET.parse(xslt_path)
            self.transform = ET.XSLT(self.xslt_tree)
        except Exception as exc:
            raise MathExtractionError(f"Failed to initialize XSLT math engine: {exc}") from exc

    def omml_to_mathml(self, omml_element: ET._Element) -> str:
        """Converts an m:oMath block into a syntactically valid MathML XML string."""
        try:
            dom = ET.fromstring(ET.tostring(omml_element))
            result_tree = self.transform(dom)
            mathml_str = ET.tostring(result_tree, encoding="utf-8").decode("utf-8").strip()

            # Validate resulting XML is well-formed
            ET.fromstring(mathml_str.encode("utf-8"))
            return mathml_str
        except Exception as exc:
            raise MathExtractionError(f"Failed to convert OMML element to MathML: {exc}") from exc
```

#### 4. Acceptance Criteria

- **[AC-1.2.1]** Every `m:oMath` / `m:oMathPara` element detected in a Word file produces syntactically valid MathML (verified via `ET.fromstring()`), with the total count of extracted math blocks matching the count of source `m:oMath` elements. (Note: exact structural/visual fidelity for highly nested equations is not guaranteed byte-for-byte — validity and count-matching are the enforceable bar.)
- **[AC-1.2.2]** Chemical formula extraction (e.g. H₂SO₄ + 2NaOH → Na₂SO₄ + 2H₂O) preserves subscript numbers and arrows intact, correctly classified as `chemistry_formula`.
- **[AC-1.2.3]** Paragraph, table, and heading order is preserved exactly as in the source Word document — no interleaving or dropping of nested paragraphs.
- **[AC-1.2.4]** Any XSLT initialization failure or missing stylesheet raises `MathExtractionError` immediately (Fail-Fast, same principle as `TextIntegrityError`) — no silent skip of unconvertible equations.
- **[AC-1.2.5]** A `.docx` file containing a legacy OLE equation object (`w:object`/`v:shape`) produces a `legacy_equation_placeholder` block at the correct document position — the total extracted block count matches source element count (`m:oMath` + `w:object`/`v:shape` equations combined), with zero silently-dropped equations of either kind.

---
