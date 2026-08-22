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


def dialog_insert_budget_s() -> float:
    """How long a dialog may spend building list rows before it yields.

    A TIME budget rather than a row count, because what a row COSTS is a
    property of the machine: the same six rows are a blink on one PC and a
    visible stall on another, so a fixed count promises responsiveness on the
    machine it was tuned on and nothing on any other.

    The tier still matters on top of that, and not in the direction it first
    looks. A time budget already holds the stall itself constant — what it
    does not hold constant is the cost AROUND it, and that is what differs: a
    weak machine spends longer servicing the event loop between turns and
    repaints the list more slowly, so its budget is cut to keep the gap
    between two chances to press ✕ near the same place. A capable machine has
    the opposite problem — its round trips are nearly free and its rows are
    cheap, so a small budget spends most of the work going round the loop
    rather than building anything.
    """
    tier = _tier()
    if tier == "high":
        return 0.060
    if tier == "mid":
        return 0.040
    return 0.024


def dialog_insert_min_rows() -> int:
    """Rows to build per turn regardless of the budget above.

    A floor, so a machine slow enough that one row outlasts the whole budget
    still makes visible progress instead of inching forward one row per
    event-loop turn.
    """
    tier = _tier()
    if tier == "high":
        return 24
    if tier == "mid":
        return 8
    return 3


def config_write_debounce_ms() -> int:
    """Debounce for config.json / library.json writes."""
    tier = _tier()
    if tier == "high":
        return 500
    if tier == "mid":
        return 1000
    return 2000


# ── Background upkeep: polling, memory sweeps, idle release ─────────────────
# Three consumers that used to each invent their own numbers, now reading the
# same tier — but NOT the same multiplier. Their directions genuinely differ,
# and one shared knob applied uniformly would get at least one backwards:
#
#   process polling      weak machine → poll LESS often   (spend less CPU)
#   memory sweeps        weak machine → sweep MORE often  (give RAM back sooner)
#   idle doc release     weak machine → release SOONER    (hold less)
#
# Free RAM is deliberately kept out of the first and third: those decide how
# a machine behaves in general. It drives only the deep sweep, which is the
# one that should react to what is happening right now — the same split the
# module docstring draws for backup/sync inflight.


def process_poll_multiplier() -> float:
    """Scale factor for the process-monitor poll interval.

    Applied ON TOP of the user's ``process_poll_interval``, never instead of
    it: that is a visible 1–60s setting, and silently overriding it would
    make the spin box look broken.

    Measured on a "high" machine: a steady-state poll costs ~3.4 ms, so even
    1 s polling is ~0.3% of one core — nothing to reclaim there. The cost
    that matters is a poll that has to resolve NEW processes (~600 ms cold),
    and weak machines pay far more for it, which is what stretching the
    interval for them buys.
    """
    tier = _tier()
    if tier == "high":
        return 1.0
    if tier == "mid":
        return 1.5
    return 2.5


def memory_sweep_interval_s() -> int:
    """FLOOR for the cheap idle sweep (seconds) — how soon it may run again
    after one that actually reclaimed something.

    Cheap means it drops dead registry entries and idle watcher indices and
    nothing else — no cache purge, no gc pass, no working-set trim — so a
    weak machine can afford it more often, not less.

    This is a floor, not a schedule: the caller backs off towards
    memory_sweep_max_interval_s() while sweeps keep finding nothing.
    """
    tier = _tier()
    if tier == "high":
        return 60
    if tier == "mid":
        return 45
    return 30


def memory_sweep_max_interval_s() -> int:
    """CEILING the sweep backs off to when it keeps finding nothing.

    Repeating a cleanup that reclaims nothing is pure cost — it wakes the
    process, touches its structures and buys nothing — so a quiet app should
    drift towards checking rarely rather than keep paying every minute. The
    ceiling is lower on a weak machine, which still wants to notice sooner
    that there IS something to give back.
    """
    tier = _tier()
    if tier == "high":
        return 15 * 60
    if tier == "mid":
        return 10 * 60
    return 6 * 60


def deep_sweep_after_sweeps() -> int:
    """Cheap sweeps between two DEEP ones (cache purge + gc + working-set).

    Deliberately NOT shortened for weak machines, which is the one place the
    "weak → do it sooner" rule inverts. A deep sweep throws away decoded
    covers and hands the working set back, so the next interaction re-decodes
    and re-faults — precisely the stutter a weak machine can least afford.
    Real memory pressure triggers one early instead (see memory_pressure).
    """
    tier = _tier()
    if tier == "high":
        return 10
    if tier == "mid":
        return 12
    return 16


def memory_pressure() -> str:
    """``critical`` | ``tight`` | ``ok`` from CURRENT free RAM.

    The one signal here that reads live memory rather than the stable tier:
    it answers "should the expensive sweep run NOW", which is a question
    about this moment, not about the machine.
    """
    total_gb, avail_gb = _ram_gb()
    if not avail_gb:
        return "ok"                      # psutil missing — never guess tight
    if avail_gb < 0.75:
        return "critical"
    if avail_gb < 1.5 or (total_gb and avail_gb / total_gb < 0.10):
        return "tight"
    return "ok"


def idle_document_release_s() -> int:
    """Idle time before the save editor lets a loaded document go (seconds).

    A loaded save holds the original bytes plus the parsed structure. Ten
    minutes was the fixed rule for every machine; the tier keeps that for a
    mid one and moves the ends apart.
    """
    tier = _tier()
    if tier == "high":
        return 15 * 60
    if tier == "mid":
        return 10 * 60
    return 5 * 60


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
    logger.info(
        "Adaptive upkeep: poll=x%.1f sweep=%s..%ss deep=every %s sweeps "
        "idle_release=%smin pressure=%s",
        process_poll_multiplier(), memory_sweep_interval_s(),
        memory_sweep_max_interval_s(), deep_sweep_after_sweeps(),
        idle_document_release_s() // 60, memory_pressure(),
    )
