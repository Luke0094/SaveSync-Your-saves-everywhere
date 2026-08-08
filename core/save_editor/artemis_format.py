from .base import SaveEditorError, SaveField, _Format

class ArtemisFormat(_Format):
    """Artemis Engine settings (``system.dat``).

    See core/artemis, including why the numbered slots beside this file are
    named rather than opened.
    """
    name = "Artemis"
    engine = "Artemis"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.artemis import ArtemisError, loads
        try:
            self._save = loads(data)
        except ArtemisError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.artemis import ArtemisError
        try:
            self._save.set_value(path[0], value)
        except ArtemisError as e:
            raise SaveEditorError(str(e)) from e


