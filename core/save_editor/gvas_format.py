from pathlib import Path

from .base import (
    SaveEditorError, SaveField, _Format, _leading_group, _unique,
)

class GvasFormat(_Format):
    """Unreal Engine ``.sav`` (GVAS), UE4 and UE5 up to 5.3.

    The property list is decoded; the scalar values in it are editable and
    everything else travels as the bytes it arrived as. See core/gvas for why
    the layout is taken from the reference implementation rather than
    reconstructed.
    """
    name = "Unreal Engine"
    engine = "Unreal Engine (GVAS)"

    # The four layout decisions the GVAS header's version numbers drive. Each
    # has moved at least once in Unreal's history, and each is a plain
    # yes/no, so a build that moves one again is one of sixteen shapes rather
    # than an unknown. See variants().
    _VERSION_SWITCHES = ("force_ue5_field", "force_custom_versions",
                         "force_new_tag", "force_guid")

    def __init__(self):
        self._save = None
        self._overrides = {}

    def load(self, data: bytes) -> None:
        from core.engines.gvas import GvasError, GvasSave
        save = GvasSave()
        for key, value in self._overrides.items():
            setattr(save, key, value)
        try:
            save.load(data)
        except GvasError as e:
            raise SaveEditorError(str(e)) from e
        self._save = save

    @classmethod
    def variants(cls):
        """Every combination of the four version-driven layout switches.

        Unreal decides where the UE5 package version sits, whether a custom
        version block follows, whether property tags carry a GUID, and (since
        5.4) which property-tag shape is used — all from version numbers in
        the header. Those thresholds are written down from the engine's own
        history, so they are right about every build that existed when they
        were written and can be wrong about the next one. When that happens
        the reader walks off into the middle of a property and the save reads
        as unopenable, which is a poor answer for a file that is a perfectly
        ordinary GVAS save one field out of step.

        Sixteen combinations, minus the one already tried as written. Each is
        cheap (a header re-read plus a property walk), each has to rebuild the
        file byte-for-byte, and each has to have parsed it — see
        parse_is_plausible. So this widens what can be OPENED without widening
        what can be written wrongly.
        """
        for mask in range(1, 1 << len(cls._VERSION_SWITCHES)):
            overrides = {}
            label = []
            for bit, key in enumerate(cls._VERSION_SWITCHES):
                value = bool(mask & (1 << bit))
                overrides[key] = value
                label.append("%s=%d" % (key[len("force_"):], value))

            def tweak(fmt, _o=overrides):
                fmt._overrides = dict(_o)

            yield ", ".join(label), tweak

    def parse_is_plausible(self) -> bool:
        """Did this reading actually account for the file?

        GvasSave keeps whatever follows the property list as an opaque tail
        and writes it back untouched, which is right — it is how a property
        type this reader has never met survives a round trip. But it means a
        WRONG reading that stumbles onto an early "None" can rebuild the file
        perfectly while having parsed almost nothing, and byte-equality would
        call that a success. A real parse reaches the end of the properties:
        it finds some, and it leaves a few bytes behind, not most of the file.
        """
        save = self._save
        if save is None or not save.props:
            return False
        body = max(1, (save.raw_len or len(save.header) + len(save.tail)) - len(save.header))
        return len(save.tail) <= max(64, body // 8)

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        rows = self._save.values()
        names = _unique([name for _i, name, _k, _v in rows])
        return [SaveField((i,), names[n], kind, value, _leading_group(names[n]))
                for n, (i, _name, kind, value) in enumerate(rows)]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.gvas import GvasError
        try:
            self._save.set_value(path[0], value)
        except GvasError as e:
            raise SaveEditorError(str(e)) from e


class UnrealEncryptedFormat(GvasFormat):
    """An Unreal save the game locked with a key of its own.

    The same file GvasFormat reads, with everything — the magic included —
    under encryption. See crypt/unreal_crypt: the key is never guessed at, it
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
        from core.save_editor.crypt.game_keys import key_from_file, stored_key
        from core.save_editor.crypt.unreal_crypt import (
            KEY_FILE, decrypt, find_key, game_binaries,
        )
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
        from core.save_editor.crypt.game_keys import store_key
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
        from core.save_editor.crypt.unreal_crypt import encrypt
        return encrypt(super().dump(), self._key, self._how)


