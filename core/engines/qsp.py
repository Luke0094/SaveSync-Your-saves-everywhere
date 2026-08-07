"""QSP (Quest Soft Player) save files.

Taken from the QSP engine itself (github.com/QSPFoundation/qsp, ``qsp/game.c``
and ``qsp/coding.c``). The editor the repository list pointed at does not
parse this format — it is a P/Invoke wrapper that hands the file to
``qsplib.dll`` — so the engine's own writer is the only description there is.

A save is a list of ``\\r\\n``-separated lines, each obfuscated by shifting
every character down by five. The layout is POSITIONAL: counts drive loops,
and the variables sit at the end in 512 buckets.

That last point is why this module checks something the other formats do not
need. Everywhere else, a misread structure fails to rebuild and the file is
refused. Here the file is lines of text, so it would rebuild perfectly no
matter how badly the walk went — and a value written to the wrong line would
pass silently. The guarantee here is instead that the walk **consumes every
line exactly**: it follows every count from the header to the last variable
bucket and must land precisely on the end of the file. A structure read
wrongly runs out of lines or leaves some over, and the save is refused.
"""
import logging

logger = logging.getLogger(__name__)

DELIM = "\r\n"
SAVE_ID = "QSPSAVEDGAME"
# qsp/coding.h: every character is shifted by this on the way out.
SHIFT = 5
# qsp/declarations.h
GLOBAL_BUCKETS = 512
# qsp/bindings/qsp.h — the base type a value's type maps to.
_BASE_TUPLE, _BASE_NUM, _BASE_STR = 0, 1, 3
_BASE_TYPE = {0: _BASE_TUPLE, 1: _BASE_NUM, 2: _BASE_NUM,
              3: _BASE_STR, 4: _BASE_STR, 5: _BASE_STR, 6: _BASE_STR}


class QspError(Exception):
    pass


def decode_line(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        # The engine's special case, mirrored: what it writes as -5 came in
        # as 5, and everything else was simply shifted down.
        if code in (0xFFFB, 0xFB):
            out.append(chr(SHIFT))
        else:
            out.append(chr((code + SHIFT) & 0xFFFF))
    return "".join(out)


def encode_line(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == SHIFT:
            out.append(chr(0xFFFB))
        else:
            out.append(chr((code - SHIFT) & 0xFFFF))
    return "".join(out)


class QspSave:
    """One QSP save, walked line by line."""

    def __init__(self):
        self._lines = []          # decoded
        self._ucs2 = True
        self._values = []         # name, kind, value, line index

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, data: bytes) -> None:
        text, self._ucs2 = _to_text(data)
        raw = text.split(DELIM)
        if not raw or decode_line(raw[0]) and raw[0] != SAVE_ID:
            # The id and the version are written unencoded.
            if raw[0] != SAVE_ID:
                raise QspError("not a QSP save")
        self._lines = [raw[0], raw[1] if len(raw) > 1 else ""] + \
                      [decode_line(x) for x in raw[2:]]
        self._values = self._walk()

    def _int(self, i: int) -> int:
        try:
            return int(self._lines[i])
        except (IndexError, ValueError) as e:
            raise QspError(f"line {i} should have been a number") from e

    def _walk(self) -> list:
        """Follow every count from the header to the last bucket.

        Landing anywhere but the exact end of the file means the structure
        was not what this reader thinks it is — see the module docstring.
        """
        n = len(self._lines)
        i = 2                                   # id and version are lines 0, 1
        i += 11                                 # the fixed header values
        out = []

        def count_at(idx):
            if idx >= n:
                raise QspError("the save ends earlier than its counts say")
            return self._int(idx)

        for _ in range(2):                      # playlist files, include files
            c = count_at(i); i += 1
            i += c
        acts = count_at(i); i += 1
        for _ in range(acts):
            i += 2                              # desc, image
            lines = count_at(i); i += 1
            i += lines * 2                      # (text, line number) each
            i += 2                              # location, action index
        objs = count_at(i); i += 1
        i += objs * 2                           # name, image
        groups = count_at(i); i += 1
        i += groups * 5

        for _bucket in range(GLOBAL_BUCKETS):
            vars_here = count_at(i); i += 1
            for _v in range(vars_here):
                name = self._lines[i] if i < n else ""
                i += 1
                vals = count_at(i); i += 1
                for _k in range(vals):
                    i, entry = self._read_variant(i, n, name)
                    if entry:
                        out.append(entry)
                inds = count_at(i); i += 1
                i += inds * 2                   # index, text
        if i != n:
            raise QspError(
                f"the walk ended at line {i} of {n} — this save's structure "
                f"is not the one this reader knows")
        return out

    def _read_variant(self, i: int, n: int, name: str):
        if i >= n:
            raise QspError("a value runs past the end of the save")
        type_id = self._int(i)
        i += 1
        base = _BASE_TYPE.get(type_id)
        if base is None:
            raise QspError(f"value type {type_id} is not one this reader knows")
        if base == _BASE_TUPLE:
            count = self._int(i); i += 1
            for _ in range(count):
                i, _e = self._read_variant(i, n, name)
            return i, None
        if base == _BASE_NUM:
            entry = {"name": name, "kind": "int",
                     "value": self._int(i), "line": i}
            return i + 1, entry
        entry = {"name": name, "kind": "str",
                 "value": self._lines[i] if i < n else "", "line": i}
        return i + 1, entry

    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        return [(k, v["name"], v["kind"], v["value"])
                for k, v in enumerate(self._values)]

    def set_value(self, index: int, value) -> None:
        v = self._values[index]
        self._lines[v["line"]] = (str(int(value)) if v["kind"] == "int"
                                  else str(value))
        v["value"] = value

    def dump(self) -> bytes:
        parts = [self._lines[0], self._lines[1]] + \
                [encode_line(x) for x in self._lines[2:]]
        text = DELIM.join(parts)
        if self._ucs2:
            return text.encode("utf-16-le")
        return text.encode("cp1251", errors="replace")


def _to_text(data: bytes):
    """QSP writes either UTF-16 or a single-byte codepage. Null bytes in the
    first stretch give the wide form away."""
    head = data[:64]
    if b"\x00" in head:
        return data.decode("utf-16-le", errors="replace"), True
    return data.decode("cp1251", errors="replace"), False


def loads(data: bytes) -> QspSave:
    save = QspSave()
    save.load(data)
    return save
