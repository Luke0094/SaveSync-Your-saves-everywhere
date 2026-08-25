"""
SaveSync - Splash Screen
Shows a branded splash during application startup.
Prefers splash_animated.gif (animated) over splash.png (static).
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget, QLabel, QVBoxLayout
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient, QPen, QMovie
from PySide6.QtCore import Qt, QRectF, QSize

from core.constants import APP_NAME, APP_VERSION


def _assets_dir() -> Path:
    """Base assets directory, works both in dev and PyInstaller bundle."""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / "assets"


def _resolve_animated_gif() -> Path:
    return _assets_dir() / "splash_animated.gif"


def _resolve_static_png() -> Path:
    return _assets_dir() / "splash.png"


# ── Animated splash (QLabel + QMovie) ─────────────────────────────────────────

class _AnimatedSplash(QWidget):
    """Frameless animated-GIF splash screen.

    Provides the same close() / show() / finish() interface as QSplashScreen
    so callers need no special-casing.
    """

    def __init__(self, gif_path: str):
        # No WindowStaysOnTopHint. That flag is permanent for the life of the
        # window: the splash would sit above every other program for as long
        # as it is up, so nothing else on the machine could be used or even
        # read past it while SaveSync starts. It comes up in FRONT instead —
        # see showEvent — which is what "on top" is actually asked to mean
        # here, and anything the user clicks afterwards covers it normally.
        super().__init__(None, Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        self._movie = QMovie(gif_path)
        if not self._movie.isValid():
            raise ValueError(f"Invalid or unreadable GIF: {gif_path}")

        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setMovie(self._movie)

        # Determine size from first frame
        self._movie.jumpToFrame(0)
        first = self._movie.currentPixmap()
        sz: QSize = first.size() if not first.isNull() else QSize(480, 300)
        self.setFixedSize(sz)
        self._label.setFixedSize(sz)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._center()
        self._movie.start()

    def _center(self):
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            self.move((sg.width() - self.width()) // 2,
                      (sg.height() - self.height()) // 2)

    def showEvent(self, event):
        super().showEvent(event)
        _raise_once(self)

    # QSplashScreen-compatible API
    def finish(self, _widget=None):
        self._movie.stop()
        self.close()


# ── Fallback static pixmap (programmatic) ─────────────────────────────────────

def _ui_family() -> str:
    """The interface family, without importing ui.helpers at module load.

    The splash is drawn before the rest of the UI exists, so this stays a
    late import — a cycle here would cost the splash entirely.
    """
    try:
        from ui.helpers import ui_font_family
        return ui_font_family()
    except Exception:
        return "sans-serif"


def _create_static_pixmap() -> QPixmap:
    """Load splash.png, or draw one in-memory as last-resort fallback."""
    path = _resolve_static_png()
    if path.exists():
        pm = QPixmap(str(path))
        if not pm.isNull():
            return pm

    # Programmatic fallback — same design as generate_splash.py
    w, h = 480, 300
    pm = QPixmap(w, h)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    bg = QLinearGradient(0, 0, 0, h)
    bg.setColorAt(0.0, QColor("#16161a"))
    bg.setColorAt(1.0, QColor("#0e0e11"))
    p.fillRect(0, 0, w, h, bg)

    accent = QColor("#76b900")
    p.setPen(QPen(accent, 3))
    p.drawLine(0, 0, w, 0)

    p.setFont(QFont(_ui_family(), 28, QFont.Bold))
    p.setPen(QColor("#e8e8ea"))
    p.drawText(QRectF(0, h * 0.28, w, 50), Qt.AlignCenter, APP_NAME)

    p.setFont(QFont(_ui_family(), 11))
    p.setPen(accent)
    p.drawText(QRectF(0, h * 0.28 + 46, w, 24), Qt.AlignCenter, f"v{APP_VERSION}")

    p.setFont(QFont(_ui_family(), 10))
    p.setPen(QColor("#4a4a5a"))
    p.drawText(QRectF(0, h - 50, w, 30), Qt.AlignCenter, "Loading...")

    p.setPen(QPen(accent, 2))
    p.drawLine(w // 4, h - 12, w * 3 // 4, h - 12)
    p.end()
    return pm


def _raise_once(widget) -> None:
    """Bring a splash to the front the moment it appears — once, not forever.

    The splash used to carry WindowStaysOnTopHint, which pins it above every
    window on the machine until it closes: for the whole of startup nothing
    else could be used, because the splash was in the way of all of it. What
    is actually wanted is that it OPENS in front. That is a one-off act, so
    it happens on show and never again, and any window the user brings up
    afterwards covers it like any other.
    """
    try:
        from ui.helpers import force_foreground
        force_foreground(widget)
    except Exception:
        try:
            widget.raise_()
            widget.activateWindow()
        except Exception:
            pass


class _StaticSplash(QSplashScreen):
    """QSplashScreen that comes up in front without staying there."""

    def showEvent(self, event):
        super().showEvent(event)
        _raise_once(self)


# ── Public factory ─────────────────────────────────────────────────────────────

def create_splash():
    """Create and return the splash screen (call after QApplication init).

    Priority:
      1. splash_animated.gif  → animated _AnimatedSplash widget
      2. splash.png           → static  QSplashScreen
      3. programmatic drawing → static  QSplashScreen (last resort)
    """
    gif = _resolve_animated_gif()
    if gif.exists():
        try:
            return _AnimatedSplash(str(gif))
        except Exception:
            pass   # fall through to static

    pixmap = _create_static_pixmap()
    splash = _StaticSplash(pixmap)
    splash.setWindowFlag(Qt.FramelessWindowHint)
    return splash
