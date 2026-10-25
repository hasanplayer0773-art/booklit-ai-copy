"""
Booklet_AI Desktop — Dashboard UI Panel (STEP-6.9)

User-facing dashboard panel providing print-shop owners with aggregate
visibility into shop-level throughput and performance over time:
- Booklets processed (status='success' count) (AC-6.9.1, AC-6.9.1b).
- Average processing time (status='success' duration, excluding queue wait) (AC-6.9.4).
- Error rate (failed / (failed + success), excluding in_progress/cancelled) (AC-6.9.2).
- Filterable by time period (All Time, Today, Last 7 Days, Last 30 Days).
- Supplementary recent jobs table with status, duration, and translated error summaries.

Concurrency & Lock Discipline:
- Read-only view over SQLite job_history table (STEP-6.9 §2).
- NEVER imports or acquires DocumentStore's threading.RLock (AC-6.9.3, AC-6.3.5).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional, Union

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.state.dashboard import (
    DashboardAggregator,
    DashboardMetrics,
    TimePeriod,
)

logger = logging.getLogger("booklet_ai.ui.dashboard")


def format_duration(seconds: float) -> str:
    """Format duration in seconds into a friendly localized Arabic display string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string (e.g. '14.5 ثانية' or '2 د 15 ث').
    """
    if seconds <= 0.0:
        return "0 ثانية"
    if seconds < 60.0:
        return f"{seconds:.1f} ثانية"
    mins = int(seconds // 60)
    rem_secs = int(seconds % 60)
    return f"{mins} د {rem_secs} ث"


class MetricCard(QFrame):
    """Reusable card component for prominent KPI metric display."""

    def __init__(
        self,
        title: str,
        initial_value: str = "0",
        description: str = "",
        color_accent: str = "#0078d4",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"""
            MetricCard {{
                background-color: #fdfdfd;
                border: 1px solid #e1dfdd;
                border-top: 4px solid {color_accent};
                border-radius: 6px;
                padding: 8px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("font-size: 13px; color: #605e5c; font-weight: 500;")
        layout.addWidget(self._title_label)

        self._value_label = QLabel(initial_value)
        self._value_label.setStyleSheet(
            f"font-size: 26px; font-weight: bold; color: {color_accent};"
        )
        layout.addWidget(self._value_label)

        if description:
            self._desc_label = QLabel(description)
            self._desc_label.setStyleSheet("font-size: 11px; color: #8a8886;")
            self._desc_label.setWordWrap(True)
            layout.addWidget(self._desc_label)

    def set_value(self, value: str) -> None:
        """Update the displayed metric value."""
        self._value_label.setText(value)


class DashboardPanel(QWidget):
    """User-facing widget for aggregated shop-level usage statistics (STEP-6.9).

    Never acquires DocumentStore's threading.RLock (AC-6.9.3).
    """

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        aggregator: Optional[DashboardAggregator] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._db_conn = db_conn
        self._aggregator = aggregator or DashboardAggregator(db_conn)
        self._current_metrics = DashboardMetrics()
        self._selected_period = TimePeriod.ALL_TIME

        self._init_ui()
        self.refresh()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # 1. Header Bar: Title & Period Selector
        header_layout = QHBoxLayout()

        title_container = QVBoxLayout()
        self._title_label = QLabel(self.tr("لوحة التحكم — إحصائيات المطبعة"))
        self._title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #323130;")
        self._subtitle_label = QLabel(
            self.tr("نظرة مجمّعة لأداء وإنتاجية الكتيبات ومعدل الجودة عبر الوقت")
        )
        self._subtitle_label.setStyleSheet("font-size: 12px; color: #605e5c;")
        title_container.addWidget(self._title_label)
        title_container.addWidget(self._subtitle_label)
        header_layout.addLayout(title_container)

        header_layout.addStretch()

        # Period Filter Combo
        period_label = QLabel(self.tr("الفترة الزمنية:"))
        period_label.setStyleSheet("font-weight: 500;")
        header_layout.addWidget(period_label)

        self._period_combo = QComboBox()
        self._period_combo.addItem(self.tr("كل الأوقات"), TimePeriod.ALL_TIME.value)
        self._period_combo.addItem(self.tr("اليوم"), TimePeriod.TODAY.value)
        self._period_combo.addItem(self.tr("آخر 7 أيام"), TimePeriod.LAST_7_DAYS.value)
        self._period_combo.addItem(self.tr("آخر 30 يوماً"), TimePeriod.LAST_30_DAYS.value)
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)
        header_layout.addWidget(self._period_combo)

        # Refresh Button
        self._refresh_btn = QPushButton(self.tr("تحديث البيانات"))
        self._refresh_btn.setStyleSheet(
            "padding: 6px 14px; font-weight: 500; background-color: #0078d4; color: white; border-radius: 4px;"
        )
        self._refresh_btn.clicked.connect(self.refresh)
        header_layout.addWidget(self._refresh_btn)

        layout.addLayout(header_layout)

        # 2. KPI Cards Grid
        cards_grid = QGridLayout()
        cards_grid.setSpacing(12)

        # Card 1: Booklets Processed (AC-6.9.1)
        self._card_processed = MetricCard(
            title=self.tr("الكتيبات المنجزة"),
            initial_value="0",
            description=self.tr("إجمالي الكتيبات المكتملة بنجاح في الفترة المحددة"),
            color_accent="#107c41",  # Green accent
        )
        cards_grid.addWidget(self._card_processed, 0, 0)

        # Card 2: Average Processing Time (AC-6.9.4)
        self._card_avg_time = MetricCard(
            title=self.tr("متوسط وقت المعالجة"),
            initial_value="0 ثانية",
            description=self.tr("للمهام الناجحة فقط (يستثني وقت الانتظار بالطابور)"),
            color_accent="#0078d4",  # Blue accent
        )
        cards_grid.addWidget(self._card_avg_time, 0, 1)

        # Card 3: Error Rate (AC-6.9.2)
        self._card_error_rate = MetricCard(
            title=self.tr("معدل الأخطاء"),
            initial_value="0.0%",
            description=self.tr("نسبة المهام الفاشلة من إجمالي المهام المكتملة"),
            color_accent="#d83b01",  # Orange/Red accent
        )
        cards_grid.addWidget(self._card_error_rate, 0, 2)

        # Card 4: Total Completed vs Failed
        self._card_completed = MetricCard(
            title=self.tr("إجمالي المهام المكتملة"),
            initial_value="0",
            description=self.tr("مجموع المهام المحسومة (ناجحة + فاشلة)"),
            color_accent="#5c2d91",  # Purple accent
        )
        cards_grid.addWidget(self._card_completed, 0, 3)

        layout.addLayout(cards_grid)

        # 3. Supplementary Section: Recent Jobs Table
        table_label = QLabel(self.tr("سجل المهام الأخيرة:"))
        table_label.setStyleSheet("font-size: 14px; font-weight: bold; margin-top: 8px;")
        layout.addWidget(table_label)

        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            self.tr("المعرّف"),
            self.tr("الملف المصدر"),
            self.tr("الحالة"),
            self.tr("الصفحات"),
            self.tr("تاريخ البدء"),
            self.tr("ملاحظات / سبب الفشل"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table)

    @Slot()
    def _on_period_changed(self) -> None:
        """Handle user changing the period combo box."""
        period_val = self._period_combo.currentData()
        self._selected_period = TimePeriod(period_val)
        self.refresh()

    def set_period(self, period: Union[TimePeriod, str]) -> None:
        """Programmatically switch time period and refresh."""
        if isinstance(period, str):
            period = TimePeriod(period)
        self._selected_period = period
        idx = self._period_combo.findData(period.value)
        if idx >= 0:
            self._period_combo.setCurrentIndex(idx)
        else:
            self.refresh()

    def get_current_metrics(self) -> DashboardMetrics:
        """Return the most recently computed DashboardMetrics."""
        return self._current_metrics

    @Slot()
    def refresh(self) -> None:
        """Recompute aggregate figures directly from job_history and refresh UI.

        Satisfies AC-6.9.2: figures are recomputed on each view without stale caching.
        Satisfies AC-6.9.3: never acquires DocumentStore.lock.
        """
        # 1. Fetch fresh aggregate metrics via SQL
        metrics = self._aggregator.get_metrics(period=self._selected_period)
        self._current_metrics = metrics

        # 2. Update KPI Cards
        self._card_processed.set_value(str(metrics.booklets_processed))
        self._card_avg_time.set_value(
            format_duration(metrics.average_processing_time_seconds)
        )
        self._card_error_rate.set_value(f"{metrics.error_rate_percentage:.1f}%")
        self._card_completed.set_value(
            f"{metrics.total_completed} ({metrics.booklets_processed} ناجح / {metrics.failed_jobs} فاشل)"
        )

        # 3. Populate Recent Jobs Table
        recent_jobs = self._aggregator.get_recent_jobs(
            limit=30,
            start_time=metrics.start_time,
            end_time=metrics.end_time,
        )
        self._table.setRowCount(len(recent_jobs))
        for row_idx, job in enumerate(recent_jobs):
            # ID
            id_item = QTableWidgetItem(str(job["id"]))
            id_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row_idx, 0, id_item)

            # Source File
            file_path = job.get("source_file_path", "")
            self._table.setItem(row_idx, 1, QTableWidgetItem(file_path))

            # Status with styling
            status = job.get("status", "")
            status_item = QTableWidgetItem()
            if status == "success":
                status_item.setText(self.tr("ناجح"))
                status_item.setForeground(Qt.darkGreen)
            elif status == "failed":
                status_item.setText(self.tr("فاشل"))
                status_item.setForeground(Qt.red)
            elif status == "in_progress":
                status_item.setText(self.tr("قيد التنفيذ"))
                status_item.setForeground(Qt.blue)
            else:
                status_item.setText(status)
            status_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row_idx, 2, status_item)

            # Page Count
            pages = job.get("page_count")
            pages_text = str(pages) if pages is not None else "—"
            pages_item = QTableWidgetItem(pages_text)
            pages_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row_idx, 3, pages_item)

            # Started At
            started_at = job.get("started_at", "")
            if started_at:
                started_at = started_at[:19].replace("T", " ")
            self._table.setItem(row_idx, 4, QTableWidgetItem(started_at))

            # Error Summary or Success Note
            error_note = job.get("error_summary", "") or ""
            self._table.setItem(row_idx, 5, QTableWidgetItem(error_note))
