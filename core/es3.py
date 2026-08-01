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
_MAX_ASSET = 256 << 20
_MAX_CANDIDATES = 12
_MAX_PASSWORD = 200
# A key the player dropped in themselves, as the published dumper writes it.
_KEY_FILE = "es3.key"
# Passwords already worked out, by save file. Holding a value re-opens the
# same save several times a second, and hunting through a game's assets each
# time would be absurd.
_PASSWORDS = {}
_PASSWORD_KEEP = 32


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
    for data in sorted(game_dir.glob("*_Data")):
        for name in _ASSET_NAMES:
            candidate = data / name
            if candidate.is_file():
                yield candidate


def find_password(raw: bytes, save_path=None, game_dir=None) -> str:
    """A password that actually opens *raw*, or an empty string.

    Looked for in the order that costs least: one already worked out, then a
    key file someone put beside the save, then the game's own settings.
    """
    key = str(save_path).lower() if save_path else ""
    if key and key in _PASSWORDS:
        return _PASSWORDS[key]

    tried = []
    places = []
    if save_path:
        places.append(Path(save_path).parent)
    if game_dir:
        places.append(Path(game_dir))
    for place in places:
        try:
            keyfile = place / _KEY_FILE
            if keyfile.is_file():
                tried.append(keyfile.read_text(encoding="utf-8",
                                               errors="replace").strip())
        except OSError:
            pass
    for place in places:
        try:
            for asset in _asset_files(place):
                tried.extend(_candidates(asset))
        except OSError:
            continue

    seen = set()
    for password in tried:
        if not password or password in seen:
            continue
        seen.add(password)
        try:
            if decrypt(raw, password)[:1] in (b"{", b"["):
                if len(_PASSWORDS) >= _PASSWORD_KEEP:
                    _PASSWORDS.clear()
                if key:
                    _PASSWORDS[key] = password
                logger.info(f"Easy Save 3: found the password for "
                            f"{Path(save_path).name if save_path else 'a save'}")
                return password
        except (Es3Error, ValueError):
            continue
    return ""


def remember(save_path, password: str) -> None:
    """Keep a password the player supplied, for as long as the app runs."""
    if save_path and password:
        if len(_PASSWORDS) >= _PASSWORD_KEEP:
            _PASSWORDS.clear()
        _PASSWORDS[str(save_path).lower()] = password
