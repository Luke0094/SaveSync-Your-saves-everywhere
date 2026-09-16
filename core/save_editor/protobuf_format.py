from .base import SaveEditorError, SaveField, _Format

# A save this generic reader recovers nothing useful from is not worth
# offering — see ProtobufFormat.parse_is_plausible.
_MIN_LEAVES = 1


def _label(path: tuple) -> str:
    """A path of (field number, occurrence) pairs, read the way protoc's
    own --decode_raw would print it: dotted field numbers, with the
    occurrence only shown past the first — there is no name to show
    instead, see core.engines.protobuf_raw's module docstring."""
    parts = []
    for number, occurrence in path:
        parts.append(str(number) if occurrence == 0
                     else f"{number}#{occurrence + 1}")
    return ".".join(parts)


class ProtobufFormat(_Format):
    """A save serialised as raw Google Protocol Buffers, with no schema to
    read it by — see core/engines/protobuf_raw for the wire format itself
    and the honest limits of reading it without one. Generic across any
    game that writes a save this way; nothing here is specific to one.

    Registered ``expensive`` (see registry.py): protobuf carries no magic
    bytes at all, so telling it apart from unrelated bytes costs a real
    structural parse of the whole file, not a cheap header check — worth
    paying when someone asks to check every format, never on the
    off-chance.
    """
    name = "Protocol Buffers (raw)"
    engine = "Protocol Buffers"

    def __init__(self):
        self._msg = None

    def load(self, data: bytes) -> None:
        from core.engines.protobuf_raw import ProtoError, loads
        try:
            self._msg = loads(data)
        except ProtoError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._msg.dump()

    def parse_is_plausible(self) -> bool:
        return self._msg.field_count() >= _MIN_LEAVES

    def fields(self) -> list:
        return [SaveField(path, _label(path), kind, value)
                for path, kind, value in self._msg.leaves()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.protobuf_raw import ProtoError
        try:
            self._msg.set_value(path, value)
        except ProtoError as e:
            raise SaveEditorError(str(e)) from e
