from .base import SaveEditorError, _leading_group, _unique, _walk
from .json_format import JsonFormat

class TyranoFormat(JsonFormat):
    """TyranoScript / TyranoBuilder saves (``.sav``).

    JSON behind JavaScript's ``escape()`` — see core/tyrano, which carries the
    one part that is not obvious (``escape()`` counts in UTF-16 code units, so
    an emoji is a surrogate PAIR and not one five-digit sequence).

    What is OFFERED is a small part of what is read. A TyranoScript save holds
    the label map, the macro map, the script buffer and the line currently on
    screen as well as the game's own values, and in a long game that is most
    of the file — one of these samples is 11 MB and holds 774 values worth
    editing. So the same rule as RAGS applies: read the whole file, offer the
    part the game itself set.
    """
    name = "TyranoScript"
    engine = "TyranoScript / TyranoBuilder"
    # The wrapper rebuilds exactly and every sample proves it, but the JSON
    # inside is re-serialised, and JavaScript and Python do not have to spell
    # every float the same way. Checking the VALUES is the claim that actually
    # holds for a text format; see open_save().
    verify_exact = False

    def load(self, data: bytes) -> None:
        from core.engines.tyrano import TyranoError, loads
        try:
            self.data = loads(data)
        except TyranoError as e:
            raise SaveEditorError(str(e)) from e
        if not self._roots():
            raise SaveEditorError("no game values in this TyranoScript save")

    def dump(self) -> bytes:
        from core.engines.tyrano import dumps
        return dumps(self.data)

    def _roots(self) -> list:
        from core.engines.tyrano import state_roots
        return state_roots(self.data)

    def fields(self) -> list:
        from core.engines.tyrano import at
        roots = self._roots()
        out = []
        for path, group in roots:
            node = at(self.data, path)
            if node is None:
                continue
            cut = len(path)
            for f in _walk(node, path, group):
                f.label = ".".join(str(p) for p in f.path[cut:])
                # Slots hold the same names as each other, and a value is held
                # by its name — so with more than one slot the slot has to be
                # part of the name, or none of them could be held at all.
                if len(roots) > 1:
                    f.label = f"{group}.{f.label}"
                    f.group = group
                else:
                    f.group = _leading_group(f.label)
                out.append(f)
        labels = _unique([f.label for f in out])
        for f, label in zip(out, labels):
            f.label = label
        return out


