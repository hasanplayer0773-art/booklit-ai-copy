"""
Booklet_AI Desktop — Typography Styles Schema (STEP-2.2)

Frozen Pydantic v2 model defining all typography parameters used during
InDesign content injection. This schema is the single source of truth
for font names, sizes, leading values, and directionality settings
consumed by both the Python orchestration layer and the JSX injection
scripts.

SERIALIZATION CONTRACT: field names match 1:1 with the keys expected
by inject_tables_and_math.jsx's styles payload. Do NOT rename fields
without updating the JSX consumer simultaneously.

Key design decisions:
- math_direction is hardcoded to "left_to_right" — this is a
  non-negotiable constraint for correct BiDi rendering of equations.
  See STEP-2.2 §2 "Defense in Depth for LTR Enforcement".
- table_direction is hardcoded to "right_to_left" — Arabic reading
  flow requires Column 0 on the right-hand margin.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class TypographyStylesConfig(BaseModel):
    """Typography configuration for booklet content injection.

    Covers three content categories: body text, math formulas, and
    tables. Each category defines font, size, leading, and
    directionality settings.

    Math direction is enforced as LTR both here (as a validated
    constant) and explicitly in the JSX injection code — the
    "Defense in Depth" pattern described in STEP-2.2 §2. The
    template-level mathFormulaStyle paragraph direction is the
    primary defense; the code-level override is the secondary
    safeguard.

    Table direction is enforced as RTL for Arabic reading flow.

    Attributes:
        body_font: Primary body text font (Arabic-capable).
        body_size_pt: Body text font size in points.
        body_leading_pt: Body text leading (line spacing) in points.
        math_font: Font for math/formula blocks (LTR Latin font).
        math_size_pt: Math formula font size in points.
        math_leading_pt: Math formula leading in points.
        math_direction: Paragraph direction for math blocks.
            Must be "left_to_right" — validated to prevent
            accidental RTL assignment that would scramble equations.
        table_header_size_pt: Font size for table header cells.
        table_cell_size_pt: Font size for table body cells.
        table_direction: Table direction for column ordering.
            Must be "right_to_left" for Arabic reading flow.
    """

    model_config = ConfigDict(frozen=True)

    # Global / Body
    body_font: str = "Tajawal"
    body_size_pt: float = 11.5
    body_leading_pt: float = 17.0

    # Math & Formulas (Strict LTR — enforced both in template style and
    # explicitly in code; see "Defense in Depth" note in STEP-2.2 §2)
    math_font: str = "Cambria Math"
    math_size_pt: float = 12.0
    math_leading_pt: float = 18.0
    math_direction: Literal["left_to_right"] = "left_to_right"

    # Tables
    table_header_size_pt: float = 10.5
    table_cell_size_pt: float = 10.0
    table_direction: Literal["right_to_left"] = "right_to_left"

    @field_validator("math_direction")
    @classmethod
    def enforce_math_ltr(cls, v: str) -> str:
        """Reject any math direction other than left_to_right.

        This is a hard constraint, not a preference. Math expressions
        rendered in RTL mode suffer structural corruption from Unicode
        BiDi heuristics — brackets, exponents, and factorials all
        get repositioned incorrectly.
        """
        if v != "left_to_right":
            raise ValueError(
                f"math_direction must be 'left_to_right', got '{v}'. "
                "Math formulas under InDesign's RTL composer are "
                "structurally scrambled by Unicode BiDi heuristics — "
                "LTR direction is a non-negotiable constraint."
            )
        return v

    @field_validator("table_direction")
    @classmethod
    def enforce_table_rtl(cls, v: str) -> str:
        """Reject any table direction other than right_to_left.

        Arabic booklet tables must have Column 0 on the right-hand
        margin to match Arabic reading flow. An LTR table in an
        Arabic booklet is a layout defect, not a degradable condition.
        """
        if v != "right_to_left":
            raise ValueError(
                f"table_direction must be 'right_to_left', got '{v}'. "
                "Arabic booklets require RTL column ordering — Column 0 "
                "must be on the right-hand margin."
            )
        return v
