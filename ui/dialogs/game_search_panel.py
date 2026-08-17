"""
SaveSync - Progress panel for the batch web search.

A view onto GameSearchRunner and nothing more: closing it does not stop the
run, and reopening it (from the sidebar entry under Settings) replays whatever
has happened so far. That is the whole point — a fifty-title search must not
hold the app hostage.
"""
import logging
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QListWidget, QListWidgetItem, QFrame,
)

from i18n import t
from ui.helpers import finalize_adaptive_dialog_size, scaled
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

_UI_PROGRESS_MIN_INTERVAL_S = 0.12


class GameSearchPanel(QDialog):
    """Live progress for a running (or finished) batch search."""

    # The user closed a FINISHED run — they are done with it, so the sidebar
    # entry that reopens it should go away too. Minimising a running one does
    # not emit this.
    dismissed = Signal()
    # User chose Minimizza / ✕ while the run is still going.
    minimized = Signal()

    def __init__(self, runner, parent=None):
        super().__init__(parent)
        self._runner = runner
        self.setWindowTitle(t("game_search.title"))
        # Not modal on purpose: the app stays usable while this is open.
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._last_ui_progress_at = 0.0
        self._build()
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=520, min_h=360)
        self._replay()
        runner.progress.connect(self._on_progress)
        runner.log_line.connect(self._append)
        runner.finished.connect(self._on_finished)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel(t("game_search.title"))
        title.setObjectName("dialog_title")
        root.addWidget(title)

        self._hint = QLabel(t("game_search.running_hint"))
        self._hint.setObjectName("dialog_desc")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._bar = QProgressBar()
        self._bar.setFixedHeight(scaled(6, self))
        self._bar.setTextVisible(False)
        root.addWidget(self._bar)

        self._status = QLabel()
        self._status.setObjectName("dialog_status")
        root.addWidget(self._status)

        self._list = QListWidget()
        self._list.setObjectName("game_search_list")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._minimize_btn = QPushButton(t("game_search.minimize"))
        self._minimize_btn.setToolTip(t("game_search.minimize_tooltip"))
        self._minimize_btn.clicked.connect(self._minimize_to_sidebar)
        self._cancel_btn = QPushButton(t("game_search.cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._close_btn = QPushButton(t("common.close"))
        self._close_btn.clicked.connect(self._on_close_finished)
        self._close_btn.setVisible(False)
        btn_row.addWidget(self._minimize_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._close_btn)
        root.addLayout(btn_row)

    # ── State ────────────────────────────────────────────────────────────────

    def _minimize_to_sidebar(self):
        self.hide()
        self.minimized.emit()

    def _replay(self):
        """Rebuild from the runner — this panel may be a second window onto a
        search that started long before it."""
        self._list.clear()
        for line, matched in self._runner.lines:
            self._append_item(line, matched, scroll=False)
        if self._list.count():
            self._list.scrollToBottom()
        self._bar.setRange(0, max(1, self._runner.total))
        self._bar.setValue(self._runner.done)
        self._sync_status()
        if not self._runner.running:
            self._enter_finished_state(self._runner.matched, self._runner.total,
                                       self._runner.cancelled)

    def _sync_status(self):
        self._status.setText(t("game_search.progress",
                               done=self._runner.done, total=self._runner.total,
                               matched=self._runner.matched))

    def _append_item(self, line: str, matched: bool, *, scroll: bool = True):
        item = QListWidgetItem(("✓  " if matched else "—  ") + line)
        item.setForeground(
            QColor(palette("accent") if matched else palette("text_muted")))
        self._list.addItem(item)
        if scroll:
            self._list.scrollToBottom()

    def _append(self, line: str, matched: bool):
        # While minimised the runner still keeps ``lines``; rebuild on show.
        if not self.isVisible():
            return
        self._append_item(line, matched)

    def _on_progress(self, done: int, total: int, _name: str):
        if not self.isVisible():
            return
        now = time.monotonic()
        if (
            done < total
            and (now - self._last_ui_progress_at) < _UI_PROGRESS_MIN_INTERVAL_S
        ):
            return
        self._last_ui_progress_at = now
        self._bar.setRange(0, max(1, total))
        self._bar.setValue(done)
        self._sync_status()

    def showEvent(self, event):
        # Catch up the list after a minimise — runner state is authoritative.
        super().showEvent(event)
        self._replay()

    def _on_cancel(self):
        self._runner.cancel()
        self._cancel_btn.setEnabled(False)
        self._hint.setText(t("game_search.cancelling"))

    def _on_finished(self, matched: int, total: int, cancelled: bool):
        self._enter_finished_state(matched, total, cancelled)

    def _enter_finished_state(self, matched: int, total: int, cancelled: bool):
        self._bar.setRange(0, max(1, total))
        self._bar.setValue(total if not cancelled else self._runner.done)
        self._hint.setText(t("game_search.done_cancelled" if cancelled else "game_search.done",
                             matched=matched, total=total))
        self._cancel_btn.setVisible(False)
        self._minimize_btn.setVisible(False)
        self._close_btn.setVisible(True)
        self._sync_status()

    def _on_close_finished(self):
        """Done with this run — drop it, sidebar entry included."""
        self.dismissed.emit()
        self.accept()

    def closeEvent(self, event):
        """Closing a RUNNING search is minimising: it keeps going and the
        sidebar entry brings this back. Closing a finished one dismisses it."""
        if self._runner.running:
            event.ignore()
            self._minimize_to_sidebar()
            return
        self.dismissed.emit()
        super().closeEvent(event)
