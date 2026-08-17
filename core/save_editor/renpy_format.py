"""Ren'Py ``.save`` — thin adapter over ``core.engines.renpy``."""
from .base import SaveEditorError, SaveField, _Format, _unique


class RenpyFormat(_Format):
    """Ren'Py ``.save`` — a zip around a pickle. See ``core.engines.renpy``
    for why the pickle is read opcode by opcode instead of being unpickled,
    and why an edited save has to be re-signed."""

    @staticmethod
    def _group_of(label: str) -> str:
        """The game's own variable a value belongs to.

        Everything in a Ren'Py save hangs off the store, so grouping by the
        first step would put all of it in one pile. The step after it is the
        variable the game itself declared, which is the real division.
        """
        parts = label.split(".")
        if len(parts) > 2 and parts[0] == "store":
            return parts[1]
        return ""

    name = "Ren'Py"
    engine = "Ren'Py"
    # A zip cannot be rebuilt byte-for-byte (compression is not
    # deterministic across writers), so equality is checked on the values.
    verify_exact = False

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.renpy import RenpyError, loads
        try:
            self._save = loads(data)
        except RenpyError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        from core.engines.renpy import RenpyError
        try:
            return self._save.dump()
        except RenpyError as e:
            raise SaveEditorError(str(e)) from e

    def verify_value_round_trip(self) -> bool:
        """Prove a rebuild without a second full pickle walk on open.

        Values live only in the zip ``log`` entry. ``dump`` must write
        exactly ``_rebuilt_log()`` — byte to byte, nothing lost or altered
        on the way into the zip. That single check is the whole cost at
        open time (no edits yet, so the rebuilt log IS the original log).

        When the user has edited values, the splice is re-scanned with
        pickletools (read-only, nothing unpickled): the re-scan must return
        exactly the same sequence of names and values, position by position.
        This only runs on save, never on open.
        """
        import io
        import zipfile
        rebuilt = self.dump()
        with zipfile.ZipFile(io.BytesIO(rebuilt)) as zf:
            log = zf.read("log")
        sv = self._save
        if log != sv._rebuilt_log():
            return False
        if any("new" in v for v in sv._values):
            from core.engines.renpy import RenpyError
            try:
                rows = sv._scan(log)
            except RenpyError:
                return False
            # Positional, not by name: the splice must leave every value at
            # the same spot, in the same order, with the same name and value.
            # An edited value that vanishes from the re-scan (a wrong splice
            # offset, or a value the game never re-writes) MUST fail — the old
            # per-name check only flagged values that happened to reappear.
            # Positional comparison also keeps same-named values (nine quest
            # rewards all called "money") distinguishable.
            if len(rows) != len(sv._values):
                return False
            for v, row in zip(sv._values, rows):
                if row["name"] != v["name"] or row["value"] != v["value"]:
                    return False
        return True

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, self._group_of(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)
