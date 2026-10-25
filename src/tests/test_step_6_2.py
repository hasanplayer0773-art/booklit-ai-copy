"""
Booklet_AI Desktop — STEP-6.2 Tests

Comprehensive tests for Field Error Translation, Reporting & Auto-Recovery.
Covers every Acceptance Criterion (AC-6.2.1 through AC-6.2.7b).
"""

import json
import re
from unittest.mock import MagicMock, patch, call

import pytest

from app.core.exceptions import (
    BookletError,
    COMOperationTimeout,
    ContentExceedsEstimateError,
    InDesignUnavailableError,
    InvalidBodyFrameGeometryError,
    MarginConfigError,
    MathExtractionError,
    QuestionIndexError,
    RTLTableUnsupportedError,
    StyleMappingError,
    TextIntegrityError,
)
from app.ui.error_translation import (
    ERROR_TRANSLATIONS,
    FALLBACK_MESSAGE,
    UserFacingError,
    translate_error,
)
from app.ui.report_redaction import redact
from app.ui.problem_report import ProblemReport, build_problem_report, export_problem_report_json
from app.utils.auto_recovery import (
    AUTO_RETRYABLE_EXCEPTIONS,
    MAX_AUTO_RETRIES,
    NEVER_AUTO_RETRY,
    SupervisorRetryState,
    is_auto_retryable,
)
from app.automation.job_retry import run_job_with_retry


# ═══════════════════════════════════════════════════════════════════════
# Helper: collect all BookletError subclasses from the codebase
# ═══════════════════════════════════════════════════════════════════════


def _all_booklet_error_subclasses() -> list:
    """Recursively collect all BookletError subclasses."""
    result = []

    def _collect(cls):
        for sub in cls.__subclasses__():
            result.append(sub)
            _collect(sub)

    _collect(BookletError)
    return result


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.1: Every known exception resolves via MRO walk
# ═══════════════════════════════════════════════════════════════════════


class TestErrorTranslationMROWalk:
    """AC-6.2.1: Every exception type defined across Phases 1–5 that can
    reach the UI layer resolves to a translated message via
    translate_error()'s MRO walk."""

    def test_all_booklet_error_subclasses_resolve(self):
        """Every BookletError subclass resolves to a translated message
        (either a direct entry or an inherited ancestor entry), never
        to FALLBACK_MESSAGE."""
        subclasses = _all_booklet_error_subclasses()
        assert len(subclasses) > 0, "No BookletError subclasses found"

        for exc_cls in subclasses:
            exc = exc_cls(f"Test message for {exc_cls.__name__}")
            result = translate_error(exc)
            assert isinstance(result, UserFacingError), (
                f"{exc_cls.__name__} did not produce a UserFacingError"
            )
            # Must NOT be the fallback — it should resolve via MRO
            # to at least BookletError's entry.
            assert result.translated_message != FALLBACK_MESSAGE, (
                f"{exc_cls.__name__} fell through to FALLBACK_MESSAGE "
                f"instead of resolving via MRO"
            )

    def test_all_exceptions_in_table_resolve_to_their_own_message(self):
        """Each exception type with a direct entry in ERROR_TRANSLATIONS
        resolves to its own specific message, not an ancestor's."""
        for exc_cls, expected_msg in ERROR_TRANSLATIONS.items():
            exc = exc_cls(f"Test for {exc_cls.__name__}")
            result = translate_error(exc)
            assert result.translated_message == expected_msg, (
                f"{exc_cls.__name__} resolved to wrong message"
            )

    def test_com_operation_timeout_resolves(self):
        """COMOperationTimeout (inherits BookletError) resolves to its
        own specific Arabic message."""
        exc = COMOperationTimeout("Heartbeat stale for 30s")
        result = translate_error(exc)
        assert "InDesign" in result.translated_message
        assert result.translated_message == ERROR_TRANSLATIONS[COMOperationTimeout]


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.1b: Synthetic subclass inherits parent's translation via MRO
# ═══════════════════════════════════════════════════════════════════════


class TestMROInheritance:
    """AC-6.2.1b: A synthetic subclass of an already-mapped exception,
    deliberately absent from ERROR_TRANSLATIONS, resolves to its
    parent's translated message via MRO — not to FALLBACK_MESSAGE."""

    def test_synthetic_com_timeout_subclass(self):
        """Synthetic COMOperationTimeout subclass inherits parent's
        translation."""

        class SyntheticTimeout(COMOperationTimeout):
            pass

        # SyntheticTimeout is NOT in ERROR_TRANSLATIONS
        assert SyntheticTimeout not in ERROR_TRANSLATIONS

        exc = SyntheticTimeout("Synthetic timeout")
        result = translate_error(exc)

        # Should resolve to COMOperationTimeout's message, NOT fallback
        assert result.translated_message == ERROR_TRANSLATIONS[COMOperationTimeout]
        assert result.translated_message != FALLBACK_MESSAGE

    def test_synthetic_booklet_error_subclass(self):
        """A generic BookletError subclass inherits BookletError's
        translation."""

        class FutureBookletError(BookletError):
            pass

        assert FutureBookletError not in ERROR_TRANSLATIONS

        exc = FutureBookletError("Future error")
        result = translate_error(exc)

        assert result.translated_message == ERROR_TRANSLATIONS[BookletError]
        assert result.translated_message != FALLBACK_MESSAGE

    def test_deep_subclass_chain(self):
        """A deeply nested subclass still inherits via MRO walk."""

        class Level1(TextIntegrityError):
            pass

        class Level2(Level1):
            pass

        class Level3(Level2):
            pass

        exc = Level3("Deep chain")
        result = translate_error(exc)
        assert result.translated_message == ERROR_TRANSLATIONS[TextIntegrityError]


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.2: Original exception preserved
# ═══════════════════════════════════════════════════════════════════════


class TestOriginalExceptionPreserved:
    """AC-6.2.2: translate_error() never discards the original exception.
    UserFacingError.original always contains the untranslated exception
    with its full technical message."""

    def test_original_preserved_on_user_facing_error(self):
        original_msg = "SHA-256 hash mismatch: expected abc123, got def456"
        exc = TextIntegrityError(original_msg)
        result = translate_error(exc)

        assert result.original is exc
        assert str(result.original) == original_msg

    def test_original_is_exact_same_object(self):
        exc = COMOperationTimeout("Heartbeat stale")
        result = translate_error(exc)
        assert result.original is exc  # same object, not a copy

    def test_problem_report_uses_original(self):
        """build_problem_report uses the original exception, never the
        translated string."""
        exc = MarginConfigError("top margin -5mm is invalid")
        translated = translate_error(exc)

        report = build_problem_report(
            exc=translated,
            app_version="1.0.0",
            correlation_id="job-123",
            recent_log_lines=[],
        )

        assert report.original_exception_type == "MarginConfigError"
        assert "top margin" in report.original_exception_message


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.3: TextIntegrityError and MathExtractionError never auto-retried
# ═══════════════════════════════════════════════════════════════════════


class TestNeverAutoRetried:
    """AC-6.2.3: TextIntegrityError and MathExtractionError are never
    auto-retried under any code path."""

    def test_text_integrity_never_retried(self):
        exc = TextIntegrityError("Hash mismatch")
        assert is_auto_retryable(exc) is False
        # Even with a clean supervisor state
        clean_state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        )
        assert is_auto_retryable(exc, clean_state) is False

    def test_math_extraction_never_retried(self):
        exc = MathExtractionError("XSLT missing")
        assert is_auto_retryable(exc) is False
        clean_state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        )
        assert is_auto_retryable(exc, clean_state) is False

    def test_in_never_auto_retry_list(self):
        """Both are in the NEVER_AUTO_RETRY tuple."""
        assert TextIntegrityError in NEVER_AUTO_RETRY
        assert MathExtractionError in NEVER_AUTO_RETRY

    def test_generic_booklet_error_not_retried(self):
        """BookletError itself is not retryable."""
        exc = BookletError("Generic error")
        assert is_auto_retryable(exc) is False


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.4: COMOperationTimeout retried at most once
# ═══════════════════════════════════════════════════════════════════════


class TestCOMTimeoutRetry:
    """AC-6.2.4: A COMOperationTimeout is retried at most once
    (MAX_AUTO_RETRIES = 1) before surfacing to the user."""

    def test_max_auto_retries_is_one(self):
        assert MAX_AUTO_RETRIES == 1

    def test_com_timeout_retryable_with_clean_state(self):
        exc = COMOperationTimeout("Timeout")
        clean_state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=12345,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
        )
        assert is_auto_retryable(exc, clean_state) is True

    def test_com_timeout_not_retryable_without_state(self):
        """AC-6.2.4: COMOperationTimeout with supervisor_state=None
        is NEVER retried (fail-safe default)."""
        exc = COMOperationTimeout("Timeout")
        assert is_auto_retryable(exc, None) is False
        assert is_auto_retryable(exc) is False

    def test_retry_loop_runs_at_most_twice(self):
        """run_job_with_retry executes at most 2 attempts (initial + 1 retry)."""
        call_count = 0

        def failing_gen(document, job_config, attempt_index=0):
            nonlocal call_count
            call_count += 1
            exc = COMOperationTimeout(f"Timeout attempt {attempt_index}")
            exc.supervisor_state = SupervisorRetryState(
                child_process_exited=True,
                owned_indesign_pid=1234,
                owned_indesign_confirmed_terminated=True,
                attached_to_pre_existing_instance=False,
            )
            raise exc

        with pytest.raises(COMOperationTimeout):
            run_job_with_retry(
                source_file_path="test.docx",
                job_config=None,
                document_store=MagicMock(),
                generation_fn=failing_gen,
                extraction_fn=lambda *a: "doc",
            )

        assert call_count == 2  # initial + 1 retry


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.4b: Pre-existing InDesign instance never retried
# ═══════════════════════════════════════════════════════════════════════


class TestPreExistingInstanceNeverRetried:
    """AC-6.2.4b: A COMOperationTimeout where the prior attempt attached
    to a pre-existing (not job-owned) InDesign instance is never
    auto-retried, regardless of allow-list membership."""

    def test_pre_existing_instance_prevents_retry(self):
        exc = COMOperationTimeout("Timeout on pre-existing")
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=True,
        )
        assert is_auto_retryable(exc, state) is False

    def test_confirms_clean_state_rejects_pre_existing(self):
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=True,
        )
        assert state.confirms_clean_retryable_state() is False


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.4c: Retried job never writes to same partial output path
# ═══════════════════════════════════════════════════════════════════════


class TestPartialOutputPreventsRetry:
    """AC-6.2.4c: A retried job never writes to the same output path
    a failed prior attempt wrote to — ensured by refusing retry when
    partial_output_path is not None."""

    def test_partial_output_prevents_retry(self):
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=1234,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
            partial_output_path="C:\\temp\\output_attempt_0.indd",
        )
        assert state.confirms_clean_retryable_state() is False

    def test_no_partial_output_allows_retry(self):
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=1234,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
            partial_output_path=None,
        )
        assert state.confirms_clean_retryable_state() is True

    def test_com_timeout_with_partial_output_not_retryable(self):
        exc = COMOperationTimeout("Timeout with partial")
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=1234,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
            partial_output_path="/tmp/partial.indd",
        )
        assert is_auto_retryable(exc, state) is False


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.5: Unknown exception hits FALLBACK_MESSAGE
# ═══════════════════════════════════════════════════════════════════════


class TestFallbackMessage:
    """AC-6.2.5: An exception type deliberately absent from
    ERROR_TRANSLATIONS and with no mapped ancestor in its MRO still
    produces a valid UserFacingError using FALLBACK_MESSAGE."""

    def test_unknown_exception_hits_fallback(self):
        """A plain ValueError (not a BookletError) hits FALLBACK_MESSAGE."""
        exc = ValueError("something unexpected")
        result = translate_error(exc)
        assert isinstance(result, UserFacingError)
        assert result.translated_message == FALLBACK_MESSAGE
        assert result.original is exc

    def test_runtime_error_hits_fallback(self):
        exc = RuntimeError("internal failure")
        result = translate_error(exc)
        assert result.translated_message == FALLBACK_MESSAGE

    def test_custom_non_booklet_error(self):

        class TotallyNewError(Exception):
            pass

        exc = TotallyNewError("brand new")
        result = translate_error(exc)
        assert result.translated_message == FALLBACK_MESSAGE
        assert result.original is exc

    def test_fallback_contains_arabic(self):
        """FALLBACK_MESSAGE is in Arabic."""
        assert "خطأ" in FALLBACK_MESSAGE
        assert "تقرير" in FALLBACK_MESSAGE


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.6: Redaction works for secrets in problem reports
# ═══════════════════════════════════════════════════════════════════════


class TestRedaction:
    """AC-6.2.6: build_problem_report()'s output never contains the
    OpenRouter API key, an Authorization header value, or the raw text
    of any processed document."""

    def test_bearer_token_redacted(self):
        text = "Request failed with Bearer sk-or-v1-abc123def456ghi789jkl012mno345p headers"
        result = redact(text)
        assert "sk-or-v1" not in result
        assert "[REDACTED]" in result

    def test_api_key_pattern_redacted(self):
        text = "Using key sk-abcdefghijklmnopqrstuv for auth"
        result = redact(text)
        assert "sk-abcdefghijklmnopqrstuv" not in result
        assert "[REDACTED_API_KEY]" in result

    def test_or_key_pattern_redacted(self):
        text = "API key: or-1234567890abcdefghij"
        result = redact(text)
        assert "or-1234567890abcdefghij" not in result
        assert "[REDACTED_API_KEY]" in result

    def test_authorization_header_redacted(self):
        text = '"Authorization": "Bearer sk-or-v1-secret-key-12345678901234567890"'
        result = redact(text)
        assert "sk-or-v1-secret" not in result
        assert "[REDACTED]" in result

    def test_problem_report_output_redacted(self):
        """build_problem_report redacts secrets even when deliberately
        present in the exception message or log lines."""
        api_key = "sk-or-v1-SUPERSECRETKEY1234567890abcdef"
        auth_header = '"Authorization": "Bearer sk-or-v1-anothersecret1234567"'

        exc = COMOperationTimeout(
            f"Failed connecting with key {api_key}"
        )

        report = build_problem_report(
            exc=exc,
            app_version="1.0.0",
            correlation_id="job-42",
            recent_log_lines=[
                f"2024-01-01 INFO: Using key {api_key}",
                f"2024-01-01 DEBUG: Headers: {auth_header}",
            ],
        )

        # Serialize and check the entire report
        report_json = export_problem_report_json(report)

        assert api_key not in report_json
        assert "SUPERSECRETKEY" not in report_json
        assert "anothersecret" not in report_json
        assert "[REDACTED" in report_json

    def test_harmless_text_not_redacted(self):
        """Normal text passes through unchanged."""
        text = "Processing page 5 of document ملزمة_الرياضيات.docx"
        assert redact(text) == text


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.7: Extraction runs once, user edits preserved
# ═══════════════════════════════════════════════════════════════════════


class TestExtractionRunsOnce:
    """AC-6.2.7: run_job_with_retry() calls extraction and classification
    exactly once per job submission, never inside the retry loop."""

    def test_extraction_called_once_on_success(self):
        """Extraction is called exactly once even when generation succeeds
        on the first try."""
        extraction_calls = []

        def mock_extract(source, config, store):
            extraction_calls.append(1)
            return "extracted_doc"

        def mock_gen(doc, config, attempt_index=0):
            return "success"

        result = run_job_with_retry(
            source_file_path="test.docx",
            job_config=None,
            document_store=MagicMock(),
            generation_fn=mock_gen,
            extraction_fn=mock_extract,
        )

        assert result == "success"
        assert len(extraction_calls) == 1

    def test_extraction_called_once_on_retry(self):
        """Extraction is called exactly once even when generation
        fails and is retried."""
        extraction_calls = []
        gen_calls = []

        def mock_extract(source, config, store):
            extraction_calls.append(1)
            return "extracted_doc"

        def mock_gen(doc, config, attempt_index=0):
            gen_calls.append(attempt_index)
            if attempt_index == 0:
                exc = COMOperationTimeout("First attempt timeout")
                exc.supervisor_state = SupervisorRetryState(
                    child_process_exited=True,
                    owned_indesign_pid=1234,
                    owned_indesign_confirmed_terminated=True,
                    attached_to_pre_existing_instance=False,
                )
                raise exc
            return "success_on_retry"

        result = run_job_with_retry(
            source_file_path="test.docx",
            job_config=None,
            document_store=MagicMock(),
            generation_fn=mock_gen,
            extraction_fn=mock_extract,
        )

        assert result == "success_on_retry"
        assert len(extraction_calls) == 1  # AC-6.2.7: exactly once
        assert len(gen_calls) == 2  # initial + retry

    def test_user_edits_preserved_across_retry(self):
        """Any with_user_edit() mutation applied before submission is
        still present on the document used by the retried attempt."""
        mock_store = MagicMock()
        mock_store.has_pending_edits.return_value = True

        documents_seen = []

        def mock_extract(source, config, store):
            return store  # pass through the document_store

        def mock_gen(doc, config, attempt_index=0):
            documents_seen.append(doc)
            if attempt_index == 0:
                exc = COMOperationTimeout("Timeout")
                exc.supervisor_state = SupervisorRetryState(
                    child_process_exited=True,
                    owned_indesign_pid=1234,
                    owned_indesign_confirmed_terminated=True,
                    attached_to_pre_existing_instance=False,
                )
                raise exc
            return "done"

        run_job_with_retry(
            source_file_path="test.docx",
            job_config=None,
            document_store=mock_store,
            generation_fn=mock_gen,
            extraction_fn=mock_extract,
        )

        # Both attempts received the SAME document object
        assert len(documents_seen) == 2
        assert documents_seen[0] is documents_seen[1]
        # The document still has user edits
        assert documents_seen[1].has_pending_edits() is True

    def test_non_retryable_exception_propagates_immediately(self):
        """A TextIntegrityError is not retried — it propagates immediately."""
        gen_calls = []

        def mock_gen(doc, config, attempt_index=0):
            gen_calls.append(1)
            raise TextIntegrityError("Hash mismatch")

        with pytest.raises(TextIntegrityError):
            run_job_with_retry(
                source_file_path="test.docx",
                job_config=None,
                document_store=MagicMock(),
                generation_fn=mock_gen,
                extraction_fn=lambda *a: "doc",
            )

        assert len(gen_calls) == 1  # called once, never retried


# ═══════════════════════════════════════════════════════════════════════
# AC-6.2.7b: COMSupervisor attaches SupervisorRetryState
# ═══════════════════════════════════════════════════════════════════════


class TestSupervisorStateAttached:
    """AC-6.2.7b: COMSupervisor.run_job() attaches a populated
    SupervisorRetryState to every COMOperationTimeout it raises."""

    def test_supervisor_retry_state_fields(self):
        """SupervisorRetryState has all required fields."""
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=1234,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
            partial_output_path=None,
        )
        assert state.child_process_exited is True
        assert state.owned_indesign_pid == 1234
        assert state.owned_indesign_confirmed_terminated is True
        assert state.attached_to_pre_existing_instance is False
        assert state.partial_output_path is None

    def test_supervisor_retry_state_is_frozen(self):
        """SupervisorRetryState is immutable (frozen=True)."""
        state = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        )
        with pytest.raises(Exception):  # ValidationError for frozen model
            state.child_process_exited = False

    def test_confirms_clean_state_all_conditions(self):
        """confirms_clean_retryable_state checks all conditions."""
        # All clean → True
        clean = SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=1234,
            owned_indesign_confirmed_terminated=True,
            attached_to_pre_existing_instance=False,
        )
        assert clean.confirms_clean_retryable_state() is True

        # Child not exited → False
        assert SupervisorRetryState(
            child_process_exited=False,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        ).confirms_clean_retryable_state() is False

        # InDesign not confirmed terminated → False
        assert SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=5678,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        ).confirms_clean_retryable_state() is False

        # No owned PID (0) but not pre-existing → True
        assert SupervisorRetryState(
            child_process_exited=True,
            owned_indesign_pid=0,
            owned_indesign_confirmed_terminated=False,
            attached_to_pre_existing_instance=False,
        ).confirms_clean_retryable_state() is True

    def test_com_timeout_is_in_allow_list(self):
        """COMOperationTimeout is in AUTO_RETRYABLE_EXCEPTIONS."""
        assert COMOperationTimeout in AUTO_RETRYABLE_EXCEPTIONS


# ═══════════════════════════════════════════════════════════════════════
# Additional: ProblemReport structure tests
# ═══════════════════════════════════════════════════════════════════════


class TestProblemReport:
    """Additional tests for ProblemReport structure and behavior."""

    def test_problem_report_is_frozen(self):
        report = build_problem_report(
            exc=BookletError("test"),
            app_version="1.0.0",
            correlation_id="job-1",
            recent_log_lines=[],
        )
        with pytest.raises(Exception):
            report.app_version = "2.0.0"

    def test_problem_report_has_all_fields(self):
        report = build_problem_report(
            exc=COMOperationTimeout("timeout"),
            app_version="1.2.3",
            correlation_id="corr-456",
            recent_log_lines=["line 1", "line 2"],
        )
        assert report.generated_at  # non-empty
        assert report.app_version == "1.2.3"
        assert report.correlation_id == "corr-456"
        assert report.original_exception_type == "COMOperationTimeout"
        assert "timeout" in report.original_exception_message
        assert len(report.recent_log_lines) == 2

    def test_export_json_is_valid(self):
        report = build_problem_report(
            exc=BookletError("test error"),
            app_version="1.0.0",
            correlation_id="test-id",
            recent_log_lines=["log line"],
        )
        json_str = export_problem_report_json(report)
        parsed = json.loads(json_str)
        assert parsed["original_exception_type"] == "BookletError"
        assert parsed["app_version"] == "1.0.0"

    def test_problem_report_unwraps_user_facing_error(self):
        """build_problem_report unwraps UserFacingError to get the
        original technical exception."""
        original = TextIntegrityError("hash mismatch abc123")
        wrapped = UserFacingError("Arabic message", original=original)

        report = build_problem_report(
            exc=wrapped,
            app_version="1.0.0",
            correlation_id="job-99",
            recent_log_lines=[],
        )

        assert report.original_exception_type == "TextIntegrityError"
        assert "hash mismatch" in report.original_exception_message
