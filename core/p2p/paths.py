"""SaveSync - where P2P-received hand-picked files are put.

A file or folder someone sends by hand is downloaded straight into this
folder, laid out as the sender had it (see core.p2p.transfer.plan_targets for
how a name that is taken is handled) — it is never staged somewhere else and
copied over afterwards, so a 50 GB transfer needs 50 GB of room, not 100.
"""
from pathlib import Path


def default_files_dir() -> Path:
    """SaveSync's own folder for them, used until the person picks another."""
    from core.constants import USER_DATA_DIR
    return USER_DATA_DIR / "p2p_files"


def files_dir() -> Path:
    """The folder chosen in Settings, or the default when none is."""
    try:
        from core.config_manager import get_config
        chosen = str(get_config().get("p2p_files_dir", "") or "").strip()
    except Exception:
        chosen = ""
    return Path(chosen).expanduser() if chosen else default_files_dir()
