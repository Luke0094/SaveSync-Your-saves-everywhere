"""SaveSync - a small round number: "how many" on a button's corner (the
sidebar's friends button) or beside a name (a friend with unread messages)."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QLabel

from ui.helpers import scaled
from ui.styles.theme import palette

_MAX_SHOWN = 9


class CountBadge(QLabel):
    """Shows *n* (``9+`` past nine) and hides itself at zero. With *corner_of*
    it floats on the top-right corner of that widget and follows it when it is
    resized; without, it is an ordinary label to put in a layout."""

    def __init__(self, parent=None, corner_of=None):
        super().__init__(parent if corner_of is None else corner_of)
        self._count = 0
        self._anchor = corner_of
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h = scaled(14, self)
        self.setFixedHeight(h)
        self.setStyleSheet(
            f"QLabel {{ background-color: {palette('accent')}; color: {palette('accent_text')};"
            f" border: 1px solid {palette('bg')}; border-radius: {h // 2}px;"
            f" font-size: {scaled(9, self)}px; font-weight: 700; padding: 0 3px; }}")
        self.setVisible(False)
        if corner_of is not None:
            self._filter = _FollowResize(self)
            corner_of.installEventFilter(self._filter)

    @property
    def count(self) -> int:
        return self._count

    def shown_text(self) -> str:
        return "" if self._count <= 0 else (str(self._count) if self._count <= _MAX_SHOWN
                                            else f"{_MAX_SHOWN}+")

    def set_count(self, n: int) -> None:
        self._count = max(0, int(n))
        self.setText(self.shown_text())
        self.setVisible(self._count > 0)
        if self._count > 0:
            h = self.height()
            self.setFixedWidth(max(h, self.fontMetrics().horizontalAdvance(self.text()) + scaled(10, self)))
            self.reposition()
            self.raise_()

    def reposition(self) -> None:
        a = self._anchor
        if a is not None:
            self.move(max(0, a.width() - self.width()), 0)


class _FollowResize(QObject):
    def __init__(self, badge: CountBadge):
        super().__init__(badge)
        self._badge = badge

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._badge.reposition()
        return False
