from .base import SaveEditorError, SaveField, _Format, _leading_group, _unique

class QspFormat(_Format):
    """QSP (Quest Soft Player) saves. See core/qsp: this is the one format
    where the round trip proves nothing, so the reader instead has to account
    for every line in the file."""
    name = "QSP"
    engine = "QSP (Quest Soft Player)"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.qsp import loads, QspError
        try:
            self._save = loads(data)
        except QspError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, _leading_group(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


