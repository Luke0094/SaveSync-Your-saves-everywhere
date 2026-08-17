"""
SaveSync - Library Page
Game library with card/list view toggle, game images, launch button, and detail panel.
"""
import logging

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QComboBox, QToolButton,
    QSizePolicy,
)

from core.library import GameEntry, get_library
from core.config_manager import get_config
from i18n import t
from ui.helpers import PageScrollMixin, lock_min_size, safe_widget as _safe, scaled
from ui.styles.theme import palette, ThemedMixin

from ui.widgets.library_folders import FolderTree, _clean_tag_display
from ui.widgets.game_items import GameCard, GameRow, _display_sync_status, library_card_size
from ui.widgets.page_size import (
    PageSizeCombo, SCOPE_LIBRARY, guarded_render, page_size)
from ui.widgets.search_inputs import ClearableLineEdit

_SORT_NATURAL_DIRECTION = {
    "date_added":  "desc",
    "last_played": "desc",
    "name_asc":    "asc",
    "playtime":    "desc",
    "rating":      "desc",
    "status":      "asc",
    "last_backup": "desc",
}

# Concrete meaning of each criterion's descending / ascending direction, so
# the arrow toggle's tooltip states what the order actually does ("Newest
# first", "A → Z") instead of a bare "Ascending/Descending" that the user
# had to decode. (criterion: (descending_key, ascending_key))
_SORT_DIR_LABELS = {
    "date_added":  ("sort_dir_newest", "sort_dir_oldest"),
    "last_played": ("sort_dir_newest", "sort_dir_oldest"),
    "last_backup": ("sort_dir_newest", "sort_dir_oldest"),
    "name_asc":    ("sort_dir_za",     "sort_dir_az"),
    "playtime":    ("sort_dir_most",   "sort_dir_least"),
    "rating":      ("sort_dir_most",   "sort_dir_least"),
    "status":      ("sort_descending", "sort_ascending"),
}

# Kept for callers that only need "how big is a page by default"; the live
# value is per-list and user-chosen (ui.widgets.page_size).
PAGE_SIZE = 20


def page_numbers(current: int, total: int) -> list[int]:
    """Numbered slots for a pager: always first and last page, 3 variable
    middle slots — 5 numbered buttons in total once there are ≥5 pages."""
    if total <= 5:
        return list(range(1, total + 1))
    if current <= 3:
        middle = [2, 3, 4]
    elif current >= total - 2:
        middle = [total - 3, total - 2, total - 1]
    else:
        middle = [current - 1, current, current + 1]
    return [1] + middle + [total]


def _style_pager_btn(btn, active: bool):
    """Name a pager button so the theme paints it.

    Both looks live in DARK_THEME/LIGHT_THEME as #pager_btn_active and
    #pager_btn. Naming beats a per-button stylesheet here: the pager is
    rebuilt on every page change, in two different pages, and a theme switch
    then needs to do nothing at all to these buttons.
    """
    btn.setObjectName("pager_btn_active" if active else "pager_btn")


def build_pager(current: int, total: int, on_page,
                size_combo=None) -> QWidget:
    """Pager row: ‹  [1] … [N]  ›, optional page-size combo on the right.

    - prev hidden on the first page, next hidden on the last;
    - with a single page and no *size_combo*, the caller must not add this;
    - with *size_combo*, the row is always useful (even on one page) so the
      size control stays on the same line as the page numbers, not above them.
    *on_page* is called with the target page number.

    The buttons take their look from the theme (see _style_pager_btn), so
    callers no longer need to collect them for re-styling on a theme switch.
    A combo may be attached to at most ONE pager (Qt: one parent) — typically
    the top one when a page has two.
    """
    wrap = QWidget()
    wrap.setObjectName("transparent_bg")
    row = QHBoxLayout(wrap)
    row.setContentsMargins(0, 4, 0, 4)
    row.setSpacing(6)
    row.addStretch()

    def _btn(text: str, page: int, active: bool = False, tooltip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFixedHeight(scaled(26, wrap))
        # Fixed width from the glyph — Preferred + a 30px floor let the
        # page-size combo squeeze two-digit numbers when space was tight.
        b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # Scale the whole width (glyph floor + text advance) so the button is
        # registered with one ScaledValue; max(scaled(), raw-int) would degrade
        # to a plain int and never re-scale on DPI changes.
        b.setFixedWidth(scaled(
            max(30, b.fontMetrics().horizontalAdvance(text) + 20), wrap))
        if tooltip:
            b.setToolTip(tooltip)
        _style_pager_btn(b, active)
        b.clicked.connect(lambda _=False, p=page: on_page(p))
        return b

    if total > 1:
        if current > 1:
            row.addWidget(_btn("‹", current - 1, tooltip=t("common.prev_page")))
        for n in page_numbers(current, total):
            row.addWidget(_btn(str(n), n, active=(n == current)))
        if current < total:
            row.addWidget(_btn("›", current + 1, tooltip=t("common.next_page")))

    row.addStretch()
    if size_combo is not None:
        row.addWidget(size_combo)
    return wrap

# Singleton drag state shared between cards and folder tree
class LibraryPage(PageScrollMixin, QWidget, ThemedMixin):
    add_game_requested = Signal(str, str)  # name, exe_path
    scan_folder_requested = Signal()       # 🔍 — scan a folder for games
    folder_dropped = Signal(str)           # drag&drop of a whole folder
    backup_requested   = Signal(str)
    restore_requested  = Signal(str)
    remove_requested   = Signal(str)
    edit_requested     = Signal(str)
    sync_requested     = Signal(str)
    launch_requested   = Signal(str)
    review_provisional_requested = Signal(str)
    cheats_requested   = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._cards: dict[str, QWidget] = {}    # game_id → GameCard or GameRow
        self._view_mode = "card"                  # default: card view
        self._last_per_row: int = 0               # track columns to avoid needless rebuilds
        self._current_page: int = 1               # library pagination (PAGE_SIZE per page)
        # First grid build waits until the page is shown (async). resizeEvent
        # must not sync-rebuild before that, or the page switch freezes.
        self._pending_initial_load = True
        self._initial_load_scheduled = False
        self._build()
        self._connect_library()

    # ── Drag & drop from Desktop ─────────────────────────────────────────────

    @staticmethod
    def _resolve_lnk_target(path: str) -> str:
        """Resolve a .lnk shortcut to its target path (Windows only)."""
        from core.resolvers import resolve_lnk_target
        return resolve_lnk_target(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        from pathlib import Path
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        path = urls[0].toLocalFile()
        p = Path(path)
        # A whole folder dropped in: act like the batch scan — walk it and
        # find one candidate executable per game (first level that has any,
        # descending only while a level is empty, noise like uninstallers
        # excluded). The user still confirms the picks.
        if p.is_dir():
            event.acceptProposedAction()
            self.folder_dropped.emit(str(p))
            return
        # Platform-aware: .exe/.bat/.lnk/.url on Windows, and on Unix the
        # extension-less exec-bit binaries plus .sh/.AppImage/.x86_64/.desktop.
        from core.resolvers import is_addable_file, resolve_desktop_entry
        if not is_addable_file(path):
            return
        event.acceptProposedAction()
        # Same name derivation as every other add entry point (dialog drop /
        # file picker): a generic exe stem ("nw", "game"…) walks up to the
        # install-folder name; a shortcut keeps its filename stem.
        from core.save_detector import display_name_for_added_file
        name = display_name_for_added_file(path)
        # For .url files, extract the URL for appid
        if p.suffix.lower() == '.url':
            try:
                content = p.read_text(encoding='utf-8')
                for line in content.splitlines():
                    if line.lower().startswith('url='):
                        exe_path = line[4:].strip()
                        break
                else:
                    exe_path = path
            except Exception:
                exe_path = path
        elif p.suffix.lower() == '.desktop':
            # Linux launcher: same role as .lnk — resolve to what it starts.
            exe_path = resolve_desktop_entry(path) or path
        elif p.suffix.lower() == '.lnk':
            exe_path = self._resolve_lnk_target(str(p))
            _t = (exe_path or "").strip().strip('"')
            if _t and Path(_t).is_dir():
                # Folder shortcut: a directory must never be prefilled as the
                # executable — and it is exactly the "drop a whole folder"
                # case, so run the batch scan on it.
                self.folder_dropped.emit(str(Path(_t)))
                return
        else:
            exe_path = path
        self.add_game_requested.emit(name, exe_path)

    # ── Responsive grid: rebuild when column count changes ────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._view_mode == "card":
            per_row = self._calc_per_row()
            if per_row != self._last_per_row:
                self._last_per_row = per_row
                # First show always resizes (0 → N columns). Rebuilding here
                # runs synchronously during the page switch and is exactly the
                # pre-enter stale we deferred the load to avoid — remember the
                # column count; the pending/async load will use it.
                if self._pending_initial_load or self._initial_load_scheduled:
                    return
                if not self._cards and not (getattr(self, "_insert_queue", None) or []):
                    return
                self._rebuild_view()

    def _calc_per_row(self) -> int:
        """Calculate how many DPI-scaled cards fit in the current scroll width."""
        # Available width: total widget minus folder sidebar, margins, scrollbar
        available = self.width() - scaled(170, self) - 16 - 10
        cw, _ = library_card_size(self)
        card_w = cw + 12  # card width + spacing
        return max(1, available // card_w)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        # Header row
        top = QHBoxLayout()
        self._header = QLabel(t("library.title"))
        self._header.setObjectName("page_header")
        top.addWidget(self._header)
        top.addStretch()

        # View toggle — cards first (default)
        self._card_btn = QPushButton(t("buttons.card_view_icon"))
        self._card_btn.setObjectName("icon_btn")
        self._card_btn.setFixedSize(scaled(30, self), scaled(30, self))
        self._card_btn.setToolTip(t("tooltips.card_view"))
        self._card_btn.clicked.connect(lambda: self._set_view("card"))

        self._list_btn = QPushButton(t("buttons.list_view_icon"))
        self._list_btn.setObjectName("icon_btn")
        self._list_btn.setFixedSize(scaled(30, self), scaled(30, self))
        self._list_btn.setToolTip(t("tooltips.list_view"))
        self._list_btn.clicked.connect(lambda: self._set_view("list"))

        top.addWidget(self._card_btn)
        top.addWidget(self._list_btn)

        # Scan a folder for installed games — the bulk counterpart of "+ Add".
        self._scan_btn = QPushButton("🔍")
        # toolbar_icon_btn, not the transparent icon_btn: this one sits beside
        # "+ Add game" in the page header, and with no chrome the lone glyph
        # read as part of the background rather than as a button.
        self._scan_btn.setObjectName("toolbar_icon_btn")
        self._scan_btn.setFixedSize(scaled(34, self), scaled(34, self))
        self._scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_btn.setToolTip(t("exe_scan.button_tooltip"))
        self._scan_btn.clicked.connect(self.scan_folder_requested.emit)
        top.addWidget(self._scan_btn)

        self._add_btn = QPushButton(f"+ {t('library.add_game')}")
        self._add_btn.setObjectName("primary_btn")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(lambda: self.add_game_requested.emit("", ""))
        top.addWidget(self._add_btn)
        root.addLayout(top)

        # Filter row: search-mode + search + sort criterion + sort direction
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        # Search mode selector (left of search bar)
        self._search_mode = QComboBox()
        self._search_mode.setObjectName("library_tool_combo")
        self._search_mode.setFixedWidth(scaled(100, self))
        self._search_mode.addItem(t("library.search_by_title"),     "title")
        self._search_mode.addItem(t("library.search_by_developer"), "developer")
        filter_row.addWidget(self._search_mode)

        self._search = ClearableLineEdit()
        self._search.setPlaceholderText(t("library.search_placeholder"))
        # The search bar used to collapse to a sliver at narrow widths (the
        # stretch absorbs the shrink before any scrollbar steps in) — keep a
        # usable footprint so the page scrolls instead.
        lock_min_size(self._search, w=scaled(200, self, min_px=160))
        # Debounce: rebuilds show a "please wait" sheet; without a pause the
        # grid rebuilds on every keystroke while typing.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(320)
        self._search_timer.timeout.connect(self._apply_search_filter)
        self._search.textChanged.connect(self._on_search_text_changed)
        # Mode change is a deliberate click — apply at once, no debounce.
        self._search_mode.currentIndexChanged.connect(
            lambda _: self._apply_search_filter())
        filter_row.addWidget(self._search, 1)

        # Sort combo (criterion) + direction dropdown (asc/desc, resets to
        # a sensible default for the criterion whenever it changes)
        self._sort_combo = QComboBox()
        self._sort_combo.setFixedWidth(scaled(130, self))
        self._populate_sort_combo()
        saved_sort = get_config().get("library_sort", "date_added")
        for i in range(self._sort_combo.count()):
            if self._sort_combo.itemData(i) == saved_sort:
                self._sort_combo.setCurrentIndex(i)
                break
        filter_row.addWidget(self._sort_combo)

        # Direction: a compact arrow toggle (↓ descending / ↑ ascending)
        # instead of an "Ascending/Descending" dropdown. The arrow plus a
        # concrete tooltip ("Newest first", "A → Z", …) says what the order
        # actually means for the current criterion — the generic words did not.
        saved_dir = get_config().get("library_sort_direction", "")
        self._sort_dir = saved_dir if saved_dir in ("asc", "desc") \
            else _SORT_NATURAL_DIRECTION.get(saved_sort, "desc")
        self._sort_dir_btn = QToolButton()
        self._sort_dir_btn.setObjectName("library_sort_dir")
        self._sort_dir_btn.setFixedWidth(scaled(34, self))
        self._sort_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sort_dir_btn.clicked.connect(self._on_sort_dir_toggle)
        self._update_sort_dir_btn()
        filter_row.addWidget(self._sort_dir_btn)

        # Criterion change resets direction to that criterion's natural
        # default (e.g. Name → A-Z, Added → newest first) instead of
        # keeping whatever direction the PREVIOUS criterion was left on.
        self._sort_combo.currentIndexChanged.connect(self._on_sort_criterion_changed)

        root.addLayout(filter_row)

        # Lives on the pager row (created in _rebuild_view_inner), not above it.
        self._page_size_combo = PageSizeCombo(
            SCOPE_LIBRARY, self._on_page_size_changed)
        # Incremental card/row insert — bumped on every rebuild so a stale
        # QTimer chunk from a previous filter keystroke is dropped.
        # Chunk size comes from core.concurrency (0 = whole page sync on
        # capable machines — artificial chunking would only slow them down).
        self._insert_gen = 0

        # Body: folder sidebar + game grid
        body = QHBoxLayout()
        body.setSpacing(0)

        # Folder tree sidebar
        self._folder_tree = FolderTree()
        self._folder_tree.folder_selected.connect(self._on_folder_selected)
        self._folder_tree.tags_changed.connect(self._on_tags_changed)
        self._folder_tree.engines_changed.connect(self._on_tags_changed)
        body.addWidget(self._folder_tree)

        # Scroll area for game cards/rows
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("transparent_bg")
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(8)
        self._layout.setContentsMargins(8, 0, 0, 0)
        self._layout.addStretch()

        self._scroll.setWidget(self._container)
        self._register_page_scroll(self._scroll, list_content=True)

        # Empty / no-results label — shown instead of the scroll area, at top
        self._empty_lbl = QLabel(t("library.empty"))
        self._empty_lbl.setObjectName("library_empty")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setVisible(False)
        body.addWidget(self._empty_lbl)
        body.addWidget(self._scroll, 1)

        root.addLayout(body, 1)

        self._apply_view_btn_styles()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_initial_load()

    def on_page_enter(self):
        """Kick the first grid build or refresh without blocking page switch."""
        self._schedule_initial_load()

    def ensure_loaded(self):
        """Kick the first grid build without blocking the page switch."""
        self._schedule_initial_load()

    def _schedule_initial_load(self):
        if not self._pending_initial_load or self._initial_load_scheduled:
            return
        self._initial_load_scheduled = True
        if self.isVisible() and getattr(self, "_deferred_busy", None) is None:
            from ui.widgets.busy_overlay import DeferredBusy
            self._deferred_busy = DeferredBusy(self, t("common.please_wait"), delay_ms=0)
        # Let the page paint empty first, then fill in the background.
        QTimer.singleShot(0, self._load_library)

    def _load_library(self):
        self._pending_initial_load = False
        self._initial_load_scheduled = False
        self._rebuild_view()

    def on_page_leave(self):
        """Release off-screen rendered cover caches and stop any deferred busy."""
        self._stop_deferred_busy()
        try:
            from ui.widgets.game_items import trim_cover_cache
            trim_cover_cache()
        except Exception:
            pass

    def wipe_and_reload(self):
        """Drop every built widget and re-arm the initial load: the next
        visit (showEvent / ensure_loaded) rebuilds the grid through the
        async chunk pump. Called by the overview refresh button."""
        self._insert_gen = getattr(self, "_insert_gen", 0) + 1
        self._insert_queue = []
        # Same detach-before-wipe as _rebuild_view_inner: the combo lives
        # inside the top pager and would be destroyed with it.
        if getattr(self, "_page_size_combo", None) is not None:
            self._page_size_combo.setParent(self)
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._pending_initial_load = True
        self._initial_load_scheduled = False
        self._stop_deferred_busy()
        if self.isVisible():
            self._load_library()

    def _set_view(self, mode: str):
        if mode == self._view_mode and self._cards:
            return
        self._view_mode = mode
        self._apply_view_btn_styles()
        self._rebuild_view()

    def _apply_view_btn_styles(self):
        """Highlight the active view-toggle button. State (view mode) + palette
        dependent, so it is re-applied from refresh_styles on a theme switch."""
        _active = f"background:{palette('bg_button')};color:{palette('accent')};border:1px solid {palette('accent')};"
        _idle   = ""
        self._card_btn.setStyleSheet(_active if self._view_mode == "card" else _idle)
        self._list_btn.setStyleSheet(_active if self._view_mode == "list" else _idle)

    def _rebuild_view(self):
        """Destroy and recreate all widgets in the selected view mode.

        Layout wipe is cheap; card insert runs in QTimer chunks so entering
        the page never blocks on cover decode. Please-wait covers wipe +
        insert immediately (delay 0) so the empty grid is not visible while
        cards build.
        """
        from ui.widgets.page_size import guarded_render, SCOPE_LIBRARY
        from ui.widgets.busy_overlay import DeferredBusy
        self._pending_initial_load = False
        # Cover wipe + async insert; recreate so reveal is fresh each rebuild.
        self._stop_deferred_busy()
        if self.isVisible():
            self._deferred_busy = DeferredBusy(
                self, t("common.please_wait"), delay_ms=0)
        with guarded_render(SCOPE_LIBRARY):

            self._rebuild_view_inner()

    def _on_page_size_changed(self, _size: int):
        """Page 1 is the only sane place to land: every item has just moved to
        a different page, and the old number can be past the end."""
        self._current_page = 1
        # Same RAM cleanup as the numbered pager: a different page set is
        # about to render, so the previous one's covers can go.
        try:
            from ui.widgets.game_items import trim_cover_cache
            trim_cover_cache()
        except Exception:
            pass
        self._rebuild_view()
        self._scroll.verticalScrollBar().setValue(0)

    def _rebuild_view_inner(self):
        # Invalidate any in-flight chunk insert before tearing the layout down,
        # or a stale QTimer could append cards to the new tree.
        self._insert_gen = getattr(self, "_insert_gen", 0) + 1
        self._insert_queue = []
        # Detach before the wipe — the combo lives inside the top pager and
        # would otherwise be destroyed with it on every rebuild.
        if getattr(self, "_page_size_combo", None) is not None:
            self._page_size_combo.setParent(self)
        # Remove all existing widgets
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._cards.clear()
        lib = get_library()
        all_games = lib.all_games()

        # Collect all tags and update the sidebar tag panel
        all_tags = []
        for g in all_games:
            all_tags.extend(_clean_tag_display(x) for x in g.tags)
        self._folder_tree.update_tags(all_tags)

        # …and the engines behind them, by label: the sidebar shows "RPG
        # Maker", not "rpgmaker", and an engine typed in by hand shows as
        # typed (see engine_display).
        from core.engines.game_engine import engine_display, engine_for_game
        engine_labels = {}
        unknown_ids = set()
        for g in all_games:
            shown = engine_display(engine_for_game(g))
            if shown:
                engine_labels[g.id] = shown
            else:
                unknown_ids.add(g.id)
        self._folder_tree.update_engines(
            list(engine_labels.values()), has_unknown=bool(unknown_ids))

        games = self._sort_games(all_games)

        # Apply folder filter from sidebar
        folder_filter = self._folder_tree._selected_path
        if folder_filter and folder_filter != "__all__":
            games = [g for g in games if g.category == folder_filter
                     or g.category.startswith(folder_filter + "/")]

        # Apply tag filter (3-state: include=green must have, exclude=red must
        # not have). Matching uses tag_merge_key (case- and separator-
        # insensitive): the sidebar shows one canonical entry per tag
        # ("2D Game"), but games may still store any variant ("2d-game",
        # "adventure") — those must keep matching.
        from core.library import tag_merge_key
        selected_tags = self._folder_tree.get_selected_tags()
        excluded_tags = self._folder_tree.get_excluded_tags()
        if selected_tags:
            _sel_cf = {tag_merge_key(x) for x in selected_tags}
            games = [g for g in games if _sel_cf.issubset(
                {tag_merge_key(_clean_tag_display(x)) for x in g.tags})]
        if excluded_tags:
            _exc_cf = {tag_merge_key(x) for x in excluded_tags}
            games = [g for g in games if not _exc_cf.intersection(
                {tag_merge_key(_clean_tag_display(x)) for x in g.tags})]

        # Engine filter, same three states. A game has exactly one engine, so
        # "include" is a membership test rather than the subset test tags need.
        # The Others chip matches titles whose engine was not identified.
        others_cf = self._folder_tree.engine_others_label().casefold()
        selected_engines = {x.casefold()
                            for x in self._folder_tree.get_selected_engines()}
        excluded_engines = {x.casefold()
                            for x in self._folder_tree.get_excluded_engines()}

        def _engine_chip_for(g) -> str:
            label = engine_labels.get(g.id, "")
            return label.casefold() if label else others_cf

        if selected_engines:
            games = [g for g in games if _engine_chip_for(g) in selected_engines]
        if excluded_engines:
            games = [g for g in games
                     if _engine_chip_for(g) not in excluded_engines]

        # Apply text search filter inline so the grid reflows without gaps.
        # Filters/search run on the FULL library — pagination is applied
        # last, so a match on any page is always reachable.
        q = getattr(self, '_search', None)
        sm = getattr(self, '_search_mode', None)
        if q:
            q_text = q.text().lower().strip()
            if q_text:
                mode = sm.currentData() if sm else "title"
                if mode == "developer":
                    games = [g for g in games if q_text in (g.developer or "").lower()]
                else:
                    games = [g for g in games if q_text in g.name.lower()]

        # ── Pagination: only the current page is ever rendered ────────────
        per_page = page_size(SCOPE_LIBRARY)
        total_pages = max(1, -(-len(games) // per_page))   # ceil division
        self._current_page = max(1, min(self._current_page, total_pages))
        start = (self._current_page - 1) * per_page
        page_games = games[start:start + per_page]

        def _go_page(n: int):
            self._current_page = n
            # RAM cleanup: flipping pages must not stack the previous page's
            # covers (tens of MB per page) on top of the new page's, which is
            # how the cache crept toward its cap across a browsing session.
            # The new page re-renders in the same chunked, please-wait pass.
            try:
                from ui.widgets.game_items import trim_cover_cache
                trim_cover_cache()
            except Exception:
                pass
            self._rebuild_view()
            self._scroll.verticalScrollBar().setValue(0)

        # Pager at the TOP (both views) — page-size combo rides this row so
        # it sits with the page numbers, never on a band of its own. Shown
        # even when there is only one page, or the size control would vanish.
        self._layout.insertWidget(
            self._layout.count(),
            build_pager(self._current_page, total_pages, _go_page,
                        size_combo=self._page_size_combo))

        # Insert cards/rows in QTimer chunks so the page can paint (and the
        # user can leave) while covers decode. Please-wait already covers
        # from _rebuild_view (delay 0); keep it through the chunk pump.
        gen = self._insert_gen
        self._insert_queue = list(page_games)
        self._insert_total_pages = total_pages
        self._insert_go_page = _go_page
        self._insert_row_layout = None
        self._insert_index = 0
        from core.concurrency import library_insert_chunk_size
        cs = library_insert_chunk_size()
        # Async path always yields between chunks (0 would mean one blocking
        # pump after the first paint — still a freeze on large pages).
        self._insert_chunk_size = cs if cs > 0 else 16
        if self._view_mode == "card":
            self._layout.setSpacing(12)
            self._last_per_row = self._calc_per_row()
        else:
            self._layout.setSpacing(6)

        if not self._insert_queue:
            self._stop_deferred_busy()
            self._finish_page_insert(gen)
            return
        if getattr(self, "_deferred_busy", None) is None and self.isVisible():
            from ui.widgets.busy_overlay import DeferredBusy
            self._deferred_busy = DeferredBusy(
                self, t("common.please_wait"), delay_ms=0)
        QTimer.singleShot(0, lambda g=gen: self._async_insert_step(g))


    def _stop_deferred_busy(self):
        busy = getattr(self, "_deferred_busy", None)
        if busy is not None:
            busy.close()
            self._deferred_busy = None

    def _async_insert_step(self, gen: int):
        """One chunk of cards, then yield back to the event loop."""
        if gen != getattr(self, "_insert_gen", 0):
            return
        self._pump_insert_chunk(gen)
        if gen != getattr(self, "_insert_gen", 0):
            return
        if self._insert_queue:
            QTimer.singleShot(0, lambda g=gen: self._async_insert_step(g))
            return
        self._stop_deferred_busy()
        self._finish_page_insert(gen)

    def _pump_insert_chunk(self, gen: int):
        """Build one chunk of library widgets (caller drains the queue)."""
        if gen != getattr(self, "_insert_gen", 0):
            return
        if not _safe(self._layout):
            return
        queue = getattr(self, "_insert_queue", None) or []
        if not queue:
            return

        # 0 = capable machine: build the whole page in one turn.
        chunk_n = getattr(self, "_insert_chunk_size", 0) or len(queue)
        chunk = queue[:chunk_n]
        del queue[:chunk_n]
        self._insert_queue = queue

        if self._view_mode == "card":
            per_row = getattr(self, "_last_per_row", None) or self._calc_per_row()
            row_layout = self._insert_row_layout
            for entry in chunk:
                if self._insert_index % per_row == 0:
                    if row_layout is not None:
                        row_layout.addStretch()
                    row_widget = QWidget()
                    row_widget.setObjectName("transparent_bg")
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(12)
                    self._layout.insertWidget(self._layout.count(), row_widget)
                    self._insert_row_layout = row_layout
                card = self._make_widget(entry)
                cw, _ = library_card_size(self)
                card.setFixedWidth(cw)
                row_layout.addWidget(card)
                self._cards[entry.id] = card
                self._insert_index += 1
        else:
            for entry in chunk:
                w = self._make_widget(entry)
                self._layout.insertWidget(self._layout.count(), w)
                self._cards[entry.id] = w
                self._insert_index += 1

    def _finish_page_insert(self, gen: int):
        if gen != getattr(self, "_insert_gen", 0) or not _safe(self._layout):
            return
        if self._view_mode == "card" and self._insert_row_layout is not None:
            self._insert_row_layout.addStretch()
            self._insert_row_layout = None
        total_pages = getattr(self, "_insert_total_pages", 1)
        go_page = getattr(self, "_insert_go_page", None)
        if total_pages > 1 and callable(go_page):
            self._layout.insertWidget(
                self._layout.count(),
                build_pager(self._current_page, total_pages, go_page))
        self._layout.addStretch()
        self._update_empty_state()

    def _make_widget(self, entry: GameEntry) -> QWidget:
        if self._view_mode == "card":
            w = GameCard(entry)
        else:
            w = GameRow(entry)
        for sig in ("backup_requested","restore_requested","remove_requested",
                    "edit_requested","sync_requested","launch_requested",
                    "review_provisional_requested", "cheats_requested"):
            getattr(w, sig).connect(getattr(self, sig))
        w.detail_requested.connect(self._on_detail_requested)
        return w

    def _on_detail_requested(self, game_id: str):
        """Show inline detail panel (or edit dialog for now)."""
        self.edit_requested.emit(game_id)

    def _connect_library(self):
        lib = get_library()
        lib.game_added.connect(self._on_game_added)
        lib.game_updated.connect(self._on_game_updated)
        lib.game_removed.connect(self._on_game_removed)
        lib.bulk_finished.connect(self._on_bulk_finished)
        # Update playing badges when monitor fires
        try:
            from core.monitor import get_monitor
            get_monitor().game_launched.connect(self._on_monitor_launched)
            get_monitor().game_exited.connect(self._on_monitor_exited)
        except Exception:
            pass

    def _on_bulk_finished(self):
        """One rebuild after mass add/update (scan / multi-add / batch search)."""
        self._rebuild_view()

    def _on_monitor_launched(self, entry, _):
        if entry:
            self._set_playing_badge(entry.id, True)

    def _on_monitor_exited(self, entry):
        if entry:
            self._set_playing_badge(entry.id, False)

    def disconnect_signals(self):
        try:
            get_library().game_added.disconnect(self._on_game_added)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_updated.disconnect(self._on_game_updated)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_removed.disconnect(self._on_game_removed)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().bulk_finished.disconnect(self._on_bulk_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            from core.monitor import get_monitor
            get_monitor().game_launched.disconnect(self._on_monitor_launched)
            get_monitor().game_exited.disconnect(self._on_monitor_exited)
        except (RuntimeError, TypeError, Exception):
            pass

    def _set_playing_badge(self, game_id: str, is_playing: bool):
        card = self._cards.get(game_id)
        if _safe(card) and hasattr(card, "set_playing"):
            card.set_playing(is_playing)

    def _refresh_tag_sidebar(self):
        """Update folder-tree tag list without rebuilding cards (e.g. after edit)."""
        all_tags: list[str] = []
        for g in get_library().all_games():
            all_tags.extend(_clean_tag_display(x) for x in g.tags)
        self._folder_tree.update_tags(all_tags)

    def _on_game_added(self, entry: GameEntry):
        if entry.id not in self._cards:
            self._rebuild_view()

    def _on_game_updated(self, entry: GameEntry):
        card = self._cards.get(entry.id)
        if card is not None and hasattr(card, "_entry"):
            tags_changed = set(entry.tags) != set(card._entry.tags)
            # Detect if sort-relevant fields changed so the grid order stays current
            sort_fields_changed = (
                entry.last_played != card._entry.last_played
                or entry.playtime_seconds != card._entry.playtime_seconds
                or entry.last_backed_up != card._entry.last_backed_up
                or entry.sync_status != card._entry.sync_status
                or entry.name != card._entry.name
                or entry.average_rating() != card._entry.average_rating()
            )
        else:
            tags_changed = True
            sort_fields_changed = True
        if tags_changed:
            self._refresh_tag_sidebar()
        # Rebuild to reorder cards when sort-relevant data changed, otherwise
        # just refresh the individual card widget (cheaper).
        if sort_fields_changed:
            self._rebuild_view()
        elif card and hasattr(card, "refresh"):
            card.refresh(entry)

    def _on_game_removed(self, gid: str):
        if gid in self._cards:
            self._rebuild_view()

    def refresh_game_status(self, game_id: str):
        """Re-render one game's card so its status badge reflects current
        backup state — e.g. flip 'no saves' to 'provisional' the moment a
        provisional backup appears, without waiting for a full rebuild or a
        page change. No-op if that card isn't currently built."""
        card = self._cards.get(game_id)
        if card is None or not hasattr(card, "refresh"):
            return
        entry = get_library().get_by_id(game_id)
        if entry is not None:
            card.refresh(entry)

    def _on_search_text_changed(self, _text: str = ""):
        """Arm (or re-arm) the search debounce; empty text applies immediately."""
        if not (self._search.text() or "").strip():
            self._search_timer.stop()
            self._apply_search_filter()
            return
        self._search_timer.start()

    def _apply_search_filter(self):
        """Filter the card grid by rebuilding it with only matching games.

        We always do a full rebuild (never show/hide individual cards) so the
        grid reflows properly and leaves no empty slots regardless of whether
        query is being typed or cleared. Changing the query re-paginates the
        result set from page 1 so matches on any page are reachable.
        """
        self._current_page = 1
        self._rebuild_view()

    def _filter_cards(self, query: str):
        """Compat entry used by sort/mode callers — same as a debounced apply."""
        self._on_search_text_changed(query)

    def _on_tags_changed(self):
        """A chip filter changed — re-paginate from page 1 and rebuild."""
        self._current_page = 1
        self._rebuild_view()

    def _update_empty_state(self):
        """Show/hide the empty-state label and scroll area appropriately."""
        q = getattr(self, '_search', None)
        sm = getattr(self, '_search_mode', None)
        query = q.text().strip() if q else ""
        mode = sm.currentData() if sm else "title"
        # Any chip filter (tags or engine) narrowing the list right now
        folder_tree = getattr(self, '_folder_tree', None)
        tags_active = bool(folder_tree and folder_tree.has_active_filters())

        is_empty = (len(self._cards) == 0)
        if is_empty:
            if tags_active:
                msg = t('library.no_games_match_tags')
            elif query:
                if mode == 'developer':
                    msg = t('library.no_games_match_search_dev')
                else:
                    msg = t('library.no_games_match_search')
            else:
                msg = t('library.empty')
            self._empty_lbl.setText(msg)
            self._empty_lbl.setVisible(True)
            self._scroll.setVisible(False)
        else:
            self._empty_lbl.setVisible(False)
            self._scroll.setVisible(True)

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _populate_sort_combo(self):
        current = self._sort_combo.currentData()   # keep selection across a locale repopulate
        self._sort_combo.blockSignals(True)
        self._sort_combo.clear()
        for key, label_key in [
            ("date_added",   "library.sort_date_added"),
            ("last_played",  "library.sort_last_played"),
            ("name_asc",     "library.sort_name"),
            ("playtime",     "library.sort_playtime"),
            ("rating",       "library.sort_rating"),
            ("status",       "library.sort_status"),
            ("last_backup",  "library.sort_last_backup"),
            # "tags" sort removed — tag filtering is handled by the tag panel
            # "name_desc" (Name Z-A) removed — one name-sort direction is enough
        ]:
            self._sort_combo.addItem(t(label_key), key)
        if current is not None:
            for i in range(self._sort_combo.count()):
                if self._sort_combo.itemData(i) == current:
                    self._sort_combo.setCurrentIndex(i)
                    break
        self._sort_combo.blockSignals(False)

    def _on_sort_criterion_changed(self):
        """Sort criterion changed — reset direction to THIS criterion's own
        natural default (e.g. Name → A-Z, Added → newest first) instead of
        leaving whatever direction the previous criterion was on, then
        apply as usual."""
        criterion = self._sort_combo.currentData() or "date_added"
        self._sort_dir = _SORT_NATURAL_DIRECTION.get(criterion, "desc")
        self._update_sort_dir_btn()
        self._on_sort_changed()

    def _on_sort_changed(self):
        get_config().set("library_sort", self._sort_combo.currentData() or "date_added")
        get_config().set("library_sort_direction", self._sort_dir)
        self._current_page = 1
        self._rebuild_view()

    def _on_sort_dir_toggle(self):
        """Flip the sort direction (the ↓/↑ arrow toggle)."""
        self._sort_dir = "asc" if self._sort_dir == "desc" else "desc"
        self._update_sort_dir_btn()
        self._on_sort_changed()

    def _update_sort_dir_btn(self):
        """Refresh the arrow glyph + concrete tooltip for the current
        criterion and direction (↓ = descending, ↑ = ascending). For
        'date_added' descending this reads 'Newest first' — last added at
        the top, exactly as expected."""
        criterion = self._sort_combo.currentData() or "date_added"
        desc_key, asc_key = _SORT_DIR_LABELS.get(
            criterion, ("sort_descending", "sort_ascending"))
        is_desc = self._sort_dir != "asc"
        self._sort_dir_btn.setText("↓" if is_desc else "↑")
        meaning = t(f"library.{desc_key if is_desc else asc_key}")
        self._sort_dir_btn.setToolTip(t("library.sort_dir_tooltip", dir=meaning))

    def _sort_games(self, games: list[GameEntry]) -> list[GameEntry]:
        criterion = self._sort_combo.currentData() or "date_added"
        if criterion == "name_asc":
            result = sorted(games, key=lambda g: g.name.lower())
        elif criterion == "playtime":
            result = sorted(games, key=lambda g: g.playtime_seconds, reverse=True)
        elif criterion == "status":
            order = {"conflict": 0, "pending": 1, "local_only": 2, "synced": 3, "cloud_only": 4, "no_saves": 5, "provisional": 6}
            result = sorted(games, key=lambda g: order.get(_display_sync_status(g), 9))
        elif criterion == "last_backup":
            result = sorted(games, key=lambda g: g.last_backed_up or "", reverse=True)
        elif criterion == "last_played":
            result = sorted(games, key=lambda g: g.last_played or "", reverse=True)
        elif criterion == "rating":
            # Highest average first; unrated (0) sink to the bottom in the
            # natural direction, and the ↓/↑ toggle reverses that.
            result = sorted(games, key=lambda g: g.average_rating(), reverse=True)
        else:  # date_added (default) — newest added first. Legacy entries
               # have no timestamp (date_added is None); without a fallback
               # every untimestamped game collapses to the key "" and Python's
               # stable sort preserves library insertion order, showing them
               # OLDEST-first. The library keeps games in add order, so the
               # insertion position is the add-order signal: later position =
               # more recently added.
            _pos = {g.id: i for i, g in enumerate(games)}
            result = sorted(
                games,
                key=lambda g: (g.date_added or "", _pos[g.id]),
                reverse=True,
            )

        # Uniform direction toggle: reverse the criterion's own natural
        # order whenever the user picked the opposite direction. Works the
        # same way for every criterion — no special-casing needed beyond
        # knowing what "natural" already means for it (see
        # _SORT_NATURAL_DIRECTION above).
        chosen = self._sort_dir or _SORT_NATURAL_DIRECTION.get(criterion, "desc")
        natural = _SORT_NATURAL_DIRECTION.get(criterion, "desc")
        if chosen != natural:
            result = list(reversed(result))
        return result

    # ── Folders ──────────────────────────────────────────────────────────────

    def _on_folder_selected(self, path: str):
        """Called when user clicks a folder in the sidebar."""
        self._current_page = 1
        self._rebuild_view()

    # ── Locale ───────────────────────────────────────────────────────────────

    def update_locale(self):
        self._header.setText(t("library.title"))
        self._add_btn.setText(f"+ {t('library.add_game')}")
        self._search.setPlaceholderText(t("library.search_placeholder"))
        # Item order fixed at build time: 0 = title, 1 = developer
        self._search_mode.setItemText(0, t("library.search_by_title"))
        self._search_mode.setItemText(1, t("library.search_by_developer"))
        self._empty_lbl.setText(t("library.empty"))
        self._card_btn.setToolTip(t("tooltips.card_view"))
        self._list_btn.setToolTip(t("tooltips.list_view"))
        self._populate_sort_combo()
        self._update_sort_dir_btn()
        self._page_size_combo.update_locale()
        self._folder_tree.update_locale()
        for card in self._cards.values():
            if hasattr(card, "update_locale"):
                card.update_locale()

    # ── Theme ────────────────────────────────────────────────────────────────

    def refresh_styles(self):
        """Re-apply every palette-dependent inline style IN PLACE on a
        light/dark theme switch — page chrome, the folder tree (+ its rows and
        tag panel), the pager, AND every live game card/row — WITHOUT rebuilding
        the per-game cards (that rebuild is what froze the app on large
        libraries). No card is recreated here."""
        # 1. Registered page-chrome one-shots (search-mode combo, sort-direction
        #    button, empty-state label).
        super().refresh_styles()
        # 2. State-dependent view-toggle button highlight.
        self._apply_view_btn_styles()
        # 3. Pager buttons need nothing: #pager_btn / #pager_btn_active come
        #    from the theme, which the stylesheet swap has already re-resolved.
        # 4. Folder tree: its own chrome + folder rows + tag-filter panel/chips.
        if getattr(self, "_folder_tree", None) is not None:
            try:
                self._folder_tree.refresh_styles()
            except RuntimeError:
                pass
        # 5. Cascade into every live card/row so status/sync colours, folder
        #    borders and hover styles follow the theme — recomputed in place.
        for card in list(self._cards.values()):
            if hasattr(card, "refresh_styles"):
                try:
                    card.refresh_styles()
                except RuntimeError:
                    pass

    def _remediate_page_scrolls(self):
        """Re-mediate scroll policies after DPI scale changes to maintain proportions.
        
        Ensures game cards and folder tree maintain proper sizing after DPI changes.
        """
        try:
            # Update folder tree dimensions
            if getattr(self, "_folder_tree", None) is not None:
                try:
                    from ui.helpers import scaled
                    _sw = scaled(168, self, min_px=150)
                    self._folder_tree.setFixedWidth(_sw)
                    self._folder_tree.updateGeometry()
                except Exception:
                    pass
            
            # Update card geometries
            for card in list(self._cards.values()):
                if hasattr(card, "updateGeometry"):
                    try:
                        card.updateGeometry()
                    except RuntimeError:
                        pass
            
            # Trigger layout recalculation
            if hasattr(self, 'layout') and self.layout():
                self.layout().activate()
                self.layout().update()
        except Exception:
            pass