from .base import SaveEditorError, SaveField
from .json_format import JsonFormat

class Es3Format(JsonFormat):
    """Unity Easy Save 3 with encryption turned on.

    Inside it is JSON, so once it is open it behaves like any other JSON save
    — except that Easy Save wraps every value in its own type note, which is
    bookkeeping rather than anything to edit. See crypt/es3 for where the
    password comes from.
    """
    name = "Easy Save 3"
    engine = "Unity (Easy Save 3)"
    verify_exact = False

    def __init__(self):
        super().__init__()
        self.source_path = None
        self.game_dir = None
        # Told how long the hunt for a password has been going, and able to
        # call it off — see open_save. None means let it run.
        self.progress = None
        self._iv = b""
        self._password = ""

    def load(self, data: bytes) -> None:
        from core.save_editor.crypt.es3 import (
            Es3Error, decrypt, find_password, is_encrypted,
        )
        if not is_encrypted(data):
            # Encryption is optional, and most games leave it off.
            return super().load(data)
        self._password = find_password(data, self.source_path, self.game_dir,
                                       progress=self.progress)
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
        from core.save_editor.crypt.es3 import dumps, encrypt
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


