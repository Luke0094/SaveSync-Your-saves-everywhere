"""
SaveSync - Base Sync Provider
Abstract interface all sync providers must implement.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from i18n import t


def restrict_file_acl(path) -> None:
    """On Windows, restrict a credential/token file's ACL to the current
    user only. os.open() with 0o600 has no effect on Windows, so icacls.
    Shared by every provider that persists tokens to disk."""
    import platform
    if platform.system() != "Windows":
        return
    try:
        import os
        import subprocess
        username = os.getenv("USERNAME", "")
        if not username:
            return
        # Remove inherited permissions and grant only the current user full
        # control. CREATE_NO_WINDOW is REQUIRED: SaveSync runs windowless
        # (console=False), so without it every icacls run — e.g. a token
        # refresh during the startup provider connect — flashes a console
        # window for an instant.
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{username}:(F)"],
            capture_output=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Could not restrict ACL on {path}: {e}")


@dataclass
class RemoteFile:
    """Metadata for a file stored on a remote provider."""
    path: str               # Remote path
    modified_at: datetime
    size_bytes: int
    checksum: Optional[str] = None
    machine_id: Optional[str] = None


@dataclass
class SyncResult:
    success: bool
    message: str = ""
    files_uploaded: int = 0
    files_downloaded: int = 0
    bytes_transferred: int = 0
    error: Optional[str] = None   # human-readable error string
    conflicts: Optional[list] = None        # backup_ids of the diverging remote backups
    # Newest timestamps on each side when a cross-machine divergence was
    # detected (ISO strings) — shown by the ConflictDialog.
    conflict_local_dt: str = ""
    conflict_remote_dt: str = ""

    def __post_init__(self):
        if self.conflicts is None:
            self.conflicts = []


class SyncProvider(ABC):
    """Abstract base for all cloud/local sync providers."""

    PROVIDER_ID: str = "base"        # Override in subclass
    DISPLAY_NAME: str = "Base"       # Override in subclass (legacy, prefer DISPLAY_NAME_KEY)
    DISPLAY_NAME_KEY: str = ""       # i18n key for display name — evaluated at runtime

    def __init__(self, credentials: dict):
        self._credentials = credentials
        self._connected = False
        self._user_info: dict = {}
        # Short human-readable reason for the last connect() failure.
        # The settings UI shows this instead of a generic "authentication
        # failed" when connect() returns False without raising (e.g.
        # "rclone not found in PATH", "no URL provided").
        self.last_error: str = ""
        # Set by list_files() when it swallowed a REAL error and returned []
        # (None on success, and on the legitimate empty case of a missing
        # folder). Lets callers distinguish "confirmed empty" from "could
        # not verify" — the basis of _remote_folder_has_backup_zip's
        # fail-open. Every list_files implementation must reset it to None
        # on entry.
        self.last_list_error = None

    # ── Authentication ──────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> bool:
        """Authenticate and establish connection. Return True on success."""

    @abstractmethod
    def disconnect(self):
        """Revoke tokens / clear credentials."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def user_display(self) -> str:
        return self._user_info.get("name") or self._user_info.get("email") or t("core.user_placeholder")

    # ── File operations ─────────────────────────────────────────────────────

    @abstractmethod
    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        """List files in a remote folder."""

    @abstractmethod
    def upload(self, local_path: Path, remote_path: str) -> bool:
        """Upload a single file. Return True on success."""

    @abstractmethod
    def download(self, remote_path: str, local_path: Path) -> bool:
        """Download a single file. Return True on success."""

    @abstractmethod
    def delete_remote(self, remote_path: str) -> bool:
        """Delete a file from the remote. Return True on success."""

    @abstractmethod
    def remote_exists(self, remote_path: str) -> bool:
        """Check if a remote path exists."""

    @abstractmethod
    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        """Get metadata for a single remote file."""

    # ── Backup-based sync ──────────────────────────────────────────────────

    def sync_backups(
        self,
        game_id: str,
        game_folder: str,
        backup_manager,
        direction: str = "auto",
        progress_callback=None,
    ) -> SyncResult:
        """Sync versioned backup ZIPs instead of raw save files.

        Remote structure:
            SaveSync/backup/{game_folder}/{backup_id}.zip
            SaveSync/backup/{game_folder}/index.json

        *direction*: "auto" = bidirectional, "up" = upload only, "down" = download only.
        """
        import json as _json
        import logging as _logging
        _log = _logging.getLogger(__name__)

        remote_base = f"SaveSync/backup/{game_folder}"
        result = SyncResult(success=True)

        # ── 1. Gather local backup entries for this game ─────────────────
        local_entries = backup_manager.get_backups_for_game(game_id)
        local_ids = {e.backup_id for e in local_entries}
        remote_entries = []
        remote_ids = set()

        # ── 2. Download remote index.json (if exists) ────────────────────
        remote_index_path = f"{remote_base}/index.json"
        tmp_path = None
        try:
            if self.remote_exists(remote_index_path):
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                if self.download(remote_index_path, tmp_path):
                    try:
                        with open(tmp_path, encoding="utf-8") as f:
                            remote_entries = _json.load(f)
                        remote_ids = {e["backup_id"] for e in remote_entries}
                    except Exception as e:
                        _log.warning(f"Could not parse remote index.json: {e}")
        except Exception as e:
            _log.warning(f"Could not fetch remote index: {e}")
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

        # ── 2b. Cross-machine divergence gate (auto direction only) ───────
        # Both sides progressed independently: this machine has new backups
        # to upload AND the remote holds backups from ANOTHER machine this
        # one never confirmed downloads from. Instead of silently merging
        # the two histories, report a conflict — the worker raises the
        # ConflictDialog and the user's choice re-runs the sync with an
        # explicit direction (up / down / both). Once this machine is in
        # download_confirmed_machines (or pending_local_wins is set), the
        # gate stays open and auto sync proceeds normally.
        if direction == "auto" and remote_entries:
            try:
                from core.machine import get_machine_id
                _my_mid = get_machine_id()
                _local_new = [
                    e for e in local_entries
                    if e.backup_id not in remote_ids
                    and not (e.cloud_metadata or {}).get("pre_confirmation")
                ]
                _foreign_new = [
                    e for e in remote_entries
                    if e.get("backup_id") and e["backup_id"] not in local_ids
                    and e.get("machine_id") and e["machine_id"] != _my_mid
                ]
                if _local_new and _foreign_new:
                    _skip_gate = False
                    try:
                        from core.library import get_library
                        _lib_entry = get_library().get_by_id(game_id)
                        if _lib_entry is not None:
                            _skip_gate = (
                                _lib_entry.pending_local_wins
                                or _my_mid in (_lib_entry.cloud_metadata or {}).get(
                                    "download_confirmed_machines", [])
                            )
                    except Exception:
                        pass
                    if not _skip_gate:
                        result.conflicts = [e["backup_id"] for e in _foreign_new]
                        result.conflict_local_dt = max(
                            (e.created_at or "" for e in _local_new), default="")
                        result.conflict_remote_dt = max(
                            (e.get("created_at") or "" for e in _foreign_new), default="")
                        _log.info(
                            f"sync_backups: cross-machine divergence for {game_folder} "
                            f"({len(_foreign_new)} foreign backup(s)) — deferring to user"
                        )
                        return result
            except Exception as e:
                _log.debug(f"sync_backups: divergence gate skipped: {e}")

        # ── 3. Upload local-only backups ─────────────────────────────────
        # Pre-confirmation (temporary) backups stay local: they cover
        # auto-detected paths the user hasn't confirmed yet, so uploading
        # them would treat them as definitive. Once confirmed they get
        # promoted (flag cleared) and picked up by the next sync.
        if direction in ("auto", "up"):
            to_upload = [
                e for e in local_entries
                if e.backup_id not in remote_ids
                and not (e.cloud_metadata or {}).get("pre_confirmation")
            ]
            for entry in to_upload:
                zip_path = Path(entry.zip_path)
                if not zip_path.exists():
                    continue
                remote_zip = f"{remote_base}/{entry.backup_id}.zip"
                _log.info(f"Uploading backup {entry.backup_id} ({entry.size_human})")
                ok = self.upload(zip_path, remote_zip)
                if ok:
                    result.files_uploaded += 1
                    result.bytes_transferred += entry.size_bytes
                    # Track which provider this backup was synced to
                    # Must update the ORIGINAL entry in the index, not the copy
                    synced = entry.cloud_metadata.get("synced_to", [])
                    if self.PROVIDER_ID not in synced:
                        synced.append(self.PROVIDER_ID)
                        entry.cloud_metadata["synced_to"] = synced
                        backup_manager._mark_synced(entry.backup_id, self.PROVIDER_ID)
                    # Add to remote entries for the index update
                    remote_entries.append(entry.to_dict())
                    remote_ids.add(entry.backup_id)
                else:
                    _log.error(f"Failed to upload backup {entry.backup_id}")
                    result.success = False
                if progress_callback:
                    progress_callback(result.files_uploaded, result.files_downloaded, result.bytes_transferred)

        # ── 4. Download remote-only backups ──────────────────────────────
        if direction in ("auto", "down"):
            # Never re-download a backup this machine deliberately deleted
            # (manual delete or local retention pruning). Without this, a
            # locally-pruned backup still listed in the remote index comes
            # straight back on every sync, gets pruned again, and every
            # session reports phantom uploads+downloads with unchanged data.
            tombstoned: set = set()
            try:
                tombstoned = backup_manager.get_deleted_backup_ids(game_id)
            except Exception:
                pass

            # Also skip entries that local retention would immediately
            # delete anyway (older than the merged local+remote window):
            # downloading them would be pure churn.
            doomed: set = set()
            try:
                from core.config_manager import get_config as _get_cfg
                from core.constants import (MAX_LOCAL_BACKUPS, BACKUP_RETENTION_DAYS,
                                            MIN_KEPT_BACKUPS)
                from core.backup import BackupEntry as _BE
                _cfg = _get_cfg()
                merged: list = list(local_entries)
                for rd in remote_entries:
                    bid = rd.get("backup_id")
                    if bid and bid not in local_ids:
                        try:
                            merged.append(_BE.from_dict(dict(rd)))
                        except Exception:
                            pass
                doomed = type(backup_manager).compute_deletions(
                    merged,
                    _cfg.get("max_local_backups", MAX_LOCAL_BACKUPS),
                    _cfg.get("backup_retention_days", BACKUP_RETENTION_DAYS),
                    _cfg.get("min_kept_backups", MIN_KEPT_BACKUPS),
                ) - local_ids
            except Exception as e:
                _log.debug(f"sync_backups: pre-download prune check failed: {e}")

            skipped = {e.get("backup_id") for e in remote_entries
                       if e.get("backup_id") in (tombstoned | doomed)
                       and e.get("backup_id") not in local_ids}
            if skipped:
                _log.info(
                    f"sync_backups: skipping {len(skipped)} remote backup(s) "
                    f"(locally deleted or beyond retention): {sorted(skipped)}"
                )

            to_download = [e for e in remote_entries
                           if e.get("backup_id") and e["backup_id"] not in local_ids
                           and e["backup_id"] not in tombstoned
                           and e["backup_id"] not in doomed]
            for rentry_dict in to_download:
                bid = rentry_dict["backup_id"]
                remote_zip = f"{remote_base}/{bid}.zip"
                _log.info(f"Downloading backup {bid}")
                tmp_dl = None
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp_dl = Path(tmp.name)
                    ok = self.download(remote_zip, tmp_dl)
                    if ok and tmp_dl.exists():
                        from core.backup import BackupEntry as _BE
                        # Set origin to this provider's ID
                        rentry_dict["origin"] = self.PROVIDER_ID
                        entry_obj = _BE.from_dict(rentry_dict)
                        # Re-stamp with the LOCAL game_id: a cross-PC backup
                        # carries the originating machine's random id, and
                        # get_backups_for_game(local_id) would never find it
                        # ("no backups found" after download). The backup_id
                        # keeps its original prefix — it's just an identifier.
                        entry_obj.game_id = game_id
                        zip_data = tmp_dl.read_bytes()
                        if backup_manager.import_backup(entry_obj, zip_data):
                            result.files_downloaded += 1
                            result.bytes_transferred += len(zip_data)
                        else:
                            result.success = False
                    else:
                        _log.error(f"Failed to download backup {bid}")
                        result.success = False
                except Exception as e:
                    _log.error(f"Error downloading backup {bid}: {e}")
                    result.success = False
                finally:
                    if tmp_dl and tmp_dl.exists():
                        tmp_dl.unlink(missing_ok=True)
                if progress_callback:
                    progress_callback(result.files_uploaded, result.files_downloaded, result.bytes_transferred)

        # ── 5. Upload updated remote index.json ──────────────────────────
        # Update the remote index whenever something changed (uploads, downloads,
        # or an explicit "up" direction) so that the index always reflects the
        # current merged state across all machines.
        if result.files_uploaded > 0 or result.files_downloaded > 0 or direction in ("up", "down"):
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                                  delete=False, encoding="utf-8") as f:
                    _json.dump(remote_entries, f, indent=2)
                    idx_tmp = Path(f.name)
                if not self.upload(idx_tmp, remote_index_path):
                    _log.warning("Failed to upload updated remote index.json")
                idx_tmp.unlink(missing_ok=True)
            except Exception as e:
                _log.warning(f"Could not update remote index: {e}")

        if result.success:
            result.message = t("core.sync_complete")
        else:
            result.message = t("core.sync_files_failed")
        return result

    def list_cloud_backups(self, game_folder: str) -> list[dict]:
        """Fetch the remote backup index for a game. Returns list of entry dicts."""
        import json as _json
        import logging as _logging
        _log = _logging.getLogger(__name__)
        remote_index_path = f"SaveSync/backup/{game_folder}/index.json"
        tmp_path = None
        try:
            if not self.remote_exists(remote_index_path):
                return []
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            if self.download(remote_index_path, tmp_path):
                try:
                    with open(tmp_path, encoding="utf-8") as f:
                        remote_entries = _json.load(f)
                    return remote_entries
                except Exception as e:
                    _log.warning(f"Could not parse remote index.json: {e}")
            return []
        except Exception as e:
            _log.warning(f"Could not fetch remote index: {e}")
            return []
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

    def enforce_remote_limits(self, game_folder: str, remote_entries: list[dict],
        max_backups: int,
        retention_days: int,
        min_kept: int,
    ) -> list[dict]:
        """Delete remote backups that exceed limits. Returns the kept entries."""
        import json as _json
        from core.backup import BackupEntry as _BE, BackupManager

        entry_objs = []
        for d in remote_entries:
            try:
                entry_objs.append(_BE.from_dict(d))
            except Exception:
                continue

        to_delete = BackupManager.compute_deletions(entry_objs, max_backups, retention_days, min_kept)
        if not to_delete:
            return remote_entries

        remote_base = f"SaveSync/backup/{game_folder}"
        for bid in to_delete:
            try:
                self.delete_remote(f"{remote_base}/{bid}.zip")
            except Exception:
                pass

        kept = [d for d in remote_entries if d.get("backup_id") not in to_delete]

        # Update remote index
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                              delete=False, encoding="utf-8") as f:
                _json.dump(kept, f, indent=2)
                idx_tmp = Path(f.name)
            self.upload(idx_tmp, f"{remote_base}/index.json")
            idx_tmp.unlink(missing_ok=True)
        except Exception:
            pass

        return kept

    # ── Credential schema ────────────────────────────────────────────────────

    @classmethod
    def credential_fields(cls) -> list[dict]:
        """
        Return UI field definitions for the settings dialog.
        Each dict: {id, label, type: text|password|file|oauth, required}
        """
        return []

    def validate_credentials(self, creds: dict) -> tuple[bool, str]:
        """Validate credentials before attempting connection. (ok, error_msg)"""
        return True, ""

    @staticmethod
    def _get_timeout(default=120):
        """The user's sync timeout from config — shared by every provider
        (rclone overrides this with a minimum-floor variant)."""
        try:
            from core.config_manager import get_config
            return get_config().get("sync_timeout", default)
        except Exception:
            return default

    def list_all_cloud_backups(self) -> dict[str, list[dict]]:
        """Fetch all cloud backups using the master index. Returns dict of {game_folder: entries}."""
        import json as _json

        tmp_path = None
        try:
            # Try to read the master index first
            master_index_path = "SaveSync/backup/index.json"
            if self.remote_exists(master_index_path):
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                if self.download(master_index_path, tmp_path):
                    with open(tmp_path, encoding="utf-8") as f:
                        master_data = _json.load(f)
                    return master_data.get("games", {})

            # Fallback: scan individual folders (for backward compatibility)
            return self._scan_all_folders_fallback()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to list all cloud backups: {e}")
            return {}
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

    def _scan_all_folders_fallback(self) -> dict[str, list[dict]]:
        """Fallback method: scan all folders individually."""
        all_backups = {}
        try:
            backup_dir = "SaveSync/backup"
            if self.remote_exists(backup_dir):
                game_folders = self._list_remote_folders(backup_dir)
                for game_folder in game_folders:
                    entries = self.list_cloud_backups(game_folder)
                    if entries:
                        all_backups[game_folder] = entries
        except Exception:
            pass
        return all_backups

    def update_master_index(self, game_folder: str, entries: list[dict]) -> bool:
        """Update the master index with backup info for a specific game.

        Uses a **metadata-check** strategy to handle concurrent updates
        from other machines cheaply (no redundant download):

        1. Snapshot the remote file's metadata (mtime + size) — one
           lightweight API call, no download.
        2. Download and parse the current master index.
        3. Apply our update (set ``game_folder`` entries).
        4. Before uploading, re-check metadata.  If unchanged, no other
           machine wrote to the file — upload directly.
        5. If metadata **changed**, another machine wrote concurrently.
           Re-download the new version and **merge**: their game folders
           are preserved, ours wins for ``game_folder``.
        6. Upload the merged result.

        If the master index is corrupt or unparsable it is rebuilt from
        per-game remote indexes (best-effort).
        """
        import json as _json
        import logging as _logging
        _log = _logging.getLogger(__name__)

        tmp_files: list[Path] = []

        def _mktemp(suffix: str) -> Path:
            import tempfile
            f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            p = Path(f.name)
            f.close()
            tmp_files.append(p)
            return p

        def _meta_fingerprint(meta: "Optional[RemoteFile]") -> tuple:
            """Return a comparable fingerprint from RemoteFile metadata."""
            if meta is None:
                return (None, None, None)
            return (meta.modified_at, meta.size_bytes, meta.checksum)

        def _download_master() -> dict:
            """Download and parse the master index."""
            empty: dict = {"games": {}, "last_updated": ""}
            if not self.remote_exists(master_path):
                return empty
            tmp = _mktemp(".json")
            if not self.download(master_path, tmp):
                return empty
            try:
                with open(tmp, encoding="utf-8") as fh:
                    data = _json.load(fh)
                if not isinstance(data.get("games"), dict):
                    raise ValueError("missing or invalid 'games' key")
                return data
            except Exception as exc:
                _log.warning(f"Master index corrupt, will rebuild: {exc}")
                return self._rebuild_master_index()

        def _merge(base: dict, theirs: dict, ours_folder: str, ours_entries: list) -> dict:
            """Three-way merge: keep all of *theirs* game folders, override
            *ours_folder* with *ours_entries*."""
            merged_games = dict(theirs.get("games", {}))
            for gf, ents in base.get("games", {}).items():
                if gf not in merged_games:
                    merged_games[gf] = ents
            merged_games[ours_folder] = ours_entries
            from datetime import timezone
            return {
                "games": merged_games,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

        master_path = "SaveSync/backup/index.json"
        try:
            # Step 1: snapshot metadata before download (cheap API call)
            pre_meta = _meta_fingerprint(self.get_remote_metadata(master_path))

            # Step 2: download + parse
            base = _download_master()

            # Step 3: apply our update
            base.setdefault("games", {})[game_folder] = entries
            from datetime import timezone
            base["last_updated"] = datetime.now(timezone.utc).isoformat()

            # Step 4: check if file changed since our download
            post_meta = _meta_fingerprint(self.get_remote_metadata(master_path))

            if pre_meta != post_meta:
                # Another machine wrote — re-download and merge
                _log.info("Master index changed during update, merging")
                theirs = _download_master()
                base = _merge(base, theirs, game_folder, entries)

            # Step 5: upload
            ul_tmp = _mktemp(".json")
            with open(ul_tmp, "w", encoding="utf-8") as fh:
                _json.dump(base, fh, indent=2)
            return self.upload(ul_tmp, master_path)

        except Exception as exc:
            _log.warning(f"Failed to update master index: {exc}")
            return False
        finally:
            for p in tmp_files:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    def _rebuild_master_index(self) -> dict:
        """Rebuild the master index from individual per-game remote indexes.

        Called as a recovery path when the master index is corrupt or
        unparsable.  Scans all game folders and assembles a fresh master
        index from their ``index.json`` files.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.info("Rebuilding master index from per-game remote indexes")
        all_backups = self._scan_all_folders_fallback()
        from datetime import timezone
        return {
            "games": all_backups,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    def _list_remote_folders(self, remote_path: str) -> list[str]:
        """List folder names directly under remote_path, derived from list_files.

        Providers return RECURSIVE listings, as either full paths
        ("SaveSync/backup/<game>/<file>") or base-relative ones; the old
        exactly-two-segments rule matched neither shape, so homonym
        detection and the corrupt-index fallback scan silently returned []
        for every provider. A folder is any first component below
        remote_path that still has content beneath it.
        """
        try:
            base_parts = [p for p in remote_path.strip("/").split("/") if p]
            base_lower = [p.lower() for p in base_parts]
            folders: list[str] = []
            seen: set[str] = set()
            for file_info in self.list_files(remote_path):
                parts = [p for p in file_info.path.strip("/").split("/") if p]
                if base_lower and [p.lower() for p in parts[:len(base_lower)]] == base_lower:
                    rest = parts[len(base_parts):]
                else:
                    rest = parts   # provider returned base-relative paths
                if len(rest) >= 2:   # folder + at least one entry inside it
                    name = rest[0]
                    if name.lower() not in seen:
                        seen.add(name.lower())
                        folders.append(name)
            return folders
        except Exception:
            return []
