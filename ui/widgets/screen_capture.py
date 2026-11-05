"""SaveSync — grab a piece of the screen.

Used by the pin board: drag a rectangle over whatever is on screen and keep
it as a pinned thumbnail (a map fragment, a puzzle, a chat line).

The whole screen is grabbed ONCE, up front, and the picker then works on that
frozen copy. Selecting against the live screen would mean the picture moves
while you are drawing the box around it — and in a game it moves constantly.

Over a game in exclusive fullscreen (Windows) the picker is a window of the higher band that is never activated
(ui.win_band_hook), like the overlay: it takes no focus and no keyboard grab - a right click cancels it, as does a click
without a drag - and it covers the game's monitor. Whether the game's frame can be read at all is the game's doing: a frame
that comes back empty is reported (looks_blank) instead of being offered for selection.
"""
import logging

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog

logger = logging.getLogger(__name__)

# Set by capture_region: the last grab was an empty frame (what a game that hides its picture from screen capture hands back).
last_grab_blank = False
_BLANK_SHARE = 0.95
_DIM = QColor(0, 0, 0, 110)
_EDGE = QColor(108, 92, 231)
# Smaller than this is a stray click, not a selection.
_MIN_SIDE = 8


def looks_blank(shot: QPixmap) -> bool:
    """True when (nearly) every sampled pixel of *shot* is pure black: a frame that was not really read. Darkness is not
    blankness - a dark scene has noise in it, and only a frame with nothing at all in it passes."""
    img = shot.toImage()
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return True
    step_x, step_y = max(1, w // 64), max(1, h // 36)
    total = black = 0
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            total += 1
            if (img.pixel(x, y) & 0x00FFFFFF) == 0:
                black += 1
    return total > 0 and black / total >= _BLANK_SHARE


class RegionPicker(QDialog):
    """Fullscreen picker over a frozen copy of the screen."""

    def __init__(self, screen, shot: QPixmap, parent=None, exclusive: bool = False):
        super().__init__(parent)
        self._shot = shot
        self._exclusive = exclusive
        self._origin: QPoint | None = None
        self._rect = QRect()
        flags = (Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowStaysOnTopHint |
                 Qt.WindowType.Tool)
        if exclusive:
            # A window that cannot take the focus, of the higher band (made so while the session is on), translucent so that it
            # is layered from the start. It is the whole screen: no title strip (the session leaves it alone).
            self.setProperty("xs_adopted", True)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
            from ui.exclusive_session import hold_open
            hold_open(self, "capture-picker")
        # X11 with a game in front: like the exclusive picker, it never takes the focus or the keyboard. It used to (activateWindow +
        # grabKeyboard): the game lost the focus - and one that pauses when it does, paused - and, no longer being the active window, was
        # no longer seen as the fullscreen window in front, so the pin that came out of the picker was made as an ordinary window that
        # the window manager focused too. Nor is it a window the window manager places: it put the fullscreen picker under the panel,
        # 27 px off the picture it shows. A right click, or a click without a drag, cancels.
        self._x11_quiet = False
        if not exclusive:
            import platform
            if platform.system() != "Windows" and QGuiApplication.platformName() == "xcb":
                try:
                    from ui.helpers import game_is_running
                    from ui.x11_fullscreen import fullscreen_window_in_front
                    self._x11_quiet = bool(game_is_running() or fullscreen_window_in_front())
                except Exception:
                    logger.debug("could not tell whether a game is in front of the picker", exc_info=True)
        self._game_focus = 0
        if self._x11_quiet:
            flags |= Qt.WindowType.X11BypassWindowManagerHint | Qt.WindowType.WindowDoesNotAcceptFocus
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            from ui.x11_fullscreen import current_focus
            self._game_focus = current_focus()              # (who has the keyboard now: the game - given back below)
        self.setWindowFlags(flags)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen.geometry())
        self.setModal(True)

    def showEvent(self, event):
        super().showEvent(event)
        if self._exclusive:
            return                                  # taking the focus or the keyboard would take the game back to the desktop
        if self._x11_quiet:
            self.raise_()
            self.grabMouse()                        # (the pointer only: a grab of it does not move the focus)
            if self._game_focus:
                # A modal dialog is given the keyboard by Qt all the same: it goes back to the game, which is not told it lost it
                from ui.x11_fullscreen import focus_window
                for delay in (0, 120):
                    QTimer.singleShot(delay, lambda w=self._game_focus: focus_window(w))
            return
        # Take the mouse and keyboard outright. The thing being drawn over is
        # usually a game that is itself listening for both: without the grab
        # the drag lands in the game — the camera turns, the character moves —
        # and the picker never sees the events it exists to receive.
        self.activateWindow()
        self.raise_()
        self.grabMouse()
        self.grabKeyboard()

    def hideEvent(self, event):
        if not self._exclusive:
            self.releaseMouse()
            if not self._x11_quiet:
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
        if event.button() == Qt.MouseButton.RightButton:
            self._origin = None
            self._rect = QRect()
            self.reject()
            return
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


def capture_region(parent=None, screen=None, exclusive: bool = False) -> "QPixmap | None":
    """Let the player draw a box on screen and return that piece.

    Returns None when they cancel, or when nothing could be grabbed —
    a game in exclusive fullscreen may hand back a black or empty frame,
    which is a limitation of the display mode, not something we can work
    around from outside the game. That case is told apart from a cancel by
    ``last_grab_blank``. *screen* is the monitor to grab (the one under the
    pointer by default); *exclusive* makes the picker a window that leaves a
    game in exclusive fullscreen alone.
    """
    global last_grab_blank
    last_grab_blank = False
    screen = screen or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    try:
        shot = screen.grabWindow(0)
    except Exception as e:
        logger.warning(f"Screen grab failed: {e}")
        return None
    if shot.isNull():
        return None
    if looks_blank(shot):
        last_grab_blank = True
        logger.info("Screen capture: the frame came back empty (all black) - nothing to pick from")
        return None

    picker = RegionPicker(screen, shot, parent, exclusive)
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
