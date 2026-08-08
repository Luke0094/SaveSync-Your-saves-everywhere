"""Wolf RPG Editor — binary save adapter for the Save Editor UI.

Obfuscation primitives live in ``core.engines.wolf``; unlock + variable
database parsing live in ``crypt.wolf``. This module only bridges that into
the shared ``SaveField`` list the dialog expects.
"""
import struct

from .base import SaveEditorError, SaveField, _Format


class WolfFormat(_Format):
    """Wolf RPG Editor saves.

    The file is obfuscated and its values live in a variable database near
    the end (``crypt.wolf``). Field names come from the game's own database
    when it is lying about; a game that packs it away still gets every
    value, just numbered.
    """
    name = "Wolf RPG"
    engine = "Wolf RPG Editor"

    def __init__(self):
        self._save = None
        self.source_path = None

    def load(self, data: bytes) -> None:
        from core.engines.wolf import WolfError
        from core.save_editor.crypt.wolf import loads
        try:
            self._save = loads(data, save_path=self.source_path)
        except (WolfError, struct.error, IndexError, ValueError) as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField((i,), name, kind, value, name.split(" / ")[0])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)
