<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.2] Field Error Translation, Reporting & Auto-Recovery

#### 1. Rationale

Every Fail-Fast exception defined across Phases 1–5 (`TextIntegrityError`, `MathExtractionError`, `InvalidClassificationResponse`, `MarginConfigError`, `InvalidBodyFrameGeometryError`, `RTLTableUnsupportedError`, `StyleMappingError`, `COMOperationTimeout`, `InDesignUnavailableError`, `QuestionIndexError`, `ContentExceedsEstimateError`, etc.) is written with a technical, English-language message aimed at a developer reading logs. None of these are safe or comprehensible to hand to a non-technical print-shop operator running the packaged `.exe` with no access to the person who built it. This step adds a presentation-layer translation system, a self-contained problem-report artifact, and a narrowly-scoped auto-retry mechanism — explicitly without any remote-access or remote-support capability, which was evaluated and excluded.

**Post-review correction:** the original draft had four confirmed defects, all corrected below: (1) type-exact lookup (`ERROR_TRANSLATIONS.get(type(exc), ...)`) silently mis-routes any future subclass to the fallback message instead of inheriting its parent's translation; (2) the "no secrets in the report" guarantee relied on the rest of the codebase never logging a secret, rather than an explicit redaction step; (3) auto-retry scope and placement (what gets retried — the whole job or one step? at which layer?) were unspecified; (4) `COMOperationTimeout` was allow-listed unconditionally, without requiring proof that the supervisor's cleanup left a clean, retry-safe state.

#### 2. Architectural Placement

- **The translation layer sits strictly at the UI presentation boundary**, never inside `app/core/`, `app/automation/`, or `app/ai/`. Every exception already defined in Phases 1–5 continues to propagate with its original English message and full technical detail unchanged — translation is a display concern applied only when an exception reaches the UI layer that shows it to the end user. This preserves the original exception's diagnostic value for the "Send Problem Report" artifact and for any future developer reading logs, while presenting only the translated form to the operator. Concretely: `core → automation → ai → job orchestrator (raises/propagates original exception) → UI error boundary (calls translate_error()) → Qt notification`. Any unhandled exception reaching the Qt event loop is passed through `translate_error()` before display — never shown raw.
- **Lookup is inheritance-aware (MRO-based), not type-exact.** A static table keyed by exact `type(exc)` cannot correctly handle a future subclass (e.g. a hypothetical `MyInDesignTimeout(COMOperationTimeout)`) — it would silently fall through to the generic fallback message instead of inheriting `COMOperationTimeout`'s translation, breaking the stated guarantee that every *known* exception family is translatable. Lookup walks the exception's MRO (most-specific class first) against the table; an exception type with no match anywhere in its MRO falls back to one generic Arabic message ("حدث خطأ غير متوقع — يرجى إرسال تقرير المشكلة") plus the "Send Problem Report" button — it is never left unhandled or shown raw to the operator, and the fallback path is itself tested (AC-6.2.5) rather than assumed to work.

#### 3. Declarative Definitions

```python
# app/ui/error_translation.py
from app.core.exceptions import BookletError

class UserFacingError(Exception):
    """
    Wraps any BookletError (or subclass) for display to a non-technical
    operator. Carries the translated message plus a reference to the
    original exception, which is preserved untranslated for the
    Send Problem Report artifact and for logging — translation never
    replaces or discards the original diagnostic information.
    """
    def __init__(self, translated_message: str, original: Exception):
        self.translated_message = translated_message
        self.original = original
        super().__init__(translated_message)

# Static mapping — one fixed Arabic message per known exception type.
# Extending this table when a new exception type is introduced elsewhere
# in the codebase is a required part of adding that exception type, not
# an optional follow-up. A subclass with no explicit entry inherits its
# nearest registered ancestor's message via translate_error()'s MRO walk
# below — it does NOT need its own entry unless a more specific message
# is warranted.
ERROR_TRANSLATIONS: dict[type[Exception], str] = {
    # populated per exception type defined across Phases 1-5,
    # e.g. InDesignUnavailableError -> "⚠️ تعذر إيجاد برنامج InDesign على
    # الجهاز — تأكد إنه مثبّت ومرخّص، ثم أعد المحاولة."
}

FALLBACK_MESSAGE = "حدث خطأ غير متوقع — يرجى إرسال تقرير المشكلة."

def translate_error(exc: Exception) -> UserFacingError:
    """
    Walks exc's Method Resolution Order (most specific class first) and
    returns the first matching ERROR_TRANSLATIONS entry — NOT a type(exc)
    exact match. This guarantees a future subclass of any already-mapped
    exception (e.g. a new COMOperationTimeout subtype) inherits its
    parent's translation automatically, with no table update required
    unless a more specific message is desired.
    """
    for klass in type(exc).__mro__:
        if klass in ERROR_TRANSLATIONS:
            return UserFacingError(ERROR_TRANSLATIONS[klass], original=exc)
    return UserFacingError(FALLBACK_MESSAGE, original=exc)
```

```python
# app/ui/report_redaction.py
import re

# Centralized, mandatory redaction — the Problem Report NEVER relies on
# "the rest of the codebase doesn't log secrets" as its safety guarantee.
# Every value that reaches ProblemReport passes through this function
# first, regardless of source (exception message, log line, etc.).
_REDACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(sk|or)-[A-Za-z0-9]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r'"Authorization"\s*:\s*"[^"]*"'), '"Authorization": "[REDACTED]"'),
    # Extended, never removed, whenever a new secret-shaped value is
    # introduced anywhere in the codebase (e.g. a future provider's key
    # format) — this list is a standing requirement, not a one-time pass.
]

def redact(text: str) -> str:
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
```

```python
# app/ui/problem_report.py
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict
from app.ui.report_redaction import redact

class ProblemReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    generated_at: str
    app_version: str
    correlation_id: str          # ties this report to a specific job_history row (STEP-6.3)
    original_exception_type: str
    original_exception_message: str  # untranslated, full technical detail — but REDACTED (see below)
    recent_log_lines: list[str]      # each line REDACTED individually before inclusion
    # API keys, file contents, and any other sensitive/PII values are
    # never included as a matter of policy — enforced structurally by
    # redact() below, not merely by convention elsewhere in the codebase.

def build_problem_report(
    exc: Exception,
    app_version: str,
    correlation_id: str,
    recent_log_lines: list[str],
) -> ProblemReport:
    """
    Assembles a self-contained, shareable artifact requiring zero
    technical input from the operator — triggered by a single button
    press. The operator's only action is choosing where to send/save the
    resulting file; no manual description or log-copying is required.

    Every string field is passed through redact() before being placed on
    the model — this is the single centralized redaction boundary; no
    caller is trusted to have pre-sanitized its input, per the confirmed
    defect that "the rest of the system doesn't log secrets" is not an
    adequate guarantee on its own.

    If the triggering job is still in-flight when this is called, only a
    brief DocumentStore lock acquisition (metadata copy) is permitted —
    assembly and compression of the report itself happens after the lock
    is released, never while holding it (STEP-6.3's Live Preview applies
    the same discipline for the same reason).
    """
    return ProblemReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        app_version=app_version,
        correlation_id=correlation_id,
        original_exception_type=type(exc).__name__,
        original_exception_message=redact(str(exc)),
        recent_log_lines=[redact(line) for line in recent_log_lines],
    )
```

```python
# app/utils/auto_recovery.py
from app.core.exceptions import BookletError
from app.core.text_integrity import TextIntegrityError
from app.core.exceptions import MathExtractionError
from app.automation.com_supervisor import COMOperationTimeout, COMSupervisor
from pydantic import BaseModel, ConfigDict

# Explicit allow-list, not a deny-list: only error classes confirmed to be
# transient/environmental are eligible for auto-retry. Defaulting to
# "retry unless listed as unsafe" would eventually auto-retry a class of
# error added later in the codebase without anyone deciding that was
# safe -- the allow-list forces that to be a deliberate decision every
# time. Membership in this list is NECESSARY but not SUFFICIENT for
# COMOperationTimeout specifically — see is_auto_retryable() below.
AUTO_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    COMOperationTimeout,       # STEP-1.1: may reflect a transient InDesign hang
    # network/timeout-class exceptions from OpenRouterClient are NOT listed
    # here because STEP-1.4 already defines their handling: an AI-call
    # failure falls back to RuleBasedClassifier within the same call,
    # rather than being retried at this layer.
)

# Never auto-retried, regardless of future additions to the codebase:
# retrying implies "try the same thing again and hope it works" — for
# these, the same input will deterministically fail again, so a retry
# only delays the same Fail-Fast halt while masking the cause.
NEVER_AUTO_RETRY: tuple[type[Exception], ...] = (
    TextIntegrityError,        # a hash mismatch will mismatch again — retrying hides a real data problem
    MathExtractionError,       # a missing/broken XSLT stylesheet does not fix itself
)

MAX_AUTO_RETRIES = 1  # a single silent retry; never an unbounded loop

# Post-follow-up-review correction: the prior draft called
# supervisor_state.confirms_clean_retryable_state() and read
# getattr(exc, "supervisor_state", None) without ever defining what
# populates that attribute — three independent reviewers confirmed this
# left the safety gate unenforceable in practice (it always evaluated to
# "no state -> refuse retry", which is safe but makes COMOperationTimeout
# permanently non-retryable, silently defeating the feature's purpose).
# This is now a concrete, required contract: COMSupervisor MUST construct
# and attach one of these to COMOperationTimeout at the exact point it is
# raised (STEP-1.1's _terminate_and_cleanup() call site) — not an
# optional enrichment added later.
class SupervisorRetryState(BaseModel):
    model_config = ConfigDict(frozen=True)
    child_process_exited: bool
    owned_indesign_pid: int          # 0 if this attempt never launched/owned an instance
    owned_indesign_confirmed_terminated: bool
    attached_to_pre_existing_instance: bool  # STEP-1.1: True means this timeout is NEVER auto-retryable, regardless of the other fields
    partial_output_path: str | None  # path of any partially-written artifact from this attempt, if one exists

    def confirms_clean_retryable_state(self) -> bool:
        """
        All conditions must hold for a retry to be permitted. This is a
        plain, deterministic check over fields COMSupervisor is required
        to populate before raising — not a placeholder for future logic.
        """
        if self.attached_to_pre_existing_instance:
            return False
        if not self.child_process_exited:
            return False
        if self.owned_indesign_pid != 0 and not self.owned_indesign_confirmed_terminated:
            return False
        if self.partial_output_path is not None:
            return False
        return True

def is_auto_retryable(exc: Exception, supervisor_state: "SupervisorRetryState | None" = None) -> bool:
    """
    COMOperationTimeout requires a SECOND, more specific condition beyond
    allow-list membership: the supervisor must be able to prove the prior
    attempt's state is fully clean before a retry is permitted. Retrying
    blind risks re-attaching to the same hung pre-existing InDesign
    instance (STEP-1.1 already documents this instance is never
    auto-killed) or re-running generation over a partially-written output
    artifact from the timed-out attempt.
    """
    if isinstance(exc, NEVER_AUTO_RETRY):
        return False
    if not isinstance(exc, AUTO_RETRYABLE_EXCEPTIONS):
        return False
    if isinstance(exc, COMOperationTimeout):
        if supervisor_state is None:
            return False  # cannot prove clean state -> refuse retry, fail safe
        return supervisor_state.confirms_clean_retryable_state()
    return True
```

**Required extension to STEP-1.1's `COMSupervisor.run_job()`** (the raise site shown in STEP-1.1's `while self.process.is_alive(): ... raise COMOperationTimeout(...)` block): that call site must construct a `SupervisorRetryState` from `self._terminate_and_cleanup()`'s own return value (see STEP-1.1's `_terminate_and_cleanup()`, which already computes exactly these facts — owned PID, termination confirmation, pre-existing-instance detection — but previously discarded them after logging) and attach it to the raised exception, e.g. `raise COMOperationTimeout(msg, supervisor_state=state)`. This is not new information STEP-1.1 needs to compute; it is information `_terminate_and_cleanup()` already computes and previously threw away.

```python
# app/automation/job_retry.py — the previously-missing orchestration
# layer connecting is_auto_retryable() to an actual retry loop.
from app.utils.auto_recovery import is_auto_retryable, MAX_AUTO_RETRIES, SupervisorRetryState

def run_job_with_retry(source_file_path: str, job_config: "JobConfig",
                        document_store: "DocumentStore") -> "JobResult":
    """
    Lives in the orchestration layer (the caller of COMSupervisor.run_job()
    and of the extraction/classification pipeline) — never inside
    COMSupervisor itself, and never in the UI layer.

    Post-follow-up-review correction: the prior draft retried the ENTIRE
    pipeline (re-extraction, re-classification, re-generation) on every
    retryable failure. This was flagged as a genuine defect: extraction
    and classification are pure-Python stages that never touch COM, so a
    COMOperationTimeout — by construction the only exception this
    function ever retries (see is_auto_retryable()) — indicates a
    generation-stage failure, not an ingestion-stage one. Blindly
    re-running extraction on a COM-only failure discards every
    with_user_edit() mutation (STEP-6.3) the operator made in the preview
    screen before submitting the job, destroying real user work to work
    around a downstream rendering hang. The corrected behavior retries
    ONLY the COM/generation step on a COMOperationTimeout, reusing the
    already-extracted-and-classified DocumentStore state (including any
    user edits) exactly as it stood when the job was submitted:
    """
    attempt = 0
    last_exc: Exception | None = None
    # Extraction and classification run exactly ONCE per job submission,
    # never inside the retry loop — retrying only ever re-invokes the
    # COM/generation step against this same, already-built document.
    document = _extract_and_classify(source_file_path, job_config, document_store)
    while attempt <= MAX_AUTO_RETRIES:
        try:
            return _run_generation_once(document, job_config, attempt_index=attempt)
        except Exception as exc:
            last_exc = exc
            supervisor_state: SupervisorRetryState | None = getattr(exc, "supervisor_state", None)
            if attempt >= MAX_AUTO_RETRIES or not is_auto_retryable(exc, supervisor_state):
                raise
            attempt += 1
    raise last_exc  # unreachable in practice; satisfies static analysis
```

#### 4. Preconditions

- Every exception type introduced by any future STEP must be added to `ERROR_TRANSLATIONS` (or deliberately left to inherit via MRO, or left to the fallback message) as part of that STEP's own definition — this is a standing requirement on all future work, not a one-time backfill.
- Auto-recovery is opt-in per exception type via the allow-list (`AUTO_RETRYABLE_EXCEPTIONS`), never opt-out via a deny-list — an exception type not explicitly listed is never auto-retried, and `COMOperationTimeout`'s allow-list membership is additionally gated by `confirms_clean_retryable_state()` at the moment of the retry decision, not assumed from allow-list membership alone.
- Every string field placed onto a `ProblemReport` must pass through `redact()` — no caller may construct a `ProblemReport` by hand-populating its fields from unredacted sources.
- `run_job_with_retry()` is the only sanctioned entry point for auto-retry; no other caller may re-invoke `COMSupervisor.run_job()` or the extraction/classification pipeline directly in response to a caught exception.

#### 5. Acceptance Criteria

- **[AC-6.2.1]** Every exception type defined across Phases 1–5 that can reach the UI layer resolves to a translated message via `translate_error()`'s MRO walk (either a direct entry or an inherited ancestor entry), or explicitly and verifiably reaches `FALLBACK_MESSAGE` — verified by a test enumerating all `BookletError` subclasses in the codebase against the translation table.
- **[AC-6.2.1b]** A synthetic subclass of an already-mapped exception (e.g. `class SyntheticTimeout(COMOperationTimeout): pass`), deliberately absent from `ERROR_TRANSLATIONS`, resolves to its parent `COMOperationTimeout`'s translated message via MRO — not to `FALLBACK_MESSAGE` — verified directly, proving the inheritance defect from the prior draft is fixed.
- **[AC-6.2.2]** `translate_error()` never discards the original exception — `UserFacingError.original` always contains the untranslated exception with its full technical message, and `build_problem_report()` always uses `UserFacingError.original`, never the translated string.
- **[AC-6.2.3]** `TextIntegrityError` and `MathExtractionError` are never auto-retried under any code path — verified by confirming `is_auto_retryable()` returns `False` for both regardless of `supervisor_state`, and that `run_job_with_retry()` is the only caller of `COMSupervisor.run_job()` in response to a failure.
- **[AC-6.2.4]** A `COMOperationTimeout` is retried at most once (`MAX_AUTO_RETRIES = 1`) before surfacing to the user as a translated error — never retried in an unbounded loop, and never retried at all when `supervisor_state` cannot confirm clean state (AC-6.2.4b).
- **[AC-6.2.4b]** A `COMOperationTimeout` where the prior attempt attached to a pre-existing (not job-owned) InDesign instance — the case STEP-1.1 already flags as requiring manual verification — is never auto-retried, regardless of allow-list membership — verified by a test asserting `is_auto_retryable()` returns `False` for this specific `supervisor_state` shape.
- **[AC-6.2.4c]** A retried job never writes to the same output path a failed prior attempt wrote to — verified by confirming `run_job_with_retry()` assigns a distinct per-attempt temporary path and only the final successful attempt's output is promoted to the destination path.
- **[AC-6.2.5]** An exception type deliberately absent from `ERROR_TRANSLATIONS` and with no mapped ancestor in its MRO (simulating a genuinely new, unrelated future exception) still produces a valid `UserFacingError` using `FALLBACK_MESSAGE`, and the "Send Problem Report" button remains functional for it — never an unhandled crash or a raw English stack trace shown to the operator.
- **[AC-6.2.6]** `build_problem_report()`'s output never contains the OpenRouter API key, an `Authorization` header value, or the raw text of any processed document — verified by a test asserting these values are absent from the serialized report even when deliberately present in the input `exc` message or `recent_log_lines`, proving redaction is enforced structurally rather than by caller discipline.
- **[AC-6.2.7]** `run_job_with_retry()` calls extraction and classification exactly once per job submission, never inside the retry loop — verified by confirming `_extract_and_classify()` is invoked exactly once regardless of how many generation attempts occur, and that any `with_user_edit()` mutation applied before submission is still present on the `DocumentStore` used by the retried (second) generation attempt — the retry preserves user edits rather than discarding them via re-extraction.
- **[AC-6.2.7b]** `COMSupervisor.run_job()` attaches a populated `SupervisorRetryState` to every `COMOperationTimeout` it raises — verified by confirming the exception's `supervisor_state` attribute is never `None` for a timeout raised through the normal `_terminate_and_cleanup()` path, closing the gap where `is_auto_retryable()` could only ever see `supervisor_state=None` and therefore never actually permit a retry in practice.

---
