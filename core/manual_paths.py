"""
SaveSync - Manual save-path entry.

Backs the "add a save folder by hand" flow: the user has saves somewhere and
wants them backed up without SaveSync having found (or ever needing) the game's
executable. Nothing here is linked to an exe.

A whole COLLECTION of save folders (scan_save_collection) is read differently
from a single typed path, and the difference matters: a folder the user hands
over already holds the saves, so it IS the backup source and nothing is looked
up. Only the structure inside it is read, and only to record where those saves
belong — the chain a restore needs.

A SINGLE typed path is the one case with something to work out, since the user
may type a relative one — three kinds, in the order they are tested:

  ACTUAL      an absolute path, or one that starts under a user-profile root
              ("utente\\...", "AppData\\Roaming\\...", "LocalLow\\..."). These
              name a real location, so they are taken literally; the
              user-profile roots are expanded to this machine's own user.

  RESOLVED    a bare relative chain ("gioco\\game\\save") that WAS found under
              a known game location — an existing library entry's install
              folder, or one of the launcher/game directories the exe resolver
              already knows. It becomes an actual path.

  PREDICTED   the same relative chain when nothing matched. It is kept as the
              user's intent ("this lives next to the game") but has no location
              yet, so it cannot be backed up until it resolves.

The game name never comes from an executable here — there isn't one. It comes
from the folder, through the same generic-stem walk-up that names a game whose
exe is called "game.exe" (derive_display_name).
"""
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ACTUAL = "actual"
RESOLVED = "resolved"
PREDICTED = "predicted"

# First-segment tokens meaning "somewhere under this machine's user profile".
# Both languages, because the user types what their own Explorer shows them.
# The two forms are NOT interchangeable and the difference matters:
#   PROFILE   already inside a profile — "utente/games/x" keeps "games"
#   CONTAINER the folder that HOLDS the accounts, so the very next segment is
#             an account name ("Users/someone/games/x") and is replaced by
#             this machine's own profile rather than reproduced under it
_USER_PROFILE_TOKENS = {"utente", "user", "userprofile", "home"}
_USER_CONTAINER_TOKENS = {"users", "utenti"}
_USER_ROOT_TOKENS = _USER_PROFILE_TOKENS | _USER_CONTAINER_TOKENS
# Segments that are themselves profile-relative locations: typing
# "AppData/Roaming/MyGame" or "LocalLow/Studio/Game" means the real one.
_PROFILE_SUBROOTS = {
    "appdata", "roaming", "locallow", "localappdata", "documents", "documenti",
    "saved games", "savedgames", "my games", "mygames", "my documents",
    "documents and settings", "library", "application support",
}


@dataclass
class ManualPath:
    """One manually-entered save location."""
    raw: str                      # exactly what the user typed
    kind: str                     # ACTUAL | RESOLVED | PREDICTED
    path: str                     # resolved location ("" when PREDICTED)
    name: str                     # derived game name
    exists: bool = False

    @property
    def backupable(self) -> bool:
        """Only a real, existing folder can actually be backed up."""
        return bool(self.path) and self.exists


def _profile_root() -> Path:
    return Path(os.path.expanduser("~"))


def _expand_user_root(parts: list[str]) -> Optional[Path]:
    """Turn a profile-relative chain into this machine's real path.

    "utente/games/x"       → C:/Users/<me>/games/x
    "Users/someone/games/x"→ C:/Users/<me>/games/x   (account slot replaced)
    "AppData/Roaming/x"    → C:/Users/<me>/AppData/Roaming/x
    """
    if not parts:
        return None
    head = parts[0].strip().lower()
    if head in _USER_ROOT_TOKENS:
        rest = parts[1:]
        # In the container spelling the next segment IS the account slot, by
        # the shape of the path — unless it is itself a profile location,
        # which means the chain was written without an account at all.
        if head in _USER_CONTAINER_TOKENS and rest and not _is_profile_subroot(rest[0]):
            rest = rest[1:]
        return _profile_root().joinpath(*rest) if rest else _profile_root()
    if _is_profile_subroot(head):
        # AppData/... is the natural spelling of AppData\Roaming\...
        if head in ("roaming", "locallow") and "appdata" not in (p.lower() for p in parts):
            return _profile_root() / "AppData" / parts[0] / Path(*parts[1:]) \
                if len(parts) > 1 else _profile_root() / "AppData" / parts[0]
        return _profile_root().joinpath(*parts)
    return None


def profile_destination(chain: str):
    """Where a user-folder chain points ON THIS MACHINE, or None.

    None means the chain is relative to the game instead, and cannot be turned
    into a path without knowing where that game is.
    """
    parts = _split(chain)
    if not parts:
        return None
    try:
        return _expand_user_root(parts)
    except (OSError, ValueError):
        return None


def _is_profile_subroot(segment: str) -> bool:
    return segment.strip().lower() in _PROFILE_SUBROOTS


def _safe_iterdir(path: Path):
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _split(text: str) -> list[str]:
    """Path segments, tolerating either separator and stray quotes/spaces."""
    cleaned = text.strip().strip('"').strip()
    cleaned = cleaned.replace("\\", "/")
    return [seg for seg in cleaned.split("/") if seg not in ("", ".")]


def _dedup_existing(paths) -> list[Path]:
    """Existing paths, de-duplicated case-insensitively, order preserved."""
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path).casefold()
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.exists():
                out.append(path)
        except OSError:
            continue
    return out


def _suggested_roots() -> list[Path]:
    try:
        from core.resolvers import _get_suggested_exe_search_paths
        return [Path(p) for p in _get_suggested_exe_search_paths()]
    except Exception:
        logger.debug("Suggested search paths unavailable", exc_info=True)
        return []


def _library_install_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        from core.library import get_library
        for game in get_library().all_games():
            if not game.exe_path:
                continue
            try:
                dirs.append(Path(game.exe_path).parent)
            except Exception:
                continue
    except Exception:
        logger.debug("Library unavailable for manual-path resolution", exc_info=True)
    return dirs


def game_search_roots(limit: int = 400) -> list[Path]:
    """Where a relative chain might live: the launcher and common game
    directories, then every library entry's install folder and its parent.

    The general directories come FIRST so a large library cannot push them
    past the bound — with a few hundred games the previous order dropped
    exactly the folders most likely to hold the game being looked for.
    """
    install_dirs = _library_install_dirs()
    ordered = _suggested_roots() + install_dirs + [d.parent for d in install_dirs]
    return _dedup_existing(ordered)[:limit]


def resolve_manual_path(text: str, extra_roots: Optional[list] = None) -> ManualPath:
    """Read one manually-entered path. Never raises: bad input comes back
    PREDICTED with an empty location, which the UI shows as unresolved.

    This is the SINGLE add: the user types something and expects it to be
    found. A collection folder is not read through here — there the folder
    itself is the answer and nothing is looked up.
    """
    raw = (text or "").strip()
    if not raw:
        return ManualPath(raw="", kind=PREDICTED, path="", name="", exists=False)

    expanded = os.path.expandvars(os.path.expanduser(raw.strip('"')))
    parts = _split(expanded)

    # 1. Already a real location.
    try:
        candidate = Path(expanded)
        if candidate.is_absolute():
            return _make(raw, ACTUAL, candidate)
    except (OSError, ValueError):
        pass

    # 2. Under this machine's user profile.
    try:
        user_path = _expand_user_root(parts)
    except (OSError, ValueError):
        user_path = None
    if user_path is not None:
        return _make(raw, ACTUAL, user_path)

    # 3. A relative chain — look for it under the places games live.
    relative = Path(*parts) if parts else None
    if relative is not None:
        roots = [Path(r) for r in (extra_roots or [])] + game_search_roots()
        for root in roots:
            try:
                candidate = root / relative
                if candidate.exists():
                    return _make(raw, RESOLVED, candidate)
            except (OSError, ValueError):
                continue

    # 4. Nothing to anchor it to (yet).
    return ManualPath(raw=raw, kind=PREDICTED, path="",
                      name=derive_folder_name(raw), exists=False)


def _make(raw: str, kind: str, path: Path) -> ManualPath:
    try:
        resolved = str(path)
        exists = path.exists()
    except OSError:
        resolved, exists = str(path), False
    return ManualPath(raw=raw, kind=kind, path=resolved,
                      name=derive_folder_name(resolved), exists=exists)


def derive_folder_name(path_str: str) -> str:
    """Game name for a save folder.

    There is no executable to name this after, so the folder does the job —
    through the very same walk-up that renames a generic "game.exe": a folder
    literally called "save"/"saves"/"data" says nothing, so the nearest
    meaningful ancestor is used instead.
    """
    from core.save_detector import derive_display_name
    parts = _split(path_str)
    if not parts:
        return ""
    # derive_display_name expects a FILE path and looks at its stem, so hand it
    # a virtual file inside the folder: the folder chain is then walked exactly
    # as it would be for a real executable.
    probe = Path(*parts) / "game.exe"
    name = derive_display_name(str(probe), fallback=parts[-1])
    name = _strip_trailing_build_tag(name)
    name = re.sub(r"\s{2,}", " ", (name or "")).strip(" ._-")
    return name or parts[-1]


def _strip_trailing_build_tag(name: str) -> str:
    """Drop a trailing build tag — the "12b" in "Some Title 12b".

    Deliberately NOT done in the shared strip_version_tokens: that one feeds
    backup-folder identity and remote matching, where changing what a name
    reduces to would re-point existing folders. Here it only affects the title
    proposed for a hand-added save folder, which stays editable anyway.

    The letter case is the discriminator: build tags are written lowercase
    ("12b", "2b") while "Game 3D" or "Game 2P" are part of the title.
    """
    if not name:
        return name
    stripped = re.sub(r"[\s._\-]+\d+[a-z]\s*$", "", name)
    # Never reduce a name to nothing, and never eat a name that is only a tag.
    return stripped if stripped.strip(" ._-") else name


def resolve_many(texts: list, extra_roots: Optional[list] = None) -> list:
    """resolve_manual_path over a list, dropping blanks and duplicates while
    keeping the order the user gave."""
    out: list = []
    seen: set = set()
    for text in texts or []:
        item = resolve_manual_path(text, extra_roots=extra_roots)
        if not item.raw:
            continue
        key = (item.path or item.raw).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# Files that are folder debris rather than saves, so they never stop the
# descent through a collection folder.
_DEBRIS_SUFFIXES = {
    ".txt", ".md", ".nfo", ".log", ".url", ".lnk", ".desktop", ".html", ".htm",
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
    ".db",          # Thumbs.db / desktop.ini company
    ".ini_bak",
}
# Folders that sit BESIDE the save chain without being part of it.
_COMPANION_DIRS = {
    "docs", "doc", "documentation", "manual", "manuals", "screenshots",
    "screenshot", "images", "covers", "extras", "readme", "info",
    "__macosx", ".git",
}


@dataclass
class CollectedSave:
    """One game inside a save-collection folder."""
    source: str           # the folder in the collection ( …/Save/<Game> )
    name: str             # game name, derived from that folder
    chain: str            # the destination chain found inside it ( www/save )
    item: ManualPath      # what that chain resolves to


def save_chain_of(folder: str, max_depth: int = 8) -> str:
    """The destination chain a collection folder describes, relative to itself.

    A save-collection folder does not hold the saves at its own level: it
    mirrors WHERE they belong. "<Game>/www/save" says the saves live in
    "www/save" under the game; "<Game>/AppData/Roaming/<Studio>/<Title>"
    says they live under the user profile.

    So descend while the level is a pass-through and stop at the level that
    actually holds saves. "Pass-through" tolerates the debris that really sits
    in these folders — a readme, a cover image, a shortcut: only SAVE-like
    files stop the descent, and companion folders (docs, screenshots…) don't
    count as a fork. Requiring a literally empty level would send
    "<Game>/{www, readme.txt}" back to registering the collection folder,
    which is the mistake this exists to avoid.

    An empty chain means the folder holds the saves directly — the ordinary
    case, handled as an actual path by the caller.
    """
    try:
        base = Path(folder)
        if not base.is_dir():
            return ""
    except OSError:
        return ""

    parts: list[str] = []
    current = base
    for _ in range(max_depth):
        try:
            children = list(current.iterdir())
        except OSError:
            break
        if any(_is_file(c) and _looks_like_save_file(c) for c in children):
            break                          # this level IS the save folder
        dirs = [c for c in children if _is_dir(c) and not _is_companion_dir(c.name)]
        if len(dirs) != 1:
            break                          # nothing to follow, or a real fork
        current = dirs[0]
        parts.append(current.name)
    return "/".join(parts)


def _looks_like_save_file(path: Path) -> bool:
    """A file that would plausibly BE a save, as opposed to folder debris."""
    suffix = path.suffix.lower()
    if suffix in _DEBRIS_SUFFIXES:
        return False
    try:
        from core.save_detector import _SAVE_EXTENSIONS
        if suffix in _SAVE_EXTENSIONS:
            return True
    except Exception:
        pass
    # Unknown extension (or none): saves often have bespoke ones, so anything
    # not obviously debris counts.
    return True


def _is_companion_dir(name: str) -> bool:
    return name.strip().lower() in _COMPANION_DIRS


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def scan_save_collection(root: str, progress=None, cancel=None) -> list:
    """Read a save-collection folder: one CollectedSave per game inside it.

    This is the single add repeated — the only difference is how the folders
    are picked. Each subfolder becomes a save path exactly as if it had been
    chosen by hand: it IS the backup source, everything under it goes into the
    archive, and nothing is worked out from the library or needs an
    executable.

    The structure inside is read for one reason only: to record the chain
    ("www/save", "AppData/Roaming/…") that says where those saves belong, so a
    restore can put them back. It never moves the save path deeper — the whole
    chain travels under the folder the user selected.

    *root* itself can be named anything — it is only a container and its name
    is never read. What matters is one level down: each subfolder names a game.
    """
    out: list = []
    folders = child_folders(root)
    total = len(folders)
    for index, folder in enumerate(folders, 1):
        if cancel and cancel():
            logger.info(f"Save-collection scan cancelled after {index - 1}/{total}")
            break
        raw_name = Path(folder).name
        name = derive_folder_name(folder)
        if progress:
            progress(index, total, raw_name)
        chain = save_chain_of(folder)
        item = _make(folder, ACTUAL, Path(folder))
        # The name always comes from the collection folder, never from the
        # chain — "save" and "Roaming" are not game titles.
        item.name = name or item.name
        out.append(CollectedSave(source=folder, name=item.name, chain=chain, item=item))
    logger.info(f"Save collection {root}: {len(out)} folder(s) read")
    return out


def names_of(game) -> set:
    """Every name a game answers to, casefolded.

    Its title, its install folder, and every title it has been known by. The
    history matters here as much as the current name: a game renamed after
    its saves were registered by hand would otherwise stop recognising them,
    even though SaveSync wrote the old name down itself.
    """
    names = set()
    title = (getattr(game, "name", "") or "").strip()
    if title:
        names.add(title.casefold())
    for past in (getattr(game, "name_history", None) or []):
        past = (past or "").strip()
        if past:
            names.add(past.casefold())
    # The undecorated originals: a release folder and a folder of saves kept
    # under the same release name agree exactly, where the cleaned-up titles
    # may have lost different pieces.
    for hint in (getattr(game, "name_hints", None) or []):
        hint = (hint or "").strip()
        if hint:
            names.add(hint.casefold())
    exe = getattr(game, "exe_path", "") or ""
    if exe:
        try:
            names.add(Path(exe).parent.name.strip().casefold())
        except Exception:
            pass
    return {n for n in names if n}


_names_of = names_of        # kept: the old private spelling is used elsewhere


def _waiting_for(game, entry) -> bool:
    """True when *entry* is a hand-registered placeholder waiting for *game*.

    The match is the name coincidence and nothing else: a folder a user names
    after a game is, in this layout, that game's own folder — so when a game
    of that name turns up, that IS the game. The same signal the rest of the
    app uses to recognise a game and offer its cloud saves.
    """
    if entry.id == game.id or entry.exe_path:
        return False
    anchor = (getattr(entry, "pending_save_anchor", "") or "").strip().casefold()
    title = (entry.name or "").strip().casefold()
    candidates = {n for n in (anchor, title) if n}
    if not candidates:
        return False
    return bool(candidates & _names_of(game))


def adopt_manual_entries_for(game) -> int:
    """Hand a newly appeared game the save destinations registered for it.

    Event-driven by design — there is NO periodic re-check anywhere. A
    destination is anchored when it is registered (the game was already
    known), and otherwise the question is asked again exactly once: here, the
    moment a game whose name matches turns up. Anchoring is then a single
    path test under THAT game's folder, not a scan.

    A pending chain like "www/save" is resolved against the game's install
    folder; an already-absolute destination (a profile path) simply moves
    across. Either way the paths end up on the entry that HAS the executable,
    which is what makes the game recognisable at launch — and with it the
    cloud-saves prompt.

    Returns how many placeholder entries were absorbed.
    """
    try:
        from core.library import get_library
    except Exception:
        return 0
    lib = get_library()
    try:
        waiting = [g for g in lib.all_games() if _waiting_for(game, g)]
    except Exception:
        return 0
    if not waiting:
        return 0

    target = lib.get_by_id(game.id)
    if target is None:
        return 0
    try:
        install = Path(target.exe_path).parent if target.exe_path else None
    except Exception:
        install = None

    absorbed = 0
    for placeholder in waiting:
        gained: list = []
        chain = getattr(placeholder, "pending_save_chain", "")
        if chain and install is not None:
            candidate = install / Path(*_split(chain))
            try:
                if candidate.exists():
                    gained.append(str(candidate))
            except OSError:
                pass
        gained.extend(placeholder.save_paths or [])

        if not gained:
            # Nothing to hand over — the chain needs an install folder and
            # this game has no executable yet. Absorbing here would delete the
            # placeholder and with it the destination, which is the whole
            # thing being kept. Leave it waiting for a moment that can
            # actually place it: the launch, where the executable is a
            # certainty.
            logger.debug(f"{placeholder.name!r} stays pending: {target.name!r} "
                         "has nothing to anchor its destination to yet")
            continue

        for path in gained:
            if path not in (target.save_paths or []):
                target.save_paths = list(target.save_paths or []) + [path]
            # The destination expressed relative to the game travels with the
            # path it belongs to — one per path, since the placeholder may
            # have held several folders pointing at different places.
            moved = ""
            try:
                moved = placeholder.chain_for_path(path)
            except AttributeError:
                moved = getattr(placeholder, "save_chain", "") or ""
            if moved:
                try:
                    target.record_path_chain(path, moved)
                except AttributeError:
                    if not getattr(target, "save_chain", ""):
                        target.save_chain = moved
        if gained:
            target.save_paths_confirmed = True
        chain_known = chain or getattr(placeholder, "save_chain", "")
        if chain_known and not getattr(target, "save_chain", ""):
            target.save_chain = chain_known

        # Order matters: the placeholder goes first so it stops occupying the
        # game's natural backup/sync folder name, then the game reclaims it,
        # and only then do the archives move — straight into their final home.
        lib.remove_game(placeholder.id)
        _reclaim_folder_name(placeholder, target)
        # Whatever was backed up while this was only a placeholder becomes
        # the game's own history: the point of registering saves before the
        # game exists is that they stop being a loose pile the moment the
        # game does.
        _adopt_backups(placeholder, target)
        logger.info(f"Manual entry {placeholder.name!r} absorbed into {target.name!r}")
        absorbed += 1

    if absorbed:
        lib.update_game(target)
    return absorbed


def _reclaim_folder_name(placeholder, target) -> bool:
    """Give the game back the backup/sync folder name the placeholder held.

    add_game keeps every game's folder isolated, so a game added while its
    placeholder still existed was handed a suffixed variant ("Title_2"). Left
    alone it would keep that forever — and sync into a different cloud folder
    than the very same game on another machine, which is the one consequence
    that outlives this app's session.

    Only done when the variant is exactly the placeholder's name plus a
    numeric suffix, and only while the game has no backups of its own to
    strand in the old folder.
    """
    ph_folder = (getattr(placeholder, "computed_folder_name", "") or "").strip()
    tg_folder = (getattr(target, "computed_folder_name", "") or "").strip()
    if not ph_folder or not tg_folder or ph_folder == tg_folder:
        return False
    if not re.fullmatch(re.escape(ph_folder) + r"_\d+", tg_folder):
        return False
    try:
        from core.backup import get_backup_manager
        if get_backup_manager().get_backups_for_game(target.id):
            return False
    except Exception:
        return False
    if tg_folder not in (target.folder_history or []):
        target.folder_history = list(target.folder_history or []) + [tg_folder]
    target.computed_folder_name = ph_folder
    logger.info(f"{target.name!r} reclaimed the folder name {ph_folder!r} "
                f"from its placeholder (was {tg_folder!r})")
    return True


def _adopt_backups(placeholder, target) -> int:
    """Move the placeholder's backups onto the real game, if it made any."""
    try:
        from core.backup import get_backup_manager
        return get_backup_manager().adopt_backups(
            placeholder.id, target.id, target.name,
            to_exe_path=target.exe_path or "",
            to_folder_name=getattr(target, "computed_folder_name", "") or "",
        )
    except Exception:
        logger.warning(f"Could not re-file backups of {placeholder.name!r}", exc_info=True)
        return 0


def child_folders(root: str, limit: int = 0) -> list:
    """Immediate subfolders of *root* — the pick-list for a multiple add.

    Unlimited by default. A collection is exactly as big as the user's
    archive, and an arbitrary cap silently drops the tail: the folders past
    it never appear, so nothing tells the user they were left out. The walk
    is already bounded per folder, runs on a worker thread and is
    cancellable, so size costs time — not correctness.

    A positive *limit* truncates; the caller is expected to say so.
    """
    try:
        base = Path(root)
        if not base.is_dir():
            return []
        kids = sorted((c for c in base.iterdir() if c.is_dir()),
                      key=lambda c: c.name.lower())
        if limit and len(kids) > limit:
            logger.info(f"Manual add: {root} has {len(kids)} subfolders, showing {limit}")
            kids = kids[:limit]
        return [str(c) for c in kids]
    except OSError as e:
        logger.debug(f"Cannot list {root}: {e}")
        return []
