"""
SaveSync - Shared UI helpers
Small utilities used by multiple pages, dialogs and widgets.
"""
import logging
import os
import platform
import subprocess
from collections import OrderedDict
from typing import NamedTuple

import shiboken6 as sip
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFontMetrics, QImage, QPainter, QPen, QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QLabel, QSizePolicy, QStyle, QWidget,
)

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


class _SoftwareCursor(QWidget):
    """Arrow drawn by us — survives games that blank the OS cursor every frame.

    ``ShowCursor`` / ``SetCursor`` are per-thread: Unity hiding the pointer in
    its own process cannot be undone from SaveSync. A tiny topmost, input-
    transparent window that follows ``QCursor.pos()`` is the reliable fallback.

    Size follows the screen's logical DPI (Windows display scaling) from a
    compact 96‑DPI baseline — slightly smaller than a classic system arrow.
    """

    # Design units at 96 DPI (compact); painted / sized with ``_scale``.
    _BASE_W, _BASE_H = 14, 20
    _BASE_BODY = (
        (1, 1), (1, 14), (4, 11), (7, 18), (9, 17), (6, 10), (11, 10),
    )

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._scale = 1.0
        self._hotspot = QPoint(1, 1)
        self._apply_scale(self._dpi_scale())

    @staticmethod
    def _dpi_scale() -> float:
        screen = QApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return 1.0
        # logicalDotsPerInch tracks Windows per-monitor scaling; clamp so
        # 100% stays compact and 200%+ does not become a slab.
        return max(0.85, min(2.25, float(screen.logicalDotsPerInch()) / 96.0))

    def _apply_scale(self, scale: float) -> None:
        self._scale = scale
        self._hotspot = QPoint(max(1, round(scale)), max(1, round(scale)))
        self.setFixedSize(
            max(10, round(self._BASE_W * scale)),
            max(14, round(self._BASE_H * scale)),
        )
        self.update()

    def paintEvent(self, _event):
        s = self._scale
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, s >= 1.25)
        body = QPolygon([QPoint(round(x * s), round(y * s))
                         for x, y in self._BASE_BODY])
        p.setPen(QPen(QColor(20, 20, 24), max(1, round(s))))
        p.setBrush(QBrush(QColor(245, 245, 248)))
        p.drawPolygon(body)
        p.end()

    def follow(self) -> None:
        scale = self._dpi_scale()
        if abs(scale - self._scale) > 0.04:
            self._apply_scale(scale)
        tip = QCursor.pos() - self._hotspot
        self.move(tip)
        if not self.isVisible():
            self.show()
        self.raise_()


class SystemCursor:
    """Free the mouse while a game is locking/hiding it — without drawing a
    second arrow when one is already there.

    Used from the hotkey overlay (``show_manual``) and pins, and only while a
    game is running. Most titles already show their own cursor; for those we
    only undo ``ClipCursor`` / capture so the pointer can reach our panel.
    The software arrow (and a blank Qt override) appear only when the OS
    cursor is actually hidden or null — immersive Unity-style hides.

    Holders are counted, not calls — the first hold starts the fight loop and
    the last release tears it down.
    """

    _raised = 0
    _holders: set = set()
    _qt_override = False
    _IDC_ARROW = 32512
    _soft: QWidget | None = None
    _timer: QTimer | None = None

    @classmethod
    def hold(cls, key: str) -> None:
        """Keep the pointer usable until *key* lets go. Re-holding is harmless."""
        cls._holders.add(key)
        cls._raise()
        cls._ensure_timer()

    @classmethod
    def release(cls, key: str) -> None:
        cls._holders.discard(key)
        if not cls._holders:
            cls._stop_timer()
            cls._lower()

    @classmethod
    def release_all(cls) -> None:
        cls._holders.clear()
        cls._stop_timer()
        cls._lower()

    @classmethod
    def held_by(cls) -> set:
        return set(cls._holders)

    @classmethod
    def reassert(cls) -> None:
        """Undo clip/capture; draw a software arrow only if the OS one is gone.

        Safe to call from a short timer while any holder is active.
        """
        if not cls._holders:
            return
        # Windows: only draw when GetCursorInfo says the OS pointer is gone
        # (games that already show their own cursor keep it). Elsewhere there
        # is no equivalent probe — Proton/Wine can hide the pointer with no
        # Win32 API to ask — so while a holder is active we always draw.
        need_soft = platform.system() != "Windows"
        if platform.system() == "Windows":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                # ReleaseCapture: some Unity builds keep WM capture on their HWND
                # so the pointer never generates move events outside the game.
                if user32.GetCapture():
                    user32.ReleaseCapture()
                user32.ClipCursor(None)
                need_soft = cls._os_cursor_hidden()
                if need_soft:
                    user32.SetCursor(user32.LoadCursorW(None, cls._IDC_ARROW))
                    # Extra ShowCursor bumps are best-effort; soft arrow covers
                    # the case where the game re-hides from its own thread.
                    if not cls._cursor_showing():
                        for _ in range(16):
                            counter = user32.ShowCursor(True)
                            cls._raised += 1
                            if counter >= 0:
                                break
            except Exception as e:
                logger.debug(f"Could not reassert cursor freedom: {e}")
                need_soft = False
        cls._sync_soft(need_soft)

    @classmethod
    def _cursor_info(cls):
        """``(showing, hCursor)`` from GetCursorInfo, or ``None`` on failure."""
        if platform.system() != "Windows":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class CURSORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE),
                    ("ptScreenPos", wintypes.POINT),
                ]

            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(ci)
            if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)):
                return None
            return (bool(ci.flags & 0x1), ci.hCursor)
        except Exception:
            return None

    @classmethod
    def _cursor_showing(cls) -> bool:
        info = cls._cursor_info()
        return bool(info and info[0])

    @classmethod
    def _os_cursor_hidden(cls) -> bool:
        """True when we should draw our own arrow (no usable OS cursor)."""
        info = cls._cursor_info()
        if info is None:
            return False
        showing, handle = info
        # Hidden entirely, or "showing" a NULL/blank hotspot.
        return (not showing) or (not handle)

    @classmethod
    def _ensure_timer(cls) -> None:
        """Own the fight loop so pins/overlay do not each need one."""
        app = QApplication.instance()
        if app is None:
            return
        if cls._timer is not None and sip.isValid(cls._timer):
            if not cls._timer.isActive():
                cls._timer.start()
            return
        t = QTimer(app)
        t.setInterval(16)
        t.timeout.connect(cls.reassert)
        cls._timer = t
        t.start()

    @classmethod
    def _stop_timer(cls) -> None:
        t = cls._timer
        if t is None or not sip.isValid(t):
            cls._timer = None
            return
        t.stop()

    @classmethod
    def _soft_cursor(cls) -> QWidget | None:
        app = QApplication.instance()
        if app is None:
            return None
        if cls._soft is not None and sip.isValid(cls._soft):
            return cls._soft
        cls._soft = _SoftwareCursor()
        return cls._soft

    @classmethod
    def _sync_soft(cls, need_soft: bool) -> None:
        """Show the drawn arrow only when the OS pointer is unusable."""
        if not need_soft:
            cls._hide_soft()
            cls._clear_qt_override()
            return
        try:
            app = QApplication.instance()
            if app is not None and not cls._qt_override:
                # Blank our widgets so we do not stack Qt arrow + soft arrow.
                app.setOverrideCursor(Qt.CursorShape.BlankCursor)
                cls._qt_override = True
            w = cls._soft_cursor()
            if w is None:
                return
            w.follow()
        except RuntimeError:
            cls._soft = None

    @classmethod
    def _hide_soft(cls) -> None:
        w = cls._soft
        if w is None or not sip.isValid(w):
            cls._soft = None
            return
        try:
            w.hide()
        except RuntimeError:
            cls._soft = None

    @classmethod
    def _clear_qt_override(cls) -> None:
        if not cls._qt_override:
            return
        try:
            app = QApplication.instance()
            if app is not None:
                app.restoreOverrideCursor()
        except Exception as e:
            logger.debug(f"Could not restore Qt override cursor: {e}")
        cls._qt_override = False

    @classmethod
    def _raise(cls) -> None:
        try:
            if platform.system() == "Windows":
                import ctypes
                user32 = ctypes.windll.user32
                if user32.GetCapture():
                    user32.ReleaseCapture()
                user32.ClipCursor(None)
            cls.reassert()
        except Exception as e:
            logger.debug(f"Could not raise the system cursor: {e}")

    @classmethod
    def _lower(cls) -> None:
        cls._hide_soft()
        cls._clear_qt_override()
        if not cls._raised:
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


def set_dark_title_bar(widget, dark: bool | None = None) -> None:
    """Ensure native Windows 10/11 title bar and HWND frame match current dark/light mode."""
    if platform.system() != "Windows":
        return
    try:
        if dark is None:
            from ui.styles.theme import get_theme_manager
            dark = get_theme_manager().is_dark()
        import ctypes
        hwnd = int(widget.winId())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
        val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val)
        )
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, ctypes.byref(val), ctypes.sizeof(val)
        )
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


def clear_view_cache() -> None:
    """Release every decoded viewer image (up to 192 MB of full-screen
    pictures) and thumbnail cache."""
    _VIEW_CACHE.clear()
    _THUMB_CACHE.clear()


def trim_process_memory() -> None:
    """Purge all image/cover/editor caches, run GC and trim working set RAM back to OS."""
    try:
        clear_view_cache()
    except Exception:
        pass
    try:
        from ui.widgets.game_items import trim_cover_cache
        trim_cover_cache()
    except Exception:
        pass
    try:
        from core.save_editor import prune_all
        prune_all()
    except Exception:
        pass
    try:
        from PySide6.QtGui import QPixmapCache
        QPixmapCache.clear()
    except Exception:
        pass
    try:
        app = QApplication.instance()
        if app:
            from PySide6.QtCore import QEvent
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value if hasattr(QEvent.Type, "DeferredDelete") else 52)
            app.processEvents()
    except Exception:
        pass
    try:
        import gc
        gc.collect(2)
        gc.collect(1)
        gc.collect(0)
    except Exception:
        pass
    try:
        app = QApplication.instance()
        if app:
            app.processEvents()
    except Exception:
        pass
    try:
        import gc
        gc.collect(2)
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.EmptyWorkingSet(handle)
        except Exception:
            pass
    elif platform.system() == "Linux":
        try:
            import ctypes
            # glibc malloc_trim(0) returns unused heap memory back to the OS on Linux
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass


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


# Design baseline: logical work-area width (Qt DIPs) that matches the
# validated manual-100% look — typical 4K panel at OS 150% (≈2560 DIP).
# Ideal auto = availableGeometry.width() / 2560.
# DIPs already include OS scale on Windows, macOS and Linux (Qt high-DPI),
# so changing Scale/DPR keeps the same physical chrome size without a
# second ÷ OS% (that double-count is what pushed 4K@150% to ~133–200%).
# Callers MUST grow window geometry with ui_scale (fonts alone = compressed).
# Downscale floor is dynamic; below it, scrolls finish the fit.
_UI_RES_REF_WIDTH = 2560
_UI_SCALE_IDEAL_MIN = 0.50
_UI_SCALE_IDEAL_MAX = 4.00   # e.g. 8K @ OS 100% ≈ 300%+
_UI_SCALE_QUALITY_MAX = 4.00
_UI_SCALE_ABS_FLOOR = 0.50


# Global registry for widgets with scaled dimensions that need recalculation on DPI change
_SCALED_WIDGETS_REGISTRY: dict[int, dict] = {}  # widget_id -> {'width': (original, method), 'height': (original, method), 'size': ((w_orig, h_orig), method)}
# Re-entrancy guard: while a recalc is running, its own setFixedSize calls must
# NOT re-register — registering would re-baseline the freshly written
# "original" values from already-scaled pixels.
_SCALED_RECALC_IN_PROGRESS: bool = False

def _register_scaled_dimension(widget: QWidget, dimension_type: str, original_value: float | tuple, current_value: int | tuple) -> None:
    """Register a widget dimension that uses scaled() for recalculation on DPI change.
    
    Args:
        widget: The widget with the scaled dimension
        dimension_type: 'width', 'height', or 'size' (for setFixedSize)
        original_value: The original unscaled value (float or tuple for size)
        current_value: The current scaled value (int or tuple for size)
    """
    try:
        widget_id = id(widget)
        if widget_id not in _SCALED_WIDGETS_REGISTRY:
            _SCALED_WIDGETS_REGISTRY[widget_id] = {}
        import weakref
        _SCALED_WIDGETS_REGISTRY[widget_id][dimension_type] = (original_value, current_value, weakref.ref(widget))
    except Exception:
        pass

def _recalculate_all_scaled_dimensions() -> None:
    """Re-apply every registered scaled dimension at the CURRENT ui_scale.

    Each registry entry keeps the weakref captured at registration — that is
    the exact connection to the live widget: it resolves for any widget that
    still exists (top-level, page child, hidden popup — its place in the tree
    is irrelevant) and fails exactly when the widget is really gone, so dead
    entries are pruned without a fragile id()-based re-walk of the widget tree
    (a recycled Python id could re-apply an OLD widget's dims to an unrelated
    new one).

    setFixedSize calls made BY the recalc are not re-registered (guard flag);
    otherwise every re-apply would re-baseline the "original" values from
    pixels that are already scaled.
    """
    global _SCALED_RECALC_IN_PROGRESS
    if _SCALED_RECALC_IN_PROGRESS:
        return
    _SCALED_RECALC_IN_PROGRESS = True
    try:
        recalc_count = 0
        dead: list[tuple[int, str]] = []
        for widget_id, dimensions in list(_SCALED_WIDGETS_REGISTRY.items()):
            for dim_type, entry in list(dimensions.items()):
                # Tolerate legacy 2-tuple entries (no weakref) — prune them.
                if len(entry) < 3:
                    dead.append((widget_id, dim_type))
                    continue
                original_value, _current, widget_ref = entry
                widget = widget_ref()
                if widget is None:
                    dead.append((widget_id, dim_type))
                    continue
                try:
                    if dim_type in ('size', 'min_size', 'max_size'):
                        # setFixedSize / setMinimumSize / setMaximumSize -
                        # original is tuple (w_orig, h_orig)
                        w_orig, h_orig = original_value
                        new_w = int(scaled(w_orig, widget))
                        new_h = int(scaled(h_orig, widget))
                        _apply_dim_setter(widget, dim_type, (new_w, new_h))
                        dimensions[dim_type] = (original_value, (new_w, new_h), widget_ref)
                        recalc_count += 1
                    elif dim_type in ('width', 'height',
                                      'min_width', 'min_height',
                                      'max_width', 'max_height'):
                        # Single-axis setters - original is float
                        new_v = int(scaled(original_value, widget))
                        _apply_dim_setter(widget, dim_type, new_v)
                        dimensions[dim_type] = (original_value, new_v, widget_ref)
                        recalc_count += 1
                    widget.updateGeometry()
                    # A hidden (stacked) page keeps its stale geometry until
                    # its layout is invalidated; force it so the next show
                    # (or event loop) re-lays-out at the new scale.
                    try:
                        parent = widget.parentWidget()
                        if parent is not None:
                            pl = parent.layout()
                            if pl is not None:
                                pl.invalidate()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"Failed to recalculate {dim_type} for {type(widget).__name__}: {e}")
        for widget_id, dim_type in dead:
            dims = _SCALED_WIDGETS_REGISTRY.get(widget_id)
            if dims is None:
                continue
            dims.pop(dim_type, None)
            if not dims:
                _SCALED_WIDGETS_REGISTRY.pop(widget_id, None)
        logger.info(f"Recalculated {recalc_count} dimensions for {len(_SCALED_WIDGETS_REGISTRY)} widgets")
    except Exception as e:
        logger.warning(f"Failed to recalculate scaled dimensions: {e}")
    finally:
        _SCALED_RECALC_IN_PROGRESS = False


def _apply_dim_setter(widget: QWidget, dim_type: str, value) -> None:
    """Route a recalculated dimension to the QWidget setter it came from."""
    if dim_type == 'size':
        widget.setFixedSize(*value)
    elif dim_type == 'min_size':
        widget.setMinimumSize(*value)
    elif dim_type == 'max_size':
        widget.setMaximumSize(*value)
    elif dim_type == 'width':
        widget.setFixedWidth(value)
    elif dim_type == 'height':
        widget.setFixedHeight(value)
    elif dim_type == 'min_width':
        widget.setMinimumWidth(value)
    elif dim_type == 'min_height':
        widget.setMinimumHeight(value)
    elif dim_type == 'max_width':
        widget.setMaximumWidth(value)
    elif dim_type == 'max_height':
        widget.setMaximumHeight(value)


def _dim_actual_expected(widget, dim_type, design):
    """``(actual, expected)`` for one tracked dimension at the CURRENT scale.

    ``expected`` is ``round(design * ui_scale)`` (min_px floors are not
    remembered, so tiny widgets may report an intentional floor as a diff —
    fine for a diagnostic dump).
    """
    scale = ui_scale(widget)
    exp = lambda v: int(round(v * scale))
    if dim_type == 'min_size':
        return tuple(widget.minimumSize()), (exp(design[0]), exp(design[1]))
    if dim_type == 'max_size':
        return tuple(widget.maximumSize()), (exp(design[0]), exp(design[1]))
    if dim_type == 'size':
        return (widget.width(), widget.height()), (exp(design[0]), exp(design[1]))
    getter = {
        'width': lambda: widget.width(),
        'height': lambda: widget.height(),
        'min_width': lambda: widget.minimumWidth(),
        'min_height': lambda: widget.minimumHeight(),
        'max_width': lambda: widget.maximumWidth(),
        'max_height': lambda: widget.maximumHeight(),
    }[dim_type]
    return getter(), exp(design)


def _trace_scaled_state(tag: str) -> None:
    """SAVESYNC_TRACE=1: dump the exact chrome state for a before/after diff.

    Logs the effective ui_scale, every visible top-level window's geometry,
    and each registered scaled dimension whose ACTUAL value differs from what
    the current scale demands — the stale winners behind a "partly bigger"
    restore. No-op without the env var; never raises.
    """
    if os.environ.get("SAVESYNC_TRACE") != "1":
        return
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        lines = [f"ui_scale={ui_scale():.3f}"]
        for w in app.topLevelWidgets():
            try:
                if not w.isWindow() or not w.isVisible():
                    continue
                g = w.geometry()
                lines.append(
                    f"  window {type(w).__name__}: {g.width()}x{g.height()} "
                    f"@ {g.x()},{g.y()} min {w.minimumWidth()}x{w.minimumHeight()}")
            except Exception:
                continue
        for widget_id, dims in list(_SCALED_WIDGETS_REGISTRY.items()):
            for dim_type, entry in list(dims.items()):
                if len(entry) < 3:
                    continue
                original_value, _current, widget_ref = entry
                widget = widget_ref()
                if widget is None:
                    continue
                # Hidden pages (stacked tabs) keep stale geometries by design;
                # they re-lay-out when shown. Only visible widgets matter.
                if not widget.isVisible():
                    continue
                try:
                    actual, expected = _dim_actual_expected(
                        widget, dim_type, original_value)
                    if actual != expected:
                        on = widget.objectName() or "-"
                        lines.append(
                            f"  MISMATCH {type(widget).__name__}[{on}].{dim_type}: "
                            f"design={original_value} actual={actual} expected={expected}")
                except Exception:
                    continue
        if len(lines) > 1:
            logger.info("[TRACE] scaled state (%s):\n%s", tag, "\n".join(lines))
    except Exception:
        pass


# Monkey-patch QWidget methods to track scaled dimensions — fixed AND
# min/max. Only dimensions passed through ``scaled()`` (a ScaledValue) are
# registered; plain raw pixel values stay fixed. Without the min/max legs a
# scale change grew the floors and forgot to shrink them back (a widget that
# had ``setMinimumWidth(scaled(120, self))`` at 1.5 kept 180 px at 1.0).
_original_setFixedSize = QWidget.setFixedSize
_original_setFixedWidth = QWidget.setFixedWidth
_original_setFixedHeight = QWidget.setFixedHeight
_original_setMinimumSize = QWidget.setMinimumSize
_original_setMinimumWidth = QWidget.setMinimumWidth
_original_setMinimumHeight = QWidget.setMinimumHeight
_original_setMaximumSize = QWidget.setMaximumSize
_original_setMaximumWidth = QWidget.setMaximumWidth
_original_setMaximumHeight = QWidget.setMaximumHeight


def _make_tracked_setter(original, dim_type):
    """Build a tracked setter for one QWidget size method.

    Pair setters (``size``/``min_size``/``max_size``) register per-axis, so a
    fully or partially scaled pair survives a scale change; single-axis
    setters register their own dim type. While ``_SCALED_RECALC_IN_PROGRESS``
    is set the recalc's own calls are not re-registered (no re-baselining).
    """
    def _tracked(self, *args):
        original(self, *args)
        if _SCALED_RECALC_IN_PROGRESS:
            return
        try:
            if dim_type in ('size', 'min_size', 'max_size'):
                w, h = args
                if isinstance(w, ScaledValue) and isinstance(h, ScaledValue):
                    _register_scaled_dimension(
                        self, dim_type, (w.design, h.design), (int(w), int(h)))
                elif isinstance(w, ScaledValue):
                    axis = 'width' if dim_type == 'size' else dim_type.replace('size', 'width')
                    _register_scaled_dimension(self, axis, w.design, int(w))
                elif isinstance(h, ScaledValue):
                    axis = 'height' if dim_type == 'size' else dim_type.replace('size', 'height')
                    _register_scaled_dimension(self, axis, h.design, int(h))
            elif isinstance(args[0], ScaledValue):
                _register_scaled_dimension(self, dim_type, args[0].design, int(args[0]))
        except Exception:
            pass
    _tracked.__name__ = f"_tracked_{original.__name__}"
    return _tracked


QWidget.setFixedSize = _make_tracked_setter(_original_setFixedSize, 'size')
QWidget.setFixedWidth = _make_tracked_setter(_original_setFixedWidth, 'width')
QWidget.setFixedHeight = _make_tracked_setter(_original_setFixedHeight, 'height')
QWidget.setMinimumSize = _make_tracked_setter(_original_setMinimumSize, 'min_size')
QWidget.setMinimumWidth = _make_tracked_setter(_original_setMinimumWidth, 'min_width')
QWidget.setMinimumHeight = _make_tracked_setter(_original_setMinimumHeight, 'min_height')
QWidget.setMaximumSize = _make_tracked_setter(_original_setMaximumSize, 'max_size')
QWidget.setMaximumWidth = _make_tracked_setter(_original_setMaximumWidth, 'max_width')
QWidget.setMaximumHeight = _make_tracked_setter(_original_setMaximumHeight, 'max_height')


def windows_os_scale(widget: QWidget | None = None) -> float:
    """OS display scale (1.0 = 100%, 1.5 = 150%, 2.0 = 200%).

    Prefer ``devicePixelRatio`` (reliable on Windows/macOS/Linux). Informational
    for tooltips — auto ideal uses DIPs, which already embed this factor.
    """
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    try:
        dpr = float(screen.devicePixelRatio() or 0.0)
        if dpr >= 0.50:
            return max(0.50, min(3.0, dpr))
        return max(0.50, min(3.0, float(screen.logicalDotsPerInch()) / 96.0))
    except Exception:
        return 1.0


def _work_area_width_physical(widget: QWidget | None = None) -> float:
    """Work-area width in physical pixels (DIPs × screen DPR)."""
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return float(_UI_RES_REF_WIDTH) * windows_os_scale(widget)
    try:
        geo = screen.availableGeometry()
        dpr = float(screen.devicePixelRatio() or 1.0)
        return max(1.0, float(geo.width()) * dpr)
    except Exception:
        return float(_UI_RES_REF_WIDTH)


def _work_area_width_dip(widget: QWidget | None = None) -> int:
    """Work-area width in Qt device-independent pixels (logical).

    Already accounts for OS Scale / Retina on Windows, macOS and Linux.
    """
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return _UI_RES_REF_WIDTH
    try:
        return max(1, int(screen.availableGeometry().width()))
    except Exception:
        return _UI_RES_REF_WIDTH


def resolution_scale(widget: QWidget | None = None) -> float:
    """Chrome scale vs design baseline from **logical** work-area width."""
    width = float(_work_area_width_dip(widget))
    return max(0.50, width / float(_UI_RES_REF_WIDTH))


def ui_scale_ideal(widget: QWidget | None = None) -> float:
    """Auto target: ``logical_work_width / 2560`` (same look as manual 100% on 4K@150%)."""
    ideal = resolution_scale(widget)
    return max(_UI_SCALE_IDEAL_MIN, min(_UI_SCALE_IDEAL_MAX, ideal))


def ui_scale_quality_min(widget: QWidget | None = None) -> float:
    """Dynamic downscale floor before fonts smudge — by logical width.

    Smaller work areas may go toward ~55%; at/above the 2560 baseline hold
    ~70%. Below the floor, keep it and use scrollbars instead of crushing glyphs.
    """
    w = float(_work_area_width_dip(widget))
    if w >= float(_UI_RES_REF_WIDTH):
        return 0.70
    if w <= 640.0:
        return _UI_SCALE_ABS_FLOOR
    if w <= 1280.0:
        t = (w - 640.0) / (1280.0 - 640.0)
        return _UI_SCALE_ABS_FLOOR + t * 0.05
    t = (w - 1280.0) / (float(_UI_RES_REF_WIDTH) - 1280.0)
    return 0.55 + t * 0.15


def _read_ui_scale_auto() -> bool:
    if _ui_scale_auto_preview is not None:
        return bool(_ui_scale_auto_preview)
    try:
        from core.config_manager import get_config
        return bool(get_config().get("ui_scale_auto", True))
    except Exception:
        return True


def _read_ui_scale_pref() -> float:
    if _ui_scale_preview is not None:
        return float(_ui_scale_preview)
    try:
        from core.config_manager import get_config
        return float(get_config().get("ui_scale_factor", 1.0) or 1.0)
    except Exception:
        return 1.0


def ui_scale(widget: QWidget | None = None) -> float:
    """Logical UI scale for chrome (buttons, paddings, QSS fonts).

    **Auto**: ``logical_work_width / 2560`` then readability clamp — same look
    as manual 100% on a typical 4K @ OS 150% desktop. DIPs already embed
    Windows/macOS/Linux scaling, so switching OS % keeps physical size.
    Examples: 4K@150%→≈100%, 4K@100%→≈150%, FHD@100%→≈75%, 8K@100%→≈300%.
    Manual: ``ui_scale_factor`` 50–150%.

    Scaling fonts without resizing the window compresses the layout — callers
    must grow/shrink window geometry with the same factor.
    """
    if _read_ui_scale_auto():
        return max(
            ui_scale_quality_min(widget),
            min(_UI_SCALE_QUALITY_MAX, ui_scale_ideal(widget)),
        )
    return max(0.50, min(4.00, _read_ui_scale_pref()))


def scale_window_geometry(window: QWidget, prev: float, cur: float) -> None:
    """Grow/shrink *window* with a ui_scale change (not maximised).

    Upscaling chrome inside a fixed frame is what makes the UI look crushed;
    the window footprint has to move with the scale.
    Also triggers refresh_styles on child widgets to update their internal scaling.
    
    Saves pre-scale geometry for dirty state restoration if auto DPI is active.
    """
    if window is None or prev is None or cur is None:
        return
    if prev <= 0 or abs(cur - prev) < 0.02:
        return
    try:
        if hasattr(window, "isMaximized") and window.isMaximized():
            return
    except Exception:
        pass
    
    # Save pre-scale geometry for dirty state restoration (auto DPI only)
    try:
        from core.config_manager import get_config
        config = get_config()
        if config.get("ui_scale_auto", True):
            # Only save if this is an auto-triggered scale change
            if hasattr(window, "_pre_scale_geometry"):
                window._pre_scale_geometry = window.geometry()
            else:
                window._pre_scale_geometry = window.geometry()
    except Exception:
        pass
    
    ratio = cur / prev
    try:
        geo = window.geometry()
        nw = int(round(geo.width() * ratio))
        nh = int(round(geo.height() * ratio))
        screen = window.screen() if hasattr(window, "screen") else None
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is not None:
            ag = screen.availableGeometry()
            nw = min(max(640, nw), max(640, int(ag.width() * 0.96)))
            nh = min(max(480, nh), max(480, int(ag.height() * 0.96)))
        else:
            nw, nh = max(640, nw), max(480, nh)
        window.resize(nw, nh)
        # Refresh child widgets to apply new scale to internal dimensions
        _refresh_child_widgets(window)
    except Exception:
        pass


def _refresh_child_widgets(widget: QWidget) -> None:
    """Recursively refresh styles on all child widgets after scale change.
    
    Also updates fixed sizes, layouts, and internal widget dimensions to
    maintain proper proportions across gadgets, sidebar, and content areas.
    """
    try:
        # First refresh the widget itself if it has refresh_styles
        if hasattr(widget, 'refresh_styles'):
            try:
                widget.refresh_styles()
            except Exception:
                pass
        
        # Trigger layout recalculation to redistribute space proportionally
        if hasattr(widget, 'layout'):
            try:
                widget.layout().activate()
                widget.layout().update()
            except Exception:
                pass
        
        # Force widget geometry update
        try:
            widget.updateGeometry()
        except Exception:
            pass
        
        # Force widget repaint to ensure new dimensions are applied
        try:
            widget.update()
        except Exception:
            pass
        
        # Then recursively process children
        for child in widget.findChildren(QWidget):
            if child is widget:
                continue
            try:
                if hasattr(child, 'refresh_styles'):
                    child.refresh_styles()
                
                # Update fixed widths/heights that use scaled()
                if hasattr(child, 'minimumWidth') and child.minimumWidth() > 0:
                    child.updateGeometry()
                if hasattr(child, 'minimumHeight') and child.minimumHeight() > 0:
                    child.updateGeometry()
                
                # Force update of explicitly fixed sizes (widgets that use setFixedWidth/Height)
                # This ensures widgets with fixed dimensions get recalculated based on new scale
                if hasattr(child, 'width') and child.width() > 0:
                    child.updateGeometry()
                if hasattr(child, 'height') and child.height() > 0:
                    child.updateGeometry()
                
                # Update fixed sizes to maintain proportions
                if hasattr(child, 'sizePolicy'):
                    try:
                        child.updateGeometry()
                    except Exception:
                        pass
                
                # Force layout update for child
                if hasattr(child, 'layout') and child.layout():
                    try:
                        child.layout().activate()
                        child.layout().update()
                    except Exception:
                        pass
                
                # Force widget geometry update
                try:
                    child.updateGeometry()
                except Exception:
                    pass
                
                # Force widget repaint
                try:
                    child.update()
                except Exception:
                    pass
                        
            except Exception:
                continue
    except Exception:
        pass


def scale_all_top_level_windows(prev: float, cur: float) -> None:
    """Apply :func:`scale_window_geometry` to every visible top-level window.
    
    Also re-centers dialogs after scaling to maintain proper positioning.
    """
    app = QApplication.instance()
    if app is None or prev is None or cur is None:
        return
    if prev <= 0 or abs(cur - prev) < 0.02:
        return
    try:
        widgets = list(app.topLevelWidgets())
    except Exception:
        return
    for w in widgets:
        try:
            if not w.isWindow() or not w.isVisible():
                continue
            scale_window_geometry(w, prev, cur)
            if hasattr(w, "_last_ui_scale"):
                w._last_ui_scale = cur
            # Re-center dialogs after scaling
            if hasattr(w, "_center_on_parent"):
                try:
                    w._center_on_parent()
                except Exception:
                    pass
        except Exception:
            continue


_ui_scale_preview: float | None = None
_ui_scale_auto_preview: bool | None = None


def restore_pre_scale_geometry(window: QWidget) -> None:
    """Restore window geometry to pre-scale state if auto DPI was active.
    
    Called when user cancels DPI auto-scale changes to revert to previous size.
    """
    if window is None:
        return
    try:
        if hasattr(window, "_pre_scale_geometry"):
            geo = window._pre_scale_geometry
            if geo.isValid():
                window.setGeometry(geo)
                delattr(window, "_pre_scale_geometry")
    except Exception:
        pass


def set_ui_scale_preview(factor: float | None) -> None:
    """Temporary manual multiplier for live Settings preview; ``None`` = config."""
    global _ui_scale_preview
    if factor is None:
        _ui_scale_preview = None
        return
    _ui_scale_preview = max(0.50, min(4.00, float(factor)))


def set_ui_scale_auto_preview(auto: bool | None) -> None:
    """Temporary auto/manual flag for live Settings preview; ``None`` = config."""
    global _ui_scale_auto_preview
    _ui_scale_auto_preview = None if auto is None else bool(auto)


def clear_ui_scale_preview() -> None:
    """Drop both scale preview overrides (after Save / Cancel / reload)."""
    set_ui_scale_preview(None)
    set_ui_scale_auto_preview(None)


def scaled(px: int, widget: QWidget | None = None, *, min_px: int = 1) -> "ScaledValue":
    """Design pixels × ``ui_scale``, floored at *min_px*.

    Returns a ``ScaledValue`` — an int that remembers the design value it was
    computed from — so the fixed-size monkey-patches can tell "this dimension
    is DPI-intended" from a plain raw pixel value. Plain pixels are left
    alone (Qt DIPs already embed the OS scale); only explicitly scaled
    dimensions re-apply on ui_scale changes. Arithmetic on the result drops
    the marker: computed sizes (popup heights, offsets) are deliberately
    never registered."""
    v = max(min_px, int(round(px * ui_scale(widget))))
    return ScaledValue(px, v)


class ScaledValue(int):
    """int subclass carrying the design value it was scaled from.

    See :func:`scaled`. Plain Python class (CPython forbids nonempty
    ``__slots__`` on int subclasses) — the ``design`` attribute lives on a
    regular instance dict, so arithmetic and equality behave exactly like int
    while the marker survives."""

    def __new__(cls, design: float, value: int):
        self = int.__new__(cls, value)
        self.design = design
        return self


_FONT_SIZE_PX_RE = None
_CSS_PX_RE = None


def scale_stylesheet_fonts(qss: str, factor: float | None = None) -> str:
    """Rewrite CSS ``Npx`` lengths by *factor* (default ``ui_scale``).

    Design-time QSS stays at 100% integers. At apply time we scale fonts
    **and** padding/margins/radii/mins so inverse Windows compensation does
    not leave chrome padding at full OS size while text shrinks (blurry or
    mismatched proportions). ``0px`` stays 0; other lengths floor at 1px so
    hairline borders remain visible.
    """
    if not qss:
        return qss
    if factor is None:
        factor = ui_scale()
    if abs(factor - 1.0) < 1e-6:
        return qss
    global _CSS_PX_RE
    if _CSS_PX_RE is None:
        import re
        # Match length tokens like 13px / 10px — not inside identifiers.
        _CSS_PX_RE = re.compile(r"(?<![A-Za-z0-9_])(\d+)px\b")

    def _repl(m):
        n0 = int(m.group(1))
        if n0 == 0:
            return "0px"
        return f"{max(1, int(round(n0 * factor)))}px"

    return _CSS_PX_RE.sub(_repl, qss)


def lock_min_size(widget: QWidget, w: int | None = None, h: int | None = None,
                  *, policy_h=None, policy_v=None) -> None:
    """Keep a control from being crushed below its chrome footprint."""
    if w is not None:
        # Keep a ScaledValue (if given) so the tracked setters register the
        # design value; plain ints stay untracked exactly as before.
        widget.setMinimumWidth(max(1, w))
    if h is not None:
        widget.setMinimumHeight(max(1, h))
    if policy_h is not None or policy_v is not None:
        from PySide6.QtWidgets import QSizePolicy
        cur = widget.sizePolicy()
        widget.setSizePolicy(
            policy_h if policy_h is not None else cur.horizontalPolicy(),
            policy_v if policy_v is not None else cur.verticalPolicy(),
        )


def dialog_host_geometry(dialog: QWidget):
    """Parent frame if usable, else the dialog's / primary screen work area."""
    parent = dialog.parentWidget()
    if parent is not None and parent.isVisible():
        geo = parent.frameGeometry()
        if geo.width() >= 200 and geo.height() >= 200:
            return geo
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is not None:
        return screen.availableGeometry()
    return None


class DialogSizeResult(NamedTuple):
    """Resize-first dialog sizing result.

    *width* / *height* are the applied size (screen-capped). *prefer_w* /
    *prefer_h* are the uncapped content footprint. *capped_** mean resize
    alone could not fit — enable scroll via ``mediate_panel_scroll``.
    """
    width: int
    height: int
    capped_w: bool
    capped_h: bool
    prefer_w: int
    prefer_h: int


def preferred_dialog_size(
    dialog: QWidget,
    *,
    min_w: int = 480,
    min_h: int = 400,
    prefer_w: int | None = None,
    prefer_h: int | None = None,
    max_width_frac: float = 0.92,
    max_height_frac: float = 0.92,
) -> tuple[int, int, bool, bool, int, int]:
    """Content footprint capped to the host/screen.

    Returns ``(w, h, capped_w, capped_h, prefer_w, prefer_h)`` — the last two
    are the uncapped targets (resize-first intent).
    """
    target_w = max(1, prefer_w if prefer_w is not None else min_w)
    target_h = max(1, prefer_h if prefer_h is not None else min_h)
    ref = dialog_host_geometry(dialog)
    if ref is None:
        return target_w, target_h, False, False, target_w, target_h
    max_w = max(1, int(ref.width() * max_width_frac))
    max_h = max(1, int(ref.height() * max_height_frac))
    w, h = min(target_w, max_w), min(target_h, max_h)
    return w, h, target_w > max_w, target_h > max_h, target_w, target_h


def apply_adaptive_dialog_size(
    dialog: QWidget,
    *,
    min_w: int = 480,
    min_h: int = 400,
    prefer_w: int | None = None,
    prefer_h: int | None = None,
    max_width_frac: float = 0.92,
    max_height_frac: float = 0.92,
) -> DialogSizeResult:
    """Resize-first panel sizing for every dialog.

    Rule (all panels):
      1. Resize to the content footprint when it fits the work area.
      2. If it does not fit (low resolution / small host), clamp to the work
         area and let ``mediate_panel_scroll`` enable H and/or V scrollbars.
      3. The user may still enlarge freely afterward.
    """
    _MAX = 16777215
    ref = dialog_host_geometry(dialog)
    max_w = int(ref.width() * max_width_frac) if ref is not None else (
        prefer_w if prefer_w is not None else min_w)
    max_h = int(ref.height() * max_height_frac) if ref is not None else (
        prefer_h if prefer_h is not None else min_h)
    floor_w = min(min_w, max_w) if ref is not None else min_w
    floor_h = min(min_h, max_h) if ref is not None else min_h
    dialog.setMinimumWidth(max(1, floor_w))
    dialog.setMinimumHeight(max(1, floor_h))
    dialog.setMaximumWidth(_MAX)
    dialog.setMaximumHeight(_MAX)
    w, h, capped_w, capped_h, tw, th = preferred_dialog_size(
        dialog,
        min_w=min_w,
        min_h=min_h,
        prefer_w=prefer_w,
        prefer_h=prefer_h,
        max_width_frac=max_width_frac,
        max_height_frac=max_height_frac,
    )
    dialog.resize(w, h)
    return DialogSizeResult(w, h, capped_w, capped_h, tw, th)


def mediate_panel_scroll(
    scroll: "QScrollArea",
    size: DialogSizeResult | None = None,
    *,
    capped_w: bool = False,
    capped_h: bool = False,
    list_content: bool = False,
) -> None:
    """Enable H/V scrollbars only when resize could not fit the panel.

    *list_content*: scroll hosts a growing list inside an already-fitted
    panel — keep vertical ``AsNeeded`` so long lists remain reachable even
    when the dialog itself was not height-capped. Horizontal still follows
    the resize-first rule (bars only when width was capped), unless the
    list is known to need them after a width cap.
    """
    from PySide6.QtWidgets import QScrollArea
    if not isinstance(scroll, QScrollArea):
        return
    if size is not None:
        capped_w = size.capped_w
        capped_h = size.capped_h
    # Horizontal: only when the panel could not grow wide enough.
    scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded if capped_w
        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    if list_content:
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    else:
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if capped_h
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


def page_scroll_caps(scroll: "QScrollArea") -> tuple[bool, bool]:
    """Whether page content *must* scroll (viewport cannot hold the minimum).

    Uses ``minimumSizeHint`` / ``minimumWidth|Height``, not preferred
    ``sizeHint`` — preferred sizes are often larger than a normal window and
    would force H/V bars even when layout downscaling can still fit. Scroll
    engages on low resolutions (e.g. 720p) only when the floor still overflows.
    """
    from PySide6.QtWidgets import QScrollArea
    if not isinstance(scroll, QScrollArea):
        return False, False
    vp = scroll.viewport()
    content = scroll.widget()
    if vp is None or content is None:
        return False, False
    avail_w, avail_h = vp.width(), vp.height()
    if avail_w < 40 or avail_h < 40:
        return False, False
    mins = content.minimumSizeHint()
    need_w = max(mins.width(), content.minimumWidth())
    need_h = max(mins.height(), content.minimumHeight())
    # Ignore bogus zero/empty hints (some hosts report 0 until first layout).
    if need_w < 8:
        need_w = 0
    if need_h < 8:
        need_h = 0
    return (
        need_w > 0 and need_w > avail_w + 2,
        need_h > 0 and need_h > avail_h + 2,
    )


def mediate_page_scroll(
    scroll: "QScrollArea",
    *,
    list_content: bool = False,
) -> None:
    """Resize-first scroll policy for main-window pages (not dialogs).

    Prefer fitting via layout / UI scale / window size. Enable H and/or V
    only when the content *minimum* still overflows the viewport (typical on
    720p or a heavily shrunk window). Growing lists use ``list_content`` so
    vertical stays ``AsNeeded`` after the panel itself fitted.
    """
    capped_w, capped_h = page_scroll_caps(scroll)
    mediate_panel_scroll(
        scroll, capped_w=capped_w, capped_h=capped_h,
        list_content=list_content)


class PageScrollMixin:
    """Register page ``QScrollArea``s and re-mediate on resize/show.

    Use on Overview / Library / Sync / Backups / Settings / Cheats. Does not
    resize the main window — only toggles scrollbars when the stack viewport
    cannot hold the content footprint.
    """

    def _register_page_scroll(self, scroll, *, list_content: bool = False):
        scrolls = getattr(self, "_page_scrolls", None)
        if scrolls is None:
            self._page_scrolls = []
            scrolls = self._page_scrolls
        scrolls.append((scroll, list_content))
        mediate_page_scroll(scroll, list_content=list_content)

    def _remediate_page_scrolls(self):
        for scroll, list_content in list(getattr(self, "_page_scrolls", [])):
            try:
                if scroll is not None:
                    mediate_page_scroll(scroll, list_content=list_content)
            except RuntimeError:
                pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Viewport sizes settle after this layout pass.
        QTimer.singleShot(0, self._remediate_page_scrolls)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._remediate_page_scrolls)


def fit_dialog_to_content(
    dialog: QWidget,
    content: QWidget,
    *,
    chrome_h: int = 0,
    chrome_w: int = 0,
    min_w: int = 480,
    min_h: int = 400,
) -> DialogSizeResult:
    """Resize *dialog* to *content*'s natural size plus chrome (resize-first)."""
    content.adjustSize()
    hint = content.sizeHint()
    prefer_w = max(min_w, hint.width() + max(0, chrome_w))
    prefer_h = max(min_h, hint.height() + max(0, chrome_h))
    return apply_adaptive_dialog_size(
        dialog,
        min_w=min(min_w, prefer_w),
        min_h=min(min_h, prefer_h),
        prefer_w=prefer_w,
        prefer_h=prefer_h,
    )


def measure_dialog_prefer(
    dialog: QWidget,
    *,
    min_w: int = 480,
    min_h: int = 400,
) -> tuple[int, int]:
    """Content footprint after layout activation (post-build)."""
    lay = dialog.layout()
    if lay is not None:
        try:
            lay.activate()
        except Exception:
            pass
    try:
        dialog.ensurePolished()
    except Exception:
        pass
    hint = dialog.sizeHint()
    mins = dialog.minimumSizeHint()
    prefer_w = max(min_w, hint.width(), mins.width(), dialog.minimumWidth())
    prefer_h = max(min_h, hint.height(), mins.height(), dialog.minimumHeight())
    return prefer_w, prefer_h


def finalize_adaptive_dialog_size(
    dialog: QWidget,
    *,
    min_w: int = 480,
    min_h: int = 400,
    scroll=None,
    list_content: bool = False,
    max_width_frac: float = 0.92,
    max_height_frac: float = 0.92,
) -> DialogSizeResult:
    """Post-build resize-first: grow to content, scroll only when capped.

    Call after the dialog layout is populated. Replaces the common anti-pattern
    of ``apply_adaptive_dialog_size(min_w, min_h)`` before ``_build`` (which
    treated the floor as the preferred size and left useless scrollbars).
    """
    prefer_w, prefer_h = measure_dialog_prefer(
        dialog, min_w=min_w, min_h=min_h)
    size = apply_adaptive_dialog_size(
        dialog,
        min_w=min_w,
        min_h=min_h,
        prefer_w=prefer_w,
        prefer_h=prefer_h,
        max_width_frac=max_width_frac,
        max_height_frac=max_height_frac,
    )
    # Prefer the user's last manual size when it still fits the host.
    if restore_dialog_geometry(dialog):
        if prefer_h is not None and dialog.height() < prefer_h:
            dialog.resize(dialog.width(), prefer_h)
        # Recompute caps from the restored footprint vs host.
        host = dialog_host_geometry(dialog)
        if host is not None:
            max_w = int(host.width() * max_width_frac)
            max_h = int(host.height() * max_height_frac)
            size = DialogSizeResult(
                dialog.width(), dialog.height(),
                dialog.width() >= max_w - 2, dialog.height() >= max_h - 2,
                prefer_w, prefer_h,
            )
    hook_dialog_geometry_save(dialog)
    if scroll is not None:
        mediate_panel_scroll(scroll, size, list_content=list_content)
    return size



def hook_dialog_geometry_save(dialog: QWidget) -> None:
    """Remember size/pos when a QDialog finishes (after manual resize)."""
    if getattr(dialog, "_geom_save_hooked", False):
        return
    finished = getattr(dialog, "finished", None)
    if finished is None:
        return
    dialog._geom_save_hooked = True
    finished.connect(lambda *_a, d=dialog: save_dialog_geometry(d))


def dialog_geometry_key(dialog: QWidget) -> str:
    """Stable config key for a dialog class (override via ``_geometry_key``)."""
    custom = getattr(dialog, "_geometry_key", None)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return dialog.__class__.__name__


def save_dialog_geometry(dialog: QWidget, key: str | None = None) -> None:
    """Persist the dialog's current geometry for the next open."""
    try:
        from core.config_manager import get_config
        geo = dialog.normalGeometry() if hasattr(dialog, "normalGeometry") else dialog.geometry()
        k = key or dialog_geometry_key(dialog)
        store = dict(get_config().get("dialog_geometries", {}) or {})
        store[k] = {
            "x": int(geo.x()), "y": int(geo.y()),
            "w": int(geo.width()), "h": int(geo.height()),
        }
        get_config().set("dialog_geometries", store)
    except Exception:
        pass


def restore_dialog_geometry(dialog: QWidget, key: str | None = None) -> bool:
    """Apply a saved geometry if it still fits the work area. Returns True if used."""
    try:
        from core.config_manager import get_config
        k = key or dialog_geometry_key(dialog)
        store = get_config().get("dialog_geometries", {}) or {}
        geo = store.get(k)
        if not isinstance(geo, dict):
            return False
        w = max(200, int(geo.get("w", 0)))
        h = max(160, int(geo.get("h", 0)))
        x = int(geo.get("x", 0))
        y = int(geo.get("y", 0))
        host = dialog_host_geometry(dialog)
        if host is not None:
            # Reject stale sizes that no longer fit (e.g. after scale/monitor change).
            if w > host.width() * 0.98 or h > host.height() * 0.98:
                return False
            x = max(host.x(), min(x, host.x() + host.width() - w))
            y = max(host.y(), min(y, host.y() + host.height() - h))
        dialog.setGeometry(x, y, w, h)
        return True
    except Exception:
        return False


def clear_dialog_geometries() -> None:
    """Drop remembered dialog sizes (auto-scale may need a fresh fit)."""
    try:
        from core.config_manager import get_config
        get_config().set("dialog_geometries", {})
    except Exception:
        pass


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
        self.setMinimumWidth(scaled(40, self))
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
        self.setMinimumWidth(scaled(80, self))
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
