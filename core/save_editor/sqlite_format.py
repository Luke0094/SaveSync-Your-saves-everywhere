"""SQLite — thin adapter over ``core.engines.sqlite_db``."""
from .base import SaveEditorError, SaveField, _Format


class SqliteFormat(_Format):
    """SQLite databases — Room / Compose Desktop Java titles, and similar.

    See ``core.engines.sqlite_db``. The open gate checks value equality
    rather than raw bytes (``verify_exact`` stays False).
    """
    name = "SQLite"
    engine = "SQLite"
    verify_exact = False

    def __init__(self):
        self._db = None

    def load(self, data: bytes) -> None:
        from core.engines.sqlite_db import SqliteError, loads
        try:
            self._db = loads(data)
        except SqliteError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._db.dump()

    def fields(self) -> list:
        return [SaveField((i,), label, kind, value, group=table)
                for i, label, kind, value, table in self._db.values()]

    def set_field(self, path: tuple, value) -> None:
        self._db.set_value(path[0], value)
