"""SaveSync — grab a piece of the screen.

Used by the pin board: drag a rectangle over whatever is on screen and keep
it as a pinned thumbnail (a map fragment, a puzzle, a chat line).

The whole screen is grabbed ONCE, up front, and the picker then works on that
frozen copy. Selecting against the live screen would mean the picture moves
while you are drawing the box around it — and in a game it moves constantly.
"""
import logging

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog

logger = logging.getLogger(__name__)

_DIM = QColor(0, 0, 0, 110)
_EDGE = QColor(108, 92, 231)
# Smaller than this is a stray click, not a selection.
_MIN_SIDE = 8


class RegionPicker(QDialog):
    """Fullscreen picker over a frozen copy of the screen."""

    def __init__(self, screen, shot: QPixmap, parent=None):
        super().__init__(parent)
        self._shot = shot
        self._origin: QPoint | None = None
        self._rect = QRect()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen.geometry())
        self.setModal(True)

    def showEvent(self, event):
        super().showEvent(event)
        # Take the mouse and keyboard outright. The thing being drawn over is
        # usually a game that is itself listening for both: without the grab
        # the drag lands in the game — the camera turns, the character moves —
        # and the picker never sees the events it exists to receive.
        self.activateWindow()
        self.raise_()
        self.grabMouse()
        self.grabKeyboard()

    def hideEvent(self, event):
        self.releaseMouse()
        self.releaseKeyboard()
        super().hideEvent(event)

    @property
    def selection(self) -> QRect:
        return self._rect

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._shot)
        p.fillRect(self.rect(), _DIM)
        if self._rect.isNull():
            return
        # The chosen part is shown undimmed: what you see inside the box is
        # exactly what you get.
        src = QRect(self._rect)
        dpr = self._shot.devicePixelRatio()
        if dpr and dpr != 1.0:
            src = QRect(int(src.x() * dpr), int(src.y() * dpr),
                        int(src.width() * dpr), int(src.height() * dpr))
        p.drawPixmap(self._rect, self._shot, src)
        p.setPen(QPen(_EDGE, 1))
        p.drawRect(self._rect.adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._rect = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._rect = QRect(self._origin,
                               event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._origin is None:
            return
        self._origin = None
        if (self._rect.width() < _MIN_SIDE or self._rect.height() < _MIN_SIDE):
            self._rect = QRect()
            self.reject()
            return
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._rect = QRect()
            self.reject()
            return
        super().keyPressEvent(event)


def capture_region(parent=None) -> "QPixmap | None":
    """Let the player draw a box on screen and return that piece.

    Returns None when they cancel, or when nothing could be grabbed —
    a game in exclusive fullscreen hands back a black or empty frame,
    which is a limitation of the display mode, not something we can work
    around from outside the game.
    """
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    try:
        shot = screen.grabWindow(0)
    except Exception as e:
        logger.warning(f"Screen grab failed: {e}")
        return None
    if shot.isNull():
        return None

    picker = RegionPicker(screen, shot, parent)
    if picker.exec() != QDialog.DialogCode.Accepted:
        return None
    rect = picker.selection
    if rect.isNull():
        return None
    dpr = shot.devicePixelRatio()
    if dpr and dpr != 1.0:
        rect = QRect(int(rect.x() * dpr), int(rect.y() * dpr),
                     int(rect.width() * dpr), int(rect.height() * dpr))
    piece = shot.copy(rect)
    return None if piece.isNull() else piece
