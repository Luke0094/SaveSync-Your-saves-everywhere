"""SaveSync - "Receive a save" dialog: generate a one-time token, wait for
a sender to publish an offer against it, confirm before downloading
anything. See core.p2p.transfer for the actual DHT-mailbox + BitTorrent
mechanics this drives.

Once downloaded, the save is filed as an ARCHIVE — a backup with no
matching library game, the exact same shape "Aggiungi percorso" and a
cloud-only backup for an unrecognised game already produce (see
core.backup.get_orphan_backups: "Backups marked orphan, or whose game_id
is not in the library"). There is no need to ask which of the receiver's
games it belongs to right now, or to add a new one — it shows up under
"archive" in the Backups page immediately, and restoring it (whenever that
is actually wanted) is where the existing archive-restore flow already
asks where the files should go, via a target folder rather than a library
save_path. Filed through the same core.backup.import_backup path a
cloud-synced backup uses, so nothing here is a special case for restore to
handle differently.
"""
import hashlib
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QProgressBar, QFrame,
)

from i18n import t
from ui.helpers import scaled, open_in_file_manager
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

_POLL_MS = 1000


class P2pReceiveDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t("p2p.receive_title"))
        self._apply_window_chrome()
        self._session = None
        self._accepted_offer = False
        self._saved_path = None
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._pump)
        self._build()
        self._start_session()

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

        self._intro_lbl = QLabel(t("p2p.receive_intro"))
        self._intro_lbl.setWordWrap(True)
        outer.addWidget(self._intro_lbl)

        token_row = QHBoxLayout()
        self._token_edit = QLineEdit()
        self._token_edit.setReadOnly(True)
        self._token_edit.setStyleSheet(
            f"font-family: monospace; font-size: {scaled(13, self)}px; letter-spacing: 1px;")
        token_row.addWidget(self._token_edit, 1)
        self._copy_btn = QPushButton(t("p2p.receive_copy"))
        self._copy_btn.clicked.connect(self._copy_token)
        token_row.addWidget(self._copy_btn)
        outer.addLayout(token_row)

        self._status_lbl = QLabel(t("p2p.receive_waiting"))
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._status_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        # ── Offer (hidden until one arrives) ────────────────────────────
        self._offer_title_lbl = QLabel()
        self._offer_title_lbl.setStyleSheet(f"font-weight:700;font-size:{scaled(13, self)}px;")
        self._offer_title_lbl.setWordWrap(True)
        self._offer_title_lbl.setVisible(False)
        outer.addWidget(self._offer_title_lbl)

        self._offer_detail_lbl = QLabel()
        self._offer_detail_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._offer_detail_lbl.setVisible(False)
        outer.addWidget(self._offer_detail_lbl)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._decline_btn = QPushButton(t("p2p.receive_decline"))
        self._decline_btn.setVisible(False)
        self._decline_btn.clicked.connect(self._decline)
        btn_row.addWidget(self._decline_btn)
        self._accept_btn = QPushButton(t("p2p.receive_accept"))
        self._accept_btn.setObjectName("primary_btn")
        self._accept_btn.setVisible(False)
        self._accept_btn.clicked.connect(self._accept)
        btn_row.addWidget(self._accept_btn)
        self._open_folder_btn = QPushButton(t("p2p.receive_open_folder"))
        self._open_folder_btn.setVisible(False)
        self._open_folder_btn.clicked.connect(self._open_folder)
        btn_row.addWidget(self._open_folder_btn)
        outer.addLayout(btn_row)

        bottom_row = QHBoxLayout()
        self._new_token_btn = QPushButton(t("p2p.receive_new_token"))
        self._new_token_btn.clicked.connect(self._start_session)
        bottom_row.addWidget(self._new_token_btn)
        bottom_row.addStretch()
        close_btn = QPushButton(t("p2p.receive_close"))
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        outer.addLayout(bottom_row)

    # ── Session lifecycle ────────────────────────────────────────────────

    def _start_session(self):
        """Fresh token, fresh session — used both on open and for
        "New token" (a token is meant to work once; asking for a new one is
        how someone abandons a stale wait)."""
        if self._session is not None:
            self._session.close()
        try:
            from core.p2p.transfer import ReceiveSession
            self._session = ReceiveSession()
        except Exception as e:
            logger.warning(f"P2P receive: could not start a session: {e}")
            self._session = None
            self._token_edit.setText("")
            self._status_lbl.setText(t("p2p.receive_error", error=str(e)))
            return
        self._token_edit.setText(self._session.token)
        self._accepted_offer = False
        self._saved_path = None
        self._status_lbl.setText(t("p2p.receive_waiting"))
        self._offer_title_lbl.setVisible(False)
        self._offer_detail_lbl.setVisible(False)
        self._accept_btn.setVisible(False)
        self._decline_btn.setVisible(False)
        self._open_folder_btn.setVisible(False)
        self._progress.setVisible(False)
        self._new_token_btn.setEnabled(True)
        self._timer.start()

    def _copy_token(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._token_edit.text())
        self._copy_btn.setText(t("p2p.receive_copied"))
        QTimer.singleShot(1500, lambda: self._copy_btn.setText(t("p2p.receive_copy")))

    def _pump(self):
        if self._session is None:
            return
        self._session.pump()
        if not self._accepted_offer and self._session.offer is not None:
            self._show_offer()
        elif self._accepted_offer:
            self._update_download_progress()

    def _show_offer(self):
        offer = self._session.offer
        name = (offer.sender_username or "").strip()
        title = (t("p2p.receive_offer_title", name=name) if name
                else t("p2p.receive_offer_title_anon"))
        self._offer_title_lbl.setText(title)
        self._offer_title_lbl.setVisible(True)
        from core import fmt_size
        self._offer_detail_lbl.setText(
            t("p2p.receive_offer_detail", filename=offer.filename,
              size=fmt_size(offer.size)))
        self._offer_detail_lbl.setVisible(True)
        self._accept_btn.setVisible(True)
        self._decline_btn.setVisible(True)
        self._new_token_btn.setEnabled(False)

    def _accept(self):
        if self._session is None or self._session.offer is None:
            return
        from core.constants import USER_DATA_DIR
        dest_dir = USER_DATA_DIR / "p2p_received"
        try:
            self._session.accept(dest_dir)
        except Exception as e:
            self._status_lbl.setText(t("p2p.receive_error", error=str(e)))
            return
        self._accepted_offer = True
        self._accept_btn.setVisible(False)
        self._decline_btn.setVisible(False)
        self._progress.setVisible(True)
        self._status_lbl.setText(t("p2p.receive_downloading", percent=0))

    def _decline(self):
        # A token is single-use by design — declining one offer means
        # starting over with a fresh token rather than re-arming this one,
        # so a token that was seen once cannot quietly be reused for a
        # second, different offer.
        self._start_session()

    def _update_download_progress(self):
        if self._session is None:
            return
        p = self._session.progress()
        if p is None:
            return
        pct = int(round((p.get("progress") or 0) * 100))
        self._progress.setValue(pct)
        self._status_lbl.setText(t("p2p.receive_downloading", percent=pct))
        if self._session.download_error:
            self._status_lbl.setText(
                t("p2p.receive_error", error=self._session.download_error))
            self._timer.stop()
            return
        if self._session.download_done or pct >= 100:
            self._timer.stop()
            self._finish_download()

    def _finish_download(self):
        try:
            entry_dict, zip_path = self._session.finalize()
        except Exception as e:
            self._status_lbl.setText(t("p2p.receive_error", error=str(e)))
            return
        self._saved_path = zip_path
        imported_name = self._import_as_archive(entry_dict, zip_path)
        if imported_name:
            self._status_lbl.setText(t("p2p.receive_imported_detail", name=imported_name))
            self._progress.setVisible(False)
            return
        # No entry to import against (an older SaveSync on the sender's
        # side), or the import itself failed — the download is still
        # there, just not filed anywhere; fall back to a bare save.
        self._status_lbl.setText(t("p2p.receive_done_detail", path=str(self._saved_path)))
        self._progress.setVisible(False)
        self._open_folder_btn.setVisible(True)

    def _import_as_archive(self, entry_dict: dict, zip_path) -> str:
        """File the downloaded save as an ARCHIVE — a backup with no
        matching library game, same as "Aggiungi percorso" or a cloud-only
        backup for an unrecognised game (see core.backup.get_orphan_backups)
        — through the same import_backup() path a cloud-synced backup uses.

        The synthetic game_id is derived from the game's own computed
        folder name (same helper _game_folder_for_entry/get_install_folder_
        name use everywhere else) — NOT from who sent it. Two different
        friends sending the same game land in the same archive and share
        one rotation bucket, exactly like two backups of the same game
        would; only the per-game AMOUNT is what p2p_max_per_game limits,
        never "amount per sender". Returns the game name on success, "" on
        failure (caller falls back to a bare save).
        """
        offer = self._session.offer if self._session else None
        if not entry_dict or offer is None:
            return ""
        try:
            from datetime import datetime, timezone
            from core.backup import BackupEntry, get_backup_manager
            from core.constants import get_install_folder_name

            sender_machine = offer.sender_machine_id or ""
            game_name = str(entry_dict.get("game_name") or offer.game_name or offer.filename)
            folder_name = get_install_folder_name(
                str(entry_dict.get("exe_path") or ""), game_name)
            key = hashlib.sha1(folder_name.casefold().encode("utf-8")).hexdigest()[:16]
            game_id = f"p2p_{key}"
            zip_bytes = zip_path.read_bytes()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            entry = BackupEntry(
                game_id=game_id,
                game_name=game_name,
                backup_id=f"{game_id}_{stamp}",
                created_at=datetime.now(timezone.utc).isoformat(),
                machine_id=sender_machine,
                save_paths=list(entry_dict.get("save_paths") or []),
                zip_path="",
                size_bytes=len(zip_bytes),
                note=t("p2p.receive_archive_note",
                      name=(offer.sender_username or sender_machine[:8] or "?")),
                exe_path=str(entry_dict.get("exe_path") or ""),
                save_chains=list(entry_dict.get("save_chains") or []),
                content_chains=list(entry_dict.get("content_chains") or []),
                origin="p2p",
            )
            bm = get_backup_manager()
            if not bm.import_backup(entry, zip_bytes):
                return ""
            bm.enforce_p2p_limits(game_id)
            return game_name
        except Exception as e:
            logger.warning(f"P2P receive: could not import as an archive: {e}")
            return ""

    def _open_folder(self):
        if self._saved_path is not None:
            open_in_file_manager(self._saved_path)

    def closeEvent(self, event):
        self._timer.stop()
        if self._session is not None:
            self._session.close()
        super().closeEvent(event)

    def reject(self):
        self._timer.stop()
        if self._session is not None:
            self._session.close()
        super().reject()
