"""SaveSync — the save editor page.

Three steps, one at a time, with a way back from each:

1. pick a game (the library's own search, ghost hint and all);
2. pick one of that game's save files — with the copies SaveSync has kept
   of it, newest first, each restorable;
3. edit the values inside it.

Editing is done on files at rest. Nothing attaches to a running game and
nothing is written into one — see core/save_editor for why that boundary is
where it is.
"""
import logging
import re
from pathlib import Path

from typing import Optional
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from core.library import get_library
from core.save_editor import (SaveEditorError, delete_backup, describe,
                              explain, list_backups, open_save,
                              prune_backups, restore_backup)
from i18n import t
from ui.helpers import ElidedLabel, PageScrollMixin, scaled
from ui.modal_helpers import warning_window_modal
from ui.styles.theme import palette, ThemedMixin
from ui.widgets.search_inputs import ClearableLineEdit, GhostClearableLineEdit
from ui.widgets.page_size import (PageSizeCombo, SCOPE_CHEATS_GAMES,
                                  SCOPE_CHEATS_SAVES, guarded_render,
                                  page_size)

logger = logging.getLogger(__name__)

# A save folder is walked this deep looking for files. Saves live in the
# folder or a slot subfolder; deeper than this and we are reading the game.
_SCAN_DEPTH = 3
# Files visited under ONE save path, and rows kept once they are all in.
# Two budgets rather than one: a game can have several save paths, and a
# single allowance let the first of them use the lot.
_MAX_PER_PATH = 400
_MAX_FILES = 600
# Subfolders under a save root that are never saves (logs, caches, crash
# dumps). Backup's own skip list is narrower — a confirmed archive may still
# want odd paths — but the editor list must not offer engine noise.
_EDITOR_SKIP_DIRS = frozenset({
    "cache", "caches", "log", "logs", "temp", "tmp", "crash", "crashes",
})
# Values shown on one page of the editor. The filter and the pager together
# are how the rest is reached, so nothing is ever hidden — only paged.
_PAGE_SIZE = 40

# Groups that have a name in plain words. Some formats already use these
# words — RPG Maker 2000/2003 calls its groups "switches" and "variables" —
# while RGSS writes its own class names into the save, which are accurate but
# not what a player calls them, so those are aliased onto the same words.
# Anything unlisted keeps the name the file gave it, which is the right answer
# for every other engine.
_GROUP_KEYS = frozenset({
    "switches", "variables", "self_switches", "party", "actors", "system",
    "screen", "troop", "map", "player", "inventory",
})
_GROUP_ALIASES = {
    "Game_Switches": "switches",
    "Game_Variables": "variables",
    "Game_SelfSwitches": "self_switches",
    "Game_Party": "party",
    "Game_Actors": "actors",
    "Game_System": "system",
    "Game_Screen": "screen",
    "Game_Troop": "troop",
    "Game_Map": "map",
    "Game_Player": "player",
}


# Where one name ends and the next begins, in a label. Formats join their
# parts differently — a dot for Ruby's ivars, a slash for Wolf's database —
# and both are places a shared prefix may be cut.
_SEPARATOR = re.compile(r"(\s*[./]\s*)")


def _group_label(group: str) -> str:
    key = _GROUP_ALIASES.get(group, group)
    return t(f"cheats.groups.{key}") if key in _GROUP_KEYS else group


def _short_labels(paths: list) -> dict:
    """A short name per folder that still tells them apart.

    A game's two save folders are very often both called the same thing —
    "SaveData" beside the game and "SaveData" under the user's profile — so
    naming them by their last part alone offers two identical choices. As
    much of the path is used as it takes to make every name different, and
    no more.
    """
    out = {}
    for depth in range(1, 6):
        # Rebuilt with Path rather than joined by hand: the first part of a
        # Windows path is the drive WITH its separator, and pasting one on
        # gives "C:\\\folder".
        out = {p: (str(Path(*Path(p).parts[-depth:])) if Path(p).parts else p)
               for p in paths}
        if len(set(out.values())) == len(paths):
            break
    return out


def _by_folder(files: list, when) -> list:
    """The same files, one folder at a time, each folder's newest first.

    A save path can hold folders of its own — a profile per player, a slot
    per character — and ordering everything under it by date alone shuffles
    them together exactly as mixing two save paths does. The folders come in
    order of the newest thing in them, so the one last written to is still
    the one at the top.
    """
    folders = {}
    for f in files:
        folders.setdefault(str(f.parent), []).append(f)
    for group in folders.values():
        group.sort(key=lambda f: (-when(f), f.name.lower()))
    order = sorted(folders.values(), key=lambda g: -when(g[0]))
    return [f for group in order for f in group]


def _save_files(entry) -> list:
    """Every file under a game's save paths: one path at a time, newest first.

    Each path is walked on a budget of its own. A game often has more than
    one — Ren'Py keeps a copy beside the game and another under the user's
    profile — and with a single shared allowance the first path could use it
    all and leave the second showing nothing.

    **The paths are kept apart.** A game with two save folders usually has
    the same file names in both, and ordering the whole lot by date alone
    interleaves them: the same six names twice over, in no order anyone can
    follow. Each path's files are listed together instead, in the order the
    paths themselves are recorded, so picking a save means picking a folder
    and then a save in it.

    Within a path the newest comes first, and files written in the same
    second — which a game saving several at once produces constantly — are
    put in name order rather than in whatever order the folder happened to
    hand them over.

    The cap on how many are shown is shared out between the paths rather
    than spent in order. Grouping otherwise lets the first folder eat the
    whole allowance and leave a later one showing nothing at all — which the
    old date-ordered list never did, since it drew the newest from wherever
    they were. Whatever a path does not use goes back to the others, so a
    game with one folder is capped exactly as before.
    """
    from core.backup import _BACKUP_SKIP_DIRS, _is_skip_file
    from core.registry_saves import is_registry_path, registry_has_values

    skip_dirs = _BACKUP_SKIP_DIRS | _EDITOR_SKIP_DIRS

    def when(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    def walk(base: Path, depth: int, budget: list, found: list):
        if depth > _SCAN_DEPTH or budget[0] <= 0:
            return
        try:
            for child in sorted(base.iterdir()):
                if budget[0] <= 0:
                    return
                if child.is_dir():
                    if child.name.lower() in skip_dirs:
                        continue
                    walk(child, depth + 1, budget, found)
                elif child.is_file():
                    # Same noise rules as backup: .log / .cache / stem "log", …
                    if _is_skip_file(child):
                        continue
                    found.append(child)
                    budget[0] -= 1
        except OSError:
            return

    groups, seen = [], set()
    for raw in (entry.save_paths or []):
        # A Unity game's save is often not a file: PlayerPrefs live in the
        # registry, and SaveSync already records those as save paths. They
        # are offered here like any other save — open_save knows the
        # difference — but only when the key actually holds something.
        if is_registry_path(str(raw)):
            if registry_has_values(str(raw)):
                found = [Path(str(raw))]
            else:
                continue
        else:
            p = Path(raw)
            if p.is_file():
                if _is_skip_file(p):
                    continue
                found = [p]
            elif p.is_dir():
                found = []
                walk(p, 0, [_MAX_PER_PATH], found)
            else:
                continue
        found = _by_folder(found, when)
        kept = []
        for f in found:
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                kept.append(f)
        if kept:
            groups.append(kept)

    if not groups:
        return []
    # An equal share each, then round after round of one more apiece for
    # whoever still has files, until the allowance runs out. A path with
    # little in it simply stops asking, and what it did not take is there
    # for the others.
    share = max(1, _MAX_FILES // len(groups))
    taken = [min(share, len(g)) for g in groups]
    spare = _MAX_FILES - sum(taken)
    while spare > 0 and any(t < len(g) for t, g in zip(taken, groups)):
        for i, g in enumerate(groups):
            if spare <= 0:
                break
            if taken[i] < len(g):
                taken[i] += 1
                spare -= 1
    out = []
    for n, g in zip(taken, groups):
        out.extend(g[:n])
    return out


class _Row(QFrame, ThemedMixin):
    """A clickable line — a game, a save file, a kept copy."""

    clicked = Signal()

    def __init__(self, title: str, detail: str = "", where: str = "",
                 engine: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("cheats_row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        self._title = ElidedLabel(title, own_tooltip=False)
        self._title.setObjectName("cheats_row_title")
        # Maximum: sit at the name's natural width so the engine label
        # follows the text, not the far edge next to the paths column.
        # (Ignored + stretch 1 shoved "· Engine" against the detail.)
        self._title.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        title_row.addWidget(self._title, 0)
        if engine:
            eng = QLabel(f"· {engine}")
            eng.setObjectName("cheats_row_engine")
            eng.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            title_row.addWidget(eng, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        titles.addLayout(title_row)
        # Where the file is, under the name. Only when it is needed to tell
        # two rows apart: a game with several save paths has the same save
        # names in each of them, and without this the list reads as the same
        # file repeated rather than as one file per folder.
        if where:
            self._where = ElidedLabel(where)
            self._where.setObjectName("cheats_row_where")
            titles.addWidget(self._where)
        row.addLayout(titles, 1)
        self._detail = QLabel(detail)
        self._detail.setObjectName("cheats_row_detail")
        row.addWidget(self._detail)
        # Tip on the whole row: the title label is only as wide as its text
        # (Maximum), so hovering the stretch / engine / detail would miss it.
        self.setToolTip(title)

    def add_button(self, text: str, handler) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("cheats_row_btn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(handler)
        self.layout().addWidget(btn)
        return btn

    def add_icon_button(self, icon: str, tooltip: str, handler) -> QPushButton:
        btn = QPushButton(icon)
        btn.setObjectName("icon_btn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(scaled(30, self), scaled(28, self))
        btn.setToolTip(tooltip)
        btn.clicked.connect(handler)
        self.layout().addWidget(btn)
        return btn

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _DropZone(QFrame, ThemedMixin):
    """Somewhere to drop a save file, or click to go and find one.

    A game does not have to be in the library to have its save edited. Plenty
    are not worth adding — a RAGS game needs a pile of scripts around it just
    to start — and the save is the only part anyone wants anyway.
    """

    chosen = Signal(str)          # a file was dropped
    browse = Signal()             # the zone was clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cheats_drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(scaled(58, self))
        self._hot = False
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 14, 8)
        self._label = QLabel(t("cheats.drop_hint"))
        self._label.setObjectName("cheats_drop_label")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._label, 1)
        self._sty(self, self._look)

    def _look(self) -> str:
        edge = palette("accent") if self._hot else palette("border")
        fill = palette("bg_input") if self._hot else "transparent"
        return (f"QFrame#cheats_drop{{border:1px dashed {edge};border-radius:6px;"
                f"background:{fill};}}"
                f"QLabel#cheats_drop_label{{color:{palette('text_muted')};"
                f"font-size:{scaled(11, self)}px;background:transparent;border:none;}}")

    def retranslate(self):
        self._label.setText(t("cheats.drop_hint"))

    # ── the gestures ─────────────────────────────────────────────────────────

    @staticmethod
    def _first_file(mime) -> str:
        for url in mime.urls() if mime.hasUrls() else ():
            local = url.toLocalFile()
            if local and Path(local).is_file():
                return local
        return ""

    def _glow(self, on: bool) -> None:
        if self._hot != on:
            self._hot = on
            self._sty(self, self._look)

    def dragEnterEvent(self, event):
        if self._first_file(event.mimeData()):
            self._glow(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._glow(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._glow(False)
        path = self._first_file(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.chosen.emit(path)
        else:
            event.ignore()

    def mousePressEvent(self, event):
        """Clicking the zone opens the file picker.

        The half of this widget's own description that was never implemented:
        the label says "click to choose one", the cursor is a pointing hand,
        and the page connects `browse` to its picker — but nothing ever
        emitted it, so the click landed on a frame that quietly did nothing
        and the only way in was drag-and-drop.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.browse.emit()
            return
        super().mousePressEvent(event)

# How long a save open may run before the "please wait" overlay says more
# than that — see _open_editor_body / _on_save_load_progress. Past this it
# is very likely a real search (a seed brute force, unpacking archives,
# scanning a binary for a key), not just the ordinary case of opening a
# file, so it is worth being explicit that this specific open is the slow
# kind rather than leaving the person wondering if the app has stalled.
_DECRYPT_HINT_AFTER_S = 3.0


class _PageJumpEdit(QLineEdit):
    """The pager's own "page n of m · t values" text, doubling as the page
    jump control — no separate spinbox sitting beside it repeating the same
    number. Looks like a plain label at rest (see #cheats_page_lbl: no
    border, no background).

    Only the page NUMBER is ever actually editable, not the whole
    sentence: a QIntValidator (set by _pager) keeps every other character
    out as it's typed, and focusing the field swaps the descriptive text
    out for just the bare current page number first — so what's on screen
    while editing is a clean number, never "page 3 of 1112 · 44,464
    values" with a digit poked into the middle of it. The full sentence
    comes back through the caller's own render, via editingFinished
    (Enter, or simply clicking away) — see _jump_from_label.
    """

    def focusInEvent(self, event):
        super().focusInEvent(event)
        m = re.search(r"\d+", self.text())
        if m:
            self.setText(m.group())
        self.selectAll()


class _SaveLoadWorker(QThread):
    finished = Signal(object, object)  # (doc, exception)
    progress = Signal(float)

    def __init__(self, path: Path, game_dir: Optional[Path], parent=None, engine: str = "",
                 full_sweep: bool = False, try_recipes: bool = False):
        super().__init__(parent)
        self._path = path
        self._game_dir = game_dir
        self._engine = engine
        self._full_sweep = full_sweep
        self._try_recipes = try_recipes
        self._is_cancelled = False
        # Only Wolf LZ4's search ever looks at this (see open_save's own
        # cancel_token docstring) — built unconditionally anyway since it
        # costs nothing before something actually registers a pool with
        # it, and cancel() below needs somewhere to signal regardless of
        # which format ends up being the one that's searching.
        from core.engines.wolf_lz4 import CancelToken
        self._cancel_token = CancelToken()

    def cancel(self):
        self._is_cancelled = True
        # Stops a Wolf LZ4 search immediately (terminates its worker pool)
        # instead of leaving it to notice _is_cancelled at its next
        # scheduled check, which — see the module this token comes from —
        # can otherwise be a long wait of its own on a slow chunk.
        self._cancel_token.cancel()

    def run(self):
        # Set from inside run(): setPriority only applies to a RUNNING
        # thread. From __init__ it did nothing but log "Cannot set
        # priority, thread is not running", so these scans never
        # actually ran at idle priority — which is the one thing the
        # call was there to do while a game has the CPU.
        self.setPriority(QThread.Priority.IdlePriority)
        from time import monotonic
        start_mono = monotonic()
        last_emit = 0.0

        def _prog_tick(elapsed=0.0):
            nonlocal last_emit
            now = monotonic()
            # Throttle progress events to max 2Hz (every 500ms) to prevent event-loop saturation and UI lag during scrolling
            if now - last_emit >= 0.5:
                last_emit = now
                self.progress.emit(now - start_mono)
            return not self._is_cancelled

        # A copy of the untouched bytes, taken BEFORE detection ever tries
        # to decompress/parse them — not after a reader claims success.
        # Decompression is the risky step this exists to protect against;
        # gating the safety copy on "this turned out to be readable" would
        # defeat the point of having one. No copy means no editor either
        # (see read_source's own docstring for why the bytes are read
        # here, once, rather than trusted to still match what open_save
        # reads moments later): a raised SaveEditorError here reaches
        # _on_save_load_finished exactly like any other open failure, and
        # nothing is ever opened for editing on top of a failed snapshot.
        try:
            from core.save_editor import read_source, backup_original
            data, p, registry = read_source(self._path)
            snapshot = backup_original(p, data)
            if snapshot is None:
                raise SaveEditorError(
                    f"could not set aside a safety copy of {p.name} before opening it",
                    "cheats.err_snapshot_failed", name=p.name)
        except Exception as exc:
            self.finished.emit(None, exc)
            return

        try:
            doc = open_save(self._path, game_dir=self._game_dir, progress=_prog_tick,
                            engine=self._engine, full_sweep=self._full_sweep,
                            try_recipes=self._try_recipes,
                            cancel_token=self._cancel_token)
            if self._is_cancelled:
                self.finished.emit(None, SaveEditorError(t("cheats.loading_cancelled")))
            else:
                self.finished.emit(doc, None)
        except Exception as exc:
            self.finished.emit(None, exc)



class CheatsPage(PageScrollMixin, QWidget, ThemedMixin):
    """Pick a game, pick a save, edit what is inside it."""

    STEP_PICK, STEP_SAVES, STEP_EDIT = 0, 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)

        self._entry = None
        self._doc = None
        self._editors = {}          # field path -> widget, for the page shown
        self._pending = {}          # every edit made, whatever page it was on
        self._held = {}             # locked values (🔒 stays until cleared)
        self._hold = None
        self._hold_armed = False    # True after Apply — loop may run when playing
        self._page = 0
        self._prefix = ""           # the part every row of a group repeats
        self._loose = None          # a save opened without a game behind it
        self._all_files = []        # every save found, one folder at a time
        self._files = []            # the save list, newest first
        self._file_page = 0
        self._games_page = 0        # the library list has its own page number
        # Async row insert (same idea as library cards): gen invalidates
        # in-flight pumps when a newer list starts.
        self._row_insert_gen = 0
        self._row_insert_queue: list = []
        self._row_insert_chunk = 16
        self._row_insert_on_done = None
        self._deferred_busy = None
        self._save_load_worker = None
        self._save_load_busy = None
        self._save_load_gen = 0
        self._loaded_path = None
        self._load_notice = None
        self._hold_watch = QTimer(self)
        self._hold_watch.setInterval(1000)
        self._hold_watch.timeout.connect(self._watch_hold_game)

        # Idle timer: release the loaded save doc from RAM if untouched and no
        # hold is running. The delay follows the machine — 10 minutes was the
        # rule for every PC, and a weak one should let go sooner (see
        # core.concurrency.idle_document_release_s). Re-read on each restart
        # of the timer, so it tracks a tier that moved.
        self._idle_save_timer = QTimer(self)
        self._idle_save_timer.timeout.connect(self._on_idle_save_timeout)

        self._build()
        # Shell only — game rows fill on first on_page_enter (async chunks).
        # Like the library: once filled, keep the UI for the whole session
        # (switching to Settings and back must not rebuild).
        self._pending_initial_load = True
        self.show_step(self.STEP_PICK, refresh_pick=False)

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._back_btn = QPushButton("←")
        self._back_btn.setObjectName("cheats_back")
        self._back_btn.setFixedSize(scaled(28, self), scaled(28, self))
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._go_back)
        head.addWidget(self._back_btn)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._title = QLabel(t("cheats.title"))
        self._title.setObjectName("page_title")
        self._subtitle = QLabel(t("cheats.subtitle"))
        self._subtitle.setObjectName("cheats_subtitle")
        titles.addWidget(self._title)
        titles.addWidget(self._subtitle)
        head.addLayout(titles, 1)
        root.addLayout(head)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_pick())
        self._stack.addWidget(self._build_saves())
        self._stack.addWidget(self._build_edit())
        root.addWidget(self._stack, 1)

    def _scroller(self):
        area = QScrollArea()
        area.setObjectName("cheats_scroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("transparent_bg")
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addStretch(1)
        area.setWidget(body)
        self._register_page_scroll(area, list_content=True)
        return area, col

    def _build_pick(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        self._search = GhostClearableLineEdit()
        self._search.setPlaceholderText(t("cheats.search_placeholder"))
        self._search.setFixedHeight(scaled(32, self))
        self._search.setObjectName("list_search")
        self._search.textChanged.connect(self._on_game_search_changed)
        # ↓ or a click on the hint takes the game it is pointing at, the same
        # gesture as the library's tag search.
        self._search.ghost_accepted.connect(self._accept_ghost)
        self._search.returnPressed.connect(self._accept_ghost)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self._search, 1)
        col.addLayout(search_row)
        self._games_area, self._games_col = self._scroller()
        col.addWidget(self._games_area, 1)
        # Own page size on the pager row (with ← n/m →), not beside search.
        self._games_size_combo = PageSizeCombo(
            SCOPE_CHEATS_GAMES, self._on_games_page_size_changed)
        (bar, self._games_first, self._games_prev, self._games_page_lbl,
         self._games_next, self._games_last) = self._pager(
            self._games_size_combo)
        self._games_first.clicked.connect(lambda: self._jump_games(0))
        self._games_prev.clicked.connect(lambda: self._step_games(-1))
        self._games_next.clicked.connect(lambda: self._step_games(1))
        self._games_last.clicked.connect(lambda: self._jump_games(-1))
        self._games_page_lbl.editingFinished.connect(
            lambda: self._jump_from_label(
                self._games_page_lbl, self._jump_games))
        col.addLayout(bar)
        self._drop = _DropZone()
        self._drop.chosen.connect(self._open_loose)
        self._drop.browse.connect(self._browse_for_save)
        col.addWidget(self._drop)
        return page

    def _build_saves(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self._kept_lbl = QLabel(t("cheats.kept_copies"))
        self._kept_lbl.setObjectName("section_header")
        col.addWidget(self._kept_lbl)
        self._kept_area, self._kept_col = self._scroller()
        self._kept_area.setMaximumHeight(scaled(150, self))
        col.addWidget(self._kept_area)
        head = QHBoxLayout()
        head.setSpacing(8)
        self._files_lbl = QLabel(t("cheats.pick_save"))
        self._files_lbl.setObjectName("section_header")
        head.addWidget(self._files_lbl)
        head.addStretch(1)
        # A game can save into more than one folder, and the list runs to
        # hundreds. Narrowing it to one folder is the difference between
        # paging through all of them and looking where you know it is. Same
        # untouched QComboBox as the editor's, for the same reason: the theme
        # already dresses it in both light and dark.
        self._folder_combo = QComboBox()
        self._folder_combo.setMaximumWidth(scaled(320, self))
        self._folder_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._folder_combo.currentIndexChanged.connect(self._apply_folder)
        head.addWidget(self._folder_combo)
        col.addLayout(head)
        self._files_area, self._files_col = self._scroller()
        col.addWidget(self._files_area, 1)
        # A game with several save paths has every save listed once per path,
        # so even a modest folder runs to a hundred rows. Same pager as the
        # editor's, so the two read the same way. Page size sits on this row.
        self._saves_size_combo = PageSizeCombo(
            SCOPE_CHEATS_SAVES, self._on_saves_page_size_changed)
        (bar, self._file_first, self._file_prev, self._file_page_lbl,
         self._file_next, self._file_last) = self._pager(
            self._saves_size_combo)
        self._file_first.clicked.connect(lambda: self._jump_saves(0))
        self._file_prev.clicked.connect(lambda: self._step_saves(-1))
        self._file_next.clicked.connect(lambda: self._step_saves(1))
        self._file_last.clicked.connect(lambda: self._jump_saves(-1))
        self._file_page_lbl.editingFinished.connect(
            lambda: self._jump_from_label(
                self._file_page_lbl, self._jump_saves))
        col.addLayout(bar)
        return page

    @staticmethod
    def _pager(size_combo=None):
        """The « ← n/m → » strip: the layout and its widgets.

        Each list that needs one keeps its own, with its own page number: a
        counter shared between the save list and the editor would jump about
        as you moved from one to the other and back. Optional *size_combo*
        sits on the right of the same row.

        *first*/*last* jump straight to either end — one step with ← / →
        each is a lot of clicks on a save with tens of thousands of values.
        *lbl* (a ``_PageJumpEdit``, not a plain label) IS the page jump
        control — no separate spinbox duplicating the same number beside
        it, and a QIntValidator here keeps it a NUMBER field, not the
        whole sentence made freely editable: focusing it swaps the
        descriptive text for just the current page number, typing can
        only ever produce digits, and that call site's editingFinished
        handler (Enter, or simply clicking away) jumps there. Looks
        exactly like the label it replaces at rest — see #cheats_page_lbl's
        border/background in the stylesheets.
        """
        bar = QHBoxLayout()
        bar.setSpacing(8)
        first, prev = QPushButton("«"), QPushButton("←")
        nxt, last = QPushButton("→"), QPushButton("»")
        for btn in (first, prev, nxt, last):
            btn.setObjectName("cheats_pager")
            btn.setFixedSize(scaled(28, btn), scaled(24, btn))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl = _PageJumpEdit("")
        lbl.setObjectName("cheats_page_lbl")
        lbl.setFrame(False)
        # Only a page NUMBER can ever be typed — this only ever touches
        # actual keystrokes/paste, never the descriptive text a render()
        # sets programmatically (setText bypasses a validator entirely),
        # so "page 3 of 1112 · …" still displays fine at rest.
        lbl.setValidator(QIntValidator(1, 999_999_999, lbl))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Keep "pagina n di m · …" readable — without Minimum the page-size
        # combo used to compress this label until the numbers clipped.
        lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        bar.addStretch(1)
        bar.addWidget(first)
        bar.addWidget(prev)
        bar.addWidget(lbl)
        bar.addWidget(nxt)
        bar.addWidget(last)
        bar.addStretch(1)
        if size_combo is not None:
            bar.addWidget(size_combo)
        return bar, first, prev, lbl, nxt, last

    @staticmethod
    def _jump_from_label(edit, jump) -> None:
        """Parse a page number out of *edit*'s own text and hand it to
        *jump* (0-based) — shared by all three pagers' editingFinished, so
        this fires on Enter AND on simply clicking away.

        _PageJumpEdit's own focusInEvent already swaps the field down to
        just the bare page number before anyone can type — the validator
        set in _pager keeps it that way — so this is normally reading a
        clean number, not picking one out of a sentence. The regex search
        (rather than a plain int() on the whole field) is a second line of
        defence, not the mechanism: nothing currently reaches this with
        anything else in the field, but failing closed on the rare case it
        somehow did is cheap and exact int() is not. No digits at all is
        not a page to guess at — left alone, and the caller's own next
        render restores the descriptive text regardless of whether a jump
        happened.
        """
        m = re.search(r"\d+", edit.text())
        if not m:
            return
        page = max(1, int(m.group()))
        jump(page - 1)
        edit.clearFocus()

    @staticmethod
    def _fit_page_label(edit) -> None:
        """Grow *edit* to fit its OWN current text — called right after
        every setText() with the descriptive "page n of m · t values"
        sentence. A QLineEdit's sizeHint (Minimum size policy set in
        _pager, so it can grow but never shrinks below it) is a fixed,
        content-independent default, unlike the QLabel this field
        replaced — which sized itself to whatever text it was given
        automatically. Left alone, a longer sentence than that default
        (a locale whose wording simply runs longer, or a page/count high
        enough to add digits) just gets clipped inside the same fixed
        width instead of the field actually growing to show it."""
        width = edit.fontMetrics().horizontalAdvance(edit.text())
        edit.setMinimumWidth(width + scaled(16, edit))

    def _build_edit(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        bar = QHBoxLayout()
        # A save is not one long list, it is a handful of things the engine
        # keeps separately — switches, variables, the party. Offering them
        # apart is the difference between fifteen thousand rows and the two
        # dozen anyone came here for.
        # No stylesheet of its own. The theme already dresses QComboBox in
        # both light and dark — hover, focus, the arrow, the drop-down list —
        # and a per-widget sheet overrode only some of that, so the box came
        # out with the wrong padding and radius and no room for its arrow.
        # Letting the theme have it keeps the two in step for good.
        self._group_combo = QComboBox()
        self._group_combo.setMaximumWidth(scaled(190, self))
        self._group_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._group_combo.currentIndexChanged.connect(self._apply_group)
        bar.addWidget(self._group_combo)
        self._field_filter = ClearableLineEdit()
        self._field_filter.setObjectName("list_search")
        self._field_filter.setFixedHeight(scaled(30, self))
        self._field_filter.textChanged.connect(self._apply_field_filter)
        # Search by field NAME or by current VALUE. With thousands of values
        # the name is often unknown ("which flag is party gold?"), but the
        # number showing in-game is not — so a small toggle on the left of
        # the field switches what the text matches against.
        self._filter_by_value = False
        self._filter_mode_btn = self._field_filter.add_leading_button(
            "Aa", t("cheats.filter_mode_name"))
        self._filter_mode_btn.clicked.connect(self._toggle_filter_mode)
        self._sync_filter_mode()
        bar.addWidget(self._field_filter, 1)
        self._save_btn = QPushButton(t("cheats.apply"))
        self._save_btn.setObjectName("form_primary_btn")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._apply_edits)
        bar.addWidget(self._save_btn)
        col.addLayout(bar)
        self._edit_hint = QLabel("")
        self._edit_hint.setObjectName("form_hint")
        self._edit_hint.setWordWrap(True)
        col.addWidget(self._edit_hint)
        # Shown only over a read-only reading (see _on_save_load_finished):
        # an explicit, opt-in offer to try the generic unwrap recipe
        # battery on this same file, in case it does better than whatever
        # reader already understood it well enough to read but not to
        # trust writing back. Never shown over a save the recipe battery
        # itself already produced — retrying a retry offers nothing.
        self._recipe_retry_btn = QPushButton(t("cheats.try_recipes_readonly"))
        self._recipe_retry_btn.setObjectName("form_link_btn")
        self._recipe_retry_btn.setFlat(True)
        self._recipe_retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recipe_retry_btn.setVisible(False)
        self._recipe_retry_btn.clicked.connect(self._retry_with_recipes)
        col.addWidget(self._recipe_retry_btn)
        self._hold_lbl = QLabel("")
        self._hold_lbl.setObjectName("cheats_holding")
        self._hold_lbl.setVisible(False)
        col.addWidget(self._hold_lbl)
        self._fields_area, self._fields_col = self._scroller()
        col.addWidget(self._fields_area, 1)

        # A save can hold hundreds of values; one endless scroll is not a
        # list anyone reads. The filter narrows what is paged, so a search
        # and a page number are the same tool.
        (pager, self._first_btn, self._prev_btn, self._page_lbl,
         self._next_btn, self._last_btn) = self._pager()
        self._first_btn.clicked.connect(lambda: self._jump_page(0))
        self._prev_btn.clicked.connect(lambda: self._step_page(-1))
        self._next_btn.clicked.connect(lambda: self._step_page(1))
        self._last_btn.clicked.connect(lambda: self._jump_page(-1))
        self._page_lbl.editingFinished.connect(
            lambda: self._jump_from_label(self._page_lbl, self._jump_page))
        col.addLayout(pager)
        return page

    # ── Steps ────────────────────────────────────────────────────────────────

    def show_step(self, step: int, *, refresh_pick: bool = True):
        self._stack.setCurrentIndex(step)
        self._back_btn.setVisible(step != self.STEP_PICK)
        if step == self.STEP_PICK:
            self._subtitle.setText(t("cheats.subtitle"))
            if refresh_pick:
                self._refresh_games()
        elif step == self.STEP_EDIT and getattr(self, "_fields_incomplete", False):
            # A cancelled field-row build left the current page short —
            # re-entering the editor for the same open doc is otherwise
            # the one path that does not naturally re-render it (unlike
            # STEP_PICK above, or STEP_SAVES, which only shows through
            # picking a game and so already rebuilds fresh every time).
            self._fields_incomplete = False
            self._render_page()
        self._sync_title()

    def _sync_title(self):
        if self._stack.currentIndex() == self.STEP_PICK or self._entry is None:
            self._title.setText(t("cheats.title"))
        else:
            self._title.setText(self._entry.name)

    def _browse_for_save(self):
        from ui.widgets.file_pickers import pick_file
        path = pick_file(self, t("cheats.open_save"))
        if path:
            self._open_loose(path)

    def _open_loose(self, path: str):
        """A save chosen on its own, with no game behind it."""
        self._entry = None
        self._loose = Path(path)
        self._open_editor(self._loose)

    def _go_back(self):
        step = self._stack.currentIndex()
        if step == self.STEP_EDIT:
            # A load still actively showing its "please wait" overlay must
            # not be left to land after we've already navigated away — see
            # _abandon_in_flight_save_load's own docstring for exactly the
            # bug that leaves. A SHELVED load is deliberately left running
            # (respect_shelved defaults to True) — that is the point of
            # shelving it in the first place.
            self._abandon_in_flight_save_load()
            # Walking away from the editor stops holding: a loop rewriting a
            # file for a screen nobody is looking at is not something to
            # leave running.
            self._held = {}
            self._hold_armed = False
            self._stop_hold()
            self._hold_watch.stop()
            self._sync_hold_label()
            if self._entry is None:
                # Picked on its own, so back means that file and the copies
                # kept of it, not a game list it never came from.
                if self._loose is not None:
                    self._show_loose(self._loose)
                else:
                    self._doc = None
                    self.show_step(self.STEP_PICK)
                return
            self._open_game(self._entry)
        else:
            self._entry = None
            self._loose = None
            self._search.clear()
            self.show_step(self.STEP_PICK)

    # ── Step 1: pick a game ──────────────────────────────────────────────────

    def _matches(self) -> list:
        q = self._search.text().strip().casefold()
        games = sorted(get_library().all_games(), key=lambda g: g.name.casefold())
        if not q:
            return games
        starts = [g for g in games if g.name.casefold().startswith(q)]
        rest = [g for g in games if q in g.name.casefold() and g not in starts]
        return starts + rest

    def _on_game_search_changed(self, _text: str = ""):
        """A new search is a new list — page 2 of the old one means nothing."""
        self._games_page = 0
        self._refresh_games()

    def _on_games_page_size_changed(self, _size: int):
        self._games_page = 0
        self._refresh_games()

    def _step_games(self, delta: int):
        self._games_page += delta
        self._refresh_games()

    def _jump_games(self, index: int):
        self._games_page = index if index >= 0 else 1 << 30
        self._refresh_games()

    def _refresh_games(self):
        with guarded_render(SCOPE_CHEATS_GAMES):
            self._refresh_games_inner()

    def _refresh_games_inner(self):
        self._clear(self._games_col)
        found = self._matches()
        q = self._search.text().strip()
        # The ghost mirrors the first match, exactly like the tag search:
        # painted, never inserted, so typing is never fought with.
        if q and found and found[0].name.casefold().startswith(q.casefold()) \
                and len(found[0].name) > len(q):
            self._search.set_ghost(found[0].name[len(q):])
        else:
            self._search.set_ghost("")
        # Paged rather than cut off at a fixed 200: a library past that lost
        # its tail with nothing said, and the rows here are cheap enough that
        # the only reason to limit them is how far anyone wants to scroll.
        per_page = page_size(SCOPE_CHEATS_GAMES)
        pages = max(1, (len(found) + per_page - 1) // per_page)
        self._games_page = max(0, min(self._games_page, pages - 1))
        start = self._games_page * per_page
        self._games_page_lbl.setText(t("cheats.page_of_games",
                                       page=self._games_page + 1,
                                       pages=pages, total=len(found)))
        self._fit_page_label(self._games_page_lbl)
        self._games_first.setEnabled(self._games_page > 0)
        self._games_prev.setEnabled(self._games_page > 0)
        self._games_next.setEnabled(self._games_page < pages - 1)
        self._games_last.setEnabled(self._games_page < pages - 1)
        if not found:
            self._cancel_row_insert()
            self._add_note(self._games_col, t("cheats.no_games"))
            return
        from core.engines.game_engine import engine_for_game, engine_display
        jobs = []
        for g in found[start:start + per_page]:
            def _build(g=g):
                n = len(g.save_paths or [])
                # engine_for_game, not the raw stored field: it falls back to
                # detecting off the exe on disk when nothing was saved yet —
                # the same live check _open_game (below) and the library page
                # already do, so a game the library shows an engine for
                # doesn't turn up "Unknown" here just because nothing was
                # ever written to entry.engine.
                detected_eng = engine_for_game(g)
                # Same "Unknown" placeholder the Add/Edit Game engine field
                # shows when empty — a blank engine used to mean nothing was
                # written here at all, as if the row had no opinion on it.
                eng = engine_display(detected_eng) if detected_eng else t("common.unknown")
                row = _Row(g.name, t("cheats.n_paths", count=n) if n else
                           t("cheats.no_paths"), engine=eng)
                row.clicked.connect(lambda e=g: self._open_game(e))
                return self._games_col, row
            jobs.append(_build)
        self._begin_async_rows(jobs)


    def _accept_ghost(self):
        found = self._matches()
        if found:
            self._open_game(found[0])

    # ── Step 2: the game's saves ─────────────────────────────────────────────

    def open_for_game(self, game_id: str) -> bool:
        """Jump straight to a game — the library's context menu and the
        in-game shortcut both land here."""
        entry = get_library().get_by_id(game_id)
        if entry is None:
            return False
        self._open_game(entry)
        return True

    def ensure_loaded(self):
        """Kick initial load when user navigates to CheatsPage."""
        self.on_page_enter()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_pending_initial_load", False):
            self.on_page_enter()

    def on_page_enter(self):
        """First visit fills the pick list; later visits keep the last step
        — except a loaded document is re-validated against disk here first.

        The file behind it can change from OUTSIDE this page entirely (the
        Backups page restoring the whole save folder, cloud sync pulling a
        different version, the game's own autosave, or this page's OWN hold
        loop having been quietly re-applying values the whole time — see
        SaveHold) while this tab was not the active one, and nothing else
        on this page would ever notice: _open_editor's own staleness check
        only runs when a file is explicitly clicked again, not when this
        tab simply regains focus — so returning to a save already open in
        STEP_EDIT could otherwise go on showing values from before whatever
        changed it, indefinitely.

        Refreshed IN PLACE (via _open_editor on the same path) rather than
        dropped back to the save list — a stale view is still wrong, but
        losing your place every time you glance at another tab is its own
        kind of broken. _open_editor always clears _held/_hold_armed for
        what is, from its own point of view, an ordinary re-open; those are
        restored right after so a lock already armed before you left keeps
        being shown as held, and _hold_watch resumes checking on its own
        usual schedule (see _watch_hold_game) instead of silently going
        quiet until the next manual Apply.
        """
        if (self._doc is not None and self._loaded_path is not None
                and self._stack.currentIndex() == self.STEP_EDIT):
            try:
                mtime = self._loaded_path.stat().st_mtime_ns
            except OSError:
                mtime = 0
            if mtime != self._loaded_mtime:
                path = self._loaded_path
                held_snapshot = dict(self._held)
                armed_snapshot = self._hold_armed
                self._open_editor(path)
                self._held = held_snapshot
                self._hold_armed = armed_snapshot
                if self._held and self._hold_armed:
                    self._hold_watch.start()
                return
        if self._entry is not None or self._stack.currentIndex() != self.STEP_PICK:
            return
        QTimer.singleShot(0, self._enter_after_paint)

    def _busy_holding_in_game(self) -> bool:
        """The one condition that must never lose a loaded document: the game
        is running AND a hold is actively re-applying values into it."""
        return bool(self._playing() and self._hold is not None
                    and self._hold.is_running())

    def _reset_idle_save_timer(self):
        """(Re)start the idle release countdown when a document is loaded and
        no hold is running against a live game."""
        if getattr(self, "_doc", None) is not None and not self._busy_holding_in_game():
            from core.concurrency import idle_document_release_s
            self._idle_save_timer.setInterval(idle_document_release_s() * 1000)
            self._idle_save_timer.start()
        else:
            self._idle_save_timer.stop()

    def release_idle_document(self) -> bool:
        """Let go of the loaded save now, if it is safe to. True if released.

        Public so the background memory sweep can ask for it when the machine
        is genuinely short of RAM, instead of waiting out the countdown — a
        loaded save is the largest thing this page holds by choice (the
        original bytes plus the parsed structure). Same guard as the timer,
        so pressure can never take a document a running hold is writing to.
        """
        if getattr(self, "_doc", None) is None or self._busy_holding_in_game():
            return False
        self._on_idle_save_timeout()
        return True

    def _on_idle_save_timeout(self):
        """Release the loaded save document from RAM once it has gone
        untouched for the tier's idle window and no hold is running."""
        is_actively_holding_in_game = self._busy_holding_in_game()
        if getattr(self, "_doc", None) is not None and not is_actively_holding_in_game:
            logger.info("CheatsPage: idle window reached outside active game, releasing save document from memory.")
            self._cancel_row_insert()
            self._stop_hold()
            self._hold_watch.stop()
            self._doc = None
            self._pending.clear()
            self._editors.clear()
            self._clear(self._fields_col)
            if self._entry is not None:
                self.show_step(self.STEP_SAVES)
            else:
                self.show_step(self.STEP_PICK)
            try:
                from ui.helpers import trim_process_memory
                trim_process_memory()
            except Exception:
                pass

    def on_page_leave(self):
        """Clean up in-flight pumps and active holds when user switches to another tab.
        Shelved save-loads in the sidebar continue running uninterrupted."""
        self._cancel_row_insert()
        self._stop_hold()
        self._hold_watch.stop()
        self._reset_idle_save_timer()
        busy = getattr(self, "_save_load_busy", None)
        is_shelved = busy is not None and getattr(busy, "_shelved", False)
        if not is_shelved:
            worker = getattr(self, "_save_load_worker", None)
            if worker is not None:
                try:
                    worker.cancel()
                except Exception:
                    pass
                self._save_load_worker = None
            self._stop_deferred_busy()

    def wipe_and_reload(self):
        """Wipe save editor state and prune old save structures from memory."""
        self._cancel_row_insert()
        self._stop_hold()
        self._hold_watch.stop()
        self._doc = None
        self._loaded_path = None
        self._loaded_mtime = 0
        self._pending.clear()
        self._held.clear()
        self._editors.clear()
        self._all_files.clear()
        self._files.clear()
        self._entry = None
        self._loose = None
        # Hard reset (deep idle / switching games — see this method's own
        # docstring): even a deliberately-shelved load should not survive
        # this one, unlike _go_back's ordinary "the person navigated away".
        self._abandon_in_flight_save_load(respect_shelved=False)
        self._close_load_notice()
        if hasattr(self, "_folder_combo"):
            self._folder_combo.blockSignals(True)
            self._folder_combo.clear()
            self._folder_combo.blockSignals(False)
        if hasattr(self, "_group_combo"):
            self._group_combo.blockSignals(True)
            self._group_combo.clear()
            self._group_combo.blockSignals(False)
        self._clear(self._games_col)
        self._clear(self._kept_col)
        self._clear(self._files_col)
        self._clear(self._fields_col)
        self._pending_initial_load = True
        self.show_step(self.STEP_PICK, refresh_pick=False)
        if self.isVisible():
            self.on_page_enter()

    def _enter_after_paint(self):
        if self._entry is not None or self._stack.currentIndex() != self.STEP_PICK:
            return
        self._pending_initial_load = False
        try:
            from core.monitor import get_monitor
            playing = get_monitor().currently_playing()
        except Exception as e:
            logger.debug(f"Could not read the running game: {e}")
            playing = []
        if playing:
            self._open_game(playing[0])
        else:
            self._refresh_games()


    def _open_game(self, entry):
        self._entry = entry
        self._loose = None
        from core.engines.game_engine import engine_for_game, label as engine_label
        eng = engine_label(engine_for_game(entry))
        self._show_saves(_save_files(entry),
                         t("cheats.pick_save_engine", engine=eng) if eng
                         else t("cheats.pick_save_hint"))

    def _show_loose(self, path: Path):
        """The saves screen for one file that came in on its own.

        Worth having rather than sending the back arrow straight to the game
        list: this is where the copies SaveSync kept of that file live, and
        without it an edit made to a loose save could not be undone.
        """
        self._entry = None
        self._loose = Path(path)
        self._show_saves([self._loose], t("cheats.pick_save_hint"))

    def _show_saves(self, files, subtitle: str):
        self._doc = None
        self._all_files = list(files)
        self._files = list(files)
        self._file_page = 0
        self.show_step(self.STEP_SAVES)
        self._subtitle.setText(subtitle)
        self._fill_folders()
        # Arriving here is what applies the copy rules — "delete after N days"
        # has to hold for a save nobody has edited since, and writing is the
        # only other moment they run. Once per visit, not once per page turn:
        # paging back and forth is not a reason to go over the disk again.
        for f in self._files[:page_size(SCOPE_CHEATS_SAVES)]:
            prune_backups(f)
        self._render_saves_page()

    def _fill_folders(self):
        """Offer the folders these saves came from, in the order they appear.

        Only when there is more than one: a single folder makes the choice
        meaningless, and a control with one option in it is furniture.
        """
        combo = self._folder_combo
        folders = list(dict.fromkeys(str(f.parent) for f in self._all_files))
        combo.blockSignals(True)
        combo.clear()
        if len(folders) > 1:
            counts = {}
            for f in self._all_files:
                counts[str(f.parent)] = counts.get(str(f.parent), 0) + 1
            combo.addItem(t("cheats.all_folders"), "")
            names = _short_labels(folders)
            for where in folders:
                combo.addItem(
                    f"{names[where]} · "
                    f"{t('cheats.n_saves_in', count=counts[where])}", where)
                combo.setItemData(combo.count() - 1, where,
                                  Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(0)
        combo.blockSignals(False)
        combo.setVisible(len(folders) > 1)

    def _apply_folder(self):
        """Narrow the list to one folder, or widen it back to all of them."""
        where = self._folder_combo.currentData()
        self._files = ([f for f in self._all_files if str(f.parent) == where]
                       if where else list(self._all_files))
        self._file_page = 0
        self._render_saves_page()

    def _on_saves_page_size_changed(self, _size: int):
        self._file_page = 0
        self._render_saves_page()

    def _render_saves_page(self):
        with guarded_render(SCOPE_CHEATS_SAVES):
            self._render_saves_page_inner()

    def _render_saves_page_inner(self):
        """One page of the save list, with the copies kept of what is on it.

        The copies follow the page rather than the whole list: reading them
        means a look in the folder per save, and a game with several save
        paths has a few hundred. What anyone is here to undo is the save they
        just edited, which — being the newest — is on the first page.

        File/kept rows insert in QTimer chunks (library-style) so switching
        into this step never freezes on a long page.
        """
        self._clear(self._kept_col)
        self._clear(self._files_col)
        files = self._files
        per_page = page_size(SCOPE_CHEATS_SAVES)
        pages = max(1, (len(files) + per_page - 1) // per_page)
        self._file_page = max(0, min(self._file_page, pages - 1))
        start = self._file_page * per_page
        shown = files[start:start + per_page]

        self._file_page_lbl.setText(t("cheats.page_of_saves",
                                      page=self._file_page + 1,
                                      pages=pages, total=len(files)))
        self._fit_page_label(self._file_page_lbl)
        self._file_first.setEnabled(self._file_page > 0)
        self._file_prev.setEnabled(self._file_page > 0)
        self._file_next.setEnabled(self._file_page < pages - 1)
        self._file_last.setEnabled(self._file_page < pages - 1)

        jobs = []
        if not files:
            no_paths = self._entry is not None and not (self._entry.save_paths
                                                        or [])
            note = t("cheats.no_paths_yet" if no_paths else "cheats.no_saves")

            def _empty_files(note=note):
                lbl = QLabel(note)
                lbl.setObjectName("empty_hint")
                lbl.setWordWrap(True)
                return self._files_col, lbl

            jobs.append(_empty_files)
        else:
            show_where = len({f.parent for f in files}) > 1
            for f in shown:
                def _file_row(f=f, show_where=show_where):
                    known = describe(f)
                    detail = known or f.suffix.lower().lstrip(".") or ""
                    row = _Row(f.name, detail,
                               str(f.parent) if show_where else "")
                    row.setToolTip(f"{f.name}\n{f}")
                    row.clicked.connect(lambda p=f: self._open_editor(p))
                    return self._files_col, row
                jobs.append(_file_row)

        kept = []
        for f in shown:
            for copy, when in list_backups(f):
                kept.append((when, copy, f))
        kept.sort(reverse=True, key=lambda t_: t_[0])
        if not kept:
            def _empty_kept():
                lbl = QLabel(t("cheats.no_kept"))
                lbl.setObjectName("empty_hint")
                lbl.setWordWrap(True)
                return self._kept_col, lbl
            jobs.append(_empty_kept)
        else:
            for when, copy, target in kept[:20]:
                def _kept_row(when=when, copy=copy, target=target):
                    row = _Row(target.name, when.strftime("%d/%m/%Y %H:%M"))
                    row.setToolTip(str(copy))
                    row.add_button(
                        t("cheats.restore"),
                        lambda _=False, c=copy, tg=target, w=when: self._restore(c, tg, w))
                    row.add_icon_button(
                        "🗑", t("cheats.delete_kept"),
                        lambda _=False, c=copy, w=when: self._delete_kept(c, w))
                    return self._kept_col, row
                jobs.append(_kept_row)

        self._begin_async_rows(jobs)

    def _step_saves(self, delta: int):
        self._file_page += delta
        self._render_saves_page()

    def _jump_saves(self, index: int):
        self._file_page = index if index >= 0 else 1 << 30
        self._render_saves_page()

    def _restore(self, copy: Path, target: Path, when=None):
        from PySide6.QtWidgets import QMessageBox
        from ui.modal_helpers import question_window_modal
        when_str = when.strftime("%d/%m/%Y %H:%M") if when else copy.name
        reply = question_window_modal(
            self, t("cheats.title"),
            t("cheats.restore_confirm", name=target.name, when=when_str),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            restore_backup(copy, target)
        except SaveEditorError as e:
            warning_window_modal(self, t("cheats.title"), explain(e))
            return
        except OSError as e:
            logger.error(f"Restore failed for {target.name}: {e}")
            warning_window_modal(self, t("cheats.title"),
                                 t("cheats.write_failed", error=str(e)))
            return
        # Drop any document held for this file: the bytes on disk just changed
        # under it, and the editor must reopen from the restored file rather
        # than redisplay what was in memory before the restore.
        if getattr(self, "_loaded_path", None) == Path(target).resolve():
            self._doc = None
            self._loaded_path = None
            self._loaded_mtime = 0
        self._subtitle.setText(t("cheats.restored", name=target.name))
        if self._entry is not None:
            self._open_game(self._entry)
        else:
            self._show_loose(target)

    def _delete_kept(self, copy: Path, when):
        from PySide6.QtWidgets import QMessageBox
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("cheats.title"),
            t("cheats.delete_kept_question", when=when.strftime("%d/%m/%Y %H:%M")),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_backup(copy)
        except SaveEditorError as e:
            warning_window_modal(self, t("cheats.title"), explain(e))
            return
        self._subtitle.setText(t("cheats.deleted_kept"))
        self._render_saves_page()

    # ── Step 3: the editor ───────────────────────────────────────────────────

    def _game_dir(self):
        """Where this game is installed, when that is known.

        A save does not always live with its game — Unity writes them under
        the user's profile — and one format has to look in the game's own
        files to open the save at all. Without a library entry (a loose
        save), the save's OWN folder still works as a starting point:
        detect_engine walks UP from whatever it is given (see
        core.engines.game_engine.detect_engine), so a save kept inside or
        near the install folder is found the same way a library game's exe
        would find it — engine detection is not exclusive to the library.
        """
        exe = getattr(self._entry, "exe_path", "") if self._entry else ""
        anchor = exe or (str(self._loose) if self._loose else "")
        if not anchor:
            return None
        try:
            parent = Path(anchor).parent
            return parent if parent.is_dir() else None
        except (OSError, ValueError):
            return None

    def _playing(self) -> str:
        """The name of the game this save belongs to, if it is running now.

        A running game holds its own copy of the state in memory: an edit
        made underneath it is not seen until the save is loaded, and is
        written over the moment the game saves again. Worth saying, and only
        worth saying when it is actually true of THIS game.
        """
        try:
            from core.monitor import get_monitor
            running = get_monitor().currently_playing()
        except Exception as e:
            logger.debug(f"Could not read the running games: {e}")
            return ""
        if self._entry is not None:
            return next((g.name for g in running if g.id == self._entry.id), "")
        # A save opened on its own belongs to whichever running game keeps it.
        if self._loose is None:
            return ""
        try:
            here = self._loose.resolve()
        except OSError:
            return ""
        for g in running:
            for raw in (g.save_paths or []):
                try:
                    root = Path(raw).resolve()
                except OSError:
                    continue
                if here == root or root in here.parents:
                    return g.name
        return ""

    def _abandon_in_flight_save_load(self, respect_shelved: bool = True):
        """Cancel any save load still in flight and make sure it can never
        act again, even after this call returns.

        ``worker.cancel()`` is cooperative, not immediate — the docstring
        on ``_SaveLoadWorker.cancel`` is explicit that a search can still
        be mid-round for a couple more seconds — so its ``finished``
        signal WILL still fire later. Without bumping ``_save_load_gen``
        here too, that late arrival passes ``_on_save_load_finished``'s
        own staleness check (which compares against exactly this counter)
        and is treated as a normal, current completion: it overwrites
        ``self._doc`` and forces ``show_step(STEP_EDIT)`` again, for
        whatever page the person navigated to in the meantime. That is
        the exact shape of a real bug this method exists to close: back
        out of the editor while a load is still running, land wherever
        "back" was supposed to go, and get yanked into a half-populated
        editor a moment later because the abandoned load finished after
        all. Every caller that leaves STEP_EDIT (or wipes the page's
        state outright) needs this, not just the one that starts a new
        load — see ``_go_back`` and ``wipe_and_reload``.

        *respect_shelved*: a load put in the sidebar (``BusyOverlay``'s
        shelvable mode) is DELIBERATELY left running so the rest of the
        app stays usable while it finishes — that is the entire point of
        shelving, and leaving the editor page is not supposed to cancel
        it. True (the default — right for ``_go_back``, an ordinary
        "the person navigated away") leaves a shelved load completely
        alone: worker, overlay and generation counter untouched, so
        ``_on_save_load_finished`` still treats its eventual arrival as
        current and does what shelved completion already correctly does
        (updates the sidebar notice, does not force ``STEP_EDIT`` — see
        that method's own ``shelved`` handling). False is for an actual
        hard reset (``wipe_and_reload``, run on deep idle / switching
        games) where even a shelved load should not survive.
        """
        busy = getattr(self, "_save_load_busy", None)
        if respect_shelved and busy is not None and getattr(busy, "_shelved", False):
            return
        worker = getattr(self, "_save_load_worker", None)
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass
            self._save_load_worker = None
        if busy is not None:
            try:
                busy.close_overlay()
            except Exception:
                pass
            self._save_load_busy = None
        self._save_load_gen = getattr(self, "_save_load_gen", 0) + 1

    def _open_editor(self, path: Path, forced_engine: str = "", full_sweep: bool = False,
                     try_recipes: bool = False):
        resolved_path = Path(path).resolve()
        # Fast path: if the document for this exact save file is already in memory
        # and has not been modified externally, display it immediately without reloading!
        # Skipped for a forced-engine/full-sweep/recipe retry — that fast path
        # would just hand back the same state instead of trying again.
        try:
            mtime = resolved_path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if (not forced_engine and not full_sweep and not try_recipes
                and self._doc is not None
                and mtime  # a failed stat (locked mid-write) reads as 0 —
                           # never serve the cache off that, always reload
                and getattr(self, "_loaded_path", None) == resolved_path
                and getattr(self, "_loaded_mtime", 0) == mtime):
            self._close_load_notice()
            self.show_step(self.STEP_EDIT)
            return

        # If a DIFFERENT save file was open previously, purge old structures/AST from memory
        if getattr(self, "_loaded_path", None) != resolved_path:
            self._doc = None
            self._loaded_path = None
            self._loaded_mtime = 0
            try:
                from core.save_editor import prune_all
                prune_all()
            except Exception:
                pass

        # Not respect_shelved: this is about to become the tracked worker
        # and overlay regardless (both fields are about to be overwritten
        # below), so a shelved load left alone here would just be orphaned
        # under a gen this new load is about to reuse — see
        # _abandon_in_flight_save_load's own docstring. Starting a genuinely
        # new load always needs a fresh generation of its own.
        self._abandon_in_flight_save_load(respect_shelved=False)
        gen = self._save_load_gen

        # Enter the edit step immediately so the page never feels frozen,
        # then open the save on the next event-loop turn.
        self._cancel_row_insert()
        self._stop_hold()
        self._hold_watch.stop()
        self._pending = {}
        self._held = {}
        self._hold_armed = False
        self._page = 0
        self._doc = None
        self._loaded_path = None
        self._loaded_mtime = 0
        # A previous shelved load may still be showing its "Loaded" notice —
        # a fresh open starts clean.
        self._close_load_notice()
        self._subtitle.setText(t("common.please_wait"))
        self._edit_hint.setText("")
        self._recipe_retry_btn.setVisible(False)
        self._save_btn.setEnabled(False)
        self._field_filter.clear()
        self._group_combo.blockSignals(True)
        self._group_combo.clear()
        self._group_combo.blockSignals(False)
        self.show_step(self.STEP_EDIT)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda p=resolved_path, g=gen:
                          self._open_editor_body(p, g, forced_engine, full_sweep, try_recipes))

    def _retry_with_recipes(self):
        """The read-only escalation: "this file was read, but SaveSync
        isn't sure it's safe to write back — try to break its obfuscation?"
        — see cheats.try_recipes_readonly, shown only over a read-only
        reading. Reuses the exact same load path the unsupported-file
        menu's own recipe item does, so both escalations share one code
        path end to end (see open_save's try_recipes contract)."""
        path = getattr(self, "_loaded_path", None)
        if path is None:
            return
        self._open_editor(path, full_sweep=True, try_recipes=True)

    def _show_unsupported_dialog(self, path: Path, message: str):
        """The "not a save format SaveSync can read" notice, with its manual
        escape hatch attached to its OWN button rather than chained after a
        second, disconnected popup — a QMessageBox has to close on any
        button click before anything else can show, which is exactly what
        made "Open as…" feel like two unrelated dialogs instead of one menu
        opening off the button that offers it. Same icon-and-text layout a
        QMessageBox uses, just a plain QDialog underneath so "Open as…" can
        pop its QMenu straight from itself while the notice is still up.
        """
        from PySide6.QtWidgets import QStyle

        dlg = QDialog(self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setWindowTitle(t("cheats.title"))
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(16)

        body = QHBoxLayout()
        body.setSpacing(16)
        icon_lbl = QLabel()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        icon_px = scaled(32, self)
        icon_lbl.setPixmap(icon.pixmap(icon_px, icon_px))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.addWidget(icon_lbl, 0)
        lbl = QLabel(message)
        lbl.setWordWrap(True)
        lbl.setMinimumWidth(scaled(340, self))
        lbl.setMaximumWidth(scaled(420, self))
        body.addWidget(lbl, 1)
        outer.addLayout(body)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        open_btn = QPushButton(t("cheats.open_as"))
        open_btn.setMinimumWidth(scaled(90, self))
        open_btn.clicked.connect(
            lambda: self._exec_unsupported_menu(path, open_btn, dlg))
        btn_row.addWidget(open_btn)
        ok_btn = QPushButton(t("common.ok"))
        ok_btn.setMinimumWidth(scaled(90, self))
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        outer.addLayout(btn_row)

        dlg.exec()

    def _exec_unsupported_menu(self, path: Path, anchor_btn, dlg):
        """Both ways forward, in one menu anchored right under the "Open
        as…" button that raised it: check the few readers the automatic
        pass leaves out for cost, or name the engine directly and skip
        guessing. Picking either closes the notice dialog and retries."""
        from PySide6.QtWidgets import QMenu
        from core.engines.game_engine import known_engines, label as engine_label

        def _pick(retry):
            dlg.accept()
            retry()

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
            f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:{scaled(11, self)}px;}}"
            f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        menu.addAction(
            t("cheats.try_full_sweep"),
            lambda checked=False, p=path: _pick(
                lambda: self._open_editor(p, full_sweep=True)))
        menu.addAction(
            t("cheats.try_recipes"),
            lambda checked=False, p=path: _pick(
                lambda: self._open_editor(p, full_sweep=True, try_recipes=True)))
        menu.addSeparator()
        for eng in known_engines():
            menu.addAction(
                engine_label(eng) or eng,
                lambda checked=False, e=eng, p=path: _pick(
                    lambda: self._open_editor(p, forced_engine=e)))
        menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomLeft()))

    def _open_editor_body(self, path: Path, gen: int = 0, forced_engine: str = "",
                          full_sweep: bool = False, try_recipes: bool = False):
        if gen != getattr(self, "_save_load_gen", 0):
            return

        from ui.widgets.busy_overlay import BusyOverlay
        overlay = BusyOverlay(self, t("common.please_wait"), shelvable=True)
        overlay._reveal_after_s = 0
        overlay.reveal()
        # Most opens land in a second or two even for a format whose unlock
        # can, in the worst case, search (Wolf LZ4's seed formula, a quick
        # Easy Save 3 / Unreal key hit) — the generic "please wait" covers
        # that. Past _DECRYPT_HINT_AFTER_S, this is very likely the actual
        # expensive path (formula miss + brute force, unpacking a game's
        # archives, scanning a binary for a key), which can run minutes —
        # worth saying so explicitly rather than leaving the same one-line
        # message sitting there unchanged. See _on_save_load_progress.
        overlay._decrypt_hint_shown = False
        self._save_load_busy = overlay

        # An explicit "open as..." choice wins over everything else — the
        # user is answering the question auto-detection couldn't. Otherwise
        # the library's own answer, when there is one — see open_save()'s
        # docstring: trusted over re-detecting it from game_dir, which is
        # what left a game the library already knew the engine for opening
        # its own saves as "unsupported".
        known_engine = forced_engine
        if not known_engine and self._entry is not None:
            from core.engines.game_engine import engine_for_game
            known_engine = engine_for_game(self._entry)
        worker = _SaveLoadWorker(path, self._game_dir(), self, engine=known_engine,
                                 full_sweep=full_sweep, try_recipes=try_recipes)
        self._save_load_worker = worker

        overlay.on_shelve = lambda: self._shelved_load_start(overlay, worker)
        # Registering on_cancel (rather than wiring the button straight to
        # worker.cancel) is what tells the overlay someone else owns
        # closing it — worker.cancel() only asks the search to stop; it can
        # still be mid-round for a couple more seconds, and the overlay now
        # stays up saying so instead of vanishing early (see
        # BusyOverlay._on_cancel). _on_save_load_finished closes it for
        # real once worker.finished actually arrives, cancelled or not.
        overlay.on_cancel = worker.cancel
        worker.progress.connect(lambda el: self._on_save_load_progress(el, overlay))
        worker.finished.connect(
            lambda doc, err, p=path, ov=overlay, wk=worker, g=gen, fe=forced_engine, tr=try_recipes: (
                self._on_save_load_finished(doc, err, p, ov, wk, g, fe, tr)
            ))
        worker.start()

    def _on_save_load_progress(self, elapsed: float, overlay):
        if (overlay is not None and not getattr(overlay, "_decrypt_hint_shown", True)
                and elapsed >= _DECRYPT_HINT_AFTER_S):
            overlay._decrypt_hint_shown = True
            overlay.set_base_text(t("common.please_wait_decrypt"))
        if overlay is not None and not getattr(overlay, "_shelved", False):
            overlay.tick(elapsed)
        elif overlay is not None and getattr(overlay, "_shelved", False):
            self._shelved_load_tick(elapsed)

    def _on_save_load_finished(self, doc, err, path: Path, overlay, worker, gen: int = 0,
                               forced_engine: str = "", try_recipes: bool = False):
        if gen != getattr(self, "_save_load_gen", 0):
            try:
                overlay.close_overlay()
            except Exception:
                pass
            return

        shelved = getattr(overlay, "_shelved", False)
        cancelled = getattr(overlay, "_cancelled", False) or getattr(worker, "_is_cancelled", False)
        try:
            overlay.close_overlay()
        except Exception:
            pass
        self._save_load_busy = None
        self._save_load_worker = None
        notice = getattr(self, "_load_notice", None)

        if err is not None or doc is None:
            self._doc = None
            self._loaded_path = None
            self._loaded_mtime = 0
            if notice is not None:
                notice.hide_cancel()
                notice.finish(
                    t("cheats.loading_cancelled")
                    if cancelled
                    else t("cheats.loading_failed"),
                    hide_after_ms=4000)
            if err is not None and not cancelled and not shelved:
                # "err_unreadable" is the one genuine "nothing recognised
                # this at all" case — the others (empty file, can't read,
                # a KNOWN format SaveSync deliberately won't edit, …) have
                # a specific reason a different engine wouldn't fix, so the
                # offer to try one only makes sense here.
                if getattr(err, "key", "") == "cheats.err_unreadable":
                    self._show_unsupported_dialog(path, explain(err))
                else:
                    warning_window_modal(self, t("cheats.title"), explain(err))
            self.show_step(self.STEP_SAVES)
            return

        # The untouched-file safety copy already happened in the worker
        # thread, BEFORE decompression/detection ever ran (see
        # _SaveLoadWorker.run) — a failure there raised and reached this
        # method through the same err-is-not-None branch above, so doc
        # being non-None here already means a snapshot exists.
        self._doc = doc
        self._loaded_path = path
        try:
            self._loaded_mtime = path.stat().st_mtime_ns
        except OSError:
            self._loaded_mtime = 0
        # A manual pick or full sweep that worked is worth remembering: the
        # next save from this game should not need the same detour. Only
        # when it actually says something auto-detection didn't already
        # have right — a shared/generic reader (plain JSON, key=value text)
        # names no engine of its own, and re-guessing from that would be a
        # downgrade, not a correction.
        if self._entry is not None:
            from core.save_editor import registry as _fmt_registry
            learned = forced_engine or _fmt_registry.engine_of(type(doc._fmt))
            if learned and learned != (self._entry.engine or ""):
                self._entry.engine = learned
                get_library().update_game(self._entry)
        if notice is not None and shelved:
            notice.hide_cancel()
            notice.finish(t("cheats.loading_done", name=path.name),
                          hide_after_ms=0)
            notice.set_activatable(True)
        ro = bool(getattr(self._doc, "read_only", False))
        from core.save_editor.recipe_format import RecipeFormat
        is_recipe_doc = isinstance(getattr(self._doc, "_fmt", None), RecipeFormat)
        if ro:
            self._subtitle.setText(t("cheats.editing_read_only",
                                     name=path.name, engine=self._doc.engine))
            hint = t("cheats.read_only_hint", engine=self._doc.engine)
            # try_recipes was already asked for on THIS load and still came
            # back read-only — the escalation ran and found nothing better,
            # which is worth saying rather than looking identical to never
            # having tried.
            if try_recipes:
                hint = f"{t('cheats.recipes_no_better')} {hint}"
            self._edit_hint.setText(hint)
            # Never offered over a save the recipe battery itself already
            # produced — retrying a retry offers nothing.
            self._recipe_retry_btn.setVisible(not is_recipe_doc)
        else:
            self._subtitle.setText(t("cheats.editing",
                                     name=path.name, engine=self._doc.engine))
            self._edit_hint.setText(t("cheats.edit_hint"))
            self._recipe_retry_btn.setVisible(False)
        self._save_btn.setEnabled(not ro)
        self._save_btn.setToolTip(
            t("cheats.read_only_hint", engine=self._doc.engine) if ro else "")
        self._field_filter.clear()
        self._fill_groups()
        self._render_page()
        self._reset_idle_save_timer()
        if not shelved:
            self.show_step(self.STEP_EDIT)


    # ── shelved load: report in the sidebar notice instead of the sheet ────

    def set_load_notice(self, notice):
        """The sidebar BatchProgressNotice that reports shelved loads."""
        self._load_notice = notice

    def on_sidebar_notice_clicked(self):
        """Clicked sidebar notice: restore please-wait if still loading, or open editor if done."""
        worker = getattr(self, "_save_load_worker", None)
        busy = getattr(self, "_save_load_busy", None)
        if (worker is not None and worker.isRunning()) or (busy is not None and getattr(busy, "_shelved", False) and self._doc is None):
            self._unshelve_load()
        elif self._doc is not None:
            self.show_step(self.STEP_EDIT)
            self._close_load_notice()


    def reopen_loaded_save(self):
        self.on_sidebar_notice_clicked()

    def _unshelve_load(self):
        """Bring the Please Wait overlay back onto the screen."""
        self.show_step(self.STEP_EDIT)
        if self._save_load_busy is not None:
            self._save_load_busy.unshelve()
        notice = getattr(self, "_load_notice", None)
        if notice is not None:
            notice.hide()

    def _shelved_load_start(self, busy, worker=None):
        """The sheet was put away — return to saves list and hand reporting to the sidebar notice."""
        if self._entry is not None:
            self.show_step(self.STEP_SAVES)
        elif self._loose is not None:
            self._show_loose(self._loose)
        else:
            self.show_step(self.STEP_PICK)

        notice = getattr(self, "_load_notice", None)
        if notice is None:
            return
        notice.show_indeterminate(t("cheats.loading_side"))
        notice.set_activatable(True)
        def _cancel_and_abort():
            if worker is not None:
                worker.cancel()
            busy._on_cancel()
        notice.set_cancel(t("common.cancel_search"), _cancel_and_abort, min_seconds=30)

    def _shelved_load_tick(self, elapsed):
        notice = getattr(self, "_load_notice", None)
        if notice is None:
            return
        notice.check_cancel_elapsed(elapsed)
        sec = int(elapsed)
        if sec != getattr(self, "_last_shelved_sec", -1):
            self._last_shelved_sec = sec
            notice.set_indeterminate_text(
                f"{t('cheats.loading_side')} ({sec}s)")


    def _close_load_notice(self):
        notice = getattr(self, "_load_notice", None)
        if notice is not None:
            notice.hide_cancel()
            notice.finish(hide_after_ms=0)
            notice.hide()



    # ── the value list, a page at a time ─────────────────────────────────────

    def _visible_fields(self) -> list:
        """The fields the chosen group and the filter leave, across the whole
        save — the pager walks THIS list, so narrowing narrows the pages
        rather than hiding rows inside them."""
        fields = self._doc.fields
        group = self._group_combo.currentData() or ""
        if group:
            fields = [f for f in fields if f.group == group]
        q = self._field_filter.text().strip().casefold()
        if q:
            if getattr(self, "_filter_by_value", False):
                # Match the value AS IT IS IN THE SAVE, not the unsaved edit:
                # a filter has to keep its set stable while you change values
                # inside it, or the row you just edited drops out from under
                # you on the next repaint.
                fields = [f for f in fields
                          if q in str(f.value).casefold()]
            else:
                fields = [f for f in fields if q in f.label.casefold()]
        return fields

    def _fill_groups(self):
        """One entry per group the save actually has, in the order it keeps
        them. Read off the document rather than from a list of engines, so a
        format nobody anticipated still gets a selector that works.
        """
        groups = []
        for f in self._doc.fields:
            if f.group and f.group not in groups:
                groups.append(f.group)
        # If nearly every value is its own category, they are not categories.
        # A save that is one flat list of flags would otherwise offer
        # thousands of entries holding one value each.
        if len(groups) * 2 > len(self._doc.fields):
            groups = []
        self._group_combo.blockSignals(True)
        self._group_combo.clear()
        self._group_combo.addItem(t("cheats.all_groups"), "")
        for g in groups:
            self._group_combo.addItem(_group_label(g), g)
        # With one group there is nothing to choose between.
        self._group_combo.setVisible(len(groups) > 1)
        self._group_combo.blockSignals(False)

    def _apply_group(self):
        self._page = 0
        self._render_page()

    def _row_prefix(self) -> str:
        """What every row of the chosen group would repeat, so it can come off.

        The group's own name always does. Beyond that, an engine tends to keep
        a whole category in one container — RPG Maker's switches and its
        variables each live in an ivar called ``data`` — and that repeats on
        every row too. It is worked out from the group rather than from the
        rows the filter left, so names do not shift about as you type.
        """
        group = self._group_combo.currentData() or ""
        if not group:
            return ""
        labels = [f.label for f in self._doc.fields if f.group == group]
        if not labels:
            return ""
        # Cut only where a name is actually divided, so nothing is chopped
        # mid-word. Each separator of the first label is tried in turn and the
        # longest one every label shares wins — never the last piece, or the
        # rows would be left with no name at all.
        parts = _SEPARATOR.split(labels[0])    # name, sep, name, sep, name
        best = ""
        for i in range(1, len(parts) - 1, 2):
            token = "".join(parts[:i + 1])
            if not all(lab.startswith(token) for lab in labels):
                break
            best = token
        return best

    def _render_page(self):
        self._clear(self._fields_col)
        self._editors = {}
        # Cancels any row-insertion job still in flight from a PREVIOUS
        # render — _begin_async_rows below does this too, but only once
        # this function actually reaches it. The early return just below
        # skips that, and without cancelling here first, an old job's
        # remaining chunks keep landing (via their own queued QTimer ticks)
        # in the column that was just cleared, resurfacing rows a moment
        # after a later, real render already drew the current ones —
        # visible as fields duplicated in the list.
        self._cancel_row_insert()
        if self._doc is None:
            # Reachable mid-reload: _open_editor's own reset clears _doc
            # before the fresh document is back, but along the way it also
            # clears widgets (e.g. the field filter) whose signals are
            # still connected to this same render path — a filter box that
            # had text in it fires textChanged on .clear(), landing here
            # with nothing loaded yet. Nothing to show until the load
            # finishes and calls this again.
            return
        fields = self._visible_fields()
        self._prefix = self._row_prefix()      # once, not once per row
        pages = max(1, (len(fields) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._page = max(0, min(self._page, pages - 1))
        start = self._page * _PAGE_SIZE
        self._page_lbl.setText(t("cheats.page_of", page=self._page + 1,
                                 pages=pages, total=len(fields)))
        self._fit_page_label(self._page_lbl)
        self._first_btn.setEnabled(self._page > 0)
        self._prev_btn.setEnabled(self._page > 0)
        self._next_btn.setEnabled(self._page < pages - 1)
        self._last_btn.setEnabled(self._page < pages - 1)
        if not fields:
            self._cancel_row_insert()
            self._add_note(self._fields_col, t("cheats.no_values"))
            self._sync_hold_label()
            return
        jobs = []
        for f in fields[start:start + _PAGE_SIZE]:
            def _field(f=f):
                return self._fields_col, self._field_row(f)
            jobs.append(_field)
        self._begin_async_rows(jobs, on_done=self._sync_hold_label)

    def _field_row(self, f) -> QWidget:
        row = QFrame()
        row.setObjectName("cheats_field")
        line = QHBoxLayout(row)
        line.setContentsMargins(12, 5, 12, 5)
        line.setSpacing(10)
        # With a group chosen every row would open with the same prefix, which
        # is noise. It comes off the text only: the hold key and the tooltip
        # stay the full label, because that is what identifies the value.
        shown = f.label
        if self._prefix and shown.startswith(self._prefix):
            shown = shown[len(self._prefix):]
        name = ElidedLabel(shown)
        name.setObjectName("cheats_field_name")
        name.setToolTip(f.label)
        line.addWidget(name, 1)
        line.addWidget(self._editor_for(f))

        # Open/closed padlock. Marks stay selected; the re-apply loop runs
        # only while THIS game is running (see _watch_hold_game).
        # Read-only documents cannot be written, so the lock is inert — showing
        # it as a usable control would promise a rewrite that will never run.
        marked = f.label in self._held
        hold = QPushButton("🔒" if marked else "🔓")
        hold.setObjectName("cheats_hold_btn")
        hold.setCheckable(True)
        hold.setFixedSize(scaled(24, self), scaled(24, self))
        hold.setChecked(marked)
        if getattr(self._doc, "read_only", False):
            hold.setEnabled(False)
            hold.setCursor(Qt.CursorShape.ArrowCursor)
            hold.setToolTip(t("cheats.hold_tip_read_only"))
        else:
            hold.setCursor(Qt.CursorShape.PointingHandCursor)
            hold.setToolTip(t("cheats.hold_tip"))
            hold.toggled.connect(
                lambda on, fld=f, btn=hold: self._toggle_hold(fld, on, btn))
        line.addWidget(hold)
        return row

    def _step_page(self, delta: int):
        self._page += delta
        self._render_page()
        self._reset_idle_save_timer()

    def _jump_page(self, index: int):
        """Go straight to page *index* (0-based) — or the last page, for
        any negative index, without needing the page count up front:
        _render_page's own clamp finds it. Backs «, », and the editable
        page number alike."""
        self._page = index if index >= 0 else 1 << 30
        self._render_page()
        self._reset_idle_save_timer()

    def _editor_for(self, f):
        # An edit made on one page must survive turning to another, so every
        # change is recorded as it happens: the widgets are rebuilt per page,
        # the pending values are not.
        current = self._pending.get(f.path, f.value)
        if f.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(current))
            w.toggled.connect(lambda v, p=f.path: self._remember(p, bool(v)))
        elif f.kind == "int":
            w = QSpinBox()
            w.setRange(-2_147_483_648, 2_147_483_647)
            w.setValue(int(current))
            w.valueChanged.connect(lambda v, p=f.path: self._remember(p, int(v)))
        elif f.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(4)
            w.setRange(-1e12, 1e12)
            w.setValue(float(current))
            w.valueChanged.connect(lambda v, p=f.path: self._remember(p, float(v)))
        else:
            w = QLineEdit(str(current))
            w.setMinimumWidth(scaled(180, self))
            w.textChanged.connect(lambda v, p=f.path: self._remember(p, v))
        w.setObjectName("cheats_value")
        if getattr(self._doc, "read_only", False):
            w.setEnabled(False)
            w.setToolTip(t("cheats.read_only_hint", engine=self._doc.engine))
        self._editors[f.path] = (w, f.kind)
        return w

    def _remember(self, path, value):
        self._pending[path] = value
        self._reset_idle_save_timer()
        # A held field's target tracks what you type, so Apply picks up the
        # latest value rather than whatever was held before — but this is
        # bookkeeping only. It is NOT pushed into a currently-running hold:
        # nothing this page does should reach the file before Apply says so.
        label = next((f.label for f in self._doc.fields if f.path == path), "")
        if label in self._held:
            self._held[label] = value

    def _apply_field_filter(self, _text: str):
        self._page = 0
        self._render_page()
        self._reset_idle_save_timer()

    def _sync_filter_mode(self):
        """Reflect the current name/value mode on the toggle and placeholder."""
        by_value = getattr(self, "_filter_by_value", False)
        self._filter_mode_btn.setText("#" if by_value else "Aa")
        self._filter_mode_btn.setToolTip(
            t("cheats.filter_mode_value") if by_value
            else t("cheats.filter_mode_name"))
        self._field_filter.setPlaceholderText(
            t("cheats.filter_by_value") if by_value
            else t("cheats.filter_by_name"))

    def _toggle_filter_mode(self):
        self._filter_by_value = not getattr(self, "_filter_by_value", False)
        self._sync_filter_mode()
        if self._field_filter.text().strip():
            self._page = 0
            self._render_page()

    def _apply_edits(self):
        if self._doc is None or getattr(self._doc, "read_only", False):
            return
        self._reset_idle_save_timer()
        # Pause an active hold across the write: both write this same file.
        was_holding = self._hold is not None and self._hold.is_running()
        if was_holding:
            self._hold.stop()
        from ui.widgets.busy_overlay import busy_over
        try:
            for path, value in self._pending.items():
                self._doc.set_value(path, value)
            # Copying the original aside and re-encoding can be a second or
            # two on a large save; cover the window while it happens.
            with busy_over(self, t("common.please_wait")):
                kept = self._doc.save()
        except Exception as e:
            logger.error(f"Saving edits failed: {e}")
            warning_window_modal(self, t("cheats.title"),
                                 t("cheats.write_failed", error=str(e)))
            if was_holding and self._hold is not None:
                self._hold.start()
            return
        # kept is None when there was nothing left to copy aside — the file
        # this doc loaded from was already gone (renamed, moved) by the
        # time save() ran; see backup_original's own docstring. The write
        # itself still happened, so this is still a real "applied", just
        # named from the doc's own loaded path rather than the copy.
        applied_name = kept.name if kept is not None else self._loaded_path.name
        self._subtitle.setText(t("cheats.applied", name=applied_name))
        running = self._playing()
        # Marks persist across game stop/start; the loop needs Apply first,
        # then runs only while THIS game is up.
        if self._held:
            for f in self._doc.fields:
                if f.label in self._held:
                    self._held[f.label] = self._pending.get(f.path, f.value)
            self._hold_armed = True
            if running:
                self._start_hold()
            else:
                self._stop_hold()
            self._hold_watch.start()
            self._sync_hold_label()
        elif was_holding and self._hold is not None:
            self._hold.start()
        if running and not (self._hold is not None and self._hold.is_running()):
            warning_window_modal(self, t("cheats.title"),
                                 t("cheats.reload_warning", name=running))

    # ── holding values ───────────────────────────────────────────────────────

    def _toggle_hold(self, field, on: bool, btn=None):
        """Toggle a lock mark. Marks stay; the file loop needs Apply + game running."""
        if self._doc is None or getattr(self._doc, "read_only", False):
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)
            return
        if on:
            self._held[field.label] = self._pending.get(field.path, field.value)
            if btn is not None:
                btn.setText("🔒")
        else:
            self._held.pop(field.label, None)
            if btn is not None:
                btn.setText("🔓")
            # Unlocking the LAST field stops the loop outright rather than
            # leaving it running against nothing — that is a safety stop,
            # not a new enforcement, so it does not wait on Apply. Unlocking
            # one of SEVERAL held fields is left running unchanged (still
            # enforcing whatever the last Apply set) until Apply says so —
            # same rule as a value edit: nothing reaches the file early.
            if not self._held:
                self._hold_armed = False
                self._stop_hold()
                self._hold_watch.stop()
        self._sync_hold_label()

    def _start_hold(self):
        from core.save_editor import SaveHold

        if (not self._hold_armed or not self._held
                or not self._playing() or self._doc is None
                or getattr(self._doc, "read_only", False)):
            return
        if self._hold is not None:
            self._hold.set_values(self._held)
            if not self._hold.is_running():
                self._hold.start()
            return
        self._hold = SaveHold(self._doc.path, self._held, self)
        self._hold.reapplied.connect(lambda _n: self._sync_hold_label())
        self._hold.failed.connect(self._on_hold_failed)
        self._hold.start()

    def _stop_hold(self):
        if self._hold is not None:
            self._hold.stop()
            self._hold.deleteLater()
            self._hold = None

    def _watch_hold_game(self):
        """Pause/resume the re-apply loop with the game; keep the lock marks."""
        if not self._held or not self._hold_armed:
            self._stop_hold()
            if not self._held:
                self._hold_watch.stop()
            self._sync_hold_label()
            return
        if self._stack.currentIndex() != self.STEP_EDIT or self._doc is None:
            return
        if self._playing():
            if self._hold is None or not self._hold.is_running():
                self._start_hold()
            self._sync_hold_label()
            return
        # Game closed: stop checking the file, leave locks selected.
        if self._hold is not None:
            self._stop_hold()
            self._sync_hold_label()

    def _on_hold_failed(self, message: str):
        self._stop_hold()
        self._sync_hold_label()
        warning_window_modal(self, t("cheats.title"),
                             t("cheats.hold_failed", error=message))

    def _sync_hold_label(self):
        active = self._hold is not None and self._hold.is_running()
        if not self._held:
            self._hold_lbl.setText("")
            self._hold_lbl.setVisible(False)
            return
        if active:
            rounds = self._hold.rounds
            self._hold_lbl.setText(t("cheats.holding", count=len(self._held),
                                     rounds=rounds))
        else:
            self._hold_lbl.setText(t("cheats.hold_pending", count=len(self._held)))
        self._hold_lbl.setVisible(True)

    # ── Small helpers ────────────────────────────────────────────────────────

    def _stop_deferred_busy(self):
        busy = getattr(self, "_deferred_busy", None)
        if busy is not None:
            busy.close()
            self._deferred_busy = None

    def _cancel_row_insert(self):
        """Invalidate any in-flight chunk pump and drop the please-wait.

        If real work was actually thrown away (the queue was not already
        empty) AND this was the field-row list specifically, the page is
        left knowing the current page of fields is incomplete — see
        show_step, which re-renders it when the edit step is re-entered.
        The game list (STEP_PICK) and save list (STEP_SAVES) this same gen
        counter also covers already self-heal: re-entering either re-runs
        the action that built them in the first place (_refresh_games(),
        picking a game), not just a bare step switch."""
        had_pending_work = bool(getattr(self, "_row_insert_queue", None))
        was_field_rows = self._row_insert_on_done is not None
        self._row_insert_gen = getattr(self, "_row_insert_gen", 0) + 1
        self._row_insert_queue = []
        self._row_insert_on_done = None
        self._stop_deferred_busy()
        if had_pending_work and was_field_rows:
            self._fields_incomplete = True

    def _begin_async_rows(self, jobs: list, on_done=None):
        """Insert list rows in QTimer chunks (same pattern as library cards).

        Each *job* is a zero-arg callable returning ``(column, widget)``.
        Please-wait covers the pump when the page is visible (delay 0).
        """
        self._cancel_row_insert()
        gen = self._row_insert_gen
        self._row_insert_queue = list(jobs)
        self._row_insert_on_done = on_done
        from core.concurrency import library_insert_chunk_size
        cs = library_insert_chunk_size()
        # Match library: never one blocking pump for the whole page.
        self._row_insert_chunk = cs if cs > 0 else 16
        if not self._row_insert_queue:
            if callable(on_done):
                on_done()
            return
        if self.isVisible():
            from ui.widgets.busy_overlay import DeferredBusy
            self._deferred_busy = DeferredBusy(
                self, t("common.please_wait"), delay_ms=200)
            self._deferred_busy.set_on_cancel(self._cancel_row_insert)
        QTimer.singleShot(0, lambda g=gen: self._async_row_step(g))


    def _async_row_step(self, gen: int):
        if gen != getattr(self, "_row_insert_gen", 0):
            return
        queue = getattr(self, "_row_insert_queue", None) or []
        if not queue:
            self._finish_row_insert(gen)
            return
        chunk_n = getattr(self, "_row_insert_chunk", 0) or len(queue)
        chunk = queue[:chunk_n]
        del queue[:chunk_n]
        self._row_insert_queue = queue
        for build in chunk:
            if gen != getattr(self, "_row_insert_gen", 0):
                return
            try:
                col, widget = build()
            except Exception as e:
                logger.debug(f"Save-editor row build failed: {e}")
                continue
            if col is None or widget is None:
                continue
            try:
                self._insert(col, widget)
            except RuntimeError:
                return
        if gen != getattr(self, "_row_insert_gen", 0):
            return
        if self._row_insert_queue:
            QTimer.singleShot(0, lambda g=gen: self._async_row_step(g))
            return
        self._finish_row_insert(gen)

    def _finish_row_insert(self, gen: int):
        if gen != getattr(self, "_row_insert_gen", 0):
            return
        done = self._row_insert_on_done
        self._row_insert_on_done = None
        self._stop_deferred_busy()
        if callable(done):
            try:
                done()
            except Exception as e:
                logger.debug(f"Save-editor row on_done failed: {e}")

    def _insert(self, col: QVBoxLayout, widget: QWidget):
        col.insertWidget(col.count() - 1, widget)

    def _clear(self, col: QVBoxLayout):
        """Empty a list, without any of it becoming a window of its own.

        Detaching a widget from its parent promotes it to a top-level window,
        and it stays one until deleteLater comes round — a real window the
        system can draw. Opening a heavy save takes long enough for that to
        happen, and what appears is a piece of the editor, a lone text field
        or a single row, flashing on screen by itself. Hiding it instead
        leaves the parent as it was: the layout gives a hidden widget no
        room, so the list empties just the same.
        """
        if col is None:
            return
        while col.count() > 1:
            item = col.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            sub = item.layout()
            if sub is not None:
                while sub.count() > 0:
                    sitem = sub.takeAt(0)
                    sw = sitem.widget() if sitem else None
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()

    def _add_note(self, col: QVBoxLayout, text: str):
        lbl = QLabel(text)
        lbl.setObjectName("empty_hint")
        lbl.setWordWrap(True)
        self._insert(col, lbl)

    def update_locale(self):
        self._title.setText(t("cheats.title"))
        self._subtitle.setText(t("cheats.subtitle"))
        self._search.setPlaceholderText(t("cheats.search_placeholder"))
        self._sync_filter_mode()
        self._save_btn.setText(t("cheats.apply"))
        self._kept_lbl.setText(t("cheats.kept_copies"))
        self._files_lbl.setText(t("cheats.pick_save"))
        self._games_size_combo.update_locale()
        self._saves_size_combo.update_locale()
        self._drop.retranslate()
        if self._stack.currentIndex() == self.STEP_SAVES:
            self._render_saves_page()
        if self._doc is not None:
            # The category names are translated too, so they have to be
            # rebuilt rather than left in the language they were made in.
            self._fill_groups()
            self._render_page()
        self.show_step(self._stack.currentIndex())
