"""
SaveSync - Add save folders by hand.

For saves SaveSync has no executable for: the user points at a folder (or at a
parent folder full of them) and each one becomes an orphan backup with a normal
index (game_name = folder title, save_paths = resolved path) — never a library
GameEntry. Restore is offered later through the same cloud-saves notification
when a matching game appears.

The single add is the one place where something IS worked out: a typed path can
be relative, so core.manual_paths looks for it under the known game locations
and leaves it unresolved rather than guessing.

The name is derived from the folder through the same walk-up that renames a
generic "game.exe", and stays editable per row.
"""
import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QCheckBox, QSizePolicy, QProgressBar,
)

from i18n import t
from core.manual_paths import (
    ManualPath, CollectedSave,
    resolve_manual_path, derive_folder_name, scan_save_collection,
    save_chain_of, live_save_chain, orphan_index_save_path, profile_destination,
    ACTUAL, RESOLVED, PREDICTED,
)
from ui.helpers import ElidedLabel, finalize_adaptive_dialog_size, scaled
from ui.styles.theme import palette
from ui.modal_helpers import information_window_modal

logger = logging.getLogger(__name__)

# Visible dialog: keep chunks tiny so ✕/shelve can run between ticks.
_INSERT_CHUNK = 6
# Shelved (hidden): no widgets — advance the index in larger steps.
_INSERT_CHUNK_SHELVED = 80
_PERSIST_DEBOUNCE_MS = 1200
_UI_PROGRESS_MIN_INTERVAL_S = 0.12


def _path_line(text: str = "") -> "ElidedLabel":
    """One-line, middle-elided label for the path lines of a row.

    These rows are listed by the hundred, so a wrapped path — one long token
    over three or four lines — buries the row and makes the list unusable.
    The full value stays in the tooltip.
    """
    return ElidedLabel(text)


class _CollectionWorker(QThread):
    """Reads a save-collection folder off the GUI thread.

    Each game folder is walked and its chain resolved, which touches the disk
    once per level — with a few dozen games that is long enough to freeze the
    window, so it gets the same treatment (and the same progress bar) as the
    game scan.
    """
    step = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self._root = root
        self._stop = False
        self.setPriority(QThread.Priority.IdlePriority)

    def stop(self):
        self._stop = True

    def run(self):
        try:
            found = scan_save_collection(
                self._root,
                progress=lambda i, n, name: self.step.emit(i, n, name),
                cancel=lambda: self._stop,
            )
        except Exception as e:
            logger.warning(f"Save-collection scan failed for {self._root}: {e}")
            found = []
        self.done.emit(found)


class _ManualPathRow(QFrame):
    """One folder: include-toggle, editable name, editable path, live verdict."""

    def __init__(self, path_text: str = "", parent=None, name: str = "",
                 source: str = "", chain: str = "", item=None):
        super().__init__(parent)
        self._source = source          # folder in the collection, if any
        self._chain = chain            # destination chain read from it
        # The reading the collection scan already did. It is kept until the
        # user edits the path, so nothing is re-derived behind their back.
        self._scan_item = item
        self._path_edited = False
        self._build()
        if path_text:
            self._path_edit.setText(path_text)
        if name:
            self._name_edit.setText(name)
            self._name_edited = True   # derived from the game folder, keep it
        self._revalidate()

    def _build(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._include = QCheckBox()
        self._include.setChecked(True)
        self._include.setToolTip(t("manual_path.include_tooltip"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(t("manual_path.name_placeholder"))
        self._name_edit.setMinimumWidth(scaled(150, self))
        self._name_edited = False
        self._name_edit.textEdited.connect(self._on_name_edited)
        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedSize(scaled(24, self), scaled(24, self))
        self._remove_btn.setToolTip(t("manual_path.remove_tooltip"))
        self._remove_btn.clicked.connect(self._remove_self)
        top.addWidget(self._include)
        top.addWidget(self._name_edit, 1)
        top.addWidget(self._remove_btn)
        outer.addLayout(top)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(t("manual_path.path_placeholder"))
        self._path_edit.textEdited.connect(self._on_path_edited)
        self._path_edit.textChanged.connect(self._revalidate)
        browse = QPushButton(t("add_game.browse"))
        browse.setFixedWidth(scaled(80, self))
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)
        outer.addLayout(path_row)

        self._verdict = _path_line()
        outer.addWidget(self._verdict)

        self._origin = _path_line()
        self._origin.setObjectName("path_entry_meta")
        self._origin.setVisible(False)
        outer.addWidget(self._origin)

        self.apply_theme()

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

    # ── State ────────────────────────────────────────────────────────────────

    def _on_name_edited(self, _text: str):
        self._name_edited = True

    def _on_path_edited(self, _text: str):
        """Once the user types their own path, the scan's reading no longer
        applies and the text is resolved on its own terms."""
        self._path_edited = True
        self._scan_item = None

    def _remove_self(self):
        # Hiding takes the row out of the layout just as well, and does not
        # leave it standing as a window of its own in the meantime.
        self.hide()
        self.deleteLater()

    def is_included(self) -> bool:
        return self._include.isChecked()

    def resolved(self):
        if self._scan_item is not None and not self._path_edited:
            return self._scan_item
        return resolve_manual_path(self._path_edit.text())

    def game_name(self) -> str:
        typed = self._name_edit.text().strip()
        return typed or derive_folder_name(self._path_edit.text())

    def chain(self) -> str:
        """The game-relative destination this row was read from, if any."""
        return self._chain

    def _browse(self):
        from ui.widgets.file_pickers import pick_folder
        start = self._path_edit.text().strip()
        picked = pick_folder(self, t("manual_path.pick_folder"),
                             start_dir=start if Path(start).is_dir() else "")
        if picked:
            self._path_edit.setText(picked)

    def _revalidate(self):
        item = self.resolved()
        if not self._name_edited:
            self._name_edit.setText(item.name)
        if not item.raw:
            self._verdict.setFullText("")
            return
        # The box above already shows WHICH folder gets backed up, so saying
        # "actual path" under it tells the user nothing — every row is a real
        # folder now. What differs between rows, and what they need to see, is
        # where those saves BELONG.
        if not item.path:
            colour, text = "warning", t("manual_path.verdict_predicted")
        elif not item.exists:
            colour, text = "warning", t("manual_path.verdict_actual_missing",
                                        path=item.path)
        elif self._chain:
            destination = profile_destination(self._chain)
            if destination is not None:
                colour = "success"
                text = t("manual_path.verdict_dest_known", path=str(destination))
            else:
                colour = "success"
                text = t("manual_path.verdict_dest_relative", chain=self._chain)
        else:
            colour, text = "success", t("manual_path.verdict_dest_here")
        self._verdict.setFullText(text)
        self._verdict.setStyleSheet(
            f"color:{palette(colour) if colour != 'success' else palette('accent')};font-size:{scaled(10, self)}px;")
        if self._chain and self._source:
            self._origin.setFullText(t("manual_path.from_collection",
                                   source=self._source, chain=self._chain))
            self._origin.setVisible(True)


class _StoreWorker(QThread):
    """Match + mtime checks off the GUI thread for large batches."""
    progress = Signal(int, int, str)
    finished_ok = Signal(object)  # (added, updated, skipped, entries, parts_msg)

    def __init__(self, pending: list, parent=None):
        super().__init__(parent)
        self._pending = pending
        self._stop = False
        self.setPriority(QThread.Priority.IdlePriority)

    def stop(self):
        self._stop = True

    def run(self):
        from core.backup import get_backup_manager
        from core.library import get_library

        lib = get_library()
        games = lib.all_games()
        known_paths = {
            p.casefold()
            for g in games
            for p in (g.save_paths or [])
            if p
        }
        # Paths already archived as orphans (same absolute path in index)
        bm = get_backup_manager()
        for b in bm.get_orphan_backups():
            for p in (b.save_paths or []):
                if p:
                    known_paths.add(p.casefold())

        added = updated = skipped = 0
        entries = []
        total = len(self._pending)
        last_emit = 0.0
        try:
            for i, row in enumerate(self._pending, 1):
                # (name, item, chain, raw, from_collection)
                if len(row) >= 5:
                    name, item, chain, raw, from_collection = row[:5]
                else:
                    name, item, chain, raw = row[:4]
                    from_collection = bool(raw)
                if self._stop:
                    break
                now = time.monotonic()
                if (
                    i == 1 or i == total or self._stop
                    or (now - last_emit) >= _UI_PROGRESS_MIN_INTERVAL_S
                ):
                    self.progress.emit(i, total, name or raw or "")
                    last_emit = now
                if not item.path or not item.exists:
                    skipped += 1
                    continue
                folder_title = (name or "").strip() or (item.name or "").strip()
                if not (name or "").strip():
                    folder_title = derive_folder_name(item.path) or folder_title
                folder_title = folder_title or Path(item.path).name
                chain = (chain or "").strip()
                if not chain and not from_collection:
                    chain = live_save_chain(item.path)
                recorded = orphan_index_save_path(
                    item.path, chain, folder_title,
                    from_collection=bool(from_collection),
                )
                # Dedup on the destination / zip-root label, not the archive
                # origin (two collections could share a source path spelling).
                path_key = (recorded or item.path).casefold()
                if path_key in known_paths:
                    skipped += 1
                    continue
                try:
                    entry = bm.create_orphan_backup(
                        game_name=folder_title,
                        save_paths=[item.path],
                        content_chain=chain or "",
                        save_chain=chain or "",
                        force=True,
                        recorded_save_paths=[recorded] if recorded else None,
                    )
                except Exception:
                    logger.exception(
                        "Orphan backup failed for %s", item.path)
                    skipped += 1
                    continue
                if entry is None:
                    skipped += 1
                    continue
                known_paths.add(path_key)
                entries.append(entry)
                added += 1
        finally:
            pass
        self.finished_ok.emit((added, updated, skipped, entries, self._stop))


class ManualPathDialog(QDialog):
    """Add one save folder, or every folder inside a chosen parent."""

    shelved = Signal()
    # Sidebar label/tooltip refresh while work continues hidden.
    shelve_status = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("manual_path.title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.added_entries: list = []
        self._rows: list = []
        self._worker = None
        self._store_worker = None
        self._force_close = False
        self._cancel_op = False
        self._phase = "idle"  # idle|scanning|inserting|ready|storing
        self._insert_queue: list = []
        self._insert_index = 0
        self._collection_root = ""
        self._found_serialized: list = []
        self._last_ui_progress_at = 0.0
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(_PERSIST_DEBOUNCE_MS)
        self._persist_timer.timeout.connect(self._persist_state)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        header = QLabel(t("manual_path.title"))
        header.setObjectName("dialog_title")
        root.addWidget(header)

        desc = QLabel(t("manual_path.description"))
        desc.setObjectName("dialog_desc")
        desc.setWordWrap(True)
        root.addWidget(desc)

        # ── Single vs multiple: only a difference in how you select ──────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self._single_btn = QPushButton(t("manual_path.add_single"))
        self._single_btn.setToolTip(t("manual_path.add_single_tooltip"))
        self._single_btn.clicked.connect(self._add_single)
        self._multi_btn = QPushButton(t("manual_path.add_multiple"))
        self._multi_btn.setToolTip(t("manual_path.add_multiple_tooltip"))
        self._multi_btn.clicked.connect(self._add_multiple)
        for btn in (self._single_btn, self._multi_btn):
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setMinimumHeight(scaled(34, self))
            mode_row.addWidget(btn)
        root.addLayout(mode_row)

        self._progress = QProgressBar()
        self._progress.setFixedHeight(scaled(5, self))
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self._rows_layout = QVBoxLayout(holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(8)
        self._empty_lbl = QLabel(t("manual_path.empty"))
        self._empty_lbl.setObjectName("dialog_empty")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rows_layout.addWidget(self._empty_lbl)
        self._rows_layout.addStretch()
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll, 1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        fs = scaled(11, self)
        self._status.setStyleSheet(f"color:{palette('warning')};font-size:{fs}px;")
        root.addWidget(self._status)

        # Spelled out rather than implied: the layout this reads is a
        # convention the user follows, and it is what makes both backup and
        # restore land in the right place.
        self._collection_hint = QLabel(t("manual_path.collection_hint"))
        self._collection_hint.setWordWrap(True)
        fs = scaled(10, self)
        self._collection_hint.setStyleSheet(
            f"color:{palette('text_muted')};font-size:{fs}px;")
        self._collection_hint.setVisible(False)
        root.addWidget(self._collection_hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton(t("common.cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._save_btn = QPushButton(t("manual_path.save"))
        self._save_btn.setObjectName("primary_btn")
        self._save_btn.clicked.connect(self._commit)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=560, min_h=420, scroll=self._scroll, list_content=True)

    # ── Selection ────────────────────────────────────────────────────────────

    def _add_single(self):
        from ui.widgets.file_pickers import pick_folder
        picked = pick_folder(self, t("manual_path.pick_folder"))
        if picked:
            # Live game save folder: walk UP for the destination chain
            # (AppData/Roaming/RenPy/… or www/save), not save_chain_of which
            # descends inside a collection copy.
            self._add_row(picked, chain=live_save_chain(picked))

    def _add_multiple(self):
        """Read a whole save-collection folder.

        Each subfolder is one game and IS a backup source — the saves are
        right there. What is inside it is read only to record where they
        belong: "<Game>/www/save" says "www/save under the game", and
        "<Game>/AppData/Roaming/…" names a user folder, which is the one
        destination that can be pointed at on this machine without guessing.

        The collection folder itself can be named anything: only one level
        below it is read, and each subfolder names the game.
        """
        from ui.widgets.file_pickers import pick_folder
        parent = pick_folder(self, t("manual_path.pick_parent"))
        if not parent:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._cancel_op = False
        self._phase = "scanning"
        self._collection_root = parent
        self._multi_btn.setEnabled(False)
        self._single_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status.setText(t("manual_path.reading"))
        self._worker = _CollectionWorker(parent, parent=self)
        self._worker._root_label = parent
        self._worker.step.connect(self._on_collection_step)
        self._worker.done.connect(self._on_collection_done)
        self._persist_state_now()
        self._worker.start()

    def _on_collection_step(self, index: int, total: int, name: str):
        now = time.monotonic()
        if (
            index < total
            and (now - self._last_ui_progress_at) < _UI_PROGRESS_MIN_INTERVAL_S
        ):
            return
        self._last_ui_progress_at = now
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(index)
        self._status.setText(t("manual_path.reading_folder",
                               current=index, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_collection_done(self, found):
        root_label = getattr(self._worker, "_root_label", "") if self._worker else self._collection_root
        self._worker = None
        if self._cancel_op:
            self._set_idle(t("manual_path.cancelled"))
            return
        if not found:
            self._set_idle("")
            information_window_modal(self, t("manual_path.title"),
                                     t("manual_path.no_subfolders", path=root_label))
            return
        self._found_serialized = [self._serialize_collected(c) for c in found]
        self._insert_queue = list(found)
        self._insert_index = 0
        self._phase = "inserting"
        self._progress.setRange(0, max(1, len(found)))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._persist_state_now()
        self._insert_next_chunk()

    def _collected_at(self, index: int):
        if self._insert_queue and index < len(self._insert_queue):
            return self._insert_queue[index]
        if 0 <= index < len(self._found_serialized):
            return self._deserialize_collected(self._found_serialized[index])
        return None

    def _materialize_rows_chunk(self, up_to: int) -> bool:
        """Build missing row widgets up to *up_to*. Returns True if more left."""
        up_to = min(up_to, len(self._found_serialized))
        end = min(len(self._rows) + _INSERT_CHUNK, up_to)
        while len(self._rows) < end:
            collected = self._collected_at(len(self._rows))
            if collected is None:
                break
            self._add_row(
                collected.item.path or collected.chain or collected.source,
                name=collected.name, source=collected.source,
                chain=collected.chain, item=collected.item,
            )
        return len(self._rows) < up_to

    def _insert_next_chunk(self):
        if self._cancel_op:
            self._insert_queue = []
            self._set_idle(t("manual_path.cancelled"))
            self._clear_persisted()
            return
        total = len(self._found_serialized) or len(self._insert_queue)
        visible = self.isVisible()

        # Catch up widgets skipped while the dialog was shelved.
        if visible and len(self._rows) < self._insert_index:
            if self._materialize_rows_chunk(self._insert_index):
                QTimer.singleShot(0, self._insert_next_chunk)
                return

        chunk = _INSERT_CHUNK if visible else _INSERT_CHUNK_SHELVED
        end = min(self._insert_index + chunk, len(self._insert_queue))
        if visible:
            for collected in self._insert_queue[self._insert_index:end]:
                self._add_row(
                    collected.item.path or collected.chain or collected.source,
                    name=collected.name, source=collected.source,
                    chain=collected.chain, item=collected.item,
                )
                self._insert_index += 1
        else:
            self._insert_index = end

        name = ""
        if 0 < self._insert_index <= len(self._insert_queue):
            name = self._insert_queue[self._insert_index - 1].name or ""
        self._status.setText(t("manual_path.inserting_folder",
                               current=self._insert_index,
                               total=total, name=name))
        self._progress.setValue(self._insert_index)
        self._persist_state_soon()
        if not visible:
            self.shelve_status.emit()

        if self._insert_index < len(self._insert_queue):
            QTimer.singleShot(0, self._insert_next_chunk)
            return
        # Done inserting
        self._insert_queue = []
        self._phase = "ready"
        self._multi_btn.setEnabled(True)
        self._single_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._progress.setVisible(False)
        msg = t("manual_path.multiple_added", count=total)
        if visible and self._rows:
            unresolved = sum(
                1 for r in self._rows
                if not r.isHidden() and getattr(r, "_scan_item", None)
                and not r._scan_item.backupable)
            if unresolved:
                msg += "  " + t("manual_path.multiple_unresolved", count=unresolved)
        self._status.setText(msg)
        self._collection_hint.setVisible(True)
        self._persist_state_now()
        if not visible:
            self.shelve_status.emit()
        elif len(self._rows) < len(self._found_serialized):
            # Finished index while catching up widgets — keep materializing.
            QTimer.singleShot(0, self._finish_materialize_rows)

    def _finish_materialize_rows(self):
        if not self.isVisible() or self._cancel_op:
            return
        if self._materialize_rows_chunk(len(self._found_serialized)):
            QTimer.singleShot(0, self._finish_materialize_rows)
            return
        unresolved = sum(
            1 for r in self._rows
            if not r.isHidden() and getattr(r, "_scan_item", None)
            and not r._scan_item.backupable)
        msg = t("manual_path.multiple_added", count=len(self._found_serialized))
        if unresolved:
            msg += "  " + t("manual_path.multiple_unresolved", count=unresolved)
        self._status.setText(msg)

    @staticmethod
    def _serialize_collected(c: CollectedSave) -> dict:
        it = c.item
        return {
            "source": c.source,
            "name": c.name,
            "chain": c.chain,
            "item": {
                "raw": it.raw, "kind": it.kind, "path": it.path,
                "name": it.name, "exists": it.exists,
            },
        }

    @staticmethod
    def _deserialize_collected(d: dict) -> CollectedSave:
        raw = d.get("item") or {}
        item = ManualPath(
            raw=raw.get("raw") or "",
            kind=raw.get("kind") or ACTUAL,
            path=raw.get("path") or "",
            name=raw.get("name") or "",
            exists=bool(raw.get("exists")),
        )
        return CollectedSave(
            source=d.get("source") or "",
            name=d.get("name") or "",
            chain=d.get("chain") or "",
            item=item,
        )

    def _set_idle(self, status: str = ""):
        self._phase = "idle"
        self._cancel_op = False
        self._progress.setVisible(False)
        self._multi_btn.setEnabled(True)
        self._single_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        if status:
            self._status.setText(status)

    def _add_row(self, path_text: str, name: str = "", source: str = "",
                 chain: str = "", item=None):
        row = _ManualPathRow(path_text, parent=self, name=name,
                             source=source, chain=chain, item=item)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._empty_lbl.setVisible(False)

    def _live_rows(self) -> list:
        """Rows still on screen — a removed row deletes itself, so the list is
        filtered rather than trusted. Being hidden is what marks it gone: it
        hides and waits to be deleted, rather than detaching itself, which
        would leave it standing as a window of its own in the meantime.
        isHidden asks whether THIS row was hidden, not whether the dialog it
        sits in happens to be, so it holds before the dialog is shown too.
        """
        alive = []
        for row in self._rows:
            try:
                if not row.isHidden() and row.is_included():
                    alive.append(row)
            except RuntimeError:
                continue      # already deleted by Qt
        return alive

    # ── Commit ───────────────────────────────────────────────────────────────

    def _commit(self):
        # Rows may still be virtual after a shelved insert — materialize first.
        if (
            self._found_serialized
            and len(self._rows) < len(self._found_serialized)
        ):
            self._status.setText(t("manual_path.inserting_folder",
                                   current=len(self._rows),
                                   total=len(self._found_serialized),
                                   name=""))
            self._progress.setRange(0, max(1, len(self._found_serialized)))
            self._progress.setValue(len(self._rows))
            self._progress.setVisible(True)
            self._save_btn.setEnabled(False)
            QTimer.singleShot(0, self._materialize_then_commit)
            return

        rows = self._live_rows()
        if not rows:
            self._status.setText(t("manual_path.nothing_selected"))
            return
        if self._store_worker is not None and self._store_worker.isRunning():
            return

        pending, unresolved = [], []
        for row in rows:
            item = row.resolved()
            if item.path:
                from_collection = bool(row._source)
                if from_collection:
                    chain = row.chain() or save_chain_of(item.path)
                else:
                    chain = row.chain() or live_save_chain(item.path)
                if row._source:
                    raw = Path(row._source).name
                else:
                    raw = Path(item.path).name if item.path else ""
                pending.append(
                    (row.game_name(), item, chain, raw, from_collection))
            else:
                unresolved.append(row.game_name() or item.raw)

        if not pending:
            self._status.setText(t("manual_path.none_resolved"))
            return

        self._pending_unresolved = unresolved
        self._pending_for_store = pending
        self._cancel_op = False
        self._phase = "storing"
        self._multi_btn.setEnabled(False)
        self._single_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._progress.setRange(0, max(1, len(pending)))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._status.setText(t("manual_path.storing", current=0, total=len(pending), name=""))
        self._persist_state_now()
        self._store_worker = _StoreWorker(pending, parent=self)
        self._store_worker.progress.connect(self._on_store_step)
        self._store_worker.finished_ok.connect(self._on_store_done)
        self._store_worker.start()

    def _materialize_then_commit(self):
        if self._cancel_op:
            self._save_btn.setEnabled(True)
            return
        if self._materialize_rows_chunk(len(self._found_serialized)):
            self._progress.setValue(len(self._rows))
            self._status.setText(t("manual_path.inserting_folder",
                                   current=len(self._rows),
                                   total=len(self._found_serialized),
                                   name=""))
            QTimer.singleShot(0, self._materialize_then_commit)
            return
        self._progress.setVisible(False)
        self._save_btn.setEnabled(True)
        self._commit()

    def _on_store_step(self, current: int, total: int, name: str):
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(current)
        self._status.setText(t("manual_path.storing",
                               current=current, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_store_done(self, payload):
        added, updated, skipped, entries, cancelled = payload
        self._store_worker = None
        if cancelled:
            self._set_idle(t("manual_path.cancelled"))
            self._clear_persisted()
            return
        self.added_entries.extend(entries)
        parts = []
        if added:
            parts.append(t("manual_path.result_added", count=added))
        if updated:
            parts.append(t("manual_path.result_updated", count=updated))
        if skipped:
            parts.append(t("manual_path.result_skipped", count=skipped))
        pending = getattr(self, "_pending_for_store", []) or []
        missing = sum(1 for _n, it, _c, _r in pending if not it.exists)
        if missing:
            parts.append(t("manual_path.result_not_yet", count=missing))
        unresolved = getattr(self, "_pending_unresolved", []) or []
        if unresolved:
            parts.append(t("manual_path.result_unresolved",
                           count=len(unresolved), names=", ".join(unresolved[:5])))
        self._clear_persisted()
        self._force_close = True
        information_window_modal(self, t("manual_path.title"), "\n".join(parts) or "")
        self.accept()

    def _on_cancel_clicked(self):
        """Annulla ferma l'operazione in corso; altrimenti chiude il dialog."""
        if self.has_shelvable_work():
            self._cancel_op = True
            if self._worker is not None and self._worker.isRunning():
                self._worker.stop()
            if self._store_worker is not None and self._store_worker.isRunning():
                self._store_worker.stop()
            if self._phase == "inserting":
                self._insert_queue = []
                self._set_idle(t("manual_path.cancelled"))
                self._clear_persisted()
            self._status.setText(t("manual_path.cancelling"))
            return
        self._force_close = True
        self._clear_persisted()
        self.reject()

    def has_shelvable_work(self) -> bool:
        return self._phase in ("scanning", "inserting", "storing")

    def shelve_nav_label(self) -> str:
        return t("manual_path.shelved_nav")

    def shelve_nav_tooltip(self) -> str:
        total = len(self._found_serialized)
        if self._phase == "scanning":
            return t("manual_path.shelved_scanning")
        if self._phase == "inserting":
            base = t("manual_path.shelved_inserting")
            if total:
                return f"{base} ({self._insert_index}/{total})"
            return base
        if self._phase == "storing":
            return t("manual_path.shelved_storing")
        return t("manual_path.shelved_nav")

    def _shelve(self):
        self._persist_state_now()
        self.hide()
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.shelved.emit()

    def unshelve(self):
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.show()
        self.raise_()
        self.activateWindow()
        # Rebuild any row widgets skipped while hidden.
        if (
            self._found_serialized
            and len(self._rows) < len(self._found_serialized)
            and self._phase in ("inserting", "ready", "storing")
        ):
            if self._phase == "inserting" and self._insert_queue:
                QTimer.singleShot(0, self._insert_next_chunk)
            else:
                QTimer.singleShot(0, self._finish_materialize_rows)

    def closeEvent(self, event):
        if (not self._force_close and self.has_shelvable_work()
                and not self._cancel_op):
            event.ignore()
            self._shelve()
            return
        self._force_close = True
        self._cancel_op = True
        self._persist_timer.stop()
        worker = self._worker
        if worker is not None and worker.isRunning():
            for signal in (worker.step, worker.done):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            worker.stop()
            if not worker.wait(2000):
                logger.info("Collection worker still running at close — detached")
            self._worker = None
        sw = self._store_worker
        if sw is not None and sw.isRunning():
            sw.stop()
            sw.wait(2000)
            self._store_worker = None
        if not self.added_entries:
            # Closing without save — drop resume state unless shelved mid-work
            # (shelve ignores closeEvent above).
            self._clear_persisted()
        super().closeEvent(event)
        from ui.helpers import trim_process_memory
        QTimer.singleShot(250, trim_process_memory)

    def _persist_state_soon(self):
        if not self._persist_timer.isActive():
            self._persist_timer.start()

    def _persist_state_now(self):
        self._persist_timer.stop()
        self._persist_state()

    def _persist_state(self):
        from core import pending_batch_jobs as _pbj
        if self._phase in ("idle",) and not self._found_serialized:
            return
        _pbj.set_job(_pbj.KEY_MANUAL_BATCH, {
            "phase": self._phase,
            "root": self._collection_root,
            "found": self._found_serialized,
            "insert_index": self._insert_index,
        })

    def _clear_persisted(self):
        self._persist_timer.stop()
        from core import pending_batch_jobs as _pbj
        _pbj.clear_job(_pbj.KEY_MANUAL_BATCH)

    def restore_persisted_state(self, state: dict):
        """Resume Aggiunta multipla after an app restart."""
        if not state:
            return
        self._collection_root = state.get("root") or ""
        self._found_serialized = list(state.get("found") or [])
        self._insert_index = int(state.get("insert_index") or 0)
        phase = state.get("phase") or "ready"
        if not self._found_serialized:
            return
        found = [self._deserialize_collected(d) for d in self._found_serialized]
        self._insert_index = max(0, min(self._insert_index, len(found)))
        # Do not sync-build hundreds of row widgets here — that froze resume.
        # Shelve first; rows materialize on unshelve / while inserting hidden.
        self._collection_hint.setVisible(True)
        resume_insert = (
            self._insert_index < len(found) and phase in ("scanning", "inserting")
        )
        if resume_insert:
            self._insert_queue = found
            self._phase = "inserting"
            self._multi_btn.setEnabled(False)
            self._single_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            total = len(found)
            self._progress.setRange(0, max(1, total))
            self._progress.setValue(self._insert_index)
            self._progress.setVisible(True)
            self._status.setText(t("manual_path.inserting_folder",
                                   current=self._insert_index, total=total, name=""))
        else:
            self._phase = "ready"
            self._status.setText(t("manual_path.multiple_added", count=len(found)))
        # Shelve first so the following insert ticks run hidden (no widgets).
        QTimer.singleShot(0, self._shelve)
        if resume_insert:
            QTimer.singleShot(0, self._insert_next_chunk)
