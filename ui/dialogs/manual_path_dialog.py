"""
SaveSync - Add save folders by hand.

For saves SaveSync has no executable for: the user points at a folder (or at a
parent folder full of them) and each one becomes a backed-up entry. There is no
game to link them to and none is looked for — a folder handed over here is a
backup source, and the structure inside it is recorded as the destination a
restore needs.

The single add is the one place where something IS worked out: a typed path can
be relative, so core.manual_paths looks for it under the known game locations
and leaves it unresolved rather than guessing.

The name is derived from the folder through the same walk-up that renames a
generic "game.exe", and stays editable per row.
"""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QCheckBox, QSizePolicy, QProgressBar,
)

from i18n import t
from core.manual_paths import (
    resolve_manual_path, derive_folder_name, scan_save_collection,
    save_chain_of, names_of, profile_destination,
    ACTUAL, RESOLVED, PREDICTED,
)
from ui.helpers import ElidedLabel
from ui.styles.theme import palette
from ui.modal_helpers import information_window_modal

logger = logging.getLogger(__name__)


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
        self._name_edit.setMinimumWidth(150)
        self._name_edited = False
        self._name_edit.textEdited.connect(self._on_name_edited)
        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedSize(24, 24)
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
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)
        outer.addLayout(path_row)

        self._verdict = _path_line()
        outer.addWidget(self._verdict)

        self._origin = _path_line()
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
        self.setParent(None)
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
            f"color:{palette(colour) if colour != 'success' else palette('accent')};font-size:10px;")
        if self._chain and self._source:
            self._origin.setFullText(t("manual_path.from_collection",
                                   source=self._source, chain=self._chain))
            self._origin.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;")
            self._origin.setVisible(True)


class ManualPathDialog(QDialog):
    """Add one save folder, or every folder inside a chosen parent."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("manual_path.title"))
        self.setMinimumWidth(600)
        self.setMinimumHeight(460)
        self.added_entries: list = []
        self._rows: list = []
        self._worker = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        header = QLabel(t("manual_path.title"))
        header.setStyleSheet(f"color:{palette('text')};font-size:16px;font-weight:600;")
        root.addWidget(header)

        desc = QLabel(t("manual_path.description"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{palette('text_secondary')};font-size:11px;")
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
            btn.setMinimumHeight(34)
            mode_row.addWidget(btn)
        root.addLayout(mode_row)

        self._progress = QProgressBar()
        self._progress.setFixedHeight(5)
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
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:12px;padding:24px;")
        self._rows_layout.addWidget(self._empty_lbl)
        self._rows_layout.addStretch()
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll, 1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{palette('warning')};font-size:11px;")
        root.addWidget(self._status)

        # Spelled out rather than implied: the layout this reads is a
        # convention the user follows, and it is what makes both backup and
        # restore land in the right place.
        self._collection_hint = QLabel(t("manual_path.collection_hint"))
        self._collection_hint.setWordWrap(True)
        self._collection_hint.setStyleSheet(
            f"color:{palette('text_muted')};font-size:10px;")
        self._collection_hint.setVisible(False)
        root.addWidget(self._collection_hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        self._save_btn = QPushButton(t("manual_path.save"))
        self._save_btn.setObjectName("primary_btn")
        self._save_btn.setStyleSheet(
            f"QPushButton{{background:{palette('accent')};color:{palette('accent_text')};"
            f"border:none;border-radius:6px;padding:8px 16px;font-weight:600;}}"
            f"QPushButton:hover{{background:{palette('accent_hover')};}}"
        )
        self._save_btn.clicked.connect(self._commit)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        self.setStyleSheet(f"QDialog{{background:{palette('bg_card')};}}")

    # ── Selection ────────────────────────────────────────────────────────────

    def _add_single(self):
        from ui.widgets.file_pickers import pick_folder
        picked = pick_folder(self, t("manual_path.pick_folder"))
        if picked:
            # Read the same way as a folder picked out of a collection —
            # picking one at a time is the only difference between the two.
            self._add_row(picked, chain=save_chain_of(picked))

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
        self._multi_btn.setEnabled(False)
        self._single_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status.setText(t("manual_path.reading"))
        self._worker = _CollectionWorker(parent, parent=self)
        self._worker._root_label = parent
        self._worker.step.connect(self._on_collection_step)
        self._worker.done.connect(self._on_collection_done)
        self._worker.start()

    def _on_collection_step(self, index: int, total: int, name: str):
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(index)
        self._status.setText(t("manual_path.reading_folder",
                               current=index, total=total, name=name))

    def _on_collection_done(self, found):
        self._progress.setVisible(False)
        self._multi_btn.setEnabled(True)
        self._single_btn.setEnabled(True)
        root_label = getattr(self._worker, "_root_label", "")
        self._worker = None
        if not found:
            information_window_modal(self, t("manual_path.title"),
                                     t("manual_path.no_subfolders", path=root_label))
            return
        unresolved = 0
        for collected in found:
            self._add_row(collected.item.path or collected.chain or collected.source,
                          name=collected.name, source=collected.source,
                          chain=collected.chain, item=collected.item)
            if not collected.item.backupable:
                unresolved += 1
        msg = t("manual_path.multiple_added", count=len(found))
        if unresolved:
            msg += "  " + t("manual_path.multiple_unresolved", count=unresolved)
        self._status.setText(msg)
        self._collection_hint.setVisible(True)

    def _add_row(self, path_text: str, name: str = "", source: str = "",
                 chain: str = "", item=None):
        row = _ManualPathRow(path_text, parent=self, name=name,
                             source=source, chain=chain, item=item)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._empty_lbl.setVisible(False)

    def _live_rows(self) -> list:
        """Rows still on screen — a removed row deletes itself, so the list is
        filtered rather than trusted."""
        alive = []
        for row in self._rows:
            try:
                if row.parent() is not None and row.is_included():
                    alive.append(row)
            except RuntimeError:
                continue      # already deleted by Qt
        return alive

    # ── Commit ───────────────────────────────────────────────────────────────

    def _commit(self):
        rows = self._live_rows()
        if not rows:
            self._status.setText(t("manual_path.nothing_selected"))
            return

        pending, unresolved = [], []
        for row in rows:
            item = row.resolved()
            if item.path:
                # The chain rides along: it is what makes the saves land in
                # the right place when they are put back, here or on another
                # machine. A KNOWN location is registered even when it does
                # not exist yet — the point is to have the destination ready.
                #
                # A folder chosen one at a time is read the same way as one
                # picked out of a collection: same folder, same chain. The
                # row's own chain wins only because it was read before the
                # user could edit the path.
                chain = row.chain() or save_chain_of(item.path)
                # The folder's own name, decoration and all. The title beside
                # it has been tidied; this is what still carries the release
                # code and version, and it is what matches the game's install
                # folder exactly.
                raw = Path(item.path).name if item.path else ""
                pending.append((row.game_name(), item, chain, raw))
            else:
                # Only a typed path can end up here: a folder read out of a
                # collection is always somewhere real.
                unresolved.append(row.game_name() or item.raw)

        if not pending:
            self._status.setText(t("manual_path.none_resolved"))
            return

        added, updated, skipped = self._store(pending)
        parts = []
        if added:
            parts.append(t("manual_path.result_added", count=added))
        if updated:
            parts.append(t("manual_path.result_updated", count=updated))
        if skipped:
            parts.append(t("manual_path.result_skipped", count=skipped))
        missing = sum(1 for _n, it, _c, _r in pending if not it.exists)
        if missing:
            parts.append(t("manual_path.result_not_yet", count=missing))
        if unresolved:
            parts.append(t("manual_path.result_unresolved",
                           count=len(unresolved), names=", ".join(unresolved[:5])))
        information_window_modal(self, t("manual_path.title"), "\n".join(parts))
        self.accept()

    def closeEvent(self, event):
        """Detach a running walk rather than block the close on it."""
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
        super().closeEvent(event)

    def _store(self, pending: list) -> tuple:
        """Write the folders into the library.

        A folder already registered on some game is left alone; a name that
        matches an existing game joins that game rather than creating a
        second, near-identical entry; everything else becomes a new entry with
        no executable — legitimate here, backups only ever need the paths.
        """
        from core.library import GameEntry, get_library
        from core.machine import get_machine_id
        from core.constants import get_folder_name_for_save

        lib = get_library()
        games = lib.all_games()
        by_path = {p.casefold(): g for g in games for p in (g.save_paths or [])}
        # Every name a game answers to, its former titles included: a folder
        # named after the game must find that game even if the library has
        # since renamed it. The current title wins where two entries collide.
        by_name = {}
        for g in games:
            for known in names_of(g):
                by_name.setdefault(known, g)
        for g in games:
            title = (g.name or "").strip().casefold()
            if title:
                by_name[title] = g

        added = updated = skipped = 0
        for name, item, chain, raw in pending:
            if item.path.casefold() in by_path:
                skipped += 1
                continue
            existing = by_name.get((name or "").strip().casefold())
            if existing is not None:
                existing.save_paths = list(existing.save_paths or []) + [item.path]
                existing.save_paths_confirmed = True
                # Per path: this game may already have a folder pointing
                # somewhere else entirely, and its destination must survive.
                existing.record_path_chain(item.path, chain)
                existing.record_name_hint(raw)
                lib.update_game(existing)
                by_path[item.path.casefold()] = existing
                self.added_entries.append(existing)
                updated += 1
                continue
            entry = GameEntry(
                name=name or item.name,
                exe_path="",
                save_paths=[item.path],
                save_paths_confirmed=True,
                requires_confirmation=False,
                auto_added=False,
                machine_id=get_machine_id(),
                computed_folder_name=get_folder_name_for_save(name or item.name, "", ""),
                save_chain=chain,
            )
            entry.record_name(entry.name)
            entry.record_path_chain(item.path, chain)
            entry.record_name_hint(raw)
            entry.computed_folder_name = lib.unique_folder_name(
                entry.computed_folder_name, entry.id)
            lib.add_game(entry)
            by_path[item.path.casefold()] = entry
            by_name[(entry.name or "").strip().casefold()] = entry
            self.added_entries.append(entry)
            added += 1
            logger.info(f"Manually added save folder for {entry.name}: {item.path}")
        return added, updated, skipped
