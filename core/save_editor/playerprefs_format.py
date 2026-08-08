"""Unity PlayerPrefs — thin adapter over ``core.engines.playerprefs``."""
from .base import SaveEditorError, SaveField, _Format


class PlayerPrefsFormat(_Format):
    """Unity PlayerPrefs registry export. See ``core.engines.playerprefs``."""
    name = "Unity PlayerPrefs"
    engine = "Unity"
    verify_exact = False

    def __init__(self):
        self._doc = None

    def load(self, data: bytes) -> None:
        from core.engines.playerprefs import PlayerPrefsError, loads
        try:
            self._doc = loads(data)
        except PlayerPrefsError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._doc.dump()

    def fields(self) -> list:
        return [SaveField((i,), label, kind, value, group)
                for i, label, kind, value, group in self._doc.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.playerprefs import PlayerPrefsError
        try:
            self._doc.set_value(path[0], value)
        except PlayerPrefsError as e:
            raise SaveEditorError(str(e)) from e
