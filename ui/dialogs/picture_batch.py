"""
SaveSync - Pictures fetched for a game in the background.

PictureBatch downloads and stores a set of pictures (covers, screenshots) on
worker threads: the GUI thread only ever receives the reports, one per picture.
It knows nothing of the Add/Edit dialog that asked for them, so it can outlive
it — LatePictures carries on with whatever was still coming when Save was
pressed, into the game that was just stored, with no dialog left alive for it.
"""
import logging
import queue
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.constants import get_install_folder_name
from core.library import get_library
from ui.image_cache import (_ICON_CACHE_DIR, discard_picture, move_picture_into,
                            store_picture)

logger = logging.getLogger(__name__)

_WORKERS = 3            # at a time: a store lists a dozen, each a few hundred KB
_FETCH_TIMEOUT = 20     # s per reply; core.net.fetch_image also bounds the whole picture


class PictureBatch(QObject):
    """Pictures downloaded, re-encoded and written to a game's cache folder.

    ``picture_done(rank, url, path, created)`` is emitted on the GUI thread once
    per picture, whatever became of it: *path* is the file it was stored as ("" when
    it could not be had), *created* whether this batch wrote that file (False: an
    identical one was there already). *rank* is the caller's: it comes back as it
    went in. *url* is the one the bytes really came from (see core.net).

    Every picture is reported exactly once — a fetch that raises, a decoder that
    chokes and a disk that is full all come back as a failure, so ``pending``
    always gets down to 0 — unless cancel() was called, which reports nothing
    more.

    There is ONE destination folder, read as each picture is about to be written,
    not when it was asked for: redirect() changes it, and everything not yet
    written goes to the new one (the game was renamed meanwhile). What was
    reported before stays where it was written — moving that is the owner's
    business, it knows where it put the picture."""

    picture_done = Signal(int, str, str, bool)
    # One worker's report, from its own thread to the GUI thread:
    # generation, rank, url, path, created
    _reported = Signal(int, int, str, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()           # guards _workers
        self._todo: "queue.SimpleQueue" = queue.SimpleQueue()
        self._workers = 0
        self._gen = 0                           # bumped by cancel(): older work is void
        self._pending = 0
        self._dest: Optional[Path] = None
        self._reported.connect(self._on_reported)

    @property
    def pending(self) -> int:
        """Pictures asked for and not reported yet."""
        return self._pending

    @property
    def dest(self) -> Optional[Path]:
        """The folder the next picture is written to."""
        return self._dest

    def redirect(self, dest_dir) -> None:
        """From now on, write to *dest_dir* — including the pictures already asked
        for that have not been written yet."""
        self._dest = Path(dest_dir)

    def add(self, jobs, dest_dir, same_as=()) -> None:
        """Fetch and store *jobs* — ``(url, rank)`` pairs — into *dest_dir*.

        *same_as* are files a picture may turn out to be a copy of (the ones in the
        carousel); *dest_dir*'s own files are always compared. Call on the GUI
        thread."""
        jobs = list(jobs)
        if not jobs:
            return
        self.redirect(dest_dir)
        same_as = tuple(str(p) for p in same_as)
        self._pending += len(jobs)
        for url, rank in jobs:
            self._todo.put((self._gen, rank, url, same_as))
        with self._lock:
            for _ in range(min(len(jobs), _WORKERS - self._workers)):
                worker = threading.Thread(target=self._work, daemon=True)
                self._workers += 1
                try:
                    worker.start()
                except RuntimeError:
                    self._workers -= 1
                    logger.exception("A picture worker could not be started")
                    break
            alive = self._workers
        if not alive:
            self._fail_queued()

    def cancel(self) -> None:
        """Nobody waits for what is queued or in flight any more. Nothing more is
        reported, and a file written for a picture that is no longer wanted is
        removed — by the worker that wrote it, or when its report arrives."""
        self._gen += 1
        self._pending = 0

    def _fail_queued(self) -> None:
        """No worker could be started: report what is waiting as failed, now."""
        while True:
            try:
                gen, rank, url, _same = self._todo.get_nowait()
            except queue.Empty:
                return
            self._on_reported(gen, rank, url, "", False)

    def _work(self) -> None:
        while True:
            try:
                gen, rank, url, same_as = self._todo.get_nowait()
            except queue.Empty:
                with self._lock:
                    # A job queued since the line above has a worker to start it
                    # only if this one is counted out here, under the lock.
                    if self._todo.empty():
                        self._workers -= 1
                        return
                continue
            if gen != self._gen:
                continue                        # cancelled while it waited
            shown, path, created = url, "", False
            try:
                got = self._fetch(url)
                if got is not None:
                    shown = got[0]
                    if gen == self._gen:
                        # Read now, not when the picture was asked for.
                        stored = store_picture(shown, got[1], self._dest, same_as)
                        if stored is not None:
                            path, created = str(stored[0]), stored[1]
            except Exception:
                logger.exception(f"Picture could not be fetched or stored: {url!r}")
                path, created = "", False
            if gen != self._gen:
                # Cancelled meanwhile; whatever was written has no one to take it.
                if created:
                    discard_picture(path)
                continue
            try:
                self._reported.emit(gen, rank, shown, path, created)
            except RuntimeError:
                # The batch itself is gone: no one is waiting for any of it.
                if created:
                    discard_picture(path)
                return

    @staticmethod
    def _fetch(url: str):
        """(final url, bytes) for *url*, or None when it is not a picture. Only the
        network: a realistic browser User-Agent, redirects followed, a picture the
        candidate preview or the chip dialog already fetched handed back from the
        session's store (see core.net.fetch_image)."""
        if not url:
            return None
        if not (url.startswith("http") or url.startswith("//")):
            logger.warning(f"Not downloading {url!r}: not an http(s) link")
            return None
        from core.net import fetch_image
        return fetch_image(url, timeout=_FETCH_TIMEOUT)

    def _on_reported(self, gen: int, rank: int, url: str, path: str, created: bool) -> None:
        if gen != self._gen:
            # Cancelled after the worker had reported.
            if created:
                discard_picture(path)
            return
        self._pending = max(0, self._pending - 1)
        self.picture_done.emit(rank, url, path, created)


# ── What outlives a Save ─────────────────────────────────────────────────────

_late: "set[LatePictures]" = set()      # GUI thread only; keeps each alive until done


def game_folder(entry) -> Path:
    """The game's own folder in the icon cache, as the library names it NOW."""
    return _ICON_CACHE_DIR / get_install_folder_name(
        entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)


def late_pictures_running() -> bool:
    """True while pictures that outlived a Save are still being fetched."""
    return any(late.pending for late in _late)


class LatePictures(QObject):
    """The pictures still coming when Save was pressed.

    Save does not wait for them: they go on into the game that was just stored,
    with no dialog left to show them in. Each is put in the game's own cache
    folder — wherever it is NOW, so a rename since Save is followed — and one that
    ranks as a cover becomes the game's cover, unless the person has put another
    picture there since (the game's icon is then no longer the one this saw, and
    is left alone). A game deleted meanwhile just has nothing to receive them.

    ``finished(self)`` is emitted after the last picture."""

    finished = Signal(object)

    def __init__(self, batch: PictureBatch, entry, *, follows_cover: bool, cover_rank: int):
        super().__init__()
        self.game_id = entry.id
        self.name = entry.name
        self.total = batch.pending
        self.failed = 0
        self.dropped = 0                # arrived after the game was deleted
        self._batch = batch
        batch.setParent(self)
        # What a cover may still take over: only the icon this put there (or the
        # one the game had at Save, if it was what the batch itself had put on
        # show), and only while it is still the game's.
        self._follows = follows_cover
        self._cover_rank = cover_rank
        self._icon = entry.icon_path or ""
        # Straight into the game's folder as it is now: Save may have renamed it
        # (the folder was moved to the new name), and what comes next is written
        # there rather than into a folder that no longer exists.
        batch.redirect(game_folder(entry))
        _late.add(self)
        batch.picture_done.connect(self._on_picture_done)

    @property
    def pending(self) -> int:
        return self._batch.pending

    def _on_picture_done(self, rank: int, url: str, path: str, created: bool) -> None:
        try:
            if path:
                self._keep(rank, url, path, created)
            else:
                self.failed += 1
                logger.warning(f"Picture not added to {self.name!r}: {url!r}")
        except Exception:
            self.failed += 1
            logger.exception(f"A picture that arrived after Save could not be kept: {url!r}")
        if not self._batch.pending:
            logger.info(f"Pictures that were still coming at Save are in: {self.name!r} "
                        f"({self.failed} of {self.total} failed)")
            _late.discard(self)
            self.finished.emit(self)
            self.deleteLater()

    def _keep(self, rank: int, url: str, path: str, created: bool) -> None:
        lib = get_library()
        entry = lib.get_by_id(self.game_id)
        if entry is None:
            if created:
                discard_picture(path)
            self.dropped += 1
            logger.info(f"Picture dropped, {self.name!r} was deleted since Save: {url!r}")
            return
        src = Path(path)
        if _ICON_CACHE_DIR in src.parents:
            # Written before the game was renamed (or by a worker that had not
            # yet seen the new folder): it follows the game. (A copy found among
            # the carousel's files may be the person's own picture from elsewhere:
            # that one is used where it is, never moved.)
            src = move_picture_into(src, game_folder(entry))
        kept = str(src)
        if self._takes_cover(entry, rank):
            lib.update_game_fields(entry.id, icon_path=kept)
            self._icon, self._follows = kept, True
            self._cover_rank = max(self._cover_rank, rank)
            logger.info(f"Picture is now the cover of {self.name!r}: {url!r}")
        else:
            logger.info(f"Picture added to {self.name!r}: {url!r}")

    def _takes_cover(self, entry, rank: int) -> bool:
        current = entry.icon_path or ""
        if not current or not Path(current).is_file():
            return True                 # a game needs one
        # The same picture, whichever folder a rename has moved it to.
        return (self._follows and rank > self._cover_rank
                and Path(current).name == Path(self._icon).name)
