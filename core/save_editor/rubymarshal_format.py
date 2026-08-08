"""Ruby Marshal — thin adapter over ``core.engines.rubymarshal``."""
from .base import SaveEditorError, SaveField, _Format


class RubyMarshalFormat(_Format):
    """RPG Maker XP / VX / VX Ace, and anything else Ruby dumped.

    See ``core.engines.rubymarshal``: several Marshal streams back to back,
    walked as a graph of arrays, hashes and objects.
    """
    name = "Ruby Marshal"
    engine = "RPG Maker XP/VX/VX Ace"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.rubymarshal import MarshalError, open_save
        try:
            self._save = open_save(data)
        except MarshalError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField(path, label, kind, value, group)
                for path, label, kind, value, group in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path, value)
