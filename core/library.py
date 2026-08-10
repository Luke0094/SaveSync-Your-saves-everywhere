"""
SaveSync - Game Library
Manages the list of tracked games with their save paths.
"""
import copy
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal, QTimer

from core.constants import LIBRARY_FILE

logger = logging.getLogger(__name__)


def tag_merge_key(tag: str) -> str:
    """Canonical merge key for a tag: case-insensitive AND separator-
    insensitive. "2D Game", "2d-game" and "2d_game" all collapse to
    "2d game" — the single key every tag-merging/dedup path (library
    self-heal, filter panel, add/edit dialog) must agree on, so no pair
    of separator variants can ever branch into two catalog entries."""
    import re
    return re.sub(r"[\s_\-]+", " ", (tag or "").casefold()).strip()


VALID_SYNC_STATUSES = frozenset(
    {"synced", "pending", "conflict", "local_only", "cloud_only", "no_saves"}
)

# ── Ratings ─────────────────────────────────────────────────────────────────
# Quarter-star granularity: fine enough that "not quite four stars" can be
# said, coarse enough that a star can still be drawn for it.
RATING_STEP = 0.25
RATING_MAX = 5.0
RATING_MIN = RATING_STEP      # zero means "no rating", not "worthless"


def quantize_rating(value) -> float:
    """A rating snapped to the quarter-star grid, or 0.0 when there is none.

    Out-of-range and unparseable values collapse to 0.0 rather than being
    clamped to a star count nobody chose — a review carrying junk should
    read as unrated, not as one star.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    v = min(v, RATING_MAX)
    return round(round(v / RATING_STEP) * RATING_STEP, 2)


def review_rating(review) -> float:
    """The quantized rating of one review dict."""
    if not isinstance(review, dict):
        return 0.0
    return quantize_rating(review.get("rating"))


def review_vote_count(review) -> int:
    """How many underlying votes one stored review represents.

    Aggregate store scores (Steam percent-positive, VNDB Bayesian average)
    carry ``vote_count`` so a single row is not counted as one opinion.
    Individual reviews (user, DLsite, …) count as 1 when they say anything.
    """
    if not isinstance(review, dict):
        return 0
    try:
        n = int(review.get("vote_count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    if review_rating(review) > 0 or (review.get("text") or "").strip():
        return 1
    return 0


def reviews_display_count(reviews) -> int:
    """Total extracted opinions for UI counters (preview, merge chip, button)."""
    return sum(review_vote_count(r) for r in (reviews or []))


def review_identity(review) -> str:
    """Stable key for one review, so a site's many user reviews do not
    collapse into a single slot.

    Preference order:
      1. source + site id (DLsite's member_review_id, …)
      2. source alone for single-verdict sites (Steam/VNDB — one score each)
      3. source + reviewer + text head, for everything else (including the
         user's own reviews, which all carry source "user")
    """
    if not isinstance(review, dict):
        return ""
    src = str(review.get("source") or "").strip()
    rid = str(review.get("id") or "").strip()
    if src and rid:
        return f"{src}:{rid}"
    if src in ("steam", "vndb", "itch"):
        return src
    who = str(review.get("reviewer") or "").strip()
    text = str(review.get("text") or "").strip()[:80]
    return f"{src}|{who}|{text}"


@dataclass
class GameEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    exe_path: str = ""
    save_paths: list[str] = field(default_factory=list)
    # Subset of save_paths the user deselected during a save confirmation
    # (ui/dialogs/auto_scan_dialog.py). Deselecting a path no longer removes
    # it from save_paths outright — it stays visible/re-selectable in the
    # normal path list, just skipped when an actual backup runs. Contrast
    # with a *deleted* path (trash icon in that same dialog), which is
    # removed from save_paths entirely and instead recorded in
    # cloud_metadata['ignored_paths'] under Settings/Preferences per game,
    # since there's no ordinary UI to bring a deleted path back otherwise.
    excluded_save_paths: list[str] = field(default_factory=list)
    last_played: Optional[str] = None      # ISO datetime
    date_added: Optional[str] = None       # ISO datetime — stamped once in Library.add_game()
    playtime_seconds: int = 0              # Total playtime in seconds
    last_session_seconds: int = 0          # Duration of the most recent play session
    last_synced: Optional[str] = None      # ISO datetime
    last_backed_up: Optional[str] = None   # ISO datetime
    sync_status: str = "local_only"        # synced|pending|conflict|local_only|cloud_only|no_saves
    auto_added: bool = True
    suppressed_overlay: bool = False
    save_paths_confirmed: bool = False   # True once user has confirmed/chosen save paths
    icon_path: Optional[str] = None
    cover_focus: str = "center"            # 3x3 grid position for cover cropping (top-left, top-center, top-right, center-left, center, center-right, bottom-left, bottom-center, bottom-right)
    machine_id: Optional[str] = None       # last backup machine
    cloud_metadata: dict = field(default_factory=dict)
    # Per-game backup settings
    auto_backup_enabled: bool = True       # use global setting when True
    backup_interval_sec: int = 600         # backup every N seconds while playing (default 10 min)
    # Auto-detection tracking
    detection_method: Optional[str] = None  # 'live_tracking', 'general_scan', 'filesystem', 'manual'
    requires_confirmation: bool = False     # True if auto-detected paths need user confirmation
    # User organisation
    category: str = ""                      # folder path (e.g. "RPG/JRPG")
    description: str = ""                   # user notes / game description
    tags: list[str] = field(default_factory=list)  # user-defined tags
    # Game metadata
    developer: str = ""                     # developer / team name
    release_year: str = ""                  # e.g. "2024" or "2024-03-15"
    store_url: str = ""                     # link to store page / official site
    info_source: str = ""                   # source of API metadata (e.g. 'steam', 'itch', 'web')
    # External launcher integration
    appid: Optional[str] = None             # Steam appid, Epic game ID, etc.
    # Store original folder name for recovering old backups/sync data if name changes
    computed_folder_name: Optional[str] = None
    # History of all names this game has had (most recent last).
    # Lets the app find old backup/sync folders when the user renames the game.
    name_history: list[str] = field(default_factory=list)
    # Actual past computed_folder_name values (most recent last). Unlike
    # name_history (display names), these preserve any disambiguation suffix
    # (e.g. "Alpha_2") that get_folder_name_for_save can't reconstruct from a
    # name, so save/backup migration can still find data after a later rename.
    folder_history: list[str] = field(default_factory=list)
    # A save destination registered by hand that could not be anchored yet:
    # the chain relative to the game ("www/save") plus the folder name that
    # identifies the game on disk. Kept so the entry can be re-anchored when
    # the game is (re)installed — possibly somewhere else entirely, since the
    # destination RELATIVE to the game never changes.
    pending_save_chain: str = ""
    pending_save_anchor: str = ""
    # The same destination, kept AFTER it resolves: "www/save" describes where
    # this game's saves live relative to it, and that stays true across
    # reinstalls and across machines. Recorded on every backup so a restore
    # elsewhere can rebase even when the backup carries no executable.
    save_chain: str = ""
    # The same thing, but per save path: {path: chain}. save_chain above can
    # only hold one, and a game can perfectly well have two hand-added folders
    # with different destinations — a copy of "www/save" and a copy of
    # "AppData/Roaming/…". With one field the second destination was simply
    # dropped, silently, and those saves could only ever be put back where
    # they came from.
    #
    # A dict rather than a parallel list on purpose: save_paths is edited from
    # several places (the confirmation dialog removes entries), and positional
    # lists drift apart the first time one of them does. A stale key costs
    # nothing.
    save_path_chains: dict = field(default_factory=dict)
    # One-shot: the user chose "keep local saves" for a game whose cloud folder
    # already holds another machine's data. The NEXT sync must be forced to
    # upload (local wins) — a plain "auto" sync could otherwise DOWNLOAD a
    # newer-mtime cloud copy and overwrite the local one. Cleared on the first
    # successful sync. "up" is additive (never deletes remote), so the other
    # machine's copy survives and stays restorable.
    pending_local_wins: bool = False
    # The names this game was found under, BEFORE they were cleaned up for
    # display: the release folder with its code and version still attached
    # ("[RJ01234] Some Title v1.0"), the executable's own stem, and so on.
    #
    # The display name drops that decoration on purpose — it is noise in a
    # library — but it is the most specific identifier the game has, and it is
    # the one that matches: a folder of saves kept under the full release name
    # and the game's install folder agree on it exactly, while the two tidied
    # titles may not. Kept as a hint, never shown.
    name_hints: list[str] = field(default_factory=list)
    # Which engine built this game — "unity", "rpgmaker", … as named in
    # core.engines.game_engine, or "" when nothing said. Persisted rather
    # than detected on demand for two reasons: detection reads the install
    # folder, which is gone once a game is uninstalled while the library
    # entry (and its backups) are not; and the user can correct it by hand,
    # which a value recomputed from disk would overwrite on the next look.
    engine: str = ""
    # User/web reviews, newest first. Each is a dict:
    #   rating   float, quarter-star steps, 0.25..5 (see quantize_rating)
    #   reviewer str,   who wrote it ("" = this user)
    #   text     str,   the review itself
    #   notes    str,   private remarks, not part of the review
    #   source   str,   where it came from ("user", or a web source id)
    #   at       str,   ISO datetime it was recorded
    # Dicts rather than a dataclass: entries written by an older version
    # simply lack keys, and every reader here treats a missing key as empty.
    reviews: list[dict] = field(default_factory=list)

    def record_path_chain(self, path: str, chain: str):
        """Remember where the saves in *path* belong."""
        if not path or not chain:
            return
        self.save_path_chains = dict(self.save_path_chains or {})
        self.save_path_chains[str(path)] = chain
        # Keep the single field filled for anything still reading it, and for
        # entries written before this one existed.
        if not self.save_chain:
            self.save_chain = chain

    def chain_for_path(self, path: str) -> str:
        """The chain recorded for *path*, or the entry's own as a fallback."""
        chains = self.save_path_chains or {}
        if not chains:
            return self.save_chain or ""
        direct = chains.get(str(path))
        if direct:
            return direct
        wanted = str(path).casefold()
        for known, chain in chains.items():
            if str(known).casefold() == wanted:
                return chain
        return self.save_chain or ""

    def all_chains(self) -> list:
        """Every chain this game knows about, per path and the single one."""
        out = [c for c in (self.save_path_chains or {}).values() if c]
        if self.save_chain and self.save_chain not in out:
            out.append(self.save_chain)
        return out

    def record_exe_hints(self, exe_path: str = ""):
        """Record the raw names an executable is found under.

        The install folder first — that is where a release keeps its code and
        version — then the executable's own stem, but only when it says
        something. "game.exe" says nothing, and recording it would make every
        RPG Maker title in the library answer to the name "game".
        """
        if not exe_path:
            return
        try:
            exe = Path(exe_path)
        except (OSError, ValueError):
            return
        self.record_name_hint(exe.parent.name)
        try:
            from core.save_detector import GENERIC_EXE_STEMS
            if exe.stem.strip().lower() not in GENERIC_EXE_STEMS:
                self.record_name_hint(exe.stem)
        except Exception:
            pass

    def record_name_hint(self, raw: str):
        """Remember a name as it was found, decoration and all."""
        raw = (raw or "").strip()
        if not raw:
            return
        if raw.casefold() not in {h.casefold() for h in self.name_hints}:
            self.name_hints.append(raw)

    def rated_reviews(self) -> list:
        """Reviews that actually carry a rating, in stored order."""
        return [r for r in (self.reviews or []) if review_rating(r) > 0]

    def average_rating(self) -> float:
        """Vote-weighted mean rating on the quarter-star grid; 0 if none.

        Aggregate store scores (Steam / VNDB) weigh by ``vote_count`` so
        300 Steam opinions and 32 VNDB votes are not averaged as two equal
        rows. Individual reviews weigh 1. Unrated (text-only) rows are
        left out instead of counting as zero.
        """
        rated = self.rated_reviews()
        if not rated:
            return 0.0
        total_w = 0
        acc = 0.0
        for r in rated:
            w = review_vote_count(r)
            if w <= 0:
                continue
            acc += review_rating(r) * w
            total_w += w
        if total_w <= 0:
            return 0.0
        return quantize_rating(acc / total_w)

    def __post_init__(self):
        if self.sync_status not in VALID_SYNC_STATUSES:
            logger.warning(f"Invalid sync_status '{self.sync_status}' for '{self.name}', resetting to 'local_only'")
            self.sync_status = "local_only"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameEntry":
        entry = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        # Migrate: ensure current name is in name_history
        if entry.name and entry.name not in entry.name_history:
            entry.name_history.append(entry.name)
        return entry

    def record_name(self, new_name: str):
        """Call whenever the game's display name changes.

        Keeps name_history up to date (no duplicates, most-recent last).
        Also updates computed_folder_name to reflect the new name so that
        future backups/syncs land in a folder named after the current title,
        while the old folder name is preserved in name_history for migration.
        """
        if not new_name:
            return
        if new_name not in self.name_history:
            self.name_history.append(new_name)
        self.name = new_name
        # Recompute folder name based on the new name so the sync/backup
        # destination stays consistent with the current title.
        from core.constants import get_folder_name_for_save
        _old_folder = self.computed_folder_name
        self.computed_folder_name = get_folder_name_for_save(new_name, self.exe_path, self.id)
        # Preserve the ACTUAL previous folder (it may carry a disambiguation
        # suffix that get_folder_name_for_save can't reproduce from a name) so
        # save/backup migration can still locate and move the old data.
        if (_old_folder and _old_folder != self.computed_folder_name
                and _old_folder not in self.folder_history):
            self.folder_history.append(_old_folder)

    def mark_played(self):
        # NOTE: Callers mutate a copy then call update_game() which replaces
        # the entry under _lock, so no additional locking is needed here.
        from datetime import timezone
        self.last_played = datetime.now(timezone.utc).isoformat()
        # Only flag pending if saves actually changed since last sync.
        # Only move from "synced"; don't downgrade "conflict" or "local_only".
        if self.sync_status == "synced" and self._saves_changed_since_sync():
            self.sync_status = "pending"

    def _saves_changed_since_sync(self) -> bool:
        """Return True if any BACKUP-RELEVANT save file is newer than the
        last_synced timestamp. Files that would never be backed up (skip
        extensions, asset subdirs) and user-excluded paths are ignored —
        a game rewriting a log/cache in its save folder at boot must not
        flip the status to "pending" when nothing syncable changed.
        Falls back to True (conservative) when comparison cannot be made."""
        if not self.save_paths:
            return False
        if not self.last_synced:
            return True
        try:
            from dateutil.parser import parse as _parse_dt
            last_sync_ts = _parse_dt(self.last_synced).timestamp()
        except Exception:
            return True
        try:
            from pathlib import Path
            from core.backup import _is_skip_file, _BACKUP_SKIP_DIRS
            from core.registry_saves import is_registry_path, registry_last_write
            excluded = set(self.excluded_save_paths or [])
            for sp in self.save_paths:
                if sp in excluded:
                    continue
                # Registry saves: compare the key tree's last-write against
                # the last sync — the registry equivalent of the file mtime
                # walk below.
                if is_registry_path(sp):
                    if registry_last_write(sp) > last_sync_ts:
                        return True
                    continue
                p = Path(sp)
                if not p.exists():
                    continue
                if p.is_file():
                    if _is_skip_file(p):
                        continue
                    try:
                        if p.stat().st_mtime > last_sync_ts:
                            return True
                    except OSError:
                        pass
                    continue
                for f in p.rglob("*"):
                    if not f.is_file():
                        continue
                    if _is_skip_file(f):
                        continue
                    try:
                        rel_parts = f.relative_to(p).parts
                        if any(part.lower() in _BACKUP_SKIP_DIRS for part in rel_parts[:-1]):
                            continue
                    except ValueError:
                        pass
                    try:
                        if f.stat().st_mtime > last_sync_ts:
                            return True
                    except OSError:
                        pass
            return False
        except Exception:
            return True

    def mark_backed_up(self, machine_id: str):
        from datetime import timezone
        self.last_backed_up = datetime.now(timezone.utc).isoformat()
        self.machine_id = machine_id

    def add_playtime(self, seconds: int):
        """Add playtime in seconds to the total."""
        if seconds > 0:  # guard against negative values from clock skew
            self.playtime_seconds += seconds

    def get_playtime_formatted(self) -> str:
        """Return total playtime formatted as human readable string."""
        return format_duration(self.playtime_seconds)

    def get_last_session_formatted(self) -> str:
        """Return the most recent session's duration, human readable."""
        return format_duration(self.last_session_seconds)


def format_duration(seconds: int) -> str:
    """Human-readable duration: '2d 3h 4m', '1h 5m', '12m', '< 1m'."""
    total = max(0, int(seconds or 0))
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60

    if days > 0:
        if hours > 0:
            return f"{days}d {hours}h {minutes}m"
        return f"{days}d {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return "< 1m"


LIBRARY_SCHEMA_VERSION = 1


class LibraryManager(QObject):
    """Thread-safe game library CRUD with persistence."""
    game_added = Signal(object)       # GameEntry
    game_removed = Signal(str)        # game id
    game_updated = Signal(object)     # GameEntry
    library_loaded = Signal()

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._games: dict[str, GameEntry] = {}
        self._save_timer = QTimer(self)          # debounce disk writes
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)        # flush at most every 500ms
        self._save_timer.timeout.connect(self._flush_save)
        self._load()

    def _load(self):
        if LIBRARY_FILE.exists():
            try:
                with open(LIBRARY_FILE, encoding="utf-8") as f:
                    envelope = json.load(f)
                if isinstance(envelope, list):
                    entries = envelope
                elif isinstance(envelope, dict):
                    version = envelope.get("schema_version", 1)
                    entries = envelope.get("games", [])
                    if version > LIBRARY_SCHEMA_VERSION:
                        logger.warning(f"Library schema v{version} > supported v{LIBRARY_SCHEMA_VERSION}")
                else:
                    entries = []
                with self._lock:
                    for d in entries:
                        g = GameEntry.from_dict(d)
                        self._games[g.id] = g
                logger.info(f"Loaded {len(self._games)} games from library")
                self.merge_tag_case_variants()
            except Exception as e:
                logger.error(f"Library load error: {e}")
        self.library_loaded.emit()

    def merge_tag_case_variants(self) -> int:
        """Unify tag variants ACROSS the library: every merge-key group
        ("2DCG"/"2dcg", "Adventure"/"adventure", "2D Game"/"2d-game")
        converges to its majority spelling (ties → first seen in library
        order), and per-game duplicate variants collapse into one. The key
        is tag_merge_key — case- AND separator-insensitive. Runs at load,
        so the catalog self-heals on startup; the UI keeps the same key
        matching as a safety net for data introduced mid-session (imports,
        cloud restores). Returns the number of games rewritten."""
        from collections import Counter
        with self._lock:
            variants: dict[str, Counter] = {}
            first_idx: dict[tuple, int] = {}
            i = 0
            for g in self._games.values():
                for t in (g.tags or []):
                    if not isinstance(t, str) or not t.strip():
                        continue
                    cf = tag_merge_key(t)
                    variants.setdefault(cf, Counter())[t] += 1
                    first_idx.setdefault((cf, t), i)
                    i += 1
            canon = {
                cf: max(cnt.items(),
                        key=lambda kv: (kv[1], -first_idx[(cf, kv[0])]))[0]
                for cf, cnt in variants.items()
            }
            changed = 0
            for g in self._games.values():
                if not g.tags:
                    continue
                seen: set = set()
                new: list = []
                for t in g.tags:
                    nt = canon.get(tag_merge_key(t), t) if isinstance(t, str) else t
                    key = tag_merge_key(nt) if isinstance(nt, str) else nt
                    if key in seen:
                        continue
                    seen.add(key)
                    new.append(nt)
                if new != g.tags:
                    g.tags = new
                    changed += 1
        if changed:
            logger.info(f"Merged tag case variants in {changed} game(s)")
            self._schedule_save()
        return changed

    def _schedule_save(self):
        """Marshal the timer start to the GUI thread (QTimer is not thread-safe)."""
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(
            self._save_timer, "start",
            Qt.ConnectionType.QueuedConnection,
        )

    def _flush_save(self):
        """Actually write to disk (called by timer or immediate path)."""
        with self._write_lock:
            try:
                with self._lock:
                    entries = [g.to_dict() for g in self._games.values()]
                envelope = {"schema_version": LIBRARY_SCHEMA_VERSION, "games": entries}
                from core import atomic_replace as _atomic_replace
                tmp_path = LIBRARY_FILE.with_suffix(".tmp")
                try:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(envelope, f, indent=2)
                    _atomic_replace(tmp_path, LIBRARY_FILE)
                except Exception:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                    raise
            except Exception as e:
                logger.error(f"Library save error: {e}")

    def _save(self):
        """Immediate synchronous save (for critical ops like remove)."""
        from PySide6.QtCore import QMetaObject, Qt, QThread
        if QThread.currentThread() == self.thread():
            self._save_timer.stop()
        else:
            QMetaObject.invokeMethod(
                self._save_timer, "stop",
                Qt.ConnectionType.QueuedConnection,
            )
        self._flush_save()

    def all_games(self) -> list[GameEntry]:
        with self._lock:
            return [copy.deepcopy(g) for g in self._games.values()]

    def get_by_id(self, gid: str) -> Optional[GameEntry]:
        with self._lock:
            entry = self._games.get(gid)
            if entry is None:
                return None
            return copy.deepcopy(entry)

    def get_by_exe(self, exe_path: str) -> Optional[GameEntry]:
        if not exe_path:
            return None
        try:
            exe = Path(exe_path).resolve()
        except OSError:
            return None
        with self._lock:
            for g in self._games.values():
                if g.exe_path:
                    try:
                        if Path(g.exe_path).resolve() == exe:
                            return copy.deepcopy(g)
                    except OSError:
                        continue
        return None


    def _resolved_folder_names(self, exclude_id: str = "") -> set:
        """Case-folded set of sync/backup folder names currently occupied by
        library entries OTHER than *exclude_id*.

        Windows folder names are case-insensitive, so comparisons are made on
        the case-folded value. Safe to call while already holding ``_lock``
        (it is an RLock)."""
        from core.constants import get_install_folder_name
        names = set()
        with self._lock:
            for gid, e in self._games.items():
                if gid == exclude_id:
                    continue
                fn = get_install_folder_name(
                    e.exe_path or "", e.name, e.id, e.computed_folder_name
                )
                if fn:
                    names.add(fn.casefold())
        return names

    def unique_folder_name(self, base: str, exclude_id: str = "", also_taken=None) -> str:
        """Return *base*, or ``base_2`` / ``base_3`` / … when another library
        entry already occupies that sync/backup folder name.

        Two genuinely different games that share the same title would otherwise
        land in the same ``SaveSync/<name>`` folder and cross-contaminate. The
        suffix disambiguates them. An already-free *base* is returned unchanged,
        so a name that is unique stays stable across calls.

        *also_taken* is an optional iterable of extra folder names to avoid
        (e.g. existing CLOUD folders): the caller can make the result unique
        against destinations beyond the local library."""
        if not base:
            return base
        taken = self._resolved_folder_names(exclude_id)
        if also_taken:
            taken = taken | {s.casefold() for s in also_taken if s}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base}_{n}".casefold() in taken:
            n += 1
        return f"{base}_{n}"

    def unique_display_name(self, base: str, exclude_id: str = "", also_taken=None) -> str:
        """Return *base*, or ``base_2`` / ``base_3`` / … when another library
        entry already uses that display title (case-insensitive).

        Same suffix scheme as ``unique_folder_name`` / keep-both: two distinct
        games whose cleaned titles collide stay visually distinct in the list.
        An already-free *base* is returned unchanged."""
        if not base:
            return base
        taken = {
            (g.name or "").casefold()
            for gid, g in self._games.items()
            if gid != exclude_id and (g.name or "").strip()
        }
        if also_taken:
            taken = taken | {s.casefold() for s in also_taken if s}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base}_{n}".casefold() in taken:
            n += 1
        return f"{base}_{n}"

    def folder_name_in_use_by_other(self, folder_name: str, exclude_id: str = "") -> bool:
        """True when a live entry OTHER than *exclude_id* currently resolves to
        *folder_name* (case-insensitive).

        Used by the rename/migration paths so a game never migrates saves/backups
        OUT of a folder that is still the active home of a different game."""
        if not folder_name:
            return False
        return folder_name.casefold() in self._resolved_folder_names(exclude_id)

    def add_game(self, entry: GameEntry) -> GameEntry:
        with self._lock:
            if not entry.date_added:
                from datetime import timezone
                entry.date_added = datetime.now(timezone.utc).isoformat()
            # Keep each game's sync/backup folder isolated: if another entry
            # already occupies this title's folder, append a numeric suffix.
            from core.constants import get_folder_name_for_save
            base = entry.computed_folder_name or get_folder_name_for_save(
                entry.name, entry.exe_path, entry.id
            )
            entry.computed_folder_name = self.unique_folder_name(base, entry.id)
            self._games[entry.id] = copy.deepcopy(entry)
        self._schedule_save()
        self.game_added.emit(copy.deepcopy(entry))
        logger.info(f"Game added: {entry.name}")
        return copy.deepcopy(entry)

    def update_game(self, entry: GameEntry):
        with self._lock:
            self._games[entry.id] = copy.deepcopy(entry)
        self._schedule_save()
        self.game_updated.emit(copy.deepcopy(entry))

    def update_game_fields(self, game_id: str, **fields) -> Optional[GameEntry]:
        """Atomically update specific fields on a game entry.

        Avoids the read-modify-write race of get_by_id() + update_game()
        where concurrent updates can overwrite each other.

        Returns a deep copy of the updated entry, or None if not found.
        """
        with self._lock:
            live = self._games.get(game_id)
            if live is None:
                return None
            for key, value in fields.items():
                if hasattr(live, key):
                    setattr(live, key, value)
            snapshot = copy.deepcopy(live)
        self._schedule_save()
        self.game_updated.emit(copy.deepcopy(snapshot))
        return snapshot

    def remove_game(self, gid: str):
        with self._lock:
            if gid not in self._games:
                return
            del self._games[gid]
        self._save()
        self.game_removed.emit(gid)

    def find_by_process_name(self, process_name: str,
                              exe_path: str = "") -> Optional[GameEntry]:
        """Match a running process name to a library entry (unicode-safe).

        Priority:
        1. If *exe_path* is given, try an exact resolved-path match first.
           This disambiguates two games that share the same exe stem
           (e.g. two different games both called "launcher.exe").
        2. Exact stem match.
        3. Substring match only when the length difference is ≤ 2 chars.

        Steps 2 and 3 are name-based, so both reject a candidate whose own
        exe path is known to be a different (still existing) program — see
        is_different_program. Step 3 used to skip that test entirely, which
        let a process match a library entry whose exe merely has a
        near-identical name, purely on spelling.
        """
        from core.resolvers import fuzzy_slug as slug   # shared normalizer
        from core.resolvers import is_different_program

        # 1. Exact path match (most reliable)
        if exe_path:
            try:
                resolved = Path(exe_path).resolve()
                with self._lock:
                    games = list(self._games.values())
                for g in games:
                    if g.exe_path:
                        try:
                            if Path(g.exe_path).resolve() == resolved:
                                return copy.deepcopy(g)
                        except OSError:
                            pass
            except OSError:
                pass

        pname = slug(Path(process_name).stem)
        with self._lock:
            games = list(self._games.values())
        for g in games:
            if g.exe_path:
                stem = slug(Path(g.exe_path).stem)
                if not stem or not pname:
                    continue
                if stem == pname:
                    # Same stem but a different, still-existing path → a
                    # different game (two exes sharing a short name).
                    if is_different_program(g.exe_path, exe_path):
                        continue
                    return copy.deepcopy(g)
                shorter, longer = (stem, pname) if len(stem) <= len(pname) else (pname, stem)
                if shorter in longer and (len(longer) - len(shorter)) <= 2:
                    if is_different_program(g.exe_path, exe_path):
                        continue
                    return copy.deepcopy(g)
        return None


_library: LibraryManager | None = None
_library_lock = threading.Lock()


def get_library() -> LibraryManager:
    global _library
    if _library is None:
        with _library_lock:
            if _library is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    logger.warning("LibraryManager created before QApplication — QTimer will be re-parented on first use")
                _library = LibraryManager()
    return _library