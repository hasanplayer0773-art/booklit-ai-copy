"""
Booklet_AI Desktop — End-to-End CLI on Real Word (.docx) File (STEP-7.2)

Measures and reports every stage of the document generation pipeline:
1. Extraction: paragraphs, tables, m:oMath, legacy OLE equations.
2. Canonical Hash Verification (Zero-Mutation / TextIntegrity check).
3. Classification: AI via OpenRouter (if API key available & --no-ai not set)
   or deterministic RuleBasedClassifier fallback.
4. Payload Generation & InDesign COM Automation via COMSupervisor.
5. Measurement Report generation and serialization to report.json.

Usage:
    python tools/generate_from_docx.py <file.docx> --out build/real_test
    python tools/generate_from_docx.py sample.docx --out build/my_test --binding saddle_stitch --footer-text "الفصل الأول"
    python tools/generate_from_docx.py sample.docx --out build/my_test --no-ai
    python tools/generate_from_docx.py sample.docx --out build/my_test --build-only
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure src/ is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from docx import Document as DocxDocument

from app.ai.classifier import PRECLASSIFIED_TYPES, RuleBasedClassifier
from app.ai.client import OpenRouterClient
from app.automation.com_supervisor import COMSupervisor
from app.automation.generation_pipeline import (
    build_payload,
    submit_generate_booklet_job,
)
from app.automation.geometry import MarginConfig, PageGeometry, PageNumberingStyle
from app.automation.styles_schema import TypographyStylesConfig
from app.core.docx_extractor import DocxContentExtractor
from app.core.exceptions import (
    BookletError,
    COMOperationTimeout,
    GlyphCoverageError,
    InDesignUnavailableError,
    MissingFontError,
    TextIntegrityError,
)
from app.core.models import BookletDocument, ContentBlock
from app.core.text_integrity import canonical_normalize_text, verify_text_integrity
from app.utils.secrets_manager import SecretsManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("generate_from_docx")


def extract_document(
    docx_path: Union[str, Path],
) -> Tuple[BookletDocument, Dict[str, int], float]:
    """Stage 1: Extract content from DOCX and verify canonical hashes.

    Args:
        docx_path: Path to the input Word .docx file.

    Returns:
        Tuple of (BookletDocument, source_counts dict, extraction elapsed seconds).
    """
    t0 = time.perf_counter()
    p = Path(docx_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"DOCX file not found: {p}")

    # Inspect source DOCX for element counts directly from structure
    raw_doc = DocxDocument(str(p))
    source_paragraphs = len(raw_doc.paragraphs)
    source_tables = len(raw_doc.tables)

    # Perform sequential extraction with existing DocxContentExtractor
    extractor = DocxContentExtractor()
    booklet_doc = extractor.extract(str(p))

    # Zero-Mutation guarantee: verify canonical hash for every extracted block
    omml_math_count = 0
    legacy_ole_count = 0
    for page in booklet_doc.pages:
        for block in page.content_blocks:
            verify_text_integrity(
                block.raw_content,
                block.content_hash,
                context=f"DOCX extraction block {block.block_id}",
            )
            if block.block_type == "math_formula":
                omml_math_count += 1
            elif block.block_type == "legacy_equation_placeholder":
                legacy_ole_count += 1

    source_counts = {
        "paragraphs": source_paragraphs,
        "tables": source_tables,
        "equations": omml_math_count + legacy_ole_count,
        "omml_math": omml_math_count,
        "legacy_equations": legacy_ole_count,
    }

    elapsed = time.perf_counter() - t0
    return booklet_doc, source_counts, elapsed


def _is_non_text_block(block: Any) -> bool:
    """Determine whether a block represents non-text content for coverage audit."""
    if block.block_type in ("image_prompt", "legacy_equation_placeholder", "math_formula"):
        return True
    sp = getattr(block, "source_position", None)
    if isinstance(sp, dict) and sp.get("image_asset") is not None:
        return True
    return False


def run_coverage_audit(
    docx_path: Union[str, Path],
    booklet_doc: BookletDocument,
    allow_loss: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    """Audit source .docx body elements against extracted BookletDocument.

    1. Prints and returns one audit row per body element (w:p, w:tbl, drawings).
    2. Runs text-coverage check: canonical concatenation of non-empty source text
       vs output blocks + table cells.
    3. Lists every source paragraph not found.
    4. On loss adds warning TEXT_DROPPED and signals failure.

    Returns:
        Tuple of (audit_rows, audit_warnings, text_loss_detected).
    """
    p = Path(docx_path).resolve()
    raw_doc = DocxDocument(str(p))

    audit_rows = list(booklet_doc.audit_rows)
    print("\n── Coverage Audit (Body Elements in Order) ──────────────────────────")
    for row in audit_rows:
        idx = row.get("index", 0)
        elem_type = row.get("element_type", "")
        if elem_type == "paragraph":
            p_prev = row.get("preview", "")
            emp = row.get("is_empty", False)
            br = row.get("br_count", 0)
            bl = row.get("blocks", [])
            bl_str = bl if bl != [] else "(empty)"
            print(f"  [AUDIT] Paragraph [{idx:02d}]: preview={repr(p_prev[:35])}, empty={emp}, br={br} -> {bl_str}")
        elif elem_type == "table":
            prev = row.get("preview", "")
            bv = row.get("has_bidi_visual", "no")
            mrg = row.get("has_merged", "no")
            tid = row.get("table_id", "")
            print(f"  [AUDIT] Table     [{idx:02d}]: {prev}, bidiVisual={bv}, merged={mrg} -> {tid}")
        elif elem_type == "drawing":
            bl = row.get("blocks", [])
            print(f"  [AUDIT] Drawing   [{idx:02d}]: -> {bl}")
        else:
            prev = row.get("preview", "")
            bl = row.get("blocks", [])
            print(f"  [AUDIT] Element   [{idx:02d}] ({elem_type}): {prev} -> {bl}")

    # ── Text Coverage Check ─────────────────────────────────────────────
    source_paragraphs = [para for para in raw_doc.paragraphs if para.text.strip()]

    def _audit_norm(text: str) -> str:
        return canonical_normalize_text(text).replace("\u2212", "-")

    out_text_blocks = [
        b.raw_content
        for page in booklet_doc.pages
        for b in page.content_blocks
        if not _is_non_text_block(b)
    ]
    out_canonical_blocks = [_audit_norm(b) for b in out_text_blocks]

    missing_paragraphs: List[str] = []
    for p_idx, para in enumerate(source_paragraphs):
        lines = [line.strip() for line in para.text.split("\n") if line.strip()]
        for line in lines:
            c_line = _audit_norm(line)
            if not c_line:
                continue
            found = any(c_line in ob or ob in c_line for ob in out_canonical_blocks)
            if not found:
                for page in booklet_doc.pages:
                    for t in page.tables:
                        for row in [t.headers] + t.rows:
                            for cell in row:
                                if c_line in _audit_norm(cell):
                                    found = True
                                    break
            if not found:
                missing_paragraphs.append(f"Paragraph {p_idx}: {repr(line[:40])}")

    src_items: List[str] = []
    for para in source_paragraphs:
        for line in para.text.split("\n"):
            cl = _audit_norm(line)
            if cl:
                src_items.append(cl)
    for t in raw_doc.tables:
        for row in t.rows:
            for cell in row.cells:
                cc = _audit_norm(cell.text)
                if cc:
                    src_items.append(cc)

    out_items: List[str] = []
    for page in booklet_doc.pages:
        for b in page.content_blocks:
            if not _is_non_text_block(b):
                cb = _audit_norm(b.raw_content)
                if cb:
                    out_items.append(cb)
        for t in page.tables:
            for h in t.headers:
                ch = _audit_norm(h)
                if ch:
                    out_items.append(ch)
            for r in t.rows:
                for c in r:
                    cr = _audit_norm(c)
                    if cr:
                        out_items.append(cr)

    src_canon = " ".join(sorted(src_items))
    out_canon = " ".join(sorted(out_items))

    text_loss = (src_canon != out_canon) or bool(missing_paragraphs)
    audit_warnings: List[str] = []

    print("\n── Text Coverage Verification ───────────────────────────────────────")
    if missing_paragraphs:
        print(f"  [!] Missing source paragraphs ({len(missing_paragraphs)}):")
        for m in missing_paragraphs:
            print(f"      * {m}")

    if text_loss:
        audit_warnings.append("TEXT_DROPPED")
        print("  [✗] Text-coverage check FAILED: TEXT_DROPPED (source text dropped)")
    else:
        print("  [✓] Text-coverage check PASSED: 100% source text preserved (0 characters lost)")

    return audit_rows, audit_warnings, text_loss


def run_order_check(
    docx_path: Union[str, Path],
    payload_items: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """Compare sequence of item types in payload against sequence of body elements in source.

    Compares semantic sequence: paragraph blocks / table / equation.
    On mismatch adds warning ORDER_MISMATCH. Prints 'ORDER_CHECK: OK' on match.

    Returns:
        Tuple of (is_ok: bool, warnings: List[str]).
    """
    p = Path(docx_path).resolve()
    doc = DocxDocument(str(p))
    body = doc.element.body

    source_seq: List[str] = []
    for child in body:
        tag = child.tag
        if tag.endswith("sectPr"):
            continue
        if tag.endswith("tbl"):
            source_seq.append("table")
        elif tag.endswith("p"):
            p_items: List[str] = []
            for p_child in child:
                ctag = p_child.tag
                if ctag.endswith("oMath") or ctag.endswith("oMathPara") or ctag.endswith("object"):
                    p_items.append("equation")
                elif ctag.endswith("drawing") or ctag.endswith("pict"):
                    p_items.append("drawing")
                elif ctag.endswith("r"):
                    if any(e.tag.endswith("oMath") or e.tag.endswith("object") for e in p_child.iter()):
                        p_items.append("equation")
                    elif any(e.tag.endswith("drawing") or e.tag.endswith("pict") for e in p_child.iter()):
                        p_items.append("drawing")
                    else:
                        t = "".join(e.text for e in p_child.iter() if e.tag.endswith("t") and e.text).strip()
                        if t:
                            p_items.append("paragraph")
                else:
                    t = "".join(e.text for e in p_child.iter() if e.tag.endswith("t") and e.text).strip()
                    if t:
                        p_items.append("paragraph")

            collapsed_p = [k for k, _ in itertools.groupby(p_items)]
            source_seq.extend(collapsed_p)

    source_collapsed = [k for k, _ in itertools.groupby(source_seq)]

    payload_seq: List[str] = []
    for item in payload_items:
        i_type = item.get("item_type")
        b_type = item.get("block_type")
        if i_type == "table" or (not b_type and ("headers" in item or "table_id" in item)):
            payload_seq.append("table")
        elif b_type in ("math_formula", "legacy_equation_placeholder"):
            payload_seq.append("equation")
        elif b_type in ("image_prompt", "image_asset"):
            payload_seq.append("drawing")
        else:
            payload_seq.append("paragraph")

    payload_collapsed = [k for k, _ in itertools.groupby(payload_seq)]

    # Check non-decreasing body_index if available
    body_indices = [
        item.get("source_position", {}).get("body_index")
        if item.get("item_type") == "block"
        else item.get("body_index")
        for item in payload_items
    ]
    is_monotonic = True
    if payload_items and all(idx is not None for idx in body_indices):
        is_monotonic = all(
            body_indices[i] <= body_indices[i + 1]
            for i in range(len(body_indices) - 1)
        )

    is_ok = (source_collapsed == payload_collapsed) and is_monotonic
    warnings: List[str] = []

    print("\n── Document Order Verification (ORDER_CHECK) ────────────────────────")
    if is_ok:
        print("ORDER_CHECK: OK")
    else:
        warnings.append("ORDER_MISMATCH")
        print(f"  [!] Warning: ORDER_MISMATCH (source: {source_collapsed} vs payload: {payload_collapsed}, monotonic={is_monotonic})")

    return is_ok, warnings


def classify_document(
    booklet_doc: BookletDocument,
    no_ai: bool = False,
) -> Tuple[BookletDocument, List[str], float]:
    """Stage 2: Classify blocks with AI or RuleBasedClassifier.

    Args:
        booklet_doc: Extracted BookletDocument.
        no_ai: If True, bypass OpenRouter and use RuleBasedClassifier.

    Returns:
        Tuple of (classified BookletDocument, warnings list, classification elapsed seconds).
    """
    t0 = time.perf_counter()
    warnings: List[str] = []

    all_blocks: List[ContentBlock] = []
    for page in booklet_doc.pages:
        all_blocks.extend(page.content_blocks)

    classified_blocks: List[ContentBlock] = []

    if not no_ai:
        api_key = SecretsManager().get_api_key("openrouter") or os.environ.get("OPENROUTER_API_KEY")
        if api_key:
            try:
                def _on_fallback(msg: str) -> None:
                    warnings.append(f"AI classification fallback: {msg}")
                    print(f"    [!] Warning: {msg}")

                client = OpenRouterClient(api_key=api_key, on_fallback=_on_fallback)
                classified_blocks = client.classify_blocks(all_blocks)
            except Exception as exc:
                warn_msg = (
                    f"OpenRouter classification failed ({type(exc).__name__}: {exc}). "
                    "Falling back to RuleBasedClassifier."
                )
                warnings.append(warn_msg)
                print(f"    [!] Warning: {warn_msg}")
                classified_blocks = RuleBasedClassifier.classify_document_blocks(all_blocks)
        else:
            classified_blocks = RuleBasedClassifier.classify_document_blocks(all_blocks)
    else:
        classified_blocks = RuleBasedClassifier.classify_document_blocks(all_blocks)

    # Re-verify integrity after classification (Zero-Mutation guarantee)
    for blk in classified_blocks:
        verify_text_integrity(
            blk.raw_content,
            blk.content_hash,
            context=f"Post-classification block {blk.block_id}",
        )

    # Construct new pages and document (models are frozen)
    idx = 0
    new_pages = []
    for page in booklet_doc.pages:
        p_len = len(page.content_blocks)
        new_page = page.model_copy(
            update={"content_blocks": classified_blocks[idx : idx + p_len]}
        )
        new_pages.append(new_page)
        idx += p_len

    new_booklet_doc = booklet_doc.model_copy(update={"pages": new_pages})
    elapsed = time.perf_counter() - t0
    return new_booklet_doc, warnings, elapsed


def build_report(
    booklet_doc: BookletDocument,
    source_counts: Dict[str, int],
    timings: Dict[str, float],
    indesign_result: Optional[Dict[str, Any]] = None,
    pipeline_warnings: Optional[List[str]] = None,
    output_files: Optional[Dict[str, str]] = None,
    math_stats: Optional[Dict[str, int]] = None,
    image_stats: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Construct structured report dictionary adhering to STEP-7.2 specification.

    Args:
        booklet_doc: Classified BookletDocument.
        source_counts: Source counts dict (paragraphs, tables, equations, etc.).
        timings: Timing dict per stage in seconds.
        indesign_result: Result dict from COMSupervisor if generation executed.
        pipeline_warnings: Optional list of pipeline warnings.
        output_files: Paths to generated outputs.

    Returns:
        Structured report dictionary ready for JSON serialization.
    """
    all_blocks: List[ContentBlock] = []
    for page in booklet_doc.pages:
        all_blocks.extend(page.content_blocks)

    # Blocks per block_type
    blocks_per_type: Dict[str, int] = {}
    math_blocks: List[ContentBlock] = []
    legacy_equation_count = 0

    for blk in all_blocks:
        b_type = blk.block_type
        blocks_per_type[b_type] = blocks_per_type.get(b_type, 0) + 1
        if b_type == "math_formula":
            math_blocks.append(blk)
        elif b_type == "legacy_equation_placeholder":
            legacy_equation_count += 1

    first_3_math = [b.raw_content for b in math_blocks[:3]]

    # Diagnostics and page counts from InDesign result
    actual_page_count = None
    imposed_page_count = None
    final_page_count = None
    per_page_diagnostics = []
    combined_warnings = list(pipeline_warnings or [])

    if indesign_result:
        actual_page_count = indesign_result.get("actual_page_count")
        imposed_page_count = indesign_result.get("imposed_page_count")
        final_page_count = indesign_result.get("page_count")
        per_page_diagnostics = indesign_result.get("pages", [])
        combined_warnings.extend(indesign_result.get("warnings", []))

    report = {
        "doc_id": booklet_doc.doc_id,
        "source_file": booklet_doc.file_path,
        "source_counts": source_counts,
        "blocks_per_type": blocks_per_type,
        "total_blocks": len(all_blocks),
        "legacy_equation_count": legacy_equation_count,
        "math_formula_count": len(math_blocks),
        "math_rendered_count": (math_stats or {}).get("rendered", 0),
        "math_failed_count": (math_stats or {}).get("failed", 0),
        "first_3_math_formulas": first_3_math,
        "image_stats": {
            "found": (image_stats or {}).get("found", 0),
            "copied": (image_stats or {}).get("copied", 0),
        },
        "actual_page_count": actual_page_count,
        "imposed_page_count": imposed_page_count,
        "final_page_count": final_page_count,
        "per_page_diagnostics": per_page_diagnostics,
        "warnings": combined_warnings,
        "coverage_audit": getattr(booklet_doc, "audit_rows", []),
        "timings": timings,
        "output_files": output_files or {},
    }
    return report


def format_report_text(report: Dict[str, Any]) -> str:
    """Format human-readable CLI measurement report.

    Args:
        report: Structured report dictionary.

    Returns:
        Formatted multi-line report string for display.
    """
    lines = [
        "=" * 70,
        " Booklet_AI — STEP-7.2 Real DOCX Measurement Report",
        "=" * 70,
        f"Source File: {report.get('source_file')}",
        "",
        "── 1. Source Document Counts ─────────────────────────────────────────",
        f"  * Paragraphs:       {report['source_counts'].get('paragraphs', 0)}",
        f"  * Tables:           {report['source_counts'].get('tables', 0)}",
        f"  * Equations:        {report['source_counts'].get('equations', 0)}",
        f"    - OMML Math:      {report['source_counts'].get('omml_math', 0)}",
        f"    - Legacy OLE:     {report['source_counts'].get('legacy_equations', 0)}",
        "",
        "── 2. Content Blocks Breakdown ───────────────────────────────────────",
        f"  * Total Blocks:     {report.get('total_blocks', 0)}",
    ]
    for b_type, count in sorted(report.get("blocks_per_type", {}).items()):
        lines.append(f"    - {b_type:<25}: {count}")

    lines.extend([
        "",
        "── 3. Math & Equation Diagnostics ────────────────────────────────────",
        f"  * Legacy Equation Placeholders: {report.get('legacy_equation_count', 0)}",
        f"  * Math Formula Blocks:          {report.get('math_formula_count', 0)}",
        f"  * Math Equations Rendered:      {report.get('math_rendered_count', 0)}",
        f"  * Math Equations Failed:        {report.get('math_failed_count', 0)}",
    ])
    first_3 = report.get("first_3_math_formulas", [])
    if first_3:
        lines.append("  * First Math Formulas (sample up to 3):")
        for i, mf in enumerate(first_3, 1):
            sample = mf[:80] + "..." if len(mf) > 80 else mf
            lines.append(f"    [{i}] {sample}")
    else:
        lines.append("  * First Math Formulas: (none)")

    img_stats = report.get("image_stats", {})
    lines.extend([
        "",
        "── 4. Images Diagnostics ─────────────────────────────────────────────",
        f"  * Images Found:                 {img_stats.get('found', 0)}",
        f"  * Images Copied:                {img_stats.get('copied', 0)}",
    ])

    lines.extend([
        "",
        "── 5. Layout & Page Imposition ───────────────────────────────────────",
        f"  * Actual Content Pages:  {report.get('actual_page_count')}",
        f"  * Imposed Target Pages:  {report.get('imposed_page_count')}",
        f"  * Final Document Pages:  {report.get('final_page_count')}",
    ])
    pages = report.get("per_page_diagnostics", [])
    if pages:
        lines.append("  * Per-Page Diagnostics:")
        for p in pages:
            p_num = p.get("page_number")
            p_side = p.get("side")
            p_bounds = p.get("geometric_bounds") or p.get("geometricBounds")
            t_w = p.get("table_width")
            t_str = f", table_width={t_w:.2f}pt" if t_w is not None else ""
            lines.append(f"    - Page {p_num} (side={p_side}): bounds={p_bounds}{t_str}")

    lines.extend([
        "",
        "── 6. Timing per Stage ───────────────────────────────────────────────",
    ])
    for stage, sec in report.get("timings", {}).items():
        lines.append(f"  * {stage:<30}: {sec:.3f}s")

    warnings = report.get("warnings", [])
    lines.extend([
        "",
        f"── 7. Warnings ({len(warnings)}) ──────────────────────────────────────────────",
    ])
    if warnings:
        for w in warnings:
            lines.append(f"  * {w}")
    else:
        lines.append("  (none)")

    outputs = report.get("output_files", {})
    if outputs:
        lines.extend([
            "",
            "── 8. Output Artifacts ───────────────────────────────────────────────",
        ])
        for k, path in outputs.items():
            lines.append(f"  * {k:<15}: {path}")

    lines.append("=" * 70)
    return "\n".join(lines)


def run_pipeline(
    docx_file: Path,
    output_dir: Path,
    binding: str = "saddle_stitch",
    footer_text: Optional[str] = None,
    no_ai: bool = False,
    build_only: bool = False,
    allow_loss: bool = False,
) -> int:
    """Execute the end-to-end extraction, classification, and InDesign generation pipeline.

    Args:
        docx_file: Input Word .docx file.
        output_dir: Target output directory.
        binding: Binding method ("saddle_stitch", "perfect_bound", "spiral").
        footer_text: Optional footer text prefix.
        no_ai: If True, force offline RuleBasedClassifier.
        build_only: If True, stop after payload creation without executing InDesign.
        allow_loss: If True, do not exit non-zero on text loss.

    Returns:
        Exit code: 0 on success, non-zero on error.
    """
    total_start = time.perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Stage 1: Extraction & Hash Verification ─────────────────────
        print(f"[+] Stage 1: Extracting content from '{docx_file}'...")
        booklet_doc, source_counts, extract_time = extract_document(docx_file)
        print(
            f"[✓] Extracted: {source_counts['paragraphs']} paragraphs, "
            f"{source_counts['tables']} tables, {source_counts['equations']} equations "
            f"({extract_time:.3f}s)"
        )

        # ── Stage 1b: Coverage Audit & Text Integrity Check ─────────────
        pipeline_warnings: List[str] = list(booklet_doc.warnings)
        audit_rows, audit_warnings, text_loss = run_coverage_audit(
            docx_file, booklet_doc, allow_loss=allow_loss
        )
        pipeline_warnings.extend(audit_warnings)

        if text_loss and not allow_loss:
            print(f"\n[✗] Pipeline aborted: text loss detected (TEXT_DROPPED).")
            print(f"    Pass --allow-loss to proceed anyway.")
            print(f"[!] Real warnings count: {len(pipeline_warnings)}")
            return 1

        # ── Stage 2: Classification ─────────────────────────────────────
        print(f"[+] Stage 2: Classifying blocks (mode: {'RuleBasedClassifier' if no_ai else 'AI with fallback'})...")
        booklet_doc, class_warnings, classify_time = classify_document(booklet_doc, no_ai=no_ai)
        pipeline_warnings.extend(class_warnings)
        print(f"[✓] Classified {len(booklet_doc.pages[0].content_blocks)} blocks ({classify_time:.3f}s)")

        # ── Stage 3: Geometry, Styles & Payload Build ───────────────────
        print(f"[+] Stage 3: Building JSON payload in '{output_dir}'...")
        t0_payload = time.perf_counter()

        geometry = PageGeometry(
            page_size="A4",
            width_pt=595.28,
            height_pt=841.89,
            facing_pages=True,
            margins=MarginConfig(
                top_mm=20.0,
                bottom_mm=20.0,
                inside_spine_mm=22.0,
                outside_mm=15.0,
            ),
            numbering_style=PageNumberingStyle.ARABIC_INDIC,
        )
        styles_cfg = TypographyStylesConfig(
            body_font="Tajawal",
            math_font="Cambria Math",
        )

        payload_path = build_payload(
            document=booklet_doc,
            geometry=geometry,
            styles_cfg=styles_cfg,
            output_dir=output_dir,
            binding=binding,
            footer_text=footer_text,
        )
        payload_time = time.perf_counter() - t0_payload
        print(f"[✓] Payload JSON created: {payload_path} ({payload_time:.3f}s)")

        # ── Stage 3b: ORDER_CHECK Verification ──────────────────────────
        with open(payload_path, "r", encoding="utf-8") as f:
            payload_json = json.load(f)
        math_stats = payload_json.get("math_stats", {})
        image_stats = payload_json.get("image_stats", {})
        if math_stats.get("rendered", 0) > 0 and math_stats.get("failed", 0) == 0:
            pipeline_warnings = [w for w in pipeline_warnings if w != "MATHML_NOT_RENDERABLE"]
        for w in booklet_doc.warnings:
            if w not in pipeline_warnings:
                pipeline_warnings.append(w)

        _, order_warnings = run_order_check(
            docx_path=docx_file,
            payload_items=payload_json.get("items", []),
        )
        pipeline_warnings.extend(order_warnings)

        indesign_result: Optional[Dict[str, Any]] = None
        indesign_time = 0.0

        if build_only:
            print("\n[✓] --build-only specified. Skipping InDesign COM execution.")
        else:
            # ── Stage 4: InDesign COM Generation ────────────────────────
            print("\n[+] Stage 4: Submitting generation job to InDesign via COMSupervisor...")
            t0_indesign = time.perf_counter()
            supervisor = COMSupervisor(heartbeat_timeout_seconds=90.0)
            indesign_result = submit_generate_booklet_job(
                payload_path=payload_path,
                supervisor=supervisor,
            )
            indesign_time = time.perf_counter() - t0_indesign
            print(f"[✓] InDesign generation finished ({indesign_time:.3f}s)")

        # ── Stage 5: Compile and Save Report ────────────────────────────
        total_time = time.perf_counter() - total_start
        timings = {
            "extraction_seconds": extract_time,
            "classification_seconds": classify_time,
            "payload_build_seconds": payload_time,
            "indesign_generation_seconds": indesign_time,
            "total_seconds": total_time,
        }

        report_file = output_dir / "report.json"
        output_files: Dict[str, str] = {
            "payload_json": str(payload_path.resolve()),
            "report_json": str(report_file.resolve()),
        }
        if indesign_result:
            if "output_indd" in indesign_result:
                output_files["output_indd"] = str(indesign_result["output_indd"])
            if "output_pdf" in indesign_result:
                output_files["output_pdf"] = str(indesign_result["output_pdf"])

        report = build_report(
            booklet_doc=booklet_doc,
            source_counts=source_counts,
            timings=timings,
            indesign_result=indesign_result,
            pipeline_warnings=pipeline_warnings,
            output_files=output_files,
            math_stats=math_stats,
            image_stats=image_stats,
        )

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # Print human-readable report
        print("\n" + format_report_text(report))
        print(f"\n[!] Real warnings count: {len(report.get('warnings', []))}")
        print(f"[✓] Report saved to: {report_file}")
        return 0

    except InDesignUnavailableError as exc:
        print(f"\n[!] InDesign is not available on this machine:\n    {exc}")
        print("    Payload and extraction were verified. Run on a machine with Adobe InDesign.")
        return 0
    except BookletError as exc:
        print(f"\n[✗] Generation failed with BookletError:\n    {type(exc).__name__}: {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"\n[✗] File not found error:\n    {exc}")
        return 1
    except Exception as exc:
        print(f"\n[✗] Unexpected pipeline failure:\n    {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Booklet_AI — End-to-End CLI on Real Word (.docx) File (STEP-7.2)",
    )
    parser.add_argument(
        "docx_file",
        type=Path,
        help="Path to the input Word .docx file",
    )
    parser.add_argument(
        "--out",
        dest="output_dir",
        type=Path,
        default=Path("build/real_test"),
        help="Output directory (default: build/real_test)",
    )
    parser.add_argument(
        "--binding",
        choices=["saddle_stitch", "perfect_bound", "spiral"],
        default="saddle_stitch",
        help="Booklet binding method (default: saddle_stitch)",
    )
    parser.add_argument(
        "--footer-text",
        type=str,
        default=None,
        help="Custom footer text prefix (default: none / from first page)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable OpenRouter AI classification and use deterministic RuleBasedClassifier",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build payload and report without executing InDesign COM job (for offline testing)",
    )
    parser.add_argument(
        "--allow-loss",
        action="store_true",
        help="Allow text loss without failing exit code (default: exit non-zero on text loss)",
    )

    args = parser.parse_args()
    return run_pipeline(
        docx_file=args.docx_file,
        output_dir=args.output_dir,
        binding=args.binding,
        footer_text=args.footer_text,
        no_ai=args.no_ai,
        build_only=args.build_only,
        allow_loss=args.allow_loss,
    )


if __name__ == "__main__":
    sys.exit(main())
