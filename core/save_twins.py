"""SaveSync - a game's saves written twice, in two folders.

Ren'Py writes every save to two places at once: beside the game (``<install>/game/saves``) and in its own data
folder (``AppData/Roaming/RenPy/<game>`` on Windows, ``~/.renpy/<game>`` on Linux, ``~/Library/RenPy/<game>`` on
macOS). The scans find both, and a panel that proposes both as two equal choices asks the person to settle something
that is, almost always, one set of saves in two places.

This module tells those two copies apart from two folders that merely look alike, and says which is the one to
propose and which is its duplicate (the first found is proposed):

- **By place.** One path is ``<install>/game/saves`` (or ``save``) of a Ren'Py install (a ``renpy`` folder beside
  ``game``); the other is a folder directly under Ren'Py's data folder. Nothing else is ever a twin.
- **By content.** Ren'Py names its slots alike in every game (``1-1-LT1.save``…), so a shared file name proves
  nothing. Two folders are copies of each other when a file of the same name holds the very same bytes - a real
  save, not a stub. Folders that have drifted apart (a different or older set of slots) are two things, and are
  both proposed as they always were.

The other copy is shown too, under a "duplicate" heading and unticked: it stays on the game's list (so a later
scan does not offer it again as something new), and ticking it later moves the backup there, or backs up both.

Pure functions over paths and files; nothing is written.
"""
import os
from pathlib import Path

# A file this small can be identical in two different games (an untouched `persistent`).
_MIN_PROOF_BYTES = 4096
# How many same-named, same-sized files are compared byte for byte before giving up.
_MAX_PROOFS = 3
_MAX_PROOF_SIZE = 16 * 1024 * 1024
_CHUNK = 1 << 16


def renpy_copy_kind(path) -> str:
    """``"install"`` for ``<Ren'Py install>/game/saves``, ``"data"`` for a folder directly under Ren'Py's data
    folder, ``""`` for anything else. By place; the disk is only asked whether a ``renpy`` folder sits beside
    ``game``."""
    try:
        s = str(path)
        if not s or s.lower().startswith("registry:"):
            return ""
        parts = list(Path(os.path.normpath(s)).parts)
    except (OSError, ValueError, TypeError):
        return ""
    low = [p.lower() for p in parts]
    if len(low) >= 3 and low[-1] in ("saves", "save") and low[-2] == "game":
        try:
            if (Path(*parts[:-2]) / "renpy").is_dir():
                return "install"
        except (OSError, ValueError, TypeError):
            return ""
        return ""
    if len(low) >= 2 and low[-2] == ".renpy":
        return "data"
    if len(low) >= 4 and low[-4:-1] == ["appdata", "roaming", "renpy"]:
        return "data"
    if len(low) >= 3 and low[-3:-1] == ["library", "renpy"]:
        return "data"
    try:
        from core.skip_dirs import renpy_data_roots
        here = [p.lower() for p in Path(os.path.normpath(s)).parent.parts]
        for root in renpy_data_roots():
            if here == [p.lower() for p in root.parts]:
                return "data"
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _flat_files(folder) -> dict:
    """{name: size} of the files directly in *folder* (Ren'Py keeps its slots flat)."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        out[e.name] = e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _same_bytes(a: str, b: str) -> bool:
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(_CHUNK), fb.read(_CHUNK)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def are_twin_copies(a, b) -> bool:
    """Whether folders *a* and *b* are one game's saves written in both of Ren'Py's places: one is the install's
    ``game/saves``, the other sits in Ren'Py's data folder, and a real save of the same name holds the same bytes
    in both."""
    ka, kb = renpy_copy_kind(a), renpy_copy_kind(b)
    if {ka, kb} != {"install", "data"}:
        return False
    fa, fb = _flat_files(a), _flat_files(b)
    proofs = sorted((size, name) for name, size in fa.items()
                    if fb.get(name) == size and _MIN_PROOF_BYTES <= size <= _MAX_PROOF_SIZE)
    return any(_same_bytes(os.path.join(str(a), name), os.path.join(str(b), name))
               for _size, name in proofs[:_MAX_PROOFS])


def _key(p) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(p)))
    except (ValueError, TypeError):
        return str(p)


def split_twin_copies(paths, listed=()) -> tuple:
    """``(proposed, duplicates)``: *paths* split into what is proposed and the second copy of anything saved twice.

    *listed* is what is already on the list (the game's save paths, the rows on screen). A path that is already
    listed is never a duplicate - it is the person's. A new path that is a copy of a listed one is a duplicate (it is
    the same saves, already covered); of new copies of one another the first found is proposed and the rest are
    duplicates. Order is preserved; a path with no copy is never a duplicate."""
    paths = [p for p in (paths or []) if p]
    listed = [p for p in (listed or []) if p]
    kinds = {_key(p): renpy_copy_kind(p) for p in paths + listed}
    if not any(kinds.get(_key(p)) for p in paths):
        return list(paths), []
    listed_keys = {_key(p) for p in listed}
    universe = {}
    for p in paths + listed:
        universe.setdefault(_key(p), p)
    installs = [p for k, p in universe.items() if kinds.get(k) == "install"]
    datas = [p for k, p in universe.items() if kinds.get(k) == "data"]
    # Folders that are copies of one another form a group (union-find over the pairs that are).
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked = False
    for i in installs[:4]:
        for d in datas[:4]:
            if are_twin_copies(i, d):
                parent[find(_key(i))] = find(_key(d))
                linked = True
    if not linked:
        return list(paths), []
    covered = {find(k) for k in listed_keys if k in parent}      # groups that already have a path on the list
    seen = set()
    dup_keys = set()
    for p in paths:
        k = _key(p)
        if k not in parent or k in listed_keys:
            continue
        g = find(k)
        if g in covered or g in seen:
            dup_keys.add(k)                            # the same saves are already on the list, or proposed above
        else:
            seen.add(g)                                # the first found
    proposed = [p for p in paths if _key(p) not in dup_keys]
    duplicates = [p for p in paths if _key(p) in dup_keys]
    return proposed, duplicates
