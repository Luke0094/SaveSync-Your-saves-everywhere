"""
SaveSync - Game Resolvers
Handles resolution of launcher shortcuts (steam://, epic://, etc.) to executable paths.
"""
import logging
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def fuzzy_slug(s: str) -> str:
    """Normalize string for fuzzy matching."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


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
            for exe_path in search_base.rglob("*.exe"):
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

        # Also scan .url and .lnk files on Desktop paths (game launcher shortcuts)
        if "Desktop" in search_base.name:
            try:
                for link_path in list(search_base.glob("*.lnk")) + list(search_base.glob("*.url")):
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
