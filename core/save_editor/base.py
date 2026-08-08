"""Shared types and helpers for save-format adapters."""
from dataclasses import dataclass


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
