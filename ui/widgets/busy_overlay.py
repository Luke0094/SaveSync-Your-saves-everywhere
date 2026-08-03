"""SaveSync - "Please wait" overlay for work that blocks the GUI thread.

Some operations cannot be moved off the main thread — swapping the
application stylesheet re-resolves style rules for every live widget, and
building a page of game cards decodes their images — so for a moment the
window stops responding. A native wait cursor is easy to miss and says
nothing; this dims the widget under it and states plainly that the app is
working.

Usage::

    with busy_over(self):
        ...the blocking work...

The overlay is painted SYNCHRONOUSLY before the work starts (repaint(), not
update()): the event loop is about to be blocked, so a queued paint would
never be delivered and the overlay would flash in and out without ever being
seen. When the target isn't visible there is nothing to cover, and the
context manager simply runs the work.
"""
import logging
from contextlib import contextmanager

from PySide6.QtCore import Qt, QEvent, QEventLoop
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from i18n import t
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

# How long a piece of work may run before it is worth offering to stop it.
# Short enough that nobody sits through a minute wondering, long enough that
# the button never appears for work that was always going to be quick.
_OFFER_CANCEL_AFTER_S = 30


class BusyOverlay(QWidget):
    """A translucent sheet over its parent with a centred message."""

    def __init__(self, parent: QWidget, text: str = ""):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.CursorShape.BusyCursor)
        self._dim = QColor(palette("bg"))
        self._dim.setAlpha(190)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)
        self._base_text = text or t("common.please_wait")
        self._label = QLabel(self._base_text)
        self._label.setObjectName("busy_toast")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)

        # Only ever shown once the work has gone on long enough to be worth
        # calling off — see tick(). Built here so appearing costs nothing.
        self._cancel_btn = QPushButton(t("common.cancel_search"))
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self._on_cancel)
        lay.addWidget(self._cancel_btn, 0, Qt.AlignmentFlag.AlignCenter)

        self._cancelled = False
        self._shown_seconds = -1

        parent.installEventFilter(self)
        self._fit()

    def _on_cancel(self):
        self._cancelled = True
        self._cancel_btn.setEnabled(False)
        self._label.setText(t("common.cancelling"))
        self.repaint()

    def tick(self, elapsed: float) -> bool:
        """Show how long this has been going, and whether to carry on.

        Called from inside work that is holding the GUI thread, so the event
        loop is pumped here — otherwise the seconds would never be painted
        and the button below could never be pressed. Returns False once the
        person waiting has called it off.
        """
        if self._cancelled:
            return False
        seconds = int(elapsed)
        if seconds != self._shown_seconds:
            self._shown_seconds = seconds
            self._label.setText(f"{self._base_text}  ({seconds}s)")
            if seconds >= _OFFER_CANCEL_AFTER_S and not self._cancel_btn.isVisible():
                self._cancel_btn.show()
            self._label.repaint()
        # User input is allowed through only once there is a button to press.
        # Before that the overlay is deliberately deaf, so a stray click
        # cannot re-enter whatever is being covered.
        flags = (QEventLoop.ProcessEventsFlag.AllEvents
                 if self._cancel_btn.isVisible()
                 else QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        QApplication.processEvents(flags)
        return not self._cancelled

    def _fit(self):
        p = self.parentWidget()
        if p is not None:
            self.setGeometry(p.rect())

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._fit()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._dim)
        painter.end()

    def close_overlay(self):
        p = self.parentWidget()
        if p is not None:
            p.removeEventFilter(self)
        self.hide()
        self.deleteLater()


@contextmanager
def busy_over(widget: QWidget, text: str = ""):
    """Dim *widget* with a "please wait" sheet for the duration of the block.

    Yields the overlay, or None when there was nothing to cover. Work that
    can run long should call ``overlay.tick(seconds)`` as it goes: that is
    what paints the count and, past half a minute, offers to stop.

    Safe to use anywhere: with no visible widget to cover (startup, headless)
    it just runs the body.
    """
    overlay = None
    try:
        if widget is not None and widget.isVisible():
            overlay = BusyOverlay(widget, text)
            overlay.show()
            overlay.raise_()
            # Paint it NOW. The caller is about to block the event loop, so a
            # queued repaint would arrive only after the work is already done.
            # User input stays excluded: a click landing mid-operation could
            # re-enter whatever we are covering.
            overlay.repaint()
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    except Exception as e:                      # never let chrome break the work
        logger.debug(f"Busy overlay could not be shown: {e}")
        overlay = None
    try:
        yield overlay
    finally:
        if overlay is not None:
            try:
                overlay.close_overlay()
            except RuntimeError:
                pass
