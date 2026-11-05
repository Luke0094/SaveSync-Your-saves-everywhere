"""
SaveSync - The app window opened over a game in exclusive fullscreen (Windows).

An ordinary window takes a game out of exclusive fullscreen, and so does a window with a native frame even when it is
layered and in the higher band (measured over a Direct3D 11 title). A FRAMELESS, translucent, non-activating window
in that band does not (ui.win_band_hook), mouse included. So while a game in exclusive fullscreen is in front, "Open
App" shows the application's own main window in that form, with the real dimmed backdrop behind it - and puts both
back the way they were when it is closed.

What is different while it is up: the window has a title strip of its own instead of the system's (its title, a close
button, the place to drag it by), and it cannot take the keyboard focus - a text field that is clicked gets the keyboard
another way (ui.exclusive_keys). Whatever the application opens meanwhile - dialogs, menus, lists - is made for the same
situation (ui.exclusive_session).
"""
import logging

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QStyle, QStyleOption

from ui import win_band_hook

logger = logging.getLogger(__name__)


class _PaintOwnBackground(QObject):
    """A translucent window only takes the mouse where it has painted something, and Qt does not paint a stylesheet
    background on a translucent top-level window by itself: anywhere no child draws (margins, a status bar's gaps) would
    be a hole the click falls through to the game. This draws the window's own background before it paints."""

    def __init__(self, win, fill: bool = False):
        """*fill*: the window is painted opaque first. A dialog made for the game is translucent only so that it can be layered; where
        nothing paints (the picker's margins and gaps - 11 % of it, in the system's report of a real game) the game would show through."""
        super().__init__(win)
        self._win = win
        self._fill = fill

    def eventFilter(self, obj, event):
        # (No test that *obj* is the window: this filter is installed on that one window and nowhere else, and a dialog adopted while Qt is
        # still constructing it is known to the filter by a temporary Python object that is not the one the events later name - the
        # picker was never painted, and its fill and title strip never followed its size.)
        # (And the window is *obj*, the object the event names - the reference kept from the constructor may be that temporary object, which
        # Qt has deleted by then: "Internal C++ object already deleted".)
        if event.type() == QEvent.Type.Paint and hasattr(obj, "style"):
            win = obj
            opt = QStyleOption()
            opt.initFrom(win)
            p = QPainter(win)
            if self._fill:
                try:
                    from PySide6.QtGui import QColor
                    from ui.styles.theme import palette
                    p.fillRect(win.rect(), QColor(palette("bg")))
                except Exception:
                    p.fillRect(win.rect(), win.palette().window())
            win.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, opt, p, win)
            p.end()
        return False


def transparent_share(win, step: int = 4):
    """(sampled pixels with no alpha at all, sampled pixels) of what the window paints: the spots a click goes through."""
    image = win.grab().toImage()
    total = holes = 0
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            total += 1
            if (image.pixel(x, y) >> 24) & 0xFF == 0:
                holes += 1
    return holes, total


def screen_of_game(game_hwnd: int):
    """(the Qt screen the game's monitor is, or the primary one)."""
    try:
        from ui.exclusive_presenter import monitor_rect
        from ui.pin_mirror import physical_origin_for_screen
        rect = monitor_rect(game_hwnd)
        if rect is not None:
            for s in QGuiApplication.screens():
                if physical_origin_for_screen(s) == (rect[0], rect[1]):
                    return s
    except Exception:
        logger.debug("exclusive app: could not find the game's screen", exc_info=True)
    return QGuiApplication.primaryScreen()


def show_backdrop(blur, game_hwnd: int) -> bool:
    """The dimmed backdrop behind a dialog the overlay opened over a game in exclusive fullscreen: the same one Open App
    uses, in the higher band and on the game's monitor. False when it could not be done (the caller shows it as before)."""
    if blur is None or not win_band_hook.installed():
        return False
    try:
        win_band_hook.ensure_band(blur)
        blur.restrict_to(screen_of_game(game_hwnd).geometry())
        with win_band_hook.band_windows():
            blur.show_animated()
        return True
    except Exception:
        logger.debug("exclusive app: could not show the backdrop", exc_info=True)
        return False


def hide_backdrop(blur) -> None:
    """Take the backdrop away and give back its ordinary window: one made in band 16 stays above every ordinary window,
    the app's included, for good."""
    if blur is None:
        return
    try:
        blur.restrict_to(None)
        blur.force_hide()
        blur.destroy()
    except Exception:
        logger.debug("exclusive app: could not take the backdrop away", exc_info=True)


def enter(win, blur, game_hwnd: int, on_close=None) -> bool:
    """Show *win* (the main window) frameless and translucent in band 16, over *blur* (the dimmed backdrop), on the
    monitor of the game. Returns the state to give back to leave(), or None when it could not be done (the caller then
    does what it did before). *on_close* is what the title strip's close button does."""
    if not win_band_hook.installed():
        return None
    try:
        from ui import exclusive_session
        screen = screen_of_game(game_hwnd)
        geo = screen.geometry()
        state = {"flags": win.windowFlags(), "translucent": win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),
                 "filter": None, "strip": None,
                 "styled": win.testAttribute(Qt.WidgetAttribute.WA_StyledBackground),
                 "size": win.size()}
        if blur is not None:
            win_band_hook.ensure_band(blur)
            blur.restrict_to(geo)
            with win_band_hook.band_windows():
                blur.show_animated()
        win.hide()
        # The main window has been shown before (at start-up, from the tray): its native window exists, in the ordinary band, and
        # Qt would keep it - changing the flags only changes its style - and show it as it is: an ordinary window over the game.
        # Dropped, it is made again by the show below, inside band mode.
        win.destroy()
        exclusive_session.get_session().begin("app", lambda: win.isVisible())
        with win_band_hook.band_windows():
            win.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                               | Qt.WindowType.WindowDoesNotAcceptFocus)
            win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            win.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            win.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            w = min(win.width(), int(geo.width() * 0.9))
            h = min(win.height(), int(geo.height() * 0.9))
            win.resize(w, h)
            win.move(geo.center().x() - w // 2, geo.center().y() - h // 2)
            state["filter"] = _PaintOwnBackground(win)
            win.installEventFilter(state["filter"])
            state["strip"] = exclusive_session.add_title_strip(win, win.windowTitle() or "SaveSync",
                                                               on_close or (lambda: None))
            win.show()
        try:
            logger.debug("Open App: the app window is in band %s (%s is the one that leaves the game alone)",
                         win_band_hook.window_band(int(win.winId())), win_band_hook.BAND_SYSTEM_TOOLS)
        except Exception:
            pass

        def _report():
            try:
                holes, total = transparent_share(win)
                logger.log(logging.INFO if holes else logging.DEBUG,
                           "Open App over exclusive fullscreen: %d of %d sampled pixels of the window are transparent "
                           "(a click on one of them goes through to the game)", holes, total)
            except RuntimeError:
                pass

        QTimer.singleShot(500, _report)
        return state
    except Exception:
        logger.debug("exclusive app: could not show the app window", exc_info=True)
        return None


def leave(win, blur, state) -> None:
    """Give the main window and the backdrop back as they were."""
    try:
        from ui import exclusive_session
        win.hide()
        if state and state.get("strip") is not None:
            exclusive_session.remove_title_strip(win, state["strip"])
        if state and state.get("filter") is not None:
            win.removeEventFilter(state["filter"])
        # Qt keeps the native window when only the flags change, and this one was made in band 16: drop it, so the
        # next show makes an ordinary window (a band-16 window that is not layered takes a game out of exclusive mode).
        win.destroy()
        if state:
            win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, bool(state.get("translucent")))
            win.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, bool(state.get("styled")))
            win.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            win.setWindowFlags(state["flags"])                   # outside band mode: an ordinary window again
            win.resize(state["size"])
        if blur is not None:
            blur.restrict_to(None)
            blur.force_hide()
            blur.destroy()      # made in band 16: above every ordinary window, the app's included - the next one is ordinary
        exclusive_session.get_session().end("app")
    except Exception:
        logger.debug("exclusive app: could not give the app window back", exc_info=True)
