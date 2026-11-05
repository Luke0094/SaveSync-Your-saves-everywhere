"""
SaveSync - A pin over a game in exclusive fullscreen (Windows).

A pin is an ordinary always-on-top window, and showing an ordinary window over a game in exclusive fullscreen
takes the game back to the desktop (see ui.exclusive_presenter). So while such a game is in front a pin is not
shown as a window at all: it stays a widget that is "shown" but never put on the screen
(Qt.WA_DontShowOnScreen - it keeps its position, its layout, its timers, its geometry memory and everything else
the manager does with it), and what it looks like is drawn into the same kind of window the overlay uses: layered,
in the higher band, taking the mouse, never taking the focus.

The mouse that reaches that window is handed to the pin as real mouse events, to the widget under the pointer
(and to the one that took the press until the button is released, as Qt would), so the header drags the pin, the
grip resizes it, the buttons press: the pin's own code does all of it, and the picture follows what it did.

What cannot work, because it would need the keyboard and the keyboard cannot be taken without taking the game's
focus: typing into a note. A note can be read, moved, resized and closed over an exclusive game; to write in it,
leave the game's fullscreen first.
"""
import logging
import platform

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

_REFRESH_MS = 16


def physical_origin_for_screen(screen):
    """(left, top) of *screen* in physical pixels. Matched to the monitors Windows reports by its GDI device
    name when Qt reports that, otherwise by physical size - with the order on screen breaking a tie between
    identical monitors. None when it cannot be told."""
    from ui.exclusive_presenter import monitors_physical
    mons = monitors_physical()
    if not mons:
        return None
    for m in mons:
        if m[4] == screen.name():
            return m[0], m[1]

    def phys_size(s):
        g = s.geometry()
        d = s.devicePixelRatio()
        return round(g.width() * d), round(g.height() * d)

    size = phys_size(screen)
    same = sorted((m for m in mons if (m[2] - m[0], m[3] - m[1]) == size), key=lambda m: (m[0], m[1]))
    if len(same) == 1:
        return same[0][0], same[0][1]
    if same:
        peers = sorted((s for s in QGuiApplication.screens() if phys_size(s) == size),
                       key=lambda s: (s.geometry().x(), s.geometry().y()))
        i = peers.index(screen) if screen in peers else 0
        return same[min(i, len(same) - 1)][0], same[min(i, len(same) - 1)][1]
    return None


def physical_to_global(px: int, py: int):
    """A point in physical screen pixels as Qt's global (device-independent) position, or None."""
    for screen in QGuiApplication.screens():
        origin = physical_origin_for_screen(screen)
        if origin is None:
            continue
        g = screen.geometry()
        d = screen.devicePixelRatio()
        if origin[0] <= px < origin[0] + g.width() * d and origin[1] <= py < origin[1] + g.height() * d:
            return QPointF(g.x() + (px - origin[0]) / d, g.y() + (py - origin[1]) / d)
    return None


class PinMirror:
    """One pin, mirrored into a window over an exclusive-fullscreen game. start() turns the pin into a
    widget that is not put on the screen and draws it; stop() gives the pin its own window back."""

    def __init__(self, pin):
        self._pin = pin
        self._presenter = None
        self._grab = None                    # the widget that took the press, until the button is released
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

    @property
    def active(self) -> bool:
        return self._presenter is not None

    def start(self) -> bool:
        if platform.system() != "Windows" or self.active:
            return self.active
        from ui.exclusive_presenter import ExclusivePresenter
        pin = self._pin
        was_visible = pin.isVisible()
        pin.hide()
        pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        pin._mirrored = True
        self._presenter = ExclusivePresenter(click_through=False, on_mouse=self._on_mouse_raw)
        if was_visible:
            pin.show()
        if not self.refresh():
            self.stop()
            return False
        return True

    def stop(self) -> None:
        if self._presenter is None:
            return
        self._timer.stop()
        presenter, self._presenter = self._presenter, None
        presenter.destroy()
        pin = self._pin
        try:
            was_visible = pin.isVisible()
            pin.hide()
            pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            pin._mirrored = False
            if was_visible and not getattr(pin, "_closing", False):
                pin.show()
                pin.raise_()
                pin.assert_topmost()
        except RuntimeError:
            pass                             # the pin was deleted

    # ── drawing ──────────────────────────────────────────────────────────────

    def refresh(self) -> bool:
        """Draw the pin as it is now, where it is now."""
        if self._presenter is None:
            return False
        pin = self._pin
        try:
            if not pin.isVisible():
                self._presenter.hide()
                return True
            screen = QGuiApplication.screenAt(pin.geometry().center()) or QGuiApplication.primaryScreen()
            origin = physical_origin_for_screen(screen)
            if origin is None:
                return False
            d = screen.devicePixelRatio()
            g = screen.geometry()
            image = pin.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            w, h = image.width(), image.height()
            x = origin[0] + round((pin.x() - g.x()) * d)
            y = origin[1] + round((pin.y() - g.y()) * d)
            return self._presenter.present(w, h, bytes(image.constBits())[:w * h * 4], x, y)
        except RuntimeError:
            return False

    def _later(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    # ── the mouse ────────────────────────────────────────────────────────────

    def _on_mouse_raw(self, kind: str, x: int, y: int) -> None:
        """From the window procedure: handed on at once, so nothing slow happens there."""
        QTimer.singleShot(0, lambda: self._on_mouse(kind, x, y))

    def _scale(self) -> float:
        screen = QGuiApplication.screenAt(self._pin.geometry().center()) or QGuiApplication.primaryScreen()
        return screen.devicePixelRatio() or 1.0

    def _global_now(self, local: QPointF) -> QPointF:
        """Where the pointer is in Qt's global units. From the pointer itself, NOT from the pin: a drag moves the
        pin, and a position worked out from the pin would follow it and make the drag lag behind the hand."""
        from ui.exclusive_presenter import cursor_position
        pos = cursor_position()
        g = physical_to_global(*pos) if pos else None
        return g if g is not None else QPointF(self._pin.mapToGlobal(local.toPoint()))

    def _send(self, kind, target, local_in_pin: QPoint, button, buttons) -> bool:
        local = target.mapFrom(self._pin, local_in_pin) if target is not self._pin else local_in_pin
        ev = QMouseEvent(kind, QPointF(local), self._global_now(QPointF(local_in_pin)), button, buttons,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, ev)
        return ev.isAccepted()

    def _on_mouse(self, kind: str, x: int, y: int) -> None:
        pin = self._pin
        if self._presenter is None:
            return
        try:
            d = self._scale()
            local = QPoint(round(x / d), round(y / d))
            left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
            if kind == "down":
                target = pin.childAt(local) or pin
                while True:                              # as Qt would: up to a widget that takes the press
                    if self._send(QEvent.Type.MouseButtonPress, target, local, left, left) or target is pin:
                        break
                    target = target.parentWidget() or pin
                self._grab = target
            elif kind == "move":
                target = self._grab or pin.childAt(local) or pin
                self._send(QEvent.Type.MouseMove, target, local, none, left if self._grab else none)
            elif kind == "up":
                target, self._grab = self._grab or pin.childAt(local) or pin, None
                self._send(QEvent.Type.MouseButtonRelease, target, local, left, none)
            elif kind == "leave":
                if self._grab is None:
                    QApplication.sendEvent(pin, QEvent(QEvent.Type.Leave))
        except RuntimeError:
            return                                       # the pin was closed by the press
        self._later()
