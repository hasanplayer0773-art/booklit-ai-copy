"""
Booklet_AI Desktop — App Store (Lightweight Qt Signal Layer)

Two-Tier State Separation (STEP-1.1 §4):
- This module: lightweight Qt Signals carrying ONLY primitives/metadata.
- DocumentStore: heavy central store for full extracted content.

Qt Signal updates must complete in under 2ms with constant memory
usage (AC-1.1.4). Full Pydantic document objects are NEVER passed
through signals — only step counts, percentages, file hashes,
error messages, and similar scalar values.

Application settings (AI model name, provider, UI language) are
persisted via QSettings across application restarts (AC-6.6.3).
"""

from typing import Optional

from PySide6.QtCore import QObject, QSettings, Signal


class AppStore(QObject):
    """Application-wide state and signal hub.

    Emits lightweight signals for UI updates. Holds application-level
    settings (AI model name, provider, UI language) that can be read
    directly without restart and are persisted across restarts via QSettings
    (AC-6.6.3).

    Signals carry only primitive values — never Pydantic models or
    complex object graphs (AC-1.1.4).
    """

    # ─── Signals (primitives only — AC-1.1.4) ─────────────────────
    progress_updated = Signal(int, float)       # (step: int, percent: float)
    document_loaded = Signal(int, str)           # (page_count: int, file_hash: str)
    error_occurred = Signal(str)                 # (msg: str)
    job_started = Signal(str)                    # (doc_id: str)
    job_completed = Signal(str, bool)            # (doc_id: str, success: bool)
    ai_fallback_triggered = Signal(str)          # (reason: str)
    ui_language_changed = Signal(str)            # (lang: str) - AC-6.6.1 / AC-1.1.4

    _default_settings: Optional[QSettings] = None

    @classmethod
    def set_default_settings(cls, settings: Optional[QSettings]) -> None:
        """Inject a default QSettings object for tests or isolation."""
        cls._default_settings = settings

    def __init__(
        self,
        parent: Optional[QObject] = None,
        settings: Optional[QSettings] = None,
    ) -> None:
        super().__init__(parent)

        # ─── Persistent settings backend (AC-6.6.3) ───────────────
        self._settings = (
            settings
            if settings is not None
            else (self._default_settings or QSettings("Booklet_AI", "Booklet_AI Desktop"))
        )

        # Load persisted application settings
        self._ai_provider: str = str(
            self._settings.value("ai_provider", "openrouter")
        )
        self._ai_model_name: str = str(
            self._settings.value("ai_model_name", "google/gemini-2.0-flash-001")
        )
        loaded_lang = str(self._settings.value("ui_language", "ar"))
        self._ui_language: str = loaded_lang if loaded_lang in ("ar", "en") else "ar"

    # ─── AI Settings ──────────────────────────────────────────────

    @property
    def ai_provider(self) -> str:
        """Current AI provider (e.g. 'openrouter')."""
        return self._ai_provider

    @ai_provider.setter
    def ai_provider(self, value: str) -> None:
        self._ai_provider = value
        self._settings.setValue("ai_provider", value)

    @property
    def ai_model_name(self) -> str:
        """Current AI model name (e.g. 'google/gemini-2.0-flash-001')."""
        return self._ai_model_name

    @ai_model_name.setter
    def ai_model_name(self, value: str) -> None:
        self._ai_model_name = value
        self._settings.setValue("ai_model_name", value)

    @property
    def ui_language(self) -> str:
        """Current UI language code ('ar' or 'en')."""
        return self._ui_language

    @ui_language.setter
    def ui_language(self, value: str) -> None:
        if value not in ("ar", "en"):
            raise ValueError(
                f"Unsupported UI language: '{value}'. Must be 'ar' or 'en'."
            )
        if self._ui_language != value:
            self._ui_language = value
            self._settings.setValue("ui_language", value)
            self.ui_language_changed.emit(value)
