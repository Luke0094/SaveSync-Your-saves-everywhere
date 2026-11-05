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


def _same_game(exe_a: str, exe_b: str) -> bool:
    """The same program - or the two halves of one Unreal Engine game (its launcher and its packaged
    binary), which are one game to the player and must be one entry in the queue."""
    if exe_a == exe_b:
        return True
    from core.exe_stems import same_unreal_install
    return same_unreal_install(exe_a, exe_b)


def pending_entries() -> list[dict]:
    """History entries still pending, pruning those whose exe has since
    been added to the library (nothing left to recover) - or whose game has: the other half of an
    Unreal game that was added by its first half is the same game, not a new one. Two entries of
    one game collapse into the newest. The pruned list is written back so the history stays clean."""
    from core.exe_stems import in_unreal_install, unreal_install_roots
    config = get_config()
    raw = [h for h in config.get("unknown_game_history", [])
           if isinstance(h, dict) and h.get("exe")]
    lib = get_library()
    roots = unreal_install_roots(lib.all_known_exe_paths())
    alive: list[dict] = []
    for h in raw:
        exe = h["exe"]
        # a program the library owns - as a game's main one or as another version on its card - is not unknown
        if lib.knows_exe(exe) or (roots and in_unreal_install(exe, roots)):
            continue
        if any(_same_game(exe, kept["exe"]) for kept in alive):
            continue
        alive.append(h)
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
            if isinstance(h, dict) and not (h.get("exe") and _same_game(h["exe"], exe_path))]
    from core.save_detector import install_folder_title
    # (the install folder's name rides along as a reference - see folder_reference)
    hist.insert(0, {"name": name, "exe": exe_path, "ts": int(_time.time()), "folder": install_folder_title(exe_path, past_containers=False)})
    config.set("unknown_game_history", hist[:_MAX_ENTRIES])
    signals.changed.emit()


def folder_reference(entry: dict) -> str:
    """The install folder's name, to show beside a queue entry's detected name - or "" when it would add nothing.

    The detected name comes from the program's file name unless that is on the list of generic ones, and a list cannot know every engine's
    player: "SomeEngine_Player.exe" would read as a game of that name. The folder it sits in usually carries the real title, so it is kept as
    a reference. Not shown when it is the name already (or inside it), and never a generic folder - ``data``, ``bin``, ``win64`` are walked past
    and a container (``steamapps``, ``Games``, ``Program Files``…) is not a title: see core.save_detector.install_folder_title."""
    try:
        exe = str(entry.get("exe") or "")
        if "folder" in entry:
            ref = str(entry.get("folder") or "")
        else:
            from core.save_detector import install_folder_title
            ref = install_folder_title(exe, past_containers=False) if exe else ""
        name = str(entry.get("name") or "")
        if not ref or not name:
            return ref if ref and not name else ""
        from core.constants import match_slug
        rs, ns = match_slug(ref), match_slug(name)
        if not rs or rs == ns or rs in ns or ns in rs:
            return ""
        return ref
    except Exception:
        return ""


def remove_entry(exe_path: str) -> None:
    """Take one app out of the queue — answered "don't show again", or added.
    The one place an entry is removed on purpose. The other half of the same Unreal game goes with it:
    it is one game, and leaving it behind is what made a game just answered be asked about again."""
    config = get_config()
    hist = [h for h in config.get("unknown_game_history", [])
            if not (isinstance(h, dict) and h.get("exe") and _same_game(h["exe"], exe_path))]
    config.set("unknown_game_history", hist)
    signals.changed.emit()
