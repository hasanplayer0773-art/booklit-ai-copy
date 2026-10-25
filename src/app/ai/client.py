"""
Booklet_AI Desktop — OpenRouter AI Client (STEP-1.4)

OpenRouter API client enforcing Tagging-by-Reference and structured outputs.
Degrades gracefully to RuleBasedClassifier with explicit UI notification
via the on_fallback callback (wired to the ai_fallback_triggered signal).

Key design constraints:
- Zero-Mutation: raw_content and content_hash are never altered.
- Tagging-by-Reference: LLM only classifies, never rewrites text.
- Payload Minimization: pre-classified blocks are excluded from LLM payload.
- All-or-Nothing Response Validation: partial/malformed responses are fully
  rejected — no partial trust.
"""

import json
import logging
from typing import Callable, List, Optional

import httpx

from app.ai.classifier import PRECLASSIFIED_TYPES, RuleBasedClassifier
from app.ai.schemas import ClassificationResponse
from app.core.exceptions import BookletError
from app.core.models import ContentBlock

logger = logging.getLogger("booklet_ai.ai")


class InvalidClassificationResponse(BookletError):
    """Raised when a classification response fails exact-match validation
    against eligible_ids (missing IDs, foreign IDs, or duplicates).

    Always caught by classify_blocks()'s existing fallback handler — this
    exception's role is to make the "reject the whole batch" decision
    explicit and loggable, not to propagate to the caller.
    """


class OpenRouterClient:
    """OpenRouter API client enforcing Tagging-by-Reference and structured outputs.

    Degrades gracefully to RuleBasedClassifier with explicit UI notification
    via the on_fallback callback (wired to the ai_fallback_triggered signal).

    STEP-6.1 correction: the client reads the model string at call time
    (not at construction time) so that changing the model in the AI
    Settings UI takes effect on the next classify_blocks() call without
    an application restart (AC-6.1.1b).

    Attributes:
        api_key: OpenRouter API key (retrieved via keyring at runtime).
        model: Default LLM model identifier — overridden at call time
            by model_provider if supplied.
        model_provider: Optional callable returning the current model
            string from AppStore at call time (AC-6.1.1b).
        on_fallback: Optional callback fired when falling back to local classifier.
        endpoint: OpenRouter chat completions endpoint URL.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "google/gemini-2.0-flash-001",
        on_fallback: Optional[Callable[[str], None]] = None,
        model_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.model_provider = model_provider
        self.on_fallback = on_fallback
        self.endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def _resolve_model(self) -> str:
        """Return the model to use for the current call.

        If model_provider is set (e.g. a lambda reading AppStore),
        its return value takes precedence. Falls back to self.model.
        """
        if self.model_provider is not None:
            try:
                provided = self.model_provider()
                if provided and provided.strip():
                    return provided.strip()
            except Exception:
                pass
        return self.model

    def classify_blocks(
        self,
        blocks: List[ContentBlock],
        document_revision: int = 0,
    ) -> List[ContentBlock]:
        """Classify content blocks via OpenRouter LLM with local fallback.

        Pre-classified blocks (math_formula, chemistry_formula,
        legacy_equation_placeholder) are excluded from the LLM payload and
        retain their classification unconditionally.

        If the eligible-block payload for this single document exceeds a token budget,
        it is split into sequential chunks, each independently validated with the
        same exact-match rules (no partial/foreign/duplicate block_ids per chunk),
        then deterministically merged by block_id before returning.

        A chunk failure falls back to RuleBasedClassifier for THAT CHUNK only,
        not the whole document — consistent with per-call Audited Graceful Degradation.

        Args:
            blocks: All content blocks (including pre-classified ones).
            document_revision: Monotonically increasing revision counter of the
                calling document store for mutation race detection.

        Returns:
            New list of ContentBlock instances with updated block_type fields.
            raw_content and content_hash are guaranteed unchanged.
        """
        eligible_blocks = [
            b for b in blocks
            if b.block_type not in PRECLASSIFIED_TYPES and b.raw_content.strip()
        ]

        if not eligible_blocks or not self.api_key:
            if not self.api_key and self.on_fallback:
                self.on_fallback(
                    "OpenRouter API key not configured. Using local classifier."
                )
            return RuleBasedClassifier.classify_document_blocks(blocks)

        chunks = self._split_by_token_budget(eligible_blocks)
        classified_by_id: dict[str, ContentBlock] = {}
        for chunk in chunks:
            chunk_eligible_ids = {b.block_id for b in chunk}
            try:
                payload = self._build_payload(chunk)
                response = self._send_request(payload)
                parsed = ClassificationResponse.model_validate_json(
                    response.json()["choices"][0]["message"]["content"]
                )
                chunk_result = self._apply_classifications(chunk, parsed, chunk_eligible_ids)
                for b in chunk_result:
                    classified_by_id[b.block_id] = b
            except Exception as exc:
                err_msg = (
                    f"AI classification failed for chunk ({exc}). "
                    f"Falling back to local classifier for this chunk."
                )
                logger.warning(err_msg)
                if self.on_fallback:
                    self.on_fallback(err_msg)
                for b in RuleBasedClassifier.classify_document_blocks(chunk):
                    classified_by_id[b.block_id] = b

        return [classified_by_id.get(b.block_id, b) for b in blocks]

    def _send_request(self, payload: dict) -> httpx.Response:
        """Send httpx request to OpenRouter API endpoint."""
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            return response

    def _split_by_token_budget(self, blocks: List[ContentBlock]) -> List[List[ContentBlock]]:
        """Split blocks into sequential chunks based on token budget.

        Uses a conservative character-count proxy (CHARS_PER_TOKEN_ESTIMATE = 3.0)
        to avoid heavyweight tokenizer dependencies. Blocks are never split
        mid-block; a single block exceeding the budget becomes its own chunk.
        """
        chars_per_token_estimate = 3.0
        max_tokens_per_request = getattr(self, "max_tokens_per_request", 6000)
        max_chars = int(max_tokens_per_request * chars_per_token_estimate)

        chunks: List[List[ContentBlock]] = []
        current: List[ContentBlock] = []
        current_chars = 0
        for block in blocks:
            block_chars = len(block.raw_content)
            if current and current_chars + block_chars > max_chars:
                chunks.append(current)
                current, current_chars = [], 0
            current.append(block)
            current_chars += block_chars
        if current:
            chunks.append(current)
        return chunks

    def _build_payload(self, blocks: List[ContentBlock]) -> dict:
        """Build the OpenRouter API request payload.

        Uses json.dumps() (not str()) to produce a real JSON array that
        the model can reliably parse.

        Args:
            blocks: Eligible (non-pre-classified) blocks to classify.

        Returns:
            Complete request payload dict ready for httpx.post(json=...).
        """
        items = [
            {"block_id": b.block_id, "text": b.raw_content}
            for b in blocks
        ]
        return {
            "model": self._resolve_model(),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an academic document structure analyzer for Iraqi booklets. "
                        "Classify each text block strictly into one of: "
                        "['chapter_header', 'arabic_text', 'english_text', 'ministry_question', 'list_item']. "
                        "Return a JSON object matching this schema: "
                        '{"classifications": [{"block_id": string, "type": string}]}. '
                        "You must return exactly one classification for every block_id provided, "
                        "with no additional or missing block_ids. "
                        "Never rewrite, mutate, or add text."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(items, ensure_ascii=False),
                },
            ],
        }

    def _apply_classifications(
        self,
        original_blocks: List[ContentBlock],
        response: ClassificationResponse,
        eligible_ids: set,
    ) -> List[ContentBlock]:
        """Apply LLM classifications with strict all-or-nothing validation.

        The response MUST be a COMPLETE, EXACT match against eligible_ids:
        - No missing IDs (incomplete response)
        - No extra/foreign IDs (e.g. pre-classified block_ids)
        - No duplicate block_ids

        Any deviation causes the ENTIRE response to be rejected via
        InvalidClassificationResponse, triggering full-batch fallback
        to RuleBasedClassifier.

        Args:
            original_blocks: All original blocks (including pre-classified).
            response: Parsed classification response from the LLM.
            eligible_ids: Set of block_ids that were sent to the LLM.

        Returns:
            New list of ContentBlock instances with updated block_type fields.

        Raises:
            InvalidClassificationResponse: If the response fails
                exact-match validation.
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
                updated.append(
                    b.model_copy(update={"block_type": type_map[b.block_id]})
                )
            else:
                # Not eligible for classification (e.g. math_formula) — unchanged
                updated.append(b)
        return updated
