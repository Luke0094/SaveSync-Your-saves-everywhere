"""SaveSync — holding a value fixed in a save file.

Pick a value in the editor, hold it, and SaveSync keeps putting it back: it
watches the file, and every time the game writes a new value over it, the
held one goes back in. That is how a file-based editor gets you something
like unlimited health — not by touching the running game, but by winning
the argument about what the file says.

The limits are worth being straight about, because they decide whether this
does anything at all for a given game:

- it only bites when the game WRITES the save. A game that saves at
  checkpoints gets its values held at checkpoints; a game that autosaves
  constantly gets them held constantly.
- values the game keeps only in memory until you quit are out of reach. This
  is a file editor, and deliberately so — nothing is injected into the game.

Safety is the other half. The file is only read once it has stopped
changing, our own writes are recognised and ignored, one copy of the
original is kept when the hold starts (not once per cycle, which would bury
the backup folder), and repeated failures stop the hold instead of retrying
into a corrupt file forever.
"""
import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

# How often to look. A poll is one stat() call, so this is close to free —
# and reading the save when the time comes is instant on files this size.
# The pace is set by how quickly a value should come back, not by cost.
_POLL_MS = 200
# A save must look the same twice running before it is read: a game part-way
# through writing one has a valid mtime and half a file. With the poll above
# that puts a value back roughly half a second after the game overwrote it.
_STABLE_CHECKS = 2
# Consecutive WRITE failures before giving up. Something is wrong that
# retrying will not fix, and hammering a file the game is fighting us for is
# worse than stopping and saying so.
_MAX_FAILURES = 3
# Read failures are treated far more gently: at this cadence a file can look
# settled and still be mid-write, and a game that saves in stages can be
# briefly unreadable through no fault of its own. Only a file that stays
# unreadable this many rounds is actually broken.
_MAX_READ_FAILURES = 8


def _stamp(path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


class SaveHold(QObject):
    """Keeps chosen values in one save file at chosen values."""

    reapplied = Signal(int)      # how many values were put back, this round
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, path, values: dict, parent=None):
        """*values* maps a field LABEL to the value it must keep.

        By label, never by position. A field's path is where it sits in the
        decoded file — index 3 of this array, ivar 5 of that object — and the
        game rewriting its save can move things: one extra item in a list and
        the same path addresses a different value, which the hold would then
        confidently overwrite. Labels come from names (a key, an instance
        variable), so they survive the file being rebuilt. A label that turns
        up twice, or not at all, is skipped rather than guessed at.
        """
        super().__init__(parent)
        self._path = Path(path)
        self._values = dict(values)
        self._own_digest = b""       # what WE last wrote, so we ignore it
        self._last_stamp = _stamp(self._path)
        self._stable = 0
        self._failures = 0
        self._read_failures = 0
        self._rounds = 0
        self._backup = None
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def rounds(self) -> int:
        """How many times the held values have been put back."""
        return self._rounds

    @property
    def values(self) -> dict:
        return dict(self._values)

    def is_running(self) -> bool:
        return self._timer.isActive()

    def start(self):
        if self._timer.isActive() or not self._values:
            return
        from .save_editor import backup_original
        try:
            # ONE copy, when the hold starts. A copy per cycle would fill the
            # folder with near-identical files and bury the one that matters.
            self._backup = backup_original(self._path)
        except OSError as e:
            self.failed.emit(str(e))
            return
        self._last_stamp = _stamp(self._path)
        self._own_digest = self._digest()
        self._timer.start()
        logger.info(f"Holding {len(self._values)} value(s) in {self._path.name}")

    def stop(self):
        if not self._timer.isActive():
            return
        self._timer.stop()
        logger.info(f"Stopped holding values in {self._path.name} "
                    f"after {self._rounds} round(s)")
        self.stopped.emit()

    def set_values(self, values: dict):
        self._values = dict(values)
        if not self._values:
            self.stop()

    # ── the loop ─────────────────────────────────────────────────────────────

    def _digest(self) -> bytes:
        try:
            return hashlib.sha1(self._path.read_bytes()).digest()
        except OSError:
            return b""

    def _tick(self):
        stamp = _stamp(self._path)
        if stamp == (0, 0):
            return                          # gone for a moment: wait for it
        if stamp != self._last_stamp:
            # Still moving. Reset and let it settle before reading.
            self._last_stamp = stamp
            self._stable = 0
            return
        self._stable += 1
        if self._stable < _STABLE_CHECKS:
            return
        self._stable = 0

        digest = self._digest()
        if not digest or digest == self._own_digest:
            return                          # unchanged, or it was us
        self._reapply()

    def _reapply(self):
        from .save_editor import open_save, SaveEditorError

        try:
            doc = open_save(self._path)
        except SaveEditorError as e:
            # Most likely the game is still writing: at this cadence a file
            # can look settled between two stages of one save. Retry quietly,
            # and only call it broken once it has stayed unreadable.
            self._read_failures += 1
            logger.debug(f"Hold could not read {self._path.name} "
                         f"({self._read_failures}): {e}")
            if self._read_failures >= _MAX_READ_FAILURES:
                logger.info(f"Hold gave up on {self._path.name}: {e}")
                self.failed.emit(str(e))
                self.stop()
            return
        self._read_failures = 0

        # Resolve afresh every round, by name, and only when the name is
        # unambiguous in the file as it is NOW.
        by_label = {}
        for f in doc.fields:
            by_label.setdefault(f.label, []).append(f)
        drifted = {}
        for label, wanted in self._values.items():
            hits = by_label.get(label, [])
            if len(hits) != 1:
                if hits:
                    logger.debug(f"Hold skipped {label!r}: it appears "
                                 f"{len(hits)} times in the file now")
                continue
            field = hits[0]
            if field.value != wanted:
                drifted[label] = (field.path, wanted)
        if not drifted:
            # Nothing to do, but the file did change — remember it so the
            # next round compares against what is actually there.
            self._own_digest = self._digest()
            self._last_stamp = _stamp(self._path)
            self._failures = 0
            return

        for _label, (path, value) in drifted.items():
            doc.set_value(path, value)
        try:
            doc.write_without_backup()
        except Exception as e:                # OSError, encoding failures…
            self._failures += 1
            logger.warning(f"Hold could not write {self._path.name}: {e}")
            if self._failures >= _MAX_FAILURES:
                self.failed.emit(str(e))
                self.stop()
            return

        self._failures = 0
        self._rounds += 1
        self._own_digest = self._digest()
        self._last_stamp = _stamp(self._path)
        self.reapplied.emit(len(drifted))
        logger.debug(f"Put {len(drifted)} held value(s) back in "
                     f"{self._path.name}")
