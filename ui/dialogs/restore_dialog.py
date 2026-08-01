"""
SaveSync - Restore Dialog
Shows a backup picker for a specific game, then emits restore_confirmed(backup_id).
Called from main_window._restore_game_latest() so the user can choose which backup
to restore rather than silently restoring the latest one.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QWidget
)

from i18n import t
from ui.styles.theme import palette
from ui.backup_labels import ORIGIN_LABELS, origin_badge
from ui.modal_helpers import question_window_modal, warning_window_modal
from core import fmt_size as _fmt_size
from core.backup import get_backup_manager, BackupEntry
from core.library import get_library


def _fmt_dt(iso: str) -> str:
    # created_at is stored as naive UTC — core.to_local_dt converts it (and
    # any aware timestamp) to the user's local timezone for display.
    from core import to_local_dt
    from i18n import format_dt
    dt = to_local_dt(iso)
    if dt is not None:
        return format_dt(dt, "%d %b %Y  %H:%M")
    return iso[:16] if iso else "?"


class RestoreDialog(QDialog):
    """
    Lists all backups for a game and lets the user choose one to restore.
    Emits restore_confirmed(backup_id) on confirmation.
    """
    restore_confirmed = Signal(str)   # backup_id

    def __init__(self, game_id: str, parent=None):
        super().__init__(parent)
        self._game_id = game_id
        entry = get_library().get_by_id(game_id)
        game_name = entry.name if entry else game_id
        self.setWindowTitle(t('restore.window_title', game=game_name))
        self.setMinimumWidth(480)
        self.setMinimumHeight(340)
        # WindowModal come AddGameDialog: l’overlay resta utilizzabile
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._build(game_name)

    def _build(self, game_name: str):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        from i18n import t
        title = QLabel(f"<b>{t('backup.restore_confirm_title')}</b>")
        title.setStyleSheet(f"font-size:15px;color:{palette('text_secondary')};")
        root.addWidget(title)

        sub = QLabel(game_name)
        sub.setStyleSheet(f"color:{palette('text_hint')};font-size:12px;")
        root.addWidget(sub)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # Origin filter
        from PySide6.QtWidgets import QComboBox
        filter_row = QHBoxLayout()
        self._origin_combo = QComboBox()
        self._origin_combo.setFixedWidth(160)
        self._origin_combo.addItem(t("backups.filter_all"), "all")
        self._origin_combo.addItem(t("backups.source_local"), "local")
        try:
            from sync import get_orchestrator
            orch = get_orchestrator()
            provider = orch.provider
            if provider:
                pid = provider.PROVIDER_ID
                name = ORIGIN_LABELS.get(pid, f"☁ {pid}")
                self._origin_combo.addItem(name, pid)
        except Exception:
            pass
        self._origin_combo.currentIndexChanged.connect(self._refresh_list)
        filter_row.addWidget(self._origin_combo)
        filter_row.addStretch()
        root.addLayout(filter_row)

        # Backup list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_content = QWidget()
        self._scroll_content.setObjectName("transparent_bg")
        self._list_layout = QVBoxLayout(self._scroll_content)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        scroll.setWidget(self._scroll_content)
        root.addWidget(scroll, 1)

        self._load_backups()
        self._refresh_list()

        # Cancel button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t("common.cancel"))
        cancel_btn.setFixedHeight(32)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _load_backups(self):
        """Load local and cloud backups for the game."""
        self._local_backups = get_backup_manager().get_backups_for_game(self._game_id)
        local_ids = {b.backup_id for b in self._local_backups}
        self._cloud_only = {}
        self._cloud_entries: list[BackupEntry] = []
        try:
            from sync import get_orchestrator
            from core.constants import get_install_folder_name
            from core.library import get_library
            orch = get_orchestrator()
            entry = get_library().get_by_id(self._game_id)
            provider = orch.provider
            if provider and entry:
                game_folder = get_install_folder_name(entry.exe_path, entry.name, self._game_id, entry.computed_folder_name)
                for rd in provider.list_cloud_backups(game_folder):
                    bid = rd.get("backup_id", "")
                    if bid and bid not in local_ids:
                        rd["origin"] = provider.PROVIDER_ID
                        try:
                            be = BackupEntry.from_dict(rd)
                            self._cloud_entries.append(be)
                            self._cloud_only[bid] = rd
                        except Exception:
                            pass
        except Exception:
            pass

    def _refresh_list(self):
        """Rebuild the backup row list with the current origin filter."""
        # Clear previous rows
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filt = self._origin_combo.currentData() or "all"

        # Filter backups
        filtered: list[tuple[BackupEntry, bool]] = []  # (entry, is_cloud_only)
        for bk in self._local_backups:
            origin = getattr(bk, "origin", "local")
            synced_to = bk.cloud_metadata.get("synced_to", [])
            if (
                filt == "all"
                or (filt == "local" and origin == "local")
                or filt == origin
                or filt in synced_to
            ):
                filtered.append((bk, False))
        if filt != "local":
            for bk in self._cloud_entries:
                origin = getattr(bk, "origin", "local")
                if filt == "all" or filt == origin:
                    filtered.append((bk, True))

        filtered.sort(key=lambda x: x[0].created_dt, reverse=True)

        if not filtered:
            empty = QLabel(t("backup.no_backups"))
            empty.setStyleSheet(f"color:{palette('text_muted')};font-size:13px;padding:16px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_layout.addWidget(empty)
        else:
            for bk, cloud_only in filtered:
                self._list_layout.addWidget(self._make_row(bk, cloud_only=cloud_only))
        self._list_layout.addStretch()

    def _make_row(self, bk: BackupEntry, cloud_only: bool = False) -> QWidget:
        row_w = QFrame()
        row_w.setObjectName("restore_row")
        row_w.setFrameShape(QFrame.Shape.NoFrame)
        row_w.setStyleSheet(f"""
            QFrame#restore_row {{ background:{palette('bg_card')}; border:1px solid {palette('border')}; border-radius:6px; }}
            QFrame#restore_row:hover {{ border-color:{palette('border_hover')}; background:{palette('bg_hover')}; }}
        """)
        row = QHBoxLayout(row_w)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)

        # Info column — use text_muted instead of text_hint/text_faint
        # so metadata is readable on dark backgrounds
        date_lbl = QLabel(_fmt_dt(bk.created_at))
        date_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;font-weight:600;")

        origin_lbl = QLabel(origin_badge(bk))
        origin_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        size_lbl = QLabel(_fmt_size(bk.size_bytes))
        size_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        mid_lbl = QLabel(f"🖥 {bk.machine_id[:8]}" if bk.machine_id else "")
        mid_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;")

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        info_col.addWidget(date_lbl)
        sub_row = QHBoxLayout(); sub_row.setSpacing(8)
        sub_row.addWidget(origin_lbl)
        sub_row.addWidget(size_lbl)
        sub_row.addWidget(mid_lbl)
        if bk.note:
            note_lbl = QLabel(bk.note)
            note_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;font-style:italic;")
            sub_row.addWidget(note_lbl)
        sub_row.addStretch()
        info_col.addLayout(sub_row)

        row.addLayout(info_col, 1)

        restore_btn = QPushButton(t("buttons.restore"))
        restore_btn.setFixedHeight(28)
        restore_btn.setFixedWidth(80)
        restore_btn.setStyleSheet(
            f"QPushButton {{ background:{palette('accent')}; color:{palette('accent_text')};"
            f"border:none; border-radius:4px; font-size:10px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{palette('accent_hover')}; }}"
        )
        if cloud_only:
            restore_btn.clicked.connect(lambda _, bid=bk.backup_id: self._download_and_restore(bid))
        else:
            restore_btn.clicked.connect(lambda _, bid=bk.backup_id: self._confirm(bid))
        row.addWidget(restore_btn)

        return row_w

    def _confirm(self, backup_id: str):
        from PySide6.QtWidgets import QMessageBox
        bk = get_backup_manager().get_backup(backup_id)
        if bk:
            dt = _fmt_dt(bk.created_at)
            reply = question_window_modal(
                self, t("backup.restore_confirm_title"),
                t("backup.restore_confirm_body", date=dt),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.restore_confirmed.emit(backup_id)
        self.accept()

    def _download_and_restore(self, backup_id: str):
        """Download a cloud-only backup, import it locally, then restore."""
        from PySide6.QtWidgets import QMessageBox
        rd = self._cloud_only.get(backup_id)
        if not rd:
            return
        reply = question_window_modal(
            self, t("backup.restore_confirm_title"),
            t("restore.download_and_restore_confirm", date=_fmt_dt(rd.get("created_at", ""))),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Download in background thread to avoid freezing the UI
        from sync import get_orchestrator
        from core.constants import get_install_folder_name
        from core.library import get_library

        orch = get_orchestrator()
        entry = get_library().get_by_id(self._game_id)
        provider = orch.provider
        if not provider or not entry:
            warning_window_modal(self, t("common.error"), t("sync.provider_disconnected"))
            return

        game_folder = get_install_folder_name(entry.exe_path, entry.name, self._game_id, entry.computed_folder_name)
        remote_zip = f"SaveSync/backup/{game_folder}/{backup_id}.zip"
        _backup_id = backup_id
        _rd = rd

        import threading
        def _bg():
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
                    be = BackupEntry.from_dict(_rd)
                    zip_data = tmp_path.read_bytes()
                    tmp_path.unlink(missing_ok=True)
                    if not get_backup_manager().import_backup(be, zip_data):
                        error = t("restore.import_failed")
            except Exception as e:
                error = str(e)[:200]
            from PySide6.QtCore import QTimer
            def _done():
                try:
                    if error:
                        warning_window_modal(self, t("common.error"), error)
                    else:
                        self.restore_confirmed.emit(_backup_id)
                        self.accept()
                except RuntimeError:
                    pass
            QTimer.singleShot(0, _done)

        threading.Thread(target=_bg, daemon=True).start()

