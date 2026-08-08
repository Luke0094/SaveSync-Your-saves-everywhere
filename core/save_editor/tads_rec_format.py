"""TADS ``system.rec`` — thin adapter over ``core.engines.tads``."""
from .base import SaveEditorError, SaveField, _Format


class TadsRecFormat(_Format):
    """TAD-kit ``system.rec`` slots. See ``core.engines.tads``."""
    name = "TADS record"
    engine = "TADS"
    verify_exact = True

    def __init__(self):
        self._rec = None

    def load(self, data: bytes) -> None:
        from core.engines.tads import TadsError, loads
        try:
            self._rec = loads(data)
        except TadsError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._rec.dump()

    def fields(self) -> list:
        return [SaveField((i,), label, kind, value)
                for i, label, kind, value in self._rec.values()]

    def set_field(self, path: tuple, value) -> None:
        self._rec.set_value(path[0], value)
