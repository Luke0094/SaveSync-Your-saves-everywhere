"""SaveSync — pinned notes and images.

Pin something the player already has — a .txt of door codes, a map they
screenshotted — on top of the game, drag it somewhere it covers nothing
important, and find it there next session. A note can also be started from
scratch and saved wherever they want it.

Text pins are edited in place and written back to the ORIGINAL file, so the
note stays a normal file: usable outside SaveSync, editable by anything else,
and not trapped in a database. Nothing is copied into SaveSync's own storage,
which is also why pinning costs no disk space of its own.

Two deliberate differences from the game overlay, which shares most of its
window flags:

- a pin ACCEPTS focus. The overlay must never steal it (it only announces
  things), but a text pin is typed into, so ``WindowDoesNotAcceptFocus``
  would make it useless.
- nothing auto-hides a pin. The point of pinning something is that it is
  still there when you look back.
"""
import logging
import platform
from pathlib import Path

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizeGrip,
                               QSlider, QTextEdit, QVBoxLayout, QWidget)

from core.config_manager import get_config
from i18n import t
from ui import win_band_hook
from ui.helpers import (ElidedLabel, force_topmost, ForegroundWatcher,
                        game_is_running, popup_is_open, set_click_through, SystemCursor,
                        TRACE_Z, z_report, scaled)
from ui.styles.theme import ThemedMixin

logger = logging.getLogger(__name__)

TEXT_EXT = {".txt", ".md", ".log", ".csv", ".ini", ".cfg", ".json"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

_RECENT_MAX = 12
_DEFAULT_SIZE = QSize(280, 240)
_MIN_SIZE = QSize(160, 120)
# A picture pin may be much smaller than a note: there is no editor inside it
# that needs room to be typed in, only a header wide enough for a title and
# two buttons. Keeping the note floor for pictures is what leaves a panorama
# sitting in a band of empty space it never asked for.
_IMAGE_MIN_SIZE = QSize(140, 84)
# A pinned file is read into memory and, for text, written back. Refuse the
# ones that are obviously not notes: a multi-hundred-MB log would freeze the
# GUI thread on open, and nothing about it is readable in a 280px window.
_MAX_BYTES = 8 * 1024 * 1024
# Never fully invisible: a pin the player cannot find is a pin they cannot
# close, and these have no taskbar entry to get back to.
_MIN_OPACITY = 25
_GENERAL = "_general"
_UNSAVED = "unsaved"
# A pin made from nothing — a new note, a screen grab — takes its place in the
# recent list straight away, like one opened from a file. It has no path yet,
# so it holds that place with a marker instead, and the moment it is saved the
# marker becomes the real path WHERE IT ALREADY SITS. Never a path itself: a
# recent entry is always absolute, and nothing absolute begins like this.
_NEW_PREFIX = "pin:new:"
# How long Windows takes to finish handing the front to another window. Long
# enough that the order has settled, short enough that nobody sees the gap.
_SETTLE_MS = 120


def is_new_entry(entry) -> bool:
    """Whether a recent entry stands for a pin that has no file yet."""
    return str(entry).startswith(_NEW_PREFIX)




def kind_of(path) -> str:
    """"text", "image", or "" when this is not something we can pin."""
    ext = Path(path).suffix.lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in IMAGE_EXT:
        return "image"
    return ""


def is_pinnable(path) -> bool:
    if not kind_of(path):
        return False
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size <= _MAX_BYTES
    except OSError:
        return False


def pin_name_filter() -> str:
    exts = " ".join(f"*{e}" for e in sorted(TEXT_EXT | IMAGE_EXT))
    return f"{t('pin.add_filter')} ({exts})"


def _left_button_down() -> bool:
    """The left mouse button as the system has it (the physical one that is the primary: buttons can be swapped)."""
    import ctypes
    u = ctypes.windll.user32
    return bool(u.GetAsyncKeyState(0x02 if u.GetSystemMetrics(23) else 0x01) & 0x8000)


class _MousePoll(QObject):
    """Follows a drag of a pin over a game from the system's mouse as well as from the events. A window that is never the foreground one
    is sent the mouse only while the pointer is inside it (Windows gives the capture to the foreground window only), and the window
    runs behind the pointer when it is resized: a quick drag on the corner got ahead of it, the events stopped, and the pin was lost
    halfway. *on_pos(point)* is told where the pointer is every few milliseconds; *on_end()* once the button is up."""

    def __init__(self, on_pos, on_end):
        super().__init__()
        self._on_pos, self._on_end = on_pos, on_end
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        try:
            if not _left_button_down():
                self.stop()
                self._on_end()
                return
            self._on_pos(QCursor.pos())
        except RuntimeError:
            self.stop()

    def stop(self) -> None:
        try:
            self._timer.stop()
        except RuntimeError:
            pass


def _resize_by_mouse() -> bool:
    """Windows: a pin is resized from the mouse, the way it is moved by its title bar, and not by the system's own size loop (what
    QSizeGrip starts). That loop needs the pin to be the active window and runs under whatever fullscreen window is in front; moving
    a pin never depended on it, and resizing did - over a game in borderless fullscreen it would not begin."""
    return platform.system() == "Windows"


class PinGrip(QSizeGrip):
    """The corner grab, drawn rather than left to the platform style.

    A native size grip on a dark frameless window is all but invisible, and a
    resize handle nobody can find is a resize handle nobody uses. Keeps the
    real QSizeGrip behaviour underneath — this only changes how it looks.
    """

    def __init__(self, parent):
        super().__init__(parent)
        # Held explicitly: adding this to a layout reparents it, so parent()
        # is only incidentally the pin, and a silent hasattr() check would
        # turn a reparent into "resize no longer holds the pointer" rather
        # than into an error.
        self._pin = parent
        self.setFixedSize(scaled(14, self), scaled(14, self))
        self._hot = False
        self.dragging = False
        self.setToolTip(t("pin.resize"))

    def mousePressEvent(self, event):
        # A resize in progress is a reason to keep the pointer on screen, and
        # QSizeGrip runs its own loop — the pin cannot see it any other way.
        self.dragging = True
        self._notify()
        if getattr(self._pin, "_banded", False) or _resize_by_mouse():
            # Over a game in exclusive fullscreen the pin never takes the focus, and Windows' own resize loop (what
            # QSizeGrip starts) needs the window to be active: the pin is resized here from the mouse instead. So it is on
            # Windows over a game in borderless fullscreen, where that loop is not to be relied on either. The window runs
            # behind the pointer as it grows, and a pin that is not the foreground window is sent the mouse only while the pointer
            # is inside it: a quick drag got ahead of it and the resize was lost halfway. So the system's mouse is followed too.
            if event.button() == Qt.MouseButton.LeftButton:
                self._resize_from = (event.globalPosition().toPoint(), self._pin.size())
                self._stop_poll()
                self._poll = _MousePoll(self._resize_to, self._finish_resize)
            event.accept()
            return
        super().mousePressEvent(event)

    def _resize_to(self, point) -> None:
        start = getattr(self, "_resize_from", None)
        if start is None:
            return
        delta = point - start[0]
        floor = self._pin.minimumSize()
        self._pin.resize(max(floor.width(), start[1].width() + delta.x()),
                         max(floor.height(), start[1].height() + delta.y()))

    def _stop_poll(self) -> None:
        poll, self._poll = getattr(self, "_poll", None), None
        if poll is not None:
            poll.stop()
            poll.deleteLater()

    def _finish_resize(self) -> None:
        """The button is up (seen by the poll, or by the event, whichever first)."""
        self._stop_poll()
        self.dragging = False
        self._resize_from = None
        self._notify()
        self._pin.save_geometry()
        self._pin.assert_topmost()

    def mouseMoveEvent(self, event):
        start = getattr(self, "_resize_from", None)
        if start is not None:
            self._resize_to(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._stop_poll()
        self.dragging = False
        if getattr(self, "_resize_from", None) is not None:
            self._resize_from = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self._notify()
        # Resizing activates the pin just as dragging does, so it climbs back
        # on top here rather than on the next round of the timer.
        self._pin.save_geometry()
        self._pin.assert_topmost()

    def _notify(self):
        self._pin._sync_cursor()

    def enterEvent(self, event):
        self._hot = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hot = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        from ui.styles.theme import palette

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        colour = QColor(palette("accent") if self._hot else palette("text_muted"))
        p.setPen(QPen(colour, 1))
        w, h = self.width(), self.height()
        for offset in (2, 6, 10):          # three diagonal ticks, corner-wards
            p.drawLine(w - 2, h - offset, w - offset, h - 2)


_ZOOM_MIN, _ZOOM_MAX, _ZOOM_STEP = 50, 400, 25          # percent


class PinImageLabel(QLabel):
    """The picture of an image pin: fitted to the pin at 100 %, zoomed from there, and - once it is larger than what the pin shows -
    moved by dragging inside it. The picture is scaled once per zoom and kept; moving it is only drawing it somewhere else."""

    def __init__(self):
        super().__init__()
        self._src = None
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)          # from the centred position
        self._cache = None                     # ((width, height, fast), scaled pixmap)
        self._fast = False                     # while the zoom slider is being dragged: a quick scale, a good one after
        self._drag = None
        self._poll = None

    def set_source(self, px) -> None:
        if px is self._src:
            return
        self._src = px
        self._cache = None
        self.update()

    def _fit_size(self) -> tuple:
        """What 100 % is: the picture, in logical pixels, fitted into the label."""
        if self._src is None or self._src.isNull():
            return 0.0, 0.0
        dpr = self._src.devicePixelRatio() or 1.0
        sw, sh = self._src.width() / dpr, self._src.height() / dpr
        if sw <= 0 or sh <= 0:
            return 0.0, 0.0
        k = min(self.width() / sw, self.height() / sh)
        return sw * k, sh * k

    def _target(self) -> tuple:
        fw, fh = self._fit_size()
        return fw * self._zoom, fh * self._zoom

    def _limit(self) -> tuple:
        tw, th = self._target()
        return max(0.0, (tw - self.width()) / 2), max(0.0, (th - self.height()) / 2)

    def _clamp_pan(self) -> None:
        lx, ly = self._limit()
        self._pan = QPointF(max(-lx, min(lx, self._pan.x())), max(-ly, min(ly, self._pan.y())))

    def can_pan(self) -> bool:
        lx, ly = self._limit()
        return lx > 0.5 or ly > 0.5

    def _refresh_cursor(self) -> None:
        if self._src is not None and self.can_pan():
            self.setCursor(Qt.CursorShape.ClosedHandCursor if self._drag is not None else Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def set_zoom(self, zoom: float, fast: bool = False) -> None:
        zoom = max(_ZOOM_MIN / 100.0, min(_ZOOM_MAX / 100.0, zoom))
        old, self._zoom, self._fast = self._zoom, zoom, fast
        if old > 0:                                  # what is in the middle stays in the middle
            self._pan = QPointF(self._pan.x() * zoom / old, self._pan.y() * zoom / old)
        self._clamp_pan()
        self._cache = None
        self._refresh_cursor()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cache = None
        self._clamp_pan()
        self._refresh_cursor()

    def paintEvent(self, event):
        if self._src is None or self._src.isNull():
            super().paintEvent(event)
            return
        tw, th = self._target()
        if tw < 1 or th < 1:
            return
        dpr = self._src.devicePixelRatio() or 1.0
        key = (max(1, round(tw * dpr)), max(1, round(th * dpr)), self._fast)
        if self._cache is None or self._cache[0] != key:
            mode = Qt.TransformationMode.FastTransformation if self._fast else Qt.TransformationMode.SmoothTransformation
            px = self._src.scaled(key[0], key[1], Qt.AspectRatioMode.IgnoreAspectRatio, mode)
            px.setDevicePixelRatio(dpr)
            self._cache = (key, px)
        p = QPainter(self)
        p.drawPixmap(QPointF((self.width() - tw) / 2 + self._pan.x(), (self.height() - th) / 2 + self._pan.y()), self._cache[1])
        p.end()

    # Dragging inside it. Over a game the drag is also followed from the system's mouse (see _MousePoll).
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._src is not None and self.can_pan():
            self._drag = (event.globalPosition().toPoint(), QPointF(self._pan))
            self._refresh_cursor()
            if getattr(self.window(), "_banded", False):
                self._poll = _MousePoll(self._pan_to, self._end_pan)
            event.accept()
            return
        super().mousePressEvent(event)

    def _pan_to(self, point) -> None:
        if self._drag is None:
            return
        d = point - self._drag[0]
        self._pan = QPointF(self._drag[1].x() + d.x(), self._drag[1].y() + d.y())
        self._clamp_pan()
        self.update()

    def _end_pan(self) -> None:
        poll, self._poll = self._poll, None
        if poll is not None:
            poll.stop()
            poll.deleteLater()
        self._drag = None
        self._refresh_cursor()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self._pan_to(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._end_pan()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _TextPan(QObject):
    """Dragging inside a zoomed note moves the text instead of selecting it. A click still puts the caret where it was made; only
    a press that goes on to be dragged (past a few pixels) is a move. Active only while *active()* says the note is zoomed."""

    def __init__(self, edit, active):
        super().__init__(edit)
        self._edit, self._active = edit, active
        self._press = None                     # [global point, vertical value, horizontal value, moved]
        self._poll = None
        edit.viewport().installEventFilter(self)

    def _bars(self):
        return self._edit.verticalScrollBar(), self._edit.horizontalScrollBar()

    def _move_to(self, point) -> None:
        if self._press is None:
            return
        d = point - self._press[0]
        if not self._press[3] and abs(d.x()) + abs(d.y()) < 5:
            return
        if not self._press[3]:
            self._press[3] = True
            self._edit.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        v, h = self._bars()
        v.setValue(self._press[1] - d.y())
        h.setValue(self._press[2] - d.x())

    def _end(self) -> None:
        poll, self._poll = self._poll, None
        if poll is not None:
            poll.stop()
            poll.deleteLater()
        self._press = None
        self._edit.viewport().unsetCursor()

    def eventFilter(self, obj, ev):
        if not self._active():
            return False
        et = ev.type()
        if et == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            v, h = self._bars()
            self._press = [ev.globalPosition().toPoint(), v.value(), h.value(), False]
            if getattr(self._edit.window(), "_banded", False):
                self._poll = _MousePoll(self._move_to, self._end)
            return False                       # (the press still places the caret)
        if et == QEvent.Type.MouseMove and self._press is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self._move_to(ev.globalPosition().toPoint())
            return bool(self._press and self._press[3])      # (once it is a drag, the editor does not get it: no selecting)
        if et == QEvent.Type.MouseButtonRelease and self._press is not None:
            self._end()
            return False                       # (the editor is given the release of the press it was given: nothing moved, nothing selected)
        return False


class _ResetLabel(QLabel):
    """The percentage under the zoom bar. A click on it puts the zoom back to 100 %."""

    clicked = Signal()

    def __init__(self, text: str):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        # (taken, or the release would go to whatever is behind and the click would never complete)
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            event.accept()
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)


class PinZoomBar(QWidget):
    """A slim column on the right of a pin, shown while the pointer is on it: + at the top, - at the bottom, and between them a slider
    (up zooms in) - press the buttons, hold them, or drag. The percentage is under it; clicking it goes back to 100 %."""

    changed = Signal(int, bool)                # percent, still being dragged

    def __init__(self, pin):
        super().__init__(pin)
        self.setObjectName("pin_zoom_bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)      # (a QWidget subclass paints a style sheet's background only so)
        side = scaled(18, self)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 3, 0, 3)
        col.setSpacing(3)
        self._in = self._button("+", side, _ZOOM_STEP)
        self._out = self._button("\u2212", side, -_ZOOM_STEP)
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setObjectName("pin_zoom_slider")
        self._slider.setRange(_ZOOM_MIN, _ZOOM_MAX)
        self._slider.setValue(100)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slider.valueChanged.connect(self._moved)
        self._slider.sliderReleased.connect(lambda: self.changed.emit(self._slider.value(), False))
        self._label = _ResetLabel("100%")
        self._label.setObjectName("pin_zoom_label")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumHeight(scaled(16, self))                 # (a target to click, not only a number to read)
        self._label.clicked.connect(lambda: self._slider.setValue(100))
        col.addWidget(self._in, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self._slider, 1, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self._out, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self._label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.set_tooltips()
        self.setVisible(False)

    def _button(self, text: str, side: int, delta: int) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("pin_icon_btn")
        b.setFixedSize(side, side)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setAutoRepeat(True)
        b.setAutoRepeatDelay(350)
        b.setAutoRepeatInterval(90)
        b.clicked.connect(lambda: self._slider.setValue(self._slider.value() + delta))
        return b

    def set_tooltips(self) -> None:
        self._in.setToolTip(t("pin.zoom_in"))
        self._out.setToolTip(t("pin.zoom_out"))
        self._slider.setToolTip(t("pin.zoom"))
        self._label.setToolTip(t("pin.zoom_reset"))

    def _moved(self, value: int) -> None:
        self._label.setText(f"{value}%")
        self.changed.emit(value, self._slider.isSliderDown())

    def set_percent(self, percent: int) -> None:
        """Show a zoom that was put back (no change is announced)."""
        self._slider.blockSignals(True)
        self._slider.setValue(int(percent))
        self._slider.blockSignals(False)
        self._label.setText(f"{int(percent)}%")

    def is_dragging(self) -> bool:
        return self._slider.isSliderDown()


class PinItem(QWidget, ThemedMixin):
    """One pinned file in its own small always-on-top window.

    An empty *path* starts an unsaved note: it lives only on screen until the
    player saves it somewhere, and is dropped when the session ends.
    """

    closed = Signal(str)
    saved = Signal(str)

    def __init__(self, path: str = "", parent=None, pixmap: "QPixmap | None" = None):
        super().__init__(parent)
        self._path = str(path or "")
        if self._path:
            self._kind = kind_of(self._path)
        else:
            # An unsaved pin is a new note unless it arrived as a picture —
            # a screen capture has no file behind it yet either.
            self._kind = "image" if pixmap is not None else "text"
        self._unsaved = not self._path
        self._drag_from: QPoint | None = None
        self._source_px: QPixmap | None = None
        # Set by _load; a file that did not decode cleanly, or could not be
        # read at all, is shown but never written back.
        self._writable = True
        # Set only by a real edit. _load blocks signals while filling the
        # body, so opening a pin can never look like a change.
        self._edited = False
        self._zoom = 100                      # percent (see PinZoomBar)
        self._cursor_key = f"pin:{id(self)}"
        # X11: override-redirect while a fullscreen window is in front (see ui.x11_fullscreen)
        self._x11_bypass = False
        # Windows: over a game in exclusive fullscreen the pin is not put on the screen but mirrored into a window
        # (see ui.pin_mirror); it then has no native window of its own to raise
        self._mirrored = False
        # Windows: over a game in exclusive fullscreen the pin is made as a real window of the higher band that can never
        # take the focus (see ui.win_band_hook): it moves, resizes and closes as ever, and the game is left alone.
        self._banded = False
        self.setProperty("xs_keep", True)       # its band-16 window is managed here, not swept (ui.exclusive_session)
        # Set when a write back to the pinned file failed. The pin then has
        # nowhere it can be trusted to save to, which is the one thing that
        # brings the 💾 back on a note that already has a file.
        self._write_failed = False
        # Where this pin sits in the recent list while it has no file of its
        # own, and which game's list that is. Filled in by the manager; both
        # go once the pin is saved and its real path takes the place.
        self.recent_key = ""
        self.recent_gid = ""
        # Hiding a window raises an activation change, which re-runs
        # _sync_cursor — mid-close, with a grip still flagged as dragging,
        # that would re-take the pointer a line after releasing it.
        self._closing = False
        self._setup_window()
        self._build()
        if self._path:
            self._load()
        elif pixmap is not None:
            self._source_px = pixmap
            self._rescale()
        remembered = self._restore_geometry()
        # Only when the player has not already sized this pin themselves —
        # their choice outranks the picture's proportions.
        if not remembered and self._kind == "image" and self._source_px is not None:
            self._fit_window_to(self._source_px)

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_unsaved(self) -> bool:
        return self._unsaved

    def _floor(self) -> QSize:
        """Smallest this pin may be — smaller for pictures, see the constant."""
        base = _IMAGE_MIN_SIZE if self._kind == "image" else _MIN_SIZE
        return QSize(scaled(base.width(), self), scaled(base.height(), self))

    def set_x11_bypass(self, want: bool) -> None:
        """Plain X11: a managed window is stacked under a fullscreen one, so while one is in front the pin is an
        override-redirect window (measured: it then stays above the game, which stays fullscreen), and a
        managed one again when it is not. Changing the flag recreates the native window - hidden first, put
        back where it was, shown again if it was on screen - so only when the answer changes. An
        override-redirect window is given no keyboard by the window manager: see _X11FocusFilter."""
        if want == self._x11_bypass:
            return
        self._x11_bypass = want
        geo = self.geometry()
        was_visible = self.isVisible()
        self.hide()
        self.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, want)
        self.setGeometry(geo)
        if was_visible:
            # Not in the same turn of the event loop: a window hidden, re-flagged and shown within one turn is
            # left unmapped by the xcb plugin (measured).
            QTimer.singleShot(40, lambda g=geo: self._reshow_after_flag_change(g))

    def _reshow_after_flag_change(self, geo) -> None:
        if self._closing:
            return
        try:
            self.setGeometry(geo)
            self.show()
            self.raise_()
        except RuntimeError:
            pass                              # deleted meanwhile

    def assert_topmost(self):
        """Put this pin back above everything else.

        Qt's WindowStaysOnTopHint is not enough on Windows: clicking another
        window re-stacks ours behind it, and a pin you cannot click is a pin
        you cannot use. Same treatment the overlay gets, re-asserted on a
        timer for the same reason.
        """
        if self.isVisible() and not self._mirrored:
            from ui.blur_modal import backdrop_visible
            if backdrop_visible():
                return                        # under the dimmed backdrop, like everything else the dialogs are over
            force_topmost(self)
            # A Leave that never came (the pin was remade under the pointer, the game moved the cursor) must not keep the pointer
            # held for good: where the pointer is, is asked again here.
            try:
                over = self.frameGeometry().contains(QCursor.pos())
                if bool(getattr(self, "_hovered", False)) != over:
                    self._hovered = over
                    self._sync_cursor()
            except RuntimeError:
                return
            self._apply_click_through()

    def _apply_click_through(self) -> None:
        """Over a game in exclusive fullscreen a pin takes the mouse only where the player has a pointer: the game shows its own
        cursor, or the pointer was summoned (the hotkey). A game that has hidden its cursor gets its mouse back through the pin -
        the cursor appeared over a pin's rectangle, and the pin could be moved and resized by a mouse the player could not see.
        Same rule as the notices (ui.overlay)."""
        try:
            if not self._banded or not self.isVisible():
                return
            set_click_through(self, SystemCursor.game_hides_cursor())
        except RuntimeError:
            pass                              # deleted meanwhile

    # ── Mouse pointer ────────────────────────────────────────────────────────

    def _sync_cursor(self):
        """Keep the pointer on screen for as long as this pin is being used.

        A game may be hiding it, and the overlay lets go of it when it
        auto-hides — which must not pull the pointer out from under a note
        somebody is still typing in, or a pin halfway through a drag. Being
        the active window covers writing and clicking about inside it;
        clicking away deactivates it and hands the pointer back.
        """
        # Only while a game is actually running: on the desktop the pointer
        # is already there and nothing needs undoing. Same rule the overlay
        # uses, from the same place, so the two halves cannot drift apart.
        busy = (not self._closing and game_is_running()
                and (self.isActiveWindow()
                     or self._drag_from is not None
                     or self._grip.dragging
                     or getattr(self, "_hovered", False)))
        if busy:
            SystemCursor.hold(self._cursor_key)
        else:
            SystemCursor.release(self._cursor_key)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            self._sync_cursor()

    # ── Window ───────────────────────────────────────────────────────────────

    # ── Over a game in exclusive fullscreen ──────────────────────────────────────────────────────────

    def _band_state(self, on: bool) -> None:
        """The window attributes of a pin that is shown over such a game: translucent (so Qt keeps it layered), never
        activated. A pin in the ordinary state takes the focus when clicked, as it needs to for typing."""
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, on)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, on)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, on)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, on)
        self._banded = on

    def paintEvent(self, event):
        # A translucent window only takes the mouse where it has painted something: the card's own background has to be
        # drawn, or the footer (and with it the resize grip) is a hole the click falls through.
        if self._banded:
            from PySide6.QtGui import QPainter
            from PySide6.QtWidgets import QStyle, QStyleOption
            opt = QStyleOption()
            opt.initFrom(self)
            p = QPainter(self)
            self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, opt, p, self)
            p.end()
        super().paintEvent(event)

    def enter_band(self) -> None:
        was_visible = self.isVisible()
        self.hide()
        self._band_state(True)
        if was_visible:
            self.show()
            QTimer.singleShot(0, self._apply_click_through)

    def leave_band(self) -> None:
        was_visible = self.isVisible()
        self.hide()
        self.destroy()                    # the native window was made in band 16: the next one is an ordinary one
        self._band_state(False)
        if was_visible and not getattr(self, "_closing", False):
            # The ordinary window is made with band mode OFF: made inside it, it is a band-16 window that is not translucent, Qt
            # takes its layered style off, and a band-16 window that is not layered takes the game to the desktop (the system's
            # list of our windows in a real game: a pin with no layered bit, the moment the pins decided the game was not exclusive).
            previous = win_band_hook.set_mode(False)
            try:
                self.show()
                self.raise_()
                self.assert_topmost()
            finally:
                win_band_hook.set_mode(previous)

    def setVisible(self, visible):
        if visible and win_band_hook.installed():
            if not self._banded:
                try:
                    if get_pin_manager()._wants_mirroring():
                        self._band_state(True)        # a pin made while such a game is in front: right from its first show
                except Exception:
                    pass
            if self._banded:
                win_band_hook.ensure_band(self)
                with win_band_hook.band_windows():
                    super().setVisible(visible)
                _begin_pins_session()
                QTimer.singleShot(0, self._apply_click_through)
                return
        super().setVisible(visible)

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setObjectName("pin_item")
        self.setMinimumSize(self._floor())

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        # The header doubles as the drag handle — see mousePressEvent.
        self._header = QWidget()
        self._header.setObjectName("pin_header")
        self._header.setFixedHeight(scaled(24, self))
        head = QHBoxLayout(self._header)
        head.setContentsMargins(8, 0, 2, 0)
        head.setSpacing(2)
        self._title = ElidedLabel(self.display_name())
        self._title.setObjectName("pin_title")
        self._title.setToolTip(self._path or t("pin.unsaved_hint"))
        head.addWidget(self._title, 1)

        # Save is offered only while there is nowhere to save to yet.
        self._save_btn = QPushButton("💾")
        self._save_btn.setObjectName("pin_icon_btn")
        self._save_btn.setFixedSize(scaled(18, self), scaled(18, self))
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setToolTip(t("pin.save"))
        self._save_btn.clicked.connect(self.save_as)
        self._save_btn.setVisible(self._unsaved)
        head.addWidget(self._save_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("pin_icon_btn")
        self._close_btn.setFixedSize(scaled(18, self), scaled(18, self))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip(t("pin.close"))
        self._close_btn.clicked.connect(self.close)
        head.addWidget(self._close_btn)
        root.addWidget(self._header)

        if self._kind == "text":
            self._body = QTextEdit()
            self._body.setObjectName("pin_text")
            self._body.setAcceptRichText(False)
            # Editable straight away: clicking a pinned note to write in it is
            # the whole point, and a read-only-until-unlocked step would put a
            # click between the player and the thing they came to write down.
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(600)      # not a write per keystroke
            self._save_timer.timeout.connect(self._write_back)
            self._body.textChanged.connect(self._on_edited)
            self._text_pan = _TextPan(self._body, lambda: self._zoom != 100)
        else:
            self._body = PinImageLabel()
            self._body.setObjectName("pin_image")
            self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body.setMinimumSize(1, 1)
        root.addWidget(self._body, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addStretch(1)
        self._grip = PinGrip(self)
        foot.addWidget(self._grip, 0,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        root.addLayout(foot)

        # Opacity slider: a floating child, NOT part of the layout, so it can
        # appear and disappear on hover without reflowing the note under it.
        self._fade = QSlider(Qt.Orientation.Horizontal, self)
        self._fade.setObjectName("pin_fade")
        self._fade.setRange(_MIN_OPACITY, 100)
        self._fade.setValue(100)
        self._fade.setFixedSize(scaled(96, self), scaled(14, self))
        self._fade.setToolTip(t("pin.opacity"))
        self._fade.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fade.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.sliderReleased.connect(self._save_opacity)
        self._fade.setVisible(False)
        # The zoom bar floats too, down the right side: shown with the pointer on the pin, never part of the layout.
        self._zoom_bar = PinZoomBar(self)
        self._zoom_bar.changed.connect(self._on_zoom)
        if self._kind == "text":
            # the note's scroll bar comes and goes with its length and its zoom: the zoom bar keeps off it
            self._body.verticalScrollBar().rangeChanged.connect(lambda *_: self._place_zoom())
        self.setMouseTracking(True)

    def display_name(self) -> str:
        if self._path:
            return Path(self._path).name
        return t("pin.capture") if self._kind == "image" else t("pin.untitled")

    # ── Zoom ─────────────────────────────────────────────────────────────────

    def _on_zoom(self, percent: int, dragging: bool = False) -> None:
        self._apply_zoom(percent, dragging)
        if not dragging:
            self._save_zoom()                 # (not on every step of a drag of the slider: when it is let go)

    def _apply_zoom(self, percent: int, dragging: bool = False) -> None:
        self._zoom = int(percent)
        if self._kind == "image":
            self._body.set_zoom(percent / 100.0, fast=dragging)
        else:
            self._apply_text_zoom(percent)

    def _save_zoom(self) -> None:
        """Filed under the same name as the pin's place and fade, so it is there when the pin is opened again - and moves to the file
        when a note that had none is saved (_carry_over). 100 % is not stored: it is what a pin starts at."""
        key = self.store_key()
        if not key:
            return
        store = dict(get_config().get("pins_zoom", {}) or {})
        if self._zoom == 100:
            if store.pop(key, None) is None:
                return
        else:
            store.pop(key, None)              # (re-inserted last: the oldest go first when the store is trimmed)
            store[key] = int(self._zoom)
        if len(store) > _RECENT_MAX * 3:
            for stale in list(store)[:len(store) - _RECENT_MAX * 3]:
                store.pop(stale, None)
        get_config().set("pins_zoom", store)

    def _apply_text_zoom(self, percent: int) -> None:
        """The note's text is made larger or smaller by a size of its own (the theme's size is set by its style sheet, which a font set
        in code would not beat); where in the note the reader was stays where it was."""
        bar = self._body.verticalScrollBar()
        ratio = bar.value() / bar.maximum() if bar.maximum() else 0.0
        if percent == 100:
            self._body.setStyleSheet("")
        else:
            self._body.setStyleSheet(f"font-size: {max(6, round(scaled(12, self) * percent / 100.0))}px;")
        QTimer.singleShot(0, lambda: bar.setValue(int(ratio * bar.maximum())))

    def _place_zoom(self) -> None:
        top = self._header.height() + 6
        room = self.height() - top - self._grip.height() - 8
        h = max(0, min(scaled(170, self), room))
        w = scaled(20, self)
        right = 3
        if self._kind == "text":
            bar = self._body.verticalScrollBar()
            if bar.isVisible():
                right = self.width() - bar.mapTo(self, QPoint(0, 0)).x() + 2        # (from the scroll bar's own left edge)
        self._zoom_bar.setGeometry(self.width() - w - right, top, w, h)
        self._zoom_bar.setVisible(self._zoom_bar.isVisible() and h >= scaled(80, self))

    # ── Opacity ──────────────────────────────────────────────────────────────

    def _on_fade(self, value: int):
        self.setWindowOpacity(max(_MIN_OPACITY, value) / 100.0)

    def _save_opacity(self):
        key = self.store_key()
        if not key:
            return
        store = dict(get_config().get("pins_opacity", {}) or {})
        store[key] = int(self._fade.value())
        if len(store) > _RECENT_MAX * 3:
            for stale in list(store)[:len(store) - _RECENT_MAX * 3]:
                store.pop(stale, None)
        get_config().set("pins_opacity", store)

    def enterEvent(self, event):
        # The slider is only offered while the pointer is on the pin: it is a
        # control for the thing under the cursor, not permanent furniture.
        self._fade.setVisible(True)
        self._fade.raise_()
        self._zoom_bar.setVisible(True)
        self._place_zoom()
        self._zoom_bar.raise_()
        # The pointer that was summoned goes by itself a few seconds after the overlay (the timer of what summoned it): not while it is
        # on a pin - the pin holds it for as long as it rests there, so that the pin can be moved and resized without calling the
        # overlay up again.
        self._hovered = True
        self._sync_cursor()
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Not while it is being dragged, or the slider would vanish from under
        # the pointer the moment the drag left the window.
        if not self._fade.isSliderDown():
            self._fade.setVisible(False)
        if not self._zoom_bar.is_dragging():
            self._zoom_bar.setVisible(False)
        self._hovered = False
        QTimer.singleShot(900, self._sync_cursor)          # (a moment's grace: the pointer crosses from one pin to the next)
        super().leaveEvent(event)

    def _place_fade(self):
        w, h = self._fade.width(), self._fade.height()
        self._fade.setGeometry(QRect((self.width() - w) // 2,
                                     self.height() - h - 4, w, h))

    # ── Content ──────────────────────────────────────────────────────────────

    def _load(self):
        if self._kind == "text":
            # Read STRICTLY. A file we could not decode cleanly must never be
            # written back: doing so would replace every byte we failed to
            # understand with "?" in the player's own file. Same for a file we
            # could not read at all — the placeholder shown in its place is a
            # message, not content, and writing it out would destroy the file.
            text, writable = "", True
            try:
                text = Path(self._path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                writable = False
                try:
                    text = Path(self._path).read_text(encoding="utf-8",
                                                      errors="replace")
                except OSError:
                    text = t("pin.unreadable")
                logger.info(f"Pinned read-only (not UTF-8): {self._path}")
            except OSError as e:
                logger.warning(f"Could not read the pinned note {self._path}: {e}")
                text, writable = t("pin.unreadable"), False
            self._writable = writable
            self._body.setReadOnly(not writable)
            if not writable:
                self._title.setToolTip(f"{self._path}\n{t('pin.read_only')}")
            self._body.blockSignals(True)
            self._body.setPlainText(text)
            self._body.blockSignals(False)
        else:
            px = QPixmap(self._path)
            self._source_px = None if px.isNull() else px
            if self._source_px is None:
                self._body.setText(t("pin.unreadable"))
            self._rescale()

    def _on_edited(self):
        self._edited = True
        self._save_timer.start()

    def _write_back(self):
        """Write the note back to the file the player pinned.

        Only ever when there IS a file, it was read cleanly, and something was
        actually typed: pinning a file to look at it must not rewrite it, and
        an unsaved note has nowhere to go until the player says where.
        """
        if self._unsaved or not (self._writable and self._edited):
            return
        try:
            Path(self._path).write_text(self._body.toPlainText(), encoding="utf-8")
        except OSError as e:
            # Silently swallowing this is how a note gets lost for good: the
            # pin goes on looking normal, the player goes on typing, and the
            # file the words were meant for never receives one of them —
            # a drive unplugged, a file turned read-only. So the pin says so,
            # and offers the 💾 again: a place to save to is exactly what it
            # no longer has.
            logger.warning(f"Could not save the pinned note {self._path}: {e}")
            self._write_failed = True
            self._save_btn.setVisible(True)
            self._save_btn.setToolTip(t("pin.write_failed"))
            self._title.setToolTip(f"{self._path}\n{t('pin.write_failed')}")

    def _fit_window_to(self, px: QPixmap):
        """Open a picture pin in the shape of the picture.

        The image itself is never stretched — it is scaled with the aspect
        ratio kept — but that only decides how it sits INSIDE the window. If
        the window ignores its proportions, a tall screenshot lands in a
        square pin as a thin strip with three quarters of the pin empty. So
        the window is sized from the picture: one scale factor for both
        sides, never enlarging beyond the original, bounded by half the
        screen so a full-height grab is not a full-height window.

        The floors still win where they must — the header needs room for a
        title and two buttons — so an extremely narrow picture still gets
        some empty space. Nothing is distorted either way.
        """
        if px is None or px.isNull():
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        floor = self._floor()
        max_w, max_h = 640, 520
        if screen is not None:
            g = screen.availableGeometry()
            max_w = max(floor.width(), min(max_w, int(g.width() * 0.5)))
            max_h = max(floor.height(), min(max_h, int(g.height() * 0.5)))
        # Header and grip are not part of the picture, so they must not be
        # counted into the space it is being fitted to. Measured from the
        # built layout rather than assumed, so a font change cannot skew it.
        self.layout().activate()
        chrome = self.height() - self._body.height()
        if chrome <= 0:
            chrome = self._header.height() + self._grip.height() + 4
        room_h = max(40, max_h - chrome)
        # The window is measured in logical pixels, so the picture must be
        # too: a grab from a scaled display is physical pixels, and sizing to
        # those makes the window ratio-of-the-display times too big, with the
        # picture drawn small inside it.
        dpr = px.devicePixelRatio() or 1.0
        src_w, src_h = max(1.0, px.width() / dpr), max(1.0, px.height() / dpr)
        scale = min(max_w / src_w, room_h / src_h, 1.0)
        w = max(floor.width(), int(round(src_w * scale)))
        h = max(floor.height(), int(round(src_h * scale)) + chrome)
        self.resize(QSize(w, h))
        self.move(self._clamp(self.pos()))
        self._place_fade()

    def _confirm_overwrite(self, path: str) -> bool:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(t("pin.save_title"))
        box.setText(t("pin.overwrite", name=Path(path).name))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def save_as(self) -> str:
        """Give an unsaved note a home. Returns the chosen path, or ""."""
        if not self._banded:
            return self._save_as()
        # Over a game in exclusive fullscreen the picker and the questions that follow it (a file that exists: Qt's own, then this
        # pin's) are windows made for that game, one after the other. The pin alone does not make windows, and between one window
        # closing and the next opening nothing else would hold the session: the next one would be an ordinary window.
        from ui.exclusive_session import window_scope
        with window_scope(f"pin-save-{id(self)}", backdrop=True):
            return self._save_as()

    def _save_as(self) -> str:
        from ui.widgets.file_pickers import pick_save_path

        picture = self._kind == "image"
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT if picture else TEXT_EXT))
        chosen = pick_save_path(
            self, t("pin.save_title"),
            f"{t('pin.save_filter_image') if picture else t('pin.save_filter')} ({exts})",
            default_suffix="png" if picture else "txt")
        if not chosen:
            return ""
        if not Path(chosen).suffix:
            chosen += ".png" if picture else ".txt"
        # Belt as well as braces. The picker asks before replacing a file, but
        # the suffix we just appended can land on an existing name the dialog
        # never saw, and a new note quietly overwriting someone's file is the
        # worst thing this feature could do.
        if Path(chosen).exists() and not self._confirm_overwrite(chosen):
            return ""
        if picture:
            if self._source_px is None or not self._source_px.save(chosen):
                logger.warning(f"Could not save the capture to {chosen}")
                return ""
        else:
            try:
                Path(chosen).write_text(self._body.toPlainText(), encoding="utf-8")
            except OSError as e:
                logger.warning(f"Could not save the new note to {chosen}: {e}")
                return ""
        self._path = str(chosen)
        self._unsaved = False
        self._writable = True
        self._edited = False
        self._write_failed = False
        self._save_btn.setVisible(False)
        self._save_btn.setToolTip(t("pin.save"))
        # setFullText, not setText: this label shortens itself to fit and
        # keeps the whole string to shorten FROM. setText writes over what is
        # on screen and leaves that string behind, so the next time the pin is
        # resized the title goes back to saying "New note".
        self._title.setFullText(self.display_name())
        self._title.setToolTip(self._path)
        self.saved.emit(self._path)
        return self._path

    def _rescale(self):
        if self._kind != "image" or self._source_px is None:
            return
        area = self._body.size()
        if area.width() < 2 or area.height() < 2:
            return
        # The label draws the picture itself (PinImageLabel): in LOGICAL pixels, with the screen grab's device pixel ratio taken into
        # account (a grab from a scaled display is measured in physical ones), at the zoom the pin has.
        self._body.set_source(self._source_px)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()
        self._place_fade()
        self._place_zoom()

    # ── Geometry ─────────────────────────────────────────────────────────────

    def _clamp(self, pos: QPoint) -> QPoint:
        """Keep the pin on a screen that actually exists.

        Without this a drag past the edge — or a monitor unplugged since the
        position was saved — leaves it somewhere unreachable, and a window
        with no frame gives the player no way to bring it back.
        """
        probe = QPoint(pos.x() + self.width() // 2, pos.y() + 12)
        screen = (QGuiApplication.screenAt(probe) or self.screen()
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return pos
        g = screen.availableGeometry()
        return QPoint(max(g.left(), min(pos.x(), g.right() - self.width() + 1)),
                      max(g.top(), min(pos.y(), g.bottom() - self.height() + 1)))

    def _default_pos(self) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return QPoint(80, 80)
        g = screen.availableGeometry()
        return QPoint(g.right() - self.width() - 40, g.top() + 90)

    def store_key(self) -> str:
        """What this pin's position and fade are filed under.

        A pin with a file uses the file. One without still needs a name to be
        remembered by while it is on screen — otherwise moving a new note
        does nothing that lasts — and the place it holds in the recent list
        is exactly such a name. When the note is saved, what was filed under
        that name moves to the file, so a note put where you wanted it stays
        there.
        """
        return self._path or self.recent_key

    def _restore_geometry(self) -> bool:
        """True when a size the player chose was put back."""
        key = self.store_key()
        geo = ((get_config().get("pins_geometry", {}) or {}).get(key) or []
               if key else [])
        remembered = len(geo) == 4
        floor = self._floor()
        if remembered:
            self.resize(QSize(max(floor.width(), int(geo[2])),
                              max(floor.height(), int(geo[3]))))
            p = QPoint(int(geo[0]), int(geo[1]))
            # The saved spot may belong to a monitor that is no longer there.
            if QGuiApplication.screenAt(p) is None:
                p = self._default_pos()
            self.move(self._clamp(p))
        else:
            self.resize(_DEFAULT_SIZE)
            self.move(self._default_pos())
        saved = ((get_config().get("pins_opacity", {}) or {}).get(key)
                 if key else None)
        if isinstance(saved, int):
            self._fade.setValue(max(_MIN_OPACITY, min(100, saved)))
        zoom = ((get_config().get("pins_zoom", {}) or {}).get(key) if key else None)
        if isinstance(zoom, int) and _ZOOM_MIN <= zoom <= _ZOOM_MAX and zoom != 100:
            self._zoom_bar.set_percent(zoom)
            self._apply_zoom(zoom)
        self._place_fade()
        return remembered

    def save_geometry(self):
        key = self.store_key()
        if not key:
            return
        cfg = get_config()
        geo = dict(cfg.get("pins_geometry", {}) or {})
        geo[key] = [self.x(), self.y(), self.width(), self.height()]
        # Bounded: positions of pins never opened again must not pile up.
        if len(geo) > _RECENT_MAX * 3:
            for stale in list(geo)[:len(geo) - _RECENT_MAX * 3]:
                geo.pop(stale, None)
        cfg.set("pins_geometry", geo)

    # ── Dragging ─────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._header.geometry().contains(event.position().toPoint())):
            self._drag_from = (event.globalPosition().toPoint()
                               - self.frameGeometry().topLeft())
            self._sync_cursor()
            if self._banded:
                self._stop_move_poll()
                self._move_poll = _MousePoll(self._move_to, self._finish_move)
            event.accept()
            return
        super().mousePressEvent(event)

    def _move_to(self, point) -> None:
        if self._drag_from is not None:
            self.move(self._clamp(point - self._drag_from))

    def _stop_move_poll(self) -> None:
        poll, self._move_poll = getattr(self, "_move_poll", None), None
        if poll is not None:
            poll.stop()
            poll.deleteLater()

    def _finish_move(self) -> None:
        """The button is up and no release came (the pointer was outside the window)."""
        if self._drag_from is not None:
            self._stop_move_poll()
            self._drag_from = None
            self.save_geometry()
            self._sync_cursor()
            self.assert_topmost()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self.move(self._clamp(event.globalPosition().toPoint() - self._drag_from))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._stop_move_poll()
        if self._drag_from is not None:
            self._drag_from = None
            self.save_geometry()
            self._sync_cursor()
            # Dragging a pin makes it the active window; clicking back into
            # the game hands the front to a window that puts itself above
            # everything, and the pin waits for the next round of the timer
            # to climb back. Saying it here means the pin is already on top
            # when the pointer leaves it, which is when it is noticed.
            self.assert_topmost()
            if TRACE_Z:
                logger.info(f"pins: put back on top (after a drag) "
                            f"— {z_report(self)}")
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def update_locale(self):
        self._zoom_bar.set_tooltips()
        self._close_btn.setToolTip(t("pin.close"))
        self._save_btn.setToolTip(t("pin.save"))
        self._fade.setToolTip(t("pin.opacity"))
        self._grip.setToolTip(t("pin.resize"))
        if self._unsaved:
            # Same reason as in save_as: the label has to be told the whole
            # string, not just what to paint this once.
            self._title.setFullText(self.display_name())

    def closeEvent(self, event):
        if self._kind == "text":
            if self._save_timer.isActive():
                self._save_timer.stop()
            self._write_back()
        self.save_geometry()
        self._closing = True
        self._drag_from = None
        self._grip.dragging = False
        SystemCursor.release(self._cursor_key)
        self.closed.emit(self._path or _UNSAVED)
        super().closeEvent(event)


class _X11FocusFilter(QObject):
    """Installed only while a fullscreen window is in front on X11 and the pins are override-redirect windows,
    which the window manager gives no keyboard: a press inside a pin hands it the keyboard (XSetInputFocus), so
    a note can be typed into over the game - measured to work, with the game staying fullscreen. Under XWayland that is
    not enough (the compositor owns the keyboard): the press also takes a keyboard grab (ui.x11_fullscreen.start_typing)."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget):
            top = obj.window()
            # Only a click INTO a note's text takes the keyboard. Any press in a pin used to (its title bar, its picture, its grip): the game
            # lost the focus to a window nobody was going to type into - and a game that pauses when it does, paused - and, no longer
            # the window in front, was no longer seen as the fullscreen one, so the pins were made ordinary windows again.
            if (isinstance(top, PinItem) and top._x11_bypass and top._kind == "text"
                    and (obj is top._body or obj is top._body.viewport())):
                from ui.x11_fullscreen import focus_window, start_typing, x11_id
                focus_window(x11_id(top))
                start_typing(top)        # (under XWayland only: the keyboard belongs to the compositor there, see ui.x11_fullscreen)
        return False


class PinManager(QObject):
    """Owns the open pins and the per-game recent list behind the 📌 menu."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self._open: dict[str, PinItem] = {}
        self._unsaved: list[PinItem] = []
        # The places held in the recent list by pins that have no file yet.
        self._holding: dict[str, PinItem] = {}
        self._new_count = 0
        self._shutting_down = False
        # One timer for every pin, not one each: re-asserting z-order is a
        # handful of Win32 calls, and a per-pin timer would multiply them by
        # however many are on screen.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(1200)
        self._topmost_timer.timeout.connect(self._assert_topmost)
        # The timer alone is a backstop, not the answer. A game takes the
        # front back on every alt-tab and every loading screen, and puts
        # itself above everything as it does; a pin that only gets a turn
        # once a second spends most of that second behind it, which is what
        # "it doesn't stay pinned" looks like. This says the instant it
        # happens — the same thing that keeps the notifications up, asked for
        # here on the pins' own account rather than through the overlay.
        self._foreground = ForegroundWatcher.instance()
        self._foreground.changed.connect(self._on_foreground_changed)
        self._watching = False
        # Which game the pins on screen belong to. Held explicitly because at
        # game-exit time the monitor no longer reports it as running, and the
        # record has to be filed against the game it came from.
        self._session_game = ""
        self._x11_focus_filter = None
        self._mirrors: dict = {}          # id(pin) -> PinMirror: pins drawn over an exclusive-fullscreen game
        SystemCursor.add_observer(self._on_pointer_changed)       # (the pointer was summoned / let go: the pins take / leave the mouse)
        self._drop_stale_places()

    # ── Which game these belong to ───────────────────────────────────────────

    def current_game_id(self) -> str:
        """Recents are per game: a map pinned for one game is noise in
        another's list. Falls back to a shared bucket outside a session."""
        try:
            from core.monitor import get_monitor
            playing = get_monitor().currently_playing()
            if playing:
                return str(playing[0].id)
        except Exception as e:
            logger.debug(f"Could not read the running game for pins: {e}")
        return _GENERAL

    def _all_recent(self) -> dict:
        raw = get_config().get("pins_recent", {}) or {}
        # Migration: the first version kept one shared list.
        if isinstance(raw, list):
            return {_GENERAL: [str(p) for p in raw]}
        return {str(k): list(v) for k, v in raw.items() if isinstance(v, list)}

    def _on_pointer_changed(self) -> None:
        for item in list(self._open.values()) + list(self._unsaved):
            try:
                item._apply_click_through()
            except RuntimeError:
                continue                      # deleted meanwhile

    def assert_topmost_all(self, why: str = "asked"):
        """Put every pin back on top, on request from elsewhere.

        The overlay calls this right after raising itself: both windows sit in
        the always-on-top group and both re-assert on their own timer, so the
        last one to ask wins, and left to chance the overlay's second beat the
        pins' 1.2 — a pin made from the overlay's own menu vanished behind it.
        """
        self._assert_topmost(why)

    def _assert_topmost(self, why: str = "timer"):
        if popup_is_open():
            return
        pins = list(self._open.values()) + list(self._unsaved)
        self._apply_fullscreen_stacking(pins)
        self._apply_exclusive_mirroring(pins)
        for item in pins:
            item.assert_topmost()
        if TRACE_Z and pins:
            logger.info(f"pins: put back on top ({why}, {len(pins)} pin(s)) "
                        f"— {z_report(pins[0])}")

    def _any_banded(self) -> bool:
        """Is a pin shown as a window of the higher band right now?"""
        try:
            return any(getattr(i, "_banded", False) and i.isVisible() for i in list(self._open.values()) + list(self._unsaved))
        except RuntimeError:
            return False

    def _wants_mirroring(self) -> bool:
        """Windows: is a game in exclusive fullscreen in front - and is it one the picture is allowed over (the
        same setting and deny-list the overlay obeys)?"""
        try:
            if not get_config().get("overlay_over_exclusive", True):
                return False
            from ui.exclusive_presenter import (ExclusivePresenter, foreground_hwnd, foreground_is_exclusive,
                                                process_name)
            if not ExclusivePresenter.supported() or not foreground_is_exclusive():
                return False
            exe = process_name(foreground_hwnd())
            # the pins are real windows when the hook is there, pictures when it is not: each has its own refusals
            key = "overlay_window_deny" if win_band_hook.installed() else "overlay_exclusive_deny"
            denied = {str(e).lower() for e in (get_config().get(key, []) or [])}
            return not (exe and exe.lower() in denied)
        except Exception:
            return False

    def _apply_exclusive_mirroring(self, pins=None) -> None:
        """Windows: while a game is in exclusive fullscreen in front, every pin is mirrored (see ui.pin_mirror)
        instead of being a window the game would have to share the screen with; when it is not, each pin has its
        own window back. A no-op elsewhere."""
        import platform
        if platform.system() != "Windows":
            return
        pins = list(self._open.values()) + list(self._unsaved) if pins is None else pins
        if win_band_hook.installed():
            want = self._wants_mirroring()
            for item in pins:
                try:
                    if want and not item._banded and item.isVisible():
                        item.enter_band()
                    elif not want and item._banded:
                        item.leave_band()
                except RuntimeError:
                    continue                  # deleted meanwhile
            return
        if self._wants_mirroring():
            from ui.pin_mirror import PinMirror
            for item in pins:
                try:
                    if id(item) not in self._mirrors and item.isVisible():
                        mirror = PinMirror(item)
                        if mirror.start():
                            self._mirrors[id(item)] = mirror
                except RuntimeError:
                    continue                  # deleted meanwhile
        elif self._mirrors:
            self._stop_mirrors()
        self._prune_mirrors()

    def _stop_mirrors(self) -> None:
        for mirror in list(self._mirrors.values()):
            mirror.stop()
        self._mirrors.clear()

    def _prune_mirrors(self) -> None:
        """A pin that has been closed has no mirror any more."""
        alive = {id(i) for i in list(self._open.values()) + list(self._unsaved)}
        for key in [k for k in self._mirrors if k not in alive]:
            self._mirrors.pop(key).stop()

    def _apply_fullscreen_stacking(self, pins=None) -> None:
        """Plain X11: out from under a fullscreen window (see PinItem.set_x11_bypass). One question for all the
        pins, asked once per beat; a no-op everywhere else."""
        try:
            from ui.x11_fullscreen import dynamic_bypass_applies, fullscreen_window_in_front, xwayland_session
            if not (dynamic_bypass_applies() or xwayland_session()):
                return
            # Plain X11, and an X client of a Wayland compositor (XWayland): a managed window is stacked under a
            # fullscreen one in both (measured with Openbox, Mutter and sway), so the pins are override-redirect while one
            # is in front and managed - which is what lets a note be typed into on a Wayland desktop - when it is not.
            # Also while a game is being played, windowed or not: a window manager gives a managed pin the keyboard when it is mapped and
            # when it is clicked, which is the game losing it (and one that pauses when it does, pausing), and a click on a pin that was
            # never active is spent focusing it. An override-redirect pin is left alone by the window manager; a note is given the keyboard
            # when it is clicked INTO (_X11FocusFilter).
            want = bool(fullscreen_window_in_front() or game_is_running())
        except Exception:
            return
        pins = list(self._open.values()) + list(self._unsaved) if pins is None else pins
        for item in pins:
            try:
                item.set_x11_bypass(want)     # (nothing to do for a pin that already is what is wanted)
            except RuntimeError:
                continue                      # deleted meanwhile
        if want and pins and self._x11_focus_filter is None:
            self._x11_focus_filter = _X11FocusFilter()
            QGuiApplication.instance().installEventFilter(self._x11_focus_filter)
        elif not want and self._x11_focus_filter is not None:
            QGuiApplication.instance().removeEventFilter(self._x11_focus_filter)
            self._x11_focus_filter = None
            from ui.x11_fullscreen import stop_typing
            stop_typing()

    def _on_foreground_changed(self, hwnd: int):
        """Something else came to the front — get back above it.

        Not when it is one of the pins themselves: clicking a pin brings it
        forward, and there is nothing to recover from.
        """
        if not (self._open or self._unsaved):
            return
        for item in list(self._open.values()) + list(self._unsaved):
            try:
                if item._mirrored:
                    continue                  # no window of its own: winId() would make one
                if int(item.winId()) == hwnd:
                    return
            except RuntimeError:
                continue
        self._assert_topmost("another window came forward")  # i18n-ignore (log text)
        # And once more when the switch has settled. This fires WHILE Windows
        # is still re-ordering, so the first attempt can land before the game
        # has finished coming forward and be undone a moment later — the log
        # showed exactly that, one False here and True on the next round of
        # the timer, a whole second afterwards. A second pass just after turns
        # that second into a blink.
        QTimer.singleShot(_SETTLE_MS,
                          lambda: self._assert_topmost("just after the switch"))  # i18n-ignore (log text)

    def _sync_topmost_timer(self):
        """Watch, and keep watching, only while there is a pin on screen."""
        wanted = bool(self._open or self._unsaved)
        if wanted:
            if not self._topmost_timer.isActive():
                self._topmost_timer.start()
        else:
            self._topmost_timer.stop()
        if wanted != self._watching:
            self._watching = wanted
            self._foreground.start() if wanted else self._foreground.stop()

    # ── Recents ──────────────────────────────────────────────────────────────

    def recent(self, game_id: str = "") -> list[str]:
        """Recently pinned things for this game, newest first, minus the ones
        that are gone — a menu offering files that no longer exist is worse
        than a short menu.

        A pin with no file yet is here too, holding its place. It is kept only
        while the note it stands for is still on screen: one left behind by a
        crash names nothing, and disappears here without anyone tidying up.
        """
        gid = game_id or self.current_game_id()
        out, seen = [], set()
        for p in self._all_recent().get(gid, []):
            p = str(p)
            if p in seen:
                continue
            seen.add(p)
            if is_new_entry(p):
                if p in self._holding:
                    out.append(p)
                continue
            try:
                if Path(p).is_file():
                    out.append(p)
            except OSError:
                continue
        # Trimming to length takes files, never a place being held: the note
        # it stands for is on screen, and a line it has no room for is a note
        # that cannot be saved from the menu.
        droppable = [p for p in reversed(out) if p not in self._holding]
        for victim in droppable[:max(0, len(out) - _RECENT_MAX)]:
            out.remove(victim)
        return out

    def menu_entry(self, entry: str) -> tuple:
        """One line of the 📌 menu: what to call it, what to say on hover, and
        whether it still needs somewhere to live.

        A pin with no file is named after itself — "New note" — because there
        is no file name to use, and hovering says why there is not.
        """
        item = self._holding.get(str(entry))
        if item is not None:
            return item.display_name(), t("pin.unsaved_hint"), True
        return Path(entry).name, str(entry), False

    def save_now(self, entry: str) -> str:
        """Give the pin behind an unsaved entry a home. Returns where it went,
        or "" if the player thought better of it."""
        item = self._holding.get(str(entry))
        return item.save_as() if item is not None else ""

    def _remember(self, path: str, game_id: str = ""):
        gid = game_id or self.current_game_id()
        store = self._all_recent()
        lst = [p for p in store.get(gid, []) if str(p) != path]
        lst.insert(0, path)
        store[gid] = lst[:_RECENT_MAX]
        get_config().set("pins_recent", store)

    def _hold_place(self, item: "PinItem") -> None:
        """Give a pin with no file a place in the recent list to sit in.

        At the END of the list, not the front. A file is put at the top when
        it is pinned because you have just reached for it and are likely to
        again; a note made from nothing has never been reached for at all,
        and putting it first pushes down every entry that has. It waits at
        the bottom, and keeps that spot when it is saved.
        """
        self._new_count += 1
        key = f"{_NEW_PREFIX}{self._new_count}"
        gid = self.current_game_id()
        item.recent_key, item.recent_gid = key, gid
        # Registered BEFORE it is written down, so that a recent() running in
        # between does not decide the place is stale and skip it.
        self._holding[key] = item
        store = self._all_recent()
        lst = [str(p) for p in store.get(gid, []) if str(p) != key]
        lst.append(key)
        # Trimming drops the oldest FILE. Dropping a held place here would
        # leave a note on screen with no line in the menu — nowhere to save
        # it from, and no sign that it is there.
        while len(lst) > _RECENT_MAX:
            victim = next((p for p in reversed(lst) if not is_new_entry(p)), None)
            if victim is None:
                break
            lst.remove(victim)
        store[gid] = lst
        get_config().set("pins_recent", store)

    def _take_place(self, key: str, path: str, gid: str) -> None:
        """Turn a held place into the real path, where it already sits.

        In place rather than dropped and re-added: the entry has been in the
        menu since the note was made, and having it jump to the top the moment
        it is saved would read as a different thing arriving.
        """
        store = self._all_recent()
        # If that file was already in the list, its old entry goes: two lines
        # for one file is how a menu of twelve becomes a menu of six.
        lst = [p for p in store.get(gid, []) if str(p) != path]
        out = [path if str(p) == key else str(p) for p in lst]
        if path not in out:
            out.insert(0, path)
        store[gid] = out[:_RECENT_MAX]
        get_config().set("pins_recent", store)

    def _free_place(self, key: str, gid: str) -> None:
        """The note was closed without ever being saved: its place goes too."""
        store = self._all_recent()
        if gid in store:
            store[gid] = [p for p in store[gid] if str(p) != key]
            get_config().set("pins_recent", store)

    @staticmethod
    def _carry_over(key: str, path: str) -> None:
        """Move what was filed under a held place onto the file it now has.

        Where the note sits and how faded it is were recorded against the
        place it was holding. Saving it must not put it back in the corner
        it started in.
        """
        cfg = get_config()
        for name in ("pins_geometry", "pins_opacity", "pins_zoom"):
            store = dict(cfg.get(name, {}) or {})
            if key in store:
                store[path] = store.pop(key)
                cfg.set(name, store)

    @staticmethod
    def _forget_stored(key: str) -> None:
        cfg = get_config()
        for name in ("pins_geometry", "pins_opacity", "pins_zoom"):
            store = dict(cfg.get(name, {}) or {})
            if store.pop(key, None) is not None:
                cfg.set(name, store)

    def _drop_stale_places(self) -> None:
        """Places left behind by a session that ended badly.

        Nothing is held at start-up, so every one of these is from a note that
        never got saved and never will be. recent() already hides them; this
        is what stops them taking up room in the list for good.
        """
        try:
            store = self._all_recent()
            cleaned = {g: [p for p in lst if not is_new_entry(p)]
                       for g, lst in store.items()}
            if cleaned != store:
                get_config().set("pins_recent", cleaned)
            cfg = get_config()
            for name in ("pins_geometry", "pins_opacity", "pins_zoom"):
                kept = {k: v for k, v in (cfg.get(name, {}) or {}).items()
                        if not is_new_entry(k)}
                if kept != (cfg.get(name, {}) or {}):
                    cfg.set(name, kept)
        except Exception as e:
            logger.debug(f"Could not tidy the recent pins: {e}")

    def forget(self, path: str, game_id: str = ""):
        """Drop a file from this game's list. The file itself is untouched —
        removing a shortcut must never delete what it points at."""
        # Also take it off the screen: removing an entry while the thing it
        # names stays pinned would leave a pin with no way back to it.
        self.unpin(path)
        gid = game_id or self.current_game_id()
        store = self._all_recent()
        store[gid] = [p for p in store.get(gid, []) if str(p) != path]
        get_config().set("pins_recent", store)
        self.changed.emit()

    # ── Open / close ─────────────────────────────────────────────────────────

    def is_open(self, path: str) -> bool:
        return str(path) in self._open or str(path) in self._holding

    def all_items(self) -> list:
        """Every pin that exists: open from a file, or not saved yet."""
        return list(self._open.values()) + list(self._unsaved)

    def open_paths(self) -> list[str]:
        return list(self._open)

    def unsaved_pins(self) -> list[PinItem]:
        return list(self._unsaved)

    def pin(self, path: str) -> "PinItem | None":
        path = str(path)
        if path in self._open:
            item = self._open[path]
            item.raise_()
            return item
        # An entry standing for a pin with no file: the pin is already on
        # screen, so this brings it forward. There is nothing to open.
        item = self._holding.get(path)
        if item is not None:
            item.raise_()
            item.assert_topmost()
            return item
        if not is_pinnable(path):
            logger.info(f"Not pinnable: {path}")
            return None
        item = PinItem(path)
        item.closed.connect(self._on_item_closed)
        self._open[path] = item
        self._apply_fullscreen_stacking([item])          # (before it is shown: a window shown managed and made override-redirect after was focused once)
        item.show()
        item.raise_()
        item.assert_topmost()
        self._apply_fullscreen_stacking()
        self._sync_topmost_timer()
        self._remember(path)
        self._save_open()
        self.changed.emit()
        return item

    def new_capture(self, pixmap) -> "PinItem | None":
        """A piece of the screen, pinned straight away. Like a new note it has
        no file yet: saving gives it one, otherwise it goes with the session.
        """
        if pixmap is None or pixmap.isNull():
            return None
        return self._new_unsaved(PinItem("", pixmap=pixmap))

    def new_note(self) -> PinItem:
        """An empty note with nowhere to live yet. It becomes a real pin the
        moment the player saves it, and is dropped if they never do."""
        return self._new_unsaved(PinItem(""))

    def _new_unsaved(self, item: PinItem) -> PinItem:
        item.closed.connect(self._on_item_closed)
        item.saved.connect(lambda p, it=item: self._on_item_saved(p, it))
        # By identity, not by state: `closed` is emitted from inside
        # closeEvent, where the widget still reports itself visible, so
        # filtering the list on isVisible() would never drop anything.
        item.closed.connect(lambda _p, it=item: self._drop_unsaved(it))
        self._unsaved.append(item)
        # In the recent list from the moment it exists, like any other pin.
        self._hold_place(item)
        self._apply_fullscreen_stacking([item])          # (before it is shown: a window shown managed and made override-redirect after was focused once)
        item.show()
        item.raise_()
        item.assert_topmost()
        self._apply_fullscreen_stacking()
        self._sync_topmost_timer()
        self.changed.emit()
        return item

    def _on_item_saved(self, path: str, item: PinItem):
        if item in self._unsaved:
            self._unsaved.remove(item)
        # It may already be listed under a path it has just moved away from —
        # a note whose own file could not be written, saved somewhere that
        # works. Two entries for one pin would leave the old one behind.
        for stale in [k for k, v in self._open.items()
                      if v is item and k != str(path)]:
            self._open.pop(stale, None)
        # Saved over a file that is already pinned: the earlier pin of it is the same file, shown twice, and it stayed on screen with
        # nothing in the list to close it by. It is closed - without writing its words back over what has just been saved.
        previous = self._open.pop(str(path), None)
        if previous is not None and previous is not item:
            logger.info("Pins: %s was saved over a file that is already pinned - the earlier pin of it is closed", Path(path).name)
            try:
                previous._edited = False
                timer = getattr(previous, "_save_timer", None)
                if timer is not None:
                    timer.stop()
                previous.close()
            except RuntimeError:
                pass
        self._open[str(path)] = item
        key, gid = item.recent_key, item.recent_gid
        if key:
            # The place it has been holding becomes the file it now has, so
            # the entry that was there all along is the one you can reopen.
            self._holding.pop(key, None)
            item.recent_key, item.recent_gid = "", ""
            self._take_place(key, str(path), gid)
            self._carry_over(key, str(path))
        else:
            self._remember(str(path))
        item.save_geometry()
        self._save_open()
        self.changed.emit()
        if win_band_hook.installed():
            from ui.exclusive_session import log_windows
            QTimer.singleShot(1500, lambda: log_windows("1.5 s after a pin was saved"))

    def unpin(self, path: str):
        item = self._open.get(str(path)) or self._holding.get(str(path))
        if item is not None:
            item.close()

    def toggle(self, path: str):
        if self.is_open(path):
            self.unpin(path)
        else:
            self.pin(path)

    def close_all(self):
        """The player closing every pin: they are meant to stay closed."""
        for item in list(self._open.values()) + list(self._unsaved):
            item.close()

    def discard_unsaved(self):
        """The session is over. A note never saved anywhere has nowhere to be
        restored from, so it simply goes."""
        for item in list(self._unsaved):
            item.close()

    def shutdown(self):
        """SaveSync quitting. Take the windows down but KEEP the list of what
        was on screen — otherwise every pin would report itself closed on the
        way out and there would never be anything to restore.
        """
        self._shutting_down = True
        try:
            for item in list(self._unsaved):
                item.close()
            for item in list(self._open.values()):
                item.close()
        finally:
            self._shutting_down = False

    def _drop_unsaved(self, item: PinItem):
        if item in self._unsaved:
            self._unsaved.remove(item)
            # Never saved anywhere, so there is nothing to come back to: the
            # place it held goes with it, at the end of the session as well.
            if item.recent_key:
                self._holding.pop(item.recent_key, None)
                self._free_place(item.recent_key, item.recent_gid)
                self._forget_stored(item.recent_key)
                item.recent_key, item.recent_gid = "", ""
            self._sync_topmost_timer()
            self._prune_mirrors()
            self.changed.emit()

    def _on_item_closed(self, path: str):
        if path == _UNSAVED:
            return          # handled by _drop_unsaved, which knows which one
        self._open.pop(str(path), None)
        if not self._shutting_down:
            self._save_open()
        self._sync_topmost_timer()
        self._prune_mirrors()
        self.changed.emit()

    # ── Session persistence ──────────────────────────────────────────────────

    def _all_open(self) -> dict:
        raw = get_config().get("pins_open", {}) or {}
        if isinstance(raw, list):          # pre-per-game shape
            return {_GENERAL: [str(p) for p in raw]}
        return {str(k): list(v) for k, v in raw.items() if isinstance(v, list)}

    def _save_open(self):
        """What is on screen, recorded against the game it belongs to.

        Per game like the recents: a map pinned for one game reappearing over
        a different one is exactly the noise the per-game split removes.
        """
        store = self._all_open()
        store[self._session_game or self.current_game_id()] = list(self._open)
        get_config().set("pins_open", store)

    def restore_open(self, game_id: str = ""):
        """Re-pin what was on screen for this game. A pin that has since been
        deleted or moved is simply skipped."""
        gid = game_id or _GENERAL
        prev, self._session_game = self._session_game, gid
        try:
            for path in self._all_open().get(gid, []):
                if is_pinnable(path):
                    self.pin(str(path))
        finally:
            self._session_game = prev if gid == _GENERAL else gid

    def stow_game(self, game_id: str):
        """The game is over: take its pins off the screen but KEEP the record,
        so starting it again brings them back exactly as they were."""
        self._shutting_down = True
        try:
            for item in list(self._open.values()):
                item.close()
        finally:
            self._shutting_down = False
            self._session_game = ""

    def update_locale(self):
        for item in list(self._open.values()) + list(self._unsaved):
            item.update_locale()


class PinMenuRow(QWidget):
    """One line of the 📌 menu: pin marker, file name, and a bin.

    A plain QAction cannot carry a control of its own on the right, and the
    bin has to be reachable without pinning the file first.
    """

    activated = Signal(str)
    removed = Signal(str)
    save = Signal(str)

    def __init__(self, path: str, is_open: bool, parent=None,
                 label: str = "", tip: str = "", can_save: bool = False):
        super().__init__(parent)
        self._path = path
        self.setObjectName("pin_row")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 4, 2)
        row.setSpacing(6)

        mark = QLabel("📌" if is_open else " ")
        mark.setObjectName("pin_row_mark")
        mark.setFixedWidth(scaled(16, self))
        row.addWidget(mark)

        # A pin with no file has no file name to go by, so the caller says
        # what to call it and what to say on hover.
        name = ElidedLabel(label or Path(path).name)
        name.setObjectName("pin_row_name")
        name.setToolTip(tip or path)
        name.setMinimumWidth(scaled(150, self))
        row.addWidget(name, 1)

        # A note with nowhere to live yet can be given one from here, without
        # having to find its window first. Same 💾 it wears itself, in the
        # same place relative to the bin, so the two read as one gesture.
        if can_save:
            save_btn = QPushButton("💾")
            save_btn.setObjectName("pin_icon_btn")
            save_btn.setFixedSize(scaled(18, self), scaled(18, self))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setToolTip(t("pin.save"))
            save_btn.clicked.connect(lambda: self.save.emit(self._path))
            row.addWidget(save_btn)

        bin_btn = QPushButton("🗑")
        bin_btn.setObjectName("pin_icon_btn")
        bin_btn.setFixedSize(scaled(18, self), scaled(18, self))
        bin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bin_btn.setToolTip(t("pin.forget"))
        bin_btn.clicked.connect(lambda: self.removed.emit(self._path))
        row.addWidget(bin_btn)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        # Anywhere but the bin toggles the pin.
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._path)
        super().mouseReleaseEvent(event)


_manager: "PinManager | None" = None


def _begin_pins_session() -> None:
    """Pins over a game in exclusive fullscreen can be typed into, and show no tooltips (ui.exclusive_session). It ends by
    itself when no pin is left in the higher band."""
    try:
        from ui.exclusive_session import get_session
        get_session().begin("pins", get_pin_manager()._any_banded, windows=False)
    except Exception:
        logger.debug("pins: could not begin the exclusive session", exc_info=True)


def get_pin_manager() -> PinManager:
    global _manager
    if _manager is None:
        _manager = PinManager()
    return _manager
