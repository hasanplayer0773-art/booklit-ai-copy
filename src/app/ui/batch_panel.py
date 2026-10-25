"""
Booklet_AI Desktop — Batch Queue UI Panel (STEP-6.7)

Provides operator UI for monitoring and managing the sequential batch queue:
- Displays queued files and live per-job progress.
- Supports adding files, starting processing, and cancelling the remaining queue.
- Shows final batch summary (N succeeded, M failed).
- Provides access to translated error summaries and Problem Reports for failed jobs.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.automation.batch_queue import BatchQueue, BatchQueueSummary
from app.ui.error_translation import translate_error

logger = logging.getLogger("booklet_ai.ui.batch_panel")


class BatchPanel(QWidget):
    """User-facing widget for sequential batch booklet processing."""

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        job_config: Optional[Any] = None,
        generation_fn: Optional[Callable[..., Any]] = None,
        extraction_fn: Optional[Callable[..., Any]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._db_conn = db_conn
        self._job_config = job_config
        self._generation_fn = generation_fn
        self._extraction_fn = extraction_fn

        self._file_paths: List[str] = []
        self._worker: Optional[BatchQueue] = None
        self._last_summary: Optional[dict] = None

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Title & Description
        self._title_label = QLabel(self.tr("معالجة الدفعات — طابور المعالجة التسلسلي"))
        self._title_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self._title_label)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self._add_files_btn = QPushButton(self.tr("إضافة ملفات..."))
        self._add_files_btn.clicked.connect(self._on_add_files)
        btn_layout.addWidget(self._add_files_btn)

        self._clear_btn = QPushButton(self.tr("مسح القائمة"))
        self._clear_btn.clicked.connect(self._on_clear_queue)
        btn_layout.addWidget(self._clear_btn)

        self._start_btn = QPushButton(self.tr("بدء المعالجة"))
        self._start_btn.setStyleSheet("font-weight: bold; background-color: #0078d4; color: white;")
        self._start_btn.clicked.connect(self.start_batch)
        btn_layout.addWidget(self._start_btn)

        self._cancel_btn = QPushButton(self.tr("إلغاء المتبقي من الطابور"))
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancel_batch)
        btn_layout.addWidget(self._cancel_btn)

        layout.addLayout(btn_layout)

        # Queue Table
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([
            self.tr("الملف"),
            self.tr("الحالة"),
            self.tr("معرّف المهمة"),
            self.tr("التفاصيل / التقرير"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        # Progress Section
        progress_box = QFrame()
        progress_box.setFrameShape(QFrame.Shape.StyledPanel)
        p_layout = QVBoxLayout(progress_box)

        self._status_label = QLabel(self.tr("جاهز لبدء معالجة الملفات."))
        p_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        p_layout.addWidget(self._progress_bar)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet("font-weight: bold;")
        p_layout.addWidget(self._summary_label)

        layout.addWidget(progress_box)

    def set_files(self, file_paths: List[str]) -> None:
        """Set the queued file paths directly."""
        self._file_paths = list(file_paths)
        self._refresh_table()

    def _on_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("اختر ملفات الملازم"),
            "",
            self.tr("المستندات المدعومة (*.docx *.pdf)"),
        )
        if files:
            self._file_paths.extend(files)
            self._refresh_table()

    def _on_clear_queue(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._file_paths.clear()
        self._refresh_table()
        self._status_label.setText(self.tr("تم مسح القائمة."))
        self._progress_bar.setValue(0)
        self._summary_label.setText("")

    def _refresh_table(self) -> None:
        self._table.setRowCount(len(self._file_paths))
        for row, path in enumerate(self._file_paths):
            item_path = QTableWidgetItem(path)
            item_path.setFlags(item_path.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, item_path)

            item_status = QTableWidgetItem(self.tr("قيد الانتظار"))
            item_status.setFlags(item_status.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, item_status)

            item_id = QTableWidgetItem("-")
            item_id.setFlags(item_id.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 2, item_id)

            item_details = QTableWidgetItem("-")
            item_details.setFlags(item_details.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 3, item_details)

    def start_batch(self) -> None:
        """Start batch processing on the dedicated worker thread."""
        if not self._file_paths:
            QMessageBox.information(
                self,
                self.tr("تنبيه"),
                self.tr("يرجى إضافة ملف واحد على الأقل للمعالجة."),
            )
            return

        if self._worker and self._worker.isRunning():
            return

        self._start_btn.setEnabled(False)
        self._add_files_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        self._worker = BatchQueue(
            file_paths=self._file_paths,
            db_conn=self._db_conn,
            job_config=self._job_config,
            generation_fn=self._generation_fn,
            extraction_fn=self._extraction_fn,
            parent=self,
        )

        self._worker.job_started.connect(self._on_job_started)
        self._worker.job_succeeded.connect(self._on_job_succeeded)
        self._worker.job_failed.connect(self._on_job_failed)
        self._worker.queue_progress.connect(self._on_queue_progress)
        self._worker.queue_finished.connect(self._on_queue_finished)
        self._worker.queue_cancelled.connect(self._on_queue_cancelled)

        self._progress_bar.setValue(0)
        self._summary_label.setText("")
        self._worker.start()

    def cancel_batch(self) -> None:
        """Request cancellation of remaining queue."""
        if self._worker and self._worker.isRunning():
            self._worker.cancel_queue()
            self._cancel_btn.setEnabled(False)
            self._status_label.setText(
                self.tr("تم طلب إلغاء المهام المتبقية بعد اكتمال المهمة الحالية...")
            )

    @Slot(str, int, int)
    def _on_job_started(self, file_path: str, idx: int, total: int) -> None:
        row = idx - 1
        if 0 <= row < self._table.rowCount():
            item = self._table.item(row, 1)
            if item:
                item.setText(self.tr("جارٍ المعالجة..."))
        self._status_label.setText(
            self.tr("معالجة الملف {0} من {1}: {2}").format(idx, total, file_path)
        )

    @Slot(str, int)
    def _on_job_succeeded(self, file_path: str, job_id: int) -> None:
        row = self._find_row_for_file(file_path)
        if row is not None:
            status_item = self._table.item(row, 1)
            if status_item:
                status_item.setText(self.tr("ناجح ✓"))
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            id_item = self._table.item(row, 2)
            if id_item:
                id_item.setText(str(job_id))

    @Slot(str, str, int)
    def _on_job_failed(self, file_path: str, error_summary: str, job_id: int) -> None:
        row = self._find_row_for_file(file_path)
        if row is not None:
            status_item = self._table.item(row, 1)
            if status_item:
                status_item.setText(self.tr("فشل ✗"))
                status_item.setForeground(Qt.GlobalColor.red)
            id_item = self._table.item(row, 2)
            if id_item:
                id_item.setText(str(job_id))
            det_item = self._table.item(row, 3)
            if det_item:
                det_item.setText(error_summary)

    @Slot(int, int)
    def _on_queue_progress(self, completed: int, total: int) -> None:
        if total > 0:
            pct = int((completed / total) * 100)
            self._progress_bar.setValue(pct)

    @Slot(dict)
    def _on_queue_finished(self, summary_dict: dict) -> None:
        self._last_summary = summary_dict
        succeeded = summary_dict.get("succeeded", 0)
        failed = summary_dict.get("failed", 0)
        cancelled = summary_dict.get("cancelled", 0)
        total = summary_dict.get("total", 0)

        msg = self.tr(
            "اكتملت معالجة الدفعة: {0} نجح، {1} فشل، {2} ملغى (المجموع: {3})"
        ).format(succeeded, failed, cancelled, total)
        self._summary_label.setText(msg)
        self._status_label.setText(self.tr("اكتمل تشغيل الطابور."))

        self._start_btn.setEnabled(True)
        self._add_files_btn.setEnabled(True)
        self._clear_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

    @Slot(int, int)
    def _on_queue_cancelled(self, completed: int, remaining: int) -> None:
        for row in range(completed, len(self._file_paths)):
            status_item = self._table.item(row, 1)
            if status_item and status_item.text() == self.tr("قيد الانتظار"):
                status_item.setText(self.tr("ملغى"))
                status_item.setForeground(Qt.GlobalColor.darkGray)

    def _find_row_for_file(self, file_path: str) -> Optional[int]:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.text() == file_path:
                return row
        return None
