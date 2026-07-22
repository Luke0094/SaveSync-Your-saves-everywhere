"""
SaveSync - Configuration Import Dialogs
Preview, history, and cloud config prompt dialogs.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QRadioButton, QButtonGroup, QFrame, QListWidget,
    QListWidgetItem, QMessageBox,
)

from i18n import t
from ui.modal_helpers import question_window_modal
from ui.styles.theme import palette


def _styled_title(text: str) -> QLabel:
    """Create a dialog title label matching the app's dialog style."""
    lbl = QLabel(text)
    lbl.setObjectName("page_header")
    lbl.setStyleSheet("font-size: 18px;")
    return lbl


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet(f"color: {palette('separator')};")
    return sep


def _accent_btn(text: str) -> QPushButton:
    """Primary action button styled with the accent palette."""
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ background:{palette('accent')}; color:{palette('accent_text')}; "
        f"border:1px solid {palette('accent')}; border-radius:6px; padding:7px 16px; "
        f"font-size:12px; font-weight:600; }} "
        f"QPushButton:hover {{ background:{palette('accent_hover')}; }}"
    )
    return btn


def _secondary_btn(text: str) -> QPushButton:
    """Secondary action button with subtle border."""
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ background:{palette('bg')}; color:{palette('text_secondary')}; "
        f"border:1px solid {palette('border')}; border-radius:6px; padding:7px 16px; "
        f"font-size:12px; }} "
        f"QPushButton:hover {{ border-color:{palette('accent')}; color:{palette('accent')}; }}"
    )
    return btn


def _danger_btn(text: str) -> QPushButton:
    """Destructive action button (delete, etc.)."""
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ color:{palette('error')}; border:1px solid {palette('error')}; "
        f"background:transparent; border-radius:6px; padding:7px 16px; font-size:12px; }} "
        f"QPushButton:hover {{ background:{palette('error')}; color:{palette('accent_text')}; }}"
    )
    return btn


def _muted_btn(text: str) -> QPushButton:
    """Very subtle button for low-priority actions."""
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ color:{palette('text_muted')}; border:1px solid {palette('border_subtle')}; "
        f"background:transparent; border-radius:6px; padding:7px 16px; font-size:11px; }} "
        f"QPushButton:hover {{ border-color:{palette('text_muted')}; color:{palette('text_secondary')}; }}"
    )
    return btn


class ConfigImportPreviewDialog(QDialog):
    """Shows a preview of what will be imported and lets user choose options."""

    import_confirmed = Signal(bool, bool, bool, str)  # settings, library, creds, strategy

    def __init__(self, preview: dict, has_credentials: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("config_transfer.preview_title"))
        self.setMinimumWidth(480)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._preview = preview
        self._build(preview, has_credentials)

    def _build(self, preview: dict, has_credentials: bool):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        layout.addWidget(_styled_title(t("config_transfer.preview_title")))

        # Source info
        info_text = (
            f"{t('config_transfer.source_machine', name=preview['source_machine'], id=preview['source_machine_id'][:8])}\n"
            f"{t('config_transfer.exported_at', date=preview['exported_at'][:19].replace('T', ' '))}"
        )
        info = QLabel(info_text)
        info.setStyleSheet(f"color: {palette('text_secondary')}; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addWidget(_separator())

        # Summary
        summary_parts = []
        n_diff = len(preview.get("settings_diff", []))
        n_new = len(preview.get("games_new", []))
        n_existing = len(preview.get("games_existing", []))
        n_invalid = len(preview.get("games_invalid_paths", []))

        if n_diff:
            summary_parts.append(t("config_transfer.settings_changes", count=n_diff))
        if n_new:
            summary_parts.append(t("config_transfer.new_games", count=n_new))
        if n_existing:
            summary_parts.append(t("config_transfer.existing_games", count=n_existing))
        if n_invalid:
            summary_parts.append(t("config_transfer.invalid_paths", count=n_invalid))

        if summary_parts:
            summary = QLabel("\n".join(summary_parts))
            summary.setStyleSheet(f"color: {palette('text')}; font-size: 12px; line-height: 1.6;")
            summary.setWordWrap(True)
            layout.addWidget(summary)

        if n_invalid:
            note = QLabel(t("config_transfer.invalid_paths_note"))
            note.setStyleSheet(f"color: {palette('warning')}; font-size: 11px;")
            note.setWordWrap(True)
            layout.addWidget(note)

        layout.addWidget(_separator())

        # Checkboxes
        self._chk_settings = QCheckBox(t("config_transfer.import_settings"))
        self._chk_settings.setChecked(True)
        layout.addWidget(self._chk_settings)

        self._chk_library = QCheckBox(t("config_transfer.import_library"))
        self._chk_library.setChecked(True)
        layout.addWidget(self._chk_library)

        self._chk_creds = QCheckBox(t("config_transfer.import_credentials"))
        self._chk_creds.setChecked(has_credentials)
        self._chk_creds.setEnabled(has_credentials)
        layout.addWidget(self._chk_creds)

        # Merge strategy (only relevant if there are existing games)
        if n_existing > 0:
            layout.addWidget(_separator())

            merge_label = QLabel(t("config_transfer.merge_strategy_label"))
            merge_label.setStyleSheet(f"font-weight: bold; font-size: 12px; color: {palette('text')};")
            layout.addWidget(merge_label)

            self._strategy_group = QButtonGroup(self)
            self._rb_keep = QRadioButton(t("config_transfer.merge_keep_local"))
            self._rb_keep.setChecked(True)
            self._rb_prefer = QRadioButton(t("config_transfer.merge_prefer_imported"))
            self._rb_merge = QRadioButton(t("config_transfer.merge_combine"))
            self._strategy_group.addButton(self._rb_keep)
            self._strategy_group.addButton(self._rb_prefer)
            self._strategy_group.addButton(self._rb_merge)
            layout.addWidget(self._rb_keep)
            layout.addWidget(self._rb_prefer)
            layout.addWidget(self._rb_merge)
        else:
            self._rb_keep = None

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        cancel_btn = _secondary_btn(t("common.cancel"))
        cancel_btn.clicked.connect(self.reject)

        import_btn = _accent_btn(t("config_transfer.confirm_import"))
        import_btn.clicked.connect(self._on_confirm)

        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(import_btn)
        layout.addLayout(btn_row)

    def _on_confirm(self):
        strategy = "keep_local"
        if self._rb_keep is not None:
            if self._rb_prefer.isChecked():
                strategy = "prefer_imported"
            elif self._rb_merge.isChecked():
                strategy = "merge"
        self.import_confirmed.emit(
            self._chk_settings.isChecked(),
            self._chk_library.isChecked(),
            self._chk_creds.isChecked(),
            strategy,
        )
        self.accept()


class ConfigHistoryDialog(QDialog):
    """Lists config snapshots and allows restore/delete."""

    restore_confirmed = Signal(object)  # Path

    def __init__(self, snapshots: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("config_transfer.history_title"))
        self.setMinimumWidth(420)
        self.setMinimumHeight(320)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._snapshots = snapshots
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        layout.addWidget(_styled_title(t("config_transfer.history_title")))

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background:{palette('bg_card')}; border:1px solid {palette('border')}; "
            f"border-radius:6px; }} "
            f"QListWidget::item {{ padding:6px 10px; color:{palette('text_secondary')}; font-size:12px; }} "
            f"QListWidget::item:selected {{ background:{palette('bg_elevated')}; color:{palette('accent')}; }}"
        )
        if not self._snapshots:
            self._list.addItem(t("config_transfer.history_empty"))
        else:
            for snap in self._snapshots:
                ts = snap["timestamp"][:19].replace("T", " ")
                label = snap.get("label", "")
                display = f"{ts}"
                if label:
                    display += f"  ({label})"
                machine = snap.get("machine_name", "")
                if machine:
                    display += f"  [{machine}]"
                item = QListWidgetItem(display)
                # Store as string — Qt variant serialization may not handle Path objects
                item.setData(Qt.ItemDataRole.UserRole, str(snap["path"]))
                self._list.addItem(item)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        delete_btn = _danger_btn(t("config_transfer.delete_snapshot"))
        delete_btn.clicked.connect(self._on_delete)

        restore_btn = _accent_btn(t("config_transfer.restore_snapshot"))
        restore_btn.clicked.connect(self._on_restore)

        close_btn = _secondary_btn(t("common.close"))
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        btn_row.addWidget(restore_btn)
        layout.addLayout(btn_row)

    def _on_restore(self):
        item = self._list.currentItem()
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        reply = question_window_modal(
            self, t("config_transfer.history_title"),
            t("config_transfer.restore_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.restore_confirmed.emit(Path(path))
            self.accept()

    def _on_delete(self):
        item = self._list.currentItem()
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            from core.config_transfer import delete_config_snapshot
            if delete_config_snapshot(Path(path)):
                row = self._list.row(item)
                self._list.takeItem(row)


class CloudConfigPromptDialog(QDialog):
    """Prompt shown when a cloud config from another machine is detected."""

    import_requested = Signal(str)    # passphrase
    skipped = Signal(bool)            # never_ask

    def __init__(self, cloud_info: dict, provider_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("config_transfer.cloud_config_found"))
        self.setMinimumWidth(420)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._build(cloud_info, provider_name)

    def _build(self, cloud_info: dict, provider_name: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        layout.addWidget(_styled_title(t("config_transfer.cloud_config_found")))

        remote_meta = cloud_info.get("remote_meta")
        mod_str = ""
        if remote_meta and remote_meta.modified_at:
            try:
                from i18n import format_dt
                mod_str = format_dt(remote_meta.modified_at, "%d %b %Y, %H:%M")
            except Exception:
                pass

        desc = QLabel(t("config_transfer.cloud_config_prompt",
                        name=provider_name or "cloud", date=mod_str))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {palette('text_secondary')}; line-height: 1.5;")
        layout.addWidget(desc)

        layout.addWidget(_separator())

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        never_btn = _muted_btn(t("config_transfer.cloud_never_ask"))
        never_btn.clicked.connect(lambda: self._skip(True))

        skip_btn = _secondary_btn(t("config_transfer.cloud_skip"))
        skip_btn.clicked.connect(lambda: self._skip(False))

        import_btn = _accent_btn(t("config_transfer.cloud_import"))
        import_btn.clicked.connect(self._on_import)

        btn_row.addWidget(never_btn)
        btn_row.addStretch()
        btn_row.addWidget(skip_btn)
        btn_row.addWidget(import_btn)
        layout.addLayout(btn_row)

    def _on_import(self):
        self.import_requested.emit("")
        self.accept()

    def _skip(self, never_ask: bool):
        self.skipped.emit(never_ask)
        self.reject()
