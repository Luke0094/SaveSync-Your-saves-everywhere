"""``key = value`` text — config-style saves, and plenty of small games.

An edit rewrites only the value on its own line: the rest of the file —
comments, ordering, spacing, blank lines, line endings — is carried through
as the exact characters it arrived as. That makes the round trip exact by
construction rather than by luck.
"""
import re


class KeyValueError(ValueError):
    pass


# The only control characters that belong in configuration text.
_TEXT_CONTROLS = ("\t", "\n", "\r")
# How much of a file to look at before believing it is text. A config is
# small; this is far more than enough to catch binary pretending to be one.
_SNIFF = 1 << 16

_LINE = re.compile(
    r"^(?P<head>\s*(?P<key>[A-Za-z_][\w .\-]*)\s*[=:]\s*)"
    r"(?P<value>.*?)(?P<eol>\r?\n?)$")
_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*\r?\n?$")


def _typed(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return "bool", low == "true"
    try:
        return "int", int(raw)
    except ValueError:
        pass
    try:
        return "float", float(raw)
    except ValueError:
        pass
    return "str", raw


class KeyValueDoc:
    """One key/value text file opened for editing."""

    def __init__(self):
        self._lines = []
        self._entries = []       # (line_index, key, kind, value, section)
        self._bom = False

    def load(self, data: bytes) -> None:
        self._bom = data.startswith(b"\xef\xbb\xbf")
        text = data.decode("utf-8-sig" if self._bom else "utf-8")
        if "\n" not in text:
            # One long line is a blob, not a config — base64 saves land here.
            raise KeyValueError("not a key/value file")
        # Nor is compressed data that merely happened to decode as UTF-8.
        # Without this the editor invents a dozen entries out of noise, with
        # unreadable names and values, instead of saying it cannot read the
        # file — which is what a player reports as "the text is mangled".
        if any(ch < " " and ch not in _TEXT_CONTROLS
               for ch in text[:_SNIFF]):
            raise KeyValueError("not a key/value file")
        self._lines = text.splitlines(keepends=True)
        section = ""
        self._entries = []
        for i, line in enumerate(self._lines):
            sec = _SECTION.match(line)
            if sec:
                section = sec.group("name")
                continue
            if line.lstrip().startswith(("#", ";", "//")):
                continue
            m = _LINE.match(line)
            if not m:
                continue
            raw = m.group("value").strip()
            kind, value = _typed(raw)
            self._entries.append(
                (i, m.group("key").strip(), kind, value, section))
        if len(self._entries) < 2:
            raise KeyValueError("not a key/value file")

    def dump(self) -> bytes:
        raw = "".join(self._lines).encode("utf-8")
        return (b"\xef\xbb\xbf" + raw) if self._bom else raw

    def values(self) -> list:
        """(line_index, key, kind, value, section) for every entry."""
        return list(self._entries)

    def set_value(self, line_index: int, value) -> None:
        for idx, (n, key, kind, _old, section) in enumerate(self._entries):
            if n != line_index:
                continue
            m = _LINE.match(self._lines[n])
            if kind == "bool":
                text = "true" if value else "false"
            else:
                text = str(value)
            self._lines[n] = m.group("head") + text + m.group("eol")
            self._entries[idx] = (n, key, kind, value, section)
            return


def loads(data: bytes) -> KeyValueDoc:
    doc = KeyValueDoc()
    doc.load(data)
    return doc
