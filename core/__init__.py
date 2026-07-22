# SaveSync core package
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)


def to_local_dt(value):
    """Parse an ISO timestamp (str or datetime) and return an AWARE datetime
    converted to the user's LOCAL timezone, or None if unparseable.

    SaveSync stores several timestamps (notably BackupEntry.created_at) as
    NAIVE UTC — a bare ``fromisoformat`` + "convert only if tzinfo" check
    therefore displayed raw UTC as if it were local time. Naive inputs are
    treated as UTC here, aware inputs are converted as-is: every display
    site must go through this helper.
    """
    from datetime import datetime, timezone
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def fmt_size(n) -> str:
    """Human-readable byte size ("618 B", "3.4 KB", "1.25 GB").

    The single canonical formatter — every size shown in the UI (backup
    rows, file lists, restore dialog, size_human) goes through here so
    the whole app renders sizes identically."""
    if n < 1024:        return f"{int(n)} B"
    if n < 1024**2:     return f"{n/1024:.1f} KB"
    if n < 1024**3:     return f"{n/1024**2:.1f} MB"
    if n < 1024**4:     return f"{n/1024**3:.2f} GB"
    return f"{n/1024**4:.2f} TB"


def atomic_replace(src: Path, dst: Path, retries: int = 3, delay: float = 0.1) -> None:
    """Replace *dst* with *src* atomically, retrying on Windows lock errors.

    On Windows, Path.replace() can fail with PermissionError / OSError if
    another process (antivirus, indexer) holds a transient lock on the
    destination file.  This helper retries a few times with a short sleep.
    """
    for attempt in range(retries):
        try:
            src.replace(dst)
            return
        except OSError as e:
            if attempt < retries - 1:
                _log.debug(f"atomic_replace: attempt {attempt + 1} failed for {dst}: {e}, retrying...")
                time.sleep(delay)
            else:
                _log.error(f"atomic_replace: all {retries} attempts failed for {dst}: {e}")
                raise


def is_relative_to(path: Path, base: Path) -> bool:
    """Compatibility wrapper for Path.is_relative_to (Python 3.9+).

    Uses string comparison on resolved paths as the fallback, which is more
    robust on Windows where Path.relative_to() may fail due to case differences.
    """
    try:
        return path.is_relative_to(base)
    except AttributeError:
        # Python < 3.9 fallback — use string comparison (handles case on Windows)
        base_str = str(base.resolve())
        path_str = str(path.resolve())
        return path_str == base_str or path_str.startswith(base_str + os.sep)
