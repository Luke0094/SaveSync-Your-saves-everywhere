"""
SaveSync - Library Page
Game library with card/list view toggle, game images, launch button, and detail panel.
"""
import logging

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QComboBox, QToolButton,
)

from core.library import GameEntry, get_library
from core.config_manager import get_config
from i18n import t
from ui.helpers import safe_widget as _safe
from ui.styles.theme import palette, ThemedMixin

from ui.widgets.library_folders import FolderTree, _clean_tag_display
from ui.widgets.game_items import GameCard, GameRow, _display_sync_status

_SORT_NATURAL_DIRECTION = {
    "date_added":  "desc",
    "last_played": "desc",
    "name_asc":    "asc",
    "playtime":    "desc",
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
    "status":      ("sort_descending", "sort_ascending"),
}

# Map status to palette key for theme-aware colors
PAGE_SIZE = 20   # max cards/rows per library page (and titles per backups page)


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


def build_pager(current: int, total: int, on_page) -> QWidget:
    """Centered pager row: ‹  [1] … [N]  ›.

    - prev hidden on the first page, next hidden on the last;
    - with a single page the caller must not add the pager at all.
    *on_page* is called with the target page number.

    The buttons take their look from the theme (see _style_pager_btn), so
    callers no longer need to collect them for re-styling on a theme switch.
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
        b.setFixedHeight(26)
        b.setMinimumWidth(30)
        if tooltip:
            b.setToolTip(tooltip)
        _style_pager_btn(b, active)
        b.clicked.connect(lambda _=False, p=page: on_page(p))
        return b

    if current > 1:
        row.addWidget(_btn("‹", current - 1, tooltip=t("common.prev_page")))
    for n in page_numbers(current, total):
        row.addWidget(_btn(str(n), n, active=(n == current)))
    if current < total:
        row.addWidget(_btn("›", current + 1, tooltip=t("common.next_page")))

    row.addStretch()
    return wrap

# Singleton drag state shared between cards and folder tree
class LibraryPage(QWidget, ThemedMixin):
    add_game_requested = Signal(str, str)  # name, exe_path
    scan_folder_requested = Signal()       # 🔍 — scan a folder for games
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
        self._build()
        self._connect_library()
        self._load_library()

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
                # Folder shortcut: open the add dialog with just the name —
                # a directory must never be prefilled as the executable.
                exe_path = ""
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
                self._rebuild_view()

    def _calc_per_row(self) -> int:
        """Calculate how many 186px cards fit in the current scroll area width."""
        # Available width: total widget minus folder sidebar, margins, scrollbar
        available = self.width() - 170 - 16 - 10
        card_w = 186 + 12  # card width + spacing
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
        self._card_btn.setFixedSize(30, 30)
        self._card_btn.setToolTip(t("tooltips.card_view"))
        self._card_btn.clicked.connect(lambda: self._set_view("card"))

        self._list_btn = QPushButton(t("buttons.list_view_icon"))
        self._list_btn.setObjectName("icon_btn")
        self._list_btn.setFixedSize(30, 30)
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
        self._scan_btn.setFixedSize(34, 34)
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
        self._search_mode.setFixedWidth(100)
        self._search_mode.addItem(t("library.search_by_title"),     "title")
        self._search_mode.addItem(t("library.search_by_developer"), "developer")
        self._sty(self._search_mode, lambda: (
            f"QComboBox{{background:{palette('bg_input')};border:1px solid {palette('border')};"
            f"border-radius:4px;padding:0 6px;font-size:11px;color:{palette('text')};}}"
        ))
        filter_row.addWidget(self._search_mode)

        self._search = QLineEdit()
        self._search.setPlaceholderText(t("library.search_placeholder"))
        self._search.textChanged.connect(self._filter_cards)
        self._search_mode.currentIndexChanged.connect(lambda _: self._filter_cards(self._search.text()))
        filter_row.addWidget(self._search, 1)

        # Sort combo (criterion) + direction dropdown (asc/desc, resets to
        # a sensible default for the criterion whenever it changes)
        self._sort_combo = QComboBox()
        self._sort_combo.setFixedWidth(130)
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
        self._sort_dir_btn.setFixedWidth(34)
        self._sort_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sty(self._sort_dir_btn, lambda: (
            f"QToolButton{{background:{palette('bg_input')};border:1px solid {palette('border')};"
            f"border-radius:4px;font-size:14px;color:{palette('text')};}}"
        ))
        self._sort_dir_btn.clicked.connect(self._on_sort_dir_toggle)
        self._update_sort_dir_btn()
        filter_row.addWidget(self._sort_dir_btn)

        # Criterion change resets direction to that criterion's natural
        # default (e.g. Name → A-Z, Added → newest first) instead of
        # keeping whatever direction the PREVIOUS criterion was left on.
        self._sort_combo.currentIndexChanged.connect(self._on_sort_criterion_changed)

        root.addLayout(filter_row)

        # Body: folder sidebar + game grid
        body = QHBoxLayout()
        body.setSpacing(0)

        # Folder tree sidebar
        self._folder_tree = FolderTree()
        self._folder_tree.folder_selected.connect(self._on_folder_selected)
        self._folder_tree.tags_changed.connect(self._on_tags_changed)
        body.addWidget(self._folder_tree)

        # Scroll area for game cards/rows
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("transparent_bg")
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(8)
        self._layout.setContentsMargins(8, 0, 0, 0)
        self._layout.addStretch()

        self._scroll.setWidget(self._container)

        # Empty / no-results label — shown instead of the scroll area, at top
        self._empty_lbl = QLabel(t("library.empty"))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._sty(self._empty_lbl, lambda: (
            f"color:{palette('text_hint')};font-size:13px;padding:20px 16px;"
        ))
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setVisible(False)
        body.addWidget(self._empty_lbl)
        body.addWidget(self._scroll, 1)

        root.addLayout(body, 1)

        self._set_view("card")

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

        Wrapped in a "please wait" sheet: this runs on the GUI thread and
        every card decodes its cover, so with large artwork the page can take
        a noticeable moment and the window would otherwise just sit frozen.
        """
        from ui.widgets.busy_overlay import busy_over
        with busy_over(self):
            self._rebuild_view_inner()

    def _rebuild_view_inner(self):
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
        total_pages = max(1, -(-len(games) // PAGE_SIZE))   # ceil division
        self._current_page = max(1, min(self._current_page, total_pages))
        start = (self._current_page - 1) * PAGE_SIZE
        page_games = games[start:start + PAGE_SIZE]

        def _go_page(n: int):
            self._current_page = n
            self._rebuild_view()
            self._scroll.verticalScrollBar().setValue(0)

        # Pager at the TOP (both views) — inserted first, while the layout is
        # still empty, so it lands above the cards/rows. The bottom pager is
        # appended after the entries below.
        if total_pages > 1:
            self._layout.insertWidget(
                self._layout.count(),
                build_pager(self._current_page, total_pages, _go_page))

        if self._view_mode == "card":
            # Wrap cards in a flow-ish grid using nested HBoxLayouts.
            # A stretch is appended to EVERY row so that fixed-width cards are
            # always left-aligned regardless of how wide the window is.
            self._layout.setSpacing(12)
            row_layout = None
            per_row = self._calc_per_row()
            self._last_per_row = per_row
            for i, entry in enumerate(page_games):
                if i % per_row == 0:
                    if row_layout is not None:
                        row_layout.addStretch()  # pin previous row's cards to the left
                    row_widget = QWidget()
                    row_widget.setObjectName("transparent_bg")
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(12)
                    self._layout.insertWidget(self._layout.count(), row_widget)
                card = self._make_widget(entry)
                card.setFixedWidth(186)
                row_layout.addWidget(card)
                self._cards[entry.id] = card
            if row_layout is not None:
                row_layout.addStretch()  # pin last row's cards to the left
        else:
            self._layout.setSpacing(6)
            for entry in page_games:
                w = self._make_widget(entry)
                self._layout.insertWidget(self._layout.count(), w)
                self._cards[entry.id] = w

        # Pager at the END of the list/grid (both views)
        if total_pages > 1:
            self._layout.insertWidget(
                self._layout.count(),
                build_pager(self._current_page, total_pages, _go_page))

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
        # Update playing badges when monitor fires
        try:
            from core.monitor import get_monitor
            get_monitor().game_launched.connect(self._on_monitor_launched)
            get_monitor().game_exited.connect(self._on_monitor_exited)
        except Exception:
            pass

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
            from core.monitor import get_monitor
            get_monitor().game_launched.disconnect(self._on_monitor_launched)
            get_monitor().game_exited.disconnect(self._on_monitor_exited)
        except (RuntimeError, TypeError, Exception):
            pass

    def _set_playing_badge(self, game_id: str, is_playing: bool):
        card = self._cards.get(game_id)
        if _safe(card) and hasattr(card, "set_playing"):
            card.set_playing(is_playing)

    def _load_library(self):
        self._rebuild_view()

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

    def _filter_cards(self, query: str):
        """Filter the card grid by rebuilding it with only matching games.

        We always do a full rebuild (never show/hide individual cards) so the
        grid reflows properly and leaves no empty slots regardless of whether
        query is being typed or cleared. Changing the query re-paginates the
        result set from page 1 so matches on any page are reachable.
        """
        self._current_page = 1
        self._rebuild_view()

    def _on_tags_changed(self):
        """Tag filter changed — re-paginate from page 1 and rebuild."""
        self._current_page = 1
        self._rebuild_view()

    def _update_empty_state(self):
        """Show/hide the empty-state label and scroll area appropriately."""
        q = getattr(self, '_search', None)
        sm = getattr(self, '_search_mode', None)
        query = q.text().strip() if q else ""
        mode = sm.currentData() if sm else "title"
        # Check active tag filters
        folder_tree = getattr(self, '_folder_tree', None)
        tags_active = bool(folder_tree and (folder_tree.get_selected_tags()
                                            or folder_tree.get_excluded_tags()))

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