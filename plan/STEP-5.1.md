<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-5.1] Cost Estimation & Batch Quoting Engine

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Cost Model | Pydantic v2 (`frozen=True`) config, same pattern as `MarginConfig`/`PageGeometry` | Keeps pricing inputs (paper cost/sheet, ink coverage estimate, binding cost) as an explicit, versionable data contract — not hardcoded constants scattered in UI code. |
| Sheet Imposition Calculator | Derives sheets-per-booklet from `PageGeometry` + binding method | Booklet page count doesn't map 1:1 to printed sheets (saddle-stitch folds 4 pages/sheet); pricing must be computed from actual imposition, not raw page count. |
| Spine Thickness Estimator | Paper GSM × page count → mm, feeding STEP-2.1's `inside_spine_mm` | Closes the loop with STEP-2.1: the same spine calculation that sets binding margins also drives cost estimation, avoiding two independent (and potentially inconsistent) thickness formulas. |
| Batch Quoting | Simple per-unit × quantity with configurable volume discount tiers | Print shops quote by run size (10 copies vs. 500 copies); flat per-unit pricing without tiers would misprice bulk orders. |

#### 2. Technical Notes

- **Binding method determines sheets-per-booklet, not just spine width:** saddle-stitch (سيم) folds signatures of 4 pages per physical sheet; perfect/glue binding (غراء) is typically 1:1 or 2:1 depending on signature grouping. The calculator branches on `binding_method` rather than using a single universal page-to-sheet ratio — a wrong ratio here silently misprices every quote, so this is documented as the single highest-leverage correctness point in this phase.
- **This phase has no Fail-Fast requirement in the STEP-1.1 sense** — a cost estimate is inherently approximate (paper price fluctuates, ink coverage is estimated not measured). Instead, the engine surfaces its assumptions explicitly in the output (e.g. "estimated ink coverage: 15%, adjust if illustration-heavy") rather than presenting a bare number as if it were precise. Precision-by-disclosure, not precision-by-computation.
- **Currency and paper-size assumptions are Iraq-market defaults** (IQD, A4/A5 booklet sizes, locally common GSM weights) but stored as configurable values, not hardcoded — the same `frozen=True` Pydantic pattern used throughout the project, so future markets/currencies require config changes, not code changes.
- **This phase is deliberately decoupled from the InDesign generation pipeline (Phases 1–2):** cost estimation only needs `PageGeometry` and page count, not the actual generated document — a quote can be produced before a single page is laid out, letting a print shop quote a job before committing generation time.

#### 3. Declarative Definitions

```python
# app/core/pricing_models.py
from typing import Literal, Dict
from pydantic import BaseModel, ConfigDict

BindingMethod = Literal["saddle_stitch", "perfect_bound", "spiral"]  # سيم / غراء / حلزوني

class PaperStock(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    gsm: int
    cost_per_sheet_iqd: float
    thickness_mm_per_sheet: float

class CostModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    currency: str = "IQD"
    ink_coverage_estimate_pct: float = 15.0
    cost_per_ink_pct_per_page_iqd: float = 5.0
    binding_cost_flat_iqd: Dict[BindingMethod, float] = {
        "saddle_stitch": 250.0,
        "perfect_bound": 750.0,
        "spiral": 500.0,
    }
    volume_discount_tiers: Dict[int, float] = {
        1: 0.0,
        50: 0.05,
        200: 0.10,
        500: 0.15,
    }
```

```python
# app/core/pricing_engine.py
import math
from app.core.pricing_models import CostModelConfig, PaperStock, BindingMethod

SHEETS_PER_BOOKLET_RATIO: dict[BindingMethod, float] = {
    "saddle_stitch": 0.25,   # 4 pages per physical sheet (folded signature)
    "perfect_bound": 0.5,    # 2 pages per sheet
    "spiral": 1.0,           # 1 page per sheet (typically single-sided stock)
}

# Saddle-stitch binding folds physical sheets into signatures of 4 pages;
# a page count that isn't a multiple of 4 cannot be physically imposed
# without blank filler pages at the end. Correction to an earlier draft
# of this engine, which computed sheets directly from raw page_count —
# that undercounts sheets (and therefore cost) for any saddle-stitch job
# whose page count isn't already a multiple of 4, and silently omits the
# blank filler pages that must be added to the InDesign document itself
# to avoid back-cover pagination coming out wrong.
IMPOSITION_MULTIPLE: dict[BindingMethod, int] = {
    "saddle_stitch": 4,
    "perfect_bound": 1,  # no fixed-multiple constraint for this method
    "spiral": 1,
}

def imposed_page_count(page_count: int, binding: BindingMethod) -> int:
    """
    Rounds page_count up to the binding method's required multiple.
    For saddle_stitch, this is the actual page count that will be printed
    (including blank filler pages), not just used for sheet-cost math —
    the same value must drive both pricing AND the InDesign document's
    actual page count (STEP-2.1) to keep the two in sync.
    """
    multiple = IMPOSITION_MULTIPLE[binding]
    return math.ceil(page_count / multiple) * multiple

class PricingEngine:
    """
    Produces a disclosed-assumption cost estimate, not a precise invoice.
    Decoupled from the InDesign pipeline — operates on page count and
    geometry only.
    """
    def __init__(self, cost_config: CostModelConfig, paper: PaperStock):
        self.cost_config = cost_config
        self.paper = paper

    def sheets_required(self, page_count: int, binding: BindingMethod) -> int:
        effective_pages = imposed_page_count(page_count, binding)
        ratio = SHEETS_PER_BOOKLET_RATIO[binding]
        return math.ceil(effective_pages * ratio)

    def estimate_spine_mm(self, page_count: int) -> float:
        # Feeds the same spine calculation STEP-2.1 uses for inside_spine_mm,
        # avoiding two independently-drifting thickness formulas.
        sheets = self.sheets_required(page_count, "perfect_bound")
        return sheets * self.paper.thickness_mm_per_sheet

    def quote_unit(self, page_count: int, binding: BindingMethod) -> dict:
        sheets = self.sheets_required(page_count, binding)
        paper_cost = sheets * self.paper.cost_per_sheet_iqd
        ink_cost = (
            page_count
            * self.cost_config.ink_coverage_estimate_pct
            * self.cost_config.cost_per_ink_pct_per_page_iqd
        )
        binding_cost = self.cost_config.binding_cost_flat_iqd[binding]
        subtotal = paper_cost + ink_cost + binding_cost
        return {
            "subtotal_iqd": round(subtotal, 2),
            "assumptions": {
                "ink_coverage_pct": self.cost_config.ink_coverage_estimate_pct,
                "sheets_used": sheets,
                "paper_stock": self.paper.name,
            },
        }

    def quote_batch(self, page_count: int, binding: BindingMethod, quantity: int) -> dict:
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
            "discount_applied_pct": discount * 100,
            "total_iqd": round(total_after, 2),
            "assumptions": unit["assumptions"],
        }
```

#### 4. Acceptance Criteria

- **[AC-5.1.1]** `sheets_required()` produces different results for identical `page_count` across the three `binding_method` values, reflecting each method's actual imposition ratio.
- **[AC-5.1.5]** For `binding_method = "saddle_stitch"`, `imposed_page_count(17, "saddle_stitch")` returns `20` (rounded up to the next multiple of 4), and `sheets_required(17, "saddle_stitch")` computes sheets from that rounded value, not the raw input.
- **[AC-5.1.6]** `imposed_page_count()`'s result for a saddle-stitch job is the same value used to determine the final blank-filler-page count added to the actual InDesign document — verified by the actual `sync_filler_pages()` orchestration (STEP-2.1) calling `imposed_page_count()` directly and passing its result to `addBlankFillerPages()`, not by two independently-maintained rounding calculations. (An earlier draft asserted this link existed without the connecting code being written — `sync_filler_pages()` is that missing connection.)
- **[AC-5.1.7]** When actual generated content exceeds the pre-layout page estimate (e.g. 22 actual pages vs. a 20-page estimate), `sync_filler_pages()` catches `ContentExceedsEstimateError` distinctly from a genuine filler-insertion failure, recomputes `imposed_page_count()` against the actual count, and successfully completes generation with a corrected target — never silently truncates real content pages and never reports this as the same error class as an actual filler-page bug.
- **[AC-5.1.2]** Every quote output (`quote_unit`/`quote_batch`) includes an `assumptions` block — no bare price number is returned without its disclosed estimation basis.
- **[AC-5.1.3]** `estimate_spine_mm()` and STEP-2.1's `inside_spine_mm` margin calculation both derive from the same `PaperStock.thickness_mm_per_sheet` source value — verified by a shared config reference, not duplicated constants.
- **[AC-5.1.4]** `quote_batch()` correctly applies the highest discount tier at or below the requested quantity (e.g. quantity=200 applies the 10% tier, not the 5% or 15% tier).

---

## PHASE 5 STATUS: CLOSED (STEP-5.1)

---

## PHASE 6: AI Configuration, Field Reliability & Professional Feature Layer

**Scope note:** This phase was added after Phases 1–5 were closed and adversarially reviewed across three rounds. The features below have NOT yet been through that same adversarial review. Each item is scoped deliberately narrowly — additions beyond what is written here (e.g. remote support, portable-mode packaging, a profile system, offline English classification patterns, scanned-PDF/OCR support, or fixed per-page instructor-name headers) were explicitly evaluated and rejected for this project and must not be reintroduced without a new decision.

---
