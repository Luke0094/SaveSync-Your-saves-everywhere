"""SaveSync - the page a transfer shows, the same one on the sending and the receiving side.

From the top: the overall bar (the one the sidebar shows) with the size moved and the speed
under it, then one row per file - a tick, its name, its size, a bar with its percentage, its
speed. Pieces of a torrent do not arrive in order, so a percentage alone hides *which* part
of a file is in; resting the pointer on a file's bar opens a strip of its parts, start to end
(``PartsPopup``), the way a torrent client does.

It only draws what it is given (``set_rows``, ``set_global``) and says when a tick was
changed (``ticked``); what a tick means is for the window that owns it.
"""
from typing import NamedTuple, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHeaderView, QLabel, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette
from ui.widgets.transfer_bar import transfer_bar

COL_NAME, COL_SIZE, COL_BAR, COL_SPEED = range(4)
_ROLE_PCT = Qt.ItemDataRole.UserRole
_ROLE_STATE = Qt.ItemDataRole.UserRole + 1
_ROLE_PARTS = Qt.ItemDataRole.UserRole + 2

ACTIVE, LEFT_OUT, WHOLE = "active", "left_out", "whole"


class FileRow(NamedTuple):
    """One file of the page."""
    name: str
    size: int
    done: int = 0               # bytes of it that are in
    speed: float = 0.0          # bytes/s, 0 when idle
    state: str = ACTIVE         # ACTIVE, LEFT_OUT (not being taken), WHOLE
    tickable: bool = True       # may the person change its tick
    parts: Optional[bytes] = None   # 0-255 per slice of the file: how much of it is in


class _BarDelegate(QStyledItemDelegate):
    """A file's bar: a track, a fill up to its percentage, the percentage on it."""

    def paint(self, painter, option, index):
        if index.column() != COL_BAR:
            return super().paint(painter, option, index)
        pct = int(index.data(_ROLE_PCT) or 0)
        state = index.data(_ROLE_STATE) or ACTIVE
        rect = QRectF(option.rect.adjusted(4, 4, -4, -4))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette("bg_hover")))
        painter.drawRoundedRect(rect, 4, 4)
        if state != LEFT_OUT and pct > 0:
            fill = QColor(palette("accent"))
            fill.setAlpha(150)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(), max(rect.width() * pct / 100.0, 8.0), rect.height()), 4, 4)
        painter.setPen(QColor(palette("text_secondary" if state == LEFT_OUT else "text")))
        font = painter.font()
        font.setPixelSize(max(9, int(rect.height() * 0.72)))
        font.setWeight(font.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        painter.restore()


class PartsPopup(QFrame):
    """The strip that opens over a file's bar: each slice of the file, filled as far as it
    is in, start on the left. Frameless and click-through, like a tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._name = ""
        self._pct = 0
        self._parts = b""
        self.setFixedSize(scaled(380, self), scaled(76, self))

    def show_for(self, name: str, pct: int, parts: bytes, at: QPoint) -> None:
        self._name, self._pct, self._parts = name, pct, parts
        self.move(at)
        self.update()
        if not self.isVisible():
            self.show()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QColor(palette("border")))
        p.setBrush(QColor(palette("bg_elevated")))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)
        pad = scaled(10, self)
        font = p.font()
        font.setPixelSize(scaled(12, self))
        p.setFont(font)
        p.setPen(QColor(palette("text")))
        fm = p.fontMetrics()
        title = fm.elidedText(f"{self._name}  -  {self._pct}%", Qt.TextElideMode.ElideMiddle, w - 2 * pad)
        p.drawText(pad, pad + fm.ascent(), title)
        strip = QRectF(pad, pad + fm.height() + scaled(6, self), w - 2 * pad, scaled(16, self))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(palette("bg_hover")))
        p.drawRoundedRect(strip, 3, 3)
        n = len(self._parts)
        if n:
            accent = QColor(palette("accent"))
            cell = strip.width() / n
            for i, value in enumerate(self._parts):
                if value <= 0:
                    continue
                c = QColor(accent)
                c.setAlpha(40 + (215 * value) // 255)
                p.setBrush(c)
                p.drawRect(QRectF(strip.x() + i * cell, strip.y(), cell + 0.6, strip.height()))
        font.setPixelSize(scaled(10, self))
        p.setFont(font)
        p.setPen(QColor(palette("text_secondary")))
        p.drawText(QRectF(pad, strip.bottom() + scaled(3, self), w - 2 * pad, scaled(14, self)),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, t("p2p.parts_hint"))


class TransferFilesView(QWidget):
    ticked = Signal(str, bool)      # a file's name, whether it is now ticked

    def __init__(self, parent=None, bar_height: int = 20):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.bar = transfer_bar(bar_height)
        lay.addWidget(self.bar)
        self.sizes_lbl = QLabel("")
        lay.addWidget(self.sizes_lbl)
        self.speed_lbl = QLabel("")
        self.speed_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        lay.addWidget(self.speed_lbl)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([t("p2p.col_file"), t("p2p.col_size"), t("p2p.col_done"), t("p2p.col_speed")])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setMinimumHeight(scaled(110, self))
        self.tree.setMaximumHeight(scaled(260, self))
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_BAR, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_SPEED, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(COL_BAR, scaled(150, self))
        self.tree.setItemDelegateForColumn(COL_BAR, _BarDelegate(self.tree))
        self.tree.setMouseTracking(True)
        self.tree.viewport().installEventFilter(self)
        for col in (COL_SIZE, COL_BAR, COL_SPEED):
            self.tree.headerItem().setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setVisible(False)
        lay.addWidget(self.tree)

        self._names: list = []
        self._busy = False
        self._popup = PartsPopup(self)

    # ── what it shows ────────────────────────────────────────────────────

    def set_global(self, pct: int, sizes: str = "", speed: str = "", tip: str = "") -> None:
        self.bar.setValue(int(pct))
        self.sizes_lbl.setText(sizes)
        self.speed_lbl.setText(speed)
        for w in (self.bar, self.sizes_lbl, self.speed_lbl):
            w.setToolTip(tip)

    def set_rows(self, rows, tickable: bool = True, show: bool = True, name_tip: str = "") -> None:
        """Draw *rows* (``FileRow``). A list of one file is not shown: the overall bar says it
        all. *tickable* False takes the ticks away from the person's reach altogether (a
        view that only reports); *name_tip* is added to each name's tooltip."""
        from core import fmt_size
        from core.p2p.transfer import format_rate
        rows = list(rows)
        self.tree.setVisible(show and len(rows) > 1)
        if len(rows) <= 1:
            self._names = []
            self.tree.clear()
            return
        names = [r.name for r in rows]
        self._busy = True
        try:
            if names != self._names:
                self.tree.clear()
                for r in rows:
                    item = QTreeWidgetItem([r.name, "", "", ""])
                    item.setToolTip(COL_NAME, f"{r.name}\n{name_tip}" if name_tip else r.name)
                    for col in (COL_SIZE, COL_SPEED):
                        item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setCheckState(COL_NAME, Qt.CheckState.Checked)
                    self.tree.addTopLevelItem(item)
                self._names = names
            for i, r in enumerate(rows):
                item = self.tree.topLevelItem(i)
                if item is None:
                    continue
                pct = 100 if r.size <= 0 else min(100, int(r.done * 100 / r.size))
                if r.state == WHOLE:
                    pct = 100
                state = Qt.CheckState.Unchecked if r.state == LEFT_OUT else Qt.CheckState.Checked
                if item.checkState(COL_NAME) != state:
                    item.setCheckState(COL_NAME, state)
                flags = item.flags()
                can = tickable and r.tickable
                want = (flags | Qt.ItemFlag.ItemIsUserCheckable) if can else (flags & ~Qt.ItemFlag.ItemIsUserCheckable)
                if flags != want:
                    item.setFlags(want)
                bar_txt = t("p2p.row_left_out") if r.state == LEFT_OUT else f"{pct}%"
                if item.text(COL_BAR) != bar_txt:
                    item.setText(COL_BAR, bar_txt)
                item.setData(COL_BAR, _ROLE_PCT, pct)
                item.setData(COL_BAR, _ROLE_STATE, r.state)
                item.setData(COL_BAR, _ROLE_PARTS, r.parts)
                size_txt = fmt_size(r.size)
                if item.text(COL_SIZE) != size_txt:
                    item.setText(COL_SIZE, size_txt)
                if r.speed >= 5120 and pct < 100 and r.state != LEFT_OUT:    # under 5 KB/s is idle
                    mbs, mbps = format_rate(r.speed)
                    spd, tip = mbs, mbps
                else:
                    spd, tip = "—", ""
                if item.text(COL_SPEED) != spd:
                    item.setText(COL_SPEED, spd)
                    item.setToolTip(COL_SPEED, tip)
                if tip:
                    item.setToolTip(COL_BAR, tip)
                else:
                    item.setToolTip(COL_BAR, "")
        finally:
            self._busy = False

    def set_checked(self, name: str, checked: bool) -> None:
        """Put a tick back to what is true (a change that was refused)."""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.text(COL_NAME) == name:
                self._busy = True
                try:
                    item.setCheckState(COL_NAME, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                finally:
                    self._busy = False
                return

    def _on_item_changed(self, item, column) -> None:
        if self._busy or column != COL_NAME:
            return
        self.ticked.emit(item.text(COL_NAME), item.checkState(COL_NAME) == Qt.CheckState.Checked)

    # ── the parts strip, on resting the pointer on a bar ─────────────────

    def eventFilter(self, obj: QObject, event) -> bool:
        if obj is self.tree.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                self._hover(event.position().toPoint())
            elif kind in (QEvent.Type.Leave, QEvent.Type.Wheel):
                self._popup.hide()
        return super().eventFilter(obj, event)

    def parts_at(self, pos: QPoint):
        """``(name, pct, parts)`` of the file whose bar is under *pos* (viewport coordinates),
        or None when the pointer is elsewhere or that file has no map."""
        item = self.tree.itemAt(pos)
        if item is None or self.tree.columnAt(pos.x()) != COL_BAR:
            return None
        parts = item.data(COL_BAR, _ROLE_PARTS)
        if not parts or item.data(COL_BAR, _ROLE_STATE) == LEFT_OUT:
            return None
        return item.text(COL_NAME), int(item.data(COL_BAR, _ROLE_PCT) or 0), bytes(parts)

    def _hover(self, pos: QPoint) -> None:
        hit = self.parts_at(pos)
        if hit is None:
            self._popup.hide()
            return
        name, pct, parts = hit
        at = self.tree.viewport().mapToGlobal(pos) + QPoint(14, 18)
        self._popup.show_for(name, pct, parts, at)

    def hideEvent(self, event):
        self._popup.hide()
        super().hideEvent(event)
