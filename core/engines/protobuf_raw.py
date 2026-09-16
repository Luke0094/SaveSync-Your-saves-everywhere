"""A schema-less reader/writer for Google's Protocol Buffers wire format —
generic across every game that happens to serialise a save with it, not
specific to any one of them: unlike almost every other format this codebase
reads, protobuf carries no magic bytes, no format name, nothing that says
what it is — the wire format itself is exactly what is published in
Google's own protobuf encoding reference,
https://protobuf.dev/programming-guides/encoding/, and that is the whole of
what this module is built from.

A message is a flat sequence of (tag, value) pairs. A tag is a varint whose
low 3 bits are the WIRE TYPE and whose remaining bits are the FIELD NUMBER:

- **0 — varint**: a variable-length integer, LSB-first 7 bits per byte.
- **1 — 64-bit**: exactly 8 bytes (a double, in the common case).
- **2 — length-delimited**: a varint length, then that many bytes — a
  string, opaque bytes, or an embedded MESSAGE, and the wire format alone
  does not say which. This reader tries hardest first: a clean, complete
  parse as a nested message (see ``_try_submessage``), else valid UTF-8
  text, else it is carried as opaque bytes and offered as nothing.
- **5 — 32-bit**: exactly 4 bytes (a float, in the common case).

Nothing here NAMES a field — there is no field name anywhone in the wire
format, only a number the writer chose — so a value is offered by its path
of (field number, which occurrence) pairs down the tree, the same honesty
``core.engines.alicesoft_vsf`` already extends to a container with no names
of its own. **This is a genuine, inherent ambiguity, not a bug**: a real
protobuf field explicitly typed ``bytes`` whose content happens to look
like valid UTF-8, or happens to parse cleanly as a nested message, is
indistinguishable from one that really is a string or a message — every
schema-less protobuf tool (protoc --decode_raw included) faces exactly this
and resolves it the same way, by trying the more specific interpretation
first.

**Round-trip fidelity does not depend on any of those guesses being
right.** Every field keeps the EXACT bytes it was read with (``full_raw``:
tag, length prefix where one exists, and payload) and dump() emits that
verbatim unless the field — or something inside it — was actually edited,
in which case only the touched path is freshly re-encoded and everything
else around it is untouched. A varint's length is not canonicalised on a
field that was never touched, so even a deliberately overlong encoding
some unusual writer produced survives unchanged.
"""
import struct

TYPE_VARINT, TYPE_64BIT, TYPE_LEN, TYPE_32BIT = 0, 1, 2, 5
_VALID_WIRE_TYPES = (TYPE_VARINT, TYPE_64BIT, TYPE_LEN, TYPE_32BIT)

# How deep a length-delimited value is tried as a nested message before it
# is just left as bytes/text — past real-world save data, this is noise
# rather than structure, and it bounds recursion on adversarial input.
_MAX_DEPTH = 12
# A message decoded from a length-delimited value must have at least this
# many fields to be trusted as one — an EMPTY string trivially "parses" as
# a zero-field message too, which is a fine coincidence to fold into "no
# fields to show" rather than one to complain about, but a single
# ambiguous byte or two should not tip the scale away from plain bytes.
_MIN_SUBMESSAGE_FIELDS = 1


class ProtoError(Exception):
    pass


def _read_varint(data: bytes, pos: int, end: int):
    """(value, raw_bytes, new_pos) for the varint starting at *pos*."""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= end:
            raise ProtoError("a varint runs past the end of its message")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise ProtoError("a varint is longer than 64 bits can hold")
    return result, data[start:pos], pos


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ProtoError("a varint cannot hold a negative value")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class ProtoField:
    """One (tag, value) occurrence inside a message.

    ``full_raw`` is the EXACT original bytes of this field — tag, length
    prefix if it has one, and payload — and is what dump() emits verbatim
    whenever nothing inside this field was touched. ``children`` is the
    parsed sub-fields when this is a length-delimited value that parsed
    cleanly as a nested message; ``payload`` is the raw value bytes
    (post length-prefix, for wire type 2) that classification and editing
    both work from.
    """

    __slots__ = ("number", "wire_type", "full_raw", "payload", "children",
                "dirty")

    def __init__(self, number, wire_type, full_raw, payload, children):
        self.number = number
        self.wire_type = wire_type
        self.full_raw = full_raw
        self.payload = payload
        self.children = children          # list[ProtoField] or None
        self.dirty = False

    def _tag_bytes(self) -> bytes:
        return _encode_varint((self.number << 3) | self.wire_type)

    def serialize(self) -> bytes:
        if not self.dirty:
            return self.full_raw
        if self.wire_type == TYPE_LEN:
            if self.children is not None:
                body = b"".join(c.serialize() for c in self.children)
            else:
                body = self.payload
            return self._tag_bytes() + _encode_varint(len(body)) + body
        return self._tag_bytes() + self.payload


def _parse_fields(data: bytes, start: int, end: int, depth: int) -> list:
    pos = start
    fields = []
    while pos < end:
        field_start = pos
        tag, _tag_raw, pos = _read_varint(data, pos, end)
        number = tag >> 3
        wire_type = tag & 7
        if number == 0:
            raise ProtoError("a field number of 0 is not valid protobuf")
        if wire_type not in _VALID_WIRE_TYPES:
            raise ProtoError(f"wire type {wire_type} is not one this "
                             "reader knows")
        if wire_type == TYPE_VARINT:
            _val, val_raw, pos = _read_varint(data, pos, end)
            payload = val_raw
            children = None
        elif wire_type == TYPE_64BIT:
            if pos + 8 > end:
                raise ProtoError("a 64-bit value runs past the end of its "
                                 "message")
            payload = data[pos:pos + 8]
            pos += 8
            children = None
        elif wire_type == TYPE_32BIT:
            if pos + 4 > end:
                raise ProtoError("a 32-bit value runs past the end of its "
                                 "message")
            payload = data[pos:pos + 4]
            pos += 4
            children = None
        else:  # TYPE_LEN
            length, _len_raw, pos = _read_varint(data, pos, end)
            if length < 0 or pos + length > end:
                raise ProtoError("a length-delimited value runs past the "
                                 "end of its message")
            payload = data[pos:pos + length]
            pos += length
            children = _try_submessage(payload, depth)
        fields.append(ProtoField(number, wire_type, data[field_start:pos],
                                 payload, children))
    return fields


def _try_submessage(payload: bytes, depth: int):
    """The parsed fields of *payload* as a nested message, or None."""
    if depth >= _MAX_DEPTH or not payload:
        return None
    try:
        fields = _parse_fields(payload, 0, len(payload), depth + 1)
    except ProtoError:
        return None
    return fields if len(fields) >= _MIN_SUBMESSAGE_FIELDS else None


class ProtoMessage:
    """One protobuf message, read from a whole file with no envelope."""

    def __init__(self):
        self._fields = []

    def load(self, data: bytes) -> None:
        if not data:
            raise ProtoError("empty file")
        self._fields = _parse_fields(data, 0, len(data), 0)
        if not self._fields:
            raise ProtoError("this file holds no protobuf fields")

    def dump(self) -> bytes:
        return b"".join(f.serialize() for f in self._fields)

    def leaves(self) -> list:
        """(path, kind, value) for every editable scalar, depth-first.

        *path* is a tuple of (field_number, occurrence_index) pairs, one
        per level — the field has no name, only where it sits.
        """
        out = []

        def walk(fields, prefix):
            counts = {}
            for f in fields:
                idx = counts.get(f.number, 0)
                counts[f.number] = idx + 1
                path = prefix + ((f.number, idx),)
                if f.wire_type == TYPE_VARINT:
                    out.append((path, "int", _decode_varint_value(f.payload)))
                elif f.wire_type == TYPE_64BIT:
                    out.append((path, "float",
                               struct.unpack("<d", f.payload)[0]))
                elif f.wire_type == TYPE_32BIT:
                    out.append((path, "float",
                               struct.unpack("<f", f.payload)[0]))
                else:  # TYPE_LEN
                    if f.children is not None:
                        walk(f.children, path)
                    else:
                        try:
                            text = f.payload.decode("utf-8", errors="strict")
                        except UnicodeDecodeError:
                            continue    # opaque bytes: preserved, not shown
                        out.append((path, "str", text))
        walk(self._fields, ())
        return out

    def field_count(self) -> int:
        """How many editable leaves this message actually offers — the
        measure ``parse_is_plausible`` uses to tell a genuine protobuf save
        from unrelated bytes that merely survived the structural parse."""
        return len(self.leaves())

    def _chain(self, path: tuple) -> list:
        """Every ProtoField from the root down to *path*, in order — the
        node itself is last, its ancestors are everything before it."""
        fields = self._fields
        chain = []
        for number, occurrence in path:
            counts = {}
            found = None
            for f in fields:
                if f.number != number:
                    continue
                idx = counts.get(number, 0)
                counts[number] = idx + 1
                if idx == occurrence:
                    found = f
                    break
            if found is None:
                raise ProtoError(f"no field at {path}")
            chain.append(found)
            fields = found.children if found.children is not None else []
        return chain

    def set_value(self, path: tuple, value) -> None:
        chain = self._chain(path)
        node = chain[-1]
        if node.wire_type == TYPE_VARINT:
            new = int(value)
            if new < 0:
                raise ProtoError("this value is an unsigned varint and "
                                 "cannot be negative")
            node.payload = _encode_varint(new)
        elif node.wire_type == TYPE_64BIT:
            node.payload = struct.pack("<d", float(value))
        elif node.wire_type == TYPE_32BIT:
            node.payload = struct.pack("<f", float(value))
        else:
            if node.children is not None:
                raise ProtoError("this is a nested group of values, not "
                                 "one that can be set directly")
            node.payload = str(value).encode("utf-8")
        for f in chain:
            f.dirty = True


def _decode_varint_value(raw: bytes) -> int:
    value = 0
    shift = 0
    for b in raw:
        value |= (b & 0x7F) << shift
        shift += 7
    return value


def loads(data: bytes) -> ProtoMessage:
    msg = ProtoMessage()
    msg.load(data)
    return msg
