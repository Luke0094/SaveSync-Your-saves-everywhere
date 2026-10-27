"""SaveSync - a one-line label whose text that does not fit moves back and forth while hovered.

At rest it shows as much as fits, ended with an ellipsis; ``set_hover(True)`` (the card it sits on is
hovered) makes it slide to the end of the text and back, pausing at each end, and ``set_hover(False)``
puts it back. Text that fits never moves. The animation is time-based (the position is worked out from
the clock on each tick, so a slow frame does not slow it down) and runs only while hovered and only if
the text overflows.
"""
import time

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QSizePolicy, QStyle, QStyleOption, QWidget

SPEED_PX_PER_S = 38.0
PAUSE_S = 0.8
TICK_MS = 33


class MarqueeLabel(QWidget):
    def __init__(self, pad: int = 4, parent=None, flexible: bool = False):
        """The font, colour and background come from the style sheet (by object name): the size of the
        text is only known once that has been applied, so it is applied (``ensurePolished``) before
        the text is measured. *flexible*: a label that takes whatever width a layout gives it (a name),
        as wide as its text when there is room and squeezed - and then moving on hover - when not;
        otherwise it is sized by ``fit`` (a chip)."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._flexible = bool(flexible)
        if self._flexible:
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._text = ""
        self._pad = max(0, int(pad))
        self._hover = False
        self._offset = 0.0
        self._t0 = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ── what it shows ─────────────────────────────────────────────────────

    def text(self) -> str:
        return self._text

    def set_text(self, text: str):
        self._text = str(text or "")
        self._stop()
        self.updateGeometry()
        self.update()

    setText = set_text                  # (what a label's callers say)

    def _line_height(self) -> int:
        self.ensurePolished()
        return self.fontMetrics().height() + 2

    def sizeHint(self) -> QSize:
        if not self._flexible:
            return super().sizeHint()
        self.ensurePolished()
        return QSize(self.text_width() + 2 * self._pad + 2, self._line_height())

    def minimumSizeHint(self) -> QSize:
        if not self._flexible:
            return super().minimumSizeHint()
        return QSize(1, self._line_height())

    def text_width(self) -> int:
        return self.fontMetrics().horizontalAdvance(self._text)

    def fit(self, text: str, max_width: int) -> int:
        """Show *text* in a chip as wide as it needs but no wider than *max_width*; returns the width.
        Shown only when there is text."""
        self.set_text(text)
        if not self._text:
            self.setVisible(False)
            return 0
        self.ensurePolished()
        width = min(self.text_width() + 2 * self._pad + 2, max(2 * self._pad + 8, int(max_width)))
        self.setFixedSize(width, self.fontMetrics().height() + 2)
        self.setVisible(True)
        return width

    def overflows(self) -> bool:
        return self.text_width() > self._avail()

    def _avail(self) -> int:
        return max(1, self.width() - 2 * self._pad)

    # ── moving ────────────────────────────────────────────────────────────

    def set_hover(self, on: bool):
        on = bool(on)
        if on == self._hover:
            return
        self._hover = on
        if on and self._text and self.overflows():
            self._t0 = time.monotonic()
            self._timer.start()
        else:
            self._stop()
            self.update()

    def _stop(self):
        self._timer.stop()
        self._offset = 0.0

    def moving(self) -> bool:
        return self._timer.isActive()

    def offset_at(self, elapsed: float) -> float:
        """How far the text has slid *elapsed* seconds after the start: held at the start, slid to
        the end, held there, slid back - round and round."""
        dist = float(self.text_width() - self._avail())
        if dist <= 0:
            return 0.0
        move = dist / SPEED_PX_PER_S
        cycle = 2 * (PAUSE_S + move)
        u = elapsed % cycle
        if u < PAUSE_S:
            return 0.0
        if u < PAUSE_S + move:
            return dist * (u - PAUSE_S) / move
        if u < 2 * PAUSE_S + move:
            return dist
        return dist * (1.0 - (u - 2 * PAUSE_S - move) / move)

    def _tick(self):
        if not self.overflows():
            self._stop()
            self.update()
            return
        new = self.offset_at(time.monotonic() - self._t0)
        if int(new) != int(self._offset):
            self._offset = new
            self.update()
        else:
            self._offset = new

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)
        area = QRect(self._pad, 0, self._avail(), self.height())
        painter.setClipRect(area)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        painter.setFont(self.font())
        fm = painter.fontMetrics()
        if self._timer.isActive():
            full = QRect(area.left() - int(self._offset), 0, fm.horizontalAdvance(self._text) + 4, self.height())
            painter.drawText(full, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._text)
        else:
            shown = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, area.width())
            painter.drawText(area, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), shown)
