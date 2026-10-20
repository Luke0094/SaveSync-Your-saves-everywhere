"""SaveSync - P2P save transfer over BitTorrent's public DHT.

Sends a backup archive - or any files picked by hand - directly from one
SaveSync instance to another, peer to peer, with no server either side has to
run: the sender creates a torrent for it and seeds it (hard-linked, never
copied), the receiver downloads it once they accept - ordinary,
heavily-used BitTorrent machinery, not something built from scratch.

How the receiver's app learns the sender's magnet link: a DHT "mutable item"
(BEP44) acts as a one-time mailbox, addressed by a keypair the receiver
generates fresh for this transfer. A short random token (15 bytes, in base32)
is what gets handed to the sender, short enough to copy/paste; hashing it
gives the 32-byte SEED the keypair comes from - never the keys themselves. The
sender derives the same keypair from it and writes the offer into the mailbox;
the receiver, already watching that same address, reads it back. No salt is
used - a fresh keypair per transfer already makes the address unique.

What keeps it between the two of them (see core.p2p.secret for the keys):
- The offer in the mailbox is SEALED under a key derived from the token, so
  the DHT nodes that store it see noise. Inside is the magnet link, i.e. the
  torrent's info-hash - which is therefore a secret only the two share.
- That info-hash is announced NOWHERE: no tracker, and the DHT, local peer
  discovery and peer exchange are all off for the torrent. The two sides find
  each other at a "rendezvous" instead (see _Rendezvous): a hash derived from
  the token, which both announce and look up in the DHT.
- BitTorrent's protocol encryption is mandatory in both directions (RC4 only;
  a plain peer is refused). Its key mixes the info-hash, so a stranger who
  does not know it can neither join the swarm nor read the traffic.
A token made FOR A FRIEND (a request they accepted, an attachment they offered) is bound
to that friendship: it carries one more flag (the same byte as the private-mode flag), and
the seed everything above comes from is the token's own mixed with a key only the two
friends have (core.p2p.friends, FriendStore.bind_key). So whoever merely holds the token -
it leaked, or the friend passed it on without the key - cannot find the mailbox, write an
offer, read one, or reach the rendezvous: it is not their token, whatever it says. A token
pasted by hand or handed over with "send to friend" is not bound: it is what it always was,
a secret that whoever holds it can use once.
The token's ten minutes are for FIRST CONTACT only - the sender reaching the
receiver's mailbox - and say nothing about how long a transfer takes or how big
it is. A large offer can need a long time to be prepared (every piece of it is
hashed before the torrent exists), so a sender that is still busy after
HOLD_AFTER_SECONDS leaves a small sealed "hold" in the mailbox, again every
HOLD_EVERY_SECONDS: the receiver reads it as the sender having arrived, stops
counting the token down, and goes on waiting for the offer for as long as the
sender keeps saying it is working (HOLD_STALE_SECONDS after the last word, it
gives up). Once the offer is accepted nothing in here has a time limit at all.
Either end can call a transfer off at any time. The sender writes a sealed
``{"cancel": 1}`` into the same mailbox (the receiver's session keeps reading it
after the offer, stops, and removes what it had only partly written); the
receiver simply lets go of the torrent (request_cancel) and the sender sees its
peer leave. A video can be watched while it is still arriving (TorrentStream):
it is handed only pieces that have arrived AND match the torrent's own hash.
What this is not: the data is not encrypted a second time inside the files
(that would need an encrypted copy of everything sent, and sending is
link-only on purpose), so the cipher is BitTorrent's, not a modern AEAD. What
protects the files is therefore the secrecy of the info-hash (the cipher's key
mixes it in): it is in the sealed offer only, and the torrent's name carries 128
random bits so it cannot be worked out even for a file everybody has. Every
mailbox item is padded to one size before it is sealed (see secret.PAD_TO).
The two PCs' addresses are seen by each other and by the DHT nodes; to show
a SOCKS5 proxy's instead, see core.p2p.proxy (everything then goes through it,
or nothing goes at all).
Private mode (core.p2p.tor, optional) hides them from each other and from the DHT: the
receiver's token is one byte longer and leads to an onion address that both sides work
out from it; the receiver waits there (a ``Host``), the sender reaches it (a ``Client``),
and the offer, the "hold" notes, the cancel and the receiver's "ready" cross on a control
stream instead of through a DHT mailbox, sealed under the token just the same. libtorrent
is left as it is but listens on 127.0.0.1 only, with no DHT, no router mapping and no
uTP; the sender's connections go out through six loopback bridges, each over a Tor
circuit of its own (core.p2p.tor_link), and are what the receiver's libtorrent is
connected to. Both sides must be in private mode (P2pTorError otherwise), and a PC that
cannot start Tor sends nothing - never the open way.
TLS (core.p2p.tls, optional): the torrent is an SSL torrent under a certificate
authority derived from the token, every connection is TLS and a plain peer is
refused. A magnet link cannot start one, so the offer carries the description of a
small first torrent that in turn carries the real one's (see _seed_meta_torrent and
ReceiveSession._accept_tls). Not behind the proxy: tls_enabled().
Speed: the connection is tried over TCP first - the speed of the line - and
over uTP, the fallback that gets through a router with no open port, only when
nobody has connected after _Rendezvous.TCP_FIRST_SECONDS (uTP is user-space and
about a fifth as fast on a fast line); received data goes through the operating
system's disk cache; and the first announces at the meeting point follow quickly
(the very first is lost, the DHT not being up yet). See the README, "Speed".

The 64-byte "expanded" Ed25519 private key libtorrent's DHT signing wants
is NOT the libsodium/NaCl secret-key format (seed + public key) — it is
RFC 8032's own expanded form, clamp(SHA512(seed)). Confirmed against
libtorrent's own vendored ed25519 source (src/ed25519/keypair.cpp) and
verified end to end against the live public DHT (a put reporting
num_success > 0, a get round-tripping the exact bytes back). Getting this
byte layout wrong does not error or crash anything — libtorrent signs with
whatever garbage scalar/prefix bytes result and the write is simply
rejected by every node that correctly verifies it, which reads identically
to "the network doesn't support this" unless the key format is already
suspected. It very much does support it; the first several attempts here
just used the wrong 64 bytes.

The public surface:
- ReceiveSession — generate a token, poll for an incoming offer, accept or
  decline it, track the download, then finalize() it (a backup) or
  finalize_files() it (hand-picked files, already downloaded straight into
  their folder - see plan_targets).
- send_backup / send_files — create a torrent for one archive / for the
  picked files, seed it, and publish the offer under a token the receiver
  already generated.

What actually travels where is split in two, because BEP44 mutable items
are capped at 1000 bytes total — nowhere near enough for a full
core.backup.BackupEntry (save_paths, exe_path, save/content chains can add
up well past that for a game with several tracked folders):

- The DHT mailbox (sealed) carries only what the receiver needs to show a
  decision BEFORE downloading anything — the magnet link, the sender's
  name/machine id, the game name, the filename and size. All short strings,
  always comfortably under the limit.
- A backup's torrent carries a small ``manifest.json`` alongside the save
  archive, holding the sender's full BackupEntry (as a plain dict) and the
  archive's real file name; hand-picked files travel with no manifest at
  all. Nothing caps a torrent's payload size, so the receiver only ever
  reads this AFTER accepting — by then the transfer is already happening and
  reading one more small file costs nothing extra.
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from base64 import b32decode, b32encode, b64decode, b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.p2p import proxy as _proxy
from core.p2p import secret as _secret
from core.p2p import tls as _tls
from core.p2p import tor as _tor
from core.p2p import tor_link as _link

logger = logging.getLogger(__name__)


def _say(key: str, **kwargs) -> str:
    """*key* in the user's language, for the errors a transfer hands to the window.
    The key itself when there is no running app to say it."""
    try:
        from i18n import t
        return t(key, **kwargs)
    except Exception:
        return key


# ~120 bits of entropy — plenty for an address that exists for one transfer
# and is thrown away right after, not a long-lived secret. Behind them sits the
# moment the token was made (seconds since _TOKEN_EPOCH), so a token carries
# its own clock: it can be used for TOKEN_TTL_SECONDS and not a moment longer,
# wherever it ends up (pasted into a chat, handed to a friend and forgotten).
_TOKEN_RANDOM = 15
_TOKEN_STAMP = 4
_TOKEN_BYTES = _TOKEN_RANDOM + _TOKEN_STAMP
# A token made in private mode (Tor) has one byte more, which says so: the sender
# has to be in private mode too (it reaches the receiver's onion address, which the
# token gives, and never the DHT), and must not use the DHT for a token that is not
# one - its address would be shown to it.
_TOKEN_PRIVATE_FLAG = b"\x01"
# ...and a token made for one friend has the byte too, with this bit (both: 3). Its seed
# needs that friend's key (see _seed_of); the send entry points refuse it without one.
_TOKEN_BOUND_FLAG = b"\x02"
_TOKEN_FLAGS = (1, 2, 3)
_TOKEN_EPOCH = 1767225600           # 2026-01-01T00:00:00Z
TOKEN_TTL_SECONDS = 10 * 60
# The sender only turns a token away when it is clearly out of date: the clocks
# of the two machines are not the same one, and the receiver, which counts the
# time on its own clock, is the one that really enforces TOKEN_TTL_SECONDS.
_TOKEN_SKEW_SECONDS = 120
_DHT_SALT = b""
# How often accept-side and sender-side re-issue their DHT get/put while
# waiting — frequent enough to feel responsive, rare enough not to hammer
# the network while a person is off copy-pasting the token to someone.
_REPOLL_SECONDS = 3.0
# A sender still preparing an offer after this long says so in the mailbox, and
# says it again this often; a receiver that has heard nothing from it for
# HOLD_STALE_SECONDS stops waiting (the sender has gone).
HOLD_AFTER_SECONDS = 45.0
HOLD_EVERY_SECONDS = 30.0
HOLD_STALE_SECONDS = 5 * 60.0
_HOLD_SETTLE_SECONDS = 10.0
# While a download runs, the sender's mailbox is read this often for a "cancel";
# a sender's cancel notice is given this long to be stored before its session goes.
_CANCEL_POLL_SECONDS = 5.0
_CANCEL_SETTLE_SECONDS = 10.0
# Private mode: a "cancel" is written to the stream and the stream closed this much later.
_PRIVATE_CANCEL_LINGER_SECONDS = 3.0


class P2pError(Exception):
    pass


class P2pProxyError(P2pError):
    """The SOCKS5 proxy is switched on in Settings but cannot be used as set: no
    session is started - never one that goes direct instead (core.p2p.proxy)."""


class P2pTorError(P2pError):
    """Private mode (Tor) and the other side do not match, or Tor cannot be used:
    nothing is started - never a transfer that shows the address instead.
    ``reason`` is a short code: "unavailable" (there is no Tor program), "needs_private"
    (the token is a private-mode one and private mode is off here), "not_private"
    (private mode is on here and the token is a plain one)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class P2pBoundError(P2pError):
    """A token made for one friend, and no key of theirs to use it with: nothing is
    started - never an offer to the wrong mailbox."""


class P2pRefused(P2pError):
    """A transfer that is not taken because of what it holds (see
    ReceiveSession.name_filter); ``reason`` is a short code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class P2pTokenExpired(P2pError):
    """A token older than TOKEN_TTL_SECONDS: it can no longer be used."""


class P2pCancelled(P2pError):
    """An offer that was being prepared was called off."""


class StreamClosed(P2pError):
    """The download a stream was reading from has ended or been called off."""


class StreamTimeout(P2pError):
    """The pieces a stream was waiting for did not come."""


class StreamError(P2pError):
    """What a stream read did not match what the torrent says it should be."""


def format_rate(bytes_per_s: float) -> tuple:
    """(``"12.4 MB/s"``, ``"99.2 Mbps"``) for one transfer rate — the first
    is what is shown, the second is its tooltip (megabits, as ISPs quote).
    Both are decimal, so the tooltip is always exactly 8x the label."""
    bps = max(0.0, float(bytes_per_s or 0))
    return (f"{bps / 1_000_000:.1f} MB/s",
            f"{bps * 8 / 1_000_000:.1f} Mbps")


# A DHT mailbox item is capped at 1000 bytes in total, so the free-text
# fields that ride in it are trimmed rather than trusted to stay short.
_MAX_LABEL_CHARS = 80


def _trim_label(text: str, limit: int) -> str:
    """*text* cut to *limit* characters, keeping a file extension intact so a
    trimmed "…_2230.zip" is still a zip."""
    if len(text) <= limit:
        return text
    suffix = Path(text).suffix
    if suffix and len(suffix) <= 8 and len(suffix) < limit:
        return text[:limit - len(suffix)] + suffix
    return text[:limit]


def _safe_filename(name: str, fallback: str = "save.zip") -> str:
    """Strip any path components from a peer-supplied filename.

    *name* arrives verbatim from the other side's DHT-delivered offer —
    nothing stops a malicious or buggy peer from sending something like
    "../../../../secrets.dat". Keeping only the basename means finalize()'s
    transfer_dir / filename can never resolve outside transfer_dir.
    """
    base = Path(str(name or "")).name
    if not base or base in (".", ".."):
        return fallback
    return base


def _require_libtorrent():
    try:
        import libtorrent as lt
        return lt
    except ImportError as e:
        raise P2pError(
            "P2P transfer needs the 'libtorrent' package, which is not "
            "installed") from e


def _expanded_keys(seed32: bytes) -> tuple:
    """(public_key 32B, private_key 64B) in the exact byte layout
    libtorrent's DHT signing expects — see core.p2p.secret.signing_keys."""
    return _secret.signing_keys(seed32)


def generate_token(now: Optional[float] = None, private: bool = False,
                   bind_key: Optional[bytes] = None) -> tuple:
    """(token_str, seed32) for a fresh, single-use transfer address.
    token_str is what gets handed to the other side; seed32 is what THIS
    side keeps to derive its own keys from. It is good for
    TOKEN_TTL_SECONDS from *now* (see token_created_at). *private*: for private
    mode (Tor), see _TOKEN_PRIVATE_FLAG. *bind_key*: made for one friend, whose key
    this is (see _TOKEN_BOUND_FLAG)."""
    stamp = max(0, int((time.time() if now is None else now) - _TOKEN_EPOCH))
    token_bytes = os.urandom(_TOKEN_RANDOM) + stamp.to_bytes(_TOKEN_STAMP, "big")
    flags = (1 if private else 0) | (2 if bind_key else 0)
    if flags:
        token_bytes += bytes([flags])
    token_str = b32encode(token_bytes).decode("ascii").rstrip("=")
    return token_str, _seed_of(token_bytes, bind_key)


def _flags(token_bytes: bytes) -> int:
    return token_bytes[_TOKEN_BYTES] if len(token_bytes) > _TOKEN_BYTES else 0


def _seed_of(token_bytes: bytes, bind_key: Optional[bytes]) -> bytes:
    """The seed every key of a transfer comes from - the one place it is made, for both
    ends. A plain or private token: the hash of the token, as it has always been. One made
    for a friend: that, mixed with the friend's key, so that the token alone is of no use.
    P2pBoundError for a bound token without the key."""
    seed = hashlib.sha256(token_bytes).digest()
    if not _flags(token_bytes) & 2:
        return seed
    if not bind_key:
        raise P2pBoundError("this token was made for one friend")
    return _secret.derive(seed + bytes(bind_key), b"bound seed", 32)


def _token_bytes(token_str: str) -> bytes:
    cleaned = "".join((token_str or "").split()).upper()
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        token_bytes = b32decode(padded)
    except Exception as e:
        raise P2pError("that token is not valid") from e
    if len(token_bytes) not in (_TOKEN_BYTES, _TOKEN_BYTES + 1):
        raise P2pError("that token is not valid")
    if len(token_bytes) > _TOKEN_BYTES and token_bytes[_TOKEN_BYTES] not in _TOKEN_FLAGS:
        raise P2pError("that token is not valid")
    return token_bytes


def token_is_private(token_str: str) -> bool:
    """Is this a private-mode (Tor) token? P2pError if it is not a token."""
    return bool(_flags(_token_bytes(token_str)) & 1)


def token_is_bound(token_str: str) -> bool:
    """Was this token made for one friend (it cannot be used without that friend's
    key)? P2pError if it is not a token."""
    return bool(_flags(_token_bytes(token_str)) & 2)


def token_created_at(token_str: str) -> float:
    """When the token was made, in unix seconds (as its maker's clock had it).
    P2pError if it is not a token."""
    stamp = int.from_bytes(_token_bytes(token_str)[_TOKEN_RANDOM:_TOKEN_BYTES], "big")
    return float(_TOKEN_EPOCH + stamp)


def seed_from_token(token_str: str, *, check_age: bool = True) -> bytes:
    """The seed the token's keys come from. P2pError when it is not a token;
    P2pTokenExpired (a P2pError) when it is clearly older than its 10 minutes
    - unless *check_age* is False, for a caller that only wants to know it is
    well formed."""
    token_bytes = _token_bytes(token_str)
    if check_age:
        age = time.time() - token_created_at(token_str)
        if age > TOKEN_TTL_SECONDS + _TOKEN_SKEW_SECONDS:
            raise P2pTokenExpired("that token has expired")
    return hashlib.sha256(token_bytes).digest()


def session_seed(token_str: str, bind_key: Optional[bytes] = None, *, check_age: bool = True) -> bytes:
    """The seed a transfer under this token is made from (seed_from_token only says the
    token is one, and not run out - for a token made for a friend its answer is not the
    seed to use). *bind_key* is the key of the friend it was made for; a plain token
    ignores it. P2pBoundError for a bound token without one."""
    seed_from_token(token_str, check_age=check_age)
    return _seed_of(_token_bytes(token_str), bind_key)

def net_settings() -> dict:
    """Listening port and router port-mapping, from Settings (P2P section).

    Port 0 - the default - is a free one chosen each time: this is a one-off
    transfer session, not a long-lived seedbox with a port worth remembering.
    A fixed port is for people who forward it by hand or whose firewall only
    lets one through; if it is taken (a second session of ours, another
    program) libtorrent tries the next ports up. UPnP and NAT-PMP are the two
    ways a router is asked to open the port by itself."""
    port, upnp = 0, True
    try:
        from core.config_manager import get_config
        cfg = get_config()
        chosen = cfg.get("p2p_port", 0)
        if isinstance(chosen, int) and not isinstance(chosen, bool) and 1024 <= chosen <= 65535:
            port = chosen
        upnp = bool(cfg.get("p2p_upnp", True))
    except Exception:
        pass
    return {
        "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
        "enable_upnp": upnp,
        "enable_natpmp": upnp,
    }


def tor_enabled() -> bool:
    """Is private mode (Tor) switched on in Settings (core.p2p.tor)? It wins over the
    SOCKS5 proxy and over TLS: nothing leaves this PC but through Tor, and Tor is
    already end to end."""
    return _tor.enabled()


def tls_enabled() -> bool:
    """Is TLS for transfers switched on in Settings (core.p2p.tls)? Never behind the
    SOCKS5 proxy: TLS needs sockets that listen, and a proxy that is switched on
    (set or not) wins - hiding the address was asked for as well; nor in private
    mode (Tor). Whether this libtorrent can do it is another matter: a session asked
    for TLS that cannot have it is refused (P2pError), never quietly made without."""
    cfg = _proxy._config()
    if not cfg.get("p2p_tls", False):
        return False
    return not cfg.get("p2p_proxy", False) and not tor_enabled()


def _session_settings() -> dict:
    """What every transfer session is started with.

    The DHT is only used for the sealed mailbox and the rendezvous (see
    _Rendezvous) - never to announce the torrent itself - and BitTorrent's
    protocol encryption is mandatory in both directions, RC4 only: a peer that
    cannot do it is refused rather than talked to in the clear. Local peer
    discovery is off (it would shout the torrent on the LAN)."""
    lt = _require_libtorrent()
    if tor_enabled():
        return _tor_session_settings(lt)
    settings = {
        "enable_dht": True,
        "enable_lsd": False,
        **net_settings(),
        "out_enc_policy": int(lt.enc_policy.pe_forced),
        "in_enc_policy": int(lt.enc_policy.pe_forced),
        "allowed_enc_level": int(lt.enc_level.pe_rc4),
        "prefer_rc4": True,
        # Received data goes through the operating system's disk cache. The
        # default writes every piece straight through to the disk, which on a
        # fast line is what limited a transfer (measured on loopback: 90-150
        # MB/s against 220 with the cache on). A power cut would lose what had
        # not been flushed yet, but nothing is resumed anyway - a transfer cut
        # short is started again - and a piece only counts once it is checked.
        "disk_io_write_mode": int(lt.io_buffer_mode_t.enable_os_cache),
        # There is exactly one peer worth having, and a connection to it is
        # dropped by design while the files are still being placed (nothing is
        # wanted yet, so both sides are "done"): its default 60 s wait before
        # trying that peer again would stall every transfer for a minute.
        "min_reconnect_time": 1,
        "alert_mask": (lt.alert.category_t.error_notification
                      | lt.alert.category_t.status_notification
                      | lt.alert.category_t.storage_notification
                      | lt.alert.category_t.dht_notification
                      | lt.alert.category_t.dht_operation_notification),
    }
    try:
        px = _proxy.configured()
    except _proxy.ProxyError as e:
        # (switched on but not usable: no session at all, not a direct one)
        raise P2pProxyError(str(e)) from e
    if px is not None:
        settings.update(_proxy.lt_settings(lt, px))
    else:
        # A connection that does not come up in this long is given up on (the
        # default is 15 s): a firewall that drops a TCP attempt must not hold up
        # the uTP one that follows it (see _Rendezvous.TCP_FIRST_SECONDS). Behind a
        # proxy the default is kept - its UDP channel answers more slowly, and a
        # uTP attempt cut short would fall back to TCP through the proxy.
        settings["peer_connect_timeout"] = 5
    return settings


def _tor_session_settings(lt) -> dict:
    """What a private-mode session is started with (core.p2p.tor): libtorrent talks
    to 127.0.0.1 and nothing else - it listens there, its connections go out from
    there (so one that would reach the internet cannot be made at all), and there is
    no DHT, no router port-mapping, no local discovery and no uTP. The two PCs meet
    through Tor's loopback bridges (core.p2p.tor_link).

    A connection takes its time to come up (a Tor rendezvous is 3-60 s) and then
    moves at a few hundred kilobytes a second, so libtorrent's patience is raised:
    its default 10 s for the handshake would drop nearly every one."""
    return {
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "enable_outgoing_utp": False,
        "enable_incoming_utp": False,
        "enable_ip_notifier": False,
        "listen_interfaces": "127.0.0.1:0",
        "outgoing_interfaces": "127.0.0.1",
        # (the several bridges of one transfer are all "127.0.0.1")
        "allow_multiple_connections_per_ip": True,
        # No BitTorrent encryption: the connection is inside Tor, end to end, and only
        # whoever knows the token can reach it at all. Plain is what lets the bridges
        # give each connection a peer id of its own (core.p2p.tor_link.new_peer_id) -
        # without that, libtorrent closes all but one of a peer's connections.
        "out_enc_policy": int(lt.enc_policy.pe_disabled),
        "in_enc_policy": int(lt.enc_policy.pe_disabled),
        "disk_io_write_mode": int(lt.io_buffer_mode_t.enable_os_cache),
        "min_reconnect_time": 1,
        "peer_connect_timeout": 120,
        "handshake_timeout": 120,
        "request_timeout": 90,
        "peer_timeout": 240,
        "alert_mask": (lt.alert.category_t.error_notification
                      | lt.alert.category_t.status_notification
                      | lt.alert.category_t.storage_notification),
    }


# A session behind a proxy starts its DHT at once, but the proxy's UDP channel is
# only up a moment later, and what the DHT sent before that is lost (libtorrent
# would find its way in the end - after a minute). So the DHT is started again
# once the channel is up.
_PROXY_DHT_RESTART_SECONDS = 3.5


def _tor_role_settings(role: str) -> dict:
    """The one direction a private-mode session works in. A peer tells the other on
    which port it listens, and libtorrent remembers it and may connect there later -
    in private mode that "peer" is a bridge on this very PC, so the port would be one
    of THIS PC's, picked by the other side. So the receiver, which is only ever
    connected to (the host hands it the sender's connection), never connects out; and
    the sender, which only connects (to its own bridges, and learns nothing from an
    incoming connection since it has none), takes none."""
    return {"enable_outgoing_tcp": False} if role == "receive" else {"enable_incoming_tcp": False}


# The DHT's starting servers, as libtorrent has them unless a test says otherwise.
_DHT_BOOTSTRAP = ""
# Behind a proxy a session's DHT is switched on once its starting servers have been looked up over the
# proxy (see _start_dht_behind_proxy): a put made before is lost. session id -> the event that says so.
_PROXY_DHT_READY: dict = {}
# ... and what each of them was given: session id -> {"names": the starting servers by name, "nodes": the
# addresses found ("" - none), "busy": a try is under way, "wait": the longest the first start can take}.
_PROXY_DHT_INFO: dict = {}
_PROXY_READY_LOCK = threading.Lock()        # (sessions are made on the GUI thread and on the sending threads)
_BOOTSTRAP_TRIES = 4
# With this PC's DNS to fall back on (Settings), fewer: the fallback is the cheaper way out, and each try of
# a proxy that carries no lookups costs a whole timeout. (And a proxy that has just failed a whole round: one.)
_LOCAL_DNS_TRIES = 2
_BOOTSTRAP_RETRY_SECONDS = 5.0


def _new_session(tls: bool = False, role: str = ""):
    """A libtorrent session. *tls*: with an SSL socket beside each one that listens
    (the receiver of a TLS transfer is reached there; see core.p2p.tls). *role*
    ("send" / "receive"): in private mode, which direction it works in."""
    lt = _require_libtorrent()
    settings = _session_settings()
    if role and tor_enabled():
        settings.update(_tor_role_settings(role))
    if tls and not settings.get("force_proxy"):
        if not _tls.available(lt):
            raise P2pError("TLS is switched on in Settings, but this libtorrent cannot do it")
        settings["listen_interfaces"] = _tls.with_ssl_sockets(settings["listen_interfaces"])
    behind_proxy = bool(settings.get("force_proxy"))
    names = _DHT_BOOTSTRAP or lt.default_settings().get("dht_bootstrap_nodes", "")
    if behind_proxy:
        # (no name for libtorrent to look up on this PC: the addresses come from the thread below)
        settings["dht_bootstrap_nodes"] = ""
    ses = lt.session(settings)
    if behind_proxy:
        ready = threading.Event()
        try:
            px = _proxy.configured()
        except _proxy.ProxyError:
            px = None
        count = len([e for e in names.split(",") if e.strip()])
        wait = (_bootstrap_tries(px) * (_BOOTSTRAP_RETRY_SECONDS + _proxy.lookup_budget(count))
                + _PROXY_DHT_RESTART_SECONDS + 2.0)
        with _PROXY_READY_LOCK:
            for key in [k for k, ev in _PROXY_DHT_READY.items() if ev.is_set()][:-8]:
                _PROXY_DHT_READY.pop(key, None)
                _PROXY_DHT_INFO.pop(key, None)
            _PROXY_DHT_READY[id(ses)] = ready
            _PROXY_DHT_INFO[id(ses)] = {"names": names, "nodes": "", "busy": True, "wait": wait}
        threading.Thread(target=_start_dht_behind_proxy, args=(ses, px, names, ready),
                         name="p2p-proxy-dht", daemon=True).start()
    return ses


def proxy_dht_ready(ses):
    """The event that says a session started behind a proxy has its DHT up (see _start_dht_behind_proxy),
    or None for a session that is not waiting for one."""
    with _PROXY_READY_LOCK:
        return _PROXY_DHT_READY.get(id(ses))


def proxy_dht_state(ses) -> str:
    """"" for a session that is not behind a proxy, "starting" until its DHT has been started (see
    _start_dht_behind_proxy), "busy" while the starting servers are looked for again, "ok" once the DHT
    has some, "none" if it runs without any - the proxy would not carry the lookup (retry_proxy_dht)."""
    with _PROXY_READY_LOCK:
        info, ready = _PROXY_DHT_INFO.get(id(ses)), _PROXY_DHT_READY.get(id(ses))
        if info is None or ready is None:
            return ""
        if not ready.is_set():
            return "starting"
        return "busy" if info["busy"] else ("ok" if info["nodes"] else "none")


def retry_proxy_dht(ses) -> bool:
    """A session whose DHT was started with no starting servers (proxy_dht_state "none") looks for them
    again, on a thread of its own that ends with the try; the DHT is started over when they are found. For
    a session that lives on (the friends'): a transfer's is made new each time. False when it is not one
    that needs it, or a try is under way already."""
    with _PROXY_READY_LOCK:
        info = _PROXY_DHT_INFO.get(id(ses))
        ready = _PROXY_DHT_READY.get(id(ses))
        if info is None or ready is None or not ready.is_set() or info["busy"] or info["nodes"]:
            return False
        info["busy"] = True
    try:
        px = _proxy.configured()
    except _proxy.ProxyError:
        px = None
    threading.Thread(target=_retry_dht_behind_proxy, args=(ses, px, info),
                     name="p2p-proxy-dht-again", daemon=True).start()
    return True


def _retry_dht_behind_proxy(ses, px, info: dict) -> None:
    nodes = ""
    try:
        if px is not None:
            nodes = _proxy.bootstrap_nodes(px, info["names"])
        if nodes:
            ses.apply_settings({"enable_dht": False})
            ses.apply_settings({"dht_bootstrap_nodes": nodes, "enable_dht": True})
            logger.info("P2P: the proxy carries the lookup of the DHT's starting servers now; the DHT is started")
    except Exception:
        nodes = ""
        logger.debug("P2P: starting the DHT behind the proxy again failed", exc_info=True)
    finally:
        with _PROXY_READY_LOCK:
            info["nodes"] = nodes or info["nodes"]
            info["busy"] = False


def _wait_for_proxy_dht(ses, started: float) -> None:
    """Behind a proxy, wait until the DHT has been started again (see
    _new_session) before the first item is put: one put earlier is lost."""
    try:
        if not ses.get_settings().get("force_proxy"):
            return
    except Exception:
        return
    ready = proxy_dht_ready(ses)
    if ready is not None:
        with _PROXY_READY_LOCK:
            info = _PROXY_DHT_INFO.get(id(ses)) or {}
        ready.wait(timeout=info.get("wait") or _BOOTSTRAP_TRIES * (_BOOTSTRAP_RETRY_SECONDS + 4.0))
    wait = started + _PROXY_DHT_RESTART_SECONDS + 1.0 - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _bootstrap_tries(px) -> int:
    """How many times the lookup of the starting servers over *px* is tried before the DHT is started
    without them - or, if Settings let this PC's DNS stand in, before that does it. A proxy that is not
    there to answer is not tried again (the same goes for one that has just failed a whole round, when
    this PC's DNS is the way out)."""
    if px is None:
        return 1
    if not px.local_dns:
        return _BOOTSTRAP_TRIES
    return 1 if _proxy.gave_up(px) else _LOCAL_DNS_TRIES


def _start_dht_behind_proxy(ses, px, names: str, ready) -> None:
    """Behind a proxy: look the DHT's starting servers up OVER THE PROXY (see core.p2p.proxy - libtorrent
    would use this PC's own DNS), give libtorrent their addresses, and start its DHT again - the proxy's
    UDP channel takes a moment to come up, and a DHT started before it is dead. If the proxy will not carry
    a lookup the DHT is left without starting servers: P2P does not work then, and nothing is looked up
    on this PC instead (retry_proxy_dht looks again, for a session that lives on) - unless Settings let
    this PC's DNS do it, after a few tries so that one passing glitch does not tell it."""
    begun = time.monotonic()
    nodes = ""
    try:
        tries = _bootstrap_tries(px)
        for attempt in range(tries):
            nodes = _proxy.bootstrap_nodes(px, names) if px is not None else ""
            if nodes:
                break
            if attempt + 1 < tries:
                time.sleep(_BOOTSTRAP_RETRY_SECONDS)
        if not nodes and px is not None:
            _proxy.note_gave_up(px)
            if px.local_dns:
                nodes = names                   # (the names: libtorrent looks them up with this PC's own DNS)
                logger.info("P2P: the proxy would not carry a lookup of the DHT's starting servers; "
                            "this PC's DNS looks them up, as Settings allow")
            else:
                logger.warning("P2P: the proxy would not carry a lookup of the DHT's starting servers; "
                               "P2P cannot start behind it (nothing is looked up on this PC instead)")
        pause = _PROXY_DHT_RESTART_SECONDS - (time.monotonic() - begun)
        if pause > 0:
            time.sleep(pause)
        ses.apply_settings({"enable_dht": False})
        ses.apply_settings({"dht_bootstrap_nodes": nodes, "enable_dht": True})
    except Exception:
        logger.debug("P2P: starting the DHT behind the proxy failed", exc_info=True)
    finally:
        with _PROXY_READY_LOCK:
            info = _PROXY_DHT_INFO.get(id(ses))
            if info is not None:
                info["nodes"], info["busy"] = nodes, False
        ready.set()


def _hidden_flags(lt) -> int:
    """Torrent flags that keep the torrent off every public channel: its
    info-hash is secret (it is only ever in the sealed offer), and a stranger
    who knew it could fetch the file list and the data."""
    f = lt.torrent_flags
    return f.disable_dht | f.disable_lsd | f.disable_pex


class _Rendezvous:
    """How the two sides find each other when the torrent is announced nowhere.

    Both announce their address under a hash derived from the token (see
    core.p2p.secret.rendezvous_hash) and look that hash up; whatever address
    the other side left there is connected to. Nothing ties the hash to the
    torrent, so the DHT learns that two machines met at it, not what they
    exchange. Announcing is repeated (the first one can come before the DHT is
    reachable); looking up goes on only while there is nobody to talk to.

    There are two meeting points per token: one for a plain transfer (the port the
    DHT itself uses, whichever port that turns out to be on the other side of a
    router) and one for TLS (the SSL port, named outright: see core.p2p.tls). A
    PC announces the ones it can be reached at and looks up the one for the kind
    of transfer it is in - so a TLS sender is never sent to a plain port, nor the
    other way round."""
    ANNOUNCE_EVERY = 20.0
    # The first announce is made the moment the session starts, before its DHT
    # has a single node to give it to, and is lost; waiting a whole ANNOUNCE_EVERY
    # for the next one added up to twenty seconds before the two sides could find
    # each other. So the first few follow quickly.
    ANNOUNCE_EARLY = (2.0, 3.0, 5.0, 8.0)
    LOOKUP_EVERY = 5.0
    RETRY_EVERY = 5.0
    # A TCP connection runs at the speed of the line; uTP, the fallback that gets
    # through a router with no open port, is about a fifth of that on a fast one
    # (measured on this PC: 47 MB/s against 220). So the connection is tried over
    # TCP first, and over uTP once that has had this long and nobody is connected.
    TCP_FIRST_SECONDS = 6.0
    _IMPLIED_PORT = 2          # let the DHT record the port it saw us on (NAT)
    _RECENT_SECONDS = 90.0

    def __init__(self, ses, seed32: bytes, announce=("plain",), lookup: str = "plain"):
        lt = _require_libtorrent()
        self._ses = ses
        self._hashes = {"plain": lt.sha1_hash(_secret.rendezvous_hash(seed32)),
                        "tls": lt.sha1_hash(_secret.rendezvous_hash(seed32, b"rendezvous tls"))}
        self._hexes = {k: str(v) for k, v in self._hashes.items()}
        self._hash, self._hex = self._hashes["plain"], self._hexes["plain"]
        self._announce = tuple(announce)
        self.lookup = lookup            # which meeting point to look the other side up at
        self._announced_at = self._lookup_at = -1e9
        self._announces = 0
        self._tried: dict = {}
        self._recent: dict = {}         # address -> when it was handed out
        self._own_ips: set = set()
        # Behind a proxy nothing listens, and the address the DHT sees is the
        # proxy's: that is what the other side is to reach for (see
        # core.p2p.proxy), whatever port is named here.
        try:
            self._proxied = bool(ses.get_settings().get("force_proxy"))
        except Exception:
            self._proxied = False
        # (behind a proxy uTP over its UDP channel is the only way there is)
        self._tcp_first = not self._proxied
        self._first_endpoint_at = 0.0
        if self._tcp_first:
            self._outgoing_utp(False)

    def _outgoing_utp(self, on: bool) -> None:
        try:
            self._ses.apply_settings({"enable_outgoing_utp": on})
        except Exception:
            logger.debug("P2P: changing the connection order failed", exc_info=True)

    def tick(self, now: float, lookup: bool) -> None:
        if (self._tcp_first and lookup and self._first_endpoint_at
                and now - self._first_endpoint_at >= self.TCP_FIRST_SECONDS):
            # TCP did not get through (a router with no open port, a firewall):
            # uTP, which is what traverses a router, from here on.
            self._tcp_first = False
            self._outgoing_utp(True)
            self._tried.clear()
        try:
            gap = (self.ANNOUNCE_EARLY[self._announces] if self._announces < len(self.ANNOUNCE_EARLY)
                   else self.ANNOUNCE_EVERY)
            if now - self._announced_at >= gap:
                sent = False
                for mode in self._announce:
                    if mode == "tls":
                        port, flags = self._ses.ssl_listen_port(), 0        # (named outright)
                    else:
                        port, flags = self._ses.listen_port() or (1 if self._proxied else 0), self._IMPLIED_PORT
                    if port:
                        self._ses.dht_announce(self._hashes[mode], port, flags)
                        sent = True
                if sent:
                    self._announced_at = now
                    self._announces += 1
            if lookup and now - self._lookup_at >= self.LOOKUP_EVERY:
                self._lookup_at = now
                self._ses.dht_get_peers(self._hashes[self.lookup])
        except Exception:
            logger.debug("P2P: rendezvous call failed", exc_info=True)

    def endpoints(self, alert, now: float, connectable: bool = True) -> list:
        """The addresses worth connecting to that *alert* brings (if it is a
        lookup result for our hash), each at most once per RETRY_EVERY. With
        nothing to connect them to yet (*connectable* False: the offer is not
        accepted) none are handed out - or counted as tried."""
        name = type(alert).__name__
        if name == "external_ip_alert":
            self._own_ips.add(str(getattr(alert, "external_address", "")))
            return []
        if (not connectable or name != "dht_get_peers_reply_alert"
                or str(alert.info_hash) != self._hexes[self.lookup]):
            return []
        out = []
        for ip, port in alert.peers():
            # (behind a proxy "our" address is the proxy's, which two friends on
            # the same proxy share: the other side's would be skipped as ours)
            if ip in self._own_ips and not self._proxied:
                continue
            if now - self._tried.get((ip, port), -1e9) >= self.RETRY_EVERY:
                self._tried[(ip, port)] = now
                self._recent[(ip, port)] = now
                out.append((ip, port))
        if out and not self._first_endpoint_at:
            self._first_endpoint_at = now
        return out

    def recent(self, now: float) -> list:
        """Addresses handed out lately (a torrent added afterwards - the second
        step of a TLS transfer - can be pointed at them at once)."""
        return [ep for ep, at in self._recent.items() if now - at <= self._RECENT_SECONDS]


_MANIFEST_NAME = "manifest.json"
# The one file of the small first torrent of a TLS transfer (the real torrent's
# description), and the most such a description may be: it is what a friend sends,
# so it is bounded like everything else from a friend.
_META_NAME = "m"
_MAX_META_BYTES = 64 * 1024 * 1024
# What may go into a DHT mailbox item before it is sealed: the item is 1000
# bytes at most (bencoded), and sealing adds _secret.OVERHEAD to the message.
_MAX_OFFER_BYTES = 940

# Where a "file" offer is in being put in place (see ReceiveSession._remap).
_REMAP_NONE, _REMAP_WAIT, _REMAP_RENAMING, _REMAP_DONE = range(4)
# How long to wait for libtorrent to confirm every file was given its place.
_RENAME_TIMEOUT = 30.0
# Priority of a file that is to be downloaded (libtorrent's default).
_WANTED = 4


def plan_targets(dest, rels) -> list:
    """The place under *dest* for each of the sender's relative paths *rels*.

    The sender's layout is kept ("sub/a.txt" goes to dest/sub/a.txt), but a
    received file never lands on one that is already there: a name taken keeps
    both, the new one becoming "a (2).txt" — the same rule a move follows.
    Folders that already exist are simply used. A name that is a FILE where a
    folder is needed, or two names that differ only by case on a case-blind
    disk, are told apart the same way. Returns the targets as relative POSIX
    paths, in the order of *rels*. Checked against what is on disk NOW, so it
    has to run before anything is written."""
    dest = Path(dest)
    taken: set = set()      # casefolded relative targets already planned
    dir_names: dict = {}    # the sender's folder (casefolded) -> the name used
    out = []
    for rel in rels:
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise P2pError(f"invalid name in the transfer: {rel}")
        cur = dest
        fixed: list = []
        for k, part in enumerate(parts[:-1]):
            key = tuple(x.casefold() for x in parts[:k + 1])
            name = dir_names.get(key)
            if name is None:
                name, n = part, 2
                while True:
                    path = cur / name
                    taken_as_file = tuple(x.casefold() for x in fixed + [name]) in taken
                    if (path.exists() and not path.is_dir()) or taken_as_file:
                        name = f"{part} ({n})"
                        n += 1
                    else:
                        break
                dir_names[key] = name
            fixed.append(name)
            cur = cur / name
        leaf = parts[-1]
        stem, suffix = os.path.splitext(leaf)
        name, n = leaf, 2
        while (cur / name).exists() or tuple(x.casefold() for x in fixed + [name]) in taken:
            name = f"{stem} ({n}){suffix}"
            n += 1
        taken.add(tuple(x.casefold() for x in fixed + [name]))
        out.append("/".join(fixed + [name]))
    return out


@dataclass
class TransferOffer:
    """What the receiver can know BEFORE downloading anything — see the
    module docstring for why this is deliberately the small half."""
    magnet: str
    filename: str
    size: int
    game_name: str
    sender_username: str
    sender_machine_id: str
    # The sender's backup folder for the game. Defaulted: an older SaveSync
    # build does not send it.
    folder: str = ""
    # "backup" (a SaveSync backup, filed as an archive) or "file" (anything
    # the sender picked by hand: one or more files, sent as they are and
    # never indexed — downloaded straight into the receiver's chosen folder).
    kind: str = "backup"
    # How many files a "file" offer holds.
    count: int = 0
    # The chat attachment offer this transfer answers (its message id), or "".
    attach: str = ""
    # A TLS transfer (core.p2p.tls) has no magnet link - a magnet link cannot start
    # an SSL torrent. In its place: the description of a small first torrent (the
    # bencoded info dictionary without the authority's certificate, which the
    # receiver puts back itself), and the info-hash the real torrent must have
    # once the first one has delivered its description.
    tls: bool = False
    tls_meta: bytes = b""
    tls_hash: str = ""


class TorrentStream:
    """Read access, for a player, to the ONE file of a download that is still
    arriving.

    ``read(offset, length)`` returns those bytes of the file once they are in:
    the pieces that cover them are asked for first (and a few after them, so the
    player stays ahead), the call waits for them, and what is read back from the
    disk is checked against the torrent's own hash for each piece before a byte
    of it is handed out - so a player sees exactly the bytes the finished file
    will have, never a half-written piece. Once the whole file is in, none of that
    is needed any more and it is just read. Everything the stream needs from
    libtorrent comes in as small callables (``have``, ``want``, ``expected``,
    ``done``, ``alive``), so it can be tested without a network. Reads may come
    from any thread."""
    READ_AHEAD = 8
    MAX_READ = 256 * 1024
    PATIENCE = 120.0                    # seconds with no new piece before giving up
    _VERIFY_TRIES = 30                  # a piece just written may not be on disk yet
    _VERIFY_PAUSE = 0.1

    def __init__(self, *, name: str, path, size: int, piece_length: int, have, want, expected, done, alive,
                 begin=None):
        self.name = name
        self.path = Path(path)
        self.size = int(size)
        self.piece_length = max(1, int(piece_length))
        self._have, self._want, self._expected = have, want, expected
        self._done, self._alive = done, alive
        # Called once, the first time anything is actually read (not by looking
        # at ready()): switches the download to fetching in order.
        self._begin, self._begun = begin, False
        self._have_pieces: set = set()
        self._verified: set = set()
        self._fh = None
        self._closed = False
        self._lock = threading.Lock()
        self._asked_at: dict = {}

    @property
    def pieces(self) -> int:
        return (self.size + self.piece_length - 1) // self.piece_length

    def _is_alive(self, stop) -> None:
        if self._closed or not self._alive() or (stop is not None and stop()):
            raise StreamClosed("the stream is closed")

    def _is_have(self, piece: int) -> bool:
        if piece in self._have_pieces or self._done():
            return True
        try:
            if self._have(piece):
                self._have_pieces.add(piece)
                return True
        except Exception:
            # The torrent is gone (let go of after it finished): that is only
            # fine when it did finish.
            if self._done():
                return True
        return False

    def ready(self) -> bool:
        """Is the start of the file in (enough to see what it is)? While it is
        not, the first piece is asked for (every couple of seconds): a download
        that picks its pieces in any order would otherwise bring it last, and
        "watch while downloading" would only turn up at the very end."""
        if self.size == 0 or self._is_have(0):
            return True
        now = time.monotonic()
        if now - self._asked_at.get(0, -1e9) >= 2.0:
            self._asked_at[0] = now
            try:
                self._want(0, 0)
            except Exception:
                logger.debug("P2P stream: asking for the first piece failed", exc_info=True)
        return False

    def _ask(self, piece: int) -> None:
        """Put the piece wanted now at the front of the queue, and the next few
        behind it. Repeated every couple of seconds in case the first ask was lost."""
        now = time.monotonic()
        if now - self._asked_at.get(piece, -1e9) < 2.0:
            return
        self._asked_at[piece] = now
        try:
            self._want(piece, 0)
            for k in range(1, self.READ_AHEAD + 1):
                if piece + k < self.pieces:
                    self._want(piece + k, 250 * k)
        except Exception:
            logger.debug("P2P stream: asking for a piece failed", exc_info=True)

    def read(self, offset: int, length: int, stop=None) -> bytes:
        """Up to *length* bytes from *offset* (fewer at most MAX_READ, or at the
        end of the file; b"" at the end). Blocks until they have arrived. Raises
        StreamClosed (called off, or *stop()* said so), StreamTimeout, StreamError."""
        offset, length = int(offset), int(length)
        if offset < 0 or length < 0:
            raise StreamError("bad range")
        if offset >= self.size or length == 0:
            return b""
        if not self._begun:
            self._begun = True
            if self._begin is not None:
                try:
                    self._begin()
                except Exception:
                    logger.debug("P2P stream: switching to in-order download failed", exc_info=True)
        length = min(length, self.size - offset, self.MAX_READ)
        first, last = offset // self.piece_length, (offset + length - 1) // self.piece_length
        give_up = time.monotonic() + self.PATIENCE
        for piece in range(first, last + 1):
            while not self._is_have(piece):
                self._is_alive(stop)
                self._ask(piece)
                if time.monotonic() > give_up:
                    raise StreamTimeout("the pieces did not arrive")
                time.sleep(0.05)
            give_up = time.monotonic() + self.PATIENCE
        self._is_alive(stop)
        return self._read_file(offset, length, first, last)

    def _pread(self, offset: int, length: int) -> bytes:
        if self._fh is None:
            if self._closed:
                raise StreamClosed("the stream is closed")
            try:
                self._fh = open(self.path, "rb", buffering=0)
            except OSError as e:
                raise StreamError(f"cannot read the file: {e}") from e
        self._fh.seek(offset)
        return self._fh.read(length)

    def _read_file(self, offset: int, length: int, first: int, last: int) -> bytes:
        with self._lock:
            if not self._done():
                for piece in range(first, last + 1):
                    if piece not in self._verified:
                        self._verify(piece)
            return self._pread(offset, length)

    def _verify(self, piece: int) -> None:
        start = piece * self.piece_length
        want = self._expected(piece)
        if want is None:                # (a torrent that has no hash to check against)
            self._verified.add(piece)
            return
        for attempt in range(self._VERIFY_TRIES):
            data = self._pread(start, min(self.piece_length, self.size - start))
            if hashlib.sha1(data).digest() == want:
                self._verified.add(piece)
                return
            if self._closed:
                raise StreamClosed("the stream is closed")
            time.sleep(self._VERIFY_PAUSE)
        raise StreamError("a piece on disk does not match the torrent")

    def release(self) -> None:
        """Let go of the file (whoever was reading has finished) without ending
        the stream: a later read opens it again. A file that something holds open
        cannot be removed or moved."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def close(self) -> None:
        """The stream is over (the download ended or was called off): reads fail."""
        self._closed = True
        self.release()


class ReceiveSession:
    """One "waiting to receive" session: a token, a DHT mailbox being
    watched under it, and — once accepted — the download itself.

    Call pump() periodically (a UI timer, roughly once a second) to drive
    both the DHT lookup and the torrent download; nothing here blocks.
    """

    def __init__(self, bind_key: Optional[bytes] = None):
        """*bind_key*: the session is for one friend, whose key this is (see
        _TOKEN_BOUND_FLAG): only that friend's SaveSync can use its token."""
        self.created_at = time.time()
        self.bound = bool(bind_key)
        # Private mode (Tor): the token says so, this session hosts an onion service the
        # token leads to (core.p2p.tor_link.Host) instead of watching a DHT mailbox, and
        # the sender comes to it - nothing of this PC is announced anywhere.
        self._private = tor_enabled()
        self.token, self._seed32 = generate_token(self.created_at, private=self._private,
                                                  bind_key=bind_key)
        self._pub32, _sk64 = _expanded_keys(self._seed32)
        self._host = None
        if self._private:
            svc = _tor.service()
            if not svc.available():
                raise P2pTorError("unavailable")
            self._tls_ses = False
            self._ses = _new_session(role="receive")
            self._rv = None
            svc.start()
            address, key_blob = _tor.identity_from_seed(self._seed32)
            self._host = _link.Host(svc, address, key_blob, bt_port=self._ses.listen_port)
        else:
            # With TLS switched on (and no proxy) this session has SSL sockets as well and
            # leaves its address at both meeting points: which one the sender uses depends
            # on what it sends (see _Rendezvous).
            self._tls_ses = tls_enabled()
            self._ses = _new_session(tls=True) if self._tls_ses else _new_session()
            self._rv = _Rendezvous(self._ses, self._seed32,
                                   announce=("plain", "tls") if self._tls_ses else ("plain",))
        self._last_get_at = 0.0
        self.offer: Optional[TransferOffer] = None
        self._offer_raw: bytes = b""
        self._handle = None
        self._save_dir: Optional[Path] = None
        self.download_error: str = ""
        self.download_done = False
        # libtorrent says "finished" when the last piece is downloaded and
        # checked, NOT when the files are complete on disk: they are written
        # behind it, so reading them at once found files missing or still
        # short. Its "cache flushed" alert is no guarantee either (it comes at
        # once, with the file still filling in for another 0.1-0.2 s). What is
        # one: the torrent being REMOVED from the session - request_release()
        # asks for that, ``released`` says it has happened - after which every
        # byte is in the files and they are closed.
        self.released = False
        self._release_requested = False
        self._root_name = ""
        self._closed = False
        # A "file" offer is downloaded straight to its final place, so nothing
        # may be fetched until every file has been given that place — see
        # _begin_remap. _plan is [(index in the torrent, target path relative
        # to the save folder, size)] once it is known.
        self._remap = _REMAP_NONE
        self._plan: Optional[list] = None
        self._renames_left = 0
        self._remap_started = 0.0
        # Optional check of the file list a "file" offer turns out to hold,
        # ``name_filter(relative paths, sizes) -> reason`` ("" = fine), made
        # before anything is fetched; a reason refuses the whole transfer and
        # is kept in ``refused``.
        self.name_filter = None
        self.refused = ""
        # The sender has said, inside the token's time, that it is still
        # preparing the offer (see _Hold), and when it last said so.
        self.held = False
        self.held_at = 0.0
        # The sender called the transfer off (a sealed "cancel" in the mailbox).
        self.sender_cancelled = False
        self._notice_raw = b""
        # request_cancel(): this download is being given up; what it had only
        # partly written (``_doomed``) goes once the torrent has been let go of.
        self._cancelled = False
        self._kept: list = []
        self._doomed: list = []
        self._stream: Optional[TorrentStream] = None
        # Folders (relative to the save folder) that this transfer creates, as
        # opposed to ones that were already there — so that moving the files
        # away later can tidy up exactly those and nothing of the person's.
        self.new_dirs: list = []
        # TLS (core.p2p.tls): first a small torrent is fetched that holds the real
        # one's description (phase 1), it is let go of and its file read (2, then 3
        # once libtorrent has said it is gone), and then the real torrent is added.
        self._meta_handle = None
        self._meta_dir: Optional[Path] = None
        self._meta_phase = 0
        self._meta_removals = 0         # removals of that first torrent still to be told (they are not "released")
        self._leaf: Optional[tuple] = None

    @property
    def private(self) -> bool:
        """Is this a private-mode (Tor) session?"""
        return self._private

    def private_state(self) -> dict:
        """For a private session, what the panel says while the token cannot be used yet:
        {"state": Tor's ("starting"/"ready"/"failed"), "progress": 0-100, "error", "published":
        the onion service can be found, "sender": a sender has made contact}. {} otherwise."""
        if self._host is None:
            return {}
        st = _tor.service().status()
        return {"state": st["state"], "progress": st["progress"], "error": st["error"],
                "published": self._host.published, "sender": self._host.controls() > 0}

    @property
    def expires_at(self) -> float:
        """When the token stops being accepted (this machine's clock)."""
        return self.created_at + TOKEN_TTL_SECONDS

    def remaining(self, now: Optional[float] = None) -> float:
        """Seconds the token has left; 0 once it has run out."""
        return max(0.0, self.expires_at - (time.time() if now is None else now))

    @property
    def expired(self) -> bool:
        """True once the token is older than TOKEN_TTL_SECONDS. An offer that
        arrived in time stays good until it is answered; no new one is taken -
        unless the sender had already said it was getting one ready (preparing)."""
        return time.time() >= self.expires_at

    @property
    def preparing(self) -> bool:
        """The sender made contact in time and is still getting the offer ready
        (it keeps saying so; HOLD_STALE_SECONDS of silence and this is False)."""
        return self.held and self.offer is None and time.time() - self.held_at < HOLD_STALE_SECONDS

    @property
    def lapsed(self) -> bool:
        """The token ran out with nobody having come: expired, and no sender
        busy preparing. This - not ``expired`` - is what ends the wait."""
        return self.expired and not self.preparing

    def pump(self) -> None:
        if self._closed:
            return
        now = time.time()
        if self._host is not None:
            # (private mode: what the sender says arrives on the control stream, no
            # mailbox is looked at and no address is left anywhere)
            for item in self._host.items():
                self._on_mailbox_item(item)
        elif (self.offer is None and not self.lapsed
                and now - self._last_get_at > _REPOLL_SECONDS):
            self._last_get_at = now
            try:
                self._ses.dht_get_mutable_item(self._pub32, _DHT_SALT)
            except Exception:
                logger.debug("P2P receive: dht_get_mutable_item failed", exc_info=True)
        elif (self.offer is not None and (self._handle is not None or self._meta_phase)
                and not self.download_done
                and not self._release_requested and not self.sender_cancelled
                and now - self._last_get_at > _CANCEL_POLL_SECONDS):
            # The offer is taken, but the sender can still call it off: the
            # mailbox is looked at now and then for as long as this runs.
            self._last_get_at = now
            try:
                self._ses.dht_get_mutable_item(self._pub32, _DHT_SALT)
            except Exception:
                logger.debug("P2P receive: dht_get_mutable_item failed", exc_info=True)
        # Our address is left at the rendezvous from the start (a sender behind
        # a router can then come to us); looking the sender up begins once the
        # offer is accepted, and only while there is nobody connected yet.
        if self._rv is not None:
            lonely = False
            target = self._handle if self._handle is not None else self._meta_handle
            if target is not None and not self.download_done and not self._release_requested:
                try:
                    lonely = target.status().num_peers == 0
                except Exception:
                    pass
            self._rv.tick(now, lookup=lonely)
        for a in self._ses.pop_alerts():
            self._on_alert(a)
        self._advance_tls()
        self._advance_remap()
        if self.released and self._host is not None:
            # (private mode: the download is over, whole or called off - nobody has any more
            # business at this onion address, so it is taken down rather than left up)
            host, self._host = self._host, None
            host.close()

    def _on_alert(self, a) -> None:
        name = type(a).__name__
        target = self._handle if self._handle is not None else self._meta_handle
        live = target is not None and not self._release_requested
        if self._rv is not None:
            for ep in self._rv.endpoints(a, time.time(), connectable=live):
                try:
                    target.connect_peer(ep)
                except Exception:
                    logger.debug("P2P receive: connect_peer failed", exc_info=True)
        if name == "dht_mutable_item_alert":
            self._on_mailbox_item(bytes(getattr(a, "item", b"") or b""))
        elif name == "torrent_finished_alert":
            # A "file" offer starts with nothing selected, which libtorrent
            # calls finished: only one that comes after the files were placed
            # means the download is over. (A TLS transfer has a first torrent whose
            # finishing means something else: that is not this.)
            if (self._remap in (_REMAP_NONE, _REMAP_DONE) and self._handle is not None
                    and (self.offer is None or not self.offer.tls or a.handle == self._handle)):
                self.download_done = True
        elif name == "file_renamed_alert":
            self._renames_left -= 1
        elif name == "file_rename_failed_alert":
            self.download_error = f"could not place a received file: {a.message()}"
        elif name == "torrent_removed_alert":
            if self._meta_removals > 0:
                # the first torrent of a TLS transfer, let go of: its file is complete now
                self._meta_removals -= 1
                if self._meta_phase == 2:
                    self._meta_phase = 3
            else:
                self.released = True
        elif name in ("torrent_error_alert", "file_error_alert"):
            self.download_error = str(getattr(a, "error", "") or a.message())

    def _on_mailbox_item(self, item: bytes) -> None:
        """Something the sender left for this token: the item in the DHT mailbox, or (in
        private mode) a frame that came on the control stream. Before the offer it is the
        offer, a "hold" or a "cancel"; after it only a "cancel" matters (_read_notice)."""
        if self.offer is not None:
            self._read_notice(item)
            return
        if not item or item == self._offer_raw or self.lapsed:
            return          # (a token is only good for TOKEN_TTL_SECONDS)
        self._offer_raw = item
        try:
            # Sealed under the token: anything else in this mailbox (an
            # older build's plain offer, a stranger's guess) is not ours.
            data = json.loads(_secret.open_sealed(self._seed32, item).decode("utf-8"))
            if isinstance(data, dict) and data.get("cancel") and "magnet" not in data:
                self.sender_cancelled = True        # (called off before it began)
                return
            if isinstance(data, dict) and "hold" in data and "magnet" not in data:
                # "I am here and still busy": the first contact the token's
                # time is for. (Only counted while that time lasts, or
                # while an earlier one is still fresh - see lapsed.)
                self.held, self.held_at = True, time.time()
                return
            tls, meta, ih = bool(data.get("tls")), b"", ""
            if tls:
                meta = b64decode(str(data["m1"]).encode("ascii"), validate=True)
                ih = str(data["h2"]).lower()
                if not (0 < len(meta) <= 2000 and len(ih) == 40 and all(c in "0123456789abcdef" for c in ih)):
                    raise ValueError("the TLS description in the offer is not valid")
            self.offer = TransferOffer(
                magnet="" if tls else str(data["magnet"]),
                tls=tls, tls_meta=meta, tls_hash=ih,
                filename=_safe_filename(data.get("filename")),
                size=int(data.get("size") or 0),
                game_name=str(data.get("game_name") or ""),
                sender_username=str(data.get("sender_username") or ""),
                sender_machine_id=str(data.get("sender_machine_id") or ""),
                folder=str(data.get("folder") or ""),
                kind="file" if data.get("kind") == "file" else "backup",
                count=int(data.get("count") or 0),
                attach=str(data.get("attach") or "")[:32],
            )
        except Exception:
            logger.debug("P2P receive: mailbox item did not parse", exc_info=True)

    def _read_notice(self, item: bytes) -> None:
        """After the offer: has the sender written a "cancel" into the mailbox?"""
        if not item or item == self._offer_raw or item == self._notice_raw:
            return
        self._notice_raw = item
        try:
            data = json.loads(_secret.open_sealed(self._seed32, item).decode("utf-8"))
        except Exception:
            return
        if isinstance(data, dict) and data.get("cancel") and "magnet" not in data:
            self.sender_cancelled = True

    def accept(self, save_dir: Path) -> None:
        """Start downloading the accepted offer into *save_dir*.

        For a "file" offer *save_dir* is the folder the files are meant to end
        up in: they are downloaded straight there, not into a staging folder
        and moved afterwards (see _begin_remap)."""
        if self.offer is None:
            raise P2pError("no transfer offer to accept yet")
        lt = _require_libtorrent()
        self._save_dir = Path(save_dir)
        self._save_dir.mkdir(parents=True, exist_ok=True)
        if self.offer.tls:
            self._accept_tls(lt)
            return
        atp = lt.parse_magnet_uri(self.offer.magnet)
        atp.save_path = str(self._save_dir)
        # The sender is found at the rendezvous (see _Rendezvous), never by
        # announcing or looking up the torrent itself: no trackers, no web
        # seeds (whatever the magnet says), and the DHT, LAN discovery and
        # peer exchange are off for it.
        atp.trackers = []
        atp.url_seeds = []
        atp.flags |= _hidden_flags(lt)
        if self.offer.kind == "file":
            # Nothing is fetched until the files have their final names: data
            # written under a name that is already taken would land in the
            # person's own file.
            atp.flags |= lt.torrent_flags.default_dont_download
            self._remap = _REMAP_WAIT
        self._handle = self._ses.add_torrent(atp)
        if self._host is not None:
            # (private mode: the sender is waiting to be told the torrent is there - it
            # is the one that connects, through Tor, to this PC's BitTorrent port)
            self._host.send(_secret.seal(self._seed32, json.dumps({"ready": 1}).encode("utf-8")), sticky=True)

    def _accept_tls(self, lt) -> None:
        """The first step of a TLS transfer: add the small torrent that holds the
        real one's description, under the certificate authority this token gives."""
        if not _tls.available(lt):
            raise P2pError("this offer needs TLS, which this libtorrent cannot do")
        info = lt.bdecode(self.offer.tls_meta)
        if not isinstance(info, dict):
            raise P2pError("the offer's description is not valid")
        info[b"ssl-cert"] = _tls.root_cert_pem(self._seed32).encode("ascii")
        ti = lt.torrent_info(lt.bencode({b"info": info}))
        files = ti.files()
        pad = int(getattr(lt.file_storage, "flag_pad_file", 0))
        # (one file, "m"; a version-2 torrent may add padding after it, which is no file)
        if (files.file_path(0) != _META_NAME or files.file_size(0) > _MAX_META_BYTES
                or any(not int(files.file_flags(i)) & pad for i in range(1, files.num_files()))):
            raise P2pError("the offer's description is not valid")
        self._leaf = _tls.leaf(self._seed32)
        self._meta_dir = Path(tempfile.mkdtemp(prefix="savesync_p2p_meta_"))
        try:
            atp = lt.add_torrent_params()
            atp.ti = ti
            atp.save_path = str(self._meta_dir)
            atp.flags |= _hidden_flags(lt)
            self._rv.lookup = "tls"
            if self.offer.kind == "file":
                self._remap = _REMAP_WAIT       # (as for any "file" offer: see _begin_remap)
            self._meta_handle = self._ses.add_torrent(atp)
            self._meta_handle.set_ssl_certificate_buffer(self._leaf[0], self._leaf[1], _tls.dh_params_pem())
            self._meta_phase = 1
        except BaseException:
            _remove_dir_later(self._meta_dir)
            self._meta_dir = None
            raise

    def _advance_tls(self) -> None:
        """Move a TLS transfer from its first torrent to the real one."""
        if not self._meta_phase or self._release_requested:
            return
        if self._meta_phase == 1:
            try:
                finished = bool(self._meta_handle.status().is_finished)
            except Exception:
                return
            if finished:
                # Read the file only once libtorrent has let go of it: "finished"
                # comes before the bytes are all in the file (see request_release).
                self._meta_phase = 2
                self._meta_removals += 1
                try:
                    self._ses.remove_torrent(self._meta_handle)
                except Exception:
                    logger.debug("P2P receive: letting go of the first torrent failed", exc_info=True)
                    self._meta_removals -= 1
                    self._meta_phase = 3
                self._meta_handle = None
            return
        if self._meta_phase != 3:
            return
        self._meta_phase = 0
        lt = _require_libtorrent()
        meta_dir, self._meta_dir = self._meta_dir, None
        try:
            info = lt.bdecode((meta_dir / _META_NAME).read_bytes())
            if not isinstance(info, dict):
                raise ValueError("not a description")
            info[b"ssl-cert"] = _tls.root_cert_pem(self._seed32).encode("ascii")
            ti = lt.torrent_info(lt.bencode({b"info": info}))
            if str(ti.info_hash()) != self.offer.tls_hash:
                raise ValueError("the description is not the one the offer named")
        except Exception as e:
            logger.debug("P2P receive: the TLS description did not check out", exc_info=True)
            self.download_error = f"the transfer's description could not be used: {e}"
            return
        finally:
            _remove_dir_later(meta_dir)
        atp = lt.add_torrent_params()
        atp.ti = ti
        atp.save_path = str(self._save_dir)
        atp.flags |= _hidden_flags(lt)
        if self.offer.kind == "file":
            atp.flags |= lt.torrent_flags.default_dont_download
        self._handle = self._ses.add_torrent(atp)
        self._handle.set_ssl_certificate_buffer(self._leaf[0], self._leaf[1], _tls.dh_params_pem())
        for ep in self._rv.recent(time.time()):         # (no need to wait for the next lookup)
            try:
                self._handle.connect_peer(ep)
            except Exception:
                logger.debug("P2P receive: connect_peer failed", exc_info=True)

    @property
    def files_placed(self) -> bool:
        """False while a "file" offer is still being put in place (and so
        nothing is downloading yet); True otherwise."""
        return self._remap in (_REMAP_NONE, _REMAP_DONE)

    def _advance_remap(self) -> None:
        if self._handle is None or self._remap in (_REMAP_NONE, _REMAP_DONE):
            return
        try:
            if self._remap == _REMAP_WAIT:
                if self._handle.status().has_metadata:
                    self._begin_remap()
            elif self._renames_left <= 0:
                self._finish_remap()
            elif time.monotonic() - self._remap_started > _RENAME_TIMEOUT:
                self.download_error = _say("p2p.err_place_timeout")
        except P2pRefused as e:
            self.refused = e.reason
            self.download_error = f"refused: {e.reason}"
        except Exception as e:
            logger.debug("P2P receive: placing the files failed", exc_info=True)
            self.download_error = _say("p2p.err_place_failed", error=e)

    def _begin_remap(self) -> None:
        """The torrent's file list is known: give every file its final place
        (plan_targets) while nothing has been written yet — libtorrent just
        renames them in its own bookkeeping — then, once it has confirmed
        each (_finish_remap), let the download begin.

        A torrent file is "<root>/<path>"; the root only exists because a
        torrent needs one, and is dropped. Links and padding are not files
        and are left out (never created, never downloaded)."""
        lt = _require_libtorrent()
        fs = self._handle.torrent_file().files()
        skip = 0
        for flag in ("flag_symlink", "flag_pad_file"):
            skip |= int(getattr(lt.file_storage, flag, 0))
        idxs, rels, sizes = [], [], []
        for i in range(fs.num_files()):
            if int(fs.file_flags(i)) & skip:
                continue
            parts = fs.file_path(i).replace("\\", "/").split("/")
            idxs.append(i)
            rels.append("/".join(parts[1:]) if len(parts) > 1 else parts[0])
            sizes.append(int(fs.file_size(i)))
        if not idxs:
            raise P2pError("the transfer holds no files")
        if self.name_filter is not None:
            # (chat attachments: the files must be what was offered, and of
            # kinds that are taken at all - before a single byte is fetched)
            why = self.name_filter(rels, sizes)
            if why:
                raise P2pRefused(why)
        targets = plan_targets(self._save_dir, rels)
        new_dirs = set()
        for target in targets:
            parts = target.split("/")[:-1]
            for k in range(1, len(parts) + 1):
                sub = "/".join(parts[:k])
                if not (self._save_dir / sub).exists():
                    new_dirs.add(sub)
        self.new_dirs = sorted(new_dirs)
        self._plan = list(zip(idxs, targets, sizes))
        self._renames_left = len(idxs)
        self._remap = _REMAP_RENAMING
        self._remap_started = time.monotonic()
        for i, target in zip(idxs, targets):
            self._handle.rename_file(i, target)

    def _finish_remap(self) -> None:
        n = self._handle.torrent_file().files().num_files()
        wanted = {i for i, _t, _s in self._plan}
        self._handle.prioritize_files([_WANTED if i in wanted else 0 for i in range(n)])
        self._remap = _REMAP_DONE

    def files_complete(self) -> bool:
        """Every byte of every file of a "file" offer has arrived (bytes per
        file, not the torrent's own "finished", which is also true while
        nothing is wanted yet)."""
        if (self._remap != _REMAP_DONE or not self._plan or self._handle is None
                or self._release_requested):
            return False
        try:
            done = self._whole_pieces_progress()
            return all(int(done[i]) >= size for i, _t, size in self._plan)
        except Exception:
            return False

    def _whole_pieces_progress(self) -> list:
        """Bytes of each file that are in AND checked - counted by whole pieces. The
        default counts every block that has arrived, so a small file in a piece that
        is still half empty looks finished while nothing of it is on the disk yet:
        what decides whether a file is whole (to keep it, to let go of the torrent)
        must not go by that."""
        lt = _require_libtorrent()
        try:
            return self._handle.file_progress(lt.torrent_handle.piece_granularity)
        except TypeError:
            return self._handle.file_progress()

    @property
    def release_requested(self) -> bool:
        return self._release_requested

    def request_release(self) -> None:
        """The download is over: let the session go of the torrent, once. The
        downloaded files are complete and closed only when ``released`` turns
        True - read them before that and the tail can still be missing. The
        handle cannot be used after this."""
        if self._release_requested or self._handle is None:
            return
        self._release_requested = True
        try:
            self._root_name = self._handle.status().name      # finalize() needs it
        except Exception:
            logger.debug("P2P receive: torrent name unavailable", exc_info=True)
        try:
            self._ses.remove_torrent(self._handle)
        except Exception:
            logger.debug("P2P receive: remove_torrent failed", exc_info=True)
            self.released = True            # nothing to wait for

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def abort_stream(self) -> None:
        """Whoever is watching this download as it arrives can have no more of it."""
        if self._stream is not None:
            self._stream.close()

    def request_cancel(self) -> list:
        """Give this download up. Notes which files of a "file" offer had arrived
        whole (those are kept: the relative paths are returned) and which are
        everything else it writes, then lets go of the torrent. Once ``released``,
        discard_unfinished() removes the rest. Anything it has not yet begun to
        write is simply never written."""
        if self._meta_phase and self._handle is None and not self._cancelled and not self._release_requested:
            # (a TLS transfer still fetching its description: nothing of it is on
            # the person's disk, so there is nothing to keep or to remove)
            self._cancelled = self._release_requested = True
            self._drop_meta()
            self.released = True
            return []
        if self._cancelled or self._handle is None or self._release_requested:
            return list(self._kept)
        self._cancelled = True
        if self._stream is not None:
            self._stream.close()
        kept: list = []
        doomed: list = []
        try:
            if self._plan and self._save_dir is not None:
                done = self._whole_pieces_progress()
                for i, target, size in self._plan:
                    if int(done[i]) >= size:
                        kept.append(target)
                    else:
                        doomed.append(self._save_dir / target)
            elif self._save_dir is not None:
                # A backup: everything it writes is this transfer's own.
                fs = self._handle.torrent_file().files()
                doomed = [self._save_dir / fs.file_path(i) for i in range(fs.num_files())]
        except Exception:
            logger.debug("P2P receive: could not list what was written", exc_info=True)
        self._kept, self._doomed = kept, doomed
        self.request_release()
        return list(kept)

    def discard_unfinished(self) -> int:
        """Remove what a called-off download left half-written (call once
        ``released``, off the GUI thread - a file libtorrent has only just let go
        of can need a moment): the files that were not whole, and the folders
        this transfer made that are empty now. Nothing else is touched. Returns
        how many files were removed."""
        removed = 0
        base = self._save_dir
        if base is None:
            return 0
        try:
            root = base.resolve()
        except OSError:
            return 0
        for path in self._doomed:
            try:
                path.resolve().relative_to(root)        # (only ever inside the save folder)
            except (ValueError, OSError):
                continue
            for _ in range(10):
                try:
                    path.unlink()
                    removed += 1
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.3)
        for sub_dir in sorted(self.new_dirs, key=lambda s: s.count("/"), reverse=True):
            try:
                os.rmdir(base / sub_dir)
            except OSError:
                pass
        return removed

    def stream_source(self) -> Optional["TorrentStream"]:
        """A TorrentStream for the ONE file of a "file" offer that is arriving, so
        that it can be watched meanwhile - or None (not a single file, not placed
        yet, finished or called off, or a torrent this cannot map). The torrent is
        told to fetch in order from the first read on (not before)."""
        if self._stream is not None:
            return self._stream
        if (self._handle is None or self._remap != _REMAP_DONE or not self._plan or len(self._plan) != 1
                or self._release_requested or self._cancelled or self._save_dir is None):
            return None
        lt = _require_libtorrent()
        idx, target, size = self._plan[0]
        handle = self._handle
        try:
            ti = handle.torrent_file()
            if int(ti.files().file_offset(idx)) != 0:
                return None                 # (padding in front of it: pieces would not line up)
            piece_length = int(ti.piece_length())
            v1 = bool(ti.v1()) if hasattr(ti, "v1") else True
        except Exception:
            logger.debug("P2P receive: cannot stream this torrent", exc_info=True)
            return None

        def have(piece):
            return bool(handle.have_piece(int(piece)))

        def want(piece, deadline_ms):
            handle.set_piece_deadline(int(piece), int(deadline_ms))

        def expected(piece):
            if not v1:
                return None
            h = ti.hash_for_piece(int(piece))       # (bytes in this binding; a sha1_hash in others)
            return bytes(h) if isinstance(h, (bytes, bytearray)) else bytes(h.to_bytes())

        def begin():
            # (only once somebody really reads: just offering to stream changes nothing)
            handle.set_flags(lt.torrent_flags.sequential_download, lt.torrent_flags.sequential_download)

        self._stream = TorrentStream(
            name=Path(target).name, path=self._save_dir / target, size=size, piece_length=piece_length,
            have=have, want=want, expected=expected, begin=begin,
            done=lambda: self.download_done or (self._release_requested and not self._cancelled),
            alive=lambda: not self._cancelled and (not self._closed or self.download_done))
        return self._stream

    def finalize(self) -> tuple:
        """(backup_entry dict, path to the downloaded save archive) — call
        once download_done is True. Reads the manifest that travelled
        alongside the archive inside the torrent (see module docstring);
        backup_entry is {} if the sender didn't include one (an older
        SaveSync build), in which case only the file itself is usable.
        """
        if self._handle is None or self._save_dir is None:
            raise P2pError("nothing has been downloaded yet")
        root_name = self._root_name or self._handle.status().name
        transfer_dir = self._save_dir / root_name
        manifest_path = transfer_dir / _MANIFEST_NAME
        backup_entry: dict = {}
        # The offer's filename rides in a 1000-byte DHT item and may have been
        # trimmed to fit; the manifest carries the real one.
        filename = self.offer.filename
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest.get("backup_entry"), dict):
                    backup_entry = manifest["backup_entry"]
                if isinstance(manifest.get("filename"), str) and manifest["filename"]:
                    filename = manifest["filename"]
            except Exception:
                logger.debug("P2P receive: manifest did not parse", exc_info=True)
        save_path = transfer_dir / _safe_filename(filename)
        try:
            save_path.resolve().relative_to(transfer_dir.resolve())
        except ValueError:
            raise P2pError("received filename escaped the transfer directory")
        if not save_path.is_file():
            # An older sender's manifest has no filename and its offer may have
            # been trimmed: the save is then the one file next to the manifest.
            others = [p for p in transfer_dir.iterdir()
                      if p.is_file() and p.name != _MANIFEST_NAME] \
                if transfer_dir.is_dir() else []
            if len(others) != 1:
                raise P2pError("the downloaded save file is missing")
            save_path = others[0]
        return backup_entry, save_path

    def finalize_files(self) -> tuple:
        """(the folder the files were received into, [path of every file
        relative to it]) for a "file" offer — call once the download is
        complete and released (see request_release). They are already where they belong (see
        _begin_remap): this only checks that each one is really there, whole."""
        if self._remap != _REMAP_DONE or not self._plan or self._save_dir is None:
            raise P2pError("nothing has been downloaded yet")
        rels: list = []
        for _i, target, size in self._plan:
            path = self._save_dir / target
            if size == 0 and not path.exists():
                # An empty file has no data to write, so nothing creates it.
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                except OSError:
                    pass
            if not path.is_file():
                raise P2pError(f"a received file is missing: {target}")
            if path.stat().st_size != size:
                raise P2pError(f"a received file is incomplete: {target}")
            rels.append(target)
        return self._save_dir, rels

    def file_progress(self) -> Optional[list]:
        """``[(path, size, bytes downloaded)]`` for every file of a "file"
        transfer, under the names they are being given — None until they have
        them (a magnet link has to fetch the file list from the sender first)
        or when this is not a transfer of files."""
        if (self._handle is None or self._remap != _REMAP_DONE or not self._plan
                or self._release_requested):
            return None
        try:
            done = self._handle.file_progress()
            return [(target, size, int(done[i])) for i, target, size in self._plan]
        except Exception:
            logger.debug("P2P receive: per-file progress unavailable", exc_info=True)
            return None

    def _drop_meta(self) -> None:
        """Let go of the first torrent of a TLS transfer and its folder."""
        if self._meta_handle is not None:
            self._meta_removals += 1
            try:
                self._ses.remove_torrent(self._meta_handle)
            except Exception:
                self._meta_removals -= 1
            self._meta_handle = None
        self._meta_phase = 0
        if self._meta_dir is not None:
            _remove_dir_later(self._meta_dir)
            self._meta_dir = None

    def progress(self) -> Optional[dict]:
        """None before accept() and after request_release(); a dict of live
        download stats in between."""
        if self._handle is None and self._meta_phase and not self._release_requested:
            # (a TLS transfer fetching its description: nothing is downloading yet)
            try:
                peers = self._meta_handle.status().num_peers if self._meta_handle is not None else 0
            except Exception:
                peers = 0
            return {"progress": 0.0, "download_rate": 0, "num_peers": peers, "state": "downloading_metadata"}
        if self._handle is None or self._release_requested:
            return None
        st = self._handle.status()
        return {
            "progress": st.progress,
            "download_rate": st.download_rate,
            "num_peers": st.num_peers,
            "state": str(st.state),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stream is not None and (self._cancelled or not self.download_done):
            self._stream.close()            # (one that finished is still read from the file)
        try:
            if self._handle is not None:
                self._ses.remove_torrent(self._handle)
        except Exception:
            pass
        self._drop_meta()
        if self._host is not None:
            try:
                self._host.close()
            except Exception:
                logger.debug("P2P receive: closing the onion service failed", exc_info=True)


def _remove_dir_later(path) -> None:
    """Delete a folder of ours, retrying for a few seconds: libtorrent can still
    hold a file in it for a moment after it has been told to let go."""
    path = Path(path)

    def work():
        for _ in range(20):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
            time.sleep(0.5)

    threading.Thread(target=work, name="p2p-meta-cleanup", daemon=True).start()


@dataclass
class SendHandle:
    """A file being seeded out to whoever accepts it. put_confirmed is
    None until the DHT publish's own result comes back (see pump())."""
    _session: object
    _handle: object
    _staging_root: Optional[Path] = None
    put_confirmed: Optional[bool] = None
    _stopped: bool = False
    _rv: Optional[_Rendezvous] = None
    # (signing key, public key, sealed offer) while the publish may be retried:
    # a session that has only just started can put the item before its DHT has
    # a single live node to give it to, and that says nothing about the
    # network - so a put that reaches nobody is tried again before it is
    # reported as failed.
    _put: Optional[tuple] = None
    _put_tries: int = 1
    _put_retry_at: float = 0.0
    _put_at: float = 0.0
    # (seed, signing key, public key) of the receiver's mailbox, for cancel().
    _notice: Optional[tuple] = None
    # A TLS transfer also seeds the small first torrent that carries the real one's
    # description (core.p2p.tls); None for any other.
    _meta_handle: object = None
    # Private mode (Tor): the client that reaches the receiver's onion service
    # (core.p2p.tor_link), the token's seed, the latest moment a first contact can still
    # be made, whether the receiver has said its torrent is there, and when each bridge
    # was last pointed at.
    _client: object = None
    _seed32: bytes = b""
    _contact_by: float = 0.0
    _ready: bool = False
    _bridge_tried: dict = field(default_factory=dict)

    _BRIDGE_RETRY_SECONDS = 8.0
    _PUT_TRIES = 3
    _PUT_RETRY_SECONDS = 4.0
    # A put that has not been answered in this long was lost (made before the
    # DHT was ready, or under a DHT that was started again - as one behind a
    # proxy is): it is made again, like one that reached nobody.
    _PUT_SILENCE_SECONDS = 25.0

    def _pump_private(self, now: float) -> None:
        """Private mode: read what the receiver says, tell when the offer has gone, and
        point libtorrent at the bridges once the receiver is ready for it."""
        client = self._client
        for blob in client.items():
            try:
                data = json.loads(_secret.open_sealed(self._seed32, blob).decode("utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("ready"):
                self._ready = True
        if self.put_confirmed is None:
            if client.connected_at:
                self.put_confirmed = True       # (the offer is sent first on every control stream)
                # The receiver will take its time to look at the offer; the circuits the
                # transfer will use are made meanwhile, so that it starts at once when it says
                # it is ready (a rendezvous takes 3-60 s).
                client.warm()
            elif now > self._contact_by:
                self.put_confirmed = False      # nobody was ever reached while the token was good
        if self._ready:
            for b in client.bridges:
                if not b.live and now - self._bridge_tried.get(b.index, 0.0) >= self._BRIDGE_RETRY_SECONDS:
                    self._bridge_tried[b.index] = now
                    try:
                        self._handle.connect_peer(("127.0.0.1", b.port))
                    except Exception:
                        logger.debug("P2P send: connect_peer failed", exc_info=True)

    def pump(self) -> None:
        if self._stopped:
            return
        now = time.time()
        if self._client is not None:
            self._pump_private(now)
            for _a in self._session.pop_alerts():
                pass
            return
        if not self._put_at:
            self._put_at = now
        if (self.put_confirmed is None and self._put is not None and not self._put_retry_at
                and now - self._put_at > self._PUT_SILENCE_SECONDS):
            if self._put_tries < self._PUT_TRIES:
                self._put_retry_at = now
            else:
                self.put_confirmed = False
        if self._put_retry_at and time.time() >= self._put_retry_at and self._put is not None:
            self._put_retry_at = 0.0
            self._put_tries += 1
            self._put_at = now
            try:
                self._session.dht_put_mutable_item(*self._put, _DHT_SALT)
            except Exception:
                logger.debug("P2P send: re-publishing the offer failed", exc_info=True)
        rv = self._rv
        if rv is not None:
            lonely = False
            try:
                lonely = self._handle.status().num_peers == 0
            except Exception:
                pass
            rv.tick(time.time(), lookup=lonely)
        for a in self._session.pop_alerts():
            if type(a).__name__ == "dht_put_alert" and self.put_confirmed is None:
                if getattr(a, "num_success", 0) > 0:
                    self.put_confirmed = True
                elif self._put is not None and self._put_tries < self._PUT_TRIES:
                    self._put_retry_at = time.time() + self._PUT_RETRY_SECONDS
                else:
                    self.put_confirmed = False
            if rv is not None:
                for ep in rv.endpoints(a, time.time()):
                    for h in (self._handle, self._meta_handle):
                        if h is None:
                            continue
                        try:
                            h.connect_peer(ep)
                        except Exception:
                            logger.debug("P2P send: connect_peer failed", exc_info=True)

    def progress(self) -> dict:
        if self._stopped:
            return {"num_peers": 0, "upload_rate": 0, "all_time_upload": 0}
        st = self._handle.status()
        peers = st.num_peers
        if self._meta_handle is not None:
            try:
                peers += self._meta_handle.status().num_peers        # (the first torrent counts as a visit too)
            except Exception:
                pass
        if self._client is not None:
            peers = min(peers, 1)       # (one receiver, however many circuits it comes over)
        return {
            "num_peers": peers,
            "upload_rate": st.upload_rate,
            "all_time_upload": st.all_time_upload,
        }

    def cancel(self) -> None:
        """The person called this send off: say so in the receiver's mailbox - a
        sealed ``{"cancel": 1}``, which its session reads and stops on, cleaning
        up what it had only partly written - then stop exactly as stop() does.
        (The notice is best effort, and given a few seconds to be stored.)"""
        if self._stopped:
            return
        if self._client is not None:
            try:
                self._client.send(_secret.seal(self._seed32, json.dumps({"cancel": 1}).encode("utf-8")),
                                  sticky=True)
            except Exception:
                logger.debug("P2P send: publishing the cancel notice failed", exc_info=True)
            self.stop(_linger=_PRIVATE_CANCEL_LINGER_SECONDS)
            return
        wait = False
        notice, ses = self._notice, self._session
        if notice is not None and ses is not None:
            seed32, sk64, pub32 = notice
            try:
                blob = _secret.seal(seed32, json.dumps({"cancel": 1}).encode("utf-8"))
                ses.dht_put_mutable_item(sk64, pub32, blob, _DHT_SALT)
                wait = True
            except Exception:
                logger.debug("P2P send: publishing the cancel notice failed", exc_info=True)
        self.stop(_wait_put=wait)

    def stop(self, _wait_put: bool = False, _linger: float = 0.0) -> None:
        """Stop seeding and delete the staging folder (links only — the
        original files are never touched). Safe to call twice.

        remove_torrent is asynchronous and the session keeps the staged files
        open until it is gone, so the folder cannot simply be deleted here (on
        Windows that fails, and ignore_errors hid it): the session is let go
        and the folder removed on a background thread, retrying until it
        goes. (Private mode: the connection to the receiver is closed too, after
        *_linger* seconds - a "cancel" that was just sent needs a moment to leave.)"""
        if self._stopped:
            return
        self._stopped = True
        ses, handle, root = self._session, self._handle, self._staging_root
        meta = self._meta_handle
        client, self._client = self._client, None
        self._session = self._handle = self._staging_root = self._rv = self._put = self._notice = None
        self._meta_handle = None
        if client is not None:
            _close_later(client, _linger)
        try:
            ses.remove_torrent(handle)
        except Exception:
            pass
        if meta is not None:
            try:
                ses.remove_torrent(meta)
            except Exception:
                pass
        holder = [ses, handle, meta]
        del ses, handle, meta
        threading.Thread(target=_release_send_session, args=(holder, root, _wait_put),
                         name="p2p-send-cleanup", daemon=True).start()


def _close_later(client, delay: float) -> None:
    """Close a private-mode client (core.p2p.tor_link.Client) after *delay* seconds."""
    if delay <= 0:
        client.close()
        return

    def work():
        time.sleep(delay)
        client.close()

    threading.Thread(target=work, name="p2p-tor-close", daemon=True).start()


def _release_send_session(holder: list, root: Optional[Path], wait_put: bool = False) -> None:
    """Background half of SendHandle.stop(): wait (a few seconds at most) for
    libtorrent to confirm the torrent is gone - and, after a cancel, for the
    notice to have been stored, which needs the session alive - let go of the
    session so its files are closed, then delete the staging folder."""
    try:
        ses = holder[0]
        deadline = time.monotonic() + (_CANCEL_SETTLE_SECONDS if wait_put else 3.0)
        removed, stored = False, not wait_put
        while not (removed and stored) and time.monotonic() < deadline:
            for a in ses.pop_alerts():
                name = type(a).__name__
                removed = removed or name == "torrent_removed_alert"
                stored = stored or name == "dht_put_alert"
            if not (removed and stored):
                time.sleep(0.05)
    except Exception:
        logger.debug("P2P send: waiting for the torrent to go failed", exc_info=True)
    finally:
        holder.clear()
        ses = None          # the last reference: the session shuts down
    if root is None:
        return
    for _ in range(20):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return
        time.sleep(0.5)
    logger.warning(f"P2P send: could not remove the staging folder {root}")


def sweep_stale_staging(max_age_seconds: float = 24 * 3600) -> None:
    """Delete send-staging folders an earlier run left behind (the app closed
    before their cleanup finished). Only old ones: a younger one may belong to
    a transfer another SaveSync instance is still seeding."""
    base = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_seconds
    try:
        # (and the folder a TLS transfer's first torrent is received into, if the app
        # closed in the few seconds it exists)
        for p in [*base.glob("savesync_p2p_send_*"), *base.glob("savesync_p2p_meta_*")]:
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        logger.debug("P2P send: staging sweep failed", exc_info=True)


def send_backup(token_str: str, file_path, sender_username: str,
                sender_machine_id: str, game_name: str = "",
                backup_entry: Optional[dict] = None,
                folder_name: str = "", kind: str = "backup", progress=None,
                tls: Optional[bool] = None, friend_key: Optional[bytes] = None) -> SendHandle:
    """Create a torrent for *file_path*, start seeding it, and publish the
    offer into the DHT mailbox *token_str* (the RECEIVER's token) points to.

    *backup_entry* is the sender's own core.backup.BackupEntry, already
    turned into a plain dict (entry.to_dict()) by the caller — this module
    stays independent of core.backup and just carries it through unread. It
    travels inside the torrent, not the DHT mailbox (see module docstring
    for why), alongside a hardlink of *file_path* rather than the original in
    place — a multi-file torrent needs every member under one shared root, and
    the original save folder is not that. The file is never copied (see
    P2pLinkError).

    Returns a handle to keep seeding from — poll its pump() for whether the
    DHT publish itself succeeded, and keep the handle alive (do not stop())
    until the transfer is known to be picked up: stopping it removes the
    only seed AND the link (never the original file).
    """
    session_seed(token_str, friend_key)     # a bad token (or a bound one with no key) fails before anything is staged
    file_path = Path(file_path)
    if not file_path.is_file():
        raise P2pError(f"{file_path} is not a file")

    staging_root = _make_staging_root(file_path)
    try:
        transfer_dir = staging_root / _transfer_dir_name()
        transfer_dir.mkdir(parents=True)
        _stage_file(file_path, transfer_dir / file_path.name)
        # The real file name travels here too: the one in the DHT offer is
        # trimmed to fit (see _fit_offer) and the receiver finds the file by it.
        (transfer_dir / _MANIFEST_NAME).write_text(
            json.dumps({"backup_entry": backup_entry or {},
                        "filename": file_path.name}), encoding="utf-8")
        return _seed_and_offer(token_str, staging_root, transfer_dir, {
            "filename": file_path.name,
            "size": file_path.stat().st_size,
            "game_name": game_name,
            "folder": folder_name,
            "kind": "file" if kind == "file" else "backup",
            "sender_username": sender_username,
            "sender_machine_id": sender_machine_id,
        }, progress, tls, friend_key)
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def send_files(token_str: str, files, sender_username: str,
               sender_machine_id: str, label: str = "",
               folder_name: str = "", attach: str = "", progress=None,
               tls: Optional[bool] = None, friend_key: Optional[bytes] = None) -> SendHandle:
    """Seed several files AS THEY ARE (no zip, no backup entry) and publish
    the offer under the receiver's token.

    *files* is ``[(path on disk, relative path to send it under), ...]`` — the
    relative path keeps a picked folder's structure ("sub/a.txt"). *label* is
    what the offer is called (the folder's or file's name), *folder_name* the
    folder it came from. The receiver files nothing into its backup index:
    the files are downloaded straight into its chosen folder (see
    ReceiveSession.accept and core.p2p.paths).

    *attach* is the id of the chat attachment offer this answers (see
    core.p2p.attachments); the receiver only takes it on its own if it is the
    one it asked for. *progress* follows the preparation (see _seed_and_offer):
    however big the files are, the token's time is not what limits it.

    Same lifetime rules as send_backup: keep the handle until the transfer is
    known to be picked up. *friend_key*: the key of the friend the token was made for,
    when it was (see session_seed).
    """
    session_seed(token_str, friend_key)     # a bad token (or a bound one with no key) fails before anything is staged
    items = []
    for src, rel in files:
        src = Path(src)
        rel = Path(str(rel).replace("\\", "/"))
        if not src.is_file():
            raise P2pError(f"{src} is not a file")
        # The sender decides these names, but the receiver trusts them as
        # little as it trusts the offer: no absolute paths, no "..".
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise P2pError(f"invalid name to send: {rel}")
        items.append((src, rel))
    if not items:
        raise P2pError("no files to send")

    staging_root = _make_staging_root(items[0][0])
    try:
        # Just the files under the torrent's root: no manifest, nothing that
        # is not the person's own data — everything in the torrent is
        # downloaded straight into the receiver's folder, so an extra file
        # here would turn up there.
        transfer_dir = staging_root / _transfer_dir_name()
        transfer_dir.mkdir(parents=True)
        total = 0
        for src, rel in items:
            dest = transfer_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            _stage_file(src, dest)
            total += src.stat().st_size
        fields = {
            "filename": label or items[0][1].name,
            "size": total,
            "count": len(items),
            "game_name": "",
            "folder": folder_name,
            "kind": "file",
            "sender_username": sender_username,
            "sender_machine_id": sender_machine_id,
        }
        if attach:
            fields["attach"] = str(attach)[:32]
        return _seed_and_offer(token_str, staging_root, transfer_dir, fields, progress, tls, friend_key)
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def _transfer_dir_name() -> str:
    """The folder an offer is laid out in - the torrent's name, so it is part of
    the info-hash. 128 random bits in it keep the info-hash (the secret the
    encryption between the two PCs rests on) impossible to work out, even for a
    file everybody has. The receiver never shows or relies on this name. (26
    characters: a path on Windows has little room to spare.)"""
    return b32encode(os.urandom(16)).decode("ascii").rstrip("=").lower()


class P2pLinkError(P2pError):
    """A file cannot be offered without copying it, and it is never copied:
    a 50 GB save would need 50 GB more free space just to be sent."""


def _stage_file(src: Path, dest: Path) -> None:
    """Put *src* at *dest* for the torrent — as a hardlink: the same bytes
    under a second name, so nothing is copied and nothing extra is stored.
    Raises P2pLinkError where a link cannot be made (another volume, or a
    file system without hardlinks such as FAT/exFAT or a network share)."""
    try:
        os.link(src, dest)
    except OSError as e:
        raise P2pLinkError(f"cannot link {src.name}: {e}") from e


def _can_link(src: Path, folder: Path) -> bool:
    probe = folder / ".link_probe"
    try:
        os.link(src, probe)
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def can_send_without_copy(source) -> bool:
    """Can *source* be offered the way every transfer is - as a link, with no
    copy? (A link needs the same volume and a file system that has them.) For
    telling the person BEFORE an offer is made that it could never be sent."""
    source = Path(source)
    try:
        root = _make_staging_root(source)
    except OSError:
        return False
    try:
        return _can_link(source, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_staging_root(first_source: Path) -> Path:
    """A fresh folder to lay the offer out in, on a volume where
    *first_source* can be hardlinked (a link only works within one volume).

    The temp folder when that is the same volume; otherwise a hidden folder
    next to the source itself. _stage_file then reports it if even that is
    not possible."""
    root = Path(tempfile.mkdtemp(prefix="savesync_p2p_send_"))
    if _can_link(first_source, root):
        return root
    try:
        near = Path(tempfile.mkdtemp(prefix=".savesync_p2p_send_",
                                     dir=first_source.parent))
    except OSError:
        return root
    if _can_link(first_source, near):
        shutil.rmtree(root, ignore_errors=True)
        return near
    shutil.rmtree(near, ignore_errors=True)
    return root


class _Hold:
    """Tells the receiver the sender has arrived and is still busy.

    Getting an offer ready hashes every byte of it, which for a big file takes
    far longer than the token's ten minutes. So once that has gone on for
    HOLD_AFTER_SECONDS the sender puts a small sealed item in the token's
    mailbox - ``{"hold": n}`` - and renews it every HOLD_EVERY_SECONDS (the
    receiver reads each as "still here"). Small offers are ready long before
    that and never send one. The real offer must come AFTER the last hold has
    been stored (a mailbox keeps the highest sequence number), hence settle()."""

    def __init__(self, ses, seed32: bytes, sk64: bytes, pub32: bytes, client=None):
        self._ses, self._seed32, self._sk64, self._pub32 = ses, seed32, sk64, pub32
        # Private mode (Tor): the hold goes down the control stream to the receiver
        # (core.p2p.tor_link.Client), not into a DHT mailbox.
        self._client = client
        self._started = time.monotonic()
        self._sent_at = 0.0
        self.n = 0
        self._inflight = False

    def tick(self) -> None:
        now = time.monotonic()
        if now - self._started < HOLD_AFTER_SECONDS or (
                self._sent_at and now - self._sent_at < HOLD_EVERY_SECONDS):
            return
        if self._client is not None:
            self.n += 1
            self._sent_at = now
            self._client.send(_secret.seal(self._seed32, json.dumps({"hold": self.n}).encode("utf-8")))
            return
        self._drain()
        self.n += 1
        try:
            blob = _secret.seal(self._seed32, json.dumps({"hold": self.n}).encode("utf-8"))
            self._ses.dht_put_mutable_item(self._sk64, self._pub32, blob, _DHT_SALT)
            self._inflight = True
        except Exception:
            logger.debug("P2P send: publishing a hold failed", exc_info=True)
        self._sent_at = now

    def _drain(self) -> None:
        try:
            for a in self._ses.pop_alerts():
                if type(a).__name__ == "dht_put_alert":
                    self._inflight = False
        except Exception:
            logger.debug("P2P send: reading alerts failed", exc_info=True)

    def cancel_notice(self) -> None:
        """The preparation was called off: tell the receiver (a sealed
        ``{"cancel": 1}``, written after the last hold is stored) and give it a
        few seconds to be stored, since the session goes with the caller."""
        if self._client is not None:
            self._client.send(_secret.seal(self._seed32, json.dumps({"cancel": 1}).encode("utf-8")), sticky=True)
            time.sleep(_PRIVATE_CANCEL_LINGER_SECONDS)       # (the stream is closed by the caller, after this)
            return
        self.settle()
        try:
            blob = _secret.seal(self._seed32, json.dumps({"cancel": 1}).encode("utf-8"))
            self._ses.dht_put_mutable_item(self._sk64, self._pub32, blob, _DHT_SALT)
        except Exception:
            logger.debug("P2P send: publishing the cancel notice failed", exc_info=True)
            return
        deadline = time.monotonic() + _CANCEL_SETTLE_SECONDS
        self._inflight = True
        self._drain()
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.2)
            self._drain()

    def settle(self) -> None:
        """Wait (a few seconds at most) for the last hold to be stored, so that
        the offer written next is the newest thing in the mailbox."""
        if not self.n or self._client is not None:
            return
        deadline = time.monotonic() + _HOLD_SETTLE_SECONDS
        self._drain()
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.2)
            self._drain()


def _fit_offer(fields: dict, magnet: str) -> bytes:
    """The mailbox payload for *fields* + *magnet*, trimmed to fit.

    A DHT item is capped at 1000 bytes in total, and names are free text:
    escaped as ASCII a single character can cost six bytes, and even as UTF-8
    a CJK title costs three. The text fields are cut (longest first) until the
    whole thing fits, rather than the publish being rejected by every node."""
    fields = dict(fields)
    if magnet:                          # (a TLS offer has none: see TransferOffer)
        fields["magnet"] = magnet
    text_keys = [k for k in ("filename", "game_name", "folder", "sender_username")
                 if isinstance(fields.get(k), str)]
    for k in text_keys:
        # A lone surrogate (a mangled file name) cannot be encoded as UTF-8.
        clean = fields[k].encode("utf-8", "replace").decode("utf-8")
        fields[k] = (_trim_label(clean, _MAX_LABEL_CHARS) if k == "filename"
                     else clean[:_MAX_LABEL_CHARS])
    while True:
        payload = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        if len(payload) <= _MAX_OFFER_BYTES:
            return payload
        longest = max(text_keys, key=lambda k: len(fields[k].encode("utf-8")))
        if not fields[longest]:
            raise P2pError("the offer is too large to publish")
        fields[longest] = fields[longest][:max(0, len(fields[longest]) // 2)]


def _seed_meta_torrent(lt, ses, seed32: bytes, staging_root: Path, payload: bytes, leaf: tuple):
    """The first torrent of a TLS transfer: one small file, *payload* (the real
    torrent's description), seeded under the same certificate authority. Returns
    (its handle, its own description without the authority's certificate)."""
    meta_root = staging_root / "meta"
    meta_root.mkdir()
    (meta_root / _META_NAME).write_bytes(payload)
    fs = lt.file_storage()
    fs.add_file(_META_NAME, len(payload))
    # Version 2: its description stays a few hundred bytes however big the payload
    # is (a merkle root, not a hash per piece) - it has to fit the offer.
    ct = lt.create_torrent(fs, flags=lt.create_torrent.v2_only)
    ct.set_root_cert(_tls.root_cert_pem(seed32))
    lt.set_piece_hashes(ct, str(meta_root))
    tor = ct.generate()
    info = lt.torrent_info(lt.bencode(tor))
    atp = lt.add_torrent_params()
    atp.ti = info
    atp.save_path = str(meta_root)
    atp.flags |= lt.torrent_flags.seed_mode | _hidden_flags(lt)
    handle = ses.add_torrent(atp)
    handle.set_ssl_certificate_buffer(leaf[0], leaf[1], _tls.dh_params_pem())
    description = dict(tor[b"info"])
    description.pop(b"ssl-cert")
    return handle, lt.bencode(description)


def _seed_and_offer(token_str: str, staging_root: Path, transfer_dir: Path,
                    fields: dict, progress=None, tls: Optional[bool] = None,
                    friend_key: Optional[bytes] = None) -> SendHandle:
    """Make a torrent of *transfer_dir*, seed it, and publish *fields* + its
    magnet link into the mailbox the receiver's token points to.

    Making the torrent hashes every piece, which takes as long as the offer is
    big: *progress* (``progress(pieces done, pieces in all)`` -> False to call
    it off, which raises P2pCancelled) is told as it goes, and a preparation
    that outlasts HOLD_AFTER_SECONDS keeps the receiver waiting (see _Hold).

    *tls* (default: the Settings choice, see tls_enabled): an SSL torrent
    (core.p2p.tls). A magnet link cannot start one, so the offer carries instead the
    description of a small first torrent, which in turn carries the real torrent's;
    both are seeded here."""
    lt = _require_libtorrent()
    seed32 = session_seed(token_str, friend_key, check_age=False)     # checked when the send began
    pub32, sk64 = _expanded_keys(seed32)
    # Private mode (Tor) has to be on at both ends: the token says which this is, and a
    # PC in private mode never goes the open way (its address would be shown), nor does
    # one that is not reach an onion address.
    private = token_is_private(token_str)
    if private != tor_enabled():
        raise P2pTorError("needs_private" if private else "not_private")
    use_tls = False if private else (tls_enabled() if tls is None else bool(tls))
    svc = None
    if private:
        svc = _tor.service()
        if not svc.available():
            raise P2pTorError("unavailable")
        svc.start()

    started = time.monotonic()
    if private:
        ses = _new_session(role="send")
    else:
        ses = _new_session(tls=True) if use_tls else _new_session()
    if use_tls and not ses.ssl_listen_port():
        raise P2pError("TLS cannot be used here (it needs sockets that listen: not behind a proxy)")
    # (reaching the receiver's onion service starts now, before a single piece is
    # hashed: by the time the offer is ready the connection is usually up)
    client = _link.Client(svc, _tor.identity_from_seed(seed32)[0]) if private else None
    done = False
    try:
        fs = lt.file_storage()
        lt.add_files(fs, str(transfer_dir))
        # No tracker: a public one would be told the info-hash (and our address).
        # A version-1 torrent: the default (hybrid, v1 and v2 in one) makes libtorrent
        # order the files two ways, and on Windows the two differ when one folder's name
        # is the beginning of another's (Slot1 and Slot10, say) - it then refuses the
        # torrent ("the v1 and v2 file metadata does not match") and such a folder could
        # not be sent at all. Nothing here uses the v2 half.
        ct = lt.create_torrent(fs, flags=lt.create_torrent.v1_only)
        ct.set_creator("SaveSync")
        if use_tls:
            ct.set_root_cert(_tls.root_cert_pem(seed32))
        hold = _Hold(ses, seed32, sk64, pub32, client=client)
        pieces = max(1, ct.num_pieces())

        def _piece(index) -> None:
            hold.tick()
            if progress is not None and progress(int(index) + 1, pieces) is False:
                raise P2pCancelled("cancelled")

        try:
            lt.set_piece_hashes(ct, str(staging_root), _piece)
        except P2pCancelled:
            hold.cancel_notice()            # (the receiver has been told to wait: tell it not to)
            raise
        hold.settle()
        tor = ct.generate()
        info = lt.torrent_info(lt.bencode(tor))

        atp = lt.add_torrent_params()
        atp.ti = info
        atp.save_path = str(staging_root)
        atp.flags |= lt.torrent_flags.seed_mode | _hidden_flags(lt)
        handle = ses.add_torrent(atp)
        meta_handle, magnet = None, ""
        if use_tls:
            leaf = _tls.leaf(seed32)
            handle.set_ssl_certificate_buffer(leaf[0], leaf[1], _tls.dh_params_pem())
            description = dict(tor[b"info"])
            description.pop(b"ssl-cert")
            meta_handle, first = _seed_meta_torrent(lt, ses, seed32, staging_root, lt.bencode(description), leaf)
            fields = dict(fields)
            fields.update({"tls": 1, "m1": b64encode(first).decode("ascii"), "h2": str(info.info_hash())})
        else:
            magnet = lt.make_magnet_uri(handle)

        # The offer (magnet and all) goes into the mailbox sealed under the token,
        # so only the person holding it can read it.
        sealed = _secret.seal(seed32, _fit_offer(fields, magnet))
        if private:
            # (down the control stream to the receiver's onion service: sent first on
            # every stream that is made, so a stream that drops and comes back brings it again)
            client.send(sealed, sticky=True)
            done = True
            return SendHandle(_session=ses, _handle=handle, _staging_root=staging_root,
                              _client=client, _seed32=seed32,
                              _contact_by=token_created_at(token_str) + TOKEN_TTL_SECONDS + _TOKEN_SKEW_SECONDS)
        _wait_for_proxy_dht(ses, started)
        ses.dht_put_mutable_item(sk64, pub32, sealed, _DHT_SALT)
        done = True
        return SendHandle(_session=ses, _handle=handle, _staging_root=staging_root,
                          _rv=_Rendezvous(ses, seed32, announce=("tls",) if use_tls else ("plain",),
                                          lookup="tls" if use_tls else "plain"),
                          _put=(sk64, pub32, sealed), _notice=(seed32, sk64, pub32),
                          _meta_handle=meta_handle)
    finally:
        if client is not None and not done:
            client.close()