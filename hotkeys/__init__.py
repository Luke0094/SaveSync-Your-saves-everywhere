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
        self._bindings_lock = threading.Lock()
        self._listener = None
        self._available = False
        self._pynput_keyboard = None
        from PySide6.QtCore import Qt
        self._trigger.connect(self._on_trigger, Qt.ConnectionType.QueuedConnection)
        self._try_init()

    def _try_init(self):
        try:
            from pynput import keyboard as pynput_keyboard
            self._pynput_keyboard = pynput_keyboard
            self._available = True
        except ImportError:
            logger.warning("pynput package not available — hotkeys disabled")
            self._available = False
        except Exception as e:
            import platform
            if platform.system() == "Linux":
                logger.warning(f"Hotkey init failed (needs an X11/uinput-capable session): {e}")
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
        if not self._combos:
            return

        def _fire_for(hk: str):
            def _fire():
                self._trigger.emit(hk)
            return _fire

        mapping = {combo: _fire_for(hk) for hk, combo in self._combos.items()}
        listener = self._pynput_keyboard.GlobalHotKeys(mapping)
        listener.daemon = True
        listener.start()
        self._listener = listener

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, hotkey: str, callback: Callable) -> bool:
        if not self._available:
            return False
        try:
            self.unregister(hotkey)
            combo = _to_pynput_combo(hotkey)
            # Validate the combo up front so a bad string fails HERE (and
            # register returns False) instead of killing the shared listener.
            self._pynput_keyboard.HotKey.parse(combo)
            with self._bindings_lock:
                self._bindings[hotkey] = callback
                self._combos[hotkey] = combo
                self._rebuild_listener_locked()
            logger.info(f"Hotkey registered: {hotkey} (pynput: {combo})")
            return True
        except Exception as e:
            logger.error(f"Hotkey register error ({hotkey}): {e}")
            # Clean up partial registration on failure
            with self._bindings_lock:
                self._bindings.pop(hotkey, None)
                self._combos.pop(hotkey, None)
                try:
                    self._rebuild_listener_locked()
                except Exception:
                    pass
            return False

    def unregister(self, hotkey: str):
        with self._bindings_lock:
            if not self._available or hotkey not in self._bindings:
                return
            self._bindings.pop(hotkey, None)
            self._combos.pop(hotkey, None)
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
