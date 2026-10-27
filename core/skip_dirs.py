"""
SaveSync - Shared directory skip-lists.

Two related but deliberately DIFFERENT sets share a common base of
game-asset / VCS / engine-UI folder names that are never save data:

- ``BACKUP_SKIP_DIRS``  — used when walking a *confirmed* save folder to
  decide which subdirectories to exclude from a backup's content. Adds
  ``bin``/``libs`` (compiled game code, never saves) on top of the common
  base. Kept intentionally narrow: a backup content-walk must not skip
  broad user folders like ``desktop`` or ``resources`` that a save path
  might legitimately sit under.

- ``SCAN_SKIP_DIRS``   — used by the save-path *detector* when scanning
  broad roots (whole user profile, install dirs, …). Adds OS/system,
  browser, antivirus, messaging and package-manager folders that a scan
  would otherwise waste time descending into and that never hold saves.

Both are exposed as ``frozenset`` and consumed elsewhere under their
historical names (``core.backup._BACKUP_SKIP_DIRS`` and
``core.save_detector._SKIP_DIRS``) so existing importers keep working.
"""

# ── Ren'Py's "sync" mirror: excluded by WHERE it is, not by its name ─────────
# Ren'Py keeps an internal mirror of the save directory it sits in
# (AppData/Roaming/RenPy/<GameFolder>/sync) - every file in it is a duplicate
# of one already directly under <GameFolder>, kept there for Ren'Py's own
# cloud-sync bookkeeping. The recursive scanner used to be able to score and
# propose it as an independent save path: confirmed, it doubled that game's
# backups (every real save zipped in twice, under two different member names)
# and, once its own copy of a file drifted a beat behind the original, the
# mismatch could make a later session's state look like it had reverted to an
# old backup. The file watcher proposed it too (a write in it became "the
# folder a save was written in").
#
# It is NOT a name to ban everywhere: a folder called "sync" in another
# game's saves, or anywhere else, may hold real data, and a ban by name
# dropped it from scans and from backups without a word. So it is recognised
# by its place - <Ren'Py data folder>/<game>/sync - and nowhere else.

def renpy_data_roots() -> list:
    """Where Ren'Py keeps the saves it writes outside the game's own folder: AppData/Roaming/RenPy
    on Windows, ~/.renpy on Linux (and ~/Library/RenPy on macOS). Read from the environment each
    time, so a changed APPDATA is followed."""
    import os
    import platform
    from pathlib import Path
    system = platform.system()
    if system == "Windows":
        appdata = os.getenv("APPDATA", "")
        return [Path(appdata) / "RenPy"] if appdata else []
    home = Path.home()
    roots = [home / ".renpy"]
    if system == "Darwin":
        roots.append(home / "Library" / "RenPy")
    return roots


def is_renpy_sync_mirror(path) -> bool:
    """True when *path* is Ren'Py's own ``sync`` folder - ``<Ren'Py data folder>/<game>/sync`` -
    or something inside it. A folder of that name anywhere else is not."""
    try:
        import os
        from pathlib import Path
        # Normalised first (".." and "." collapsed, a pure string step - the disk is never
        # touched): a path that only passes through the folder on its way elsewhere is not in it,
        # and one that reaches it by a detour is.
        low = [p.lower() for p in Path(os.path.normpath(str(path))).parts]
    except (OSError, ValueError, TypeError):
        return False
    if "sync" not in low:
        return False
    for root in renpy_data_roots():
        base = [p.lower() for p in root.parts]
        n = len(base)
        if len(low) >= n + 2 and low[:n] == base and low[n + 1] == "sync":
            return True
    return False


def renpy_sync_between(root, rel_parts) -> bool:
    """Is one of the folders between *root* and a file (given as the file's path relative to
    *root*, its own name last) Ren'Py's ``sync`` mirror? For the walks that look at a save folder
    from the inside: a ``sync`` folder the person chose AS the save folder is theirs to ask for,
    and so is one in a game that is not Ren'Py's."""
    try:
        from pathlib import Path
        base = Path(str(root))
        for i, part in enumerate(rel_parts[:-1]):
            if part.lower() == "sync" and is_renpy_sync_mirror(base.joinpath(*rel_parts[:i + 1])):
                return True
    except (OSError, ValueError, TypeError):
        pass
    return False


# ── Common base: game assets, engine UI folders, VCS/build metadata ─────────
# Never save data, skipped by BOTH the backup content-walk and the scanner.
_COMMON_SKIP_DIRS = frozenset({
    "shader", "shaders", "textures", "audio", "music", "sounds", "video",
    "movies", "cutscenes", "plugins", "localization", "locale", "locales",
    "fonts", "lib", "__pycache__", ".git", ".svn", "node_modules",
    # RPG Maker MZ/MV (NW.js/Chromium-based) and similar web-UI engines:
    # game database/UI asset folders, never saves.
    "data", "rmmz-game", "css", "js", "img",
    "dictionaries", "dictionary", "effects",
    # Player/runtime profile roots these engines create under LOCALAPPDATA,
    # named after the NW.js package rather than the game. What is inside is
    # the Chromium profile of the player — cache, cookies, a "userdata"
    # folder holding the PLAYER's own settings — not the saves of any one
    # game. "userdata" is a save-folder hint on its own (see
    # SAVE_FOLDER_HINTS), which is why these roots have to be named here or
    # the profile gets offered as a save path.
    "mmz-game", "mv-game", "rmmv-game",
})

# ── Backup content-walk exclusions ──────────────────────────────────────────
# Common base plus compiled-code folders. Deliberately does NOT include the
# broad OS/browser/app folders in SCAN_SKIP_DIRS: those are scan-time noise,
# but excluding them from a confirmed save folder's content-walk could drop
# real saves that happen to live under a same-named subfolder.
BACKUP_SKIP_DIRS = _COMMON_SKIP_DIRS | frozenset({
    "bin", "libs",
})

# ── Detector scan exclusions ────────────────────────────────────────────────
# Common base plus OS/system, browser, antivirus, messaging, and
# update/package-manager folders — pure noise when scanning broad roots.
SCAN_SKIP_DIRS = _COMMON_SKIP_DIRS | frozenset({
    "windows", "system32", "syswow64",
    "programdata", "appdata/local/microsoft", "appdata/local/windows",
    "temp", "tmp", "cache", "log", "logs", "crash", "crashes",
    "monobleedingedge", "streamingassets",
    "resources", "managed",
    "desktop",        # user desktop folder, never saves
    "savesync",
    # Security / antivirus / ad-blocking software
    "adguard", "adguardbeta", "malwarebytes", "avast", "avira",
    "kaspersky", "mcafee", "norton", "bitdefender", "eset", "mbam",
    "windows defender", "windowsdefender",
    # Browsers
    "google", "chrome", "firefox", "mozilla", "microsoft edge",
    "msedge", "opera", "vivaldi", "brave", "chromium",
    # Communication / productivity apps (never game saves)
    "discord", "slack", "teams", "zoom", "skype", "telegram",
    "whatsapp", "signal", "notion", "obsidian",
    # System update / package managers
    "windowsupdate", "windowsstore", "nvidia", "amd", "intel",
})
