"""SaveSync — merging one library card into another.

The card whose edit dialog is open survives; the card picked from the list is absorbed and then
deleted. It is the opposite of splitting a version off into a card of its own, and like the split it
is staged in the dialog and only done when the dialog is saved.

What moves across, and what stays:

- programs: the absorbed card's executable and all its versions become versions of the surviving one
  (the same rule as anywhere else: the wrapper of an Unreal game is not a version);
- save folders: joined. A folder the absorbed card had not confirmed arrives UNTICKED on a card that
  was confirmed, so a confirmed card never starts backing up a folder nobody has looked at;
- backups: re-filed under the surviving card (adopt_backups), zips included, so the history is not left
  behind on a card that no longer exists. They move FIRST: if some cannot, the absorbed card is kept
  as it was and nothing else is stored;
- details: tags, languages and reviews are joined; every other detail (description, developer, year,
  links, category, engine, launcher id, cover) is taken from the absorbed card only where the surviving
  one has none;
- names and folders: the absorbed card's names and backup folders are put on the surviving card's
  history, and its id is recorded (cloud_metadata["merged_from"]), so its old cloud folder is recognised
  as this game's;
- numbers: playtime is added up, the last played / last backed up dates are the later of the two,
  the added date the earlier;
- settings kept per game in the configuration (ignored paths, unticked files, confirmed processes...) are
  re-keyed to the surviving card.

The cloud follows with the surviving card's next sync (sync.SyncWorker._migrate_remote_folders): what the
absorbed card had up there is copied under the surviving card's folder, the index is published, a fresh
listing has to confirm it, and only then does the old folder lose it - and go, once nothing is left in it.
What it cannot vouch for (another PC's backups in that folder) stays, and so does the folder.
"""
import copy
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Settings kept per game id in the configuration, as {game id: value}. Values are joined, the surviving
# card's first.
_STORES_BY_GAME = (
    "auto_scan_deleted_paths",
    "auto_scan_deselected_paths",
    "auto_scan_excluded_files",
    "suppressed_ingame_notifs",
    "scan_auto_accept_games",
    "pins_recent",
    "pins_open",
)


# ── the fields ───────────────────────────────────────────────────────────────

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path or "")))


def _later(a, b):
    """The later of two ISO dates, either of which may be missing."""
    if not a or not b:
        return a or b
    try:
        from datetime import datetime, timezone

        def parse(s):
            d = datetime.fromisoformat(str(s))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return a if parse(a) >= parse(b) else b
    except (TypeError, ValueError):
        return max(str(a), str(b))


def _earlier(a, b):
    if not a or not b:
        return a or b
    return b if _later(a, b) == a else a


def merge_entry_into(keep, absorb, icon_path: str = "") -> dict:
    """Join *absorb* into *keep*, in place. Returns what was added, for the log.

    *icon_path*: the cover of *absorb* as it now sits in *keep*'s own folder, used only if *keep* has
    none (the caller copies the file; this only records it)."""
    from core.library import (merge_name_histories, normalize_languages, review_identity,
                              tag_merge_key)
    added = {"exe_paths": 0, "save_paths": 0, "save_paths_unticked": 0}

    # programs
    if absorb.exe_path and not keep.exe_path:
        keep.exe_path = absorb.exe_path
        keep.record_exe_hints(keep.exe_path)
    before = {str(p).casefold() for p, _l in keep.all_exe_paths()}
    for path, label in absorb.all_exe_paths():
        keep.record_exe_version(path, label)
    added["exe_paths"] = len({str(p).casefold() for p, _l in keep.all_exe_paths()} - before)

    # save folders
    had_paths = bool(keep.save_paths)
    have = {_norm(p) for p in keep.save_paths}
    keep_confirmed = bool(keep.save_paths_confirmed) and not keep.requires_confirmation
    absorb_confirmed = bool(absorb.save_paths_confirmed) and not absorb.requires_confirmation
    keep.save_paths = list(keep.save_paths or [])
    keep.excluded_save_paths = list(keep.excluded_save_paths or [])
    excluded_have = {_norm(p) for p in keep.excluded_save_paths}
    absorb_excluded = {_norm(p) for p in (absorb.excluded_save_paths or [])}
    for p in absorb.save_paths or []:
        if _norm(p) in have:
            continue
        keep.save_paths.append(p)
        have.add(_norm(p))
        added["save_paths"] += 1
        # unticked when it was, or when it was never confirmed and the card taking it has been
        unticked = _norm(p) in absorb_excluded or (keep_confirmed and had_paths and not absorb_confirmed)
        if unticked and _norm(p) not in excluded_have:
            keep.excluded_save_paths.append(p)
            excluded_have.add(_norm(p))
            added["save_paths_unticked"] += 1
    if not had_paths and keep.save_paths:
        # nothing of its own: the confirmation state of what came in comes in with it
        keep.save_paths_confirmed = absorb.save_paths_confirmed
        keep.requires_confirmation = absorb.requires_confirmation
        if not keep.detection_method:
            keep.detection_method = absorb.detection_method
    chains = dict(keep.save_path_chains or {})
    known = {_norm(p) for p in chains}
    for p, chain in (absorb.save_path_chains or {}).items():
        if _norm(p) not in known:
            chains[p] = chain
            known.add(_norm(p))
    keep.save_path_chains = chains
    for attr in ("save_chain", "pending_save_chain", "pending_save_anchor"):
        if not getattr(keep, attr, "") and getattr(absorb, attr, ""):
            setattr(keep, attr, getattr(absorb, attr))

    # names and backup folders
    keep.name_history = merge_name_histories(
        keep.name_history, absorb.name_history, [absorb.name], current=keep.name)
    for hint in absorb.name_hints or []:
        keep.record_name_hint(hint)
    folders = list(keep.folder_history or [])
    for f in list(absorb.folder_history or []) + [absorb.computed_folder_name]:
        if f and f != keep.computed_folder_name and f not in folders:
            folders.append(f)
    keep.folder_history = folders

    # details: joined, or filled where empty
    tags = list(keep.tags or [])
    seen = {tag_merge_key(x) for x in tags}
    for x in absorb.tags or []:
        if tag_merge_key(x) not in seen:
            seen.add(tag_merge_key(x))
            tags.append(x)
    keep.tags = tags
    keep.languages = normalize_languages(list(keep.languages or []) + list(absorb.languages or []))
    reviews = list(keep.reviews or [])
    ids = {review_identity(r) for r in reviews}
    for r in absorb.reviews or []:
        if review_identity(r) not in ids:
            ids.add(review_identity(r))
            reviews.append(r)
    keep.reviews = sorted(reviews, key=lambda r: str((r or {}).get("at") or ""), reverse=True)
    for attr in ("description", "developer", "release_year", "store_url", "category", "engine", "appid"):
        if not getattr(keep, attr, "") and getattr(absorb, attr, ""):
            setattr(keep, attr, getattr(absorb, attr))
            if attr == "developer" and not keep.info_source:
                keep.info_source = absorb.info_source
    if icon_path and not keep.icon_path:
        keep.icon_path = icon_path
        keep.cover_focus = absorb.cover_focus or keep.cover_focus

    # the cards this one has absorbed: the provider matches a zip that only it holds by the id in its name
    meta = dict(keep.cloud_metadata or {})
    merged = list(meta.get("merged_from") or [])
    for gid in [absorb.id] + list((absorb.cloud_metadata or {}).get("merged_from") or []):
        if gid and gid != keep.id and gid not in merged:
            merged.append(gid)
    meta["merged_from"] = merged
    # The folders either card was forked away from (core.config_sync.fork_card) are shared with another PC's
    # cards and never a past home of this one: the merged card must not treat them as old names either.
    forked = list(meta.get("forked_from") or [])
    for f in list((absorb.cloud_metadata or {}).get("forked_from") or []):
        if f and f not in forked:
            forked.append(f)
    if forked:
        meta["forked_from"] = forked
    keep.cloud_metadata = meta

    # numbers
    keep.playtime_seconds = int(keep.playtime_seconds or 0) + int(absorb.playtime_seconds or 0)
    if absorb.last_played and _later(keep.last_played, absorb.last_played) == absorb.last_played != keep.last_played:
        keep.last_session_seconds = absorb.last_session_seconds
    keep.last_played = _later(keep.last_played, absorb.last_played)
    keep.last_backed_up = _later(keep.last_backed_up, absorb.last_backed_up)
    keep.date_added = _earlier(keep.date_added, absorb.date_added)
    return added


# ── the settings kept per game ───────────────────────────────────────────────

def _join(kept, moved):
    """Two values of one per-game setting as one: lists and dictionaries joined, anything else the
    surviving card's own unless it has none."""
    if isinstance(kept, list) and isinstance(moved, list):
        return kept + [x for x in moved if x not in kept]
    if isinstance(kept, dict) and isinstance(moved, dict):
        out = dict(kept)
        for k, v in moved.items():
            out[k] = _join(out[k], v) if k in out else v
        return out
    return kept if kept not in (None, "", [], {}) else moved


def remap_game_id_stores(absorb_id: str, keep_id: str, config=None) -> list:
    """Re-key the per-game settings of *absorb_id* onto *keep_id*. Returns the settings changed."""
    if config is None:
        from core.config_manager import get_config
        config = get_config()
    changed = []
    for key in _STORES_BY_GAME:
        store = config.get(key, {})
        if not isinstance(store, dict) or absorb_id not in store:
            continue
        new = copy.deepcopy(store)
        moved = new.pop(absorb_id)
        new[keep_id] = _join(new[keep_id], moved) if keep_id in new else moved
        config.set(key, new)
        changed.append(key)
    confirmed = config.get("confirmed_process_matches", {})
    if isinstance(confirmed, dict) and absorb_id in confirmed.values():
        config.set("confirmed_process_matches",
                   {proc: (keep_id if gid == absorb_id else gid) for proc, gid in confirmed.items()})
        changed.append("confirmed_process_matches")
    rejected = config.get("rejected_process_matches", {})
    if isinstance(rejected, dict) and any(absorb_id in (v or []) for v in rejected.values()):
        config.set("rejected_process_matches",
                   {proc: [g for g in ids if g != absorb_id] for proc, ids in rejected.items()
                    if [g for g in ids if g != absorb_id]})
        changed.append("rejected_process_matches")
    return changed


# ── the cover ────────────────────────────────────────────────────────────────

def _icon_dir(entry) -> Path:
    from core.constants import USER_DATA_DIR, get_install_folder_name
    return USER_DATA_DIR / "icons" / get_install_folder_name(
        entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)


def _carry_icon(keep, absorb) -> str:
    """The absorbed card's cover copied into the surviving card's own folder, when it has none.
    Empty string when there is nothing to carry."""
    try:
        if keep.icon_path and Path(keep.icon_path).exists():
            return ""
        src = Path(absorb.icon_path or "")
        if not absorb.icon_path or not src.is_file():
            return ""
        dest_dir = _icon_dir(keep)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists() and dest.resolve() != src.resolve():
            dest = dest_dir / f"merged_{src.name}"
        if not dest.exists():
            shutil.copy2(src, dest)
        return str(dest)
    except OSError as e:
        logger.warning(f"Could not carry the cover across: {e}")
        return ""


def _drop_icon_dir(absorb, keep) -> None:
    """The pictures folder of the absorbed card goes with it, unless the surviving card uses it."""
    try:
        gone = _icon_dir(absorb)
        if not gone.is_dir():
            return
        mine = _icon_dir(keep)
        icon = Path(keep.icon_path) if keep.icon_path else None
        if gone.resolve() == mine.resolve() or (icon is not None and gone.resolve() in icon.resolve().parents):
            return
        shutil.rmtree(gone)
    except OSError as e:
        logger.warning(f"Could not remove the pictures of the absorbed card: {e}")


# ── what the dialog needs ────────────────────────────────────────────────────

def playing_ids() -> set:
    """The cards a game is running for right now."""
    try:
        from core.monitor import get_monitor
        return {e.id for e in get_monitor().currently_playing()}
    except Exception:
        return set()


def describe_card(game_id: str) -> dict:
    """{exes, paths, backups} of a card, for the list it is picked from."""
    from core.backup import get_backup_manager
    from core.library import get_library
    g = get_library().get_by_id(game_id)
    if g is None:
        return {"exes": 0, "paths": 0, "backups": 0}
    try:
        backups = len(get_backup_manager().get_backups_for_game(game_id))
    except Exception:
        backups = 0
    return {"exes": len(g.all_exe_paths()), "paths": len(g.save_paths or []), "backups": backups}


def _was_in_cloud(entries) -> bool:
    """Whether any of these backups has been through a provider."""
    if any((getattr(b, "cloud_metadata", None) or {}).get("synced_to") for b in entries):
        return True
    try:
        from sync import get_orchestrator
        from sync.base import has_published_to
        return any(has_published_to(pid, entries) for pid in get_orchestrator().get_connected_provider_ids())
    except Exception:
        return False


@dataclass
class MergeOutcome:
    merged: bool = False        # the absorbed card is gone and everything of it is on the surviving one
    in_cloud: bool = False      # either card had been synced: the surviving one has to be synced now
    reason: str = ""            # "", "gone", "same", "playing", "backups_left"
    backups_moved: int = 0
    added: dict = field(default_factory=dict)
    keep: object = None         # the surviving card as stored now


def merge_cards(keep_id: str, absorb_id: str) -> MergeOutcome:
    """Absorb the card *absorb_id* into the card *keep_id*. Both are read as they are NOW, not as
    some dialog saw them. Order matters, so that a failure part-way leaves nothing stranded and a
    second try adds nothing up twice: the backups move onto the surviving card first; only when none
    is left on the absorbed one is the surviving card stored with what it takes from it, then the
    settings are re-keyed, and last the absorbed card goes."""
    from core.backup import get_backup_manager
    from core.library import get_library
    lib = get_library()
    keep = lib.get_by_id(keep_id)
    absorb = lib.get_by_id(absorb_id)
    if keep is None or absorb is None:
        return MergeOutcome(reason="gone", keep=keep)
    if absorb.id == keep.id:
        return MergeOutcome(reason="same", keep=keep)
    busy = playing_ids()
    if keep.id in busy or absorb.id in busy:
        return MergeOutcome(reason="playing", keep=keep)

    icon = _carry_icon(keep, absorb)
    added = merge_entry_into(keep, absorb, icon_path=icon)
    bm = get_backup_manager()
    in_cloud = _was_in_cloud(bm.get_backups_for_game(absorb.id) + bm.get_backups_for_game(keep.id))
    if in_cloud:
        # until the card completes a sync: the provider's zips come down first (SyncWorker._download_before_backup)
        keep.cloud_metadata = {**(keep.cloud_metadata or {}), "merge_sync_pending": True}
    had_backups = bool(bm.get_backups_for_game(absorb.id))
    moved = bm.adopt_backups(absorb.id, keep.id, keep.name, to_exe_path=keep.exe_path or "",
                             to_folder_name=keep.computed_folder_name or "")
    if bm.get_backups_for_game(absorb.id):
        # Nothing of the card has been stored on the surviving one yet, so trying again adds up nothing twice.
        logger.warning(f"Not every backup of '{absorb.name}' could be moved onto '{keep.name}': "
                       "its card is kept so nothing is stranded")
        return MergeOutcome(reason="backups_left", backups_moved=moved, added=added, keep=lib.get_by_id(keep.id))
    if (had_backups or in_cloud) and keep.sync_status == "synced":
        keep.sync_status = "pending"
    lib.update_game(keep)
    remap_game_id_stores(absorb.id, keep.id)
    lib.remove_game(absorb.id)
    _drop_icon_dir(absorb, keep)
    logger.info(f"Merged '{absorb.name}' into '{keep.name}': {added}, {moved} backup(s) moved")
    return MergeOutcome(merged=True, in_cloud=in_cloud, backups_moved=moved, added=added, keep=keep)
