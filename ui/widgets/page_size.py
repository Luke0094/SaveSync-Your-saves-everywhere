"""How many items a paginated list shows, per list, chosen by the user.

Every long list in the app (library cards, backup titles, the save editor's
two pickers) used a hard-coded page size. This makes it a per-list setting
with the same three presets everywhere plus a custom value, and — because a
custom value is the one way a user can ask for more work than their machine
can do — a guard that puts a list back to the default when rendering it took
the app down.

The guard is deliberately crude: the scope is written to disk BEFORE a risky
render and removed after it, both flushed synchronously. A scope still marked
at startup means the render never returned, so its size is not trusted again.
Only sizes above the largest preset are guarded — the presets cannot be the
cause, and an fsync per page change is not worth paying for them.
"""

import logging
from contextlib import contextmanager

from PySide6.QtWidgets import QComboBox, QInputDialog

from core.config_manager import get_config
from i18n import t

logger = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_PRESETS = (10, 20, 50)
# A custom size is the user's call, but not without an end: past this the
# render is certain to hang rather than merely be slow.
PAGE_SIZE_MAX = 500

_SIZES_KEY = "page_sizes"
_GUARD_KEY = "page_size_render_guard"

# Every scope that has a selector, so recovery can name them.
SCOPE_LIBRARY = "library"
SCOPE_BACKUPS = "backups"
SCOPE_CHEATS_GAMES = "cheats_games"
SCOPE_CHEATS_SAVES = "cheats_saves"


def page_size(scope: str) -> int:
    """The page size for *scope*, always a usable number."""
    raw = (get_config().get(_SIZES_KEY, {}) or {}).get(scope)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return PAGE_SIZE_DEFAULT
    if value < 1:
        return PAGE_SIZE_DEFAULT
    return min(value, PAGE_SIZE_MAX)


def set_page_size(scope: str, value: int) -> int:
    """Store the page size for *scope*; returns what was actually stored."""
    value = max(1, min(int(value), PAGE_SIZE_MAX))
    sizes = dict(get_config().get(_SIZES_KEY, {}) or {})
    sizes[scope] = value
    get_config().set(_SIZES_KEY, sizes)
    return value


def is_risky(value: int) -> bool:
    """True for a size big enough to be worth guarding a render with."""
    return value > max(PAGE_SIZE_PRESETS)


@contextmanager
def guarded_render(scope: str):
    """Mark a risky render in progress, so a crash inside it is survivable.

    A no-op for a preset size, which keeps the common path free of disk I/O.
    """
    size = page_size(scope)
    if not is_risky(size):
        yield
        return
    config = get_config()
    guard = dict(config.get(_GUARD_KEY, {}) or {})
    guard[scope] = size
    config.set(_GUARD_KEY, guard)
    config.save()          # must be on disk BEFORE the render is attempted
    try:
        yield
    finally:
        guard = dict(config.get(_GUARD_KEY, {}) or {})
        if guard.pop(scope, None) is not None:
            config.set(_GUARD_KEY, guard)
            config.save()


def recover_page_sizes() -> dict:
    """Reset any page size whose render never finished. Call once at startup.

    Returns {scope: size_that_failed} for what was reset, so the caller can
    say something about it.
    """
    config = get_config()
    guard = dict(config.get(_GUARD_KEY, {}) or {})
    if not guard:
        return {}
    sizes = dict(config.get(_SIZES_KEY, {}) or {})
    for scope in guard:
        sizes[scope] = PAGE_SIZE_DEFAULT
    config.set(_SIZES_KEY, sizes)
    config.set(_GUARD_KEY, {})
    config.save()
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
        self.setFixedHeight(26)
        # Wide enough for "Personalizzato…" / "Custom…" plus the drop arrow;
        # AdjustToContents alone still clipped the label on Windows.
        self.setMinimumWidth(130)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip(t("common.per_page_tooltip"))
        self._reload_items()
        self.activated.connect(self._on_activated)

    def _reload_items(self):
        current = page_size(self._scope)
        self.blockSignals(True)
        self.clear()
        for preset in PAGE_SIZE_PRESETS:
            self.addItem(str(preset), preset)
        if current not in PAGE_SIZE_PRESETS:
            # The active custom value needs a slot of its own, or reopening
            # the page would show a preset that isn't what is being used.
            self.addItem(str(current), current)
        self.addItem(t("common.per_page_custom"), "custom")
        idx = self.findData(current)
        self.setCurrentIndex(idx if idx >= 0 else PAGE_SIZE_PRESETS.index(
            PAGE_SIZE_DEFAULT))
        self.blockSignals(False)

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
        value, ok = QInputDialog.getInt(
            self, t("common.per_page_custom_title"),
            t("common.per_page_custom_prompt", max=PAGE_SIZE_MAX),
            page_size(self._scope), 1, PAGE_SIZE_MAX, 5)
        return value if ok else None
