"""SaveSync — which engine a game was built with.

Only used to answer questions where the engine changes the right answer. The
first of those is ``.dat``: Unity games routinely save into one, while RPG
Maker ships its entire game database as ``.dat``/``.rvdata`` files that are
engine data and never a save. Excluding the extension outright is right for
one and wrong for the other, so the engine has to be known first.

Detection reads the INSTALL folder, never the save folder: an engine leaves
its fingerprints next to its executable, and a save folder in AppData looks
much the same whoever wrote it.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# How far up from the executable to look. Many games ship the exe in a
# subfolder ("Game/Binaries/Win64/Game.exe"), so the markers can sit a level
# or two above it.
_MAX_UP = 3
# Directory entries are listed once per folder; a game folder with thousands
# of files should not turn a yes/no question into a directory walk.
_MAX_ENTRIES = 400

RPGMAKER = "rpgmaker"
UNITY = "unity"
UNREAL = "unreal"
RENPY = "renpy"
GODOT = "godot"
GAMEMAKER = "gamemaker"

# Extensions each engine genuinely WRITES SAVES into, and which therefore
# must not be excluded from detection for a game built with it. One table so
# the answer to "why is this file offered / not offered?" is a single line to
# read, and a single line to change.
#
# These are engine conventions, not something SaveSync measures: the entries
# say what an engine's own save API produces by default, and what its
# communities overwhelmingly ship. Add to a row when a real game proves it.
_ENGINE_SAVE_EXTENSIONS = {
    # BinaryFormatter / custom binary writers land in .dat or .bin; the same
    # engine's build artefacts live in <Game>_Data, not in a save folder.
    UNITY:     {".dat", ".bin"},
    # FileAccess.store_var writes an opaque binary blob, named .dat or .bin
    # by convention (Godot itself imposes no extension on user:// files).
    GODOT:     {".dat", ".bin"},
    # buffer_save / ds_map_secure_save default to .dat in the sandbox folder.
    GAMEMAKER: {".dat"},
    # Ren'Py writes .save, Unreal writes .sav — neither is on the skip list,
    # so neither needs an exception. Listed to say so out loud.
    RENPY:     set(),
    UNREAL:    set(),
    # The opposite case, and the reason this table exists: RPG Maker ships
    # its game DATABASE as .dat/.rvdata. Excluding it here is correct.
    RPGMAKER:  set(),
}

_LABELS = {
    RPGMAKER: "RPG Maker",
    UNITY: "Unity",
    UNREAL: "Unreal Engine",
    RENPY: "Ren'Py",
    GODOT: "Godot",
    GAMEMAKER: "GameMaker",
}

_cache: dict = {}


def label(engine: str) -> str:
    return _LABELS.get(engine, "")


def _names(folder: Path) -> tuple:
    """(lowercased names, lowercased suffixes) of one folder, bounded."""
    names, suffixes = set(), set()
    try:
        for i, child in enumerate(folder.iterdir()):
            if i >= _MAX_ENTRIES:
                break
            names.add(child.name.lower())
            if child.is_file():
                suffixes.add(child.suffix.lower())
    except OSError:
        pass
    return names, suffixes


def _engine_of_folder(folder: Path) -> str:
    names, suffixes = _names(folder)
    if not names:
        return ""

    # Ren'Py: its own runtime folder sits beside the exe.
    if "renpy" in names and ("lib" in names or "game" in names):
        return RENPY
    # RPG Maker MV/MZ ship a www/ (or js/) tree with the engine core in it.
    for js in (folder / "www" / "js", folder / "js"):
        try:
            if js.is_dir() and any(
                    f.name.lower() in ("rpg_core.js", "rmmz_core.js")
                    for f in js.iterdir()):
                return RPGMAKER
        except OSError:
            pass
    # RPG Maker XP/VX/VX Ace: the RGSS runtime, and Data/*.rvdata*.
    if any(n.startswith("rgss") for n in names):
        return RPGMAKER
    data = folder / "Data"
    try:
        if data.is_dir() and any(
                f.suffix.lower() in (".rvdata2", ".rvdata", ".rxdata")
                for f in data.iterdir()):
            return RPGMAKER
    except OSError:
        pass
    # Unity: the player library, or the <Game>_Data folder it always makes.
    if "unityplayer.dll" in names:
        return UNITY
    for n in names:
        if n.endswith("_data") and (folder / n / "globalgamemanagers").exists():
            return UNITY
    # GameMaker keeps its whole game in one file next to the exe.
    if "data.win" in names:
        return GAMEMAKER
    # Godot packs everything into a .pck beside the exe.
    if ".pck" in suffixes:
        return GODOT
    # Unreal: the engine tree, or the cooked content packs.
    if "engine" in names and (folder / "Engine" / "Binaries").is_dir():
        return UNREAL
    return ""


def detect_engine(exe_path: str = "", game_dir: str = "") -> str:
    """The engine a game was built with, or "" when nothing says.

    Cheap, and POSITIVE answers are cached: what an executable was built
    with does not change while SaveSync runs, and this is asked once per
    detection run. An unknown answer is not cached — see below.
    """
    key = str(exe_path or game_dir or "")
    if not key:
        return ""
    if key in _cache:
        return _cache[key]

    start = Path(game_dir) if game_dir else Path(exe_path).parent
    engine = ""
    folder = start
    for _ in range(_MAX_UP):
        try:
            if folder.is_dir():
                engine = _engine_of_folder(folder)
                if engine:
                    break
        except OSError:
            break
        if folder.parent == folder:
            break
        folder = folder.parent

    # Only a POSITIVE answer is remembered. "Unknown" is often just "not
    # reachable yet" — an unplugged drive, a game added before it was
    # installed — and caching that would keep the game unknown for the rest
    # of the session with no way to retry. Re-asking costs a bounded listing
    # of at most three folders.
    if engine:
        _cache[key] = engine
        logger.debug(f"{Path(key).name}: looks like {label(engine)}")
    return engine


def detection_skip_extensions(engine: str = "") -> set:
    """Extensions that must not DRIVE automatic save detection.

    The base set is deliberately broad because those extensions are engine
    noise far more often than they are saves. When the engine is known to
    save into one of them, it comes off the list for that game — which is
    the whole reason the engine is detected at all.
    """
    from core.constants import DETECTION_SKIP_EXTENSIONS

    return set(DETECTION_SKIP_EXTENSIONS) - _ENGINE_SAVE_EXTENSIONS.get(engine, set())


def engine_save_extensions(engine: str = "") -> set:
    """The extensions this engine actually saves into — see the table."""
    return set(_ENGINE_SAVE_EXTENSIONS.get(engine, set()))


def engine_for_game(entry) -> str:
    """The engine of a library entry, from whatever it can be read off."""
    if entry is None:
        return ""
    exe = getattr(entry, "exe_path", "") or ""
    return detect_engine(exe_path=exe) if exe else ""
