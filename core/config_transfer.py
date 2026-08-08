"""
SaveSync - Configuration Transfer
Export/import full config (settings + library + credentials) between machines.
Supports local file export and cloud-based config sync.
"""
import base64
import hashlib
import json
import logging
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.library import GameEntry

from core.constants import (
    USER_DATA_DIR, CONFIG_FILE, LIBRARY_FILE, CONFIG_HISTORY_DIR,
)

logger = logging.getLogger(__name__)

CONFIG_TRANSFER_VERSION = 2
CLOUD_CONFIG_PATH = "SaveSync/savesync_config"
MAX_CONFIG_HISTORY = 10

# Keys that are machine-specific and should NOT be exported
_MACHINE_SPECIFIC_KEYS = frozenset({
    "machine_id",
    "last_cloud_config_hash",
    "last_cloud_config_import",
    "suppress_cloud_config_prompt",
    # Per-machine schedule stamps — each install tracks its own last run.
    "auto_export_config_last",
    "backup_verify_last",
    # Note: suppressed_overlay_apps and ignored_processes ARE exported so the
    # blocklist roams with the user.  On a machine where a path doesn't exist
    # it simply never matches — no harm done.
})


# ── Export ──────────────────────────────────────────────────────────────────

def _config_content_hash(settings: dict, library: list, creds_export) -> str:
    """Hash the meaningful config content (ignoring timestamps/machine info)."""
    content = json.dumps({"s": settings, "l": library, "c": creds_export},
                         sort_keys=True, default=str)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Last exported config hash — used to skip redundant exports
_last_export_hash: str = ""
_export_hash_lock = threading.Lock()


def export_config(include_credentials: bool = True,
                  skip_if_unchanged: bool = False) -> bytes | None:
    """Export full config as encrypted bytes (internal key, no passphrase).

    Returns None if *skip_if_unchanged* is True and config hasn't changed.
    """
    global _last_export_hash

    from core.config_manager import get_config
    from core.library import get_library
    from core.machine import get_machine_id

    all_settings = get_config().get_all()
    settings = {k: v for k, v in all_settings.items()
                if k not in _MACHINE_SPECIFIC_KEYS}

    library = [g.to_dict() for g in get_library().all_games()]

    creds_export = None
    if include_credentials:
        try:
            from core.credentials import get_credential_store
            creds_export = get_credential_store().export_credentials()
        except Exception as e:
            logger.warning(f"Could not export credentials: {e}")

    if skip_if_unchanged:
        current_hash = _config_content_hash(settings, library, creds_export)
        with _export_hash_lock:
            if current_hash == _last_export_hash:
                return None
            _last_export_hash = current_hash

    payload = {
        "version": CONFIG_TRANSFER_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "machine_id": get_machine_id(),
        "machine_name": platform.node(),
        "settings": settings,
        "library": library,
        "credentials_export": creds_export,
    }

    raw = json.dumps(payload, default=str).encode("utf-8")
    key = _derive_transfer_key()

    from core.credentials import encrypt_data
    return encrypt_data(raw, key)


def export_config_to_file(file_path: Path,
                          include_credentials: bool = True,
                          skip_if_unchanged: bool = False) -> bool | None:
    """Export config to a .savesync file.

    Returns True on success, False on error, None if skipped (unchanged).
    """
    try:
        encrypted = export_config(include_credentials,
                                  skip_if_unchanged=skip_if_unchanged)
        if encrypted is None:
            logger.info("Config file export skipped — no changes")
            return None
        encoded = base64.b64encode(encrypted)
        from core import atomic_replace as _atomic_replace
        tmp = file_path.with_suffix(".tmp")
        tmp.write_bytes(encoded)
        _atomic_replace(tmp, file_path)
        return True
    except Exception as e:
        logger.error(f"Config export failed: {e}")
        return False


# ── Import ──────────────────────────────────────────────────────────────────

def import_config_from_bytes(data: bytes) -> dict:
    """Decrypt and parse config export. Returns parsed dict.

    Raises ValueError on corrupt data.
    """
    key = _derive_transfer_key()
    from core.credentials import decrypt_data
    try:
        raw = decrypt_data(data, key)
    except Exception:
        raise ValueError("Corrupted or incompatible config data")

    parsed = json.loads(raw.decode("utf-8"))
    version = parsed.get("version", 1)
    if version > CONFIG_TRANSFER_VERSION:
        logger.warning(f"Config version {version} > supported {CONFIG_TRANSFER_VERSION}")
    return parsed


def import_config_from_file(file_path: Path) -> dict:
    """Read and decrypt a .savesync file.

    Raises ValueError on corrupt data or invalid file format.
    """
    try:
        encoded = file_path.read_bytes()
        encrypted = base64.b64decode(encoded)
    except Exception as e:
        raise ValueError(f"Invalid config file: {e}") from e
    return import_config_from_bytes(encrypted)


def preview_import(parsed: dict) -> dict:
    """Analyse parsed config and return a summary for the UI."""
    from core.config_manager import get_config
    from core.library import get_library

    current_settings = get_config().get_all()
    current_games = {g.name.lower(): g for g in get_library().all_games()}

    imported_settings = parsed.get("settings", {})
    imported_library = parsed.get("library", [])

    # Settings diff
    settings_diff = []
    for k, v in imported_settings.items():
        if k in _MACHINE_SPECIFIC_KEYS:
            continue
        if current_settings.get(k) != v:
            settings_diff.append(k)

    # Library analysis
    games_new = []
    games_existing = []
    games_invalid_paths = []
    for gd in imported_library:
        name = gd.get("name", "")
        name_lower = name.lower()
        if name_lower in current_games:
            games_existing.append(name)
        else:
            games_new.append(name)
        # Check paths
        exe = gd.get("exe_path", "")
        saves = gd.get("save_paths", [])
        has_valid = False
        if exe and Path(exe).exists():
            has_valid = True
        from core.registry_saves import is_registry_path as _is_reg
        for sp in saves:
            # Registry entries count as valid: they are user-relative
            # pointers, meaningful on any machine.
            if sp and (_is_reg(sp) or Path(sp).exists()):
                has_valid = True
                break
        if not has_valid and (exe or saves):
            games_invalid_paths.append(name)

    is_identical = (not settings_diff and not games_new
                    and not parsed.get("credentials_export"))

    return {
        "source_machine": parsed.get("machine_name", "?"),
        "source_machine_id": parsed.get("machine_id", ""),
        "exported_at": parsed.get("exported_at", ""),
        "settings_diff": settings_diff,
        "games_new": games_new,
        "games_existing": games_existing,
        "games_invalid_paths": games_invalid_paths,
        "has_credentials": parsed.get("credentials_export") is not None,
        "is_identical": is_identical,
    }


def apply_import(
    parsed: dict,
    import_settings: bool = True,
    import_library: bool = True,
    import_credentials: bool = True,
    merge_strategy: str = "keep_local",
) -> dict:
    """Apply an imported config. Returns summary dict.

    merge_strategy: "keep_local" | "prefer_imported" | "merge"
    """
    from core.config_manager import get_config
    from core.library import get_library, GameEntry

    # Safety snapshot before making changes
    save_config_snapshot("pre_import")

    summary = {
        "settings_applied": 0,
        "games_added": 0,
        "games_merged": 0,
        "credentials_imported": False,
        "paths_cleared": [],
    }

    config = get_config()
    lib = get_library()

    # ── Settings ────────────────────────────────────────────────────────
    if import_settings:
        imported_settings = parsed.get("settings", {})
        for k, v in imported_settings.items():
            if k in _MACHINE_SPECIFIC_KEYS:
                continue
            config.set(k, v)
            summary["settings_applied"] += 1
        config.save()

    # ── Library ─────────────────────────────────────────────────────────
    if import_library:
        current_by_name = {g.name.lower(): g for g in lib.all_games()}
        imported_library = parsed.get("library", [])

        for gd in imported_library:
            name = gd.get("name", "")
            name_lower = name.lower()

            existing = current_by_name.get(name_lower)
            if existing:
                # Merge into existing game
                _merge_game(existing, gd, merge_strategy)
                cleared = _clear_invalid_paths(existing)
                if cleared:
                    summary["paths_cleared"].append(name)
                lib.update_game(existing)
                summary["games_merged"] += 1
            else:
                # Add as new game with fresh ID
                new_entry = GameEntry.from_dict(gd)
                import uuid
                new_entry.id = str(uuid.uuid4())
                cleared = _clear_invalid_paths(new_entry)
                if cleared:
                    summary["paths_cleared"].append(name)
                lib.add_game(new_entry)
                summary["games_added"] += 1

    # ── Credentials ─────────────────────────────────────────────────────
    # Only import credentials on the same machine — they are encrypted
    # with machine-specific keys and won't work on a different machine.
    if import_credentials and parsed.get("credentials_export"):
        from core.machine import get_machine_id
        source_machine = parsed.get("machine_id", "")
        if source_machine == get_machine_id():
            try:
                from core.credentials import get_credential_store
                ok = get_credential_store().import_credentials(
                    parsed["credentials_export"]
                )
                summary["credentials_imported"] = ok
            except Exception as e:
                logger.warning(f"Credential import failed: {e}")
        else:
            summary["credentials_skipped_machine"] = True
            logger.info("Credentials skipped — different machine")

    return summary


def _merge_game(existing: 'GameEntry', imported_dict: dict,
                strategy: str) -> None:
    """Merge imported game data into an existing entry.

    User-valuable title info must never be silently dropped by an import:
    playtime fills only when local is zero (never inflate an existing total
    on re-import), last_played keeps the most recent timestamp (and carries
    its session length along), tags/name_history are unioned, and
    descriptive metadata fills fields that are empty locally.
    """
    # Playtime: transfer into an empty slot only. Taking max() on every
    # import could leave "ghost hours" when the same export is re-applied
    # or when both sides already reflect the same sessions.
    if existing.playtime_seconds <= 0:
        imported_playtime = int(imported_dict.get("playtime_seconds", 0) or 0)
        if imported_playtime > 0:
            existing.playtime_seconds = imported_playtime

    # last_played: keep the most recent; the matching session length
    # travels with whichever side wins.
    imported_last_played = imported_dict.get("last_played") or ""
    if imported_last_played and imported_last_played > (existing.last_played or ""):
        existing.last_played = imported_last_played
        imported_session = imported_dict.get("last_session_seconds", 0)
        if imported_session:
            existing.last_session_seconds = imported_session
    elif not existing.last_session_seconds:
        existing.last_session_seconds = imported_dict.get("last_session_seconds", 0)

    # Tags / name history: union, preserving existing order first
    for list_key in ("tags", "name_history"):
        imported_list = imported_dict.get(list_key) or []
        if imported_list:
            current = list(getattr(existing, list_key) or [])
            for v in imported_list:
                if v not in current:
                    current.append(v)
            setattr(existing, list_key, current)

    # Descriptive metadata: fill only fields that are empty locally
    for fill_key in ("description", "developer", "release_year",
                     "store_url", "category", "info_source", "engine"):
        if not getattr(existing, fill_key, "") and imported_dict.get(fill_key):
            setattr(existing, fill_key, imported_dict[fill_key])

    # Reviews: union keyed by source, local side winning. A review is written
    # once and is worth keeping — the same reasoning as playtime — but two
    # machines that both searched Steam hold the same verdict twice, so the
    # source is what decides identity. The user's own review ("user") stays
    # whatever this machine has: an import must not rewrite what they wrote.
    imported_reviews = imported_dict.get("reviews") or []
    if imported_reviews:
        from core.library import review_identity
        merged_reviews = list(existing.reviews or [])
        have = {review_identity(r) for r in merged_reviews if isinstance(r, dict)}
        for review in imported_reviews:
            if not isinstance(review, dict):
                continue
            key = review_identity(review)
            if not key or key in have:
                continue
            have.add(key)
            merged_reviews.append(review)
        existing.reviews = merged_reviews

    # Merge cloud metadata (copy to avoid in-place mutation before update_game)
    imported_cloud = imported_dict.get("cloud_metadata", {})
    if imported_cloud:
        merged = dict(existing.cloud_metadata)
        merged.update(imported_cloud)
        existing.cloud_metadata = merged

    # Merge per-game settings
    for key in ("auto_backup_enabled", "backup_interval_sec"):
        if key in imported_dict:
            setattr(existing, key, imported_dict[key])

    # Path merge depends on strategy
    if strategy == "prefer_imported":
        existing.exe_path = imported_dict.get("exe_path", existing.exe_path)
        existing.save_paths = imported_dict.get("save_paths", existing.save_paths)
        existing.icon_path = imported_dict.get("icon_path", existing.icon_path)
        existing.save_paths_confirmed = False
    elif strategy == "merge":
        # Union of save paths
        imported_saves = imported_dict.get("save_paths", [])
        existing_set = set(existing.save_paths)
        for sp in imported_saves:
            if sp not in existing_set:
                existing.save_paths.append(sp)
        existing.save_paths_confirmed = False
    # "keep_local": don't touch paths


def _clear_invalid_paths(entry: 'GameEntry') -> bool:
    """Clear paths that don't exist on this machine. Returns True if any were cleared.

    Virtual registry entries (registry:HKCU\\...) are ALWAYS kept: they are
    user-relative pointers whose key may simply not exist yet on this
    machine (it appears on first play or on restore) — dropping them here
    would permanently lose the registry-save wiring on config import.
    """
    from core.registry_saves import is_registry_path
    cleared = False
    if entry.exe_path and not Path(entry.exe_path).exists():
        entry.exe_path = ""
        cleared = True
    valid_saves = [sp for sp in entry.save_paths
                   if is_registry_path(sp) or Path(sp).exists()]
    if len(valid_saves) < len(entry.save_paths):
        cleared = True
    entry.save_paths = valid_saves
    if cleared:
        entry.save_paths_confirmed = False
    return cleared


# ── Config History ──────────────────────────────────────────────────────────

def save_config_snapshot(label: str = "") -> Optional[Path]:
    """Save current config + library to a timestamped snapshot folder."""
    try:
        CONFIG_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        # Use UTC for both folder name and metadata to avoid sort inconsistencies
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        folder_name = f"{ts}_{label}" if label else ts
        snap_dir = CONFIG_HISTORY_DIR / folder_name
        snap_dir.mkdir(exist_ok=True)

        if CONFIG_FILE.exists():
            shutil.copy2(CONFIG_FILE, snap_dir / "config.json")
        if LIBRARY_FILE.exists():
            shutil.copy2(LIBRARY_FILE, snap_dir / "library.json")

        # Metadata
        meta = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "machine_name": platform.node(),
        }
        (snap_dir / "metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        # Enforce max history
        _enforce_history_limit()
        return snap_dir
    except Exception as e:
        logger.error(f"Could not save config snapshot: {e}")
        return None


def list_config_snapshots() -> list[dict]:
    """Return list of saved snapshots, newest first."""
    if not CONFIG_HISTORY_DIR.exists():
        return []
    snapshots = []
    for d in CONFIG_HISTORY_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_file = d / "metadata.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        snapshots.append({
            "path": d,
            "timestamp": meta.get("timestamp", d.name[:19].replace("T", " ")),
            "label": meta.get("label", ""),
            "machine_name": meta.get("machine_name", ""),
        })
    snapshots.sort(key=lambda s: s["timestamp"], reverse=True)
    return snapshots


def restore_config_snapshot(snapshot_path: Path) -> bool:
    """Restore config + library from a snapshot. Saves current state first."""
    try:
        # Safety net: snapshot current config before restoring
        save_config_snapshot("pre_restore")

        src_config = snapshot_path / "config.json"
        src_library = snapshot_path / "library.json"

        if src_config.exists():
            shutil.copy2(src_config, CONFIG_FILE)
        if src_library.exists():
            shutil.copy2(src_library, LIBRARY_FILE)

        # Reload singletons.  Acquire their internal locks to avoid racing
        # with concurrent get()/set() calls from other threads.
        from core.config_manager import get_config
        from core.library import get_library
        cfg = get_config()
        with cfg._io_lock:
            cfg._load()
        lib = get_library()
        with lib._lock:
            lib._load()

        # Notify the rest of the application that config/library changed
        # so all pages (library, sync, etc.) refresh their state.
        lib.library_loaded.emit()

        logger.info(f"Config restored from snapshot: {snapshot_path.name}")
        return True
    except Exception as e:
        logger.error(f"Config restore failed: {e}")
        return False


def delete_config_snapshot(snapshot_path: Path) -> bool:
    """Delete a snapshot folder."""
    try:
        if snapshot_path.exists() and snapshot_path.parent == CONFIG_HISTORY_DIR:
            shutil.rmtree(snapshot_path)
            return True
    except Exception as e:
        logger.error(f"Could not delete snapshot: {e}")
    return False


def _enforce_history_limit():
    """Keep at most MAX_CONFIG_HISTORY snapshots."""
    snaps = list_config_snapshots()
    while len(snaps) > MAX_CONFIG_HISTORY:
        oldest = snaps.pop()
        delete_config_snapshot(oldest["path"])


# ── Cloud Config Sync ───────────────────────────────────────────────────────

def upload_config_to_cloud(provider,
                           include_credentials: bool = True,
                           skip_if_unchanged: bool = False) -> bool | None:
    """Upload encrypted config to the connected sync provider.

    Returns True on success, False on error, None if skipped (unchanged).
    """
    try:
        encrypted = export_config(include_credentials,
                                  skip_if_unchanged=skip_if_unchanged)
        if encrypted is None:
            logger.info("Config upload skipped — no changes")
            return None
        encoded = base64.b64encode(encrypted)
        tmp = USER_DATA_DIR / "savesync_config_upload.tmp"
        try:
            tmp.write_bytes(encoded)
            ok = provider.upload(tmp, CLOUD_CONFIG_PATH)
            return ok
        finally:
            if tmp.exists():
                tmp.unlink()
    except Exception as e:
        logger.error(f"Cloud config upload failed: {e}")
        return False


def check_cloud_config(provider) -> Optional[dict]:
    """Check if a config file exists on the cloud provider.

    Returns {"exists": True, "remote_meta": RemoteFile} or None.
    Does NOT decrypt — just checks existence and metadata.
    """
    try:
        if not provider.is_connected:
            return None
        meta = provider.get_remote_metadata(CLOUD_CONFIG_PATH)
        if meta is not None:
            return {"exists": True, "remote_meta": meta}
    except Exception as e:
        logger.debug(f"Cloud config check failed: {e}")
    return None


def download_and_parse_cloud_config(provider) -> dict:
    """Download config from cloud and decrypt it.

    Raises ValueError on corrupt data.
    """
    tmp = USER_DATA_DIR / "savesync_config_download.tmp"
    try:
        ok = provider.download(CLOUD_CONFIG_PATH, tmp)
        if not ok:
            raise RuntimeError("Download failed")
        encoded = tmp.read_bytes()
        encrypted = base64.b64decode(encoded)
        return import_config_from_bytes(encrypted)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def should_prompt_cloud_import(remote_meta) -> bool:
    """Determine if the user should be prompted to import cloud config.

    Returns False if: same machine, already imported, or user opted out.
    """
    from core.config_manager import get_config

    config = get_config()

    if config.get("suppress_cloud_config_prompt", False):
        return False

    # Build a stable fingerprint from remote metadata.
    # Normalise modified_at to ISO string truncated to seconds to avoid
    # precision differences between providers causing repeated prompts.
    if remote_meta is None:
        return False

    mod_str = ""
    if remote_meta.modified_at:
        mod_str = remote_meta.modified_at.replace(microsecond=0).isoformat()
    fingerprint = f"{remote_meta.size_bytes}_{mod_str}"
    last_hash = config.get("last_cloud_config_hash")
    if last_hash == fingerprint:
        return False  # already imported this version

    return True


def mark_cloud_config_imported(remote_meta) -> None:
    """Record that the cloud config was imported so we don't prompt again."""
    if remote_meta is None:
        return
    from core.config_manager import get_config
    config = get_config()
    mod_str = ""
    if remote_meta.modified_at:
        mod_str = remote_meta.modified_at.replace(microsecond=0).isoformat()
    fingerprint = f"{remote_meta.size_bytes}_{mod_str}"
    config.set("last_cloud_config_hash", fingerprint)
    config.set("last_cloud_config_import", datetime.now(timezone.utc).isoformat())
    config.save()


# ── Helpers ─────────────────────────────────────────────────────────────────

_INTERNAL_KEY = "SaveSync_ConfigTransfer_v2"


def _derive_transfer_key() -> bytes:
    """Derive the AES-256 key for config transfer from the internal fixed
    key (the v1 passphrase-based scheme is gone)."""
    return hashlib.sha256(
        _INTERNAL_KEY.encode()
    ).digest()
