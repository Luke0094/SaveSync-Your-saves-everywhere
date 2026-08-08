"""Unity PlayerPrefs — registry exports as JSON.

On Windows, Unity's PlayerPrefs live under
``HKCU\\Software\\<company>\\<product>``. SaveSync backs those up by exporting
the key as JSON (see ``core.registry_saves``); this module reads that same
export so backup and editor agree about what a key contains.

Unity mangles the names: a preference called "gold" is stored as
``gold_h3096647``, the number being a checksum of the name. The checksum
cannot be turned back into anything, but it does not need to be — the name
is in front of it, and only the suffix is hidden for display. It is kept,
because writing the value back under a different name would leave the game
unable to find it.
"""
import base64
import json
import re
import struct


class PlayerPrefsError(ValueError):
    pass


# Unity hides a preference's name behind a checksum of it: "gold" is stored
# as "gold_h3096647". Only the tail is dropped, and only for display.
_PREFS_SUFFIX = re.compile(r"_h\d+$")
# Registry value types, from winreg. Named here so this module reads without
# importing winreg, which does not exist off Windows.
_REG_SZ, _REG_BINARY, _REG_DWORD, _REG_QWORD = 1, 3, 4, 11


def _prefs_label(regname: str) -> str:
    return _PREFS_SUFFIX.sub("", regname) or regname


def _prefs_read(spec: dict) -> tuple:
    """(kind, value) for one exported registry value, or ("", None)."""
    vtype = int(spec.get("t", _REG_BINARY))
    if "i" in spec and vtype in (_REG_DWORD, _REG_QWORD):
        return "int", int(spec["i"])
    if "s" in spec and vtype == _REG_SZ:
        return "str", str(spec["s"])
    if "b" not in spec or vtype != _REG_BINARY:
        return "", None
    try:
        raw = base64.b64decode(spec["b"])
    except Exception:
        return "", None
    # Unity writes a string as its UTF-8 bytes with a terminator, and a
    # float as its four bytes. A terminator is what tells them apart; four
    # bytes that do not end in one are the only other thing it writes.
    if raw.endswith(b"\0"):
        try:
            return "str", raw[:-1].decode("utf-8")
        except UnicodeDecodeError:
            return "", None
    if len(raw) == 4:
        return "float", struct.unpack("<f", raw)[0]
    return "", None


def _prefs_write(spec: dict, value) -> dict:
    """The same value re-encoded, keeping the type the registry had."""
    kind, _old = _prefs_read(spec)
    vtype = int(spec.get("t", _REG_BINARY))
    if kind == "int":
        return {"t": vtype, "i": int(value)}
    if kind == "str" and vtype == _REG_SZ:
        return {"t": vtype, "s": str(value)}
    if kind == "str":
        raw = str(value).encode("utf-8") + b"\0"
    elif kind == "float":
        raw = struct.pack("<f", float(value))
    else:
        raise PlayerPrefsError(
            "this preference is of a kind SaveSync leaves alone")
    return {"t": vtype, "b": base64.b64encode(raw).decode("ascii")}


class PlayerPrefsDoc:
    """One registry-export document opened for editing."""

    def __init__(self):
        self._doc = None
        self._spots = []          # (values dict, registry name, where)

    def load(self, data: bytes) -> None:
        try:
            self._doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise PlayerPrefsError(
                f"this registry export will not read: {e}") from e
        self._spots = []
        self._gather(self._doc.get("tree") or {}, "")
        if not self._spots:
            raise PlayerPrefsError("this registry key holds no values to edit")

    def _gather(self, node: dict, where: str) -> None:
        values = node.get("values") or {}
        for regname in sorted(values):
            kind, _ = _prefs_read(values[regname])
            if kind:
                self._spots.append((values, regname, where))
        for child in sorted(node.get("subkeys") or {}):
            self._gather(node["subkeys"][child],
                         f"{where}\\{child}" if where else child)

    def dump(self) -> bytes:
        return json.dumps(self._doc, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def values(self) -> list:
        """(index, label, kind, value, group) for every editable preference."""
        out = []
        for i, (values, regname, where) in enumerate(self._spots):
            kind, value = _prefs_read(values[regname])
            out.append((i, _prefs_label(regname), kind, value,
                        where or "(this game)"))
        return out

    def set_value(self, index: int, value) -> None:
        values, regname, _where = self._spots[index]
        values[regname] = _prefs_write(values[regname], value)


def loads(data: bytes) -> PlayerPrefsDoc:
    doc = PlayerPrefsDoc()
    doc.load(data)
    return doc
