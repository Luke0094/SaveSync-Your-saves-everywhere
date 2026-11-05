"""
SaveSync - What the windows of the application need for as long as a game is in exclusive fullscreen (Windows).

ui.win_band_hook makes it possible to show a real Qt window over such a game; this module is what keeps the rest of the
application's windows from taking the game back to the desktop while one is up:

* every window Qt creates meanwhile is created in the higher band (the hook's mode stays on for the session), including
  the menus, the lists of combo boxes and the dialogs the application opens;
* a dialog gets the shape that does not take the game out of exclusive mode (frameless, non-activating, translucent) and a
  small title strip of its own - the title, a close button, and the place to drag it by;
* the native file dialogs (a window of the system, which cannot be made like that) are replaced by Qt's own;
* tooltips are not shown (an ordinary window);
* a popup menu does not have the mouse captured as it normally does - capturing needs the foreground - so a press outside
  it is noticed here and closes it;
* a text field that is clicked gets the keyboard (ui.exclusive_keys).

A session is begun by whoever puts a window up over the game (the overlay, the application window, the pins) and ends when
the last of them says it no longer needs it - or, if it forgets to say so, when the question it was begun with
(``still_needed``) is answered "no". Nothing here does anything while no session is on.
"""
import logging
import os
import platform
import time
import weakref
from collections import deque
from contextlib import contextmanager
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
                               QPlainTextEdit, QPushButton, QTextEdit, QToolTip, QWidget)

from ui import win_band_hook

logger = logging.getLogger(__name__)

_u32 = None


def _user32():
    global _u32
    if _u32 is None:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        _u32 = u
    return _u32


def _describe_window(hwnd) -> tuple:
    """(process id, class name) of a window."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    pid = wintypes.DWORD(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(80)
    u.GetClassNameW(hwnd, buf, 80)
    return int(pid.value), buf.value


def _front_window() -> tuple:
    """(handle, process id, class name) of the window in front; zeros when there is none."""
    hwnd = _user32().GetForegroundWindow()
    if not hwnd:
        return 0, 0, ""
    pid, cls = _describe_window(hwnd)
    return int(hwnd), pid, cls


def _win32_state(hwnd: int) -> str:
    """What the system says about a window: visible, cloaked (hidden by the shell although shown), the layered alpha, its place."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetLayeredWindowAttributes.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte),
                                             ctypes.POINTER(wintypes.DWORD)]
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    ex = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
    key, alpha, flags = wintypes.DWORD(0), ctypes.c_ubyte(0), wintypes.DWORD(0)
    layered = ("alpha %d" % alpha.value) if u.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)) \
        else "per-pixel alpha"
    cloaked = wintypes.DWORD(0)
    try:
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(wintypes.HWND(hwnd), 13, ctypes.byref(cloaked), 4)
    except Exception:
        pass
    return (f"IsWindowVisible={bool(u.IsWindowVisible(hwnd))} cloaked={cloaked.value} rect=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
            f"exstyle=0x{ex:08X} ({layered})")


def _paint_state(w: QWidget) -> str:
    """How much of what the window paints is see-through (a window that paints nothing is shown, and cannot be seen)."""
    try:
        image = w.grab().toImage()
        dpr = image.devicePixelRatio() or 1.0
        total = clear = 0
        where: dict = {}
        for y in range(0, image.height(), 8):
            for x in range(0, image.width(), 8):
                total += 1
                if (image.pixel(x, y) >> 24) & 0xFF == 0:
                    clear += 1
                    lx, ly = int(x / dpr), int(y / dpr)
                    try:
                        child = w.childAt(lx, ly)
                        key = f"{type(child).__name__} {child.objectName()!r}" if child is not None else "the window itself"
                    except RuntimeError:
                        key = "(gone)"
                    box = where.setdefault(key, [lx, ly, lx, ly, 0])
                    box[0], box[1], box[2], box[3] = min(box[0], lx), min(box[1], ly), max(box[2], lx), max(box[3], ly)
                    box[4] += 1
        # (Where they are, and what is there: a see-through zone is named by the widget that should have painted it.)
        where_text = ""
        if where:
            top = sorted(where.items(), key=lambda kv: -kv[1][4])[:6]
            where_text = "; see-through zones: " + ", ".join(f"{k} x{v[4]} in ({v[0]},{v[1]})-({v[2]},{v[3]})" for k, v in top)
        return f"{clear} of {total} sampled pixels are fully transparent, opacity {w.windowOpacity():.2f}{where_text}"
    except Exception as e:
        return f"(could not be read: {e})"


def _strip_frame(hwnd: int) -> bool:
    """A window that is still framed when it is about to be shown (whatever gave the frame back): the frame is taken off at the
    system's level, and the layered style is set again, so that Qt's next update of it is the first (a window that has been
    through SetLayeredWindowAttributes refuses a per-pixel update). True when it had to be done."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    style = u.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
    if not style & 0x00C00000:                                  # WS_CAPTION
        return False

    def _signed(v: int) -> int:
        return v - (1 << 32) if v >= (1 << 31) else v
    style = (style & ~(0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000)) | 0x80000000    # no caption/frame/menu/boxes; a popup
    ex = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
    ex &= ~(0x00000001 | 0x00000100)                            # WS_EX_DLGMODALFRAME, WS_EX_WINDOWEDGE
    u.SetWindowLongW(hwnd, -20, _signed(ex & ~0x00080000))      # not layered ...
    u.SetWindowLongW(hwnd, -16, _signed(style))
    u.SetWindowLongW(hwnd, -20, _signed(ex | 0x00080000))       # ... and layered again
    u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010)    # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER|NOACTIVATE
    return True


def log_windows(why: str) -> None:
    """Every visible window of this process as the system has it - what exists, where, in which band, whether it lets the mouse
    through. Written when something is reported (a kick, a zone that takes the mouse after a notice is gone)."""
    try:
        import ctypes
        from ctypes import wintypes
        u = _user32()
        lines = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [proto, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]

        def _each(hwnd, _lparam):
            try:
                if u.IsWindowVisible(hwnd):
                    pid, cls = _describe_window(hwnd)
                    if pid == os.getpid():
                        r = wintypes.RECT()
                        u.GetWindowRect(hwnd, ctypes.byref(r))
                        ex = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
                        lines.append(f"    {cls!r} band {win_band_hook.window_band(int(hwnd))} rect=({r.left},{r.top},{r.right},{r.bottom}) "
                                     f"exstyle=0x{ex:08X}{' lets the mouse through' if ex & 0x20 else ''}")
            except Exception:
                pass
            return True
        keep = proto(_each)
        u.EnumWindows(keep, 0)
        # (INFO when a game has just lost the front - the list that names the culprit; otherwise it is only for whoever asks for DEBUG)
        logger.log(logging.INFO if "lost the front" in why else logging.DEBUG,
                   "Exclusive session: our visible windows (%s):\n%s", why, "\n".join(lines[:40]) or "    (none)")
    except Exception:
        logger.debug("exclusive session: could not list our windows", exc_info=True)


def _window_on_top_at_centre(w: QWidget) -> tuple:
    """(is it w itself, class of the window the system finds at the middle of w)."""
    c = w.mapToGlobal(w.rect().center())
    dpr = w.devicePixelRatioF()
    from ctypes import wintypes
    u = _user32()
    h = u.WindowFromPoint(wintypes.POINT(round(c.x() * dpr), round(c.y() * dpr)))
    root = u.GetAncestor(h, 2) or h
    if not root:
        return False, "(nothing)"
    return int(root) == int(w.winId()), _describe_window(root)[1]


_DIALOG_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
_TICK_MS = 80
_MOUSE_MS = 16                       # short enough not to miss a click
_STRIP_H = 26


class TitleStrip(QWidget):
    """The title bar of a window that has no frame: its title, a close button, and a place to take hold of it."""

    def __init__(self, win: QWidget, title: str, on_close: Callable[[], None]):
        super().__init__(win)
        from ui.helpers import scaled
        from ui.styles.theme import palette
        self.setObjectName("xs_strip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._on_close = on_close
        try:
            get_session()._register_closable(self)
        except Exception:
            pass
        self._press = None
        h = int(scaled(_STRIP_H, win))
        self.setFixedHeight(h)
        self.setStyleSheet(
            f"QWidget#xs_strip{{background:{palette('bg_elevated')};border-bottom:1px solid {palette('border_subtle')};}}"
            f"QLabel#xs_title{{color:{palette('text_secondary')};font-size:{scaled(11, win)}px;background:transparent;}}"
            f"QPushButton#xs_close{{border:none;background:transparent;color:{palette('text_muted')};"
            f"font-size:{scaled(13, win)}px;padding:0 {scaled(10, win)}px;}}"
            f"QPushButton#xs_close:hover{{background:{palette('error')};color:#fff;}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(int(scaled(10, win)), 0, 0, 0)
        lay.setSpacing(0)
        self._label = QLabel(title)
        self._label.setObjectName("xs_title")
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._close = QPushButton("✕")
        self._close.setObjectName("xs_close")
        self._close.setFixedHeight(h)
        self._close.setCursor(Qt.CursorShape.ArrowCursor)
        self._close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._close.clicked.connect(lambda: on_close())
        lay.addWidget(self._label, 1)
        lay.addWidget(self._close)
        win.installEventFilter(self)
        self.fit()
        self.raise_()
        self.show()

    @property
    def _win(self):
        """The window the strip is on, as it is now (a dialog adopted while Qt is still constructing it is known to its creator by a
        temporary Python object that is deleted when the constructor returns: the window is asked of the strip's parent)."""
        return self.parentWidget()

    def close_window(self) -> None:
        self._on_close()

    def fit(self) -> None:
        self.setGeometry(0, 0, self._win.width(), self.height())
        self.raise_()

    def set_title(self, title: str) -> None:
        self._label.setText(title)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Show):      # (installed on the window only: see _PaintOwnBackground)
            self.fit()
        return False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press = ev.globalPosition().toPoint() - self._win.pos()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._press is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(ev.globalPosition().toPoint() - self._press)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._press = None


def add_title_strip(win: QWidget, title: str, on_close: Callable[[], None]) -> dict:
    """Give *win* a title strip on top and make room for it. Returns what remove_title_strip needs."""
    strip = TitleStrip(win, title, on_close)
    m = win.contentsMargins()
    win.setContentsMargins(m.left(), m.top() + strip.height(), m.right(), m.bottom())
    return {"strip": strip, "margins": m}


def remove_title_strip(win: QWidget, state: Optional[dict]) -> None:
    if not state:
        return
    try:
        win.removeEventFilter(state["strip"])
        state["strip"].hide()
        state["strip"].deleteLater()
        m = state["margins"]
        win.setContentsMargins(m.left(), m.top(), m.right(), m.bottom())
    except RuntimeError:
        pass


class _DialogPointer(QObject):
    """A dialog made for a game is no use without a pointer, and the game may have hidden its own: the overlay that opened it lets go
    of the pointer a moment later (it hides after any button), so the dialog holds one for as long as it is on screen."""

    def __init__(self, dlg: QWidget):
        super().__init__(dlg)
        self._dlg = dlg
        key = f"dialog:{id(dlg)}"
        self._key = key
        dlg.installEventFilter(self)
        # (not a method of this object: it goes with the dialog, and the release must still run)
        dlg.destroyed.connect(lambda *_: _DialogPointer._let_go(key))

    @staticmethod
    def _let_go(key: str) -> None:
        try:
            from ui.helpers import SystemCursor
            SystemCursor.release(key)
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        et = ev.type()                                                  # (installed on the dialog only: see _PaintOwnBackground)
        try:
            if et == QEvent.Type.Show:
                from ui.helpers import SystemCursor
                SystemCursor.hold(self._key)
            elif et == QEvent.Type.Hide:
                self._let_go(self._key)
        except Exception:
            pass
        return False


class _FillHoles(QWidget):
    """A transparent child on top of every other widget of a dialog made for a game, that paints the theme's background BEHIND whatever
    has been drawn (CompositionMode_DestinationOver), so it lands only where nothing is. The dialog's window is translucent only so
    that it can be layered, and wherever nothing paints the game shows through: the real file picker had gaps of 9 %, named by the
    log as "the window itself" (between its widgets) and the rounded corners of its buttons - which neither the dialog's own
    background nor a widget behind the others had covered. A fill that goes on last cannot be undone by what was drawn before it."""

    def __init__(self, dlg: QWidget):
        super().__init__(dlg)
        self.setObjectName("xs_holes")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # The application's style sheet gives every QWidget a background (QWidget { background-color: ... }): this one paints none of its
        # own - Qt must not paint one for it, or the whole dialog would be covered. (Both: the attribute stops Qt from painting any
        # background for the widget, the rule says transparent should something paint it all the same.)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setStyleSheet("QWidget#xs_holes{background:transparent;border:none;}")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        dlg.installEventFilter(self)
        self._fit()

    @property
    def _dlg(self):
        return self.parentWidget()

    def _fit(self) -> None:
        try:
            self.setGeometry(self._dlg.rect())
            self.raise_()
            self.show()
        except RuntimeError:
            pass

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Show):          # (installed on the dialog only: see _PaintOwnBackground)
            self._fit()
        return False

    def paintEvent(self, _event):
        from PySide6.QtGui import QColor, QPainter
        from ui.styles.theme import palette
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOver)
        p.fillRect(self.rect(), QColor(palette("bg")))
        p.end()


def adopt_dialog(dlg: QDialog) -> None:
    """Make a dialog that is about to be shown the kind of window that does not take a game out of exclusive fullscreen."""
    from ui.exclusive_app import _PaintOwnBackground
    dlg.setProperty("xs_adopted", True)
    # A dialog that was translucent before this (a lightbox with rounded corners drawn by its own widgets) is painted as it is
    # designed; every other one is translucent only so that it can be layered, and is painted opaque first, or the game would
    # show through wherever nothing paints.
    # (Said by the dialog itself - a property - not guessed from its attribute: a dialog is adopted while it is still being built, by
    # then something may have set the attribute for another reason, and a fill that is skipped leaves the gaps the log measured.)
    designed_translucent = bool(dlg.property("xs_designed_translucent"))
    translucent_attribute = dlg.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    # (the attributes first: the flags make Qt build the native window, which has to know by then that it is translucent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    dlg.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    dlg.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    # Two ways a dialog ends up in the ordinary band, and a window there takes the game to the desktop:
    #  - its native window was made before this (a title bar colour asked for, a handle asked for in the constructor): the flags
    #    change its style but not its band, so it is dropped and made again when it is shown;
    #  - it has an owner (the main window, hidden in the tray, is the owner of most of them): a window with an owner is made by the
    #    system in the owner's band, and the hook leaves it alone. Without one it is a window of the higher band like any other.
    # A third: a window made FRAMED and then made frameless and translucent keeps its frame (the system's own description of the [i]
    # panel in a real game: a rect 16 px wider and 39 px taller than the widget, the dialog-frame bits in its style) - the frame is
    # painted by Windows, the inside by Qt's per-pixel layer, and that window is visible, in band 16, on top, "painted", and shows
    # nothing. So the window is ALWAYS made new, from its final flags, as the application window is.
    owner = dlg.parentWidget()
    try:
        if dlg.windowHandle() is not None:
            dlg.destroy()
    except RuntimeError:
        pass
    dlg.setParent(None, _DIALOG_FLAGS)
    try:
        # (it is made now, inside band mode: Qt shows a window that exists)
        with win_band_hook.band_windows():
            dlg.create()
    except Exception:
        logger.debug("exclusive session: could not make the dialog's window in the higher band", exc_info=True)
    _keep_alive(dlg)
    logger.debug("Exclusive session: %s adopted (it was owned by %s; translucent by design: %s, translucent attribute before this: %s)",
                 type(dlg).__name__, type(owner).__name__ if owner is not None else "nothing", designed_translucent, translucent_attribute)
    dlg.installEventFilter(_PaintOwnBackground(dlg, fill=not designed_translucent))
    if not designed_translucent:
        _FillHoles(dlg)
    _DialogPointer(dlg)
    key = f"window-{id(dlg)}"
    dlg.setProperty("xs_hold", key)
    add_title_strip(dlg, dlg.windowTitle(), lambda k=key: _reject_live(k))
    dlg.windowTitleChanged.connect(lambda text, k=key: _retitle(k, text))
    hold_open(dlg)

    # The polish that adopts a dialog comes while it is still being BUILT (laying it out polishes it), and the code that opens it
    # then asks for its own window flags - a title bar, a close button - which gave the frame back: the [i] panel in a real game was
    # adopted, and its window was a framed one all the same (16 px wider and 39 taller than the widget, the dialog-frame bits in its
    # style). Over a game these calls are ignored; the window is the one made here.
    def _keep_flags(*_args, **_kwargs):
        return None
    try:
        dlg.setWindowFlags = _keep_flags
        dlg.setWindowFlag = _keep_flags
    except Exception:
        logger.debug("exclusive session: could not keep a dialog's flags", exc_info=True)


_kept_dialogs: list = []


def _keep_alive(dlg: QWidget) -> None:
    """A dialog that had an owner was kept alive by it; without one, only a reference of ours does (the code that opened it often
    lets go of its own when the function returns, relying on the owner). Dropped when Qt deletes the dialog."""
    _kept_dialogs.append(dlg)

    def _gone(*_):
        try:
            _kept_dialogs.remove(dlg)
        except ValueError:
            pass
    try:
        dlg.destroyed.connect(_gone)
    except RuntimeError:
        pass


def hold_open(widget: QWidget, name: str = "") -> None:
    """Keep the session on for as long as *widget* is visible. The overlay that opened a window is gone a moment later (it
    hides after any button), and the session with it unless the window holds it open: the sweep at the end of a session
    drops the higher-band windows still around."""
    key = name or f"window-{id(widget)}"
    widget.setProperty("xs_hold", key)                 # (found again by it: see _live_widget)
    born = time.monotonic()

    def _showing() -> bool:
        try:
            w = _live_widget(key)
            # (A dialog is adopted while it is still being built, a good while before it is shown: it is not "gone" in that time.)
            return (w is not None and w.isVisible()) or time.monotonic() - born < 5.0
        except RuntimeError:
            return False

    get_session().begin(key, _showing)


def _live_widget(key: str):
    """The top-level window that was given *key* (hold_open), as it is NOW. A dialog adopted while Qt is still constructing it (the file
    pickers) is known to the adoption by a temporary Python object, deleted when the constructor returns; what it kept - a weak reference,
    a bound method - goes dead with it. The window is found again by the property it was given."""
    for w in QApplication.topLevelWidgets():
        try:
            if w.property("xs_hold") == key:
                return w
        except RuntimeError:
            continue
    return None


def _reject_live(key: str) -> None:
    w = _live_widget(key)
    if w is not None:
        try:
            w.reject()
        except (RuntimeError, AttributeError):
            pass


@contextmanager
def window_scope(name: str, backdrop: bool = False):
    """For the length of a call that opens one window after another (a picker, then a question about a file that exists, then
    another): the windows it opens are made for the game, and the session does not end in the moment between one window
    closing and the next opening - there, nothing else would be holding it.

    With *backdrop* the game is dimmed behind these windows - the vignette Open App and the [i] panel use - and the pointer is
    kept for the length of the scope: a picker is no use without one, and the game may have hidden its own."""
    live = [True]
    session = get_session()
    session.begin(name, lambda: live[0])
    if backdrop:
        session.show_backdrop(name)
    try:
        yield
    finally:
        live[0] = False
        if backdrop:
            session.hide_backdrop(name)
        session.end(name)


def _retitle(key: str, text: str) -> None:
    d = _live_widget(key)
    if d is None:
        return
    try:
        strip = d.findChild(TitleStrip)
        if strip is not None:
            strip.set_title(text)
    except RuntimeError:
        pass


class ExclusiveSession(QObject):
    def __init__(self):
        super().__init__()
        self._owners = {}                                   # name -> (still_needed, makes_windows)
        self._mode_on = False
        self._filtering = False
        self._native_dialogs = None
        self._btn_prev = False
        self._warned = set()
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(_MOUSE_MS)
        self._mouse_timer.timeout.connect(self._mouse_tick)
        self._keys = None
        self._closables = []                         # weak references to the title strips: the windows that can be closed
        self._esc_on = False                         # Escape is handed to the top closable window
        self._events = deque(maxlen=80)              # what happened lately to the windows (see _watch_front)
        self._game = 0                               # the window of the game that was in front when the session began
        self._lost_ticks = 0
        self._reported_by = -1                       # the window that took the front when it was last told
        self._last_front = 0
        self._blur = None                            # the dimmed backdrop of a window_scope(backdrop=True)
        self._blur_owners = set()
        self._pointer_owners = set()
        self._seen_popups = set()                    # the popups and tool menus open at the last tick of the mouse watch
        self._tip = None                             # the session's tooltip window (_SessionTip)
        self._tip_owner = None                       # weakref to the widget it is for

    def show_backdrop(self, owner: str) -> None:
        """The game dimmed behind the windows of *owner* and the pointer kept for them. Before the first of its windows exists, so
        that they are made after it and sit above it. Not a second backdrop on top of one that is up already (the application's)."""
        try:
            from ui import exclusive_app
            from ui.blur_modal import BlurModalWidget
            from ui.helpers import SystemCursor
            SystemCursor.hold(f"scope:{owner}")
            self._pointer_owners.add(owner)
            if any(isinstance(w, BlurModalWidget) and w is not self._blur and w.isVisible() for w in QApplication.topLevelWidgets()):
                return
            if self._blur is None:
                self._blur = BlurModalWidget()
            if exclusive_app.show_backdrop(self._blur, self._game or _front_window()[0]):
                self._blur_owners.add(owner)
                self._note(f"backdrop shown for {owner}")
        except Exception:
            logger.debug("exclusive session: could not show the backdrop", exc_info=True)

    def hide_backdrop(self, owner: str) -> None:
        try:
            if owner in self._pointer_owners:
                self._pointer_owners.discard(owner)
                from ui.helpers import SystemCursor
                SystemCursor.release(f"scope:{owner}")
            if owner in self._blur_owners:
                self._blur_owners.discard(owner)
                if not self._blur_owners and self._blur is not None:
                    from ui import exclusive_app
                    exclusive_app.hide_backdrop(self._blur)
        except Exception:
            logger.debug("exclusive session: could not take the backdrop away", exc_info=True)

    def _note(self, text: str) -> None:
        self._events.append((time.monotonic(), text))

    @property
    def keys(self):
        if self._keys is None:
            from ui.exclusive_keys import ExclusiveKeys
            self._keys = ExclusiveKeys(self._is_hotkey, self._hotkey_modifiers, self)
        return self._keys

    def active(self) -> bool:
        return bool(self._owners)

    # ── begin / end ──────────────────────────────────────────────────────────────────────────────────

    def begin(self, owner: str, still_needed: Callable[[], bool], windows: bool = True) -> None:
        if platform.system() != "Windows" or not win_band_hook.installed():
            return
        first = not self._owners
        self._owners[owner] = (still_needed, windows)
        if first:
            self._start()
        self._update_mode()

    def end(self, owner: str) -> None:
        if self._owners.pop(owner, None) is None:
            return
        if not self._owners:
            self._stop()
        else:
            self._update_mode()

    def _update_mode(self) -> None:
        want = any(w for _f, w in self._owners.values())
        if want and not self._mode_on:
            win_band_hook.set_mode(True)
            self._mode_on = True
            try:
                app = QApplication.instance()
                self._native_dialogs = app.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
                app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
            except Exception:
                pass
        elif not want and self._mode_on:
            self._mode_off()

    def _mode_off(self) -> None:
        win_band_hook.set_mode(False)
        self._mode_on = False
        if self._native_dialogs is not None:
            try:
                QApplication.instance().setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, bool(self._native_dialogs))
            except Exception:
                pass
            self._native_dialogs = None

    def _start(self) -> None:
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)
            self._filtering = True
        self._btn_prev = False
        self._lost_ticks, self._reported_by, self._last_front = 0, -1, 0
        try:
            hwnd, pid, cls = _front_window()
            self._game = hwnd if (hwnd and pid != os.getpid()) else 0
            self._note(f"session began; in front: {cls!r} (process {pid}){' - this is taken as the game' if self._game else ''}")
        except Exception:
            self._game = 0
        self._timer.start()
        self._mouse_timer.start()
        for w in QApplication.topLevelWidgets():              # (windows that were already up when the session began)
            try:
                if w.isWindow() and w.isVisible():
                    w.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            except RuntimeError:
                continue
        logger.debug("Exclusive session: started (%s)", ", ".join(sorted(self._owners)))

    def _stop(self) -> None:
        self._hide_tip()
        self._timer.stop()
        self._mouse_timer.stop()
        if self._keys is not None:
            self._keys.shutdown()
        app = QApplication.instance()
        if app is not None and self._filtering:
            app.removeEventFilter(self)
            self._filtering = False
        if self._mode_on:
            self._mode_off()
        self._sweep()
        self._warned.clear()
        self._game = 0
        self._esc_on = False
        for owner in list(self._blur_owners | self._pointer_owners):
            self.hide_backdrop(owner)
        logger.debug("Exclusive session: ended")

    def _sweep(self) -> None:
        """Windows made in the higher band during the session and still around (a dialog, a menu) would stay in it for good:
        they are dropped, and Qt makes an ordinary one the next time they are shown."""
        for hwnd in list(win_band_hook._state["created"]):
            try:
                w = QWidget.find(hwnd)
                if w is None or not w.isWindow() or w.property("xs_keep"):
                    continue                                 # (the overlay, the pins, the app window manage their own)
                if win_band_hook.window_band(hwnd) == win_band_hook.BAND_SYSTEM_TOOLS:
                    w.hide()
                    w.destroy()
            except RuntimeError:
                continue
        win_band_hook._state["created"].clear()

    # ── what happens meanwhile ───────────────────────────────────────────────────────────────────────

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QEvent.Type.ToolTip:
            return self._on_tooltip_event(obj, ev)          # (a Qt tooltip is an ordinary window: ours is not)
        if et == QEvent.Type.MouseButtonPress:
            self._hide_tip()
        if et in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate) \
                and isinstance(obj, QWidget) and obj.isWindow():
            try:
                self._record_window(obj, et)
            except Exception:
                pass
        if et == QEvent.Type.MouseButtonPress:
            if isinstance(obj, QWidget):
                self._note(f"pressed on {type(obj).__name__} (in {type(obj.window()).__name__})")
                if self._toggle_off_menu_of(obj):
                    return True
                self.keys.on_mouse_press(obj)
                self._prepare_combo(obj)
            return False
        if et == QEvent.Type.ContextMenu and self._mode_on and isinstance(obj, QWidget) and self._text_context_menu(obj, ev):
            return True
        if et == QEvent.Type.Polish and self._mode_on and isinstance(obj, QDialog) and obj.isWindow() \
                and not obj.property("xs_adopted"):
            try:
                adopt_dialog(obj)
            except Exception:
                logger.warning("Exclusive session: could not adopt %s", type(obj).__name__, exc_info=True)
        elif et == QEvent.Type.Polish and self._mode_on and self._is_stray_window(obj):
            self._keep_off_screen(obj)
        elif et == QEvent.Type.ParentChange and isinstance(obj, QWidget) and obj.property("xs_stray") and not obj.isWindow():
            obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)       # it has found its parent: an ordinary widget
            obj.setProperty("xs_stray", False)
        elif et == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            # Qt asks a widget for its tooltip only when its window is the ACTIVE one (or says WA_AlwaysShowToolTips), and a window made for
            # a game never is: no title, no version, no date, no donut had a tooltip over the game.
            obj.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            # Qt rewrites the extended style of a window while it shows it and can drop the non-activating bit; the audit would put
            # it back within a tick, but a press before that takes the game out of exclusive fullscreen.
            try:
                hwnd = int(obj.winId())
                if obj.property("xs_adopted"):
                    if _strip_frame(hwnd):
                        self._note(f"frame taken off {type(obj).__name__} at the system's level")
                        logger.warning("Exclusive session: %s was about to be shown with a frame although it was adopted - the frame was "
                                       "taken off at the system's level", type(obj).__name__)
                    win_band_hook.ensure_noactivate(hwnd, layered=True)          # (an adopted dialog is translucent)
                    self._place_on_game_screen(obj)
                elif win_band_hook.window_band(hwnd) == win_band_hook.BAND_SYSTEM_TOOLS:
                    self._make_sound(obj, hwnd)
            except Exception:
                logger.debug("exclusive session: could not fix a window's style", exc_info=True)
        return False

    # (These two are asked by the keyboard hook's own thread, for every key: they only LOOK at the manager that exists - making
    # one there would build a Qt object on the wrong thread.)
    @staticmethod
    def _hotkey_modifiers() -> set:
        try:
            import hotkeys
            mgr = hotkeys._hotkey_mgr
            return mgr.modifiers_in_use() if mgr is not None else set()
        except Exception:
            return set()

    @staticmethod
    def _is_hotkey(name: str, mods: set) -> bool:
        try:
            import hotkeys
            mgr = hotkeys._hotkey_mgr
            return mgr.chord_matches(name, mods) if mgr is not None else False
        except Exception:
            return False

    def _tick(self) -> None:
        for name, (needed, _w) in list(self._owners.items()):
            try:
                ok = bool(needed())
            except Exception:
                ok = False
            if not ok:
                logger.debug("Exclusive session: %s no longer needs it", name)
                self.end(name)
        if not self._owners:
            return
        try:
            self._audit()
            self._watch_front()
            self._sync_escape()
        except Exception:
            logger.debug("exclusive session: tick failed", exc_info=True)

    def _mouse_tick(self) -> None:
        try:
            self._watch_mouse()
        except Exception:
            logger.debug("exclusive session: mouse watch failed", exc_info=True)

    def _watch_mouse(self) -> None:
        import ctypes
        down = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000 or ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000)
        edge = down and not self._btn_prev
        self._btn_prev = down
        self._tip_still_wanted()
        keys = self._keys
        if keys is not None and keys._target is not None and not keys.armed():
            keys.disarm("the field is gone")
        # a popup is closed by a press outside it (Qt would have captured the mouse for that; it cannot without the foreground)
        pops = [w for w in QApplication.topLevelWidgets()
                if w.isVisible() and (w.windowType() == Qt.WindowType.Popup or w.property("xs_toolpopup"))]
        # A popup that was not there at the last tick was opened by the press this tick is looking at (a combo box opens its list while it is
        # being pressed): that press is not a press OUTSIDE it - without this the list or menu was closed the moment it appeared, and the
        # box "never opened".
        now = {id(p) for p in pops}
        fresh, self._seen_popups = now - self._seen_popups, now
        if not edge:
            return
        pos = QCursor.pos()

        def _inside(p) -> bool:
            if p.frameGeometry().contains(pos):
                return True
            owner = p.parentWidget()                               # (a press on the combo box that owns the list is the combo box's: it toggles)
            return bool(p.property("xs_toolpopup") and isinstance(owner, QComboBox) and owner.rect().contains(owner.mapFromGlobal(pos)))
        if pops and not any(_inside(p) for p in pops):
            for p in pops:
                if id(p) in fresh:
                    continue
                if self._is_combo_list(p):
                    p.parentWidget().hidePopup()
                else:
                    p.close()
        # typing goes on only while the pointer works inside our own windows
        if keys is not None and keys.armed():
            hit = QApplication.widgetAt(pos)
            if hit is None or win_band_hook.window_band(int(hit.window().winId())) != win_band_hook.BAND_SYSTEM_TOOLS:
                keys.disarm("pressed outside")

    def _register_closable(self, strip) -> None:
        self._closables = [r for r in self._closables if r() is not None]
        self._closables.append(weakref.ref(strip))

    def _top_closable(self):
        for ref in reversed(self._closables):
            s = ref()
            try:
                if s is not None and s.isVisible() and s._win.isVisible():
                    return s
            except RuntimeError:
                continue
        return None

    def _close_top(self) -> None:
        """Escape: close the window that is on top - the application window, a dialog - as its ✕ would."""
        lists = self._tool_popups()
        if lists:
            self._note("Escape closes a combo box's list or a menu")
            if self._is_combo_list(lists[-1]):
                lists[-1].parentWidget().hidePopup()
            else:
                lists[-1].close()
            return
        s = self._top_closable()
        if s is None:
            return
        self._note("Escape closes " + type(s._win).__name__)
        s.close_window()

    def _sync_escape(self) -> None:
        """Escape is taken (ui.exclusive_keys) for exactly as long as a window that can be closed is up."""
        has = self._top_closable() is not None or bool(self._tool_popups())
        if has == self._esc_on:
            return
        self._esc_on = has
        self.keys.set_escape_handler(self._close_top if has else None)

    def _watch_front(self) -> None:
        """The game that was in front when the session began is no longer: whatever took it (or minimised it) is written to the log
        with the last seconds of what happened to our windows - so a report of "it kicked me to the desktop" names the window."""
        game = self._game
        if not game:
            return
        u = _user32()
        if not u.IsWindow(game):
            self._game = 0
            return
        hwnd, pid, cls = _front_window()
        if hwnd != self._last_front:
            self._last_front = hwnd
            self._note(f"in front: {cls!r} (process {pid}{', ours' if pid == os.getpid() else ''})")
        minimised = bool(u.IsIconic(game))
        lost = minimised or hwnd != game
        if not lost:
            self._lost_ticks, self._reported_by = 0, -1
            return
        self._lost_ticks += 1
        if self._lost_ticks < 2 or self._reported_by == hwnd:     # (a flicker of one tick is not a loss; told once per window)
            return
        self._reported_by = hwnd
        now = time.monotonic()
        story = "\n".join(f"    {now - t:5.2f}s ago: {text}" for t, text in list(self._events) if now - t < 6.0)
        logger.warning("Exclusive session: the game lost the front - in front now: %r (process %s%s), the game is minimised: %s. "
                       "What happened to our windows in the last seconds:\n%s", cls, pid, ", ours" if pid == os.getpid() else "",
                       minimised, story or "    (nothing recorded)")
        log_windows("when the game lost the front")

    def _text_context_menu(self, obj, ev) -> bool:
        """The menu of a text field (cut, copy, paste ...) is made inside Qt and shown as a popup: it is built here and shown as a tool window
        instead. True when it was taken over (the event is then consumed)."""
        field = obj
        for _ in range(3):
            if field is None or isinstance(field, (QLineEdit, QTextEdit, QPlainTextEdit)):
                break
            field = field.parentWidget()
        if not isinstance(field, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return False
        try:
            if win_band_hook.window_band(int(field.window().winId())) != win_band_hook.BAND_SYSTEM_TOOLS:
                return False
            menu = field.createStandardContextMenu()
            if menu is None:
                return False
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            menu.popup(ev.globalPos())                     # (the handling above turns it into a tool window)
            return True
        except Exception:
            logger.debug("exclusive session: could not show a text field's menu", exc_info=True)
            return False

    @staticmethod
    def _python_event_override(obj) -> bool:
        """Does a class of the application (not Qt) define event() for this widget? The donut chart does, to answer ToolTip with the slice under
        the pointer."""
        for k in type(obj).__mro__:
            if k.__module__.startswith("PySide6") or k.__module__ in ("shiboken6", "builtins"):
                return False
            if "event" in k.__dict__:
                return True
        return False

    def _on_tooltip_event(self, obj, ev) -> bool:
        """A tooltip was asked for: shown in the session's own window, and the event is taken (always True). The text is the widget's tooltip, an
        item's (a list or table: Qt shows those itself, as a window, from inside) or what the widget's own event() makes of it (it then calls
        QToolTip.showText, which shows the same window)."""
        try:
            if not isinstance(obj, QWidget):
                return True
            text, owner = obj.toolTip(), obj
            if not text:
                view = obj.parentWidget()
                if isinstance(view, QAbstractItemView) and view.viewport() is obj:
                    data = view.indexAt(ev.pos()).data(Qt.ItemDataRole.ToolTipRole)
                    text = str(data) if data else ""
                elif self._python_event_override(obj):
                    obj.event(ev)                           # (its own handling: shows through QToolTip.showText, or hides)
                    if ev.isAccepted():
                        return True
                if not text:
                    # Qt hands a tooltip request that a widget leaves unanswered to its parent, and so on up to the window (a label inside
                    # a card that has the tooltip); the tooltip of whichever has one is the one shown.
                    w = obj.parentWidget()
                    while w is not None and not text:
                        text, owner = w.toolTip(), w
                        w = None if w.isWindow() else w.parentWidget()
            if text:
                self._show_tip(ev.globalPos(), text, owner)
            # (Nothing to say is not a reason to hide: a widget that shows its tooltip itself, from a mouse move - the bar chart - is
            # asked a moment after the pointer rests, answers nothing, and its tooltip was hidden by that: a flash, and a tooltip only
            # while the mouse moved. One that is not wanted any more goes with the pointer: _tip_still_wanted.)
        except Exception:
            logger.debug("exclusive session: could not show a tooltip", exc_info=True)
        return True

    def _show_tip(self, pos, text: str, owner=None) -> None:
        """*owner*: the widget the tooltip is for. Qt's own tooltip goes when the pointer leaves the widget; this one goes with it (_watch_mouse)."""
        for _ in range(2):
            try:
                if self._tip is None:
                    self._tip = _SessionTip()
                self._tip.show_text(pos, text)
                self._tip_owner = weakref.ref(owner) if owner is not None else None
                return
            except RuntimeError:
                self._tip = None                            # (its window was swept at the end of a session, or deleted: made again)

    def _tip_still_wanted(self) -> None:
        """The tooltip is hidden once the pointer is not over the widget it was shown for."""
        tip = self._tip
        if tip is None or self._tip_owner is None:
            return
        try:
            if not tip.isVisible():
                return
            owner = self._tip_owner()
            if owner is None or not owner.isVisible() or not owner.rect().contains(owner.mapFromGlobal(QCursor.pos())):
                tip.hide()
        except RuntimeError:
            self._tip = None

    def _hide_tip(self) -> None:
        tip = self._tip
        if tip is not None:
            try:
                tip.hide()
            except RuntimeError:
                self._tip = None

    @staticmethod
    def _is_combo_list(p) -> bool:
        """A combo box's own list (made a tool window by _prepare_combo), as opposed to a menu that has a combo box for its parent: the
        library's sort box is a QComboBox whose showPopup() shows a QMenu, and whose hidePopup() does nothing - closing that menu by
        hidePopup() left it open, and every press on the box made another on top of it (the log: four, then six identical windows)."""
        try:
            return bool(p.property("xs_toolpopup") and isinstance(p.parentWidget(), QComboBox) and not isinstance(p, QMenu))
        except RuntimeError:
            return False

    def _toggle_off_menu_of(self, obj) -> bool:
        """A press on the widget a menu was opened from closes the menu and is not passed on (as with a Qt popup): otherwise the press
        opens the same menu again on top of it."""
        owners = []
        w = obj
        for _ in range(4):
            if w is None:
                break
            owners.append(w)
            w = w.parentWidget()
        for p in self._tool_popups():
            if obj.window() is p:
                continue                                            # (a press INSIDE the menu - on an entry - is the menu's own)
            if isinstance(p, QMenu) and any(p.parentWidget() is o for o in owners):
                p.close()
                self._note("a press on the owner of a menu closed it")
                return True
        return False

    @staticmethod
    def _tool_popups() -> list:
        """The lists of combo boxes that are open as tool windows (see _prepare_combo)."""
        found = []
        for w in QApplication.topLevelWidgets():
            try:
                if w.property("xs_toolpopup") and w.isVisible():
                    found.append(w)
            except RuntimeError:
                continue
        return found

    def _prepare_combo(self, obj) -> None:
        """A combo box's list is a Qt popup, and a popup takes the foreground when it opens (measured: see ui.overlay._popup_menu) - over a
        game in exclusive fullscreen that is the game taken to the desktop (the log: the file picker's "Files of type" box). Before Qt opens
        the list it is made what the overlay's menus are: a tool window of the higher band that never takes the focus, translucent and
        painted by us like the dialogs, without the shadow window the system makes for a popup. Being no popup, Qt does not close it when
        something else is pressed: _watch_mouse and Escape do (it closes itself when an entry is chosen)."""
        combo = obj
        for _ in range(3):
            if combo is None or isinstance(combo, QComboBox):
                break
            combo = combo.parentWidget()
        if not isinstance(combo, QComboBox):
            return
        try:
            if win_band_hook.window_band(int(combo.window().winId())) != win_band_hook.BAND_SYSTEM_TOOLS:
                return                                         # (a window of the ordinary band: not over a game)
            box = combo.view().window()
            if box is combo.window() or box.property("xs_toolpopup") or box.isVisible():
                return
            from ui.exclusive_app import _PaintOwnBackground
            box.setProperty("xs_toolpopup", True)
            box.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                               | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
            box.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            box.installEventFilter(_PaintOwnBackground(box, fill=True))
            _FillHoles(box)
            self._note("a combo box's list was made a tool window (it is not shown as a popup)")
        except Exception:
            logger.debug("exclusive session: could not prepare a combo box's list", exc_info=True)

    @staticmethod
    def _make_sound(w, hwnd: int) -> bool:
        """A window of the higher band must be non-activating AND layered, or it takes the game to the desktop. Qt takes both away
        from a window it paints in the ordinary way (the auto-complete list of the picker's name box, a combo box's list ... and a pin
        remade in a hurry: the system's list of our windows in a real game showed one with no layered bit). A translucent window is
        layered by Qt itself; an opaque one is made layered here, with an alpha of 255 so that it is still seen. True when it changed."""
        import ctypes
        from ctypes import wintypes
        u = _user32()
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        before = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
        translucent = w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        win_band_hook.ensure_noactivate(hwnd, layered=translucent)
        if not translucent:
            win_band_hook.ensure_layered(hwnd)
        return (u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF) != before

    @staticmethod
    def _is_stray_window(w) -> bool:
        """A plain widget with no parent, no title and no window type of its own: nobody meant it to be a window - a label
        built before it is laid out and shown too early. Over a game in exclusive fullscreen that is a window that takes the
        game to the desktop (the log of a real game: dozens of them in the moment before it was). Dialogs, the application
        window, menus, and everything that is made a tool/popup/tooltip window on purpose are not touched."""
        try:
            return (isinstance(w, QWidget) and not isinstance(w, (QDialog, QMainWindow, QMenu)) and w.isWindow()
                    and w.parentWidget() is None and w.windowType() == Qt.WindowType.Window
                    and not w.windowTitle() and not w.property("xs_keep") and not w.property("xs_stray"))
        except RuntimeError:
            return False

    def _keep_off_screen(self, w) -> None:
        w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)          # Qt then never shows its native window
        w.setProperty("xs_stray", True)
        key = ("stray", type(w).__name__)
        self._note(f"kept off the screen: {type(w).__name__} (a window nobody asked for)")
        if key not in self._warned:
            self._warned.add(key)
            logger.debug("Exclusive session: %s was about to be shown as a window of its own (no parent, no window type) - kept "
                         "off the screen, it would take the game to the desktop", type(w).__name__)

    def _record_window(self, w: QWidget, et) -> None:
        name = type(w).__name__
        if et == QEvent.Type.Hide:
            self._note(f"hide {name}")
            return
        if et == QEvent.Type.WindowActivate:
            self._note(f"ACTIVATED {name}")
            return
        if et == QEvent.Type.WindowDeactivate:
            self._note(f"deactivated {name}")
            return
        hwnd = int(w.winId())
        band = win_band_hook.window_band(hwnd)
        owner = w.parentWidget()
        g = w.geometry()
        scr = w.screen()
        self._note(f"show {name} band={band} owner={type(owner).__name__ if owner is not None else '-'} {g.width()}x{g.height()} "
                   f"at {g.x()},{g.y()} on {scr.name() if scr is not None else '?'}")
        if band is not None and band != win_band_hook.BAND_SYSTEM_TOOLS:
            key = ("shown", name, w.objectName())
            if key not in self._warned:
                self._warned.add(key)
                logger.warning("Exclusive session: %s was shown in band %s (owned by %s) - an ordinary window takes the game back to "
                               "the desktop", name, band, type(owner).__name__ if owner is not None else "nothing")

    def _place_on_game_screen(self, dlg: QWidget) -> None:
        """A dialog made for the game belongs on the game's monitor - where the dimmed backdrop is. Its place was worked out before
        it lost its owner, from the main window, which is on the primary monitor."""
        try:
            game = self._game or _front_window()[0]
            if not game:
                return
            from ui import exclusive_app
            screen = exclusive_app.screen_of_game(game)
            if screen is None:
                return
            geo = screen.geometry()
            frame = dlg.frameGeometry()
            if not geo.contains(frame.center()):
                frame.moveCenter(geo.center())
                dlg.move(frame.topLeft())
            ref = weakref.ref(dlg)

            def _report():
                d = ref()
                try:
                    if d is None:
                        return
                    itself, top = _window_on_top_at_centre(d) if d.isVisible() else (False, "(not visible)")
                    g = d.geometry()
                    # (Only a window that is wrong is worth INFO: see-through zones, not in the higher band, not on the game's monitor,
                    # something else on top of it.)
                    painted = _paint_state(d)
                    band = win_band_hook.window_band(int(d.winId()))
                    fine = (itself and band == win_band_hook.BAND_SYSTEM_TOOLS and "see-through zones" not in painted
                            and (d.screen() is None or d.screen().name() == screen.name()))
                    level = logging.DEBUG if fine else logging.INFO
                    logger.log(level, "Exclusive session: %s as the system sees it: %s; painted: %s", type(d).__name__,
                               _win32_state(int(d.winId())), painted)
                    logger.log(level, "Exclusive session: %s, 0.6 s after it was shown: visible=%s, band %s, %dx%d at %d,%d on %s (the game is on %s); "
                               "at its centre the window on top is %s", type(d).__name__, d.isVisible(), band,
                               g.width(), g.height(), g.x(), g.y(), d.screen().name() if d.screen() else "?", screen.name(),
                               "itself" if itself else f"ANOTHER ({top})")
                except RuntimeError:
                    pass
            QTimer.singleShot(600, _report)
        except Exception:
            logger.debug("exclusive session: could not place a dialog on the game's screen", exc_info=True)

    def _audit(self) -> None:
        """A visible window of ours that is not in the higher band is about to take the game back to the desktop (or has):
        said once in the log, so a report names it."""
        for w in QApplication.topLevelWidgets():
            if not w.isVisible() or not w.isWindow():
                continue
            key = (type(w).__name__, w.objectName())
            try:
                band = win_band_hook.window_band(int(w.winId()))
            except Exception:
                continue
            if band == win_band_hook.BAND_SYSTEM_TOOLS:
                if self._make_sound(w, int(w.winId())) and key not in self._warned:
                    self._warned.add(key)
                    logger.warning("Exclusive session: %s was on screen in the higher band but a click could activate it - made "
                                   "non-activating", key[0])
            elif band is not None and key not in self._warned:
                self._warned.add(key)
                owner = w.parentWidget()
                logger.warning("Exclusive session: %s is on screen and is not a window of the higher band (band %s, owned by %s) - "
                               "it takes the game back to the desktop", key[0], band,
                               type(owner).__name__ if owner is not None else "nothing")


_session: Optional[ExclusiveSession] = None


_menu_patch_done = False
_tooltip_patch_done = False


class _SessionTip(QLabel):
    """A tooltip that is not an ordinary window. Qt's own (QTipLabel) is one, and an ordinary window takes a game in exclusive fullscreen to the
    desktop - the overview's chart tooltips did (the log: a ToolTip-class window on screen a moment before the game was taken away). This
    one is made as the dialogs and the menus are: a tool window that never takes the focus or the mouse, translucent and painted by us,
    without the shadow window the system makes for a popup."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setProperty("xs_tip", True)
        self.setWordWrap(True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_text(self, pos: QPoint, text: str) -> None:
        from ui.helpers import scaled
        from ui.styles.theme import palette
        self.setStyleSheet(f"QLabel{{background:transparent;border:none;color:{palette('text')};font-size:{scaled(11, self)}px;"
                           f"padding:{scaled(5, self)}px {scaled(8, self)}px;}}")
        self.setMaximumWidth(scaled(420, self))
        self.setText(text)
        self.adjustSize()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        x, y = pos.x() + 16, pos.y() + 20
        if screen is not None:
            geo = screen.availableGeometry()
            if x + self.width() > geo.right():
                x = pos.x() - self.width() - 8
            if y + self.height() > geo.bottom():
                y = pos.y() - self.height() - 8
            x, y = max(geo.left(), x), max(geo.top(), y)
        self.move(x, y)
        self.show()
        self.raise_()
        self._timer.start(6000)

    def paintEvent(self, event):
        from ui.styles.theme import palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(QColor(palette("bg_elevated")))
        p.setPen(QColor(palette("border")))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 5, 5)
        p.end()
        super().paintEvent(event)                                  # (the text)


def _install_tooltip_patch() -> None:
    """The session takes over the tooltips (a Qt tooltip is an ordinary window, and an ordinary window takes the game to the desktop):
    Qt's own tooltip events are answered by ExclusiveSession._on_tooltip_event, and code that asks for one itself - the overview's charts do,
    from their mouse-move handlers - bypassed that (the log: a ToolTip-class window, 165 x 5 px, on screen a moment before the game was
    taken to the desktop). While a session is on QToolTip.showText and hideText act on the session's own tooltip window instead."""
    global _tooltip_patch_done
    if _tooltip_patch_done or platform.system() != "Windows":
        return
    _tooltip_patch_done = True
    orig_show, orig_hide = QToolTip.showText, QToolTip.hideText

    def _show(*args, **kwargs):
        s = _session
        if s is not None and s._mode_on:
            try:
                if len(args) >= 2 and args[1]:
                    owner = args[2] if len(args) > 2 and isinstance(args[2], QWidget) else QApplication.widgetAt(args[0])
                    s._show_tip(args[0], str(args[1]), owner)
                else:
                    s._hide_tip()
            except Exception:
                logger.debug("exclusive session: could not show a tooltip", exc_info=True)
            return None
        return orig_show(*args, **kwargs)

    def _hide(*args, **kwargs):
        s = _session
        if s is not None and s._mode_on:
            s._hide_tip()
            return None
        return orig_hide(*args, **kwargs)
    QToolTip.showText = staticmethod(_show)
    QToolTip.hideText = staticmethod(_hide)


def _menu_wants_tool(menu) -> bool:
    """Is *menu* about to be shown as a Qt popup from a window made for a game?"""
    s = _session
    if s is None or not s._mode_on or menu.property("xs_toolpopup"):
        return False
    if menu.windowType() != Qt.WindowType.Popup:
        return False                                       # (the overlay's menus are tool windows already)
    parent = menu.parentWidget()
    if parent is None:
        return True
    return win_band_hook.window_band(int(parent.window().winId())) == win_band_hook.BAND_SYSTEM_TOOLS


def _make_menu_tool(menu) -> None:
    """The same treatment as a combo box's list (ExclusiveSession._prepare_combo) and the overlay's menus: a tool window that never takes the
    focus, translucent, without the shadow window the system makes for a popup."""
    menu.setProperty("xs_toolpopup", True)
    menu.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                        | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # Qt closes a menu when an entry is chosen only if it is a popup (the overlay's tool menus close themselves for the same reason): without
    # this a chosen entry ran its action and the menu stayed on screen until something else closed it.
    menu.triggered.connect(lambda *_, m=menu: m.hide())


def _finish_menu(menu) -> None:
    win_band_hook.ensure_noactivate(int(menu.winId()), layered=True)       # (Qt drops the non-activating bit as it shows the window)
    from ui.helpers import note_tool_menu
    note_tool_menu(menu)                                   # (so that the overlay and the pins do not climb back over it)
    get_session()._note("a menu was shown as a tool window (it is not shown as a popup)")


def _install_menu_patch() -> None:
    """Every Qt menu in a window made for a game is shown as a tool window, not as a popup: the file picker's right-click menu took the game
    to the desktop (the log: two QMenus with the system's shadow window beside each, and the game lost the front a moment later).

    A menu made in Python is made one as soon as it is constructed (replacing QMenu.exec does not reach the calls on an instance in
    PySide6, so the menu is changed before anyone asks it to show): menu.exec(pos) then stays what it was - it returns when the menu is
    gone, with the entry chosen, and the menu closes as it always did when an entry is chosen. A menu made inside Qt (a text field's) is
    changed when it is popped up. Being no popup, Qt does not close it when something else is pressed: the session does (a press outside it,
    Escape)."""
    global _menu_patch_done
    if _menu_patch_done or platform.system() != "Windows":
        return
    _menu_patch_done = True
    orig_init, orig_popup = QMenu.__init__, QMenu.popup

    def _init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            if _menu_wants_tool(self):
                _make_menu_tool(self)
                from ui.helpers import note_tool_menu
                note_tool_menu(self)
        except Exception:
            logger.debug("exclusive session: could not make a menu a tool window", exc_info=True)

    def _popup(self, *args, **kwargs):
        try:
            if isinstance(self, QMenu) and args and isinstance(args[0], QPoint) and _menu_wants_tool(self):
                _make_menu_tool(self)
                result = orig_popup(self, *args, **kwargs)
                _finish_menu(self)
                return result
        except Exception:
            logger.debug("exclusive session: could not show a menu as a tool window", exc_info=True)
        return orig_popup(self, *args, **kwargs)

    QMenu.__init__ = _init
    QMenu.popup = _popup


def get_session() -> ExclusiveSession:
    global _session
    if _session is None:
        _session = ExclusiveSession()
        try:
            _install_menu_patch()
            _install_tooltip_patch()
        except Exception:
            logger.debug("exclusive session: could not install the menu and tooltip handling", exc_info=True)
    return _session
