from .base import SaveEditorError, SaveField, _Format


class AliceSoftVsfFormat(_Format):
    """AliceSoft's VSF save-slot summary (date, scene description, playtime).

    See core/engines/alicesoft_vsf, which is written from the engine
    reimplementation the format is described in rather than from staring at
    bytes. Sits beside a GSAVE ``.asd``/``.sav`` from the same slot, which
    is what ``alicesoft_format`` reads — this is the other half, the
    load-menu summary rather than the game's own resumable state.

    The container names no fields — a game's own script decides how many
    values there are and in what order — so each slot is offered by its
    position and its type rather than a guessed-at name, the same honesty
    AliceSoftFormat already extends to a numbered global it cannot name.
    """
    name = "AliceSoft VSF"
    engine = "AliceSoft System"

    def __init__(self):
        self._save = None

    def load(self, data: bytes) -> None:
        from core.engines.alicesoft_vsf import VsfError, loads
        try:
            self._save = loads(data)
        except VsfError as e:
            raise SaveEditorError(str(e)) from e

    def dump(self) -> bytes:
        return self._save.dump()

    def fields(self) -> list:
        return [SaveField((i,), f"Value {i + 1}", kind, value)
                for i, kind, value in self._save.values()]

    def set_field(self, path: tuple, value) -> None:
        from core.engines.alicesoft_vsf import VsfError
        try:
            self._save.set_value(path[0], value)
        except VsfError as e:
            raise SaveEditorError(str(e)) from e
