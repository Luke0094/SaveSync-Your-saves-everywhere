"""
SaveSync - In-process regression guards.

The packaged / day-to-day tree does not ship a ``tests/`` suite. Checks that
must keep holding (config-history restore vs rotation, …) run here, on a
background thread after startup, and surface failures the same way as the
scheduled backup integrity sweep (status bar + tray).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False


def run_startup_self_checks(
    on_failure: Optional[Callable[[str, str], None]] = None,
    on_done: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    """Run all guards once. *on_failure(check_id, detail)* is called on the
    worker thread — callers should marshal to the GUI via a Qt signal.
    *on_done()* is called on the same thread when the checks have actually
    completed (success or failure), so callers can record a last-run time
    without racing the background work. *on_progress(check_id, index, total)*
    is called on the same thread right before each check starts, so the GUI
    can surface the sweep in the sidebar instead of appearing frozen."""
    global _running
    with _lock:
        if _running:
            return
        _running = True

    def _work():
        global _running
        try:
            from core.config_transfer import self_check_config_history_restore
            if on_progress:
                on_progress("config_history_restore", 1, 2)
            ok, detail = self_check_config_history_restore()
            if ok:
                logger.info("Self-check ok: config_history_restore")
            else:
                logger.error("Self-check FAILED: config_history_restore — %s", detail)
                if on_failure:
                    on_failure("config_history_restore", detail or "failed")
        except Exception as e:
            logger.error("Self-check crashed: config_history_restore — %s", e)
            if on_failure:
                try:
                    on_failure("config_history_restore", str(e)[:200])
                except Exception:
                    pass
        try:
            # Index construction: legacy backups (pre per-file manifests) carry
            # no manifest/save_hash, so the mtime preflight and content-hash
            # dedup treat the game as permanently changed → Backup Tutti
            # re-creates backups that never changed. Repair = rebuild that
            # metadata from the zip (autofix), like the manual "Verifica
            # Backup" does per entry.
            from core.backup import get_backup_manager
            if on_progress:
                on_progress("backup_index", 2, 2)
            repaired, failed = get_backup_manager().repair_legacy_backups()
            if failed:
                logger.error(
                    "Self-check FAILED: backup_index — %d backups could not be "
                    "repaired", failed)
                if on_failure:
                    on_failure("backup_index", f"{failed} backups unreadable")
            elif repaired:
                logger.info(
                    "Self-check ok: backup_index — %d legacy backups repaired",
                    repaired)
            else:
                logger.info("Self-check ok: backup_index")
        except Exception as e:
            logger.error("Self-check crashed: backup_index — %s", e)
            if on_failure:
                try:
                    on_failure("backup_index", str(e)[:200])
                except Exception:
                    pass
        finally:
            with _lock:
                _running = False
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=_work, name="savesync-self-checks", daemon=True).start()
