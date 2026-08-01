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
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from i18n import t
from ui.styles.theme import palette

logger = logging.getLogger(__name__)


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
        self._label = QLabel(text or t("common.please_wait"))
        self._label.setObjectName("busy_toast")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._label)

        parent.installEventFilter(self)
        self._fit()

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
        yield
    finally:
        if overlay is not None:
            try:
                overlay.close_overlay()
            except RuntimeError:
                pass
