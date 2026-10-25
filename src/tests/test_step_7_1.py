"""
Booklet_AI Desktop — Tests for STEP-7.1 (Real generate_booklet implementation)

Verifies:
1. build_payload() produces a complete UTF-8 JSON payload with ensure_ascii=False,
   proper geometry, styles, and per-block fields (block_type, raw_content, content_hash, user_edited).
2. Fail-Fast text integrity verification halts execution and raises TextIntegrityError
   on any unauthorized block tampering before writing.
3. COMJobRequest picklability under Windows spawn IPC.
4. com_supervisor error handling maps JSX result errors to typed BookletError exceptions.
5. generate_booklet.jsx static contract compliance (NEVER_INTERACT, MAX_PAGES_SAFETY,
   atomic insert pattern, style mapping).
"""

import json
import multiprocessing
import pickle
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.automation.com_supervisor import (
    _find_jsx_script,
    _raise_typed_error,
    _run_add_filler_pages,
    _run_generate_booklet,
)
from app.automation.generation_pipeline import build_payload
from app.automation.geometry import MarginConfig, PageGeometry, PageNumberingStyle
from app.automation.job_protocol import COMJobRequest
from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import (
    BookletError,
    ContentExceedsEstimateError,
    InvalidBodyFrameGeometryError,
    MarginConfigError,
    MissingFontError,
    RTLTableUnsupportedError,
    StyleConfigError,
    StyleMappingError,
    TextIntegrityError,
)
from app.core.models import BookletDocument, ContentBlock, PageData, TableData
from app.core.text_integrity import compute_canonical_hash


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sample_document() -> BookletDocument:
    """Fixture providing a multi-block, multi-page BookletDocument."""
    h_text = "الفصل الأول: الدوال الحقيقية"
    b1 = ContentBlock(
        block_id="b-h1",
        block_type="chapter_header",
        raw_content=h_text,
        content_hash=compute_canonical_hash(h_text),
    )

    p_text = "الدالة هي علاقة تربط كل عنصر من المنطلق بعنصر وحيد من المستقر."
    b2 = ContentBlock(
        block_id="b-p1",
        block_type="arabic_text",
        raw_content=p_text,
        content_hash=compute_canonical_hash(p_text),
    )

    q_text = "سؤال وزاري: بيّن نوع الدالة f(x) = x² + 1 من حيث كونها متباينة أو شاملة."
    b3 = ContentBlock(
        block_id="b-q1",
        block_type="ministry_question",
        raw_content=q_text,
        content_hash=compute_canonical_hash(q_text),
    )

    tbl = TableData(
        table_id="tbl-1",
        headers=["x", "f(x)"],
        rows=[["1", "2"], ["2", "5"]],
    )

    m_text = "f(x) = x^2 + 1"
    b4 = ContentBlock(
        block_id="b-m1",
        block_type="math_formula",
        raw_content=m_text,
        content_hash=compute_canonical_hash(m_text),
    )

    page1 = PageData(
        page_number=1,
        width=595.28,
        height=841.89,
        header_text="الرياضيات",
        content_blocks=[b1, b2],
    )
    page2 = PageData(
        page_number=2,
        width=595.28,
        height=841.89,
        header_text="الرياضيات",
        content_blocks=[b3],
        tables=[tbl],
    )
    page3 = PageData(
        page_number=3,
        width=595.28,
        height=841.89,
        header_text="الرياضيات",
        content_blocks=[b4],
    )

    return BookletDocument(
        doc_id="doc-math-101",
        file_path="C:/docs/math.docx",
        file_type="docx",
        pages=[page1, page2, page3],
    )


@pytest.fixture
def sample_geometry() -> PageGeometry:
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


@pytest.fixture
def sample_styles() -> TypographyStylesConfig:
    return TypographyStylesConfig(
        body_font="Tajawal",
        math_font="Cambria Math",
    )


# ─── 1. Payload Building Tests ─────────────────────────────────────────


class TestPayloadBuilding:
    def test_build_payload_creates_valid_json_utf8(
        self,
        sample_document: BookletDocument,
        sample_geometry: PageGeometry,
        sample_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """build_payload writes valid UTF-8 JSON without escaping Arabic characters."""
        payload_path = build_payload(
            document=sample_document,
            geometry=sample_geometry,
            styles_cfg=sample_styles,
            output_dir=tmp_path,
            binding="saddle_stitch",
        )

        assert payload_path.exists()
        raw_bytes = payload_path.read_bytes()
        # Verify no escaped unicode like \u0627 for standard Arabic
        assert "الفصل الأول" in raw_bytes.decode("utf-8")

        with open(payload_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["doc_id"] == "doc-math-101"
        assert data["binding"] == "saddle_stitch"
        # STEP-7.1b: target_page_count is no longer derived from source document;
        # computed dynamically inside JSX from actual post-injection page count.
        assert "target_page_count" not in data
        assert "geometry" in data
        assert data["geometry"]["margins"]["inside_spine_mm"] == 22.0
        assert data["geometry"]["numbering_style"] == "arabic_indic"
        assert data["styles"]["body_font"] == "Tajawal"
        assert data["styles"]["math_direction"] == "left_to_right"

        # Verify blocks structure
        assert len(data["blocks"]) == 4
        for b in data["blocks"]:
            assert "block_type" in b
            assert "raw_content" in b
            assert "content_hash" in b
            assert "user_edited" in b
            assert b["user_edited"] is False

        # Verify tables structure
        assert len(data["tables"]) == 1
        assert data["tables"][0]["table_id"] == "tbl-1"
        assert data["tables"][0]["headers"] == ["x", "f(x)"]

        # Verify items structure
        assert len(data["items"]) == 5  # 4 blocks + 1 table

    def test_user_edited_block_marked_in_payload(
        self,
        sample_document: BookletDocument,
        sample_geometry: PageGeometry,
        sample_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """User-edited blocks have user_edited=True and updated hash in payload."""
        orig_block = sample_document.pages[0].content_blocks[1]
        edited_block = orig_block.with_user_edit("الدالة هي علاقة رياضية مصححة.")

        new_page1 = sample_document.pages[0].model_copy(
            update={
                "content_blocks": [
                    sample_document.pages[0].content_blocks[0],
                    edited_block,
                ]
            }
        )
        updated_doc = sample_document.model_copy(
            update={"pages": [new_page1, sample_document.pages[1], sample_document.pages[2]]}
        )

        payload_path = build_payload(
            document=updated_doc,
            geometry=sample_geometry,
            styles_cfg=sample_styles,
            output_dir=tmp_path,
        )

        with open(payload_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        edited_in_payload = [b for b in data["blocks"] if b["block_id"] == "b-p1"][0]
        assert edited_in_payload["user_edited"] is True
        assert edited_in_payload["raw_content"] == "الدالة هي علاقة رياضية مصححة."
        assert edited_in_payload["content_hash"] == edited_block.content_hash


# ─── 2. Hash Fail-Fast Verification Tests ──────────────────────────────


class TestTextIntegrityFailFast:
    def test_tampered_content_raises_text_integrity_error(
        self,
        sample_document: BookletDocument,
        sample_geometry: PageGeometry,
        sample_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """Modifying raw_content without updating content_hash raises TextIntegrityError."""
        # Tamper directly with the raw_content of a block
        bad_block = sample_document.pages[0].content_blocks[1].model_copy(
            update={"raw_content": "محتوى تم التلاعب به سراً دون تحديث البصمة"}
        )
        bad_page = sample_document.pages[0].model_copy(
            update={
                "content_blocks": [
                    sample_document.pages[0].content_blocks[0],
                    bad_block,
                ]
            }
        )
        bad_doc = sample_document.model_copy(
            update={"pages": [bad_page, sample_document.pages[1], sample_document.pages[2]]}
        )

        with pytest.raises(TextIntegrityError, match="Text integrity check failed"):
            build_payload(
                document=bad_doc,
                geometry=sample_geometry,
                styles_cfg=sample_styles,
                output_dir=tmp_path,
            )

        # Confirm no payload file was written
        assert not list(tmp_path.glob("*.json"))

    def test_diacritic_tampering_raises_text_integrity_error(
        self,
        sample_document: BookletDocument,
        sample_geometry: PageGeometry,
        sample_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """Tampering with a single diacritic raises TextIntegrityError."""
        orig = sample_document.pages[0].content_blocks[0]  # "الفصل الأول: الدوال الحقيقية"
        # Add a fatha
        tampered_text = "الفَصْل الأول: الدوال الحقيقية"
        bad_block = orig.model_copy(update={"raw_content": tampered_text})
        bad_page = sample_document.pages[0].model_copy(update={"content_blocks": [bad_block]})
        bad_doc = sample_document.model_copy(update={"pages": [bad_page]})

        with pytest.raises(TextIntegrityError):
            build_payload(
                document=bad_doc,
                geometry=sample_geometry,
                styles_cfg=sample_styles,
                output_dir=tmp_path,
            )


# ─── 3. COMJobRequest Picklability Tests ───────────────────────────────


class TestCOMJobRequestPicklability:
    def test_job_request_pickle_roundtrip(self, tmp_path: Path):
        """COMJobRequest round-trips cleanly through pickle across process boundary."""
        payload_file = str(tmp_path / "test_payload.json")
        req = COMJobRequest(
            job_command="generate_booklet",
            payload_file_path=payload_file,
            preflight_output_path=str(tmp_path / "preflight.json"),
        )

        pickled = pickle.dumps(req)
        restored = pickle.loads(pickled)

        assert isinstance(restored, COMJobRequest)
        assert restored.job_command == "generate_booklet"
        assert restored.payload_file_path == payload_file
        assert restored.preflight_output_path == str(tmp_path / "preflight.json")

    def test_job_request_is_frozen(self):
        """COMJobRequest is immutable."""
        req = COMJobRequest(
            job_command="generate_booklet",
            payload_file_path="C:/payload.json",
        )
        with pytest.raises(Exception):
            req.job_command = "add_filler_pages"  # type: ignore[misc]


# ─── 4. Supervisor Execution & Typed Error Mapping Tests ───────────────


class TestSupervisorExecutionAndErrors:
    def test_raise_typed_error_mapping(self):
        """_raise_typed_error correctly maps error substrings to typed exceptions."""
        with pytest.raises(MissingFontError):
            _raise_typed_error("MissingFontError: required font 'Tajawal' is not installed")

        with pytest.raises(StyleConfigError):
            _raise_typed_error("StyleConfigError: BodyArabicStyle.composer failed: cannot assign")

        with pytest.raises(TextIntegrityError):
            _raise_typed_error("TextIntegrityError: hash mismatch")

        with pytest.raises(StyleMappingError):
            _raise_typed_error("StyleMappingError: no InDesign style registered")

        with pytest.raises(ContentExceedsEstimateError):
            _raise_typed_error("ContentExceedsEstimateError: actual 6 > target 4")

        with pytest.raises(InvalidBodyFrameGeometryError):
            _raise_typed_error("InvalidBodyFrameGeometryError: negative height")

        with pytest.raises(MarginConfigError):
            _raise_typed_error("MarginConfigError: missing required field")

        with pytest.raises(RTLTableUnsupportedError):
            _raise_typed_error("RTLTableUnsupportedError: World-Ready build required")

        with pytest.raises(BookletError):
            _raise_typed_error("SomeGenericError: something else failed")

    def test_run_generate_booklet_success(self, tmp_path: Path, monkeypatch):
        """_run_generate_booklet executes DoScript and updates heartbeat."""
        payload_file = tmp_path / "test_payload.json"
        result_file = tmp_path / "test_result.json"

        payload_data = {
            "doc_id": "test",
            "result_file_path": str(result_file),
        }
        payload_file.write_text(json.dumps(payload_data), encoding="utf-8")

        # Mock app.DoScript to write the success result file
        mock_app = MagicMock()

        def fake_doscript(*args, **kwargs):
            result_file.write_text(
                json.dumps({
                    "status": "ok",
                    "page_count": 4,
                    "warnings": [],
                }),
                encoding="utf-8",
            )

        mock_app.DoScript.side_effect = fake_doscript
        heartbeat = multiprocessing.Value("d", 0.0)

        _run_generate_booklet(mock_app, str(payload_file), heartbeat)

        assert heartbeat.value > 0.0
        assert mock_app.DoScript.called
        assert mock_app.ScriptArgs.SetValue.called

    def test_run_generate_booklet_raises_typed_error_from_result(
        self, tmp_path: Path
    ):
        """_run_generate_booklet parses error result JSON and raises typed exception."""
        payload_file = tmp_path / "test_payload.json"
        result_file = tmp_path / "test_result.json"

        payload_data = {
            "doc_id": "test",
            "result_file_path": str(result_file),
        }
        payload_file.write_text(json.dumps(payload_data), encoding="utf-8")

        mock_app = MagicMock()

        def fake_doscript_fail(*args, **kwargs):
            result_file.write_text(
                json.dumps({
                    "status": "error",
                    "error": "StyleMappingError: paragraph style 'CustomStyle' not found",
                }),
                encoding="utf-8",
            )

        mock_app.DoScript.side_effect = fake_doscript_fail
        heartbeat = multiprocessing.Value("d", 0.0)

        with pytest.raises(StyleMappingError, match="CustomStyle"):
            _run_generate_booklet(mock_app, str(payload_file), heartbeat)

    def test_run_add_filler_pages_success(self, tmp_path: Path):
        """_run_add_filler_pages executes ExtendScript and updates heartbeat."""
        payload_file = tmp_path / "test_filler_payload.json"
        result_file = tmp_path / "test_filler_result.json"

        payload_data = {
            "doc_id": "filler_test",
            "target_page_count": 8,
            "master_name": "Booklet_Master",
            "result_file_path": str(result_file),
        }
        payload_file.write_text(json.dumps(payload_data), encoding="utf-8")

        mock_app = MagicMock()

        def fake_doscript(*args, **kwargs):
            result_file.write_text(
                json.dumps({"status": "ok", "page_count": 8}),
                encoding="utf-8",
            )

        mock_app.DoScript.side_effect = fake_doscript
        heartbeat = multiprocessing.Value("d", 0.0)

        _run_add_filler_pages(mock_app, str(payload_file), heartbeat)

        assert heartbeat.value > 0.0
        assert mock_app.DoScript.called


# ─── 5. JSX Script Static Contract Tests ───────────────────────────────


class TestJSXScriptStaticContract:
    def test_generate_booklet_jsx_exists_and_contains_contracts(self):
        """Verify generate_booklet.jsx has all required architectural contracts."""
        jsx_path = _find_jsx_script("generate_booklet.jsx")
        assert jsx_path.exists()
        content = jsx_path.read_text(encoding="utf-8")

        # 1. Include json2.jsx via relative path
        assert '#include "lib/json2.jsx"' in content

        # 2. Modal dialog suppression
        assert "UserInteractionLevels.NEVER_INTERACT" in content

        # 3. Plan STEP-1.1 functions
        assert "function findMasterFrameByLabelOnSide" in content
        assert "function ensureFrameCapacity" in content
        assert "Booklet_Body_Story" in content
        assert "MAX_PAGES_SAFETY = 500" in content

        # 4. Block injection subroutines
        assert "injectMathFormulaBlock" in content
        assert "injectRTLTable" in content
        assert "resolveAndApplyStyle" in content
        assert "addBlankFillerPages" in content

        # 5. Styles creation
        assert "ChapterHeaderStyle" in content
        assert "BodyArabicStyle" in content
        assert "MinistryQuestionStyle" in content
        assert "MathFormulaStyle" in content

        # 6. STEP-7.1b Review Fixes
        # RulerOrigin page relative
        assert "doc.viewPreferences.rulerOrigin = RulerOrigin.PAGE_ORIGIN" in content
        # World-Ready Composer
        assert "Adobe World-Ready Paragraph Composer" in content
        # Non-silent failures: MissingFontError & StyleConfigError
        assert "MissingFontError" in content
        assert "StyleConfigError" in content
        # requireMarginField with no fallbacks
        assert "requireMarginField(geometry.margins, \"inside_spine_mm\")" in content
        assert "requireMarginField(geometry.margins, \"outside_mm\")" in content
        # Dynamic imposition from actual page count & unified multiples
        assert "IMPOSITION_MULTIPLE_MAP" in content
        assert '"saddle_stitch": 4' in content
        assert '"perfect_bound": 1' in content
        assert '"spiral":        1' in content
        assert '"perfect_bind"' not in content
        assert '"wire"' not in content
        assert "doc.close(SaveOptions.NO)" in content
        assert "composer_ok" in content
        assert "actual_page_count" in content
        assert "imposed_page_count" in content

        # 7. Result JSON writing
        assert "writeResultToFile" in content
