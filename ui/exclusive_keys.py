"""
SaveSync - Typing into our own windows while a game is in exclusive fullscreen (Windows).

The windows shown over such a game (ui.win_band_hook) never take the keyboard focus - taking it takes the game back to
the desktop - so nothing typed reaches them. This module gives them the keyboard another way, for exactly as long as a
text field of one of them has been clicked:

* a low-level keyboard hook is installed in THIS process, on a thread of its own (nothing is injected anywhere, and nothing is
  installed while no field is selected);
* the keys it takes are not passed on, so the game does not see them, and are handed to the selected field as ordinary
  Qt key events (the character comes from the keyboard layout of the window in front, so AltGr and accented keys work);
* the field is shown with its caret, and the hook is removed again when the player clicks anything else, presses Escape,
  the field goes away, or the session ends.

Always passed on, never taken: Alt, the Windows key and the lock keys, any key pressed together with Alt (not AltGr) or the
Windows key (Alt+Tab, Alt+F4, the Start menu), the shortcut SaveSync itself is listening for and the modifiers that
shortcut is made of (taking Ctrl away would leave SaveSync's own hotkey listener blind to Ctrl+Alt+S). Shift and Ctrl are
taken like any other key when no shortcut uses them, so the game does not see them either. A key released that was not
taken when it went down is passed on too, so the game never sees a release without its press or the reverse.

Accents typed with a dead key (an accent, then a letter) are composed by the keyboard layout and come out as one character.
Not covered: input-method composition (Chinese, Japanese, Korean): the keys are translated by the layout's own letters, and what
an IME would make of them is not available to a window that never has the focus.
"""
import ctypes
import logging
import platform
import threading
import weakref
from collections import deque
from ctypes import wintypes
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QPainter, QPalette
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

logger = logging.getLogger(__name__)

_WH_KEYBOARD_LL = 13
_HC_ACTION = 0
_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
_LLKHF_EXTENDED = 0x01

_VK_SHIFT, _VK_CONTROL, _VK_MENU = 0x10, 0x11, 0x12
_VK_LSHIFT, _VK_RSHIFT, _VK_LCONTROL, _VK_RCONTROL, _VK_LMENU, _VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
_VK_LWIN, _VK_RWIN, _VK_CAPITAL = 0x5B, 0x5C, 0x14
_ALWAYS_PASSED = {_VK_MENU, _VK_LMENU, _VK_RMENU, _VK_LWIN, _VK_RWIN, _VK_CAPITAL, 0x90, 0x91}   # Alt, Win, Caps, Num, Scroll
_SHIFT_KEYS = {_VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT}
_CTRL_KEYS = {_VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL}

_SPECIAL = {
    0x08: Qt.Key.Key_Backspace, 0x09: Qt.Key.Key_Tab, 0x0D: Qt.Key.Key_Return, 0x1B: Qt.Key.Key_Escape,
    0x20: Qt.Key.Key_Space, 0x21: Qt.Key.Key_PageUp, 0x22: Qt.Key.Key_PageDown, 0x23: Qt.Key.Key_End,
    0x24: Qt.Key.Key_Home, 0x25: Qt.Key.Key_Left, 0x26: Qt.Key.Key_Up, 0x27: Qt.Key.Key_Right, 0x28: Qt.Key.Key_Down,
    0x2D: Qt.Key.Key_Insert, 0x2E: Qt.Key.Key_Delete,
}
_NO_TEXT = set(_SPECIAL) - {0x20}
_TEXT_FOR_KEY = {0x08: "\b", 0x09: "\t", 0x0D: "\r", 0x1B: "\x1b"}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def text_input_of(obj) -> Optional[QWidget]:
    """The editable text field *obj* is, or is the viewport / inner line edit of; None when it is not one (or cannot be
    typed into)."""
    w = obj if isinstance(obj, QWidget) else None
    for _ in range(4):
        if w is None:
            return None
        if isinstance(w, QLineEdit):
            return w if (not w.isReadOnly() and w.isEnabled()) else None
        if isinstance(w, (QTextEdit, QPlainTextEdit)):
            return w if (not w.isReadOnly() and w.isEnabled()) else None
        if isinstance(w, QAbstractSpinBox):
            edit = w.lineEdit()
            return edit if (edit is not None and w.isEnabled() and not w.isReadOnly()) else None
        w = w.parentWidget()
    return None


def _qt_key(vk: int, flags: int, text: str) -> int:
    if vk == 0x0D and flags & _LLKHF_EXTENDED:
        return int(Qt.Key.Key_Enter)
    if vk in _SPECIAL:
        return int(_SPECIAL[vk])
    if 0x70 <= vk <= 0x87:
        return int(Qt.Key.Key_F1) + (vk - 0x70)
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return vk
    if 0x60 <= vk <= 0x69:
        return int(Qt.Key.Key_0) + (vk - 0x60)
    if text:
        return ord(text[0].upper())
    return 0


class _CaretMark(QWidget):
    """The text caret, drawn by us. The window the field is in is never the active one, so Qt's own caret depends on a focus event that
    was handed to the field by hand: in a real game it disappeared while typing, and with it any way to see where the next letter
    goes (or where the arrow keys had taken it). This one is a child of the field (of its viewport, for a multi-line one) that follows
    the field's own idea of where the caret is, shows solid right after a key and blinks after that, and is hidden while there is a
    selection (as the real one is)."""

    def __init__(self, field):
        host = field.viewport() if hasattr(field, "viewport") else field
        super().__init__(host)
        self.setObjectName("xs_caret")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # (the application's style sheet gives every QWidget a background: this one paints only the bar, see ui.exclusive_session._FillHoles)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setStyleSheet("QWidget#xs_caret{background:transparent;border:none;}")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._field = field
        self._on = True
        self._timer = QTimer(self)
        self._timer.setInterval(530)
        self._timer.timeout.connect(self._blink)
        self.hide()
        for name in ("cursorPositionChanged", "selectionChanged", "textChanged"):
            try:
                getattr(field, name).connect(self.refresh)
            except (AttributeError, TypeError, RuntimeError):
                pass
        self.refresh()

    def _blink(self) -> None:
        self._on = not self._on
        self.update()

    def refresh(self, *_args) -> None:
        try:
            f = self._field
            selected = f.hasSelectedText() if isinstance(f, QLineEdit) else f.textCursor().hasSelection()
            if selected:
                self.hide()
                self._timer.stop()
                return
            r = f.cursorRect()
            if isinstance(f, QLineEdit):
                # (A line edit's cursorRect is the caret with room around it for the input method's window - 5 px to its left, 4 to its
                # right, a pixel below: the caret itself is 5 px in from the left of it. Taken as it was, the caret stood slightly
                # before the end of the text.)
                self.setGeometry(r.x() + 5, r.y(), 2, max(r.height() - 1, 10))
            else:
                self.setGeometry(r.x(), r.y(), 2, max(r.height(), 10))
            self._on = True
            self._timer.start()
            self.show()
            self.raise_()
            self.update()
        except (RuntimeError, AttributeError):
            pass

    def paintEvent(self, _event):
        if not self._on:
            return
        try:
            p = QPainter(self)
            p.fillRect(self.rect(), self._field.palette().color(QPalette.ColorRole.Text))
            p.end()
        except RuntimeError:
            pass

    def stop(self) -> None:
        try:
            self._timer.stop()
            self.hide()
            self.deleteLater()
        except RuntimeError:
            pass


class ExclusiveKeys(QObject):
    """See the module docstring. *passthrough(key_name, modifiers)* says whether a key is the shortcut SaveSync listens
    for (names are lower case: a letter, a digit, "f5"; modifiers a set of "ctrl", "alt", "shift", "win").

    The hook lives on a thread of its own, with its own message loop: a low-level hook is only answered while the thread
    that installed it is pumping messages, and Windows gives it about 300 ms (measured) before it lets the key through
    unanswered. On the main thread that meant every key waited for whatever the application was busy with, and a letter
    typed during a stall of a second or more went to the game. Everything the hook thread touches (the keys taken, the
    modifiers held, the dead-key state of the layout) is its own; Qt is only ever called from the main thread, to which
    the keys are handed in order through one queued signal."""

    _wake = Signal()                                # emitted by the hook thread: queued to the main thread
    _wake_esc = Signal()                            # the same, for an Escape taken while no field is selected

    def __init__(self, passthrough: Optional[Callable[[str, set], bool]] = None,
                 shortcut_modifiers: Optional[Callable[[], set]] = None, parent=None):
        super().__init__(parent)
        self._passthrough = passthrough
        self._shortcut_modifiers = shortcut_modifiers
        self._held = set()                        # modifiers whose press was taken: "shift", "ctrl" (hook thread)
        self._target = None                       # weakref to the selected field
        self._taking = False                      # the hook thread takes keys only while this is set
        self._hook = None                         # the hook's handle, while the hook thread has one
        self._proc = None                         # kept alive
        self._thread = None
        self._thread_id = 0
        self._taken = set()                       # keys whose press was taken (their release is taken too) (hook thread)
        self._queue = deque()
        self._typing = False                      # a field is selected: the hook takes the keys (otherwise it takes Escape only)
        self._completer = None                    # (weakref to the field, its completer, its mode) while the field completes in the box
        self._caret = None                        # the _CaretMark of the field typed into
        self._esc_handler = None                  # what a bare Escape does while a closable window of ours is up
        self._wake.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._wake_esc.connect(self._deliver_esc, Qt.ConnectionType.QueuedConnection)
        self._u = None
        self._layout_override = None              # tests: a keyboard layout to translate with, in place of the window in front's
        self.taken_count = 0

    # ── the selected field ───────────────────────────────────────────────────────────────────────────

    def armed(self) -> bool:
        return self._current() is not None

    def target(self):
        return self._current()

    def _current(self):
        w = self._target() if self._target is not None else None
        if w is None:
            return None
        try:
            if not w.isVisible():
                return None
        except RuntimeError:
            return None
        return w

    def arm(self, field: QWidget) -> bool:
        """Send the keys to *field* from now on. Returns once the hook is in place: the first key after this call is ours."""
        if platform.system() != "Windows":
            return False
        old = self._current()
        if old is field:
            return True
        if old is not None:
            self._focus(old, False)
        self._target = weakref.ref(field)
        self._typing = True
        self._tame_completer(field)
        try:
            logger.debug("Exclusive keys: typing goes to %s %r now (it has the focus: %s, its window is the active one: %s)",
                         type(field).__name__, field.objectName(), field.hasFocus(), field.window().isActiveWindow())
        except RuntimeError:
            pass
        if not self._install():
            self._target = None
            self._typing = False
            self._give_back_completer()
            return False
        self._focus(field, True)
        self._show_caret(field)
        return True

    def _tame_completer(self, field) -> None:
        """A field typed into over a game keeps its completer, but completes INSIDE the box: its list would open as a window of its own,
        a popup the game does not take well. The suggestion is the rest of a name, selected - the next key replaces it, Backspace or
        Delete removes it ("tekken" back to "te"), and a name that matches nothing is not completed. Let go as it was."""
        self._give_back_completer()
        try:
            completer = field.completer() if hasattr(field, "completer") else None
            if completer is not None:
                from PySide6.QtWidgets import QCompleter
                self._completer = (weakref.ref(field), completer, completer.completionMode())
                completer.setCompletionMode(QCompleter.CompletionMode.InlineCompletion)
        except Exception:
            logger.debug("exclusive keys: could not make the completer inline", exc_info=True)

    def _give_back_completer(self) -> None:
        pair, self._completer = self._completer, None
        if pair is None:
            return
        try:
            if pair[0]() is not None:
                pair[1].setCompletionMode(pair[2])
        except RuntimeError:
            pass

    def _show_caret(self, field) -> None:
        self._hide_caret()
        try:
            self._caret = _CaretMark(field)
        except Exception:
            self._caret = None
            logger.debug("exclusive keys: could not draw the caret", exc_info=True)

    def _hide_caret(self) -> None:
        caret, self._caret = self._caret, None
        if caret is not None:
            caret.stop()

    def disarm(self, why: str = "") -> None:
        old = self._current()
        self._give_back_completer()
        self._hide_caret()
        self._target = None
        self._typing = False
        if self._esc_handler is None:
            self._uninstall()
        self._queue.clear()
        if old is not None:
            self._focus(old, False)
            logger.debug("exclusive keys: released (%s)", why)

    def set_escape_handler(self, handler: Optional[Callable[[], None]]) -> None:
        """While *handler* is set (a window of ours that can be closed is up), a bare Escape is taken, not passed on, and runs it:
        the window never has the keyboard, so Escape would otherwise reach the game behind it. Every other key is left alone unless
        a field is selected."""
        self._esc_handler = handler
        if platform.system() != "Windows":
            return
        if handler is not None:
            if not self._install():
                self._esc_handler = None
        elif not self._typing:
            self._uninstall()

    def retarget_if_moved(self) -> None:
        """The key that was just delivered moved the focus inside the window (Tab): follow it."""
        cur = self._current()
        if cur is None:
            return
        fw = cur.window().focusWidget()
        nxt = text_input_of(fw) if fw is not None and fw is not cur else None
        if nxt is not None and nxt is not cur:
            self._focus(cur, False)
            self._target = weakref.ref(nxt)
            self._tame_completer(nxt)
            self._focus(nxt, True)
            self._show_caret(nxt)

    @staticmethod
    def _focus(w: QWidget, on: bool) -> None:
        """The window is never the active one, so Qt does not tell the field it has the focus; it is told here, which is
        what makes its caret blink."""
        try:
            ev = QFocusEvent(QEvent.Type.FocusIn if on else QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason)
            QApplication.sendEvent(w, ev)
            w.update()
        except RuntimeError:
            pass

    # ── the hook thread ──────────────────────────────────────────────────────────────────────────────

    def _api(self):
        if self._u is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            u.SetWindowsHookExW.argtypes = [ctypes.c_int, proto, wintypes.HINSTANCE, wintypes.DWORD]
            u.SetWindowsHookExW.restype = ctypes.c_void_p
            u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            u.CallNextHookEx.restype = ctypes.c_ssize_t
            u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            u.GetAsyncKeyState.argtypes = [ctypes.c_int]
            u.GetAsyncKeyState.restype = ctypes.c_short
            u.GetKeyState.argtypes = [ctypes.c_int]
            u.GetKeyState.restype = ctypes.c_short
            u.GetForegroundWindow.restype = ctypes.c_void_p
            u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u.GetWindowThreadProcessId.restype = wintypes.DWORD
            u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
            u.GetKeyboardLayout.restype = ctypes.c_void_p
            u.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR,
                                      ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
            u.ToUnicodeEx.restype = ctypes.c_int
            u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostThreadMessageW.restype = wintypes.BOOL
            self._u = u
        return self._u

    def _install(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            self._taking = True
            return True
        try:
            self._api()
        except Exception:
            logger.info("exclusive keys: the keyboard hook could not be prepared", exc_info=True)
            return False
        ready = threading.Event()
        outcome = {"ok": False}
        self._taken.clear()
        self._held.clear()
        self._thread = threading.Thread(target=self._hook_main, args=(ready, outcome), name="exclusive-keys-hook", daemon=True)
        self._thread.start()
        if not ready.wait(2.0) or not outcome["ok"]:
            logger.info("exclusive keys: the keyboard hook could not be installed")
            self._taking = False
            return False
        self._taking = True
        return True

    def _hook_main(self, ready: threading.Event, outcome: dict) -> None:
        """The hook thread: installs the hook, answers it, and removes it again when told to stop."""
        u = self._u
        hook = None
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            k.GetModuleHandleW.restype = wintypes.HINSTANCE
            k.GetCurrentThreadId.restype = wintypes.DWORD
            proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            def _proc(code, wparam, lparam):
                try:
                    if code == _HC_ACTION and self._taking:
                        kb = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                        if self._take(kb, wparam):
                            return 1
                except Exception:
                    pass
                return u.CallNextHookEx(None, code, wparam, lparam)

            self._proc = proto(_proc)
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)              # makes the thread's message queue
            self._thread_id = int(k.GetCurrentThreadId())
            hook = u.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, k.GetModuleHandleW(None), 0)
            if not hook:
                logger.info("exclusive keys: SetWindowsHookEx failed (error %s)", ctypes.get_last_error())
                return
            self._hook = hook
            outcome["ok"] = True
            ready.set()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:        # the hook is answered from inside this call
                pass
        except Exception:
            logger.info("exclusive keys: the hook thread failed", exc_info=True)
        finally:
            if hook:
                try:
                    u.UnhookWindowsHookEx(hook)
                except Exception:
                    pass
            self._flush_dead_key()
            self._hook = None
            self._proc = None
            self._taking = False
            ready.set()

    def _uninstall(self) -> None:
        self._taking = False                       # from this instant no key is taken
        th, self._thread = self._thread, None
        if th is not None and th.is_alive() and self._u is not None:
            try:
                self._u.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
            th.join(1.0)
        self._hook = None
        self._thread_id = 0
        self._taken.clear()
        self._held.clear()

    def _down(self, vk: int) -> bool:
        return bool(self._u.GetAsyncKeyState(vk) & 0x8000)

    def _modifiers(self) -> set:
        mods = set(self._held)                    # (a modifier we took never reached the system's own key state)
        if self._down(_VK_SHIFT):
            mods.add("shift")
        if self._down(_VK_CONTROL):
            mods.add("ctrl")
        if self._down(_VK_MENU):
            mods.add("alt")
        if self._down(_VK_LWIN) or self._down(_VK_RWIN):
            mods.add("win")
        return mods

    def _take(self, kb, wparam) -> bool:
        """Runs inside the hook, on the hook thread: keep it short. True = the key is ours (not passed on)."""
        vk = int(kb.vkCode)
        down = wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
        if not self._typing:
            return self._take_escape_only(vk, down)
        kind = "shift" if vk in _SHIFT_KEYS else "ctrl" if vk in _CTRL_KEYS else ""
        if not down:
            if vk in self._taken:
                self._taken.discard(vk)
                if kind:
                    self._held.discard(kind)
                return True                                   # its press was ours, so is its release
            return False
        if vk in _ALWAYS_PASSED:
            return False
        if kind:
            used = self._shortcut_modifiers() if self._shortcut_modifiers is not None else set()
            if kind in used:
                return False                                  # a shortcut is made of it: it has to reach SaveSync's listener
            self._held.add(kind)
            self._taken.add(vk)
            self.taken_count += 1
            return True
        # Alt (not AltGr = right Alt) or the Windows key: a command for the system or another program
        left_alt = self._down(_VK_LMENU) and not self._down(_VK_RMENU)
        if left_alt or self._down(_VK_LWIN) or self._down(_VK_RWIN):
            return False
        mods = self._modifiers()
        name = self._key_name(vk)
        if self._passthrough is not None and name and self._passthrough(name, mods):
            return False
        repeat = vk in self._taken
        self._taken.add(vk)
        altgr = self._down(_VK_RMENU)
        text = self._layout_text(vk, int(kb.scanCode), mods)
        self.taken_count += 1
        if text is None:                                      # a dead key: remembered by the layout, nothing to type yet
            return True
        self._queue.append((vk, int(kb.scanCode), int(kb.flags), mods, repeat, text, altgr))
        self._wake.emit()
        return True

    def _take_escape_only(self, vk: int, down: bool) -> bool:
        """No field is selected: only a bare Escape is ours, and only while something is waiting for it. (Hook thread.)"""
        if not down:
            if vk in self._taken:
                self._taken.discard(vk)
                return True                                   # its press was ours, so is its release
            return False
        if vk != 0x1B or self._esc_handler is None:
            return False
        if self._modifiers():
            return False                                      # Ctrl+Shift+Esc, Alt+Esc ...: not ours
        if vk in self._taken:
            return True                                       # the key repeating: one press, one close
        self._taken.add(vk)
        self.taken_count += 1
        self._wake_esc.emit()
        return True

    @staticmethod
    def _key_name(vk: int) -> str:
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x70 <= vk <= 0x87:
            return f"f{vk - 0x6F}"
        return {0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc", 0x20: "space", 0x21: "page_up",
                0x22: "page_down", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
                0x2D: "insert", 0x2E: "delete"}.get(vk, "")

    def _flush_dead_key(self) -> None:
        """An accent that was typed and never followed by a letter (the player clicked away) is still waiting in the layout, and
        it is not the hook thread's alone (measured: the main thread's next translation of "e" came out as "é"): the next thing
        typed anywhere in this program would be accented. A space completes it, as it would have."""
        try:
            u = self._u
            buf = ctypes.create_unicode_buffer(8)
            state = (ctypes.c_ubyte * 256)()
            for _ in range(3):
                if u.ToUnicodeEx(0x20, 0x39, state, buf, 8, 0, self._layout()) >= 0:
                    break
        except Exception:
            logger.debug("exclusive keys: could not clear a pending dead key", exc_info=True)

    def _layout(self):
        if self._layout_override:
            return self._layout_override
        u = self._u
        fg = u.GetForegroundWindow()
        tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
        return u.GetKeyboardLayout(tid)

    def _layout_text(self, vk: int, scan: int, mods: set):
        """The character this key makes on the keyboard layout of the window in front. ToUnicodeEx is given a state built from
        the keys really held, and it is the layout that composes: a dead key (an accent) returns None - it makes nothing yet
        and is remembered by the layout, in the state of THIS thread - and the next key comes back composed ("é"), or as the
        accent and the letter when the two do not combine."""
        if vk in _NO_TEXT:
            return _TEXT_FOR_KEY.get(vk, "")
        try:
            u = self._u
            state = (ctypes.c_ubyte * 256)()
            if "shift" in mods:
                state[_VK_SHIFT] = 0x80
            altgr = self._down(_VK_RMENU)
            if "ctrl" in mods or altgr:
                state[_VK_CONTROL] = 0x80
            if "alt" in mods or altgr:
                state[_VK_MENU] = 0x80
            if u.GetKeyState(_VK_CAPITAL) & 1:
                state[_VK_CAPITAL] = 0x01
            if "ctrl" in mods and not altgr:
                return ""                                     # a shortcut (Ctrl+A ...) makes no character
            buf = ctypes.create_unicode_buffer(8)
            n = u.ToUnicodeEx(vk, scan, state, buf, 8, 0, self._layout())
            if n < 0:
                return None
            return buf.value[:n] if n > 0 else ""
        except Exception:
            return ""

    def _deliver_esc(self) -> None:
        handler = self._esc_handler
        if handler is not None:
            try:
                handler()
            except Exception:
                logger.debug("exclusive keys: the Escape handler failed", exc_info=True)

    def _deliver(self) -> None:
        while self._queue:
            try:
                vk, scan, flags, mods, repeat, text, altgr = self._queue.popleft()
            except IndexError:
                return
            tgt = self._current()
            if tgt is None:
                self.disarm("the field is gone")
                return
            qmods = Qt.KeyboardModifier.NoModifier
            if "shift" in mods:
                qmods |= Qt.KeyboardModifier.ShiftModifier
            if "ctrl" in mods:
                qmods |= Qt.KeyboardModifier.ControlModifier
            if "alt" in mods:
                qmods |= Qt.KeyboardModifier.AltModifier
            if altgr:                                         # AltGr is Ctrl+Alt to the system, and neither to Qt
                qmods &= ~(Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
            ev = QKeyEvent(QEvent.Type.KeyPress, _qt_key(vk, flags, text), qmods, scan, vk, 0, text, repeat, 1)
            QApplication.sendEvent(tgt, ev)
            if self._caret is not None:
                self._caret.refresh()                          # (solid right after a key, wherever the key put it)
            if vk == 0x1B and not ev.isAccepted():
                self.disarm("Escape")                         # nothing used Escape: it ends the typing
                return
            self.retarget_if_moved()

    # ── what the session tells it ─────────────────────────────────────────────────────────────────────

    def on_mouse_press(self, obj) -> None:
        """A mouse press reached one of our widgets: a text field takes the keyboard, anything else lets go of it."""
        field = text_input_of(obj)
        if field is not None:
            try:
                from ui import win_band_hook
                if win_band_hook.window_band(int(field.window().winId())) != win_band_hook.BAND_SYSTEM_TOOLS:
                    field = None
            except Exception:
                field = None
        if field is not None:
            self.arm(field)
        elif self.armed():
            self.disarm("pressed elsewhere")

    def shutdown(self) -> None:
        self._esc_handler = None
        self.disarm("shutdown")
