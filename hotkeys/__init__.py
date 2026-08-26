"""
SaveSync - Hotkey Manager
Global keyboard shortcuts via pynput. Callbacks are ALWAYS dispatched to the
Qt GUI thread, never called directly from the listener's daemon thread.

pynput replaces the previous `keyboard` package: same feature set on
Windows, but on Linux it works as a regular user through X11/uinput
(`keyboard` required root), and it is the better-maintained option on
macOS as well (still needs the Accessibility permission there).
"""
import logging
import threading
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)


# App hotkey strings (from ui/widgets/hotkey_edit.py and config defaults,
# e.g. "alt+ctrl+s", "ctrl+shift+f5") → pynput GlobalHotKeys syntax
# ("<alt>+<ctrl>+s"): modifiers and named keys get angle brackets, single
# characters pass through bare.
_PYNPUT_MODS = {
    "ctrl": "<ctrl>", "control": "<ctrl>",
    "alt": "<alt>",
    "shift": "<shift>",
    "win": "<cmd>", "windows": "<cmd>", "super": "<cmd>",
    "cmd": "<cmd>", "meta": "<cmd>",
}
_PYNPUT_KEY_ALIASES = {
    "page up": "page_up", "page down": "page_down",
    "return": "enter", "escape": "esc", "del": "delete",
    "minus": "-",
}

# Canonical modifier names, in the order a normalized hotkey string lists
# them. "ctrl+alt+s" and "alt+ctrl+s" are the same shortcut, so they have to
# reduce to the same key: bindings are stored per string, and two spellings
# of one shortcut used to live side by side, each with its own callback.
_MOD_ORDER = ("ctrl", "alt", "shift", "win")

# Virtual-key codes for the physical check in _modifiers_held (Windows only).
_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}
_VK_LWIN, _VK_RWIN = 0x5B, 0x5C


def _split_hotkey(hotkey: str) -> tuple[list[str], list[str]]:
    """(canonical modifiers, remaining keys) for a hotkey string."""
    mods, keys = set(), []
    for part in (p.strip().lower() for p in hotkey.split("+")):
        if not part:
            continue
        if part in _PYNPUT_MODS:
            mods.add({"control": "ctrl", "windows": "win", "super": "win",
                      "cmd": "win", "meta": "win"}.get(part, part))
        else:
            keys.append(_PYNPUT_KEY_ALIASES.get(part, part))
    return [m for m in _MOD_ORDER if m in mods], keys


def normalize_hotkey(hotkey: str) -> str:
    """A hotkey string in canonical form: fixed modifier order, lowercase."""
    mods, keys = _split_hotkey(hotkey)
    return "+".join(mods + keys)


def _wayland_without_x() -> bool:
    """True on a Wayland session with no X display to fall back on."""
    import os
    import sys
    if sys.platform in ("win32", "darwin"):
        return False
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY")
                      or os.environ.get("XDG_SESSION_TYPE", "").lower()
                      == "wayland")
    return on_wayland and not os.environ.get("DISPLAY")


def _to_pynput_combo(hotkey: str) -> str:
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey: {hotkey!r}")
    out = []
    for p in parts:
        if p in _PYNPUT_MODS:
            out.append(_PYNPUT_MODS[p])
            continue
        p = _PYNPUT_KEY_ALIASES.get(p, p)
        out.append(p if len(p) == 1 else f"<{p}>")
    return "+".join(out)


def _modifiers_held(required: list[str]) -> bool:
    """True when exactly *required* modifiers are physically down right now.

    pynput decides a hotkey fired by comparing the set of keys it believes
    are held. That belief is wrong whenever a key-up is missed — which
    happens on Windows when a modifier is released while another window has
    the keyboard, or after a UAC prompt — and a stuck Alt makes plain Ctrl+S
    look like Alt+Ctrl+S. Asking the OS what is actually down closes that
    gap. Only meaningful on Windows; elsewhere the check passes.
    """
    try:
        import ctypes
        get_state = ctypes.windll.user32.GetAsyncKeyState
    except (ImportError, AttributeError, OSError):
        return True

    def _down(vk: int) -> bool:
        return bool(get_state(vk) & 0x8000)

    for name, vk in _VK.items():
        if _down(vk) != (name in required):
            return False
    if (_down(_VK_LWIN) or _down(_VK_RWIN)) != ("win" in required):
        return False
    return True


class HotkeyManager(QObject):
    """
    Thread-safe hotkey manager.
    pynput fires callbacks in its listener daemon thread. We route them
    through a Signal (which queues across threads) so all user callbacks
    execute on the Qt GUI thread.

    pynput's GlobalHotKeys takes its full binding map at construction, so
    (un)registering rebuilds the single listener from the current bindings
    — registrations are rare (startup + settings changes), the churn is
    negligible.
    """
    _trigger = Signal(str)   # internal — carries hotkey string, safe from any thread

    def __init__(self):
        super().__init__()
        self._bindings: Dict[str, Callable] = {}   # hotkey_str → callback
        self._combos: Dict[str, str] = {}          # hotkey_str → pynput combo
        self._mods: Dict[str, list] = {}           # hotkey_str → modifiers
        self._bindings_lock = threading.Lock()
        self._listener = None
        self._available = False
        self._pynput_keyboard = None
        from PySide6.QtCore import Qt
        self._trigger.connect(self._on_trigger, Qt.ConnectionType.QueuedConnection)
        self._try_init()

    def _try_init(self):
        try:
            import os
            import platform
            # On Linux, ensure the unprivileged X11/xorg backend is used when available
            # so no special root or /dev/uinput group permissions are required across distros.
            if platform.system() == "Linux" and os.environ.get("DISPLAY") and not os.environ.get("PYNPUT_BACKEND_KEYBOARD"):
                os.environ["PYNPUT_BACKEND_KEYBOARD"] = "xorg"
            from pynput import keyboard as pynput_keyboard
            self._pynput_keyboard = pynput_keyboard
            self._available = True
        except ImportError:
            logger.warning("pynput package not available — hotkeys disabled")
            self._available = False
        except Exception as e:
            import platform
            if platform.system() == "Linux":
                logger.warning(f"Hotkey init failed (needs an active desktop session): {e}")
            elif platform.system() == "Darwin":
                logger.warning(f"Hotkey init failed (requires accessibility permissions on macOS): {e}")
            else:
                logger.warning(f"Hotkey init error: {e}")
            self._available = False

    # ── Listener lifecycle ────────────────────────────────────────────────────

    def _rebuild_listener_locked(self) -> None:
        """(Re)start the single GlobalHotKeys listener from _combos.
        Caller must hold _bindings_lock."""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if not self._combos or not self._pynput_keyboard:
            return

        def _fire_for(hk: str):
            mods = self._mods.get(hk, [])

            def _fire():
                # Checked here, on the listener thread, while the keys are
                # still down — not in the queued slot, where the user may
                # already have let go.
                if not _modifiers_held(mods):
                    logger.debug(
                        f"Hotkey {hk} ignored: modifiers not actually held")
                    self._clear_listener_state()
                    return
                self._trigger.emit(hk)
            return _fire

        mapping = {combo: _fire_for(hk) for hk, combo in self._combos.items()}
        try:
            listener = self._pynput_keyboard.GlobalHotKeys(mapping)
            listener.daemon = True
            listener.start()
            self._listener = listener
        except Exception as e:
            logger.warning(f"Could not start hotkey listener: {e}")
            self._listener = None


    def _clear_listener_state(self):
        """Drop pynput's idea of which keys are down.

        Without this a modifier whose key-up was missed stays "held" for the
        rest of the session, so every later press of the remaining keys looks
        like the full combination.
        """
        listener = self._listener
        for hk in getattr(listener, "_hotkeys", ()) or ():
            try:
                hk._state.clear()
            except Exception:
                pass

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, hotkey: str, callback: Callable) -> bool:
        if not self._available:
            return False
        hotkey = normalize_hotkey(hotkey)
        try:
            self.unregister(hotkey)
            combo = _to_pynput_combo(hotkey)
            # Validate the combo up front so a bad string fails HERE (and
            # register returns False) instead of killing the shared listener.
            self._pynput_keyboard.HotKey.parse(combo)
            with self._bindings_lock:
                self._bindings[hotkey] = callback
                self._combos[hotkey] = combo
                self._mods[hotkey] = _split_hotkey(hotkey)[0]
                self._rebuild_listener_locked()
            if _wayland_without_x():
                # Registered, and it will never fire. Only the compositor
                # sees a key that is not addressed to a focused window, and
                # Wayland has no protocol for a client to ask for one — so
                # pynput's X backend has nothing to listen to. Said out
                # loud, because "Hotkey registered" on its own reads as
                # working and the user is left wondering why nothing
                # happens. XWayland gives it back.
                logger.warning(
                    "Hotkey %s registered but a Wayland session with no X "
                    "display cannot deliver it — global shortcuts need "
                    "XWayland, or the overlay can be opened from the tray "
                    "and the window.", hotkey)
            else:
                logger.info(f"Hotkey registered: {hotkey} (pynput: {combo})")
            return True
        except Exception as e:
            logger.error(f"Hotkey register error ({hotkey}): {e}")
            # Clean up partial registration on failure
            with self._bindings_lock:
                self._bindings.pop(hotkey, None)
                self._combos.pop(hotkey, None)
                self._mods.pop(hotkey, None)
                try:
                    self._rebuild_listener_locked()
                except Exception:
                    pass
            return False

    def unregister(self, hotkey: str):
        hotkey = normalize_hotkey(hotkey)
        with self._bindings_lock:
            if not self._available or hotkey not in self._bindings:
                return
            self._bindings.pop(hotkey, None)
            self._combos.pop(hotkey, None)
            self._mods.pop(hotkey, None)
            try:
                self._rebuild_listener_locked()
            except Exception as e:
                logger.debug(f"Hotkey unregister warning ({hotkey}): {e}")

    def unregister_all(self):
        with self._bindings_lock:
            keys = list(self._bindings.keys())
        for hk in keys:
            self.unregister(hk)

    def update_hotkey(self, old_hotkey: str, new_hotkey: str, callback: Callable) -> bool:
        """Atomically switch hotkey.  Rolls back to old_hotkey if registration fails.

        If both the new registration AND the rollback fail, logs a warning
        so the caller knows no hotkey is active.
        """
        self.unregister(old_hotkey)
        if not self.register(new_hotkey, callback):
            if not self.register(old_hotkey, callback):
                logger.warning(f"Hotkey rollback also failed — no hotkey registered "
                               f"(old={old_hotkey!r}, new={new_hotkey!r})")
            return False
        return True

    # ── Internal slot (GUI thread) ────────────────────────────────────────────

    @Slot(str)
    def _on_trigger(self, hotkey: str):
        """Called on the Qt GUI thread — safe to call any Qt code."""
        with self._bindings_lock:
            cb = self._bindings.get(hotkey)
        if cb:
            try:
                cb()
            except Exception as e:
                logger.error(f"Hotkey callback error ({hotkey}): {e}")

    @property
    def is_available(self) -> bool:
        return self._available


_hotkey_mgr: Optional[HotkeyManager] = None
_hotkey_lock = threading.Lock()


def get_hotkey_manager() -> HotkeyManager:
    global _hotkey_mgr
    if _hotkey_mgr is None:
        with _hotkey_lock:
            if _hotkey_mgr is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("HotkeyManager requires QApplication — create QApplication first")
                _hotkey_mgr = HotkeyManager()
    return _hotkey_mgr
