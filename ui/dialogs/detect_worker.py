"""
SaveSync - Background save-path detection worker for the Add/Edit dialog.

Extracted verbatim from ui/dialogs/add_game_dialog.py. Runs the full
detection engine (live tracking priority + optional general scan) off the
GUI thread. Pure move — no behavior change.
"""
import logging

from PySide6.QtCore import QThread, Signal

from core.save_detector import detect_save_paths

logger = logging.getLogger(__name__)


class DetectWorker(QThread):
    found = Signal(list, bool)  # paths, is_live_tracking

    def __init__(self, game_name: str, exe_path: str, general_scan: bool = False, tracked_snapshot: dict = None, appid: str = ""):
        super().__init__()
        self._game_name = game_name
        self._exe_path  = exe_path
        self._general_scan = general_scan
        self._tracked_snapshot = tracked_snapshot or {}
        self._appid = appid
        self._should_stop = False

    def stop(self):
        self._should_stop = True
        from core.save_detector import cancel_detection
        cancel_detection()

    def run(self):
        """Enhanced detection with live tracking priority and optional general scan"""
        try:
            if self._should_stop:
                self.found.emit([], False)
                return
            # First, check if the game is currently running (manual addition too)
            pid = None
            is_live = False
            try:
                for key, entry in self._tracked_snapshot.items():
                    if self._should_stop:
                        self.found.emit([], False)
                        return
                    if entry is None:
                        continue
                    if (entry.exe_path == self._exe_path or
                        entry.name.lower() == self._game_name.lower()):
                        pid = key[0]  # ProcessKey is (pid, create_time)
                        is_live = True
                        logger.info(f"Game {self._game_name} is running with PID {pid} - using live tracking")
                        break
            except Exception as e:
                logger.debug(f"Could not check running status for {self._game_name}: {e}")

            if self._should_stop:
                self.found.emit([], False)
                return
            # Use live tracking if game is running (most reliable)
            detected_paths = detect_save_paths(
                game_name=self._game_name,
                exe_path=self._exe_path,
                pid=pid,  # This enables live tracking if PID is available
                appid=self._appid  # Also search using appid for external launcher games
            )
            
            # If general scan is requested and no live tracking was used,
            # add additional paths from the shared broader scan (appid is
            # already handled by detect_save_paths).
            if self._general_scan and not is_live:
                try:
                    from core.save_detector import general_scan_paths, expand_selectable_paths
                    from core.constants import SAVE_FOLDER_HINTS
                    from core.config_manager import get_config as _get_cfg

                    # User-configurable "save folder suggestions" from Settings
                    # drive scoring confidence here — same as detect_save_paths.
                    hints = _get_cfg().get("save_folder_hints", SAVE_FOLDER_HINTS)
                    detected_paths.extend(general_scan_paths(
                        self._game_name, self._exe_path, hints, detected_paths,
                        should_stop=lambda: self._should_stop,
                        timeout_s=60,
                        require_backup_content=True,
                    ))
                    # Normalize the merged detect+general set: expand folders
                    # without direct files into per-subfolder entries and drop
                    # parent/child duplicates (e.g. "game" + "game/save").
                    detected_paths = expand_selectable_paths(detected_paths)

                except Exception as e:
                    logger.debug(f"General scan failed for {self._game_name}: {e}")
            
            if self._should_stop:
                self.found.emit([], False)
                return
            
            method = "live tracking" if is_live else ("general scan" if self._general_scan else "filesystem scan")
            logger.info(f"Manual addition detection for {self._game_name} via {method}: {len(detected_paths)} paths")
            
            # Emit both paths and method info
            self.found.emit(detected_paths, is_live)
            
        except Exception as e:
            if self._should_stop:
                return
            logger.error(f"Detection failed for {self._game_name}: {e}")
            self.found.emit([], False)  # Emit empty list on error


