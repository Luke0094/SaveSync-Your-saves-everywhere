"""Ren'Py save files.

A ``.save`` is a ZIP holding a ``log`` entry — a pickle of the whole game
state — plus a screenshot, a small JSON blob, and (since Ren'Py 8) a
``signatures`` entry.

Two things make this format its own module rather than a few lines:

**The pickle is never executed.** Unpickling a Ren'Py save would import and
construct the game's own classes, which is the same as running code from a
file — the thing Ren'Py's own security page warns about. Instead the opcode
stream is walked with ``pickletools`` (which only reads), values are found by
their names, and an edit splices new bytes over the old ones. Nothing in the
save is ever instantiated.

**Ren'Py 8 refuses a save whose signature does not match.** The signature is
ECDSA over the ``log`` bytes, made with a key that lives on the player's own
machine. So an edited save is re-signed with that same key, and if the key
cannot be found the save is NOT written: handing back a file the game will
reject is worse than saying no.
"""
import base64
import io
import logging
import os
import pickletools
import re
import struct
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


class RenpyError(Exception):
    pass


# The mark the pickle stack uses to remember where a run of items began.
_MARK = object()
# Keys whose value names the record holding them, so a repeated field can be
# told apart: the reward of Quest[intro_wolf] rather than the fourth "money".
_IDENTIFIERS = ("objectname", "name", "id", "label", "title")
# Ren'Py's undo buffer. It is a copy of past states, not the state itself:
# editing it changes nothing a player would see, and it is nearly half of
# everything in the file.
_ROLLBACK = "RollbackLog"
# Ren'Py's own container types. Naming them in a path adds a word to every
# step and tells nobody anything: "kermit_perks.RevertableDict.description"
# is the same address as "kermit_perks.description", only longer. Classes
# the GAME defined are kept, because those do identify what a value belongs
# to — Quest[intro_wolf] is worth saying.
_PLUMBING = frozenset({
    "RevertableDict", "RevertableList", "RevertableSet", "RevertableObject",
    "dict", "list", "set", "tuple", "defaultdict", "OrderedDict",
})


class _Node:
    """A stand-in for one object in the stream — never the object itself."""
    __slots__ = ("kind", "cls", "items", "value", "span")

    def __init__(self, kind, value=None, span=None, cls=""):
        self.kind = kind          # dict | list | obj | class | scalar | other
        self.cls = cls
        self.items = []
        self.value = value
        self.span = span


def _describe(node, trail, out, seen=None):
    """Give every scalar under *node* the path that identifies it."""
    if seen is None:
        seen = set()
    if not isinstance(node, _Node) or id(node) in seen:
        return
    seen.add(id(node))
    if node.kind == "scalar":
        key = trail[-1] if trail else ""
        if node.cls and key and not str(key).startswith("_"):
            # The first step is the save's position in the outermost list,
            # which is always the same and says nothing.
            parts = [str(t) for t in trail]
            if parts and parts[0].isdigit():
                parts = parts[1:]
            # A name opening with an underscore is Ren'Py talking to itself —
            # the dialogue history, the set of defaults, an object's version
            # stamp. Editing those does nothing a player would see. The test
            # allows for a step carrying its class in front of the key, as in
            # "store._history_list", where the underscore is not first.
            if any(p.startswith("_") or "._" in p for p in parts):
                return
            out.append({"name": ".".join(parts), "kind": node.cls,
                        "value": node.value,
                        "start": node.span[0], "end": node.span[1]})
        return
    # BUILD folds a state dict into an object, so one node can hold both
    # (key, value) pairs and bare items. Take them as they come.
    pairs = [it for it in node.items if isinstance(it, tuple)]
    loose = [it for it in node.items if not isinstance(it, tuple)]
    named = ""
    for key, child in pairs:
        if (key in _IDENTIFIERS and isinstance(child, _Node)
                and child.kind == "scalar" and child.cls == "str"):
            named = str(child.value)
            break
    cls = "" if node.cls in _PLUMBING else (node.cls or "")
    here = f"{cls}[{named}]" if named else cls
    if node.cls.startswith(_ROLLBACK):
        return
    for key, child in pairs:
        step = f"{here}.{key}" if here else str(key)
        _describe(child, trail + (step,), out, seen)
    for i, child in enumerate(loose):
        step = f"{here}[{i}]" if here else str(i)
        _describe(child, trail + (step,), out, seen)


# A value is offered when a name is followed straight away by a scalar — the
# shape a dict takes in a pickle stream. Names are filtered to things that
# look like variables, because the stream also carries class and module names.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Ren'Py's own bookkeeping. Editing these breaks the save rather than the game.
_INTERNAL_PREFIXES = ("_", "renpy.", "store.")
_STRING_OPS = ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8")
_INT_OPS = ("BININT", "BININT1", "BININT2", "LONG1")
_BOOL_OPS = ("NEWTRUE", "NEWFALSE")
# The memo opcodes that sit between a key and its value.
_MEMO_OPS = ("BINPUT", "LONG_BINPUT", "PUT", "MEMOIZE", "FRAME")


def _encode_int(value: int) -> bytes:
    """The shortest pickle opcode that carries *value*, as Python's own
    pickler would choose it."""
    if 0 <= value <= 0xFF:
        return b"K" + bytes([value])                    # BININT1
    if 0 <= value <= 0xFFFF:
        return b"M" + struct.pack("<H", value)          # BININT2
    if -0x80000000 <= value <= 0x7FFFFFFF:
        return b"J" + struct.pack("<i", value)          # BININT
    raw = value.to_bytes((value.bit_length() // 8) + 1, "little", signed=True)
    return b"\x8a" + bytes([len(raw)]) + raw            # LONG1


def _encode_float(value: float) -> bytes:
    return b"G" + struct.pack(">d", float(value))       # BINFLOAT


def _encode_str(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) < 256:
        return b"\x8c" + bytes([len(raw)]) + raw        # SHORT_BINUNICODE
    return b"X" + struct.pack("<I", len(raw)) + raw     # BINUNICODE


class RenpySave:
    """One Ren'Py save, opened for reading and editing."""

    def __init__(self):
        self._entries = []       # (name, bytes) in the order the zip had them
        self._log = b""
        self._values = []        # dicts: name, kind, value, start, end
        self._signed = False

    # ── reading ──────────────────────────────────────────────────────────────

    def load(self, data: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if "log" not in names:
                    raise RenpyError("no log entry: not a Ren'Py save")
                self._entries = [(n, zf.read(n)) for n in names]
        except zipfile.BadZipFile as e:
            raise RenpyError("not a zip archive") from e
        self._signed = any(n == "signatures" for n, _ in self._entries)
        self._log = dict(self._entries)["log"]
        self._values = self._scan(self._log)
        if not self._values:
            raise RenpyError("no editable values found in the save")

    @staticmethod
    def _scan(log: bytes) -> list:
        """Every scalar in the pickle, with the path that identifies it.

        The stream is walked by simulating the pickle STACK — pickletools says
        what each opcode takes and leaves — so the container every value sits
        in is known rather than guessed. Nothing is unpickled: the stack holds
        markers standing for objects, never objects, so no class is imported
        and no ``__reduce__`` ever runs. That is the property this whole module
        exists to keep.

        Reading the stream flat, as this used to, misses almost all of it.
        Ren'Py memoises a key name the first time it writes it and refers back
        to it afterwards, so from the second object of a class onward the keys
        are back-references and a flat pass cannot see the pair at all: 1,484
        values found in a save that holds 34,696. Worse, the ones it did find
        arrived as bare names, so nine quest rewards all called "money" were
        indistinguishable — and a value whose name is not unique cannot be
        held, because holding resolves by name and skips anything ambiguous.
        """
        try:
            ops = list(pickletools.genops(log))
        except Exception as e:
            raise RenpyError(f"could not read the save's pickle: {e}") from e
        ends = [ops[i + 1][2] for i in range(len(ops) - 1)] + [len(log)]

        stack, memo, scalars = [], {}, []

        def take_to_mark():
            out = []
            while stack:
                top = stack.pop()
                if top is _MARK:
                    return out[::-1]
                out.append(top)
            return out[::-1]

        for i, (op, arg, pos) in enumerate(ops):
            name = op.name
            if name == "MARK":
                stack.append(_MARK)
            elif name in ("EMPTY_DICT", "EMPTY_LIST", "EMPTY_SET"):
                stack.append(_Node("dict" if name == "EMPTY_DICT" else "list"))
            elif name in ("DICT", "LIST", "FROZENSET", "TUPLE"):
                items = take_to_mark()
                node = _Node("dict" if name == "DICT" else "list")
                node.items = (list(zip(items[::2], items[1::2]))
                              if name == "DICT" else items)
                stack.append(node)
            elif name in ("TUPLE1", "TUPLE2", "TUPLE3"):
                n = int(name[-1])
                node = _Node("list")
                node.items = [stack.pop() for _ in range(min(n, len(stack)))][::-1]
                stack.append(node)
            elif name in ("SETITEMS", "SETITEM"):
                if name == "SETITEMS":
                    items = take_to_mark()
                else:
                    v = stack.pop() if stack else None
                    k = stack.pop() if stack else None
                    items = [k, v]
                target = stack[-1] if stack else None
                if isinstance(target, _Node):
                    for k, v in zip(items[::2], items[1::2]):
                        target.items.append(
                            (k.value if isinstance(k, _Node) else None, v))
            elif name in ("APPENDS", "APPEND"):
                items = take_to_mark() if name == "APPENDS" else (
                    [stack.pop()] if stack else [])
                target = stack[-1] if stack else None
                if isinstance(target, _Node):
                    target.items.extend(items)
            elif name == "BUILD":
                state = stack.pop() if stack else None
                target = stack[-1] if stack else None
                if isinstance(target, _Node) and isinstance(state, _Node):
                    target.items.extend(state.items)
            elif name in ("GLOBAL", "STACK_GLOBAL"):
                if name == "STACK_GLOBAL":
                    cls = stack.pop() if stack else None
                    if stack:
                        stack.pop()
                    label = getattr(cls, "value", "") or ""
                else:
                    parts = str(arg).split()
                    label = parts[-1] if parts else ""
                node = _Node("class")
                node.cls = str(label)
                stack.append(node)
            elif name in ("NEWOBJ", "NEWOBJ_EX", "REDUCE", "OBJ", "INST"):
                if stack:
                    stack.pop()
                cls = stack.pop() if stack else None
                node = _Node("obj")
                node.cls = getattr(cls, "cls", "") or ""
                stack.append(node)
            elif name in ("BINPUT", "LONG_BINPUT", "PUT"):
                if stack and stack[-1] is not _MARK:
                    memo[arg] = stack[-1]
            elif name == "MEMOIZE":
                if stack and stack[-1] is not _MARK:
                    memo[len(memo)] = stack[-1]
            elif name in ("BINGET", "LONG_BINGET", "GET"):
                stack.append(memo.get(arg, _Node("other")))
            elif name in _STRING_OPS or name in ("BINSTRING", "SHORT_BINSTRING"):
                node = _Node("scalar", arg, (pos, ends[i]), "str")
                stack.append(node)
                scalars.append(node)
            elif name in _INT_OPS or name in ("LONG", "INT"):
                node = _Node("scalar", arg, (pos, ends[i]), "int")
                stack.append(node)
                scalars.append(node)
            elif name in ("BINFLOAT", "FLOAT"):
                node = _Node("scalar", arg, (pos, ends[i]), "float")
                stack.append(node)
                scalars.append(node)
            elif name in _BOOL_OPS:
                node = _Node("scalar", name == "NEWTRUE", (pos, ends[i]), "bool")
                stack.append(node)
                scalars.append(node)
            elif name in ("PROTO", "FRAME", "STOP"):
                continue
            else:
                # Anything not modelled keeps the simulation in step by its own
                # declared stack effect, rather than drifting silently.
                if any(s is pickletools.markobject for s in op.stack_before):
                    take_to_mark()
                else:
                    for _ in range(min(len(op.stack_before), len(stack))):
                        stack.pop()
                for _ in op.stack_after:
                    stack.append(_Node("other"))

        out = []
        for root in stack:
            _describe(root, (), out)
        return out


    # ── the values ───────────────────────────────────────────────────────────

    def values(self) -> list:
        """(index, name, kind, value) for everything that can be edited."""
        return [(i, v["name"], v["kind"], v["value"])
                for i, v in enumerate(self._values)]

    def set_value(self, index: int, value) -> None:
        v = self._values[index]
        if v["kind"] == "int":
            v["new"] = _encode_int(int(value))
        elif v["kind"] == "float":
            v["new"] = _encode_float(float(value))
        elif v["kind"] == "bool":
            v["new"] = b"\x88" if value else b"\x89"    # NEWTRUE / NEWFALSE
        else:
            v["new"] = _encode_str(str(value))
        v["value"] = value

    def _rebuilt_log(self) -> bytes:
        """The pickle with the edits spliced in.

        Applied back to front so that replacing a value never moves the
        offsets of the ones not yet done. Pickle refers to earlier objects by
        memo INDEX, never by byte offset, so a replacement of a different
        length is safe.
        """
        edits = [v for v in self._values if "new" in v]
        if not edits:
            return self._log
        out = self._log
        for v in sorted(edits, key=lambda x: x["start"], reverse=True):
            out = out[:v["start"]] + v["new"] + out[v["end"]:]
        return out

    def is_signed(self) -> bool:
        return self._signed

    # ── writing ──────────────────────────────────────────────────────────────

    def dump(self) -> bytes:
        log = self._rebuilt_log()
        signatures = None
        if self._signed and log != self._log:
            signatures = sign_log(log)
            if signatures is None:
                raise RenpyError(
                    "this save is signed, and the signing key for this "
                    "machine could not be found — Ren'Py would refuse to "
                    "load an edited save without a matching signature")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in self._entries:
                if name == "log":
                    blob = log
                elif name == "signatures" and signatures is not None:
                    blob = signatures.encode("utf-8")
                zf.writestr(name, blob)
        return buf.getvalue()


# ── Signing ──────────────────────────────────────────────────────────────────

def _token_dirs() -> list:
    """Where Ren'Py keeps the signing key on this machine."""
    out = []
    appdata = os.getenv("APPDATA", "")
    if appdata:
        out.append(Path(appdata) / "RenPy" / "tokens")
    home = Path.home()
    out.append(home / ".renpy" / "tokens")
    out.append(home / "Library" / "RenPy" / "tokens")
    return out


def find_signing_keys() -> list:
    """Every private signing key Ren'Py has on this machine, DER-encoded.

    These are the player's OWN keys, used by their own copy of Ren'Py to sign
    their own saves. Signing an edited save with them is what lets their game
    load it; nothing here reads or writes anyone else's.
    """
    keys = []
    for d in _token_dirs():
        f = d / "security_keys.txt"
        try:
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "signing-key":
                    try:
                        keys.append(base64.b64decode(parts[1]))
                    except Exception:
                        continue
        except OSError:
            continue
    return keys


def sign_log(log: bytes) -> "str | None":
    """The ``signatures`` entry for *log*, or None when no key is available.

    ECDSA over P-256 with SHA-1, and the signature written as the raw 64-byte
    r||s pair rather than DER — that is what Ren'Py's own signer produces, and
    a signature in any other shape would simply fail its check.
    """
    keys = find_signing_keys()
    if not keys:
        return None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature)
    except ImportError:
        logger.info("cryptography is not available; cannot sign a Ren'Py save")
        return None

    lines = []
    for der in keys:
        try:
            private = serialization.load_der_private_key(der, password=None)
            der_sig = private.sign(log, ec.ECDSA(hashes.SHA1()))
            r, s = decode_dss_signature(der_sig)
            raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            public = private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo)
            lines.append("signature " + base64.b64encode(public).decode("ascii")
                         + " " + base64.b64encode(raw).decode("ascii"))
        except Exception as e:
            logger.debug(f"Could not sign with one of the keys: {e}")
            continue
    if not lines:
        return None
    return "\n".join(lines) + "\n"


def loads(data: bytes) -> RenpySave:
    save = RenpySave()
    save.load(data)
    return save
