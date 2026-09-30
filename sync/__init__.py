"""
SaveSync - Sync Registry & Orchestrator
Manages provider selection, conflict detection, and sync execution.
"""
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Type

from PySide6.QtCore import QObject, QThread, Signal

from sync.base import SyncProvider, SyncResult, has_published_to
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
                 orphan: bool = False,
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
        self._orphan = bool(orphan)

    def _old_folder_candidates(self, current_folder: str) -> list[str]:
        """Folder names this game has lived under, other than *current_folder*.

        Reconstructed from the display-name history PLUS the actual past
        folder names (folder_history), which keep any disambiguation suffix a
        display name cannot reproduce. Read from the library as well as from
        what the caller passed: several callers never hand a name_history over.
        Folders another live game still owns are left out — a disambiguated
        game ("Foo~ab12") keeps the plain base ("Foo") in its name history, and
        "Foo" may be somebody else's home.
        """
        from core.constants import get_folder_name_for_save
        game_id = self._game_id
        names = list(self._name_history)
        folders: list[str] = []
        try:
            from core.library import get_library as _gl
            _entry = _gl().get_by_id(game_id)
            if _entry is not None:
                names += list(_entry.name_history or [])
                folders += list(_entry.folder_history or [])
        except Exception:
            pass
        candidates = [get_folder_name_for_save(n, self._exe_path, game_id)
                      for n in names] + folders
        out: list[str] = []
        for old_folder in candidates:
            if not old_folder or old_folder == current_folder or old_folder in out:
                continue
            try:
                from core.library import get_library
                if get_library().folder_name_in_use_by_other(old_folder, game_id):
                    continue
            except Exception:
                pass
            out.append(old_folder)
        return out

    def _recorded_moves(self) -> dict:
        """``{provider_id: ["old→new", ...]}`` of remote moves already made for
        this game (GameEntry.cloud_metadata["remote_rehomed"])."""
        try:
            from core.library import get_library
            e = get_library().get_by_id(self._game_id)
            return dict((e.cloud_metadata or {}).get("remote_rehomed") or {}) if e else {}
        except Exception:
            return {}

    def _record_move(self, provider_id: str, pair: str) -> None:
        try:
            from core.library import get_library
            lib = get_library()
            e = lib.get_by_id(self._game_id)
            if e is None:
                return
            meta = dict(e.cloud_metadata or {})
            moves = {k: list(v) for k, v in (meta.get("remote_rehomed") or {}).items()}
            if pair not in moves.setdefault(provider_id, []):
                moves[provider_id].append(pair)
            meta["remote_rehomed"] = moves
            lib.update_game_fields(self._game_id, cloud_metadata=meta)
        except Exception:
            logger.debug("could not record the remote move", exc_info=True)

    def _migrate_remote_folders(self, providers: list, current_folder: str, bm) -> dict:
        """Re-home this game's remote backups from old-name folders into
        *current_folder*, on every provider that still has the old folder.

        Remote backups live under ``SaveSync/backup/<folder>`` (see
        SyncProvider.sync_backups); this used to look one level up, at the
        legacy ``SaveSync/<folder>``, so a renamed game never found its old
        folder and the sync that followed met an empty folder under the new
        name. Returns ``{provider_id: {"gone": [...], "trimmed": {...}}}`` —
        old folders that were emptied and removed, and old folders that still
        hold somebody else's backups, with the entries left in their index —
        so the master index can follow.
        """
        candidates = self._old_folder_candidates(current_folder)
        report: dict = {}
        if not candidates:
            return report
        # One move per (provider, old → new) pair, ever. The name history never
        # shrinks, so without this every later sync would look at the old
        # folder again — and if another machine has since started it over on
        # purpose ("keep both"), take that away too.
        done = self._recorded_moves()
        for provider in providers:
            for old_folder in candidates:
                pair = f"{old_folder}→{current_folder}"
                if pair in done.get(provider.PROVIDER_ID, []):
                    # Moved before. What it can still have left behind is an
                    # empty folder (a delete that did not go through then).
                    self._tidy_empty_old_folder(provider, old_folder)
                    continue
                try:
                    res = self._rehome_remote_folder(
                        provider, old_folder, current_folder, bm)
                except Exception as e:
                    logger.warning(
                        f"[{provider.PROVIDER_ID}] Remote folder move "
                        f"{old_folder} → {current_folder} failed: {e}")
                    continue
                if res is None:
                    continue   # nothing moved (or could not be): try again next time
                self._record_move(provider.PROVIDER_ID, pair)
                rep = report.setdefault(
                    provider.PROVIDER_ID, {"gone": [], "trimmed": {}})
                if res["old_left"] is None:
                    rep["gone"].append(old_folder)
                else:
                    rep["trimmed"][old_folder] = res["old_left"]
        return report

    def _tidy_empty_old_folder(self, provider, old_folder: str) -> None:
        """Remove *old_folder* on the provider when — and only when — nothing is
        in it. The folders offered here are this game's own past homes (see
        _old_folder_candidates), and an empty one is a leftover of its rename."""
        base = f"SaveSync/backup/{old_folder}"
        try:
            if not provider.remote_exists(base):
                return
            if provider.list_files(base) or getattr(provider, "last_list_error", None):
                return
            if provider.delete_remote(base):
                logger.info(f"[{provider.PROVIDER_ID}] Removed the empty old folder {old_folder}")
        except Exception:
            logger.debug(f"[{provider.PROVIDER_ID}] Could not tidy {old_folder}", exc_info=True)

    def _rehome_remote_folder(self, provider, old_folder: str,
                              current_folder: str, bm) -> Optional[dict]:
        """Move this game's zips — and the index that describes them — from
        one remote folder to another. None when there was nothing of this
        game to move or the old folder could not be read.

        Order matters and is what makes a half-finished move recoverable:
        every zip goes up to the new folder first, THEN the new index, and
        only then does anything come off the old folder. A run that stops
        after the zips leaves the old folder whole; one that stops after the
        index leaves the new folder whole. Never a folder with zips and no
        index, which the next sync would have to read as a failed fetch.

        Only backups this game owns move (its own rows in the local index).
        Whatever else sits in the old folder is another game's and stays,
        with its index trimmed instead of deleted.
        """
        import json as _json
        import tempfile
        from pathlib import Path
        from core.backup import BACKUP_DIR

        old_base = f"SaveSync/backup/{old_folder}"
        new_base = f"SaveSync/backup/{current_folder}"
        if not provider.remote_exists(old_base):
            return None
        old_files = provider.list_files(old_base)
        if not old_files and getattr(provider, "last_list_error", None):
            return None   # could not verify what is there — touch nothing

        own = {e.backup_id: e for e in bm.get_backups_for_game(self._game_id)}
        game_id = self._game_id

        def _name(rf) -> str:
            return rf.path.replace("\\", "/").split("/")[-1]

        # (backup_id, file name, local zip — None when only the provider has it)
        to_move: list[tuple[str, str, Optional[Path]]] = []
        for rf in old_files:
            fname = _name(rf)
            if not fname.lower().endswith(".zip"):
                continue
            stem = fname[:-4]
            if stem not in own and game_id not in fname:
                continue
            candidates = []
            if stem in own and own[stem].zip_path:
                candidates.append(Path(own[stem].zip_path))
            candidates += [BACKUP_DIR / current_folder / fname,
                           BACKUP_DIR / old_folder / fname]
            # No local copy is not a reason to leave it behind: local retention
            # prunes zips that stay listed remotely, and one left in the old
            # folder would keep that folder (and a trimmed index) alive for
            # good. It is relayed through a temp file instead.
            to_move.append((stem, fname, next((p for p in candidates if p.exists()), None)))
        old_names = {_name(rf) for rf in old_files}

        # The old folder's index, read now: it can be all that is left there. An
        # earlier version moved the zips and left the index behind — the new
        # folder then had zips and no index, which a sync must refuse to build
        # from one machine's rows — and this game's entries in that index still
        # have to travel, for zips that are ALREADY under the new name.
        old_idx_entries = provider.list_cloud_backups(old_folder)
        mine_in_index = [d for d in old_idx_entries
                         if d.get("backup_id")
                         and (d["backup_id"] in own or game_id in d["backup_id"])]
        if not to_move and not mine_in_index:
            return None

        new_files = provider.list_files(new_base)
        if not new_files and getattr(provider, "last_list_error", None):
            return None
        new_names = {_name(rf) for rf in new_files}

        # 1. Zips into the new folder.
        moved: list[tuple[str, str]] = []
        for stem, fname, local in to_move:
            if fname in new_names:
                moved.append((stem, fname))
                continue
            relay = None
            try:
                if local is None:
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
                        relay = Path(tf.name)
                    if not provider.download(f"{old_base}/{fname}", relay):
                        logger.warning(
                            f"[{provider.PROVIDER_ID}] Could not fetch {fname} from {old_folder}")
                        continue
                    local = relay
                if provider.upload(local, f"{new_base}/{fname}"):
                    moved.append((stem, fname))
                else:
                    logger.warning(
                        f"[{provider.PROVIDER_ID}] Could not copy {fname} to {current_folder}")
            finally:
                if relay is not None:
                    relay.unlink(missing_ok=True)
        # Entries whose zip is already where it belongs: nothing to copy, but
        # their rows go into the new index all the same.
        already = {d["backup_id"] for d in mine_in_index
                   if f'{d["backup_id"]}.zip' in new_names}
        if not moved and not already:
            return None
        moved_ids = {stem for stem, _ in moved} | already

        # 2. The index, before anything leaves the old folder.
        entries = [d for d in old_idx_entries if d.get("backup_id") in moved_ids]
        listed = {d.get("backup_id") for d in entries}
        for stem in moved_ids - listed:
            row = own.get(stem)
            if row is not None:
                entries.append(bm.publishable_dict(row))
        # Every row that moves carries the game's CURRENT title: the old index
        # still names it as it was, and the move would otherwise keep the old
        # title alive under the new folder. The old one stays on the row as a
        # reference (name_history), so the game is still recognisable by it.
        if self._game_name:
            from core.library import get_library, reference_history
            try:
                _e = get_library().get_by_id(game_id)
                lib_history = list(_e.name_history or []) if _e is not None else []
            except Exception:
                lib_history = []
            entries = [dict(d, game_name=self._game_name,
                            name_history=reference_history(
                                d.get("name_history"), [d.get("game_name")],
                                lib_history, current=self._game_name))
                       for d in entries]
        # A row this machine made says where its zip is read from; that is now
        # the new folder, and the old path would name a folder that is gone.
        # Another machine's rows are left as it wrote them (the same rule
        # sync_backups follows when it republishes an index).
        from core.machine import get_machine_id
        me = get_machine_id()
        entries = [dict(d, zip_path=own[d["backup_id"]].zip_path)
                   if (d.get("backup_id") in own
                       and getattr(own[d["backup_id"]], "machine_id", None) == me
                       and getattr(own[d["backup_id"]], "zip_path", ""))
                   else d
                   for d in entries]
        merged = list(provider.list_cloud_backups(current_folder))
        seen = {d.get("backup_id") for d in merged}
        merged += [d for d in entries if d.get("backup_id") not in seen]
        idx_tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                             encoding="utf-8") as f:
                _json.dump(merged, f, indent=2)
                idx_tmp = Path(f.name)
            if not provider.upload(idx_tmp, f"{new_base}/index.json"):
                logger.warning(
                    f"[{provider.PROVIDER_ID}] Could not write the index of "
                    f"{current_folder}; old folder {old_folder} left as it was")
                return None
        finally:
            if idx_tmp is not None:
                idx_tmp.unlink(missing_ok=True)

        # 3. Off the old folder — the zips that were there; the ones already under
        # the new name were never in it.
        for _stem, fname in moved:
            if fname not in old_names:
                continue
            try:
                provider.delete_remote(f"{old_base}/{fname}")
            except Exception as e:
                logger.debug(f"[{provider.PROVIDER_ID}] Could not delete old {fname}: {e}")
            logger.info(f"[{provider.PROVIDER_ID}] Migrated {fname}: "
                        f"{old_folder} → {current_folder}")

        # 4. What is left of the old folder. "Empty" means empty of everything
        # but the index: a folder named after a shared title can be a
        # homonym's home, and deleting the index that describes ITS backups
        # would cost that game its history.
        left = [d for d in old_idx_entries if d.get("backup_id") not in moved_ids]
        old_idx = f"{old_base}/index.json"
        remaining = [f for f in provider.list_files(old_base)
                     if _name(f) != "index.json"]
        if not remaining:
            try:
                provider.delete_remote(old_idx)
                # The folder itself — but only if a fresh listing says nothing
                # is in it any more. A cloud provider's folder delete is
                # normally recursive, so it is never issued on the strength of
                # a listing taken before the index came off.
                if not provider.list_files(old_base) and not getattr(
                        provider, "last_list_error", None):
                    provider.delete_remote(old_base)
            except Exception as e:
                logger.debug(f"[{provider.PROVIDER_ID}] Could not clear {old_folder}: {e}")
            return {"moved": len(moved), "old_left": None}
        if old_idx_entries and len(left) != len(old_idx_entries):
            trim_tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                                 encoding="utf-8") as f:
                    _json.dump(left, f, indent=2)
                    trim_tmp = Path(f.name)
                provider.upload(trim_tmp, old_idx)
            finally:
                if trim_tmp is not None:
                    trim_tmp.unlink(missing_ok=True)
        return {"moved": len(moved), "old_left": left}

    def _sync_one_provider(self, provider: SyncProvider, bm, game_folder,
                           rehomed: Optional[dict] = None) -> SyncResult:
        """Run sync_backups against a single provider.

        *rehomed* is this provider's entry of _migrate_remote_folders' report
        (old folders emptied / trimmed by a rename), so the master index can
        follow the move.
        """
        rehomed = rehomed or {}

        def _on_progress(up, down, bytes_total):
            self.progress.emit(
                f"⟳ {self._game_name} [{provider.PROVIDER_ID}]: ↑{up} ↓{down} ({bytes_total // 1024}KB)"
            )

        # "Keep both" on the rename notification (see
        # SyncOrchestrator.find_renamed_remote_folder): this game's old remote
        # folder is gone on purpose, so a missing index there is expected.
        allow_fresh = False
        try:
            from core.library import get_library as _gl
            _e = _gl().get_by_id(self._game_id)
            allow_fresh = bool(
                _e is not None
                and game_folder in ((_e.cloud_metadata or {}).get("remote_folder_reset") or []))
        except Exception:
            pass

        result = provider.sync_backups(
            self._game_id,
            game_folder,
            bm,
            direction=self._direction,
            progress_callback=_on_progress,
            allow_fresh_index=allow_fresh,
        )

        # Refresh the master index whenever anything actually transferred —
        # NOT gated on result.success (sync_backups' own internal remote
        # index.json update, a few lines above where it returns, uses this
        # same looser condition on purpose: one failed file in a batch of
        # several does not undo the ones that genuinely went up). Without
        # this, a partial failure — say 2 of 3 backups uploaded, the 3rd
        # hit a network error — left result.success False and skipped this
        # refresh entirely, even though list_cloud_backups() below re-fetches
        # the REAL remote index.json (already correctly updated inside
        # sync_backups to include exactly the 2 that succeeded) rather than
        # trusting result's own bookkeeping — so running this here after a
        # partial failure is accurate, not a risk. Skipping it just left the
        # LOCAL index stale until some later, fully-successful sync happened
        # to refresh it, and every sync until then treated already-uploaded
        # backups as still needing to be checked from scratch.
        # Limit enforcement only applies when new backups were uploaded.
        #
        # A run that only republished the index (a rename, a note, a changed
        # row) or that re-homed a renamed game's folder transfers nothing, so
        # the counts alone left the master index describing the old folder.
        # Those two triggers additionally require an index that could be read
        # back: an empty list there means the fetch failed, and writing it
        # over the game's master entry would erase it.
        transferred = result.files_uploaded > 0 or result.files_downloaded > 0
        republished = result.index_published or bool(rehomed)
        if transferred or republished:
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
                if remote_entries is not None and (transferred or remote_entries):
                    with SyncWorker._master_index_lock:
                        provider.update_master_index(
                            game_folder, remote_entries,
                            remove_folders=list(rehomed.get("gone") or []))
                        # Old folders that still hold another game's backups
                        # keep their master row, minus what moved out.
                        for old_folder, left in (rehomed.get("trimmed") or {}).items():
                            provider.update_master_index(old_folder, left)
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

            # Ensure a fresh backup exists before uploading. Prefer a single
            # mtime preflight here; if already current, skip create_backup
            # entirely (avoids a second identical walk inside create_backup).
            if self._direction in ("auto", "up"):
                if self._orphan:
                    # An archive is read from the folders the USER handed
                    # over and recorded on it, which are not the same list
                    # as the destinations its index carries — a collection
                    # copy on D:\ restores into the profile, and a relative
                    # chain like "www/save" restores under a game that may
                    # not exist yet. Backing one up from save_paths meant
                    # zipping the DESTINATION: the archive's contents
                    # silently became the live folder, the zip root lost the
                    # source folder's name, the chains came back empty
                    # because an archive has no library row to derive them
                    # from, and the destination was merged into the source
                    # list — so the next re-backup read both and produced a
                    # zip holding two copies of the same saves.
                    #
                    # rebackup_archive is the one function that knows the
                    # difference. It runs the same mtime preflight (force=
                    # False) and reports "unchanged" without writing a zip,
                    # which is the skip this branch was reaching for.
                    try:
                        created, detail = bm.rebackup_archive(self._game_id)
                        if not created:
                            logger.debug(
                                "Sync: archive %r not re-backed up: %s",
                                self._game_name, detail)
                    except Exception:
                        logger.exception(
                            "Sync: pre-sync archive backup failed for %r",
                            self._game_name)
                else:
                    save_paths = [str(p) for p in self._save_paths]
                    if bm.is_backup_current(
                        self._game_id, save_paths,
                        excluded_paths=self._excluded_paths,
                    ):
                        logger.debug(
                            "Sync skip local backup for %r — mtime already current",
                            self._game_name,
                        )
                    else:
                        bm.create_backup(
                            self._game_id, self._game_name, save_paths,
                            exe_path=self._exe_path,
                            computed_folder_name=self._computed_folder_name,
                            name_history=self._name_history,
                            excluded_paths=self._excluded_paths,
                            skip_mtime_preflight=True,
                        )

            # A rename that never produced a backup left the zips in the old
            # local folder — create_backup is what moves them, and a sync that
            # finds the saves unchanged never gets that far. Same for the
            # remote side below, which only a sync can do.
            rehomed: dict = {}
            if not self._orphan and self._direction in ("auto", "up"):
                try:
                    bm.consolidate_game_folder(self._game_id, game_folder)
                except Exception:
                    logger.debug("Sync: local folder consolidation failed", exc_info=True)
                # The index is published from these rows: they carry the
                # game's current title, whatever it was called when each was made.
                try:
                    bm.retitle_rows(self._game_id, self._game_name)
                except Exception:
                    logger.debug("Sync: retitling the backup rows failed", exc_info=True)

                # Move the remote folder of any old name into the current one
                # BEFORE syncing, so the index under the new name exists
                # (see SyncProvider.sync_backups' failed-fetch guard).
                try:
                    rehomed = self._migrate_remote_folders(active, game_folder, bm)
                except Exception:
                    logger.warning("Sync: remote folder migration failed", exc_info=True)

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
                    result = self._sync_one_provider(
                        provider, bm, game_folder,
                        rehomed=rehomed.get(provider.PROVIDER_ID))
                    combined.files_uploaded += result.files_uploaded
                    combined.files_downloaded += result.files_downloaded
                    combined.bytes_transferred += result.bytes_transferred
                    # A folder move re-published the index too, even when the
                    # sync itself then found nothing further to send.
                    combined.index_published = (
                        combined.index_published or result.index_published
                        or bool(rehomed.get(provider.PROVIDER_ID)))
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
    batch_progress    = Signal(int, int, str)  # done, total, current name
    batch_finished    = Signal(int, str)       # done count, last synced name

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
        from collections import deque
        self._sync_job_queue: deque = deque()
        self._sync_batch: dict | None = None
        self._sync_max_inflight: int = 1

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

    def enqueue_sync_batch(self, jobs: list[dict], source: str = "sync_page",
                           prior_completed_ids: list[str] | None = None):
        """Queue Sync Tutti with adaptive concurrency + resume persistence.

        *prior_completed_ids* carries what an interrupted batch had already
        finished. Without it a resume wrote ``completed_ids: []`` and a total
        counting only what was LEFT straight over the persisted job: the
        notice restarted at 0/remaining instead of continuing at done/total,
        and — worse — a second close mid-resume lost the history for good,
        because the record of the first run's completions had been erased.
        Backup Tutti's resume already keeps its tally; this is the same shape.
        """
        from datetime import datetime, timezone
        from core.concurrency import sync_max_inflight, log_limits
        from core import pending_batch_jobs as _pbj
        if not jobs:
            return
        log_limits()
        self._sync_max_inflight = sync_max_inflight()
        ids = [j["game_id"] for j in jobs if j.get("game_id")]
        completed = [g for g in (prior_completed_ids or []) if g not in ids]
        total = len(ids) + len(completed)
        self._sync_batch = {
            "pending_ids": list(ids),
            "completed_ids": completed,
            "total": total,
            "source": source or "sync_page",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _pbj.set_job(_pbj.KEY_SYNC_ALL, {
            "pending_ids": list(ids),
            "completed_ids": list(completed),
            "jobs": list(jobs),
            "started_at": self._sync_batch["started_at"],
            "source": source or "sync_page",
        })
        first_name = (jobs[0].get("game_name") or "") if jobs else ""
        self.batch_progress.emit(len(completed), total, first_name)
        self._orphan_synced_pending: set[str] = set()
        for j in jobs:
            self.sync_game(
                j["game_id"], j.get("game_name") or "",
                j.get("save_paths") or [],
                direction=j.get("direction") or "auto",
                exe_path=j.get("exe_path") or "",
                computed_folder_name=j.get("computed_folder_name"),
                name_history=j.get("name_history"),
                excluded_paths=j.get("excluded_paths"),
                orphan=bool(j.get("orphan")),
                _batch=True,
            )

    def sync_game(self, game_id: str, game_name: str, save_paths: list,
                  direction: str = "auto", exe_path: str = "", computed_folder_name: str | None = None,
                  name_history: list[str] | None = None, excluded_paths: list[str] | None = None,
                  orphan: bool = False,
                  _batch: bool = False):
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
        job = {
            "game_id": game_id,
            "game_name": game_name,
            "save_paths": list(save_paths or []),
            "direction": direction,
            "exe_path": exe_path or "",
            "computed_folder_name": computed_folder_name or "",
            "name_history": list(name_history or []),
            "excluded_paths": list(excluded_paths or []) if excluded_paths is not None else None,
            "orphan": bool(orphan),
            "batch": bool(_batch),
        }
        with self._sync_lock:
            if game_id in self._syncing_games:
                logger.warning(f"Sync already in progress for {game_name}, skipping")
                return
            # Also skip if already queued
            if any(j.get("game_id") == game_id for j in self._sync_job_queue):
                logger.warning(f"Sync already queued for {game_name}, skipping")
                return
            self._sync_job_queue.append(job)
        self._pump_sync_queue()

    def _pump_sync_queue(self):
        from core.concurrency import sync_max_inflight
        # Re-asked each pass rather than frozen at the start of the batch —
        # see the note on _pump_backup_queue. A sync job carries a backup
        # AND a network transfer, so it is the one most worth backing off.
        cap = sync_max_inflight()
        self._sync_max_inflight = cap
        max_iterations = max(cap * 4, 64)
        for _ in range(max_iterations):
            with self._sync_lock:
                inflight = len(self._syncing_games)
                if inflight >= cap or not self._sync_job_queue:
                    return
                job = self._sync_job_queue.popleft()
                gid = job["game_id"]
                if gid in self._syncing_games:
                    continue
                self._syncing_games.add(gid)
            self._start_sync_worker(job)

    def _start_sync_worker(self, job: dict):
        game_id = job["game_id"]
        game_name = job.get("game_name") or ""
        connected = self.get_connected_providers()
        if not connected:
            logger.warning("Sync requested but no provider connected")
            with self._sync_lock:
                self._syncing_games.discard(game_id)
            self._mark_sync_batch_done(game_id, game_name)
            self._pump_sync_queue()
            return

        if self._sync_batch and job.get("batch"):
            done = len(self._sync_batch.get("completed_ids") or [])
            total = int(self._sync_batch.get("total") or 0)
            self.batch_progress.emit(done, total, game_name)

        self._cleanup_workers()
        self.sync_started.emit(game_id)
        worker = SyncWorker(
            connected, game_id, game_name, job.get("save_paths") or [],
            job.get("direction") or "auto",
            exe_path=job.get("exe_path") or "",
            computed_folder_name=job.get("computed_folder_name") or "",
            name_history=job.get("name_history") or [],
            excluded_paths=job.get("excluded_paths") or [],
            orphan=bool(job.get("orphan")),
        )

        def _on_done(result, _worker=worker, _gid=game_id, _batch=job.get("batch"),
                     _name=game_name):
            try:
                _worker.finished.disconnect()
                _worker.progress.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._on_sync_done(_gid, result, batch=_batch, game_name=_name)

        from PySide6.QtCore import Qt
        worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(lambda msg: logger.info(msg))
        worker.conflict_detected.connect(
            lambda gid, ldt, rdt: self.conflict_detected.emit(gid, {"local": ldt, "remote": rdt})
        )
        with self._sync_lock:
            self._workers.append(worker)
        worker.start()

    def _mark_sync_batch_done(self, game_id: str, game_name: str = ""):
        from core import pending_batch_jobs as _pbj
        if not self._sync_batch:
            return
        pending = [g for g in (self._sync_batch.get("pending_ids") or []) if g != game_id]
        completed = list(self._sync_batch.get("completed_ids") or [])
        if game_id and game_id not in completed:
            completed.append(game_id)
        self._sync_batch["pending_ids"] = pending
        self._sync_batch["completed_ids"] = completed
        # The completion notice needs the name when exactly ONE game was
        # actually synced (like Backup Tutti shows the name for one backup).
        if game_name:
            self._sync_batch["last_synced_name"] = game_name
        total = int(self._sync_batch.get("total") or 0)
        done = len(completed)
        last_synced_name = self._sync_batch.get("last_synced_name") or ""
        next_name = ""
        if pending:
            # Prefer the queued job's game_name (orphans have no library row).
            want = pending[0]
            with self._sync_lock:
                for j in self._sync_job_queue:
                    if j.get("game_id") == want:
                        next_name = j.get("game_name") or ""
                        break
            if not next_name:
                try:
                    from core.library import get_library as _gl
                    e = _gl().get_by_id(want)
                    next_name = e.name if e else ""
                except Exception:
                    next_name = ""
            if not next_name:
                try:
                    from core.backup import get_backup_manager
                    backs = get_backup_manager().get_backups_for_game(want)
                    if backs:
                        next_name = backs[0].game_name or ""
                except Exception:
                    pass
        self.batch_progress.emit(done, total, next_name)
        # Throttle disk: persist every 8 completions, always on batch end.
        persist = (not pending) or (done % 8 == 0)
        job = _pbj.mark_game_done(_pbj.KEY_SYNC_ALL, game_id, persist=persist)
        if not pending:
            # Flush deferred orphan index stamps + pending-jobs file.
            try:
                from core.backup import get_backup_manager
                pending_ids = list(getattr(self, "_orphan_synced_pending", set()) or [])
                self._orphan_synced_pending = set()
                if pending_ids:
                    get_backup_manager().flush_orphan_indexes(pending_ids)
            except Exception:
                logger.debug("orphan index flush failed", exc_info=True)
            try:
                _pbj.flush()
            except Exception:
                pass
            self._sync_batch = None
            self.batch_finished.emit(done, last_synced_name or "")
            return

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

    def _on_sync_done(self, game_id: str, result: SyncResult,
                      batch: bool = False, game_name: str = ""):
        # Resolve the display name now so history rows survive a game being
        # renamed/removed later.
        if not game_name:
            try:
                from core.library import get_library as _gl
                _e = _gl().get_by_id(game_id)
                game_name = _e.name if _e else ""
            except Exception:
                pass
        if not game_name:
            try:
                from core.backup import get_backup_manager
                _backs = get_backup_manager().get_backups_for_game(game_id)
                if _backs:
                    game_name = _backs[0].game_name or ""
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
        # Retire the one-shot "keep local wins" flag only after a sync that
        # actually moved bytes. An empty success (0↑ 0↓ — e.g. keep-local
        # right after re-adding a game, before any save path exists) used to
        # clear the flag too early; the next auto-sync then downloaded the
        # old cloud history the user had just declined.
        # Not gated on result.success — see the matching comment on the local
        # index refresh above: a partial batch failure still moved real bytes
        # for the files that succeeded, and that bookkeeping should reflect it.
        if result.files_uploaded > 0 or result.files_downloaded > 0:
            try:
                from core.library import get_library as _gl
                _e3 = _gl().get_by_id(game_id)
                if _e3 is not None and getattr(_e3, "pending_local_wins", False):
                    _gl().update_game_fields(game_id, pending_local_wins=False)
            except Exception:
                pass
        # The fresh start "keep both" allowed has happened once the folder has
        # an index of its own again: the escape is spent, and must not stay
        # open for a later, genuinely failed fetch.
        if result.success and (result.files_uploaded > 0 or result.index_published):
            try:
                from core.library import get_library as _gl
                _er = _gl().get_by_id(game_id)
                if _er is not None and (_er.cloud_metadata or {}).get("remote_folder_reset"):
                    _meta = dict(_er.cloud_metadata)
                    _meta.pop("remote_folder_reset", None)
                    _gl().update_game_fields(game_id, cloud_metadata=_meta)
            except Exception:
                pass
        # Stamp the machine_id on cloud_metadata so other machines can detect cross-machine syncs
        if result.files_uploaded > 0 or result.files_downloaded > 0:
            try:
                from core.library import get_library as _gl
                from core.machine import get_machine_id as _mid
                entry = _gl().get_by_id(game_id)
                if entry is not None:
                    if result.files_uploaded > 0:
                        cloud_meta = dict(entry.cloud_metadata or {})
                        cloud_meta["last_sync_machine"] = _mid()
                        # Reset download confirmations since the cloud data changed
                        cloud_meta["download_confirmed_machines"] = [_mid()]
                        _gl().update_game_fields(game_id, cloud_metadata=cloud_meta)
                else:
                    # Orphan archive (Aggiungi percorso): no library row — stamp
                    # synced_to / hash on the backup index instead.
                    from core.backup import get_backup_manager
                    pids = [p.PROVIDER_ID for p in self.get_connected_providers()]
                    backs = get_backup_manager().get_backups_for_game(game_id)
                    h = ""
                    if backs:
                        h = (backs[0].cloud_metadata or {}).get("save_hash") or ""
                    # During Sync Tutti defer index.json writes to batch end.
                    in_batch = bool(batch or self._sync_batch)
                    get_backup_manager().mark_orphan_synced(
                        game_id, provider_ids=pids, save_hash=h,
                        persist=not in_batch)
                    if in_batch:
                        if not hasattr(self, "_orphan_synced_pending"):
                            self._orphan_synced_pending = set()
                        self._orphan_synced_pending.add(game_id)
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
        if batch or self._sync_batch:
            self._mark_sync_batch_done(game_id, game_name)
        self._pump_sync_queue()

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

    def find_renamed_remote_folder(self, game_id: str, current_folder: str) -> Optional[dict]:
        """Where this game's backups went, when ITS remote folder is gone.

        Another machine that renamed the game moves the remote folder to the
        new name (see SyncWorker._migrate_remote_folders), so here the old
        folder is simply missing and every sync would meet an empty one. The
        link between the two is the backups themselves: a backup id is the
        same on every machine, so the folder whose index holds ids this game
        already has locally IS the game, under its new name.

        Conservative on purpose — None (nothing to say) unless the game's own
        folder is confirmed empty, the provider could be read, and some other
        folder holds backups this game already has (the one sharing the most
        wins). Returns ``{"folder", "name", "provider"}``: the folder to follow
        and the title its newest entry from another machine carries. Meant for
        a background thread (network).
        """
        if not game_id or not current_folder:
            return None
        from core.backup import get_backup_manager
        rows = list(get_backup_manager().get_backups_for_game(game_id))
        local_ids = {b.backup_id for b in rows}
        if not local_ids:
            return None
        try:
            from core.machine import get_machine_id
            mine = get_machine_id()
        except Exception:
            mine = ""
        for p in self.get_connected_providers():
            # A game that never published to this provider has no folder there
            # to have lost — same test sync_backups uses for "history exists".
            # Without it every launch of such a game would download the master
            # index (or scan every folder's) for nothing.
            if not has_published_to(p.PROVIDER_ID, rows):
                continue
            try:
                # Its own folder still holds backups (or cannot be checked —
                # this fails open): nothing was renamed as far as we can tell.
                if self._remote_folder_has_backup_zip(p, current_folder):
                    continue
                found = p.list_all_cloud_backups()
            except Exception:
                continue
            best = None
            for folder, entries in (found or {}).items():
                if folder == current_folder or not isinstance(entries, list):
                    continue
                ids = {e.get("backup_id") for e in entries if isinstance(e, dict)}
                overlap = len(ids & local_ids)
                if overlap and (best is None or overlap > best[0]):
                    best = (overlap, folder, entries)
            if best is None:
                continue
            _n, folder, entries = best
            try:
                from core.library import get_library
                if get_library().folder_name_in_use_by_other(folder, game_id):
                    continue   # another game here already lives there
            except Exception:
                pass
            named = sorted((e for e in entries if isinstance(e, dict) and e.get("game_name")),
                           key=lambda e: e.get("created_at") or "", reverse=True)
            others = [e for e in named if e.get("machine_id") != mine]
            pick = (others or named or [None])[0]
            return {"folder": folder, "name": (pick or {}).get("game_name") or folder,
                    "provider": p.PROVIDER_ID}
        return None

    def cloud_name_folders(self, base_folder: str) -> list[str]:
        """Remote backup folders whose base name — with any ``_N`` disambiguation
        suffix stripped — matches *base_folder*, and that actually contain a
        backup zip.

        Two genuinely different games sharing a title land in same-named folders
        (``Alpha``, ``Alpha~7f31c0``, …). Counting them lets the unknown-game prompt
        tell "one cloud copy → offer download" from "several same-named copies →
        a real conflict to resolve". Best-effort: returns [] when no provider is
        connected or the provider can't enumerate folders."""
        import re
        if not base_folder:
            return []

        def _base(f: str) -> str:
            # Undo whatever unique_folder_name appended. Shared with the
            # function that appends it (core.constants) rather than spelled
            # out here: this used to strip _2/_3 with its own regex, and when
            # the distinguishing tag stopped being a number — because "_2" is
            # indistinguishable from a sequel — a local copy of the rule would
            # simply have stopped matching, silently, and same-named cloud
            # folders would no longer have been recognised as related.
            from core.constants import strip_disambiguation_tag
            return strip_disambiguation_tag(f).casefold()

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
        (``Alpha~7f31c0``) instead of syncing into — and contaminating — the
        other game's ``Alpha``. Never call this automatically: a legitimately
        identical game on a second machine must keep ``Alpha`` to find its own
        saves."""
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

    def delete_cloud_backup(self, provider, folder: str, backup_id: str, bm) -> tuple:
        """Delete a backup that exists ONLY on *provider*: its zip, its row in
        the folder's index and in the master index, and the folder itself when
        that leaves it empty. ``(True, "")``, or ``(False, reason)`` with the
        reason "local" (a local copy exists), "zip" (the provider would not
        delete it) or "error".

        The two sides are separate things and are never deleted together: a
        backup that still has a local copy is refused here — deleting locally
        never touched the cloud, and deleting from the cloud never touches
        the local one. Blocking (provider calls): run it off the GUI thread.

        The zip goes first, so a provider that refuses leaves everything as
        it was; only then is the index rewritten. Another machine that still
        holds the backup locally will publish it again at its next sync — a
        delete here is a delete on THIS provider, not a tombstone."""
        import json as _json
        import tempfile
        if bm.get_backup(backup_id) is not None:
            return False, "local"
        base = f"SaveSync/backup/{folder}"
        try:
            rows = list(provider.list_cloud_backups(folder))
            zip_path = f"{base}/{backup_id}.zip"
            if provider.remote_exists(zip_path) and not provider.delete_remote(zip_path):
                return False, "zip"
            left = [r for r in rows if r.get("backup_id") != backup_id]
            if left:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                                 encoding="utf-8") as f:
                    _json.dump(left, f, indent=2)
                    tmp = Path(f.name)
                try:
                    if not provider.upload(tmp, f"{base}/index.json"):
                        return False, "error"
                finally:
                    tmp.unlink(missing_ok=True)
                provider.update_master_index(folder, left)
            else:
                index_path = f"{base}/index.json"
                if provider.remote_exists(index_path):
                    provider.delete_remote(index_path)
                # The folder goes only when nothing at all is left in it.
                if (not provider.list_files(base)
                        and not getattr(provider, "last_list_error", None)):
                    provider.delete_remote(base)
                provider.update_master_index(folder, None)
        except Exception:
            logger.warning(f"[{getattr(provider, 'PROVIDER_ID', '?')}] Could not delete "
                           f"{backup_id} from {folder}", exc_info=True)
            return False, "error"
        logger.info(f"[{provider.PROVIDER_ID}] Deleted cloud backup {backup_id} from {folder}")
        return True, ""

    @staticmethod
    def _local_root_of(provider):
        """The folder on THIS disk a provider works out of — None for an API
        provider. A synced OneDrive / Dropbox / Drive folder and a plain one
        both are: the delegate holds the folder."""
        for candidate in (provider, getattr(provider, "_local_delegate", None)):
            root = getattr(candidate, "_root", None)
            if root is not None:
                return Path(root)
        return None

    def local_zip_index(self, provider):
        """``{folder (casefolded): {zip names}}`` for a provider's whole backup
        tree, or None when that cannot be said cheaply.

        Read off the disk — directory names only, never file contents:
        list_files hashes every file it lists, which for a tree of zips is
        every byte of it (and, in a synced folder, can pull placeholders
        down). What lets the Backups tab list a cloud entry only when there
        is a backup behind it. None for an API provider or a tree that
        cannot be read, and then nothing is hidden. Read-only: nothing here
        deletes or writes anything, on either side."""
        root = self._local_root_of(provider)
        if root is None:
            return None
        out: dict = {}
        try:
            with os.scandir(root / "SaveSync" / "backup") as entries:
                folders = [e for e in entries if e.is_dir()]
            for e in folders:
                with os.scandir(e.path) as files:
                    out[e.name.casefold()] = {f.name for f in files
                                              if f.name.lower().endswith(".zip")}
        except OSError:
            return None       # one folder unreadable: no claim about any
        return out


_orchestrator: Optional[SyncOrchestrator] = None
_orch_lock = threading.Lock()


def get_orchestrator() -> SyncOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        with _orch_lock:
            if _orchestrator is None:
                _orchestrator = SyncOrchestrator()
    return _orchestrator
