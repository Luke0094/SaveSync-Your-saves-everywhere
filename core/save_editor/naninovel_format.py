"""Naninovel (``.nson``) — editor adapter over ``core.engines.naninovel``."""
import json

from .base import SaveEditorError, SaveField, _walk
from .json_format import JsonFormat


class NaninovelFormat(JsonFormat):
    """Naninovel binary saves: raw-deflate JSON (see ``engines.naninovel``).

    The plain-text form is left to the JSON reader. Inner manager JSON is
    unwrapped here so the dialog shows game variables, not plumbing.
    """
    name = "Naninovel"
    engine = "Naninovel"
    verify_exact = True

    def __init__(self):
        super().__init__()
        self._inner = []

    def load(self, data: bytes) -> None:
        from core.engines.naninovel import NaninovelError, compress, decompress
        try:
            plain = decompress(data)
        except NaninovelError as e:
            raise SaveEditorError(str(e)) from e
        super().load(plain)
        self._unwrap()
        # Whether THIS file can be put back exactly as it came. Most can:
        # the engine settings reproduce them to the byte. A save written by a
        # build that packed it differently cannot be, and says so here
        # rather than being refused — it still opens, and is still checked,
        # by reading back what was written and comparing the values.
        self.verify_exact = compress(plain) == data

    def _unwrap(self) -> None:
        """Open the JSON that Naninovel keeps inside its own JSON.

        The outer file is a map from a .NET type name to that manager's
        state, and each state is not an object but a STRING with the object
        written inside it. The game's own variables — what anyone opening a
        save is looking for — live one level down there, so a reader that
        stops at the outer layer offers the file's plumbing and none of its
        contents.

        Each inner text is kept exactly as it arrived and only re-written if
        something in it was changed. Re-encoding one that nobody touched
        would risk spelling it differently from the way the game did, and
        that is the difference between a save rebuilt byte for byte and one
        merely rebuilt.
        """
        self._inner = []
        node = self.data if isinstance(self.data, dict) else {}
        chunk = node.get("objectJsonMap")
        if not isinstance(chunk, dict):
            return
        values = chunk.get("values")
        keys = chunk.get("keys")
        if not isinstance(values, list):
            return
        for i, text in enumerate(values):
            if not isinstance(text, str) or not text.startswith("{"):
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            name = keys[i] if isinstance(keys, list) and i < len(keys) else ""
            self._inner.append({"at": i, "data": parsed, "dirty": False,
                                "name": _naninovel_manager(str(name))})

    def dump(self) -> bytes:
        from core.engines.naninovel import compress
        for slot in getattr(self, "_inner", []):
            if slot["dirty"]:
                self.data["objectJsonMap"]["values"][slot["at"]] = json.dumps(
                    slot["data"], ensure_ascii=False, separators=(",", ":"))
        return compress(super().dump())

    def fields(self) -> list:
        # A state that was opened is offered by its contents, not as the
        # thousands of characters of JSON it is written as. Offering both
        # would show the same value twice, and let one be edited through a
        # view that the other would then overwrite.
        opened = {slot["at"] for slot in self._inner}
        out = []
        for f in super().fields():
            if (len(f.path) >= 3 and f.path[0] == "objectJsonMap"
                    and f.path[1] == "values" and f.path[2] in opened):
                continue
            out.append(SaveField(("outer",) + f.path, f.label, f.kind,
                                 f.value, f.group))
        for n, slot in enumerate(self._inner):
            for f in _walk(slot["data"]):
                out.append(SaveField(("inner", n) + f.path, f.label, f.kind,
                                     f.value, slot["name"] or f.group))
        return out

    def set_field(self, path: tuple, value) -> None:
        if path and path[0] == "outer":
            super().set_field(path[1:], value)
            return
        slot = self._inner[path[1]]
        node = slot["data"]
        for key in path[2:-1]:
            node = node[key]
        node[path[-1]] = value
        slot["dirty"] = True


def _naninovel_manager(type_name: str) -> str:
    """The readable half of a .NET type name, for use as a heading.

    Naninovel writes the whole assembly-qualified name — the type, the
    assembly, its version, culture and public key. Only the first is worth
    showing, and only its last part.
    """
    head = type_name.split(",", 1)[0]
    head = head.split("+", 1)[0]
    head = head.rsplit(".", 1)[-1]
    return head.split("`", 1)[0]
