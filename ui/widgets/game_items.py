"""
SaveSync - Library game items (grid card + list row) and their helpers.

Extracted verbatim from ui/pages/library_page.py: the status/display
helpers, image-cache lookup, save-folder open helpers, the shared
_GameItemMixin (manual drag, context menu, playing badge) and the GameCard/
GameRow widgets. Pure move — no behavior change. The mixin MUST stay first
in the MRO of both classes so its Qt virtual handlers win over QWidget's.
"""
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QObject, QSize, QRectF
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter, QImageReader
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QMenu, QApplication, QGraphicsOpacityEffect,
)

from core.library import GameEntry, get_library
from core.config_manager import get_config
from i18n import t
from ui.helpers import (display_scale, load_pixmap_any as _load_pixmap_any,
                        open_in_file_manager, scaled_for_screen)
from ui.styles.theme import palette, ThemedMixin
from ui.widgets.library_drag import _active_drag, DragProxy
from ui.widgets.library_folders import (FolderRow, _clean_tag_display,
                                        _ensure_children_field,
                                        _flatten_folders,
                                        _get_folder_color_by_path,
                                        _hex_to_rgb)

logger = logging.getLogger(__name__)


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".ico", ".avif"}

STATUS_ICONS  = {"synced":"✓","pending":"⟳","conflict":"⚠","local_only":"💾","cloud_only":"☁","no_saves":"—","provisional":"◌"}

# Each sort criterion's own "natural" direction — what its underlying
# comparison already produces before any user-chosen reversal. Newest/most
# first for anything date- or amount-based; alphabetical for name; a fixed
# severity order for status. Used both as the default shown when a
# criterion is first selected and as the pivot _sort_games reverses against.
_STATUS_PALETTE_KEY = {
    "synced": "success", "pending": "warning", "conflict": "error",
    "local_only": "info", "cloud_only": "cloud", "no_saves": "text_hint",
    "provisional": "provisional",
}

def _status_color(status: str) -> str:
    """Return theme-aware color for a sync status."""
    key = _STATUS_PALETTE_KEY.get(status, "text_hint")
    return palette(key)

def _display_sync_status(entry) -> str:
    """Sync-status bucket for display purposes (card badge, filters, sort).

    Identical to entry.sync_status/"local_only" whenever the game has
    confirmed save_paths. When it doesn't, "no_saves" is upgraded to
    "provisional" if live tracking has already produced at least one
    provisional (pre-confirmation) backup for this game — there IS
    restorable data, the user just hasn't confirmed which paths to keep.
    A genuinely untouched game (nothing detected at all) still reads
    "no_saves".
    """
    if entry.save_paths:
        return entry.sync_status or "local_only"
    try:
        from core.backup import get_backup_manager
        for b in get_backup_manager().get_backups_for_game(entry.id):
            if (b.cloud_metadata or {}).get("pre_confirmation"):
                return "provisional"
    except Exception:
        pass
    return "no_saves"

PLACEHOLDER_ICON = "🎮"


def _fmt_dt(iso: Optional[str]) -> str:
    if not iso: return t("library.never")
    from core import to_local_dt
    from i18n import format_dt
    dt = to_local_dt(iso)
    if dt is None: return iso
    return format_dt(dt, "%d %b %Y")


def _fmt_dt_short(iso: Optional[str]) -> str:
    """Like _fmt_dt but includes the time — used for 'when was the last
    session launched' hover text."""
    if not iso: return t("library.never")
    from core import to_local_dt
    from i18n import format_dt
    dt = to_local_dt(iso)
    if dt is None: return iso
    return format_dt(dt, "%d %b %H:%M")


def _session_hover(entry: GameEntry, font_size: str) -> tuple[str, str]:
    """Hover (text, style) for a playtime label: the most recent session's
    duration. Empty strings when there is no recorded session (no swap)."""
    if entry.last_session_seconds <= 0:
        return "", ""
    txt = f"▶ {t('library.last_session')}: {entry.get_last_session_formatted()}"
    return txt, f"color:{palette('accent')};font-size:{font_size};background:transparent;"


def _sync_hover(entry: GameEntry, font_size: str) -> tuple[str, str]:
    """Hover (text, style) for a sync-status label: when the game was last
    synced. Empty strings when it never synced (no swap)."""
    if not entry.last_synced:
        return "", ""
    txt = f"☁ {t('library.last_synced')}: {_fmt_dt_short(entry.last_synced)}"
    return txt, f"color:{palette('accent')};font-size:{font_size};font-weight:500;background:transparent;"


def _icon_cache_dirs(entry: GameEntry) -> list[Path]:
    """Cache folders that may hold this game's images: the game's own
    USER_DATA_DIR/icons/<folder> plus the stored icon's parent when that
    differs (handles name-changed games)."""
    from core.constants import USER_DATA_DIR, get_install_folder_name
    game_folder = get_install_folder_name(entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)
    dirs = [USER_DATA_DIR / "icons" / game_folder]
    if entry.icon_path:
        icon_cache = Path(entry.icon_path).parent
        if icon_cache not in dirs and icon_cache.parent == USER_DATA_DIR / "icons":
            dirs.append(icon_cache)
    return dirs


def _cache_image_files(entry: GameEntry) -> set[str]:
    """Lowercased paths of every image currently inside the game's icon-cache
    folder(s) — a cheap listing GameCard uses on hover to detect images added
    since the last full scan."""
    found: set[str] = set()
    for d in _icon_cache_dirs(entry):
        try:
            if d.exists():
                for f in d.iterdir():
                    if f.is_file() and f.suffix.lower() in _IMG_EXTS:
                        found.add(str(f).lower())
        except OSError:
            pass
    return found


def _find_all_game_images(entry: GameEntry) -> list[str]:
    """Search near exe_path for ALL image files that look like game covers.
    Returns a list of paths sorted by relevance (best match first)."""
    results: list[str] = []
    # If a user-set icon exists, put it first.
    # Resolve stale refs: compression may have renamed .png → .jpg.
    _icon = entry.icon_path
    if _icon and not Path(_icon).exists():
        _jpg_alt = Path(_icon).with_suffix(".jpg")
        if _jpg_alt.exists():
            _icon = str(_jpg_alt)
    if _icon and Path(_icon).exists():
        results.append(_icon)

    # Icon-cache folder(s) — scanned regardless of exe_path so images dropped
    # into the cache show up even for entries with no executable set.
    seen_lower = {p.lower() for p in results}
    for cache_dir in _icon_cache_dirs(entry):
        try:
            if not cache_dir.exists():
                continue
            for f in cache_dir.iterdir():
                if f.is_file() and f.suffix.lower() in _IMG_EXTS:
                    fp = str(f)
                    if fp.lower() in seen_lower:
                        continue
                    seen_lower.add(fp.lower())
                    results.append(fp)
        except OSError:
            pass

    if not entry.exe_path:
        return results

    root = Path(entry.exe_path).parent
    candidates: list[tuple[int, Path]] = []
    try:
        if root.exists():
            for f in root.iterdir():
                if f.suffix.lower() not in _IMG_EXTS:
                    continue
                if _icon and str(f) == _icon:
                    continue
                name = f.stem.lower()
                game_slug = "".join(c for c in entry.name.lower() if c.isalnum())
                score = 0
                if game_slug and game_slug in name.replace(" ", ""):
                    score += 50
                for kw in ("cover", "art", "banner", "poster", "header", "capsule", "box"):
                    if kw in name:
                        score += 20
                try:
                    if f.stat().st_size > 5000:
                        score += 5
                except OSError:
                    pass
                candidates.append((score, f))
    except (PermissionError, OSError) as e:
        logger.debug(f"Could not scan for game images near {entry.exe_path}: {e}")
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, f in candidates:
            fp = str(f)
            if fp.lower() not in seen_lower:
                seen_lower.add(fp.lower())
                results.append(fp)
    return results


def _find_game_image(entry: GameEntry) -> Optional[str]:
    """Search near exe_path for the best image file that looks like a game cover."""
    images = _find_all_game_images(entry)
    return images[0] if images else None


# ── Cover framing ─────────────────────────────────────────────────────────────
# cover_focus carries the whole framing, as a plain string so it round-trips
# through the library JSON untouched and every _make_pixmap call site keeps
# working unchanged:
#   "center", "top-left", …   the 3x3 grid (what every existing entry has)
#   "custom:<mode>:<zoom>:<x>:<y>"
#       mode  cover | fit | stretch   zoom  ≥ 1.0
#       x, y  0..1 — where the visible window sits (0.5 = centred), the same
#             axis the 3x3 grid samples at 0 / 0.5 / 1.
COVER_MODES = ("cover", "fit", "stretch")
_FOCUS_AXIS = {"left": 0.0, "top": 0.0, "center": 0.5, "right": 1.0, "bottom": 1.0}


def parse_cover_focus(focus: Optional[str]) -> tuple[str, float, float, float]:
    """(mode, zoom, x, y) for any focus string, past or present.

    Total by design: entries written before custom framing existed, and
    anything corrupted, fall back to the plain centred cover rather than
    raising in the middle of painting a card.
    """
    text = (focus or "center").strip()
    if text.lower().startswith("custom:"):
        parts = text.split(":")
        try:
            mode = parts[1].strip().lower()
            if mode not in COVER_MODES:
                mode = "cover"
            zoom = max(0.1, min(8.0, float(parts[2])))
            x = max(0.0, min(1.0, float(parts[3])))
            y = max(0.0, min(1.0, float(parts[4])))
            return mode, zoom, x, y
        except (IndexError, ValueError, TypeError):
            return "cover", 1.0, 0.5, 0.5
    # 3x3 grid: "top-left", "center-right", … — each half names one axis.
    x = y = 0.5
    for token in text.lower().split("-"):
        if token in ("left", "right"):
            x = _FOCUS_AXIS[token]
        elif token in ("top", "bottom"):
            y = _FOCUS_AXIS[token]
    return "cover", 1.0, x, y


def format_cover_focus(mode: str, zoom: float, x: float, y: float) -> str:
    """Serialize a custom framing back into cover_focus."""
    mode = mode if mode in COVER_MODES else "cover"
    return f"custom:{mode}:{zoom:.3f}:{x:.4f}:{y:.4f}"


def render_cover(px: QPixmap, w: int, h: int, focus: Optional[str] = "center",
                 dpr: float = 1.0) -> QPixmap:
    """Frame *px* into a w×h cover according to *focus*.

    One path for all three modes — they differ only in how the source is
    scaled, never in how it is positioned:
      cover    fill the frame, overflow cropped (the historical behaviour)
      fit      whole image visible, letterboxed
      stretch  distorted to fill exactly
    Position comes from (x, y): with the image larger than the frame it
    chooses which part shows, with it smaller it chooses where it sits.

    *w* and *h* are Qt's coordinates — the size the cover will occupy. The
    pixmap is built *dpr* times that in real pixels and told so, which is
    what lets it be drawn one pixel to one pixel instead of stretched.
    """
    mode, zoom, x, y = parse_cover_focus(focus)
    dpr = max(1.0, float(dpr or 1.0))
    # Everything below is in real pixels; only the size stamped on the result
    # is in Qt's.
    pw, ph = max(1, int(round(w * dpr))), max(1, int(round(h * dpr)))
    tw = max(1, int(round(pw * zoom)))
    th = max(1, int(round(ph * zoom)))
    aspect = {
        "cover": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        "fit": Qt.AspectRatioMode.KeepAspectRatio,
        "stretch": Qt.AspectRatioMode.IgnoreAspectRatio,
    }[mode]
    scaled = px.scaled(tw, th, aspect, Qt.TransformationMode.SmoothTransformation)

    canvas = QPixmap(pw, ph)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        # int() truncates toward zero, which reproduces the old
        # `extra // 2` / `extra` crop offsets exactly for the 3x3 values.
        dx = int((pw - scaled.width()) * x)
        dy = int((ph - scaled.height()) * y)
        painter.drawPixmap(dx, dy, scaled)
    finally:
        painter.end()
    canvas.setDevicePixelRatio(dpr)
    return canvas


# Covers are decoded on the GUI thread, once per card, every time the library
# rebuilds — so both halves of that cost are worth cutting.
_COVER_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_COVER_CACHE_MAX = 192          # ~a page of cards in both view modes, plus slack
# …but a cover on a scaled display is that scale SQUARED in bytes, so the
# count comes down to keep the memory where it was. Never below a page of
# cards, which is what the cache exists to hold.
_COVER_CACHE_MIN = 48


def _cover_cache_max(dpr: float) -> int:
    scale = max(1.0, float(dpr or 1.0)) ** 2
    return max(_COVER_CACHE_MIN, int(_COVER_CACHE_MAX / scale))

# Above this many source pixels, decoding the file at full resolution costs
# more than asking the decoder for a smaller image. BELOW it the reverse is
# true — the reader's own overhead (header parse, non-native scaling) makes
# small files several times slower that way — which is why this is a
# threshold and not an always-on optimisation. Measured crossover sits
# between 2 and 8 megapixels.
_BIG_IMAGE_PIXELS = 4_000_000


# How much detail to keep beyond what the render strictly needs. The final
# step is a smooth downscale, which wants more source than target to look
# good. Measured on a cover full of text and thin rules — the content that
# actually shows the damage, unlike a gradient:
#   margin 1: mean error 9/255, peaks at 50, visibly pixelated
#   margin 2: mean 3.9, peaks at 28
#   margin 4: mean 1.4, peaks at 15 — indistinguishable, and still 2-3.5x
#             faster than decoding the file at full resolution
_COVER_DETAIL_MARGIN = 4


def _load_cover_source(path: str, need_w: int, need_h: int) -> QPixmap:
    """Decode *path* cheaply, at a size that still comfortably covers
    need_w × need_h.

    A 4000x6000 cover is 24 million pixels on its way to a 186x240 card. Qt's
    JPEG/PNG readers can produce a reduced image directly; for JPEG a
    power-of-two reduction is what libjpeg does natively, so the size asked
    for is always src/2, /4 or /8 rather than an exact fit.

    *need_w/need_h* are what render_cover will actually scale to — which is
    the card size TIMES the framing zoom, up to 8x. Reducing against the card
    size alone left a zoomed-in cover being blown back up from a thumbnail,
    which is what made them look pixelated.
    """
    floor_w = max(1, need_w) * _COVER_DETAIL_MARGIN
    floor_h = max(1, need_h) * _COVER_DETAIL_MARGIN
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        src = reader.size()
        if (src.isValid() and src.width() > 0
                and src.width() * src.height() > _BIG_IMAGE_PIXELS):
            sw, sh, shift = src.width(), src.height(), 0
            while shift < 3 and sw // 2 >= floor_w and sh // 2 >= floor_h:
                sw //= 2
                sh //= 2
                shift += 1
            if shift:
                reader.setScaledSize(QSize(sw, sh))
                img = reader.read()
                if not img.isNull():
                    return QPixmap.fromImage(img)
    except Exception as e:
        logger.debug(f"Reduced-size decode failed for '{path}': {e}")
    # Small image, unsupported format, or the reader failed — the plain path
    # also carries the PIL fallback (AVIF and friends).
    return _load_pixmap_any(path)


def _make_pixmap(path: Optional[str], w: int, h: int, focus: str = "center") -> Optional[QPixmap]:
    if not path:
        return None
    try:
        # Keyed on mtime and size too: an edited or replaced cover file must
        # not keep serving the old render.
        # The screen's scale belongs in the key: the same cover at the same
        # size is a different number of real pixels on a scaled display, and
        # serving the smaller one would put the pixelation straight back.
        dpr = display_scale()
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size, w, h, focus, dpr)
        except OSError:
            key = None
        if key is not None:
            hit = _COVER_CACHE.get(key)
            if hit is not None:
                _COVER_CACHE.move_to_end(key)
                return hit

        # render_cover scales to the card size times the framing zoom AND the
        # screen's scale, so the decode has to be told that size — asking for
        # the card size alone is what left a cover being blown back up.
        _mode, _zoom, _x, _y = parse_cover_focus(focus)
        _z = max(1.0, _zoom) * dpr
        px = _load_cover_source(path, int(round(w * _z)), int(round(h * _z)))
        if px.isNull():
            logger.debug(f"Pixmap load returned null for path: {path}")
            return None
        out = render_cover(px, w, h, focus, dpr)
        if key is not None and out is not None:
            _COVER_CACHE[key] = out
            while len(_COVER_CACHE) > _cover_cache_max(dpr):
                _COVER_CACHE.popitem(last=False)
        return out
    except Exception as e:
        logger.debug(f"Pixmap load failed for path '{path}': {e}")
        return None


def _resolve_save_folder_targets(entry: GameEntry) -> list:
    """The game's registered save paths as openable targets — existing
    folders (a file path resolves to its parent) plus virtual registry
    entries kept as their ``registry:`` strings (opened in regedit).
    Duplicates (case-insensitive) collapse, original order is kept."""
    from core.registry_saves import is_registry_path, registry_key_exists
    targets: list = []
    seen: set[str] = set()
    for sp in entry.save_paths:
        if is_registry_path(sp):
            key = sp.lower()
            if key in seen or not registry_key_exists(sp):
                continue
            seen.add(key)
            targets.append(sp)
            continue
        p = Path(sp)
        target = p if p.is_dir() else p.parent
        key = str(target).lower()
        if key in seen or not target.exists():
            continue
        seen.add(key)
        targets.append(target)
    return targets


def _open_save_target(target):
    from core.registry_saves import is_registry_path, open_in_regedit
    if is_registry_path(str(target)):
        open_in_regedit(str(target))
    else:
        open_in_file_manager(target)


def _populate_save_folder_menu(menu: QMenu, targets: list) -> None:
    """Fill *menu* with one entry per save target (short two-segment label,
    full path as tooltip; registry keys open in regedit). Shared by the
    context-menu SUBMENU and the standalone chooser popup."""
    from core.registry_saves import is_registry_path, registry_display
    menu.setToolTipsVisible(True)   # short labels — full path shown on hover
    for target in targets:
        if is_registry_path(str(target)):
            disp = registry_display(str(target))
            short = "\\".join(disp.split("\\")[-2:])
            action = menu.addAction(f"🗝  {short}")
            action.setToolTip(disp)
        else:
            parts = target.parts
            short = str(Path(*parts[-2:])) if len(parts) >= 2 else str(target)
            action = menu.addAction(f"📂  {short}")
            action.setToolTip(str(target))
        action.triggered.connect(lambda checked=False, p=target: _open_save_target(p))




def _web_search_game_dialog(item: QWidget, game_id: str):
    """Context-menu "web search": open the edit dialog pre-armed with a web
    search, then refresh *item* (a GameCard or GameRow) when it closes."""
    from ui.dialogs.add_game_dialog import AddGameDialog

    entry = get_library().get_by_id(game_id)
    if not entry:
        return
    # Create dialog with no parent to ensure it's shown correctly
    dialog = AddGameDialog(entry=entry, parent=None)
    # Start search after dialog is shown
    QTimer.singleShot(200, dialog._web_search)
    dialog.exec()
    # Refresh after dialog closes (entry may have been updated)
    refreshed = get_library().get_by_id(game_id)
    if refreshed:
        item.refresh(refreshed)


# ── Game Card (grid view) ─────────────────────────────────────────────────────

class _PlaytimeLabel(QLabel, ThemedMixin):
    """Card playtime label with a hover effect: shows total playtime
    normally and the most recent session's duration while hovered.

    Only the TEXT swap happens here — both colours are constant per theme
    and live in the QSS under ``#playtime_lbl``, so the label carries no
    stylesheet of its own (a library page holds one per card, and each one
    would cost triple to re-polish on a theme switch)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playtime_lbl")
        self._normal_text = ""
        self._hover_text = ""
        self._hovering = False

    def set_entry(self, entry: GameEntry):
        total = entry.get_playtime_formatted()
        self._normal_text = f"🕐 {total}" if entry.playtime_seconds > 0 else ""
        if entry.last_session_seconds > 0:
            session = entry.get_last_session_formatted()
            launched = _fmt_dt_short(entry.last_played)
            # Hover shows the most recent session's duration AND when it was
            # launched. The tooltip popup is intentionally dropped — the
            # in-place text swap already conveys this.
            self._hover_text = f"▶ {session} · {launched}"
        else:
            self._hover_text = ""
        self.setToolTip("")
        # Drives #playtime_lbl[hasHover="1"]:hover — without a session to show,
        # the text stays put on hover, so the colour must stay put too.
        has_hover = "1" if (self._hover_text and self._normal_text) else "0"
        if self.property("hasHover") != has_hover:
            self.setProperty("hasHover", has_hover)
            self.style().unpolish(self)
            self.style().polish(self)
        self._apply(self._hovering)

    def _apply(self, hovering: bool):
        self._hovering = hovering
        if hovering and self._hover_text and self._normal_text:
            self.setText(self._hover_text)
        else:
            self.setText(self._normal_text)

    def refresh_styles(self):
        super().refresh_styles()
        self._apply(self._hovering)   # keep the right text for the hover state

    def enterEvent(self, event):
        super().enterEvent(event)
        self._apply(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._apply(False)


class _HoverSwapLabel(QLabel, ThemedMixin):
    """QLabel that swaps to an alternate text/style while hovered and
    restores its normal text/style on leave — an in-place hover effect with
    no tooltip popup. Used for the card sync-status label and the list-view
    playtime label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._normal_text = ""
        self._normal_style = ""
        self._hover_text = ""
        self._hover_style = ""
        self._hovering = False

    def set_texts(self, normal_text: str, normal_style: str,
                  hover_text: str = "", hover_style: str = ""):
        self._normal_text = normal_text
        self._normal_style = normal_style
        self._hover_text = hover_text
        self._hover_style = hover_style or normal_style
        self._apply(self._hovering)

    def _apply(self, hovering: bool):
        self._hovering = hovering
        if hovering and self._hover_text:
            self.setText(self._hover_text)
            self.setStyleSheet(self._hover_style)
        else:
            self.setText(self._normal_text)
            self.setStyleSheet(self._normal_style)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._apply(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._apply(False)


class _GameItemMixin:
    """Behaviour shared by GameCard (grid view) and GameRow (list view):
    manual drag to folders, the full context menu with its actions, and
    the playing-badge toggle. Both classes provide the attributes and
    signals the mixin relies on (_entry, detail_requested,
    backup_requested, …) — keeping ONE copy prevents the drift that
    once broke web-search in the list view only."""

    def _show_context_menu(self, anchor):
        self._show_context_menu_at(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def contextMenuEvent(self, event):
        self._show_context_menu_at(event.globalPos())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton) or not getattr(self, '_drag_start', None):
            return
        if not self._dragging:
            if (event.pos() - self._drag_start).manhattanLength() < 15:
                return
            self._dragging = True
            # Grab pixmap BEFORE dimming
            px = self.grab()
            # Dim original
            eff = QGraphicsOpacityEffect(self)
            eff.setOpacity(0.3)
            self.setGraphicsEffect(eff)
            top = self.window()
            tree = self._find_folder_tree()
            proxy = DragProxy(px, top, sidebar=tree)
            _active_drag.update({"game_id": self._entry.id, "proxy": proxy,
                                 "source": self, "offset": self._drag_start})
        gpos = event.globalPosition().toPoint()
        proxy = _active_drag.get("proxy")
        if proxy:
            top = self.window()
            offset = _active_drag.get("offset", QPoint(0, 0))
            proxy.move(top.mapFromGlobal(gpos) - offset)
            proxy.update_for_sidebar(gpos)
        tree = self._find_folder_tree()
        if tree:
            tree.update_drag_hover(gpos)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_dragging = getattr(self, '_dragging', False)
        self._dragging = False
        if was_dragging:
            self._finish_drag(event.globalPosition().toPoint())
        elif getattr(self, '_drag_start', None) is not None:
            self.detail_requested.emit(self._entry.id)
        self._drag_start = None

    def _find_folder_tree(self):
        p = self._find_library_page()
        return p._folder_tree if p else None

    def _find_library_page(self):
        # Call-time import: library_page imports THIS module at load time,
        # so a top-level import here would be circular.
        from ui.pages.library_page import LibraryPage
        w = self.parentWidget()
        while w:
            if isinstance(w, LibraryPage):
                return w
            w = w.parentWidget()
        return None

    def _finish_drag(self, global_pos: QPoint):
        """End manual drag — check drop target and clean up."""
        proxy = _active_drag.pop("proxy", None)
        game_id = _active_drag.pop("game_id", "")
        _active_drag.clear()
        self.setGraphicsEffect(None)
        tree = self._find_folder_tree()
        if tree:
            tree.clear_drag_hover()
        if proxy:
            proxy.hide()
            proxy.deleteLater()
        widget_under = QApplication.widgetAt(global_pos)
        while widget_under:
            if isinstance(widget_under, FolderRow):
                lib = get_library()
                entry = lib.get_by_id(game_id)
                if entry:
                    path = widget_under._path
                    entry.category = "" if path == "__all__" else path
                    lib.update_game(entry)
                break
            widget_under = widget_under.parentWidget()

    def _show_context_menu_at(self, pos):
        self._build_context_menu().exec(pos)

    def _build_context_menu(self) -> QMenu:
        """Build the full context menu (split from exec for testability)."""
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
            f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
            f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        menu.addAction(t('library.details'),         lambda: self.detail_requested.emit(self._entry.id))
        menu.addAction(t("library.backup_now"),  lambda: self.backup_requested.emit(self._entry.id))
        menu.addAction(t("sync.sync_now"),       lambda: self.sync_requested.emit(self._entry.id))
        menu.addAction(t("library.restore"),     lambda: self.restore_requested.emit(self._entry.id))
        # Straight to the save editor for THIS game — the page's search step
        # is skipped, which is the point of coming from here.
        menu.addAction(t("cheats.menu"),         lambda: self.cheats_requested.emit(self._entry.id))
        if _display_sync_status(self._entry) == "provisional":
            menu.addAction(t("library.review_provisional"),
                            lambda: self.review_provisional_requested.emit(self._entry.id))
        menu.addSeparator()
        self._add_folder_submenu(menu)
        menu.addAction(t("library.edit"),        lambda: self.edit_requested.emit(self._entry.id))
        menu.addAction(t("library.web_search"),  lambda: self._web_search_game(self._entry.id))
        menu.addAction(t("library.adjust_cover_focus"), lambda: self._show_cover_focus_dialog())
        # "Open save folder": with several targets the choice is a SUBMENU —
        # exec-ing a second popup from this menu's triggered handler left a
        # floating empty popup on Windows (nested popup during teardown).
        _targets = _resolve_save_folder_targets(self._entry)
        if len(_targets) > 1:
            # Explicit parent: the wrapper addMenu(str) returns can be
            # garbage-collected at method exit taking the C++ submenu with
            # it (empirically reproduced) — parenting to the menu keeps it
            # alive for the exec that happens in the caller.
            sub = QMenu(t("library.open_folder"), menu)
            sub.setStyleSheet(
                f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
                f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
                f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
                f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
            )
            _populate_save_folder_menu(sub, _targets)
            menu.addMenu(sub)
        elif len(_targets) == 1:
            _t0 = _targets[0]
            menu.addAction(t("library.open_folder"),
                           lambda checked=False, p=_t0: _open_save_target(p))
        else:
            act = menu.addAction(t("library.open_folder"))
            act.setEnabled(False)
        menu.addSeparator()
        menu.addAction(t("library.remove"),      lambda: self.remove_requested.emit(self._entry.id))
        return menu

    def _add_folder_submenu(self, menu: QMenu):
        folders = get_config().get("library_folders", [])
        if not folders:
            return
        _ensure_children_field(folders)
        flat = _flatten_folders(folders)
        if not flat:
            return
        # Explicit parent + addMenu(QMenu): the wrapper addMenu(str) returns
        # can be GC'd once this method exits (empirically reproduced), and
        # exec happens later in the caller.
        sub = QMenu(t("library.move_to_folder"), menu)
        sub.setStyleSheet(
            f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
            f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
            f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        menu.addMenu(sub)
        sub.addAction(t("library.no_folder"), lambda: self._set_folder(""))
        sub.addSeparator()
        for path, color_key, depth in flat:
            name = path.split("/")[-1]
            indent = "  " * depth
            px = QPixmap(12, 12)
            px.fill(QColor(palette(color_key)))
            sub.addAction(QIcon(px), f"{indent}{name}",
                          lambda p=path: self._set_folder(p))

    def _set_folder(self, folder_path: str):
        lib = get_library()
        entry = lib.get_by_id(self._entry.id)
        if entry:
            entry.category = folder_path
            lib.update_game(entry)

    def _web_search_game(self, game_id: str):
        _web_search_game_dialog(self, game_id)

    def _show_cover_focus_dialog(self):
        """Show a dialog with a 3x3 grid to select cover focus position."""
        from PySide6.QtWidgets import QDialog, QGridLayout, QPushButton, QLabel
        from PySide6.QtCore import QEvent
        
        dialog = QDialog(self)
        dialog.setWindowTitle(t("library.adjust_cover_focus"))
        dialog.setFixedSize(320, 380)
        
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        # Instructions
        info = QLabel(t("library.cover_focus_instruction"))
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
        layout.addWidget(info)
        
        # Load the original image once for all previews
        original_px = None
        if self._img_path:
            try:
                original_px = _load_pixmap_any(self._img_path)
            except Exception:
                pass
        
        # 3x3 grid of buttons showing preview
        grid_layout = QGridLayout()
        grid_layout.setSpacing(8)
        
        focus_positions = [
            "top-left", "top-center", "top-right",
            "center-left", "center", "center-right",
            "bottom-left", "bottom-center", "bottom-right"
        ]
        
        current_focus = getattr(self._entry, 'cover_focus', 'center')
        
        # Use same aspect ratio as actual card (186x240) but half resolution for performance
        card_w, card_h = 186, 240
        preview_w, preview_h = card_w // 2, card_h // 2  # 93x120
        button_w, button_h = 100, 125  # Slightly larger than preview for visible borders
        
        scaled_px = None
        if original_px and not original_px.isNull():
            scaled_px = scaled_for_screen(
                original_px, preview_w, preview_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        
        # Event filter for hover effects
        buttons = []
        
        class HoverFilter(QObject):
            def __init__(self, normal_style, hover_style):
                super().__init__()
                self.normal_style = normal_style
                self.hover_style = hover_style
            
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Type.Enter:
                    obj.setStyleSheet(self.hover_style)
                elif event.type() == QEvent.Type.Leave:
                    obj.setStyleSheet(self.normal_style)
                return super().eventFilter(obj, event)
        
        for i, pos in enumerate(focus_positions):
            btn = QPushButton()
            btn.setFixedSize(button_w, button_h)  # Slightly larger than preview for visible borders
            btn.setCheckable(True)
            btn.setChecked(pos == current_focus)
            
            # Create preview with this focus from the pre-scaled image
            if scaled_px:
                # Calculate crop offset based on focus position (same logic as _make_pixmap)
                extra_w = max(0, scaled_px.width() - preview_w)
                extra_h = max(0, scaled_px.height() - preview_h)
                
                x_off = extra_w // 2
                y_off = extra_h // 2
                
                if "left" in pos:
                    x_off = 0
                elif "right" in pos:
                    x_off = extra_w
                
                if "top" in pos:
                    y_off = 0
                elif "bottom" in pos:
                    y_off = extra_h
                
                preview = scaled_px.copy(x_off, y_off, preview_w, preview_h)
                icon = QIcon(preview)
                btn.setIcon(icon)
                # Add margin so icon doesn't fill entire button - borders will be visible
                btn.setIconSize(preview.size())
            else:
                btn.setText("🖼️")
            
            # Custom hover effect using event filter
            padding = (button_w - preview_w) // 2
            normal_style = (
                f"QPushButton{{background:{palette('bg_elevated')};border:1px solid {palette('border')};border-radius:6px;padding:{padding}px;}}"
                f"QPushButton:checked{{border:2px solid {palette('accent')};}}"
            )
            hover_style = (
                f"QPushButton{{background:{palette('bg_hover')};border:1px solid {palette('accent')};border-radius:6px;padding:{padding}px;}}"
                f"QPushButton:checked{{border:2px solid {palette('accent')};}}"
            )
            btn.setStyleSheet(normal_style)
            
            hover_filter = HoverFilter(normal_style, hover_style)
            btn.installEventFilter(hover_filter)
            
            btn.clicked.connect(lambda checked=False, p=pos: self._set_cover_focus(p, dialog))
            
            buttons.append((btn, hover_filter))
            
            row = i // 3
            col = i % 3
            grid_layout.addWidget(btn, row, col)
        
        layout.addLayout(grid_layout)

        # ── Custom framing ────────────────────────────────────────────────────
        # The grid covers "which ninth"; anything else (pan, zoom, fit mode)
        # lives in the editor below, opened in place on the same dialog.
        custom_btn = QPushButton(t("library.cover_focus_custom"))
        custom_btn.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:6px;padding:7px;font-size:12px;}}"
            f"QPushButton:hover{{border-color:{palette('accent')};}}"
        )
        _current_is_custom = str(current_focus).lower().startswith("custom:")
        if _current_is_custom:
            custom_btn.setText(f"{t('library.cover_focus_custom')} ✓")
        custom_btn.clicked.connect(lambda: self._show_cover_custom_editor(dialog, original_px))
        layout.addWidget(custom_btn)
        layout.addStretch()

        # Apply theme styling
        dialog.setStyleSheet(
            f"QDialog{{background:{palette('bg_card')};}}"
        )

        dialog.exec()

    def _show_cover_custom_editor(self, parent_dialog, original_px):
        """Free framing (pan / zoom / fill-fit-stretch) for this cover."""
        from PySide6.QtWidgets import QDialog, QPushButton
        from ui.modal_helpers import information_window_modal
        from ui.widgets.cover_editor import CoverCustomEditor

        if original_px is None or original_px.isNull():
            information_window_modal(
                self, t("library.adjust_cover_focus"), t("library.cover_no_image"))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(t("library.cover_focus_custom"))
        dlg.setStyleSheet(f"QDialog{{background:{palette('bg_card')};}}")
        vbox = QVBoxLayout(dlg)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(12)

        editor = CoverCustomEditor(
            original_px, game_name=_clean_tag_display(self._entry.name),
            focus=getattr(self._entry, "cover_focus", "center"), parent=dlg)
        vbox.addWidget(editor)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t("common.cancel"))
        cancel_btn.clicked.connect(dlg.reject)
        save_btn = QPushButton(t("common.save_changes"))
        save_btn.setObjectName("primary_btn")
        save_btn.setStyleSheet(
            f"QPushButton{{background:{palette('accent')};color:{palette('accent_text')};"
            f"border:none;border-radius:6px;padding:7px 14px;font-weight:600;}}"
            f"QPushButton:hover{{background:{palette('accent_hover')};}}"
        )
        save_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        vbox.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Same commit path as a preset click, so the card refreshes and the
            # preset dialog closes behind it.
            self._set_cover_focus(editor.focus_string(), parent_dialog)

    def _set_cover_focus(self, focus: str, dialog):
        """Set the cover focus and update the entry."""
        self._entry.cover_focus = focus
        lib = get_library()
        lib.update_game(self._entry)
        self._update_cover()
        dialog.accept()

    def set_playing(self, is_playing: bool):
        if hasattr(self, "_playing_badge"):
            self._playing_badge.setVisible(is_playing)



class GameCard(_GameItemMixin, QFrame, ThemedMixin):
    backup_requested = Signal(str)
    restore_requested= Signal(str)
    remove_requested = Signal(str)
    edit_requested   = Signal(str)
    sync_requested   = Signal(str)
    launch_requested = Signal(str)
    detail_requested = Signal(str)
    review_provisional_requested = Signal(str)
    cheats_requested = Signal(str)

    def __init__(self, entry: GameEntry, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("game_card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._entry = entry
        self._img_path = _find_game_image(entry)
        # Slideshow state
        self._all_images: list[str] = []   # populated lazily on first hover
        self._slideshow_idx: int = 0
        self._slideshow_timer = QTimer(self)
        self._slideshow_timer.setInterval(1800)
        self._slideshow_timer.timeout.connect(self._slideshow_tick)
        # Cover crossfade state (~150 ms QPainter blend between slides —
        # a single-label blend avoids z-order issues with the badge/dots)
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._fade_step)
        self._fade_from: Optional[QPixmap] = None
        self._fade_to: Optional[QPixmap] = None
        self._fade_t: float = 0.0
        self._build()

    # ── Hover slideshow ───────────────────────────────────────────────────────
    def enterEvent(self, event):
        super().enterEvent(event)
        # Keep the slideshow list in sync with the icon cache on every hover:
        # drop images deleted since the last scan, and force a full re-scan
        # when the cache folder holds images we don't know about yet (a cheap
        # cache-dir listing vs the full exe-folder walk of
        # _find_all_game_images).
        if self._all_images:
            kept = [p for p in self._all_images if Path(p).exists()]
            known = {p.lower() for p in kept}
            if (len(kept) != len(self._all_images)
                    or not _cache_image_files(self._entry).issubset(known)):
                kept = []
            self._all_images = kept
        if not self._all_images:
            self._all_images = _find_all_game_images(self._entry)
            # A re-scan can change the cover too (first image ever added for
            # this game, or the old cover deleted) — reflect it immediately.
            new_cover = self._all_images[0] if self._all_images else None
            if new_cover != self._img_path:
                self._img_path = new_cover
                self._update_cover()
        n = len(self._all_images)
        if n >= 1:
            self._slideshow_idx = 0
            self._dots_bar.setVisible(True)
            self._rebuild_dots(n)
            self._update_dot_highlight()
            if n > 1:
                self._slideshow_timer.start()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._slideshow_timer.stop()
        self._stop_cover_fade()
        self._dots_bar.setVisible(False)
        # Reset to cover image
        self._update_cover()

    # ── Slideshow dot indicators ──────────────────────────────────────────────
    #
    # Strategy: always show exactly DOTS_VISIBLE dots (7) in a sliding window
    # centred on the active image, iOS-style.  For ≤7 images we show one dot
    # per image exactly.  For >7 images the window scrolls and the two edge
    # dots are rendered at 60 % size to hint that more images exist beyond the
    # visible window.
    #
    _DOTS_VISIBLE = 7   # number of dot slots always shown

    def _rebuild_dots(self, n: int):
        """Create exactly min(n, _DOTS_VISIBLE) dot widgets."""
        while self._dots_layout.count():
            item = self._dots_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        slots = min(n, self._DOTS_VISIBLE)
        for _ in range(slots):
            dot = QWidget()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet("background:rgba(255,255,255,0.5);border-radius:3px;")
            self._dots_layout.addWidget(dot)

        self._total_images = n          # remember total for window calc
        self._dots_slots  = slots

        # Centre horizontally
        self._dots_bar.adjustSize()
        bar_w = self._dots_bar.width()
        x = max(0, (186 - bar_w) // 2)
        self._dots_bar.move(x, 106)

    def _update_dot_highlight(self):
        """Repaint the dot strip for the current _slideshow_idx.

        When total images > _DOTS_VISIBLE the window is centred on the active
        image (clamped to valid range).  Edge dots are shown more dimly when
        there are hidden images beyond them.
        """
        slots = getattr(self, '_dots_slots', 0)
        total = getattr(self, '_total_images', slots)
        if slots == 0:
            return

        idx = self._slideshow_idx

        if total <= slots:
            window_start = 0
        else:
            half = slots // 2
            window_start = max(0, min(idx - half, total - slots))

        for slot in range(slots):
            img_i = window_start + slot
            item = self._dots_layout.itemAt(slot)
            if not item:
                continue
            dot = item.widget()
            if not dot:
                continue

            is_active = (img_i == idx)
            has_more_left  = (slot == 0 and window_start > 0)
            has_more_right = (slot == slots - 1 and window_start + slots < total)
            is_edge_hint   = has_more_left or has_more_right

            if is_active:
                style = f"background:{palette('accent')};border-radius:3px;"
            elif is_edge_hint:
                style = "background:rgba(255,255,255,0.25);border-radius:3px;"
            else:
                style = "background:rgba(255,255,255,0.5);border-radius:3px;"
            dot.setStyleSheet(style)

    def _slideshow_tick(self):
        if not self._all_images:
            self._slideshow_timer.stop()
            return
        self._slideshow_idx = (self._slideshow_idx + 1) % len(self._all_images)
        path = self._all_images[self._slideshow_idx]
        focus = getattr(self._entry, 'cover_focus', 'center')
        px = _make_pixmap(path, 186, 240, focus)
        if px and not px.isNull():
            self._start_cover_fade(px)
            self._cover.setText("")
        else:
            # Image was deleted from the cache mid-slideshow — prune every now-
            # missing frame so we stop cycling empties, fall back to the cover,
            # and stop/resync the dot strip (fully re-synced on the next hover).
            self._all_images = [p for p in self._all_images if Path(p).exists()]
            self._stop_cover_fade()
            self._update_cover()
            n = len(self._all_images)
            if n <= 1:
                self._slideshow_timer.stop()
                self._dots_bar.setVisible(False)
            else:
                self._slideshow_idx %= n
                self._rebuild_dots(n)
        self._update_dot_highlight()

    # ── Cover crossfade (light ~150 ms blend between slideshow frames) ───────

    _FADE_MS = 150

    def _start_cover_fade(self, new_px: QPixmap):
        cur = self._cover.pixmap()
        if cur is None or cur.isNull():
            # Nothing to fade from (placeholder icon) — swap instantly
            self._cover.setPixmap(new_px)
            return
        self._fade_from = QPixmap(cur)
        self._fade_to = new_px
        self._fade_t = 0.0
        self._fade_timer.start()

    def _fade_step(self):
        self._fade_t += self._fade_timer.interval() / float(self._FADE_MS)
        if self._fade_t >= 1.0 or self._fade_from is None or self._fade_to is None:
            final = self._fade_to
            self._stop_cover_fade()
            if final is not None and not final.isNull():
                self._cover.setPixmap(final)
            return
        t = self._fade_t
        # The blend canvas must carry the SAME scale as the frames going into
        # it. QPixmap.size() is in real pixels while QPainter.drawPixmap and
        # QLabel both work in Qt's coordinates, so a canvas left at scale 1
        # was 2x too big on a scaled display: each frame landed in its
        # top-left quarter and the label, which does not resize a pixmap,
        # showed that quarter — the whole cover, shrunk, instead of the
        # framing render_cover had just produced. Only the fade frames were
        # affected, which is why the cover looked right until it changed.
        dpr = max(1.0, float(self._fade_to.devicePixelRatio() or 1.0))
        blended = QPixmap(self._fade_to.size())
        blended.setDevicePixelRatio(dpr)
        blended.fill(Qt.GlobalColor.transparent)
        # Explicit source/target rects rather than a point: the two frames can
        # differ in scale (one straight from the cache, one re-rendered), and
        # both have to fill the same frame.
        target = QRectF(0.0, 0.0,
                        blended.width() / dpr, blended.height() / dpr)
        p = QPainter(blended)
        # Outgoing frame opaque, incoming one faded over it. Fading BOTH (the
        # obvious 1-t / t pair) blends them onto transparency instead of onto
        # each other, so the canvas peaked at 75% alpha mid-fade and the card
        # behind it showed through.
        p.drawPixmap(target, self._fade_from, QRectF(self._fade_from.rect()))
        p.setOpacity(t)
        p.drawPixmap(target, self._fade_to, QRectF(self._fade_to.rect()))
        p.end()
        self._cover.setPixmap(blended)

    def _stop_cover_fade(self):
        if self._fade_timer.isActive():
            self._fade_timer.stop()
        self._fade_from = None
        self._fade_to = None
        self._fade_t = 0.0

    def _build(self):
        self.setFixedSize(186, 240)
        # Frame: QSS when the card has no folder colour, inline when it does.
        # NOT registered with _sty — refresh_styles() calls this directly, so
        # the objectName and the sheet always move together.
        self._apply_card_style()

        # ── Cover fills the entire card ───────────────────────────────────────
        # The card's children are styled from the theme QSS by objectName
        # rather than each getting its own stylesheet: a library page holds
        # one of each per card, and a widget carrying its own sheet costs
        # about 3x more to re-polish when the theme changes.
        self._cover = QLabel(self)
        self._cover.setObjectName("game_card_cover")
        self._cover.setFixedSize(186, 240)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_cover()

        # Playing badge (top-left overlay)
        self._playing_badge = QLabel(f"▶ {t('library.playing').upper()}", self)
        self._playing_badge.setObjectName("playing_badge")
        self._playing_badge.setVisible(False)
        self._playing_badge.move(8, 8)
        self._playing_badge.adjustSize()

        # Slideshow dot indicators (bottom-center, visible only on hover)
        self._dots_bar = QWidget(self)
        self._dots_bar.setObjectName("game_card_dots")
        self._dots_layout = QHBoxLayout(self._dots_bar)
        self._dots_layout.setContentsMargins(12, 6, 12, 6)
        self._dots_layout.setSpacing(5)
        self._dots_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dots_slots = 0
        self._total_images = 0
        self._dots_bar.setFixedHeight(20)
        self._dots_bar.setMinimumWidth(40)
        self._dots_bar.setVisible(False)
        # Position: bottom-center of the cover image (above the info panel at y=130)
        self._dots_bar.move(0, 104)   # centred above the info panel
        self._dots_bar.adjustSize()

        # ── Bottom info overlay (absolute positioned, full width) ─────────────
        # Translucent tint over the cover; the per-theme rgba lives in the QSS.
        self._bottom = QWidget(self)
        self._bottom.setObjectName("game_card_bottom")
        self._bottom.setGeometry(0, 130, 186, 110)
        bl = QVBoxLayout(self._bottom)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(2)

        # _clean_tag_display heals a name still stored with HTML entities
        # ("N&amp;R") for display, without rewriting the stored value.
        _disp_name = _clean_tag_display(self._entry.name)
        self._name_lbl = QLabel(_disp_name)
        self._name_lbl.setObjectName("game_card_name")
        self._name_lbl.setWordWrap(False)
        fm = self._name_lbl.fontMetrics()
        elided = fm.elidedText(_disp_name, Qt.TextElideMode.ElideRight, 162)
        self._name_lbl.setText(elided)
        self._name_lbl.setToolTip(_disp_name)

        # Sync-status label with a hover effect: shows when the game was last
        # synced while hovered (no popup — in-place text swap). Its palette-
        # derived text/style is (re)computed by _apply_status from _entry.
        self._status_lbl = _HoverSwapLabel()
        self._apply_status()

        # Playtime with hover effect: total normally, last session on hover
        self._playtime_lbl = _PlaytimeLabel()
        self._playtime_lbl.set_entry(self._entry)

        bl.addWidget(self._name_lbl)

        # Status row: status label + compact sync button side-by-side
        _status_row = QHBoxLayout()
        _status_row.setContentsMargins(0, 0, 0, 0)
        _status_row.setSpacing(4)
        _status_row.addWidget(self._status_lbl, 1)
        self._sync_card_btn = QPushButton(t("buttons.sync"))
        # card_flat_btn, not the shared icon_btn: no background of its own,
        # because the bottom overlay propagates its rgba fill to children and
        # painted this button as a darker rectangle.
        self._sync_card_btn.setObjectName("card_flat_btn")
        self._sync_card_btn.setFixedSize(22, 18)
        self._sync_card_btn.setToolTip(t("sync.sync_now"))
        self._sync_card_btn.clicked.connect(lambda: self.sync_requested.emit(self._entry.id))
        _status_row.addWidget(self._sync_card_btn)
        bl.addLayout(_status_row)

        bl.addWidget(self._playtime_lbl)

        # Action bar
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 4, 0, 0)
        bar.setSpacing(4)

        self._launch_btn = QPushButton(t("buttons.play"))
        # card_play_btn: the shared primary_btn look, tightened for the card.
        self._launch_btn.setObjectName("card_play_btn")
        self._launch_btn.setFixedHeight(26)
        self._launch_btn.clicked.connect(lambda: self.launch_requested.emit(self._entry.id))

        self._backup_btn = QPushButton(t("buttons.backup"))
        # Same transparency treatment as the ⟳ sync button above, one size up.
        self._backup_btn.setObjectName("card_flat_btn_lg")
        self._backup_btn.setFixedSize(26, 26)
        self._backup_btn.setToolTip(t("library.backup_now"))
        self._backup_btn.clicked.connect(lambda: self.backup_requested.emit(self._entry.id))

        more_btn = QPushButton(t("buttons.more"))
        # card_more_btn, not the shared icon_btn: it keeps a solid backdrop so
        # the small ⋯ glyph stays readable over the cover art (see the QSS).
        more_btn.setObjectName("card_more_btn")
        more_btn.setFixedSize(26, 26)
        more_btn.clicked.connect(lambda: self._show_context_menu(more_btn))

        bar.addWidget(self._launch_btn, 1)
        bar.addWidget(self._backup_btn)
        bar.addWidget(more_btn)
        bl.addLayout(bar)

    def _card_style(self) -> str:
        """Inline frame style — ONLY for a card filed under a coloured folder.

        The colour is per-card state, so that variant can't live in the
        theme QSS. Without a folder the card takes #game_card_grid instead
        and carries no stylesheet at all, which is the common case and the
        one worth keeping cheap on a theme switch.
        """
        folder_color = self._get_folder_color()
        if not folder_color:
            return ""
        return f"""
            QFrame#game_card {{
                background: {palette('bg_card')};
                border: 1px solid {palette('border')};
                border-left: 3px solid {folder_color};
                border-radius: 10px;
            }}
            QFrame#game_card:hover {{
                border-color: {palette('border_hover')};
                border-left: 3px solid {folder_color};
                background: {palette('bg_hover')};
            }}
        """

    def _apply_card_style(self):
        """Swap between the QSS-styled frame and the folder-colour variant.

        The objectName has to move too: the inline rules are written for
        #game_card, and leaving the name at #game_card_grid would let the
        plain QSS rule paint over the folder border.
        """
        sheet = self._card_style()
        self.setObjectName("game_card" if sheet else "game_card_grid")
        self.setStyleSheet(sheet)
        self.style().unpolish(self)
        self.style().polish(self)

    def _apply_status(self):
        """(Re)compute the sync-status label's text + palette-derived styles
        from the current entry. Used at build, on refresh(entry) and on a
        theme switch so the badge colour always matches state AND theme."""
        status = _display_sync_status(self._entry)
        _st_hover, _st_hover_style = _sync_hover(self._entry, "10px")
        self._status_lbl.set_texts(
            f"{STATUS_ICONS.get(status,'?')}  {t(f'library.status_{status}')}",
            f"color:{_status_color(status)};font-size:10px;font-weight:500;background:transparent;",
            _st_hover, _st_hover_style,
        )

    def refresh_styles(self):
        # Everything on the card is styled by objectName and was already
        # re-resolved by the stylesheet swap. Only the frame (when the card
        # carries a folder colour) and the state-dependent status badge are
        # recomputed. NO card is recreated.
        super().refresh_styles()
        self._apply_card_style()
        self._apply_status()
        self._playtime_lbl.refresh_styles()

    def _get_folder_color(self) -> str:
        cat = self._entry.category
        if not cat:
            return ""
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        color_key = _get_folder_color_by_path(folders, cat)
        return palette(color_key) if color_key else ""

    def _update_cover(self):
        focus = getattr(self._entry, 'cover_focus', 'center')
        px = _make_pixmap(self._img_path, 186, 240, focus)
        if px:
            self._cover.setPixmap(px)
            self._cover.setText("")
        else:
            self._cover.setText(PLACEHOLDER_ICON)

    def refresh(self, entry: GameEntry):
        self._entry = entry
        # Invalidate the cached slideshow list so images added or deleted since
        # the last scan (e.g. after editing details, or clearing the icon cache)
        # are re-picked-up on the next hover instead of cycling stale frames.
        self._all_images = []
        new_img = _find_game_image(entry)
        if new_img != self._img_path:
            self._img_path = new_img
            self._update_cover()
        _disp_name = _clean_tag_display(entry.name)
        fm = self._name_lbl.fontMetrics()
        elided = fm.elidedText(_disp_name, Qt.TextElideMode.ElideRight, 166)
        self._name_lbl.setText(elided)
        self._name_lbl.setToolTip(_disp_name)
        self._apply_status()   # self._entry already == entry (set above)
        self._playtime_lbl.set_entry(entry)
        self._apply_card_style()

    def update_locale(self):
        self._launch_btn.setText(t("buttons.play"))
        self._backup_btn.setToolTip(t("library.backup_now"))
        self._sync_card_btn.setToolTip(t("sync.sync_now"))
        self._playing_badge.setText(f"▶ {t('library.playing').upper()}")
        self._playing_badge.adjustSize()
        self.refresh(self._entry)


# ── Game Row (list view) ──────────────────────────────────────────────────────

class GameRow(_GameItemMixin, QFrame, ThemedMixin):
    backup_requested = Signal(str)
    restore_requested= Signal(str)
    remove_requested = Signal(str)
    edit_requested   = Signal(str)
    sync_requested   = Signal(str)
    launch_requested = Signal(str)
    detail_requested = Signal(str)
    review_provisional_requested = Signal(str)
    cheats_requested = Signal(str)

    def __init__(self, entry: GameEntry, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("game_card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._entry = entry
        self._img_path = _find_game_image(entry)
        self._build()

    def _build(self):
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        # Thumbnail
        # Styled from the theme QSS by objectName, like the card's children —
        # see the note in GameCard._build.
        self._thumb = QLabel()
        self._thumb.setObjectName("game_row_thumb")
        self._thumb.setFixedSize(48, 48)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        focus = getattr(self._entry, 'cover_focus', 'center')
        px = _make_pixmap(self._img_path, 48, 48, focus)
        if px:
            self._thumb.setPixmap(px)
        else:
            self._thumb.setText(PLACEHOLDER_ICON)
        row.addWidget(self._thumb)

        # Folder color dot
        self._folder_dot = QLabel("●")
        self._folder_dot.setFixedWidth(14)
        self._folder_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_folder_dot()
        row.addWidget(self._folder_dot)

        # Info
        info = QVBoxLayout()
        info.setSpacing(3)
        self._name_lbl = QLabel(_clean_tag_display(self._entry.name))
        self._name_lbl.setObjectName("game_name")

        meta = QHBoxLayout()
        meta.setSpacing(12)
        self._played_lbl = QLabel(f"{t('library.last_played')}: {_fmt_dt(self._entry.last_played)}")
        self._played_lbl.setObjectName("game_meta")
        self._synced_lbl = QLabel(f"{t('library.last_synced')}: {_fmt_dt(self._entry.last_synced)}")
        self._synced_lbl.setObjectName("game_meta")
        # Playtime with a hover effect: total normally, most recent session's
        # duration while hovered (in-place text swap, no popup). Palette-derived
        # styles are (re)computed by _apply_playtime from _entry.
        self._playtime_lbl = _HoverSwapLabel()
        self._playtime_lbl.setObjectName("game_meta")
        self._apply_playtime()
        meta.addWidget(self._played_lbl)
        meta.addWidget(self._synced_lbl)
        meta.addWidget(self._playtime_lbl)
        meta.addStretch()

        info.addWidget(self._name_lbl)
        info.addLayout(meta)
        row.addLayout(info, 1)

        # Playing badge
        self._playing_badge = QLabel(f"▶ {t('library.playing').upper()}")
        self._playing_badge.setObjectName("playing_badge")   # shared with the card view
        self._playing_badge.setVisible(False)
        row.addWidget(self._playing_badge)

        # Status (text + palette-derived colour recomputed by _apply_status)
        self._status_lbl = QLabel()
        self._status_lbl.setObjectName("_status")
        self._apply_status()
        row.addWidget(self._status_lbl)

        # Buttons — compact icon row: [▶ play] [💾 backup] [⟳ sync] [⋯ more].
        # The play button is an ICON like its neighbours (the wide "▶ Play"
        # text button belongs to the card view only).
        self._launch_btn = QPushButton("▶")
        # row_play_btn, not the shared primary_btn: the zero padding this
        # needs (and the cancelled :pressed) live with the rule in the theme.
        self._launch_btn.setObjectName("row_play_btn")
        self._launch_btn.setFixedSize(28, 28)
        self._launch_btn.setToolTip(t("buttons.play").lstrip("▶").strip())
        self._launch_btn.clicked.connect(lambda: self.launch_requested.emit(self._entry.id))

        self._backup_btn = QPushButton(t("buttons.backup"))
        self._backup_btn.setObjectName("icon_btn")
        self._backup_btn.setFixedSize(28, 28)
        self._backup_btn.setToolTip(t("library.backup_now"))
        self._backup_btn.clicked.connect(lambda: self.backup_requested.emit(self._entry.id))

        self._sync_btn = QPushButton(t("buttons.sync"))
        self._sync_btn.setObjectName("icon_btn")
        self._sync_btn.setFixedSize(28, 28)
        self._sync_btn.setToolTip(t("sync.sync_now"))
        self._sync_btn.clicked.connect(lambda: self.sync_requested.emit(self._entry.id))

        more_btn = QPushButton(t("buttons.more"))
        more_btn.setObjectName("icon_btn")
        more_btn.setFixedSize(28, 28)
        more_btn.clicked.connect(lambda: self._show_context_menu(more_btn))

        for b in (self._launch_btn, self._backup_btn, self._sync_btn, more_btn):
            row.addWidget(b)

    def _update_folder_dot(self):
        cat = self._entry.category
        if not cat:
            self._folder_dot.setVisible(False)
            return
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        color_key = _get_folder_color_by_path(folders, cat)
        if color_key:
            style = f"color:{palette(color_key)};font-size:14px;background:transparent;"
            self._folder_dot.setStyleSheet(style)
            self._folder_dot.setVisible(True)
        else:
            self._folder_dot.setVisible(False)

    def _apply_status(self):
        """(Re)compute the status label's text + palette-derived colour from
        the current entry (state + theme dependent)."""
        status = _display_sync_status(self._entry)
        self._status_lbl.setText(f"{STATUS_ICONS.get(status,'?')} {t(f'library.status_{status}')}")
        style = f"color:{_status_color(status)};font-size:11px;font-weight:600;min-width:90px;background:transparent;"
        self._status_lbl.setStyleSheet(style)

    def _apply_playtime(self):
        """(Re)compute the hover-swap playtime label from the current entry."""
        pt = self._entry.get_playtime_formatted()
        _pt_hover, _pt_hover_style = _session_hover(self._entry, "11px")
        self._playtime_lbl.set_texts(
            f"🕐 {pt}" if self._entry.playtime_seconds > 0 else "",
            f"color:{palette('text_muted')};font-size:11px;",
            _pt_hover, _pt_hover_style,
        )

    def refresh(self, entry: GameEntry):
        self._entry = entry
        new_img = _find_game_image(entry)
        if new_img != self._img_path:
            self._img_path = new_img
            focus = getattr(entry, 'cover_focus', 'center')
            px = _make_pixmap(new_img, 48, 48, focus)
            if px:
                self._thumb.setPixmap(px)
            else:
                self._thumb.setText(PLACEHOLDER_ICON)
        self._name_lbl.setText(_clean_tag_display(entry.name))
        self._played_lbl.setText(f"{t('library.last_played')}: {_fmt_dt(entry.last_played)}")
        self._synced_lbl.setText(f"{t('library.last_synced')}: {_fmt_dt(entry.last_synced)}")
        self._apply_playtime()   # self._entry already == entry (set above)
        self._apply_status()
        self._update_folder_dot()

    def refresh_styles(self):
        # Thumb, playing badge, play button, name and meta labels are all
        # styled by the global QSS via their objectName and follow the app
        # stylesheet on their own. Only the state-dependent ones are
        # recomputed here:
        super().refresh_styles()
        self._apply_status()
        self._apply_playtime()
        self._update_folder_dot()

    def update_locale(self):
        # Icon button: the glyph is locale-independent, only the tooltip moves.
        self._launch_btn.setToolTip(t("buttons.play").lstrip("▶").strip())
        self._backup_btn.setToolTip(t("library.backup_now"))
        self._sync_btn.setToolTip(t("sync.sync_now"))
        self._playing_badge.setText(f"▶ {t('library.playing').upper()}")
        self.refresh(self._entry)


# ── LibraryPage ───────────────────────────────────────────────────────────────

