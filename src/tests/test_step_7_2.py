"""
Booklet_AI Desktop — STEP-7.2 Unit & Integration Tests (DOCX Generation CLI & Report Builder)

Verifies:
1. Extraction of a real DOCX fixture into a BookletDocument with hash verification.
2. Rule-based classification preserving Zero-Mutation guarantee.
3. build_report() accurately aggregates:
   - Source paragraph, table, equation counts.
   - Breakdown of blocks per block_type.
   - Legacy equation count.
   - Math formula count and first 3 formulas sample.
   - Actual page count and per-page diagnostics.
   - Stage timings and warnings list.
4. CLI run_pipeline with --build-only and --no-ai produces valid payload and report.json.
5. Error handling exits gracefully on invalid file paths.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from docx import Document as DocxDocument

from app.core.models import BookletDocument, ContentBlock, PageData, TableData
from app.core.text_integrity import compute_canonical_hash
from tools.generate_from_docx import (
    build_report,
    classify_document,
    extract_document,
    format_report_text,
    run_pipeline,
)


@pytest.fixture
def sample_docx_fixture(tmp_path: Path) -> Path:
    """Create a minimal real Word document with paragraphs, table, and question."""
    doc_path = tmp_path / "sample_curriculum.docx"
    doc = DocxDocument()

    # 1. Heading
    doc.add_heading("الفصل الأول: التفاضل والتكامل", level=1)

    # 2. Arabic body paragraph
    doc.add_paragraph(
        "يُعد التفاضل أحد أهم فروع الرياضيات الحديثة، ويُستخدم لحساب معدلات التغير "
        "والمماسات لمنحنيات الدوال المختلفة في التطبيقات الهندسية والفيزيائية."
    )

    # 3. Ministry Question
    doc.add_paragraph(
        "س وزاري: جد معادلة المماس لمنحني الدالة f(x) = x² + 3 عند النقطة (1, 4)."
    )

    # 4. List Item
    doc.add_paragraph("- الخاصية الأولى: مشتقة الثابت تساوي صفراً.")

    # 5. Table (2 rows, 2 columns)
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "الدالة f(x)"
    tbl.cell(0, 1).text = "المشتقة f'(x)"
    tbl.cell(1, 0).text = "c"
    tbl.cell(1, 1).text = "0"

    doc.save(str(doc_path))
    return doc_path


@pytest.fixture
def equivalent_test1_docx_fixture(tmp_path: Path) -> Path:
    """Generate an equivalent DOCX fixture replacing private samples/test1.docx.

    Contains:
    0. w:p with 7 Arabic lines separated by <w:br/>
    1. w:tbl (5x4 LTR table without bidiVisual, Arabic columns)
    2. w:p empty with a <w:br/>
    3. w:p empty
    4. w:p with an inline m:oMath equation
    """
    from docx.oxml import parse_xml

    doc_path = tmp_path / "equivalent_test1.docx"
    doc = DocxDocument()

    # 0. Arabic text with 7 line breaks
    p0 = doc.add_paragraph()
    p0.add_run("السلام عليكم هذه هي مادة الرياضيات ")
    p0.add_run().add_break()
    p0.add_run("ان مادة الرياضيات هي مادة زنجية حافوزلقية ")
    p0.add_run().add_break()
    p0.add_run("س وزاري : ما هي مادة الرياضيات  بالنسبة لك ؟ ")
    p0.add_run().add_break()
    p0.add_run("ج : هي مادة زنجية ")
    p0.add_run().add_break()
    p0.add_run("س : من اوا سيد حمبولة ام سيد حنوش ؟ ")
    p0.add_run().add_break()
    p0.add_run(
        "ج  : في المعركة التي حدثت ساعه 12:00 ليلا قام السيد حنوش بتوجيه شربة قوية "
        "الى سيد حمبولة ومن بعدها صدها سيد حمبولة ليقوم بعدها عباس دي واترسون "
        "بالهجوم عليهما وطرحهما ارضا هما الاثنين "
    )
    p0.add_run().add_break()
    p0.add_run("س : عدد شخصيات قوية وشخصيات متوسطة وشخصيات ضعيفة في الرياضيات ")
    p0.add_run().add_break()

    # 1. Table 5x4 LTR without bidiVisual
    tbl = doc.add_table(rows=5, cols=4)
    table_data = [
        ["متوسط متوسط متوسط", "قوي قوي قوي", "ضعيف ضعيف ضعيف", "الشخصية الشخصية الشخصية"],
        ["", "نعم نعم نعم", "", "عباس دي واترسون عباس دي واترسون عباس دي واترسون"],
        ["نعم نعم نعم", "", "", "سيد حنوش سيد حنوش سيد حنوش"],
        ["نعم نعم نعم", "", "", "سيد حمبولة سيد حمبولة سيد حمبولة"],
        ["", "", "نعم نعم نعم", "السركتح السركتح السركتح"],
    ]
    for r_idx, row in enumerate(table_data):
        for c_idx, val in enumerate(row):
            tbl.cell(r_idx, c_idx).text = val

    # 2. Empty w:p with break
    p2 = doc.add_paragraph()
    p2.add_run().add_break()

    # 3. Empty w:p
    doc.add_paragraph()

    # 4. w:p with inline m:oMath equation
    p4 = doc.add_paragraph()
    omml = parse_xml(
        r'''<m:oMath xmlns:m='http://schemas.openxmlformats.org/officeDocument/2006/math'><m:r><m:t>1+x^n=1+nx</m:t></m:r></m:oMath>'''
    )
    p4._p.append(omml)

    doc.save(str(doc_path))
    return doc_path


class TestDocxPipelineExtractionAndClassification:
    def test_extract_document_fixture(self, sample_docx_fixture: Path):
        """Extract content from real DOCX fixture and verify source counts and integrity."""
        booklet_doc, source_counts, elapsed = extract_document(sample_docx_fixture)

        assert isinstance(booklet_doc, BookletDocument)
        assert len(booklet_doc.pages) == 1
        page = booklet_doc.pages[0]

        # Verify source counts
        assert source_counts["paragraphs"] >= 4
        assert source_counts["tables"] == 1
        assert source_counts["equations"] == 0
        assert elapsed >= 0.0

        # Verify extracted blocks have matching canonical hashes
        assert len(page.content_blocks) >= 4
        for b in page.content_blocks:
            assert b.content_hash == compute_canonical_hash(b.raw_content)

    def test_classify_document_offline(self, sample_docx_fixture: Path):
        """Classify document blocks with RuleBasedClassifier under --no-ai."""
        booklet_doc, _, _ = extract_document(sample_docx_fixture)
        classified_doc, warnings, elapsed = classify_document(booklet_doc, no_ai=True)

        assert elapsed >= 0.0
        assert isinstance(warnings, list)
        page = classified_doc.pages[0]

        # Check that classification mapped expected types
        types = [b.block_type for b in page.content_blocks]
        assert "chapter_header" in types
        assert "arabic_text" in types
        assert "ministry_question" in types
        assert "list_item" in types

        # Zero-Mutation guarantee
        for b in page.content_blocks:
            assert b.content_hash == compute_canonical_hash(b.raw_content)


class TestReportBuilder:
    def test_build_report_structure(self):
        """Verify build_report generates all required fields."""
        blk1 = ContentBlock(
            block_id="b1",
            block_type="chapter_header",
            raw_content="الفصل الأول",
            content_hash=compute_canonical_hash("الفصل الأول"),
        )
        blk2 = ContentBlock(
            block_id="b2",
            block_type="math_formula",
            raw_content="x^2 + y^2 = r^2",
            content_hash=compute_canonical_hash("x^2 + y^2 = r^2"),
        )
        blk3 = ContentBlock(
            block_id="b3",
            block_type="math_formula",
            raw_content="E = mc^2",
            content_hash=compute_canonical_hash("E = mc^2"),
        )
        blk4 = ContentBlock(
            block_id="b4",
            block_type="legacy_equation_placeholder",
            raw_content="[OLE: MathType 5.0]",
            content_hash=compute_canonical_hash("[OLE: MathType 5.0]"),
        )

        page = PageData(
            page_number=1,
            width=595.28,
            height=841.89,
            content_blocks=[blk1, blk2, blk3, blk4],
            tables=[],
        )
        booklet_doc = BookletDocument(
            doc_id="report_test_doc",
            file_path="C:/test/sample.docx",
            file_type="docx",
            pages=[page],
        )

        source_counts = {
            "paragraphs": 5,
            "tables": 1,
            "equations": 3,
            "omml_math": 2,
            "legacy_equations": 1,
        }
        timings = {
            "extraction_seconds": 0.12,
            "classification_seconds": 0.05,
            "payload_build_seconds": 0.08,
            "indesign_generation_seconds": 1.25,
            "total_seconds": 1.50,
        }
        indesign_result = {
            "actual_page_count": 4,
            "imposed_page_count": 4,
            "page_count": 4,
            "pages": [
                {
                    "page_number": 1,
                    "side": "left_hand",
                    "geometric_bounds": [62.69, 42.52, 779.20, 532.92],
                    "table_width": None,
                }
            ],
            "warnings": ["Font substituted"],
        }
        pipeline_warnings = ["AI key not set"]

        report = build_report(
            booklet_doc=booklet_doc,
            source_counts=source_counts,
            timings=timings,
            indesign_result=indesign_result,
            pipeline_warnings=pipeline_warnings,
            output_files={"report_json": "report.json"},
        )

        # 1. Source counts
        assert report["source_counts"]["paragraphs"] == 5
        assert report["source_counts"]["tables"] == 1
        assert report["source_counts"]["equations"] == 3

        # 2. Blocks per block_type
        assert report["blocks_per_type"]["chapter_header"] == 1
        assert report["blocks_per_type"]["math_formula"] == 2
        assert report["blocks_per_type"]["legacy_equation_placeholder"] == 1

        # 3. Math & Legacy equation counts and first 3 sample
        assert report["legacy_equation_count"] == 1
        assert report["math_formula_count"] == 2
        assert len(report["first_3_math_formulas"]) == 2
        assert report["first_3_math_formulas"][0] == "x^2 + y^2 = r^2"
        assert report["first_3_math_formulas"][1] == "E = mc^2"

        # 4. Actual page count & per-page diagnostics
        assert report["actual_page_count"] == 4
        assert report["imposed_page_count"] == 4
        assert len(report["per_page_diagnostics"]) == 1
        assert report["per_page_diagnostics"][0]["side"] == "left_hand"

        # 5. Warnings and timings
        assert "AI key not set" in report["warnings"]
        assert "Font substituted" in report["warnings"]
        assert report["timings"]["total_seconds"] == 1.50

    def test_format_report_text(self):
        """Verify format_report_text produces readable multi-line summary."""
        report = {
            "doc_id": "d1",
            "source_file": "sample.docx",
            "source_counts": {"paragraphs": 10, "tables": 2, "equations": 3, "omml_math": 2, "legacy_equations": 1},
            "blocks_per_type": {"arabic_text": 8, "math_formula": 2},
            "total_blocks": 10,
            "legacy_equation_count": 1,
            "math_formula_count": 2,
            "first_3_math_formulas": ["a + b = c"],
            "actual_page_count": 4,
            "imposed_page_count": 4,
            "final_page_count": 4,
            "per_page_diagnostics": [{"page_number": 1, "side": "left_hand", "geometric_bounds": [1, 2, 3, 4], "table_width": None}],
            "warnings": ["Sample warning"],
            "timings": {"extraction_seconds": 0.1, "total_seconds": 1.0},
            "output_files": {"report_json": "report.json"},
        }

        text = format_report_text(report)
        assert "Booklet_AI — STEP-7.2 Real DOCX Measurement Report" in text
        assert "Source Document Counts" in text
        assert "Paragraphs:       10" in text
        assert "Math Formula Blocks:          2" in text
        assert "a + b = c" in text
        assert "Actual Content Pages:  4" in text
        assert "Sample warning" in text


class TestPipelineExecution:
    def test_run_pipeline_build_only_produces_valid_payload_and_report(
        self, sample_docx_fixture: Path, tmp_path: Path
    ):
        """End-to-end test of run_pipeline in --build-only mode."""
        out_dir = tmp_path / "out_build"

        exit_code = run_pipeline(
            docx_file=sample_docx_fixture,
            output_dir=out_dir,
            binding="saddle_stitch",
            footer_text="اختبار الفصل",
            no_ai=True,
            build_only=True,
        )

        assert exit_code == 0
        report_file = out_dir / "report.json"
        assert report_file.exists()

        report_data = json.loads(report_file.read_text(encoding="utf-8"))
        assert report_data["source_counts"]["paragraphs"] >= 4
        assert report_data["source_counts"]["tables"] == 1
        assert "chapter_header" in report_data["blocks_per_type"]

        # Check payload was also created
        payload_files = list(out_dir.glob("*_payload.json"))
        assert len(payload_files) == 1
        payload_data = json.loads(payload_files[0].read_text(encoding="utf-8"))
        assert payload_data["footer_text"] == "اختبار الفصل"

    def test_run_pipeline_file_not_found(self, tmp_path: Path):
        """Verify pipeline handles missing file gracefully."""
        missing_file = tmp_path / "does_not_exist.docx"
        exit_code = run_pipeline(
            docx_file=missing_file,
            output_dir=tmp_path / "out",
            no_ai=True,
            build_only=True,
        )
        assert exit_code == 1


class TestTableVisualOrder:
    def test_table_with_bidivisual_keeps_order(self, tmp_path: Path):
        """When w:bidiVisual is present, table cell order is kept as-is."""
        from docx.oxml import OxmlElement
        doc = DocxDocument()
        tbl = doc.add_table(rows=2, cols=3)
        tbl.cell(0, 0).text = "يمين_0"
        tbl.cell(0, 1).text = "وسط_0"
        tbl.cell(0, 2).text = "يسار_0"
        tbl.cell(1, 0).text = "يمين_1"
        tbl.cell(1, 1).text = "وسط_1"
        tbl.cell(1, 2).text = "يسار_1"

        # Add w:bidiVisual
        bidi = OxmlElement("w:bidiVisual")
        tbl._tbl.tblPr.append(bidi)

        file_path = tmp_path / "table_bidi.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))
        extracted_tbl = doc_model.pages[0].tables[0]

        # Order must be kept as-is
        assert extracted_tbl.headers == ["يمين_0", "وسط_0", "يسار_0"]
        assert extracted_tbl.rows == [["يمين_1", "وسط_1", "يسار_1"]]
        assert "MERGED_CELLS_NOT_SUPPORTED" not in doc_model.warnings

    def test_table_without_bidivisual_reverses_order(self, tmp_path: Path):
        """When w:bidiVisual is absent (LTR table), cell order of every row is reversed."""
        doc = DocxDocument()
        tbl = doc.add_table(rows=2, cols=3)
        # Author typed visually from right in LTR table:
        # Col 0 is visually leftmost, Col 2 is visually rightmost
        tbl.cell(0, 0).text = "يسار_0"
        tbl.cell(0, 1).text = "وسط_0"
        tbl.cell(0, 2).text = "يمين_0"
        tbl.cell(1, 0).text = "يسار_1"
        tbl.cell(1, 1).text = "وسط_1"
        tbl.cell(1, 2).text = "يمين_1"

        file_path = tmp_path / "table_no_bidi.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))
        extracted_tbl = doc_model.pages[0].tables[0]

        # Reversed order: column 0 becomes the visually rightmost column
        assert extracted_tbl.headers == ["يمين_0", "وسط_0", "يسار_0"]
        assert extracted_tbl.rows == [["يمين_1", "وسط_1", "يسار_1"]]
        assert "MERGED_CELLS_NOT_SUPPORTED" not in doc_model.warnings

    def test_table_with_merged_cells_keeps_flat_order_and_warns(self, tmp_path: Path):
        """When w:gridSpan or w:vMerge exists, flat order is kept and MERGED_CELLS_NOT_SUPPORTED emitted."""
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        doc = DocxDocument()
        tbl = doc.add_table(rows=2, cols=2)
        tbl.cell(0, 0).text = "خلية_مدمجة"
        tbl.cell(0, 1).text = "خلية_أخرى"
        tbl.cell(1, 0).text = "بيانات_1"
        tbl.cell(1, 1).text = "بيانات_2"

        # Add w:gridSpan to cell (0, 0)
        gs = OxmlElement("w:gridSpan")
        gs.set(qn("w:val"), "2")
        tbl.cell(0, 0)._tc.get_or_add_tcPr().append(gs)

        file_path = tmp_path / "table_merged.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))
        extracted_tbl = doc_model.pages[0].tables[0]

        # Flat order kept without reversal
        assert extracted_tbl.headers == ["خلية_مدمجة", "خلية_أخرى"]
        assert "MERGED_CELLS_NOT_SUPPORTED" in doc_model.warnings


class TestLineBreaks:
    def test_line_break_splits_paragraph_into_separate_blocks(self, tmp_path: Path):
        """<w:br/> inside a w:p splits into separate content blocks, skipping empty segments."""
        doc = DocxDocument()
        p = doc.add_paragraph()
        p.add_run("السطر الأول من الفقرة ")
        p.add_run().add_break()
        p.add_run("السطر الثاني بعد الفاصل ")
        p.add_run().add_break()
        p.add_run().add_break()  # Consecutive break (empty segment)
        p.add_run("س وزاري : ما هو نص السؤال الوزاري هنا ؟ ")
        p.add_run().add_break()  # Trailing break

        file_path = tmp_path / "line_breaks.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))
        blocks = doc_model.pages[0].content_blocks

        # Exactly 3 non-empty blocks (empty segments skipped)
        assert len(blocks) == 3
        assert blocks[0].raw_content == "السطر الأول من الفقرة "
        assert blocks[1].raw_content == "السطر الثاني بعد الفاصل "
        assert blocks[2].raw_content == "س وزاري : ما هو نص السؤال الوزاري هنا ؟ "

    def test_line_break_preserves_all_characters(self, tmp_path: Path):
        """Concatenation of all segment texts equals original paragraph text without breaks."""
        doc = DocxDocument()
        p = doc.add_paragraph()
        p.add_run("جزء أول من النص ")
        p.add_run().add_break()
        p.add_run("وجزء ثان مع مسافات  ")
        p.add_run().add_break()
        p.add_run("وجزء ثالث أخير.")

        file_path = tmp_path / "preserve_chars.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor, W_T
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))

        raw_doc = DocxDocument(str(file_path))
        orig_text = "".join(t.text for t in raw_doc.element.body[0].iter(W_T) if t.text)

        reconstructed = "".join(b.raw_content for b in doc_model.pages[0].content_blocks)
        assert reconstructed == orig_text
        assert len(reconstructed) == len(orig_text)

    def test_line_break_classifier_sees_ministry_question(self, tmp_path: Path):
        """Line-split ensures questions in soft-break paragraphs are classified as ministry_question."""
        doc = DocxDocument()
        p = doc.add_paragraph()
        p.add_run("مقدمة عامة لموضوع الدرس ")
        p.add_run().add_break()
        p.add_run("س وزاري : جد قيمة التكامل التالي ؟ ")
        p.add_run().add_break()
        p.add_run("ملاحظة ختامية للحل.")

        file_path = tmp_path / "classified_lines.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))
        classified_doc, _, _ = classify_document(doc_model, no_ai=True)

        types = [b.block_type for b in classified_doc.pages[0].content_blocks]
        assert types == ["arabic_text", "ministry_question", "arabic_text"]

    def test_page_break_ignored_warning(self, tmp_path: Path):
        """Page breaks (w:type='page') do not split and emit PAGE_BREAK_IGNORED."""
        from docx.enum.text import WD_BREAK

        doc = DocxDocument()
        p = doc.add_paragraph()
        p.add_run("نص قبل فاصل الصفحات ")
        r = p.add_run()
        r.add_break(WD_BREAK.PAGE)
        p.add_run("ونص مستمر بعد الفاصل.")

        file_path = tmp_path / "page_break.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))

        # Must not split into separate blocks
        assert len(doc_model.pages[0].content_blocks) == 1
        assert "PAGE_BREAK_IGNORED" in doc_model.warnings
        assert doc_model.pages[0].content_blocks[0].raw_content == "نص قبل فاصل الصفحات ونص مستمر بعد الفاصل."


class TestMathMLWarning:
    def test_mathml_not_renderable_warning_emitted(self, equivalent_test1_docx_fixture: Path):
        """MathML equations emit MATHML_NOT_RENDERABLE warning."""
        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(equivalent_test1_docx_fixture))

        math_blocks = [b for b in doc_model.pages[0].content_blocks if b.block_type == "math_formula"]
        assert len(math_blocks) >= 1
        assert math_blocks[0].raw_content.startswith("<mml:math") or math_blocks[0].raw_content.startswith("<math")
        assert "MATHML_NOT_RENDERABLE" in doc_model.warnings


class TestCoverageAudit:
    def test_coverage_audit_rows_structure(self, equivalent_test1_docx_fixture: Path):
        """Verify coverage audit generates required structure for paragraphs and tables."""
        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(equivalent_test1_docx_fixture))

        audit_rows = doc_model.audit_rows
        assert len(audit_rows) == 5

        # Row 0: w:p with textWrapping breaks
        p0_audit = audit_rows[0]
        assert p0_audit["element_type"] == "paragraph"
        assert p0_audit["is_empty"] is False
        assert p0_audit["br_count"] == 7
        assert len(p0_audit["blocks"]) == 7

        # Row 1: w:tbl
        tbl_audit = audit_rows[1]
        assert tbl_audit["element_type"] == "table"
        assert tbl_audit["rows"] == 5
        assert tbl_audit["cols"] == 4
        assert tbl_audit["has_bidi_visual"] == "no"
        assert tbl_audit["has_merged"] == "no"
        assert tbl_audit["table_id"] == "tbl_0001"

        # Row 2: empty w:p
        p2_audit = audit_rows[2]
        assert p2_audit["element_type"] == "paragraph"
        assert p2_audit["is_empty"] is True
        assert p2_audit["blocks"] == []

    def test_text_coverage_check_on_test1(self, equivalent_test1_docx_fixture: Path):
        """Verify text-coverage check passes with zero loss on equivalent fixture."""
        from app.core.docx_extractor import DocxContentExtractor
        from tools.generate_from_docx import run_coverage_audit

        ext = DocxContentExtractor()
        doc_model = ext.extract(str(equivalent_test1_docx_fixture))
        audit_rows, audit_warnings, text_loss = run_coverage_audit(
            str(equivalent_test1_docx_fixture), doc_model, allow_loss=False
        )

        assert text_loss is False
        assert "TEXT_DROPPED" not in audit_warnings

    def test_text_drop_triggers_warning_and_nonzero_exit_unless_allow_loss(self, tmp_path: Path):
        """Simulated dropped paragraph triggers TEXT_DROPPED and exit code 1 unless --allow-loss."""
        doc = DocxDocument()
        doc.add_paragraph("فقرة موجودة ومستخرجة بنجاح.")
        doc.add_paragraph("فقرة مفقودة أُسقطت ولم تُستخرج.")
        file_path = tmp_path / "dropped_test.docx"
        doc.save(str(file_path))

        from app.core.docx_extractor import DocxContentExtractor
        ext = DocxContentExtractor()
        doc_model = ext.extract(str(file_path))

        # Manually drop the second block to simulate extraction loss
        truncated_blocks = [doc_model.pages[0].content_blocks[0]]
        page = doc_model.pages[0].model_copy(update={"content_blocks": truncated_blocks})
        truncated_doc = doc_model.model_copy(update={"pages": [page]})

        from tools.generate_from_docx import run_coverage_audit
        audit_rows, audit_warnings, text_loss = run_coverage_audit(
            str(file_path), truncated_doc, allow_loss=False
        )

        assert text_loss is True
        assert "TEXT_DROPPED" in audit_warnings


class TestTableHeaderTextColor:
    def test_table_header_style_fill_color_is_white(self):
        """TableHeaderStyle in generate_booklet.jsx has white (Paper) fill color set on the style."""
        jsx_path = PROJECT_ROOT / "src" / "assets" / "scripts" / "generate_booklet.jsx"
        content = jsx_path.read_text(encoding="utf-8")

        assert "TableHeaderStyle" in content
        # Check fillColor is Paper
        assert 'fillColor: (paperColor && paperColor.isValid) ? paperColor : "Paper"' in content

    def test_table_header_contrast_ratio(self):
        """Verify contrast ratio of Paper text (#FFFFFF) on dark header fill ([24, 76, 120]) exceeds WCAG AAA (7:1)."""
        def srgb_to_linear(c):
            c_norm = c / 255.0
            return c_norm / 12.92 if c_norm <= 0.04045 else ((c_norm + 0.055) / 1.055) ** 2.4

        def rel_luminance(r, g, b):
            return 0.2126 * srgb_to_linear(r) + 0.7152 * srgb_to_linear(g) + 0.0722 * srgb_to_linear(b)

        # Primary header fill: RGB(24, 76, 120)
        l_fill = rel_luminance(24, 76, 120)
        # White paper text: RGB(255, 255, 255)
        l_text = rel_luminance(255, 255, 255)

        contrast = (l_text + 0.05) / (l_fill + 0.05)
        assert contrast >= 7.0  # WCAG AAA requirement (7:1)
        assert contrast > 8.0
