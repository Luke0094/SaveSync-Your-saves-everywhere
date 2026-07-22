"""
HotkeyEdit — capture key combinations safely.
- Builds key map at runtime using int values, no missing Qt.Key attributes
- Escape restores previous saved value
- F1-F12 and 0-9 all work
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLineEdit
import i18n
from ui.styles.theme import palette

# Modifier keys by their Qt int values
_MOD_MAP: dict[int, str] = {
    int(Qt.Key.Key_Control): "ctrl",
    int(Qt.Key.Key_Shift):   "shift",
    int(Qt.Key.Key_Alt):     "alt",
    int(Qt.Key.Key_Meta):    "win",
}
try:
    _MOD_MAP[int(Qt.Key.Key_Super_L)] = "win"
    _MOD_MAP[int(Qt.Key.Key_Super_R)] = "win"
except AttributeError:
    pass

_MODIFIER_INTS = set(_MOD_MAP.keys())


def _build_special_map() -> dict[int, str]:
    """Build special-key map at runtime — only include keys that actually exist."""
    candidates = [
        # Function keys
        ("Key_F1","f1"),("Key_F2","f2"),("Key_F3","f3"),("Key_F4","f4"),
        ("Key_F5","f5"),("Key_F6","f6"),("Key_F7","f7"),("Key_F8","f8"),
        ("Key_F9","f9"),("Key_F10","f10"),("Key_F11","f11"),("Key_F12","f12"),
        # Navigation
        ("Key_Home","home"),("Key_End","end"),
        ("Key_PageUp","page up"),("Key_PageDown","page down"),
        ("Key_Insert","insert"),("Key_Delete","delete"),
        ("Key_Backspace","backspace"),("Key_Return","enter"),
        ("Key_Enter","enter"),("Key_Space","space"),("Key_Tab","tab"),
        ("Key_Up","up"),("Key_Down","down"),
        ("Key_Left","left"),("Key_Right","right"),
        # Punctuation — only add if attr exists
        ("Key_Minus","minus"),("Key_Equal","="),
        ("Key_BracketLeft","["),("Key_BracketRight","]"),
        ("Key_Backslash","\\"),("Key_Semicolon",";"),
        ("Key_Apostrophe","'"),("Key_Comma",","),
        ("Key_Period","."),("Key_Slash","/"),
        # Tilde/grave — name varies between Qt versions
        ("Key_Agrave","`"),("Key_Grave","`"),
        # Numpad
        ("Key_0","0"),("Key_1","1"),("Key_2","2"),("Key_3","3"),("Key_4","4"),
        ("Key_5","5"),("Key_6","6"),("Key_7","7"),("Key_8","8"),("Key_9","9"),
    ]
    result: dict[int, str] = {}
    for attr, name in candidates:
        try:
            k = int(getattr(Qt.Key, attr))
            if k not in result:   # first match wins (e.g. Key_Grave vs Key_Agrave)
                result[k] = name
        except AttributeError:
            pass
    return result


_SPECIAL_MAP = _build_special_map()


class HotkeyEdit(QLineEdit):
    """
    Click to start capture. Press modifier(s) + a key → saved.
    Escape → restores previous saved value.
    """
    hotkey_captured = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._capturing   = False
        self._current_mods: set[str] = set()
        self._saved_value = ""          # last confirmed value — restored on Escape
        self.setReadOnly(True)
        self.setPlaceholderText(i18n.t('hotkey.click_to_record'))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_idle_style()

    # ── Public helpers ────────────────────────────────────────────────────────

    def setText(self, text: str):
        super().setText(text)
        self._saved_value = text        # keep saved_value in sync when loaded externally

    # ── Events ────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if not self._capturing:
            self._start_capture()

    def focusOutEvent(self, event):
        if self._capturing:
            self._cancel_capture()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if not self._capturing:
            self._start_capture()
            return

        key_int = int(event.key())

        # Escape → cancel and restore
        if key_int == int(Qt.Key.Key_Escape):
            self._cancel_capture()
            return

        # Pure modifier press → accumulate and show preview
        if key_int in _MODIFIER_INTS:
            self._current_mods.add(_MOD_MAP[key_int])
            parts = sorted(self._current_mods)
            super().setText("+".join(parts) + "+…")
            return

        # Real key → finish
        name = self._key_to_name(key_int, event)
        if not name:
            return
        parts = sorted(self._current_mods) + [name]
        self._finish_capture("+".join(parts))

    def keyReleaseEvent(self, event: QKeyEvent):
        self._current_mods.discard(_MOD_MAP.get(int(event.key()), ""))
        # If all modifiers released without a real key, reset text
        if self._capturing and not self._current_mods:
            super().setText(i18n.t('hotkey.press_keys'))

    # ── Internals ─────────────────────────────────────────────────────────────

    def _start_capture(self):
        self._capturing    = True
        self._current_mods = set()
        super().setText(i18n.t('hotkey.press_keys'))
        self.setStyleSheet(f"QLineEdit {{ border: 1px solid {palette('accent')}; color: {palette('accent')}; "
                           f"background: {palette('bg_elevated')}; border-radius: 4px; padding: 4px 8px; }}")
        self.setFocus()

    def _finish_capture(self, combo: str):
        self._capturing    = False
        self._current_mods = set()
        self._saved_value  = combo
        super().setText(combo)
        self._set_idle_style()
        self.hotkey_captured.emit(combo)
        self.clearFocus()

    def update_locale(self):
        if not self._capturing:
            self.setPlaceholderText(i18n.t('hotkey.click_to_record'))

    def hideEvent(self, event):
        if self._capturing:
            self._cancel_capture()
        super().hideEvent(event)

    def _cancel_capture(self):
        self._capturing    = False
        self._current_mods = set()
        super().setText(self._saved_value)
        self._set_idle_style()
        self.clearFocus()

    def _set_idle_style(self):
        self.setStyleSheet(f"QLineEdit {{ border: 1px solid {palette('border_hover')}; color: {palette('text')}; "
                           f"background: {palette('bg_card')}; border-radius: 4px; padding: 4px 8px; }}")

    def _key_to_name(self, key_int: int, event: QKeyEvent) -> str:
        # 1. Special map (F-keys, arrows, etc.)
        if key_int in _SPECIAL_MAP:
            return _SPECIAL_MAP[key_int]
        # 2. A-Z range (Qt uses uppercase int values)
        if int(Qt.Key.Key_A) <= key_int <= int(Qt.Key.Key_Z):
            return chr(key_int).lower()
        # 3. Printable character from event text (handles numpad, symbols)
        txt = event.text().strip()
        if txt and txt.isprintable() and len(txt) == 1:
            return txt
        return ""
