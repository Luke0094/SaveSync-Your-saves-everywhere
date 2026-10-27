"""SaveSync - "Receive a save" panel: shows the one-time token, an incoming
offer (confirm before downloading anything) and the download's progress.

It is only a VIEW. The session itself lives in ui.p2p_receive_service, which
keeps the token (good for 10 minutes from when it was made, and shown here
counting down) alive with this panel closed, reopens the panel when an offer
arrives and carries on downloading with it closed — see there, and
core.p2p.transfer for the actual DHT-mailbox + BitTorrent mechanics. Next to
Copy, the token can be handed straight to a friend (ui.p2p_friends_service).
"""
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QProgressBar, QFrame, QTreeWidget, QTreeWidgetItem, QHeaderView, QMenu,
    QMessageBox,
)

from i18n import t
from ui.helpers import scaled, open_in_file_manager
from ui.p2p_friends_service import format_remaining
from ui.p2p_receive_service import (
    WAITING, OFFER, DOWNLOADING, DONE, ERROR,
)
from ui.styles.theme import palette
from ui.widgets.presence_dot import dot_icon
from ui.widgets.transfer_bar import transfer_bar
from ui.widgets.transfer_files_view import TransferFilesView

logger = logging.getLogger(__name__)

# How many received file names the panel lists before "and N more".
_LIST_PREVIEW = 10


class P2pReceiveDialog(QDialog):
    _friends = None         # the friends service, when the application has one

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._svc = service
        self.setAcceptDrops(True)       # files dropped here are sent (see dropEvent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t("p2p.receive_title"))
        self._apply_window_chrome()
        self._build()
        self._svc.changed.connect(self._refresh)
        self._svc.move_finished.connect(self._on_move_finished)
        self._friends = getattr(self._svc, "friends", None)
        if self._friends is not None:
            self._friends.send_result.connect(self._on_friend_send_result)
            self._friends.lock_changed.connect(self._refresh)
            self._friends.acquire()
        # Counts the token's time down (the service only speaks up when
        # something changes).
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_valid)
        self._tick.start()
        self._svc.panel_opened()
        self._refresh()

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
        self.setMinimumWidth(scaled(520, self))

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
        # On the right of Copy: hand the token straight to a friend.
        self._friend_btn = QPushButton(t("p2p.receive_friends_button"))
        self._friend_btn.setToolTip(t("p2p.receive_friends_tooltip"))
        self._friend_btn.clicked.connect(self._show_friend_menu)
        token_row.addWidget(self._friend_btn)
        outer.addLayout(token_row)

        # How long the token can still be used, and what became of the last
        # one handed to a friend.
        self._valid_lbl = QLabel("")
        self._valid_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._valid_lbl)
        self._friend_note_lbl = QLabel("")
        self._friend_note_lbl.setWordWrap(True)
        self._friend_note_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._friend_note_lbl.setVisible(False)
        outer.addWidget(self._friend_note_lbl)
        # A token made for one friend says so: it is of use to nobody else, and must not
        # be pasted around as if it were.
        self._bound_lbl = QLabel("")
        self._bound_lbl.setWordWrap(True)
        self._bound_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._bound_lbl.setVisible(False)
        outer.addWidget(self._bound_lbl)

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
        self._offer_detail_lbl.setWordWrap(True)
        self._offer_detail_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._offer_detail_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._offer_detail_lbl.setVisible(False)
        outer.addWidget(self._offer_detail_lbl)

        # The page a transfer shows - the same one the sending window has: the overall bar
        # (with its percentage, whether it is one file or many) and the speed, then a row per
        # file with its tick, bar and speed; resting the pointer on a file's bar shows which
        # parts of it are in.
        self._view = TransferFilesView()
        self._view.setVisible(False)
        self._view.ticked.connect(self._on_file_ticked)
        # (the names this window and its tests know the parts by)
        self._progress = self._view.bar
        self._speed_lbl = self._view.speed_lbl
        self._files_tree = self._view.tree
        outer.addWidget(self._view)
        self._tick_hint_lbl = QLabel(t("p2p.receive_files_tick_hint"))
        self._tick_hint_lbl.setWordWrap(True)
        self._tick_hint_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._tick_hint_lbl.setVisible(False)
        outer.addWidget(self._tick_hint_lbl)
        # Something that arrived did not match what was offered (see ReceiveSession.integrity_problem).
        self._integrity_lbl = QLabel()
        self._integrity_lbl.setWordWrap(True)
        self._integrity_lbl.setStyleSheet(f"color:{palette('warning')};")
        self._integrity_lbl.setVisible(False)
        outer.addWidget(self._integrity_lbl)

        # ── Files received by hand, already in their folder ────────────
        # Says where they went. Kept across closing the panel (the service
        # holds the note) until OK; the files themselves are not tracked
        # in any way and never added to the backup index.
        self._recv_frame = QFrame()
        recv = QVBoxLayout(self._recv_frame)
        recv.setContentsMargins(0, 0, 0, 0)
        recv.setSpacing(8)
        self._recv_title_lbl = QLabel()
        self._recv_title_lbl.setStyleSheet(f"font-weight:700;font-size:{scaled(13, self)}px;")
        self._recv_title_lbl.setWordWrap(True)
        recv.addWidget(self._recv_title_lbl)
        self._recv_in_lbl = QLabel(t("p2p.receive_files_saved_in"))
        self._recv_in_lbl.setWordWrap(True)
        recv.addWidget(self._recv_in_lbl)
        path_row = QHBoxLayout()
        self._recv_path_lbl = QLabel()
        self._recv_path_lbl.setWordWrap(True)
        self._recv_path_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._recv_path_lbl.setStyleSheet("font-weight:600;")
        path_row.addWidget(self._recv_path_lbl, 1)
        self._recv_open_btn = QPushButton("📁")
        self._recv_open_btn.setObjectName("toolbar_icon_btn")
        self._recv_open_btn.setFixedSize(scaled(30, self), scaled(30, self))
        self._recv_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recv_open_btn.setToolTip(t("p2p.receive_open_folder"))
        self._recv_open_btn.clicked.connect(self._open_received_folder)
        path_row.addWidget(self._recv_open_btn, 0, Qt.AlignmentFlag.AlignTop)
        recv.addLayout(path_row)
        self._recv_list_lbl = QLabel()
        self._recv_list_lbl.setWordWrap(True)
        self._recv_list_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._recv_list_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        recv.addWidget(self._recv_list_lbl)
        # What was left out on purpose and is not here.
        self._recv_skipped_lbl = QLabel()
        self._recv_skipped_lbl.setWordWrap(True)
        self._recv_skipped_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._recv_skipped_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        self._recv_skipped_lbl.setVisible(False)
        recv.addWidget(self._recv_skipped_lbl)
        recv_btns = QHBoxLayout()
        recv_btns.addStretch()
        self._recv_ok_btn = QPushButton(t("common.ok"))
        self._recv_ok_btn.setObjectName("primary_btn")
        self._recv_ok_btn.clicked.connect(self._svc.dismiss_received)
        recv_btns.addWidget(self._recv_ok_btn)
        self._recv_move_btn = QPushButton(t("p2p.receive_move_to"))
        self._recv_move_btn.clicked.connect(self._move_received)
        recv_btns.addWidget(self._recv_move_btn)
        recv.addLayout(recv_btns)
        self._recv_frame.setVisible(False)
        outer.addWidget(self._recv_frame)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._decline_btn = QPushButton(t("p2p.receive_decline"))
        self._decline_btn.setVisible(False)
        self._decline_btn.clicked.connect(self._svc.decline)
        btn_row.addWidget(self._decline_btn)
        self._accept_btn = QPushButton(t("p2p.receive_accept"))
        self._accept_btn.setObjectName("primary_btn")
        self._accept_btn.setVisible(False)
        self._accept_btn.clicked.connect(self._svc.accept)
        btn_row.addWidget(self._accept_btn)
        # An accepted download can be stopped at any time (what had only partly
        # arrived is removed; what had arrived whole stays).
        self._cancel_btn = QPushButton(t("p2p.receive_cancel"))
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._cancel_download)
        btn_row.addWidget(self._cancel_btn)
        self._open_folder_btn = QPushButton(t("p2p.receive_open_folder"))
        self._open_folder_btn.setVisible(False)
        self._open_folder_btn.clicked.connect(self._open_folder)
        btn_row.addWidget(self._open_folder_btn)
        outer.addLayout(btn_row)

        bottom_row = QHBoxLayout()
        self._new_token_btn = QPushButton(t("p2p.receive_new_token"))
        self._new_token_btn.clicked.connect(self._svc.new_session)
        bottom_row.addWidget(self._new_token_btn)
        # Sending lives here too: anything can be shared, not only a backup
        # (the Backups page sends one specific backup).
        self._send_file_btn = QPushButton(t("p2p.send_file_button"))
        self._send_file_btn.setToolTip(t("p2p.send_file_button_tooltip"))
        self._send_file_btn.clicked.connect(self._send_file)
        bottom_row.addWidget(self._send_file_btn)
        bottom_row.addStretch()
        close_btn = QPushButton(t("p2p.receive_close"))
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        outer.addLayout(bottom_row)

    # ── View of the service's state ──────────────────────────────────────

    def _refresh(self):
        svc = self._svc
        state = svc.state
        # A token is single use: once the offer is accepted and finished (or
        # failed) it is spent, and must not stay up to be copied to the next
        # sender.
        live_token = state in (WAITING, OFFER, DOWNLOADING)
        self._token_edit.setText(svc.token if live_token else "")
        self._copy_btn.setEnabled(live_token and bool(svc.token))
        # Only a token nobody has used yet can be handed on - and not while the
        # friend list is locked: the menu would show who your friends are.
        locked = self._friends is not None and self._friends.locked
        self._friend_btn.setEnabled(state == WAITING and bool(svc.token)
                                    and self._friends is not None and not locked)
        self._friend_btn.setToolTip(t("p2p.receive_friends_locked") if locked
                                    else t("p2p.receive_friends_tooltip"))
        bound = svc.bound_to if live_token else ""
        bound_friend = self._friends.friend(bound) if bound and self._friends is not None else None
        self._bound_lbl.setText(t("p2p.receive_bound_note", name=bound_friend.display) if bound_friend else "")
        self._bound_lbl.setVisible(bound_friend is not None)
        self._refresh_valid()

        offer = svc.offer
        # Once files received by hand are in, the note below says it all: the
        # offer that brought them is no longer worth showing.
        files_done = state == DONE and offer is not None and offer.kind == "file"
        show_offer = (offer is not None and not files_done
                      and state in (OFFER, DOWNLOADING, DONE))
        self._offer_title_lbl.setVisible(show_offer)
        self._offer_detail_lbl.setVisible(show_offer)
        if show_offer:
            name = (offer.sender_username or "").strip()
            sfx = "_file" if offer.kind == "file" else ""
            self._offer_title_lbl.setText(
                t(f"p2p.receive_offer_title{sfx}", name=name) if name
                else t(f"p2p.receive_offer_title_anon{sfx}"))
            self._offer_detail_lbl.setText(self._offer_detail(offer))

        self._accept_btn.setVisible(state == OFFER)
        self._decline_btn.setVisible(state == OFFER)
        self._cancel_btn.setVisible(state == DOWNLOADING and not svc.importing and not svc.cancelling)
        self._new_token_btn.setEnabled(state in (WAITING, ERROR, DONE))
        downloading = state == DOWNLOADING and not svc.importing
        # (a transfer that finished with files left out keeps its page: ticking one goes on)
        show_page = downloading or svc.resumable
        self._view.setVisible(show_page)
        self._speed_lbl.setVisible(show_page)
        rows = svc.file_rows if show_page else []
        self._tick_hint_lbl.setVisible(len(rows) > 1)
        self._update_file_tree(rows)
        problem = svc.integrity if downloading else None
        self._integrity_lbl.setVisible(bool(problem))
        if problem:
            names = t("p2p.integrity_names", names=", ".join(problem.get("files") or [])) \
                if problem.get("files") else ""
            self._integrity_lbl.setText("⚠ " + t("p2p.integrity_warning", count=problem["count"], names=names))
        self._open_folder_btn.setVisible(
            state == DONE and not svc.imported_name and svc.saved_path is not None)

        if state == WAITING:
            self._status_lbl.setText(
                t("p2p.receive_token_renewed_proxy") if svc.token_renewed_proxy
                else t("p2p.receive_token_renewed") if svc.token_renewed
                else t("p2p.receive_waiting"))
        elif state == OFFER:
            self._status_lbl.setText(t("p2p.receive_waiting_decision"))
        elif state == DOWNLOADING and svc.importing:
            self._status_lbl.setText(t("p2p.receive_importing"))
        elif state == DOWNLOADING and svc.cancelling:
            self._status_lbl.setText(t("p2p.receive_cancelling"))
        elif state == DOWNLOADING:
            self._progress.setValue(svc.pct)
            # Nothing has arrived yet (the sender is still being reached, or the
            # file list is on its way): not "0%", which reads as stuck.
            waiting = svc.pct == 0 and svc.rate <= 0
            self._status_lbl.setText(t("p2p.receive_connecting") if waiting
                                     else t("p2p.receive_downloading", percent=svc.pct))
            if waiting and svc.idle_seconds >= 45:
                self._status_lbl.setText(self._status_lbl.text() + "\n" + self._connection_hint())
            if svc.idle_seconds >= 60:
                self._status_lbl.setText(self._status_lbl.text() + "\n" + t(
                    "p2p.receive_idle", minutes=max(1, int(svc.idle_seconds // 60))))
                if getattr(svc.offer, "tls", False):
                    self._status_lbl.setText(self._status_lbl.text() + " " + t("p2p.receive_idle_tls"))
            from core.p2p.transfer import format_rate
            mbs, mbps = format_rate(svc.rate)
            self._speed_lbl.setText(t("p2p.speed_down", speed=mbs))
            # (megabits on every figure the pointer can rest on)
            for w in (self._speed_lbl, self._progress, self._status_lbl):
                w.setToolTip(mbps)
        elif state == DONE and files_done:
            self._view.set_global(100, "", "")
            self._status_lbl.setText(t("p2p.receive_complete") + (
                "\n" + t("p2p.receive_skipped_resumable") if svc.resumable else "")
                + ("\n" + svc.resume_note if svc.resume_note else ""))
        elif state == DONE:
            if svc.imported_name:
                self._status_lbl.setText(
                    t("p2p.receive_imported_detail", name=svc.imported_name))
            else:
                # No entry to import against (an older SaveSync on the
                # sender's side), or the import itself failed — the download
                # is still there, just not filed anywhere.
                self._status_lbl.setText(
                    t("p2p.receive_done_detail", path=str(svc.saved_path)))
        elif state == ERROR:
            self._status_lbl.setText(t("p2p.receive_error", error=svc.error))
        self._refresh_received()

    def _connection_hint(self) -> str:
        """Why nothing has arrived, as far as the session can tell (the other PC not found at
        all, or found and still being connected to): see core.p2p.transfer.describe_direct_failure."""
        from core.p2p.transfer import tor_enabled
        if tor_enabled():
            return t("p2p.no_connection_tor")
        try:
            key, params = self._svc.session.connection_hint()
            return t(key, **params)
        except Exception:
            return t("p2p.no_connection_direct")

    def _update_file_tree(self, rows):
        """Hand the page its rows: each file with its tick, bar and speed. A file that is
        left out shows as such and may be ticked again (asking for it, if the transfer is
        over); one that is whole cannot be unticked, nor the last one still wanted (leaving
        everything out is cancelling, which has its own button)."""
        from core import fmt_size
        from ui.widgets.transfer_files_view import ACTIVE, LEFT_OUT, WHOLE, FileRow
        svc = self._svc
        left_out = set(svc.skipped_files)
        maps = svc.file_maps or {}
        wanted_left = [r[0] for r in rows if r[0] not in left_out]
        out = []
        for rel, size, done, speed in rows:
            if rel in left_out:
                state, tickable = LEFT_OUT, True
            elif size <= 0 or done >= size:
                state, tickable = WHOLE, False
            else:
                state, tickable = ACTIVE, wanted_left != [rel]
            out.append(FileRow(rel, size, done, speed, state, tickable, maps.get(rel)))
        self._view.set_rows(out)
        counted = [r for r in rows if r[0] not in left_out]
        self._view.sizes_lbl.setText(
            t("p2p.send_sizes", sent=fmt_size(sum(r[2] for r in counted)), total=fmt_size(sum(r[1] for r in counted)))
            if counted and svc.state == DOWNLOADING else "")

    def _on_file_ticked(self, name, wanted):
        """The person ticked or unticked a file of the list."""
        if not self._svc.set_file_wanted(name, wanted):
            # (not possible any more - it finished, or it was the last one wanted):
            # the box goes back to what is true
            self._view.set_checked(name, not wanted)

    def _refresh_received(self):
        from core import fmt_size
        rec = self._svc.received
        offer = self._svc.offer
        if offer is not None and offer.kind != "file":
            rec = None          # a save is coming: it goes to Backups, no path to show
        self._recv_frame.setVisible(bool(rec))
        if not rec:
            return
        files = rec["files"]
        sender = rec.get("sender") or ""
        self._recv_title_lbl.setText(
            t("p2p.receive_files_title", count=len(files), size=fmt_size(rec["size"]))
            + (f" - {sender}" if sender else ""))
        self._recv_path_lbl.setText(str(rec["dir"]))
        self._recv_path_lbl.setToolTip(str(rec["dir"]))
        shown = files[:_LIST_PREVIEW]
        text = "\n".join(shown)
        if len(files) > len(shown):
            text += "\n" + t("p2p.receive_files_more", count=len(files) - len(shown))
        self._recv_list_lbl.setText(text)
        left_out = list(rec.get("skipped") or [])
        self._recv_skipped_lbl.setVisible(bool(left_out))
        if left_out:
            shown_out = left_out[:_LIST_PREVIEW]
            names = ", ".join(shown_out)
            if len(left_out) > len(shown_out):
                names += ", " + t("p2p.receive_files_more", count=len(left_out) - len(shown_out))
            self._recv_skipped_lbl.setText(t("p2p.receive_skipped_note", count=len(left_out), names=names))
        busy = self._svc.moving
        self._recv_ok_btn.setEnabled(not busy)
        self._recv_move_btn.setEnabled(not busy)
        self._recv_move_btn.setText(t("p2p.receive_moving") if busy else t("p2p.receive_move_to"))

    @staticmethod
    def _offer_detail(offer) -> str:
        from core import fmt_size
        lines = []
        if offer.kind == "file":
            if offer.folder:
                lines.append(t("p2p.receive_offer_folder", folder=offer.folder))
            lines.append(t("p2p.receive_offer_files", name=offer.filename,
                           count=offer.count or 1, size=fmt_size(offer.size)))
            if getattr(offer, "tls", False):
                lines.append(t("p2p.receive_offer_tls"))
            return "\n".join(lines)
        if offer.game_name:
            lines.append(t("p2p.receive_offer_game", game=offer.game_name))
        if offer.folder:
            lines.append(t("p2p.receive_offer_folder", folder=offer.folder))
        lines.append(t("p2p.receive_offer_detail", filename=offer.filename,
                       size=fmt_size(offer.size)))
        if getattr(offer, "tls", False):
            lines.append(t("p2p.receive_offer_tls"))
        return "\n".join(lines)

    # ── The token's clock, and handing it to a friend ───────────────────

    def _refresh_valid(self):
        svc = self._svc
        if svc.state == WAITING and svc.token:
            # A sender that has come and is still getting a big offer ready has
            # used the token: its clock stops, and the wait is for them.
            ps = getattr(svc, "private_state", None) or {}
            if ps.get("state") == "failed":
                text = t("p2p.receive_private_failed", error=ps.get("error", ""))
            elif ps and ps.get("state") != "ready":
                text = t("p2p.receive_private_starting", percent=int(ps.get("progress", 0)))
            elif ps and not ps.get("published") and not svc.sender_preparing:
                text = t("p2p.receive_private_publishing")
            elif svc.sender_preparing:
                text = t("p2p.receive_sender_preparing")
            else:
                text = t("p2p.receive_token_valid", time=format_remaining(svc.token_remaining))
            self._valid_lbl.setText(text)
        else:
            self._valid_lbl.setText("")

    def _cancel_download(self):
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("p2p.receive_title"), t("p2p.receive_cancel_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._svc.cancel_download()

    def _show_friend_menu(self):
        friends = self._friends
        if friends is None:
            return
        menu = QMenu(self)
        people = friends.friends()
        for f in people:
            mark = "✓ " if f.connected else "⏳ "
            act = menu.addAction(dot_icon(friends.is_online(f.id)), mark + t("p2p.receive_send_to", name=f.display))
            act.setToolTip(friends.presence_tip(f.id))
            act.triggered.connect(lambda _=False, fid=f.id: self._send_to_friend(fid))
        if not people:
            hint = menu.addAction(t("p2p.receive_friends_none"))
            hint.setEnabled(False)
        menu.addSeparator()
        add = menu.addAction(t("p2p.receive_friends_manage"))
        add.triggered.connect(self._open_friends)
        menu.exec(self._friend_btn.mapToGlobal(self._friend_btn.rect().bottomLeft()))

    def _open_friends(self):
        # The friend list is a panel on the main window, not a window of its own: it
        # could not be used from behind this dialog, so this one is put away first
        # (closing it does not end the session, see done()) and the list opened as
        # soon as it is gone.
        win = self.parent()
        opener = getattr(win, "_open_p2p_friends_panel", None)
        if callable(opener):
            self.accept()
            QTimer.singleShot(0, lambda: opener(toggle=False))

    def _send_to_friend(self, fid: str):
        svc = self._svc
        f = self._friends.friend(fid) if self._friends is not None else None
        if f is None or svc.state != WAITING or not svc.token:
            return
        if svc.bound_to and svc.bound_to != fid:
            self._friend_note_lbl.setText(t("p2p.receive_bound_other", name=f.display))
            self._friend_note_lbl.setVisible(True)
            return
        try:
            svc.send_token_to_friend(fid)
        except Exception as e:
            self._friend_note_lbl.setText(t("p2p.receive_send_to_failed", name=f.display, error=str(e)))
            self._friend_note_lbl.setVisible(True)
            return
        self._friend_note_lbl.setText(t("p2p.receive_sending_to", name=f.display))
        self._friend_note_lbl.setVisible(True)

    def _on_friend_send_result(self, fid: str, ok: bool):
        f = self._friends.friend(fid) if self._friends is not None else None
        name = f.display if f is not None else "?"
        if ok:
            self._friend_note_lbl.setText(t("p2p.receive_sent_to", name=name))
        else:
            self._friend_note_lbl.setText(t("p2p.receive_send_to_failed", name=name,
                                            error=t("p2p.send_rejected")))
        self._friend_note_lbl.setVisible(True)

    def _copy_token(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._token_edit.text())
        self._copy_btn.setText(t("p2p.receive_copied"))
        QTimer.singleShot(1500, lambda: self._copy_btn.setText(t("p2p.receive_copy")))

    def _send_file(self, paths=None):
        """Send a file or folder: pick it with the app's own picker, or take the
        *paths* dropped on this panel.

        The panel closes first. What happens next is a picker, a list to tick
        and a send window of its own, and leaving this one open behind them is a
        window in the way of every one (closing it does not end the session:
        see done()). The steps run once it is gone, from the main window."""
        window = self.parent()
        self.accept()
        from ui.dialogs.p2p_send_flow import send_files_flow
        QTimer.singleShot(0, lambda: send_files_flow(window, paths=paths))

    # ── Files dropped on the panel are sent, as if picked with "Send a file…" ──

    @staticmethod
    def _dropped(event) -> list:
        from ui.dialogs.p2p_friends_dialog import dropped_paths
        return dropped_paths(event.mimeData())

    def dragEnterEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._dropped(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # (asked once the drop itself is over: a window opened inside it would
        # hold the program the files came from)
        QTimer.singleShot(0, lambda: self._send_file(paths))

    def _move_received(self):
        """"Move to…": the folder picker, then the move itself."""
        rec = self._svc.received
        if not rec or self._svc.moving:
            return
        from ui.widgets.file_pickers import pick_folder
        dest = pick_folder(self, t("p2p.receive_move_pick"),
                           start_dir=str(rec["dir"]), save_target=True)
        if dest:
            self._svc.move_files(dest)

    def _open_received_folder(self):
        rec = self._svc.received
        if rec:
            open_in_file_manager(rec["dir"])

    def _on_move_finished(self, moved: int, failed, dest: str, new_rels=None):
        # A move that worked needs no message: the path shown above has just
        # changed to where the files are now. Only a failure is worth a popup.
        if failed:
            from ui.modal_helpers import warning_window_modal
            lines = "\n".join(f"{rel}: {err}" for rel, err in failed[:5])
            warning_window_modal(
                self, t("p2p.receive_title"),
                t("p2p.receive_move_failed", count=len(failed), errors=lines))

    def _open_folder(self):
        if self._svc.saved_path is not None:
            open_in_file_manager(self._svc.saved_path)

    def done(self, result):
        # Closing the panel does NOT end the session — the service keeps the
        # token alive for a while and reopens the panel when an offer comes.
        self._tick.stop()
        pairs = [(self._svc.changed, self._refresh),
                 (self._svc.move_finished, self._on_move_finished)]
        if self._friends is not None:
            pairs.append((self._friends.send_result, self._on_friend_send_result))
            pairs.append((self._friends.lock_changed, self._refresh))
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        if self._friends is not None:
            self._friends.release()
        self._svc.panel_closed()
        super().done(result)
