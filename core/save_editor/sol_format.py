from .base import SaveEditorError, SaveField, _Format, _leading_group, _unique

class SolFormat(_Format):
    """Adobe Flash shared objects (``.sol``), AMF0 and AMF3.

    Edited by splicing, like Ren'Py's pickle — see core/sol for why that
    matters more here than anywhere else.
    """
    name = "Adobe Flash"
    engine = "Flash (shared object)"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.sol import loads, SolError
        try:
            self._save = loads(data)
        except SolError as e:
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


