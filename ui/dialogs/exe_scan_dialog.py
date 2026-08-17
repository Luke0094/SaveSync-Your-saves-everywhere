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
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QCheckBox, QFileDialog, QProgressBar,
    QMessageBox, QSizePolicy,
)

from i18n import t
from ui.helpers import ElidedLabel, finalize_adaptive_dialog_size, scaled
from ui.styles.theme import palette
from ui.modal_helpers import question_window_modal, information_window_modal

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 8
_UI_PROGRESS_MIN_INTERVAL_S = 0.12


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
        self.setPriority(QThread.Priority.IdlePriority)

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
        self._remove_btn.setFixedSize(scaled(24, self), scaled(24, self))
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
        browse.setFixedWidth(scaled(80, self))
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
            fs = scaled(10, self)
            self._note.setStyleSheet(f"color:{palette('warning')};font-size:{fs}px;")
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
            fs = scaled(10, self)
            self._note.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")
        else:
            self._note.setFullText(t("exe_scan.path_not_found", path=path))
            fs = scaled(10, self)
            self._note.setStyleSheet(f"color:{palette('warning')};font-size:{fs}px;")

    # ── Read-out ─────────────────────────────────────────────────────────────

    def is_included(self) -> bool:
        return self._include.isChecked()

    def game_name(self) -> str:
        return self._name_edit.text().strip()

    def exe_path(self) -> str:
        return self._path_edit.text().strip()


class _StoreWorker(QThread):
    """Add confirmed scan hits to the library off the GUI thread."""
    progress = Signal(int, int, str)
    finished_ok = Signal(object)  # (added, skipped, invalid, entries, cancelled)

    def __init__(self, pending: list, parent=None):
        super().__init__(parent)
        self._pending = pending  # list[(name, exe)]
        self._stop = False
        self.setPriority(QThread.Priority.IdlePriority)

    def stop(self):
        self._stop = True

    def run(self):
        from core.library import GameEntry, get_library
        from core.machine import get_machine_id
        from core.constants import get_folder_name_for_save
        from core.concurrency import config_write_debounce_ms

        lib = get_library()
        added = skipped = invalid = 0
        entries = []
        total = len(self._pending)
        last_emit = 0.0
        # Pace writes like config/library debounce on weaker machines so a
        # 500-game commit does not stampede the disk the way a sync GUI
        # store used to.
        pace_s = max(0.0, config_write_debounce_ms() / 1000.0 / 8.0)
        lib.begin_bulk()
        try:
            for i, (name, exe) in enumerate(self._pending, 1):
                if self._stop:
                    break
                now = time.monotonic()
                if (
                    i == 1 or i == total or self._stop
                    or (now - last_emit) >= _UI_PROGRESS_MIN_INTERVAL_S
                ):
                    self.progress.emit(i, total, name or exe or "")
                    last_emit = now
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
                entries.append(entry)
                added += 1
                logger.info(f"Scan-added game: {name} ({exe})")
                if pace_s and i < total and not self._stop:
                    time.sleep(pace_s)
        finally:
            lib.end_bulk()
            # Durability: wait for library.json like a forced backup flush,
            # not only the debounced timer (which may still be pending).
            try:
                lib._save()
            except Exception:
                logger.warning("Library flush after exe-scan store failed",
                               exc_info=True)
        self.finished_ok.emit((added, skipped, invalid, entries, self._stop))


class ExeScanDialog(QDialog):
    """Folder → candidates → confirmed library entries."""

    # (game_ids) — emitted when the user asked for the web-search pass.
    search_requested = Signal(object)
    shelved = Signal()
    shelve_status = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("exe_scan.title"))
        # Same contract as Add/Edit (the URL-drop flow): window-modal while
        # open — every extra action (another scan, save, …) is blocked — but
        # ✕ shelves it into the sidebar, freeing the main window so other
        # game cards stay usable while the work finishes in the background.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._worker = None
        self._store_worker = None
        self._rows: list = []
        self.added_entries: list = []
        self._pending_hits: list = []
        self._insert_index = 0
        self._last_ui_progress_at = 0.0
        self._phase = "idle"  # idle|scanning|inserting|ready|storing
        self._cancel_op = False
        self._force_close = False
        self._wants_search = False
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        header = QLabel(t("exe_scan.title"))
        header.setObjectName("dialog_title")
        root.addWidget(header)

        desc = QLabel(t("exe_scan.description"))
        desc.setObjectName("dialog_desc")
        desc.setWordWrap(True)
        root.addWidget(desc)

        pick_row = QHBoxLayout()
        pick_row.setSpacing(6)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText(t("exe_scan.folder_placeholder"))
        self._folder_edit.returnPressed.connect(self._start_scan)
        browse = QPushButton(t("add_game.browse"))
        browse.setFixedWidth(scaled(90, self))
        browse.clicked.connect(self._browse_folder)
        self._scan_btn = QPushButton(t("exe_scan.scan"))
        self._scan_btn.setObjectName("primary_btn")
        self._scan_btn.setFixedWidth(scaled(110, self))
        self._scan_btn.clicked.connect(self._start_scan)
        pick_row.addWidget(self._folder_edit, 1)
        pick_row.addWidget(browse)
        pick_row.addWidget(self._scan_btn)
        root.addLayout(pick_row)

        self._progress = QProgressBar()
        self._progress.setFixedHeight(scaled(5, self))
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._status = QLabel()
        self._status.setObjectName("dialog_status")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self._rows_layout = QVBoxLayout(holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(8)
        self._empty_lbl = QLabel(t("exe_scan.empty"))
        self._empty_lbl.setObjectName("dialog_empty")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        self._cancel_btn = QPushButton(t("common.cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._save_btn = QPushButton(t("exe_scan.save"))
        self._save_btn.setObjectName("primary_btn")
        self._save_btn.clicked.connect(self._commit)
        bottom.addWidget(self._cancel_btn)
        bottom.addWidget(self._save_btn)
        root.addLayout(bottom)

        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=640, min_h=480, scroll=self._scroll, list_content=True)

    # ── Scanning ─────────────────────────────────────────────────────────────

    def _browse_folder(self):
        from ui.widgets.file_pickers import pick_folder
        picked = pick_folder(self, t("exe_scan.pick_folder"),
                             start_dir=self._folder_edit.text().strip())
        if picked:
            self._folder_edit.setText(picked)
            self._start_scan()

    def start_folder(self, folder_path: str):
        """Pre-fill the folder and start scanning it — used when a whole
        folder was dropped on the library page."""
        root = (folder_path or "").strip().strip('"')
        if not root:
            return
        self._folder_edit.setText(root)
        self._start_scan()

    def _start_scan(self):
        root = self._folder_edit.text().strip().strip('"')
        if not root or not Path(root).is_dir():
            self._status.setText(t("exe_scan.bad_folder"))
            return
        if self._worker is not None and self._worker.isRunning():
            return
        if self._store_worker is not None and self._store_worker.isRunning():
            return
        self._clear_rows()
        self._cancel_op = False
        self._phase = "scanning"
        self._scan_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status.setText(t("exe_scan.scanning"))
        self._worker = _ScanWorker(root, max_depth=4, parent=self)
        self._worker.step.connect(self._on_step)
        self._worker.done.connect(self._on_scan_done)
        self._worker.start()

    def _on_step(self, index: int, total: int, name: str):
        now = time.monotonic()
        if (
            index < total
            and (now - self._last_ui_progress_at) < _UI_PROGRESS_MIN_INTERVAL_S
        ):
            return
        self._last_ui_progress_at = now
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(index)
        self._status.setText(t("exe_scan.scanning_folder", current=index, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_scan_done(self, hits):
        from core.exe_scan import drop_already_in_library
        self._worker = None
        self._scan_btn.setEnabled(True)
        if self._cancel_op:
            self._phase = "idle"
            self._save_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._status.setText(t("exe_scan.cancelled"))
            return
        new, present = drop_already_in_library(hits or [])
        self._pending_hits = list(new)
        self._insert_index = 0
        if not new:
            self._phase = "idle"
            self._save_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._status.setText(t("exe_scan.none_found") if not present
                                 else t("exe_scan.all_known", count=len(present)))
            return
        self._phase = "inserting"
        self._save_btn.setEnabled(False)
        self._progress.setRange(0, max(1, len(new)))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._status.setText(t("exe_scan.found", count=len(new)))
        self._present_skipped = len(present)
        QTimer.singleShot(0, self._insert_next_chunk)

    def _insert_next_chunk(self):
        if self._cancel_op:
            self._pending_hits = []
            self._phase = "idle"
            self._save_btn.setEnabled(True)
            self._scan_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._status.setText(t("exe_scan.cancelled"))
            return
        total = len(self._pending_hits)
        if not total:
            return
        visible = self.isVisible()
        chunk = _INSERT_CHUNK if visible else max(_INSERT_CHUNK, 40)
        end = min(self._insert_index + chunk, total)
        if visible:
            for hit in self._pending_hits[self._insert_index:end]:
                self._add_row(hit)
                self._insert_index += 1
        else:
            # Shelved: advance without building widgets; catch up on unshelve.
            self._insert_index = end
        self._progress.setValue(self._insert_index)
        self._status.setText(
            f"{t('exe_scan.found', count=total)}  ({self._insert_index}/{total})"
        )
        if not visible:
            self.shelve_status.emit()
        if self._insert_index < total:
            QTimer.singleShot(0, self._insert_next_chunk)
            return
        # Catch up widgets if we finished the index while shelved / catching up.
        if self.isVisible() and len(self._rows) < total:
            while len(self._rows) < total:
                self._add_row(self._pending_hits[len(self._rows)])
            self._pending_hits = []
        elif not self.isVisible() and len(self._rows) < total:
            # Keep hits for materialize on unshelve.
            self._phase = "ready"
            self._save_btn.setEnabled(True)
            self._scan_btn.setEnabled(True)
            self._progress.setVisible(False)
            msg = t("exe_scan.found", count=total)
            skipped = getattr(self, "_present_skipped", 0)
            if skipped:
                msg += "  " + t("exe_scan.skipped_known", count=skipped)
            self._status.setText(msg)
            self.shelve_status.emit()
            return
        else:
            self._pending_hits = []
        self._phase = "ready"
        self._save_btn.setEnabled(True)
        self._scan_btn.setEnabled(True)
        self._progress.setVisible(False)
        msg = t("exe_scan.found", count=total)
        skipped = getattr(self, "_present_skipped", 0)
        if skipped:
            msg += "  " + t("exe_scan.skipped_known", count=skipped)
        self._status.setText(msg)
        if not self.isVisible():
            self.shelve_status.emit()

    def _materialize_pending_rows(self):
        hits = self._pending_hits
        if not hits or len(self._rows) >= len(hits):
            self._pending_hits = []
            return
        end = min(len(self._rows) + _INSERT_CHUNK, len(hits))
        while len(self._rows) < end:
            self._add_row(hits[len(self._rows)])
        if len(self._rows) < len(hits):
            QTimer.singleShot(0, self._materialize_pending_rows)
            return
        self._pending_hits = []

    def _add_row(self, hit):
        row = _CandidateRow(hit, parent=self)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._empty_lbl.setVisible(False)

    def _clear_rows(self):
        for row in self._rows:
            try:
                row.hide()
                row.deleteLater()
            except RuntimeError:
                pass
        self._rows = []
        self._empty_lbl.setVisible(True)

    def _live_rows(self) -> list:
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
        if self._store_worker is not None and self._store_worker.isRunning():
            return
        rows = self._live_rows()
        if not rows:
            self._status.setText(t("exe_scan.nothing_selected"))
            return

        wants_search = self._search_cb.isChecked()
        if wants_search:
            reply = question_window_modal(
                self, t("exe_scan.search_warning_title"),
                t("exe_scan.search_warning", count=len(rows)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        pending = [(row.game_name(), row.exe_path()) for row in rows]
        self._wants_search = wants_search
        self._cancel_op = False
        self._phase = "storing"
        self._scan_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._search_cb.setEnabled(False)
        self._progress.setRange(0, max(1, len(pending)))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._status.setText(t("exe_scan.storing", current=0, total=len(pending), name=""))
        self._store_worker = _StoreWorker(pending, parent=self)
        self._store_worker.progress.connect(self._on_store_step)
        self._store_worker.finished_ok.connect(self._on_store_done)
        self._store_worker.start()

    def _on_store_step(self, current: int, total: int, name: str):
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(current)
        self._status.setText(t("exe_scan.storing",
                               current=current, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_store_done(self, payload):
        added, skipped, invalid, entries, cancelled = payload
        self._store_worker = None
        self._scan_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._search_cb.setEnabled(True)
        self._progress.setVisible(False)
        if cancelled:
            self._phase = "ready"
            self._status.setText(t("exe_scan.cancelled"))
            return
        self.added_entries.extend(entries)
        parts = []
        if added:
            parts.append(t("exe_scan.result_added", count=added))
        if skipped:
            parts.append(t("exe_scan.result_skipped", count=skipped))
        if invalid:
            parts.append(t("exe_scan.result_invalid", count=invalid))
        if not added:
            self._phase = "ready"
            self._status.setText("  ".join(parts) or t("exe_scan.nothing_added"))
            return
        self._phase = "idle"
        information_window_modal(self, t("exe_scan.title"), "\n".join(parts))
        if self._wants_search and self.added_entries:
            self.search_requested.emit([e.id for e in self.added_entries])
        self._force_close = True
        self.accept()

    # ── Shelve / lifecycle ───────────────────────────────────────────────────

    def has_shelvable_work(self) -> bool:
        return self._phase in ("scanning", "inserting", "storing")

    def shelve_nav_label(self) -> str:
        return t("exe_scan.shelved_nav")

    def shelve_nav_tooltip(self) -> str:
        if self._phase == "scanning":
            return t("exe_scan.shelved_scanning")
        if self._phase == "inserting":
            return t("exe_scan.shelved_inserting")
        if self._phase == "storing":
            return t("exe_scan.shelved_storing")
        return t("exe_scan.shelved_nav")

    def _shelve(self):
        self.hide()
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.shelved.emit()

    def unshelve(self):
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.show()
        self.raise_()
        self.activateWindow()
        if self._phase == "inserting" and self._pending_hits:
            QTimer.singleShot(0, self._insert_next_chunk)
        elif self._pending_hits and len(self._rows) < len(self._pending_hits):
            QTimer.singleShot(0, self._materialize_pending_rows)

    def _on_cancel_clicked(self):
        if self.has_shelvable_work():
            self._cancel_op = True
            if self._worker is not None and self._worker.isRunning():
                self._worker.stop()
            if self._store_worker is not None and self._store_worker.isRunning():
                self._store_worker.stop()
            if self._phase == "inserting":
                self._pending_hits = []
                self._phase = "ready"
                self._save_btn.setEnabled(True)
                self._scan_btn.setEnabled(True)
                self._progress.setVisible(False)
            self._status.setText(t("exe_scan.cancelling"))
            return
        self._force_close = True
        self.reject()

    def closeEvent(self, event):
        if (not self._force_close and self.has_shelvable_work()
                and not self._cancel_op):
            event.ignore()
            self._shelve()
            return
        self._force_close = True
        self._cancel_op = True
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
        sw = self._store_worker
        if sw is not None and sw.isRunning():
            sw.stop()
            sw.wait(2000)
            self._store_worker = None
        super().closeEvent(event)
        from ui.helpers import trim_process_memory
        QTimer.singleShot(250, trim_process_memory)
