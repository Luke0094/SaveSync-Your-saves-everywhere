"""
SaveSync - Machine Identity Manager
Generates a stable machine fingerprint to detect cross-machine conflicts.
"""
import hashlib
import platform
import uuid
import logging

from core.constants import MACHINE_ID_FILE, MACHINE_FIELDS

logger = logging.getLogger(__name__)


def _generate_machine_id() -> str:
    """Create a stable fingerprint from hardware/OS info."""
    mac = uuid.getnode()
    # Check if the multicast bit is set (bit 0 of first octet), which indicates
    # a random MAC address rather than a real hardware MAC.
    if mac & 0x010000000000:
        # Random MAC — fall back to hostname + platform info instead
        mac_str = f"{platform.node()}|{platform.system()}|{platform.machine()}"
    else:
        mac_str = str(mac)

    # Use MACHINE_FIELDS to determine which platform attributes to include
    _field_getters = {
        "node": platform.node,
        "processor": platform.processor,
        "machine": platform.machine,
        "system": platform.system,
    }
    parts = [_field_getters[f]() for f in MACHINE_FIELDS if f in _field_getters]
    parts.append(mac_str)
    raw = "|".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_machine_id() -> str:
    """Return (or generate and cache) the machine ID."""
    if MACHINE_ID_FILE.exists():
        try:
            mid = MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
            if len(mid) == 32:
                return mid
        except Exception:
            pass
    mid = _generate_machine_id()
    tmp_path = MACHINE_ID_FILE.with_name(MACHINE_ID_FILE.name + ".tmp")
    try:
        from core import atomic_replace as _atomic_replace
        tmp_path.write_text(mid, encoding="utf-8")
        _atomic_replace(tmp_path, MACHINE_ID_FILE)
    except Exception as e:
        logger.warning(f"Could not persist machine ID: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return mid
