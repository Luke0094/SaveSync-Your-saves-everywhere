"""
SaveSync - Shared UI helpers
Small utilities used by multiple pages, dialogs and widgets.
"""
import logging
import os
import platform
import subprocess

import shiboken6 as sip
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy

logger = logging.getLogger(__name__)


class ScreenSignalMixin:
    """Screen add/remove/geometry tracking for frameless always-on-top
    widgets (overlay, blur modal).

    The subclass implements ``_on_screen_changed()`` (re-position/resize)
    and calls ``_connect_screen_changes()`` once after construction;
    ``_screen_signals_cleanup()`` (call it from cleanup/deleteLater)
    disconnects everything so no signal ever fires into a destroyed widget.
    """

    _SCREEN_SIGNALS = ("geometryChanged", "availableGeometryChanged")

    def _connect_screen_changes(self):
        app = QApplication.instance()
        if not app:
            return
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        for screen in app.screens():
            self._connect_one_screen(screen)

    def _connect_one_screen(self, screen):
        for sig in self._SCREEN_SIGNALS:
            getattr(screen, sig).connect(self._on_screen_changed)

    def _disconnect_one_screen(self, screen):
        # Broad except: a removed QScreen may be a partially-deleted C++ object
        for sig in self._SCREEN_SIGNALS:
            try:
                getattr(screen, sig).disconnect(self._on_screen_changed)
            except (RuntimeError, TypeError, Exception):
                pass

    def _on_screen_added(self, screen):
        self._connect_one_screen(screen)
        self._on_screen_changed()

    def _on_screen_removed(self, screen):
        self._disconnect_one_screen(screen)
        self._on_screen_changed()

    def _screen_signals_cleanup(self):
        app = QApplication.instance()
        if not app:
            return
        try:
            app.screenAdded.disconnect(self._on_screen_added)
        except (RuntimeError, TypeError):
            pass
        try:
            app.screenRemoved.disconnect(self._on_screen_removed)
        except (RuntimeError, TypeError):
            pass
        for screen in app.screens():
            self._disconnect_one_screen(screen)

    def _on_screen_changed(self, *_args):
        raise NotImplementedError


def safe_widget(w) -> bool:
    """True if *w* still exists as a live C++ Qt object."""
    try:
        return w is not None and sip.isValid(w)
    except Exception:
        return w is not None


def open_in_file_manager(target) -> None:
    """Open a folder (or a file's location) in the system file manager."""
    try:
        if platform.system() == "Windows":
            os.startfile(str(target))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        logger.warning(f"Could not open folder '{target}': {e}")


def force_topmost(widget) -> None:
    """Force a frameless always-on-top widget above all windows without
    stealing focus — shared by the overlay and the blur modal.

    - Windows: Win32 SetWindowPos with HWND_TOPMOST + NOACTIVATE, plus
      WS_EX_TOOLWINDOW so the widget never shows in taskbar/alt-tab.
    - Linux/macOS: Qt raise_() re-stacks the widget (X11 sends
      _NET_WM_STATE_ABOVE; on Wayland fullscreen it's a compositor no-op;
      macOS raises the NSWindow level).
    """
    if platform.system() == "Windows":
        try:
            import ctypes
            hwnd = int(widget.winId())
            user32 = ctypes.windll.user32
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_NOOWNERZORDER = 0x0200
            HWND_TOPMOST = -1
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
            extended_style = user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, extended_style | 0x00000080)  # WS_EX_TOOLWINDOW
        except Exception as e:
            logger.debug(f"force_topmost failed: {e}")
    else:
        try:
            widget.raise_()
        except Exception:
            pass


def load_pixmap_any(path: str) -> QPixmap:
    """QPixmap(path) with a PIL fallback for formats Qt has no plugin for
    (notably .avif — decoded via pillow-avif-plugin). Returns a null pixmap
    when neither decoder can read the file."""
    px = QPixmap(path)
    if not px.isNull():
        return px
    try:
        try:
            import pillow_avif  # noqa: F401 — registers the AVIF codec in PIL
        except ImportError:
            pass
        from PIL import Image as _PILImage
        pil_img = _PILImage.open(path)
        if pil_img.mode != "RGBA":
            pil_img = pil_img.convert("RGBA")
        w, h = pil_img.size
        data = pil_img.tobytes("raw", "RGBA")
        qimg = QImage(data, w, h, QImage.Format_RGBA8888)
        px = QPixmap.fromImage(qimg)
    except Exception as e:
        logger.debug(f"PIL fallback failed for '{path}': {e}")
    return px


class TopmostPinMixin:
    """Keep a dialog above a fullscreen game, without a focus war.

    A plain Qt dialog cannot surface itself over a game that keeps reclaiming
    the foreground — raise_() only re-stacks our own windows. Re-asserting
    HWND_TOPMOST on a timer does, and SWP_NOACTIVATE keeps it from stealing
    the game's focus. Unlike the overlay this deliberately does NOT set
    WS_EX_TOOLWINDOW, so the dialog stays in the taskbar and alt-tab.

    Shared by the save-confirmation panel and the sync-conflict dialog: both
    are decisions the user has to make while a game may be in front.
    """

    _TOPMOST_INTERVAL_MS = 1000

    def start_topmost_pin(self):
        """Begin re-pinning (Windows only — elsewhere the window manager
        honours WindowStaysOnTopHint on its own)."""
        import platform
        from PySide6.QtCore import QTimer
        if platform.system() != "Windows":
            return
        timer = getattr(self, "_topmost_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(self._TOPMOST_INTERVAL_MS)
            timer.timeout.connect(self.repin_topmost)
            self._topmost_timer = timer
        if not timer.isActive():
            timer.start()
        self.repin_topmost()

    def repin_topmost(self):
        try:
            import ctypes
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
            SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0010, 0x0200
            HWND_TOPMOST = -1
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        except Exception:
            pass

    def stop_topmost_pin(self):
        timer = getattr(self, "_topmost_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass


def apply_game_friendly_flags(dialog):
    """Window recipe for a dialog that may open over a running game: on top
    where the platform supports it, and shown without stealing focus."""
    import platform
    from PySide6.QtCore import Qt
    flags = (Qt.WindowType.Dialog
             | Qt.WindowType.CustomizeWindowHint
             | Qt.WindowType.WindowTitleHint
             | Qt.WindowType.WindowCloseButtonHint)
    if platform.system() == "Windows":
        flags |= Qt.WindowType.WindowStaysOnTopHint
    dialog.setWindowFlags(flags)
    dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)


class ElidedLabel(QLabel):
    """One-line label that shortens its text in the MIDDLE to fit, keeping the
    full value in the tooltip.

    Paths are the wrong shape for word wrap: they are one long token, so a
    wrapped label grows to three or four lines and — inside a list of a few
    hundred rows — both buries the rest of the row and makes the list
    unreadable. Eliding in the middle keeps the two ends that identify a path
    (the drive and the leaf) while the whole thing stays available on hover.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFullText(text)

    def setFullText(self, text: str):
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply_elide()

    def fullText(self) -> str:
        return self._full

    def _apply_elide(self):
        metrics = QFontMetrics(self.font())
        width = max(40, self.width() - 2)
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()
