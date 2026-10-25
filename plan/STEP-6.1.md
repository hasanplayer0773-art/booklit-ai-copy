<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.1] AI Configuration UI

#### 1. Rationale

STEP-1.4 established the `OpenRouterClient` with a configurable `model` string and `keyring`-based secret storage, but no user-facing surface to set these values, verify them, or recover from a bad configuration was ever specified. Without this, a wrong or expired API key silently degrades every job to `RuleBasedClassifier` (per STEP-1.4's Audited Graceful Degradation policy) with no clear path for a non-technical user to notice or fix it proactively.

**Post-review correction:** the original draft of this STEP specified a synchronous `test_connection()` call and did not address two concrete defects caught in adversarial review: (1) a naive connection test can report success for a model that cannot actually satisfy STEP-1.4's `response_format: {"type": "json_object"}` structured-output requirement, silently deferring the real failure to the first production job; (2) if `OpenRouterClient` is constructed once at startup with a fixed model string, changing the model in this UI has no effect until restart. Both are corrected below.

#### 2. Components & Requirements

| Component | Requirement |
|---|---|
| API Key field | Read/write access to the `keyring`-stored secret (STEP-1.1's Secrets tooling). Never displayed in plaintext after initial entry — masked, with an explicit "reveal" toggle. |
| Test Connection button | Fires a bounded, cancellable, **asynchronous** request (never on the Qt main thread — same `QThread`/worker discipline STEP-1.1 already mandates for heavy operations) against a concrete OpenRouter endpoint, and reports success/failure via a Qt Signal once complete. |
| Model name field | Free-text input (replacing any fixed dropdown), stored via the same `AppStore` mechanism STEP-1.4 already describes as holding the user-overridable model string. Local validation only (non-empty, whitespace-trimmed, matches the `provider/model` slug shape) — this is a cheap sanity check, not proof the model is valid or reachable; that is `test_connection()`'s job. |
| Help affordance | A static tooltip/info box explaining, in plain language, how to obtain a valid model identifier from OpenRouter's model catalog — content only, no live fetch of OpenRouter's catalog is required for v1. The tooltip must not claim every listed OpenRouter model is compatible with this pipeline's structured-output requirement. |

#### 3. Architectural Corrections

- **Test Connection must validate the same capability the production pipeline depends on, not just generic reachability.** A test that only confirms "the API key is accepted and the endpoint responds" can pass for a model that does not support `response_format: {"type": "json_object"}` (STEP-1.4's `_build_payload()`), producing a false "connected successfully" result followed by a hard failure on the first real classification job. `test_connection()`'s request body must include the same `response_format` constraint as production, using a minimal synthetic payload (e.g. one placeholder block), so a structural incompatibility is caught here rather than mid-job.
- **`test_connection()` runs off the Qt UI thread.** It follows the same worker-thread discipline STEP-1.1 already establishes for any operation that could block the event loop (httpx's existing 15s timeout, per STEP-1.4, makes a main-thread call an unacceptable freeze risk). The UI issues the request and receives the result via a Qt Signal; the button is disabled/shows a spinner state for the duration, and the operation is cancellable.
- **`OpenRouterClient` must read the current model name from `AppStore` at call time, not at construction time.** If the client is instantiated once at application startup, changing the model string in this UI must still take effect on the *next* classification call without requiring an application restart — this is the same "immediately available" requirement AC-6.1.1 already states for the API key, extended explicitly to the model field, which the original draft omitted.
- **A concrete endpoint is specified, not a vague "minimal request."** `test_connection()` posts to the same `https://openrouter.ai/api/v1/chat/completions` endpoint `OpenRouterClient` uses in production (STEP-1.4), with a single-block synthetic payload and the real `response_format` constraint — not a different, cheaper endpoint that would not actually exercise the capability being validated.

#### 4. Declarative Definitions

```python
# app/ui/ai_settings.py — data contract only; widget implementation is a UI-layer concern
from pydantic import BaseModel, ConfigDict
from typing import Literal

class ConnectionTestResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    success: bool
    # Distinguishes at least these cases so the UI can show an actionable
    # message rather than one generic failure string (AC-6.1.2).
    failure_reason: Literal[
        "missing_key", "invalid_key", "unreachable_endpoint",
        "unknown_model", "structured_output_unsupported",
        "timeout", "rate_limited", "unknown_error", None
    ] = None
    message: str          # plain-language result, safe to display directly
    latency_ms: float | None = None

class ModelNameValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    valid: bool
    message: str

def validate_model_name_locally(raw: str) -> ModelNameValidationResult:
    """
    Cheap, synchronous, local-only sanity check — never proves the model
    is real or reachable. Trims whitespace; rejects empty strings and
    values that don't match OpenRouter's 'provider/model' slug shape.
    Called before enabling the Test Connection button, not as a
    substitute for it.
    """
    ...

def test_connection(api_key: str, model: str) -> "QtWorkerHandle[ConnectionTestResult]":
    """
    Runs on a dedicated QThread/worker, never the Qt main thread. Posts a
    minimal synthetic payload to OpenRouter's chat/completions endpoint
    (the same endpoint OpenRouterClient uses in production) WITH the
    same response_format={"type": "json_object"} constraint STEP-1.4's
    classify_blocks() depends on, so a model that cannot satisfy
    structured outputs is caught here rather than during a real job.
    Distinct code path from OpenRouterClient.classify_blocks() — this
    must never be used to classify real ContentBlocks; it exists solely
    to validate configuration before a job is submitted. Returns a
    handle whose result arrives via Qt Signal, not a direct return value.
    """
    ...
```

#### 5. Preconditions

- The API key must be persisted through `keyring` exactly as STEP-1.1 specifies — this UI is a front-end onto that existing mechanism, not a parallel storage path.
- `test_connection()` must be a distinct function/code path from `OpenRouterClient.classify_blocks()`, so that a connection test can never be mistaken for, or accidentally trigger, production classification of real document content.
- `OpenRouterClient` must expose a mechanism (constructor accepting a callable/reference into `AppStore`, or re-instantiation per call) that guarantees the model string used on any given classification call is whatever `AppStore` currently holds, not a value captured once at startup.

#### 6. Acceptance Criteria

- **[AC-6.1.1]** Saving a new API key through the settings UI persists it via `keyring` and makes it immediately available to `OpenRouterClient` on the next classification call, without requiring an application restart.
- **[AC-6.1.1b]** Changing the model name through the settings UI is reflected in the *next* `classify_blocks()` call without an application restart — verified by a test that changes the `AppStore` model value between two calls and confirms the second call's outbound payload uses the new value.
- **[AC-6.1.2]** `test_connection()` returns `success=False` with a `failure_reason` and plain-language `message` distinguishing at least: missing key, invalid key, unreachable endpoint, unknown model, and structured-output-unsupported.
- **[AC-6.1.2b]** A model name that accepts a generic completion request but rejects `response_format: {"type": "json_object"}` is reported by `test_connection()` as `failure_reason="structured_output_unsupported"`, not as a generic success — verified against a test double that simulates this exact response shape.
- **[AC-6.1.3]** `test_connection()` never causes any `ContentBlock` to be classified or mutated — verified by confirming it does not call `classify_blocks()` or any function that accepts real document content.
- **[AC-6.1.4]** The API key is never rendered in plaintext in logs, error reports (STEP-6.2), or the "Send Problem Report" artifact — masking is enforced at the point of display, not left to the caller to remember.
- **[AC-6.1.5]** `test_connection()` never executes on the Qt main thread — verified by confirming the UI event loop remains responsive (e.g. a progress spinner continues animating) for the full duration of a deliberately slow/stalled test call in a test harness.
- **[AC-6.1.6]** `validate_model_name_locally()` rejects an empty string and a value with no `/` separator, and accepts a well-formed `provider/model` string, without making any network call.

---
