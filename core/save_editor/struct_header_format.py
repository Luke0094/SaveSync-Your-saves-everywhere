from .base import SaveEditorError, SaveField, _Format


class StructHeaderFormat(_Format):
    """A save whose format is a known, documented, FIXED binary header —
    see core/engines/struct_header for the shared reading/writing
    machinery and core/engines/known_headers for the layout list every
    instance of this reader is tried against. Generic across every game
    whose header is documented there; nothing in THIS class is specific to
    any one of them — see that module for what is.

    ``name``/``engine`` start as this generic description and are replaced,
    per instance, by whichever layout actually matched, the moment load()
    succeeds — save_editor.make_doc reads them off the instance for
    exactly this reason.
    """
    name = "Known save header"
    engine = "Known save header"

    def __init__(self):
        self._save = None
        self.source_path = None

    def load(self, data: bytes) -> None:
        from core.engines.known_headers import KNOWN_LAYOUTS
        from core.engines.struct_header import (StructHeaderError,
                                                 StructHeaderSave,
                                                 find_layout)
        ext = self.source_path.suffix.lower() if self.source_path else ""
        layout = find_layout(data, ext, KNOWN_LAYOUTS)
        if layout is None:
            raise SaveEditorError(
                "this file's header does not match any known layout")
        save = StructHeaderSave(layout)
        try:
            save.load(data)
        except StructHeaderError as e:
            raise SaveEditorError(str(e)) from e
        self._save = save
        self.name = layout.name
        self.engine = layout.engine

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField((name,), label, "int", value)
                for name, label, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.struct_header import StructHeaderError
        try:
            self._save.set_value(path[0], value)
        except StructHeaderError as e:
            raise SaveEditorError(str(e)) from e
