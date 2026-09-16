"""Wolf RPG Editor saves, the LZ4 variant: unlock and read the values inside.

``core.engines.wolf_lz4`` holds the cryptography and the seed search. This
module bridges that into the same variable-database reader
``core.save_editor.crypt.wolf`` already has, rather than writing a second
one: once unlocked, this format's body is exactly the same shape a standard
Wolf save's is (see wolf_lz4's own module docstring) — marker, name, and the
variable database ``WolfValues`` already knows how to find, label, edit and
splice edits back into. Only how the bytes get UNLOCKED differs; everything
past that is inherited.

The seed search itself can cost minutes the first time a save SLOT is
opened (see ``find_seed``'s own docstring) — worth paying once, never
worth paying again for the same slot. ``core.engines.wolf_lz4`` already
remembers a seed in memory for the life of the running app; this module
adds the other half, written down against the GAME the same way
``crypt.es3`` remembers an Easy Save 3 password — so the answer survives
closing SaveSync, not just closing one save. See ``game_keys.stored_value``/
``store_value``: one game can have many save slots, each with its own salt
and seed, which is why this is a small table keyed by salt rather than one
flat key the way Easy Save 3's password is.
"""
import logging
from pathlib import Path

from core.engines.wolf_lz4 import (WolfLZ4Error, find_seed, is_candidate,
                                   lock, salt, unlock, verify_seed)
from core.save_editor.crypt.wolf import WolfSaveError, WolfValues

logger = logging.getLogger(__name__)

_KIND = "wolfrpg_lz4"
# The save's own folder, and a couple above it — same reasoning and same
# depth as crypt.es3's own climb: a save kept inside "<Game>/Save/" needs to
# climb past that one folder to reach the game itself, and this stays a
# look at the game rather than a disk search.
_MAX_CLIMB = 3


class WolfLZ4SaveError(WolfSaveError):
    pass


def _validate_wolf_body(payload: bytes) -> bool:
    """A real proof the engine's own cheap ~15-byte check can't give: does
    *payload* actually contain a parseable Wolf variable database — by
    trying ``WolfValues._locate``/``_parse_db`` for real, not just checking
    the shape of its first ~15 bytes.

    Confirmed directly against real captured game data both ways: a
    correctly-decrypted ``SaveData02.sav`` payload loads cleanly (44,464
    real records) — this genuinely works, not just in theory. A
    correctly-decrypted ``System.sav`` payload does NOT (raises "could not
    find the variable database"), and that is CORRECT, not a bug: Wolf RPG
    Editor's ``System.sav`` holds engine/system state, not the player
    variable database this parser looks for — the same "could not find"
    result the plain, unmodified ``WolfValues.load()`` path gives on it
    too, independent of anything wolf_lz4-specific.

    That means this function is a real proof when it says yes, but a firm
    "no" from it does NOT mean "wrong seed/span" — it can also just mean
    "this particular save file never had one". Callers use it as a FIRST,
    stronger attempt with the cheap default as fallback (see
    ``WolfLZ4Values.load``), never as the only check, so a save like
    ``System.sav`` still opens via the same path it always did rather than
    being refused outright.
    """
    try:
        WolfValues(body_offset=0).load(payload, plain=payload)
        return True
    except Exception:
        return False


def _places(save_path, game_dir) -> list:
    """Candidate folders that stand for "this game", closest first — see
    crypt.es3.find_password, which this mirrors exactly."""
    places = []
    if save_path:
        here = Path(save_path).parent
        places.append(here)
        for parent in list(here.parents)[:_MAX_CLIMB]:
            places.append(parent)
    if game_dir:
        places.append(Path(game_dir))
    seen, unique = set(), []
    for place in places:
        try:
            marker = str(place.resolve()).lower()
        except OSError:
            marker = str(place).lower()
        if marker not in seen:
            seen.add(marker)
            unique.append(place)
    return unique


def _recall_seed(raw: bytes, places: list):
    """A seed remembered from a previous open of a save from the SAME slot
    (same salt), for any of *places* — re-proven with verify_seed before
    being trusted, the same way crypt.es3 re-checks a remembered password
    rather than assuming a stored value still applies."""
    if not places:
        return None
    from core.save_editor.crypt.game_keys import stored_value
    name = bytes(salt(raw)).hex()
    for place in places:
        remembered = stored_value(_KIND, place, name)
        if not remembered:
            continue
        try:
            seed = int(remembered)
        except ValueError:
            continue
        if verify_seed(raw, seed):
            return seed
    return None


def _remember_seed(raw: bytes, place, seed: int) -> None:
    if place is None:
        return
    from core.save_editor.crypt.game_keys import store_value
    store_value(_KIND, place, bytes(salt(raw)).hex(), str(seed))


class WolfLZ4Values(WolfValues):
    """A Wolf-LZ4 save's variable database, opened for editing.

    *body_offset* is 0, not the standard scheme's 0x14: what ``load()`` hands
    the shared parser is the already-decompressed payload, whose OWN first
    byte is the marker — there is no clear header in front of it to skip,
    that header belongs to the OUTER, still-locked file.
    """

    def __init__(self):
        super().__init__(body_offset=0)
        self._seed = None
        # The two length fields plus whatever's in the clear header, from
        # unlock() — dump() needs them back to rebuild the outer file.
        self._locked_header = b""
        # The (a, b) post-encrypt swap span offsets unlock() found, or None
        # if this file didn't need one — see core.engines.wolf_lz4's own
        # module docstring. dump() re-applies the same swap on the way out.
        self._swap_spans = None
        # Whether _swap_spans (when not None) was confirmed by the strong,
        # real-parser validator rather than accepted on the cheap default
        # alone, at READ time — kept for diagnostics, not read by dump()
        # any more: dump() now refuses to write back ANY save that needed a
        # swap at all, validated or not (see dump()'s own comment for why
        # "validated for reading" turned out not to mean "safe to reuse for
        # writing").
        self._swap_validated = False

    def load(self, raw: bytes, project=None, seed: int = None, progress=None,
             save_path=None, game_dir=None, cancel_token=None) -> None:
        if not is_candidate(raw):
            raise WolfLZ4SaveError("not tagged as a Wolf-LZ4 save")
        places = _places(save_path, game_dir)
        fresh = seed is None
        if fresh:
            seed = _recall_seed(raw, places)
            fresh = seed is None
        if fresh:
            seed = find_seed(raw, progress=progress, cancel_token=cancel_token)
        self._seed = seed
        if self._seed is None:
            raise WolfLZ4SaveError(
                "could not recover this save's encryption key — either it "
                "was called off, this file's salt genuinely has none "
                "(corrupted, or truncated), or this save's own post-encrypt "
                "byte swap happens to land inside the cheap filter's own "
                "check window, hiding the true key from the search entirely "
                "(see core.engines.wolf_lz4._MARKER_CHECK_BASES)")
        # fresh here means "found just now by a real search" — a cancelled
        # or failed search already raised above, and a recalled/explicit
        # seed is already written down (or deliberately not this module's
        # to write down), so only a genuine new find is worth persisting.
        if fresh and places:
            _remember_seed(raw, places[0], self._seed)
        # Strong-parser-validated first: real testing (not theorised) found
        # the cheap default alone can accept a wrong seed/swap-span pair on
        # some content (see core.engines.wolf_lz4.unlock's docstring) — a
        # real Wolf variable database parsing cleanly is proof the cheap
        # check can't give. Falls back to the cheap default only if THAT
        # fails too: a genuine "no" from _validate_wolf_body can mean "this
        # save never had a variable database" (System.sav, say) rather than
        # "wrong seed" — see that function's own docstring — and a file
        # like that should still open the way it always has, not be
        # refused just because the stronger check doesn't apply to it.
        try:
            self._locked_header, _dst_cap, payload, self._swap_spans = \
                unlock(raw, self._seed, validator=_validate_wolf_body)
            self._swap_validated = True
        except WolfLZ4Error:
            try:
                self._locked_header, _dst_cap, payload, self._swap_spans = \
                    unlock(raw, self._seed)
                self._swap_validated = False
            except WolfLZ4Error as e:
                raise WolfLZ4SaveError(str(e)) from e
        super().load(raw, project=project, plain=payload)

    def dump(self) -> bytes:
        """Splice the edits into the decompressed body, then recompress and
        re-lock — the reverse of ``load``'s unlock.

        The swap span (see ``_swap_spans``), when this save needs one, is
        not reused as-is: it is recomputed fresh from ``_locked_header``
        via the closed-form ``derive_swap_spans`` (see
        ``core.engines.wolf_lz4``'s module docstring for where that came
        from) and checked against what ``load`` actually found. Since both
        offsets are a pure function of two bytes IN that header, and an
        edit never touches the header at all, this always reproduces
        exactly what ``load`` used — recomputing it is not a formality,
        it is what turns "reuse the old value" from an assumption into a
        proof. The one case this format's own "never guess" standard
        still applies to: a save whose swap ``load`` could only locate via
        the search fallback (see ``unlock``'s own docstring) — meaning
        this formula did not explain it, which real testing has not seen
        happen but which this still refuses rather than risk.
        """
        if self._swap_spans is not None:
            from core.engines.wolf_lz4 import derive_swap_spans
            recomputed = derive_swap_spans(self._locked_header)
            if recomputed != self._swap_spans:
                raise WolfLZ4SaveError(
                    "this save's post-encrypt byte swap was not one the "
                    "known formula explains (it needed the slower search "
                    "fallback to be read at all) — writing is refused "
                    "rather than risking a save the real game cannot load "
                    "correctly")
            self._swap_spans = recomputed
        edits = [r for r in self._records if "new" in r]
        out = self.plain
        for rec in sorted(edits, key=lambda r: r["offset"], reverse=True):
            o, n = rec["offset"], rec["length"]
            out = out[:o] + rec["new"] + out[o + n:]
        return lock(self._locked_header, self._seed, out, self._swap_spans)


def loads(raw: bytes, save_path=None, seed: int = None, progress=None,
         game_dir=None, cancel_token=None) -> WolfLZ4Values:
    """Read a Wolf-LZ4 save. Mirrors ``crypt.wolf.loads`` — same reason to
    look beside the save for the game's own database.

    *game_dir*, when the caller has it, is one more place a remembered seed
    might be written down under — see ``_places``. Not required: the save's
    own folder (and a couple of parents) is usually enough on its own.

    *cancel_token*, when given a ``core.engines.wolf_lz4.CancelToken``, lets
    a caller stop an in-progress search immediately rather than waiting for
    *progress* to next be consulted — see that class's own docstring.
    """
    from core.save_editor.crypt.wolf import find_project

    save = WolfLZ4Values()
    save.load(raw, find_project(save_path) if save_path else None,
              seed=seed, progress=progress, save_path=save_path,
              game_dir=game_dir, cancel_token=cancel_token)
    return save
