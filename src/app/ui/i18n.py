"""
Booklet_AI Desktop — Internationalization & Translation System (STEP-6.6)

Implements Qt's native translation system (QTranslator + .ts/.qm files)
for PySide6 with dynamic runtime switching between Arabic and English.

Architecture:
- UI strings are externalized via Qt's built-in translation system
  (QTranslator + .ts/.qm files) — not a custom JSON dictionary.
- ERROR_TRANSLATIONS entries from STEP-6.2 are bound directly to the
  same .ts/.qm resource mechanism (AC-6.6.2).
- Selected language drives both string lookup AND Qt layout direction:
  QApplication.setLayoutDirection(Qt.RightToLeft) for Arabic,
  Qt.LeftToRight for English (AC-6.6.1b).
- UI language selection is persisted across application restarts via
  AppStore / QSettings (AC-6.6.3).
- Changing UI language setting never alters document_content_hash
  or ContentBlock.raw_content (AC-6.6.1c).
- Dynamic language switching without application restart (AC-6.6.1).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QWidget,
)

if TYPE_CHECKING:
    from app.state.app_store import AppStore

logger = logging.getLogger("booklet_ai.i18n")

SUPPORTED_LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "ar"


def get_translations_dir() -> Path:
    """Resolve the directory containing .ts and .qm translation files."""
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller bundle mode
        candidates = [
            Path(sys._MEIPASS) / "assets" / "translations",
            Path(sys._MEIPASS) / "translations",
        ]
        for c in candidates:
            if c.is_dir():
                return c

    # Source mode
    current = Path(__file__).resolve()
    candidates = [
        current.parent.parent.parent / "assets" / "translations",  # src/assets/translations
        current.parent / "translations",
    ]
    for c in candidates:
        if c.is_dir():
            return c

    fallback = current.parent.parent.parent / "assets" / "translations"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_ts_path(lang: str) -> Path:
    """Get the path to the .ts source file for a given language."""
    return get_translations_dir() / f"booklet_ai_{lang}.ts"


def get_qm_path(lang: str) -> Path:
    """Get the path to the compiled .qm file for a given language."""
    return get_translations_dir() / f"booklet_ai_{lang}.qm"


def compile_ts_to_qm(ts_path: Path, qm_path: Path) -> bool:
    """Compile a .ts source file to a .qm binary using pyside6-lrelease."""
    if not ts_path.exists():
        logger.warning(".ts file does not exist at %s", ts_path)
        return False

    qm_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pyside6-lrelease", str(ts_path), "-qm", str(qm_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("Compiled %s -> %s: %s", ts_path.name, qm_path.name, res.stdout.strip())
        return True
    except Exception as exc:
        logger.warning("Failed to run pyside6-lrelease on %s: %s", ts_path, exc)
        return False


def load_ts_catalog(ts_path: Path) -> Dict[Tuple[str, str], str]:
    """Parse a Qt .ts XML file into a dictionary of {(context, source): translation}.

    Used for headless verification, fallback lookups, and ensuring translation
    symmetry (AC-6.6.2).
    """
    catalog: Dict[Tuple[str, str], str] = {}
    if not ts_path.exists():
        return catalog

    try:
        tree = ET.parse(ts_path)
        root = tree.getroot()
        for context in root.findall("context"):
            name_elem = context.find("name")
            context_name = name_elem.text.strip() if name_elem is not None and name_elem.text else ""
            for msg in context.findall("message"):
                src_elem = msg.find("source")
                trans_elem = msg.find("translation")
                if src_elem is not None and src_elem.text:
                    source_text = src_elem.text
                    trans_text = trans_elem.text if trans_elem is not None and trans_elem.text else ""
                    catalog[(context_name, source_text)] = trans_text
    except Exception as exc:
        logger.error("Error parsing .ts file %s: %s", ts_path, exc)

    return catalog


# Cached in-memory catalogs loaded from .ts
_CATALOG_CACHE: Dict[str, Dict[Tuple[str, str], str]] = {}


def get_ts_catalog(lang: str) -> Dict[Tuple[str, str], str]:
    """Get the cached .ts catalog for a language, parsing on first access."""
    if lang not in _CATALOG_CACHE:
        ts_path = get_ts_path(lang)
        _CATALOG_CACHE[lang] = load_ts_catalog(ts_path)
    return _CATALOG_CACHE[lang]


def reload_ts_catalogs() -> None:
    """Clear and reload the cached .ts catalogs."""
    _CATALOG_CACHE.clear()
    for lang in SUPPORTED_LANGUAGES:
        get_ts_catalog(lang)


# ═══════════════════════════════════════════════════════════════════════
# TranslationManager — Runtime Qt translation & layout controller
# ═══════════════════════════════════════════════════════════════════════


class TranslationManager(QObject):
    """Coordinates runtime UI translation, QTranslator lifecycle, and layout direction.

    Guarantees:
    - [AC-6.6.1] Switching language updates static labels and displayed errors without restart.
    - [AC-6.6.1b] Sets QApplication.layoutDirection to RightToLeft (ar) or LeftToRight (en).
    - [AC-6.6.3] Persists language choice via AppStore / QSettings.
    """

    language_changed = Signal(str)  # ("ar" or "en")

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._current_language: str = DEFAULT_LANGUAGE
        self._translator: Optional[object] = None  # QTranslator instance
        self._app_store: Optional[AppStore] = None

    @property
    def current_language(self) -> str:
        """The currently active UI language code ('ar' or 'en')."""
        return self._current_language

    def bind_app_store(self, app_store: AppStore) -> None:
        """Bind an AppStore instance to synchronize settings."""
        self._app_store = app_store
        # Synchronize to persisted language
        if app_store.ui_language in SUPPORTED_LANGUAGES:
            self.set_language(app_store.ui_language, app_store=app_store)

    def set_language(
        self,
        lang: str,
        app_store: Optional[AppStore] = None,
    ) -> bool:
        """Switch the application UI language and layout direction.

        Args:
            lang: Target language code ('ar' or 'en').
            app_store: Optional AppStore instance to persist setting.

        Returns:
            True if language was successfully switched.
        """
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Unsupported language code: '{lang}'. Must be one of {SUPPORTED_LANGUAGES}"
            )

        from PySide6.QtCore import QTranslator

        app = QApplication.instance() or QCoreApplication.instance()

        if app is not None:
            # 1. Remove previous translator
            if self._translator is not None:
                app.removeTranslator(self._translator)
                self._translator = None

            # 2. Load and install new translator
            qm_path = get_qm_path(lang)
            if not qm_path.exists():
                ts_path = get_ts_path(lang)
                if ts_path.exists():
                    compile_ts_to_qm(ts_path, qm_path)

            if qm_path.exists():
                translator = QTranslator(app)
                if translator.load(str(qm_path)):
                    app.installTranslator(translator)
                    self._translator = translator
                    logger.debug("Installed QTranslator from %s", qm_path)
                else:
                    logger.warning("Failed to load QTranslator from %s", qm_path)

            # 3. Apply layout direction directly (AC-6.6.1b)
            if isinstance(app, QApplication):
                new_direction = (
                    Qt.LayoutDirection.RightToLeft
                    if lang == "ar"
                    else Qt.LayoutDirection.LeftToRight
                )
                QApplication.setLayoutDirection(new_direction)
                logger.debug(
                    "Set QApplication.layoutDirection to %s for lang '%s'",
                    "RightToLeft" if lang == "ar" else "LeftToRight",
                    lang,
                )

                # 4. Broadcast LanguageChange event to all top-level widgets
                change_event = QEvent(QEvent.Type.LanguageChange)
                for widget in QApplication.topLevelWidgets():
                    app.sendEvent(widget, change_event)

        self._current_language = lang

        # 5. Persist setting to AppStore (AC-6.6.3)
        target_store = app_store or self._app_store
        if target_store is not None:
            if target_store.ui_language != lang:
                target_store.ui_language = lang

        # 6. Emit signal
        self.language_changed.emit(lang)
        return True

    def translate(
        self,
        context: str,
        source_text: str,
        disambiguation: Optional[str] = None,
        n: int = -1,
        lang: Optional[str] = None,
    ) -> str:
        """Translate a string for the given context and source text.

        Uses Qt's QCoreApplication.translate when available; falls back
        to the parsed .ts catalog if running in headless mode or if the
        translator returned the untranslated key.
        """
        target_lang = lang or self._current_language
        app = QCoreApplication.instance()

        if app is not None and (lang is None or lang == self._current_language):
            translated = QCoreApplication.translate(context, source_text, disambiguation, n)
            if translated and translated != source_text:
                return translated

        # Fallback to .ts catalog directly
        catalog = get_ts_catalog(target_lang)
        if (context, source_text) in catalog:
            return catalog[(context, source_text)]

        return source_text


# Global TranslationManager singleton
_GLOBAL_TRANSLATION_MANAGER: Optional[TranslationManager] = None


def get_translation_manager() -> TranslationManager:
    """Get or create the global TranslationManager singleton."""
    global _GLOBAL_TRANSLATION_MANAGER
    if _GLOBAL_TRANSLATION_MANAGER is None:
        _GLOBAL_TRANSLATION_MANAGER = TranslationManager()
    return _GLOBAL_TRANSLATION_MANAGER


def set_translation_manager(manager: Optional[TranslationManager]) -> None:
    """Set or reset the global TranslationManager singleton (useful in tests)."""
    global _GLOBAL_TRANSLATION_MANAGER
    _GLOBAL_TRANSLATION_MANAGER = manager


def get_current_language() -> str:
    """Get the currently active UI language code ('ar' or 'en')."""
    return get_translation_manager().current_language


def tr(context: str, source_text: str, lang: Optional[str] = None) -> str:
    """Convenience helper to translate text for a context."""
    return get_translation_manager().translate(context, source_text, lang=lang)


# ═══════════════════════════════════════════════════════════════════════
# LanguageSelectorWidget — User-facing language dropdown
# ═══════════════════════════════════════════════════════════════════════


class LanguageSelectorWidget(QWidget):
    """Interactive language switcher widget (AC-6.6.1).

    Switches between Arabic and English without application restart.
    Automatically reflects the current language from TranslationManager
    and persists changes to the associated AppStore.
    """

    def __init__(
        self,
        app_store: Optional[AppStore] = None,
        translation_manager: Optional[TranslationManager] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._app_store = app_store
        self._tm = translation_manager or get_translation_manager()

        self._init_ui()
        self._sync_ui_state()

        # Connect signals
        self._tm.language_changed.connect(self._on_language_changed)
        if self._app_store is not None:
            self._app_store.ui_language_changed.connect(self._on_app_store_language_changed)

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._label = QLabel(self.tr("Interface Language:"))
        layout.addWidget(self._label)

        self._combo = QComboBox()
        self._combo.addItem("العربية", "ar")
        self._combo.addItem("English", "en")
        self._combo.currentIndexChanged.connect(self._on_combo_index_changed)
        layout.addWidget(self._combo)

    def _sync_ui_state(self) -> None:
        """Sync combo box selection to current language without emitting."""
        current = self._tm.current_language
        index = self._combo.findData(current)
        if index != -1 and self._combo.currentIndex() != index:
            self._combo.blockSignals(True)
            self._combo.setCurrentIndex(index)
            self._combo.blockSignals(False)

    def _on_combo_index_changed(self, index: int) -> None:
        """User changed selection in combo box."""
        lang = self._combo.itemData(index)
        if lang and lang != self._tm.current_language:
            self._tm.set_language(lang, app_store=self._app_store)

    def _on_language_changed(self, lang: str) -> None:
        self._sync_ui_state()
        self.retranslateUi()

    def _on_app_store_language_changed(self, lang: str) -> None:
        if lang != self._tm.current_language:
            self._tm.set_language(lang, app_store=self._app_store)

    def retranslateUi(self) -> None:
        """Update static label text when language changes."""
        self._label.setText(
            QCoreApplication.translate("AISettingsWidget", "Interface Language") + ":"
        )

    def changeEvent(self, event: QEvent) -> None:
        """Qt event hook for dynamic language change."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)
