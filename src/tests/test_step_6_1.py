"""
Booklet_AI Desktop — STEP-6.1 Tests

Comprehensive tests for the AI Configuration UI (STEP-6.1):
- ConnectionTestResult / ModelNameValidationResult data contracts
- validate_model_name_locally() — AC-6.1.6
- test_connection() — AC-6.1.2, AC-6.1.2b, AC-6.1.3, AC-6.1.5
- OpenRouterClient model_provider dynamic resolution — AC-6.1.1b
- API key persistence via SecretsManager — AC-6.1.1
- Key masking — AC-6.1.4
"""

import json
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

# ─── Data Models ──────────────────────────────────────────────────────

from app.ui.ai_settings import (
    ConnectionTestResult,
    ConnectionTestWorker,
    ModelNameValidationResult,
    _mask_key,
    test_connection,
    validate_model_name_locally,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Pydantic Model Tests
# ═══════════════════════════════════════════════════════════════════════


class TestConnectionTestResult:
    """Verify the ConnectionTestResult data contract."""

    def test_success_result(self):
        result = ConnectionTestResult(
            success=True,
            failure_reason=None,
            message="Connected successfully.",
            latency_ms=123.4,
        )
        assert result.success is True
        assert result.failure_reason is None
        assert result.latency_ms == 123.4

    def test_failure_result_with_reason(self):
        result = ConnectionTestResult(
            success=False,
            failure_reason="invalid_key",
            message="Key is invalid.",
        )
        assert result.success is False
        assert result.failure_reason == "invalid_key"
        assert result.latency_ms is None

    def test_frozen_immutability(self):
        result = ConnectionTestResult(
            success=True, failure_reason=None, message="ok"
        )
        with pytest.raises(ValidationError):
            result.success = False  # type: ignore[misc]

    def test_all_failure_reasons_accepted(self):
        reasons = [
            "missing_key", "invalid_key", "unreachable_endpoint",
            "unknown_model", "structured_output_unsupported",
            "timeout", "rate_limited", "unknown_error", None,
        ]
        for reason in reasons:
            r = ConnectionTestResult(
                success=reason is None,
                failure_reason=reason,
                message=f"Test: {reason}",
            )
            assert r.failure_reason == reason

    def test_invalid_failure_reason_rejected(self):
        with pytest.raises(ValidationError):
            ConnectionTestResult(
                success=False,
                failure_reason="not_a_valid_reason",  # type: ignore[arg-type]
                message="bad",
            )


class TestModelNameValidationResult:
    """Verify the ModelNameValidationResult data contract."""

    def test_valid_result(self):
        r = ModelNameValidationResult(valid=True, message="ok")
        assert r.valid is True

    def test_frozen_immutability(self):
        r = ModelNameValidationResult(valid=True, message="ok")
        with pytest.raises(ValidationError):
            r.valid = False  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# 2. validate_model_name_locally() — AC-6.1.6
# ═══════════════════════════════════════════════════════════════════════


class TestValidateModelNameLocally:
    """AC-6.1.6: local-only validation, no network calls."""

    def test_empty_string_rejected(self):
        r = validate_model_name_locally("")
        assert r.valid is False

    def test_whitespace_only_rejected(self):
        r = validate_model_name_locally("   ")
        assert r.valid is False

    def test_none_input_rejected(self):
        r = validate_model_name_locally(None)  # type: ignore[arg-type]
        assert r.valid is False

    def test_no_slash_rejected(self):
        r = validate_model_name_locally("gemini-2.0-flash")
        assert r.valid is False
        assert "/" in r.message or "provider/model" in r.message

    def test_wellformed_slug_accepted(self):
        r = validate_model_name_locally("google/gemini-2.0-flash-001")
        assert r.valid is True

    def test_openai_slug_accepted(self):
        r = validate_model_name_locally("openai/gpt-4o")
        assert r.valid is True

    def test_slug_with_dots_accepted(self):
        r = validate_model_name_locally("meta-llama/llama-3.1-70b-instruct")
        assert r.valid is True

    def test_whitespace_trimmed(self):
        r = validate_model_name_locally("  google/gemini-2.0-flash-001  ")
        assert r.valid is True

    def test_special_chars_rejected(self):
        r = validate_model_name_locally("google/gemini 2.0 flash")
        assert r.valid is False

    def test_no_network_call_made(self):
        """Verify no network activity happens during validation."""
        with patch("httpx.Client") as mock_client:
            validate_model_name_locally("google/gemini-2.0-flash-001")
            mock_client.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 3. test_connection() — AC-6.1.2, AC-6.1.2b, AC-6.1.3
# ═══════════════════════════════════════════════════════════════════════


class TestTestConnection:
    """AC-6.1.2: test_connection returns distinct failure reasons."""

    def test_missing_key(self):
        """AC-6.1.2: missing_key when API key is empty."""
        result = test_connection("", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "missing_key"

    def test_missing_key_whitespace(self):
        result = test_connection("   ", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "missing_key"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_invalid_key_401(self, mock_client_cls):
        """AC-6.1.2: invalid_key on HTTP 401."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-bad-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "invalid_key"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_unknown_model_404(self, mock_client_cls):
        """AC-6.1.2: unknown_model on HTTP 404."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "fake/nonexistent-model")
        assert result.success is False
        assert result.failure_reason == "unknown_model"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_rate_limited_429(self, mock_client_cls):
        """AC-6.1.2: rate_limited on HTTP 429."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "rate_limited"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_timeout(self, mock_client_cls):
        """AC-6.1.2: timeout when request exceeds 15s."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.TimeoutException("timed out")
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "timeout"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_unreachable_endpoint(self, mock_client_cls):
        """AC-6.1.2: unreachable_endpoint on connection failure."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "unreachable_endpoint"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_structured_output_unsupported_invalid_json(self, mock_client_cls):
        """AC-6.1.2b: structured_output_unsupported when response is not JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "This is plain text, not JSON"
                    }
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "structured_output_unsupported"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_structured_output_unsupported_missing_choices(self, mock_client_cls):
        """AC-6.1.2b: structured_output_unsupported when response lacks choices."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"error": "something"}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "structured_output_unsupported"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_success_with_valid_json_response(self, mock_client_cls):
        """AC-6.1.2: success when model returns valid JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"status": "ok"}'
                    }
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-valid-key", "google/gemini-2.0-flash-001")
        assert result.success is True
        assert result.failure_reason is None
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    @patch("app.ui.ai_settings.httpx.Client")
    def test_success_payload_includes_response_format(self, mock_client_cls):
        """AC-6.1.2b: test_connection sends response_format: json_object."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"status": "ok"}'}}]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        test_connection("sk-key", "google/gemini-2.0-flash-001")

        # Verify the posted payload
        call_args = mock_client.post.call_args
        posted_json = call_args.kwargs.get("json") or call_args[1].get("json")
        assert posted_json["response_format"] == {"type": "json_object"}
        assert posted_json["model"] == "google/gemini-2.0-flash-001"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_success_posts_to_correct_endpoint(self, mock_client_cls):
        """test_connection posts to the same endpoint as production."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"status": "ok"}'}}]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        test_connection("sk-key", "google/gemini-2.0-flash-001")

        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url")
        assert url == "https://openrouter.ai/api/v1/chat/completions"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_server_error_500(self, mock_client_cls):
        """AC-6.1.2: generic server error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason_phrase = "Internal Server Error"
        mock_response.json.side_effect = Exception("no json")
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is False
        assert result.failure_reason == "unknown_error"


# ═══════════════════════════════════════════════════════════════════════
# 4. AC-6.1.3 — test_connection never classifies real content
# ═══════════════════════════════════════════════════════════════════════


class TestTestConnectionIsolation:
    """AC-6.1.3: test_connection must never call classify_blocks or
    accept real ContentBlocks.
    """

    def test_no_classify_blocks_import(self):
        """Verify test_connection source does not import or call classify_blocks."""
        import inspect
        source = inspect.getsource(test_connection)
        doc = test_connection.__doc__ or ""
        code_only = source.replace(doc, "")
        assert "classify_blocks" not in code_only

    def test_no_content_block_parameter(self):
        """Verify test_connection signature has no ContentBlock parameter."""
        import inspect
        sig = inspect.signature(test_connection)
        param_names = list(sig.parameters.keys())
        assert param_names == ["api_key", "model"]

    def test_synthetic_payload_only(self):
        """Verify the payload contains synthetic test content, not real blocks."""
        with patch("app.ui.ai_settings.httpx.Client") as mock_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "choices": [{"message": {"content": '{"status": "ok"}'}}]
            }
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            test_connection("sk-key", "google/gemini-2.0-flash-001")

            posted_json = mock_client.post.call_args.kwargs.get("json") or \
                          mock_client.post.call_args[1].get("json")
            user_content = posted_json["messages"][1]["content"]
            parsed = json.loads(user_content)
            # Must be synthetic test content
            assert len(parsed) == 1
            assert parsed[0]["block_id"] == "test_probe_0"


# ═══════════════════════════════════════════════════════════════════════
# 5. AC-6.1.4 — API key never in plaintext in logs
# ═══════════════════════════════════════════════════════════════════════


class TestKeyMasking:
    """AC-6.1.4: API key masking."""

    def test_mask_short_key(self):
        assert _mask_key("abc") == "***"

    def test_mask_empty_key(self):
        assert _mask_key("") == "(empty)"

    def test_mask_long_key(self):
        key = "sk-or-1234567890abcdef"
        masked = _mask_key(key)
        assert key not in masked
        assert masked.startswith("sk-o")
        assert masked.endswith("cdef")
        assert "*" in masked

    def test_mask_exactly_8_chars(self):
        assert _mask_key("12345678") == "********"


# ═══════════════════════════════════════════════════════════════════════
# 6. AC-6.1.1b — OpenRouterClient reads model at call time
# ═══════════════════════════════════════════════════════════════════════


class TestOpenRouterClientModelProvider:
    """AC-6.1.1b: changing model in AppStore is reflected in the next
    classify_blocks() call without restart.
    """

    def test_model_provider_overrides_static_model(self):
        """model_provider callable takes precedence over constructor model."""
        from app.ai.client import OpenRouterClient

        client = OpenRouterClient(
            api_key="sk-test",
            model="default/model",
            model_provider=lambda: "dynamic/model-v2",
        )
        assert client._resolve_model() == "dynamic/model-v2"

    def test_model_provider_none_uses_static(self):
        """When no model_provider, static model is used."""
        from app.ai.client import OpenRouterClient

        client = OpenRouterClient(api_key="sk-test", model="static/model")
        assert client._resolve_model() == "static/model"

    def test_model_provider_empty_falls_back_to_static(self):
        """When model_provider returns empty, falls back to static."""
        from app.ai.client import OpenRouterClient

        client = OpenRouterClient(
            api_key="sk-test",
            model="fallback/model",
            model_provider=lambda: "",
        )
        assert client._resolve_model() == "fallback/model"

    def test_model_provider_exception_falls_back_to_static(self):
        """When model_provider raises, falls back to static."""
        from app.ai.client import OpenRouterClient

        def broken_provider():
            raise RuntimeError("AppStore crashed")

        client = OpenRouterClient(
            api_key="sk-test",
            model="safe/model",
            model_provider=broken_provider,
        )
        assert client._resolve_model() == "safe/model"

    def test_model_change_reflected_between_calls(self):
        """AC-6.1.1b: changing AppStore model is reflected in the next call's
        outbound payload.
        """
        from app.ai.client import OpenRouterClient
        from app.core.models import ContentBlock

        # Simulate an AppStore-like mutable holder
        model_holder = {"model": "google/gemini-2.0-flash-001"}
        client = OpenRouterClient(
            api_key="sk-test",
            model="default/unused",
            model_provider=lambda: model_holder["model"],
        )

        # First call: resolve to initial model
        assert client._resolve_model() == "google/gemini-2.0-flash-001"

        # Change the model (simulating AppStore.ai_model_name = ...)
        model_holder["model"] = "openai/gpt-4o"

        # Second call: must use the NEW model without restart
        assert client._resolve_model() == "openai/gpt-4o"

    @patch("app.ai.client.httpx.Client")
    def test_build_payload_uses_resolved_model(self, mock_client_cls):
        """AC-6.1.1b: _build_payload uses _resolve_model(), not self.model."""
        from app.ai.client import OpenRouterClient
        from app.core.models import ContentBlock

        model_holder = {"model": "anthropic/claude-3-opus"}
        client = OpenRouterClient(
            api_key="sk-test",
            model="default/unused",
            model_provider=lambda: model_holder["model"],
        )

        # Create a minimal block for payload building
        block = ContentBlock(
            block_id="b1",
            raw_content="Test content",
            content_hash="abc123",
            block_type="arabic_text",
            source_page=1,
        )

        payload = client._build_payload([block])
        assert payload["model"] == "anthropic/claude-3-opus"

        # Now change
        model_holder["model"] = "openai/gpt-4o"
        payload2 = client._build_payload([block])
        assert payload2["model"] == "openai/gpt-4o"


# ═══════════════════════════════════════════════════════════════════════
# 7. AC-6.1.1 — API key persisted via keyring
# ═══════════════════════════════════════════════════════════════════════


class TestAPIKeyPersistence:
    """AC-6.1.1: API key persistence through SecretsManager/keyring."""

    def test_set_and_get_key(self):
        """Saving a key makes it immediately retrievable."""
        from app.utils.secrets_manager import SecretsManager

        with patch("app.utils.secrets_manager.keyring") as mock_keyring:
            stored = {}
            mock_keyring.get_password.side_effect = (
                lambda svc, prov: stored.get(f"{svc}:{prov}")
            )
            mock_keyring.set_password.side_effect = (
                lambda svc, prov, key: stored.update({f"{svc}:{prov}": key})
            )

            sm = SecretsManager()
            sm.set_api_key("openrouter", "sk-new-key-123")
            retrieved = sm.get_api_key("openrouter")
            assert retrieved == "sk-new-key-123"

    def test_key_immediately_available_to_client(self):
        """AC-6.1.1: after save, OpenRouterClient can use the new key."""
        from app.utils.secrets_manager import SecretsManager

        with patch("app.utils.secrets_manager.keyring") as mock_keyring:
            stored = {}
            mock_keyring.get_password.side_effect = (
                lambda svc, prov: stored.get(f"{svc}:{prov}")
            )
            mock_keyring.set_password.side_effect = (
                lambda svc, prov, key: stored.update({f"{svc}:{prov}": key})
            )

            sm = SecretsManager()

            # No key initially
            assert sm.get_api_key("openrouter") is None

            # Save a new key
            sm.set_api_key("openrouter", "sk-or-fresh-key")

            # Immediately available
            key = sm.get_api_key("openrouter")
            assert key == "sk-or-fresh-key"


# ═══════════════════════════════════════════════════════════════════════
# 8. AC-6.1.5 — test_connection never on Qt main thread
# ═══════════════════════════════════════════════════════════════════════


class TestConnectionTestWorker:
    """AC-6.1.5: ConnectionTestWorker runs off the Qt main thread.

    These tests verify the worker's interface and behavior without
    requiring a running QApplication (we mock the QThread mechanics).
    """

    def test_worker_has_result_ready_signal(self):
        """Worker exposes result_ready signal."""
        assert hasattr(ConnectionTestWorker, "result_ready")

    def test_worker_run_calls_test_connection(self):
        """Worker.run() delegates to test_connection."""
        with patch("app.ui.ai_settings.test_connection") as mock_test:
            mock_test.return_value = ConnectionTestResult(
                success=True, failure_reason=None, message="ok"
            )
            worker = ConnectionTestWorker.__new__(ConnectionTestWorker)
            worker._api_key = "sk-key"
            worker._model = "google/gemini-2.0-flash-001"
            worker.result_ready = MagicMock()

            worker.run()

            mock_test.assert_called_once_with("sk-key", "google/gemini-2.0-flash-001")
            worker.result_ready.emit.assert_called_once()
            emitted = worker.result_ready.emit.call_args[0][0]
            assert emitted["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# 9. AC-6.1.2b — Model rejecting structured output is caught
# ═══════════════════════════════════════════════════════════════════════


class TestStructuredOutputValidation:
    """AC-6.1.2b: A model that accepts a generic request but rejects
    response_format: json_object is reported as
    failure_reason='structured_output_unsupported', not as generic success.
    """

    @patch("app.ui.ai_settings.httpx.Client")
    def test_model_returns_non_json_text(self, mock_client_cls):
        """Model responds with 200 but plaintext content → caught."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Sure! Here is my analysis of the text blocks..."
                    }
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "model/that-ignores-json-mode")
        assert result.success is False
        assert result.failure_reason == "structured_output_unsupported"

    @patch("app.ui.ai_settings.httpx.Client")
    def test_model_returns_valid_json(self, mock_client_cls):
        """Model responds with 200 AND valid JSON → success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"classifications": [{"block_id": "test_probe_0", "type": "arabic_text"}]}'
                    }
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "google/gemini-2.0-flash-001")
        assert result.success is True
        assert result.failure_reason is None

    @patch("app.ui.ai_settings.httpx.Client")
    def test_model_returns_error_json_body(self, mock_client_cls):
        """Model returns 400 with JSON error body."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.reason_phrase = "Bad Request"
        mock_response.json.return_value = {
            "error": {
                "message": "This model does not support response_format"
            }
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = test_connection("sk-key", "model/no-json-support")
        assert result.success is False
        # HTTP 400 → unknown_error (not 401/404/429)
        assert result.failure_reason == "unknown_error"
        assert "response_format" in result.message


# ═══════════════════════════════════════════════════════════════════════
# 10. Backward Compatibility — existing client tests still work
# ═══════════════════════════════════════════════════════════════════════


class TestBackwardCompatibility:
    """Ensure the OpenRouterClient model_provider parameter is optional
    and existing usage patterns (without model_provider) still work.
    """

    def test_client_without_model_provider(self):
        """Client works without model_provider (backward compat)."""
        from app.ai.client import OpenRouterClient
        client = OpenRouterClient(api_key="sk-test", model="google/gemini-2.0-flash-001")
        assert client._resolve_model() == "google/gemini-2.0-flash-001"
        assert client.model_provider is None

    def test_client_on_fallback_still_works(self):
        """on_fallback parameter unchanged."""
        from app.ai.client import OpenRouterClient
        callback = MagicMock()
        client = OpenRouterClient(
            api_key="sk-test",
            model="google/gemini-2.0-flash-001",
            on_fallback=callback,
        )
        assert client.on_fallback is callback


# ═══════════════════════════════════════════════════════════════════════
# 11. AISettingsWidget UI Component Tests (AC-6.1.1, AC-6.1.1b, AC-6.1.4, AC-6.1.6)
# ═══════════════════════════════════════════════════════════════════════


class TestAISettingsWidgetUI:
    """Verify AISettingsWidget user interaction, button states, and masking."""

    @pytest.fixture(autouse=True)
    def setup_qapp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication(["--platform", "offscreen"])

    def test_widget_initialization(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget
        from PySide6.QtWidgets import QLineEdit

        app_store = AppStore()
        app_store.ai_model_name = "test/model-1"
        secrets = MagicMock(spec=SecretsManager)
        secrets.has_api_key.return_value = True

        widget = AISettingsWidget(app_store, secrets)
        assert widget._model_input.text() == "test/model-1"
        assert widget._key_input.echoMode() == QLineEdit.EchoMode.Password
        assert "Windows Credential Manager" in widget._key_status.text()

    def test_toggle_reveal(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget
        from PySide6.QtWidgets import QLineEdit

        widget = AISettingsWidget(AppStore(), MagicMock(spec=SecretsManager))
        assert widget._key_input.echoMode() == QLineEdit.EchoMode.Password

        widget._reveal_btn.setChecked(True)
        assert widget._key_input.echoMode() == QLineEdit.EchoMode.Normal

        widget._reveal_btn.setChecked(False)
        assert widget._key_input.echoMode() == QLineEdit.EchoMode.Password

    def test_save_api_key(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget

        secrets = MagicMock(spec=SecretsManager)
        widget = AISettingsWidget(AppStore(), secrets)
        widget._key_input.setText("sk-new-key-xyz")
        widget._save_key_btn.click()

        secrets.set_api_key.assert_called_once_with("openrouter", "sk-new-key-xyz")
        assert widget._key_input.text() == ""
        assert "بنجاح" in widget._key_status.text()

    def test_save_model_name_valid(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget

        app_store = AppStore()
        widget = AISettingsWidget(app_store, MagicMock(spec=SecretsManager))
        widget._model_input.setText("openai/gpt-4o")
        widget._save_model_btn.click()

        assert app_store.ai_model_name == "openai/gpt-4o"
        assert "تم حفظ الموديل" in widget._model_status.text()

    def test_save_model_name_invalid(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget

        app_store = AppStore()
        widget = AISettingsWidget(app_store, MagicMock(spec=SecretsManager))
        widget._model_input.setText("invalid_slug_no_slash")
        widget._save_model_btn.click()

        assert app_store.ai_model_name != "invalid_slug_no_slash"
        assert "صيغة غير صالحة" in widget._model_status.text()

    def test_run_test_connection_invalid_model(self):
        from app.state.app_store import AppStore
        from app.utils.secrets_manager import SecretsManager
        from app.ui.ai_settings import AISettingsWidget

        widget = AISettingsWidget(AppStore(), MagicMock(spec=SecretsManager))
        widget._model_input.setText("bad_model")
        widget._run_test_connection()

        assert "صيغة غير صالحة" in widget._test_status.text()
        assert widget._test_btn.isEnabled() is True

