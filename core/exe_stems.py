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
    "vcredist", "vcredist x86", "vcredist x64", "dxsetup", "dxwebsetup",
    "directx", "dotnetfx", "ndp452-kb2901907-x86-x64-allos-enu",
    "oalinst", "openal", "ue4prereqsetup x64", "ueprereqsetup x64",
    "d3dcompiler 47",
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
    "remove tool files from game", "use me to open the tool",
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

# Real game executables whose NAME says nothing: shipped under a generic
# stem, so the title has to come from the install folder instead. These are
# launchable — they must never be treated as noise.
GENERIC_TITLE_STEMS = expand_stems({
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
GENERIC_DIR_STEMS = expand_stems({
    "bin", "lib", "lib64", "common", "build", "dist", "desktop",
    "game unpacked",
})
