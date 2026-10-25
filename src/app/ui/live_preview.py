"""
Booklet_AI Desktop — Live Preview Renderer (STEP-6.3)

Renders the current DocumentStore state as a preview in the UI
WITHOUT triggering any COM/InDesign call.

Architecture:
- Reads directly from the existing in-memory DocumentStore for the
  currently-open job.
- Requires no new persistent storage — only renders the already-extracted,
  already-classified BookletDocument tree (STEP-1.1's Pydantic models)
  before the COM/InDesign step runs.
- Fully decoupled from InDesign: renders from ContentBlock/PageData/
  TableData alone.

Lock discipline (AC-6.3.1b):
- DocumentStore's RLock is held ONLY long enough to copy an immutable
  snapshot of the page(s) being rendered.
- The lock is released BEFORE any Qt painting, layout, or rendering
  work begins — rendering a large page while holding the lock would
  block classification updates, edits, undo, and job-progress signals.

No COM/InDesign dependency (AC-6.3.1):
- This module never imports win32com, pythoncom, or any COM-related module.
- The preview is a pure Python + Qt rendering of the logical content tree.
"""

import logging
from typing import TYPE_CHECKING, List, Optional

from PySide6.QtCore import QRectF, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from app.state.document_store import DocumentStore

from app.core.models import ContentBlock, PageData, TableData

logger = logging.getLogger("booklet_ai.live_preview")

# ═══════════════════════════════════════════════════════════════════════
# Block type → visual style mapping (for preview rendering)
# ═══════════════════════════════════════════════════════════════════════

_BLOCK_STYLE = {
    "chapter_header": {
        "font_size": 18,
        "bold": True,
        "color": QColor("#1a237e"),
        "bg": QColor("#e8eaf6"),
        "label": "عنوان الفصل",
    },
    "arabic_text": {
        "font_size": 13,
        "bold": False,
        "color": QColor("#212121"),
        "bg": QColor("#ffffff"),
        "label": "نص عربي",
    },
    "english_text": {
        "font_size": 13,
        "bold": False,
        "color": QColor("#37474f"),
        "bg": QColor("#fafafa"),
        "label": "نص إنجليزي",
    },
    "ministry_question": {
        "font_size": 13,
        "bold": False,
        "color": QColor("#b71c1c"),
        "bg": QColor("#ffebee"),
        "label": "سؤال وزاري",
    },
    "math_formula": {
        "font_size": 14,
        "bold": False,
        "color": QColor("#1b5e20"),
        "bg": QColor("#e8f5e9"),
        "label": "معادلة رياضية",
    },
    "chemistry_formula": {
        "font_size": 14,
        "bold": False,
        "color": QColor("#e65100"),
        "bg": QColor("#fff3e0"),
        "label": "معادلة كيميائية",
    },
    "image_prompt": {
        "font_size": 12,
        "bold": False,
        "color": QColor("#4a148c"),
        "bg": QColor("#f3e5f5"),
        "label": "صورة",
    },
    "list_item": {
        "font_size": 13,
        "bold": False,
        "color": QColor("#212121"),
        "bg": QColor("#f5f5f5"),
        "label": "عنصر قائمة",
    },
    "legacy_equation_placeholder": {
        "font_size": 12,
        "bold": True,
        "color": QColor("#ff6f00"),
        "bg": QColor("#fff8e1"),
        "label": "معادلة قديمة",
    },
}

_DEFAULT_STYLE = {
    "font_size": 13,
    "bold": False,
    "color": QColor("#212121"),
    "bg": QColor("#ffffff"),
    "label": "محتوى",
}


def _get_style(block_type: str) -> dict:
    """Get the visual style for a given block type."""
    return _BLOCK_STYLE.get(block_type, _DEFAULT_STYLE)


# ═══════════════════════════════════════════════════════════════════════
# Snapshot — immutable copy taken under the lock
# ═══════════════════════════════════════════════════════════════════════


class PageSnapshot:
    """Immutable snapshot of a single page's data for rendering.

    Taken while holding DocumentStore's RLock, then rendered after
    the lock is released (AC-6.3.1b).

    Attributes:
        page_number: 1-based page number.
        width: Page width in points.
        height: Page height in points.
        header_text: Running header text.
        footer_text: Running footer text.
        content_blocks: List of (block_id, block_type, raw_content, user_edited) tuples.
        tables: List of (table_id, headers, rows) tuples.
    """

    __slots__ = (
        "page_number",
        "width",
        "height",
        "header_text",
        "footer_text",
        "content_blocks",
        "tables",
    )

    def __init__(self, page: PageData) -> None:
        self.page_number = page.page_number
        self.width = page.width
        self.height = page.height
        self.header_text = page.header_text
        self.footer_text = page.footer_text
        self.content_blocks = [
            (b.block_id, b.block_type, b.raw_content, b.user_edited)
            for b in page.content_blocks
        ]
        self.tables = [
            (t.table_id, list(t.headers), [list(row) for row in t.rows])
            for t in page.tables
        ]


def take_snapshot(
    document_store: "DocumentStore",
    page_number: Optional[int] = None,
    doc_id: Optional[str] = None,
) -> List[PageSnapshot]:
    """Take an immutable snapshot of page(s) from DocumentStore.

    The RLock is held ONLY for the duration of copying the data.
    No Qt painting, rendering, or I/O occurs inside the lock.

    Args:
        document_store: The DocumentStore to snapshot from.
        page_number: If given, snapshot only this page. Otherwise all pages.
        doc_id: Optional document ID. If None, uses the active document.

    Returns:
        List of PageSnapshot objects (empty if document not found).
    """
    with document_store.lock:
        if page_number is not None:
            page = document_store.get_page_slice(page_number, doc_id)
            if page is None:
                return []
            return [PageSnapshot(page)]
        else:
            pages = document_store.get_all_pages(doc_id)
            return [PageSnapshot(p) for p in pages]
    # Lock is released here — all rendering happens AFTER this point.


# ═══════════════════════════════════════════════════════════════════════
# Preview Widget — Qt widget for rendering the snapshot
# ═══════════════════════════════════════════════════════════════════════


class BlockPreviewWidget(QWidget):
    """Renders a single ContentBlock's preview.

    Displays block type label, user-edited indicator, and the raw
    content text with styling based on block_type.
    """

    def __init__(
        self,
        block_id: str,
        block_type: str,
        raw_content: str,
        user_edited: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        style = _get_style(block_type)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        # Type label + edited badge
        header_layout = QHBoxLayout()
        type_label = QLabel(style["label"])
        type_label.setStyleSheet(
            f"font-weight: bold; color: {style['color'].name()}; font-size: 11px;"
        )
        header_layout.addWidget(type_label)

        if user_edited:
            edited_badge = QLabel("✏️ معدّل")
            edited_badge.setStyleSheet(
                "color: #ff6f00; font-size: 10px; font-weight: bold;"
            )
            header_layout.addWidget(edited_badge)

        header_layout.addStretch()

        id_label = QLabel(f"[{block_id}]")
        id_label.setStyleSheet("color: #9e9e9e; font-size: 9px;")
        header_layout.addWidget(id_label)

        layout.addLayout(header_layout)

        # Content text
        content_label = QLabel(raw_content)
        content_label.setWordWrap(True)
        content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        font = QFont("Amiri", style["font_size"])
        if style["bold"]:
            font.setBold(True)
        content_label.setFont(font)
        content_label.setStyleSheet(
            f"color: {style['color'].name()}; padding: 4px;"
        )

        layout.addWidget(content_label)

        # Widget background
        self.setStyleSheet(
            f"background-color: {style['bg'].name()}; "
            f"border: 1px solid #e0e0e0; border-radius: 4px; margin: 2px;"
        )
        self.setLayout(layout)


class TablePreviewWidget(QWidget):
    """Renders a table preview using a simple grid layout."""

    def __init__(
        self,
        table_id: str,
        headers: List[str],
        rows: List[List[str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        # Table header label
        title = QLabel(f"📊 جدول [{table_id}]")
        title.setStyleSheet("font-weight: bold; color: #0d47a1; font-size: 11px;")
        layout.addWidget(title)

        # Render as simple text representation for preview
        # (Full table rendering is InDesign's job)
        if headers:
            header_text = " | ".join(headers)
            header_label = QLabel(header_text)
            header_label.setStyleSheet(
                "font-weight: bold; color: #1565c0; padding: 2px; "
                "border-bottom: 2px solid #1565c0;"
            )
            header_label.setWordWrap(True)
            header_label.setFont(QFont("Amiri", 12))
            layout.addWidget(header_label)

        for row in rows[:10]:  # Show first 10 rows max in preview
            row_text = " | ".join(row)
            row_label = QLabel(row_text)
            row_label.setWordWrap(True)
            row_label.setFont(QFont("Amiri", 11))
            row_label.setStyleSheet(
                "padding: 2px; border-bottom: 1px solid #e0e0e0;"
            )
            layout.addWidget(row_label)

        if len(rows) > 10:
            more_label = QLabel(f"... و {len(rows) - 10} صفوف أخرى")
            more_label.setStyleSheet("color: #757575; font-size: 10px;")
            layout.addWidget(more_label)

        self.setStyleSheet(
            "background-color: #e3f2fd; border: 1px solid #90caf9; "
            "border-radius: 4px; margin: 2px;"
        )


class PagePreviewWidget(QWidget):
    """Renders a single page's preview with all its blocks and tables."""

    def __init__(
        self,
        snapshot: PageSnapshot,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        # Page header
        page_header = QLabel(f"— صفحة {snapshot.page_number} —")
        page_header.setAlignment(Qt.AlignCenter)
        page_header.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #424242; "
            "padding: 4px; margin-bottom: 4px;"
        )
        layout.addWidget(page_header)

        # Running header
        if snapshot.header_text:
            header = QLabel(snapshot.header_text)
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet(
                "color: #757575; font-size: 10px; font-style: italic; "
                "border-bottom: 1px solid #bdbdbd; padding-bottom: 4px;"
            )
            layout.addWidget(header)

        # Content blocks
        for block_id, block_type, content, user_edited in snapshot.content_blocks:
            block_widget = BlockPreviewWidget(
                block_id=block_id,
                block_type=block_type,
                raw_content=content,
                user_edited=user_edited,
            )
            layout.addWidget(block_widget)

        # Tables
        for table_id, headers, rows in snapshot.tables:
            table_widget = TablePreviewWidget(
                table_id=table_id,
                headers=headers,
                rows=rows,
            )
            layout.addWidget(table_widget)

        # Running footer
        if snapshot.footer_text:
            footer = QLabel(snapshot.footer_text)
            footer.setAlignment(Qt.AlignCenter)
            footer.setStyleSheet(
                "color: #757575; font-size: 10px; font-style: italic; "
                "border-top: 1px solid #bdbdbd; padding-top: 4px;"
            )
            layout.addWidget(footer)

        self.setStyleSheet(
            "background-color: #ffffff; border: 2px solid #bdbdbd; "
            "border-radius: 6px; margin: 8px;"
        )


class LivePreviewPanel(QWidget):
    """Main Live Preview panel widget.

    Displays the current DocumentStore state as a scrollable preview
    of all pages, without any COM/InDesign dependency.

    Signals:
        page_changed: Emitted with the current page number when navigation occurs.

    Usage:
        panel = LivePreviewPanel(document_store)
        panel.refresh()  # Snapshot + render
    """

    page_changed = Signal(int)

    def __init__(
        self,
        document_store: "DocumentStore",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._document_store = document_store
        self._snapshots: List[PageSnapshot] = []
        self._current_page = 1

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the preview panel UI."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Navigation bar
        nav_layout = QHBoxLayout()
        self._prev_btn = QPushButton("◀ السابقة")
        self._prev_btn.clicked.connect(self._go_prev)
        self._next_btn = QPushButton("التالية ▶")
        self._next_btn.clicked.connect(self._go_next)
        self._page_label = QLabel("صفحة 0 / 0")
        self._page_label.setAlignment(Qt.AlignCenter)
        self._page_label.setStyleSheet("font-weight: bold; font-size: 13px;")

        self._refresh_btn = QPushButton("🔄 تحديث")
        self._refresh_btn.clicked.connect(self.refresh)

        nav_layout.addWidget(self._prev_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(self._page_label)
        nav_layout.addStretch()
        nav_layout.addWidget(self._next_btn)
        nav_layout.addWidget(self._refresh_btn)
        main_layout.addLayout(nav_layout)

        # Scroll area for page content
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._content_container = QWidget()
        self._content_layout = QVBoxLayout(self._content_container)
        self._scroll.setWidget(self._content_container)
        main_layout.addWidget(self._scroll)

        self.setLayout(main_layout)

    @Slot()
    def refresh(self) -> None:
        """Take a fresh snapshot from DocumentStore and re-render.

        The lock is held ONLY during take_snapshot() — all rendering
        happens after the lock is released (AC-6.3.1b).
        """
        # Step 1: snapshot under the lock (brief)
        self._snapshots = take_snapshot(self._document_store)

        # Step 2: render AFTER the lock is released
        if self._current_page > len(self._snapshots):
            self._current_page = max(1, len(self._snapshots))
        self._render_current_page()

    def _render_current_page(self) -> None:
        """Render the current page's snapshot into the scroll area.

        This is pure Qt rendering — no DocumentStore lock, no COM/InDesign.
        """
        # Clear existing content
        while self._content_layout.count():
            child = self._content_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        total = len(self._snapshots)
        if total == 0:
            empty_label = QLabel("لا يوجد محتوى للعرض")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: #757575; font-size: 14px; padding: 40px;")
            self._content_layout.addWidget(empty_label)
            self._page_label.setText("صفحة 0 / 0")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            return

        idx = self._current_page - 1
        if 0 <= idx < total:
            page_widget = PagePreviewWidget(self._snapshots[idx])
            self._content_layout.addWidget(page_widget)

        self._page_label.setText(f"صفحة {self._current_page} / {total}")
        self._prev_btn.setEnabled(self._current_page > 1)
        self._next_btn.setEnabled(self._current_page < total)
        self.page_changed.emit(self._current_page)

    @Slot()
    def _go_prev(self) -> None:
        """Navigate to the previous page."""
        if self._current_page > 1:
            self._current_page -= 1
            self._render_current_page()

    @Slot()
    def _go_next(self) -> None:
        """Navigate to the next page."""
        if self._current_page < len(self._snapshots):
            self._current_page += 1
            self._render_current_page()

    def go_to_page(self, page_number: int) -> None:
        """Jump to a specific page.

        Args:
            page_number: 1-based page number to display.
        """
        if 1 <= page_number <= len(self._snapshots):
            self._current_page = page_number
            self._render_current_page()

    @property
    def total_pages(self) -> int:
        """Return the total number of pages in the current snapshot."""
        return len(self._snapshots)
