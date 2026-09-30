"""
SaveSync - Unknown-game detections queue (persistence layer).

Every unknown-game detection is persisted in its OWN list
(config["unknown_game_history"]) — deliberately separate from the
backup/sync notification flow — so a game flagged while another program
held the screen is never lost. The queue is surfaced by the OVERLAY
itself: the top-left badge counts the pending detections and the
unknown-game notification lets the user browse the whole queue in place
with the carousel arrows (see OverlayWidget.show_unknown_queue), and the
Overview shows the same queue as a banner while nothing is being played (see
ui.widgets.unknown_banner). The old dedicated "detected games (not in
library)" panel is gone. Entries whose executable has since been added to the
library are pruned automatically on refresh; suppressing an app ("don't show
again") removes its entry.

It is ONE queue with several views: every change goes through this module and
is announced on ``signals.changed``, and each view re-reads the list from
there — nothing keeps a private copy that an answer given elsewhere would
leave stale.
"""
import logging

from PySide6.QtCore import QObject, Signal

from core.config_manager import get_config
from core.library import get_library

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 50


class _QueueSignals(QObject):
    """One notification for the whole queue. Every view of it — the overlay's
    carousel and badge, the Overview's banner — listens here and re-reads the
    list, so a detection, an add or a "don't show again" made in one place
    is never still showing in another."""
    changed = Signal()


signals = _QueueSignals()


def feature_enabled() -> bool:
    """The unknown-process feature is on. With it off nothing is recorded and
    no view offers the queue."""
    return bool(get_config().get("show_overlay_on_unknown", True))


def visible_entries() -> list[dict]:
    """The queue as a view should show it: nothing at all while the feature is
    off, the pending entries otherwise."""
    return pending_entries() if feature_enabled() else []


def pending_entries() -> list[dict]:
    """History entries still pending, pruning those whose exe has since
    been added to the library (nothing left to recover). The pruned list
    is written back so the history stays clean."""
    config = get_config()
    raw = [h for h in config.get("unknown_game_history", [])
           if isinstance(h, dict) and h.get("exe")]
    lib = get_library()
    alive = [h for h in raw if lib.get_by_exe(h["exe"]) is None]
    if len(alive) != len(raw):
        config.set("unknown_game_history", alive)
        signals.changed.emit()      # an add elsewhere took entries out
    return alive


def pending_unknown_count() -> int:
    """How many unknown-game detections are still pending — drives the
    overlay's top-left badge and the hotkey routing (queue before manual
    overlay while non-empty)."""
    return len(pending_entries())


def record_unknown_game(name: str, exe_path: str):
    """Persist a detection in the history (newest first, deduped by exe,
    capped). Called by the main window on every unknown-game signal —
    including when the live overlay notification is disabled — so the
    history is complete regardless of what was shown on screen."""
    import time as _time
    config = get_config()
    hist = [h for h in config.get("unknown_game_history", [])
            if isinstance(h, dict) and h.get("exe") != exe_path]
    hist.insert(0, {"name": name, "exe": exe_path, "ts": int(_time.time())})
    config.set("unknown_game_history", hist[:_MAX_ENTRIES])
    signals.changed.emit()


def remove_entry(exe_path: str) -> None:
    """Take one app out of the queue — answered "don't show again", or added.
    The one place an entry is removed on purpose."""
    config = get_config()
    hist = [h for h in config.get("unknown_game_history", [])
            if not (isinstance(h, dict) and h.get("exe") == exe_path)]
    config.set("unknown_game_history", hist)
    signals.changed.emit()
