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
              ("utente\\...", "AppData\\Roaming\\...", "LocalLow\\..."), or that
              CONTAINS such a marker after a leading game-folder label
              ("MyGame\\AppData\\Roaming\\..."). These name a real location, so
              they are taken literally; the user-profile roots are expanded to
              this machine's own user.

  RESOLVED    a bare relative chain ("gioco\\game\\save") that WAS found under
              a known game location — an existing library entry's install
              folder, or one of the launcher/game directories the exe resolver
              already knows. It becomes an actual path.

  PREDICTED   the same relative chain when nothing matched. It is kept as the
              user's intent ("this lives next to the game") but has no location
              yet, so it cannot be backed up until it resolves.

When the single path is a *live* game save folder (not a collection copy),
``live_save_chain`` walks UP like generic exe stems: a user-system folder
(Roaming, …) yields a profile chain that keeps engine hosts (RenPy under
Roaming); otherwise the chain is install-relative (www/save). Collection
adds still use ``save_chain_of`` (walk DOWN inside the copy). Orphan indexes
record the destination / zip-root label via ``orphan_index_save_path``, never
the collection parent path.

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


def _canonical_profile_parts(parts: list[str]) -> list[str]:
    """Normalize well-known profile segment spellings for this OS tree.

    Typed input is often all-lowercase ("appdata/roaming/…"); joining that
    under the home folder still works on Windows, but restore/display look
    wrong next to the real ``AppData\\Roaming`` tree. Only rewrite known
    markers — game/studio folder names stay as typed.
    """
    canon = {
        "appdata": "AppData",
        "roaming": "Roaming",
        "local": "Local",
        "locallow": "LocalLow",
        "localappdata": "AppData",  # expanded specially below when alone
        "documents": "Documents",
        "documenti": "Documents",
        "saved games": "Saved Games",
        "savedgames": "Saved Games",
        "my games": "My Games",
        "mygames": "My Games",
        "my documents": "Documents",
        "library": "Library",
        "application support": "Application Support",
    }
    out: list[str] = []
    for seg in parts:
        key = seg.strip().lower()
        out.append(canon.get(key, seg))
    return out


def _profile_anchor_index(parts: list[str]) -> int:
    """Index of the first profile/system marker in *parts*, or -1.

    A hand-typed or mirrored chain often keeps the game folder in front
    ("MyGame/AppData/Roaming/…"). The marker — not the leading title — is
    what names a real location under this machine's user profile.
    """
    for i, seg in enumerate(parts):
        key = seg.strip().lower()
        if key in _USER_ROOT_TOKENS or _is_profile_subroot(key):
            return i
    return -1


def _expand_user_root(parts: list[str]) -> Optional[Path]:
    """Turn a profile-relative chain into this machine's real path.

    "utente/games/x"              → C:/Users/<me>/games/x
    "Users/someone/games/x"       → C:/Users/<me>/games/x   (account slot replaced)
    "AppData/Roaming/x"           → C:/Users/<me>/AppData/Roaming/x
    "MyGame/AppData/Roaming/x"    → C:/Users/<me>/AppData/Roaming/x
         (leading game folder is only a label; AppData marks the real root)
    """
    if not parts:
        return None

    # Drop a leading game/release folder when a system marker appears later.
    # Without this, "Title\\AppData\\Roaming\\…" stays PREDICTED even though
    # the trailing chain is an unambiguous profile path.
    anchor = _profile_anchor_index(parts)
    if anchor > 0:
        parts = parts[anchor:]

    head = parts[0].strip().lower()
    if head in _USER_ROOT_TOKENS:
        rest = parts[1:]
        # In the container spelling the next segment IS the account slot, by
        # the shape of the path — unless it is itself a profile location,
        # which means the chain was written without an account at all.
        if head in _USER_CONTAINER_TOKENS and rest and not _is_profile_subroot(rest[0]):
            rest = rest[1:]
        rest = _canonical_profile_parts(rest)
        return _profile_root().joinpath(*rest) if rest else _profile_root()
    if _is_profile_subroot(head):
        # AppData/... is the natural spelling of AppData\Roaming\...
        # LocalLow / Roaming alone imply they sit under AppData.
        if head in ("roaming", "locallow") and "appdata" not in (p.lower() for p in parts):
            tail = _canonical_profile_parts(parts)
            return (_profile_root() / "AppData").joinpath(*tail)
        if head == "localappdata":
            # "LocalAppData/Studio/Game" → ~/AppData/Local/Studio/Game
            rest = _canonical_profile_parts(parts[1:])
            base = _profile_root() / "AppData" / "Local"
            return base.joinpath(*rest) if rest else base
        return _profile_root().joinpath(*_canonical_profile_parts(parts))
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
        # Ren'Py / engine homes that sit directly under the user profile
        # (Linux ~/.renpy, macOS ~/Library/RenPy) — not AppData-shaped, but
        # still a direct per-user destination.
        head = parts[0].strip().lower()
        if head in {".renpy", "renpy"}:
            return _profile_root().joinpath(*parts)
        if head == "library" and len(parts) >= 2 and parts[1].strip().lower() == "renpy":
            return _profile_root().joinpath(*_canonical_profile_parts(parts))
        return _expand_user_root(parts)
    except (OSError, ValueError):
        return None


# Segments that are pass-through under a game install (relative chain), same
# spirit as generic exe stems — never the game title itself.
_RELATIVE_PASS_SEGMENTS = frozenset({
    "save", "saves", "savedata", "savedata2", "savegame", "savegames",
    "saved games", "savedgames", "game", "www", "data", "userdata",
    "user", "users", "slot", "slots", "profile", "profiles",
    "config", "cfg", "system", "persistent",
}) | {s.lower() for s in (
    # Keep in sync with save_detector.GENERIC_EXE_STEMS folder-ish names
    "bin", "lib", "lib64", "common", "build", "dist", "desktop",
)}

# Engine host folders under a user profile that MUST stay in a profile chain
# (Ren'Py: Roaming/RenPy/<Game> — the game folder is descriptive, but without
# RenPy the chain would not reach the system root correctly).
_ENGINE_PROFILE_HOSTS = frozenset({
    "renpy", ".renpy",
    "unity", "unity3d",
    "godot",
})

# Install-library containers: walking up into these means the relative chain
# has ended (everything below was game-relative).
_INSTALL_CONTAINERS = frozenset({
    "games", "giochi", "steamapps", "common", "downloads", "download",
    "program files", "program files (x86)", "programdata", "public",
})


def live_save_chain(path_str: str) -> str:
    """Destination chain for a *live* save folder (not a collection copy).

    Walks UP from *path_str* the same way generic stems walk for naming:

      • Hit a user-system folder (AppData / Roaming / Documents / …)
        → profile chain from that root down, **including** engine hosts
        like RenPy (``AppData/Roaming/RenPy/<Game>``).
      • Otherwise → install-relative chain (``www/save``, ``game/save``),
        stopping before the meaningful game-folder name.

    Empty string: nothing useful could be derived.
    """
    if not (path_str or "").strip():
        return ""
    try:
        path = Path(path_str).expanduser().resolve()
    except (OSError, ValueError):
        try:
            path = Path(path_str).expanduser()
        except (OSError, ValueError):
            return ""

    parts = list(path.parts)
    if not parts:
        return ""
    # Drop Windows drive / UNC root from the segment list used for chains.
    start = 0
    if path.drive or (parts and parts[0] in ("\\", "/", path.anchor)):
        start = 1
    segs = parts[start:]
    if not segs:
        return ""
    lower = [s.lower() for s in segs]

    # ── Profile / per-user system root ──────────────────────────────────
    profile_at = -1
    for i, n in enumerate(lower):
        if n in _USER_ROOT_TOKENS or n == "appdata":
            profile_at = i
            break
        if n in ("roaming", "local", "locallow"):
            profile_at = (i - 1) if i > 0 and lower[i - 1] == "appdata" else i
            break
        if n in (".renpy", "renpy"):
            # Keep AppData/Roaming or Library above RenPy in the chain.
            if i >= 2 and lower[i - 2] == "appdata" and lower[i - 1] == "roaming":
                profile_at = i - 2
            elif i >= 1 and lower[i - 1] == "appdata":
                profile_at = i - 1
            elif i >= 1 and lower[i - 1] == "library":
                profile_at = i - 1
            else:
                profile_at = i  # ~/.renpy/…
            break
        if n == "library" and i + 1 < len(lower) and lower[i + 1] in (
                "renpy", "application support"):
            profile_at = i
            break
        if n in _PROFILE_SUBROOTS:
            profile_at = i
            break

    if profile_at >= 0:
        body = segs[profile_at:]
        # Users/<someone>/AppData/… → drop container + account, keep AppData…
        if body and body[0].lower() in _USER_CONTAINER_TOKENS:
            body = body[1:]
            if body and not _is_profile_subroot(body[0]) and body[0].lower() not in (
                    "appdata", ".renpy", "renpy", "library"):
                body = body[1:]  # account name
        if body and body[0].lower() in _USER_PROFILE_TOKENS:
            body = body[1:]
        if not body:
            return ""
        # Canonicalize known profile spellings; keep RenPy / game names as-is.
        return "/".join(_canonical_profile_parts(body))

    # ── Install-relative: collect pass-through segments up to game title ─
    chain_rev: list[str] = []
    for s, sl in zip(reversed(segs), reversed(lower)):
        if sl in _INSTALL_CONTAINERS or sl in _CONTAINER_DIR_NAMES_SAFE:
            break
        if sl in _RELATIVE_PASS_SEGMENTS or sl in _ENGINE_PROFILE_HOSTS:
            # Engine hosts without a profile root above are still pass-through
            # only when we already have something below (rare); usually RenPy
            # is handled in the profile branch.
            if sl in _ENGINE_PROFILE_HOSTS and not chain_rev:
                break
            chain_rev.append(s)
            continue
        # Meaningful folder = game install name — not part of the chain.
        break
    chain_rev.reverse()
    return "/".join(chain_rev)


# Local copy of install/container names used when walking relative chains
# (avoid importing save_detector's private set at module load).
_CONTAINER_DIR_NAMES_SAFE = frozenset({
    "games", "giochi", "steamapps", "downloads", "download", "documents",
    "program files", "program files (x86)", "programdata", "users", "public",
    "appdata", "local", "roaming", "locallow", "windows", "system32",
})


def orphan_index_save_path(
    source: str,
    chain: str,
    game_name: str = "",
    *,
    from_collection: bool = False,
) -> str:
    """Path stored on an orphan index entry (destination / zip-root label).

    Never the collection parent (``D:\\VN Games\\Save\\…``): that is only the
    zip *source*. Profile chains resolve on this machine; relative collection
    chains keep the game folder *name* so restore can match zip tops.
    Live (non-collection) folders record themselves when they are the real
    destination.
    """
    chain = (chain or "").strip()
    if chain:
        dest = profile_destination(chain)
        if dest is not None:
            return str(dest)
        if from_collection:
            return Path(source).name if source else (game_name or "")
        return source or game_name or ""
    if from_collection:
        return Path(source).name if source else (game_name or "")
    return source or game_name or ""


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


def latest_save_mtime(path_str: str) -> float:
    """Newest write under a save folder (bounded scan). 0 if unreadable."""
    if not path_str:
        return 0.0
    try:
        from core.save_detector import _latest_write_ts
        return float(_latest_write_ts(path_str) or 0.0)
    except Exception:
        return 0.0


_PRODUCT_CODE_RE = re.compile(r"(?<![a-zA-Z0-9])((?:RJ|RE|VJ)\d{4,10})(?![a-zA-Z0-9])",
                              re.IGNORECASE)


def product_codes(text: str) -> set[str]:
    """DLsite-family product codes in *text* (RJ/RE/VJ + digits), uppercased.

    These — not bare version tokens like ``v1.2`` — are strong enough to say
    two similarly named folders are different games.
    """
    if not text:
        return set()
    return {m.group(1).upper() for m in _PRODUCT_CODE_RE.finditer(text)}


def _game_product_codes(game) -> set[str]:
    codes: set[str] = set()
    codes |= product_codes(getattr(game, "name", "") or "")
    for past in (getattr(game, "name_history", None) or []):
        codes |= product_codes(past or "")
    for hint in (getattr(game, "name_hints", None) or []):
        codes |= product_codes(hint or "")
    exe = getattr(game, "exe_path", "") or ""
    if exe:
        try:
            codes |= product_codes(Path(exe).parent.name)
        except Exception:
            pass
    return codes


def _clean_title_keys(game) -> set[str]:
    """Version-stripped titles this game answers to (casefolded)."""
    from core.constants import strip_version_tokens
    keys: set[str] = set()
    for raw in (
        [getattr(game, "name", "") or ""]
        + list(getattr(game, "name_history", None) or [])
    ):
        cleaned = strip_version_tokens((raw or "").strip())
        if cleaned:
            keys.add(cleaned.casefold())
    return keys


def find_manual_game_match(games: list, cleaned_name: str, raw_folder: str):
    """Which library entry a hand-added folder belongs to, if any.

    Version tokens alone do NOT split identity: ``My Game v1`` and
    ``My Game v2`` are the same game. Distinct product codes (RJ/RE/VJ…)
    or an exact raw-folder hit do — those are the hints strong enough to
    classify two similarly named releases as different.

      1. Exact full folder name in names_of / hints.
      2. Same cleaned title, rejecting candidates whose product codes
         conflict with the incoming folder's codes.
    """
    raw_key = (raw_folder or "").strip().casefold()
    if raw_key:
        for g in games:
            if raw_key in names_of(g):
                return g

    clean_key = (cleaned_name or "").strip().casefold()
    if not clean_key:
        from core.constants import strip_version_tokens
        clean_key = strip_version_tokens((raw_folder or "").strip()).casefold()
    if not clean_key:
        return None

    incoming_codes = product_codes(raw_folder)
    hits: list[tuple] = []  # (game, codes)
    for g in games:
        if clean_key not in _clean_title_keys(g):
            # Also allow the live cleaned display name equality.
            title = (getattr(g, "name", "") or "").strip().casefold()
            if title != clean_key:
                continue
        codes = _game_product_codes(g)
        # Both sides carry codes and they share none → different products.
        if incoming_codes and codes and incoming_codes.isdisjoint(codes):
            continue
        hits.append((g, codes))

    if not hits:
        return None

    def _uniq(games_list: list):
        out, seen = [], set()
        for g in games_list:
            gid = getattr(g, "id", id(g))
            if gid in seen:
                continue
            seen.add(gid)
            out.append(g)
        return out

    if incoming_codes:
        same_code = _uniq([g for g, c in hits if c & incoming_codes])
        if len(same_code) == 1:
            return same_code[0]
        if len(same_code) > 1:
            return None
        # Incoming has a code no library game shares. Other coded titles for
        # this clean name are different products — do not attach to them.
        # An uncoded entry with the same clean title is the same game seen
        # first without its RJ tag (or a version-only rename).
        other_coded = [g for g, c in hits if c]
        uncoded = _uniq([g for g, c in hits if not c])
        if other_coded and not uncoded:
            return None
        if len(uncoded) >= 1:
            return uncoded[0]
        return None

    # No product code on the incoming folder: version noise is irrelevant —
    # any single clean-title hit is a match; several are version variants of
    # the same title, so join the first rather than spawning another entry.
    uniq = _uniq([g for g, _c in hits])
    return uniq[0] if uniq else None


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
    """Deprecated no-op: hand-added saves are orphan backup indexes.

    Restore is offered through the cloud-saves notification (also offline),
    not by silently merging placeholder GameEntry rows into the library.
    """
    return 0


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
