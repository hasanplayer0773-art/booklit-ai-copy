"""
Booklet_AI Desktop — STEP-2.3 Tests

Tests for Typography & Ministerial Question Callout Matrix:
- Style dispatch table completeness and resolve_style_name()
- Fail-Fast on unmapped/unknown block types (AC-2.3.2)
- Template preflight payload generation and result validation (AC-2.3.4)
- Style resolution covers every ContentBlock (AC-2.3.1)
- Legacy equation placeholder mapping (AC-2.3.6)
"""

import json
import tempfile
from pathlib import Path

import pytest

from app.automation.style_dispatch import (
    BLOCK_STYLE_MAP,
    get_all_required_style_names,
    resolve_style_name,
)
from app.automation.template_preflight import (
    build_preflight_payload,
    check_preflight_result,
)
from app.core.exceptions import BookletError, StyleMappingError
from app.core.models import BlockType, ContentBlock


# ─── Helpers ───────────────────────────────────────────────────────────

def _all_block_types() -> list[str]:
    """Extract all valid BlockType literal values from the type annotation."""
    return list(BlockType.__args__)


def _make_block(block_type: str, content: str = "test") -> ContentBlock:
    """Create a ContentBlock with minimal required fields."""
    from app.core.text_integrity import compute_canonical_hash

    return ContentBlock(
        block_id=f"test-{block_type}",
        block_type=block_type,
        raw_content=content,
        content_hash=compute_canonical_hash(content),
    )


# ═══════════════════════════════════════════════════════════════════════
# AC-2.3.1: Every ContentBlock resolves to exactly one paragraph style
# ═══════════════════════════════════════════════════════════════════════


class TestStyleDispatchCompleteness:
    """Verify that every BlockType in the taxonomy has a mapping."""

    def test_every_block_type_has_a_mapping(self) -> None:
        """Every value in the BlockType Literal union must appear as a
        key in BLOCK_STYLE_MAP. If a new block_type is added to models.py
        without a corresponding style entry, this test fails."""
        all_types = _all_block_types()
        for bt in all_types:
            assert bt in BLOCK_STYLE_MAP, (
                f"BlockType '{bt}' has no entry in BLOCK_STYLE_MAP. "
                f"Add a mapping in style_dispatch.py."
            )

    def test_resolve_returns_string_for_every_block_type(self) -> None:
        """resolve_style_name() must return a non-empty string for
        every valid block type (AC-2.3.1)."""
        for bt in _all_block_types():
            style_name = resolve_style_name(bt)
            assert isinstance(style_name, str)
            assert len(style_name) > 0

    def test_no_duplicate_style_names(self) -> None:
        """Each block type must map to a unique style name — two
        different block types sharing a style would make styling
        ambiguous."""
        style_names = list(BLOCK_STYLE_MAP.values())
        assert len(style_names) == len(set(style_names)), (
            f"Duplicate style names found in BLOCK_STYLE_MAP: "
            f"{[s for s in style_names if style_names.count(s) > 1]}"
        )

    def test_content_block_resolves_to_exactly_one_style(self) -> None:
        """A ContentBlock's block_type resolves to exactly one style
        (AC-2.3.1) — not zero, not multiple."""
        for bt in _all_block_types():
            block = _make_block(bt)
            result = resolve_style_name(block.block_type)
            assert result == BLOCK_STYLE_MAP[bt]


# ═══════════════════════════════════════════════════════════════════════
# AC-2.3.2: Unmapped block_type raises StyleMappingError immediately
# ═══════════════════════════════════════════════════════════════════════


class TestStyleMappingFailFast:
    """Verify Fail-Fast behavior for unmapped block types."""

    def test_unmapped_block_type_raises_style_mapping_error(self) -> None:
        """An unmapped block_type must raise StyleMappingError
        immediately, never silently fallback to body style (AC-2.3.2)."""
        with pytest.raises(StyleMappingError) as exc_info:
            resolve_style_name("totally_nonexistent_type")  # type: ignore[arg-type]
        assert "totally_nonexistent_type" in str(exc_info.value)

    def test_style_mapping_error_is_booklet_error(self) -> None:
        """StyleMappingError must be a subclass of BookletError for
        centralized error handling."""
        assert issubclass(StyleMappingError, BookletError)

    def test_empty_string_raises_style_mapping_error(self) -> None:
        """Empty string block_type must not silently match anything."""
        with pytest.raises(StyleMappingError):
            resolve_style_name("")  # type: ignore[arg-type]

    def test_none_raises_style_mapping_error(self) -> None:
        """None block_type must not silently match anything."""
        with pytest.raises(StyleMappingError):
            resolve_style_name(None)  # type: ignore[arg-type]

    def test_case_sensitive_resolution(self) -> None:
        """Style resolution must be case-sensitive — 'Arabic_Text' is
        not the same as 'arabic_text'."""
        with pytest.raises(StyleMappingError):
            resolve_style_name("Arabic_Text")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# AC-2.3.3 / AC-2.3.5: Ministry question callout (Python-side contracts)
# ═══════════════════════════════════════════════════════════════════════


class TestMinistryQuestionMapping:
    """Verify that ministry_question blocks map correctly."""

    def test_ministry_question_maps_to_style(self) -> None:
        """ministry_question must map to MinistryQuestionStyle."""
        assert resolve_style_name("ministry_question") == "MinistryQuestionStyle"

    def test_ministry_question_is_in_block_style_map(self) -> None:
        """ministry_question must have an explicit entry."""
        assert "ministry_question" in BLOCK_STYLE_MAP


# ═══════════════════════════════════════════════════════════════════════
# AC-2.3.4: Template precondition check (style existence validation)
# ═══════════════════════════════════════════════════════════════════════


class TestTemplatePreflight:
    """Verify the template preflight payload and result validation."""

    def test_preflight_payload_contains_all_styles(self) -> None:
        """build_preflight_payload() must include every style name
        from BLOCK_STYLE_MAP."""
        payload = build_preflight_payload()
        required = payload["required_styles"]
        all_styles = get_all_required_style_names()
        assert set(required) == all_styles

    def test_preflight_payload_writes_to_file(self, tmp_path: Path) -> None:
        """build_preflight_payload() with an output_path must create
        a valid JSON file."""
        out = tmp_path / "preflight_payload.json"
        payload = build_preflight_payload(output_path=out)
        assert out.exists()
        with open(out, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == payload

    def test_preflight_ok_result_passes(self, tmp_path: Path) -> None:
        """A result with status='ok' must not raise."""
        result_file = tmp_path / "result.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"status": "ok"}, f)
        # Should not raise
        check_preflight_result(result_file)

    def test_preflight_missing_styles_raises_error(self, tmp_path: Path) -> None:
        """A result with missing styles must raise StyleMappingError
        (AC-2.3.4)."""
        result_file = tmp_path / "result.json"
        missing = ["ChapterHeaderStyle", "BodyArabicStyle"]
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"status": "error", "missing_styles": missing}, f)

        with pytest.raises(StyleMappingError) as exc_info:
            check_preflight_result(result_file)
        assert "ChapterHeaderStyle" in str(exc_info.value)
        assert "BodyArabicStyle" in str(exc_info.value)

    def test_preflight_missing_file_raises_error(self, tmp_path: Path) -> None:
        """A missing result file must raise FileNotFoundError."""
        fake_path = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            check_preflight_result(fake_path)

    def test_preflight_malformed_result_raises_error(self, tmp_path: Path) -> None:
        """A malformed result file must raise ValueError."""
        result_file = tmp_path / "result.json"
        with open(result_file, "w", encoding="utf-8") as f:
            f.write('"just a string"')

        with pytest.raises(ValueError):
            check_preflight_result(result_file)


# ═══════════════════════════════════════════════════════════════════════
# AC-2.3.6: legacy_equation_placeholder mapping
# ═══════════════════════════════════════════════════════════════════════


class TestLegacyEquationPlaceholder:
    """Verify that legacy_equation_placeholder blocks are handled."""

    def test_legacy_maps_to_distinct_style(self) -> None:
        """legacy_equation_placeholder must map to
        LegacyEquationPlaceholderStyle (AC-2.3.6)."""
        result = resolve_style_name("legacy_equation_placeholder")
        assert result == "LegacyEquationPlaceholderStyle"

    def test_legacy_style_is_distinct_from_body(self) -> None:
        """LegacyEquationPlaceholderStyle must not be the same as
        BodyArabicStyle — it must be visually distinct so the designer
        cannot miss the gap (AC-2.3.6)."""
        legacy_style = resolve_style_name("legacy_equation_placeholder")
        body_style = resolve_style_name("arabic_text")
        assert legacy_style != body_style

    def test_legacy_never_raises_style_mapping_error(self) -> None:
        """legacy_equation_placeholder must resolve without error
        (AC-2.3.6)."""
        # Should not raise
        resolve_style_name("legacy_equation_placeholder")

    def test_legacy_in_required_styles(self) -> None:
        """LegacyEquationPlaceholderStyle must be in the set of
        required template styles (for precondition check AC-2.3.4)."""
        required = get_all_required_style_names()
        assert "LegacyEquationPlaceholderStyle" in required


# ═══════════════════════════════════════════════════════════════════════
# get_all_required_style_names() utility
# ═══════════════════════════════════════════════════════════════════════


class TestGetAllRequiredStyleNames:
    """Verify the utility function for collecting all style names."""

    def test_returns_frozen_set(self) -> None:
        """Must return a frozenset (immutable)."""
        result = get_all_required_style_names()
        assert isinstance(result, frozenset)

    def test_count_matches_block_style_map(self) -> None:
        """The number of unique style names must match the number
        of entries in BLOCK_STYLE_MAP."""
        result = get_all_required_style_names()
        assert len(result) == len(BLOCK_STYLE_MAP)

    def test_every_style_is_a_non_empty_string(self) -> None:
        """Every style name must be a non-empty string."""
        for name in get_all_required_style_names():
            assert isinstance(name, str)
            assert len(name) > 0


# ═══════════════════════════════════════════════════════════════════════
# Specific style name verification
# ═══════════════════════════════════════════════════════════════════════


class TestSpecificStyleNames:
    """Verify each block type maps to the expected style name per plan."""

    @pytest.mark.parametrize(
        "block_type,expected_style",
        [
            ("chapter_header", "ChapterHeaderStyle"),
            ("arabic_text", "BodyArabicStyle"),
            ("english_text", "BodyLatinStyle"),
            ("ministry_question", "MinistryQuestionStyle"),
            ("math_formula", "MathFormulaStyle"),
            ("chemistry_formula", "ChemistryFormulaStyle"),
            ("image_prompt", "ImagePlaceholderStyle"),
            ("list_item", "ListItemStyle"),
            ("legacy_equation_placeholder", "LegacyEquationPlaceholderStyle"),
        ],
    )
    def test_block_type_maps_to_expected_style(
        self, block_type: str, expected_style: str
    ) -> None:
        """Each block type must map to its expected style name
        as defined in the STEP-2.3 plan."""
        assert resolve_style_name(block_type) == expected_style
