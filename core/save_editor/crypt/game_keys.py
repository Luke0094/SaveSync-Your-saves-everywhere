"""Keys a game needs before its saves can be read, remembered per game.

Some engines lock their saves with a key that lives in the game rather than
in the save: Easy Save 3 bakes a password into the build, and an Unreal game
that encrypts does the same with its own. Working one out can mean unpacking
archives — seconds of work — and doing it again for the next save of the same
game would be paying twice for an answer already known.

So a key is written down against the GAME, not in one list of them all: two
games' keys have nothing to do with each other, and trying every key ever
seen against every save would be work that can only fail. *kind* keeps the
engines apart, so an Unreal key is never offered to Easy Save.

What is stored is a game's own save-encryption key. Nothing here belongs to
the player, and none of it opens anything but the saves already on this
machine.
"""
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = "game_keys"


def game_identity(place) -> Path:
    """The folder that stands for the GAME a place belongs to.

    A game's data folder and the game folder holding it are the same game, so
    both reduce to one name — otherwise a key found through the save would
    not be found again through the library.
    """
    p = Path(place)
    return p.parent if p.name.lower().endswith("_data") else p


def _key_file(kind: str, place) -> Path:
    from core.constants import USER_DATA_DIR
    game = game_identity(place)
    name = hashlib.sha1(str(game).lower().encode("utf-8")).hexdigest()[:12]
    return USER_DATA_DIR / _DIR / f"{name}.{kind}.json"


def stored_key(kind: str, place) -> str:
    """The key remembered for this game and engine, or an empty string."""
    try:
        path = _key_file(kind, place)
        if not path.is_file():
            return ""
        return str(json.loads(path.read_text(encoding="utf-8")).get("key") or "")
    except Exception as e:
        logger.debug(f"{kind}: a stored key could not be read ({e})")
        return ""


def store_key(kind: str, place, key: str) -> None:
    """Remember *key* as this game's, so it is never worked out twice."""
    if not key:
        return
    try:
        path = _key_file(kind, place)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The game's folder is written beside the key so a person can read
        # the file: a directory of hashes says nothing on its own.
        body = json.dumps({"game": str(game_identity(place)), "key": key},
                          ensure_ascii=False, indent=1)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        logger.info(f"{kind}: remembered the key for {game_identity(place).name}")
    except OSError as e:
        logger.debug(f"{kind}: a key could not be stored ({e})")


def key_from_file(place, filename: str) -> str:
    """A key the player dropped beside their save, as published tools write it."""
    try:
        candidate = Path(place) / filename
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8",
                                       errors="replace").strip()
    except OSError:
        pass
    return ""
