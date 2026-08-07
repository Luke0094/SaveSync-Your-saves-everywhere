"""RPG Maker 2000 / 2003 save files (``.lsd``), the LCF chunk format.

Built from liblcf (github.com/EasyRPG/liblcf), which is the reference for this
format: the chunk identifiers below are its generated tables, not guesses.

An LCF file is a length-prefixed name followed by a stream of chunks — an id,
a length, and that many bytes — where some chunks are themselves streams of
chunks. Every chunk is kept as the bytes it arrived as; only the ones worth
editing are decoded. Rebuilding therefore reproduces the file exactly, and a
chunk this reader does not understand cannot be damaged by one it does.

Not yet checked against a save from a real game: none of the reference
projects ships one. The byte-exact round trip is what makes that acceptable —
a file this reader gets wrong fails to rebuild and is refused as read-only,
rather than written back broken.
"""
import logging

logger = logging.getLogger(__name__)

HEADER = b"LcfSaveData"

# Top level (liblcf: ChunkSave).
SAVE_SYSTEM = 0x65
SAVE_INVENTORY = 0x6D

# Inside the system chunk (liblcf: ChunkSaveSystem).
SYS_SWITCHES_SIZE = 0x1F
SYS_SWITCHES = 0x20
SYS_VARIABLES_SIZE = 0x21
SYS_VARIABLES = 0x22
SYS_SAVE_COUNT = 0x83

# Inside the inventory chunk (liblcf: ChunkSaveInventory). These are the
# numbers anyone actually opens a save editor to change.
INV_GOLD = 0x15
INV_NAMED = {
    0x15: "gold",
    0x17: "timer1_frames",
    0x1B: "timer2_frames",
    0x20: "battles",
    0x21: "defeats",
    0x22: "escapes",
    0x23: "victories",
    0x29: "turns",
    0x2A: "steps",
}


class LcfError(Exception):
    pass


def read_int(data: bytes, pos: int):
    """liblcf's packed integer: 7 bits per byte, big-endian, high bit set on
    every byte but the last."""
    value = 0
    for _ in range(5):
        if pos >= len(data):
            raise LcfError("truncated LCF integer")
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos
    raise LcfError("LCF integer too long")


def write_int(value: int) -> bytes:
    """The same encoding, and the same treatment of negatives: liblcf writes
    the value as unsigned 32-bit, so -1 becomes five bytes rather than an
    error."""
    v = value & 0xFFFFFFFF
    out = bytearray()
    for i in range(28, -1, -7):
        if v >= (1 << i) or i == 0:
            out.append(((v >> i) & 0x7F) | (0x80 if i > 0 else 0))
    return bytes(out)


def read_chunks(data: bytes):
    """((id, payload)…, terminated) for a chunk stream.

    Whether a stream ends with an explicit zero chunk or simply with its
    parent's length is RECORDED rather than assumed: re-emitting it the other
    way changes the file by a byte, which is the difference between a save
    that rebuilds exactly and one this reader would then refuse to write.
    """
    out = []
    pos = 0
    terminated = False
    while pos < len(data):
        cid, pos = read_int(data, pos)
        if cid == 0:
            terminated = True
            break
        size, pos = read_int(data, pos)
        if pos + size > len(data):
            raise LcfError("chunk runs past the end of the file")
        out.append((cid, data[pos:pos + size]))
        pos += size
    return out, terminated


def write_chunks(chunks: list, terminate: bool = True) -> bytes:
    out = bytearray()
    for cid, payload in chunks:
        out += write_int(cid)
        out += write_int(len(payload))
        out += payload
    if terminate:
        out += write_int(0)
    return bytes(out)


def _int_array(payload: bytes) -> list:
    out, pos = [], 0
    while pos < len(payload):
        v, pos = read_int(payload, pos)
        out.append(v)
    return out


def _write_int_array(values: list) -> bytes:
    return b"".join(write_int(v) for v in values)


class LsdSave:
    """One .lsd, decoded far enough to edit the numbers in it."""

    def __init__(self):
        self._header = b""
        self._chunks = []            # top level, in order
        self._terminated = True      # did the top-level stream end with a 0?
        self._values = []            # dicts describing each editable value

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, data: bytes) -> None:
        length, pos = read_int(data, 0)
        name = data[pos:pos + length]
        if name != HEADER:
            raise LcfError("not an RPG Maker 2000/2003 save")
        self._header = data[:pos + length]
        self._chunks, self._terminated = read_chunks(data[pos + length:])
        self._values = self._collect()
        if not self._values:
            raise LcfError("no editable values found")

    def _collect(self) -> list:
        out = []
        for i, (cid, payload) in enumerate(self._chunks):
            if cid == SAVE_INVENTORY:
                subs, _term = read_chunks(payload)
                for j, (sub, raw) in enumerate(subs):
                    if sub in INV_NAMED:
                        try:
                            value, _ = read_int(raw, 0)
                        except LcfError:
                            continue
                        out.append({"kind": "int", "name": INV_NAMED[sub],
                                    "where": ("inventory", i, j), "value": value})
            elif cid == SAVE_SYSTEM:
                subs, _term = read_chunks(payload)
                for j, (sub, raw) in enumerate(subs):
                    if sub == SYS_VARIABLES:
                        for k, v in enumerate(_int_array(raw)):
                            out.append({"kind": "int", "name": f"variable {k + 1}",
                                        "where": ("variables", i, j, k), "value": v})
                    elif sub == SYS_SWITCHES:
                        for k, v in enumerate(raw):
                            out.append({"kind": "bool", "name": f"switch {k + 1}",
                                        "where": ("switches", i, j, k),
                                        "value": bool(v)})
                    elif sub == SYS_SAVE_COUNT:
                        try:
                            value, _ = read_int(raw, 0)
                        except LcfError:
                            continue
                        out.append({"kind": "int", "name": "save count",
                                    "where": ("system", i, j), "value": value})
        return out

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(i, v["name"], v["kind"], v["value"])
                for i, v in enumerate(self._values)]

    def groups(self) -> list:
        """What each value IS — a switch, a variable, an inventory count, the
        save's own bookkeeping — in the same order as ``values``.

        The reader already knows this, because it is how it finds a value
        again to write it. Handing it out lets the editor show the switches
        apart from the variables instead of one undivided list.
        """
        return [v["where"][0] for v in self._values]

    def set_value(self, index: int, value) -> None:
        v = self._values[index]
        where = v["where"]
        kind = where[0]
        cid, payload = self._chunks[where[1]]
        subs, sub_terminated = read_chunks(payload)
        sub_id, raw = subs[where[2]]

        if kind in ("inventory", "system"):
            raw = write_int(int(value))
        elif kind == "variables":
            arr = _int_array(raw)
            arr[where[3]] = int(value)
            raw = _write_int_array(arr)
        else:                                   # switches
            buf = bytearray(raw)
            buf[where[3]] = 1 if value else 0
            raw = bytes(buf)

        subs[where[2]] = (sub_id, raw)
        self._chunks[where[1]] = (cid, write_chunks(subs, sub_terminated))
        v["value"] = value

    def dump(self) -> bytes:
        return self._header + write_chunks(self._chunks, self._terminated)


def loads(data: bytes) -> LsdSave:
    save = LsdSave()
    save.load(data)
    return save
