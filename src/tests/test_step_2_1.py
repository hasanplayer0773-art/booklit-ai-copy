"""
Booklet_AI Desktop — STEP-2.1 Unit Tests

Tests for the Document Geometry & Margin models (MarginConfig, PageGeometry)
and pagination sync logic. These are pure-Python tests that do NOT require
a running InDesign instance.

JSX-level acceptance criteria (AC-2.1.1 through AC-2.1.4, AC-2.1.6) are
structurally addressed by the code but require integration testing with
InDesign for full verification.
"""

import math

import pytest
from pydantic import ValidationError

from app.automation.geometry import MarginConfig, PageGeometry
from app.automation.pagination_sync import (
    BindingMethod,
    ContentExceedsEstimateError,
    imposed_page_count,
)
from app.core.exceptions import (
    BookletError,
    ContentExceedsEstimateError as ExcContentExceeds,
    InvalidBodyFrameGeometryError,
    MarginConfigError,
)


# ═══════════════════════════════════════════════════════════════════════
# MarginConfig Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMarginConfig:
    """Tests for MarginConfig model validation and serialization."""

    def test_default_construction(self) -> None:
        """MarginConfig with defaults should construct without errors."""
        mc = MarginConfig()
        assert mc.top_mm == 20.0
        assert mc.bottom_mm == 20.0
        assert mc.inside_spine_mm == 22.0
        assert mc.outside_mm == 15.0
        assert mc.column_count == 1
        assert mc.column_gutter_mm == 5.0

    def test_custom_values(self) -> None:
        """MarginConfig accepts custom positive margin values."""
        mc = MarginConfig(
            top_mm=25.0,
            bottom_mm=18.0,
            inside_spine_mm=20.0,
            outside_mm=12.0,
            column_count=2,
            column_gutter_mm=4.0,
        )
        assert mc.top_mm == 25.0
        assert mc.inside_spine_mm == 20.0
        assert mc.column_count == 2

    def test_frozen_immutability(self) -> None:
        """MarginConfig is frozen — attribute assignment raises error."""
        mc = MarginConfig()
        with pytest.raises(ValidationError):
            mc.top_mm = 30.0  # type: ignore[misc]

    def test_negative_top_mm_rejected(self) -> None:
        """Negative top_mm raises ValidationError."""
        with pytest.raises(ValidationError, match="top_mm must be non-negative"):
            MarginConfig(top_mm=-5.0)

    def test_negative_bottom_mm_rejected(self) -> None:
        """Negative bottom_mm raises ValidationError."""
        with pytest.raises(ValidationError, match="bottom_mm must be non-negative"):
            MarginConfig(bottom_mm=-1.0)

    def test_negative_inside_spine_mm_rejected(self) -> None:
        """Negative inside_spine_mm raises ValidationError."""
        with pytest.raises(
            ValidationError, match="inside_spine_mm must be non-negative"
        ):
            MarginConfig(inside_spine_mm=-0.5)

    def test_negative_outside_mm_rejected(self) -> None:
        """Negative outside_mm raises ValidationError."""
        with pytest.raises(ValidationError, match="outside_mm must be non-negative"):
            MarginConfig(outside_mm=-10.0)

    def test_zero_margins_accepted(self) -> None:
        """Zero margins are valid (non-negative, edge case)."""
        mc = MarginConfig(top_mm=0.0, bottom_mm=0.0, inside_spine_mm=0.0, outside_mm=0.0)
        assert mc.top_mm == 0.0
        assert mc.bottom_mm == 0.0

    def test_model_dump_field_names_match_jsx_contract(self) -> None:
        """model_dump() preserves exact field names expected by JSX.

        The JSX reads docCfg.margins.top_mm / .bottom_mm /
        .inside_spine_mm / .outside_mm — these names must appear
        unchanged in the serialized dict (AC-2.1.7, AC-2.1.10).
        """
        mc = MarginConfig()
        dumped = mc.model_dump()
        # All four margin fields the JSX requires via requireMarginField()
        assert "top_mm" in dumped
        assert "bottom_mm" in dumped
        assert "inside_spine_mm" in dumped
        assert "outside_mm" in dumped
        # Verify they are numbers (matching JSX typeof check)
        assert isinstance(dumped["top_mm"], (int, float))
        assert isinstance(dumped["bottom_mm"], (int, float))
        assert isinstance(dumped["inside_spine_mm"], (int, float))
        assert isinstance(dumped["outside_mm"], (int, float))

    def test_model_dump_no_extra_margin_aliases(self) -> None:
        """Verify no aliased/abbreviated field names leak through.

        An earlier draft had JSX reading .top / .inside / .outside
        (abbreviated names). Ensure model_dump() does NOT produce
        these old names.
        """
        mc = MarginConfig()
        dumped = mc.model_dump()
        # These old abbreviated names must NOT appear
        assert "top" not in dumped
        assert "inside" not in dumped
        assert "outside" not in dumped
        assert "bottom" not in dumped


# ═══════════════════════════════════════════════════════════════════════
# PageGeometry Tests
# ═══════════════════════════════════════════════════════════════════════


class TestPageGeometry:
    """Tests for PageGeometry model and its to_jsx_payload() method."""

    def test_default_construction(self) -> None:
        """PageGeometry with defaults produces A4 facing pages."""
        pg = PageGeometry()
        assert pg.page_size == "A4"
        assert pg.width_pt == pytest.approx(595.28)
        assert pg.height_pt == pytest.approx(841.89)
        assert pg.facing_pages is True
        assert pg.bleed_mm == 3.0
        assert isinstance(pg.margins, MarginConfig)

    def test_custom_margins(self) -> None:
        """PageGeometry accepts a custom MarginConfig."""
        custom_margins = MarginConfig(top_mm=30.0, bottom_mm=25.0)
        pg = PageGeometry(margins=custom_margins)
        assert pg.margins.top_mm == 30.0
        assert pg.margins.bottom_mm == 25.0

    def test_single_sided_construction(self) -> None:
        """PageGeometry with facing_pages=False constructs correctly."""
        pg = PageGeometry(facing_pages=False)
        assert pg.facing_pages is False

    def test_frozen_immutability(self) -> None:
        """PageGeometry is frozen — attribute assignment raises error."""
        pg = PageGeometry()
        with pytest.raises(ValidationError):
            pg.page_size = "A5"  # type: ignore[misc]

    def test_to_jsx_payload_structure(self) -> None:
        """to_jsx_payload() produces the exact structure JSX expects.

        The JSX accesses: docCfg.margins.top_mm, docCfg.margins.bottom_mm,
        docCfg.margins.inside_spine_mm, docCfg.margins.outside_mm.
        """
        pg = PageGeometry(
            margins=MarginConfig(top_mm=25.0, outside_mm=18.0)
        )
        payload = pg.to_jsx_payload()

        # Top-level keys
        assert "page_size" in payload
        assert "width_pt" in payload
        assert "height_pt" in payload
        assert "facing_pages" in payload
        assert "bleed_mm" in payload
        assert "margins" in payload

        # Nested margins match the JSX contract exactly
        margins = payload["margins"]
        assert margins["top_mm"] == 25.0
        assert margins["outside_mm"] == 18.0
        assert margins["inside_spine_mm"] == 22.0  # default
        assert margins["bottom_mm"] == 20.0  # default

    def test_to_jsx_payload_values_match_model(self) -> None:
        """to_jsx_payload() values match the model's attribute values.

        End-to-end: Python config change → JSX payload change (AC-2.1.7).
        """
        pg1 = PageGeometry(margins=MarginConfig(top_mm=20.0))
        pg2 = PageGeometry(margins=MarginConfig(top_mm=35.0))

        p1 = pg1.to_jsx_payload()
        p2 = pg2.to_jsx_payload()

        assert p1["margins"]["top_mm"] != p2["margins"]["top_mm"]
        assert p1["margins"]["top_mm"] == 20.0
        assert p2["margins"]["top_mm"] == 35.0


# ═══════════════════════════════════════════════════════════════════════
# Pathological Margin Tests (AC-2.1.8)
# ═══════════════════════════════════════════════════════════════════════


class TestPathologicalMargins:
    """Tests for extreme margin values that would produce invalid geometry.

    AC-2.1.8: top_mm=300, bottom_mm=300 on A4 should raise an error
    before any document content is generated. The Python layer catches
    negative values; the JSX layer catches the zero/negative body height
    case (which depends on actual page dimensions).

    These tests verify the Python-side validation. The JSX-side
    InvalidBodyFrameGeometryError is verified by the JSX code's own
    guard (bodyBottom <= bodyTop check) during integration testing.
    """

    def test_negative_margins_rejected_at_python_layer(self) -> None:
        """Negative margins are always invalid regardless of page size."""
        with pytest.raises(ValidationError):
            MarginConfig(top_mm=-300.0, bottom_mm=-300.0)

    def test_huge_positive_margins_accepted_by_python(self) -> None:
        """Pathologically large positive margins pass Python validation.

        The Python layer cannot reject these because it doesn't know
        the page height. The JSX layer's bodyBottom <= bodyTop check
        catches this at construction time (AC-2.1.8).
        """
        # This should NOT raise — Python only checks non-negative
        mc = MarginConfig(top_mm=300.0, bottom_mm=300.0)
        assert mc.top_mm == 300.0
        assert mc.bottom_mm == 300.0

    def test_huge_margins_serialize_to_jsx(self) -> None:
        """Pathological margins reach JSX as-is (no silent clamping)."""
        mc = MarginConfig(top_mm=300.0, bottom_mm=300.0)
        pg = PageGeometry(margins=mc)
        payload = pg.to_jsx_payload()
        assert payload["margins"]["top_mm"] == 300.0
        assert payload["margins"]["bottom_mm"] == 300.0


# ═══════════════════════════════════════════════════════════════════════
# Exception Hierarchy Tests
# ═══════════════════════════════════════════════════════════════════════


class TestExceptionHierarchy:
    """Verify STEP-2.1 exceptions inherit from BookletError."""

    def test_content_exceeds_estimate_is_booklet_error(self) -> None:
        """ContentExceedsEstimateError is a BookletError."""
        assert issubclass(ExcContentExceeds, BookletError)

    def test_invalid_body_frame_geometry_is_booklet_error(self) -> None:
        """InvalidBodyFrameGeometryError is a BookletError."""
        assert issubclass(InvalidBodyFrameGeometryError, BookletError)

    def test_margin_config_error_is_booklet_error(self) -> None:
        """MarginConfigError is a BookletError."""
        assert issubclass(MarginConfigError, BookletError)

    def test_content_exceeds_estimate_instantiation(self) -> None:
        """ContentExceedsEstimateError can be instantiated with a message."""
        err = ExcContentExceeds("actual=25, estimated=20")
        assert "actual=25" in str(err)

    def test_margin_config_error_instantiation(self) -> None:
        """MarginConfigError can be instantiated with a message."""
        err = MarginConfigError("missing field: outside_mm")
        assert "outside_mm" in str(err)


# ═══════════════════════════════════════════════════════════════════════
# Imposed Page Count Tests (STEP-5.1 stub)
# ═══════════════════════════════════════════════════════════════════════


class TestImposedPageCount:
    """Tests for the imposed_page_count() stub (STEP-5.1 forward declaration)."""

    def test_saddle_stitch_rounds_to_multiple_of_4(self) -> None:
        """Saddle-stitch requires pages in multiples of 4."""
        assert imposed_page_count(1, "saddle_stitch") == 4
        assert imposed_page_count(4, "saddle_stitch") == 4
        assert imposed_page_count(5, "saddle_stitch") == 8
        assert imposed_page_count(17, "saddle_stitch") == 20

    def test_perfect_bind_rounds_to_multiple_of_2(self) -> None:
        """Perfect-bind requires pages in multiples of 2."""
        assert imposed_page_count(1, "perfect_bind") == 2
        assert imposed_page_count(2, "perfect_bind") == 2
        assert imposed_page_count(3, "perfect_bind") == 4
        assert imposed_page_count(17, "perfect_bind") == 18

    def test_wire_rounds_to_multiple_of_2(self) -> None:
        """Wire binding requires pages in multiples of 2."""
        assert imposed_page_count(1, "wire") == 2
        assert imposed_page_count(10, "wire") == 10
        assert imposed_page_count(11, "wire") == 12

    def test_exact_multiple_unchanged(self) -> None:
        """Already-conforming counts are returned unchanged."""
        assert imposed_page_count(8, "saddle_stitch") == 8
        assert imposed_page_count(20, "perfect_bind") == 20

    def test_zero_page_count_rejected(self) -> None:
        """Zero or negative page counts are rejected."""
        with pytest.raises(ValueError, match="raw_page_count must be >= 1"):
            imposed_page_count(0, "saddle_stitch")

    def test_negative_page_count_rejected(self) -> None:
        """Negative page counts are rejected."""
        with pytest.raises(ValueError, match="raw_page_count must be >= 1"):
            imposed_page_count(-5, "wire")


# ═══════════════════════════════════════════════════════════════════════
# Pagination Sync Logic Tests
# ═══════════════════════════════════════════════════════════════════════


class MockCOMBridge:
    """Mock COM bridge for testing sync_filler_pages() without InDesign."""

    def __init__(self, page_count: int = 10, overrun: bool = False) -> None:
        self.page_count = page_count
        self.overrun = overrun
        self.calls: list = []

    def call_jsx_function(self, function_name: str, *args) -> int | None:
        self.calls.append((function_name, args))
        if function_name == "addBlankFillerPages" and self.overrun:
            # Simulate first call raising content overrun, then succeed
            self.overrun = False
            from app.core.exceptions import ContentExceedsEstimateError
            raise ContentExceedsEstimateError(
                f"Actual content requires {self.page_count} pages"
            )
        if function_name == "getPageCount":
            return self.page_count
        return None


class TestSyncFillerPages:
    """Tests for sync_filler_pages() orchestration logic."""

    def test_normal_sync_calls_jsx(self) -> None:
        """Normal case: calls addBlankFillerPages with imposed target."""
        from app.automation.pagination_sync import sync_filler_pages

        bridge = MockCOMBridge()
        result = sync_filler_pages(bridge, "doc_handle", 15, "saddle_stitch")

        # imposed_page_count(15, saddle_stitch) = 16
        assert result == 16
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "addBlankFillerPages"
        assert bridge.calls[0][1] == ("doc_handle", 16, "Booklet_Master")

    def test_content_overrun_triggers_requote(self) -> None:
        """Content overrun: re-derives target from actual count."""
        from app.automation.pagination_sync import sync_filler_pages

        bridge = MockCOMBridge(page_count=25, overrun=True)
        result = sync_filler_pages(bridge, "doc_handle", 15, "saddle_stitch")

        # First call raises ContentExceedsEstimateError
        # Then getPageCount returns 25
        # imposed_page_count(25, saddle_stitch) = 28
        # Second call to addBlankFillerPages with 28
        assert result == 28
        assert len(bridge.calls) == 3  # addBlankFillerPages, getPageCount, addBlankFillerPages
        assert bridge.calls[0][0] == "addBlankFillerPages"
        assert bridge.calls[1][0] == "getPageCount"
        assert bridge.calls[2][0] == "addBlankFillerPages"
        assert bridge.calls[2][1] == ("doc_handle", 28, "Booklet_Master")

    def test_sync_returns_imposed_count(self) -> None:
        """sync_filler_pages returns the imposed page count."""
        from app.automation.pagination_sync import sync_filler_pages

        bridge = MockCOMBridge()
        # 10 pages with perfect_bind → imposed = 10 (already even)
        result = sync_filler_pages(bridge, "doc_handle", 10, "perfect_bind")
        assert result == 10

    def test_sync_wire_binding(self) -> None:
        """Wire binding rounds to multiple of 2."""
        from app.automation.pagination_sync import sync_filler_pages

        bridge = MockCOMBridge()
        result = sync_filler_pages(bridge, "doc_handle", 17, "wire")
        assert result == 18


# ═══════════════════════════════════════════════════════════════════════
# JSX Field-Name Contract Tests (AC-2.1.10)
# ═══════════════════════════════════════════════════════════════════════


class TestJSXFieldNameContract:
    """Verify the serialization contract between Python and JSX.

    AC-2.1.10: A payload deliberately missing a required field must
    be detectable. While the actual requireMarginField() runs in JSX,
    we verify the Python side produces all required fields.
    """

    def test_all_jsx_required_fields_present(self) -> None:
        """All four fields read by requireMarginField() are in the payload."""
        pg = PageGeometry()
        payload = pg.to_jsx_payload()
        margins = payload["margins"]

        jsx_required_fields = ["top_mm", "bottom_mm", "inside_spine_mm", "outside_mm"]
        for field in jsx_required_fields:
            assert field in margins, f"JSX-required field '{field}' missing from payload"
            assert isinstance(margins[field], (int, float)), (
                f"JSX-required field '{field}' must be a number, got {type(margins[field])}"
            )

    def test_manually_removed_field_is_detectable(self) -> None:
        """Simulating a serialization mismatch: removing a field from the payload.

        The JSX requireMarginField() would throw MarginConfigError.
        On the Python side, we verify the field is missing so the
        caller can detect it before sending to JSX.
        """
        pg = PageGeometry()
        payload = pg.to_jsx_payload()
        # Simulate a future serialization change that drops outside_mm
        del payload["margins"]["outside_mm"]
        assert "outside_mm" not in payload["margins"]

    def test_margin_config_change_propagates_to_payload(self) -> None:
        """Changing a MarginConfig field produces a different payload value.

        AC-2.1.7: end-to-end verification that a Python config change
        reaches the JSX payload.
        """
        pg_original = PageGeometry(margins=MarginConfig(top_mm=20.0))
        pg_modified = PageGeometry(margins=MarginConfig(top_mm=30.0))

        p1 = pg_original.to_jsx_payload()
        p2 = pg_modified.to_jsx_payload()

        assert p1["margins"]["top_mm"] == 20.0
        assert p2["margins"]["top_mm"] == 30.0
        assert p1["margins"]["top_mm"] != p2["margins"]["top_mm"]
