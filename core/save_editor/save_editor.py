"""SaveSync — save-file editor.

Opens a save file, exposes the values inside it as an editable list, and
writes it back — with the original kept aside first, so any edit can be
undone.

This deliberately does NOT touch running processes: nothing is injected into
game memory, nothing is attached to, nothing is patched at runtime. It reads
and writes files at rest, the same thing a text editor does, which is both
the honest way to do it and the reason SaveSync cannot be mistaken for
something that tampers with programs.

Two rules the whole module is built around:

- **Never guess.** A format is either understood well enough to rebuild it
  byte-for-byte, or it is reported as unsupported. A half-understood binary
  format that is written back anyway produces a save the game refuses to
  load — and the player finds out hours later.
- **Prove the round trip.** Before any file is offered for editing, it is
  decoded and re-encoded, and the result must match the original exactly.
  If it does not, the file is read-only. This catches every parsing gap
  without having to enumerate them.
"""
import json
import logging
import re
import shutil
from dataclasses import dataclass, field as _field
from datetime import datetime
from pathlib import Path

from core.constants import USER_DATA_DIR

logger = logging.getLogger(__name__)

# Engines we can read AND rebuild exactly.
# Anything else is named for the player rather than silently mangled.
# Named for the player, and with what is actually missing for each — saying
# which is the difference between a limitation and a shrug.
_RECOGNISED_ONLY = {
    ".es3": ("Unity (Easy Save 3)",
             "it is encrypted and its password is not in the game's files "
             "where Easy Save usually leaves it — put the key in an "
             "es3.key file beside the save and it will open"),
    # AliceSoft .asd carries several things in the same container, and all
    # the ones with values in them open (see AliceSoftFormat). What reaches
    # here is one that has none to offer, or one that is encrypted.
    ".asd": ("AliceSoft System 4",
             "it carries no values that can be named — the gallery lists are "
             "a run of numbers with nothing saying what they unlock, and the "
             "engine scrambles some of the rest"),
    ".vsf": ("AliceSoft System 4",
             "it is the flag file beside the save, and it carries no names to "
             "show a value under"),
    # RPG Developer Bakin — "YUKRDATA", then its Yukar runtime's own object
    # stream. Same reason: read, not described.
    ".sgs": ("RPG Developer Bakin",
             "its values are written as a plain object stream with nothing "
             "naming or typing them"),
}


def _is_alicesoft(data: bytes) -> bool:
    """An AliceSoft container this reader could not open.

    Reached only once AliceSoftFormat has already declined the file, so what
    is left is a numbered save slot, an encrypted one, or the smaller "CSD"
    container the engine keeps its common settings in. All of them are worth
    naming rather than calling a mystery. The CSD check carries the deflate
    marker that has to follow it, because three bytes alone would claim files
    that are nothing of the sort.
    """
    if data[:4] == b"GD\x01\x01":
        return True
    return data[:4] == b"CSD\x00" and data[16:17] == b"\x78"

# How deep to walk a Ruby object graph looking for values. RPG Maker nests a
# few levels; past this it is the engine's own bookkeeping.
_MARSHAL_DEPTH = 12

_BACKUP_DIR = USER_DATA_DIR / "save_edits"
# How many copies of one save to keep before the oldest is dropped, and how
# long to keep them at all. Both are in Settings; these are what they start
# at, and what is used when there is no configuration to ask (the editor runs
# headless in tests, where reading the config would need a running app).
_DEFAULT_COPIES = 3
_DEFAULT_COPY_DAYS = 7


class SaveEditorError(Exception):
    """Raised for a file we will not write back.

    The message is written in English because it is also what goes into the
    log. The few failures a player actually sees carry, as well, the name of
    the phrase to say it with and the values to fill in — so the window can
    put it in their own language while the log stays one language throughout.
    """

    def __init__(self, message: str, key: str = "", **params):
        super().__init__(message)
        self.key = key
        self.params = params


def explain(error) -> str:
    """What to show a person about *error*, in the language they chose."""
    from i18n import t
    key = getattr(error, "key", "")
    if not key:
        return str(error)
    said = t(key, **getattr(error, "params", {}))
    # t() hands the key back when there is nothing to say it with; the
    # English message is a far better answer than a dotted key.
    return str(error) if said == key else said


@dataclass
class SaveField:
    """One editable value, addressed by its path inside the document."""
    path: tuple
    label: str
    kind: str            # "int" | "float" | "bool" | "str"
    value: object
    group: str = ""      # the container it sits in, for display


# ── Formats ──────────────────────────────────────────────────────────────────

class _Format:
    """Base: decode bytes, expose fields, encode back."""
    name = ""
    engine = ""
    # True when re-encoding must reproduce the original bytes exactly. Binary
    # formats must; text and container formats are re-serialised (whitespace,
    # compression) and prove themselves by decoding to the same VALUES.
    verify_exact = True

    def load(self, data: bytes) -> None:
        raise NotImplementedError

    def dump(self) -> bytes:
        raise NotImplementedError

    def fields(self) -> list:
        raise NotImplementedError

    def set_field(self, path: tuple, value) -> None:
        raise NotImplementedError


def _leading_group(label: str) -> str:
    """The container a value sits in, when its own name says so.

    Some formats keep their values flat and have nothing to group by. Naming
    the group after the engine, as this used to, produced one category holding
    every value — which is not a category, just a second word for "all".
    Saying there is no grouping is the truthful answer, and the selector then
    keeps out of the way.
    """
    head, sep, _rest = label.partition(".")
    return head if sep else ""


def _unique(names: list) -> list:
    """The same names, with the repeats numbered so each one is its own.

    A held value is found again by its label, and one that turns up twice is
    skipped rather than guessed at — so a format that repeats a name would
    quietly refuse to hold any of them. Only the repeats are numbered; names
    that were already unique are left as they are.
    """
    seen = {}
    for name in names:
        seen[name] = seen.get(name, 0) + 1
    out, count = [], {}
    for name in names:
        if seen[name] == 1:
            out.append(name)
            continue
        count[name] = count.get(name, 0) + 1
        out.append(f"{name} #{count[name]}")
    return out


def _paired_dict(node) -> list:
    """A dictionary written as two parallel lists, or [].

    Unity's own JSON writer cannot express a dictionary, so everything that
    uses one — and Naninovel's variables are the case in point — comes out
    as ``{"keys": [...], "values": [...]}``. Read literally that gives rows
    called "values.0" and "values.1"; read as the pairing it is, it gives
    rows called by the names the game uses.
    """
    if not isinstance(node, dict) or set(node) != {"keys", "values"}:
        return []
    keys, values = node["keys"], node["values"]
    if not isinstance(keys, list) or not isinstance(values, list):
        return []
    if len(keys) != len(values) or not all(isinstance(k, str) for k in keys):
        return []
    return list(zip(keys, values))


def _walk(node, prefix=(), group="") -> list:
    """Every scalar leaf of a JSON-shaped structure, with its path."""
    out = []
    pairs = _paired_dict(node)
    if pairs:
        depth = len(prefix) + 2          # the path of the value itself
        for i, (name, val) in enumerate(pairs):
            found = _walk(val, prefix + ("values", i), group)
            for f in found:
                # The value itself takes the name it is filed under. Anything
                # nested INSIDE it keeps its own trail after that name, so a
                # structure under one key stays distinguishable.
                tail = [str(p) for p in f.path[depth:]]
                f.label = ".".join([name] + tail)
            out.extend(found)
        return out
    if isinstance(node, dict):
        for key, val in node.items():
            out.extend(_walk(val, prefix + (key,), group or str(key)))
    elif isinstance(node, list):
        for i, val in enumerate(node):
            out.extend(_walk(val, prefix + (i,), group))
    else:
        if isinstance(node, bool):
            kind = "bool"
        elif isinstance(node, int):
            kind = "int"
        elif isinstance(node, float):
            kind = "float"
        elif isinstance(node, str):
            kind = "str"
        else:
            return out          # null and anything exotic: shown by nobody
        label = ".".join(str(p) for p in prefix)
        out.append(SaveField(prefix, label, kind, node, group))
    return out


class JsonFormat(_Format):
    """Plain JSON — Unity, many indie engines, and anything sane."""
    name = "JSON"
    engine = "JSON"
    verify_exact = False

    def __init__(self):
        self.data = None
        self._encoding = "utf-8"
        self._bom = False

    def load(self, data: bytes) -> None:
        self._bom = data.startswith(b"\xef\xbb\xbf")
        text = data.decode("utf-8-sig" if self._bom else "utf-8")
        self.data = json.loads(text)

    def dump(self) -> bytes:
        text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        raw = text.encode("utf-8")
        return (b"\xef\xbb\xbf" + raw) if self._bom else raw

    def fields(self) -> list:
        return _walk(self.data)

    def set_field(self, path: tuple, value) -> None:
        node = self.data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value


class XmlFormat(_Format):
    """Plain XML — what .NET's own serializer writes, so Unity and Godot
    games that save through it, and anything else that picked XML.

    Read with the standard library's parser, which does not resolve external
    entities, so a save cannot talk this into fetching anything.

    Values live in two places in XML and both are offered: the text inside an
    element, and the attributes on it. Everything else about the document —
    its elements, their order, their nesting — is carried through untouched,
    the same bargain the JSON reader makes: the file is not reproduced byte
    for byte, it is reproduced value for value, and that is checked on the
    way out.
    """
    name = "XML"
    engine = "XML"
    verify_exact = False

    def __init__(self):
        self._tree = None
        self._spots = []          # (element, attribute name or None)
        self._declaration = b""
        self._encoding = "utf-8"

    def load(self, data: bytes) -> None:
        import xml.etree.ElementTree as ET
        head = data[:200].lstrip()
        if head.startswith(b"<?xml"):
            end = data.find(b"?>")
            if end > 0:
                self._declaration = data[:end + 2]
                match = re.search(rb'encoding=["\']([\w-]+)["\']',
                                  self._declaration)
                if match:
                    self._encoding = match.group(1).decode("ascii", "ignore")
        try:
            self._tree = ET.fromstring(data.decode(self._encoding, "replace"))
        except ET.ParseError as e:
            raise SaveEditorError(f"this XML will not parse: {e}") from e
        self._spots = []
        self._gather(self._tree, "")
        if not self._spots:
            raise SaveEditorError("this XML holds no values to edit")

    def _gather(self, node, prefix: str) -> None:
        where = f"{prefix}/{node.tag}" if prefix else str(node.tag)
        for name in node.attrib:
            self._spots.append((node, name, where))
        children = list(node)
        text = (node.text or "").strip()
        # An element with children has no value of its own: whatever sits
        # between its tags is the layout of the file, not a value anybody set.
        if text and not children:
            self._spots.append((node, None, where))
        for child in children:
            self._gather(child, where)

    def dump(self) -> bytes:
        import xml.etree.ElementTree as ET
        body = ET.tostring(self._tree, encoding="unicode")
        raw = body.encode(self._encoding, "xmlcharrefreplace")
        if self._declaration:
            return self._declaration + b"\n" + raw
        return raw

    def fields(self) -> list:
        out = []
        for i, (node, attr, where) in enumerate(self._spots):
            text = node.attrib[attr] if attr else (node.text or "").strip()
            label = f"{node.tag}@{attr}" if attr else str(node.tag)
            group = where.rsplit("/", 1)[0] if "/" in where else "(root)"
            out.append(SaveField((i,), label, _kind_of_text(text),
                                 _value_of_text(text), group))
        return out

    def set_field(self, path: tuple, value) -> None:
        node, attr, _where = self._spots[path[0]]
        text = "true" if value is True else "false" if value is False \
            else str(value)
        if attr:
            node.attrib[attr] = text
        else:
            node.text = text


def _kind_of_text(text: str) -> str:
    """What a piece of text in a save is: a number, a flag, or words."""
    low = text.strip().lower()
    if low in ("true", "false"):
        return "bool"
    try:
        int(text)
        return "int"
    except ValueError:
        pass
    try:
        float(text)
        return "float"
    except ValueError:
        return "str"


def _value_of_text(text: str):
    kind = _kind_of_text(text)
    if kind == "bool":
        return text.strip().lower() == "true"
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


class PlayerPrefsFormat(_Format):
    """Unity PlayerPrefs — a save that is not a file at all.

    On Windows, Unity's PlayerPrefs live in the registry under
    ``HKCU\\Software\\<company>\\<product>``, and a great many games keep
    their whole save there. SaveSync already backs those up, exporting the
    key as JSON (see core/registry_saves); this reads that same export, so
    the two agree by construction about what a key contains.

    Unity mangles the names: a preference called "gold" is stored as
    ``gold_h3096647``, the number being a checksum of the name. The checksum
    cannot be turned back into anything, but it does not need to be — the
    name is in front of it, and only the suffix is hidden for display. It is
    kept, because writing the value back under a different name would leave
    the game unable to find it.

    A value's type is whatever the registry says it is, and it is written
    back as that same type: an integer as a DWORD, a string as the bytes
    Unity writes, terminator included.
    """
    name = "Unity PlayerPrefs"
    engine = "Unity"
    verify_exact = False

    def __init__(self):
        self._doc = None
        self._spots = []          # (values dict, registry name, kind)

    def load(self, data: bytes) -> None:
        try:
            self._doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise SaveEditorError(f"this registry export will not read: {e}") \
                from e
        self._spots = []
        self._gather(self._doc.get("tree") or {}, "")
        if not self._spots:
            raise SaveEditorError("this registry key holds no values to edit")

    def _gather(self, node: dict, where: str) -> None:
        values = node.get("values") or {}
        for regname in sorted(values):
            kind = _prefs_kind(values[regname])
            if kind:
                self._spots.append((values, regname, where))
        for child in sorted(node.get("subkeys") or {}):
            self._gather(node["subkeys"][child],
                         f"{where}\\{child}" if where else child)

    def dump(self) -> bytes:
        return json.dumps(self._doc, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def fields(self) -> list:
        out = []
        for i, (values, regname, where) in enumerate(self._spots):
            kind, value = _prefs_read(values[regname])
            out.append(SaveField((i,), _prefs_label(regname), kind, value,
                                 where or "(this game)"))
        return out

    def set_field(self, path: tuple, value) -> None:
        values, regname, _where = self._spots[path[0]]
        values[regname] = _prefs_write(values[regname], value)


# Unity hides a preference's name behind a checksum of it: "gold" is stored
# as "gold_h3096647". Only the tail is dropped, and only for display.
_PREFS_SUFFIX = re.compile(r"_h\d+$")
# Registry value types, from winreg. Named here so this module reads without
# importing winreg, which does not exist off Windows.
_REG_SZ, _REG_BINARY, _REG_DWORD, _REG_QWORD = 1, 3, 4, 11


def _prefs_label(regname: str) -> str:
    return _PREFS_SUFFIX.sub("", regname) or regname


def _prefs_kind(spec: dict) -> str:
    return _prefs_read(spec)[0]


def _prefs_read(spec: dict) -> tuple:
    """(kind, value) for one exported registry value, or ("", None)."""
    import base64
    import struct
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
    import base64
    import struct
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
        raise SaveEditorError("this preference is of a kind SaveSync leaves "
                              "alone")
    return {"t": vtype, "b": base64.b64encode(raw).decode("ascii")}


class NaninovelFormat(JsonFormat):
    """Naninovel (``.nson``) — JSON deflated with no wrapper around it.

    Naninovel can write its saves as plain text or as binary, and the binary
    one is a raw deflate stream: no zlib header, no gzip header, nothing
    naming it. Handing such a file to anything that expects one of those
    fails, which is why a `.nson` read as plain JSON does not open.

    The deflate settings are the ones that reproduce the file byte for byte,
    found by trying: an unchanged save has to come back out unchanged, and
    for a compressed format that means matching the compressor as well as
    the contents. The plain-text form is left to the JSON reader.
    """
    name = "Naninovel"
    engine = "Naninovel"
    verify_exact = True

    # Raw deflate: a negative window size is what says "no header".
    _WINDOW = -15
    _LEVEL = 6
    _MEMORY = 8

    def __init__(self):
        super().__init__()
        self._inner = []

    def load(self, data: bytes) -> None:
        import zlib
        try:
            plain = zlib.decompressobj(self._WINDOW).decompress(data)
        except zlib.error as e:
            raise SaveEditorError(f"not a deflated save: {e}") from e
        if not plain:
            raise SaveEditorError("the save unpacks to nothing")
        super().load(plain)
        self._unwrap()
        # Whether THIS file can be put back exactly as it came. Most can:
        # the settings above reproduce them to the byte. A save written by a
        # build that packed it differently cannot be, and says so here
        # rather than being refused — it still opens, and is still checked,
        # by reading back what was written and comparing the values.
        self.verify_exact = self._repack(plain) == data

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

    def _repack(self, plain: bytes) -> bytes:
        import zlib
        packer = zlib.compressobj(self._LEVEL, zlib.DEFLATED, self._WINDOW,
                                  self._MEMORY)
        return packer.compress(plain) + packer.flush()

    def dump(self) -> bytes:
        for slot in getattr(self, "_inner", []):
            if slot["dirty"]:
                self.data["objectJsonMap"]["values"][slot["at"]] = json.dumps(
                    slot["data"], ensure_ascii=False, separators=(",", ":"))
        return self._repack(super().dump())

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


class _LzStringJson(JsonFormat):
    """JSON squeezed with LZString and written as base64.

    RPG Maker MV and MZ save this way — and so do a great many HTML games,
    because it is what the LZString library does. The compression therefore
    says nothing about which engine wrote the file, and naming a Twine game
    "RPG Maker MV" because both use the same library helps nobody. What is
    INSIDE tells them apart, so each reader below claims only the shape it
    recognises and the generic one takes whatever is left.
    """
    name = "LZString JSON"
    engine = "HTML game"

    def __init__(self):
        super().__init__()
        self.source_path = None

    def load(self, data: bytes) -> None:
        from core.lzstring import decompress_from_base64
        text = decompress_from_base64(data.decode("ascii", errors="strict").strip())
        if not text:
            raise SaveEditorError("not an LZString payload")
        parsed = json.loads(text)
        if not self.claims(parsed):
            raise SaveEditorError(f"not a {self.name} save")
        self.data = parsed

    def claims(self, data) -> bool:
        return True

    def dump(self) -> bytes:
        from core.lzstring import compress_to_base64
        text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return compress_to_base64(text).encode("ascii")


class RpgMakerMvFormat(_LzStringJson):
    """RPG Maker MV/MZ — the engine's whole game object, compressed."""
    name = "RPG Maker MV"
    engine = "RPG Maker MV/MZ"

    # The engine writes its global objects at the top of every save.
    _MARKERS = {"party", "actors", "switches", "variables", "player"}

    def claims(self, data) -> bool:
        if self.source_path is not None and Path(self.source_path).suffix.lower() \
                in (".rpgsave", ".rmmzsave"):
            return True
        return isinstance(data, dict) and bool(self._MARKERS & set(data))


class SugarCubeFormat(_LzStringJson):
    """Twine's SugarCube — the HTML game format behind a lot of browser games.

    It keeps the whole play history: an index into it, and a chain of deltas
    holding the variables at each step.
    """
    name = "Twine (SugarCube)"
    engine = "Twine / SugarCube"

    def claims(self, data) -> bool:
        state = data.get("state") if isinstance(data, dict) else None
        return isinstance(state, dict) and bool({"index", "delta", "history"}
                                                & set(state))


class RpgMakerMzFormat(JsonFormat):
    """RPG Maker MZ — JSON deflated, and then written out as if it were text.

    MZ does not compress the way MV does. It deflates with zlib and hands the
    result to the file writer as a *string*, so every byte above 0x7f lands on
    disk as the two bytes UTF-8 spells it with. Handing the file straight to
    zlib therefore fails: the wrapper has to come off first.

    Matching MZ's own deflate level is what keeps a save that was opened and
    not changed identical to the one that was there.
    """
    name = "RPG Maker MZ"
    engine = "RPG Maker MV/MZ"
    verify_exact = True

    _LEVEL = 1

    def __init__(self):
        super().__init__()
        self._wrapped = True

    def load(self, data: bytes) -> None:
        import zlib
        try:
            binary = data.decode("utf-8").encode("latin-1")
        except (UnicodeDecodeError, UnicodeEncodeError):
            # Some builds write the bytes as they are. Both are worth trying;
            # which one it was has to be remembered for writing back.
            binary, self._wrapped = data, False
        try:
            plain = zlib.decompress(binary)
        except zlib.error as e:
            raise SaveEditorError(f"not a deflated save: {e}") from e
        super().load(plain)

    def dump(self) -> bytes:
        import zlib
        packed = zlib.compress(super().dump(), self._LEVEL)
        return packed.decode("latin-1").encode("utf-8") if self._wrapped else packed


class KeyValueFormat(_Format):
    """``key = value`` text — config-style saves, and plenty of small games.

    An edit rewrites only the value on its own line: the rest of the file —
    comments, ordering, spacing, blank lines, line endings — is carried
    through as the exact characters it arrived as. That makes the round trip
    exact by construction rather than by luck, which is the difference
    between a format that is safe to write and one that merely usually is.
    """
    name = "Key/value text"
    engine = "Text (key = value)"

    # The only control characters that belong in configuration text.
    _TEXT_CONTROLS = ("\t", "\n", "\r")
    # How much of a file to look at before believing it is text. A config is
    # small; this is far more than enough to catch binary pretending to be one.
    _SNIFF = 1 << 16

    _LINE = re.compile(
        r"^(?P<head>\s*(?P<key>[A-Za-z_][\w .\-]*)\s*[=:]\s*)"
        r"(?P<value>.*?)(?P<eol>\r?\n?)$")
    _SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*\r?\n?$")

    def __init__(self):
        self._lines = []
        self._entries = []       # (line_index, key, kind, value, section)
        self._bom = False

    def load(self, data: bytes) -> None:
        self._bom = data.startswith(b"\xef\xbb\xbf")
        text = data.decode("utf-8-sig" if self._bom else "utf-8")
        if "\n" not in text:
            # One long line is a blob, not a config — base64 saves land here.
            raise SaveEditorError("not a key/value file")
        # Nor is compressed data that merely happened to decode as UTF-8.
        # Without this the editor invents a dozen entries out of noise, with
        # unreadable names and values, instead of saying it cannot read the
        # file — which is what a player reports as "the text is mangled".
        if any(ch < " " and ch not in self._TEXT_CONTROLS
               for ch in text[:self._SNIFF]):
            raise SaveEditorError("not a key/value file")
        self._lines = text.splitlines(keepends=True)
        section = ""
        for i, line in enumerate(self._lines):
            sec = self._SECTION.match(line)
            if sec:
                section = sec.group("name")
                continue
            if line.lstrip().startswith(("#", ";", "//")):
                continue
            m = self._LINE.match(line)
            if not m:
                continue
            raw = m.group("value").strip()
            kind, value = self._typed(raw)
            self._entries.append((i, m.group("key").strip(), kind, value, section))
        if len(self._entries) < 2:
            raise SaveEditorError("not a key/value file")

    @staticmethod
    def _typed(raw: str):
        low = raw.lower()
        if low in ("true", "false"):
            return "bool", low == "true"
        try:
            return "int", int(raw)
        except ValueError:
            pass
        try:
            return "float", float(raw)
        except ValueError:
            pass
        return "str", raw

    def dump(self) -> bytes:
        raw = "".join(self._lines).encode("utf-8")
        return (b"\xef\xbb\xbf" + raw) if self._bom else raw

    def fields(self) -> list:
        return [SaveField((n,), key, kind, value, section)
                for n, key, kind, value, section in self._entries]

    def set_field(self, path: tuple, value) -> None:
        for idx, (n, key, kind, _old, section) in enumerate(self._entries):
            if n != path[0]:
                continue
            m = self._LINE.match(self._lines[n])
            if kind == "bool":
                text = "true" if value else "false"
            else:
                text = str(value)
            self._lines[n] = m.group("head") + text + m.group("eol")
            self._entries[idx] = (n, key, kind, value, section)
            return


class RubyMarshalFormat(_Format):
    """RPG Maker XP / VX / VX Ace, and anything else Ruby dumped.

    A save from those engines is several Marshal streams written back to
    back, so the file is read and written as a sequence. Values are reached
    through a path of steps rather than a dotted string, because the shapes
    involved — arrays, hashes with arbitrary keys, objects with instance
    variables — have no single key type to join on.
    """
    name = "Ruby Marshal"
    engine = "RPG Maker XP/VX/VX Ace"

    def __init__(self):
        self._streams = []

    def load(self, data: bytes) -> None:
        from core.rubymarshal import load_all
        self._streams = load_all(data)
        if not self._streams:
            raise SaveEditorError("no Marshal stream in the file")

    def dump(self) -> bytes:
        from core.rubymarshal import dump_all
        return dump_all(self._streams)

    # ── walking ──
    def _children(self, node):
        """(step, label, child) for everything inside *node*."""
        from core.rubymarshal import RHash, RObject, RStructVal, RUserMarshal

        if isinstance(node, list):
            for i, v in enumerate(node):
                yield ("i", i), str(i), v
        elif isinstance(node, RHash):
            for i, (k, v) in enumerate(node.pairs):
                yield ("h", i), _key_label(k, i), v
        elif isinstance(node, (RObject, RStructVal)):
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            for i, (k, v) in enumerate(pairs):
                yield ("o", i), str(k).lstrip("@"), v
        elif isinstance(node, RUserMarshal):
            yield ("u", 0), "value", node.value

    def _get_parent(self, path):
        node = self._streams[path[0]]
        for step in path[1:-1]:
            node = self._child_at(node, step)
        return node

    def _child_at(self, node, step):
        from core.rubymarshal import RHash, RObject, RStructVal, RUserMarshal
        kind, idx = step
        if kind == "i":
            return node[idx]
        if kind == "h":
            return node.pairs[idx][1]
        if kind == "o":
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            return pairs[idx][1]
        return node.value

    def _set_child(self, node, step, value):
        from core.rubymarshal import RHash, RObject, RStructVal
        kind, idx = step
        if kind == "i":
            node[idx] = value
        elif kind == "h":
            k, _ = node.pairs[idx]
            node.pairs[idx] = (k, value)
        elif kind == "o":
            pairs = node.ivars if isinstance(node, RObject) else node.pairs
            k, _ = pairs[idx]
            pairs[idx] = (k, value)
        else:
            node.value = value

    def fields(self) -> list:
        from core.rubymarshal import RFloat, RString

        out, seen = [], set()

        def walk(node, path, labels, depth):
            # Ruby graphs contain back-references; without an identity guard
            # a save that points at itself would walk forever.
            if depth > _MARSHAL_DEPTH or id(node) in seen:
                return
            seen.add(id(node))
            for step, label, child in self._children(node):
                here = path + (step,)
                names = labels + (label,)
                if isinstance(child, bool):
                    kind = "bool"
                elif isinstance(child, int):
                    kind = "int"
                elif isinstance(child, RFloat):
                    kind = "float"
                elif isinstance(child, RString):
                    kind = "str"
                else:
                    walk(child, here, names, depth + 1)
                    continue
                value = (child.value if isinstance(child, RFloat)
                         else child.text() if isinstance(child, RString) else child)
                out.append(SaveField(here, ".".join(names), kind, value,
                                     names[0] if names else ""))

        for i, stream in enumerate(self._streams):
            walk(stream, (i,), (self._stream_name(stream, i),), 0)
        return out

    @staticmethod
    def _stream_name(stream, index: int) -> str:
        """What the save itself calls this stream.

        Ruby writes an object's class name into the file, and an RPG Maker
        save is a run of them: Game_Switches, Game_Variables, Game_Party and
        the rest. Naming them is not decoration — Game_Switches and
        Game_Variables both keep their contents in an ivar called ``data``, so
        without the class name switch 12 and variable 12 are the same label,
        which makes them impossible to tell apart and impossible to hold.
        """
        from core.rubymarshal import RObject
        return stream.cls if isinstance(stream, RObject) else str(index)

    def set_field(self, path: tuple, value) -> None:
        from core.rubymarshal import RFloat, RString

        parent = self._get_parent(path)
        current = self._child_at(parent, path[-1])
        if isinstance(current, RFloat):
            value = current.with_value(value)
        elif isinstance(current, RString):
            # Keep the string OBJECT: it may be shared, and replacing it
            # would turn a back-reference into a second copy.
            current.data = str(value).encode("utf-8")
            return
        elif isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int):
            value = int(value)
        self._set_child(parent, path[-1], value)


def _key_label(key, index: int) -> str:
    from core.rubymarshal import RString
    if isinstance(key, RString):
        return key.text()
    if isinstance(key, (str, int)):
        return str(key).lstrip("@")
    return str(index)


class GvasFormat(_Format):
    """Unreal Engine ``.sav`` (GVAS), UE4 and UE5 up to 5.3.

    The property list is decoded; the scalar values in it are editable and
    everything else travels as the bytes it arrived as. See core/gvas for why
    the layout is taken from the reference implementation rather than
    reconstructed.
    """
    name = "Unreal Engine"
    engine = "Unreal Engine (GVAS)"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.gvas import GvasError, loads
        try:
            self._save = loads(data)
        except GvasError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, _leading_group(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        from core.gvas import GvasError
        try:
            self._save.set_value(path[0], value)
        except GvasError as e:
            raise SaveEditorError(str(e)) from e


class UnrealEncryptedFormat(GvasFormat):
    """An Unreal save the game locked with a key of its own.

    The same file GvasFormat reads, with everything — the magic included —
    under encryption. See core/unreal_crypt: the key is never guessed at, it
    is supplied, and it is accepted only when what comes out starts with
    GVAS. So this either opens the real save or declines; there is no middle
    where it might produce something plausible and wrong.
    """
    name = "Unreal Engine (encrypted)"
    engine = "Unreal Engine (GVAS)"

    def __init__(self):
        super().__init__()
        self.source_path = None
        self.game_dir = None
        # Told how long the hunt has been going, and able to call it off —
        # see open_save. None means let it run.
        self.progress = None
        self._started = None
        self._key = ""
        self._how = ""

    def _places(self) -> list:
        """Where a key for this save might be kept, nearest first."""
        out = []
        if self.source_path is not None:
            here = Path(self.source_path).parent
            out.append(here)
            out.extend(list(here.parents)[:3])
        if self.game_dir:
            out.append(Path(self.game_dir))
        return out

    def _find_key(self, data: bytes) -> tuple:
        """A key that opens this save: remembered, given, or hunted for.

        In that order, because that is the order of what they cost — the
        first two are instant and the third reads the game's compiled code.
        """
        from core.game_keys import key_from_file, stored_key
        from core.unreal_crypt import KEY_FILE, decrypt, find_key, game_binaries
        places = self._places()
        for place in places:
            for candidate in (stored_key("unreal", place),
                              key_from_file(place, KEY_FILE)):
                if not candidate:
                    continue
                plain, how = decrypt(data, candidate)
                if plain:
                    return plain, candidate, how, place
        # Nothing to hand, so look in the game itself — the same thing Easy
        # Save does, except that an Unreal key has no marker to look up and
        # has to be found by trying. Only possible with the game in reach:
        # its saves live under the user's profile, nowhere near it.
        if not self.game_dir:
            return b"", "", "", None
        binaries = game_binaries(self.game_dir)
        if not binaries:
            return b"", "", "", None
        key, how = find_key(data, binaries, on_tick=self._tick)
        if key:
            plain, how2 = decrypt(data, key)
            if plain:
                # Written down against the save, not against the game that
                # yielded it: the save is what will be opened next time, and
                # it may well be opened with the game out of reach.
                return plain, key, how2 or how, places[0] if places else None
        return b"", "", "", None

    def _tick(self):
        """Report how long the hunt has run, and whether to carry on."""
        if self.progress is None:
            return True
        import time
        if self._started is None:
            self._started = time.monotonic()
        return self.progress(time.monotonic() - self._started) is not False

    def load(self, data: bytes) -> None:
        from core.game_keys import store_key
        plain, key, how, place = self._find_key(data)
        if not plain:
            raise SaveEditorError(
                "this Unreal save is encrypted by the game and no key for it "
                "was found, in the save's own folders or in the game")
        self._key, self._how = key, how
        super().load(plain)
        if place is not None:
            store_key("unreal", place, key)

    def dump(self) -> bytes:
        from core.unreal_crypt import encrypt
        return encrypt(super().dump(), self._key, self._how)


class RenpyFormat(_Format):
    """Ren'Py ``.save`` — a zip around a pickle. See core/renpy_save for why
    the pickle is read opcode by opcode instead of being unpickled, and why an
    edited save has to be re-signed."""

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
        from core.renpy_save import loads, RenpyError
        try:
            self._save = loads(data)
        except RenpyError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        from core.renpy_save import RenpyError
        try:
            return self._save.dump()
        except RenpyError as e:
            raise SaveEditorError(str(e)) from e

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, self._group_of(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


class LcfFormat(_Format):
    """RPG Maker 2000/2003 ``.lsd``. See core/lcf: chunks this reader does not
    understand travel as their own bytes, so the file rebuilds exactly."""
    name = "RPG Maker 2000/2003"
    engine = "RPG Maker 2000/2003"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.lcf import LcfError, loads
        try:
            self._save = loads(data)
        except LcfError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.lcf import LcfError
        try:
            self._save.set_value(path[0], value)
        except LcfError as e:
            raise SaveEditorError(str(e)) from e


class SolFormat(_Format):
    """Adobe Flash shared objects (``.sol``), AMF0 and AMF3.

    Edited by splicing, like Ren'Py's pickle — see core/sol for why that
    matters more here than anywhere else.
    """
    name = "Adobe Flash"
    engine = "Flash (shared object)"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.sol import loads, SolError
        try:
            self._save = loads(data)
        except SolError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, _leading_group(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


class QspFormat(_Format):
    """QSP (Quest Soft Player) saves. See core/qsp: this is the one format
    where the round trip proves nothing, so the reader instead has to account
    for every line in the file."""
    name = "QSP"
    engine = "QSP (Quest Soft Player)"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.qsp import loads, QspError
        try:
            self._save = loads(data)
        except QspError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, _leading_group(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


class Es3Format(JsonFormat):
    """Unity Easy Save 3 with encryption turned on.

    Inside it is JSON, so once it is open it behaves like any other JSON save
    — except that Easy Save wraps every value in its own type note, which is
    bookkeeping rather than anything to edit. See core/es3 for where the
    password comes from.
    """
    name = "Easy Save 3"
    engine = "Unity (Easy Save 3)"
    verify_exact = False

    def __init__(self):
        super().__init__()
        self.source_path = None
        self.game_dir = None
        # Told how long the hunt for a password has been going, and able to
        # call it off — see open_save. None means let it run.
        self.progress = None
        self._iv = b""
        self._password = ""

    def load(self, data: bytes) -> None:
        from core.es3 import Es3Error, decrypt, find_password, is_encrypted
        if not is_encrypted(data):
            # Encryption is optional, and most games leave it off.
            return super().load(data)
        self._password = find_password(data, self.source_path, self.game_dir,
                                       progress=self.progress)
        if not self._password:
            raise SaveEditorError(
                "this Easy Save 3 file is encrypted and its password is not "
                "in the game's files — put the key in an es3.key file beside "
                "the save")
        self._iv = data[:16]
        try:
            super().load(decrypt(data, self._password))
        except Es3Error as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        from core.es3 import dumps, encrypt
        # Easy Save's own layout, not the compact one: a save that was opened
        # and left alone then comes back out as the identical file.
        plain = dumps(self.data)
        return encrypt(plain, self._password, self._iv) if self._password else plain

    def fields(self) -> list:
        # Easy Save stores each value as {"__type": ..., "value": ...}. The
        # type is not the player's business, and the name reads better without
        # the ".value" that every single entry would otherwise carry.
        out = []
        for f in super().fields():
            if f.path and f.path[-1] == "__type":
                continue
            label = f.label
            if label.endswith(".value"):
                label = label[:-len(".value")]
            out.append(SaveField(f.path, label, f.kind, f.value, f.group))
        return out


class RagsFormat(_Format):
    """RAGS (``.rsv``) — a .NET object graph behind AES.

    See core/rags: the whole save is read, but only the objects holding game
    state are offered. The rest is the game's own logic and presentation, and
    there are three million values of it.
    """
    name = "RAGS"
    engine = "Rapid Adventure Game System"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.rags import RagsError, loads
        try:
            self._save = loads(data)
        except RagsError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.rags import RagsError
        try:
            self._save.set_value(path[0], value)
        except RagsError as e:
            raise SaveEditorError(str(e)) from e


class KirikiriFormat(_Format):
    """KiriKiri / KAG saves (``.ksd``).

    The save is the game's state written as a TJS dictionary, in UTF-16 —
    readable text, not a binary structure. See core/kirikiri for the three
    wrappers it arrives in and for why the real proof is that the walk
    accounts for every character rather than that the file round-trips.
    """
    name = "KiriKiri"
    engine = "KiriKiri / KAG"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.kirikiri import KirikiriError, loads
        try:
            self._save = loads(data)
        except KirikiriError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


class WolfFormat(_Format):
    """Wolf RPG Editor saves.

    The file is obfuscated (core/wolf) and its values live in a variable
    database near the end (core/wolf_save). Field names come from the game's
    own database when it is lying about; a game that packs it away still gets
    every value, just numbered.
    """
    name = "Wolf RPG"
    engine = "Wolf RPG Editor"

    def __init__(self):
        self._save = None
        self.source_path = None

    def load(self, data: bytes) -> None:
        import struct
        from core.wolf import WolfError
        from core.wolf_save import loads
        try:
            self._save = loads(data, save_path=self.source_path)
        except (WolfError, struct.error, IndexError, ValueError) as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField((i,), name, kind, value, name.split(" / ")[0])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)


class AliceSoftFormat(_Format):
    """AliceSoft System 4 global data (``.asd``, and ``.sav``).

    See core/alicesoft, which is written from the engine reimplementation the
    format is described in rather than from staring at bytes. Two different
    things arrive in the same container: the global data, which is named and
    typed, and the numbered save slots, which are a dump of the virtual
    machine. Both open — from a slot it is the game's own global variables
    that are offered, and with the game in the library they carry the names
    the game gave them.
    """
    name = "AliceSoft System 4"
    engine = "AliceSoft System"

    def __init__(self):
        self._save = None
        self.game_dir = None

    def load(self, data: bytes) -> None:
        from core.alicesoft import AliceSoftError, loads
        try:
            self._save = loads(data, game_dir=self.game_dir)
        except AliceSoftError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.alicesoft import AliceSoftError
        try:
            self._save.set_value(path[0], value)
        except AliceSoftError as e:
            raise SaveEditorError(str(e)) from e


class ArtemisFormat(_Format):
    """Artemis Engine settings (``system.dat``).

    See core/artemis, including why the numbered slots beside this file are
    named rather than opened.
    """
    name = "Artemis"
    engine = "Artemis"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.artemis import ArtemisError, loads
        try:
            self._save = loads(data)
        except ArtemisError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        groups = self._save.groups()
        return [SaveField((i,), name, kind, value, groups[i])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.artemis import ArtemisError
        try:
            self._save.set_value(path[0], value)
        except ArtemisError as e:
            raise SaveEditorError(str(e)) from e


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
        from core.tyrano import TyranoError, loads
        try:
            self.data = loads(data)
        except TyranoError as e:
            raise SaveEditorError(str(e)) from e
        if not self._roots():
            raise SaveEditorError("no game values in this TyranoScript save")

    def dump(self) -> bytes:
        from core.tyrano import dumps
        return dumps(self.data)

    def _roots(self) -> list:
        from core.tyrano import state_roots
        return state_roots(self.data)

    def fields(self) -> list:
        from core.tyrano import at
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


# ── Detection ────────────────────────────────────────────────────────────────

_BY_EXTENSION = {
    ".json": JsonFormat,
    # Naninovel writes either a deflate stream or plain text; the deflated
    # reader is tried first and the JSON one catches the rest.
    ".nson": NaninovelFormat,
    ".es3": Es3Format,            # JSON, encrypted or not, in Easy Save's layout
    ".rpgsave": RpgMakerMvFormat,
    ".rmmzsave": RpgMakerMzFormat,   # MZ deflates; MV's LZString is tried after
    ".xml": XmlFormat,            # .NET's serializer, so Unity and Godot too
    ".ini": KeyValueFormat,
    ".cfg": KeyValueFormat,
    ".conf": KeyValueFormat,
    ".lsd": LcfFormat,
    ".sol": SolFormat,
    ".rsv": RagsFormat,           # RAGS: .NET objects behind fixed AES
    ".save": RenpyFormat,         # Ren'Py; Unity/Godot .save fall through
    ".sav": GvasFormat,           # Unreal; other engines' .sav fall through
    ".rvdata2": RubyMarshalFormat,
    ".rvdata": RubyMarshalFormat,
    ".rxdata": RubyMarshalFormat,
}


def _in_unreal_save_folder(path: Path) -> bool:
    """Whether *path* sits where Unreal itself puts a game's saves.

    ``<Game>/Saved/SaveGames`` is the engine's own layout, not something a
    game chooses, so it identifies an Unreal save even when the file will not
    identify itself.
    """
    parts = [p.lower() for p in Path(path).parts]
    return "savegames" in parts and "saved" in parts


def _looks_encrypted_unreal(data: bytes) -> bool:
    try:
        from core.unreal_crypt import looks_encrypted
        return looks_encrypted(data)
    except Exception:
        return False


def _candidates(path: Path, data: bytes) -> list:
    """Formats worth trying for this file, best guess first.

    Extension first because it is the strongest hint, then content: plenty of
    engines put JSON in a .dat or a .save, and plenty put something else in a
    .sav.
    """
    out = []
    ext = path.suffix.lower()
    if ext in _BY_EXTENSION:
        out.append(_BY_EXTENSION[ext])
    # Ruby stamps every Marshal stream with its version, which is as strong
    # a signal as a magic number — and RPG Maker also writes .dat files this
    # way, which no extension would have told us.
    if data[:2] == b"":
        out.append(RubyMarshalFormat)
    if data.startswith(b"GVAS"):
        out.append(GvasFormat)
    # A .sol carries its magic six bytes in, after the version and length.
    if data[6:10] == b"TCSO":
        out.append(SolFormat)
    # KiriKiri by its extension, and by its compressed marker whatever it is
    # called. The other two wrappers — plain UTF-16 text, or a thumbnail
    # bitmap — are far too ordinary to claim a file on their own.
    if ext == ".ksd" or data.startswith(b"\xfe\xfe\x02\xff\xfe"):
        out.append(KirikiriFormat)
    # Wolf hides behind obfuscation, so only unlocking it can tell.
    if ext == ".sav" and len(data) > 0x20:
        try:
            from core.wolf import is_wolf_save
            if is_wolf_save(data):
                out.append(WolfFormat)
        except Exception:
            pass
    # AliceSoft names itself in its first four bytes, which is just as well:
    # it puts the same container behind .asd and behind .sav, and what is
    # INSIDE decides whether it can be opened at all.
    if data[:4] in (b"GD\x01\x01", b"PSR\x00"):
        out.append(AliceSoftFormat)
    # An Unreal save whose game encrypted it says nothing about itself — the
    # magic is under the encryption with everything else. Where it SITS says
    # it instead: "Saved/SaveGames" is Unreal's own folder, written by the
    # engine and not by the game. Tried last, and only ever with a key that
    # then has to produce the magic, so a file that merely lives there and is
    # something else costs one failed decryption.
    if _in_unreal_save_folder(path) and _looks_encrypted_unreal(data):
        out.append(UnrealEncryptedFormat)
    # Artemis writes settings, global data and slots all into a .dat, and all
    # three name themselves in the first four bytes.
    if data[:3] == b"BOW":
        out.append(ArtemisFormat)
    # TyranoScript also writes .sav, so the extension cannot tell it from
    # Unreal or Wolf. What can is that its JSON arrives escaped: the opening
    # brace is on disk as the three characters "%7B", which nothing else here
    # starts with.
    if data[:3] in (b"%7B", b"%5B"):
        out.append(TyranoFormat)
    # QSP names itself in the clear, in either of the two encodings it uses.
    if (data.startswith(b"QSPSAVEDGAME")
            or data.startswith("QSPSAVEDGAME".encode("utf-16-le"))):
        out.append(QspFormat)
    if data.startswith(b"PK"):
        out.append(RenpyFormat)
    # The LCF name is length-prefixed, so an .lsd starts with its length.
    if data[:1] == bytes([len(b"LcfSaveData")]) and data[1:12] == b"LcfSaveData":
        out.append(LcfFormat)
    head = data[:1].lstrip()
    if head[:1] in (b"{", b"["):
        out.append(JsonFormat)
    # XML, whatever the file is called: a game that saves through .NET's
    # serializer often names the result .sav or .dat.
    if data[:200].lstrip()[:1] == b"<":
        out.append(XmlFormat)
    # A deflate stream opens with a marker whose two bytes are both under
    # 0x80, so it survives MZ's text wrapper and can be recognised as it is.
    if data[:1] == b"x" and len(data) > 8:
        out.append(RpgMakerMzFormat)
    # LZString base64: cheap to try and it fails fast. Which engine wrote it
    # is decided by what comes out, most particular reader first.
    if data[:1].isalnum() and len(data) > 8 and re.fullmatch(
            rb"[A-Za-z0-9+/=\s]+", data[:512] or b""):
        out.append(RpgMakerMvFormat)
        out.append(SugarCubeFormat)
        out.append(_LzStringJson)
    out.append(JsonFormat)
    out.append(KeyValueFormat)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


@dataclass
class SaveDocument:
    """A save file opened for editing."""
    path: Path
    format_name: str
    engine: str
    fields: list = _field(default_factory=list)
    _fmt: object = None
    _original: bytes = b""
    # Set when the save is a registry key rather than a file — see open_save.
    _registry: str = ""
    # The values as they stand in the file, for the formats whose bytes
    # cannot be compared — see dirty_against_disk.
    _baseline: tuple = ()

    def set_value(self, path: tuple, value) -> None:
        self._fmt.set_field(path, value)
        for f in self.fields:
            if f.path == path:
                f.value = value

    def dirty_against_disk(self) -> bool:
        """Whether anything here differs from what is in the file.

        Comparing the bytes is right only for a format that rebuilds them
        exactly. The others re-encode — LZString picks a different packing,
        a zip is written afresh, JSON is spelled without its spacing — so an
        untouched save comes out as different bytes carrying identical
        values, and comparing bytes would call it modified when nobody
        modified it. For those the values are what is compared, which is
        what the question is actually asking.
        """
        if getattr(self._fmt, "verify_exact", True):
            return self._fmt.dump() != self._original
        return self._value_snapshot() != self._baseline

    def _value_snapshot(self) -> tuple:
        return tuple((f.path, f.value) for f in self._fmt.fields())

    def save(self) -> Path:
        """Write the edits back, keeping the original first.

        Returns the path of the copy that was set aside — the thing "undo"
        needs. Writing happens only after that copy exists.
        """
        if self._registry:
            # Nothing to copy aside on disk, so the copy IS the export: the
            # key exactly as it stands, written where a file's backup would
            # go. Restoring it is the same import that writing uses.
            backup = backup_original(self.path, self._original)
            self.write_without_backup()
            return backup
        backup = backup_original(self.path)
        self.write_without_backup()
        return backup

    def write_without_backup(self) -> None:
        """Write the edits with no copy taken.

        For the value-hold loop, which takes ONE copy when it starts and then
        rewrites the same file repeatedly — a copy per cycle would bury the
        original under near-identical files. Everything else must go through
        save(), which keeps the original first.

        Written to a temporary file and moved into place, so a game reading
        the save mid-write sees either the old file or the new one, never
        half of each.
        """
        data = self._fmt.dump()
        if self._registry:
            from core.registry_saves import import_registry_tree
            if not import_registry_tree(self._registry, data):
                raise SaveEditorError(
                    "the changes could not be written back to the registry")
            self._original = data
            self._baseline = self._value_snapshot()
            return
        tmp = self.path.with_suffix(self.path.suffix + ".savesync-tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        self._original = data
        self._baseline = self._value_snapshot()


def describe(path) -> str:
    """The engine this file looks like, for a file we cannot edit."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[0] if known else ""


def why_not(path) -> str:
    """Why that file cannot be opened, in a few words."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[1] if known else ""


def open_save(path, game_dir=None, progress=None) -> SaveDocument:
    """Open *path* for editing, or explain why it cannot be.

    *game_dir* is where the game itself is installed, when that is known. A
    save does not always sit with its game — Unity puts them under the user's
    profile — and one format needs to look in the game's own files.

    *progress* is for the one format whose search can run long: it is called
    with the seconds elapsed and stops the search by returning False. Every
    other format ignores it.
    """
    # Unity's PlayerPrefs are a save that is not a file: SaveSync proposes
    # them as "registry:HKCU\..." and backs them up already, so the same
    # export is what gets edited here. Everything downstream then works on
    # bytes exactly as it does for a file.
    from core.registry_saves import (export_registry_key, is_registry_path,
                                     registry_display)
    registry = str(path) if is_registry_path(str(path)) else ""
    if registry:
        p = Path(registry_display(registry).replace("\\", "/"))
        data = export_registry_key(registry)
        if not data:
            raise SaveEditorError(
                "that registry key could not be read, or holds nothing")
    else:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise SaveEditorError(f"cannot read {p.name}: {e}",
                                  "cheats.err_cannot_read", name=p.name,
                                  reason=str(e)) from e
        if not data:
            raise SaveEditorError("the file is empty", "cheats.err_empty")

    def prepare(cls):
        """A reader, told where the file came from.

        A couple of formats need that: Wolf looks beside the save for the
        game's database, Easy Save 3 looks inside the game for its password.
        The verification below builds a SECOND reader, and it has to be given
        the same context — without it that reader cannot open what the first
        one just wrote, and a perfectly good save is rejected.
        """
        fmt = cls()
        if hasattr(fmt, "source_path"):
            fmt.source_path = p
        if game_dir and hasattr(fmt, "game_dir"):
            fmt.game_dir = game_dir
        if progress is not None and hasattr(fmt, "progress"):
            fmt.progress = progress
        return fmt

    for cls in ([PlayerPrefsFormat] if registry else _candidates(p, data)):
        fmt = prepare(cls)
        try:
            fmt.load(data)
        except Exception as e:
            # Every format is tried in turn, so a file that is not this one's
            # is entirely normal. Worth a line all the same: without it, a
            # mistake inside a reader is indistinguishable from a file that
            # simply was not that format.
            logger.debug(f"{p.name}: not {cls.name} ({type(e).__name__}: {e})")
            continue
        # The round trip is the whole safety argument: if we cannot rebuild
        # what we just read, we do not understand the file well enough to
        # write to it, whatever the extension says.
        try:
            rebuilt = fmt.dump()
        except Exception:
            continue
        # Asked of the reader, not of its class: a format can only know
        # which guarantee it can offer once it has seen the file. Naninovel
        # is the case — most of its saves are rebuilt byte for byte, and the
        # odd one written by a different build of the game is not.
        if not fmt.verify_exact:
            # Re-serialised formats differ in whitespace or compression, so
            # equality is checked where it means something: reading the
            # rebuilt bytes must give back the same values.
            try:
                probe = prepare(cls)
                probe.load(rebuilt)
                if ([(f.label, f.value) for f in probe.fields()]
                        != [(f.label, f.value) for f in fmt.fields()]):
                    continue
            except Exception:
                continue
        elif rebuilt != data:
            logger.info(f"{p.name}: {cls.name} round trip differs — read-only")
            continue
        fields = fmt.fields()
        if not fields:
            continue
        doc = SaveDocument(path=p, format_name=cls.name, engine=cls.engine,
                           fields=fields, _fmt=fmt, _original=data,
                           _registry=registry)
        doc._baseline = doc._value_snapshot()
        return doc

    known = describe(p)
    if known:
        reason = why_not(p)
        # The missing piece for some of these is not in the save at all — it
        # is in the GAME, and a save dropped in on its own arrives with no
        # game behind it. Then "cannot be opened" is not the whole answer:
        # adding the game is a step that gets somewhere, and leaving it out
        # hides it. Once the game IS known, the reader has already looked, so
        # the offer is dropped rather than repeated at someone who took it.
        if not game_dir:
            reason = _KEY_LIVES_IN_THE_GAME.get(p.suffix.lower(), reason)
        raise SaveEditorError(
            f"{p.name} looks like a {known} save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=known,
            reason=reason)
    # An Unreal save that GVAS could not read is worth saying so about, rather
    # than calling it unrecognised: the file IS one, and what stopped the
    # reader is something inside it rather than the format being a mystery.
    if data[:4] == b"GVAS":
        raise SaveEditorError(
            f"{p.name} is an Unreal Engine save, but SaveSync could not read "
            f"all the way through it",
            "cheats.err_unreal_new", name=p.name)
    # An encrypted Unreal save, which by then is one whose key was not found:
    # saying so is the difference between a file nobody can identify and one
    # that only needs its key.
    if _in_unreal_save_folder(p) and _looks_encrypted_unreal(data):
        from core.unreal_crypt import KEY_FILE
        reason = (f"the game encrypted it with a key of its own, and none was "
                  f"found — put the key in a {KEY_FILE} file beside the save")
        if not game_dir:
            reason += ", or add the game to the library"
        raise SaveEditorError(
            f"{p.name} is an Unreal Engine save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name,
            engine="Unreal Engine", reason=reason)
    # Artemis puts its settings, its across-playthroughs data and its slots
    # all into a .dat. Only the first opens; the other two are worth naming.
    if data[:3] == b"BOW":
        reason = ("its values sit in a tagged tree, and following one wrongly "
                  "would write a number into the wrong place")
        raise SaveEditorError(
            f"{p.name} looks like an Artemis save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine="Artemis",
            reason=reason)
    # AliceSoft puts the same container behind a .sav as often as behind a
    # .asd, and the extension map cannot see that. Its own header can.
    if _is_alicesoft(data):
        engine, reason = _RECOGNISED_ONLY[".asd"]
        raise SaveEditorError(
            f"{p.name} looks like a {engine} save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=engine,
            reason=reason)
    # Some engines encrypt their saves outright, and an encrypted file has
    # nothing in it to recognise — every byte is as likely as every other. The
    # only thing that can name one is the game it belongs to, which is what
    # the engine detector is for. Asked last, and only about a file nothing
    # else claimed, so it cannot take a save away from a reader that works.
    engine_said, have_game = _engine_that_encrypts(p, game_dir)
    if engine_said:
        reason = ("the engine encrypts it, and the key is inside the game's "
                  "own program rather than in the save")
        if not have_game:
            reason += (" — add the game to the library so SaveSync has its "
                       "executable to look in")
        raise SaveEditorError(
            f"{p.name} looks like a {engine_said} save, which SaveSync cannot "
            f"edit yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=engine_said,
            reason=reason)
    raise SaveEditorError(f"{p.name} is not a save format SaveSync can read",
                          "cheats.err_unreadable", name=p.name)


# Engines whose saves are encrypted with a key that lives in the game, so
# that no amount of looking at the file will identify or open it.
_ENCRYPTING_ENGINES = ("srpgstudio",)

# What to say instead, for formats whose missing piece is inside the GAME,
# when SaveSync does not know where the game is.
#
# A separate sentence rather than a line tacked onto the usual one, because
# the usual one is not true in this case: _RECOGNISED_ONLY says the password
# "is not in the game's files", and with no game to look in, nothing looked.
# Saying so, and naming the step that would fix it, is the difference between
# a dead end and an instruction.
_KEY_LIVES_IN_THE_GAME = {
    ".es3": ("it is encrypted, and the password is baked into the game's own "
             "build rather than kept in the save — add the game to the "
             "library and SaveSync will read it out of there, or put the key "
             "in an es3.key file beside the save"),
}


def _engine_that_encrypts(path: Path, game_dir=None) -> tuple:
    """(engine name, whether the game itself was found) for an encrypted save.

    ("", False) for every other file.

    The game folder is used when it is known, and the save's own folder is
    tried when it is not — these engines keep their saves under the game, so
    the detector's walk upward usually reaches it either way. Which of the two
    answered matters: the key to one of these saves is inside the game's own
    program file, so a game SaveSync cannot see is a game whose saves it could
    not open even once it knows how. Saying which case it is turns a dead end
    into something the player can act on — add the game, and the executable
    comes with it.
    """
    from core.game_engine import detect_engine, label
    if game_dir:
        engine = detect_engine(game_dir=str(game_dir))
        if engine in _ENCRYPTING_ENGINES:
            return label(engine), True
    engine = detect_engine(game_dir=str(path.parent))
    if engine in _ENCRYPTING_ENGINES:
        return label(engine), bool(game_dir)
    return "", False


# ── Keeping the original ─────────────────────────────────────────────────────

def _slot_dir(path: Path, create: bool = True) -> Path:
    """Where copies of *path* are kept.

    Only creating it when something is about to be WRITTEN there. Reading is
    the common case — the editor asks about every save it lists — and a read
    that creates a folder left one empty directory behind per save file ever
    looked at, hundreds of them, none of which pruning could ever tidy.
    """
    # Keyed by the full path so two games' "save1.json" never collide.
    import hashlib
    key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    d = _BACKUP_DIR / key
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def backup_original(path, contents: bytes = None) -> Path:
    """Put a dated copy of *path* aside and return where it went.

    *contents* is for a save that is not a file — a registry key, whose
    "original" is the export taken when it was opened. Everything else about
    keeping copies, naming them and pruning them is the same either way.
    """
    p = Path(path)
    d = _slot_dir(p)
    # Milliseconds AND a collision guard: saving and then undoing happen
    # within the same second, and a second-resolution name would have the
    # undo's copy overwrite the pristine original it exists to protect.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    dest = d / f"{stamp}__{p.name}"
    n = 1
    while dest.exists():
        dest = d / f"{stamp}-{n}__{p.name}"
        n += 1
    if contents is None:
        shutil.copy2(p, dest)
    else:
        dest.write_bytes(contents)
    (d / "origin.txt").write_text(str(p), encoding="utf-8")
    prune_backups(p)
    logger.info(f"Kept the original of {p.name} at {dest.name}")
    return dest


def copy_policy() -> tuple:
    """How many copies to keep of one save, and for how many days."""
    try:
        from core.config_manager import get_config
        cfg = get_config()
        return (max(1, int(cfg.get("save_edit_copies", _DEFAULT_COPIES))),
                max(1, int(cfg.get("save_edit_copy_days", _DEFAULT_COPY_DAYS))))
    except Exception:
        # No configuration to ask — headless, or before the app is up.
        return _DEFAULT_COPIES, _DEFAULT_COPY_DAYS


def prune_backups(path) -> int:
    """Apply both rules to the copies kept of *path*; returns how many went.

    The newest copy is never dropped, whatever its age. Age alone could
    otherwise clear the lot — leaving an edit with nothing to undo it with,
    which is the one thing these copies exist to prevent.
    """
    keep, days = copy_policy()
    p = Path(path)
    try:
        # By time, not by name: two copies taken in the same millisecond get a
        # collision suffix, and "...-1__name" sorts BEFORE "...__name", which
        # would make the newest look like the oldest.
        kept = [f for f, _ in reversed(list_backups(p))]
    except OSError:
        return 0
    cutoff = datetime.now().timestamp() - days * 86400
    gone = 0
    # Oldest first, and never the last one standing.
    for old in kept[:-1]:
        too_many = len(kept) - gone > keep
        try:
            too_old = old.stat().st_mtime < cutoff
        except OSError:
            continue
        if not (too_many or too_old):
            continue
        try:
            old.unlink()
            gone += 1
        except OSError:
            pass
    if gone:
        logger.info(f"Dropped {gone} old copies of {p.name} "
                    f"(keeping {keep}, for {days} days)")
    return gone


def prune_all() -> int:
    """Apply the rules to every save the editor has kept copies of.

    Run once when the app starts. Without it the age rule would only hold for
    saves somebody happens to open again — edit a file today, never look at
    that game again, and its copies would sit there for good, which is not
    what "delete after seven days" says. Each slot folder remembers the file
    it belongs to, so they can all be found from here.
    """
    gone = 0
    try:
        slots = list(_BACKUP_DIR.iterdir())
    except OSError:
        return 0
    for slot in slots:
        if not slot.is_dir():
            continue
        try:
            origin = (slot / "origin.txt").read_text(encoding="utf-8").strip()
        except OSError:
            # A folder with nothing in it and no origin: left behind by an
            # older version, which made one for every save it merely LOOKED
            # at. rmdir refuses a folder holding anything, so this can only
            # ever remove the empty ones.
            try:
                slot.rmdir()
            except OSError:
                pass
            continue
        if origin:
            try:
                gone += prune_backups(Path(origin))
            except OSError:
                continue
    if gone:
        logger.info(f"Cleared {gone} old save-editor copies at startup")
    return gone


def list_backups(path) -> list:
    """Copies kept for *path*, newest first, as (file, modified)."""
    p = Path(path)
    out = []
    d = _slot_dir(p, create=False)
    if not d.is_dir():
        return []
    try:
        for f in d.glob(f"*__{p.name}"):
            try:
                out.append((f, datetime.fromtimestamp(f.stat().st_mtime)))
            except OSError:
                continue
    except OSError:
        return []
    return sorted(out, key=lambda t: t[1], reverse=True)


def restore_backup(backup, target) -> None:
    """Put a kept copy back. The file being replaced is itself kept first, so
    an undo can be undone."""
    b, t = Path(backup), Path(target)
    if not b.is_file():
        raise SaveEditorError("that copy is no longer there",
                              "cheats.err_copy_gone")
    if t.exists():
        backup_original(t)
    shutil.copy2(b, t)
    logger.info(f"Restored {t.name} from {b.name}")
