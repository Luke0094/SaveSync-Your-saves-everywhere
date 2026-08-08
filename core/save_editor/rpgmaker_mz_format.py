"""RPG Maker MZ — JSON behind the engine's zlib / UTF-8 wrap.

Packing lives in ``core.engines.rpgmaker``; this adapter only exposes the
JSON fields the dialog edits.
"""
from .base import SaveEditorError
from .json_format import JsonFormat


class RpgMakerMzFormat(JsonFormat):
    name = "RPG Maker MZ"
    engine = "RPG Maker MV/MZ"
    verify_exact = True

    def __init__(self):
        super().__init__()
        self._wrapped = True

    def load(self, data: bytes) -> None:
        from core.engines.rpgmaker import RpgMakerError, mz_decompress
        try:
            plain, self._wrapped = mz_decompress(data)
        except RpgMakerError as e:
            raise SaveEditorError(str(e)) from e
        super().load(plain)

    def dump(self) -> bytes:
        from core.engines.rpgmaker import mz_compress
        return mz_compress(super().dump(), self._wrapped)
