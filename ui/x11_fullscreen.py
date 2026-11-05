"""
SaveSync - Over a fullscreen window on X11 (Linux).

Measured on a real X server with Openbox (no compositor), with a fullscreen window in front:

- a MANAGED window - the overlay or a pin as they are - is stacked UNDER it: EWMH puts a fullscreen window
  above the always-on-top ones, so the window the player is meant to see is not on screen (it showed only
  for the instant after it was mapped, because mapping it took the focus from the game);
- an OVERRIDE-REDIRECT window (Qt's X11BypassWindowManagerHint) stays above it, and the game stays fullscreen
  and active - the window manager is simply not asked;
- an override-redirect window never gets keyboard focus from the window manager, so a note could not be typed
  into; XSetInputFocus on the pin after the click fixes that, and the game stays fullscreen.

So the overlay and the pins become override-redirect for as long as a fullscreen window is in front, and are
managed windows otherwise. That is all this module answers: is one in front (fullscreen_window_in_front), does
this session decide it that way at all (dynamic_bypass_applies), and a way to hand a window the keyboard
(focus_window).

Measured since, on the same stand-in: Mutter as an X11 compositing window manager, and Openbox with picom (a compositor)
- the overlay and a pin stay above the fullscreen window there too, a note can be typed into, and the game stays fullscreen
and active. KWin as the X11 window manager and compositor (software OpenGL, on Xvfb) the same: 4 runs out of 5.

A Wayland session runs SaveSync under XWayland. Measured with sway (a wlroots compositor, headless) and a fullscreen X11
window: an override-redirect window stays above it, a managed one does not - so the overlay (OverlayWidget._setup_window)
and the pins are override-redirect there as well (xwayland_session; the pins only while a fullscreen window is in front, since
a managed one would be under it). Measured the same way with Mutter and with KWin as Wayland compositors (headless, through XWayland, judged by
their own screenshots): the overlay and a pin stay above the fullscreen game and the game stays fullscreen. Not measured: real GNOME Shell and
Plasma sessions, and SaveSync as a native Wayland client (it would need the layer-shell protocol, which Qt for Python does not expose).

Typing into a pin over the game under XWayland: the compositor owns the keyboard there and sends the keys to the game's surface, and
XSetInputFocus on the pin changed nothing under sway. A keyboard grab taken on Qt's own X connection (start_typing) diverts the keys inside
XWayland's X server from the game to the pin (measured under sway with a virtual keyboard: the game's windows received none while it was held,
the note received them, the game stayed fullscreen and focused in the compositor). It is given up on Escape, when the pin goes and once the
pointer has been away for a moment, and the X focus goes back to the window that had it.

Everything here is best effort: with python-xlib missing, no EWMH, or any error, the answer is "no fullscreen
window", which is the behaviour before this existed.
"""
import logging
import os
import platform

logger = logging.getLogger(__name__)

_display = None


def dynamic_bypass_applies() -> bool:
    """True on a plain X11 session: the only place this decides anything. Under a Wayland compositor the overlay
    bypasses the window manager permanently, and elsewhere there is no X11 to ask."""
    if platform.system() != "Linux":
        return False
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return False
    return bool(os.environ.get("DISPLAY"))


def xwayland_session() -> bool:
    """True when SaveSync is an X client of a Wayland compositor (XWayland): a Wayland session, an X display, and Qt's xcb
    platform - which SaveSync asks for itself (main.py), for window placement and the global hotkey."""
    if platform.system() != "Linux":
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"):
        return False
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.platformName() == "xcb"
    except Exception:
        return False


def _get_display():
    global _display
    if _display is None:
        from Xlib import display
        _display = display.Display()
    return _display


def _reset_display() -> None:
    global _display
    try:
        if _display is not None:
            _display.close()
    except Exception:
        pass
    _display = None


_last_fullscreen = [False]                         # the last answer, for a moment when only this program's own window is active


def _fullscreen_active_window(d, root, atom, mine: int) -> bool:
    """The same question for a window manager that publishes no stacking list - Weston's, which is what WSLg runs: its _NET_SUPPORTED is
    only the move/resize, the window state and _NET_ACTIVE_WINDOW. The active window stands for "the window in front"; when it is one of
    this program's own (the overlay took the active state) the last answer stands."""
    from Xlib import X
    active = root.get_full_property(atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    wid = int(active.value[0]) if active is not None and len(active.value) else 0
    if not wid:
        return _last_fullscreen[0]
    w = d.create_resource_object("window", wid)
    pid = w.get_full_property(atom("_NET_WM_PID"), X.AnyPropertyType)
    if pid is not None and len(pid.value) and int(pid.value[0]) == mine:
        return _last_fullscreen[0]
    state = w.get_full_property(atom("_NET_WM_STATE"), X.AnyPropertyType)
    states = set(state.value) if state is not None else set()
    _last_fullscreen[0] = atom("_NET_WM_STATE_FULLSCREEN") in states and atom("_NET_WM_STATE_HIDDEN") not in states
    return _last_fullscreen[0]


def fullscreen_window_in_front() -> bool:
    """True when the topmost window on the current desktop that is not one of this program's own is fullscreen
    according to the window manager (EWMH: _NET_WM_STATE_FULLSCREEN). This program's own windows are skipped
    by process id, so the overlay appearing - and taking the active state - does not make the answer flip."""
    if not (dynamic_bypass_applies() or xwayland_session()):
        return False
    try:
        from Xlib import X
        d = _get_display()
        root = d.screen().root
        atom = d.intern_atom
        stacking = root.get_full_property(atom("_NET_CLIENT_LIST_STACKING"), X.AnyPropertyType)
        if stacking is None:
            return _fullscreen_active_window(d, root, atom, os.getpid())
        current = root.get_full_property(atom("_NET_CURRENT_DESKTOP"), X.AnyPropertyType)
        desktop = int(current.value[0]) if current is not None and len(current.value) else None
        a_state, a_full = atom("_NET_WM_STATE"), atom("_NET_WM_STATE_FULLSCREEN")
        a_hidden, a_pid, a_desk = atom("_NET_WM_STATE_HIDDEN"), atom("_NET_WM_PID"), atom("_NET_WM_DESKTOP")
        mine = os.getpid()
        for wid in reversed(list(stacking.value)):          # bottom to top: the top window is the last one
            w = d.create_resource_object("window", wid)
            pid = w.get_full_property(a_pid, X.AnyPropertyType)
            if pid is not None and len(pid.value) and int(pid.value[0]) == mine:
                continue
            where = w.get_full_property(a_desk, X.AnyPropertyType)
            if (where is not None and len(where.value) and desktop is not None
                    and int(where.value[0]) not in (desktop, 0xFFFFFFFF)):
                continue                                    # on another desktop
            state = w.get_full_property(a_state, X.AnyPropertyType)
            states = set(state.value) if state is not None else set()
            if a_hidden in states:
                continue                                    # minimised: not in front
            _last_fullscreen[0] = a_full in states
            return _last_fullscreen[0]
        return False
    except ImportError:
        return False
    except Exception:
        logger.debug("Could not ask the window manager about fullscreen windows", exc_info=True)
        _reset_display()
        return False


def x11_id(widget) -> int:
    """The X window of *widget*. QWidget.winId() goes on answering with the id the window had BEFORE setWindowFlags() recreated it - a
    window that no longer exists (measured: the app's main window, once it was made always-on-top for Open App) - where the QWindow's
    is the real one."""
    handle = widget.windowHandle()
    return int((handle if handle is not None else widget).winId())


def stack_index(wids) -> dict:
    """{wid: where the top-level window that holds it is among the root's children, from the bottom (0) up}. A window that is gone
    is left out."""
    out = {}
    try:
        d = _get_display()
        root = d.screen().root
        kids = [c.id for c in root.query_tree().children]
        for wid in wids:
            try:
                w = d.create_resource_object("window", int(wid))
                for _ in range(6):
                    parent = w.query_tree().parent
                    if parent is None or not getattr(parent, "id", 0) or parent.id == root.id:
                        break
                    w = parent
                if w.id in kids:
                    out[wid] = kids.index(w.id)
            except Exception:
                continue                                   # (gone in the meantime)
    except Exception:
        logger.debug("Could not read the window order", exc_info=True)
        _reset_display()
    return out


def stack_below(lower_wid: int, upper_wid: int) -> bool:
    """Put the X window *lower_wid* directly under the top-level window that holds *upper_wid* (its frame, once a window manager
    has reparented it). For an override-redirect window - the dimmed backdrop - and a managed one - the app or a dialog: a window
    manager stacks its own frames among themselves and leaves override-redirect windows where they are, so a frame mapped AFTER the
    backdrop can end up under it (xfwm4: the app opened beneath its own vignette, and a click on the app was a click on the backdrop,
    which closes it). Best effort; False when it could not be done."""
    try:
        from Xlib import X, error
        d = _get_display()
        root = d.screen().root
        frame = d.create_resource_object("window", int(upper_wid))
        for _ in range(6):                              # (client -> the manager's frame -> the root)
            parent = frame.query_tree().parent
            if parent is None or not getattr(parent, "id", 0) or parent.id == root.id:
                break
            frame = parent
        refused = error.CatchError(error.BadWindow, error.BadMatch)
        d.create_resource_object("window", int(lower_wid)).configure(sibling=frame.id, stack_mode=X.Below, onerror=refused)
        d.sync()
        return refused.get_error() is None
    except Exception:
        logger.debug("Could not stack a window under another", exc_info=True)
        _reset_display()
        return False


def make_input_transparent(wid: int) -> bool:
    """Let the mouse go through the X window *wid*: an empty INPUT shape. Qt's WindowTransparentForInput is meant to do this and does
    not under every window manager (measured with xfwm4: a window with the flag still took the pointer - the drawn arrow SaveSync
    puts under the pointer took every click, and a pin under it could not be moved). Best effort."""
    try:
        from Xlib.ext import shape
        d = _get_display()
        d.create_resource_object("window", int(wid)).shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
        d.sync()
        return True
    except Exception:
        logger.debug("Could not make a window input-transparent", exc_info=True)
        _reset_display()
        return False


_previous_focus = [0]                                # the window that had the X input focus before a pin took it (XWayland)


def current_focus() -> int:
    """The X window that has the keyboard right now (0 when it cannot be told)."""
    try:
        return int(getattr(_get_display().get_input_focus().focus, "id", 0))
    except Exception:
        logger.debug("Could not read the X input focus", exc_info=True)
        _reset_display()
        return 0


def focus_window(wid: int) -> None:
    """Give the keyboard to window *wid* (an override-redirect window gets none from the window manager).
    Best effort."""
    if not (dynamic_bypass_applies() or xwayland_session()):
        return
    try:
        from Xlib import X
        d = _get_display()
        if xwayland_session():
            held = getattr(d.get_input_focus().focus, "id", 0)       # (None / PointerRoot come back as plain numbers)
            if held and held != int(wid):
                _previous_focus[0] = held
        d.create_resource_object("window", int(wid)).set_input_focus(X.RevertToParent, X.CurrentTime)
        d.sync()
    except Exception:
        logger.debug("Could not give the keyboard to a pin", exc_info=True)
        _reset_display()


# ── typing into a pin over a game, under XWayland ───────────────────────────────────────────────────

def _qt_xlib():
    """(libX11, Qt's own Display*) or (None, None). A keyboard grab is held by the X CLIENT that took it, and the keys it diverts
    go to that client: it has to be taken on the connection Qt reads its events from, not on one of ours."""
    try:
        import ctypes
        import ctypes.util
        from PySide6.QtGui import QGuiApplication
        interface = QGuiApplication.instance().nativeInterface()        # QNativeInterface.QX11Application
        display = int(interface.display())
        if not display:
            return None, None
        lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        lib.XGrabKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        lib.XGrabKeyboard.restype = ctypes.c_int
        lib.XUngrabKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        return lib, ctypes.c_void_p(display)
    except Exception:
        logger.debug("Qt's X display is not available", exc_info=True)
        return None, None


_typing = None


class _XWaylandTyping:
    """Under a Wayland compositor the keyboard belongs to the compositor: it sends the keys to the game's surface, and
    XSetInputFocus on a pin changes nothing (measured with sway). The game is an X client of the same XWayland, though, and the
    keys do arrive in XWayland's X server - there a keyboard grab on the pin diverts them from the game to the pin (measured: the
    game's windows received none while it was held, 4 of 5 letters when it was not), and the game stays fullscreen and focused in the
    compositor, which never hears of it. Held only while a note is being typed into: given up on Escape, when the pin goes, and
    once the pointer has been away from it for a moment (a game must not lose its keys because a note was clicked once).
    Works while the game itself is an X client; for a game that is not (a native Wayland one) no key arrives and nothing changes."""

    AWAY_TICKS = 3                                  # x 100 ms

    def __init__(self):
        from PySide6.QtCore import QObject, QTimer
        owner = self

        class _Filter(QObject):
            def eventFilter(self, obj, event):
                from PySide6.QtCore import QEvent, Qt
                if (owner.pin is not None and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape):
                    owner.end()
                    return True                     # Escape gives the keyboard back; it is not typed into the note
                return False

        self.pin = None
        self._away = 0
        self._held = False
        self._filter = _Filter()
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    def begin(self, pin) -> bool:
        if self._held and self.pin is pin:
            return True
        self.end()
        lib, display = _qt_xlib()
        if lib is None:
            return False
        try:
            status = lib.XGrabKeyboard(display, int(pin.winId()), 1, 1, 1, 0)       # owner_events, async, async, CurrentTime
            lib.XFlush(display)
        except Exception:
            logger.debug("Could not grab the keyboard for a pin", exc_info=True)
            return False
        if status != 0:                                                                # GrabSuccess
            logger.debug("The keyboard grab for a pin was refused (status %s)", status)
            return False
        from PySide6.QtGui import QGuiApplication
        self.pin, self._held, self._away = pin, True, 0
        QGuiApplication.instance().installEventFilter(self._filter)
        self._timer.start()
        return True

    def end(self) -> None:
        if not self._held:
            self.pin = None
            return
        self._held = False
        self._timer.stop()
        try:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.instance().removeEventFilter(self._filter)
        except Exception:
            pass
        lib, display = _qt_xlib()
        if lib is not None:
            try:
                lib.XUngrabKeyboard(display, 0)
                lib.XFlush(display)
            except Exception:
                logger.debug("Could not give the keyboard back", exc_info=True)
        try:
            pin_wid = int(self.pin.winId())
        except Exception:
            pin_wid = 0
        self.pin = None
        _give_focus_back(pin_wid)

    def _tick(self) -> None:
        try:
            pin = self.pin
            if pin is None or not pin.isVisible():
                self.end()
                return
            # Away only when BOTH Qt's enter/leave tracking and the pointer position say so: each can be stale by itself under
            # XWayland (measured: the position right after the compositor moved the pointer, the tracking with a virtual pointer).
            from PySide6.QtGui import QCursor
            inside = pin.underMouse() or pin.frameGeometry().contains(QCursor.pos())
            self._away = 0 if inside else self._away + 1
            if self._away >= self.AWAY_TICKS:
                self.end()
        except RuntimeError:                         # the pin is gone
            self.end()


def _give_focus_back(pin_wid: int) -> None:
    """The game had the X input focus before the pin took it (focus_window): once the pin is done with the keyboard it goes back,
    unless something else has taken it since. Without this a note that was clicked once keeps every key for good, on the
    compositors where XSetInputFocus does take effect."""
    previous, _previous_focus[0] = _previous_focus[0], 0
    if not previous or not pin_wid:
        return
    try:
        from Xlib import X
        d = _get_display()
        if getattr(d.get_input_focus().focus, "id", 0) == pin_wid:
            d.create_resource_object("window", previous).set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()
    except Exception:
        logger.debug("Could not give the input focus back", exc_info=True)
        _reset_display()


def start_typing(pin) -> bool:
    """Under XWayland only: divert the keyboard from the game to *pin* (see _XWaylandTyping). False when that does not apply."""
    global _typing
    if not xwayland_session():
        return False
    if _typing is None:
        _typing = _XWaylandTyping()
    return _typing.begin(pin)


def stop_typing() -> None:
    if _typing is not None:
        _typing.end()
