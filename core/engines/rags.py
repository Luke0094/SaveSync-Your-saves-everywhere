"""RAGS saves (``.rsv``) — Rapid Adventure Game System.

A RAGS save is a .NET ``BinaryFormatter`` stream (MS-NRBF) with AES-256-CBC
over the whole file. The key and vector are fixed in the engine rather than
per-game; the pair used here comes from rags2html
(github.com/Kassy2048/rags2html) and is accepted on the same terms as
everything else in this editor — it is used because it demonstrably decrypts
to a valid NRBF header naming ``RagsLib``, not because a page said so.

**Only part of the save is offered, on purpose.** The stream in front of me
holds three million primitive values, but almost all of them are the colours,
fonts and command lists that make up the game's own logic and presentation.
Offering those as a flat list would be seventy-eight thousand pages of noise
with the handful of things anyone came for buried in it. So the walk reads
everything — it has to, the format is sequential — and keeps the objects that
hold *game state*: variables, objects, the player, rooms, timers. In the save
this was built against that is 639 variables and 201 objects rather than 3.1
million anything.

Editing splices into the decrypted bytes and re-encrypts. Nothing in NRBF
refers to a file offset — objects point at each other by id — so a string
that changes length simply shifts what follows it, harmlessly. And because
the vector is fixed rather than random, a save that was opened and not
changed re-encrypts to the identical file, which the editor checks.
"""
import logging
import struct

logger = logging.getLogger(__name__)

_KEY = bytes.fromhex("B4BDC259B1104A6531F8109C851BCF9A"
                     "D09BDD208851C9CBAB782AEC356CC1E3")
_IV = bytes.fromhex("31F8109C851BCF9A203D6C71A7BD1487")

# The classes worth showing, and which of their members names the instance.
_TARGETS = {
    "Rags.GameVariable": ("varname", "Variables"),
    "Rags.GameObject": ("name", "Objects"),
    "Rags.Player": ("Name", "Player"),
    "Rags.Room": ("Name", "Rooms"),
    "Rags.Timer": ("Name", "Timers"),
    "Rags.StatusBarItem": ("Name", "Status bar"),
}
# Members that are the engine's own bookkeeping rather than anything to edit.
_SKIP_MEMBERS = {"UniqueID", "UniqueIdentifier", "value__"}

# PrimitiveTypeEnum: width in bytes, struct code, and how we call the kind.
_PRIMITIVES = {
    1: (1, "<?", "bool"), 2: (1, "<B", "int"), 6: (8, "<d", "float"),
    7: (2, "<h", "int"), 8: (4, "<i", "int"), 9: (8, "<q", "int"),
    10: (1, "<b", "int"), 11: (4, "<f", "float"), 12: (8, "<q", "int"),
    13: (8, "<q", "int"), 14: (2, "<H", "int"), 15: (4, "<I", "int"),
    16: (8, "<Q", "int"),
}
_VARIABLE_PRIMITIVES = {3, 5, 18}      # Char, Decimal, String
# How deep an object may nest before we call it a runaway. The save this was
# built against nests two deep; the ceiling matters only because each level
# costs about three Python frames, so it has to bite well before the
# interpreter's own recursion limit turns a bad file into a crash rather than
# a refusal.
_MAX_DEPTH = 200


class RagsError(Exception):
    pass


def _pkcs7_strip(data: bytes) -> bytes:
    pad = data[-1] if data else 0
    if 0 < pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt(raw: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    if len(raw) < 16 or len(raw) % 16:
        raise RagsError("not a whole number of blocks")
    dec = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
    return _pkcs7_strip(dec.update(raw) + dec.finalize())


def encrypt(plain: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    pad = 16 - (len(plain) % 16)
    enc = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).encryptor()
    return enc.update(plain + bytes([pad]) * pad) + enc.finalize()


def is_rags_save(raw: bytes) -> bool:
    """Cheap and certain: decrypt one block and look for the NRBF header."""
    try:
        from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                            modes)
        if len(raw) < 32 or len(raw) % 16:
            return False
        dec = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
        head = dec.update(raw[:32])
        return head[:5] == b"\x00\x01\x00\x00\x00"
    except Exception:
        return False


def _encode_length(n: int) -> bytes:
    """NRBF's seven-bit length. Growing a string past 127 bytes makes this
    prefix itself a byte longer, which is exactly the case a fixed-width
    assumption would corrupt."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


class _Walk:
    """One pass over the record stream, keeping what matters."""

    def __init__(self, data: bytes):
        self.d = data
        self.o = 0
        self.layouts = {}       # metadata object id -> (class name, members)
        self.texts = {}         # string object id -> text
        self.found = []         # one dict per interesting object
        self._depth = 0

    # ── primitives of the format ─────────────────────────────────────────────

    def u8(self) -> int:
        v = self.d[self.o]
        self.o += 1
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def length(self) -> int:
        n = shift = 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7
            if shift > 35:
                raise RagsError("length prefix out of range")

    def text(self) -> str:
        n = self.length()
        if self.o + n > len(self.d):
            raise RagsError("a string runs past the end")
        s = self.d[self.o:self.o + n].decode("utf-8", "replace")
        self.o += n
        return s

    def primitive(self, kind: int):
        """Reads one bare primitive; returns (kind name, value, start, end)."""
        start = self.o
        if kind in _PRIMITIVES:
            width, code, name = _PRIMITIVES[kind]
            value = struct.unpack_from(code, self.d, self.o)[0]
            self.o += width
            return name, value, start, self.o
        if kind in (18, 5):
            return "str", self.text(), start, self.o
        if kind == 3:                       # Char: a single code point
            b = self.d[self.o]
            self.o += 1 if b < 0x80 else (2 if b < 0xE0 else (3 if b < 0xF0 else 4))
            return "str", self.d[start:self.o].decode("utf-8", "replace"), start, self.o
        if kind == 17:
            return "", None, start, self.o
        raise RagsError(f"unknown primitive {kind} at {start}")

    # ── records ──────────────────────────────────────────────────────────────

    def class_info(self):
        obj = self.i32()
        name = self.text()
        count = self.i32()
        return obj, name, [self.text() for _ in range(count)]

    def member_types(self, count: int) -> list:
        kinds = [self.u8() for _ in range(count)]
        out = []
        for k in kinds:
            if k in (0, 7):
                out.append((k, self.u8()))
            elif k == 3:
                self.text()
                out.append((k, None))
            elif k == 4:
                self.text()
                self.i32()
                out.append((k, None))
            else:
                out.append((k, None))
        return out

    def members(self, layout, collect=None):
        """Walk an object's members, recording the ones worth showing."""
        names, types = layout
        for i, (kind, extra) in enumerate(types):
            name = names[i] if i < len(names) else f"member {i}"
            if kind == 0:                   # a primitive, written bare
                what, value, start, end = self.primitive(extra)
                if collect is not None and what and name not in _SKIP_MEMBERS:
                    collect.append({"member": name, "kind": what,
                                    "value": value, "start": start, "end": end})
            else:
                here = self.o
                kid = self.d[here] if here < len(self.d) else -1
                self.record()
                if collect is None or name in _SKIP_MEMBERS:
                    continue
                if kid == 6:                # an inline string: editable
                    oid = struct.unpack_from("<i", self.d, here + 1)[0]
                    collect.append({"member": name, "kind": "str",
                                    "value": self.texts.get(oid, ""),
                                    "start": here + 5, "end": self.o,
                                    "string": True})
                elif kid == 9:              # a shared string, by reference
                    collect.append({"member": name, "kind": "ref",
                                    "ref": struct.unpack_from("<i", self.d,
                                                              here + 1)[0]})

    def record(self) -> int:
        if self._depth > _MAX_DEPTH:
            raise RagsError("nested too deeply")
        t = self.u8()
        if t == 0:                          # SerializedStreamHeader
            self.o += 16
        elif t == 1:                        # ClassWithId
            self.i32()
            meta = self.i32()
            found = self.layouts.get(meta)
            if found is None:
                raise RagsError(f"class {meta} was never defined")
            self._object(found)
        elif t in (2, 3):                   # members, no type information
            obj, name, names = self.class_info()
            if t == 3:
                self.i32()
            layout = (name, (names, [(2, None)] * len(names)))
            self.layouts[obj] = layout
            self._object(layout)
        elif t in (4, 5):                   # members with type information
            obj, name, names = self.class_info()
            types = self.member_types(len(names))
            if t == 5:
                self.i32()
            layout = (name, (names, types))
            self.layouts[obj] = layout
            self._object(layout)
        elif t == 6:                        # BinaryObjectString
            oid = self.i32()
            self.texts[oid] = self.text()
        elif t == 7:                        # BinaryArray
            self.i32()
            shape = self.u8()
            rank = self.i32()
            lengths = [self.i32() for _ in range(rank)]
            if shape in (3, 4, 5):
                for _ in range(rank):
                    self.i32()
            kind = self.u8()
            extra = None
            if kind in (0, 7):
                extra = self.u8()
            elif kind == 3:
                self.text()
            elif kind == 4:
                self.text()
                self.i32()
            total = 1
            for n in lengths:
                total *= n
            if kind == 0:
                for _ in range(total):
                    self.primitive(extra)
            else:
                self.slots(total)
        elif t == 8:                        # MemberPrimitiveTyped
            self.primitive(self.u8())
        elif t == 9:                        # MemberReference
            self.i32()
        elif t in (10, 11):                 # ObjectNull / MessageEnd
            pass
        elif t == 12:                       # BinaryLibrary
            self.i32()
            self.text()
        elif t == 13:
            self.u8()
        elif t == 14:
            self.i32()
        elif t == 15:                       # ArraySinglePrimitive
            self.i32()
            n = self.i32()
            kind = self.u8()
            for _ in range(n):
                self.primitive(kind)
        elif t in (16, 17):                 # ArraySingleObject / String
            self.i32()
            self.slots(self.i32())
        else:
            raise RagsError(f"unknown record {t} at {self.o - 1}")
        return t

    def _object(self, layout):
        name, members = layout
        target = _TARGETS.get(name)
        self._depth += 1
        if target is None:
            self.members(members)
        else:
            collected = []
            self.members(members, collected)
            self.found.append({"cls": name, "naming": target[0],
                               "group": target[1], "members": collected})
        self._depth -= 1

    def slots(self, n: int) -> None:
        """*n* array slots — remembering that one null record can fill many."""
        filled = 0
        while filled < n:
            here = self.o
            t = self.record()
            if t == 13:
                filled += self.d[here + 1]
            elif t == 14:
                filled += struct.unpack_from("<i", self.d, here + 1)[0]
            else:
                filled += 1

    def run(self) -> None:
        while self.o < len(self.d):
            if self.record() == 11:         # MessageEnd
                break
        # The invariant: a stream we understand is a stream we consume. Any
        # tail left over means a construct this reader does not know, and a
        # save it does not fully understand is one it must not write to.
        if self.o != len(self.d):
            raise RagsError(
                f"{len(self.d) - self.o} bytes left after the end of the save")


class RagsSave:
    """One RAGS save, opened for editing."""

    def __init__(self):
        self.plain = b""
        self._values = []     # label, kind, value, start, end

    def load(self, raw: bytes) -> None:
        self.plain = decrypt(raw)
        if self.plain[:5] != b"\x00\x01\x00\x00\x00":
            raise RagsError("not a RAGS save")
        walk = _Walk(self.plain)
        walk.run()
        self._build(walk)
        logger.info(f"RAGS save: {len(self._values)} values from "
                    f"{len(walk.found)} objects")

    def _build(self, walk: _Walk) -> None:
        used = {}
        for obj in walk.found:
            # The instance's own name; it may be spelled out or shared.
            title = ""
            for m in obj["members"]:
                if m["member"] != obj["naming"]:
                    continue
                title = (m["value"] if m["kind"] == "str"
                         else walk.texts.get(m.get("ref", -1), ""))
                break
            title = (title or "").strip() or "unnamed"
            used[(obj["group"], title)] = used.get((obj["group"], title), 0) + 1
            n = used[(obj["group"], title)]
            if n > 1:                       # two things of the same name
                title = f"{title} #{n}"
            for m in obj["members"]:
                if m["kind"] == "ref" or m["member"] == obj["naming"]:
                    continue
                self._values.append({
                    "label": f"{obj['group']} / {title} / {m['member']}",
                    "group": obj["group"], "kind": m["kind"],
                    "value": m["value"], "start": m["start"], "end": m["end"],
                    "string": m.get("string", False),
                    "code": m.get("code")})

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(i, v["label"], v["kind"], v["value"])
                for i, v in enumerate(self._values)]

    def groups(self) -> list:
        return [v["group"] for v in self._values]

    def set_value(self, index: int, value) -> None:
        rec = self._values[index]
        if rec["kind"] == "str":
            raw = str(value).encode("utf-8")
            rec["new"] = _encode_length(len(raw)) + raw
        elif rec["kind"] == "bool":
            rec["new"] = b"\x01" if value else b"\x00"
        else:
            width = rec["end"] - rec["start"]
            code = {(1, "int"): "<b", (2, "int"): "<h", (4, "int"): "<i",
                    (8, "int"): "<q", (4, "float"): "<f",
                    (8, "float"): "<d"}.get((width, rec["kind"]))
            if code is None:
                raise RagsError(f"cannot write a {width}-byte {rec['kind']}")
            rec["new"] = struct.pack(
                code, int(value) if rec["kind"] == "int" else float(value))
        rec["value"] = value

    def dump(self) -> bytes:
        out = self.plain
        # Back to front, so an edit never moves a span still to be used.
        for rec in sorted((r for r in self._values if "new" in r),
                          key=lambda r: r["start"], reverse=True):
            out = out[:rec["start"]] + rec["new"] + out[rec["end"]:]
        return encrypt(out)


def loads(raw: bytes) -> RagsSave:
    save = RagsSave()
    save.load(raw)
    return save
