"""SaveSync - "Send this save" window: paste the receiver's token, publish
the offer, keep seeding until it's picked up. See core.p2p.transfer for the
DHT-mailbox + BitTorrent mechanics this drives.

Two pages. The first asks for the token. Once the send starts the second takes
its place: how much of the offer the receiver has, as one bar and, for several
files, a row for each. ✕ does not stop the transfer: it puts the window away in
the sidebar (see the shelf methods at the end) and the transfer goes on; the
Cancel button is what stops it.
"""
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHeaderView, QVBoxLayout, QHBoxLayout, QLabel, QMenu,
    QPushButton, QLineEdit, QMessageBox, QTreeWidget, QTreeWidgetItem, QWidget,
)

from i18n import t
from ui.helpers import scaled
from ui.modal_helpers import question_window_modal
from ui.styles.theme import palette
from ui.widgets.presence_dot import dot_icon
from ui.widgets.transfer_bar import transfer_bar
from ui.widgets.transfer_files_view import ACTIVE, LEFT_OUT, WHOLE, FileRow, TransferFilesView

logger = logging.getLogger(__name__)

_POLL_MS = 1000
# How long an offer can sit with nobody connected before the window says why
# that may be (a direct connection that cannot be made, a slow first circuit).
_NO_PEER_HINT_SECONDS = 45.0


class P2pSendDialog(QDialog):
    """*entry* is the core.backup.BackupEntry for the one backup being
    sent — its full to_dict() travels with the transfer (inside the
    torrent, not the DHT mailbox — see core.p2p.transfer's module
    docstring) so the receiver can file the download under one of THEIR
    OWN games and restore it normally instead of only getting a bare file.
    """

    _send_result = Signal(object, object)   # (SendHandle | None, error str | None)
    # ✕ put the window away while the transfer goes on / how it is doing, for the
    # sidebar entry the main window keeps for it (see MainWindow._open_p2p_send_dialog).
    shelved = Signal()
    shelve_status = Signal()

    def __init__(self, entry, parent=None, *, files=None, label="",
                 folder_name="", token="", auto_start=False):
        """Send *entry*'s backup — or, with ``entry=None``, the *files*
        picked by hand and confirmed in P2pSendConfirmDialog:
        ``[(path on disk, relative path)]``, sent as they are. *label* names
        the offer, *folder_name* is the folder they came from. *token* fills
        in the receiver's token (one a friend sent); with *auto_start* the
        send begins as soon as the window is up.

        👥 asks a friend for their token instead of waiting for it to be pasted: they
        get a request in their chat and, when they accept, their token is filled in
        here and the send starts - the same ten minutes any token has."""
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
        # What is on offer, file by file: (the name it travels under, its size).
        self._offered: list = []
        try:
            if self._file_path is not None:
                self._file_size = self._file_path.stat().st_size
                self._offered = [(self._file_path.name, self._file_size)]
            else:
                for p, rel in self._files:
                    self._offered.append((str(rel).replace("\\", "/"), Path(p).stat().st_size))
                self._file_size = sum(size for _rel, size in self._offered)
        except OSError:
            pass
        self._pct = 0                   # how far the receiver has got, for the sidebar
        self._sent = 0                  # (bytes it holds)
        self._rate = 0.0
        self._force_close = False       # really closing: not putting away in the sidebar
        self._marked_done = False
        self._page_shown_at = 0.0       # when the transfer page came up (monotonic)
        self._peer_seen = False         # has anyone connected to take it yet
        # Before the offer exists every byte is read once (hashed): a big file
        # takes minutes. (pieces done, pieces in all) is written by the worker
        # thread; _prep_cancel tells it to stop.
        self._preparing = False
        self._prep = (0, 0)
        self._prep_cancel = False
        # Files the other PC finished without, as it told us (names, and how many more).
        self._left_out: list = []
        self._left_out_more = 0
        self._partial_shown = False     # the other PC finished without some files: waiting, in case it wants them
        self._row_prev: dict = {}       # per file: (bytes held, when, smoothed speed)
        self._source_snap: dict = {}    # the files as they were when the send began: (size, mtime)
        self._source_checked = 0.0
        self._source_changed = ""
        # Set once the dialog is closed: a publish still being prepared on the
        # worker thread must not start seeding into a window nobody can see.
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._pump)
        self._send_result.connect(self._on_send_result)
        self._friend_token_ids: list = []
        self._ask = None                # (friend id, request id) of the token I asked for
        self._ask_svc = None            # the service whose answers this is listening to
        self._holding = False           # holds the friends' network open while a request waits
        self._answered = ("", "")       # (token, friend id) of the answer to my request
        self._build()
        # Tokens friends have handed over can be picked instead of pasted; the
        # list keeps itself current (each one runs out after ten minutes).
        self._friend_timer = QTimer(self)
        self._friend_timer.setInterval(1000)
        self._friend_timer.timeout.connect(self._tick_friends)
        self._friend_timer.start()
        self._tick_friends()
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

        # ── Page 1: the token ────────────────────────────────────────────
        self._token_box = QWidget()
        tb = QVBoxLayout(self._token_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(12)
        self._intro_lbl = QLabel(t("p2p.send_intro"))
        self._intro_lbl.setWordWrap(True)
        self._intro_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        tb.addWidget(self._intro_lbl)

        self._friend_combo = QComboBox()
        self._friend_combo.setVisible(False)
        self._friend_combo.activated.connect(self._pick_friend_token)
        tb.addWidget(self._friend_combo)

        token_row = QHBoxLayout()
        self._token_edit = QLineEdit()
        self._token_edit.setPlaceholderText(t("p2p.send_token_placeholder"))
        self._token_edit.setStyleSheet(
            f"font-family: monospace; font-size: {scaled(13, self)}px; letter-spacing: 1px;")
        self._token_edit.returnPressed.connect(self._on_send_clicked)
        token_row.addWidget(self._token_edit, 1)
        self._ask_btn = QPushButton("👥")
        self._ask_btn.setToolTip(t("p2p.send_ask_tooltip"))
        self._ask_btn.setFixedWidth(scaled(44, self))
        self._ask_btn.setVisible(False)         # (shown once there are friends to ask: _tick_friends)
        self._ask_btn.clicked.connect(self._show_ask_menu)
        token_row.addWidget(self._ask_btn)
        self._send_btn = QPushButton(t("p2p.send_button"))
        self._send_btn.setObjectName("primary_btn")
        self._send_btn.clicked.connect(self._on_send_clicked)
        token_row.addWidget(self._send_btn)
        tb.addLayout(token_row)
        outer.addWidget(self._token_box)

        # What the window is saying now, on either page.
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._status_lbl)

        # ── Page 2: the transfer ─────────────────────────────────────────
        # How much of the offer the RECEIVER has (not how much has been pushed
        # out: a seeder's own progress is always whole), as one bar with its
        # percentage on it, in bytes, and file by file for more than one.
        self._transfer_box = QWidget()
        xb = QVBoxLayout(self._transfer_box)
        xb.setContentsMargins(0, 0, 0, 0)
        xb.setSpacing(8)
        # The page a transfer shows - the same one the receiving window has: the overall bar
        # (the one the sidebar shows), the size and speed under it, then a row per file with its
        # tick, bar and speed. Resting the pointer on a file's bar shows which parts are in.
        self._view = TransferFilesView()
        xb.addWidget(self._view)
        # (the names this window and its tests know the parts by)
        self._progress = self._view.bar
        self._sizes_lbl = self._view.sizes_lbl
        self._speed_lbl = self._view.speed_lbl
        self._files_tree = self._view.tree
        # A file being sent changed on this PC (see _watch_sources).
        self._warn_lbl = QLabel("")
        self._warn_lbl.setWordWrap(True)
        self._warn_lbl.setStyleSheet(f"color:{palette('warning')};")
        self._warn_lbl.setVisible(False)
        xb.addWidget(self._warn_lbl)

        self._hint_lbl = QLabel(t("p2p.send_background_hint"))
        self._hint_lbl.setWordWrap(True)
        self._hint_lbl.setStyleSheet(
            f"color:{palette('text_secondary')};font-size:{scaled(11, self)}px;")
        xb.addWidget(self._hint_lbl)
        self._transfer_box.setVisible(False)
        outer.addWidget(self._transfer_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        # Away in the sidebar (the transfer goes on) / stop it / done.
        self._hide_btn = QPushButton(t("p2p.send_hide"))
        self._hide_btn.setToolTip(t("p2p.send_hide_tip"))
        self._hide_btn.clicked.connect(self.reject)
        self._hide_btn.setVisible(False)
        btn_row.addWidget(self._hide_btn)
        self._cancel_btn = QPushButton(t("p2p.send_cancel_transfer"))
        self._cancel_btn.clicked.connect(self._ask_cancel)
        self._cancel_btn.setVisible(False)
        btn_row.addWidget(self._cancel_btn)
        self._close_btn = QPushButton(t("common.close"))
        self._close_btn.clicked.connect(self._close_pressed)
        btn_row.addWidget(self._close_btn)
        outer.addLayout(btn_row)

    def _on_send_clicked(self):
        token = self._token_edit.text().strip()
        if not token or not self._send_btn.isEnabled():
            return
        from core.p2p.transfer import (
            P2pError, P2pLinkError, P2pTokenExpired, seed_from_token, token_is_bound)
        try:
            seed_from_token(token)
        except P2pTokenExpired:
            self._status_lbl.setText(t("p2p.send_token_expired"))
            return
        except P2pError:
            self._status_lbl.setText(t("p2p.send_invalid_token"))
            return
        # A token made for one friend only works with that friend's key. Read here, on the
        # GUI thread (the friend list is not for the worker below to touch): from the answer
        # to my request, else from whose token this is in my inbox.
        friend_key = None
        svc = self._friends()
        if svc is not None:
            fid = self._answered[1] if token == self._answered[0] else ""
            friend_key = svc.bind_key(fid) if fid else svc.bind_key_for_token(token)
        if token_is_bound(token) and friend_key is None:
            self._status_lbl.setText(t("p2p.send_bound_token"))
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
        # The transfer page comes up now, not when the offer is ready: for a big
        # file the preparation is the longest part, and it has its own progress.
        self._preparing, self._prep, self._prep_cancel = True, (0, 0), False
        self._snapshot_sources()
        self._show_transfer_page()
        self._show_preparing()
        self._timer.start()
        self.shelve_status.emit()

        def _prepared(done, total):
            self._prep = (done, total)
            return not self._prep_cancel

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
                                         folder_name=folder_name, friend_key=friend_key,
                                         progress=_prepared)
                else:
                    # Hand-picked files: sent as they are - no zip, no game, no
                    # backup entry. They land in the receiver's chosen folder.
                    from core.p2p.transfer import send_files
                    handle = send_files(token, files, username, machine_id,
                                        label=label, folder_name=folder_name, friend_key=friend_key,
                                        progress=_prepared)
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
        self._preparing = False
        if self._closed:
            # Closed while the offer was being prepared: the transfer that
            # just came back is one nobody is watching.
            if handle is not None:
                handle.stop()
            return
        if error:
            self._timer.stop()
            self._show_token_page()
            self._status_lbl.setText(t("p2p.send_failed", error=error))
            self._send_btn.setEnabled(True)
            self._token_edit.setEnabled(True)
            if not self.isVisible():
                # (put away in the sidebar while it was being prepared: the
                # failure is for the person to see)
                self.unshelve()
            self.shelve_status.emit()
            return
        self._handle = handle
        self._used_token = self._token_edit.text().strip()
        self._forget_friend_token(self._used_token)
        self._status_lbl.setText(
            t("p2p.send_seeding", name=self._display_name))
        self._show_transfer_page()
        self._hint_lbl.setText(t("p2p.send_background_hint"))
        self._timer.start()
        self.shelve_status.emit()

    def _show_preparing(self):
        """The files are being read to make the offer: how far that has got."""
        done, total = self._prep
        pct = min(99, int(done * 100 / total)) if total else 0
        self._pct = pct
        self._progress.setValue(pct)
        self._status_lbl.setText(t("p2p.send_preparing", name=self._display_name, percent=pct))
        self._hint_lbl.setText(t("p2p.send_preparing_hint"))
        self._hint_lbl.setVisible(True)

    # ── The two pages ────────────────────────────────────────────────────

    def _show_transfer_page(self):
        """The send has started: the token is spent and its box goes, what the
        receiver has takes its place."""
        self._token_box.setVisible(False)
        self._transfer_box.setVisible(True)
        self._page_shown_at = time.monotonic()
        self._peer_seen = False
        self._partial_shown = False
        self._row_prev = {}
        self._view.set_global(0)
        self._hide_btn.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._close_btn.setVisible(False)
        self._set_rows([FileRow(rel, size) for rel, size in self._offered])

    def _show_token_page(self):
        """Back to asking for a token (the offer could not be delivered)."""
        self._transfer_box.setVisible(False)
        self._token_box.setVisible(True)
        self._hide_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._close_btn.setVisible(True)
        self._pct, self._sent, self._rate = 0, 0, 0.0

    def _set_rows(self, rows):
        """One row per file when there is more than one to tell apart. The ticks are the
        receiver's to change, not the sender's: the files in the package are fixed while it
        runs (the receiver checks every part against what it was offered)."""
        self._view.set_rows(rows, tickable=False, name_tip=t("p2p.tick_tip_send"))

    def _file_speed(self, rel: str, have: int, now: float) -> float:
        """How fast the other PC is getting a file: the change in what it holds since the
        last look, smoothed so the number does not flicker."""
        prev = self._row_prev.get(rel)
        if prev is None:
            self._row_prev[rel] = (have, now, 0.0)
            return 0.0
        held, then, speed = prev
        if now - then > 0.2:
            inst = max(0, have - held) / (now - then)
            speed = 0.6 * inst + 0.4 * speed if speed else inst
            if inst == 0.0:
                speed *= 0.5
            if speed < 1.0:
                speed = 0.0
            self._row_prev[rel] = (have, now, speed)
        return speed

    def _update_transfer(self, p: dict) -> bool:
        """Redraw what the receiver has. True once it has everything it is going to take."""
        from core import fmt_size
        total = self._file_size
        try:
            d = self._handle.delivery()
        except Exception:
            logger.debug("P2P send: reading the receiver's progress failed", exc_info=True)
            d = None
        rows = None
        # Files the other PC finished without (it says so when it does): they are not
        # part of what it was going to take, so the figures leave them out.
        left_out = set(d.get("skipped") or []) if d is not None else set()
        self._left_out = sorted(left_out)
        self._left_out_more = int(d.get("more") or 0) if d is not None else 0
        if left_out:
            total = sum(size for rel, size in self._offered if rel not in left_out)
        if d is not None:
            held = {rel: have for rel, _size, have in d["files"]}
            maps = d.get("maps") or {}
            now = time.monotonic()
            rows = []
            for rel, size in self._offered:
                if rel in left_out:
                    have, state = 0, LEFT_OUT
                else:
                    have = size if d["done"] else min(size, held.get(rel, 0))
                    state = WHOLE if size <= 0 or have >= size else ACTIVE
                rows.append(FileRow(rel, size, have, self._file_speed(rel, have, now) if state == ACTIVE else 0.0,
                                    state, False, maps.get(rel)))
            sent = sum(r.done for r in rows)
            done = bool(d["done"])
        else:
            # (nobody has connected yet, or it was not possible to ask): all that
            # can be said is how much has gone out
            uploaded = p.get("all_time_upload", 0)
            sent = min(total, int(uploaded))
            done = bool(total and uploaded >= total)
        self._sent = sent
        self._pct = 100 if done else (min(99, int(sent * 100 / total)) if total else 0)
        self._progress.setValue(self._pct)
        self._sizes_lbl.setText(t("p2p.send_sizes", sent=fmt_size(sent), total=fmt_size(total)))
        if rows is not None:
            self._set_rows(rows)
        return done

    def _connection_hint(self) -> str:
        """Why nobody has connected, as far as the session can tell (not found at all, or found and
        still being connected to): see core.p2p.transfer.describe_direct_failure."""
        from core.p2p.transfer import tor_enabled
        if tor_enabled():
            return t("p2p.no_connection_tor")
        try:
            key, params = self._handle.connection_hint()
        except Exception:
            key, params = "p2p.no_connection_direct", {}
        return t(key, **params)

    def _done_text(self) -> str:
        """The line for a send that is over: complete, or - when the other PC said it
        finished without some files - which ones it did not take."""
        if not self._left_out:
            return t("p2p.send_done")
        shown = self._left_out[:5]
        names = ", ".join(shown)
        rest = len(self._left_out) - len(shown) + self._left_out_more
        if rest > 0:
            names += ", " + t("p2p.receive_files_more", count=rest)
        return t("p2p.send_done_partial", count=len(self._left_out) + self._left_out_more, names=names)

    def _show_partial(self):
        """The other PC has what it wanted and left some files out. Nothing is wrong and
        nothing is closed: this window keeps offering, in case it wants them after all (it
        goes back to sending by itself when it does). Close ends it, and tells it so."""
        self._partial_shown = True
        self._status_lbl.setText(self._done_text())
        self._speed_lbl.setText("")
        self._hint_lbl.setText(t("p2p.send_partial_hint"))
        self._hint_lbl.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._close_btn.setVisible(True)
        self._hide_btn.setVisible(True)

    def _show_sending_again(self):
        """It asked for some of the files it had left out: back to a transfer under way."""
        self._partial_shown = False
        self._hint_lbl.setText(t("p2p.send_background_hint"))
        self._cancel_btn.setVisible(True)
        self._close_btn.setVisible(False)
        self._hide_btn.setVisible(True)

    def _snapshot_sources(self):
        """What the files are like now, to notice if one is changed while it is being sent."""
        self._source_snap = {}
        self._source_changed = ""
        if self._entry is not None:
            return                      # (a backup: an archive of ours, nobody edits it)
        for path, _rel in list(self._files or [])[:500]:
            try:
                st = Path(path).stat()
                self._source_snap[str(path)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass

    def _watch_sources(self):
        """Every few seconds: is a file that is being sent different from when the send
        began? The other PC checks every part against what was offered and refuses the
        changed ones, so it would simply stop short - say why."""
        now = time.monotonic()
        if self._source_changed or not self._source_snap or now - self._source_checked < 5.0:
            return
        self._source_checked = now
        for path, before in self._source_snap.items():
            try:
                st = Path(path).stat()
                changed = (st.st_size, st.st_mtime_ns) != before
            except OSError:
                changed = True
            if changed:
                self._source_changed = Path(path).name
                self._warn_lbl.setText("⚠ " + t("p2p.source_changed", name=self._source_changed))
                self._warn_lbl.setVisible(True)
                return

    def _close_pressed(self):
        """The Close button. While the other PC could still ask for files it left out, this
        is what ends the send (✕ and Hide only put the window away)."""
        if self._partial_shown:
            self.cancel_background_work()
        else:
            self.reject()

    def _pump(self):
        if self._handle is None:
            if self._preparing:
                self._show_preparing()
                self.shelve_status.emit()
            return
        self._handle.pump()
        if self._handle.put_confirmed is False:
            # Nobody can pick this offer up: end it, and let the person try
            # again (same token, or a new one) rather than leave them stuck.
            self._timer.stop()
            self._handle.stop()
            self._handle = None
            self._show_token_page()
            self._status_lbl.setText(
                t("p2p.send_failed", error=t("p2p.send_rejected")))
            self._send_btn.setEnabled(True)
            self._token_edit.setEnabled(True)
            self.shelve_status.emit()
            return
        p = self._handle.progress()
        peers = p.get("num_peers", 0)
        done = self._update_transfer(p)
        if not self._marked_done:
            self._watch_sources()
        partial = bool(done and self._left_out)
        if done and not partial and not self._marked_done:
            self._marked_done = True
            self._partial_shown = False
            self._status_lbl.setText(self._done_text())
            self._speed_lbl.setText("")
            self._hint_lbl.setVisible(False)
            self._hide_btn.setVisible(False)
            self._cancel_btn.setVisible(False)
            self._close_btn.setVisible(True)
        elif partial and not self._marked_done:
            self._show_partial()
        elif self._partial_shown and not done:
            self._show_sending_again()
        elif peers <= 0 and not self._peer_seen and not self._marked_done:
            # Nobody has come for it: after a while say why that may be and what
            # to try, rather than leave a bar at 0 with nothing to go on.
            text = t("p2p.send_seeding", name=self._display_name)
            if time.monotonic() - self._page_shown_at >= _NO_PEER_HINT_SECONDS:
                text += "\n" + self._connection_hint()
            if self._status_lbl.text() != text:
                self._status_lbl.setText(text)
        elif peers > 0 and not self._marked_done:
            self._peer_seen = True
            from core.p2p.transfer import format_rate
            self._status_lbl.setText(t("p2p.send_peers", peers=peers))
            self._rate = float(p.get("upload_rate", 0))
            mbs, mbps = format_rate(self._rate)
            self._speed_lbl.setText(t("p2p.speed_up", speed=mbs))
            # Megabits on every figure the pointer can rest on, not only the speed.
            for w in (self._speed_lbl, self._sizes_lbl, self._progress):
                w.setToolTip(mbps)
        self.shelve_status.emit()

    # ── Asking a friend for a token ──────────────────────────────────────

    def _idle(self) -> bool:
        """Nothing is being sent yet, and the token box is free."""
        return self._handle is None and self._send_btn.isEnabled() and not self._closed

    def _tick_friends(self):
        self._refresh_friend_tokens()
        svc = self._friends()
        self._ask_btn.setVisible(svc is not None and not svc.locked)
        self._ask_btn.setEnabled(self._idle())
        self._refresh_ask(svc)

    def _show_ask_menu(self):
        svc = self._friends()
        if svc is None or svc.locked or not self._idle():
            return
        menu = QMenu(self)
        people = svc.friends()
        for f in people:
            mark = "✓ " if f.connected else "⏳ "
            act = menu.addAction(dot_icon(svc.is_online(f.id)), mark + t("p2p.send_ask_item", name=f.display))
            act.setToolTip(svc.presence_tip(f.id))
            act.triggered.connect(lambda _=False, fid=f.id: self._ask_friend(fid))
        if not people:
            hint = menu.addAction(t("p2p.receive_friends_none"))
            hint.setEnabled(False)
        menu.exec(self._ask_btn.mapToGlobal(self._ask_btn.rect().bottomLeft()))

    def _ask_friend(self, fid: str):
        from core.p2p.friends import FriendError
        svc = self._friends()
        f = svc.friend(fid) if svc is not None else None
        if f is None or not self._idle():
            return
        wait = svc.ask_wait(fid)
        if wait > 0:
            # (before the request that is out is taken back: it stays as it was)
            self._status_lbl.setText(t("p2p.send_ask_wait", name=f.display, seconds=int(wait) + 1))
            return
        self._withdraw_ask()            # one request at a time: the last is taken back
        try:
            rid = svc.request_token(fid, self._display_name)
        except FriendError as e:
            self._status_lbl.setText(
                t("p2p.send_ask_full", name=f.display) if "queue full" in str(e)
                else t("p2p.send_ask_failed", name=f.display, error=str(e)))
            return
        self._ask = (fid, rid)
        svc.request_answered.connect(self._on_ask_answered)
        self._ask_svc = svc
        # The answer is read from the friend's mailbox: it has to be looked at - at the
        # fast rate, and whatever Settings say about listening in the background - and the
        # list kept from locking itself under the wait, as in the Receive dialog (this one
        # is modal too, and has the person's attention).
        if not self._holding:
            svc.acquire()
            self._holding = True
        self._refresh_ask(svc)

    def _friend_name(self, svc, fid: str) -> str:
        f = svc.friend(fid) if svc is not None else None
        return f.display if f is not None else "?"

    def _on_ask_answered(self, fid: str, rid: str, token: str):
        """The friend accepted: their token goes in the box and the send starts, as with
        a token picked from "sent by a friend" - the same checks, the same ten minutes."""
        if self._ask != (fid, rid) or not self._idle():
            return
        self._forget_ask()
        self._answered = (token, fid)       # (so the send knows whose key to use, even if the inbox dropped the token)
        self._token_edit.setText(token)
        self._status_lbl.setText(
            t("p2p.send_ask_accepted", name=self._friend_name(self._friends(), fid)))
        self._on_send_clicked()

    def _refresh_ask(self, svc):
        """What the status line says about a request that is out (while nothing is
        being sent: then it is the transfer's)."""
        if self._ask is None or svc is None or not self._idle():
            return
        fid, rid = self._ask
        name = self._friend_name(svc, fid)
        state = svc.request_state(rid)
        if state == "waiting":
            from ui.p2p_friends_service import format_remaining
            self._status_lbl.setText(t("p2p.send_ask_waiting", name=name,
                                       time=format_remaining(svc.request_remaining(rid))))
        elif state in ("declined", "expired"):
            self._status_lbl.setText(t(f"p2p.send_ask_{state}", name=name))
            self._forget_ask()
        elif state != "answered":
            self._forget_ask()          # (called off, replaced, or the list was locked)

    def _withdraw_ask(self):
        """Take back a request still waiting (the friend must not be able to accept into a
        window that is gone, or one that has asked someone else), and stop listening."""
        if self._ask is not None:
            fid, rid = self._ask
            svc = self._friends()
            if svc is not None and svc.request_state(rid) == "waiting":
                svc.withdraw_request(fid, rid)
        self._forget_ask()
        if self._ask_svc is not None:
            try:
                self._ask_svc.request_answered.disconnect(self._on_ask_answered)
            except (RuntimeError, TypeError):
                pass
            self._ask_svc = None

    def _forget_ask(self):
        """The request is settled (or taken back): nothing more to wait for, so the
        network is let go of (the hold taken in _ask_friend, on the same service)."""
        self._ask = None
        if self._holding and self._ask_svc is not None:
            self._holding = False
            self._ask_svc.release()

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
        self._prep_cancel = True        # (a preparation still running stops at its next piece)
        self._withdraw_ask()
        self._friend_timer.stop()
        self._timer.stop()
        if self._handle is not None:
            # Called off before it was done: the receiver is told (and removes
            # what it had only partly received); a send that finished just stops.
            (self._handle.stop if self._marked_done else self._handle.cancel)()
            self._handle = None

    # ── Closing, and putting away ────────────────────────────────────────

    def _wants_shelving(self) -> bool:
        """✕ (and Esc, and Hide) on a transfer that is under way puts the window
        away instead of ending it: the person wanted the window gone, not the
        transfer stopped. Before a send has started, or after it is done, there
        is nothing to keep going."""
        return ((self._handle is not None or self._preparing) and not self._marked_done
                and not self._force_close and not self._closed)

    def reject(self):
        # Also what the window's own close button reaches (QDialog.closeEvent).
        if self._wants_shelving():
            self._shelve()
            return
        super().reject()

    def closeEvent(self, event):
        if self._wants_shelving():
            event.ignore()
            self._shelve()
            return
        super().closeEvent(event)

    def _ask_cancel(self):
        """The Cancel button: the only way, besides the sidebar's own, to stop
        a transfer. The receiver is told, and removes what it had only partly
        received."""
        reply = question_window_modal(
            self, t("p2p.send_title"), t("p2p.send_cancel_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.cancel_background_work()

    def cancel_background_work(self):
        """Stop the transfer and close the window (what the sidebar's cancel
        button calls, after asking for itself)."""
        self._force_close = True
        QDialog.reject(self)

    def done(self, result):
        # Every way out of the dialog ends here, so the transfer is stopped
        # whichever one was taken.
        self._stop_and_close()
        super().done(result)

    # ── The sidebar entry (see MainWindow._open_p2p_send_dialog) ─────────

    def has_shelvable_work(self) -> bool:
        return self._wants_shelving()

    def shelve_working(self) -> bool:
        """Is something moving (as opposed to waiting, for the other PC to ask for more)?"""
        return self._wants_shelving() and not self._partial_shown

    _has_shelvable_work = has_shelvable_work        # (the name the memory sweep asks)

    def shelve_progress(self) -> tuple:
        """``(done, total, name)`` for the entry's bar: how much of the offer the
        receiver has, in percent."""
        return (self._pct, 100, "") if (self._handle is not None or self._preparing) else (0, 0, "")

    def shelve_nav_label(self) -> str:
        if self._marked_done or self._partial_shown:
            return t("p2p.send_shelved_done")
        if self._preparing and self._handle is None:
            return t("p2p.send_shelved_prep", pct=self._pct)
        if self._handle is not None and (self._sent or self._pct):
            return t("p2p.send_shelved_pct", pct=self._pct)
        return t("p2p.send_shelved_nav")

    def shelve_nav_tooltip(self) -> str:
        from core import fmt_size
        lines = [self._display_name]
        if self._marked_done:
            lines.append(t("p2p.send_done"))
        elif self._preparing and self._handle is None:
            lines.append(t("p2p.send_preparing", name=self._display_name, percent=self._pct))
        elif self._handle is not None:
            lines.append(t("p2p.send_sizes", sent=fmt_size(self._sent),
                           total=fmt_size(self._file_size)))
        return "\n".join(lines)

    def _shelve(self):
        self.hide()
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.shelved.emit()
        self.shelve_status.emit()

    def unshelve(self):
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.show()
        self.raise_()
        self.activateWindow()
