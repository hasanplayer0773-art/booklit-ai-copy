"""
Booklet_AI Desktop — Tests for STEP-2.2
Arabic RTL Tables & LTR Math Injection Engine

Tests cover:
- TypographyStylesConfig model (defaults, validation, immutability)
- RTLTableUnsupportedError exception
- JSX script structural validation (inject_tables_and_math.jsx)
- Acceptance Criteria AC-2.2.1 through AC-2.2.4 verification

Note: JSX tests validate script structure and logic patterns since
ExtendScript cannot be executed outside InDesign. Python-side models
and exceptions are tested with full unit tests.
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import (
    BookletError,
    RTLTableUnsupportedError,
)


# ─── Path Constants ────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JSX_SCRIPT_PATH = PROJECT_ROOT / "assets" / "scripts" / "inject_tables_and_math.jsx"


# ═══════════════════════════════════════════════════════════════════════
# § 1. TypographyStylesConfig Tests
# ═══════════════════════════════════════════════════════════════════════


class TestTypographyStylesConfigDefaults:
    """Verify default values match the STEP-2.2 plan specification."""

    def test_body_defaults(self) -> None:
        cfg = TypographyStylesConfig()
        assert cfg.body_font == "Tajawal"
        assert cfg.body_size_pt == 11.5
        assert cfg.body_leading_pt == 17.0

    def test_math_defaults(self) -> None:
        cfg = TypographyStylesConfig()
        assert cfg.math_font == "Cambria Math"
        assert cfg.math_size_pt == 12.0
        assert cfg.math_leading_pt == 18.0
        assert cfg.math_direction == "left_to_right"

    def test_table_defaults(self) -> None:
        cfg = TypographyStylesConfig()
        assert cfg.table_header_size_pt == 10.5
        assert cfg.table_cell_size_pt == 10.0
        assert cfg.table_direction == "right_to_left"


class TestTypographyStylesConfigImmutability:
    """Verify frozen=True enforcement — no mutation allowed."""

    def test_frozen_model_rejects_mutation(self) -> None:
        cfg = TypographyStylesConfig()
        with pytest.raises(ValidationError):
            cfg.body_font = "Arial"  # type: ignore[misc]

    def test_frozen_math_direction_rejects_mutation(self) -> None:
        cfg = TypographyStylesConfig()
        with pytest.raises(ValidationError):
            cfg.math_direction = "right_to_left"  # type: ignore[misc]


class TestTypographyStylesConfigValidation:
    """Verify Literal type and validator constraints."""

    def test_math_direction_rejects_rtl(self) -> None:
        """math_direction='right_to_left' must raise ValidationError."""
        with pytest.raises(ValidationError, match="left_to_right"):
            TypographyStylesConfig(math_direction="right_to_left")  # type: ignore[arg-type]

    def test_math_direction_rejects_arbitrary(self) -> None:
        """math_direction must be exactly 'left_to_right'."""
        with pytest.raises(ValidationError):
            TypographyStylesConfig(math_direction="auto")  # type: ignore[arg-type]

    def test_table_direction_rejects_ltr(self) -> None:
        """table_direction='left_to_right' must raise ValidationError."""
        with pytest.raises(ValidationError, match="right_to_left"):
            TypographyStylesConfig(table_direction="left_to_right")  # type: ignore[arg-type]

    def test_table_direction_rejects_arbitrary(self) -> None:
        """table_direction must be exactly 'right_to_left'."""
        with pytest.raises(ValidationError):
            TypographyStylesConfig(table_direction="auto")  # type: ignore[arg-type]

    def test_valid_custom_fonts(self) -> None:
        """Custom font names are allowed as long as directions are valid."""
        cfg = TypographyStylesConfig(
            body_font="Amiri",
            math_font="Consolas",
            body_size_pt=13.0,
        )
        assert cfg.body_font == "Amiri"
        assert cfg.math_font == "Consolas"
        assert cfg.body_size_pt == 13.0
        # Directions still at defaults
        assert cfg.math_direction == "left_to_right"
        assert cfg.table_direction == "right_to_left"


class TestTypographyStylesConfigSerialization:
    """Verify model_dump() produces the payload expected by JSX."""

    def test_model_dump_keys(self) -> None:
        cfg = TypographyStylesConfig()
        dumped = cfg.model_dump()

        expected_keys = {
            "body_font",
            "body_size_pt",
            "body_leading_pt",
            "math_font",
            "math_size_pt",
            "math_leading_pt",
            "math_direction",
            "table_header_size_pt",
            "table_cell_size_pt",
            "table_direction",
        }
        assert set(dumped.keys()) == expected_keys

    def test_model_dump_values(self) -> None:
        cfg = TypographyStylesConfig()
        dumped = cfg.model_dump()
        assert dumped["math_direction"] == "left_to_right"
        assert dumped["table_direction"] == "right_to_left"


# ═══════════════════════════════════════════════════════════════════════
# § 2. RTLTableUnsupportedError Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRTLTableUnsupportedError:
    """Verify the exception exists, inherits correctly, and is raisable."""

    def test_inherits_from_booklet_error(self) -> None:
        assert issubclass(RTLTableUnsupportedError, BookletError)

    def test_inherits_from_exception(self) -> None:
        assert issubclass(RTLTableUnsupportedError, Exception)

    def test_raise_and_catch(self) -> None:
        with pytest.raises(RTLTableUnsupportedError):
            raise RTLTableUnsupportedError(
                "This InDesign installation does not support "
                "RIGHT_TO_LEFT_DIRECTION tables."
            )

    def test_catch_as_booklet_error(self) -> None:
        """RTLTableUnsupportedError must be catchable as BookletError."""
        with pytest.raises(BookletError):
            raise RTLTableUnsupportedError("test")


# ═══════════════════════════════════════════════════════════════════════
# § 3. JSX Script Structural Validation
# ═══════════════════════════════════════════════════════════════════════


class TestJSXScriptExists:
    """Verify the JSX script file exists at the expected path."""

    def test_script_file_exists(self) -> None:
        assert JSX_SCRIPT_PATH.exists(), (
            f"inject_tables_and_math.jsx not found at {JSX_SCRIPT_PATH}"
        )

    def test_script_not_empty(self) -> None:
        content = JSX_SCRIPT_PATH.read_text(encoding="utf-8")
        assert len(content) > 100, "JSX script appears to be empty or stub"


class TestJSXScriptStructure:
    """Validate key structural elements of inject_tables_and_math.jsx."""

    @pytest.fixture(autouse=True)
    def _load_jsx(self) -> None:
        self.jsx = JSX_SCRIPT_PATH.read_text(encoding="utf-8")

    # ── AC-2.2.1: RTL Table Direction ──────────────────────────────

    def test_has_injectRTLTable_function(self) -> None:
        """The injectRTLTable function must be defined."""
        assert "function injectRTLTable(" in self.jsx

    def test_sets_rtl_table_direction(self) -> None:
        """Must set tableDirection to RIGHT_TO_LEFT_DIRECTION."""
        assert "TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION" in self.jsx
        assert "indTable.tableDirection" in self.jsx

    def test_rtl_unsupported_error(self) -> None:
        """Must throw RTLTableUnsupportedError on missing RTL support."""
        assert "RTLTableUnsupportedError" in self.jsx
        # Verify it's a throw, not just a comment
        assert re.search(
            r'throw\s+new\s+Error\(\s*"RTLTableUnsupportedError',
            self.jsx,
        )

    def test_rtl_check_before_use(self) -> None:
        """RTL support check must appear BEFORE tableDirection assignment."""
        check_pos = self.jsx.find("typeof TableDirectionOptions")
        assignment_pos = self.jsx.find(
            "indTable.tableDirection =\n        TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION"
        )
        assert check_pos < assignment_pos, (
            "RTL support check must appear before tableDirection assignment"
        )

    # ── AC-2.2.2: Header Row Repetition ────────────────────────────

    def test_header_row_count_set(self) -> None:
        """Tables with headers must set headerRowCount = 1."""
        assert "headerRowCount:" in self.jsx
        # Verify it's conditional on headers
        assert re.search(
            r"headerRowCount:\s*headers\.length\s*>\s*0\s*\?\s*1\s*:\s*0",
            self.jsx,
        )

    # ── AC-2.2.3: LTR Math Direction ──────────────────────────────

    def test_has_injectMathFormulaBlock_function(self) -> None:
        """The injectMathFormulaBlock function must be defined."""
        assert "function injectMathFormulaBlock(" in self.jsx

    def test_math_explicit_ltr_direction(self) -> None:
        """Must explicitly set paragraph direction to LTR for math blocks."""
        assert (
            "ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION" in self.jsx
        )

    def test_math_applies_style(self) -> None:
        """Must apply mathFormulaStyle to the injected text range."""
        assert "styles.mathFormulaStyle" in self.jsx
        assert "applyParagraphStyle" in self.jsx

    def test_math_defense_in_depth_comment(self) -> None:
        """Must contain a Defense in Depth comment explaining the dual
        enforcement of LTR direction (template + code)."""
        assert "Defense in Depth" in self.jsx

    # ── AC-2.2.4: Proportional Column Widths ──────────────────────

    def test_proportional_width_calculation(self) -> None:
        """Must calculate column widths proportional to content width."""
        assert "colCharWeights" in self.jsx
        assert "frameWidthPt" in self.jsx
        assert "totalWeight" in self.jsx

    def test_column_width_assignment(self) -> None:
        """Must assign calculated widths to each column."""
        assert re.search(
            r"indTable\.columns\.item\(colIdx\)\.width\s*=\s*assignedWidth",
            self.jsx,
        )

    def test_minimum_column_weight(self) -> None:
        """Must enforce a minimum column weight to prevent zero-width."""
        assert re.search(
            r"Math\.max\(maxLen,\s*4\)",
            self.jsx,
        )

    # ── Table Formatting ──────────────────────────────────────────

    def test_vertical_center_alignment(self) -> None:
        """Cells must use vertical center alignment."""
        matches = re.findall(
            r"VerticalJustification\.CENTER_ALIGN",
            self.jsx,
        )
        # At least in header cells and body cells
        assert len(matches) >= 2

    def test_header_cell_padding(self) -> None:
        """Header cells must have professional padding (4.0 top/bottom, 6.0 left/right)."""
        assert "hCell.topInset = 4.0" in self.jsx
        assert "hCell.bottomInset = 4.0" in self.jsx
        assert "hCell.leftInset = 6.0" in self.jsx
        assert "hCell.rightInset = 6.0" in self.jsx

    def test_body_cell_padding(self) -> None:
        """Body cells must have professional padding (3.5 top/bottom, 5.0 left/right)."""
        assert "bCell.topInset = 3.5" in self.jsx
        assert "bCell.bottomInset = 3.5" in self.jsx
        assert "bCell.leftInset = 5.0" in self.jsx
        assert "bCell.rightInset = 5.0" in self.jsx

    def test_alternating_row_fills(self) -> None:
        """Body rows must alternate between tableAltColor and paperColor."""
        assert "colors.tableAltColor" in self.jsx
        assert "colors.paperColor" in self.jsx
        assert re.search(
            r"isAlt\s*\?\s*colors\.tableAltColor\s*:\s*colors\.paperColor",
            self.jsx,
        )

    def test_header_fill_color(self) -> None:
        """Header cells must be filled with primaryColor."""
        assert "hCell.fillColor = colors.primaryColor" in self.jsx

    def test_border_stroke_configuration(self) -> None:
        """Table borders must be configured with correct colors and weights."""
        assert "indTable.topBorderStrokeColor = colors.primaryColor" in self.jsx
        assert "indTable.topBorderStrokeWeight = 1.0" in self.jsx
        assert "indTable.bottomBorderStrokeColor = colors.primaryColor" in self.jsx
        assert "indTable.bottomBorderStrokeWeight = 1.0" in self.jsx
        assert "indTable.leftBorderStrokeColor = colors.tableBorderColor" in self.jsx
        assert "indTable.leftBorderStrokeWeight = 0.5" in self.jsx
        assert "indTable.rightBorderStrokeColor = colors.tableBorderColor" in self.jsx
        assert "indTable.rightBorderStrokeWeight = 0.5" in self.jsx

    # ── Atomic Insertion ──────────────────────────────────────────

    def test_trailing_paragraph_break_table(self) -> None:
        """Table injection must end with a paragraph break."""
        # Look for the mandatory newline after table insertion
        assert re.search(
            r'story\.insertionPoints\[-1\]\.contents\s*=\s*"\\r"',
            self.jsx,
        )

    def test_trailing_paragraph_break_math(self) -> None:
        """Math injection must include a paragraph break after the text."""
        assert re.search(
            r'mathText\s*\+\s*"\\r"',
            self.jsx,
        )

    # ── Guard Clause ──────────────────────────────────────────────

    def test_empty_table_guard(self) -> None:
        """Must return early for empty tables (no headers and no rows)."""
        assert re.search(
            r"headers\.length\s*===\s*0\s*&&\s*rows\.length\s*===\s*0",
            self.jsx,
        )
