"""SaveSync - owns the friend list and the messages friends send.

The friend list itself, the encryption and the DHT mailboxes are in core.p2p
(friends, friend_net). This is the part that lives with the application:

- one FriendStore and one FriendNet, created on the main window and shared by
  the Friends dialog, the Receive panel (to hand my token to a friend) and the
  Send dialog (to pick a token a friend handed me);
- an inbox. A message from a friend is a token to send them something with,
  or a text. Tokens are kept for the session with their state (usable, used,
  replaced by a newer one, run out); texts are the conversation, kept in the
  friend store (core.p2p.friends) with how far each got (pending, sent,
  delivered) - the service only presents them. Whether a message has been read
  is what the badges count. A token is good for 10 minutes from when its maker
  made it, whatever happened to it on the way, so the deadline is the earlier
  of two readings: its own stamp, and "now plus the seconds it said it had
  left". Either clock being a little off can then only make a token look older
  than it is, never younger;
- announcing: ``message_received`` fires for a message the person should be
  told about, and only for those - not while the list is locked, and not
  while they are already looking at that friend's messages. The badges are
  counted either way;
- the lock: with a lock password set, the file is encrypted with it (see
  core.p2p.friends), so the list is closed at start-up - sealed: nothing of it is
  read, so nothing is listened for - closes again after the configured minutes
  without use, and is opened with the password. Closing it saves it, stops the
  network and forgets everything decrypted; it waits for a transfer that is still
  going on, which has records to keep. While it is closed nothing is announced and
  no name or message is handed to anyone, and nothing arrives either: what friends
  send waits in their outbox and in the DHT until it is opened (texts are never
  dropped, see core.p2p.friend_net). A forgotten password is answered by
  reset_all(): the list is deleted for good, and what rested on it (sends under
  way, the token a friend may hold, the friends' settings) with it;
- when the network is up. It is a DHT session of its own, so it only runs when
  there is something for it to do: friends exist and the person has not turned
  "stay reachable" off (Settings), or the Friends dialog / Receive panel is
  open, or a token has just been sent;
- which network it is: the DHT one (core.p2p.friend_net), or - with private mode on
  (core.p2p.tor) - the direct links over Tor (core.p2p.tor_friends). Switching the setting
  makes it again the other way (``_follow_mode``); the outbox stays.
"""
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from core.p2p import attachments as _a
from core.p2p import friend_net as _fn
from core.p2p import friends as _f
from core.p2p.friends import FriendError
from i18n import t

logger = logging.getLogger(__name__)

TICK_MS = 1000
# After a token goes out the network stays up this long (to settle the put).
_SEND_GRACE_SECONDS = 120
# A token we were told about with this many seconds or fewer left is not shown.
_MIN_USEFUL_SECONDS = 5
# The inbox is for this session: bounded, and old messages are let go.
_MAX_PER_FRIEND = 30
_KEEP_SECONDS = 24 * 3600
# Wrong lock passwords in a row before the next try has to wait.
_FAILS_BEFORE_WAIT = 5
_WAIT_SECONDS = 30
# An attachment I am sending: how long, once the offer is out, to wait for the
# friend's PC to make its FIRST connection - that is all the time there is to
# limit; once connected the transfer runs for as long as it takes, whatever its
# size - and how long to keep seeding after a whole copy has gone out (the last
# bytes are in flight when the count says "done").
_SEND_CONNECT_SECONDS = 12 * 60
_SEND_LINGER_SECONDS = 30
# Offers are checked for having run out this often.
_LAPSE_EVERY = 30


def _tor_enabled() -> bool:
    from core.p2p import tor as _tor
    return _tor.enabled()


def format_remaining(seconds: float) -> str:
    """"9:41" - the time left on a token."""
    s = max(0, int(seconds + 0.999))
    return f"{s // 60}:{s % 60:02d}"


@dataclass
class FriendMessage:
    id: str
    friend_id: str
    kind: str                 # "token", "text", or "gap" (a note: some did not arrive)
    token: str
    name: str                 # who sent it, as shown when it arrived
    received_at: float
    deadline: float           # wall-clock time after which the token is no good
    created: float = 0.0      # when the token was made (its own stamp)
    read: bool = False
    used: bool = False
    replaced: bool = False    # a newer token from the same friend has come
    body: str = ""            # a text's words (plain text - never markup)
    direction: str = "in"     # "in" (from the friend) or "out" (mine)
    delivery: str = ""        # for my texts: "pending", "sent" or "delivered"
    missed: int = 0           # for a "gap": how many did not arrive
    # For kind "file" (an attachment offer, either way): {"label", "count",
    # "size", "state" (core.p2p.friends.FILE_STATES), "why", "dir", "files"}.
    attachment: Optional[dict] = None

    def remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.deadline - (time.time() if now is None else now))

    def state(self, now: Optional[float] = None) -> str:
        """A token: "active" (can be used), "used", "replaced" or "expired". A
        text of mine: how far it got ("pending", "sent", "delivered"); one from
        the friend: "received". An attachment: where it is (its ``state``)."""
        if self.kind == "file":
            return (self.attachment or {}).get("state", "failed")
        if self.kind == "text":
            return (self.delivery or "pending") if self.direction == "out" else "received"
        if self.kind == "gap":
            return "gap"
        if self.used:
            return "used"
        if self.remaining(now) <= 0:
            return "expired"
        if self.replaced:
            return "replaced"
        return "active"


@dataclass
class _Outgoing:
    """An attachment being sent in the background (see offer_file)."""
    handle: object
    total: int
    started: float
    uploaded: int = 0
    rate: float = 0.0
    peers_seen: bool = False
    done_at: float = 0.0


class P2pFriendsService(QObject):
    changed = Signal()                  # the list, a status, the inbox or the lock changed
    message_received = Signal(str, str)     # friend id, message id: tell the person
    send_result = Signal(str, bool)     # friend id, did the DHT take my token
    lock_changed = Signal(bool)         # the list was locked / unlocked
    # (friend id, offer id, SendHandle | None, reason | None): an attachment's
    # files have been prepared (or could not be) on a worker thread.
    _send_ready = Signal(str, str, object, object)

    def __init__(self, parent=None, *, store: Optional["_f.FriendStore"] = None,
                 net: Optional["_fn.FriendNet"] = None,
                 name_fn: Optional[Callable[[], str]] = None,
                 background_fn: Optional[Callable[[], bool]] = None,
                 autolock_fn: Optional[Callable[[], int]] = None,
                 media_fn: Optional[Callable[[], bool]] = None,
                 forget_prefs_fn: Optional[Callable[[], int]] = None):
        super().__init__(parent)
        self._forget_prefs_fn = forget_prefs_fn or _forget_friend_preferences
        self._name_fn = name_fn or _config_name
        self._background_fn = background_fn or _config_background
        self._autolock_fn = autolock_fn or _config_autolock
        self._media_fn = media_fn or _config_media
        self.store = store if store is not None else _f.FriendStore()
        # (a net given by the caller - a test's - is kept as it is; otherwise it is the
        # DHT one, or in private mode the Tor one, and follows the setting)
        self._net_given = net is not None
        self._net_private = False if net is not None else _tor_enabled()
        self.net = net if net is not None else self._make_net(self._net_private)
        self._messages: list = []
        self._states: dict = {}         # message id -> state, to see when one changes
        self._viewing: Optional[str] = None
        self._users = 0
        self._active_until = 0.0
        self._started = False
        self._last_active = time.time()
        self._fails = 0
        self._blocked_until = 0.0
        # A lock password closes the list from the start.
        self._locked = self.store.has_lock_password
        # Locked, but a transfer is still going on: the list is closed on screen now
        # and sealed as soon as it is over.
        self._seal_pending = False
        self._seal_retry_at = 0.0
        # Attachments. ``receiver_fn`` (set by the main window) gives the
        # receive service that takes an accepted one in; ``_sends`` holds the
        # ones I am sending, keyed by (friend id, offer id).
        self.receiver_fn: Optional[Callable] = None
        self._recv = None
        self._sends: dict = {}
        self._preparing: dict = {}  # (friend id, offer id) -> {"pct", "cancel"}: files being got ready
        self._gen = 0               # bumped when every send is stopped: later arrivals are dropped
        self._last_lapse = 0.0
        self._send_ready.connect(self._on_send_ready)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ── life ─────────────────────────────────────────────────────────────

    def _make_net(self, private: bool):
        if private:
            from core.p2p import tor_friends as _tf
            return _tf.TorFriendNet(self.store, name_fn=self._name_fn)
        return _fn.FriendNet(self.store, name_fn=self._name_fn)

    def _follow_mode(self) -> None:
        """Private mode was switched on or off in Settings: the friends' network is made
        again the other way (the one that was running shows the address it was started
        with - the DHT one is the DHT's, the Tor one Tor's). What waits in the outbox stays."""
        if self._net_given:
            return
        private = _tor_enabled()
        if private == self._net_private:
            return
        self._net_private = private
        self.net.close()
        self.net = self._make_net(private)
        self.net.boost(30)
        self.changed.emit()

    def start(self) -> None:
        """Begin looking after the friends (the main window calls this a few
        seconds after the application is up)."""
        if self._started:
            return
        self._started = True
        self.net.boost(30)
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._started = False
        self._timer.stop()
        self._stop_sends()
        self.net.close()

    def acquire(self) -> None:
        """A window that needs the network is open (balance with release())."""
        self._users += 1
        self.touch()
        self.net.boost(30)
        if not self._started:
            self.start()

    def release(self) -> None:
        self._users = max(0, self._users - 1)
        self.touch()                    # the time to lock starts when it closes

    def touch(self) -> None:
        """The list was used just now (for the lock timer)."""
        self._last_active = time.time()

    def _wanted(self, now: float) -> bool:
        if not self.store.friends:
            return False
        return bool(self._users) or now < self._active_until or bool(self._background_fn())

    # ── the lock ─────────────────────────────────────────────────────────

    @property
    def has_lock_password(self) -> bool:
        return self.store.has_lock_password

    @property
    def locked(self) -> bool:
        return self._locked and self.store.has_lock_password

    def lock(self) -> None:
        """Close the list (needs a lock password): on screen at once, and sealed -
        saved, the network stopped, everything decrypted forgotten - as soon as no
        transfer is in the middle of things."""
        if not self.store.has_lock_password or self._locked:
            return
        self._locked = True
        self._viewing = None
        self._seal_pending = True
        self._try_seal()
        self.lock_changed.emit(True)
        self.changed.emit()

    def _busy(self) -> bool:
        """Is something going on that the list has to keep a record of: files being
        got ready or sent to a friend, or one being received from a chat card?"""
        if self._sends or self._preparing:
            return True
        recv = self._recv
        return bool(recv is not None and getattr(recv, "expected", None))

    def _try_seal(self) -> None:
        """Seal the list if it is closed and nothing holds it open (see lock())."""
        if not (self._seal_pending and self._locked and self.store.has_lock_password):
            self._seal_pending = False
            return
        if self.store.sealed:
            self._seal_pending = False
            return
        if self._busy() or time.time() < self._seal_retry_at:
            return
        try:
            self.store.seal()
        except FriendError:
            logger.warning("P2P friends: the friend list could not be sealed; trying again", exc_info=True)
            self._seal_retry_at = time.time() + 30
            return
        self._seal_pending = False
        self.net.close()                    # (nobody to listen for, and nothing to do it with)
        self._messages.clear()
        self._states = {}

    def unlock_wait(self) -> float:
        """Seconds before another password may be tried (after too many wrong)."""
        return max(0.0, self._blocked_until - time.time())

    def unlock(self, password: str) -> bool:
        if not self.locked:
            return True
        if self.unlock_wait() > 0:
            return False
        if self.store.unseal(password):
            self._fails = 0
            self._locked = False
            self._seal_pending = False
            self.touch()
            self.lock_changed.emit(False)
            self.changed.emit()
            if self.store.friends:
                # (nothing was listening while it was sealed: start now, and look at
                # the mailboxes at once rather than at the next second's tick)
                self.start()
                self._tick()
            return True
        self._fails += 1
        if self._fails >= _FAILS_BEFORE_WAIT:
            self._fails = 0
            self._blocked_until = time.time() + _WAIT_SECONDS
        return False

    def set_lock_password(self, new: str, current: str = "") -> None:
        """Set (or change) the lock password: the file is encrypted with it from
        now on. Changing needs the current one."""
        self.store.set_lock_password(new, current)     # (FriendError: too short, wrong password)
        self.touch()
        self.changed.emit()

    def remove_lock_password(self, current: str) -> None:
        self.store.clear_lock_password(current)         # (FriendError for a wrong password)
        self._locked = False
        self._seal_pending = False
        self.lock_changed.emit(False)
        self.changed.emit()

    def reset_all(self) -> dict:
        """The lock password is forgotten (or the person wants a clean start):
        delete the friend list for good, and with it everything that rested on it.

        - the list itself - identity, friends, conversations, the lock password -
          for good (core.p2p.friends.FriendStore.wipe): a new friend code is made
          the next time one is needed, and nobody who was a friend can reach this
          PC, or be reached from it, until they are added again;
        - what was being sent to a friend is stopped (and the receiving end told),
          and the token waiting in the Receive panel, which a friend may hold, is
          dropped (a transfer asked for from a chat card is called off; what had
          arrived whole stays);
        - what Settings remembers about a friend (their messages silenced) goes.
        Files already received, link files the person made, and every other
        setting are not touched.

        Returns ``{"deleted": files removed, "failed": copies that could not be,
        "preferences": entries forgotten}``. FriendError when the list's file could
        not be deleted - nothing has been changed then."""
        had_lock = self.store.has_lock_password
        deleted, failed = self.store.wipe()         # (first: if this raises nothing else has been touched)
        self._stop_sends(notify=True)
        recv = self.receiver_fn() if self.receiver_fn is not None else self._recv
        if recv is not None:
            try:
                recv.revoke_friend_access()
            except Exception:
                logger.warning("P2P friends: ending what was handed to friends failed", exc_info=True)
        self.net.close()
        self._messages.clear()
        self._states = {}
        self._viewing = None
        self._locked = False
        self._seal_pending = False
        self._fails = 0
        self._blocked_until = 0.0
        self._last_active = time.time()
        forgotten = 0
        try:
            forgotten = int(self._forget_prefs_fn() or 0)
        except Exception:
            logger.warning("P2P friends: forgetting the friends' settings failed", exc_info=True)
        if had_lock:
            self.lock_changed.emit(False)
        self.changed.emit()
        return {"deleted": deleted, "failed": len(failed), "preferences": forgotten}

    def media_allowed(self) -> bool:
        """May a received picture or video be shown inside the chat (Settings)?"""
        try:
            return bool(self._media_fn())
        except Exception:
            return False

    def autolock_minutes(self) -> int:
        try:
            return max(0, int(self._autolock_fn()))
        except Exception:
            return 0

    # ── what the dialogs ask ─────────────────────────────────────────────

    @property
    def code(self) -> str:
        return self.store.code

    @property
    def my_name(self) -> str:
        return (self._name_fn() or "").strip()

    def friends(self) -> list:
        """The friends - none while the list is locked."""
        return [] if self.locked else list(self.store.friends)

    # silenced friends ----------------------------------------------------
    def is_silenced(self, fid: str) -> bool:
        f = None if self.locked else self.store.get(fid)
        return bool(f is not None and f.silenced)

    def set_silenced(self, fid: str, on: bool = True) -> None:
        """"Don't show again" for a friend's messages (and the way back). Kept in
        the sealed friend file - not in config.json, which would list the friends."""
        if self.locked:
            return
        self.store.set_silenced(fid, on)
        self.changed.emit()

    def silenced_friends(self) -> list:
        return [] if self.locked else [f for f in self.store.friends if f.silenced]

    def friend(self, fid: str):
        return None if self.locked else self.store.get(fid)

    def add_friend(self, code: str, alias: str = ""):
        """Add a friend by the code they gave. FriendError says why not."""
        f = self.store.add(code, alias)
        self.net.boost(60)
        self.touch()
        self._tick()
        self.changed.emit()
        return f

    def remove_friend(self, fid: str) -> None:
        self._stop_sends(fid)
        self.store.remove(fid)
        self._messages = [m for m in self._messages if m.friend_id != fid]
        self._tick()
        self.changed.emit()

    def rename_friend(self, fid: str, alias: str) -> None:
        self.store.set_alias(fid, alias)
        self.changed.emit()

    # linking another PC --------------------------------------------------
    def export_link(self, password: str) -> bytes:
        return _f.export_link(self.store, password)

    def import_link(self, blob: bytes, password: str) -> None:
        """Adopt the identity and friends of a link file (FriendError when the
        password is wrong or the file is not one)."""
        doc = _f.parse_link(blob, password)
        self.store.adopt(doc)
        self._reload()

    def retire_here(self) -> None:
        """Forget the identity and friends on this PC (after moving to another)."""
        self.store.retire()
        self._reload()

    def _reload(self) -> None:
        self._stop_sends()
        self.net.close()
        self._messages.clear()
        self._viewing = None
        self.changed.emit()

    # the inbox -----------------------------------------------------------
    def _chat_message(self, fid: str, e: dict, name: str) -> FriendMessage:
        att = None
        if e["kind"] == "file":
            info = e.get("f") or {}
            att = {"label": info.get("l", ""), "count": int(info.get("c") or 0),
                   "size": int(info.get("s") or 0), "state": e.get("fs", "failed"),
                   "why": e.get("fw", ""), "dir": e.get("sd", ""),
                   "files": list(e.get("sf") or [])}
        return FriendMessage(
            id=e["id"], friend_id=fid, kind=e["kind"], token="", name=name,
            received_at=float(e.get("ts") or 0), deadline=0.0, read=bool(e.get("read", True)),
            body=e.get("b", ""), direction=e["dir"], delivery=e.get("state", ""),
            missed=int(e.get("missed") or 0), attachment=att)

    def private_info(self) -> dict:
        """In private mode, what the window can say: Tor's "state" ("starting", "ready",
        "failed"), its "progress", an "error", how many friends are "online" over the
        direct link and how many "links" there are. {} when private mode is off."""
        if not self._net_private or not hasattr(self.net, "status"):
            return {}
        try:
            return self.net.status()
        except Exception:
            return {}

    def network_status(self) -> str:
        """What to say about the network: "" (nothing), "proxy" (everything goes
        through the SOCKS5 proxy), "blocked" (the proxy is on but cannot be
        used, so nothing goes out at all), "private" (the friends are reached over
        Tor), "private_starting" (Tor is not up yet) or "private_blocked" (it cannot
        be started)."""
        if self._net_private and not self._net_given:
            info = self.private_info()
            if getattr(self.net, "error", "") or info.get("state") == "failed":
                return "private_blocked"
            return "private" if info.get("state") == "ready" else "private_starting"
        from core.p2p import proxy as _proxy
        sig = _proxy.signature()
        if sig is None:
            return ""
        if sig == "invalid" or getattr(self.net, "error", ""):
            return "blocked"
        return "proxy"

    def messages(self, fid: str) -> list:
        """The conversation with a friend - texts, tokens and notes that some
        did not arrive - newest first (empty while locked)."""
        if self.locked:
            return []
        f = self.store.get(fid)
        name = f.display if f is not None else "?"
        out = [m for m in self._messages if m.friend_id == fid]
        out += [self._chat_message(fid, e, name) for e in self.store.chat(fid)]
        return sorted(out, key=lambda m: m.received_at, reverse=True)

    def message(self, mid: str) -> Optional[FriendMessage]:
        found = next((m for m in self._messages if m.id == mid), None)
        if found is not None:
            return found
        for f in self.store.friends:
            for e in f.chat:
                if e["id"] == mid:
                    return self._chat_message(f.id, e, f.display)
        return None

    def unread(self, fid: str) -> int:
        return (sum(1 for m in self._messages if m.friend_id == fid and not m.read)
                + self.store.unread_chat(fid))

    def unread_friends(self) -> int:
        """How many DIFFERENT friends have something unread (the sidebar badge)."""
        ids = {m.friend_id for m in self._messages if not m.read}
        ids |= {f.id for f in self.store.friends if self.store.unread_chat(f.id)}
        return len(ids)

    def unread_total(self) -> int:
        return (sum(1 for m in self._messages if not m.read)
                + sum(self.store.unread_chat(f.id) for f in self.store.friends))

    def mark_read(self, fid: str) -> None:
        changed = False
        for m in self._messages:
            if m.friend_id == fid and not m.read:
                m.read = True
                changed = True
        changed |= self.store.mark_chat_read(fid)
        if changed:
            self.changed.emit()

    # texts ---------------------------------------------------------------
    def text_room(self, fid: str, body: str) -> bool:
        """Is there room in my mailbox for this text (see FriendNet.text_room)?"""
        try:
            return bool(self.net.text_room(fid, body))
        except Exception:
            return False

    def send_text(self, fid: str, body: str) -> None:
        """Send a friend a text. FriendError says why not ("empty message",
        "message too long", "queue full")."""
        self.net.send_text(fid, body)
        self._active_until = time.time() + _SEND_GRACE_SECONDS
        self.touch()
        self._tick()
        self.changed.emit()

    def clear_conversation(self, fid: str) -> None:
        """Forget the conversation with this friend on this PC."""
        self._stop_sends(fid)
        self.store.clear_chat(fid)
        self._messages = [m for m in self._messages if m.friend_id != fid]
        self.changed.emit()

    def set_viewing(self, fid: Optional[str]) -> None:
        """The person is looking at this friend's messages (None: at none).
        What they see is read, and what arrives meanwhile is read as it comes."""
        self._viewing = fid or None
        if fid:
            self.touch()
            self.mark_read(fid)

    def preview(self, msg: FriendMessage) -> str:
        """A line for the notification: the text itself (one line, cut short),
        or what the token is and how long it has."""
        if msg.kind == "text":
            from core.p2p import markup
            line = " ".join(markup.plain(msg.body).split())     # (without its marks)
            return line if len(line) <= 90 else line[:89].rstrip() + "…"
        if msg.kind == "file" and msg.attachment:
            return t("p2p.friends_notice_file", name=msg.attachment["label"])
        return t("p2p.friends_notice_token", time=format_remaining(msg.remaining()))

    def tokens(self) -> list:
        """The tokens friends have sent that can still be used, soonest to run
        out first (none while the list is locked)."""
        if self.locked:
            return []
        now = time.time()
        return sorted((m for m in self._messages if m.state(now) == "active"),
                      key=lambda m: m.deadline)

    def consume(self, token: str) -> None:
        """A token has been used (a transfer was started with it)."""
        changed = False
        for m in self._messages:
            if m.token == token and (not m.used or not m.read):
                m.used = m.read = True
                changed = True
        if changed:
            self.changed.emit()

    def send_my_token(self, fid: str, token: str, ttl: float, reply_to: str = "") -> None:
        """Hand the friend *token* (which has *ttl* seconds left). The outcome
        arrives as ``send_result``. FriendError when the friend is unknown.
        *reply_to* is the attachment offer the token accepts (accept_file)."""
        self.net.send_token(fid, token, ttl, reply_to=reply_to)
        self._active_until = time.time() + _SEND_GRACE_SECONDS
        self.touch()
        self._tick()

    # ── attachments ──────────────────────────────────────────────────────
    #
    # Offering is a message (the friend sees a card, nothing moves). Accepting
    # makes a fresh transfer token on THEIR side and answers with it; mine then
    # sends to that token, in the background, and their receive service takes
    # the arrival only if it is exactly what the card said (see
    # core.p2p.attachments, ui.p2p_receive_service.start_attachment).

    def offer_file(self, fid: str, files: list, label: str = "") -> None:
        """Offer the friend *files* - ``[(path, relative path)]`` the person
        has picked and confirmed. Nothing is sent now: the files go only if the
        friend accepts (and only while this PC is running, within a day).
        FriendError says why not: "locked", "empty", "type" (a kind that is
        never sent), "name", "too_many", "missing", "no link" (could only be
        sent by copying them, which is never done), "queue full"."""
        if self.locked:
            raise FriendError("locked")
        why = _a.check_outgoing(files)
        if why:
            raise FriendError(why)
        paths, total = [], 0
        for src, rel in files:
            p = Path(src)
            try:
                size = p.stat().st_size
            except OSError:
                raise FriendError("missing")
            if not p.is_file():
                raise FriendError("missing")
            paths.append((str(p), str(rel).replace("\\", "/"), size))
            total += size
        from core.p2p.transfer import can_send_without_copy
        if not can_send_without_copy(paths[0][0]):
            raise FriendError("no link")
        info = {"l": _a.clean_label(label or Path(paths[0][0]).name), "c": len(paths), "s": total}
        self.net.send_file(fid, info, paths)
        self._active_until = time.time() + _SEND_GRACE_SECONDS
        self.touch()
        self._tick()
        self.changed.emit()

    def can_offer(self, fid: str) -> bool:
        """Is there room in my mailbox for one more attachment offer?"""
        try:
            return bool(self.net.text_room(fid, "", file={"l": "x" * 60, "c": 1, "s": 1}))
        except Exception:
            return False

    def accept_file(self, fid: str, mid: str) -> None:
        """The person clicked Accept on a friend's attachment card. FriendError
        says why not: "locked", "not available", "type" (never taken), "busy"
        (another transfer is being received), "space" (not enough room in the
        folder for it), "unavailable"."""
        if self.locked:
            raise FriendError("locked")
        e = self.store.file_entry(fid, mid)
        if e is None or e["dir"] != "in" or e["fs"] != "offered":
            raise FriendError("not available")
        info = e["f"]
        if info["c"] > _a.MAX_FILES or (info["c"] == 1 and _a.is_blocked_name(info["l"])):
            self.store.set_file_state(fid, mid, "blocked", "type")
            self.changed.emit()
            raise FriendError("type")
        recv = self.receiver_fn() if self.receiver_fn is not None else None
        if recv is None:
            raise FriendError("unavailable")
        self._recv = recv
        from core.p2p.transfer import P2pError
        try:
            recv.start_attachment(fid, mid, info["c"], info["s"])
        except P2pError as ex:
            raise FriendError(str(ex) or "error")
        self.store.set_file_state(fid, mid, "waiting")
        self.mark_read(fid)
        self.changed.emit()

    def decline_file(self, fid: str, mid: str) -> None:
        """No thanks. (The friend is not told; the card there just goes unanswered.)"""
        e = self.store.file_entry(fid, mid)
        if e is not None and e["dir"] == "in" and e["fs"] == "offered":
            self.store.set_file_state(fid, mid, "declined")
            self.changed.emit()

    def cancel_file(self, fid: str, mid: str) -> None:
        """Take back an offer of mine, stop sending it, or give up waiting for
        the friend to send what I accepted."""
        e = self.store.file_entry(fid, mid)
        if e is None:
            return
        if e["dir"] == "out" and e["fs"] in ("offered", "sending"):
            self._stop_sends(fid, mid, notify=True)         # (the receiving session is told too)
            self.store.set_file_state(fid, mid, "cancelled")
            self._tell(fid, mid, "withdraw")
        elif e["dir"] == "in" and e["fs"] in ("waiting", "receiving") and self._recv is not None:
            self._recv.cancel_attachment(mid)       # (it answers with "cancelled")
            self._tell(fid, mid, "stop")
        self.changed.emit()

    def _tell(self, fid: str, mid: str, kind: str) -> None:
        """Let the friend know the offer is off (best effort: if it never gets
        there, the other end finds out when the connection goes)."""
        try:
            self.net.send_cancel(fid, mid, kind)
            self._active_until = time.time() + _SEND_GRACE_SECONDS
            self._tick()
        except FriendError:
            logger.debug("P2P friends: could not tell a friend an offer is off", exc_info=True)

    def _on_cancel(self, fid: str, msg: dict) -> bool:
        """The friend called an attachment off. "withdraw": their offer is gone
        (or they stopped sending it) - what is arriving here is stopped and
        cleaned up, and the card says it was cancelled. "stop": they stopped
        receiving mine - the attempt ends, and the offer waits for them to
        accept again."""
        mid, kind = msg.get("re", ""), msg.get("k", "")
        e = self.store.file_entry(fid, mid)
        if e is None:
            return False
        if kind == "withdraw" and e["dir"] == "in" and e["fs"] in ("offered", "waiting", "receiving"):
            self.store.set_file_state(fid, mid, "cancelled", "by_friend")
            if e["fs"] != "offered" and self._recv is not None:
                self._recv.cancel_attachment(mid)
            return True
        if kind == "stop" and e["dir"] == "out" and e["fs"] in ("offered", "sending"):
            self._stop_sends(fid, mid)                      # (back to offered)
            self.store.set_file_state(fid, mid, "offered", "friend_stopped")
            return True
        return False

    def stream_for(self, fid: str, mid: str):
        """The core.p2p.transfer.TorrentStream of the video attachment *mid* while
        it is still arriving - so it can be watched meanwhile - or None: not one
        that is arriving, not a single video file, Settings forbid it, or the
        list is locked."""
        if self.locked or not self.media_allowed() or self._recv is None:
            return None
        exp = getattr(self._recv, "expected", None)
        if not exp or exp.get("mid") != mid or exp.get("fid") != fid:
            return None
        stream = self._recv.stream_source(mid)
        if stream is None or not _a.video_name_ok(stream.name):
            return None
        return stream

    def transfer_info(self, fid: str, mid: str) -> Optional[dict]:
        """Live figures for an attachment on the move: ``{"prep": percent}``
        while I am getting the files ready, ``{"pct", "rate"}`` while it is being
        sent or received, ``{"wait": seconds left on the token}`` while an
        accepted one waits for the friend, or ``{"preparing": True}`` once the
        friend has said they are getting it ready (the token no longer counts
        down). None otherwise."""
        prep = self._preparing.get((fid, mid))
        if prep is not None:
            return {"prep": int(prep["pct"])}
        s = self._sends.get((fid, mid))
        if s is not None:
            pct = 100 if s.done_at else (min(99, int(s.uploaded * 100 / s.total)) if s.total else 0)
            return {"pct": pct, "rate": s.rate}
        recv = self._recv
        exp = getattr(recv, "expected", None) if recv is not None else None
        if exp and exp.get("mid") == mid and exp.get("fid") == fid:
            from ui import p2p_receive_service as rs
            if recv.state == rs.DOWNLOADING:
                return {"pct": int(recv.pct), "rate": float(recv.rate),
                        "idle": float(getattr(recv, "idle_seconds", 0.0) or 0.0),
                        "tls": bool(getattr(getattr(recv, "offer", None), "tls", False))}
            if recv.state == rs.WAITING:
                if getattr(recv, "sender_preparing", False):
                    return {"preparing": True}
                return {"wait": float(recv.token_remaining)}
        return None

    def on_attachment_event(self, fid: str, mid: str, event: str, detail=None) -> None:
        """What the receive service reports about an accepted attachment."""
        st = self.store
        if event == "receiving":
            st.set_file_state(fid, mid, "receiving")
        elif event == "saved":
            d = detail if isinstance(detail, dict) else {}
            st.set_file_state(fid, mid, "saved", sd=str(d.get("dir") or ""),
                              sf=[str(x) for x in (d.get("files") or [])][:50])
        elif event == "failed":
            why = str(detail or "error")
            # A refusal (not what was offered, a kind never taken) is final; a
            # hiccup is worth another try.
            st.set_file_state(fid, mid, "offered" if why == "error" else "failed", why)
        elif event == "cancelled":
            d = detail if isinstance(detail, dict) else {}
            kept = [str(x) for x in (d.get("kept") or [])][:50]
            if (st.file_entry(fid, mid) or {}).get("fs") == "cancelled":
                return                              # (already shown as called off by the friend)
            # (what an earlier attempt kept is not this one's business: always rewritten)
            extra = {"sd": str(d.get("dir") or "") if kept else "", "sf": kept}
            if d.get("by") == "sender":
                st.set_file_state(fid, mid, "cancelled", "by_friend", **extra)
            elif d:
                st.set_file_state(fid, mid, "offered", "cancelled_kept" if kept else "cancelled_here", **extra)
            else:
                st.set_file_state(fid, mid, "offered", **extra)
        elif event == "expired":
            st.set_file_state(fid, mid, "offered", "no_answer")
        elif event == "mismatch":
            st.set_file_state(fid, mid, "offered", "different_offer")
        else:
            return
        self.changed.emit()

    # sending ---------------------------------------------------------------
    def _answer_offer(self, fid: str, mid: str, msg: dict, now: float) -> bool:
        """A token that answers (``re``) the attachment offer *mid*: the friend
        has accepted. True when it was about one of my offers (handled or
        ignored), False when there is no such offer (it is an ordinary token)."""
        from core.p2p import transfer as tr
        e = self.store.file_entry(fid, mid)
        if e is None or e["dir"] != "out":
            return False
        if e["fs"] != "offered" or (fid, mid) in self._sends:
            return True                     # over, cancelled or already under way
        token = str(msg.get("token") or "")
        try:
            tr.seed_from_token(token)       # a real token, and not run out
        except tr.P2pError:
            return True
        files = [tuple(p) for p in e.get("fp") or []]
        for path, _rel, size in files:
            try:
                ok = Path(path).is_file() and Path(path).stat().st_size == size
            except OSError:
                ok = False
            if not ok:
                # Not what was offered any more: not sent.
                self.store.set_file_state(fid, mid, "failed", "changed")
                return True
        if not files:
            self.store.set_file_state(fid, mid, "expired")
            return True
        self.store.set_file_state(fid, mid, "sending")
        label, name, gen = e["f"]["l"], self.my_name, self._gen
        total = sum(s for _p, _r, s in files)
        prep = {"pct": 0, "cancel": False}
        self._preparing[(fid, mid)] = prep

        def _progress(done, pieces):
            prep["pct"] = int(done * 100 / pieces) if pieces else 0
            return not prep["cancel"]

        def _work():
            handle, reason = None, None
            try:
                from core.machine import get_machine_id
                # However big the files are this just takes as long as it takes:
                # the friend is told we are working (core.p2p.transfer._Hold), so
                # their token's ten minutes are not what is being raced.
                handle = tr.send_files(token, [(p, r) for p, r, _s in files], name,
                                       get_machine_id(), label=label, folder_name="", attach=mid,
                                       progress=_progress)
            except tr.P2pCancelled:
                reason = "cancelled"
            except tr.P2pTorError as e:
                reason = "tor_" + e.reason          # (private mode on at one end only, or no Tor)
            except tr.P2pLinkError:
                reason = "link"
            except Exception:
                logger.warning("P2P friends: preparing an attachment failed", exc_info=True)
                reason = "error"
            try:
                self._send_ready.emit(fid, mid, handle, (reason, total, gen))
            except RuntimeError:            # the service is gone: nobody will stop it
                if handle is not None:
                    handle.stop()

        threading.Thread(target=_work, name="p2p-attachment-prepare", daemon=True).start()
        return True

    def _on_send_ready(self, fid: str, mid: str, handle, info) -> None:
        reason, total, gen = info
        self._preparing.pop((fid, mid), None)
        e = self.store.file_entry(fid, mid)
        if e is None or e["fs"] != "sending" or gen != self._gen:
            if handle is not None:          # cancelled, cleared or stopped while it was prepared
                handle.stop()
            return
        if reason or handle is None:
            again = reason in ("error", "cancelled") or str(reason).startswith("tor_")
            self.store.set_file_state(fid, mid, "offered" if again else "failed", reason or "error")
            self.changed.emit()
            return
        self._sends[(fid, mid)] = _Outgoing(handle=handle, total=int(total), started=time.time())
        self.changed.emit()

    def _pump_sends(self, now: float) -> bool:
        changed = False
        for key, s in list(self._sends.items()):
            fid, mid = key
            try:
                s.handle.pump()
                p = s.handle.progress()
            except Exception:
                logger.debug("P2P friends: polling an attachment failed", exc_info=True)
                p = {}
            uploaded, peers = int(p.get("all_time_upload", 0)), int(p.get("num_peers", 0))
            s.rate = float(p.get("upload_rate", 0))
            if peers or uploaded:
                # (a small file can be over between two polls: bytes that have
                # gone out are as good as seeing the friend connected)
                s.peers_seen = True
            s.uploaded = uploaded
            outcome = None
            if s.handle.put_confirmed is False:
                outcome = ("offered", "network")        # nobody could take the offer: try again
            elif not s.done_at and (uploaded >= s.total and s.peers_seen):
                s.done_at = now
                self.store.set_file_state(fid, mid, "sent")
                changed = True
            elif s.done_at:
                if not peers or now - s.done_at > _SEND_LINGER_SECONDS:
                    outcome = ("sent", "")
            elif not s.peers_seen and now - s.started > _SEND_CONNECT_SECONDS:
                outcome = ("offered", "no_pickup")
            # (no limit once the friend has connected: a transfer runs as long as
            # it takes; it ends when it is whole, or when it is cancelled)
            if outcome is not None:
                self._stop_send(key)
                if outcome[0] != "sent":
                    self.store.set_file_state(fid, mid, outcome[0], outcome[1])
                changed = True
        return changed

    def _stop_send(self, key, notify: bool = False) -> None:
        s = self._sends.pop(key, None)
        if s is not None:
            try:
                s.handle.cancel() if notify else s.handle.stop()
            except Exception:
                logger.debug("P2P friends: stopping an attachment failed", exc_info=True)

    def _stop_sends(self, fid: Optional[str] = None, mid: Optional[str] = None,
                    notify: bool = False) -> None:
        """Stop the attachments being sent - all, one friend's, or one. One that
        was stopped before it was done goes back to being an offer (the person
        cancelling it says so afterwards). Files still being got ready are told
        to stop hashing."""
        match = lambda k: (fid is None or k[0] == fid) and (mid is None or k[1] == mid)
        for key in [k for k in self._sends if match(k)]:
            self._stop_send(key, notify)
        for key, prep in list(self._preparing.items()):
            if match(key):
                prep["cancel"] = True
        if fid is None and mid is None:
            self._gen += 1                  # (what is still being prepared is dropped when it is ready)
        for f in self.store.friends:
            if fid is not None and f.id != fid:
                continue
            for e in f.chat:
                if (e["kind"] == "file" and e["dir"] == "out" and e.get("fs") == "sending"
                        and (mid is None or e["id"] == mid)):
                    self.store.set_file_state(f.id, e["id"], "offered")

    # ── the tick ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        now = time.time()
        changed = False
        self._follow_mode()
        if self._seal_pending:
            self._try_seal()
        minutes = self.autolock_minutes()
        if (minutes > 0 and not self._locked and self.store.has_lock_password
                and not self._users and now - self._last_active > minutes * 60):
            self.lock()                 # (it says so itself)
        events: list = []
        if self._wanted(now):
            if self._users:
                self.net.boost(15, now)
            try:
                events = self.net.pump(now)
            except Exception:
                logger.warning("P2P friends: pumping the network failed", exc_info=True)
                events = []
            for ev in events:
                kind = ev.get("kind")
                if kind == "sent":
                    self.send_result.emit(ev["friend"], bool(ev.get("ok")))
                elif kind == "token":
                    changed |= self._on_token(ev["friend"], ev["msg"], now)
                elif kind == "text":
                    changed |= self._on_text(ev["friend"], ev["msg"])
                elif kind == "cancel":
                    changed |= self._on_cancel(ev["friend"], ev["msg"])
                elif kind in ("hello", "text_sent", "delivered"):
                    changed = True
        elif self.net.running:
            self.net.close()
        changed |= self._prune(now)
        changed |= self._pump_sends(now)
        if now - self._last_lapse >= _LAPSE_EVERY:
            self._last_lapse = now
            changed |= bool(self.store.lapse_files(now))
        if events or changed:
            self.changed.emit()

    def _prune(self, now: float) -> bool:
        """Let go of what is old; True when something that shows changed (a
        token ran out, a message was dropped)."""
        per: dict = {}
        out = []
        for m in sorted(self._messages, key=lambda m: m.received_at, reverse=True):
            if now - m.received_at >= _KEEP_SECONDS:
                continue
            per[m.friend_id] = per.get(m.friend_id, 0) + 1
            if per[m.friend_id] <= _MAX_PER_FRIEND:
                out.append(m)
        self._messages = out
        states = {m.id: m.state(now) for m in out}
        changed = states != self._states
        self._states = states
        return changed

    def _on_text(self, fid: str, entry: dict) -> bool:
        """A text arrived (the store has it, unread). If the person is looking
        at this friend's messages it is read at once and not announced; a locked
        list keeps it unread and says nothing - the badge will."""
        if self._viewing == fid:
            self.store.mark_chat_read(fid)
        elif not self.locked and not self.is_silenced(fid):
            self.message_received.emit(fid, entry["id"])
        return True

    def _on_token(self, fid: str, msg: dict, now: float) -> bool:
        from core.p2p import transfer as tr
        if msg.get("re") and self._answer_offer(fid, msg["re"], msg, now):
            return True                 # an accepted attachment: sent, not shown as a token
        token = msg.get("token", "")
        try:
            tr.seed_from_token(token, check_age=False)          # only: is it a token?
            created = tr.token_created_at(token)
        except tr.P2pError:
            logger.debug("P2P friends: a friend sent something that is not a token")
            return False
        by_arrival = now + max(0, int(msg.get("ttl") or 0))
        deadline = min(created + tr.TOKEN_TTL_SECONDS, by_arrival)
        if deadline - now < _MIN_USEFUL_SECONDS:
            return False                    # it has all but run out: not worth keeping
        if any(m.friend_id == fid and m.token == token for m in self._messages):
            return False                    # the same token again
        f = self.store.get(fid)
        seen_now = self._viewing == fid
        m = FriendMessage(id=str(msg.get("id") or token), friend_id=fid, kind="token", token=token,
                          name=(f.display if f is not None else (msg.get("name") or "?")),
                          received_at=now, deadline=deadline, created=created, read=seen_now)
        # One live token per friend: the one made last wins, the others are stale.
        newest = max((x.created for x in self._messages
                      if x.friend_id == fid and x.kind == "token"), default=0.0)
        if created >= newest:
            for x in self._messages:
                if x.friend_id == fid and x.kind == "token":
                    x.replaced = True
        else:
            m.replaced = True
        self._messages.append(m)
        if not seen_now and not self.locked and not self.is_silenced(fid):
            self.message_received.emit(fid, m.id)
        return True


# ── settings ─────────────────────────────────────────────────────────────

def _config_name() -> str:
    try:
        from core.config_manager import get_config
        return str(get_config().get("p2p_username", "") or "").strip()
    except Exception:
        return ""


def _config_background() -> bool:
    try:
        from core.config_manager import get_config
        return bool(get_config().get("p2p_friends_online", True))
    except Exception:
        return True


def _forget_friend_preferences() -> int:
    """Take the friends out of Settings' list of silenced notifications
    ("friend:<id>" entries); how many there were. Nothing else in the settings
    is touched."""
    from core.config_manager import get_config
    cfg = get_config()
    current = cfg.get("suppressed_ingame_notifs", {}) or {}
    keep = {k: v for k, v in current.items() if not str(k).startswith("friend:")}
    if len(keep) != len(current):
        cfg.set("suppressed_ingame_notifs", keep)
    return len(current) - len(keep)


def _config_media() -> bool:
    try:
        from core.config_manager import get_config
        return bool(get_config().get("p2p_friends_media", True))
    except Exception:
        return False


def _config_autolock() -> int:
    try:
        from core.config_manager import get_config
        return int(get_config().get("p2p_friends_autolock_minutes", 10))
    except Exception:
        return 0


# ── the one shared instance (set by the main window) ─────────────────────

_instance: Optional[P2pFriendsService] = None


def get_friends_service() -> Optional[P2pFriendsService]:
    """The running service, or None where friends are not set up (tests, a
    dialog opened on its own)."""
    return _instance


def set_friends_service(svc: Optional[P2pFriendsService]) -> None:
    global _instance
    _instance = svc
