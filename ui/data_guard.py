"""SaveSync - keeps its own working files in place while it runs.

Tools that clean a PC (caches, temp files, "junk") can remove files SaveSync made - or unpacked -
for its own use while it is running. It reads most of them once and trusts they stay, so what
used to be there is simply gone and nothing says so: folders that a write expects, icons a style
sheet points at, and, for the packaged program, the files it unpacked when it started
(translations, images, the Tor program).

Nothing here runs by itself on a timer. A look is asked for (``request``) at the moments somebody is
about to use SaveSync: its window coming back from the tray or from minimised, an Add/Edit panel
opening, and - not more than every ten minutes - the pointer going onto a game card. It is made on a
worker thread (``look``): a few dozen files and, for the packaged program, a few hundred stats.

- ``ensure_data_dirs`` / ``ensure_ui_icons``: put back what can be put back at once;
- ``bundle_missing``: the unpacked program files are not something to rebuild from inside the
  running program - which of the ones seen at start are gone, so that the person is told to
  restart while it is still one thing that is wrong, rather than finding a feature broken at a time;
- ``clean_stale_runtime``: the unpack folder of earlier runs that ended badly (a crash, a kill)
  is removed, for the packaged program that unpacks under SaveSync's own data folder.
"""
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Folders of the data directory that something writes into without making them first.
DATA_DIRS = ("icons", "logs", "backups", "config_history")
# Files the packaged program unpacks that it needs later, not only at start: looked for at start
# (the ones that are there), and again now and then.
BUNDLE_SENTINELS = ("i18n/locales/en.json", "assets/icon.png", "tor")
# How soon after the last look another may be made: for what a person is about to use (the window
# back, an Add/Edit panel), and for the pointer merely going over a game card.
MIN_GAP_ACTION_SECONDS = 10.0
MIN_GAP_HOVER_SECONDS = 10 * 60.0
# An unpack folder this young may belong to a second copy that is just starting.
STALE_AFTER_SECONDS = 10 * 60
# How many unpacked files are noted at start (a build has a few hundred libraries).
MAX_NOTED_FILES = 3000

_seen_at_start: Optional[list] = None
_requester = None               # what answers a request (the main window's), None until there is one
_last_look = 0.0                # time.monotonic() of the last look asked for


def set_requester(fn) -> None:
    """Register what starts a look (called with the reason). The start-up counts as a look: the
    files were all just made."""
    global _requester, _last_look
    _requester, _last_look = fn, time.monotonic()


def request(reason: str = "action", hover: bool = False) -> bool:
    """Ask for a look now, unless one was asked for too lately (see the gaps above) or nothing is
    there to make it (a test, a second window). Call on the GUI thread. True if one was started."""
    global _last_look
    if _requester is None:
        return False
    now = time.monotonic()
    if now - _last_look < (MIN_GAP_HOVER_SECONDS if hover else MIN_GAP_ACTION_SECONDS):
        return False
    _last_look = now
    try:
        _requester(reason)
    except Exception:
        logger.debug("Could not start a look at SaveSync's working files", exc_info=True)
        return False
    return True


def look() -> list:
    """The look: put back the folders and icons that are gone, and return the unpacked program
    files that are (see bundle_missing). No GUI in it: meant for a worker thread."""
    ensure_data_dirs()
    ensure_ui_icons()
    return bundle_missing()


def ensure_data_dirs() -> list:
    """Make the folders in DATA_DIRS that are not there. The names of the ones made."""
    from core.constants import USER_DATA_DIR
    made = []
    for name in DATA_DIRS:
        folder = Path(USER_DATA_DIR) / name
        try:
            if not folder.is_dir():
                folder.mkdir(parents=True, exist_ok=True)
                made.append(name)
        except OSError:
            logger.debug(f"Could not make {folder}", exc_info=True)
    if made:
        logger.warning(f"SaveSync's data folder had lost: {', '.join(made)} - made again")
    return made


def ensure_ui_icons() -> bool:
    """The chevrons the style sheet points at, for the theme in use: written again when gone.
    True if any was missing."""
    try:
        from core.constants import USER_DATA_DIR
        from ui.styles.arrow_icons import ensure_arrow_icons
        from ui.styles.theme import get_theme_manager
        theme = get_theme_manager().current
        folder = Path(USER_DATA_DIR) / "ui_icons" / theme
        missing = any(not (folder / f"{n}{s}.svg").is_file()
                      for n in ("up", "down", "left", "right") for s in ("", "_muted"))
        if missing:
            ensure_arrow_icons(theme)
            ensure_arrow_icons(theme, suffix="_muted")
            logger.warning("SaveSync's chevron icons had been removed - written again")
        return missing
    except Exception:
        logger.debug("Could not check the chevron icons", exc_info=True)
        return False


# ── the packaged program's own files ─────────────────────────────────────────

def bundle_dir() -> Optional[Path]:
    """Where the packaged program unpacked itself, or None when it is run from source."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def remember_bundle() -> list:
    """Note which of BUNDLE_SENTINELS are there now (call once, at start). Returns them."""
    global _seen_at_start
    if _seen_at_start is not None:
        return list(_seen_at_start)         # (noted already: what is gone since must not be forgotten)
    base = bundle_dir()
    if base is None:
        _seen_at_start = []
        return []
    seen = [s for s in BUNDLE_SENTINELS if (base / s).exists()]
    # Every program library that was unpacked: the ones loaded at start are held open (and cannot
    # be removed), but those loaded the first time a feature is used - the transfer library, an
    # image format, a TLS backend - are only files until then, and are what a sweep takes.
    try:
        for pattern in ("*.pyd", "*.dll"):
            for f in base.rglob(pattern):
                if len(seen) >= MAX_NOTED_FILES:
                    break
                seen.append(f.relative_to(base).as_posix())
    except OSError:
        logger.debug("Could not list the unpacked libraries", exc_info=True)
    _seen_at_start = seen
    return list(_seen_at_start)


def bundle_missing() -> list:
    """The unpacked files noted at start that are not there any more. Empty when run from source
    (nothing was unpacked) or when nothing was noted."""
    base = bundle_dir()
    if base is None or not _seen_at_start:
        return []
    try:
        return [s for s in _seen_at_start if not (base / s).exists()]
    except OSError:
        return []


def clean_stale_runtime(current: Optional[Path] = None) -> int:
    """Remove the unpack folders (``_MEI…``) that earlier runs left in SaveSync's own runtime
    folder. Only there: that folder is the program's (see the spec's ``runtime_tmpdir``), so it
    is never anyone else's temp files that go; and only after the single-instance lock is held,
    when no other copy can be using one. *current* (the folder in use; this run's own unless
    given) is never touched, nor is a folder young enough to be a second copy starting up.
    Returns how many were removed."""
    base = bundle_dir() if current is None else current
    if base is None:
        return 0
    runtime = base.parent
    if runtime.name.lower() != "runtime" or runtime.parent.name.lower() != "savesync":
        return 0
    removed = 0
    now = time.time()
    try:
        children = list(runtime.iterdir())
    except OSError:
        return 0
    for child in children:
        if not child.name.startswith("_MEI") or not child.is_dir():
            continue
        try:
            if child.resolve() == base.resolve():
                continue
            if now - child.stat().st_mtime < STALE_AFTER_SECONDS:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 0 if child.exists() else 1
        except OSError:
            continue
    if removed:
        logger.info(f"Removed {removed} unpack folder(s) left by earlier runs")
    return removed
