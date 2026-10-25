"""
Booklet_AI Desktop — Error Display Widget (STEP-6.6)

User-facing widget for rendering translated errors.
Supports dynamic language retranslation when the application UI
language changes, without requiring an application restart (AC-6.6.1).
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.error_translation import UserFacingError, translate_error
from app.ui.i18n import get_translation_manager

logger = logging.getLogger("booklet_ai.ui.error_display")


class ErrorBanner(QFrame):
    """Visual banner that renders translated operator-safe errors.

    Guarantees:
    - [AC-6.6.1] Switching the UI language setting updates any
      currently-displayed translated error message without requiring
      an application restart.
    - Preserves the original exception reference for problem report generation.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._current_exc: Optional[Exception] = None
        self._current_user_error: Optional[UserFacingError] = None

        self._init_ui()
        self.setVisible(False)

        # Connect to TranslationManager signal as well for instant retranslation
        get_translation_manager().language_changed.connect(self._on_language_changed)

    def _init_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setStyleSheet(
            "QFrame { "
            "  background-color: #fff3cd; "
            "  border: 1px solid #ffeeba; "
            "  border-radius: 4px; "
            "  padding: 8px; "
            "}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        self._icon_label = QLabel("⚠️")
        self._icon_label.setStyleSheet("font-size: 18px;")
        layout.addWidget(self._icon_label)

        self._message_label = QLabel("")
        self._message_label.setWordWrap(True)
        self._message_label.setStyleSheet("color: #856404; font-weight: 500;")
        layout.addWidget(self._message_label, stretch=1)

        self._dismiss_btn = QPushButton("✕")
        self._dismiss_btn.setFixedSize(24, 24)
        self._dismiss_btn.setStyleSheet(
            "QPushButton { border: none; font-weight: bold; color: #856404; }"
            "QPushButton:hover { background-color: #ffe8a1; border-radius: 12px; }"
        )
        self._dismiss_btn.clicked.connect(self.clear)
        layout.addWidget(self._dismiss_btn)

    @property
    def current_exception(self) -> Optional[Exception]:
        """The underlying technical exception currently displayed."""
        return self._current_exc

    @property
    def current_user_error(self) -> Optional[UserFacingError]:
        """The UserFacingError currently displayed."""
        return self._current_user_error

    @property
    def displayed_text(self) -> str:
        """The text currently displayed on the banner."""
        return self._message_label.text()

    def show_error(self, exc: Exception) -> None:
        """Display an error translated to the current UI language.

        Args:
            exc: Exception to translate and display.
        """
        self._current_exc = exc
        self._current_user_error = translate_error(exc)
        self._message_label.setText(self._current_user_error.translated_message)
        self.setVisible(True)

    def clear(self) -> None:
        """Dismiss and clear the error banner."""
        self._current_exc = None
        self._current_user_error = None
        self._message_label.setText("")
        self.setVisible(False)

    def retranslateUi(self) -> None:
        """Retranslate currently-displayed error message without restart (AC-6.6.1)."""
        if self._current_exc is not None:
            self._current_user_error = translate_error(self._current_exc)
            self._message_label.setText(self._current_user_error.translated_message)

    def _on_language_changed(self, lang: str) -> None:
        self.retranslateUi()

    def changeEvent(self, event: QEvent) -> None:
        """Respond to Qt LanguageChange events."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)
