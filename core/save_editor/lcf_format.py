from .base import SaveEditorError, SaveField, _Format

class LcfFormat(_Format):
    """RPG Maker 2000/2003 ``.lsd``. See core/lcf: chunks this reader does not
    understand travel as their own bytes, so the file rebuilds exactly."""
    name = "RPG Maker 2000/2003"
    engine = "RPG Maker 2000/2003"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.lcf import LcfError, loads
        try:
            self._save = loads(data)
        except LcfError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.lcf import LcfError
        try:
            self._save.set_value(path[0], value)
        except LcfError as e:
            raise SaveEditorError(str(e)) from e


