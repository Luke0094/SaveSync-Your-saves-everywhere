from .base import SaveEditorError, SaveField, _Format

class RagsFormat(_Format):
    """RAGS (``.rsv``) — a .NET object graph behind AES.

    See core/rags: the whole save is read, but only the objects holding game
    state are offered. The rest is the game's own logic and presentation, and
    there are three million values of it.
    """
    name = "RAGS"
    engine = "Rapid Adventure Game System"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.rags import RagsError, loads
        try:
            self._save = loads(data)
        except RagsError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.rags import RagsError
        try:
            self._save.set_value(path[0], value)
        except RagsError as e:
            raise SaveEditorError(str(e)) from e


