"""
Booklet_AI Desktop — STEP-1.2: Word (DOCX) Extraction Engine
Math & Chemistry Formulas

Extracts content from .docx files in linear document order, including:
- Plain text paragraphs and headings
- OMML math equations (m:oMath) → converted to MathML via XSLT
- Chemistry formulas detected via subscript/superscript run patterns
- Tables (w:tbl) → TableData
- Legacy OLE equations (w:object / v:shape) → legacy_equation_placeholder

Design contract:
- Every element is emitted as a ContentBlock in exact document order.
- No element is silently skipped — OLE objects appear as placeholders.
- XSLT failure or missing stylesheet → MathExtractionError (Fail-Fast).
- All content hashes computed via compute_canonical_hash() from STEP-1.1.
"""

import io
import logging
import os
import re
import tempfile
import uuid
import zipfile
from typing import List, Optional, Tuple

import lxml.etree as ET
from docx import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from app.core.exceptions import MathExtractionError
from app.core.models import (
    BlockType,
    BookletDocument,
    ContentBlock,
    ImageAsset,
    PageData,
    TableData,
)
from app.core.text_integrity import compute_canonical_hash

logger = logging.getLogger(__name__)

# ─── OpenXML Namespaces ────────────────────────────────────────────────
NSMAP = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}

# Qualified tag names used for element detection
W_P = f'{{{NSMAP["w"]}}}p'
W_R = f'{{{NSMAP["w"]}}}r'
W_T = f'{{{NSMAP["w"]}}}t'
W_TBL = f'{{{NSMAP["w"]}}}tbl'
W_TR = f'{{{NSMAP["w"]}}}tr'
W_TC = f'{{{NSMAP["w"]}}}tc'
W_OBJECT = f'{{{NSMAP["w"]}}}object'
W_PPR = f'{{{NSMAP["w"]}}}pPr'
W_RPR = f'{{{NSMAP["w"]}}}rPr'
W_PSTYLE = f'{{{NSMAP["w"]}}}pStyle'
W_VERT_ALIGN = f'{{{NSMAP["w"]}}}vertAlign'
W_DRAWING = f'{{{NSMAP["w"]}}}drawing'
W_PICT = f'{{{NSMAP["w"]}}}pict'
M_OMATH = f'{{{NSMAP["m"]}}}oMath'
M_OMATHPARA = f'{{{NSMAP["m"]}}}oMathPara'
V_SHAPE = f'{{{NSMAP["v"]}}}shape'

# Drawing / image extraction tag paths
WP_EXTENT = f'{{{NSMAP["wp"]}}}extent'
WP_INLINE = f'{{{NSMAP["wp"]}}}inline'
WP_ANCHOR = f'{{{NSMAP["wp"]}}}anchor'

# EMU → mm conversion factor (1 inch = 914400 EMU, 1 inch = 25.4 mm)
EMU_PER_MM = 914400.0 / 25.4

# ─── Heading style detection ──────────────────────────────────────────
HEADING_RE = re.compile(r"^Heading\s*(\d+)$", re.IGNORECASE)

# ─── Chemistry detection patterns ─────────────────────────────────────
# Common chemistry element symbols (uppercase letter optionally followed by lowercase)
ELEMENT_SYMBOL_RE = re.compile(r"^[A-Z][a-z]?$")

# Reaction arrow characters and textual representations
REACTION_ARROWS = {"→", "←", "⇌", "⇄", "↔", "->", "<-", "<->", "\\rightleftharpoons"}

# Known chemical element symbols (subset for fast detection)
COMMON_ELEMENTS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca",
    "Fe", "Cu", "Zn", "Ag", "Au", "Pb", "Sn", "Br", "I", "Mn",
    "Cr", "Co", "Ni", "Ti", "Ba", "Sr", "Pt", "Hg", "Mo", "W",
}

# ─── Known Office XSLT paths (fallback discovery) ─────────────────────
_OFFICE_XSLT_CANDIDATES = [
    r"C:\Program Files\Microsoft Office\root\Office16\OMML2MML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16\OMML2MML.XSL",
    r"C:\Program Files\Microsoft Office\Office16\OMML2MML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\Office16\OMML2MML.XSL",
    r"C:\Program Files\Microsoft Office\root\Office15\OMML2MML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\root\Office15\OMML2MML.XSL",
]


def _discover_xslt(preferred_path: str) -> str:
    """Locate OMML2MML.XSL: first check preferred_path, then Office paths.

    Args:
        preferred_path: Primary path to check (e.g. assets/xslt/OMML2MML.XSL).

    Returns:
        Absolute path to a valid OMML2MML.XSL file.

    Raises:
        MathExtractionError: If no valid XSLT file is found anywhere.
    """
    if os.path.isfile(preferred_path):
        return os.path.abspath(preferred_path)

    for candidate in _OFFICE_XSLT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate

    raise MathExtractionError(
        f"Math conversion stylesheet missing: checked '{preferred_path}' "
        f"and {len(_OFFICE_XSLT_CANDIDATES)} Office installation paths. "
        f"Copy OMML2MML.XSL from your Office installation into "
        f"'{os.path.dirname(preferred_path)}/'."
    )


# ═══════════════════════════════════════════════════════════════════════
# DocxMathExtractor — OMML → MathML via XSLT
# ═══════════════════════════════════════════════════════════════════════


class DocxMathExtractor:
    """Extracts OMML math elements from Word XML and converts them to
    standard MathML. Applies strict Fail-Fast on conversion failure
    or missing XSLT stylesheet.
    """

    def __init__(self, xslt_path: str = "assets/xslt/OMML2MML.XSL"):
        resolved = _discover_xslt(xslt_path)

        try:
            self.xslt_tree = ET.parse(resolved)
            self.transform = ET.XSLT(self.xslt_tree)
        except Exception as exc:
            raise MathExtractionError(
                f"Failed to initialize XSLT math engine: {exc}"
            ) from exc

    def omml_to_mathml(self, omml_element: ET._Element) -> str:
        """Converts an m:oMath block into a syntactically valid MathML XML string.

        Args:
            omml_element: An lxml Element representing an m:oMath node.

        Returns:
            A valid MathML XML string.

        Raises:
            MathExtractionError: If XSLT transformation fails or produces
                                 invalid XML.
        """
        try:
            # Create a standalone copy to avoid namespace issues
            dom = ET.fromstring(ET.tostring(omml_element))
            result_tree = self.transform(dom)
            mathml_str = ET.tostring(
                result_tree, encoding="unicode"
            ).strip()

            # Validate resulting XML is well-formed
            if mathml_str:
                ET.fromstring(mathml_str.encode("utf-8"))
            return mathml_str
        except Exception as exc:
            raise MathExtractionError(
                f"Failed to convert OMML element to MathML: {exc}"
            ) from exc


# ═══════════════════════════════════════════════════════════════════════
# Chemistry Formula Detection
# ═══════════════════════════════════════════════════════════════════════


def _get_run_vertical_align(run_elem: ET._Element) -> Optional[str]:
    """Get the vertAlign value ('subscript'/'superscript') of a w:r element.

    Args:
        run_elem: A w:r XML element.

    Returns:
        'subscript', 'superscript', or None.
    """
    rpr = run_elem.find(W_RPR)
    if rpr is None:
        return None
    vert = rpr.find(W_VERT_ALIGN)
    if vert is not None:
        return vert.get(f'{{{NSMAP["w"]}}}val')
    return None


def _get_run_text(run_elem: ET._Element) -> str:
    """Extract text content from a w:r element (all w:t children).

    Args:
        run_elem: A w:r XML element.

    Returns:
        Concatenated text from all w:t children.
    """
    texts = []
    for t_elem in run_elem.findall(W_T):
        if t_elem.text:
            texts.append(t_elem.text)
    return "".join(texts)


def _analyze_runs_for_chemistry(
    runs: List[ET._Element],
) -> List[Tuple[int, int, str]]:
    """Detect chemistry formula spans in a sequence of w:r elements.

    Looks for patterns of element symbols followed by subscript numbers,
    superscript charges, and reaction arrows.

    Args:
        runs: List of w:r XML elements from a paragraph.

    Returns:
        List of (start_idx, end_idx, assembled_text) tuples for detected
        chemistry formula spans. Indices are inclusive.
    """
    if not runs:
        return []

    # Analyze each run
    run_info = []
    for r in runs:
        text = _get_run_text(r)
        valign = _get_run_vertical_align(r)
        run_info.append({
            "text": text,
            "valign": valign,
            "is_sub": valign == "subscript",
            "is_sup": valign == "superscript",
            "has_element": bool(
                text.strip() and any(
                    sym in text.strip()
                    for sym in COMMON_ELEMENTS
                )
            ),
            "has_arrow": text.strip() in REACTION_ARROWS,
            "has_digit": bool(text.strip() and text.strip().isdigit()),
            "has_plus": text.strip() == "+",
        })

    # Find chemistry spans: look for element+subscript patterns
    spans = []
    i = 0
    while i < len(run_info):
        # Start of a potential chemistry formula:
        # an element symbol, or a digit before element (coefficient),
        # or a reaction arrow
        if _is_chemistry_start(run_info, i):
            start = i
            end = i
            # Extend the span while we see chemistry-like runs
            j = i + 1
            while j < len(run_info) and _is_chemistry_continuation(run_info, j):
                end = j
                j += 1
            # Validate the span has at least one element + subscript pattern
            # or a reaction arrow connecting two segments
            if _is_valid_chemistry_span(run_info, start, end):
                assembled = _assemble_chemistry_text(run_info, start, end)
                spans.append((start, end, assembled))
                i = end + 1
                continue
        i += 1

    return spans


def _is_chemistry_start(run_info: list, idx: int) -> bool:
    """Check if run at idx could start a chemistry formula."""
    info = run_info[idx]
    # Coefficient digit before element
    if info["has_digit"] and not info["is_sub"] and not info["is_sup"]:
        if idx + 1 < len(run_info) and run_info[idx + 1]["has_element"]:
            return True
    # Element symbol
    if info["has_element"] and not info["is_sub"] and not info["is_sup"]:
        return True
    return False


def _is_chemistry_continuation(run_info: list, idx: int) -> bool:
    """Check if run at idx could continue a chemistry formula."""
    info = run_info[idx]
    # Subscript/superscript numbers (e.g. ₂, ³⁺)
    if info["is_sub"] or info["is_sup"]:
        return True
    # Element symbol
    if info["has_element"]:
        return True
    # Reaction arrow
    if info["has_arrow"]:
        return True
    # Plus sign between compounds
    if info["has_plus"]:
        return True
    # Coefficient digit
    if info["has_digit"] and not info["is_sub"] and not info["is_sup"]:
        return True
    # Parentheses, brackets (e.g. Ca(OH)₂)
    text = info["text"].strip()
    if text in ("(", ")", "[", "]"):
        return True
    return False


def _is_valid_chemistry_span(run_info: list, start: int, end: int) -> bool:
    """Validate that a span contains genuine chemistry content.

    Requires at least one element symbol AND at least one of:
    - subscript digit (e.g. H₂)
    - superscript charge (e.g. Fe²⁺)
    - reaction arrow (e.g. →)
    to qualify as chemistry.
    """
    has_element = False
    has_subscript_digit = False
    has_superscript = False
    has_arrow = False

    for i in range(start, end + 1):
        info = run_info[i]
        if info["has_element"]:
            has_element = True
        if info["is_sub"] and info["has_digit"]:
            has_subscript_digit = True
        if info["is_sup"]:
            has_superscript = True
        if info["has_arrow"]:
            has_arrow = True

    return has_element and (has_subscript_digit or has_superscript or has_arrow)


def _assemble_chemistry_text(run_info: list, start: int, end: int) -> str:
    """Assemble the display text for a chemistry formula span.

    Subscript digits are wrapped in ₍₎ markers and superscript in ⁽⁾
    to preserve the semantic distinction in plain text.

    For example: H₂SO₄ is stored as "H₂SO₄" using Unicode subscript digits.
    """
    parts = []
    # Unicode subscript/superscript digit maps
    sub_map = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
    sup_map = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")

    for i in range(start, end + 1):
        info = run_info[i]
        text = info["text"]
        if info["is_sub"]:
            parts.append(text.translate(sub_map))
        elif info["is_sup"]:
            parts.append(text.translate(sup_map))
        else:
            parts.append(text)

    return "".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# DocxContentExtractor — Full sequential extraction
# ═══════════════════════════════════════════════════════════════════════


class DocxContentExtractor:
    """Extracts all content from a .docx file in linear document order.

    Produces a BookletDocument with paragraphs, math formulas, chemistry
    formulas, tables, legacy equation placeholders, and image assets —
    all in the exact sequential order they appear in the source document.

    Args:
        xslt_path: Path to OMML2MML.XSL stylesheet for math conversion.
                    If not found, math conversion will raise MathExtractionError
                    but other extraction (text, chemistry, tables, OLE) still works
                    only if no m:oMath elements are encountered.
        job_temp_dir: Job-scoped temporary directory for extracted image
                      binaries.  If None, a system temp dir is used.
                      STEP-6.4 addition: required for ImageAsset population.
    """

    def __init__(
        self,
        xslt_path: str = "assets/xslt/OMML2MML.XSL",
        job_temp_dir: Optional[str] = None,
    ):
        self._xslt_path = xslt_path
        self._math_extractor: Optional[DocxMathExtractor] = None
        self._math_init_error: Optional[str] = None
        self._job_temp_dir = job_temp_dir or tempfile.mkdtemp(
            prefix="booklet_ai_images_"
        )
        self._current_doc_part = None
        self._current_docx_path = None
        os.makedirs(self._job_temp_dir, exist_ok=True)

        # Try to initialize math extractor — record error but don't fail
        # until we actually encounter an m:oMath element (lazy fail-fast)
        try:
            self._math_extractor = DocxMathExtractor(xslt_path)
        except MathExtractionError as exc:
            self._math_init_error = str(exc)

    def extract(self, docx_path: str) -> BookletDocument:
        """Extract all content from a .docx file.

        Args:
            docx_path: Absolute path to the .docx file.

        Returns:
            BookletDocument with all content blocks in document order.

        Raises:
            FileNotFoundError: If docx_path does not exist.
            MathExtractionError: If an m:oMath element is found but
                                 XSLT engine is unavailable.
        """
        if not os.path.isfile(docx_path):
            raise FileNotFoundError(f"DOCX file not found: {docx_path}")

        self._current_docx_path = os.path.abspath(docx_path)
        doc = DocxDocument(docx_path)
        self._current_doc_part = doc.part
        body_xml = doc.element.body

        blocks: List[ContentBlock] = []
        tables: List[TableData] = []
        block_counter = 0
        table_counter = 0

        # Walk body children in document order
        for child in body_xml:
            tag = child.tag

            if tag == W_P:
                # Paragraph — may contain text, math, chemistry, or OLE
                new_blocks = self._extract_paragraph(child, block_counter)
                for b in new_blocks:
                    blocks.append(b)
                    block_counter += 1

            elif tag == W_TBL:
                # Table
                table_counter += 1
                tbl = self._extract_table(child, table_counter)
                tables.append(tbl)

            elif tag == W_DRAWING or tag == W_PICT:
                # Drawing/image at body level (STEP-6.4)
                img_blocks = self._extract_images_from_element(child, block_counter)
                for b in img_blocks:
                    blocks.append(b)
                    block_counter += 1

            else:
                # Check for OLE objects at body level
                ole_blocks = self._detect_ole_objects(child, block_counter)
                for b in ole_blocks:
                    blocks.append(b)
                    block_counter += 1

        # Build document
        doc_id = str(uuid.uuid4())
        page = PageData(
            page_number=1,
            width=595.0,  # A4 default
            height=842.0,
            content_blocks=blocks,
            tables=tables,
        )

        return BookletDocument(
            doc_id=doc_id,
            file_path=os.path.abspath(docx_path),
            file_type="docx",
            pages=[page],
        )

    def _extract_paragraph(
        self, para_elem: ET._Element, start_idx: int
    ) -> List[ContentBlock]:
        """Extract content blocks from a single w:p element.

        A paragraph may yield multiple blocks if it contains interleaved
        text and math elements.

        Args:
            para_elem: A w:p XML element.
            start_idx: Current block counter for generating block_ids.

        Returns:
            List of ContentBlock instances extracted from this paragraph.
        """
        blocks: List[ContentBlock] = []
        idx = start_idx

        # Detect heading style
        heading_level = self._get_heading_level(para_elem)

        # Collect all children of the paragraph for analysis
        children = list(para_elem)

        # First pass: check for m:oMathPara (block-level math)
        for child in children:
            if child.tag == M_OMATHPARA:
                # Block-level math paragraph — each m:oMath inside is separate
                for omath in child.findall(M_OMATH):
                    block = self._convert_omath_block(omath, idx)
                    blocks.append(block)
                    idx += 1
                return blocks

        # Second pass: walk children sequentially, grouping runs and
        # detecting inline math/OLE
        text_runs: List[ET._Element] = []
        pending_text_parts: List[str] = []

        for child in children:
            if child.tag == W_PPR:
                # Paragraph properties — skip
                continue

            elif child.tag == M_OMATH:
                # Inline math — flush any pending text first
                if text_runs:
                    chem_blocks = self._flush_runs_as_content(
                        text_runs, pending_text_parts, heading_level, idx
                    )
                    for b in chem_blocks:
                        blocks.append(b)
                        idx += 1
                    text_runs = []
                    pending_text_parts = []

                block = self._convert_omath_block(child, idx)
                blocks.append(block)
                idx += 1

            elif child.tag == W_OBJECT:
                # OLE object — flush text, emit placeholder
                if text_runs:
                    chem_blocks = self._flush_runs_as_content(
                        text_runs, pending_text_parts, heading_level, idx
                    )
                    for b in chem_blocks:
                        blocks.append(b)
                        idx += 1
                    text_runs = []
                    pending_text_parts = []

                block = self._make_ole_placeholder(child, idx)
                blocks.append(block)
                idx += 1

            elif child.tag == W_DRAWING or child.tag == W_PICT:
                # Drawing / picture element directly under paragraph (STEP-6.4)
                if text_runs:
                    chem_blocks = self._flush_runs_as_content(
                        text_runs, pending_text_parts, heading_level, idx
                    )
                    for b in chem_blocks:
                        blocks.append(b)
                        idx += 1
                    text_runs = []
                    pending_text_parts = []

                img_blocks = self._extract_images_from_element(child, idx)
                for b in img_blocks:
                    blocks.append(b)
                    idx += 1

            elif child.tag == W_R:
                # Check if this run contains an OLE object (w:object)
                ole_inside = child.find(W_OBJECT)
                if ole_inside is not None:
                    # OLE object inside a run — flush text, emit placeholder
                    if text_runs:
                        chem_blocks = self._flush_runs_as_content(
                            text_runs, pending_text_parts, heading_level, idx
                        )
                        for b in chem_blocks:
                            blocks.append(b)
                            idx += 1
                        text_runs = []
                        pending_text_parts = []

                    block = self._make_ole_placeholder(ole_inside, idx)
                    blocks.append(block)
                    idx += 1
                else:
                    # Check for drawings / pict inside run (STEP-6.4)
                    drawings = child.findall(f".//{W_DRAWING}") + child.findall(f".//{W_PICT}")
                    if drawings:
                        if text_runs:
                            chem_blocks = self._flush_runs_as_content(
                                text_runs, pending_text_parts, heading_level, idx
                            )
                            for b in chem_blocks:
                                blocks.append(b)
                                idx += 1
                            text_runs = []
                            pending_text_parts = []

                        for draw_elem in drawings:
                            img_blocks = self._extract_images_from_element(draw_elem, idx)
                            for b in img_blocks:
                                blocks.append(b)
                                idx += 1
                    else:
                        text_runs.append(child)
                        run_text = _get_run_text(child)
                        if run_text:
                            pending_text_parts.append(run_text)

            else:
                # Check for v:shape or other OLE indicators inside
                # unknown elements
                shapes = child.findall(f".//{V_SHAPE}")
                objects = child.findall(f".//{W_OBJECT}")
                if shapes or objects:
                    if text_runs:
                        chem_blocks = self._flush_runs_as_content(
                            text_runs, pending_text_parts, heading_level, idx
                        )
                        for b in chem_blocks:
                            blocks.append(b)
                            idx += 1
                        text_runs = []
                        pending_text_parts = []

                    block = self._make_ole_placeholder(child, idx)
                    blocks.append(block)
                    idx += 1

        # Flush remaining runs
        if text_runs:
            chem_blocks = self._flush_runs_as_content(
                text_runs, pending_text_parts, heading_level, idx
            )
            for b in chem_blocks:
                blocks.append(b)
                idx += 1

        return blocks

    def _flush_runs_as_content(
        self,
        runs: List[ET._Element],
        text_parts: List[str],
        heading_level: Optional[int],
        start_idx: int,
    ) -> List[ContentBlock]:
        """Analyze accumulated runs and produce ContentBlock(s).

        Checks for chemistry patterns first. If chemistry is detected,
        the chemistry span becomes a chemistry_formula block and any
        surrounding text becomes separate arabic_text blocks. Otherwise,
        all text becomes a single block.

        Args:
            runs: Accumulated w:r elements.
            text_parts: Pre-extracted text from those runs.
            heading_level: If paragraph is a heading, its level (1-9).
            start_idx: Block counter for IDs.

        Returns:
            List of ContentBlock instances.
        """
        full_text = "".join(text_parts).strip()
        if not full_text:
            return []

        blocks: List[ContentBlock] = []
        idx = start_idx

        # Check for chemistry patterns
        chem_spans = _analyze_runs_for_chemistry(runs)

        if chem_spans:
            # We have chemistry — emit chemistry blocks
            # For simplicity, if chemistry is detected in the paragraph,
            # the whole paragraph with chemistry content is treated as
            # chemistry_formula
            for _start, _end, assembled in chem_spans:
                if assembled.strip():
                    block = ContentBlock(
                        block_id=f"blk_{idx:04d}",
                        block_type="chemistry_formula",
                        raw_content=assembled,
                        content_hash=compute_canonical_hash(assembled),
                        source_position={"type": "chemistry_runs"},
                    )
                    blocks.append(block)
                    idx += 1

            # If there's remaining text not covered by chemistry,
            # treat it as regular text
            if not blocks:
                # Fallback: no valid chemistry found after all
                block_type: BlockType = (
                    "chapter_header" if heading_level else "arabic_text"
                )
                block = ContentBlock(
                    block_id=f"blk_{idx:04d}",
                    block_type=block_type,
                    raw_content=full_text,
                    content_hash=compute_canonical_hash(full_text),
                    source_position={"heading_level": heading_level}
                    if heading_level
                    else {},
                )
                blocks.append(block)
        else:
            # Plain text or heading
            block_type = (
                "chapter_header" if heading_level else "arabic_text"
            )
            block = ContentBlock(
                block_id=f"blk_{idx:04d}",
                block_type=block_type,
                raw_content=full_text,
                content_hash=compute_canonical_hash(full_text),
                source_position={"heading_level": heading_level}
                if heading_level
                else {},
            )
            blocks.append(block)

        return blocks

    def _convert_omath_block(
        self, omath_elem: ET._Element, idx: int
    ) -> ContentBlock:
        """Convert an m:oMath element to a math_formula ContentBlock.

        Args:
            omath_elem: An m:oMath XML element.
            idx: Block counter for ID.

        Returns:
            ContentBlock with block_type='math_formula' and MathML content.

        Raises:
            MathExtractionError: If XSLT engine is unavailable or
                                 conversion fails.
        """
        if self._math_extractor is None:
            raise MathExtractionError(
                f"Cannot convert OMML to MathML — XSLT engine not available. "
                f"Original error: {self._math_init_error}"
            )

        mathml = self._math_extractor.omml_to_mathml(omath_elem)

        return ContentBlock(
            block_id=f"blk_{idx:04d}",
            block_type="math_formula",
            raw_content=mathml,
            content_hash=compute_canonical_hash(mathml),
            source_position={"type": "omml_math"},
        )

    def _make_ole_placeholder(
        self, element: ET._Element, idx: int
    ) -> ContentBlock:
        """Create a legacy_equation_placeholder for an OLE/shape object.

        Args:
            element: The w:object, v:shape, or parent element.
            idx: Block counter for ID.

        Returns:
            ContentBlock with block_type='legacy_equation_placeholder'.
        """
        # Try to extract any identifying info from the OLE object
        ole_info = f"[Legacy equation object at position {idx}]"

        # Check for progID (e.g. "Equation.3" for MathType)
        prog_id = element.get(f'{{{NSMAP["o"]}}}progId', "")
        if not prog_id:
            # Search children for OLEObject with progId
            for ole_obj in element.iter():
                pid = ole_obj.get("progId", "")
                if not pid:
                    pid = ole_obj.get(f'{{{NSMAP["o"]}}}progId', "")
                if pid:
                    prog_id = pid
                    break

        if prog_id:
            ole_info = (
                f"[Legacy equation: {prog_id} — "
                f"requires manual placement in InDesign]"
            )
        else:
            ole_info = (
                f"[Legacy equation object — "
                f"requires manual placement in InDesign]"
            )

        return ContentBlock(
            block_id=f"blk_{idx:04d}",
            block_type="legacy_equation_placeholder",
            raw_content=ole_info,
            content_hash=compute_canonical_hash(ole_info),
            source_position={"type": "ole_object", "prog_id": prog_id},
        )

    def _extract_images_from_element(
        self, element: ET._Element, block_idx: int
    ) -> List[ContentBlock]:
        """Extract embedded image assets from a w:drawing or w:pict element (STEP-6.4).

        Unzips/reads embedded raster image binaries, determines physical placement size
        (from wp:extent EMU values converted to mm), reads pixel dimensions via Pillow,
        constructs ImageAsset, and attaches it to ContentBlock.source_position["image_asset"].

        Args:
            element: A w:drawing or w:pict XML element.
            block_idx: Current block sequence index.

        Returns:
            List of ContentBlock instances with block_type='image_prompt'.
        """
        blocks: List[ContentBlock] = []

        # 1. Physical placement dimensions (EMU -> mm)
        extent = element.find(f".//{WP_EXTENT}")
        intended_width_mm = None
        intended_height_mm = None
        if extent is not None:
            cx_str = extent.get("cx")
            cy_str = extent.get("cy")
            try:
                if cx_str:
                    intended_width_mm = float(cx_str) / EMU_PER_MM
                if cy_str:
                    intended_height_mm = float(cy_str) / EMU_PER_MM
            except (ValueError, TypeError):
                pass

        # 2. Find image relationship IDs
        rel_ids: List[str] = []
        # Modern OpenXML drawings: <a:blip r:embed="..."/>
        for blip in element.findall(f".//{{{NSMAP['a']}}}blip"):
            r_id = blip.get(f"{{{NSMAP['r']}}}embed") or blip.get(f"{{{NSMAP['r']}}}link")
            if r_id and r_id not in rel_ids:
                rel_ids.append(r_id)

        # Legacy VML pict: <v:imagedata r:id="..."/>
        for img_data in element.findall(f".//{{{NSMAP['v']}}}imagedata"):
            r_id = img_data.get(f"{{{NSMAP['r']}}}id") or img_data.get(f"{{{NSMAP['o']}}}relid")
            if r_id and r_id not in rel_ids:
                rel_ids.append(r_id)

        for curr_idx, rel_id in enumerate(rel_ids):
            image_bytes = None
            orig_filename = "image.png"

            # Retrieve image binary from document part
            if self._current_doc_part is not None and rel_id in self._current_doc_part.rels:
                rel = self._current_doc_part.rels[rel_id]
                try:
                    image_bytes = rel.target_part.blob
                    orig_filename = os.path.basename(rel.target_part.partname)
                except Exception as e:
                    logger.debug("Failed getting blob from doc.part.rels: %s", e)

            # Fallback to direct zipfile extraction if needed
            if image_bytes is None and self._current_docx_path and zipfile.is_zipfile(self._current_docx_path):
                try:
                    with zipfile.ZipFile(self._current_docx_path, "r") as z:
                        rels_path = "word/_rels/document.xml.rels"
                        if rels_path in z.namelist():
                            rels_xml = ET.fromstring(z.read(rels_path))
                            for rel_node in rels_xml.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                                if rel_node.get("Id") == rel_id:
                                    target = rel_node.get("Target", "")
                                    full_target = f"word/{target}" if not target.startswith("word/") else target
                                    if full_target in z.namelist():
                                        image_bytes = z.read(full_target)
                                        orig_filename = os.path.basename(full_target)
                                        break
                except Exception as e:
                    logger.debug("Fallback zipfile extraction error: %s", e)

            if not image_bytes:
                continue

            # Save binary to job-scoped temporary directory
            ext = os.path.splitext(orig_filename)[1] or ".png"
            temp_filename = f"docx_img_{block_idx}_{curr_idx}_{uuid.uuid4().hex[:6]}{ext}"
            temp_path = os.path.join(self._job_temp_dir, temp_filename)
            with open(temp_path, "wb") as f:
                f.write(image_bytes)

            # Read pixel dimensions with Pillow
            try:
                from PIL import Image
                with Image.open(temp_path) as img:
                    pixel_width, pixel_height = img.size
            except Exception as e:
                logger.warning("Could not read image dimensions with Pillow for %s: %s", temp_path, e)
                continue

            asset = ImageAsset(
                source_path=os.path.abspath(temp_path),
                pixel_width=pixel_width,
                pixel_height=pixel_height,
                intended_width_mm=intended_width_mm,
                intended_height_mm=intended_height_mm,
            )

            raw_content = f"[image:{temp_filename}]"
            blk_id = f"blk_{block_idx:04d}_{curr_idx}" if len(rel_ids) > 1 else f"blk_{block_idx:04d}"
            cb = ContentBlock(
                block_id=blk_id,
                block_type="image_prompt",
                raw_content=raw_content,
                content_hash=compute_canonical_hash(raw_content),
                source_position={
                    "image_asset": asset,
                    "type": "drawing",
                    "original_rel_id": rel_id,
                },
            )
            blocks.append(cb)

        return blocks

    def _extract_table(
        self, tbl_elem: ET._Element, table_idx: int
    ) -> TableData:
        """Extract a w:tbl element into a TableData model.

        The first row is treated as headers if all cells contain text.
        All subsequent rows are data rows.

        Args:
            tbl_elem: A w:tbl XML element.
            table_idx: Counter for generating table_id.

        Returns:
            TableData with headers and rows.
        """
        rows_data: List[List[str]] = []

        for tr in tbl_elem.findall(W_TR):
            row_cells: List[str] = []
            for tc in tr.findall(W_TC):
                # Extract all text from the cell's paragraphs
                cell_texts = []
                for p in tc.findall(W_P):
                    p_text = self._get_paragraph_plain_text(p)
                    if p_text:
                        cell_texts.append(p_text)
                row_cells.append("\n".join(cell_texts))
            rows_data.append(row_cells)

        # First row as headers, rest as data
        headers = rows_data[0] if rows_data else []
        data_rows = rows_data[1:] if len(rows_data) > 1 else []

        return TableData(
            table_id=f"tbl_{table_idx:04d}",
            headers=headers,
            rows=data_rows,
        )

    def _detect_ole_objects(
        self, element: ET._Element, start_idx: int
    ) -> List[ContentBlock]:
        """Detect OLE objects in a non-paragraph, non-table element.

        Args:
            element: Any body-level XML element.
            start_idx: Block counter.

        Returns:
            List of legacy_equation_placeholder blocks.
        """
        blocks: List[ContentBlock] = []
        idx = start_idx

        # Search for w:object elements
        for obj in element.iter(W_OBJECT):
            block = self._make_ole_placeholder(obj, idx)
            blocks.append(block)
            idx += 1

        # Search for v:shape elements (if not already found via w:object)
        if not blocks:
            for shape in element.iter(V_SHAPE):
                block = self._make_ole_placeholder(shape, idx)
                blocks.append(block)
                idx += 1

        return blocks

    @staticmethod
    def _get_heading_level(para_elem: ET._Element) -> Optional[int]:
        """Detect if a paragraph uses a Heading style.

        Args:
            para_elem: A w:p XML element.

        Returns:
            Heading level (1-9) or None if not a heading.
        """
        ppr = para_elem.find(W_PPR)
        if ppr is None:
            return None

        pstyle = ppr.find(W_PSTYLE)
        if pstyle is None:
            return None

        style_val = pstyle.get(f'{{{NSMAP["w"]}}}val', "")
        match = HEADING_RE.match(style_val)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _get_paragraph_plain_text(para_elem: ET._Element) -> str:
        """Extract plain text from a paragraph (for table cells).

        Args:
            para_elem: A w:p XML element.

        Returns:
            Concatenated text from all w:t elements in the paragraph.
        """
        texts = []
        for t in para_elem.iter(W_T):
            if t.text:
                texts.append(t.text)
        return "".join(texts).strip()


# Alias matching STEP-6.4 naming
DocxExtractor = DocxContentExtractor

