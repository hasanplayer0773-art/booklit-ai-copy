"""
Booklet_AI Desktop — Field Error Translation (STEP-6.2 & STEP-6.6)

Translates technical, English-language exception messages into
operator-safe messages (Arabic or English) at the UI presentation boundary.

Architecture:
- translate_error() sits strictly at the UI boundary — never inside
  app/core/, app/automation/, or app/ai/.
- Lookup is MRO-based (most-specific class first), NOT type-exact.
  A future subclass of any already-mapped exception inherits its
  parent's translation automatically, with no table update required
  unless a more specific message is desired.
- The original exception is ALWAYS preserved on UserFacingError.original
  for diagnostic purposes (problem reports, logging).
- STEP-6.2's ERROR_TRANSLATIONS entries and their English equivalents are
  unified in Qt's translation system (.ts/.qm resource mechanism) —
  not a separate parallel table (AC-6.6.2).
"""

import logging
from typing import Dict, Optional, Type

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
from app.ui.i18n import get_current_language, tr

# Optional imports — these exception types are defined outside
# app/core/exceptions.py but still have translations.
try:
    from app.core.pdf_extractor import PDFExtractionError
except ImportError:  # pragma: no cover
    PDFExtractionError = None  # type: ignore[assignment,misc]

try:
    from app.ai.client import InvalidClassificationResponse
except ImportError:  # pragma: no cover
    InvalidClassificationResponse = None  # type: ignore[assignment,misc]

try:
    from app.ai.embedding_client import EmbeddingError
except ImportError:  # pragma: no cover
    EmbeddingError = None  # type: ignore[assignment,misc]

try:
    from app.core.exceptions import PreflightBlockerError
except ImportError:  # pragma: no cover
    PreflightBlockerError = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

ERROR_TRANSLATION_CONTEXT = "ErrorTranslation"


# ═══════════════════════════════════════════════════════════════════════
# UserFacingError — wrapper for translated exceptions
# ═══════════════════════════════════════════════════════════════════════


class UserFacingError(Exception):
    """Wraps any exception for display to a non-technical operator.

    Carries the translated message plus a reference to the original
    exception, which is preserved untranslated for the Send Problem
    Report artifact and for logging — translation never replaces or
    discards the original diagnostic information (AC-6.2.2).
    """

    def __init__(self, translated_message: str, original: Exception) -> None:
        self.translated_message = translated_message
        self.original = original
        super().__init__(translated_message)


# ═══════════════════════════════════════════════════════════════════════
# Exception → Translation Key Mapping (AC-6.6.2)
# ═══════════════════════════════════════════════════════════════════════

EXCEPTION_TRANSLATION_KEYS: Dict[Type[Exception], str] = {
    TextIntegrityError: "TextIntegrityError",
    MathExtractionError: "MathExtractionError",
    COMOperationTimeout: "COMOperationTimeout",
    InDesignUnavailableError: "InDesignUnavailableError",
    InvalidBodyFrameGeometryError: "InvalidBodyFrameGeometryError",
    ContentExceedsEstimateError: "ContentExceedsEstimateError",
    MarginConfigError: "MarginConfigError",
    StyleMappingError: "StyleMappingError",
    RTLTableUnsupportedError: "RTLTableUnsupportedError",
    QuestionIndexError: "QuestionIndexError",
    BookletError: "BookletError",
}

if PDFExtractionError is not None:
    EXCEPTION_TRANSLATION_KEYS[PDFExtractionError] = "PDFExtractionError"

if InvalidClassificationResponse is not None:
    EXCEPTION_TRANSLATION_KEYS[InvalidClassificationResponse] = "InvalidClassificationResponse"

if EmbeddingError is not None:
    EXCEPTION_TRANSLATION_KEYS[EmbeddingError] = "EmbeddingError"

if PreflightBlockerError is not None:
    EXCEPTION_TRANSLATION_KEYS[PreflightBlockerError] = "PreflightBlockerError"


def get_error_message(key: str, lang: Optional[str] = None) -> str:
    """Retrieve error message from the unified Qt translation mechanism (AC-6.6.2)."""
    return tr(ERROR_TRANSLATION_CONTEXT, key, lang=lang)


# ═══════════════════════════════════════════════════════════════════════
# Unified Translation Tables — Sourced from .ts/.qm mechanism (AC-6.6.2)
# ═══════════════════════════════════════════════════════════════════════

ERROR_TRANSLATIONS: Dict[Type[Exception], str] = {
    exc_cls: get_error_message(key, "ar")
    for exc_cls, key in EXCEPTION_TRANSLATION_KEYS.items()
}

ERROR_TRANSLATIONS_EN: Dict[Type[Exception], str] = {
    exc_cls: get_error_message(key, "en")
    for exc_cls, key in EXCEPTION_TRANSLATION_KEYS.items()
}

FALLBACK_MESSAGE: str = get_error_message("FALLBACK_MESSAGE", "ar")
FALLBACK_MESSAGE_EN: str = get_error_message("FALLBACK_MESSAGE", "en")


# ═══════════════════════════════════════════════════════════════════════
# translate_error() — MRO-based bilingual lookup (AC-6.6.1, AC-6.6.2)
# ═══════════════════════════════════════════════════════════════════════


def translate_error(
    exc: Exception,
    lang: Optional[str] = None,
) -> UserFacingError:
    """Translate a technical exception into an operator-safe message.

    Walks exc's Method Resolution Order (most specific class first) and
    returns the first matching translation entry — NOT a type(exc)
    exact match. This guarantees a future subclass of any already-mapped
    exception inherits its parent's translation automatically.

    Supports both Arabic and English locales. If `lang` is omitted, the
    active UI language from TranslationManager is used (AC-6.6.1).

    An exception type with no match anywhere in its MRO falls back to
    FALLBACK_MESSAGE (or FALLBACK_MESSAGE_EN) — it is never left
    unhandled or shown raw to the operator (AC-6.2.5).

    Args:
        exc: The exception to translate.
        lang: Optional language code ('ar' or 'en'). Defaults to current UI language.

    Returns:
        UserFacingError wrapping the translated message and preserving
        the original exception in .original.
    """
    effective_lang = lang or get_current_language()

    for klass in type(exc).__mro__:
        if klass in EXCEPTION_TRANSLATION_KEYS:
            key = EXCEPTION_TRANSLATION_KEYS[klass]
            msg = get_error_message(key, lang=effective_lang)
            return UserFacingError(msg, original=exc)

    fallback = get_error_message("FALLBACK_MESSAGE", lang=effective_lang)
    return UserFacingError(fallback, original=exc)
