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
TYRANO = "tyrano"
BAKIN = "bakin"
SRPGSTUDIO = "srpgstudio"
ALICESOFT = "alicesoft"
ARTEMIS = "artemis"
# Not engines but packaging: a Chromium runtime with a game's HTML and
# JavaScript inside it. They are the answer only when nothing more particular
# is — see _engine_of_folder.
NWJS = "nwjs"
ELECTRON = "electron"

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
    # Artemis writes EVERY save as a .dat — the slots, the data kept across
    # playthroughs and the settings alike — so for this engine the extension
    # has to come off the skip list, or none of its saves is ever detected.
    # This is the same case as Unity's .dat above, and the reason the table
    # exists at all.
    ARTEMIS:    {".dat"},
    # The rest save into extensions nothing skips: TyranoScript, AliceSoft
    # and SRPG Studio write .sav, Bakin .sgs. Listed to say so, as Ren'Py and
    # Unreal are above.
    TYRANO:     set(),
    BAKIN:      set(),
    SRPGSTUDIO: set(),
    ALICESOFT:  set(),
    # A wrapper says nothing about what the game inside it saves into, and
    # .dat next to a Chromium runtime is as often the runtime's own as it is
    # a save. Unknown is the conservative side and this is the same case.
    NWJS:       set(),
    ELECTRON:   set(),
}

# The folder an engine really saves into, when that folder's NAME is one the
# detector otherwise throws away. The companion to the table above: that one
# says which extensions are saves for an engine, this one says which folders
# are — same question, same shape, same reason for existing.
#
# "data" is skipped everywhere because for most engines it is the game's own
# content; for TyranoScript it IS the whole game. Bakin puts the player's
# saves in data/savedata, so no single rule can be right for both, and the
# engine is what tells them apart.
_ENGINE_SAVE_DIRS = {
    BAKIN: (("data", "savedata"),),
}


_LABELS = {
    RPGMAKER: "RPG Maker",
    UNITY: "Unity",
    UNREAL: "Unreal Engine",
    RENPY: "Ren'Py",
    GODOT: "Godot",
    GAMEMAKER: "GameMaker",
    TYRANO: "TyranoScript",
    BAKIN: "RPG Developer Bakin",
    SRPGSTUDIO: "SRPG Studio",
    ALICESOFT: "AliceSoft System",
    ARTEMIS: "Artemis",
    NWJS: "NW.js",
    ELECTRON: "Electron",
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
    # TyranoScript keeps its runtime in a folder of its own. Where that folder
    # sits depends on how the game was exported — TyranoBuilder puts the game
    # under data/, a plain TyranoScript export puts the runtime at the top,
    # and an Electron build buries both under resources/app. So it is looked
    # for rather than listed, and it must be found BEFORE the wrappers below:
    # a Tyrano game is packaged with NW.js or Electron, and answering with the
    # wrapper would be answering with the box instead of what is in it.
    for probe in (folder / "data" / "tyrano", folder / "tyrano",
                  folder / "www" / "tyrano",
                  folder / "resources" / "app" / "data" / "tyrano",
                  folder / "resources" / "app" / "tyrano"):
        if probe.is_dir():
            return TYRANO
    # Bakin ships its runtime beside the game rather than as the game: the
    # executable at the top is the title's own, and bakinplayer is under data.
    if "bakinengine.dll" in names or "bakinplayer.exe" in names:
        return BAKIN
    if (folder / "data" / "bakinengine.dll").exists() \
            or (folder / "data" / "data.rbpack").exists():
        return BAKIN
    # AliceSoft: the archives its System 3/4 games are built out of, and the
    # ini its launcher reads.
    if "alicestart.ini" in names or ".ald" in suffixes or ".alk" in suffixes:
        return ALICESOFT
    if any(n.startswith("system4") and n.endswith(".ini") for n in names):
        return ALICESOFT
    # Artemis packs its content into .pfs archives, root.pfs first.
    #
    # Every marker in this function has been checked against a real
    # installation except one: the Electron branch at the end. It is written
    # narrowly for that reason — a marker that is too tight only fails to
    # recognise a game, which is where it already stood, while one that is too
    # loose claims somebody else's.
    if "root.pfs" in names or ".pfs" in suffixes:
        return ARTEMIS
    # SRPG Studio: its packed data, and only alongside the folders a published
    # game carries — .dts on its own is too plain a name to claim a game with.
    if ".dts" in suffixes and ("material" in names or "resource" in names
                              or "save" in names):
        return SRPGSTUDIO
    # The wrappers, last of all. NW.js and Electron are a Chromium runtime
    # with somebody's game inside, and every engine above that ships as one —
    # RPG Maker MV on NW.js, TyranoScript on either — would be answered with
    # the wrapper instead of with itself if this ran any earlier.
    if "nw.dll" in names or "package.nw" in names:
        return NWJS
    if (folder / "resources" / "app.asar").exists() \
            or (folder / "resources" / "app").is_dir():
        return ELECTRON
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
    # A wrapper is the answer only when nothing more particular is, and that
    # has to hold across the WHOLE walk rather than within one folder: a game
    # whose executable sits in a subfolder keeps the Chromium runtime beside
    # it and its own engine a level up, so stopping at the first answer would
    # stop on the wrapper and never reach the engine. Remember it and keep
    # walking; it is returned only if the levels above have nothing to say.
    wrapper = ""
    folder = start
    for _ in range(_MAX_UP):
        try:
            if folder.is_dir():
                engine = _engine_of_folder(folder)
                if engine and engine not in (NWJS, ELECTRON):
                    break
                if engine:
                    wrapper, engine = wrapper or engine, ""
        except OSError:
            break
        if folder.parent == folder:
            break
        folder = folder.parent
    engine = engine or wrapper

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


def saves_in_skipped_dir(engine: str, path) -> bool:
    """Whether *path* is a folder this engine genuinely saves into, even
    though its name is on the skip list — see _ENGINE_SAVE_DIRS."""
    from pathlib import Path as _Path

    tails = _ENGINE_SAVE_DIRS.get(engine)
    if not tails:
        return False
    parts = [p.lower() for p in _Path(str(path)).parts]
    return any(len(parts) >= len(tail) and parts[-len(tail):] == list(tail)
               for tail in tails)


def engine_for_game(entry) -> str:
    """The engine of a library entry, from whatever it can be read off."""
    if entry is None:
        return ""
    exe = getattr(entry, "exe_path", "") or ""
    return detect_engine(exe_path=exe) if exe else ""
