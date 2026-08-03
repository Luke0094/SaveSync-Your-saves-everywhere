"""AliceSoft System 4 saves — the "GD" global-data files.

Taken from the engine reimplementation the format is actually described in:
nunuhara's libsys4 (``src/savefile.c``, ``include/system4/savefile.h``) and
the ``ain_data_type`` table in ``include/system4/ain.h``. alice-tools' ``asd
dump`` / ``asd build`` are the same code. Nothing here was inferred from
looking at bytes: every field, every order, every version difference below is
in that source, and the byte-level reading only confirmed it.

An AliceSoft save is a small container — ``GD\\x01\\x01``, the uncompressed
length, then a deflate stream — around one of TWO different things:

- **GSAVE**, the game's global data: named, typed variables, the arrays and
  strings they point at, and structs described by a table of field names.
  This is what the module reads and writes.
- **RSAVE**, which opens ``RSM``: a dump of the virtual machine — its stack,
  its call frames, its heap. That is the numbered save slots, it is a far
  larger structure, and it is reported rather than opened.

Both wear ``.asd``, and both also arrive as ``.sav``, so which one a file
holds can only be told after the deflate stream is unpacked.

**Why the walk can be trusted.** GSAVE stores the offset of each of its five
sections in its header, and the reader must arrive at each one exactly. That
is the same guarantee core/qsp gets from consuming every line: a structure
misread by even one field lands somewhere else and is refused, rather than
producing plausible values that would be written back to the wrong places.
"""
import logging
import struct
import zlib

logger = logging.getLogger(__name__)

MAGIC = b"GD\x01\x01"
_RSAVE_MAGIC = b"RSM\x00"
# A third container, holding the same kind of payload as GSAVE behind a
# different header: magic, a word that is always zero here, then the sizes
# before and after packing. The gallery and music-room files use it.
_PSR_MAGIC = b"PSR\x00"
_PSR_HEADER = 16
# RSAVE heap object tags, from savefile.c.
_H_GLOBALS, _H_LOCALS, _H_STRING = 0, 1, 2
_H_ARRAY, _H_STRUCT, _H_DELEGATE, _H_NULL = 3, 4, 5, -1
# The versions savefile.c accepts. Anything else is a format this reader has
# not been shown and must not guess at.
_RSAVE_VERSIONS = (4, 6, 7, 9)
# The game's own compiled code, which is where the global variables' NAMES
# live — the save holds only their values and types, in order. See _AinGlobals.
_AIN_MAGIC = b"AI2\x00"
_AIN_HEADER = 16
_AIN_GLOB = b"GLOB"
# A global record: its name, then its type, the struct it belongs to, its
# array rank, and the group it is filed under. Four words, and reading three
# of them looks right for a while — the parse re-syncs on the next name — up
# to the first group number containing a zero byte.
_AIN_GLOB_FIELDS = 4
# Sanity bounds for a section found by its tag rather than by walking to it.
_AIN_MIN_GLOBALS = 1
_AIN_MAX_GLOBALS = 100000
_AIN_MAX_NAME = 512
# savefile.c: an encrypted save opens 0x1a and is XORed with an MT19937
# keystream. None is implemented here — see loads().
_ENCRYPTED = 0x1A
# savefile.c reads the level back off the deflate stream so that rewriting a
# save reproduces it. Anything else is written at zlib's own default.
_LEVELS = {0x01: 1, 0xDA: 9}
_DEFAULT_LEVEL = -1

# savefile.h
_EMPTY_STRING = 0x7FFFFFFF
_RECORD_GLOBALS = 1000
# ain.h. Only the types a value can actually BE are named; the rest are
# reference and container types, and appear below only as sets.
AIN_VOID, AIN_INT, AIN_FLOAT, AIN_STRING, AIN_STRUCT = 0, 10, 11, 12, 13
AIN_FUNC_TYPE, AIN_BOOL, AIN_LONG_INT, AIN_DELEGATE = 27, 47, 55, 63
# ain.h, macro AIN_ARRAY_TYPE
_ARRAY_TYPES = frozenset({14, 15, 16, 17, 30, 50, 58, 66})
# ain.h, macro AIN_REF_TYPE
_REF_TYPES = frozenset({18, 19, 20, 21, 22, 23, 24, 25, 31, 32, 51, 52, 59,
                        60, 67, 69, 80})
# savefile.c, gsave_validate_value: these carry their value directly and are
# not an index into anything.
_IMMEDIATE = frozenset({AIN_VOID, AIN_INT, AIN_BOOL, AIN_FUNC_TYPE,
                        AIN_DELEGATE, AIN_LONG_INT, AIN_FLOAT}) | _REF_TYPES

# What a value is offered AS. Anything not here is structure rather than a
# value, and is walked into instead of shown.
_KINDS = {AIN_INT: "int", AIN_LONG_INT: "int", AIN_BOOL: "bool",
          AIN_FLOAT: "float", AIN_STRING: "str"}

# AliceSoft is a Japanese engine and its strings are Shift-JIS throughout —
# the names of the variables included, not just their contents, so a global
# is as likely to be named in Japanese as in English.
_ENCODING = "cp932"
# How deep to follow a struct into a struct. The samples go three or four
# levels; past this it is the engine's own object graph.
_MAX_DEPTH = 8


class AliceSoftError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def int32(self) -> int:
        try:
            value = struct.unpack_from("<i", self.data, self.pos)[0]
        except struct.error as e:
            raise AliceSoftError("the save ends inside a value") from e
        self.pos += 4
        return value

    def cstring(self) -> bytes:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise AliceSoftError("a name runs to the end of the save")
        out = self.data[self.pos:end]
        self.pos = end + 1
        return out

    def raw(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise AliceSoftError("the save ends inside a block of bytes")
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def int_array(self) -> list:
        return [self.int32() for _ in range(self._count())]

    def string_array(self) -> list:
        return [self.cstring() for _ in range(self._count())]

    def _count(self) -> int:
        n = self.int32()
        # A count is a length, and the bytes it promises have to exist. A
        # negative or absurd one is a misread structure, and catching it here
        # names the problem instead of asking for a gigabyte of nothing.
        if n < 0 or self.pos + n > len(self.data):
            raise AliceSoftError(f"a count of {n} items cannot be right here")
        return n

    def expect(self, offset: int, what: str) -> None:
        """The check the whole reader rests on — see the module docstring."""
        if self.pos != offset:
            raise AliceSoftError(
                f"the {what} should start at byte {offset} and the walk "
                f"reached {self.pos} — this is not the structure this reader "
                f"knows")


class _Writer:
    def __init__(self):
        self.data = bytearray()

    def int32(self, value: int) -> None:
        self.data += struct.pack("<i", value)

    def cstring(self, raw: bytes) -> None:
        self.data += raw + b"\0"

    def raw(self, block: bytes) -> None:
        self.data += block

    def int_array(self, values) -> None:
        self.int32(len(values))
        for value in values:
            self.int32(value)

    def string_array(self, values) -> None:
        self.int32(len(values))
        for value in values:
            self.cstring(value)

    def hole(self) -> int:
        """Room for an offset that is only known once the section is written."""
        where = len(self.data)
        self.int32(0)
        return where

    def fill(self, where: int) -> None:
        struct.pack_into("<i", self.data, where, len(self.data))


class GSave:
    """The global data of one AliceSoft save, read whole."""

    def __init__(self):
        self.key = b""
        self.uk1 = 0
        self.version = 0
        self.uk2 = 0
        self.nr_ain_globals = 0
        self.group = b""
        self.records = []
        self.globals = []
        self.strings = []
        self.arrays = []
        self.keyvals = []
        self.struct_defs = []
        self.trailing = b""

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, buf: bytes) -> None:
        r = _Reader(buf)
        self.key = r.cstring()
        self.uk1 = r.int32()
        self.version = r.int32()
        if self.version not in (4, 5, 7):
            raise AliceSoftError(
                f"this is a version {self.version} save and the format is "
                f"only described for 4, 5 and 7")
        self.uk2 = r.int32()
        self.nr_ain_globals = r.int32()
        counts, offsets = {}, {}
        for name in ("records", "globals", "strings", "arrays", "keyvals"):
            offsets[name] = r.int32()
            counts[name] = r.int32()
        if self.version >= 5:
            self.group = r.cstring()

        r.expect(offsets["records"], "records")
        for _ in range(counts["records"]):
            self.records.append(self._read_record(r))

        r.expect(offsets["globals"], "globals")
        for _ in range(counts["globals"]):
            g = {"type": r.int32(), "value": r.int32(), "name": r.cstring()}
            if self.version <= 5:
                g["unknown"] = r.int32()
            self.globals.append(g)

        r.expect(offsets["strings"], "strings")
        self.strings = [r.cstring() for _ in range(counts["strings"])]

        r.expect(offsets["arrays"], "arrays")
        for _ in range(counts["arrays"]):
            self.arrays.append(self._read_array(r))

        r.expect(offsets["keyvals"], "key-values")
        for _ in range(counts["keyvals"]):
            if self.version <= 5:
                self.keyvals.append({"type": r.int32(), "value": r.int32(),
                                     "name": r.cstring()})
            else:
                # From version 7 a key-value is only its value: what it is
                # called and what type it has come from the struct definition
                # of the record that points at it.
                self.keyvals.append({"value": r.int32()})

        if self.version >= 7:
            for _ in range(r.int32()):
                sd = {"name": r.cstring(), "fields": []}
                for _f in range(r.int32()):
                    sd["fields"].append({"type": r.int32(),
                                         "name": r.cstring()})
                self.struct_defs.append(sd)
        self.trailing = buf[r.pos:]
        self._validate()

    def _read_record(self, r: _Reader) -> dict:
        rec = {}
        if self.version <= 5:
            rec["type"] = r.int32()
            rec["struct_name"] = r.cstring()
        else:
            rec["struct_index"] = r.int32()
            rec["type"] = (_RECORD_GLOBALS if rec["struct_index"] == -1
                           else AIN_STRUCT)
        rec["indices"] = [r.int32() for _ in range(r.int32())]
        return rec

    def _read_array(self, r: _Reader) -> dict:
        a = {"rank": r.int32()}
        a["dimensions"] = [r.int32() for _ in range(max(0, a["rank"]))]
        expected = 0
        if a["rank"] > 0:
            expected = 1
            for size in a["dimensions"][1:]:
                expected *= size
        a["flat"] = []
        count = r.int32()
        if count != expected:
            raise AliceSoftError(
                f"an array says it holds {count} rows where its own "
                f"dimensions need {expected}")
        for _ in range(count):
            fa = {"nr_values": r.int32()}
            if fa["nr_values"] != a["dimensions"][0]:
                raise AliceSoftError("an array row is not the length its "
                                     "dimensions give it")
            fa["type"] = r.int32() if self.version >= 7 else None
            values = []
            for _v in range(fa["nr_values"]):
                value = r.int32()
                kind = fa["type"] if self.version >= 7 else r.int32()
                values.append([value, kind])
            fa["values"] = values
            a["flat"].append(fa)
        return a

    def _validate(self) -> None:
        """savefile.c's gsave_validate_value, over everything that was read.

        A value whose type says "index into the strings" and whose number is
        past the end of them means the walk went wrong somewhere it happened
        to survive, and the file must not be written back.
        """
        def ok(value, kind):
            if kind in _IMMEDIATE:
                return True
            if kind == AIN_STRING:
                return 0 <= value < len(self.strings) or value == _EMPTY_STRING
            if kind == AIN_STRUCT:
                return 0 <= value < len(self.records)
            if kind in _ARRAY_TYPES:
                return 0 <= value < len(self.arrays)
            return False

        for g in self.globals:
            if not ok(g["value"], g["type"]):
                raise AliceSoftError(
                    f"global {g['name'].decode(_ENCODING, 'replace')!r} points "
                    f"outside the save")
        for a in self.arrays:
            for fa in a["flat"]:
                for value, kind in fa["values"]:
                    if not ok(value, kind):
                        raise AliceSoftError("an array value points outside "
                                             "the save")

    # ── writing ──────────────────────────────────────────────────────────────

    def dump(self) -> bytes:
        w = _Writer()
        w.cstring(self.key)
        w.int32(self.uk1)
        w.int32(self.version)
        w.int32(self.uk2)
        w.int32(self.nr_ain_globals)
        holes = {}
        for name, rows in (("records", self.records), ("globals", self.globals),
                           ("strings", self.strings), ("arrays", self.arrays),
                           ("keyvals", self.keyvals)):
            holes[name] = w.hole()
            w.int32(len(rows))
        if self.version >= 5:
            w.cstring(self.group)

        w.fill(holes["records"])
        for rec in self.records:
            if self.version <= 5:
                w.int32(rec["type"])
                w.cstring(rec["struct_name"])
            else:
                w.int32(rec["struct_index"])
            w.int32(len(rec["indices"]))
            for index in rec["indices"]:
                w.int32(index)

        w.fill(holes["globals"])
        for g in self.globals:
            w.int32(g["type"])
            w.int32(g["value"])
            w.cstring(g["name"])
            if self.version <= 5:
                w.int32(g["unknown"])

        w.fill(holes["strings"])
        for s in self.strings:
            w.cstring(s)

        w.fill(holes["arrays"])
        for a in self.arrays:
            w.int32(a["rank"])
            for size in a["dimensions"]:
                w.int32(size)
            w.int32(len(a["flat"]))
            for fa in a["flat"]:
                w.int32(fa["nr_values"])
                if self.version >= 7:
                    w.int32(fa["type"])
                for value, kind in fa["values"]:
                    w.int32(value)
                    if self.version <= 5:
                        w.int32(kind)

        w.fill(holes["keyvals"])
        for kv in self.keyvals:
            if self.version <= 5:
                w.int32(kv["type"])
            w.int32(kv["value"])
            if self.version <= 5:
                w.cstring(kv["name"])

        if self.version >= 7:
            w.int32(len(self.struct_defs))
            for sd in self.struct_defs:
                w.cstring(sd["name"])
                w.int32(len(sd["fields"]))
                for fd in sd["fields"]:
                    w.int32(fd["type"])
                    w.cstring(fd["name"])
        return bytes(w.data) + self.trailing


def _text(raw: bytes) -> str:
    return raw.decode(_ENCODING, errors="replace")


def _as_value(raw: int, kind: int, strings: list):
    if kind == AIN_FLOAT:
        return struct.unpack("<f", struct.pack("<i", raw))[0]
    if kind == AIN_BOOL:
        return bool(raw)
    if kind == AIN_STRING:
        if raw == _EMPTY_STRING or not (0 <= raw < len(strings)):
            return ""
        return _text(strings[raw])
    return raw


class RSave:
    """A numbered save slot: a dump of the running virtual machine.

    Where a GSAVE is a list of named values, this is the interpreter's whole
    state — its stack, its call frames, and a heap of tens of thousands of
    objects. Written from savefile.c, and held to the same bargain as the
    rest of this module: every byte is accounted for, and what is read back
    out is the file that came in.

    Only the global frame is offered for editing. The heap around it is the
    engine's own bookkeeping — object references, strings shared between
    them, structures whose meaning is in code — and a value changed there is
    as likely to break the save as to help. The globals are the game's own
    variables, which is what anyone opening a save came for.
    """

    def __init__(self):
        self.version = 0
        self.key = b""
        self.comments = None
        self.comments_only = False
        self.body = {}
        self.heap = []
        self.func_names = None

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, buf: bytes) -> None:
        r = _Reader(buf)
        if r.raw(4) != _RSAVE_MAGIC:
            raise AliceSoftError("not a numbered save slot")
        self.version = r.int32()
        if self.version not in _RSAVE_VERSIONS:
            raise AliceSoftError(
                f"this slot is version {self.version}, and only "
                f"{', '.join(str(v) for v in _RSAVE_VERSIONS)} are known")
        self.key = r.cstring()
        if self.version >= 7:
            self.comments = r.string_array()
        if r.pos == len(buf):
            # A slot written before the game had anything to remember.
            self.comments_only = True
            return
        b = self.body
        b["ip"] = self._return_record(r)
        b["uk1"] = r.int32()
        b["stack"] = r.int_array()
        # savefile.c pairs these three up into call frames as it reads. They
        # are kept as they lie instead: pairing them and taking them apart
        # again would have to reproduce which frames carried a struct
        # pointer, and nothing here needs the frames to be understood.
        b["local_ptrs"] = r.int_array()
        b["frame_types"] = r.int_array()
        b["struct_ptrs"] = r.int_array()
        b["returns"] = [self._return_record(r) for _ in range(r._count())]
        b["uk2"], b["uk3"], b["uk4"] = r.int32(), r.int32(), r.int32()
        b["next_seq"] = r.int32() if self.version >= 9 else None
        self.heap = [self._heap_object(r) for _ in range(r._count())]
        if self.version >= 6:
            self.func_names = r.string_array()
        r.expect(len(buf), "end of the slot")

    def _return_record(self, r: _Reader) -> dict:
        addr = r.int32()
        if addr == -1:
            return {"addr": addr}
        return {"addr": addr, "caller": r.cstring(),
                "local": r.int32(), "crc": r.int32()}

    def _symbol(self, r: _Reader):
        """A name, or the number that stood for one before version 6."""
        return r.int32() if self.version == 4 else r.cstring()

    def _slots(self, r: _Reader) -> list:
        """A run of values given as its size in bytes, not its count."""
        size = r.int32()
        if size < 0 or size % 4:
            raise AliceSoftError(
                f"a block of values is {size} bytes, which is not a whole "
                f"number of them")
        return [r.int32() for _ in range(size // 4)]

    def _heap_object(self, r: _Reader) -> dict:
        tag = r.int32()
        h = {"tag": tag}
        if tag == _H_NULL:
            return h
        if tag not in (_H_GLOBALS, _H_LOCALS, _H_STRING, _H_ARRAY,
                       _H_STRUCT, _H_DELEGATE):
            raise AliceSoftError(f"a heap object is of unknown kind {tag}")
        h["ref"] = r.int32()
        if self.version >= 9:
            h["seq"] = r.int32()
        if tag in (_H_GLOBALS, _H_LOCALS):
            if self.version == 4 or tag == _H_GLOBALS:
                h["func"] = r.int32()
            else:
                h["func"] = r.cstring()
            h["types"] = r.int_array()
            if tag == _H_LOCALS and self.version >= 9:
                h["struct_ptr"] = r.int32()
            h["slots"] = self._slots(r)
        elif tag == _H_STRING:
            h["uk"] = r.int32()
            h["text"] = r.raw(r._count())
        elif tag == _H_ARRAY:
            h["rank_minus_1"] = r.int32()
            h["data_type"] = r.int32()
            h["struct_type"] = self._symbol(r)
            h["root_rank"] = r.int32()
            h["is_not_empty"] = r.int32()
            h["slots"] = self._slots(r)
        elif tag == _H_STRUCT:
            h["ctor"] = self._symbol(r)
            h["dtor"] = self._symbol(r)
            h["uk"] = r.int32()
            h["struct_type"] = self._symbol(r)
            h["types"] = r.int_array()
            h["slots"] = self._slots(r)
        else:                                   # delegate
            h["slots"] = self._slots(r)
        return h

    # ── writing ──────────────────────────────────────────────────────────────

    def dump(self) -> bytes:
        w = _Writer()
        w.raw(_RSAVE_MAGIC)
        w.int32(self.version)
        w.cstring(self.key)
        if self.version >= 7:
            w.string_array(self.comments)
        if self.comments_only:
            return bytes(w.data)
        b = self.body
        self._put_return(w, b["ip"])
        w.int32(b["uk1"])
        w.int_array(b["stack"])
        w.int_array(b["local_ptrs"])
        w.int_array(b["frame_types"])
        w.int_array(b["struct_ptrs"])
        w.int32(len(b["returns"]))
        for record in b["returns"]:
            self._put_return(w, record)
        w.int32(b["uk2"])
        w.int32(b["uk3"])
        w.int32(b["uk4"])
        if self.version >= 9:
            w.int32(b["next_seq"])
        w.int32(len(self.heap))
        for h in self.heap:
            self._put_heap(w, h)
        if self.version >= 6:
            w.string_array(self.func_names)
        return bytes(w.data)

    def _put_return(self, w: _Writer, record: dict) -> None:
        w.int32(record["addr"])
        if record["addr"] == -1:
            return
        w.cstring(record["caller"])
        w.int32(record["local"])
        w.int32(record["crc"])

    def _put_symbol(self, w: _Writer, value) -> None:
        (w.int32 if self.version == 4 else w.cstring)(value)

    def _put_slots(self, w: _Writer, slots) -> None:
        w.int32(len(slots) * 4)
        for value in slots:
            w.int32(value)

    def _put_heap(self, w: _Writer, h: dict) -> None:
        tag = h["tag"]
        w.int32(tag)
        if tag == _H_NULL:
            return
        w.int32(h["ref"])
        if self.version >= 9:
            w.int32(h["seq"])
        if tag in (_H_GLOBALS, _H_LOCALS):
            if self.version == 4 or tag == _H_GLOBALS:
                w.int32(h["func"])
            else:
                w.cstring(h["func"])
            w.int_array(h["types"])
            if tag == _H_LOCALS and self.version >= 9:
                w.int32(h["struct_ptr"])
            self._put_slots(w, h["slots"])
        elif tag == _H_STRING:
            w.int32(h["uk"])
            w.int32(len(h["text"]))
            w.raw(h["text"])
        elif tag == _H_ARRAY:
            w.int32(h["rank_minus_1"])
            w.int32(h["data_type"])
            self._put_symbol(w, h["struct_type"])
            w.int32(h["root_rank"])
            w.int32(h["is_not_empty"])
            self._put_slots(w, h["slots"])
        elif tag == _H_STRUCT:
            self._put_symbol(w, h["ctor"])
            self._put_symbol(w, h["dtor"])
            w.int32(h["uk"])
            self._put_symbol(w, h["struct_type"])
            w.int_array(h["types"])
            self._put_slots(w, h["slots"])
        else:
            self._put_slots(w, h["slots"])

    # ── what can be edited ───────────────────────────────────────────────────

    def globals_frame(self) -> dict:
        """The one frame holding the game's own variables, or None."""
        for h in self.heap:
            if h["tag"] == _H_GLOBALS:
                return h
        return None


def global_names(game_dir, expect_types=None) -> list:
    """The names of a game's global variables, read from its own code.

    A slot records what its globals are worth and what type each one is, in
    order, and nothing about what they are called: the names live in the
    ``.ain`` the game runs. So this is the same shape as an encrypted save
    whose key is in the game — the save alone can only offer numbered
    values, and with the game in the library it can name every one of them.

    *expect_types* is the list of types the save itself carries. A section is
    only accepted when it lists exactly as many globals with exactly those
    types, which is what says the game and the save are the same build. It
    also settles the one way this could go quietly wrong: the section is
    found by its tag, and those four letters can occur inside the compiled
    code by chance, so a run of bytes that merely PARSES is not enough.

    Returns [(name, type)], or [] when the game is not to hand.
    """
    from pathlib import Path
    root = Path(game_dir) if game_dir else None
    if root is None or not root.is_dir():
        return []
    for ain in sorted(root.glob("*.ain")):
        try:
            names = _read_ain_globals(ain, expect_types)
        except (OSError, AliceSoftError, zlib.error, struct.error) as e:
            logger.debug(f"AliceSoft: {ain.name} gave no names ({e})")
            continue
        if names:
            logger.info(f"AliceSoft: {len(names)} global names from {ain.name}")
            return names
    return []


def _read_ain_globals(path, expect_types=None) -> list:
    raw = path.read_bytes()
    if raw[:4] != _AIN_MAGIC:
        return []
    body = zlib.decompress(raw[_AIN_HEADER:])
    # The sections before GLOB are walked past by name in the engine's own
    # reader; here the tag is searched for instead. Walking would mean
    # decoding all sixteen thousand functions — a whole second format, read
    # only to be thrown away. Every hit is tried, not just the first, since
    # the first may be four bytes that happen to spell the tag.
    start = 0
    while True:
        at = body.find(_AIN_GLOB, start)
        if at < 0:
            return []
        names = _try_globals(body, at)
        if names and (expect_types is None
                      or [k for _n, k in names] == list(expect_types)):
            return names
        start = at + 1


def _try_globals(body: bytes, at: int) -> list:
    """Read a GLOB section at *at*, or [] if that is not what is there."""
    if at + 8 > len(body):
        return []
    count = struct.unpack_from("<i", body, at + 4)[0]
    if not _AIN_MIN_GLOBALS <= count <= _AIN_MAX_GLOBALS:
        return []
    pos = at + 8
    out = []
    for _ in range(count):
        end = body.find(b"\0", pos)
        if end < 0 or end - pos > _AIN_MAX_NAME:
            return []
        name = body[pos:end]
        pos = end + 1
        if pos + 4 * _AIN_GLOB_FIELDS > len(body):
            return []
        kind = struct.unpack_from("<i", body, pos)[0]
        pos += 4 * _AIN_GLOB_FIELDS
        out.append((_text(name), kind))
    return out


class AliceSave:
    """One AliceSoft save, opened for editing."""

    def __init__(self):
        self.gsave = GSave()
        # Set when the file turned out to be a numbered slot instead. The two
        # hold different things and only one is ever in use.
        self.rsave = None
        # Where the game is installed, when it is known. A numbered slot can
        # only put names to its values with the game's own code to read them
        # from — see global_names.
        self.game_dir = None
        self._container = MAGIC
        self._rstrings = []
        self._level = _DEFAULT_LEVEL
        self._slots = []      # where each offered value lives
        self._rows = []       # (name, kind, group) as shown
        # The engine type each offered value was read AS, decided while the
        # save is walked and kept. From version 7 a key-value carries no type
        # of its own — it is the struct definition of the record pointing at
        # it that says what it is — so working the type out again afterwards
        # means asking a table that has only one answer per key-value. Two
        # records CAN point at one, and then that answer is the wrong one for
        # one of them. Deciding once, where the record is in hand, cannot go
        # wrong that way.
        self._types = []

    # ── the values worth showing ─────────────────────────────────────────────

    def _collect(self) -> None:
        gs = self.gsave
        seen_records, seen_arrays = set(), set()

        def add(slot, name, kind, group):
            self._slots.append(slot)
            self._types.append(kind)
            self._rows.append((name, _KINDS[kind], group))

        def walk_array(index, name, group, depth):
            if index in seen_arrays or depth > _MAX_DEPTH:
                return
            seen_arrays.add(index)
            a = gs.arrays[index]
            for f, fa in enumerate(a["flat"]):
                for i, (value, kind) in enumerate(fa["values"]):
                    label = f"{name}[{i}]" if len(a["flat"]) == 1 \
                        else f"{name}[{f}][{i}]"
                    if kind in _KINDS:
                        add(("array", index, f, i), label, kind, group)
                    elif kind == AIN_STRUCT:
                        walk_record(value, label, group, depth + 1)
                    elif kind in _ARRAY_TYPES:
                        walk_array(value, label, group, depth + 1)

        def walk_record(index, name, group, depth):
            if index in seen_records or depth > _MAX_DEPTH:
                return
            if not 0 <= index < len(gs.records):
                return
            seen_records.add(index)
            rec = gs.records[index]
            fields = self._fields_of(rec)
            for j, (fname, kind) in enumerate(fields):
                if j >= len(rec["indices"]):
                    break
                kv = rec["indices"][j]
                label = f"{name}.{fname}"
                if kind in _KINDS:
                    add(("keyval", kv), label, kind, group)
                elif kind == AIN_STRUCT:
                    walk_record(gs.keyvals[kv]["value"], label, group, depth + 1)
                elif kind in _ARRAY_TYPES:
                    walk_array(gs.keyvals[kv]["value"], label, group, depth + 1)

        for i, g in enumerate(gs.globals):
            name, kind, value = _text(g["name"]), g["type"], g["value"]
            if kind in _KINDS:
                add(("global", i), name, kind, "Globals")
            elif kind == AIN_STRUCT:
                walk_record(value, name, name, 1)
            elif kind in _ARRAY_TYPES:
                walk_array(value, name, name, 1)

    def _fields_of(self, rec: dict) -> list:
        """(name, type) of each field of *rec*, from wherever it is described.

        From version 7 a key-value carries only its number; the name and the
        type live in the struct definition table, matched by the record's
        struct index. Before that each key-value carried its own.
        """
        gs = self.gsave
        if gs.version <= 5:
            return [(_text(gs.keyvals[k]["name"]), gs.keyvals[k]["type"])
                    for k in rec["indices"] if 0 <= k < len(gs.keyvals)]
        si = rec.get("struct_index", -1)
        if not 0 <= si < len(gs.struct_defs):
            return []
        return [(_text(f["name"]), f["type"])
                for f in gs.struct_defs[si]["fields"]]

    def _raw_at(self, slot) -> int:
        """The number one offered slot holds, wherever it is kept."""
        gs = self.gsave
        if slot[0] == "rglobal":
            return self.rsave.globals_frame()["slots"][slot[1]]
        if slot[0] == "global":
            return gs.globals[slot[1]]["value"]
        if slot[0] == "keyval":
            return gs.keyvals[slot[1]]["value"]
        _tag, array, flat, i = slot
        return gs.arrays[array]["flat"][flat]["values"][i][0]

    def _put_raw(self, slot, value: int) -> None:
        gs = self.gsave
        if slot[0] == "rglobal":
            self.rsave.globals_frame()["slots"][slot[1]] = value
        elif slot[0] == "global":
            gs.globals[slot[1]]["value"] = value
        elif slot[0] == "keyval":
            gs.keyvals[slot[1]]["value"] = value
        else:
            _tag, array, flat, i = slot
            gs.arrays[array]["flat"][flat]["values"][i][0] = value

    # ── the interface the editor uses ────────────────────────────────────────

    def load(self, data: bytes) -> None:
        buf = self._unpack(data)
        if buf[:4] == _RSAVE_MAGIC:
            self.rsave = RSave()
            self.rsave.load(buf)
            self._collect_globals()
            if not self._rows:
                raise AliceSoftError(
                    "this slot holds no global variables to edit")
            return
        try:
            self.gsave.load(buf)
        except AliceSoftError:
            if self._container == _PSR_MAGIC:
                # The gallery and music-room files: a count and then a run of
                # numbers saying which pictures and tracks have been seen.
                # What each number means is the game's business, written in
                # its script and nowhere in the file, so there is nothing
                # here to put a name against.
                raise AliceSoftError(
                    "this is the gallery list, which is a run of numbers "
                    "with nothing in the file to say what any of them "
                    "unlocks") from None
            raise
        self._collect()
        if not self._rows:
            raise AliceSoftError("this save holds no values that can be named")

    def _unpack(self, data: bytes) -> bytes:
        """The payload inside whichever container this file uses."""
        if data[:4] == _PSR_MAGIC:
            if len(data) < _PSR_HEADER:
                raise AliceSoftError("the save is too short to hold anything")
            self._container = _PSR_MAGIC
            raw_size, packed_size = struct.unpack_from("<II", data, 8)
            payload = data[_PSR_HEADER:]
            if len(payload) != packed_size:
                raise AliceSoftError(
                    f"the save says it packs into {packed_size} bytes and it "
                    f"holds {len(payload)}")
        elif data[:4] == MAGIC:
            if len(data) < 10:
                raise AliceSoftError("the save is too short to hold anything")
            self._container = MAGIC
            raw_size = struct.unpack_from("<I", data, 4)[0]
            payload = data[8:]
        else:
            raise AliceSoftError("not an AliceSoft save")
        if payload[:1] == bytes([_ENCRYPTED]):
            raise AliceSoftError(
                "this save is encrypted, and SaveSync does not unscramble it")
        self._level = _LEVELS.get(payload[1], _DEFAULT_LEVEL)
        try:
            buf = zlib.decompress(payload)
        except zlib.error as e:
            raise AliceSoftError(f"the save will not unpack: {e}") from e
        if len(buf) != raw_size:
            raise AliceSoftError(
                f"the save says it unpacks to {raw_size} bytes and it "
                f"unpacked to {len(buf)}")
        return buf

    def _collect_globals(self) -> None:
        """Offer the game's own variables, named where the game can name them.

        The slot gives a type for every global and a value for every global,
        in the order the game declared them. The names come from the game's
        code, and are only used when it lists exactly as many globals with
        exactly the same types — the one check that says the two are talking
        about the same build. Mismatched, the values are still offered, by
        number: a save is editable with the game uninstalled, just less
        legibly.
        """
        frame = self.rsave.globals_frame()
        if frame is None:
            return
        types, slots = frame["types"], frame["slots"]
        self._rstrings = [
            h["text"].split(b"\0")[0] if h["tag"] == _H_STRING else b""
            for h in self.rsave.heap]
        named = global_names(self.game_dir, expect_types=types)
        group = "Globals" if named else "Globals (unnamed)"
        for i, kind in enumerate(types):
            if kind not in _KINDS or i >= len(slots):
                continue
            name = named[i][0] if named else f"global[{i}]"
            self._slots.append(("rglobal", i))
            self._types.append(kind)
            self._rows.append((name, _KINDS[kind], group))

    def dump(self) -> bytes:
        body = self.rsave.dump() if self.rsave else self.gsave.dump()
        packed = zlib.compress(body, self._level)
        if self._container == _PSR_MAGIC:
            return (_PSR_MAGIC + struct.pack("<III", 0, len(body), len(packed))
                    + packed)
        return MAGIC + struct.pack("<I", len(body)) + packed

    def values(self) -> list:
        strings = self._rstrings if self.rsave else self.gsave.strings
        out = []
        for i, (name, kind, _group) in enumerate(self._rows):
            raw = self._raw_at(self._slots[i])
            out.append((i, name, kind,
                        _as_value(raw, self._types[i], strings)))
        return out

    def groups(self) -> list:
        return [group for _n, _k, group in self._rows]

    def set_value(self, index: int, value) -> None:
        slot = self._slots[index]
        raw, kind = self._raw_at(slot), self._types[index]
        if kind == AIN_FLOAT:
            new = struct.unpack("<i", struct.pack("<f", float(value)))[0]
        elif kind == AIN_BOOL:
            new = 1 if value else 0
        elif kind == AIN_STRING:
            try:
                raw_text = str(value).encode(_ENCODING)
            except UnicodeEncodeError as e:
                raise AliceSoftError(
                    "AliceSoft saves are written in Shift-JIS and this text "
                    "cannot be spelled in it") from e
            if self.rsave is not None:
                # A slot's string is an object on the heap, and the number in
                # the variable says which. Same reasoning as below: the text
                # is replaced where it lies rather than the variable being
                # pointed somewhere new. The terminator the engine wrote is
                # put back, since the length counts it.
                heap = self.rsave.heap
                if not (0 <= raw < len(heap) and heap[raw]["tag"] == _H_STRING):
                    raise AliceSoftError(
                        "this text is not stored where the save says it is")
                ends_null = heap[raw]["text"].endswith(b"\0")
                heap[raw]["text"] = raw_text + (b"\0" if ends_null else b"")
                self._rstrings[raw] = raw_text
                return
            if 0 <= raw < len(self.gsave.strings):
                # The number stays where it is and the text it points at is
                # replaced. Two variables genuinely CAN share one entry, and
                # repointing only one of them is a change this cannot
                # describe — so both would read the new text, which is what
                # sharing an entry means.
                self.gsave.strings[raw] = raw_text
                return
            # An empty one points at no entry at all, so there is nothing to
            # replace: it gets an entry of its own at the end. Nothing is
            # renumbered by that, and the count is worked out when the save
            # is written, so every other value keeps pointing where it did.
            self.gsave.strings.append(raw_text)
            new = len(self.gsave.strings) - 1
        else:
            new = int(value)
        self._put_raw(slot, new)


def loads(data: bytes, game_dir=None) -> AliceSave:
    save = AliceSave()
    save.game_dir = game_dir
    save.load(data)
    return save


def is_alicesoft(data: bytes) -> bool:
    """Whether *data* is an AliceSoft container, whatever it is called."""
    if data[:4] == _PSR_MAGIC:
        return len(data) > _PSR_HEADER
    return data[:4] == MAGIC and len(data) > 9
