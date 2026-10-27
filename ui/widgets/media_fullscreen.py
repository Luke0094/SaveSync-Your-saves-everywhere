"""SaveSync - the chat's pictures: made to fit the room a card has, and shown full screen on request.

The preview of a picture a friend sent is a thumbnail (ui.widgets.safe_image.load_preview). ``fit_pixmap``
makes it no wider than the card it is put on; ``ClickablePicture`` is the label that shows it (a click
goes full screen); ``open_picture_fullscreen`` decodes the picture again at the size of the screen - under
the same limits, only when asked - and shows it on a black window that covers it. Esc, a click or any key
ends it. The window is a child of the panel it was opened from, so it goes when that does.

``IdleChrome`` is how full screen hides what floats over the picture, the way the Add/Edit game picture
viewer does: after a while without the mouse moving every control goes, and the pointer with it; a move
brings them back, and they stay while the pointer rests on one of them.
"""
import logging

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from i18n import t

logger = logging.getLogger(__name__)


def fit_pixmap(pixmap: QPixmap, width: int) -> QPixmap:
    """*pixmap* no wider than *width* pixels, in proportion; as it is when it already fits."""
    if pixmap is None or pixmap.isNull() or width >= pixmap.width():
        return pixmap
    return pixmap.scaledToWidth(max(1, int(width)), Qt.TransformationMode.SmoothTransformation)


class IdleChrome(QObject):
    """Controls that go away while the mouse is idle on a full screen window.

    The same behaviour as the Add/Edit picture viewer's: after IDLE_MS without a move the *chrome*
    widgets are hidden and the pointer is blanked; any mouse move (or key) over the window brings
    them back and starts the wait again; and while the pointer is resting on one of the *controls*
    nothing is hidden, since somebody is using them. The chrome floats over the picture - it is never
    part of a layout - so hiding it never moves or resizes the picture.

    *window*: the full screen window; *chrome*: what is hidden; *controls*: what keeps it shown while
    the pointer is on it (buttons, a slider); *watch*: more widgets whose mouse moves count (the picture
    itself, the bar: a child that takes a move does not pass it on). *pointer_pos*: the pointer in
    global coordinates (the mouse's, unless a test says otherwise)."""
    IDLE_MS = 1500

    def __init__(self, window, chrome=(), controls=(), watch=(), pointer_pos=None):
        super().__init__(window)
        self._window = window
        self._chrome = list(chrome)
        self._controls = list(controls)
        self._pointer = pointer_pos or QCursor.pos
        self._on = False
        self._shown = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.IDLE_MS)
        self._timer.timeout.connect(self._idle)
        for w in (window, *watch):
            w.setMouseTracking(True)
            w.installEventFilter(self)

    @property
    def hidden(self) -> bool:
        return not self._shown

    def start(self):
        """Full screen is up: the wait begins."""
        self._on = True
        self._set(True)
        self._timer.start()

    def stop(self):
        """Full screen is over (or being left): everything is shown again."""
        self._on = False
        self._timer.stop()
        self._set(True)

    def wake(self):
        """Mouse activity: controls back, wait again."""
        if not self._on:
            return
        if not self._shown:
            self._set(True)
        self._timer.start()

    def _on_control(self) -> bool:
        pos = self._pointer()
        for w in self._controls:
            try:
                if w.isVisible() and w.rect().contains(w.mapFromGlobal(pos)):
                    return True
            except RuntimeError:
                pass
        return False

    def _idle(self):
        if not self._on:
            return
        if self._on_control():
            self._timer.start()             # (somebody is using them: look again later)
            return
        self._set(False)

    def _set(self, visible: bool):
        self._shown = visible
        for w in self._chrome:
            try:
                w.setVisible(visible)
            except RuntimeError:
                pass
        try:
            if visible:
                self._window.unsetCursor()
            else:
                self._window.setCursor(Qt.CursorShape.BlankCursor)
        except RuntimeError:
            pass

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.KeyPress):
            self.wake()
        return False


class ClickablePicture(QLabel):
    """A picture on a card; a click on it asks for it full screen."""
    clicked = Signal()

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t("p2p.friends_fullscreen"))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class FullscreenPicture(QWidget):
    """A picture on a black window the size of the screen."""
    closed = Signal()
    HINT_MS = 2500

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self._pm = pixmap
        self._hint = True
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.idle = IdleChrome(self)            # (no controls: only the pointer goes, as in Add/Edit)
        QTimer.singleShot(self.HINT_MS, self._hide_hint)

    def _hide_hint(self):
        try:
            self._hint = False
            self.update()
        except RuntimeError:
            pass                    # (already gone)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        if self._pm is not None and not self._pm.isNull():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            shown = self._pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width() - shown.width()) // 2, (self.height() - shown.height()) // 2, shown)
        if self._hint:
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(self.rect().adjusted(0, 0, 0, -18), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                       t("p2p.friends_fullscreen_exit"))

    def keyPressEvent(self, event):
        self.close()

    def mouseReleaseEvent(self, event):
        self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


def open_picture_fullscreen(path, anchor):
    """The picture at *path* full screen, on the screen *anchor* is on; the window, or None when
    the picture is not one this app will decode (ui.widgets.safe_image's limits apply, as to every
    preview). *anchor*: the widget it is opened from, which the window belongs to."""
    from ui.widgets.safe_image import load_preview
    host = anchor.window() if anchor is not None else None
    screen = (host.screen() if host is not None else None) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    size, dpr = screen.size(), max(1.0, float(screen.devicePixelRatio()))
    pixmap = load_preview(path, max_w=int(size.width() * dpr), max_h=int(size.height() * dpr))
    if pixmap is None:
        return None
    win = FullscreenPicture(pixmap, anchor)
    win.setScreen(screen)
    win.showFullScreen()
    win.activateWindow()
    win.setFocus()
    win.idle.start()
    return win
