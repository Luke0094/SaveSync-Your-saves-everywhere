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
from ui.helpers import finalize_adaptive_dialog_size, scaled
from ui.styles.theme import palette


class UpdateAvailableDialog(QDialog):
    """Window-modal notice with changelog and a link to the release page."""

    def __init__(self, info: ReleaseInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self.setWindowTitle(t("update.title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._build()
        from ui.helpers import set_dark_title_bar
        set_dark_title_bar(self)
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=480, min_h=400)

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
        card.setMaximumWidth(scaled(460, self))
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(22, 18, 22, 18)
        card_lay.setSpacing(12)

        title = QLabel(t("update.title"))
        title.setObjectName("update_title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(title)

        summary = QLabel(t("update.available",
                           version=self._info.version,
                           current=APP_VERSION))
        summary.setObjectName("update_summary")
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(summary)

        note = QLabel(t("update.manual_note"))
        note.setObjectName("update_note")
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(note)

        body = (self._info.body or "").strip() or t("update.no_notes")
        notes = QTextEdit()
        notes.setObjectName("update_notes")
        notes.setReadOnly(True)
        notes.setPlainText(body)
        notes.setMinimumHeight(scaled(160, self))
        card_lay.addWidget(notes, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        later = QPushButton(t("update.later"))
        later.setObjectName("update_later_btn")
        later.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def paintEvent(self, event):
        painter = QPainter(self)
        dim = QColor(palette("bg"))
        dim.setAlpha(190)
        painter.fillRect(self.rect(), dim)
        painter.end()
