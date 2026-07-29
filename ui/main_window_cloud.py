"""
SaveSync - Cloud-save discovery + cross-machine conflict flows.

CloudFlowsMixin hosts the MainWindow methods extracted verbatim for the
"cloud saves exist for this game/machine" journey: the launch-time cloud
check (backgrounded, resolved via _on_cloud_check_result), the
download/restore hand-off, unknown-game cloud discovery + verify dialog,
the cross-machine divergence prompt (_on_conflict_detected) and the user's
resolution (_handle_conflict_choice, incl. the session-scoped "keep local"
up-only mode). MainWindow provides every attribute these methods touch
(_overlay, _cloud_check_lock, _pending_cloud_*, _cross_machine_local_only,
…); the mixin MUST precede QMainWindow in the MRO.
"""
import logging
from typing import Callable, Optional

from PySide6.QtCore import Slot

from i18n import t
from core.config_manager import get_config
from core.library import get_library
from sync import get_orchestrator

logger = logging.getLogger(__name__)


class CloudFlowsMixin:

    def _persist_cloud_no_local_decline(self, game_id: str, name: str = ""):
        """Persist a per-game "use local saves — don't re-prompt to download at
        launch" decision, so the cloud-download prompt is not re-shown on every
        restart. game_id-keyed in config['suppressed_cloud_no_local'] and
        checked by the no_local gate in _on_cloud_check_result; reversible from
        Settings → suppressed-games list."""
        cfg = get_config()
        suppressed = list(cfg.get("suppressed_cloud_no_local", []))
        if game_id not in suppressed:
            suppressed.append(game_id)
            cfg.set("suppressed_cloud_no_local", suppressed)
            logger.info(f"Cloud-download prompt suppressed for {name or game_id!r} (user chose local saves)")


    def _check_cloud_on_launch(self, game_id: str, on_resolved: Optional[Callable] = None):
        """Check for cloud saves when a game launches and, if appropriate,
        show an in-game yes/no prompt to download/restore them — the same
        action pattern as "unknown game with cloud saves → download & add
        to library", minus the add-to-library step since the game is
        already known.

        *on_resolved*, if given, is called once the (backgrounded) network
        check has completed — see _on_cloud_check_result(). It fires as
        soon as the check itself resolves, not once the user has answered
        any prompt that check may result in: a "no local backup yet, want
        to download?" question is exactly the situation where the watcher
        and in-game backup timer (what on_resolved starts, in
        _start_tracking_after_cloud_check) matter most, so they must not
        wait on how long the player takes to notice/answer an overlay.

        The actual network round-trip (orch.check_cloud_saves) runs in a
        background thread: it used to run directly on the GUI thread,
        meaning a slow or unresponsive provider could stall the whole app —
        including the overlay itself — for as long as the request took,
        while the player was already in-game.
        """
        entry = get_library().get_by_id(game_id)
        if entry is None:
            if on_resolved:
                on_resolved()
            return

        orch = get_orchestrator()
        if not orch.is_online():
            if on_resolved:
                on_resolved()
            return

        if on_resolved:
            with self._cloud_check_lock:
                self._cloud_check_on_resolved[game_id] = on_resolved

        import threading
        _game_id = game_id
        _exe_path = entry.exe_path
        _name = entry.name
        _cfn = entry.computed_folder_name

        def _do_check():
            try:
                has_cloud = orch.check_cloud_saves(
                    _game_id, exe_path=_exe_path, game_name=_name,
                    computed_folder_name=_cfn,
                )
            except Exception as e:
                logger.debug(f"_check_cloud_on_launch: check_cloud_saves failed: {e}")
                has_cloud = False
            with self._cloud_check_lock:
                self._cloud_check_results[_game_id] = has_cloud
            from PySide6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
            try:
                QMetaObject.invokeMethod(
                    self, "_on_cloud_check_result", _Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, _game_id),
                )
            except RuntimeError:
                pass

        threading.Thread(target=_do_check, daemon=True).start()


    @Slot(str)
    def _on_cloud_check_result(self, game_id: str):
        """GUI-thread continuation of _check_cloud_on_launch, run once the
        (possibly slow) network check has completed in a background thread.
        Everything below is local/cheap — no I/O — so it's safe to run
        directly here.
        """
        with self._cloud_check_lock:
            has_cloud = self._cloud_check_results.pop(game_id, False)
            on_resolved = self._cloud_check_on_resolved.pop(game_id, None)

        # One-shot suppression: the user already answered the cloud question
        # through another flow (e.g. "download & add" / "add without
        # downloading" on the unknown-game notification) — don't re-prompt.
        if game_id in self._suppress_cloud_prompt_once:
            self._suppress_cloud_prompt_once.discard(game_id)
            has_cloud = False

        entry = get_library().get_by_id(game_id) if has_cloud else None

        # Decide FIRST whether a cloud notification will actually be shown,
        # before calling on_resolved — that decision is exactly what
        # on_resolved needs (as show_toast) to avoid firing the plain
        # "tracking" toast an instant before a cloud prompt overwrites it
        # on the same overlay widget. Getting this backwards (call
        # on_resolved unconditionally, decide about the notification after)
        # is what caused a visible flicker: two show_animated() calls in the
        # same synchronous pass, each cancelling and restarting the other's
        # fade-in within milliseconds.
        notification_kind = None   # "different_machine" | "no_local" | None
        if entry is not None and self._overlay is not None:
            from core.machine import get_machine_id
            machine_id = get_machine_id()
            cloud_meta = entry.cloud_metadata or {}
            last_machine = cloud_meta.get("last_sync_machine", "")
            confirmed_machines: list = cloud_meta.get("download_confirmed_machines", [])

            if (last_machine and last_machine != machine_id
                    and machine_id not in confirmed_machines):
                if get_config().get("show_overlay_on_cloud", True):
                    notification_kind = "different_machine"
            elif get_config().get("show_overlay_on_cloud", True):
                from core.backup import get_backup_manager
                has_local = bool(get_backup_manager().get_backups_for_game(entry.id))
                if not has_local:
                    if entry.id not in get_config().get("suppressed_cloud_no_local", []):
                        notification_kind = "no_local"
                elif entry.sync_status in ("local_only", "cloud_only", "pending"):
                    notification_kind = "sync_prompt"

        if on_resolved:
            on_resolved(show_toast=(notification_kind is None))

        if notification_kind == "different_machine":
            # Same non-blocking overlay pattern as every other cloud
            # notification — replaces a previous blocking QMessageBox tied
            # to the (often hidden, while in-game) main window, which could
            # appear behind a fullscreen game or never be seen at all, and
            # froze the GUI thread until answered.
            self._pending_cloud_notification[game_id] = "different_machine"
            self._overlay.show_cloud_saves_different_machine(entry.name, entry.exe_path)
        elif notification_kind == "no_local":
            self._pending_cloud_notification[game_id] = "no_local"
            self._overlay.show_cloud_saves_no_local(entry.name, entry.exe_path)
        elif notification_kind == "sync_prompt":
            # Anything other than a confirmed two-way "synced" state means
            # the local copy hasn't been reconciled with the cloud copy.
            self._overlay.show_cloud_saves(entry.name, entry.exe_path)


    def _mark_cloud_machine_confirmed(self, game_id: str):
        """Record that this machine has already been asked about a cloud
        save uploaded elsewhere, so the prompt isn't repeated for the same
        cloud version on every subsequent launch."""
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        from core.machine import get_machine_id
        machine_id = get_machine_id()
        cloud_meta = dict(entry.cloud_metadata or {})
        confirmed = list(cloud_meta.get("download_confirmed_machines", []))
        if machine_id not in confirmed:
            confirmed.append(machine_id)
        cloud_meta["download_confirmed_machines"] = confirmed
        get_library().update_game_fields(game_id, cloud_metadata=cloud_meta)


    def _restore_after_cloud_download(self, game_id: str):
        """Apply the most recently downloaded cloud backup to the game's save directory.

        Flow:
          1. Find the most recent local backup for this game (just synced-down).
          2. Call restore_backup() — it resolves the right save location on
             *this* machine itself (username substitution, then a fresh
             auto-detect scan if needed) and only writes the result back into
             the library entry once it's actually confirmed valid here. We
             deliberately do NOT pre-seed entry.save_paths from the backup's
             own metadata: that metadata is the *other* machine's path and,
             written in blind, would leave a permanently wrong path on record
             even when resolution later fails.
          3. If files failed (locked / wrong path), offer a force-restore dialog.
        """
        from core.backup import get_backup_manager
        from core.library import get_library

        entry = get_library().get_by_id(game_id)
        if not entry:
            return

        bm = get_backup_manager()
        backups = bm.get_backups_for_game(game_id)
        if not backups:
            # Cross-PC backups may still be filed under the originating
            # machine's game_id (import normally re-files them, but cover the
            # case where that hasn't happened yet): find them by the stable
            # name-derived storage folder instead.
            try:
                from core.constants import get_install_folder_name
                folder = get_install_folder_name(
                    entry.exe_path or "", entry.name, entry.id,
                    entry.computed_folder_name)
                backups = bm.get_backups_for_folder(folder)
            except Exception as _e:
                logger.debug(f"_restore_after_cloud_download: folder fallback failed: {_e}")
        if not backups:
            logger.warning(f"_restore_after_cloud_download: no backups found for {entry.name}")
            return

        latest = max(backups, key=lambda b: b.created_dt)

        logger.info(f"Restoring cloud backup {latest.backup_id} for {entry.name}")
        # Pass the LOCAL game_id so cross-PC path resolution finds this
        # machine's library entry even if the backup is still filed under the
        # originating machine's game_id (fallback lookup above).
        result = bm.restore_backup(latest.backup_id, lib_game_id=game_id)

        if result.success and not result.failed:
            logger.info(f"Cloud backup restore successful for {entry.name}")
            return

        # Some files failed — offer force restore
        if result.failed:
            failed_files = {f.arc_name for f in result.failed}
            msg = (
                f"{entry.name}\n\n"
                + t("restore.files_failed", count=len(failed_files))
                + "\n\n"
                + t("restore.force_restore_question")
            )
            from ui.modal_helpers import question_window_modal
            from PySide6.QtWidgets import QMessageBox
            reply = question_window_modal(
                self._main_window if hasattr(self, '_main_window') else None,
                t("restore.title"),
                msg,
            )
            if reply == QMessageBox.StandardButton.Yes:
                bm.restore_backup(latest.backup_id, only_files=failed_files,
                                  lib_game_id=game_id)

        elif not result.success:
            logger.warning(
                f"Cloud backup restore failed for {entry.name}: {result.errors}"
            )


    def _cloud_folder_registration(self, provider, folder: str):
        """(registered_name, registered_path) from the most recent backup in a
        cloud folder — shows the user what a cloud copy actually belongs to."""
        try:
            entries = provider.list_cloud_backups(folder)
            if entries:
                latest = max(entries, key=lambda e: e.get("created_at", ""))
                return (latest.get("game_name", ""), latest.get("exe_path", ""))
        except Exception:
            pass
        return ("", "")


    @Slot()
    def _process_cloud_found_unknown(self):
        """Main-thread: dispatch queued unknown-game cloud-check results.

        One cloud folder with this name → normal download prompt (dropdown can
        open a details check). Several same-named folders → a real conflict, so
        the primary action opens the verify-conflicts dialog. The candidate
        folders are stashed by exe_path for whichever dialog the user opens."""
        with self._cloud_found_lock:
            results = list(self._pending_cloud_found)
            self._pending_cloud_found.clear()
        for name, exe_path, cloud_meta in results:
            if not self._overlay:
                continue
            folders = (cloud_meta or {}).get("folders") if cloud_meta else None
            if not folders:
                self._overlay.show_game_detected(name, exe_path)
                continue
            self._pending_cloud_verify[exe_path] = {"name": name, "folders": folders}
            if len(folders) >= 2:
                self._overlay.show_cloud_saves_conflict(name, exe_path)
            else:
                self._overlay.show_cloud_saves_unknown(name, exe_path)


    def _open_cloud_verify_dialog(self, exe_path: str):
        """Open the cloud-verify dialog for a queued unknown game (candidates
        were stashed by exe_path when the notification was shown)."""
        stash = self._pending_cloud_verify.get(exe_path)
        if not stash:
            return
        detected = stash.get("name") or exe_path
        candidates = stash.get("folders") or []
        if not candidates:
            return
        from ui.dialogs.cloud_verify_dialog import CloudVerifyDialog
        dlg = CloudVerifyDialog(detected, candidates, self)
        dlg.resolution.connect(
            lambda choice, folder: self._on_cloud_verify_result(exe_path, detected, choice, folder)
        )
        dlg.exec()


    def _on_cloud_verify_result(self, exe_path: str, detected_name: str, choice: str, folder: str):
        """Act on the cloud-verify choice."""
        self._pending_cloud_verify.pop(exe_path, None)
        if choice == "cancel":
            return
        if choice == "download":
            # This cloud copy IS this game's: adopt its folder and download.
            self._add_and_download_unknown(exe_path, force_folder_name=folder)
        elif choice == "homonym":
            # Same-name DIFFERENT game: own cloud folder, no download.
            self._add_homonym_unknown(exe_path, detected_name)


    def _on_conflict_detected(self, game_id: str, conflict_info: dict):
        """Show ConflictDialog when auto-sync detects both sides changed."""
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        # "Keep local" was already chosen this session: honour it silently
        # (up-only) instead of re-asking on every auto sync.
        if game_id in self._cross_machine_local_only:
            get_orchestrator().sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path, direction="up",
                computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history),
            )
            return
        from ui.dialogs.conflict_dialog import ConflictDialog
        from datetime import datetime
        local_time = None
        remote_time = None
        try:
            local_str = conflict_info.get("local", "")
            if local_str:
                local_time = datetime.fromisoformat(local_str)
        except (ValueError, TypeError):
            pass
        try:
            remote_str = conflict_info.get("remote", "")
            if remote_str:
                remote_time = datetime.fromisoformat(remote_str)
        except (ValueError, TypeError):
            pass
        dlg = ConflictDialog(entry.name, local_time, remote_time, self)
        dlg.resolution.connect(lambda choice: self._handle_conflict_choice(entry, choice))
        # Same in-game backdrop the save-confirmation panel gets: a conflict
        # can appear while a game is running, and the vignette is what makes
        # it read as a decision to make rather than a stray window.
        try:
            self._show_blur_for_dialog(dlg)
        except Exception:
            logger.debug("Blur backdrop unavailable for conflict dialog", exc_info=True)
        try:
            dlg.exec()
        finally:
            try:
                self._on_blur_dialog_gone(dlg)
            except Exception:
                pass


    def _handle_conflict_choice(self, entry, choice: str):
        """Handle user's conflict resolution choice."""
        if choice == "cancel":
            return
        orch = get_orchestrator()
        if choice in ("cloud", "both"):
            # Downloading the other machine's backups was accepted: persist
            # the confirmation so the divergence gate never re-prompts this
            # machine for this game (mirrors the cloud-prompt flow).
            self._mark_cloud_machine_confirmed(entry.id)
        elif choice == "local":
            # Up-only for the rest of the session, without re-asking.
            self._cross_machine_local_only.add(entry.id)
        if choice == "both":
            # Keep both: backup local first, download cloud version,
            # then upload local saves once the download finishes.
            # The upload must be chained via sync_finished because
            # sync_game guards against concurrent syncs per game_id.
            self._backup_game(entry.id)
            self._pending_both_upload = entry  # chain upload after download
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path, direction="down", computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history)
            )
        else:
            direction_map = {"local": "up", "cloud": "down"}
            direction = direction_map.get(choice, "auto")
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path, direction=direction, computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history)
            )

