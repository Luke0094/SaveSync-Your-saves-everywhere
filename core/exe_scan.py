"""
SaveSync - Find installed games under a folder.

Backs the "scan a folder for games" flow: point at a games directory and get
one candidate executable per game, ready to be confirmed and added.

The walk follows the shape of a games folder rather than the filesystem:

    Games/
      Game One/game.exe            → found at level 0 of "Game One"
      Game Two/x86/game.exe        → nothing at level 0, so descend
      Game Three/bin/win64/g.exe   → …and keep descending, up to max_depth

Each top-level folder yields at most ONE candidate — the executable that most
plausibly IS the game — because that is what the user confirms: a row per
title, not a row per file. Installers, uninstallers, crash handlers and
redistributables never win that slot; the path stays editable, so a wrong pick
is a correction, not a dead end.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Stems that are never the game, however they rank otherwise.
_NOISE_STEMS = {
    "unins000", "unins001", "uninstall", "uninstaller", "uninst",
    "setup", "install", "installer", "update", "updater", "patcher",
    "vcredist", "vcredist_x86", "vcredist_x64", "dxsetup", "dxwebsetup",
    "directx", "dotnetfx", "ndp452-kb2901907-x86-x64-allos-enu",
    "oalinst", "openal", "ue4prereqsetup_x64", "ueprereqsetup_x64",
    "unitycrashhandler32", "unitycrashhandler64", "crashhandler",
    "crashreporter", "crashpad_handler", "bugsplat", "sentry",
    "quicksfv", "7z", "winrar", "notification_helper", "python", "pythonw",
    "javaw", "java", "node", "nw_elf", "d3dcompiler_47",
    # Helper scripts and tools that ship inside game folders, and that a scan
    # kept proposing as the game itself.
    "gamepro", "startwithtool", "tool", "tools", "patch", "gameupdate",
    "windowsiconupdater", "iconupdater",
    "remove tool files from game", "use me to open the tool",
}
# Exact stems are not enough: "uninstalltof.exe" sits in a folder called
# a folder whose name it echoes, so it fuzzy-matched that folder better
# than the real game did and won the pick. Kept deliberately narrow — an over-broad rule hides real
# games from the scan, which is harder to notice than a bad suggestion.
# "remove"/"use me"/… are handled as EXACT stems above rather than prefixes,
# because "Remove Heart.exe" is a plausible game title.
_NOISE_PREFIXES = ("unins", "uninstall", "uninst")
_NOISE_FRAGMENTS = (
    "uninstall", "iconupdater", "crashhandler", "crashreporter",
    "vcredist", "dxsetup", "prereqsetup",
)
# Folders never worth descending into for a game binary.
_NOISE_DIRS = {
    "redist", "redistributable", "redistributables", "_commonredist",
    "directx", "dotnet", "vcredist", "support", "docs", "documentation",
    "manual", "soundtrack", "ost", "extras", "dlc", "mods", "save", "saves",
    "savedata", "screenshots", "logs", "cache", "temp", "tmp",
    "__pycache__", ".git", ".svn", "node_modules",
}

_MAX_ENTRIES_PER_DIR = 4000          # a runaway directory must not stall the UI


@dataclass
class ScanHit:
    """One candidate game found under the scanned root."""
    folder: str                       # the top-level folder it was found in
    exe_path: str                     # best candidate executable
    name: str                         # display name derived for it
    depth: int = 0                    # how far below the folder the exe sat
    alternatives: list = field(default_factory=list)   # other executables there


def _is_noise_dir(name: str) -> bool:
    return name.strip().lower() in _NOISE_DIRS


def _is_noise_exe(path: Path) -> bool:
    stem = path.stem.strip().lower()
    if stem in _NOISE_STEMS:
        return True
    if any(stem.startswith(prefix) for prefix in _NOISE_PREFIXES):
        return True
    return any(fragment in stem for fragment in _NOISE_FRAGMENTS)


def _executables_in(folder: Path) -> list:
    """Runnable files directly inside *folder* (no recursion), noise removed."""
    from core.resolvers import is_executable_file
    out = []
    try:
        for i, child in enumerate(folder.iterdir()):
            if i >= _MAX_ENTRIES_PER_DIR:
                logger.info(f"Scan: {folder} has over {_MAX_ENTRIES_PER_DIR} entries, truncated")
                break
            try:
                if not child.is_file():
                    continue
            except OSError:
                continue
            if is_executable_file(child) and not _is_noise_exe(child):
                out.append(child)
    except OSError as e:
        logger.debug(f"Scan: cannot list {folder}: {e}")
    return out


def _score_candidate(exe: Path, folder: Path) -> tuple:
    """Rank key for "which of these IS the game" — higher sorts first.

    A stem that echoes the folder name is the strongest signal (a folder and
    its exe sharing a title); a generic stem ("game.exe", "launcher.exe") is normal for
    games and must not be punished below a random tool, only ranked under a
    matching name; size breaks the remaining ties, since the engine binary
    dwarfs its helpers.
    """
    from core.resolvers import fuzzy_score
    from core.save_detector import GENERIC_EXE_STEMS
    stem = exe.stem
    name_match = fuzzy_score(folder.name, stem)
    generic = stem.strip().lower() in GENERIC_EXE_STEMS
    try:
        size = exe.stat().st_size
    except OSError:
        size = 0
    # Generic stems rank BELOW a name match but ABOVE unrelated binaries,
    # which is what the middle term encodes.
    return (name_match, 1 if generic else 0, size)


def _pick(candidates: list, folder: Path) -> Optional[Path]:
    if not candidates:
        return None
    return max(candidates, key=lambda c: _score_candidate(c, folder))


def _scan_subfolder(folder: Path, depth: int, max_depth: int = 4,
                     cancel: Optional[Callable[[], bool]] = None) -> list[ScanHit]:
    """Scan a folder tree recursively, discovering games at each level."""
    if cancel and cancel():
        return []
    if depth > max_depth:
        return []

    from core.save_detector import derive_display_name

    # Check if this folder itself directly contains a game binary
    direct_exes = _executables_in(folder)
    if direct_exes:
        best = _pick(direct_exes, folder)
        if best is not None:
            return [ScanHit(
                folder=str(folder),
                exe_path=str(best),
                name=derive_display_name(str(best)),
                depth=depth,
                alternatives=[str(p) for p in direct_exes if p != best][:20],
            )]

    # No direct executable: search non-noise subdirectories
    subdirs: list[Path] = []
    try:
        for child in folder.iterdir():
            try:
                if child.is_dir() and not _is_noise_dir(child.name):
                    subdirs.append(child)
            except OSError:
                continue
    except OSError:
        return []

    hits: list[ScanHit] = []
    for sub in subdirs:
        if cancel and cancel():
            break
        sub_hits = _scan_subfolder(sub, depth + 1, max_depth, cancel)
        hits.extend(sub_hits)
    return hits


def find_single_game_in_folder(folder_path: str | Path, max_depth: int = 3) -> Optional[tuple[str, str]]:
    """Find the single most likely game executable inside *folder_path*.

    Returns ``(display_name, exe_path)`` or None if no game executable found.
    Used for single-folder drag & drop onto the library page.
    """
    p = Path(folder_path)
    if not p.is_dir():
        return None
    hits = _scan_subfolder(p, 0, max_depth=max_depth)
    if not hits:
        return None
    # Pick top hit (or shallowest depth)
    hits.sort(key=lambda h: h.depth)
    best = hits[0]
    return (best.name, best.exe_path)


def scan_folder_for_games(root: str, max_depth: int = 4,
                          cancel: Optional[Callable[[], bool]] = None,
                          progress: Optional[Callable[[int, int, str], None]] = None) -> list:
    """Scan *root* and return one ScanHit per game found in any subfolder.

    *max_depth* bounds how far below a folder the walk may go before giving
    up on it — an unbounded descent over a games drive is exactly the
    cold-cache walk that has to be avoided. Folders yielding nothing within
    that budget are simply absent from the result.
    """
    from core.save_detector import derive_display_name

    base = Path(root)
    try:
        if not base.is_dir():
            return []
    except OSError:
        return []

    try:
        children = sorted((c for c in base.iterdir() if c.is_dir() and not _is_noise_dir(c.name)),
                          key=lambda c: c.name.lower())
    except OSError as e:
        logger.warning(f"Scan: cannot list {root}: {e}")
        return []

    hits: list[ScanHit] = []
    total = len(children)

    # Loose executables sitting directly in the scanned root folder count too
    for exe in _executables_in(base):
        hits.append(ScanHit(folder=str(base), exe_path=str(exe),
                            name=derive_display_name(str(exe)), depth=0))

    for index, folder in enumerate(children, 1):
        if cancel and cancel():
            logger.info(f"Scan cancelled after {index - 1}/{total} folders")
            break
        if progress:
            progress(index, total, folder.name)
        folder_hits = _scan_subfolder(folder, depth=0, max_depth=max_depth, cancel=cancel)
        hits.extend(folder_hits)

    logger.info(f"Scan of {root}: {len(hits)} candidate(s) from {total} folder(s)")
    return hits


def drop_already_in_library(hits: list) -> tuple:
    """Split *hits* into (new, already_present) by resolved executable path."""
    try:
        from core.library import get_library
        lib = get_library()
    except Exception:
        return list(hits), []
    new, present = [], []
    for hit in hits:
        try:
            existing = lib.get_by_exe(hit.exe_path)
        except Exception:
            existing = None
        (present if existing is not None else new).append(hit)
    return new, present
