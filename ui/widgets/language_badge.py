"""SaveSync — the language readout beside a game's version.

One language shows as itself — English when the game has it, the first
otherwise — and always with a ▾, because a click opens the list as a menu with
a bin on each row: a game bought in a regional store may carry languages the
user's own copy does not, and those can be taken off. A game with several also
opens the whole list on hover, one language per line.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget,
                               QWidgetAction)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette


class _LanguageList(QFrame):
    """The hover list: a small frameless window that never takes the pointer."""

    def __init__(self, languages: list, owner):
        super().__init__(owner.window(),
                         Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName("language_list")
        self.setStyleSheet(
            f"#language_list{{background:{palette('bg_card')};"
            f"border:1px solid {palette('border_hover')};border-radius:8px;}}"
            f"QLabel{{color:{palette('text')};font-size:{scaled(11, owner)}px;"
            f"padding:0 6px;background:transparent;}}"
            f"QLabel#language_list_hint{{color:{palette('text_muted')};"
            f"font-size:{scaled(10, owner)}px;}}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        for name in languages:
            lay.addWidget(QLabel(name))
        hint = QLabel(t('add_game.languages_edit_hint'))
        hint.setObjectName("language_list_hint")
        lay.addWidget(hint)


class _LanguageMenuRow(QWidget):
    """One row of the click menu: the language and a bin. Only the bin acts —
    a click on the name does nothing, so a language is never dropped by a
    stray click — and the row under the pointer lights up in the accent. Built
    like
    the exe-version menu row (a QWidgetAction's widget paints its own
    background and colours; hover is tracked here)."""
    remove_requested = Signal()

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("language_menu_row")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 6, 4)
        row.setSpacing(12)
        self._lbl = QLabel(name)
        row.addWidget(self._lbl, 1)
        trash = QToolButton()
        trash.setText("\U0001f5d1")
        trash.setAutoRaise(True)
        trash.setCursor(Qt.CursorShape.PointingHandCursor)
        trash.setToolTip(t('add_game.languages_remove_hint', name=name))
        trash.setStyleSheet(
            "QToolButton{border:none;background:transparent;border-radius:4px;padding:2px;}"
            f"QToolButton:hover{{background:{palette('error')};}}")
        trash.clicked.connect(self.remove_requested.emit)
        row.addWidget(trash)
        self._apply_colors(hover=False)

    def _apply_colors(self, hover: bool) -> None:
        # The accent, as every highlighted row in the app: green with the theme's
        # text-on-accent colour (black on the dark theme's, white on the light's).
        bg = palette('accent') if hover else palette('bg_card')
        fg = palette('accent_text') if hover else palette('text')
        self.setStyleSheet(f"#language_menu_row{{background:{bg};border-radius:6px;}}")
        self._lbl.setStyleSheet(
            f"color:{fg};background:transparent;font-size:{scaled(11, self)}px;")

    # Hover is tracked here, in Python, exactly as the exe version dropdown's rows
    # do it (see _ExeVersionMenuRow): the row is lit while the pointer is in it.
    def enterEvent(self, event):
        self._apply_colors(hover=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_colors(hover=False)
        super().leaveEvent(event)


class LanguageBadge(QLineEdit):
    """Chip styled like the version and engine readouts beside it. Read-only
    text; a click opens the menu that removes languages."""
    languages_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._languages: list = []
        self._popup = None
        self._menu = None
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(scaled(22, self, min_px=20))
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        font = self.font()
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setStyleSheet(
            f"QLineEdit{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;"
            f"padding:1px 8px;font-size:{scaled(11, self)}px;font-weight:600;}}")
        self.setVisible(False)

    def languages(self) -> list:
        return list(self._languages)

    def set_languages(self, languages) -> None:
        """Show *languages* (English names, English first). Hidden when empty."""
        self._languages = list(languages or [])
        self._hide_list()
        if not self._languages:
            self.setText("")
            self.setToolTip("")
            self.setVisible(False)
            return
        many = len(self._languages) > 1
        self.setText(self._languages[0] + "  ▾")
        # With several the hover list carries the hint; one has no list.
        self.setToolTip("" if many else t('add_game.languages_edit_hint'))
        self.setCursorPosition(0)
        fm = self.fontMetrics()
        text = self.text()
        text_w = max(fm.horizontalAdvance(text), fm.boundingRect(text).width())
        self.setFixedWidth(min(max(text_w + 8 + 8 + 1 + 1 + 10, 36), 220))
        self.setVisible(True)

    # ── click menu ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._languages:
            self._show_menu()
            event.accept()
            return
        super().mousePressEvent(event)

    def _new_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:10px;padding:6px;}}")
        return menu

    def _show_menu(self) -> None:
        """One row per language. A bin drops its row and the menu stays open,
        so several can go in one visit; the change is announced at once and
        the owner decides when it is kept (Save) or thrown away (Cancel)."""
        self._hide_list()
        menu = self._menu = self._new_menu()
        actions: dict = {}
        for name in self._languages:
            row = _LanguageMenuRow(name, menu)
            action = QWidgetAction(menu)
            action.setDefaultWidget(row)
            menu.addAction(action)
            actions[name] = action
            row.remove_requested.connect(
                lambda n=name: self._remove_from_menu(n, menu, actions))
        menu.setMinimumWidth(max(self.width(), 140))
        try:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
        finally:
            self._menu = None
            menu.deleteLater()

    def _remove_from_menu(self, name: str, menu: QMenu, actions: dict) -> None:
        action = actions.pop(name, None)
        if action is not None:
            menu.removeAction(action)
        remaining = [n for n in self._languages if n != name]
        self.set_languages(remaining)
        self.languages_changed.emit(list(remaining))
        if not remaining:
            menu.close()
        else:
            menu.adjustSize()

    # ── hover list ───────────────────────────────────────────────────────────

    def enterEvent(self, event):
        super().enterEvent(event)
        if len(self._languages) > 1 and self._popup is None and self._menu is None:
            self._popup = _LanguageList(self._languages, self)
            self._popup.adjustSize()
            self._popup.move(self.mapToGlobal(self.rect().bottomLeft()))
            self._popup.show()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hide_list()

    def hideEvent(self, event):
        self._hide_list()
        super().hideEvent(event)

    def _hide_list(self) -> None:
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.hide()
                popup.deleteLater()
            except RuntimeError:
                pass
