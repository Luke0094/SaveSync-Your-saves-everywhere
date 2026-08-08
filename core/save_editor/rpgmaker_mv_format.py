"""RPG Maker MV — the engine's whole game object, LZString-compressed.

Packing is ``core.engines.rpgmaker`` / ``lzstring``; this adapter only claims
MV-shaped JSON and exposes fields.
"""
from pathlib import Path

from .lzstring_json_format import LzStringJsonFormat


class RpgMakerMvFormat(LzStringJsonFormat):
    name = "RPG Maker MV"
    engine = "RPG Maker MV/MZ"

    # The engine writes its global objects at the top of every save.
    _MARKERS = {"party", "actors", "switches", "variables", "player"}

    def claims(self, data) -> bool:
        if self.source_path is not None and Path(self.source_path).suffix.lower() \
                in (".rpgsave", ".rmmzsave"):
            return True
        return isinstance(data, dict) and bool(self._MARKERS & set(data))
