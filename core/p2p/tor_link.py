"""SaveSync - the plumbing between libtorrent / the friend list and Tor (private mode).

libtorrent is left exactly as it is - hardlinked staging, pieces, cancel, streaming,
all of it - but it only ever talks to 127.0.0.1. Small loopback sockets carry its
connections over Tor, the way a port forward would (core.p2p.tor says why):

- the *host* is the end that waits: it serves an onion service, and what arrives is
  sorted by its first byte - ``C`` is a CONTROL stream (frames: a 4-byte length and a
  sealed message; the offer, a "hold", a "cancel", "ready", a friend's slot), ``B``
  is BitTorrent, piped to libtorrent's own loopback port;
- the *client* is the end that reaches out: it keeps one control stream to the host,
  and has a few BRIDGES - local ports that libtorrent is pointed at, each of which
  opens a Tor stream of its own to the host's BitTorrent side. Every bridge has its
  own circuit, because one Tor circuit carries only a fifth of a megabyte a second
  here and they add up (measured: 1 circuit 0.06-0.18 MB/s, 6 circuits 0.33, no more
  from 8 or 12): libtorrent sees each as another peer and spreads the work by itself.

For that to be true the bridges change three things in what passes - and only the first
messages of a connection, the rest is copied as it is (BitTorrent's own encryption is off in
this mode: Tor is end to end already): the *peer id* in the handshake is made new for each
connection, the *listen port* is taken out of the extended handshake, and so are the
"upload only" notices. libtorrent keeps ONE connection per peer id and per announced listen
port, so without the first two five of the six circuits were closed at once; the port is also
one of THIS PC's, chosen by the other side, which libtorrent would otherwise remember and
try; and a receiver that has not yet put the files in their places (it wants none of them for
a moment) is "upload only", which makes libtorrent drop the connection and never connect to
that peer again - the sender, the end that connects, would stay outside for good. The circuits
are also made ahead (``Client.warm``) once the offer has been delivered, while the receiver
reads it.

Two things this relies on, learned the hard way: whoever is behind an onion service
must never speak first (tor 0.4.9 on Windows leaves the first bytes in its buffer
for ever - the connecting end always sends the byte that says what it is), and a
connection that takes a while to set up (a rendezvous is 3-60 s) needs libtorrent's
handshake timeout raised accordingly (core.p2p.transfer._session_settings).
"""
import logging
import os
import queue
import socket
import struct
import threading
import time
from typing import Callable, Optional

from core.p2p import tor as _tor

logger = logging.getLogger(__name__)

FRAME_MAX = 64 * 1024
# What a connection says first: a control stream, a BitTorrent one, or - a stream made only so
# that its circuit is ready when the real ones come (Client.warm) - nothing more.
CONTROL, BITTORRENT, WARM = b"C", b"B", b"W"
# Frames waiting to be read: past this a flood is dropped rather than kept.
_INBOX_MAX = 256
# How many circuits carry one transfer's BitTorrent traffic.
CIRCUITS = 6
_MAX_CONNECTIONS = 24
_HELLO_TIMEOUT = 30.0               # a connection must say what it is within this
_RETRY_START, _RETRY_MAX = 1.0, 15.0
# Reaching an onion service is usually a matter of seconds and now and then a matter of minutes
# (a circuit through a slow relay; a service so new that the directories do not have it yet:
# seen, 60 s and a 300 s run of timeouts). So a try that is slow is not waited for alone: after
# RACE_AFTER seconds another goes out on circuits of its own, and so on up to the Client's
# ``race`` of them at once; the first to answer is the one used and the rest are let go.
DIAL_TIMEOUT = 120.0
RACE_AFTER = 15.0
RACE_MAX = 3


# ── frames ───────────────────────────────────────────────────────────────────

def write_frame(sock: socket.socket, data: bytes) -> None:
    if len(data) > FRAME_MAX:
        raise ValueError("frame too large")
    sock.sendall(struct.pack(">I", len(data)) + data)


def read_frame(sock: socket.socket) -> bytes:
    """One frame; ConnectionError when the other end has gone or sent nonsense."""
    head = _read_exact(sock, 4)
    (n,) = struct.unpack(">I", head)
    if n > FRAME_MAX:
        raise ConnectionError("frame too large")
    return _read_exact(sock, n) if n else b""


def _read_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("closed")
        out += chunk
    return bytes(out)


def _close(sock) -> None:
    for fn in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            fn()
        except OSError:
            pass


HANDSHAKE_LEN = 68
_HANDSHAKE_HEAD = b"\x13BitTorrent protocol"


def check_handshake(head: bytes) -> bytes:
    """*head* (the first HANDSHAKE_LEN bytes of a connection) if it is a plain BitTorrent
    handshake; ValueError for anything else - only BitTorrent is carried on a BitTorrent
    stream."""
    if len(head) != HANDSHAKE_LEN or not head.startswith(_HANDSHAKE_HEAD):
        raise ValueError("not a BitTorrent handshake")
    return head


def new_peer_id(head: bytes) -> bytes:
    """The handshake *head* with a fresh peer id (its last 20 bytes: the client's
    fingerprint kept, the rest random). libtorrent closes a second connection from a
    peer it is already connected to - the same id - so the circuits of one transfer,
    each a connection to the same peer, would be one in the end. With an id of its
    own each is a peer of its own, and they are all used (see the module docstring)."""
    head = check_handshake(head)
    return head[:HANDSHAKE_LEN - 12] + os.urandom(12)


def strip_listen_port(msg: bytes) -> bytes:
    """The extended handshake *msg* (BitTorrent message 20, sub-id 0) without its "p"
    entry - the port the peer says it listens on. libtorrent takes that to be where the
    peer can be reached and files every connection under it: the circuits of one transfer
    all say the same, so it keeps one and closes the rest; and the port it has been
    given is one of THIS PC's (the peer is a bridge here), chosen by the other side,
    that it may later try to connect to. Without it neither can happen."""
    import libtorrent as lt
    d = lt.bdecode(msg[2:])
    if not isinstance(d, dict):
        raise ValueError("not an extended handshake")
    d.pop(b"p", None)
    # Neither does either end hear that the other is "upload only" (BEP 21): a receiver that
    # is told what is in an offer before it wants any of it (files are given their places
    # first) is that for a moment, and libtorrent drops a connection between two such peers
    # and then never connects to one it has marked so - the sender, which is the one that
    # connects here, would be left outside for good.
    d.pop(b"upload_only", None)
    ext = d.get(b"m")
    if isinstance(ext, dict):
        ext.pop(b"upload_only", None)
    return msg[:2] + lt.bencode(d)


_MAX_FILTERED_MESSAGES = 8          # (the extended handshake is the first message libtorrent sends)
_MAX_MESSAGE = 1 << 20


def _copy_filtering(src, dst, ext_fn, counter) -> None:
    """Pass BitTorrent messages from *src* to *dst*, handing the extended handshake to
    *ext_fn* on the way, until it has gone by (or a few messages have): then plain copying
    goes on (the caller's loop). Whatever was read past that point is forwarded as it is."""
    buf = bytearray()
    seen = 0
    while True:
        data = src.recv(65536)
        if not data:
            if buf:
                dst.sendall(bytes(buf))
            raise ConnectionError("closed")
        buf += data
        out = bytearray()
        finished = False
        while len(buf) >= 4 and not finished:
            (n,) = struct.unpack(">I", bytes(buf[:4]))
            if n > _MAX_MESSAGE:
                raise ValueError("message too large")
            if len(buf) < 4 + n:
                break
            msg = bytes(buf[4:4 + n])
            del buf[:4 + n]
            if n >= 2 and msg[0] == 20 and msg[1] == 0:
                msg = ext_fn(msg)
                finished = True
            out += struct.pack(">I", len(msg)) + msg
            seen += 1
            finished = finished or seen >= _MAX_FILTERED_MESSAGES
        if finished:
            out += buf              # (anything after it goes through untouched)
            buf.clear()
        if out:
            dst.sendall(bytes(out))
            if counter is not None:
                counter[0] += len(out)
        if finished:
            return


def pump(a: socket.socket, b: socket.socket, on_close: Optional[Callable[[], None]] = None,
         counter: Optional[list] = None, head_ab: Optional[Callable[[bytes], bytes]] = None,
         head_ba: Optional[Callable[[bytes], bytes]] = None,
         ext_ab: Optional[Callable[[bytes], bytes]] = None,
         ext_ba: Optional[Callable[[bytes], bytes]] = None) -> None:
    """Copy bytes both ways between *a* and *b* until one ends; then close both and
    call *on_close* once. Returns at once (two threads do the work). *counter* is a
    one-item list that gets the bytes copied added to it. *head_ab* / *head_ba*: the
    first HANDSHAKE_LEN bytes going a -> b / b -> a are passed through that function
    first (it may change them, or raise ValueError: the connection is then closed);
    *ext_ab* / *ext_ba* likewise for the extended handshake that follows."""
    left = [2]
    lock = threading.Lock()

    def one(src, dst, head_fn, ext_fn):
        try:
            if head_fn is not None:
                head = _read_exact(src, HANDSHAKE_LEN)
                dst.sendall(head_fn(head))
            if ext_fn is not None:
                _copy_filtering(src, dst, ext_fn, counter)
            while True:
                data = src.recv(262144)
                if not data:
                    break
                dst.sendall(data)
                if counter is not None:
                    counter[0] += len(data)
        except (OSError, ConnectionError, ValueError):
            pass
        _close(src)
        _close(dst)
        with lock:
            left[0] -= 1
            last = left[0] == 0
        if last and on_close is not None:
            try:
                on_close()
            except Exception:
                logger.debug("Tor link: an on_close handler failed", exc_info=True)

    threading.Thread(target=one, args=(a, b, head_ab, ext_ab), name="tor-pump", daemon=True).start()
    threading.Thread(target=one, args=(b, a, head_ba, ext_ba), name="tor-pump", daemon=True).start()


def _keep(q: "queue.Queue", item) -> None:
    """Queue *item* unless the queue is full (a flood is dropped, not remembered)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        logger.debug("Tor link: a frame was dropped (the queue is full)")


def _listener() -> socket.socket:
    srv = socket.socket()
    srv.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    return srv


# ── the end that waits ───────────────────────────────────────────────────────

class Host:
    """An onion service and what arrives at it (see the module docstring).

    *bt_port* gives libtorrent's loopback port (or 0 / None: BitTorrent streams are
    refused - nothing is ready for them yet)."""

    def __init__(self, tor_service, address: str, key_blob: str,
                 bt_port: Optional[Callable[[], int]] = None):
        self._tor = tor_service
        self.address = address
        self._bt_port = bt_port
        self._inbox: "queue.Queue" = queue.Queue(maxsize=_INBOX_MAX)
        self._lock = threading.Lock()
        self._controls: list = []
        self._sticky: list = []
        self._count = 0
        self._closed = False
        self._srv = _listener()
        self.port = self._srv.getsockname()[1]
        self.bt_streams = 0
        threading.Thread(target=self._accept, name="tor-host", daemon=True).start()
        tor_service.host(address, key_blob, {_tor.ONION_PORT: self.port})

    @property
    def published(self) -> bool:
        return self._tor.hosted(self.address)

    def controls(self) -> int:
        with self._lock:
            return len(self._controls)

    def items(self) -> list:
        """The frames that have come in on control streams since the last call."""
        out = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                return out

    def send(self, blob: bytes, sticky: bool = False) -> int:
        """Send a frame on every control stream; how many it went out on. *sticky* ones
        are also sent first on every control stream that comes later (until replaced by
        ``clear_sticky``)."""
        with self._lock:
            if sticky:
                self._sticky.append(blob)
            conns = list(self._controls)
        sent = 0
        for c in conns:
            try:
                write_frame(c, blob)
                sent += 1
            except OSError:
                self._drop(c)
        return sent

    def clear_sticky(self) -> None:
        with self._lock:
            self._sticky = []

    def _drop(self, c) -> None:
        with self._lock:
            if c in self._controls:
                self._controls.remove(c)
        _close(c)

    def _accept(self) -> None:
        while not self._closed:
            try:
                c, _ = self._srv.accept()
            except OSError:
                if self._closed:
                    return
                time.sleep(0.2)
                continue
            with self._lock:
                self._count += 1
                refuse = self._count > _MAX_CONNECTIONS
            if refuse:
                self._done()
                _close(c)
                continue
            threading.Thread(target=self._serve, args=(c,), name="tor-host-conn", daemon=True).start()

    def _done(self) -> None:
        with self._lock:
            self._count -= 1

    def _serve(self, c: socket.socket) -> None:
        keep = False
        try:
            c.settimeout(_HELLO_TIMEOUT)
            kind = c.recv(1)
            if kind == CONTROL:
                keep = True
                self._control(c)
            elif kind == BITTORRENT:
                port = int(self._bt_port() or 0) if self._bt_port is not None else 0
                if port:
                    c.settimeout(None)
                    local = socket.create_connection(("127.0.0.1", port), timeout=5)
                    local.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.bt_streams += 1
                    keep = True

                    def finished():
                        self.bt_streams -= 1
                        self._done()
                    pump(c, local, on_close=finished, head_ab=check_handshake, ext_ab=strip_listen_port)
            # (WARM, or anything else: nothing is asked of us - the stream is simply let go,
            # which is all it was made for)
        except (OSError, ConnectionError):
            pass
        finally:
            if not keep:
                _close(c)
                self._done()

    def _control(self, c: socket.socket) -> None:
        c.settimeout(None)
        with self._lock:
            self._controls.append(c)
            sticky = list(self._sticky)
        try:
            for blob in sticky:
                write_frame(c, blob)
            while not self._closed:
                _keep(self._inbox, read_frame(c))
        except (OSError, ConnectionError):
            pass
        finally:
            self._drop(c)
            self._done()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._tor.unhost(self.address)
        except Exception:
            logger.debug("Tor link: unhosting failed", exc_info=True)
        _close(self._srv)
        with self._lock:
            conns, self._controls = list(self._controls), []
        for c in conns:
            _close(c)


# ── the end that reaches out ─────────────────────────────────────────────────

class Bridge:
    """A local port for libtorrent to connect to: whatever connects is carried to the
    host's BitTorrent side over a Tor stream of its own (its own circuit)."""

    def __init__(self, client: "Client", index: int):
        self.client, self.index = client, index
        self._srv = _listener()
        self.port = self._srv.getsockname()[1]
        self.live = False               # a connection is being carried (or being set up)
        self.generation = 0             # bumped whenever a stream failed: the next one is a new circuit
        self.moved = [0]                # bytes carried, all streams
        self.opened = 0                 # streams that came up
        self.last_error = ""
        self.opened_at = 0.0
        self.warmed = False             # a warming stream (see warm) got through
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, name="tor-bridge", daemon=True).start()

    def _accept(self) -> None:
        while not self.client.closed:
            try:
                c, _ = self._srv.accept()
            except OSError:
                if self.client.closed:
                    return
                time.sleep(0.2)
                continue
            with self._lock:
                busy, self.live = self.live, True
            if busy:                    # (one connection at a time; libtorrent is asked for more elsewhere)
                _close(c)
                continue
            threading.Thread(target=self._carry, args=(c,), name="tor-bridge-conn", daemon=True).start()

    def _carry(self, c: socket.socket) -> None:
        try:
            s = self.client.tor.open_stream(
                self.client.address, _tor.ONION_PORT,
                isolation=f"{self.client.tag}-b{self.index}-{self.generation}", timeout=120.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(BITTORRENT)
        except (_tor.TorError, OSError) as e:
            self.last_error = str(e)
            self._ended()
            _close(c)
            return
        if self.client.closed:              # (the transfer ended while the circuit was being made)
            _close(s)
            _close(c)
            self._ended()
            return
        self.opened += 1
        self.opened_at = time.time()
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # (each connection is a peer of its own, on both sides: see new_peer_id)
        pump(c, s, on_close=self._ended, counter=self.moved, head_ab=new_peer_id, head_ba=new_peer_id,
             ext_ab=strip_listen_port, ext_ba=strip_listen_port)

    def warm(self) -> None:
        """Make this bridge's circuit now, without a connection to carry: a stream is opened
        on it and let go at once, and the next one with the same isolation key reuses what was
        built (a rendezvous takes 3-60 s; this is that time not spent when the receiver says it
        is ready). Does nothing while a connection is being carried."""
        with self._lock:
            if self.live:
                return
        isolation = f"{self.client.tag}-b{self.index}-{self.generation}"

        def work():
            try:
                s = self.client.tor.open_stream(self.client.address, _tor.ONION_PORT,
                                                isolation=isolation, timeout=120.0)
                try:
                    s.sendall(WARM)
                finally:
                    _close(s)
                self.warmed = True
            except (_tor.TorError, OSError):
                pass

        threading.Thread(target=work, name="tor-bridge-warm", daemon=True).start()

    def _ended(self) -> None:
        with self._lock:
            self.live = False
            # (whatever ended it, the next stream is made on a circuit of its own: the
            # last one may well be the trouble)
            self.generation += 1

    def close(self) -> None:
        _close(self._srv)


class Client:
    """The reaching-out end: one control stream to the host at *address*, kept up
    (re-made when it drops), and the bridges."""

    def __init__(self, tor_service, address: str, circuits: int = CIRCUITS,
                 retry_max: float = _RETRY_MAX, skip_when: Optional[Callable[[], bool]] = None,
                 race: int = RACE_MAX):
        """*circuits*: how many bridges (0: a control stream only, as a friend link has).
        *retry_max*: the most a wait between two tries can grow to (a friend who is not
        there is looked for, not hammered at). *skip_when*: while it says True no try is
        made (the other end is connected to us already). *race*: how many tries at once a
        slow one is allowed to grow to (see RACE_AFTER)."""
        self.tor = tor_service
        self.address = address
        self.tag = os.urandom(6).hex()
        self.closed = False
        self._inbox: "queue.Queue" = queue.Queue(maxsize=_INBOX_MAX)
        self._wlock = threading.Lock()
        self._ctl: Optional[socket.socket] = None
        self._sticky: list = []
        self._generation = 0
        self._wake = threading.Event()
        self._skip_when = skip_when
        self.retry_max = retry_max
        self.race = max(1, int(race))
        self.attempts = 0
        self.last_error = ""
        self.connected_at = 0.0
        self.bridges = [Bridge(self, i) for i in range(max(0, circuits))]
        threading.Thread(target=self._control_loop, name="tor-client", daemon=True).start()

    def nudge(self) -> None:
        """Try again now rather than after the wait (somebody is looking)."""
        self._wake.set()

    def warm(self) -> None:
        """Have the circuits of the bridges made before anything needs them (Bridge.warm)."""
        for b in self.bridges:
            b.warm()

    @property
    def connected(self) -> bool:
        return self._ctl is not None

    def items(self) -> list:
        out = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                return out

    def send(self, blob: bytes, sticky: bool = False) -> bool:
        """Send a frame if the control stream is up (True), else not (False). *sticky*
        frames are kept and sent first on every control stream made from now on."""
        with self._wlock:
            if sticky:
                self._sticky.append(blob)
            ctl = self._ctl
            if ctl is None:
                return False
            try:
                write_frame(ctl, blob)
                return True
            except OSError:
                self._lose(ctl)
                return False

    def _lose(self, ctl) -> None:
        if self._ctl is ctl:
            self._ctl = None
        _close(ctl)

    def _dial(self) -> Optional[socket.socket]:
        """A stream to the host, made by the first of several tries to get there (see
        RACE_AFTER); None when every try failed (``last_error`` says how) or this client was
        closed meanwhile. Tries that come in after the winner are closed as they do."""
        results: "queue.Queue" = queue.Queue()
        decided = threading.Event()

        def attempt(n: int) -> None:
            try:
                s = self.tor.open_stream(self.address, _tor.ONION_PORT, timeout=DIAL_TIMEOUT,
                                         isolation=f"{self.tag}-c{self._generation}-{n}")
            except (_tor.TorError, OSError) as e:
                results.put((None, e))
                return
            if decided.is_set() or self.closed:
                _close(s)                   # (too late: another got there first)
                return
            results.put((s, None))

        started, pending = 0, 0
        next_try = time.monotonic()
        end = next_try + DIAL_TIMEOUT + 5
        winner = None
        while winner is None and not self.closed:
            now = time.monotonic()
            if started < self.race and now >= next_try and now < end - RACE_AFTER:
                threading.Thread(target=attempt, args=(started,), name="tor-dial", daemon=True).start()
                started, pending, next_try = started + 1, pending + 1, now + RACE_AFTER
            if pending == 0 or now >= end:
                break
            try:
                s, err = results.get(timeout=0.5)
            except queue.Empty:
                continue
            pending -= 1
            if s is not None:
                winner = s
            else:
                # (a try that FAILS says the service is not up, and another at once would say
                # the same: only a slow one is raced; the retry is the caller's, after its wait)
                self.last_error = str(err)
                if pending == 0:
                    break
        decided.set()
        if winner is not None and self.closed:
            _close(winner)
            return None
        return winner

    def _control_loop(self) -> None:
        delay = _RETRY_START
        while not self.closed:
            if not self.tor.ready or (self._skip_when is not None and self._skip_when()):
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            self.attempts += 1
            try:
                s = self._dial()
                if s is None:
                    if self.closed:
                        return
                    raise _tor.TorError(self.last_error or "the onion service could not be reached")
                s.sendall(CONTROL)
                with self._wlock:
                    self._ctl = s
                    for blob in self._sticky:
                        write_frame(s, blob)
                self.connected_at = time.time()
                self.last_error = ""
                delay = _RETRY_START
                while not self.closed:
                    _keep(self._inbox, read_frame(s))
            except (_tor.TorError, OSError, ConnectionError) as e:
                self.last_error = str(e)
            finally:
                with self._wlock:
                    ctl, self._ctl = self._ctl, None
                if ctl is not None:
                    _close(ctl)
            if self.closed:
                return
            self._generation += 1       # (a new circuit: the last one may be the trouble)
            self._wake.wait(min(delay, self.retry_max))
            self._wake.clear()
            delay = min(self.retry_max, delay * 1.7)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._wake.set()
        with self._wlock:
            ctl, self._ctl = self._ctl, None
        if ctl is not None:
            _close(ctl)
        for b in self.bridges:
            b.close()
