"""
SaveSync - Credits dialog.

Opened from the sidebar button (above the Online/Offline status). Built
fresh on every open, so it always picks up the CURRENT theme palette and
language — no refresh_styles/update_locale wiring needed.
"""
import webbrowser

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from i18n import t
from core.constants import APP_NAME, APP_VERSION, GITHUB_URL
from ui.helpers import scaled
from ui.styles.theme import palette

_WALLETS = [
    ("Bitcoin",  "3G3MDNUh51g6iK7ZRSQPX4EeBXEb3UyAtw"),
    ("Litecoin", "MEmeHh7A3Cfp9KvcqurviaJXpYL9HXuVJV"),
]

_LOGO_PX = 72


def _logo_pixmap(logical: int = _LOGO_PX) -> QPixmap:
    """Credits mark: same S + border + arrows as the app icon, no dark card.

    Drawn at *physical* pixels for the current DPR so HiDPI stays sharp
    (scaling the 256px PNG to 72 logical looked soft/pixelated on 125–200%).
    Transparent fill so light and dark dialog backgrounds show through.
    """
    from PySide6.QtCore import QRectF

    app = QApplication.instance()
    dpr = float(app.devicePixelRatio() if app is not None else 1.0)
    size = max(1, int(round(logical * dpr)))

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    margin = max(1, size // 32)
    radius = size * 0.18
    # Accent ring only — no filled card (that read as a second rectangle).
    pen = QPen(QColor("#76b900"), max(1, size // 32))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(margin, margin, size - margin * 2, size - margin * 2,
                      radius, radius)

    font = QFont("Segoe UI", int(size * 0.52), QFont.Weight.Bold)
    p.setFont(font)
    p.setPen(QColor("#76b900"))
    p.drawText(QRectF(0, -size * 0.02, size, size),
               int(Qt.AlignmentFlag.AlignCenter), "S")

    if size >= 48:
        # Mid gray reads on both light and dark dialog backgrounds
        # (the app-icon silver was for the dark card fill).
        arrow_pen = QPen(QColor(palette("text_secondary")), max(1, size // 40))
        arrow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arrow_pen)
        cx, cy = size * 0.5, size * 0.78
        span = size * 0.18
        p.drawLine(int(cx - span), int(cy), int(cx + span), int(cy))
        p.drawLine(int(cx + span), int(cy),
                   int(cx + span - size * 0.06), int(cy - size * 0.05))
        cy2 = cy + size * 0.08
        p.drawLine(int(cx + span), int(cy2), int(cx - span), int(cy2))
        p.drawLine(int(cx - span), int(cy2),
                   int(cx - span + size * 0.06), int(cy2 - size * 0.05))

    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


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
        toast.setObjectName("credits_toast")
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
    sep.setObjectName("credits_sep")
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(scaled(1, sep))
    return sep


class CreditsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("credits.title"))
        self.setFixedSize(scaled(440, self), scaled(480, self))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(28, 24, 28, 20)

        # ── Brand + running version ───────────────────────────────────────────
        # Logo from assets/icon.png. Slightly larger label than the pixmap so
        # antialiased edges are not clipped; no fill — a filled QLabel read as
        # a rectangle behind the icon in the light theme.
        logo_px = _logo_pixmap()
        logo = QLabel()
        logo.setObjectName("credits_logo")
        logo.setFixedSize(_LOGO_PX + 4, _LOGO_PX + 4)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if not logo_px.isNull():
            logo.setPixmap(logo_px)
        logo_row = QHBoxLayout()
        logo_row.setContentsMargins(0, 0, 0, 0)
        logo_row.addStretch()
        logo_row.addWidget(logo)
        logo_row.addStretch()
        root.addLayout(logo_row)

        brand = QLabel(APP_NAME)
        brand.setObjectName("credits_heading")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_font = QFont()
        brand_font.setPointSize(16)
        brand_font.setBold(True)
        brand.setFont(brand_font)
        root.addWidget(brand)

        ver = QLabel(t("credits.version", version=APP_VERSION))
        ver.setObjectName("credits_muted")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(ver)

        root.addWidget(_hsep())

        # ── Developer ─────────────────────────────────────────────────────────
        dev_lbl = QLabel(t("credits.developer"))
        dev_lbl.setObjectName("credits_heading")
        dev_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dev_font = QFont()
        dev_font.setPointSize(13)
        dev_font.setBold(True)
        dev_lbl.setFont(dev_font)
        root.addWidget(dev_lbl)

        dev_row = QHBoxLayout()
        dev_row.setSpacing(10)
        dev_row.addStretch()

        name_lbl = QLabel("Luke0094")  # i18n-ignore: a person's handle, never translated
        name_lbl.setObjectName("credits_muted")
        name_font = QFont()
        name_font.setPointSize(11)
        name_lbl.setFont(name_font)
        dev_row.addWidget(name_lbl)

        github_btn = QPushButton("GitHub")
        github_btn.setObjectName("credits_github_btn")
        github_btn.setFixedWidth(scaled(100, self))
        github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        github_btn.setToolTip(t("credits.github_tooltip"))
        # Colours live in the theme (#credits_github_btn) so light/dark hover
        # both resolve correctly — an inline sheet baked at open time could
        # keep the dark-theme hover after a theme switch.
        github_btn.clicked.connect(lambda: webbrowser.open(GITHUB_URL))
        dev_row.addWidget(github_btn)
        dev_row.addStretch()
        root.addLayout(dev_row)

        root.addWidget(_hsep())

        # ── Donations ─────────────────────────────────────────────────────────
        don_lbl = QLabel(t("credits.donations"))
        don_lbl.setObjectName("credits_heading")
        don_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        don_font = QFont()
        don_font.setPointSize(12)
        don_font.setBold(True)
        don_lbl.setFont(don_font)
        root.addWidget(don_lbl)

        for coin, address in _WALLETS:
            root.addLayout(self._wallet_row(coin, address))

        root.addStretch()
        root.addWidget(_hsep())

        close_btn = QPushButton(t("common.close"))
        close_btn.setObjectName("primary_btn")
        close_btn.setFixedWidth(scaled(120, self))
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
        lbl.setObjectName("credits_coin")
        lbl.setFixedWidth(scaled(64, lbl))
        lbl_font = QFont()
        lbl_font.setBold(True)
        lbl.setFont(lbl_font)
        row.addWidget(lbl)
        entry = _CopyOnClickField(address)
        entry.setObjectName("credits_wallet_field")
        entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        entry.setCursorPosition(0)
        row.addWidget(entry)
        return row
