"""
Booklet_AI Desktop — STEP-7.3 Unit & Integration Tests (Preserve Source Document Order)

Verifies:
1. Models: TableData has body_index: Optional[int] = None.
   ContentBlock keeps source_position with body_index and sub_index keys.
2. DocxContentExtractor sets body_index on every block and table.
3. build_payload merges and sorts blocks and tables by (body_index, sub_index)
   when all items have body_index.
4. build_payload falls back to source_bbox y0 or old behaviour when items lack body_index.
5. tools/generate_from_docx.py ORDER_CHECK:
   - Verifies sequence of item types in payload matches sequence in source document.
   - Emits ORDER_CHECK: OK on match.
   - Emits warning ORDER_MISMATCH on mismatch.
6. DOCX fixture paragraph -> table -> paragraph -> equation gives payload items
   [block..., table, block..., math].
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
import pytest

try:
    from tests.conftest import requires_xslt
except ImportError:
    from conftest import requires_xslt

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from docx import Document as DocxDocument
from docx.oxml import parse_xml

from app.automation.generation_pipeline import build_payload
from app.automation.geometry import MarginConfig, PageGeometry, PageNumberingStyle
from app.automation.styles_schema import TypographyStylesConfig
from app.core.docx_extractor import DocxContentExtractor
from app.core.models import BookletDocument, ContentBlock, PageData, TableData
from app.core.text_integrity import compute_canonical_hash, verify_text_integrity
from tools.generate_from_docx import run_order_check, run_pipeline


@pytest.fixture
def docx_order_fixture(tmp_path: Path) -> Path:
    """DOCX fixture with sequence: paragraph -> table -> paragraph -> equation."""
    doc_path = tmp_path / "order_fixture.docx"
    doc = DocxDocument()

    # 1. Paragraph with two lines separated by <w:br/>
    p1 = doc.add_paragraph()
    p1.add_run("الفقرة الأولى - السطر الأول")
    p1.add_run().add_break()
    p1.add_run("الفقرة الأولى - السطر الثاني")

    # 2. Table (2x2)
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "عمود 1"
    tbl.cell(0, 1).text = "عمود 2"
    tbl.cell(1, 0).text = "بيانات 1"
    tbl.cell(1, 1).text = "بيانات 2"

    # 3. Paragraph
    doc.add_paragraph("الفقرة الثانية بعد الجدول")

    # 4. Equation paragraph (m:oMath)
    p_eq = doc.add_paragraph()
    omml = parse_xml(
        "<m:oMath xmlns:m='http://schemas.openxmlformats.org/officeDocument/2006/math'>"
        "<m:r><m:t>x^2 + y^2 = r^2</m:t></m:r>"
        "</m:oMath>"
    )
    p_eq._p.append(omml)

    doc.save(str(doc_path))
    return doc_path


def _make_dummy_geometry() -> PageGeometry:
    return PageGeometry(
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


def _make_dummy_styles() -> TypographyStylesConfig:
    return TypographyStylesConfig(
        body_font="Tajawal",
        math_font="Cambria Math",
    )


def test_table_data_body_index_model():
    """Requirement 1: TableData has body_index: Optional[int] = None."""
    t_default = TableData(table_id="tbl_1", headers=["A"], rows=[["1"]])
    assert t_default.body_index is None

    t_indexed = TableData(table_id="tbl_2", headers=["A"], rows=[["1"]], body_index=3)
    assert t_indexed.body_index == 3


@requires_xslt
def test_docx_extractor_body_index_assignment(docx_order_fixture: Path):
    """Requirement 2: DocxContentExtractor sets body_index on every block and table."""
    extractor = DocxContentExtractor()
    doc = extractor.extract(str(docx_order_fixture))

    page = doc.pages[0]
    blocks = page.content_blocks
    tables = page.tables

    # Paragraph 1 had a <w:br> -> produced 2 blocks with body_index=0, sub_index=0 and 1
    assert len(blocks) == 4
    assert blocks[0].source_position.get("body_index") == 0
    assert blocks[0].source_position.get("sub_index") == 0

    assert blocks[1].source_position.get("body_index") == 0
    assert blocks[1].source_position.get("sub_index") == 1

    # Table is child 1 -> body_index=1
    assert len(tables) == 1
    assert tables[0].body_index == 1

    # Paragraph 2 is child 2 -> body_index=2, sub_index=0
    assert blocks[2].source_position.get("body_index") == 2
    assert blocks[2].source_position.get("sub_index") == 0

    # Equation is child 3 -> body_index=3, sub_index=0
    assert blocks[3].source_position.get("body_index") == 3
    assert blocks[3].source_position.get("sub_index") == 0
    assert blocks[3].block_type == "math_formula"

    # Integrity check: verify hash matches raw content
    for b in blocks:
        verify_text_integrity(b.raw_content, b.content_hash, context=b.block_id)


@requires_xslt
def test_build_payload_preserves_document_order(docx_order_fixture: Path, tmp_path: Path):
    """Requirement 3 & 5: Payload items are ordered [block..., table, block..., math]."""
    extractor = DocxContentExtractor()
    doc = extractor.extract(str(docx_order_fixture))

    payload_path = build_payload(
        document=doc,
        geometry=_make_dummy_geometry(),
        styles_cfg=_make_dummy_styles(),
        output_dir=tmp_path / "out",
    )

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    items = payload.get("items", [])
    assert len(items) == 5

    # Sequence must be: block (p1.0), block (p1.1), table (tbl), block (p2), math (eq)
    assert items[0]["item_type"] == "block"
    assert items[0]["source_position"]["body_index"] == 0
    assert items[0]["source_position"]["sub_index"] == 0

    assert items[1]["item_type"] == "block"
    assert items[1]["source_position"]["body_index"] == 0
    assert items[1]["source_position"]["sub_index"] == 1

    assert items[2]["item_type"] == "table"
    assert items[2]["body_index"] == 1

    assert items[3]["item_type"] == "block"
    assert items[3]["source_position"]["body_index"] == 2
    assert items[3]["source_position"]["sub_index"] == 0

    assert items[4]["item_type"] == "block"
    assert items[4]["block_type"] == "math_formula"
    assert items[4]["source_position"]["body_index"] == 3
    assert items[4]["source_position"]["sub_index"] == 0

    # Ensure blocks and tables lists are still present and unaltered
    assert len(payload["blocks"]) == 4
    assert len(payload["tables"]) == 1


def test_build_payload_no_body_index_keeps_old_behaviour(tmp_path: Path):
    """Requirement 3 & 5: When items lack body_index, old behaviour is kept (all blocks, then all tables)."""
    text1 = "نص تجريبي 1"
    text2 = "نص تجريبي 2"
    b1 = ContentBlock(
        block_id="blk_0001",
        block_type="arabic_text",
        raw_content=text1,
        content_hash=compute_canonical_hash(text1),
        source_position={},  # No body_index, no bbox
    )
    b2 = ContentBlock(
        block_id="blk_0002",
        block_type="arabic_text",
        raw_content=text2,
        content_hash=compute_canonical_hash(text2),
        source_position={},  # No body_index, no bbox
    )
    tbl = TableData(
        table_id="tbl_0001",
        headers=["عنوان"],
        rows=[["قيمة"]],
        body_index=None,
        source_bbox=None,
    )
    page = PageData(
        page_number=1,
        width=595.0,
        height=842.0,
        content_blocks=[b1, b2],
        tables=[tbl],
    )
    doc = BookletDocument(
        doc_id="legacy_test",
        file_path="legacy.pdf",
        file_type="pdf",
        pages=[page],
    )

    payload_path = build_payload(
        document=doc,
        geometry=_make_dummy_geometry(),
        styles_cfg=_make_dummy_styles(),
        output_dir=tmp_path / "out_legacy",
    )

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    items = payload.get("items", [])
    assert len(items) == 3
    # In old behavior: all blocks come first, then all tables
    assert items[0]["item_type"] == "block"
    assert items[0]["block_id"] == "blk_0001"
    assert items[1]["item_type"] == "block"
    assert items[1]["block_id"] == "blk_0002"
    assert items[2]["item_type"] == "table"
    assert items[2]["table_id"] == "tbl_0001"


@requires_xslt
def test_order_check_ok(docx_order_fixture: Path, tmp_path: Path, capsys):
    """Requirement 4: run_order_check prints ORDER_CHECK: OK when order matches."""
    extractor = DocxContentExtractor()
    doc = extractor.extract(str(docx_order_fixture))

    payload_path = build_payload(
        document=doc,
        geometry=_make_dummy_geometry(),
        styles_cfg=_make_dummy_styles(),
        output_dir=tmp_path / "out_check",
    )

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    is_ok, warnings = run_order_check(
        docx_path=docx_order_fixture,
        payload_items=payload.get("items", []),
    )

    assert is_ok is True
    assert warnings == []
    captured = capsys.readouterr()
    assert "ORDER_CHECK: OK" in captured.out


@requires_xslt
def test_order_check_detects_mismatch(docx_order_fixture: Path, tmp_path: Path, capsys):
    """Requirement 4: run_order_check adds ORDER_MISMATCH on swapped order."""
    extractor = DocxContentExtractor()
    doc = extractor.extract(str(docx_order_fixture))

    payload_path = build_payload(
        document=doc,
        geometry=_make_dummy_geometry(),
        styles_cfg=_make_dummy_styles(),
        output_dir=tmp_path / "out_mismatch",
    )

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    # Deliberately move table to the very end (simulating the old bug)
    items = list(payload.get("items", []))
    table_item = items.pop(2)
    items.append(table_item)

    is_ok, warnings = run_order_check(
        docx_path=docx_order_fixture,
        payload_items=items,
    )

    assert is_ok is False
    assert "ORDER_MISMATCH" in warnings
    captured = capsys.readouterr()
    assert "ORDER_MISMATCH" in captured.out


@requires_xslt
def test_run_pipeline_end_to_end_order(docx_order_fixture: Path, tmp_path: Path, capsys):
    """Full CLI pipeline on order fixture passes order check with 0 exit code."""
    out_dir = tmp_path / "cli_out"
    exit_code = run_pipeline(
        docx_file=docx_order_fixture,
        output_dir=out_dir,
        build_only=True,
        no_ai=True,
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "ORDER_CHECK: OK" in captured.out
    assert "ORDER_MISMATCH" not in captured.out

    report_path = out_dir / "report.json"
    assert report_path.exists()
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    assert "ORDER_MISMATCH" not in report.get("warnings", [])
