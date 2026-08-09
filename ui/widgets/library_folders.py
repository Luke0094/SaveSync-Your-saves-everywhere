"""
SaveSync - Library sidebar folder tree + tag filter panel.

Extracted verbatim from ui/pages/library_page.py: FOLDER_COLOR_KEYS, the
folder-tree dict helpers, the color-swatch styling helpers, TagFilterPanel,
FolderRow and FolderTree. Pure move — no behavior change.
"""
import logging

from PySide6.QtCore import Qt, Signal, QPoint, QEvent
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QMenu, QWidget, QGraphicsOpacityEffect, QScrollArea, QSizePolicy,
    QSplitter, QStackedWidget, QMessageBox, QApplication,
)

from i18n import t
from core.config_manager import get_config
from core.library import get_library
from ui.modal_helpers import input_text_window_modal, question_window_modal
from ui.styles.theme import palette, ThemedMixin
from ui.widgets.library_drag import _active_drag, DragProxy
from ui.widgets.search_inputs import GhostClearableLineEdit, _SuggestPopup

logger = logging.getLogger(__name__)

FOLDER_COLOR_KEYS = [
    "folder_red", "folder_orange", "folder_yellow", "folder_green",
    "folder_blue", "folder_purple", "folder_pink", "folder_gray",
]


def _clean_tag_display(tag: str) -> str:
    """Repair a tag's HTML-entity encoding for display/filtering.

    Mirrors AddGameDialog._clean_tag (html.unescape) for tags that still
    carry a well-formed entity such as "&#039;", but ALSO repairs tags that
    were already saved with the leading '&' stripped somewhere along the
    way — e.g. "Woman#039;s Viewpoint" instead of "Woman&#039;s Viewpoint".
    Plain html.unescape() can't recover those: with no '&' left, "#039;"
    isn't valid entity syntax any more, so nothing gets decoded. Used
    consistently everywhere the library page reads a game's tags (both for
    the sidebar's displayed tag list and for the actual include/exclude
    filter matching), so a repaired display name and the raw stored value
    are always treated as the same tag.
    """
    import html
    import re as _re_tag
    if not tag:
        return tag
    repaired = _re_tag.sub(r'(?<!&)#(x?[0-9A-Fa-f]{2,7};)', r'&#\1', tag)
    return html.unescape(repaired).strip()


# ── Folder tree helpers ──────────────────────────────────────────────────────

def _flatten_folders(folders: list[dict], prefix: str = "") -> list[tuple[str, str, int]]:
    """Flatten nested folder tree to [(path, color_key, depth), ...].
    Example: [{name:"RPG", color:"folder_blue", children:[{name:"JRPG",...}]}]
    → [("RPG", "folder_blue", 0), ("RPG/JRPG", "folder_purple", 1)]"""
    result = []
    for f in folders:
        path = f"{prefix}{f['name']}" if not prefix else f"{prefix}/{f['name']}"
        result.append((path, f.get("color") or "folder_gray", len(path.split("/")) - 1))
        result.extend(_flatten_folders(f.get("children", []), path))
    return result


def _get_folder_color_by_path(folders: list[dict], path: str) -> str:
    """Navigate the nested tree and return the palette color key for a folder path."""
    if not path:
        return ""
    parts = path.split("/")
    current = folders
    color = "folder_gray"
    for part in parts:
        found = False
        for f in current:
            if f["name"] == part:
                color = f.get("color") or "folder_gray"
                current = f.get("children", [])
                found = True
                break
        if not found:
            return ""
    return color


def _add_folder_to_tree(folders: list[dict], parent_path: str, name: str, color: str) -> bool:
    """Add a folder under parent_path. Empty parent_path means root level."""
    new_entry = {"name": name, "color": color, "children": []}
    if not parent_path:
        if any(f["name"] == name for f in folders):
            return False
        folders.append(new_entry)
        return True
    parts = parent_path.split("/")
    current = folders
    for part in parts:
        for f in current:
            if f["name"] == part:
                current = f.setdefault("children", [])
                break
        else:
            return False
    if any(f["name"] == name for f in current):
        return False
    current.append(new_entry)
    return True


def _remove_folder_from_tree(folders: list[dict], path: str) -> bool:
    """Remove a folder by its full path. Returns True if found and removed."""
    parts = path.split("/")
    if len(parts) == 1:
        for i, f in enumerate(folders):
            if f["name"] == parts[0]:
                folders.pop(i)
                return True
        return False
    parent_parts, target = parts[:-1], parts[-1]
    current = folders
    for part in parent_parts:
        for f in current:
            if f["name"] == part:
                current = f.get("children", [])
                break
        else:
            return False
    for i, f in enumerate(current):
        if f["name"] == target:
            current.pop(i)
            return True
    return False


def _ensure_children_field(folders: list[dict]):
    """Migrate flat folder list: ensure every folder has a 'children' key."""
    for f in folders:
        if "children" not in f:
            f["children"] = []
        _ensure_children_field(f["children"])

def _hex_to_rgb(hex_color: str) -> str:
    """Convert '#rrggbb' to 'r,g,b' for use in rgba()."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        return f"{int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)}"
    return "0,0,0"


def _swatch_style(hex_c: str, selected: bool) -> str:
    """Stylesheet for a colour-swatch button in the new-folder dialog.
    *hex_c* is already-resolved (the swatch colour itself); the selection ring
    uses the current theme's text colour."""
    if selected:
        return f"QPushButton{{background:{hex_c};border:2px solid {palette('text')};border-radius:14px;}}"
    return (
        f"QPushButton{{background:{hex_c};border:2px solid transparent;border-radius:14px;}}"
        f"QPushButton:hover{{border-color:{palette('text')};}}"
    )

class TagFilterPanel(QFrame, ThemedMixin):
    """Panel with 3-state chip buttons for filtering the library.
    Click 1: include (green) — game must have this value.
    Click 2: exclude (red)  — game must NOT have this value.
    Click 3: deselect       — value not used for filtering.

    Built for tags and reused verbatim for engines: the interaction is the
    same, only the words around it differ, so the labels come in as i18n
    keys rather than being hard-coded here.
    """
    tags_changed = Signal()
    cleared = Signal()      # "clear all" pressed, before tags_changed

    def __init__(self, parent=None, *,
                 header_key: str = "library.filter_tags",
                 search_key: str = "library.search_tags",
                 clear_key: str = "library.clear_tags",
                 active_key: str = "library.active_tag_filters",
                 merge_variants: bool = True):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._states: dict[str, int] = {}   # tag → 0=off 1=include 2=exclude
        self._all_tags: list[str] = []
        self._header_key = header_key
        self._search_key = search_key
        self._clear_key = clear_key
        self._active_key = active_key
        # Tags arrive in every spelling a web source felt like; engine names
        # come from one table and must not be collapsed into each other.
        self._merge_variants = merge_variants
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)

        header = QLabel(t(self._header_key))
        self._sty(header, lambda: (
            f"color:{palette('text_muted')};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;background:transparent;"
        ))
        layout.addWidget(header)
        self._header = header

        self._search = GhostClearableLineEdit()
        self._search.setPlaceholderText(t(self._search_key))
        self._search.setFixedHeight(24)
        self._sty(self._search, lambda: (
            f"QLineEdit{{background:{palette('bg_input')};border:1px solid {palette('border')};"
            f"border-radius:4px;padding:0 6px;font-size:11px;color:{palette('text')};}}"
        ))
        self._search.textChanged.connect(self._filter)
        # Backups-search-style suggestions: typing opens a popup list of
        # matching tags (first row highlighted, ghost mirrors it); ↓/↑
        # navigate, Enter / row click / ghost click CONFIRM — which only
        # LOADS the tag into the search (the list below shows it). It never
        # toggles include/exclude: that stays the user's click on the tag
        # row. No comma segmentation: this is a single-tag filter.
        self._suggest_matches: list[str] = []
        self._suggest = _SuggestPopup(self)
        self._suggest.item_activated.connect(self._on_suggest_clicked)
        self._search.returnPressed.connect(self._confirm_search_suggest)
        self._search.ghost_accepted.connect(self._confirm_search_suggest)
        self._search.installEventFilter(self)
        layout.addWidget(self._search)

        # ── Tag list (scrollable) ─────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # No fixed cap — the panel now lives in a draggable splitter pane
        # (see FolderTree), so this should grow/shrink with whatever space
        # the user gives it. A floor keeps it from disappearing entirely.
        self._scroll.setMinimumHeight(40)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._scroll_content = QWidget()
        # Named, not styled: an own stylesheet here would outrank the theme
        # for every chip inside and wipe out their include/exclude fill.
        self._scroll_content.setObjectName("tag_scroll_body")
        self._tag_layout = QVBoxLayout(self._scroll_content)
        self._tag_layout.setContentsMargins(0, 0, 0, 0)
        self._tag_layout.setSpacing(1)
        self._scroll.setWidget(self._scroll_content)
        # Stretch 3 vs the recap's 1: the tag list keeps space priority —
        # the selections recap must never crush its readability.
        layout.addWidget(self._scroll, 3)

        # ── Active selections recap (BELOW the tag list, independent) ─────
        # The list above is filter-dependent, so a ✓/✕ tag that doesn't
        # match the current search text becomes invisible — this strip
        # always shows the live selection for a clear read of the active
        # filter. It is INDEPENDENT of the list: it grows with the entries
        # up to five rows, then scrolls inside its own fixed-height area,
        # so a large selection can never push the tag list out of view.
        # Hidden entirely at zero selections. Clicking an entry removes it.
        self._selected_lbl = QLabel(t(self._active_key))
        self._sty(self._selected_lbl, lambda: (
            f"color:{palette('text_muted')};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;background:transparent;margin-top:2px;"
        ))
        self._selected_lbl.setVisible(False)
        layout.addWidget(self._selected_lbl)
        self._selected_scroll = QScrollArea()
        self._selected_scroll.setWidgetResizable(True)
        self._selected_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._selected_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._selected_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                            QSizePolicy.Policy.Expanding)
        self._selected_wrap = QWidget()
        self._selected_wrap.setObjectName("tag_scroll_body")   # same reason
        self._selected_layout = QVBoxLayout(self._selected_wrap)
        self._selected_layout.setContentsMargins(0, 0, 0, 0)
        self._selected_layout.setSpacing(1)
        self._selected_scroll.setWidget(self._selected_wrap)
        self._selected_scroll.setVisible(False)
        layout.addWidget(self._selected_scroll, 1)
        self._selected_buttons: dict[str, QPushButton] = {}

        self._clear_btn = QPushButton(t(self._clear_key))
        self._sty(self._clear_btn, lambda: (
            f"QPushButton{{color:{palette('text_muted')};font-size:10px;background:transparent;"
            f"border:none;padding:2px;}}"
            f"QPushButton:hover{{color:{palette('accent')};}}"
        ))
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.clicked.connect(self._clear_all)
        layout.addWidget(self._clear_btn)

        self._tag_buttons: dict[str, QPushButton] = {}

    @staticmethod
    def _style_tag_btn(btn: QPushButton, state: int):
        """Tag a chip with its filter state and let the theme QSS paint it.

        The look lives in DARK_THEME/LIGHT_THEME under ``#tag_chip``, keyed
        on the ``tagState`` property (0 off, 1 include, 2 exclude), instead
        of a per-button stylesheet: a library with a few hundred tags used
        to hand every one of them its own sheet, which is what made a
        light/dark switch pause. No unpolish needed anywhere — every state
        change rebuilds these buttons from scratch.
        """
        btn.setObjectName("tag_chip")
        btn.setProperty("tagState", str(state))

    def set_tags(self, tags: list[str]):
        # MERGE by tag_merge_key (case- AND separator-insensitive):
        # "2DCG"/"2dcg", "Adventure"/"adventure" or "2D Game"/"2d-game"
        # are ONE tag here, never two branching entries. The canonical
        # spelling is the most frequent variant across the library (ties →
        # first seen), so a variant added later joins the established
        # spelling. Ordering is casefold too — one interleaved alphabet.
        from collections import Counter
        from core.library import tag_merge_key
        if not self._merge_variants:
            seen: dict[str, str] = {}
            for x in tags:
                value = (x or "").strip()
                if value:
                    seen.setdefault(value.casefold(), value)
            self._all_tags = sorted(seen.values(),
                                    key=lambda s: (s.casefold(), s))
            live = {v.casefold() for v in self._all_tags}
            self._states = {k: s for k, s in self._states.items()
                            if s and k.casefold() in live}
            self._rebuild_checks(self._search.text())
            return
        variants: dict[str, Counter] = {}
        first_idx: dict[tuple, int] = {}
        for i, x in enumerate(tags):
            ct = _clean_tag_display(x)
            if not ct:
                continue
            cf = tag_merge_key(ct)
            variants.setdefault(cf, Counter())[ct] += 1
            first_idx.setdefault((cf, ct), i)
        canon = {
            cf: max(cnt.items(),
                    key=lambda kv: (kv[1], -first_idx[(cf, kv[0])]))[0]
            for cf, cnt in variants.items()
        }
        self._all_tags = sorted(canon.values(), key=lambda s: (s.casefold(), s))
        # Migrate states onto the canonical keys (drop the stale ones): a
        # ✓ set on "adventure" must survive the merge into "Adventure".
        self._states = {canon[tag_merge_key(t)]: s for t, s in self._states.items()
                        if s and tag_merge_key(t) in canon}
        self._rebuild_checks(self._search.text())

    def get_selected(self) -> set[str]:
        """Returns included tags (state==1). Use get_excluded() for red tags."""
        return {t for t, s in self._states.items() if s == 1}

    def get_excluded(self) -> set[str]:
        """Returns excluded tags (state==2)."""
        return {t for t, s in self._states.items() if s == 2}

    def _rebuild_checks(self, filter_text: str = ""):
        while self._tag_layout.count():
            item = self._tag_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._tag_buttons.clear()
        q = filter_text.lower().strip()
        for tag in self._all_tags:
            if q and q not in tag.lower():
                continue
            state = self._states.get(tag, 0)
            prefix = "✓ " if state == 1 else ("✕ " if state == 2 else "  ")
            btn = QPushButton(prefix + tag)
            self._style_tag_btn(btn, state)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, t=tag: self._cycle_state(t))
            self._tag_layout.addWidget(btn)
            self._tag_buttons[tag] = btn
        self._rebuild_selected_recap()

    def _rebuild_selected_recap(self):
        """Refresh the always-visible strip of ACTIVE selections (✓ green,
        ✕ red) above the filterable list — includes first, then excludes,
        each alphabetical. Clicking an entry removes it from the
        selection."""
        while self._selected_layout.count():
            item = self._selected_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._selected_buttons.clear()
        active = sorted(((tag, s) for tag, s in self._states.items() if s),
                        key=lambda x: (x[1], x[0].casefold()))
        for tag, s in active:
            btn = QPushButton(("✓ " if s == 1 else "✕ ") + tag)
            self._style_tag_btn(btn, s)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(t("library.active_tag_remove"))
            btn.clicked.connect(lambda _=False, t=tag: self._drop_state(t))
            self._selected_layout.addWidget(btn)
            self._selected_buttons[tag] = btn
        # Flexible height: grows with the entries up to five rows, but the
        # LAYOUT may compress it down to a single row when the splitter
        # pane shrinks — a hard fixed height used to push the clear-all
        # button out of the visible pane. The tag list keeps priority
        # (stretch 3 vs 1) so a large selection never crushes it; whatever
        # doesn't fit the granted height scrolls inside the area.
        if active:
            row_h = max(b.sizeHint().height()
                        for b in self._selected_buttons.values())
            rows = min(len(active), 5)
            cap = rows * row_h + (rows - 1) * self._selected_layout.spacing() + 2
            self._selected_scroll.setMinimumHeight(row_h + 2)
            self._selected_scroll.setMaximumHeight(cap)
        self._selected_lbl.setVisible(bool(active))
        self._selected_scroll.setVisible(bool(active))

    def _drop_state(self, tag: str):
        """Recap entry clicked — remove that tag from the selection."""
        if self._states.pop(tag, None) is not None:
            self._rebuild_checks(self._search.text())
            self.tags_changed.emit()

    def _cycle_state(self, tag: str):
        current = self._states.get(tag, 0)
        self._states[tag] = (current + 1) % 3
        self._rebuild_checks(self._search.text())
        self.tags_changed.emit()

    def _filter(self, text: str):
        self._rebuild_checks(text)
        self._update_suggest(text)

    # ── Search suggestions popup (backups-search style) ──────────────────

    def _suggest_candidates(self, q: str) -> list[str]:
        """Tags matching *q* (contains, case-insensitive; prefix matches
        first). An EXACT match is excluded — a fully-typed tag needs no
        suggestion and would only re-open the popup after a confirm."""
        q_cf = q.casefold()
        matches = [c for c in self._all_tags
                   if q_cf in c.casefold() and c.casefold() != q_cf]
        matches.sort(key=lambda c: (not c.casefold().startswith(q_cf), c.casefold()))
        return matches[:50]

    def _update_suggest(self, text: str):
        q = text.strip()
        matches = self._suggest_candidates(q) if q else []
        self._suggest_matches = matches
        if not matches:
            self._hide_suggest()
            return
        if not self._search.hasFocus():
            return          # never OPEN unfocused; FocusOut hides
        # Parent the popup to the window so the (small) panel can't clip it.
        host = self.window() or self
        if self._suggest.parent() is not host:
            self._suggest.setParent(host)
        self._suggest.set_items(matches)   # backups-style: first row highlighted
        pos = self._search.mapTo(host, QPoint(0, self._search.height()))
        self._suggest.move(pos)
        self._suggest.setFixedWidth(max(self._search.width(), 180))
        self._suggest.show()
        self._suggest.raise_()
        self._apply_search_ghost()

    def _hide_suggest(self):
        if self._suggest.isVisible():
            self._suggest.hide()
        self._search.set_ghost("")

    def _apply_search_ghost(self):
        """Mirror the highlighted popup tag as a paint-only hint: typed
        'av' + highlighted 'Avventura' paints 'ventura'; a non-prefix match
        shows as '  —  name'."""
        q = self._search.text().strip()
        row = self._suggest.current_row()
        if not q or not self._suggest.isVisible() \
                or not (0 <= row < len(self._suggest_matches)):
            self._search.set_ghost("")
            return
        name = self._suggest_matches[row]
        if name.casefold().startswith(q.casefold()) and len(name) > len(q):
            self._search.set_ghost(name[len(q):])
        else:
            self._search.set_ghost(f"  —  {name}")

    def _on_suggest_clicked(self, row: int):
        if 0 <= row < len(self._suggest_matches):
            self._confirm_search_suggest(row)

    def _confirm_search_suggest(self, row: int = -1):
        """Confirm the highlighted (or clicked) tag: LOAD it into the
        search — the list below then shows it — and nothing else. The
        include/exclude choice stays the user's click on the tag row."""
        if row < 0:
            row = self._suggest.current_row() if self._suggest.isVisible() else -1
        if not (0 <= row < len(self._suggest_matches)):
            return
        name = self._suggest_matches[row]
        self._search.setText(name)
        # AFTER setText: its textChanged→_update_suggest may re-open the
        # popup with longer tags containing this one — a confirm must end
        # with the popup closed.
        self._hide_suggest()

    def eventFilter(self, obj, event):
        """↓/↑/Esc routing for the search popup: arrows navigate (↓ opens
        it when hidden), the ghost follows the highlight, Esc closes just
        the popup, focus loss hides it."""
        if obj is getattr(self, '_search', None):
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    if not self._suggest.isVisible() and key == Qt.Key.Key_Down:
                        self._update_suggest(self._search.text())
                    if self._suggest.isVisible():
                        self._suggest.move_selection(
                            1 if key == Qt.Key.Key_Down else -1)
                        self._apply_search_ghost()
                        return True
                    return False
                if key == Qt.Key.Key_Escape and self._suggest.isVisible():
                    self._hide_suggest()
                    return True
            elif event.type() == QEvent.Type.FocusOut:
                self._hide_suggest()
        return super().eventFilter(obj, event)

    def clear_selection(self, *, emit: bool = True):
        """Drop every include/exclude in this panel."""
        self._states.clear()
        self._rebuild_checks(self._search.text())
        if emit:
            self.tags_changed.emit()

    def _clear_all(self):
        # Announced BEFORE this panel emits, so an owner clearing a sibling
        # panel too has already done it by the time the list re-filters —
        # one rebuild, with every selection gone, instead of two.
        self.cleared.emit()
        self.clear_selection()

    def update_locale(self):
        self._header.setText(t(self._header_key))
        self._search.setPlaceholderText(t(self._search_key))
        self._clear_btn.setText(t(self._clear_key))
        self._selected_lbl.setText(t(self._active_key))
        self._rebuild_checks(self._search.text())

    def refresh_styles(self):
        # Re-apply the registered one-shot styles (header/search/clear). The
        # tag chips are NOT re-styled here any more: their look comes from
        # the theme QSS via #tag_chip, which the stylesheet swap has already
        # re-resolved by the time this runs.
        super().refresh_styles()
        if hasattr(self._suggest, 'refresh_styles'):
            self._suggest.refresh_styles()


# ── Folder Tree Sidebar ─────────────────────────────────────────────────────

class FolderRow(QFrame, ThemedMixin):
    """Single row in the folder tree sidebar — drop target for manual drag."""
    clicked = Signal(str)        # folder_path
    add_sub = Signal(str)        # parent folder_path

    def __init__(self, path: str, name: str, color_key: str, depth: int, parent=None):
        super().__init__(parent)
        self._path = path
        self._name = name
        self._color_key = color_key
        self._depth = depth
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        # Scoped selector target: the row style must NOT cascade to child
        # QFrames (the 3px color bar) — an unscoped QFrame{...} rule gave
        # the bar the selection border too, squeezing the folder icon.
        self.setObjectName("folder_row")
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        indent = 8 + self._depth * 14
        layout.setContentsMargins(indent, 2, 4, 2)
        layout.setSpacing(5)

        # Folder icon with color
        icon_lbl = QLabel("📁")
        icon_lbl.setFixedWidth(18)
        icon_lbl.setStyleSheet("font-size:12px;background:transparent;")
        layout.addWidget(icon_lbl)

        # Color bar (colour re-read from self._color_key on refresh)
        bar = QFrame()
        bar.setFixedSize(3, 16)
        self._sty(bar, lambda: f"background:{palette(self._color_key)};border-radius:1px;")
        layout.addWidget(bar)

        # Name
        lbl = QLabel(self._name)
        lbl.setStyleSheet(self._label_style())
        layout.addWidget(lbl, 1)
        self._label = lbl

        # "+" button for sub-folder (visible on hover via stylesheet)
        self._add_btn = QPushButton("+")
        self._add_btn.setFixedSize(18, 18)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # On "Tutti i giochi" the + creates a ROOT folder (same convenience
        # as the bottom "new folder" button); on folders, a subfolder.
        self._add_btn.setToolTip(t("library.new_folder") if self._path == "__all__"
                                 else t("library.add_subfolder"))
        self._sty(self._add_btn, lambda: (
            f"QPushButton{{color:{palette('text_muted')};font-size:12px;font-weight:700;"
            f"background:transparent;border:1px solid {palette('border')};border-radius:9px;padding:0;}}"
            f"QPushButton:hover{{color:{palette('accent')};border-color:{palette('accent')};}}"
        ))
        self._add_btn.clicked.connect(lambda: self.add_sub.emit(self._path))
        layout.addWidget(self._add_btn)
        # Hide by default, show on hover
        self._add_btn.setVisible(False)

        self._apply_style()

    def enterEvent(self, event):
        self._add_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._add_btn.setVisible(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._folder_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._path == "__all__":
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton) or not getattr(self, '_drag_start', None):
            return
        if not getattr(self, '_folder_dragging', False):
            if (event.pos() - self._drag_start).manhattanLength() < 15:
                return
            self._folder_dragging = True
            px = self.grab()
            eff = QGraphicsOpacityEffect(self)
            eff.setOpacity(0.3)
            self.setGraphicsEffect(eff)
            top = self.window()
            tree = self.parentWidget()
            while tree and not isinstance(tree, FolderTree):
                tree = tree.parentWidget()
            proxy = DragProxy(px, top, sidebar=tree)
            _active_drag.update({"folder_path": self._path, "proxy": proxy,
                                 "source": self, "offset": self._drag_start,
                                 "type": "folder"})
        gpos = event.globalPosition().toPoint()
        proxy = _active_drag.get("proxy")
        if proxy:
            top = self.window()
            offset = _active_drag.get("offset", QPoint(0, 0))
            proxy.move(top.mapFromGlobal(gpos) - offset)
            proxy.update_for_sidebar(gpos)
        # Highlight target folder
        tree = self.parentWidget()
        while tree and not isinstance(tree, FolderTree):
            tree = tree.parentWidget()
        if tree:
            tree.update_drag_hover(gpos)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_dragging = getattr(self, '_folder_dragging', False)
        self._folder_dragging = False
        if was_dragging:
            self._finish_folder_drag(event.globalPosition().toPoint())
        elif getattr(self, '_drag_start', None) is not None:
            self.clicked.emit(self._path)
        self._drag_start = None

    def _finish_folder_drag(self, global_pos: QPoint):
        proxy = _active_drag.pop("proxy", None)
        src_path = _active_drag.pop("folder_path", "")
        _active_drag.clear()
        self.setGraphicsEffect(None)
        tree = self.parentWidget()
        while tree and not isinstance(tree, FolderTree):
            tree = tree.parentWidget()
        if tree:
            tree.clear_drag_hover()
        if proxy:
            proxy.hide()
            proxy.deleteLater()
        if not src_path:
            return
        # Find target folder row
        target_path = ""
        widget_under = QApplication.widgetAt(global_pos)
        while widget_under:
            if isinstance(widget_under, FolderRow) and widget_under is not self:
                target_path = widget_under._path
                break
            widget_under = widget_under.parentWidget()
        if not target_path or target_path == src_path:
            return
        # Don't allow moving into own subtree
        if target_path.startswith(src_path + "/"):
            return
        # Move folder in config
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        # Extract the folder node
        src_parts = src_path.split("/")
        src_name = src_parts[-1]
        # Find and remove source
        node = None
        parent_list = folders
        for part in src_parts[:-1]:
            for f in parent_list:
                if f["name"] == part:
                    parent_list = f.get("children", [])
                    break
        for i, f in enumerate(parent_list):
            if f["name"] == src_name:
                node = parent_list.pop(i)
                break
        if not node:
            return
        # Insert into target
        if target_path == "__all__":
            # Move to root
            folders.append(node)
        else:
            target_parts = target_path.split("/")
            dest = folders
            for part in target_parts:
                for f in dest:
                    if f["name"] == part:
                        dest = f.setdefault("children", [])
                        break
            dest.append(node)
        get_config().set("library_folders", folders)
        # Update game categories
        new_base = f"{target_path}/{src_name}" if target_path != "__all__" else src_name
        lib = get_library()
        for game in lib.all_games():
            if game.category == src_path or game.category.startswith(src_path + "/"):
                game.category = new_base + game.category[len(src_path):]
                lib.update_game(game)
        # Rebuild sidebar
        tree = self.parentWidget()
        while tree and not isinstance(tree, FolderTree):
            tree = tree.parentWidget()
        if tree:
            tree.rebuild()

    def set_drag_hover(self, hover: bool):
        """Highlight this row as a drop target during drag."""
        self._drag_hover = hover
        self._apply_style()

    def _frame_style(self) -> str:
        color = palette(self._color_key)
        if getattr(self, '_drag_hover', False):
            return (f"QFrame#folder_row{{background:{palette('bg_elevated')};border-radius:5px;"
                    f"border:2px dashed {color};}}")
        elif self._selected:
            return (f"QFrame#folder_row{{background:{palette('bg_elevated')};border-radius:5px;"
                    f"border-left:3px solid {color};}}")
        return (f"QFrame#folder_row{{background:transparent;border-radius:5px;}}"
                f"QFrame#folder_row:hover{{background:{palette('bg_hover')};}}")

    def _label_style(self) -> str:
        if getattr(self, '_drag_hover', False):
            return f"color:{palette(self._color_key)};font-size:11px;font-weight:700;background:transparent;"
        elif self._selected:
            return f"color:{palette('text')};font-size:11px;font-weight:600;background:transparent;"
        return f"color:{palette('text_secondary')};font-size:11px;background:transparent;"

    def _apply_style(self):
        # State-dependent (drag/selected) AND palette-dependent — recomputed
        # from the two helpers above, so refresh_styles reflects the theme.
        self.setStyleSheet(self._frame_style())
        self._label.setStyleSheet(self._label_style())

    def refresh_styles(self):
        super().refresh_styles()   # colour bar + "+" button (registered)
        self._apply_style()        # frame + label (state-dependent)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()


class FolderTree(QFrame, ThemedMixin):
    """Sidebar showing the folder tree, plus the tag/engine filter tabs."""
    folder_selected = Signal(str)  # folder_path or "__all__"
    tags_changed = Signal()        # tag selection changed
    engines_changed = Signal()     # engine selection changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(170)
        self._sty(self, lambda: f"background:{palette('bg_card')};border-right:1px solid {palette('border')};")
        self._rows: list[FolderRow] = []
        self._new_folder_btn = None
        self._selected_path = "__all__"

        # Outer layout just hosts the splitter below.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Draggable divider between the folder list and the tag filter, so
        # either section can take more or less of the sidebar's height —
        # replaces the old fixed static separator + hard height cap.
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(7)
        self._sty(self._splitter, lambda: (
            f"QSplitter::handle{{background:{palette('separator')};}}"
            f"QSplitter::handle:hover{{background:{palette('accent')};}}"
        ))
        outer.addWidget(self._splitter)

        # ── Top pane: "All games" row + folder tree + "+ new folder" ────
        self._folders_pane = QWidget()
        self._layout = QVBoxLayout(self._folders_pane)
        self._layout.setContentsMargins(4, 8, 4, 4)
        self._layout.setSpacing(2)
        self._folders_pane.setMinimumHeight(90)
        self._splitter.addWidget(self._folders_pane)

        # ── Bottom pane: "filter by" tabs — tags | engine ─────────────────
        # Two filters of the same shape share the pane. Both stay ACTIVE
        # while hidden: switching tab changes what is being edited, not what
        # is being filtered. "Clear all" empties both tabs.
        self._filters_pane = QWidget()
        filters_col = QVBoxLayout(self._filters_pane)
        filters_col.setContentsMargins(0, 0, 0, 0)
        filters_col.setSpacing(2)

        self._filter_by_lbl = QLabel(t("library.filter_by"))
        self._sty(self._filter_by_lbl, lambda: (
            f"color:{palette('text_muted')};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;background:transparent;"))
        filters_col.addWidget(self._filter_by_lbl)

        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(4)
        self._tab_tags_btn = QPushButton(t("library.tab_tags"))
        self._tab_engines_btn = QPushButton(t("library.tab_engines"))
        self._filter_tab_btns = (self._tab_tags_btn, self._tab_engines_btn)
        self._tab_tags_btn.setToolTip(t("library.filter_tags"))
        self._tab_engines_btn.setToolTip(t("library.filter_engines"))
        for i, btn in enumerate(self._filter_tab_btns):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedHeight(24)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda _=False, idx=i: self._show_filter_tab(idx))
            tabs_row.addWidget(btn, 1)
        filters_col.addLayout(tabs_row)

        self._filter_stack = QStackedWidget()
        self._tag_panel = TagFilterPanel()
        self._tag_panel.tags_changed.connect(self.tags_changed)
        self._engine_panel = TagFilterPanel(
            header_key="library.filter_engines",
            search_key="library.search_engines",
            clear_key="library.clear_engines",
            active_key="library.active_engine_filters",
            merge_variants=False)
        self._engine_panel.tags_changed.connect(self.engines_changed)
        self._tag_panel.cleared.connect(self._clear_sibling_filters)
        self._engine_panel.cleared.connect(self._clear_sibling_filters)
        self._filter_stack.addWidget(self._tag_panel)
        self._filter_stack.addWidget(self._engine_panel)
        filters_col.addWidget(self._filter_stack, 1)
        self._has_unknown_engines = False

        # Was a hard setMaximumHeight(215) (no resize possible at all); now
        # just a floor so the header/search/clear row always stays usable
        # however far the user drags the handle up. TagFilterPanel's own
        # tag-list scroll area grows to fill whatever extra height the
        # splitter gives this pane (see TagFilterPanel._build).
        self._filters_pane.setMinimumHeight(160)
        self._splitter.addWidget(self._filters_pane)
        self._show_filter_tab(0)

        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._restore_splitter_sizes()
        self._splitter.splitterMoved.connect(self._save_splitter_sizes)

        self.rebuild()

    def _restore_splitter_sizes(self) -> None:
        """Re-apply the user's folder/filter split (or the default)."""
        raw = get_config().get("library_filter_splitter", [260, 215]) or [260, 215]
        try:
            sizes = [max(1, int(raw[0])), max(1, int(raw[1]))]
        except (TypeError, ValueError, IndexError, KeyError):
            sizes = [260, 215]
        self._splitter.setSizes(sizes)

    def _save_splitter_sizes(self, *_args) -> None:
        sizes = self._splitter.sizes()
        if len(sizes) < 2 or sizes[0] <= 0 or sizes[1] <= 0:
            return
        get_config().set("library_filter_splitter",
                         [int(sizes[0]), int(sizes[1])])

    def rebuild(self):
        # Clear the folder-list pane (top splitter pane only — the tag
        # panel is a permanent splitter pane and is never rebuilt here)
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._rows.clear()

        # "All games" row — its + creates a ROOT folder ("__all__" is
        # normalized to "" inside _add_folder)
        all_row = FolderRow("__all__", t("library.all_folders"), "accent", 0)
        all_row.clicked.connect(self._on_row_clicked)
        all_row.add_sub.connect(self._add_folder)
        all_row.set_selected(self._selected_path == "__all__")
        self._layout.addWidget(all_row)
        self._rows.append(all_row)

        # Build from config
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        for path, color_key, depth in _flatten_folders(folders):
            name = path.split("/")[-1]
            row = FolderRow(path, name, color_key, depth)
            row.clicked.connect(self._on_row_clicked)
            row.add_sub.connect(self._add_folder)
            row.set_selected(self._selected_path == path)
            self._layout.addWidget(row)
            self._rows.append(row)

        self._layout.addStretch()

        # Add folder button (recreated each rebuild — kept in a single attr so
        # refresh_styles can re-colour it in place; no per-rebuild accumulation)
        add_btn = QPushButton(f"+ {t('library.new_folder')}")
        add_btn.setStyleSheet(self._new_folder_btn_style())
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(lambda: self._add_folder(""))
        self._layout.addWidget(add_btn)
        self._new_folder_btn = add_btn

    def _new_folder_btn_style(self) -> str:
        return (
            f"QPushButton{{color:{palette('text_muted')};font-size:11px;background:transparent;"
            f"border:1px dashed {palette('border')};border-radius:4px;padding:4px;}}"
            f"QPushButton:hover{{color:{palette('accent')};border-color:{palette('accent')};}}"
        )

    def refresh_styles(self):
        # Page chrome of the tree (self bg + splitter handle) is registered…
        super().refresh_styles()
        # …the folder rows, the "+ new folder" button and the tag panel are
        # re-styled in place so NO row/panel is recreated on a theme switch.
        for row in list(self._rows):
            try:
                row.refresh_styles()
            except RuntimeError:
                pass
        if self._new_folder_btn is not None:
            try:
                self._new_folder_btn.setStyleSheet(self._new_folder_btn_style())
            except RuntimeError:
                pass
        self._tag_panel.refresh_styles()
        self._engine_panel.refresh_styles()

    def update_locale(self):
        """Retranslate the sidebar: rebuild covers the folder rows ("All
        games", "+ new folder"); the filter panes are permanent panes rebuild
        never touches, so they retranslate their own header/search/clear."""
        self.rebuild()
        self._filter_by_lbl.setText(t("library.filter_by"))
        self._tab_tags_btn.setText(t("library.tab_tags"))
        self._tab_engines_btn.setText(t("library.tab_engines"))
        self._tab_tags_btn.setToolTip(t("library.filter_tags"))
        self._tab_engines_btn.setToolTip(t("library.filter_engines"))
        self._tag_panel.update_locale()
        self._engine_panel.update_locale()
        # Others chip label is translated — rebuild engine chips so the
        # wording tracks the locale while keeping include/exclude state.
        if self._has_unknown_engines:
            old = getattr(self, "_others_label", None)
            engines = [x for x in self._engine_panel._all_tags if x != old]
            self.update_engines(engines, has_unknown=True)
        self._show_filter_tab(self._filter_stack.currentIndex())

    def update_drag_hover(self, global_pos: QPoint):
        """Highlight the folder row under the cursor during drag."""
        for row in self._rows:
            row_global = row.mapToGlobal(QPoint(0, 0))
            row_rect = row.rect()
            hit = (row_global.x() <= global_pos.x() <= row_global.x() + row_rect.width()
                   and row_global.y() <= global_pos.y() <= row_global.y() + row_rect.height())
            row.set_drag_hover(hit)

    def clear_drag_hover(self):
        """Remove all drag hover highlights."""
        for row in self._rows:
            row.set_drag_hover(False)

    def _clear_sibling_filters(self):
        """Clear the filter tabs that did not raise the clear signal."""
        sender = self.sender()
        for panel in (self._tag_panel, self._engine_panel):
            if panel is not sender:
                panel.clear_selection(emit=False)

    def _show_filter_tab(self, index: int):
        """Switch which filter is on show; both keep filtering either way."""
        self._filter_stack.setCurrentIndex(index)
        for i, btn in enumerate(self._filter_tab_btns):
            active = i == index
            btn.setObjectName("filter_tab")
            btn.setProperty("active", "1" if active else "0")
            # Belt-and-suspenders: Fusion sometimes ignores QSS text colour
            # on property-keyed buttons, leaving green-on-green (invisible).
            if active:
                btn.setStyleSheet(
                    f"QPushButton#filter_tab{{background:{palette('accent')};"
                    f"color:{palette('accent_text')};"
                    f"border:1px solid {palette('accent')};"
                    f"border-radius:3px;font-size:10px;font-weight:700;"
                    f"padding:2px 6px;}}")
            else:
                btn.setStyleSheet(
                    f"QPushButton#filter_tab{{background:{palette('bg_elevated')};"
                    f"color:{palette('text_secondary')};"
                    f"border:1px solid {palette('border_hover')};"
                    f"border-radius:3px;font-size:10px;font-weight:600;"
                    f"padding:2px 6px;}}"
                    f"QPushButton#filter_tab:hover{{color:{palette('text')};"
                    f"border-color:{palette('accent')};}}")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

    def update_tags(self, all_tags: list[str]):
        """Refresh available tags from game library."""
        self._tag_panel.set_tags(all_tags)

    def update_engines(self, all_engines: list[str], *, has_unknown: bool = False):
        """Refresh available engines from the game library.

        Populated from the games themselves rather than from the full table
        of known engines: a filter offering twelve engines for a library with
        two is a longer list that says less. *has_unknown* adds an Others
        chip for titles whose engine was not identified.
        """
        self._has_unknown_engines = bool(has_unknown)
        old_others = getattr(self, "_others_label", None)
        self._others_label = t("library.engine_others")
        # Keep include/exclude across a locale switch when only the Others
        # wording changed.
        if old_others and old_others != self._others_label:
            st = self._engine_panel._states.pop(old_others, None)
            if st:
                self._engine_panel._states[self._others_label] = st
        chips = [x for x in all_engines
                 if x and x != old_others and x != self._others_label]
        if has_unknown:
            chips.append(self._others_label)
        self._engine_panel.set_tags(chips)

    def engine_others_label(self) -> str:
        """Current translated label of the Others engine chip."""
        return getattr(self, "_others_label", None) or t("library.engine_others")

    def get_selected_tags(self) -> set[str]:
        return self._tag_panel.get_selected()

    def get_excluded_tags(self) -> set[str]:
        return self._tag_panel.get_excluded()

    def get_selected_engines(self) -> set[str]:
        return self._engine_panel.get_selected()

    def get_excluded_engines(self) -> set[str]:
        return self._engine_panel.get_excluded()

    def has_active_filters(self) -> bool:
        """Whether any chip filter is narrowing the library right now."""
        return bool(self.get_selected_tags() or self.get_excluded_tags()
                    or self.get_selected_engines()
                    or self.get_excluded_engines())

    def _on_row_clicked(self, path: str):
        self._selected_path = path
        for row in self._rows:
            row.set_selected(row._path == path)
        self.folder_selected.emit(path)

    def _add_folder(self, parent_path: str):
        # The all-games row's + / context menu create ROOT folders
        if parent_path == "__all__":
            parent_path = ""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setWindowTitle(t("library.new_folder"))
        dlg.setMinimumWidth(320)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        lay.setContentsMargins(16, 16, 16, 16)

        # Transient application-modal dialog: it cannot be open across a theme
        # switch, so its palette-derived styles are read once at creation
        # (built into style vars to keep the setStyleSheet calls palette-free).
        _dlg_lbl_style = f"color:{palette('text_muted')};font-size:11px;font-weight:600;"

        # Name input
        name_lbl = QLabel(t("library.folder_name"))
        name_lbl.setStyleSheet(_dlg_lbl_style)
        lay.addWidget(name_lbl)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText(t("library.folder_name"))
        lay.addWidget(name_edit)

        # Color picker — horizontal row of colored buttons
        color_lbl = QLabel(t("library.folder_color"))
        color_lbl.setStyleSheet(_dlg_lbl_style)
        lay.addWidget(color_lbl)
        color_row = QHBoxLayout()
        color_row.setSpacing(6)
        selected_color = {"key": FOLDER_COLOR_KEYS[0]}  # default
        color_buttons: list[tuple[QPushButton, str]] = []  # (btn, color_key)
        for ck in FOLDER_COLOR_KEYS:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            hex_c = palette(ck)
            btn.setStyleSheet(_swatch_style(hex_c, False))
            def _on_pick(_checked, c=ck, b=btn):
                selected_color["key"] = c
                for ob, ob_ck in color_buttons:
                    h = palette(ob_ck)
                    ob.setStyleSheet(_swatch_style(h, False))
                h = palette(c)
                b.setStyleSheet(_swatch_style(h, True))
            btn.clicked.connect(_on_pick)
            color_row.addWidget(btn)
            color_buttons.append((btn, ck))
        color_row.addStretch()
        lay.addLayout(color_row)
        # Select first by default
        if color_buttons:
            color_buttons[0][0].click()

        # OK / Cancel
        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(dlg.accept)
        bbox.rejected.connect(dlg.reject)
        lay.addWidget(bbox)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = name_edit.text().strip()
        if not name:
            return
        color_key = selected_color["key"]
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        if _add_folder_to_tree(folders, parent_path, name, color_key):
            get_config().set("library_folders", folders)
            self.rebuild()

    def contextMenuEvent(self, event):
        # Find which row was right-clicked
        for row in self._rows:
            if row.geometry().contains(event.pos()):
                if row._path == "__all__":
                    # All-games row: just the root "new folder" shortcut
                    menu = QMenu(self)
                    menu.setStyleSheet(
                        f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
                        f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
                        f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
                        f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
                    )
                    menu.addAction(f"+ {t('library.new_folder')}",
                                   lambda: self._add_folder(""))
                    menu.exec(event.globalPos())
                else:
                    self._show_folder_menu(row._path, event.globalPos())
                return

    def _show_folder_menu(self, path: str, pos):
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
            f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
            f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        menu.addAction(t("library.add_subfolder"), lambda: self._add_folder(path))
        menu.addAction(t("library.rename_folder"), lambda: self._rename_folder(path))
        menu.addSeparator()
        color_menu = QMenu(t("library.change_folder_color"), menu)
        color_menu.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
            f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
            f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        menu.addMenu(color_menu)
        for ck in FOLDER_COLOR_KEYS:
            color_name = ck.replace("folder_", "")
            label = t(f"library.color_{color_name}")
            px = QPixmap(12, 12)
            px.fill(QColor(palette(ck)))
            color_menu.addAction(QIcon(px), label, lambda c=ck: self._change_color(path, c))
        menu.addSeparator()
        menu.addAction(t("library.delete_folder"), lambda: self._delete_folder(path))
        menu.exec(pos)

    def _rename_folder(self, path: str):
        old_name = path.split("/")[-1]
        new_name, ok = input_text_window_modal(
            self, t("library.rename_folder"), t("library.folder_name"), text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        # Find and rename
        parts = path.split("/")
        current = folders
        for part in parts[:-1]:
            for f in current:
                if f["name"] == part:
                    current = f.get("children", [])
                    break
        for f in current:
            if f["name"] == parts[-1]:
                f["name"] = new_name
                break
        get_config().set("library_folders", folders)
        # Update games that were in this folder
        new_path = "/".join(parts[:-1] + [new_name]) if len(parts) > 1 else new_name
        lib = get_library()
        for game in lib.all_games():
            if game.category == path or game.category.startswith(path + "/"):
                game.category = new_path + game.category[len(path):]
                lib.update_game(game)
        self.rebuild()

    def _change_color(self, path: str, color_key: str):
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        parts = path.split("/")
        current = folders
        for part in parts[:-1]:
            for f in current:
                if f["name"] == part:
                    current = f.get("children", [])
                    break
        for f in current:
            if f["name"] == parts[-1]:
                f["color"] = color_key
                break
        get_config().set("library_folders", folders)
        self.rebuild()

    def _delete_folder(self, path: str):
        reply = question_window_modal(
            self, t("library.delete_folder"),
            t("library.delete_folder_confirm", name=path.split("/")[-1]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        _remove_folder_from_tree(folders, path)
        get_config().set("library_folders", folders)
        # Clear category for games in this folder
        lib = get_library()
        for game in lib.all_games():
            if game.category == path or game.category.startswith(path + "/"):
                game.category = ""
                lib.update_game(game)
        if self._selected_path == path:
            self._selected_path = "__all__"
            self.folder_selected.emit("__all__")
        self.rebuild()



