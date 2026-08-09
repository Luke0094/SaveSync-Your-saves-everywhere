"""
SaveSync - Filesystem Watcher
Watches game save directories in real-time. If a path doesn't exist yet,
watches the nearest existing parent and triggers once the expected folder
is created — then switches to watching the folder itself.
"""
import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, List, Dict, Set, Optional

from PySide6.QtCore import QObject, Signal, Slot
from core.constants import SAVE_FOLDER_HINTS as _DEFAULT_HINTS, SKIP_FILENAME_STEMS, strip_version_tokens

logger = logging.getLogger(__name__)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog not installed - real-time save detection disabled")

from core import is_relative_to as _is_relative_to_compat


_DEBOUNCE_SEC = 3.0
_MAX_CACHE_SIZE = 50000  # Cap on _BACKED_UP_FILES / _KNOWN_FILES to prevent memory leaks

# Words too generic to identify WHICH game a common-root (AppData/…) event
# belongs to — excluded from the broadened install-identity filter in
# _CommonRootSaveHandler so a term like "game" or "save" can't match every
# other app's data. (A game's own NAME words are still used as-is.)
_GENERIC_IDENTITY_TERMS = frozenset({
    "game", "games", "data", "save", "saves", "savedata", "app", "application",
    "launcher", "start", "main", "run", "play", "player", "client", "engine",
    "bin", "win", "win32", "win64", "x86", "x64", "release", "debug", "build",
    "dist", "content", "assets", "www", "temp", "cache", "common", "default",
})


class _BoundedSet:
    """Set-like container with a max size and FIFO eviction."""

    def __init__(self, maxsize: int = _MAX_CACHE_SIZE):
        self._maxsize = maxsize
        self._data: OrderedDict[str, None] = OrderedDict()

    def add(self, item: str) -> None:
        if item in self._data:
            return
        self._data[item] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def discard(self, item: str) -> None:
        self._data.pop(item, None)

    def difference_update(self, other) -> None:
        for item in other:
            self._data.pop(item, None)

    def __contains__(self, item: str) -> bool:
        return item in self._data

    def __iter__(self):
        return iter(self._data)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def _is_subpath(file_key: str, parent: Path) -> bool:
    """Check if file_key is under parent directory."""
    try:
        return _is_relative_to_compat(Path(file_key).resolve(), parent)
    except (ValueError, OSError):
        return False

# File extensions to ignore (not save files)
_IGNORE_EXTENSIONS = frozenset({
    ".tmp", ".temp", ".log", ".cache", ".bak", ".old",
    ".exe", ".dll", ".so", ".dylib",  # Executables/libraries
    ".mp3", ".wav", ".ogg", ".flac",  # Audio files
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tga",  # Images
    ".mp4", ".avi", ".mkv", ".webm",  # Videos
    ".pdf", ".txt", ".md", ".rtf",  # Documents
    ".zip", ".rar", ".7z", ".tar", ".gz",  # Archives
    ".ttf", ".otf", ".woff", ".woff2",  # Fonts
    ".shader", ".fx", ".hlsl", ".glsl",  # Shaders
    ".info", ".html", ".htm", ".dat", ".bin",  # Detection-excluded formats
})


def _is_ignored_file(path: Path) -> bool:
    """True if *path* must never be treated as a save-file candidate by live
    tracking — either its extension is in _IGNORE_EXTENSIONS or its filename
    stem (without extension) matches SKIP_FILENAME_STEMS (e.g. "log"/"logs",
    whatever the extension — "log.dat", "log.json", extension-less "log")."""
    return path.suffix.lower() in _IGNORE_EXTENSIONS or path.stem.lower() in SKIP_FILENAME_STEMS

# Maximum file size for save files (100MB - reasonable for save files)
_MAX_SAVE_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# Lock for thread-safe access to module-level caches
_CACHE_LOCK = threading.Lock()

# Persistent memory of files that have been backed up (never forget)
_BACKED_UP_FILES = _BoundedSet()    # Files that have been backed up at least once
# Cache for pending files waiting to be backed up, keyed by game_id
_PENDING_FILES: dict[str, set[str]] = {}    # game_id → set of file paths
# Cache for known files (existing files at startup)
_KNOWN_FILES = _BoundedSet()         # Files that existed when monitoring started
# Index of known files grouped by parent directory for O(1) lookup.
# Maps directory path (str) → set of full file paths (str).
_KNOWN_FILES_BY_DIR: Dict[str, Set[str]] = {}
# Pattern database for save file naming conventions
_SAVE_PATTERNS: Dict[str, List[re.Pattern]] = {}  # game_id → list of patterns
_MAX_PATTERN_ENTRIES = 500
# Seed patterns discovered through file movement/creation
_SEED_PATTERNS: Dict[str, Set[str]] = {}  # directory_path → set of patterns
# Files discovered as save files through movement/creation
_DISCOVERED_SAVE_FILES = _BoundedSet()

# ── Temporal correlation (unattributed → claimed) ───────────────────────────
# A game may write a save to a profile folder whose name shares NOTHING with
# its library identity (e.g. Ren'Py's Roaming/RenPy/<save_directory> uses the
# INTERNAL title). The name pre-filter of _CommonRootSaveHandler rightly
# drops such events for attribution — but instead of discarding them, they
# are buffered here and CLAIMED for a game when an attributed save event of
# that game lands within a tight window of the same instant: the (near-)
# simultaneous double-write IS the association. The window is deliberately
# narrow — a same-game double write is milliseconds apart, while two
# unrelated processes writing save-like files in the same couple of seconds
# is rare; save-LIKE events get the full window, weaker candidates a
# stricter one.
from collections import deque as _deque
_UNATTRIBUTED_EVENTS: "_deque" = _deque(maxlen=64)   # (ts, file_key, strong)
_LAST_ANCHOR_TS: Dict[str, float] = {}               # game_id → last attributed ts
_CLAIMED_EVENTS = _BoundedSet()                      # file_key già reclamati
_CORR_DEFAULT_WINDOW_MS = 1000
_CORR_WEAK_RATIO = 0.4   # weaker candidates get a stricter slice of the window
_CORR_WEAK_CAP = 2       # max weak claims per anchor sweep

# Only a real WRITE moves the correlation clock. This has to be enforced
# explicitly, twice over, because neither check is sufficient alone:
#
#  - by event type, because a deletion or a rename away is not "the game
#    saved" and must not open a window;
#  - by mtime, because watchdog on Windows subscribes ReadDirectoryChangesW
#    to LAST_ACCESS, ATTRIBUTES and SECURITY as well as LAST_WRITE, and maps
#    every one of them onto FileModifiedEvent. A plain read of a save file,
#    an antivirus scan or an archive-bit flip is therefore indistinguishable
#    from the game writing — unless the file's modification time is checked
#    to have actually moved.
_CORR_WRITE_EVENT_TYPES = frozenset({"created", "modified", "moved"})

# Floor for the mtime freshness probe. Deliberately NOT the correlation
# window: this only answers "was this event backed by a real write?", while
# the window answers "how close to the anchor?". FAT32/exFAT store mtimes at
# 2-second granularity, so anything tighter would reject genuine writes on
# removable drives.
_CORR_MTIME_TOLERANCE_S = 2.0

_CORR_SETTINGS_CACHE: dict = {"ts": 0.0, "enabled": False,
                              "strong_s": _CORR_DEFAULT_WINDOW_MS / 1000.0,
                              "weak_s": _CORR_DEFAULT_WINDOW_MS / 1000.0 * _CORR_WEAK_RATIO}
_CORR_SETTINGS_TTL = 5.0


def correlation_settings() -> tuple:
    """(enabled, strong_window_s, weak_window_s) from the user's settings.

    Cached for a few seconds: this is consulted on every filesystem event,
    and a config read per event would put lock traffic on the watchdog
    thread. The TTL is short enough that toggling the option in Settings
    takes effect while the game is still running.
    """
    import time as _time
    now = _time.time()
    if now - _CORR_SETTINGS_CACHE["ts"] > _CORR_SETTINGS_TTL:
        enabled = False
        window_ms = _CORR_DEFAULT_WINDOW_MS
        try:
            from core.config_manager import get_config
            cfg = get_config()
            enabled = bool(cfg.get("save_correlation_enabled", False))
            window_ms = int(cfg.get("save_correlation_window_ms",
                                    _CORR_DEFAULT_WINDOW_MS))
        except Exception:
            pass
        strong = max(0.05, window_ms / 1000.0)
        _CORR_SETTINGS_CACHE.update({
            "ts": now, "enabled": enabled,
            "strong_s": strong, "weak_s": strong * _CORR_WEAK_RATIO,
        })
    return (_CORR_SETTINGS_CACHE["enabled"], _CORR_SETTINGS_CACHE["strong_s"],
            _CORR_SETTINGS_CACHE["weak_s"])


def _is_write_event(event) -> bool:
    """True for create/modify/move — the events that mean "this file changed".

    Excludes deletions and the read-only open/close events inotify emits.
    """
    return getattr(event, "event_type", "") in _CORR_WRITE_EVENT_TYPES


def _has_fresh_write(src_path: str, now: float) -> bool:
    """True when *src_path* carries a modification time that just moved.

    The second half of the write test above. A file that vanished (a delete
    slipping through as a modify) fails here too, which is what stops the
    size-check OSError fallthrough in _unattributed_savelike_strength from
    scoring a deleted .sav as a strong candidate.
    """
    try:
        return (now - Path(src_path).stat().st_mtime) <= _CORR_MTIME_TOLERANCE_S
    except (OSError, ValueError):
        return False


_CORR_SKIP_DIR_NAMES: Set[str] = set()


def _corr_skip_dir_names() -> Set[str]:
    """Noise directory names (browsers, launchers, caches…) — lazy union of
    the detector and backup skip sets, resolved once at first use to avoid
    import-order coupling."""
    if not _CORR_SKIP_DIR_NAMES:
        try:
            from core.save_detector import _SKIP_DIRS
            from core.backup import _BACKUP_SKIP_DIRS
            _CORR_SKIP_DIR_NAMES.update(_SKIP_DIRS)
            _CORR_SKIP_DIR_NAMES.update(_BACKUP_SKIP_DIRS)
        except Exception:
            _CORR_SKIP_DIR_NAMES.update({"cache", "logs", "temp"})
    return _CORR_SKIP_DIR_NAMES


_EXCL_TOKENS_CACHE: dict = {"ts": 0.0, "tokens": set(), "user_tokens": set()}

# Common words that must NEVER become token evidence via a user-ignored
# process: ignoring "Game.exe" or "Launcher.exe" is legitimate, but the
# word must not veto every "GameData"/"LauncherStory" folder fragment.
# (Whole-component matches are unaffected — a folder literally named
# "game" still matches the full vocabulary.)
_USER_TOKEN_GUARD = frozenset({
    "game", "games", "launcher", "launch", "setup", "update", "updater",
    "install", "installer", "uninstall", "client", "server", "service",
    "services", "host", "tool", "tools", "app", "apps", "main", "start",
    "starter", "play", "player", "engine", "system", "windows", "save",
    "saves", "data", "config", "settings", "runtime", "helper", "web",
    "online", "cloud", "sync", "backup", "temp", "test", "demo", "beta",
    "alpha", "shell", "steam", "origin",
})


def _excluded_process_tokens() -> Set[str]:
    """Exclusion vocabulary for correlation candidates: the skip-dir names,
    the monitor's hardcoded system/updater/antivirus process stems, and the
    user's own ignored_processes from settings (stems). Cached 30s so the
    config read doesn't run per-event. The cache also keeps the SUBSET of
    user stems that qualify as token evidence (see _user_token_stems)."""
    import time as _time
    now = _time.time()
    if now - _EXCL_TOKENS_CACHE["ts"] > 30.0:
        tokens: Set[str] = set(_corr_skip_dir_names())
        user_tokens: Set[str] = set()
        try:
            from core.monitor import _SYSTEM_STEMS
            tokens.update(_SYSTEM_STEMS)
        except Exception:
            pass
        try:
            from core.config_manager import get_config
            for entry in get_config().get("ignored_processes", []):
                e = str(entry).strip()
                if not e:
                    continue
                if "/" in e or "\\" in e:
                    e = Path(e).stem
                stem = Path(e).stem.lower()
                if stem:
                    tokens.add(stem)
                    # Token-level evidence only for DISTINCTIVE user stems:
                    # the WHOLE stem (never its fragments), long enough to
                    # be a name and not a guarded common word.
                    if len(stem) >= 5 and stem not in _USER_TOKEN_GUARD \
                            and not stem.isdigit():
                        user_tokens.add(stem)
        except Exception:
            pass
        _EXCL_TOKENS_CACHE["tokens"] = tokens
        _EXCL_TOKENS_CACHE["user_tokens"] = user_tokens
        _EXCL_TOKENS_CACHE["ts"] = now
    return _EXCL_TOKENS_CACHE["tokens"]


def _user_token_stems() -> Set[str]:
    """User-ignored process stems that may act as token evidence — filled
    by _excluded_process_tokens() (same 30s cache)."""
    _excluded_process_tokens()
    return _EXCL_TOKENS_CACHE["user_tokens"]


def _component_tokens(name: str) -> Set[str]:
    """Identity tokens of one path component, resolved the same way generic
    exe stems are: CamelCase split + separator split, lowercased. This is
    what lets "BraveSoftware" / "Brave-Browser-Beta" match the excluded
    stem "brave" — the exact-component match alone never would."""
    from core.constants import CAMEL_SPLIT_RE
    out: Set[str] = {name.lower()}
    spaced = re.sub(CAMEL_SPLIT_RE, " ", name)
    for tok in re.split(r"[\s\-_.]+", spaced):
        tl = tok.lower()
        if len(tl) >= 4:            # short fragments ("e", "up") stay out
            out.add(tl)
    return out


# Token-level evidence is ALLOWLIST-based. The old approach used the full
# excluded-stems set minus a handful of "too generic" words — but any stem
# that happens to be an English word blocks legitimate games whose folder
# merely CONTAINS it: "SteamWorld Dig"→"steam", "Brave Little Toaster"→
# "brave", "Discord Times"→"discord", "System Shock"→"system"… A subtractive
# list can never keep up with that. Only DISTINCTIVE, non-dictionary
# vendor/product names may fire on a word-fragment of a folder name; every
# word-like stem (steam, brave, origin, discord, teams, slack, acrobat, …)
# is enforced at WHOLE-component level only (a folder literally named
# "Steam"/"Brave" is still the vendor dir and still blocks).
_TOKEN_BRAND_STEMS = frozenset({
    # Browsers / updaters
    "bravesoftware", "brave-browser", "msedge", "msedgewebview2",
    "msedgeupdate", "microsoftedgeupdate", "googleupdate", "crashpad_handler",
    # GPU vendors
    "nvsphelper64", "nvcontainer", "nvdisplay.container",
    "amdrsserv", "amddvr", "radeonsoft",
    # Launchers and their helpers
    "epicgameslauncher", "epicwebhelper", "epiconlineserviceshost",
    "epiconlineservicesuihelper", "epiconlineservicesinstallhelper",
    "goggalaxy", "eadesktop", "ubisoftconnect", "rockstargameslauncher",
    "bethesdalauncher", "itchio", "battlenet", "playnite",
    # Chat / media (distinct brand spellings only)
    "spotify", "telegram", "discordptb", "discordcanary",
    # OneDrive family (the bare "onedrive" is position-aware — see
    # _SYNC_ROOT_STEMS)
    "onedrive", "onedrivestandaloneupdater", "filecoauth",
    "microsoft.sharepoint",
    # Adobe services (NOT the word-like "acrobat")
    "acrord32", "acrotray", "acrocef", "adobecollabsync", "adobearm",
    "adobeipcbroker", "armsvc",
    "savesync",
})


# Sync-folder names that are ALSO excluded process stems. The OneDrive
# CLIENT is an excluded process, but the OneDrive SYNC FOLDER is a supported
# save location (SAVE_FOLDER_HINTS ships "{USERPROFILE}/OneDrive/Documents/
# My Games") — the stem must not veto every save synced there. The veto is
# kept POSITION-AWARE instead: outside AppData/ProgramData the component is
# the user's sync root ("OneDrive", "OneDrive - Company") and is safe;
# under AppData/ProgramData it is the client's own app dir and stays vetoed.
_SYNC_ROOT_STEMS = frozenset({"onedrive"})


def _path_matches_excluded_process(parent: Path) -> bool:
    """True when any component of *parent* resolves to an excluded
    process/vendor name — WHOLE-component matches use the full vocabulary
    (hardcoded system stems, skip dirs, the user's ignored_processes);
    TOKEN-level matches (word-fragments of CamelCase/compound components)
    only ever fire on the curated brand allowlist, so a game folder that
    shares a dictionary word with an ignored process can never be blocked.
    DISTINCTIVE user-ignored stems (≥5 chars, common words guarded — see
    _user_token_stems) match as a CONTIGUOUS substring of a component
    instead: CamelCase splitting would shred "SuperDuperTool" into common
    fragments, while the contiguous full stem inside "SuperDuperTool-Cache"
    is unambiguous — and it is a name the user explicitly chose to veto."""
    excl = _excluded_process_tokens()
    token_excl = excl & _TOKEN_BRAND_STEMS
    user_stems = _user_token_stems()
    lower = [c.lower() for c in parent.parts]
    in_app_data = any(c in ("appdata", "programdata") for c in lower)
    for comp, cl in zip(parent.parts, lower):
        if not in_app_data and any(cl == s or cl.startswith(s + " ")
                                   for s in _SYNC_ROOT_STEMS):
            continue          # sync root, not the client process
        if cl in excl:
            return True
        if any(us in cl for us in user_stems):
            return True
        toks = _component_tokens(comp) & token_excl
        if toks and not in_app_data:
            toks -= _SYNC_ROOT_STEMS
        if toks:
            return True
    return False


def _unattributed_savelike_strength(file_path: Path) -> int:
    """0 = ignore, 1 = weak candidate, 2 = save-like (strong).

    Context-free version of the _SaveHandler heuristics (no learned
    patterns — the whole point is that no game context matched)."""
    try:
        if file_path.suffix.lower() in _IGNORE_EXTENSIONS:
            return 0
        if file_path.stem.lower() in SKIP_FILENAME_STEMS:
            return 0
        # Excluded processes/vendors (hardcoded + user settings), resolved
        # at TOKEN level like generic exe stems: a Brave WebStorage write
        # half a second after a game save must never be claimed.
        if _path_matches_excluded_process(file_path.parent):
            return 0
        try:
            if file_path.stat().st_size > _MAX_SAVE_FILE_SIZE:
                return 0
        except OSError:
            pass
        if file_path.suffix.lower() in ('.sav', '.save', '.backup'):
            return 2
        if _matches_common_save_patterns(file_path):
            return 2
        try:
            from core.config_manager import get_config
            hints = get_config().get("save_folder_hints", _DEFAULT_HINTS)
        except Exception:
            hints = list(_DEFAULT_HINTS)
        parent_name = file_path.parent.name.lower()
        if any(h in parent_name for h in hints):
            return 2
        return 1
    except (OSError, ValueError):
        return 0


def _record_unattributed(src_path: str, game_id: str, inner_handler=None):
    """Buffer a name-filter-rejected event; claim it when this game's last
    ANCHOR falls within the correlation window.

    The anchor is any event on a game-scoped watch (install tree / known
    save paths) — INCLUDING files rejected as saves (logs, temp files):
    they still prove the game's process is writing at that instant, which
    is exactly the association we need. There is deliberately NO
    anchor-less claiming: a write with no game-linked anchor could belong
    to any process on the machine (an antivirus update, a background
    updater) and the filesystem event carries no writer identity, so
    "a game is running" alone is never enough evidence.

    Callers gate on the setting too, but the check is repeated here first so
    the opt-out holds for any caller and the disabled path costs no stat()."""
    import time as _time
    enabled, strong_s, weak_s = correlation_settings()
    if not enabled:
        return
    try:
        fp = Path(src_path)
    except (OSError, ValueError):
        return
    strength = _unattributed_savelike_strength(fp)
    if strength == 0:
        return
    now = _time.time()
    # The event said "modified"; the mtime has to agree. On Windows a read or
    # an attribute change is delivered as the very same event.
    if not _has_fresh_write(src_path, now):
        return
    file_key = str(fp)
    with _CACHE_LOCK:
        if file_key in _BACKED_UP_FILES or file_key in _CLAIMED_EVENTS:
            return
        _UNATTRIBUTED_EVENTS.append((now, file_key, strength == 2))
        anchor = _LAST_ANCHOR_TS.get(game_id, 0.0)
    window = strong_s if strength == 2 else weak_s
    if anchor and (now - anchor) <= window:
        _claim_event_for_game(game_id, file_key,
                              f"Δ={(now - anchor) * 1000:.0f}ms from anchor")
        if inner_handler is not None:
            try:
                inner_handler.schedule_fire()
            except Exception:
                pass


def _claim_event_for_game(game_id: str, file_key: str, reason: str):
    with _CACHE_LOCK:
        if file_key in _CLAIMED_EVENTS:
            return
        _CLAIMED_EVENTS.add(file_key)
        _PENDING_FILES.setdefault(game_id, set()).add(file_key)
        _DISCOVERED_SAVE_FILES.add(file_key)
    try:
        _add_seed_pattern(str(Path(file_key).parent), Path(file_key))
    except Exception:
        pass
    logger.info(f"Correlated save claimed for {game_id} ({reason}): {file_key}")


def _claim_correlated_for_anchor(game_id: str, anchor_ts: float) -> int:
    """A game-linked anchor event just landed: sweep the buffer for events
    whose timestamps fall inside the window, in BOTH directions. Returns
    the number of NEW claims."""
    _enabled, strong_s, weak_s = correlation_settings()
    strong: list = []
    weak: list = []
    with _CACHE_LOCK:
        snapshot = list(_UNATTRIBUTED_EVENTS)
        already = {fk for _, fk, _s in snapshot if fk in _CLAIMED_EVENTS}
    for ts, file_key, is_strong in snapshot:
        if file_key in already:
            continue
        delta = abs(anchor_ts - ts)
        if is_strong and delta <= strong_s:
            strong.append((delta, file_key))
        elif not is_strong and delta <= weak_s:
            weak.append((delta, file_key))
    for delta, file_key in strong:
        _claim_event_for_game(game_id, file_key,
                              f"Δ={delta * 1000:.0f}ms from anchor")
    for delta, file_key in sorted(weak)[:_CORR_WEAK_CAP]:
        _claim_event_for_game(game_id, file_key,
                              f"Δ={delta * 1000:.0f}ms from anchor, weak")
    return len(strong) + min(len(weak), _CORR_WEAK_CAP)


def _extract_save_pattern(file_path: Path) -> str:
    """Extract a pattern from a save file name for comparison."""
    name = file_path.name.lower()
    
    # Remove common variations and normalize
    normalized = name
    # Order matters: apply specific patterns (dates, player, char) before generic \d+
    replacements = [
        (r'\d{4}[-_]?\d{2}[-_]?\d{2}', '{DATE}'),  # Replace dates YYYY-MM-DD
        (r'\d{8}', '{DATE}'),       # Replace compact dates YYYYMMDD
        (r'[a-zA-Z0-9_]+_player', '{PLAYER}'),  # Player names
        (r'player_[a-zA-Z0-9_]+', '{PLAYER}'),
        (r'[a-zA-Z0-9_]+_char', '{CHAR}'),      # Character names
        (r'char_[a-zA-Z0-9_]+', '{CHAR}'),
        (r'\d+', '{NUM}'),           # Replace remaining numbers last
    ]
    
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized)
    
    # Remove extra separators
    normalized = re.sub(r'[_\s]+', '_', normalized)
    normalized = normalized.strip('_')
    
    return normalized


def _is_similar_save_file(file_path: Path, known_patterns: Set[str], similarity_threshold: float = 0.4) -> bool:
    """Check if a file name is similar to known save patterns."""
    current_pattern = _extract_save_pattern(file_path)
    
    for known_pattern in known_patterns:
        # Direct string containment check for simple cases
        if known_pattern in current_pattern or current_pattern in known_pattern:
            return True
            
        # Similarity check based on pattern components
        current_parts = set(current_pattern.split('_'))
        known_parts = set(known_pattern.split('_'))
        
        if not current_parts or not known_parts:
            continue
            
        # Calculate Jaccard similarity
        intersection = len(current_parts & known_parts)
        union = len(current_parts | known_parts)
        similarity = intersection / union if union > 0 else 0
        
        if similarity >= similarity_threshold:
            return True
    
    return False


def _matches_common_save_patterns(file_path: Path) -> bool:
    """Check if file matches common save naming patterns."""
    name = file_path.name.lower()
    
    # Enhanced patterns for better detection
    enhanced_patterns = [
        # Numeric patterns
        r'^.*save.*\d+.*\.(sav|dat|save|bak)$',
        r'^.*slot.*\d+.*\.(sav|dat|save|bak)$',
        r'^.*profile.*\d+.*\.(sav|dat|save|bak)$',
        r'^\d{1,3}\.(sav|dat|save|bak)$',
        
        # Auto/quick save
        r'^.*auto.*save.*\.(sav|dat|save|bak)$',
        r'^.*quick.*save.*\.(sav|dat|save|bak)$',
        
        # Player/character
        r'^.*player.*\.(sav|dat|save|bak)$',
        r'^.*character.*\.(sav|dat|save|bak)$',
        r'^.*char.*\.(sav|dat|save|bak)$',
        
        # Chapter/level/stage
        r'^.*chapter.*\d+.*\.(sav|dat|save|bak)$',
        r'^.*level.*\d+.*\.(sav|dat|save|bak)$',
        r'^.*stage.*\d+.*\.(sav|dat|save|bak)$',
        
        # Generic save patterns
        r'^.*save.*\.(sav|dat|save|bak)$',
        r'^.*game.*\.(sav|dat|save|bak)$',
        r'^.*progress.*\.(sav|dat|save|bak)$',
        r'^.*backup.*\.(sav|dat|save|bak)$',
    ]
    
    for pattern in enhanced_patterns:
        if re.match(pattern, name, re.IGNORECASE):
            return True
    
    return False


def _add_seed_pattern(directory_path: str, file_path: Path):
    """Add a discovered save file pattern to the seed patterns for its directory."""
    pattern = _extract_save_pattern(file_path)

    with _CACHE_LOCK:
        if directory_path not in _SEED_PATTERNS:
            _SEED_PATTERNS[directory_path] = set()
        _SEED_PATTERNS[directory_path].add(pattern)
    logger.debug(f"Added seed pattern '{pattern}' for directory {directory_path}")


def _find_similar_files_in_directory(file_path: Path, seed_patterns: Set[str]) -> List[Path]:
    """Find files in the same directory that are similar to the seed patterns."""
    similar_files = []
    # Snapshot discovered files under lock to avoid data race
    with _CACHE_LOCK:
        discovered_snapshot = set(_DISCOVERED_SAVE_FILES)

    try:
        directory = file_path.parent
        for sibling_file in directory.iterdir():
            if not sibling_file.is_file():
                continue

            # Skip if it's the same file or already processed
            sibling_key = str(sibling_file)
            if sibling_key == str(file_path) or sibling_key in discovered_snapshot:
                continue
            
            # Check file extension/name and size
            if _is_ignored_file(sibling_file):
                continue
                
            try:
                file_size = sibling_file.stat().st_size
                if file_size > _MAX_SAVE_FILE_SIZE:
                    continue
            except OSError:
                continue
            
            # Check if similar to any seed pattern
            if _is_similar_save_file(sibling_file, seed_patterns):
                similar_files.append(sibling_file)
                logger.debug(f"Found similar file: {sibling_file.name}")
                
    except (OSError, PermissionError):
        pass
    
    return similar_files


def _scan_directory_for_seeded_saves(directory_path: str, new_save_file: Path):
    """Scan directory for additional save files based on newly discovered save file."""
    with _CACHE_LOCK:
        if directory_path not in _SEED_PATTERNS:
            return []
        seed_patterns = set(_SEED_PATTERNS[directory_path])  # snapshot
    similar_files = _find_similar_files_in_directory(new_save_file, seed_patterns)
    
    # Mark discovered files
    discovered_files = []
    with _CACHE_LOCK:
        for similar_file in similar_files:
            file_key = str(similar_file)
            if file_key not in _DISCOVERED_SAVE_FILES and file_key not in _BACKED_UP_FILES:
                _DISCOVERED_SAVE_FILES.add(file_key)
                discovered_files.append(similar_file)
                logger.info(f"Discovered additional save file: {similar_file.name}")
    
    return discovered_files


def _learn_save_patterns(game_id: str, save_paths: List[str]):
    """Learn save file patterns from existing files for a game."""
    patterns: Set[str] = set()
    
    for path_str in save_paths:
        path = Path(path_str)
        if not path.exists():
            continue
            
        try:
            for file_path in path.rglob("*"):
                if file_path.is_file():
                    if not _is_ignored_file(file_path):
                        try:
                            file_size = file_path.stat().st_size
                            if file_size <= _MAX_SAVE_FILE_SIZE:
                                pattern = _extract_save_pattern(file_path)
                                patterns.add(pattern)
                        except OSError:
                            pass
        except (OSError, PermissionError):
            pass
    
    # Convert to compiled regex patterns for faster matching
    compiled_patterns = []
    for pattern in patterns:
        # Split pattern on placeholders, escape literal parts, then reassemble
        # with regex equivalents for the placeholders.
        _PLACEHOLDER_RE = re.compile(r'(\{NUM\}|\{DATE\}|\{PLAYER\}|\{CHAR\})')
        _PLACEHOLDER_MAP = {
            '{NUM}': r'\d+',
            '{DATE}': r'(?:\d{4}[-_]?\d{2}[-_]?\d{2}|\d{8})',
            '{PLAYER}': r'[a-zA-Z0-9_]+',
            '{CHAR}': r'[a-zA-Z0-9_]+',
        }
        parts = _PLACEHOLDER_RE.split(pattern)
        regex_parts = []
        for part in parts:
            if part in _PLACEHOLDER_MAP:
                regex_parts.append(_PLACEHOLDER_MAP[part])
            else:
                regex_parts.append(re.escape(part))
        regex_pattern = f".*{''.join(regex_parts)}$"

        try:
            compiled_patterns.append(re.compile(regex_pattern, re.IGNORECASE))
        except re.error:
            # Skip invalid patterns
            continue
    
    with _CACHE_LOCK:
        _SAVE_PATTERNS[game_id] = compiled_patterns
        # Evict oldest entries if over capacity, but skip games that are
        # still actively watched (have pending files) to avoid silently
        # degrading save detection for running games.
        while len(_SAVE_PATTERNS) > _MAX_PATTERN_ENTRIES:
            evicted = False
            for candidate_key in list(_SAVE_PATTERNS):
                if candidate_key == game_id:
                    continue  # don't evict the entry we just added
                if candidate_key not in _PENDING_FILES:
                    del _SAVE_PATTERNS[candidate_key]
                    evicted = True
                    break
            if not evicted:
                break  # all entries are active, can't evict safely
    from core.library import get_library
    _entry = get_library().get_by_id(game_id)
    _name = _entry.name if _entry else game_id
    logger.info(f"Learned {len(compiled_patterns)} save patterns for {_name}")


class _SaveHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
    """Debounced file event handler."""

    _hints_cache: list | None = None
    _hints_cache_time: float = 0.0
    _HINTS_CACHE_TTL: float = 30.0  # re-read config every 30 seconds

    # Initialize hints cache once at class level; refreshed every _HINTS_CACHE_TTL seconds.
    _hints_initialized = False

    def __init__(self, game_id: str, on_change: Callable[[str], None]):
        if WATCHDOG_AVAILABLE:
            super().__init__()
        self._game_id   = game_id
        self._on_change = on_change
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        # cancel() cannot stop a Timer that has already begun _fire_all_pending;
        # these flags serialize in-flight fires and re-arm debounce if events
        # arrived during one (avoids two _on_change calls for the same batch).
        self._firing = False
        self._rearm_after_fire = False
        # Pre-initialize hints cache on first handler creation (avoids
        # doing config I/O on every file-system event).
        with _CACHE_LOCK:
            if not _SaveHandler._hints_initialized:
                import time as _time
                if _SaveHandler._hints_cache is None:
                    try:
                        from core.config_manager import get_config
                        _SaveHandler._hints_cache = get_config().get("save_folder_hints", _DEFAULT_HINTS)
                    except Exception:
                        _SaveHandler._hints_cache = list(_DEFAULT_HINTS)
                    _SaveHandler._hints_cache_time = _time.time()
                _SaveHandler._hints_initialized = True

    def on_any_event(self, event: "FileSystemEvent"):
        if event.is_directory:
            return

        # Correlation ANCHOR — set BEFORE any rejection below. This handler
        # only receives game-linked events (game-scoped watches, or common
        # -root events that passed the name filter), so even a file we then
        # reject as a save (a log, a temp file) proves the game's process
        # is writing at this instant. That timestamp is what lets a
        # simultaneous save-like write in an unrelated-named profile folder
        # be claimed — the association the name filters can't provide.
        #
        # It must, however, be an actual write. Anchoring on every event
        # regardless of type left the window permanently open during a long
        # session: a running game touches its own tree constantly, and reads,
        # attribute changes and deletions all arrive as FileModifiedEvent —
        # so any unrelated save-like write anywhere on the machine correlated.
        # Off by default; the guards are ordered so that costs one dict
        # lookup and no stat() when it is.
        if correlation_settings()[0] and _is_write_event(event):
            import time as _time
            _anchor_now = _time.time()
            if _has_fresh_write(event.src_path, _anchor_now):
                with _CACHE_LOCK:
                    _LAST_ANCHOR_TS[self._game_id] = _anchor_now
                try:
                    if _claim_correlated_for_anchor(self._game_id, _anchor_now):
                        # Claims must surface even when THIS event goes on to
                        # be rejected (the log itself is not a save — the
                        # claimed profile write is).
                        self.schedule_fire()
                except Exception as _corr_err:
                    logger.debug(f"Correlation sweep failed: {_corr_err}")

        # Ignore files with non-save extensions/filenames
        try:
            file_path = Path(event.src_path)
            file_ext = file_path.suffix.lower()
            if file_ext in _IGNORE_EXTENSIONS or file_path.stem.lower() in SKIP_FILENAME_STEMS:
                return
                
            # Check file size — on transient OSError (file locked during write),
            # skip the size check and let the debounce timer handle it on next event
            try:
                file_size = file_path.stat().st_size
                if file_size > _MAX_SAVE_FILE_SIZE:
                    logger.debug(f"Ignoring large file {file_path.name}: {file_size/1024/1024:.1f}MB")
                    return
            except OSError:
                # File temporarily inaccessible — don't block watchdog thread.
                # The debounce timer will fire and re-check existence later.
                pass
            
            # Check if file was already backed up
            file_key = str(file_path)
            with _CACHE_LOCK:
                if file_key in _BACKED_UP_FILES:
                    logger.debug(f"File {file_path.name} already backed up, skipping")
                    return
            
            # Enhanced detection: Check if this looks like a save file
            is_save_file = False
            
            # 1. Check against learned patterns for this game
            with _CACHE_LOCK:
                _game_patterns = list(_SAVE_PATTERNS.get(self._game_id, []))
            for pattern in _game_patterns:
                if pattern.match(file_path.name):
                    is_save_file = True
                    logger.debug(f"File {file_path.name} matches learned pattern")
                    break
            
            # 2. Check against common save patterns
            if not is_save_file:
                if _matches_common_save_patterns(file_path):
                    is_save_file = True
                    logger.debug(f"File {file_path.name} matches common save pattern")
            
            # 3. Check similarity to known save files in the same directories
            if not is_save_file:
                # Get known patterns from the same directory (filter by string prefix
                # to avoid O(n) Path construction for every known file)
                known_patterns = set()
                try:
                    parent_str = str(file_path.parent)
                    # Use the per-directory index for O(1) lookup instead of
                    # iterating the entire _KNOWN_FILES set on every event.
                    with _CACHE_LOCK:
                        dir_files = list(_KNOWN_FILES_BY_DIR.get(parent_str, ()))
                    for known_file in dir_files:
                        pattern = _extract_save_pattern(Path(known_file))
                        known_patterns.add(pattern)

                    if known_patterns and _is_similar_save_file(file_path, known_patterns):
                        is_save_file = True
                        logger.debug(f"File {file_path.name} is similar to known save files")
                except Exception:
                    pass
            
            # 4. NEW: Check against seed patterns from discovered save files
            if not is_save_file:
                directory_path = str(file_path.parent)
                with _CACHE_LOCK:
                    seed_snapshot = set(_SEED_PATTERNS.get(directory_path, set()))
                if seed_snapshot and _is_similar_save_file(file_path, seed_snapshot):
                    is_save_file = True
                    logger.debug(f"File {file_path.name} matches seed pattern from discovered saves")
            
            # 5. If still not identified as save file, apply heuristics
            if not is_save_file:
                # Check if it's in a save-like directory structure
                parent_name = file_path.parent.name.lower()
                import time as _time
                now = _time.time()
                with _CACHE_LOCK:
                    if _SaveHandler._hints_cache is None or (now - _SaveHandler._hints_cache_time) > _SaveHandler._HINTS_CACHE_TTL:
                        from core.config_manager import get_config
                        _SaveHandler._hints_cache = get_config().get("save_folder_hints", _DEFAULT_HINTS)
                        _SaveHandler._hints_cache_time = now
                    _hints = list(_SaveHandler._hints_cache)
                if any(hint in parent_name for hint in _hints):
                    is_save_file = True
                    logger.debug(f"File {file_path.name} is in save-like directory: {parent_name}")
                
                # Check if it has save-like extension (.dat removed: it's a
                # detection-excluded format, see constants.DETECTION_SKIP_EXTENSIONS)
                save_extensions = {'.sav', '.save', '.backup'}
                if file_ext in save_extensions:
                    is_save_file = True
                    logger.debug(f"File {file_path.name} has save-like extension: {file_ext}")
            
            if not is_save_file:
                logger.debug(f"File {file_path.name} does not appear to be a save file")
                return
            
            # NEW: Add to seed patterns and scan for similar files
            directory_path = str(file_path.parent)
            _add_seed_pattern(directory_path, file_path)
            
            # Scan for additional similar files in the same directory
            additional_saves = _scan_directory_for_seeded_saves(directory_path, file_path)
            
            # Add the original file and any discovered files to pending
            # Note: files are added to _BACKED_UP_FILES only after backup succeeds
            # (in _fire_all_pending), not here, to avoid marking files as backed up
            # if the backup later fails.
            with _CACHE_LOCK:
                if self._game_id not in _PENDING_FILES:
                    _PENDING_FILES[self._game_id] = set()
                _PENDING_FILES[self._game_id].add(file_key)
                _DISCOVERED_SAVE_FILES.add(file_key)
                logger.info(f"Save file detected: {file_path.name}")

                # Add any additional discovered files
                for additional_file in additional_saves:
                    additional_key = str(additional_file)
                    _PENDING_FILES[self._game_id].add(additional_key)
                    logger.info(f"Additional save file discovered: {additional_file.name}")
            
        except (ValueError, OSError):
            return

        self.schedule_fire()

    def schedule_fire(self):
        """(Re)arm the debounced backup trigger for all pending files."""
        with self._lock:
            if self._firing:
                # A previous debounce is already inside _fire_all_pending;
                # cancel() cannot stop it. Ask it to re-arm when it finishes
                # instead of stacking a second Timer that would race it.
                self._rearm_after_fire = True
                return
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SEC, self._fire_all_pending)
            self._timer.daemon = True
            self._timer.start()

    def _fire_all_pending(self):
        """Backup ALL pending files for this game and clear pending cache."""
        with self._lock:
            self._timer = None
            if self._firing:
                # Overlapping Timer that cancel() could not stop — fold into
                # a single trailing re-arm rather than a second _on_change.
                self._rearm_after_fire = True
                return
            self._firing = True
            self._rearm_after_fire = False
        try:
            with _CACHE_LOCK:
                my_pending = _PENDING_FILES.get(self._game_id, set())
                if not my_pending:
                    return
                # Snapshot the pending files but keep them in _PENDING_FILES.
                # They will only be removed after backup succeeds (via
                # mark_game_files_backed_up) or on next successful fire.
                pending_snapshot = set(my_pending)

            # File I/O outside the lock to avoid blocking other threads
            pending_files = []
            for file_key in pending_snapshot:
                try:
                    file_path = Path(file_key)
                    if file_path.exists():
                        pending_files.append(str(file_path))
                except (OSError, ValueError):
                    pass

            if pending_files:
                logger.info(f"Backing up {len(pending_files)} save files: {[Path(f).name for f in pending_files]}")
                try:
                    self._on_change(self._game_id)
                    # The callback is asynchronous (queued to the Qt event loop).
                    # Files remain in _PENDING_FILES until the backup completes
                    # and mark_game_files_backed_up() moves them to _BACKED_UP_FILES.
                    # On failure, they stay pending and will be retried on next event.
                except Exception as e:
                    logger.warning(f"Backup callback failed for {self._game_id}, "
                                   f"files will be retried: {e}")
        finally:
            with self._lock:
                self._firing = False
                rearm = self._rearm_after_fire
                self._rearm_after_fire = False
            if rearm:
                self.schedule_fire()

    def cancel(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._rearm_after_fire = False


class _PendingPathHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
    """
    Watches the nearest existing parent directory waiting for a specific
    subdirectory to appear. Once the target path is created, calls on_created.
    """

    def __init__(self, target: Path, on_created: Callable[[Path], None]):
        if WATCHDOG_AVAILABLE:
            super().__init__()
        self._target     = target
        self._on_created = on_created
        self._fired      = False
        self._fire_lock  = threading.Lock()

    def on_created(self, event: "FileSystemEvent"):
        should_fire = False
        with self._fire_lock:
            if self._fired:
                return
            try:
                created = Path(event.src_path)
                if (created == self._target or _is_relative_to_compat(created, self._target)) and self._target.exists():
                    self._fired = True
                    should_fire = True
            except Exception:
                return
        if should_fire:
            self._on_created(self._target)

    def on_modified(self, event: "FileSystemEvent"):
        # Some OS/fs combos fire modified instead of created
        self.on_created(event)


def _get_common_save_roots(appid: str = "") -> list[Path]:
    """Return OS-specific common save-data roots to watch for games with no
    configured save_paths. These cover engines (Godot, Unity, etc.) that write
    saves to AppData or similar locations without leaving persistent file handles
    (which the open-file tracker would miss).

    *appid* adds the game's own Proton prefix, where a Windows game running
    under Proton actually writes — no XDG root covers it. Only that one game's
    prefix, and only while it is running: watching every prefix on the machine
    would burn one recursive inotify watch per installed Proton game, and
    Linux caps those per user."""
    import platform as _plat
    roots: list[Path] = []
    system = _plat.system()
    if system == "Windows":
        for env_var in ("APPDATA", "LOCALAPPDATA"):
            v = os.environ.get(env_var)
            if v:
                p = Path(v)
                if p.exists():
                    roots.append(p)
        profile = os.environ.get("USERPROFILE")
        if profile:
            for rel in ("AppData/LocalLow", "Documents", "Saved Games"):
                p = Path(profile) / rel
                if p.exists():
                    roots.append(p)
    elif system == "Linux":
        home = Path.home()
        # ".renpy" is Ren'Py's engine data dir on Unix (the AppData/RenPy
        # analogue) — NOT under ~/.local/share, so it needs its own root.
        for rel in (".local/share", ".config", ".renpy"):
            p = home / rel
            if p.exists():
                roots.append(p)
        if appid:
            try:
                from core.save_detector import compat_prefix_roots
                roots.extend(compat_prefix_roots(str(appid)))
            except Exception as e:
                logger.debug(f"Could not add the Proton prefix for {appid}: {e}")
    elif system == "Darwin":
        home = Path.home()
        # macOS Ren'Py uses ~/.renpy and/or ~/Library/RenPy — neither is under
        # ~/Library/Application Support, so list them explicitly.
        for rel in ("Library/Application Support", "Documents", ".renpy", "Library/RenPy"):
            p = home / rel
            if p.exists():
                roots.append(p)
    return roots


class _CommonRootSaveHandler:
    """Filesystem-event handler for common save-root directories.

    Wraps a real _SaveHandler but adds a fast game-name pre-filter so the
    vast majority of events in AppData (which belong to OTHER applications)
    are discarded immediately without entering the heavier _SaveHandler logic.

    Used when a game has no configured save_paths — the watcher must scan
    broad root directories to find where the game writes its saves.
    """
    def __init__(self, game_id: str, game_name: str, on_change, extra_terms=None):
        self._inner = _SaveHandler(game_id, on_change)
        # Words that identify THIS game in a broad-root event path. The library
        # display name (words > 2 chars) is used as before; additionally the
        # install-folder name / exe stem (extra_terms, words > 3 chars and not
        # generic) are included so a roaming engine folder named after the
        # install — e.g. Ren'Py's AppData/RenPy/<save_directory> — is accepted
        # even when its name diverges from the display name (the exact case
        # where the AppData location used to be silently dropped).
        parts: set[str] = set()
        if game_name:
            for part in game_name.replace("-", " ").replace("_", " ").split():
                if len(part) > 2:
                    parts.add(part.lower())
        for term in (extra_terms or []):
            for part in str(term).replace("-", " ").replace("_", " ").split():
                pl = part.lower()
                if len(pl) > 3 and pl not in _GENERIC_IDENTITY_TERMS:
                    parts.add(pl)
        self._game_name_parts: list[str] = list(parts)

    def dispatch(self, event):       # watchdog calls this
        self.on_any_event(event)

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Fast pre-filter: at least one word from the game name must appear
        # somewhere in the file path (e.g. "Modification App" → "modification"
        # or "app" must be in the path)
        if self._game_name_parts:
            path_lower = event.src_path.lower()
            if not any(part in path_lower for part in self._game_name_parts):
                # No nominal link — but don't just discard it: buffer it for
                # TEMPORAL correlation. If this game saves in the same
                # instant (double-write engines), the event gets claimed.
                # Same two guards as the anchor side: opt-in, and only for
                # events that really are a create/modify/move.
                if correlation_settings()[0] and _is_write_event(event):
                    _record_unattributed(event.src_path, self._inner._game_id,
                                         self._inner)
                return
        self._inner.on_any_event(event)

    def __getattr__(self, name):     # delegate watchdog protocol methods
        return getattr(self._inner, name)


class SaveWatcher(QObject):
    save_changed     = Signal(str)   # game_id — save file modified
    folder_appeared  = Signal(str)   # game_id — watched folder now exists

    def __init__(self, parent=None):
        super().__init__(parent)
        self._observer: "Observer | None" = None
        self._handlers:  dict[str, _SaveHandler]         = {}  # game_id → handler
        self._watches:   dict[str, object]               = {}  # "game_id:path" → watch
        self._pending:   dict[str, dict[str, _PendingPathHandler]]  = {}  # game_id → {path: handler}
        self._pnd_watches: dict[str, dict[str, object]]             = {}  # game_id → {path: watch}
        self._available = WATCHDOG_AVAILABLE

    def start(self):
        if not self._available:
            logger.warning("SaveWatcher disabled (watchdog unavailable)")
            return
        self._observer = Observer()
        self._observer.start()
        logger.info("Filesystem watcher started")

    def stop(self):
        if self._observer:
            for h in list(self._handlers.values()):
                h.cancel()
            self._observer.stop()
            self._observer.join(timeout=3)
            if self._observer.is_alive():
                logger.warning("Filesystem observer did not stop within timeout, abandoning thread")
            self._observer = None
            self._handlers.clear()
            self._watches.clear()
            self._pending.clear()
            self._pnd_watches.clear()
        # Clear module-level caches to avoid stale state on restart
        with _CACHE_LOCK:
            _BACKED_UP_FILES.clear()
            _KNOWN_FILES.clear()
            _KNOWN_FILES_BY_DIR.clear()
            _PENDING_FILES.clear()
            _SEED_PATTERNS.clear()
            _DISCOVERED_SAVE_FILES.clear()
            _SAVE_PATTERNS.clear()
            _SaveHandler._hints_cache = None
            _SaveHandler._hints_initialized = False
        logger.info("Filesystem watcher stopped")

    def watch_game(self, game_id: str, save_paths: list[str], game_name: str = ""):
        if not self._available or not self._observer:
            return
        # Virtual registry entries can't be watched by watchdog — their
        # change detection runs in the live-tracking poll (last-write gate).
        from core.registry_saves import is_registry_path
        save_paths = [p for p in save_paths if not is_registry_path(p)]
        self.unwatch_game(game_id)

        # Learn patterns from existing files before starting to watch
        _learn_save_patterns(game_id, save_paths)

        handler = _SaveHandler(game_id, self._on_save_changed)
        self._handlers[game_id] = handler
        watched_any = False

        for path_str in save_paths:
            path = Path(path_str)
            if path.exists():
                try:
                    # File entries (e.g. Save1.rxdata beside the exe): watch
                    # the parent directory non-recursively — watchdog needs
                    # a directory, and recursing an install root would sweep
                    # the whole installation into event handling.
                    if path.is_file():
                        w = self._observer.schedule(handler, str(path.parent), recursive=False)
                    else:
                        w = self._observer.schedule(handler, str(path), recursive=True)
                    self._watches[f"{game_id}:{path_str}"] = w
                    watched_any = True
                    logger.debug(f"Watching: {path}")
                    self._initialize_backed_up_files(path)
                except Exception as e:
                    logger.warning(f"Could not watch {path}: {e}")
            else:
                # Path doesn't exist yet — watch nearest existing parent
                self._watch_pending(game_id, path)
                watched_any = True  # pending counts as "being watched"

        # Broad discovery covers two blind spots that a single early hit must
        # not silence: (1) real save data can show up somewhere unrelated
        # entirely (AppData/Roaming, Documents, …) that a per-path watch
        # above never touches, and (2) a per-path watch on a FILE (like a
        # log sitting in the install root) only watches its parent
        # directory non-recursively — deliberately, so recursing an entire
        # install tree isn't the default — which means a save folder that
        # later appears a level or two below it (e.g. "<install>/game/
        # save/") is invisible to that watch. This used to run only when
        # save_paths was completely empty, so the very first hit (even a
        # single stray log file) permanently turned broad discovery off —
        # this is why detection looked inconsistent ("works sometimes, not
        # others"): it depended on which path happened to be found first,
        # not on anything time-related.
        #
        # Broad discovery now runs for the whole tracked session regardless
        # of confirmation state — confirming some paths doesn't mean every
        # other save location for this game has necessarily been found
        # yet, and live tracking should keep surfacing new candidates for
        # the user to review. What DOES change once the user has confirmed
        # at least one path is what happens with newly-discovered ones:
        # they're still reported (via save_changed → _pending_auto_scans),
        # but they no longer get auto-backed-up as "provisional" the way
        # they do before any confirmation — see _ingame_backup_tick.
        _install_dir: Optional[Path] = None
        try:
            from core.library import get_library
            _entry = get_library().get_by_id(game_id)
            if _entry is not None and _entry.exe_path:
                _p = Path(_entry.exe_path).parent
                if _p.exists():
                    _install_dir = _p
        except Exception:
            pass   # not in the library yet (mid add-game flow)

        if game_name:
            # OS-wide common roots (AppData/Roaming, Documents, …) — these are
            # shared by every other application too, so name-filtered. The
            # install-folder name and exe stem are passed as extra identity
            # terms so a roaming engine folder named after the install (e.g.
            # Ren'Py's save_directory) is not filtered out when its name
            # diverges from the library display name.
            _identity_extra: list[str] = []
            if _install_dir is not None:
                _identity_extra.append(strip_version_tokens(_install_dir.name))
            if _entry is not None and getattr(_entry, "exe_path", ""):
                _identity_extra.append(strip_version_tokens(Path(_entry.exe_path).stem))
            _appid = getattr(_entry, "appid", "") if _entry is not None else ""
            common_roots = _get_common_save_roots(_appid or "")
            root_handler = _CommonRootSaveHandler(
                game_id, game_name, self._on_save_changed, extra_terms=_identity_extra)
            for root in common_roots:
                key = f"{game_id}:__root__:{root}"
                if key not in self._watches:
                    try:
                        w = self._observer.schedule(root_handler, str(root), recursive=True)
                        self._watches[key] = w
                        watched_any = True
                        logger.debug(f"Common-root watching {root} for {game_name!r}")
                    except Exception as e:
                        logger.debug(f"Could not watch root {root} for {game_id}: {e}")

            # The game's OWN install tree — recursive, no name-filter needed
            # (everything under it is already scoped to this one game).
            # Skipped if a confirmed save_path already covers it (exactly,
            # or as an ancestor) to avoid watching the same tree twice.
            if _install_dir is not None:
                _install_str = str(_install_dir)
                already_covered = any(
                    _install_str == ps or _install_str.startswith(ps + os.sep)
                    for ps in save_paths
                )
                key = f"{game_id}:__install__:{_install_str}"
                if key not in self._watches and not already_covered:
                    try:
                        w = self._observer.schedule(handler, _install_str, recursive=True)
                        self._watches[key] = w
                        watched_any = True
                        logger.debug(f"Install-root watching {_install_dir} for {game_name!r}")
                    except Exception as e:
                        logger.debug(f"Could not watch install root {_install_dir} for {game_id}: {e}")

        if not watched_any:
            logger.debug(f"No existing save paths for {game_id}; watching for folder creation")

    def _initialize_backed_up_files(self, path: Path):
        """Scan existing files and mark them as already known/backed-up.

        Populates both:
        - ``_KNOWN_FILES`` — so similarity checks work correctly.
        - ``_BACKED_UP_FILES`` — so the watcher does NOT emit save_changed for
          files that already existed before monitoring started (i.e. files that
          were backed up in a previous session or unchanged since last exit).

        This prevents a spurious backup+sync immediately after the app starts
        or a game is detected, when no saves have actually changed.
        """
        found_files: list[str] = []
        try:
            # File entries baseline just themselves; directories recurse.
            iter_files = [path] if path.is_file() else path.rglob("*")
            for file_path in iter_files:
                if file_path.is_file():
                    if not _is_ignored_file(file_path):
                        try:
                            file_size = file_path.stat().st_size
                            if file_size <= _MAX_SAVE_FILE_SIZE:
                                found_files.append(str(file_path))
                        except OSError:
                            pass
        except (OSError, PermissionError):
            logger.debug(f"Could not scan existing files in {path}")

        with _CACHE_LOCK:
            for fp in found_files:
                _KNOWN_FILES.add(fp)
                # Mark as already backed-up so the watcher treats these files
                # as baseline — only NEW modifications will trigger save_changed.
                _BACKED_UP_FILES.add(fp)
                parent_dir = str(Path(fp).parent)
                _KNOWN_FILES_BY_DIR.setdefault(parent_dir, set()).add(fp)
        logger.info(
            f"Initialized watcher baseline: {len(found_files)} files in {path} "
            f"(marked as already backed-up)"
        )

    def _watch_pending(self, game_id: str, target: Path):
        """Watch for target directory to be created."""
        if not self._observer:
            return
        # Find nearest existing ancestor
        parent = target
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.exists():
            logger.warning(f"Could not find existing parent for {target}")
            return

        def _on_folder_created(path: Path, gid=game_id, tgt=target):
            # Marshal to Qt main thread
            from PySide6.QtCore import QMetaObject, Qt, Q_ARG
            QMetaObject.invokeMethod(
                self, "_emit_folder_appeared",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, gid),
            )
            # Re-watch game with actual path now that it exists
            QMetaObject.invokeMethod(
                self, "_rewatch_game",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, gid),
            )

        pnd_handler = _PendingPathHandler(target, _on_folder_created)
        if game_id not in self._pending:
            self._pending[game_id] = {}
        self._pending[game_id][str(target)] = pnd_handler
        try:
            w = self._observer.schedule(pnd_handler, str(parent), recursive=True)
            if game_id not in self._pnd_watches:
                self._pnd_watches[game_id] = {}
            self._pnd_watches[game_id][str(target)] = w
            logger.debug(f"Pending watch on {parent} for {target}")
        except Exception as e:
            logger.warning(f"Pending watch error: {e}")

    @Slot(str)
    def _rewatch_game(self, game_id: str):
        from core.library import get_library
        entry = get_library().get_by_id(game_id)
        if entry and entry.save_paths:
            self.watch_game(game_id, entry.save_paths)

    def _cleanup_game_caches(self, save_paths: list[str]):
        """Remove files from global caches when game is unwatched."""
        # Phase 1: snapshot cache keys under lock
        with _CACHE_LOCK:
            backed_snapshot = set(_BACKED_UP_FILES)
            known_snapshot = set(_KNOWN_FILES)
            pending_snapshot = {gid: set(files) for gid, files in _PENDING_FILES.items()}
            discovered_snapshot = set(_DISCOVERED_SAVE_FILES)

        # Phase 2: resolve paths and determine removals without holding the lock
        remove_backed: set = set()
        remove_known: set = set()
        remove_pending: dict[str, list] = {}
        remove_seed_keys: list[str] = []
        remove_discovered: set = set()

        from core.registry_saves import is_registry_path
        for path_str in save_paths:
            # Registry entries never enter these file caches (watch_game
            # filters them before anything is seeded) — resolving them
            # would only produce a bogus CWD-relative key.
            if is_registry_path(path_str):
                continue
            path = Path(path_str)
            try:
                resolved_path = path.resolve()
            except OSError:
                continue

            remove_backed.update(fk for fk in backed_snapshot if _is_subpath(fk, resolved_path))
            remove_known.update(fk for fk in known_snapshot if _is_subpath(fk, resolved_path))

            for gid, files in pending_snapshot.items():
                for file_key in files:
                    try:
                        if _is_relative_to_compat(Path(file_key).resolve(), resolved_path):
                            remove_pending.setdefault(gid, []).append(file_key)
                    except (ValueError, OSError):
                        pass

            remove_seed_keys.append(str(resolved_path))
            remove_seed_keys.append(str(path))

            for file_key in discovered_snapshot:
                try:
                    if _is_subpath(file_key, resolved_path):
                        remove_discovered.add(file_key)
                except (ValueError, OSError):
                    pass

        # Phase 3: re-acquire lock and apply removals
        with _CACHE_LOCK:
            _BACKED_UP_FILES.difference_update(remove_backed)
            _KNOWN_FILES.difference_update(remove_known)
            # Keep per-directory index in sync
            for fp in remove_known:
                parent_dir = str(Path(fp).parent)
                dir_set = _KNOWN_FILES_BY_DIR.get(parent_dir)
                if dir_set is not None:
                    dir_set.discard(fp)
                    if not dir_set:
                        del _KNOWN_FILES_BY_DIR[parent_dir]

            for gid, keys in remove_pending.items():
                if gid in _PENDING_FILES:
                    for file_key in keys:
                        _PENDING_FILES[gid].discard(file_key)
                    if not _PENDING_FILES[gid]:
                        del _PENDING_FILES[gid]

            for key in remove_seed_keys:
                _SEED_PATTERNS.pop(key, None)

            _DISCOVERED_SAVE_FILES.difference_update(remove_discovered)

    def unwatch_game(self, game_id: str):
        if not self._observer:
            return
        h = self._handlers.pop(game_id, None)
        if h:
            h.cancel()
        for k in [k for k in self._watches if k.startswith(f"{game_id}:")]:
            try:
                self._observer.unschedule(self._watches.pop(k))
            except Exception:
                pass
        self._pending.pop(game_id, {})
        pnd_w_dict = self._pnd_watches.pop(game_id, {})
        for pnd_w in pnd_w_dict.values():
            try:
                self._observer.unschedule(pnd_w)
            except Exception:
                pass
        
        # Clean up global caches for this game's paths
        from core.library import get_library
        entry = get_library().get_by_id(game_id)
        if entry and entry.save_paths:
            self._cleanup_game_caches(entry.save_paths)
        
        # Clean up learned patterns
        with _CACHE_LOCK:
            _SAVE_PATTERNS.pop(game_id, None)
        logger.debug(f"Cleaned up patterns for game {game_id}")

    def update_game_paths(self, game_id: str, save_paths: list[str]):
        self.watch_game(game_id, save_paths)

    # ── Thread → GUI marshallers ──────────────────────────────────────────────

    def _on_save_changed(self, game_id: str):
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self, "_emit_save_changed",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, game_id),
        )

    @Slot(str)
    def _emit_save_changed(self, game_id: str):
        self.save_changed.emit(game_id)

    @Slot(str)
    def _emit_folder_appeared(self, game_id: str):
        self.folder_appeared.emit(game_id)

    @property
    def is_available(self) -> bool:
        return self._available


_watcher: SaveWatcher | None = None
_watcher_lock = threading.Lock()


def get_save_watcher() -> SaveWatcher:
    global _watcher
    if _watcher is None:
        with _watcher_lock:
            if _watcher is None:
                _watcher = SaveWatcher()
    return _watcher


def mark_game_files_backed_up(game_id: str) -> None:
    """Move a game's pending files to the backed-up set.

    Called by the UI layer after a backup has actually succeeded, so the
    watcher no longer re-queues these files on the next change event.
    """
    with _CACHE_LOCK:
        pending = _PENDING_FILES.pop(game_id, set())
        for fp in pending:
            _BACKED_UP_FILES.add(fp)


def get_pending_save_paths(game_id: str, exe_dir: str = "") -> list[str]:
    """Live-tracking discoveries for *game_id*, as confirmation-panel paths.

    Every file the watchdog has classified as a save (i.e. one actually
    modified or created since the game launched) is reported as its
    CONTAINING FOLDER, so all sibling save slots come with it — this is how
    live tracking *surfaces* a save directory (e.g. a Ren'Py ``game/saves``)
    with nothing hardcoded. The exception: when that folder is itself a
    watched ROOT — the game's install dir, or an OS save root such as
    Documents / Saved Games / AppData — proposing the whole tree would be
    catastrophic, so it degrades to the individual file. Read-only: unlike
    mark_game_files_backed_up() it never pops the queue.
    """
    roots: set[str] = set()
    for r in _get_common_save_roots():
        roots.add(os.path.normcase(os.path.normpath(str(r))))
    if exe_dir:
        roots.add(os.path.normcase(os.path.normpath(exe_dir)))
    with _CACHE_LOCK:
        files = list(_PENDING_FILES.get(game_id, set()))
    out: list[str] = []
    seen: set[str] = set()
    for fp in files:
        parent = os.path.dirname(fp)
        # Normal case → the containing save folder; folder is a watched root
        # → keep the individual file (never propose a whole root as a save dir).
        cand = parent if (parent and os.path.normcase(os.path.normpath(parent)) not in roots) else fp
        key = os.path.normcase(os.path.normpath(cand))
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out
