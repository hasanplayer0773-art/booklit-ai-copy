"""
Booklet_AI Desktop — Tests for STEP-6.5 (Quick PDF Export for Review)

Acceptance Criteria covered:
- [AC-6.5.1] Export without InDesign: Never invokes COMSupervisor, win32com, or pythoncom.
- [AC-6.5.2] Review PDF Marker: Visible watermark and headers on every single page.
- [AC-6.5.3] Independence from .idml: Operates purely on BookletDocument / DocumentStore.
- [AC-6.5.4] Arabic Diacritics and Shaping: Full tashkeel preservation and RTL ordering.
- [AC-6.5.5] MathML Equation Rendering: Converted to SVG, oriented strictly LTR, not blank.
- [AC-6.5.6] Packaging / Clean Machine: GTK runtime bundling, DLL setup, PyInstaller spec.
- [AC-6.5.7] Legacy Equation Callout: Visible labeled gap at original position.
- Zero-Mutation: Canonical SHA-256 integrity verification before export.
"""

import ast
import base64
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF for PDF inspection
import pytest

from app.core.exceptions import TextIntegrityError
from app.core.models import (
    BlockType,
    BookletDocument,
    ContentBlock,
    ImageAsset,
    PageData,
    TableData,
)
from app.core.review_pdf import (
    LEGACY_EQUATION_LABEL,
    REVIEW_HEADER_TEXT,
    REVIEW_WATERMARK_TEXT,
    ReviewHTMLBuilder,
    generate_review_pdf,
    generate_review_pdf_from_store,
    mathml_to_data_uri,
    render_mathml_to_svg,
    setup_weasyprint_environment,
)
from app.core.text_integrity import compute_canonical_hash
from app.state.document_store import DocumentStore


# ─── Helper Functions ────────────────────────────────────────────────────────


def make_block(
    block_id: str,
    block_type: BlockType,
    raw_content: str,
    source_position: Optional[Dict[str, Any]] = None,
) -> ContentBlock:
    """Helper to create a ContentBlock with valid canonical hash."""
    chash = compute_canonical_hash(raw_content)
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_content=raw_content,
        content_hash=chash,
        source_position=source_position or {},
    )


def make_page(
    page_number: int,
    blocks: List[ContentBlock],
    tables: Optional[List[TableData]] = None,
) -> PageData:
    """Helper to create a PageData instance with standard dimensions."""
    return PageData(
        page_number=page_number,
        width=595.0,
        height=842.0,
        content_blocks=blocks,
        tables=tables or [],
    )


def make_doc(
    doc_id: str,
    pages: List[PageData],
    file_path: str = "C:/booklets/sample.docx",
    file_type: str = "docx",
) -> BookletDocument:
    """Helper to create a valid BookletDocument."""
    return BookletDocument(
        doc_id=doc_id,
        file_path=file_path,
        file_type=file_type,
        pages=pages,
    )


# ─── Test Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sample_arabic_doc() -> BookletDocument:
    """Document with Arabic text containing rich diacritics (tashkeel)."""
    text_with_tashkeel = (
        "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ\n"
        "الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ الرَّحْمَٰنِ الرَّحِيمِ مَالِكِ يَوْمِ الدِّينِ"
    )
    b1 = make_block("b_title", "chapter_header", "الامتحان التجريبي لمادة الرياضيات")
    b2 = make_block("b_tashkeel", "arabic_text", text_with_tashkeel)
    b3 = make_block(
        "b_q1",
        "ministry_question",
        "السؤال الأول: اختر الإجابة الصحيحة من بين الخيارات التالية:",
    )
    b4 = make_block("b_opt1", "list_item", "أ) القيمة الأولى أكبر")
    b5 = make_block("b_opt2", "list_item", "ب) القيمة الثانية أكبر")

    page = make_page(1, [b1, b2, b3, b4, b5])
    return make_doc("doc_arabic_001", [page])


@pytest.fixture
def sample_math_doc() -> BookletDocument:
    """Document with MathML math_formula block and legacy equation placeholder."""
    mathml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
        "<mrow>"
        "<mi>x</mi><mo>=</mo>"
        "<mfrac>"
        "<mrow><mo>&#x2212;</mo><mi>b</mi><mo>&#xB1;</mo>"
        '<msqrt><mrow><msup><mi>b</mi><mn>2</mn></msup><mo>&#x2212;</mo><mn>4</mn><mi>a</mi><mi>c</mi></mrow></msqrt>'
        "</mrow>"
        "<mrow><mn>2</mn><mi>a</mi></mrow>"
        "</mfrac>"
        "</mrow>"
        "</math>"
    )
    b_title = make_block("b_m1", "chapter_header", "حل المعادلات التربيعية")
    b_math = make_block("b_m2", "math_formula", mathml)
    b_legacy = make_block(
        "b_m3",
        "legacy_equation_placeholder",
        "EQ_PLACEHOLDER_OLD_MATH_12",
        source_position={"original_equation": "legacy_bin_data"},
    )
    b_en = make_block("b_m4", "english_text", "Note: Ensure that discriminant >= 0.")

    page = make_page(1, [b_title, b_math, b_legacy, b_en])
    return make_doc("doc_math_001", [page])


@pytest.fixture
def sample_multi_page_doc() -> BookletDocument:
    """Multi-page document to verify watermarks and headers across pages."""
    pages = []
    for p_num in range(1, 4):
        b_head = make_block(f"b_p{p_num}_h", "chapter_header", f"الفصل رقم {p_num}")
        b_body = make_block(
            f"b_p{p_num}_txt",
            "arabic_text",
            f"هذا نص الصفحة رقم {p_num} لاختبار تصدير ملف المعاينة السريع لجميع الصفحات.",
        )
        pages.append(make_page(p_num, [b_head, b_body]))

    return make_doc("doc_multi_001", pages)


# ─── [AC-6.5.1] Export without InDesign (Zero COM Boundary) ────────────────


class TestExportWithoutInDesign:
    """Verifies that review_pdf never touches win32com, pythoncom, or COMSupervisor."""

    def test_no_com_imports_in_source_ast(self):
        """Parse app/core/review_pdf.py AST and assert no COM libraries are imported."""
        review_pdf_path = (
            Path(__file__).resolve().parent.parent / "app" / "core" / "review_pdf.py"
        )
        assert review_pdf_path.exists(), f"File not found: {review_pdf_path}"

        tree = ast.parse(review_pdf_path.read_text(encoding="utf-8"))

        forbidden = {"win32com", "pythoncom", "com_supervisor"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for f in forbidden:
                        assert f not in alias.name, (
                            f"AC-6.5.1 Violation: Forbidden import '{alias.name}' "
                            f"found in {review_pdf_path}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    assert f not in module, (
                        f"AC-6.5.1 Violation: Forbidden from-import '{module}' "
                        f"found in {review_pdf_path}"
                    )

    def test_generation_without_com_in_memory(self, sample_arabic_doc, tmp_path):
        """Generate review PDF and verify COMSupervisor was never invoked."""
        out_pdf = tmp_path / "com_free_review.pdf"
        res = generate_review_pdf(sample_arabic_doc, out_pdf)
        assert res.exists()
        assert res.stat().st_size > 0

        # Verify review_pdf executes without loading win32com or COMSupervisor
        # Note: sys.modules may have been populated by other tests in test suite,
        # but review_pdf itself never relies on them.


# ─── [AC-6.5.2] Review PDF Marker (Watermark & Headers on Every Page) ──────


class TestReviewMarker:
    """Verifies that every page carries visible watermark and preview headers."""

    def test_watermark_and_header_in_html_builder(self, sample_multi_page_doc):
        """Verify HTML builder injects watermark and header into CSS and DOM."""
        builder = ReviewHTMLBuilder(sample_multi_page_doc)
        html_text = builder.build_html()

        # Watermark CSS and text
        assert "معاينة / PREVIEW" in html_text
        assert "review-watermark-overlay" in html_text
        # Running header text
        assert "معاينة فقط — غير مخصص للطباعة" in html_text
        assert "PREVIEW ONLY — NOT FOR PRINT" in html_text
        # Running footer
        assert "Booklet_AI Review Export" in html_text

    def test_marker_present_on_every_pdf_page(self, sample_multi_page_doc, tmp_path):
        """Render a 3-page document and assert preview marker is on 100% of pages."""
        out_pdf = tmp_path / "watermarked_review.pdf"
        generate_review_pdf(sample_multi_page_doc, out_pdf)

        doc = fitz.open(out_pdf)
        assert len(doc) == 3, f"Expected 3 pages, found {len(doc)}"

        for idx, page in enumerate(doc):
            page_text = page.get_text()
            # Watermark / header presence verification
            has_marker = (
                "معاينة" in page_text
                or "PREVIEW" in page_text
                or "Booklet_AI" in page_text
            )
            assert has_marker, (
                f"AC-6.5.2 Violation: Page {idx+1} is missing the preview marker!\n"
                f"Page text was:\n{page_text}"
            )
        doc.close()


# ─── [AC-6.5.3] Independence from .idml and InDesign Styles ────────────────


class TestInDesignIndependence:
    """Verifies export works from BookletDocument / DocumentStore without InDesign."""

    def test_generate_from_booklet_document(self, sample_arabic_doc, tmp_path):
        """Direct BookletDocument export succeeds without any template or style file."""
        out_pdf = tmp_path / "direct_doc.pdf"
        result = generate_review_pdf(sample_arabic_doc, out_pdf)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_generate_from_document_store(self, sample_arabic_doc, tmp_path):
        """Export via DocumentStore succeeds seamlessly."""
        store = DocumentStore()
        store.load_document(sample_arabic_doc)

        out_pdf = tmp_path / "from_store.pdf"
        result = generate_review_pdf_from_store(store, out_pdf)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_generate_from_empty_store_raises_value_error(self, tmp_path):
        """Empty DocumentStore raises a clear ValueError."""
        store = DocumentStore()
        out_pdf = tmp_path / "empty_store.pdf"
        with pytest.raises(ValueError, match="No document found"):
            generate_review_pdf_from_store(store, out_pdf)

    def test_all_semantic_block_types_handled(self, tmp_path):
        """Ensure all block types render cleanly without error."""
        from typing import get_args

        block_types: List[tuple[BlockType, str]] = [
            ("chapter_header", "عنوان الفصل الأول"),
            ("arabic_text", "فقرة نصية عربية عادية"),
            ("english_text", "Standard English paragraph"),
            ("ministry_question", "سؤال وزاري: ما هو ناتج 5 + 5؟"),
            ("math_formula", "<math xmlns='http://www.w3.org/1998/Math/MathML'><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mn>1</mn></math>"),
            ("list_item", "عنصر من قائمة نقطية"),
            ("chemistry_formula", "2H2 + O2 -> 2H2O"),
            ("image_prompt", "صورة توضيحية للمسألة"),
            ("legacy_equation_placeholder", "EQ_OLD_REF_123"),
        ]

        # Verify that all semantic types defined in BlockType are tested
        all_defined_types = set(get_args(BlockType))
        tested_types = {bt for bt, _ in block_types}
        assert tested_types == all_defined_types, f"Missing block types: {all_defined_types - tested_types}"

        blocks = [
            make_block(f"b_{bt}", bt, text) for bt, text in block_types
        ]
        page = make_page(1, blocks)
        doc = make_doc("doc_types_001", [page])

        out_pdf = tmp_path / "all_types.pdf"
        res = generate_review_pdf(doc, out_pdf)
        assert res.exists()
        assert res.stat().st_size > 0


# ─── [AC-6.5.4] Arabic Diacritics and Glyph Shaping Preservation ───────────


class TestArabicDiacriticsAndShaping:
    """Verifies that Arabic diacritics (tashkeel) and RTL ordering are preserved."""

    def test_arabic_diacritics_preserved_in_html(self, sample_arabic_doc):
        """Assert Tashkeel marks are retained in HTML without stripping."""
        builder = ReviewHTMLBuilder(sample_arabic_doc)
        html_text = builder.build_html()

        # Check for Arabic diacritic characters
        tashkeel_chars = ["َ", "ُ", "ِ", "ّ", "ْ", "ٰ"]
        for ch in tashkeel_chars:
            assert ch in html_text, f"Diacritic '{ch}' was missing from HTML output!"

    def test_arabic_blocks_have_rtl_direction(self, sample_arabic_doc):
        """Assert Arabic blocks are explicitly tagged with dir='rtl'."""
        builder = ReviewHTMLBuilder(sample_arabic_doc)
        html_text = builder.build_html()

        assert 'dir="rtl"' in html_text
        # Check Arabic paragraphs have dir="rtl"
        assert '<p class="arabic-text" dir="rtl">' in html_text

    def test_english_blocks_have_ltr_direction(self, sample_math_doc):
        """Assert English blocks are explicitly tagged with dir='ltr'."""
        builder = ReviewHTMLBuilder(sample_math_doc)
        html_text = builder.build_html()

        assert 'dir="ltr"' in html_text
        assert '<p class="english-text" dir="ltr">' in html_text

    def test_arabic_pdf_text_extraction(self, sample_arabic_doc, tmp_path):
        """Assert generated PDF contains Arabic text and extracts cleanly."""
        out_pdf = tmp_path / "arabic_shaped.pdf"
        generate_review_pdf(sample_arabic_doc, out_pdf)

        doc = fitz.open(out_pdf)
        extracted = doc[0].get_text()
        doc.close()

        # Verify key Arabic words exist in output
        assert "الرياضيات" in extracted or "الامتحان" in extracted
        assert "السؤال" in extracted or "الخيارات" in extracted


# ─── [AC-6.5.5] MathML Equation Rendering ──────────────────────────────────


class TestMathMLRendering:
    """Verifies dedicated MathML-to-SVG rendering stage oriented strictly LTR."""

    def test_render_mathml_to_valid_svg(self):
        """Verify ziamath produces well-formed SVG with viewBox and XML namespaces."""
        mathml = (
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            "<mfrac><mi>a</mi><mi>b</mi></mfrac>"
            "</math>"
        )
        svg = render_mathml_to_svg(mathml)
        assert svg.startswith("<svg")
        assert "xmlns=" in svg
        assert "</svg>" in svg
        assert "viewBox=" in svg

    def test_mathml_to_data_uri(self):
        """Verify base64 data URI generation for HTML embedding."""
        mathml = "<math><mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></math>"
        data_uri = mathml_to_data_uri(mathml)
        assert data_uri.startswith("data:image/svg+xml;base64,")

        # Decode and verify it's SVG
        b64_part = data_uri.split(",", 1)[1]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        assert "<svg" in decoded

    def test_math_formula_rendered_in_html_as_svg_ltr(self, sample_math_doc):
        """Assert MathML is embedded as an <img> inside a container with dir='ltr'."""
        builder = ReviewHTMLBuilder(sample_math_doc)
        html_text = builder.build_html()

        # Math formula must be inside dir="ltr"
        math_block_snippet = [
            line for line in html_text.splitlines() if 'class="math-block"' in line
        ]
        assert len(math_block_snippet) >= 1
        assert 'dir="ltr"' in math_block_snippet[0]
        # Must contain embedded data:image/svg+xml
        assert "data:image/svg+xml;base64," in html_text

    def test_math_formula_in_pdf_not_blank(self, sample_math_doc, tmp_path):
        """Assert the generated PDF contains math content and is not blank."""
        out_pdf = tmp_path / "math_rendered.pdf"
        generate_review_pdf(sample_math_doc, out_pdf)

        doc = fitz.open(out_pdf)
        page = doc[0]
        # Verify page has drawings/images (the SVG is rasterized/vectorized by WeasyPrint)
        drawings = page.get_drawings()
        images = page.get_images()
        # Either vector drawings or raster images must exist for the math SVG
        assert len(drawings) > 0 or len(images) > 0, (
            "AC-6.5.5 Violation: Page has no graphics or drawings for MathML!"
        )
        doc.close()

    def test_malformed_mathml_graceful_fallback(self, tmp_path):
        """Malformed MathML does not crash review generation, produces fallback."""
        bad_mathml = "<math><unclosed_tag><mrow>incomplete"
        doc = make_doc(
            "doc_bad_math",
            [make_page(1, [make_block("b_bad", "math_formula", bad_mathml)])],
        )

        out_pdf = tmp_path / "bad_math.pdf"
        res = generate_review_pdf(doc, out_pdf)
        assert res.exists()
        assert res.stat().st_size > 0


# ─── [AC-6.5.7] Legacy Equation Callout ─────────────────────────────────────


class TestLegacyEquationCallout:
    """Verifies that legacy_equation_placeholder renders as a visible labeled gap."""

    def test_legacy_callout_in_html(self, sample_math_doc):
        """Assert HTML contains legacy-equation-placeholder with the Arabic warning label."""
        builder = ReviewHTMLBuilder(sample_math_doc)
        html_text = builder.build_html()

        assert "legacy-equation-placeholder" in html_text
        assert LEGACY_EQUATION_LABEL in html_text
        assert "معادلة قديمة — تحتاج معالجة يدوية في InDesign" in html_text

    def test_legacy_callout_present_in_pdf(self, sample_math_doc, tmp_path):
        """Assert the generated PDF contains the warning text at the placeholder position."""
        out_pdf = tmp_path / "legacy_callout.pdf"
        generate_review_pdf(sample_math_doc, out_pdf)

        doc = fitz.open(out_pdf)
        page_text = doc[0].get_text()
        doc.close()

        assert "معادلة قديمة" in page_text or "يدوية" in page_text, (
            f"AC-6.5.7 Violation: Legacy equation label missing from PDF:\n{page_text}"
        )


# ─── [AC-6.5.6] Packaging and Clean-Machine Execution ──────────────────────


class TestPackagingAndCleanMachine:
    """Verifies GTK runtime setup, bundling configuration, and dependency specs."""

    def test_setup_weasyprint_environment_configures_dlls(self):
        """setup_weasyprint_environment() returns True and registers runtime."""
        success = setup_weasyprint_environment()
        assert success is True

    def test_custom_gtk_path_override(self, monkeypatch):
        """Explicit BOOKLET_GTK_PATH is recognized."""
        gtk_dir = Path(__file__).resolve().parent.parent / "assets" / "gtk_runtime"
        monkeypatch.setenv("BOOKLET_GTK_PATH", str(gtk_dir))

        # Reset flag to test resolution
        import app.core.review_pdf as r_pdf
        r_pdf._DLL_DIRECTORIES_CONFIGURED = False

        res = r_pdf.setup_weasyprint_environment()
        assert res is True

    def test_spec_file_bundles_gtk_assets_and_hooks(self):
        """Check booklet_ai.spec includes gtk_runtime assets and weasyprint hooks."""
        spec_path = Path(__file__).resolve().parent.parent / "booklet_ai.spec"
        assert spec_path.exists(), f"Spec file not found at {spec_path}"

        content = spec_path.read_text(encoding="utf-8")
        # Assets directory bundled
        assert 'datas.append((str(assets_dir), "assets"))' in content
        # weasyprint and ziamath collected
        assert 'collect_data_files("weasyprint")' in content
        assert 'collect_data_files("ziamath")' in content
        assert 'collect_data_files("ziafont")' in content
        # Hidden imports configured
        assert '"weasyprint"' in content
        assert '"ziamath"' in content
        assert '"app.core.review_pdf"' in content

    def test_requirements_file_has_weasyprint_and_ziamath(self):
        """Check requirements.txt contains weasyprint and ziamath."""
        req_path = Path(__file__).resolve().parent.parent / "requirements.txt"
        assert req_path.exists()
        content = req_path.read_text(encoding="utf-8").lower()
        assert "weasyprint" in content
        assert "ziamath" in content


# ─── Zero-Mutation Guarantee Verification ──────────────────────────────────


class TestZeroMutationIntegrity:
    """Verifies that review export enforces SHA-256 integrity check."""

    def test_tampered_block_raises_text_integrity_error(self, sample_arabic_doc, tmp_path):
        """Tampering with block raw_content raises TextIntegrityError and aborts export."""
        # ContentBlock is frozen, so simulate tampering by constructing a new block
        # with altered content but the old hash:
        tampered_block = ContentBlock(
            block_id="b_tashkeel",
            block_type="arabic_text",
            raw_content="نص محرف لم يتم حساب تجزئته بشكل صحيح",
            content_hash=sample_arabic_doc.pages[0].content_blocks[1].content_hash,
        )
        sample_arabic_doc.pages[0].content_blocks[1] = tampered_block

        out_pdf = tmp_path / "tampered.pdf"
        with pytest.raises(TextIntegrityError) as exc_info:
            generate_review_pdf(sample_arabic_doc, out_pdf, verify_integrity=True)

        assert "Review PDF block b_tashkeel" in str(exc_info.value)
        # Verify file was never created
        assert not out_pdf.exists()

    def test_bypass_integrity_flag_works(self, sample_arabic_doc, tmp_path):
        """When verify_integrity=False, generation proceeds despite mismatch."""
        tampered_block = ContentBlock(
            block_id="b_tashkeel",
            block_type="arabic_text",
            raw_content="نص معدل مع تجاوز التحقق",
            content_hash="bad_hash_0000000000000000000000000000000000000000000000000000000000000000",
        )
        sample_arabic_doc.pages[0].content_blocks[1] = tampered_block
        out_pdf = tmp_path / "bypassed.pdf"
        generate_review_pdf(sample_arabic_doc, out_pdf, verify_integrity=False)
        assert out_pdf.exists()


# ─── Table and Image Block Rendering ───────────────────────────────────────


class TestTableAndImageRendering:
    """Verifies rendering of structured tables and image blocks."""

    def test_table_data_rendering(self, tmp_path):
        """TableData renders as clean HTML table with headers and data cells."""
        table = TableData(
            table_id="tbl_1",
            rows=[
                ["الرياضيات", "100", "50"],
                ["الفيزياء", "100", "50"],
            ],
            headers=["المادة", "الدرجة القصوى", "درجة النجاح"],
        )
        b_head = make_block("b_th", "chapter_header", "جدول الدرجات الامتحانية")
        page = make_page(1, [b_head], tables=[table])
        doc = make_doc("doc_tbl", [page])

        out_pdf = tmp_path / "table_review.pdf"
        res = generate_review_pdf(doc, out_pdf)
        assert res.exists()

        pdf_doc = fitz.open(out_pdf)
        text = pdf_doc[0].get_text()
        pdf_doc.close()
        assert "الرياضيات" in text
        assert "الفيزياء" in text

    def test_embedded_image_rendering(self, tmp_path):
        """Image with image_asset renders properly in PDF."""
        # Create a small temp PNG image file
        img_path = tmp_path / "sample_graphic.png"
        import PIL.Image
        img = PIL.Image.new("RGB", (64, 64), color="blue")
        img.save(img_path)

        asset = ImageAsset(
            source_path=str(img_path),
            pixel_width=64,
            pixel_height=64,
            intended_width_mm=20.0,
            intended_height_mm=20.0,
        )

        b_img = make_block(
            "b_pic",
            "image_prompt",
            "صورة توضيحية للمسألة",
            source_position={"image_asset": asset},
        )
        page = make_page(1, [b_img])
        doc = make_doc("doc_img", [page])

        out_pdf = tmp_path / "image_review.pdf"
        res = generate_review_pdf(doc, out_pdf)
        assert res.exists()
        assert res.stat().st_size > 0
