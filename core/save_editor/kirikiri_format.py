from .base import SaveEditorError, SaveField, _Format

class KirikiriFormat(_Format):
    """KiriKiri / KAG saves (``.ksd``).

    The save is the game's state written as a TJS dictionary, in UTF-16 —
    readable text, not a binary structure. See core/kirikiri for the three
    wrappers it arrives in and for why the real proof is that the walk
    accounts for every character rather than that the file round-trips.
    """
    name = "KiriKiri"
    engine = "KiriKiri / KAG"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.kirikiri import KirikiriError, loads
        try:
            self._save = loads(data)
        except KirikiriError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


