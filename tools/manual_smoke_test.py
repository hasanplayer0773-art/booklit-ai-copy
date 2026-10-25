"""
Booklet_AI Desktop — Manual InDesign Smoke Test (STEP-7.1)

Generates a realistic 3-page sample booklet containing:
1. Chapter Heading (chapter_header)
2. Arabic Body Text (arabic_text)
3. Ministerial Question Callout (ministry_question)
4. RTL Arabic Table (TableData)
5. Mathematical Formula (math_formula, strict LTR)

Usage:
    # Full execution with InDesign COM automation:
    python tools/manual_smoke_test.py

    # Build payload only (without requiring InDesign installed):
    python tools/manual_smoke_test.py --build-only

    # Specify custom output directory:
    python tools/manual_smoke_test.py --output-dir "C:/path/to/output"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure src/ is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from app.automation.com_supervisor import COMSupervisor
from app.automation.generation_pipeline import build_payload, generate_booklet
from app.automation.geometry import MarginConfig, PageGeometry, PageNumberingStyle
from app.automation.pagination_sync import BindingMethod
from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import BookletError, InDesignUnavailableError
from app.core.models import BookletDocument, ContentBlock, PageData, TableData
from app.core.text_integrity import compute_canonical_hash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("manual_smoke_test")


def create_sample_3page_document() -> BookletDocument:
    """Build a realistic 3-page sample booklet document with all required block types."""
    # ── Page 1: Heading + Arabic Paragraph ─────────────────────────────
    h1_text = "الفصل الأول: الأعداد المركبة ومبرهنة ديموافر"
    h1_block = ContentBlock(
        block_id="block-h1",
        block_type="chapter_header",
        raw_content=h1_text,
        content_hash=compute_canonical_hash(h1_text),
        source_position={"page": 1, "paragraph_index": 0},
    )

    p1_text = (
        "العدد المركب هو أي عدد يمكن كتابته على الصورة a + bi حيث a و b عددان حقيقيان، "
        "و i هو الوحدة التخيلية التي تحقق الخاصية الأساسية i² = -1. "
        "تُعد الأعداد المركبة امتداداً طبيعياً لمجموعة الأعداد الحقيقية، وتلعب دوراً جوهرياً "
        "في حل المعادلات التربيعية والتكعيبية ودراسة الظواهر الموجية والفيزيائية."
    )
    p1_block = ContentBlock(
        block_id="block-p1",
        block_type="arabic_text",
        raw_content=p1_text,
        content_hash=compute_canonical_hash(p1_text),
        source_position={"page": 1, "paragraph_index": 1},
    )

    page1 = PageData(
        page_number=1,
        width=595.28,
        height=841.89,
        header_text="الرياضيات — السادس العلمي",
        footer_text="الفصل الأول",
        content_blocks=[h1_block, p1_block],
        tables=[],
    )

    # ── Page 2: Ministry Question + RTL Table ──────────────────────────
    q_text = (
        "سؤال وزاري (2022 الدور الأول): إذا كان العدد المركب z = 1 + i√3، "
        "فجد كلاً من المقياس والسعة الأساسية للعدد z، ثم اكتبه بالصيغة القطبية، "
        "واحسب z⁶ باستخدام مبرهنة ديموافر."
    )
    q_block = ContentBlock(
        block_id="block-q1",
        block_type="ministry_question",
        raw_content=q_text,
        content_hash=compute_canonical_hash(q_text),
        source_position={"page": 2, "paragraph_index": 0},
    )

    table1 = TableData(
        table_id="table-01",
        headers=["الرمز الرياضي", "المعنى الاصطلاحي", "القانون / الخاصية"],
        rows=[
            ["r = |z|", "المقياس (Modulus)", "r = √(x² + y²)"],
            ["θ = arg(z)", "السعة الأساسية", "cos θ = x/r , sin θ = y/r"],
            ["z = r(cos θ + i sin θ)", "الصيغة القطبية", "صيغة أويلر المقابلة"],
            ["zⁿ = rⁿ(cos nθ + i sin nθ)", "مبرهنة ديموافر", "لأي عدد صحيح n"],
        ],
    )

    page2 = PageData(
        page_number=2,
        width=595.28,
        height=841.89,
        header_text="الرياضيات — السادس العلمي",
        footer_text="الفصل الأول",
        content_blocks=[q_block],
        tables=[table1],
    )

    # ── Page 3: Math Formula ───────────────────────────────────────────
    m_text = "(cos θ + i sin θ)^n = cos(nθ) + i sin(nθ)"
    m_block = ContentBlock(
        block_id="block-m1",
        block_type="math_formula",
        raw_content=m_text,
        content_hash=compute_canonical_hash(m_text),
        source_position={"page": 3, "paragraph_index": 0},
    )

    page3 = PageData(
        page_number=3,
        width=595.28,
        height=841.89,
        header_text="الرياضيات — السادس العلمي",
        footer_text="الفصل الأول",
        content_blocks=[m_block],
        tables=[],
    )

    return BookletDocument(
        doc_id="smoke_test_booklet",
        file_path="C:/smoke_test/sample_math_booklet.docx",
        file_type="docx",
        pages=[page1, page2, page3],
    )


def run_smoke_test(output_dir: Path, build_only: bool = False) -> int:
    """Execute the smoke test workflow."""
    print("=" * 70)
    print(" Booklet_AI — STEP-7.1 Manual Smoke Test")
    print("=" * 70)

    output_dir.mkdir(parents=True, exist_ok=True)
    doc = create_sample_3page_document()
    print(f"[✓] Created sample BookletDocument with {len(doc.pages)} pages:")
    for p in doc.pages:
        b_types = [b.block_type for b in p.content_blocks]
        t_count = len(p.tables)
        print(f"    - Page {p.page_number}: blocks={b_types}, tables={t_count}")

    # Geometry & Typography
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

    print(f"\n[+] Building JSON payload in: {output_dir}")
    try:
        payload_path = build_payload(
            document=doc,
            geometry=geometry,
            styles_cfg=styles_cfg,
            output_dir=output_dir,
            binding="saddle_stitch",
        )
        print(f"[✓] Payload JSON successfully written: {payload_path}")
        print(f"    Size: {payload_path.stat().st_size} bytes")
    except Exception as e:
        print(f"[✗] Failed to build payload: {e}")
        return 1

    if build_only:
        print("\n[✓] --build-only specified. Skipping InDesign COM execution.")
        print("=" * 70)
        return 0

    print("\n[+] Submitting generation job to COMSupervisor...")
    supervisor = COMSupervisor(heartbeat_timeout_seconds=60.0)

    try:
        result = generate_booklet(
            document=doc,
            geometry=geometry,
            styles_cfg=styles_cfg,
            output_dir=output_dir,
            supervisor=supervisor,
            binding="saddle_stitch",
        )
        print("[✓] InDesign generation completed successfully!")
        if isinstance(result, dict):
            print(f"    - Actual Content Pages: {result.get('actual_page_count')}")
            print(f"    - Imposed Target Pages: {result.get('imposed_page_count')}")
            print(f"    - Final Document Pages: {result.get('page_count')}")
            print(f"    - World-Ready Composer: {'✓ OK' if result.get('composer_ok') else '✗ FAIL'}")
            print(f"    - Output INDD: {result.get('output_indd')}")
            print(f"    - Output PDF:  {result.get('output_pdf')}")
            pages_diag = result.get("pages", [])
            if pages_diag:
                print("\n    [+] Page Layout Diagnostics (STEP-7.1e):")
                for p_info in pages_diag:
                    p_num = p_info.get("page_number")
                    p_side = p_info.get("side")
                    p_bounds = p_info.get("geometric_bounds") or p_info.get("geometricBounds")
                    t_width = p_info.get("table_width")
                    t_str = f", table_width={t_width:.2f}pt" if t_width is not None else ""
                    print(f"      * Page {p_num} (side={p_side}): bounds={p_bounds}{t_str}")
            warnings = result.get("warnings", [])
            if warnings:
                print(f"    - Warnings ({len(warnings)}):")
                for w in warnings:
                    print(f"      * {w}")
        print("=" * 70)
        return 0

    except InDesignUnavailableError as e:
        print(f"\n[!] InDesign is not available on this machine:\n    {e}")
        print("    Payload was generated and verified. Run on a machine with Adobe InDesign.")
        print("=" * 70)
        return 0
    except BookletError as e:
        print(f"\n[✗] Generation failed with BookletError:\n    {type(e).__name__}: {e}")
        print("=" * 70)
        return 1
    except Exception as e:
        print(f"\n[✗] Unexpected error during InDesign generation:\n    {type(e).__name__}: {e}")
        print("=" * 70)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run STEP-7.1 manual InDesign smoke test."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "build" / "smoke_test_output",
        help="Directory to write output files (default: build/smoke_test_output)",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build and verify payload JSON without launching InDesign",
    )
    args = parser.parse_args()
    code = run_smoke_test(output_dir=args.output_dir, build_only=args.build_only)
    sys.exit(code)


if __name__ == "__main__":
    main()
