"""RecipeFormat — the last-resort, explicitly opt-in unwrap search.

A save nothing in the registry recognises, opened by trying a small,
bounded battery of generic unwrap recipes (see
``core.save_editor.crypt.recipes``) and handing whatever a recipe exposes
to the registry's own readers — the same relationship
``core.engines.wolf_lz4`` already has with the standard Wolf variable
database parser, generalised so "hand off to" means "try every registered
reader" instead of one hardcoded one.

Reached only through ``open_save``'s explicit ``try_recipes`` escalation
(see ``save_editor.py``) — **never** registered in ``registry.SPECS``: it
consumes ``registry.sniffed()``/``fallback_readers()`` internally to find
what a recipe exposes, so it cannot also be a member of what it consumes,
and ``all_readers(include_expensive=True)`` is exactly what "Check every
format (slower)" already runs — putting this there would make that
existing, already-documented-cost button silently start running the
recipe battery too.

Only ever succeeds as a fully writable result: the search only proves the
*unlock* direction (a candidate reader loads the unwrapped payload and
proves its OWN round trip); ``verify_value_round_trip`` below explicitly
proves *lock* as well — recompress, re-wrap, reread through the same
recipe — since that is the direction ``dump()``/saving actually depends
on and the search loop itself never exercises it.
"""
import hashlib
from pathlib import Path

from .base import SaveEditorError, _Format
from .crypt import recipes

# A game's obfuscation shows up in its header and is constant across save
# slots — hashing only the first 64 bytes is what lets every later save from
# the same slot hit this cache, where hashing the whole file would miss that
# reuse entirely (each slot's actual VALUES differ). A cache hit is always
# re-verified through the exact same gate a fresh search result is — see
# ``RecipeFormat.load`` — never trusted blindly, the same way
# ``crypt.es3.find_password`` treats a remembered password.
_RESULT_CACHE: dict = {}
_RESULT_CACHE_KEEP = 64


def max_recipe_bytes() -> int:
    """A save needing more than this is a small blob, not an archive — see
    core.save_editor.crypt.recipes for where this cap is actually enforced,
    both for the search itself and for what a single decompression attempt
    may produce."""
    return recipes.MAX_RECIPE_BYTES


def _cache_key(data: bytes) -> str:
    return hashlib.sha1(data[:64]).hexdigest()


def _remember(key: str, spec: tuple) -> None:
    if len(_RESULT_CACHE) >= _RESULT_CACHE_KEEP:
        _RESULT_CACHE.clear()
    _RESULT_CACHE[key] = spec


def _candidate_readers(payload: bytes, path) -> list:
    """Registry readers worth trying on an unwrapped *payload* — sniffed by
    content, plus the generic fallbacks, MINUS the searching readers: a
    recipe search finding a candidate that itself wants to hunt for a key
    or a seed would turn one opt-in "much slower" escalation into a nested
    one, which is not what a person asking for this once bargained for.
    """
    from . import registry as registry_module

    ext = path.suffix.lower() if path is not None else ""
    seen = set()
    out = []
    for cls in registry_module.sniffed(path, payload, ext):
        if cls not in seen:
            seen.add(cls)
            out.append(cls)
    for cls in registry_module.fallback_readers():
        if cls not in seen:
            seen.add(cls)
            out.append(cls)
    expensive = registry_module.expensive_readers()
    return [cls for cls in out if cls not in expensive]


def _verify_candidate(cls, fmt, payload: bytes) -> bool:
    """The same exact-or-value round trip + parse_is_plausible() standard
    open_save's main candidate loop holds every top-level save to — not the
    stricter byte-exact-only gate the variant/full-sweep passes use, since
    an inner candidate here is exactly the kind of re-serialising format
    that standard already accommodates.
    """
    try:
        exact = getattr(fmt, "verify_exact", True)
        if exact:
            if fmt.dump() != payload:
                return False
            return bool(fmt.parse_is_plausible())
        if hasattr(fmt, "verify_value_round_trip"):
            if not fmt.verify_value_round_trip():
                return False
            return bool(fmt.parse_is_plausible())
        rebuilt = fmt.dump()
        probe = cls()
        if hasattr(probe, "source_path") and hasattr(fmt, "source_path"):
            probe.source_path = fmt.source_path
        probe.load(rebuilt)
        if ([(f.label, f.value) for f in probe.fields()]
                != [(f.label, f.value) for f in fmt.fields()]):
            return False
        return bool(fmt.parse_is_plausible())
    except Exception:
        return False


class RecipeFormat(_Format):
    """A mystery save, opened by trying generic unwrap recipes.

    Cost, measured by ``maintenance/test_recipe_format.py`` on its own negative
    test case (4 MiB of random data — the worst case, since nothing in it
    ever passes the cheap prefix gate and every one of the roughly 10⁴
    recipes runs to completion): under 3-4 seconds on ordinary hardware.
    Nowhere near ``wolf_lz4.find_seed``'s own worst case (~10 minutes) —
    this search is ~10⁴ combinations, not 2³², and needs neither numpy nor
    multiprocessing to stay well inside "much slower, but still seconds".
    """
    name = "Recovered"
    engine = ""
    # A recipe's inner content is exactly the re-serialising case: it went
    # through compression/decompression and an XOR keystream, so comparing
    # raw bytes across that means nothing — values are what is compared.
    verify_exact = False

    def __init__(self):
        self.source_path = None
        self._inner = None
        self._inner_cls = None
        self._spec = None
        self._header = b""
        self._description = ""

    def _path_obj(self):
        return self.source_path if self.source_path is not None else Path("")

    def _try_payload(self, payload: bytes):
        """The first candidate reader (if any) that both loads *payload*
        and proves the round trip — or None."""
        path = self._path_obj()
        for cls in _candidate_readers(payload, path):
            fmt = cls()
            if hasattr(fmt, "source_path"):
                fmt.source_path = self.source_path
            try:
                fmt.load(payload)
            except Exception:
                continue
            if not _verify_candidate(cls, fmt, payload):
                continue
            return cls, fmt
        return None

    def load(self, data: bytes) -> None:
        if not data or len(data) > recipes.MAX_RECIPE_BYTES:
            raise SaveEditorError(
                "too large for a generic unwrap search — this is meant for "
                "small, obfuscated saves, not archives")

        key = _cache_key(data)
        cached_spec = _RESULT_CACHE.get(key)
        if cached_spec is not None:
            payload = recipes.apply_recipe(data, cached_spec)
            if payload is not None:
                hit = self._try_payload(payload)
                if hit is not None:
                    cls, fmt = hit
                    self._accept(data, cached_spec, cls, fmt, "cached")
                    return

        for description, spec, payload in recipes.find_candidates(data):
            hit = self._try_payload(payload)
            if hit is None:
                continue
            cls, fmt = hit
            self._accept(data, spec, cls, fmt, description)
            _remember(key, spec)
            return

        raise SaveEditorError(
            "no generic unwrap recipe exposed content a registered reader "
            "could confirm")

    def _accept(self, data: bytes, spec: tuple, cls, fmt, description: str) -> None:
        self._spec = spec
        self._header = data[:spec[0]]
        self._inner_cls = cls
        self._inner = fmt
        self._description = description
        # The instance name, not the class default: what actually surfaces
        # in the UI is what was found (e.g. "JSON") rather than a static
        # "Recovered" — make_doc/engine_label_for read getattr(fmt, "name",
        # cls.name), so this is the name a recipe-opened save is shown
        # under. The recipe's own description stays a debug-log detail.
        self.name = getattr(fmt, "name", cls.name)

    def dump(self) -> bytes:
        payload = self._inner.dump()
        return recipes.lock_recipe(payload, self._spec, self._header)

    def fields(self) -> list:
        return self._inner.fields()

    def set_field(self, path: tuple, value) -> None:
        self._inner.set_field(path, value)

    def parse_is_plausible(self) -> bool:
        try:
            return bool(self._inner.parse_is_plausible())
        except Exception:
            return False

    def verify_value_round_trip(self) -> bool:
        """Proves the LOCK direction explicitly: recompress, re-wrap, and
        read the result back through this same recipe — the search loop
        that found it only ever proved unlock, and dump()/saving depends
        on lock, so nothing else checks it if this does not.
        """
        try:
            payload = self._inner.dump()
        except Exception:
            return False
        try:
            relocked = recipes.lock_recipe(payload, self._spec, self._header)
            reapplied = recipes.apply_recipe(relocked, self._spec)
        except Exception:
            return False
        if reapplied is None:
            return False
        probe = self._inner_cls()
        if hasattr(probe, "source_path"):
            probe.source_path = self.source_path
        try:
            probe.load(reapplied)
        except Exception:
            return False
        try:
            return ([(f.label, f.value) for f in probe.fields()]
                    == [(f.label, f.value) for f in self._inner.fields()])
        except Exception:
            return False
