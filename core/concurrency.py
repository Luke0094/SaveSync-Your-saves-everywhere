"""
SaveSync - Adaptive limits for heavy batch I/O and UI work.

Derived from logical CPU count and available RAM (psutil when present).
Weak machines get conservative caps; capable ones are not artificially
slowed — artificial pauses/chunking there cost more than they help.

The capability *tier* used for UI / verify / debounce is based on stable
signals (CPU count + total RAM) and cached briefly so a momentary free-RAM
dip (game launch, antivirus) does not flip library chunking on and off.
Backup/Sync inflight still honour *current* free RAM and drop to 1 when
the machine is critically short of memory.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# How long a stable tier answer is reused. Long enough to ride out a game
# launch / GC spike; short enough that adding RAM or closing heavy apps
# is noticed within a minute.
_TIER_CACHE_S = 45.0
_tier_cached_until: float = 0.0
_tier_cached_value: str = ""


def _cpu_count() -> int:
    return max(1, int(os.cpu_count() or 1))


def _ram_gb() -> tuple[float, float]:
    """Return (total_gb, available_gb). Zeros when unknown."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / (1024 ** 3), vm.available / (1024 ** 3)
    except Exception:
        return 0.0, 0.0


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _compute_stable_tier() -> str:
    """Capability band from CPU + total RAM only (not free RAM)."""
    cpu = _cpu_count()
    total_gb, _avail_gb = _ram_gb()
    # Without RAM info (psutil missing), never promote to "high" — a
    # many-core VPS/container with little memory is common on Linux.
    if not total_gb:
        if cpu >= 4:
            return "mid"
        return "low"
    if total_gb <= 8.0:
        return "low" if cpu < 4 else "mid"
    if cpu >= 8 and total_gb >= 12.0:
        return "high"
    if cpu >= 4:
        return "mid"
    return "low"


def _tier() -> str:
    """Coarse capability band: ``high`` | ``mid`` | ``low``.

    Cached for ``_TIER_CACHE_S`` so callers that hit this on every library
    rebuild or verify step do not oscillate when free RAM blips.
    """
    global _tier_cached_until, _tier_cached_value
    now = time.monotonic()
    if _tier_cached_value and now < _tier_cached_until:
        return _tier_cached_value
    value = _compute_stable_tier()
    _tier_cached_value = value
    _tier_cached_until = now + _TIER_CACHE_S
    return value


def backup_max_inflight() -> int:
    """How many game backups may run at once."""
    cpu = _cpu_count()
    total_gb, avail_gb = _ram_gb()
    tier = _tier()
    n = max(1, cpu // 2)
    # Ceiling scales with the machine — do not hard-cap every PC at 4.
    hi = 8 if tier == "high" else (4 if tier == "mid" else 2)
    n = _clamp(n, 1, hi)
    # Live free-RAM check: during a heavy game we still want to back off,
    # even if the stable tier stays "high".
    if avail_gb and avail_gb < 1.5:
        n = 1
    elif total_gb and total_gb <= 8.0:
        n = min(n, 2)
    return n


def sync_max_inflight() -> int:
    """How many sync workers may run at once (backup + network per job)."""
    cpu = _cpu_count()
    total_gb, avail_gb = _ram_gb()
    tier = _tier()
    n = max(1, cpu // 3)
    hi = 4 if tier == "high" else (2 if tier == "mid" else 1)
    n = _clamp(n, 1, hi)
    if avail_gb and avail_gb < 1.5:
        n = 1
    elif total_gb and total_gb <= 8.0:
        n = min(n, 1)
    return n


def verify_throttle_s() -> float:
    """Pause between CRC checks in a verify sweep. 0 = no artificial delay."""
    tier = _tier()
    if tier == "high":
        return 0.0
    if tier == "mid":
        return 0.02
    return 0.08


def library_insert_chunk_size() -> int:
    """How many library cards/rows to build per event-loop turn.

    ``0`` means build the whole page synchronously (fast machines): chunking
    would only stretch the load and keep the busy sheet up longer.
    """
    tier = _tier()
    if tier == "high":
        return 0
    if tier == "mid":
        return 16
    return 6


def config_write_debounce_ms() -> int:
    """Debounce for config.json / library.json writes."""
    tier = _tier()
    if tier == "high":
        return 500
    if tier == "mid":
        return 1000
    return 2000


def log_limits() -> None:
    total_gb, avail_gb = _ram_gb()
    logger.info(
        "Adaptive limits: tier=%s backup=%s sync=%s verify_throttle=%.0fms "
        "lib_chunk=%s debounce=%sms (cpu=%s ram=%.1f/%.1f GB, tier_cache=%.0fs)",
        _tier(), backup_max_inflight(), sync_max_inflight(),
        verify_throttle_s() * 1000, library_insert_chunk_size(),
        config_write_debounce_ms(),
        _cpu_count(), avail_gb, total_gb, _TIER_CACHE_S,
    )
