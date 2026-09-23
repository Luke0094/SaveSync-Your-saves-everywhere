"""SaveSync - "Send this save" dialog: paste the receiver's token, publish
the offer, keep seeding until it's picked up. See core.p2p.transfer for the
DHT-mailbox + BitTorrent mechanics this drives.
"""
import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QMessageBox,
)

from i18n import t
from ui.helpers import scaled
from ui.modal_helpers import question_window_modal
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

_POLL_MS = 1000


class P2pSendDialog(QDialog):
    """*entry* is the core.backup.BackupEntry for the one backup being
    sent — its full to_dict() travels with the transfer (inside the
    torrent, not the DHT mailbox — see core.p2p.transfer's module
    docstring) so the receiver can file the download under one of THEIR
    OWN games and restore it normally instead of only getting a bare file.
    """

    _send_result = Signal(object, object)   # (SendHandle | None, error str | None)

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._file_path = Path(entry.zip_path)
        self._display_name = f"{entry.game_name} — {(entry.created_at or '')[:10]}"
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t("p2p.send_title"))
        self._apply_window_chrome()
        self._handle = None
        self._file_size = 0
        try:
            self._file_size = self._file_path.stat().st_size
        except OSError:
            pass
        self._marked_done = False
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._pump)
        self._send_result.connect(self._on_send_result)
        self._build()

    def _apply_window_chrome(self):
        bg = QColor(palette("bg"))
        fg = QColor(palette("text"))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            f"QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}")
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self)
        except Exception:
            pass

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        self.setMinimumWidth(scaled(380, self))

        name_lbl = QLabel(self._display_name)
        name_lbl.setStyleSheet(f"font-weight:700;font-size:{scaled(13, self)}px;")
        name_lbl.setWordWrap(True)
        outer.addWidget(name_lbl)

        self._intro_lbl = QLabel(t("p2p.send_intro"))
        self._intro_lbl.setWordWrap(True)
        self._intro_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._intro_lbl)

        token_row = QHBoxLayout()
        self._token_edit = QLineEdit()
        self._token_edit.setPlaceholderText(t("p2p.send_token_placeholder"))
        self._token_edit.setStyleSheet(
            f"font-family: monospace; font-size: {scaled(13, self)}px; letter-spacing: 1px;")
        self._token_edit.returnPressed.connect(self._on_send_clicked)
        token_row.addWidget(self._token_edit, 1)
        self._send_btn = QPushButton(t("p2p.send_button"))
        self._send_btn.setObjectName("primary_btn")
        self._send_btn.clicked.connect(self._on_send_clicked)
        token_row.addWidget(self._send_btn)
        outer.addLayout(token_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._close_btn = QPushButton(t("common.close"))
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._close_btn)
        outer.addLayout(btn_row)

    def _on_send_clicked(self):
        token = self._token_edit.text().strip()
        if not token:
            return
        self._send_btn.setEnabled(False)
        self._token_edit.setEnabled(False)
        self._status_lbl.setText(t("p2p.send_sending"))

        from core.config_manager import get_config
        from core.machine import get_machine_id
        username = (get_config().get("p2p_username", "") or "").strip()
        machine_id = get_machine_id()
        file_path = self._file_path
        game_name = self._entry.game_name
        # publishable_dict, not a raw to_dict(): the receiver is a
        # different machine's SaveSync, same as a cloud provider — this
        # entry's LOCAL-only bookkeeping (last_restored above all: it
        # marks a state as "restored, so don't flag a match as a
        # regression" — meaningful only to the machine that did the
        # restoring) must not travel with it and get imported as if it
        # were a fact about the receiver's own history.
        from core.backup import get_backup_manager
        entry_dict = get_backup_manager().publishable_dict(self._entry)

        def _work():
            try:
                from core.p2p.transfer import send_backup
                handle = send_backup(token, file_path, username, machine_id,
                                     game_name=game_name, backup_entry=entry_dict)
                self._send_result.emit(handle, None)
            except Exception as e:
                self._send_result.emit(None, str(e))

        threading.Thread(target=_work, daemon=True).start()

    def _on_send_result(self, handle, error):
        if error:
            self._status_lbl.setText(t("p2p.send_failed", error=error))
            self._send_btn.setEnabled(True)
            self._token_edit.setEnabled(True)
            return
        self._handle = handle
        self._status_lbl.setText(
            t("p2p.send_seeding", name=self._display_name))
        self._timer.start()

    def _pump(self):
        if self._handle is None:
            return
        self._handle.pump()
        if self._handle.put_confirmed is False:
            self._status_lbl.setText(
                t("p2p.send_failed", error="the network did not accept the offer"))
            self._timer.stop()
            return
        p = self._handle.progress()
        peers = p.get("num_peers", 0)
        if peers > 0:
            self._status_lbl.setText(t("p2p.send_peers", peers=peers))
        # Rough "probably done" signal: at least one full copy uploaded.
        uploaded = p.get("all_time_upload", 0)
        if not self._marked_done and self._file_size and uploaded >= self._file_size:
            self._marked_done = True
            self._status_lbl.setText(t("p2p.send_done"))

    def _stop_and_close(self):
        self._timer.stop()
        if self._handle is not None:
            self._handle.stop()

    def reject(self):
        if self._handle is not None and not self._marked_done:
            reply = question_window_modal(
                self, t("p2p.send_title"), t("p2p.send_close_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._stop_and_close()
        super().reject()

    def closeEvent(self, event):
        self._stop_and_close()
        super().closeEvent(event)
