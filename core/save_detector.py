"""
SaveSync - Save File Detector
Multi-strategy heuristic detection of save data locations.

Strategies (in order of confidence):
1. Process open-file tracking (live, highest accuracy)  
2. Engine-specific known paths (RenPy, RPGMaker, Unity, Unreal, etc.)
3. Filesystem scan of common save locations
4. Windows Registry (HKCU\\Software)
5. Relative paths near exe (game/saves, saves/, data/, etc.)
"""
import logging
import os
import platform
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from core.constants import (
    SAVE_FOLDER_HINTS, WATCH_PATHS_TEMPLATES, SKIP_EXTENSIONS,
    DETECTION_SKIP_EXTENSIONS, SKIP_FILENAME_STEMS, USER_DATA_DIR,
    strip_version_tokens, CAMEL_SPLIT_RE,
)
from core.config_manager import get_config
from core import is_relative_to as _is_relative_to
import i18n

# Resolve SaveSync's own data dir once — never detect it as a game save path
_OWN_DATA_DIR = str(USER_DATA_DIR.resolve()).lower()

# Module-level cancel support for long-running scans.
# Set by UI when the user clicks cancel; checked by _scan_dir between
# directory iterations so the thread exits promptly.
#
# Cancellation is EPOCH-based, not a latching flag: cancel_detection()
# bumps a global counter, and every detection run snapshots the counter
# when it starts (per thread). A run is cancelled only if the counter
# advanced AFTER its snapshot — i.e. cancels only hit scans that were
# in flight when the user clicked cancel. The previous latching Event
# stayed set until the add-game dialog happened to reset it, so one
# cancelled dialog scan silently emptied every later background scan
# (live tracking found paths, expand_selectable_paths dropped them all,
# and the auto-scan review dialog never had anything to show).
import threading as _threading
_cancel_lock = _threading.Lock()
_cancel_epoch = 0                    # bumped by cancel_detection()
_run_epoch = _threading.local()      # per-thread snapshot taken at run start

def cancel_detection():
    """Signal all in-flight scans to abort as soon as possible."""
    global _cancel_epoch
    with _cancel_lock:
        _cancel_epoch += 1

def reset_cancel():
    """Mark the calling thread as starting a fresh run (pre-epoch API kept
    for existing callers; _begin_detection_run does this automatically)."""
    _begin_detection_run()

def _begin_detection_run():
    """Snapshot the cancel epoch for the calling thread. Only cancels
    issued after this point affect the thread's current run."""
    _run_epoch.value = _cancel_epoch

def _is_cancelled() -> bool:
    snap = getattr(_run_epoch, "value", None)
    if snap is None:
        # First check on a thread that never snapshotted (helpers called
        # outside detect_save_paths): treat it as the start of a run.
        _run_epoch.value = snap = _cancel_epoch
    return _cancel_epoch > snap

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()

# Extensions that are definitely save file formats (boost score when found).
# ".dat" removed on purpose: it's in DETECTION_SKIP_EXTENSIONS now (too noisy
# as a detection signal — engine data, installers, caches all use it).
# ".json" removed too: RPG Maker MV/MZ ship their entire game database as
# .json data files — presence of .json is NOT evidence of a save folder.
_SAVE_EXTENSIONS = frozenset({
    ".sav", ".save", ".cfg", ".ini", ".xml", ".db", ".sqlite",
    ".rpgsave", ".rxdata", ".rvdata", ".rvdata2", ".rgssad",  # RPGMaker XP/VX/VX Ace
    ".save.gz", ".bak",
    ".lsd",   # RPG2000/2003
    ".p",     # some Ren'Py
})

# Directories to always skip during scan. Shared game-asset/VCS names live in
# core.skip_dirs and are reused by the backup content-walk under
# _BACKUP_SKIP_DIRS; the scan set adds OS/system/browser/AV/app folders.
from core.skip_dirs import SCAN_SKIP_DIRS as _SKIP_DIRS

# Generic exe stems that are meaningless as search terms or context.
# Shared across save detection and API search — must be kept in sync.
GENERIC_EXE_STEMS = {
    'game', 'game64', 'game32', 'launcher', 'launch', 'start',
    'main', 'app', 'application', 'run', 'play', 'client',
    'setup', 'install', 'installer', 'uninstall', 'unins000',
    'update', 'patcher', 'updater',
    'bootstrap', 'bootstrapper', 'loader', 'engine',
    'nw', 'nwjs',   # NW.js runtime exe (RPG Maker MV/MZ ship "nw.exe")
    'savesync', 'save',
    'menu', 'title', 'gui', 'ui', 'frontend',
    'runtime', 'redist', 'redistributable',
    # Tools and helpers shipped beside a game — meaningless as a title, so a
    # game is named after its folder instead of after them.
    'gamepro', 'tool', 'tools', 'patch', 'gameupdate', 'startwithtool',
    'windowsiconupdater', 'iconupdater',
    'win64', 'win32', 'x64', 'x86', 'win',
    'release', 'debug', 'test', 'dev',
    'program', 'executable', 'exe',
    'default', 'settings', 'config',
    # Generic directory names (not typical exe stems but used for folder
    # name filtering and walk-up — prevents matching build trees, etc.)
    'bin', 'lib', 'lib64', 'common', 'build', 'dist', 'desktop',
    'game_unpacked',
}
# Internal alias so save_detector's existing references work unchanged
_GENERIC_EXE_STEMS = GENERIC_EXE_STEMS

# Folder names that are install roots / container dirs, never a game title.
# Only used by derive_display_name's folder walk-up (kept separate from
# GENERIC_EXE_STEMS, which also drives save-detection scoring).
_CONTAINER_DIR_NAMES = frozenset({
    'games', 'giochi', 'steamapps', 'downloads', 'download', 'documents',
    'program files', 'program files (x86)', 'programdata', 'users', 'public',
    'appdata', 'local', 'roaming', 'locallow',
})


def derive_display_name(exe_path: str, fallback: str = "") -> str:
    """Best human display name for a game executable.

    Uses the exe stem unless it is a generic name ("game", "launcher",
    "start"…). In that case it walks UP the folder structure — same idea as
    the save-search hints — and returns the nearest non-generic folder name
    (version tokens stripped), so a game is never labeled "Game" or
    "Launcher". Falls back to *fallback*, then to the raw stem.
    """
    try:
        p = Path(exe_path)
        stem = p.stem.strip()
        if stem and stem.lower() not in _GENERIC_EXE_STEMS:
            return stem
        # Generic stem → nearest meaningful ancestor folder name
        cur = p.parent
        while cur != cur.parent:
            n = cur.name.strip()
            nl = n.lower()
            if n and nl not in _GENERIC_EXE_STEMS and nl not in _CONTAINER_DIR_NAMES:
                # Strip version/build markers and surrounding brackets so
                # "[RJ123456] Super Game v1.2" → "Super Game"
                cleaned = strip_version_tokens(n)
                cleaned = re.sub(r'[\[\]\(\)\{\}]', ' ', cleaned)
                cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' ._-')
                # Peel a TRAILING run of release decorations ("… - Win",
                # "… PC ENG"): the same noise vocabulary the search layer
                # uses, but applied token-by-token from the END only, so
                # interior punctuation of real titles survives untouched
                # (a hyphenated title keeps its hyphen; a folder like
                # "Game v1.2 - Win" derives as just "Game").
                try:
                    from core.game_sources.common import _RELEASE_NOISE
                    while True:
                        m = re.search(r'[\s._\-–—]+([A-Za-z0-9]+)\s*$', cleaned)
                        if not m or m.group(1).lower() not in _RELEASE_NOISE:
                            break
                        cleaned = cleaned[:m.start()].rstrip(' ._-–—')
                except Exception:
                    pass
                return cleaned or n
            cur = cur.parent
        return fallback or stem
    except (OSError, ValueError):
        return fallback or Path(exe_path).stem


def display_name_for_added_file(path: str) -> str:
    """Display name for a file the user added by drag-drop or file picker: an
    executable walks generic stems ("nw", "game"…) up to the install-folder
    name via derive_display_name, while a shortcut (.lnk/.url, or .desktop on
    Linux) keeps its filename stem. Single source of truth for every add entry
    point — including Unix binaries, which are typically extension-less and
    named exactly like the generic stems this is here to replace."""
    from core.resolvers import is_executable_file, is_shortcut_file
    p = Path(path)
    if is_shortcut_file(p):
        return p.stem
    if is_executable_file(p):
        return derive_display_name(path)
    return p.stem


# ── Engine-specific paths ─────────────────────────────────────────────────────

def _renpy_container_roots() -> list[Path]:
    """Ren'Py roaming save containers: AppData/RenPy/<GameFolder> on
    Windows, ~/.renpy/<GameFolder> on Linux, plus ~/Library/RenPy on macOS.
    Same engine host everywhere — only the base directory differs. Shared by
    the name-matched engine scan and the temporal-correlation discovery."""
    roots: list[Path] = []
    if _SYSTEM == "Windows":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            roots.append(Path(appdata) / "RenPy")
    else:
        _home = Path.home()
        roots.append(_home / ".renpy")
        if _SYSTEM == "Darwin":
            roots.append(_home / "Library" / "RenPy")
    return roots


def _latest_write_ts(path_str: str, max_entries_scanned: int = 400) -> float:
    """Most recent mtime across a folder: itself, its direct children (files
    AND subdirs) and one bounded level down — the measuring companion of
    _dir_has_recent_activity, for timestamp CORRELATION between folders."""
    latest = 0.0
    try:
        p = Path(path_str)
        if p.is_file():
            return p.stat().st_mtime
        if not p.is_dir():
            return 0.0
        latest = p.stat().st_mtime
        count = 0
        for child in p.iterdir():
            count += 1
            if count > max_entries_scanned:
                break
            try:
                ts = child.stat().st_mtime
                if ts > latest:
                    latest = ts
                if child.is_dir():
                    for sub in child.iterdir():
                        count += 1
                        if count > max_entries_scanned:
                            break
                        try:
                            ts = sub.stat().st_mtime
                            if ts > latest:
                                latest = ts
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        pass
    return latest


def correlated_engine_paths(exe_path: str, known_paths: list[str],
                            since_ts: float, window_s: Optional[float] = None,
                            anchor_ts: Optional[float] = None,
                            own_game_id: str = "") -> list[str]:
    """Engine-container folders claimed by TEMPORAL correlation.

    Some engines write the same save to two places at once — Ren'Py stores
    a copy under the install dir AND under Roaming/RenPy/<save_directory> —
    but the roaming folder's name comes from the game's INTERNAL title,
    which may share nothing with the library display name, so no amount of
    name matching can link them. What does link them is the CLOCK: the two
    writes land within the same instant. A container subfolder is claimed
    for this game when its latest write falls within *window_s* of the
    latest write in an already-associated path (*anchor_ts* when the caller
    just observed the event). The window is deliberately TIGHT: a same-game
    double write lands milliseconds apart — the narrowness is the
    false-positive guard. A bare fresh mtime is never enough: without a
    fresh write on the already-associated side there is no anchor.

    This is the POLL-side redundancy, scoped to the engine containers
    (cheap: a few dozen stats). The GENERIC any-root version lives in
    core.watcher — event-precise claims of name-filter-rejected events
    (_record_unattributed / _claim_correlated_for_anchor).

    Guards, in order: correlation must be enabled in Settings (it is off by
    default — see save_correlation_enabled); candidate must not already be
    covered by a known path; its activity must postdate the session
    (*since_ts*); it must correlate with the anchor; a folder whose name slug
    matches a DIFFERENT library game is never claimed; and it must contain
    selectable content.

    *window_s* defaults to the configured save_correlation_window_ms; an
    explicit value still wins, so a caller can correlate on its own terms.
    """
    from core.game_engine import detect_engine
    # Decides whether ".dat" counts as content here — see
    # _selectable_skip_sets.
    _engine = detect_engine(exe_path=exe_path) if exe_path else ""
    from core.watcher import correlation_settings
    _enabled, _strong_s, _weak_s = correlation_settings()
    if not _enabled:
        return []
    if window_s is None:
        window_s = _strong_s

    known_fs = [k for k in (known_paths or [])
                if not k.lower().startswith("registry:")]
    if anchor_ts is None:
        anchor_ts = max((_latest_write_ts(k) for k in known_fs), default=0.0)
    if anchor_ts <= 0 or anchor_ts < since_ts - 2.0:
        return []          # nothing fresh on the associated side → no anchor

    known_norm = []
    for k in known_fs:
        try:
            known_norm.append(os.path.normcase(str(Path(k).resolve())))
        except OSError:
            known_norm.append(os.path.normcase(k))

    # Slugs of OTHER library games (name + exe stem): a container folder
    # nominally belonging to another title must never be claimed by this one.
    other_slugs: set = set()
    try:
        from core.library import get_library
        for g in get_library().all_games():
            if own_game_id and g.id == own_game_id:
                continue
            for term in (g.name, Path(g.exe_path).stem if g.exe_path else ""):
                s = re.sub(r"[^a-z0-9]", "", (term or "").lower())
                if len(s) >= 4:
                    other_slugs.add(s)
    except Exception:
        pass

    results: list[str] = []
    for container in _renpy_container_roots():
        try:
            if not container.exists():
                continue
            for child in container.iterdir():
                if not child.is_dir():
                    continue
                try:
                    child_norm = os.path.normcase(str(child.resolve()))
                except OSError:
                    continue
                if any(child_norm == kn or child_norm.startswith(kn + os.sep)
                       or kn.startswith(child_norm + os.sep)
                       for kn in known_norm):
                    continue
                # Trailing numeric build suffix dropped before slugging
                base = re.sub(r'-\d+$', '', child.name)
                cslug = re.sub(r"[^a-z0-9]", "", base.lower())
                if cslug and cslug in other_slugs:
                    continue
                cand_ts = _latest_write_ts(str(child))
                if cand_ts < since_ts - 2.0:
                    continue
                if abs(cand_ts - anchor_ts) > window_s:
                    continue
                if not path_has_backup_content(str(child), engine=_engine):
                    continue
                results.append(str(child))
                logger.info(
                    f"Correlated save folder claimed by write-time match "
                    f"(Δ={abs(cand_ts - anchor_ts):.1f}s): {child}")
        except OSError:
            continue
    return results


def _engine_paths(exe_path: str, game_name: str, appid: Optional[str] = None,
                  extra_terms: Optional[list[str]] = None) -> list[tuple[int, str]]:
    """
    Returns (score, path) tuples for engine-specific known locations.
    Checked before generic scan.

    extra_terms: additional search terms (exe stem, folder name) to try
    alongside game_name when matching AppData subfolders.
    """
    if not exe_path:
        return []

    exe     = Path(exe_path)
    exe_dir = exe.parent
    game_slug = re.sub(r"[^a-z0-9]", "", game_name.lower())
    results: list[tuple[int, str]] = []

    # All slugs to match against: display name + extra terms (exe stem etc.)
    all_slugs: list[str] = [game_slug] if game_slug else []
    for t_ in (extra_terms or []):
        s = re.sub(r"[^a-z0-9]", "", t_.lower())
        if s and s not in all_slugs:
            all_slugs.append(s)

    # Extract appid number from launcher URL (e.g., steam://rungameid/730 -> 730)
    appid_code = None
    if appid:
        appid_code = re.sub(r'^.*?://', '', appid)
        appid_code = re.sub(r'^.*/', '', appid_code)

    # ── Ren'Py ────────────────────────────────────────────────────────────────
    # Local <exe_dir>/game/{saves,save} is handled by the filesystem scan via
    # _score_folder + SAVE_FOLDER_HINTS — no hardcoded path needed here.

    # Ren'Py backup saves in AppData/Roaming/RenPy/<GameFolder>/
    # Folder name often includes version suffix: "MyGameV2" etc.
    renpy_roots = _renpy_container_roots()
    for renpy_roaming in renpy_roots:
        if not renpy_roaming.exists():
            continue
        for child in sorted(renpy_roaming.iterdir()):
            if not child.is_dir():
                continue
            cslug = re.sub(r"[^a-z0-9]", "", child.name.lower())
            # Match: any of our slugs starts the folder slug, or is
            # contained within it (handles version suffixes like v0.1.8)
            matched = False
            for slug in all_slugs:
                if not slug or len(slug) < 2:
                    continue
                if cslug == slug:              # exact
                    matched = True; break
                if cslug.startswith(slug):     # "solv018".startswith("sol")
                    matched = True; break
                if slug.startswith(cslug) and len(cslug) >= 2:
                    matched = True; break
                # Substring match: only for slugs ≥ 4 chars; shorter
                # slugs (e.g. "ps", "fs") match too many unrelated
                # Ren'Py folders via random 2-letter substrings.
                if len(slug) >= 4 and slug in cslug:
                    matched = True; break
            if matched:
                results.append((92, str(child)))

    # ── RPGMaker VX Ace / MV / MZ ────────────────────────────────────────────
    # VX Ace: AppData/Roaming/<GameName>/
    # MV/MZ:  exe_dir/www/save/  or  AppData/Roaming/<company>/<game>/
    if _SYSTEM == "Windows":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            rpg_direct = Path(appdata) / game_name
            try:
                if rpg_direct.exists() and any(rpg_direct.iterdir()):
                    results.append((90, str(rpg_direct)))
            except (PermissionError, OSError):
                pass
            # Slug match in AppData/Roaming
            try:
                for child in Path(appdata).iterdir():
                    if not child.is_dir():
                        continue
                    cslug = re.sub(r"[^a-z0-9]", "", child.name.lower())
                    if game_slug and len(game_slug) >= 6 and (game_slug in cslug or cslug in game_slug) and len(cslug) <= len(game_slug) * 2:
                        if child != rpg_direct:
                            results.append((82, str(child)))
            except OSError:
                pass

    # RPGMaker MV/MZ local save folder
    for rp in [exe_dir / "www" / "save", exe_dir / "save", exe_dir / "SaveData"]:
        if rp.exists():
            results.append((92, str(rp)))

    # Single save files directly beside exe (old RPGMaker XP/VX/Ace without sub-folder)
    _SINGLE_FILE_EXTS = {".sav", ".save", ".rpgsave", ".lsd", ".rxdata", ".rvdata", ".rvdata2", ".rgssad", ".p"}
    try:
        singles = [f for f in exe_dir.iterdir() if f.is_file() and f.suffix.lower() in _SINGLE_FILE_EXTS]
    except (PermissionError, OSError):
        singles = []
    for sf in singles:
        results.append((85, str(sf)))   # return individual save files, not the entire exe_dir

    # ── Unity ─────────────────────────────────────────────────────────────────
    if _SYSTEM == "Windows":
        locallow = Path(os.getenv("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow"
        if locallow.exists():
            try:
                for company in locallow.iterdir():
                    if not company.is_dir(): continue
                    for game_dir2 in company.iterdir():
                        if not game_dir2.is_dir(): continue
                        dslug = re.sub(r"[^a-z0-9]", "", game_dir2.name.lower())
                        
                        # STRONGER MATCHING: Require better similarity for Unity LocalLow paths
                        if game_slug:
                            # Exact match or very close match only
                            if game_slug == dslug:
                                results.append((88, str(game_dir2)))
                            elif len(dslug) >= len(game_slug) - 2 and len(dslug) <= len(game_slug) + 2:
                                # Only allow if there's substantial overlap
                                overlap = sum(1 for i, c in enumerate(game_slug) if i < len(dslug) and dslug[i] == c)
                                if overlap >= min(len(game_slug), len(dslug)) * 0.7:  # 70% overlap required
                                    results.append((85, str(game_dir2)))
            except OSError:
                pass

    # ── Godot / GameMaker ─────────────────────────────────────────────────────
    if _SYSTEM == "Windows":
        localappdata = os.getenv("LOCALAPPDATA", "")
        if localappdata:
            for child_name in [game_name, game_slug]:
                for base in [Path(localappdata), Path(localappdata) / "Godot" / "app_userdata"]:
                    candidate = base / child_name
                    if candidate.exists():
                        results.append((86, str(candidate)))

    # ── Generic relative paths near exe ──────────────────────────────────────
    for rel, sc in [("saves", 72), ("save", 70), ("SaveData", 70),
                    ("savegame", 68), ("data/saves", 68), ("data", 55), ("user", 50)]:
        p = exe_dir / rel
        if p.exists() and p.is_dir():
            results.append((sc, str(p)))
    
    # Broader scan: any subfolder whose name contains save-related keywords
    # Catches variants like "savedata_zh", "saves_cloud", "savegame_backup", etc.
    _save_keywords = ["save", "savedata", "savegame", "userdata"]
    try:
        for child in exe_dir.iterdir():
            if not child.is_dir():
                continue
            cname = child.name.lower()
            if any(kw in cname for kw in _save_keywords):
                if str(child) not in [p for _, p in results]:
                    results.append((68, str(child)))
    except (PermissionError, OSError):
        pass

    # ── Appid-based paths (look for folders named like the appid) ────────────
    if appid_code:
        # Check near exe
        for rel in [appid_code, f"appid_{appid_code}"]:
            p = exe_dir / rel
            if p.exists() and p.is_dir():
                results.append((75, str(p)))
        
        # Check common launcher userdata paths (Steam, Epic, GOG, etc.)
        # These are typically in Program Files but also in user directories.
        # We use _get_launcher_install_paths from resolvers so that Steam
        # installations on non-C: drives (D:, E:, etc.) are also found.
        launcher_userdata_roots = []
        if _SYSTEM == "Windows":
            localappdata = os.getenv("LOCALAPPDATA", "")
            
            from core.resolvers import _get_launcher_install_paths
            launcher_paths = _get_launcher_install_paths()
            
            # Steam userdata paths from every detected Steam install
            for steam_path in launcher_paths.get("steam", []):
                userdata = Path(steam_path) / "userdata"
                if userdata.exists():
                    launcher_userdata_roots.append(userdata)
            
            # LocalAppData paths for all launchers
            if localappdata:
                for launcher in ["Steam", "Epic Games", "GOG Galaxy"]:
                    p = Path(localappdata) / launcher / "userdata"
                    if p.exists():
                        launcher_userdata_roots.append(p)
            
            # Epic/GOG userdata from their install directories
            for launcher_key in ["epic", "gog"]:
                for install_path in launcher_paths.get(launcher_key, []):
                    userdata = Path(install_path) / "userdata"
                    if userdata.exists() and userdata not in launcher_userdata_roots:
                        launcher_userdata_roots.append(userdata)
        elif _SYSTEM == "Linux":
            home = Path.home()
            launcher_userdata_roots = [
                home / ".steam" / "steam" / "userdata",
                home / ".local" / "share" / "Steam" / "userdata",
            ]
        elif _SYSTEM == "Darwin":
            home = Path.home()
            launcher_userdata_roots = [
                home / "Library" / "Application Support" / "Steam" / "userdata",
            ]
        
        for userdata_root in launcher_userdata_roots:
            if userdata_root.exists():
                try:
                    for user_folder in userdata_root.iterdir():
                        if not user_folder.is_dir(): continue
                        appid_folder = user_folder / appid_code
                        if appid_folder.exists():
                            remote = appid_folder / "remote"
                            if remote.exists():
                                results.append((90, str(remote)))
                            else:
                                results.append((85, str(appid_folder)))
                except OSError:
                    pass

    return results


# ── Process open-file tracking ────────────────────────────────────────────────

# Extensions that are never save files even when opened by the game process
_NON_SAVE_EXTENSIONS = frozenset({
    '.exe', '.dll', '.so', '.dylib', '.pdb', '.lib', '.a', '.o',
    '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tga', '.dds',
    '.mp3', '.ogg', '.wav', '.flac', '.m4a', '.opus',
    '.mp4', '.webm', '.avi', '.mkv', '.ogv',
    '.ttf', '.otf', '.woff', '.woff2',
    '.vert', '.frag', '.glsl', '.hlsl', '.spv',
    '.pak', '.bsa', '.ba2',      # game asset archives (never saves)
    '.log', '.txt',               # log / readme text files (separate from saves)
    # Detection-excluded formats (see constants.DETECTION_SKIP_EXTENSIONS)
    '.info', '.html', '.htm', '.dat', '.bin',
})


# Matches a filename as (prefix)(number)(suffix) -- used to recognise
# numbered save-slot siblings such as save1.dat / save2.dat or
# slot_03.sav / slot_07.sav, where only the digits differ.
_SAVE_SLOT_RE = re.compile(r'^(.*?)(\d+)(\D*)$')


def _looks_like_save_sibling(name_a: str, name_b: str) -> bool:
    """True if *name_b* is the same save-slot family as *name_a* -- identical
    prefix and suffix text, with only the numeric slot differing (so
    save2.dat recently written pulls in save1.dat even though the latter
    wasn't touched this session, but leaves unrelated files like
    settings.ini or readme.txt alone).
    """
    if name_a == name_b:
        return False
    ma = _SAVE_SLOT_RE.match(name_a)
    mb = _SAVE_SLOT_RE.match(name_b)
    if not ma or not mb:
        return False
    return ma.group(1) == mb.group(1) and ma.group(3) == mb.group(3)


def _dir_has_recent_activity(path_str: str, since_ts: float, slack: float = 2.0,
                              max_entries_scanned: int = 300) -> bool:
    """Cheap freshness check for a candidate save location: True if the
    directory itself, anything directly inside it, or any file ONE level
    deeper has an mtime at/after *since_ts*. Bounded (max_entries_scanned)
    and never deeper than one nested level — this runs on every
    live-tracking poll tick, so it must stay fast. The one-level descent
    matters for engines that nest saves in a subfolder (e.g. Ren'Py's
    Roaming/RenPy/<game>/saves): an in-place overwrite down there never
    bumps the <game> folder's own mtime, so a direct-children-only check
    missed those saves entirely.

    Used to validate engine-known-location candidates (e.g. Ren'Py's
    Roaming saves folder) during live tracking, where relying solely on a
    momentary open-file-handle snapshot (_live_save_paths) misses games
    that do fast atomic (open-write-close) saves between poll ticks — the
    write completes and the handle closes well within a single polling
    interval, so the snapshot approach can see nothing even though the
    save genuinely just happened. Checking the filesystem's own mtime
    instead catches this regardless of how briefly the file was open.
    """
    try:
        p = Path(path_str)
        if p.is_file():
            return p.stat().st_mtime >= since_ts - slack
        if not p.is_dir():
            return False
        try:
            if p.stat().st_mtime >= since_ts - slack:
                return True
        except OSError:
            pass
        count = 0
        for child in p.iterdir():
            count += 1
            if count > max_entries_scanned:
                break
            try:
                # A fresh mtime counts for files AND subdirs: a subdir's own
                # mtime bumps on create/rename inside it (atomic-save pattern).
                if child.stat().st_mtime >= since_ts - slack:
                    return True
                if child.is_dir():
                    # One bounded level deeper for in-place overwrites that
                    # leave the subdir's mtime untouched.
                    for sub in child.iterdir():
                        count += 1
                        if count > max_entries_scanned:
                            break
                        try:
                            if sub.is_file() and sub.stat().st_mtime >= since_ts - slack:
                                return True
                        except OSError:
                            continue
            except OSError:
                continue
        return False
    except OSError:
        return False


def _live_save_paths(pid: int) -> list[str]:
    """Track save-file directories by following the game's process tree,
    narrowed to files with actual *write* evidence since launch.

    Strategy: collect every file that the game process **and all its
    children** currently have open (ancestry -- not file names, extensions,
    or timestamps -- is what proves a file belongs to the game; this is
    accurate even for generically-named saves in unexpected directories),
    then keep only the ones whose mtime shows they were modified or created
    *during this session* (at/after the process's own start time). A file
    merely opened for reading -- a texture, a shader cache, a config the
    game loaded at boot -- has an old mtime and is correctly excluded, which
    is what previously caused nearly the whole game folder to be swept in:
    any directory containing *any* opened file qualified, write or not.

    For every file that does qualify, same-folder "sibling" files matching
    the same save-slot naming pattern (save2.dat just written -> save1.dat)
    are pulled in too, even though their own mtime is old -- they're
    unmistakably part of the same save set, just not the active slot.

    Returns a list of directory paths that contain at least one file with
    write evidence this session (or a same-pattern sibling of one).
    """
    try:
        import psutil
        proc = psutil.Process(pid)
    except Exception as e:
        logger.debug(f"Live tracking: cannot attach to PID {pid}: {e}")
        return []

    # Collect the full process tree: game process + all descendants
    try:
        all_procs = [proc] + proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        all_procs = [proc]

    exe_lower = ""
    exe_dir: Optional[Path] = None
    try:
        exe_lower = proc.exe().lower()
        exe_dir = Path(proc.exe()).parent
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Reference point for "recent": when the game itself started. Anything
    # written at/after this is evidence of an active save this session;
    # anything older is pre-existing data the player didn't just touch.
    try:
        launch_ts = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        launch_ts = 0.0
    _MTIME_SLACK_S = 2.0  # filesystem/clock granularity safety margin

    written_files: set[Path] = set()

    for p in all_procs:
        try:
            open_fds = p.open_files()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        for f in open_fds:
            fpath = f.path
            if not fpath:
                continue

            # Skip the game exe itself
            if exe_lower and fpath.lower() == exe_lower:
                continue

            fp = Path(fpath)

            # Skip known non-save extensions (assets, libraries, logs...)
            if fp.suffix.lower() in _NON_SAVE_EXTENSIONS:
                continue

            # Skip files whose stem is always noise regardless of extension
            # (e.g. a "log" file written as log.dat or with no extension) —
            # the shared exclusion the backup/watcher/detector paths already use.
            if fp.stem.lower() in SKIP_FILENAME_STEMS:
                continue

            # Skip known system / engine directories
            f_parts = {part.lower() for part in fp.parts}
            if f_parts & _SKIP_DIRS:
                continue

            # Ren'Py ships its own interpreter/common-code in a "renpy"
            # folder sitting right next to the game's exe — never saves.
            # This is intentionally NOT a name-based/global skip (unlike
            # _SKIP_DIRS above): the real save location Ren'Py itself
            # writes to is the OS profile's Roaming/AppData folder, which
            # is *also* named "renpy"/"RenPy" and must never be excluded.
            # Checking "is this file under the game's own exe_dir" is what
            # correctly tells the two apart.
            if exe_dir is not None and "renpy" in f_parts:
                try:
                    fp.relative_to(exe_dir)
                    continue   # under exe_dir -> the bundled interpreter, skip
                except ValueError:
                    pass       # not under exe_dir -> e.g. the real Roaming save folder, keep

            # Skip obvious Windows/Linux system prefixes
            fpath_l = fpath.lower().replace('\\', '/')
            if any(sys_p in fpath_l for sys_p in (
                '/windows/', '/system32/', '/program files/',
                '/program files (x86)/', '/windowsapps/',
            )):
                continue

            # An open handle alone doesn't say whether the process is
            # reading (assets, configs -- opened constantly, never saves)
            # or writing (saves). The filesystem timestamp does: only a
            # file actually modified/created since the game launched counts
            # as save evidence.
            try:
                if fp.stat().st_mtime < launch_ts - _MTIME_SLACK_S:
                    continue
            except OSError:
                continue

            written_files.add(fp)
            logger.debug(
                f"Live tracking [pid={p.pid} {p.name()!r}]: write evidence -> {fp}"
            )

    if not written_files:
        return []

    # Pull in same-folder, same-naming-pattern siblings of every file with
    # write evidence (save2.dat written -> save1.dat joins it), without
    # sweeping in unrelated files that merely live in the same directory.
    all_files: set[Path] = set(written_files)
    dirs_with_evidence = {f.parent for f in written_files}
    for d in dirs_with_evidence:
        try:
            siblings = [c for c in d.iterdir() if c.is_file()]
        except OSError:
            continue
        written_here = [f for f in written_files if f.parent == d]
        for sib in siblings:
            if sib in all_files:
                continue
            if any(_looks_like_save_sibling(w.name, sib.name) for w in written_here):
                all_files.add(sib)
                logger.debug(f"Live tracking: sibling match -> {sib}")

    # Folder entries for dedicated save dirs; per-FILE entries when the
    # written files sit in an install/root folder (the dir contains a game
    # program, or is the game exe's own dir) — backing up the whole install
    # folder for a couple of Save*.rxdata beside the exe is exactly the
    # "spread" this avoids. Program detection is platform-aware: on Unix the
    # binary is usually extension-less, so an ".exe" test would never fire
    # and every Linux install root fell through to the whole-folder branch.
    from core.resolvers import is_program_binary
    result_set: set[str] = set()
    for d in {f.parent for f in all_files}:
        is_install_root = (exe_dir is not None and d == exe_dir)
        if not is_install_root:
            try:
                is_install_root = any(
                    c.is_file() and is_program_binary(c) for c in d.iterdir()
                )
            except OSError:
                is_install_root = False
        if is_install_root:
            for f in all_files:
                if f.parent == d:
                    result_set.add(str(f))
        else:
            result_set.add(str(d))
    result = list(result_set)
    logger.info(
        f"Live tracking: {len(result)} save entrie(s) / {len(all_files)} file(s) "
        f"with write evidence from PID {pid} (tree: {len(all_procs)} process(es))"
    )
    return result


# ── Platform watch roots ──────────────────────────────────────────────────────

# ── Wine / Proton prefixes (Linux, and Wine on macOS) ────────────────────────
# A Windows game running under Proton or Wine writes exactly where it would on
# Windows — only the whole C: drive is a folder inside a "prefix". So the saves
# are not in any XDG location: they are under
# <prefix>/drive_c/users/<user>/AppData/... and the existing Windows-shaped
# heuristics find them unchanged, once pointed at the right root.
#
# Only the USER directory of each prefix is offered as a root, never the prefix
# itself: the rest of it is a synthetic Windows install (windows/, Program
# Files, the registry hives) that holds no player data and would multiply the
# scan cost by the size of a Windows tree, per game.

def _steam_library_roots() -> list[Path]:
    """Every Steam library on this machine, from libraryfolders.vdf.

    Games are routinely installed on a second drive, and their Proton prefix
    lives beside them — looking only under ~/.steam would miss those.
    """
    roots: list[Path] = []
    seen: set[str] = set()
    home = Path.home()
    candidates = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        home / "Library" / "Application Support" / "Steam",
    ]
    for base in candidates:
        if not base.exists():
            continue
        if str(base) not in seen:
            seen.add(str(base))
            roots.append(base)
        vdf = base / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # The file is Valve's KeyValues format; every library appears as a
        # "path" entry. Matching that one key avoids depending on a parser
        # for a format whose surrounding shape has changed between clients.
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = Path(m.group(1))
            if p.exists() and str(p) not in seen:
                seen.add(str(p))
                roots.append(p)
    return roots


# The save-bearing folders inside a prefix user directory. This mirrors the
# Windows side of WATCH_PATHS_TEMPLATES on purpose: the scanner walks a bounded
# number of levels below each root it is given, so handing it the user
# directory instead of these leaves the real save folders out of reach — the
# depth budget is spent crossing AppData/LocalLow/<publisher> before the game's
# own folder is ever reached.
_PREFIX_SAVE_SUBDIRS = (
    "AppData/Roaming",          # {APPDATA}
    "AppData/Local",            # {LOCALAPPDATA}
    "AppData/LocalLow",
    "Documents",
    "Documents/My Games",
    "Documents/Electronic Arts",
    "Documents/Rockstar Games",
    "Saved Games",
)


def _prefix_user_dirs(prefix: Path) -> list[Path]:
    """Save-bearing directories inside a Wine/Proton prefix."""
    users = prefix / "drive_c" / "users"
    if not users.is_dir():
        return []
    out = []
    try:
        for d in users.iterdir():
            # "Public" holds shared shell folders, not player data.
            if not d.is_dir() or d.name.lower() == "public":
                continue
            for rel in _PREFIX_SAVE_SUBDIRS:
                p = d.joinpath(*rel.split("/"))
                if p.is_dir():
                    out.append(p)
    except OSError:
        pass
    return out


# Without an appid every prefix on the machine is a candidate, and a library
# can hold hundreds — each one walked several levels deep, for every search
# term. Only the most recently written prefixes are kept: a prefix's mtime
# moves when the game runs, so this is "what has actually been played".
_MAX_SCANNED_PREFIXES = 16


def _recent_dirs(dirs: list[Path], limit: int = _MAX_SCANNED_PREFIXES) -> list[Path]:
    """The *limit* most recently modified directories, newest first."""
    if len(dirs) <= limit:
        return dirs
    def when(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(dirs, key=when, reverse=True)[:limit]


def _proton_prefix_dirs(appid: str = "") -> list[Path]:
    """User directories of Proton prefixes.

    With *appid* known this goes straight to that game's prefix — one stat
    instead of walking every prefix on the machine, which matters because a
    Steam library can hold hundreds.
    """
    out: list[Path] = []
    for lib in _steam_library_roots():
        compat = lib / "steamapps" / "compatdata"
        if not compat.is_dir():
            continue
        if appid:
            out.extend(_prefix_user_dirs(compat / str(appid) / "pfx"))
            continue
        try:
            entries = [e for e in compat.iterdir() if e.is_dir()]
        except OSError:
            continue
        for entry in _recent_dirs(entries):
            out.extend(_prefix_user_dirs(entry / "pfx"))
    return out


def _wine_prefix_dirs() -> list[Path]:
    """User directories of plain Wine prefixes and the common managers."""
    home = Path.home()
    out: list[Path] = []
    explicit = os.getenv("WINEPREFIX", "")
    if explicit:
        out.extend(_prefix_user_dirs(Path(explicit)))
    out.extend(_prefix_user_dirs(home / ".wine"))
    # Managers keep one prefix per game under a predictable parent. The cap
    # applies to the union, not to each parent: capping per parent would let
    # five populated managers through at five times the intended ceiling.
    managed: list[Path] = []
    for parent in (home / ".local" / "share" / "bottles" / "bottles",
                   home / "Games" / "Heroic" / "Prefixes",
                   home / ".config" / "heroic" / "tools" / "wine",
                   home / ".local" / "share" / "lutris" / "runners" / "winesteam" / "prefix",
                   home / "Games"):
        if not parent.is_dir():
            continue
        try:
            managed.extend(e for e in parent.iterdir() if e.is_dir())
        except OSError:
            continue
    for entry in _recent_dirs(managed):
        out.extend(_prefix_user_dirs(entry))
    return out


def compat_prefix_roots(appid: str = "") -> list[Path]:
    """Save roots inside Wine/Proton prefixes. Empty on Windows.

    With *appid* this answers one question only — "where does THIS Steam game
    save under Proton" — and leaves the standalone Wine prefixes out: they
    belong to other games, and the caller adds them anyway through the
    no-appid call that builds the general watch list.
    """
    if _SYSTEM == "Windows":
        return []
    try:
        found = _proton_prefix_dirs(appid)
        if not appid:
            found += _wine_prefix_dirs()
    except Exception as e:
        logger.debug(f"Wine/Proton prefix lookup failed: {e}")
        return []
    out, seen = [], set()
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    if out:
        logger.debug(f"Wine/Proton save roots: {len(out)}"
                     + (f" (appid {appid})" if appid else ""))
    return out


def _resolve_watch_paths(config=None) -> list[Path]:
    paths: list[Path] = []
    if config is None:
        config = get_config()
    if _SYSTEM == "Windows":
        env = {
            "APPDATA":      os.getenv("APPDATA", ""),
            "LOCALAPPDATA": os.getenv("LOCALAPPDATA", ""),
            "USERPROFILE":  os.getenv("USERPROFILE", str(Path.home())),
        }
        templates = list(WATCH_PATHS_TEMPLATES)
        for extra in config.get("extra_watch_paths", []):
            templates.append(extra)
        for tmpl in templates:
            resolved = tmpl
            for k, v in env.items():
                resolved = resolved.replace(f"{{{k}}}", v)
            p = Path(resolved)
            if p.exists():
                paths.append(p)
    elif _SYSTEM == "Darwin":
        home = Path.home()
        for d in [home/"Library"/"Application Support", home/"Library"/"Preferences",
                  home/"Documents", home/"Saved Games"]:
            if d.exists(): paths.append(d)
    else:
        home = Path.home()
        xdg_data = Path(os.getenv("XDG_DATA_HOME", str(home/".local"/"share")))
        xdg_cfg  = Path(os.getenv("XDG_CONFIG_HOME", str(home/".config")))
        for d in [xdg_data, xdg_cfg, home/".steam"/"steam"/"userdata",
                  home/"snap", home/".var"/"app"]:
            if d.exists(): paths.append(d)
        # Windows games under Proton/Wine save inside their prefix, which no
        # XDG path covers. Without an appid here every prefix is offered; the
        # detector narrows to the game's own when it knows it.
        paths.extend(compat_prefix_roots())
    for extra in config.get("extra_watch_paths", []):
        p = Path(extra)
        if _SYSTEM != "Windows" and p.exists() and p not in paths:
            paths.append(p)
    return paths


# ── Registry detection (Windows only) ────────────────────────────────────────

def _registry_save_paths(game_name: str, hkcu_only: bool = False) -> list[str]:
    """
    Enhanced registry detection that looks for specific game entries
    rather than broad substring matches.

    NOTE: this extracts FOLDER PATHS stored as string values under a key
    whose name matches the game — it does not (and cannot) surface saves
    kept purely inside registry values (e.g. Unity PlayerPrefs binaries):
    the backup engine is file-based.

    hkcu_only skips the HKLM sweep — used by the live-tracking poll, where
    a full HKLM\\Software walk every cycle would be far too heavy.
    """
    if _SYSTEM != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    
    slug = re.sub(r"[^a-z0-9]", "", game_name.lower())
    found: list[str] = []

    def _scan_key(hkey, sub_key: str, depth: int = 0):
        if depth > 3:  # Limit depth to avoid scanning too deep
            return
        try:
            with winreg.OpenKey(hkey, sub_key, 0, winreg.KEY_READ) as key:
                key_name = sub_key.split("\\")[-1].lower()
                key_slug = re.sub(r"[^a-z0-9]", "", key_name)
                
                # STRICTER MATCHING: Require stronger similarity for registry keys
                if slug and len(slug) >= 4:
                    # Exact match or very close match
                    if slug == key_slug:
                        # Check values for save paths
                        idx = 0
                        while True:
                            try:
                                vname, vdata, vtype = winreg.EnumValue(key, idx)
                                if vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                                    expanded = os.path.expandvars(str(vdata))
                                    p = Path(expanded)
                                    # Only accept if it looks like a save path
                                    if p.is_dir() and len(expanded) > 3:
                                        # Additional validation: check if path contains save-related terms
                                        path_lower = str(p).lower()
                                        save_indicators = ['save', 'data', 'user', 'profile', 'progress']
                                        if any(indicator in path_lower for indicator in save_indicators):
                                            found.append(str(p))
                                idx += 1
                            except OSError:
                                break
                
                # Only recurse into subkeys that might be game-related
                sub_idx = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, sub_idx)
                        child_lower = child.lower()
                        
                        # Skip obvious non-game keys
                        skip_keys = {'microsoft', 'windows', 'system', 'software', 'classes', 
                                   'policies', 'secure', 'trusted', 'network', 'internet'}
                        if child_lower not in skip_keys:
                            _scan_key(hkey, f"{sub_key}\\{child}", depth + 1)
                        sub_idx += 1
                    except OSError:
                        break
        except (OSError, PermissionError):
            pass

    # Scan both HKCU\Software and HKLM\Software for broader coverage
    _scan_key(winreg.HKEY_CURRENT_USER, "Software")
    if not hkcu_only:
        _scan_key(winreg.HKEY_LOCAL_MACHINE, "Software")

    return found


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_folder(folder: Path, game_name: str, hints: list[str], exe_path: Optional[str] = None) -> int:
    """
    Enhanced scoring system that prioritizes exact game name/developer matches
    and root application paths over generic save folder hints.
    """
    score = 0
    name = folder.name.lower()
    path_str = str(folder).lower().replace("\\", "/")

    # Never score SaveSync's own data directory
    try:
        if str(folder.resolve()).lower().startswith(_OWN_DATA_DIR):
            return 0
    except (OSError, ValueError):
        pass
    game_slug = re.sub(r"[^a-z0-9]", "", game_name.lower())

    # Skip obvious non-save directories
    if name in _SKIP_DIRS:
        return 0
    for skip in _SKIP_DIRS:
        if f"/{skip}/" in path_str:
            return 0

    path_parts_slugged = [re.sub(r"[^a-z0-9]", "", part) for part in path_str.split("/")]
    path_slug = re.sub(r"[^a-z0-9]", "", path_str)

    # Build context slugs: game name + exe stem + parent folder
    context_slugs = []
    if game_slug and len(game_slug) >= 2:
        context_slugs.append(game_slug)
    if exe_path:
        exe_stem = Path(exe_path).stem.lower()
        if exe_stem not in _GENERIC_EXE_STEMS:
            exe_slug = re.sub(r"[^a-z0-9]", "", exe_stem)
            if exe_slug and len(exe_slug) > 3:
                context_slugs.append(exe_slug)
            else:
                # Stem too short or 3 chars (e.g. "ps", "pro", "run") — would hit
                # unrelated words like "apocalypse".  Use parent folder instead.
                _short_parent = re.sub(r"[^a-z0-9]", "", Path(exe_path).parent.name.lower())
                if _short_parent and len(_short_parent) >= 3:
                    context_slugs.append(_short_parent)
        else:
            parent_slug = re.sub(r"[^a-z0-9]", "", Path(exe_path).parent.name.lower())
            if parent_slug:
                context_slugs.append(parent_slug)

    # HIGH PRIORITY: Exact game name or developer match in path
    for slug in context_slugs:
        if len(slug) < 3:  # ≥3 chars required: prevents "ps" matching inside "apocalypse"
            continue
        if path_parts_slugged and slug == path_parts_slugged[-1]:  # Exact folder name match
            score += 120
            break
        elif slug in path_slug:
            for i, part in enumerate(path_parts_slugged):
                if slug in part and len(part) >= len(slug):
                    if i == len(path_parts_slugged) - 1:  # Last folder
                        if len(part) <= len(slug) + 3:
                            score += 90
                        else:
                            score += 40
                    else:  # Parent folder
                        score += 60
                    break
            break
    
    # MEDIUM PRIORITY: Known developer/publisher patterns
    # Only contribute if game name context is also present — otherwise
    # "steam"/"microsoft" in a path inflates scores for unrelated games.
    known_developers = {
        'valve', 'steam', 'epic', 'ubisoft', 'ea', 'electronic arts',
        'bethesda', 'rockstar', 'blizzard', 'activision', 'square enix',
        'capcom', 'konami', 'sega', 'nintendo', 'sony', 'microsoft'
    }
    
    has_any_context = any(slug in path_slug for slug in context_slugs if len(slug) >= 3)
    if has_any_context:
        for dev in known_developers:
            if dev in path_str:
                score += 25
                break
    
    # MEDIUM PRIORITY: Engine-specific validated paths only
    engine_validated_paths = [
        'locallow',  # Unity specific
        'saved',     # Unreal Engine specific  
        'renpy',     # Ren'Py specific
        'rpgmaker',  # RPG Maker specific
        'godot',     # Godot specific
    ]
    
    for engine_path in engine_validated_paths:
        if engine_path in path_str:
            # Only give engine points if there's also some game name context
            if any(slug in path_str for slug in context_slugs if len(slug) >= 3):
                score += 40
            else:
                score += 15  # Lower without game context
            break
    
    # LOW PRIORITY: Save folder hints (only if no higher priority matches
    # AND there's game-relevant context in the path).
    # Without game context, a save folder from a completely different game
    # must not receive hint-based points and should be penalised.
    has_context = any(slug in path_slug for slug in context_slugs if len(slug) >= 3)
    if has_context:  # Only consider hints with game context
        for hint in hints:
            if hint == name or hint in name.split("_") or name.startswith(hint):
                score += 20
                break
            if hint in name:
                score += 10
                break

    # PENALTY: Folders whose name looks like a save folder but have NO
    # game-name context in the entire path — they belong to another game.
    if not has_context:
        folder_save_hint = any(name.startswith(h) or h in name for h in hints)
        if folder_save_hint:
            score = max(0, score - 30)
    
    # PENALTY: Generic locations without strong game context
    if 'locallow' in path_str or 'appdata/local' in path_str:
        if score < 70:  # Only penalize if we don't have strong evidence
            score -= 20  # Significant penalty for generic locations
        elif score < 85:
            score -= 10  # Smaller penalty for moderate evidence
    
    # CONTENT ANALYSIS: Only consider content if we have some path relevance
    if score >= 20:  # Only check content for folders that already have some relevance
        try:
            entries = list(folder.iterdir())
            if not entries:
                return max(0, score - 10)  # Small penalty for empty folders
            
            files = [e for e in entries if e.is_file()]
            
            # Files with save extensions - strong signal
            save_files = [f for f in files if f.suffix.lower() in _SAVE_EXTENSIONS]
            if save_files:
                score += min(25, len(save_files) * 5)  # Reduced weight
            else:
                # No save files: if every file present has a skip-extension
                # (or a skip filename stem, e.g. "log"/"logs" regardless of
                # extension), this is an asset-only folder with no backup
                # content.
                _skip_all = SKIP_EXTENSIONS | DETECTION_SKIP_EXTENSIONS
                all_skippable = all(
                    f.suffix.lower() in _skip_all or not f.suffix
                    or f.stem.lower() in SKIP_FILENAME_STEMS
                    for f in files
                ) if files else True
                if all_skippable:
                    return max(0, score - 10)

            # Penalize directories with too many non-save files
            non_save_files = [f for f in files if f.suffix.lower() not in _SAVE_EXTENSIONS and 
                            f.suffix.lower() not in SKIP_EXTENSIONS and
                            f.stem.lower() not in SKIP_FILENAME_STEMS]
            if len(non_save_files) > 50:
                score -= 30  # Heavy penalty for directories with many non-save files
            elif len(non_save_files) > 20:
                score -= 15
                
            # Focus bonus: small, focused directories are more likely saves
            if len(entries) < 15:
                score += 5
            elif len(entries) > 100:
                score -= 15
                
        except (PermissionError, OSError):
            return 0
    
    # STRICTER THRESHOLDS
    # Require stronger evidence for positive identification
    return max(0, min(score, 120))


# ── Public API ────────────────────────────────────────────────────────────────

def _is_valid_save_context(path: Path, game_name: str, exe_path: Optional[str] = None, engine_detected: bool = False) -> bool:
    """
    Final validation filter that checks if a path has legitimate save context.
    This prevents false positives from generic directories.

    If engine_detected is True the path was found by an engine-specific detector
    and the save-indicator string check is skipped.
    """
    # Engine-specific detectors have high confidence; skip the string check
    if engine_detected:
        return True

    path_str = str(path).lower()
    game_slug = re.sub(r"[^a-z0-9]", "", game_name.lower())

    generic_names = {'game', 'app', 'application', 'program', 'main'}
    is_generic_name = game_name.lower() in generic_names

    has_game_context = False
    if game_slug and game_slug in re.sub(r"[^a-z0-9]", "", path_str):
        has_game_context = True

    # Standard user-data roots where game folders often live without a "saves" subdirectory
    standard_roots = ['appdata', 'documents', 'my documents', 'userprofile']
    in_standard_root = any(r in path_str for r in standard_roots)

    # Strong game-name context in the path always validates — the folder
    # may be the game root itself (e.g. "SuperGameStoryRPG") which
    # doesn't contain "save" in the name but IS the game's install dir.
    if has_game_context and not is_generic_name:
        return True

    if is_generic_name:
        save_indicators = ['save', 'data', 'user', 'profile', 'progress']
    else:
        save_indicators = ['save', 'data', 'user', 'profile', 'progress']

    has_save_indicator = any(indicator in path_str for indicator in save_indicators)

    # A folder in AppData/Documents that exactly matches the game slug is always
    # a valid candidate — games like "sol" store saves directly in
    # AppData/Roaming/sol/ without any "saves" subdirectory in the path.
    if has_game_context and in_standard_root and not is_generic_name:
        return True

    if not has_save_indicator:
        return False
    
    # For generic names, require much stronger evidence
    if is_generic_name:
        # Must be in a very specific save directory structure
        folder_name = path.name.lower()
        strong_save_folders = ['saves', 'save', 'savedata', 'save_data', 'savegame', 'save_game']
        
        if folder_name not in strong_save_folders:
            return False
        
        # And must be reasonably close to exe or in standard location
        if exe_path:
            exe_dir = Path(exe_path).parent
            try:
                # Check if it's reasonably close to exe
                if _is_relative_to(path, exe_dir):
                    relative_parts = len(path.relative_to(exe_dir).parts)
                    return relative_parts <= 2  # Max 2 levels deep from exe for generic names
            except (ValueError, AttributeError, OSError):
                pass
        
        # Check if it's in standard save locations
        standard_save_roots = ['appdata', 'documents']
        if any(root in path_str for root in standard_save_roots):
            return True
        
        return False
    
    # If no game context, be much stricter
    # Only allow if it's a very clear save directory structure
    folder_name = path.name.lower()
    strong_save_folders = ['saves', 'save', 'savedata', 'save_data', 'savegame', 'save_game']
    
    if folder_name in strong_save_folders:
        # Additional check: must be in a reasonable location
        if exe_path:
            exe_dir = Path(exe_path).parent
            try:
                # Check if it's reasonably close to exe
                if _is_relative_to(path, exe_dir):
                    relative_parts = len(path.relative_to(exe_dir).parts)
                    return relative_parts <= 3  # Max 3 levels deep from exe
            except (ValueError, AttributeError, OSError):
                pass
        
        # Check if it's in standard save locations
        standard_save_roots = ['appdata', 'documents', 'program files']
        if any(root in path_str for root in standard_save_roots):
            return True
    
    return False


@dataclass
class PathValidation:
    """Result of validating a save path."""
    ok: bool
    warning: str = ""


def validate_save_path(path_str: str) -> PathValidation:
    """Check if a path looks like a valid save location.
    Returns ok=True if usable, ok=False with a warning message otherwise."""
    from core.registry_saves import is_registry_path, registry_key_exists
    if is_registry_path(path_str):
        # Virtual registry entry: valid when the key exists (and inside the
        # allowed HKCU\Software area — registry_key_exists enforces that).
        if registry_key_exists(path_str):
            return PathValidation(ok=True)
        return PathValidation(ok=False, warning=i18n.t('errors.path_not_found'))
    p = Path(path_str)

    if not p.exists():
        return PathValidation(ok=False, warning=i18n.t('errors.path_not_found'))

    if p.is_file():
        # Individual file — always ok (e.g. single .sav beside exe)
        return PathValidation(ok=True)

    if not p.is_dir():
        return PathValidation(ok=False, warning=i18n.t('errors.path_not_file_or_directory'))

    try:
        entries = list(p.iterdir())
    except (PermissionError, OSError):
        return PathValidation(ok=False, warning=i18n.t('errors.cannot_read_directory'))

    if not entries:
        return PathValidation(ok=False, warning=i18n.t('errors.folder_empty_no_saves'))

    files = [e for e in entries if e.is_file()]
    if not files:
        return PathValidation(ok=False, warning=i18n.t('errors.folder_contains_no_files'))

    # Check if any file has a save-like extension
    _NON_SAVE_ONLY = {".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".lib", ".pdb", ".sys"}
    exts = {f.suffix.lower() for f in files}
    if exts and exts.issubset(_NON_SAVE_ONLY):
        return PathValidation(ok=False, warning=i18n.t('errors.folder_only_executables'))

    return PathValidation(ok=True)


def detect_save_paths(
    game_name: str,
    exe_path: Optional[str] = None,
    pid: Optional[int] = None,
    appid: Optional[str] = None,
    live_only: bool = False,
    correlate_paths: Optional[list[str]] = None,
) -> list[str]:
    """
    Detect save data locations for a game. Results sorted by confidence.
    
    Args:
        game_name: Display name of the game
        exe_path:  Path to the game executable (used for relative searches)
        pid:       Running PID (enables live open-file detection, most accurate)
        live_only: Only use live open-file tracking; skip engine/registry/fs scans
    """
    _begin_detection_run()   # cancels from earlier runs must not leak in
    if not game_name:
        game_name = ""
    config = get_config()
    hints  = config.get("save_folder_hints", SAVE_FOLDER_HINTS)
    logger.info("detect_save_paths: cancel=%s hints=%d terms_start=%s",
                _is_cancelled(), len(hints), game_name[:50])
    seen: set[str] = set()
    results: list[str] = []

    # Build ranked search terms: display name, exe stem (CamelCase-split),
    # install folder stem, and appid number.
    search_terms: list[str] = [game_name] if game_name else []

    if exe_path:
        import re as _re
        exe_stem = Path(exe_path).stem
        _exe_clean = _re.sub(r'[^a-z0-9]', '', exe_stem.lower())
        if exe_stem.lower() not in _GENERIC_EXE_STEMS and len(_exe_clean) > 3:
            # CamelCase → spaced (e.g. "SuperGameStoryRV" → "Super Game Story RV")
            spaced = _re.sub(CAMEL_SPLIT_RE, ' ', exe_stem).strip()
            for t_ in [exe_stem, spaced]:
                if t_ and t_ not in search_terms:
                    search_terms.append(t_)
        elif exe_stem.lower() not in _GENERIC_EXE_STEMS:
            # Stem too short (e.g. "ps.exe") — use parent folder name for matching
            parent_name = Path(exe_path).parent.name
            if parent_name and parent_name.lower() not in _GENERIC_EXE_STEMS:
                spaced_p = _re.sub(CAMEL_SPLIT_RE, ' ', parent_name).strip()
                for t_ in [parent_name, spaced_p]:
                    if t_ and t_ not in search_terms:
                        search_terms.append(t_)
        else:
            # Generic exe — try to get name from parent folder instead
            parent_name = Path(exe_path).parent.name
            if parent_name and parent_name.lower() not in _GENERIC_EXE_STEMS:
                spaced_p = _re.sub(CAMEL_SPLIT_RE, ' ', parent_name).strip()
                for t_ in [parent_name, spaced_p]:
                    if t_ and t_ not in search_terms:
                        search_terms.append(t_)

    if appid:
        import re as _re2
        game_id = _re2.sub(r'^.*?://', '', appid)
        game_id = _re2.sub(r'^.*/', '', game_id)
        if game_id and game_id not in search_terms:
            search_terms.append(game_id)

    # Get user's deselected paths for this game (if available)
    deselected_paths = set()
    from core.library import get_library
    library = get_library()
    try:
        game_entry = library.get_by_exe(exe_path) if exe_path else None
    except Exception:
        game_entry = None
    if game_entry is not None:
        deselected_paths = set(config.get("auto_scan_deselected_paths", {}).get(game_entry.id, []))

    def _add(path_str: str, is_live_result: bool = False, engine_detected: bool = False):
        # Virtual registry entries (registry:HKCU\...) short-circuit every
        # filesystem check below — the linkage/precision work was already
        # done by the strict key matching in core.registry_saves. Only the
        # user-deselection filter and dedupe apply.
        from core.registry_saves import is_registry_path as _is_reg
        if _is_reg(path_str):
            if path_str in deselected_paths:
                return
            key = path_str.lower()
            if key not in seen:
                seen.add(key)
                results.append(path_str)
                logger.debug(f"Added registry save entry: {path_str}")
            return

        # Never detect SaveSync's own data directory as a game save path
        try:
            if str(Path(path_str).resolve()).lower().startswith(_OWN_DATA_DIR):
                logger.debug(f"Skipping own data dir: {path_str}")
                return
        except (OSError, ValueError):
            pass

        # Skip paths whose components match _SKIP_DIRS (lib, savesync, etc.)
        try:
            p_parts = set(part.lower() for part in Path(path_str).parts)
            if p_parts & _SKIP_DIRS:
                logger.debug(f"Skipping path with skip dir: {path_str}")
                return
        except (OSError, ValueError):
            pass

        # Skip paths that user has previously deselected
        if path_str in deselected_paths:
            logger.debug(f"Skipping deselected path: {path_str}")
            return

        key = str(Path(path_str)).lower()
        if key not in seen:
            # Live tracking results get priority — skip final validation
            if is_live_result:
                seen.add(key)
                results.append(path_str)
                logger.debug(f"Added live tracking result: {path_str}")
            else:
                # Validate against all search terms so short exe-stem names
                # like "sol" are found even when game_name is different.
                valid = engine_detected or any(
                    _is_valid_save_context(Path(path_str), term, exe_path, engine_detected)
                    for term in search_terms if term
                )
                if valid:
                    seen.add(key)
                    results.append(path_str)

    # Strategy 1: Live open-file tracking (highest confidence, only when running)
    if pid:
        live_paths = _live_save_paths(pid)
        for p in live_paths:
            _add(p, is_live_result=True)
        logger.info(f"Live tracking strategy found {len(live_paths)} paths")

    # When live_only is set, skip the registry/generic-fs strategies (3-4 —
    # the broad, low-precision sources), but still check known engine-
    # specific locations (Strategy 2) for RECENT activity. This matters for
    # engines that do fast atomic (open→write→close) saves — e.g. Ren'Py
    # writing an autosave on every scene change — where the save file is
    # only open for a fraction of a second and a periodic open-file-handle
    # poll (Strategy 1 above) can easily miss the brief window entirely,
    # even though the file on disk is genuinely brand new. Gating by mtime
    # keeps this precise: a KNOWN location is only trusted if something in
    # it was actually touched since the game launched, so an old/unrelated
    # engine folder still can't sneak in just because it's a recognised path.
    if live_only:
        if exe_path:
            try:
                # Feed the same exe-stem / install-folder slugs the non-live
                # Strategy 2 uses (below), so a roaming engine folder — e.g.
                # Ren'Py's AppData/RenPy/<save_directory> — is matched by the
                # install identity even when its name diverges from the library
                # display name (the case where the roaming save used to be
                # invisible in live tracking).
                _ep = Path(exe_path)
                _live_extra: list[str] = []
                for _t in (_ep.stem, _ep.parent.name):
                    _t = strip_version_tokens(_t or "")
                    if _t and _t.lower() not in _GENERIC_EXE_STEMS:
                        _live_extra.append(_t)
                _engine_hits_live = _engine_paths(exe_path, game_name, appid, extra_terms=_live_extra)
            except Exception:
                _engine_hits_live = []
            since_ts = None
            if pid:
                try:
                    import psutil as _psutil
                    since_ts = _psutil.Process(pid).create_time()
                except Exception:
                    since_ts = None
            if since_ts is None:
                since_ts = time.time() - 300   # conservative fallback: last 5 min
            for score, p in _engine_hits_live:
                if score >= 50 and _dir_has_recent_activity(p, since_ts):
                    _add(p, is_live_result=True)
                    logger.debug(f"Live-only: engine path passed recency check → {p}")

            # Registry-pointed folders (mixed games: progress in files at a
            # folder the game records under its own HKCU key). Same recency
            # gate as engine hits; HKCU only — a full HKLM sweep every
            # 60 s poll would be far too heavy.
            _reg_terms = ([game_name] if game_name else []) + _live_extra
            try:
                _reg_paths: list[str] = []
                for _rt in _reg_terms:
                    for _rp in _registry_save_paths(_rt, hkcu_only=True):
                        if _rp not in _reg_paths:
                            _reg_paths.append(_rp)
                for p in _reg_paths:
                    if _dir_has_recent_activity(p, since_ts):
                        _add(p, is_live_result=True)
                        logger.debug(f"Live-only: registry folder passed recency check → {p}")
            except Exception as e:
                logger.debug(f"Live-only registry check failed: {e}")

            # Temporal correlation: an engine-container folder whose name
            # shares nothing with the game (Ren'Py save_directory set to
            # the internal title) is claimed when its write time matches an
            # already-associated path's write time — the simultaneous
            # double-write IS the association.
            if correlate_paths:
                try:
                    _own_id = game_entry.id if game_entry is not None else ""
                    for p in correlated_engine_paths(exe_path, correlate_paths,
                                                     since_ts,
                                                     own_game_id=_own_id):
                        _add(p, is_live_result=True)
                except Exception as e:
                    logger.debug(f"Live-only correlation check failed: {e}")

            # Saves INSIDE registry values (Unity PlayerPrefs): gate on the
            # key tree's real last-write timestamp — the registry
            # equivalent of the folder mtime check above.
            try:
                from core.registry_saves import (find_registry_value_keys,
                                                 registry_last_write)
                for rp in find_registry_value_keys(_reg_terms):
                    if registry_last_write(rp) >= since_ts - 2.0:
                        _add(rp, is_live_result=True)
                        logger.debug(f"Live-only: registry key passed last-write check → {rp}")
            except Exception as e:
                logger.debug(f"Live-only registry value-key check failed: {e}")

        # Normalize live results too: expand_selectable_paths only merges a
        # subtree into a folder that has DIRECT files (which live-tracking
        # parents always do — they're the parents of written files), so the
        # precision of Strategy 1 is preserved; what this adds is exact-dup
        # removal and never returning both "game" and "game/save".
        results = expand_selectable_paths(results)
        logger.info(
            f"Live-only tracking found {len(results)} save paths for '{game_name}'"
        )
        return results

    # Strategy 2: Engine-specific paths — ALL scores, deduplicated
    # Cache engine_hits to avoid calling _engine_paths twice
    engine_hits = []
    if exe_path:
        extra = [t for t in search_terms if t != game_name]
        engine_hits = sorted(_engine_paths(exe_path, game_name, appid, extra_terms=extra), key=lambda x: -x[0])
        logger.info(f"_engine_paths returned {len(engine_hits)} hits: {[(s, p) for s, p in engine_hits]}")
        for score, p in engine_hits:
            if score >= 50:
                _add(p, engine_detected=True)

    # Strategy 3: Registry (Windows). The game linkage was already
    # established by the STRICT key-name match inside _registry_save_paths
    # (exact slug equality), so the folder must be trusted like an engine
    # hit — name-similarity validation would reject perfectly good targets
    # ("...\CompanyName\SavedGames" rarely contains the game name) and
    # silently threw the whole strategy away.
    for p in _registry_save_paths(game_name):
        _add(p, engine_detected=True)

    # Strategy 3b: saves living INSIDE registry values (Unity PlayerPrefs:
    # HKCU\Software\<Vendor>\<Product> full of REG_BINARY prefs). Proposed
    # as virtual "registry:HKCU\..." entries that the backup engine
    # exports/restores as JSON inside the zip.
    try:
        from core.registry_saves import find_registry_value_keys
        for rp in find_registry_value_keys(search_terms):
            _add(rp, engine_detected=True)
    except Exception as e:
        logger.debug(f"Registry value-key detection failed: {e}")

    # Strategy 4: Filesystem scan
    search_roots: list[Path] = list(_resolve_watch_paths(config))
    # With the appid known, this game's own Proton prefix goes to the FRONT:
    # it is where a Windows game running under Proton actually saves, and
    # searching it first is both cheaper and more likely to be right than
    # walking every prefix on the machine.
    if appid:
        for _pfx in reversed(compat_prefix_roots(str(appid))):
            if _pfx not in search_roots:
                search_roots.insert(0, _pfx)
    if exe_path:
        exe_dir = Path(exe_path).parent
        for p in [exe_dir, exe_dir.parent]:
            if p.exists():
                search_roots.insert(0, p)
        # Walk up the parent chain for Ren'Py and other engines where the
        # exe is buried inside lib/windows-i686/ or similar platform dirs.
        # Stop when we either cross the filesystem root or land on a dir
        # whose name doesn't look like an engine/platform directory.
        _RENPY_PLATFORM_DIRS = frozenset({
            'windows-i686', 'windows-x86_64', 'win64', 'win32',
            'linux-i686', 'linux-x86_64', 'linux-arm64',
            'darwin', 'macos', 'mac', 'universal',
            'lib', 'bin', 'binaries', 'common', 'engine',
        })
        _cur = exe_dir.parent
        while _cur != _cur.parent:  # stop at filesystem root
            if _cur.name.lower() in _RENPY_PLATFORM_DIRS:
                _up = _cur.parent
                if _up.exists() and _up not in search_roots:
                    search_roots.insert(0, _up)
                _cur = _up
            else:
                break

    candidates: list[tuple[int, Path]] = []
    logger.info("Search roots (%d): %s", len(search_roots),
        [str(r)[-50:] for r in search_roots])
    # Scan with every search term so the detector finds folders named after
    # the exe stem or CamelCase variant when the display name doesn't match.
    for root in search_roots:
        for term in search_terms:
            if not term:
                continue
            logger.info("SCAN root=%s term=%s depth=0 start candidates=%d",
                str(root)[-50:], term[:30], len(candidates))
            try:
                before = len(candidates)
                _scan_dir(root, term, hints, candidates, depth=0, max_depth=4, exe_path=exe_path)
                added = len(candidates) - before
                if added:
                    logger.info("SCAN root=%s term=%s depth=0 done +%d total=%d",
                        str(root)[-50:], term[:30], added, len(candidates))
            except OSError as e:
                logger.debug(f"Scan I/O error in {root} ({term!r}): {e}")
            except Exception as e:
                logger.warning(f"Scan error in {root} ({term!r}): {e}")

    # Lower engine paths (score 40-49) — reuse cached engine_hits
    if exe_path:
        for score, p in engine_hits:
            if 40 <= score < 50:
                key = str(Path(p)).lower()
                if key not in seen:
                    candidates.append((score, Path(p)))

    # Merge filesystem results — raise minimum quality threshold significantly
    # Only accept high-confidence matches to avoid false positives
    MIN_CONFIDENCE_THRESHOLD = 75  # Increased from 60 - much stricter
    MAX_RESULTS_PER_GAME = 3       # Reduced from 5 - fewer results
    
    if candidates:
        logger.info("Top candidates: %s",
            [(s, str(p)[-60:]) for s, p in sorted(candidates, key=lambda x: -x[0])[:5]])
    
    for score, path in sorted(candidates, key=lambda x: -x[0]):
        if len(results) >= MAX_RESULTS_PER_GAME:
            break
        key = str(path).lower()
        if key not in seen and score >= MIN_CONFIDENCE_THRESHOLD:
            # Validate against every search term — a path found via a short
            # exe stem must pass context check with that term, not just the
            # display name (which may not match the folder at all).
            valid = any(
                _is_valid_save_context(path, term, exe_path)
                for term in search_terms if term
            )
            if valid:
                seen.add(key)
                results.append(str(path))
                logger.info("RESULT added: %s (score=%d)", path, score)
            else:
                logger.info("SKIPPED (validation failed): %s (score=%d)", path, score)

    # ── Normalization: independent selectable entries, no compaction ───────
    # Folders without direct files are expanded into per-subfolder entries;
    # a subtree is merged into one entry only when the main folder itself
    # holds selectable files; duplicates like "game" + "game/save" collapse
    # to just "game/save".
    results = expand_selectable_paths(results)

    logger.info(
        f"Detected {len(results)} save paths for '{game_name}' "
        f"(live:{bool(pid)}, engine:{bool(exe_path)}, fs:{len(candidates)})"
    )
    return results


def _selectable_skip_sets(include_detection_excluded: bool = True,
                          engine: str = "") -> tuple[set, set, set]:
    """(skip_extensions, skip_dirs, skip_filename_stems) shared by every
    "does this path contain anything worth showing/backing up?" check —
    single source of truth for auto-scan filtering, add-game general scan,
    and path expansion.

    *engine*, when known, narrows the detection-only exclusions: ".dat" is
    engine data in RPG Maker and a save in Unity, so the same blanket rule
    cannot be right for both. Unknown engine keeps the broad list, which is
    the safe direction — a save that is merely not auto-detected can still
    be added by hand, while noise proposed as a save cannot be un-seen.
    """
    from core.backup import _BACKUP_SKIP_EXTENSIONS, _BACKUP_SKIP_DIRS
    from core.game_engine import detection_skip_extensions
    exts = set(_BACKUP_SKIP_EXTENSIONS)
    if include_detection_excluded:
        exts |= detection_skip_extensions(engine)
    return exts, set(_BACKUP_SKIP_DIRS), set(SKIP_FILENAME_STEMS)


def _is_selectable_file(f: Path, skip_exts: set, skip_stems: set) -> bool:
    """True if *f* would actually be backed up / shown as selectable —
    neither a skip-extension nor a skip-filename-stem (e.g. "log"/"logs",
    any extension) match."""
    return f.suffix.lower() not in skip_exts and f.stem.lower() not in skip_stems


def path_has_backup_content(path_str, include_detection_excluded: bool = True,
                            engine: str = "") -> bool:
    """True if *path_str* (file or directory, recursive) contains at least
    one file that would actually be backed up / shown as selectable.

    Unreadable paths return True (conservative: never hide what we can't
    inspect). Shared helper — do not duplicate this walk elsewhere."""
    from core.registry_saves import is_registry_path, registry_has_values
    if is_registry_path(str(path_str)):
        return registry_has_values(str(path_str))
    skip_exts, skip_dirs, skip_stems = _selectable_skip_sets(
        include_detection_excluded, engine)
    p = Path(path_str)
    try:
        if p.is_file():
            return _is_selectable_file(p, skip_exts, skip_stems)
        if not p.is_dir():
            return False
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            if not _is_selectable_file(f, skip_exts, skip_stems):
                continue
            try:
                rel_parts = f.relative_to(p).parts
                if any(part.lower() in skip_dirs for part in rel_parts[:-1]):
                    continue
            except ValueError:
                pass
            return True
    except (PermissionError, OSError):
        return True   # can't read → conservative: treat as having content
    return False


def _dir_has_direct_selectable_file(p: Path) -> bool:
    """True if *p* directly (non-recursively) contains at least one file
    that would actually be shown/backed up (not a skip-extension or
    skip-filename-stem file)."""
    skip_exts, _, skip_stems = _selectable_skip_sets()
    try:
        for child in p.iterdir():
            if child.is_file() and _is_selectable_file(child, skip_exts, skip_stems):
                return True
    except (PermissionError, OSError):
        return True   # can't read → conservative: treat as having content
    return False


def expand_selectable_paths(paths: list[str], max_expand_depth: int = 3) -> list[str]:
    """Normalize detected save paths into independent, selectable entries.

    Rules (per explicit product requirement — no path "compaction"):
    - Each folder is its own entry; entries are never silently merged into
      a parent that happens to also be detected.
    - A folder is kept AS ONE entry (subtree merged) ONLY when it directly
      contains one or more selectable files — e.g. ``save/`` holding both
      files and a ``profile/`` subfolder stays a single ``save`` entry.
    - A folder with NO direct files but with subfolders is EXPANDED: each
      non-excluded subfolder becomes its own entry (recursively, up to
      *max_expand_depth*), so ``game/`` with only ``save/`` inside yields
      ``game/save`` — never both ``game`` and ``game/save``.
    - Exact duplicates (case/normalization differences) are removed, and a
      descendant is dropped when a kept ancestor entry already covers it.
    - Individual file entries pass through untouched.
    """
    from core.backup import _BACKUP_SKIP_DIRS
    from core.registry_saves import is_registry_path as _is_reg
    # Configured hints (not the hardcoded constant) drive the install-root
    # subfolder pick below, like the rest of detection.
    _hints = get_config().get("save_folder_hints", SAVE_FOLDER_HINTS)

    # Virtual registry entries pass through untouched (dedupe only): there
    # is no subtree to expand and no files to inspect.
    reg_entries: list[str] = []
    _reg_seen: set[str] = set()
    fs_paths: list[str] = []
    for p in paths:
        if _is_reg(p):
            if p.lower() not in _reg_seen:
                _reg_seen.add(p.lower())
                reg_entries.append(p)
        else:
            fs_paths.append(p)
    paths = fs_paths

    # Step 1: exact-duplicate removal (case-insensitive, resolved)
    resolved_map: dict[str, str] = {}
    for p in paths:
        try:
            key = str(Path(p).resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key not in resolved_map:
            resolved_map[key] = p
        else:
            # Case/normalization collision: keep the variant that actually
            # exists on disk. Path.resolve() does NOT case-correct a path
            # component that doesn't exist, so a wrong-case (non-existent)
            # string can otherwise win the slot and make the real folder
            # vanish in _expand (which drops paths that are neither a file
            # nor a dir). Prefer an existing path over a stored missing one.
            try:
                if not Path(resolved_map[key]).exists() and Path(p).exists():
                    resolved_map[key] = p
            except OSError:
                pass

    _excluded_dirs = set(_SKIP_DIRS) | set(_BACKUP_SKIP_DIRS)

    def _expand(p: Path, depth: int, out: list[Path]):
        if _is_cancelled():
            return
        if p.is_file():
            out.append(p)
            return
        if not p.is_dir():
            return
        # Install/root folders (the game program lives right here) must NEVER
        # become a whole-folder entry: e.g. RPG Maker XP writes Save1.rxdata
        # beside Game.exe — the backup must cover those save files only, not
        # the entire installation. Emit the save-like files individually and
        # only follow save-named subfolders. Program detection is
        # platform-aware (see is_program_binary): a Linux game binary carries
        # no extension, so an ".exe" test never fired there.
        from core.resolvers import is_program_binary
        try:
            direct = list(p.iterdir())
        except (PermissionError, OSError):
            out.append(p)
            return
        if any(c.is_file() and is_program_binary(c) for c in direct):
            for f in sorted((c for c in direct
                             if c.is_file() and c.suffix.lower() in _SAVE_EXTENSIONS),
                            key=lambda c: c.name.lower()):
                out.append(f)
            for sub in sorted((c for c in direct if c.is_dir()
                               and c.name.lower() not in _excluded_dirs
                               and any(h in c.name.lower() for h in _hints)),
                              key=lambda c: c.name.lower()):
                _expand(sub, depth + 1, out)
            return
        if _dir_has_direct_selectable_file(p):
            out.append(p)               # merged entry: files live right here
            return
        if depth >= max_expand_depth:
            # Depth cap reached with no direct files: keep as-is rather than
            # dropping a subtree that may still contain saves further down.
            try:
                if any(True for _ in p.iterdir()):
                    out.append(p)
            except (PermissionError, OSError):
                out.append(p)
            return
        subdirs = []
        try:
            subdirs = [c for c in p.iterdir()
                       if c.is_dir() and c.name.lower() not in _excluded_dirs]
        except (PermissionError, OSError):
            out.append(p)               # unreadable: keep original entry
            return
        if not subdirs:
            return                      # empty / excluded-only folder → drop
        for sub in sorted(subdirs, key=lambda c: c.name.lower()):
            _expand(sub, depth + 1, out)

    expanded: list[Path] = []
    for original in resolved_map.values():
        _expand(Path(original), 0, expanded)

    # Step 2: dedup expanded results and drop descendants already covered by
    # a kept ancestor (ancestors survive expansion only when merged — i.e.
    # they have direct files — so they genuinely cover their subtree).
    unique: list[Path] = []
    seen_keys: set[str] = set()
    for p in expanded:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(p)

    dir_entries = [p for p in unique if p.is_dir()]
    result: list[str] = []
    for p in unique:
        covered = False
        for anc in dir_entries:
            if anc == p:
                continue
            try:
                p.relative_to(anc)
            except ValueError:
                continue
            covered = True
            logger.debug(f"Dedup: dropping {p} (covered by merged entry {anc})")
            break
        if not covered:
            result.append(str(p))
    return result + reg_entries


def _scan_dir(
    root: Path,
    game_name: str,
    hints: list[str],
    results: list,
    depth: int,
    max_depth: int,
    max_results: int = 300,
    exe_path: Optional[str] = None,
):
    if depth > max_depth or len(results) >= max_results or _is_cancelled():
        if len(results) >= max_results and depth <= 1:
            logger.info("SCAN exit depth=%d buffer full (%d)", depth, len(results))
        return
    if depth <= 1:
        logger.info("SCAN enter root=%s depth=%d buf=%d", root.name, depth, len(results))
    try:
        for entry in root.iterdir():
            if _is_cancelled():
                return
            if not entry.is_dir():
                continue
            if entry.name.lower() in _SKIP_DIRS:
                continue
            # Skip Unity engine asset directories: "<GameName>_Data/" contains
            # engine resources (shaders, assets, managed dlls) — never saves.
            if entry.name.endswith("_Data") or entry.name.endswith("_data"):
                continue
            # Skip Ren'Py engine dir only when it's next to the exe (game
            # install directory), NOT when it's in AppData/Roaming/RenPy/
            # where roaming saves live.
            if entry.name.lower() == 'renpy':
                parent_str = str(entry.parent).lower()
                if 'appdata' not in parent_str and 'roaming' not in parent_str:
                    continue
            if len(results) >= max_results:
                logger.info("SCAN exit depth=%d at entry=%s buffer full (%d)", depth, entry.name, len(results))
                return
            score = _score_folder(entry, game_name, hints, exe_path=exe_path)
            # Add every game-relevant folder as a candidate; the final
            # validation (MIN_CONFIDENCE_THRESHOLD=75) will filter them.
            # We keep the candidate list broad because save folders can
            # have arbitrary names that no hint list can fully cover.
            if score > 0:
                results.append((score, entry))
                if entry.name.lower() == 'saves' or score >= 100:
                    logger.info("SCAN entry score=%d depth=%d name=%s parent=%s", score, depth, entry.name, entry.parent.name)
                if score >= 75:
                    logger.info("CANDIDATE (score=%d): %s", score, entry)
            # Recurse into game-relevant folders that might contain saves.
            # At depth >= 1 we only recurse if the folder name contains a
            # save hint — otherwise the parent-path context bonus (+60)
            # triggers wasteful deep recursion into every subdirectory of
            # the game folder (Characters/, gui/, images/, …), filling
            # the candidate buffer before we ever reach saves/ at depth 1.
            # We do NOT use score < 80 as a recursion gate: game root
            # folders score high (exact slug match) but must still be
            # recursed into to reach nested save dirs.
            hint_match = any(h in entry.name.lower() for h in hints)
            if depth < max_depth:
                if depth >= 1 and not hint_match:
                    pass  # non-save directory, skip recursion
                else:
                    _scan_dir(entry, game_name, hints, results, depth + 1, max_depth, max_results, exe_path=exe_path)
    except (PermissionError, OSError):
        pass

def general_scan_paths(game_name: str, exe_path: str, hints: list[str],
                       already: list[str],
                       extra_terms: "list[str] | None" = None,
                       should_stop=None,
                       timeout_s: float = 0,
                       require_backup_content: bool = False) -> list[str]:
    """The "general/extended scan" shared by the add-game dialog's
    DetectWorker and the at-exit auto-scan worker: walk the standard save
    roots (exe folder first) with _scan_dir for every search term, re-score
    each candidate against the real game name, and return the paths worth
    adding (score >= 60, not already in *already*, deduped).

    *extra_terms* adds search terms beyond the display name (exe stem,
    CamelCase split). *should_stop* is polled between units of work;
    *timeout_s* > 0 bounds the whole walk. *require_backup_content* also
    drops folders whose content is entirely excluded (dll/exe/engine data).
    The caller merges the result and runs expand_selectable_paths on the
    combined set.
    """
    import time as _time
    from core.game_engine import detect_engine
    start = _time.time()
    # Read once per scan: the engine decides whether ".dat" is a save here.
    _engine = detect_engine(exe_path=exe_path) if exe_path else ""

    def _stopped() -> bool:
        if should_stop is not None and should_stop():
            return True
        return timeout_s > 0 and (_time.time() - start) > timeout_s

    terms = [game_name] + [t for t in (extra_terms or [])
                           if t and t != game_name]
    search_roots = _resolve_watch_paths()
    if exe_path:
        exe_dir = Path(exe_path).parent
        if exe_dir.exists():
            search_roots.insert(0, exe_dir)   # prioritize the game directory

    candidates: list = []
    for root in search_roots:
        if _stopped():
            break
        for term in terms:
            if _stopped():
                break
            try:
                _scan_dir(root, term, hints, candidates,
                          depth=0, max_depth=4, exe_path=exe_path)
            except Exception as e:
                logger.debug(f"General scan error in {root} ({term!r}): {e}")

    already_set = {str(p) for p in already}
    finals: list = []
    for score, path in candidates:
        if str(path) in already_set:
            continue
        rescore = _score_folder(path, game_name, hints, exe_path=exe_path)
        combined = max(score, rescore) if rescore > 0 else score
        if combined > 0:
            finals.append((combined, path))

    MIN_GENERAL_THRESHOLD = 60   # slightly lower than live detection
    out: list[str] = []
    for score, path in sorted(finals, key=lambda x: -x[0]):
        p = str(path)
        if p in already_set or p in out or score < MIN_GENERAL_THRESHOLD:
            continue
        if require_backup_content and not path_has_backup_content(path, engine=_engine):
            logger.debug(f"General scan skip (no backup content): {path}")
            continue
        out.append(p)
    if candidates:
        logger.info(
            f"General scan for {game_name!r}: {len(candidates)} candidates "
            f"-> {len(out)} kept")
    return out
