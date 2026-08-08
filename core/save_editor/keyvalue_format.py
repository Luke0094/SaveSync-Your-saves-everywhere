"""Key/value text — thin adapter over ``core.engines.keyvalue``."""
from .base import SaveEditorError, SaveField, _Format


class KeyValueFormat(_Format):
    """``key = value`` text. See ``core.engines.keyvalue``."""
    name = "Key/value text"
    engine = "Text (key = value)"

    def __init__(self):
        self._doc = None

    def load(self, data: bytes) -> None:
        from core.engines.keyvalue import KeyValueError, loads
        try:
            self._doc = loads(data)
        except KeyValueError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._doc.dump()

    def fields(self) -> list:
        return [SaveField((n,), key, kind, value, section)
                for n, key, kind, value, section in self._doc.values()]

    def set_field(self, path: tuple, value) -> None:
        self._doc.set_value(path[0], value)
