"""
SaveSync - Main Window
NVIDIA App-inspired sidebar with Overview, Library, Sync, Backups, Settings.
"""
import logging
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, Slot, Signal, QEvent
from PySide6.QtGui import QIcon, QAction, QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QFrame, QStackedWidget,
    QSystemTrayIcon, QMenu, QApplication, QStatusBar, QMessageBox,
)

from i18n import t, get_engine
from ui.styles.theme import palette
from ui.modal_helpers import question_window_modal, warning_window_modal
from ui.overlay import OverlayWidget
from ui.main_window_cloud import CloudFlowsMixin
from ui.blur_modal import BlurModalWidget
from ui.pages.overview_page import OverviewPage
from ui.pages.library_page import LibraryPage
from ui.pages.sync_page import SyncPage
from ui.pages.backups_page import BackupsPage
from ui.pages.settings_page import SettingsPage
from ui.dialogs.add_game_dialog import AddGameDialog
from ui.dialogs.auto_scan_dialog import show_auto_scan_dialog
from core.config_manager import get_config
from core.library import get_library, GameEntry
from core.monitor import get_monitor
from core.backup import get_backup_manager
from core.machine import get_machine_id
from hotkeys import get_hotkey_manager
from sync import get_orchestrator
from ui.helpers import scaled


class NavButton(QPushButton):
    """Sidebar entry. Optional trailing status dot for put-away background work:
    blinking while running, solid green when done, solid red on failure."""

    def __init__(self, text: str, icon: str = "", parent=None):
        super().__init__(parent)
        self._icon  = icon
        self._label = text
        self._status = ""          # "" | "running" | "done" | "failed"
        self._blink_on = True
        self._update_text()
        self.setObjectName("nav_btn")
        self.setCheckable(False)
        self.setFixedHeight(scaled(44, self))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._blink = QTimer(self)
        self._blink.setInterval(520)
        self._blink.timeout.connect(self._on_blink)

    def _update_text(self):
        self.setText(f"  {self._icon}  {self._label}" if self._icon else f"  {self._label}")

    def set_active(self, active: bool):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def update_label(self, text: str):
        self._label = text
        self._update_text()

    def set_status(self, status: str):
        """Show a trailing status indicator (running / done / failed / clear)."""
        status = (status or "").strip()
        if status not in ("", "running", "done", "failed"):
            status = ""
        self._status = status
        self.setProperty("notice", "true" if status else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if status == "running":
            self._blink_on = True
            if not self._blink.isActive():
                self._blink.start()
        else:
            self._blink.stop()
            self._blink_on = True
        self.update()

    def _on_blink(self):
        self._blink_on = not self._blink_on
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._status:
            return
        if self._status == "running" and not self._blink_on:
            return
        if self._status == "running":
            color = QColor(palette("accent"))
        elif self._status == "done":
            color = QColor(palette("success"))
        else:
            color = QColor(palette("error"))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        d = 8
        x = self.width() - 16
        y = (self.height() - d) // 2
        painter.drawEllipse(x, y, d, d)
        painter.end()


class MainWindow(CloudFlowsMixin, QMainWindow):
    # Emitted from the integrity-sweep worker thread; the slot that shows the
    # notice must run on the GUI thread, which a signal guarantees.
    backup_verify_problems = Signal(int, int)   # bad, total
    # Same reason: the regression check runs on a worker thread.
    save_regression_found = Signal(str, str, bool)  # game_id, newest_backup_id, after_restore
    # In-app self-check (config history restore, …) failed on a worker thread.
    self_check_failed = Signal(str, str)  # check_id, detail
    # Same thread: progress + completion of the automatic self-check sweep,
    # surfaced in the sidebar like Backup/Sync Tutti instead of blocking.
    self_check_progress = Signal(str, int, int)  # check_id, index, total
    self_check_done = Signal()
    # Launcher URL → exe fuzzy search finished off the GUI thread.
    launcher_exe_resolved = Signal(str, str, str)  # game_id, url, exe_path ("" if none)
    # GitHub Releases check finished off the GUI thread.
    update_available = Signal(object)  # ReleaseInfo

    def __init__(self):
        super().__init__()
        self.setWindowTitle(t("app.name"))
        # Soft floor — keep room for scroll mediation on low res (720p).
        # A high % min forced near-fullscreen and fought quality-limited scale.
        from PySide6.QtWidgets import QApplication as _QApp
        _scr = _QApp.primaryScreen()
        if _scr is not None:
            _ag = _scr.availableGeometry()
            self.setMinimumSize(
                min(720, max(640, int(_ag.width() * 0.50))),
                min(520, max(480, int(_ag.height() * 0.55))),
            )
        else:
            self.setMinimumSize(640, 480)
        # Explicitly set window icon for proper taskbar display in frozen builds.
        # Prefer .ico for Windows taskbar, fall back to .png.
        _icon = QApplication.instance().windowIcon()
        if _icon.isNull():
            _icon_candidates = [
                Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / "assets" / "icon.ico",
                Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / "assets" / "icon.png",
            ]
            for _ic_path in _icon_candidates:
                if _ic_path.exists():
                    _icon = QIcon(str(_ic_path))
                    break
        if not _icon.isNull():
            self.setWindowIcon(_icon)
        # Default footprint tracks ui_scale so auto compensation (e.g. 4K @
        # Win 100% ≈ 200%) still fits without page scroll on a normal open.
        from ui.helpers import ui_scale as _ui_scale
        _s = _ui_scale(self)
        _dw, _dh = int(round(1360 * _s)), int(round(880 * _s))
        if _scr is not None:
            _ag = _scr.availableGeometry()
            _dw = min(_dw, max(640, int(_ag.width() * 0.92)))
            _dh = min(_dh, max(480, int(_ag.height() * 0.92)))
        self.resize(max(640, _dw), max(480, _dh))
        self._restore_window_state()
        self._active_nav_idx   = 0
        self._nav_buttons: list[NavButton] = []
        self._overlay: Optional[OverlayWidget] = None
        self._blur_modal: Optional[BlurModalWidget] = None
        self._is_modal_mode = False  # Track if window is in modal mode
        self._hidden_for_game = False  # True while minimised to tray for a running game
        # Store original window flags to restore properly
        self._original_window_flags = None
        # exe_path → display_name for unknown games seen this session
        self._pending_unknown: dict[str, str] = {}
        import threading as _th
        # Guards _pending_auto_scans: written by live-tracking scan threads
        # and the watchdog bridge, read on the GUI thread.
        self._bg_scan_lock = _th.Lock()
        self._pending_auto_scans: dict[str, list] = {}
        self._live_tracking_timers: dict[str, "QTimer"] = {}
        # Thread-safe deques for background restore/backup results
        from collections import deque
        self._quick_restore_results: deque = deque()  # (success, game_name) from overlay
        self._restore_results: deque = deque()         # ("step1"|"step2", ...) from full restore
        self._backup_results: deque = deque()
        self._restore_lock = _th.Lock()
        self._backup_lock = _th.Lock()
        # Adaptive backup queue (Backup Tutti + single backups share the cap).
        self._backup_job_queue: deque = deque()   # dicts: game_id, force, silent
        self._backup_inflight: set[str] = set()
        self._backup_queued: set[str] = set()
        self._backup_batch: dict | None = None    # active Tutti tally + persist
        self._backup_max_inflight: int = 2
        self._manual_path_dlg = None
        # game_id → has_cloud bool, written by the background thread in
        # _check_cloud_on_launch and consumed by _on_cloud_check_result on
        # the GUI thread — keeps the network round-trip off the GUI thread.
        self._cloud_check_lock = _th.Lock()
        self._cloud_check_results: dict[str, bool] = {}
        # game_id -> callback, set by _check_cloud_on_launch(on_resolved=...)
        # and invoked by _on_cloud_check_result() once the (backgrounded)
        # network check completes. A plain Python callable can't be passed
        # through QMetaObject.invokeMethod's Q_ARG (Qt's meta-object system
        # only marshals Qt/QVariant-compatible types), so it's stored here
        # instead and looked up by the game_id that DOES cross that boundary.
        self._cloud_check_on_resolved: dict[str, Callable] = {}
        # game_ids whose next cloud-check prompt must be skipped once: set
        # when the user has ALREADY answered the cloud question through
        # another flow (e.g. "download & add to library" on an unknown
        # game) — re-prompting "cloud saves available, download?" moments
        # later for the same decision would be a duplicate.
        self._suppress_cloud_prompt_once: set[str] = set()
        # game_id -> "no_local" | "different_machine": a cloud notification
        # SHOWN but not yet resolved by an explicit user action (the action
        # handlers pop it). Closing the prompt does NOT pop it — that is what
        # lets _toggle_overlay() re-summon it via the hotkey until decided.
        self._pending_cloud_notification: dict[str, str] = {}
        # library game_id → orphan backup game_id (hand-added archive to adopt
        # when the user accepts the same cloud-saves notification).
        self._pending_orphan_adopt: dict[str, str] = {}
        # Unanswered "is this process really that game?" prompts:
        # (process_name, game_id) → game name. Same role as the dict above —
        # it keeps the question re-summonable by the hotkey until answered.
        self._pending_unverified: dict[tuple, str] = {}
        # game_id → QTimer for periodic in-game backup
        self._ingame_backup_timers: dict[str, "QTimer"] = {}
        # Pending "both" conflict resolution: chain upload after download
        self._pending_both_upload: Optional[GameEntry] = None
        # game_id → the conflict's local/remote timestamps, kept from
        # detection until the user opens the comparison window from the
        # conflict notification (empty for a conflict surfaced at launch out
        # of a status recorded in an earlier session).
        self._pending_conflict_info: dict[str, dict] = {}
        # Games where the user answered "keep local" to the cross-machine
        # divergence dialog: for the rest of the session their auto syncs
        # silently go up-only instead of re-asking on every sync.
        self._cross_machine_local_only: set[str] = set()
        # Track overlay/dialog shown state per exe (session-only)
        self._session_shown_exes: set[str] = set()
        self._overlay_shown_exes: set[str] = set()
        # Open auto-scan dialog (single-game): live-detected paths are pushed
        # into it as they arrive (see _push_paths_to_open_scan_dialog).
        self._live_scan_dlg = None
        # Dialog currently backed by the blur vignette (in-game overlay
        # flows outside full modal mode) — see _show_blur_for_dialog.
        self._blur_dialog = None
        self._really_quit = False   # set by _quit_app: closeEvent must not divert to tray
        self._pending_cloud_found: list[tuple] = []  # (name, exe_path, cloud_meta|None) awaiting UI callback
        self._pending_cloud_verify: dict = {}        # exe_path -> {"name", "folders"} for the verify dialog
        import threading as _threading
        self._cloud_found_lock = _threading.Lock()
        self._exit_dialog_shown_exes: set[str] = set()
        self._tray_click_timer = QTimer()
        self._tray_click_timer.setSingleShot(True)
        self._tray_click_timer.timeout.connect(self._on_tray_single_click)

        self._setup_ui()
        self._setup_tray()
        self._setup_blur_modal()
        self._setup_overlay()
        self._setup_monitors()
        self._setup_hotkeys()
        self._connect_orchestrator()
        self._connect_i18n()
        self._setup_cleanup()
        self.backup_verify_problems.connect(self._on_backup_verify_problems)
        self.save_regression_found.connect(self._on_save_regression)
        # Index zip-existence sweep (may already have finished before connect).
        _bm = get_backup_manager()
        _bm.index_validation_failed.connect(self._on_index_validation_failed)
        _bm.index_validation_recovered.connect(self._on_index_validation_recovered)
        _err = _bm.last_validation_error()
        if _err:
            QTimer.singleShot(0, lambda e=_err: self._on_index_validation_failed(e))
        self.launcher_exe_resolved.connect(self._on_launcher_exe_resolved)
        # backup_id SaveSync itself last restored, per game — landing on that
        # state is the intended outcome, not something to warn about.
        self._last_restored: dict[str, str] = {}
        # Games already warned about in the current episode. The post-restore
        # check samples several times, and a regression that persists would
        # otherwise raise the same prompt at every sample.
        self._regression_warned: set = set()
        # Shown but not yet acknowledged, per game: game_id -> (backup_id,
        # after_restore). A warning the player never saw — the overlay was
        # missed, or something else took the screen — must not be lost, so the
        # hotkey re-summons it for as long as it sits here. Only the
        # acknowledge button and an actual restore take it out.
        self._pending_regression: dict[str, tuple[str, bool]] = {}
        # In-flight regression check + rearm (same pattern as watcher.py):
        # a second call for the same game does not start a parallel scan, but
        # is replayed when the current one finishes — so a genuine new session
        # still gets its _regression_warned / _pending_regression reset.
        self._regression_check_lock = threading.Lock()
        self._regression_checking: set[str] = set()
        self._regression_rearm: dict[str, bool] = {}
        self._setup_backup_verify()
        self._setup_auto_export_config()
        self._setup_update_check()
        self._setup_startup_self_checks()
        # A previous run may have died between suspending a game for a forced
        # restore and resuming it. The game would still be frozen, with
        # nothing on screen to explain why.
        try:
            from core.backup import resume_orphaned_process
            if resume_orphaned_process():
                self._status_bar.showMessage(t("backup.resumed_frozen_game"), 8000)
        except Exception as e:
            logger.debug(f"Orphaned-process resume check failed: {e}")
        
        # Store original window flags after all setup is complete
        self._original_window_flags = self.windowFlags()

    @Slot(str)
    def handleSavesyncUrl(self, url_str: str):
        from urllib.parse import unquote
        from core.resolvers import parse_launcher_url, launch_with_url
        url_str = unquote(url_str)
        parsed = parse_launcher_url(url_str)
        if parsed:
            appid = parsed.get("appid")
            if appid:
                logger.info(f"Handling launcher URL: {parsed.get('launcher')} appid={appid}")
                launch_with_url(url_str)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setFrameShape(QFrame.Shape.NoFrame)
        sidebar.setObjectName("sidebar")
        # Design 220 restores the classic sidebar footprint (original UI was
        # a fixed 220px); floor resists DPI downscale crush.
        _side_w = scaled(220, self, min_px=210)
        sidebar.setFixedWidth(_side_w)
        self._sidebar = sidebar
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        self._sidebar_logo = QLabel(t("app.name").upper())
        self._sidebar_logo.setObjectName("sidebar_logo")
        self._sidebar_tagline = QLabel(t("app.tagline"))
        self._sidebar_tagline.setObjectName("sidebar_tagline")
        sl.addWidget(self._sidebar_logo)
        sl.addWidget(self._sidebar_tagline)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sl.addWidget(sep)

        self._nav_defs = [
            ("nav.overview",  "◈"),
            ("nav.library",   "📚"),
            ("nav.sync",      "☁"),
            ("nav.backups",   "💾"),
            ("nav.settings",  "⚙"),
        ]
        for i, (key, icon) in enumerate(self._nav_defs):
            btn = NavButton(t(key), icon)
            btn.clicked.connect(lambda _, idx=i: self._switch_page(idx))
            self._nav_buttons.append(btn)
            sl.addWidget(btn)

        # Under Settings: the way back to a batch web search that was put away.
        # Not a page — click the search BatchProgressNotice to reopen the panel.
        # (Legacy NavButton removed: same done/total bar as Backup/Sync Tutti.)

        # Same idea for Add/Edit Game: ✕ during exe/web/detect shelves the
        # dialog. Several shelved dialogs can coexist — one NavButton each.
        self._shelved_adds_host = QWidget()
        self._shelved_adds_layout = QVBoxLayout(self._shelved_adds_host)
        self._shelved_adds_layout.setContentsMargins(0, 0, 0, 0)
        self._shelved_adds_layout.setSpacing(0)
        self._shelved_adds_host.setVisible(False)
        sl.addWidget(self._shelved_adds_host)
        # [{dlg, btn}, ...] — order matches sidebar top→bottom
        self._shelved_add_entries: list[dict] = []

        # Batch Backup/Sync Tutti + online title search: done/total + thin bar.
        from ui.widgets.batch_progress import BatchProgressNotice
        self._backup_batch_notice = BatchProgressNotice()
        self._sync_batch_notice = BatchProgressNotice()
        self._search_batch_notice = BatchProgressNotice()
        self._verify_batch_notice = BatchProgressNotice()
        self._search_batch_notice.set_activatable(True)
        self._search_batch_notice.activated.connect(self._show_game_search_panel)
        # Save-editor loads that were put away to the sidebar keep reporting
        # here; clicking one that finished reopens the loaded save's editor
        # (same idea as the search notice reopening its panel).
        self._cheats_load_notice = BatchProgressNotice()
        self._cheats_load_notice.set_activatable(True)
        self._cheats_load_notice.activated.connect(
            lambda _=False: self._reopen_shelved_load())
        sl.addWidget(self._backup_batch_notice)
        sl.addWidget(self._sync_batch_notice)
        sl.addWidget(self._search_batch_notice)
        sl.addWidget(self._verify_batch_notice)
        sl.addWidget(self._cheats_load_notice)

        sl.addStretch()

        # Credits button — just above the Online/Offline status
        self._credits_btn = QPushButton(t("credits.title"))
        self._credits_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._credits_btn.setFlat(True)
        self._credits_btn.clicked.connect(self._show_credits)
        self._credits_btn.setObjectName("credits_nav_btn")
        sl.addWidget(self._credits_btn)

        # Under Credits, but a page like any other: same NavButton, same
        # stack, same active highlight — only the position differs.
        self._cheats_nav_btn = NavButton(t("cheats.nav"), "🎲")
        _cheats_idx = 5
        self._cheats_nav_btn.clicked.connect(
            lambda _=False, idx=_cheats_idx: self._switch_page(idx))
        self._nav_buttons.append(self._cheats_nav_btn)
        sl.addWidget(self._cheats_nav_btn)

        # Status dot at bottom of sidebar
        self._sidebar_status = QLabel(t("status.offline"))
        self._sidebar_status.setStyleSheet(
            f"color: {palette('text_muted')}; font-size: {scaled(10, self)}px; padding: 8px 16px;"
        )
        sl.addWidget(self._sidebar_status)

        mid = get_machine_id()[:8]
        self._machine_lbl = QLabel(f"ID: {mid}\u2026")
        self._machine_lbl.setObjectName("sidebar_machine")
        sl.addWidget(self._machine_lbl)

        root.addWidget(sidebar)

        # Content stack
        self._stack = QStackedWidget()
        self._stack.setObjectName("content_area")

        self._overview_page  = OverviewPage()
        self._library_page   = LibraryPage()
        self._sync_page      = SyncPage()
        self._backups_page   = BackupsPage()
        self._settings_page  = SettingsPage()
        # Cheats is built on first open — heavy editor UI unused by most sessions.
        self._cheats_page    = None

        for page in (self._overview_page, self._library_page,
                     self._sync_page, self._backups_page, self._settings_page):
            self._stack.addWidget(page)
        self._stack.addWidget(QWidget())  # cheats placeholder (index 5)

        root.addWidget(self._stack, 1)

        # Wire library page signals
        self._library_page.add_game_requested.connect(self._show_add_game)
        self._library_page.scan_folder_requested.connect(self._show_scan_folder)
        self._library_page.folder_dropped.connect(self._show_scan_folder_at)
        self._library_page.cheats_requested.connect(self._open_cheats_for)
        self._library_page.backup_requested.connect(self._backup_game)
        self._library_page.restore_requested.connect(self._restore_game_latest)
        self._library_page.remove_requested.connect(self._remove_game)
        self._library_page.edit_requested.connect(self._edit_game)
        self._library_page.sync_requested.connect(self._sync_game)
        self._library_page.launch_requested.connect(self._launch_game)
        self._library_page.review_provisional_requested.connect(self._show_provisional_paths_manager)

        # Wire overview page signals
        self._overview_page.backup_requested.connect(self._backup_game)
        self._overview_page.backup_all_requested.connect(self._start_backup_all)
        self._overview_page.open_library.connect(lambda: self._switch_page(1))
        self._overview_page.open_sync.connect(lambda: self._switch_page(2))
        self._overview_page.refresh_all_requested.connect(self._on_refresh_all_pages)

        # Wire backups page signals
        self._backups_page.backup_requested.connect(self._backup_game)
        self._backups_page.backup_all_requested.connect(self._start_backup_all)
        self._backups_page.restore_requested.connect(self._restore_game_by_id)
        self._backups_page.manual_paths_requested.connect(self._show_manual_path_dialog)
        # Manual "Verifica Backup" runs on a worker thread; surface it in the
        # sidebar so the UI keeps working while the sweep runs.
        self._backups_page.verify_started.connect(self._on_verify_batch_started)
        self._backups_page.verify_progress.connect(self._on_verify_batch_progress)
        self._backups_page.verify_finished.connect(self._on_verify_batch_finished)

        # Sync batch progress (orchestrator → sidebar)
        orch = get_orchestrator()
        orch.batch_progress.connect(self._on_sync_batch_progress)
        orch.batch_finished.connect(self._on_sync_batch_finished)

        # Settings
        self._settings_page.hotkey_changed.connect(self._update_hotkey)
        self._settings_page.hotkeys_reload.connect(self._setup_hotkeys)
        self._settings_page.theme_changed.connect(self._on_theme_changed)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(t("status.ready"))

        self._switch_page(0)

        # Orphanize legacy Aggiungi-percorso stubs once BackupManager exists.
        QTimer.singleShot(200, self._migrate_manual_path_stubs)
        # Resume unfinished Backup/Sync Tutti / Aggiunta multipla after UI is up.
        QTimer.singleShot(400, self._resume_pending_batch_jobs)
        # Re-apply fonts/chrome when the window moves to another monitor
        # (resolution_scale follows availableGeometry width).
        QTimer.singleShot(0, self._install_dpi_change_watch)
        # Automated background memory trimmer (periodic cleanup when idle)
        self._auto_memory_trim_timer = QTimer(self)
        self._auto_memory_trim_timer.setInterval(60000)
        self._auto_memory_trim_timer.timeout.connect(self._on_auto_memory_trim_tick)
        self._auto_memory_trim_timer.start()

        self._ui_scale_reapply_timer = QTimer(self)
        self._ui_scale_reapply_timer.setSingleShot(True)
        self._ui_scale_reapply_timer.setInterval(200)
        self._ui_scale_reapply_timer.timeout.connect(self._reapply_ui_scale)
        self._last_ui_scale = None

    def _on_auto_memory_trim_tick(self):
        """Periodic background memory cleanup: free working set and caches when app is idle."""
        try:
            from core.monitor import get_monitor
            if get_monitor().currently_playing():
                return  # Skip trimming while user is actively playing a game
        except Exception:
            pass
        try:
            from ui.helpers import trim_process_memory
            trim_process_memory()
        except Exception:
            pass

    def _install_dpi_change_watch(self):
        """Event-driven DPI/scale change detection (Qt screen signals).

        Windows scaling changes (Settings → Display → Scale) re-perceive the
        screens: Qt then emits dots-per-inch signals on the affected QScreen
        (see _wire_dpi_screens for the per-binding set). The previous
        attempt listened to ``primaryScreenChanged`` / window ``screenChanged``
        — those only fire on monitor changes, never on a scaling change, which
        is why the 5 s poll was bolted on as the real detector.

        The 30 s safety poll was REMOVED: when the app hops screens quickly
        (a TV plugged in and out) the poll could re-apply a stale scale
        right after the signals already fixed it, leaving the UI painted in
        a mirrored/stale version until the next interaction — the Qt screen
        signals are the whole detector now."""
        from ui.helpers import ui_scale
        self._last_ui_scale = ui_scale(self)
        app = QApplication.instance()
        if app is None:
            return
        self._wired_screen_ids: set[int] = set()
        self._wire_dpi_screens(app.screens())
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        wh = self.windowHandle()
        if wh is not None:
            wh.screenChanged.connect(self._on_host_screen_changed)
            try:
                if hasattr(wh, "devicePixelRatioChanged"):
                    wh.devicePixelRatioChanged.connect(self._on_screen_dpi_changed)
            except Exception:
                pass

    def _wire_dpi_screens(self, screens):
        # PySide6 6.9 does not expose QScreen.devicePixelRatioChanged — the
        # DPI re-perception after a Windows scaling change is picked up via
        # the dots-per-inch signals (guarded per binding version).
        for s in screens:
            if id(s) in self._wired_screen_ids:
                continue
            try:
                for sig in ("logicalDotsPerInchChanged",
                            "physicalDotsPerInchChanged",
                            "physicalSizeChanged",
                            "virtualGeometryChanged"):
                    if hasattr(s, sig):
                        getattr(s, sig).connect(self._on_screen_dpi_changed)
            except Exception:
                pass
            self._wired_screen_ids.add(id(s))

    def _on_screen_added(self, screen):
        logger.info(f"Screen added: wiring DPI signals for {screen.name()}")
        self._wire_dpi_screens([screen])

    def _on_screen_removed(self, screen):
        self._wired_screen_ids.discard(id(screen))

    def _on_screen_dpi_changed(self, _value=None):
        logger.info("DPI change detected via Qt screen signals")
        self._ui_scale_reapply_timer.start()

    def _on_host_screen_changed(self, _screen=None):
        logger.info(f"Screen changed signal received, triggering DPI reapply")
        self._ui_scale_reapply_timer.start()
        # Windows can also shrink the window while monitors are settling
        # (a low-res TV plugged in and out). The saved size is still the
        # user's; re-apply it once the new screen is stable.
        QTimer.singleShot(600, self._refresh_window_geometry_after_screen)

    def _refresh_window_geometry_after_screen(self):
        """Re-apply the saved window size after a monitor change.

        The DPI reapply grows the window with the scale, but a monitor swap
        alone does not always trigger it (same scale, different resolution):
        the window would stay small until the user manually resizes. This
        restores the saved size — clamped to the new screen — and only ever
        GROWS the window, never shrinks a size the user set deliberately.
        """
        try:
            if self.windowState() & Qt.WindowState.WindowMaximized:
                return
            cfg = get_config()
            geo = cfg.get("window_geometry", None)
            if not isinstance(geo, dict):
                return
            w = max(900, int(geo.get("w", 1100)))
            h = max(600, int(geo.get("h", 720)))
            if self.width() >= w and self.height() >= h:
                return
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                ag = screen.availableGeometry()
                w = min(w, max(640, int(ag.width() * 0.96)))
                h = min(h, max(480, int(ag.height() * 0.96)))
            self.resize(max(self.width(), w), max(self.height(), h))
        except Exception:
            pass
    
    def _reapply_ui_scale(self):
        """Refresh theme fonts + grow/shrink all windows when scale changed.

        Single coherent flow: window geometry → theme/fonts → page style
        cascade (via _on_theme_changed and _force_complete_refresh) →
        fixed-dimension recalc → settings slider sync. Each piece lives in
        exactly one place; earlier versions also refreshed pages, library
        cards (_load_library AND _rebuild_view), sidebar and overlay from
        here, restyling every widget 2-3 times per scale change."""
        from ui.helpers import (
            ui_scale, scale_all_top_level_windows,
            _recalculate_all_scaled_dimensions, _trace_scaled_state,
            clear_dialog_geometries,
        )
        from ui.styles.theme import get_theme_manager
        cur = ui_scale(self)
        prev = self._last_ui_scale
        if prev is not None and abs(cur - prev) < 0.02:
            return
        _trace_scaled_state(f"before {prev}->{cur}")
        self._last_ui_scale = cur
        logger.info(f"Applying UI scale change from {prev} to {cur}")
        scale_all_top_level_windows(prev if prev is not None else 1.0, cur)
        # Dialog footprints saved at the OLD scale would reopen stale-bigger
        # (or smaller) until manually resized — drop them for a fresh fit.
        # (Settings preview already clears them; this covers the live DPI path.)
        try:
            from core.config_manager import get_config
            if get_config().get("ui_scale_auto", True):
                clear_dialog_geometries()
        except Exception:
            pass
        theme = get_config().get("theme", "dark")
        get_theme_manager().apply(theme, QApplication.instance())
        self._on_theme_changed(theme)

        # Force complete refresh of all widgets
        self._force_complete_refresh()

        # Recalculate fixed-size chrome at the new scale
        try:
            _recalculate_all_scaled_dimensions()
        except Exception as e:
            logger.warning(f"Failed to recalculate scaled widgets: {e}")

        # Update settings page slider and percentage to reflect new scale
        if hasattr(self, "_settings_page") and self._settings_page is not None:
            try:
                current_scale = ui_scale(self)
                # Align controls AND the dirty snapshot so the automatic DPI
                # switch never looks like a user change.
                self._settings_page.sync_external_ui_scale(current_scale)
                logger.info(f"Updated settings page scale UI to {current_scale}")
            except Exception as e:
                logger.warning(f"Failed to update settings page scale UI: {e}")
        _trace_scaled_state(f"after {prev}->{cur}")
    
    def _force_complete_refresh(self):
        """Force complete refresh of all child widgets."""
        try:
            from ui.helpers import _refresh_child_widgets
            _refresh_child_widgets(self)
            logger.info("Forced complete refresh of all child widgets")
            
            # Force rebuild of problematic components
            self._rebuild_problematic_components()
        except Exception as e:
            logger.warning(f"Failed to force complete refresh: {e}")
    
    def _rebuild_problematic_components(self):
        """Rebuild components that don't scale properly with DPI changes."""
        try:
            # Rebuild sidebar navigation slots
            if hasattr(self, "_sidebar"):
                try:
                    self._sidebar.updateGeometry()
                    self._sidebar.update()
                    logger.info("Rebuilt sidebar")
                except Exception as e:
                    logger.warning(f"Failed to rebuild sidebar: {e}")
            
            # Rebuild overlay
            try:
                if hasattr(self, '_overlay') and self._overlay is not None:
                    self._overlay.updateGeometry()
                    self._overlay.update()
                    logger.info("Rebuilt overlay")
                else:
                    logger.info("No overlay instance found to rebuild")
            except Exception as e:
                logger.warning(f"Failed to rebuild overlay: {e}")
            
            # Force rebuild of game cards in library
            if hasattr(self, "_library_page") and self._library_page is not None:
                try:
                    self._library_page._rebuild_view()
                    logger.info("Rebuilt library cards")
                except Exception as e:
                    logger.warning(f"Failed to rebuild library cards: {e}")
            
            # Force rebuild of sync page components
            if hasattr(self, "_sync_page") and self._sync_page is not None:
                try:
                    self._sync_page.updateGeometry()
                    self._sync_page.update()
                    logger.info("Rebuilt sync page")
                except Exception as e:
                    logger.warning(f"Failed to rebuild sync page: {e}")
            
            # Force rebuild of backups page components
            if hasattr(self, "_backups_page") and self._backups_page is not None:
                try:
                    self._backups_page.updateGeometry()
                    self._backups_page.update()
                    logger.info("Rebuilt backups page")
                except Exception as e:
                    logger.warning(f"Failed to rebuild backups page: {e}")
            
            # Force rebuild of overview page components
            if hasattr(self, "_overview_page") and self._overview_page is not None:
                try:
                    self._overview_page.updateGeometry()
                    self._overview_page.update()
                    logger.info("Rebuilt overview page")
                except Exception as e:
                    logger.warning(f"Failed to rebuild overview page: {e}")
                    
        except Exception as e:
            logger.warning(f"Failed to rebuild problematic components: {e}")

    def _migrate_manual_path_stubs(self):
        try:
            n = get_library().migrate_manual_path_stubs()
            if n:
                try:
                    self._backups_page._load_games()
                    self._backups_page._refresh_list()
                except Exception:
                    pass
                try:
                    self._library_page._load_library()
                except Exception:
                    pass
        except Exception:
            logger.exception("Manual-path stub migration failed")

    def _style_credits_btn(self):
        """Theme-owned via #credits_nav_btn — kept as a hook for theme refresh."""
        self._credits_btn.setObjectName("credits_nav_btn")

    def _show_credits(self):
        from ui.dialogs.credits_dialog import CreditsDialog
        CreditsDialog(self).exec()

    def _on_sync_batch_progress(self, done: int, total: int, name: str):
        if total <= 0:
            return
        if done == 0 and not self._sync_batch_notice.isVisible():
            self._sync_batch_notice.begin(t("batch.sync_label"), total, name or "")
        else:
            self._sync_batch_notice.update_progress(done, total, name or "")

    def _on_sync_batch_finished(self, done: int, name: str = ""):
        if done == 1 and name:
            msg = t("batch.sync_done_one", name=name)
        else:
            msg = t("batch.sync_done", done=done)
        self._sync_batch_notice.finish(msg)
        # One aggregated toast at the end (plain append to the already-built
        # queue). Single-game sync toasts stay suppressed during the batch.
        try:
            if self._overlay:
                self._overlay.show_batch_done("sync", done, name if done == 1 else "")
        except Exception:
            logger.debug("batch toast after Sync Tutti failed", exc_info=True)
        self._update_sidebar_status()
        try:
            self._overview_page.refresh()
        except Exception:
            pass
        # The batch moved real data — the library cards' sync status, the
        # backups list (freshly downloaded zips) and the overview activity
        # would otherwise stay stale until the user happens to trigger a
        # rebuild ("Sync Tutti didn't update anything" report). Library
        # reload is chunked and only runs when the page is visible; the
        # backups list refreshes in place.
        try:
            self._library_page.wipe_and_reload()
        except Exception:
            pass
        try:
            self._backups_page._load_games()
            self._backups_page._refresh_list()
        except Exception:
            pass
        from ui.helpers import trim_process_memory
        QTimer.singleShot(400, trim_process_memory)

    def _resume_pending_batch_jobs(self):
        """Restart Backup/Sync Tutti / Aggiunta multipla left unfinished."""
        from core import pending_batch_jobs as _pbj
        from core.concurrency import backup_max_inflight, sync_max_inflight

        bak = _pbj.get_job(_pbj.KEY_BACKUP_ALL)
        if bak and bak.get("pending_ids"):
            ids = list(bak["pending_ids"])
            completed = list(bak.get("completed_ids") or [])
            logger.info(
                f"Resuming Backup Tutti: {len(ids)} remaining "
                f"({len(completed)} already done)")
            self._backup_max_inflight = backup_max_inflight()
            self._backup_batch = {
                "pending_ids": ids,
                "completed_ids": completed,
                "force": bool(bak.get("force")),
                "started_at": bak.get("started_at") or "",
                "source": bak.get("source") or "resume",
                "total": len(ids) + len(completed),
                # Genuinely NEW backups (dedup-skipped games must not inflate
                # the completion message: 21 checked ≠ 21 created).
                "created_ids": [],
            }
            get_library().begin_bulk()
            first = get_library().get_by_id(ids[0])
            self._backup_batch_notice.begin(
                t("batch.backup_label"),
                self._backup_batch["total"],
                first.name if first else "")
            self._backup_batch_notice.update_progress(
                len(completed), self._backup_batch["total"],
                first.name if first else "")
            for gid in ids:
                self._enqueue_backup(
                    gid, force_full=bool(bak.get("force")),
                    silent=True, part_of_batch=True)
            self._pump_backup_queue()

        syn = _pbj.get_job(_pbj.KEY_SYNC_ALL)
        if syn and syn.get("pending_ids"):
            orch = get_orchestrator()
            if orch.is_online():
                pending_set = set(syn["pending_ids"])
                jobs = [j for j in (syn.get("jobs") or [])
                        if j.get("game_id") in pending_set]
                if not jobs:
                    # Rebuild minimal jobs from library
                    for gid in syn["pending_ids"]:
                        e = get_library().get_by_id(gid)
                        if e and e.save_paths:
                            jobs.append({
                                "game_id": e.id,
                                "game_name": e.name,
                                "save_paths": list(e.save_paths),
                                "exe_path": e.exe_path or "",
                                "computed_folder_name": e.computed_folder_name or "",
                                "name_history": list(e.name_history or []),
                            })
                if jobs:
                    logger.info(f"Resuming Sync Tutti: {len(jobs)} remaining")
                    # Preserve completed tally in notice via enqueue rebuild
                    orch._sync_max_inflight = sync_max_inflight()
                    orch.enqueue_sync_batch(jobs, source="resume")
            else:
                logger.info("Pending Sync Tutti kept on disk — provider offline")

        man = _pbj.get_job(_pbj.KEY_MANUAL_BATCH)
        if man:
            self._show_manual_path_dialog(resume_state=man)

        sea = _pbj.get_job(_pbj.KEY_SEARCH_BATCH)
        if sea and sea.get("pending_ids"):
            pending = list(sea["pending_ids"])
            completed = list(sea.get("completed_ids") or [])
            logger.info(
                f"Resuming batch web search: {len(pending)} remaining "
                f"({len(completed)} already done)")
            self._start_batch_game_search(
                pending,
                prior_done=len(completed),
                prior_matched=int(sea.get("matched") or 0),
                prior_completed_ids=completed,
            )

    def _show_manual_path_dialog(self, resume_state: dict | None = None):
        """Open Aggiunta multipla with show() so ✕ can shelve to the sidebar."""
        from ui.dialogs.manual_path_dialog import ManualPathDialog
        existing = self._manual_path_dlg
        if existing is not None:
            try:
                existing.unshelve()
                return
            except RuntimeError:
                self._manual_path_dlg = None
        dlg = ManualPathDialog(self)
        self._manual_path_dlg = dlg
        dlg.shelved.connect(lambda: self._on_manual_path_shelved(dlg))
        dlg.shelve_status.connect(lambda: self._refresh_manual_path_shelf(dlg))
        dlg.finished.connect(lambda _r: self._on_manual_path_finished(dlg))
        if resume_state:
            dlg.restore_persisted_state(resume_state)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _refresh_manual_path_shelf(self, dlg):
        for e in self._shelved_add_entries:
            if e.get("dlg") is not dlg:
                continue
            btn = e.get("btn")
            if btn is None:
                return
            try:
                btn.set_status("running" if dlg.has_shelvable_work() else "done")
                btn.update_label(dlg.shelve_nav_label())
                btn.setToolTip(dlg.shelve_nav_tooltip())
            except RuntimeError:
                pass
            return

    def _on_manual_path_shelved(self, dlg):
        entry = None
        for e in self._shelved_add_entries:
            if e.get("dlg") is dlg:
                entry = e
                break
        if entry is None:
            btn = NavButton(t("manual_path.shelved_nav"), "📁")
            btn.clicked.connect(lambda _=False, d=dlg: self._resurrect_manual_path(d))
            self._shelved_adds_layout.addWidget(btn)
            entry = {"dlg": dlg, "btn": btn, "kind": "manual_path"}
            self._shelved_add_entries.append(entry)
            self._shelved_adds_host.setVisible(True)
        self._refresh_manual_path_shelf(dlg)
        QTimer.singleShot(0, lambda d=dlg: self._clear_phantom_add_game_modal(d))

    def _resurrect_manual_path(self, dlg):
        try:
            dlg.unshelve()
        except RuntimeError:
            pass

    def _on_manual_path_finished(self, dlg):
        if getattr(dlg, "added_entries", None):
            try:
                self._backups_page._load_games()
                self._backups_page._refresh_list()
            except Exception:
                pass
            try:
                self._library_page.refresh_styles()
            except Exception:
                pass
        if self._manual_path_dlg is dlg:
            self._manual_path_dlg = None
        # Remove sidebar entry
        keep = []
        for e in self._shelved_add_entries:
            if e.get("dlg") is dlg:
                btn = e.get("btn")
                if btn is not None:
                    self._shelved_adds_layout.removeWidget(btn)
                    btn.deleteLater()
            else:
                keep.append(e)
        self._shelved_add_entries = keep
        self._shelved_adds_host.setVisible(bool(keep))

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        icon = QApplication.instance().windowIcon()
        if icon.isNull():
            # Fallback: load from assets directly
            _icon_candidates = [
                Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / "assets" / "icon.ico",
                Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / "assets" / "icon.png",
            ]
            for _ic_path in _icon_candidates:
                if _ic_path.exists():
                    icon = QIcon(str(_ic_path))
                    break

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip(t("app.name"))

        tray_menu = QMenu()
        tray_menu.addAction(QAction(t("tray.open"), self, triggered=self.show_and_raise))
        tray_menu.addSeparator()
        tray_menu.addAction(QAction(t("tray.overview"),      self, triggered=lambda: (self.show_and_raise(), self._switch_page(0))))
        tray_menu.addAction(QAction(t("tray.library"),       self, triggered=lambda: (self.show_and_raise(), self._switch_page(1))))
        tray_menu.addAction(QAction(t("tray.backups"),       self, triggered=lambda: (self.show_and_raise(), self._switch_page(3))))
        tray_menu.addSeparator()
        tray_menu.addAction(QAction(t("tray.quit"), self, triggered=self._quit_app))

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # Cancel any pending single click timer
            self._tray_click_timer.stop()
            self.show_and_raise()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Start timer for single click detection
            self._tray_click_timer.start(250)  # 250ms delay for double click detection

    def _on_tray_single_click(self):
        # Single click action: show overlay
        if self._overlay:
            stats = self._overview_page.get_stats_for_overlay()
            self._overlay.show_manual(stats)

    def show_and_raise(self):
        """Show and bring to front from any state including minimised."""
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray_for_game(self):
        """Minimise SaveSync to the system tray when a game starts, so it stays
        out of the way during play. The counterpart _restore_from_tray_after_game
        brings it back on game exit. No-op if disabled, already hidden, or while
        the modal/blur app is showing."""
        if not get_config().get("hide_to_tray_on_game_launch", True):
            return
        if self._is_modal_mode or self._hidden_for_game or not self.isVisible():
            return
        try:
            self._save_window_state()
            self.hide()
            self._hidden_for_game = True
        except RuntimeError:
            pass

    def _restore_from_tray_after_game(self, exiting_id: str = ""):
        """Bring SaveSync back from the tray once NO game is still playing — the
        counterpart to _hide_to_tray_for_game. Only restores what WE hid
        (_hidden_for_game), and stays hidden while another game is still
        running so overlapping sessions don't pop the window early."""
        if not self._hidden_for_game:
            return
        try:
            others = [g for g in get_monitor().currently_playing() if g.id != exiting_id]
            if others:
                return   # another game is still running — stay in the tray
        except Exception:
            pass
        self._hidden_for_game = False
        self.show_and_raise()

    def _quit_app(self):
        """Full application exit (tray 'Quit'). Since Qt 6.3 quit() first
        CLOSES every top-level window and ABORTS the quit if one ignores
        its close event — which our minimize-to-tray closeEvent does, so a
        tray Quit with the window open just re-hid it to the tray. The
        flag makes that close pass accept unconditionally."""
        self._really_quit = True
        QApplication.quit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                from ui.helpers import trim_process_memory
                QTimer.singleShot(150, trim_process_memory)

    def closeEvent(self, event):
        config = get_config()

        # Explicit quit in progress (tray Quit, or the no-tray branch
        # below): never divert this close into the tray.
        if getattr(self, '_really_quit', False):
            self._save_window_state()
            event.accept()
            return

        if self._is_modal_mode:
            self._hide_modal_app()
            event.ignore()
            return

        # Always persist window geometry before hiding or quitting
        self._save_window_state()

        if config.get("minimize_to_tray", True):
            event.ignore()
            self.hide()
            from ui.helpers import trim_process_memory
            trim_process_memory()
        else:
            event.accept()
            # Mark before quit(): its own close-all-windows pass re-enters
            # closeEvent, which must keep accepting.
            self._really_quit = True
            QApplication.quit()

    def _save_window_state(self):
        """Persist current window geometry and maximised state to config.

        Uses config.save() (immediate flush) instead of the debounced set(),
        so the data reaches disk before the process exits.
        """
        config = get_config()
        is_max = bool(self.windowState() & Qt.WindowState.WindowMaximized)
        config.set("window_maximized", is_max)

        # normalGeometry() returns the pre-maximise size even when maximised,
        # so we can always store it without an oversized window on next launch.
        try:
            geo = self.normalGeometry()
        except Exception:
            geo = self.geometry()

        config.set("window_geometry", {
            "x": geo.x(), "y": geo.y(),
            "w": geo.width(), "h": geo.height(),
        })
        # Force immediate disk write — debounced write timer may not fire before exit
        config.save()

    def _restore_window_state(self):
        """Restore window geometry from config, falling back to defaults.
        
        Also performs emergency safety check: if saved geometry is unusable
        (window too large for screen or completely off-screen), resets to safe scale.
        """
        config = get_config()
        geo = config.get("window_geometry", None)
        if geo and isinstance(geo, dict):
            try:
                from PySide6.QtWidgets import QApplication as _QApp
                screen = _QApp.primaryScreen()
                available = screen.availableGeometry() if screen else None
                # Only floor absurd values (corrupt config) — do NOT inflate a
                # legitimately saved size: max(900, w) would trip the emergency
                # reset below on small screens (e.g. a real 800x600 window).
                x = int(geo.get("x", 100))
                y = int(geo.get("y", 100))
                w = max(200, int(geo.get("w", 1100)))
                h = max(200, int(geo.get("h", 720)))

                # Emergency safety check: if window is too large for screen
                if available:
                    screen_w = available.width()
                    screen_h = available.height()
                    if w > screen_w * 0.9 or h > screen_h * 0.9:
                        # Emergency reset: window too large, clamp to a safe
                        # size. This used to ALSO disable auto-scale
                        # (ui_scale_auto=False + factor 1.0) — but the saved
                        # geometry can be oversized for a totally benign
                        # reason (a monitor swap since the last run), and
                        # silently turning the user's auto-scale off on every
                        # such boot is what made "auto scale randomly
                        # disabled" reports: the scale itself is fine, only
                        # the saved window no longer fits this screen.
                        logger.warning(
                            f"Emergency: Saved window size ({w}x{h}) too large for screen "
                            f"({screen_w}x{screen_h}). Clamping to safe size."
                        )
                        # Use safe default geometry
                        w = min(1100, int(screen_w * 0.8))
                        h = min(720, int(screen_h * 0.8))
                        x = available.x() + (screen_w - w) // 2
                        y = available.y() + (screen_h - h) // 2

                        # Show emergency toast after window is shown
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(100, self._show_dpi_emergency_toast)
                    else:
                        # Clamp position so window stays on-screen. When the
                        # saved size fits the screen (≤90%), clamping x/y is
                        # safe in both dimensions.
                        x = max(available.x(), min(x, available.right() - w))
                        y = max(available.y(), min(y, available.bottom() - h))

                self.setGeometry(x, y, w, h)
            except Exception:
                pass
        if config.get("window_maximized", False):
            self.showMaximized()

    def _ensure_cheats_page(self):
        """Create the Cheats page on first use (replaces the stack placeholder)."""
        if self._cheats_page is not None:
            return self._cheats_page
        from ui.pages.cheats_page import CheatsPage
        page = CheatsPage()
        page.set_load_notice(self._cheats_load_notice)
        old = self._stack.widget(5)
        self._stack.removeWidget(old)
        old.deleteLater()
        self._stack.insertWidget(5, page)
        self._cheats_page = page
        return page

    def _reopen_shelved_load(self):
        """Sidebar notice clicked: back to the Cheats page, restore progress or open loaded editor."""
        page = self._ensure_cheats_page()
        self._switch_page(5)
        page.on_sidebar_notice_clicked()


    def _show_dpi_emergency_toast(self):
        """Show emergency DPI reset toast notification."""
        try:
            from PySide6.QtWidgets import QLabel, QWidget
            from PySide6.QtCore import Qt, QTimer
            from ui.styles.theme import palette
            from i18n import t
            from ui.helpers import scaled
            
            # Create toast widget
            toast = QWidget()
            toast.setObjectName("dpi_emergency_toast")
            
            # Use scaled dimensions for styling
            pad = scaled(12, self)
            border_radius = scaled(8, self)
            toast.setStyleSheet(f"""
                #dpi_emergency_toast {{
                    background: {palette('bg_card')};
                    border: 1px solid {palette('warning')};
                    border-radius: {border_radius}px;
                    padding: {pad}px;
                }}
            """)
            
            layout = QVBoxLayout(toast)
            layout.setContentsMargins(scaled(16, self), scaled(12, self), scaled(16, self), scaled(12, self))
            layout.setSpacing(scaled(4, self))
            
            title = QLabel(t("dpi.emergency_title"))
            title.setStyleSheet(f"color: {palette('warning')}; font-weight: bold; font-size: {scaled(13, self)}px;")
            layout.addWidget(title)
            
            msg = QLabel(t("dpi.emergency_message_auto"))
            msg.setStyleSheet(f"color: {palette('text')}; font-size: {scaled(11, self)}px;")
            msg.setWordWrap(True)
            layout.addWidget(msg)
            
            # Position toast at top center of main window with scaled dimensions
            geo = self.geometry()
            toast_w = min(scaled(400, self), geo.width() - scaled(40, self))
            toast_h = toast.sizeHint().height()
            x = geo.x() + (geo.width() - toast_w) // 2
            y = geo.y() + scaled(20, self)
            toast.setGeometry(x, y, toast_w, toast_h + scaled(40, self))
            toast.setParent(self)
            toast.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
            toast.show()
            
            # Auto-hide after 5 seconds
            QTimer.singleShot(5000, toast.hide)
        except Exception:
            pass

    def _open_cheats_for(self, game_id: str):
        """The library's "Cheats" entry: open the editor already on that
        game, so the search step is skipped."""
        if self._ensure_cheats_page().open_for_game(game_id):
            self._switch_page(5)

    def _switch_page(self, idx: int):
        # If leaving previous page, invoke on_page_leave hook to cancel workers/pumps
        if self._active_nav_idx is not None and self._active_nav_idx != idx:
            old_page = self._stack.widget(self._active_nav_idx)
            if old_page is not None and hasattr(old_page, 'on_page_leave') and callable(old_page.on_page_leave):
                try:
                    old_page.on_page_leave()
                except Exception:
                    logger.debug("on_page_leave failed", exc_info=True)
        if idx == 5:
            self._ensure_cheats_page()
        for i, btn in enumerate(self._nav_buttons):
            btn.set_active(i == idx)
        self._stack.setCurrentIndex(idx)
        self._active_nav_idx = idx
        # A theme switch that happened while this page was hidden left its
        # inline, palette-dependent styles on the old palette — apply them now,
        # before it is painted.
        page = self._stack.widget(idx)
        if getattr(page, "_styles_stale", False):
            self._refresh_page_styles(page)
        # Overview / Library / Backups / Sync load after the page paints.
        if idx == 0:
            if hasattr(self, '_overview_page') and self._overview_page:
                self._overview_page.refresh_on_enter()
        elif idx == 1:
            self._library_page.on_page_enter()
        elif idx == 2:
            self._sync_page.ensure_loaded()
        elif idx == 3:
            self._backups_page.ensure_loaded()
        elif idx == 4:
            if hasattr(self._settings_page, 'ensure_loaded'):
                self._settings_page.ensure_loaded()
            if hasattr(self._settings_page, '_load_suppression_list'):
                self._settings_page._load_suppression_list()
        elif idx == 5:
            self._cheats_page.on_page_enter()

    def _on_refresh_all_pages(self):
        """Overview refresh button: wipe every open page so the next visit
        re-runs its async chunk pump (fresh reveal, fresh data)."""
        try:
            if get_monitor().currently_playing():
                return  # in-game: wiping pages would fight live file writes
        except Exception:
            pass
        for page in (self._library_page, self._sync_page, self._backups_page,
                     self._settings_page):
            wipe = getattr(page, "wipe_and_reload", None)
            if callable(wipe):
                try:
                    wipe()
                except Exception:
                    logger.debug(
                        f"wipe_and_reload failed for {type(page).__name__}",
                        exc_info=True)
        if self._cheats_page is not None:
            try:
                self._cheats_page.wipe_and_reload()
            except Exception:
                logger.debug("wipe_and_reload failed for CheatsPage",
                             exc_info=True)
        try:
            from ui.helpers import trim_process_memory
            trim_process_memory()
        except Exception:
            pass

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _setup_overlay(self):
        self._overlay = OverlayWidget()
        self._overlay.action_requested.connect(self._on_overlay_action)
        self._overlay.dont_show_again.connect(self._on_suppress_overlay)
        self._overlay.exclusive_blocked.connect(self._on_overlay_blocked_by_fullscreen)

    def _on_overlay_blocked_by_fullscreen(self, title: str, message: str):
        """Overlay could not show because a game is in exclusive fullscreen.

        In exclusive fullscreen no window can appear without destroying the
        game's display mode — the only feedback we can give is an audio cue.
        """
        try:
            import winsound
            # Play the system "notification" sound (async, non-blocking)
            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def _setup_blur_modal(self):
        """Setup the blur modal background widget."""
        self._blur_modal = BlurModalWidget()
        self._blur_modal.background_clicked.connect(self._on_blur_background_clicked)
        
        # Track focus changes to handle modal behavior
        QApplication.instance().focusObjectChanged.connect(self._on_focus_changed)

    def _show_tracking_toast_if_playing(self, game_id: str, delay_ms: int = 350):
        """Show the deferred "now tracking" toast for a game whose cloud
        prompt has just been resolved.

        When a cloud prompt is going to be shown at launch, the plain
        tracking toast is skipped (show_toast=False in
        _start_tracking_after_cloud_check) to avoid racing the same overlay
        widget — the prompt's resolution must therefore hand over to the
        toast (parent → child notification order), otherwise the session is
        tracked without ever being communicated. Called by every resolution
        path of the cloud prompts; no-op if the game already exited.
        """
        entry = get_library().get_by_id(game_id)
        if not entry or not self._overlay:
            return
        if not get_config().get("show_overlay_on_launch", True):
            return
        playing_ids = {g.id for g in get_monitor().currently_playing()}
        if entry.id not in playing_ids:
            return
        from core.engines.game_engine import engine_display, engine_for_game
        eng = engine_display(engine_for_game(entry))
        QTimer.singleShot(
            delay_ms, lambda n=entry.name, e=entry.exe_path, eng=eng:
                self._overlay.show_game_launched(n, e, eng))


    def _on_overlay_action(self, action: str, context: str):
        # Matched FIRST: this context is "procname|game_id", not an exe path,
        # so it must never fall through to a branch that treats it as one.
        if action in ("confirm_process_match", "reject_process_match"):
            proc_name, _, game_id = context.partition("|")
            self._pending_unverified.pop((proc_name, game_id), None)   # answered
            get_monitor().confirm_unverified_match(
                proc_name, game_id, accept=(action == "confirm_process_match"))
            return
        # Same shape ("game_id|backup_id"), so it is matched before anything
        # that would read the context as an executable path.
        if action in ("restore_newest", "force_restore"):
            game_id, _, backup_id = context.partition("|")
            self._pending_regression.pop(game_id, None)   # acted on
            self._restore_after_regression(game_id, backup_id,
                                           freeze=(action == "force_restore"))
            return
        # The player says they have seen it: stop re-summoning it. Same
        # context shape, so it is matched here rather than falling through to
        # a branch that would read the context as an executable path.
        if action == "regression_ack":
            game_id, _, _bk = context.partition("|")
            self._pending_regression.pop(game_id, None)
            return
        if action == "add_game":
            self._auto_add_game_from_overlay(context)
        elif action == "download_saves":
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                get_orchestrator().sync_game(entry.id, entry.name, entry.save_paths, direction="down", exe_path=entry.exe_path, computed_folder_name=entry.computed_folder_name, name_history=list(entry.name_history))
                self._show_tracking_toast_if_playing(entry.id)
        elif action == "dismiss":
            # "Later" on a cloud prompt (sync_prompt kind): the prompt
            # suppressed the launch toast, so hand over to it now.
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                self._show_tracking_toast_if_playing(entry.id)
        elif action == "resolve_conflict_details":
            # Conflict notification primary: open the local-vs-cloud
            # comparison. The notification is NOT popped here — closing the
            # window without choosing has to leave the question re-summonable
            # (_handle_conflict_choice pops it once something is decided).
            entry = get_library().get_by_exe(context)
            if entry:
                self._open_conflict_dialog(entry)
        elif action in ("conflict_keep_local", "conflict_keep_cloud",
                        "conflict_keep_both"):
            # One-click resolutions from that notification's dropdown: same
            # handler as the comparison window's buttons, so "keep both"
            # still backs up locally before downloading, and "keep local"
            # still goes up-only for the rest of the session.
            entry = get_library().get_by_exe(context)
            if entry:
                self._handle_conflict_choice(entry, action.rsplit("_", 1)[1])
                self._show_tracking_toast_if_playing(entry.id)
        elif action == "homonym_library_game":
            # "That cloud folder is a different game with the same title":
            # move this game onto its own cloud-unique folder instead of
            # syncing into the other one, and don't download anything.
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                self._pending_conflict_info.pop(entry.id, None)
                self._claim_unique_cloud_folder(entry)
                self._show_tracking_toast_if_playing(entry.id)
        elif action == "download_saves_unknown_game":
            # State A primary: download from the ACTUAL candidate folder (which
            # may be a suffixed variant, e.g. only Alpha_2 exists), not the
            # recomputed base name — else it would pull from an empty folder.
            stash = self._pending_cloud_verify.pop(context, None)
            _folder = ""
            if stash and stash.get("folders"):
                _folder = stash["folders"][0].get("folder", "")
            self._add_and_download_unknown(context, force_folder_name=_folder)
        elif action in ("verify_details_unknown_game", "verify_conflicts_unknown_game"):
            # Show what the cloud copy/copies are registered as and let the user
            # download the right one or declare a same-name different game.
            self._open_cloud_verify_dialog(context)
        elif action == "homonym_unknown_game":
            # Direct "it's a homonym" shortcut — from the State A dropdown, or as
            # State B's dropdown (where a plain keep-local has no clear folder).
            # Gives the game its OWN cloud-unique folder and skips download, same
            # as choosing homonym inside the verify dialog.
            stash = self._pending_cloud_verify.pop(context, None)
            self._add_homonym_unknown(context, (stash or {}).get("name", ""))

        elif action == "download_saves_no_local":
            # context = exe_path of known library game with no local backups
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                # Hand-added orphan archive: same accept action, restore from
                # local index (path + name already recorded) instead of cloud.
                if self._apply_orphan_backup_to_game(entry):
                    self._show_tracking_toast_if_playing(entry.id)
                    try:
                        self._backups_page._load_games()
                        self._backups_page._refresh_list()
                    except Exception:
                        pass
                    return
                orch = get_orchestrator()

                def _on_no_local_sync_done(game_id: str, result):
                    if game_id != entry.id:
                        return
                    try:
                        orch.sync_finished.disconnect(_on_no_local_sync_done)
                    except RuntimeError:
                        pass

                    def _restore_then_toast():
                        self._restore_after_cloud_download(entry.id)
                        self._show_tracking_toast_if_playing(entry.id)

                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(300, _restore_then_toast)

                orch.sync_finished.connect(_on_no_local_sync_done)
                orch.sync_game(
                    entry.id, entry.name, entry.save_paths,
                    direction="down", exe_path=entry.exe_path,
                    computed_folder_name=entry.computed_folder_name,
                    name_history=list(entry.name_history),
                )
        elif action == "continue_local_no_local":
            # Dropdown choice on the cloud notification: proceed WITHOUT
            # downloading provider saves — keep local ones. Resolves the
            # notification and hands over to the normal tracking toast
            # (parent → child notification order).
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                getattr(self, "_pending_orphan_adopt", {}).pop(entry.id, None)
                # Persist the "use local saves" choice so the download prompt is
                # not re-shown on every restart (reversible from Settings →
                # suppressed-games list).
                self._persist_cloud_no_local_decline(entry.id, entry.name)
                self._show_tracking_toast_if_playing(entry.id)

        elif action == "add_game_no_download":
            # Dropdown choice on the unknown-game cloud notification (State A
            # keep-local): add to library WITHOUT downloading provider saves.
            # One cloud folder here, so the game keeps the base name (assume it's
            # the same game). The user already answered the cloud question, so
            # the launch-flow check must not immediately re-ask it. force_local_wins:
            # the game keeps the base folder (shared with the cloud copy), so its
            # first sync must upload (local wins), not download the cloud version.
            self._pending_cloud_verify.pop(context, None)
            self._auto_add_game_from_overlay(context, force_local_wins=True)
            _added = get_library().get_by_exe(context)
            if _added:
                self._suppress_cloud_prompt_once.add(_added.id)
                # Persist so the just-added game does not re-prompt for cloud
                # download on the next launch (reversible from Settings).
                self._persist_cloud_no_local_decline(_added.id, _added.name)

        elif action == "suppress_cloud_no_local":
            # "Don't show again" — permanent per-game suppression
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                getattr(self, "_pending_orphan_adopt", {}).pop(entry.id, None)
                self._persist_cloud_no_local_decline(entry.id, entry.name)
                self._show_tracking_toast_if_playing(entry.id)

        elif action == "download_saves_different_machine":
            # context = exe_path. Cloud saves were last uploaded by another
            # machine; user chose to download & replace the local copy.
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                orch = get_orchestrator()

                def _on_diff_machine_sync_done(game_id: str, result):
                    if game_id != entry.id:
                        return
                    try:
                        orch.sync_finished.disconnect(_on_diff_machine_sync_done)
                    except RuntimeError:
                        pass

                    def _restore_then_toast():
                        self._restore_after_cloud_download(entry.id)
                        self._show_tracking_toast_if_playing(entry.id)

                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(300, _restore_then_toast)

                orch.sync_finished.connect(_on_diff_machine_sync_done)
                orch.sync_game(
                    entry.id, entry.name, entry.save_paths,
                    direction="down", exe_path=entry.exe_path,
                    computed_folder_name=entry.computed_folder_name,
                    name_history=list(entry.name_history),
                )
                self._mark_cloud_machine_confirmed(entry.id)

        elif action == "decline_cloud_different_machine":
            # "Keep Local" — don't re-prompt for this same cloud version again
            entry = get_library().get_by_exe(context)
            if entry:
                self._pending_cloud_notification.pop(entry.id, None)
                self._mark_cloud_machine_confirmed(entry.id)
                self._show_tracking_toast_if_playing(entry.id)

        elif action == "open_app":
            self._show_modal_app(from_user_action=True)
        elif action == "backup_all":
            ids = [g.id for g in get_library().all_games() if g.save_paths]
            self._start_backup_all(ids, source="overlay")
        elif action == "backup_current":
            # In-game overlay "Backup": only the game(s) currently running
            for g in get_monitor().currently_playing():
                if g.save_paths:
                    self._backup_game(g.id)
                else:
                    # No confirmed paths yet (first session of an overlay-added
                    # game): back up the live-detected paths as a TEMPORARY
                    # backup instead of silently doing nothing — same logic as
                    # the periodic in-game tick.
                    self._ingame_backup_tick(g.id)
        elif action == "open_auto_scan":
            # [i] on the manual overlay: open the save-path scan panel so
            # live-detected files can be reviewed/accepted mid-game.
            playing = get_monitor().currently_playing()
            gid = playing[0].id if playing else None
            pre_scanned = None
            if gid:
                with self._bg_scan_lock:
                    pending = list(self._pending_auto_scans.get(gid, []))
                pre_scanned = pending or None
            try:
                # Manual overlay open must NEVER auto-start a scan: it shows
                # the paths live tracking already found (pre_scanned), and if
                # there are none it opens EMPTY. Scanning — the normal scan or
                # the broader Extended one, which merges live-tracking results
                # with new finds — is only ever a conscious action the user
                # takes via the panel button, never something that fires just
                # because the panel was opened.
                dlg = show_auto_scan_dialog(self, pre_scanned, game_id=gid,
                                            user_initiated=True, auto_scan=False)
                if not dlg:
                    # Pending paths existed but were all excluded/already
                    # covered (nothing selectable to hand over) — the user
                    # explicitly asked for the panel, so it must still open,
                    # but EMPTY, with Extended Scan available as the opt-in.
                    dlg = show_auto_scan_dialog(self, None, game_id=gid,
                                                user_initiated=True, auto_scan=False)
                self._track_scan_dialog(dlg)
                # Same in-game backdrop as "open app" from the overlay.
                self._show_blur_for_dialog(dlg)
            except Exception as e:
                logger.error(f"Open auto-scan from overlay failed: {e}")
        elif action == "quick_restore":
            # context = backup_id
            self._quick_restore_from_overlay(context)

    def _quick_restore_from_overlay(self, backup_id: str):
        """Execute a quick restore in a background thread to avoid UI freeze."""
        import threading
        def _do_restore():
            from core.backup import get_backup_manager
            mgr = get_backup_manager()
            result = mgr.restore_backup(backup_id)
            game_name = ""
            bk = mgr.get_backup(backup_id)
            if bk:
                # Remember what WE put there: landing on that exact state is
                # the intended outcome, and must not read as a regression.
                # Only here — the game id comes from the backup, not from the
                # overlay context, which carries just the backup id.
                self._last_restored[bk.game_id] = backup_id
                entry = get_library().get_by_id(bk.game_id)
                game_name = entry.name if entry else ""
            # Thread-safe append with lock
            with self._restore_lock:
                self._quick_restore_results.append((result.success, game_name))
            from PySide6.QtCore import QMetaObject, Qt
            try:
                QMetaObject.invokeMethod(
                    self, "_on_quick_restore_done",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                logger.debug("Quick restore: MainWindow destroyed before result delivery")

        threading.Thread(target=_do_restore, daemon=True).start()

    @Slot()
    def _on_quick_restore_done(self):
        with self._restore_lock:
            if not self._quick_restore_results:
                return
            try:
                success, game_name = self._quick_restore_results.popleft()
            except IndexError:
                return
        if self._overlay:
            self._overlay.show_restore_result(success, game_name)

    def _show_modal_app(self, from_user_action: bool = False):
        """Show SaveSync app with modal blur background — overlay style, never steals focus.
        
        Should only be called when the user explicitly requests it (open_app button).
        from_user_action guards against accidental calls from focus events.
        """
        if not from_user_action:
            logger.debug("_show_modal_app: ignoring call not from user action")
            return
        try:
            if self._is_modal_mode:
                self._hide_modal_app()

            self._is_modal_mode = True

            # Save game foreground before we touch anything
            _saved_fg = None
            if platform.system() == "Windows":
                try:
                    import ctypes
                    _saved_fg = ctypes.windll.user32.GetForegroundWindow()
                except Exception:
                    pass

            # Show blur modal background
            if self._blur_modal:
                self._blur_modal.show_animated()

            # Make main window visible without activating (overlay-like)
            base_flags = self._original_window_flags if self._original_window_flags is not None else self.windowFlags()
            new_flags = base_flags | Qt.WindowType.WindowStaysOnTopHint
            self.setWindowFlags(new_flags)

            if self.isMinimized():
                self.showNormal()
            elif not self.isVisible():
                self.show()
            else:
                self.show()  # re-apply flags

            # Force topmost without stealing focus (same as overlay)
            self._ensure_modal_topmost()

            # Restore game foreground if we stole it
            if _saved_fg and platform.system() == "Windows":
                try:
                    import ctypes
                    current = ctypes.windll.user32.GetForegroundWindow()
                    our_hwnd = int(self.winId())
                    if current == our_hwnd and _saved_fg != our_hwnd:
                        ctypes.windll.user32.SetForegroundWindow(_saved_fg)
                except Exception:
                    pass

            logger.info("SaveSync shown in modal mode (overlay-like)")

        except Exception as e:
            logger.error(f"Failed to show modal app: {e}")
            self.show_and_raise()

    def _hide_modal_app(self):
        """Hide modal blur background and exit modal mode.
        Guard against re-entry, skip show()/hide() flash."""
        if not self._is_modal_mode:
            return  # already exited modal mode
        try:
            self._is_modal_mode = False

            # Force-hide blur immediately (no async animation that can linger)
            if self._blur_modal:
                self._blur_modal.force_hide()

            # Restore original window flags — defer application to next show
            restore_flags = self._original_window_flags if self._original_window_flags is not None else (self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
            self.setWindowFlags(restore_flags)

            # Just hide — don't show()+hide() which causes flash
            self.hide()

            logger.info("SaveSync modal mode ended - minimized to tray")

        except Exception as e:
            logger.error(f"Failed to hide modal app: {e}")

    def _ensure_modal_topmost(self):
        """Ensure the main window stays on top in modal mode."""
        if not self._is_modal_mode:
            return
            
        try:
            import platform
            if platform.system() == "Windows":
                import ctypes

                # Get window handle
                hwnd = int(self.winId())

                # Set window flags
                user32 = ctypes.windll.user32
                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOACTIVATE = 0x0010
                HWND_TOPMOST = -1
                
                # Apply topmost
                user32.SetWindowPos(
                    hwnd, HWND_TOPMOST,
                    0, 0, 0, 0,
                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                )
        except Exception as e:
            logger.debug(f"Failed to ensure modal topmost: {e}")

    def _show_blur_for_dialog(self, dlg):
        """Back a game-friendly dialog with the same blur vignette used when
        the app itself is opened in-game, and tear it down with the dialog.

        Used by overlay actions that open a standalone dialog over a running
        game (e.g. the [i] save-path scan panel) instead of the full modal app.
        """
        if dlg is None or not self._blur_modal:
            return
        self._blur_dialog = dlg
        self._blur_modal.show_animated()
        # Both the blur and the dialog live in the topmost band — re-raise
        # the dialog so it sits above the vignette, not under it.
        dlg.raise_()
        dlg.destroyed.connect(lambda *_, d=dlg: self._on_blur_dialog_gone(d))

    def _on_blur_dialog_gone(self, dlg=None):
        # Only the dialog that currently owns the backdrop may tear it down —
        # opening the panel twice re-points _blur_dialog, and the first
        # dialog's destroy must not kill the blur under the second one.
        if dlg is not None and self._blur_dialog is not None \
                and dlg is not self._blur_dialog:
            return
        self._blur_dialog = None
        # Leave the backdrop alone if the full modal app owns it.
        if self._blur_modal and not self._is_modal_mode:
            self._blur_modal.force_hide()

    def _on_blur_background_clicked(self):
        """Handle clicks on the blur modal background."""
        # Outside modal mode the blur may be backing a standalone dialog:
        # clicking the vignette dismisses it (its destroyed hook hides the
        # blur), mirroring how the same click closes the modal app.
        dlg = self._blur_dialog
        if dlg is not None and not self._is_modal_mode:
            try:
                dlg.close()
            except RuntimeError:
                self._on_blur_dialog_gone()
            return
        # Close modal when background is clicked
        self._hide_modal_app()  # This now minimizes to tray

    def _on_focus_changed(self, focused_object):
        """Handle focus changes to maintain modal behavior.
        Check if ANY app window is active before hiding.
        Guarded against re-entrancy to prevent signal loops from
        SetForegroundWindow re-emitting the focus change."""
        if not self._is_modal_mode:
            return
        # Only react if the main window is actually visible (i.e. open_app was
        # clicked and the app was brought to foreground).  If the window is
        # hidden we are NOT in a visible modal session, so ignore focus events.
        if not self.isVisible():
            return
        if getattr(self, '_handling_focus_change', False):
            return
        self._handling_focus_change = True
        try:
            # Check if focus moved away from our app
            if focused_object is None:
                # Focus lost - check if it's because user switched applications
                import platform
                if platform.system() == "Windows":
                    try:
                        from PySide6.QtWidgets import QApplication
                        if QApplication.activeWindow() is not None:
                            return
                        import ctypes
                        user32 = ctypes.windll.user32
                        foreground_hwnd = user32.GetForegroundWindow()
                        our_hwnd = int(self.winId())

                        # Also exclude the blur overlay window itself — it is a
                        # Tool/FramelessWindowHint window that can briefly become
                        # foreground during show/hide transitions.
                        blur_hwnd = 0
                        try:
                            if self._blur_modal and self._blur_modal.isVisible():
                                blur_hwnd = int(self._blur_modal.winId())
                        except (RuntimeError, Exception):
                            pass

                        if foreground_hwnd in (our_hwnd, blur_hwnd):
                            return  # still our window/overlay — do nothing

                        if foreground_hwnd != our_hwnd:
                            # User switched to another application.
                            def _safe_hide():
                                try:
                                    if not self.isVisible():
                                        return
                                    self._hide_modal_app()
                                except RuntimeError:
                                    pass
                            QTimer.singleShot(100, _safe_hide)
                    except Exception:
                        pass
        finally:
            self._handling_focus_change = False

    def schedule_launcher_exe_resolve(self, game_id: str, url: str,
                                      game_name: str = "",
                                      shortcut_dir: str = "",
                                      timeout: int = 30):
        """Find the real exe for a launcher URL on a worker thread.

        The fuzzy disk walk often takes many seconds — never run it on the
        GUI thread. When an exe is found, ``launcher_exe_resolved`` updates
        the library entry (exe_path + keep URL in appid for launching).
        """
        if not game_id or not url:
            return
        import threading
        import time
        from pathlib import Path
        from core.resolvers import (
            find_executable_by_fuzzy_name, get_appid_from_url,
            parse_launcher_url, _get_suggested_exe_search_paths,
            _get_default_exe_search_paths, _get_launcher_install_paths,
        )

        def _bg(gid=game_id, launch_url=url, name=game_name, sdir=shortcut_dir):
            exe_found = ""
            try:
                if not parse_launcher_url(launch_url):
                    self.launcher_exe_resolved.emit(gid, launch_url, "")
                    return
                deadline = time.monotonic() + timeout
                appid = get_appid_from_url(launch_url) or ""
                paths = list(_get_suggested_exe_search_paths())
                if sdir:
                    sd = Path(sdir)
                    if sd.exists() and sd not in paths:
                        paths.insert(0, sd)
                # Prefer launcher install roots, then the wide default set.
                try:
                    for _dirs in _get_launcher_install_paths().values():
                        for d in _dirs:
                            p = Path(d)
                            if p.exists() and p not in paths:
                                paths.append(p)
                except Exception:
                    pass
                try:
                    for d in _get_default_exe_search_paths():
                        p = Path(d)
                        if p.exists() and p not in paths:
                            paths.append(p)
                except Exception:
                    pass
                if name and time.monotonic() < deadline:
                    hit = find_executable_by_fuzzy_name(
                        name, paths, deadline=deadline)
                    if hit:
                        exe_found = str(hit)
                if not exe_found and appid and time.monotonic() < deadline:
                    hit = find_executable_by_fuzzy_name(
                        appid, paths, deadline=deadline)
                    if hit:
                        exe_found = str(hit)
            except Exception as e:
                logger.debug(f"Background launcher exe resolve failed: {e}")
            self.launcher_exe_resolved.emit(gid, launch_url, exe_found)

        threading.Thread(target=_bg, daemon=True).start()

    @Slot(str, str, str)
    def _on_launcher_exe_resolved(self, game_id: str, url: str, exe_path: str):
        """Apply a background URL→exe hit to the library entry."""
        if not exe_path:
            return
        from core.resolvers import is_launcher_url
        from core.library import get_library
        lib = get_library()
        entry = lib.get_by_id(game_id)
        if entry is None:
            return
        # Do not clobber a path the user already set by hand.
        cur = (entry.exe_path or "").strip()
        if cur and not is_launcher_url(cur) and Path(cur).exists():
            return
        entry.exe_path = exe_path
        # Launch URL stays in appid — never move it into exe_path.
        if url:
            entry.appid = url
        try:
            entry.record_exe_hints(exe_path)
        except Exception:
            pass
        lib.update_game(entry)
        logger.info(f"Resolved launcher URL for '{entry.name}' → {exe_path}")
        try:
            get_monitor().start_tracking(entry, exe_path)
        except Exception:
            pass
        try:
            self._library_page.refresh_game_status(game_id)
        except Exception:
            pass

    def _auto_add_game_from_overlay(self, exe_path: str, force_folder_name: str = "",
                                    force_local_wins: bool = False):
        """Automatically add a game to library from overlay detection.

        *force_folder_name*, when given, pins the game's sync/backup folder (used
        by the cloud-verify flow: adopt a specific cloud folder on "download", or
        take a cloud-unique one on "homonym"). add_game keeps it as long as it's
        free locally.

        *force_local_wins* marks the game so its NEXT sync uploads (local wins)
        instead of a plain "auto" sync that could download a newer cloud copy —
        set for the "keep local saves" choices."""
        from core.library import get_library, GameEntry
        from core.monitor import get_monitor, get_pending_appid
        from core.resolvers import get_appid_from_url, is_launcher_url

        # Launcher URL: keep it in appid for launching; leave exe_path empty
        # until the background fuzzy search finds the real file. Never store
        # steam://… (etc.) as exe_path.
        original_url = exe_path if exe_path and is_launcher_url(exe_path) else None
        appid = None
        game_name = None
        if original_url:
            appid = original_url
            game_name = get_pending_appid(original_url)
            exe_path = ""

        # Extract game name from executable — prefer detected name from
        # overlay; a queued (no longer running) detection falls back to the
        # name recorded in the unknown-game history.
        from core.save_detector import derive_display_name as _derive_name
        pending_key = original_url or exe_path
        pending_name = self._pending_unknown.get(pending_key) if pending_key else None
        if not pending_name and pending_key:
            pending_name = next(
                (h.get("name") for h in get_config().get("unknown_game_history", [])
                 if isinstance(h, dict) and h.get("exe") == pending_key and h.get("name")),
                None)
        if original_url:
            name = game_name or pending_name or get_appid_from_url(original_url) or "Game"
        else:
            name = game_name or pending_name or _derive_name(exe_path)

        # Try to get appid from pending (detected from parent process)
        if not appid:
            appid = get_pending_appid(exe_path) if exe_path else None

        # Create new game entry
        entry = GameEntry(
            name=name,
            exe_path=exe_path,
            save_paths=[],  # Will be detected via auto-scan on exit
            requires_confirmation=True,  # Will trigger auto-scan dialog on exit
            save_paths_confirmed=False,
            last_backed_up=None,
            last_synced=None,
            playtime_seconds=0,
            machine_id=None,
            suppressed_overlay=False,
            appid=appid,
        )

        # The names as found on disk — release folder and executable stem —
        # kept for matching, never for display.
        if exe_path:
            entry.record_exe_hints(exe_path)

        if appid:
            logger.info(f"Auto-adding game with launcher appid: {appid}")

        if force_folder_name:
            # Pin the sync/backup folder (cloud-verify flow). add_game still
            # runs its local-uniqueness check, but won't change a locally-free
            # name — so an adopted cloud folder (or a cloud-unique homonym one)
            # is preserved.
            entry.computed_folder_name = force_folder_name

        # Persist the local-wins flag BEFORE add_game so it's stored before the
        # monitor starts tracking (no window where a sync could fire without it).
        entry.pending_local_wins = force_local_wins

        # Add to library
        get_library().add_game(entry)

        # Start tracking only when we already know a real process path.
        monitor = get_monitor()
        if exe_path:
            monitor.start_tracking(entry, exe_path)

        # Remove from pending unknown list
        if pending_key:
            self._pending_unknown.pop(pending_key, None)
        if original_url:
            self._pending_unknown.pop(original_url, None)

        logger.info(f"Auto-added game to library: {name}")

        if original_url:
            self.schedule_launcher_exe_resolve(
                entry.id, original_url, game_name=name or "")

        # Show only the "added" confirmation here. The tracking notification
        # is deliberately NOT chained from this overlay any more: adding the
        # game triggers the monitor's game_launched flow (via refresh_tracked,
        # guarded by the parent/child ancestor check), which runs the cloud-
        # saves check FIRST and only then shows the single tracking toast —
        # chaining a second one from here is what produced the duplicate.
        if self._overlay:
            from core.engines.game_engine import engine_display, engine_for_game
            self._overlay.show_game_added(
                name, exe_path or original_url or "", then_track=False,
                engine=engine_display(engine_for_game(entry)))

    def _on_suppress_overlay(self, exe_path: str):
        config = get_config()
        suppressed = config.get("suppressed_overlay_apps", [])
        if exe_path not in suppressed:
            suppressed.append(exe_path)
            config.set("suppressed_overlay_apps", suppressed)
        # Also remove from pending so post-exit dialog never fires
        self._pending_unknown.pop(exe_path, None)
        self._session_shown_exes.discard(exe_path)
        # Drop it from the persisted unknown-game queue too: a suppressed
        # app must stop counting on the overlay badge and disappear from
        # the browsable queue.
        config.set("unknown_game_history",
                   [h for h in config.get("unknown_game_history", [])
                    if not (isinstance(h, dict) and h.get("exe") == exe_path)])
        if self._overlay:
            self._overlay.refresh_unknown_badge()

    # ── Hotkeys ───────────────────────────────────────────────────────────────

    def _setup_hotkeys(self):
        """Bind the one hotkey this app has, replacing whatever was bound.

        unregister_all first: the manager keys bindings by hotkey string, so
        a config replaced wholesale (reset, import, snapshot restore) would
        otherwise leave the PREVIOUS combination listening alongside the new
        one — a shortcut the user had changed away from kept working.
        """
        mgr = get_hotkey_manager()
        mgr.unregister_all()
        hotkey = get_config().get("overlay_hotkey", "alt+ctrl+s")
        mgr.register(hotkey, self._toggle_overlay)

    def _toggle_overlay(self):
        """Toggle overlay. If a currently-playing game has an unresolved
        cloud-saves notification (shown but not yet answered), re-summon
        exactly that first. Otherwise: if an unknown game is running, show
        add-to-library prompt. If a known game tracking popup is shown,
        close it and open manual overlay.
        """
        if not self._overlay:
            return

        # Ahead of everything else: an unacknowledged warning that something
        # is putting older saves back. It stays re-summonable for as long as
        # the game is running and the player has not acknowledged it — a
        # warning about saves being overwritten right now outranks a question
        # about where to sync from.
        if self._pending_regression:
            muted = get_config().get("suppressed_ingame_notifs", {})
            for gid in {g.id for g in get_monitor().currently_playing()}:
                pending = self._pending_regression.get(gid)
                # Muted from the notification itself while it was on screen:
                # the pending record predates that choice, so drop it here
                # rather than letting the hotkey resurrect a muted warning.
                if pending and "regression" in muted.get(gid, []):
                    self._pending_regression.pop(gid, None)
                    continue
                entry = get_library().get_by_id(gid) if pending else None
                if entry is None:
                    continue
                backup_id, after_restore = pending
                self._overlay.show_save_reverted(entry.name, gid,
                                                 backup_id, after_restore)
                return

        # Next: an unresolved cloud notification for a game
        # that's currently running. "Unresolved" means the user hasn't yet
        # clicked any of that notification's own buttons (download,
        # decline, or don't-show-again — see the action handlers in
        # _on_overlay_action, which pop this dict entry) — it stays
        # pending indefinitely otherwise, so the hotkey can keep bringing
        # it back for as long as it takes the player to notice and decide.
        if self._pending_cloud_notification:
            playing_ids = {g.id for g in get_monitor().currently_playing()}
            for gid in playing_ids:
                notif_type = self._pending_cloud_notification.get(gid)
                if not notif_type:
                    continue
                entry = get_library().get_by_id(gid)
                if not entry:
                    continue
                if notif_type == "no_local":
                    self._overlay.show_cloud_saves_no_local(entry.name, entry.exe_path)
                elif notif_type == "different_machine":
                    self._overlay.show_cloud_saves_different_machine(entry.name, entry.exe_path)
                elif notif_type == "sync_prompt":
                    self._overlay.show_cloud_saves(entry.name, entry.exe_path)
                elif notif_type in ("conflict_diverged", "conflict_unreconciled"):
                    self._overlay.show_cloud_conflict_resolve(
                        entry.name, entry.exe_path,
                        diverged=(notif_type == "conflict_diverged"))
                else:
                    continue
                return

        # Next: an unanswered "is this process really that game?" prompt. Same
        # rule as the cloud prompts above — a decision-required question stays
        # re-summonable until it is actually answered, otherwise dismissing it
        # once would leave the game untracked for the rest of the session with
        # no way to bring the question back.
        if self._pending_unverified:
            (proc_name, game_id), game_name = next(iter(self._pending_unverified.items()))
            self._overlay.show_unverified_match(game_name, proc_name, game_id)
            return

        # Check if overlay is currently showing a tracking popup for a known game.
        # NOTE: use the module-level get_library — a function-local re-import
        # here would shadow it for the WHOLE method and crash the earlier use
        # in the pending-cloud branch above (UnboundLocalError).
        if self._overlay.isVisible() and hasattr(self._overlay, '_context_exe') and self._overlay._context_exe:
            known_game = get_library().get_by_exe(self._overlay._context_exe)
            
            if known_game:
                # Known-game tracking popup: close it, then open the manual
                # overlay once the hide has started.
                self._overlay.hide_animated()
                self._overlay._auto_hide_timer.stop()
                self._overlay._pending_auto_hide = 0
                QTimer.singleShot(150, lambda: self._show_manual_forced())
                return
        
        # Check if any currently-running process is an unknown game. Prefer the
        # one actually in the FOREGROUND (the game the user is looking at) over
        # an arbitrary dict-order entry — otherwise a stale unknown-game
        # notification that merely arrived later would hijack the prompt.
        # This path stays alive even when show_overlay_on_unknown is off: that
        # setting silences the live popup and the history queue, not the
        # deliberate "add what I'm looking at" hotkey.
        config = get_config()
        running_unknown = [
            (name, exe) for exe, name in self._pending_unknown.items()
            if exe not in config.get("suppressed_overlay_apps", [])
        ]
        if running_unknown and not get_monitor().currently_playing():
            name, exe = self._pick_foreground_unknown(running_unknown)
            self._overlay.show_game_detected(name, exe)
            return

        # Out of game with PENDING unknown-game detections: the same hotkey
        # serves the queue first — but ONLY while the unknown-process feature
        # is on. With it off, dredging up every silenced detection would be
        # the opposite of what the user asked for.
        from ui.unknown_history import pending_unknown_count
        if (config.get("show_overlay_on_unknown", True)
                and not get_monitor().currently_playing()
                and pending_unknown_count() > 0):
            self._overlay.show_unknown_queue()
            return

        # Default behavior: show manual overlay
        stats = self._overview_page.get_stats_for_overlay()
        self._overlay.show_manual(stats)

    def _pick_foreground_unknown(self, candidates):
        """From the running unknown games, pick the one in the actual foreground
        (what the user is looking at) so the overlay's add prompt targets the
        game in execution — not a later, stale unknown-game notification. Falls
        back to the most recently detected unknown when the foreground can't be
        resolved."""
        fg = self._foreground_exe_path()
        if fg:
            try:
                fg_res = Path(fg).resolve()
                for name, exe in candidates:
                    try:
                        if Path(exe).resolve() == fg_res:
                            return name, exe
                    except (OSError, ValueError):
                        continue
            except (OSError, ValueError):
                pass
        return candidates[-1]   # fallback: most recently detected unknown

    def _foreground_exe_path(self) -> str:
        """Executable path of the current foreground window's process (Windows),
        or '' when unavailable."""
        if platform.system() != "Windows":
            return ""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""
            import psutil
            return psutil.Process(int(pid.value)).exe() or ""
        except Exception:
            return ""

    def _show_manual_forced(self):
        """Show manual overlay without toggle logic (used after hiding other overlay)."""
        stats = self._overview_page.get_stats_for_overlay()
        # Force show by setting opacity to 0 first to bypass toggle check
        self._overlay.setWindowOpacity(0.0)
        # Ensure timer is reset before showing
        self._overlay._auto_hide_timer.stop()
        self._overlay._pending_auto_hide = 0
        self._overlay.show_manual(stats)

    def _update_hotkey(self, old: str, new: str):
        get_hotkey_manager().update_hotkey(old, new, self._toggle_overlay)

    def _on_theme_changed(self, theme: str):
        """Re-apply inline palette() styles in place after a theme switch (see
        _on_theme_changed_inner). Re-entrancy-guarded."""
        if getattr(self, '_rebuilding_pages', False):
            return
        self._rebuilding_pages = True
        try:
            self._on_theme_changed_inner(theme)
        finally:
            self._rebuilding_pages = False

    def _on_theme_changed_inner(self, theme: str):
        """Re-apply inline (palette-dependent) styles IN PLACE across all pages
        instead of rebuilding their widget trees — so switching light/dark no
        longer freezes on large libraries (the old rebuild recreated one card
        per game). The global QSS re-polish is done by ThemeManager.apply; this
        covers the inline styles the QSS can't reach. Each page exposes
        refresh_styles() (ui.styles.theme.ThemedMixin) which cascades into its
        cards. SettingsPage keeps its own in-place _refresh_styles path and is
        intentionally not in this list.

        Only the page on screen is refreshed now. The others are flagged and
        catch up in _switch_page, on the way in: over 90% of the app's widgets
        belong to pages nobody is looking at, and restyling them all was the
        larger half of what made the switch pause.

        Sidebar chrome (Credits, nav polish, machine id) is ALWAYS refreshed
        here — it used to live inside _refresh_page_styles, which never runs
        when the theme is changed from Settings (that page is not in the
        list below), so Credits kept the previous theme until the next tab
        switch.
        """
        pages = [self._overview_page, self._library_page,
                 self._sync_page, self._backups_page]
        if self._cheats_page is not None:
            pages.append(self._cheats_page)
        for page in pages:
            if not callable(getattr(page, "refresh_styles", None)):
                continue
            if page is not self._stack.currentWidget():
                page._styles_stale = True
                continue
            self._refresh_page_styles(page)
        # Always — independent of which stack page is visible.
        self._refresh_sidebar_chrome()

    def _refresh_page_styles(self, page):
        """Run a page's style cascade and clear its stale flag."""
        page._styles_stale = False
        try:
            page.refresh_styles()
        except Exception:
            logger.debug("Page refresh_styles failed", exc_info=True)

    def _refresh_sidebar_chrome(self):
        """Restyle sidebar pieces that use inline palette() or need a re-polish.

        Called on every theme switch (and not deferred to the next tab change).
        Also refreshes DPI/accessibility width when UI scale changes.
        """
        from ui.helpers import scaled
        if getattr(self, "_sidebar", None) is not None:
            self._sidebar.setFixedWidth(scaled(220, self, min_px=210))

        self._update_sidebar_status()

        for btn in self._nav_buttons:
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        for btn in (self._cheats_nav_btn,):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        for entry in self._shelved_add_entries:
            btn = entry.get("btn")
            if btn is not None:
                btn.style().unpolish(btn)
                btn.style().polish(btn)
                btn.update()
        for notice in (
            getattr(self, "_backup_batch_notice", None),
            getattr(self, "_sync_batch_notice", None),
            getattr(self, "_search_batch_notice", None),
        ):
            if notice is not None:
                try:
                    notice.refresh_styles()
                except RuntimeError:
                    pass
        self._style_credits_btn()
        # Force Qt to drop any cached :hover painting from the previous theme.
        self._credits_btn.style().unpolish(self._credits_btn)
        self._credits_btn.style().polish(self._credits_btn)
        self._credits_btn.update()
        if self._overlay:
            self._overlay.refresh_styles()

    # ── Save-state regression ────────────────────────────────────────────────

    def _check_save_regression(self, game_id: str, after_restore: bool = False):
        """Has something put an older save state back for this game?

        Runs at game launch and again just after a restore — the two moments
        a launcher's own cloud sync gets a chance to overwrite what is on
        disk. Never on a timer: the check is per-game and event-driven on
        purpose, so a hundred games in the library cost nothing.

        Concurrent calls for the same game (e.g. false-positive exit then
        auto re-launch while the first scan is still on disk) coalesce: one
        worker runs, later intents are rearmed and replayed on the GUI thread
        when it finishes — same idiom as watcher.py / backups_page download.
        """
        entry = get_library().get_by_id(game_id)
        if entry is None or not entry.save_paths:
            return
        if not after_restore:
            self._regression_warned.discard(game_id)   # a launch is a new episode
            self._pending_regression.pop(game_id, None)
        if game_id in self._regression_warned:
            return

        with self._regression_check_lock:
            if game_id in self._regression_checking:
                # Keep the latest after_restore intent; do not drop a genuine
                # new-session reset that arrived while a prior scan was flying.
                self._regression_rearm[game_id] = after_restore
                return
            self._regression_checking.add(game_id)

        from core.backup import get_backup_manager

        def _run():
            try:
                mgr = get_backup_manager()
                expected = self._last_restored.get(game_id, "")
                older = mgr.detect_regression(game_id, list(entry.save_paths),
                                              expected_backup_id=expected)
                if older is not None:
                    backups = mgr.get_backups_for_game(game_id)
                    newest = backups[0].backup_id if backups else ""
                    self.save_regression_found.emit(game_id, newest, after_restore)
            except Exception as e:
                logger.debug(f"Save regression check failed for {game_id}: {e}")
            finally:
                with self._regression_check_lock:
                    self._regression_checking.discard(game_id)
                    rearm = self._regression_rearm.pop(game_id, None)
                if rearm is not None:
                    # Marshal back to the GUI thread (same as
                    # backups_page._download_then_restore).
                    QTimer.singleShot(
                        0,
                        lambda gid=game_id, ar=rearm:
                            self._check_save_regression(gid, ar))

        threading.Thread(target=_run, daemon=True).start()

    @Slot(str, str, bool)
    def _on_save_regression(self, game_id: str, newest_backup_id: str,
                            after_restore: bool):
        entry = get_library().get_by_id(game_id)
        if entry is None or self._overlay is None:
            return
        if game_id in self._regression_warned:
            return
        # "Don't show again" is per game, like every other in-game notification.
        # Checked here rather than in the overlay so a muted game never even
        # becomes a pending warning the hotkey could bring back.
        if "regression" in get_config().get(
                "suppressed_ingame_notifs", {}).get(game_id, []):
            return
        self._regression_warned.add(game_id)
        self._pending_regression[game_id] = (newest_backup_id, after_restore)
        logger.warning(f"Save state for '{entry.name}' regressed"
                       + (" right after a restore" if after_restore else ""))
        self._overlay.show_save_reverted(entry.name, game_id,
                                         newest_backup_id, after_restore)

    def _restore_after_regression(self, game_id: str, backup_id: str, freeze: bool):
        """Put the newest backup back, optionally with the game frozen.

        Freezing is what wins the race against a launcher that re-syncs the
        moment it sees the files change: the game cannot write (nor can its
        in-process sync), so the files stay as they were written.
        """
        if not backup_id:
            return
        from core.backup import get_backup_manager
        import threading

        pid = self._find_game_pid(game_id) if freeze else 0
        self._regression_warned.discard(game_id)
        self._status_bar.showMessage(t("backup.restoring"), 0)

        def _run():
            try:
                get_backup_manager().restore_backup(backup_id, freeze_pid=pid,
                                                    lib_game_id=game_id)
                self._last_restored[game_id] = backup_id
            except Exception as e:
                logger.error(f"Forced restore failed for {game_id}: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ── Periodic backup integrity check ──────────────────────────────────────

    # Deliberately late and slow: the check is a safety net, not something the
    # user is waiting on. Starting it minutes after launch keeps it clear of
    # the startup burst, and re-checking the due date hourly means a machine
    # left running for days still runs it on time without a long-lived timer
    # that has to survive suspend.
    _VERIFY_FIRST_DELAY_MS = 5 * 60 * 1000
    _VERIFY_RECHECK_MS = 60 * 60 * 1000

    def _setup_backup_verify(self):
        self._verify_timer = QTimer(self)
        self._verify_timer.setInterval(self._VERIFY_RECHECK_MS)
        self._verify_timer.timeout.connect(self._maybe_run_backup_verify)
        self._verify_timer.start()
        QTimer.singleShot(self._VERIFY_FIRST_DELAY_MS, self._maybe_run_backup_verify)
        self._verify_thread = None
        # The save editor's own copies age out on their own schedule. Doing it
        # here, once, is what makes "delete after N days" true for a save
        # nobody has opened since — the editor itself only ever sees the files
        # somebody goes back to.
        QTimer.singleShot(self._VERIFY_FIRST_DELAY_MS, self._prune_save_edit_copies)

    def _setup_startup_self_checks(self):
        """In-app regression guards (no shipped ``tests/`` suite).

        Same delay pattern as backup verify: a few minutes after launch, off
        the GUI thread; failures use status bar + tray.
        """
        self.self_check_failed.connect(self._on_self_check_failed)
        self.self_check_progress.connect(self._on_self_check_progress)
        self.self_check_done.connect(self._on_self_check_done)
        QTimer.singleShot(self._VERIFY_FIRST_DELAY_MS, self._run_startup_self_checks)

    def _run_startup_self_checks(self):
        # Only run self-checks if enabled in settings
        if not get_config().get("self_checks", True):
            logger.info("Self-checks disabled in settings, skipping")
            return
        
        # Check if configuration history exists before running self-checks.
        # The real checkpoint history lives under CONFIG_HISTORY_DIR — NOT a
        # "config_dir/history" folder (that path never exists, so this gate
        # silently skipped the checks even when snapshots were present).
        from core.constants import CONFIG_HISTORY_DIR
        if not CONFIG_HISTORY_DIR.exists() or not any(CONFIG_HISTORY_DIR.iterdir()):
            logger.info("No configuration history found, skipping self-checks")
            return
        
        # Check if enough time has passed since last check based on frequency
        frequency_days = get_config().get("self_checks_frequency", 7)
        last_check = get_config().get("last_self_check", 0)
        current_time = int(time.time())
        seconds_per_day = 86400
        min_seconds = frequency_days * seconds_per_day
        
        if current_time - last_check < min_seconds:
            days_until_next = (min_seconds - (current_time - last_check)) // seconds_per_day
            logger.info(f"Self-checks already run recently, next check in {days_until_next} days")
            return
            
        # Run self-checks and update last check time
        from core.self_checks import run_startup_self_checks

        # Surface the sweep in the sidebar like Backup/Sync Tutti. The
        # callbacks run on the worker thread — the Qt signals marshal them
        # back to the GUI thread.
        self._verify_batch_notice.begin(t("batch.verify_label"), 2)

        def _progress(check_id: str, index: int, total: int):
            self.self_check_progress.emit(check_id, index, total)

        def _fail(check_id: str, detail: str):
            self.self_check_failed.emit(check_id, detail)

        def _on_complete():
            # Update last check time on successful completion
            try:
                get_config().set("last_self_check", int(time.time()))
                logger.info("Self-checks completed, updated last check time")
            except Exception as e:
                logger.warning(f"Failed to update last self-check time: {e}")
            self.self_check_done.emit()

        # Run checks and record completion once the worker thread is done.
        run_startup_self_checks(on_failure=_fail, on_done=_on_complete,
                                on_progress=_progress)

    @Slot(str, int, int)
    def _on_self_check_progress(self, check_id: str, index: int, total: int):
        names = {
            "config_history_restore": t("batch.verify_snapshot"),
            "backup_index": t("batch.verify_index"),
        }
        self._verify_batch_notice.update_progress(
            index, max(total, 1), names.get(check_id, check_id))

    @Slot()
    def _on_self_check_done(self):
        self._verify_batch_notice.finish(t("batch.verify_done"), hide_after_ms=4000)

    @Slot(int)
    def _on_verify_batch_started(self, total: int):
        self._verify_batch_notice.begin(t("batch.verify_label"), total)

    @Slot(int, int, str)
    def _on_verify_batch_progress(self, done: int, total: int, name: str):
        self._verify_batch_notice.update_progress(done, total, name)

    @Slot(str)
    def _on_verify_batch_finished(self, msg: str):
        self._verify_batch_notice.finish(msg, hide_after_ms=4000)
        from ui.helpers import trim_process_memory
        QTimer.singleShot(400, trim_process_memory)

    @Slot(str, str)
    def _on_self_check_failed(self, check_id: str, detail: str):
        msg = t("self_checks.failed", check=check_id)
        if detail:
            msg = f"{msg} ({detail[:80]})"
        self._status_bar.showMessage(msg, 20000)
        try:
            if self._tray is not None:
                self._tray.showMessage(
                    t("app.name"),
                    t("self_checks.failed", check=check_id),
                    self._tray.icon(), 10000)
        except Exception:
            logger.debug("Could not raise a tray notice for self-check",
                         exc_info=True)

    def _setup_auto_export_config(self):
        """Schedule encrypted config uploads to the sync provider."""
        self._auto_export_timer = QTimer(self)
        self._auto_export_timer.setInterval(self._VERIFY_RECHECK_MS)
        self._auto_export_timer.timeout.connect(self._maybe_run_auto_export_config)
        self._auto_export_timer.start()
        QTimer.singleShot(self._VERIFY_FIRST_DELAY_MS,
                          self._maybe_run_auto_export_config)
        self._auto_export_thread = None

    def _maybe_run_auto_export_config(self):
        """Upload config to the sync provider if enabled and due."""
        cfg = get_config()
        if not cfg.get("auto_export_config_enabled", False):
            return
        if self._auto_export_thread is not None and self._auto_export_thread.is_alive():
            return
        if get_monitor().currently_playing():
            logger.debug("Auto config export postponed — a game is running")
            return

        days = max(1, int(cfg.get("auto_export_config_interval_days", 7)))
        last = cfg.get("auto_export_config_last", "") or ""
        if last:
            try:
                from datetime import datetime, timedelta
                if datetime.utcnow() - datetime.fromisoformat(last) < timedelta(days=days):
                    return
            except (ValueError, TypeError):
                pass

        import threading
        from datetime import datetime

        def _run():
            try:
                from sync import get_orchestrator
                orch = get_orchestrator()
                if not orch.is_online() or not orch.provider:
                    logger.debug("Auto config export postponed — provider offline")
                    return
                from core.config_transfer import (
                    upload_config_to_cloud, save_config_snapshot,
                )
                result = upload_config_to_cloud(
                    orch.provider, skip_if_unchanged=True)
                if result is True:
                    save_config_snapshot("auto_upload")
                    get_config().set(
                        "auto_export_config_last",
                        datetime.utcnow().isoformat())
                    logger.info("Scheduled config export uploaded to cloud")
                elif result is None:
                    get_config().set(
                        "auto_export_config_last",
                        datetime.utcnow().isoformat())
                    logger.info("Scheduled config export skipped — unchanged")
                else:
                    logger.warning("Scheduled config export failed")
            except Exception as e:
                logger.debug(f"Scheduled config export error: {e}")

        self._auto_export_thread = threading.Thread(target=_run, daemon=True)
        self._auto_export_thread.start()

    # ── GitHub Releases check ─────────────────────────────────────────────────

    # Same late/hourly pattern as backup verify. The interval itself lives in
    # config with jitter (~12 h ± 2 h); this timer only asks "is it due?".
    _UPDATE_RECHECK_MS = 60 * 60 * 1000

    def _setup_update_check(self):
        self._update_check_thread = None
        self._update_dialog_open = False
        self.update_available.connect(self._on_update_available)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(self._UPDATE_RECHECK_MS)
        self._update_timer.timeout.connect(self._maybe_check_for_updates)
        self._update_timer.start()
        from core.update_check import first_delay_ms
        QTimer.singleShot(first_delay_ms(), self._maybe_check_for_updates)

    def _maybe_check_for_updates(self):
        """Poll GitHub Releases when enabled and the jittered interval is due."""
        from core.update_check import (
            fetch_latest_release, is_check_due, mark_check_attempted,
            should_notify,
        )
        cfg = get_config()
        if not cfg.get("check_for_updates", True):
            return
        if self._update_check_thread is not None and self._update_check_thread.is_alive():
            return
        if not is_check_due(cfg):
            return
        if self._update_dialog_open:
            return

        import threading

        def _run():
            try:
                info = fetch_latest_release()
                mark_check_attempted(get_config())
                if info is None:
                    return
                if should_notify(info, get_config()):
                    self.update_available.emit(info)
            except Exception as e:
                logger.debug(f"Update check failed: {e}")
                try:
                    mark_check_attempted(get_config())
                except Exception:
                    pass

        self._update_check_thread = threading.Thread(target=_run, daemon=True)
        self._update_check_thread.start()

    @Slot(object)
    def _on_update_available(self, info):
        if self._update_dialog_open or info is None:
            return
        from core.update_check import mark_notified
        from ui.dialogs.update_dialog import UpdateAvailableDialog

        self._update_dialog_open = True
        try:
            dlg = UpdateAvailableDialog(info, self)
            dlg.exec()
            mark_notified(get_config(), info.version)
        except Exception as e:
            logger.debug(f"Update dialog failed: {e}")
        finally:
            self._update_dialog_open = False

    @staticmethod
    def _prune_save_edit_copies():
        try:
            from core.save_editor import prune_all
            prune_all()
        except Exception as e:              # never let housekeeping stop startup
            logger.debug(f"Could not clear old save-editor copies: {e}")

    def _maybe_run_backup_verify(self):
        """Run the integrity sweep if it is enabled and due.

        Skipped while a game is running: the check is pure reading, but it
        competes for disk with the in-game backups, and nothing is lost by
        waiting an hour.
        """
        cfg = get_config()
        if not cfg.get("backup_verify_enabled", True):
            return
        if self._verify_thread is not None and self._verify_thread.is_alive():
            return
        if get_monitor().currently_playing():
            logger.debug("Backup verify postponed — a game is running")
            return

        days = max(1, int(cfg.get("backup_verify_interval_days", 7)))
        last = cfg.get("backup_verify_last", "") or ""
        if last:
            try:
                from datetime import datetime, timedelta
                if datetime.utcnow() - datetime.fromisoformat(last) < timedelta(days=days):
                    return
            except (ValueError, TypeError):
                pass        # unreadable stamp — treat as never run

        import threading
        from datetime import datetime

        def _run():
            from core.backup import get_backup_manager
            mgr = get_backup_manager()
            ids = [b.backup_id for b in mgr.get_all_backups()]
            if not ids:
                return
            try:
                # Throttled + batched index writes; skip archives still "ok"
                # within the last half-day so a weekly schedule does not
                # re-read every zip on a library of hundreds.
                results = mgr.verify_backups(ids, deep=False)
            except Exception as e:
                logger.debug(f"Scheduled verify failed: {e}")
                return
            bad = sum(1 for state, _ in results.values() if state != "ok")
            get_config().set("backup_verify_last", datetime.utcnow().isoformat())
            logger.info(
                f"Scheduled backup verification: {len(results) - bad}/{len(results)} intact"
            )
            if bad:
                self.backup_verify_problems.emit(bad, len(results))

        self._verify_thread = threading.Thread(target=_run, daemon=True)
        self._verify_thread.start()

    @Slot(int, int)
    def _on_backup_verify_problems(self, bad: int, total: int):
        """Surface a failed sweep where the user will see it."""
        msg = t("backups.verify_result_bad", bad=bad, total=total)
        self._status_bar.showMessage(msg, 15000)
        try:
            if self._tray is not None:
                self._tray.showMessage(t("app.name"), msg,
                                       self._tray.icon(), 10000)
        except Exception:
            logger.debug("Could not raise a tray notice for the verify result",
                         exc_info=True)

    @Slot(str)
    def _on_index_validation_failed(self, detail: str):
        """Startup index check could not finish — same surface as verify problems."""
        msg = t("backups.index_validate_failed")
        if detail:
            msg = f"{msg} ({detail[:80]})"
        self._status_bar.showMessage(msg, 20000)
        try:
            if self._tray is not None:
                self._tray.showMessage(
                    t("app.name"),
                    t("backups.index_validate_failed"),
                    self._tray.icon(), 10000)
        except Exception:
            logger.debug("Could not raise a tray notice for index validation",
                         exc_info=True)

    @Slot()
    def _on_index_validation_recovered(self):
        msg = t("backups.index_validate_recovered")
        self._status_bar.showMessage(msg, 8000)

    # ── Monitor ───────────────────────────────────────────────────────────────

    def _setup_monitors(self):
        monitor = get_monitor()
        monitor.game_launched.connect(self._on_game_launched)
        monitor.game_exited.connect(self._on_game_exited)
        monitor.unknown_game_detected.connect(self._on_unknown_game)
        monitor.unknown_game_exited.connect(self._on_unknown_game_exited)
        monitor.game_match_unverified.connect(self._on_unverified_match)
        monitor.game_match_unverified_gone.connect(self._on_unverified_match_gone)
        monitor.start()

        # A hand-registered destination waiting for its game is picked up the
        # moment that game appears — no periodic re-check, just this one
        # event, matched on the name coincidence.
        get_library().game_added.connect(self._on_library_game_added_refresh)

        from core.watcher import get_save_watcher
        self._watcher = get_save_watcher()
        self._watcher.save_changed.connect(self._on_save_changed)
        self._watcher.folder_appeared.connect(self._on_folder_appeared)
        self._watcher.start()

        lib = get_library()
        # Watcher is started lazily: watch_game is called in _on_game_launched
        # and unwatch_game in _on_game_exited, so no paths are watched at startup.
        lib.game_added.connect(lambda _: get_monitor().refresh_tracked())
        lib.bulk_finished.connect(lambda: get_monitor().refresh_tracked())
        self._last_known_paths: dict[str, list] = {
            e.id: list(e.save_paths) for e in lib.all_games()
        }
        def _on_game_updated(e):
            old_paths = self._last_known_paths.get(e.id)
            if old_paths != e.save_paths:
                self._last_known_paths[e.id] = list(e.save_paths)
                self._watcher.update_game_paths(e.id, e.save_paths)
        lib.game_updated.connect(_on_game_updated)

        def _on_game_removed(gid: str):
            self._watcher.unwatch_game(gid)
            self._last_known_paths.pop(gid, None)
            # Clear seen_exes for this game so overlay re-fires if re-added
            mon = get_monitor()
            tracked = mon.get_tracked_snapshot()
            for key, entry in tracked.items():
                if entry is not None and entry.id == gid and entry.exe_path:
                    mon.clear_seen_exe(entry.exe_path)
            # Clear session suppression too
            self._session_shown_exes = {
                exe for exe in self._session_shown_exes
                if exe not in get_config().get("suppressed_overlay_apps", [])
            }

        lib.game_removed.connect(_on_game_removed)

    def _on_save_changed(self, game_id: str):
        """Save file changed (watchdog). Two independent jobs:

        1) DISCOVERY (always, regardless of the during-game backup toggle):
           the watchdog is the only thing that reliably sees atomic save
           writes — the 60 s open-file poll misses them. Bridge the folders it
           just found into _pending_auto_scans so they get PROPOSED at the
           confirmation panel, and push them live into an already-open panel.
           This is what makes "I saved but nothing was proposed" work, and it
           surfaces the real save dir (e.g. game/saves) with nothing hardcoded.
        2) BACKUP (only when during-game backup is enabled): provisional
           backup when there is no confirmed path, else the confirmed-path one.
        """
        entry = get_library().get_by_id(game_id)
        if not entry:
            return

        # 1) Discovery — surface actually-modified folders for confirmation.
        try:
            from core.watcher import get_pending_save_paths
            exe_dir = ""
            if entry.exe_path:
                try:
                    exe_dir = str(Path(entry.exe_path).parent)
                except Exception:
                    exe_dir = ""
            # Temporal correlation happens INSIDE the watcher now: events
            # rejected by the common-root name filter get buffered and
            # claimed when an attributed save lands in the same instant, so
            # they arrive here through get_pending_save_paths like any
            # other discovery.
            discovered = get_pending_save_paths(game_id, exe_dir)
            if discovered:
                # Identity, not string equality: the watcher reports a folder
                # with its on-disk casing while the open-file scan reports it
                # as the game opened it, and both spellings used to be kept
                # as two separate pending paths.
                from core.save_detector import path_identity as _pid
                added: list[str] = []
                with self._bg_scan_lock:
                    merged = list(self._pending_auto_scans.get(game_id, []))
                    known = {_pid(p) for p in merged}
                    for p in discovered:
                        if _pid(p) not in known:
                            known.add(_pid(p))
                            merged.append(p)
                            added.append(p)
                    self._pending_auto_scans[game_id] = merged
                if added:
                    for p in added:
                        logger.info(
                            f"Live tracking (watchdog) surfaced save path "
                            f"for {entry.name}: {p}")
                    # Live-update an already-open confirmation panel.
                    self._push_paths_to_open_scan_dialog(game_id, added)
                    # Kick the provisional-backup timer for a not-yet-confirmed
                    # game the first time live tracking surfaces something: the
                    # timer is only started at launch, when nothing was
                    # discovered yet, so without this the partial backups never
                    # begin. Guarded on "not already running" so repeated save
                    # events don't keep resetting the interval (which would
                    # starve the backup). Internally gated by auto_backup_enabled.
                    if not entry.save_paths and game_id not in self._ingame_backup_timers:
                        self._start_ingame_backup_timer(entry)
        except Exception as e:
            logger.debug(f"Watchdog discovery bridge failed for {game_id}: {e}")

        # 2) Backup — unchanged gating (during-game backup toggle).
        if not get_config().get("backup_during_game", False):
            return
        if not entry.save_paths:
            # No confirmed path yet — exclusively the standalone provisional
            # mechanism's job (see _backup_provisional_paths), which has its
            # own auto_backup_enabled check internally.
            self._backup_provisional_paths(game_id, silent=True)
            return
        if entry.auto_backup_enabled:
            self._backup_game(game_id, silent=True)  # automatic — no toast spam

    def _on_game_launched(self, entry: GameEntry, exe_path: str):
        """Known game started.

        Ordering (per explicit product requirement): the cloud-saves check
        must resolve — download & restore, explicit decline, or dismiss —
        *before* the tracking notification/timers start, not concurrently
        with them. Previously both fired off two independently-scheduled
        timers racing for the same overlay widget; now cloud-check runs
        first and its own completion (in _on_cloud_check_result) is what
        triggers everything else via _start_tracking_after_cloud_check().
        """
        if not entry:
            return

        # Bring back the notes and images that were pinned for THIS game.
        try:
            from ui.widgets.pins import get_pin_manager
            get_pin_manager().restore_open(entry.id)
        except Exception as e:
            logger.debug(f"Could not restore this game's pins: {e}")

        # Launching is the one moment a game is GUARANTEED to have its
        # executable. Orphan hand-added archives (and cloud saves) are offered
        # through _check_cloud_on_launch — same notification, no silent adopt.
        self._update_sidebar_status()

        # Minimise SaveSync to the tray for the duration of play (restored on
        # exit by _restore_from_tray_after_game).
        self._hide_to_tray_for_game()

        # Reset per-session dialog guard when game starts (allows dialog on next exit)
        if hasattr(self, '_scan_dialog_shown_this_session'):
            self._scan_dialog_shown_this_session.discard(entry.id)

        game_id = entry.id
        import weakref
        weak_self = weakref.ref(self)

        def _run_cloud_check_then_track(gid=game_id, exe=exe_path):
            s = weak_self()
            if s is None:
                return
            s._check_cloud_on_launch(
                gid,
                on_resolved=lambda show_toast=True: s._start_tracking_after_cloud_check(gid, exe, show_toast=show_toast),
            )

        # A short delay lets the process fully settle before we touch its
        # open files / hit the network — not tied to any overlay timer.
        QTimer.singleShot(400, _run_cloud_check_then_track)

    def _start_tracking_after_cloud_check(self, game_id: str, exe_path: str, show_toast: bool = True):
        """Second half of game-launch handling: watcher, in-game backup
        timer, live-tracking scan, and (conditionally) the "now tracking"
        overlay toast. Runs once the cloud-saves check has resolved for
        this game — see _on_game_launched / _check_cloud_on_launch.

        show_toast=False when _on_cloud_check_result is about to show (or
        just showed) a cloud notification for this same game: both calls
        target the same single overlay widget, and show_animated() cancels
        whatever animation is already in flight and restarts fresh on every
        call — so firing the plain "tracking" toast immediately before a
        cloud prompt, in the same synchronous call, doesn't queue politely,
        it visibly flickers (the toast's own fade-in gets cancelled and
        replaced within milliseconds by the cloud prompt's fade-in). The
        cloud prompt already conveys "we see this game running" on its own,
        so the generic toast is simply skipped rather than raced.
        """
        entry = get_library().get_by_id(game_id)
        if entry is None:
            return

        # Did anything put an older save state back while we weren't looking?
        self._check_save_regression(game_id)

        # Start watching save paths for this specific game now that it's running.
        # We also watch common save roots for games without configured paths so
        # that filesystem events can discover them (e.g. Godot AppData paths).
        self._watcher.watch_game(entry.id, entry.save_paths, game_name=entry.name)

        # Always start in-game backup timer (uses confirmed paths or pre-scanned)
        self._start_ingame_backup_timer(entry)

        # Always start live-tracking background scan so newly created saves
        # are picked up even when the game already has confirmed paths.
        # The scan is lightweight (open-file poll) and only runs while playing.
        if get_config().get("auto_scan_on_exit", True):
            self._start_live_tracking_loop(entry)

        if show_toast and self._overlay and get_config().get("show_overlay_on_launch", True):
            from core.engines.game_engine import engine_display, engine_for_game
            self._overlay.show_game_launched(
                entry.name, exe_path,
                engine=engine_display(engine_for_game(entry)))

    def _start_live_tracking_loop(self, entry: GameEntry):
        """Poll open files every 60 s while game is running.

        Detected paths are immediately fed into:
        - _pending_auto_scans  (shown at game exit for confirmation)
        - backup timer          (temporary backups while playing)
        """
        game_id = entry.id

        # Cancel any existing loop for this game
        existing = self._live_tracking_timers.pop(game_id, None)
        if existing:
            existing.stop()
            existing.deleteLater()

        def _poll():
            # Stop polling once game exits
            playing_ids = {e.id for e in get_monitor().currently_playing()}
            if game_id not in playing_ids:
                timer = self._live_tracking_timers.pop(game_id, None)
                if timer:
                    timer.stop()
                    timer.deleteLater()
                return

            # Find current PID for this game.
            # Prefer find_game_process (handles launcher→child scenarios)
            # to reach the real game process that has save files open.
            _monitor = get_monitor()
            pid = _monitor.find_game_process(game_id, entry.exe_path or "")

            if not pid:
                # Fallback: tracked snapshot (bridge exe still running)
                tracked = _monitor.get_tracked_snapshot()
                for key, te in tracked.items():
                    if te and te.id == game_id:
                        pid = key[0]
                        break

            if not pid:
                return

            import threading
            game_name = entry.name
            exe_path  = entry.exe_path
            appid     = entry.appid
            _lock     = self._bg_scan_lock

            def _scan():
                try:
                    from core.save_detector import detect_save_paths
                    # Known paths feed the temporal-correlation stage: a
                    # container folder written in the same instant as one of
                    # these is claimed even without any name linkage.
                    with _lock:
                        _known = list(self._pending_auto_scans.get(game_id, []))
                    _entry_now = get_library().get_by_id(game_id)
                    if _entry_now is not None:
                        _known = list(_entry_now.save_paths or []) + _known
                    paths = detect_save_paths(
                        game_name=game_name,
                        exe_path=exe_path,
                        pid=pid,
                        appid=appid,
                        live_only=True,
                        correlate_paths=_known or None,
                    )
                    if not paths:
                        return

                    # Merge newly found paths into pending (identity-based, so
                    # a differently-cased spelling of a folder the watcher
                    # already surfaced is not added a second time)
                    from core.save_detector import path_identity as _pid
                    with _lock:
                        existing_pending = self._pending_auto_scans.get(game_id, [])
                        merged = list(existing_pending)
                        known = {_pid(p) for p in merged}
                        for p in paths:
                            if _pid(p) not in known:
                                known.add(_pid(p))
                                merged.append(p)
                                logger.info(f"Live tracking found new path for {game_name}: {p}")
                        self._pending_auto_scans[game_id] = merged

                    # Re-start backup timer if it has no paths yet
                    from PySide6.QtCore import QMetaObject, Qt as _Qt
                    try:
                        QMetaObject.invokeMethod(self, "_on_live_paths_detected",
                                                 _Qt.ConnectionType.QueuedConnection)
                    except RuntimeError:
                        pass
                except Exception as e:
                    logger.debug(f"Live tracking poll failed for {game_name}: {e}")

            threading.Thread(target=_scan, daemon=True).start()

        timer = QTimer(self)
        timer.setInterval(60_000)   # poll every 60 s
        timer.timeout.connect(_poll)
        timer.start()
        self._live_tracking_timers[game_id] = timer

        # Also fire immediately (after a short delay so the process settles)
        QTimer.singleShot(3000, _poll)

    def _start_ingame_backup_timer(self, entry: GameEntry):
        """Start a repeating timer to backup saves every N seconds while playing.

        Uses the per-game auto_backup_enabled flag and backup_interval_sec.
        The first tick is deferred by the remaining interval since last backup.
        """
        if not entry:
            return

        self._stop_ingame_backup_timer(entry.id)
        # Per-game flag controls in-game periodic backup (no separate global toggle needed)
        if not entry.auto_backup_enabled:
            return

        backup_paths = entry.save_paths
        if not backup_paths:
            # Unconfirmed paths → temporary backups (see _ingame_backup_tick);
            # not for suppressed games, whose detections are discarded at exit.
            if get_config().get("scan_auto_accept_games", {}).get(entry.id):
                return
            pre_scanned = getattr(self, '_pending_auto_scans', {}).get(entry.id, [])
            backup_paths = pre_scanned

        if not backup_paths:
            return

        interval_ms = max(30, entry.backup_interval_sec) * 1000

        # Calculate how long ago the last backup was
        first_delay_ms = interval_ms
        if entry.last_backed_up:
            try:
                from datetime import timezone
                from dateutil.parser import parse as _parse
                age_s = (
                    __import__('datetime').datetime.now(timezone.utc)
                    - _parse(entry.last_backed_up)
                ).total_seconds()
                remaining_s = entry.backup_interval_sec - age_s
                if remaining_s > 5:
                    # Wait out the remaining interval before the first backup
                    first_delay_ms = int(remaining_s * 1000)
                    logger.debug(
                        f"In-game backup timer: first tick in {remaining_s:.0f}s "
                        f"(last backup was {age_s:.0f}s ago) for {entry.name}"
                    )
                else:
                    first_delay_ms = interval_ms
            except Exception:
                pass

        timer = QTimer(self)
        timer.setSingleShot(False)
        timer.setInterval(interval_ms)
        timer.timeout.connect(lambda gid=entry.id: self._ingame_backup_tick(gid))
        # Use a one-shot timer for the first delayed tick, then switch to repeating
        if first_delay_ms != interval_ms:
            timer.setSingleShot(True)
            def _first_tick(gid=entry.id, t=timer, iv=interval_ms):
                self._ingame_backup_tick(gid)
                t.setSingleShot(False)
                t.setInterval(iv)
                t.start()
            timer.timeout.disconnect()
            timer.timeout.connect(_first_tick)
        timer.start(first_delay_ms if first_delay_ms != interval_ms else interval_ms)
        self._ingame_backup_timers[entry.id] = timer
        logger.debug(
            f"In-game backup timer started for {entry.name} "
            f"(interval={entry.backup_interval_sec}s, first_tick_in={first_delay_ms//1000}s)"
        )

    def _stop_ingame_backup_timer(self, game_id: str):
        timer = self._ingame_backup_timers.pop(game_id, None)
        if timer:
            timer.stop()
            timer.deleteLater()

    def _ingame_backup_tick(self, game_id: str):
        """Periodic backup tick — runs backup in a background thread to avoid GUI freeze."""
        # If game is no longer tracked as playing, cancel the timer
        playing_ids = {e.id for e in get_monitor().currently_playing()}
        if game_id not in playing_ids:
            self._stop_ingame_backup_timer(game_id)
            return
        entry = get_library().get_by_id(game_id)
        if not entry:
            self._stop_ingame_backup_timer(game_id)
            return

        if not entry.save_paths:
            # No confirmed path yet for this game — exclusively the
            # provisional mechanism's job (see _backup_provisional_paths),
            # standalone and never merged with the normal backup pipeline
            # below. The timer keeps running either way (a path may still
            # get confirmed mid-session); nothing more to do on this tick.
            self._backup_provisional_paths(game_id, silent=True)
            return

        backup_paths = entry.save_paths
        if not backup_paths:
            self._stop_ingame_backup_timer(game_id)
            return

        # Run backup in a thread to avoid blocking the GUI.
        # Re-fetch the entry inside the thread to avoid racing with
        # concurrent updates (e.g. save path changes on the GUI thread).
        import threading
        _game_id = game_id
        _name = entry.name
        _paths = list(backup_paths)
        _excluded = list(entry.excluded_save_paths or [])
        _exe = entry.exe_path
        _cfn = entry.computed_folder_name
        _name_history = list(entry.name_history) if entry.name_history else []
        _note = t('main.auto_in_game')

        def _do_backup():
            max_mb = get_config().get("max_backup_size_mb", 512)
            backup, created = get_backup_manager().create_backup(
                _game_id, _name, _paths,
                exe_path=_exe,
                note=_note, max_size_mb=max_mb, force=False,
                computed_folder_name=_cfn,
                name_history=_name_history,
                excluded_paths=_excluded,
                return_status=True,
            )
            if created:
                from datetime import datetime, timezone as _tz
                get_library().update_game_fields(
                    _game_id,
                    last_backed_up=datetime.now(_tz.utc).isoformat(),
                    machine_id=get_machine_id(),
                )
                # Signal the GUI thread to show the in-game backup notification
                from PySide6.QtCore import QMetaObject, Qt as _Qt
                try:
                    QMetaObject.invokeMethod(
                        self, "_show_ingame_backup_notif",
                        _Qt.ConnectionType.QueuedConnection,
                    )
                except RuntimeError:
                    pass

        t_backup = threading.Thread(target=_do_backup, daemon=True)
        t_backup.start()




    @Slot()
    def _show_ingame_backup_notif(self):
        """Show backup notification for the currently playing game.
        Called on the main thread from _ingame_backup_tick's background thread.
        """
        if not self._overlay:
            return
        playing = get_monitor().currently_playing()
        if not playing:
            return
        entry = playing[0]
        if get_config().get("show_overlay_on_backup", True):
            self._overlay.show_backup_done(entry.name, game_id=entry.id)

    @Slot()
    def _show_ingame_provisional_backup_notif(self):
        """Show the TEMPORARY-backup notification for the currently playing
        game. Independently silenceable from the normal one (see
        Overlay.show_provisional_backup_done — its own notif_type means the
        per-game "don't show again" link only suppresses this variant).
        Called on the main thread from _backup_provisional_paths's
        background thread.
        """
        playing = get_monitor().currently_playing()
        if not playing:
            return
        entry = playing[0]
        # A provisional backup now exists for this game: flip its library card
        # badge from "no saves" to "provisional" immediately, without waiting
        # for a full rebuild or a page change.
        try:
            self._library_page.refresh_game_status(entry.id)
        except Exception:
            pass
        if self._overlay and get_config().get("show_overlay_on_backup", True):
            self._overlay.show_provisional_backup_done(entry.name, game_id=entry.id)

    @Slot()
    def _on_live_paths_detected(self):
        """Called on the main thread after a live-tracking scan merged new
        paths into _pending_auto_scans.

        Re-starts the in-game backup timer only for games that are *currently
        playing* and still have no confirmed paths — the timer was started at
        launch, when nothing was discovered yet, so without this kick the
        provisional backups would never begin for poll-discovered paths
        (the watchdog bridge does the same kick for its own discoveries).
        """
        from core.monitor import get_monitor as _gm
        playing_ids = {e.id for e in _gm().currently_playing()}
        with self._bg_scan_lock:
            pending_ids = [gid for gid, paths in self._pending_auto_scans.items()
                           if paths]
        for game_id in pending_ids:
            # Only act on currently-playing games — results for a game that
            # already exited must not bleed into a later session.
            if game_id not in playing_ids:
                continue
            entry = get_library().get_by_id(game_id)
            if not entry:
                continue
            if entry.save_paths:
                continue  # already has confirmed paths — timer running
            if game_id not in self._ingame_backup_timers:
                # _start_ingame_backup_timer reads _pending_auto_scans itself
                self._start_ingame_backup_timer(entry)
                logger.info(
                    f"Re-started backup timer for {entry.name} "
                    "after live path detection"
                )

    def _on_game_exited(self, entry: GameEntry):
        # Guard FIRST: the monitor can emit false positives when a poll briefly
        # misses the process. Teardown (watcher, in-game backup timer, live
        # tracking, pins) must not run until we confirm the process is gone —
        # otherwise a blip leaves those down for poll_interval + ~6s until
        # game_launched re-arms them, with pinned notes flickering off/on.
        if entry.exe_path:
            exe_name = Path(entry.exe_path).name
            still_running = False
            try:
                import psutil
                for proc in psutil.process_iter(('pid', 'name', 'exe')):
                    try:
                        pinfo = proc.info
                        if pinfo.get('exe'):
                            if Path(pinfo['exe']).resolve() == Path(entry.exe_path).resolve():
                                still_running = True
                                break
                        elif pinfo.get('name', '').lower() == exe_name.lower():
                            if proc.is_running():
                                still_running = True
                                break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception:
                pass  # fallback: treat as genuine exit
            if still_running:
                logger.debug(
                    f"_on_game_exited: {entry.name!r} process still running — "
                    "ignoring false-positive exit (no teardown)"
                )
                return

        self._update_sidebar_status()
        self._stop_ingame_backup_timer(entry.id)
        # Stop watching save paths for this game — no need to watch when not running
        self._watcher.unwatch_game(entry.id)

        # Stop live tracking polling loop
        lt = self._live_tracking_timers.pop(entry.id, None)
        if lt:
            lt.stop()
            lt.deleteLater()

        # A note started during the session and never saved anywhere has
        # nothing to be restored from, so it goes with the session. The saved
        # ones come off the screen too, but are remembered against this game.
        try:
            from ui.widgets.pins import get_pin_manager
            _pins = get_pin_manager()
            _pins.discard_unsaved()
            _pins.stow_game(entry.id)
        except Exception as e:
            logger.debug(f"Could not put this game's pins away: {e}")

        # If the app is still in modal/blur mode when the game closes, dismiss it
        if self._is_modal_mode:
            self._hide_modal_app()

        # Re-fetch entry from library to get current save_paths (may have been updated during gameplay)
        entry = get_library().get_by_id(entry.id) or entry

        # Genuine exit — bring the window back from the tray (unless another
        # game is still playing).
        self._restore_from_tray_after_game(entry.id)

        # Release any priority prompt tied to this game's exe so a now-closed
        # game can't wedge later notifications.
        if self._overlay and entry.exe_path:
            self._overlay.clear_priority_for(entry.exe_path)

        # Backup on exit (if enabled and game has save paths).
        # We do NOT force the backup: if nothing changed since the last
        # backup the hash check in create_backup will skip it silently,
        # preventing a redundant sync+notification when the game is reopened.
        config = get_config()
        if config.get("backup_on_exit", True) and entry.auto_backup_enabled and entry.save_paths:
            logger.info(f"Game exited — checking for exit backup for {entry.name}")
            self._backup_game(entry.id, force_full=False)
        # Trigger auto scan confirmation for this specific game if needed
        self._check_auto_scan_for_game(entry)
        from ui.helpers import trim_process_memory
        QTimer.singleShot(1000, trim_process_memory)

    def _on_unknown_game_exited(self, exe_path: str):
        """An unregistered process exited. We track and scan ONLY games the user
        added to the library, so a non-library process closing must NOT open an
        add / auto-scan dialog — prompting on the exit of every arbitrary app
        (e.g. Notepad) would flood the user. The start-time unknown-game overlay
        notification is the sole touchpoint for adding an unknown game; here we
        only release the detection guards so a relaunch can re-notify."""
        # Release any priority prompt / detection guards tied to this exe: a
        # prompt whose subject just closed must not wedge detection by deferring
        # later notifications, and clearing the per-session/monitor guards lets
        # a relaunch re-fire the unknown-game notification.
        if self._overlay:
            self._overlay.clear_priority_for(exe_path)
        self._session_shown_exes.discard(exe_path)
        self._overlay_shown_exes.discard(exe_path)
        try:
            get_monitor().clear_seen_exe(exe_path)
        except Exception:
            pass
        self._pending_unknown.pop(exe_path, None)

    def _on_unverified_match(self, entry, proc_name: str):
        """A process matched *entry* by name only (its path was unreadable).

        The monitor has deliberately NOT started tracking it. Ask the user
        whether it really is that game — the alternative, guessing, can file
        one game's saves under another. The answer is persisted by the
        monitor, so this appears once per executable name.
        """
        if not entry or not self._overlay:
            return
        # Unresolved until the user answers — kept here so the hotkey can
        # re-summon it, exactly like an unanswered cloud prompt. Without this
        # a dismissed prompt is unrecoverable: the monitor won't re-ask this
        # session and the game stays untracked with no way back.
        self._pending_unverified[(proc_name, entry.id)] = entry.name
        self._overlay.show_unverified_match(entry.name, proc_name, entry.id)

    def _on_unverified_match_gone(self, proc_name: str, game_id: str):
        """The process exited before the user answered — take the prompt down
        rather than leave a question about something that no longer runs."""
        self._pending_unverified.pop((proc_name, game_id), None)
        if self._overlay:
            self._overlay.dismiss_unverified_match(proc_name, game_id)

    def _on_unknown_game(self, name: str, exe_path: str):
        """Unknown process started: check for cloud saves first; fall back to add-to-library."""
        if get_library().get_by_exe(exe_path) is not None:
            return
        config = get_config()
        if exe_path in config.get("suppressed_overlay_apps", []):
            return
        # Session map first: even with the live popup off, the hotkey must
        # still be able to offer "add this" for the unknown process in the
        # foreground. The persisted queue is a separate concern.
        self._pending_unknown[exe_path] = name
        if not config.get("show_overlay_on_unknown", True):
            return
        # History + badge only while the feature is on — with it off the
        # hotkey must not resurface a queue of silenced detections.
        from ui.unknown_history import record_unknown_game
        record_unknown_game(name, exe_path)
        if self._overlay and self._overlay.isVisible():
            self._overlay.refresh_unknown_badge()
        if exe_path in self._session_shown_exes:
            return
        self._session_shown_exes.add(exe_path)
        self._overlay_shown_exes.add(exe_path)

        # If provider is connected, check in background for matching cloud saves
        from sync import get_orchestrator
        if get_orchestrator().is_online():
            self._check_provider_for_unknown_game(name, exe_path)
        else:
            if self._overlay:
                self._overlay.show_game_detected(name, exe_path)


    def _check_provider_for_unknown_game(self, name: str, exe_path: str):
        """Background: look for cloud saves matching this unknown game's predicted folder."""
        import threading

        def _bg():
            cloud_meta = None   # None → no cloud saves; {"folders": [...]} otherwise
            try:
                from sync import get_orchestrator
                from core.constants import get_folder_name_for_save
                orch = get_orchestrator()
                if orch.is_online():
                    provider = orch.provider
                    if provider:
                        folder = get_folder_name_for_save(name, exe_path, "")
                        # If a DIFFERENT game already in the library occupies this
                        # name-derived folder, cloud saves found there are THAT
                        # game's: this unknown game (different exe) gets a numeric
                        # suffix on add, so its own cloud folder is empty — don't
                        # mis-attribute the other game's saves to it.
                        from core.library import get_library
                        if not get_library().folder_name_in_use_by_other(folder, ""):
                            # Every cloud folder sharing this base name (Alpha,
                            # Alpha_2, …). 1 → normal prompt; 2+ → a real conflict.
                            cloud_folders = orch.cloud_name_folders(folder)
                            if not cloud_folders and provider.list_cloud_backups(folder):
                                cloud_folders = [folder]   # enumeration unsupported; base has saves
                            candidates = []
                            for cf in cloud_folders:
                                reg_name, reg_path = self._cloud_folder_registration(provider, cf)
                                candidates.append(
                                    {"folder": cf, "name": reg_name or name, "path": reg_path}
                                )
                            if candidates:
                                cloud_meta = {"folders": candidates}
            except Exception:
                pass
            with self._cloud_found_lock:
                self._pending_cloud_found.append((name, exe_path, cloud_meta))
            from PySide6.QtCore import QMetaObject, Qt
            try:
                QMetaObject.invokeMethod(
                    self, "_process_cloud_found_unknown",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                pass

        threading.Thread(target=_bg, daemon=True).start()



    def _add_and_download_unknown(self, exe_path: str, force_folder_name: str = ""):
        """Add an unknown game then pull its cloud saves (add → sync down →
        restore latest). *force_folder_name* pins the folder to download from
        when the user picked a specific cloud copy."""
        self._auto_add_game_from_overlay(exe_path, force_folder_name=force_folder_name)
        # The user just answered the cloud question, so the launch-flow cloud
        # check that fires right after the add must not re-prompt.
        _added = get_library().get_by_exe(exe_path)
        if _added:
            self._suppress_cloud_prompt_once.add(_added.id)

        def _deferred_sync_and_restore(exe=exe_path):
            entry = get_library().get_by_exe(exe)
            if not entry:
                return
            orch = get_orchestrator()

            def _on_sync_done(game_id: str, result):
                if game_id != entry.id:
                    return
                try:
                    orch.sync_finished.disconnect(_on_sync_done)
                except RuntimeError:
                    pass
                from PySide6.QtCore import QTimer
                QTimer.singleShot(300, lambda: self._restore_after_cloud_download(entry.id))

            orch.sync_finished.connect(_on_sync_done)
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                direction="down", exe_path=entry.exe_path,
                computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history),
            )

        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, _deferred_sync_and_restore)

    def _claim_unique_cloud_folder(self, entry) -> bool:
        """Move an ALREADY-ADDED game onto its own cloud-unique folder.

        The library counterpart of _add_homonym_unknown: the cloud folder this
        game's title resolves to turns out to hold a same-titled DIFFERENT
        game (uploaded from another machine), so syncing into it would mix two
        games' saves. The game keeps its saves and history and simply stops
        aiming at that folder.

        The old folder is deliberately NOT recorded in folder_history: that
        history means "a folder that used to be mine", and the whole point
        here is that it never was — the migration paths would otherwise walk
        back into the other game's folder later.

        Returns True when the folder actually changed.
        """
        from sync import get_orchestrator
        from core.constants import get_folder_name_for_save
        from core.backup import get_backup_manager
        base = entry.computed_folder_name or get_folder_name_for_save(
            entry.name, entry.exe_path, entry.id)
        unique = get_orchestrator().cloud_unique_folder(base, exclude_id=entry.id)
        if not unique or unique == base:
            logger.info(f"{entry.name!r}: no same-name cloud folder to step "
                        f"aside from ({base!r} is already unique)")
            return False
        # Local backups move first: the entry must never point at a folder
        # whose zips are still elsewhere.
        get_backup_manager().relocate_game_backups(entry.id, base, unique)
        updated = get_library().update_game_fields(
            entry.id, computed_folder_name=unique) or entry
        # One-shot, not the permanent per-game mute: only the next launch check
        # needs covering (it can run before the upload below lands and lifts
        # local_only). Silencing the game forever would also hide a later,
        # legitimate "cloud has saves, you have none" for its own folder.
        self._suppress_cloud_prompt_once.add(updated.id)
        logger.info(f"{updated.name!r} declared a homonym of the cloud folder "
                    f"{base!r} — now syncing to {unique!r}")
        # Upload into the (empty) new folder so the game leaves local_only and
        # is no longer asked to reconcile with a folder that isn't its own.
        get_orchestrator().sync_game(
            updated.id, updated.name, updated.save_paths,
            exe_path=updated.exe_path, direction="up",
            computed_folder_name=unique,
            name_history=list(updated.name_history),
        )
        return True

    def _add_homonym_unknown(self, exe_path: str, detected_name: str):
        """Add a same-name-but-different game with its OWN cloud-unique folder
        (unique vs local library AND cloud), so its future sync creates a
        distinct folder instead of contaminating the other game's — and don't
        download anything. Used by the homonym choice and by keep-local in a
        known-conflict (State B) prompt."""
        from sync import get_orchestrator
        from core.constants import get_folder_name_for_save
        base = get_folder_name_for_save(detected_name, exe_path, "")
        unique = get_orchestrator().cloud_unique_folder(base)
        # force_local_wins is belt-and-suspenders: the unique folder is empty on
        # the cloud, so "up" just uploads — but if anything ever landed there,
        # the user's local still wins rather than being overwritten.
        self._auto_add_game_from_overlay(exe_path, force_folder_name=unique, force_local_wins=True)
        _added = get_library().get_by_exe(exe_path)
        if _added:
            self._suppress_cloud_prompt_once.add(_added.id)
            self._persist_cloud_no_local_decline(_added.id, _added.name)



    def _track_scan_dialog(self, dlg):
        """Remember the open auto-scan dialog so freshly live-detected paths
        can be pushed into it while it stays open (cleared on destroy)."""
        if dlg is None:
            return
        self._live_scan_dlg = dlg
        dlg.destroyed.connect(lambda *_: setattr(self, "_live_scan_dlg", None))

    def _push_paths_to_open_scan_dialog(self, game_id: str, paths: list[str]):
        """Feed live-detected paths into the auto-scan dialog if one is open
        for this game — the panel keeps emitting live detections while open."""
        dlg = self._live_scan_dlg
        if dlg is None or not paths:
            return
        try:
            entry = get_library().get_by_id(game_id)
            dlg.push_live_paths(game_id, entry.name if entry else "", paths)
        except RuntimeError:
            self._live_scan_dlg = None   # C++ side already deleted

    def _check_auto_scan_for_game(self, entry: GameEntry):
        """Show save-path confirmation dialog at game exit — only when needed.

        Shows the dialog ONLY when:
        A) Game was auto-added (requires_confirmation=True) AND has no confirmed paths yet.
        B) Live tracking found genuinely NEW paths not already in entry.save_paths.

        Never shows:
        - When all live-tracked paths already match confirmed save_paths.
        - When auto_scan_on_exit is disabled.
        - When the game entry is suppressed.
        - When the dialog was already shown this session for this game.
        """
        config = get_config()

        if not config.get("auto_scan_on_exit", True):
            return

        if entry.suppressed_overlay:
            return

        # Guard: never show twice per session for the same game
        already_shown = getattr(self, '_scan_dialog_shown_this_session', set())
        if entry.id in already_shown:
            return

        # Collect live-detected paths (pop from pending — one shot)
        pre_scanned_paths: list[str] = self._pending_auto_scans.pop(entry.id, [])

        # ── Decide whether we need to show the dialog ──────────────────────────

        confirmed_paths = set(entry.save_paths or [])

        # Case A: game was auto-added and has never had paths confirmed
        needs_first_confirmation = (
            entry.requires_confirmation and not entry.save_paths_confirmed
        )

        # Case B: live tracking found paths not yet in confirmed list.
        # Path-containment check (shared with the manual in-game panel):
        # a pre-scanned file under an already-configured save folder is NOT
        # a new path, and neither is a parent of an already-confirmed one.
        from ui.dialogs.auto_scan_dialog import filter_uncovered_paths
        new_paths = filter_uncovered_paths(pre_scanned_paths, list(confirmed_paths))
        has_new_paths = bool(new_paths)

        if not needs_first_confirmation and not has_new_paths:
            # Nothing genuinely new — skip the dialog entirely
            logger.debug(
                f"_check_auto_scan: skipping for {entry.name!r} "
                f"(confirmed={entry.save_paths_confirmed}, "
                f"new_paths={has_new_paths}, pre_scanned={len(pre_scanned_paths)})"
            )
            return

        # Per-game "don't show again": the user opted this game out of the
        # at-exit confirmation dialog — discard whatever was found, without
        # touching save_paths. Auto-adding here would be dangerous: live
        # tracking can pick up paths that aren't saves at all (e.g. an RPG
        # Maker game writing process logs elsewhere), and only the dialog
        # gives the user a chance to reject those. Suppressing means
        # suppressing, not silently accepting.
        per_game_skip: dict = config.get("scan_auto_accept_games", {})
        if per_game_skip.get(entry.id):
            logger.info(
                f"Discarding {len(new_paths)} detected path(s) for {entry.name!r} "
                f"(per-game 'don't show again')"
            )
            # Discarded detections take their temporary session backups with
            # them (definitive/confirmed backups are never touched).
            try:
                get_backup_manager().discard_pre_confirmation_backups(entry.id)
            except Exception as e:
                logger.warning(
                    f"Could not discard pre-confirmation backups for {entry.name}: {e}")
            return

        # ── Build paths to show in the dialog ─────────────────────────────────
        # For first confirmation, show ALL pre-scanned paths (even those already
        # in confirmed_paths, since the user hasn't confirmed them yet).
        # For subsequent scans, show only genuinely new paths.
        # If ALL pre-scanned paths are already confirmed and we only have
        # needs_first_confirmation, there's nothing new to show — confirm silently.
        if needs_first_confirmation:
            # If all pre-scanned paths are already confirmed (or subpaths of confirmed),
            # just mark confirmed — saves in an already-configured folder are not new.
            if pre_scanned_paths and not new_paths:
                logger.info(
                    f"_check_auto_scan: all {len(pre_scanned_paths)} paths already "
                    f"confirmed for {entry.name!r} — confirming silently"
                )
                get_library().update_game_fields(
                    entry.id,
                    save_paths_confirmed=True,
                    requires_confirmation=False,
                )
                # Silent confirmation still IS a confirmation: any temporary
                # session backups of these paths become definitive history.
                try:
                    get_backup_manager().promote_pre_confirmation_backups(
                        entry.id, note=t('main.auto_in_game'))
                except Exception as e:
                    logger.warning(
                        f"Could not promote pre-confirmation backups for {entry.name}: {e}")
                self._update_sidebar_status()
                return
            paths_to_show = pre_scanned_paths
        else:
            paths_to_show = new_paths

        # Final gate: only paths with actually-selectable content justify a
        # dialog. A detected path whose files are all excluded (or that was
        # permanently deleted before) must produce NO notification and NO
        # panel at all — not even a "1 path found" that then shows nothing.
        try:
            from ui.dialogs.auto_scan_dialog import filter_selectable_paths
            paths_to_show = filter_selectable_paths(entry.id, paths_to_show)
        except Exception as e:
            logger.debug(f"_check_auto_scan: selectable filter failed: {e}")

        if not paths_to_show:
            logger.debug(
                f"_check_auto_scan: no selectable paths for {entry.name!r} — skipping dialog"
            )
            return

        try:
            self._track_scan_dialog(show_auto_scan_dialog(
                self,
                paths_to_show,
                game_id=entry.id,
            ))
        except Exception as e:
            logger.error(f"Auto-scan dialog error: {e}")

    def _update_sidebar_status(self):
        playing = get_monitor().currently_playing()
        if playing:
            self._sidebar_status.setText(f"🎮 {playing[0].name}")
            fs = scaled(10, self)
            pad_v = scaled(8, self)
            pad_h = scaled(16, self)
            self._sidebar_status.setStyleSheet(f"color: {palette('accent')}; font-size: {fs}px; padding: {pad_v}px {pad_h}px;")
        else:
            orch = get_orchestrator()
            fs = scaled(10, self)
            pad_v = scaled(8, self)
            pad_h = scaled(16, self)
            if orch.is_online():
                self._sidebar_status.setText(t('main.online'))
                self._sidebar_status.setStyleSheet(f"color: {palette('info')}; font-size: {fs}px; padding: {pad_v}px {pad_h}px;")
            else:
                self._sidebar_status.setText(t('main.offline'))
                self._sidebar_status.setStyleSheet(f"color: {palette('text_muted')}; font-size: {fs}px; padding: {pad_v}px {pad_h}px;")

    # ── Orchestrator ──────────────────────────────────────────────────────────

    def _connect_orchestrator(self):
        orch = get_orchestrator()
        orch.sync_finished.connect(self._on_sync_finished)
        orch.provider_changed.connect(self._update_sidebar_status)
        orch.providers_updated.connect(self._update_sidebar_status)
        orch.conflict_detected.connect(self._on_conflict_detected)
        # Kick the (already background-threaded) provider load promptly — a
        # short settle lets the first paint land, but the old 500 ms was pure
        # artificial delay before the connection even starts.
        QTimer.singleShot(150, self._load_provider_async)



    def _load_provider_async(self):
        """Load all configured sync providers in a background thread."""
        from PySide6.QtCore import QThread, Signal as QSignal

        class _LoadWorker(QThread):
            done = QSignal(dict)  # {pid: bool}
            def run(self_w):
                try:
                    results = get_orchestrator().load_all_providers()
                    self_w.done.emit(results)
                except Exception as e:
                    logger.warning(f"load_all_providers error: {e}")
                    self_w.done.emit({})

        self._provider_load_worker = _LoadWorker()
        self._provider_load_worker.done.connect(self._on_providers_loaded)
        self._provider_load_worker.finished.connect(self._provider_load_worker.deleteLater)
        self._provider_load_worker.start()

    def _on_providers_loaded(self, results: dict):
        """Handle multi-provider load results and schedule reconnect for previously-connected failures."""
        self._update_sidebar_status()
        connected = [p for p, ok in results.items() if ok]
        failed = [p for p, ok in results.items() if not ok]
        if connected:
            logger.info(f"Sync providers connected on startup: {connected}")
        if failed:
            logger.warning(f"Failed to connect providers on startup: {failed}")
            self._status_bar.showMessage(t("sync.startup_reconnect_failed"), 8000)
            # Only auto-reconnect providers that were previously connected successfully
            providers_state = get_config().get("providers_connected", {})
            orch = get_orchestrator()
            for pid in failed:
                if providers_state.get(pid, False):
                    orch._schedule_reconnect(pid)
                else:
                    logger.debug(f"Skipping reconnect for '{pid}' (never connected successfully)")

    def _on_sync_finished(self, game_id: str, result):
        from datetime import datetime
        entry = get_library().get_by_id(game_id)
        name = entry.name if entry else ""
        if not name:
            try:
                from core.backup import get_backup_manager
                backs = get_backup_manager().get_backups_for_game(game_id)
                if backs:
                    name = (backs[0].game_name
                            or get_backup_manager()._game_folder_for_entry(backs[0])
                            or "")
            except Exception:
                name = ""
        if not name:
            name = game_id

        in_batch = bool(getattr(get_orchestrator(), "_sync_batch", None))

        # Chain the upload half of a "both" conflict resolution.
        # The download has just finished — now upload local saves.
        pending = getattr(self, '_pending_both_upload', None)
        if pending is not None and pending.id == game_id:
            self._pending_both_upload = None
            if result.success:
                get_orchestrator().sync_game(
                    pending.id, pending.name, pending.save_paths,
                    exe_path=pending.exe_path, direction="up",
                    computed_folder_name=pending.computed_folder_name,
                )

        if result.success:
            no_changes = (result.files_uploaded == 0 and result.files_downloaded == 0
                         and not result.conflicts)
            if entry:
                from datetime import timezone as _tz
                if result.conflicts:
                    get_library().update_game_fields(game_id, sync_status="conflict")
                elif no_changes:
                    # Up 0 / down 0: do NOT stamp last_synced — the library
                    # card and overview "recent activity" would claim a sync
                    # that moved nothing. The sync history log still records
                    # the run. Only clear a stuck pending badge.
                    if entry.sync_status == "pending":
                        get_library().update_game_fields(
                            game_id, sync_status="synced")
                else:
                    from core.backup import get_backup_manager as _gbm
                    recents = _gbm().get_backups_for_game(game_id)
                    synced_hash = (recents[0].cloud_metadata or {}).get("save_hash", "") if recents else ""
                    get_library().update_game_fields(
                        game_id,
                        last_synced=datetime.now(_tz.utc).isoformat(),
                        sync_status="synced",
                        cloud_metadata={**((entry.cloud_metadata or {})), "last_synced_hash": synced_hash},
                    )
            # During Sync Tutti the sidebar notice is the only live UI —
            # per-item status/overlay spam freezes the app on large batches.
            if not in_batch:
                if result.conflicts:
                    self._status_bar.showMessage(
                        t("sync.sync_conflicts", game=name, count=len(result.conflicts)), 8000)
                elif no_changes:
                    self._status_bar.showMessage(
                        t("sync.sync_unchanged", game=name), 4000)
                else:
                    self._status_bar.showMessage(t("sync.sync_success", game=name), 5000)
                    if self._overlay:
                        self._overlay.show_sync_done(name)
                self._update_sidebar_status()
        else:
            if entry:
                get_library().update_game_fields(game_id, sync_status="pending")
            if not in_batch:
                self._status_bar.showMessage(t("sync.sync_error", error=result.message), 8000)
                self._update_sidebar_status()

    # ── Game actions ──────────────────────────────────────────────────────────

    def _on_library_game_added_refresh(self, entry):
        """Library gained a real game — orphan archives wait for launch notify."""
        if entry is None:
            return
        # No silent adopt: matching orphans are offered via the cloud-saves
        # notification on first launch (_check_cloud_on_launch).
        try:
            self._library_page.refresh_styles()
        except Exception:
            pass

    def _show_scan_folder(self):
        """🔍 — scan a folder for installed games, confirm, add.

        Uses show() (not exec) so ✕ can shelve during scan/insert/store —
        same pattern as Aggiunta multipla.
        """
        from ui.dialogs.exe_scan_dialog import ExeScanDialog
        existing = getattr(self, "_exe_scan_dlg", None)
        if existing is not None:
            try:
                existing.unshelve()
                return
            except RuntimeError:
                self._exe_scan_dlg = None
        dlg = ExeScanDialog(self)
        self._exe_scan_dlg = dlg
        dlg.search_requested.connect(self._start_batch_game_search)
        dlg.shelved.connect(lambda: self._on_exe_scan_shelved(dlg))
        dlg.shelve_status.connect(lambda: self._refresh_exe_scan_shelf(dlg))
        dlg.finished.connect(lambda _r: self._on_exe_scan_finished(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _show_scan_folder_at(self, folder_path: str):
        """A whole folder was dropped on the library — same flow as 🔍,
        with the scan started on the dropped folder immediately."""
        from ui.dialogs.exe_scan_dialog import ExeScanDialog
        existing = getattr(self, "_exe_scan_dlg", None)
        if existing is not None:
            try:
                existing.unshelve()
                return
            except RuntimeError:
                self._exe_scan_dlg = None
        dlg = ExeScanDialog(self)
        self._exe_scan_dlg = dlg
        dlg.search_requested.connect(self._start_batch_game_search)
        dlg.shelved.connect(lambda: self._on_exe_scan_shelved(dlg))
        dlg.shelve_status.connect(lambda: self._refresh_exe_scan_shelf(dlg))
        dlg.finished.connect(lambda _r: self._on_exe_scan_finished(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.start_folder(folder_path)

    def _refresh_exe_scan_shelf(self, dlg):
        for e in self._shelved_add_entries:
            if e.get("dlg") is not dlg:
                continue
            btn = e.get("btn")
            if btn is None:
                return
            try:
                btn.set_status("running" if dlg.has_shelvable_work() else "done")
                btn.update_label(dlg.shelve_nav_label())
                btn.setToolTip(dlg.shelve_nav_tooltip())
            except RuntimeError:
                pass
            return

    def _on_exe_scan_shelved(self, dlg):
        entry = None
        for e in self._shelved_add_entries:
            if e.get("dlg") is dlg:
                entry = e
                break
        if entry is None:
            btn = NavButton(t("exe_scan.shelved_nav"), "🔍")
            btn.clicked.connect(lambda _=False, d=dlg: self._resurrect_exe_scan(d))
            self._shelved_adds_layout.addWidget(btn)
            entry = {"dlg": dlg, "btn": btn, "kind": "exe_scan"}
            self._shelved_add_entries.append(entry)
            self._shelved_adds_host.setVisible(True)
        self._refresh_exe_scan_shelf(dlg)
        QTimer.singleShot(0, lambda d=dlg: self._clear_phantom_add_game_modal(d))

    def _resurrect_exe_scan(self, dlg):
        try:
            dlg.unshelve()
        except RuntimeError:
            pass

    def _on_exe_scan_finished(self, dlg):
        if getattr(self, "_exe_scan_dlg", None) is dlg:
            self._exe_scan_dlg = None
        if getattr(dlg, "added_entries", None):
            try:
                self._library_page._load_library()
            except Exception:
                pass
            try:
                self._backups_page._load_games()
                self._backups_page._refresh_list()
            except Exception:
                pass
        keep = []
        for e in self._shelved_add_entries:
            if e.get("dlg") is dlg:
                btn = e.get("btn")
                if btn is not None:
                    self._shelved_adds_layout.removeWidget(btn)
                    btn.deleteLater()
            else:
                keep.append(e)
        self._shelved_add_entries = keep
        self._shelved_adds_host.setVisible(bool(keep))
        from ui.helpers import trim_process_memory
        QTimer.singleShot(300, trim_process_memory)

    def _start_batch_game_search(
        self,
        game_ids,
        *,
        prior_done: int = 0,
        prior_matched: int = 0,
        prior_completed_ids: list | None = None,
    ):
        """Kick off the opt-in web-search pass over freshly added games.

        Opens the detail panel by default (same expectation as before). The
        sidebar bar tracks progress in parallel; the panel only stays hidden
        if the user already minimised it during this run.
        """
        if not game_ids:
            return
        runner = self._game_search_runner()
        if runner.running:
            # Already going — reopen only if the user has not minimised.
            self._ensure_game_search_panel(
                show=not getattr(self, "_search_user_minimized", False))
            return
        if runner.start(
            list(game_ids),
            prior_done=prior_done,
            prior_matched=prior_matched,
            prior_total=len(game_ids) + max(0, int(prior_done)),
            prior_completed_ids=prior_completed_ids,
        ):
            self._search_user_minimized = False
            self._search_batch_notice.begin(
                t("batch.search_label"), runner.total)
            if prior_done:
                self._search_batch_notice.update_progress(
                    prior_done, runner.total, "")
            self._search_batch_notice.setToolTip(
                t("game_search.sidebar_tooltip_running"))
            self._ensure_game_search_panel(show=True)

    def _game_search_runner(self):
        runner = getattr(self, "_search_runner", None)
        if runner is None:
            from ui.game_search_runner import GameSearchRunner
            runner = GameSearchRunner(self)
            runner.progress.connect(self._on_search_batch_progress)
            runner.finished.connect(self._on_batch_search_finished)
            self._search_runner = runner
        return runner

    def _on_search_batch_progress(self, done: int, total: int, name: str):
        self._search_batch_notice.update_progress(done, total, name or "")

    def _on_search_panel_minimized(self):
        self._search_user_minimized = True

    def _ensure_game_search_panel(self, *, show: bool):
        runner = self._game_search_runner()
        if not runner.has_run:
            return
        panel = getattr(self, "_search_panel", None)
        if panel is not None:
            try:
                if show:
                    self._search_user_minimized = False
                    panel.show()
                    panel.raise_()
                    panel.activateWindow()
                return
            except RuntimeError:
                self._search_panel = None
        from ui.dialogs.game_search_panel import GameSearchPanel
        panel = GameSearchPanel(runner, self)
        panel.dismissed.connect(self._on_batch_search_dismissed)
        panel.minimized.connect(self._on_search_panel_minimized)
        self._search_panel = panel
        if show:
            self._search_user_minimized = False
            panel.show()
        else:
            panel.hide()

    def _show_game_search_panel(self):
        """Open (or re-show) the batch-search panel from the sidebar notice."""
        self._search_batch_notice.stop_auto_hide()
        self._search_user_minimized = False
        self._ensure_game_search_panel(show=True)

    def _on_batch_search_finished(self, matched: int, total: int, cancelled: bool):
        # Refresh the library so newly fetched covers and metadata show up.
        try:
            self._library_page._load_library()
        except Exception:
            logger.debug("Library refresh after batch search failed", exc_info=True)
        msg = t(
            "batch.search_done_cancelled" if cancelled else "batch.search_done",
            matched=matched, total=total,
        )
        # Stay a bit so the user can open the log; auto-hide if ignored.
        self._search_batch_notice.finish(msg, hide_after_ms=12000)
        self._search_batch_notice.setToolTip(
            t("game_search.nav_tooltip_failed" if cancelled
              else "game_search.nav_tooltip_done"))
        from ui.helpers import trim_process_memory
        QTimer.singleShot(400, trim_process_memory)

    def _on_batch_search_dismissed(self):
        """The user closed a finished run: retire it, so the sidebar entry
        doesn't linger pointing at a search nobody is waiting on any more."""
        self._search_panel = None
        self._search_batch_notice.stop_auto_hide()
        self._search_batch_notice.hide()
        runner = getattr(self, "_search_runner", None)
        if runner is not None and not runner.running:
            self._search_runner = None

    def _show_add_game(self, name: str = "", exe_path: str = ""):
        # Never steal focus back to a different shelved card — open a new one.
        # Construct shell on the next tick; sections fill async inside the dialog.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._open_add_game_dialog(name, exe_path))

    def _open_add_game_dialog(self, name: str = "", exe_path: str = ""):
        dlg = AddGameDialog(name=name, exe_path=exe_path, parent=self)

        def _on_added(entry):
            self._session_shown_exes.discard(entry.exe_path)
            self._overlay_shown_exes.discard(entry.exe_path)
            self._exit_dialog_shown_exes.discard(entry.exe_path)
            self._pending_unknown.pop(entry.exe_path, None)

        dlg.game_added.connect(_on_added)
        self._wire_add_game_shelve(dlg)
        # show_and_build — shell immediately, form sections + populate in chunks
        # (not exec()/open(): exec nests a loop; open() fights shelving).
        dlg.show_and_build()

    def _wire_add_game_shelve(self, dlg: AddGameDialog):
        """Keep a shelved Add/Edit dialog alive and reachable from the sidebar."""
        dlg.shelved.connect(lambda d=dlg: self._on_add_game_shelved(d))
        dlg.background_idle.connect(lambda d=dlg: self._on_add_game_background_idle(d))
        dlg.background_status_changed.connect(
            lambda status, d=dlg: self._on_add_game_bg_status(d, status))
        dlg.finished.connect(lambda _r, d=dlg: self._on_add_game_dialog_finished(d))

    def _shelved_entry_for(self, dlg) -> dict | None:
        for entry in self._shelved_add_entries:
            if entry.get("dlg") is dlg:
                return entry
        return None

    def _on_add_game_shelved(self, dlg):
        entry = self._shelved_entry_for(dlg)
        if entry is None:
            btn = NavButton(t("add_game.shelved_nav"), "📋")
            btn.clicked.connect(lambda _=False, d=dlg: self._resurrect_shelved_add_game(d))
            self._shelved_adds_layout.addWidget(btn)
            entry = {"dlg": dlg, "btn": btn}
            self._shelved_add_entries.append(entry)
            self._shelved_adds_host.setVisible(True)
        try:
            status = dlg._bg_notice_status or (
                "running" if dlg._has_shelvable_work() else "done")
        except RuntimeError:
            status = "running"
        entry["btn"].set_status(status)
        self._sync_add_dlg_nav_tip(dlg)
        # Next tick: clear any phantom modal still grabbing the main window.
        QTimer.singleShot(0, lambda d=dlg: self._clear_phantom_add_game_modal(d))

    def _clear_phantom_add_game_modal(self, dlg=None):
        """If a shelved (hidden) Add/Edit is still Qt's active modal widget,
        force NonModal so the library stays clickable."""
        targets = [dlg] if dlg is not None else [
            e["dlg"] for e in self._shelved_add_entries]
        for d in targets:
            if d is None:
                continue
            try:
                if d.isVisible():
                    continue
                modal = QApplication.activeModalWidget()
                if modal is d or d.isModal():
                    d.setWindowModality(Qt.WindowModality.NonModal)
            except RuntimeError:
                pass

    def _on_add_game_background_idle(self, dlg=None):
        self._sync_add_dlg_nav_tip(dlg)

    def _on_add_game_bg_status(self, dlg, status: str):
        entry = self._shelved_entry_for(dlg)
        if entry is None:
            # Still visible (not shelved yet) — ignore sidebar updates.
            return
        entry["btn"].set_status(status or "")
        self._sync_add_dlg_nav_tip(dlg)

    def _sync_add_dlg_nav_tip(self, dlg=None):
        targets = (
            [self._shelved_entry_for(dlg)] if dlg is not None
            else list(self._shelved_add_entries)
        )
        for entry in targets:
            if not entry:
                continue
            d, btn = entry.get("dlg"), entry.get("btn")
            if d is None or btn is None:
                continue
            try:
                btn.update_label(d.shelve_nav_label())
                btn.setToolTip(d.shelve_nav_tooltip())
            except RuntimeError:
                continue

    def _resurrect_shelved_add_game(self, dlg=None) -> bool:
        """Re-show a shelved Add/Edit. If *dlg* is None, re-show the first."""
        if dlg is None:
            if not self._shelved_add_entries:
                return False
            dlg = self._shelved_add_entries[0]["dlg"]
        try:
            dlg.unshelve()
            return True
        except RuntimeError:
            self._remove_shelved_add_entry(dlg)
            return False

    def resurrect_shelved_add_for_entry(self, entry_id: str):
        """Re-show the shelved edit for *entry_id*, or None if none."""
        if not entry_id:
            return None
        for entry in list(self._shelved_add_entries):
            d = entry.get("dlg")
            try:
                ed = getattr(d, "_editing_entry", None)
                if ed is not None and ed.id == entry_id:
                    d.unshelve()
                    return d
            except RuntimeError:
                self._remove_shelved_add_entry(d)
        return None

    def _remove_shelved_add_entry(self, dlg):
        entry = self._shelved_entry_for(dlg)
        if entry is None:
            return
        self._shelved_add_entries.remove(entry)
        btn = entry.get("btn")
        if btn is not None:
            self._shelved_adds_layout.removeWidget(btn)
            btn.deleteLater()
        self._shelved_adds_host.setVisible(bool(self._shelved_add_entries))

    def _on_add_game_dialog_finished(self, dlg):
        self._remove_shelved_add_entry(dlg)
        # Accept/reject tear the dialog down for real; without this the
        # Add/Edit dialog (and every pixmap it decoded) stays alive as a
        # hidden child of the main window — one stack of RAM per card opened.
        dlg.deleteLater()
        from ui.helpers import trim_process_memory
        QTimer.singleShot(250, trim_process_memory)

    def _start_backup_all(self, game_ids=None, force_full: bool = False,
                          source: str = "overview"):
        """Enqueue Backup Tutti with sidebar progress and disk resume state."""
        from datetime import datetime, timezone
        from core.concurrency import backup_max_inflight, log_limits
        from core import pending_batch_jobs as _pbj

        if game_ids is None:
            game_ids = [g.id for g in get_library().all_games() if g.save_paths]
        ids = [gid for gid in game_ids if gid]
        if not ids:
            return
        log_limits()
        self._backup_max_inflight = backup_max_inflight()
        completed: list[str] = []
        self._backup_batch = {
            "pending_ids": list(ids),
            "completed_ids": completed,
            "force": bool(force_full),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source": source or "overview",
            "total": len(ids),
            # Genuinely NEW backups (dedup-skipped games must not inflate the
            # completion message: 21 checked ≠ 21 created).
            "created_ids": [],
        }
        # Suppress per-game library/overview rebuilds until the batch ends.
        get_library().begin_bulk()
        _pbj.set_job(_pbj.KEY_BACKUP_ALL, {
            "pending_ids": list(ids),
            "completed_ids": [],
            "force": bool(force_full),
            "started_at": self._backup_batch["started_at"],
            "source": source or "overview",
        })
        first = get_library().get_by_id(ids[0])
        self._backup_batch_notice.begin(
            t("batch.backup_label"), len(ids),
            first.name if first else "")
        for gid in ids:
            self._enqueue_backup(gid, force_full=force_full, silent=True,
                                part_of_batch=True)
        self._pump_backup_queue()

    def _enqueue_backup(self, game_id: str, force_full: bool = False,
                        silent: bool = False, part_of_batch: bool = False):
        if not game_id:
            return
        with self._backup_lock:
            if game_id in self._backup_inflight or game_id in self._backup_queued:
                return
            self._backup_queued.add(game_id)
            self._backup_job_queue.append({
                "game_id": game_id,
                "force": bool(force_full),
                "silent": bool(silent),
                "batch": bool(part_of_batch),
            })

    def _pump_backup_queue(self):
        from core.concurrency import backup_max_inflight
        cap = self._backup_max_inflight or backup_max_inflight()
        while True:
            with self._backup_lock:
                if len(self._backup_inflight) >= cap:
                    return
                if not self._backup_job_queue:
                    return
                job = self._backup_job_queue.popleft()
                gid = job["game_id"]
                self._backup_queued.discard(gid)
                if gid in self._backup_inflight:
                    continue
                self._backup_inflight.add(gid)
            self._run_backup_job(job)

    def _run_backup_job(self, job: dict):
        """Create a backup for a game in a background thread to avoid UI freeze.

        Always uses entry.save_paths — the user's own confirmed paths — and
        nothing else. Deliberately NOT provisional-aware: this is the
        general-purpose backup helper used by broad operations too (e.g.
        "backup all", which iterates every game's save_paths), and those
        must never see live-tracking's not-yet-confirmed detections. The
        provisional mechanism is fully standalone — see
        _backup_provisional_paths, used only by the game-specific,
        live-tracking-driven callers (the in-game timer, a save-changed
        event) — and only for as long as this game has NO confirmed path
        at all; normal and provisional backups are never mixed.
        """
        game_id = job["game_id"]
        force_full = job.get("force", False)
        silent = job.get("silent", False)
        entry = get_library().get_by_id(game_id)
        if not entry:
            self._finish_backup_job(game_id, batch=job.get("batch", False))
            return
        if self._backup_batch and job.get("batch"):
            done = len(self._backup_batch.get("completed_ids") or [])
            total = int(self._backup_batch.get("total") or 0)
            self._backup_batch_notice.update_progress(done, total, entry.name)
        config = get_config()
        max_mb = config.get("max_backup_size_mb", 512)
        name = entry.name
        save_paths = list(entry.save_paths or [])
        exe_path = entry.exe_path
        computed = entry.computed_folder_name
        excluded = entry.excluded_save_paths

        def _do_backup():
            backup, created = get_backup_manager().create_backup(
                game_id, name, save_paths,
                exe_path=exe_path,
                max_size_mb=max_mb,
                force=force_full,
                computed_folder_name=computed,
                excluded_paths=excluded,
                return_status=True,
            )
            with self._backup_lock:
                self._backup_results.append(
                    (game_id, backup, silent, created, job.get("batch", False)))
            from PySide6.QtCore import QMetaObject, Qt
            try:
                QMetaObject.invokeMethod(
                    self, "_on_backup_done",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                logger.debug("Backup: MainWindow destroyed before result delivery")

        threading.Thread(target=_do_backup, daemon=True).start()

    def _backup_game(self, game_id: str, force_full: bool = False, silent: bool = False):
        """Enqueue a single-game backup under the adaptive concurrency cap."""
        from core.concurrency import backup_max_inflight
        self._backup_max_inflight = backup_max_inflight()
        self._enqueue_backup(game_id, force_full=force_full, silent=silent,
                             part_of_batch=False)
        self._pump_backup_queue()

    def _finish_backup_job(self, game_id: str, batch: bool = False,
                           created: bool = False):
        from core import pending_batch_jobs as _pbj
        with self._backup_lock:
            self._backup_inflight.discard(game_id)
        if batch and self._backup_batch:
            pending = [g for g in (self._backup_batch.get("pending_ids") or [])
                       if g != game_id]
            completed = list(self._backup_batch.get("completed_ids") or [])
            if game_id and game_id not in completed:
                completed.append(game_id)
            self._backup_batch["pending_ids"] = pending
            self._backup_batch["completed_ids"] = completed
            # Count only genuinely NEW backups. The completion message must
            # say how many backups were actually created — not how many
            # games were checked (dedup skips inflate that number).
            if created and game_id and game_id not in self._backup_batch["created_ids"]:
                self._backup_batch["created_ids"].append(game_id)
            total = int(self._backup_batch.get("total") or 0)
            done = len(completed)
            entry = get_library().get_by_id(pending[0]) if pending else None
            name = entry.name if entry else ""
            self._backup_batch_notice.update_progress(done, total, name)
            persist = (not pending) or (done % 8 == 0)
            _pbj.mark_game_done(_pbj.KEY_BACKUP_ALL, game_id, persist=persist)
            if not pending:
                try:
                    _pbj.flush()
                except Exception:
                    pass
                created_ids = list(self._backup_batch.get("created_ids") or [])
                self._backup_batch = None
                try:
                    get_library().end_bulk()
                except Exception:
                    logger.debug("end_bulk after Backup Tutti failed", exc_info=True)
                done_msg = self._backup_batch_done_message(created_ids)
                self._backup_batch_notice.finish(done_msg)
                # One aggregated toast at the end (the queue is already built;
                # a plain append, no per-game rebuilds). Respect the same
                # setting that gates single-backup toasts. A single created
                # backup reports the game's name, not just the number.
                try:
                    if (get_config().get("show_overlay_on_backup", True)
                            and self._overlay):
                        _bname = ""
                        if len(created_ids) == 1:
                            _bentry = get_library().get_by_id(created_ids[0])
                            _bname = _bentry.name if _bentry else ""
                        self._overlay.show_batch_done(
                            "backup", len(created_ids), _bname)
                except Exception:
                    logger.debug("batch toast after Backup Tutti failed", exc_info=True)
                self._update_sidebar_status()
                try:
                    self._overview_page.refresh()
                except Exception:
                    pass
                try:
                    self._backups_page._load_games()
                    self._backups_page._refresh_list()
                except Exception:
                    pass
                from ui.helpers import trim_process_memory
                QTimer.singleShot(400, trim_process_memory)
        self._pump_backup_queue()

    def _backup_batch_done_message(self, created_ids: list) -> str:
        """Completion notice for Backup Tutti.

        The progress bar counts every game CHECKED (21/21), but the completion
        message must only count the backups that were actually CREATED — the
        rest were dedup-skipped (content unchanged). One game → show its name;
        more than one → just the number; zero → nothing new to say.
        """
        n = len(created_ids)
        if n == 0:
            return t("batch.backup_done_none")
        if n == 1:
            entry = get_library().get_by_id(created_ids[0])
            name = entry.name if entry else ""
            return (t("batch.backup_done_one", name=name) if name
                    else t("batch.backup_done", done=1))
        return t("batch.backup_done", done=n)

    def _backup_provisional_paths(self, game_id: str, silent: bool = True):
        """Back up whatever live tracking has found so far for a game that
        has NO confirmed save path yet — a TEMPORARY/provisional backup the
        user can restore in-game even before ever confirming anything.

        Standalone from the normal backup pipeline (_backup_game) by
        design: normal and provisional backups must never mix. Runs
        exclusively while entry.save_paths is completely empty — the
        moment the user confirms even a single path, it moves into
        entry.save_paths and this mechanism stops touching this game
        entirely (see auto_scan_dialog.py's confirm handler and
        BackupManager.resolve_pre_confirmation_backups). Any OTHER path
        live tracking finds after that point is still discovered and
        surfaced through _pending_auto_scans for the user to review later,
        but is deliberately not auto-backed-up — it isn't the user's
        confirmed path yet, and provisional mode has already ended for
        this game.
        """
        entry = get_library().get_by_id(game_id)
        if not entry or entry.save_paths:
            return   # nothing to do, or already confirmed — not this mechanism's job
        if not entry.auto_backup_enabled:
            return
        if get_config().get("scan_auto_accept_games", {}).get(entry.id):
            return   # user suppressed confirmation for this game — avoid churn
        pre_scanned = getattr(self, '_pending_auto_scans', {}).get(entry.id, [])
        if not pre_scanned:
            return
        config = get_config()
        max_mb = config.get("max_backup_size_mb", 512)
        _note = t('main.auto_pre_confirm')
        _name = entry.name

        import threading
        def _do_backup():
            backup, created = get_backup_manager().create_backup(
                game_id, _name, pre_scanned,
                exe_path=entry.exe_path,
                note=_note,
                max_size_mb=max_mb,
                force=False,
                computed_folder_name=entry.computed_folder_name,
                excluded_paths=entry.excluded_save_paths,
                pre_confirmation=True,
                return_status=True,
            )
            if created:
                from PySide6.QtCore import QMetaObject, Qt
                try:
                    QMetaObject.invokeMethod(
                        self, "_show_ingame_provisional_backup_notif",
                        Qt.ConnectionType.QueuedConnection,
                    )
                except RuntimeError:
                    logger.debug("Backup: MainWindow destroyed before notification")

        threading.Thread(target=_do_backup, daemon=True).start()

    def _show_provisional_paths_manager(self, game_id: str):
        """Let the user review and confirm a game's provisional (live-tracking-
        discovered, not-yet-confirmed) saves from the library — the SAME
        confirmation dialog used at game-exit (AutoScanDialog), just
        reachable any time instead of only right after playing.

        Confirming there adds the kept paths to entry.save_paths (ending
        provisional mode for this game) and resolves every pre-confirmation
        backup per path — promoted for what was kept, discarded for what
        was rejected (see AutoScanDialog's confirm handler and
        BackupManager.resolve_pre_confirmation_backups). If a kept path
        has no backup at all yet (e.g. discovery outran the debounce
        timer), a normal backup is created for it immediately so
        confirming never leaves a path with nothing to restore from.

        Candidate paths are the union of this session's _pending_auto_scans
        (if the game was played this session) and whatever paths existing
        provisional backups already cover (so this still works after an
        app restart, when _pending_auto_scans has reset to empty but the
        backups themselves are still on disk).
        """
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        paths = set(getattr(self, '_pending_auto_scans', {}).get(game_id, []))
        for b in get_backup_manager().get_backups_for_game(game_id):
            if (b.cloud_metadata or {}).get("pre_confirmation"):
                paths.update(b.save_paths or [])
        if not paths:
            return   # nothing provisional left to review (already resolved elsewhere)
        from ui.dialogs.auto_scan_dialog import show_auto_scan_dialog
        dlg = show_auto_scan_dialog(self, pre_scanned_paths=sorted(paths),
                                    game_id=game_id, user_initiated=True)
        if not dlg:
            # Every candidate turned out non-selectable (e.g. already
            # excluded) once filtered — still open the panel so the click
            # visibly does something, just empty rather than auto-scanning.
            dlg = show_auto_scan_dialog(self, None, game_id=game_id,
                                        user_initiated=True, auto_scan=False)
        if dlg:
            # Whatever the user does in the panel — confirm (kept paths move to
            # save_paths) or discard everything (provisional backups deleted) —
            # the card badge must re-render. Confirm rides game_updated, but a
            # pure discard changes no entry field, so refresh on close covers
            # the reverse transition (Provvisorio → no saves).
            dlg.finished.connect(
                lambda *_: self._library_page.refresh_game_status(game_id))

    @Slot()
    def _on_backup_done(self):
        """Handle backup completion on the GUI thread."""
        batch = False
        with self._backup_lock:
            if not self._backup_results:
                return
            try:
                item = self._backup_results.popleft()
                if len(item) == 5:
                    game_id, backup, silent, created, batch = item
                else:
                    game_id, backup, silent, created = item
            except IndexError:
                return
        try:
            if backup:
                entry = get_library().get_by_id(game_id)
                if not entry:
                    return

                # `created` (from create_backup's return_status) says precisely
                # whether this was a genuinely new backup or a dedup-skip that
                # returned the existing latest entry — no timestamp guessing, so a
                # backup made seconds after the previous one is no longer
                # misclassified as new (the spurious "unchanged but synced" bug).
                from datetime import datetime

                # Always mark pending watcher files as backed up — whether the
                # backup is genuinely new or a dedup skip (content unchanged).
                # Without this, dedup-skipped files stay pending forever and
                # re-trigger the backup flow on every filesystem event.
                try:
                    from core.watcher import mark_game_files_backed_up
                    mark_game_files_backed_up(game_id)
                except Exception:
                    pass

                if created:
                    # Use atomic field update to avoid clobbering concurrent
                    # playtime changes from the monitor's exit handler.
                    from datetime import timezone as _tz
                    lib = get_library()
                    lib.update_game_fields(
                        game_id,
                        last_backed_up=datetime.now(_tz.utc).isoformat(),
                        machine_id=get_machine_id(),
                    )

                    config = get_config()
                    # Never auto-sync mid Backup Tutti — would stampede Sync Tutti
                    # and freeze the UI the same way toast spam did.
                    if (not batch
                            and config.get("auto_sync_after_backup", False)
                            and entry.save_paths):
                        orch = get_orchestrator()
                        if orch.is_online():
                            logger.info(f"Auto-syncing after backup for {entry.name}")
                            orch.sync_game(
                                entry.id, entry.name, entry.save_paths,
                                exe_path=entry.exe_path,
                                computed_folder_name=entry.computed_folder_name,
                                name_history=list(entry.name_history))

                    if silent or batch:
                        logger.info(f"Auto-backup created silently for {entry.name}")
                    elif config.get("show_overlay_on_backup", True):
                        self._status_bar.showMessage(
                            t("notifications.backup_created", game=entry.name), 5000)
                        if self._overlay:
                            self._overlay.show_backup_done(entry.name)
                    else:
                        logger.info(
                            f"Backup created for {entry.name} (notifications disabled)")
                else:
                    # Dedup skip — content unchanged since the previous backup.
                    config = get_config()
                    _needs_reconcile = (
                        entry.sync_status == "pending"
                        or getattr(entry, "pending_local_wins", False)
                    )
                    if (not batch
                            and _needs_reconcile
                            and config.get("auto_sync_after_backup", False)
                            and entry.save_paths):
                        orch = get_orchestrator()
                        if orch.is_online():
                            logger.info(
                                f"Reconciling pending sync status for {entry.name} "
                                f"(backup unchanged)")
                            orch.sync_game(
                                entry.id, entry.name, entry.save_paths,
                                exe_path=entry.exe_path,
                                computed_folder_name=entry.computed_folder_name,
                                name_history=list(entry.name_history))
                    if not silent and not batch:
                        msg = t("notifications.backup_unchanged", game=entry.name)
                        self._status_bar.showMessage(msg, 4000)
                        if self._overlay:
                            self._overlay.show_backup_done(entry.name, skipped=True)
        finally:
            self._finish_backup_job(game_id, batch=batch, created=bool(created))

    def _restore_game_latest(self, game_id: str):
        """Open backup picker — RestoreDialog handles its own confirmation."""
        from ui.dialogs.restore_dialog import RestoreDialog
        dlg = RestoreDialog(game_id, parent=self)
        # confirmed=True because RestoreDialog already asked the user
        dlg.restore_confirmed.connect(
            lambda bid: self._restore_game_by_id(game_id, bid, confirmed=True)
        )
        dlg.exec()

    def _restore_game_by_id(self, game_id: str, backup_id: str, confirmed: bool = False):
        """Restore backup_id for game_id.  If confirmed=False, ask the user first.
        For orphan backups (no game in library), prompts user with custom file picker."""
        lib_entry = get_library().get_by_id(game_id)
        bk = get_backup_manager().get_backup(backup_id)
        target_dir = ""
        is_orphan = lib_entry is None or not bk or not bk.save_paths

        if is_orphan:
            # Orphan backup (archivio senza gioco in libreria): apri il browse file custom per selezionare dove salvarlo
            from ui.widgets.file_pickers import pick_folder
            target_dir = pick_folder(self, t("backup.select_restore_destination"))
            if not target_dir:
                return

        if not confirmed and not is_orphan:
            if bk:
                from core import to_local_dt
                _dt = to_local_dt(bk.created_at)
                if _dt is not None:
                    from i18n import format_dt
                    dt = format_dt(_dt, "%d %b %Y %H:%M")
                else:
                    dt = bk.created_at[:16] if bk.created_at else "?"
                reply = question_window_modal(
                    self, t("backup.restore_confirm_title"),
                    t("backup.restore_confirm_body", date=dt),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        # Run restore in a background thread so the UI stays responsive
        # during zip decompression.  The result is delivered to the GUI
        # thread via _on_restore_step1_done which handles the retry flow.
        self._status_bar.showMessage(t("backup.restoring"), 0)
        import threading

        def _do_restore():
            result = get_backup_manager().restore_backup(backup_id, target_dir=target_dir)
            # Remember what WE put there: landing on that exact state is
            # the intended outcome, and must not read as a regression.
            self._last_restored[game_id] = backup_id
            with self._restore_lock:
                self._restore_results.append(("step1", game_id, backup_id, result, target_dir))
            from PySide6.QtCore import QMetaObject, Qt
            try:
                QMetaObject.invokeMethod(
                    self, "_on_restore_step1_done",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                pass

        threading.Thread(target=_do_restore, daemon=True).start()

    @Slot()
    def _on_restore_step1_done(self):
        """Handle initial restore result on the GUI thread."""
        with self._restore_lock:
            # Find the step1 result
            pending = None
            for i, item in enumerate(self._restore_results):
                if isinstance(item, tuple) and len(item) == 5 and item[0] == "step1":
                    pending = item
                    del self._restore_results[i]
                    break
        if pending is None:
            return
        _, game_id, backup_id, result, target_dir = pending
        self._status_bar.clearMessage()

        if result.success:
            if target_dir:
                self._status_bar.showMessage(
                    t("backup.restore_orphan_success", path=target_dir), 4000)
            elif result.used_fallback_dir:
                # Files were written, but NOT to the game's real save
                # location — every save_path was still a foreign-user path
                # after resolution (see restore_backup's hard safety gate).
                # A silent "success" here would be indistinguishable from a
                # real restore, so this needs its own, unambiguous message
                # rather than folding into _finalize_restore's normal
                # success path.
                warning_window_modal(
                    self, t("backup.restore_confirm_title"),
                    t("backup.restore_fallback_warning", path=result.used_fallback_dir),
                )
            self._finalize_restore(game_id)
            return

        if not result.failed:
            self._status_bar.showMessage(t("backup.restore_failed"), 4000)
            return

        # Some files failed — offer to freeze the game process
        n_failed = len(result.failed)
        failed_names = ", ".join(Path(f).name for f in result.failed[:3])
        if n_failed > 3:
            failed_names += f" (+{n_failed - 3})"

        reply = question_window_modal(
            self, t("backup.restore_confirm_title"),
            t("backup.restore_locked_files",
              count=n_failed, files=failed_names),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            if result.restored:
                self._finalize_restore(game_id)
            return

        # Find game PID and retry with freeze in background
        freeze_pid = self._find_game_pid(game_id)
        if not freeze_pid:
            warning_window_modal(
                self, t("backup.restore_confirm_title"),
                t("backup.restore_cant_find_process"),
            )
            if result.restored:
                self._finalize_restore(game_id)
            return

        self._status_bar.showMessage(t("backup.restoring"), 0)
        _first_result = result
        import threading

        def _do_retry():
            retry = get_backup_manager().restore_backup(
                backup_id,
                freeze_pid=freeze_pid,
                only_files=set(_first_result.failed),
            )
            with self._restore_lock:
                self._restore_results.append(("step2", game_id, _first_result, retry))
            from PySide6.QtCore import QMetaObject, Qt
            try:
                QMetaObject.invokeMethod(
                    self, "_on_restore_step2_done",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                pass

        threading.Thread(target=_do_retry, daemon=True).start()

    @Slot()
    def _on_restore_step2_done(self):
        """Handle freeze-retry restore result on the GUI thread."""
        with self._restore_lock:
            pending = None
            for i, item in enumerate(self._restore_results):
                if isinstance(item, tuple) and len(item) == 4 and item[0] == "step2":
                    pending = item
                    del self._restore_results[i]
                    break
        if pending is None:
            return
        _, game_id, first_result, retry = pending
        self._status_bar.clearMessage()

        if retry.success:
            self._finalize_restore(game_id)
            return

        # Freeze didn't help — inform user
        if retry.failed:
            detail = "\n".join(retry.errors[:5])
            warning_window_modal(
                self, t("backup.restore_confirm_title"),
                t("backup.restore_still_failed",
                  count=len(retry.failed)) + "\n\n" + detail,
            )
        if first_result.restored or retry.restored:
            self._finalize_restore(game_id)

    def _finalize_restore(self, game_id: str):
        """Update library after a successful (or partial) restore, and — this
        was the missing half — actually tell the user it worked.

        Previously this method only updated internal bookkeeping
        (mark_backed_up / sync_status) with no status-bar message, no
        overlay, nothing. The restore itself was completing correctly on
        disk, but from the user's side clicking "Restore" appeared to do
        nothing at all, since there was no confirmation of any kind on the
        success path (contrast with backup creation, which always shows a
        status-bar message and an overlay toast — see the auto-backup tick
        handler above). show_restore_result() already existed on the
        overlay and was already wired up for the *quick-restore* flow — it
        just was never connected here, on the main Backups/Library page
        restore path.
        """
        entry = get_library().get_by_id(game_id)
        if entry:
            entry.mark_backed_up(get_machine_id())
            if get_orchestrator().is_online():
                entry.sync_status = "pending"
            get_library().update_game(entry)

        game_name = entry.name if entry else ""
        self._status_bar.showMessage(
            t("backup.restore_success", game=game_name) if game_name
            else t("backup.restore_success_generic"),
            5000,
        )
        if self._overlay and get_config().get("show_overlay_on_backup", True):
            self._overlay.show_restore_result(True, game_name)

        # A launcher's automatic sync reacts on its own schedule: some
        # overwrite within a second of seeing the files change, others only
        # when their client next talks to the server. One sample would catch
        # the fast ones and miss the rest, so the state is looked at a few
        # times over the first minute. The check stops at the first hit, and
        # the next game launch remains the backstop for anything slower.
        for delay in self._REGRESSION_AFTER_RESTORE_MS:
            QTimer.singleShot(
                delay,
                lambda gid=game_id: self._check_save_regression(gid, after_restore=True))

    _REGRESSION_AFTER_RESTORE_MS = (3000, 10000, 30000, 60000)

    def _find_game_pid(self, game_id: str) -> int:
        """Find the PID of a running game using the monitor's public API.
        Handles launcher→child process scenarios (e.g., launcher.exe spawns game.exe)."""
        entry = get_library().get_by_id(game_id)
        if not entry or not entry.exe_path:
            return 0
        try:
            return get_monitor().find_game_process(game_id, entry.exe_path)
        except Exception as e:
            logger.warning(f"Could not find game PID: {e}")
        return 0

    def _launch_game(self, game_id: str):
        """Launch the game - via launcher URL if available, otherwise directly.
        
        When launching via URL, if game exists by exe_path, update appid."""
        import subprocess, platform
        entry = get_library().get_by_id(game_id)
        if not entry or (not entry.exe_path and not entry.appid):
            self._status_bar.showMessage(t("status.no_executable"), 3000)
            return
        
        launched_via_url = False
        if entry.appid:
            try:
                if platform.system() == "Windows":
                    import os
                    os.startfile(entry.appid)
                else:
                    subprocess.Popen(["xdg-open", entry.appid])
                launched_via_url = True
                self._status_bar.showMessage(t('core.launching_via_launcher', name=entry.name), 3000)
                logger.info(f"Launched {entry.name} via {entry.appid}")
            except Exception as e:
                logger.warning(f"Launch via URL failed for {entry.name}: {e}")
        
        if not launched_via_url:
            try:
                if platform.system() == "Windows":
                    import os
                    os.startfile(entry.exe_path)
                else:
                    subprocess.Popen([entry.exe_path])
                self._status_bar.showMessage(f"{t('core.launching')} {entry.name}…", 3000)
            except Exception as e:
                self._status_bar.showMessage(f"{t('core.launch_failed')}: {e}", 5000)
                logger.warning(f"Launch failed for {entry.exe_path}: {e}")

    def _on_folder_appeared(self, game_id: str):
        """Save folder just appeared for the first time.
        If user hasn't yet confirmed save paths for this game, suggest scanning now."""
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        if entry.save_paths_confirmed:
            # User already confirmed paths — silently update watcher paths
            self._watcher.watch_game(game_id, entry.save_paths, game_name=entry.name)
            return
        # Show persistent status bar invite
        self._status_bar.showMessage(
            t('main.save_folder_detected', name=entry.name), 8000
        )
        # Also flash overlay if visible
        if self._overlay:
            from core.engines.game_engine import engine_display, engine_for_game
            self._overlay.show_game_launched(
                entry.name, entry.exe_path or "",
                engine=engine_display(engine_for_game(entry)))

    def _remove_game(self, game_id: str):
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        exe_path = entry.exe_path

        # Ask for confirmation before removing
        reply = question_window_modal(
            self, t("app.name"),
            t("library.remove_confirm", name=entry.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Remove from library
        get_library().remove_game(game_id)
        
        # Clean up tracking for this exe
        if exe_path:
            self._session_shown_exes.discard(exe_path)
            self._overlay_shown_exes.discard(exe_path)
        
        # Clean up icon cache subfolder for this game
        from core.constants import USER_DATA_DIR
        from core.constants import get_install_folder_name
        if entry.name:
            game_folder = get_install_folder_name(entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)
            game_icon_dir = USER_DATA_DIR / "icons" / game_folder
            if game_icon_dir.exists():
                try:
                    import shutil
                    shutil.rmtree(game_icon_dir)
                except Exception:
                    pass
            self._exit_dialog_shown_exes.discard(exe_path)
            self._pending_unknown.pop(exe_path, None)
            
            # Allow overlay to show again for this exe in future
            monitor = get_monitor()
            if hasattr(monitor, 'clear_seen_exe'):
                monitor.clear_seen_exe(exe_path)

    def _edit_game(self, game_id: str):
        # Same game already shelved → reopen that card; otherwise a new edit.
        if self.resurrect_shelved_add_for_entry(game_id) is not None:
            return
        entry = get_library().get_by_id(game_id)
        if entry:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda e=entry: self._open_edit_game_dialog(e))

    def _open_edit_game_dialog(self, entry):
        dlg = AddGameDialog(entry=entry, parent=self)
        self._wire_add_game_shelve(dlg)
        dlg.show_and_build()

    def _sync_game(self, game_id: str):
        entry = get_library().get_by_id(game_id)
        if not entry:
            return
        orch = get_orchestrator()
        if not orch.is_online():
            self._status_bar.showMessage(t("sync.no_provider"), 4000)
            return
        orch.sync_game(game_id, entry.name, entry.save_paths, exe_path=entry.exe_path, computed_folder_name=entry.computed_folder_name, name_history=list(entry.name_history))
        self._status_bar.showMessage(t("sync.syncing"), 2000)

    # ── i18n ──────────────────────────────────────────────────────────────────

    def _connect_i18n(self):
        get_engine().language_changed.connect(self._on_language_changed)

    def _on_language_changed(self, locale: str):
        # Relabeling every page (and rebuilding library chrome) blocks the
        # GUI thread long enough to look hung — same "please wait" sheet as
        # a theme swap.
        from ui.widgets.busy_overlay import busy_over
        # Relabel blocks the GUI thread with no mid-work ticks — show at once.
        with busy_over(self, delay_ms=0) as overlay:
            self._on_language_changed_inner(locale, overlay=overlay)

    def _on_language_changed_inner(self, locale: str, overlay=None):
        if overlay is not None:
            overlay.set_base_text(t("common.please_wait"))
        self.setWindowTitle(t("app.name"))
        # Pins are top-level windows of their own: nothing else in the tree
        # walk below reaches them.
        try:
            from ui.widgets.pins import get_pin_manager
            get_pin_manager().update_locale()
        except Exception as e:
            logger.debug(f"Pin locale refresh failed: {e}")
        # Sidebar
        self._sidebar_logo.setText(t("app.name").upper())
        self._sidebar_tagline.setText(t("app.tagline"))
        self._credits_btn.setText(t("credits.title"))
        # Sidebar status ("Online"/"Offline"/"🎮 <game>") is otherwise only
        # repainted on the next provider/monitor event, so it would stay in
        # the previous language until then — refresh it here explicitly.
        self._update_sidebar_status()
        if overlay is not None:
            overlay.pump()
        # Nav buttons
        for i, (key, icon) in enumerate(self._nav_defs):
            self._nav_buttons[i].update_label(t(key))
        # Not in _nav_defs — it sits under Credits, not with the others.
        self._cheats_nav_btn.update_label(t('cheats.nav'))
        self._sync_add_dlg_nav_tip()
        if self._cheats_page is not None and hasattr(self._cheats_page, 'update_locale'):
            self._cheats_page.update_locale()
        if overlay is not None:
            overlay.pump()
        # Tray menu — rebuild with translated strings
        if hasattr(self, '_tray') and self._tray:
            self._tray.setToolTip(t("app.name"))
            menu = self._tray.contextMenu()
            if menu:
                actions = menu.actions()
                # Account for separators in the action list:
                # Open, separator, Overview, Library, Backups, separator, Quit
                tray_labels = [
                    t("tray.open"), None, t("tray.overview"), t("tray.library"),
                    t("tray.backups"), None, t("tray.quit"),
                ]
                for action, label in zip(actions, tray_labels):
                    if label is not None:
                        action.setText(label)
        # Status bar
        self._status_bar.showMessage(t("status.ready"), 1000)
        # All pages (cheats may still be a lazy placeholder)
        pages = [self._overview_page, self._library_page,
                 self._sync_page, self._backups_page, self._settings_page]
        if self._cheats_page is not None:
            pages.append(self._cheats_page)
        for page in pages:
            if hasattr(page, "update_locale"):
                page.update_locale()
            if overlay is not None:
                overlay.pump()
        name = t(f"languages.{locale}")
        self._status_bar.showMessage(f"🌐 {name}", 3000)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _setup_cleanup(self):
        QApplication.instance().aboutToQuit.connect(self._on_quit)

    def _on_quit(self):
        # Never leave the pointer raised on the way out: the counter belongs
        # to this process, and quitting with it up outlives the panel it was
        # raised for.
        try:
            from ui.helpers import SystemCursor
            SystemCursor.release_all()
        except Exception as e:
            logger.debug(f"Cursor restore on quit failed: {e}")
        # Take the pins down without forgetting them — they come back on the
        # next start, which is the point of pinning something.
        try:
            from ui.widgets.pins import get_pin_manager
            get_pin_manager().shutdown()
        except Exception as e:
            logger.debug(f"Pin shutdown failed: {e}")
        # Stop all in-game backup timers
        for gid in list(self._ingame_backup_timers):
            self._stop_ingame_backup_timer(gid)
        get_monitor().stop()
        if hasattr(self, "_watcher"):
            self._watcher.stop()
        get_orchestrator().shutdown()
        # Disconnect SyncPage signals to prevent stale callbacks during teardown
        if hasattr(self, '_sync_page') and hasattr(self._sync_page, 'disconnect_signals'):
            self._sync_page.disconnect_signals()

        # Clean up overlay and blur modal to disconnect screen signals
        if hasattr(self, '_overlay') and self._overlay:
            if hasattr(self._overlay, 'cleanup'):
                self._overlay.cleanup()
        if hasattr(self, '_blur_modal') and self._blur_modal:
            if hasattr(self._blur_modal, 'cleanup'):
                self._blur_modal.cleanup()

        get_hotkey_manager().unregister_all()
        # Disconnect focus change handler
        try:
            QApplication.instance().focusObjectChanged.disconnect(self._on_focus_changed)
        except (RuntimeError, AttributeError):
            pass
        get_config().save()
        logger.info("SaveSync shutdown complete")