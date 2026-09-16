"""AliceSoft's VSF save-slot summary files — the per-slot metadata (date,
scene description, playtime) a game shows in its own load menu, distinct
from the ``GD\\x01\\x01`` GSAVE container ``core.engines.alicesoft`` reads
for full game state. The two sit side by side from the same slot, e.g.
``Save01.asd`` (state) + ``Save01.vsf`` (summary).

Taken from the engine reimplementation the format is actually described in:
nunuhara's xsystem4 (``src/hll/VSFile.c``) — the HLL library a game's own
script calls to read and write ".vsf" files one value at a time. Nothing
here was inferred from staring at bytes: the container is a fixed 8-byte
magic followed by a flat, self-describing stream of TYPE-TAGGED values (a
type byte, then that many bytes of payload), with no length prefix, no
section table, and — critically — no field names anywhere in the file: a
game's own script decides how many values there are and in what order, in
code this reader never sees. So a VSF is read and offered as a plain
ordered list of typed slots rather than as named fields, the same honesty
``core.engines.alicesoft`` already extends to a numbered global whose name
the game's own ``.ain`` cannot supply.
"""
import struct

MAGIC = b"VSF\x00\x00\x00\x00\x00"

TYPE_BYTE, TYPE_INT, TYPE_FLOAT, TYPE_STRING = 0, 1, 2, 3
_KINDS = {TYPE_BYTE: "int", TYPE_INT: "int", TYPE_FLOAT: "float",
          TYPE_STRING: "str"}

# AliceSoft is a Japanese engine and its strings are Shift-JIS throughout —
# same reasoning and same encoding as core.engines.alicesoft.
_ENCODING = "cp932"


class VsfError(Exception):
    pass


def is_vsf(data: bytes) -> bool:
    return data[:len(MAGIC)] == MAGIC


class VsfSave:
    """One VSF file: a flat, ordered list of typed slots."""

    def __init__(self):
        # Each entry is [type, raw] — raw is an int for BYTE/INT, a float
        # for FLOAT, and RAW BYTES (not yet decoded) for STRING, so an
        # untouched string round-trips byte-for-byte even where its bytes
        # would not decode losslessly — see set_value for where a STRING is
        # actually re-encoded, and only there.
        self._slots = []

    def load(self, data: bytes) -> None:
        if not is_vsf(data):
            raise VsfError("not a VSF save")
        pos = len(MAGIC)
        slots = []
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == TYPE_BYTE:
                if pos >= len(data):
                    raise VsfError("the save ends inside a byte value")
                slots.append([t, data[pos]])
                pos += 1
            elif t == TYPE_INT:
                if pos + 4 > len(data):
                    raise VsfError("the save ends inside an int value")
                slots.append([t, struct.unpack_from("<i", data, pos)[0]])
                pos += 4
            elif t == TYPE_FLOAT:
                if pos + 4 > len(data):
                    raise VsfError("the save ends inside a float value")
                slots.append([t, struct.unpack_from("<f", data, pos)[0]])
                pos += 4
            elif t == TYPE_STRING:
                end = data.find(b"\x00", pos)
                if end < 0:
                    raise VsfError(
                        "a string runs to the end of the save with no "
                        "terminator")
                slots.append([t, data[pos:end]])
                pos = end + 1
            else:
                raise VsfError(
                    f"byte {pos - 1} claims value type {t}, which this "
                    f"reader does not know")
        if not slots:
            raise VsfError("this save holds no values")
        self._slots = slots

    def dump(self) -> bytes:
        out = bytearray(MAGIC)
        for t, raw in self._slots:
            out.append(t)
            if t == TYPE_BYTE:
                out.append(raw)
            elif t == TYPE_INT:
                out += struct.pack("<i", raw)
            elif t == TYPE_FLOAT:
                out += struct.pack("<f", raw)
            else:
                out += raw + b"\x00"
        return bytes(out)

    def values(self) -> list:
        """(index, kind, value) for every slot, decoded for display."""
        out = []
        for i, (t, raw) in enumerate(self._slots):
            value = raw.decode(_ENCODING, errors="replace") \
                if t == TYPE_STRING else raw
            out.append((i, _KINDS[t], value))
        return out

    def set_value(self, index: int, value) -> None:
        t, _raw = self._slots[index]
        if t == TYPE_BYTE:
            new = int(value)
            if not 0 <= new <= 255:
                raise VsfError("this value is a single byte and must be "
                               "0-255")
        elif t == TYPE_INT:
            new = int(value)
        elif t == TYPE_FLOAT:
            new = float(value)
        else:
            try:
                new = str(value).encode(_ENCODING)
            except UnicodeEncodeError as e:
                raise VsfError(
                    "AliceSoft saves are written in Shift-JIS and this "
                    "text cannot be spelled in it") from e
        self._slots[index][1] = new


def loads(data: bytes) -> VsfSave:
    save = VsfSave()
    save.load(data)
    return save
