"""Wolf RPG saves: the values inside them.

``core/wolf`` unlocks the file. This reads what is in it.

A Wolf save ends with a *variable database*: one record per database type,
each carrying its own field layout followed by the rows. Crucially that
layout is stored IN THE SAVE — a list of field codes, 1000 and up for numbers
and 2000 and up for strings — so the values can be read without the game at
all. The game's ``CDataBase.project`` is needed only to put NAMES on them, and
plenty of released games pack it away inside ``Data.wolf``, so it is optional
here.

Finding where that database starts is the interesting part. The save's
earlier sections are a long reverse-engineered walk, and porting all of them
to reach the last one would be a great deal of code to trust. Instead the
database is located by its own shape: a byte, a type count, and then records
that must parse cleanly and finish within a few bytes of the end of the file.
A wrong guess desynchronises almost immediately and lands nowhere near.

That test is not quite unique on its own — one save here also parses cleanly
at a second, later offset — so the earliest offset wins. A false start has to
begin LATER than the real one to still reach the end, which makes "earliest"
the reliable tie-break, and the project file's own type count settles it
outright when the file is there.
"""
import logging
import re
import struct
from collections import Counter
from pathlib import Path

from core.wolf import WolfError, decrypt, encrypt, fix_checksum, START_OFFSET

logger = logging.getLogger(__name__)

# Field codes below this are numbers; from here up they are strings.
_STRING_FROM = 2000
# How close to the end of the file a correct reading must land. Wolf writes a
# short trailing section after the database; on every save examined it was
# two bytes.
_TAIL_SLACK = 8
# Type counts are small. Searching past this is looking for coincidences.
_MAX_TYPES = 255
# How many types in a row may hold no fields before a reading is taken to be
# a wrong offset rather than a database. Real ones do have unused slots; the
# longest run measured was one, so this is sixteen times the observed worst.
_MAX_EMPTY_RUN = 16
# Guards so a wrong offset gives up immediately instead of allocating.
_MAX_FIELDS = 100_000
_MAX_ROWS = 100_000
_MAX_STRING = 1 << 20
# Text in a Wolf save is Shift-JIS unless the header says otherwise.
_UTF8_FLAG_AT, _UTF8_FLAG = 6, 0x55
# Field codes seen in real databases run 1000+ for numbers and 2000+ for
# strings. Requiring that range rejects essentially every wrong offset on its
# first field — a random four bytes lands in it about once in two million —
# which is what makes searching the whole file affordable.
_CODE_LOW, _CODE_HIGH = 1000, 3000
# A type count, little-endian, capped at _MAX_TYPES: one non-zero byte and
# three zeroes.
_CANDIDATE = re.compile(rb"[\x01-\xff]\x00\x00\x00")


# Game databases already parsed, by path, size and modification time. The
# database belongs to the game rather than to any one save, and holding a
# value asks for it again on every round.
_PROJECTS = {}
_PROJECT_KEEP = 32


def _needle_offsets(data: bytes, count: int):
    """Every place *count* appears as a little-endian word, in order."""
    needle = struct.pack("<I", count)
    at = data.find(needle, START_OFFSET)
    while at >= 0:
        yield at, count
        at = data.find(needle, at + 1)


class WolfSaveError(WolfError):
    pass


class _Cursor:
    __slots__ = ("d", "o")

    def __init__(self, data: bytes, offset: int = 0):
        self.d = data
        self.o = offset

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        return v

    def blob(self) -> bytes:
        n = self.u32()
        if n > _MAX_STRING:
            raise WolfSaveError("string length out of range")
        raw = self.d[self.o:self.o + n]
        if len(raw) != n:
            raise WolfSaveError("string runs past the end")
        self.o += n
        return raw


def read_project(path) -> list:
    """The game's own database: (type name, field names, row names) per type.

    Only used for labels. A game that packs this away simply gets numbered
    labels instead.
    """
    data = Path(path).read_bytes()
    c = _Cursor(data)

    def text() -> str:
        return c.blob().rstrip(b"\x00").decode("cp932", errors="replace")

    if struct.unpack_from("<I", data, 0)[0] > 0xFF:
        raise WolfSaveError("this database is encrypted")
    types = []
    for _ in range(c.u32()):
        name = text()
        fields = [text() for _ in range(c.u32())]
        rows = [text() for _ in range(c.u32())]
        text()                                   # description
        # Two statements on purpose. "c.o += c.u32()" reads c.o BEFORE
        # evaluating the right-hand side, so the four bytes the length itself
        # occupies get un-consumed by the assignment — the cursor ends up
        # four bytes short, every type, and the file desynchronises later on
        # in a way that looks like a corrupt string.
        type_list_size = c.u32()
        c.o += type_list_size                    # the field type list
        for _ in range(c.u32()):
            text()
        for _ in range(c.u32()):
            for _ in range(c.u32()):
                text()
        for _ in range(c.u32()):
            for _ in range(c.u32()):
                c.u32()
        for _ in range(c.u32()):
            c.u32()
        types.append((name, fields, rows))
    if c.o != len(data):
        raise WolfSaveError("the database did not end where it should")
    return types


def find_project(save_path) -> "list | None":
    """The game database that belongs to a save, if it is lying about."""
    base = Path(save_path).parent
    for rel in ("../Data/BasicData/CDataBase.project",
                "Data/BasicData/CDataBase.project"):
        candidate = (base / rel).resolve()
        try:
            if not candidate.is_file():
                continue
            st = candidate.stat()
            key = (str(candidate).lower(), st.st_size, st.st_mtime_ns)
            if key not in _PROJECTS:
                if len(_PROJECTS) >= _PROJECT_KEEP:
                    _PROJECTS.clear()
                _PROJECTS[key] = read_project(candidate)
            return _PROJECTS[key]
        except (OSError, WolfSaveError, struct.error) as e:
            logger.debug(f"Could not read {candidate}: {e}")
    return None


class WolfValues:
    """The variable database of one Wolf save, opened for editing."""

    def __init__(self):
        self.plain = b""
        self._records = []        # dicts: label, kind, value, offset, length
        self._encoding = "cp932"
        self._db_offset = -1
        self._type_count = 0
        self._parse_end = 0

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, raw: bytes, project=None) -> None:
        self.plain = decrypt(raw)
        if self.plain[START_OFFSET] != 0x19:
            raise WolfSaveError("not a Wolf RPG save")
        if len(self.plain) > _UTF8_FLAG_AT and self.plain[_UTF8_FLAG_AT] == _UTF8_FLAG:
            self._encoding = "utf-8"

        best = self._locate(len(project) if project else 0)
        if best is None:
            raise WolfSaveError(
                "could not find the variable database in this save")
        self._db_offset, self._type_count, self._records = best
        self._label(project)
        logger.info(f"Wolf save: {len(self._records)} values, database at "
                    f"0x{self._db_offset:x} ({self._type_count} types)")

    def _locate(self, want: int):
        """Where the database starts, found by its shape.

        *want* is the type count the game's own database says to expect, or 0
        when that file is not around and any count will be considered.

        Every candidate is a small number stored little-endian, so — with the
        count capped at _MAX_TYPES = 255 — it is one non-zero byte followed by
        three zero bytes. One regular expression finds every one of them in a
        single pass at C speed, in increasing order, which is also the order
        the tie-break wants: the first that parses cleanly is the earliest,
        so it can be returned on the spot.
        """
        data = self.plain
        n = len(data)
        if want > _MAX_TYPES:
            # A game with more types than fit in a byte. Rare enough that the
            # plain search is the right answer rather than a wider pattern.
            spots = _needle_offsets(data, want)
        else:
            spots = ((m.start(), data[m.start()])
                     for m in _CANDIDATE.finditer(data, START_OFFSET))
        for at, count in spots:
            if at < 1 or (want and count != want):
                continue
            try:
                records = self._parse_db(at - 1, count)
            except (WolfSaveError, struct.error, IndexError):
                continue
            # A database with nothing in it is not the one we are looking for.
            # Types whose rows hold no fields consume no bytes, so a run of
            # them can end up anywhere at all, including two bytes from the
            # end — which is otherwise exactly what a correct reading looks
            # like.
            if not records:
                continue
            # A correct reading consumes the database and stops just short of
            # the end of the file. _parse_db leaves the cursor on the instance
            # because the last record is not necessarily the last thing in it.
            if n - self._parse_end <= _TAIL_SLACK:
                return (at - 1, count, records)
        return None

    def _parse_db(self, offset: int, type_count: int) -> list:
        c = _Cursor(self.plain, offset + 1)      # the byte Wolf skips
        if c.u32() != type_count:
            raise WolfSaveError("type count does not match")
        out = []
        empty_run = 0
        for t in range(type_count):
            unknown = c.i32()
            field_count = unknown
            if unknown <= -1:
                if unknown <= -2:
                    c.i32()                      # data-id specification
                field_count = c.u32()
            config = []
            if field_count > 0:
                if field_count > _MAX_FIELDS:
                    raise WolfSaveError("field count out of range")
                # Checked as they are read, not after. A save is full of small
                # numbers, so a wrong offset routinely claims a field count of
                # fifty or a hundred; reading them all and judging afterwards
                # costs a hundred times what stopping at the first bad one
                # does, on every wrong offset in the file.
                for _ in range(field_count):
                    code = c.u32()
                    if not _CODE_LOW <= code < _CODE_HIGH:
                        raise WolfSaveError("field code out of range")
                    config.append(code)
            rows = c.u32()
            if rows > _MAX_ROWS:
                raise WolfSaveError("row count out of range")
            if not config:
                # No fields means the rows hold nothing and consume no bytes.
                # Walking them would spin the row count times over an
                # unmoving cursor, which on a wrong offset is unbounded work
                # for a reading that was never going to be accepted.
                #
                # An empty type also advances the cursor by only its header,
                # so a wrong offset that reads as nothing but empty types
                # strolls through every type it claims to have and costs more
                # than every real candidate put together. Real databases do
                # have empty types — unused slots — but never many in a row:
                # the longest run in the games measured was one.
                empty_run += 1
                if empty_run > _MAX_EMPTY_RUN:
                    raise WolfSaveError("nothing but empty types")
                continue
            empty_run = 0
            for r in range(rows):
                # Wolf writes every number first, then every string.
                for f, code in enumerate(config):
                    if code < _STRING_FROM:
                        out.append({"type": t, "row": r, "field": f,
                                    "kind": "int", "value": c.i32(),
                                    "offset": c.o - 4, "length": 4})
                for f, code in enumerate(config):
                    if code >= _STRING_FROM:
                        at = c.o
                        raw = c.blob()
                        out.append({"type": t, "row": r, "field": f,
                                    "kind": "str",
                                    "value": raw.rstrip(b"\x00").decode(
                                        self._encoding, errors="replace"),
                                    "offset": at, "length": c.o - at})
            if c.o > len(self.plain):
                raise WolfSaveError("ran past the end of the save")
        self._parse_end = c.o
        return out

    def _label(self, project) -> None:
        counts = Counter()
        for rec in self._records:
            t, r, f = rec["type"], rec["row"], rec["field"]
            if project and t < len(project):
                name, fields, rows = project[t]
                row = rows[r] if r < len(rows) and rows[r] else f"row {r}"
                field = fields[f] if f < len(fields) and fields[f] else f"field {f}"
                rec["label"] = f"{name or f'type {t}'} / {row} / {field}"
            else:
                rec["label"] = f"type {t} / row {r} / field {f}"
            counts[rec["label"]] += 1
        # Wolf reuses one name across a whole block of fields — a row of a
        # hundred resistances every one of which is called the same thing.
        # Labels have to come out unique: a held value is found again by its
        # label on every round, and an ambiguous one is skipped rather than
        # guessed at, which would quietly turn holding into nothing at all.
        # Only the repeats carry coordinates, so names that already read
        # clearly are left alone.
        for rec in self._records:
            if counts[rec["label"]] > 1:
                rec["label"] += f"  #{rec['type']}.{rec['row']}.{rec['field']}"

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(i, rec["label"], rec["kind"], rec["value"])
                for i, rec in enumerate(self._records)]

    def set_value(self, index: int, value) -> None:
        rec = self._records[index]
        if rec["kind"] == "int":
            rec["new"] = struct.pack("<i", int(value))
        else:
            raw = str(value).encode(self._encoding, errors="replace")
            # Wolf stores a length and the bytes, with the terminator counted.
            rec["new"] = struct.pack("<I", len(raw) + 1) + raw + b"\x00"
        rec["value"] = value

    def dump(self) -> bytes:
        """Splice the edits in, put the checksum right, and lock it again."""
        edits = [r for r in self._records if "new" in r]
        out = self.plain
        # Back to front, so an edit never moves an offset still to be used.
        for rec in sorted(edits, key=lambda r: r["offset"], reverse=True):
            o, n = rec["offset"], rec["length"]
            out = out[:o] + rec["new"] + out[o + n:]
        return encrypt(fix_checksum(out))


def loads(raw: bytes, save_path=None) -> WolfValues:
    """Read a Wolf save, using anything the game beside it can tell us."""
    save = WolfValues()
    save.load(raw, find_project(save_path) if save_path else None)
    return save
