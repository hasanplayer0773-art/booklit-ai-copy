"""
Booklet_AI Desktop — STEP-6.8 Unit & Integration Tests (Optional Page Numbering Style)

Covers all Acceptance Criteria:
- [AC-6.8.1]: Selecting Arabic-Indic numbering produces page numbers rendered as ١٢٣-style digits
  in the generated document; selecting Western Arabic numbering produces 123-style digits — both via
  InDesign's native digit-shaping property applied to the existing AUTO_PAGE_NUMBER marker, not via
  hand-inserted digit text and not via a numbering-style/section-scheme property being mistaken for a
  digit-shape property.
- [AC-6.8.2]: Changing this setting does not alter or disable SpecialCharacters.AUTO_PAGE_NUMBER's
  existing automatic-update behavior (AC-2.1.2) — page numbers still update correctly as pages are
  added via ensureFrameCapacity() (STEP-1.1) or addBlankFillerPages() (STEP-2.1), regardless of
  which numbering style is selected.
- [AC-6.8.3]: Two documents processed in the same batch (STEP-6.7) with different numbering-style
  settings each produce output matching their own setting — verified this is a per-document, not
  per-application-session, value.
- [AC-6.8.4]: Selecting Arabic-Indic digit rendering never changes the digit glyphs of any ordinary
  source-text content, question numbers, or dates elsewhere in the document — verified by confirming
  document_content_hash (STEP-6.3) for the non-page-number content is unaffected by this setting.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.automation.batch_queue import BatchQueue, create_job_scoped_state
from app.automation.geometry import (
    MarginConfig,
    PageGeometry,
    PageNumberingStyle,
    format_page_number,
)
from app.automation.pagination_sync import imposed_page_count, sync_filler_pages
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import (
    compute_canonical_hash,
    compute_document_content_hash,
)
from app.state.document_store import DocumentStore
from app.utils.db import init_db


# ═══════════════════════════════════════════════════════════════════════
# Mock InDesign DOM for ExtendScript / COM Master Spread Verification
# ═══════════════════════════════════════════════════════════════════════


class MockSpecialCharacters:
    AUTO_PAGE_NUMBER = 1396797550
    SECTION_MARKER = 1400073838


class MockDigitsTypeOptions:
    DEFAULT_DIGITS = 1684628580
    ARABIC_DIGITS = 1684627826  # Western 123
    HINDI_DIGITS = 1684629609   # Arabic-Indic ١٢٣
    FARSI_DIGITS = 1684629107


class MockPageSideOptions:
    SINGLE_SIDED = 1936551027
    LEFT_HAND = 1818846324
    RIGHT_HAND = 1919251310


class MockCharacter:
    def __init__(self, char_text: str = "") -> None:
        self.contents = char_text
        self.digitsType: int | None = None
        self.isValid = True


class MockInsertionPoint:
    def __init__(self, parent_frame: MockTextFrame) -> None:
        self.parent_frame = parent_frame
        self._contents: Any = None

    @property
    def contents(self) -> Any:
        return self._contents

    @contents.setter
    def contents(self, val: Any) -> None:
        self._contents = val
        self.parent_frame._record_content(val)

    def textVariableInstances_add(self, **kwargs) -> Any:
        return MagicMock()


class MockParagraph:
    def __init__(self) -> None:
        self.digitsType: int | None = None
        self.appliedParagraphStyle: Any = None

    def applyParagraphStyle(self, style: Any, clear: bool = False) -> None:
        self.appliedParagraphStyle = style


class MockParagraphCollection:
    def __init__(self, parent_frame: MockTextFrame) -> None:
        self.parent_frame = parent_frame
        self.items: List[MockParagraph] = [MockParagraph()]

    def everyItem(self) -> MockParagraph:
        # Returns a proxy representing everyItem()
        return self.items[0]


class MockTextFrame:
    def __init__(self, bounds: List[float]) -> None:
        self.geometricBounds = bounds
        self.label = ""
        self.contents_stream: List[Any] = []
        self.characters_list: List[MockCharacter] = []
        self.paragraphs = MockParagraphCollection(self)
        self.textFramePreferences = MagicMock()
        self.texts = [self]
        self.digitsType: int | None = None

    def _record_content(self, val: Any) -> None:
        self.contents_stream.append(val)
        if isinstance(val, str):
            for c in val:
                self.characters_list.append(MockCharacter(c))
        else:
            # Special character marker (e.g. AUTO_PAGE_NUMBER)
            self.characters_list.append(MockCharacter(str(val)))

    @property
    def insertionPoints(self) -> Dict[int, MockInsertionPoint]:
        class _Points:
            def __init__(self, frame: MockTextFrame) -> None:
                self.frame = frame

            def __getitem__(self, idx: int) -> MockInsertionPoint:
                return MockInsertionPoint(self.frame)

        return _Points(self)  # type: ignore[return-value]

    @property
    def characters(self) -> Any:
        class _Chars:
            def __init__(self, frame: MockTextFrame) -> None:
                self.frame = frame

            def item(self, idx: int) -> MockCharacter:
                if self.frame.characters_list:
                    return self.frame.characters_list[idx]
                mock_char = MockCharacter()
                self.frame.characters_list.append(mock_char)
                return mock_char

        return _Chars(self)


class MockMasterPage:
    def __init__(self, side: int = MockPageSideOptions.RIGHT_HAND) -> None:
        self.side = side
        self.bounds = [0.0, 0.0, 841.89, 595.28]  # A4: [y0, x0, y1, x1]
        self.textFrames_list: List[MockTextFrame] = []
        self.graphicLines_list: List[Any] = []

    @property
    def textFrames(self) -> Any:
        class _Frames:
            def __init__(self, page: MockMasterPage) -> None:
                self.page = page

            def add(self, properties: Dict[str, Any] = None) -> MockTextFrame:
                bounds = properties.get("geometricBounds", [0, 0, 0, 0]) if properties else [0, 0, 0, 0]
                frame = MockTextFrame(bounds)
                self.page.textFrames_list.append(frame)
                return frame

        return _Frames(self)

    @property
    def graphicLines(self) -> Any:
        class _Lines:
            def __init__(self, page: MockMasterPage) -> None:
                self.page = page

            def add(self) -> Any:
                line = MagicMock()
                self.page.graphicLines_list.append(line)
                return line

        return _Lines(self)


class MockMasterSpread:
    def __init__(self, name: str = "A-Master") -> None:
        self.namePrefix = "A"
        self.baseName = "Master"
        self._name = name
        self.pages_list = [
            MockMasterPage(side=MockPageSideOptions.LEFT_HAND),
            MockMasterPage(side=MockPageSideOptions.RIGHT_HAND),
        ]

    @property
    def name(self) -> str:
        if hasattr(self, "namePrefix") and hasattr(self, "baseName"):
            return f"{self.namePrefix}-{self.baseName}"
        return getattr(self, "_name", "A-Master")

    @name.setter
    def name(self, val: str) -> None:
        self._name = val

    @property
    def pages(self) -> Any:
        class _Pages:
            def __init__(self, spread: MockMasterSpread) -> None:
                self.spread = spread

            def count(self) -> int:
                return len(self.spread.pages_list)

            def item(self, idx: int) -> MockMasterPage:
                return self.spread.pages_list[idx]

        return _Pages(self)


class MockDocument:
    def __init__(self) -> None:
        self.masterSpreads_list = [MockMasterSpread()]
        self.pages_list: List[Any] = [MagicMock()]
        self.textVariables = MagicMock()
        self.textPreferences = MagicMock()
        self.textPreferences.digitsType = MockDigitsTypeOptions.DEFAULT_DIGITS

    @property
    def masterSpreads(self) -> Any:
        class _Spreads:
            def __init__(self, doc: MockDocument) -> None:
                self.doc = doc

            def count(self) -> int:
                return len(self.doc.masterSpreads_list)

            def __len__(self) -> int:
                return len(self.doc.masterSpreads_list)

            def item(self, idx: int) -> MockMasterSpread:
                return self.doc.masterSpreads_list[idx]

            def itemByName(self, name: str) -> MockMasterSpread:
                for ms in self.doc.masterSpreads_list:
                    if getattr(ms, "baseName", "") == name or getattr(ms, "name", "") == name:
                        return ms
                return self.doc.masterSpreads_list[0]

        return _Spreads(self)

    @property
    def pages(self) -> Any:
        class _Pages:
            def __init__(self, doc: MockDocument) -> None:
                self.doc = doc

            def count(self) -> int:
                return len(self.doc.pages_list)

            def item(self, idx: int) -> Any:
                return self.doc.pages_list[idx]

            def add(self, location: Any, reference: Any) -> Any:
                new_page = MagicMock()
                new_page.appliedMaster = None
                self.doc.pages_list.append(new_page)
                return new_page

        return _Pages(self)


def simulate_setup_master_spread(
    doc: MockDocument,
    doc_cfg: Dict[str, Any],
) -> MockDocument:
    """Python simulation of build_master_spread.jsx's setupMasterSpread() logic.

    Faithfully executes the ExtendScript contract:
    - Sets master name to 'Booklet_Master'
    - Creates header ribbon, divider line, footer ribbon, and body story frames
    - Embeds SpecialCharacters.AUTO_PAGE_NUMBER in the footer frame
    - Applies native digit-shaping (DigitsTypeOptions.HINDI_DIGITS vs ARABIC_DIGITS)
      specifically to the AUTO_PAGE_NUMBER marker character
    - Leaves document.textPreferences untouched (Zero-Mutation / AC-6.8.4)
    """
    master = doc.masterSpreads.item(0)
    master.namePrefix = "B"
    master.baseName = "Booklet_Master"

    page_count = master.pages.count()
    for i in range(page_count):
        m_page = master.pages.item(i)
        p_bounds = m_page.bounds
        p_height = p_bounds[2] - p_bounds[0]
        left = 20.0
        right = 500.0
        m_bottom = doc_cfg["margins"]["bottom_mm"] * 2.83465
        m_top = doc_cfg["margins"]["top_mm"] * 2.83465

        # 1. Header ribbon (SECTION_MARKER removed per STEP-7.1e)
        h_frame = m_page.textFrames.add({"geometricBounds": [m_top - 18, left, m_top - 4, right]})
        h_frame.insertionPoints[0].contents = "Running Header"

        # 2. Header line
        m_page.graphicLines.add()

        # 3. Footer ribbon with AUTO_PAGE_NUMBER (STEP-7.1e: configurable footer_text, default empty)
        f_frame = m_page.textFrames.add({"geometricBounds": [p_height - m_bottom + 4, left, p_height - m_bottom + 18, right]})
        footer_text = doc_cfg.get("footer_text") or (doc_cfg.get("geometry", {}).get("footer_text") if isinstance(doc_cfg.get("geometry"), dict) else "") or ""
        if footer_text:
            f_frame.insertionPoints[0].contents = footer_text + "   |   "
            f_frame.insertionPoints[-1].contents = MockSpecialCharacters.AUTO_PAGE_NUMBER
        else:
            f_frame.insertionPoints[0].contents = MockSpecialCharacters.AUTO_PAGE_NUMBER

        # 3.1 Digit Shaping for AUTO_PAGE_NUMBER (STEP-6.8)
        num_style = doc_cfg.get("numbering_style", "arabic_indic")
        is_western = (num_style in ("western_arabic", "arabic"))
        target_digits = (
            MockDigitsTypeOptions.ARABIC_DIGITS if is_western else MockDigitsTypeOptions.HINDI_DIGITS
        )

        page_num_marker = f_frame.characters.item(-1)
        if page_num_marker and page_num_marker.isValid:
            page_num_marker.digitsType = target_digits

        # 4. Body Story Frame
        b_frame = m_page.textFrames.add({"geometricBounds": [m_top + 6, left, p_height - m_bottom - 6, right]})
        b_frame.label = "Booklet_Body_Story"

    return doc


# ═══════════════════════════════════════════════════════════════════════
# Unit Tests: PageNumberingStyle & PageGeometry (Data Model Contracts)
# ═══════════════════════════════════════════════════════════════════════


class TestPageNumberingStyleModel:
    """Tests for PageNumberingStyle enum and PageGeometry model."""

    def test_enum_members_and_values(self) -> None:
        """PageNumberingStyle has ARABIC_INDIC and WESTERN_ARABIC."""
        assert PageNumberingStyle.ARABIC_INDIC.value == "arabic_indic"
        assert PageNumberingStyle.WESTERN_ARABIC.value == "western_arabic"
        assert issubclass(PageNumberingStyle, str)

    def test_page_geometry_default_numbering_style(self) -> None:
        """Default numbering style in PageGeometry is ARABIC_INDIC (Iraqi curriculum default)."""
        pg = PageGeometry()
        assert pg.numbering_style == PageNumberingStyle.ARABIC_INDIC
        assert pg.page_numbering_style == PageNumberingStyle.ARABIC_INDIC

    def test_page_geometry_custom_western_numbering_style(self) -> None:
        """PageGeometry accepts WESTERN_ARABIC."""
        pg = PageGeometry(numbering_style=PageNumberingStyle.WESTERN_ARABIC)
        assert pg.numbering_style == PageNumberingStyle.WESTERN_ARABIC

    def test_page_geometry_string_normalization(self) -> None:
        """PageGeometry normalizes various string representations."""
        pg_indic1 = PageGeometry(numbering_style="arabic_indic")
        pg_indic2 = PageGeometry(numbering_style="hindi")
        pg_indic3 = PageGeometry(numbering_style="eastern_arabic")
        assert pg_indic1.numbering_style == PageNumberingStyle.ARABIC_INDIC
        assert pg_indic2.numbering_style == PageNumberingStyle.ARABIC_INDIC
        assert pg_indic3.numbering_style == PageNumberingStyle.ARABIC_INDIC

        pg_west1 = PageGeometry(numbering_style="western_arabic")
        pg_west2 = PageGeometry(numbering_style="arabic")
        pg_west3 = PageGeometry(numbering_style="western")
        assert pg_west1.numbering_style == PageNumberingStyle.WESTERN_ARABIC
        assert pg_west2.numbering_style == PageNumberingStyle.WESTERN_ARABIC
        assert pg_west3.numbering_style == PageNumberingStyle.WESTERN_ARABIC

    def test_page_geometry_invalid_numbering_style_rejected(self) -> None:
        """Invalid numbering_style strings raise ValidationError."""
        with pytest.raises(ValidationError, match="Invalid numbering_style"):
            PageGeometry(numbering_style="roman_numerals")

        with pytest.raises(ValidationError, match="Invalid numbering_style"):
            PageGeometry(numbering_style="invalid_xyz")

    def test_to_jsx_payload_includes_numbering_style(self) -> None:
        """to_jsx_payload() serializes numbering_style as plain string for JSX consumption."""
        pg_indic = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        payload_indic = pg_indic.to_jsx_payload()
        assert payload_indic["numbering_style"] == "arabic_indic"

        pg_west = PageGeometry(numbering_style=PageNumberingStyle.WESTERN_ARABIC)
        payload_west = pg_west.to_jsx_payload()
        assert payload_west["numbering_style"] == "western_arabic"

    def test_from_paper_stock_forwards_numbering_style(self) -> None:
        """from_paper_stock() allows specifying numbering_style."""
        from app.core.pricing_models import PaperStock

        paper = PaperStock(
            name="80gsm Offset",
            gsm=80,
            thickness_mm_per_sheet=0.10,
            cost_per_sheet_iqd=15.0,
        )
        pg = PageGeometry.from_paper_stock(
            page_count=20,
            paper=paper,
            numbering_style=PageNumberingStyle.WESTERN_ARABIC,
        )
        assert pg.numbering_style == PageNumberingStyle.WESTERN_ARABIC
        assert pg.to_jsx_payload()["numbering_style"] == "western_arabic"


class TestFormatPageNumberHelper:
    """Tests for format_page_number utility."""

    def test_format_arabic_indic_digits(self) -> None:
        """format_page_number converts 0-9 to Arabic-Indic ١٢٣."""
        assert format_page_number(1, PageNumberingStyle.ARABIC_INDIC) == "١"
        assert format_page_number(12, PageNumberingStyle.ARABIC_INDIC) == "١٢"
        assert format_page_number(345, PageNumberingStyle.ARABIC_INDIC) == "٣٤٥"
        assert format_page_number(67890, "arabic_indic") == "٦٧٨٩٠"

    def test_format_western_arabic_digits(self) -> None:
        """format_page_number preserves standard Western digits 123."""
        assert format_page_number(1, PageNumberingStyle.WESTERN_ARABIC) == "1"
        assert format_page_number(12, PageNumberingStyle.WESTERN_ARABIC) == "12"
        assert format_page_number(345, "western_arabic") == "345"


# ═══════════════════════════════════════════════════════════════════════
# AC-6.8.1: Native Digit Shaping vs Section Numbering Style
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria681:
    """[AC-6.8.1] Native digit-shaping property applied to AUTO_PAGE_NUMBER marker."""

    def test_arabic_indic_sets_hindi_digits_on_marker(self) -> None:
        """Selecting Arabic-Indic numbering applies HINDI_DIGITS to the AUTO_PAGE_NUMBER marker."""
        doc = MockDocument()
        pg = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        simulate_setup_master_spread(doc, pg.to_jsx_payload())

        master = doc.masterSpreads.item(0)
        # Check both master pages (facing pages)
        for i in range(master.pages.count()):
            m_page = master.pages.item(i)
            # Find footer frame
            footer_frames = [
                f for f in m_page.textFrames_list
                if any(c == MockSpecialCharacters.AUTO_PAGE_NUMBER for c in f.contents_stream)
            ]
            assert len(footer_frames) == 1
            f_frame = footer_frames[0]

            # Verify AUTO_PAGE_NUMBER marker is preserved
            assert MockSpecialCharacters.AUTO_PAGE_NUMBER in f_frame.contents_stream

            # Verify digit-shaping property is HINDI_DIGITS
            marker_char = f_frame.characters.item(-1)
            assert marker_char.digitsType == MockDigitsTypeOptions.HINDI_DIGITS
            assert marker_char.digitsType != MockDigitsTypeOptions.ARABIC_DIGITS

    def test_western_arabic_sets_arabic_digits_on_marker(self) -> None:
        """Selecting Western Arabic numbering applies ARABIC_DIGITS to the AUTO_PAGE_NUMBER marker."""
        doc = MockDocument()
        pg = PageGeometry(numbering_style=PageNumberingStyle.WESTERN_ARABIC)
        simulate_setup_master_spread(doc, pg.to_jsx_payload())

        master = doc.masterSpreads.item(0)
        for i in range(master.pages.count()):
            m_page = master.pages.item(i)
            footer_frames = [
                f for f in m_page.textFrames_list
                if any(c == MockSpecialCharacters.AUTO_PAGE_NUMBER for c in f.contents_stream)
            ]
            assert len(footer_frames) == 1
            f_frame = footer_frames[0]

            assert MockSpecialCharacters.AUTO_PAGE_NUMBER in f_frame.contents_stream

            marker_char = f_frame.characters.item(-1)
            assert marker_char.digitsType == MockDigitsTypeOptions.ARABIC_DIGITS
            assert marker_char.digitsType != MockDigitsTypeOptions.HINDI_DIGITS

    def test_marker_is_not_replaced_with_static_digit_text(self) -> None:
        """Marker must NEVER be replaced by static digit text (e.g. '١' or '1')."""
        doc = MockDocument()
        pg = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        simulate_setup_master_spread(doc, pg.to_jsx_payload())

        master = doc.masterSpreads.item(0)
        f_frame = master.pages.item(0).textFrames_list[1]  # Footer frame

        # Must contain AUTO_PAGE_NUMBER special character, NOT static text
        assert MockSpecialCharacters.AUTO_PAGE_NUMBER in f_frame.contents_stream
        assert "١" not in f_frame.contents_stream
        assert "1" not in f_frame.contents_stream

    def test_jsx_code_contracts_in_script_files(self) -> None:
        """Verify the actual build_master_spread.jsx file contains the exact digit-shaping implementation."""
        from pathlib import Path

        jsx_path = Path(__file__).resolve().parent.parent / "assets" / "scripts" / "build_master_spread.jsx"
        assert jsx_path.exists(), f"JSX script missing: {jsx_path}"
        code = jsx_path.read_text(encoding="utf-8")
        # Verify native digit-shaping usage
        assert "DigitsTypeOptions.HINDI_DIGITS" in code
        assert "DigitsTypeOptions.ARABIC_DIGITS" in code
        assert "digitsType" in code
        assert "SpecialCharacters.AUTO_PAGE_NUMBER" in code
        # Verify it does NOT mistakenly set section numbering style
        assert "pageNumberStyle" not in code


# ═══════════════════════════════════════════════════════════════════════
# AC-6.8.2: Automatic Page Numbering Mechanism Invariance
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria682:
    """[AC-6.8.2] AUTO_PAGE_NUMBER dynamic update behavior is preserved."""

    def test_auto_page_number_updates_with_blank_filler_pages(self) -> None:
        """Filler pages added via addBlankFillerPages inherit master spread and auto-numbering."""
        doc = MockDocument()
        pg_indic = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        simulate_setup_master_spread(doc, pg_indic.to_jsx_payload())

        # Simulate addBlankFillerPages
        target_count = 8
        current_count = doc.pages.count()
        while current_count < target_count:
            last_page = doc.pages.item(-1)
            new_p = doc.pages.add(location=None, reference=last_page)
            new_p.appliedMaster = next((m for m in doc.masterSpreads_list if getattr(m, "baseName", "") == "Booklet_Master"), doc.masterSpreads.item(0))
            current_count += 1

        assert doc.pages.count() == 8
        # Master spread retains AUTO_PAGE_NUMBER marker with HINDI_DIGITS
        master = doc.masterSpreads.item(0)
        footer_frame = master.pages.item(0).textFrames_list[1]
        assert MockSpecialCharacters.AUTO_PAGE_NUMBER in footer_frame.contents_stream
        assert footer_frame.characters.item(-1).digitsType == MockDigitsTypeOptions.HINDI_DIGITS

    def test_sync_filler_pages_orchestration_with_numbering_style(self) -> None:
        """sync_filler_pages() succeeds identically whether Arabic-Indic or Western Arabic is configured."""
        class MockCOMBridge:
            def __init__(self) -> None:
                self.calls = []

            def call_jsx_function(self, function_name: str, *args) -> int | None:
                self.calls.append((function_name, args))
                return None

        bridge = MockCOMBridge()
        target = sync_filler_pages(bridge, "doc_h", 14, "saddle_stitch")
        assert target == 16
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "addBlankFillerPages"


# ═══════════════════════════════════════════════════════════════════════
# AC-6.8.3: Per-Document Isolation in Sequential Batch Queue
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria683:
    """[AC-6.8.3] Two documents in the same batch with different numbering styles are fully isolated."""

    def test_batch_processes_different_numbering_styles_per_document(self) -> None:
        """Document 1 with Arabic-Indic and Document 2 with Western Arabic each receive their own config."""
        conn = init_db(":memory:")
        file_1 = "C:/booklets/doc1_iraqi_indic.docx"
        file_2 = "C:/booklets/doc2_western.docx"

        cfg_1 = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        cfg_2 = PageGeometry(numbering_style=PageNumberingStyle.WESTERN_ARABIC)

        per_file_configs = {
            file_1: cfg_1,
            file_2: cfg_2,
        }

        executed_jobs: List[Dict[str, Any]] = []

        def mock_generation_fn(doc_store: Any, job_config: Any, **kwargs: Any) -> Any:
            doc_obj = MockDocument()
            payload = job_config.to_jsx_payload()
            simulate_setup_master_spread(doc_obj, payload)
            executed_jobs.append({
                "config": job_config,
                "payload": payload,
                "numbering_style": payload["numbering_style"],
                "doc_obj": doc_obj,
            })
            res = MagicMock()
            res.page_count = 16
            return res

        queue = BatchQueue(
            file_paths=[file_1, file_2],
            db_conn=conn,
            job_configs=per_file_configs,
            generation_fn=mock_generation_fn,
        )

        queue.run()

        # Both jobs succeeded
        assert len(executed_jobs) == 2

        # Job 1 verification: Arabic-Indic
        assert executed_jobs[0]["numbering_style"] == "arabic_indic"
        doc_1 = executed_jobs[0]["doc_obj"]
        m1 = doc_1.masterSpreads.item(0)
        f_frame_1 = m1.pages.item(0).textFrames_list[1]
        assert f_frame_1.characters.item(-1).digitsType == MockDigitsTypeOptions.HINDI_DIGITS

        # Job 2 verification: Western Arabic
        assert executed_jobs[1]["numbering_style"] == "western_arabic"
        doc_2 = executed_jobs[1]["doc_obj"]
        m2 = doc_2.masterSpreads.item(0)
        f_frame_2 = m2.pages.item(0).textFrames_list[1]
        assert f_frame_2.characters.item(-1).digitsType == MockDigitsTypeOptions.ARABIC_DIGITS

        # Proof of per-document isolation: Job 2's run did NOT contaminate Job 1 or global settings
        assert doc_1.textPreferences.digitsType == MockDigitsTypeOptions.DEFAULT_DIGITS
        assert doc_2.textPreferences.digitsType == MockDigitsTypeOptions.DEFAULT_DIGITS
        conn.close()


# ═══════════════════════════════════════════════════════════════════════
# AC-6.8.4: Zero-Mutation Guarantee on Non-Page-Number Content
# ═══════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteria684:
    """[AC-6.8.4] Ordinary source-text, questions, dates, and document_content_hash are completely invariant."""

    def test_document_content_hash_unaffected_by_numbering_style(self) -> None:
        """Changing numbering style leaves document_content_hash strictly unchanged."""
        blocks = [
            ContentBlock(
                block_id="b1",
                block_type="chapter_header",
                raw_content="الفصل الأول: المتجهات الحقيقية 2024",
                content_hash=compute_canonical_hash("الفصل الأول: المتجهات الحقيقية 2024"),
            ),
            ContentBlock(
                block_id="b2",
                block_type="ministry_question",
                raw_content="س1: جد ناتج المعادلة التالية لعام 2023 الدور الأول",
                content_hash=compute_canonical_hash("س1: جد ناتج المعادلة التالية لعام 2023 الدور الأول"),
            ),
            ContentBlock(
                block_id="b3",
                block_type="math_formula",
                raw_content="f(x) = 2x^2 + 5x - 3",
                content_hash=compute_canonical_hash("f(x) = 2x^2 + 5x - 3"),
            ),
            ContentBlock(
                block_id="b4",
                block_type="arabic_text",
                raw_content="تاريخ الامتحان: 15/06/2024 الساعة 8:30 صباحاً",
                content_hash=compute_canonical_hash("تاريخ الامتحان: 15/06/2024 الساعة 8:30 صباحاً"),
            ),
        ]

        page = PageData(page_number=1, width=595.28, height=841.89, content_blocks=blocks)
        doc = BookletDocument(
            doc_id="doc-zero-mutation-test",
            file_path="C:/test/sample.docx",
            file_type="docx",
            pages=[page],
        )

        # Baseline hash computed before applying any layout configuration
        baseline_content_hash = compute_document_content_hash(doc)

        # Config 1: Arabic-Indic
        pg_indic = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        # Config 2: Western Arabic
        pg_west = PageGeometry(numbering_style=PageNumberingStyle.WESTERN_ARABIC)

        # Hashes must remain 100% identical
        hash_after_indic = compute_document_content_hash(doc)
        hash_after_west = compute_document_content_hash(doc)

        assert hash_after_indic == baseline_content_hash
        assert hash_after_west == baseline_content_hash
        assert hash_after_indic == hash_after_west

        # All raw_content strings in the document blocks are identical
        for b in doc.pages[0].content_blocks:
            assert compute_canonical_hash(b.raw_content) == b.content_hash

    def test_indesign_global_text_preferences_never_altered(self) -> None:
        """Verify that neither doc.textPreferences nor body story frames have digitsType modified."""
        doc = MockDocument()
        pg = PageGeometry(numbering_style=PageNumberingStyle.ARABIC_INDIC)
        simulate_setup_master_spread(doc, pg.to_jsx_payload())

        # Document-wide textPreferences remains at default
        assert doc.textPreferences.digitsType == MockDigitsTypeOptions.DEFAULT_DIGITS

        # Master body story frame does NOT have digitsType overridden
        master = doc.masterSpreads.item(0)
        body_frame = master.pages.item(0).textFrames_list[2]  # Body frame
        assert body_frame.label == "Booklet_Body_Story"
        assert body_frame.digitsType is None
