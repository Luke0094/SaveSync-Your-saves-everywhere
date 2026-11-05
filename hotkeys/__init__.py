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
import sys
import threading
import time
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

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


# Key names → virtual-key codes, for reading the keyboard state directly (Windows; see HotkeyManager._watch_tick).
_VK_NAMED = {"space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
             "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
             "down": 0x28}
_WATCH_MS = 25                  # how often the keyboard state is read
_SILENT_AFTER_S = 0.25          # a shortcut held this long (or let go) with no sign from the listener: it is silent
_RESTART_GAP_S = 2.0
_DOUBLE_FIRE_S = 0.3            # the same shortcut delivered twice this close is one press seen by both ways


def _vk_for(key: str) -> int:
    """The virtual-key code of a hotkey's key name ("s", "f9", "esc"), or 0 when it has none we know."""
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x6F + int(key[1:])
    return _VK_NAMED.get(key, 0)


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
        # Once the shortcut stopped answering while the listener's thread was alive and nothing said why. A stalled main
        # thread is not the reason on Windows 11 (the hook survived stalls of 8 s with a key arriving in them - measured),
        # so the cause is not known. Whatever it is, the shortcut must not depend on it: the real keyboard state is read as
        # well, and a shortcut that is held while the listener says nothing is fired from here, the listener is made
        # again, and it is written to the log so the cause can be found.
        self._last_fire: Dict[str, float] = {}     # hotkey_str → when the listener last reported it
        self._held_since: Dict[str, float] = {}    # hotkey_str → when its keys went down (inf: already judged)
        self._watch_timer = None
        self._last_tick = time.monotonic()
        self._restarting = False
        self._generation = 0                       # bumped whenever the bindings change: a listener built for older ones is dropped
        self._last_restart = 0.0
        self._last_delivered: Dict[str, float] = {}   # hotkey_str → when its callback last ran
        self.silent_count = 0
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
        self._generation += 1
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if not self._combos or not self._pynput_keyboard:
            return
        mapping = self._mapping_locked()
        try:
            listener = self._pynput_keyboard.GlobalHotKeys(mapping)
            listener.daemon = True
            listener.start()
            self._listener = listener
        except Exception as e:
            logger.warning(f"Could not start hotkey listener: {e}")
            self._listener = None

    def _mapping_locked(self) -> dict:
        """The pynput mapping (combo -> what to do) for the current bindings. Caller must hold _bindings_lock."""
        def _fire_for(hk: str):
            mods = self._mods.get(hk, [])

            def _fire():
                self._last_fire[hk] = time.monotonic()
                # Checked here, on the listener thread, while the keys are
                # still down — not in the queued slot, where the user may
                # already have let go.
                if not _modifiers_held(mods):
                    logger.info(
                        f"Hotkey {hk} ignored: modifiers not actually held")
                    self._clear_listener_state()
                    return
                self._trigger.emit(hk)
            return _fire

        return {combo: _fire_for(hk) for hk, combo in self._combos.items()}


    # ── The listener being silent ─────────────────────────────────────────────────────────────────────────

    def _start_watch(self) -> None:
        if sys.platform != "win32" or self._watch_timer is not None:
            return
        self._last_tick = time.monotonic()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(_WATCH_MS)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start()

    def _stop_watch(self) -> None:
        if self._watch_timer is not None:
            self._watch_timer.stop()
            self._watch_timer.deleteLater()
            self._watch_timer = None
        self._held_since.clear()

    def _watch_tick(self) -> None:
        """Windows: is a registered shortcut physically held while the listener has not reported it? Then it is fired from here."""
        try:
            import ctypes
            get_state = ctypes.windll.user32.GetAsyncKeyState
        except (ImportError, AttributeError, OSError):
            return
        now = time.monotonic()
        # When the GUI thread was busy (showing the overlay, say) this tick comes late, and a shortcut that is first SEEN now went down
        # some time ago - while the listener, on its own thread, reported it at once. Dated from now, it would look unreported.
        late = max(0.0, now - self._last_tick - _WATCH_MS / 1000.0)
        self._last_tick = now
        with self._bindings_lock:
            combos = list(self._combos)
        for hk in combos:
            mods, keys = _split_hotkey(hk)
            vk = _vk_for(keys[0]) if len(keys) == 1 else 0
            if not vk:
                continue
            down = bool(get_state(vk) & 0x8000) and _modifiers_held(mods)
            since = self._held_since.get(hk)
            if since is None:
                if down:
                    self._held_since[hk] = now - min(late, 2.0)   # it has just gone down: judged a moment later
                continue
            if since == float("inf"):                     # judged already: wait for it to be let go
                if not down:
                    self._held_since.pop(hk, None)
                continue
            if down and now - since < _SILENT_AFTER_S:
                continue                                  # (judged when it is let go, or when it has been held long enough)
            if down:
                self._held_since[hk] = float("inf")
            else:
                self._held_since.pop(hk, None)
            if self._last_fire.get(hk, 0.0) >= since - 0.05:
                logger.debug("Hotkey %s: pressed, the listener reported it", hk)
                continue                                  # the listener reported it: all is well
            self.silent_count += 1
            logger.warning("Hotkey %s was pressed and the listener stayed silent - fired from the keyboard state, "
                           "listener made again", hk)
            self._trigger.emit(hk)
            if now - self._last_restart >= _RESTART_GAP_S:
                self._last_restart = now
                self.restart_listener("it stayed silent")

    def restart_listener(self, reason: str = "") -> None:
        """Start the listener again from the current bindings, on a thread of its own (making it can take seconds on a busy machine).
        Done when a game starts - the moment the hotkey is wanted - and whenever the shortcut has been caught going unanswered
        (see _watch_tick)."""
        if not self._available:
            return
        with self._bindings_lock:
            if not self._combos:
                return
            mapping = self._mapping_locked()
            generation = self._generation
        if self._restarting:
            return
        self._restarting = True
        # Building the listener is not a few milliseconds when the machine is busy: pynput reads the keyboard layout while it is made,
        # and with a game starting that took 6 seconds (measured in a log) - with the GUI thread, and the shortcut's watcher, waiting
        # for it. So it is made on a thread of its own, outside the lock the watcher takes.
        threading.Thread(target=self._restart_worker, args=(mapping, generation, reason), daemon=True,
                         name="hotkey-listener").start()

    def _restart_worker(self, mapping: dict, generation: int, reason: str) -> None:
        try:
            fresh = None
            try:
                fresh = self._pynput_keyboard.GlobalHotKeys(mapping)
                fresh.daemon = True
                fresh.start()
            except Exception as e:
                logger.warning(f"Could not start hotkey listener: {e}")
                fresh = None
            old = None
            with self._bindings_lock:
                if fresh is not None and generation == self._generation:
                    old, self._listener = self._listener, fresh
                    fresh = None
            for dropped in (old, fresh):                  # the one that was replaced, or the new one if the bindings changed meanwhile
                if dropped is not None:
                    try:
                        dropped.stop()
                    except Exception:
                        pass
            logger.debug(f"Hotkey listener restarted{(' (' + reason + ')') if reason else ''}")
        finally:
            self._restarting = False

    def modifiers_in_use(self) -> set:
        """The modifiers ("ctrl", "alt", "shift", "win") any registered shortcut is made of."""
        out = set()
        for hk in list(self._bindings):
            out |= set(_split_hotkey(hk)[0])
        return out

    def chord_matches(self, key: str, modifiers: set) -> bool:
        """True when *key* (lower case: "s", "f5", "esc") pressed with exactly *modifiers* held ("ctrl", "alt", "shift",
        "win") is one of the registered shortcuts. Whoever takes keys away from the program in front (typing into one of our
        windows over a game) uses it to leave the shortcut alone."""
        mods = set(modifiers)
        for hk in list(self._bindings):
            hk_mods, hk_keys = _split_hotkey(hk)
            if hk_keys == [key] and set(hk_mods) == mods:
                return True
        return False

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
            self._start_watch()
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
            self._held_since.pop(hotkey, None)
            try:
                self._rebuild_listener_locked()
            except Exception as e:
                logger.debug(f"Hotkey unregister warning ({hotkey}): {e}")
            if not self._combos:
                self._stop_watch()

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
        # The listener and the watcher (_watch_tick) can both report one press when the listener answers late: the shortcut
        # that opens the overlay is a toggle, and a second delivery would close it again at once.
        now = time.monotonic()
        if now - self._last_delivered.get(hotkey, -10.0) < _DOUBLE_FIRE_S:
            logger.info("Hotkey %s delivered twice for one press - the second one is ignored", hotkey)
            return
        self._last_delivered[hotkey] = now
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
