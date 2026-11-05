"""
SaveSync - Shared executable-stem vocabulary.

Three independent passes ask a version of the same question about an
executable's name, and each used to answer it from its own hand-written list:

- ``core.exe_scan``      — which executable in this folder IS the game?
- ``core.save_detector`` — is this name informative enough to be a title?
- ``core.monitor``       — is this running process a game at all?

The lists were never fully in sync, and the cost of that is silent: an
updater added to one of them keeps winning the pick, keeps being proposed as
a title, or keeps being announced as a launched game from the other two.
``GameUpdate.exe`` was exactly that — listed as scan noise and as a generic
title, still announced by the process monitor as a game the user had started.

So the vocabulary lives here ONCE, cut into families by what the program
actually is, and each consumer composes the families it cares about (the same
shape as ``core.skip_dirs``, where two skip-lists share one base). A new
updater is one entry in ``UPDATER_STEMS`` and every pass learns it.

The families are NOT interchangeable, which is why they stay separate:
"never the game" (an installer, a crash handler) and "a real game exe with an
uninformative name" ("game.exe", "launcher.exe") are different verdicts, and
merging them would either hide games from the scan or send "Game" to the
metadata APIs as a title.

Spelling is handled by a combinator rather than by hand — see stem_variants.
"""
import re
from pathlib import Path
from typing import Optional

# Space, underscore and hyphen are the same word break as far as an
# executable name is concerned. A dot is NOT: it separates a real name part
# ("nvdisplay.container") and collapsing it would merge unrelated stems.
_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize_stem(name: str) -> str:
    """An exe stem reduced to its comparison form: lowercase, word breaks
    removed. ``"Game Update"``, ``"game_update"`` and ``"GameUpdate"`` all
    come out as ``"gameupdate"``."""
    return _SEPARATORS.sub("", (name or "").strip().lower())


def stem_variants(name: str) -> frozenset[str]:
    """Every spelling of *name* a program might actually ship under.

    ``"game update"`` yields ``gameupdate``, ``game update``,
    ``game_update`` and ``game-update`` — the four forms that used to be
    typed out by hand in two different files, where any one of them could be
    (and was) forgotten. A single-word name has only itself.

    Write the multi-word form in the lists below and the joins come free:
    listing ``"gameupdate"`` would generate only itself.
    """
    parts = [p for p in _SEPARATORS.split((name or "").strip().lower()) if p]
    if not parts:
        return frozenset()
    if len(parts) == 1:
        return frozenset(parts)
    return frozenset(sep.join(parts) for sep in ("", " ", "_", "-"))


def expand_stems(names) -> frozenset[str]:
    """Union of stem_variants over *names* — how every family below is built."""
    out: set[str] = set()
    for name in names:
        out |= stem_variants(name)
    return frozenset(out)


def stem_in(name: str, stems) -> bool:
    """True when *name* is listed in *stems* under any spelling.

    Tests the name as written, then normalized, then normalized with dots
    treated as a word break too — so a set built with expand_stems matches
    even a spelling the combinator did not emit (``"GAME  UPDATE"``,
    ``"Game.Update"``).

    The dot is handled HERE and not in normalize_stem, and the distinction
    is the whole point. normalize_stem is the module's canonical form: it
    is what stems are grouped and compared by, and folding dots into it
    would genuinely merge unrelated names, which is why the separator
    class deliberately leaves the dot out (``nvdisplay.container``).
    A membership question is narrower than a canonical form — it asks only
    "is this one of the names we listed", and every listed name is itself
    dot-free, so the extra probe can add a match but can never merge two
    entries. Without it ``"Game.Update"`` fell through both tests (the dot
    survives normalization, and no generated variant contains one) and
    stem_in returned False for the very example this docstring cites.
    """
    s = (name or "").strip().lower()
    if s in stems or normalize_stem(s) in stems:
        return True
    return "." in s and normalize_stem(s.replace(".", " ")) in stems


# ── Families ────────────────────────────────────────────────────────────────

# Installers and uninstallers. "unins000"/"unins001" are Inno Setup's
# generated names; the prefix rule in core.exe_scan catches the rest.
INSTALLER_STEMS = expand_stems({
    "setup", "install", "installer",
    "uninstall", "uninstaller", "uninst", "unins000", "unins001",
})

# Updaters and patchers. Written multi-word so every join is generated —
# this family is the reason this module exists.
UPDATER_STEMS = expand_stems({
    "update", "updater", "auto update", "auto updater", "patcher",
    "game update", "game updater",
    "icon updater", "windows icon updater",
})

# Crash reporters. Shipped by the engine, run alongside the game, never it.
CRASH_HANDLER_STEMS = expand_stems({
    "crash handler", "crash reporter", "crashpad handler",
    "unity crash handler", "unitycrashhandler32", "unitycrashhandler64",
    "bugsplat", "sentry",
})

# Anti-cheat services. Installed BESIDE the game, often in the game's own
# install directory or a subfolder of it, and running for the whole
# session — unlike the crash handlers above, not necessarily small: a
# heavy anti-cheat service can transiently out-weigh the actual game in
# memory. core.monitor's directory-match fallback picks whichever process
# in the install folder is currently using the most memory as "the game";
# without excluding these by name first, that comparison has no way to
# tell a well-known anti-cheat service apart from the thing it's guarding.
ANTICHEAT_STEMS = expand_stems({
    "easyanticheat", "easyanticheat launcher", "easyanticheat eos setup",
    "battleye", "beservice",
    "pnkbstra", "pnkbstrb",
    "vgc", "vgtray",
})

# Redistributables and prerequisites a game installer drops in its folder.
REDIST_STEMS = expand_stems({
    "vcredist", "vcredist x86", "vcredist x64", "dxsetup", "dxwebsetup",  # i18n-ignore (file-name stems, data)
    "directx", "dotnetfx", "ndp452-kb2901907-x86-x64-allos-enu",
    "oalinst", "openal", "ue4prereqsetup x64", "ueprereqsetup x64",  # i18n-ignore (file-name stems, data)
    "d3dcompiler 47",  # i18n-ignore (file-name stems, data)
})

# Interpreters and archivers that a game folder may contain but that are
# never the title. Note "nw"/"nwjs" are deliberately ABSENT: NW.js *is* the
# executable an RPG Maker MV/MZ game runs as, so it must stay launchable —
# it is only uninformative as a NAME (see GENERIC_TITLE_STEMS).
RUNTIME_STEMS = expand_stems({
    "python", "pythonw", "java", "javaw", "node", "nw elf",
    "quicksfv", "7z", "winrar", "notification helper",
})

# Helper tools shipped inside a game folder that a scan kept proposing as
# the game itself. Distinctive names — nothing else is plausibly called
# this, so they are safe anywhere a stem is tested.
HELPER_TOOL_STEMS = expand_stems({
    "gamepro", "start with tool",
    "remove tool files from game", "use me to open the tool",  # i18n-ignore (file-name stems, data)
})

# The same idea in bare dictionary words. Kept apart because these are ALSO
# ordinary folder names: core.watcher reuses the process ignore-list to veto
# path components, and a save path under a folder called "Tools" or "Patch"
# must not be vetoed by a rule aimed at tool executables. Safe where the
# subject is definitely an exe stem (the folder scan, the title derivation),
# never as path evidence.
AMBIGUOUS_TOOL_STEMS = expand_stems({
    "tool", "tools", "patch",
})

# Everything that is never the game, whatever it scores otherwise.
NEVER_A_GAME_STEMS = (
    INSTALLER_STEMS | UPDATER_STEMS | CRASH_HANDLER_STEMS | ANTICHEAT_STEMS
    | REDIST_STEMS | RUNTIME_STEMS | HELPER_TOOL_STEMS | AMBIGUOUS_TOOL_STEMS
)

# Programs that ship BESIDE an application and are never a game, in the
# process-monitor sense: a running one must not be announced as a launched
# game. Installing a game runs vcredist/DXSETUP/oalinst for minutes, which
# clears the monitor's runtime threshold, so the redistributables belong
# here as much as the installer that dropped them.
#
# Two families are deliberately LEFT OUT, and both would cause a real miss:
#
# - RUNTIME_STEMS. An interpreter is how a whole class of games runs —
#   Minecraft IS javaw.exe, Ren'Py IS python. Ignoring the runtime would
#   ignore the game.
# - AMBIGUOUS_TOOL_STEMS. core.watcher reuses this set to veto save-path
#   components, where "tools"/"patch" are ordinary folder names.
NEVER_A_GAME_PROCESS_STEMS = (
    INSTALLER_STEMS | UPDATER_STEMS | CRASH_HANDLER_STEMS | ANTICHEAT_STEMS
    | REDIST_STEMS | HELPER_TOOL_STEMS
)

# An ENGINE's own player/runtime executable, as opposed to the game's. Many
# engines run a title through a fixed binary that is named after the engine,
# not the game: the game ships a launcher (or the user starts the player
# directly) and the process that actually runs is "bakinplayer.exe" - often
# buried in a "data" folder. Used as a name it labels every title built with
# that engine identically, so the title comes from the install folder instead,
# exactly as for "game.exe".
#
# These are real, launchable programs: they are in GENERIC_TITLE_STEMS (below)
# and must NEVER join a "never a game" family or the process monitor would
# stop recognising the game they run.
#
# Only names an engine genuinely ships under. Multi-word forms are written
# spaced so every join is generated (see stem_variants).
ENGINE_PLAYER_STEMS = expand_stems({
    # RPG Developer Bakin: the player, and the MTool translation wrapper some
    # releases put around it (data/MtoolBakinPlayerWrapper.exe).
    "bakin player", "mtool bakin player wrapper",
    # RPG Maker 2000 / 2003 runtime, and its open-source player.
    "rpg rt", "easyrpg player",
    # Kirikiri / KAG (Kirikiri 2 "TVP", Kirikiri Z).
    "tvpwin32", "tvpwin64", "krkr", "krkrz", "kirikiri",
    # Siglus / RealLive (VisualArt's) engines; the Steam releases ship the Siglus player as "SiglusEngine_Steam.exe".
    "siglusengine", "siglusengine steam", "reallive",
    # Electron wrapper (the NW.js one, "nw"/"nwjs", is listed below).
    "electron",
})

# Real game executables whose NAME says nothing: shipped under a generic
# stem, so the title has to come from the install folder instead. These are
# launchable — they must never be treated as noise.
GENERIC_TITLE_STEMS = ENGINE_PLAYER_STEMS | expand_stems({
    "game", "game64", "game32", "launcher", "launch", "start",
    "main", "app", "application", "run", "play", "client",
    "bootstrap", "bootstrapper", "loader", "engine",
    "nw", "nwjs",   # NW.js runtime exe (RPG Maker MV/MZ ship "nw.exe")
    "savesync", "save",
    "menu", "title", "gui", "ui", "frontend",
    "runtime", "redist", "redistributable",
    "game launcher",
    "win64", "win32", "x64", "x86", "win",
    "release", "debug", "test", "dev",
    "program", "executable", "exe",
    "default", "settings", "config",
})

# Build/library folder names that are also uninformative as a title — used
# for folder-name filtering and the install-folder walk-up, not just stems.
# ── Unreal Engine's packaged binary ─────────────────────────────────────────────────────────
# A packaged Unreal game runs as "<Project>-Win64-Shipping.exe" - the platform and the build
# configuration are part of the name - deep inside its install:
#
#     <install>/<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe
#
# The name is the engine project's CODE NAME, which says nothing about the title, and beside the
# install folder's top there is usually a launcher stub named after the game that starts it: two
# programs, one game. The layout is rigid, so it is recognised by where the file is, not guessed.
_UNREAL_BINARY_RE = re.compile(r"^.+-(?:win64|win32|wingdk)-(?:shipping|test|development|debuggame)$",
                               re.IGNORECASE)
_UNREAL_PLATFORM_DIRS = frozenset({"win64", "win32", "wingdk"})


def unreal_install_root(exe_path) -> Optional[Path]:
    """The folder an Unreal Engine game is installed in, from the path of its packaged binary
    (``<install>/<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe``); None for any other path.
    Pure string work: the disk is not touched."""
    try:
        p = Path(str(exe_path))
        if not _UNREAL_BINARY_RE.match(p.stem):
            return None
        platform_dir = p.parent
        binaries = platform_dir.parent
        project = binaries.parent
        root = project.parent
        if (platform_dir.name.lower() in _UNREAL_PLATFORM_DIRS and binaries.name.lower() == "binaries"
                and project.name and root.name and root != project):
            return root
    except (OSError, ValueError, TypeError):
        pass
    return None


def _same_path(a, b) -> bool:
    return str(a).replace("\\", "/").rstrip("/").casefold() == str(b).replace("\\", "/").rstrip("/").casefold()


def unreal_install_roots(exe_paths) -> set:
    """The install folders (normalised) of every Unreal game among *exe_paths*. One pass, so a long
    library can be asked about once instead of once per candidate."""
    roots = set()
    for exe in exe_paths:
        root = unreal_install_root(exe)
        if root is not None:
            roots.add(str(root).replace("\\", "/").rstrip("/").casefold())
    return roots


def in_unreal_install(exe_path, roots) -> bool:
    """True when *exe_path* is a part of one of the Unreal installs in *roots* (from
    unreal_install_roots): a packaged binary of it, or a program in its install folder."""
    try:
        root = unreal_install_root(exe_path)
        folder = root if root is not None else Path(str(exe_path)).parent
        return str(folder).replace("\\", "/").rstrip("/").casefold() in roots
    except (OSError, ValueError, TypeError):
        return False


def same_unreal_install(exe_a, exe_b) -> bool:
    """True when the two executables are parts of ONE Unreal Engine game: two packaged binaries of
    the same install, or a packaged binary and a launcher that sits in that install's folder. False
    for everything else - two programs sharing a folder are not assumed to be one game."""
    root_a, root_b = unreal_install_root(exe_a), unreal_install_root(exe_b)
    try:
        if root_a and root_b:
            return _same_path(root_a, root_b)
        if root_a:
            return _same_path(root_a, Path(str(exe_b)).parent)
        if root_b:
            return _same_path(root_b, Path(str(exe_a)).parent)
    except (OSError, ValueError, TypeError):
        pass
    return False


# Folders that hold many unrelated programs: two programs "in" one of these are not one install.
_SHARED_FOLDERS = frozenset({
    "downloads", "desktop", "documents", "program files", "program files (x86)", "games", "steamapps", "common", "windows",
    "users", "appdata", "local", "roaming", "temp", "tmp", "system32", "syswow64", "programdata", "bin", "apps",
})


def in_install_tree(outer_exe, inner_exe) -> bool:
    """True when *inner_exe* sits in the folder of *outer_exe* or below it: the shape of a launcher and the program it
    starts. Pure string work. A folder that holds many unrelated programs (a drive root, Downloads, Program Files, a library
    root such as ``Games``) is not an install, so nothing in it is "inside" anything."""
    try:
        outer = Path(str(outer_exe)).parent
        inner = Path(str(inner_exe)).parent
        o = str(outer).replace("\\", "/").rstrip("/").casefold()
        i = str(inner).replace("\\", "/").rstrip("/").casefold()
        if not o or not outer.name or outer.name.casefold() in _SHARED_FOLDERS or outer == outer.parent:
            return False
        return i == o or i.startswith(o + "/")
    except (OSError, ValueError, TypeError):
        return False


def is_unreal_wrapper_pair(exe_a, exe_b) -> bool:
    """True when one program is an Unreal game's launcher STUB (any program beside the install's top folder) and the other its packaged binary: one program
    started through a wrapper. Not two packaged binaries of one root - a game that ships ``Binaries/Win32`` and ``Binaries/Win64`` has two builds, which are
    versions (same_unreal_install is true for them too, and that is right for announcing a game once, not for deciding they are one program)."""
    root_a, root_b = unreal_install_root(exe_a), unreal_install_root(exe_b)
    if bool(root_a) == bool(root_b):
        return False
    root, other = (root_a, exe_b) if root_a else (root_b, exe_a)
    try:
        return _same_path(root, Path(str(other)).parent)
    except (OSError, ValueError, TypeError):
        return False


# Folders a build lives in beside its sibling builds, which name no title: the install is the one above them.
_PLATFORM_DIRS = frozenset({
    "bin", "bin32", "bin64", "x86", "x64", "x86_64", "x86-64", "amd64", "win32", "win64", "wingdk", "binaries", "release", "debug",
    "32", "64", "32bit", "64bit", "windows",
})


def install_root(exe_path) -> Optional[Path]:
    """The folder an executable's INSTALL is rooted at: an Unreal game's install root, otherwise the first folder above the program that is not a platform or
    runtime folder (``x64``, ``win64``, ``bin``, ``Binaries``…). None when there is no such folder. Pure string work."""
    try:
        unreal = unreal_install_root(exe_path)
        if unreal is not None:
            return unreal
        folder = Path(str(exe_path)).parent
        while folder.name and folder.name.casefold() in _PLATFORM_DIRS and folder != folder.parent:
            folder = folder.parent
        return folder if folder.name else None
    except (OSError, ValueError, TypeError):
        return None


def shares_install(exe_a, exe_b) -> bool:
    """True when two different programs belong to ONE install: the same install root (one folder, or builds that sit in ``x86``/``x64``, ``bin/win32``/
    ``bin/win64``, ``Binaries/Win32``/``Binaries/Win64`` beside each other), or one inside the folder of the other (a launcher and the game below it).
    False for programs that merely share a folder that holds many (a drive root, Downloads, Program Files, a library root such as ``Games``) and for two
    different games under one. A 32-bit and a 64-bit build of a game share an install; they are still two programs, and two versions."""
    try:
        ra, rb = install_root(exe_a), install_root(exe_b)
        if (ra and rb and ra.name.casefold() not in _SHARED_FOLDERS and rb.name.casefold() not in _SHARED_FOLDERS and _same_path(ra, rb)):
            return True
    except (OSError, ValueError, TypeError):
        pass
    return in_install_tree(exe_a, exe_b) or in_install_tree(exe_b, exe_a)


GENERIC_DIR_STEMS = expand_stems({
    "bin", "lib", "lib64", "common", "build", "dist", "desktop",
    "game unpacked",
    # Where an engine keeps its runtime: Bakin's player lives in
    # "<install>/data/bakinplayer.exe", so the folder the walk-up meets first
    # is "data" - which names nothing. The install is the one above it.
    "data",
})
