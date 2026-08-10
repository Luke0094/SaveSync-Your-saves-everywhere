"""
SaveSync - Configuration Manager
JSON-based persistent settings with debounced disk writes.
"""
import copy
import json
import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, QTimer

from core.constants import CONFIG_FILE, SAVE_FOLDER_HINTS

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "language": "en",
    "theme": "dark",
    "launch_on_startup": False,
    "minimize_to_tray": True,
    "show_overlay_on_launch": True,
    # Popup + hotkey queue when an unknown process looks like a game.
    # Separate from the tracked-game launch toast. Off: no live popup, no
    # history queue, no badge — but the hotkey on the focused unknown
    # process still offers a quick add (session _pending_unknown).
    "show_overlay_on_unknown": True,
    "show_overlay_on_cloud": True,
    "show_overlay_on_backup": True,
    "overlay_hotkey": "alt+ctrl+s",
    "auto_backup": False,             # auto backup+sync without asking
    "auto_sync_after_backup": False,  # auto sync after backup completes
    "auto_scan_on_exit": True,        # auto scan for save paths when game exits
    "backup_on_exit": True,           # backup when game closes
    "backup_during_game": False,      # periodic backup while playing (per-game interval controls timing)
    "max_local_backups": 6,
    "backup_retention_days": 30,
    "min_kept_backups": 3,
    "max_backup_size_mb": 512,
    # The copies the save editor keeps before it writes: how many of one save
    # to hold on to, and for how long. Separate from the backup policy above,
    # which is about whole save folders rather than single edited files.
    "save_edit_copies": 3,
    "save_edit_copy_days": 7,
    "process_poll_interval": 1,
    "save_scan_debounce": 5,
    # Temporal correlation: claim a save-like write elsewhere on disk when it
    # lands within the window of a write to an already-known path of the
    # running game. Finds saves no name matching can (Ren'Py's roaming folder
    # is named after the game's INTERNAL title), but it infers from timing
    # alone, so it is opt-in — during a long session the false positives cost
    # more than the occasional catch.
    "save_correlation_enabled": False,
    "save_correlation_window_ms": 1000,
    # Periodic backup integrity check. Opens each archive and confirms every
    # member still passes its CRC — the cheap check that catches a truncated
    # upload or a half-written file, which otherwise surfaces only when a
    # restore is attempted. Runs in the background, well after startup.
    "backup_verify_enabled": True,
    "backup_verify_interval_days": 7,
    "backup_verify_last": "",      # ISO datetime of the last completed run
    # Periodic cloud config export — same encrypted payload as a manual
    # "export to provider", so library/settings are not lost between machines.
    "auto_export_config_enabled": False,
    "auto_export_config_interval_days": 7,
    "auto_export_config_last": "",  # ISO datetime of last successful/skipped run
    # GitHub Releases check. No auto-update (onefile build): notify only.
    # Interval is stored in seconds with jitter (~12 h ± 2 h) so every
    # install does not poll on the same clock.
    "check_for_updates": True,
    "update_check_last": "",
    "update_check_interval_sec": 0,
    "update_notified_version": "",
    # Pinned notes/images: only paths and window geometry. The files stay
    # where the player put them and are never copied into SaveSync.
    "pins_recent": {},             # game_id -> recently pinned files, newest first
    "pins_open": {},               # game_id -> re-pinned when that game runs
    "pins_geometry": {},           # path -> [x, y, w, h]
    "pins_opacity": {},            # path -> percent
    "save_folder_hints": list(SAVE_FOLDER_HINTS),  # sourced from constants to avoid duplication
    "extra_watch_paths": [],
    "sync_provider": None,             # deprecated: single provider (migrated to sync_providers)
    "sync_credentials": {},             # deprecated: legacy plaintext creds
    "sync_providers": [],               # list of active provider IDs
    "providers_connected": {},          # {pid: bool} - True after successful connection
    "provider_was_connected": False,    # deprecated: single-provider flag (migrated to providers_connected)
    "ignored_processes": [],
    "suppressed_overlay_apps": [],
    "machine_id": None,
    "sync_timeout": 120,            # timeout in seconds for sync provider operations
    "schema_version": 1,
    "auto_scan_confirmed_games": [],  # Games where auto-scan was confirmed
    "auto_scan_deselected_paths": {},  # Paths explicitly deselected by user {game_id: [paths]}
    "auto_scan_deleted_paths": {},     # Paths explicitly deleted by user {game_id: [paths]}
    # Answers to the "is this process really that game?" prompt, asked when a
    # process matched a library entry by NAME ONLY because its own path was
    # unreadable (elevated process). Keyed by process filename — the path is
    # exactly what's missing, so it cannot be part of the key.
    "confirmed_process_matches": {},   # {proc_name: game_id} — treat as that game
    "rejected_process_matches": {},    # {proc_name: [game_id]} — never that game
    # Per-game notification "don't re-prompt" choices. These MUST be registered
    # here: _load() drops any key not in _DEFAULTS (or starting with "sync_"),
    # so before this they behaved as session-only and reappeared every restart
    # — which also left the Settings → suppressed-games reset list empty.
    "suppressed_cloud_no_local": [],   # game_ids: skip "download cloud saves?" at launch
    "suppressed_ingame_notifs": {},    # {game_id: [...]}: in-game notification suppression
    "scan_auto_accept_games": {},      # {game_id: ...}: auto-accept the save scan at exit
    "last_cloud_config_hash": None,    # fingerprint of last imported cloud config
    "last_cloud_config_import": None,  # ISO datetime of last cloud config import
    "suppress_cloud_config_prompt": False,  # user chose "never ask" for cloud config
    "library_sort": "date_added",           # sort criterion for library page
    "library_sort_direction": "",           # "asc"/"desc" — empty = use criterion's natural default
    "library_folders": [],                  # [{name: str, color: str}, ...] user-defined folders
    # Folder tree vs tag/engine filter panes in the library sidebar (QSplitter sizes).
    "library_filter_splitter": [260, 215],
    # Unknown-game detections history — its OWN list, deliberately separate
    # from the backup/sync notification flow. Written only while
    # show_overlay_on_unknown is on; the hotkey opens this queue first when
    # non-empty. [{name, exe, ts}, ...] newest first.
    "unknown_game_history": [],
    # How many items each paginated list shows: {scope: int} (see
    # ui.widgets.page_size). Its companion holds the scope of a render that
    # was in progress when the app went down, which is how a page size too
    # big for the machine is undone instead of crashing on sight every time.
    "page_sizes": {},
    "page_size_render_guard": {},
}

# Configuration validation rules
_VALIDATION_RULES: dict[str, Callable] = {
    "max_local_backups": lambda x: isinstance(x, int) and 1 <= x <= 100,
    "backup_retention_days": lambda x: isinstance(x, int) and 1 <= x <= 365,
    "save_edit_copies": lambda x: isinstance(x, int) and 1 <= x <= 50,
    "save_edit_copy_days": lambda x: isinstance(x, int) and 1 <= x <= 365,
    "min_kept_backups": lambda x: isinstance(x, int) and 0 <= x <= 50,
    "process_poll_interval": lambda x: isinstance(x, (int, float)) and 1 <= x <= 60,
    "save_scan_debounce": lambda x: isinstance(x, (int, float)) and 1 <= x <= 30,
    "save_correlation_enabled": lambda x: isinstance(x, bool),
    "save_correlation_window_ms": lambda x: isinstance(x, int) and 100 <= x <= 10000,
    "backup_verify_enabled": lambda x: isinstance(x, bool),
    "backup_verify_interval_days": lambda x: isinstance(x, int) and 1 <= x <= 365,
    "auto_export_config_enabled": lambda x: isinstance(x, bool),
    "auto_export_config_interval_days": lambda x: isinstance(x, int) and 1 <= x <= 365,
    # A plain list is the pre-per-game shape; still accepted so an existing
    # config loads and is migrated on first read instead of being reset.
    "pins_recent": lambda x: (
        isinstance(x, list) and all(isinstance(p, str) for p in x)) or (
        isinstance(x, dict) and all(
            isinstance(v, list) and all(isinstance(p, str) for p in v)
            for v in x.values())),
    "pins_opacity": lambda x: isinstance(x, dict) and all(
        isinstance(v, int) and 0 <= v <= 100 for v in x.values()),
    "pins_open": lambda x: (
        isinstance(x, list) and all(isinstance(p, str) for p in x)) or (
        isinstance(x, dict) and all(
            isinstance(v, list) and all(isinstance(p, str) for p in v)
            for v in x.values())),
    "pins_geometry": lambda x: isinstance(x, dict) and all(
        isinstance(v, list) and len(v) == 4
        and all(isinstance(n, int) for n in v) for v in x.values()),
    "overlay_hotkey": lambda x: isinstance(x, str) and len(x.strip()) > 0,
    "unknown_game_history": lambda x: isinstance(x, list),
    "page_sizes": lambda x: isinstance(x, dict) and all(
        isinstance(v, int) and 1 <= v <= 500 for v in x.values()),
    "page_size_render_guard": lambda x: isinstance(x, dict),
    "library_filter_splitter": lambda x: (
        isinstance(x, (list, tuple)) and len(x) == 2
        and all(isinstance(n, int) and n >= 0 for n in x)),
    "language": lambda x: isinstance(x, str) and x in ["en", "it"],
    "theme": lambda x: isinstance(x, str) and x in ["dark", "light"],
    "launch_on_startup": lambda x: isinstance(x, bool),
    "minimize_to_tray": lambda x: isinstance(x, bool),
    "show_overlay_on_launch": lambda x: isinstance(x, bool),
    "show_overlay_on_unknown": lambda x: isinstance(x, bool),
    "show_overlay_on_cloud": lambda x: isinstance(x, bool),
    "show_overlay_on_backup": lambda x: isinstance(x, bool),
    "auto_sync_after_backup": lambda x: isinstance(x, bool),
    "auto_scan_on_exit": lambda x: isinstance(x, bool),
    "max_backup_size_mb": lambda x: isinstance(x, int) and 10 <= x <= 4096,
    "sync_timeout": lambda x: isinstance(x, (int, float)) and 10 <= x <= 600,
    "backup_on_exit": lambda x: isinstance(x, bool),
    "backup_during_game": lambda x: isinstance(x, bool),
    "auto_backup": lambda x: isinstance(x, bool),
}

_WRITE_DEBOUNCE_MS = 500   # batch writes within 500ms window
_MISSING = object()  # sentinel for get() default detection


class ConfigManager(QObject):
    config_changed = Signal(str, object)  # key, value

    def __init__(self):
        super().__init__()
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._io_lock = threading.Lock()  # protects _dirty and _writing
        # Debounced write timer — coalesces rapid sequential set() calls
        self._write_timer = QTimer(self)
        self._write_timer.setSingleShot(True)
        self._write_timer.setInterval(_WRITE_DEBOUNCE_MS)
        self._write_timer.timeout.connect(self._flush)
        self._writing = False  # prevent race condition during write
        self._load()

    def _load(self):
        # Deep-copy defaults so mutable values (lists, dicts) are independent
        # of the module-level _DEFAULTS and cannot be corrupted in-place.
        self._data = copy.deepcopy(_DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
                # Only merge keys that are still valid (ignore stale / renamed keys)
                for k, v in saved.items():
                    if k in _DEFAULTS or (k.startswith("sync_") and isinstance(v, (str, int, float, bool, list, dict, type(None)))):
                        self._data[k] = v
                # Migrate single-provider config to multi-provider
                old_pid = self._data.get("sync_provider")
                if old_pid and not self._data.get("sync_providers"):
                    self._data["sync_providers"] = [old_pid]
                    was = self._data.get("provider_was_connected", False)
                    pc = self._data.get("providers_connected", {})
                    if old_pid not in pc:
                        pc[old_pid] = was
                    self._data["providers_connected"] = pc
                    self._data["sync_provider"] = None
                    self._data["provider_was_connected"] = False
                    self._dirty = True
                    logger.info(f"Migrated single-provider config to multi-provider: {old_pid}")

                # Validate loaded values against rules
                for k in list(self._data.keys()):
                    if k in _VALIDATION_RULES and not _VALIDATION_RULES[k](self._data[k]):
                        logger.warning(f"Invalid config value for '{k}': {self._data[k]!r}, reverting to default")
                        default = _DEFAULTS.get(k)
                        if default is not None:
                            self._data[k] = default
                        else:
                            logger.warning(f"No default available for '{k}', keeping current value")
            except Exception as e:
                logger.error(f"Config load error: {e}")

    def _flush(self):
        """Actually write to disk (called by debounce timer or explicit save())."""
        with self._io_lock:
            if not self._dirty or self._writing:
                return
            self._writing = True
            self._dirty = False  # Clear before write; concurrent set() will re-dirty
            # Snapshot data under lock to avoid concurrent mutation during json.dump
            data_snapshot = copy.deepcopy(self._data)
        try:
            from core import atomic_replace as _atomic_replace
            tmp_path = CONFIG_FILE.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data_snapshot, f, indent=2, default=str)
            _atomic_replace(tmp_path, CONFIG_FILE)
            with self._io_lock:
                self._writing = False
                need_reschedule = self._dirty
            # Re-schedule outside the lock to avoid holding _io_lock
            # while invoking Qt methods (which could deadlock if a slot
            # tries to call set() on this ConfigManager).
            if need_reschedule:
                from PySide6.QtCore import QMetaObject, Qt
                try:
                    QMetaObject.invokeMethod(
                        self._write_timer, "start",
                        Qt.ConnectionType.QueuedConnection,
                    )
                except RuntimeError:
                    pass  # Qt objects torn down during shutdown
        except Exception as e:
            logger.error(f"Config save error: {e}")
            with self._io_lock:
                self._dirty = True  # Re-dirty so changes are retried
                self._writing = False
            try:
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(
                    self._write_timer, "start",
                    Qt.ConnectionType.QueuedConnection,
                )
            except Exception as timer_err:
                logger.error(f"Failed to reschedule config write timer: {timer_err}")
                # Fallback: attempt direct write since timer is unavailable
                try:
                    with self._io_lock:
                        fallback_snapshot = copy.deepcopy(self._data)
                    tmp_path = CONFIG_FILE.with_suffix(".tmp")
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(fallback_snapshot, f, indent=2, default=str)
                    _atomic_replace(tmp_path, CONFIG_FILE)
                except Exception:
                    pass
        except BaseException:
            # Ensure _writing is cleared even on KeyboardInterrupt / SystemExit
            with self._io_lock:
                self._writing = False
            raise

    def save(self):
        """Force immediate write (use sparingly - prefer debounced behaviour)."""
        from PySide6.QtCore import QMetaObject, Qt, QThread
        # Stop timer synchronously if we're on the GUI thread
        if QThread.currentThread() == self.thread():
            self._write_timer.stop()
        else:
            # From non-GUI thread: stop timer via QueuedConnection (non-blocking).
            # BlockingQueuedConnection could deadlock if the GUI thread is waiting
            # on a lock held by this thread.  A non-blocking stop is safe because
            # _flush() re-checks _dirty under _io_lock and will pick up changes.
            try:
                QMetaObject.invokeMethod(
                    self._write_timer, "stop",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                # Qt objects may be partially torn down during shutdown
                pass
        # Force dirty so _flush() actually writes even if timer already cleared it
        with self._io_lock:
            self._dirty = True
        self._flush()

    def get(self, key: str, default: Any = _MISSING) -> Any:
        with self._io_lock:
            if default is not _MISSING:
                val = self._data.get(key, default)
            else:
                val = self._data.get(key, _DEFAULTS.get(key))
            # Return copies of mutable types to prevent in-place mutation
            if isinstance(val, (dict, list)):
                return copy.deepcopy(val)
            return val

    def set(self, key: str, value: Any, persist: bool = True):
        if key in _VALIDATION_RULES:
            validator = _VALIDATION_RULES[key]
            if not validator(value):
                default = _DEFAULTS.get(key)
                if default is not None:
                    logger.warning(f"Invalid value for {key}: {value!r}, using default: {default!r}")
                    value = default
                else:
                    logger.error(f"Invalid value for {key}: {value!r} and no default available, ignoring set()")
                    return

        with self._io_lock:
            self._data[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
            # Snapshot value under lock to avoid emitting a stale value
            emit_value = copy.deepcopy(self._data[key]) if isinstance(self._data[key], (dict, list)) else self._data[key]
            if persist:
                self._dirty = True
                should_start = not self._writing
            else:
                should_start = False
        self.config_changed.emit(key, emit_value)
        if persist and should_start:
            # Marshal timer start to GUI thread
            from PySide6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(
                self._write_timer, "start",
                Qt.ConnectionType.QueuedConnection,
            )

    def get_all(self) -> dict:
        with self._io_lock:
            return copy.deepcopy(self._data)

_config: ConfigManager | None = None
_config_lock = threading.Lock()


def get_config() -> ConfigManager:
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("ConfigManager requires QApplication — create QApplication first")
                _config = ConfigManager()
    return _config
