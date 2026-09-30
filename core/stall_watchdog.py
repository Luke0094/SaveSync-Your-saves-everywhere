"""SaveSync — say where the GUI thread is stuck when it stops answering.

The log of a frozen window looks the same as the log of an idle one: the
background threads keep writing, the GUI thread writes nothing. This gives the
next freeze a cause on record. A heartbeat ticks on the GUI thread; a daemon
thread watches it, and when the beat goes stale it logs what the GUI thread is
executing right then — once per stall, plus how long it lasted.

Read-only: it never touches the GUI, never raises into it, and costs one
timer tick every half second.
"""
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

_BEAT_MS = 500
_POLL_S = 1.0
_STALL_S = 5.0


class StallWatchdog:
    def __init__(self, stall_s: float = _STALL_S, poll_s: float = _POLL_S):
        self._stall_s = stall_s
        self._poll_s = poll_s
        self._beat = time.monotonic()
        self._gui_ident = 0
        self._timer = None
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        """Call from the GUI thread, once the event loop is about to run."""
        if self._thread is not None:
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        self._gui_ident = threading.get_ident()
        self._timer = QTimer(QApplication.instance())
        self._timer.setInterval(_BEAT_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._beat = time.monotonic()
        self._thread = threading.Thread(
            target=self._watch, name="StallWatchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _tick(self) -> None:
        self._beat = time.monotonic()

    def _watch(self) -> None:
        stalled_since = None
        last_look = time.monotonic()
        while not self._stop.wait(self._poll_s):
            now = time.monotonic()
            # This thread's own clock jumping means the machine slept, not that
            # the GUI thread hung — start over rather than report a stall.
            if now - last_look > self._poll_s * 4:
                self._beat = now
                stalled_since = None
                last_look = now
                continue
            last_look = now
            age = now - self._beat
            if age >= self._stall_s:
                if stalled_since is None:
                    stalled_since = self._beat
                    self._report(age)
            elif stalled_since is not None:
                logger.warning(
                    f"GUI thread answering again after {now - stalled_since:.1f}s")
                stalled_since = None

    def _report(self, age: float) -> None:
        try:
            frame = sys._current_frames().get(self._gui_ident)
            stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
            logger.warning(
                f"GUI thread has not answered for {age:.1f}s — it is executing:\n{stack}")
        except Exception:
            logger.debug("stall report failed", exc_info=True)


_watchdog = None


def start_stall_watchdog() -> None:
    global _watchdog
    if _watchdog is None:
        _watchdog = StallWatchdog()
        _watchdog.start()
