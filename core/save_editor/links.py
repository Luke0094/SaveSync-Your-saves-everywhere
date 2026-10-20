"""SaveSync — values a save stores more than once.

Games like to keep the same number in two places: the list the screen shows,
and a lookup built from it for speed. Edit the lookup and nothing changes,
because the game reads the list (and rebuilds the lookup from it); edit the
list and the lookup catches up. Which of the two is the real one is the game's
secret, and a save editor listing both side by side leaves the player to find
out the hard way.

This finds those pairs, so the editor can say "this value is connected with
that one". It reads nothing but the labels and values the editor already has,
and it decides nothing: it names candidates, the person picks.

What counts as a copy. A thing in a save usually carries a name of its own
inside it, under a key like ``id`` or ``name`` (``Item A``, ``k1``). A
*lookup* is a group of numbers each stored under such a name, ``counts.k1``.
A *list* is a run of records each holding one named thing and a number beside
it, ``items.3`` = ``[<Item A>, 20]``. The lookup is a copy of the list when
most of its names are the names of that list's records. Then each number in
the lookup and the number beside the same thing in the list are connected.

Why so strict, rather than "anything that mentions the same name": a loot
table is a list of ``[<Item A>, 35]`` too, and 35 is a chance, not a quantity.
Matching on the name alone links the two; asking that a whole lookup agree
with a whole list does not, because a loot table holds a handful of the items
a bag holds, not nearly all of them.

Precision matters more than reach: a wrong suggestion costs the person's
trust in the right ones. So:

- an object's own attributes (``value``, ``rarity``…) are never offered as its
  quantity — the search for the number beside a named object starts above it;
- a lookup needs most of its keys to be names, so a group of ordinary
  attributes is not mistaken for one;
- equal values only ORDER the answer. They must not filter it: the moment
  someone edits one copy the two stop being equal, and that is exactly when
  they need pointing at each other. The one exception is a plain record that
  keeps its number right beside its name, ``{"id": "k1", "qty": 3}``, where
  nothing else says which of its numbers is the quantity.
"""
import re
from dataclasses import dataclass

# Keys that say what a thing IS. Compared lower case, after the "#2" numbering
# the editor adds to repeated labels is taken off. Earlier reads better.
_IDENT_KEYS = ("name", "title", "label", "id", "key", "uid", "uuid", "code",
               "objectname", "identifier", "item_id", "itemid")
_IDENT_RANK = {k: i for i, k in enumerate(_IDENT_KEYS)}
# What an identifying string can look like: a name, not a sentence.
_MAX_IDENT_LEN = 48
# A record has a handful of names (id, name, title…). Many more under one
# parent means it is a collection, and a collection identifies nothing.
_MAX_RECORD_IDENTS = 8
# How far above a named object to look for the number kept beside it.
_CLIMB = 3
# More numbers than this under one parent is a collection, not a record.
_MAX_GROUP = 4
# Fewer names than this is not enough to say a lookup was built from a list.
_MIN_KEYS = 2
# The share of a lookup's keys that must be names of the list's records.
_AGREE = 0.6
_MAX_LINKS = 6
# Past this the index costs more memory than the answer is worth.
_MAX_FIELDS = 150_000
_REPEAT = re.compile(r"\s#\d+$")


def _parent(label: str) -> str:
    """*label* without its last step ('' when it has one step).

    A dot inside ``[...]`` (a name such as ``Mr. Jones``) is not a step."""
    i = label.rfind(".")
    while i > 0:
        before = label[:i]
        opened = before.rfind("[")
        if opened > before.rfind("]"):
            i = label.rfind(".", 0, opened)
            continue
        return before
    return ""


def _last(label: str) -> str:
    """The last step of *label*, without the "#2" the editor numbers repeats with."""
    p = _parent(label)
    tail = label[len(p) + 1:] if p else label
    if "#" in tail:
        tail = _REPEAT.sub("", tail)
    return tail


def _is_object(label: str) -> bool:
    """Whether the last step of *label* is a named object, ``Class[name]``."""
    return _last(label).endswith("]")


@dataclass(frozen=True)
class Link:
    field: object        # the SaveField it points at
    how: str             # "keyed": stored under the name | "beside": next to the thing
    same_value: bool     # equal to the value asked about, as the save was opened


class FieldLinks:
    """Finds, for a number, the other number that holds the same thing.

    Built once, when a save is opened, from its fields: the values it keeps
    for ordering are the file's own, whatever is edited afterwards. Everything
    is decided here; ``for_field`` is then a dictionary lookup, cheap enough to
    ask for every row on screen.

    *aliases* is ``(where an object is mentioned again, where it is listed)``
    for formats that write an object once and refer back to it (Ren'Py's
    pickle): without it an inventory slot holding an item first listed
    elsewhere has no name beside it to be recognised by.
    """

    def __init__(self, fields: list, aliases=()):
        self._fields = fields
        self.enabled = 0 < len(fields) <= _MAX_FIELDS
        self._pos = {}
        self._ids = []            # (label, value) of every identifying row, real or virtual
        self._ident_rows = {}     # identifying string -> [index into _ids]
        self._ident_under = {}    # ancestor label -> [index into _ids]
        self._seen_ids = set()
        self._by_parent = {}      # parent label -> [numeric field index]
        self._v0 = []
        self._links = {}          # field index -> {partner field index: how}
        self._who = {}            # field index -> [names]
        if not self.enabled:
            return
        self._v0 = [f.value for f in fields]
        for i, f in enumerate(fields):
            self._pos[f.path] = i
            if f.kind == "str":
                v = f.value
                if (isinstance(v, str) and v and len(v) <= _MAX_IDENT_LEN
                        and "\n" not in v
                        and _last(f.label).lower() in _IDENT_RANK):
                    self._add_ident(f.label, v)
            elif f.kind in ("int", "float"):
                self._by_parent.setdefault(_parent(f.label), []).append(i)
        try:
            self._add_aliases(aliases)
            self._build()
        except Exception:
            # A hint must never be the thing that breaks opening a save.
            self._links = {}
            self._who = {}

    # ── building ─────────────────────────────────────────────────────────────

    def _add_ident(self, label: str, value: str) -> None:
        key = (label, value)
        if key in self._seen_ids:
            return
        self._seen_ids.add(key)
        k = len(self._ids)
        self._ids.append(key)
        self._ident_rows.setdefault(value, []).append(k)
        anc = label
        for _ in range(_CLIMB + 2):
            anc = _parent(anc)
            if not anc:
                break
            self._ident_under.setdefault(anc, []).append(k)

    def _add_aliases(self, aliases) -> None:
        """An object mentioned again carries the names it was listed with."""
        for again, listed in aliases:
            under = self._ident_under.get(listed)
            if not under or len(under) > _MAX_RECORD_IDENTS * 2:
                continue
            prefix = listed + "."
            for k in list(under):
                label, value = self._ids[k]
                if label.startswith(prefix):
                    self._add_ident(again + label[len(listed):], value)

    def _record_of(self, k: int):
        """Where the number beside the thing named by id row *k* is kept:
        ``(record label, [numeric field index], is a plain record)`` or None."""
        obj = _parent(self._ids[k][0])
        if not _is_object(obj):
            # {"id": "k1", "qty": 3}: its numbers sit right beside its name.
            group = self._by_parent.get(obj)
            return (obj, group, True) if group and "." in obj else None
        anc = obj
        for _ in range(_CLIMB):
            anc = _parent(anc)
            if not anc or "." not in anc:        # reached the namespace itself
                return None
            group = self._by_parent.get(anc)
            if group:
                return (anc, group, False) if len(group) <= _MAX_GROUP else None
        return None

    def _names(self, k: int) -> list:
        """What id row *k*'s thing is called, best name first."""
        around = self._ident_under.get(_parent(self._ids[k][0]), ())
        seen = {self._ids[k][1]}
        if len(around) <= _MAX_RECORD_IDENTS:
            seen.update(self._ids[a][1] for a in around)

        def rank(s: str) -> int:
            return min((_IDENT_RANK.get(_last(self._ids[a][0]).lower(), 99)
                        for a in self._ident_rows.get(s, ())), default=99)

        return sorted(seen, key=lambda s: (rank(s), s))

    def _build(self) -> None:
        fields = self._fields
        for lookup, rows in self._by_parent.items():
            # The namespace itself (every variable of the game) is never a
            # lookup: its names are variables, which _AGREE below rules out.
            if len(rows) < _MIN_KEYS:
                continue
            keys = {}                          # name -> numeric field index
            for i in rows:
                k = _last(fields[i].label)
                if k in self._ident_rows and not k.isdigit():
                    keys[k] = i
            if len(keys) < _MIN_KEYS or len(keys) < _AGREE * len(rows):
                continue
            # Which list's records carry these names?
            by_list = {}                       # list label -> {name: [records]}
            for name in keys:
                for k in self._ident_rows[name]:
                    rec = self._record_of(k)
                    if rec is None or rec[0] == lookup:
                        continue
                    by_list.setdefault(_parent(rec[0]), {}).setdefault(
                        name, []).append((k, rec))
            for per_name in by_list.values():
                if len(per_name) < _MIN_KEYS or len(per_name) < _AGREE * len(keys):
                    continue
                for name, recs in per_name.items():
                    e = keys[name]
                    for k, (_label, group, plain) in recs:
                        for j in group:
                            if j == e:
                                continue
                            if plain and self._v0[j] != self._v0[e]:
                                continue
                            self._links.setdefault(e, {}).setdefault(j, "beside")
                            self._links.setdefault(j, {}).setdefault(e, "keyed")
                            self._who.setdefault(e, self._names(k))
                            self._who.setdefault(j, self._names(k))

    # ── asking ───────────────────────────────────────────────────────────────

    def for_field(self, f) -> list:
        """Links for *f*, best first; [] when it has none or is not a number."""
        if not self.enabled or f.kind not in ("int", "float"):
            return []
        i = self._pos.get(f.path)
        partners = self._links.get(i)
        if not partners:
            return []
        mine = self._v0[i]
        out = [Link(self._fields[j], how, self._v0[j] == mine)
               for j, how in partners.items()]
        # Equal values first (the likeliest mirror), then the shortest label:
        # a stable, explainable order.
        out.sort(key=lambda l: (not l.same_value, len(l.field.label)))
        return out[:_MAX_LINKS]

    def identity(self, f) -> str:
        """What *f* is about, for a heading: ``Item A · k1``."""
        i = self._pos.get(f.path)
        return " · ".join(self._who.get(i, ())[:2]) if i is not None else ""
