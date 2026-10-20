"""SaveSync - friends in private mode: a direct link over Tor instead of DHT mailboxes.

core.p2p.friend_net keeps two mailboxes per friendship in the BitTorrent DHT. They are
sealed, they change every six hours, and the nodes that store them see no names - but
they DO see two addresses: the one that writes a mailbox and the one that reads it,
and two PCs that keep touching the same key are, to anyone watching those nodes, two
PCs that talk to each other. Behind the DHT there is no way to hide that (it is UDP,
and Tor carries none).

So in private mode (core.p2p.tor) the DHT is not used at all. Each friendship has two
onion addresses, one for each end, that both friends work out from the secret they
share (core.p2p.friends.Mailboxes.onion_seed): this PC serves its own and reaches for
the friend's. When both are on, a stream is up between them (core.p2p.tor_link) and
what the mailbox would have held - the very same sealed slot, with the hello, the
tokens, the numbered texts and the acknowledgement - goes down it whenever it changes,
and again every KEEPALIVE_SECONDS. Neither address is ever seen by the other PC, the
relays, or the DHT.

What that costs, and why it is the price of it: nothing waits in the network for a
friend who is off. A text stays in the outbox (numbered, kept until acknowledged, as
always) and goes the moment both are on; a token (ten minutes) needs the friend on while
it is good, and is reported as not delivered after TOKEN_GIVE_UP_SECONDS otherwise.

The part that stays the same is everything above the transport: FriendNet's sending
(text_room / send_text / send_file / send_token / send_cancel), the events it gives
back, the acknowledgements. Only where a slot goes - and where one comes from - is new.
"""
import logging
import time
from typing import Callable, Optional

from core.p2p import friend_net as _fn
from core.p2p import friends as _f
from core.p2p import tor as _tor
from core.p2p import tor_link as _link
from core.p2p.friends import FriendError

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 45.0
# A token that has been waiting this long without a link to the friend is given up on.
TOKEN_GIVE_UP_SECONDS = 180.0
# How long a try for a friend who is not there may be put off (looked at more often while
# somebody is watching: see FriendNet.boost).
RETRY_SLOW_SECONDS = 90.0
RETRY_FAST_SECONDS = 15.0
# While somebody is watching, a friend who is not there is looked for at once at most this often.
NUDGE_EVERY_SECONDS = 20.0


class _State:
    """What runs for one friendship."""

    def __init__(self, host, client):
        self.host, self.client = host, client
        self.last_plain = b""
        self.next_keepalive = 0.0
        self.was_connected = False
        self.nudged_at = 0.0
        self.hello: Optional[dict] = None
        self.hello_at = 0.0

    @property
    def connected(self) -> bool:
        return self.host.controls() > 0 or self.client.connected

    def close(self) -> None:
        self.client.close()
        self.host.close()


class TorFriendNet(_fn.FriendNet):
    """FriendNet over the direct links of private mode (see the module docstring)."""

    def __init__(self, store: "_f.FriendStore", name_fn: Callable[[], str] = lambda: "",
                 service_fn: Optional[Callable] = None):
        super().__init__(store, name_fn=name_fn, session_factory=lambda: None)
        self._service_fn = service_fn or _tor.service
        self._states: dict = {}

    # ── set-up ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return bool(self._states)

    def _signature(self):
        return "tor"

    def _drop_session(self) -> None:
        self._close_states()

    def _close_states(self) -> None:
        states, self._states = self._states, {}
        for st in states.values():
            try:
                st.close()
            except Exception:
                logger.debug("P2P friends: closing a link failed", exc_info=True)

    def status(self) -> dict:
        """For the window: Tor's state, how many friends are connected right now."""
        svc = self._service_fn()
        st = svc.status()
        return {"state": st["state"], "progress": st["progress"], "error": self.error or st["error"],
                "online": sum(1 for s in self._states.values() if s.connected),
                "links": len(self._states)}

    def online(self, fid: str, now: Optional[float] = None) -> bool:
        """Is the friend there now? In private mode that is a live stream to them, not a guess."""
        st = self._states.get(fid)
        return bool(st is not None and st.connected and not self.error)

    def _make_state(self, link: "_fn._Link", svc) -> _State:
        mb = link.mb
        mine, mine_key = _tor.identity_from_seed(mb.onion_seed(True))
        theirs, _ = _tor.identity_from_seed(mb.onion_seed(False))
        host = _link.Host(svc, mine, mine_key)
        client = _link.Client(svc, theirs, circuits=0, retry_max=RETRY_SLOW_SECONDS,
                              skip_when=lambda: host.controls() > 0, race=2)
        return _State(host, client)

    # ── the loop ─────────────────────────────────────────────────────────

    def pump(self, now: Optional[float] = None) -> list:
        """Do what is due; return events (the same kinds FriendNet gives)."""
        now = time.time() if now is None else now
        self.sync(now)
        self.feed_groups(now)
        events: list = []
        if not self._links:
            self._close_states()
            return events
        svc = self._service_fn()
        if not svc.available():
            self.error = "tor_missing"
            return events
        self.error = ""
        svc.start()
        for fid in [x for x in self._states if x not in self._links]:
            self._states.pop(fid).close()
        fast = now < self._fast_until
        for fid, link in self._links.items():
            link.at = now
            st = self._states.get(fid)
            if st is None:
                try:
                    st = self._states[fid] = self._make_state(link, svc)
                except (FriendError, _tor.TorError, OSError):
                    logger.debug("P2P friends: a link could not be made", exc_info=True)
                    continue
            st.client.retry_max = RETRY_FAST_SECONDS if fast else RETRY_SLOW_SECONDS
            self._pump_link(link, st, now, events, fast)
        self.store.flush()
        return events

    def _pump_link(self, link: "_fn._Link", st: _State, now: float, events: list, fast: bool) -> None:
        ch_in = link.mb.link_in()
        for blob in st.host.items() + st.client.items():
            self._take(link, ch_in, b"link", blob, now, events)
        connected = st.connected
        newly = connected and not st.was_connected
        st.was_connected = connected
        if not connected and fast and now - st.nudged_at >= NUDGE_EVERY_SECONDS:
            # (somebody is watching: look for the friend again at once, rather than after the
            # wait - but not back to back for as long as the window is open)
            st.nudged_at = now
            st.client.nudge()
        self._give_up_tokens(link, now, events)
        if not connected:
            return
        plain = self._slot(link, st, now)
        if not (newly or plain != st.last_plain or now >= st.next_keepalive):
            return
        sealed = link.mb.link_out().seal(plain)
        went = st.host.send(sealed)
        if st.client.send(sealed):
            went += 1
        if not went:
            return
        st.last_plain, st.next_keepalive = plain, now + KEEPALIVE_SECONDS
        ids = [m["id"] for m in _f.unpack_slot(plain)]      # (as the friend will read it)
        settled = [i for i in ids if i in link.pending_tokens]
        if settled:
            link.pending_tokens.difference_update(settled)
            events.append({"kind": "sent", "friend": link.fid, "ids": settled, "ok": True})
        stored = self.store.mark_sent(link.fid, ids)
        if stored:
            events.append({"kind": "text_sent", "friend": link.fid, "ids": stored})

    def _slot(self, link: "_fn._Link", st: _State, now: float) -> bytes:
        """What my side of the link says now: the hello (renewed with the keepalive, so the
        slot does not change every second), the tokens still good, the texts the friend has
        not acknowledged, and my acknowledgement of theirs."""
        if st.hello is None or now - st.hello_at >= KEEPALIVE_SECONDS:
            st.hello, st.hello_at = _f.make_hello(self._name_fn(), now), now
        keep = [m for m in link.out_msgs if m["t"] in _f.EPHEMERAL_KINDS
                and now - m["at"] <= _f.TOKEN_KEEP_SECONDS]
        link.out_msgs = [st.hello] + keep
        f = self.store.get(link.fid)
        texts = list(f.outbox) if f is not None else []
        return _f.pack_slot(link.out_msgs + texts, self.store.ack_for(link.fid))

    def _give_up_tokens(self, link: "_fn._Link", now: float, events: list) -> None:
        """A token that has waited for a link to the friend longer than TOKEN_GIVE_UP_SECONDS
        is dropped, and whoever is waiting for it told (it would be of no use by now)."""
        late = [m for m in link.out_msgs if m["id"] in link.pending_tokens
                and now - m["at"] > TOKEN_GIVE_UP_SECONDS]
        if not late:
            return
        ids = [m["id"] for m in late]
        link.pending_tokens.difference_update(ids)
        link.out_msgs = [m for m in link.out_msgs if m["id"] not in ids]
        events.append({"kind": "sent", "friend": link.fid, "ids": ids, "ok": False})

    # ── tidy-up ──────────────────────────────────────────────────────────

    def close(self) -> None:
        self._close_states()
        self._links.clear()
