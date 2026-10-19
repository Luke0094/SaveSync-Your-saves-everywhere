"""SaveSync - "open this link?" - the question asked before a web address in a
friend's message is handed to the browser.

Nothing about the address has been fetched or checked beyond what
core.p2p.markup.check_url does (a real http(s) address with a domain name, no
user name in it). The dialog shows exactly where it goes - the host on its own
line, in the form the browser will use - and says so when that host is written
with look-alike letters or the connection is not encrypted. Cancel is the
default button; the link is opened only by pressing Open.
"""
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette


class P2pLinkDialog(QDialog):
    def __init__(self, safe, friend_name: str = "", parent=None):
        """*safe* is a core.p2p.markup.SafeUrl."""
        super().__init__(parent)
        self._safe = safe
        self.opened = False
        self.setWindowTitle(t("p2p.link_title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(palette("bg")))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(palette("text")))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}")
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self)
        except Exception:
            pass
        self.setMinimumWidth(scaled(420, self))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        intro = QLabel(t("p2p.link_intro", name=friend_name or "?"))
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(intro)

        host = QLabel(safe.host)
        host.setTextFormat(Qt.TextFormat.PlainText)
        host.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        host.setStyleSheet(f"font-family: Consolas, monospace; font-weight: 700; "
                           f"font-size: {scaled(16, self)}px;")
        lay.addWidget(host)

        notes = []
        if safe.international:
            notes.append(t("p2p.link_international", shown=safe.shown_host))
        if not safe.secure:
            notes.append(t("p2p.link_insecure"))
        for note in notes:
            lbl = QLabel("⚠ " + note)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setStyleSheet(f"color:{palette('warning')};")
            lay.addWidget(lbl)

        full = QPlainTextEdit(safe.url)
        full.setReadOnly(True)
        full.setFixedHeight(scaled(64, self))
        full.setStyleSheet("font-family: Consolas, monospace;")
        lay.addWidget(full)

        hint = QLabel(t("p2p.link_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{palette('text_secondary')};")
        lay.addWidget(hint)

        row = QHBoxLayout()
        copy = QPushButton(t("p2p.link_copy"))
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._safe.url))
        row.addWidget(copy)
        row.addStretch(1)
        self.cancel_btn = QPushButton(t("common.cancel"))
        self.cancel_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self.reject)
        row.addWidget(self.cancel_btn)
        self.open_btn = QPushButton(t("p2p.link_open"))
        self.open_btn.setAutoDefault(False)
        self.open_btn.clicked.connect(self._open)
        row.addWidget(self.open_btn)
        lay.addLayout(row)
        self.cancel_btn.setFocus()

    def _open(self):
        # The checked, ASCII-only address - never the text as it was typed.
        self.opened = bool(QDesktopServices.openUrl(QUrl(self._safe.url, QUrl.ParsingMode.StrictMode)))
        self.accept()
