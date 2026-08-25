"""
SaveSync - Game Resolvers
Handles resolution of launcher shortcuts (steam://, epic://, etc.) to executable paths.
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == 'win32' or os.name == 'nt'
_IS_MACOS = sys.platform == 'darwin'
_IS_LINUX = not _IS_WINDOWS and not _IS_MACOS

# ── What counts as "an executable the user can add" ───────────────────────────
# THREE platforms, three tables — not "Windows and everything else". Windows
# says it with an extension; Linux mostly doesn't (the common case is a
# suffix-less file carrying the exec bit, so recognition there needs a stat,
# not a glob); macOS ships its programs as .app bundles, which are
# DIRECTORIES. Shortcuts are each platform's own indirection format and are
# deliberately kept apart from real executables.
#
# The tables do not bleed into each other. A .sh cannot be launched by
# Windows and a .exe cannot be launched by Linux, so offering one on the
# other's file dialog, drag-drop gate or install scan only ever produces a
# candidate that fails at launch. Foreign builds sitting in a folder (a
# Proton/WSL tree on an NTFS drive) are still recognised as evidence that the
# folder is a game install — that is what is_program_binary is for, and it is
# host-independent by design.
_EXEC_SUFFIXES_WINDOWS = ('.exe', '.bat', '.cmd')
# .exe is on the LINUX list, and that is not a leftover: launch_executable
# runs a Windows game through Proton or Wine here, so one is something this
# machine can genuinely start — and a picker that filtered it out hid every
# game in a Wine prefix from the one dialog meant for finding games.
_EXEC_SUFFIXES_LINUX = ('.sh', '.appimage', '.x86_64', '.x86', '.run', '.bin',
                        '.exe')
# macOS: .app is the bundle (a directory), .command is Finder's double-
# clickable shell script. AppImage/.x86_64/.run are Linux packaging formats
# and never appear here.
# Same reasoning as Linux: CrossOver and Wine run Windows games on macOS.
_EXEC_SUFFIXES_MACOS = ('.app', '.command', '.sh', '.exe')
_SHORTCUT_SUFFIXES_WINDOWS = ('.lnk', '.url')
_SHORTCUT_SUFFIXES_LINUX = ('.desktop',)
# macOS aliases are extension-less Finder metadata, not a file format this
# can resolve — there is no macOS counterpart to .lnk/.desktop to offer.
_SHORTCUT_SUFFIXES_MACOS: tuple[str, ...] = ()
_MACOS_BUNDLE_SUFFIX = '.app'


def executable_suffixes() -> tuple[str, ...]:
    """Executable file extensions for this platform (lowercase, with dot)."""
    if _IS_WINDOWS:
        return _EXEC_SUFFIXES_WINDOWS
    if _IS_MACOS:
        return _EXEC_SUFFIXES_MACOS
    return _EXEC_SUFFIXES_LINUX


def shortcut_suffixes() -> tuple[str, ...]:
    """Shortcut/launcher-file extensions for this platform."""
    if _IS_WINDOWS:
        return _SHORTCUT_SUFFIXES_WINDOWS
    if _IS_MACOS:
        return _SHORTCUT_SUFFIXES_MACOS
    return _SHORTCUT_SUFFIXES_LINUX


def is_shortcut_file(path) -> bool:
    """True for a shortcut that points at a game rather than being one."""
    return Path(path).suffix.lower() in shortcut_suffixes()


def is_executable_file(path) -> bool:
    """True when *path* is a program THIS machine can actually launch.

    Strictly platform-native — the question behind every add-a-game entry
    point (file dialog, drag & drop, folder scan) is "can the user run this
    here", and a foreign build answers no however plausible its name looks.

    On Windows: .exe/.bat/.cmd, and nothing else. A .sh, .AppImage or
    .x86_64 on an NTFS drive is a Linux build that Windows cannot start;
    .bin files are assets/data. Offering any of them produced a library
    entry whose ▶ Play could only ever fail.

    On Linux: extension-less files carrying the exec bit — the usual shape
    of a game binary — plus .sh/.AppImage/.x86_64/.x86/.run/.bin, and .exe.
    A .exe has no exec bit and never will: it is answered on its extension
    alone, because what starts it is Wine or Proton and not the kernel.

    On macOS: .app bundles (directories), .command/.sh scripts, and
    extension-less exec-bit binaries.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix and suffix not in executable_suffixes():
        return False
    if _IS_WINDOWS:
        # Windows needs the extension to run anything at all; an
        # extension-less file is data here whatever its first bytes say.
        return bool(suffix)
    if suffix == _MACOS_BUNDLE_SUFFIX:
        return p.is_dir()
    if suffix:
        return True
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


# Compiled-program extensions of ANY platform — see is_program_binary.
_PROGRAM_BINARY_SUFFIXES = ('.exe', '.appimage', '.x86_64', '.x86')
# The launcher script a game ships INSTEAD of exposing its binary — Ren'Py
# and Unity both do this, and the binary itself sits a few folders down. It
# counts as the same evidence the binary would: this folder is an install,
# not save data. (The extension-less form of the same wrapper is why
# _BINARY_MAGICS knows the shebang.)
_LAUNCHER_SCRIPT_SUFFIXES = ('.bat', '.cmd', '.sh', '.command')
# Suffixes that name a program on one platform and a data blob on another —
# Windows ships assets as .bin, Linux ships game binaries and self-extracting
# installers under both. Neither the name nor the host OS can settle it, so
# these are decided on the file's own first bytes instead of guessed.
_MAYBE_BINARY_SUFFIXES = ('.bin', '.run')


def is_program_binary(path) -> bool:
    """True only when *path* is a game program — a compiled binary or the
    launcher script standing in for one — and not an asset file or directory.

    Deliberately HOST-INDEPENDENT, unlike is_executable_file: this answers
    "is there a game program in this folder", which is how an install root is
    told apart from a save folder, and that is a fact about the folder rather
    than about the machine reading it. A Proton/WSL tree on a Windows drive
    holds ELF binaries and is just as much an install root there as on Linux,
    so the same file must get the same answer on every OS.

    Recognises compiled formats and launcher scripts by extension, and reads
    the ELF/Mach-O/PE magic of the rest — extension-less files (additionally
    requiring the exec bit on Unix, since FAT/NTFS mounts hand it out to
    every file) and the ambiguous .bin/.run.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix and suffix not in _MAYBE_BINARY_SUFFIXES:
        return (suffix in _PROGRAM_BINARY_SUFFIXES
                or suffix in _LAUNCHER_SCRIPT_SUFFIXES)
    try:
        if not suffix and not _IS_WINDOWS and not (p.is_file() and os.access(p, os.X_OK)):
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


def executable_name_filter(all_files_label: str = "") -> str:
    """QFileDialog name filter for picking a game executable.

    Both labels are translated, and neither used to be: a picker in Italian
    still offered "Executables" and "All Files". The default is filled from
    i18n rather than from an English literal, so a caller that passes
    nothing gets the user's language instead of the author's.

    The ORDER differs by platform on purpose. On Windows a game is a .exe
    and the extension filter is the useful default. Everywhere else a
    native binary usually has no extension at all — filtering by suffix
    would hide exactly what the user came to pick — so "all files" leads
    and the suffix list stays available for the Windows games run through
    Wine or Proton.
    """
    patterns = " ".join(f"*{s}" for s in executable_suffixes() + shortcut_suffixes())
    if not all_files_label:
        try:
            from i18n import t
            all_files_label = t("file_picker.filter_any_file")
        except Exception:
            all_files_label = "All files"
    try:
        from i18n import t
        exe_label = t("file_picker.filter_executables")
    except Exception:
        exe_label = "Executables"
    if _IS_WINDOWS:
        return f"{exe_label} ({patterns});;{all_files_label} (*)"
    return f"{all_files_label} (*);;{exe_label} ({patterns})"


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

    Windows globs its OWN extensions only — one cheap pass per suffix over a
    Program Files tree. It used to glob the Linux ones too, on the theory that
    a WSL/Proton build might sit there, and every hit was a candidate the user
    could not launch; the folder scan and the magic-byte check still find
    foreign installs where that actually matters. Unix cannot glob for its
    main case (a suffix-less binary), so it walks everything and rejects by
    suffix, which is a pure string test: the same tree yields ~80x more
    entries, so anything per-entry costlier than that (notably the exec-bit
    stat) is left to the caller, which only pays it for entries that scored.
    """
    suffixes = executable_suffixes()
    if _IS_WINDOWS:
        for _s in suffixes:
            yield from search_base.rglob(f"*{_s}")
        return
    for path in search_base.rglob("*"):
        suffix = path.suffix.lower()
        if not suffix or suffix in suffixes:
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

    Scans the home directory and this platform's own install/mount roots —
    drive letters on Windows, /Applications and /Volumes on macOS, the FHS
    mount points on Linux. macOS used to get the Linux list (plus a walk of
    "/" that SIP and the privacy prompts block anyway).
    """
    paths = [Path.home()]

    if _IS_WINDOWS:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:/")
            if p.exists():
                paths.append(p)
        return paths

    if _IS_MACOS:
        roots = ["/Applications", "/Volumes"]
    else:
        roots = ["/media", "/mnt", "/opt"]
    for mp in roots:
        p = Path(mp)
        if p.exists():
            paths.append(p)
    if not _IS_MACOS:
        # Linux fallback: the whole tree. Not on macOS, where "/" is mostly
        # system-owned and the interesting parts are already listed above.
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
    """Get default installation paths for different game launchers.

    One branch per platform: every launcher puts its library somewhere
    different on each. macOS used to be handed the Linux branch, so none of
    its three paths existed and a suggested scan there searched nothing —
    Steam lives under ~/Library/Application Support, not ~/.local/share.
    """
    paths = {}

    if _IS_WINDOWS:
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
    elif _IS_MACOS:
        # Every launcher installs under ~/Library/Application Support, and
        # the games themselves are .app bundles in /Applications.
        home = Path.home()
        appsup = home / "Library" / "Application Support"
        for key, dirs in (
            ("steam", [appsup / "Steam"]),
            ("epic", [appsup / "Epic", Path("/Users/Shared/Epic Games")]),
            ("gog", [appsup / "GOG.com" / "Galaxy", home / "GOG Games"]),
            ("user", [Path("/Applications"), home / "Applications"]),
        ):
            for d in dirs:
                if d.exists() and d not in paths.get(key, []):
                    paths.setdefault(key, []).append(d)
    else:
        # Linux
        home = Path.home()

        # Steam
        steam_dirs = [
            home / ".local" / "share" / "Steam",
            home / ".steam" / "steam",
            Path("/opt/steam"),
            Path("/usr/share/steam"),
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

    # Desktop folders — where the platform HAS a shortcut format the fuzzy
    # search can read there (.lnk/.url on Windows, .desktop on Linux). macOS
    # aliases are not a readable format, so its Desktop is not searched.
    if _IS_WINDOWS:
        for desktop in (Path.home() / "Desktop", Path("C:/Users/Public/Desktop")):
            if desktop.exists():
                paths.setdefault("desktop", []).append(desktop)
    elif not _IS_MACOS:
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            paths.setdefault("desktop", []).append(desktop)

    # Always add user Games folder
    user_games = Path.home() / "Games"
    if user_games.exists() and user_games not in paths.get("user", []):
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


def launch_executable(exe_path: str) -> None:
    """Start the game at *exe_path*, the way THIS platform starts things.

    The counterpart of launch_with_url, and platform-split for the same
    reason: the three systems do not agree on what "run this file" means.
    Windows goes through the shell (``os.startfile``) so a .lnk/.url or an
    associated file works. macOS cannot exec a .app — it is a directory —
    so bundles go through ``open``, while a plain Unix binary or .command
    is exec'd directly. Linux exec's directly.

    Raises whatever the underlying call raises; the caller reports it.
    """
    if _IS_WINDOWS:
        os.startfile(exe_path)          # type: ignore[attr-defined]
        return
    if _IS_MACOS and Path(exe_path).suffix.lower() == _MACOS_BUNDLE_SUFFIX:
        subprocess.Popen(["open", exe_path])
        return
    # A .exe is not something this kernel can run. Off Windows it goes
    # through Wine or Proton, in the prefix it belongs to.
    if Path(exe_path).suffix.lower() == ".exe":
        command, env = windows_runner_command(exe_path)
        if command:
            logger.info("Launching %s through %s", Path(exe_path).name,
                        Path(command[0]).name)
            subprocess.Popen(command, env=env,
                             cwd=str(Path(exe_path).parent))
            return
        raise RuntimeError(
            "This is a Windows executable and there is no Wine or Proton "
            "on this system to run it with. Install wine, or launch the "
            "game through Steam so its own Proton is used."
        )
    subprocess.Popen([exe_path])


def wine_prefix_for(exe_path) -> str:
    """The Wine/Proton prefix *exe_path* lives inside, or "".

    A prefix is recognised by its shape — a ``drive_c`` directory with the
    executable somewhere beneath it — rather than by a list of known
    layouts, because Steam, Lutris, Bottles and Heroic all arrange theirs
    differently and a game moved by hand belongs to none of them.

    It matters far more than it looks: run with the wrong prefix, a game
    finds none of its registry, none of its runtime, and — the part that
    concerns SaveSync — writes its saves somewhere new.
    """
    try:
        current = Path(exe_path).resolve()
    except (OSError, ValueError):
        return ""
    for parent in current.parents:
        if parent.name == "drive_c":
            return str(parent.parent)
    return ""


def _proton_for_prefix(prefix: str) -> str:
    """The Proton build serving *prefix*, or "".

    A Proton prefix sits at ``<library>/steamapps/compatdata/<appid>/pfx``,
    and the runtime that made it is a normal Steam app in the same tree.
    Newest wins: Proton is backwards compatible and a game that ran under
    an older build runs under a newer one.
    """
    p = Path(prefix)
    if p.name != "pfx" or "compatdata" not in p.parts:
        return ""
    try:
        steamapps = p.parents[list(p.parts).index("compatdata") and 2]
    except (IndexError, ValueError):
        return ""
    common = steamapps / "common"
    if not common.is_dir():
        return ""
    builds = []
    try:
        for entry in common.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("proton"):
                runner = entry / "proton"
                if runner.is_file():
                    builds.append(runner)
    except OSError:
        return ""
    if not builds:
        return ""
    builds.sort(key=lambda b: b.parent.name)
    return str(builds[-1])


def windows_runner_command(exe_path) -> tuple:
    """``(argv, env)`` that runs *exe_path*, or ``(None, None)``.

    Proton first when the executable is inside a Proton prefix — that is
    the runtime the game was installed with, and the one whose prefix
    already holds its registry. Wine otherwise, with WINEPREFIX pointed at
    whatever prefix the executable sits in, so a game keeps writing its
    saves where it has always written them.
    """
    if _IS_WINDOWS:
        return None, None
    env = dict(os.environ)
    prefix = wine_prefix_for(exe_path)

    proton = _proton_for_prefix(prefix) if prefix else ""
    if proton:
        compat = str(Path(prefix).parent)          # …/compatdata/<appid>
        steam_root = ""
        for candidate in (Path.home() / ".steam" / "steam",
                          Path.home() / ".local" / "share" / "Steam"):
            if candidate.is_dir():
                steam_root = str(candidate)
                break
        env.setdefault("STEAM_COMPAT_DATA_PATH", compat)
        if steam_root:
            env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", steam_root)
        return [proton, "run", str(exe_path)], env

    runner = env.get("WINE") or shutil.which("wine") or shutil.which("wine64")
    if not runner:
        return None, None
    if prefix:
        env["WINEPREFIX"] = prefix
    return [runner, str(exe_path)], env


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
