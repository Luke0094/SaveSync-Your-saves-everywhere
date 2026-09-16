"""A small, reusable reader for a save that opens with a FIXED, unlabelled
binary header — a run of typed fields at fixed offsets, with everything
past it left as an opaque, unparsed tail. There is no tag, no length
prefix, nothing in the bytes themselves that says a field is there or what
it means — that knowledge exists only in the game's own source, or in a
real reader someone else already wrote from it, so a field's meaning is
never guessed at here, only looked up.

What IS generic is the mechanism: a ``HeaderLayout`` is a small,
declarative (name, label, struct format) list plus a detection rule, and
this module knows how to read, edit and write any of them the same way.
Adding the next documented game header is one more ``HeaderLayout`` entry
(see ``core.engines.known_headers``), not a new module, a new ``_Format``
subclass, or a new registry entry.
"""
import struct


class StructHeaderError(Exception):
    pass


class HeaderField:
    """One fixed-offset value in a header. *fmt* is a ``struct`` format
    character for a single value — ``<i``, ``<Q``, ``<h``, and so on."""

    __slots__ = ("name", "label", "fmt", "size")

    def __init__(self, name: str, label: str, fmt: str):
        self.name = name
        self.label = label
        self.fmt = fmt
        self.size = struct.calcsize(fmt)


class HeaderLayout:
    """One game's known, documented header shape.

    *matches* is called as ``matches(data, ext)`` and must be exact — every
    layout's own module says where its signature was confirmed against
    real save files, not guessed at.
    """

    def __init__(self, name: str, engine: str, source: str, fields, matches):
        self.name = name
        self.engine = engine
        self.source = source
        self.fields = tuple(fields)
        self.matches = matches
        self.header_len = sum(f.size for f in self.fields)


def find_layout(data: bytes, ext: str, layouts):
    """The first layout in *layouts* whose ``matches`` accepts *data*, or
    None. A layout's own matches() misbehaving on adversarial input is not
    grounds to crash detection for every other layout after it."""
    for layout in layouts:
        try:
            if layout.matches(data, ext):
                return layout
        except Exception:
            continue
    return None


class StructHeaderSave:
    """One header, read and written against a specific ``HeaderLayout``."""

    def __init__(self, layout: HeaderLayout):
        self.layout = layout
        self._values = {}
        self._tail = b""

    def load(self, data: bytes) -> None:
        if len(data) < self.layout.header_len:
            raise StructHeaderError(
                f"too short to hold a {self.layout.name} header")
        pos = 0
        values = {}
        for f in self.layout.fields:
            values[f.name] = struct.unpack_from(f.fmt, data, pos)[0]
            pos += f.size
        self._values = values
        self._tail = data[pos:]

    def dump(self) -> bytes:
        out = bytearray()
        for f in self.layout.fields:
            out += struct.pack(f.fmt, self._values[f.name])
        out += self._tail
        return bytes(out)

    def values(self) -> list:
        """(name, label, value) for every field, in header order."""
        return [(f.name, f.label, self._values[f.name])
                for f in self.layout.fields]

    def set_value(self, name: str, value) -> None:
        field = next((f for f in self.layout.fields if f.name == name), None)
        if field is None:
            raise StructHeaderError(f"no such field: {name}")
        new = int(value)
        try:
            struct.pack(field.fmt, new)
        except struct.error as e:
            raise StructHeaderError(
                f"{field.label} cannot hold {new}: {e}") from e
        self._values[name] = new
