"""SaveSync - friends over the BitTorrent DHT: the running part.

core.p2p.friends says what a friendship is made of (keys, mailboxes, sealed
messages); this drives it. One DHT-only libtorrent session (no torrent in it),
started when there is a friend to look after, does two things per friend:

- READ the friend's mailbox every few seconds (their key for me, derived from
  our shared secret) and hand the new messages up as events;
- WRITE my mailbox for them - a "hello" with my name, the tokens I send, the
  texts they have not acknowledged yet and my own acknowledgement of theirs -
  whenever it changes, and again every REFRESH_SECONDS so the item does not
  expire from the DHT.

Texts are kept in the friend store (core.p2p.friends.FriendStore) and put back
in the mailbox every time it is written, so they survive a restart and go out
again until the friend acknowledges them. send_text refuses a text that the
mailbox has no room for, rather than push another one out.

Nothing here blocks: ``pump()`` is called about once a second by the service
that owns this (ui.p2p_friends_service) and returns what happened.
"""
import json
import logging
import threading
import time
from typing import Callable, Optional

from core.p2p import friends as _f
from core.p2p import proxy as _proxy
from core.p2p.friends import FriendError

logger = logging.getLogger(__name__)


class _Link:
    """The two mailboxes of one friendship (they change every epoch, see
    core.p2p.friends) and what is going on with them."""

    def __init__(self, friend, mb: "_f.Mailboxes", now: float):
        self.fid = friend.id
        self.mb = mb
        self.at = now                       # the time of the last pump (the "now" of the properties below)
        self.out_msgs: list = []            # what my mailbox for them holds
        self.dirty = False                  # changed since it was last put
        self.inflight: Optional[list] = None    # message ids in the put underway
        self.inflight_key = b""             # the mailbox it is written to
        self.inflight_at = 0.0              # when it was made
        self.out_epoch: Optional[int] = None    # the epoch my mailbox was last written under
        self.tries = 0                      # puts since the last one that worked
        self.retry_at = 0.0
        self.next_poll = now
        self.refresh_at = 0.0
        self.last_blobs: dict = {}          # incoming mailbox key -> the last item read from it
        self.pending_tokens: set = set()    # token ids not yet confirmed stored

    @property
    def out(self) -> "_f.Channel":
        """My mailbox for the friend as it is now."""
        return self.mb.out(self.mb.epoch(self.at))

    @property
    def inc(self) -> "_f.Channel":
        """The friend's mailbox for me as it is now."""
        return self.mb.inc(self.mb.epoch(self.at))


class FriendNet:
    POLL_SECONDS = 20.0
    FAST_POLL_SECONDS = 6.0
    REFRESH_SECONDS = 45 * 60
    # After a put that reached nobody (a session that has just started has no
    # DHT nodes yet): wait this long before the next try, then give up.
    PUT_RETRY_DELAYS = (4.0, 8.0, 15.0, 30.0)
    # A put whose answer has not come in this long is taken as lost (made before
    # the DHT was ready, or under a DHT that was restarted): without this the
    # mailbox would wait for it for ever and never be written again.
    PUT_SILENCE_SECONDS = 40.0
    # Behind a proxy the first put waits for the DHT to be started again once the
    # proxy's UDP channel is up (core.p2p.transfer._new_session).
    PROXY_SETTLE_SECONDS = 6.0

    def __init__(self, store: "_f.FriendStore", name_fn: Callable[[], str] = lambda: "",
                 session_factory: Optional[Callable] = None):
        self.store = store
        self._name_fn = name_fn
        self._session_factory = session_factory
        self._ses = None
        self._proxy_sig = None              # the proxy setting the session was started under
        self._links: dict = {}              # friend id -> _Link
        self._fast_until = 0.0
        self._failed_at = 0.0
        self._failed_sig = None
        self.error = ""

    # ── set-up ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._ses is not None

    def boost(self, seconds: float, now: Optional[float] = None) -> None:
        """Look at the friends' mailboxes more often for a while (somebody is
        watching, or a token has just gone out)."""
        self._fast_until = max(self._fast_until, (time.time() if now is None else now) + seconds)

    def _drop_session(self) -> None:
        """Let go of the session, keeping the friends and what is waiting to go
        out: the next one is started under whatever the settings say by then."""
        holder = [self._ses] if self._ses is not None else []
        self._ses = None
        for link in self._links.values():
            link.inflight, link.inflight_key = None, b""
            link.dirty = True
            link.retry_at = 0.0
            link.next_poll = 0.0
        self._failed_at = 0.0
        if holder:
            threading.Thread(target=holder.clear, name="p2p-friends-close", daemon=True).start()

    def _signature(self):
        """The proxy setting now (a factory-made session, as in a test, has none)."""
        return None if self._session_factory is not None else _proxy.signature()

    def _session(self, now: float):
        # A session started with the proxy off must not go on once it is switched
        # on (nor the other way round, nor under another proxy): the address it
        # shows is the one it was started with.
        if self._ses is not None and self._signature() != self._proxy_sig:
            self._drop_session()
        if self._ses is not None:
            return self._ses
        # (a start that failed is tried again in a minute - at once if the proxy
        # setting has changed since, which may well be the reason)
        if now - self._failed_at < 60 and self._signature() == self._failed_sig:
            return None
        try:
            if self._session_factory is not None:
                self._ses = self._session_factory()
            else:
                from core.p2p import transfer as _tr
                sig = self._signature()
                self._ses = _tr._new_session()
                self._proxy_sig = sig
                if sig is not None:
                    for link in self._links.values():
                        link.retry_at = max(link.retry_at, now + self.PROXY_SETTLE_SECONDS)
            self.error = ""
        except Exception as e:
            self._failed_at = now
            self._failed_sig = self._signature()
            self.error = str(e)
            logger.warning(f"P2P friends: could not start the network: {e}")
        return self._ses

    def sync(self, now: Optional[float] = None) -> None:
        """Make the links match the friend list (a friend added or removed)."""
        now = time.time() if now is None else now
        wanted = {f.id: f for f in self.store.friends}
        for fid in [x for x in self._links if x not in wanted]:
            del self._links[fid]
        for fid, f in wanted.items():
            if fid in self._links:
                continue
            try:
                mb = _f.Mailboxes(self.store.priv, f.pub)
            except FriendError:
                logger.warning("P2P friends: a friend's key could not be used")
                continue
            link = _Link(f, mb, now)
            link.out_msgs = [_f.make_hello(self._name_fn(), now)]
            link.dirty = True
            self._links[fid] = link

    def _find_in(self, key: bytes):
        """(link, channel) of the incoming mailbox with this DHT key, or None."""
        for link in self._links.values():
            ch = link.mb.incoming_with_key(key)
            if ch is not None:
                return link, ch
        return None

    def _find_out(self, key: bytes):
        for link in self._links.values():
            if link.inflight_key == key:
                return link
        return None

    # ── sending ──────────────────────────────────────────────────────────

    def text_room(self, fid: str, body: str, now: Optional[float] = None,
                  file: Optional[dict] = None) -> bool:
        """Would a text of *body* (or an attachment offer, *file*) still fit my
        mailbox for the friend, next to everything that has to be in it (hello,
        my acknowledgement, the texts still waiting, and room for one token)?
        One that does not is refused - never one of the others dropped to make
        space."""
        now = time.time() if now is None else now
        self.sync(now)
        link, f = self._links.get(fid), self.store.get(fid)
        info = _f.clean_file_info(file) if file is not None else None
        if file is not None:
            if info is None:
                return False
            body = _f.file_fallback(info)
        else:
            body = _f.clean_text(body)
        if link is None or f is None or not body or _f.text_bytes(body) > _f.MAX_TEXT_BYTES:
            return False
        candidate = _f.make_text(f.tx_epoch or "000000", f.tx_next, body, now, file=info)
        tokens = [m for m in link.out_msgs if m["t"] in ("token", "cancel")]
        msgs = [_f.make_hello(self._name_fn(), now)] + tokens + list(f.outbox) + [candidate]
        size = len(_f.payload(msgs, self.store.ack_for(fid) or {"e": "000000", "n": 0}))
        return size + (0 if tokens else _f.TOKEN_RESERVE) <= _f.MAX_SLOT_PLAIN

    def send_text(self, fid: str, body: str, now: Optional[float] = None) -> dict:
        """Send the friend a text message. FriendError says why not: an unknown
        friend, an empty or too long text, or "queue full" (too many still
        waiting for the friend to acknowledge them). The text is kept, numbered,
        and goes out until it is acknowledged; ``text_sent`` / ``delivered``
        events follow."""
        now = time.time() if now is None else now
        self.sync(now)
        link = self._links.get(fid)
        if link is None:
            raise FriendError("not a friend")
        clean = _f.clean_text(body)
        if not clean:
            raise FriendError("empty message")
        if _f.text_bytes(clean) > _f.MAX_TEXT_BYTES:
            raise FriendError("message too long")
        if not self.text_room(fid, clean, now):
            raise FriendError("queue full")
        wire = self.store.queue_text(fid, clean, now)
        link.dirty = True
        link.retry_at = 0.0
        self.boost(90, now)
        return wire

    def send_file(self, fid: str, info: dict, paths: list, now: Optional[float] = None) -> dict:
        """Offer the friend an attachment: *info* is ``{"l", "c", "s"}`` (see
        core.p2p.friends.clean_file_info), *paths* ``[(path, relative path,
        size)]`` of the files on this PC. Travels exactly like a text (numbered,
        acknowledged, never dropped); FriendError says why not ("queue full" is
        the same one a text gets)."""
        now = time.time() if now is None else now
        self.sync(now)
        link = self._links.get(fid)
        if link is None:
            raise FriendError("not a friend")
        if _f.clean_file_info(info) is None:
            raise FriendError("not an attachment")
        if not self.text_room(fid, "", now, file=info):
            raise FriendError("queue full")
        wire = self.store.queue_text(fid, "", now, file=info, paths=paths)
        link.dirty = True
        link.retry_at = 0.0
        self.boost(90, now)
        return wire

    def send_token(self, fid: str, token: str, ttl: float,
                   now: Optional[float] = None, reply_to: str = "") -> str:
        """Leave *token* (good for *ttl* more seconds) in the friend's mailbox.
        Returns the message id; the outcome comes back as a "sent" event.
        *reply_to* is the id of the attachment offer it accepts, if it does."""
        now = time.time() if now is None else now
        self.sync(now)
        link = self._links.get(fid)
        if link is None:
            raise FriendError("not a friend")
        msg = _f.make_token(token, ttl, self._name_fn(), now, reply_to=reply_to)
        link.out_msgs.append(msg)
        link.pending_tokens.add(msg["id"])
        link.dirty = True
        link.retry_at = 0.0
        self.boost(90, now)
        return msg["id"]

    def send_cancel(self, fid: str, reply_to: str, kind: str, now: Optional[float] = None) -> str:
        """Tell the friend an attachment offer is off (``kind``: "withdraw" or
        "stop", see core.p2p.friends.make_cancel). Not worth keeping once it has
        gone out, like a token: no events follow."""
        now = time.time() if now is None else now
        self.sync(now)
        link = self._links.get(fid)
        if link is None:
            raise FriendError("not a friend")
        if kind not in _f.CANCEL_KINDS:
            raise FriendError("not a cancel")
        msg = _f.make_cancel(reply_to, kind, self._name_fn(), now)
        link.out_msgs.append(msg)
        link.dirty = True
        link.retry_at = 0.0
        self.boost(60, now)
        return msg["id"]

    # ── the loop ─────────────────────────────────────────────────────────

    def pump(self, now: Optional[float] = None) -> list:
        """Do what is due; return events: ``{"kind": "hello"|"token", "friend",
        "msg"}`` for a message that arrived, ``{"kind": "sent", "friend",
        "ids", "ok"}`` when a put of tokens has been settled."""
        now = time.time() if now is None else now
        self.sync(now)
        events: list = []
        if not self._links:
            return events
        ses = self._session(now)
        if ses is None:
            return events
        interval = self.FAST_POLL_SECONDS if now < self._fast_until else self.POLL_SECONDS
        for link in self._links.values():
            link.at = now
            epoch = link.mb.epoch(now)
            if link.out_epoch is not None and link.out_epoch != epoch:
                link.dirty = True           # a new epoch: all that waits is written under it
                link.mb.forget_before(epoch)
                link.last_blobs = {k: v for k, v in link.last_blobs.items()
                                   if link.mb.incoming_with_key(k) is not None}
            if now >= link.next_poll:
                link.next_poll = now + interval
                for e in link.mb.read_epochs(now):
                    try:
                        ses.dht_get_mutable_item(link.mb.inc(e).mailbox_pub, b"")
                    except Exception:
                        logger.debug("P2P friends: dht_get_mutable_item failed", exc_info=True)
            if link.inflight is not None and now - link.inflight_at > self.PUT_SILENCE_SECONDS:
                link.inflight, link.inflight_key = None, b""
                link.dirty = True
                link.retry_at = now
            if link.inflight is None and now >= link.retry_at \
                    and (link.dirty or now >= link.refresh_at):
                self._publish(ses, link, now)
        try:
            alerts = ses.pop_alerts()
        except Exception:
            alerts = []
        for a in alerts:
            name = type(a).__name__
            if name == "dht_mutable_item_alert":
                self._on_item(a, now, events)
            elif name == "dht_put_alert":
                self._on_put(a, now, events)
        self.store.flush()
        return events

    def _publish(self, ses, link: "_Link", now: float) -> None:
        # My hello is renewed (the name may have changed) and tokens that have
        # long run out are let go; then the mailbox is written in one go.
        hello = _f.make_hello(self._name_fn(), now)
        keep = [m for m in link.out_msgs if m["t"] in ("token", "cancel")
                and now - m["at"] <= _f.TOKEN_KEEP_SECONDS]
        link.out_msgs = [hello] + keep
        f = self.store.get(link.fid)
        texts = list(f.outbox) if f is not None else []
        plain = _f.pack_slot(link.out_msgs + texts, self.store.ack_for(link.fid))
        epoch = link.mb.epoch(now)
        ch = link.mb.out(epoch)
        sealed = ch.seal(plain)
        try:
            ses.dht_put_mutable_item(ch.mailbox_sk, ch.mailbox_pub, sealed, b"")
        except Exception:
            logger.debug("P2P friends: dht_put_mutable_item failed", exc_info=True)
            link.retry_at = now + self.PUT_RETRY_DELAYS[0]
            return
        # What actually went into the mailbox (a crowded one gives up tokens).
        link.inflight = [m["id"] for m in json.loads(plain.decode("utf-8"))["m"]]
        link.inflight_key = ch.mailbox_pub
        link.out_epoch = epoch
        link.inflight_at = now
        link.dirty = False
        link.tries += 1
        link.refresh_at = now + self.REFRESH_SECONDS

    def _on_put(self, a, now: float, events: list) -> None:
        link = self._find_out(bytes(getattr(a, "public_key", b"") or b""))
        if link is None or link.inflight is None:
            return
        ids, link.inflight, link.inflight_key = link.inflight, None, b""
        settled = [i for i in ids if i in link.pending_tokens]
        if getattr(a, "num_success", 0) > 0:
            link.tries = 0
            link.pending_tokens.difference_update(settled)
            if settled:
                events.append({"kind": "sent", "friend": link.fid, "ids": settled, "ok": True})
            stored = self.store.mark_sent(link.fid, ids)
            if stored:
                events.append({"kind": "text_sent", "friend": link.fid, "ids": stored})
            return
        if link.tries <= len(self.PUT_RETRY_DELAYS):
            link.retry_at = now + self.PUT_RETRY_DELAYS[link.tries - 1]
            link.dirty = True
            return
        # Out of tries: tell whoever is waiting, and stop carrying those tokens.
        link.tries = 0
        link.pending_tokens.difference_update(settled)
        link.out_msgs = [m for m in link.out_msgs if m["id"] not in settled]
        if settled:
            events.append({"kind": "sent", "friend": link.fid, "ids": settled, "ok": False})
        f = self.store.get(link.fid)
        if f is not None and f.outbox:
            # Texts are not given up on: try again in a minute, and on.
            link.retry_at = now + 60
            link.dirty = True

    def _on_item(self, a, now: float, events: list) -> None:
        key = bytes(getattr(a, "key", b"") or b"")
        found = self._find_in(key)
        if found is None:
            return
        link, ch = found
        self._take(link, ch, key, bytes(getattr(a, "item", b"") or b""), now, events)

    def _take(self, link: "_Link", ch: "_f.Channel", key: bytes, blob: bytes, now: float, events: list) -> None:
        """A sealed slot from the friend (read from their mailbox, or - in private mode -
        received on the direct link): open it and apply what it says. *key* tells it from
        the last one read from the same place."""
        if not blob or blob == link.last_blobs.get(key):
            return
        try:
            msgs, ack = _f.parse_slot(ch.open(blob))
        except Exception:
            logger.debug("P2P friends: a mailbox item could not be opened", exc_info=True)
            return
        link.last_blobs[key] = blob
        if not msgs and not ack:
            return
        latest = max((m["at"] for m in msgs), default=0) or now
        name = next((m["name"] for m in reversed(msgs) if m["name"]), "")
        self.store.note_seen(link.fid, name, now=min(latest, now))
        # What they say they have of mine: those texts are delivered.
        delivered = self.store.apply_ack(link.fid, ack)
        if delivered:
            events.append({"kind": "delivered", "friend": link.fid, "ids": delivered})
            link.dirty = True           # they no longer need to be in my mailbox
        texts = sorted((m for m in msgs if m["t"] == "text"), key=lambda m: m["n"])
        for m in texts:
            entry = self.store.receive_text(link.fid, m, now=now)
            if entry is not None:
                events.append({"kind": "text", "friend": link.fid, "msg": entry})
                link.dirty = True       # my acknowledgement has moved on
        for m in msgs:
            if m["t"] != "text" and self.store.remember(link.fid, m["id"]):
                events.append({"kind": m["t"], "friend": link.fid, "msg": m})

    # ── tidy-up ──────────────────────────────────────────────────────────

    def close(self) -> None:
        # Letting go of a libtorrent session waits for its network thread to
        # wind down: not on the GUI thread.
        holder = [self._ses] if self._ses is not None else []
        self._ses = None
        self._links.clear()
        if holder:
            threading.Thread(target=holder.clear, name="p2p-friends-close", daemon=True).start()
