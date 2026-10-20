"""SaveSync - the mark that says whether a friend is there: green when they are, red when not.

Drawn in the theme's own colours at the moment it is painted (so it follows a change of theme), and
never alone: whoever shows one also gives its words - a tooltip, an accessible name - because green and
red are not the same thing to everybody.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ui.styles.theme import palette


def presence_color(online: bool) -> QColor:
    return QColor(palette("success" if online else "error"))


class PresenceDot(QWidget):
    """A small round mark, *size* pixels across."""

    def __init__(self, online: bool = False, size: int = 10, parent=None):
        super().__init__(parent)
        self._online = bool(online)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)     # (a click goes to what it sits on)

    @property
    def online(self) -> bool:
        return self._online

    def set_online(self, online: bool) -> None:
        online = bool(online)
        if online != self._online:
            self._online = online
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(presence_color(self._online))
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        p.end()


def dot_icon(online: bool, size: int = 12) -> QIcon:
    """The same mark as an icon, for a menu entry."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(presence_color(online))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)
