from .base import SaveEditorError, SaveField, _Format

class AliceSoftFormat(_Format):
    """AliceSoft System 4 global data (``.asd``, and ``.sav``).

    See core/alicesoft, which is written from the engine reimplementation the
    format is described in rather than from staring at bytes. Two different
    things arrive in the same container: the global data, which is named and
    typed, and the numbered save slots, which are a dump of the virtual
    machine. Both open — from a slot it is the game's own global variables
    that are offered, and with the game in the library they carry the names
    the game gave them.
    """
    name = "AliceSoft System 4"
    engine = "AliceSoft System"

    def __init__(self):
        self._save = None
        self.game_dir = None

    def load(self, data: bytes) -> None:
        from core.engines.alicesoft import AliceSoftError, loads
        try:
            self._save = loads(data, game_dir=self.game_dir)
        except AliceSoftError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.alicesoft import AliceSoftError
        try:
            self._save.set_value(path[0], value)
        except AliceSoftError as e:
            raise SaveEditorError(str(e)) from e


