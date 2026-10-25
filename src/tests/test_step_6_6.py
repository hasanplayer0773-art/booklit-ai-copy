"""
Booklet_AI Desktop — STEP-6.6 Tests

Comprehensive test suite for Bilingual UI (Arabic/English).
Covers every Acceptance Criterion (AC-6.6.1 through AC-6.6.3):
- AC-6.6.1: Dynamic UI language switching updates static labels & errors without restart
- AC-6.6.1b: Sets QApplication.layoutDirection() to RightToLeft (ar) or LeftToRight (en)
- AC-6.6.1c: Changing UI language never alters document_content_hash or raw_content
- AC-6.6.2: ERROR_TRANSLATIONS AR & EN entries both present in .ts/.qm mechanism (no drift)
- AC-6.6.3: UI language selection persisted across application restarts
"""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, QSettings, Qt, QTranslator
from PySide6.QtWidgets import QApplication

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
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import compute_canonical_hash
from app.state.app_store import AppStore
from app.state.document_store import DocumentStore
from app.ui.ai_settings import AISettingsWidget
from app.ui.error_display import ErrorBanner
from app.ui.error_translation import (
    ERROR_TRANSLATION_CONTEXT,
    ERROR_TRANSLATIONS,
    ERROR_TRANSLATIONS_EN,
    EXCEPTION_TRANSLATION_KEYS,
    FALLBACK_MESSAGE,
    FALLBACK_MESSAGE_EN,
    UserFacingError,
    get_error_message,
    translate_error,
)
from app.ui.i18n import (
    LanguageSelectorWidget,
    TranslationManager,
    get_current_language,
    get_qm_path,
    get_translations_dir,
    get_ts_catalog,
    get_ts_path,
    load_ts_catalog,
    set_translation_manager,
    tr,
)
from app.utils.secrets_manager import SecretsManager


@pytest.fixture(autouse=True)
def qapp():
    """Ensure a QApplication instance exists in offscreen mode for all tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    yield app


@pytest.fixture
def clean_tm():
    """Provide a fresh TranslationManager instance for isolated testing."""
    tm = TranslationManager()
    set_translation_manager(tm)
    yield tm
    # Reset to default
    set_translation_manager(None)


@pytest.fixture
def temp_ini_settings():
    """Provide a temporary isolated QSettings instance backed by a temporary file."""
    with tempfile.NamedTemporaryFile(suffix=".ini", delete=False) as tmp:
        ini_path = tmp.name

    settings = QSettings(ini_path, QSettings.Format.IniFormat)
    yield settings, ini_path

    del settings
    try:
        Path(ini_path).unlink()
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════
# 1. AC-6.6.2: Translation Resource Symmetry & Unified Mechanism
# ═══════════════════════════════════════════════════════════════════════


class TestAC_6_6_2_TranslationResourceSymmetry:
    """AC-6.6.2: STEP-6.2's ERROR_TRANSLATIONS Arabic entries and their English
    equivalents are both present in the same .ts/.qm resource mechanism —
    verified by confirming no exception type's translation key has an Arabic
    entry without a corresponding English entry or vice versa."""

    def test_ts_files_exist_on_disk(self):
        """Both Arabic and English .ts translation source files exist."""
        ar_ts = get_ts_path("ar")
        en_ts = get_ts_path("en")
        assert ar_ts.is_file(), f"Missing Arabic .ts file at {ar_ts}"
        assert en_ts.is_file(), f"Missing English .ts file at {en_ts}"

    def test_qm_files_exist_on_disk(self):
        """Both Arabic and English compiled .qm binary files exist."""
        ar_qm = get_qm_path("ar")
        en_qm = get_qm_path("en")
        assert ar_qm.is_file(), f"Missing Arabic .qm file at {ar_qm}"
        assert en_qm.is_file(), f"Missing English .qm file at {en_qm}"

    def test_qm_files_load_into_qtranslator(self):
        """QTranslator successfully loads both compiled .qm files."""
        for lang in ("ar", "en"):
            qm_path = get_qm_path(lang)
            translator = QTranslator()
            loaded = translator.load(str(qm_path))
            assert loaded is True, f"Failed to load {lang} .qm file: {qm_path}"

    def test_error_translation_keys_symmetry_in_ts_catalogs(self):
        """Every exception translation key has BOTH an Arabic and English entry (AC-6.6.2)."""
        ar_catalog = load_ts_catalog(get_ts_path("ar"))
        en_catalog = load_ts_catalog(get_ts_path("en"))

        # Check every exception type mapped in EXCEPTION_TRANSLATION_KEYS
        for exc_cls, key in EXCEPTION_TRANSLATION_KEYS.items():
            lookup_key = (ERROR_TRANSLATION_CONTEXT, key)

            assert lookup_key in ar_catalog, (
                f"Missing Arabic translation in .ts for exception key '{key}' ({exc_cls.__name__})"
            )
            assert lookup_key in en_catalog, (
                f"Missing English translation in .ts for exception key '{key}' ({exc_cls.__name__})"
            )

            ar_text = ar_catalog[lookup_key]
            en_text = en_catalog[lookup_key]

            assert ar_text and len(ar_text.strip()) > 0, (
                f"Empty Arabic translation for '{key}'"
            )
            assert en_text and len(en_text.strip()) > 0, (
                f"Empty English translation for '{key}'"
            )

        # Also check FALLBACK_MESSAGE
        fallback_key = (ERROR_TRANSLATION_CONTEXT, "FALLBACK_MESSAGE")
        assert fallback_key in ar_catalog
        assert fallback_key in en_catalog

    def test_zero_asymmetry_between_ts_error_catalogs(self):
        """No exception key exists in Arabic without an English entry or vice versa."""
        ar_catalog = load_ts_catalog(get_ts_path("ar"))
        en_catalog = load_ts_catalog(get_ts_path("en"))

        ar_error_keys = {
            source for (ctx, source) in ar_catalog.keys()
            if ctx == ERROR_TRANSLATION_CONTEXT
        }
        en_error_keys = {
            source for (ctx, source) in en_catalog.keys()
            if ctx == ERROR_TRANSLATION_CONTEXT
        }

        diff_ar_minus_en = ar_error_keys - en_error_keys
        diff_en_minus_ar = en_error_keys - ar_error_keys

        assert diff_ar_minus_en == set(), (
            f"Keys present in Arabic .ts but missing in English: {diff_ar_minus_en}"
        )
        assert diff_en_minus_ar == set(), (
            f"Keys present in English .ts but missing in Arabic: {diff_en_minus_ar}"
        )

    def test_error_translations_dictionaries_match_keys(self):
        """ERROR_TRANSLATIONS (AR) and ERROR_TRANSLATIONS_EN have identical exception classes."""
        assert set(ERROR_TRANSLATIONS.keys()) == set(ERROR_TRANSLATIONS_EN.keys())
        assert len(ERROR_TRANSLATIONS) == len(EXCEPTION_TRANSLATION_KEYS)

    def test_all_exceptions_translate_in_both_languages(self, clean_tm):
        """translate_error() translates every mapped exception into AR and EN."""
        for exc_cls in EXCEPTION_TRANSLATION_KEYS.keys():
            exc = exc_cls(f"Technical error in {exc_cls.__name__}")

            # Translate Arabic
            ar_result = translate_error(exc, lang="ar")
            assert isinstance(ar_result, UserFacingError)
            assert ar_result.translated_message == ERROR_TRANSLATIONS[exc_cls]
            assert ar_result.original is exc

            # Translate English
            en_result = translate_error(exc, lang="en")
            assert isinstance(en_result, UserFacingError)
            assert en_result.translated_message == ERROR_TRANSLATIONS_EN[exc_cls]
            assert en_result.original is exc

            # Messages must be distinct between AR and EN
            assert ar_result.translated_message != en_result.translated_message

    def test_mro_inheritance_in_both_languages(self, clean_tm):
        """A subclass not directly in the table inherits ancestor translations in both AR and EN."""
        class CustomTimeoutSubclass(COMOperationTimeout):
            pass

        exc = CustomTimeoutSubclass("Custom timeout event")

        ar_res = translate_error(exc, lang="ar")
        assert ar_res.translated_message == ERROR_TRANSLATIONS[COMOperationTimeout]

        en_res = translate_error(exc, lang="en")
        assert en_res.translated_message == ERROR_TRANSLATIONS_EN[COMOperationTimeout]

    def test_fallback_message_in_both_languages(self, clean_tm):
        """Unmapped exceptions resolve to FALLBACK_MESSAGE in AR and FALLBACK_MESSAGE_EN in EN."""
        exc = ZeroDivisionError("division by zero")

        ar_res = translate_error(exc, lang="ar")
        assert ar_res.translated_message == FALLBACK_MESSAGE
        assert "خطأ" in ar_res.translated_message

        en_res = translate_error(exc, lang="en")
        assert en_res.translated_message == FALLBACK_MESSAGE_EN
        assert "unexpected error" in en_res.translated_message.lower()


# ═══════════════════════════════════════════════════════════════════════
# 2. AC-6.6.1b: Layout Direction Direct Verification
# ═══════════════════════════════════════════════════════════════════════


class TestAC_6_6_1b_LayoutDirection:
    """AC-6.6.1b: Switching the UI language setting to Arabic sets
    QApplication.layoutDirection() to Qt.RightToLeft, and to English sets it
    to Qt.LeftToRight — verified directly, not inferred from string content."""

    def test_switching_to_arabic_sets_layout_direction_rtl(self, clean_tm, qapp):
        """Switching to 'ar' sets QApplication.layoutDirection() to Qt.RightToLeft."""
        clean_tm.set_language("en")
        assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight

        clean_tm.set_language("ar")
        assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft

    def test_switching_to_english_sets_layout_direction_ltr(self, clean_tm, qapp):
        """Switching to 'en' sets QApplication.layoutDirection() to Qt.LeftToRight."""
        clean_tm.set_language("ar")
        assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft

        clean_tm.set_language("en")
        assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight

    def test_repeated_layout_direction_toggling(self, clean_tm, qapp):
        """Repeated toggles alternate layout direction deterministically."""
        for _ in range(3):
            clean_tm.set_language("ar")
            assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft

            clean_tm.set_language("en")
            assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight

    def test_invalid_language_code_raises_and_preserves_direction(self, clean_tm, qapp):
        """An unsupported language raises ValueError and does not alter layout direction."""
        clean_tm.set_language("ar")
        assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft

        with pytest.raises(ValueError, match="Unsupported language"):
            clean_tm.set_language("fr")

        # Direction remains RightToLeft
        assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft


# ═══════════════════════════════════════════════════════════════════════
# 3. AC-6.6.1: Dynamic Switching Without Application Restart
# ═══════════════════════════════════════════════════════════════════════


class TestAC_6_6_1_DynamicSwitchingWithoutRestart:
    """AC-6.6.1: Switching the UI language setting updates all static labels
    and any currently-displayed translated error message without requiring
    an application restart."""

    def test_error_banner_updates_displayed_error_without_restart(self, clean_tm, qapp):
        """An active error banner immediately re-translates its message on language switch."""
        clean_tm.set_language("ar")

        banner = ErrorBanner()
        exc = TextIntegrityError("Hash mismatch test")
        banner.show_error(exc)

        # Initially displayed in Arabic
        assert banner.displayed_text == ERROR_TRANSLATIONS[TextIntegrityError]
        assert "تم اكتشاف تغيير" in banner.displayed_text

        # Switch to English — banner must update text immediately without restart
        clean_tm.set_language("en")
        assert banner.displayed_text == ERROR_TRANSLATIONS_EN[TextIntegrityError]
        assert "Unauthorized modification" in banner.displayed_text

        # Switch back to Arabic
        clean_tm.set_language("ar")
        assert banner.displayed_text == ERROR_TRANSLATIONS[TextIntegrityError]

    def test_ai_settings_widget_labels_update_without_restart(self, clean_tm, qapp):
        """AISettingsWidget static labels update dynamically when language changes."""
        clean_tm.set_language("ar")

        app_store = AppStore()
        secrets = MagicMock(spec=SecretsManager)
        secrets.has_api_key.return_value = True

        widget = AISettingsWidget(app_store, secrets)

        # Arabic initial state
        assert widget._save_key_btn.text() == "حفظ المفتاح"
        assert widget._save_model_btn.text() == "حفظ الموديل"
        assert widget._test_btn.text() == "🔌 اختبار الاتصال"
        assert widget._lang_group.title() == "لغة الواجهة"
        assert widget._key_group.title() == "مفتاح OpenRouter API"

        # Switch language to English
        clean_tm.set_language("en", app_store=app_store)

        # Static labels updated to English without restart
        assert widget._save_key_btn.text() == "Save Key"
        assert widget._save_model_btn.text() == "Save Model"
        assert widget._test_btn.text() == "🔌 Test Connection"
        assert widget._lang_group.title() == "Interface Language"
        assert widget._key_group.title() == "OpenRouter API Key"

        # Switch back to Arabic
        clean_tm.set_language("ar", app_store=app_store)
        assert widget._save_key_btn.text() == "حفظ المفتاح"
        assert widget._save_model_btn.text() == "حفظ الموديل"

    def test_language_selector_widget_interaction(self, clean_tm, qapp):
        """User interacting with LanguageSelectorWidget triggers live language change."""
        clean_tm.set_language("ar")

        app_store = AppStore()
        selector = LanguageSelectorWidget(app_store=app_store, translation_manager=clean_tm)

        assert selector._combo.currentText() == "العربية"
        assert clean_tm.current_language == "ar"

        # User chooses English from dropdown
        en_idx = selector._combo.findData("en")
        selector._combo.setCurrentIndex(en_idx)

        # Both TranslationManager and AppStore updated immediately
        assert clean_tm.current_language == "en"
        assert app_store.ui_language == "en"
        assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight

        # User chooses Arabic
        ar_idx = selector._combo.findData("ar")
        selector._combo.setCurrentIndex(ar_idx)
        assert clean_tm.current_language == "ar"
        assert app_store.ui_language == "ar"
        assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft


# ═══════════════════════════════════════════════════════════════════════
# 4. AC-6.6.1c: Zero-Mutation & Document Content Independence
# ═══════════════════════════════════════════════════════════════════════


class TestAC_6_6_1c_DocumentContentIndependence:
    """AC-6.6.1c: Changing the UI language setting never alters
    document_content_hash (STEP-6.3) or any ContentBlock.raw_content
    for the currently-open document — verified by confirming UI locale
    state and document-content state are structurally independent."""

    def test_language_switch_never_alters_document_hashes_or_content(self, clean_tm):
        """Toggling language multiple times leaves document_content_hash and all raw_content invariant."""
        doc_store = DocumentStore()

        raw_texts = [
            "الفصل الأول: المتجهات والقوى في بعدين",
            "سؤال وزاري 2021: احسب القوة المحصلة المؤثرة في الجسم.",
            "E = mc^2 + \\int_0^\\infty f(x) dx",
            "جدول النتائج التجريبية والتطبيقات العملية",
        ]

        blocks = [
            ContentBlock(
                block_id=f"blk-{i}",
                block_type="arabic_text",
                raw_content=text,
                content_hash=compute_canonical_hash(text),
            )
            for i, text in enumerate(raw_texts)
        ]

        page = PageData(
            page_number=1,
            width=595.0,
            height=842.0,
            content_blocks=blocks,
        )

        doc = BookletDocument(
            doc_id="doc-bilingual-test",
            file_path="C:/test/arabic_booklet.docx",
            file_type="docx",
            pages=[page],
            document_content_hash="initial_placeholder_hash",
        )
        # Compute combined document content hash
        combined_text = "".join(b.raw_content for b in blocks)
        expected_doc_hash = compute_canonical_hash(combined_text)
        doc = doc.model_copy(update={"document_content_hash": expected_doc_hash})

        doc_store.load_document(doc)

        # Baseline snapshots
        stored_doc_before = doc_store.get_document("doc-bilingual-test")
        assert stored_doc_before is not None
        baseline_doc_hash = stored_doc_before.document_content_hash
        baseline_blocks = [
            (b.block_id, b.raw_content, b.content_hash)
            for b in stored_doc_before.pages[0].content_blocks
        ]

        # Rapidly toggle UI language setting 6 times
        for _ in range(3):
            clean_tm.set_language("en")
            clean_tm.set_language("ar")

        # Inspect document from store after language changes
        stored_doc_after = doc_store.get_document("doc-bilingual-test")
        assert stored_doc_after is not None

        # Document content hash must be completely identical
        assert stored_doc_after.document_content_hash == baseline_doc_hash

        # Every ContentBlock must have identical raw_content and content_hash
        after_blocks = [
            (b.block_id, b.raw_content, b.content_hash)
            for b in stored_doc_after.pages[0].content_blocks
        ]

        assert len(after_blocks) == len(baseline_blocks)
        for (b_id, b_raw, b_hash), (a_id, a_raw, a_hash) in zip(baseline_blocks, after_blocks):
            assert b_id == a_id
            assert b_raw == a_raw, f"Content mutation detected on block {b_id}!"
            assert b_hash == a_hash, f"Hash mismatch detected on block {b_id}!"

    def test_structural_separation_of_locale_and_document_paths(self):
        """TranslationManager and AppStore locale properties have no dependencies on DocumentStore."""
        import ast
        import inspect
        import app.ui.i18n as i18n_mod
        import app.state.app_store as app_store_mod

        # Verify no imported symbols from document domain in i18n module
        assert "DocumentStore" not in i18n_mod.__dict__
        assert "ContentBlock" not in i18n_mod.__dict__
        assert "PageData" not in i18n_mod.__dict__
        assert "BookletDocument" not in i18n_mod.__dict__

        # Verify no document symbols imported in AppStore
        assert "DocumentStore" not in app_store_mod.__dict__
        assert "ContentBlock" not in app_store_mod.__dict__

        # Inspect AST imports of i18n to verify no document module is imported
        i18n_tree = ast.parse(inspect.getsource(i18n_mod))
        imported_modules = []
        for node in ast.walk(i18n_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        for mod in imported_modules:
            assert "document" not in mod.lower()
            assert "models" not in mod.lower()


# ═══════════════════════════════════════════════════════════════════════
# 5. AC-6.6.3: Language Selection Persistence Across Restarts
# ═══════════════════════════════════════════════════════════════════════


class TestAC_6_6_3_LanguagePersistenceAcrossRestarts:
    """AC-6.6.3: The selected UI language is persisted across application restarts."""

    def test_language_persisted_to_qsettings_and_reloaded_on_restart(self, temp_ini_settings):
        """Setting ui_language saves to QSettings and a fresh AppStore loads it."""
        settings, ini_path = temp_ini_settings

        # Session 1: initial launch with Arabic default
        store1 = AppStore(settings=settings)
        assert store1.ui_language == "ar"

        # User changes language to English
        store1.ui_language = "en"
        assert store1.ui_language == "en"

        # Simulate application restart: instantiate fresh AppStore from the same settings
        del store1
        fresh_settings = QSettings(ini_path, QSettings.Format.IniFormat)
        store2 = AppStore(settings=fresh_settings)

        # Loaded language on startup must be English
        assert store2.ui_language == "en"

        # Session 2: User changes language back to Arabic
        store2.ui_language = "ar"
        del store2

        # Simulate second restart
        final_settings = QSettings(ini_path, QSettings.Format.IniFormat)
        store3 = AppStore(settings=final_settings)
        assert store3.ui_language == "ar"

    def test_translation_manager_initialization_from_persisted_setting(self, temp_ini_settings, qapp):
        """TranslationManager bound to fresh AppStore adopts persisted language and layout."""
        settings, ini_path = temp_ini_settings

        # Pre-set settings to English
        settings.setValue("ui_language", "en")
        settings.sync()

        # Simulate fresh app startup
        store = AppStore(settings=settings)
        assert store.ui_language == "en"

        tm = TranslationManager()
        tm.bind_app_store(store)

        # TranslationManager must initialize to 'en' and set LTR
        assert tm.current_language == "en"
        assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight

    def test_app_store_rejects_invalid_language_codes(self, temp_ini_settings):
        """AppStore.ui_language validates input and rejects unsupported language codes."""
        settings, _ = temp_ini_settings
        store = AppStore(settings=settings)

        with pytest.raises(ValueError, match="Unsupported UI language"):
            store.ui_language = "es"

        with pytest.raises(ValueError, match="Unsupported UI language"):
            store.ui_language = ""

        # Remains valid 'ar'
        assert store.ui_language == "ar"
