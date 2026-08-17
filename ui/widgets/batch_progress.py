"""
Sidebar / panel progress for batch operations.

Same UX as Aggiunta multipla folder loading: ``{done}/{total} — {name}``
plus a thin determinate QProgressBar. Styled like other sidebar chrome
(bg_elevated + accent chunk), not a floating card.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from ui.styles.theme import ThemedMixin
from ui.helpers import scaled

# Coalesce rapid Sync/Backup Tutti ticks so the sidebar does not repaint
# hundreds of times per second and freeze the rest of the UI.
_PROGRESS_UI_MIN_INTERVAL_S = 0.12


class BatchProgressNotice(QFrame, ThemedMixin):
    """Compact progress block for the main-window sidebar."""

    # Optional: click to reopen a detail panel (batch web search).
    activated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("batch_progress_notice")
        self.setVisible(False)
        self._activatable = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(5)
        self._label = QLabel()
        self._label.setObjectName("batch_progress_label")
        self._label.setWordWrap(True)
        self._bar = QProgressBar()
        self._bar.setObjectName("batch_progress_bar")
        self._bar.setFixedHeight(scaled(4, self))
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        lay.addWidget(self._label)
        lay.addWidget(self._bar)
        # Optional cancel button — used by the shelved save-loading notice,
        # where the user must be able to call the work off from the sidebar.
        self._cancel_btn = QPushButton()
        self._cancel_btn.setObjectName("batch_progress_cancel")
        self._cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        lay.addWidget(self._cancel_btn)
        self._cancel_handler = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._kind = ""
        self._last_ui_at = 0.0
        self._pending: tuple[int, int, str] | None = None
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(int(_PROGRESS_UI_MIN_INTERVAL_S * 1000))
        self._flush_timer.timeout.connect(self._flush_pending)

    def refresh_styles(self):
        # Chrome lives in the app QSS (#batch_progress_notice); nothing to re-apply.
        pass

    def set_activatable(self, enabled: bool):
        """When True, a click emits ``activated`` (cursor becomes a hand)."""
        self._activatable = bool(enabled)
        self.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor)
            if self._activatable else QCursor(Qt.CursorShape.ArrowCursor)
        )

    def set_cancel(self, label: str, handler, min_seconds: int = 30):
        """Show a cancel button that calls *handler* when pressed.
        If min_seconds > 0, button stays hidden until min_seconds have elapsed.
        """
        self._cancel_handler = handler
        self._cancel_label = label
        self._cancel_min_seconds = max(0, int(min_seconds))
        self._cancel_btn.setText(label)
        self._cancel_btn.setEnabled(True)
        if self._cancel_min_seconds <= 0:
            self._cancel_btn.show()
        else:
            self._cancel_btn.hide()
            if hasattr(self, "_cancel_timer") and self._cancel_timer is not None:
                try:
                    self._cancel_timer.stop()
                except Exception:
                    pass
            self._cancel_timer = QTimer(self)
            self._cancel_timer.setSingleShot(True)
            self._cancel_timer.timeout.connect(self._reveal_cancel)
            self._cancel_timer.start(int(self._cancel_min_seconds * 1000))

    def _reveal_cancel(self):
        if self._cancel_handler is not None and self.isVisible():
            self._cancel_btn.show()

    def check_cancel_elapsed(self, elapsed: float):
        """Check if elapsed seconds reached min_seconds threshold to show cancel button."""
        if self._cancel_handler is not None and not self._cancel_btn.isVisible():
            if elapsed >= getattr(self, "_cancel_min_seconds", 30):
                self._cancel_btn.show()

    def _on_cancel_clicked(self):
        self._cancel_btn.setEnabled(False)
        if self._cancel_handler is not None:
            try:
                self._cancel_handler()
            except Exception:
                pass

    def cancel_done(self):
        """Re-enable the cancel button after a failed/ignored cancel."""
        self._cancel_btn.setEnabled(True)

    def hide_cancel(self):
        if hasattr(self, "_cancel_timer") and self._cancel_timer is not None:
            try:
                self._cancel_timer.stop()
            except Exception:
                pass
            self._cancel_timer = None
        self._cancel_handler = None
        self._cancel_btn.hide()
        self._cancel_btn.setEnabled(True)

    def show_indeterminate(self, text: str):
        """Busy-bar mode with no done/total — the bar pulses instead."""
        self._hide_timer.stop()
        self._flush_timer.stop()
        self._pending = None
        self._kind = ""
        self._bar.setRange(0, 0)
        self._label.setText(text)
        if self._label.objectName() != "batch_progress_label":
            self._label.setObjectName("batch_progress_label")
            self._label.style().unpolish(self._label)
            self._label.style().polish(self._label)
        self._label.setStyleSheet("")
        self.setVisible(True)

    def set_indeterminate_text(self, text: str):
        """Update the label while in busy-bar mode (seconds ticking)."""
        self._label.setText(text)

    def mousePressEvent(self, event):
        if (
            self._activatable
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.hide()
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)


    def begin(self, kind: str, total: int, name: str = ""):
        """Show at the start of a batch (*kind* is a short prefix label)."""
        self._hide_timer.stop()
        self._flush_timer.stop()
        self._pending = None
        self._kind = kind or ""
        self._cancel_btn.hide()
        self._cancel_btn.setEnabled(True)
        self._cancel_handler = None
        total = max(0, int(total))
        self._bar.setRange(0, max(1, total))
        self._bar.setValue(0)
        self._set_text(0, total, name)
        self._last_ui_at = time.monotonic()
        self.setVisible(True)

    def update_progress(self, done: int, total: int, name: str = ""):
        done = max(0, int(done))
        total = max(0, int(total))
        now = time.monotonic()
        # Always paint first/last; coalesce the middle of a large batch.
        if (
            done <= 1
            or done >= total
            or (now - self._last_ui_at) >= _PROGRESS_UI_MIN_INTERVAL_S
        ):
            self._apply_progress(done, total, name)
            return
        self._pending = (done, total, name or "")
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending(self):
        if self._pending is None:
            return
        done, total, name = self._pending
        self._pending = None
        self._apply_progress(done, total, name)

    def _apply_progress(self, done: int, total: int, name: str):
        self._bar.setRange(0, max(1, total))
        self._bar.setValue(min(done, total) if total else 0)
        self._set_text(done, total, name)
        self._last_ui_at = time.monotonic()
        self.setVisible(True)

    def finish(self, message: str = "", hide_after_ms: int = 2500):
        self._flush_timer.stop()
        self._pending = None
        if self._bar.minimum() == 0 and self._bar.maximum() == 0:
            self._bar.setRange(0, 1)
        total = self._bar.maximum()
        self._bar.setValue(total)
        if message:
            self._label.setText(message)
            self._label.setObjectName("batch_progress_label_done")
            self._label.setStyleSheet("")
            self._label.style().unpolish(self._label)
            self._label.style().polish(self._label)
        self.setVisible(True)
        if hide_after_ms > 0:
            self._hide_timer.start(hide_after_ms)
        else:
            self._hide_timer.stop()

    def stop_auto_hide(self):
        self._hide_timer.stop()

    def _set_text(self, done: int, total: int, name: str):
        prefix = self._kind or ""
        if name:
            self._label.setText(f"{prefix} {done}/{total} — {name}".strip())
        else:
            self._label.setText(f"{prefix} {done}/{total}".strip())
        # Reset weight after a finish() that used the done objectName.
        if self._label.objectName() != "batch_progress_label":
            self._label.setObjectName("batch_progress_label")
            self._label.style().unpolish(self._label)
            self._label.style().polish(self._label)
        self._label.setStyleSheet("")
