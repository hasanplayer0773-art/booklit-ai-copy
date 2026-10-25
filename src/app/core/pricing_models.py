"""
Booklet_AI Desktop — Pricing Models (STEP-5.1)

Immutable (frozen=True) Pydantic v2 configuration models for booklet cost
estimation, paper stocks, and volume discount tiers.
"""

from __future__ import annotations

from typing import Dict, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


# Binding methods matching common Iraqi print-shop types:
#   saddle_stitch (سيم)   — folded signatures of 4 pages
#   perfect_bound (غراء)  — adhesive spine binding (sheet-fed signatures)
#   spiral        (حلزوني) — spiral / coil binding
# Aliases included for backwards compatibility with earlier prototypes:
#   perfect_bind  — alias for perfect_bound
#   wire          — alias for double-sided wire binding (كبس)
BindingMethod = Literal[
    "saddle_stitch",
    "perfect_bound",
    "spiral",
    "perfect_bind",
    "wire",
]


class PaperStock(BaseModel):
    """Configuration for a specific paper stock.

    Defines GSM weight, per-sheet cost in IQD, and calibrated sheet thickness.
    The thickness_mm_per_sheet field drives both PricingEngine.estimate_spine_mm()
    and MarginConfig.inside_spine_mm (STEP-2.1) from a single shared contract [AC-5.1.3].
    """

    model_config = ConfigDict(frozen=True)

    name: str
    gsm: int
    cost_per_sheet_iqd: float
    thickness_mm_per_sheet: float

    @field_validator("gsm")
    @classmethod
    def validate_gsm_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"gsm must be positive, got {v}")
        return v

    @field_validator("cost_per_sheet_iqd", "thickness_mm_per_sheet")
    @classmethod
    def validate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"value must be non-negative, got {v}")
        return v


class CostModelConfig(BaseModel):
    """Cost model configuration for booklet pricing engine.

    Stores configurable parameters for currency, estimated ink coverage,
    ink costs, binding costs per method, and volume discount tiers.
    All defaults reflect Iraq-market print-shop practices.
    """

    model_config = ConfigDict(frozen=True)

    currency: str = "IQD"
    ink_coverage_estimate_pct: float = 15.0
    cost_per_ink_pct_per_page_iqd: float = 5.0
    binding_cost_flat_iqd: Dict[str, float] = Field(
        default_factory=lambda: {
            "saddle_stitch": 250.0,
            "perfect_bound": 750.0,
            "spiral": 500.0,
        }
    )
    volume_discount_tiers: Dict[int, float] = Field(
        default_factory=lambda: {
            1: 0.0,
            50: 0.05,
            200: 0.10,
            500: 0.15,
        }
    )

    @field_validator("ink_coverage_estimate_pct")
    @classmethod
    def validate_ink_coverage(cls, v: float) -> float:
        if v < 0.0 or v > 100.0:
            raise ValueError(
                f"ink_coverage_estimate_pct must be between 0 and 100, got {v}"
            )
        return v

    @field_validator("cost_per_ink_pct_per_page_iqd")
    @classmethod
    def validate_ink_cost(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                f"cost_per_ink_pct_per_page_iqd must be non-negative, got {v}"
            )
        return v


# ─── Common Iraqi Print-Shop Paper Stock Presets ──────────────────────────

WOODFREE_70GSM = PaperStock(
    name="Woodfree 70 GSM",
    gsm=70,
    cost_per_sheet_iqd=25.0,
    thickness_mm_per_sheet=0.09,
)

WOODFREE_80GSM = PaperStock(
    name="Woodfree 80 GSM",
    gsm=80,
    cost_per_sheet_iqd=30.0,
    thickness_mm_per_sheet=0.10,
)

GLOSSY_100GSM = PaperStock(
    name="Glossy Art 100 GSM",
    gsm=100,
    cost_per_sheet_iqd=45.0,
    thickness_mm_per_sheet=0.11,
)

GLOSSY_150GSM = PaperStock(
    name="Glossy Art 150 GSM",
    gsm=150,
    cost_per_sheet_iqd=60.0,
    thickness_mm_per_sheet=0.16,
)

DEFAULT_PAPER_STOCK = WOODFREE_80GSM
