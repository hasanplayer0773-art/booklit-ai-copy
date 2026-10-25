"""
Booklet_AI Desktop — Document Geometry & Margin Models (STEP-2.1)

Frozen Pydantic v2 models defining the geometric properties of a
booklet document: margins, page size, bleeds, and facing-page layout.

SERIALIZATION CONTRACT: when MarginConfig is serialized via
model_dump(), field names are passed through UNCHANGED to the JSON
payload consumed by build_master_spread.jsx. The JSX reads
docCfg.margins.top_mm / .bottom_mm / .inside_spine_mm / .outside_mm
— matching these field names exactly. Do NOT rename or alias these
fields without updating the JSX reader simultaneously.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Dict, TYPE_CHECKING, Union

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

if TYPE_CHECKING:
    from app.core.pricing_models import PaperStock


class MarginConfig(BaseModel):
    """Page margin configuration for booklet geometry.

    Field names are a serialization contract with the JSX layer —
    build_master_spread.jsx reads these exact names via
    requireMarginField(). An earlier draft used abbreviated names
    (.top / .inside / .outside) masked by || fallback defaults;
    both the mismatch and the fallback pattern were removed.

    Attributes:
        top_mm: Top margin in millimeters (min 12.0mm enforced by JSX
                for header ribbon clearance).
        bottom_mm: Bottom margin in millimeters.
        inside_spine_mm: Inner (spine-side) margin for binding offset (كعب).
        outside_mm: Outer margin (opposite the spine).
        column_count: Number of text columns in the body frame.
        column_gutter_mm: Gutter width between columns in millimeters.
    """

    model_config = ConfigDict(frozen=True)

    top_mm: float = 20.0
    bottom_mm: float = 20.0
    inside_spine_mm: float = 22.0  # Safe spine offset for booklet binding (كعب)
    outside_mm: float = 15.0
    column_count: int = 1
    column_gutter_mm: float = 5.0

    @model_validator(mode="after")
    def validate_margins_nonnegative(self) -> MarginConfig:
        """Reject negative margins at the Python layer.

        Defense-in-depth: catches the confirmed bug class (unbounded
        margin values producing zero/negative body-frame height) before
        a bad config ever reaches JSX. The JSX-side
        InvalidBodyFrameGeometryError check remains as a second
        independent guard, since this validator cannot know the target
        page's actual height (resolved later against PageGeometry).

        This validator only catches the always-invalid-regardless-of-
        page-size case: negative margins, which are never valid.
        """
        for field_name in ("top_mm", "bottom_mm", "inside_spine_mm", "outside_mm"):
            value = getattr(self, field_name)
            if value < 0:
                raise ValueError(
                    f"{field_name} must be non-negative, got {value}"
                )
        return self

    @classmethod
    def from_paper_stock(
        cls,
        page_count: int,
        paper: PaperStock,
        base_inside_margin_mm: float = 15.0,
        min_inside_spine_mm: float = 20.0,
        top_mm: float = 20.0,
        bottom_mm: float = 20.0,
        outside_mm: float = 15.0,
        column_count: int = 1,
        column_gutter_mm: float = 5.0,
    ) -> MarginConfig:
        """Create MarginConfig with inside_spine_mm derived from PaperStock [AC-5.1.3]."""
        inside_spine_mm = calculate_inside_spine_mm(
            page_count=page_count,
            paper=paper,
            base_margin_mm=base_inside_margin_mm,
            min_margin_mm=min_inside_spine_mm,
        )
        return cls(
            top_mm=top_mm,
            bottom_mm=bottom_mm,
            inside_spine_mm=inside_spine_mm,
            outside_mm=outside_mm,
            column_count=column_count,
            column_gutter_mm=column_gutter_mm,
        )


def calculate_inside_spine_mm(
    page_count: int,
    paper: PaperStock,
    base_margin_mm: float = 15.0,
    min_margin_mm: float = 20.0,
) -> float:
    """Derive inside_spine_mm margin from PaperStock.thickness_mm_per_sheet.

    Shares the same thickness calculation formula as PricingEngine.estimate_spine_mm()
    using the shared PaperStock configuration reference [AC-5.1.3].
    """
    if page_count < 1:
        raise ValueError(f"page_count must be >= 1, got {page_count}")
    sheets = math.ceil(page_count * 0.5)
    spine_thickness = sheets * paper.thickness_mm_per_sheet
    return round(max(min_margin_mm, base_margin_mm + spine_thickness), 2)


class PageNumberingStyle(str, Enum):
    """Digit rendering style for automatic page numbering (STEP-6.8).

    Controls whether page numbers display using Arabic-Indic digits (١٢٣)
    or Western Arabic numerals (123) in InDesign's master footer frame.

    Attributes:
        ARABIC_INDIC: Arabic-Indic numerals (١٢٣), standard in Iraqi curricula.
                      Mapped to DigitsTypeOptions.HINDI_DIGITS in InDesign.
        WESTERN_ARABIC: Western Arabic numerals (123).
                        Mapped to DigitsTypeOptions.ARABIC_DIGITS in InDesign.
    """

    ARABIC_INDIC = "arabic_indic"
    WESTERN_ARABIC = "western_arabic"


ARABIC_INDIC_DIGITS_TABLE = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def format_page_number(
    page_number: int,
    style: Union[PageNumberingStyle, str] = PageNumberingStyle.ARABIC_INDIC,
) -> str:
    """Format a page number according to the specified numbering style.

    Args:
        page_number: 1-based page number.
        style: PageNumberingStyle or string ('arabic_indic' or 'western_arabic').

    Returns:
        Formatted page number string (e.g. '١٢' or '12').
    """
    s = str(page_number)
    style_str = style.value if isinstance(style, PageNumberingStyle) else str(style)
    norm = style_str.strip().lower().replace("-", "_")
    if norm in ("arabic_indic", "hindi", "hindi_digits", "eastern_arabic"):
        return s.translate(ARABIC_INDIC_DIGITS_TABLE)
    return s


class PageGeometry(BaseModel):
    """Complete page geometry configuration for a booklet document.

    Combines page dimensions, bleed, facing-page mode, margin configuration,
    and page numbering style (STEP-6.8) into a single frozen model. The
    to_jsx_payload() method serializes this to the exact dict structure
    expected by build_master_spread.jsx's docCfg parameter.

    Attributes:
        page_size: Named page size (e.g. "A4", "A5", "B5").
        width_pt: Page width in points.
        height_pt: Page height in points.
        facing_pages: Whether the document uses facing pages (mirrored
                      inside/outside margins for binding).
        bleed_mm: Bleed area in millimeters (extends beyond trim).
        margins: Margin configuration (embedded MarginConfig).
        numbering_style: Digit rendering style for AUTO_PAGE_NUMBER marker
                         (Arabic-Indic ١٢٣ vs Western Arabic 123).
    """

    model_config = ConfigDict(frozen=True)

    page_size: str = "A4"
    width_pt: float = 595.28
    height_pt: float = 841.89
    facing_pages: bool = True
    bleed_mm: float = 3.0
    margins: MarginConfig = MarginConfig()
    numbering_style: PageNumberingStyle = PageNumberingStyle.ARABIC_INDIC

    @field_validator("numbering_style", mode="before")
    @classmethod
    def validate_numbering_style(cls, v: Any) -> PageNumberingStyle:
        """Validate and normalize numbering_style input."""
        if isinstance(v, PageNumberingStyle):
            return v
        if isinstance(v, str):
            v_norm = v.strip().lower().replace("-", "_")
            if v_norm in ("arabic_indic", "hindi", "hindi_digits", "eastern_arabic"):
                return PageNumberingStyle.ARABIC_INDIC
            if v_norm in ("western_arabic", "arabic", "arabic_digits", "western", "default"):
                return PageNumberingStyle.WESTERN_ARABIC
        raise ValueError(
            f"Invalid numbering_style '{v}'. Must be PageNumberingStyle.ARABIC_INDIC "
            f"('arabic_indic') or PageNumberingStyle.WESTERN_ARABIC ('western_arabic')."
        )

    @property
    def page_numbering_style(self) -> PageNumberingStyle:
        """Alias for numbering_style."""
        return self.numbering_style

    @classmethod
    def from_paper_stock(
        cls,
        page_count: int,
        paper: PaperStock,
        page_size: str = "A4",
        width_pt: float = 595.28,
        height_pt: float = 841.89,
        facing_pages: bool = True,
        bleed_mm: float = 3.0,
        base_inside_margin_mm: float = 15.0,
        min_inside_spine_mm: float = 20.0,
        top_mm: float = 20.0,
        bottom_mm: float = 20.0,
        outside_mm: float = 15.0,
        column_count: int = 1,
        column_gutter_mm: float = 5.0,
        numbering_style: PageNumberingStyle = PageNumberingStyle.ARABIC_INDIC,
    ) -> PageGeometry:
        """Create PageGeometry with margins derived from PaperStock [AC-5.1.3]."""
        margins = MarginConfig.from_paper_stock(
            page_count=page_count,
            paper=paper,
            base_inside_margin_mm=base_inside_margin_mm,
            min_inside_spine_mm=min_inside_spine_mm,
            top_mm=top_mm,
            bottom_mm=bottom_mm,
            outside_mm=outside_mm,
            column_count=column_count,
            column_gutter_mm=column_gutter_mm,
        )
        return cls(
            page_size=page_size,
            width_pt=width_pt,
            height_pt=height_pt,
            facing_pages=facing_pages,
            bleed_mm=bleed_mm,
            margins=margins,
            numbering_style=numbering_style,
        )

    def to_jsx_payload(self) -> Dict[str, Any]:
        """Serialize to the dict structure expected by build_master_spread.jsx.

        The JSX reads docCfg.margins.top_mm, docCfg.margins.bottom_mm, etc.
        and docCfg.numbering_style for digit shaping (STEP-6.8).

        Returns:
            A plain dict matching the JSX payload contract. All margin
            field names are preserved unchanged (no aliasing).
        """
        payload = self.model_dump()
        payload["numbering_style"] = self.numbering_style.value
        return payload
