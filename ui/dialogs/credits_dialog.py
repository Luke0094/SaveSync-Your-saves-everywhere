"""
SaveSync - Credits dialog.

Opened from the sidebar button (above the Online/Offline status). Built
fresh on every open, so it always picks up the CURRENT theme palette and
language — no refresh_styles/update_locale wiring needed.
"""
import webbrowser

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from i18n import t
from ui.styles.theme import palette

_GITHUB_URL = "https://github.com/Luke0094/SaveSync"

_WALLETS = [
    ("Bitcoin",  "3G3MDNUh51g6iK7ZRSQPX4EeBXEb3UyAtw"),
    ("Litecoin", "MEmeHh7A3Cfp9KvcqurviaJXpYL9HXuVJV"),
]


class _CopyOnClickField(QLineEdit):
    """Read-only wallet field: any left click copies the WHOLE address to
    the clipboard and confirms with a "Copied!" toast held for 1.5 s.

    Mouse events are swallowed instead of forwarded: the default QLineEdit
    handling would move the caret and let a drag grow a PARTIAL selection
    (from which Ctrl+C copies half an address). Here the full text is
    always selected and always what lands on the clipboard. The toast is a
    self-managed ToolTip-flagged label, NOT QToolTip — that one is torn
    down by the very next mouse release/move, so it only lived while the
    button was held."""

    _TOAST_MS = 1500

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t("credits.copy_hint"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._toast: QLabel | None = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            QApplication.clipboard().setText(self.text())
            self.selectAll()   # visual echo: the WHOLE address was copied
            self._show_toast()
        # Deliberately no super(): no caret moves, no drag selection.

    def mouseMoveEvent(self, event):
        pass                   # no drag-selection — all or nothing

    def mouseReleaseEvent(self, event):
        pass

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def _show_toast(self):
        if self._toast is not None:
            self._toast.deleteLater()
        toast = QLabel(t("credits.copied"), self,
                       Qt.WindowType.ToolTip
                       | Qt.WindowType.FramelessWindowHint)
        toast.setStyleSheet(
            f"QLabel{{color:{palette('accent_text')};background:{palette('accent')};"
            f"border:none;border-radius:4px;padding:4px 10px;"
            f"font-size:11px;font-weight:600;}}"
        )
        toast.adjustSize()
        anchor = self.mapToGlobal(QPoint(self.width() // 2, 0))
        toast.move(anchor.x() - toast.width() // 2,
                   anchor.y() - toast.height() - 6)
        toast.show()
        self._toast = toast

        def _dismiss(w=toast):
            if self._toast is w:
                self._toast = None
            w.deleteLater()
        QTimer.singleShot(self._TOAST_MS, _dismiss)


def _hsep() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"background:{palette('border')};border:none;")
    return sep


class CreditsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("credits.title"))
        self.setFixedSize(440, 380)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(28, 24, 28, 20)

        # ── Developer ─────────────────────────────────────────────────────────
        dev_lbl = QLabel(t("credits.developer"))
        dev_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dev_font = QFont()
        dev_font.setPointSize(13)
        dev_font.setBold(True)
        dev_lbl.setFont(dev_font)
        dev_lbl.setStyleSheet(f"color:{palette('text')};")
        root.addWidget(dev_lbl)

        dev_row = QHBoxLayout()
        dev_row.setSpacing(10)
        dev_row.addStretch()

        name_lbl = QLabel("Luke0094")
        name_font = QFont()
        name_font.setPointSize(11)
        name_lbl.setFont(name_font)
        name_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        dev_row.addWidget(name_lbl)

        github_btn = QPushButton("GitHub")
        github_btn.setFixedWidth(100)
        github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        github_btn.setToolTip(t("credits.github_tooltip"))
        github_btn.setStyleSheet(
            f"QPushButton{{color:{palette('text')};background:{palette('bg_elevated')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;"
            f"padding:4px 10px;font-size:11px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{palette('accent')};color:{palette('accent')};}}"
        )
        github_btn.clicked.connect(lambda: webbrowser.open(_GITHUB_URL))
        dev_row.addWidget(github_btn)
        dev_row.addStretch()
        root.addLayout(dev_row)

        root.addWidget(_hsep())

        # ── Donations ─────────────────────────────────────────────────────────
        don_lbl = QLabel(t("credits.donations"))
        don_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        don_font = QFont()
        don_font.setPointSize(12)
        don_font.setBold(True)
        don_lbl.setFont(don_font)
        don_lbl.setStyleSheet(f"color:{palette('text')};")
        root.addWidget(don_lbl)

        for coin, address in _WALLETS:
            root.addLayout(self._wallet_row(coin, address))

        root.addStretch()
        root.addWidget(_hsep())

        close_btn = QPushButton(t("common.close"))
        close_btn.setObjectName("primary_btn")
        close_btn.setFixedWidth(120)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

    @staticmethod
    def _wallet_row(coin: str, address: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(f"{coin}:")
        lbl.setFixedWidth(64)
        lbl_font = QFont()
        lbl_font.setBold(True)
        lbl.setFont(lbl_font)
        lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:11px;")
        row.addWidget(lbl)
        entry = _CopyOnClickField(address)
        entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        entry.setCursorPosition(0)
        entry.setStyleSheet(
            f"QLineEdit{{color:{palette('text_secondary')};background:{palette('bg_input')};"
            f"border:1px solid {palette('border')};border-radius:4px;"
            f"padding:4px 6px;font-size:10px;}}"
        )
        row.addWidget(entry)
        return row
