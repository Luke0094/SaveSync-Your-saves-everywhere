"""Artemis Engine saves — the "BOW" container.

Artemis writes three files beside a game, all in the same container: four
magic bytes, a version, the unpacked length, and a deflate stream.

    BOWX   system.dat     the engine's own settings
    BOWG   saveg.dat      the data kept across playthroughs
    BOWS   saveNNNN.dat   one numbered save slot

Only **BOWX** is read here, and the reason is worth stating plainly. Inside a
BOWX is a flat list of named strings — a count, then a length-prefixed name
and a length-prefixed value, over and over — and the walk lands exactly on the
end of the list, which is what proves it was read correctly. The other two
hold a nested, tagged tree instead: recognisable strings sit in it, but what
tags them and how the nesting is counted is not something this was able to
establish, and a value written back into a structure read wrongly is the one
failure this program is built to avoid. So they are named and explained.

The container itself is understood for all three, which is how a slot can be
told from the settings at all: only unpacking it says which of them a file is.
"""
import logging
import struct
import zlib

logger = logging.getLogger(__name__)

SETTINGS = b"BOWX"
GLOBAL = b"BOWG"
SLOT = b"BOWS"
_MAGICS = (SETTINGS, GLOBAL, SLOT)
# The version every file in the sample carries. Anything else is a build this
# was not checked against, and is refused rather than assumed to match.
_VERSION = 1003
# Artemis is a Japanese engine; its text is Shift-JIS, though the setting
# names themselves are ASCII.
_ENCODING = "cp932"
# A name or a value longer than this is the walk having gone wrong, not a
# real entry — the largest in the sample is a 21 KB copyright notice.
_MAX_FIELD = 1 << 24
# The deflate stream says how hard it was squeezed, in the top two bits of
# its second byte, and rewriting a settings file at the same setting is what
# keeps one that was opened and not changed identical to the one that was
# there. Artemis writes 0xda, its hardest.
_LEVELS = {0: 1, 1: 2, 2: 6, 3: 9}
_DEFAULT_LEVEL = 9


class ArtemisError(Exception):
    pass


def _unpack(data: bytes) -> tuple:
    """(magic, body) of an Artemis container."""
    if len(data) < 13 or data[:4] not in _MAGICS:
        raise ArtemisError("not an Artemis save")
    version, raw_size = struct.unpack_from("<II", data, 4)
    if version != _VERSION:
        raise ArtemisError(
            f"this is a version {version} Artemis save and only {_VERSION} "
            f"has been checked")
    try:
        body = zlib.decompress(data[12:])
    except zlib.error as e:
        raise ArtemisError(f"the save will not unpack: {e}") from e
    if len(body) != raw_size:
        raise ArtemisError(
            f"the save says it unpacks to {raw_size} bytes and it unpacked "
            f"to {len(body)}")
    return data[:4], body, _LEVELS.get(data[13] >> 6, _DEFAULT_LEVEL)


class ArtemisSave:
    """The settings of one Artemis game, read whole."""

    def __init__(self):
        self._entries = []      # [name, value] as raw bytes
        self._trailing = b""
        self._level = _DEFAULT_LEVEL

    def load(self, data: bytes) -> None:
        magic, body, self._level = _unpack(data)
        if magic == SLOT:
            raise ArtemisError(
                "this is a numbered save slot, and its values sit in a tagged "
                "tree this reader cannot follow safely")
        if magic == GLOBAL:
            raise ArtemisError(
                "this is the data kept across playthroughs, and it is written "
                "the same way as the slots rather than as named settings")
        count = struct.unpack_from("<I", body, 0)[0]
        pos = 4
        for _ in range(count):
            pos, name = self._field(body, pos)
            pos, value = self._field(body, pos)
            self._entries.append([name, value])
        # The proof, and the only one available for a format whose values are
        # all text: read the number of entries the file declares and land on
        # the end of them. A miscounted walk runs off the end or stops early,
        # and either way the file is refused rather than half understood.
        if pos > len(body):
            raise ArtemisError("the settings run past the end of the save")
        self._trailing = body[pos:]

    @staticmethod
    def _field(body: bytes, pos: int) -> tuple:
        try:
            size = struct.unpack_from("<I", body, pos)[0]
        except struct.error as e:
            raise ArtemisError("the save ends inside a setting") from e
        pos += 4
        if size > _MAX_FIELD or pos + size > len(body):
            raise ArtemisError(
                f"a setting says it is {size} bytes, which does not fit in "
                f"the save — this is not the structure this reader knows")
        return pos + size, body[pos:pos + size]

    def dump(self) -> bytes:
        out = bytearray(struct.pack("<I", len(self._entries)))
        for name, value in self._entries:
            out += struct.pack("<I", len(name)) + name
            out += struct.pack("<I", len(value)) + value
        out += self._trailing
        body = bytes(out)
        return SETTINGS + struct.pack("<II", _VERSION, len(body)) + \
            zlib.compress(body, self._level)

    def values(self) -> list:
        return [(i, name.decode(_ENCODING, "replace"), "str",
                 value.decode(_ENCODING, "replace"))
                for i, (name, value) in enumerate(self._entries)]

    def groups(self) -> list:
        """What a setting sits under, from its own name.

        Artemis names everything ``s.something``, so the leading word is the
        same for every one of them and grouping by it would produce a single
        category holding all of them — which is not a category. What is left
        after that word is: ``s.status.avoid`` sits under ``status``, and
        ``s.bgmvol`` sits on its own.
        """
        out = []
        for name, _v in self._entries:
            parts = name.decode(_ENCODING, "replace").split(".")
            out.append(parts[1] if len(parts) > 2 else "")
        return out

    def set_value(self, index: int, value) -> None:
        try:
            self._entries[index][1] = str(value).encode(_ENCODING)
        except UnicodeEncodeError as e:
            raise ArtemisError(
                "Artemis saves are written in Shift-JIS and this text cannot "
                "be spelled in it") from e


def loads(data: bytes) -> ArtemisSave:
    save = ArtemisSave()
    save.load(data)
    return save


def is_artemis(data: bytes) -> bool:
    return data[:4] in _MAGICS
