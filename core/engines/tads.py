"""TADS / TAD-kit ``system.rec`` slots.

Whitespace-separated tokens, NUL-padded to a fixed size. Values are edited
in place; the padding length is preserved on write.
"""
import re


class TadsError(ValueError):
    pass


# Integers, letter-prefixed codes (q0, f12), and bare letter flags (qf).
_TOKEN = re.compile(r"^[A-Za-z]*-?\d*$")


class TadsRec:
    """One ``system.rec`` buffer opened for editing."""

    def __init__(self):
        self._tokens: list[str] = []
        self._pad = 0
        self._size = 0

    def load(self, data: bytes) -> None:
        if not data or data[:1] not in b"0123456789":
            raise TadsError("not a TADS record")
        self._size = len(data)
        stripped = data.rstrip(b"\x00")
        self._pad = len(data) - len(stripped)
        if self._pad < 1 and len(data) != 2048:
            # Live slots are typically 2048 bytes with a NUL tail; without
            # that shape this is almost certainly something else.
            raise TadsError("not a TADS record")
        try:
            text = stripped.decode("ascii")
        except UnicodeDecodeError as e:
            raise TadsError("not a TADS record") from e
        tokens = text.split()
        if len(tokens) < 8 or not all(
                t and _TOKEN.match(t) for t in tokens):
            raise TadsError("not a TADS record")
        self._tokens = tokens

    def dump(self) -> bytes:
        body = " ".join(self._tokens).encode("ascii")
        if len(body) > self._size:
            # A slot is a FIXED-size buffer (load() rejects anything else —
            # see its own "not a TADS record" gate on non-2048 sizes with no
            # padding to spare), edited in place with the padding shrinking
            # to absorb growth. There's no reserve past that for a value
            # that grows enough to eat the padding entirely — e.g. a score
            # or counter pushed to a much longer digit string. Silently
            # writing a longer-than-original buffer here would have gone
            # straight to disk with no check: save_editor.py's write path
            # calls dump() and writes the bytes directly, no round-trip
            # verification in between. Raising here, before any bytes are
            # written, is what actually stops that — the caller sees a
            # clear failure instead of a corrupted save.
            raise TadsError(
                f"edited value(s) grew the record by "
                f"{len(body) - self._size} byte(s) past its original "
                f"{self._size}-byte size — this slot has no room left")
        if self._size > len(body):
            return body + (b"\x00" * (self._size - len(body)))
        return body

    def values(self) -> list:
        """(index, label, kind, value) for every token."""
        out = []
        for i, tok in enumerate(self._tokens):
            if re.fullmatch(r"-?\d+", tok):
                out.append((i, f"v{i}", "int", int(tok)))
            else:
                out.append((i, f"v{i}", "str", tok))
        return out

    def set_value(self, index: int, value) -> None:
        if isinstance(value, bool):
            self._tokens[index] = "1" if value else "0"
        elif isinstance(value, int):
            # Keep a letter prefix when the slot had one (q12, f0, …).
            m = re.match(r"^([A-Za-z]+)", self._tokens[index])
            prefix = m.group(1) if m and not re.fullmatch(
                r"-?\d+", self._tokens[index]) else ""
            # Bare letter flags stay letters unless a digit value is forced.
            if prefix and not re.search(r"\d", self._tokens[index]):
                self._tokens[index] = (
                    f"{prefix}{int(value)}" if value else prefix)
            else:
                self._tokens[index] = f"{prefix}{int(value)}"
        else:
            self._tokens[index] = str(value)


def loads(data: bytes) -> TadsRec:
    rec = TadsRec()
    rec.load(data)
    return rec
