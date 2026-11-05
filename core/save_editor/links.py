"""SaveSync — values a save stores more than once.

Games like to keep the same number in two places: the list the screen shows,
and a lookup built from it for speed. Edit the lookup and nothing changes,
because the game reads the list (and rebuilds the lookup from it); edit the
list and the lookup catches up. Which of the two is the real one is the game's
secret, and a save editor listing both side by side leaves the player to find
out the hard way.

This finds those copies, so the editor can say "this value is also kept
there". It reads nothing but the labels and values the editor already has, and
it decides nothing: it names candidates, the person picks.

A thing can be kept in more than two places (the lookup, a saved copy of the
list, an older snapshot): every place that holds it is a member of ONE group,
and each member points at all the others - so the answer is the same from
whichever copy the person clicks, and none is left out.

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

The record that holds a thing's name (``name``, ``id``…) is tied to the numbers that hold its quantity, and
back: a bag of ids is unreadable until something says what each id is.

Precision matters more than reach: a wrong suggestion costs the person's
trust in the right ones. So there are two tiers, and they are shown apart:

- STRONG: the list covers most of the lookup (the rule above). Values are not
  looked at, only used to order the answer.
- WEAK: a list that covers only PART of the lookup (a snapshot, a stash). Few
  names are not enough to tell a copy from a loot table, so here the numbers
  have to speak: at least ``_WEAK_NAMES`` shared names, and most of them holding
  the same number as the save was opened. A loot table's numbers are chances
  and prices; they do not match the bag.

And, in either tier:

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
# A list covering only part of a lookup: at least this many shared names...
_WEAK_NAMES = 5
# ...and at least this share of them holding the number the lookup holds.
_WEAK_AGREE = 0.6
_MAX_LINKS = 8
# What a plain record calls its quantity: with one of these beside a name, the number is the quantity
# whatever it is equal to now.
_QTY_NAMES = frozenset({"qty", "quantity", "count", "amount", "num", "number", "stack", "owned", "have", "quant", "n"})
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
    tier: str = "strong"  # "strong" | "weak" (a partial copy, recognised by its numbers)
    who: str = ""        # what the thing is called, for a tooltip: ``Item A · k1``


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
        self._links = {}          # field index -> {partner field index: (how, tier)}
        self._who = {}            # field index -> [names]
        self._ident_idx = {}      # label of a real identifying row -> its field index
        self._things = []         # (every name a thing goes by, {member field index: (how, tier, id row)})
        self._name_links = {}     # identifying row's field index -> its thing's members (shared dict)
        self._thing_of = {}       # member field index -> index into _things
        self._thing_rows = {}     # thing index -> [identifying rows' field indexes], best first
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
                    self._ident_idx[f.label] = i
            elif f.kind in ("int", "float"):
                self._by_parent.setdefault(_parent(f.label), []).append(i)
        try:
            self._add_aliases(aliases)
            self._build()
        except Exception:
            # A hint must never be the thing that breaks opening a save.
            self._links = {}
            self._who = {}
            self._name_links = {}
            self._thing_rows = {}

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
            # name -> every (place, how, tier) the lookup's entry is kept in
            places = {}
            for per_name in by_list.values():
                tier = self._tier(keys, per_name)
                if tier is None:
                    continue
                for name, recs in per_name.items():
                    e = keys[name]
                    for k, (_label, group, plain) in recs:
                        for j in group:
                            if j == e:
                                continue
                            if plain and self._v0[j] != self._v0[e] \
                                    and _last(fields[j].label).lower() not in _QTY_NAMES:
                                continue
                            places.setdefault(name, {}).setdefault(j, ("beside", tier, k))
            for name, copies in places.items():
                e = keys[name]
                # member field index -> (how it is kept, tier, id row that names the thing)
                members = {e: ("keyed", "strong", None)}
                members.update({j: (how, tier, k) for j, (how, tier, k) in copies.items()})
                # Call the thing by the richest name any copy gives it ("Item A", not just the id k1).
                named_by = max((m[2] for m in members.values() if m[2] is not None),
                               key=lambda k: len(self._names(k)), default=None)
                if named_by is not None:
                    t = len(self._things)
                    self._things.append((frozenset(self._names(named_by)), members))
                    for m in members:
                        self._thing_of.setdefault(m, t)
                # Every member points at every other: from whichever copy the person clicks, the
                # answer is the same set of places.
                for i in members:
                    for j, (how_j, tier_j, _k) in members.items():
                        if i == j:
                            continue
                        # A weak member makes the link weak, seen from either side.
                        tier = "weak" if "weak" in (members[i][1], tier_j) else "strong"
                        old = self._links.setdefault(i, {}).get(j)
                        if old is None or (old[1] == "weak" and tier == "strong"):
                            self._links[i][j] = (how_j, tier)
                    if named_by is not None:
                        self._who.setdefault(i, self._names(named_by))
        self._index_names()

    def _index_names(self) -> None:
        """Tie the rows that SAY what a thing is (``name``, ``id``…) to the numbers that hold it.

        The numbers point at each other, but a bag of ``k1 = 166`` tells the person nothing: the name
        of the thing sits in its own record, far from the quantity. A row whose record goes by a subset of
        the names of a thing found above is that thing's name row: it points at the places its quantity
        is kept in, and they at it."""
        for t, (names, members) in enumerate(self._things):
            rows = {}                                 # record -> (names it gives, rank, label length, field index)
            for v in names:
                for kk in self._ident_rows.get(v, ()):
                    label = self._ids[kk][0]
                    idx = self._ident_idx.get(label)
                    given = self._names(kk)
                    if idx is None or not set(given) <= names:
                        continue
                    self._name_links.setdefault(idx, members)
                    rec = _parent(label)
                    cand = (-len(given), _IDENT_RANK.get(_last(label).lower(), 99), len(label), idx)
                    if rec not in rows or cand < rows[rec]:
                        rows[rec] = cand
            # Back from a quantity only the records that say the most about the thing (a record that holds just
            # its id adds nothing beside one that holds its name too).
            best = min((c[0] for c in rows.values()), default=0)
            self._thing_rows[t] = [c[3] for c in sorted(rows.values()) if c[0] == best]

    def _tier(self, keys: dict, per_name: dict):
        """"strong", "weak" or None: how well this list's records match a lookup's keys."""
        n = len(per_name)
        if n >= _MIN_KEYS and n >= _AGREE * len(keys):
            return "strong"
        if n < _WEAK_NAMES:
            return None
        agree = 0
        for name, recs in per_name.items():
            mine = self._v0[keys[name]]
            if any(self._v0[j] == mine for _k, (_l, group, _p) in recs for j in group):
                agree += 1
        return "weak" if agree >= _WEAK_AGREE * n else None

    # ── asking ───────────────────────────────────────────────────────────────

    def for_field(self, f) -> list:
        """Links for *f*, best first; [] when it has none or is not a number.

        Every other place that keeps the same thing, strong ones before weak ones, then equal
        values (the likeliest mirror), then the shortest label: a stable, explainable order."""
        if not self.enabled:
            return []
        if f.kind == "str":
            return self._quantities_of_name(f)
        if f.kind not in ("int", "float"):
            return []
        i = self._pos.get(f.path)
        partners = self._links.get(i)
        if not partners:
            return []
        mine = self._v0[i]
        out = []
        for j, (how, tier) in partners.items():
            who = " · ".join(self._who.get(j, ())[:2])
            out.append(Link(self._fields[j], how, self._v0[j] == mine, tier, who))
        out.sort(key=lambda l: (l.tier != "strong", not l.same_value, len(l.field.label)))
        return out[:_MAX_LINKS]

    def _quantities_of_name(self, f) -> list:
        """For a row that names a thing (``name``, ``id``…): the numbers that hold how many of it there are."""
        members = self._name_links.get(self._pos.get(f.path))
        if not members:
            return []
        out = []
        for j, (how, tier, _k) in members.items():
            who = " · ".join(self._who.get(j, ())[:2])
            out.append(Link(self._fields[j], how, False, tier, who))
        out.sort(key=lambda l: (l.tier != "strong", len(l.field.label)))
        return out[:_MAX_LINKS]

    def names_of(self, f, limit: int = 1) -> list:
        """For a number: the row that says what it is - the record that says the most about the thing, ``name`` before ``id``.

        A bag of ``k1 = 166`` is unreadable on its own; this is the way to the row that says what it is."""
        if not self.enabled or f.kind not in ("int", "float"):
            return []
        i = self._pos.get(f.path)
        t = self._thing_of.get(i)
        if t is None:
            return []
        who = " · ".join(self._who.get(i, ())[:2])
        return [Link(self._fields[j], "named", False, "strong", who) for j in self._thing_rows.get(t, ())[:limit]]

    def who_is(self, f) -> str:
        """What *f*'s thing is called (``Item A · k1``), '' when it has no name to give."""
        i = self._pos.get(f.path)
        if f.kind == "str":
            members = self._name_links.get(i)
            i = next(iter(members), None) if members else None
        return " · ".join(self._who.get(i, ())[:2]) if i is not None else ""


def menu_rows(partners, pending=None, max_label: int = 70) -> list:
    """What the links menu shows, as rows: ``{"label", "value", "tip", "tier", "field"}``.

    The menu never names the value that was clicked: the person is looking at its row. It lists the OTHER
    places, each with where it is and what it holds now (*pending*: edits made and not yet saved, by path),
    and the thing's name goes into the tooltip, so a row of ids can be read as what it is."""
    rows = []
    for link in partners:
        pf = link.field
        label = pf.label if len(pf.label) <= max_label else "\u2026" + pf.label[-(max_label - 1):]
        shown = (pending or {}).get(pf.path, pf.value)
        tip = pf.label + (f"\n{link.who}" if link.who else "")
        rows.append({"label": label, "value": shown, "tip": tip, "tier": link.tier, "field": pf})
    return rows
