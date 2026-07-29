"""
SaveSync - Backup Manager
Creates, lists, and restores local versioned backups of save data.
"""
import copy
import json
import logging
import os
import platform
import shutil
import threading
import zipfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_index_lock = threading.RLock()

from PySide6.QtCore import QObject, Signal

from core.constants import BACKUP_DIR, MAX_LOCAL_BACKUPS, BACKUP_RETENTION_DAYS, MIN_KEPT_BACKUPS, SKIP_EXTENSIONS, SKIP_FILENAME_STEMS, get_install_folder_name
from core.config_manager import get_config
from core.machine import get_machine_id
import i18n

# Extensions that should NEVER be in a save backup (game assets, binaries)
_BACKUP_SKIP_EXTENSIONS = frozenset(SKIP_EXTENSIONS) | frozenset({
    ".dylib", ".bundle",        # macOS libraries
    ".pak", ".pck", ".asset",   # game asset packages
    ".bank", ".fsb",            # audio banks (FMOD, Wwise)
    ".dds", ".ktx", ".pvr",    # compressed textures
    ".webm", ".bik", ".usm",   # game video formats
    ".zip", ".rar", ".7z", ".tar", ".gz",  # archives
    ".tmp", ".temp", ".cache",  # temporary files
})

# Filenames (stem, case-insensitive, extension irrelevant) that should NEVER
# be in a save backup — e.g. "log", "log.txt", "log.dat" are engine noise,
# not player data, no matter what extension they happen to have.
_BACKUP_SKIP_FILENAME_STEMS = SKIP_FILENAME_STEMS


def _is_skip_file(f: Path) -> bool:
    """True if *f* must never be treated as save/backup content — either
    its extension is in _BACKUP_SKIP_EXTENSIONS or its filename stem
    (without extension) matches _BACKUP_SKIP_FILENAME_STEMS."""
    return (f.suffix.lower() in _BACKUP_SKIP_EXTENSIONS
            or f.stem.lower() in _BACKUP_SKIP_FILENAME_STEMS)


def _is_in_skipped_subdir(f: Path, root: Path, keep: frozenset = frozenset()) -> bool:
    """True if any directory between *root* and *f* is a banned backup
    subdirectory (game assets, caches — see _BACKUP_SKIP_DIRS). Files whose
    relative position can't be computed are treated as skipped.

    *keep* holds the directory names on the save chain the user declared for
    this game. A chain as ordinary as "data/www/save" crosses a folder that
    normally reads as game assets, and dropping it would silently leave those
    saves out of the archive. The user pointed at that folder, so the chain
    wins over the general rule.
    """
    try:
        rel_parts = f.relative_to(root).parts
    except ValueError:
        return True
    return any(part.lower() in _BACKUP_SKIP_DIRS and part.lower() not in keep
               for part in rel_parts[:-1])


def _library_entry(game_id: str):
    if not game_id:
        return None
    try:
        from core.library import get_library
        return get_library().get_by_id(game_id)
    except Exception:
        return None


def _declared_chain(game_id: str, save_path: str = "") -> str:
    """The save chain recorded for *save_path*, or the entry's own.

    Per path, because one game can hold two hand-added folders with different
    destinations; the single field is only the fallback for entries written
    before that was possible.
    """
    entry = _library_entry(game_id)
    if entry is None:
        return ""
    if save_path and hasattr(entry, "chain_for_path"):
        return entry.chain_for_path(save_path) or ""
    return getattr(entry, "save_chain", "") or ""


def _chain_parts(chain: str) -> list:
    return [s for s in (chain or "").replace("\\", "/").split("/") if s and s != "."]


def _content_chains(save_paths: list, game_id: str) -> list:
    """For each save path, the declared chain when it really is INSIDE it.

    The test is the path itself — the chain has to exist under the folder —
    so a chain belonging to some other save location of the same game is not
    attached to this one.
    """
    out = []
    for path in save_paths:
        parts = _chain_parts(_declared_chain(game_id, path))
        if not parts:
            out.append("")
            continue
        try:
            out.append("/".join(parts) if (Path(path) / Path(*parts)).exists() else "")
        except (OSError, ValueError):
            out.append("")
    return out


def chain_destination(chain: str, game_id: str = "") -> Optional[Path]:
    """Where a chain's contents belong ON THIS MACHINE, or None.

    Two shapes, and the difference is the whole point:
      "AppData/Roaming/Studio/CODE"  → this machine's own profile. The chain
          carries no account name, so there is nothing to translate: the
          current user IS the answer.
      "www/save"                      → under the game's install folder, which
          means there has to be an executable on file to anchor it.
    """
    parts = _chain_parts(chain)
    if not parts:
        return None
    try:
        from core.manual_paths import profile_destination
        profile = profile_destination(chain)
    except Exception:
        profile = None
    if profile is not None:
        return profile
    exe = ""
    try:
        from core.library import get_library
        entry = get_library().get_by_id(game_id) if game_id else None
        exe = (getattr(entry, "exe_path", "") or "") if entry else ""
    except Exception:
        exe = ""
    if not exe:
        return None
    try:
        return Path(exe).parent.joinpath(*parts)
    except (OSError, ValueError):
        return None


def _declared_chain_dirs(game_id: str) -> frozenset:
    """Directory names on EVERY chain recorded for *game_id*, lowercased.

    All of them, not just the one that matches the path being walked: the set
    only ever spares a directory the user pointed at, and working out which
    chain belongs to which path here would buy nothing.
    """
    entry = _library_entry(game_id)
    if entry is None:
        return frozenset()
    try:
        chains = entry.all_chains() if hasattr(entry, "all_chains") \
            else [getattr(entry, "save_chain", "") or ""]
    except Exception:
        return frozenset()
    return frozenset(part.strip().lower()
                     for chain in chains
                     for part in (chain or "").replace("\\", "/").split("/")
                     if part.strip())

# Subdirectory names to skip entirely during backup (game assets, not saves).
# Shared with the save-path detector via core.skip_dirs; kept under the
# historical name here so existing importers (core.library, save_detector,
# ui.widgets.file_list_widget) are unaffected.
from core.skip_dirs import BACKUP_SKIP_DIRS as _BACKUP_SKIP_DIRS

logger = logging.getLogger(__name__)


# ── Process freeze helpers (Windows-only) ────────────────────────────────────

def _set_process_suspended(pid: int, suspend: bool) -> bool:
    """Suspend (freeze) or resume a process by PID. Returns True on success.
    Uses the undocumented but stable NtSuspendProcess/NtResumeProcess on
    Windows, SIGSTOP/SIGCONT on POSIX."""
    try:
        if platform.system() == "Windows":
            import ctypes
            PROCESS_SUSPEND_RESUME = 0x0800
            kernel32 = ctypes.windll.kernel32
            ntdll = ctypes.windll.ntdll
            handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
            if not handle:
                return False
            try:
                nt_call = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
                return nt_call(handle) == 0  # STATUS_SUCCESS
            finally:
                kernel32.CloseHandle(handle)
        else:
            import signal
            os.kill(pid, signal.SIGSTOP if suspend else signal.SIGCONT)
            return True
    except Exception as e:
        logger.warning(f"Could not {'suspend' if suspend else 'resume'} process {pid}: {e}")
        return False


def _suspend_process(pid: int) -> bool:
    return _set_process_suspended(pid, True)


def _resume_process(pid: int) -> bool:
    return _set_process_suspended(pid, False)


from core import is_relative_to as _is_relative_to, atomic_replace as _atomic_replace


def _substitute_profile_user_candidates(path_str: str, new_user: str) -> list[str]:
    """If *path_str* looks like a per-user profile path — Windows
    ``...\\Users\\<name>\\...``, macOS ``/Users/<name>/...`` or Linux
    ``/home/<name>/...`` — return the path variants with the account-name
    segment replaced by *new_user*, one candidate PER marker position.

    Every ``users``/``home`` segment is tried (not just the first): a path
    can contain more than one — e.g. a foreign profile nested under a
    local one — and only the caller can tell which substitution actually
    exists on this machine. Positions already followed by *new_user* are
    skipped. Empty list = nothing to substitute.

    Used to make a backup's save_paths from another PC/account usable on
    this one: the directory structure under the profile is almost always
    identical between machines — only the account name differs.
    """
    try:
        parts = list(Path(path_str).parts)
    except (OSError, ValueError):
        return []
    candidates: list[str] = []
    for i, part in enumerate(parts[:-1]):
        if part.strip("\\/").lower() in ("users", "home"):
            if parts[i + 1] == new_user:
                continue  # this segment already uses the current user
            new_parts = parts.copy()
            new_parts[i + 1] = new_user
            try:
                candidates.append(str(Path(*new_parts)))
            except Exception:
                continue
    return candidates


def _is_foreign_user_path(path_str: str, current_user: str) -> bool:
    """True if *path_str* contains a Windows ``Users\\<name>``, macOS
    ``/Users/<name>``, or Linux ``/home/<name>`` segment where <name> is
    NOT *current_user*.

    Used as a hard safety gate in restore_backup(): if path resolution
    (same-machine check, username substitution, fresh re-detection) all
    fail to fix such a path, it must never be used as an extraction
    target. ``Path.mkdir(parents=True)`` will happily fabricate a fake
    ``C:\\Users\\OldUser\\AppData\\...`` tree that doesn't correspond to
    any real account on this PC — the write "succeeds" with no error, but
    the actual game (reading from the real current user's profile) never
    sees the restored files. That silent-success-but-nothing-happened
    outcome is worse than a clear failure.
    """
    # A path inside the current user's OWN home can never be a foreign
    # profile, no matter what Users/<name> segments appear higher up
    # (relocated or nested profiles) — resolution may legitimately have
    # just produced such a path, and the marker scan below would misread
    # its first Users segment.
    try:
        if _is_relative_to(Path(path_str).resolve(), Path.home().resolve()):
            return False
    except (OSError, ValueError):
        pass
    try:
        parts = list(Path(path_str).parts)
    except (OSError, ValueError):
        return False
    for marker in ("users", "home"):
        for i, part in enumerate(parts[:-1]):
            if part.strip("\\/").lower() == marker:
                return parts[i + 1] != current_user
    return False


@dataclass
class BackupEntry:
    game_id: str
    game_name: str
    backup_id: str
    created_at: str           # ISO datetime
    machine_id: str
    save_paths: list[str]
    zip_path: str
    size_bytes: int = 0
    note: str = ""
    cloud_metadata: dict = field(default_factory=dict)
    origin: str = "local"     # "local", "onedrive", "google_drive", "dropbox", etc.
    # The game's exe_path at the moment this backup was created. Lets a
    # cross-machine restore recognise a save_path that's simply "some
    # subfolder under the install directory" (rather than a per-user
    # profile path) and rebase it onto THIS machine's actual install
    # location — different drive letter, different parent folder
    # entirely, doesn't matter — instead of only handling the Users/home
    # substitution case. See _resolve_cross_machine_paths's install-
    # relative tier.
    exe_path: str = ""
    # Each save_path expressed RELATIVE to the game's install folder ("" when
    # it isn't under it — a profile path, say). Parallel to save_paths.
    #
    # The install-relative tier can normally derive this at restore time by
    # subtracting exe_path from save_paths. Recording it removes the
    # dependency on that: a backup taken while the game had no executable on
    # file (a save folder registered by hand, before the game itself turned
    # up) carries no exe_path to subtract, and its saves would otherwise be
    # unrestorable on another machine even though the destination — "www/save
    # under the game" — is perfectly well known. Old index files simply lack
    # the field and fall back to the derivation.
    save_chains: list[str] = field(default_factory=list)
    # Each save_path's chain INSIDE it — the structure the folder reproduces,
    # which is where those files belong. Parallel to save_paths, "" when there
    # is none.
    #
    # Different from save_chains, and deliberately a separate field: there the
    # chain says where the save path sits relative to the game, here it says
    # what the save path CONTAINS. A folder handed over by hand is the second
    # kind — "<Title>/AppData/Roaming/Studio/CODE" is a copy of a destination,
    # not a location under an install folder — and without this the
    # destination is known to the library and to nobody else, so a restore
    # can only put the files back where they were copied from.
    content_chains: list[str] = field(default_factory=list)

    def chain_for(self, save_path: str) -> str:
        """The install-relative chain recorded for *save_path*, if any."""
        try:
            return self.save_chains[self.save_paths.index(save_path)]
        except (ValueError, IndexError, TypeError):
            return ""

    def content_chain_for(self, save_path: str) -> str:
        """The chain that lives INSIDE *save_path*, if any."""
        try:
            return self.content_chains[self.save_paths.index(save_path)]
        except (ValueError, IndexError, TypeError):
            return ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackupEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def created_dt(self) -> datetime:
        try:
            dt = datetime.fromisoformat(self.created_at)
            # Normalise to naive UTC so comparisons with other naive-UTC
            # timestamps are consistent regardless of the local timezone.
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            return datetime.min

    @property
    def size_human(self) -> str:
        from core import fmt_size
        return fmt_size(self.size_bytes)


@dataclass
class RestoreResult:
    """Outcome of a restore_backup() call."""
    success: bool
    restored: list[str] = field(default_factory=list)   # files written
    skipped: list[str] = field(default_factory=list)     # identical, not touched
    failed: list[str] = field(default_factory=list)      # could not write (locked, etc.)
    errors: list[str] = field(default_factory=list)      # error messages per failed file
    process_frozen: bool = False                          # True if game was suspended
    # True when NONE of the backup's save_paths could be resolved to a real
    # location on this machine (see restore_backup's foreign-path guard) and
    # files were written to an internal SaveSync export folder instead of
    # the game's actual save location. success can still be True (the write
    # itself succeeded) — this field is what tells the caller/UI the restore
    # did NOT reach the game and needs the user to fix the save path.
    used_fallback_dir: str = ""


class BackupManager(QObject):
    backup_created = Signal(object)   # BackupEntry
    backup_restored = Signal(str)     # game_id
    backup_deleted = Signal(str)      # backup_id

    # Cap per game for the deleted-ids tombstone list (newest kept)
    _MAX_TOMBSTONES_PER_GAME = 200

    def __init__(self):
        super().__init__()
        self._index: list[BackupEntry] = []
        # Tombstones: backup_ids deliberately removed on THIS machine
        # (manual delete or local limit pruning). Consulted by sync so a
        # pruned backup is never re-downloaded from the provider — that
        # download/prune/download loop is what made every sync report
        # uploads+downloads even with completely unchanged data.
        self._deleted_ids: dict[str, list[str]] = self._load_deleted_ids()
        self._load_all_indexes()

    # ── Deleted-backup tombstones ────────────────────────────────────────────

    @staticmethod
    def _deleted_ids_path() -> Path:
        return BACKUP_DIR / "deleted_backups.json"

    def _load_deleted_ids(self) -> dict[str, list[str]]:
        try:
            p = self._deleted_ids_path()
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): [str(b) for b in v] for k, v in data.items()
                            if isinstance(v, list)}
        except Exception as e:
            logger.warning(f"Could not load deleted-backups tombstones: {e}")
        return {}

    def _save_deleted_ids(self):
        try:
            p = self._deleted_ids_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with _index_lock:
                data = dict(self._deleted_ids)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _atomic_replace(tmp, p)
        except Exception as e:
            logger.warning(f"Could not save deleted-backups tombstones: {e}")

    def _record_deleted(self, game_id: str, backup_ids):
        if not backup_ids:
            return
        with _index_lock:
            lst = self._deleted_ids.setdefault(game_id, [])
            for bid in backup_ids:
                if bid not in lst:
                    lst.append(bid)
            if len(lst) > self._MAX_TOMBSTONES_PER_GAME:
                self._deleted_ids[game_id] = lst[-self._MAX_TOMBSTONES_PER_GAME:]
        self._save_deleted_ids()

    def get_deleted_backup_ids(self, game_id: str) -> set[str]:
        """Backup ids deliberately deleted on this machine (see __init__)."""
        with _index_lock:
            return set(self._deleted_ids.get(game_id, []))

    # ── Per-game index I/O ──────────────────────────────────────────────────

    @staticmethod
    def _game_index_path(game_folder: str) -> Path:
        return BACKUP_DIR / game_folder / "index.json"

    def _game_folder_for_entry(self, entry: BackupEntry) -> str:
        """Derive the game subfolder name from an entry's zip_path."""
        zp = Path(entry.zip_path)
        if zp.parent != BACKUP_DIR and zp.parent.parent == BACKUP_DIR:
            return zp.parent.name
        return get_install_folder_name(
            entry.save_paths[0] if entry.save_paths else "", entry.game_name
        )

    def _load_all_indexes(self):
        """Scan all game subdirectories for their index.json files."""
        entries: list[BackupEntry] = []
        if not BACKUP_DIR.exists():
            with _index_lock:
                self._index = entries
            return

        # Load per-game indexes from subdirectories
        for sub in sorted(BACKUP_DIR.iterdir()):
            if not sub.is_dir():
                continue
            idx_path = sub / "index.json"
            if not idx_path.exists():
                continue
            try:
                with open(idx_path, encoding="utf-8") as f:
                    data = json.load(f)
                for d in data:
                    try:
                        entries.append(BackupEntry.from_dict(d))
                    except (TypeError, KeyError) as e:
                        logger.warning(f"Skipping corrupt entry in {idx_path}: {e}")
            except Exception as e:
                logger.error(f"Index load error for {idx_path}: {e}")

        # Legacy: migrate old global index.json if it exists
        old_global = BACKUP_DIR / "index.json"
        if old_global.exists():
            try:
                existing_ids = {e.backup_id for e in entries}
                with open(old_global, encoding="utf-8") as f:
                    data = json.load(f)
                migrated = 0
                for d in data:
                    try:
                        e = BackupEntry.from_dict(d)
                        if e.backup_id not in existing_ids:
                            entries.append(e)
                            migrated += 1
                    except (TypeError, KeyError):
                        pass
                if migrated:
                    logger.info(f"Migrated {migrated} entries from legacy global index")
                # Remove legacy file after migration
                old_global.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Legacy index migration failed: {e}")

        with _index_lock:
            self._index = entries
        self._validate_index()

    def _validate_index(self):
        """Remove entries whose zip file no longer exists on disk."""
        removed: list[str] = []
        affected_games: set[str] = set()
        with _index_lock:
            valid = []
            for entry in self._index:
                if Path(entry.zip_path).exists():
                    valid.append(entry)
                else:
                    removed.append(entry.backup_id)
                    affected_games.add(entry.game_id)
            if removed:
                self._index = valid
        if removed:
            for gid in affected_games:
                self._save_game_index(gid)
            for bid in removed:
                logger.info(f"Backup index: removed stale entry {bid} (zip missing)")
                self.backup_deleted.emit(bid)

    def import_backup(self, entry: BackupEntry, zip_data: bytes) -> bool:
        """Import a backup ZIP downloaded from cloud into the local backup dir.

        Writes the zip to the standard backup path, adds the entry to the
        index.  Returns True on success.
        """
        # Derive folder name: prefer current library entry's computed_folder_name
        # so that backups from another machine (with stale save_paths) are still
        # stored under the correct local game folder.
        try:
            from core.library import get_library as _get_lib
            _lib_e = _get_lib().get_by_id(entry.game_id)
            if _lib_e:
                game_folder = get_install_folder_name(
                    _lib_e.exe_path or "", _lib_e.name,
                    _lib_e.id, _lib_e.computed_folder_name
                )
            else:
                game_folder = get_install_folder_name(
                    "", entry.game_name, entry.game_id, None
                )
        except Exception:
            game_folder = get_install_folder_name(
                entry.save_paths[0] if entry.save_paths else "", entry.game_name
            )
        dest_dir = BACKUP_DIR / game_folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{entry.backup_id}.zip"

        try:
            tmp = dest_path.with_suffix(".tmp")
            tmp.write_bytes(zip_data)
            tmp.replace(dest_path)
        except Exception as e:
            logger.error(f"Failed to write imported backup {entry.backup_id}: {e}")
            return False

        # Update entry with local path and size
        imported = BackupEntry(
            game_id=entry.game_id,
            game_name=entry.game_name,
            backup_id=entry.backup_id,
            created_at=entry.created_at,
            machine_id=entry.machine_id,
            save_paths=entry.save_paths,
            zip_path=str(dest_path),
            size_bytes=dest_path.stat().st_size,
            note=entry.note,
            cloud_metadata=entry.cloud_metadata,
            origin=entry.origin,
            exe_path=entry.exe_path,
        )
        migrated = False
        with _index_lock:
            existing = next((b for b in self._index
                             if b.backup_id == imported.backup_id), None)
            if existing is not None:
                # Already imported. If filed under another machine's game_id,
                # migrate it — and every entry in the same storage folder — to
                # the local id, so get_backups_for_game(local_id) finds
                # cross-PC backups imported before the sync-time re-stamp.
                # (Whole folder at once: one per-folder index file.)
                if existing.game_id != imported.game_id:
                    target_folder = self._game_folder_for_entry(existing)
                    for b in self._index:
                        if (b.game_id != imported.game_id
                                and self._game_folder_for_entry(b) == target_folder):
                            b.game_id = imported.game_id
                    migrated = True
                had_tombstone = False
            else:
                self._index.append(imported)
                # An explicit re-import (e.g. restore of a cloud-only backup)
                # overrides a previous local deletion — drop the tombstone.
                lst = self._deleted_ids.get(imported.game_id)
                had_tombstone = bool(lst and imported.backup_id in lst)
                if had_tombstone:
                    lst.remove(imported.backup_id)
        if existing is not None:
            if migrated:
                self._save_game_index(imported.game_id)
                logger.info(
                    f"Re-filed imported backup {imported.backup_id} under local "
                    f"game_id {imported.game_id}"
                )
            return True
        if had_tombstone:
            self._save_deleted_ids()
        self._save_game_index(imported.game_id)
        self.backup_created.emit(imported)
        logger.info(f"Imported cloud backup: {imported.backup_id} ({imported.size_human})")
        return True

    def _save_game_index(self, game_id: str, _folder_hint: str = ""):
        """Save index.json for a single game into its backup subfolder.

        If no entries remain, removes the index.json and the empty folder.
        *_folder_hint* is used when no entries remain (can't derive from zip_path).
        """
        with _index_lock:
            game_entries = [b for b in self._index if b.game_id == game_id]

        if not game_entries:
            # No entries left — clean up the index file and empty folder
            if _folder_hint:
                idx_path = self._game_index_path(_folder_hint)
                try:
                    idx_path.unlink(missing_ok=True)
                    if idx_path.parent != BACKUP_DIR and idx_path.parent.exists():
                        if not any(idx_path.parent.iterdir()):
                            idx_path.parent.rmdir()
                except Exception:
                    pass
            return

        folder = self._game_folder_for_entry(game_entries[0])
        idx_path = self._game_index_path(folder)
        idx_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            data = [b.to_dict() for b in game_entries]
            tmp_path = idx_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _atomic_replace(tmp_path, idx_path)
        except Exception as e:
            logger.error(f"Game index save error for {game_id}: {e}")

    def _save_index(self):
        """Save all per-game index files. Used after bulk operations."""
        game_ids: set[str] = set()
        with _index_lock:
            for b in self._index:
                game_ids.add(b.game_id)
        for gid in game_ids:
            self._save_game_index(gid)

    @staticmethod
    def _file_fingerprint(path: Path, prev_fp: str = "") -> str:
        """Per-file fingerprint ``size|mtime|sha256``. The content hash means a
        file rewritten with identical bytes but a fresh mtime is NOT counted as
        a change — the mtime-only false positive that produced redundant
        backups. *prev_fp* is this file's fingerprint from the previous backup:
        when its size and mtime still match, the stored content hash is reused
        instead of re-reading the file, so unchanged files are never hashed."""
        import hashlib
        st = path.stat()
        size_mtime = f"{st.st_size}|{st.st_mtime:.6f}"
        if prev_fp:
            parts = prev_fp.split("|")
            if len(parts) == 3 and parts[2] and f"{parts[0]}|{parts[1]}" == size_mtime:
                return prev_fp          # fast path: size+mtime unchanged
        try:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            return f"{size_mtime}|{h.hexdigest()}"
        except OSError:
            return f"{size_mtime}|"      # unreadable → empty content hash

    @staticmethod
    def _fp_content(fp: str) -> str:
        """Content-hash component of a ``size|mtime|sha256`` fingerprint. Empty
        for a legacy ``size|mtime`` fingerprint (or a missing/unreadable file),
        so it compares as changed exactly once after an upgrade."""
        if not fp:
            return ""
        parts = fp.split("|")
        return parts[2] if len(parts) == 3 else ""

    @staticmethod
    def _content_hash(data: bytes) -> str:
        """SHA-256 of raw bytes — used for restore-time comparison."""
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def _get_recent_files(self, save_paths: list[str], hours: int = 24,
                          keep_dirs: frozenset = frozenset()) -> list[Path]:
        """Get files modified in the last N hours for selective backup."""
        import time
        cutoff_time = time.time() - (hours * 3600)
        recent_files: list[Path] = []
        
        for spath in save_paths:
            p = Path(spath)
            if not p.exists():
                continue
                
            if p.is_file():
                try:
                    if p.stat().st_mtime >= cutoff_time:
                        recent_files.append(p)
                except OSError:
                    pass
            elif p.is_dir():
                try:
                    for f in p.rglob("*"):
                        if f.is_file():
                            if _is_skip_file(f):
                                continue
                            if _is_in_skipped_subdir(f, p, keep_dirs):
                                continue
                            try:
                                if f.stat().st_mtime >= cutoff_time:
                                    recent_files.append(f)
                            except OSError:
                                pass
                except OSError:
                    pass
                    
        return recent_files

    def create_backup(
        self,
        game_id: str,
        game_name: str,
        save_paths: list[str],
        exe_path: str = "",
        note: str = "",
        max_size_mb: int = 512,
        computed_folder_name: str | None = None,
        force: bool = False,
        selective: bool = False,
        recent_hours: int = 24,
        origin: str = "local",
        name_history: list[str] | None = None,
        excluded_paths: list[str] | None = None,
        pre_confirmation: bool = False,
        return_status: bool = False,
    ) -> "Optional[BackupEntry] | tuple[Optional[BackupEntry], bool]":
        """Create a zip backup of all save paths for a game.
        Skips silently if nothing changed since last backup (unless force=True).

        Args:
            selective: If True, only backup files modified in recent_hours
            recent_hours: Hours to look back for modified files when selective=True
            name_history: Past names of this game; used to migrate old backup
                folders into the current computed_folder_name folder.
            excluded_paths: Entries from *save_paths* the user deselected during
                a save confirmation (GameEntry.excluded_save_paths). They stay
                in the game's normal path list (still shown, still editable,
                still re-includable) but are skipped here — never zipped,
                never counted toward the change-hash, never selected by
                selective/recent-files mode — so a deselection actually means
                something instead of being silently ignored at backup time.
            pre_confirmation: Mark this backup as TEMPORARY — created from
                auto-detected save paths the user has not confirmed yet
                (first session of a game auto-added from the overlay). Such
                backups protect the player's saves during that first session
                and are restorable in-game, but they are excluded from cloud
                upload until the paths are confirmed. On confirmation they
                are promoted to definitive (promote_pre_confirmation_backups)
                or discarded (discard_pre_confirmation_backups) when the
                detections are rejected/suppressed. Same store and same
                rotation limits as ordinary backups.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def _ret(_entry, _created):
            # created=True only for a genuinely new backup; callers gate sync /
            # notifications / last_backed_up on it instead of guessing from a
            # timestamp window.
            return (_entry, _created) if return_status else _entry

        excluded_set = set(excluded_paths or [])
        if excluded_set:
            skipped = [p for p in save_paths if p in excluded_set]
            if skipped:
                logger.debug(f"create_backup: skipping {len(skipped)} excluded path(s) for '{game_name}': {skipped}")
            save_paths = [p for p in save_paths if p not in excluded_set]

        # Virtual registry entries (Unity PlayerPrefs & co.) ride along in
        # save_paths; they are exported to JSON inside the zip instead of
        # being walked as folders.
        from core.registry_saves import (is_registry_path, registry_key_exists,
                                         export_registry_key, registry_arc_name,
                                         registry_export_fingerprint,
                                         registry_last_write)
        reg_save_paths = [p for p in save_paths if is_registry_path(p)]
        fs_save_paths = [p for p in save_paths if not is_registry_path(p)]
        valid_reg = [r for r in reg_save_paths if registry_key_exists(r)]
        # Directories the user's own recorded chain passes through are never
        # treated as game assets — see _is_in_skipped_subdir.
        chain_dirs = _declared_chain_dirs(game_id)
        valid_paths = [Path(p) for p in fs_save_paths if Path(p).exists()]
        if not valid_paths and not valid_reg:
            logger.warning(f"No valid save paths for backup of '{game_name}'")
            return _ret(None, False)

        # Resolve current folder name
        game_folder = get_install_folder_name(exe_path, game_name, game_id, computed_folder_name)

        # ── Folder migration: move old-name directories into current folder ──
        if name_history:
            self._migrate_old_backup_folders(
                game_id, game_folder, name_history, exe_path
            )

        game_backup_dir = BACKUP_DIR / game_folder
        game_backup_dir.mkdir(parents=True, exist_ok=True)

        backup_id = f"{game_id}_{now.strftime('%Y%m%d_%H%M%S')}"
        zip_path = game_backup_dir / f"{backup_id}.zip"
        all_files: list[tuple[Path, Path]] = []   # (file_path, relative_root)

        # Build set of recently modified files for selective mode
        recent_file_set: set[str] | None = None
        if selective:
            import time as _time
            recent_files = self._get_recent_files(fs_save_paths, recent_hours, chain_dirs)
            reg_recent = any(
                registry_last_write(r) >= _time.time() - recent_hours * 3600
                for r in valid_reg)
            if not recent_files and not reg_recent:
                logger.info(f"Selective backup skipped for '{game_name}' — no recent changes")
                return _ret(None, False)
            recent_file_set = {str(f.resolve()) for f in recent_files}

        # Collect all candidate save files (filtered)
        for sp in valid_paths:
            if sp.is_dir():
                for f in sp.rglob("*"):
                    if not f.is_file():
                        continue
                    if _is_skip_file(f):
                        continue
                    if _is_in_skipped_subdir(f, sp, chain_dirs):
                        continue
                    # In selective mode, skip files not recently modified
                    if recent_file_set is not None:
                        try:
                            if str(f.resolve()) not in recent_file_set:
                                continue
                        except OSError:
                            continue
                    try:
                        all_files.append((f, sp.parent))
                    except OSError:
                        pass
            elif sp.is_file():
                if recent_file_set is not None:
                    try:
                        if str(sp.resolve()) in recent_file_set:
                            all_files.append((sp, sp.parent))
                    except OSError:
                        pass
                else:
                    all_files.append((sp, sp.parent))

        if not all_files and not valid_reg:
            logger.warning(f"No files found to backup for '{game_name}'")
            return _ret(None, False)

        # ── Per-file change detection (content-based) ──────────────────────
        # Each file's fingerprint is size|mtime|sha256; a file counts as
        # "changed" only when its CONTENT hash differs from the previous
        # backup's — a rewrite with identical bytes but a fresh mtime is NOT a
        # change (that mtime-only false positive is what produced redundant
        # backups). The zip ALWAYS contains ALL files (every backup is
        # self-contained), but we skip creating a new one if nothing changed.
        # Unchanged files are never re-read: _file_fingerprint reuses the
        # stored content hash when size+mtime still match.
        import hashlib
        recent = self.get_backups_for_game(game_id) if not force else []
        prev_manifest = (recent[0].cloud_metadata or {}).get("file_manifest", {}) if recent else {}
        new_manifest: dict[str, str] = {}     # arc_name → "size|mtime|sha256"
        resolved_files: list[tuple[Path, Path, str]] = []  # (file, rel_root, arc_name)
        changed_arc_names: list[str] = []
        seen_arc_names: set[str] = set()

        for f, rel_root in all_files:
            try:
                arc_name = str(f.relative_to(rel_root))
                if arc_name in seen_arc_names:
                    prefix = f"_{hashlib.sha256(str(rel_root).encode()).hexdigest()[:8]}"
                    parts = Path(arc_name).parts
                    if len(parts) > 1:
                        arc_name = str(Path(parts[0] + prefix) / Path(*parts[1:]))
                    else:
                        p = Path(parts[0])
                        arc_name = f"{p.stem}{prefix}{p.suffix}"
                seen_arc_names.add(arc_name)

                fp = self._file_fingerprint(f, prev_manifest.get(arc_name, ""))
                new_manifest[arc_name] = fp
                resolved_files.append((f, rel_root, arc_name))

                if self._fp_content(fp) != self._fp_content(prev_manifest.get(arc_name, "")):
                    changed_arc_names.append(arc_name)
            except (OSError, ValueError) as e:
                logger.warning(f"Skipping file {f}: {e}")

        # ── Registry exports (state INSIDE registry values) ────────────────
        # Canonical JSON per key: identical registry state ⇒ identical bytes
        # ⇒ the same content-hash change detection files get.
        reg_exports: list[tuple[str, bytes]] = []   # (arc_name, data)
        for rp in valid_reg:
            data = export_registry_key(rp)
            if data is None:
                logger.warning(f"Registry export failed, key skipped: {rp}")
                continue
            arc_name = registry_arc_name(rp)
            if arc_name in seen_arc_names:
                continue        # two keys sanitizing identically — keep first
            seen_arc_names.add(arc_name)
            fp = registry_export_fingerprint(data)
            new_manifest[arc_name] = fp
            reg_exports.append((arc_name, data))
            if self._fp_content(fp) != self._fp_content(prev_manifest.get(arc_name, "")):
                changed_arc_names.append(arc_name)

        # Also detect files that were DELETED since the previous backup
        # (present in prev_manifest but absent now)
        deleted_files = set(prev_manifest.keys()) - set(new_manifest.keys())
        if deleted_files:
            changed_arc_names.extend(f"[deleted] {d}" for d in deleted_files)

        # Whole-state content hash, derived from the per-file content hashes —
        # one read pass, no separate scan. Stored as save_hash and used as the
        # fast "nothing changed" early-out below.
        _state = hashlib.sha256()
        for _arc in sorted(new_manifest):
            _state.update(f"{_arc}|{self._fp_content(new_manifest[_arc])}\n".encode())
        current_hash = _state.hexdigest()

        # Dedup gates (skipped entirely when force=True → recent is empty):
        if recent:
            last_hash = (recent[0].cloud_metadata or {}).get("save_hash", "")
            if current_hash and current_hash == last_hash:
                logger.info(f"Backup already current for '{game_name}' — reusing existing")
                return _ret(recent[0], False)
            # Hard debounce: never more than 1 backup per 30 seconds.
            try:
                age = (now - recent[0].created_dt).total_seconds()
            except (ValueError, TypeError):
                age = 999  # malformed timestamp, allow backup
            if age < 30:
                return _ret(recent[0], False)

        if not changed_arc_names:
            logger.info(f"Backup skipped for '{game_name}' — all {len(new_manifest)} files unchanged (per-file check)")
            return _ret(recent[0] if recent else None, False)

        # Pre-scan total size (ALL files — zip is always self-contained)
        max_bytes = max_size_mb * 1024 * 1024
        total_bytes = sum(len(d) for _, d in reg_exports)
        for f, _, _ in resolved_files:
            try:
                total_bytes += f.stat().st_size
            except OSError:
                pass

        if total_bytes > max_bytes:
            logger.error(
                f"Backup aborted for '{game_name}': "
                f"{total_bytes // (1024*1024)} MB exceeds limit of {max_size_mb} MB"
            )
            return _ret(None, False)

        zip_path_tmp = None
        try:
            # Write ALL files to .tmp first, then atomic rename.
            # Every backup is a complete snapshot so any single backup can
            # be restored without depending on previous ones.
            zip_path_tmp = zip_path.with_suffix(".tmp")
            zf = None
            try:
                zf = zipfile.ZipFile(zip_path_tmp, "w", zipfile.ZIP_DEFLATED)
                for f, rel_root, arc_name in resolved_files:
                    try:
                        zf.write(f, arc_name)
                    except (OSError, ValueError) as e:
                        logger.warning(f"Skipping file {f}: {e}")
                for arc_name, data in reg_exports:
                    zf.writestr(arc_name, data)
            finally:
                if zf:
                    zf.close()

            zip_path_tmp.replace(zip_path)

            changed_real = [c for c in changed_arc_names if not c.startswith("[deleted]")]
            logger.info(
                f"Backup: {len(changed_real)}/{len(new_manifest)} files changed, "
                f"{len(deleted_files)} deleted since previous backup"
            )

            metadata = {
                "save_hash": current_hash,
                "file_manifest": new_manifest,
                "files_changed": len(changed_real),
                "files_total": len(new_manifest),
                "backup_type": "full",  # always self-contained
            }
            if pre_confirmation:
                metadata["pre_confirmation"] = True

            _recorded_paths = [str(p) for p in valid_paths] + valid_reg
            entry = BackupEntry(
                game_id=game_id,
                game_name=game_name,
                backup_id=backup_id,
                created_at=now.isoformat(),
                machine_id=get_machine_id(),
                save_paths=_recorded_paths,
                zip_path=str(zip_path),
                size_bytes=zip_path.stat().st_size,
                note=note,
                cloud_metadata=metadata,
                origin=origin,
                exe_path=exe_path,
                save_chains=self._install_relative_chains(
                    _recorded_paths, exe_path, game_id),
                content_chains=_content_chains(_recorded_paths, game_id),
            )
            with _index_lock:
                self._index.append(entry)
            self._save_game_index(game_id)
            self._enforce_limits(game_id)
            self.backup_created.emit(entry)
            logger.info(f"Backup created: {backup_id} ({entry.size_human})")
            return _ret(entry, True)
        except Exception as e:
            # Cleanup on failure
            if zip_path_tmp is not None and zip_path_tmp.exists():
                try:
                    zip_path_tmp.unlink()
                except OSError:
                    pass
            logger.error(f"Backup failed for '{game_name}': {e}")
            return _ret(None, False)

    @staticmethod
    def _install_relative_chains(save_paths: list, exe_path: str,
                                 game_id: str = "") -> list:
        """Express each save path relative to the game's install folder.

        Two sources, in order: the executable being backed up (subtract its
        folder), and — when there is no executable — the destination the user
        registered by hand for this game, which is exactly this chain and is
        the only thing that survives the game not existing yet.

        "" for anything that isn't install-relative (a profile path), so the
        list stays positionally parallel to save_paths.
        """
        from core.registry_saves import is_registry_path
        install_dir = None
        if exe_path:
            try:
                install_dir = Path(exe_path).parent.resolve()
            except (OSError, ValueError):
                install_dir = None

        # Only consulted when there is no executable to subtract; per path,
        # since one game can have declared more than one destination.
        def declare(path: str) -> str:
            if install_dir is not None or not game_id:
                return ""
            return _declared_chain(game_id, path)

        chains = []
        for path in save_paths:
            if is_registry_path(path):
                chains.append("")
                continue
            if install_dir is not None:
                try:
                    chains.append(Path(path).resolve().relative_to(install_dir).as_posix())
                except (ValueError, OSError):
                    chains.append("")
                continue
            # No executable on file: fall back to what the user declared, but
            # only when the path really ends with it — otherwise it describes
            # some other save location of the same game.
            declared = declare(path)
            if declared and Path(path).as_posix().lower().endswith(
                    "/" + declared.strip("/").lower()):
                chains.append(declared.strip("/"))
            else:
                chains.append("")
        return chains

    def _resolve_cross_machine_paths(self, entry: "BackupEntry",
                                     lib_game_id: str = "") -> list[str]:
        """Cross-machine resolution with registry pass-through.

        Virtual ``registry:HKCU\\...`` entries are user-relative BY NATURE —
        there is nothing to rebase across machines — so they are carved out,
        the filesystem tiers run on the rest, and they are spliced back in.
        """
        from core.registry_saves import is_registry_path
        all_paths = list(entry.save_paths or [])
        reg = [p for p in all_paths if is_registry_path(p)]
        if not reg:
            return self._resolve_cross_machine_fs_paths(entry, lib_game_id)
        from dataclasses import replace as _dc_replace
        fs_entry = _dc_replace(entry, save_paths=[p for p in all_paths
                                                  if not is_registry_path(p)])
        resolved_fs = self._resolve_cross_machine_fs_paths(fs_entry, lib_game_id)
        # A tier may return the library entry's paths, which can already
        # contain the registry entries — dedupe before splicing back.
        return [p for p in resolved_fs if p not in reg] + reg

    def _resolve_cross_machine_fs_paths(self, entry: "BackupEntry",
                                        lib_game_id: str = "") -> list[str]:
        """Resolve a backup's recorded save_paths to valid locations on
        *this* machine, transparently handling backups restored on a
        different PC or under a different OS user account.

        *lib_game_id*: the LOCAL library game_id to resolve against. Defaults
        to the backup's own game_id, but callers restoring a cross-PC backup
        that is still filed under a foreign game_id pass the local id so the
        library entry (and its index save path) can still be found.

        Four tiers, cheapest/most-precise first — evaluated per path so a
        game with several save_paths can mix outcomes:

          1. Already valid here (same PC, or paths that don't depend on the
             user account) — left untouched. The overwhelming common case.
          2. Username substitution — most cross-PC mismatches are nothing
             more than a different account name in an otherwise identical
             profile path (...\\Users\\OLD\\AppData\\... →
             ...\\Users\\NEW\\AppData\\...). Cheap, deterministic, and keeps
             the exact folder structure recorded at backup time. Works
             identically for Windows Users\\, macOS /Users/, and Linux
             /home/ — it's a plain path-segment match, not tied to any
             specific subfolder convention like Roaming/AppData.
          3. Install-directory-relative rebasing — for a save path that
             lives INSIDE the game's own install folder rather than a
             per-user profile (e.g. <install_dir>/saves/slot1), the drive
             letter or parent path can differ completely between machines
             (D:\\Games\\MyGame vs E:\\SteamLibrary\\MyGame) while the
             portion *relative to the exe* stays identical. Recomputing
             that relative portion against the exe's CURRENT location on
             this machine resolves it exactly, without any scoring/
             guessing — this only requires knowing where the exe was at
             backup time (BackupEntry.exe_path) and where it is now
             (the library entry's own exe_path).
          4. Fresh detection — if any path still can't be resolved, re-run
             the same auto-detect engine the Add Game dialog uses (we
             already know the game's name / exe / appid from the library),
             which finds the right save folder(s) here even if the layout
             differs entirely (different store, fresh install that's never
             been run, or an exe_path we never actually recorded).

        Whichever tier resolves things, the corrected paths are written back
        into the library entry, so future restores — and the Add/Edit Game
        dialog — immediately reflect the correct, current-machine path
        instead of re-detecting (or showing nothing) every time.
        """
        paths = list(entry.save_paths or [])

        from core.library import get_library
        lib_entry = get_library().get_by_id(lib_game_id or entry.game_id)

        def _parent_exists(p: str) -> bool:
            # "Valid on this machine" = the path itself exists, or its
            # DIRECT parent does (a save leaf that just hasn't been created
            # yet under a real game folder). The previous grandparent check
            # (pp.parent.parent.exists()) accepted foreign paths from other
            # PCs whenever a generic ancestor happened to exist here — e.g.
            # old-PC "D:\Games\MyGame\save" was kept because "D:\Games"
            # exists locally, Tier 1 short-circuited the install-dir rebase
            # and re-detection, and the restore then fabricated the folder
            # on the wrong disk with an apparent success.
            # See _under_current_profile for the companion rule that covers
            # save folders not created yet at all (first save pending).
            try:
                pp = Path(p)
                return pp.exists() or pp.parent.exists()
            except (OSError, ValueError):
                return False

        def _under_current_profile(p: str) -> bool:
            """Accept a path under THIS user's standard save roots even when
            its parent chain does not exist yet: engines create their profile
            subfolders on the first save (e.g. Roaming/<Studio>/<Game>/save),
            the structure below the profile is machine-independent, and
            fabricating there is exactly where the game will write/read on
            this machine — the canonical case being a provider restore on a
            fresh system where the game has never saved locally.

            Deliberately restricted to the engine save roots (Roaming/
            Local/LocalLow/Documents/Saved Games, Temp excluded): foreign
            profiles, foreign disks, and arbitrary profile folders like
            Desktop still go through substitution/rebase/re-detection."""
            try:
                home = Path.home().resolve()
                rp = Path(p).resolve()
            except (OSError, ValueError):
                return False
            if _is_relative_to(rp, home / "AppData" / "Local" / "Temp"):
                return False   # never a save location
            for root in ("AppData/Roaming", "AppData/Local", "AppData/LocalLow",
                         "Documents", "Saved Games"):
                if _is_relative_to(rp, home.joinpath(*root.split("/"))):
                    return True
            return False

        def _rebase_on_current_install(p: str) -> Optional[str]:
            """Tier 3: if *p* sits under the OLD exe's install directory,
            re-root the same relative path under the CURRENT exe's install
            directory. Returns None if there's nothing to rebase against
            (no recorded old exe_path, no current exe_path, or *p* isn't
            actually inside the old install directory at all)."""
            old_exe = entry.exe_path
            new_exe = lib_entry.exe_path if lib_entry else ""
            if not old_exe or not new_exe:
                return None
            try:
                old_install_dir = Path(old_exe).parent
                new_install_dir = Path(new_exe).parent
                rel = Path(p).relative_to(old_install_dir)
            except (ValueError, OSError):
                return None   # p isn't under old_install_dir at all
            try:
                candidate = str(new_install_dir / rel)
            except Exception:
                return None
            return candidate

        def _under_current_install(p: str) -> bool:
            """Accept a save path under THIS machine's current install
            directory for the game even when it doesn't exist yet.

            The disk-location companion to _under_current_profile: a game that
            keeps its saves inside its own install folder (e.g. RPG Maker XP
            writing Save1.rxdata beside Game.exe) creates them on the first
            save, so the folder may legitimately not exist at restore time —
            and, unlike a foreign USER profile, fabricating a folder under the
            real, current install directory is exactly right. The install root
            (drive/path) is the other thing that changes between machines."""
            new_exe = lib_entry.exe_path if lib_entry else ""
            if not new_exe:
                return False
            try:
                install_dir = Path(new_exe).parent.resolve()
                rp = Path(p).resolve()
            except (OSError, ValueError):
                return False
            return _is_relative_to(rp, install_dir)

        def _rebase_on_declared_chain(p: str) -> Optional[str]:
            """Last install-relative resort: the chain RECORDED on the backup.

            Tier 3 subtracts the old executable from the old path, so it needs
            that executable. A backup taken while the game had none — a save
            folder registered by hand before the game itself existed — has
            nothing to subtract, yet its destination ("www/save under the
            game") was known all along and travels with the backup. With this
            machine's install folder that is all it takes.
            """
            chain = entry.chain_for(p) if hasattr(entry, "chain_for") else ""
            new_exe = lib_entry.exe_path if lib_entry else ""
            if not chain or not new_exe:
                return None
            try:
                candidate = Path(new_exe).parent.joinpath(*chain.split("/"))
            except (OSError, ValueError):
                return None
            candidate_str = str(candidate)
            if candidate_str == p:
                return None
            return candidate_str if (_parent_exists(candidate_str)
                                     or _under_current_install(candidate_str)) else None

        def _resolve_via_tiers(paths_in: list[str]):
            """Run Tiers 1-3 over *paths_in* and return
            (resolved, all_ok, any_changed). The game's own recorded path is
            authoritative; only the two things that differ between machines are
            adjusted, and a not-yet-created target is accepted (restore creates
            it — the game would otherwise create it on first save):
              1. valid here (exists / direct-parent exists / under this user's
                 profile or this machine's install dir, even if not created),
              2. username substitution across every users/home marker,
              3. install-directory-relative rebasing (disk/path change).
            """
            current_user = Path.home().name
            resolved_: list[str] = []
            all_ok_ = True
            any_changed_ = False
            for p in paths_in:
                if _parent_exists(p) or _under_current_profile(p) or _under_current_install(p):
                    resolved_.append(p)
                    continue
                substituted = next(
                    (c for c in _substitute_profile_user_candidates(p, current_user)
                     if _parent_exists(c) or _under_current_profile(c)),
                    None,
                )
                if substituted:
                    resolved_.append(substituted)
                    any_changed_ = True
                    continue
                rebased = _rebase_on_current_install(p)
                if rebased and (_parent_exists(rebased) or _under_current_install(rebased)):
                    resolved_.append(rebased)
                    any_changed_ = True
                    continue
                declared = _rebase_on_declared_chain(p)
                if declared:
                    resolved_.append(declared)
                    any_changed_ = True
                    continue
                resolved_.append(p)
                all_ok_ = False
            return resolved_, all_ok_, any_changed_

        if not paths:
            # Nothing recorded on the backup itself (can happen for some cloud
            # metadata round-trips) — skip straight to the library-index /
            # detection tiers below rather than giving up.
            all_ok = False
            resolved = []
        else:
            # ── Tiers 1+2+3 on the backup's own recorded save_paths ─────────
            resolved, all_ok, any_changed = _resolve_via_tiers(paths)
            if all_ok:
                if any_changed:
                    logger.info(
                        f"restore_backup: remapped save_paths to current machine "
                        f"for '{entry.game_name}': {resolved}"
                    )
                # Record on the library entry so future exit-backups cover the
                # restored location: fills "my saves" when empty, and when the
                # user already keeps their own path(s) the restored one is
                # APPENDED as an additional path — never dropped, never
                # clobbering what the user configured.
                self._persist_resolved_paths(resolved, lib_entry, merge=True)
                return resolved

        # ── Tier 3.5 — reconstruct from the LIBRARY INDEX path ─────────────
        # Before name-based detection: the library's stored save path (run
        # through the same user/disk tiers) is where the game actually
        # reads/writes here, while Tier 4's name matching rarely fits engine
        # 'productName' folders. Detection stays a genuine last resort.
        if lib_entry and lib_entry.save_paths:
            lib_resolved, lib_ok, lib_changed = _resolve_via_tiers(list(lib_entry.save_paths))
            if lib_ok and lib_resolved:
                verb = "reconstructed" if lib_changed else "using"
                logger.info(
                    f"restore_backup: {verb} library index save_paths for "
                    f"'{entry.game_name}': {lib_resolved}"
                )
                self._persist_resolved_paths(lib_resolved, lib_entry)
                return lib_resolved

        # ── Tier 4 — fresh auto-detection (same engine as Add Game) ────────
        if lib_entry and (lib_entry.name or lib_entry.exe_path):
            # If the game happens to be running right now — notably true for
            # the "just detected as unknown → add to library → download &
            # restore cloud saves" flow, where the process that triggered
            # detection is still alive at this exact moment — pass its PID
            # through so detect_save_paths's live open-file strategy (exact,
            # mtime-verified) can contribute alongside the static heuristics,
            # instead of Tier 4 relying on heuristics alone even when a much
            # more precise signal is available for free.
            _live_pid = None
            try:
                from core.monitor import get_monitor
                if lib_entry.exe_path:
                    _found_pid = get_monitor().find_pid_by_exe(lib_entry.exe_path)
                    _live_pid = _found_pid if _found_pid else None
            except Exception:
                _live_pid = None
            try:
                from core.save_detector import detect_save_paths
                detected = detect_save_paths(
                    lib_entry.name or entry.game_name,
                    exe_path=lib_entry.exe_path or None,
                    appid=lib_entry.appid or None,
                    pid=_live_pid,
                )
            except Exception as _det_err:
                logger.debug(f"restore_backup: fresh detection failed: {_det_err}")
                detected = []
            if detected:
                logger.info(
                    f"restore_backup: re-detected save_paths on this machine "
                    f"for '{entry.game_name}' (live pid={_live_pid}): {detected}"
                )
                self._persist_resolved_paths(detected, lib_entry)
                return detected

        # ── Tier 5 — last resort: whatever the library already has on file ─
        if lib_entry and lib_entry.save_paths and any(_parent_exists(p) for p in lib_entry.save_paths):
            logger.info(
                f"restore_backup: falling back to library save_paths for "
                f"'{entry.game_name}': {lib_entry.save_paths}"
            )
            return list(lib_entry.save_paths)

        logger.warning(
            f"restore_backup: could not resolve save_paths on this machine for "
            f"'{entry.game_name}' — proceeding with original (possibly stale) "
            f"paths: {resolved}"
        )
        return resolved

    def _persist_resolved_paths(self, paths: list[str], lib_entry,
                                merge: bool = False) -> None:
        """Write freshly-resolved save path(s) back into the library entry so
        future restores/backups — and the Add/Edit Game dialog — reflect them
        immediately (update_game_fields mutates the in-memory entry under lock
        before returning; the disk flush is debounced).

        merge=True: keep the user's existing paths and APPEND the new ones
        (path-normalized dedupe) instead of replacing the list — used for the
        backup's own restored location. merge=False replaces: used when the
        list IS the corrected version of the library's own paths (Tier 3.5)
        or fresh detection with nothing valid on file (Tier 4).

        Keyed on lib_entry.id (the LOCAL library id): a cross-PC backup can
        still carry the originating machine's game_id, which would make an
        update by that id a silent no-op. Skips when nothing would change
        (no redundant write/signal per restore)."""
        if not lib_entry:
            return
        from core.registry_saves import is_registry_path
        existing = list(lib_entry.save_paths or [])
        if merge and existing:
            def _norm(p: str) -> str:
                if is_registry_path(p):
                    return p.lower()
                return os.path.normcase(os.path.normpath(p))
            seen = {_norm(p) for p in existing}
            new_paths = existing + [p for p in paths if _norm(p) not in seen]
        else:
            # Replace applies to FILESYSTEM paths only: the resolver never
            # re-evaluates registry entries (they're user-relative by
            # nature), so a Tier-4 replace must not wipe them from the
            # library.
            kept_reg = [p for p in existing if is_registry_path(p)
                        and p not in paths]
            new_paths = list(paths) + kept_reg
        if existing == new_paths:
            return
        try:
            from core.library import get_library
            get_library().update_game_fields(
                lib_entry.id, save_paths=new_paths, save_paths_confirmed=True,
            )
        except Exception as _persist_err:
            logger.debug(f"restore_backup: failed to persist resolved paths: {_persist_err}")

    def restore_backup(self, backup_id: str,
                       freeze_pid: int = 0,
                       only_files: set[str] | None = None,
                       lib_game_id: str = "") -> RestoreResult:
        """Restore a backup zip to its original save locations.

        Files whose content matches the backup are skipped.  Files that
        cannot be written (locked) are recorded in *result.failed* — the
        restore continues with the remaining files.

        Args:
            freeze_pid:  If > 0, suspend this process before writing and
                         resume it after.
            only_files:  If set, only restore these arc_names (used for
                         retrying previously failed files).  Safety backup
                         is skipped when retrying.

        Returns:
            RestoreResult with per-file detail.
        """
        entry = self.get_backup(backup_id)
        if not entry:
            logger.error(f"Backup not found: {backup_id}")
            return RestoreResult(success=False, errors=["Backup not found"])

        # ── Chain redirect ──────────────────────────────────────────────────
        # A folder handed over by hand is a COPY of a destination: it holds
        # "AppData/Roaming/Studio/CODE", or "www/save", inside it. Putting the
        # files back where they were copied from would rebuild the copy and
        # leave the game with nothing, so the chain decides where they go —
        # this machine's own profile, or this machine's install folder.
        #
        # Keyed by the archive's top-level name, which is the save folder's
        # name AT BACKUP TIME, so it is read before any path resolution.
        # A list per name, not one entry: two folders of the same game share
        # the same folder NAME as often as not, and it is the chain that tells
        # their archive members apart.
        redirects: dict = {}
        for _sp in entry.save_paths:
            _chain = entry.content_chain_for(_sp)
            if not _chain:
                continue
            _dest = chain_destination(_chain, lib_game_id or entry.game_id)
            if _dest is None:
                continue
            redirects.setdefault(Path(_sp).name, []).append(
                (_chain_parts(_chain), _dest))
            logger.info(f"Restore: '{Path(_sp).name}' carries {_chain} — "
                        f"restoring it to {_dest}")
        # Longest chain first: "data/www/save" must be tried before "data".
        for _name in redirects:
            redirects[_name].sort(key=lambda pair: len(pair[0]), reverse=True)

        # ── Cross-machine path resolution ──────────────────────────────────────
        # Backups made on another PC/account carry save_paths from that
        # machine's user profile (e.g. C:\Users\OldUser\…), which don't exist
        # here. We've already identified the *game* correctly (entry.game_id
        # matched a library entry) — so we resolve the right path for *this*
        # user too, instead of giving up. See _resolve_cross_machine_paths().
        try:
            resolved_paths = self._resolve_cross_machine_paths(entry, lib_game_id)
            if resolved_paths != entry.save_paths:
                from dataclasses import replace as _dc_replace
                entry = _dc_replace(entry, save_paths=resolved_paths)
        except Exception as _path_err:
            logger.debug(f"restore_backup path-resolution failed: {_path_err}")

        # ── Hard safety gate ────────────────────────────────────────────────
        # If resolution above still leaves a path pointing at a DIFFERENT
        # user's profile, extraction must never target it: Path.mkdir(parents=
        # True) would silently fabricate a fake "C:\Users\OldUser\..." tree
        # that isn't this machine's real profile, the write would report
        # success, and the actual game (reading its real current-user
        # profile) would never see the restored files — a "nothing happened"
        # outcome with no error anywhere. Any such path is dropped; if that
        # empties save_paths entirely, the whole restore redirects to the
        # same internal-export fallback used when a backup has no save_paths
        # at all, and the caller is told so via used_fallback_dir.
        _current_user = Path.home().name
        _safe_paths = [p for p in entry.save_paths if not _is_foreign_user_path(p, _current_user)]
        if entry.save_paths and not _safe_paths:
            logger.warning(
                f"restore_backup: every save_path for '{entry.game_name}' still "
                f"points at another user's profile after resolution — refusing "
                f"to fabricate it; restoring to internal export folder instead. "
                f"Original paths: {entry.save_paths}"
            )
            from dataclasses import replace as _dc_replace
            entry = _dc_replace(entry, save_paths=[])
        elif len(_safe_paths) != len(entry.save_paths):
            from dataclasses import replace as _dc_replace
            entry = _dc_replace(entry, save_paths=_safe_paths)

        saved_zip_path = Path(entry.zip_path)
        if not saved_zip_path.exists():
            logger.error(f"Backup zip missing: {saved_zip_path}")
            return RestoreResult(success=False, errors=["Backup zip missing"])

        # Copy zip to temp to protect from _enforce_limits deletion.
        # Use TemporaryDirectory as context manager to guarantee cleanup
        # even if shutil.copy2 or any later step fails.
        import tempfile
        _restore_tmp_ctx = tempfile.TemporaryDirectory(prefix="savesync_restore_")
        try:
            _restore_tmp = Path(_restore_tmp_ctx.name)
            _restore_zip = _restore_tmp / saved_zip_path.name
            shutil.copy2(saved_zip_path, _restore_zip)
        except Exception as e:
            _restore_tmp_ctx.cleanup()
            logger.error(f"Failed to prepare restore temp dir: {e}")
            return RestoreResult(success=False, errors=[f"Temp copy failed: {e}"])

        # ── Pre-restore safety backup (skip when retrying failed files) ───────
        is_retry = only_files is not None
        if not is_retry and entry.save_paths:
            from core.registry_saves import is_registry_path as _is_reg
            from core.registry_saves import registry_key_exists as _reg_exists
            valid = [p for p in entry.save_paths
                     if (_reg_exists(p) if _is_reg(p) else Path(p).exists())]
            # What is about to be overwritten is the redirect target, not the
            # folder the files were copied from — so that is what needs the
            # safety copy.
            for _pairs in redirects.values():
                for _parts, _dest in _pairs:
                    try:
                        if _dest.exists() and str(_dest) not in valid:
                            valid.append(str(_dest))
                    except OSError:
                        pass
            if valid:
                _exe = ""
                try:
                    from core.library import get_library
                    _game = get_library().get_by_id(entry.game_id)
                    if _game:
                        _exe = _game.exe_path
                except Exception:
                    pass
                _cfn = _game.computed_folder_name if _game else None
                safety = self.create_backup(
                    entry.game_id, entry.game_name, valid,
                    exe_path=_exe,
                    note=i18n.t('backup.pre_restore_safety'), force=True,
                    computed_folder_name=_cfn,
                )
                if not safety:
                    logger.warning(f"Pre-restore safety backup failed for '{entry.game_name}' — proceeding with restore")

        zip_path = _restore_zip
        if not zip_path.exists():
            # Let the finally block handle cleanup via _restore_tmp_ctx
            return RestoreResult(success=False, errors=["Backup zip removed"])

        result = RestoreResult(success=True)
        frozen = False

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # ── Registry state first (independent of filesystem targets) ──
                # __registry__/ members are re-imported into HKCU (validated
                # inside import_registry_tree) and EXCLUDED from the file
                # extraction below.
                from core.registry_saves import (arc_name_is_registry,
                                                 is_registry_path as _is_reg,
                                                 import_registry_tree,
                                                 export_registry_key,
                                                 registry_arc_name)
                reg_members = [a for a in zf.namelist() if arc_name_is_registry(a)]
                if reg_members:
                    reg_targets = {registry_arc_name(rp): rp
                                   for rp in entry.save_paths if _is_reg(rp)}
                    for arc in reg_members:
                        if only_files is not None and arc not in only_files:
                            continue
                        target = reg_targets.get(arc.replace("\\", "/"))
                        if target is None:
                            # Older/foreign entry without the virtual path
                            # recorded: fall back to the payload's own key
                            # (import re-validates it against the same
                            # HKCU\Software gate either way).
                            try:
                                import json as _json
                                _doc = _json.loads(zf.read(arc))
                                target = "registry:" + str(_doc.get("key", ""))
                            except Exception:
                                result.failed.append(arc)
                                result.errors.append(f"{arc}: unreadable registry payload")
                                continue
                        data = zf.read(arc)
                        try:
                            if export_registry_key(target) == data:
                                result.skipped.append(arc)   # already identical
                                continue
                        except Exception:
                            pass
                        if import_registry_tree(target, data):
                            result.restored.append(arc)
                        else:
                            result.failed.append(arc)
                            result.errors.append(f"{arc}: registry import failed")

                fs_save_paths = [p for p in entry.save_paths if not _is_reg(p)]
                if not fs_save_paths:
                    fallback = BACKUP_DIR / f"restored_{backup_id}"
                    _fallback_files = [a for a in zf.namelist()
                                       if not a.endswith('/')
                                       and not arc_name_is_registry(a)]
                    if _fallback_files:
                        fallback.mkdir(parents=True, exist_ok=True)
                        result.used_fallback_dir = str(fallback)
                        for arc_name in _fallback_files:
                            dest = fallback / arc_name
                            if not _is_relative_to(dest.resolve(), fallback.resolve()):
                                continue
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            self._write_restore_file(dest, zf.read(arc_name), arc_name, result)
                        logger.warning(f"No save_paths — restored to {fallback}")
                else:
                    # ── Build work list ───────────────────────────────────
                    save_parents = [Path(sp).parent for sp in fs_save_paths]
                    # Build lookup: folder name → parent, preferring the first
                    # save_path whose final component matches the archive root.
                    # Use (name, full_path) to disambiguate when multiple save
                    # paths share the same folder name.
                    _sp_lookup: dict[str, Path] = {}
                    for sp, parent in zip(fs_save_paths, save_parents):
                        sp_name = Path(sp).name
                        if sp_name not in _sp_lookup:
                            _sp_lookup[sp_name] = parent

                    # Cross-machine rename remap target: archive roots carry
                    # the save folder's name AT BACKUP TIME. When resolution
                    # redirected the restore to a folder with a DIFFERENT
                    # name (re-detected here, or taken from the library),
                    # no lookup entry matches — extracting via the fallback
                    # parent would fabricate the OLD folder name next to
                    # the real one. With exactly one directory save_path
                    # the mapping is unambiguous: strip the stale root and
                    # extract INTO that directory instead.
                    _single_dir_target: Optional[Path] = None
                    if len(fs_save_paths) == 1:
                        _sp0 = Path(fs_save_paths[0])
                        if _sp0.is_dir() or (not _sp0.exists() and not _sp0.suffix):
                            _single_dir_target = _sp0

                    work: list[tuple[Path, bytes, str]] = []

                    for arc_name in zf.namelist():
                        if arc_name.endswith('/'):
                            continue
                        if arc_name_is_registry(arc_name):
                            continue    # already handled above
                        # If retrying, only process the specified files
                        if only_files is not None and arc_name not in only_files:
                            continue

                        # Sanitize arc_name to prevent zip slip (path traversal)
                        arc_name_clean = Path(arc_name).as_posix()
                        if ('..' in arc_name_clean.split('/')
                                or arc_name_clean.startswith('/')
                                or (len(arc_name_clean) >= 2 and arc_name_clean[1] == ':')):
                            logger.warning(f"Skipping suspicious archive entry: {arc_name}")
                            continue
                        arc_path = Path(arc_name_clean)
                        # Match the archive's top-level directory to the correct
                        # save_path parent.  Falls back to the first parent.
                        restore_root = save_parents[0]
                        dest = None
                        if arc_path.parts:
                            # The chain decides first: these files describe a
                            # destination, and the copy they came from is not it.
                            _rest = arc_path.parts[1:]
                            for _cparts, _target in redirects.get(arc_path.parts[0], ()):
                                _head = [p.casefold() for p in _rest[:len(_cparts)]]
                                if _head == [c.casefold() for c in _cparts] \
                                        and len(_rest) > len(_cparts):
                                    restore_root = _target
                                    dest = _target.joinpath(*_rest[len(_cparts):])
                                    break
                        if dest is None and arc_path.parts:
                            matched_parent = _sp_lookup.get(arc_path.parts[0])
                            if matched_parent is not None:
                                restore_root = matched_parent
                            elif _single_dir_target is not None and len(arc_path.parts) > 1:
                                # Renamed-folder remap (see _single_dir_target
                                # above): drop the stale root component and
                                # land inside the resolved directory.
                                restore_root = _single_dir_target
                                dest = _single_dir_target / Path(*arc_path.parts[1:])
                        if dest is None:
                            dest = restore_root / arc_path
                        if not _is_relative_to(dest.resolve(), restore_root.resolve()):
                            continue

                        backup_data = zf.read(arc_name)

                        # Skip identical files
                        if dest.exists():
                            try:
                                if self._content_hash(dest.read_bytes()) == self._content_hash(backup_data):
                                    result.skipped.append(arc_name)
                                    continue
                            except OSError:
                                pass

                        work.append((dest, backup_data, arc_name))

                    if not work:
                        result.success = True
                        self.backup_restored.emit(entry.game_id)
                        return result

                    # ── Freeze if requested ───────────────────────────────
                    if freeze_pid > 0:
                        frozen = _suspend_process(freeze_pid)
                        result.process_frozen = frozen
                        if frozen:
                            logger.info(f"Suspended process {freeze_pid}")

                    # ── Write each file independently ─────────────────────
                    for dest, backup_data, arc_name in work:
                        self._write_restore_file(dest, backup_data, arc_name, result)

            if result.failed:
                result.success = False
                logger.warning(
                    f"Restore partial for {backup_id}: "
                    f"{len(result.restored)} ok, {len(result.failed)} failed"
                )
            else:
                logger.info(
                    f"Restore complete for {backup_id}: "
                    f"{len(result.restored)} written, {len(result.skipped)} skipped"
                )

            self.backup_restored.emit(entry.game_id)
            return result

        except zipfile.BadZipFile:
            return RestoreResult(success=False, errors=["Corrupt backup zip"])
        except Exception as e:
            logger.error(f"Restore failed: {e}")
            return RestoreResult(success=False, errors=[str(e)])
        finally:
            if frozen and freeze_pid > 0:
                if _resume_process(freeze_pid):
                    logger.info(f"Resumed process {freeze_pid}")
                else:
                    logger.warning(f"Process {freeze_pid} no longer exists (crashed or killed during restore)")
            try:
                _restore_tmp_ctx.cleanup()
            except Exception:
                pass

    def _write_restore_file(self, dest: Path, data: bytes, arc_name: str,
                            result: RestoreResult):
        """Atomic write of one file.  Records outcome in *result*; never raises."""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_suffix(dest.suffix + ".restore_tmp")
            try:
                tmp_dest.write_bytes(data)
                tmp_dest.replace(dest)
                result.restored.append(arc_name)
            except PermissionError as e:
                if tmp_dest.exists():
                    try: tmp_dest.unlink()
                    except OSError: pass
                result.failed.append(arc_name)
                result.errors.append(f"{arc_name}: {e}")
            except Exception as e:
                if tmp_dest.exists():
                    try: tmp_dest.unlink()
                    except OSError: pass
                result.failed.append(arc_name)
                result.errors.append(f"{arc_name}: {e}")
        except Exception as e:
            result.failed.append(arc_name)
            result.errors.append(f"{arc_name}: {e}")

    def _migrate_old_backup_folders(
        self,
        game_id: str,
        current_folder: str,
        name_history: list[str],
        exe_path: str = "",
    ) -> None:
        """Move backup zips from old-name subdirs into *current_folder*.

        Called whenever the game's computed_folder_name changes (rename).
        Skips any folder that IS the current folder or that doesn't exist.
        Updates zip_path in all in-memory index entries that were moved.
        """
        from core.constants import get_folder_name_for_save
        current_dir = BACKUP_DIR / current_folder
        seen_old: set[str] = set()

        # Candidate old folders: names reconstructed from display-name history,
        # PLUS actual past folder names (folder_history) which preserve any
        # disambiguation suffix a display name can't reproduce.
        candidate_folders = [get_folder_name_for_save(n, exe_path, game_id) for n in name_history]
        try:
            from core.library import get_library as _gl
            _entry = _gl().get_by_id(game_id)
            if _entry is not None:
                candidate_folders += list(_entry.folder_history or [])
        except Exception:
            pass

        for old_folder in candidate_folders:
            if not old_folder or old_folder == current_folder or old_folder in seen_old:
                continue
            seen_old.add(old_folder)
            # Skip folders still owned by a different live game. A disambiguated
            # game (folder "Foo_2") keeps the un-suffixed base ("Foo") in its
            # name_history, but "Foo" may be another game's active folder. The
            # zip glob below is game_id-scoped so nothing is stolen, but this
            # also avoids the wasteful scan and any cleanup on a shared dir.
            try:
                from core.library import get_library
                if get_library().folder_name_in_use_by_other(old_folder, game_id):
                    continue
            except Exception:
                pass
            old_dir = BACKUP_DIR / old_folder
            if not old_dir.is_dir():
                continue

            # Move every zip belonging to this game_id
            for zip_file in list(old_dir.glob(f"{game_id}_*.zip")):
                dest = current_dir / zip_file.name
                try:
                    current_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(zip_file), str(dest))
                    logger.info(f"Migrated backup {zip_file.name}: {old_folder} → {current_folder}")
                    # Patch in-memory index
                    with _index_lock:
                        for entry in self._index:
                            if Path(entry.zip_path).name == zip_file.name:
                                entry.zip_path = str(dest)
                except Exception as e:
                    logger.warning(f"Failed to migrate {zip_file}: {e}")

            # Also move index.json if the old folder only contained this game
            old_index = old_dir / "index.json"
            if old_index.exists():
                try:
                    remaining_zips = list(old_dir.glob("*.zip"))
                    if not remaining_zips:
                        old_index.unlink(missing_ok=True)
                        if not any(old_dir.iterdir()):
                            old_dir.rmdir()
                except Exception:
                    pass

        # Persist updated paths
        self._save_index()

    def delete_backup(self, backup_id: str) -> bool:
        entry = self.get_backup(backup_id)
        if not entry:
            return False
        game_id = entry.game_id
        folder_hint = self._game_folder_for_entry(entry)
        try:
            p = Path(entry.zip_path)
            if p.exists():
                p.unlink()
        except Exception:
            pass
        with _index_lock:
            self._index = [b for b in self._index if b.backup_id != backup_id]
        self._save_game_index(game_id, _folder_hint=folder_hint)
        self._record_deleted(game_id, [backup_id])
        self.backup_deleted.emit(backup_id)
        return True

    def _mark_synced(self, backup_id: str, provider_id: str):
        """Mark a backup as synced to a provider (updates the original in _index)."""
        game_id = None
        with _index_lock:
            for entry in self._index:
                if entry.backup_id == backup_id:
                    synced = entry.cloud_metadata.get("synced_to", [])
                    if provider_id not in synced:
                        synced.append(provider_id)
                        entry.cloud_metadata["synced_to"] = synced
                    game_id = entry.game_id
                    break
        # Write to disk outside the lock to reduce contention
        if game_id is not None:
            self._save_game_index(game_id)

    def resolve_pre_confirmation_backups(self, game_id: str,
                                         discarded_paths: list[str] = None,
                                         note: str = "",
                                         promote_rest: bool = True) -> tuple[int, int]:
        """Resolve every temporary (pre-confirmation) backup of *game_id*
        after a confirmation round — per PATH, not all-or-nothing for the
        whole game.

        A single confirmation round can accept some detected paths and
        delete others at the same time (the auto-scan dialog lets the user
        pick per path). Blanket-promoting every pre-confirmation backup in
        that case would keep session backups for paths the user just
        rejected; blanket-discarding would just as wrongly throw away
        backups for paths the user kept. So each backup is judged by which
        paths it actually covers (BackupEntry.save_paths): a backup that
        covers ANY path in *discarded_paths* is discarded (per the
        confirmation contract — a rejected detection's session backups go
        with it); every other pre-confirmation backup for this game is
        promoted to definitive history, same as promote_pre_confirmation_backups.

        With *promote_rest* False the non-discarded backups are left
        provisional (only the ones covering *discarded_paths* are dropped) —
        used when rows are deleted outside a full confirmation, so the
        surviving detections stay pending until the user actually confirms them.

        Returns (promoted_count, discarded_count).
        """
        discarded_set = set(discarded_paths or [])
        to_discard: list[str] = []
        promoted = 0
        with _index_lock:
            for entry in self._index:
                if entry.game_id != game_id:
                    continue
                if not (entry.cloud_metadata or {}).get("pre_confirmation"):
                    continue
                if discarded_set and discarded_set.intersection(entry.save_paths or []):
                    to_discard.append(entry.backup_id)
                elif promote_rest:
                    entry.cloud_metadata.pop("pre_confirmation", None)
                    if note:
                        entry.note = note
                    promoted += 1
        if promoted:
            self._save_game_index(game_id)
            self._enforce_limits(game_id)
        for bid in to_discard:
            self.delete_backup(bid)
        if promoted or to_discard:
            logger.info(
                f"Resolved pre-confirmation backups for game {game_id}: "
                f"{promoted} promoted, {len(to_discard)} discarded"
            )
        return promoted, len(to_discard)

    def promote_pre_confirmation_backups(self, game_id: str,
                                         note: str = "") -> int:
        """Turn every temporary (pre-confirmation) backup of *game_id* into a
        definitive one: the user just confirmed the auto-detected save paths,
        so the session backups protecting them are now regular history.

        *note*, when given, replaces the backups' provisional note so the UI
        stops labelling them as pending. Rotation limits are re-enforced
        afterwards; promoted backups become eligible for cloud upload on the
        next sync. Returns the number of promoted entries.
        """
        promoted = 0
        with _index_lock:
            for entry in self._index:
                if entry.game_id != game_id:
                    continue
                if (entry.cloud_metadata or {}).get("pre_confirmation"):
                    entry.cloud_metadata.pop("pre_confirmation", None)
                    if note:
                        entry.note = note
                    promoted += 1
        if promoted:
            self._save_game_index(game_id)
            self._enforce_limits(game_id)
            logger.info(
                f"Promoted {promoted} pre-confirmation backup(s) for game {game_id}"
            )
        return promoted

    def discard_pre_confirmation_backups(self, game_id: str) -> int:
        """Delete every temporary (pre-confirmation) backup of *game_id* —
        the auto-detected paths they covered were rejected or suppressed,
        so per the confirmation contract their session backups go with them.
        Definitive backups are never touched. Returns the number deleted.
        """
        with _index_lock:
            temp_ids = [
                b.backup_id for b in self._index
                if b.game_id == game_id
                and (b.cloud_metadata or {}).get("pre_confirmation")
            ]
        for bid in temp_ids:
            self.delete_backup(bid)
        if temp_ids:
            logger.info(
                f"Discarded {len(temp_ids)} pre-confirmation backup(s) for game {game_id}"
            )
        return len(temp_ids)

    def adopt_backups(self, from_game_id: str, to_game_id: str,
                      to_game_name: str, to_exe_path: str = "",
                      to_folder_name: str = "") -> int:
        """Re-file every backup of *from_game_id* under *to_game_id*.

        A save folder registered by hand starts as a placeholder entry: real
        saves, but no game behind them. Whatever was backed up in the meantime
        belongs to that placeholder's id and its own storage folder — history
        stranded on a stub. When the game itself turns up, this moves those
        backups across so they become that game's own history: the zips are
        relocated into its folder, the entries re-stamped, and both per-folder
        indexes rewritten.

        Deliberately a MOVE, not a copy: two indexes claiming the same
        backup_id would resurface it as a duplicate on the next scan.

        Returns how many backups were re-filed.
        """
        if not from_game_id or not to_game_id or from_game_id == to_game_id:
            return 0
        with _index_lock:
            moving = [b for b in self._index if b.game_id == from_game_id]
            old_folder = self._game_folder_for_entry(moving[0]) if moving else ""
        if not moving:
            return 0

        target_folder = to_folder_name or get_install_folder_name(
            to_exe_path or "", to_game_name, to_game_id, to_folder_name or None)
        dest_dir = BACKUP_DIR / target_folder
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot prepare {dest_dir} to adopt backups: {e}")
            return 0

        moved = 0
        for entry in moving:
            src = Path(entry.zip_path)
            dest = dest_dir / src.name
            try:
                if src.exists() and src.resolve() != dest.resolve():
                    if dest.exists():
                        # Same backup_id already there — keep the existing file
                        # and drop the duplicate rather than overwrite it.
                        src.unlink(missing_ok=True)
                    else:
                        shutil.move(str(src), str(dest))
            except Exception as e:
                logger.warning(f"Could not move {src} into {dest_dir}: {e}")
                continue
            with _index_lock:
                for b in self._index:
                    if b.backup_id == entry.backup_id:
                        b.game_id = to_game_id
                        b.game_name = to_game_name or b.game_name
                        b.zip_path = str(dest)
            moved += 1

        if moved:
            self._save_game_index(to_game_id)
            # Only clean up the OLD folder when it really is a different one.
            # When the game reclaimed the placeholder's folder name the two
            # coincide, and the cleanup branch would delete the index that was
            # just written there — the backups would survive in memory and
            # vanish on the next start.
            if old_folder and old_folder != target_folder:
                self._save_game_index(from_game_id, _folder_hint=old_folder)
            logger.info(f"Adopted {moved} backup(s) from placeholder {from_game_id} "
                        f"into {to_game_name!r} ({to_game_id})")
        return moved

    def get_backups_for_game(self, game_id: str) -> list[BackupEntry]:
        with _index_lock:
            snapshot = [copy.deepcopy(b) for b in self._index if b.game_id == game_id]
        return sorted(
            snapshot,
            key=lambda b: b.created_dt,
            reverse=True,
        )

    def get_backups_for_folder(self, folder_name: str) -> list[BackupEntry]:
        """Return backups whose stable storage FOLDER (name-derived) matches
        *folder_name*, regardless of game_id. Belt-and-suspenders lookup for
        cross-PC backups that may still be filed under another machine's
        game_id (import_backup normally migrates these, but this covers any
        entry that hasn't been re-imported yet)."""
        if not folder_name:
            return []
        with _index_lock:
            snapshot = [copy.deepcopy(b) for b in self._index
                        if self._game_folder_for_entry(b) == folder_name]
        return sorted(snapshot, key=lambda b: b.created_dt, reverse=True)

    def get_all_backups(self) -> list[BackupEntry]:
        """Return entire index (flat). Use for batch counting — no disk I/O."""
        with _index_lock:
            return [copy.deepcopy(b) for b in self._index]

    def get_backup(self, backup_id: str) -> Optional[BackupEntry]:
        with _index_lock:
            entry = next((b for b in self._index if b.backup_id == backup_id), None)
            return copy.deepcopy(entry) if entry else None

    @staticmethod
    def compute_deletions(
        entries: list[BackupEntry],
        max_backups: int,
        retention_days: int,
        min_kept: int,
    ) -> set[str]:
        """Determine which backup IDs should be deleted to respect limits.

        Reusable for both local and cloud enforcement.  *entries* must all
        belong to the same game.  Returns a set of backup_ids to remove.
        """
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        game_backups = sorted(entries, key=lambda b: b.created_dt)
        to_delete: set[str] = set()

        # Step 1: expired by age
        for b in game_backups:
            if b.created_dt < cutoff:
                to_delete.add(b.backup_id)

        # Protect newest min_kept
        if min_kept > 0:
            newest = game_backups[-min_kept:] if len(game_backups) >= min_kept else game_backups
            to_delete -= {b.backup_id for b in newest}

        # Step 2: trim by count
        remaining = [b for b in game_backups if b.backup_id not in to_delete]
        while len(remaining) > max_backups:
            oldest = remaining.pop(0)
            to_delete.add(oldest.backup_id)

        return to_delete

    def _enforce_limits(self, game_id: str):
        """Remove oldest backups when limits are exceeded.
        Always keeps at least `min_kept_backups` most recent backups regardless
        of age, so the user never loses all history.
        """
        config = get_config()
        max_backups    = config.get("max_local_backups",    MAX_LOCAL_BACKUPS)
        retention_days = config.get("backup_retention_days", BACKUP_RETENTION_DAYS)
        min_kept       = config.get("min_kept_backups",     MIN_KEPT_BACKUPS)

        # Hold _index_lock for the entire read-modify cycle to prevent races
        zip_paths_to_delete: list[str] = []
        with _index_lock:
            game_backups = [b for b in self._index if b.game_id == game_id]
            to_delete = self.compute_deletions(game_backups, max_backups, retention_days, min_kept)

            if not to_delete:
                return

            # Collect zip paths before removing from index
            for b in game_backups:
                if b.backup_id in to_delete:
                    zip_paths_to_delete.append(b.zip_path)

            # Remove from index while still holding the lock
            self._index = [b for b in self._index if b.backup_id not in to_delete]

        # Disk I/O and signals outside the lock
        parents_to_check: set[Path] = set()
        for zp in zip_paths_to_delete:
            try:
                p = Path(zp)
                if p.exists():
                    parents_to_check.add(p.parent)
                    p.unlink()
            except Exception:
                pass
        # Tombstone pruned ids so sync never re-downloads what the local
        # retention limits just removed (download→prune→download loop).
        self._record_deleted(game_id, to_delete)
        for bid in to_delete:
            self.backup_deleted.emit(bid)
        # Clean up empty game subfolders (keep if index.json remains)
        for parent in parents_to_check:
            try:
                if parent != BACKUP_DIR and parent.exists():
                    remaining = [f for f in parent.iterdir() if f.name != "index.json"]
                    if not remaining:
                        (parent / "index.json").unlink(missing_ok=True)
                        parent.rmdir()
            except Exception:
                pass

        self._save_game_index(game_id)


_backup_mgr: BackupManager | None = None
_backup_lock = threading.Lock()


def get_backup_manager() -> BackupManager:
    global _backup_mgr
    if _backup_mgr is None:
        with _backup_lock:
            if _backup_mgr is None:
                _backup_mgr = BackupManager()
    return _backup_mgr