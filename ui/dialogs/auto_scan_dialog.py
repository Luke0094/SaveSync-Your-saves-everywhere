"""
SaveSync - Automatic Save Scan Dialog
Shows discovered save paths and asks for confirmation before exit.
"""
import logging
import platform
from pathlib import Path
from typing import List, Dict, Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QCheckBox, QProgressBar,
)

from core.save_detector import detect_save_paths
from core.library import get_library, GameEntry
from core.monitor import get_monitor
from core.config_manager import get_config
from i18n import t
from ui.styles.theme import palette
from ui.helpers import ElidedCheckBox, TopmostPinMixin, apply_game_friendly_flags, finalize_adaptive_dialog_size, lock_min_size, scaled

logger = logging.getLogger(__name__)


def filter_selectable_paths(game_id: str, paths: List[str]) -> List[str]:
    """Return only the paths that would actually be shown as selectable
    entries in the confirmation dialog for *game_id*.

    Drops:
    - paths the user permanently deleted in a previous session,
    - paths whose entire content is excluded by extension/dir bans
      (nothing selectable would appear in the file browser — see the
      shared save_detector.path_has_backup_content helper).

    Shared by add_found_paths() and by callers that must decide whether the
    dialog/notification is worth showing at all — a "1 path found" popup
    with zero selectable files must never appear.

    Case/separator-equivalent spellings of one folder are collapsed first:
    the watcher and the open-file scan legitimately report the same folder
    with different casing ("…\\Save" vs "…\\save"), which would otherwise
    show up as two identical-looking rows to confirm.
    """
    from core.save_detector import (
        path_has_backup_content, dedupe_paths, path_identity,
    )
    from core.engines.game_engine import engine_for_game
    from core.library import get_library
    config = get_config()
    paths = dedupe_paths(paths)
    # Same identity rule as the dedupe above: a path the user deleted stays
    # deleted even when it is re-detected with different casing.
    deleted_paths = {
        path_identity(p)
        for p in config.get("auto_scan_deleted_paths", {}).get(game_id, [])
    }
    # ".dat" is engine data in one engine and a save in another, so the
    # exclusion has to know which game this is.
    engine = engine_for_game(get_library().get_by_id(game_id)) if game_id else ""

    result: List[str] = []
    for path in paths:
        if path_identity(path) in deleted_paths:
            logger.debug(f"Skipping previously deleted path: {path}")
            continue
        if not path_has_backup_content(path, engine=engine):
            logger.debug(f"Skipping path with no selectable files: {path}")
            continue
        result.append(path)
    return result


def filter_uncovered_paths(paths: List[str], confirmed_paths: List[str]) -> List[str]:
    """Drop paths already covered by *confirmed_paths*.

    A path is covered when it equals a confirmed path, lives inside one
    (a save file under an already-configured save folder) or is a parent
    of one (a redundant broader folder). Shared by the automatic at-exit
    confirmation flow and the manually opened in-game panel so both apply
    the same rule — previously only the automatic flow filtered these, so
    the manual panel could propose paths that were already configured.
    """
    from core.registry_saves import is_registry_path
    resolved_confirmed: List[Path] = []
    for cp in confirmed_paths or []:
        if is_registry_path(cp):
            continue
        try:
            resolved_confirmed.append(Path(cp).resolve())
        except Exception:
            resolved_confirmed.append(Path(cp))
    confirmed_reg = [c.lower() for c in (confirmed_paths or [])
                     if is_registry_path(c)]
    if not resolved_confirmed and not confirmed_reg:
        return list(paths)

    def _covered(p: str) -> bool:
        # Registry entries: covered by an identical confirmed key or by a
        # confirmed ancestor/descendant key (string prefix on \\-separated
        # keys) — never by filesystem paths.
        if is_registry_path(p):
            pl = p.lower()
            return any(pl == cl or pl.startswith(cl + "\\")
                       or cl.startswith(pl + "\\") for cl in confirmed_reg)
        try:
            pp = Path(p).resolve()
        except Exception:
            pp = Path(p)
        for cpp in resolved_confirmed:
            if pp == cpp:
                return True
            try:
                pp.relative_to(cpp)
                return True
            except ValueError:
                pass
            try:
                cpp.relative_to(pp)
                return True
            except ValueError:
                pass
        return False

    return [p for p in paths if not _covered(p)]


class ScanWorkerThread(QThread):
    """Background thread for scanning save paths"""
    progress = Signal(int, int)  # current, total
    found_paths = Signal(str, str, list)  # game_id, game_name, paths
    scan_done = Signal()
    error = Signal(str)

    def __init__(self, games: List[GameEntry], general_scan: bool = False,
                 tracked_snapshot: Optional[Dict] = None, force: bool = False):
        super().__init__()
        self.games = games
        self.general_scan = general_scan
        self._tracked_snapshot = tracked_snapshot or {}
        self._should_stop = False
        # When True, scan every game in *games* regardless of its current
        # save_paths/requires_confirmation state. Needed for single-game
        # re-scans (see AutoScanDialog._use_pre_scanned_paths): that call
        # deliberately targets a game that may already have confirmed
        # paths — we're checking whether anything NEW showed up, which is
        # exactly the case the plain "already has paths, skip it" rule
        # (meant for broad library-wide sweeps) would otherwise discard.
        self.force = force
        self.setPriority(QThread.Priority.IdlePriority)

    def stop(self):
        self._should_stop = True

    def run(self):
        try:
            total_games = len(self.games)
            
            for i, game in enumerate(self.games):
                if self._should_stop:
                    break

                self.progress.emit(i + 1, total_games)

                # Skip games that already have confirmed AND complete save paths
                # (still scan if save_paths is empty, requires confirmation,
                # or this is a forced single-game re-scan)
                if game.save_paths and not game.requires_confirmation and not self.force:
                    continue

                # Check stop flag before expensive detection
                if self._should_stop:
                    break

                # Try to get live PID first (most reliable method)
                pid = None
                try:
                    for pkey, entry in self._tracked_snapshot.items():
                        if entry is not None and entry.id == game.id:
                            pid = pkey[0]  # ProcessKey = (pid, create_time)
                            logger.info(f"Found running PID {pid} for game {game.name}")
                            break
                except Exception as e:
                    logger.debug(f"Could not get PID for {game.name}: {e}")
                
                # Detect save paths with live tracking if available
                try:
                    detected_paths = detect_save_paths(
                        game_name=game.name,
                        exe_path=game.exe_path,
                        pid=pid,  # This enables live tracking if PID is available
                        appid=game.appid,  # Also search using appid for external launcher games
                    )
                    
                    # If general scan is requested and no live tracking was
                    # used, add additional paths from the shared broader scan
                    # (appid is already handled by detect_save_paths).
                    if self.general_scan and not pid:
                        try:
                            from core.save_detector import general_scan_paths, expand_selectable_paths
                            from core.constants import SAVE_FOLDER_HINTS, CAMEL_SPLIT_RE
                            import re as _re

                            # User-configurable "save folder suggestions" from Settings
                            # drive scoring confidence here — same as detect_save_paths.
                            hints = get_config().get("save_folder_hints", SAVE_FOLDER_HINTS)

                            # Extra search terms: exe stem + CamelCase split
                            extra_terms = []
                            if game.exe_path:
                                stem = Path(game.exe_path).stem
                                spaced = _re.sub(CAMEL_SPLIT_RE, ' ', stem).strip()
                                extra_terms = [t for t in (stem, spaced) if t]

                            detected_paths.extend(general_scan_paths(
                                game.name, game.exe_path, hints, detected_paths,
                                extra_terms=extra_terms,
                                should_stop=lambda: self._should_stop,
                            ))
                            if self._should_stop:
                                return

                            # Normalize the merged live+general set: expands
                            # folders without direct files and removes
                            # parent/child duplicates (e.g. "game" + "game/save").
                            detected_paths = expand_selectable_paths(detected_paths)

                        except Exception as e:
                            logger.debug(f"General scan failed for {game.name}: {e}")

                    if detected_paths:
                        method = "live tracking" if pid else ("general scan" if self.general_scan and not pid else "filesystem scan")
                        self.found_paths.emit(game.id, game.name, detected_paths)
                        logger.info(f"Auto-scan found {len(detected_paths)} paths for {game.name} via {method}")
                    
                except Exception as e:
                    logger.debug(f"Scan error for {game.name}: {e}")
                    continue
                
                # Small delay to prevent overwhelming the system
                self.msleep(50)
            
            self.scan_done.emit()
            
        except Exception as e:
            self.error.emit(str(e))


class SavePathItem(QWidget):
    """Widget for displaying a single save path option"""
    
    def __init__(self, game_name: str, game_id: str, paths: List[str], parent=None):
        super().__init__(parent)
        self.game_name = game_name
        self.game_id = game_id
        self.paths = paths
        self._all_detected_paths = paths.copy()  # Store all detected paths for tracking
        self._locally_deleted_paths: List[str] = []  # Deletions pending Save (NOT persisted yet)
        self.checkboxes = []
        self.open_buttons = []
        self.delete_buttons = []

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Game name header
        header = QLabel(f"\U0001f3ae {self.game_name}")
        header.setObjectName("auto_scan_game_header")
        layout.addWidget(header)

        # Path checkboxes with delete buttons + file browser
        self.file_lists = []  # FileListWidget per path
        for path in self.paths:
            self._build_path_row(path)

        # Zero-height marker for the END of the path-row block. Rows added
        # later (restore, or a slower scan pass) are inserted here so they
        # join the list — inserting before the separator instead dropped them
        # UNDER the ignored-paths footer row below, visually outside the list.
        self._rows_end_anchor = QWidget()
        self._rows_end_anchor.setFixedHeight(0)
        layout.addWidget(self._rows_end_anchor)

        # Recovery of paths trashed in THIS confirmation: a counter plus the
        # Manage dialog, which is where restoring now lives. Deliberately
        # scoped to the session — a confirmation panel is about the paths just
        # proposed, and the paths ignored in earlier runs only added noise
        # here. They remain reviewable in Settings → excluded paths.
        ignored_row = QHBoxLayout()
        ignored_row.setContentsMargins(20, 0, 2, 0)
        self._ignored_count_lbl = QLabel()
        self._ignored_count_lbl.setObjectName("auto_scan_muted")
        manage_btn = QPushButton(t("add_game.manage_ignored_paths_btn"))
        lock_min_size(
            manage_btn, scaled(88, self, min_px=72), scaled(24, self, min_px=22),
            policy_h=QSizePolicy.Policy.Minimum,
            policy_v=QSizePolicy.Policy.Fixed)
        manage_btn.setObjectName("auto_scan_sm_btn")
        manage_btn.clicked.connect(self._open_ignored_dialog)
        ignored_row.addWidget(self._ignored_count_lbl, 1)
        ignored_row.addWidget(manage_btn)
        layout.addLayout(ignored_row)
        self._refresh_ignored_count()

        # Add separator — kept as a live reference so add_paths() can insert
        # new rows above it later without rebuilding this whole widget.
        self._separator_line = QFrame()
        self._separator_line.setFrameShape(QFrame.Shape.HLine)
        self._separator_line.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(self._separator_line)

    def _refresh_ignored_count(self):
        """Update the counter of paths trashed during THIS confirmation."""
        if not hasattr(self, "_ignored_count_lbl"):
            return
        n = len(self._locally_deleted_paths)
        if n:
            self._ignored_count_lbl.setText(t("add_game.session_ignored_count", count=n))
        else:
            self._ignored_count_lbl.setText(t("add_game.session_ignored_none"))

    def _open_ignored_dialog(self):
        """Review and restore the paths trashed during this confirmation.

        Restoring lives here rather than on a button of its own: the rows
        already carry an open and a delete button, and a third one competing
        with them for a width the paths themselves need was the wrong trade.

        add_paths() refuses anything still listed in _locally_deleted_paths
        (an in-session deletion must survive a later scan pass re-finding the
        path), so a restored path is dropped from that list FIRST — otherwise
        it would silently fail to come back.
        """
        from PySide6.QtWidgets import QDialog
        from ui.dialogs.add_game_dialog import IgnoredPathsDialog
        dlg = IgnoredPathsDialog(self.game_id, self.game_name, parent=self,
                                 extra_paths=list(self._locally_deleted_paths),
                                 session_only=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            restored = list(getattr(dlg, "restored_paths", []) or [])
            for p in restored:
                if p in self._locally_deleted_paths:
                    self._locally_deleted_paths.remove(p)
            if restored:
                self.add_paths(restored)
                logger.info(f"Restored {len(restored)} just-deleted path(s) for {self.game_name}")
        self._refresh_ignored_count()

    def _build_path_row(self, path: str):
        """Build and append the checkbox+open+delete+file-browser row for one
        path. Shared by init_ui() (initial build) and add_paths() (merging
        in additional paths found by a subsequent scan pass) so both
        produce identical rows.
        """
        from functools import partial
        layout = self.layout()
        path_layout = QHBoxLayout()
        path_layout.setContentsMargins(20, 2, 2, 2)

        # The path is the checkbox's own label and save paths are routinely
        # longer than the panel, so it is elided in the middle: without that
        # the row sizes itself to the whole string and pushes the open and
        # delete buttons off to the right, behind a horizontal scrollbar.
        # Display only — the real path lives in self.paths, never read back
        # off the label.
        from core.registry_saves import is_registry_path, registry_display
        checkbox = ElidedCheckBox()
        checkbox.setObjectName("list_cb_sm")
        if is_registry_path(path):
            checkbox.setTooltipSuffix(t('auto_scan.registry_key_tooltip'))
            checkbox.setFullText(f"\U0001f5dd {registry_display(path)}")
        else:
            checkbox.setFullText(f"\U0001f4c1 {path}")
        checkbox.setChecked(True)
        self.checkboxes.append(checkbox)
        path_layout.addWidget(checkbox, 1)

        # Open button — deciding whether a detected path really holds this
        # game's saves is much easier with the folder (or the registry key)
        # in front of you than from the path string alone. Bound to the path,
        # not to a row index, so deletions never need it rebound.
        open_btn = QPushButton("\U0001f4c2")
        open_btn.setFixedSize(scaled(24, self), scaled(24, self))
        open_btn.setObjectName("auto_scan_icon_btn")
        open_btn.setToolTip(t('add_game.open_folder'))
        open_btn.clicked.connect(partial(self._open_path, path))
        self.open_buttons.append(open_btn)
        path_layout.addWidget(open_btn)

        # Delete button
        delete_btn = QPushButton("\U0001f5d1")
        delete_btn.setFixedSize(scaled(24, self), scaled(24, self))
        delete_btn.setObjectName("auto_scan_icon_btn")
        delete_btn.setToolTip(t('auto_scan.remove_path'))
        delete_btn.clicked.connect(partial(self._on_delete_clicked, len(self.delete_buttons)))
        self.delete_buttons.append(delete_btn)
        path_layout.addWidget(delete_btn)

        # Insert at the end-of-rows anchor if it already exists (i.e. this
        # call came from add_paths() after init_ui() already ran); otherwise
        # just append (still building the initial rows).
        insert_at = layout.indexOf(self._rows_end_anchor) if getattr(self, '_rows_end_anchor', None) else -1
        if insert_at >= 0:
            layout.insertLayout(insert_at, path_layout)
        else:
            layout.addLayout(path_layout)

        # Collapsible file browser under each path
        from ui.widgets.file_list_widget import FileListWidget
        file_list = FileListWidget(path)
        self.file_lists.append(file_list)
        if insert_at >= 0:
            layout.insertWidget(insert_at + 1, file_list)
        else:
            layout.addWidget(file_list)

    def add_paths(self, new_paths: List[str]):
        """Merge additional paths (found by a later scan pass — e.g. a
        fast heuristic scan completing after the instant live-tracking
        pre-fill already displayed something) into this already-built item.

        Only genuinely new paths are added: ones not already shown, and not
        already deleted by the user earlier in this same dialog session
        (respecting an in-session deletion rather than having it silently
        reappear because a slower/different scan pass re-found it).
        """
        from core.save_detector import path_identity as _pid
        _shown = {_pid(p) for p in self.paths}
        _dropped = {_pid(p) for p in self._locally_deleted_paths}
        added = []
        for path in new_paths:
            _key = _pid(path)
            if _key in _shown or _key in _dropped:
                continue
            _shown.add(_key)
            self.paths.append(path)
            # Guarded: a RESTORED path is already in _all_detected_paths
            # (delete_path() drops it from self.paths only). apply_changes()
            # rebuilds save_paths from _all_detected_paths, so appending
            # blindly would write the path into the game twice.
            if path not in self._all_detected_paths:
                self._all_detected_paths.append(path)
            self._build_path_row(path)
            added.append(path)
        return added

    def delete_path(self, index: int):
        """Delete a path from the list by index.

        Deletion is recorded locally in _locally_deleted_paths and is only
        persisted to config when the user clicks Apply.  Pressing Close/Skip
        discards the change.
        """
        if 0 <= index < len(self.paths):
            path_to_delete = self.paths[index]

            # Hide and remove widgets from layout BEFORE deleteLater to prevent
            # stale widget references while deleteLater is pending.
            cb = self.checkboxes[index]
            btn = self.delete_buttons[index]
            # The open button goes with them: only the row's widgets are
            # removed (the empty QHBoxLayout stays), so one left behind would
            # keep floating in a row that no longer has a path.
            open_btn = self.open_buttons[index] if index < len(self.open_buttons) else None
            for w in (cb, btn, open_btn):
                if w is None:
                    continue
                w.setVisible(False)
                if w.parent() and w.parent().layout():
                    w.parent().layout().removeWidget(w)
                w.deleteLater()

            # Track deletion locally — NOT written to config yet
            if path_to_delete not in self._locally_deleted_paths:
                self._locally_deleted_paths.append(path_to_delete)

            # Remove file list widget from UI
            if index < len(self.file_lists):
                fl = self.file_lists[index]
                fl.setVisible(False)
                if fl.parent() and fl.parent().layout():
                    fl.parent().layout().removeWidget(fl)
                fl.deleteLater()
                del self.file_lists[index]

            # Remove from lists
            del self.paths[index]
            del self.checkboxes[index]
            del self.delete_buttons[index]
            if index < len(self.open_buttons):
                del self.open_buttons[index]

            # Rebind remaining delete buttons with corrected indices
            from functools import partial
            for new_idx, btn in enumerate(self.delete_buttons):
                try:
                    btn.clicked.disconnect()
                except RuntimeError:
                    pass
                btn.clicked.connect(partial(self._on_delete_clicked, new_idx))

            logger.info(f"User deleted path for {self.game_name}: {path_to_delete} (pending Apply)")
            self._refresh_ignored_count()

    def _open_path(self, path_str: str, checked=False):
        """Open a proposed path for inspection — same behaviour as the
        Add/Edit Game rows: registry keys go to regedit, folders to the
        system file manager, and a path that doesn't exist (yet) says so
        instead of failing silently."""
        from core.registry_saves import is_registry_path, open_in_regedit
        from ui.helpers import open_in_file_manager
        from ui.modal_helpers import information_window_modal
        if is_registry_path(path_str):
            open_in_regedit(path_str)
            return
        target = Path(path_str)
        if not target.exists():
            information_window_modal(
                self, t('add_game.folder_not_found'),
                t('add_game.folder_not_exist', path=path_str))
            return
        open_in_file_manager(target)

    def _on_delete_clicked(self, idx, checked=False):
        """Stable callback for delete buttons — avoids lambda closure leaks.
        Guards against invalid indices from rapid clicks."""
        if 0 <= idx < len(self.paths):
            self.delete_path(idx)

    def get_selected_paths(self) -> List[str]:
        """Get list of selected paths"""
        selected = []
        deselected = []

        for i, checkbox in enumerate(self.checkboxes):
            if checkbox.isChecked():
                selected.append(self.paths[i])
            else:
                deselected.append(self.paths[i])

        # Remember deselected paths in config
        if deselected:
            config = get_config()
            deselected_paths = config.get("auto_scan_deselected_paths", {})
            if self.game_id not in deselected_paths:
                deselected_paths[self.game_id] = []

            # Add new deselected paths
            for path in deselected:
                if path not in deselected_paths[self.game_id]:
                    deselected_paths[self.game_id].append(path)

            config.set("auto_scan_deselected_paths", deselected_paths)
            logger.debug(f"Remembered {len(deselected)} deselected paths for {self.game_name}")

        return selected

    def get_excluded_files(self) -> dict[str, set[str]]:
        """Get per-path excluded files from file browsers.
        Returns {path_str: set(relative_paths_excluded)}."""
        excluded = {}
        for fl in getattr(self, 'file_lists', []):
            exc = fl.get_excluded_files()
            if exc:
                excluded[fl.get_path()] = exc
        return excluded


class AutoScanDialog(TopmostPinMixin, QDialog):
    """Dialog for confirming auto-discovered save paths"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t('auto_scan.window_title'))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        self.scan_thread = None
        self.path_items: List[SavePathItem] = []
        self.games_without_saves: List[GameEntry] = []
        # True when the user explicitly opened this panel (e.g. via the in-game
        # overlay [i] shortcut). A user-opened panel must NEVER auto-close on
        # "no paths found" — the user asked for it and decides when to close.
        self._user_initiated: bool = False
        self._topmost_timer = None  # periodic HWND_TOPMOST re-pin while shown
        # Cancellable handle for the "no paths found" auto-close timer, so a
        # subsequent Extended Scan can stop it instead of the dialog closing
        # itself mid-scan (which crashed when the scan thread then delivered
        # results to deleted widgets).
        self._autoclose_timer: Optional[QTimer] = None
        # Set by _use_pre_scanned_paths() when this dialog is confirming a
        # single just-exited game (the normal post-game-close flow) rather
        # than running a library-wide scan. Keeps "Extended Scan" scoped to
        # that one game instead of silently picking up every other game in
        # the library that happens to be missing save paths.
        self._single_game_mode_id: Optional[str] = None
        
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel(t('auto_scan.header'))
        header.setObjectName("dialog_title")
        layout.addWidget(header)
        
        description = QLabel(t('auto_scan.description'))
        description.setWordWrap(True)
        description.setObjectName("dialog_desc")
        layout.addWidget(description)
        
        # Progress section
        progress_widget = QWidget()
        progress_layout = QVBoxLayout(progress_widget)
        
        self.progress_label = QLabel(t('auto_scan.preparing_scan'))
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(scaled(4, self))
        self.progress_bar.setVisible(False)
        
        # Extended scan button — disabled while a scan is running
        scan_options_layout = QHBoxLayout()
        self.extended_scan_btn = QPushButton(t('auto_scan.extended_scan_btn'))
        self.extended_scan_btn.setToolTip(t('auto_scan.general_scan_hint'))
        self.extended_scan_btn.setEnabled(False)
        self.extended_scan_btn.clicked.connect(self._restart_with_extended_scan)
        scan_options_layout.addWidget(self.extended_scan_btn)
        scan_options_layout.addStretch()

        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addLayout(scan_options_layout)
        layout.addWidget(progress_widget)
        
        # Results area
        self.results_area = QScrollArea()
        self.results_area.setVisible(False)
        self.results_area.setWidgetResizable(True)
        self.results_area.setFrameShape(QFrame.Shape.NoFrame)

        self.results_widget = QWidget()
        self.results_widget.setObjectName("transparent_bg")
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_area.setWidget(self.results_widget)
        
        layout.addWidget(self.results_area, 1)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        self.stop_btn = QPushButton(t('auto_scan.stop_scan'))
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setVisible(False)
        
        self.apply_btn = QPushButton(t('auto_scan.apply_saves'))
        self.apply_btn.clicked.connect(self.apply_changes)
        self.apply_btn.setVisible(False)
        
        self.skip_btn = QPushButton(t('auto_scan.skip'))
        self.skip_btn.clicked.connect(self.skip)
        self.skip_btn.setVisible(False)
        
        button_layout.addWidget(self.stop_btn)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_btn)
        button_layout.addWidget(self.skip_btn)
        
        # Add "Don't show again" option
        dont_show_layout = QHBoxLayout()
        self.dont_show_cb = QCheckBox(t('auto_scan.dont_show_again'))
        self.dont_show_cb.setToolTip(t('auto_scan.dont_show_tooltip'))
        self.dont_show_cb.setChecked(False)
        
        dont_show_layout.addStretch()
        dont_show_layout.addWidget(self.dont_show_cb)
        
        layout.addLayout(button_layout)
        layout.addLayout(dont_show_layout)

        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=560, min_h=420, scroll=self.results_area,
            list_content=True)

    def start_scan(self, general_scan: bool = False):
        """Start the automatic scan process.  Opens immediately; scanning runs live."""
        # A new scan is starting — cancel any pending no-paths auto-close so
        # it can't fire and close the dialog while this scan is running.
        self._cancel_autoclose()
        self._general_scan_mode = general_scan
        self.extended_scan_btn.setEnabled(False)  # disabled while scanning

        if self._single_game_mode_id:
            # Confirming one specific (just-exited) game — re-fetch it fresh
            # rather than falling back to "every game in the library missing
            # save paths", which would silently change what Extended Scan
            # actually searches for.
            game = get_library().get_by_id(self._single_game_mode_id)
            self.games_without_saves = [game] if game else []
        else:
            # Get games without save paths
            library = get_library()
            self.games_without_saves = [
                game for game in library.all_games()
                if not game.save_paths
            ]

        if not self.games_without_saves:
            self.progress_label.setText(t('auto_scan.all_games_configured'))
            QTimer.singleShot(2000, self.accept)
            return

        self.progress_label.setText(t('auto_scan.scanning_games', count=len(self.games_without_saves)))
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.stop_btn.setVisible(True)
        self.apply_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)

        tracked_snapshot = {}
        try:
            monitor = get_monitor()
            if monitor and monitor.is_active:
                tracked_snapshot = monitor.get_tracked_snapshot()
        except Exception:
            pass
        self.scan_thread = ScanWorkerThread(
            self.games_without_saves, general_scan, tracked_snapshot=tracked_snapshot,
            force=bool(self._single_game_mode_id),
        )
        self.scan_thread.progress.connect(self.update_progress)
        self.scan_thread.found_paths.connect(self.add_found_paths)
        self.scan_thread.scan_done.connect(self.scan_finished)
        self.scan_thread.error.connect(self.scan_error)
        self.scan_thread.start()

    def _restart_with_extended_scan(self):
        """Run the general/extended scan ON TOP of the current results.

        Explicit product requirement: clicking this button must NOT change
        the panel layout, close it, or wipe entries already shown (saved
        paths or live-scan results). New findings are appended to the
        existing list via add_found_paths()'s merge logic; already-shown
        paths are left exactly as they are.
        """
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait(1500)
        # A user-initiated extended scan must never auto-close the dialog,
        # even when it finds nothing new — see scan_finished().
        self._user_extended_scan = True
        self.start_scan(general_scan=True)

    def update_progress(self, current: int, total: int):
        """Update progress bar"""
        self.progress_bar.setValue(current)
        self.progress_label.setText(t('auto_scan.scanning_games', count=f"{current}/{total}"))

    def add_found_paths(self, game_id: str, game_name: str, paths: List[str]):
        """Add discovered paths to the results.

        Matches by game_id (not name) to avoid ambiguity when multiple
        games share the same display name.
        """
        if not game_id:
            logger.warning(f"Could not find game ID for {game_name}")
            return
        
        # Filter out permanently deleted paths and paths with nothing
        # selectable. Deselection (unchecked but kept) is no longer a
        # separate "never show again" mechanism — it's tracked directly on
        # the GameEntry as excluded_save_paths, and a path can legitimately
        # be re-detected in a later session; its checkbox is just
        # initialised unchecked below to match, rather than the path being
        # hidden from the dialog entirely.
        filtered_paths = filter_selectable_paths(game_id, paths)

        # For games with already-confirmed paths, also drop re-detections
        # covered by the confirmed list (same rule as the automatic at-exit
        # flow) — this is the single choke point for everything the dialog
        # shows, including live detections pushed while the panel is open.
        _game = get_library().get_by_id(game_id)
        if _game is not None and _game.save_paths_confirmed and not _game.requires_confirmation:
            filtered_paths = filter_uncovered_paths(filtered_paths, _game.save_paths or [])

        if not filtered_paths:
            logger.info(f"All paths for {game_name} were previously deselected, deleted or already covered")
            return

        # Merge into an already-displayed item for this same game rather
        # than creating a duplicate widget — this is what makes it safe to
        # call add_found_paths() more than once per game, e.g. once from
        # the instant live-tracking pre-fill and again when a fast
        # heuristic scan pass completes shortly after and finds more.
        existing_item = next((it for it in self.path_items if it.game_id == game_id), None)
        if existing_item is not None:
            newly_added = existing_item.add_paths(filtered_paths)
            if newly_added:
                self._apply_saved_path_state(game_id, existing_item, newly_added)
            return

        # _all_detected_paths (set by SavePathItem.__init__ from
        # filtered_paths) must contain ONLY what the user actually sees:
        # apply_changes() writes every entry of that list back into the
        # game's save_paths, so a filtered-out path (previously deleted,
        # nothing selectable, or already covered by a confirmed path) kept
        # in it would be silently re-added on Apply without ever having
        # been shown.
        item = SavePathItem(game_name, game_id, filtered_paths)
        self._apply_saved_path_state(game_id, item, filtered_paths)

        self.path_items.append(item)
        self.results_layout.addWidget(item)

    def _apply_saved_path_state(self, game_id: str, item: "SavePathItem", paths_to_apply: List[str]):
        """Restore excluded/checked state and per-file exclusions for the
        given (newly shown) paths on *item* — shared by the fresh-item and
        merge-into-existing branches of add_found_paths() so both end up
        with identically-initialised rows.
        """
        config = get_config()

        # Reflect the game's current excluded_save_paths: a path already
        # marked excluded starts unchecked here too, so re-opening this
        # dialog doesn't silently flip it back to "will be backed up"
        # just because it got re-detected this session.
        game = get_library().get_by_id(game_id)
        already_excluded = set((game.excluded_save_paths if game else []) or [])
        if already_excluded:
            for i, path in enumerate(item.paths):
                if path in paths_to_apply and path in already_excluded and i < len(item.checkboxes):
                    item.checkboxes[i].setChecked(False)

        # Restore per-file exclusions saved in a previous session
        saved_excl = config.get("auto_scan_excluded_files", {}).get(game_id, {})
        if saved_excl:
            for fl in item.file_lists:
                path_key = fl.get_path()
                if path_key in paths_to_apply and path_key in saved_excl:
                    fl.set_excluded_files(set(saved_excl[path_key]))
                    logger.debug(
                        f"Restored {len(saved_excl[path_key])} excluded files "
                        f"for {item.game_name} path {path_key}"
                    )

    def scan_finished(self):
        """Called when scanning is complete"""
        self.progress_label.setText(t('auto_scan.scan_completed', count=len(self.path_items)))
        self.stop_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        # Only enable extended scan button if we did a normal scan (not already extended)
        if not getattr(self, '_general_scan_mode', False):
            self.extended_scan_btn.setEnabled(True)

        if self.path_items:
            self.results_area.setVisible(True)
            self.apply_btn.setEnabled(True)
            self.apply_btn.setVisible(True)
            self.skip_btn.setEnabled(True)
            self.skip_btn.setVisible(True)
        elif getattr(self, '_user_extended_scan', False) or self._user_initiated:
            # User explicitly opened the panel (overlay [i]) or clicked
            # Extended Scan: never auto-close or re-layout under them — just
            # report and let them decide when to close.
            self.progress_label.setText(t('auto_scan.no_paths_found'))
            self.skip_btn.setEnabled(True)
            self.skip_btn.setVisible(True)
            self.skip_btn.setText(t('auto_scan.close'))
        else:
            self.progress_label.setText(t('auto_scan.no_paths_found'))
            # Cancellable so an Extended Scan clicked within the 2s window
            # stops this close instead of the dialog vanishing mid-scan.
            self._cancel_autoclose()
            self._autoclose_timer = QTimer(self)
            self._autoclose_timer.setSingleShot(True)
            self._autoclose_timer.timeout.connect(self.accept)
            self._autoclose_timer.start(2000)

    def _cancel_autoclose(self):
        """Stop a pending 'no paths found' auto-close, if any."""
        if self._autoclose_timer is not None:
            try:
                self._autoclose_timer.stop()
            except RuntimeError:
                pass
            self._autoclose_timer = None

    def show_idle_empty_state(self):
        """Open in an EMPTY state without scanning anything: live tracking
        had nothing usable to show for this game (e.g. every pending path
        was already excluded). Mirrors scan_finished()'s "user opened the
        panel, nothing found" messaging — Extended Scan is enabled and
        ready, Close is available — but critically never actually runs a
        scan on its own: general_scan is only ever a conscious, opt-in
        action via that button, never something that fires just because
        the user opened the manual panel and there was nothing to show
        them yet.
        """
        self.progress_bar.setVisible(False)
        self.progress_label.setText(t('auto_scan.no_paths_found'))
        self.extended_scan_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)
        self.skip_btn.setVisible(True)
        self.skip_btn.setText(t('auto_scan.close'))

    def scan_error(self, error_msg: str):
        """Handle scan errors"""
        self._cancel_autoclose()
        self.progress_label.setText(t('auto_scan.scan_error', error=error_msg))
        self.skip_btn.setEnabled(True)
        self.skip_btn.setVisible(True)
        self.skip_btn.setText(t('auto_scan.close'))
        self.extended_scan_btn.setEnabled(not getattr(self, '_general_scan_mode', False))

    def stop_scan(self):
        """Stop the scanning process"""
        if self.scan_thread:
            self.scan_thread.stop()
        self.progress_label.setText(t('auto_scan.scan_stopped'))
        self.stop_btn.setVisible(False)
        self.skip_btn.setEnabled(True)
        self.skip_btn.setVisible(True)
        self.skip_btn.setText(t('auto_scan.close'))

    def apply_changes(self):
        """Apply the selected save paths to games"""
        library = get_library()
        config = get_config()
        updated_count = 0
        confirmed_game_ids = []
        games_to_backup = []
        
        for item in self.path_items:
            selected_paths = item.get_selected_paths()          # checked, not deleted
            all_detected_paths = getattr(item, '_all_detected_paths', selected_paths)  # everything shown this round, INCLUDING deleted
            locally_deleted = list(getattr(item, '_locally_deleted_paths', None) or [])
            # Shown this round, left unchecked, but NOT deleted — this is a
            # deselection: it must stay in save_paths (still visible, still
            # re-selectable later) and only be skipped at actual backup time.
            deselected_this_round = [
                p for p in all_detected_paths
                if p not in selected_paths and p not in locally_deleted
            ]

            if selected_paths or all_detected_paths:
                # Resolve the game directly by the id the item was created
                # with. self.games_without_saves is only populated by the
                # "scan the whole library" code path (start_scan()) — when
                # this dialog is opened for a single just-played game via
                # _use_pre_scanned_paths()/show_auto_scan_dialog(game_id=...),
                # that list is empty, which used to mean this whole block
                # silently never ran: nothing was written to the library —
                # no save_paths, no deselections, no deletions, regardless
                # of what the user picked or clicked Apply on.
                game = library.get_by_id(getattr(item, 'game_id', None))
                if game is None:
                    # Legacy fallback for any caller still matching by name.
                    game = next(
                        (g for g in self.games_without_saves if g.name == item.game_name),
                        None,
                    )
                if game is not None:
                    old_paths = list(game.save_paths)  # Store old paths for cleanup
                    old_excluded = set(game.excluded_save_paths or [])

                    # CRITICAL: a scan session only ever shows what THIS scan
                    # actually re-detected (all_detected_paths) — it is NOT a
                    # full picture of every path the game has ever had
                    # confirmed. A pre-existing confirmed path that this
                    # session's scan simply didn't happen to re-detect (the
                    # player didn't touch that save slot this session) must
                    # be left completely untouched here — the user never saw
                    # it in this dialog, so they can't have decided anything
                    # about it. Only all_detected_paths entries are eligible
                    # to be added/kept/dropped based on the choices below.
                    # (Previously `game.save_paths = selected_paths` replaced
                    # the ENTIRE list with only this round's subset, so
                    # deleting even a single new false-positive here silently
                    # wiped out every previously-confirmed path too.)
                    preserved_untouched = [p for p in old_paths if p not in all_detected_paths]
                    kept_from_this_round = [p for p in all_detected_paths if p not in locally_deleted]
                    game.save_paths = preserved_untouched + kept_from_this_round

                    # excluded_save_paths tracks deselected-but-kept paths —
                    # skipped at backup time, but still ordinary, visible,
                    # re-includable entries in save_paths (never removed).
                    # Deleted paths are excluded from THIS set on purpose:
                    # they're gone from save_paths entirely, tracked instead
                    # via _save_user_path_preferences below for restoration
                    # from Settings/Preferences per game.
                    new_excluded = (
                        (old_excluded - set(selected_paths) - set(locally_deleted))
                        | set(deselected_this_round)
                    )
                    game.excluded_save_paths = [p for p in game.save_paths if p in new_excluded]

                    game.save_paths_confirmed = True  # Mark as confirmed by user
                    game.requires_confirmation = False  # No longer requires confirmation
                    library.update_game(game)
                    confirmed_game_ids.append(game.id)
                    games_to_backup.append((game, old_paths))  # Store old paths for cleanup
                    
                    # Save deletions (permanent, goes to per-game ignored-paths
                    # record) — deselections no longer need separate tracking
                    # here since excluded_save_paths above already covers them.
                    self._save_user_path_preferences(
                        game.id, all_detected_paths, selected_paths,
                        locally_deleted_paths=locally_deleted,
                    )

                    # The user just confirmed this game's paths: the temporary
                    # (pre-confirmation) backups created during the session to
                    # protect the still-unconfirmed saves become definitive
                    # history — UNLESS they cover a path the user just deleted
                    # in this same round, in which case they're discarded
                    # instead (per path, since a round can accept some
                    # detections and reject others at once). Runs AFTER
                    # _save_user_path_preferences so deleted paths were
                    # already purged/stripped from them; rotation limits are
                    # re-enforced by the promotion itself.
                    try:
                        from core.backup import get_backup_manager
                        get_backup_manager().resolve_pre_confirmation_backups(
                            game.id, discarded_paths=locally_deleted,
                            note=t('main.auto_in_game'))
                    except Exception as e:
                        logger.warning(
                            f"Could not resolve pre-confirmation backups for {game.name}: {e}")

                    # Save per-file exclusions from file browsers
                    excluded_files: dict[str, list[str]] = {}
                    for fl_item in item.file_lists:
                        exc = fl_item.get_excluded_files()
                        if exc:
                            excluded_files[fl_item.get_path()] = sorted(exc)
                    if excluded_files:
                        all_excl = dict(config.get("auto_scan_excluded_files", {}))
                        if game.id not in all_excl:
                            all_excl[game.id] = {}
                        for path_key, files in excluded_files.items():
                            existing = set(all_excl[game.id].get(path_key, []))
                            all_excl[game.id][path_key] = sorted(existing | set(files))
                        config.set("auto_scan_excluded_files", all_excl)
                        logger.info(
                            f"Saved excluded files for {game.name}: "
                            f"{sum(len(v) for v in excluded_files.values())} files"
                        )
                    
                    updated_count += 1
                    logger.info(f"Updated {len(selected_paths)} save paths for {game.name} (confirmed by user)")
                else:
                    logger.warning(
                        f"apply_changes: could not resolve a library game for "
                        f"item {item.game_name!r} (game_id={getattr(item, 'game_id', None)!r}) "
                        f"— nothing saved for this entry"
                    )
        
        # Clean up existing backups in background thread to avoid UI freeze.
        # Use the backup manager's index lock to prevent racing with a
        # concurrent restore that might be reading the same zip file.
        # Bind the method reference before spawning so the thread does not
        # need to access `self` (the dialog may be destroyed before the
        # thread finishes).
        if games_to_backup:
            import threading
            _cleanup_fn = self._cleanup_wrong_backup_paths
            _work = [(game, old_paths, list(game.save_paths)) for game, old_paths in games_to_backup]
            def _bg_cleanup():
                from core.backup import _index_lock
                for game, old_paths, new_paths in _work:
                    try:
                        with _index_lock:
                            _cleanup_fn(game, old_paths, new_paths)
                    except Exception as e:
                        logger.error(f"Backup path cleanup failed for {game.name}: {e}")
            threading.Thread(target=_bg_cleanup, daemon=True).start()
        
        # Trigger a backup for games that now have save paths.
        # Two settings can request it:
        #   - auto_backup: always back up right after confirmation.
        #   - backup_on_exit: the exit backup normally runs in
        #     _on_game_exited, but when the game closed WITHOUT confirmed
        #     paths it was skipped there — the user confirming the paths in
        #     this dialog (which opens right after exit) is the deferred
        #     completion of that same flow, so honor the setting here.
        #     Only for games not currently running: a running game gets its
        #     regular exit backup later, now that its paths are confirmed.
        def _wants_backup_now(game) -> bool:
            if config.get("auto_backup", False):
                return True
            if config.get("backup_on_exit", True) and game.auto_backup_enabled:
                playing_ids = {g.id for g in get_monitor().currently_playing()}
                return game.id not in playing_ids
            return False

        backup_now = [(g, op) for g, op in games_to_backup if _wants_backup_now(g)]
        if backup_now:
            logger.info(f"Triggering auto-backup for {len(backup_now)} games after auto-scan confirmation")
            for game, _ in backup_now:
                try:
                    # Import here to avoid circular imports
                    from core.backup import get_backup_manager
                    from core.machine import get_machine_id
                    
                    # Check if same machine
                    same = not game.machine_id or get_machine_id() == game.machine_id
                    if same:
                        max_mb = config.get("max_backup_size_mb", 512)
                        backup = get_backup_manager().create_backup(
                            game.id, game.name, game.save_paths,
                            exe_path=game.exe_path,
                            note="auto (post-scan)", max_size_mb=max_mb, force=False,
                            computed_folder_name=game.computed_folder_name,
                        )
                        if backup:
                            game.mark_backed_up(get_machine_id())
                            library.update_game(game)
                            logger.info(f"Auto-backup completed for {game.name}")
                            
                            # Auto-sync if enabled
                            if config.get("auto_sync_after_backup", False):
                                from sync import get_orchestrator
                                orch = get_orchestrator()
                                if orch.is_online():
                                    orch.sync_game(game.id, game.name, game.save_paths, exe_path=game.exe_path)
                                    logger.info(f"Auto-sync completed for {game.name}")
                    else:
                        logger.debug(f"Skipping backup for {game.name} - different machine")
                except Exception as e:
                    logger.error(f"Auto-backup failed for {game.name}: {e}")
        
        # Save "don't show again" preference — per-game, not global.
        # Games flagged here are simply SKIPPED by future at-exit
        # confirmations: whatever the scan finds for them is discarded,
        # never silently added (detected paths can be false positives —
        # e.g. log folders — that only the dialog lets the user reject).
        if self.dont_show_cb.isChecked():
            per_game: dict = dict(config.get("scan_auto_accept_games", {}))
            for gid in confirmed_game_ids:
                per_game[gid] = True
            config.set("scan_auto_accept_games", per_game)
            logger.info(
                f"Per-game scan-dialog suppression enabled for: {confirmed_game_ids}"
            )
        
        self.progress_label.setText(t('auto_scan.games_updated', games=updated_count, paths=len(self.path_items)))
        
        # Auto-close after successful update
        QTimer.singleShot(1500, self.accept)

    def _save_dont_show_preference(self):
        """Save 'don't show again' for games in the current dialog: future
        at-exit confirmations for them are skipped entirely (findings
        discarded, nothing auto-added)."""
        config = get_config()
        per_game: dict = dict(config.get("scan_auto_accept_games", {}))
        suppressed_now: list[str] = []
        for item in self.path_items:
            gid = getattr(item, 'game_id', None)
            if gid:
                per_game[gid] = True
                suppressed_now.append(gid)
        if per_game:
            config.set("scan_auto_accept_games", per_game)
            logger.info(f"Per-game scan-dialog suppression saved on skip for: {list(per_game.keys())}")
        # Suppressing WITHOUT confirming means the detected paths are
        # rejected wholesale — the temporary session backups that covered
        # them go too (confirmed/definitive backups are never touched).
        try:
            from core.backup import get_backup_manager
            bm = get_backup_manager()
            for gid in suppressed_now:
                bm.discard_pre_confirmation_backups(gid)
        except Exception as e:
            logger.warning(f"Could not discard pre-confirmation backups on skip: {e}")

    def _save_user_path_preferences(self, game_id: str, all_detected_paths: list[str], selected_paths: list[str],
                                     locally_deleted_paths: list[str] | None = None):
        """Persist a *deletion* (trash icon) so the scanner never proposes
        that path again, and clean up any existing backup content for it.

        Deletions are written here (at Apply time), NOT in delete_path(),
        so that Close/Skip discards the change. Simple deselection (checkbox
        left unchecked, path NOT deleted) is intentionally NOT handled here
        any more — it's a soft, reversible choice tracked directly on the
        GameEntry as excluded_save_paths (see apply_changes()): the path
        stays in the game's normal save_paths list and its existing backup
        history is left alone, only future backups skip it. Only an actual
        deletion is treated as a hard "this was wrong" decision that also
        purges/strips it from existing backups.
        """
        config = get_config()

        if not locally_deleted_paths:
            return

        deleted_config = dict(config.get("auto_scan_deleted_paths", {}))
        existing_del = deleted_config.get(game_id, [])
        merged_del = list(set(existing_del) | set(locally_deleted_paths))
        deleted_config[game_id] = merged_del
        config.set("auto_scan_deleted_paths", deleted_config)
        logger.info(f"Persisted {len(locally_deleted_paths)} deleted paths for {game_id}")

        remaining_paths = [p for p in all_detected_paths if p not in locally_deleted_paths]
        self._purge_live_backups_for_rejected_paths(game_id, locally_deleted_paths, remaining_paths)

    def _purge_live_backups_for_rejected_paths(
        self, game_id: str, rejected_paths: list[str], kept_paths: list[str]
    ) -> None:
        """Delete backup zips (or strip entries) that cover rejected save paths.

        A backup is fully deleted if it contains *no* file from any kept path.
        Otherwise we strip the rejected entries from the zip in-place.
        """
        try:
            import zipfile, tempfile, shutil, os
            from pathlib import Path as _P
            from core.backup import get_backup_manager

            bm = get_backup_manager()
            backups = bm.get_backups_for_game(game_id)
            if not backups:
                return

            def _belongs_to(arc_name: str, paths: list[str]) -> bool:
                for p in paths:
                    norm = _P(p).as_posix().lstrip("/").lower()
                    if arc_name.lower().startswith(norm):
                        return True
                return False

            for backup in list(backups):
                zip_file = _P(backup.zip_path)
                if not zip_file.exists():
                    continue

                try:
                    with zipfile.ZipFile(zip_file, 'r') as zf:
                        all_names = zf.namelist()
                except Exception:
                    continue

                rejected_names = [n for n in all_names if _belongs_to(n, rejected_paths)]
                kept_names     = [n for n in all_names if _belongs_to(n, kept_paths)]

                if not rejected_names:
                    continue  # nothing to remove

                if not kept_names:
                    # Entire zip only covers rejected paths — delete it
                    try:
                        bm.delete_backup(backup.id)
                        logger.info(f"Deleted live-tracking backup {backup.id} (all paths rejected)")
                    except Exception as e:
                        logger.warning(f"Could not delete backup {backup.id}: {e}")
                    continue

                # Strip rejected entries from the zip
                fd, tmp = tempfile.mkstemp(suffix='.zip', dir=zip_file.parent)
                os.close(fd)
                try:
                    with zipfile.ZipFile(zip_file, 'r') as src:
                        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
                            for item in src.infolist():
                                if item.filename not in rejected_names:
                                    dst.writestr(item, src.read(item.filename))
                    shutil.move(tmp, str(zip_file))
                    logger.info(
                        f"Stripped {len(rejected_names)} rejected entries from backup {backup.id}"
                    )
                except Exception as e:
                    logger.warning(f"Could not strip backup {backup.id}: {e}")
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"_purge_live_backups_for_rejected_paths failed: {e}")

    def _cleanup_wrong_backup_paths(self, game: 'GameEntry', old_paths: list[str], new_paths: list[str]):
        """Clean up existing backups by removing wrong paths while keeping correct ones"""
        try:
            from core.backup import get_backup_manager
            import zipfile
            import tempfile
            from pathlib import Path

            backup_manager = get_backup_manager()
            backups = backup_manager.get_backups_for_game(game.id)
            
            if not backups or not old_paths:
                return
            
            # Find paths that were in old backups but not in new confirmed paths
            wrong_paths = [path for path in old_paths if path not in new_paths]
            
            if not wrong_paths:
                return  # No wrong paths to clean up
            
            logger.info(f"Cleaning up wrong paths from backups for {game.name}: {wrong_paths}")
            
            # Process each backup to remove wrong paths
            cleaned_count = 0
            for backup in backups:
                try:
                    backup_file = Path(backup.zip_path)
                    if not backup_file.exists():
                        continue
                    
                    # Create a temporary backup without wrong paths
                    temp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_file:
                            temp_path = temp_file.name
                            
                            with zipfile.ZipFile(backup_file, 'r') as original_zip:
                                with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as cleaned_zip:
                                    # Copy all files except those from wrong paths
                                    files_to_keep = []
                                    files_to_skip = []
                                    
                                    for file_info in original_zip.infolist():
                                        file_path = file_info.filename
                                        
                                        # Check if this file is from a wrong path
                                        is_wrong_path = False
                                        for wrong_path in wrong_paths:
                                            # Normalize paths for comparison
                                            wrong_normalized = wrong_path.replace('\\', '/').rstrip('/') + '/'
                                            file_normalized = file_path.rstrip('/')
                                            
                                            if file_normalized.startswith(wrong_normalized) or file_normalized == wrong_path.rstrip('/'):
                                                is_wrong_path = True
                                                files_to_skip.append(file_path)
                                                break
                                        
                                        if not is_wrong_path:
                                            # Keep this file with original metadata
                                            files_to_keep.append(file_path)
                                            # Read the file data
                                            file_data = original_zip.read(file_info)
                                            # Create new ZipInfo with original metadata
                                            new_info = zipfile.ZipInfo(file_info.filename)
                                            new_info.compress_type = file_info.compress_type
                                            new_info.external_attr = file_info.external_attr
                                            new_info.date_time = file_info.date_time
                                            new_info.comment = file_info.comment
                                            new_info.create_system = file_info.create_system
                                            new_info.extract_version = file_info.extract_version
                                            new_info.internal_attr = file_info.internal_attr
                                            new_info.reserved = file_info.reserved
                                            
                                            # Write file with preserved metadata
                                            cleaned_zip.writestr(new_info, file_data)
                            
                            # Verify the cleaned backup has files
                            with zipfile.ZipFile(temp_path, 'r') as test_zip:
                                if len(test_zip.infolist()) == 0:
                                    logger.warning(f"Cleaned backup {backup.id} would be empty, keeping original")
                                    # Remove temp file and continue to next backup
                                    Path(temp_path).unlink()
                                    continue
                            
                            Path(temp_path).replace(backup_file)
                            cleaned_count += 1
                            logger.debug(f"Cleaned backup {backup.id} for {game.name}, kept {len(files_to_keep)} files, skipped {len(files_to_skip)} files")
                            
                    except Exception as e:
                        logger.error(f"Failed to clean backup {backup.id} for {game.name}: {e}")
                        # Clean up temp file if it exists
                        if temp_path and Path(temp_path).exists():
                            Path(temp_path).unlink()
                        
                except Exception as e:
                    logger.error(f"Failed to process backup {backup.id} for {game.name}: {e}")
            
            if cleaned_count > 0:
                logger.info(f"Cleaned {cleaned_count} backups for {game.name}, removed wrong paths: {wrong_paths}")
            
        except Exception as e:
            logger.error(f"Failed to cleanup wrong backup paths for {game.name}: {e}")

    def _use_pre_scanned_paths(self, pre_scanned_paths: list[str], game_id: str = None) -> bool:
        """Use pre-scanned paths directly without running a new scan.

        Returns True when there is actually something selectable to show.
        Returns False when every pre-scanned path is excluded/deleted/empty
        — in that case the caller must NOT show the dialog at all: a
        "N paths found" panel with zero selectable entries is exactly the
        false-positive notification this guards against.
        """
        library = get_library()

        # Look up the specific game by ID (works even if it already has save paths)
        game = library.get_by_id(game_id) if game_id else None

        if game is None:
            # Fallback: find games without save paths
            self.games_without_saves = [
                g for g in library.all_games()
                if not g.save_paths
            ]
            if not self.games_without_saves:
                return False
            game = self.games_without_saves[0]

        # Pre-filter BEFORE any UI is prepared: only genuinely selectable
        # paths count toward the "paths found" number the user sees.
        selectable = filter_selectable_paths(game.id, pre_scanned_paths)

        # Paths already covered by confirmed save paths are not news — same
        # rule the automatic at-exit flow applies. Skipped while the game
        # still awaits its FIRST confirmation: there the user must see every
        # detected path, including ones provisionally sitting in save_paths.
        if game.save_paths_confirmed and not game.requires_confirmation:
            selectable = filter_uncovered_paths(selectable, game.save_paths or [])

        if not selectable:
            logger.info(
                f"Background scan for {game.name!r}: all {len(pre_scanned_paths)} "
                "path(s) excluded or already covered — not showing dialog"
            )
            return False

        logger.info(f"Using {len(selectable)} pre-scanned paths from background scan")

        # This dialog is confirming exactly one game (the one that just
        # exited) — remember it so Extended Scan stays scoped to it instead
        # of falling back to start_scan()'s "every game in the library"
        # behaviour, and so the button (disabled by default, only ever
        # re-enabled by the start_scan()/scan_finished() path that this
        # pre-scanned shortcut bypasses entirely) actually becomes usable.
        self._single_game_mode_id = game.id
        self._general_scan_mode = False
        self.games_without_saves = [game]
        self.extended_scan_btn.setEnabled(True)

        self.add_found_paths(game.id, game.name, selectable)
        if not self.path_items:
            # Defensive: add_found_paths applied a stricter filter and
            # nothing survived — same rule, don't show an empty panel.
            return False
        self.results_area.setVisible(True)

        self.progress_label.setText(t('auto_scan.background_paths_found', count=len(selectable)))
        self.progress_bar.setVisible(False)
        self.stop_btn.setVisible(False)
        self.apply_btn.setVisible(True)
        self.skip_btn.setVisible(True)
        self.skip_btn.setText(t('auto_scan.close'))
        return True

        # Deliberately nothing further here. Live tracking identifies saves
        # by actual file content/mtime evidence, not by name/keyword
        # scoring — that is strictly more accurate than the heuristic
        # strategies, not merely a faster approximation of them. Chaining
        # an automatic heuristic pass on top doesn't add coverage, it
        # dilutes an already-correct, already-complete result with
        # generically name-matched folders (e.g. the game's own root)
        # that the precise live result never included in the first place.
        # The user sees these results immediately, with no extra wait; if
        # they want a broader (lower-precision) sweep, Extended Scan above
        # remains a manual, on-demand choice.

    def push_live_paths(self, game_id: str, game_name: str, paths: List[str]):
        """Merge freshly live-detected paths into the OPEN dialog.

        Called by the main window whenever background live tracking produces
        results for the game this (user-opened, single-game) dialog is
        showing — so the panel keeps receiving detections in real time
        instead of being a frozen snapshot of open-time state.
        """
        if not paths or game_id != self._single_game_mode_id:
            return
        before = len(self.path_items)
        self.add_found_paths(game_id, game_name, paths)
        if not self.path_items:
            return
        # Reveal/refresh the results UI (mirrors scan_finished's found-state)
        self.results_area.setVisible(True)
        self.apply_btn.setEnabled(True)
        self.apply_btn.setVisible(True)
        self.skip_btn.setEnabled(True)
        self.skip_btn.setVisible(True)
        if len(self.path_items) != before:
            self.progress_label.setText(
                t('auto_scan.background_paths_found', count=len(self.path_items)))

    def skip(self):
        """Skip and close dialog without saving changes"""
        # Save "Don't show again" preference even when skipping
        if self.dont_show_cb.isChecked():
            self._save_dont_show_preference()
        self.reject()

    def showEvent(self, event):
        """Once shown, keep this panel pinned above the game. A game can
        re-assert its own topmost / toggle fullscreen on a refresh and push a
        plain always-on-top dialog behind it (dropping it from primary to
        secondary); a periodic re-pin — topmost, never re-activating — keeps
        it primary, the same approach the overlay/modal app uses. A panel the
        user opened explicitly is also given focus once."""
        super().showEvent(event)
        if self._user_initiated:
            # Focus a user-opened panel once (never on every tick, so we don't
            # fight the user typing) — WA_ShowWithoutActivating suppressed the
            # activation on show.
            try:
                self.raise_()
                self.activateWindow()
            except RuntimeError:
                pass
        self.start_topmost_pin()

    def closeEvent(self, event):
        """Handle dialog close — reject to avoid auto-confirming paths"""
        self.stop_topmost_pin()
        if self.scan_thread and self.scan_thread.isRunning():
            self.stop_scan()
            try:
                self.scan_thread.progress.disconnect()
                self.scan_thread.found_paths.disconnect()
                self.scan_thread.scan_done.disconnect()
                self.scan_thread.error.disconnect()
            except (RuntimeError, TypeError):
                pass
            if not self.scan_thread.wait(2000):
                logger.warning("Scan thread did not stop within 2s — detaching (it will exit on its own)")
        self.reject()
        from ui.helpers import trim_process_memory
        QTimer.singleShot(250, trim_process_memory)


def show_auto_scan_dialog(parent=None, pre_scanned_paths: Optional[list[str]] = None,
                          game_id: str = None,
                          user_initiated: bool = False,
                          auto_scan: bool = True) -> Optional["AutoScanDialog"]:
    """Show the auto scan dialog (non-modal so overlay notifications stay interactive).

    Returns the dialog when shown, None otherwise (truthy/falsy for callers
    that only care whether it opened; the instance lets the main window keep
    feeding live-detected paths into the open panel via push_live_paths).

    Args:
        parent: Parent widget
        pre_scanned_paths: Optional list of pre-scanned paths to use instead of running new scan
        game_id: Optional game ID to associate pre-scanned paths with the correct game
        user_initiated: True when the user explicitly opened this panel (e.g.
            via the in-game overlay [i] shortcut). Such a panel must never
            auto-close on "no paths found".
        auto_scan: Only relevant when pre_scanned_paths is empty/None — whether
            to automatically start a scan. Defaults to True (existing
            behavior). Pass False to open the panel in its idle "nothing
            to show yet" state instead: a scan (especially the Extended/
            general one) must stay a conscious, opt-in action for the
            user, never something that starts on its own just because the
            panel was opened with nothing pre-scanned to show.
    """
    dialog = AutoScanDialog(parent)
    dialog._user_initiated = user_initiated
    # Non-modal: use show() so the overlay remains interactive while this dialog is open.
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    # The parent MainWindow is usually hidden in the tray and a game is
    # often fullscreen in the foreground: a plain Qt.Dialog has no way to
    # surface itself there (raise_() only re-stacks our own windows) and
    # ends up invisible behind the game. Same game-friendly window recipe
    # as UnknownGameDialog: stay on top without stealing the game's focus.
    flags = (Qt.WindowType.Dialog
             | Qt.WindowType.CustomizeWindowHint
             | Qt.WindowType.WindowTitleHint
             | Qt.WindowType.WindowCloseButtonHint)
    if platform.system() == "Windows":
        flags |= Qt.WindowType.WindowStaysOnTopHint
    dialog.setWindowFlags(flags)
    dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    if pre_scanned_paths:
        if not dialog._use_pre_scanned_paths(pre_scanned_paths, game_id=game_id):
            # Nothing selectable (excluded / already saved / empty) — the
            # dialog must not open, and no "paths found" signal reaches
            # the user at all.
            dialog.deleteLater()
            return None
    else:
        # No pre-scanned paths: with a game_id the scan stays scoped to
        # that game (force-rescanned even if it already has confirmed
        # paths — used by the in-game [i] overlay shortcut).
        if game_id:
            dialog._single_game_mode_id = game_id
        if auto_scan:
            QTimer.singleShot(0, dialog.start_scan)
        else:
            # Caller explicitly does not want a scan started on its own
            # (e.g. the overlay's manual-open fallback when live tracking
            # had nothing usable to hand over) — open empty instead;
            # Extended Scan stays a conscious, opt-in action for the user.
            dialog.show_idle_empty_state()

    dialog.show()
    dialog.raise_()
    return dialog
