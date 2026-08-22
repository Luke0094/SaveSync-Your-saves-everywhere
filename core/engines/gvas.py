"""Unreal Engine save files (GVAS) — the ``.sav`` UE4 and UE5 games write.

The layout here follows uesave (github.com/trumank/uesave), which is the
reference implementation for this format. That matters: an earlier attempt at
this was written from memory and got three things wrong — the property size
is a 32-bit field, not 64; there is an array index after it; and a bool's
value lives in the property TAG, not in its body. Guessing at a binary format
produces a file the game refuses to load, so it is read from the spec or not
at all.

What is decoded: the property list, and the scalar values inside it —
integers, floats, booleans, strings. Everything else (structs, arrays, maps,
enums) is carried through as the exact bytes it arrived as. That is enough
for the values anyone edits, and it makes the round trip exact whether or not
every construct is understood.

UE 5.4 rewrote the property tag. Instead of a type name followed by whatever
extras that type needs, it writes a type TREE — a name and its parameters,
each a name and its parameters in turn — then the size, then a byte of flags
that says which of the optional fields follow. The bool's value moved into
those flags, and the struct's name and GUID moved into the tree. Both shapes
are read here; which one a file uses is decided by the engine version in its
own header, at 5.4, exactly as the reference implementation decides it.
"""
import logging
import struct

logger = logging.getLogger(__name__)

MAGIC = b"GVAS"
# UE 5.4 replaced the flat property tag with a nested type tree.
_NEW_TAG_FROM = (5, 4)
# EPropertyTagFlags, from the reference implementation.
_F_ARRAY_INDEX = 0x01
_F_PROPERTY_GUID = 0x02
_F_EXTENSIONS = 0x04
# Listed for completeness and deliberately not acted on: it only tells the
# reference implementation which of two ways to resolve a struct's type, and
# the type tree travels through here as bytes either way.
_F_NATIVE_SERIALIZE = 0x08
_F_BOOL_TRUE = 0x10
# A type tree is a few levels at most — Map<Name, Struct> is three. Deeper
# than this is a broken file, and refusing it beats recursing until Python
# gives up.
_MAX_TYPE_DEPTH = 32
# The property GUID flag appeared in 4.12 (VER_UE4_PROPERTY_GUID_IN_PROPERTY_TAG).
_GUID_FROM = (4, 12)
# Custom version block, same threshold.
_CUSTOM_VERSIONS_FROM = (4, 12)


class GvasError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise GvasError("truncated save file")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def string(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            return self.take(n)[:-1].decode("utf-8", errors="replace")
        return self.take(-n * 2)[:-2].decode("utf-16-le", errors="replace")


def put_string(s: str) -> bytes:
    """Encode as UE does: ASCII where it can, UTF-16 where it must, always
    with the terminator the engine expects."""
    if s == "":
        return struct.pack("<i", 0)
    try:
        raw = s.encode("ascii") + b"\x00"
        return struct.pack("<i", len(raw)) + raw
    except UnicodeEncodeError:
        raw = s.encode("utf-16-le") + b"\x00\x00"
        return struct.pack("<i", -(len(raw) // 2)) + raw


# Scalar bodies: (struct format, byte width). Bools are not here — their
# value is a byte in the tag, and their body is empty.
_SCALARS = {
    "IntProperty":    ("<i", 4),
    "Int8Property":   ("<b", 1),
    "Int16Property":  ("<h", 2),
    "Int64Property":  ("<q", 8),
    "UInt8Property":  ("<B", 1),
    "UInt16Property": ("<H", 2),
    "UInt32Property": ("<I", 4),
    "UInt64Property": ("<Q", 8),
    "FloatProperty":  ("<f", 4),
    "DoubleProperty": ("<d", 8),
}
_STRINGS = ("StrProperty", "NameProperty")


def _type_tree(r: "_Reader", depth: int = 0) -> str:
    """Read one node of a UE 5.4 property type name; return the name at its root.

    A node is a name and a list of parameters, each a node in turn, so
    ``ArrayProperty(IntProperty)`` and ``MapProperty(NameProperty,
    StructProperty(/Script/Game.Thing, <guid>))`` have the same shape. Only
    the root name says what kind of property this is, which is all an editor
    needs to know; everything below it travels back out as the bytes it came
    in as, so a type this reader has never heard of still rebuilds exactly.
    """
    if depth > _MAX_TYPE_DEPTH:
        raise GvasError("property type nested deeper than any real one")
    name = r.string()
    count = r.u32()
    # A parameter cannot be shorter than its own length field, so a count
    # bigger than the bytes left is a broken file, not a long list.
    if count > len(r.data) - r.pos:
        raise GvasError("property type claims more parameters than the file has bytes")
    for _ in range(count):
        _type_tree(r, depth + 1)
    return name


class GvasSave:
    """One GVAS file, decoded far enough to edit and rebuild exactly."""

    def __init__(self):
        self.header = b""
        self.save_type = ""
        self.props = []          # ordered list of property dicts
        self.tail = b""
        self.engine = (0, 0)
        self.new_tag = False     # UE 5.4+ writes the tag differently
        self.raw_len = 0         # what load() was given, for plausibility
        # Version-driven layout decisions, forced. None = work it out from the
        # version in the file, which is what every normal read does. A caller
        # sets one of these only to try a shape the version did not predict —
        # see _Format.variants and open_save's auto-resolution pass. Unreal
        # has moved each of these between releases, and a build that moves one
        # again reads as "not a GVAS file" rather than as a newer one.
        self.force_ue5_field = None
        self.force_custom_versions = None
        self.force_new_tag = None
        self.force_guid = None

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, data: bytes) -> None:
        if not data.startswith(MAGIC):
            raise GvasError("not a GVAS file")
        self.raw_len = len(data)
        r = _Reader(data)
        r.take(4)
        save_version = r.u32()
        r.u32()                                   # package version UE4
        # 34 is a game-specific version that does NOT carry the UE5 field.
        ue5_field = (save_version >= 3 and save_version != 34)
        if self.force_ue5_field is not None:
            ue5_field = bool(self.force_ue5_field)
        if ue5_field:
            r.u32()                               # package version UE5
        major, minor = r.u16(), r.u16()
        r.u16(); r.u32()                          # patch, build
        r.string()                                # engine version name
        self.engine = (major, minor)
        custom_versions = (major, minor) >= _CUSTOM_VERSIONS_FROM
        if self.force_custom_versions is not None:
            custom_versions = bool(self.force_custom_versions)
        if custom_versions:
            r.u32()                               # custom format version
            for _ in range(r.u32()):
                r.take(16); r.i32()               # guid + version
        self.save_type = r.string()
        self.header = data[:r.pos]

        self.new_tag = ((major, minor) >= _NEW_TAG_FROM
                        if self.force_new_tag is None else bool(self.force_new_tag))
        if self.new_tag:
            self._read_new_props(r, data)
            return

        has_guid = ((major, minor) >= _GUID_FROM
                    if self.force_guid is None else bool(self.force_guid))
        while True:
            name = r.string()
            if name == "None":
                break
            ptype = r.string()
            size = r.u32()
            index = r.u32()
            extra_start = r.pos
            bool_value = None
            if ptype == "BoolProperty":
                bool_value = r.u8() > 0
            elif ptype in ("ByteProperty", "EnumProperty", "ArrayProperty"):
                r.string()
            elif ptype == "SetProperty":
                # Key type only. When that key is a struct, the reference
                # implementation looks its type up from a registry the CALLER
                # supplies — it is not in the file, and reading one here eats
                # the first bytes of the body.
                r.string()
            elif ptype == "MapProperty":
                r.string()                        # key type
                r.string()                        # value type
            elif ptype == "StructProperty":
                r.string()                        # struct type
                r.take(16)                        # struct guid
            extra = data[extra_start:r.pos]
            guid = b""
            if has_guid:
                flag = r.u8()
                guid = bytes([flag]) + (r.take(16) if flag else b"")
            body = r.take(size)
            self.props.append({
                "name": name, "type": ptype, "index": index,
                "extra": extra, "guid": guid, "body": body,
                "bool": bool_value,
            })
        self.tail = data[r.pos:]

    def _read_new_props(self, r: "_Reader", data: bytes) -> None:
        """The property list as UE 5.4 and later write it.

        Name, then the type tree, then the size, then one byte of flags that
        says which of the optional fields come after it. A bool has no body:
        its value is a bit in those flags.
        """
        while True:
            name = r.string()
            if name == "None":
                break
            tag_start = r.pos
            ptype = _type_tree(r)
            tag = data[tag_start:r.pos]
            size = r.u32()
            flags = r.u8()
            if flags & _F_EXTENSIONS:
                # The reference implementation does not read these either, and
                # their length is not knowable from here — carrying on would
                # read the rest of the file at the wrong offset and call the
                # rubbish it found "values".
                raise GvasError("this save carries property extensions, "
                                "which SaveSync does not read")
            index = r.u32() if flags & _F_ARRAY_INDEX else 0
            guid = r.take(16) if flags & _F_PROPERTY_GUID else b""
            body = r.take(size)
            self.props.append({
                "name": name, "type": ptype, "index": index,
                "extra": b"", "guid": guid, "body": body,
                "tag": tag, "flags": flags,
                "bool": bool(flags & _F_BOOL_TRUE) if ptype == "BoolProperty" else None,
            })
        self.tail = data[r.pos:]

    # ── writing ──────────────────────────────────────────────────────────────

    def dump(self) -> bytes:
        if self.new_tag:
            return self._dump_new()
        out = [self.header]
        for p in self.props:
            extra = p["extra"]
            if p["type"] == "BoolProperty":
                # The value IS the tag data for a bool.
                extra = bytes([1 if p["bool"] else 0])
            out.append(put_string(p["name"]))
            out.append(put_string(p["type"]))
            out.append(struct.pack("<I", len(p["body"])))
            out.append(struct.pack("<I", p["index"]))
            out.append(extra)
            out.append(p["guid"])
            out.append(p["body"])
        out.append(put_string("None"))
        out.append(self.tail)
        return b"".join(out)

    def _dump_new(self) -> bytes:
        """The UE 5.4 shape, written back in the order it was read.

        The type tree goes out as the bytes it came in as, so a property whose
        type this reader has never met is still rebuilt exactly. Only the
        size, the bool bit and the body can have changed.
        """
        out = [self.header]
        for p in self.props:
            flags = p["flags"]
            if p["type"] == "BoolProperty":
                # A bool has no body: the value IS a bit of the flags.
                flags = (flags | _F_BOOL_TRUE) if p["bool"] else (flags & ~_F_BOOL_TRUE)
            out.append(put_string(p["name"]))
            out.append(p["tag"])
            out.append(struct.pack("<I", len(p["body"])))
            out.append(bytes([flags]))
            if flags & _F_ARRAY_INDEX:
                out.append(struct.pack("<I", p["index"]))
            if flags & _F_PROPERTY_GUID:
                out.append(p["guid"])
            out.append(p["body"])
        out.append(put_string("None"))
        out.append(self.tail)
        return b"".join(out)

    # ── the values inside ────────────────────────────────────────────────────

    def values(self) -> list:
        """(index, name, kind, value) for every value that can be edited."""
        out = []
        for i, p in enumerate(self.props):
            t = p["type"]
            if t == "BoolProperty":
                out.append((i, p["name"], "bool", bool(p["bool"])))
            elif t in _SCALARS:
                fmt, width = _SCALARS[t]
                if len(p["body"]) != width:
                    continue
                value = struct.unpack(fmt, p["body"])[0]
                kind = "float" if t in ("FloatProperty", "DoubleProperty") else "int"
                out.append((i, p["name"], kind, value))
            elif t in _STRINGS:
                try:
                    out.append((i, p["name"], "str", _Reader(p["body"]).string()))
                except GvasError:
                    continue
        return out

    def set_value(self, index: int, value) -> None:
        p = self.props[index]
        t = p["type"]
        if t == "BoolProperty":
            # Where the value actually lives differs between the two shapes —
            # a byte of the tag before 5.4, a bit of the flags after — so it
            # is kept here and written by whichever dump is in charge.
            p["bool"] = bool(value)
            return
        if t in _SCALARS:
            fmt, _width = _SCALARS[t]
            number = float(value) if t in ("FloatProperty", "DoubleProperty") else int(value)
            p["body"] = struct.pack(fmt, number)
            return
        if t in _STRINGS:
            # The body length changes with the text, and the size field is
            # written from len(body) — so nothing else has to be touched.
            p["body"] = put_string(str(value))
            return
        raise GvasError(f"{t} is not an editable value")


def loads(data: bytes) -> GvasSave:
    save = GvasSave()
    save.load(data)
    return save
