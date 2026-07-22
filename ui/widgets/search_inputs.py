"""
SaveSync - Shared search input widgets.

Self-contained input widgets used by the pages' search bars:

- ClearableLineEdit — line edit with an embedded "×" clear button
  (library sidebar search).
- _GhostLineEdit — paints a non-physical completion hint after the typed
  text (backups title search).
- _SearchCombo — editable combo that swallows arrow/wheel navigation of
  the CLOSED native list and notifies when the native popup opens.
- _SuggestPopup — lightweight typing-suggestions dropdown fully driven by
  its page (no focus stealing).

The underscore names are kept from their original in-page definitions to
keep call sites and object names stable across the extraction.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox, QFrame, QLineEdit, QListWidget, QListWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from i18n import t
from ui.styles.theme import palette, ThemedMixin


class ClearableLineEdit(QLineEdit):
    """QLineEdit with a small red circular "×" clear button embedded on the
    right, visible only while there's text, to instantly clear it. The
    button repositions itself on resize so it stays flush with the right
    edge even if the field is later resized (e.g. a resizable sidebar)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clear_btn = QToolButton(self)
        self._clear_btn.setText("×")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setToolTip(t("common.clear"))
        self._clear_btn.setFixedSize(15, 15)
        self._clear_btn.setStyleSheet(
            "QToolButton{background:transparent;color:#d16565;"
            "border:1px solid #d16565;border-radius:7px;"
            "font-size:10px;font-weight:bold;padding:0px;}"
            "QToolButton:hover{background:#d16565;color:#ffffff;}"
        )
        self._clear_btn.setVisible(False)
        self._clear_btn.clicked.connect(self.clear)
        self.textChanged.connect(lambda text: self._clear_btn.setVisible(bool(text)))
        # Reserve space on the right so typed text never runs under the button
        self.setTextMargins(0, 0, 20, 0)
        self._position_clear_btn()

    def _position_clear_btn(self):
        r = self.rect()
        x = r.right() - self._clear_btn.width() - 4
        y = (r.height() - self._clear_btn.height()) // 2
        self._clear_btn.move(x, y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_clear_btn()


class _GhostPaintMixin:
    """Paints a non-physical completion hint after the typed text. The hint
    is pure paint — it is never inserted into the buffer, so the user can
    keep editing at any moment and Backspace/Delete behave exactly like on
    plain text. Mixin so the same behavior attaches to different QLineEdit
    flavors (plain, clearable)."""

    _ghost: str = ""

    def set_ghost(self, text: str):
        if text != self._ghost:
            self._ghost = text
            self.update()

    def ghost(self) -> str:
        """The completion hint currently painted ("" when none)."""
        return self._ghost

    def keyPressEvent(self, event):
        # ↓ with a visible ghost = explicit accept gesture (ghost_accepted).
        # Deliberately NOT Enter: Enter must keep meaning "take my text as
        # typed", or a new entry that happens to prefix an existing one
        # could never be entered. Hosts that route ↓ elsewhere (the backups
        # page popup) consume the key in an event filter before it gets
        # here, so this never fights them.
        if (event.key() == Qt.Key.Key_Down and self._ghost
                and self.cursorPosition() == len(self.text())):
            self.ghost_accepted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # A click ON the painted hint is the same accept gesture as ↓.
        if self._ghost and self.text() and self.cursorPosition() == len(self.text()):
            start = self.cursorRect().right() + 2
            width = self.fontMetrics().horizontalAdvance(self._ghost)
            x = event.position().x()
            if start <= x <= start + width + 4:
                self.ghost_accepted.emit()
                event.accept()
                return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._ghost or not self.text() or not self.hasFocus():
            return
        # Only meaningful while the caret sits at the end of the typed text
        if self.cursorPosition() != len(self.text()):
            return
        p = QPainter(self)
        p.setPen(QColor(palette('text_hint')))
        cr = self.cursorRect()
        rect = self.rect()
        p.drawText(
            cr.right() + 2, rect.top(),
            rect.width() - cr.right() - 4, rect.height(),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self._ghost,
        )
        p.end()


class _GhostLineEdit(_GhostPaintMixin, QLineEdit):
    """Plain line edit with the ghost completion hint (backups title
    search, add/edit tag input)."""

    ghost_accepted = Signal()   # ↓ key or click on the painted hint

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ghost = ""


class GhostClearableLineEdit(_GhostPaintMixin, ClearableLineEdit):
    """ClearableLineEdit + ghost completion hint (library sidebar tag
    search): embedded "×" clear button AND the paint-only completion."""

    ghost_accepted = Signal()   # ↓ key or click on the painted hint

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ghost = ""


class _SearchCombo(QComboBox):
    """Editable combo whose native dropdown ("Tutti i titoli" + full list)
    notifies the page when opened, so the typing-suggestions popup can get
    out of the way. The native list is the ONLY place the all-titles reset
    entry appears — it is never offered while typing.

    Arrow keys and the mouse wheel are swallowed here: QComboBox would
    otherwise navigate the CLOSED native list (emitting activated), silently
    replacing the active filter/placeholder with whatever title happens to be
    next — the "invisible list scrolling" bug. Typing-popup navigation is
    handled separately by the page's event filter on the line edit; the open
    native popup has its own view and is unaffected."""

    _NAV_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down,
                 Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_native_popup = None   # set by the page
        self.on_nav_key = None        # set by the page — typing-popup routing

    def showPopup(self):
        if callable(self.on_native_popup):
            self.on_native_popup()
        super().showPopup()

    def keyPressEvent(self, event):
        if event.key() in self._NAV_KEYS:
            # An arrow reaching the COMBO means the page's line-edit filter
            # did not consume it (event-filter ordering is environment
            # dependent). Swallowing it silently here is what made ↑/↓ dead
            # on the typing popup in those environments — route it to the
            # popup instead, so navigation works through EITHER path. The
            # closed native list must still never be driven by arrows, so
            # the event is consumed either way.
            if callable(self.on_nav_key) and event.key() in (
                    Qt.Key.Key_Up, Qt.Key.Key_Down):
                self.on_nav_key(event.key())
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        event.accept()


class _SuggestPopup(QFrame, ThemedMixin):
    """Lightweight suggestions dropdown shown while typing in the search
    field. It is a plain child widget (not a top-level window), takes no
    focus, and is fully driven by the page: the line edit keeps keyboard
    focus the whole time, arrows just move the highlight here."""

    item_activated = Signal(int)   # row index

    MAX_VISIBLE_ROWS = 8

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("backup_suggest_popup")
        self._sty(self, lambda: (
            f"QFrame#backup_suggest_popup{{background:{palette('bg_card')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;}}"
        ))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self._list = QListWidget()
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sty(self._list, lambda: (
            f"QListWidget{{background:transparent;border:none;font-size:12px;"
            f"color:{palette('text_secondary')};}}"
            f"QListWidget::item{{padding:5px 8px;border-radius:4px;}}"
            f"QListWidget::item:selected{{background:{palette('accent')};"
            f"color:{palette('accent_text')};}}"
        ))
        self._list.itemClicked.connect(
            lambda item: self.item_activated.emit(self._list.row(item)))
        lay.addWidget(self._list)
        self.hide()

    def set_items(self, labels: list, select_first: bool = True,
                  keep_selection: bool = True):
        """Rebuild the rows.

        With *keep_selection* (default) the previously highlighted label is
        KEPT highlighted when still present — a refresh (typing debounce,
        background rebuild) must never yank the user's ↓/↑ navigation back
        to the first row; resetting to row 0 on every set_items is exactly
        what made the highlight look stuck on the first entry. Hosts where
        a text edit must RETURN TO THE NULL selection instead (the tag
        input: only explicit arrow navigation may select) pass
        keep_selection=False. With no carried-over selection: row 0 when
        *select_first*, else NO selection."""
        prev = None
        if keep_selection:
            prev_item = self._list.currentItem()
            prev = prev_item.text() if prev_item else None
        self._list.clear()
        for label in labels:
            self._list.addItem(QListWidgetItem(label))
        if labels:
            if prev is not None and prev in labels:
                self._list.setCurrentRow(labels.index(prev))
            elif select_first:
                self._list.setCurrentRow(0)
            else:
                self._list.setCurrentRow(-1)
        row_h = self._list.sizeHintForRow(0) if labels else 0
        visible = min(len(labels), self.MAX_VISIBLE_ROWS)
        self.setFixedHeight(max(1, visible * max(row_h, 20) + 10))

    def clear_selection(self):
        """Back to the NULL selection (no highlighted row)."""
        self._list.setCurrentRow(-1)

    def move_selection(self, delta: int):
        n = self._list.count()
        if not n:
            return
        row = self._list.currentRow()
        self._list.setCurrentRow(max(0, min(n - 1, row + delta)))

    def current_row(self) -> int:
        return self._list.currentRow()

    def count(self) -> int:
        return self._list.count()
