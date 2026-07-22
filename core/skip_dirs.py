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
