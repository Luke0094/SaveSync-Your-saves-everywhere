"""
SaveSync - Sync Registry & Orchestrator
Manages provider selection, conflict detection, and sync execution.
"""
import logging
import threading
from typing import Optional, Type

from PySide6.QtCore import QObject, QThread, Signal

from sync.base import SyncProvider, SyncResult
from core.config_manager import get_config
import i18n

logger = logging.getLogger(__name__)


# ── Provider registry ────────────────────────────────────────────────────────

_PROVIDER_REGISTRY: dict[str, Type[SyncProvider]] = {}


def register_provider(cls: Type[SyncProvider]):
    _PROVIDER_REGISTRY[cls.PROVIDER_ID] = cls


def _register_all():
    from sync.local_provider    import LocalProvider
    from sync.google_drive      import GoogleDriveProvider
    from sync.onedrive_provider import OneDriveProvider
    from sync.dropbox_provider  import DropboxProvider
    from sync.webdav_provider   import WebDAVProvider
    from sync.rclone_provider   import RcloneProvider

    for cls in (LocalProvider, GoogleDriveProvider, OneDriveProvider,
                DropboxProvider, WebDAVProvider, RcloneProvider):
        register_provider(cls)


_register_all()


def available_providers() -> list[dict]:
    """Return list of {id, name} for UI rendering. Names are resolved at call time for i18n."""
    result = []
    for pid, cls in _PROVIDER_REGISTRY.items():
        if cls.DISPLAY_NAME_KEY:
            name = i18n.t(cls.DISPLAY_NAME_KEY)
        else:
            name = cls.DISPLAY_NAME  # fallback for providers without i18n key
        result.append({"id": pid, "name": name})
    return result


def get_provider_class(provider_id: str) -> Optional[Type[SyncProvider]]:
    return _PROVIDER_REGISTRY.get(provider_id)


def get_provider_fields(provider_id: str) -> list[dict]:
    cls = get_provider_class(provider_id)
    return cls.credential_fields() if cls else []


# ── Worker thread for async sync ─────────────────────────────────────────────

class SyncWorker(QThread):
    progress         = Signal(str)
    finished         = Signal(object)   # SyncResult
    conflict_detected = Signal(str, str, str)   # game_id, local_dt, remote_dt

    # Class-level lock to serialize master index updates across all workers
    _master_index_lock = threading.Lock()

    def __init__(self, providers: list[SyncProvider], game_id: str, game_name: str,
                 save_paths: list, direction: str = "auto", exe_path: str = "",
                 computed_folder_name: str = "", name_history: list[str] | None = None,
                 excluded_paths: list[str] | None = None,
                 parent=None):
        super().__init__(parent)
        self._providers = providers  # snapshot of connected providers
        self._game_id   = game_id
        self._game_name = game_name
        self._save_paths = save_paths
        self._direction  = direction
        self._exe_path   = exe_path
        self._computed_folder_name = computed_folder_name
        self._name_history = name_history or []
        self._excluded_paths = excluded_paths or []

    def _migrate_remote_folders(self, providers: list, current_folder: str) -> None:
        """Move remote backup files from old-name folders into *current_folder*.

        For each past name, if a remote folder exists under the old name it
        means the game was renamed.  We copy each relevant zip to the new
        remote folder (using the local copy we already have) then delete the
        old remote file.  Finally the old remote folder's index.json is removed.
        """
        from core.constants import get_folder_name_for_save
        from core.backup import BACKUP_DIR
        game_id = self._game_id

        # Candidate old folders: reconstructed from display-name history PLUS
        # actual past folder names (folder_history), which preserve any
        # disambiguation suffix a display name can't reproduce.
        candidate_folders = [get_folder_name_for_save(n, self._exe_path, game_id)
                             for n in self._name_history]
        try:
            from core.library import get_library as _gl
            _entry = _gl().get_by_id(game_id)
            if _entry is not None:
                candidate_folders += list(_entry.folder_history or [])
        except Exception:
            pass

        seen_old: set[str] = set()
        for old_folder in candidate_folders:
            if not old_folder or old_folder == current_folder or old_folder in seen_old:
                continue
            seen_old.add(old_folder)
            # Never migrate OUT of a folder still owned by a different live game.
            # A disambiguated game (folder "Foo_2") keeps the un-suffixed base
            # ("Foo") in its name_history, but "Foo" may be another game's active
            # folder — touching it would delete that game's remote index.json or
            # steal its files. Only migrate from folders that are truly ex-mine.
            try:
                from core.library import get_library
                if get_library().folder_name_in_use_by_other(old_folder, game_id):
                    continue
            except Exception:
                pass
            old_remote_base = f"SaveSync/{old_folder}"
            new_remote_base = f"SaveSync/{current_folder}"

            for provider in providers:
                try:
                    if not provider.remote_exists(old_remote_base):
                        continue
                    # List backup zips for this game in the old remote folder
                    try:
                        remote_files = provider.list_files(old_remote_base)
                    except Exception:
                        continue

                    for rf in remote_files:
                        fname = rf.path.split("/")[-1]
                        if game_id not in fname:
                            continue
                        # Find the local copy to re-upload to new folder
                        local_zip = BACKUP_DIR / current_folder / fname
                        if not local_zip.exists():
                            local_zip = BACKUP_DIR / old_folder / fname
                        if not local_zip.exists():
                            logger.debug(f"Skipping {fname}: no local copy found")
                            continue
                        new_remote = f"{new_remote_base}/{fname}"
                        old_remote = f"{old_remote_base}/{fname}"
                        try:
                            if provider.upload(local_zip, new_remote):
                                provider.delete_remote(old_remote)
                                logger.info(
                                    f"[{provider.PROVIDER_ID}] Migrated {fname}: "
                                    f"{old_folder} → {current_folder}"
                                )
                        except Exception as e:
                            logger.warning(f"[{provider.PROVIDER_ID}] Migration failed for {fname}: {e}")

                    # Clean up old remote index.json once the folder is empty.
                    # "Empty" has to mean empty of EVERYTHING, not just of our
                    # own zips: a folder named after a shared title can be a
                    # homonym's home on another machine, and deleting the index
                    # that describes ITS backups would cost that game its
                    # history — for a folder this game never even uploaded to.
                    try:
                        remaining = provider.list_files(old_remote_base)
                        others = [f for f in remaining
                                  if f.path.split("/")[-1] != "index.json"]
                        if not others:
                            old_idx = f"{old_remote_base}/index.json"
                            if provider.remote_exists(old_idx):
                                provider.delete_remote(old_idx)
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug(f"[{provider.PROVIDER_ID}] Remote migration check failed: {e}")

    def _sync_one_provider(self, provider: SyncProvider, bm, game_folder) -> SyncResult:
        """Run sync_backups against a single provider."""
        def _on_progress(up, down, bytes_total):
            self.progress.emit(
                f"⟳ {self._game_name} [{provider.PROVIDER_ID}]: ↑{up} ↓{down} ({bytes_total // 1024}KB)"
            )

        result = provider.sync_backups(
            self._game_id,
            game_folder,
            bm,
            direction=self._direction,
            progress_callback=_on_progress,
        )

        # After any successful sync, refresh the master index so it
        # reflects downloads as well as uploads.  Limit enforcement only
        # applies when new backups were uploaded.
        if result.success and (result.files_uploaded > 0 or result.files_downloaded > 0):
            try:
                from core.config_manager import get_config
                from core.constants import MAX_LOCAL_BACKUPS, BACKUP_RETENTION_DAYS, MIN_KEPT_BACKUPS
                cfg = get_config()
                remote_entries = provider.list_cloud_backups(game_folder)

                # Enforce remote limits only when we uploaded new backups
                if remote_entries and result.files_uploaded > 0:
                    remote_entries = provider.enforce_remote_limits(
                        game_folder, remote_entries,
                        cfg.get("max_local_backups", MAX_LOCAL_BACKUPS),
                        cfg.get("backup_retention_days", BACKUP_RETENTION_DAYS),
                        cfg.get("min_kept_backups", MIN_KEPT_BACKUPS),
                    )

                # Always update master index after any change (serialized locally)
                if remote_entries is not None:
                    with SyncWorker._master_index_lock:
                        provider.update_master_index(game_folder, remote_entries)
            except Exception as e:
                logger.warning(f"Post-sync index update failed for {provider.PROVIDER_ID}: {e}")

        return result

    def run(self):
        active = [p for p in self._providers if p.is_connected]
        if not active:
            self.finished.emit(SyncResult(success=False, error=i18n.t('sync.provider_disconnected')))
            return

        self.progress.emit(f"Syncing {self._game_name}...")
        try:
            from core.backup import get_backup_manager
            from core.constants import get_install_folder_name
            bm = get_backup_manager()

            game_folder = get_install_folder_name(self._exe_path, self._game_name, self._game_id, self._computed_folder_name)

            if self.isInterruptionRequested():
                self.finished.emit(SyncResult(success=False, message="Sync cancelled"))
                return

            # Ensure a fresh backup exists before uploading
            if self._direction in ("auto", "up"):
                save_paths = [str(p) for p in self._save_paths]
                bm.create_backup(
                    self._game_id, self._game_name, save_paths,
                    exe_path=self._exe_path,
                    computed_folder_name=self._computed_folder_name,
                    name_history=self._name_history,
                    excluded_paths=self._excluded_paths,
                )

            # Migrate remote folders for old names before sync
            if self._name_history:
                self._migrate_remote_folders(active, game_folder)

            if self.isInterruptionRequested():
                self.finished.emit(SyncResult(success=False, message="Sync cancelled"))
                return

            # Sync to each provider sequentially
            combined = SyncResult(success=True)
            failed_providers: list[str] = []
            for provider in active:
                if self.isInterruptionRequested():
                    combined.success = False
                    combined.message = "Sync cancelled"
                    break
                try:
                    result = self._sync_one_provider(provider, bm, game_folder)
                    combined.files_uploaded += result.files_uploaded
                    combined.files_downloaded += result.files_downloaded
                    combined.bytes_transferred += result.bytes_transferred
                    if result.conflicts:
                        # Cross-machine divergence: stop the whole run and
                        # let the user's ConflictDialog choice re-launch the
                        # sync with an explicit direction (up / down / both).
                        combined.conflicts.extend(result.conflicts)
                        combined.conflict_local_dt = result.conflict_local_dt
                        combined.conflict_remote_dt = result.conflict_remote_dt
                        self.conflict_detected.emit(
                            self._game_id,
                            result.conflict_local_dt,
                            result.conflict_remote_dt,
                        )
                        break
                    if not result.success:
                        combined.success = False
                        err = result.error or i18n.t('sync.operation_failed_no_error')
                        combined.error = (combined.error or "") + f"[{provider.PROVIDER_ID}] {err}; "
                        failed_providers.append(provider.PROVIDER_ID)
                except Exception as e:
                    logger.error(f"Sync error for {self._game_name} on {provider.PROVIDER_ID}: {e}", exc_info=True)
                    combined.success = False
                    user_error = self._classify_error(e)
                    combined.error = (combined.error or "") + f"[{provider.PROVIDER_ID}] {user_error}; "
                    failed_providers.append(provider.PROVIDER_ID)

            if not combined.success and not combined.message:
                combined.message = i18n.t('sync.failed_to_sync', game=self._game_name)

            # Attach failed provider IDs for reconnect logic
            combined._failed_providers = failed_providers

            self.finished.emit(combined)
        except Exception as e:
            logger.error(f"Sync worker error for {self._game_name}: {e}", exc_info=True)
            result = SyncResult(
                success=False,
                message=i18n.t('sync.sync_failed_for', game=self._game_name),
                error=self._classify_error(e)
            )
            result._failed_providers = [p.PROVIDER_ID for p in active]
            self.finished.emit(result)

    @staticmethod
    def _classify_error(e: Exception) -> str:
        err_str = str(e).lower()
        if "401" in err_str or "403" in err_str or "auth" in err_str or "token" in err_str:
            return i18n.t('sync.error_auth_expired')
        elif "timeout" in err_str or "timed out" in err_str:
            return i18n.t('sync.error_timeout')
        elif "connection" in err_str or "network" in err_str or "refused" in err_str:
            return i18n.t('sync.error_network')
        elif "permission" in err_str or "access denied" in err_str:
            return i18n.t('sync.error_permission')
        elif "not found" in err_str or "404" in err_str:
            return i18n.t('sync.error_not_found')
        elif "quota" in err_str or "storage" in err_str or "space" in err_str:
            return i18n.t('sync.error_quota')
        else:
            return str(e)[:120]


# ── Sync orchestrator singleton ───────────────────────────────────────────────

class SyncOrchestrator(QObject):
    sync_started      = Signal(str)          # game_id
    sync_finished     = Signal(str, object)  # game_id, SyncResult
    conflict_detected = Signal(str, object)  # game_id, conflict_info dict
    provider_changed  = Signal(str)          # provider_id or "" (backward compat)
    providers_updated = Signal()             # emitted after any provider connect/disconnect

    def __init__(self):
        super().__init__()
        self._providers: dict[str, SyncProvider] = {}
        self._workers:  list[SyncWorker]       = []
        self._syncing_games: set[str]          = set()  # prevent double-sync
        self._sync_lock = threading.Lock()
        self._reconnect_state: dict[str, dict] = {}  # {pid: {"attempts": int, "timer": QTimer}}
        self._max_reconnect_attempts = 5
        self._max_history = 100
        # History is persisted so the sync page keeps its entries across
        # app restarts (one entry per sync run, never aggregated).
        self._sync_history: list[dict] = self._load_history()

    @staticmethod
    def _history_path():
        from core.constants import USER_DATA_DIR
        return USER_DATA_DIR / "sync_history.json"

    def _load_history(self) -> list[dict]:
        import json as _json
        try:
            p = self._history_path()
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    data = _json.load(f)
                if isinstance(data, list):
                    return data[: self._max_history]
        except Exception as e:
            logger.warning(f"Could not load sync history: {e}")
        return []

    def _save_history(self):
        import json as _json
        try:
            with self._sync_lock:
                data = list(self._sync_history)
            p = self._history_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, indent=2)
            from core import atomic_replace as _atomic_replace
            _atomic_replace(tmp, p)
        except Exception as e:
            logger.warning(f"Could not save sync history: {e}")

    # ── Provider loading ─────────────────────────────────────────────────────

    def load_provider(self, provider_id: str) -> bool:
        """Load and connect a single provider by its ID."""
        from core.credentials import get_credential_store
        config = get_config()

        pid = provider_id
        if not pid:
            return False

        cls = get_provider_class(pid)
        if not cls:
            return False

        creds = get_credential_store().load_provider(pid)
        if not creds:
            # Legacy migration: move plaintext config creds to secure store
            legacy_creds = config.get("sync_credentials", {})
            if legacy_creds:
                get_credential_store().save(pid, legacy_creds)
                config.set("sync_credentials", {})
                logger.info("Migrated credentials from config to secure store")
                creds = get_credential_store().load_provider(pid)
                if not creds:
                    return False
            else:
                logger.debug(f"No credentials found for provider {pid}")
                return False

        instance = cls(creds)
        try:
            ok = instance.connect()
        except Exception as e:
            # load_provider is also called from the auto-reconnect QTimer
            # slot — a raising connect() must never escape into the Qt
            # event loop.
            logger.error(f"Provider {pid} connect raised: {e}")
            ok = False
        if ok:
            with self._sync_lock:
                self._providers[pid] = instance
            # Update config tracking
            providers_list = config.get("sync_providers", [])
            if pid not in providers_list:
                providers_list.append(pid)
                config.set("sync_providers", providers_list)
            pc = config.get("providers_connected", {})
            pc[pid] = True
            config.set("providers_connected", pc)
            self.provider_changed.emit(pid)
            self.providers_updated.emit()
        return ok

    def load_all_providers(self) -> dict[str, bool]:
        """Load and connect providers that were previously connected successfully."""
        config = get_config()
        pids = list(config.get("sync_providers", []))
        pc = config.get("providers_connected", {})
        # Only attempt providers that were previously connected
        to_load = [pid for pid in pids if pc.get(pid, False)]
        # Clean up providers that never connected from the list
        stale = [pid for pid in pids if not pc.get(pid, False)]
        if stale:
            cleaned = [pid for pid in pids if pid not in stale]
            config.set("sync_providers", cleaned)
            logger.debug(f"Removed never-connected providers from config: {stale}")
        results = {}
        for pid in to_load:
            results[pid] = self.load_provider(pid)
        return results

    def set_provider(self, provider: SyncProvider):
        """Set an already-connected provider (used after UI connect flow)."""
        pid = provider.PROVIDER_ID
        with self._sync_lock:
            self._providers[pid] = provider
        config = get_config()
        providers_list = config.get("sync_providers", [])
        if pid not in providers_list:
            providers_list.append(pid)
            config.set("sync_providers", providers_list)
        pc = config.get("providers_connected", {})
        pc[pid] = True
        config.set("providers_connected", pc)
        self.provider_changed.emit(pid)
        self.providers_updated.emit()
        self.reset_reconnect(pid)

    def disconnect_provider(self, provider_id: str = None):
        """Disconnect a specific provider, or all if provider_id is None."""
        with self._sync_lock:
            if provider_id is None:
                pids_to_disconnect = list(self._providers.keys())
            else:
                pids_to_disconnect = [provider_id]
        for pid in pids_to_disconnect:
            self._disconnect_one(pid)
        self.providers_updated.emit()

    def _disconnect_one(self, pid: str):
        """Disconnect and remove a single provider."""
        with self._sync_lock:
            prov = self._providers.pop(pid, None)
        if prov:
            try:
                prov.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting provider {pid}: {e}")
        config = get_config()
        providers_list = config.get("sync_providers", [])
        if pid in providers_list:
            providers_list.remove(pid)
            config.set("sync_providers", providers_list)
        pc = config.get("providers_connected", {})
        pc.pop(pid, None)
        config.set("providers_connected", pc)
        with self._sync_lock:
            has_remaining = bool(self._providers)
        self.provider_changed.emit(pid if has_remaining else "")

    def shutdown(self):
        """Stop all running workers and wait for completion."""
        workers_snapshot = list(self._workers)
        for w in workers_snapshot:
            if w.isRunning():
                w.requestInterruption()
                w.quit()
                if not w.wait(10000):
                    logger.warning("Sync worker did not stop within timeout, forcing termination")
                    w.terminate()
                    w.wait(2000)
        self._workers.clear()
        with self._sync_lock:
            self._syncing_games.clear()

    # ── Provider access ──────────────────────────────────────────────────────

    def is_online(self) -> bool:
        with self._sync_lock:
            return any(p.is_connected for p in self._providers.values())

    @property
    def provider(self) -> Optional[SyncProvider]:
        """Return first connected provider or None."""
        with self._sync_lock:
            for p in self._providers.values():
                if p.is_connected:
                    return p
        return None

    def get_provider(self, provider_id: str) -> Optional[SyncProvider]:
        with self._sync_lock:
            return self._providers.get(provider_id)

    def get_connected_providers(self) -> list[SyncProvider]:
        with self._sync_lock:
            return [p for p in self._providers.values() if p.is_connected]

    def get_connected_provider_ids(self) -> list[str]:
        with self._sync_lock:
            return [pid for pid, p in self._providers.items() if p.is_connected]

    @property
    def sync_history(self) -> list[dict]:
        with self._sync_lock:
            return list(self._sync_history)

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync_game(self, game_id: str, game_name: str, save_paths: list,
                  direction: str = "auto", exe_path: str = "", computed_folder_name: str | None = None,
                  name_history: list[str] | None = None, excluded_paths: list[str] | None = None):
        # Fill excluded_paths from the library when the caller didn't pass
        # them: the pre-sync backup hashes save_paths WITHOUT exclusions
        # otherwise, sees a "different" content hash than the last real
        # backup (made WITH exclusions) and creates a spurious second
        # backup before every sync.
        if excluded_paths is None:
            try:
                from core.library import get_library as _gl
                _e = _gl().get_by_id(game_id)
                if _e is not None:
                    excluded_paths = list(_e.excluded_save_paths or [])
            except Exception:
                excluded_paths = None
        # One-shot "keep local wins": the user chose keep-local for a game whose
        # cloud folder already holds another machine's data, so force this sync to
        # UPLOAD — a plain "auto" would download a newer-mtime cloud copy and
        # overwrite the local one. Read only here; the flag is cleared on the
        # first SUCCESSFUL sync, so a failed sync keeps the protection.
        if direction == "auto":
            try:
                from core.library import get_library as _gl
                _e2 = _gl().get_by_id(game_id)
                if _e2 is not None and getattr(_e2, "pending_local_wins", False):
                    direction = "up"
                    logger.info(f"'{game_name}': keep-local → forcing upload (local wins)")
            except Exception:
                pass
        with self._sync_lock:
            if game_id in self._syncing_games:
                logger.warning(f"Sync already in progress for {game_name}, skipping")
                return
            self._syncing_games.add(game_id)

        connected = self.get_connected_providers()
        if not connected:
            logger.warning("Sync requested but no provider connected")
            with self._sync_lock:
                self._syncing_games.discard(game_id)
            return

        self._cleanup_workers()
        self.sync_started.emit(game_id)
        worker = SyncWorker(
            connected, game_id, game_name, save_paths, direction,
            exe_path=exe_path,
            computed_folder_name=computed_folder_name or "",
            name_history=name_history or [],
            excluded_paths=excluded_paths or [],
        )

        def _on_done(result, _worker=worker, _gid=game_id):
            try:
                _worker.finished.disconnect()
                _worker.progress.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._on_sync_done(_gid, result)

        from PySide6.QtCore import Qt
        worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(lambda msg: logger.info(msg))
        worker.conflict_detected.connect(
            lambda gid, ldt, rdt: self.conflict_detected.emit(gid, {"local": ldt, "remote": rdt})
        )
        with self._sync_lock:
            self._workers.append(worker)
        worker.start()

    def _cleanup_workers(self):
        """Remove finished workers from the list."""
        with self._sync_lock:
            alive = []
            for w in self._workers:
                if w.isRunning():
                    alive.append(w)
                else:
                    w.deleteLater()
            self._workers = alive

    def _on_sync_done(self, game_id: str, result: SyncResult):
        # Resolve the display name now so history rows survive a game being
        # renamed/removed later.
        game_name = ""
        try:
            from core.library import get_library as _gl
            _e = _gl().get_by_id(game_id)
            game_name = _e.name if _e else ""
        except Exception:
            pass
        with self._sync_lock:
            self._syncing_games.discard(game_id)
            from datetime import datetime, timezone
            self._sync_history.insert(0, {
                "game_id": game_id,
                "game_name": game_name,
                "time": datetime.now(timezone.utc).isoformat(),
                "success": result.success,
                "message": result.message or "",
                "files_uploaded": result.files_uploaded,
                "files_downloaded": result.files_downloaded,
                "bytes": result.bytes_transferred,
            })
            if len(self._sync_history) > self._max_history:
                self._sync_history = self._sync_history[:self._max_history]
        self._save_history()
        # Retire the one-shot "keep local wins" flag once a sync SUCCEEDS (any
        # direction — an explicit later download should also clear it). A failed
        # sync leaves it set, so the next attempt is still forced to upload.
        if result.success:
            try:
                from core.library import get_library as _gl
                _e3 = _gl().get_by_id(game_id)
                if _e3 is not None and getattr(_e3, "pending_local_wins", False):
                    _gl().update_game_fields(game_id, pending_local_wins=False)
            except Exception:
                pass
        # Stamp the machine_id on cloud_metadata so other machines can detect cross-machine syncs
        if result.success and result.files_uploaded > 0:
            try:
                from core.library import get_library as _gl
                from core.machine import get_machine_id as _mid
                entry = _gl().get_by_id(game_id)
                if entry:
                    cloud_meta = dict(entry.cloud_metadata or {})
                    cloud_meta["last_sync_machine"] = _mid()
                    # Reset download confirmations since the cloud data changed
                    cloud_meta["download_confirmed_machines"] = [_mid()]
                    _gl().update_game_fields(game_id, cloud_metadata=cloud_meta)
            except Exception as _e:
                logger.debug(f"Failed to stamp sync machine: {_e}")
        # Detect connection loss and trigger per-provider auto-reconnect
        if not result.success and result.error:
            err_lower = result.error.lower()
            if any(kw in err_lower for kw in ("connection", "timeout", "network", "refused", "reset", "ssl")):
                failed_pids = getattr(result, '_failed_providers', [])
                for pid in failed_pids:
                    logger.warning(f"Sync failed with network error for {pid}, scheduling reconnect")
                    self._schedule_reconnect(pid)
        self.sync_finished.emit(game_id, result)
        self._cleanup_workers()

    # ── Per-provider reconnect ───────────────────────────────────────────────

    def _schedule_reconnect(self, provider_id: str):
        """Schedule a reconnection attempt for a specific provider."""
        with self._sync_lock:
            state = self._reconnect_state.setdefault(provider_id, {"attempts": 0, "timer": None})
            if state["attempts"] >= self._max_reconnect_attempts:
                logger.warning(f"Max reconnect attempts reached for {provider_id}, giving up")
                return
            delay = min(5000 * (2 ** state["attempts"]), 60000)
            state["attempts"] += 1
            attempt = state["attempts"]
            from PySide6.QtCore import QTimer
            if state["timer"] is None:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda pid=provider_id: self._try_reconnect(pid))
                state["timer"] = timer
            logger.info(f"Scheduling reconnect for {provider_id} attempt {attempt} in {delay}ms")
            state["timer"].start(delay)

    def _try_reconnect(self, provider_id: str):
        """Attempt to reconnect a specific provider."""
        with self._sync_lock:
            existing = self._providers.get(provider_id)
            if existing is not None and existing.is_connected:
                state = self._reconnect_state.get(provider_id, {})
                state["attempts"] = 0
                return
            attempt = self._reconnect_state.get(provider_id, {}).get("attempts", 0)
        logger.info(f"Attempting auto-reconnect for {provider_id} (attempt {attempt})...")
        ok = self.load_provider(provider_id)
        if ok:
            logger.info(f"Auto-reconnect successful for {provider_id}")
            with self._sync_lock:
                state = self._reconnect_state.get(provider_id, {})
                state["attempts"] = 0
        else:
            logger.warning(f"Auto-reconnect failed for {provider_id} (attempt {attempt})")
            self._schedule_reconnect(provider_id)

    def reset_reconnect(self, provider_id: str = None):
        """Reset reconnect counter for a specific provider, or all."""
        with self._sync_lock:
            if provider_id:
                state = self._reconnect_state.get(provider_id, {})
                state["attempts"] = 0
                if state.get("timer"):
                    state["timer"].stop()
            else:
                for state in self._reconnect_state.values():
                    state["attempts"] = 0
                    if state.get("timer"):
                        state["timer"].stop()

    # ── Cloud check ──────────────────────────────────────────────────────────

    def resolve_remote_game_folder(self, provider, folder_candidates: list) -> Optional[str]:
        """Find the ACTUAL remote backup folder for a game on *provider*.

        Exact candidate matches win; otherwise folder names are compared
        with version/build tokens ignored (``MyGame-v0.5`` ≡ ``MyGame
        v0.8`` ≡ ``MyGame build12``): install-derived names often embed a
        version that changes with updates while the game stays the same.
        Returns the remote folder name to use, or None.
        """
        from core.constants import version_insensitive_slug
        candidates = [c for c in folder_candidates if c]
        for c in candidates:
            try:
                if provider.remote_exists(f"SaveSync/backup/{c}"):
                    return c
            except Exception:
                continue
        wanted = {version_insensitive_slug(c) for c in candidates}
        wanted.discard("")
        if not wanted:
            return None
        try:
            remote_folders = list(provider.list_all_cloud_backups().keys())
        except Exception:
            return None
        for rf in remote_folders:
            if version_insensitive_slug(rf) in wanted:
                logger.info(f"Remote folder matched version-insensitively: {rf!r}")
                return rf
        return None

    def check_cloud_saves(self, game_id: str, exe_path: str = "", game_name: str = "",
                          computed_folder_name: str | None = None) -> bool:
        """Return True if cloud saves exist on any connected provider.

        Backup zips live under ``SaveSync/backup/<folder>`` (see
        sync_backups) — that is the primary location to check (the bare
        ``SaveSync/<folder>`` path is the legacy raw-save layout). Folder
        matching goes through resolve_remote_game_folder, so a version/
        build suffix that changed since the upload doesn't hide the saves;
        past names from name_history are candidates too.
        """
        from core.constants import get_install_folder_name, get_folder_name_for_save
        candidates = [get_install_folder_name(exe_path, game_name, game_id, computed_folder_name)]
        try:
            from core.library import get_library as _gl
            _e = _gl().get_by_id(game_id)
            if _e is not None:
                for hn in _e.name_history:
                    fn = get_folder_name_for_save(hn, exe_path or "", game_id)
                    if fn not in candidates:
                        candidates.append(fn)
                # Past folders carrying a disambiguation suffix survive only in
                # folder_history (a display name can't reproduce the suffix).
                for fn in (_e.folder_history or []):
                    if fn and fn not in candidates:
                        candidates.append(fn)
        except Exception:
            pass
        for p in self.get_connected_providers():
            try:
                folder = self.resolve_remote_game_folder(p, candidates)
                if folder and self._remote_folder_has_backup_zip(p, folder):
                    return True
                if p.remote_exists(f"SaveSync/{candidates[0]}"):   # legacy raw-save layout
                    return True
            except Exception:
                continue
        return False

    def cloud_name_folders(self, base_folder: str) -> list[str]:
        """Remote backup folders whose base name — with any ``_N`` disambiguation
        suffix stripped — matches *base_folder*, and that actually contain a
        backup zip.

        Two genuinely different games sharing a title land in same-named folders
        (``Alpha``, ``Alpha_2``, …). Counting them lets the unknown-game prompt
        tell "one cloud copy → offer download" from "several same-named copies →
        a real conflict to resolve". Best-effort: returns [] when no provider is
        connected or the provider can't enumerate folders."""
        import re
        if not base_folder:
            return []

        def _base(f: str) -> str:
            # Mirror unique_folder_name's output: it only ever appends _2, _3, …
            return re.sub(r'_\d+$', '', f).casefold()

        target = _base(base_folder)
        found: list[str] = []
        for p in self.get_connected_providers():
            try:
                for f in p._list_remote_folders("SaveSync/backup"):
                    if (_base(f) == target and f not in found
                            and self._remote_folder_has_backup_zip(p, f)):
                        found.append(f)
            except Exception:
                continue
        return found

    def cloud_unique_folder(self, base: str, exclude_id: str = "") -> str:
        """A folder name unique against BOTH the local library and existing
        cloud folders sharing *base*'s name.

        Used ONLY when the user explicitly confirms a same-name game is a
        different one (homonymy), so the new game gets its own cloud folder
        (``Alpha_2``) instead of syncing into — and contaminating — the other
        game's ``Alpha``. Never call this automatically: a legitimately identical
        game on a second machine must keep ``Alpha`` to find its own saves."""
        from core.library import get_library
        cloud = self.cloud_name_folders(base)
        return get_library().unique_folder_name(base, exclude_id, also_taken=cloud)

    def _remote_folder_has_backup_zip(self, provider, folder: str) -> bool:
        """True only if the resolved remote folder actually contains a backup
        ``.zip`` — not just a leftover/empty folder or a stale ``index.json``.
        The provider copy may have been deleted (e.g. via the provider's web UI)
        while the folder lingered, which used to still trigger a "download
        saves?" prompt with nothing to fetch.

        Listing the folder for a real zip is the right discriminator: it fixes
        the "zips gone → no prompt" case AND avoids the index-based false
        negative ("zips present but index.json missing" still prompts). Fails
        OPEN on a listing error so a genuinely-present backup is never hidden;
        a confirmed-empty listing (the folder exists but holds no zip) means
        there is nothing to download.

        Providers swallow their transport errors and return [] — the
        last_list_error contract (set on a REAL error, None on success and
        on the legitimate missing-folder empty) is what lets the fail-open
        actually work for them; the except below covers providers that
        raise instead (LocalProvider and the delegate modes)."""
        try:
            files = provider.list_files(f"SaveSync/backup/{folder}")
        except Exception:
            return True   # cannot verify → never hide a possibly-real backup
        if not files and getattr(provider, "last_list_error", None):
            return True   # listing failed inside the provider → cannot verify
        for f in (files or []):
            try:
                if str(getattr(f, "path", "") or "").lower().endswith(".zip"):
                    return True
            except Exception:
                continue
        return False


_orchestrator: Optional[SyncOrchestrator] = None
_orch_lock = threading.Lock()


def get_orchestrator() -> SyncOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        with _orch_lock:
            if _orchestrator is None:
                _orchestrator = SyncOrchestrator()
    return _orchestrator
