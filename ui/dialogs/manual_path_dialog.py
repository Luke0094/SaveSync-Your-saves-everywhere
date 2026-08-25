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

from PySide6.QtCore import Qt, QSize, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QWidget, QFrame, QSizePolicy, QProgressBar,
)

from i18n import t
from core.manual_paths import (
    ManualPath, CollectedSave,
    resolve_manual_path, derive_folder_name, scan_save_collection,
    save_chain_of, live_save_chain, orphan_index_save_path, profile_destination,
    ACTUAL, RESOLVED, PREDICTED,
)
from ui.helpers import (ElidedLabel, center_dialog,
                        finalize_adaptive_dialog_size, scaled)
from ui.widgets.windowed_list import WindowedListMixin
from ui.styles.theme import palette
from ui.modal_helpers import information_window_modal

logger = logging.getLogger(__name__)

# Visible dialog: keep the gap between ticks tiny so ✕/shelve can run between
# them. That used to be spelled as a fixed row count (six), but a COUNT is the
# wrong unit for the promise. What has to stay
# small is the time between two chances to press ✕, and one row costs roughly
# 4-5 ms to build (measured: two QLineEdits, a checkbox, two buttons, two
# elided labels, three layouts and a stylesheet each — nearly all of it Qt
# widget construction). Six of those is ~28 ms of work followed by a full
# event-loop round trip that re-lays-out and repaints the scroll area, so a
# collection of several hundred folders spent much of its time going round
# the loop rather than building anything. Rows are built until this budget is
# spent instead, which keeps the gap between ticks at about a frame — the
# responsiveness the count was standing in for — while cutting the number of
# round trips for a big list by an order of magnitude.
# Both come from core.concurrency, which is where every "how much work per
# turn" answer in the app is derived from the machine's CPU/RAM tier — the
# same place the library's own chunk size, the poll rates and the sweep
# intervals come from. Read per pass rather than at import: the tier is
# cached there and can change when the machine's free RAM does.
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

    def stop(self):
        self._stop = True

    def run(self):
        # Set from inside run(): setPriority only applies to a RUNNING
        # thread. From __init__ it did nothing but log "Cannot set
        # priority, thread is not running", so these scans never
        # actually ran at idle priority — which is the one thing the
        # call was there to do while a game has the CPU.
        self.setPriority(QThread.Priority.IdlePriority)
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
                 source: str = "", chain: str = "", item=None, index: int = -1):
        super().__init__(parent)
        self._source = source          # folder in the collection, if any
        self._chain = chain            # destination chain read from it
        # Which entry of the dialog's master list this row is showing. Only a
        # page of rows exists at a time, so a row is a VIEW of that entry and
        # everything the user does to it has to be written back there — see
        # ManualPathDialog._sync_row_to_entry. -1 for the single-add row,
        # which has no master list behind it.
        self._entry_index = index
        # Held directly rather than reached through parent(): adding the row
        # to the scroll area's layout reparents it to the holder widget, so
        # by the time anyone asks, parent() is no longer the dialog.
        self._dialog = parent
        # The reading the collection scan already did. It is kept until the
        # user edits the path, so nothing is re-derived behind their back.
        self._scan_item = item
        self._path_edited = False
        # Populating the fields below fires textChanged, which is wired to
        # _revalidate — so every row used to validate itself once while it was
        # still half-built (path set, name not) and again at the end, and each
        # pass rebuilds a stylesheet string and re-applies it. Rows are listed
        # by the hundred here, so that doubled work is a visible part of how
        # long a big collection takes to appear. One validation, once the row
        # is actually complete.
        self._building = True
        try:
            self._build()
            if path_text:
                self._path_edit.setText(path_text)
            if name:
                self._name_edit.setText(name)
                self._name_edited = True   # derived from the game folder, keep it
        finally:
            self._building = False
        self._revalidate()

    def _build(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(t("manual_path.name_placeholder"))
        self._name_edit.setMinimumWidth(scaled(150, self))
        self._name_edited = False
        self._name_edit.textEdited.connect(self._on_name_edited)
        # One verb, not two. A tick-box and a remove button asked the same
        # question twice — "is this one going in?" — and answered it in two
        # places that could disagree, while the unticked rows stayed on
        # screen taking up room in a list already hundreds long. The row is
        # in the batch because it is in the list; the bin takes it out.
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
        # Explicit padding and font size, not the app default. An unstyled
        # QPushButton inherits the theme's 7px/16px padding, and in a 24px
        # box that leaves no room at all for the glyph — the button draws as
        # an empty square that still takes clicks, which is exactly how this
        # one looked.
        self._remove_btn.setToolTip(t("manual_path.remove_tooltip"))
        self._remove_btn.clicked.connect(self._remove_self)
        # First in the row, in the column the tick-box used to hold — see
        # the same choice in the folder-scan dialog.
        top.addWidget(self._remove_btn)
        top.addWidget(self._name_edit, 1)
        outer.addLayout(top)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(t("manual_path.path_placeholder"))
        self._path_edit.textEdited.connect(self._on_path_edited)
        self._path_edit.textChanged.connect(self._revalidate)
        # Open the folder, next to the one that picks a different one.
        # Reading a row is mostly deciding whether THIS is the right folder,
        # and a path string is a poor way to answer that when the contents
        # settle it in a glance. The path field carries the stretch, so it
        # gives up exactly this button's width and nothing else moves.
        self._open_btn = QPushButton("📂")
        _ob = scaled(26, self, min_px=22)
        self._open_btn.setFixedSize(_ob, _ob)
        self._open_btn.setObjectName("auto_scan_icon_btn")
        self._open_btn.setToolTip(t("add_game.open_folder"))
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.clicked.connect(self._open_folder)
        browse = QPushButton(t("add_game.browse"))
        browse.setFixedWidth(scaled(80, self))
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(self._open_btn)
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
        self._write_back()

    def _on_path_edited(self, _text: str):
        """Once the user types their own path, the scan's reading no longer
        applies and the text is resolved on its own terms."""
        self._path_edited = True
        self._scan_item = None
        self._write_back()

    def _write_back(self, *_a) -> None:
        """Push this row's current state into the entry it is showing.

        A page of rows is destroyed when the user turns to the next one, so a
        name typed here, a path corrected here or a box unticked here survives
        only if it is recorded outside the widget. The dialog owns that record.
        """
        if getattr(self, "_building", False) or self._entry_index < 0:
            return
        sync = getattr(self._dialog, "_sync_row_to_entry", None)
        if callable(sync):
            sync(self)

    def _remove_self(self):
        # Recorded before the widget goes, or turning the page would bring it
        # straight back — the entry, not the row, is what "removed" belongs to.
        if self._entry_index >= 0:
            drop = getattr(self._dialog, "_mark_entry_removed", None)
            if callable(drop):
                drop(self._entry_index)
        # Hiding takes the row out of the layout just as well, and does not
        # leave it standing as a window of its own in the meantime.
        self.hide()
        self.deleteLater()

    def is_included(self) -> bool:
        """Every row still on screen is in the batch — the bin is what removes
        one. Kept as a method because callers ask the question by name."""
        return True

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

    def _open_folder(self):
        """Show this row's folder in the file manager.

        Falls back to the nearest parent that exists rather than refusing:
        a row can hold a path that is still being typed, or one predicted
        for a game that is not installed here, and landing next to where it
        WOULD be is more use than an error.
        """
        from ui.helpers import open_in_file_manager
        raw = (self._path_edit.text() or "").strip().strip('"')
        target = Path(raw) if raw else None
        while target is not None and not target.exists():
            parent = target.parent
            target = None if parent == target else parent
        if target is None:
            return
        open_in_file_manager(target)

    def _browse(self):
        from ui.widgets.file_pickers import pick_folder
        start = self._path_edit.text().strip()
        picked = pick_folder(self, t("manual_path.pick_folder"),
                             start_dir=start if Path(start).is_dir() else "")
        if picked:
            self._path_edit.setText(picked)

    def _revalidate(self):
        if getattr(self, "_building", False):
            return          # fields still being populated — see __init__
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




def _identity(chain: str, folder_title: str) -> str:
    """What makes an archive that archive, and nothing else.

    The destination its saves belong to plus the name it carries: a chain
    relative to the game, or the profile chain that resolves to an absolute
    path on whatever machine it lands on. Deliberately NOT the folder the
    zip was read from, and deliberately nothing about the files.

    The folder moves — a drive back under another letter, a collection
    reorganised — and the files churn, because an archive is detached from
    the library precisely so the user can go on playing out of it. They
    delete a save, start a new one, and by the afternoon nothing read off
    disk resembles what was archived. An identity built on either of those
    is an identity that stops matching for reasons that are not about
    identity at all.
    """
    clean = (chain or "").replace("\\", "/").strip("/").casefold()
    return clean + "|" + (folder_title or "").strip().casefold()


def _store_key(name, item, chain, raw, from_collection):
    """``(folder_title, chain, recorded, path_key, identity, origin)``.

    The store and the question asked before it have to agree on which folder
    an entry IS, down to the spelling. Deriving that twice, in two places,
    is how they stop agreeing.

    *recorded* / *path_key* is the destination label the index files it
    under, which restore reads. *identity* is what makes two entries the
    same archive, and *origin* is the folder actually in front of us — the
    pair the question is about.
    """
    folder_title = (name or "").strip() or (item.name or "").strip()
    if not (name or "").strip():
        folder_title = derive_folder_name(item.path) or folder_title
    folder_title = folder_title or Path(item.path).name
    chain = (chain or "").strip()
    if not chain and not from_collection:
        chain = live_save_chain(item.path)
    recorded = orphan_index_save_path(
        item.path, chain, folder_title, from_collection=bool(from_collection))
    # Dedup on the destination / zip-root label, not the archive origin (two
    # collections could share a source path spelling).
    return (folder_title, chain, recorded, (recorded or item.path).casefold(),
            _identity(chain, folder_title), (item.path or "").casefold())


def _archive_index(bm):
    """``(known_paths, orphan_owner, archives)`` — what identifies an archive.

    By LOCATION, so a folder already archived is recognised as itself, and by
    IDENTITY, so one reached from somewhere new can be asked about. Nothing
    is read off disk: every path here comes from the library and the backup
    index.

    One record per archive, not per backup. An archive with five backups in
    it used to appear five times, which was harmless while a machine was
    picking between them and is not once a person is.
    """
    from core.library import get_library
    known_paths = {p.casefold() for g in get_library().all_games()
                   for p in (g.save_paths or []) if p}
    orphan_owner: dict = {}
    by_id: dict = {}
    for b in bm.get_orphan_backups():
        locations = {p.casefold() for p in (b.save_paths or []) if p}
        origins = {p.casefold() for p in bm.orphan_source_paths(b) if p}
        for p in locations | origins:
            known_paths.add(p)
            orphan_owner.setdefault(p, b.game_id)
        rec = by_id.get(b.game_id)
        if rec is None:
            rec = by_id[b.game_id] = {
                "game_id": b.game_id, "name": "", "identity": "",
                "locations": set(), "origins": set(), "entry": b,
            }
        rec["locations"] |= locations
        rec["origins"] |= origins
        if b.created_dt >= rec["entry"].created_dt:
            rec["entry"] = b
            rec["name"] = (b.game_name or "").strip().casefold()
            rec["identity"] = _identity(
                next((c for c in (b.save_chains or []) if c), "")
                or next((c for c in (b.content_chains or []) if c), ""),
                b.game_name or "")
    return known_paths, orphan_owner, list(by_id.values())


def drop_already_archived(found: list) -> tuple:
    """Split collected folders into ``(new, already_archived)``.

    The counterpart of core.exe_scan.drop_already_in_library, which is what
    the bulk Add Game list uses so it never shows a game you already have.
    This list had no equivalent: every folder in the collection was listed,
    already-saved ones included, and the only thing that noticed was the
    store — which quietly re-backed them up instead of adding a duplicate.
    Correct, but invisible, so a long list gave the user no way to tell what
    was actually new.

    The same key the store deduplicates on (_store_key's destination label),
    so what is hidden here is exactly what would have been folded in there.
    """
    try:
        from core.backup import get_backup_manager
        known_paths, _owner, _archives = _archive_index(get_backup_manager())
    except Exception:
        logger.debug("could not read the archive index", exc_info=True)
        return list(found), []
    if not known_paths:
        return list(found), []
    new, present = [], []
    for c in found:
        try:
            _title, _chain, _recorded, path_key, _ident, _origin = _store_key(
                c.name, c.item, c.chain, c.source, True)
        except Exception:
            new.append(c)
            continue
        (present if path_key in known_paths else new).append(c)
    return new, present


class _StoreWorker(QThread):
    """Match + mtime checks off the GUI thread for large batches."""
    progress = Signal(int, int, str)
    # (added, updated, skipped, entries, cancelled, reindexed). Read by
    # INDEX at the other end, never unpacked into names: this tuple has
    # grown twice, and the last time a handler unpacked a fixed count out
    # of it the whole run finished, wrote its archives, and then raised
    # before the summary — which read as "it just hangs".
    finished_ok = Signal(object)

    def __init__(self, pending: list, decisions: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self._pending = pending
        # path key -> the archive the user said this folder belongs to.
        # Absent means "its own archive": a merge is the one answer of the
        # two the user cannot undo afterwards, so it is never the default.
        self._decisions = dict(decisions or {})
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        # Set from inside run(): setPriority only applies to a RUNNING
        # thread. From __init__ it did nothing but log "Cannot set
        # priority, thread is not running", so these scans never
        # actually ran at idle priority — which is the one thing the
        # call was there to do while a game has the CPU.
        self.setPriority(QThread.Priority.IdlePriority)
        from core.backup import get_backup_manager

        # Where every archive and every library game already lives. Which
        # folder is which is settled here; whether two folders sharing a
        # TITLE are one archive was settled before this thread started, by
        # asking — see ui/dialogs/archive_choice_dialog.py.
        bm = get_backup_manager()
        known_paths, orphan_owner, _archives = _archive_index(bm)

        added = updated = skipped = reindexed = 0
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
                (folder_title, chain, recorded,
                 path_key, ident, origin) = _store_key(
                    name, item, chain, raw, from_collection)
                # Same archive, new origin. That was put to the user before
                # this batch started — see archive_choice_dialog — and
                # nothing here second-guesses the answer.
                #
                # "" is a real answer, meaning "a different game": it has to
                # override the label match below, because a folder out of a
                # collection files itself under its own NAME and that label
                # is exactly the thing the two of them share.
                answer = self._decisions.get(origin)
                relocate_to = None
                if answer:
                    logger.info(
                        "Storing %s into archive %s — the user said they are "
                        "the same saves", item.path, answer)
                    relocate_to = answer
                    known_paths.add(path_key)
                    orphan_owner[path_key] = answer
                if answer != "" and path_key in known_paths:
                    # Already archived. Re-adding the same folder is almost
                    # always "here are this folder's newer saves", not "make
                    # me a second archive of it" — and a second archive is
                    # what it used to do, under a name disambiguated from the
                    # first, so the user ended up with two entries for one
                    # folder and retention pruning neither.
                    #
                    # rebackup_archive is the function built for this: it
                    # reuses the existing game_id and backup folder, and it
                    # runs create_backup WITHOUT force, so a folder whose
                    # contents have not changed since last time writes no new
                    # zip at all and simply reports "unchanged". That is the
                    # "is anything actually different?" check that was
                    # missing — reloading a backup that has not moved on now
                    # costs nothing and adds nothing.
                    #
                    # Only OUR archives. A path belonging to a real library
                    # game is that game's business; it is reported as skipped
                    # exactly as before.
                    owner = relocate_to or orphan_owner.get(path_key)
                    if not owner:
                        skipped += 1
                        continue
                    # Whether or not anything has changed, this re-add says
                    # where the folder IS — one of the places, added to the
                    # ones already recorded rather than replacing them. A
                    # game keeps its saves in more than one, and an archive
                    # from before origins were recorded has none at all,
                    # which is what leaves its recurring check greyed out
                    # with nothing the user can do about it.
                    wrote_origin = False
                    try:
                        # The folder, and — separately — where its files go
                        # back. The two are only the same when the user
                        # handed over a live save folder; a copy on a shelf
                        # rebuilds somewhere else entirely, and neither of
                        # them is the archive's stripped title.
                        wrote_origin = bm.add_orphan_sources(
                            owner, [item.path],
                            dest_map={item.path: {
                                "path": recorded or item.path,
                                "chain": chain or "",
                                "content": chain or "",
                            }})
                    except Exception:
                        logger.debug("could not record archive origin", exc_info=True)
                    try:
                        # Re-pointed only when the match was made on CONTENT:
                        # a match on the path is already looking at the right
                        # folder, and overriding it there would rewrite a
                        # perfectly good record for no reason.
                        did, detail = bm.rebackup_archive(
                            owner,
                            sources=[item.path] if relocate_to else None,
                            recorded=[recorded] if (relocate_to and recorded) else None)
                    except Exception:
                        logger.exception(
                            "Archive refresh failed for %s", item.path)
                        skipped += 1
                        continue
                    if not did:
                        # "unchanged" is the good outcome here, not a failure.
                        logger.info("Archive %s not rewritten: %s", owner, detail)
                        # But the archive's own index sits under each backup,
                        # and a folder named for the first time changes it.
                        # Reporting that as "skipped" said nothing happened
                        # to an archive that had just been told where its
                        # saves live — which is the whole reason for the run.
                        if wrote_origin:
                            reindexed += 1
                        else:
                            skipped += 1
                        continue
                    updated += 1
                    fresh = bm.get_backup(detail)
                    if fresh is not None:
                        entries.append(fresh)
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
        self.finished_ok.emit(
            (added, updated, skipped, entries, self._stop, reindexed))


class ManualPathDialog(WindowedListMixin, QDialog):
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

        # Sized for the LIST, not for an empty dialog. This measures at build
        # time, when no rows exist, so the floor IS the answer it lands on —
        # and 420px left a viewport of 163px against a 75px row, i.e. two
        # titles on screen at once out of however many were found. The floor
        # is what has to carry a useful number of rows; the caps in
        # apply_adaptive_dialog_size still keep it inside the work area on a
        # small display.
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=720, min_h=660, scroll=self._scroll, list_content=True)
        # Centred once the size is settled — a panel this large opening
        # in a corner, or with its title bar off the top of the screen,
        # is what Qt does when nothing tells it otherwise.
        center_dialog(self)

    # ── Selection ────────────────────────────────────────────────────────────

    def _add_single(self):
        from ui.widgets.file_pickers import pick_folder
        picked = pick_folder(self, t("manual_path.pick_folder"))
        if not picked:
            return
        # Straight into the same master list a collection scan fills, not a
        # loose row beside it. The list on screen is a WINDOW onto that list
        # — rows outside the visible span do not exist — so a row that is not
        # backed by an entry would vanish the next time the user scrolled,
        # and would not be in the batch at commit either.
        #
        # Live game save folder: walk UP for the destination chain
        # (AppData/Roaming/RenPy/… or www/save), not save_chain_of, which
        # descends inside a collection copy.
        chain = live_save_chain(picked)
        item = resolve_manual_path(picked)
        self._found_serialized.append({
            "source": "",
            "name": derive_folder_name(picked) or Path(picked).name,
            "chain": chain,
            "item": {"raw": item.raw, "kind": item.kind, "path": item.path,
                     "name": item.name, "exists": item.exists},
        })
        self._collection_hint.setVisible(False)
        self._render_list()
        self._status.setText("")
        self._persist_state_soon()

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
        # Folders already covered by an archive (or by a library game) are
        # not news. Hidden rather than listed, exactly as the bulk Add Game
        # list hides games already in the library — and counted, so "42
        # folders, 13 already saved" is visible instead of 55 rows the user
        # has to tell apart by eye.
        found, already = drop_already_archived(found)
        self._already_archived = len(already)
        if already:
            logger.info("Collection scan: %d folder(s) already archived",
                        len(already))
        if not found:
            self._set_idle("")
            information_window_modal(
                self, t("manual_path.title"),
                t("manual_path.all_known", count=len(already)))
            return
        self._found_serialized = [self._serialize_collected(c) for c in found]
        # No chunk pump any more: only a page is built, and a page is small
        # enough to appear at once. What used to be inserted here folder by
        # folder — hundreds of rows, over seconds — is now the master list,
        # and the rows come and go with the page.
        self._insert_index = len(found)
        self._progress.setVisible(False)
        self._render_list()
        self._phase = "ready"
        self._multi_btn.setEnabled(True)
        self._single_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        _known = getattr(self, "_already_archived", 0)
        if _known:
            msg = t("manual_path.found_with_known",
                    total=len(found) + _known, known=_known, count=len(found))
        else:
            msg = t("manual_path.multiple_added", count=len(found))
        unresolved = sum(1 for c in found if not c.item.backupable)
        if unresolved:
            msg += "  " + t("manual_path.multiple_unresolved", count=unresolved)
        self._status.setText(msg)
        self._collection_hint.setVisible(True)
        self._persist_state_now()
        if not self.isVisible():
            self.shelve_status.emit()

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A taller viewport shows more rows, so the built window has to grow
        # with it — see WindowedListMixin._wl_update.
        self._wl_update()

    def _set_idle(self, status: str = ""):
        self._phase = "idle"
        self._cancel_op = False
        self._progress.setVisible(False)
        self._multi_btn.setEnabled(True)
        self._single_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        # Back to a button that starts work — _mark_finished may have turned
        # it into the spent "Done" one, and leaving that label on an enabled
        # button would invite a press that means the opposite of what it says.
        self._save_btn.setText(t("manual_path.save"))
        if status:
            self._status.setText(status)
        # The sidebar has to hear about this. Coming to rest — cancelled,
        # or finished — leaves a put-away entry blinking "running" with a
        # progress bar frozen where the work stopped, which reads as still
        # in flight and is the opposite of what just happened.
        try:
            self.shelve_status.emit()
        except RuntimeError:
            pass

    # ── The master list, and the page showing part of it ────────────────────

    def _sync_row_to_entry(self, row) -> None:
        """Record a row's current state on the entry it is showing."""
        i = getattr(row, "_entry_index", -1)
        if not (0 <= i < len(self._found_serialized)):
            return
        entry = self._found_serialized[i]
        try:
            entry["name"] = row._name_edit.text()
            typed = row._path_edit.text()
            if row._path_edited:
                # The scan's own reading no longer applies once the path is
                # typed over, so the entry stops claiming one too.
                entry["item"] = dict(entry.get("item") or {},
                                     raw=typed, path=typed)
                entry["path_edited"] = True
        except RuntimeError:
            pass

    def _mark_entry_removed(self, index: int) -> None:
        """The ✕ on a row takes its entry out of the batch for good."""
        if 0 <= index < len(self._found_serialized):
            self._found_serialized[index]["removed"] = True
            self._persist_state_soon()

    def _kept_entries(self) -> list:
        """(index, entry) for everything still in the batch, in order."""
        return [(i, e) for i, e in enumerate(self._found_serialized)
                if not e.get("removed")]

    def _sync_visible_rows(self) -> None:
        """Flush every built row into the master list."""
        self._wl_sync_visible()

    def release_batch(self) -> None:
        """Drop the batch and every widget showing it.

        A collection of several hundred folders is a large object graph — the
        scan results, the serialised copies, and a row of widgets each — and
        the dialog outlives its own usefulness: it is kept for shelving, so
        without this it went on holding all of it until the app closed. Once
        the batch is stored, or the user walks away from it, none of it
        answers any question any more.
        """
        self._wl_clear()
        self._clear_rows()
        self._found_serialized = []
        self._already_archived = 0
        self._insert_index = 0
        self._collection_root = ""
        self._pending_for_store = []
        self._pending_unresolved = []
        self._empty_lbl.setVisible(True)
        self._collection_hint.setVisible(False)

    def _clear_rows(self) -> None:
        """Empty the list. The holder goes with it — see _wl_render, which
        builds a new one rather than reusing this."""
        self._rows = []
        holder = QWidget(self._scroll)
        holder.setObjectName("transparent_bg")
        from PySide6.QtWidgets import QVBoxLayout as _VBox
        layout = _VBox(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._empty_lbl.setParent(holder)
        layout.addWidget(self._empty_lbl)
        self._empty_lbl.setVisible(True)
        layout.addStretch()
        self._rows_layout = layout
        # Never leaves the previous holder unparented — see swap_scroll_widget.
        from ui.helpers import swap_scroll_widget
        swap_scroll_widget(self._scroll, holder)

    # ── The windowed list (see ui.widgets.windowed_list) ────────────────────

    def _render_list(self) -> None:
        """Show the list. Only the rows in view are built."""
        self._wl_render(self._scroll, self._empty_lbl)

    def _wl_entries(self) -> list:
        return self._kept_entries()

    def _wl_sync_row(self, row) -> None:
        self._sync_row_to_entry(row)

    def _wl_row_height(self) -> int:
        """The TALLEST shape a row can take, measured once.

        Every row in the window is clamped to this one number, so it has to
        be the largest of them and not the smallest. A folder read out of a
        collection carries an extra line naming where it came from, and
        measuring a blank probe cut that line — and the bottom of the path
        field under it — off every such row: they could be read but not
        edited, which is exactly the folders a collection scan produces.
        """
        h = getattr(self, "_row_h", 0)
        if not h:
            # index=-1 on BOTH: a probe is a ruler, not a view of an entry,
            # and one claiming to be entry 0 is one bad connect away from
            # writing a measurement into the user's first folder.
            for kw in ({"name": "", "index": -1},
                       {"name": "x", "index": -1, "source": "x", "chain": "x"}):
                probe = _ManualPathRow("x", parent=self, **kw)
                # sizeHint alone can come back short for a widget that has
                # never been laid out; minimumSizeHint is what the layout
                # will actually refuse to go below, and a row clamped under
                # THAT is a row whose QLineEdits get squeezed out of reach.
                h = max(h, probe.sizeHint().height(),
                        probe.minimumSizeHint().height())
                probe.setParent(None)
                probe.deleteLater()
            self._row_h = max(1, h)
        # self._row_h, not the local: they differ whenever the probes
        # measured nothing (h == 0), and returning the 0 handed every row
        # setFixedHeight(0) — built, counted, and invisible.
        return self._row_h

    def _wl_make_row(self, key, entry):
        collected = self._deserialize_collected(entry)
        return _ManualPathRow(
            collected.item.path or collected.chain or collected.source,
            parent=self, name=collected.name, source=collected.source,
            chain=collected.chain, item=collected.item, index=key)

    # How many extra rows to keep built above and below the viewport, so a
    # scroll does not arrive somewhere blank before the rebuild catches up.
    _WINDOW_BUFFER = 6

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

    def _collision_decisions(self, pending):
        """Ask about every folder reached from somewhere new.

        ``{origin path: archive game_id}`` for the ones the user said are the
        same saves, and ``{origin path: ""}`` for the ones they kept apart —
        both are answers, and the second has to be recorded because a folder
        out of a collection is filed under its own name, which is precisely
        the label the two of them would otherwise share.

        ``None`` when the user backed out, and then nothing at all is stored:
        this runs before the first zip is written exactly so that answer
        costs nothing.

        A folder the archive already knows — same identity, same origin — is
        not a question and is never asked about. That is the ordinary reload
        of a collection, and it is the whole batch when nothing has moved.
        """
        from core.backup import get_backup_manager
        from ui.dialogs.archive_choice_dialog import (CANCEL, UPDATE,
                                                      ArchiveChoiceDialog,
                                                      archive_card)
        bm = get_backup_manager()
        known, _owner, archives = _archive_index(bm)
        by_identity: dict = {}
        for a in archives:
            if a["identity"].strip("|"):
                by_identity.setdefault(a["identity"], []).append(a)
        if not by_identity:
            return {}

        decisions: dict = {}
        blanket = None
        for row in pending:
            name, item, chain, raw, from_collection = row[:5]
            if not item.path or not item.exists:
                continue
            (title, _c, _r, path_key, ident, origin) = _store_key(
                name, item, chain, raw, from_collection)
            # Somewhere already accounted for is never a question: an
            # archive's own folder, or a save path belonging to a real
            # library game. The second is not hypothetical — a library game
            # whose title matches an archive would be asked about and then
            # skipped by the store regardless, which is a question with no
            # consequence attached to either answer.
            if path_key in known or origin in known:
                continue
            same = by_identity.get(ident)
            if not same:
                continue
            # More than one archive under one identity is the user's own
            # doing — they answered "keep both" before. The newest is the one
            # offered; keeping them apart again is always the other button.
            arch = max(same, key=lambda a: a["entry"].created_dt)
            if blanket is not None:
                decisions[origin] = arch["game_id"] if blanket == UPDATE else ""
                continue
            dlg = ArchiveChoiceDialog(title, item.path,
                                      archive_card(arch["entry"], bm),
                                      parent=self)
            dlg.exec()
            answer = dlg.choice()
            if answer == CANCEL:
                return None
            decisions[origin] = arch["game_id"] if answer == UPDATE else ""
            if dlg.applies_to_all():
                blanket = answer
        return decisions

    def _commit(self):
        if self._store_worker is not None and self._store_worker.isRunning():
            return
        # Everything on screen goes into the master list before anything is
        # read out of it: only the rows in VIEW exist as widgets, so they are
        # never the whole batch and must never be treated as it. This is the
        # difference between storing 570 folders and silently storing twenty.
        self._sync_visible_rows()
        pending, unresolved = self._pending_from_entries()

        if not pending:
            self._status.setText(t("manual_path.none_resolved"))
            return

        # Same-name folders are settled with the user BEFORE anything is
        # written, so backing out here leaves nothing half-done.
        decisions = self._collision_decisions(pending)
        if decisions is None:
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
        self._store_worker = _StoreWorker(pending, decisions, parent=self)
        self._store_worker.progress.connect(self._on_store_step)
        self._store_worker.finished_ok.connect(self._on_store_done)
        self._store_worker.start()

    def _pending_from_entries(self):
        """The batch, read from the master list rather than from widgets."""
        pending, unresolved = [], []
        for _i, entry in self._kept_entries():
            if entry.get("included") is False:
                continue
            collected = self._deserialize_collected(entry)
            item = collected.item
            if entry.get("path_edited") and item.path:
                item = resolve_manual_path(item.path)
            if not item.path:
                unresolved.append(collected.name or item.raw)
                continue
            from_collection = bool(collected.source)
            chain = collected.chain or (save_chain_of(item.path)
                                        if from_collection
                                        else live_save_chain(item.path))
            raw = (Path(collected.source).name if collected.source
                   else (Path(item.path).name if item.path else ""))
            name = (collected.name or "").strip() or derive_folder_name(item.path)
            pending.append((name, item, chain, raw, from_collection))
        return pending, unresolved

    def _on_store_step(self, current: int, total: int, name: str):
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(current)
        self._step = (current, total, name)
        self._status.setText(t("manual_path.storing",
                               current=current, total=total, name=name))
        if not self.isVisible():
            self.shelve_status.emit()

    def _on_store_done(self, payload):
        added, updated, skipped = payload[0], payload[1], payload[2]
        entries, cancelled = payload[3], payload[4]
        reindexed = payload[5] if len(payload) > 5 else 0
        self._store_worker = None
        # Archives that were already written stay reported even on a cancel.
        # They exist on disk either way, and dropping them here meant a
        # cancelled batch left backups the Backups page was never told about.
        self.added_entries.extend(entries or [])
        if cancelled:
            self._set_idle(t("manual_path.cancelled"))
            self._clear_persisted()
            return
        parts = []
        if added:
            parts.append(t("manual_path.result_added", count=added))
        if updated:
            parts.append(t("manual_path.result_updated", count=updated))
        if reindexed:
            parts.append(t("manual_path.result_reindexed", count=reindexed))
        if skipped:
            parts.append(t("manual_path.result_skipped", count=skipped))
        pending = getattr(self, "_pending_for_store", []) or []
        # Index, not unpacking. These rows carry FIVE fields (the fifth,
        # from_collection, was added later) and unpacking four names out of
        # them raised ValueError right here — after the archives had been
        # written and after added_entries was filled, but before
        # _clear_persisted, the summary and accept(). So a completed run
        # showed no summary, never closed, never told the Backups page
        # anything (which is what _on_manual_path_finished hangs off
        # accept()), left the batch marked as still pending, and left the
        # buttons disabled mid-run with Cancel unable to resolve — the whole
        # "it just hangs after loading" report, from one arity mismatch.
        missing = sum(1 for row in pending if not row[1].exists)
        if missing:
            parts.append(t("manual_path.result_not_yet", count=missing))
        unresolved = getattr(self, "_pending_unresolved", []) or []
        if unresolved:
            parts.append(t("manual_path.result_unresolved",
                           count=len(unresolved), names=", ".join(unresolved[:5])))
        self._clear_persisted()
        self._force_close = True
        self._mark_finished()
        summary = "\n".join(parts) or ""
        if not self.isVisible():
            # Put away in the sidebar. That is the user saying “tell me
            # later”, so a modal summary arriving over whatever they moved
            # on to is exactly what they asked not to happen — and it
            # closes this dialog behind it, which refreshes the Backups
            # page under their hands. It waits on the sidebar entry, which
            # now reads done, until they come back for it.
            # The batch is NOT dropped here. Clicking the notification has
            # to bring up this panel with its entries AND the confirmation
            # over it — a summary floating over an empty list says what
            # happened to nothing you can see.
            self._pending_summary = summary
            self.shelve_status.emit()
            return
        self.release_batch()
        information_window_modal(self, t("manual_path.title"), summary)
        self.accept()

    def _mark_finished(self):
        """Turn the primary button into a spent "Done" before the summary.

        The run is over and there is nothing left to add, so the button that
        starts it must not still be sitting there looking pressable. It stays
        disabled and says so.
        """
        try:
            self._phase = "idle"
            self._progress.setVisible(False)
            self._save_btn.setEnabled(False)
            self._save_btn.setText(t("manual_path.done"))
        except RuntimeError:
            pass

    def _on_cancel_clicked(self):
        """Annulla ferma l'operazione in corso; altrimenti chiude il dialog."""
        if self.has_shelvable_work():
            self._cancel_op = True
            scan_running = self._worker is not None and self._worker.isRunning()
            store_running = (self._store_worker is not None
                             and self._store_worker.isRunning())
            if scan_running:
                self._worker.stop()
            if store_running:
                self._store_worker.stop()
            if self._phase == "inserting":
                self._set_idle(t("manual_path.cancelled"))
                self._clear_persisted()
                return
            if not (scan_running or store_running):
                # Nothing is actually running, so no finished-signal is
                # coming to clear this up. "Annullamento…" was a message
                # waiting on a worker that had already gone, and it stayed on
                # screen for good — with the phase still claiming work in
                # flight, so the next press did the same thing again.
                self._set_idle(t("manual_path.cancelled"))
                self._clear_persisted()
                return
            self._status.setText(t("manual_path.cancelling"))
            return
        self._force_close = True
        self._clear_persisted()
        self.release_batch()
        self.reject()

    def has_shelvable_work(self) -> bool:
        return self._phase in ("scanning", "inserting", "storing")

    def shelve_progress(self) -> tuple:
        """``(done, total, name)`` for the sidebar, or ``(0, 0, "")``.

        Work put away is work the user cannot see, so the one thing the
        sidebar owes them is how far it has got. A blinking dot said
        something was happening and nothing more, which for a batch of
        several hundred folders is the same as saying nothing.
        """
        if self._phase == "storing":
            return getattr(self, "_step", (0, 0, ""))
        if self._phase in ("scanning", "inserting"):
            return (self._insert_index, len(self._found_serialized), "")
        return (0, 0, "")

    def shelve_nav_label(self) -> str:
        done, total, name = self.shelve_progress()
        if total:
            return t("common.progress_label",
                     label=name or t("manual_path.shelved_nav"),
                     done=done, total=total)
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
        pending = getattr(self, "_pending_summary", None)
        if pending is not None:
            # It finished while this was put away. The list is drawn FIRST
            # so the confirmation lands on top of the folders it is talking
            # about, rather than over the empty panel a batch that finished
            # out of sight would otherwise leave behind.
            self._pending_summary = None
            self._render_list()
            information_window_modal(self, t("manual_path.title"), pending)
            self.release_batch()
            self.accept()
            return
        # Draw the list again: nothing was built while this was hidden.
        #
        # This used to hand off to a chunked row-inserter, which the windowed
        # list replaced and which no longer EXISTS — so coming back from the
        # sidebar raised AttributeError here, before a single row was drawn,
        # and left the panel stuck mid-"inserting" with its buttons disabled.
        # There is nothing to insert in chunks any more: only the rows on
        # screen are ever built, so drawing them is the whole job.
        if self._found_serialized and not self._rows:
            if self._phase == "inserting":
                self._set_idle()
                self._status.setText(
                    t("manual_path.multiple_added",
                      count=len(self._found_serialized)))
            self._render_list()

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
        # Whatever route brought us here, the batch is finished with. Shelving
        # takes its own path out (it ignores closeEvent above), so this only
        # ever runs when the dialog is genuinely done.
        self.release_batch()
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
        if not self._found_serialized:
            return
        found = [self._deserialize_collected(d) for d in self._found_serialized]
        self._insert_index = max(0, min(self._insert_index, len(found)))
        # Do not sync-build hundreds of row widgets here — that froze resume.
        # Shelve first; rows materialize on unshelve / while inserting hidden.
        self._collection_hint.setVisible(True)
        # There is no half-finished insert to resume any more. The batch IS
        # the entries, and the list draws only the rows on screen — so a
        # scan interrupted by a restart comes back complete and ready, not
        # part-way through building several hundred widgets.
        #
        # It used to resume into an "inserting" phase driven by a chunked
        # inserter that the windowed list replaced. That method is gone, so
        # this raised AttributeError with the buttons already disabled: the
        # panel came back with no rows and nothing that could be pressed.
        self._insert_index = len(found)
        self._phase = "ready"
        self._status.setText(t("manual_path.multiple_added", count=len(found)))
        self._render_list()
