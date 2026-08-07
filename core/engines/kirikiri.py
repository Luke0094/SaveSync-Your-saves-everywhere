"""KiriKiri / KAG saves (``.ksd``).

A KiriKiri save is not a binary structure to reverse-engineer: it is the
game's state written out as a TJS dictionary literal, in UTF-16, and it is
readable as it stands::

    %[
     "core" => %[
      "opacity" => int 255,
      "storage" => string "1232",
      "mcolor" => void,

Only the wrapper varies, and there are three of them in the wild:

- the text on its own, starting with a byte-order mark;
- ``\\xfe\\xfe\\x02`` then the compressed and uncompressed sizes, then the
  text deflated — and the games examined used zlib's default level, so it
  recompresses to the very same bytes;
- a bitmap first, which is the thumbnail the game shows in its load menu,
  followed by the text at the offset the bitmap's own header declares.

Editing splices the new value into the text and leaves every other character
as it arrived, so the file rebuilds exactly whichever wrapper it came in.

The round trip is therefore not much of a test on its own: a reader that
understood nothing and simply kept the text would pass it. So the real check
is that the walk **accounts for every character** — the parser must arrive at
the end of the text with nothing left over. A construct it does not know
stops it, and the file is reported unreadable rather than half-read.
"""
import logging
import struct
import zlib

logger = logging.getLogger(__name__)

_COMPRESSED_MAGIC = b"\xfe\xfe\x02"
_BOM = b"\xff\xfe"
_BMP_MAGIC = b"BM"
# Magic, then the compressed and uncompressed sizes as 64-bit words.
_COMPRESSED_HEADER = 21
# The level every game examined had used — and it matters, because matching it
# is what lets a save be written back byte-for-byte.
_ZLIB_LEVEL = 6
# A save is a page or two of text. Well past anything seen, and it stops a
# corrupt length from asking for a gigabyte.
_MAX_TEXT = 64 << 20
# How deep the dictionary may nest before we call it a runaway.
_MAX_DEPTH = 64

# TJS names a scalar's type before the value itself: ``int 255``.
_TYPED = ("int", "real", "string")
_BARE = {"void": None, "true": True, "false": False, "null": None}


class KirikiriError(Exception):
    pass


class _Scan:
    """A cursor over the save's text, which is also where edits will land."""

    __slots__ = ("s", "i", "n")

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    def ws(self) -> None:
        """Whitespace, and comments \u2014 TJS writes the plain decimal of a hex
        float beside it, as ``0x1.E000000000000p3 /* 15 */``."""
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n\ufeff":
                self.i += 1
            elif self.s.startswith("/*", self.i):
                end = self.s.find("*/", self.i + 2)
                if end < 0:
                    raise KirikiriError("a comment runs past the end of the save")
                self.i = end + 2
            elif self.s.startswith("//", self.i):
                nl = self.s.find("\n", self.i)
                self.i = self.n if nl < 0 else nl
            else:
                return

    def at(self, lit: str) -> bool:
        return self.s.startswith(lit, self.i)

    def take(self, lit: str) -> None:
        if not self.at(lit):
            raise KirikiriError(
                f"expected {lit!r} at {self.i}, found {self.s[self.i:self.i + 12]!r}")
        self.i += len(lit)

    def word(self) -> str:
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
            self.i += 1
        return self.s[start:self.i]

    def string(self) -> str:
        """A quoted string, returned raw — quotes and escapes included."""
        if self.s[self.i] not in "\"'":
            raise KirikiriError(f"expected a string at {self.i}")
        quote = self.s[self.i]
        start = self.i
        self.i += 1
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            self.i += 1
            if c == quote:
                return self.s[start:self.i]
        raise KirikiriError("a string runs past the end of the save")

    def number(self) -> str:
        """A number, decimal or hexadecimal.

        TJS writes reals in hexadecimal so they survive exactly — ``0x1.72p8``
        — where the exponent is marked with ``p``, because ``e`` is a digit in
        hex. Decimal numbers mark it with ``e`` as usual.
        """
        start = self.i
        if self.i < self.n and self.s[self.i] in "+-":
            self.i += 1
        hexed = self.s[self.i:self.i + 2].lower() == "0x"
        if hexed:
            self.i += 2
        exponent = "pP" if hexed else "eE"
        while self.i < self.n:
            c = self.s[self.i]
            if c.isdigit() or c == ".":
                self.i += 1
            elif hexed and c in "abcdefABCDEF":
                self.i += 1
            elif c in exponent:
                self.i += 1
                if self.i < self.n and self.s[self.i] in "+-":
                    self.i += 1
            else:
                break
        if self.i == start:
            raise KirikiriError(f"expected a number at {start}")
        return self.s[start:self.i]


def _number(raw: str):
    """The value a TJS numeric literal stands for, and whether it is a real."""
    low = raw.lower()
    hexed = low.startswith(("0x", "-0x", "+0x"))
    real = "." in raw or ("p" in low if hexed else "e" in low)
    if not real:
        return int(raw, 0), False
    return (float.fromhex(raw) if hexed else float(raw)), True


def _unquote(raw: str) -> str:
    """The text a TJS string literal stands for."""
    out, i = [], 1
    end = len(raw) - 1
    while i < end:
        c = raw[i]
        if c == "\\" and i + 1 < end:
            nxt = raw[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\",
                        '"': '"', "'": "'"}.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _quote(text: str) -> str:
    body = (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{body}"'


class KsdSave:
    """One KiriKiri save, opened for editing."""

    def __init__(self):
        self.prefix = b""          # the thumbnail bitmap, when there is one
        self.compressed = False
        self.text = ""
        self._values = []          # label, kind, value, start, end

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, raw: bytes) -> None:
        self.prefix, body = self._unwrap(raw)
        try:
            self.text = body.decode("utf-16-le")
        except UnicodeDecodeError as e:
            raise KirikiriError(f"the text is not UTF-16: {e}") from e
        self._values = []
        scan = _Scan(self.text)
        scan.ws()
        self._value(scan, (), 0)
        scan.ws()
        # The invariant the whole format rests on: everything was accounted
        # for. Anything left means a construct this reader does not know, and
        # a save it does not fully understand is one it must not write to.
        if scan.i != scan.n:
            raise KirikiriError(
                f"{scan.n - scan.i} characters left unread at {scan.i}")
        logger.info(f"KiriKiri save: {len(self._values)} values, "
                    f"{'compressed' if self.compressed else 'plain'}"
                    f"{', with a thumbnail' if self.prefix else ''}")

    def _unwrap(self, raw: bytes):
        """The bytes before the text, and the text's own bytes."""
        if raw.startswith(_COMPRESSED_MAGIC):
            if len(raw) < _COMPRESSED_HEADER:
                raise KirikiriError("too short to be a KiriKiri save")
            packed, plain = struct.unpack_from("<QQ", raw, 3 + 2)
            if plain > _MAX_TEXT or packed != len(raw) - _COMPRESSED_HEADER:
                raise KirikiriError("the sizes in the header do not fit the file")
            try:
                body = zlib.decompress(raw[_COMPRESSED_HEADER:])
            except zlib.error as e:
                raise KirikiriError(f"could not decompress: {e}") from e
            if len(body) != plain:
                raise KirikiriError("the save is not the size it says it is")
            self.compressed = True
            return b"", body
        if raw.startswith(_BMP_MAGIC):
            # The thumbnail's own header says how long it is, which is where
            # the text begins. Searching for the mark instead would find it
            # inside the picture.
            if len(raw) < 6:
                raise KirikiriError("too short to be a KiriKiri save")
            at = struct.unpack_from("<I", raw, 2)[0]
            if not (6 <= at < len(raw)) or raw[at:at + 2] != _BOM:
                raise KirikiriError("no save text after the thumbnail")
            return raw[:at], raw[at:]
        if raw.startswith(_BOM):
            return b"", raw
        raise KirikiriError("not a KiriKiri save")

    # ── the walk ─────────────────────────────────────────────────────────────

    def _value(self, c: _Scan, path: tuple, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise KirikiriError("nested too deeply")
        c.ws()
        if c.at("(const)"):
            c.take("(const)")
            c.ws()
        if c.at("%["):
            return self._dict(c, path, depth)
        if c.at("["):
            return self._array(c, path, depth)
        self._scalar(c, path)

    def _dict(self, c: _Scan, path: tuple, depth: int) -> None:
        c.take("%[")
        c.ws()
        while not c.at("]"):
            key = _unquote(c.string())
            c.ws()
            c.take("=>")
            self._value(c, path + (key,), depth + 1)
            c.ws()
            if c.at(","):
                c.take(",")
                c.ws()
        c.take("]")

    def _array(self, c: _Scan, path: tuple, depth: int) -> None:
        c.take("[")
        c.ws()
        index = 0
        while not c.at("]"):
            self._value(c, path + (str(index),), depth + 1)
            index += 1
            c.ws()
            if c.at(","):
                c.take(",")
                c.ws()
        c.take("]")

    def _scalar(self, c: _Scan, path: tuple) -> None:
        mark = c.i
        word = c.word() if (c.i < c.n and (c.s[c.i].isalpha() or c.s[c.i] == "_")) else ""
        if word in _TYPED:
            c.ws()
            start = c.i
            if word == "string":
                raw = c.string()
                kind, value = "str", _unquote(raw)
            else:
                value, real = _number(c.number())
                kind = "float" if (real or word == "real") else "int"
            self._record(path, kind, value, start, self._with_note(c))
            return
        if word in _BARE:
            # void and the like carry nothing to edit, so they are walked
            # past rather than offered — the same as a null anywhere else.
            return
        if word:
            raise KirikiriError(f"unknown word {word!r} at {mark}")
        # A bare literal, with no type in front of it.
        if c.s[c.i] in "\"'":
            start = c.i
            value = _unquote(c.string())
            self._record(path, "str", value, start, self._with_note(c))
            return
        start = c.i
        value, real = _number(c.number())
        self._record(path, "float" if real else "int", value, start,
                     self._with_note(c))

    @staticmethod
    def _with_note(c: _Scan) -> int:
        """Where a value's text really ends.

        A hex real is followed by its decimal in a comment — ``0x1.72p8 /* 370
        */``. Left alone, an edit would change the number and leave the
        comment stating the old one. Counting the comment as part of the value
        means an edit replaces both, and a value nobody touches keeps its
        exact characters either way.
        """
        j = c.i
        while j < c.n and c.s[j] in " \t":
            j += 1
        if c.s.startswith("/*", j):
            shut = c.s.find("*/", j + 2)
            line_end = c.s.find("\n", j)
            # Only a comment on the value's own line belongs to the value.
            if shut > 0 and (line_end < 0 or shut < line_end):
                c.i = shut + 2
                return c.i
        return c.i

    def _record(self, path, kind, value, start, end) -> None:
        # A value sitting at the root is not inside anything, so it belongs to
        # no category. Some games write a save that is one flat list of flags;
        # calling each of those its own group would offer thousands of
        # categories of one value each, which is no grouping at all.
        self._values.append({"label": ".".join(path), "kind": kind,
                             "value": value, "start": start, "end": end,
                             "group": path[0] if len(path) > 1 else ""})

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(i, v["label"], v["kind"], v["value"])
                for i, v in enumerate(self._values)]

    def groups(self) -> list:
        return [v["group"] for v in self._values]

    def set_value(self, index: int, value) -> None:
        rec = self._values[index]
        if rec["kind"] == "str":
            rec["new"] = _quote(value)
        elif rec["kind"] == "int":
            rec["new"] = str(int(value))
        else:
            rec["new"] = repr(float(value))
        rec["value"] = value

    def dump(self) -> bytes:
        text = self.text
        # Back to front, so an edit never moves a span still to be used.
        for rec in sorted((r for r in self._values if "new" in r),
                          key=lambda r: r["start"], reverse=True):
            text = text[:rec["start"]] + rec["new"] + text[rec["end"]:]
        body = text.encode("utf-16-le")
        if not self.compressed:
            return self.prefix + body
        packed = zlib.compress(body, _ZLIB_LEVEL)
        return (_COMPRESSED_MAGIC + _BOM
                + struct.pack("<QQ", len(packed), len(body)) + packed)


def loads(raw: bytes) -> KsdSave:
    save = KsdSave()
    save.load(raw)
    return save
