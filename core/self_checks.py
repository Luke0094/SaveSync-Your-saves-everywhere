"""
SaveSync - Data-integrity checks.

ONE list of checks, run from exactly TWO places:

- automatically, every N days (``self_checks_frequency``), from the main
  window's scheduler;
- on demand, from the ⚕️ button on the Backups page.

They used to be three different lists. The automatic sweep repaired legacy
backup metadata but never opened an archive; the manual button opened every
archive but never repaired anything; and the zip-existence sweep was a third
thing again, on its own retrying thread at every launch, reachable from
neither. So "run the checks" meant something different depending on where you
asked from, and the answer the user got depended on which button they found —
which is the opposite of what a diagnostic is for.

Adding a check here now adds it to both entry points at once.

The checks, in the order they run (each depends on the previous having
cleaned up after itself):

    backup_index_zips      drop index entries whose zip is gone
    backup_index           rebuild manifests/hashes missing on legacy backups
    backup_archives        open every archive and CRC its contents
    config_history_restore config-snapshot restore guard

Everything runs on one worker thread; callbacks fire there, so GUI callers
must marshal to their own thread (a Qt signal).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False
# The cancel flag of the run in flight. One run at a time is already enforced
# by _running, so one event is enough — and keeping it here means neither
# entry point has to carry it around to reach a Cancel button.
_cancel_event: Optional[threading.Event] = None

# Ordered once, here. The GUI shows these ids as progress labels.
CHECK_IDS = (
    "backup_index_zips",
    "backup_index",
    "backup_archives",
    "config_history_restore",
)


@dataclass
class CheckResult:
    """What a run found. Both entry points render their message from this."""
    failures: list[tuple[str, str]] = field(default_factory=list)
    # backup_index_zips
    removed_ids: list[str] = field(default_factory=list)
    # backup_index
    repaired: int = 0
    # backup_archives
    archives_bad: int = 0
    archives_total: int = 0
    # config_history_restore
    snapshots_ok: bool = True
    snapshots_detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures


def is_running() -> bool:
    """True while a run is in flight — both entry points refuse to stack."""
    with _lock:
        return _running


def request_cancel() -> bool:
    """Ask the run in flight to stop. False when there is nothing to stop.

    Cooperative: the runner checks between checks, and the archive pass
    threads the same event into verify_backups, which checks between zips.
    So a cancel lands within one archive rather than at the end of 679 of
    them — which is the whole reason the sweep needs a Cancel button.
    """
    with _lock:
        event = _cancel_event
    if event is None:
        return False
    event.set()
    logger.info("Integrity checks: cancellation requested")
    return True


def run_checks(
    *,
    backup_ids: Optional[list[str]] = None,
    skip_recent_hours: float = 0.0,
    on_failure: Optional[Callable[[str, str], None]] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    on_backup_result: Optional[Callable[[str, str, str], None]] = None,
    on_done: Optional[Callable[[CheckResult], None]] = None,
    cancel=None,
) -> bool:
    """Run every check once on a worker thread. False if one is already going.

    *backup_ids* scopes the per-archive CRC pass — the Backups page passes the
    listed game's backups, the scheduler passes nothing and gets all of them.
    The index-level checks are always global: they are about the index, not
    about a selection.

    *skip_recent_hours* reuses an "ok" verdict newer than that instead of
    re-opening the archive. The scheduled sweep leans on it; a user who
    clicked the button asked for a real answer, and passes 0.

    Callbacks run on the WORKER thread: *on_failure(check_id, detail)*,
    *on_progress(check_id, index, total)* before each check,
    *on_backup_result(backup_id, state, detail)* per archive, and
    *on_done(CheckResult)* once at the end (success or failure).
    """
    global _running, _cancel_event
    with _lock:
        if _running:
            return False
        _running = True
        # A caller may bring its own; otherwise make one so request_cancel()
        # always has something to set.
        _cancel_event = cancel if cancel is not None else threading.Event()
    cancel = _cancel_event

    def _work():
        global _running, _cancel_event
        result = CheckResult()

        def _fail(check_id: str, detail: str):
            result.failures.append((check_id, detail))
            if on_failure:
                try:
                    on_failure(check_id, detail)
                except Exception:
                    logger.debug("on_failure callback raised", exc_info=True)

        def _cancelled() -> bool:
            return cancel is not None and cancel.is_set()

        total = len(CHECK_IDS)
        for index, check_id in enumerate(CHECK_IDS, 1):
            if _cancelled():
                logger.info("Checks cancelled before %s", check_id)
                break
            if on_progress:
                try:
                    on_progress(check_id, index, total)
                except Exception:
                    logger.debug("on_progress callback raised", exc_info=True)
            try:
                _RUNNERS[check_id](result, _fail, backup_ids,
                                   skip_recent_hours, on_backup_result, cancel)
            except Exception as e:
                logger.error("Check crashed: %s — %s", check_id, e)
                _fail(check_id, str(e)[:200])

        with _lock:
            _running = False
            _cancel_event = None
        if on_done:
            try:
                on_done(result)
            except Exception:
                logger.debug("on_done callback raised", exc_info=True)

    threading.Thread(target=_work, name="savesync-checks", daemon=True).start()
    return True


# ── The checks ──────────────────────────────────────────────────────────────
# Each takes the shared result plus the run's parameters and records what it
# found. Raising is allowed — run_checks turns it into a failure entry.

def _check_backup_index_zips(result, fail, backup_ids, skip_recent_hours,
                             on_backup_result, cancel):
    """Index rows whose zip is gone (deleted outside SaveSync, failed sync)
    are dropped. First, because every later check would otherwise spend its
    time on archives that are not there."""
    from core.backup import get_backup_manager
    removed, error = get_backup_manager().validate_index_zips()
    result.removed_ids = list(removed)
    if error:
        fail("backup_index_zips", error)
    elif removed:
        logger.info("Check ok: backup_index_zips — %d stale entries dropped",
                    len(removed))
    else:
        logger.info("Check ok: backup_index_zips")


def _check_backup_index(result, fail, backup_ids, skip_recent_hours,
                        on_backup_result, cancel):
    """Legacy backups (pre per-file manifests) carry no manifest/save_hash, so
    the mtime preflight and content-hash dedup treat the game as permanently
    changed → "Backup Tutti" re-creates backups that never changed. Repair
    rebuilds that metadata from the zip."""
    from core.backup import get_backup_manager
    repaired, failed = get_backup_manager().repair_legacy_backups()
    result.repaired = repaired
    if failed:
        fail("backup_index", f"{failed} backups unreadable")
    elif repaired:
        logger.info("Check ok: backup_index — %d legacy backups repaired",
                    repaired)
    else:
        logger.info("Check ok: backup_index")


def _check_backup_archives(result, fail, backup_ids, skip_recent_hours,
                           on_backup_result, cancel):
    """Open each archive and CRC its members — the only check that proves a
    backup can actually be restored. Results are written into the index, so
    the per-backup health dots pick them up without a second pass."""
    from core.backup import get_backup_manager
    mgr = get_backup_manager()
    ids = (list(backup_ids) if backup_ids is not None
           else [b.backup_id for b in mgr.get_all_backups()])
    result.archives_total = len(ids)
    if not ids:
        logger.info("Check ok: backup_archives — nothing to verify")
        return

    bad = [0]

    def _one(bid: str, state: str, detail: str):
        if state != "ok":
            bad[0] += 1
        if on_backup_result:
            try:
                on_backup_result(bid, state, detail)
            except Exception:
                logger.debug("on_backup_result callback raised", exc_info=True)

    mgr.verify_backups(ids, deep=False, on_one=_one, cancel=cancel,
                       skip_recent_hours=skip_recent_hours)
    result.archives_bad = bad[0]
    if bad[0]:
        fail("backup_archives", f"{bad[0]}/{len(ids)} archives unhealthy")
    else:
        logger.info("Check ok: backup_archives — %d verified", len(ids))


def _check_config_history_restore(result, fail, backup_ids, skip_recent_hours,
                                  on_backup_result, cancel):
    """Config-history checkpoints must still restore over a rotated history —
    same data-integrity story as the archives, for settings instead of saves."""
    from core.config_transfer import self_check_config_history_restore
    ok, detail = self_check_config_history_restore()
    result.snapshots_ok = ok
    result.snapshots_detail = detail or ""
    if ok:
        logger.info("Check ok: config_history_restore")
    else:
        fail("config_history_restore", detail or "failed")


_RUNNERS = {
    "backup_index_zips": _check_backup_index_zips,
    "backup_index": _check_backup_index,
    "backup_archives": _check_backup_archives,
    "config_history_restore": _check_config_history_restore,
}
