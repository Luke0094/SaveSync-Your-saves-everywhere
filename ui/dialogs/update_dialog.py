"""Dialog: a newer SaveSync release is on GitHub.

Visual language matches the "please wait" sheet — dimmed parent, centred
card with the busy_toast chrome — because that is the tone people already
know for a calm, temporary notice. There is no auto-installer: the build is
a onefile executable, so the action is opening the release page.
"""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

from core.constants import APP_VERSION
from core.update_check import ReleaseInfo
from i18n import t
from ui.styles.theme import palette


class UpdateAvailableDialog(QDialog):
    """Window-modal notice with changelog and a link to the release page."""

    def __init__(self, info: ReleaseInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self.setWindowTitle(t("update.title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(480, 420)
        self.resize(520, 480)
        self.setStyleSheet(f"QDialog{{background:{palette('bg')};}}")
        self._build()

    def _build(self):
        # Outer sheet reuses the busy-overlay dim; the card inside is the
        # busy_toast surface, grown to hold a changelog.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sheet = _DimSheet(self)
        sheet_lay = QVBoxLayout(sheet)
        sheet_lay.setContentsMargins(28, 28, 28, 28)
        sheet_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame()
        card.setObjectName("busy_toast")
        card.setMaximumWidth(460)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(22, 18, 22, 18)
        card_lay.setSpacing(12)

        title = QLabel(t("update.title"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color:{palette('text')};font-size:15px;font-weight:700;"
            f"background:transparent;border:none;")
        card_lay.addWidget(title)

        summary = QLabel(t("update.available",
                           version=self._info.version,
                           current=APP_VERSION))
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary.setStyleSheet(
            f"color:{palette('text_secondary')};font-size:12px;"
            f"font-weight:500;background:transparent;border:none;")
        card_lay.addWidget(summary)

        note = QLabel(t("update.manual_note"))
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet(
            f"color:{palette('text_muted')};font-size:11px;"
            f"background:transparent;border:none;")
        card_lay.addWidget(note)

        body = (self._info.body or "").strip() or t("update.no_notes")
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(body)
        notes.setMinimumHeight(160)
        notes.setStyleSheet(
            f"QTextEdit{{background:{palette('bg')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:6px;"
            f"padding:8px;font-size:12px;font-weight:400;}}")
        card_lay.addWidget(notes, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        later = QPushButton(t("update.later"))
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        later.setStyleSheet(
            f"QPushButton{{color:{palette('text')};background:{palette('bg_elevated')};"
            f"border:1px solid {palette('border')};border-radius:4px;"
            f"padding:7px 16px;font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{palette('border_hover')};}}")
        later.clicked.connect(self.reject)
        btn_row.addWidget(later)

        open_btn = QPushButton(t("update.open_page"))
        open_btn.setObjectName("primary_btn")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._open_page)
        btn_row.addWidget(open_btn)

        card_lay.addLayout(btn_row)
        sheet_lay.addWidget(card)
        root.addWidget(sheet)

    def _open_page(self):
        webbrowser.open(self._info.html_url)
        self.accept()


class _DimSheet(QWidget):
    """Full-dialog dim fill — same idea as BusyOverlay's translucent sheet."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dim = QColor(palette("bg"))
        self._dim.setAlpha(190)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._dim)
        painter.end()
