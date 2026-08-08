"""SQLite save databases — Room / Compose Desktop Java titles, and similar.

The page layout of a SQLite file changes when a value is written, so callers
usually check value equality rather than raw bytes. Schema / Room bookkeeping
tables are skipped; only typed cells (integers, reals, text) in the game's
own tables are offered.
"""
import os
import sqlite3
import tempfile
from pathlib import Path


class SqliteError(ValueError):
    pass


_SKIP_TABLES = {
    "room_master_table", "room_table_modification_log",
    "sqlite_sequence", "android_metadata",
}


def _typed(sql_type: str, raw):
    if isinstance(raw, bool) or sql_type in ("BOOLEAN",):
        return "bool", bool(raw)
    if isinstance(raw, int) and "REAL" not in sql_type \
            and "FLOAT" not in sql_type and "DOUBLE" not in sql_type:
        return "int", int(raw)
    if isinstance(raw, (int, float)) and (
            "REAL" in sql_type or "FLOAT" in sql_type
            or "DOUBLE" in sql_type or isinstance(raw, float)):
        return "float", float(raw)
    if isinstance(raw, int):
        return "int", int(raw)
    if isinstance(raw, float):
        return "float", float(raw)
    return "str", str(raw)


class SqliteDb:
    """One SQLite file opened for editing."""

    def __init__(self):
        self._tmp = ""
        self._con = None
        self._entries: list = []   # (table, pk_cols, pk_vals, col, kind, value)

    def load(self, data: bytes) -> None:
        if not data.startswith(b"SQLite format 3\x00"):
            raise SqliteError("not a SQLite database")
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        Path(self._tmp).write_bytes(data)
        self._con = sqlite3.connect(self._tmp)
        self._con.row_factory = sqlite3.Row
        self._entries = []
        cur = self._con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in _SKIP_TABLES:
                continue
            cols = list(cur.execute(f'PRAGMA table_info("{table}")'))
            if not cols:
                continue
            # col: (cid, name, type, notnull, dflt, pk)
            pk_cols = [c[1] for c in sorted(cols, key=lambda c: c[5] or 0)
                       if c[5]]
            if not pk_cols:
                # Fall back to rowid for tables without an explicit PK.
                pk_cols = ["rowid"]
            col_meta = {c[1]: (c[2] or "").upper() for c in cols}
            editable = [c[1] for c in cols
                        if c[1] not in pk_cols
                        and "BLOB" not in col_meta.get(c[1], "")]
            if not editable:
                continue
            select_cols = ", ".join(
                f'"{c}"' for c in (pk_cols + editable
                                   if pk_cols != ["rowid"]
                                   else ["rowid"] + editable))
            try:
                rows = cur.execute(
                    f'SELECT {select_cols} FROM "{table}"').fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                if pk_cols == ["rowid"]:
                    pk_vals = (row[0],)
                    data_offset = 1
                else:
                    pk_vals = tuple(row[i] for i in range(len(pk_cols)))
                    data_offset = len(pk_cols)
                for i, col in enumerate(editable):
                    raw = row[data_offset + i]
                    if raw is None:
                        continue
                    kind, value = _typed(col_meta.get(col, ""), raw)
                    self._entries.append(
                        (table, tuple(pk_cols), pk_vals, col, kind, value))
        if not self._entries:
            raise SqliteError("no editable values in this SQLite database")

    def dump(self) -> bytes:
        assert self._con is not None
        self._con.commit()
        # Ensure WAL pages land in the main file before we read it back.
        try:
            self._con.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.Error:
            pass
        self._con.close()
        data = Path(self._tmp).read_bytes()
        self._con = sqlite3.connect(self._tmp)
        self._con.row_factory = sqlite3.Row
        return data

    def values(self) -> list:
        """(index, label, kind, value, table) for every editable cell."""
        out = []
        for i, (table, _pk_cols, pk_vals, col, kind, value) in enumerate(
                self._entries):
            pk_part = ",".join(str(v) for v in pk_vals)
            label = f"{table}.{pk_part}.{col}"
            out.append((i, label, kind, value, table))
        return out

    def set_value(self, index: int, value) -> None:
        assert self._con is not None
        table, pk_cols, pk_vals, col, kind, _old = self._entries[index]
        if kind == "bool":
            store = 1 if value else 0
        elif kind == "int":
            store = int(value)
        elif kind == "float":
            store = float(value)
        else:
            store = str(value)
        where = " AND ".join(f'"{c}"=?' for c in pk_cols)
        self._con.execute(
            f'UPDATE "{table}" SET "{col}"=? WHERE {where}',
            (store, *pk_vals))
        self._entries[index] = (table, pk_cols, pk_vals, col, kind,
                                bool(store) if kind == "bool" else store)

    def close(self) -> None:
        try:
            if self._con is not None:
                self._con.close()
        except Exception:
            pass
        self._con = None
        if self._tmp:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
            self._tmp = ""

    def __del__(self):
        self.close()


def loads(data: bytes) -> SqliteDb:
    db = SqliteDb()
    db.load(data)
    return db
