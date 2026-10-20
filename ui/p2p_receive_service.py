"""SaveSync - owns the P2P "receive" session so it outlives its panel.

The panel (ui.dialogs.p2p_receive_dialog) used to be the session: closing it
closed the token's DHT watch, so the person had to sit looking at it until the
sender had pasted the token in. Now the session lives here, on the main
window, and the panel is only a view of it:

- a token is good for TOKEN_TTL_SECONDS (10 minutes) from the moment it is
  made, with the panel open or closed; closing the panel keeps the session —
  and so the token — until then, and opening it again shows the SAME token.
  When the time runs out unanswered the token is replaced (panel open) or the
  session dropped (panel closed)
- an offer that arrives while the panel is closed is announced through
  ``offer_arrived`` so the main window can bring the panel up by itself
- a token is single use: accepting, declining or finishing an offer retires
  it, and the next time the panel is opened it gets a fresh one
- an accepted download keeps running with the panel closed

See core.p2p.transfer for the DHT-mailbox + BitTorrent mechanics.

Once downloaded, the save is filed as an ARCHIVE — a backup with no matching
library game (see core.backup.get_orphan_backups), through the same
core.backup.import_backup path a cloud-synced backup uses, so nothing here is
a special case for restore to handle differently.

Files someone sends by hand are different: they are downloaded straight into
the folder chosen in Settings (core.p2p.paths.files_dir) — no staging folder,
no copy afterwards — and are never indexed, tracked or expired. What the panel
shows once they are in (``received``) is only a note of where they went, kept
in memory until the person presses OK; "Move to…" relocates them from there.
"""
import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import t

logger = logging.getLogger(__name__)

POLL_MS = 1000
# How many polls to wait for libtorrent to let go of a finished download (its
# files are complete only then - see ReceiveSession.request_release) before
# going ahead anyway. Long enough for a slow disk to take in a big transfer.
_RELEASE_WAIT_TICKS = 120
# The transfer's own bookkeeping file (see core.p2p.transfer). It is not
# something that was received: a folder holding only this is empty.
_MANIFEST_NAME = "manifest.json"

# States
IDLE = "idle"                # no session
WAITING = "waiting"          # token out, no offer yet
OFFER = "offer"              # an offer is waiting for a decision
DOWNLOADING = "downloading"
DONE = "done"                # finished (imported, or saved as a bare file)
ERROR = "error"


def _unique_target(path: Path) -> Path:
    """*path*, or "name (2).ext", "name (3).ext"… when it is taken — a moved
    file never replaces one that is already there."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _current_proxy_signature():
    """What a waiting session was started under and must not outlive: the proxy
    setting (the address it shows), whether TLS is on (whether it listens for
    it, and where it leaves its address) and whether private mode (Tor) is."""
    from core.p2p import proxy as _proxy
    from core.p2p import transfer as _tr
    sig = (_proxy.signature(), _tr.tls_enabled())
    if _tr.tor_enabled():
        return (*sig, "tor")
    return None if sig == (None, False) else sig          # (None: the plain default, as ever)


def _received_dir() -> Path:
    from core.constants import USER_DATA_DIR
    return USER_DATA_DIR / "p2p_received"


def _prune_transfer_dir(path: Path, base: Path, attempts: int = 1) -> None:
    """Remove one download folder (directly under *base*) once nothing is left
    in it — and ONLY then. A file in it is something that was received, and
    what happens to those is decided by the person and by the retention
    settings, never here: only empty folders and the transfer's own manifest
    are taken away, and a folder that still holds anything stays as it is.

    *attempts* > 1 retries, because libtorrent can hold a file open for a
    moment after its torrent is removed (Windows will not delete it meanwhile)
    — only pass it off the GUI thread. Anything that is not a direct child of
    *base* is left alone: an empty torrent name would otherwise resolve to
    *base* itself."""
    path = Path(path)
    try:
        if path.resolve().parent != Path(base).resolve():
            return
    except OSError:
        return
    manifest = path / _MANIFEST_NAME
    for attempt in range(max(1, attempts)):
        if not path.exists():
            return
        held = [p for p in path.rglob("*")
                if (p.is_file() or p.is_symlink()) and p != manifest]
        if held:
            return                  # something was received: leave it all alone
        try:
            manifest.unlink(missing_ok=True)
        except OSError:
            pass
        # Deepest first; rmdir refuses a folder that is not empty.
        for dirpath, _dirs, _files in os.walk(path, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        if not path.exists():
            return
        if attempt + 1 < attempts:
            time.sleep(0.5)


class P2pReceiveService(QObject):
    offer_arrived = Signal()
    changed = Signal()
    # (files moved, [(path, error)] for those that could not be, destination,
    # [the new path of each file that was moved])
    move_finished = Signal(int, object, str, object)
    # (game name filed as an archive, "" when it could not be; downloaded file)
    archive_imported = Signal(str, str)
    # (friend id, attachment offer id, what happened, details): the transfer
    # asked for from a chat card moved on - "receiving", "saved" ({"dir",
    # "files"}), "failed" (a reason code), "expired" (the token ran out unused),
    # "cancelled" (the session was replaced) or "mismatch" (what came was not
    # what was offered, and is left for the person to decide on).
    attachment_event = Signal(str, str, str, object)
    # (why: "user" or "sender", the relative paths that had arrived whole and were
    # kept, the folder they are in): a download that was called off has been
    # cleaned up (worker thread -> here).
    cancel_finished = Signal(str, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        # The chat attachment this session was made for, if any: {"fid", "mid",
        # "count", "size"}. An offer that is exactly that one is taken at once
        # (the person said yes on the card); anything else waits for them.
        self.expected = None
        self.state = IDLE
        self.error = ""
        self.pct = 0
        self.rate = 0.0               # bytes/s while downloading
        self.imported_name = ""
        self.saved_path = None
        # True while a downloaded save is being filed as an archive (off the
        # GUI thread); the state stays DOWNLOADING until that is done.
        self.importing = False
        # Files received by hand (a "file" offer), already in their folder:
        # {"dir": Path they were received into, "files": [paths relative to
        # it], "size": bytes, "sender": str}. Only a note for the panel — in
        # memory, never in the backup index, never expired — that survives the
        # panel being closed, until the person presses OK (dismiss_received).
        self.received = None
        # True when the token on show replaced one that ran out (the panel says
        # so); cleared by the next token made on purpose.
        self.token_renewed = False
        # ...or one dropped because the proxy setting changed (a session shows the
        # address it was started under; see _proxy_changed).
        self.token_renewed_proxy = False
        self._proxy_sig = None
        # Private mode: (friend id, reply_to) of tokens waiting for the onion address to be up.
        self._token_waiting: list = []
        # The friends the token of this session has been handed to (send_token_to_friend).
        # A token is a secret that whoever holds it can use, once: it must not go to a second
        # friend while the first may still be using it (see answer_request).
        self._issued_to: set = set()
        # The friend this session's token was made for, if it was (new_session's bind_fid):
        # only that friend's SaveSync can use it, see core.p2p.transfer.
        self._bound_to = ""
        # Set by the main window: the friends service, so the panel can hand the
        # token straight to a friend (None when friends are not in use).
        self.friends = None
        self.moving = False
        self._moving_rec = None
        # A download is being called off ("user" / "sender"): the torrent is let
        # go of, then what was half written is removed (see _progress_cancel).
        self._cancelling = ""
        self._cancel_started = False
        # When something last arrived (a download that is not moving is NOT ended -
        # there is no time limit on a connection - but the person is told, and can
        # cancel: see idle_seconds).
        self._moved_at = 0.0
        self._last_pct = -1
        # [(relative path, size, bytes done, bytes/s)] of a multi-file
        # download in progress, for the panel's per-file list.
        self.file_rows: list = []
        self._file_prev: dict = {}
        self._file_speed: dict = {}
        self._file_prev_t = 0.0
        self._release_ticks = 0
        self.move_finished.connect(self._on_move_finished)
        self.archive_imported.connect(self._on_archive_imported)
        self.cancel_finished.connect(self._on_cancel_finished)
        self._panel_open = False
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._pump)
        threading.Thread(target=self._sweep_stale, name="p2p-receive-sweep",
                         daemon=True).start()

    # ── Start-up housekeeping ────────────────────────────────────────────

    def _sweep_stale(self) -> None:
        """Start-up housekeeping, off the GUI thread: tidy up what earlier runs
        left. Send-staging folders (links only) older than a day go; of the
        backup-download folders, only the EMPTY ones (see _prune_transfer_dir)
        — received files are never deleted from here, and the folder hand-sent
        files are received into is not touched at all."""
        try:
            from core.p2p.transfer import sweep_stale_staging
            sweep_stale_staging()
        except Exception:
            logger.debug("P2P: staging sweep failed", exc_info=True)
        try:
            base = _received_dir()
            if not base.is_dir():
                return
            for child in base.iterdir():
                if self.session is not None:
                    return          # a transfer has started since: leave it be
                if child.is_dir():
                    _prune_transfer_dir(child, base)
        except Exception:
            logger.debug("P2P receive: sweep failed", exc_info=True)

    # ── Panel hooks ──────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        return self.session.token if self.session is not None else ""

    @property
    def offer(self):
        return self.session.offer if self.session is not None else None

    @property
    def private_state(self) -> dict:
        """Private mode (Tor): whether the token can be used yet - Tor's start and the
        onion service being found (core.p2p.transfer.ReceiveSession.private_state); {}
        for an ordinary session."""
        if self.session is None:
            return {}
        try:
            return self.session.private_state()
        except Exception:
            return {}

    @property
    def token_remaining(self) -> float:
        """Seconds the token on show can still be used (0 when there is none,
        or an offer is already being dealt with)."""
        if self.session is None or self.state != WAITING:
            return 0.0
        return self.session.remaining()

    @property
    def sender_preparing(self) -> bool:
        """The sender has come (in time) and is still getting a large offer
        ready: the token is no longer counting down, and the wait goes on."""
        return self.session is not None and self.state == WAITING and self.session.preparing

    def send_token_to_friend(self, fid: str, reply_to: str = "") -> None:
        """Hand the token on show to a friend, with the time it has left (see
        ui.p2p_friends_service). Does nothing unless a token is out and unused.
        *reply_to* is the attachment offer the token accepts, if it does."""
        if self.friends is None or self.session is None or self.state != WAITING:
            return
        if self._bound_to and fid != self._bound_to:
            return                      # (made for another friend: it would be of no use to this one)
        self._issued_to.add(fid)
        if self.session.private and not self._reachable():
            # Private mode: a token is of no use until the onion address it leads to can be
            # found, and a friend who tries before that is told "not found" and may not try
            # again for a long while. So it goes out once the address is up (a few seconds;
            # the first start of Tor on a PC takes up to two minutes).
            if (fid, reply_to) not in self._token_waiting:
                self._token_waiting.append((fid, reply_to))
            return
        self.friends.send_my_token(fid, self.session.token, self.session.remaining(),
                                   reply_to=reply_to)

    def _reachable(self) -> bool:
        """Private mode: is the onion address of this token up (Tor running, the service found)?"""
        ps = self.private_state
        return bool(ps.get("state") == "ready" and ps.get("published"))

    def _send_waiting_tokens(self) -> None:
        """Hand over the tokens that were held back (see send_token_to_friend) once they can be used."""
        if not self._token_waiting or self.session is None or self.state != WAITING or not self._reachable():
            return
        waiting, self._token_waiting = self._token_waiting, []
        for fid, reply_to in waiting:
            try:
                self.friends.send_my_token(fid, self.session.token, self.session.remaining(), reply_to=reply_to)
            except Exception:
                logger.warning("P2P receive: handing the token to a friend failed", exc_info=True)

    def start_attachment(self, fid: str, mid: str, count: int, size: int) -> None:
        """The person accepted an attachment in the chat: make a fresh token for
        it - made for that friend, see new_session - remember what is expected, and
        answer the friend with that token. The offer that comes back is taken by itself
        only if it is exactly this one (see _take_expected). P2pError says why not."""
        from core.p2p import attachments as _a
        from core.p2p.paths import files_dir
        from core.p2p.transfer import P2pError
        if self.friends is None:
            raise P2pError("unavailable")
        if self.state in (OFFER, DOWNLOADING) or self.importing or self.moving:
            raise P2pError("busy")
        if not _a.free_space_ok(files_dir(), size):
            raise P2pError("space")
        self.new_session(bind_fid=fid)      # (a token that was waiting is replaced; this one is made for the friend)
        if self.session is None:
            raise P2pError(self.error or "no session")
        self.expected = {"fid": fid, "mid": mid, "count": int(count), "size": int(size)}
        self.session.name_filter = lambda rels, sizes: _a.check_incoming(rels, sizes, count, size)
        self.send_token_to_friend(fid, reply_to=mid)

    def answer_request(self, fid: str, rid: str) -> None:
        """A friend asked for a token (to send me a save) and the person accepted: make a
        new token for that request and hand it to them, with the time it has left. It is
        never one that was already out: a token is a secret that whoever holds it can use,
        once, so each is made for one friend. The offer that comes back waits for the
        person, as any other. P2pError says why not: "busy" - something is being received,
        or a transfer asked for from a chat card is waiting for its own token, which a new
        session would replace - or "token_out": the token of this session was given to
        another friend, who has not used it yet and would lose it."""
        from core.p2p.transfer import P2pError
        if self.friends is None:
            raise P2pError("unavailable")
        if (self.state in (OFFER, DOWNLOADING) or self.importing or self.moving
                or self._cancelling or self.expected is not None):
            raise P2pError("busy")
        if (self.state == WAITING and self.session is not None and not self.session.lapsed
                and self._issued_to - {fid}):
            raise P2pError("token_out")
        self.new_session(bind_fid=fid)
        if self.session is None or self.state != WAITING:
            raise P2pError(self.error or "no session")
        self.send_token_to_friend(fid, reply_to=rid)

    def cancel_attachment(self, mid: str) -> None:
        """Call the attachment *mid* off: give up waiting for the friend to send
        it, or - once it is arriving - stop it (see cancel_download: what was only
        half received is removed)."""
        if self.expected is None or self.expected["mid"] != mid:
            return
        if self.state == WAITING:
            self._drop_session()                # says "cancelled" itself
            self.changed.emit()
        elif self.state == DOWNLOADING:
            self.cancel_download()

    def revoke_friend_access(self) -> None:
        """The friend list was wiped (ui.p2p_friends_service.reset_all): nothing
        handed to a friend may be used any more. The token that waits - or an offer
        not yet accepted - is dropped, and the token a friend may hold dies with
        it; a transfer asked for from a chat card is called off (what had arrived
        whole stays, as with any cancel). A download the person started by hand
        is left to finish."""
        if self.state in (WAITING, OFFER):
            self._drop_session()
            self.changed.emit()
        elif self.state == DOWNLOADING and self.expected is not None:
            self.cancel_download()

    def stream_source(self, mid: str):
        """A core.p2p.transfer.TorrentStream for the attachment *mid* while it is
        arriving (so a video can be watched meanwhile), or None."""
        if (self.expected is None or self.expected["mid"] != mid or self.state != DOWNLOADING
                or self.session is None or self._cancelling or self.importing):
            return None
        return self.session.stream_source()

    # ── Calling a download off ───────────────────────────────────────────

    @property
    def idle_seconds(self) -> float:
        """How long a download in progress has gone without a byte arriving (0
        when it is moving, or there is none). It is never ended for that - a
        connection has no time limit - but a sender whose PC was switched off
        leaves it here for ever, so the person is told and can cancel."""
        if self.state != DOWNLOADING or self.importing or not self._moved_at:
            return 0.0
        return max(0.0, time.time() - self._moved_at)

    @property
    def cancelling(self) -> bool:
        """A download is being called off (the torrent is let go of, what was half
        written is removed)."""
        return bool(self._cancelling)

    def cancel_download(self) -> None:
        """The person stopped an accepted download. The torrent is let go of; the
        files that had arrived whole stay, and whatever was only partly written
        is removed (nothing else is touched). Does nothing unless a download is
        under way."""
        if self.state == DOWNLOADING and self.session is not None and not self.importing \
                and not self._cancelling and not self.session.release_requested:
            self._begin_cancel("user")

    def _begin_cancel(self, why: str) -> None:
        self._cancelling = why
        self._cancel_started = False
        self.session.request_cancel()
        self.changed.emit()

    def _progress_cancel(self) -> None:
        """Wait for libtorrent to let go of the files, then remove what is
        half-written (a worker: a file just released can need a moment)."""
        s = self.session
        if self._cancel_started or (not s.released and self._release_ticks < _RELEASE_WAIT_TICKS):
            self._release_ticks += 1
            return
        self._cancel_started = True
        why, dest = self._cancelling, str(s._save_dir or "")
        kept, kind = list(s._kept), (s.offer.kind if s.offer is not None else "file")
        root_name, save_dir = s._root_name, s._save_dir

        def _work():
            try:
                s.discard_unfinished()
                if kind != "file" and root_name and save_dir is not None:
                    _prune_transfer_dir(Path(save_dir) / root_name, _received_dir(), attempts=10)
            except Exception:
                logger.debug("P2P receive: cleaning up a called-off download failed", exc_info=True)
            self.cancel_finished.emit(why, kept, dest)

        threading.Thread(target=_work, name="p2p-receive-cancel", daemon=True).start()

    def _on_cancel_finished(self, why: str, kept, dest: str) -> None:
        if not self._cancelling or self.session is None:
            return                              # the session was dropped while it ran
        self._cancelling = ""
        self._timer.stop()
        offer, session = self.session.offer, self.session
        exp, self.expected = self.expected, None
        # Files that had arrived whole are received files like any other: the
        # usual note (OK / Move to...) says where they are.
        if kept and offer is not None and offer.kind == "file" and dest:
            size = 0
            for rel in kept:
                try:
                    size += (Path(dest) / rel).stat().st_size
                except OSError:
                    pass
            alive_dirs = [d for d in session.new_dirs if (Path(dest) / d).exists()]
            self.received = {"dir": Path(dest), "files": list(kept), "size": size,
                             "sender": (offer.sender_username or "").strip(), "new_dirs": alive_dirs}
        if exp is not None:
            self.attachment_event.emit(exp["fid"], exp["mid"], "cancelled",
                                       {"by": why, "kept": list(kept), "dir": dest})
        if why == "sender":
            self._drop_session()
            self.state = ERROR
            self.error = t("p2p.receive_sender_cancelled")
            self.changed.emit()
        elif self._panel_open:
            self.new_session()                  # (a token is meant to work once)
        else:
            self._drop_session()
            self.changed.emit()

    def _sender_cancelled(self) -> None:
        """The sender wrote a cancel into the mailbox."""
        s = self.session
        if self.state == DOWNLOADING:
            if not self.importing and not s.download_done and not s.release_requested:
                self._begin_cancel("sender")
            return
        exp, self.expected = self.expected, None
        if exp is not None:
            self.attachment_event.emit(exp["fid"], exp["mid"], "cancelled", {"by": "sender", "kept": [], "dir": ""})
        self._drop_session()
        self.state = ERROR
        self.error = t("p2p.receive_sender_cancelled")
        self.changed.emit()

    def _clear_expected(self, event: str, detail=None) -> None:
        exp, self.expected = self.expected, None
        if self.session is not None:
            self.session.name_filter = None
        if exp is not None:
            self.attachment_event.emit(exp["fid"], exp["mid"], event, detail)

    def _take_expected(self) -> bool:
        """An offer arrived on the token made for an attachment: take it on the
        spot when it is that attachment (named by the sender, the same number of
        files, the same size), and say so. Anything else is not taken by itself
        - it is shown as an ordinary offer."""
        exp, offer = self.expected, self.session.offer
        if exp is None:
            return False
        if not (offer.kind == "file" and offer.attach == exp["mid"]
                and offer.count == exp["count"] and offer.size == exp["size"]):
            self._clear_expected("mismatch")
            return False
        self.accept()
        if self.state != DOWNLOADING:
            self._clear_expected("failed", "error")
            return True
        self.attachment_event.emit(exp["fid"], exp["mid"], "receiving", None)
        return True

    def panel_opened(self) -> None:
        """The panel is up: keep (or, if the last one is spent, make) a session."""
        self._panel_open = True
        if (self.state in (DONE, ERROR) or self.session is None
                or (self.state == WAITING and self.session.lapsed)):
            self.new_session()
        else:
            self.changed.emit()

    def panel_closed(self) -> None:
        self._panel_open = False
        if self.state in (DONE, ERROR):
            self._drop_session()          # spent: nothing worth keeping
        # WAITING / OFFER: the same token stays until its 10 minutes are up
        # (see _pump); DOWNLOADING runs on and is retired when it finishes.

    # ── Session lifecycle ────────────────────────────────────────────────

    @property
    def bound_to(self) -> str:
        """The id of the friend the token on show was made for ("" if for nobody in
        particular)."""
        return self._bound_to if self.session is not None else ""

    def new_session(self, bind_fid: str = "") -> None:
        """Fresh token, fresh session — first open, "New token", or after a
        decline (a token is meant to work once). *bind_fid*: the token is made for that
        friend - it is useless without the key only the two of them have - and P2pError
        "locked" if that key is not to be had (the session that waits is left as it is)."""
        bind_key = None
        if bind_fid:
            bind_key = self.friends.bind_key(bind_fid) if self.friends is not None else None
            if not bind_key:
                from core.p2p.transfer import P2pError
                raise P2pError("locked")
        self._drop_session()
        self.token_renewed = False
        self.token_renewed_proxy = False
        try:
            from core.p2p.transfer import ReceiveSession
            sig = _current_proxy_signature()
            self.session = ReceiveSession(bind_key=bind_key) if bind_key else ReceiveSession()
            self._bound_to = bind_fid
            self._proxy_sig = sig
        except Exception as e:
            logger.warning(f"P2P receive: could not start a session: {e}")
            self.session = None
            self.state = ERROR
            from core.p2p.transfer import P2pProxyError, P2pTorError
            if isinstance(e, P2pProxyError):
                self.error = t("p2p.proxy_blocked")
            elif isinstance(e, P2pTorError):
                self.error = t(f"p2p.tor_{e.reason}")
            else:
                self.error = str(e)
            self.changed.emit()
            return
        self.state = WAITING
        self.error = ""
        self.pct = 0
        self.rate = 0.0
        self.imported_name = ""
        self.saved_path = None
        self.importing = False
        self._reset_file_rows()
        self._timer.start()
        self.changed.emit()

    def _drop_session(self) -> None:
        self._timer.stop()
        self._token_waiting = []
        self._issued_to = set()
        self._bound_to = ""
        if self.expected is not None:
            self._clear_expected("cancelled")
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                logger.debug("P2P receive: closing the session failed", exc_info=True)
        self.session = None
        self.state = IDLE

    def _token_expired(self) -> None:
        """The token ran out with nobody having used it: the person looking at
        the panel gets a new one (and is told); with the panel closed the
        session is simply dropped - the next time it opens it makes one."""
        if self.expected is not None:
            self._clear_expected("expired")     # (a token made for a chat attachment)
        if self._panel_open:
            self.new_session()
            self.token_renewed = True
            self.changed.emit()
        else:
            self._drop_session()
            self.changed.emit()

    def _proxy_changed(self) -> None:
        """The proxy setting was changed while a token waited (or an offer was not yet
        accepted): that session shows the address it was started under - it announces
        it in the DHT - so it is dropped, and with the panel open replaced by a new
        token (the old one is no use). A transfer already accepted finishes as it
        began."""
        if self.expected is not None:
            self._clear_expected("expired")
        if self._panel_open:
            self.new_session()
            self.token_renewed_proxy = True
        else:
            self._drop_session()
        self.changed.emit()

    def shutdown(self) -> None:
        self._drop_session()

    # ── Driving the session ──────────────────────────────────────────────

    def _pump(self) -> None:
        if self.session is None:
            self._timer.stop()
            return
        if self.state in (WAITING, OFFER) and self._proxy_sig != _current_proxy_signature():
            self._proxy_changed()
            return
        self.session.pump()
        self._send_waiting_tokens()
        if (self.session.sender_cancelled and not self._cancelling
                and self.state in (WAITING, OFFER, DOWNLOADING)):
            self._sender_cancelled()
            if self.session is None or self.state != DOWNLOADING:
                return
        if self.state == WAITING and self.session.offer is not None:
            self.state = OFFER
            if self._take_expected():
                self.changed.emit()
                return
            self.changed.emit()
            self.offer_arrived.emit()
        elif self.state == WAITING and self.session.lapsed:
            self._token_expired()
        elif self.state == DOWNLOADING and self._cancelling:
            self._progress_cancel()
        elif self.state == DOWNLOADING:
            self._update_download()

    def accept(self) -> None:
        if self.session is None or self.session.offer is None:
            return
        try:
            if self.session.offer.kind == "file":
                # Straight into the folder chosen in Settings (SaveSync's own
                # by default): the files are never staged and copied over.
                from core.p2p.paths import files_dir
                self.session.accept(files_dir())
            else:
                self.session.accept(_received_dir())
                # A backup is filed in the Backups page and tracked like any
                # other: the note about earlier hand-sent files has no place
                # beside it, and would otherwise come back on the next token.
                self.received = None
        except Exception as e:
            self.error = str(e)
            self.state = ERROR
            self.changed.emit()
            return
        self.state = DOWNLOADING
        self.pct = 0
        self.rate = 0.0
        self._moved_at = time.time()
        self._last_pct = -1
        self._release_ticks = 0
        self._reset_file_rows()
        self.changed.emit()

    def decline(self) -> None:
        # Declining retires the token: starting over with a fresh one means a
        # token that was seen once cannot quietly be reused for a second,
        # different offer.
        self.new_session()

    def _update_download(self) -> None:
        if self.session.release_requested:
            self._wait_for_release()
            return
        p = self.session.progress()
        if p is None:
            return
        raw = float(p.get("progress") or 0)
        is_files = self.session.offer is not None and self.session.offer.kind == "file"
        if self.session.download_error:
            self.error = self.session.download_error
            self.state = ERROR
            self._timer.stop()
            self.session.abort_stream()
            self._clear_expected("failed", self.session.refused or "error")
            self.changed.emit()
            return
        if is_files and not self.session.files_placed:
            # Still fetching the file list and giving each file its place:
            # nothing is downloading yet, and libtorrent's own progress is
            # meaningless until it is.
            self.pct = 0
            self.rate = 0.0
            self.changed.emit()
            return
        if is_files:
            # Bytes actually received per file: the torrent's own "finished"
            # is also true while nothing was wanted yet.
            complete = self.session.files_complete()
        else:
            # libtorrent's own word for "every piece is in", not a rounded
            # percentage: int(round(0.995 * 100)) is already 100, and going on
            # at that point cut the last half percent off the file.
            complete = (self.session.download_done or raw >= 1.0
                        or str(p.get("state") or "").lower().endswith(("finished", "seeding")))
        # Never 100% until the files are really whole (_finish_download sets
        # it): the last stretch is libtorrent letting go of them.
        self.pct = min(99, int(raw * 100))
        self.rate = float(p.get("download_rate") or 0)
        if self.rate > 0 or self.pct != self._last_pct:
            self._moved_at, self._last_pct = time.time(), self.pct
        if is_files:
            self._update_file_rows()
        if complete:
            # Every piece is in, but the files are written behind that: the
            # torrent is let go of, and nothing reads the files until libtorrent
            # says it has (only then is every byte in them).
            self.rate = 0.0
            self.session.request_release()
            self._wait_for_release()
            return
        self.changed.emit()

    def _wait_for_release(self) -> None:
        if not self.session.released and self._release_ticks < _RELEASE_WAIT_TICKS:
            self._release_ticks += 1
            self.changed.emit()
            return
        self._timer.stop()
        self._finish_download()

    def _finish_download(self) -> None:
        if self.session.offer is not None and self.session.offer.kind == "file":
            self._finish_files()
            return
        try:
            entry_dict, zip_path = self.session.finalize()
        except Exception as e:
            self.error = str(e)
            self.state = ERROR
            self.changed.emit()
            return
        self.saved_path = zip_path
        self.pct = 100
        offer = self.session.offer
        # The data is on disk; stop seeding/networking for a spent token, and
        # let go of the downloaded files so the archive can be moved.
        try:
            self.session.close()
        except Exception:
            pass
        # Reading the archive, writing it into the backup folder and checking
        # it takes as long as the archive is big: off the GUI thread. The
        # state stays DOWNLOADING (the panel reads "importing") until it is done.
        self.importing = True
        self.changed.emit()
        threading.Thread(target=self._import_worker,
                         args=(entry_dict, zip_path, offer),
                         name="p2p-receive-import", daemon=True).start()

    def _import_worker(self, entry_dict: dict, zip_path: Path, offer) -> None:
        name = ""
        try:
            name = self._import_as_archive(entry_dict, zip_path, offer)
        except Exception:
            logger.warning("P2P receive: importing the save failed", exc_info=True)
        if name:
            # The archive was moved out, so its download folder is empty bar
            # the manifest: take that away (a folder that still holds anything,
            # e.g. after the copy fallback, is left exactly as it is).
            _prune_transfer_dir(Path(zip_path).parent, _received_dir(), attempts=10)
        self.archive_imported.emit(name, str(zip_path))

    def _on_archive_imported(self, name: str, zip_path: str) -> None:
        if not self.importing:
            return              # the session was dropped while it ran
        self.importing = False
        self.imported_name = name
        self.saved_path = None if name else Path(zip_path)
        self.state = DONE
        self.changed.emit()
        if not self._panel_open:
            self._notify_received()

    def _reset_file_rows(self) -> None:
        self.file_rows = []
        self._file_prev = {}
        self._file_speed = {}
        self._file_prev_t = 0.0

    def _update_file_rows(self) -> None:
        """Per-file progress and speed for the panel. libtorrent reports bytes
        done per file but no per-file rate, so the speed is the change since
        the last poll, smoothed so the numbers do not flicker."""
        fp = self.session.file_progress()
        if not fp:
            return
        now = time.monotonic()
        dt = now - self._file_prev_t if self._file_prev_t else 0.0
        rows = []
        for rel, size, done in fp:
            speed = self._file_speed.get(rel, 0.0)
            if dt > 0.2:
                moved = done - self._file_prev.get(rel, done)
                inst = moved / dt if moved > 0 else 0.0
                speed = inst if not speed else 0.6 * inst + 0.4 * speed
                if inst == 0.0:
                    speed *= 0.5
                if speed < 1.0:
                    speed = 0.0
                self._file_speed[rel] = speed
            rows.append((rel, size, done, speed))
        if dt > 0.2 or not self._file_prev_t:
            self._file_prev = {rel: done for rel, _s, done in fp}
            self._file_prev_t = now
        self.file_rows = rows

    def _finish_files(self) -> None:
        """A "file" offer arrived — and is already in its folder, downloaded
        straight there. All that is left is to tell the person where it went
        (``received``); "Move to…" (move_files) can relocate it. Nothing is
        filed in the backup index — these need not be saves at all."""
        offer = self.session.offer
        try:
            dest, rels = self.session.finalize_files()
        except Exception as e:
            self.error = str(e)
            self.state = ERROR
            self._clear_expected("failed", "error")
            self.changed.emit()
            return
        exp = self.expected
        if exp is not None:
            # Taken from a friend's chat: marked as coming from the Internet,
            # like a browser's download, so Windows's own checks apply when any
            # of it is opened.
            from core.p2p import attachments as _a
            for rel in rels:
                _a.mark_from_internet(Path(dest) / rel)
        size = 0
        for rel in rels:
            try:
                size += (Path(dest) / rel).stat().st_size
            except OSError:
                pass
        self.received = {
            "dir": Path(dest), "files": list(rels), "size": size,
            "sender": (offer.sender_username or "").strip() if offer else "",
            "new_dirs": list(self.session.new_dirs),
        }
        self.imported_name = ""
        self.saved_path = None
        self.pct = 100
        self._reset_file_rows()
        self.state = DONE
        try:
            self.session.close()
        except Exception:
            pass
        self.changed.emit()
        if exp is not None:
            self._clear_expected("saved", {"dir": str(dest), "files": list(rels)})
        if not self._panel_open:
            self._notify_received()

    def move_files(self, dest) -> None:
        """Move the files just received into the folder *dest*, keeping the
        layout the sender used; a name already taken there is kept and the new
        file gets "(2)". A MOVE, not a copy — a rename on the same drive; across
        drives the files are copied and the originals removed, one by one. Off
        the GUI thread, as that can take a while."""
        rec = self.received
        dest = Path(dest)
        if not rec or self.moving:
            return
        src_dir, rels = Path(rec["dir"]), list(rec["files"])
        try:
            if dest.resolve() == src_dir.resolve():
                return              # already there
        except OSError:
            pass
        new_dirs = list(rec.get("new_dirs") or [])
        self.moving = True
        self._moving_rec = rec
        self.changed.emit()

        def _work():
            moved, failed, new_rels = 0, [], []
            from core.p2p import attachments as _a
            for rel in rels:
                src = src_dir / rel
                try:
                    target = _unique_target(dest / rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    marked = _a.has_internet_mark(src)
                    shutil.move(str(src), str(target))
                    if marked and not _a.has_internet_mark(target):
                        # (across drives a move is a copy, and a copy does not take the mark with it)
                        _a.mark_from_internet(target)
                    moved += 1
                    new_rels.append(target.relative_to(dest).as_posix())
                except OSError as e:
                    failed.append((rel, str(e)))
            # Folders this transfer made and has now emptied go; ones that were
            # there before it (and anything still in them) are left alone.
            for sub in sorted(new_dirs, key=lambda s: s.count("/"), reverse=True):
                try:
                    os.rmdir(src_dir / sub)
                except OSError:
                    pass
            self.move_finished.emit(moved, failed, str(dest), new_rels)

        threading.Thread(target=_work, name="p2p-receive-move", daemon=True).start()

    def _on_move_finished(self, moved: int, failed, dest: str, new_rels=None) -> None:
        self.moving = False
        rec = self.received
        # Only act on the files this move was about: a newer transfer may have
        # replaced the note while it ran.
        if rec is not None and rec is self._moving_rec:
            if failed:
                # Some stayed where they were; those are what is left to move.
                left = {rel for rel, _err in failed}
                rec["files"] = [r for r in rec["files"] if r in left]
            else:
                rec["dir"] = Path(dest)
                rec["files"] = list(new_rels or [])
                rec["new_dirs"] = []
        self._moving_rec = None
        self.changed.emit()

    def dismiss_received(self) -> None:
        """OK: the person has seen where the files went. The note goes; the
        files stay where they are."""
        if self.moving:
            return
        self.received = None
        if self.state == DONE:
            self.new_session()          # a fresh token for whatever comes next
        else:
            self.changed.emit()

    def _notify_received(self) -> None:
        """Download finished with the panel closed: say so, once."""
        try:
            win = self.parent()
            tray = getattr(win, "_tray", None) or getattr(win, "tray_icon", None)
            if tray is not None and hasattr(tray, "showMessage"):
                if self.received:
                    msg = t("p2p.receive_files_ready",
                            count=len(self.received["files"]),
                            path=str(self.received["dir"]))
                else:
                    msg = (t("p2p.receive_imported_detail", name=self.imported_name)
                           if self.imported_name else t("p2p.receive_title"))
                tray.showMessage(t("p2p.receive_done_title"), msg)
        except Exception:
            logger.debug("P2P receive: completion notice failed", exc_info=True)

    # ── Filing the download ──────────────────────────────────────────────

    def _import_as_archive(self, entry_dict: dict, zip_path, offer) -> str:
        """File the downloaded save as an ARCHIVE. The synthetic game_id is
        derived from the game's own computed folder name (same helper
        _game_folder_for_entry/get_install_folder_name use everywhere else)
        — NOT from who sent it. Two different friends sending the same game
        land in the same archive and share one rotation bucket; only the
        per-game AMOUNT is what p2p_max_per_game limits, never "amount per
        sender". Returns the game name on success, "" on failure (the caller
        falls back to a bare save).

        Runs on a worker thread: *offer* is the one the download was accepted
        for, taken on the GUI thread, because the session may be gone by now.
        """
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
            # The game name comes from the other side. It names a folder under
            # the backup directory, so a name that would climb out of it (or
            # be the directory itself) is refused outright.
            if (folder_name.strip(" .") == "" or "/" in folder_name
                    or "\\" in folder_name or ".." in Path(folder_name).parts):
                logger.warning("P2P receive: refusing a game name that is not a safe folder name")
                return ""
            key = hashlib.sha1(folder_name.casefold().encode("utf-8")).hexdigest()[:16]
            game_id = f"p2p_{key}"
            size_bytes = Path(zip_path).stat().st_size
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            # Every field the sender recorded (BackupEntry.from_dict — the
            # same rule import_backup follows, so a field added later is not
            # dropped here), then what is true of THIS copy overwritten. The
            # sender's cloud bookkeeping describes their provider, not ours.
            required = {"game_id": "", "game_name": "", "backup_id": "",
                        "created_at": "", "machine_id": "", "save_paths": [],
                        "zip_path": ""}
            entry = BackupEntry.from_dict({**required, **entry_dict})
            for attr in ("save_paths", "save_chains", "content_chains", "name_history"):
                value = getattr(entry, attr, [])
                setattr(entry, attr, [str(v) for v in value]
                        if isinstance(value, list) else [])
            entry.game_id = game_id
            entry.game_name = game_name
            entry.backup_id = f"{game_id}_{stamp}"
            entry.created_at = datetime.now(timezone.utc).isoformat()
            entry.machine_id = sender_machine
            entry.zip_path = ""
            entry.size_bytes = size_bytes
            entry.note = t("p2p.receive_archive_note",
                           name=(offer.sender_username or sender_machine[:8] or "?"))
            entry.exe_path = str(entry_dict.get("exe_path") or "")
            entry.cloud_metadata = {}
            entry.origin = "p2p"
            entry.planned_deletion = ""
            bm = get_backup_manager()
            # The download is MOVED into the backup folder: no second copy of
            # a large archive, and nothing left in p2p_received to clean up.
            # libtorrent may still hold the file for a moment after its torrent
            # is removed (a rename of an open file fails on Windows), so the
            # move is retried; only if it never works is the archive copied.
            filed = False
            for attempt in range(5):
                filed = bm.import_backup(entry, source_path=zip_path)
                if filed or not Path(zip_path).exists():
                    break
                time.sleep(1.0)
            if not filed and Path(zip_path).exists():
                logger.warning("P2P receive: could not move the download; copying it instead")
                filed = bm.import_backup(entry, Path(zip_path).read_bytes())
            if not filed:
                return ""
            bm.enforce_p2p_limits(game_id)
            return game_name
        except Exception as e:
            logger.warning(f"P2P receive: could not import as an archive: {e}")
            return ""
