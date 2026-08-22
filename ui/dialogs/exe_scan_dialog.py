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

from PySide6.QtCore import Qt, QSize, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QCheckBox, QFileDialog, QProgressBar,
    QMessageBox, QSizePolicy,
)

from i18n import t
from ui.helpers import (ElidedLabel, center_dialog,
                        finalize_adaptive_dialog_size, scaled)
from ui.widgets.windowed_list import WindowedListMixin
from ui.styles.theme import palette
from ui.modal_helpers import question_window_modal, information_window_modal

logger = logging.getLogger(__name__)

# Rows per event-loop turn is the wrong unit and a fixed number is the wrong
# source for it: what a row COSTS is a property of the machine. Both of these
# come from core.concurrency, derived from the same CPU/RAM tier as the
# library's chunk size, the poll rates and the sweep intervals — so a capable
# PC stops paying for round trips it does not need and a weak one keeps the
# gap between two chances to press ✕ where it was. Read per pass: the tier is
# cached there and moves with the machine's free RAM.
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

    def __init__(self, hit, parent=None, index: int = -1):
        super().__init__(parent)
        self._hit = hit
        # Which entry of the dialog's master list this row is showing. Only
        # the rows on screen exist, so a row is a view of that entry and
        # every edit has to be written back — see
        # ExeScanDialog._sync_row_to_entry.
        self._entry_index = index
        self._dialog = parent
        self._build()

    def _write_back(self, *_a) -> None:
        if self._entry_index < 0:
            return
        sync = getattr(self._dialog, "_sync_row_to_entry", None)
        if callable(sync):
            sync(self)

    def _build(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._name_edit = QLineEdit(self._hit.name)
        self._name_edit.setPlaceholderText(t("exe_scan.name_placeholder"))
        self._name_edit.textEdited.connect(self._write_back)
        # One verb. A tick-box beside a remove button asked "is this one
        # going in?" twice, and left the rows nobody wanted sitting in a list
        # that is already long. The row is in the batch because it is in the
        # list; the bin takes it out.
        self._remove_btn = QPushButton()
        _bin = scaled(24, self)
        self._remove_btn.setFixedSize(_bin, _bin)
        # A rendered icon, not button text. As TEXT the glyph is at the mercy
        # of the theme's button padding — in a 24px box there is no room left
        # to draw it, so the control came out empty while still taking
        # clicks. See helpers.emoji_icon.
        from ui.helpers import emoji_icon as _emoji_icon
        self._remove_btn.setIcon(_emoji_icon("🗑", int(_bin * 0.68),
                                             self.devicePixelRatioF()))
        self._remove_btn.setIconSize(QSize(int(_bin * 0.68), int(_bin * 0.68)))
        # See the same button in the manual-path dialog: the theme's default
        # padding leaves a 24px box with no room to draw the glyph.
        self._remove_btn.setToolTip(t("exe_scan.remove_tooltip"))
        self._remove_btn.clicked.connect(self._remove_self)
        # First in the row, in the column the tick-box used to hold. The
        # eye already goes there to decide about a row, and a bin at the far
        # right of a long line reads as a scrollbar ornament rather than the
        # one control the row has.
        top.addWidget(self._remove_btn)
        top.addWidget(self._name_edit, 1)
        outer.addLayout(top)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self._path_edit = QLineEdit(self._hit.exe_path)
        self._path_edit.setPlaceholderText(t("exe_scan.path_placeholder"))
        self._path_edit.textChanged.connect(self._revalidate)
        self._path_edit.textEdited.connect(self._write_back)
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
        # Recorded on the entry before the widget goes: the row is only a
        # view, and scrolling back would otherwise bring it straight back.
        if self._entry_index >= 0:
            drop = getattr(self._dialog, "_mark_entry_removed", None)
            if callable(drop):
                drop(self._entry_index)
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
        """Every row still on screen is in the batch — the bin is what removes
        one. Kept as a method because callers ask the question by name."""
        return True

    def game_name(self) -> str:
        return self._name_edit.text().strip()

    def exe_path(self) -> str:
        return self._path_edit.text().strip()


class _StoreWorker(QThread):
    """Add confirmed scan hits to the library off the GUI thread."""
    progress = Signal(int, int, str)
    # (added, skipped, invalid, entries, cancelled). Read by INDEX at the
    # other end, never unpacked into names: the sibling worker's tuple has
    # grown twice, and the time a handler unpacked a fixed count out of one
    # the whole run finished, wrote everything, and then raised before the
    # summary — which read as "it just hangs".
    finished_ok = Signal(object)

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
                # The executable is the hint, so a title that collides with
                # one already in the library gets the SAME distinguishing tag
                # every time this game is scanned in, rather than a fresh one
                # per run — see core.constants.disambiguate_name.
                entry.computed_folder_name = lib.unique_folder_name(
                    entry.computed_folder_name, entry.id, hint=exe)
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


class ExeScanDialog(WindowedListMixin, QDialog):
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
        # Every scan result, whether or not a widget is showing it — see
        # _build_list. The rows are a window onto this.
        self._entries: list = []
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

        # Same reasoning as the manual-path dialog: this measures with the
        # list empty, so the floor is what the window actually opens at, and
        # it has to carry a useful number of rows rather than one or two.
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=720, min_h=660, scroll=self._scroll, list_content=True)
        # Centred once the size is settled — a panel this large opening
        # in a corner, or with its title bar off the top of the screen,
        # is what Qt does when nothing tells it otherwise.
        center_dialog(self)

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
        self._step = (index, total, name)
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
        self._present_skipped = len(present)
        self._status.setText(self._found_message(len(new)))
        self._progress.setVisible(False)
        self._save_btn.setEnabled(False)
        QTimer.singleShot(0, self._build_list)

    def _found_message(self, shown: int) -> str:
        """What the scan found, counted the way a person counts folders.

        The number people compare against is how many game folders are IN the
        directory — so that is the number to lead with. Reporting only the
        remainder made a scan of 109 folders say "96 games found" beside "13
        already in the library", which reads as 109 + 13 and leaves the user
        to work out that the two numbers overlap.
        """
        known = getattr(self, "_present_skipped", 0)
        if not known:
            return t("exe_scan.found", count=shown)
        return t("exe_scan.found_with_known",
                 total=shown + known, known=known, count=shown)

    def _build_list(self):
        """Show the found games, building only the rows that are on screen.

        The same windowing as the save-folder dialog, and for the same
        reason: a row here is six widgets with two editable fields, and Qt
        lays out and paints every one of them on a window resize whether it
        is visible or not. Keeping a widget per result made that cost grow
        with the scan. One continuous list, no pages — but the rows that
        exist are the ones in view, with spacers standing in for the rest at
        exactly the height they would have taken.

        The scan results are the master list from here on. Rows write their
        edits back into it (see _sync_row_to_entry), which is what makes them
        safe to destroy the moment they scroll away.
        """
        self._entries = [
            {"name": h.name, "exe_path": h.exe_path,
             "folder": getattr(h, "folder", ""), "depth": getattr(h, "depth", 0)}
            for h in self._pending_hits
        ]
        self._pending_hits = []
        self._render_list()
        self._finish_list(len(self._entries))

    # ── The master list, and the window showing part of it ──────────────────

    _WINDOW_BUFFER = 6

    def _sync_row_to_entry(self, row) -> None:
        """Record a row's current state on the entry it is showing."""
        i = getattr(row, "_entry_index", -1)
        if not (0 <= i < len(self._entries)):
            return
        try:
            self._entries[i]["name"] = row._name_edit.text()
            self._entries[i]["exe_path"] = row._path_edit.text()
        except RuntimeError:
            pass

    def _mark_entry_removed(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self._entries[index]["removed"] = True
            self._render_list()

    def _kept_entries(self) -> list:
        return [(i, e) for i, e in enumerate(self._entries)
                if not e.get("removed")]

    def _sync_visible_rows(self) -> None:
        self._wl_sync_visible()

    # ── The windowed list (see ui.widgets.windowed_list) ────────────────────

    def _render_list(self) -> None:
        """Show the results. Only the rows in view are built."""
        self._wl_render(self._scroll, self._empty_lbl)

    def _wl_entries(self) -> list:
        return self._kept_entries()

    def _wl_sync_row(self, row) -> None:
        self._sync_row_to_entry(row)

    def _wl_row_height(self) -> int:
        h = getattr(self, "_row_h", 0)
        if not h:
            from core.exe_scan import ScanHit
            probe = _CandidateRow(ScanHit(folder="", exe_path="", name=""),
                                  parent=self)
            h = max(1, probe.sizeHint().height())
            probe.setParent(None)
            probe.deleteLater()
            self._row_h = h
        return h

    def _wl_make_row(self, key, entry):
        from core.exe_scan import ScanHit
        hit = ScanHit(folder=entry.get("folder", ""),
                      exe_path=entry.get("exe_path", ""),
                      name=entry.get("name", ""),
                      depth=entry.get("depth", 0))
        return _CandidateRow(hit, parent=self, index=key)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._wl_update()

    def _finish_list(self, total: int):
        self._phase = "ready"
        self._save_btn.setEnabled(True)
        self._scan_btn.setEnabled(True)
        self._progress.setVisible(False)
        self._status.setText(self._found_message(total))
        if not self.isVisible():
            self.shelve_status.emit()

    def release_batch(self) -> None:
        """Drop the scan results and the rows showing them.

        The dialog is kept alive for shelving, so without this a scan of a
        few hundred folders stayed in memory — hits, rows and all — for as
        long as the app ran, long after it had been confirmed or abandoned.
        """
        self._pending_hits = []
        self._entries = []
        self._wl_clear()
        self._insert_index = 0
        for row in list(self._rows):
            try:
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass
        self._rows = []

    def _clear_rows(self):
        for row in self._rows:
            try:
                row.hide()
                row.deleteLater()
            except RuntimeError:
                pass
        self._rows = []
        self._empty_lbl.setVisible(True)

    # ── Commit ───────────────────────────────────────────────────────────────

    def _commit(self):
        if self._store_worker is not None and self._store_worker.isRunning():
            return
        # Everything on screen goes into the master list before anything is
        # read out of it: only the rows in VIEW exist as widgets, so they are
        # never the whole batch and must never be treated as it.
        self._sync_visible_rows()
        pending = [((e.get("name") or "").strip(), (e.get("exe_path") or "").strip())
                   for _i, e in self._kept_entries()]
        pending = [(n, p) for n, p in pending if n and p]
        if not pending:
            self._status.setText(t("exe_scan.nothing_selected"))
            return

        wants_search = self._search_cb.isChecked()
        if wants_search:
            reply = question_window_modal(
                self, t("exe_scan.search_warning_title"),
                t("exe_scan.search_warning", count=len(pending)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
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
        self._step = (current, total, name)
        self._status.setText(t("exe_scan.storing",
                               current=current, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_store_done(self, payload):
        added, skipped, invalid = payload[0], payload[1], payload[2]
        entries, cancelled = payload[3], payload[4]
        self._store_worker = None
        self._scan_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._search_cb.setEnabled(True)
        self._progress.setVisible(False)
        if cancelled:
            self._phase = "ready"
            self._status.setText(t("exe_scan.cancelled"))
            # Coming to rest has to reach the sidebar, or a put-away entry
            # goes on blinking "running" with its bar frozen where the work
            # stopped — which reads as still in flight.
            self.shelve_status.emit()
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
        self._force_close = True
        summary = "\n".join(parts)
        if not self.isVisible():
            # Put away in the sidebar: the run is done and the user is not
            # here. Nothing opens by itself — not the summary, and not the
            # online search they ticked, which is a long job with a warning
            # of its own and no business starting behind their back. The
            # batch is kept too, so clicking the notification brings up
            # this panel with its rows AND the confirmation over it.
            self._pending_summary = summary
            self.shelve_status.emit()
            return
        # "Then search online" is the user saying what happens next, so a
        # confirmation of what was just added is a step they already took —
        # and one standing between them and the search's own warning. With
        # the box ticked the search starts straight away; without it, the
        # summary is the end of the run and worth reading.
        if self._wants_search and self.added_entries:
            self.search_requested.emit([e.id for e in self.added_entries])
        else:
            information_window_modal(self, t("exe_scan.title"), summary)
        # Added — the scan results have done their job and nothing asks
        # about them again. See release_batch.
        self.release_batch()
        self.accept()

    # ── Shelve / lifecycle ───────────────────────────────────────────────────

    def has_shelvable_work(self) -> bool:
        return self._phase in ("scanning", "inserting", "storing")

    def shelve_progress(self) -> tuple:
        """``(done, total, name)`` for the sidebar, or ``(0, 0, "")``.

        Work put away is work the user cannot see, so how far it has got is
        the one thing the sidebar owes them. A blinking dot said something
        was happening and nothing more, which for a few hundred folders is
        the same as saying nothing.
        """
        if self._phase in ("scanning", "storing"):
            return getattr(self, "_step", (0, 0, ""))
        # Inserting has nothing to count: the list is handed over whole and
        # only a screenful of it is ever built, so counting live rows would
        # report about twenty out of however many were found and then stop.
        return (0, 0, "")

    def shelve_nav_label(self) -> str:
        done, total, name = self.shelve_progress()
        if total:
            return t("common.progress_label",
                     label=name or t("exe_scan.shelved_nav"),
                     done=done, total=total)
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
        pending = getattr(self, "_pending_summary", None)
        if pending is not None:
            # It finished while this was put away. The list is drawn FIRST
            # so the confirmation lands on top of the rows it is talking
            # about, and the online search — if it was ticked — starts from
            # here, with the user in front of it.
            self._pending_summary = None
            if self._wants_search and self.added_entries:
                self.search_requested.emit([e.id for e in self.added_entries])
            else:
                self._render_list()
                information_window_modal(self, t("exe_scan.title"), pending)
            self.release_batch()
            self.accept()
            return
        # The list is built in one pass, so there is never a half-finished
        # insert to resume — only hits that were found while hidden.
        # A scan that finished while hidden still has its hits to turn into
        # entries; one that already did just needs its window drawn again.
        if self._pending_hits:
            QTimer.singleShot(0, self._build_list)
        elif self._entries:
            QTimer.singleShot(0, self._render_list)

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
        self.release_batch()
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
        # Whatever route brought us here, the scan is finished with. Shelving
        # leaves by its own path (it ignores closeEvent above), so this only
        # runs when the dialog is genuinely done.
        self.release_batch()
        from ui.helpers import trim_process_memory
        QTimer.singleShot(250, trim_process_memory)
