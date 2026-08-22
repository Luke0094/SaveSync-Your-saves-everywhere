"""SaveSync - "Please wait" overlay for work that blocks the GUI thread.

Some operations cannot be moved off the main thread — swapping the
application stylesheet re-resolves style rules for every live widget, and
building a page of game cards decodes their images — so a moment the
window stops responding. A native wait cursor is easy to miss and says
nothing; this dims the widget under it and states plainly that the app is
working.

Usage::

    with busy_over(self):
        ...the blocking work...

By default the sheet appears only after a short delay (``delay_ms``, ~100 ms).
Faster work finishes unseen. If it crosses that line it usually already runs
longer — reveal and dismiss with the work, with no extra hold afterwards.
Pass ``delay_ms=0`` when the pause is known to be long and the event loop
will not run (theme / language swap).

When the overlay does show, it is painted SYNCHRONOUSLY (repaint(), not
update()): the event loop is about to be blocked, so a queued paint would
never be delivered. Work that pumps the loop (``tick`` / ``processEvents``)
lets a pending delayed reveal fire mid-operation.

When the target isn't visible there is nothing to cover, and the context
manager simply runs the work.
"""
import logging
import time
from contextlib import contextmanager

from PySide6.QtCore import Qt, QEvent, QEventLoop, QTimer
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

# Default: only show the dim sheet once work has outlasted a blink (~100 ms
# still feels instantaneous). Past that, a stall usually runs to a few hundred
# ms anyway — reveal then, and do NOT hold the sheet after the work ends
# (that would be a delay we invent ourselves).
_SHOW_AFTER_MS = 100


class BusyOverlay(QWidget):
    """A translucent sheet over its parent with a centred message."""

    def __init__(self, parent: QWidget, text: str = "",
                 shelvable: bool = False):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._dim = QColor(palette("bg"))
        self._dim.setAlpha(190)

        # Clean up any dragged card proxy
        try:
            from ui.widgets.library_drag import cancel_active_drag
            cancel_active_drag()
        except Exception:
            pass

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

        # "Minimizza": lets the user put long work (save decrypt, batch
        # scans) away instead of staring at the sheet. The work continues —
        # the overlay's tick() keeps pumping, now with user input allowed —
        # and a sidebar notice the caller provides takes over the reporting.
        self._shelvable = bool(shelvable)
        self._shelved = False
        self._shelve_btn = QPushButton(t("common.minimize"))
        self._shelve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._shelve_btn.hide()
        self._shelve_btn.clicked.connect(self._on_shelve)
        lay.addWidget(self._shelve_btn, 0, Qt.AlignmentFlag.AlignCenter)
        # Caller hooks for shelved mode: on_shelve() fires the moment the
        # sheet is put away; sidebar_tick(elapsed) updates the sidebar notice
        # on every progress call while shelved.
        self.on_shelve = None
        self.sidebar_tick = None

        self._cancelled = False
        self._shown_seconds = -1
        self._revealed = False
        self._start_time = time.monotonic()
        # Seconds of elapsed work before reveal() is allowed via tick().
        # busy_over sets this from delay_ms.
        self._reveal_after_s = _SHOW_AFTER_MS / 1000.0
        # Internal countdown: drives the seconds text and the cancel button
        # even for work that never calls tick() itself, as long as the event
        # loop gets pumped (chunked builds, any processEvents). Idempotent
        # with tick()'s own update — same text, same threshold.
        self._countdown = QTimer(self)
        self._countdown.setInterval(200)
        self._countdown.timeout.connect(self._countdown_tick)

        parent.installEventFilter(self)
        self._fit()
        # Stay hidden until reveal() — avoids flashing on sub-delay work.
        self.hide()

    def unshelve(self):
        """Bring the sheet back from shelved mode."""
        self._shelved = False
        self._revealed = False
        self.reveal()

    def reveal(self):
        """Show the sheet now (idempotent when already visible)."""
        if self._revealed and self.isVisible():
            return
        try:
            if self.parentWidget() is None:
                return
        except RuntimeError:
            return
        if not hasattr(self, "_start_time") or self._start_time is None:
            self._start_time = time.monotonic()
        self._revealed = True
        self._shelved = False
        self.setCursor(Qt.CursorShape.BusyCursor)
        self._fit()
        self.show()
        self.raise_()
        if self._shelvable:
            self._shelve_btn.show()
        elapsed = max(0, int(time.monotonic() - self._start_time))
        self._update_countdown(elapsed)
        self.repaint()
        flags = (QEventLoop.ProcessEventsFlag.AllEvents
                 if (self._cancel_btn.isVisible() or self._shelve_btn.isVisible())
                 else QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        QApplication.processEvents(flags)
        if not self._countdown.isActive():
            self._countdown.start()

    def _countdown_tick(self):
        if self._cancelled:
            self._countdown.stop()
            return
        elapsed = max(0, int(time.monotonic() - getattr(self, "_start_time", time.monotonic())))
        self._update_countdown(elapsed)

    def _update_countdown(self, seconds: int):
        if self._cancelled:
            return
        if seconds == self._shown_seconds and self._label.text():
            return
        self._shown_seconds = seconds
        self._label.setText(f"{self._base_text}  ({max(0, seconds)}s)")
        if seconds >= _OFFER_CANCEL_AFTER_S and not self._cancel_btn.isVisible():
            self._cancel_btn.show()
            self._cancel_btn.setEnabled(True)
        if self._revealed and self.isVisible():
            self._label.repaint()



    def _on_shelve(self):
        self._shelved = True
        self._revealed = False
        self._countdown.stop()
        self.hide()
        if self.on_shelve is not None:
            try:
                self.on_shelve()
            except Exception:
                logger.debug("shelve hook failed", exc_info=True)

    def _on_cancel(self):
        self._cancelled = True
        self._countdown.stop()
        self._cancel_btn.setEnabled(False)
        self._label.setText(t("common.cancelling"))
        if self._revealed:
            self.repaint()
        if getattr(self, "on_cancel", None) is not None:
            try:
                self.on_cancel()
            except Exception:
                pass
        QTimer.singleShot(200, self.close_overlay)


    def tick(self, elapsed: float) -> bool:
        """Show how long this has been going, and whether to carry on.

        Called from inside work that is holding the GUI thread, so the event
        loop is pumped here — otherwise the seconds would never be painted
        and the button below could never be pressed. Returns False once the
        person waiting has called it off.

        Also reveals a delayed overlay once *elapsed* passes the threshold.
        """
        if self._cancelled:
            return False
        if self._shelved:
            # Put away: the sidebar notice (caller's sidebar_tick) reports
            # progress; the loop keeps pumping, WITH user input allowed so
            # the rest of the app is usable while the work continues.
            if self.sidebar_tick is not None:
                try:
                    self.sidebar_tick(elapsed)
                except Exception:
                    logger.debug("sidebar tick failed", exc_info=True)
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents)
            return not self._cancelled
        if not self._revealed or not self.isVisible():
            if elapsed >= self._reveal_after_s:
                self.reveal()
        if not self._revealed or not self.isVisible():
            # Still in the grace period — pump so a QTimer reveal can fire.
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            return not self._cancelled
        self._update_countdown(max(0, int(elapsed)))
        # User input is allowed through only once there is a button to press.
        # Before that the overlay is deliberately deaf, so a stray click
        # cannot re-enter whatever is being covered.
        flags = (QEventLoop.ProcessEventsFlag.AllEvents
                 if (self._cancel_btn.isVisible() or self._shelve_btn.isVisible())
                 else QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        QApplication.processEvents(flags)
        return not self._cancelled

    def pump(self):
        """Update elapsed timer and pump GUI events so the counter advances during heavy work."""
        if self._cancelled:
            return
        if not self._revealed or not self.isVisible():
            elapsed = time.monotonic() - getattr(self, "_start_time", time.monotonic())
            if elapsed >= self._reveal_after_s:
                self.reveal()
        if not self._revealed or not self.isVisible():
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            return
        elapsed = max(0, int(time.monotonic() - getattr(self, "_start_time", time.monotonic())))
        self._update_countdown(elapsed)
        flags = (QEventLoop.ProcessEventsFlag.AllEvents
                 if (self._cancel_btn.isVisible() or self._shelve_btn.isVisible())
                 else QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        QApplication.processEvents(flags)

    def set_base_text(self, text: str):
        """Update base label text (e.g. upon language change)."""
        self._base_text = text or t("common.please_wait")
        self._shown_seconds = -1  # force text update
        elapsed = max(0, int(time.monotonic() - getattr(self, "_start_time", time.monotonic())))
        self._update_countdown(elapsed)

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
        self._countdown.stop()
        p = self.parentWidget()
        if p is not None:
            p.removeEventFilter(self)
        self.hide()
        self.deleteLater()


@contextmanager
def busy_over(widget: QWidget, text: str = "", delay_ms: int = _SHOW_AFTER_MS,
              shelvable: bool = False):
    """Dim *widget* with a "please wait" sheet for the duration of the block.

    Yields the overlay, or None when there was nothing to cover. Work that
    can run long should call ``overlay.tick(seconds)`` as it goes: that is
    what paints the count and, past half a minute, offers to stop. Even
    without ticks, the sheet's own countdown advances whenever the event
    loop gets pumped, so chunked work shows the same seconds + cancel.

    *delay_ms*: wait this long before showing the sheet (default ~100 ms).
    Work that finishes sooner never flashes the overlay. Crossing that line
    usually means a longer stall already — show then, and dismiss as soon as
    the work ends (no artificial hold). Use ``0`` when the pause is known to
    block the event loop for a long time with no ticks (theme / language).

    *shelvable*: when True the sheet carries a "to sidebar" button. Pressing
    it hides the sheet while the work continues; set ``overlay.on_shelve``
    (show a sidebar notice) and ``overlay.sidebar_tick`` (update it) to
    follow the work from the sidebar. Tick-driven work keeps pumping with
    user input allowed once shelved.

    Safe to use anywhere: with no visible widget to cover (startup, headless)
    it just runs the body.
    """
    overlay = None
    timer = None
    try:
        if widget is not None and widget.isVisible():
            overlay = BusyOverlay(widget, text, shelvable=shelvable)
            overlay._reveal_after_s = max(0, int(delay_ms)) / 1000.0
            if delay_ms <= 0:
                overlay.reveal()
            else:
                # Fires when something pumps the event loop (chunked page
                # builds, tick(), …). If work ends first, finally stops it
                # and the sheet is never seen.
                timer = QTimer(overlay)
                timer.setSingleShot(True)
                timer.timeout.connect(overlay.reveal)
                timer.start(max(1, int(delay_ms)))
    except Exception as e:                      # never let chrome break the work
        logger.debug(f"Busy overlay could not be shown: {e}")
        overlay = None
        timer = None
    try:
        yield overlay
    finally:
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        if overlay is not None:
            try:
                overlay.close_overlay()
            except RuntimeError:
                pass


class DeferredBusy:
    """Non-blocking please-wait: page stays interactive; sheet after *delay_ms*.

    Use while work continues via ``QTimer`` / chunk pumps — the user can already
    see the page. If the work finishes before *delay_ms*, nothing is shown.
    """

    def __init__(self, widget: QWidget, text: str = "",
                 delay_ms: int = _SHOW_AFTER_MS):
        self._overlay: BusyOverlay | None = None
        self._timer: QTimer | None = None
        self._closed = False
        if widget is None or not widget.isVisible():
            return
        try:
            self._overlay = BusyOverlay(widget, text)
            self._overlay._reveal_after_s = max(0, int(delay_ms)) / 1000.0
            if delay_ms <= 0:
                self._overlay.reveal()
            else:
                self._timer = QTimer(self._overlay)
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(self._overlay.reveal)
                self._timer.start(max(1, int(delay_ms)))
        except Exception as e:
            logger.debug(f"DeferredBusy could not start: {e}")
            self._overlay = None
            self._timer = None

    def set_base_text(self, text: str):
        """Update base label text on the underlying overlay."""
        if self._overlay is not None and not self._closed:
            self._overlay.set_base_text(text)

    def pump(self):
        """Pump the underlying overlay timer and paint events."""
        if self._overlay is not None and not self._closed:
            self._overlay.pump()

    def set_on_cancel(self, fn):
        """What the sheet's Cancel button should actually stop.

        Without one the button sets a flag, says "Cancelling…" and waits
        for a caller that is not listening — which is a button that does
        nothing, in front of a sheet that does not go away.
        """
        if self._overlay is not None and not self._closed:
            self._overlay.on_cancel = fn

    def alive(self) -> bool:
        """Whether there is still a sheet here to reuse."""
        return self._overlay is not None and not self._closed

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except RuntimeError:
                pass
            self._timer = None
        if self._overlay is not None:
            try:
                self._overlay.close_overlay()
            except RuntimeError:
                pass
            self._overlay = None
