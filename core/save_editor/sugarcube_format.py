"""Twine's SugarCube — the HTML game format behind a lot of browser games.

It keeps the whole play history: an index into it, and a chain of deltas
holding the variables at each step. Packed with the same LZString base64
as RPG Maker MV; the shape inside is what claims the file.
"""
from .lzstring_json_format import LzStringJsonFormat


class SugarCubeFormat(LzStringJsonFormat):
    name = "Twine (SugarCube)"
    engine = "Twine / SugarCube"

    def claims(self, data) -> bool:
        state = data.get("state") if isinstance(data, dict) else None
        return isinstance(state, dict) and bool({"index", "delta", "history"}
                                                & set(state))
