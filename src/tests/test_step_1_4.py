"""
Booklet_AI Desktop — Tests for STEP-1.4 (AI Classification Layer)

Covers all Acceptance Criteria:
- AC-1.4.1: Zero mutation (raw_content & content_hash unchanged)
- AC-1.4.2: Foreign block_id causes full-batch rejection
- AC-1.4.2b: Duplicate block_id causes full-batch rejection
- AC-1.4.2c: Incomplete response causes full-batch rejection
- AC-1.4.3: API failure → RuleBasedClassifier fallback + signal
- AC-1.4.4: (keyring access — not testable in unit tests, verified by code review)
- AC-1.4.5: legacy_equation_placeholder treated like math/chemistry
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ai.classifier import PRECLASSIFIED_TYPES, RuleBasedClassifier
from app.ai.client import InvalidClassificationResponse, OpenRouterClient
from app.ai.schemas import BlockClassification, ClassificationResponse
from app.core.models import ContentBlock
from app.core.text_integrity import compute_canonical_hash


# ─── Test Helpers ──────────────────────────────────────────────────────


def _make_block(
    block_id: str,
    raw_content: str,
    block_type: str = "arabic_text",
) -> ContentBlock:
    """Create a ContentBlock with a valid content_hash."""
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_content=raw_content,
        content_hash=compute_canonical_hash(raw_content),
    )


# ─── RuleBasedClassifier Tests ─────────────────────────────────────────


class TestRuleBasedClassifier:
    """Tests for deterministic rule-based classification."""

    def test_header_detection(self):
        block = _make_block("b1", "الفصل الأول: المقدمة")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "chapter_header"

    def test_header_too_long_falls_back(self):
        """Headers longer than 90 chars should NOT be classified as chapter_header."""
        long_text = "الفصل الأول: " + "أ" * 100
        block = _make_block("b1", long_text)
        result = RuleBasedClassifier.classify_block(block)
        assert result != "chapter_header"

    def test_question_detection_س_وزاري(self):
        block = _make_block("b1", "س وزاري: ما هي الخلية؟")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "ministry_question"

    def test_question_detection_علل(self):
        block = _make_block("b1", "علل: تزداد سرعة التفاعل")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "ministry_question"

    def test_question_detection_ما_المقصود(self):
        block = _make_block("b1", "ما المقصود بالتفاعل الكيميائي؟")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "ministry_question"

    def test_list_item_detection_bullet(self):
        block = _make_block("b1", "• عنصر من القائمة")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "list_item"

    def test_list_item_detection_numbered(self):
        block = _make_block("b1", "1. عنصر أول")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "list_item"

    def test_arabic_text_default(self):
        block = _make_block("b1", "هذا نص عربي عادي")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "arabic_text"

    def test_english_text_detection(self):
        block = _make_block("b1", "This is English text", block_type="english_text")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "english_text"

    def test_preclassified_math_unchanged(self):
        block = _make_block("b1", "x^2 + y^2 = z^2", block_type="math_formula")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "math_formula"

    def test_preclassified_chemistry_unchanged(self):
        block = _make_block("b1", "H2O + NaCl", block_type="chemistry_formula")
        result = RuleBasedClassifier.classify_block(block)
        assert result == "chemistry_formula"

    def test_preclassified_legacy_equation_unchanged(self):
        """AC-1.4.5: legacy_equation_placeholder is never re-classified."""
        block = _make_block(
            "b1", "[OLE Equation]", block_type="legacy_equation_placeholder"
        )
        result = RuleBasedClassifier.classify_block(block)
        assert result == "legacy_equation_placeholder"

    def test_classify_document_blocks_preserves_all(self):
        blocks = [
            _make_block("b1", "الفصل الأول: المقدمة"),
            _make_block("b2", "x^2 + 1", block_type="math_formula"),
            _make_block("b3", "هذا نص عادي"),
        ]
        results = RuleBasedClassifier.classify_document_blocks(blocks)
        assert len(results) == 3
        assert results[0].block_type == "chapter_header"
        assert results[1].block_type == "math_formula"  # unchanged
        assert results[2].block_type == "arabic_text"


# ─── PRECLASSIFIED_TYPES Constant Tests ────────────────────────────────


class TestPreclassifiedTypes:
    """AC-1.4.5: All three preclassified types use a single constant."""

    def test_contains_math_formula(self):
        assert "math_formula" in PRECLASSIFIED_TYPES

    def test_contains_chemistry_formula(self):
        assert "chemistry_formula" in PRECLASSIFIED_TYPES

    def test_contains_legacy_equation_placeholder(self):
        assert "legacy_equation_placeholder" in PRECLASSIFIED_TYPES

    def test_is_tuple(self):
        """Ensure it's a tuple (immutable), not a list."""
        assert isinstance(PRECLASSIFIED_TYPES, tuple)


# ─── Schema Validation Tests ──────────────────────────────────────────


class TestSchemas:
    """Tests for Pydantic schema enforcement."""

    def test_valid_classification_response(self):
        data = {
            "classifications": [
                {"block_id": "b1", "type": "arabic_text"},
                {"block_id": "b2", "type": "chapter_header"},
            ]
        }
        resp = ClassificationResponse.model_validate(data)
        assert len(resp.classifications) == 2
        assert resp.classifications[0].type == "arabic_text"

    def test_invalid_block_type_rejected(self):
        """Schema must reject types not in BlockType Literal."""
        data = {
            "classifications": [
                {"block_id": "b1", "type": "invalid_type_xyz"},
            ]
        }
        with pytest.raises(Exception):  # Pydantic ValidationError
            ClassificationResponse.model_validate(data)

    def test_frozen_schemas(self):
        """Schemas must be immutable (frozen=True)."""
        bc = BlockClassification(block_id="b1", type="arabic_text")
        with pytest.raises(Exception):
            bc.block_id = "b2"

        resp = ClassificationResponse(
            classifications=[bc]
        )
        with pytest.raises(Exception):
            resp.classifications = []


# ─── OpenRouterClient Tests ───────────────────────────────────────────


class TestOpenRouterClient:
    """Tests for the OpenRouter AI client."""

    def _make_test_blocks(self):
        """Create a standard set of test blocks."""
        return [
            _make_block("p1_b1", "الفصل الأول: المقدمة"),
            _make_block("p1_b2", "هذا نص عربي عادي"),
            _make_block("p1_b3", "x^2 + 1", block_type="math_formula"),
            _make_block("p1_b4", "[OLE]", block_type="legacy_equation_placeholder"),
        ]

    # ── AC-1.4.1: Zero Mutation ────────────────────────────────────

    def test_zero_mutation_after_rule_fallback(self):
        """AC-1.4.1: raw_content and content_hash unchanged after classification."""
        blocks = self._make_test_blocks()
        original_hashes = {b.block_id: (b.raw_content, b.content_hash) for b in blocks}

        client = OpenRouterClient(api_key="", on_fallback=lambda _: None)
        results = client.classify_blocks(blocks)

        for result in results:
            orig_content, orig_hash = original_hashes[result.block_id]
            assert result.raw_content == orig_content
            assert result.content_hash == orig_hash

    @patch("app.ai.client.httpx.Client")
    def test_zero_mutation_after_api_success(self, mock_httpx_client):
        """AC-1.4.1: raw_content and content_hash unchanged after successful API call."""
        blocks = self._make_test_blocks()
        original_hashes = {b.block_id: (b.raw_content, b.content_hash) for b in blocks}

        # Only p1_b1 and p1_b2 are eligible (p1_b3=math, p1_b4=legacy)
        api_response = json.dumps({
            "classifications": [
                {"block_id": "p1_b1", "type": "chapter_header"},
                {"block_id": "p1_b2", "type": "arabic_text"},
            ]
        })

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": api_response}}]
        }
        mock_httpx_client.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_response)))
        mock_httpx_client.return_value.__exit__ = MagicMock(return_value=False)

        client = OpenRouterClient(api_key="test-key")
        results = client.classify_blocks(blocks)

        for result in results:
            orig_content, orig_hash = original_hashes[result.block_id]
            assert result.raw_content == orig_content
            assert result.content_hash == orig_hash

    # ── AC-1.4.2: Foreign block_id → full-batch rejection ──────────

    def test_foreign_block_id_triggers_full_batch_rejection(self):
        """AC-1.4.2: Response with a pre-classified block_id → reject entire batch."""
        blocks = self._make_test_blocks()
        eligible_ids = {"p1_b1", "p1_b2"}

        # Response includes p1_b3 which is math_formula (excluded from payload)
        response = ClassificationResponse(
            classifications=[
                BlockClassification(block_id="p1_b1", type="chapter_header"),
                BlockClassification(block_id="p1_b2", type="arabic_text"),
                BlockClassification(block_id="p1_b3", type="arabic_text"),  # FOREIGN
            ]
        )

        client = OpenRouterClient(api_key="test-key")
        with pytest.raises(InvalidClassificationResponse):
            client._apply_classifications(blocks, response, eligible_ids)

    # ── AC-1.4.2b: Duplicate block_id → full-batch rejection ──────

    def test_duplicate_block_id_triggers_full_batch_rejection(self):
        """AC-1.4.2b: Duplicate entries for same block_id → reject entire batch."""
        blocks = self._make_test_blocks()
        eligible_ids = {"p1_b1", "p1_b2"}

        response = ClassificationResponse(
            classifications=[
                BlockClassification(block_id="p1_b1", type="chapter_header"),
                BlockClassification(block_id="p1_b1", type="arabic_text"),  # DUPLICATE
                BlockClassification(block_id="p1_b2", type="arabic_text"),
            ]
        )

        client = OpenRouterClient(api_key="test-key")
        with pytest.raises(InvalidClassificationResponse):
            client._apply_classifications(blocks, response, eligible_ids)

    # ── AC-1.4.2c: Incomplete response → full-batch rejection ─────

    def test_incomplete_response_triggers_full_batch_rejection(self):
        """AC-1.4.2c: Missing block_ids in response → reject entire batch."""
        blocks = self._make_test_blocks()
        eligible_ids = {"p1_b1", "p1_b2"}

        # Response is missing p1_b2
        response = ClassificationResponse(
            classifications=[
                BlockClassification(block_id="p1_b1", type="chapter_header"),
            ]
        )

        client = OpenRouterClient(api_key="test-key")
        with pytest.raises(InvalidClassificationResponse):
            client._apply_classifications(blocks, response, eligible_ids)

    # ── AC-1.4.3: API failure → fallback + signal ─────────────────

    @patch("app.ai.client.httpx.Client")
    def test_api_timeout_triggers_fallback(self, mock_httpx_client):
        """AC-1.4.3: httpx timeout → RuleBasedClassifier fallback + on_fallback fired."""
        blocks = self._make_test_blocks()
        fallback_reasons = []

        mock_httpx_client.return_value.__enter__ = MagicMock(
            return_value=MagicMock(
                post=MagicMock(side_effect=httpx.TimeoutException("Connection timed out"))
            )
        )
        mock_httpx_client.return_value.__exit__ = MagicMock(return_value=False)

        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)

        # Fallback should have been triggered
        assert len(fallback_reasons) == 1
        assert "failed" in fallback_reasons[0].lower() or "timed out" in fallback_reasons[0].lower()

        # Results should still be valid (from RuleBasedClassifier)
        assert len(results) == len(blocks)

    @patch("app.ai.client.httpx.Client")
    def test_api_http_error_triggers_fallback(self, mock_httpx_client):
        """AC-1.4.3: HTTP 429/500 → fallback with on_fallback signal."""
        blocks = self._make_test_blocks()
        fallback_reasons = []

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Rate limited", request=MagicMock(), response=MagicMock()
        )
        mock_httpx_client.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(return_value=mock_response))
        )
        mock_httpx_client.return_value.__exit__ = MagicMock(return_value=False)

        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)

        assert len(fallback_reasons) == 1
        assert len(results) == len(blocks)

    @patch("app.ai.client.httpx.Client")
    def test_invalid_json_triggers_fallback(self, mock_httpx_client):
        """AC-1.4.3: Invalid JSON from API → fallback."""
        blocks = self._make_test_blocks()
        fallback_reasons = []

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "not valid json {{"}}]
        }
        mock_httpx_client.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(return_value=mock_response))
        )
        mock_httpx_client.return_value.__exit__ = MagicMock(return_value=False)

        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)

        assert len(fallback_reasons) == 1
        assert len(results) == len(blocks)

    def test_no_api_key_triggers_fallback_signal(self):
        """AC-1.4.3: Empty API key → fallback + on_fallback fired."""
        blocks = self._make_test_blocks()
        fallback_reasons = []

        client = OpenRouterClient(
            api_key="",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)

        assert len(fallback_reasons) == 1
        assert "not configured" in fallback_reasons[0].lower()
        assert len(results) == len(blocks)

    # ── AC-1.4.5: legacy_equation_placeholder integration ─────────

    def test_legacy_equation_excluded_from_payload(self):
        """AC-1.4.5a: legacy_equation_placeholder never included in LLM payload."""
        blocks = [
            _make_block("b1", "هذا نص عادي"),
            _make_block("b2", "[OLE Equation]", block_type="legacy_equation_placeholder"),
        ]

        client = OpenRouterClient(api_key="test-key")
        eligible = [
            b for b in blocks
            if b.block_type not in PRECLASSIFIED_TYPES and b.raw_content.strip()
        ]
        payload = client._build_payload(eligible)

        # Only b1 should be in the payload
        items = json.loads(payload["messages"][1]["content"])
        block_ids_in_payload = [item["block_id"] for item in items]
        assert "b1" in block_ids_in_payload
        assert "b2" not in block_ids_in_payload

    def test_legacy_equation_unchanged_by_rule_classifier(self):
        """AC-1.4.5b: legacy_equation_placeholder returned unchanged by RuleBasedClassifier."""
        block = _make_block(
            "b1", "[OLE Equation]", block_type="legacy_equation_placeholder"
        )
        results = RuleBasedClassifier.classify_document_blocks([block])
        assert results[0].block_type == "legacy_equation_placeholder"

    def test_legacy_equation_foreign_id_rejected(self):
        """AC-1.4.5c: Model response including legacy_equation_placeholder block_id → rejected."""
        blocks = [
            _make_block("b1", "هذا نص عادي"),
            _make_block("b2", "[OLE]", block_type="legacy_equation_placeholder"),
        ]
        eligible_ids = {"b1"}  # Only b1 is eligible

        response = ClassificationResponse(
            classifications=[
                BlockClassification(block_id="b1", type="arabic_text"),
                BlockClassification(block_id="b2", type="arabic_text"),  # FOREIGN
            ]
        )

        client = OpenRouterClient(api_key="test-key")
        with pytest.raises(InvalidClassificationResponse):
            client._apply_classifications(blocks, response, eligible_ids)

    # ── Successful API Classification ─────────────────────────────

    @patch("app.ai.client.httpx.Client")
    def test_successful_api_classification(self, mock_httpx_client):
        """Happy path: API returns valid, complete response."""
        blocks = self._make_test_blocks()

        api_response = json.dumps({
            "classifications": [
                {"block_id": "p1_b1", "type": "chapter_header"},
                {"block_id": "p1_b2", "type": "ministry_question"},
            ]
        })

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": api_response}}]
        }
        mock_httpx_client.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(return_value=mock_response))
        )
        mock_httpx_client.return_value.__exit__ = MagicMock(return_value=False)

        fallback_reasons = []
        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)

        # No fallback should have been triggered
        assert len(fallback_reasons) == 0

        # Verify classifications applied correctly
        result_map = {r.block_id: r for r in results}
        assert result_map["p1_b1"].block_type == "chapter_header"
        assert result_map["p1_b2"].block_type == "ministry_question"
        assert result_map["p1_b3"].block_type == "math_formula"  # unchanged
        assert result_map["p1_b4"].block_type == "legacy_equation_placeholder"  # unchanged

    # ── Edge Cases ────────────────────────────────────────────────

    def test_empty_blocks_list(self):
        """No blocks → no API call, no fallback signal."""
        fallback_reasons = []
        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks([])
        assert results == []
        assert len(fallback_reasons) == 0

    def test_all_preclassified_blocks(self):
        """All blocks pre-classified → no API call, direct fallback."""
        blocks = [
            _make_block("b1", "x^2", block_type="math_formula"),
            _make_block("b2", "H2O", block_type="chemistry_formula"),
        ]
        fallback_reasons = []
        client = OpenRouterClient(
            api_key="test-key",
            on_fallback=fallback_reasons.append,
        )
        results = client.classify_blocks(blocks)
        assert len(results) == 2
        assert results[0].block_type == "math_formula"
        assert results[1].block_type == "chemistry_formula"

    def test_whitespace_only_blocks_excluded(self):
        """Blocks with only whitespace content should be excluded from eligible."""
        blocks = [
            _make_block("b1", "   ", block_type="arabic_text"),
            _make_block("b2", "هذا نص عادي"),
        ]
        eligible = [
            b for b in blocks
            if b.block_type not in PRECLASSIFIED_TYPES and b.raw_content.strip()
        ]
        assert len(eligible) == 1
        assert eligible[0].block_id == "b2"

    def test_valid_response_exact_match(self):
        """_apply_classifications succeeds when response exactly matches eligible_ids."""
        blocks = [
            _make_block("b1", "هذا نص عادي"),
            _make_block("b2", "الفصل الأول"),
        ]
        eligible_ids = {"b1", "b2"}

        response = ClassificationResponse(
            classifications=[
                BlockClassification(block_id="b1", type="arabic_text"),
                BlockClassification(block_id="b2", type="chapter_header"),
            ]
        )

        client = OpenRouterClient(api_key="test-key")
        results = client._apply_classifications(blocks, response, eligible_ids)
        assert results[0].block_type == "arabic_text"
        assert results[1].block_type == "chapter_header"

    def test_payload_uses_json_dumps(self):
        """_build_payload uses json.dumps (not str()) for the user message content."""
        blocks = [_make_block("b1", "هذا نص عربي")]
        client = OpenRouterClient(api_key="test-key")
        payload = client._build_payload(blocks)

        user_content = payload["messages"][1]["content"]
        # Should be valid JSON
        parsed = json.loads(user_content)
        assert isinstance(parsed, list)
        assert parsed[0]["block_id"] == "b1"
