"""Unity's Easy Save 3, when the developer turned encryption on.

An Easy Save 3 file is JSON. Encryption is optional and off by default, so
most of them can simply be opened — those never reach this module. The ones
that were encrypted are AES-128 in CBC mode: the first sixteen bytes of the
file are the initialisation vector, and the key is derived from a password
with PBKDF2 over a hundred rounds, salted with that same vector.

The password is chosen by whoever made the game, and it is *in* the game: the
Easy Save settings are baked into the build as an ``ES3Defaults`` object, and
the password sits in it as ordinary text. So SaveSync reads it out of the
game's own files — files at rest, the same as everything else it does. It
never attaches to a running game, which is how the published tools for this
work and is exactly the line this program does not cross.

Nothing here guesses. A candidate password is accepted only when it actually
decrypts the save to JSON, so a wrong string simply fails instead of quietly
producing rubbish.
"""
import hashlib
import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

_IV_SIZE = 16
_KEY_SIZE = 16
_ROUNDS = 100
# Where Easy Save keeps its settings inside a built game.
_SETTINGS_MARKER = b"ES3Defaults"
# Asset files worth opening, and how far past the marker to keep reading.
_ASSET_NAMES = ("resources.assets", "globalgamemanagers.assets",
                "sharedassets0.assets")
# …and then every other asset file in the same folder. The three above are
# where Easy Save's settings usually land and are tried first because they
# usually answer, but a game is free to put them in sharedassets7 or in a
# numbered level, and looking only at three names left those unopenable.
_ASSET_GLOBS = ("*.assets", "level*", "globalgamemanagers")
# Bundles, which are the same data squeezed — see core/unityfs. Only reached
# when the plain files above have not answered, because unpacking one costs
# real time where reading a plain file costs almost none.
_BUNDLE_GLOBS = ("data.unity3d", "*.bundle")
# An archive larger than this is left alone: at roughly 11 MB a second it
# would be minutes on its own, and the settings object being looked for is a
# few hundred bytes that games do not bury in their largest archive.
_MAX_BUNDLE_FILE = 64 << 20
_MAX_BUNDLES = 12
# There is no time limit on the search. Unpacking is around 11 MB a second,
# so a game shipped as archives can take a while — but stopping early means
# reporting a save as unopenable when the key was there to be found, which is
# the worse answer. Instead the caller is told how long it has been going and
# decides: see the *progress* argument to find_password.
#
# Whatever is found is written down against that game (see _KEY_DIR), so the
# cost is paid once and never again for it.
_MAX_ASSET = 256 << 20
_MAX_CANDIDATES = 12
_MAX_PASSWORD = 200
# How far above the save to look for the game it belongs to.
_MAX_CLIMB = 3
# A key the player dropped in themselves, as the published dumper writes it.
_KEY_FILE = "es3.key"
# Passwords already worked out, by save file. Holding a value re-opens the
# same save several times a second, and hunting through a game's assets each
# time would be absurd.
_PASSWORDS = {}
_PASSWORD_KEEP = 32

# A password, remembered against the GAME it belongs to.
#
# Finding one can mean unpacking a game's archives, which is seconds of work,
# and doing that again for the next save of the same game would be paying
# twice for an answer already known. So it is written down — but per game,
# not in one list of them all: two games' passwords have nothing to do with
# each other, and trying every password ever seen against every save would be
# work that can only fail.
#
# Kept by core/game_keys, which every engine with this problem shares.
_KIND = "es3"


class Es3Error(Exception):
    pass


def is_encrypted(raw: bytes) -> bool:
    """Plain Easy Save 3 is JSON; anything else was encrypted."""
    return bool(raw) and raw[:1] not in (b"{", b"[")


def _cipher(password: str, iv: bytes):
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), iv,
                              _ROUNDS, _KEY_SIZE)
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def decrypt(raw: bytes, password: str) -> bytes:
    if len(raw) <= _IV_SIZE or (len(raw) - _IV_SIZE) % 16:
        raise Es3Error("not a whole number of blocks")
    iv = raw[:_IV_SIZE]
    dec = _cipher(password, iv).decryptor()
    out = dec.update(raw[_IV_SIZE:]) + dec.finalize()
    pad = out[-1] if out else 0
    if not 0 < pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise Es3Error("wrong password")
    return out[:-pad]


def encrypt(plain: bytes, password: str, iv: bytes) -> bytes:
    """Lock it again with the vector it came with.

    Reusing the original vector is deliberate: it makes writing an unchanged
    save produce the very same bytes, so a file SaveSync merely opened is
    provably left alone.
    """
    if len(iv) != _IV_SIZE:
        raise Es3Error("the initialisation vector is the wrong size")
    pad = 16 - (len(plain) % 16)
    enc = _cipher(password, iv).encryptor()
    return iv + enc.update(plain + bytes([pad]) * pad) + enc.finalize()


def dumps(data) -> bytes:
    """JSON written the way Easy Save writes it.

    Any valid JSON would be read back by the game, but matching its own style
    means a save that was opened and not changed comes out as the very same
    bytes. Tabs, a space either side of the colon, CRLF line endings — and
    arrays kept on one line, which is the one place Easy Save differs from
    everybody else.
    """
    import json

    def write(node, depth):
        pad = "\t" * depth
        inner = "\t" * (depth + 1)
        if isinstance(node, dict):
            if not node:
                return "{}"
            body = ",\r\n".join(
                f"{inner}{json.dumps(k, ensure_ascii=False)} : {write(v, depth + 1)}"
                for k, v in node.items())
            return "{\r\n" + body + "\r\n" + pad + "}"
        if isinstance(node, list):
            if not node:
                return "[]"
            body = ",".join(write(v, depth + 1) for v in node)
            return "[\r\n" + inner + body + "\r\n" + pad + "]"
        return json.dumps(node, ensure_ascii=False)

    return write(data, 0).encode("utf-8")


def _candidates(path: Path) -> list:
    """Strings stored next to Easy Save's settings in a built game."""
    import mmap
    try:
        size = path.stat().st_size
        if size > _MAX_ASSET or size < len(_SETTINGS_MARKER):
            return []
        # Mapped rather than read: a game's asset file can be hundreds of
        # megabytes, and all that is wanted is a short run of text in it.
        with open(path, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as blob:
                return _walk_strings(blob)
    except (OSError, ValueError):
        return []


def _walk_strings(blob) -> list:
    at = blob.find(_SETTINGS_MARKER)
    if at < 0:
        return []
    out = []
    # Unity writes a string as its length and then its bytes, on a four-byte
    # boundary. Walking forward from the marker turns up the save's file name
    # and, right behind it, the password.
    o = (at + len(_SETTINGS_MARKER) + 3) & ~3
    while len(out) < _MAX_CANDIDATES and o + 4 <= len(blob):
        n = struct.unpack_from("<i", blob, o)[0]
        if 0 < n <= _MAX_PASSWORD and o + 4 + n <= len(blob):
            raw = blob[o + 4:o + 4 + n]
            if all(32 <= c < 127 for c in raw):
                out.append(raw.decode("ascii"))
            o = (o + 4 + n + 3) & ~3
        else:
            o += 4
    return out


def _asset_files(game_dir: Path):
    """The asset files worth searching, at or under *game_dir*.

    The folder ITSELF counts when it is the game's data folder, and that is
    not a corner case: Unity's own save location is under the user's profile,
    but plenty of games keep the save inside ``<Game>_Data`` instead, right
    beside the very assets the password is in. Looking only for a ``*_Data``
    CHILD missed those — the file was one directory listing away and the save
    was reported as unopenable.
    """
    folders = []
    if game_dir.name.lower().endswith("_data"):
        folders.append(game_dir)
    folders.extend(sorted(game_dir.glob("*_Data")))

    for folder in folders:
        seen = set()
        # The likely three first, so the common case is answered without
        # listing the folder at all.
        for name in _ASSET_NAMES:
            candidate = folder / name
            if candidate.is_file():
                seen.add(candidate.name.lower())
                yield candidate
        for pattern in _ASSET_GLOBS:
            try:
                for candidate in sorted(folder.glob(pattern)):
                    if (candidate.is_file()
                            and candidate.name.lower() not in seen
                            and candidate.suffix.lower() != ".resS".lower()):
                        seen.add(candidate.name.lower())
                        yield candidate
            except OSError:
                continue


def _bundle_files(game_dir: Path):
    """The squeezed archives worth unpacking, biggest cost last."""
    folders = []
    if game_dir.name.lower().endswith("_data"):
        folders.append(game_dir)
    folders.extend(sorted(game_dir.glob("*_Data")))
    out = []
    for folder in folders:
        for pattern in _BUNDLE_GLOBS:
            try:
                for candidate in folder.glob(pattern):
                    if candidate.is_file() and candidate.stat().st_size <= _MAX_BUNDLE_FILE:
                        out.append(candidate)
            except OSError:
                continue
    # Smallest first: the settings object is tiny and often in a small
    # bundle, and a miss then costs the least.
    out.sort(key=lambda p: p.stat().st_size)
    return out[:_MAX_BUNDLES]


def _bundle_candidates(path: Path, on_tick=None) -> list:
    """Strings from Easy Save's settings inside a squeezed archive."""
    from core.save_editor.unityfs import SIGNATURE, UnityFsError, unpack
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if not raw.startswith(SIGNATURE):
        return []
    try:
        blob = unpack(raw, stop_after=_SETTINGS_MARKER, on_tick=on_tick)
    except (UnityFsError, Exception) as e:
        logger.debug(f"{path.name}: could not unpack ({e})")
        return []
    if _SETTINGS_MARKER not in blob:
        return []
    logger.info(f"Easy Save 3: settings found inside {path.name}")
    return _walk_strings(blob)


def stored_key(place) -> str:
    """The password remembered for this game, or an empty string."""
    from core.save_editor.game_keys import stored_key as _stored
    return _stored(_KIND, place)


def _store_key(place, password: str) -> None:
    """Remember *password* as this game's, so it is never hunted for twice."""
    from core.save_editor.game_keys import store_key
    store_key(_KIND, place, password)


def find_password(raw: bytes, save_path=None, game_dir=None,
                  progress=None) -> str:
    """A password that actually opens *raw*, or an empty string.

    Looked for in the order that costs least: one already worked out this
    run, then the ones written down from before, then a key file someone put
    beside the save, then the game's own settings, and only then the archives
    — which are the only step that takes real time.

    *progress*, when given, is called as the search goes on with the seconds
    elapsed so far. Returning False from it stops the search and reports no
    password; anything else carries on. It exists so the person waiting can
    see that something is happening and call it off, rather than having a
    limit chosen for them.
    """
    key = str(save_path).lower() if save_path else ""
    if key and key in _PASSWORDS:
        return _PASSWORDS[key]

    def _accepts(password: str) -> bool:
        try:
            return decrypt(raw, password)[:1] in (b"{", b"[")
        except (Es3Error, ValueError):
            return False

    def _keep(password: str, place=None) -> str:
        if len(_PASSWORDS) >= _PASSWORD_KEEP:
            _PASSWORDS.clear()
        if key:
            _PASSWORDS[key] = password
        if place is not None:
            _store_key(place, password)
        logger.info(f"Easy Save 3: found the password for "
                    f"{Path(save_path).name if save_path else 'a save'}")
        return password

    tried = []
    places = []
    if save_path:
        # The save's own folder, and a couple above it. A game that keeps its
        # save inside its own tree puts the assets one or two directories up
        # — "<Game>/<Game>_Data/SaveFile.es3" is the common shape — and
        # without climbing, a save that came in on its own could never be
        # opened even with the whole game sitting around it. Bounded at three
        # so this stays a look at the game and never becomes a disk search.
        here = Path(save_path).parent
        places.append(here)
        for parent in list(here.parents)[:_MAX_CLIMB]:
            places.append(parent)
    if game_dir:
        places.append(Path(game_dir))
    # Same folder reached two ways is the same folder: opening its assets
    # twice would double the cost of every miss.
    seen_places, unique = set(), []
    for place in places:
        try:
            marker = str(place.resolve()).lower()
        except OSError:
            marker = str(place).lower()
        if marker not in seen_places:
            seen_places.add(marker)
            unique.append(place)
    places = unique

    # This game's own key, if it was worked out before. Only ITS key is
    # tried — a password belongs to one game, and trying every game's would
    # be work that can only fail.
    for place in places:
        remembered = stored_key(place)
        if remembered and _accepts(remembered):
            return _keep(remembered)

    for place in places:
        try:
            keyfile = place / _KEY_FILE
            if keyfile.is_file():
                tried.append((keyfile.read_text(encoding="utf-8",
                                                errors="replace").strip(), place))
        except OSError:
            pass
    for place in places:
        try:
            for asset in _asset_files(place):
                tried.extend((p, place) for p in _candidates(asset))
        except OSError:
            continue

    for password, place in tried:
        if password and _accepts(password):
            return _keep(password, place)

    # The squeezed archives are only opened when nothing above has answered:
    # unpacking one is seconds where reading a plain file is milliseconds, so
    # it is a last resort rather than part of the sweep. It runs to the end
    # unless the person waiting calls it off.
    import time
    started = time.monotonic()

    def _carry_on() -> bool:
        if progress is None:
            return True
        try:
            return progress(time.monotonic() - started) is not False
        except Exception as e:
            logger.debug(f"Easy Save 3: the progress callback raised ({e})")
            return True

    for place in places:
        try:
            bundles = _bundle_files(place)
        except OSError:
            continue
        for bundle in bundles:
            if not _carry_on():
                logger.info("Easy Save 3: the search was called off")
                return ""
            for password in _bundle_candidates(bundle, on_tick=_carry_on):
                if password and _accepts(password):
                    return _keep(password, place)
    return ""


def remember(save_path, password: str, game_dir=None) -> None:
    """Keep a password the player supplied.

    Written down against the game as well as held for the session, so a key
    typed in once does not have to be typed in again — the same promise the
    search itself makes.
    """
    if save_path and password:
        _store_key(game_dir or Path(save_path).parent, password)
        if len(_PASSWORDS) >= _PASSWORD_KEEP:
            _PASSWORDS.clear()
        _PASSWORDS[str(save_path).lower()] = password
