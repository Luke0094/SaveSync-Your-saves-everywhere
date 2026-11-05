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

# ── SaveSync's own data folder: excluded by WHERE it is ──────────────────────
# %APPDATA%/SaveSync holds the backups, the logs, the cache and the library -
# written by SaveSync itself, never by a game, and never a save location.
#
# The scans and the open-file tracker already kept it out (the "savesync" skip
# name, the detector's own-data guard). The file WATCHER did not: it funnels
# every event of a game-scoped watch AND of the common roots (AppData/Roaming,
# Documents, ...) through one handler that asked only about Ren'Py's mirror.
# SaveSync names each game's backup folder after the game, so a backup written
# under %APPDATA%/SaveSync/backups/<Game> carries the game's name in its path,
# passes the common-root name filter, and its parent - "backups" - matches the
# built-in "backup" save-folder hint. The folder was then proposed to the user
# in the save-confirmation panel as if the game had written it.
#
# Recognised by place (the data folder, resolved once) rather than by a bare
# folder name, so a game's own folder called "SaveSync" is not collateral.

_OWN_DATA_PARTS: tuple = ()


def _own_data_parts() -> tuple:
    """The data folder as tuples of case-folded components - as configured AND
    as resolved (they differ behind a junction or a symlink, and watcher events
    carry the spelling the watch was armed with). Resolved on first use and
    kept, because the watcher asks about every filesystem event."""
    global _OWN_DATA_PARTS
    if not _OWN_DATA_PARTS:
        import os
        try:
            from core.constants import USER_DATA_DIR
            spellings = {os.path.normpath(str(USER_DATA_DIR)),
                         os.path.normpath(os.path.realpath(str(USER_DATA_DIR)))}
        except (OSError, ValueError, ImportError):
            return ()
        _OWN_DATA_PARTS = tuple(sorted(
            tuple(p.lower() for p in sp.split(os.sep) if p) for sp in spellings))
    return _OWN_DATA_PARTS


# The P2P receive folder is SaveSync's too (files a friend sends by hand land in
# it, laid out as the sender had them - often with the game's name and its save
# files), but the person can point it ANYWHERE in Settings (p2p_files_dir), so it
# cannot be recognised by sitting under the data folder. Its default does, and
# is covered above.
#
# A folder picked "anywhere" may also be a place that real saves live in - the
# Documents folder, a games folder - and excluding that wholesale would hide
# those saves from every route without a word, which is a far worse failure than
# the spurious proposal this guards against. So it is only excluded when it is a
# folder of its own: not a drive root, the profile, an OS save root or an
# ancestor of one, and with no game of the library inside it.
_P2P_PARTS_CACHE: dict = {"raw": None, "at": 0.0, "parts": ()}
_P2P_PARTS_TTL_S = 30.0   # the library half of the answer can change under a fixed setting


def _split_lower(path) -> tuple:
    import os
    return tuple(p.lower() for p in os.path.normpath(str(path)).split(os.sep) if p)


def _is_prefix(short: tuple, long: tuple) -> bool:
    return len(long) >= len(short) and long[:len(short)] == short


def _p2p_exclusion_parts(raw: str) -> tuple:
    """Component tuples of the custom P2P folder (as configured and as resolved),
    or () when there is none or it must not be excluded wholesale - see above."""
    if not raw:
        return ()          # the default folder is under the data folder: already covered
    import os
    try:
        from core.p2p.paths import files_dir
        chosen = files_dir()
        spellings = {_split_lower(chosen), _split_lower(os.path.realpath(str(chosen)))}
        spellings.discard(())
        if not spellings:
            return ()
        from pathlib import Path
        from core.watcher import _get_common_save_roots
        broad = [_split_lower(Path.home())] + [_split_lower(r) for r in _get_common_save_roots()]
        for sp in spellings:
            # a root, the profile or an OS save root, or something that holds one
            if len(sp) <= 1 or any(_is_prefix(sp, b) for b in broad):
                return ()
        from core.library import get_library
        for g in get_library().all_games():
            for p in [getattr(g, "exe_path", "") or ""] + list(getattr(g, "save_paths", None) or []):
                if p and any(_is_prefix(sp, _split_lower(p)) for sp in spellings):
                    return ()   # shared with a game: not a folder of its own
        return tuple(sorted(spellings))
    except Exception:
        return ()          # cannot tell -> do not exclude: hiding a real save is the worse error


def _p2p_files_parts() -> tuple:
    """The custom P2P folder's components, or (): recomputed when the setting
    changes (so a new folder takes effect at once) and every few seconds after."""
    try:
        from core.config_manager import get_config
        raw = str(get_config().get("p2p_files_dir", "") or "").strip()
    except Exception:
        return ()
    import time
    now = time.monotonic()
    c = _P2P_PARTS_CACHE
    if c["raw"] != raw or now - c["at"] >= _P2P_PARTS_TTL_S:
        c["parts"] = _p2p_exclusion_parts(raw)
        c["raw"], c["at"] = raw, now
    return c["parts"]


def is_own_data_path(path) -> bool:
    """True when *path* is inside a folder SaveSync itself writes to: its data
    folder, or the custom folder the person chose for P2P-received files.

    Compared component by component, so a sibling that merely starts with the
    same letters ("SaveSync2") is not the folder. Never touches the disk for
    *path*; the watcher calls this for every event."""
    try:
        parts = _split_lower(path)
    except (ValueError, TypeError):
        return False
    return any(_is_prefix(b, parts) for b in _own_data_parts() + _p2p_files_parts())


# ── The kernel's virtual folders: excluded by WHERE they are ─────────────────
# /dev/shm holds the shared-memory files a process opens for itself. Wine and Proton keep one open (wine-<id>-fsync) for as long as a Windows game
# runs, written to since it launched - so the open-file tracker read it as the game saving, and proposed /dev/shm as its save folder. /proc and
# /sys are the kernel's own. Nothing a player would back up lives in any of them. /run is NOT in the list: removable and second disks are mounted
# under /run/media, and a game kept on one saves there.
_VIRTUAL_ROOTS = ("dev", "proc", "sys")


def is_os_virtual_location(path) -> bool:
    """True when *path* is in, or under, /dev, /proc or /sys (Unix only). Never touches the disk."""
    import os
    if os.name == "nt":
        return False
    try:
        text = str(path)
        parts = [p for p in os.path.normpath(text).split("/") if p]
    except (ValueError, TypeError):
        return False
    return text.startswith("/") and bool(parts) and parts[0] in _VIRTUAL_ROOTS


# ── Wine's and Proton's own files in a prefix: excluded by WHERE they are ─────
# A prefix holds the game's saves (drive_c/users/<user>/..., Documents, AppData) AND what Wine keeps for itself: the registry files and update stamps
# at its root, the dosdevices links, the Windows folder (drive_c/windows), and - in a Proton compat folder - the bookkeeping beside pfx/. A game's own
# save is none of these, and the ones written while a game runs (the registry is saved as it changes) read as "files written during the session".
# Recognised by place: a folder merely named "windows" or a file named "version" anywhere else is left alone, and the root files count only
# where a drive_c sits beside them.
_WINE_ROOT_FILES = frozenset({"user.reg", "system.reg", "userdef.reg", ".update-timestamp", ".update-arch", ".update", "pfx.lock", "config_info",
                              "tracked_files", "version"})


def _is_prefix_root(folder: str) -> bool:
    import os
    return os.path.isdir(os.path.join(folder, "drive_c")) or os.path.isdir(os.path.join(folder, "pfx", "drive_c"))


def is_wine_internal_path(path) -> bool:
    """True when *path* is something Wine or Proton keep for themselves inside a prefix (Unix only). Touches the disk only for a file named like
    one of the prefix's root files, or under a ``dosdevices`` folder - to see whether a prefix really is there."""
    import os
    if os.name == "nt":
        return False
    try:
        text = str(path).replace("\\", "/")
    except (ValueError, TypeError):
        return False
    parts = [p for p in text.split("/") if p]
    low = [p.lower() for p in parts]
    for i, p in enumerate(low):
        if p == "drive_c" and i + 1 < len(low) and low[i + 1] == "windows":
            return True
        if p == "dosdevices" and i >= 1 and _is_prefix_root("/" + "/".join(parts[:i])):
            return True
    if low and low[-1] in _WINE_ROOT_FILES and len(parts) >= 2:
        return _is_prefix_root("/" + "/".join(parts[:-1]))
    return False


# ── The machinery that runs a Windows game on Linux: excluded by WHERE it is ──
# Under umu or Proton a game's process tree holds more than the game: the Steam Linux Runtime container's tools (pressure-vessel-wrap, pv-verify,
# pv-adverb, srt-bwrap, ...), the Proton and Wine builds' own programs (the wineserver, the loaders), and Wine's programs in a prefix's Windows
# folder (winedevice, plugplay, rpcss, ...). Every one of them used to be announced as an unknown game. Recognised by the folder they live in -
# never by name: a game called "proton" or "runtime" is not touched, one kept in steamapps/common is, unless it is Proton or the runtime itself.


def is_wine_runtime_program(path) -> bool:
    """True when the program at *path* belongs to the machinery that runs Windows games on Linux (Unix only), not to a game. Never touches the disk."""
    import os
    if os.name == "nt":
        return False
    try:
        low = str(path).replace("\\", "/").lower()
    except (ValueError, TypeError):
        return False
    parts = [p for p in low.split("/") if p]
    if not parts:
        return False
    if "/drive_c/windows/" in low or parts[-1] in ("wineserver", "wineserver64"):
        return True
    folders = parts[:-1]
    for i, p in enumerate(folders):
        # the container runtime: <...>/pressure-vessel/..., <...>/steam-runtime-tools-0/..., steamapps/common/SteamLinuxRuntime_<name>
        if p == "pressure-vessel" or p.startswith("steam-runtime") or p.startswith("steamlinuxruntime"):
            return True
        # a Proton / Wine build: a Steam compatibilitytools.d folder, or steamapps/common/Proton*
        if p == "compatibilitytools.d":
            return True
        if p == "common" and i >= 1 and folders[i - 1] == "steamapps" and i + 1 < len(folders) and folders[i + 1].startswith("proton"):
            return True
    return False


# ── The Steam client's own data: excluded by WHERE it is ─────────────────────────────────────────────────────────────
# The Steam client keeps a Chromium profile for its interface (steamwebhelper, in `Steam/htmlcache` - on Windows under %LOCALAPPDATA%, elsewhere
# under `Steam/config/htmlcache`), its caches, logs and crash dumps. It writes to them the whole time a game runs, so the tracker read the
# "Network" folder of that profile as the game's saves. None of it is a save. What lives BESIDE it is, and is not touched: `userdata/<id>/<app>/remote`
# (Steam Cloud) and `steamapps/common/<game>` (a game's own files). Recognised by place - the folder right under a `Steam` folder - never by name.
_STEAM_CLIENT_DIRS = frozenset({"htmlcache", "appcache", "depotcache", "logs", "dumps", "package"})


# What Steam writes into a game's own folder under `userdata/<account>/<app>/`: the Steam Cloud bookkeeping (remotecache.vdf - which file is where, with which
# hash, as of when). It changes at every sync and is never a save. The Cloud saves themselves live in `userdata/<account>/<app>/remote/`, one level down; the app
# folder holds nothing else of a game's (screenshots aside). A file by this name is Steam's wherever it is, so it is the one exclusion made by NAME.
_STEAM_BOOKKEEPING_FILES = frozenset({"remotecache.vdf"})


def is_steam_client_data(path) -> bool:
    """True when *path* is in, or under, a folder the Steam client keeps for itself (see above), or is the Steam Cloud bookkeeping inside `userdata`:
    the file remotecache.vdf, the folder of the app that holds it (`userdata/<account>/<app>`, exactly - its `remote` folder is the saves), the account's
    folder and its `config`. Never touches the disk."""
    try:
        parts = [p for p in str(path).replace("\\", "/").lower().split("/") if p]
    except (ValueError, TypeError):
        return False
    if parts and parts[-1] in _STEAM_BOOKKEEPING_FILES:
        return True
    for i, p in enumerate(parts):
        # <Steam>/userdata/<account>[/<app>][/config]: the account and app folders are numbers, which is what tells Steam's from a game's own "userdata"
        if p == "userdata" and "steam" in parts[:i] and i + 1 < len(parts) and parts[i + 1].isdigit():
            rest = parts[i + 2:]
            if not rest or (len(rest) == 1 and rest[0].isdigit()) or rest[0] == "config":
                return True
        if p != "steam" or i + 1 >= len(parts):
            continue
        nxt = parts[i + 1]
        if nxt in _STEAM_CLIENT_DIRS:
            return True
        if nxt == "config" and i + 2 < len(parts) and parts[i + 2] == "htmlcache":
            return True
        # %LOCALAPPDATA%/Steam holds nothing but that profile
        if i >= 2 and parts[i - 1] == "local" and parts[i - 2] == "appdata":
            return True
    return False


# ── Unity's own files in a game's data folder: excluded by WHERE they are ────────────────────────────────────────────
# A Unity game's data folder (Application.persistentDataPath: `AppData/LocalLow/<company>/<game>` on Windows, `~/.config/unity3d/<company>/<game>` on
# Linux) is shared by the game's saves and by what Unity and its packages keep there for themselves: the player's logs, the Addressables catalogs it
# downloads (one json and one hash per content update - dozens, up to megabytes each), Analytics (config, values, archived events, shader-variant
# reports), the asset cache, crash reports, and the I2 Localization cache. A game that keeps its saves elsewhere (cloud, the registry) has NOTHING else in
# the folder, so the tracker read the engine's writes as the game saving and proposed the folder; a game that saves there had the same files zipped in.
# Recognised by place - right under that `<company>/<game>` folder - never by name: a game's own "Unity" or "Analytics" folder anywhere else, or one
# deeper in its saves, is left alone.
_UNITY_RUNTIME_DIRS = frozenset({"unity", "unitycache", "crashes", "analytics"})
_UNITY_RUNTIME_FILES = frozenset({"player.log", "player-prev.log", "i2.loc"})


def _unity_data_rest(parts: list):
    """The components after a Unity data folder (<LocalLow>/<company>/<game>, or ~/.config/unity3d/<company>/<game>), or None when *parts* is not
    inside one. *parts* are lower-case."""
    for i, p in enumerate(parts):
        windows = p == "locallow" and i >= 1 and parts[i - 1] == "appdata"
        linux = p == "unity3d" and i >= 1 and parts[i - 1] == ".config"
        if (windows or linux) and len(parts) >= i + 3:
            return parts[i + 3:]
    return None


def is_unity_runtime_data(path) -> bool:
    """True when *path* is, or is inside, something Unity or one of its packages keeps in a game's data folder for itself (see above). The folder itself
    and anything else in it - the game's saves - is not. Never touches the disk."""
    try:
        parts = [p for p in str(path).replace("\\", "/").lower().split("/") if p]
    except (ValueError, TypeError):
        return False
    rest = _unity_data_rest(parts)
    if not rest:
        return False
    first = rest[0]
    if first in _UNITY_RUNTIME_DIRS or first.startswith("com.unity."):
        return True
    return len(rest) == 1 and first in _UNITY_RUNTIME_FILES


def unity_runtime_between(root, rel_parts) -> bool:
    """Is the file at *rel_parts* (relative to *root*, its own name last) Unity's own, by the place it sits in? For the walks that look at a folder from the
    inside, as renpy_sync_between does: the same files are skipped when the folder was picked as the save folder."""
    try:
        from pathlib import Path
        return is_unity_runtime_data(Path(str(root)).joinpath(*rel_parts))
    except (OSError, ValueError, TypeError):
        return False


def engine_data_between(root, rel_parts) -> bool:
    """A file inside *root* that belongs to an engine, not to the game's saves: Ren'Py's `sync` mirror or Unity's own files."""
    return renpy_sync_between(root, rel_parts) or unity_runtime_between(root, rel_parts)


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
