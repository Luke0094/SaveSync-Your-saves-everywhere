"""Plain XML — thin adapter over ``core.engines.xml_save``."""
from .base import SaveEditorError, SaveField, _Format


class XmlFormat(_Format):
    """Plain XML saves. See ``core.engines.xml_save``."""
    name = "XML"
    engine = "XML"
    verify_exact = False

    def __init__(self):
        self._doc = None

    def load(self, data: bytes) -> None:
        from core.engines.xml_save import XmlSaveError, loads
        try:
            self._doc = loads(data)
        except XmlSaveError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._doc.dump()

    def fields(self) -> list:
        return [SaveField((i,), label, kind, value, group)
                for i, label, kind, value, group in self._doc.values()]

    def set_field(self, path: tuple, value) -> None:
        self._doc.set_value(path[0], value)
