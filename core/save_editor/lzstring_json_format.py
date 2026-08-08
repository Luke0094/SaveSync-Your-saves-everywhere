"""JSON squeezed with LZString and written as base64.

RPG Maker MV and a great many HTML games share this packing — what is
INSIDE tells them apart, so subclasses claim only the shape they recognise
and this generic reader takes whatever is left. The codec itself is
``core.engines.lzstring``; MV helpers also live in ``core.engines.rpgmaker``.
"""
import json

from .base import SaveEditorError
from .json_format import JsonFormat


class LzStringJsonFormat(JsonFormat):
    name = "LZString JSON"
    engine = "HTML game"

    def __init__(self):
        super().__init__()
        self.source_path = None

    def load(self, data: bytes) -> None:
        from core.engines.rpgmaker import RpgMakerError, mv_decompress
        try:
            text = mv_decompress(data)
        except RpgMakerError as e:
            raise SaveEditorError(str(e)) from e
        parsed = json.loads(text)
        if not self.claims(parsed):
            raise SaveEditorError(f"not a {self.name} save")
        self.data = parsed

    def claims(self, data) -> bool:
        return True

    def dump(self) -> bytes:
        from core.engines.rpgmaker import mv_compress
        text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return mv_compress(text)


# Back-compat alias used in detection order lists.
_LzStringJson = LzStringJsonFormat
