"""SaveSync — the library configuration, kept in step between PCs.

One encrypted file on the sync provider (`SaveSync/savesync_config`) holds the library and the settings. It
is rewritten whole on every push, so two PCs that push without reading first erase each other's changes -
and a card merged on one PC (core.merge_cards) that the other PC does not know about would send its backups
into a folder that no longer exists. Three rules keep that from happening:

- A push goes out when the library or the settings CHANGED (a signature that ignores what changes all the
  time: playtime, sync stamps, window geometry), not on a timer. That is what the switch "export the
  configuration automatically" means: on, every change is pushed; off, nothing is pushed on a change and the
  configuration is exchanged only when a sync runs into a conflict (the answer to the question below).
- Before any push the file on the provider is looked at (`preflight`). If it is not the one this PC last
  wrote or read, someone else pushed since: nothing is uploaded until the player has decided.
- Before a game is synced, the same look says whether a merge made on the other PC concerns it
  (`hold_for_game`): importing it brings the merge here, so the backups of both titles end up under one
  folder instead of being cut off.

What travels is the LIBRARY and the player's RULES about games and processes - save-folder hints, the processes and
overlays to ignore, which process is which game, what was deleted or unticked in a game's folders, which games
never ask to download saves, the library's folders. They matter for a merge (a rule about a card must follow the
card), so they are joined with this PC's own, never replacing them. The PREFERENCES of a PC - theme, language,
hotkeys, ports, folders, polling - are its own: they are in the pack, but are applied only when the player imports
them (Settings -> Transfer).

The player decides with one of: import (the other PC's library is added, the merges are made here too),
keep this PC's library apart (its configuration goes to a file of its own and the titles in conflict get
cloud folders of their own, so nothing is mixed), or not now.
"""
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

CLOUD_CONFIG_BASE = "SaveSync/savesync_config"

# What a game's entry changes by itself, session after session: not a reason to push the configuration.
_VOLATILE_GAME_FIELDS = frozenset({
    "playtime_seconds", "last_session_seconds", "last_played", "last_synced", "last_backed_up",
    "sync_status", "cloud_metadata",
})
# Settings that follow the player's windows around.
_NOISY_SETTING_MARKS = ("geometr", "splitter", "page_size", "window_size", "scroll", "_last", "last_")

# Who may be asked, how often: the look at the provider is cached this long.
_LOOK_TTL_S = 60.0


def cloud_config_path() -> str:
    """Where this PC's configuration lives on the provider: the shared file, or the file of its own once
    the player chose to keep this library apart."""
    from core.config_manager import get_config
    tag = str(get_config().get("config_separate_tag", "") or "").strip()
    return f"{CLOUD_CONFIG_BASE}.{tag}" if tag else CLOUD_CONFIG_BASE


def meaningful_signature() -> str:
    """A hash of what describes the library and the settings, without what changes all the time."""
    from core.config_manager import get_config
    from core.config_transfer import _MACHINE_SPECIFIC_KEYS
    from core.library import get_library
    settings = {k: v for k, v in get_config().get_all().items()
                if k not in _MACHINE_SPECIFIC_KEYS and not any(m in k for m in _NOISY_SETTING_MARKS)}
    games = sorted(
        ({k: v for k, v in g.to_dict().items() if k not in _VOLATILE_GAME_FIELDS} for g in get_library().all_games()),
        key=lambda d: (str(d.get("name", "")), str(d.get("id", ""))))
    return hashlib.sha256(json.dumps({"s": settings, "l": games}, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def is_push_due() -> bool:
    """True when the library or the settings changed since the last push."""
    from core.config_manager import get_config
    return meaningful_signature() != str(get_config().get("config_push_signature", "") or "")


def note_pushed(provider) -> None:
    """After a push: what was written is the file this PC knows, and the state it was written from."""
    from core.config_manager import get_config
    from core.config_transfer import remote_fingerprint
    from core.library import get_library
    cfg = get_config()
    try:
        meta = provider.get_remote_metadata(cloud_config_path())
        if meta is not None:
            cfg.set("last_cloud_config_hash", remote_fingerprint(meta))
    except Exception:
        logger.debug("could not read back the configuration file's state", exc_info=True)
    cfg.set("config_push_signature", meaningful_signature())
    cfg.set("config_push_games", len(get_library().all_game_ids()))
    cfg.save()
    invalidate()


# ── looking before writing ───────────────────────────────────────────────────

@dataclass
class Preflight:
    kind: str = "clear"              # "clear" | "foreign" | "unreadable" | "unavailable" | "shrunk"
    remote_meta: object = None
    parsed: Optional[dict] = None
    detail: str = ""
    machine_name: str = ""
    exported_at: str = ""
    new_games: list = field(default_factory=list)       # titles the other PC has that this one does not
    merges: list = field(default_factory=list)          # [{keep_id, keep_name, absorb_id, absorb_name, backups}]
    local_games: int = 0
    last_games: int = 0

    @property
    def fingerprint(self) -> str:
        from core.config_transfer import remote_fingerprint
        return remote_fingerprint(self.remote_meta) if self.remote_meta is not None else ""


def find_remote_merges(parsed: dict) -> list:
    """The merges the other PC made that this one has not: a card of its library lists, as absorbed,
    a card this library still has on its own. Recognised by the names and backup folders the absorbing
    card carries, and by the ids of the cards it records (a backup's file name starts with the id of the
    card it was made under)."""
    from core.backup import get_backup_manager
    from core.library import get_library
    local = get_library().all_games()
    by_name = {}
    for g in local:
        by_name.setdefault(g.name.casefold(), g)
    remote = parsed.get("library", []) or []
    remote_names = {str(gd.get("name", "")).casefold() for gd in remote}
    bm = get_backup_manager()
    out, seen = [], set()
    for gd in remote:
        absorbed_ids = {str(i) for i in ((gd.get("cloud_metadata") or {}).get("merged_from") or []) if i}
        if not absorbed_ids:
            continue
        keep = by_name.get(str(gd.get("name", "")).casefold())
        if keep is None:
            continue
        cur_name = str(gd.get("name", "")).casefold()
        cur_folder = str(gd.get("computed_folder_name") or "").casefold()
        names = {str(n).casefold() for n in (gd.get("name_history") or [])} - {cur_name}
        folders = {str(f).casefold() for f in (gd.get("folder_history") or [])} - {cur_folder}
        for g in local:
            if g.id == keep.id or (keep.id, g.id) in seen or g.name.casefold() in remote_names:
                continue
            rows = None
            hit = g.name.casefold() in names or str(g.computed_folder_name or "").casefold() in folders
            if not hit:
                rows = bm.get_backups_for_game(g.id)
                hit = any(str(b.backup_id).split("_", 1)[0] in absorbed_ids for b in rows)
            if not hit:
                continue
            seen.add((keep.id, g.id))
            out.append({"keep_id": keep.id, "keep_name": keep.name, "absorb_id": g.id, "absorb_name": g.name,
                        "backups": len(rows if rows is not None else bm.get_backups_for_game(g.id)),
                        # the cloud folders the other PC's merge is about: where the surviving card is, and where the absorbed one was
                        "shared_folders": [f for f in [gd.get("computed_folder_name")] + list(gd.get("folder_history") or []) if f]})
    return out


def _look(provider) -> Preflight:
    """The uncached look at the file on the provider (network)."""
    from core.config_manager import get_config
    from core.config_transfer import (CONFIG_TRANSFER_VERSION, download_and_parse_cloud_config,
                                      remote_fingerprint)
    from core.library import get_library
    from core.machine import get_machine_id
    cfg = get_config()
    meta = provider.get_remote_metadata(cloud_config_path())
    pre = Preflight(remote_meta=meta, local_games=len(get_library().all_game_ids()),
                    last_games=int(cfg.get("config_push_games", 0) or 0))
    if meta is not None and remote_fingerprint(meta) != str(cfg.get("last_cloud_config_hash", "") or ""):
        try:
            parsed = download_and_parse_cloud_config(provider)
        except ValueError as e:         # (not decryptable, not JSON, not base64: the file itself is bad)
            pre.kind, pre.detail = "unreadable", str(e)
            return pre
        except Exception as e:          # (the download did not go through: nothing is known about the file)
            pre.kind, pre.detail = "unavailable", str(e)
            return pre
        if int(parsed.get("version", 1) or 1) > CONFIG_TRANSFER_VERSION:
            pre.kind, pre.detail = "unreadable", "newer"
            return pre
        pre.parsed = parsed
        pre.machine_name = str(parsed.get("machine_name", "") or "")
        pre.exported_at = str(parsed.get("exported_at", "") or "")
        if parsed.get("machine_id") == get_machine_id():
            # (written by this PC before it kept track of what it wrote)
            cfg.set("last_cloud_config_hash", remote_fingerprint(meta))
            cfg.save()
        else:
            have = {g.name.casefold() for g in get_library().all_games()}
            pre.new_games = [str(gd.get("name", "")) for gd in parsed.get("library", [])
                             if str(gd.get("name", "")).casefold() not in have]
            pre.merges = find_remote_merges(parsed)
            if pre.new_games or pre.merges:
                pre.kind = "foreign"
                return pre
            # nothing to bring in: what differs is settings and details, which the next push carries
            cfg.set("last_cloud_config_hash", remote_fingerprint(meta))
            cfg.save()
    if pre.last_games >= 4 and pre.local_games * 2 < pre.last_games:
        pre.kind = "shrunk"
    return pre


_cache_lock = threading.Lock()
_look_lock = threading.Lock()       # one look at a time: the syncs of many games ask at once, and the second reads the first's answer
_cache: dict = {"at": 0.0, "pre": None, "gen": 0}


def invalidate() -> None:
    with _cache_lock:
        _cache["at"], _cache["pre"] = 0.0, None


def preflight(provider, fresh: bool = False) -> Preflight:
    """Is it safe to write the configuration? (cached for a minute; *fresh* looks again). Meant for a
    thread that may use the network."""
    def cached(min_gen=None):
        with _cache_lock:
            if _cache["pre"] is None:
                return None
            if min_gen is not None:
                return _cache["pre"] if _cache["gen"] > min_gen else None
            return _cache["pre"] if time.monotonic() - _cache["at"] < _LOOK_TTL_S else None

    with _cache_lock:
        asked_at = _cache["gen"]
    if not fresh:
        hit = cached()
        if hit is not None:
            return hit
    with _look_lock:
        # (a look that finished while this one waited for the lock is as good as one made now)
        hit = cached(min_gen=asked_at) if fresh else cached()
        if hit is not None:
            return hit
        pre = _look(provider)
        with _cache_lock:
            _cache["at"], _cache["pre"] = time.monotonic(), pre
            _cache["gen"] += 1
        return pre


def hold_for_game(provider, game_id: str) -> Optional[Preflight]:
    """The look, if a merge the other PC made concerns this game: syncing it now would send its backups
    where the other PC no longer has them. None when the game is free to sync."""
    # (whether the switch for pushing the configuration is on or off: it decides how OFTEN the configuration goes
    # up, not whether a sync may cut a merged game's backups off - that is what a conflict is for)
    try:
        pre = preflight(provider)
    except Exception:
        logger.debug("config look before sync failed", exc_info=True)
        return None
    if pre.kind != "foreign" or not pre.merges:
        return None
    ids = {m["keep_id"] for m in pre.merges} | {m["absorb_id"] for m in pre.merges}
    return pre if game_id in ids else None


# ── the answers ──────────────────────────────────────────────────────────────

def import_library(pre: Preflight, merge_pairs=None) -> dict:
    """Bring the other PC's library here: titles this PC lacks are added (with only the paths that exist on this PC),
    details of the ones it has are joined - and no path, executable or provider record of a game this PC already
    has is touched, whether or not it exists right now (the same game may sit on D: there and on C: here) -
    then the merges the player agreed to are made here too.
    *merge_pairs*: [(keep_id, absorb_id)], default every merge found."""
    from core.config_transfer import apply_import, mark_cloud_config_imported
    from core.merge_cards import merge_cards
    pairs = list(merge_pairs) if merge_pairs is not None else [(m["keep_id"], m["absorb_id"]) for m in pre.merges]
    summary = apply_import(pre.parsed, import_settings=False, import_library=True, import_credentials=False,
                           merge_strategy="keep_local", machine_paths=False)
    summary["merged"], summary["merge_skipped"] = [], []
    for keep_id, absorb_id in pairs:
        out = merge_cards(keep_id, absorb_id)
        (summary["merged"] if out.merged else summary["merge_skipped"]).append((keep_id, absorb_id, out.reason))
    summary["rules"] = import_rules(pre.parsed)
    if pre.remote_meta is not None:
        mark_cloud_config_imported(pre.remote_meta)
    invalidate()
    return summary


# What a player decides about games and processes, and expects to follow them from PC to PC.
_RULE_LISTS = ("save_folder_hints", "ignored_processes", "suppressed_overlay_apps")       # plain values: joined
_RULE_ID_LISTS = ("suppressed_cloud_no_local", "auto_scan_confirmed_games")                # game ids: translated, then joined
_RULE_BY_GAME = ("auto_scan_deselected_paths", "auto_scan_deleted_paths", "auto_scan_excluded_files",
                 "suppressed_ingame_notifs", "scan_auto_accept_games")                     # {game id: value}
_RULE_PROCESS = ("confirmed_process_matches", "rejected_process_matches")                  # {process: game id | [game ids]}


def game_id_map(parsed: dict) -> dict:
    """{the other PC's game id: this library's game id}, by title (the way an import matches games), and
    through the ids a card says it absorbed. A game this library does not have maps to nothing."""
    from core.library import get_library
    by_name = {}
    for g in get_library().all_games():
        by_name.setdefault(g.name.casefold(), g)
    out = {}
    for gd in parsed.get("library", []) or []:
        local = by_name.get(str(gd.get("name", "")).casefold())
        if local is None:
            continue
        if gd.get("id"):
            out[str(gd["id"])] = local.id
        for gid in (gd.get("cloud_metadata") or {}).get("merged_from") or []:
            out.setdefault(str(gid), local.id)
    return out


def import_rules(parsed: dict) -> dict:
    """Bring the other PC's rules here, joined with this PC's own (this PC's first; nothing is replaced, nothing
    removed). Rules about a game follow it by title: the other PC's game ids mean nothing here. Returns
    {setting: number of values added}."""
    from core.config_manager import get_config
    from core.merge_cards import _join
    cfg = get_config()
    theirs = parsed.get("settings", {}) or {}
    ids = game_id_map(parsed)
    changed = {}

    def put(key, value, before):
        if value != before:
            cfg.set(key, value)
            changed[key] = changed.get(key, 0) + 1

    for key in _RULE_LISTS:
        mine = list(cfg.get(key, []) or [])
        add = [v for v in (theirs.get(key) or []) if v not in mine]
        if add:
            put(key, mine + add, mine)
    mine = list(cfg.get("library_folders", []) or [])
    have = {str(f.get("name", "")).casefold() for f in mine if isinstance(f, dict)}
    add = [f for f in (theirs.get("library_folders") or []) if isinstance(f, dict) and str(f.get("name", "")).casefold() not in have]
    if add:
        put("library_folders", mine + add, mine)
    for key in _RULE_ID_LISTS:
        mine = list(cfg.get(key, []) or [])
        add = [ids[g] for g in (theirs.get(key) or []) if g in ids and ids[g] not in mine]
        if add:
            put(key, mine + list(dict.fromkeys(add)), mine)
    for key in _RULE_BY_GAME:
        mine = cfg.get(key, {}) or {}
        new = dict(mine)
        for gid, value in (theirs.get(key) or {}).items():
            local = ids.get(str(gid))
            if local is None:
                continue
            new[local] = _join(new[local], value) if local in new else value
        if new != mine:
            put(key, new, mine)
    mine = dict(cfg.get("confirmed_process_matches", {}) or {})
    new = dict(mine)
    for proc, gid in (theirs.get("confirmed_process_matches") or {}).items():
        if proc not in new and ids.get(str(gid)):
            new[proc] = ids[str(gid)]
    if new != mine:
        put("confirmed_process_matches", new, mine)
    mine = {k: list(v) for k, v in (cfg.get("rejected_process_matches", {}) or {}).items()}
    new = {k: list(v) for k, v in mine.items()}
    for proc, gids in (theirs.get("rejected_process_matches") or {}).items():
        for gid in gids or []:
            local = ids.get(str(gid))
            if local and local not in new.setdefault(proc, []):
                new[proc].append(local)
    new = {k: v for k, v in new.items() if v}
    if new != mine:
        put("rejected_process_matches", new, mine)
    if changed:
        cfg.save()
    return changed


def machine_tag() -> str:
    from core.machine import get_machine_id
    return get_machine_id()[:8]


def fork_card(game_id: str, only_from=None) -> str:
    """The card gets a cloud folder of its own: its local backups follow, and its next upload creates
    the folder. (Nothing is copied in the cloud and nothing there is touched.) Returns the new folder,
    "" if the card was gone - or if it is already apart: *only_from*, when given, are the shared folders
    the fork is about, and a card that is not in one of them (the player already declined to download, and
    the card was forked then) is left where it is."""
    from core.backup import get_backup_manager
    from core.constants import get_folder_name_for_save
    from core.library import get_library
    lib = get_library()
    e = lib.get_by_id(game_id)
    if e is None:
        return ""
    base = e.computed_folder_name or get_folder_name_for_save(e.name, e.exe_path, e.id)
    if only_from is not None and base.casefold() not in {str(f).casefold() for f in only_from}:
        return ""
    unique = lib.unique_folder_name(base, e.id, also_taken={base}, hint=e.id)
    if not unique or unique == base:
        return ""
    get_backup_manager().relocate_game_backups(e.id, base, unique)
    # The new folder starts empty on purpose: its missing index is expected, not a failed fetch
    # (the same allowance "keep both" gives a renamed folder; it is spent by the first upload).
    meta = dict(e.cloud_metadata or {})
    meta["remote_folder_reset"] = list(dict.fromkeys(list(meta.get("remote_folder_reset") or []) + [unique]))
    # The folder it leaves is shared with the other PC's card and is not this game's any more: no sync may
    # treat it as an old name to move this card's zips out of (SyncWorker._old_folder_candidates).
    meta["forked_from"] = list(dict.fromkeys(list(meta.get("forked_from") or []) + [base]))
    lib.update_game_fields(e.id, computed_folder_name=unique, sync_status="pending", cloud_metadata=meta)
    logger.info(f"'{e.name}' keeps apart from the other PC's library: cloud folder {unique!r} (was {base!r})")
    return unique


def keep_separate(pre: Preflight) -> dict:
    """Keep this PC's library apart: its configuration goes to a file of its own from now on, and the
    titles the other PC merged - both the card that absorbed and the one absorbed - fork into cloud
    folders of their own. Only those; the rest of the library stays as it was."""
    from core.config_manager import get_config
    cfg = get_config()
    cfg.set("config_separate_tag", machine_tag())
    # (what was read and written so far belonged to the shared file: the new one starts from nothing)
    cfg.set("last_cloud_config_hash", "")
    cfg.set("config_push_signature", "")
    cfg.set("config_push_games", 0)
    cfg.save()
    forked = {}
    ids, shared = [], set()
    for m in pre.merges:
        shared.update(m.get("shared_folders") or [])
        for gid in (m["keep_id"], m["absorb_id"]):
            if gid not in ids:
                ids.append(gid)
    for gid in ids:
        new = fork_card(gid, only_from=shared)
        if new:
            forked[gid] = new
    invalidate()
    return {"separate_file": cloud_config_path(), "forked": forked}
