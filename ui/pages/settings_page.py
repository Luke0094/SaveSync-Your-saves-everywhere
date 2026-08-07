import logging
import os

logger = logging.getLogger(__name__)
"""
SaveSync - Settings Page
Full settings with startup, monitor tuning, ignored processes, and more.
"""
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFrame, QScrollArea, QLineEdit, QCheckBox,
    QSpinBox, QPlainTextEdit, QGroupBox, QFormLayout,
    QListWidget, QListWidgetItem, QMenu,
)
from PySide6.QtGui import QColor

from i18n import t, get_engine
from core.config_manager import get_config
from core.startup import set_launch_on_startup, get_launch_on_startup
from ui.styles.theme import get_theme_manager, palette
from ui.modal_helpers import information_window_modal, warning_window_modal
from ui.widgets.hotkey_edit import HotkeyEdit


def _group(title: str) -> QGroupBox:
    """A settings section. The look is the theme's QGroupBox rule — there is
    nothing to style here, and nothing to re-apply on a theme switch."""
    return QGroupBox(title)


class SettingsPage(QWidget):
    hotkey_changed = Signal(str, str)   # old, new
    theme_changed  = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = True          # suppress dirty during load
        self._dirty = False
        self._snapshot = {}
        self._pre_change_theme = None
        self._pre_change_lang = None
        self._build()
        self._load()
        self._snapshot = self._take_snapshot()
        self._loading = False
        # Live refresh of the dynamic lists while the page is open (see
        # _on_config_changed); showEvent covers changes made elsewhere.
        get_config().config_changed.connect(self._on_config_changed)

    # ── Build UI ─────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 0)
        root.setSpacing(0)

        self._header = QLabel(t("settings.title"))
        self._header.setObjectName("page_header")
        root.addWidget(self._header)
        root.addSpacing(20)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("transparent_bg")
        self._scroll_layout = QVBoxLayout(content)
        self._scroll_layout.setSpacing(16)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)

        # ── Appearance ────────────────────────────────────────────────────────
        app_grp = _group(t("settings.section_appearance"))
        app_form = QFormLayout(app_grp)
        app_form.setSpacing(14)

        self._lang_combo = QComboBox()
        self._lang_combo.setMaxVisibleItems(10)
        self._lang_combo.setToolTip(t("settings.language_tooltip"))
        _display_fallback = {"en": "English", "it": "Italiano"}
        engine = get_engine()
        locale_names = {}
        locales_dir = Path(__file__).parent.parent.parent / "i18n" / "locales"
        for f in locales_dir.glob("*.json"):
            code = f.stem
            name = _display_fallback.get(code, code)
            # Self-describing dictionaries: a locale file that carries its
            # own endonym under languages.<code> ("languages": {"es":
            # "Español"}) names itself in the combo — dropping a new
            # translation file in i18n/locales is ALL it takes to appear
            # here with a proper display name.
            try:
                import json as _json
                with open(f, encoding="utf-8") as _fh:
                    _data = _json.load(_fh)
                _own = (_data.get("languages") or {}).get(code)
                if _own and isinstance(_own, str):
                    name = _own
            except Exception:
                pass
            locale_names[code] = name
        for code in engine.available_locales():
            self._lang_combo.addItem(locale_names.get(code, code), code)
        self._lang_combo.currentIndexChanged.connect(self._on_language_change)
        self._lang_lbl = QLabel(t("settings.language"))
        app_form.addRow(self._lang_lbl, self._lang_combo)

        self._theme_combo = QComboBox()
        self._theme_combo.setMaxVisibleItems(10)
        self._theme_combo.setToolTip(t("settings.theme_tooltip"))
        self._theme_combo.addItem(t("settings.theme_dark"),  "dark")
        self._theme_combo.addItem(t("settings.theme_light"), "light")
        self._theme_combo.currentIndexChanged.connect(self._on_theme_change)
        self._theme_lbl = QLabel(t("settings.theme"))
        app_form.addRow(self._theme_lbl, self._theme_combo)

        self._scroll_layout.addWidget(app_grp)

        # ── Behaviour ─────────────────────────────────────────────────────────
        beh_grp = _group(t("settings.section_behaviour"))
        beh_form = QFormLayout(beh_grp)
        beh_form.setSpacing(14)

        self._startup_cb = QCheckBox(t("settings.launch_on_startup"))
        self._startup_cb.setToolTip(t("settings.launch_on_startup_tooltip"))
        beh_form.addRow("", self._startup_cb)
        self._tray_cb = QCheckBox(t("settings.minimize_to_tray"))
        self._tray_cb.setToolTip(t("settings.minimize_to_tray_tooltip"))
        beh_form.addRow("", self._tray_cb)
        self._hide_on_game_cb = QCheckBox(t("settings.hide_to_tray_on_game_launch"))
        self._hide_on_game_cb.setToolTip(t("settings.hide_to_tray_on_game_launch_tooltip"))
        beh_form.addRow("", self._hide_on_game_cb)
        self._backup_on_exit_cb = QCheckBox(t("settings.backup_on_exit"))
        self._backup_on_exit_cb.setToolTip(t("settings.backup_on_exit_tooltip"))
        beh_form.addRow("", self._backup_on_exit_cb)
        self._auto_sync_cb = QCheckBox(t("settings.auto_sync_after_backup"))
        self._auto_sync_cb.setToolTip(t("settings.auto_sync_after_backup_tooltip"))
        beh_form.addRow("", self._auto_sync_cb)
        # The explanation is on the option itself — hovering the label is what
        # people try first, and a "?" next to one option out of a dozen just
        # raised the question of why the others didn't have one.
        self._auto_scan_cb = QCheckBox(t("settings.auto_scan_on_exit"))
        self._auto_scan_cb.setToolTip(t("settings.auto_scan_on_exit_tooltip"))
        beh_form.addRow("", self._auto_scan_cb)

        # ── Per-game suppression list ─────────────────────────────────────────
        # Shows games that have "don't confirm scan" or "don't show notification"
        # set, with individual remove buttons.  Populated in _load_settings.
        self._suppression_group = QGroupBox(t("settings.game_suppressions_title"))
        _supp_vbox = QVBoxLayout(self._suppression_group)
        _supp_vbox.setSpacing(4)
        _supp_vbox.setContentsMargins(8, 8, 8, 8)

        self._supp_hint = QLabel(t("settings.game_suppressions_hint"))
        self._supp_hint.setWordWrap(True)
        _supp_vbox.addWidget(self._supp_hint)

        self._suppression_search = self._make_search_box()
        _supp_vbox.addWidget(self._suppression_search)

        self._suppression_list = QListWidget()
        self._suppression_list.setFixedHeight(120)
        self._suppression_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self._bind_search_box(self._suppression_search, self._suppression_list)
        _supp_vbox.addWidget(self._suppression_list)

        self._unblock_game_btn = QPushButton(t("settings.unblock_selected"))
        self._unblock_game_btn.setFixedHeight(28)
        self._unblock_game_btn.clicked.connect(self._unblock_selected_game_prefs)
        _supp_vbox.addWidget(self._unblock_game_btn)

        self._scroll_layout.addWidget(beh_grp)

        # ── Overlay ───────────────────────────────────────────────────────────
        ov_grp = _group(t("settings.section_overlay"))
        ov_form = QFormLayout(ov_grp)
        ov_form.setSpacing(14)

        self._hotkey_edit = HotkeyEdit()
        self._hotkey_lbl = QLabel(t("settings.hotkey"))
        ov_form.addRow(self._hotkey_lbl, self._hotkey_edit)
        self._overlay_launch_cb = QCheckBox(t("settings.show_overlay_on_launch"))
        self._overlay_launch_cb.setToolTip(t("settings.show_overlay_on_launch_tooltip"))
        ov_form.addRow("", self._overlay_launch_cb)
        self._overlay_unknown_cb = QCheckBox(t("settings.show_overlay_on_unknown"))
        self._overlay_unknown_cb.setToolTip(t("settings.show_overlay_on_unknown_tooltip"))
        ov_form.addRow("", self._overlay_unknown_cb)
        self._overlay_cloud_cb = QCheckBox(t("settings.show_overlay_on_cloud"))
        self._overlay_cloud_cb.setToolTip(t("settings.show_overlay_on_cloud_tooltip"))
        ov_form.addRow("", self._overlay_cloud_cb)
        self._overlay_backup_cb = QCheckBox(t("settings.show_overlay_on_backup"))
        self._overlay_backup_cb.setToolTip(t("settings.show_overlay_on_backup_tooltip"))
        ov_form.addRow("", self._overlay_backup_cb)

        self._scroll_layout.addWidget(ov_grp)

        # ── Backup policy ─────────────────────────────────────────────────────
        bk_grp = _group(t("settings.section_backup_policy"))
        bk_form = QFormLayout(bk_grp)
        bk_form.setSpacing(14)

        self._max_backups_spin = QSpinBox()
        self._max_backups_spin.setRange(1, 100)
        self._max_backups_spin.setToolTip(t("settings.max_backups_tooltip"))
        self._max_backups_lbl = QLabel(t("settings.max_backups"))
        bk_form.addRow(self._max_backups_lbl, self._max_backups_spin)
        self._retention_spin = QSpinBox()
        self._retention_spin.setRange(1, 365)
        self._retention_spin.setToolTip(t("settings.backup_retention_tooltip"))
        self._retention_lbl = QLabel(t("settings.backup_retention"))
        bk_form.addRow(self._retention_lbl, self._retention_spin)
        self._min_kept_spin = QSpinBox()
        self._min_kept_spin.setRange(0, 50)
        self._min_kept_spin.setToolTip(t("settings.min_kept_backups_tooltip"))
        self._min_kept_lbl = QLabel(t("settings.min_kept_backups"))
        bk_form.addRow(self._min_kept_lbl, self._min_kept_spin)
        self._max_size_spin = QSpinBox()
        self._max_size_spin.setRange(10, 4096)
        self._max_size_spin.setSuffix(" MB")
        self._max_size_spin.setToolTip(t("settings.max_size_mb_tooltip"))
        self._max_size_lbl = QLabel(t("settings.max_size_mb"))
        bk_form.addRow(self._max_size_lbl, self._max_size_spin)

        # Periodic integrity check — the archive is the whole point of a
        # backup, and a damaged one is worth finding before it is needed.
        self._verify_cb = QCheckBox(t("settings.backup_verify"))
        self._verify_cb.setToolTip(t("settings.backup_verify_tooltip"))
        bk_form.addRow("", self._verify_cb)
        self._verify_days_spin = QSpinBox()
        self._verify_days_spin.setRange(1, 365)
        self._verify_days_spin.setSuffix(" " + t("settings.days_suffix"))
        self._verify_days_spin.setToolTip(t("settings.backup_verify_interval_tooltip"))
        self._verify_days_lbl = QLabel(t("settings.backup_verify_interval"))
        bk_form.addRow(self._verify_days_lbl, self._verify_days_spin)
        self._verify_cb.toggled.connect(self._verify_days_spin.setEnabled)
        self._verify_cb.toggled.connect(self._verify_days_lbl.setEnabled)

        self._scroll_layout.addWidget(bk_grp)

        # The save editor keeps a copy of a file before it writes to it. That
        # is a different thing from the backup policy above — one edited file,
        # not a whole save folder — so it gets its own rules.
        ed_grp = _group(t("settings.section_save_edit_copies"))
        ed_form = QFormLayout(ed_grp)
        ed_form.setSpacing(14)
        self._edit_copies_spin = QSpinBox()
        self._edit_copies_spin.setRange(1, 50)
        self._edit_copies_spin.setToolTip(t("settings.save_edit_copies_tooltip"))
        self._edit_copies_lbl = QLabel(t("settings.save_edit_copies"))
        ed_form.addRow(self._edit_copies_lbl, self._edit_copies_spin)
        self._edit_copy_days_spin = QSpinBox()
        self._edit_copy_days_spin.setRange(1, 365)
        self._edit_copy_days_spin.setSuffix(" " + t("settings.days_suffix"))
        self._edit_copy_days_spin.setToolTip(t("settings.save_edit_copy_days_tooltip"))
        self._edit_copy_days_lbl = QLabel(t("settings.save_edit_copy_days"))
        ed_form.addRow(self._edit_copy_days_lbl, self._edit_copy_days_spin)
        self._scroll_layout.addWidget(ed_grp)

        # ── Process monitor ───────────────────────────────────────────────────
        pm_grp = _group(t("settings.section_process_monitor"))
        pm_form = QFormLayout(pm_grp)
        pm_form.setSpacing(14)

        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(1, 60)
        self._poll_spin.setSuffix(" sec")
        self._poll_spin.setToolTip(t("settings.poll_interval"))
        self._poll_lbl = QLabel(t("settings.poll_interval"))
        pm_form.addRow(self._poll_lbl, self._poll_spin)

        # ── Unified ignored-process list ──────────────────────────────────────
        # Combines suppressed_overlay_apps (full exe path, added automatically
        # when user clicks "don't show again") with any legacy ignored_processes
        # entries. Items here are always full paths or clear stems — no risk of
        # accidentally blocking unrelated processes with the same name.
        self._ignored_lbl = QLabel(t("settings.ignored_processes_section"))
        self._ignored_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        self._ignored_hint = QLabel(t("settings.ignored_processes_hint"))
        self._ignored_hint.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;")
        self._ignored_hint.setWordWrap(True)
        # Single-argument addRow = SPANNING row: hint, search, list and
        # button take the whole section width (the two-column ("", widget)
        # form kept them squeezed into the field column).
        pm_form.addRow(self._ignored_lbl)
        pm_form.addRow(self._ignored_hint)

        self._suppressed_search = self._make_search_box()
        pm_form.addRow(self._suppressed_search)

        self._suppressed_list = QListWidget()
        self._suppressed_list.setFixedHeight(150)
        self._suppressed_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._suppressed_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._apply_suppressed_list_style()
        self._bind_search_box(self._suppressed_search, self._suppressed_list)
        pm_form.addRow(self._suppressed_list)

        self._unblock_btn = QPushButton(t("settings.unblock_selected"))
        self._unblock_btn.setFixedHeight(28)
        self._unblock_btn.clicked.connect(self._unblock_selected)
        pm_form.addRow(self._unblock_btn)

        self._scroll_layout.addWidget(pm_grp)
        self._scroll_layout.addWidget(self._suppression_group)

        # ── Excluded save paths ───────────────────────────────────────────────
        # Paths deleted (trash icon) during save confirmation — live tracking,
        # auto-detect, general/extended scan. Kept in their OWN store/box so
        # they don't bloat the per-game notification preferences above.
        # Restoring an entry removes the exclusion, so a later scan can
        # legitimately re-propose that path.
        self._excluded_paths_group = QGroupBox(t("settings.excluded_paths_title"))
        _exp_vbox = QVBoxLayout(self._excluded_paths_group)
        _exp_vbox.setSpacing(4)
        _exp_vbox.setContentsMargins(8, 8, 8, 8)

        self._exp_hint = QLabel(t("settings.excluded_paths_hint"))
        self._exp_hint.setWordWrap(True)
        _exp_vbox.addWidget(self._exp_hint)

        self._excluded_paths_search = self._make_search_box()
        _exp_vbox.addWidget(self._excluded_paths_search)

        self._excluded_paths_list = QListWidget()
        self._excluded_paths_list.setFixedHeight(120)
        self._excluded_paths_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self._bind_search_box(self._excluded_paths_search, self._excluded_paths_list)
        _exp_vbox.addWidget(self._excluded_paths_list)

        self._restore_paths_btn = QPushButton(t("settings.restore_selected"))
        self._restore_paths_btn.setFixedHeight(28)
        self._restore_paths_btn.clicked.connect(self._restore_selected_excluded_paths)
        _exp_vbox.addWidget(self._restore_paths_btn)

        self._scroll_layout.addWidget(self._excluded_paths_group)
        # Hints/search/lists of both sections share one theme-aware styler,
        # re-invoked by _refresh_styles on every theme switch.
        self._apply_prefs_section_styles()

        # ── Save detection ────────────────────────────────────────────────────
        det_grp = _group(t("settings.section_save_detection"))
        det_form = QFormLayout(det_grp)
        det_form.setSpacing(14)

        self._hints_lbl = QLabel(t("settings.save_hints"))
        det_form.addRow(self._hints_lbl, QLabel())
        self._hints_edit = QPlainTextEdit()
        self._hints_edit.setFixedHeight(90)
        self._hints_edit.setPlaceholderText(t("settings.save_hints_desc"))
        self._hints_edit.setToolTip(t("settings.save_hints_tooltip"))
        det_form.addRow("", self._hints_edit)

        # Temporal correlation — opt-in. It infers association from timing
        # alone, so the window is exposed too: the tighter it is, the more it
        # demands a genuine simultaneous double-write rather than a
        # coincidence.
        self._correlation_cb = QCheckBox(t("settings.save_correlation"))
        self._correlation_cb.setToolTip(t("settings.save_correlation_tooltip"))
        det_form.addRow("", self._correlation_cb)

        self._correlation_spin = QSpinBox()
        self._correlation_spin.setRange(100, 10000)
        self._correlation_spin.setSingleStep(100)
        self._correlation_spin.setSuffix(" ms")
        self._correlation_spin.setToolTip(t("settings.save_correlation_window_tooltip"))
        self._correlation_lbl = QLabel(t("settings.save_correlation_window"))
        det_form.addRow(self._correlation_lbl, self._correlation_spin)
        # The window only means anything while the option is on.
        self._correlation_cb.toggled.connect(self._correlation_spin.setEnabled)
        self._correlation_cb.toggled.connect(self._correlation_lbl.setEnabled)

        self._scroll_layout.addWidget(det_grp)

        # ── Config Transfer ─────────────────────────────────────────────────
        transfer_grp = _group(t("settings.section_config_transfer"))
        transfer_form = QFormLayout(transfer_grp)
        transfer_form.setSpacing(14)

        self._export_btn = QPushButton(t("settings.export_config"))
        self._export_btn.setMinimumHeight(36)
        self._apply_save_btn_style(self._export_btn)
        self._export_btn.clicked.connect(self._on_export_config_menu)
        transfer_form.addRow("", self._export_btn)

        self._import_btn = QPushButton(t("settings.import_config"))
        self._import_btn.setMinimumHeight(36)
        self._apply_save_btn_style(self._import_btn)
        self._import_btn.clicked.connect(self._on_import_config_menu)
        transfer_form.addRow("", self._import_btn)

        self._history_btn = QPushButton(t("settings.config_history"))
        self._history_btn.setMinimumHeight(36)
        self._apply_cancel_btn_style(self._history_btn)  # secondary — subtle
        self._history_btn.clicked.connect(self._on_config_history)
        transfer_form.addRow("", self._history_btn)

        self._scroll_layout.addWidget(transfer_grp)

        # ── Inline buttons (bottom of scroll) ────────────────────────────────
        self._inline_row = QFrame()
        self._inline_row.setFrameShape(QFrame.Shape.NoFrame)
        self._apply_inline_row_style()
        inline_layout = QHBoxLayout(self._inline_row)
        inline_layout.setContentsMargins(12, 12, 12, 12)
        inline_layout.setSpacing(8)

        self._inline_save = QPushButton(t("settings.save"))
        self._inline_save.setMinimumHeight(36)
        self._inline_save.setEnabled(False)
        self._inline_save.clicked.connect(self._save)

        self._inline_cancel = QPushButton(t("common.cancel"))
        self._inline_cancel.setMinimumHeight(36)
        self._inline_cancel.setEnabled(False)
        self._inline_cancel.clicked.connect(self._cancel)

        self._saved_lbl = QLabel()
        self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
        self._saved_lbl.setVisible(False)

        self._inline_reset = QPushButton(t("buttons.reset"))
        self._inline_reset.setMinimumHeight(36)
        self._inline_reset.setToolTip(t("tooltips.reset_defaults"))
        self._inline_reset.clicked.connect(self._reset)

        # Apply consistent styles
        self._apply_save_btn_style(self._inline_save)
        self._apply_cancel_btn_style(self._inline_cancel)
        self._apply_reset_btn_style(self._inline_reset)

        inline_layout.addWidget(self._inline_save)
        inline_layout.addWidget(self._inline_cancel)
        inline_layout.addWidget(self._saved_lbl)
        inline_layout.addStretch()
        inline_layout.addWidget(self._inline_reset)

        self._scroll_layout.addWidget(self._inline_row)

        self._scroll = scroll
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # ── Floating footer (overlay, not in layout — no scroll range impact) ─
        self._footer = QFrame(self)
        self._footer.setFrameShape(QFrame.Shape.NoFrame)
        self._apply_footer_style()
        footer_layout = QHBoxLayout(self._footer)
        footer_layout.setContentsMargins(12, 12, 12, 12)
        footer_layout.setSpacing(8)

        self._save_btn = QPushButton(t("settings.save"))
        self._save_btn.setMinimumHeight(36)
        self._save_btn.clicked.connect(self._save)

        self._cancel_btn = QPushButton(t("common.cancel"))
        self._cancel_btn.setMinimumHeight(36)
        self._cancel_btn.clicked.connect(self._cancel)

        self._apply_save_btn_style(self._save_btn)
        self._apply_cancel_btn_style(self._cancel_btn)

        footer_layout.addWidget(self._save_btn)
        footer_layout.addWidget(self._cancel_btn)
        footer_layout.addStretch()

        self._footer.setVisible(False)

        # Track scroll to show/hide floating footer
        scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Keep references for theme refresh
        self._groups = [app_grp, beh_grp, ov_grp, bk_grp, ed_grp, pm_grp,
                        self._suppression_group,
                        self._excluded_paths_group, det_grp, transfer_grp]
        # (group, i18n key) pairs for retranslating the section titles in
        # place — suppression/excluded groups are retitled separately in
        # update_locale (their keys sit outside the section_* family).
        self._group_title_keys = [
            (app_grp,      "settings.section_appearance"),
            (beh_grp,      "settings.section_behaviour"),
            (ov_grp,       "settings.section_overlay"),
            (bk_grp,       "settings.section_backup_policy"),
            (ed_grp,       "settings.section_save_edit_copies"),
            (pm_grp,       "settings.section_process_monitor"),
            (det_grp,      "settings.section_save_detection"),
            (transfer_grp, "settings.section_config_transfer"),
        ]

        # Connect change signals
        self._connect_change_signals()

    @staticmethod
    def _apply_save_btn_style(btn):
        """Primary form button — painted by the theme's #form_primary_btn."""
        btn.setObjectName("form_primary_btn")

    @staticmethod
    def _apply_cancel_btn_style(btn):
        """Secondary form button — theme rule #form_secondary_btn."""
        btn.setObjectName("form_secondary_btn")

    def _apply_inline_row_style(self):
        self._inline_row.setStyleSheet(
            f"QFrame {{ background: {palette('bg')}; "
            f"border: 1px solid {palette('border')}; "
            f"border-radius: 10px; }}"
        )

    def _apply_footer_style(self):
        self._footer.setStyleSheet(
            f"QFrame {{ background: {palette('bg')}; "
            f"border: 1px solid {palette('border')}; "
            f"border-radius: 10px; }}"
        )

    def _apply_reset_btn_style(self, btn):
        btn.setStyleSheet(
            f"QPushButton {{ color:{palette('warning')}; border:1px solid {palette('warning')}; "
            f"background:transparent; border-radius:6px; padding:7px 16px; font-size:12px; }} "
            f"QPushButton:hover {{ background:{palette('warning')}; color:{palette('accent_text')}; }}"
        )

    def _apply_suppressed_list_style(self):
        self._suppressed_list.setStyleSheet(
            f"QListWidget{{background:{palette('bg_card')};border:1px solid {palette('accent')};border-radius:6px;}}"
            f"QListWidget::item{{padding:4px 8px;color:{palette('text_secondary')};font-size:11px;}}"
            f"QListWidget::item:selected{{background:{palette('bg_elevated')};color:{palette('error')};}}"
        )
        # Its search box is named #list_search and needs nothing here.

    def _apply_prefs_section_styles(self):
        """Single theme-aware styler for the 'game preferences' and 'excluded
        save paths' sections (hints, search boxes, lists) — used both at build
        time and from _refresh_styles. SettingsPage is not rebuilt on theme
        switch (it preserves unsaved form state), so these palette()-based
        inline styles must be re-applied here or they keep old-theme colors.
        """
        _hint = f"color:{palette('text_muted')};font-size:10px;"
        _list = (
            f"QListWidget{{background:{palette('bg_input')};border:1px solid {palette('border')};"
            f"border-radius:4px;font-size:11px;color:{palette('text')};}}"
            f"QListWidget::item{{padding:3px 6px;}}"
            f"QListWidget::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        # The two search boxes are absent from this list on purpose: they are
        # named #list_search and the theme paints them.
        for w, style in (
            (getattr(self, '_supp_hint', None), _hint),
            (getattr(self, '_exp_hint', None), _hint),
            (getattr(self, '_suppression_list', None), _list),
            (getattr(self, '_excluded_paths_list', None), _list),
        ):
            if w is not None:
                try:
                    w.setStyleSheet(style)
                except RuntimeError:
                    pass

    def _connect_change_signals(self):
        self._startup_cb.stateChanged.connect(self._mark_dirty)
        self._tray_cb.stateChanged.connect(self._mark_dirty)
        self._hide_on_game_cb.stateChanged.connect(self._mark_dirty)
        self._backup_on_exit_cb.stateChanged.connect(self._mark_dirty)
        self._auto_sync_cb.stateChanged.connect(self._mark_dirty)
        self._auto_scan_cb.stateChanged.connect(self._mark_dirty)
        self._overlay_launch_cb.stateChanged.connect(self._mark_dirty)
        self._overlay_unknown_cb.stateChanged.connect(self._mark_dirty)
        self._overlay_cloud_cb.stateChanged.connect(self._mark_dirty)
        self._overlay_backup_cb.stateChanged.connect(self._mark_dirty)
        self._max_backups_spin.valueChanged.connect(self._mark_dirty)
        self._retention_spin.valueChanged.connect(self._mark_dirty)
        self._edit_copies_spin.valueChanged.connect(self._mark_dirty)
        self._edit_copy_days_spin.valueChanged.connect(self._mark_dirty)
        self._min_kept_spin.valueChanged.connect(self._mark_dirty)
        self._max_size_spin.valueChanged.connect(self._mark_dirty)
        self._poll_spin.valueChanged.connect(self._mark_dirty)
        self._verify_cb.stateChanged.connect(self._mark_dirty)
        self._verify_days_spin.valueChanged.connect(self._mark_dirty)
        self._correlation_cb.stateChanged.connect(self._mark_dirty)
        self._correlation_spin.valueChanged.connect(self._mark_dirty)
        self._hints_edit.textChanged.connect(self._mark_dirty)
        self._hotkey_edit.textChanged.connect(self._mark_dirty)

    def _make_search_box(self) -> "QLineEdit":
        """Search field used by the exclusion/suppression lists."""
        from PySide6.QtWidgets import QLineEdit
        box = QLineEdit()
        box.setPlaceholderText(t("settings.search_list"))
        box.setFixedHeight(26)
        box.setClearButtonEnabled(True)
        box.setObjectName("list_search")
        return box

    @staticmethod
    def _bind_search_box(box, list_widget):
        """Live-filter *list_widget* rows by the text typed into *box*."""
        def _filter(text: str):
            needle = text.lower().strip()
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                item.setHidden(bool(needle) and needle not in item.text().lower())
        box.textChanged.connect(_filter)

    # notif_type stored in suppressed_ingame_notifs → how to name it in the
    # list. A dict rather than a chain of ifs so a type added to the overlay
    # cannot silently vanish from here: unmapped ones are shown raw (see
    # _suppression_kinds), which is ugly enough to get noticed and fixed.
    _SUPPRESSION_LABELS = {
        "backup":            "settings.suppression_backup_notif",
        "provisional_backup": "settings.suppression_provisional_notif",
        "sync":              "settings.suppression_sync_notif",
        "regression":        "settings.suppression_regression_notif",
    }

    def _game_label(self, game_id: str) -> str:
        """A name for *game_id* that means something to the reader.

        The library is the first source, but these lists outlive it: removing
        a game leaves its id behind in the config, and the label fell back to
        the first 12 characters of a UUID ("805f3c38-540") — which names
        nothing and cannot be matched to anything the user remembers. The
        backup index still knows the title, so it is asked next; only when
        even that is gone does the row say so in words.
        """
        from core.library import get_library
        entry = get_library().get_by_id(game_id)
        if entry is not None and entry.name:
            return entry.name
        try:
            from core.backup import get_backup_manager
            for b in get_backup_manager().get_backups_for_game(game_id):
                if b.game_name:
                    return b.game_name
        except Exception:
            logger.debug("Backup index unavailable for label lookup", exc_info=True)
        return t("settings.removed_game", id=game_id[:8])

    def _suppression_kinds(self, game_id: str, scan_accept: dict,
                           notif_suppress: dict, cloud_no_local) -> list[str]:
        """What is actually suppressed for *game_id*, named for the reader.

        Empty when nothing is: a game whose entry survives with a falsy value
        or an empty type list has no preference to show, and listing it as
        "skip path confirmation" (the old fallback for an empty result) stated
        the opposite of the truth.
        """
        kinds: list[str] = []
        if scan_accept.get(game_id):
            kinds.append(t("settings.suppression_scan"))
        for notif_type in notif_suppress.get(game_id) or []:
            key = self._SUPPRESSION_LABELS.get(notif_type)
            kinds.append(t(key) if key else str(notif_type))
        if game_id in (cloud_no_local or []):
            kinds.append(t("settings.suppression_cloud_no_local"))
        return kinds

    def _load_excluded_paths_list(self):
        """Populate the excluded-save-paths QListWidget from
        auto_scan_deleted_paths (deletions made in the confirmation panels)."""
        from core.config_manager import get_config
        from core.library import get_library
        from PySide6.QtCore import Qt as _Qt

        config = get_config()
        deleted: dict = config.get("auto_scan_deleted_paths", {})

        self._excluded_paths_list.clear()
        entries = [(gid, p) for gid, paths in deleted.items() for p in (paths or [])]
        if not entries:
            placeholder = QListWidgetItem(t("settings.excluded_paths_empty"))
            placeholder.setFlags(_Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(QColor(palette('text_muted')))
            self._excluded_paths_list.addItem(placeholder)
            return

        for gid, path in sorted(entries, key=lambda e: (e[0], e[1].lower())):
            game_name = self._game_label(gid)
            item = QListWidgetItem(f"{game_name}  —  {path}")
            item.setData(_Qt.ItemDataRole.UserRole, (gid, path))
            item.setToolTip(f"{game_name}\n{path}")
            self._excluded_paths_list.addItem(item)
        # Re-apply any active search filter to the fresh items
        self._excluded_paths_search.textChanged.emit(self._excluded_paths_search.text())

    def _restore_selected_excluded_paths(self):
        """Remove the exclusion for the selected paths: they are simply
        deleted from the excluded list, so a later scan can re-find and
        re-propose them."""
        from core.config_manager import get_config
        from PySide6.QtCore import Qt as _Qt
        selected = self._excluded_paths_list.selectedItems()
        if not selected:
            return
        config = get_config()
        deleted: dict = {k: list(v) for k, v in config.get("auto_scan_deleted_paths", {}).items()}
        changed = False
        for item in selected:
            data = item.data(_Qt.ItemDataRole.UserRole)
            if not data:
                continue
            gid, path = data
            if path in deleted.get(gid, []):
                deleted[gid].remove(path)
                if not deleted[gid]:
                    deleted.pop(gid)
                changed = True
        if changed:
            config.set("auto_scan_deleted_paths", deleted)
        self._load_excluded_paths_list()

    def _load_suppression_list(self):
        """Populate the per-game notification-suppression QListWidget."""
        from core.config_manager import get_config
        from core.library import get_library
        from PySide6.QtCore import Qt as _Qt

        config = get_config()
        scan_accept: dict       = config.get("scan_auto_accept_games", {})
        notif_suppress: dict    = config.get("suppressed_ingame_notifs", {})
        cloud_no_local: list    = config.get("suppressed_cloud_no_local", [])

        # Only games that really have something suppressed: an id can outlive
        # its preference (the value emptied, the game deleted), and such rows
        # said "skip path confirmation" while suppressing nothing.
        by_game = {}
        for game_id in set(scan_accept) | set(notif_suppress) | set(cloud_no_local):
            kinds = self._suppression_kinds(
                game_id, scan_accept, notif_suppress, cloud_no_local)
            if kinds:
                by_game[game_id] = kinds
        all_game_ids = set(by_game)

        self._suppression_list.clear()
        # Keep the excluded-paths box in sync (same refresh triggers:
        # page load and navigation to Settings).
        self._load_excluded_paths_list()

        if not all_game_ids:
            placeholder = QListWidgetItem(t("settings.game_suppressions_empty"))
            placeholder.setFlags(_Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(QColor(palette('text_muted')))
            self._suppression_list.addItem(placeholder)
            return

        for game_id in sorted(all_game_ids, key=lambda g: self._game_label(g).lower()):
            game_name = self._game_label(game_id)
            detail = " · ".join(by_game[game_id])
            item = QListWidgetItem(f"{game_name}  —  {detail}")
            item.setData(_Qt.ItemDataRole.UserRole, game_id)
            item.setToolTip(f"{game_name}\n{detail}")
            self._suppression_list.addItem(item)
        # Re-apply any active search filter to the fresh items
        self._suppression_search.textChanged.emit(self._suppression_search.text())

    def _unblock_selected_game_prefs(self):
        """Remove all selected per-game preference entries."""
        from PySide6.QtCore import Qt as _Qt
        selected = self._suppression_list.selectedItems()
        if not selected:
            return
        for item in selected:
            gid = item.data(_Qt.ItemDataRole.UserRole)
            if gid:
                self._remove_suppression(gid)

    def _remove_suppression(self, game_id: str):
        """Remove all suppressions for a specific game and refresh the list."""
        from core.config_manager import get_config
        config = get_config()

        scan_accept: dict = dict(config.get("scan_auto_accept_games", {}))
        scan_accept.pop(game_id, None)
        config.set("scan_auto_accept_games", scan_accept)

        notif_suppress: dict = dict(config.get("suppressed_ingame_notifs", {}))
        notif_suppress.pop(game_id, None)
        config.set("suppressed_ingame_notifs", notif_suppress)

        cloud_no_local: list = list(config.get("suppressed_cloud_no_local", []))
        if game_id in cloud_no_local:
            cloud_no_local.remove(game_id)
            config.set("suppressed_cloud_no_local", cloud_no_local)

        self._load_suppression_list()

    def _mark_dirty(self):
        if self._loading:
            return
        has_changes = self._take_snapshot() != self._snapshot
        self._dirty = has_changes
        # Inline buttons always reflect dirty state
        self._inline_save.setEnabled(has_changes)
        self._inline_cancel.setEnabled(has_changes)
        # Show/hide floating footer
        self._update_footer()

    def _on_scroll(self, *_args):
        self._update_footer()

    def _is_inline_visible(self) -> bool:
        """Inline is visible only when scrollbar is at the very bottom."""
        sb = self._scroll.verticalScrollBar()
        if not sb or sb.maximum() == 0:
            return True  # no scrollbar, everything fits
        return sb.value() >= sb.maximum() - 5

    def _update_footer(self):
        """Show floating footer when dirty AND inline buttons are scrolled out of view."""
        show = self._dirty and not self._is_inline_visible()
        self._footer.setVisible(show)
        if show:
            self._position_footer()

    def _position_footer(self):
        """Align footer exactly over where the inline row would be at the bottom of the scroll."""
        geo = self._scroll.geometry()
        self._footer.setFixedWidth(geo.width())
        self._footer.adjustSize()
        x = geo.x()
        y = geo.y() + geo.height() - self._footer.height()
        self._footer.move(x, y)
        self._footer.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._footer.isVisible():
            self._position_footer()

    def _take_snapshot(self) -> dict:
        return {
            "language": self._lang_combo.currentData(),
            "theme": self._theme_combo.currentData(),
            "hotkey": self._hotkey_edit.text().strip(),
            "startup": self._startup_cb.isChecked(),
            "tray": self._tray_cb.isChecked(),
            "backup_exit": self._backup_on_exit_cb.isChecked(),
            "auto_sync": self._auto_sync_cb.isChecked(),
            "auto_scan": self._auto_scan_cb.isChecked(),
            "overlay_launch": self._overlay_launch_cb.isChecked(),
            "overlay_unknown": self._overlay_unknown_cb.isChecked(),
            "overlay_cloud": self._overlay_cloud_cb.isChecked(),
            "overlay_backup": self._overlay_backup_cb.isChecked(),
            "max_backups": self._max_backups_spin.value(),
            "retention": self._retention_spin.value(),
            "edit_copies": self._edit_copies_spin.value(),
            "edit_copy_days": self._edit_copy_days_spin.value(),
            "min_kept": self._min_kept_spin.value(),
            "max_size": self._max_size_spin.value(),
            "poll": self._poll_spin.value(),
            "verify": self._verify_cb.isChecked(),
            "verify_days": self._verify_days_spin.value(),
            "correlation": self._correlation_cb.isChecked(),
            "correlation_window": self._correlation_spin.value(),
            "ignored": "",  # managed via unified process list, not a textarea
            "hints": self._hints_edit.toPlainText(),
        }

    # ── Load / Save ──────────────────────────────────────────────────────────

    def _load(self, apply_theme_locale=True):
        self._loading = True
        config = get_config()

        self._lang_combo.blockSignals(True)
        locale = config.get("language", "en")
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == locale:
                self._lang_combo.setCurrentIndex(i)
                break
        self._lang_combo.blockSignals(False)
        if apply_theme_locale:
            # Revert runtime locale to saved value
            get_engine().set_locale(locale)

        self._theme_combo.blockSignals(True)
        theme = config.get("theme", "dark")
        for i in range(self._theme_combo.count()):
            if self._theme_combo.itemData(i) == theme:
                self._theme_combo.setCurrentIndex(i)
                break
        self._theme_combo.blockSignals(False)
        if apply_theme_locale:
            # Revert runtime theme to saved value
            from PySide6.QtWidgets import QApplication
            get_theme_manager().apply(theme, QApplication.instance())

        self._hotkey_edit.setText(config.get("overlay_hotkey", "alt+ctrl+s"))
        self._startup_cb.setChecked(get_launch_on_startup())
        self._tray_cb.setChecked(config.get("minimize_to_tray", True))
        self._hide_on_game_cb.setChecked(config.get("hide_to_tray_on_game_launch", True))
        self._backup_on_exit_cb.setChecked(config.get("backup_on_exit", True))
        self._auto_sync_cb.setChecked(config.get("auto_sync_after_backup", False))
        self._auto_scan_cb.setChecked(config.get("auto_scan_on_exit", True))
        self._load_suppression_list()
        self._overlay_launch_cb.setChecked(config.get("show_overlay_on_launch", True))
        self._overlay_unknown_cb.setChecked(config.get("show_overlay_on_unknown", True))
        self._overlay_cloud_cb.setChecked(config.get("show_overlay_on_cloud", True))
        self._overlay_backup_cb.setChecked(config.get("show_overlay_on_backup", True))
        self._max_backups_spin.setValue(config.get("max_local_backups", 6))
        self._retention_spin.setValue(config.get("backup_retention_days", 30))
        self._edit_copies_spin.setValue(config.get("save_edit_copies", 3))
        self._edit_copy_days_spin.setValue(config.get("save_edit_copy_days", 7))
        self._min_kept_spin.setValue(config.get("min_kept_backups", 3))
        self._max_size_spin.setValue(config.get("max_backup_size_mb", 512))
        self._poll_spin.setValue(config.get("process_poll_interval", 1))
        _ver_on = config.get("backup_verify_enabled", True)
        self._verify_cb.setChecked(_ver_on)
        self._verify_days_spin.setValue(config.get("backup_verify_interval_days", 7))
        self._verify_days_spin.setEnabled(_ver_on)
        self._verify_days_lbl.setEnabled(_ver_on)
        _corr_on = config.get("save_correlation_enabled", False)
        self._correlation_cb.setChecked(_corr_on)
        self._correlation_spin.setValue(config.get("save_correlation_window_ms", 1000))
        # toggled doesn't fire when the state doesn't change, so the enabled
        # state of the window row is applied explicitly on load.
        self._correlation_spin.setEnabled(_corr_on)
        self._correlation_lbl.setEnabled(_corr_on)
        # Unified ignored-process list: suppressed_overlay_apps (auto, full path)
        # + any legacy ignored_processes entries that have a path separator
        self._load_unified_ignored_list()
        hints = config.get("save_folder_hints", [])
        self._hints_edit.setPlainText("\n".join(hints))
        self._loading = False

    def _save(self):
        config = get_config()
        old_hotkey = config.get("overlay_hotkey", "alt+ctrl+s")
        new_hotkey = self._hotkey_edit.text().strip() or "alt+ctrl+s"

        if not self._validate_hotkey(new_hotkey):
            self._saved_lbl.setText(t("settings.invalid_hotkey", hotkey=new_hotkey))
            self._saved_lbl.setStyleSheet(f"color: {palette('warning')};")
            self._saved_lbl.setVisible(True)
            def _hide_saved_lbl():
                try:
                    self._saved_lbl.setVisible(False)
                except RuntimeError:
                    pass
            QTimer.singleShot(3000, _hide_saved_lbl)
            return

        config.set("language",               self._lang_combo.currentData())
        config.set("theme",                  self._theme_combo.currentData())
        config.set("overlay_hotkey",         new_hotkey)
        config.set("minimize_to_tray",       self._tray_cb.isChecked())
        config.set("hide_to_tray_on_game_launch", self._hide_on_game_cb.isChecked())
        config.set("backup_on_exit",         self._backup_on_exit_cb.isChecked())
        config.set("auto_sync_after_backup", self._auto_sync_cb.isChecked())
        config.set("auto_scan_on_exit",      self._auto_scan_cb.isChecked())
        config.set("show_overlay_on_launch", self._overlay_launch_cb.isChecked())
        config.set("show_overlay_on_unknown", self._overlay_unknown_cb.isChecked())
        config.set("show_overlay_on_cloud",  self._overlay_cloud_cb.isChecked())
        config.set("show_overlay_on_backup", self._overlay_backup_cb.isChecked())
        config.set("max_local_backups",      self._max_backups_spin.value())
        config.set("backup_retention_days",  self._retention_spin.value())
        config.set("save_edit_copies",       self._edit_copies_spin.value())
        config.set("save_edit_copy_days",    self._edit_copy_days_spin.value())
        config.set("min_kept_backups",       self._min_kept_spin.value())
        config.set("max_backup_size_mb",     self._max_size_spin.value())
        config.set("process_poll_interval",  self._poll_spin.value())
        config.set("backup_verify_enabled",      self._verify_cb.isChecked())
        config.set("backup_verify_interval_days", self._verify_days_spin.value())
        config.set("save_correlation_enabled",   self._correlation_cb.isChecked())
        config.set("save_correlation_window_ms", self._correlation_spin.value())

        # ignored_processes: keep existing entries untouched; removals happen
        # via _unblock_selected which removes from both lists atomically.
        # (The manual textarea was removed — entries are managed via the unified list.)

        hints_raw = self._hints_edit.toPlainText()
        hints = [h.strip() for h in hints_raw.splitlines() if h.strip()]
        config.set("save_folder_hints", hints)

        want_startup = self._startup_cb.isChecked()
        if want_startup != get_launch_on_startup():
            ok = set_launch_on_startup(want_startup)
            if not ok:
                self._startup_cb.setChecked(get_launch_on_startup())

        if old_hotkey != new_hotkey:
            self.hotkey_changed.emit(old_hotkey, new_hotkey)

        from core.monitor import get_monitor
        get_monitor().restart_with_new_interval()

        # Clear pre-change tracking after successful save
        self._pre_change_theme = None
        self._pre_change_lang = None

        self._snapshot = self._take_snapshot()
        self._dirty = False
        self._inline_save.setEnabled(False)
        self._inline_cancel.setEnabled(False)
        self._footer.setVisible(False)

        self._saved_lbl.setText(t("settings.saved"))
        self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
        self._saved_lbl.setVisible(True)
        def _hide_saved_lbl():
            try:
                self._saved_lbl.setVisible(False)
            except RuntimeError:
                pass
        QTimer.singleShot(2000, _hide_saved_lbl)

    def _cancel(self):
        # Revert theme and language to what they were before changes
        config = get_config()
        saved_theme = getattr(self, '_pre_change_theme', None) or config.get("theme", "dark")
        saved_lang = getattr(self, '_pre_change_lang', None) or config.get("language", "en")
        current_theme = self._theme_combo.currentData()
        current_lang = self._lang_combo.currentData()
        if current_theme and current_theme != saved_theme:
            from PySide6.QtWidgets import QApplication
            get_theme_manager().apply(saved_theme, QApplication.instance())
            self._refresh_styles()
            self.theme_changed.emit(saved_theme)
        if current_lang and current_lang != saved_lang:
            get_engine().set_locale(saved_lang)
        # Clear pre-change tracking
        self._pre_change_theme = None
        self._pre_change_lang = None
        self._load(apply_theme_locale=False)
        self._snapshot = self._take_snapshot()
        self._dirty = False
        self._inline_save.setEnabled(False)
        self._inline_cancel.setEnabled(False)
        self._footer.setVisible(False)

    def on_page_leave(self):
        """Called when navigating away from the settings page.
        If there are unsaved changes, revert theme/language to saved values."""
        if self._dirty:
            self._cancel()

    def _unblock_selected(self):
        """Remove selected entries from the unified ignored-process list.

        Removes from both suppressed_overlay_apps (full-path automatic entries)
        and ignored_processes (legacy/manual entries), then clears the monitor's
        seen-exe cache so the process can be detected again.
        """
        selected = [item.text() for item in self._suppressed_list.selectedItems()]
        if not selected:
            return
        config = get_config()

        # Remove from suppressed_overlay_apps
        suppressed = list(config.get("suppressed_overlay_apps", []))
        changed_suppressed = False
        for entry in selected:
            if entry in suppressed:
                suppressed.remove(entry)
                changed_suppressed = True
        if changed_suppressed:
            config.set("suppressed_overlay_apps", suppressed)

        # Also remove from ignored_processes (path-like entries only)
        ignored = list(config.get("ignored_processes", []))
        changed_ignored = False
        for entry in selected:
            if entry in ignored:
                ignored.remove(entry)
                changed_ignored = True
        if changed_ignored:
            config.set("ignored_processes", ignored)

        from core.monitor import get_monitor
        mon = get_monitor()
        for entry in selected:
            mon.clear_seen_exe(entry)
        # Refresh the list widget
        self._load_unified_ignored_list()

    # Config keys that feed the dynamic lists → their reload method. Used
    # both by showEvent (page opened after changes happened elsewhere) and
    # by the live config_changed hook (changes landing WHILE the page is
    # visible — e.g. "don't show again" clicked on an overlay notification
    # with Settings open).
    _LIST_CONFIG_KEYS = {
        "suppressed_overlay_apps":   "_load_unified_ignored_list",
        "ignored_processes":         "_load_unified_ignored_list",
        "suppressed_ingame_notifs":  "_load_suppression_list",
        "scan_auto_accept_games":    "_load_suppression_list",
        "suppressed_cloud_no_local": "_load_suppression_list",
        "auto_scan_deleted_paths":   "_load_excluded_paths_list",
    }

    def showEvent(self, event):
        """Re-read the dynamic lists every time the page becomes visible:
        "don't show again" (unknown-game notifications) and path exclusions
        land in config while the user is elsewhere — without this the table
        showed the state from when the page was first built."""
        super().showEvent(event)
        try:
            self._load_unified_ignored_list()
            self._load_excluded_paths_list()
            self._load_suppression_list()
        except Exception as e:
            logger.debug(f"Settings lists refresh on show failed: {e}")

    def _on_config_changed(self, key: str, _value):
        """Live refresh while the page IS visible. Hidden pages skip (the
        showEvent reload covers them) and loads-in-progress skip (the
        change came from _load itself)."""
        method_name = self._LIST_CONFIG_KEYS.get(key)
        if not method_name:
            return
        if not self.isVisible() or getattr(self, "_loading", False):
            return
        try:
            getattr(self, method_name)()
        except Exception as e:
            logger.debug(f"Live list refresh for {key} failed: {e}")

    def _load_unified_ignored_list(self):
        """Refresh just the unified ignored-process QListWidget."""
        config = get_config()
        self._suppressed_list.clear()
        seen: set[str] = set()
        for exe in config.get("suppressed_overlay_apps", []):
            if exe and exe not in seen:
                seen.add(exe)
                item = QListWidgetItem(exe)
                item.setToolTip(exe)
                self._suppressed_list.addItem(item)
        for proc in config.get("ignored_processes", []):
            if proc and ("/" in proc or "\\" in proc or os.sep in proc):
                if proc not in seen:
                    seen.add(proc)
                    item = QListWidgetItem(proc)
                    item.setToolTip(proc)
                    self._suppressed_list.addItem(item)

    # ── Config Transfer handlers ───────────────────────────────────────────

    def _build_transfer_menu(self, btn, file_action, cloud_action,
                              file_label, cloud_label_key) -> QMenu:
        """Build a styled menu matching the button width with centered text."""
        from PySide6.QtGui import QAction
        from sync import get_orchestrator

        menu = QMenu(self)
        menu.setMinimumWidth(btn.width())
        menu.setStyleSheet(
            f"QMenu {{ padding: 4px 0; }}"
            f"QMenu::item {{ padding: 10px 16px; text-align: center; }}"
        )

        file_act = QAction(file_label, menu)
        file_act.triggered.connect(file_action)
        menu.addAction(file_act)

        orch = get_orchestrator()
        if orch.is_online():
            connected = orch.get_connected_providers()
            if connected:
                from ui.backup_labels import ORIGIN_LABELS
                pid = connected[0].PROVIDER_ID
                name = ORIGIN_LABELS.get(pid, pid)
                menu.addSeparator()
                cloud_act = QAction(t(cloud_label_key, provider=name), menu)
                cloud_act.triggered.connect(cloud_action)
                menu.addAction(cloud_act)

        return menu

    def _on_export_config_menu(self):
        from sync import get_orchestrator
        if not get_orchestrator().is_online():
            self._on_export_config()
            return
        menu = self._build_transfer_menu(
            self._export_btn,
            self._on_export_config,
            self._on_upload_config_cloud,
            t("settings.export_to_file"),
            "settings.export_to_provider",
        )
        menu.exec(self._export_btn.mapToGlobal(self._export_btn.rect().bottomLeft()))

    def _on_import_config_menu(self):
        from sync import get_orchestrator
        if not get_orchestrator().is_online():
            self._on_import_config()
            return
        menu = self._build_transfer_menu(
            self._import_btn,
            self._on_import_config,
            self._on_download_config_cloud,
            t("settings.import_from_file"),
            "settings.import_from_provider",
        )
        menu.exec(self._import_btn.mapToGlobal(self._import_btn.rect().bottomLeft()))

    def _on_download_config_cloud(self):
        """Download and import config from the connected provider."""
        from sync import get_orchestrator
        orch = get_orchestrator()
        if not orch.is_online():
            information_window_modal(self, t("settings.import_config"),
                                    t("sync.provider_disconnected"))
            return
        provider = orch.provider
        if not provider:
            information_window_modal(self, t("settings.import_config"),
                                    t("sync.provider_disconnected"))
            return
        try:
            from core.config_transfer import download_and_parse_cloud_config
            parsed = download_and_parse_cloud_config(provider)
        except ValueError:
            warning_window_modal(self, t("settings.import_config"),
                                t("settings.import_corrupt"))
            return
        except Exception as e:
            warning_window_modal(self, t("settings.import_config"),
                                t("settings.import_failed", error=str(e)[:200]))
            return
        from core.config_transfer import preview_import, apply_import
        preview = preview_import(parsed)
        if preview.get("is_identical"):
            self._saved_lbl.setStyleSheet(f"color: {palette('text_muted')};")
            self._saved_lbl.setText(t("settings.import_identical"))
            self._saved_lbl.setVisible(True)
            return
        from ui.dialogs.config_import_dialog import ConfigImportPreviewDialog
        dlg = ConfigImportPreviewDialog(preview, preview["has_credentials"], self)

        def _do_import(settings, library, creds, strategy):
            try:
                result = apply_import(parsed, settings, library, creds, strategy)
                self._load()
                msg = t("settings.import_success",
                         settings=result["settings_applied"],
                         games_added=result["games_added"],
                         games_merged=result["games_merged"])
                if result.get("credentials_skipped_machine"):
                    msg += f"\n{t('settings.credentials_skipped_machine')}"
                self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
                self._saved_lbl.setText(msg)
                self._saved_lbl.setVisible(True)
            except Exception as e:
                warning_window_modal(self, t("settings.import_config"),
                                    t("settings.import_failed", error=str(e)[:200]))

        dlg.import_confirmed.connect(_do_import)
        dlg.exec()

    def _on_export_config(self):
        from ui.widgets.file_pickers import pick_save_path
        path = pick_save_path(
            self, t("settings.export_config"), "SaveSync Config (*.savesync)",
            default_name="savesync_config.savesync")
        if not path:
            return
        from core.config_transfer import export_config_to_file, save_config_snapshot
        result = export_config_to_file(Path(path))
        if result is True:
            # Record a history snapshot of the exported config so the
            # configuration history reflects each export (mirrors the
            # import/restore snapshot pattern).
            save_config_snapshot("export")
            self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
            self._saved_lbl.setText(t("settings.export_success"))
            self._saved_lbl.setVisible(True)
        elif result is None:
            self._saved_lbl.setStyleSheet(f"color: {palette('text_muted')};")
            self._saved_lbl.setText(t("settings.config_unchanged"))
            self._saved_lbl.setVisible(True)
        else:
            warning_window_modal(self, t("settings.export_config"),
                                t("settings.export_failed", error="I/O error"))

    def _on_import_config(self):
        from ui.widgets.file_pickers import pick_file
        path = pick_file(self, t("settings.import_config"),
                         "SaveSync Config (*.savesync)")
        if not path:
            return
        from core.config_transfer import import_config_from_file, preview_import, apply_import
        try:
            parsed = import_config_from_file(Path(path))
        except ValueError:
            warning_window_modal(self, t("settings.import_config"),
                                t("settings.import_corrupt"))
            return
        preview = preview_import(parsed)
        if preview.get("is_identical"):
            self._saved_lbl.setStyleSheet(f"color: {palette('text_muted')};")
            self._saved_lbl.setText(t("settings.import_identical"))
            self._saved_lbl.setVisible(True)
            return
        from ui.dialogs.config_import_dialog import ConfigImportPreviewDialog
        dlg = ConfigImportPreviewDialog(preview, preview["has_credentials"], self)

        def _do_import(settings, library, creds, strategy):
            try:
                result = apply_import(parsed, settings, library, creds, strategy)
                self._load()
                msg = t("settings.import_success",
                         settings=result["settings_applied"],
                         games_added=result["games_added"],
                         games_merged=result["games_merged"])
                if result.get("credentials_skipped_machine"):
                    msg += f"\n{t('settings.credentials_skipped_machine')}"
                self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
                self._saved_lbl.setText(msg)
                self._saved_lbl.setVisible(True)
            except Exception as e:
                warning_window_modal(self, t("settings.import_config"),
                                    t("settings.import_failed", error=str(e)[:200]))

        dlg.import_confirmed.connect(_do_import)
        dlg.exec()

    def _on_upload_config_cloud(self):
        from sync import get_orchestrator
        orch = get_orchestrator()
        if not orch.is_online():
            information_window_modal(self, t("settings.upload_config_cloud"),
                                    t("sync.provider_disconnected"))
            return
        provider = orch.provider
        if not provider:
            information_window_modal(self, t("settings.upload_config_cloud"),
                                    t("sync.provider_disconnected"))
            return
        from core.config_transfer import upload_config_to_cloud, save_config_snapshot
        result = upload_config_to_cloud(provider)
        if result is True:
            # Record a history snapshot on cloud upload too (same rationale
            # as file export).
            save_config_snapshot("upload")
            self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
            self._saved_lbl.setText(t("settings.upload_success"))
            self._saved_lbl.setVisible(True)
        elif result is None:
            self._saved_lbl.setStyleSheet(f"color: {palette('text_muted')};")
            self._saved_lbl.setText(t("settings.config_unchanged"))
            self._saved_lbl.setVisible(True)
        else:
            warning_window_modal(self, t("settings.upload_config_cloud"),
                                t("settings.upload_failed"))

    def _on_config_history(self):
        from core.config_transfer import list_config_snapshots, restore_config_snapshot
        from ui.dialogs.config_import_dialog import ConfigHistoryDialog
        snaps = list_config_snapshots()
        dlg = ConfigHistoryDialog(snaps, self)

        def _do_restore(snap_path):
            if restore_config_snapshot(snap_path):
                self._load()
                self._saved_lbl.setText(t("config_transfer.restore_success"))
                self._saved_lbl.setVisible(True)

        dlg.restore_confirmed.connect(_do_restore)
        dlg.exec()

    def _validate_hotkey(self, hotkey: str) -> bool:
        if not hotkey:
            return False
        parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
        if not parts:
            return False
        modifiers = {"ctrl", "shift", "alt", "win", "cmd", "super", "meta"}
        has_modifier = any(p in modifiers for p in parts)
        has_key = any(p not in modifiers for p in parts)
        return has_modifier and has_key

    def _reset(self):
        DEFAULTS = {
            "language":               "en",
            "theme":                  "dark",
            "overlay_hotkey":         "alt+ctrl+s",
            "minimize_to_tray":       True,
            "hide_to_tray_on_game_launch": True,
            "backup_on_exit":         True,
            "auto_sync_after_backup": False,
            "auto_scan_on_exit":      True,
            "show_overlay_on_launch": True,
            "show_overlay_on_unknown": True,
            "show_overlay_on_cloud":  True,
            "show_overlay_on_backup": True,
            "max_local_backups":      6,
            "backup_retention_days":  30,
            "min_kept_backups":       3,
            "max_backup_size_mb":     512,
            "process_poll_interval":  1,
            "backup_verify_enabled":      True,
            "backup_verify_interval_days": 7,
            "save_correlation_enabled":   False,
            "save_correlation_window_ms": 1000,
            "ignored_processes":      [],
            "save_folder_hints":      [],
            "suppressed_overlay_apps": [],
        }
        config = get_config()
        for k, v in DEFAULTS.items():
            config.set(k, v)
        self._load()
        self._snapshot = self._take_snapshot()
        self._dirty = False
        self._inline_save.setEnabled(False)
        self._inline_cancel.setEnabled(False)
        self._footer.setVisible(False)
        self._saved_lbl.setText(f"✓ {t('settings.reset_done')}")
        self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
        self._saved_lbl.setVisible(True)
        def _hide_saved_lbl():
            try:
                self._saved_lbl.setVisible(False)
            except RuntimeError:
                pass
        QTimer.singleShot(2500, _hide_saved_lbl)

    # ── Theme / Language ─────────────────────────────────────────────────────

    def _on_language_change(self):
        code = self._lang_combo.currentData()
        if code:
            # Store the original language before applying so _cancel can revert
            if getattr(self, '_pre_change_lang', None) is None:
                self._pre_change_lang = get_config().get("language", "en")
            get_engine().set_locale(code)
            self._mark_dirty()

    def _on_theme_change(self):
        theme = self._theme_combo.currentData()
        if theme:
            # Store the original theme before applying so _cancel can revert
            if getattr(self, '_pre_change_theme', None) is None:
                self._pre_change_theme = get_config().get("theme", "dark")
            from PySide6.QtWidgets import QApplication
            get_theme_manager().apply(theme, QApplication.instance())
            # Don't persist to config yet — wait for _save()
            self.theme_changed.emit(theme)
            self._refresh_styles()
            self._mark_dirty()

    def _refresh_styles(self):
        # The section boxes need nothing: their look is the theme's QGroupBox
        # rule, already re-resolved by the stylesheet swap.
        self._apply_suppressed_list_style()
        self._apply_prefs_section_styles()
        self._apply_inline_row_style()
        self._apply_footer_style()
        # The form buttons need nothing: #form_primary_btn/#form_secondary_btn
        # come from the theme. Only the reset button still carries its own.
        self._apply_reset_btn_style(self._inline_reset)
        self._saved_lbl.setStyleSheet(f"color: {palette('success')};")
        if not self._hotkey_edit._capturing:
            self._hotkey_edit._set_idle_style()

    def update_locale(self):
        self._header.setText(t("settings.title"))
        for grp, key in self._group_title_keys:
            grp.setTitle(t(key))
        self._lang_lbl.setText(t("settings.language"))
        self._theme_lbl.setText(t("settings.theme"))
        # Item order fixed at build time: 0 = dark, 1 = light
        self._theme_combo.setItemText(0, t("settings.theme_dark"))
        self._theme_combo.setItemText(1, t("settings.theme_light"))
        self._hotkey_lbl.setText(t("settings.hotkey"))
        self._startup_cb.setText(t("settings.launch_on_startup"))
        self._tray_cb.setText(t("settings.minimize_to_tray"))
        self._hide_on_game_cb.setText(t("settings.hide_to_tray_on_game_launch"))
        self._hide_on_game_cb.setToolTip(t("settings.hide_to_tray_on_game_launch_tooltip"))
        self._backup_on_exit_cb.setText(t("settings.backup_on_exit"))
        self._backup_on_exit_cb.setToolTip(t("settings.backup_on_exit_tooltip"))
        self._auto_sync_cb.setText(t("settings.auto_sync_after_backup"))
        self._auto_scan_cb.setText(t("settings.auto_scan_on_exit"))
        self._auto_scan_cb.setToolTip(t("settings.auto_scan_on_exit_tooltip"))
        self._lang_combo.setToolTip(t("settings.language_tooltip"))
        self._theme_combo.setToolTip(t("settings.theme_tooltip"))
        self._startup_cb.setToolTip(t("settings.launch_on_startup_tooltip"))
        self._tray_cb.setToolTip(t("settings.minimize_to_tray_tooltip"))
        self._auto_sync_cb.setToolTip(t("settings.auto_sync_after_backup_tooltip"))
        self._overlay_launch_cb.setToolTip(t("settings.show_overlay_on_launch_tooltip"))
        self._overlay_unknown_cb.setToolTip(t("settings.show_overlay_on_unknown_tooltip"))
        self._overlay_cloud_cb.setToolTip(t("settings.show_overlay_on_cloud_tooltip"))
        self._overlay_backup_cb.setToolTip(t("settings.show_overlay_on_backup_tooltip"))
        self._max_backups_spin.setToolTip(t("settings.max_backups_tooltip"))
        self._retention_spin.setToolTip(t("settings.backup_retention_tooltip"))
        self._max_size_spin.setToolTip(t("settings.max_size_mb_tooltip"))
        self._hints_edit.setToolTip(t("settings.save_hints_tooltip"))
        self._overlay_launch_cb.setText(t("settings.show_overlay_on_launch"))
        self._overlay_unknown_cb.setText(t("settings.show_overlay_on_unknown"))
        self._overlay_cloud_cb.setText(t("settings.show_overlay_on_cloud"))
        self._overlay_backup_cb.setText(t("settings.show_overlay_on_backup"))
        self._max_backups_lbl.setText(t("settings.max_backups"))
        self._retention_lbl.setText(t("settings.backup_retention"))
        self._edit_copies_lbl.setText(t("settings.save_edit_copies"))
        self._edit_copies_spin.setToolTip(t("settings.save_edit_copies_tooltip"))
        self._edit_copy_days_lbl.setText(t("settings.save_edit_copy_days"))
        self._edit_copy_days_spin.setToolTip(t("settings.save_edit_copy_days_tooltip"))
        self._edit_copy_days_spin.setSuffix(" " + t("settings.days_suffix"))
        self._min_kept_lbl.setText(t("settings.min_kept_backups"))
        self._min_kept_spin.setToolTip(t("settings.min_kept_backups_tooltip"))
        self._max_size_lbl.setText(t("settings.max_size_mb"))
        self._poll_lbl.setText(t("settings.poll_interval"))
        self._poll_spin.setToolTip(t("settings.poll_interval"))
        self._ignored_lbl.setText(t("settings.ignored_processes_section"))
        self._ignored_hint.setText(t("settings.ignored_processes_hint"))
        self._unblock_btn.setText(t("settings.unblock_selected"))
        self._suppression_group.setTitle(t("settings.game_suppressions_title"))
        self._supp_hint.setText(t("settings.game_suppressions_hint"))
        self._unblock_game_btn.setText(t("settings.unblock_selected"))
        self._exp_hint.setText(t("settings.excluded_paths_hint"))
        self._restore_paths_btn.setText(t("settings.restore_selected"))
        self._export_btn.setText(t("settings.export_config"))
        self._import_btn.setText(t("settings.import_config"))
        self._history_btn.setText(t("settings.config_history"))

        self._hints_lbl.setText(t("settings.save_hints"))
        self._hints_edit.setPlaceholderText(t("settings.save_hints_desc"))
        self._verify_cb.setText(t("settings.backup_verify"))
        self._verify_cb.setToolTip(t("settings.backup_verify_tooltip"))
        self._verify_days_lbl.setText(t("settings.backup_verify_interval"))
        self._verify_days_spin.setSuffix(" " + t("settings.days_suffix"))
        self._verify_days_spin.setToolTip(t("settings.backup_verify_interval_tooltip"))
        self._correlation_cb.setText(t("settings.save_correlation"))
        self._correlation_cb.setToolTip(t("settings.save_correlation_tooltip"))
        self._correlation_lbl.setText(t("settings.save_correlation_window"))
        self._correlation_spin.setToolTip(t("settings.save_correlation_window_tooltip"))
        self._excluded_paths_group.setTitle(t("settings.excluded_paths_title"))
        self._excluded_paths_search.setPlaceholderText(t("settings.search_list"))
        self._suppression_search.setPlaceholderText(t("settings.search_list"))
        self._suppressed_search.setPlaceholderText(t("settings.search_list"))
        # Re-fill both preference lists so their placeholder rows ("no game
        # preferences saved", "no excluded paths") and the per-item
        # suppression-kind labels pick up the new locale.
        self._load_suppression_list()
        self._inline_save.setText(t("settings.save"))
        self._inline_cancel.setText(t("common.cancel"))
        self._inline_reset.setText(t("buttons.reset"))
        self._inline_reset.setToolTip(t("tooltips.reset_defaults"))
        self._save_btn.setText(t("settings.save"))
        self._cancel_btn.setText(t("common.cancel"))
        self._hotkey_edit.update_locale()
