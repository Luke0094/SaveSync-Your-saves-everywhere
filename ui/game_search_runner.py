"""
SaveSync - Batch web search after a folder scan.

Deliberately a controller, not a dialog: the run must survive its own window
being closed, so the user can start a search over fifty titles, put it away,
keep using the app, and reopen it later from the sidebar. The panel
(ui/dialogs/game_search_panel.py) is a view onto this object and holds no
state of its own.

One title at a time on a worker thread — the search is network-bound and
already parallel internally — with the cancel flag checked between titles so
stopping is immediate in practice and never leaves a half-applied entry.
"""
import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)


class _SearchWorker(QThread):
    one_done = Signal(str, str, bool, str)   # game_id, name, matched, detail
    all_done = Signal()

    def __init__(self, game_ids: list, cancel_event: threading.Event, parent=None):
        super().__init__(parent)
        self._game_ids = list(game_ids)
        self._cancel = cancel_event

    def run(self):
        from core.library import get_library
        from core.game_api import search_game_info_multi
        from core.enrichment import apply_game_info
        from pathlib import Path

        lib = get_library()
        for game_id in self._game_ids:
            if self._cancel.is_set():
                logger.info("Batch search cancelled")
                break
            entry = lib.get_by_id(game_id)
            if entry is None:
                continue
            name = entry.name
            try:
                folder = ""
                if entry.exe_path:
                    try:
                        folder = Path(entry.exe_path).parent.name
                    except Exception:
                        folder = ""
                results = search_game_info_multi(
                    name,
                    appid=entry.appid or None,
                    enable_web_fallback=True,
                    exe_path=entry.exe_path or "",
                    folder_name=folder,
                )
            except Exception as e:
                logger.debug(f"Search failed for {name}: {e}")
                self.one_done.emit(game_id, name, False, str(e)[:80])
                continue

            if self._cancel.is_set():
                break
            if not results:
                self.one_done.emit(game_id, name, False, "")
                continue

            # "Auto-accept the first valid result": the list is already
            # scored best-first and thresholded by the search layer.
            best = results[0]
            try:
                changed = apply_game_info(entry, best)
                if changed:
                    lib.update_game(entry)
                self.one_done.emit(game_id, name, bool(changed),
                                   best.name if best else "")
            except Exception as e:
                logger.warning(f"Could not apply search result for {name}: {e}")
                self.one_done.emit(game_id, name, False, str(e)[:80])
        self.all_done.emit()


class GameSearchRunner(QObject):
    """Owns a batch search. Survives the panel that shows it."""

    progress = Signal(int, int, str)     # done, total, last title
    finished = Signal(int, int, bool)    # matched, total, cancelled
    log_line = Signal(str, bool)         # message, matched

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._cancel = threading.Event()
        self.total = 0
        self.done = 0
        self.matched = 0
        self.lines: list = []            # (message, matched) — replayed into a reopened panel
        self.cancelled = False
        self.running = False

    # ── Control ──────────────────────────────────────────────────────────────

    def start(self, game_ids: list) -> bool:
        if self.running or not game_ids:
            return False
        self._cancel = threading.Event()
        self.total = len(game_ids)
        self.done = self.matched = 0
        self.lines = []
        self.cancelled = False
        self.running = True
        self._worker = _SearchWorker(game_ids, self._cancel)
        self._worker.one_done.connect(self._on_one)
        self._worker.all_done.connect(self._on_all)
        self._worker.start()
        logger.info(f"Batch web search started for {self.total} title(s)")
        return True

    def cancel(self):
        """Ask the run to stop. It ends after the title in flight — no entry
        is ever left half-written."""
        if not self.running:
            return
        self.cancelled = True
        self._cancel.set()
        logger.info("Batch web search cancel requested")

    def wait(self, ms: int = 15000) -> bool:
        if self._worker is None:
            return True
        return self._worker.wait(ms)

    @property
    def has_run(self) -> bool:
        """True once a search exists to look at — running or finished."""
        return self.total > 0

    # ── Worker callbacks ─────────────────────────────────────────────────────

    def _on_one(self, _game_id: str, name: str, matched: bool, detail: str):
        self.done += 1
        if matched:
            self.matched += 1
        line = f"{name} → {detail}" if (matched and detail and detail != name) else name
        self.lines.append((line, matched))
        self.progress.emit(self.done, self.total, name)
        self.log_line.emit(line, matched)

    def _on_all(self):
        self.running = False
        logger.info(f"Batch web search finished: {self.matched}/{self.total} matched"
                    + (" (cancelled)" if self.cancelled else ""))
        self.finished.emit(self.matched, self.total, self.cancelled)
