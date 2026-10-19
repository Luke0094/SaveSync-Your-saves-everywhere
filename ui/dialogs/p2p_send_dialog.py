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
    QComboBox, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QMessageBox, QProgressBar,
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

    def __init__(self, entry, parent=None, *, files=None, label="",
                 folder_name="", token="", auto_start=False):
        """Send *entry*'s backup — or, with ``entry=None``, the *files*
        picked by hand and confirmed in P2pSendConfirmDialog:
        ``[(path on disk, relative path)]``, sent as they are. *label* names
        the offer, *folder_name* is the folder they came from. *token* fills
        in the receiver's token (one a friend sent); with *auto_start* the
        send begins as soon as the window is up."""
        super().__init__(parent)
        self._entry = entry
        self._files = list(files or [])
        self._folder_name = folder_name
        if entry is not None:
            self._file_path = Path(entry.zip_path)
            self._display_name = f"{entry.game_name} — {(entry.created_at or '')[:10]}"
        else:
            self._file_path = None
            self._display_name = label or (
                self._files[0][1] if len(self._files) == 1 else
                t("p2p.send_n_files", count=len(self._files)))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t("p2p.send_title") if entry is not None
                            else t("p2p.send_title_file"))
        self._apply_window_chrome()
        self._handle = None
        self._file_size = 0
        try:
            if self._file_path is not None:
                self._file_size = self._file_path.stat().st_size
            else:
                self._file_size = sum(Path(p).stat().st_size for p, _r in self._files)
        except OSError:
            pass
        self._marked_done = False
        # Set once the dialog is closed: a publish still being prepared on the
        # worker thread must not start seeding into a window nobody can see.
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._pump)
        self._send_result.connect(self._on_send_result)
        self._friend_token_ids: list = []
        self._build()
        # Tokens friends have handed over can be picked instead of pasted; the
        # list keeps itself current (each one runs out after ten minutes).
        self._friend_timer = QTimer(self)
        self._friend_timer.setInterval(1000)
        self._friend_timer.timeout.connect(self._refresh_friend_tokens)
        self._friend_timer.start()
        self._refresh_friend_tokens()
        if token:
            self._token_edit.setText(token)
            if auto_start:
                QTimer.singleShot(0, self._on_send_clicked)

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

        self._friend_combo = QComboBox()
        self._friend_combo.setVisible(False)
        self._friend_combo.activated.connect(self._pick_friend_token)
        outer.addWidget(self._friend_combo)

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

        # How much has gone out so far (one full copy = 100%). Shown for every
        # send, one file or many; per-file figures need the receiver's side.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        self._progress.setToolTip(t("p2p.send_progress_tip"))
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        self._speed_lbl = QLabel("")
        self._speed_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._speed_lbl.setVisible(False)
        outer.addWidget(self._speed_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._close_btn = QPushButton(t("common.close"))
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._close_btn)
        outer.addLayout(btn_row)

    def _on_send_clicked(self):
        token = self._token_edit.text().strip()
        if not token or not self._send_btn.isEnabled():
            return
        from core.p2p.transfer import (
            P2pError, P2pLinkError, P2pTokenExpired, seed_from_token)
        try:
            seed_from_token(token)
        except P2pTokenExpired:
            self._status_lbl.setText(t("p2p.send_token_expired"))
            return
        except P2pError:
            self._status_lbl.setText(t("p2p.send_invalid_token"))
            return
        self._send_btn.setEnabled(False)
        self._token_edit.setEnabled(False)

        from core.config_manager import get_config
        from core.machine import get_machine_id
        username = (get_config().get("p2p_username", "") or "").strip()
        machine_id = get_machine_id()
        file_path = self._file_path
        files = self._files
        label = self._display_name
        folder_name = self._folder_name
        if self._entry is not None:
            game_name = self._entry.game_name
            # The archive's own file name is just its backup id; the folder
            # it lives in is what tells the receiver which game this is.
            folder_name = file_path.parent.name
            # publishable_dict, not a raw to_dict(): the receiver is a
            # different machine's SaveSync, same as a cloud provider — this
            # entry's LOCAL-only bookkeeping (last_restored above all: it
            # marks a state as "restored, so don't flag a match as a
            # regression" — meaningful only to the machine that did the
            # restoring) must not travel with it and get imported as if it
            # were a fact about the receiver's own history.
            from core.backup import get_backup_manager
            entry_dict = get_backup_manager().publishable_dict(self._entry)
        self._status_lbl.setText(t("p2p.send_sending"))

        def _deliver(handle, error):
            try:
                self._send_result.emit(handle, error)
            except RuntimeError:
                # The dialog is gone: nobody will ever stop this transfer.
                if handle is not None:
                    handle.stop()

        def _work():
            try:
                if self._entry is not None:
                    from core.p2p.transfer import send_backup
                    handle = send_backup(token, file_path, username, machine_id,
                                         game_name=game_name, backup_entry=entry_dict,
                                         folder_name=folder_name)
                else:
                    # Hand-picked files: sent as they are - no zip, no game, no
                    # backup entry. They land in the receiver's chosen folder.
                    from core.p2p.transfer import send_files
                    handle = send_files(token, files, username, machine_id,
                                        label=label, folder_name=folder_name)
            except P2pLinkError:
                # Never copied to be sent: say why, in the app's language.
                _deliver(None, t("p2p.send_no_link"))
                return
            except Exception as e:
                from core.p2p.transfer import P2pTorError
                if isinstance(e, P2pTorError):
                    # (private mode on at one end only, or no Tor program)
                    _deliver(None, t(f"p2p.tor_{e.reason}"))
                else:
                    _deliver(None, str(e))
                return
            _deliver(handle, None)

        threading.Thread(target=_work, daemon=True).start()

    def _on_send_result(self, handle, error):
        if self._closed:
            # Closed while the offer was being prepared: the transfer that
            # just came back is one nobody is watching.
            if handle is not None:
                handle.stop()
            return
        if error:
            self._status_lbl.setText(t("p2p.send_failed", error=error))
            self._send_btn.setEnabled(True)
            self._token_edit.setEnabled(True)
            return
        self._handle = handle
        self._used_token = self._token_edit.text().strip()
        self._forget_friend_token(self._used_token)
        self._status_lbl.setText(
            t("p2p.send_seeding", name=self._display_name))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._timer.start()

    def _pump(self):
        if self._handle is None:
            return
        self._handle.pump()
        if self._handle.put_confirmed is False:
            # Nobody can pick this offer up: end it, and let the person try
            # again (same token, or a new one) rather than leave them stuck.
            self._timer.stop()
            self._handle.stop()
            self._handle = None
            self._progress.setVisible(False)
            self._speed_lbl.setVisible(False)
            self._status_lbl.setText(
                t("p2p.send_failed", error=t("p2p.send_rejected")))
            self._send_btn.setEnabled(True)
            self._token_edit.setEnabled(True)
            return
        p = self._handle.progress()
        peers = p.get("num_peers", 0)
        if peers > 0 and not self._marked_done:
            self._status_lbl.setText(t("p2p.send_peers", peers=peers))
            from core.p2p.transfer import format_rate
            mbs, mbps = format_rate(p.get("upload_rate", 0))
            self._speed_lbl.setText(t("p2p.speed_up", speed=mbs))
            self._speed_lbl.setToolTip(mbps)
            self._speed_lbl.setVisible(True)
        # Rough "probably done" signal: at least one full copy uploaded.
        uploaded = p.get("all_time_upload", 0)
        if self._file_size:
            self._progress.setValue(min(100, int(uploaded * 100 / self._file_size)))
        if not self._marked_done and self._file_size and uploaded >= self._file_size:
            self._marked_done = True
            self._status_lbl.setText(t("p2p.send_done"))
            self._speed_lbl.setVisible(False)

    # ── Tokens friends sent ──────────────────────────────────────────────

    @staticmethod
    def _friends():
        try:
            from ui.p2p_friends_service import get_friends_service
            return get_friends_service()
        except Exception:
            return None

    def _refresh_friend_tokens(self):
        from ui.p2p_friends_service import format_remaining
        svc = self._friends()
        toks = svc.tokens() if svc is not None else []
        # Only worth offering before a send is under way.
        if self._handle is not None or not self._send_btn.isEnabled():
            toks = []
        ids = [tk.token for tk in toks]
        combo = self._friend_combo
        if ids != self._friend_token_ids:
            self._friend_token_ids = ids
            combo.blockSignals(True)
            combo.clear()
            if toks:
                combo.addItem(t("p2p.send_friend_tokens"), "")
                for tk in toks:
                    combo.addItem("", tk.token)
            combo.blockSignals(False)
        for i, tk in enumerate(toks, start=1):
            f = svc.friend(tk.friend_id)
            name = f.display if f is not None else tk.name
            combo.setItemText(i, t("p2p.send_friend_token_item", name=name,
                                   time=format_remaining(tk.remaining())))
        combo.setVisible(bool(toks))

    def _pick_friend_token(self, index: int):
        token = self._friend_combo.itemData(index)
        if token:
            self._token_edit.setText(token)
            self._token_edit.setFocus()

    def _forget_friend_token(self, token: str):
        """A transfer is under way with this token: it is spent."""
        svc = self._friends()
        if svc is not None and token:
            svc.consume(token)
        self._refresh_friend_tokens()

    def _stop_and_close(self):
        self._closed = True
        self._friend_timer.stop()
        self._timer.stop()
        if self._handle is not None:
            # Called off before it was done: the receiver is told (and removes
            # what it had only partly received); a send that finished just stops.
            (self._handle.stop if self._marked_done else self._handle.cancel)()
            self._handle = None

    def reject(self):
        # Also reached from the window's own close button (QDialog.closeEvent
        # calls it), so the question is asked once, before anything is
        # stopped; answering No leaves the transfer running.
        if self._handle is not None and not self._marked_done:
            reply = question_window_modal(
                self, t("p2p.send_title"), t("p2p.send_close_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def done(self, result):
        # Every way out of the dialog ends here, so the transfer is stopped
        # whichever one was taken.
        self._stop_and_close()
        super().done(result)
