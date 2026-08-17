"""
SaveSync - Shared manual-drag infrastructure for the library sidebar.

One module-level _active_drag dict + the DragProxy ghost widget, shared by
BOTH drag sources (game cards/rows and folder rows): the dict is mutated in
place, never rebound, so every importer sees the same live drag state.
"""
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget


_active_drag: dict = {}  # {"game_id": str, "proxy": QLabel, "source": QWidget, "offset": QPoint}


class DragProxy(QLabel):
    """Floating card image that follows cursor during drag.
    Hides itself when over the folder sidebar so folder rows stay visible."""

    def __init__(self, pixmap: QPixmap, window: QWidget, sidebar: QWidget = None):
        super().__init__(window)
        scaled = pixmap.scaled(
            int(pixmap.width() * 0.92), int(pixmap.height() * 0.92),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        self.setFixedSize(scaled.size())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._sidebar = sidebar
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.92)
        self.setGraphicsEffect(self._opacity)
        self.show()
        self.raise_()

    def update_for_sidebar(self, global_pos: QPoint):
        """Fade out when cursor is over the sidebar area."""
        if not self._sidebar:
            return
        sb_global = self._sidebar.mapToGlobal(QPoint(0, 0))
        over_sidebar = (sb_global.x() <= global_pos.x() <= sb_global.x() + self._sidebar.width())
        self._opacity.setOpacity(0.25 if over_sidebar else 0.92)


def cancel_active_drag():
    """Cancel any active drag in progress, destroying the proxy widget."""
    proxy = _active_drag.pop("proxy", None)
    if proxy is not None:
        try:
            proxy.hide()
            proxy.deleteLater()
        except Exception:
            pass
    src = _active_drag.pop("source", None)
    if src is not None:
        try:
            src.setGraphicsEffect(None)
        except Exception:
            pass
    _active_drag.clear()


# ── Tag Filter Panel ─────────────────────────────────────────────────────────
