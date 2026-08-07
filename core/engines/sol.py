"""Flash Local Shared Objects (``.sol``) — what Flash games saved into.

A ``.sol`` is a small header, a name, and then a list of properties encoded
in AMF: AMF0 for older files, AMF3 for anything built with ActionScript 3.
Both are implemented here, from encodings produced by a reference AMF library
rather than from memory.

Values are edited the way Ren'Py's pickle is: the file is walked to find
where each value's bytes sit, and an edit splices new bytes over the old
ones. Everything not edited is the original file, byte for byte. That matters
more in AMF3 than anywhere else — it keeps a string reference table that is
built in the order strings first appear, and rebuilding the file from a
decoded tree would have to reproduce those choices exactly. Splicing does not
have to reproduce anything.
"""
import logging
import struct

logger = logging.getLogger(__name__)

MAGIC = b"TCSO"

# AMF0 markers.
_A0_NUMBER, _A0_BOOL, _A0_STRING, _A0_OBJECT = 0x00, 0x01, 0x02, 0x03
_A0_NULL, _A0_UNDEFINED, _A0_REFERENCE = 0x05, 0x06, 0x07
_A0_ECMA_ARRAY, _A0_OBJECT_END, _A0_STRICT_ARRAY = 0x08, 0x09, 0x0A
_A0_DATE, _A0_LONG_STRING, _A0_XML, _A0_TYPED, _A0_AMF3 = 0x0B, 0x0C, 0x0F, 0x10, 0x11

# AMF3 markers.
_A3_UNDEFINED, _A3_NULL, _A3_FALSE, _A3_TRUE = 0x00, 0x01, 0x02, 0x03
_A3_INT, _A3_DOUBLE, _A3_STRING = 0x04, 0x05, 0x06
_A3_DATE, _A3_ARRAY, _A3_OBJECT = 0x08, 0x09, 0x0A
_A3_BYTEARRAY = 0x0C

# A save with more values than this is a database, not something to page
# through, and walking it all would only cost time.
_MAX_VALUES = 5000


class SolError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise SolError("truncated .sol file")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self.take(8))[0]

    def utf(self) -> str:
        return self.take(self.u16()).decode("utf-8", errors="replace")

    def u29(self) -> int:
        """AMF3's variable-length integer: 7 bits per byte for three bytes,
        then a full 8 in the fourth."""
        value = 0
        for i in range(3):
            b = self.u8()
            value = (value << 7) | (b & 0x7F)
            if not (b & 0x80):
                return value
        return (value << 8) | self.u8()


def write_u29(value: int) -> bytes:
    v = value & 0x1FFFFFFF
    if v < 0x80:
        return bytes([v])
    if v < 0x4000:
        return bytes([(v >> 7) | 0x80, v & 0x7F])
    if v < 0x200000:
        return bytes([(v >> 14) | 0x80, ((v >> 7) & 0x7F) | 0x80, v & 0x7F])
    return bytes([(v >> 22) | 0x80, ((v >> 15) & 0x7F) | 0x80,
                  ((v >> 8) & 0x7F) | 0x80, v & 0xFF])


def _amf3_int_bytes(value: int) -> bytes:
    """AMF3 stores integers in 29 bits, signed. Anything outside that range is
    a double in AMF3 too, so it is written as one."""
    # Strictly INSIDE the 29-bit range. -2^28 is representable, but the
    # reference encoders write it as a double, and matching them exactly is
    # worth more than squeezing one boundary value into a smaller form.
    if -(1 << 28) < value < (1 << 28):
        return bytes([_A3_INT]) + write_u29(value & 0x1FFFFFFF)
    return bytes([_A3_DOUBLE]) + struct.pack(">d", float(value))


class SolFile:
    """One .sol, walked far enough to find and replace the values in it."""

    def __init__(self):
        self._data = b""
        self.name = ""
        self.amf_version = 0
        self._values = []        # name, kind, value, start, end

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, data: bytes) -> None:
        r = _Reader(data)
        r.u16()                                   # endian / version marker
        size = r.u32()
        if r.take(4) != MAGIC:
            raise SolError("not a Flash shared object")
        r.take(6)                                 # 00 04 00 00 00 00
        self.name = r.utf()
        self.amf_version = r.u32()
        if size + 6 != len(data):
            logger.debug(".sol length field disagrees with the file size")
        self._data = data
        self._values = []
        self._strings = []                        # AMF3 string reference table
        try:
            while r.pos < len(data) and len(self._values) < _MAX_VALUES:
                if self.amf_version == 3:
                    key = self._a3_string(r)
                else:
                    key = r.utf()
                if not key and r.pos >= len(data):
                    break
                self._read_value(r, key)
                if r.pos < len(data):
                    r.u8()                        # the 0x00 after each property
        except SolError:
            # A property this reader cannot follow ends the walk. What was
            # found before it is still valid, and everything stays editable
            # by splice — nothing is rebuilt from a partial understanding.
            logger.debug(".sol walk stopped early")
        if not self._values:
            raise SolError("no editable values in this shared object")

    def _a3_string(self, r: _Reader) -> str:
        header = r.u29()
        if not (header & 1):                      # a reference to an earlier one
            idx = header >> 1
            return self._strings[idx] if idx < len(self._strings) else ""
        text = r.take(header >> 1).decode("utf-8", errors="replace")
        if text:
            self._strings.append(text)
        return text

    def _record(self, name, kind, value, start, end):
        self._values.append({"name": name, "kind": kind, "value": value,
                             "start": start, "end": end})

    def _read_value(self, r: _Reader, name: str, depth: int = 0):
        if depth > 12:
            raise SolError("nested too deeply")
        if self.amf_version == 3:
            self._read_amf3(r, name, depth)
        else:
            self._read_amf0(r, name, depth)

    def _read_amf0(self, r: _Reader, name: str, depth: int):
        start = r.pos
        marker = r.u8()
        if marker == _A0_NUMBER:
            self._record(name, "float", r.f64(), start, r.pos)
        elif marker == _A0_BOOL:
            self._record(name, "bool", r.u8() != 0, start, r.pos)
        elif marker == _A0_STRING:
            self._record(name, "str", r.utf(), start, r.pos)
        elif marker == _A0_LONG_STRING:
            r.take(r.u32())
        elif marker in (_A0_NULL, _A0_UNDEFINED):
            pass
        elif marker == _A0_REFERENCE:
            r.u16()
        elif marker == _A0_DATE:
            r.f64(); r.u16()
        elif marker in (_A0_OBJECT, _A0_ECMA_ARRAY):
            if marker == _A0_ECMA_ARRAY:
                r.u32()                           # associative count, advisory
            while True:
                key = r.utf()
                if not key:
                    if r.u8() != _A0_OBJECT_END:
                        raise SolError("object did not end where expected")
                    break
                self._read_amf0(r, f"{name}.{key}" if name else key, depth + 1)
        elif marker == _A0_STRICT_ARRAY:
            for i in range(r.u32()):
                self._read_amf0(r, f"{name}.{i}", depth + 1)
        elif marker == _A0_TYPED:
            r.utf()
            while True:
                key = r.utf()
                if not key:
                    if r.u8() != _A0_OBJECT_END:
                        raise SolError("typed object did not end where expected")
                    break
                self._read_amf0(r, f"{name}.{key}" if name else key, depth + 1)
        elif marker == _A0_AMF3:
            self._read_amf3(r, name, depth + 1)
        else:
            raise SolError(f"AMF0 marker {marker:#x} is not one this reader knows")

    def _read_amf3(self, r: _Reader, name: str, depth: int):
        start = r.pos
        marker = r.u8()
        if marker == _A3_INT:
            raw = r.u29()
            # 29-bit two's complement.
            value = raw - (1 << 29) if raw & (1 << 28) else raw
            self._record(name, "int", value, start, r.pos)
        elif marker == _A3_DOUBLE:
            self._record(name, "float", r.f64(), start, r.pos)
        elif marker in (_A3_FALSE, _A3_TRUE):
            self._record(name, "bool", marker == _A3_TRUE, start, r.pos)
        elif marker == _A3_STRING:
            self._record(name, "str", self._a3_string(r), start, r.pos)
        elif marker in (_A3_UNDEFINED, _A3_NULL):
            pass
        elif marker == _A3_DATE:
            header = r.u29()
            if header & 1:
                r.f64()
        elif marker == _A3_BYTEARRAY:
            header = r.u29()
            if header & 1:
                r.take(header >> 1)
        elif marker == _A3_ARRAY:
            header = r.u29()
            if not (header & 1):
                return                            # a reference to an earlier array
            dense = header >> 1
            while True:                           # the associative part first
                key = self._a3_string(r)
                if not key:
                    break
                self._read_amf3(r, f"{name}.{key}" if name else key, depth + 1)
            for i in range(dense):
                self._read_amf3(r, f"{name}.{i}", depth + 1)
        elif marker == _A3_OBJECT:
            header = r.u29()
            if not (header & 1):
                return                            # object reference
            if (header >> 1) & 1:                 # traits, not a traits ref
                traits = header >> 2
                if traits & 1:
                    raise SolError("externalizable objects are not read")
                dynamic = bool((traits >> 1) & 1)
                count = traits >> 2
                self._a3_string(r)                # class name
                members = [self._a3_string(r) for _ in range(count)]
                for m in members:
                    self._read_amf3(r, f"{name}.{m}" if name else m, depth + 1)
                if dynamic:
                    while True:
                        key = self._a3_string(r)
                        if not key:
                            break
                        self._read_amf3(r, f"{name}.{key}" if name else key,
                                        depth + 1)
            else:
                raise SolError("traits references are not read")
        else:
            raise SolError(f"AMF3 marker {marker:#x} is not one this reader knows")

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(i, v["name"], v["kind"], v["value"])
                for i, v in enumerate(self._values)]

    def set_value(self, index: int, value) -> None:
        v = self._values[index]
        if self.amf_version == 3:
            if v["kind"] == "int":
                v["new"] = _amf3_int_bytes(int(value))
            elif v["kind"] == "float":
                v["new"] = bytes([_A3_DOUBLE]) + struct.pack(">d", float(value))
            elif v["kind"] == "bool":
                v["new"] = bytes([_A3_TRUE if value else _A3_FALSE])
            else:
                raw = str(value).encode("utf-8")
                # Written as a literal, never as a reference: a reference
                # would point at a string that is no longer what we mean.
                v["new"] = (bytes([_A3_STRING])
                            + write_u29((len(raw) << 1) | 1) + raw)
        else:
            if v["kind"] in ("int", "float"):
                v["new"] = bytes([_A0_NUMBER]) + struct.pack(">d", float(value))
            elif v["kind"] == "bool":
                v["new"] = bytes([_A0_BOOL, 1 if value else 0])
            else:
                raw = str(value).encode("utf-8")
                if len(raw) > 0xFFFF:
                    v["new"] = (bytes([_A0_LONG_STRING])
                                + struct.pack(">I", len(raw)) + raw)
                else:
                    v["new"] = (bytes([_A0_STRING])
                                + struct.pack(">H", len(raw)) + raw)
        v["value"] = value

    def dump(self) -> bytes:
        edits = [v for v in self._values if "new" in v]
        if not edits:
            return self._data
        out = self._data
        # Back to front, so an edit never moves the offsets of the ones still
        # to be applied.
        for v in sorted(edits, key=lambda x: x["start"], reverse=True):
            out = out[:v["start"]] + v["new"] + out[v["end"]:]
        # The header carries the length of everything after it.
        return out[:2] + struct.pack(">I", len(out) - 6) + out[6:]


def loads(data: bytes) -> SolFile:
    save = SolFile()
    save.load(data)
    return save
