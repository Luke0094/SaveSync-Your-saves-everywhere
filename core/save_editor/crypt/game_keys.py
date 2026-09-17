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
    """Remember *key* as this game's, so it is never worked out twice.

    Merges into whatever the file already holds rather than replacing it —
    same reason store_value below reads-then-writes: a (kind, place) file
    can hold both this single key AND store_value's own "values" table (see
    its docstring), and a plain overwrite here would silently wipe that
    table the moment store_key next ran for that same file. Every caller
    today uses "kind" values (es3/unreal for store_key, wolfrpg_lz4 for
    store_value) that never collide on the same file, so this has not
    fired in practice — but nothing about store_key's OWN logic should
    depend on that staying true forever, any more than store_value's does.
    """
    if not key:
        return
    try:
        path = _key_file(kind, place)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        body_data = {"game": str(game_identity(place)), "key": key}
        if existing.get("values"):
            body_data["values"] = existing["values"]
        # The game's folder is written beside the key so a person can read
        # the file: a directory of hashes says nothing on its own.
        body = json.dumps(body_data, ensure_ascii=False, indent=1)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        logger.info(f"{kind}: remembered the key for {game_identity(place).name}")
    except OSError as e:
        logger.debug(f"{kind}: a key could not be stored ({e})")


def stored_value(kind: str, place, name: str) -> str:
    """One NAMED value remembered for this game and engine, or "".

    Distinct from stored_key/store_key above: those hold the ONE key a
    whole game needs (an Easy Save 3 password, an Unreal key). Some engines
    need more than one remembered fact per game instead — Wolf RPG's LZ4
    variant recovers one keystream seed per save-SLOT salt, not one seed
    for the whole game, so "the key" is really a small, growing table. Both
    live in the same per-(kind, game) file rather than a second one, which
    is what keeps this "the same game_keys system" rather than a parallel
    store next to it.
    """
    try:
        path = _key_file(kind, place)
        if not path.is_file():
            return ""
        values = json.loads(path.read_text(encoding="utf-8")).get("values") or {}
        return str(values.get(name) or "")
    except Exception as e:
        logger.debug(f"{kind}: a stored value could not be read ({e})")
        return ""


def store_value(kind: str, place, name: str, value: str) -> None:
    """Remember *value* under *name* for this game — see stored_value.

    Merges into whatever the file already holds (its "key", and any other
    named values) rather than replacing it — a game with several save
    slots adds one entry per slot's salt over time, and each write must
    leave the others exactly as they were.
    """
    if not value:
        return
    try:
        path = _key_file(kind, place)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        values = dict(existing.get("values") or {})
        values[name] = value
        body_data = {"game": str(game_identity(place)), "values": values}
        if existing.get("key"):
            body_data["key"] = existing["key"]
        body = json.dumps(body_data, ensure_ascii=False, indent=1)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        logger.info(f"{kind}: remembered {name} for {game_identity(place).name}")
    except OSError as e:
        logger.debug(f"{kind}: a value could not be stored ({e})")


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
