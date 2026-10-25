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
from app.core.text_integrity import verify_text_integrity
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
        "first_3_math_formulas": first_3_math,
        "actual_page_count": actual_page_count,
        "imposed_page_count": imposed_page_count,
        "final_page_count": final_page_count,
        "per_page_diagnostics": per_page_diagnostics,
        "warnings": combined_warnings,
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
    ])
    first_3 = report.get("first_3_math_formulas", [])
    if first_3:
        lines.append("  * First Math Formulas (sample up to 3):")
        for i, mf in enumerate(first_3, 1):
            sample = mf[:80] + "..." if len(mf) > 80 else mf
            lines.append(f"    [{i}] {sample}")
    else:
        lines.append("  * First Math Formulas: (none)")

    lines.extend([
        "",
        "── 4. Layout & Page Imposition ───────────────────────────────────────",
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
        "── 5. Timing per Stage ───────────────────────────────────────────────",
    ])
    for stage, sec in report.get("timings", {}).items():
        lines.append(f"  * {stage:<30}: {sec:.3f}s")

    warnings = report.get("warnings", [])
    lines.extend([
        "",
        f"── 6. Warnings ({len(warnings)}) ──────────────────────────────────────────────",
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
            "── 7. Output Artifacts ───────────────────────────────────────────────",
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
) -> int:
    """Execute the end-to-end extraction, classification, and InDesign generation pipeline.

    Args:
        docx_file: Input Word .docx file.
        output_dir: Target output directory.
        binding: Binding method ("saddle_stitch", "perfect_bound", "spiral").
        footer_text: Optional footer text prefix.
        no_ai: If True, force offline RuleBasedClassifier.
        build_only: If True, stop after payload creation without executing InDesign.

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

        # ── Stage 2: Classification ─────────────────────────────────────
        print(f"[+] Stage 2: Classifying blocks (mode: {'RuleBasedClassifier' if no_ai else 'AI with fallback'})...")
        booklet_doc, pipeline_warnings, classify_time = classify_document(booklet_doc, no_ai=no_ai)
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
        )

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # Print human-readable report
        print("\n" + format_report_text(report))
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

    args = parser.parse_args()
    return run_pipeline(
        docx_file=args.docx_file,
        output_dir=args.output_dir,
        binding=args.binding,
        footer_text=args.footer_text,
        no_ai=args.no_ai,
        build_only=args.build_only,
    )


if __name__ == "__main__":
    sys.exit(main())
