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
}

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


def _walk(node, prefix=(), group="") -> list:
    """Every scalar leaf of a JSON-shaped structure, with its path."""
    out = []
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
        self._iv = b""
        self._password = ""

    def load(self, data: bytes) -> None:
        from core.es3 import Es3Error, decrypt, find_password, is_encrypted
        if not is_encrypted(data):
            # Encryption is optional, and most games leave it off.
            return super().load(data)
        self._password = find_password(data, self.source_path, self.game_dir)
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


# ── Detection ────────────────────────────────────────────────────────────────

_BY_EXTENSION = {
    ".json": JsonFormat,
    ".nson": JsonFormat,          # Naninovel
    ".es3": Es3Format,            # JSON, encrypted or not, in Easy Save's layout
    ".rpgsave": RpgMakerMvFormat,
    ".rmmzsave": RpgMakerMzFormat,   # MZ deflates; MV's LZString is tried after
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

    def set_value(self, path: tuple, value) -> None:
        self._fmt.set_field(path, value)
        for f in self.fields:
            if f.path == path:
                f.value = value

    def dirty_against_disk(self) -> bool:
        return self._fmt.dump() != self._original

    def save(self) -> Path:
        """Write the edits back, keeping the original first.

        Returns the path of the copy that was set aside — the thing "undo"
        needs. Writing happens only after that copy exists.
        """
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
        tmp = self.path.with_suffix(self.path.suffix + ".savesync-tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        self._original = data


def describe(path) -> str:
    """The engine this file looks like, for a file we cannot edit."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[0] if known else ""


def why_not(path) -> str:
    """Why that file cannot be opened, in a few words."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[1] if known else ""


def open_save(path, game_dir=None) -> SaveDocument:
    """Open *path* for editing, or explain why it cannot be.

    *game_dir* is where the game itself is installed, when that is known. A
    save does not always sit with its game — Unity puts them under the user's
    profile — and one format needs to look in the game's own files.
    """
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
        return fmt

    for cls in _candidates(p, data):
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
        if not cls.verify_exact:
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
        return SaveDocument(path=p, format_name=cls.name, engine=cls.engine,
                            fields=fields, _fmt=fmt, _original=data)

    known = describe(p)
    if known:
        raise SaveEditorError(
            f"{p.name} looks like a {known} save, which SaveSync cannot edit "
            f"yet: {why_not(p)}",
            "cheats.err_known_not_editable", name=p.name, engine=known,
            reason=why_not(p))
    # An Unreal save that GVAS could not read is worth saying so about, rather
    # than calling it unrecognised: the file IS one, and what stopped the
    # reader is something inside it rather than the format being a mystery.
    if data[:4] == b"GVAS":
        raise SaveEditorError(
            f"{p.name} is an Unreal Engine save, but SaveSync could not read "
            f"all the way through it",
            "cheats.err_unreal_new", name=p.name)
    raise SaveEditorError(f"{p.name} is not a save format SaveSync can read",
                          "cheats.err_unreadable", name=p.name)


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


def backup_original(path) -> Path:
    """Put a dated copy of *path* aside and return where it went."""
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
    shutil.copy2(p, dest)
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
