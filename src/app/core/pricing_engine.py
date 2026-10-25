"""
Booklet_AI Desktop — Cost Estimation & Batch Quoting Engine (STEP-5.1)

Decoupled cost calculation engine that estimates paper sheets, ink consumption,
spine thickness, and volume-discounted batch quotes based on page count and
binding method alone.

Surfaces explicit estimation assumptions in every quote [AC-5.1.2].
Provides unified spine thickness calculations feeding both pricing and
STEP-2.1 inside_spine_mm margin calculations [AC-5.1.3].
"""

from __future__ import annotations

import math
from typing import Any, Dict

from app.core.pricing_models import (
    DEFAULT_PAPER_STOCK,
    BindingMethod,
    CostModelConfig,
    PaperStock,
)

SHEETS_PER_BOOKLET_RATIO: dict[str, float] = {
    "saddle_stitch": 0.25,   # 4 pages per physical sheet (folded signature)
    "perfect_bound": 0.5,    # 2 pages per sheet
    "spiral": 1.0,           # 1 page per sheet (typically single-sided stock)
    "perfect_bind": 0.5,     # alias
    "wire": 0.5,             # alias for wire (double-sided / sheet-fed)
}

# Saddle-stitch binding folds physical sheets into signatures of 4 pages;
# a page count that isn't a multiple of 4 cannot be physically imposed
# without blank filler pages at the end. Correction to an earlier draft
# of this engine, which computed sheets directly from raw page_count —
# that undercounts sheets (and therefore cost) for any saddle-stitch job
# whose page count isn't already a multiple of 4, and silently omits the
# blank filler pages that must be added to the InDesign document itself
# to avoid back-cover pagination coming out wrong.
IMPOSITION_MULTIPLE: dict[str, int] = {
    "saddle_stitch": 4,
    "perfect_bound": 1,      # no fixed-multiple constraint for this method (STEP-5.1)
    "spiral": 1,
    "perfect_bind": 2,       # sheet-fed multiple of 2 (STEP-2.1)
    "wire": 2,               # double-sided wire binding requires multiple of 2 (STEP-2.1)
}

_CANONICAL_BINDING: dict[str, str] = {
    "perfect_bind": "perfect_bound",
    "wire": "spiral",
}


def _canonical_binding(binding: str) -> str:
    """Resolve binding method aliases to canonical names."""
    return _CANONICAL_BINDING.get(binding, binding)


def imposed_page_count(page_count: int, binding: BindingMethod) -> int:
    """Rounds page_count up to the binding method's required multiple.

    For saddle_stitch, this is the actual page count that will be printed
    (including blank filler pages), not just used for sheet-cost math —
    the same value must drive both pricing AND the InDesign document's
    actual page count (STEP-2.1) to keep the two in sync.

    Args:
        page_count: Raw page count (must be >= 1).
        binding: Binding method ("saddle_stitch", "perfect_bound", "spiral", etc.).

    Returns:
        The imposed page count rounded up to the required signature multiple.

    Raises:
        ValueError: If page_count < 1 or unknown binding method.
    """
    if page_count < 1:
        raise ValueError(f"raw_page_count must be >= 1, got {page_count}")

    canonical = _canonical_binding(binding)
    if binding not in IMPOSITION_MULTIPLE and canonical not in IMPOSITION_MULTIPLE:
        raise ValueError(f"Unknown binding method: {binding}")

    multiple = IMPOSITION_MULTIPLE.get(
        binding, IMPOSITION_MULTIPLE.get(canonical, 1)
    )
    return int(math.ceil(page_count / multiple) * multiple)


class PricingEngine:
    """Produces a disclosed-assumption cost estimate, not a precise invoice.

    Decoupled from the InDesign pipeline — operates on page count and
    geometry only.
    """

    def __init__(
        self,
        cost_config: CostModelConfig | None = None,
        paper: PaperStock | None = None,
    ) -> None:
        self.cost_config = cost_config if cost_config is not None else CostModelConfig()
        self.paper = paper if paper is not None else DEFAULT_PAPER_STOCK

    def sheets_required(self, page_count: int, binding: BindingMethod) -> int:
        """Calculate the physical paper sheets required for a booklet.

        Computes sheets from imposed_page_count, not raw page count [AC-5.1.5].
        """
        if page_count < 1:
            raise ValueError(f"raw_page_count must be >= 1, got {page_count}")

        effective_pages = imposed_page_count(page_count, binding)
        canonical = _canonical_binding(binding)
        if binding not in SHEETS_PER_BOOKLET_RATIO and canonical not in SHEETS_PER_BOOKLET_RATIO:
            raise ValueError(f"Unknown binding method: {binding}")

        ratio = SHEETS_PER_BOOKLET_RATIO.get(
            binding, SHEETS_PER_BOOKLET_RATIO.get(canonical, 0.5)
        )
        return int(math.ceil(effective_pages * ratio))

    def estimate_spine_mm(self, page_count: int) -> float:
        """Estimate spine thickness in millimeters based on paper stock.

        Feeds the same spine calculation STEP-2.1 uses for inside_spine_mm,
        avoiding two independently-drifting thickness formulas [AC-5.1.3].
        """
        if page_count < 1:
            raise ValueError(f"raw_page_count must be >= 1, got {page_count}")

        sheets = self.sheets_required(page_count, "perfect_bound")
        return round(sheets * self.paper.thickness_mm_per_sheet, 4)

    def calculate_inside_spine_margin(
        self,
        page_count: int,
        base_inside_margin_mm: float = 15.0,
        min_margin_mm: float = 20.0,
    ) -> float:
        """Derives inside_spine_mm from the shared PaperStock configuration.

        Directly uses self.paper.thickness_mm_per_sheet through estimate_spine_mm(),
        guaranteeing that the pricing engine and STEP-2.1 geometry share the exact
        same paper stock thickness without duplicated constants [AC-5.1.3].
        """
        spine_mm = self.estimate_spine_mm(page_count)
        return round(max(min_margin_mm, base_inside_margin_mm + spine_mm), 2)

    def create_margin_config(
        self,
        page_count: int,
        base_inside_margin_mm: float = 15.0,
        min_margin_mm: float = 20.0,
        top_mm: float = 20.0,
        bottom_mm: float = 20.0,
        outside_mm: float = 15.0,
        column_count: int = 1,
        column_gutter_mm: float = 5.0,
    ) -> Any:
        """Build MarginConfig with inside_spine_mm dynamically calculated from paper stock."""
        from app.automation.geometry import MarginConfig

        inside_spine_mm = self.calculate_inside_spine_margin(
            page_count=page_count,
            base_inside_margin_mm=base_inside_margin_mm,
            min_margin_mm=min_margin_mm,
        )
        return MarginConfig(
            top_mm=top_mm,
            bottom_mm=bottom_mm,
            inside_spine_mm=inside_spine_mm,
            outside_mm=outside_mm,
            column_count=column_count,
            column_gutter_mm=column_gutter_mm,
        )

    def quote_unit(self, page_count: int, binding: BindingMethod) -> dict[str, Any]:
        """Generate a single booklet cost estimate with disclosed assumptions [AC-5.1.2]."""
        if page_count < 1:
            raise ValueError(f"raw_page_count must be >= 1, got {page_count}")

        sheets = self.sheets_required(page_count, binding)
        paper_cost = sheets * self.paper.cost_per_sheet_iqd
        ink_cost = (
            page_count
            * self.cost_config.ink_coverage_estimate_pct
            * self.cost_config.cost_per_ink_pct_per_page_iqd
        )
        canonical = _canonical_binding(binding)
        if binding in self.cost_config.binding_cost_flat_iqd:
            binding_cost = self.cost_config.binding_cost_flat_iqd[binding]
        elif canonical in self.cost_config.binding_cost_flat_iqd:
            binding_cost = self.cost_config.binding_cost_flat_iqd[canonical]
        else:
            raise ValueError(f"Unknown binding method for cost lookup: {binding}")

        subtotal = paper_cost + ink_cost + binding_cost
        return {
            "subtotal_iqd": round(subtotal, 2),
            "assumptions": {
                "ink_coverage_pct": self.cost_config.ink_coverage_estimate_pct,
                "sheets_used": sheets,
                "paper_stock": self.paper.name,
            },
        }

    def quote_batch(
        self,
        page_count: int,
        binding: BindingMethod,
        quantity: int,
    ) -> dict[str, Any]:
        """Generate a batch quote applying volume discount tiers [AC-5.1.4].

        Always includes the unit quote's assumptions block [AC-5.1.2].
        """
        if quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {quantity}")

        unit = self.quote_unit(page_count, binding)
        discount = 0.0
        for threshold in sorted(self.cost_config.volume_discount_tiers):
            if quantity >= threshold:
                discount = self.cost_config.volume_discount_tiers[threshold]

        total_before = unit["subtotal_iqd"] * quantity
        total_after = total_before * (1 - discount)
        return {
            "quantity": quantity,
            "unit_price_iqd": unit["subtotal_iqd"],
            "discount_applied_pct": round(discount * 100, 2),
            "total_iqd": round(total_after, 2),
            "assumptions": unit["assumptions"],
        }
