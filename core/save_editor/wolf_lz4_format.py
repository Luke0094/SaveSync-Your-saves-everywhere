"""Wolf RPG Editor (LZ4 variant) — binary save adapter for the Save Editor UI.

Cryptography and the seed search live in ``core.engines.wolf_lz4``; unlock +
variable database parsing in ``crypt.wolf_lz4`` (itself built on
``crypt.wolf``, which already knows the database shape this format shares —
see that module's own docstring). This one only bridges that into the
shared ``SaveField`` list the dialog expects, the same job
``wolf_format.py`` does for the standard scheme.
"""
import struct

from .base import SaveEditorError, SaveField, _Format


class WolfLZ4Format(_Format):
    """Wolf RPG Editor saves, the LZ4-compressed variant.

    Unlocking this one costs a seed search the first time a given save
    SLOT is opened — see ``core.engines.wolf_lz4.find_seed`` — which is why
    it carries a ``progress`` hook (open_save wires it up automatically,
    same as Easy Save 3's password search) and is registered as
    ``expensive`` (see ``registry.py``): never worth blind-sniffing every
    ``.sav`` on the chance it is one of these, only tried when the game is
    already known to be Wolf RPG Editor.
    """
    name = "Wolf RPG (LZ4)"
    engine = "Wolf RPG Editor"

    def __init__(self):
        self._save = None
        self.source_path = None
        # Filled in by open_save's prepare() when the library knows the
        # game's install folder — one more place a seed already found for
        # this save's slot might be written down (see crypt.wolf_lz4's own
        # game_keys-backed recall). Not required: the save's own folder
        # (and a couple of parents) is usually enough on its own.
        self.game_dir = None
        # Told how long the seed search has been going, and able to call it
        # off — see open_save. None means let it run.
        self.progress = None
        # Filled in the same way as progress — see open_save's prepare().
        # Lets whoever asked for the search stop it immediately (terminate
        # the search's own Pool, not just set a flag it notices later) —
        # see core.engines.wolf_lz4.CancelToken for why that distinction
        # matters. None means there is nothing to hand a search that can
        # already only be stopped through progress returning False.
        self.cancel_token = None

    def load(self, data: bytes) -> None:
        from core.engines.wolf_lz4 import WolfLZ4Error
        from core.save_editor.crypt.wolf import WolfError
        from core.save_editor.crypt.wolf_lz4 import loads
        try:
            self._save = loads(data, save_path=self.source_path,
                               progress=self.progress, game_dir=self.game_dir,
                               cancel_token=self.cancel_token)
        except (WolfError, WolfLZ4Error, struct.error, IndexError, ValueError) as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField((i,), name, kind, value, name.split(" / ")[0])
                for i, name, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        self._save.set_value(path[0], value)
