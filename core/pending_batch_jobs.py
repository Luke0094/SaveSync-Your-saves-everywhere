"""
SaveSync - Persist unfinished batch jobs across restarts.

Covers Backup Tutti, Sync Tutti, Aggiunta multipla, and batch web search so a
crash or quit can resume without redoing completed work.
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from typing import Any, Optional

from core.constants import USER_DATA_DIR

logger = logging.getLogger(__name__)

_PATH = USER_DATA_DIR / "pending_batch_jobs.json"
_lock = threading.Lock()
# In-memory mirror so mark_game_done(persist=False) does not re-read a stale
# file and wipe progress between throttled disk flushes.
_cache: Optional[dict] = None

KEY_BACKUP_ALL = "backup_all"
KEY_SYNC_ALL = "sync_all"
KEY_MANUAL_BATCH = "manual_batch"
KEY_SEARCH_BATCH = "search_batch"


def _load_disk() -> dict:
    try:
        if _PATH.is_file():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"Failed to load pending batch jobs: {e}")
    return {}


def _ensure_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = _load_disk()
    return _cache


def _write_disk(data: dict) -> None:
    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        # Compact JSON: Aggiunta multipla can persist hundreds of folder
        # entries; pretty-print made every mid-batch write a GUI stall.
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(_PATH)
    except Exception as e:
        logger.warning(f"Failed to save pending batch jobs: {e}")


def _save(data: dict, *, persist: bool = True) -> None:
    global _cache
    _cache = data
    if not persist:
        return
    if data:
        _write_disk(data)
    elif _PATH.is_file():
        try:
            _PATH.unlink()
        except OSError:
            _write_disk({})


def get_job(key: str) -> Optional[dict]:
    with _lock:
        data = _ensure_cache()
        job = data.get(key)
        return deepcopy(job) if isinstance(job, dict) else None


def set_job(key: str, job: Optional[dict]) -> None:
    with _lock:
        data = _ensure_cache()
        if job is None:
            data.pop(key, None)
        else:
            data[key] = job
        _save(data, persist=True)


def update_job(key: str, **fields: Any) -> Optional[dict]:
    with _lock:
        data = _ensure_cache()
        job = data.get(key)
        if not isinstance(job, dict):
            return None
        job.update(fields)
        data[key] = job
        _save(data, persist=True)
        return deepcopy(job)


def clear_job(key: str) -> None:
    set_job(key, None)


def flush() -> None:
    """Force the in-memory cache to disk (end of a throttled Sync Tutti)."""
    with _lock:
        data = _ensure_cache()
        _save(data, persist=True)


def mark_game_done(key: str, game_id: str, *, persist: bool = True) -> Optional[dict]:
    """Move *game_id* from pending_ids to completed_ids. Clears job when empty.

    *persist*=False updates the in-memory snapshot only (Sync/Backup Tutti
    throttle disk writes so the GUI stays responsive).
    """
    with _lock:
        data = _ensure_cache()
        job = data.get(key)
        if not isinstance(job, dict):
            return None
        pending = [g for g in (job.get("pending_ids") or []) if g != game_id]
        completed = list(job.get("completed_ids") or [])
        if game_id and game_id not in completed:
            completed.append(game_id)
        job["pending_ids"] = pending
        job["completed_ids"] = completed
        if not pending:
            data.pop(key, None)
            _save(data, persist=True)  # always flush when the batch ends
            return None
        data[key] = job
        _save(data, persist=persist)
        return deepcopy(job)
