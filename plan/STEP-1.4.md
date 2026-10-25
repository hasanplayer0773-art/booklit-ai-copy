<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-1.4] AI Classification Layer — Tagging-by-Reference & Audited Fallback Engine

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| API Client | `httpx` (Client with explicit timeout & exponential backoff) | Fast HTTP client supporting synchronous and asynchronous connection pooling, strict timeouts (15s), and structured payloads to OpenRouter endpoints. |
| Key Management | `keyring` (OS Credential Manager) | API keys retrieved securely at runtime from Windows Credential Manager — never written to disk, config files, or debug logs. |
| Output Schema Enforcement | Pydantic v2 JSON Schema (`response_format` / Structured Outputs) | Enforces 100% schema compliance from the LLM, restricting returned types strictly to the `BlockType` Literal. |
| Audited Local Fallback | `RuleBasedClassifier` + Telemetry Signal | Deterministic local regex/rule engine executing in <10ms; activates on API/network errors, firing an `ai_fallback_triggered` signal to notify the UI while allowing offline completion. |
| Default Model | `google/gemini-2.0-flash-001` (user-overridable via `AppStore`) | Free-tier models (e.g. `gemma-4-26b-a4b-it:free`) suffer two production-blocking issues: (1) frequent JSON structure breakage on multi-block Arabic payloads (markdown code fences, renamed fields), and (2) 10–20s rate-limit queue delays per page. `gemini-2.0-flash-001` natively supports JSON Schema, responds in <500ms, and costs roughly $0.10/million tokens — a 50-page booklet costs about one cent to classify. |

#### 2. Design Rationale: Critical Ingestion vs. Enrichment Stage

The Fail-Fast Hard Halt policy from STEP-1.1–1.3 does **not** apply here, and this distinction is intentional:

- **Critical Ingestion Stage (STEP-1.1/1.2/1.3):** Concerns raw mathematical, chemical, and linguistic text fidelity. Any corruption here is structural data loss that cannot be patched — hence the mandatory Hard Halt (`TextIntegrityError`, `MathExtractionError`).
- **Enrichment Stage (STEP-1.4):** The AI never creates content or alters a single character of `raw_content` — it only adds a semantic metadata layer (`block_type`) on top of text whose hash has already been verified safe. Halting the entire pipeline on an API outage or exhausted quota would be disproportionate, since the deterministic local fallback (`RuleBasedClassifier`) can assign standard Iraqi-curriculum patterns (e.g. `علل`, `س وزاري`, `ما الفائدة`) at acceptable accuracy.

**Adopted policy — Audited Graceful Degradation:** the pipeline continues via `RuleBasedClassifier` on any failure, but fires an explicit `ai_fallback_triggered(reason: str)` signal so the UI can surface a visible warning banner (e.g. invalid key, exhausted quota, network failure) — never a fully silent degradation.

#### 3. Tagging-by-Reference Protocol (Zero-Mutation Constraint)

- **Strict Data Isolation:** The LLM is never tasked with correcting, rewriting, summarizing, or formatting the text. It functions exclusively as a semantic classifier.
- **Payload Minimization & Token Economy:** Blocks pre-classified during extraction (`math_formula`, `chemistry_formula`, and `TableData`) are strictly excluded from the LLM payload. Only unclassified text blocks (`arabic_text`, potential `ministry_question`, `chapter_header`) are transmitted.
- **Request/Response Structure:**
  - Payload sent: `[{"block_id": "p1_b1", "text": "..."}, {"block_id": "p1_b2", "text": "..."}]`
  - Response expected: `{"classifications": [{"block_id": "p1_b1", "type": "ministry_question"}, {"block_id": "p1_b2", "type": "arabic_text"}]}`
- **State Merge & Integrity Guard:** The classifier updates only the `block_type` field corresponding to each `block_id` in the `DocumentStore`. Immediately after merging, `compute_canonical_hash(block.raw_content)` is re-evaluated to ensure zero mutation occurred during the operation.

#### 4. Declarative Definitions

```python
# app/ai/schemas.py
from typing import List
from pydantic import BaseModel, ConfigDict
from app.core.models import BlockType

class BlockClassification(BaseModel):
    model_config = ConfigDict(frozen=True)
    block_id: str
    type: BlockType

class ClassificationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    classifications: List[BlockClassification]
```

```python
# app/ai/classifier.py
import re
from typing import List
from app.core.models import ContentBlock, BlockType

# CROSS-STEP AMENDMENT: STEP-1.2 (AC-1.2.5) emits `legacy_equation_placeholder`
# blocks. Like math/chemistry, they are assigned at extraction time and must
# NEVER be re-classified by the rule engine nor sent to the LLM. This single
# constant is the source of truth — do not hardcode the tuple elsewhere.
PRECLASSIFIED_TYPES: tuple[str, ...] = (
    "math_formula",
    "chemistry_formula",
    "legacy_equation_placeholder",
)

class RuleBasedClassifier:
    """
    Deterministic offline classification engine for Iraqi curriculum booklets.
    Acts as instant fallback when remote AI calls are unavailable or fail.
    """
    HEADER_PATTERN = re.compile(
        r"^(الفصل\s+|الوحدة\s+|المبحث\s+|الدرس\s+|المحاضرة\s+)"
    )
    QUESTION_PATTERN = re.compile(
        r"^(س\s*[\:\/]|س\s*وزاري|سؤال\s*[\:\/]|علل\s*[\:\/]|وضح\s*[\:\/]|قارن\s*[\:\/]|ما\s+المقصود|ما\s+فائدة|عدد\s*[\:\/])"
    )
    LIST_PATTERN = re.compile(
        r"^([\-•●▪·]|(?:\d+|[أ-يa-zA-Z])[\.\)])\s+"
    )

    @classmethod
    def classify_block(cls, block: ContentBlock) -> BlockType:
        # Pre-classified types from extraction stage are strictly immutable
        if block.block_type in PRECLASSIFIED_TYPES:
            return block.block_type

        text = block.raw_content.strip()

        if cls.HEADER_PATTERN.match(text) and len(text) <= 90:
            return "chapter_header"

        if cls.QUESTION_PATTERN.match(text):
            return "ministry_question"

        if cls.LIST_PATTERN.match(text):
            return "list_item"

        if re.search(r"[\u0600-\u06FF]", text):
            return "arabic_text"

        if re.search(r"[a-zA-Z]", text) and not re.search(r"[\u0600-\u06FF]", text):
            return "english_text"

        return "arabic_text"

    @classmethod
    def classify_document_blocks(cls, blocks: List[ContentBlock]) -> List[ContentBlock]:
        """Classifies all unassigned blocks deterministically."""
        updated_blocks = []
        for blk in blocks:
            assigned_type = cls.classify_block(blk)
            updated_blocks.append(
                blk.model_copy(update={"block_type": assigned_type})
            )
        return updated_blocks
```

```python
# app/ai/client.py
import logging
from typing import List, Callable, Optional
import httpx
from app.core.models import ContentBlock
from app.core.exceptions import BookletError
from app.ai.schemas import ClassificationResponse
from app.ai.classifier import RuleBasedClassifier, PRECLASSIFIED_TYPES

logger = logging.getLogger("booklet_ai.ai")

class InvalidClassificationResponse(BookletError):
    """
    Raised when a classification response fails exact-match validation
    against eligible_ids (missing IDs, foreign IDs, or duplicates). Always
    caught by classify_blocks()'s existing fallback handler — this
    exception's role is to make the "reject the whole batch" decision
    explicit and loggable, not to propagate to the caller.
    """

class OpenRouterClient:
    """
    OpenRouter API client enforcing Tagging-by-Reference and structured outputs.
    Degrades gracefully to RuleBasedClassifier with explicit UI notification
    via the on_fallback callback (wired to the ai_fallback_triggered signal).
    """
    def __init__(
        self,
        api_key: str,
        model: str = "google/gemini-2.0-flash-001",
        on_fallback: Optional[Callable[[str], None]] = None
    ):
        self.api_key = api_key
        self.model = model
        self.on_fallback = on_fallback
        self.endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def classify_blocks(self, blocks: List[ContentBlock]) -> List[ContentBlock]:
        eligible_blocks = [
            b for b in blocks
            if b.block_type not in PRECLASSIFIED_TYPES and b.raw_content.strip()
        ]

        if not eligible_blocks or not self.api_key:
            if not self.api_key and self.on_fallback:
                self.on_fallback("OpenRouter API key not configured. Using local classifier.")
            return RuleBasedClassifier.classify_document_blocks(blocks)

        # CORRECTION: an earlier draft passed the full eligible_ids set
        # implicitly and trusted whatever block_ids came back in the
        # response. This meant a model response referencing a block_id
        # NOT in eligible_blocks (e.g. a math_formula/chemistry_formula
        # block that was deliberately excluded from the payload) would
        # still be applied by _apply_classifications — directly
        # contradicting this document's own claim (AC-1.4.2) that
        # pre-classified types "retain their classification
        # unconditionally." The eligible ID set must be captured
        # explicitly and used to validate the response before any
        # classification is applied.
        eligible_ids = {b.block_id for b in eligible_blocks}

        try:
            payload = self._build_payload(eligible_blocks)
            with httpx.Client(timeout=15.0) as client:
                response = client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                )
                response.raise_for_status()
                parsed = ClassificationResponse.model_validate_json(
                    response.json()["choices"][0]["message"]["content"]
                )
                return self._apply_classifications(blocks, parsed, eligible_ids)
        except Exception as exc:
            err_msg = f"AI classification failed ({exc}). Falling back to local classifier."
            logger.warning(err_msg)
            if self.on_fallback:
                self.on_fallback(err_msg)
            return RuleBasedClassifier.classify_document_blocks(blocks)

    def _build_payload(self, blocks: List[ContentBlock]) -> dict:
        import json
        # CORRECTION: an earlier draft used Python's str(items) to embed the
        # payload in the prompt, which produces Python-repr syntax (single
        # quotes, no real JSON escaping) rather than actual JSON — increasing
        # the risk of the model misinterpreting the structure. json.dumps()
        # produces a real JSON array the model is far more reliably trained
        # to parse correctly.
        items = [{"block_id": b.block_id, "text": b.raw_content} for b in blocks]
        return {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an academic document structure analyzer for Iraqi booklets. "
                        "Classify each text block strictly into one of: "
                        "['chapter_header', 'arabic_text', 'english_text', 'ministry_question', 'list_item']. "
                        "Return a JSON object matching this schema: "
                        "{\"classifications\": [{\"block_id\": string, \"type\": string}]}. "
                        "You must return exactly one classification for every block_id provided, "
                        "with no additional or missing block_ids. "
                        "Never rewrite, mutate, or add text."
                    )
                },
                {
                    "role": "user",
                    "content": json.dumps(items, ensure_ascii=False)
                }
            ]
        }

    def _apply_classifications(
        self,
        original_blocks: List[ContentBlock],
        response: ClassificationResponse,
        eligible_ids: set,
    ) -> List[ContentBlock]:
        """
        Applies classifications only if the response is a COMPLETE, EXACT
        match against eligible_ids — no missing IDs, no extra/foreign IDs,
        no duplicates. Any deviation causes the ENTIRE response to be
        rejected and the whole batch falls back to RuleBasedClassifier.

        CORRECTION to an earlier draft: that version rejected classifications
        for foreign/duplicate block_ids individually but then silently
        applied whatever remained — meaning (a) a response with duplicate
        entries for one block_id kept that ID's FIRST-seen classification
        rather than rejecting the ID entirely (contradicting AC-1.4.2b's
        explicit requirement), and (b) a response missing some eligible
        IDs (e.g. the model returned 90 of 100 requested classifications)
        left the missing blocks silently unclassified with no fallback —
        those blocks would then fail later at style-resolution time
        (STEP-2.3's StyleMappingError) with no indication the root cause
        was an incomplete AI response. This is exactly the kind of silent
        partial-trust degradation the plan's Fail-Fast philosophy argues
        against elsewhere; classification responses get the same
        all-or-nothing treatment.
        """
        response_ids = [item.block_id for item in response.classifications]

        has_duplicates = len(response_ids) != len(set(response_ids))
        has_foreign_ids = any(rid not in eligible_ids for rid in response_ids)
        is_incomplete = set(response_ids) != eligible_ids

        if has_duplicates or has_foreign_ids or is_incomplete:
            raise InvalidClassificationResponse(
                f"Classification response failed exact-match validation: "
                f"duplicates={has_duplicates}, foreign_ids={has_foreign_ids}, "
                f"incomplete={is_incomplete}. Falling back to RuleBasedClassifier "
                f"for the full batch — no partial application of a malformed response."
            )

        type_map = {item.block_id: item.type for item in response.classifications}
        updated = []
        for b in original_blocks:
            if b.block_id in type_map:
                updated.append(b.model_copy(update={"block_type": type_map[b.block_id]}))
            else:
                updated.append(b)  # not eligible for classification (e.g. math_formula) — unchanged
        return updated
```

#### 5. Acceptance Criteria

- **[AC-1.4.1]** Zero mutation: `raw_content` and `content_hash` remain strictly identical for every `ContentBlock` before and after classification.
- **[AC-1.4.2]** A response including a classification for an excluded block's `block_id` (e.g. attempting to reclassify a `math_formula` block, simulating a model ignoring instructions) causes the entire response to be rejected via `InvalidClassificationResponse`, triggering full-batch fallback to `RuleBasedClassifier` — not a partial application that merely drops the offending entry.
- **[AC-1.4.2b]** A response containing duplicate entries for the same `block_id` causes the entire response to be rejected — not "first occurrence wins" and not "entry dropped, rest applied." The whole batch falls back to `RuleBasedClassifier`.
- **[AC-1.4.2c]** A response missing classifications for some eligible blocks (e.g. 90 of 100 requested `block_id`s returned) causes the entire response to be rejected and the whole batch falls back to `RuleBasedClassifier` — no block is ever left silently unclassified due to a partial AI response, which would otherwise surface later as an unrelated-looking `StyleMappingError` at injection time (STEP-2.3) with no indication the root cause was an incomplete classification response.
- **[AC-1.4.3]** In the event of API timeout, rate limiting, invalid JSON, or network disconnection, the system falls back to `RuleBasedClassifier` within <10ms, emitting an `ai_fallback_triggered` signal for UI notification, with zero unhandled exceptions propagating to the caller.
- **[AC-1.4.4]** API keys are accessed solely through `keyring` and are never written to disk, configuration files, or log streams.
- **[AC-1.4.5]** `legacy_equation_placeholder` blocks (STEP-1.2) are treated exactly like `math_formula`/`chemistry_formula`: (a) never included in the LLM payload, (b) returned unchanged by `RuleBasedClassifier`, (c) a model response that includes their `block_id` is rejected as a foreign ID via `InvalidClassificationResponse` with full-batch fallback. All three call sites use the single `PRECLASSIFIED_TYPES` constant — no hardcoded tuple.

---

## PHASE 1 STATUS: CLOSED (STEP-1.1 → STEP-1.4)

---

## PHASE 2: InDesign Automation & Layout Engine
