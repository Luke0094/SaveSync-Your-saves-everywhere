"""
SaveSync - Blur Modal Widget
Provides a dark blur background effect for modal dialogs and windows.
- Fullscreen dark blur overlay
- Modal behavior - blocks interaction with background
- Supports transparency and blur effects
"""
import logging
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, Signal
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtWidgets import QWidget, QApplication

logger = logging.getLogger(__name__)

_FADE_IN_MS = 200
_FADE_OUT_MS = 280


from ui.helpers import force_topmost, ScreenSignalMixin


class BlurModalWidget(QWidget, ScreenSignalMixin):
    """Fullscreen blur modal background widget."""
    
    # Signal emitted when the modal is clicked (optional - can be used to close)
    background_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_window()
        self._setup_animation()
        self._connect_screen_changes()

    def _setup_window(self):
        """Setup window properties for fullscreen blur overlay."""
        # Fullscreen frameless window
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.NoDropShadowWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.BypassWindowManagerHint  # Helps with fullscreen behavior
        )
        
        # Transparent background with blur effect
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)  # Linux support
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        
        # Set to cover entire virtual desktop (all screens)
        self._update_geometry()

    def _setup_animation(self):
        """Setup fade animations."""
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _update_geometry(self):
        """Update widget geometry to cover all screens (re-computed on every call)."""
        app = QApplication.instance()
        if not app:
            return
        # Union of all screen geometries — works for multi-monitor and resolution changes
        from PySide6.QtCore import QRect
        union = QRect()
        for screen in app.screens():
            union = union.united(screen.geometry())
        if union.isValid():
            self.setGeometry(union)

    # Screen add/remove/geometry wiring comes from ScreenSignalMixin;
    # only the reaction is blur-specific:
    def _on_screen_changed(self, *_args):
        """Handle screen geometry changes."""
        self._update_geometry()

    def cleanup(self):
        """Disconnect screen signals to prevent crashes after widget destruction."""
        self._screen_signals_cleanup()

    def deleteLater(self):
        """Ensure screen signals are disconnected before destruction."""
        self.cleanup()
        super().deleteLater()

    def show_animated(self):
        """Show the blur modal with fade-in animation — never steals game focus."""
        # Cancel any ongoing animation and disconnect stale hide connection
        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect(self.hide)
        except RuntimeError:
            pass

        # Set initial state
        self.setWindowOpacity(0.0)

        # Update geometry before showing (handles resolution changes)
        self._update_geometry()

        self.show()

        # Force topmost without activating (overlay-like, no raise_())
        self._force_topmost()

        # Fade in
        self._fade_anim.setDuration(_FADE_IN_MS)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def force_hide(self):
        """Immediately hide the blur overlay without animation.

        Use this when the game closes or the app needs guaranteed cleanup
        (hide_animated is async and may leave the overlay visible on fast exits).
        """
        try:
            self._fade_anim.stop()
            try:
                self._fade_anim.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
        except Exception:
            pass
        try:
            self.hide()
        except RuntimeError:
            pass

    def hide_animated(self):
        """Hide the blur modal with fade-out animation."""
        self._fade_anim.stop()

        # Disconnect any existing connections to avoid duplicates
        try:
            self._fade_anim.finished.disconnect(self.hide)
        except RuntimeError:
            pass

        self._fade_anim.setDuration(_FADE_OUT_MS)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        # Use a one-shot callback that checks widget validity before hiding.
        # Prevents use-after-free if widget is destroyed before animation finishes.
        try:
            import sip as _sip
            _has_sip = True
        except ImportError:
            from PySide6 import shiboken6 as _sip
            _has_sip = True
        except Exception:
            _has_sip = False

        def _on_fade_out_done():
            try:
                self._fade_anim.finished.disconnect(_on_fade_out_done)
            except RuntimeError:
                return  # animation or widget already destroyed
            # Guard against calling hide() on a deleted C++ object
            try:
                if _has_sip and not _sip.isValid(self):
                    return
                self.hide()
            except RuntimeError:
                pass
        self._fade_anim.finished.connect(_on_fade_out_done)
        self._fade_anim.start()

    def _force_topmost(self):
        """Force the widget to stay on top of all other windows."""
        force_topmost(self)   # shared with the overlay — see ui.helpers

    def paintEvent(self, event):
        """Paint the dark blur effect."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Vignette effect (single pass — no double paint)
        center_x = self.rect().center().x()
        center_y = self.rect().center().y()
        max_radius = max(self.width(), self.height())
        
        # Create radial gradient for vignette
        from PySide6.QtGui import QRadialGradient
        gradient = QRadialGradient(center_x, center_y, max_radius)
        gradient.setColorAt(0.0, QColor(0, 0, 0, 120))   # Lighter at center
        gradient.setColorAt(0.7, QColor(0, 0, 0, 160))   # Medium
        gradient.setColorAt(1.0, QColor(0, 0, 0, 200))   # Darker at edges
        
        painter.fillRect(self.rect(), QBrush(gradient))
        painter.end()

    def mousePressEvent(self, event):
        """Handle mouse press on background."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.background_clicked.emit()

    def resizeEvent(self, event):
        """Handle resize events."""
        super().resizeEvent(event)
        # Guard against infinite recursion: _update_geometry calls setGeometry
        # which can trigger another resizeEvent
        if not getattr(self, '_updating_geometry', False):
            self._updating_geometry = True
            try:
                self._update_geometry()
            finally:
                self._updating_geometry = False
