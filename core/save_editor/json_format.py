import json

from .base import SaveField, _Format, _walk

class JsonFormat(_Format):
    """Plain JSON — Unity, many indie engines, and anything sane.

    When the file's own indent / newlines can be reproduced, the round trip
    is byte-exact (``verify_exact`` flips on after load). Compact one-liners
    and odd spacing fall back to value equality, which is what the open
    gate checks for every non-exact format.
    """
    name = "JSON"
    engine = "JSON"
    verify_exact = False

    def __init__(self):
        self.data = None
        self._encoding = "utf-8"
        self._bom = False
        self._indent = None          # None = compact
        self._newline = "\n"
        self._trailing_nl = False
        self.verify_exact = False

    def load(self, data: bytes) -> None:
        self._bom = data.startswith(b"\xef\xbb\xbf")
        text = data.decode("utf-8-sig" if self._bom else "utf-8")
        self._trailing_nl = text.endswith("\n")
        self._newline = "\r\n" if "\r\n" in text else "\n"
        # Pretty-printed files use a 2- or 4-space indent after the first
        # newline; everything else is written compact.
        self._indent = None
        if "\n" in text:
            for candidate in (4, 2):
                if f"\n{' ' * candidate}\"" in text.replace("\r\n", "\n") \
                        or f"\n{' ' * candidate}{{" in text.replace("\r\n", "\n") \
                        or f"\n{' ' * candidate}[" in text.replace("\r\n", "\n"):
                    self._indent = candidate
                    break
            if self._indent is None and text.lstrip()[:1] in "{[":
                # Multiline but no obvious indent — still prefer indent=2 over
                # crushing to one line when the source had newlines.
                self._indent = 2
        self.data = json.loads(text)
        # Prefer the byte-exact gate when our style rebuild matches the file.
        self.verify_exact = (self.dump() == data)

    def dump(self) -> bytes:
        if self._indent is not None:
            text = json.dumps(self.data, ensure_ascii=False, indent=self._indent)
            if self._newline == "\r\n":
                text = text.replace("\n", "\r\n")
        else:
            text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        if self._trailing_nl and not text.endswith(("\n", "\r\n")):
            text += self._newline
        raw = text.encode("utf-8")
        return (b"\xef\xbb\xbf" + raw) if self._bom else raw

    def fields(self) -> list:
        return _walk(self.data)

    def set_field(self, path: tuple, value) -> None:
        node = self.data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value


