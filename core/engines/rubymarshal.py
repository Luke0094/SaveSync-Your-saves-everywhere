"""Ruby's Marshal format (version 4.8) — what RPG Maker XP, VX and VX Ace
write their saves in.

A save file from those engines is not one Marshal stream but several, dumped
back to back: the engine calls ``Marshal.dump`` once per object it saves. So
the file is read as a sequence of streams and written back the same way.

Everything needed to re-emit the stream byte-for-byte is preserved, including
the symbol table and the object back-references Ruby uses when the same
object appears twice. That is not a nicety: the editor refuses to write any
file it cannot rebuild exactly, so an approximate reader would simply never
be allowed to save.

Unknown constructs are kept as opaque blobs rather than guessed at, for the
same reason.
"""
import struct

MARSHAL_MAJOR = 4
MARSHAL_MINOR = 8


class MarshalError(Exception):
    pass


class RSymbol(str):
    """A Ruby symbol. A str subclass so it reads naturally, distinct so it
    can be written back as a symbol rather than a string."""
    __slots__ = ()


class RFloat:
    """A Ruby Float, keeping the exact bytes Ruby wrote.

    Ruby dumps a float as decimal text — and when that text does not name the
    number exactly, it adds a nul and a few bytes of the mantissa to make up
    the difference. Those trailing bytes are not text: read them as ASCII and
    anything above 0x7f becomes a replacement character, which is written back
    out as a question mark. One byte changes, and the file no longer rebuilds
    exactly, so a perfectly good save is refused as unreadable.

    So the payload is kept exactly as it arrived, and only read as text when
    somebody wants to see the number.
    """
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = (raw if isinstance(raw, (bytes, bytearray))
                    else str(raw).encode("ascii", "replace"))

    @property
    def text(self) -> str:
        return self.raw.split(b"\0")[0].decode("ascii", "replace")

    @property
    def value(self) -> float:
        try:
            return float(self.text)
        except ValueError:
            return 0.0

    def with_value(self, v: float) -> "RFloat":
        # A float being replaced is written as plain text: the mantissa bytes
        # existed to pin down the number that is going away.
        return RFloat(repr(float(v)).encode("ascii"))

    def __repr__(self):
        return f"RFloat({self.text!r})"


class RString:
    """A Ruby String: raw bytes plus whatever instance variables rode along
    (normally just :E, the encoding flag)."""
    __slots__ = ("data", "ivars")

    def __init__(self, data: bytes, ivars=None):
        self.data = data
        self.ivars = ivars or []

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")

    def __repr__(self):
        return f"RString({self.data!r})"


class RObject:
    """A plain Ruby object: its class name and its instance variables."""
    __slots__ = ("cls", "ivars")

    def __init__(self, cls: str, ivars=None):
        self.cls = cls
        self.ivars = ivars or []      # list of (RSymbol, value)

    def __repr__(self):
        return f"RObject({self.cls}, {len(self.ivars)} ivars)"


class RUserDef:
    """An object with its own ``_dump``/``_load`` — the payload is opaque."""
    __slots__ = ("cls", "data", "ivars")

    def __init__(self, cls: str, data: bytes, ivars=None):
        self.cls = cls
        self.data = data
        self.ivars = ivars or []


class RUserMarshal:
    """An object using ``marshal_dump``/``marshal_load``."""
    __slots__ = ("cls", "value")

    def __init__(self, cls: str, value):
        self.cls = cls
        self.value = value


class RHash:
    """A Ruby Hash. Ordered, and keys may be unhashable in Python terms, so
    this is a list of pairs rather than a dict."""
    __slots__ = ("pairs", "default")

    def __init__(self, pairs=None, default=None):
        self.pairs = pairs if pairs is not None else []
        self.default = default

    def __repr__(self):
        return f"RHash({len(self.pairs)} pairs)"


class RRaw:
    """A construct this reader does not model — kept as the exact bytes it
    occupied so the stream still rebuilds."""
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data


class RBignum:
    __slots__ = ("sign", "words")

    def __init__(self, sign: bytes, words: bytes):
        self.sign = sign
        self.words = words


class RClassRef:
    """A Class/Module reference ('c' / 'm' / 'M')."""
    __slots__ = ("kind", "name")

    def __init__(self, kind: str, name: bytes):
        self.kind = kind
        self.name = name


class RExtended:
    """``e`` — an object extended by a module."""
    __slots__ = ("module", "inner")

    def __init__(self, module, inner):
        self.module = module
        self.inner = inner


class RStructVal:
    __slots__ = ("cls", "pairs")

    def __init__(self, cls, pairs):
        self.cls = cls
        self.pairs = pairs


class RRegexp:
    __slots__ = ("source", "options", "ivars")

    def __init__(self, source: bytes, options: int, ivars=None):
        self.source = source
        self.options = options
        self.ivars = ivars or []


class RSubclassed:
    """``C`` — a subclass of a built-in (String/Array/Hash)."""
    __slots__ = ("cls", "inner")

    def __init__(self, cls, inner):
        self.cls = cls
        self.inner = inner


# ── Reading ──────────────────────────────────────────────────────────────────

class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.symbols = []
        self.objects = []

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise MarshalError("truncated Marshal stream")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise MarshalError("truncated Marshal stream")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def long(self) -> int:
        """Ruby's packed integer: a length byte that often IS the value."""
        c = self.byte()
        if c > 127:
            c -= 256
        if c == 0:
            return 0
        if 1 <= c <= 4:
            raw = self.take(c)
            return int.from_bytes(raw, "little", signed=False)
        if -4 <= c <= -1:
            n = -c
            raw = self.take(n)
            v = int.from_bytes(raw, "little", signed=False)
            return v - (1 << (8 * n))
        if c > 0:
            return c - 5
        return c + 5

    # ── values ──
    def value(self):
        t = chr(self.byte())
        if t == "0":
            return None
        if t == "T":
            return True
        if t == "F":
            return False
        if t == "i":
            return self.long()
        if t == ":":
            sym = RSymbol(self.take(self.long()).decode("utf-8", "replace"))
            self.symbols.append(sym)
            return sym
        if t == ";":
            idx = self.long()
            if not (0 <= idx < len(self.symbols)):
                raise MarshalError("symbol link out of range")
            return self.symbols[idx]
        if t == "@":
            idx = self.long()
            if not (0 <= idx < len(self.objects)):
                raise MarshalError("object link out of range")
            return self.objects[idx]
        if t == "I":
            inner = self.value()
            ivars = self._ivars()
            if isinstance(inner, RString):
                inner.ivars = ivars
                return inner
            if isinstance(inner, RRegexp):
                inner.ivars = ivars
                return inner
            if isinstance(inner, RUserDef):
                inner.ivars = ivars
                return inner
            return RObject("__ivar_wrapped__", [(RSymbol("__inner__"), inner)] + ivars)
        if t == '"':
            s = RString(self.take(self.long()))
            self.objects.append(s)
            return s
        if t == "f":
            f = RFloat(self.take(self.long()))
            self.objects.append(f)
            return f
        if t == "[":
            arr = []
            self.objects.append(arr)
            for _ in range(self.long()):
                arr.append(self.value())
            return arr
        if t in "{}":
            h = RHash()
            self.objects.append(h)
            for _ in range(self.long()):
                k = self.value()
                v = self.value()
                h.pairs.append((k, v))
            if t == "}":
                h.default = self.value()
            return h
        if t == "o":
            cls = self.value()
            obj = RObject(str(cls))
            self.objects.append(obj)
            obj.ivars = self._ivars()
            return obj
        if t == "u":
            cls = self.value()
            data = self.take(self.long())
            u = RUserDef(str(cls), data)
            self.objects.append(u)
            return u
        if t == "U":
            cls = self.value()
            u = RUserMarshal(str(cls), None)
            self.objects.append(u)
            u.value = self.value()
            return u
        if t == "l":
            sign = self.take(1)
            words = self.take(self.long() * 2)
            b = RBignum(sign, words)
            self.objects.append(b)
            return b
        if t in "cm M":
            if t == " ":
                raise MarshalError("unexpected byte")
            name = self.take(self.long())
            r = RClassRef(t, name)
            self.objects.append(r)
            return r
        if t == "e":
            mod = self.value()
            inner = self.value()
            return RExtended(mod, inner)
        if t == "S":
            cls = self.value()
            st = RStructVal(cls, [])
            self.objects.append(st)
            for _ in range(self.long()):
                k = self.value()
                v = self.value()
                st.pairs.append((k, v))
            return st
        if t == "/":
            src = self.take(self.long())
            opts = self.byte()
            r = RRegexp(src, opts)
            self.objects.append(r)
            return r
        if t == "C":
            cls = self.value()
            inner = self.value()
            return RSubclassed(cls, inner)
        raise MarshalError(f"unsupported Marshal type {t!r}")

    def _ivars(self):
        out = []
        for _ in range(self.long()):
            k = self.value()
            v = self.value()
            out.append((k, v))
        return out


# ── Writing ──────────────────────────────────────────────────────────────────

class _Writer:
    def __init__(self):
        self.out = bytearray()
        self.symbols = {}
        self.objects = {}

    def long(self, n: int):
        if n == 0:
            self.out.append(0)
            return
        if 0 < n < 123:
            self.out.append(n + 5)
            return
        if -124 < n < 0:
            self.out.append((n - 5) & 0xFF)
            return
        # Otherwise: a signed count, then that many little-endian bytes.
        # Ruby's own loop, kept literally: emit a byte, shift right by 8,
        # and stop once nothing but sign is left. Deriving the width any
        # other way produces a longer-but-still-valid encoding, which reads
        # back correctly and rebuilds the file DIFFERENTLY — and a file that
        # does not rebuild exactly is one the editor refuses to write.
        body = bytearray()
        v = n
        end = 0 if n > 0 else -1
        for _ in range(8):
            body.append(v & 0xFF)
            v >>= 8                     # arithmetic: -1 stays -1
            if v == end:
                break
        self.out.append(len(body) if n > 0 else (-len(body)) & 0xFF)
        self.out += body

    def _obj_ref(self, obj) -> bool:
        """Emit a back-reference if this exact object was written before.

        Ruby writes a link the second time it sees the same object, so
        reproducing that is what makes the round trip byte-exact.
        """
        key = id(obj)
        if key in self.objects:
            self.out += b"@"
            self.long(self.objects[key])
            return True
        self.objects[key] = len(self.objects)
        return False

    def _register(self, obj):
        self.objects[id(obj)] = len(self.objects)

    def symbol(self, sym: str):
        if sym in self.symbols:
            self.out += b";"
            self.long(self.symbols[sym])
            return
        self.symbols[sym] = len(self.symbols)
        raw = str(sym).encode("utf-8")
        self.out += b":"
        self.long(len(raw))
        self.out += raw

    def value(self, v):
        if v is None:
            self.out += b"0"
            return
        if v is True:
            self.out += b"T"
            return
        if v is False:
            self.out += b"F"
            return
        if isinstance(v, RSymbol):
            self.symbol(v)
            return
        if isinstance(v, bool):
            self.out += b"T" if v else b"F"
            return
        if isinstance(v, int):
            self.out += b"i"
            self.long(v)
            return
        if isinstance(v, RString):
            if self._obj_ref(v):
                return
            if v.ivars:
                self.out += b"I"
            self.out += b'"'
            self.long(len(v.data))
            self.out += v.data
            if v.ivars:
                self._ivars(v.ivars)
            return
        if isinstance(v, RFloat):
            if self._obj_ref(v):
                return
            raw = v.raw
            self.out += b"f"
            self.long(len(raw))
            self.out += raw
            return
        if isinstance(v, list):
            if self._obj_ref(v):
                return
            self.out += b"["
            self.long(len(v))
            for item in v:
                self.value(item)
            return
        if isinstance(v, RHash):
            if self._obj_ref(v):
                return
            self.out += b"}" if v.default is not None else b"{"
            self.long(len(v.pairs))
            for k, val in v.pairs:
                self.value(k)
                self.value(val)
            if v.default is not None:
                self.value(v.default)
            return
        if isinstance(v, RObject):
            if v.cls == "__ivar_wrapped__":
                inner = v.ivars[0][1]
                rest = v.ivars[1:]
                self.out += b"I"
                self.value(inner)
                self._ivars(rest)
                return
            if self._obj_ref(v):
                return
            self.out += b"o"
            self.symbol(RSymbol(v.cls))
            self._ivars(v.ivars)
            return
        if isinstance(v, RUserDef):
            if self._obj_ref(v):
                return
            if v.ivars:
                self.out += b"I"
            self.out += b"u"
            self.symbol(RSymbol(v.cls))
            self.long(len(v.data))
            self.out += v.data
            if v.ivars:
                self._ivars(v.ivars)
            return
        if isinstance(v, RUserMarshal):
            if self._obj_ref(v):
                return
            self.out += b"U"
            self.symbol(RSymbol(v.cls))
            self.value(v.value)
            return
        if isinstance(v, RBignum):
            if self._obj_ref(v):
                return
            self.out += b"l"
            self.out += v.sign
            self.long(len(v.words) // 2)
            self.out += v.words
            return
        if isinstance(v, RClassRef):
            if self._obj_ref(v):
                return
            self.out += v.kind.encode("ascii")
            self.long(len(v.name))
            self.out += v.name
            return
        if isinstance(v, RExtended):
            self.out += b"e"
            self.value(v.module)
            self.value(v.inner)
            return
        if isinstance(v, RStructVal):
            if self._obj_ref(v):
                return
            self.out += b"S"
            self.value(v.cls)
            self.long(len(v.pairs))
            for k, val in v.pairs:
                self.value(k)
                self.value(val)
            return
        if isinstance(v, RRegexp):
            if self._obj_ref(v):
                return
            if v.ivars:
                self.out += b"I"
            self.out += b"/"
            self.long(len(v.source))
            self.out += v.source
            self.out.append(v.options)
            if v.ivars:
                self._ivars(v.ivars)
            return
        if isinstance(v, RSubclassed):
            self.out += b"C"
            self.value(v.cls)
            self.value(v.inner)
            return
        if isinstance(v, RRaw):
            self.out += v.data
            return
        raise MarshalError(f"cannot write {type(v).__name__}")

    def _ivars(self, ivars):
        self.long(len(ivars))
        for k, v in ivars:
            self.value(k)
            self.value(v)


def loads(data: bytes):
    """One Marshal stream → its value."""
    if len(data) < 2 or data[0] != MARSHAL_MAJOR or data[1] != MARSHAL_MINOR:
        raise MarshalError("not a Marshal 4.8 stream")
    r = _Reader(data)
    r.pos = 2
    return r.value()


def dumps(value) -> bytes:
    """A value → one Marshal stream."""
    w = _Writer()
    w.out += bytes([MARSHAL_MAJOR, MARSHAL_MINOR])
    w.value(value)
    return bytes(w.out)


def load_all(data: bytes) -> list:
    """Every stream in a file. RPG Maker writes several, back to back."""
    out = []
    pos = 0
    while pos < len(data):
        if data[pos] != MARSHAL_MAJOR or data[pos + 1] != MARSHAL_MINOR:
            raise MarshalError("expected a Marshal header")
        r = _Reader(data)
        r.pos = pos + 2
        out.append(r.value())
        pos = r.pos
    return out


def dump_all(values: list) -> bytes:
    return b"".join(dumps(v) for v in values)


# How deep to walk a Ruby object graph looking for values. RPG Maker nests a
# few levels; past this it is the engine's own bookkeeping.
_MARSHAL_DEPTH = 12


def _key_label(key, index: int) -> str:
    if isinstance(key, RString):
        return key.text()
    if isinstance(key, (str, int)):
        return str(key).lstrip("@")
    return str(index)


class MarshalSave:
    """Several Marshal streams opened for editing (RPG Maker XP/VX/Ace)."""

    def __init__(self, streams: list):
        self.streams = streams

    def dump(self) -> bytes:
        return dump_all(self.streams)

    def _children(self, node):
        """(step, label, child) for everything inside *node*."""
        if isinstance(node, list):
            for i, v in enumerate(node):
                yield ("i", i), str(i), v
        elif isinstance(node, RHash):
            for i, (k, v) in enumerate(node.pairs):
                yield ("h", i), _key_label(k, i), v
        elif isinstance(node, (RObject, RStructVal)):
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            for i, (k, v) in enumerate(pairs):
                yield ("o", i), str(k).lstrip("@"), v
        elif isinstance(node, RUserMarshal):
            yield ("u", 0), "value", node.value

    def _child_at(self, node, step):
        kind, idx = step
        if kind == "i":
            return node[idx]
        if kind == "h":
            return node.pairs[idx][1]
        if kind == "o":
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            return pairs[idx][1]
        return node.value

    def _set_child(self, node, step, value):
        kind, idx = step
        if kind == "i":
            node[idx] = value
        elif kind == "h":
            k, _ = node.pairs[idx]
            node.pairs[idx] = (k, value)
        elif kind == "o":
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            k, _ = pairs[idx]
            pairs[idx] = (k, value)
        else:
            node.value = value

    def _get_parent(self, path):
        node = self.streams[path[0]]
        for step in path[1:-1]:
            node = self._child_at(node, step)
        return node

    @staticmethod
    def _stream_name(stream, index: int) -> str:
        """What the save itself calls this stream.

        Ruby writes an object's class name into the file, and an RPG Maker
        save is a run of them: Game_Switches, Game_Variables, Game_Party and
        the rest. Naming them is not decoration — Game_Switches and
        Game_Variables both keep their contents in an ivar called ``data``, so
        without the class name switch 12 and variable 12 are the same label.
        """
        return stream.cls if isinstance(stream, RObject) else str(index)

    def values(self) -> list:
        """(path, label, kind, value, group) for every editable leaf."""
        out, seen = [], set()

        def walk(node, path, labels, depth):
            # Ruby graphs contain back-references; without an identity guard
            # a save that points at itself would walk forever.
            if depth > _MARSHAL_DEPTH or id(node) in seen:
                return
            seen.add(id(node))
            for step, label, child in self._children(node):
                here = path + (step,)
                names = labels + (label,)
                if isinstance(child, bool):
                    kind = "bool"
                elif isinstance(child, int):
                    kind = "int"
                elif isinstance(child, RFloat):
                    kind = "float"
                elif isinstance(child, RString):
                    kind = "str"
                else:
                    walk(child, here, names, depth + 1)
                    continue
                value = (child.value if isinstance(child, RFloat)
                         else child.text() if isinstance(child, RString)
                         else child)
                out.append((here, ".".join(names), kind, value,
                            names[0] if names else ""))

        for i, stream in enumerate(self.streams):
            walk(stream, (i,), (self._stream_name(stream, i),), 0)
        return out

    def set_value(self, path: tuple, value) -> None:
        parent = self._get_parent(path)
        current = self._child_at(parent, path[-1])
        if isinstance(current, RFloat):
            value = current.with_value(value)
        elif isinstance(current, RString):
            # Keep the string OBJECT: it may be shared, and replacing it
            # would turn a back-reference into a second copy.
            current.data = str(value).encode("utf-8")
            return
        elif isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int):
            value = int(value)
        self._set_child(parent, path[-1], value)


def open_save(data: bytes) -> MarshalSave:
    """Every stream in a RPG Maker-style save, ready to edit."""
    streams = load_all(data)
    if not streams:
        raise MarshalError("no Marshal stream in the file")
    return MarshalSave(streams)
