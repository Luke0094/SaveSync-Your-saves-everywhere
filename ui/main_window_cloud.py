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


    def _entry_has_local_backups(self, entry) -> bool:
        """True if this game has local backup archives stored (in BackupManager)."""
        from core.backup import get_backup_manager
        bm = get_backup_manager()
        if bm.get_backups_for_game(entry.id):
            return True
        try:
            from core.constants import get_install_folder_name
            folder = get_install_folder_name(
                entry.exe_path or "", entry.name, entry.id,
                entry.computed_folder_name,
            )
            if not folder:
                return False
            lib_ids = bm.library_game_ids()
            for b in bm.get_backups_for_folder(folder) or []:
                if bm.is_orphan_entry(b):
                    continue
                if b.game_id and b.game_id not in lib_ids:
                    continue
                if b.game_id and b.game_id != entry.id and b.game_id in lib_ids:
                    # Another live library game owns this zip — not ours.
                    continue
                return True
        except Exception:
            pass
        return False

    def _entry_has_live_saves_on_disk(self, entry) -> bool:
        """True if at least one configured save path exists and contains non-empty files."""
        if not entry or not entry.save_paths:
            return False
        from pathlib import Path
        for p_str in entry.save_paths:
            if not p_str:
                continue
            try:
                p = Path(p_str)
                if p.is_file() and p.exists() and p.stat().st_size > 0:
                    return True
                if p.is_dir() and p.exists():
                    for f in p.rglob("*"):
                        if f.is_file() and f.stat().st_size > 0:
                            return True
            except Exception:
                pass
        return False

    def _check_cloud_on_launch(self, game_id: str, on_resolved: Optional[Callable] = None):
        """Check for cloud saves when a game launches and, if appropriate,
        show an in-game yes/no prompt to download/restore them — the same
        action pattern as "unknown game with cloud saves → download & add
        to library", minus the add-to-library step since the game is
        already known.

        Also runs when offline: hand-added orphan archives (same index shape
        as cloud) are offered through this same notification path. Offline
        or online, this is also THE place a local divergence gets caught —
        see core.backup.BackupManager.detect_regression, called here
        unconditionally rather than from a separate check: whether the
        current save matches this game's own backup history is a purely
        local question, answered the same way regardless of what the cloud
        side finds, and folding it in here means one decision tree produces
        one notification instead of two systems that could each fire for
        what is really the same underlying fact.

        *on_resolved*, if given, is called once the (backgrounded) check
        has completed — see _on_cloud_check_result(). It fires as soon as
        the check itself resolves, not once the user has answered any
        prompt that check may result in: a "no local backup yet, want to
        download?" question is exactly the situation where the watcher and
        in-game backup timer (what on_resolved starts, in
        _start_tracking_after_cloud_check) matter most, so they must not
        wait on how long the player takes to notice/answer an overlay.

        Everything here — the network round-trip AND the local regression
        check (real file reads/hashing, not free) — runs in a background
        thread: it used to run directly on the GUI thread, meaning a slow
        or unresponsive provider could stall the whole app, including the
        overlay itself, for as long as the request took, while the player
        was already in-game.
        """
        entry = get_library().get_by_id(game_id)
        if entry is None:
            if on_resolved:
                on_resolved()
            return

        if on_resolved:
            with self._cloud_check_lock:
                self._cloud_check_on_resolved[game_id] = on_resolved

        orch = get_orchestrator()
        online = orch.is_online()

        import threading
        _game_id = game_id
        _exe_path = entry.exe_path
        _name = entry.name
        _cfn = entry.computed_folder_name
        _save_paths = list(entry.save_paths or [])
        from core.monitor import get_monitor
        _since = get_monitor().tracked_process_start_time(game_id)
        # The backup id SaveSync itself last restored onto this game's save
        # folder (see _restore_after_regression / _restore_game_by_id) — if
        # that write is still what's on disk, detect_regression must read it
        # as the intended outcome, not a fresh regression. Without this, a
        # restore's own pre-restore safety copy (of whatever was on disk
        # right before the restore) becomes the newest backup by timestamp,
        # and the very next check would find current content matching an
        # OLDER one instead — the just-completed restore reported as a brand
        # new regression against itself.
        #
        # Read from the persisted flag on the backup entry itself (see
        # BackupManager.mark_last_restored/get_last_restored_backup_id),
        # not the in-memory dict alone: a restore followed by quitting and
        # reopening SaveSync before the next launch would otherwise lose
        # the answer along with the process, and the same false regression
        # would fire again with nothing left to explain it.
        from core.backup import get_backup_manager as _gbm
        _expected_backup_id = (_gbm().get_last_restored_backup_id(game_id)
                               or self._last_restored.get(game_id, ""))

        def _do_check():
            has_cloud = False
            if online:
                try:
                    has_cloud = orch.check_cloud_saves(
                        _game_id, exe_path=_exe_path, game_name=_name,
                        computed_folder_name=_cfn,
                    )
                except Exception as e:
                    logger.debug(f"_check_cloud_on_launch: check_cloud_saves failed: {e}")
                    has_cloud = False
            regressed_to, is_unbacked = None, False
            if _save_paths:
                try:
                    from core.backup import get_backup_manager
                    regressed_to, is_unbacked = get_backup_manager().detect_regression(
                        _game_id, _save_paths, changes_explained_since=_since,
                        expected_backup_id=_expected_backup_id)
                except Exception as e:
                    logger.debug(f"_check_cloud_on_launch: detect_regression failed: {e}")
            with self._cloud_check_lock:
                self._cloud_check_results[_game_id] = {
                    "has_cloud": has_cloud,
                    "regressed_to": regressed_to,
                    "is_unbacked": is_unbacked,
                }
            from PySide6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
            try:
                QMetaObject.invokeMethod(
                    self, "_on_cloud_check_result", _Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, _game_id),
                )
            except RuntimeError:
                pass

        threading.Thread(target=_do_check, daemon=True).start()


    def _stash_orphan_match(self, entry) -> bool:
        """If an orphan archive matches *entry*, remember it for accept.

        Returns True when a matching unapplied archive exists and this game
        still has no backups under its own id. Orphan zips in the title
        folder must NOT block this — they are the thing being offered.
        """
        if entry is None:
            return False
        try:
            from core.backup import get_backup_manager
            bm = get_backup_manager()
            if bm.get_backups_for_game(entry.id):
                self._pending_orphan_adopt.pop(entry.id, None)
                return False
            orphans = bm.find_orphan_backups_for_game(entry)
            if not orphans:
                self._pending_orphan_adopt.pop(entry.id, None)
                return False
            self._pending_orphan_adopt[entry.id] = orphans[0].game_id
            return True
        except Exception:
            logger.debug("Orphan match failed", exc_info=True)
            return False

    def _apply_orphan_backup_to_game(self, entry) -> bool:
        """Adopt matching orphan index + restore to the chain destination.

        The archive folder (collection copy under e.g. D:\\VN Games\\Save) is
        only the zip *source*. Live game save_paths must come from the recorded
        chain (AppData/Roaming/…, www/save, …) — never from that origin path.
        """
        if entry is None:
            return False
        try:
            from core.backup import get_backup_manager, chain_destination
            bm = get_backup_manager()
            orphan_gid = getattr(self, "_pending_orphan_adopt", {}).pop(entry.id, None)
            orphans = bm.get_backups_for_game(orphan_gid) if orphan_gid else []
            if not orphans:
                orphans = bm.find_orphan_backups_for_game(entry)
            if not orphans:
                return False

            by_oid: dict[str, list] = {}
            for b in orphans:
                by_oid.setdefault(b.game_id, []).append(b)

            paths: list[str] = []
            for oid, backs in by_oid.items():
                # The newest row that describes the ARCHIVE. backs[0] is
                # simply the newest, and after a restore that is the safety
                # copy of the destination — which carries no chains, so the
                # destination would be rebuilt from whatever it happened to
                # hold rather than from what the archive recorded.
                newest = bm.newest_archive_row(backs) or backs[0]
                for p in (newest.save_paths or []):
                    chain = (
                        newest.content_chain_for(p)
                        or newest.chain_for(p)
                        or ""
                    ).strip()
                    dest = ""
                    if chain:
                        resolved = chain_destination(chain, entry.id)
                        if resolved is not None:
                            dest = str(resolved)
                    if not dest:
                        # Keep the chain for later rebases / restore; do NOT
                        # register the archive copy as the live save folder.
                        if chain and not (entry.save_chain or "").strip():
                            entry.save_chain = chain
                        logger.info(
                            f"Orphan archive for {entry.name!r}: chain "
                            f"{chain!r} — no live destination yet "
                            f"(skipped origin {p!r})"
                        )
                        continue
                    if dest not in paths:
                        paths.append(dest)
                        if chain:
                            entry.record_path_chain(dest, chain)
                bm.adopt_backups(
                    oid, entry.id, entry.name,
                    entry.exe_path or "",
                    entry.computed_folder_name or "",
                )

            if paths or (entry.save_chain or "").strip():
                if paths:
                    merged = list(entry.save_paths or [])
                    for p in paths:
                        if p not in merged:
                            merged.append(p)
                    entry.save_paths = merged
                    entry.save_paths_confirmed = True
                get_library().update_game(entry)

            owned = bm.get_backups_for_game(entry.id)
            if owned:
                bm.restore_backup(owned[0].backup_id, lib_game_id=entry.id)
            logger.info(
                f"Applied orphan archive to {entry.name!r} "
                f"({len(paths)} destination path(s))"
            )
            return True
        except Exception:
            logger.exception("Failed to apply orphan archive")
            return False

    @Slot(str)
    def _on_cloud_check_result(self, game_id: str):
        """GUI-thread continuation of _check_cloud_on_launch, run once the
        (possibly slow) network check AND the local regression check have
        both completed in the same background pass. Everything below is
        local/cheap — no I/O — so it's safe to run directly here.
        """
        with self._cloud_check_lock:
            _result = self._cloud_check_results.pop(game_id, None) or {}
            has_cloud = bool(_result.get("has_cloud"))
            regressed_to = _result.get("regressed_to")
            is_unbacked = bool(_result.get("is_unbacked"))
            on_resolved = self._cloud_check_on_resolved.pop(game_id, None)

        # One-shot suppression: the user already answered the cloud question
        # through another flow (e.g. "download & add" / "add without
        # downloading" on the unknown-game notification) — don't re-prompt.
        if game_id in self._suppress_cloud_prompt_once:
            self._suppress_cloud_prompt_once.discard(game_id)
            has_cloud = False

        entry = get_library().get_by_id(game_id)

        # A name-similarity match still hasn't settled which library entry
        # this running game even IS (see _identity_still_ambiguous) — a
        # cloud prompt asking to download/restore THIS entry's saves would
        # be asking about the wrong game as often as not. Held back
        # entirely (not shown, not even orphan-matched) until the mystery
        # is answered, at which point _recheck_cloud_after_identity_resolved
        # runs this same check for real, for whichever entry it resolved to.
        if entry is not None and self._identity_still_ambiguous(entry.id):
            if on_resolved:
                on_resolved(show_toast=True)
            return

        # Hand-added orphan archives: same notification as cloud no_local.
        has_orphan = self._stash_orphan_match(entry) if entry is not None else False

        # Decide FIRST whether a cloud notification will actually be shown,
        # before calling on_resolved — that decision is exactly what
        # on_resolved needs (as show_toast) to avoid firing the plain
        # "tracking" toast an instant before a cloud prompt overwrites it
        # on the same overlay widget. Getting this backwards (call
        # on_resolved unconditionally, decide about the notification after)
        # is what caused a visible flicker: two show_animated() calls in the
        # same synchronous pass, each cancelling and restarting the other's
        # fade-in within milliseconds.
        # "regression" | "unbacked" | "different_machine" | "no_local"
        # | "conflict_diverged" | "conflict_unreconciled" | "sync_prompt" | None
        notification_kind = None
        if entry is not None and self._overlay is not None:
            from core.machine import get_machine_id
            machine_id = get_machine_id()
            cloud_meta = entry.cloud_metadata or {}
            last_machine = cloud_meta.get("last_sync_machine", "")
            confirmed_machines: list = cloud_meta.get("download_confirmed_machines", [])
            has_local = self._entry_has_local_backups(entry)
            has_live_saves = self._entry_has_live_saves_on_disk(entry)
            _muted = entry.id in get_config().get("suppressed_cloud_no_local", [])
            show = get_config().get("show_overlay_on_cloud", True)
            _notifs_muted = get_config().get("suppressed_ingame_notifs", {}).get(entry.id, [])

            # 0) Regression (an OLDER state came back) is checked first and
            # unconditionally — but this is a rare safety net closing a
            # specific gap, not something that meaningfully competes with
            # the cloud-side findings below in real usage, so giving it top
            # priority costs nothing in practice.
            if regressed_to is not None and "regression" not in _notifs_muted:
                notification_kind = "regression"

            # 1) When user has no live saves on disk (deleted save files or empty save folder),
            #    propose restoring/downloading available backups (local, orphan, or cloud)
            #    instead of firing premature live tracking notification.
            # Unchanged from before local/unbacked existed: different_machine
            # and everything below it in this tree are the common, expected
            # findings and keep their original priority untouched. "unbacked"
            # is deliberately NOT checked here — see the fallback after this
            # whole tree, below.
            if notification_kind is not None:
                pass
            elif not has_live_saves and (has_cloud or has_local or has_orphan) and show and not _muted:
                notification_kind = "no_local"
            elif has_orphan and show and not _muted:
                notification_kind = "no_local"
            elif has_cloud:
                if (last_machine and last_machine != machine_id
                        and machine_id not in confirmed_machines):
                    if show:
                        notification_kind = "different_machine"
                elif show:
                    # One per-game "don't ask me to download at launch" list backs
                    # both cloud-download prompts (the no-local one and the
                    # not-reconciled one): they ask the same question, so muting
                    # one and still being asked the other made no sense.
                    if not has_local or not has_live_saves:
                        if not _muted:
                            notification_kind = "no_local"
                    elif _muted:
                        pass
                    elif entry.sync_status == "conflict":
                        # A conflict recorded in an earlier session and never
                        # resolved: nothing used to bring it back, because the
                        # comparison window only ever opened from a LIVE
                        # detection during auto-sync.
                        notification_kind = "conflict_diverged"
                    elif entry.sync_status == "local_only":
                        # Never synced, yet a cloud copy exists — local backups
                        # made by hand plus a cloud folder from elsewhere (or a
                        # same-named different game). Neither side can be assumed
                        # to win, so it is the reconcile decision, NOT a download
                        # prompt: that is what made this ask every single launch
                        # before, since nothing about the status ever changed.
                        # Any resolution syncs, which moves the status off
                        # local_only and stops the asking.
                        notification_kind = "conflict_unreconciled"
                    elif entry.sync_status in ("cloud_only", "pending"):
                        # Keep-local already chosen: pending_local_wins forces the
                        # next auto-sync to upload — do not re-ask "download?".
                        if getattr(entry, "pending_local_wins", False):
                            pass
                        elif not has_local or not has_live_saves:
                            # Nothing real to protect locally either way — a
                            # genuinely cloud_only game, or a "pending" one
                            # whose local copy has since been wiped/reinstalled.
                            # Offering to grab the cloud copy is right for both.
                            notification_kind = "sync_prompt"
                        elif entry.sync_status == "pending":
                            # "pending" means LOCAL is the side that moved past
                            # the last synced state — mark_played() only ever
                            # sets it starting FROM "synced" (see its own
                            # docstring), never as a hint that the cloud might
                            # independently hold something newer instead; that
                            # case has its own explicit status ("conflict").
                            # With real local content actually present, a
                            # DOWNLOAD prompt has nothing to offer — confirmed
                            # via a real incident: sync_prompt's primary action
                            # is a forced direction="down" sync, which would
                            # have pulled the OLDER cloud copy over local
                            # content the cloud had simply never seen yet. The
                            # divergence resolves itself the moment the
                            # ordinary exit-backup+autosync uploads it, same as
                            # any other "pending" game — no prompt needed here.
                            pass
                        elif self._local_content_matches_known_backup(entry):
                            # "Newer than last sync" is only the mtime saying so —
                            # the content hash still matches the last backup, so
                            # there is nothing new to download. The date-only
                            # (sync_status pending) check used to ask every
                            # launch for mtime touches; the local content check
                            # goes beyond the date.
                            pass
                        else:
                            notification_kind = "sync_prompt"

            # Fallback, checked LAST: local content matches nothing in this
            # game's own backup history, but nothing above already found a
            # more specific/actionable thing to say about it (different
            # machine, an unresolved conflict, cloud has something to sync).
            # This is what actually closes the original gap — a save that
            # regressed or changed entirely outside SaveSync while
            # sync_status stayed stuck at "synced" (nothing else updates it)
            # falls through every branch above with nothing to say, which is
            # exactly when this matters.
            if notification_kind is None and is_unbacked and "unbacked" not in _notifs_muted:
                notification_kind = "unbacked"

            logger.info(
                f"Cloud launch check for {entry.name!r}: "
                f"has_cloud={has_cloud}, has_orphan={has_orphan}, "
                f"has_local={has_local}, "
                f"sync_status={entry.sync_status!r}, "
                f"last_sync_machine={last_machine!r}, "
                f"prompt={notification_kind!r}"
            )

        if on_resolved:
            on_resolved(show_toast=(notification_kind is None))

        if notification_kind == "regression":
            self._pending_cloud_notification[game_id] = "regression"
            from core.backup import get_backup_manager
            # Prefer the newest TRUSTED backup — a pre_confirmation entry
            # (this game's own unresolved-warning capture, or an earlier
            # restore's rejected safety copy) makes a misleading "restore
            # back to" default, silently passed off as known-good when
            # it isn't — but always offer SOMETHING when any backup
            # exists at all: the choice is restore-or-keep, and that
            # needs a real target on both sides. See
            # newest_restore_target's own docstring.
            _newest_id = get_backup_manager().newest_restore_target(game_id)
            self._overlay.show_save_reverted(entry.name, game_id, _newest_id, False)
        elif notification_kind == "unbacked":
            self._pending_cloud_notification[game_id] = "unbacked"
            from core.backup import get_backup_manager
            _newest_id = get_backup_manager().newest_restore_target(game_id)
            self._overlay.show_save_reverted(
                entry.name, game_id, _newest_id, unbacked=True)
        elif notification_kind == "different_machine":
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
            self._pending_cloud_notification[game_id] = "sync_prompt"
            self._overlay.show_cloud_saves(entry.name, entry.exe_path)
        elif notification_kind in ("conflict_diverged", "conflict_unreconciled"):
            self._pending_cloud_notification[game_id] = notification_kind
            # Cheap local date only here (GUI thread). Remote is filled when
            # the user opens the comparison dialog — see _ensure_conflict_times.
            self._stash_local_conflict_time(entry)
            self._overlay.show_cloud_conflict_resolve(
                entry.name, entry.exe_path,
                diverged=(notification_kind == "conflict_diverged"))
        elif game_id in self._pending_cloud_notification or game_id in self._pending_regression:
            # Nothing to show THIS launch, but a PREVIOUS one left a
            # regression/unbacked warning the player never answered (the
            # overlay is non-blocking — closing the game without clicking
            # anything is the common case). Only the overlay's own action
            # handlers ever pop these, so an unanswered one would otherwise
            # sit there forever even once whatever raised it is long gone —
            # and _launch_notification_pending (which both dicts feed)
            # would then believe a warning is still pending permanently,
            # silently disabling this game's in-game timer, reactive
            # backup, and exit backup for the rest of the process. regressed_to
            # here is this SAME launch's fresh, unified detect_regression
            # result (see _check_cloud_on_launch) — the one source of truth
            # both dicts ultimately describe — so it having come back clean
            # is exactly the confirmation needed to call the old one stale.
            self._pending_cloud_notification.pop(game_id, None)
            self._pending_regression.pop(game_id, None)
            logger.debug(
                f"Cleared stale pending regression/unbacked warning for "
                f"{entry.name if entry else game_id}: this launch's check came back clean")


    def _stash_local_conflict_time(self, entry) -> None:
        """Record the newest local backup date for a launch-time reconcile."""
        info = dict(self._pending_conflict_info.get(entry.id) or {})
        if info.get("local"):
            return
        try:
            from core.backup import get_backup_manager
            from core.constants import get_install_folder_name
            bm = get_backup_manager()
            backs = list(bm.get_backups_for_game(entry.id) or [])
            if not backs:
                folder = get_install_folder_name(
                    entry.exe_path or "", entry.name, entry.id,
                    entry.computed_folder_name,
                )
                if folder:
                    lib_ids = bm.library_game_ids()
                    backs = [
                        b for b in (bm.get_backups_for_folder(folder) or [])
                        if not bm.is_orphan_entry(b)
                        and (not b.game_id or b.game_id in lib_ids)
                    ]
            if backs:
                info["local"] = max(
                    (b.created_at or "" for b in backs), default="")
                self._pending_conflict_info[entry.id] = info
        except Exception:
            logger.debug("Could not resolve local conflict time", exc_info=True)


    def _local_content_matches_known_backup(self, entry) -> bool:
        """True when the CURRENT save contents match SOME backup already
        kept for this game — checked against the WHOLE history, not only
        the newest one.

        Date-only logic (``sync_status == "pending"``) treats any touch as new
        work and re-asks the cloud download at every launch; comparing the
        content hash instead of the dates stops asking when the bytes are
        already something SaveSync has already kept. Falls back to False
        (keep asking) when the comparison cannot be made.

        Only the newest backup used to be checked — which meant restoring
        an OLDER one on purpose (Backups page → Restore, picking anything
        but the top row) left local content matching that older backup
        while still not matching the newest, so the very next launch fired
        the cloud prompt right back, as if the deliberate restore had never
        happened. Matching anywhere in the kept history is what tells that
        case apart from a genuine external change: a restore reproduces
        some backup's exact hash, an untracked edit made outside SaveSync
        does not match anything on file at all.

        A file the CURRENT session itself just wrote — an engine rewriting
        its own settings on open is the confirmed case, RPG Maker's is a
        real example — does not count as a mismatch either; see
        current_state_hash's changes_explained_since for why that is not
        "the mtime changed" in the sense the docstring above means to exclude,
        it is a genuine content difference that still isn't what this prompt
        exists to catch (an external, unattended change). This is always
        called with the game already launched (see _on_cloud_check_result),
        so the tracked process's own create_time is the exact cutoff; the
        grace-window fallback is only for the rare case it isn't tracked.
        """
        try:
            from core.backup import get_backup_manager
            from core.monitor import get_monitor
            bm = get_backup_manager()
            backups = bm.get_backups_for_game(entry.id)
            if not backups:
                return False
            since = get_monitor().tracked_process_start_time(entry.id)
            if not since:
                import time as _time
                since = _time.time() - bm._PID_TRACKING_LAG_S
            current = bm.current_state_hash(
                entry.id, entry.save_paths or [],
                changes_explained_since=since)
            if not current:
                return False
            return any(
                current == (b.cloud_metadata or {}).get("save_hash", "")
                for b in backups
            )
        except Exception:
            logger.debug("Local content check failed", exc_info=True)
            return False

    def _ensure_conflict_times(self, entry) -> None:
        """Make sure ``_pending_conflict_info`` has local/remote dates to show.

        Live auto-sync conflicts already stash ISO timestamps. Launch-time
        ``conflict_unreconciled`` does not — without this the dialog shows
        two "unknown" cards even when both sides have dated backups.
        Remote listing may hit the network; call only when opening the dialog.
        """
        self._stash_local_conflict_time(entry)
        info = dict(self._pending_conflict_info.get(entry.id) or {})
        if not info.get("remote"):
            try:
                from core.constants import get_install_folder_name, get_folder_name_for_save
                folder = get_install_folder_name(
                    entry.exe_path or "", entry.name, entry.id,
                    entry.computed_folder_name,
                )
                orch = get_orchestrator()
                candidates = [folder] if folder else []
                for hn in (entry.name_history or []):
                    fn = get_folder_name_for_save(hn, entry.exe_path or "", entry.id)
                    if fn and fn not in candidates:
                        candidates.append(fn)
                newest = ""
                for p in orch.get_connected_providers():
                    resolved = orch.resolve_remote_game_folder(p, candidates)
                    if not resolved:
                        continue
                    remote = p.list_cloud_backups(resolved) or []
                    if not remote:
                        continue
                    candidate = max(
                        (e.get("created_at") or "" for e in remote), default="")
                    if candidate > newest:
                        newest = candidate
                if newest:
                    info["remote"] = newest
            except Exception:
                logger.debug("Could not resolve remote conflict time",
                             exc_info=True)
        if info:
            self._pending_conflict_info[entry.id] = info


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
                lib_ids = bm.library_game_ids()
                candidates = bm.get_backups_for_folder(folder) or []
                # Same folder name does not mean same game: reject anything
                # clearly owned by another live library game (see
                # _entry_has_local_backups above) so a name collision can't
                # restore an unrelated game's save over this one's.
                backups = [
                    b for b in candidates
                    if not bm.is_orphan_entry(b)
                    and not (b.game_id and b.game_id != entry.id and b.game_id in lib_ids)
                ]
            except Exception as _e:
                logger.debug(f"_restore_after_cloud_download: folder fallback failed: {_e}")
        if not backups:
            logger.warning(f"_restore_after_cloud_download: no backups found for {entry.name}")
            from ui.modal_helpers import warning_window_modal
            warning_window_modal(
                self._main_window if hasattr(self, '_main_window') else None,
                t("restore.title"),
                t("restore.restore_failed", game=entry.name),
            )
            return

        latest = max(backups, key=lambda b: b.created_dt)

        # Backup-before-download: restoring writes the downloaded archive
        # OVER the live save folder, so any local-only progress must be
        # archived first (the same rule the "keep both" conflict path uses).
        # create_backup dedups by content hash — unchanged saves cost nothing.
        try:
            if entry.save_paths:
                from core.config_manager import get_config
                _cfg = get_config()
                bm.create_backup(
                    game_id, entry.name, list(entry.save_paths),
                    exe_path=entry.exe_path or "",
                    note=t("main.backup_before_download_note"),
                    max_size_mb=_cfg.get("max_backup_size_mb", 512),
                    force=False,
                    computed_folder_name=entry.computed_folder_name or "",
                    excluded_paths=list(entry.excluded_save_paths or []),
                    name_history=list(entry.name_history or []),
                )
        except Exception as e:
            logger.warning(
                f"Backup-before-download failed for {entry.name}: {e}")

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
            from ui.modal_helpers import warning_window_modal
            warning_window_modal(
                self._main_window if hasattr(self, '_main_window') else None,
                t("restore.title"),
                t("restore.restore_failed", game=entry.name),
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
        """Auto-sync found both sides changed — surface it as a notification.

        The overlay prompt is the entry point (same treatment as every other
        cloud decision), and the ConflictDialog behind it is reached from that
        prompt's primary action. A modal appearing on its own could land
        behind a fullscreen game, and it asked for a decision without the
        player having been told a conflict existed. Falls back to opening the
        dialog directly when there is no overlay to notify through.
        """
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
        # The two dates live here until the user asks to compare them.
        self._pending_conflict_info[game_id] = dict(conflict_info or {})
        if self._overlay is not None and entry.exe_path:
            self._pending_cloud_notification[game_id] = "conflict_diverged"
            self._overlay.show_cloud_conflict_resolve(
                entry.name, entry.exe_path, diverged=True)
            return
        self._open_conflict_dialog(entry)

    def _open_conflict_dialog(self, entry):
        """The local-vs-cloud comparison window, with both versions dated.

        Reached from the conflict notification's primary action; also the
        direct path when no overlay is available. Timestamps come from the
        live sync detection when present, otherwise from backup indexes
        (see ``_ensure_conflict_times``).
        """
        self._ensure_conflict_times(entry)
        conflict_info = self._pending_conflict_info.get(entry.id) or {}
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
            # Closing the comparison without deciding means "later": the
            # notification stays pending so the hotkey can bring it back.
            return
        # Decided — the prompt has served its purpose and the stashed dates
        # are spent.
        self._pending_cloud_notification.pop(entry.id, None)
        self._pending_conflict_info.pop(entry.id, None)
        orch = get_orchestrator()
        if choice in ("cloud", "both"):
            # Downloading the other machine's backups was accepted: persist
            # the confirmation so the divergence gate never re-prompts this
            # machine for this game (mirrors the cloud-prompt flow).
            self._mark_cloud_machine_confirmed(entry.id)
        elif choice == "local":
            # Up-only for the rest of the session AND for later auto-syncs
            # (exit backup, sync page, …). Without pending_local_wins, a
            # plain "auto" sync after "keep local" still pulled remote-only
            # backups (e.g. from a previous library entry of the same game).
            # Stay on "pending" (not "synced"): the post-exit path that
            # reconciles when the backup is unchanged only runs for pending,
            # and that is the usual case — close the game with nothing new.
            # Launch skips the download prompt while pending_local_wins is set.
            self._cross_machine_local_only.add(entry.id)
            try:
                from core.machine import get_machine_id
                mid = get_machine_id()
                cloud_meta = dict(entry.cloud_metadata or {})
                confirmed = list(cloud_meta.get("download_confirmed_machines", []))
                if mid not in confirmed:
                    confirmed.append(mid)
                cloud_meta["download_confirmed_machines"] = confirmed
                get_library().update_game_fields(
                    entry.id,
                    pending_local_wins=True,
                    sync_status="pending",
                    cloud_metadata=cloud_meta,
                )
            except Exception:
                logger.debug("Could not persist keep-local wins", exc_info=True)
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
        elif choice == "cloud":
            # "Keep Cloud" must actually land on disk — a plain sync_game
            # only pulls the archive into local backup history and marks
            # the game synced; without restoring afterward the live save
            # folder would silently keep holding the rejected local save.
            def _on_cloud_sync_done(game_id: str, result):
                if game_id != entry.id:
                    return
                try:
                    orch.sync_finished.disconnect(_on_cloud_sync_done)
                except RuntimeError:
                    pass
                if result.success:
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(300, lambda: self._restore_after_cloud_download(entry.id))

            orch.sync_finished.connect(_on_cloud_sync_done)
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path, direction="down", computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history)
            )
        else:
            direction_map = {"local": "up"}
            direction = direction_map.get(choice, "auto")
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path, direction=direction, computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history)
            )

