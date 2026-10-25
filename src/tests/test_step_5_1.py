"""
Booklet_AI Desktop — STEP-5.1 Test Suite
Tests for Cost Estimation & Batch Quoting Engine.

Verifies all Acceptance Criteria:
- [AC-5.1.1] sheets_required() produces different results across binding methods.
- [AC-5.1.2] Every quote output includes an assumptions block.
- [AC-5.1.3] estimate_spine_mm() and inside_spine_mm derive from shared PaperStock.
- [AC-5.1.4] quote_batch() correctly applies the highest discount tier at or below quantity.
- [AC-5.1.5] saddle_stitch imposed_page_count(17) returns 20, sheets_required uses it.
- [AC-5.1.6] imposed_page_count drives sync_filler_pages directly.
- [AC-5.1.7] sync_filler_pages catches ContentExceedsEstimateError and re-derives target.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.automation.geometry import (
    MarginConfig,
    PageGeometry,
    calculate_inside_spine_mm,
)
from app.automation.pagination_sync import sync_filler_pages
from app.core.exceptions import BookletError, ContentExceedsEstimateError
from app.core.pricing_engine import (
    IMPOSITION_MULTIPLE,
    SHEETS_PER_BOOKLET_RATIO,
    PricingEngine,
    imposed_page_count,
)
from app.core.pricing_models import (
    DEFAULT_PAPER_STOCK,
    GLOSSY_100GSM,
    GLOSSY_150GSM,
    WOODFREE_70GSM,
    WOODFREE_80GSM,
    CostModelConfig,
    PaperStock,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Pricing Models Tests
# ═══════════════════════════════════════════════════════════════════════


class TestPricingModels:
    """Tests for PaperStock and CostModelConfig frozen data contracts."""

    def test_paper_stock_frozen_immutability(self) -> None:
        """PaperStock is immutable (frozen=True)."""
        paper = PaperStock(
            name="Test Stock",
            gsm=80,
            cost_per_sheet_iqd=30.0,
            thickness_mm_per_sheet=0.10,
        )
        with pytest.raises(ValidationError):
            paper.cost_per_sheet_iqd = 35.0  # type: ignore

    def test_paper_stock_validation(self) -> None:
        """PaperStock rejects non-positive GSM or negative costs."""
        with pytest.raises(ValidationError, match="gsm must be positive"):
            PaperStock(
                name="Bad GSM",
                gsm=0,
                cost_per_sheet_iqd=30.0,
                thickness_mm_per_sheet=0.10,
            )

        with pytest.raises(ValidationError, match="value must be non-negative"):
            PaperStock(
                name="Bad Cost",
                gsm=80,
                cost_per_sheet_iqd=-5.0,
                thickness_mm_per_sheet=0.10,
            )

        with pytest.raises(ValidationError, match="value must be non-negative"):
            PaperStock(
                name="Bad Thickness",
                gsm=80,
                cost_per_sheet_iqd=30.0,
                thickness_mm_per_sheet=-0.01,
            )

    def test_paper_stock_presets(self) -> None:
        """Verify default Iraqi market presets exist and are valid."""
        assert WOODFREE_70GSM.gsm == 70
        assert WOODFREE_80GSM.gsm == 80
        assert GLOSSY_100GSM.gsm == 100
        assert GLOSSY_150GSM.gsm == 150
        assert DEFAULT_PAPER_STOCK == WOODFREE_80GSM
        assert WOODFREE_80GSM.cost_per_sheet_iqd > 0
        assert WOODFREE_80GSM.thickness_mm_per_sheet > 0

    def test_cost_model_config_frozen_immutability(self) -> None:
        """CostModelConfig is immutable (frozen=True)."""
        cfg = CostModelConfig()
        with pytest.raises(ValidationError):
            cfg.currency = "USD"  # type: ignore

    def test_cost_model_config_defaults(self) -> None:
        """CostModelConfig has sensible Iraq-market defaults."""
        cfg = CostModelConfig()
        assert cfg.currency == "IQD"
        assert cfg.ink_coverage_estimate_pct == 15.0
        assert cfg.cost_per_ink_pct_per_page_iqd == 5.0
        assert "saddle_stitch" in cfg.binding_cost_flat_iqd
        assert "perfect_bound" in cfg.binding_cost_flat_iqd
        assert "spiral" in cfg.binding_cost_flat_iqd
        assert cfg.binding_cost_flat_iqd["saddle_stitch"] == 250.0
        assert cfg.binding_cost_flat_iqd["perfect_bound"] == 750.0
        assert cfg.binding_cost_flat_iqd["spiral"] == 500.0
        assert cfg.volume_discount_tiers == {1: 0.0, 50: 0.05, 200: 0.10, 500: 0.15}

    def test_cost_model_config_validation(self) -> None:
        """CostModelConfig validates coverage and cost ranges."""
        with pytest.raises(ValidationError, match="ink_coverage_estimate_pct must be between 0 and 100"):
            CostModelConfig(ink_coverage_estimate_pct=150.0)

        with pytest.raises(ValidationError, match="ink_coverage_estimate_pct must be between 0 and 100"):
            CostModelConfig(ink_coverage_estimate_pct=-5.0)

        with pytest.raises(ValidationError, match="cost_per_ink_pct_per_page_iqd must be non-negative"):
            CostModelConfig(cost_per_ink_pct_per_page_iqd=-1.0)


# ═══════════════════════════════════════════════════════════════════════
# 2. Imposition & Sheets Required Tests (AC-5.1.1, AC-5.1.5)
# ═══════════════════════════════════════════════════════════════════════


class TestImpositionAndSheets:
    """Tests for imposed_page_count() and sheets_required()."""

    def test_imposed_page_count_saddle_stitch_ac_5_1_5(self) -> None:
        """[AC-5.1.5] For saddle_stitch, 17 pages rounds up to 20."""
        assert imposed_page_count(17, "saddle_stitch") == 20
        assert imposed_page_count(1, "saddle_stitch") == 4
        assert imposed_page_count(4, "saddle_stitch") == 4
        assert imposed_page_count(5, "saddle_stitch") == 8
        assert imposed_page_count(16, "saddle_stitch") == 16
        assert imposed_page_count(22, "saddle_stitch") == 24

    def test_imposed_page_count_perfect_bound_and_spiral(self) -> None:
        """Perfect-bound and spiral have multiple=1 in STEP-5.1."""
        assert imposed_page_count(17, "perfect_bound") == 17
        assert imposed_page_count(1, "perfect_bound") == 1
        assert imposed_page_count(17, "spiral") == 17
        assert imposed_page_count(1, "spiral") == 1

    def test_imposed_page_count_aliases(self) -> None:
        """Aliases (perfect_bind, wire) maintain backwards compatibility."""
        assert imposed_page_count(17, "perfect_bind") == 18
        assert imposed_page_count(17, "wire") == 18

    def test_imposed_page_count_invalid_input(self) -> None:
        """Page counts < 1 raise ValueError."""
        with pytest.raises(ValueError, match="raw_page_count must be >= 1"):
            imposed_page_count(0, "saddle_stitch")
        with pytest.raises(ValueError, match="raw_page_count must be >= 1"):
            imposed_page_count(-5, "perfect_bound")

    def test_sheets_required_distinct_across_methods_ac_5_1_1(self) -> None:
        """[AC-5.1.1] sheets_required() produces different results across binding methods."""
        engine = PricingEngine()
        page_count = 20

        sheets_saddle = engine.sheets_required(page_count, "saddle_stitch")
        sheets_perfect = engine.sheets_required(page_count, "perfect_bound")
        sheets_spiral = engine.sheets_required(page_count, "spiral")

        # saddle_stitch: 20 * 0.25 = 5 sheets
        # perfect_bound: 20 * 0.5 = 10 sheets
        # spiral: 20 * 1.0 = 20 sheets
        assert sheets_saddle == 5
        assert sheets_perfect == 10
        assert sheets_spiral == 20
        assert len({sheets_saddle, sheets_perfect, sheets_spiral}) == 3

    def test_sheets_required_uses_imposed_page_count_ac_5_1_5(self) -> None:
        """[AC-5.1.5] sheets_required(17, 'saddle_stitch') computes from imposed 20 pages."""
        engine = PricingEngine()
        # 17 pages -> imposed=20 -> 20 * 0.25 = 5 sheets
        sheets = engine.sheets_required(17, "saddle_stitch")
        assert sheets == 5

        # 13 pages -> imposed=16 -> 16 * 0.25 = 4 sheets
        assert engine.sheets_required(13, "saddle_stitch") == 4


# ═══════════════════════════════════════════════════════════════════════
# 3. Spine Thickness & Geometry Derivation Tests (AC-5.1.3)
# ═══════════════════════════════════════════════════════════════════════


class TestSpineThicknessAndGeometry:
    """Tests for estimate_spine_mm() and inside_spine_mm margin integration."""

    def test_estimate_spine_mm_calculation(self) -> None:
        """estimate_spine_mm() calculates sheets(perfect_bound) * thickness."""
        paper = PaperStock(
            name="Standard 80",
            gsm=80,
            cost_per_sheet_iqd=30.0,
            thickness_mm_per_sheet=0.10,
        )
        engine = PricingEngine(paper=paper)

        # 100 pages -> 50 sheets * 0.10 mm = 5.0 mm
        spine = engine.estimate_spine_mm(100)
        assert spine == 5.0

    def test_shared_config_reference_ac_5_1_3(self) -> None:
        """[AC-5.1.3] estimate_spine_mm and inside_spine_mm derive from shared PaperStock."""
        custom_paper = PaperStock(
            name="Heavy 120 GSM",
            gsm=120,
            cost_per_sheet_iqd=45.0,
            thickness_mm_per_sheet=0.16,
        )
        engine = PricingEngine(paper=custom_paper)
        page_count = 100

        # 1. Engine's spine estimate
        spine_mm = engine.estimate_spine_mm(page_count)
        # 100 pages -> 50 sheets * 0.16 = 8.0 mm
        assert spine_mm == 8.0

        # 2. Geometry module's inside_spine_mm derivation using the SAME paper stock
        inside_spine_from_func = calculate_inside_spine_mm(
            page_count=page_count,
            paper=custom_paper,
            base_margin_mm=15.0,
            min_margin_mm=20.0,
        )
        # 15.0 base + 8.0 spine = 23.0 mm (> min 20.0)
        assert inside_spine_from_func == 23.0

        # 3. MarginConfig factory from PaperStock
        margin_cfg = MarginConfig.from_paper_stock(
            page_count=page_count,
            paper=custom_paper,
            base_inside_margin_mm=15.0,
        )
        assert margin_cfg.inside_spine_mm == 23.0

        # 4. PricingEngine factory creating MarginConfig
        engine_margin_cfg = engine.create_margin_config(
            page_count=page_count,
            base_inside_margin_mm=15.0,
        )
        assert engine_margin_cfg.inside_spine_mm == 23.0

        # 5. PageGeometry factory from PaperStock
        page_geom = PageGeometry.from_paper_stock(
            page_count=page_count,
            paper=custom_paper,
            base_inside_margin_mm=15.0,
        )
        assert page_geom.margins.inside_spine_mm == 23.0

        # Verify that changing thickness directly changes both
        thicker_paper = PaperStock(
            name="Thick 200 GSM",
            gsm=200,
            cost_per_sheet_iqd=80.0,
            thickness_mm_per_sheet=0.25,
        )
        engine_thick = PricingEngine(paper=thicker_paper)
        # 50 sheets * 0.25 = 12.5 mm
        assert engine_thick.estimate_spine_mm(page_count) == 12.5
        assert engine_thick.calculate_inside_spine_margin(page_count, 15.0) == 27.5
        assert (
            MarginConfig.from_paper_stock(page_count, thicker_paper, 15.0).inside_spine_mm
            == 27.5
        )

    def test_spine_margin_respects_minimum_margin(self) -> None:
        """Small spine thickness falls back to min_margin_mm safeguard."""
        paper = PaperStock(
            name="Light",
            gsm=60,
            cost_per_sheet_iqd=20.0,
            thickness_mm_per_sheet=0.08,
        )
        # 4 pages -> 2 sheets * 0.08 = 0.16 mm spine
        # 15.0 + 0.16 = 15.16 mm < min_margin 20.0 mm
        margin = calculate_inside_spine_mm(
            page_count=4,
            paper=paper,
            base_margin_mm=15.0,
            min_margin_mm=20.0,
        )
        assert margin == 20.0


# ═══════════════════════════════════════════════════════════════════════
# 4. Unit Quoting Tests (AC-5.1.2)
# ═══════════════════════════════════════════════════════════════════════


class TestQuoteUnit:
    """Tests for quote_unit() and disclosed assumptions block."""

    def test_quote_unit_structure_and_assumptions_ac_5_1_2(self) -> None:
        """[AC-5.1.2] Every quote includes an assumptions block (no bare prices)."""
        engine = PricingEngine()
        quote = engine.quote_unit(20, "saddle_stitch")

        assert "subtotal_iqd" in quote
        assert "assumptions" in quote
        assumptions = quote["assumptions"]
        assert "ink_coverage_pct" in assumptions
        assert "sheets_used" in assumptions
        assert "paper_stock" in assumptions
        assert assumptions["ink_coverage_pct"] == 15.0
        assert assumptions["sheets_used"] == 5
        assert assumptions["paper_stock"] == "Woodfree 80 GSM"

    def test_quote_unit_calculation_accuracy(self) -> None:
        """quote_unit() calculates exact subtotal = paper + ink + binding."""
        cfg = CostModelConfig(
            ink_coverage_estimate_pct=10.0,
            cost_per_ink_pct_per_page_iqd=4.0,
            binding_cost_flat_iqd={
                "saddle_stitch": 300.0,
                "perfect_bound": 800.0,
                "spiral": 600.0,
            },
        )
        paper = PaperStock(
            name="Custom 80",
            gsm=80,
            cost_per_sheet_iqd=20.0,
            thickness_mm_per_sheet=0.10,
        )
        engine = PricingEngine(cost_config=cfg, paper=paper)

        # 20 pages saddle_stitch:
        # sheets = 5
        # paper_cost = 5 * 20.0 = 100.0
        # ink_cost = 20 pages * 10.0% * 4.0 IQD = 800.0
        # binding_cost = 300.0
        # subtotal = 100 + 800 + 300 = 1200.0 IQD
        quote = engine.quote_unit(20, "saddle_stitch")
        assert quote["subtotal_iqd"] == 1200.0

    def test_quote_unit_invalid_inputs(self) -> None:
        """quote_unit() rejects page_count < 1 or invalid binding."""
        engine = PricingEngine()
        with pytest.raises(ValueError, match="raw_page_count must be >= 1"):
            engine.quote_unit(0, "saddle_stitch")

        with pytest.raises(ValueError, match="Unknown binding method"):
            engine.quote_unit(20, "unknown_binding")  # type: ignore


# ═══════════════════════════════════════════════════════════════════════
# 5. Batch Quoting & Volume Discount Tests (AC-5.1.2, AC-5.1.4)
# ═══════════════════════════════════════════════════════════════════════


class TestQuoteBatch:
    """Tests for quote_batch() and volume discount tiers."""

    def test_volume_discount_tier_application_ac_5_1_4(self) -> None:
        """[AC-5.1.4] quote_batch() correctly applies the highest discount tier at or below quantity."""
        engine = PricingEngine()
        # Default tiers: {1: 0.0, 50: 0.05, 200: 0.10, 500: 0.15}
        page_count = 20
        binding = "saddle_stitch"
        unit_price = engine.quote_unit(page_count, binding)["subtotal_iqd"]

        # Quantity 1: Tier 1 (0% discount)
        q1 = engine.quote_batch(page_count, binding, quantity=1)
        assert q1["discount_applied_pct"] == 0.0
        assert q1["total_iqd"] == unit_price

        # Quantity 49: Still Tier 1 (0% discount)
        q49 = engine.quote_batch(page_count, binding, quantity=49)
        assert q49["discount_applied_pct"] == 0.0

        # Quantity 50: Tier 50 (5% discount)
        q50 = engine.quote_batch(page_count, binding, quantity=50)
        assert q50["discount_applied_pct"] == 5.0
        assert q50["total_iqd"] == round(unit_price * 50 * 0.95, 2)

        # Quantity 199: Tier 50 (5% discount)
        q199 = engine.quote_batch(page_count, binding, quantity=199)
        assert q199["discount_applied_pct"] == 5.0

        # Quantity 200: Exactly Tier 200 (10% discount, NOT 5% or 15%) [AC-5.1.4]
        q200 = engine.quote_batch(page_count, binding, quantity=200)
        assert q200["discount_applied_pct"] == 10.0
        assert q200["total_iqd"] == round(unit_price * 200 * 0.90, 2)

        # Quantity 499: Still Tier 200 (10% discount)
        q499 = engine.quote_batch(page_count, binding, quantity=499)
        assert q499["discount_applied_pct"] == 10.0

        # Quantity 500: Tier 500 (15% discount)
        q500 = engine.quote_batch(page_count, binding, quantity=500)
        assert q500["discount_applied_pct"] == 15.0
        assert q500["total_iqd"] == round(unit_price * 500 * 0.85, 2)

        # Quantity 1000: Tier 500 (15% discount)
        q1000 = engine.quote_batch(page_count, binding, quantity=1000)
        assert q1000["discount_applied_pct"] == 15.0

    def test_batch_quote_includes_assumptions_ac_5_1_2(self) -> None:
        """[AC-5.1.2] quote_batch() includes the assumptions block."""
        engine = PricingEngine()
        batch = engine.quote_batch(20, "saddle_stitch", quantity=100)
        assert "assumptions" in batch
        assert batch["assumptions"]["sheets_used"] == 5
        assert batch["assumptions"]["paper_stock"] == "Woodfree 80 GSM"

    def test_batch_quote_invalid_quantity(self) -> None:
        """quote_batch() rejects quantity < 1."""
        engine = PricingEngine()
        with pytest.raises(ValueError, match="quantity must be >= 1"):
            engine.quote_batch(20, "saddle_stitch", quantity=0)
        with pytest.raises(ValueError, match="quantity must be >= 1"):
            engine.quote_batch(20, "saddle_stitch", quantity=-10)


# ═══════════════════════════════════════════════════════════════════════
# 6. Pagination Sync Orchestration Tests (AC-5.1.6, AC-5.1.7)
# ═══════════════════════════════════════════════════════════════════════


class MockCOMBridge:
    """Mock COM bridge for testing sync_filler_pages() interaction."""

    def __init__(self, actual_page_count: int = 10, overrun: bool = False) -> None:
        self.actual_page_count = actual_page_count
        self.overrun = overrun
        self.calls: list = []

    def call_jsx_function(self, function_name: str, *args) -> int | None:
        self.calls.append((function_name, args))
        if function_name == "addBlankFillerPages" and self.overrun:
            self.overrun = False
            raise ContentExceedsEstimateError(
                f"Actual content requires {self.actual_page_count} pages"
            )
        if function_name == "getPageCount":
            return self.actual_page_count
        return None


class TestPaginationSyncWiring:
    """Tests verifying sync_filler_pages wiring to imposed_page_count."""

    def test_sync_filler_pages_calls_pricing_imposed_count_ac_5_1_6(self) -> None:
        """[AC-5.1.6] sync_filler_pages calls imposed_page_count directly."""
        bridge = MockCOMBridge()
        # For saddle-stitch with estimate=17, imposed target is 20
        result = sync_filler_pages(bridge, "doc_handle", 17, "saddle_stitch")

        assert result == 20
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "addBlankFillerPages"
        assert bridge.calls[0][1] == ("doc_handle", 20, "Booklet_Master")

    def test_sync_filler_pages_content_overrun_handling_ac_5_1_7(self) -> None:
        """[AC-5.1.7] sync_filler_pages catches ContentExceedsEstimateError distinctly and re-derives."""
        # Pre-layout estimate is 20 pages; actual content overruns to 22 pages
        bridge = MockCOMBridge(actual_page_count=22, overrun=True)
        result = sync_filler_pages(bridge, "doc_handle", 20, "saddle_stitch")

        # 1. First call tried target=20 (from estimate=20)
        # 2. JSX raised ContentExceedsEstimateError (actual=22)
        # 3. sync_filler_pages caught it distinctly
        # 4. getPageCount returned 22
        # 5. recomputed imposed_page_count(22, "saddle_stitch") = 24
        # 6. called addBlankFillerPages with 24
        assert result == 24
        assert len(bridge.calls) == 3
        assert bridge.calls[0] == ("addBlankFillerPages", ("doc_handle", 20, "Booklet_Master"))
        assert bridge.calls[1] == ("getPageCount", ("doc_handle",))
        assert bridge.calls[2] == ("addBlankFillerPages", ("doc_handle", 24, "Booklet_Master"))

    def test_sync_filler_pages_genuine_error_not_caught_as_overrun(self) -> None:
        """Non-overrun errors (genuine failures) propagate without being masked."""
        class FailingCOMBridge:
            def call_jsx_function(self, function_name: str, *args) -> None:
                raise BookletError("InDesign COM fatal communication failure")

        failing_bridge = FailingCOMBridge()
        with pytest.raises(BookletError, match="InDesign COM fatal communication failure"):
            sync_filler_pages(failing_bridge, "doc_handle", 20, "saddle_stitch")
