"""
SaveSync - Backups Page
Per-game backup list. _empty_lbl is permanent (never deleted in clear loop).
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QPoint, QRect
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QComboBox, QMessageBox, QApplication,
    QSizePolicy,
)

from i18n import t
from core import fmt_size as _fmt_size
from core.library import get_library, GameEntry
from core.backup import get_backup_manager, BackupEntry
from ui.backup_labels import ORIGIN_LABELS, origin_badge
from ui.helpers import (
    PageScrollMixin, lock_min_size, open_in_file_manager,
    safe_widget as _safe, scaled,
)
from ui.modal_helpers import (
    message_box_window_modal,
    question_window_modal,
    warning_window_modal,
)
from ui.styles.theme import palette, ThemedMixin


class BackupRow(QFrame, ThemedMixin):
    restore_requested = Signal(str)
    delete_requested  = Signal(str)

    def __init__(self, entry: BackupEntry, is_playing: bool = False,
                 cloud_only: bool = False, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._is_playing = is_playing
        self._cloud_only = cloud_only
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("backup_row")
        self._build()

    def _build(self):
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)

        # Local-time display: created_at is stored as naive UTC — always
        # convert through core.to_local_dt.
        from core import to_local_dt
        from i18n import format_dt
        dt = to_local_dt(self._entry.created_at)
        if dt is not None:
            date_str = format_dt(dt, "%d %b %Y  %H:%M")
        else:
            date_str = self._entry.created_at or "?"

        # Left column — chrome via theme (#backup_row_*); verify-dot stays inline.
        date_lbl = QLabel(date_str)
        date_lbl.setObjectName("backup_row_date")
        size_lbl = QLabel(self._entry.size_human)
        size_lbl.setObjectName("backup_row_meta")
        machine_lbl = QLabel()
        if self._entry.machine_id:
            machine_lbl.setText(f"🖥  {self._entry.machine_id[:8]}…")
            machine_lbl.setObjectName("backup_row_meta_sm")

        origin_lbl = QLabel(origin_badge(self._entry))
        origin_lbl.setObjectName("backup_row_meta_sm")

        note_lbl = QLabel(self._entry.note or "")
        note_lbl.setObjectName("backup_row_note")

        # Integrity dot — grey until the backup has been checked. Clicking it
        # checks this one; the page header checks them all. Colour-by-state is
        # left inline on purpose: it IS the state, so it belongs with the code
        # that knows it, not in the theme.
        self._verify_dot = QPushButton("●")
        self._verify_dot.setFixedSize(scaled(18, self), scaled(18, self))
        self._verify_dot.setCursor(Qt.CursorShape.PointingHandCursor)
        self._verify_dot.clicked.connect(self._on_verify_clicked)
        self._apply_verify_dot()

        left_col = QVBoxLayout()
        left_col.setSpacing(2)
        _date_row = QHBoxLayout()
        _date_row.setSpacing(6)
        _date_row.setContentsMargins(0, 0, 0, 0)
        _date_row.addWidget(self._verify_dot)
        _date_row.addWidget(date_lbl)
        _date_row.addStretch()
        left_col.addLayout(_date_row)
        sub = QHBoxLayout(); sub.setSpacing(10)
        sub.addWidget(size_lbl); sub.addWidget(origin_lbl); sub.addWidget(machine_lbl); sub.addWidget(note_lbl)
        sub.addStretch()
        left_col.addLayout(sub)

        # In-game safety badge
        if self._is_playing:
            badge = QLabel(t('backups.game_running'))
            badge.setObjectName("backup_row_playing")
            left_col.addWidget(badge)

        row.addLayout(left_col, 1)

        # Backup folder button
        backup_folder_btn = QPushButton("📁")
        backup_folder_btn.setObjectName("icon_btn")
        backup_folder_btn.setFixedSize(scaled(28, self), scaled(28, self))
        backup_folder_btn.setToolTip(t("tooltips.open_backup_folder"))
        backup_folder_btn.clicked.connect(self._on_open_backup_folder)

        self._restore_btn = QPushButton(t("buttons.restore"))
        self._restore_btn.setObjectName("primary_btn")
        self._restore_btn.setFixedHeight(scaled(28, self))
        # Never clip the label: "Ripristina" at the scaled font needs ~104px
        # plus QSS padding. 92px was too narrow and sheared the leading "R".
        self._restore_btn.setMinimumWidth(scaled(108, self))
        self._restore_btn.setMaximumWidth(scaled(140, self))
        self._restore_btn.setToolTip(
            t("core.game_running_warning") if self._is_playing
            else t("backup.restore_confirm")
        )
        self._restore_btn.clicked.connect(self._on_restore_clicked)

        del_btn = QPushButton("🗑")
        del_btn.setObjectName("icon_btn")
        del_btn.setFixedSize(scaled(30, self), scaled(28, self))
        del_btn.setToolTip(t("tooltips.delete_backup"))
        del_btn.clicked.connect(self._confirm_delete)


        row.addWidget(backup_folder_btn)
        row.addWidget(self._restore_btn)
        row.addWidget(del_btn)

    def _on_restore_clicked(self):
        if self._is_playing:
            reply = message_box_window_modal(
                self, QMessageBox.Icon.Warning,
                t("core.game_running_title"),
                t("core.game_running_message"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        if self._cloud_only:
            self._download_then_restore()
        else:
            self.restore_requested.emit(self._entry.backup_id)

    def _download_then_restore(self):
        """Download a cloud-only backup in a background thread, import locally, then request restore."""
        from sync import get_orchestrator
        from core.constants import get_install_folder_name
        from core.library import get_library

        orch = get_orchestrator()
        # Cloud-only rows carry origin = PROVIDER_ID (set when listed).
        # Never use orch.provider (first connected) — the zip may live on
        # another connected store.
        origin = (getattr(self._entry, "origin", "") or "").strip()
        provider = orch.get_provider(origin) if origin and origin != "local" else None
        if provider is not None and not provider.is_connected:
            provider = None
        if not provider:
            warning_window_modal(self, t("common.error"), t("sync.provider_disconnected"))
            return

        # Prefer the remote folder recorded when this cloud entry was
        # listed — authoritative even when the local install-folder name
        # carries a different version/build suffix.
        game_folder = (self._entry.cloud_metadata or {}).get("remote_folder", "")
        if not game_folder:
            entry = get_library().get_by_id(self._entry.game_id)
            if entry:
                # Game is in local library — compute folder from its metadata
                game_folder = get_install_folder_name(entry.exe_path, entry.name, self._entry.game_id, entry.computed_folder_name)
                resolved = orch.resolve_remote_game_folder(provider, [game_folder])
                if resolved:
                    game_folder = resolved
            else:
                # Cloud-only backup from another PC: game_id stores the remote folder name directly
                game_folder = self._entry.game_id

        remote_zip = f"SaveSync/backup/{game_folder}/{self._entry.backup_id}.zip"
        backup_entry = self._entry
        self._restore_btn.setEnabled(False)

        import threading
        def _bg_download():
            from pathlib import Path
            import tempfile
            error = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                ok = provider.download(remote_zip, tmp_path)
                if not ok or not tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                    error = t("restore.download_failed")
                else:
                    zip_data = tmp_path.read_bytes()
                    tmp_path.unlink(missing_ok=True)
                    if not get_backup_manager().import_backup(backup_entry, zip_data):
                        error = t("restore.import_failed")
            except Exception as e:
                error = str(e)[:200]
            def _done():
                try:
                    self._restore_btn.setEnabled(True)
                except RuntimeError:
                    return
                if error:
                    warning_window_modal(self, t("common.error"), error)
                else:
                    self.restore_requested.emit(backup_entry.backup_id)
            # Marshal to GUI thread via a singleshot
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, _done)

        threading.Thread(target=_bg_download, daemon=True).start()

    def _confirm_delete(self):
        reply = question_window_modal(
            self, t('backups.delete_backup'),
            t('backups.delete_backup_question', date=(self._entry.created_at or "")[:10]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(self._entry.backup_id)

    def refresh_styles(self):
        """Replay the registered styles, then the dot — its colour comes from
        the entry's state, so it can't be a registered one-shot."""
        super().refresh_styles()
        if hasattr(self, "_verify_dot"):
            self._apply_verify_dot()

    # ── Integrity dot ────────────────────────────────────────────────────────

    _VERIFY_LOOK = {
        "ok":      ("success",   "backups.verify_ok"),
        "corrupt": ("error",     "backups.verify_corrupt"),
        "changed": ("warning",   "backups.verify_changed"),
        "missing": ("error",     "backups.verify_missing"),
        "":        ("text_hint", "backups.verify_unchecked"),
    }

    def _apply_verify_dot(self):
        """Paint the dot for the entry's recorded state and explain it on hover."""
        state = getattr(self._entry, "verify_state", "") or ""
        colour_key, msg_key = self._VERIFY_LOOK.get(state, self._VERIFY_LOOK[""])
        self._verify_dot.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;padding:0;"
            f"font-size:{scaled(11, self)}px;color:{palette(colour_key)};}}"
        )
        tip = t(msg_key)
        detail = getattr(self._entry, "verify_detail", "")
        if detail and state not in ("", "ok"):
            tip += f"\n{detail}"
        when = getattr(self._entry, "verify_at", "")
        if when:
            from core import to_local_dt
            from i18n import format_dt
            dt = to_local_dt(when)
            if dt is not None:
                tip += "\n" + t("backups.verify_checked_at",
                                when=format_dt(dt, "%d %b %Y  %H:%M"))
        else:
            tip += "\n" + t("backups.verify_click")
        self._verify_dot.setToolTip(tip)

    def _on_verify_clicked(self):
        """Check just this backup. Shallow: opening the archive and checking
        every member's CRC is what catches a broken file, and it is quick
        enough to run inline without freezing the list. Throttled to once
        per minute per row — a spam click on the dot re-opens the same zip."""
        from time import monotonic
        now = monotonic()
        if now - getattr(self, "_last_verify_mono", 0.0) < 60.0:
            return
        self._last_verify_mono = now
        from core.backup import get_backup_manager
        self._verify_dot.setEnabled(False)
        try:
            state, detail = get_backup_manager().verify_backup(
                self._entry.backup_id, deep=False)
            self._entry.verify_state = state
            self._entry.verify_detail = detail
            from datetime import datetime
            self._entry.verify_at = datetime.utcnow().isoformat()
            self._apply_verify_dot()
        except Exception as e:
            logger.warning(f"Verify failed for {self._entry.backup_id}: {e}")
        finally:
            self._verify_dot.setEnabled(True)

    def _on_open_backup_folder(self):
        """Open the backup folder for this entry.

        For cloud-only / provider-origin entries: opens the provider's local
        sync folder (e.g. OneDrive\\SaveSync\\backup\\GameName) when available.
        Falls back to SaveSync's own backup directory.
        """
        from core.constants import BACKUP_DIR

        target: Optional[str] = None
        origin = getattr(self._entry, "origin", "local")

        if origin and origin != "local":
            try:
                from sync import get_orchestrator
                provider = get_orchestrator().provider
                if provider and provider.PROVIDER_ID == origin:
                    provider_root = getattr(provider, "_root", None)
                    if provider_root and Path(provider_root).exists():
                        root = Path(provider_root)
                        # For cloud-only entries: game_id IS the provider folder name.
                        # For local entries synced to provider: derive from zip_path.
                        game_folder: Optional[str] = None
                        if self._cloud_only or not self._entry.zip_path:
                            game_folder = self._entry.game_id
                        else:
                            zip_p = Path(self._entry.zip_path)
                            try:
                                rel = zip_p.relative_to(BACKUP_DIR)
                                game_folder = rel.parts[0] if rel.parts else None
                            except ValueError:
                                pass

                        if game_folder:
                            candidate = root / "SaveSync" / "backup" / game_folder
                            target = str(candidate) if candidate.exists() else str(root / "SaveSync")
                        else:
                            target = str(root)
            except Exception as e:
                logger.debug(f"Could not resolve provider folder for open: {e}")

        if not target:
            if self._entry.zip_path:
                zip_p = Path(self._entry.zip_path)
                target = str(zip_p.parent) if zip_p.parent.exists() else str(BACKUP_DIR)
            else:
                target = str(BACKUP_DIR)

        if not Path(target).exists():
            target = str(BACKUP_DIR)

        open_in_file_manager(target)


from ui.widgets.search_inputs import (GhostClearableLineEdit, _SearchCombo,
                                      _SuggestPopup)  # clearable ghost for title search


class BackupsPage(PageScrollMixin, QWidget, ThemedMixin):
    restore_requested = Signal(str, str)
    backup_requested  = Signal(str)
    backup_all_requested = Signal(object)  # list[str] game ids
    manual_paths_requested = Signal()
    # Manual "Verifica Backup" sweep — the main window mirrors these into the
    # sidebar notice so the UI never looks frozen while it runs.
    verify_started = Signal(int)               # total
    verify_progress = Signal(int, int, str)    # done, total, name
    verify_finished = Signal(str)              # result message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_game_id: Optional[str] = None
        self._game_filter_text: str = ""          # current text-filter for game search
        self._game_combo_updating: bool = False   # guard against recursive textChanged
        # Collapsible per-title sections: which titles are expanded, and the
        # current page of the paginated title list (PAGE_SIZE per page).
        self._expanded_titles: set = set()
        self._backups_page_num: int = 1
        # Dynamic children tracked for in-place theme restyle (reset on every
        # _refresh_list). Group-header buttons register on THIS page's own
        # ThemedMixin registry via self._sty; each BackupRow owns a SEPARATE
        # registry, so live rows are cascaded explicitly in refresh_styles().
        self._backup_rows: list = []
        # backup_id → (state, detail, when) from the last check run, for rows
        # that were not on screen when it happened (collapsed titles build
        # their rows only when opened). Cleared on every _refresh_list, where
        # the entries are re-read from the index and already carry it.
        self._verify_fresh: dict[str, tuple] = {}
        # Cache for _load_games(): game_ids that have cloud-only backups
        # (not yet downloaded locally), keyed to the "provider_only" filter.
        # None = not yet computed for this filter activation; invalidated
        # in _on_source_filter_changed() so it's never more than one
        # filter-switch stale, and is never recomputed on every keystroke.
        self._provider_only_extra_game_ids: Optional[set] = None
        self._provider_only_phantom_folders: Optional[dict] = None
        # Debounce timer: fires 120 ms after the last keystroke to avoid
        # rebuilding the combo + list on every character typed.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._do_game_search)
        # First list build waits until the page is shown so busy_over can cover
        # it (hidden rebuilds have nothing to dim).
        self._pending_initial_load = True
        self._build()
        self._connect_signals()

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_initial_load:
            self._pending_initial_load = False
            QTimer.singleShot(0, self._load_games)

    def ensure_loaded(self):
        """Kick the first list build without blocking the page switch."""
        if self._pending_initial_load:
            self._pending_initial_load = False
            QTimer.singleShot(0, self._load_games)

    def on_page_leave(self):
        """Stop any deferred busy on leaving page."""
        self._stop_list_busy()

    def wipe_and_reload(self):
        """Re-arm the initial load and immediately wipe off-screen widgets
        (called by the overview refresh button)."""
        self._refresh_gen = getattr(self, "_refresh_gen", 0) + 1
        self._group_insert_queue = []
        self._stop_list_busy()
        if getattr(self, "_page_size_combo", None) is not None:
            self._page_size_combo.setParent(self)
        if hasattr(self, "_list_layout"):
            while self._list_layout.count():
                item = self._list_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        self._pending_initial_load = True
        if self.isVisible():
            QTimer.singleShot(0, self._load_games)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(scaled(20, self, min_px=14), scaled(16, self, min_px=10), scaled(20, self, min_px=14), scaled(16, self, min_px=10))
        root.setSpacing(scaled(12, self, min_px=8))

        header_row = QHBoxLayout()
        header_row.setSpacing(scaled(8, self, min_px=6))
        self._header = QLabel(t("backup.title"))
        self._header.setObjectName("page_header")
        header_row.addWidget(self._header)

        # Caduceus next to the page title — a check on the archives that
        # title names, not another action beside "Backup now". Progress and
        # outcome sit in the label beside it; the per-backup dots update too.
        self._verify_btn = QPushButton("⚕️")
        self._verify_btn.setObjectName("toolbar_icon_btn")
        self._verify_btn.setFixedSize(scaled(30, self), scaled(30, self))
        self._verify_btn.clicked.connect(self._on_verify_all)
        self._refresh_verify_tooltip()
        header_row.addWidget(self._verify_btn)
        self._verify_status_tone = "text_hint"
        self._verify_status = QLabel("")
        self._verify_status.setVisible(False)
        self._sty(self._verify_status, lambda: (
            f"color:{palette(self._verify_status_tone)};font-size:{scaled(12, self)}px;"))
        header_row.addWidget(self._verify_status)
        header_row.addStretch()

        # Add save folders that have no executable behind them — the only way
        # into the backup pipeline for saves SaveSync never detected.
        # U+2795 rather than a plain "+": the emoji is drawn in colour by the
        # system font, so it reads at a glance next to the text buttons —
        # a bare glyph in the muted icon-button colour did not.
        self._add_paths_btn = QPushButton("➕")
        # Same chrome as the "Open folder" / "Backup now" buttons it sits
        # next to — as a bare glyph it disappeared into the header.
        self._add_paths_btn.setObjectName("toolbar_icon_btn")
        self._add_paths_btn.setFixedSize(scaled(30, self), scaled(30, self))
        self._add_paths_btn.setToolTip(t("manual_path.button_tooltip"))
        self._add_paths_btn.clicked.connect(self._on_add_manual_paths)
        header_row.addWidget(self._add_paths_btn)

        self._open_folder_btn = QPushButton(t("buttons.open_folder"))
        self._open_folder_btn.setFixedHeight(scaled(30, self))
        self._open_folder_btn.setToolTip(t("tooltips.open_save_folder"))
        self._open_folder_btn.clicked.connect(self._on_open_save_folder)
        lock_min_size(self._open_folder_btn, w=scaled(130, self, min_px=110))
        header_row.addWidget(self._open_folder_btn)

        self._backup_now_btn = QPushButton(t("buttons.backup_now"))
        self._backup_now_btn.setObjectName("primary_btn")
        self._backup_now_btn.setToolTip(t("backups.backup_now_tooltip"))
        self._backup_now_btn.clicked.connect(self._on_backup_now)
        self._backup_now_btn.setEnabled(True)    # always active — falls back to backup-all
        lock_min_size(self._backup_now_btn, w=scaled(110, self, min_px=95))
        header_row.addWidget(self._backup_now_btn)
        root.addLayout(header_row)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(scaled(8, self, min_px=6))
        sel_row.setContentsMargins(0, 0, 0, 0)

        # ── Game search field ─────────────────────────────────────────────
        # Editable combo. Typing drives a CUSTOM suggestions popup (see
        # _SuggestPopup): the combo's own model is never rebuilt per
        # keystroke, so the filtered titles stay put while typing. The
        # native dropdown (arrow click) is the only place the "Tutti i
        # titoli" reset entry exists. The currently highlighted suggestion
        # is mirrored in the line edit as a painted, NON-physical
        # completion hint (see GhostClearableLineEdit) — Enter or ↑/↓ + Enter (or
        # a click on the popup row) confirms it; clicking away keeps the
        # typed text as a placeholder and the filter active.
        self._game_combo = _SearchCombo()
        self._game_combo.setMinimumWidth(scaled(130, self, min_px=100))
        self._game_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._game_combo.setMinimumHeight(scaled(32, self))
        self._game_combo.setEditable(True)
        self._game_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._game_combo.setLineEdit(GhostClearableLineEdit())
        # Kill the built-in completer Qt installs on every editable combo:
        # its INLINE completion physically writes the first alphabetical
        # match into the buffer while typing — exactly the "physical text"
        # behavior this field must never have (it also broke the typing
        # popup, which then filtered on the auto-completed title).
        self._game_combo.setCompleter(None)
        # The global QSS styles QLineEdit with its own border+padding; inside
        # the (already padded/bordered) combo frame that squashed the field
        # and clipped the typed text — make the embedded editor bare.
        # Keep the clear button's own stylesheet (do not wipe QToolButton).
        self._game_combo.lineEdit().setStyleSheet(
            "QLineEdit{background:transparent;border:none;padding:0;margin:0;}"
        )
        self._game_combo.lineEdit().setPlaceholderText(t('backups.search_game'))
        # Opening the native dropdown dismisses the typing popup.
        self._game_combo.on_native_popup = self._hide_suggest_popup
        # Fallback arrow routing: if ↑/↓ ever reach the combo instead of
        # being consumed by the line-edit filter below (filter ordering is
        # environment dependent), they still navigate the typing popup.
        self._game_combo.on_nav_key = self._on_search_nav_key

        # (clean_name, item_data) candidates for the typing popup — rebuilt
        # by _load_games alongside the combo model.
        self._search_candidates: list[tuple[str, str]] = []
        self._suggest_popup = _SuggestPopup(self)
        self._suggest_popup.item_activated.connect(self._on_suggest_clicked)

        self._game_combo.lineEdit().textChanged.connect(self._on_game_text_changed)
        # Explicit selection via the combo's own dropdown-arrow list.
        self._game_combo.activated.connect(self._on_game_activated)
        # Enter confirms the highlighted suggestion (or, without a popup,
        # matches the typed text; with no match the typed text itself
        # becomes the placeholder while the filter stays active).
        self._game_combo.lineEdit().returnPressed.connect(self._on_game_search_confirmed)
        self._game_combo.lineEdit().installEventFilter(self)
        sel_row.addWidget(self._game_combo, 3)

        # ── Source-type filter ────────────────────────────────────────────
        self._origin_combo = QComboBox()
        self._origin_combo.setMinimumWidth(scaled(100, self, min_px=90))
        self._origin_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._origin_combo.currentIndexChanged.connect(self._on_source_filter_changed)
        sel_row.addWidget(self._origin_combo, 2)

        # ── Provider sub-selector (visible only when "Solo origine provider") ─
        self._provider_sub_combo = QComboBox()
        self._provider_sub_combo.setMinimumWidth(scaled(100, self, min_px=90))
        self._provider_sub_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._provider_sub_combo.currentIndexChanged.connect(self._on_provider_sub_changed)
        self._provider_sub_combo.setVisible(False)
        sel_row.addWidget(self._provider_sub_combo, 2)

        # Populate both combos (widgets must exist first)
        self._rebuild_origin_filter()

        self._summary_lbl = QLabel()
        self._summary_lbl.setObjectName("backup_summary")
        self._summary_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._summary_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        lock_min_size(self._summary_lbl, w=scaled(130, self, min_px=110))
        sel_row.addWidget(self._summary_lbl, 0)
        root.addLayout(sel_row)

        # Lives on the (top) pager row with the page numbers — see _refresh_list.
        from ui.widgets.page_size import PageSizeCombo, SCOPE_BACKUPS
        self._page_size_combo = PageSizeCombo(
            SCOPE_BACKUPS, self._on_page_size_changed)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_widget = QWidget()
        self._list_widget.setObjectName("transparent_bg")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)

        self._empty_lbl = QLabel(t("backup.no_backups"))
        self._empty_lbl.setObjectName("backup_empty")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_layout.addWidget(self._empty_lbl)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_widget)
        root.addWidget(self._scroll, 1)
        self._register_page_scroll(self._scroll, list_content=True)

    def _connect_signals(self):
        lib = get_library()
        lib.game_added.connect(self._on_lib_changed)
        lib.game_removed.connect(self._on_lib_changed)
        lib.bulk_finished.connect(self._on_bulk_finished)
        get_backup_manager().backup_created.connect(self._on_backup_created)
        get_backup_manager().backup_deleted.connect(self._on_backup_deleted)
        from sync import get_orchestrator
        orch = get_orchestrator()
        orch.provider_changed.connect(self._on_provider_state_changed)
        orch.providers_updated.connect(self._on_provider_state_changed)
        orch.sync_finished.connect(self._on_sync_finished)

    def _on_bulk_finished(self):
        if not getattr(self, "_pending_initial_load", False):
            self._load_games()

    def _on_provider_state_changed(self, _pid: str = ""):
        """Refresh origin filter when provider connects/disconnects."""
        self._rebuild_origin_filter()
        if not getattr(self, "_pending_initial_load", False):
            self._load_games()

    def _on_sync_finished(self, _game_id: str, _result):
        """Refresh after sync completes (new backups may have been created/downloaded)."""
        self._rebuild_origin_filter()
        if not getattr(self, "_pending_initial_load", False):
            self._load_games()

    def _on_lib_changed(self, arg):
        # If a game was removed and it was the selected game, clear selection.
        # setCurrentIndex() must stay signal-blocked here too — same cascade
        # risk as in _load_games() (unblocked → textChanged → stale filter text
        # left in self._game_filter_text → _do_game_search picks it up later).
        if isinstance(arg, str) and self._selected_game_id == arg:
            le = self._game_combo.lineEdit()
            self._game_combo.blockSignals(True)
            if le:
                le.blockSignals(True)
            try:
                self._game_combo.setCurrentIndex(0)
            finally:
                if le:
                    le.blockSignals(False)
                self._game_combo.blockSignals(False)
            self._selected_game_id = None
            self._game_filter_text = ""
        if not getattr(self, "_pending_initial_load", False):
            self._load_games()

    def _on_backup_created(self, _):
        try:
            if get_library()._in_bulk():
                return
        except Exception:
            pass
        self._rebuild_origin_filter()
        if not getattr(self, "_pending_initial_load", False):
            self._refresh_list()

    def _on_backup_deleted(self, _backup_id: str):
        """A backup went away — from ANYWHERE.

        Only this page's own delete button used to refresh the listing, so
        every other path left it showing archives that no longer exist:
        retention pruning after a new backup, the auto-scan dialog's cleanup,
        a sync prune, and the zip-existence check dropping rows whose archive
        is gone. Riding the signal instead of the button covers all of them,
        and _load_games() (which ends in _refresh_list) also rebuilds the
        title search — the counts and candidates come from the same index.
        """
        try:
            if get_library()._in_bulk():
                return
        except Exception:
            pass
        if getattr(self, "_pending_initial_load", False):
            return
        self._load_games()

    def disconnect_signals(self):
        try:
            get_library().game_added.disconnect(self._on_lib_changed)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_removed.disconnect(self._on_lib_changed)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().bulk_finished.disconnect(self._on_bulk_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            get_backup_manager().backup_created.disconnect(self._on_backup_created)
        except (RuntimeError, TypeError):
            pass
        try:
            get_backup_manager().backup_deleted.disconnect(self._on_backup_deleted)
        except (RuntimeError, TypeError):
            pass
        try:
            from sync import get_orchestrator
            orch = get_orchestrator()
            orch.provider_changed.disconnect(self._on_provider_state_changed)
            orch.providers_updated.disconnect(self._on_provider_state_changed)
            orch.sync_finished.disconnect(self._on_sync_finished)
        except (RuntimeError, TypeError):
            pass

    def _rebuild_origin_filter(self):
        """Rebuild the 5-option source filter.  Provider selection is handled
        separately by _rebuild_provider_sub_combo."""
        self._origin_combo.blockSignals(True)
        current = self._origin_combo.currentData()
        self._origin_combo.clear()
        self._origin_combo.addItem(t("backups.filter_all"),          "all")
        self._origin_combo.addItem(t("backups.filter_local_only"),   "local_only")
        self._origin_combo.addItem(t("backups.filter_local_sync"),   "local_sync")
        self._origin_combo.addItem(t("backups.filter_archive_only"), "archive_only")
        self._origin_combo.addItem(t("backups.provider_only"),       "provider_only")

        from sync import get_orchestrator
        has_provider = get_orchestrator().is_online()

        # Disable "provider only" when no provider connected
        _item3 = self._origin_combo.model().item(4)
        if _item3:
            if has_provider:
                _item3.setFlags(_item3.flags() | Qt.ItemFlag.ItemIsEnabled)
            else:
                _item3.setFlags(_item3.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                if current == "provider_only":
                    current = "all"

        restored = False
        if current:
            for i in range(self._origin_combo.count()):
                if self._origin_combo.itemData(i) == current:
                    self._origin_combo.setCurrentIndex(i)
                    restored = True
                    break
        if not restored:
            self._origin_combo.setCurrentIndex(0)
        self._origin_combo.blockSignals(False)

        self._rebuild_provider_sub_combo()

    def _rebuild_provider_sub_combo(self):
        """Rebuild the provider sub-selector shown under 'Solo origine provider'."""
        filt = self._get_origin_filter()
        is_provider_only = filt == "provider_only"
        self._provider_sub_combo.setVisible(is_provider_only)
        if not is_provider_only:
            return

        from sync import get_orchestrator
        orch = get_orchestrator()
        provider = orch.provider if orch.is_online() else None

        self._provider_sub_combo.blockSignals(True)
        cur = self._provider_sub_combo.currentData()
        self._provider_sub_combo.clear()
        self._provider_sub_combo.addItem(t("backups.filter_all_providers"), "__all__")
        if provider:
            pid = provider.PROVIDER_ID
            pname = ORIGIN_LABELS.get(pid, f"☁ {pid}")
            self._provider_sub_combo.addItem(pname, pid)
        # Restore selection
        for i in range(self._provider_sub_combo.count()):
            if self._provider_sub_combo.itemData(i) == cur:
                self._provider_sub_combo.setCurrentIndex(i)
                break
        self._provider_sub_combo.blockSignals(False)

    def _get_origin_filter(self) -> str:
        """Return current source filter key: 'all', 'local_only', 'local_sync',
        'archive_only', 'provider_only', '__sep__', or a provider_id string."""
        return self._origin_combo.currentData() or "all"

    def _matches_origin_filter(self, entry) -> bool:
        filt      = self._get_origin_filter()
        origin    = getattr(entry, "origin", "local")
        synced_to = entry.cloud_metadata.get("synced_to", [])

        if filt in ("all", "__sep__"):
            return True
        if filt == "local_only":
            return origin == "local" and not synced_to
        if filt == "local_sync":
            return origin == "local"
        if filt == "archive_only":
            # Same bucket as the donut's "Archivi" slice: backups with NO
            # library game — Aggiungi-percorso archives (orphan flag) and
            # leftovers after a game was removed (game_id no longer in the
            # library). Cloud phantoms enter with their folder name as
            # game_id, which is never a library id, so they match too.
            if origin == "local":
                bm = get_backup_manager()
                if bm.is_orphan_entry(entry):
                    return True
                gid = getattr(entry, "game_id", None)
                if not gid:
                    return True
                lib_ids = getattr(self, "_archive_lib_ids", None)
                if lib_ids is None:
                    lib_ids = bm.library_game_ids()
                    self._archive_lib_ids = lib_ids
                return gid not in lib_ids
            return True  # cloud-phantom entries are archive-only by nature
        if filt == "provider_only":
            return False  # cloud-only entries injected separately in _refresh_list
        # Provider-specific filter (a provider_id like "google_drive"):
        # show local backups that were synced to/from that provider
        return origin == filt or filt in synced_to

    def _load_games(self, text_filter: str = ""):
        """Repopulate game combo respecting source-filter and optional text search."""
        self._archive_lib_ids = None  # library may have changed; recompute on demand
        mgr = get_backup_manager()
        all_backups = mgr.get_all_backups()
        filt = self._get_origin_filter()

        from sync import get_orchestrator
        provider = get_orchestrator().provider
        provider_id = provider.PROVIDER_ID if provider else None

        # Categorise game IDs for each filter bucket. Definitions (exact spec):
        #   local_only   = has a local backup that has NEVER been synced anywhere
        #   local_sync   = has ANY local backup at all, synced or not
        #                  (local_only is always a subset of this)
        #   provider_only = has a CLOUD-side backup but ZERO local backups —
        #                  mutually exclusive with local_sync BY DEFINITION:
        #                  a game with even one local backup entry can never
        #                  qualify, whether or not that local backup has also
        #                  been synced to a provider. This differs from the
        #                  previous implementation, which added a game here
        #                  if ANY of its backups merely mentioned the
        #                  provider (origin or synced_to) — that let a mixed
        #                  game (has local backups, one of which happens to
        #                  also be synced) show up under "Solo origine
        #                  provider" too, when it should only ever appear
        #                  under "Locale + sync".
        counts:          dict[str, int] = {}
        local_only_ids:  set[str]       = set()
        local_sync_ids:  set[str]       = set()   # ANY local backup, synced or not
        provider_ids:    set[str]       = set()   # filled in below, post-loop

        for b in all_backups:
            counts[b.game_id] = counts.get(b.game_id, 0) + 1
            origin    = getattr(b, "origin", "local")
            synced_to = b.cloud_metadata.get("synced_to", [])
            if origin == "local":
                local_sync_ids.add(b.game_id)
                if not synced_to:
                    local_only_ids.add(b.game_id)

        # A game whose backups exist ONLY on the cloud (never downloaded —
        # so it has no entry at all in mgr.get_all_backups(), the local
        # index) would never appear in provider_ids above, and so could
        # never be selected here under "Solo origine provider" — even
        # though _refresh_list()'s own aggregate view already knows how to
        # fetch and display exactly that backup once a game IS selected.
        # This cache closes that gap: a single list_all_cloud_backups()
        # call (not one call per keystroke — computed once per filter
        # activation, invalidated in _on_source_filter_changed) checked
        # against library folder names. Also computed under "all", since
        # "Tutte le origini" should genuinely include provider-exclusive
        # titles too, matching the same extension made in _refresh_list().
        phantom_folders: dict[str, int] = {}   # folder_name -> backup count
        if filt in ("provider_only", "archive_only", "all") and provider is not None:
            if self._provider_only_extra_game_ids is None:
                extra: set[str] = set()
                phantoms: dict[str, int] = {}
                try:
                    all_cloud = provider.list_all_cloud_backups()
                    cloud_folders = {f.lower(): f for f in all_cloud.keys()}
                    from core.constants import (get_install_folder_name,
                                                get_folder_name_for_save,
                                                version_insensitive_slug)
                    # Secondary index with version/build tokens ignored —
                    # MyGame-v0.5 (remote) still matches MyGame v0.8 (local).
                    cloud_norm = {version_insensitive_slug(f): f.lower()
                                  for f in all_cloud.keys()}
                    matched_folders: set[str] = set()
                    for g in get_library().all_games():
                        candidate_folders = {
                            get_install_folder_name(g.exe_path or "", g.name, g.id, g.computed_folder_name).lower()
                        }
                        for hn in g.name_history:
                            candidate_folders.add(get_folder_name_for_save(hn, g.exe_path or "", g.id).lower())
                        for _fh in (g.folder_history or []):
                            candidate_folders.add(_fh.lower())
                        hit = candidate_folders & cloud_folders.keys()
                        if not hit:
                            hit = {cloud_norm[version_insensitive_slug(c)]
                                   for c in candidate_folders
                                   if version_insensitive_slug(c) in cloud_norm}
                        if hit:
                            matched_folders.update(hit)
                            # Mutual exclusivity with local_sync: a game
                            # with ANY local backup (synced or not) never
                            # qualifies for provider_only, no matter how
                            # many of its backups also happen to exist on
                            # the cloud — that's exactly what "Locale +
                            # sync" is for. Only a genuinely local-empty
                            # game with a cloud match belongs here.
                            if g.id not in local_sync_ids:
                                extra.add(g.id)
                    # Any cloud folder NOT matched to a local library game is
                    # a true phantom — has no library game_id at all, so the
                    # folder name itself doubles as the combo's itemData.
                    for folder_lower, folder_original in cloud_folders.items():
                        if folder_lower not in matched_folders:
                            phantoms[folder_original] = len(all_cloud.get(folder_original, []))
                except Exception as e:
                    logger.debug(f"_load_games: provider_only combo pre-check failed: {e}")
                self._provider_only_extra_game_ids = extra
                self._provider_only_phantom_folders = phantoms
            provider_ids = provider_ids | self._provider_only_extra_game_ids
            phantom_folders = self._provider_only_phantom_folders or {}

        tf = text_filter.lower()

        # Block BOTH the combo AND its lineEdit to prevent textChanged from firing
        # when addItem() auto-selects the first entry and updates the lineEdit text.
        # Without this, adding the first item causes textChanged → _on_game_text_changed
        # → _load_games() recursion that crashes PySide6 silently.
        self._game_combo.blockSignals(True)
        _le = self._game_combo.lineEdit()
        if _le:
            _le.blockSignals(True)
        selection_cleared = False
        try:
            self._game_combo.clear()
            self._search_candidates = []
            # First item: reset / show all — present in the NATIVE dropdown
            # only; the typing popup never offers it.
            self._game_combo.addItem(t('backups.all_games'), "__all__")
            for g in sorted(get_library().all_games(), key=lambda x: x.name.lower()):
                # Archive only = backups with NO library game: list no
                # library titles at all — just orphan/phantom folders below.
                if filt == "archive_only":
                    continue
                # Source/origin filter
                if filt == "local_only" and g.id not in local_only_ids:
                    continue
                if filt == "local_sync" and g.id not in local_sync_ids:
                    continue
                if filt == "provider_only" and provider_id and g.id not in provider_ids:
                    continue
                if provider_id and filt == provider_id and g.id not in provider_ids:
                    continue
                # Typing-popup candidates: always the FULL origin-filtered
                # set (the popup applies the text filter itself), so its
                # entries never vanish when this model is rebuilt.
                # Exclude games with 0 backups: after deletion a rebuild is
                # triggered, and stale candidates would otherwise remain
                # searchable until the next full restart.
                n = counts.get(g.id, 0)
                if n == 0:
                    continue
                self._search_candidates.append((g.name, g.id))
                # Native dropdown items: respect the active text filter —
                # with "dark" typed, "My Game" must not be selectable there.
                if tf and tf not in g.name.lower():
                    continue
                label = f"{g.name}  ({n})"
                self._game_combo.addItem(label, g.id)


            # Phantom entries: cloud-only backups with no matching local
            # library game at all — selectable using the folder name itself
            # as itemData (there's no library game_id to use). Cloud icon
            # prefix distinguishes them visually from real library titles.
            for folder_name, backup_count in sorted(phantom_folders.items(), key=lambda kv: kv[0].lower()):
                self._search_candidates.append((folder_name, folder_name))
                if tf and tf not in folder_name.lower():
                    continue
                label = f"\u2601 {folder_name}  ({backup_count})" if backup_count else f"\u2601 {folder_name}"
                self._game_combo.addItem(label, folder_name)

            # Local orphan archives (Aggiungi percorso): same index as cloud,
            # no library game — folder name is the selector.
            if filt in ("all", "local_only", "local_sync", "archive_only", ""):
                try:
                    for folder_name, backup_count in sorted(
                            (mgr.orphan_folders() or {}).items(),
                            key=lambda kv: kv[0].lower()):
                        # Skip if already listed as a cloud phantom
                        if folder_name in phantom_folders:
                            continue
                        if any(self._game_combo.itemData(i) == folder_name
                               for i in range(self._game_combo.count())):
                            continue
                        self._search_candidates.append((folder_name, folder_name))
                        if tf and tf not in folder_name.lower():
                            continue
                        label = (f"{folder_name}  ({backup_count})"
                                 if backup_count else folder_name)
                        self._game_combo.addItem(label, folder_name)
                except Exception:
                    logger.debug("orphan folder combo failed", exc_info=True)

            # Re-select the previously chosen game, if it's still in the list.
            # This MUST stay inside the blocked scope: setCurrentIndex() on an
            # editable combo writes the item's label straight into the line-edit,
            # which — if signals were unblocked — would re-enter
            # _on_game_text_changed and (a) wipe _selected_game_id back to None
            # and (b) leave that label as real, non-placeholder text. That was
            # the root cause of selections "sticking" as literal written text.
            if self._selected_game_id:
                for i in range(self._game_combo.count()):
                    if self._game_combo.itemData(i) == self._selected_game_id:
                        self._game_combo.setCurrentIndex(i)
                        break
                else:
                    # The selected title is no longer in the combo — its last
                    # backup was just deleted, and with it the game's folder,
                    # so it dropped out of the counts above. Leaving the id
                    # set kept the page filtering by a title that is gone:
                    # the listing came up empty with no way back, and the
                    # placeholder still named it. Fall back to "all titles".
                    self._selected_game_id = None
                    self._game_filter_text = ""
                    self._backups_page_num = 1
                    selection_cleared = True
                    self._game_combo.setCurrentIndex(0)
                    if _le:
                        _le.setPlaceholderText(t('backups.search_game'))

            # Whatever Qt physically wrote into the line-edit as a side effect
            # of addItem()'s auto-select-first-item behaviour or the
            # setCurrentIndex() above must not be shown as real text. If the
            # user is mid-typing (field focused, live filter present), their
            # typed text is restored verbatim — a background rebuild (backup
            # created, provider change) must never blank the search box.
            if _le:
                if self._game_filter_text and _le.hasFocus():
                    _le.setText(self._game_filter_text)
                    _le.setCursorPosition(len(self._game_filter_text))
                else:
                    _le.clear()
        finally:
            if _le:
                _le.blockSignals(False)
            self._game_combo.blockSignals(False)

        if selection_cleared:
            # The button's tooltip says "check this title" vs "check all",
            # and the selection it named is gone.
            self._refresh_verify_tooltip()

        # Candidate list changed — refresh the typing popup in place (it is
        # driven by _search_candidates, not by the combo model, so entries
        # stay visible while typing).
        if self._suggest_popup.isVisible():
            self._update_suggest_popup()

        self._refresh_list()

    def _clean_item_label(self, item_text: str) -> str:
        """Strip a combo item's display-only decoration — the trailing
        "  (N)" count and, for provider-only phantom entries, the leading
        "☁ " icon — down to the bare comparable/placeholder name.

        Used everywhere a label needs to be matched against typed text or
        shown as a placeholder: without stripping the cloud-icon prefix, a
        phantom title's label (e.g. "☁ SomeGame  (2)") never matches
        typed text like "Some" via startswith(), since the string
        literally starts with the icon character instead — which is why
        the inline suggestion previously never fired for provider-only
        titles even though they were already present in the combo.
        """
        clean = item_text.split("  (")[0].strip()
        if clean.startswith("\u2601"):
            clean = clean[1:].strip()
        return clean

    @staticmethod
    def _point_in_widget(global_point, widget) -> bool:
        """True if *global_point* falls inside *widget*'s visible screen rect."""
        try:
            if widget is None or not widget.isVisible():
                return False
            top_left = widget.mapToGlobal(widget.rect().topLeft())
            return QRect(top_left, widget.size()).contains(global_point)
        except RuntimeError:
            return False

    def eventFilter(self, obj, event):
        """Keyboard + focus routing for the search line edit.

        - ↑/↓ move the highlight in the typing popup ONLY while it is already
          open (they never open it, never touch an active placeholder filter,
          and never drive the native combo list). The line edit text is NEVER
          touched, only the painted ghost hint follows the highlighted title.
        - Esc closes the popup.
        - FocusOut / a click anywhere outside the field+popup: does NOT confirm
          — the typed text becomes a placeholder, the popup closes, and the
          filter stays active (_on_game_search_blurred).
        """
        # App-level guard (active while the search field has focus): a mouse
        # press anywhere outside the field/popup collapses the typed text to
        # a placeholder — regardless of whether the suggestion popup is open
        # (non-matching text keeps it closed) or the clicked widget takes
        # keyboard focus (no FocusOut would fire).
        if (event.type() == QEvent.Type.MouseButtonPress
                and getattr(self, "_suggest_click_filter", False)
                and self._game_combo.lineEdit().hasFocus()):
            try:
                gp = event.globalPosition().toPoint()
            except AttributeError:
                gp = event.globalPos()
            if (not self._point_in_widget(gp, self._game_combo)
                    and not self._point_in_widget(gp, self._suggest_popup)):
                self._on_game_search_blurred()
                self._game_combo.lineEdit().clearFocus()
            # Never consume — the click must still reach its real target.

        if obj is self._game_combo.lineEdit():
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    # Only navigate an already-open popup. Consume the key
                    # regardless so a bare editable combo can't cycle its
                    # items into the buffer (which would wipe the placeholder
                    # filter) and the native list isn't driven by arrows.
                    self._on_search_nav_key(key)
                    return True
                if key == Qt.Key.Key_Escape and self._suggest_popup.isVisible():
                    self._hide_suggest_popup()
                    return True
            elif event.type() == QEvent.Type.FocusIn:
                self._install_click_filter()
            elif event.type() == QEvent.Type.FocusOut:
                self._on_game_search_blurred()
        return super().eventFilter(obj, event)

    def _on_search_nav_key(self, key):
        """Shared ↑/↓ handling for the typing popup — reached from the
        line-edit event filter AND from the combo-level fallback (see
        _SearchCombo.on_nav_key), so navigation works no matter which
        layer the key lands on."""
        if self._suggest_popup.isVisible():
            self._suggest_popup.move_selection(
                1 if key == Qt.Key.Key_Down else -1)
            self._apply_ghost()
        else:
            logger.debug("search nav key with no visible typing popup")

    def _current_suggestions(self) -> list[tuple[str, str]]:
        """(clean_name, item_data) candidates matching the typed filter —
        contains-match, case-insensitive. Never includes 'Tutti i titoli':
        the reset entry only exists in the user-opened native dropdown."""
        tf = self._game_filter_text.lower()
        if not tf:
            return []
        return [(name, data) for name, data in self._search_candidates
                if tf in name.lower()]

    def _update_suggest_popup(self):
        """Show/refresh the typing popup under the search field."""
        matches = self._current_suggestions()
        if not matches:
            self._hide_suggest_popup()
            return
        if not self._game_combo.lineEdit().hasFocus():
            # Never OPEN over an unfocused field; a real blur is already
            # handled by the FocusOut branch of eventFilter. Hiding here too
            # made transient focus flicker during background rebuilds yank a
            # visible popup away mid-navigation.
            return
        self._suggest_popup.set_items([name for name, _ in matches])
        pos = self._game_combo.mapTo(self, QPoint(0, self._game_combo.height()))
        self._suggest_popup.move(pos)
        self._suggest_popup.setFixedWidth(max(self._game_combo.width(), scaled(280, self)))
        self._suggest_popup.show()
        self._suggest_popup.raise_()
        # Defensive: FocusIn normally installed this already.
        self._install_click_filter()
        self._apply_ghost()

    def _hide_suggest_popup(self):
        if self._suggest_popup.isVisible():
            self._suggest_popup.hide()
        le = self._game_combo.lineEdit()
        if hasattr(le, "set_ghost"):
            le.set_ghost("")

    def _install_click_filter(self):
        """Watch app-wide mouse presses while the search field is focused, so
        a click anywhere outside it collapses typed text to a placeholder even
        when the target widget takes no keyboard focus (no FocusOut) and even
        with no suggestion popup open (non-matching text)."""
        if not getattr(self, "_suggest_click_filter", False):
            app = QApplication.instance()
            if app:
                app.installEventFilter(self)
                self._suggest_click_filter = True

    def _remove_click_filter(self):
        if getattr(self, "_suggest_click_filter", False):
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self)
            self._suggest_click_filter = False

    def _apply_ghost(self):
        """Mirror the highlighted popup title into the line edit as a
        painted (non-physical) completion hint: typed 'exam' + highlighted
        'Example Game 1' paints 'ple Game 1' after the caret."""
        le = self._game_combo.lineEdit()
        if not hasattr(le, "set_ghost"):
            return
        matches = self._current_suggestions()
        row = self._suggest_popup.current_row()
        typed = le.text()
        if not typed or not self._suggest_popup.isVisible() or not (0 <= row < len(matches)):
            le.set_ghost("")
            return
        name = matches[row][0]
        if name.lower().startswith(typed.lower()) and len(name) > len(typed):
            le.set_ghost(name[len(typed):])
        else:
            le.set_ghost(f"  —  {name}")

    def _on_suggest_clicked(self, row: int):
        """Popup row clicked — explicit confirmation of that title."""
        matches = self._current_suggestions()
        if 0 <= row < len(matches):
            name, data = matches[row]
            self._select_game(data, name)

    def _select_game(self, gid: str, clean_name: str):
        """Confirm a title: selection set, name shown as placeholder (the
        field stays physically empty and ready to be typed over), popup
        closed, list refreshed."""
        self._search_timer.stop()
        self._hide_suggest_popup()
        self._selected_game_id = gid
        self._backups_page_num = 1
        self._game_combo_updating = True
        try:
            le = self._game_combo.lineEdit()
            le.clear()
            le.setPlaceholderText(clean_name)
        finally:
            self._game_combo_updating = False
        self._refresh_verify_tooltip()
        self._refresh_list()

    def _on_game_text_changed(self, text: str):
        """Typed text changed: update the typing popup immediately (its
        candidate list is NOT rebuilt from the combo model per keystroke,
        so entries never vanish mid-typing) and debounce the heavier
        backup-list refresh."""
        if self._game_combo_updating:
            return
        self._game_filter_text = text.strip()
        self._selected_game_id = None
        self._backups_page_num = 1
        self._refresh_verify_tooltip()
        self._update_suggest_popup()
        self._search_timer.start()   # (re)starts; fires 120 ms after last key

    def _do_game_search(self):
        """Debounced: rebuild the NATIVE dropdown filtered by the typed
        characters (typing 'dark' must remove 'My Game' from the arrow
        dropdown too) and refresh the backup list. The typing popup is
        unaffected: it reads from _search_candidates, which _load_games
        always builds from the FULL origin-filtered set — so its entries
        never vanish mid-typing. The filter stays applied for as long as
        the user-input placeholder is active."""
        if self._selected_game_id:
            return   # a title was confirmed meanwhile — list already refreshed
        self._load_games(text_filter=self._game_filter_text)

    def _on_game_activated(self, index: int):
        """User explicitly selected a game via the combo's own native
        dropdown-arrow list (the only place 'Tutti i titoli' exists)."""
        if index < 0:
            return
        # Stop the debounce timer outright: a keystroke-triggered refresh
        # can still be pending here, and letting it fire after this
        # confirmation would overwrite the placeholder set below.
        self._search_timer.stop()
        self._hide_suggest_popup()

        gid = self._game_combo.itemData(index)
        if not gid or gid == "__all__":
            # "Tutti i titoli" selected — the only explicit reset action;
            # typing fresh over the old placeholder is the other one (see
            # _on_game_text_changed, which always clears _selected_game_id).
            self._selected_game_id = None
            self._game_filter_text = ""
            self._backups_page_num = 1
            self._refresh_verify_tooltip()
            self._game_combo_updating = True
            try:
                self._game_combo.lineEdit().clear()
                self._game_combo.lineEdit().setPlaceholderText(t('backups.search_game'))
                self._load_games()
            finally:
                self._game_combo_updating = False
            self._refresh_list()
            return

        clean = self._clean_item_label(self._game_combo.itemText(index))
        self._select_game(gid, clean)

    def _on_game_search_confirmed(self):
        """Enter pressed in the search field.

        Priority:
        1. A highlighted row in the typing popup → confirm that title
           (the popup auto-highlights the first match, so 'exam' + Enter
           lands on 'Example Game 1' exactly like the painted hint showed).
        2. No popup → match typed text against titles (prefix, then
           contains).
        3. No match at all → the typed text itself becomes the placeholder
           and stays active as the list filter (explicit requirement:
           confirming free text without a matching title must keep it as
           the user-input placeholder).
        """
        if self._game_combo_updating:
            return
        le = self._game_combo.lineEdit()
        typed = le.text().strip()
        if not typed:
            return  # Already empty — placeholder already shown

        matches = self._current_suggestions()
        if self._suggest_popup.isVisible() and matches:
            row = self._suggest_popup.current_row()
            if 0 <= row < len(matches):
                name, data = matches[row]
                self._select_game(data, name)
                return

        # Fallback matching directly against the candidates
        typed_lower = typed.lower()
        confirmed: Optional[tuple] = None
        for name, data in self._search_candidates:
            if name.lower().startswith(typed_lower):
                confirmed = (data, name)
                break
        if not confirmed:
            for name, data in self._search_candidates:
                if typed_lower in name.lower():
                    confirmed = (data, name)
                    break

        if confirmed:
            self._select_game(confirmed[0], confirmed[1])
            return

        # No matching title: keep the typed text as the active filter,
        # shown as a placeholder (field ready to be typed over fresh).
        self._search_timer.stop()
        self._hide_suggest_popup()
        self._game_combo_updating = True
        try:
            le.clear()
            le.setPlaceholderText(typed)
        finally:
            self._game_combo_updating = False
        self._refresh_list()

    def _on_game_search_blurred(self):
        """LineEdit lost focus (clicked elsewhere in the window).

        Deliberately does NOT confirm/autocomplete a game — that's Enter's
        or the popup's job only. The typed text becomes a plain placeholder
        reminder with no selection implied, and the filter (e.g. 'exam' →
        every matching "Example Game" title) stays exactly as it was.
        """
        self._remove_click_filter()
        self._hide_suggest_popup()
        if self._game_combo_updating:
            return
        le = self._game_combo.lineEdit()
        typed = le.text().strip()
        if not typed:
            return  # Already empty — placeholder already shown

        self._game_combo_updating = True
        try:
            le.clear()
            le.setPlaceholderText(typed)
            # self._game_filter_text / self._selected_game_id are left as-is
            # (already correctly reflect "typed, nothing confirmed" from
            # _on_game_text_changed) — no game is picked just by leaving
            # the field, and the list stays filtered to what was typed.
        finally:
            self._game_combo_updating = False

    def _on_provider_sub_changed(self):
        """Provider sub-selector changed — refresh backup list."""
        self._backups_page_num = 1
        self._refresh_list()

    def resizeEvent(self, event):
        # A visible typing popup would keep a stale position — dismiss it.
        super().resizeEvent(event)
        if hasattr(self, "_suggest_popup"):
            self._hide_suggest_popup()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_suggest_popup"):
            self._hide_suggest_popup()
        self._remove_click_filter()

    def _on_source_filter_changed(self):
        """Source filter changed — show/hide sub-combo, clear game selection, reload."""
        self._selected_game_id = None
        self._provider_only_extra_game_ids = None  # invalidate; recomputed on demand
        self._provider_only_phantom_folders = None
        self._backups_page_num = 1
        self._refresh_verify_tooltip()
        self._hide_suggest_popup()
        self._game_combo_updating = True
        try:
            self._game_combo.lineEdit().clear()
            self._game_combo.lineEdit().setPlaceholderText(t('backups.search_game'))
            self._game_filter_text = ""
        finally:
            self._game_combo_updating = False
        self._rebuild_provider_sub_combo()  # show/hide sub-combo based on filter
        self._load_games()
        self._refresh_list()


    def _set_verify_status(self, text: str, tone: str = "text_hint"):
        """Show (or clear) the in-header verify progress/result label."""
        self._verify_status_tone = tone
        if not text:
            self._verify_status.clear()
            self._verify_status.setVisible(False)
            return
        self._verify_status.setText(text)
        self._verify_status.setVisible(True)
        self._verify_status.setStyleSheet(
            f"color:{palette(self._verify_status_tone)};font-size:{scaled(12, self)}px;")

    def _refresh_verify_tooltip(self):
        """Idle checkup tip: all backups, or only the selected game's."""
        if not _safe(self._verify_btn) or not self._verify_btn.isEnabled():
            return
        if self._selected_game_id:
            self._verify_btn.setToolTip(t("backups.verify_game_tooltip"))
        else:
            self._verify_btn.setToolTip(t("backups.verify_all_tooltip"))

    def _verify_idle_tooltip(self) -> str:
        if self._selected_game_id:
            return t("backups.verify_game_tooltip")
        return t("backups.verify_all_tooltip")

    def _show_cooldown_toast(self, seconds: int):
        """Show a toast notification when an action is on cooldown."""
        try:
            mw = self.window()
            if mw is not None and hasattr(mw, '_overlay') and mw._overlay:
                from i18n import t as _t
                msg = _t("notifications.cooldown_active", seconds=seconds)
                mw._overlay.show_notice(msg)
                return
        except Exception:
            pass
        # Fallback: use verify status label
        self._set_verify_status(f"⏱ {seconds}s", tone="text_hint")
        QTimer.singleShot(3000, lambda: self._set_verify_status(""))

    def _on_verify_all(self):
        """Run the data-integrity checks now, on a worker thread.

        The SAME list the scheduler runs every N days — see core.self_checks.
        This button used to open every archive and check the config snapshots
        but never repaired legacy backup metadata or pruned index rows whose
        zip was gone, while the automatic sweep did the opposite. "Run the
        checks" now means one thing regardless of where it was asked from.

        Threaded rather than inline: opening each archive and CRC-checking
        every member is I/O bound and a game with many large backups would
        otherwise freeze the window for seconds. Progress is shown next to
        the health button; the per-backup dots update as results arrive.

        Throttled to one sweep per minute: verifying re-opens every archive,
        and a double-click or an impatient re-click must not start a second
        sweep on top of the first.
        """
        from time import monotonic
        now = monotonic()
        remaining = 60.0 - (now - getattr(self, "_last_verify_all_mono", 0.0))
        if remaining > 0:
            self._show_cooldown_toast(int(remaining) + 1)
            return

        from core.backup import get_backup_manager
        mgr = get_backup_manager()
        gid = self._selected_game_id
        entries = (mgr.get_backups_for_game(gid) if gid else mgr.get_all_backups())
        ids = [b.backup_id for b in entries]
        names = {b.backup_id: (b.game_name or "").strip() for b in entries}
        total = len(ids)

        # Callbacks land on the checks' worker thread; these signals carry
        # them back to the GUI thread (queued, because the emitter differs).
        from PySide6.QtCore import QObject, Signal as _Signal

        class _CheckBridge(QObject):
            step = _Signal(str, int, int)     # check_id, index, total
            one = _Signal(str, str, str)      # backup_id, state, detail
            finished = _Signal(object)        # CheckResult

        bridge = _CheckBridge(self)
        self._check_bridge = bridge           # keep alive for the run

        done_n = [0]

        # Only the archive pass has per-item progress; the index and snapshot
        # checks are single steps, named so the status line does not sit on a
        # stale "12/40" while they run.
        _step_keys = {
            "backup_index_zips": "batch.verify_index",
            "backup_index": "batch.verify_index",
            "config_history_restore": "batch.verify_snapshot",
        }

        def _on_step(check_id: str, index: int, total_checks: int):
            key = _step_keys.get(check_id)
            if key:
                self._set_verify_status(t(key))

        def _on_one(bid, state, detail):
            from datetime import datetime
            done_n[0] += 1
            name = names.get(bid, "")
            msg = (t("backups.verify_running_named",
                     done=done_n[0], total=total, name=name)
                   if name else
                   t("backups.verify_running", done=done_n[0], total=total))
            self._set_verify_status(msg)
            self._verify_btn.setToolTip(msg)
            self.verify_progress.emit(done_n[0], total, name)

            _at = datetime.utcnow().isoformat()
            # Remember the outcome for rows that DON'T exist yet: a collapsed
            # title holds no BackupRow at all, so the loop below has nothing to
            # repaint for it. Expanding it later builds rows from the entry
            # copies this page listed with, whose verify state predates the
            # run — the dots came up grey for backups just checked.
            self._verify_fresh[bid] = (state, detail, _at)
            for row in list(getattr(self, "_backup_rows", ())):
                try:
                    if row._entry.backup_id == bid:
                        row._entry.verify_state = state
                        row._entry.verify_detail = detail
                        row._entry.verify_at = _at
                        row._apply_verify_dot()
                        break
                except RuntimeError:
                    pass      # row already gone (list rebuilt mid-run)

        def _on_finished(result):
            self._verify_btn.setEnabled(True)
            bad = result.archives_bad
            checked = result.archives_total
            msg = (t("backups.verify_result_all_ok", total=checked) if not bad
                   else t("backups.verify_result_bad", bad=bad, total=checked))
            if result.snapshots_ok:
                msg += "\n" + t("backups.verify_snapshots_ok")
            else:
                msg += "\n" + t("backups.verify_snapshots_bad",
                                detail=result.snapshots_detail)
            self._set_verify_status(
                msg, tone="success" if result.ok else "error")
            self._verify_btn.setToolTip(
                msg + "\n" + self._verify_idle_tooltip())
            logger.info(f"Integrity checks: {msg}")
            self.verify_finished.emit(msg)
            # The index checks can drop rows (zip gone) or repair metadata —
            # either way what this page is showing is now out of date.
            if result.removed_ids or result.repaired:
                self._refresh_list()
                self._load_games()

        bridge.step.connect(_on_step)
        bridge.one.connect(_on_one)
        bridge.finished.connect(_on_finished)

        from core.self_checks import run_checks
        started = run_checks(
            backup_ids=ids,
            skip_recent_hours=0,        # explicit request → always re-open
            on_progress=lambda c, i, n: bridge.step.emit(c, i, n),
            on_backup_result=lambda b, s, d: bridge.one.emit(b, s, d),
            on_done=lambda r: bridge.finished.emit(r),
        )
        if not started:
            # The scheduled sweep is already running — don't stack a second.
            self._set_verify_status(t("settings.diagnostics_running"))
            QTimer.singleShot(3000, lambda: self._set_verify_status(""))
            return

        self._last_verify_all_mono = now
        self._verify_btn.setEnabled(False)
        start_msg = (t("backups.verify_running", done=0, total=total)
                     if total else t("settings.diagnostics_running"))
        self._set_verify_status(start_msg)
        self._verify_btn.setToolTip(start_msg)
        self.verify_started.emit(max(total, 1))

    def _on_open_save_folder(self):
        """Open SaveSync's main backup directory in the file manager.

        Deliberately selection-independent: the old per-title behaviour
        (opening the selected game's save folders) belonged to the removed
        title-selection flow — each backup row has its own 📁 button for
        that entry's folder."""
        from core.constants import BACKUP_DIR
        open_in_file_manager(BACKUP_DIR)

    def _on_add_manual_paths(self):
        """Register save folders by hand — for saves with no executable."""
        self.manual_paths_requested.emit()

    def _on_page_size_changed(self, _size: int):
        """Back to the first page: the titles have just been redistributed."""
        self._backups_page_num = 1
        self._refresh_list()

    def _refresh_list(self):
        """Refresh the backup list for the selected game (non-blocking enter)."""
        if not _safe(self._list_layout):
            return
        self._pending_initial_load = False
        self._refresh_gen = getattr(self, "_refresh_gen", 0) + 1
        gen = self._refresh_gen
        self._stop_list_busy()
        QTimer.singleShot(0, lambda g=gen: self._refresh_list_deferred(g))

    def _stop_list_busy(self):
        busy = getattr(self, "_list_busy", None)
        if busy is not None:
            try:
                busy.close()
            except Exception:
                pass
            self._list_busy = None

    def _refresh_list_deferred(self, gen: int):
        if gen != getattr(self, "_refresh_gen", 0):
            return
        if not _safe(self._list_layout):
            return
        from ui.widgets.busy_overlay import DeferredBusy
        from ui.widgets.page_size import guarded_render, SCOPE_BACKUPS
        # Cover wipe + async group insert (same idea as library cards).
        if self.isVisible():
            self._list_busy = DeferredBusy(
                self, t("common.please_wait"), delay_ms=0)
        try:
            with guarded_render(SCOPE_BACKUPS):
                self._refresh_list_inner(gen)
        except Exception:
            self._stop_list_busy()
            raise

    def _refresh_list_inner(self, gen: int = 0):
        """Rebuild the listing. Split out of _refresh_list so the render (and
        only the render) sits inside the page-size guard."""
        # Theme-restyle bookkeeping: the group headers + backup rows below are
        # all rebuilt, so drop last generation's tracked rows and prune dead
        # header entries from this page's ThemedMixin registry (permanent
        # widgets like summary/empty stay — they're always live) so it can't
        # grow unbounded across refreshes.
        self._backup_rows = []
        # The listing below is re-read from the index, which verify_backup
        # already wrote the results into — nothing left to patch by hand.
        self._verify_fresh.clear()
        self.prune_themed_styles()

        # Detach before the wipe — the combo lives inside the top pager and
        # would otherwise be destroyed with it on every refresh.
        if getattr(self, "_page_size_combo", None) is not None:
            self._page_size_combo.setParent(self)

        # Remove non-permanent widgets and stale spacer items
        to_remove_widgets = []
        to_remove_spacers = []
        for i in range(self._list_layout.count()):
            item = self._list_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None and w is not self._empty_lbl:
                to_remove_widgets.append(w)
            elif w is None and item.spacerItem() is not None:
                to_remove_spacers.append(item)
        for w in to_remove_widgets:
            self._list_layout.removeWidget(w)
            w.deleteLater()
        for item in to_remove_spacers:
            self._list_layout.removeItem(item)

        gid  = self._selected_game_id
        tf   = self._game_filter_text.lower()
        mgr  = get_backup_manager()
        filt = self._get_origin_filter()

        # game_ids with ANY local backup (synced or not) — used below to
        # keep "provider_only" mutually exclusive with "local_sync": a
        # game with even one local backup entry belongs there instead,
        # regardless of whether that backup is also synced to a provider.
        local_sync_ids: set[str] = {b.game_id for b in mgr.get_all_backups()
                                     if getattr(b, "origin", "local") == "local"}

        # For provider_only, the sub-combo selects "all providers" or a specific one
        sub_provider = None
        if filt == "provider_only" and hasattr(self, '_provider_sub_combo'):
            sub_provider = self._provider_sub_combo.currentData()  # "__all__" or provider_id

        is_provider_specific = sub_provider and sub_provider != "__all__"
        provider_only = filt == "provider_only"

        # Get local backups
        if gid:
            local_backups = mgr.get_backups_for_game(gid)
            if not local_backups:
                # Orphan / phantom folder name selected from the combo
                local_backups = mgr.get_backups_for_folder(gid)
        elif tf:
            # No specific game selected but text filter active —
            # show backups for all games whose name contains the text
            matching_ids = {
                g.id for g in get_library().all_games()
                if tf in g.name.lower()
            }
            local_backups = [b for b in mgr.get_all_backups() if b.game_id in matching_ids]
        else:
            local_backups = mgr.get_all_backups()
        local_ids = {b.backup_id for b in local_backups}

        # provider_only / provider-specific → hide local entries, fetch cloud-only
        if provider_only or is_provider_specific:
            backups = []
        else:
            backups = [b for b in local_backups if self._matches_origin_filter(b)]

        # Fetch cloud-only entries when needed
        cloud_only_ids: set[str] = set()
        if filt in ("all", "provider_only", "archive_only") or is_provider_specific:
            try:
                from sync import get_orchestrator
                from core.constants import get_install_folder_name
                orch = get_orchestrator()
                # Every connected store — orch.provider is only the first and
                # hid cloud-only backups that lived on the second.
                providers = list(orch.get_connected_providers())
                if is_provider_specific:
                    providers = [p for p in providers if p.PROVIDER_ID == filt]
                for provider in providers:
                    from core.backup import BackupEntry
                    def _append_cloud_only(rd: dict, fallback_gid: str, fallback_name: str,
                                           remote_folder: str = "",
                                           _prov=provider):
                        bid = rd.get("backup_id", "")
                        if not bid or bid in local_ids or bid in cloud_only_ids:
                            return
                        rd.setdefault("game_id", fallback_gid)
                        rd.setdefault("game_name", fallback_name)
                        rd.setdefault("created_at", "")
                        rd.setdefault("machine_id", "")
                        rd.setdefault("save_paths", [])
                        rd.setdefault("zip_path", "")
                        rd.setdefault("size_bytes", 0)
                        rd.setdefault("note", "")
                        rd.setdefault("cloud_metadata", {})
                        if remote_folder:
                            # Where the zip actually lives — download must
                            # target this folder even if the local install
                            # name carries a different version suffix.
                            rd["cloud_metadata"]["remote_folder"] = remote_folder
                        rd["origin"] = _prov.PROVIDER_ID
                        try:
                            backups.append(BackupEntry.from_dict(rd))
                            cloud_only_ids.add(bid)
                        except Exception:
                            pass

                    if (provider_only or filt == "all") and not gid:
                        # No specific game selected: show every title whose
                        # backups exist ONLY on the cloud — this includes
                        # BOTH real library games with zero local backups
                        # (matched by folder name, but using their REAL
                        # game_id/name so selecting them later behaves like
                        # any other library game) AND true foreign folders
                        # with no library match at all. A library game that
                        # ALSO has local backups is excluded here — it
                        # belongs under "Locale + sync" instead, regardless
                        # of whether one of its backups is also synced to
                        # the provider.
                        from core.constants import get_folder_name_for_save
                        _lib_folder_to_game: dict[str, "GameEntry"] = {}
                        _local_backed_folders: set[str] = set()
                        for _g in get_library().all_games():
                            _folders = {
                                get_install_folder_name(
                                    _g.exe_path or "", _g.name, _g.id, _g.computed_folder_name
                                ).lower()
                            }
                            for _hn in _g.name_history:
                                _folders.add(get_folder_name_for_save(_hn, _g.exe_path or "", _g.id).lower())
                            for _fh in (_g.folder_history or []):
                                _folders.add(_fh.lower())
                            for _f in _folders:
                                _lib_folder_to_game[_f] = _g
                            if _g.id in local_sync_ids:
                                _local_backed_folders.update(_folders)

                        def _cloud_entry_matches_filter(display_name: str) -> bool:
                            return (not tf) or (tf in display_name.lower())

                        try:
                            from core.constants import version_insensitive_slug
                            _lib_norm_to_game = {version_insensitive_slug(f): g
                                                 for f, g in _lib_folder_to_game.items()}
                            _local_backed_norm = {version_insensitive_slug(f)
                                                  for f in _local_backed_folders}
                            all_cloud_backups = provider.list_all_cloud_backups()
                            for game_folder, backup_entries in all_cloud_backups.items():
                                folder_lower = game_folder.lower()
                                folder_norm = version_insensitive_slug(game_folder)
                                if (folder_lower in _local_backed_folders
                                        or folder_norm in _local_backed_norm):
                                    continue   # has a local backup -> belongs under Locale + sync, not here
                                # Exact match first, then version/build-insensitive
                                lib_game = (_lib_folder_to_game.get(folder_lower)
                                            or _lib_norm_to_game.get(folder_norm))
                                if filt == "archive_only" and lib_game is not None:
                                    continue   # has a library game -> not an archive
                                display_gid = lib_game.id if lib_game else game_folder
                                display_name = lib_game.name if lib_game else game_folder
                                if not _cloud_entry_matches_filter(display_name):
                                    continue
                                for rd in backup_entries:
                                    _append_cloud_only(rd, fallback_gid=display_gid,
                                                       fallback_name=display_name,
                                                       remote_folder=game_folder)
                        except Exception:
                            # Fallback to old method if master index fails
                            try:
                                remote_files = provider.list_files("SaveSync")
                                folders: set[str] = set()
                                for rf in remote_files:
                                    p = getattr(rf, "path", "") or ""
                                    if not p.startswith("SaveSync/backup/"):
                                        continue
                                    if not p.endswith("/index.json"):
                                        continue
                                    # Extract <game_folder> from SaveSync/backup/<game_folder>/index.json
                                    rel = p[len("SaveSync/backup/"):]
                                    game_folder = rel.split("/", 1)[0].strip("/")
                                    if game_folder:
                                        folders.add(game_folder)
                                from core.constants import version_insensitive_slug as _vslug
                                _lib_norm_fb = {_vslug(f): g for f, g in _lib_folder_to_game.items()}
                                _local_norm_fb = {_vslug(f) for f in _local_backed_folders}
                                for game_folder in sorted(folders, key=lambda s: s.lower()):
                                    folder_lower = game_folder.lower()
                                    folder_norm = _vslug(game_folder)
                                    if (folder_lower in _local_backed_folders
                                            or folder_norm in _local_norm_fb):
                                        continue
                                    lib_game = (_lib_folder_to_game.get(folder_lower)
                                                or _lib_norm_fb.get(folder_norm))
                                    display_gid = lib_game.id if lib_game else game_folder
                                    display_name = lib_game.name if lib_game else game_folder
                                    if not _cloud_entry_matches_filter(display_name):
                                        continue
                                    for rd in provider.list_cloud_backups(game_folder):
                                        _append_cloud_only(rd, fallback_gid=display_gid,
                                                           fallback_name=display_name,
                                                           remote_folder=game_folder)
                            except Exception:
                                pass
                    else:
                        # Specific game selected OR non-provider-only provider filter:
                        # fall back to checking only library games.
                        game_ids_to_check = [gid] if gid else [g.id for g in get_library().all_games()]
                        for check_gid in game_ids_to_check:
                            entry = get_library().get_by_id(check_gid)
                            if entry:
                                game_folder = get_install_folder_name(entry.exe_path, entry.name, check_gid, entry.computed_folder_name)
                                # Version/build-insensitive resolution against
                                # the actual remote folders (install names may
                                # carry a version suffix that changed).
                                _resolved = orch.resolve_remote_game_folder(provider, [game_folder])
                                if _resolved:
                                    game_folder = _resolved
                                fallback_name = entry.name
                            elif gid:
                                # check_gid doesn't resolve to a real library
                                # entry — this is a phantom/foreign game
                                # selected straight from the combo (see
                                # _load_games' provider-only phantom
                                # entries), where the folder name itself
                                # IS the identifier. Use it directly rather
                                # than silently skipping, which previously
                                # made such an entry disappear from the list
                                # the instant it was selected even though it
                                # was visible a moment earlier in the
                                # unselected aggregate view.
                                game_folder = check_gid
                                fallback_name = check_gid
                            else:
                                continue
                            for rd in provider.list_cloud_backups(game_folder):
                                _append_cloud_only(rd, fallback_gid=check_gid,
                                                   fallback_name=fallback_name,
                                                   remote_folder=game_folder)
            except Exception:
                pass

        backups.sort(key=lambda b: b.created_dt, reverse=True)
        if not gid:
            backups.sort(key=lambda b: (b.game_name or "").lower())

        if not backups:
            if _safe(self._empty_lbl):
                self._empty_lbl.setText(t("backup.no_backups"))
                self._empty_lbl.setVisible(True)
            if _safe(self._summary_lbl):
                self._summary_lbl.setText(t("backup.no_backups"))
            self._stop_list_busy()
            return

        if _safe(self._empty_lbl):
            self._empty_lbl.setVisible(False)

        # Check if game is currently running
        from core.monitor import get_monitor
        playing_ids = {e.id for e in get_monitor().currently_playing()}
        is_playing = bool(gid) and gid in playing_ids

        total_size = sum(b.size_bytes for b in backups)
        if _safe(self._summary_lbl):
            count = len(backups)
            size_str = _fmt_size(total_size)
            summary = f"{t('backups.count', count=count)}  \u2022  {size_str}"
            if is_playing:
                summary += f"  •  {t('backups.game_running')}"
            self._summary_lbl.setText(summary)

        # ── Group by title (collapsible "spoiler" per game) ───────────────
        groups: dict[str, dict] = {}
        order: list[str] = []
        for bk in backups:
            key = getattr(bk, "game_id", "") or (bk.game_name or "?")
            if key not in groups:
                groups[key] = {"name": bk.game_name or key, "entries": []}
                order.append(key)
            groups[key]["entries"].append(bk)

        # ── Pagination over titles: only the current page is rendered ─────
        from ui.pages.library_page import build_pager
        from ui.widgets.page_size import SCOPE_BACKUPS, page_size
        per_page = page_size(SCOPE_BACKUPS)
        total_pages = max(1, -(-len(order) // per_page))
        self._backups_page_num = max(1, min(getattr(self, "_backups_page_num", 1), total_pages))
        start = (self._backups_page_num - 1) * per_page
        page_keys = order[start:start + per_page]

        def _go_page(n: int):
            self._backups_page_num = n
            self._refresh_list()

        # Top pager carries the page-size combo (same line as the numbers).
        # Shown even on one page so the size control does not vanish.
        # Bottom pager is numbers only — a combo can have one parent.
        self._list_layout.addWidget(
            build_pager(self._backups_page_num, total_pages, _go_page,
                        size_combo=self._page_size_combo))

        single_group = len(order) == 1
        # Queue group widgets; insert in QTimer chunks (library-style).
        self._group_insert_gen = gen
        self._group_insert_queue = []
        for key in page_keys:
            grp = groups[key]
            expanded = single_group or key in self._expanded_titles

            def _make(key=key, grp=grp, expanded=expanded):
                return self._build_backup_group(
                    key, grp["name"], grp["entries"], gid, is_playing,
                    cloud_only_ids, expanded)

            self._group_insert_queue.append(_make)
        self._group_insert_bottom_pager = None
        if total_pages > 1:
            self._group_insert_bottom_pager = lambda: build_pager(
                self._backups_page_num, total_pages, _go_page)
        from core.concurrency import library_insert_chunk_size
        cs = library_insert_chunk_size()
        self._group_insert_chunk = cs if cs > 0 else 16
        if not self._group_insert_queue:
            if self._group_insert_bottom_pager is not None:
                self._list_layout.addWidget(self._group_insert_bottom_pager())
            self._list_layout.addStretch()
            self._stop_list_busy()
            return
        QTimer.singleShot(0, lambda g=gen: self._async_group_insert_step(g))

    def _async_group_insert_step(self, gen: int):
        if gen != getattr(self, "_refresh_gen", 0):
            return
        if not _safe(self._list_layout):
            self._stop_list_busy()
            return
        queue = getattr(self, "_group_insert_queue", None) or []
        if not queue:
            self._finish_group_insert(gen)
            return
        chunk_n = getattr(self, "_group_insert_chunk", 0) or len(queue)
        chunk = queue[:chunk_n]
        del queue[:chunk_n]
        self._group_insert_queue = queue
        for make in chunk:
            if gen != getattr(self, "_refresh_gen", 0):
                return
            try:
                self._list_layout.addWidget(make())
            except RuntimeError:
                self._stop_list_busy()
                return
        if queue:
            QTimer.singleShot(0, lambda g=gen: self._async_group_insert_step(g))
            return
        self._finish_group_insert(gen)

    def _finish_group_insert(self, gen: int):
        if gen != getattr(self, "_refresh_gen", 0):
            return
        if not _safe(self._list_layout):
            self._stop_list_busy()
            return
        bottom = getattr(self, "_group_insert_bottom_pager", None)
        self._group_insert_bottom_pager = None
        if callable(bottom):
            try:
                self._list_layout.addWidget(bottom())
            except RuntimeError:
                pass
        self._list_layout.addStretch()
        self._stop_list_busy()

    def _archive_auto_widget(self, entries: list):
        """Per-archive scheduled-backup control, or None when not applicable.

        Shown only for a LOCAL archive: a cloud-only listing has nothing on
        this machine to re-read, and a library game already has its own
        auto-backup setting in Add/Edit Game.
        """
        from core.backup import get_backup_manager
        if not entries:
            return None
        newest = max(entries, key=lambda b: b.created_dt)
        if getattr(newest, "origin", "local") != "local":
            return None
        mgr = get_backup_manager()
        if not (newest.cloud_metadata or {}).get("orphan"):
            return None
        game_id = newest.game_id
        if not game_id or game_id in mgr.library_game_ids():
            return None

        sources = mgr.orphan_source_paths(newest)
        enabled, interval = mgr.archive_auto_backup(game_id)

        from PySide6.QtWidgets import QCheckBox, QSpinBox
        box = QWidget()
        box.setObjectName("transparent_bg")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 4)
        outer.setSpacing(2)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        cb = QCheckBox(t("backups.archive_auto_backup"))
        cb.setToolTip(t("backups.archive_auto_backup_tooltip"))
        cb.setChecked(bool(enabled))
        every = QLabel(t("backups.archive_auto_every"))
        spin = QSpinBox()
        # Minutes: an archive's folder is a live save folder, and the point
        # of watching one is catching a session's worth of play. Capped at a
        # week so a stray keystroke cannot park it out of sight.
        spin.setRange(1, 10080)
        spin.setValue(int(interval))
        spin.setSuffix(f" {t('backups.archive_auto_minutes')}")
        spin.setFixedWidth(scaled(110, self, min_px=96))
        row.addWidget(cb)
        row.addWidget(every)
        row.addWidget(spin)
        row.addStretch()
        outer.addLayout(row)

        hint = QLabel()
        hint.setObjectName("settings_hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        def _refresh_hint():
            if not sources:
                hint.setText(t("backups.archive_auto_no_source"))
                return
            src = sources[0]
            from pathlib import Path as _P
            if not _P(src).is_dir():
                hint.setText(t("backups.archive_auto_source_missing"))
                return
            hint.setText(t("backups.archive_auto_source", path=src))

        def _apply(*_):
            mgr.set_archive_auto_backup(game_id, cb.isChecked(), spin.value())
            spin.setEnabled(cb.isChecked())
            _refresh_hint()

        # No source recorded means nothing to re-read: the control would
        # promise a backup that can never happen.
        if not sources:
            cb.setEnabled(False)
            spin.setEnabled(False)
        else:
            spin.setEnabled(cb.isChecked())
            cb.toggled.connect(_apply)
            spin.valueChanged.connect(_apply)
        _refresh_hint()
        return box

    def _build_backup_group(self, key: str, title: str, entries: list,
                            gid, is_playing: bool, cloud_only_ids: set,
                            expanded: bool) -> QWidget:
        """One collapsible per-title section: header row (toggle) + lazily
        built list of BackupRow widgets. Rows are only created when the
        section is first expanded, so collapsed titles cost nothing."""
        wrap = QWidget()
        wrap.setObjectName("transparent_bg")
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)

        header = QPushButton()
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        total_size = sum(b.size_bytes for b in entries)
        header.setObjectName("backup_group_header")

        # Layout INSIDE the button: arrow, then the title (middle-elided so a
        # long title can never push the count off the right edge), then the
        # right-aligned "count · size". A plain text button clipped the count
        # at narrow widths — the count mix report.
        from ui.helpers import ElidedLabel
        arrow_lbl = QLabel("▼" if expanded else "▶")
        arrow_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        title_lbl = ElidedLabel(title, own_tooltip=False)
        title_lbl.setObjectName("backup_group_title")
        meta_lbl = QLabel(
            f"{t('backups.count', count=len(entries))}  ·  {_fmt_size(total_size)}")
        meta_lbl.setObjectName("backup_group_meta")
        _head = QHBoxLayout(header)
        _head.setContentsMargins(12, 0, 12, 0)
        _head.setSpacing(8)
        _head.addWidget(arrow_lbl)
        _head.addWidget(title_lbl, 1)
        _head.addWidget(meta_lbl)
        header.setToolTip(t("backups.hide_saves") if expanded else t("backups.show_saves"))
        col.addWidget(header)

        body = QWidget()
        body.setObjectName("transparent_bg")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(10, 0, 0, 0)
        body_lay.setSpacing(6)
        body.setVisible(False)
        col.addWidget(body)

        # Archives only: a folder handed over without adding the game to the
        # library gets no scheduled backups from anywhere else, so it carries
        # its own. Inside the body rather than the header — the header is one
        # big toggle button, and a control inside it would fight the click.
        arch = self._archive_auto_widget(entries)
        if arch is not None:
            body_lay.addWidget(arch)

        built = {"done": False}

        def _build_rows(tick=None):
            if built["done"]:
                return
            built["done"] = True
            for i, bk in enumerate(entries):
                # Apply any check that ran while this title was collapsed, so
                # a freshly expanded group shows the dots it earned instead of
                # the state its listing copy was made with.
                _fresh = self._verify_fresh.get(bk.backup_id)
                if _fresh:
                    bk.verify_state, bk.verify_detail, bk.verify_at = _fresh
                is_cloud = bk.backup_id in cloud_only_ids
                row = BackupRow(bk, is_playing=is_playing, cloud_only=is_cloud)
                _target_gid = gid if gid is not None else getattr(bk, "game_id", "")
                row.restore_requested.connect(
                    lambda bid, g=_target_gid: self._on_restore(g, bid))
                row.delete_requested.connect(self._on_delete)
                # Track the live row so refresh_styles() can cascade a theme
                # switch into it without recreating it (rows own a separate
                # ThemedMixin registry from the page).
                self._backup_rows.append(row)
                body_lay.addWidget(row)
                if tick is not None and i % 20 == 19:
                    tick(i + 1)

        def _toggle():
            now_open = not body.isVisible()
            if now_open:
                # Building BackupRow widgets for a title can stall — cover it
                # (sheet only if it outlasts ~100 ms).
                from ui.widgets.busy_overlay import busy_over
                with busy_over(self, t("common.please_wait")):
                    _build_rows()
                self._expanded_titles.add(key)
            else:
                self._expanded_titles.discard(key)
            body.setVisible(now_open)
            arrow_lbl.setText("▼" if now_open else "▶")
            header.setToolTip(t("backups.hide_saves") if now_open else t("backups.show_saves"))

        header.clicked.connect(_toggle)

        if expanded:
            _build_rows()
            body.setVisible(True)
            self._expanded_titles.add(key)
        return wrap

    def refresh_styles(self):
        """Re-apply inline, palette-dependent styles IN PLACE on a light/dark
        theme switch — no widget-tree rebuild (called by MainWindow on every
        theme change).

        super().refresh_styles() covers the widgets registered on THIS page's
        own registry: the summary/empty labels and the current group-header
        buttons (registered via self._sty in _build_backup_group). The
        suggestions popup and every live BackupRow own SEPARATE registries, so
        they are cascaded explicitly here — otherwise they'd keep stale colours
        until the next rebuild."""
        super().refresh_styles()
        if _safe(self._suggest_popup):
            self._suggest_popup.refresh_styles()
        for row in list(getattr(self, "_backup_rows", ())):
            try:
                row.refresh_styles()
            except RuntimeError:
                pass   # underlying C++ row already deleted — skip
        # Pager buttons need nothing here: #pager_btn / #pager_btn_active come
        # from the theme, already re-resolved by the stylesheet swap.

    def _remediate_page_scrolls(self):
        """Re-mediate scroll policies after DPI scale changes to maintain proportions.
        
        Ensures backup rows maintain proper sizing after DPI changes.
        """
        try:
            # Update row geometries
            for row in list(getattr(self, "_backup_rows", ())):
                if hasattr(row, "updateGeometry"):
                    try:
                        row.updateGeometry()
                    except RuntimeError:
                        pass
            
            # Trigger layout recalculation
            if hasattr(self, 'layout') and self.layout():
                self.layout().activate()
                self.layout().update()
        except Exception:
            pass

    def _on_backup_now(self):
        """Trigger backup.

        No selection → backup all tracked games (same as overlay 'Backup all').
        Game selected → backup only that game (or sync if provider filter active).
        """
        gid = self._selected_game_id
        if not gid:
            from core.library import get_library
            ids = [g.id for g in get_library().all_games() if g.save_paths]
            self.backup_all_requested.emit(ids)
            return

        filt = self._get_origin_filter()
        if filt not in ("all", "local_only", "local_sync"):
            from sync import get_orchestrator
            from core.library import get_library
            orch = get_orchestrator()
            entry = get_library().get_by_id(gid)
            if orch.is_online() and entry and entry.save_paths:
                orch.sync_game(
                    entry.id, entry.name, entry.save_paths,
                    exe_path=entry.exe_path,
                    computed_folder_name=entry.computed_folder_name,
                )
        else:
            self.backup_requested.emit(gid)

    def _on_restore(self, game_id: str, backup_id: str):
        self.restore_requested.emit(game_id, backup_id)

    def _on_delete(self, backup_id: str):
        # The refresh rides backup_deleted (see _on_backup_deleted): it has
        # to work for the delete paths that never pass through here anyway,
        # so having the button refresh separately only meant two rebuilds
        # for one deletion — and one code path that could be kept correct
        # while the other rotted.
        get_backup_manager().delete_backup(backup_id)

    def update_locale(self):
        if _safe(self._header):
            self._header.setText(t("backup.title"))
        if _safe(self._open_folder_btn):
            self._open_folder_btn.setText(t("buttons.open_folder"))
            self._open_folder_btn.setToolTip(t("tooltips.open_save_folder"))
        if _safe(self._verify_btn):
            # Icon-only: the label is the emoji. While a check is running the
            # status text next to it is live; otherwise reset the tooltip.
            self._refresh_verify_tooltip()
        if _safe(self._verify_status) and self._verify_btn.isEnabled():
            # A finished result was in the old language — clear rather than
            # guess which key it came from.
            if self._verify_status.isVisible():
                self._set_verify_status("")
        if _safe(self._add_paths_btn):
            self._add_paths_btn.setToolTip(t("manual_path.button_tooltip"))
        if _safe(self._backup_now_btn):
            self._backup_now_btn.setText(t("buttons.backup_now"))
            self._backup_now_btn.setToolTip(t("backups.backup_now_tooltip"))
        if _safe(self._page_size_combo):
            self._page_size_combo.update_locale()
        if _safe(self._empty_lbl):
            self._empty_lbl.setText(t("backup.no_backups"))
        # Rebuild origin filter with translated items
        self._rebuild_origin_filter()
        # The search field's default placeholder ("Search title") is plain
        # translated text — swap it, but only while it IS the default: an
        # active text filter or a confirmed game shows user/game text as
        # placeholder, which must survive a language switch.
        if not self._game_filter_text and not self._selected_game_id:
            le = self._game_combo.lineEdit()
            if le is not None:
                le.setPlaceholderText(t('backups.search_game'))
        # Rebuild game combo with translated default item
        self._load_games()