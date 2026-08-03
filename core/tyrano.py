"""TyranoScript / TyranoBuilder saves — JSON behind JavaScript's ``escape()``.

TyranoScript keeps the whole game state in one JavaScript object and stores it
with ``$.storage.setItem``, which is ``escape(JSON.stringify(obj))``. So the
file on disk is ordinary JSON with every character JavaScript did not consider
safe written as ``%XX``, and everything above 0xFF as ``%uXXXX``.

The subtlety, and the reason this is a module of its own rather than three
lines in the editor: ``escape()`` works on UTF-16 CODE UNITS, not on
characters. An emoji is one character in Python and two code units in
JavaScript, so ``escape()`` writes it as a surrogate PAIR — ``%uD83D%uDE00``,
not ``%u1F600``. Escaping character by character produces a five-digit
sequence no TyranoScript build would ever write, and unescaping it back reads
the fifth digit as an ordinary character. Both directions therefore go through
UTF-16 explicitly, which is what the engine itself is doing.

The saves come in three shapes, all of them this same wrapper:

- ``<game>_sf.sav``          the system flags, which outlive any one playthrough
- ``<game>_tyrano_data.sav`` every numbered slot, under ``data``
- ``..._quick_save.sav`` / ``..._auto_save.sav``   one slot on its own
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# ECMA-262 B.2.1.1: what escape() leaves alone. Everything else is written as
# a percent sequence, so this set is what decides whether a rebuilt save is
# byte-for-byte the file that was there.
_KEEP = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./")

_ESCAPED = re.compile(r"%u([0-9A-Fa-f]{4})|%([0-9A-Fa-f]{2})")


class TyranoError(Exception):
    pass


def js_escape(text: str) -> str:
    """JavaScript's ``escape()``, over UTF-16 code units — see the docstring."""
    units = text.encode("utf-16-le")
    out = []
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        if code < 128 and chr(code) in _KEEP:
            out.append(chr(code))
        elif code < 256:
            out.append("%%%02X" % code)
        else:
            out.append("%%u%04X" % code)
    return "".join(out)


def js_unescape(text: str) -> str:
    """JavaScript's ``unescape()``.

    Built as a list of UTF-16 code units and decoded once at the end, so a
    surrogate pair written as two ``%uXXXX`` sequences comes back as the one
    character it stands for. Decoding each sequence on its own would give two
    lone surrogates, which is not a string Python will encode again.
    """
    units = bytearray()

    def unit(code: int) -> None:
        units.append(code & 0xFF)
        units.append((code >> 8) & 0xFF)

    pos = 0
    for m in _ESCAPED.finditer(text):
        for ch in text[pos:m.start()]:
            unit(ord(ch))
        wide, byte = m.groups()
        unit(int(wide, 16) if wide else int(byte, 16))
        pos = m.end()
    for ch in text[pos:]:
        unit(ord(ch))
    # surrogatepass: a save may legitimately hold half a pair, and refusing to
    # read the file over it would be a worse answer than carrying it through
    # untouched — it re-encodes to exactly the bytes it came from.
    return bytes(units).decode("utf-16-le", errors="surrogatepass")


def loads(data: bytes):
    """The object inside a TyranoScript save."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TyranoError("not text, so not a TyranoScript save") from e
    if "%" not in text[:64]:
        # Every TyranoScript save opens on an escaped "{" or "[". Plain JSON
        # in a .sav is somebody else's file and is read by the JSON reader.
        raise TyranoError("not escaped, so not a TyranoScript save")
    try:
        obj = json.loads(js_unescape(text))
    except (ValueError, UnicodeDecodeError) as e:
        raise TyranoError(f"not JSON once unescaped: {e}") from e
    if not isinstance(obj, (dict, list)):
        raise TyranoError("a TyranoScript save holds an object or a list")
    return obj


def dumps(obj) -> bytes:
    """The bytes TyranoScript itself would have written for *obj*."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return js_escape(text).encode("utf-8")


def state_roots(obj) -> list:
    """Where the GAME's own values live, as (path, group) pairs.

    A save holds far more than the player's state: the label map, the macro
    map, the message currently on screen, the script buffer — engine
    bookkeeping, and in a long game most of the file. TyranoScript keeps what
    the game itself set in two places, and those are what this returns:

    - ``stat.f``  the game variables, one set per save slot
    - ``sf``      the system flags, which are the whole of an ``_sf.sav``

    Returning nothing means the file is a TyranoScript save whose state is
    somewhere this does not know about, and it is refused rather than opened
    on its bookkeeping.
    """
    roots = []

    def slot(path, node, group):
        stat = node.get("stat") if isinstance(node, dict) else None
        if isinstance(stat, dict):
            for key in ("f", "sf"):
                if isinstance(stat.get(key), (dict, list)):
                    roots.append((path + ("stat", key), group))

    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        # The numbered slots. They are shown by their number rather than by
        # their title: a title is a line of the game's dialogue, HTML tags and
        # all, and makes a poor name for a category.
        for i, node in enumerate(obj["data"]):
            slot(("data", i), node, f"Slot {i + 1}")
    elif isinstance(obj, dict) and isinstance(obj.get("stat"), dict):
        slot((), obj, "Save")
    elif isinstance(obj, dict):
        # An _sf.sav: the system flags ARE the file, with no wrapper at all.
        roots.append(((), "System"))
    return roots


def at(obj, path):
    """The node *path* points at, or None when it does not point at one."""
    node = obj
    for key in path:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return None
    return node
