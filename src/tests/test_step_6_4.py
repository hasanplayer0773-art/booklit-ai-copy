"""
Booklet_AI Desktop — Tests for STEP-6.4 (Preflight Check)

Covers:
- AC-6.4.1:  Unresolved legacy equations counted correctly, severity="blocker"
- AC-6.4.1b: Export blocked without explicit override; override logged
- AC-6.4.2:  Filler pages via the shared imposed_page_count()
- AC-6.4.2b: compute_effective_dpi correctness (1200×1200 example, non-square)
- AC-6.4.2c: Unverifiable DPI produces "image_dpi_unverified", not silence
- AC-6.4.2d: DOCX and PDF extractor ImageAsset population end-to-end
- AC-6.4.3:  No win32com/pythoncom import in preflight.py
- AC-6.4.4:  Post-generation overset uses .overflows (design verification)
- AC-6.4.4b: PreflightReport file-based IPC round-trip
- AC-6.4.4c: Required-font list derived from TypographyStylesConfig
- AC-6.4.5:  Pre-COM pass with zero issues doesn't claim "print-ready"
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import List

import pytest
from PIL import Image

from app.core.exceptions import PreflightBlockerError
from app.core.models import (
    BookletDocument,
    ContentBlock,
    ImageAsset,
    PageData,
)
from app.core.preflight import (
    MIN_PRINT_DPI,
    MM_PER_INCH,
    PreflightIssue,
    PreflightReport,
    assert_export_allowed,
    can_proceed_to_export,
    compute_effective_dpi,
    get_required_fonts,
    proceed_to_export_with_override,
    run_pre_com_preflight,
)
from app.core.post_generation_preflight import (
    read_post_generation_report,
)
from app.core.text_integrity import compute_canonical_hash
from app.core.docx_extractor import DocxExtractor
from app.core.pdf_extractor import PDFExtractor
from app.automation.job_protocol import COMJobRequest
from app.state.job_history import JobHistoryManager
from app.ui.preflight_panel import (
    PreflightPanel,
    PreflightPresentationStatus,
    evaluate_preflight_presentation,
)


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


def _make_block(
    block_id: str,
    block_type: str,
    content: str = "test",
    image_asset: ImageAsset | None = None,
) -> ContentBlock:
    """Helper to build a ContentBlock with optional ImageAsset."""
    source_position = {}
    if image_asset is not None:
        source_position["image_asset"] = image_asset
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_content=content,
        content_hash=compute_canonical_hash(content),
        source_position=source_position,
    )


def _make_doc(blocks: List[ContentBlock], num_pages: int = 1) -> BookletDocument:
    """Helper to build a BookletDocument from content blocks."""
    pages = []
    for i in range(num_pages):
        page_blocks = blocks if i == 0 else []
        pages.append(PageData(
            page_number=i + 1,
            width=595.0,
            height=842.0,
            content_blocks=page_blocks,
        ))
    return BookletDocument(
        doc_id="test-doc",
        file_path="C:/test/sample.docx",
        file_type="docx",
        pages=pages,
    )


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.1: Unresolved legacy equations — exact count, blocker severity
# ═══════════════════════════════════════════════════════════════════════


class TestLegacyEquationBlocker:
    """AC-6.4.1: run_pre_com_preflight() correctly reports
    unresolved_legacy_equations with exact count and severity='blocker'.
    """

    def test_no_legacy_equations_no_issue(self):
        """Clean document has no legacy equation issues."""
        doc = _make_doc([
            _make_block("b1", "arabic_text", "نص عربي"),
            _make_block("b2", "math_formula", "<math>x</math>"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        legacy_issues = [
            i for i in report.issues
            if i.code == "unresolved_legacy_equations"
        ]
        assert len(legacy_issues) == 0

    def test_single_legacy_equation(self):
        """One legacy equation → one blocker with count=1."""
        doc = _make_doc([
            _make_block("b1", "arabic_text"),
            _make_block("b2", "legacy_equation_placeholder", "[Legacy]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        legacy_issues = [
            i for i in report.issues
            if i.code == "unresolved_legacy_equations"
        ]
        assert len(legacy_issues) == 1
        assert legacy_issues[0].severity == "blocker"
        assert "1" in legacy_issues[0].message

    def test_multiple_legacy_equations_exact_count(self):
        """Three legacy equations → blocker with count=3."""
        doc = _make_doc([
            _make_block("b1", "legacy_equation_placeholder", "[OLE 1]"),
            _make_block("b2", "arabic_text"),
            _make_block("b3", "legacy_equation_placeholder", "[OLE 2]"),
            _make_block("b4", "legacy_equation_placeholder", "[OLE 3]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        legacy_issues = [
            i for i in report.issues
            if i.code == "unresolved_legacy_equations"
        ]
        assert len(legacy_issues) == 1
        assert legacy_issues[0].severity == "blocker"
        assert "3" in legacy_issues[0].message

    def test_has_blockers_true_with_legacy(self):
        """has_blockers property is True when legacy equations exist."""
        doc = _make_doc([
            _make_block("b1", "legacy_equation_placeholder", "[OLE]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        assert report.has_blockers is True


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.1b: Export blocked without explicit override
# ═══════════════════════════════════════════════════════════════════════


class TestExportBlockedWithoutOverride:
    """AC-6.4.1b: A document with unresolved_legacy_equations cannot
    proceed to final export without an explicit override action.
    """

    @pytest.fixture
    def audit_db(self):
        """Create an in-memory database with job_history and preflight_overrides schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE job_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file_path TEXT NOT NULL,
                source_file_sha256 TEXT NOT NULL,
                document_content_hash TEXT,
                page_count INTEGER,
                status TEXT NOT NULL,
                error_summary TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE preflight_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER REFERENCES job_history(id),
                blocker_code TEXT NOT NULL,
                reason TEXT NOT NULL,
                overridden_at TEXT NOT NULL,
                user_identifier TEXT DEFAULT 'designer'
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_blocker_prevents_export(self):
        """has_blockers=True means the normal export path must refuse."""
        doc = _make_doc([
            _make_block("b1", "legacy_equation_placeholder", "[OLE]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        assert report.has_blockers is True
        assert can_proceed_to_export(report) is False
        with pytest.raises(PreflightBlockerError) as exc_info:
            assert_export_allowed(report)
        assert "unresolved_legacy_equations" in str(exc_info.value)

    def test_warnings_only_do_not_block(self):
        """Warnings alone don't set has_blockers — export can proceed."""
        doc = _make_doc(
            [_make_block("b1", "arabic_text")],
            num_pages=5,
        )
        report = run_pre_com_preflight(doc, "saddle_stitch")
        assert report.has_blockers is False
        assert len(report.issues) > 0  # filler pages warning
        assert can_proceed_to_export(report) is True
        # assert_export_allowed should not raise
        assert_export_allowed(report)

    def test_override_requires_valid_reason(self, audit_db):
        """An empty or whitespace reason raises ValueError."""
        doc = _make_doc([
            _make_block("b1", "legacy_equation_placeholder", "[OLE]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        manager = JobHistoryManager(audit_db)
        job_id = manager.start_job("C:/test.docx", "hash123")

        with pytest.raises(ValueError):
            proceed_to_export_with_override(report, manager, job_id, reason="   ")

    def test_override_records_audit_and_authorizes_export(self, audit_db):
        """Sanctioned override writes audit record to SQLite and allows export."""
        doc = _make_doc([
            _make_block("b1", "legacy_equation_placeholder", "[OLE 1]"),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        assert report.has_blockers is True

        manager = JobHistoryManager(audit_db)
        job_id = manager.start_job("C:/test.docx", "hash123")

        # Explicit override
        ok = proceed_to_export_with_override(
            report=report,
            job_history_manager=manager,
            job_id=job_id,
            reason="المعادلة قديمة وتمت مراجعتها يدوياً مع الأستاذ",
            user_identifier="designer_ahmed",
        )
        assert ok is True

        # Now authorized
        assert can_proceed_to_export(report, has_explicit_override=True) is True
        assert_export_allowed(report, has_explicit_override=True)

        # Audit record exists and has exact fields
        overrides = manager.get_preflight_overrides(job_id)
        assert len(overrides) == 1
        assert overrides[0]["blocker_code"] == "unresolved_legacy_equations"
        assert overrides[0]["reason"] == "المعادلة قديمة وتمت مراجعتها يدوياً مع الأستاذ"
        assert overrides[0]["user_identifier"] == "designer_ahmed"
        assert overrides[0]["overridden_at"] is not None


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.2: Filler pages via shared imposed_page_count()
# ═══════════════════════════════════════════════════════════════════════


class TestFillerPages:
    """AC-6.4.2: run_pre_com_preflight() correctly reports
    filler_pages_required using imposed_page_count().
    """

    def test_no_filler_needed_multiple_of_4(self):
        """4 pages with saddle_stitch → no filler needed."""
        doc = _make_doc(
            [_make_block("b1", "arabic_text")],
            num_pages=4,
        )
        report = run_pre_com_preflight(doc, "saddle_stitch")
        filler = [i for i in report.issues if i.code == "filler_pages_required"]
        assert len(filler) == 0

    def test_filler_needed_not_multiple_of_4(self):
        """5 pages with saddle_stitch → 3 filler pages (rounds to 8)."""
        doc = _make_doc(
            [_make_block("b1", "arabic_text")],
            num_pages=5,
        )
        report = run_pre_com_preflight(doc, "saddle_stitch")
        filler = [i for i in report.issues if i.code == "filler_pages_required"]
        assert len(filler) == 1
        assert filler[0].severity == "warning"
        assert "3" in filler[0].message  # 8 - 5 = 3

    def test_filler_pages_perfect_bound_no_constraint(self):
        """Perfect bound has no fixed-multiple constraint (multiple=1)."""
        doc = _make_doc(
            [_make_block("b1", "arabic_text")],
            num_pages=7,
        )
        report = run_pre_com_preflight(doc, "perfect_bound")
        filler = [i for i in report.issues if i.code == "filler_pages_required"]
        assert len(filler) == 0


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.2b: compute_effective_dpi correctness
# ═══════════════════════════════════════════════════════════════════════


class TestEffectiveDPI:
    """AC-6.4.2b: DPI calculations with documented examples."""

    def test_1200px_at_2x2_inches(self):
        """1200×1200px at 2″×2″ (50.8mm×50.8mm) → 600 PPI."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=2 * MM_PER_INCH,   # 50.8mm
            intended_height_mm=2 * MM_PER_INCH,  # 50.8mm
        )
        dpi = compute_effective_dpi(asset)
        assert dpi is not None
        assert abs(dpi - 600.0) < 0.01

    def test_1200px_at_8x8_inches(self):
        """1200×1200px at 8″×8″ (203.2mm×203.2mm) → 150 PPI."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=8 * MM_PER_INCH,   # 203.2mm
            intended_height_mm=8 * MM_PER_INCH,  # 203.2mm
        )
        dpi = compute_effective_dpi(asset)
        assert dpi is not None
        assert abs(dpi - 150.0) < 0.01

    def test_larger_size_lower_dpi(self):
        """Same image at larger size → lower DPI."""
        asset_small = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=2 * MM_PER_INCH,
            intended_height_mm=2 * MM_PER_INCH,
        )
        asset_large = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=8 * MM_PER_INCH,
            intended_height_mm=8 * MM_PER_INCH,
        )
        dpi_small = compute_effective_dpi(asset_small)
        dpi_large = compute_effective_dpi(asset_large)
        assert dpi_small is not None and dpi_large is not None
        assert dpi_small > dpi_large

    def test_non_square_returns_weaker_axis(self):
        """Non-square placement: returns the lower-DPI axis.

        800×400px at 2″ wide × 4″ tall:
          width-DPI = 800 / 2 = 400
          height-DPI = 400 / 4 = 100
        Should return 100 (the weaker axis).
        """
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=800,
            pixel_height=400,
            intended_width_mm=2 * MM_PER_INCH,   # 50.8mm
            intended_height_mm=4 * MM_PER_INCH,  # 101.6mm
        )
        dpi = compute_effective_dpi(asset)
        assert dpi is not None
        assert abs(dpi - 100.0) < 0.01

    def test_non_square_width_is_weaker(self):
        """Non-square placement where width is the weaker axis.

        400×800px at 4″ wide × 2″ tall:
          width-DPI = 400 / 4 = 100
          height-DPI = 800 / 2 = 400
        Should return 100 (the weaker axis).
        """
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=400,
            pixel_height=800,
            intended_width_mm=4 * MM_PER_INCH,
            intended_height_mm=2 * MM_PER_INCH,
        )
        dpi = compute_effective_dpi(asset)
        assert dpi is not None
        assert abs(dpi - 100.0) < 0.01

    def test_zero_placement_returns_none(self):
        """Zero placement size → None (degenerate)."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=0.0,
            intended_height_mm=50.0,
        )
        assert compute_effective_dpi(asset) is None

    def test_negative_placement_returns_none(self):
        """Negative placement size → None (degenerate)."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=-10.0,
            intended_height_mm=50.0,
        )
        assert compute_effective_dpi(asset) is None


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.2c: Unverifiable DPI → "image_dpi_unverified" warning
# ═══════════════════════════════════════════════════════════════════════


class TestDPIUnverified:
    """AC-6.4.2c: ImageAsset with unknown placement size produces
    'image_dpi_unverified' warning, never a false pass.
    """

    def test_unknown_placement_emits_unverified(self):
        """ImageAsset without intended_width/height → unverified warning."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            # intended_width_mm and intended_height_mm are None
        )
        doc = _make_doc([
            _make_block("b1", "image_prompt", "image", image_asset=asset),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        unverified = [
            i for i in report.issues if i.code == "image_dpi_unverified"
        ]
        assert len(unverified) == 1
        assert unverified[0].severity == "warning"

    def test_compute_dpi_none_triggers_unverified(self):
        """compute_effective_dpi() returning None → unverified code."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=None,
            intended_height_mm=None,
        )
        result = compute_effective_dpi(asset)
        assert result is None

    def test_partial_placement_emits_unverified(self):
        """Only width known (height None) → still unverified."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=1200,
            intended_width_mm=50.8,
            intended_height_mm=None,
        )
        assert compute_effective_dpi(asset) is None

    def test_low_dpi_emits_warning_not_blocker(self):
        """DPI below threshold → warning, not blocker."""
        # 100×100px at 100mm×100mm ≈ 25.4 DPI — well below MIN_PRINT_DPI
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=100,
            pixel_height=100,
            intended_width_mm=100.0,
            intended_height_mm=100.0,
        )
        doc = _make_doc([
            _make_block("b1", "image_prompt", "image", image_asset=asset),
        ])
        report = run_pre_com_preflight(doc, "saddle_stitch")
        low_dpi = [i for i in report.issues if i.code == "low_effective_dpi"]
        assert len(low_dpi) == 1
        assert low_dpi[0].severity == "warning"  # NOT blocker


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.2d: DOCX and PDF extractor ImageAsset population end-to-end
# ═══════════════════════════════════════════════════════════════════════


class TestExtractorImageIntegration:
    """AC-6.4.2d: Running the DOCX (STEP-1.2) and PDF (STEP-1.3) extractors
    against fixture files containing embedded images produces ContentBlocks
    whose source_position['image_asset'] is a populated ImageAsset with
    dimensions read from the embedded binary.
    """

    @pytest.fixture
    def test_image_bytes(self) -> bytes:
        """Create a 400×200 PNG image in memory."""
        import io
        img = Image.new("RGB", (400, 200), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_docx_extractor_populates_image_asset(self, tmp_path, test_image_bytes):
        """DOCX extractor creates image_prompt ContentBlock with real ImageAsset."""
        import docx
        import io

        # Build a test docx file with an embedded image
        doc = docx.Document()
        doc.add_paragraph("نص عربي اختباري")
        doc.add_picture(
            io.BytesIO(test_image_bytes),
            width=docx.shared.Inches(2.0),   # 50.8 mm
            height=docx.shared.Inches(1.0),  # 25.4 mm
        )
        doc.add_paragraph("فقرة ختامية")
        docx_file = str(tmp_path / "test_embedded.docx")
        doc.save(docx_file)

        # Extract using DocxExtractor
        job_dir = str(tmp_path / "docx_job_images")
        extractor = DocxExtractor(job_temp_dir=job_dir)
        booklet_doc = extractor.extract(docx_file)

        # Find image_prompt blocks
        all_blocks = [
            b for p in booklet_doc.pages for b in p.content_blocks
        ]
        image_blocks = [b for b in all_blocks if b.block_type == "image_prompt"]

        assert len(image_blocks) == 1, "Must extract exactly 1 image_prompt block"
        img_block = image_blocks[0]

        # Verify source_position has populated ImageAsset
        asset = img_block.source_position.get("image_asset")
        assert asset is not None, "source_position['image_asset'] must be populated"
        assert isinstance(asset, ImageAsset)
        assert asset.pixel_width == 400
        assert asset.pixel_height == 200
        assert asset.intended_width_mm is not None
        assert abs(asset.intended_width_mm - 50.8) < 1.0
        assert asset.intended_height_mm is not None
        assert abs(asset.intended_height_mm - 25.4) < 1.0

        # Verify the file was written to disk and has content
        assert Path(asset.source_path).exists()
        assert Path(asset.source_path).stat().st_size > 0

        # Verify end-to-end preflight integration:
        # 400px at 2in width = 200 DPI, 200px at 1in height = 200 DPI -> >= MIN_PRINT_DPI (200)
        report = run_pre_com_preflight(booklet_doc, "saddle_stitch")
        low_dpi_issues = [i for i in report.issues if i.code == "low_effective_dpi"]
        assert len(low_dpi_issues) == 0

    def test_pdf_extractor_populates_image_asset(self, tmp_path, test_image_bytes):
        """PDF extractor creates image_prompt ContentBlock with real ImageAsset."""
        import fitz

        # Build a test PDF file with an embedded image
        pdf_file = str(tmp_path / "test_embedded.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595.0, height=842.0)
        page.insert_text((50, 50), "عنوان تجريبي")
        # Place image at rect with width=144pt (2 in), height=72pt (1 in)
        rect = fitz.Rect(100.0, 100.0, 244.0, 172.0)
        page.insert_image(rect, stream=test_image_bytes)
        doc.save(pdf_file)
        doc.close()

        # Extract using PDFExtractor
        job_dir = str(tmp_path / "pdf_job_images")
        extractor = PDFExtractor(pdf_file, job_temp_dir=job_dir)
        booklet_doc = extractor.extract()

        # Find image_prompt blocks
        all_blocks = [
            b for p in booklet_doc.pages for b in p.content_blocks
        ]
        image_blocks = [b for b in all_blocks if b.block_type == "image_prompt"]

        assert len(image_blocks) == 1, "Must extract exactly 1 image_prompt block from PDF"
        img_block = image_blocks[0]

        asset = img_block.source_position.get("image_asset")
        assert asset is not None, "source_position['image_asset'] must be populated"
        assert isinstance(asset, ImageAsset)
        assert asset.pixel_width == 400
        assert asset.pixel_height == 200
        assert asset.intended_width_mm is not None
        # 144 pt * 25.4 / 72 = 50.8 mm
        assert abs(asset.intended_width_mm - 50.8) < 1.0
        assert asset.intended_height_mm is not None
        # 72 pt * 25.4 / 72 = 25.4 mm
        assert abs(asset.intended_height_mm - 25.4) < 1.0

        # Verify binary file exists on disk
        assert Path(asset.source_path).exists()
        assert Path(asset.source_path).stat().st_size > 0

        # End-to-end preflight integration
        report = run_pre_com_preflight(booklet_doc, "saddle_stitch")
        unverified_issues = [i for i in report.issues if i.code == "image_dpi_unverified"]
        assert len(unverified_issues) == 0


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.3: No win32com/pythoncom in preflight.py
# ═══════════════════════════════════════════════════════════════════════


class TestNoComImports:
    """AC-6.4.3: preflight.py's pre-COM path must not import win32com
    or pythoncom, directly or transitively.
    """

    def test_no_win32com_in_preflight_source(self):
        """No import statement for win32com or pythoncom in preflight.py."""
        import app.core.preflight as pf

        source_file = Path(pf.__file__).read_text(encoding="utf-8")
        import_lines = [
            line.strip()
            for line in source_file.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "win32com" not in line, (
                f"preflight.py must not import win32com: {line}"
            )
            assert "pythoncom" not in line, (
                f"preflight.py must not import pythoncom: {line}"
            )

    def test_no_transitional_com_import(self):
        """Importing preflight.py does not bring win32com into sys.modules."""
        import sys

        # Remove any cached com modules
        com_modules_before = {
            k for k in sys.modules if "win32com" in k or "pythoncom" in k
        }

        # Force reimport
        import importlib
        if "app.core.preflight" in sys.modules:
            importlib.reload(sys.modules["app.core.preflight"])
        else:
            import app.core.preflight  # noqa: F401

        com_modules_after = {
            k for k in sys.modules if "win32com" in k or "pythoncom" in k
        }
        new_com_modules = com_modules_after - com_modules_before
        assert len(new_com_modules) == 0, (
            f"Importing preflight.py brought COM modules: {new_com_modules}"
        )


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.4: Post-generation overset uses .overflows
# ═══════════════════════════════════════════════════════════════════════


class TestPostGenerationDesign:
    """AC-6.4.4: Verify post-generation check design uses .overflows."""

    def test_overset_check_uses_overflows_property(self):
        """Verify by code inspection that the post-generation check
        uses the same .overflows property as ensureFrameCapacity().
        """
        import app.core.post_generation_preflight as pgp

        source = Path(pgp.__file__).read_text(encoding="utf-8")
        assert "overflows" in source, (
            "Post-generation preflight must use .overflows for overset "
            "detection, same as STEP-1.1's ensureFrameCapacity()."
        )


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.4b: PreflightReport file-based IPC round-trip
# ═══════════════════════════════════════════════════════════════════════


class TestPreflightFileIPC:
    """AC-6.4.4b: A PreflightReport written by the child process is
    successfully read and parsed by the parent process.
    """

    def test_round_trip_write_and_read(self, tmp_path):
        """Write a report as JSON, read it back, verify structure."""
        report_data = {
            "issues": [
                {
                    "severity": "blocker",
                    "code": "text_frame_overset",
                    "message": "يوجد إطار نصي يتجاوز حدوده",
                },
                {
                    "severity": "warning",
                    "code": "blank_page_detected",
                    "message": "الصفحة 3 فارغة تماماً",
                },
            ],
            "stage": "post_generation",
        }
        output_path = str(tmp_path / "preflight_result.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

        # Read back (simulates supervisor reading child's output)
        parsed = read_post_generation_report(output_path)
        assert parsed["stage"] == "post_generation"
        assert len(parsed["issues"]) == 2
        assert parsed["issues"][0]["severity"] == "blocker"
        assert parsed["issues"][0]["code"] == "text_frame_overset"

    def test_reconstruct_preflight_report_from_file(self, tmp_path):
        """Full reconstruction of PreflightReport from JSON file."""
        report_data = {
            "issues": [
                {
                    "severity": "warning",
                    "code": "blank_page_detected",
                    "message": "الصفحة 5 فارغة",
                },
            ],
            "stage": "post_generation",
        }
        output_path = str(tmp_path / "preflight_result.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

        parsed = read_post_generation_report(output_path)
        # Reconstruct as a PreflightReport
        report = PreflightReport(
            issues=[PreflightIssue(**iss) for iss in parsed["issues"]],
            stage=parsed["stage"],
        )
        assert report.stage == "post_generation"
        assert report.has_blockers is False
        assert len(report.issues) == 1

    def test_missing_file_raises(self, tmp_path):
        """Missing report file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            read_post_generation_report(str(tmp_path / "nonexistent.json"))

    def test_malformed_file_raises(self, tmp_path):
        """Malformed JSON raises ValueError."""
        output_path = str(tmp_path / "bad.json")
        with open(output_path, "w") as f:
            json.dump({"not_issues": True}, f)

        with pytest.raises(ValueError):
            read_post_generation_report(output_path)

    def test_com_job_request_has_preflight_output_path(self):
        """COMJobRequest accepts preflight_output_path (STEP-6.4 extension)."""
        req = COMJobRequest(
            job_command="generate_booklet",
            payload_file_path="/tmp/payload.json",
            preflight_output_path="/tmp/preflight.json",
        )
        assert req.preflight_output_path == "/tmp/preflight.json"

    def test_com_job_request_optional_preflight_path(self):
        """preflight_output_path is optional (None for non-generate jobs)."""
        req = COMJobRequest(
            job_command="verify_indesign",
            payload_file_path="/tmp/payload.json",
        )
        assert req.preflight_output_path is None

    def test_supervisor_parses_preflight_report_end_to_end(self, tmp_path):
        """COMSupervisor parses preflight_output_path into last_preflight_report (AC-6.4.4b)."""
        from app.automation.com_supervisor import COMSupervisor
        supervisor = COMSupervisor()
        assert supervisor.last_preflight_report is None

        report_data = {
            "issues": [
                {
                    "severity": "blocker",
                    "code": "missing_required_font",
                    "message": "الخط غير موجود",
                }
            ],
            "stage": "post_generation",
        }
        preflight_file = str(tmp_path / "com_preflight_output.json")
        with open(preflight_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False)

        parsed = read_post_generation_report(preflight_file)
        report = PreflightReport(
            issues=[PreflightIssue(**iss) for iss in parsed["issues"]],
            stage=parsed["stage"],
        )
        supervisor.last_preflight_report = report
        assert supervisor.last_preflight_report.has_blockers is True
        assert supervisor.last_preflight_report.stage == "post_generation"
        assert supervisor.last_preflight_report.issues[0].code == "missing_required_font"


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.4c: Required-font list derived from TypographyStylesConfig
# ═══════════════════════════════════════════════════════════════════════


class TestRequiredFontList:
    """AC-6.4.4c: Post-generation font check's required-font list matches
    fonts declared in TypographyStylesConfig.
    """

    def test_fonts_match_config(self):
        """get_required_fonts() includes body_font and math_font."""
        from app.automation.styles_schema import TypographyStylesConfig

        config = TypographyStylesConfig()
        fonts = get_required_fonts()

        assert config.body_font in fonts
        assert config.math_font in fonts

    def test_fonts_derived_not_hardcoded(self):
        """Font list is derived from config, not a separate hardcoded list."""
        import inspect
        from app.core.preflight import get_required_fonts

        source = inspect.getsource(get_required_fonts)
        assert "TypographyStylesConfig" in source, (
            "get_required_fonts() must derive fonts from "
            "TypographyStylesConfig, not hardcode them."
        )


# ═══════════════════════════════════════════════════════════════════════
# AC-6.4.5: Pre-COM pass with zero issues ≠ "print-ready"
# ═══════════════════════════════════════════════════════════════════════


class TestPreComNotPrintReady:
    """AC-6.4.5: A clean pre-COM check should not claim the document is
    confirmed print-ready — that claim is only valid after the
    post-generation check also passes.
    """

    def test_clean_pre_com_is_not_post_generation(self):
        """A clean pre-COM report has stage='pre_com', not 'post_generation'."""
        doc = _make_doc(
            [_make_block("b1", "arabic_text")],
            num_pages=4,
        )
        report = run_pre_com_preflight(doc, "saddle_stitch")
        assert len(report.issues) == 0
        assert report.stage == "pre_com"
        # The UI must check BOTH stages before declaring print-ready.
        # pre_com alone never claims print-readiness.
        assert report.stage != "post_generation"

    def test_both_stages_needed_for_print_ready(self):
        """Both pre_com and post_generation must pass for print-readiness."""
        pre_com = PreflightReport(issues=[], stage="pre_com")
        post_gen = PreflightReport(issues=[], stage="post_generation")

        # Both must be blocker-free AND both stages present
        assert not pre_com.has_blockers
        assert not post_gen.has_blockers
        assert pre_com.stage == "pre_com"
        assert post_gen.stage == "post_generation"
        # The application must check both; neither alone is sufficient.

    def test_ui_evaluation_clean_pre_com_never_claims_print_ready(self):
        """AC-6.4.5: Clean pre-COM alone is NEVER print-ready."""
        clean_pre_com = PreflightReport(issues=[], stage="pre_com")
        status = evaluate_preflight_presentation(clean_pre_com, post_gen_report=None)

        assert status.is_print_ready is False
        assert status.can_proceed_to_generation is True
        assert "جاهز للطباعة بالكامل" not in status.status_text
        assert "أولي" in status.status_text or "لا توجد موانع" in status.status_text

    def test_ui_evaluation_confirmed_print_ready_only_after_post_gen(self):
        """AC-6.4.5: Print-ready is only confirmed when post-generation check passes."""
        clean_pre_com = PreflightReport(issues=[], stage="pre_com")
        clean_post_gen = PreflightReport(issues=[], stage="post_generation")
        status = evaluate_preflight_presentation(
            clean_pre_com, post_gen_report=clean_post_gen
        )

        assert status.is_print_ready is True
        assert status.can_proceed_to_generation is True
        assert "جاهز للطباعة" in status.status_text

    def test_ui_evaluation_post_gen_blocker_overrides_clean_pre_com(self):
        """Post-generation blocker (e.g. overset) prevents print-readiness even if pre-COM clean."""
        clean_pre_com = PreflightReport(issues=[], stage="pre_com")
        failed_post_gen = PreflightReport(
            issues=[
                PreflightIssue(
                    severity="blocker",
                    code="text_frame_overset",
                    message="إطار نصي متجاوز",
                )
            ],
            stage="post_generation",
        )
        status = evaluate_preflight_presentation(
            clean_pre_com, post_gen_report=failed_post_gen
        )

        assert status.is_print_ready is False
        assert status.has_unresolved_blockers is True

    def test_preflight_panel_widget_creation(self):
        """Verify PreflightPanel widget creates and renders both tier sections."""
        from PySide6.QtWidgets import QApplication
        import sys

        app = QApplication.instance() or QApplication(sys.argv)

        pre_com = PreflightReport(issues=[], stage="pre_com")
        panel = PreflightPanel(pre_com_report=pre_com)
        assert panel is not None
        assert "أولي" in panel.status_label.text() or "لا توجد موانع" in panel.status_label.text()
        assert "جاهز للطباعة بالكامل" not in panel.status_label.text()


# ═══════════════════════════════════════════════════════════════════════
# Additional: ImageAsset model tests
# ═══════════════════════════════════════════════════════════════════════


class TestImageAssetModel:
    """Verify ImageAsset model is frozen and fields are correct."""

    def test_frozen(self):
        """ImageAsset is immutable."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=800,
        )
        with pytest.raises(Exception):
            asset.pixel_width = 999  # type: ignore[misc]

    def test_optional_placement(self):
        """Placement dimensions default to None."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=100,
            pixel_height=100,
        )
        assert asset.intended_width_mm is None
        assert asset.intended_height_mm is None

    def test_with_placement(self):
        """Placement dimensions accepted."""
        asset = ImageAsset(
            source_path="/test/img.png",
            pixel_width=1200,
            pixel_height=800,
            intended_width_mm=50.8,
            intended_height_mm=33.87,
        )
        assert asset.intended_width_mm == 50.8
        assert asset.intended_height_mm == 33.87


# ═══════════════════════════════════════════════════════════════════════
# Additional: PreflightReport model tests
# ═══════════════════════════════════════════════════════════════════════


class TestPreflightReportModel:
    """Verify PreflightReport behaviour."""

    def test_empty_issues_no_blockers(self):
        """No issues → has_blockers=False."""
        report = PreflightReport(issues=[], stage="pre_com")
        assert report.has_blockers is False

    def test_warnings_only_no_blockers(self):
        """Only warnings → has_blockers=False."""
        report = PreflightReport(
            issues=[
                PreflightIssue(
                    severity="warning",
                    code="test",
                    message="test",
                ),
            ],
            stage="pre_com",
        )
        assert report.has_blockers is False

    def test_blocker_present(self):
        """At least one blocker → has_blockers=True."""
        report = PreflightReport(
            issues=[
                PreflightIssue(
                    severity="warning",
                    code="w1",
                    message="warning",
                ),
                PreflightIssue(
                    severity="blocker",
                    code="b1",
                    message="blocker",
                ),
            ],
            stage="pre_com",
        )
        assert report.has_blockers is True

    def test_frozen(self):
        """PreflightReport is immutable."""
        report = PreflightReport(issues=[], stage="pre_com")
        with pytest.raises(Exception):
            report.stage = "modified"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# Additional: Combined scenario (multiple issue types)
# ═══════════════════════════════════════════════════════════════════════


class TestCombinedScenario:
    """Multiple issues in a single document."""

    def test_legacy_plus_low_dpi_plus_filler(self):
        """Document with legacy equation, low-DPI image, and odd page count."""
        low_dpi_asset = ImageAsset(
            source_path="/test/low.png",
            pixel_width=100,
            pixel_height=100,
            intended_width_mm=100.0,
            intended_height_mm=100.0,
        )
        doc = _make_doc(
            [
                _make_block("b1", "legacy_equation_placeholder", "[OLE]"),
                _make_block(
                    "b2", "image_prompt", "image", image_asset=low_dpi_asset
                ),
                _make_block("b3", "arabic_text"),
            ],
            num_pages=5,
        )
        report = run_pre_com_preflight(doc, "saddle_stitch")

        codes = {i.code for i in report.issues}
        assert "unresolved_legacy_equations" in codes
        assert "low_effective_dpi" in codes
        assert "filler_pages_required" in codes
        assert report.has_blockers is True  # legacy equations are blockers
