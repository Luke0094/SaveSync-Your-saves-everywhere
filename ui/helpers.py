"""
SaveSync - Shared UI helpers
Small utilities used by multiple pages, dialogs and widgets.
"""
import logging
import os
import platform
import subprocess
from collections import OrderedDict

import shiboken6 as sip
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QFontMetrics
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QSizePolicy, QStyle

logger = logging.getLogger(__name__)


class ScreenSignalMixin:
    """Screen add/remove/geometry tracking for frameless always-on-top
    widgets (overlay, blur modal).

    The subclass implements ``_on_screen_changed()`` (re-position/resize)
    and calls ``_connect_screen_changes()`` once after construction;
    ``_screen_signals_cleanup()`` (call it from cleanup/deleteLater)
    disconnects everything so no signal ever fires into a destroyed widget.
    """

    _SCREEN_SIGNALS = ("geometryChanged", "availableGeometryChanged")

    def _connect_screen_changes(self):
        app = QApplication.instance()
        if not app:
            return
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        for screen in app.screens():
            self._connect_one_screen(screen)

    def _connect_one_screen(self, screen):
        for sig in self._SCREEN_SIGNALS:
            getattr(screen, sig).connect(self._on_screen_changed)

    def _disconnect_one_screen(self, screen):
        # Broad except: a removed QScreen may be a partially-deleted C++ object
        for sig in self._SCREEN_SIGNALS:
            try:
                getattr(screen, sig).disconnect(self._on_screen_changed)
            except (RuntimeError, TypeError, Exception):
                pass

    def _on_screen_added(self, screen):
        self._connect_one_screen(screen)
        self._on_screen_changed()

    def _on_screen_removed(self, screen):
        self._disconnect_one_screen(screen)
        self._on_screen_changed()

    def _screen_signals_cleanup(self):
        app = QApplication.instance()
        if not app:
            return
        try:
            app.screenAdded.disconnect(self._on_screen_added)
        except (RuntimeError, TypeError):
            pass
        try:
            app.screenRemoved.disconnect(self._on_screen_removed)
        except (RuntimeError, TypeError):
            pass
        for screen in app.screens():
            self._disconnect_one_screen(screen)

    def _on_screen_changed(self, *_args):
        raise NotImplementedError


def safe_widget(w) -> bool:
    """True if *w* still exists as a live C++ Qt object."""
    try:
        return w is not None and sip.isValid(w)
    except Exception:
        return w is not None


def open_in_file_manager(target) -> None:
    """Open a folder (or a file's location) in the system file manager."""
    try:
        if platform.system() == "Windows":
            os.startfile(str(target))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        logger.warning(f"Could not open folder '{target}': {e}")


def game_is_running() -> bool:
    """True while SaveSync is tracking a live game session.

    The one place this question is answered, because more than one thing acts
    on it — the overlay and every pin — and they must not disagree about when
    the pointer is SaveSync's business.
    """
    try:
        from core.monitor import get_monitor
        return bool(get_monitor().currently_playing())
    except Exception:
        return False


class SystemCursor:
    """Make the mouse pointer usable while a game is hiding it.

    Plenty of games hide the pointer outright and confine it to their own
    window. Both are done from the game's process, and neither can be undone
    for the game from outside it — but neither has to be. Two things are
    enough to make an overlay clickable:

    - release the confinement (``ClipCursor(NULL)``), so the pointer can
      actually travel to a window that is not the game's;
    - raise THIS process's cursor display counter, which is what Windows
      consults while the pointer is over one of our windows.

    Every increment is counted so it can be undone exactly: leaving the
    counter raised would leave a pointer sitting over the game after the
    overlay is gone, which is precisely the thing the game turned off.

    More than one thing needs the pointer, and they overlap: the overlay is
    one, a note being typed into is another, a pin mid-drag a third. So this
    counts HOLDERS, not calls — the pointer comes up for the first hold and
    goes down only when the last one lets go. A single flag would have the
    overlay's auto-hide yank the pointer out from under a note the player was
    still writing in.

    A no-op away from Windows: X11/Wayland/macOS have no equivalent global
    hide for another process to undo.
    """

    _raised = 0
    _holders: set = set()

    @classmethod
    def hold(cls, key: str) -> None:
        """Keep the pointer up until *key* lets go. Re-holding is harmless."""
        cls._holders.add(key)
        cls._raise()

    @classmethod
    def release(cls, key: str) -> None:
        cls._holders.discard(key)
        if not cls._holders:
            cls._lower()

    @classmethod
    def release_all(cls) -> None:
        cls._holders.clear()
        cls._lower()

    @classmethod
    def held_by(cls) -> set:
        return set(cls._holders)

    @classmethod
    def _raise(cls) -> None:
        if platform.system() != "Windows" or cls._raised:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.ClipCursor(None)
            # ShowCursor returns the NEW counter; visible at >= 0. Every call
            # is counted, INCLUDING the one that reaches 0 — counting only the
            # calls that stayed negative would under-record by one and leave
            # the counter a notch higher after every show/restore cycle. The
            # bound stops a runaway loop if the call ever stops incrementing.
            raised = 0
            while raised < 16:
                counter = user32.ShowCursor(True)
                raised += 1
                if counter >= 0:
                    break
            cls._raised = raised
        except Exception as e:
            logger.debug(f"Could not raise the system cursor: {e}")

    @classmethod
    def _lower(cls) -> None:
        if platform.system() != "Windows" or not cls._raised:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            for _ in range(cls._raised):
                user32.ShowCursor(False)
        except Exception as e:
            logger.debug(f"Could not restore the system cursor: {e}")
        finally:
            cls._raised = 0


def above_foreground(hwnd: int) -> bool:
    """Whether *hwnd* really sits above the window that is in front.

    "I asked for topmost" and "I am on top" are different claims. A game in
    borderless fullscreen carries the always-on-top flag too, so both windows
    can hold it while only one of them is actually visible.
    """
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetForegroundWindow.restype = wintypes.HWND
        front = user32.GetForegroundWindow()
        if not front or front == hwnd:
            return True
        walk = hwnd
        # Bounded: the chain is a few dozen windows on any real desktop, and
        # a loop that cannot end is worse than an answer of "no".
        for _ in range(500):
            walk = user32.GetWindow(walk, 2)          # GW_HWNDNEXT
            if not walk:
                return False
            if walk == front:
                return True
        return False
    except Exception as e:
        logger.debug(f"Could not read the window order: {e}")
        return True


# With SAVESYNC_TRACE=1 every attempt to put a window back on top says what
# it found and what it achieved. Read once: the check costs a name lookup
# rather than an environment read per round.
TRACE_Z = os.environ.get("SAVESYNC_TRACE") == "1"


def z_report(widget) -> str:
    """Where a window actually sits right now, in words, for the trace log.

    Used by the overlay and the pins alike, because they share the mechanism
    and therefore share its failure: both can hold the always-on-top flag and
    still be underneath a game that holds it too.
    """
    if platform.system() != "Windows":
        return "not Windows"
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowLongW.restype = wintypes.DWORD
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        hwnd = int(widget.winId())
        front = user32.GetForegroundWindow()
        topmost = bool(user32.GetWindowLongW(hwnd, -20) & 0x00000008)
        name = ctypes.create_unicode_buffer(96)
        user32.GetWindowTextW(front, name, 96)
        return (f"topmost={topmost} above_foreground={above_foreground(hwnd)} "
                f"foreground={name.value[:48]!r}")
    except Exception as e:
        return f"z-order unreadable: {e}"


def popup_is_open() -> bool:
    """Whether one of our own menus is up.

    Re-ordering a window to the top puts it over any menu that is open, and
    the overlay does that every second while the 📌 menu opens inside the
    overlay's own rectangle — so the menu you just asked for disappears
    behind it. Nothing needs raising while a menu is up: a menu is only ever
    open because the player is looking at SaveSync, not at the game.
    """
    try:
        from PySide6.QtWidgets import QApplication
        return QApplication.activePopupWidget() is not None
    except Exception:
        return False


def force_topmost(widget) -> None:
    """Force a frameless always-on-top widget above all windows without
    stealing focus — shared by the overlay, the pins and the blur modal.

    - Windows: Win32 SetWindowPos with HWND_TOPMOST + NOACTIVATE, plus
      WS_EX_TOOLWINDOW so the widget never shows in taskbar/alt-tab.
    - Linux/macOS: Qt raise_() re-stacks the widget (X11 sends
      _NET_WM_STATE_ABOVE; on Wayland fullscreen it's a compositor no-op;
      macOS raises the NSWindow level).

    Asking for HWND_TOPMOST does nothing to a window that ALREADY carries the
    flag — and a borderless game carries it too. So the window keeps the flag,
    the request succeeds, and it stays underneath the game all the same; the
    trace showed exactly that, `topmost=True above_foreground=False` round
    after round. HWND_TOP is what re-orders it, and it does so from INSIDE
    the always-on-top group.

    Not by leaving the group and re-entering it, which was the first attempt:
    that works too, but for the moment it takes, the window is below
    everything. With an overlay on one timer and a pin on another, both
    stepping out and back in, the game — which never moves — ends up above
    both. Staying in the group throughout leaves no such moment.
    """
    if platform.system() == "Windows":
        try:
            import ctypes
            hwnd = int(widget.winId())
            user32 = ctypes.windll.user32
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_NOOWNERZORDER = 0x0200
            HWND_TOPMOST = -1
            HWND_TOP = 0
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
            if not above_foreground(hwnd):
                user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags)
            extended_style = user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, extended_style | 0x00000080)  # WS_EX_TOOLWINDOW
        except Exception as e:
            logger.debug(f"force_topmost failed: {e}")
    else:
        try:
            widget.raise_()
        except Exception:
            pass


def load_pixmap_any(path: str) -> QPixmap:
    """QPixmap(path) with a PIL fallback for formats Qt has no plugin for
    (notably .avif — decoded via pillow-avif-plugin). Returns a null pixmap
    when neither decoder can read the file."""
    px = QPixmap(path)
    if not px.isNull():
        return px
    try:
        try:
            import pillow_avif  # noqa: F401 — registers the AVIF codec in PIL
        except ImportError:
            pass
        from PIL import Image as _PILImage
        pil_img = _PILImage.open(path)
        if pil_img.mode != "RGBA":
            pil_img = pil_img.convert("RGBA")
        w, h = pil_img.size
        data = pil_img.tobytes("raw", "RGBA")
        qimg = QImage(data, w, h, QImage.Format_RGBA8888)
        px = QPixmap.fromImage(qimg)
    except Exception as e:
        logger.debug(f"PIL fallback failed for '{path}': {e}")
    return px


# Decoded images, kept so that going back to one costs nothing.
#
# Decoding is what the wait is: measured on this library, a large cover takes
# 342–487 ms to decode and 13–17 ms to scale, so re-reading a picture the
# viewer has already shown is nearly all of the delay for none of the work.
# Held as a small ring rather than a big one because these are whole
# uncompressed images — see viewer_pixmap for the size they are held at.
_VIEW_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
# Bounded in BYTES, not in pictures: one of these is as big as the screen —
# 33 MB at 4K — so a count that looked modest would be a quarter of a
# gigabyte. This holds roughly the last five, which is what browsing back and
# forth actually touches.
_VIEW_CACHE_BYTES = 192 * 1024 * 1024


def _pixmap_bytes(px: QPixmap) -> int:
    return max(1, px.width() * px.height() * (px.depth() // 8 or 4))


# Thumbnails are kept separately and by count: they are a few kilobytes each,
# so the strip of them costs less than one of the pictures above.
_THUMB_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_THUMB_CACHE_MAX = 128


def _screen_pixel_bounds() -> tuple:
    """The largest screen's size in real pixels — no image needs more."""
    app = QApplication.instance()
    if app is None:
        return 3840, 2160
    w = h = 0
    for s in app.screens():
        g, r = s.geometry(), s.devicePixelRatio() or 1.0
        w = max(w, int(g.width() * r))
        h = max(h, int(g.height() * r))
    return max(1280, w), max(720, h)


def viewer_pixmap(path: str) -> QPixmap:
    """A decoded image ready to show, cached, and never bigger than a screen.

    Two costs are cut, and only one of them is the cache. A picture larger
    than the display cannot show more than the display holds, so it is
    brought down to that ONCE, on the way in — which is also what keeps the
    cache affordable: a 6000×4544 photograph is 109 MB held whole and 33 MB
    held at screen size, and the viewer cannot tell the difference.

    Keyed on the file's timestamp and length as well as its path, so a
    picture that was replaced on disk is decoded afresh.
    """
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None:
        hit = _VIEW_CACHE.get(key)
        if hit is not None:
            _VIEW_CACHE.move_to_end(key)
            return hit

    px = load_pixmap_any(path)
    if px.isNull():
        return px
    max_w, max_h = _screen_pixel_bounds()
    if px.width() > max_w or px.height() > max_h:
        px = px.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    if key is not None:
        _VIEW_CACHE[key] = px
        total = sum(_pixmap_bytes(p) for p in _VIEW_CACHE.values())
        while len(_VIEW_CACHE) > 1 and total > _VIEW_CACHE_BYTES:
            _k, dropped = _VIEW_CACHE.popitem(last=False)
            total -= _pixmap_bytes(dropped)
    return px


def thumbnail_pixmap(path: str, w: int, h: int) -> QPixmap:
    """A small, cached copy of *path*, at the screen's real pixel count.

    Kept apart from viewer_pixmap because the sizes are not comparable: a
    strip of these costs less than one full picture, so they can all be held
    while the big ones cannot. The decode is asked to produce a reduced image
    where the format allows — JPEG halves natively, PNG cannot, so this is a
    saving on some files and merely harmless on the rest.
    """
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size, w, h)
    except OSError:
        key = None
    if key is not None:
        hit = _THUMB_CACHE.get(key)
        if hit is not None:
            _THUMB_CACHE.move_to_end(key)
            return hit

    dpr = display_scale()
    need_w, need_h = max(1, int(w * dpr)) * 2, max(1, int(h * dpr)) * 2
    px = QPixmap()
    try:
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImageReader
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        src = reader.size()
        if src.isValid() and src.width() > 0:
            sw, sh, shift = src.width(), src.height(), 0
            while shift < 3 and sw // 2 >= need_w and sh // 2 >= need_h:
                sw //= 2
                sh //= 2
                shift += 1
            if shift:
                reader.setScaledSize(QSize(sw, sh))
            img = reader.read()
            if not img.isNull():
                px = QPixmap.fromImage(img)
    except Exception as e:
        logger.debug(f"Reduced decode failed for '{path}': {e}")
    if px.isNull():
        px = load_pixmap_any(path)
    if px.isNull():
        return px

    out = scaled_for_screen(px, w, h)
    if key is not None:
        _THUMB_CACHE[key] = out
        while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
            _THUMB_CACHE.popitem(last=False)
    return out


def display_scale() -> float:
    """How many real pixels the screen puts in one of Qt's.

    1.0 on an ordinary display, 1.5 at Windows' 150%, 2.0 on a Mac retina.

    The LARGEST screen factor is taken, not the current one: a window can be
    dragged to another monitor, and an image with more detail than the screen
    it lands on merely shrinks, while one with less is visibly rough.
    """
    app = QApplication.instance()
    if app is None:
        return 1.0
    try:
        return max([s.devicePixelRatio() for s in app.screens()] or [1.0])
    except Exception:
        return 1.0


def scaled_for_screen(px: QPixmap, w: int, h: int,
                      mode=Qt.AspectRatioMode.KeepAspectRatio) -> QPixmap:
    """*px* fitted to a w×h area, at the screen's real pixel count.

    Qt's coordinates are not pixels on a display that magnifies: fitting an
    image to them throws away the detail the magnification then has to invent
    back, which is what makes a picture look soft in a small frame and fine
    filling the screen — the bigger the frame, the less was thrown away. The
    result is built at the real count and stamped with the scale, so it is
    drawn one pixel to one pixel while still occupying the w×h asked for.
    """
    if px.isNull():
        return px
    dpr = display_scale()
    out = px.scaled(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))),
                    mode, Qt.TransformationMode.SmoothTransformation)
    out.setDevicePixelRatio(dpr)
    return out


class ForegroundWatcher(QObject):
    """Tells you the moment another window comes to the front.

    A window that has to stay above a running game cannot wait for a poll to
    come round. A game takes the foreground back on every alt-tab, every
    loading screen, every time it is clicked, and it puts itself above
    everything as it does — so a window re-asserting itself only once a
    second spends most of that second behind the game. Re-asserting at the
    instant the foreground changes is what the overlay does, and it is why
    the overlay stays up where a timer alone does not.

    Windows only: elsewhere the window manager honours "stays on top" without
    help, and start() simply does nothing. One hook is shared by everyone who
    asks for it, and it is taken down when the last of them stops.
    """

    changed = Signal(int)          # the window that came forward

    _instance = None

    @classmethod
    def instance(cls) -> "ForegroundWatcher":
        if cls._instance is None:
            cls._instance = ForegroundWatcher()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._hook = None
        self._proc = None
        self._users = 0

    def start(self):
        self._users += 1
        if self._users == 1:
            self._install()

    def stop(self):
        self._users = max(0, self._users - 1)
        if self._users == 0:
            self._remove()

    def _install(self):
        if platform.system() != "Windows" or self._hook is not None:
            return
        try:
            import ctypes
            from ctypes import wintypes
            from PySide6.QtCore import QTimer

            EVENT_SYSTEM_FOREGROUND = 0x0003
            WINEVENT_OUTOFCONTEXT = 0x0000
            WINEVENTPROC = ctypes.WINFUNCTYPE(
                None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
                ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)

            def _fired(_hook, _event, hwnd, _obj, _child, _tid, _time):
                # Back to the event loop before touching anything Qt owns:
                # this runs from inside a Windows message, not from ours.
                try:
                    QTimer.singleShot(0, lambda h=int(hwnd or 0):
                                      self.changed.emit(h))
                except Exception:
                    pass

            self._proc = WINEVENTPROC(_fired)      # a ref, or it is collected
            self._hook = ctypes.windll.user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                self._proc, 0, 0, WINEVENT_OUTOFCONTEXT)
            if not self._hook:
                self._hook, self._proc = None, None
        except Exception as e:
            logger.debug(f"Could not watch the foreground window: {e}")
            self._hook, self._proc = None, None

    def _remove(self):
        if self._hook is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.UnhookWinEvent(self._hook)
        except Exception as e:
            logger.debug(f"Could not stop watching the foreground: {e}")
        finally:
            self._hook, self._proc = None, None


class TopmostPinMixin:
    """Keep a dialog above a fullscreen game, without a focus war.

    A plain Qt dialog cannot surface itself over a game that keeps reclaiming
    the foreground — raise_() only re-stacks our own windows. Re-asserting
    HWND_TOPMOST on a timer does, and SWP_NOACTIVATE keeps it from stealing
    the game's focus. Unlike the overlay this deliberately does NOT set
    WS_EX_TOOLWINDOW, so the dialog stays in the taskbar and alt-tab.

    Shared by the save-confirmation panel and the sync-conflict dialog: both
    are decisions the user has to make while a game may be in front.
    """

    _TOPMOST_INTERVAL_MS = 1000

    def start_topmost_pin(self):
        """Begin re-pinning (Windows only — elsewhere the window manager
        honours WindowStaysOnTopHint on its own)."""
        import platform
        from PySide6.QtCore import QTimer
        if platform.system() != "Windows":
            return
        timer = getattr(self, "_topmost_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(self._TOPMOST_INTERVAL_MS)
            timer.timeout.connect(self.repin_topmost)
            self._topmost_timer = timer
        if not timer.isActive():
            timer.start()
        self.repin_topmost()

    def repin_topmost(self):
        try:
            import ctypes
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
            SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0010, 0x0200
            HWND_TOPMOST = -1
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        except Exception:
            pass

    def stop_topmost_pin(self):
        timer = getattr(self, "_topmost_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass


def apply_game_friendly_flags(dialog):
    """Window recipe for a dialog that may open over a running game: on top
    where the platform supports it, and shown without stealing focus."""
    import platform
    from PySide6.QtCore import Qt
    flags = (Qt.WindowType.Dialog
             | Qt.WindowType.CustomizeWindowHint
             | Qt.WindowType.WindowTitleHint
             | Qt.WindowType.WindowCloseButtonHint)
    if platform.system() == "Windows":
        flags |= Qt.WindowType.WindowStaysOnTopHint
    dialog.setWindowFlags(flags)
    dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)


class ElidedLabel(QLabel):
    """One-line label that shortens its text in the MIDDLE to fit, keeping the
    full value in the tooltip.

    Paths are the wrong shape for word wrap: they are one long token, so a
    wrapped label grows to three or four lines and — inside a list of a few
    hundred rows — both buries the rest of the row and makes the list
    unreadable. Eliding in the middle keeps the two ends that identify a path
    (the drive and the leaf) while the whole thing stays available on hover.
    """

    def __init__(self, text: str = "", parent=None, *, own_tooltip: bool = True):
        super().__init__(parent)
        self._full = ""
        # False when a parent row owns the tip (hover on stretch/engine still
        # shows the full title; this label alone is only as wide as its text).
        self._own_tooltip = own_tooltip
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFullText(text)

    def setFullText(self, text: str):
        self._full = text or ""
        self._apply_elide()
        self.updateGeometry()

    def fullText(self) -> str:
        return self._full

    def sizeHint(self):
        from PySide6.QtCore import QSize
        metrics = QFontMetrics(self.font())
        # Slack: elidedText() is stricter than horizontalAdvance, and QSS
        # fonts may polish after the first hint.
        return QSize(metrics.horizontalAdvance(self._full) + 8, metrics.height())

    def minimumSizeHint(self):
        from PySide6.QtCore import QSize
        metrics = QFontMetrics(self.font())
        return QSize(40, metrics.height())

    def _apply_elide(self):
        metrics = QFontMetrics(self.font())
        # Before the first layout pass width is 0. Using a fake 40px budget
        # here made short names elide (file2→file2....mzsave) while a longer
        # neighbour that later got a real geometry stayed whole — looking
        # like a character-limit bug when there was ample row space.
        if self.width() <= 0:
            super().setText(self._full)
            self._sync_tooltip(elided=False)
            return
        width = max(1, self.width() - 2)
        full_w = metrics.horizontalAdvance(self._full)
        if full_w <= width:
            super().setText(self._full)
            self._sync_tooltip(elided=False)
            return
        elided = metrics.elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, width)
        # ElideMiddle can replace a couple of letters with "…" and end up
        # wider than the original ("Love" → "…ve"). Keep the full text then.
        if (elided == self._full
                or metrics.horizontalAdvance(elided) >= full_w):
            super().setText(self._full)
            self._sync_tooltip(elided=False)
            return
        super().setText(elided)
        self._sync_tooltip(elided=True)

    def _sync_tooltip(self, elided: bool):
        if not self._own_tooltip:
            self.setToolTip("")
            return
        # Full value only when shortened — otherwise the tip repeats what
        # is already on screen.
        self.setToolTip(self._full if elided else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()


class ElidedCheckBox(QCheckBox):
    """Checkbox whose own label is a path: middle-elided to fit, full value in
    the tooltip.

    Same reasoning as ElidedLabel, applied where the path IS the label. A
    plain QCheckBox sizes itself to its entire text, so one long path widens
    the row past the viewport and pushes the buttons that follow it (open,
    delete) out behind a horizontal scrollbar. Elision here is purely visual:
    callers keep the real path themselves and must never read it back off the
    label.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self._tooltip_suffix = ""
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFullText(text)

    def setFullText(self, text: str):
        self._full = text or ""
        self.setToolTip("\n".join(p for p in (self._full, self._tooltip_suffix) if p))
        self._apply_elide()

    def fullText(self) -> str:
        return self._full

    def setTooltipSuffix(self, suffix: str):
        """Extra explanatory line kept under the full value in the tooltip."""
        self._tooltip_suffix = suffix or ""
        self.setFullText(self._full)

    def _label_offset(self) -> int:
        """Width the indicator and its spacing take away from the text."""
        style = self.style()
        return (style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, self)
                + style.pixelMetric(QStyle.PixelMetric.PM_CheckBoxLabelSpacing, None, self)
                + 4)

    def _apply_elide(self):
        metrics = QFontMetrics(self.font())
        if self.width() <= 0:
            super().setText(self._full)
            return
        width = max(1, self.width() - self._label_offset())
        full_w = metrics.horizontalAdvance(self._full)
        if full_w <= width:
            super().setText(self._full)
            return
        elided = metrics.elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, width)
        if (elided == self._full
                or metrics.horizontalAdvance(elided) >= full_w):
            super().setText(self._full)
            return
        super().setText(elided)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()
