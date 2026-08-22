"""How many items a paginated list shows, per list, chosen by the user.

Every long list in the app (library cards, backup titles, the save editor's
two pickers) used a hard-coded page size. This makes it a per-list setting
with the same three presets everywhere plus a custom value, and — because a
custom value is the one way a user can ask for more work than their machine
can do — a guard that puts a list back to the default when rendering it took
the app down.

The guard is deliberately crude: the scope is written to a tiny sidecar
BEFORE a risky render and removed after it. A scope still marked at startup
means the render never returned, so its size is not trusted again.
Only sizes above the largest preset are guarded — the presets cannot be the
cause. The sidecar avoids rewriting the whole config.json twice per oversized
page rebuild (previously an immediate config.save() on enter and exit).
"""

import json
import logging
from contextlib import contextmanager

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QComboBox

from core.config_manager import get_config
from core.constants import USER_DATA_DIR
from i18n import t
from ui.helpers import scaled

# Closed width: enough for a 2-digit size + arrow. The popup is sized
# separately so "Personalizzato…" / "Custom…" is never clipped there.
# Global QComboBox QSS uses min-width:120px — overridden via #page_size_combo.
_COMBO_WIDTH = 58

logger = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_PRESETS = (10, 20, 50)
# A custom size is the user's call, but not without an end: past this the
# render is certain to hang rather than merely be slow.
PAGE_SIZE_MAX = 500

_SIZES_KEY = "page_sizes"
_GUARD_KEY = "page_size_render_guard"  # legacy key inside config.json
_GUARD_FILE = USER_DATA_DIR / "page_size_render_guard.json"

# Every scope that has a selector, so recovery can name them.
SCOPE_LIBRARY = "library"
SCOPE_BACKUPS = "backups"
SCOPE_CHEATS_GAMES = "cheats_games"
SCOPE_CHEATS_SAVES = "cheats_saves"
SCOPE_REVIEWS = "reviews"
SCOPE_MANUAL_PATHS = "manual_paths"

# Per-scope ceilings and preset lists. The generic maximum is NOT offered as
# a preset — that is what put "500" in every dropdown. Reviews are denser
# than a row of cards, so they get their own smaller ladder.
SCOPE_MAXIMUM = {
    SCOPE_REVIEWS: 50,
    # A manual-path row is five widgets tall and carries two editable fields,
    # so it costs far more than a card to build, lay out and repaint. Several
    # hundred of them at once is what made that dialog crawl, drop rows out
    # of the paint and freeze on a window resize; the ceiling is what stops
    # the size control offering a number that brings all of it back.
    SCOPE_MANUAL_PATHS: 100,
}
SCOPE_PRESETS = {
    SCOPE_REVIEWS: (5, 10, 20),
    SCOPE_MANUAL_PATHS: (25, 50, 100),
}
SCOPE_DEFAULT = {
    SCOPE_REVIEWS: 10,
    SCOPE_MANUAL_PATHS: 25,
}


def scope_maximum(scope: str) -> int:
    """The largest page size *scope* allows."""
    return SCOPE_MAXIMUM.get(scope, PAGE_SIZE_MAX)


def scope_presets(scope: str) -> tuple:
    """The presets offered for *scope*, never above its ceiling."""
    ceiling = scope_maximum(scope)
    raw = SCOPE_PRESETS.get(scope, PAGE_SIZE_PRESETS)
    presets = tuple(p for p in raw if p <= ceiling)
    return presets or (min(raw[0], ceiling),)


def page_size(scope: str) -> int:
    """The page size for *scope*, always a usable number."""
    ceiling = scope_maximum(scope)
    default = min(SCOPE_DEFAULT.get(scope, PAGE_SIZE_DEFAULT), ceiling)
    raw = (get_config().get(_SIZES_KEY, {}) or {}).get(scope)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, ceiling)


def set_page_size(scope: str, value: int) -> int:
    """Store the page size for *scope*; returns what was actually stored."""
    value = max(1, min(int(value), scope_maximum(scope)))
    sizes = dict(get_config().get(_SIZES_KEY, {}) or {})
    sizes[scope] = value
    get_config().set(_SIZES_KEY, sizes)
    return value


def is_risky(value: int) -> bool:
    """True for a size big enough to be worth guarding a render with."""
    return value > max(PAGE_SIZE_PRESETS)


def _read_guard_file() -> dict:
    try:
        if _GUARD_FILE.exists():
            with open(_GUARD_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out = {}
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
                return out
    except Exception as e:
        logger.debug(f"Could not read page-size guard: {e}")
    return {}


def _write_guard_file(data: dict) -> None:
    try:
        _GUARD_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GUARD_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        from core import atomic_replace
        atomic_replace(tmp, _GUARD_FILE)
    except Exception as e:
        logger.warning(f"Could not write page-size guard: {e}")


def _clear_guard_scope(scope: str) -> None:
    data = _read_guard_file()
    if scope not in data:
        return
    data.pop(scope, None)
    if not data:
        try:
            _GUARD_FILE.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"Could not remove page-size guard: {e}")
        return
    _write_guard_file(data)


@contextmanager
def guarded_render(scope: str):
    """Mark a risky render in progress, so a crash inside it is survivable.

    A no-op for a preset size, which keeps the common path free of disk I/O.
    Uses a tiny sidecar file — not config.json — so a custom page size does
    not flush the whole settings document twice per rebuild.
    """
    size = page_size(scope)
    if not is_risky(size):
        yield
        return
    data = _read_guard_file()
    data[scope] = size
    _write_guard_file(data)
    try:
        yield
    finally:
        _clear_guard_scope(scope)


def recover_page_sizes() -> dict:
    """Reset any page size whose render never finished. Call once at startup.

    Returns {scope: size_that_failed} for what was reset, so the caller can
    say something about it. Also migrates a legacy guard left in config.json.
    """
    config = get_config()
    guard = _read_guard_file()
    legacy = dict(config.get(_GUARD_KEY, {}) or {})
    if legacy:
        for scope, size in legacy.items():
            try:
                guard[str(scope)] = int(size)
            except (TypeError, ValueError):
                pass
        config.set(_GUARD_KEY, {})
    if not guard:
        return {}
    sizes = dict(config.get(_SIZES_KEY, {}) or {})
    for scope in guard:
        sizes[scope] = PAGE_SIZE_DEFAULT
    config.set(_SIZES_KEY, sizes)
    config.save()
    try:
        _GUARD_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    logger.warning(
        "Page size reset to %d for %s: the previous render did not finish "
        "(sizes were %s)", PAGE_SIZE_DEFAULT, ", ".join(guard), guard)
    return guard


class PageSizeCombo(QComboBox):
    """Presets as bare numbers plus a "custom…" entry, bound to one scope.

    *on_change* is called with the new size after it has been stored, and is
    where the page rebuilds itself (and resets to page 1 — a size change moves
    every item to a different page, so keeping the number would land the user
    somewhere unrelated, or past the end).
    """

    def __init__(self, scope: str, on_change, parent=None):
        super().__init__(parent)
        self._scope = scope
        self._on_change = on_change
        self.setObjectName("page_size_combo")
        self.setFixedHeight(scaled(26, self))
        self.setFixedWidth(_COMBO_WIDTH)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(2)
        self.setToolTip(t("common.per_page_tooltip"))
        self._reload_items()
        self.activated.connect(self._on_activated)

    def _reload_items(self):
        current = page_size(self._scope)
        presets = scope_presets(self._scope)
        self.blockSignals(True)
        self.clear()
        for preset in presets:
            self.addItem(str(preset), preset)
        if current not in presets:
            # The active custom value needs a slot of its own, or reopening
            # the page would show a preset that isn't what is being used.
            self.addItem(str(current), current)
        self.addItem(t("common.per_page_custom"), "custom")
        idx = self.findData(current)
        self.setCurrentIndex(idx if idx >= 0 else 0)
        self.blockSignals(False)
        self._fit_popup()

    def _fit_popup(self):
        """Popup wide enough for labels and tall enough for every item.

        Reviews offer four rows (5 / 10 / 20 / Custom…): without an explicit
        visible-item count the view clipped to ~3 and hid the rest behind a
        scrollbar.
        """
        fm = QFontMetrics(self.font())
        widest = max(
            (fm.horizontalAdvance(self.itemText(i))
             for i in range(self.count())),
            default=0)
        view = self.view()
        view.setMinimumWidth(max(_COMBO_WIDTH, widest + 28))
        n = max(1, self.count())
        self.setMaxVisibleItems(n)
        row_h = view.sizeHintForRow(0)
        if row_h <= 0:
            row_h = fm.height() + 10
        # Frame + a little air so the last row is not clipped by the border.
        view.setMinimumHeight(row_h * n + 6)

    def update_locale(self):
        """Re-translate the "custom…" entry and the tooltip.

        The presets are bare numbers, but the last item and the tooltip are
        words: without this they keep the language they were built in, which
        is what the dropdown showed after a live language switch.
        """
        self.setToolTip(t("common.per_page_tooltip"))
        self._reload_items()

    def _on_activated(self, index: int):
        data = self.itemData(index)
        if data == "custom":
            value = self._ask_custom()
            if value is None:
                self._reload_items()      # cancelled — put the shown value back
                return
        else:
            value = int(data)
        stored = set_page_size(self._scope, value)
        self._reload_items()
        logger.info("Page size for %s set to %d", self._scope, stored)
        if callable(self._on_change):
            self._on_change(stored)

    def _ask_custom(self):
        # Not QInputDialog.getInt: the static helper leaves Qt's own English
        # OK/Cancel in place and lays the explanation out on a single line.
        from ui.modal_helpers import input_int_window_modal
        ceiling = scope_maximum(self._scope)
        # The warning about slow renders only makes sense where a size big
        # enough to cause one can be asked for; a capped list cannot.
        prompt = ("common.per_page_custom_prompt"
                  if ceiling > max(PAGE_SIZE_PRESETS)
                  else "common.per_page_custom_prompt_capped")
        value, ok = input_int_window_modal(
            self, t("common.per_page_custom_title"),
            t(prompt, max=ceiling),
            page_size(self._scope), 1, ceiling, 5)
        return value if ok else None
