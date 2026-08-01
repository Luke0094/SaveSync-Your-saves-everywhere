"""
SaveSync - Scan a folder for games and confirm what to add.

Point at a games directory; every top-level folder that holds an executable
becomes one confirmable row — name and path both editable, removable — and
the rows the user keeps become library entries.

Nothing is added without confirmation, and the optional web-search pass that
can follow is opt-in, warned about, cancellable, and runs outside this dialog
(see ui/game_search_runner.py) so closing this window never kills it.
"""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QCheckBox, QFileDialog, QProgressBar,
    QMessageBox, QSizePolicy,
)

from i18n import t
from ui.helpers import ElidedLabel
from ui.styles.theme import palette
from ui.modal_helpers import question_window_modal, information_window_modal

logger = logging.getLogger(__name__)


def _path_line(text: str = "") -> "ElidedLabel":
    """One-line, middle-elided label for the path lines of a row.

    These rows are listed by the hundred, so a wrapped path — one long token
    over three or four lines — buries the row and makes the list unusable.
    The full value stays in the tooltip.
    """
    return ElidedLabel(text)


class _ScanWorker(QThread):
    """Runs the folder walk off the GUI thread."""
    step = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, root: str, max_depth: int, parent=None):
        super().__init__(parent)
        self._root = root
        self._max_depth = max_depth
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from core.exe_scan import scan_folder_for_games
        try:
            hits = scan_folder_for_games(
                self._root, max_depth=self._max_depth,
                cancel=lambda: self._stop,
                progress=lambda i, n, name: self.step.emit(i, n, name),
            )
        except Exception as e:
            logger.warning(f"Scan failed for {self._root}: {e}")
            hits = []
        self.done.emit(hits)


class _CandidateRow(QFrame):
    """One found game: include, name, executable path, remove."""

    def __init__(self, hit, parent=None):
        super().__init__(parent)
        self._hit = hit
        self._build()

    def _build(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._include = QCheckBox()
        self._include.setChecked(True)
        self._include.setToolTip(t("exe_scan.include_tooltip"))
        self._name_edit = QLineEdit(self._hit.name)
        self._name_edit.setPlaceholderText(t("exe_scan.name_placeholder"))
        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedSize(24, 24)
        self._remove_btn.setToolTip(t("exe_scan.remove_tooltip"))
        self._remove_btn.clicked.connect(self._remove_self)
        top.addWidget(self._include)
        top.addWidget(self._name_edit, 1)
        top.addWidget(self._remove_btn)
        outer.addLayout(top)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self._path_edit = QLineEdit(self._hit.exe_path)
        self._path_edit.setPlaceholderText(t("exe_scan.path_placeholder"))
        self._path_edit.textChanged.connect(self._revalidate)
        browse = QPushButton(t("add_game.browse"))
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)
        outer.addLayout(path_row)

        self._note = _path_line()
        outer.addWidget(self._note)

        self.apply_theme()
        self._revalidate()

    def apply_theme(self):
        self.setStyleSheet(
            f"QFrame{{background:{palette('bg_card')};border:1px solid {palette('border')};"
            f"border-radius:6px;}}"
        )
        self._remove_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{palette('text_muted')};border:none;}}"
            f"QPushButton:hover{{color:{palette('error')};}}"
        )
        self._revalidate()

    def _remove_self(self):
        # Hiding takes the row out of the layout just as well, and does not
        # leave it standing as a window of its own in the meantime.
        self.hide()
        self.deleteLater()

    def _browse(self):
        # Same shortcut-aware picker as Add Game: a folder shortcut navigates
        # instead of closing the dialog.
        from ui.widgets.file_pickers import pick_executable
        start = str(Path(self._path_edit.text()).parent) if self._path_edit.text() else ""
        picked = pick_executable(self, t("add_game.select_executable"), start_dir=start)
        if picked:
            self._path_edit.setText(picked)

    def _revalidate(self):
        path = self._path_edit.text().strip()
        if not path:
            self._note.setFullText(t("exe_scan.path_missing"))
            self._note.setStyleSheet(f"color:{palette('warning')};font-size:10px;")
            return
        exists = False
        try:
            exists = Path(path).exists()
        except OSError:
            exists = False
        if exists:
            depth = getattr(self._hit, "depth", 0)
            self._note.setFullText(t("exe_scan.found_at", path=path) if depth == 0
                               else t("exe_scan.found_deep", path=path, depth=depth))
            self._note.setStyleSheet(f"color:{palette('accent')};font-size:10px;")
        else:
            self._note.setFullText(t("exe_scan.path_not_found", path=path))
            self._note.setStyleSheet(f"color:{palette('warning')};font-size:10px;")

    # ── Read-out ─────────────────────────────────────────────────────────────

    def is_included(self) -> bool:
        return self._include.isChecked()

    def game_name(self) -> str:
        return self._name_edit.text().strip()

    def exe_path(self) -> str:
        return self._path_edit.text().strip()


class ExeScanDialog(QDialog):
    """Folder → candidates → confirmed library entries."""

    # (game_ids) — emitted when the user asked for the web-search pass.
    search_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("exe_scan.title"))
        self.setMinimumWidth(680)
        self.setMinimumHeight(520)
        self._worker = None
        self._rows: list = []
        self.added_entries: list = []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        header = QLabel(t("exe_scan.title"))
        header.setStyleSheet(f"color:{palette('text')};font-size:16px;font-weight:600;")
        root.addWidget(header)

        desc = QLabel(t("exe_scan.description"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{palette('text_secondary')};font-size:11px;")
        root.addWidget(desc)

        pick_row = QHBoxLayout()
        pick_row.setSpacing(6)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText(t("exe_scan.folder_placeholder"))
        self._folder_edit.returnPressed.connect(self._start_scan)
        browse = QPushButton(t("add_game.browse"))
        browse.setFixedWidth(90)
        browse.clicked.connect(self._browse_folder)
        self._scan_btn = QPushButton(t("exe_scan.scan"))
        self._scan_btn.setObjectName("primary_btn")
        self._scan_btn.setFixedWidth(110)
        self._scan_btn.clicked.connect(self._start_scan)
        pick_row.addWidget(self._folder_edit, 1)
        pick_row.addWidget(browse)
        pick_row.addWidget(self._scan_btn)
        root.addLayout(pick_row)

        self._progress = QProgressBar()
        self._progress.setFixedHeight(5)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        root.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self._rows_layout = QVBoxLayout(holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(8)
        self._empty_lbl = QLabel(t("exe_scan.empty"))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:12px;padding:24px;")
        self._rows_layout.addWidget(self._empty_lbl)
        self._rows_layout.addStretch()
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll, 1)

        # ── Save row, with the web-search opt-in on the same line ────────────
        bottom = QHBoxLayout()
        self._search_cb = QCheckBox(t("exe_scan.then_search"))
        self._search_cb.setToolTip(t("exe_scan.then_search_tooltip"))
        bottom.addWidget(self._search_cb)
        bottom.addStretch()
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        self._save_btn = QPushButton(t("exe_scan.save"))
        self._save_btn.setObjectName("primary_btn")
        self._save_btn.setStyleSheet(
            f"QPushButton{{background:{palette('accent')};color:{palette('accent_text')};"
            f"border:none;border-radius:6px;padding:8px 16px;font-weight:600;}}"
            f"QPushButton:hover{{background:{palette('accent_hover')};}}"
        )
        self._save_btn.clicked.connect(self._commit)
        bottom.addWidget(cancel)
        bottom.addWidget(self._save_btn)
        root.addLayout(bottom)

        self.setStyleSheet(f"QDialog{{background:{palette('bg_card')};}}")

    # ── Scanning ─────────────────────────────────────────────────────────────

    def _browse_folder(self):
        from ui.widgets.file_pickers import pick_folder
        picked = pick_folder(self, t("exe_scan.pick_folder"),
                             start_dir=self._folder_edit.text().strip())
        if picked:
            self._folder_edit.setText(picked)
            self._start_scan()

    def _start_scan(self):
        root = self._folder_edit.text().strip().strip('"')
        if not root or not Path(root).is_dir():
            self._status.setText(t("exe_scan.bad_folder"))
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._clear_rows()
        self._scan_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status.setText(t("exe_scan.scanning"))
        self._worker = _ScanWorker(root, max_depth=4, parent=self)
        self._worker.step.connect(self._on_step)
        self._worker.done.connect(self._on_scan_done)
        self._worker.start()

    def _on_step(self, index: int, total: int, name: str):
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(index)
        self._status.setText(t("exe_scan.scanning_folder", current=index, total=total, name=name))

    def _on_scan_done(self, hits):
        from core.exe_scan import drop_already_in_library
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        new, present = drop_already_in_library(hits or [])
        for hit in new:
            self._add_row(hit)
        if not new:
            self._status.setText(t("exe_scan.none_found") if not present
                                 else t("exe_scan.all_known", count=len(present)))
        else:
            msg = t("exe_scan.found", count=len(new))
            if present:
                msg += "  " + t("exe_scan.skipped_known", count=len(present))
            self._status.setText(msg)

    def _add_row(self, hit):
        row = _CandidateRow(hit, parent=self)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._empty_lbl.setVisible(False)

    def _clear_rows(self):
        for row in self._rows:
            try:
                # Hidden, not detached: a widget with no parent is a window of
                # its own until deleteLater comes round, and one that lives
                # even for an instant can be drawn on screen.
                row.hide()
                row.deleteLater()
            except RuntimeError:
                pass
        self._rows = []
        self._empty_lbl.setVisible(True)

    def _live_rows(self) -> list:
        """Rows still on screen. A removed row hides itself and waits to be
        deleted, so being hidden is what marks it gone — not being detached,
        which would leave it standing as a window of its own in the meantime.
        isHidden asks whether THIS row was hidden, not whether the dialog it
        sits in happens to be, so it holds before the dialog is shown too.
        """
        alive = []
        for row in self._rows:
            try:
                if not row.isHidden() and row.is_included():
                    alive.append(row)
            except RuntimeError:
                continue
        return alive

    # ── Commit ───────────────────────────────────────────────────────────────

    def _commit(self):
        rows = self._live_rows()
        if not rows:
            self._status.setText(t("exe_scan.nothing_selected"))
            return

        wants_search = self._search_cb.isChecked()
        if wants_search:
            # Explicit, before anything is written: this pass is slow and its
            # results are auto-accepted, so the user has to agree to both.
            reply = question_window_modal(
                self, t("exe_scan.search_warning_title"),
                t("exe_scan.search_warning", count=len(rows)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        added, skipped, invalid = self._store(rows)
        parts = []
        if added:
            parts.append(t("exe_scan.result_added", count=added))
        if skipped:
            parts.append(t("exe_scan.result_skipped", count=skipped))
        if invalid:
            parts.append(t("exe_scan.result_invalid", count=invalid))
        if not added:
            self._status.setText("  ".join(parts) or t("exe_scan.nothing_added"))
            return

        information_window_modal(self, t("exe_scan.title"), "\n".join(parts))
        if wants_search and self.added_entries:
            self.search_requested.emit([e.id for e in self.added_entries])
        self.accept()

    def _store(self, rows: list) -> tuple:
        from core.library import GameEntry, get_library
        from core.machine import get_machine_id
        from core.constants import get_folder_name_for_save

        lib = get_library()
        added = skipped = invalid = 0
        for row in rows:
            exe = row.exe_path()
            name = row.game_name()
            if not exe or not name:
                invalid += 1
                continue
            try:
                if not Path(exe).exists():
                    invalid += 1
                    continue
            except OSError:
                invalid += 1
                continue
            if lib.get_by_exe(exe) is not None:
                skipped += 1
                continue
            entry = GameEntry(
                name=name,
                exe_path=exe,
                save_paths=[],
                # Scanning finds the game, never its saves: leave it to the
                # normal detection/confirmation flow rather than inventing any.
                save_paths_confirmed=False,
                requires_confirmation=True,
                auto_added=False,
                machine_id=get_machine_id(),
                computed_folder_name=get_folder_name_for_save(name, exe, ""),
            )
            entry.record_name(name)
            entry.record_exe_hints(exe)
            entry.computed_folder_name = lib.unique_folder_name(
                entry.computed_folder_name, entry.id)
            lib.add_game(entry)
            self.added_entries.append(entry)
            added += 1
            logger.info(f"Scan-added game: {name} ({exe})")
        return added, skipped, invalid

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        """Leave nothing pointing at a dialog that is going away.

        The walk checks its cancel flag between folders, so a cold-cache pass
        over a large directory can still be seconds from returning. Rather
        than block the close on it, the signals are disconnected first: the
        thread then finishes into nothing instead of delivering results to a
        destroyed widget.
        """
        worker = self._worker
        if worker is not None and worker.isRunning():
            for signal in (worker.step, worker.done):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            worker.stop()
            if not worker.wait(2000):
                logger.info("Scan worker still running at close — detached")
            self._worker = None
        super().closeEvent(event)
