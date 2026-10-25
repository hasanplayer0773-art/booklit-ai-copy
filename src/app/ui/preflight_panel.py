"""
Booklet_AI Desktop — Preflight Panel UI (STEP-6.4)

Provides the presentation and interactive review interface for the
two-tier preflight system:
  1. Pre-COM checks (Python layer, pre-generation)
  2. Post-generation checks (InDesign COM child process, post-composition)

Key architectural guarantees enforced here:
- AC-6.4.5: A clean pre-COM check with zero issues NEVER claims the document
  is print-ready. Print-readiness is only achieved when post-generation checks
  have also passed without blockers.
- Visual tier distinction: Pre-COM and Post-Generation checks are presented
  as distinct, non-interchangeable sections.
- AC-6.4.1b: Explicit override gate for blockers requiring a recorded reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.preflight import PreflightReport
from app.ui.i18n import get_current_language, tr


@dataclass(frozen=True)
class PreflightPresentationStatus:
    """Evaluated presentation state of the two-tier preflight."""

    status_text: str
    is_print_ready: bool
    can_proceed_to_generation: bool
    has_unresolved_blockers: bool
    tier1_clean: bool
    tier2_clean: bool


def evaluate_preflight_presentation(
    pre_com_report: PreflightReport,
    post_gen_report: Optional[PreflightReport] = None,
    has_override: bool = False,
    lang: Optional[str] = None,
) -> PreflightPresentationStatus:
    """Evaluate overall preflight status for display according to strict AC-6.4.5 rules.

    A pre-COM pass with zero issues is GUARANTEED NEVER to claim the document
    is print-ready; that claim is ONLY made after the post-generation check
    also passes without blockers.

    Args:
        pre_com_report: Report from run_pre_com_preflight().
        post_gen_report: Optional report from post-generation InDesign checks.
        has_override: True if an explicit user override has been logged.
        lang: Optional language code ('ar' or 'en'). Defaults to active UI language.

    Returns:
        PreflightPresentationStatus with safe display fields.
    """
    tier1_clean = not pre_com_report.has_blockers and len(pre_com_report.issues) == 0
    tier2_clean = (
        post_gen_report is not None
        and not post_gen_report.has_blockers
        and len(post_gen_report.issues) == 0
    )

    # 1. Unresolved blockers in Tier 1 without override
    if pre_com_report.has_blockers and not has_override:
        return PreflightPresentationStatus(
            status_text=tr("PreflightStatus", "status_blockers", lang=lang),
            is_print_ready=False,
            can_proceed_to_generation=False,
            has_unresolved_blockers=True,
            tier1_clean=False,
            tier2_clean=False,
        )

    # 2. Blockers overridden in Tier 1
    if pre_com_report.has_blockers and has_override and post_gen_report is None:
        return PreflightPresentationStatus(
            status_text=tr("PreflightStatus", "status_override", lang=lang),
            is_print_ready=False,
            can_proceed_to_generation=True,
            has_unresolved_blockers=False,
            tier1_clean=False,
            tier2_clean=False,
        )

    # 3. Tier 1 passed (clean or warnings only), but Tier 2 has not run yet.
    # CRITICAL (AC-6.4.5): Never claim print-ready!
    if post_gen_report is None:
        return PreflightPresentationStatus(
            status_text=tr("PreflightStatus", "status_tier1_clean", lang=lang),
            is_print_ready=False,
            can_proceed_to_generation=True,
            has_unresolved_blockers=False,
            tier1_clean=tier1_clean,
            tier2_clean=False,
        )

    # 4. Tier 2 has run: check if it has blockers
    if post_gen_report.has_blockers and not has_override:
        return PreflightPresentationStatus(
            status_text=tr("PreflightStatus", "status_tier2_failed", lang=lang),
            is_print_ready=False,
            can_proceed_to_generation=False,
            has_unresolved_blockers=True,
            tier1_clean=tier1_clean,
            tier2_clean=False,
        )

    # 5. Both tiers passed without blockers -> confirmed print-ready
    return PreflightPresentationStatus(
        status_text=tr("PreflightStatus", "status_print_ready", lang=lang),
        is_print_ready=True,
        can_proceed_to_generation=True,
        has_unresolved_blockers=False,
        tier1_clean=tier1_clean,
        tier2_clean=tier2_clean,
    )


class OverrideDialog(QDialog):
    """Dialog for explicit operator override of preflight blockers (AC-6.4.1b)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("تجاوز موانع الفحص المسبق صراحة")
        self.setMinimumWidth(400)
        self.setLayoutDirection(Qt.RightToLeft)

        layout = QVBoxLayout(self)

        info_label = QLabel(
            "تنبيه: أنت تقوم بتجاوز موانع فحص الطباعة صراحة.\n"
            "سيتم تسجيل هذا الإجراء في سجل العمليات لأغراض التدقيق."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #b71c1c; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(info_label)

        form = QFormLayout()
        self.reason_edit = QTextEdit()
        self.reason_edit.setPlaceholderText("اكتب سبب التجاوز هنا (إلزامي)...")
        form.addRow("سبب التجاوز:", self.reason_edit)

        self.user_edit = QLineEdit()
        self.user_edit.setText("المصمم")
        form.addRow("اسم المصمم / المشغل:", self.user_edit)
        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        button_box.button(QDialogButtonBox.Ok).setText("تأكيد التجاوز وتسجيله")
        button_box.button(QDialogButtonBox.Cancel).setText("إلغاء")
        button_box.accepted.connect(self._validate_and_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _validate_and_accept(self) -> None:
        if not self.reason_edit.toPlainText().strip():
            self.reason_edit.setStyleSheet("border: 1px solid red;")
            return
        self.accept()

    def get_override_data(self) -> tuple[str, str]:
        """Return (reason, user_identifier)."""
        return (
            self.reason_edit.toPlainText().strip(),
            self.user_edit.text().strip() or "designer",
        )


class PreflightPanel(QWidget):
    """Interactive Qt widget presenting two-tier Preflight Check (STEP-6.4)."""

    override_requested = Signal(str, str)  # (reason, user_identifier)

    def __init__(
        self,
        pre_com_report: PreflightReport,
        post_gen_report: Optional[PreflightReport] = None,
        has_override: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._pre_com_report = pre_com_report
        self._post_gen_report = post_gen_report
        self._has_override = has_override

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # ── Banner Header ─────────────────────────────────────────────
        status = evaluate_preflight_presentation(
            self._pre_com_report,
            self._post_gen_report,
            self._has_override,
        )

        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.StyledPanel)
        banner_layout = QHBoxLayout(self.banner)

        self.status_icon = QLabel()
        if status.is_print_ready:
            self.status_icon.setText("✅")
            self.banner.setStyleSheet(
                "background-color: #e8f5e9; border: 1px solid #4caf50; border-radius: 4px; padding: 8px;"
            )
        elif status.has_unresolved_blockers:
            self.status_icon.setText("⛔")
            self.banner.setStyleSheet(
                "background-color: #ffebee; border: 1px solid #f44336; border-radius: 4px; padding: 8px;"
            )
        else:
            self.status_icon.setText("ℹ️")
            self.banner.setStyleSheet(
                "background-color: #e3f2fd; border: 1px solid #2196f3; border-radius: 4px; padding: 8px;"
            )

        self.status_label = QLabel(status.status_text)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        banner_layout.addWidget(self.status_icon)
        banner_layout.addWidget(self.status_label, 1)

        main_layout.addWidget(self.banner)

        # ── Two Tier Distinction Display ──────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)

        # Section 1: Pre-COM Tier
        s1_title = QLabel("1. فحص ما قبل InDesign (الأولي)")
        s1_title.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 10px; color: #1565c0;")
        content_layout.addWidget(s1_title)
        self._render_report_section(content_layout, self._pre_com_report)

        # Section 2: Post-Generation Tier
        s2_title = QLabel("2. فحص ما بعد الإنشاء (النهائي في InDesign)")
        s2_title.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 15px; color: #2e7d32;")
        content_layout.addWidget(s2_title)

        if self._post_gen_report is not None:
            self._render_report_section(content_layout, self._post_gen_report)
        else:
            pending_label = QLabel("لم يتم بعد (سيعمل تلقائياً داخل InDesign بعد التوليد)")
            pending_label.setStyleSheet("color: #757575; font-style: italic;")
            content_layout.addWidget(pending_label)

        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)

        # ── Override Action Button (if blockers present) ──────────────
        if status.has_unresolved_blockers:
            self.override_btn = QPushButton("تسجيل تجاوز صريح للموانع للمتابعة...")
            self.override_btn.setStyleSheet(
                "background-color: #c62828; color: white; font-weight: bold; padding: 8px;"
            )
            self.override_btn.clicked.connect(self._open_override_dialog)
            main_layout.addWidget(self.override_btn)

    def _render_report_section(self, layout: QVBoxLayout, report: PreflightReport) -> None:
        if not report.issues:
            clean_label = QLabel("✔ لا توجد مشاكل مسجلة في هذه المرحلة.")
            clean_label.setStyleSheet("color: #2e7d32; margin-right: 15px;")
            layout.addWidget(clean_label)
            return

        for issue in report.issues:
            row = QHBoxLayout()
            icon = "⛔ [مانع]" if issue.severity == "blocker" else "⚠️ [تحذير]"
            badge = QLabel(icon)
            badge.setStyleSheet(
                "color: #c62828; font-weight: bold;"
                if issue.severity == "blocker"
                else "color: #ef6c00; font-weight: bold;"
            )
            msg = QLabel(issue.message)
            msg.setWordWrap(True)
            row.addWidget(badge)
            row.addWidget(msg, 1)
            layout.addLayout(row)

    def _open_override_dialog(self) -> None:
        dialog = OverrideDialog(self)
        if dialog.exec() == QDialog.Accepted:
            reason, user_id = dialog.get_override_data()
            self.override_requested.emit(reason, user_id)

    def retranslateUi(self) -> None:
        """Update preflight status banner on language change (AC-6.6.1)."""
        status = evaluate_preflight_presentation(
            self._pre_com_report,
            self._post_gen_report,
            self._has_override,
        )
        self.status_label.setText(status.status_text)
        if hasattr(self, "override_btn"):
            self.override_btn.setText(
                tr("PreflightPanel", "Record Blocker Override")
            )

    def changeEvent(self, event: QEvent) -> None:
        """Handle Qt LanguageChange event dynamically without restart (AC-6.6.1)."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)
