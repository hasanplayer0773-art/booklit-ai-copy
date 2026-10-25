"""
Booklet_AI Desktop — AI Configuration UI (STEP-6.1)

User-facing settings panel for OpenRouter API key and model configuration.
Includes:
- ConnectionTestResult / ModelNameValidationResult data contracts
- validate_model_name_locally(): cheap synchronous slug-shape check
- test_connection(): async-safe structured-output validation against
  the real OpenRouter chat/completions endpoint (same response_format
  constraint as production), runs OFF the Qt UI thread
- ConnectionTestWorker(QThread): wraps test_connection for non-blocking UI
- AISettingsWidget(QWidget): full settings panel

Security invariants:
- API key is NEVER rendered in plaintext in logs, error reports, or
  problem-report artifacts (AC-6.1.4).
- test_connection() is a DISTINCT code path from classify_blocks() and
  never accepts or processes real ContentBlocks (AC-6.1.3).
"""

import json
import logging
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import QEvent, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.state.app_store import AppStore
from app.ui.i18n import LanguageSelectorWidget, get_translation_manager
from app.utils.secrets_manager import SecretsManager

logger = logging.getLogger("booklet_ai.ui.ai_settings")

# ─── Mask utility (AC-6.1.4) ────────────────────────────────────────


def _mask_key(key: str) -> str:
    """Return a masked representation of an API key for safe logging.

    Shows only the first 4 and last 4 characters if long enough,
    otherwise fully masks it. NEVER logs the full key.
    """
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


# ═══════════════════════════════════════════════════════════════════════
# 1. Data Contracts (Pydantic v2, frozen)
# ═══════════════════════════════════════════════════════════════════════


class ConnectionTestResult(BaseModel):
    """Outcome of a test_connection() call.

    Carries a machine-readable failure_reason AND a plain-language
    message safe for direct display in the UI (AC-6.1.2).
    """

    model_config = ConfigDict(frozen=True)

    success: bool
    failure_reason: Literal[
        "missing_key",
        "invalid_key",
        "unreachable_endpoint",
        "unknown_model",
        "structured_output_unsupported",
        "timeout",
        "rate_limited",
        "unknown_error",
        None,
    ] = None
    message: str
    latency_ms: float | None = None


class ModelNameValidationResult(BaseModel):
    """Outcome of validate_model_name_locally()."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    message: str


# ═══════════════════════════════════════════════════════════════════════
# 2. Local Validation (no network) — AC-6.1.6
# ═══════════════════════════════════════════════════════════════════════

# OpenRouter model slugs follow the shape: provider/model-name
# e.g. "google/gemini-2.0-flash-001", "openai/gpt-4o"
_MODEL_SLUG_RE = re.compile(
    r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+$"
)


def validate_model_name_locally(raw: str) -> ModelNameValidationResult:
    """Cheap, synchronous, local-only sanity check.

    Trims whitespace; rejects empty strings and values that don't match
    OpenRouter's 'provider/model' slug shape. Never proves the model is
    real or reachable — that is test_connection()'s job (AC-6.1.6).

    Called before enabling the Test Connection button, not as a
    substitute for it.
    """
    trimmed = raw.strip() if raw else ""

    if not trimmed:
        return ModelNameValidationResult(
            valid=False,
            message="اسم الموديل مطلوب — لا يمكن أن يكون فارغاً.",
        )

    if "/" not in trimmed:
        return ModelNameValidationResult(
            valid=False,
            message=(
                "صيغة غير صالحة — يجب أن يكون على شكل 'provider/model' "
                "(مثال: google/gemini-2.0-flash-001)."
            ),
        )

    if not _MODEL_SLUG_RE.match(trimmed):
        return ModelNameValidationResult(
            valid=False,
            message=(
                "الاسم يحتوي على أحرف غير مسموحة — يُسمح فقط بالحروف "
                "الإنكليزية والأرقام والشرطة والنقطة والشرطة السفلية."
            ),
        )

    return ModelNameValidationResult(
        valid=True,
        message=f"✓ صيغة صالحة: {trimmed}",
    )


# ═══════════════════════════════════════════════════════════════════════
# 3. test_connection() — AC-6.1.2, AC-6.1.2b, AC-6.1.3, AC-6.1.5
# ═══════════════════════════════════════════════════════════════════════

# The SAME endpoint used by OpenRouterClient in production (STEP-1.4).
_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def test_connection(api_key: str, model: str) -> ConnectionTestResult:
    """Validate API key + model against the real OpenRouter endpoint.

    Posts a minimal synthetic payload WITH the same
    ``response_format: {"type": "json_object"}`` constraint that
    STEP-1.4's ``_build_payload()`` depends on, so a model that
    cannot satisfy structured outputs is caught HERE rather than
    during a real classification job (AC-6.1.2b).

    This is a DISTINCT code path from production document classification — it never
    accepts, processes, or mutates any ``ContentBlock`` (AC-6.1.3).

    Must be called from a worker thread, never the Qt main thread
    (AC-6.1.5). The caller (``ConnectionTestWorker``) is responsible
    for thread discipline.

    Returns:
        ConnectionTestResult with actionable failure_reason and message.
    """
    # ── Pre-flight: missing key ──────────────────────────────────
    if not api_key or not api_key.strip():
        return ConnectionTestResult(
            success=False,
            failure_reason="missing_key",
            message="مفتاح API غير مُعيَّن — أدخل المفتاح أولاً ثم اضغط حفظ.",
        )

    # ── Build synthetic payload (mirrors _build_payload structure) ─
    synthetic_payload = {
        "model": model.strip(),
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a test probe. Return a JSON object with "
                    'a single key: {"status": "ok"}.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    [{"block_id": "test_probe_0", "text": "اختبار الاتصال"}],
                    ensure_ascii=False,
                ),
            },
        ],
    }

    start_ns = time.perf_counter_ns()

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                _OPENROUTER_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=synthetic_payload,
            )
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

        # ── HTTP error classification ────────────────────────────
        if response.status_code == 401:
            return ConnectionTestResult(
                success=False,
                failure_reason="invalid_key",
                message="مفتاح API غير صالح أو منتهي الصلاحية (401 Unauthorized).",
                latency_ms=latency_ms,
            )

        if response.status_code == 404:
            return ConnectionTestResult(
                success=False,
                failure_reason="unknown_model",
                message=f"الموديل '{model}' غير موجود أو غير متاح على OpenRouter (404).",
                latency_ms=latency_ms,
            )

        if response.status_code == 429:
            return ConnectionTestResult(
                success=False,
                failure_reason="rate_limited",
                message="تم تجاوز حد الطلبات المسموح — حاول مرة أخرى بعد قليل (429).",
                latency_ms=latency_ms,
            )

        if response.status_code >= 400:
            # Try to extract error detail from OpenRouter's response body
            detail = ""
            try:
                body = response.json()
                detail = body.get("error", {}).get("message", "")
            except Exception:
                pass
            return ConnectionTestResult(
                success=False,
                failure_reason="unknown_error",
                message=f"خطأ من الخادم ({response.status_code}): {detail or response.reason_phrase}",
                latency_ms=latency_ms,
            )

        # ── Success path: validate structured output ─────────────
        try:
            body = response.json()
            content_str = body["choices"][0]["message"]["content"]
            # The model MUST return valid JSON (structured output).
            json.loads(content_str)
        except (KeyError, IndexError):
            return ConnectionTestResult(
                success=False,
                failure_reason="structured_output_unsupported",
                message=(
                    "الموديل أجاب بنجاح لكن لم يُرجع مخرجات منظمة (JSON). "
                    "هذا الموديل غير متوافق مع متطلبات التصنيف المنظم للمشروع."
                ),
                latency_ms=latency_ms,
            )
        except (json.JSONDecodeError, TypeError):
            return ConnectionTestResult(
                success=False,
                failure_reason="structured_output_unsupported",
                message=(
                    "الموديل أجاب لكن المحتوى ليس JSON صالح — "
                    "هذا يعني أن الموديل لا يدعم response_format: json_object."
                ),
                latency_ms=latency_ms,
            )

        logger.info(
            "Connection test succeeded for model '%s' in %.0f ms "
            "(key: %s)",
            model,
            latency_ms,
            _mask_key(api_key),
        )

        return ConnectionTestResult(
            success=True,
            failure_reason=None,
            message=f"✓ تم الاتصال بنجاح بالموديل '{model}' ({latency_ms:.0f} مللي ثانية).",
            latency_ms=latency_ms,
        )

    except httpx.TimeoutException:
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        return ConnectionTestResult(
            success=False,
            failure_reason="timeout",
            message="انتهت مهلة الاتصال (15 ثانية) — تحقق من اتصالك بالإنترنت.",
            latency_ms=latency_ms,
        )

    except httpx.ConnectError:
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        return ConnectionTestResult(
            success=False,
            failure_reason="unreachable_endpoint",
            message="تعذر الاتصال بخادم OpenRouter — تحقق من الإنترنت أو إعدادات البروكسي.",
            latency_ms=latency_ms,
        )

    except Exception as exc:
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        logger.warning(
            "Unexpected error in test_connection: %s (key: %s)",
            type(exc).__name__,
            _mask_key(api_key),
        )
        return ConnectionTestResult(
            success=False,
            failure_reason="unknown_error",
            message=f"خطأ غير متوقع: {type(exc).__name__}",
            latency_ms=latency_ms,
        )


test_connection.__test__ = False  # Prevent pytest from treating this as a test


# ═══════════════════════════════════════════════════════════════════════
# 4. ConnectionTestWorker (QThread) — AC-6.1.5
# ═══════════════════════════════════════════════════════════════════════


class ConnectionTestWorker(QThread):
    """Runs test_connection() on a dedicated QThread.

    Emits results via Qt Signal so the UI event loop is never blocked
    (AC-6.1.5). The button is disabled and a spinner state is shown
    for the duration.

    Signals:
        result_ready(dict): Emitted when test completes. The dict is
            a ConnectionTestResult serialized via model_dump() — Qt
            signals carry primitives, not Pydantic objects (AC-1.1.4).
    """

    result_ready = Signal(dict)

    def __init__(
        self, api_key: str, model: str, parent=None
    ) -> None:
        super().__init__(parent)
        self._api_key = api_key
        self._model = model

    def run(self) -> None:  # noqa: D401 — QThread override
        """Execute test_connection off the Qt main thread."""
        result = test_connection(self._api_key, self._model)
        self.result_ready.emit(result.model_dump())


# ═══════════════════════════════════════════════════════════════════════
# 5. AISettingsWidget (QWidget) — full settings panel
# ═══════════════════════════════════════════════════════════════════════


class AISettingsWidget(QWidget):
    """User-facing panel for configuring the OpenRouter API key and model.

    Constructor requires:
        app_store: AppStore instance for reading/writing ai_model_name.
        secrets_manager: SecretsManager for keyring-backed API key storage.

    API key changes are persisted via keyring and immediately available
    to OpenRouterClient on the next classify_blocks() call (AC-6.1.1).
    Model name changes update AppStore.ai_model_name and are reflected
    in the next call without restart (AC-6.1.1b).
    """

    def __init__(
        self,
        app_store: AppStore,
        secrets_manager: SecretsManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_store = app_store
        self._secrets = secrets_manager
        self._worker: ConnectionTestWorker | None = None
        self._last_saved_model: str | None = None
        self._last_test_result: ConnectionTestResult | None = None

        self._init_ui()
        self._load_current_values()

    # ─── UI Construction ──────────────────────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Language Selection Group (STEP-6.6) ───────────────────
        self._lang_group = QGroupBox("لغة الواجهة")
        lang_layout = QVBoxLayout(self._lang_group)
        self._lang_selector = LanguageSelectorWidget(
            app_store=self._app_store, parent=self
        )
        lang_layout.addWidget(self._lang_selector)
        layout.addWidget(self._lang_group)

        # ── API Key Group ─────────────────────────────────────────
        self._key_group = QGroupBox("مفتاح OpenRouter API")
        key_layout = QVBoxLayout(self._key_group)

        key_row = QHBoxLayout()
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("sk-or-...")
        key_row.addWidget(self._key_input)

        self._reveal_btn = QPushButton("👁")
        self._reveal_btn.setFixedWidth(36)
        self._reveal_btn.setCheckable(True)
        self._reveal_btn.setToolTip("إظهار/إخفاء المفتاح")
        self._reveal_btn.toggled.connect(self._toggle_reveal)
        key_row.addWidget(self._reveal_btn)

        self._save_key_btn = QPushButton("حفظ المفتاح")
        self._save_key_btn.clicked.connect(self._save_api_key)
        key_row.addWidget(self._save_key_btn)

        key_layout.addLayout(key_row)

        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        key_layout.addWidget(self._key_status)

        layout.addWidget(self._key_group)

        # ── Model Name Group ──────────────────────────────────────
        self._model_group = QGroupBox("اسم الموديل (Model)")
        model_layout = QVBoxLayout(self._model_group)

        model_row = QHBoxLayout()
        self._model_input = QLineEdit()
        self._model_input.setPlaceholderText("google/gemini-2.0-flash-001")
        self._model_input.textChanged.connect(self._on_model_text_changed)
        model_row.addWidget(self._model_input)

        self._save_model_btn = QPushButton("حفظ الموديل")
        self._save_model_btn.clicked.connect(self._save_model_name)
        model_row.addWidget(self._save_model_btn)

        model_layout.addLayout(model_row)

        self._model_status = QLabel("")
        self._model_status.setWordWrap(True)
        model_layout.addWidget(self._model_status)

        # Help tooltip
        help_label = QLabel(
            "💡 للحصول على اسم الموديل، زُر "
            '<a href="https://openrouter.ai/models">openrouter.ai/models</a> '
            "وانسخ اسم الموديل بصيغة provider/model-name. "
            "تأكد أن الموديل يدعم structured output (JSON mode) — "
            "ليست كل الموديلات متوافقة مع متطلبات هذا المشروع."
        )
        help_label.setOpenExternalLinks(True)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #666; font-size: 11px;")
        model_layout.addWidget(help_label)

        layout.addWidget(self._model_group)

        # ── Test Connection ───────────────────────────────────────
        self._test_group = QGroupBox("اختبار الاتصال")
        test_layout = QVBoxLayout(self._test_group)

        test_row = QHBoxLayout()
        self._test_btn = QPushButton("🔌 اختبار الاتصال")
        self._test_btn.clicked.connect(self._run_test_connection)
        test_row.addWidget(self._test_btn)
        test_layout.addLayout(test_row)

        self._test_status = QLabel("اضغط لاختبار الاتصال بالموديل المحدد.")
        self._test_status.setWordWrap(True)
        test_layout.addWidget(self._test_status)

        layout.addWidget(self._test_group)
        layout.addStretch()

    # ─── Load Current Values ──────────────────────────────────────

    def _load_current_values(self) -> None:
        """Load current API key presence and model name from stores."""
        # Check if a key exists (never display it)
        has_key = self._secrets.has_api_key("openrouter")
        if has_key:
            self._key_status.setText("✓ مفتاح API محفوظ في Windows Credential Manager.")
            self._key_status.setStyleSheet("color: green;")
        else:
            self._key_status.setText("⚠ لا يوجد مفتاح API محفوظ.")
            self._key_status.setStyleSheet("color: orange;")

        # Load model name from AppStore
        current_model = self._app_store.ai_model_name
        self._model_input.setText(current_model)

    def _refresh_status_labels(self) -> None:
        """Refresh status labels in the active language (AC-6.6.1)."""
        has_key = self._secrets.has_api_key("openrouter")
        if has_key:
            self._key_status.setText(
                self.tr("API key saved in Windows Credential Manager.")
            )
            self._key_status.setStyleSheet("color: green;")
        else:
            self._key_status.setText(self.tr("No API key saved."))
            self._key_status.setStyleSheet("color: orange;")

        if self._last_saved_model:
            self._model_status.setText(
                f"{self.tr('Model saved:')} {self._last_saved_model}"
            )
            self._model_status.setStyleSheet("color: green;")

        if self._worker is None and self._last_test_result is None:
            self._test_status.setText(
                self.tr("Click to test connection with specified model.")
            )

    def retranslateUi(self) -> None:
        """Update all static labels when UI language changes (AC-6.6.1)."""
        self._lang_group.setTitle(self.tr("Interface Language"))
        self._key_group.setTitle(self.tr("OpenRouter API Key"))
        self._reveal_btn.setToolTip(self.tr("Show/hide API key"))
        self._save_key_btn.setText(self.tr("Save Key"))
        self._model_group.setTitle(self.tr("Model Name (Model)"))
        self._save_model_btn.setText(self.tr("Save Model"))
        self._test_group.setTitle(self.tr("Test Connection"))

        if self._worker is None:
            self._test_btn.setText(self.tr("🔌 Test Connection"))
        else:
            self._test_btn.setText(self.tr("⏳ Testing..."))

        self._lang_selector.retranslateUi()
        self._refresh_status_labels()

    def changeEvent(self, event: QEvent) -> None:
        """Qt event hook for dynamic language change."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    # ─── Slots ────────────────────────────────────────────────────

    @Slot(bool)
    def _toggle_reveal(self, checked: bool) -> None:
        """Toggle API key visibility between masked and plaintext."""
        if checked:
            self._key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._reveal_btn.setText("🙈")
        else:
            self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._reveal_btn.setText("👁")

    @Slot()
    def _save_api_key(self) -> None:
        """Persist API key via keyring (AC-6.1.1)."""
        key = self._key_input.text().strip()
        if not key:
            self._key_status.setText("⚠ أدخل مفتاح API أولاً.")
            self._key_status.setStyleSheet("color: orange;")
            return

        try:
            self._secrets.set_api_key("openrouter", key)
            self._key_status.setText("✓ تم حفظ مفتاح API بنجاح.")
            self._key_status.setStyleSheet("color: green;")
            # Clear the input after save (never keep key in UI memory)
            self._key_input.clear()
            logger.info(
                "API key saved via keyring (key: %s)", _mask_key(key)
            )
        except Exception as exc:
            self._key_status.setText(f"✗ فشل الحفظ: {type(exc).__name__}")
            self._key_status.setStyleSheet("color: red;")

    @Slot(str)
    def _on_model_text_changed(self, text: str) -> None:
        """Validate model name locally as user types (AC-6.1.6)."""
        result = validate_model_name_locally(text)
        self._model_status.setText(result.message)
        self._model_status.setStyleSheet(
            "color: green;" if result.valid else "color: orange;"
        )

    @Slot()
    def _save_model_name(self) -> None:
        """Save model name to AppStore (AC-6.1.1b)."""
        raw = self._model_input.text()
        result = validate_model_name_locally(raw)
        if not result.valid:
            self._model_status.setText(f"✗ {result.message}")
            self._model_status.setStyleSheet("color: red;")
            return

        trimmed = raw.strip()
        self._last_saved_model = trimmed
        self._app_store.ai_model_name = trimmed
        self._model_status.setText(
            f"✓ تم حفظ الموديل: {trimmed} — سيُستخدم في المهمة القادمة."
        )
        self._model_status.setStyleSheet("color: green;")
        logger.info("Model name updated in AppStore: '%s'", trimmed)

    @Slot()
    def _run_test_connection(self) -> None:
        """Launch test_connection on a worker thread (AC-6.1.5)."""
        # Get current API key from keyring (not from the input field)
        api_key = self._secrets.get_api_key("openrouter")
        model = self._model_input.text().strip()

        # Validate model locally first
        model_check = validate_model_name_locally(model)
        if not model_check.valid:
            self._test_status.setText(f"✗ {model_check.message}")
            self._test_status.setStyleSheet("color: red;")
            return

        # Disable button, show busy state
        self._test_btn.setEnabled(False)
        self._test_btn.setText("⏳ جاري الاختبار...")
        self._test_status.setText("جاري اختبار الاتصال...")
        self._test_status.setStyleSheet("color: #666;")

        # Launch worker
        self._worker = ConnectionTestWorker(
            api_key=api_key or "", model=model, parent=self
        )
        self._worker.result_ready.connect(self._on_test_result)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    @Slot(dict)
    def _on_test_result(self, result_dict: dict) -> None:
        """Handle test_connection result from worker thread."""
        result = ConnectionTestResult.model_validate(result_dict)
        self._last_test_result = result
        self._test_status.setText(result.message)
        if result.success:
            self._test_status.setStyleSheet("color: green; font-weight: bold;")
        else:
            self._test_status.setStyleSheet("color: red;")

    @Slot()
    def _on_worker_finished(self) -> None:
        """Re-enable the test button after worker completes."""
        self._test_btn.setEnabled(True)
        self._test_btn.setText("🔌 اختبار الاتصال")
        self._worker = None
