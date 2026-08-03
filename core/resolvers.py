"""
SaveSync - Game Resolvers
Handles resolution of launcher shortcuts (steam://, epic://, etc.) to executable paths.
"""
import logging
import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == 'nt'

# ── What counts as "an executable the user can add" ───────────────────────────
# Windows says it with an extension; Unix mostly doesn't — the common case is a
# suffix-less file carrying the exec bit, so recognition there needs a stat, not
# a glob. Shortcuts are the platform's own indirection format (.lnk/.url vs
# .desktop) and are deliberately kept apart from real executables: the add flow
# resolves them to a target and derives the display name from the shortcut's
# filename rather than the binary's.
_EXEC_SUFFIXES_WINDOWS = ('.exe', '.bat', '.cmd')
_SHORTCUT_SUFFIXES_WINDOWS = ('.lnk', '.url')
# .x86_64/.x86 are Unity's Linux builds, .AppImage a self-contained bundle,
# .command a double-clickable macOS script. .app is a macOS bundle DIRECTORY,
# handled separately since every suffix/is_file() test would reject it.
_EXEC_SUFFIXES_POSIX = ('.sh', '.appimage', '.x86_64', '.x86', '.run', '.bin', '.command')
_SHORTCUT_SUFFIXES_POSIX = ('.desktop',)
_MACOS_BUNDLE_SUFFIX = '.app'


def executable_suffixes() -> tuple[str, ...]:
    """Executable file extensions for this platform (lowercase, with dot)."""
    return _EXEC_SUFFIXES_WINDOWS if _IS_WINDOWS else _EXEC_SUFFIXES_POSIX


def shortcut_suffixes() -> tuple[str, ...]:
    """Shortcut/launcher-file extensions for this platform."""
    return _SHORTCUT_SUFFIXES_WINDOWS if _IS_WINDOWS else _SHORTCUT_SUFFIXES_POSIX


def is_shortcut_file(path) -> bool:
    """True for a shortcut that points at a game rather than being one."""
    return Path(path).suffix.lower() in shortcut_suffixes()


def is_executable_file(path) -> bool:
    """True when *path* is a runnable program on this platform.

    On Unix a game binary usually has NO extension at all, so the exec bit is
    the real signal; the suffix list only covers the conventions that do exist
    (.sh, .AppImage, Unity's .x86_64…). A macOS .app is a directory and is
    accepted as itself.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if _IS_WINDOWS:
        return suffix in _EXEC_SUFFIXES_WINDOWS
    if suffix in _EXEC_SUFFIXES_POSIX:
        return True
    if suffix == _MACOS_BUNDLE_SUFFIX:
        return p.is_dir()
    if suffix:
        return False
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


# Leading bytes of a real program. Used ONLY by is_program_binary — the exec
# bit alone is not trustworthy evidence on Unix: FAT/NTFS mounts are commonly
# mounted with it set on every single file, which would make any save folder
# on such a mount look like a game install.
_BINARY_MAGICS = (
    b'\x7fELF',            # Linux/BSD executables and shared objects
    b'\xcf\xfa\xed\xfe',   # Mach-O 64-bit
    b'\xce\xfa\xed\xfe',   # Mach-O 32-bit
    b'\xca\xfe\xba\xbe',   # Mach-O universal binary
    b'#!',                 # shebang — how a launcher wrapper script starts
)


def is_program_binary(path) -> bool:
    """Stricter than is_executable_file: is this the program a game is
    *installed as*?

    is_executable_file answers "could the user launch this" and is
    deliberately permissive, because there the user picked the file. This one
    backs an automatic decision — "this folder is an install root, so back up
    its save files individually instead of the whole folder" — where a false
    positive silently narrows what gets backed up. So an extension-less file
    must both carry the exec bit AND actually start like a binary.

    Windows keeps the exact ``.exe`` test the heuristic has always used.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if _IS_WINDOWS:
        return suffix == '.exe'
    if suffix in _EXEC_SUFFIXES_POSIX:
        return True
    if suffix:
        return False
    try:
        if not (p.is_file() and os.access(p, os.X_OK)):
            return False
        with open(p, 'rb') as fh:
            head = fh.read(4)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in _BINARY_MAGICS)


def is_different_program(stored_exe: str, running_exe: str) -> bool:
    """True only with POSITIVE evidence that two exe paths are different
    programs — the rule that keeps same-named executables apart.

    An exe stem is not an identity: plenty of games ship ``game.exe``,
    ``launcher.exe`` or a short shared name, so a name-based match must be able to
    reject a candidate the path contradicts.

    Returns False whenever the evidence is missing or ambiguous:
    - either path unknown (an unreadable process exe proves nothing), or
    - the stored path no longer EXISTS. That's a moved or reinstalled game,
      not a different one — rejecting the name match there would lose the
      entry (and its backups) instead of disambiguating it, which is the
      opposite of what this guard is for.
    """
    if not stored_exe or not running_exe:
        return False
    try:
        stored_resolved = str(Path(stored_exe).resolve()).casefold()
        running_resolved = str(Path(running_exe).resolve()).casefold()
    except OSError:
        return False
    if stored_resolved == running_resolved:
        return False
    try:
        return Path(stored_exe).exists()
    except OSError:
        return False


def is_addable_file(path) -> bool:
    """True for anything the add-game entry points accept: a real executable
    or a shortcut to one. Single source of truth for drag & drop and Browse."""
    return is_executable_file(path) or is_shortcut_file(path)


def executable_name_filter(all_files_label: str = "All Files") -> str:
    """QFileDialog name filter for picking a game executable.

    On Unix "All Files" comes FIRST on purpose: the typical game binary has no
    extension, so no pattern can match it and a restrictive default filter
    would hide exactly what the user came to select.
    """
    patterns = " ".join(f"*{s}" for s in executable_suffixes() + shortcut_suffixes())
    if _IS_WINDOWS:
        return f"Executables ({patterns});;{all_files_label} (*)"
    return f"{all_files_label} (*);;Executables ({patterns})"


def fuzzy_slug(s: str) -> str:
    """Normalize string for fuzzy matching.

    Folds accents as it always did, but no longer discards everything that
    is not Latin — see core.constants.match_slug.
    """
    from core.constants import match_slug
    return match_slug(s)


def _has_latin(slug: str) -> bool:
    """Whether a slug holds anything the substring scores can read."""
    return any("a" <= ch <= "z" or "0" <= ch <= "9" for ch in slug)


def fuzzy_score(query: str, target: str) -> float:
    """Calculate fuzzy match score between query and target.
    
    Returns a score from 0-100 based on similarity.
    Uses a combination of substring matching and character overlap.
    """
    query_slug = fuzzy_slug(query)
    target_slug = fuzzy_slug(target)
    
    if not query_slug or not target_slug:
        return 0.0
    
    if query_slug == target_slug:
        return 100.0

    # Everything below scores by substring and by shared characters, which is
    # reasoning about an alphabet of twenty-six letters written in separate
    # words. Japanese is neither, and scoring it this way was measured
    # against a live title database and made the answers worse rather than
    # better. An exact match above still counts in any script; a partial one
    # is only judged where the judging means something.
    if not (_has_latin(query_slug) and _has_latin(target_slug)):
        return 0.0

    # Exact substring match (query inside target or vice versa)
    if query_slug in target_slug:
        return 85.0 * len(query_slug) / len(target_slug)
    
    if target_slug in query_slug:
        return 75.0
    
    # Check for word-level matches (important for "My Great Game" -> "game")
    query_words = query_slug.split()
    target_words = target_slug.split()
    
    matched_words = 0
    for qw in query_words:
        for tw in target_words:
            if qw in tw or tw in qw:
                matched_words += 1
                break
    
    if matched_words > 0:
        word_score = 60.0 * matched_words / max(len(query_words), len(target_words))
        if word_score > 0:
            return word_score
    
    # Character overlap
    common_chars = set(query_slug) & set(target_slug)
    if common_chars:
        overlap_ratio = len(common_chars) / max(len(query_slug), len(target_slug))
        return 45.0 * overlap_ratio
    
    return 0.0


def find_executable_by_fuzzy_name(name: str,
                                  search_paths: Optional[list[Path]] = None,
                                  deadline: Optional[float] = None,
                                  cancel_event=None) -> Optional[Path]:
    """Find executable by fuzzy name search.

    ONE disk pass scoring ALL name variants at once: the old version
    re-walked every search path once per word of the name ("My Great
    Game" = three full-disk scans) — on a cold filesystem cache that
    alone blew past any caller-side timeout with NOTHING to show.

    Args:
        name: The name to search for (can be game name or appid)
        search_paths: Optional list of paths to search in
        deadline: Optional time.monotonic() timestamp — when reached, the
            scan STOPS and returns the best candidate seen SO FAR (a
            partial answer beats a timeout with nothing)
        cancel_event: Optional threading.Event checked alongside deadline

    Returns:
        Path to the best-matching executable, None if none scored.
    """
    if search_paths is None:
        search_paths = _get_default_exe_search_paths()

    # Full name first, then individual words — as score WEIGHTS in a
    # single pass (full-name matches keep their old priority without
    # re-scanning the disk once per word).
    weighted: list[tuple[str, str, float]] = [(name, fuzzy_slug(name), 1.0)]
    if " " in name:
        for word in name.split():
            weighted.append((word, fuzzy_slug(word), 0.85))

    return _search_executables_multi(weighted, search_paths,
                                     deadline=deadline,
                                     cancel_event=cancel_event)


class _DeadlineHit(Exception):
    pass


def _iter_executable_candidates(search_base: Path):
    """Files worth scoring as a game executable under *search_base*.

    Windows keeps the exact ``rglob("*.exe")`` pass it always had — one glob,
    ~1k hits in a Program Files tree. Unix cannot glob for its main case (a
    suffix-less binary), so it walks everything and rejects by suffix, which
    is a pure string test: the same tree yields ~80x more entries, so anything
    per-entry costlier than that (notably the exec-bit stat) is left to the
    caller, which only pays it for entries that actually scored.
    """
    if _IS_WINDOWS:
        yield from search_base.rglob("*.exe")
        return
    for path in search_base.rglob("*"):
        suffix = path.suffix.lower()
        if not suffix or suffix in _EXEC_SUFFIXES_POSIX or suffix == _MACOS_BUNDLE_SUFFIX:
            yield path


def _search_executables_multi(weighted_names: list[tuple[str, str, float]],
                              search_paths: list[Path],
                              deadline: Optional[float] = None,
                              cancel_event=None) -> Optional[Path]:
    """Single-pass search helper. Also scans ``.lnk``/``.url`` on Desktop
    paths. *weighted_names* is [(name, slug, weight), ...]."""
    import time as _time

    candidates: list[tuple[float, Path]] = []
    checked = 0

    def _expired() -> bool:
        if deadline is not None and _time.monotonic() >= deadline:
            return True
        return cancel_event is not None and cancel_event.is_set()

    for search_base in search_paths:
        if _expired():
            break
        if not search_base.exists():
            continue

        try:
            for exe_path in _iter_executable_candidates(search_base):
                checked += 1
                # rglob can grind through hundreds of thousands of
                # entries on a cold cache — honour the deadline INSIDE
                # the walk, not just between phases.
                if checked % 128 == 0 and _expired():
                    raise _DeadlineHit
                try:
                    folder_slug = fuzzy_slug(exe_path.parent.name)
                    stem = exe_path.stem
                    score = 0.0
                    for _name, _slug, _w in weighted_names:
                        folder_score = fuzzy_score(_slug, folder_slug)
                        stem_score = fuzzy_score(_name, stem)
                        # Folder match weighted higher, like before
                        score = max(score,
                                    max(folder_score * 1.2, stem_score) * _w)
                    if score >= 30.0:
                        # The exec-bit stat is deliberately deferred to here:
                        # on Unix the candidate stream includes every
                        # suffix-less file, and stat-ing all of them (vs only
                        # the few that scored) is what would blow the deadline.
                        if _IS_WINDOWS or is_executable_file(exe_path):
                            candidates.append((score, exe_path))
                except Exception:
                    pass
        except _DeadlineHit:
            logger.info(
                f"Fuzzy search deadline reached after {checked} files — "
                f"returning best of {len(candidates)} candidate(s)")
            break
        except Exception as e:
            logger.debug(f"Error scanning {search_base}: {e}")

        # Also scan shortcut files on Desktop paths (game launcher shortcuts):
        # .lnk/.url on Windows, .desktop entries on Linux.
        if "Desktop" in search_base.name:
            try:
                link_paths = []
                for _suffix in shortcut_suffixes():
                    link_paths.extend(search_base.glob(f"*{_suffix}"))
                for link_path in link_paths:
                    for _name, _slug, _w in weighted_names:
                        stem_score = fuzzy_score(_name, link_path.stem) * _w
                        if stem_score >= 30.0:
                            candidates.append((stem_score, link_path))
            except Exception as e:
                logger.debug(f"Error scanning Desktop in {search_base}: {e}")

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        logger.info(f"Fuzzy search found: {candidates[0][1]} "
                    f"(score {candidates[0][0]:.0f}, {checked} files scanned)")
        return candidates[0][1]

    logger.info(f"Fuzzy search found no matches ({checked} files scanned)")
    return None


def _get_default_exe_search_paths() -> list[Path]:
    """Get default paths to search for executables (wide scan).
    
    Scans home directory and all mount points/disks.
    """
    import os
    
    paths = []
    
    # User home
    paths.append(Path.home())
    
    # Scan all drives/mounts
    if os.name == 'nt':
        # Windows: scan all drive letters
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:/"
            p = Path(drive)
            if p.exists():
                paths.append(p)
    else:
        # Unix: scan common mount points
        for mp in ["/media", "/mnt", "/opt", "/Applications"]:
            p = Path(mp)
            if p.exists():
                paths.append(p)
        # Also try root as fallback
        paths.append(Path("/"))
    
    return paths


def _get_suggested_exe_search_paths() -> list[Path]:
    """Get suggested paths to search for executables (fast, targeted scan).
    
    Uses extra_watch_paths and launcher install paths.
    """
    from core.config_manager import get_config
    
    paths = []
    config = get_config()
    
    # Extra watch paths from config
    extra_paths = config.get("extra_watch_paths", [])
    for p in extra_paths:
        pp = Path(p)
        if pp.exists():
            paths.append(pp)
    
    # Launcher installation paths
    launcher_paths = _get_launcher_install_paths()
    for launcher, dirs in launcher_paths.items():
        paths.extend(dirs)
    
    return paths


def _get_launcher_install_paths() -> dict[str, list[Path]]:
    """Get default installation paths for different game launchers."""
    import os
    
    paths = {}
    
    if os.name == 'nt':
        # Try registry first for Steam (most common)
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
            steam_path = winreg.QueryValueEx(key, "InstallPath")[0]
            winreg.CloseKey(key)
            if steam_path:
                paths["steam"] = [Path(steam_path)]
        except Exception:
            pass
        
        # Check standard Program Files locations
        for pf in [Path("C:/Program Files"), Path("C:/Program Files (x86)")]:
            if not pf.exists():
                continue
            for launcher in ["Steam", "Epic Games", "Epic Games Launcher", "GOG Galaxy", "Ubisoft Game Launcher"]:
                p = pf / launcher
                if p.exists():
                    paths.setdefault(launcher.lower().replace(" ", ""), []).append(p)
        
        # Check other drives
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            for pf in [f"{letter}:/Program Files", f"{letter}:/Program Files (x86)"]:
                p = Path(pf)
                if not p.exists():
                    continue
                for launcher in ["Steam", "Epic Games", "Epic Games Launcher", "GOG Galaxy", "Ubisoft Game Launcher"]:
                    lp = p / launcher
                    if lp.exists():
                        launcher_key = launcher.lower().replace(" ", "")
                        if lp not in paths.get(launcher_key, []):
                            paths.setdefault(launcher_key, []).append(lp)
    else:
        # Unix-like
        home = Path.home()
        
        # Steam
        steam_dirs = [
            home / ".local" / "share" / "Steam",
            home / ".steam" / "steam",
            "/opt/steam",
            "/usr/share/steam",
        ]
        for d in steam_dirs:
            if d.exists():
                paths.setdefault("steam", []).append(d)
        
        # Epic Games
        for d in [home / ".local" / "share" / "EpicGamesLauncher", home / "Epic Games"]:
            if d.exists():
                paths.setdefault("epic", []).append(d)
        
        # GOG
        for d in [home / "GOG Games", home / ".local" / "share" / "gog"]:
            if d.exists():
                paths.setdefault("gog", []).append(d)
        
        # Games folder
        if (home / "Games").exists():
            paths.setdefault("user", []).append(home / "Games")
    
    # Desktop folders — user may have game launcher .url files there
    import os as _os
    if _os.name == 'nt':
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            paths.setdefault("desktop", []).append(desktop)
        public_desktop = Path("C:/Users/Public/Desktop")
        if public_desktop.exists():
            paths.setdefault("desktop", []).append(public_desktop)

    # Always add user Games folder
    user_games = Path.home() / "Games"
    if user_games.exists():
        paths.setdefault("user", []).append(user_games)
    
    return paths


def _parse_steam_url(url: str) -> Optional[dict]:
    """Parse steam:// URL.
    
    Formats:
    - steam://run/<appid> - Launch game
    - steam://rungameid/<appid> - Launch game
    - steam://nav/games - Navigate to games
    """
    if not url.startswith("steam://"):
        return None
    
    path = url.replace("steam://", "").strip("/")
    parts = path.split("/")
    
    action = parts[0] if parts else ""
    appid = parts[1] if len(parts) > 1 else None
    
    if action in ("run", "rungameid") and appid:
        return {
            "launcher": "steam",
            "appid": appid,
            "action": action,
        }
    
    return None


def _parse_epic_url(url: str) -> Optional[dict]:
    """Parse Epic Games launcher URL.
    
    Formats:
    - epic://launch/<namespace>/<appid>          - Launch game (old format)
    - com.epicgames.launcher://apps/<ids>?action=launch  - Launch game (real .url format)
    """
    import urllib.parse
    
    if url.startswith("epic://"):
        path = url.replace("epic://", "").strip("/")
        parts = path.split("/")
        action = parts[0] if parts else ""
        if action == "launch" and len(parts) >= 3:
            return {
                "launcher": "epic",
                "namespace": parts[1] if len(parts) > 1 else None,
                "appid": parts[2] if len(parts) > 2 else None,
                "action": action,
            }
        return None
    
    # Real format from Epic Games Store .url files:
    # com.epicgames.launcher://apps/NAMESPACE%3AAPPID%3ALOCAL_ID?action=launch&silent=true
    if url.startswith("com.epicgames.launcher://"):
        parsed = urllib.parse.urlparse(url)
        # Structure: scheme=com.epicgames.launcher, netloc=apps, path=/<ids>
        if parsed.netloc != "apps":
            return None
        decoded_path = urllib.parse.unquote(parsed.path.strip("/"))
        ids = decoded_path.split(":")
        return {
            "launcher": "epic",
            "namespace": ids[0] if len(ids) > 0 else None,
            "appid": ids[1] if len(ids) > 1 else None,
            "action": "launch",
        }
    
    return None


def _parse_gog_url(url: str) -> Optional[dict]:
    """Parse gog:// URL."""
    if not url.startswith("gog://"):
        return None
    
    path = url.replace("gog://", "").strip("/")
    parts = path.split("/")
    
    action = parts[0] if parts else ""
    
    if action == "game" and len(parts) >= 2:
        return {
            "launcher": "gog",
            "appid": parts[1],
            "action": action,
        }
    
    return None


def _parse_ubisoft_url(url: str) -> Optional[dict]:
    """Parse ubisoft:// URL."""
    if not url.startswith("ubisoft://"):
        return None
    
    path = url.replace("ubisoft://", "").strip("/")
    parts = path.split("/")
    
    action = parts[0] if parts else ""
    
    if action == "play" and len(parts) >= 2:
        return {
            "launcher": "ubisoft",
            "appid": parts[1],
            "action": action,
        }
    
    return None


# All recognised launcher URL schemes.
LAUNCHER_URL_PREFIXES = (
    "steam://", "epic://", "gog://", "ubisoft://",
    "com.epicgames.launcher://",
)


def is_launcher_url(url: str) -> bool:
    """Return True if *url* is a launcher URL (any custom protocol).

    Recognises:
    - Known schemes (steam://, epic://, etc.)
    - Any ``scheme://`` that is *not* http/https — catches Rockstar,
      Bethesda, Battle.net, and any other game launcher protocol.
    """
    if not url or "://" not in url:
        return False
    if url.startswith(("http://", "https://")):
        return False
    return True


def parse_launcher_url(url: str) -> Optional[dict]:
    """Parse a launcher URL and extract launcher type and appid.
    
    Known launchers (fully parsed):
    - steam://run/<appid>
    - epic://launch/<namespace>/<appid>
    - com.epicgames.launcher://apps/<ids>
    - gog://game/<appid>
    - ubisoft://play/<appid>
    
    Any other ``scheme://`` (rockstar://, battle.net://, etc.) is returned
    as a generic result so the URL can be saved and used for launching.
    
    Args:
        url: The launcher URL
        
    Returns:
        Dict with 'launcher', 'appid', etc. or None if not a launcher URL
    """
    url = url.strip()
    
    if not is_launcher_url(url):
        return None
    
    parsers = [
        _parse_steam_url,
        _parse_epic_url,
        _parse_gog_url,
        _parse_ubisoft_url,
    ]
    
    for parser in parsers:
        result = parser(url)
        if result:
            logger.info(f"Parsed launcher URL: {result}")
            return result
    
    # Unknown launcher — return generic entry so the URL is preserved
    scheme = url.split("://")[0] if "://" in url else "unknown"
    logger.info(f"Unknown launcher '{scheme}', using URL directly: {url}")
    return {
        "launcher": scheme,
        "appid": url,
        "action": "launch",
    }


def launch_with_url(url: str) -> bool:
    """Launch a game using its launcher URL.
    
    Args:
        url: The launcher URL (steam://, epic://, etc.)
        
    Returns:
        True if launch was successful
    """
    import os
    import sys

    try:
        if os.name == 'nt':
            os.startfile(url)
        elif sys.platform == 'darwin':
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
        logger.info(f"Launched via URL: {url}")
        return True
    except Exception as e:
        logger.error(f"Failed to launch {url}: {e}")
        return False


def get_appid_from_url(url: str) -> Optional[str]:
    """Extract appid from a launcher URL."""
    parsed = parse_launcher_url(url)
    if parsed:
        return parsed.get("appid")
    return None


def resolve_desktop_entry(path: str) -> str:
    """Resolve a Linux ``.desktop`` launcher to the program it starts.

    The Unix counterpart of resolve_lnk_target: reads the ``Exec=`` key from
    the ``[Desktop Entry]`` group and strips the field codes the spec allows
    there (``%u``, ``%F``, ``%i``…) — they are placeholders the desktop
    environment substitutes at launch, not part of the path. Arguments after
    the program are kept, mirroring what resolve_lnk_target does with a
    shortcut's Arguments. Returns the original *path* when there is nothing
    usable to resolve.
    """
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return path
    in_entry = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('['):
            # Only the main group counts — later groups are extra "actions"
            # (Desktop Action New, …) with their own Exec= lines.
            in_entry = line.lower() == '[desktop entry]'
            continue
        if not in_entry or not line.lower().startswith('exec='):
            continue
        command = line[5:].strip()
        # Drop field codes; %% is a literal percent and must survive.
        command = re.sub(r'(?<!%)%[fFuUdDnNickvm]', '', command).strip()
        if command.startswith('"'):
            end = command.find('"', 1)
            if end > 0:
                target = command[1:end]
                rest = command[end + 1:].strip()
                return f"{target} {rest}".strip() if rest else target
        return command or path
    return path


def resolve_lnk_target(path: str) -> str:
    """Resolve a .lnk shortcut to its target path (Windows only).

    Uses ``win32com.client.Dispatch("WScript.Shell")`` under the hood.
    Returns the original *path* if resolution fails (e.g. pywin32 not
    installed, or the file is not a valid .lnk).
    """
    import os
    if os.name != 'nt' or not path.lower().endswith('.lnk'):
        return path
    try:
        from win32com.client import Dispatch
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(path)
        target = shortcut.TargetPath
        if target:
            args = shortcut.Arguments
            return (target + " " + args).strip() if args else target
        return path
    except Exception:
        return path
