"""SaveSync - hiding this PC's address behind a SOCKS5 proxy (optional).

Peer to peer cannot hide an address by itself: the friend on the other end
connects to this PC, and the DHT nodes that keep the mailboxes see who asks them.
The only way out is to let something else be the one that is seen - a SOCKS5
proxy (many VPN services offer one). When it is switched on in Settings:

- EVERY P2P packet and connection goes through it - the DHT (mailboxes, the
  rendezvous), the transfers, both ways. libtorrent is told to use nothing else
  (``force_proxy``): if the proxy is down or refuses, nothing is sent at all,
  never "directly instead". A proxy that is switched on but not filled in blocks
  P2P the same way (ProxyError), it does not fall back to going direct.
- This PC stops listening and stops asking the router to open a port (UPnP / NAT-PMP):
  nobody can connect IN. The two PCs meet the way two PCs behind a router do,
  each reaching for the other's address as the DHT saw it - which is the
  proxy's. That needs a proxy that does SOCKS5 UDP (UDP ASSOCIATE) and gives a
  client one address for all its traffic; Tor's SOCKS port does not do UDP.
- The proxy's login is kept apart from config.json, encrypted for this PC.

What it does NOT hide: the proxy's operator (and the network up to it) still knows
this PC, and sees that it runs P2P, when, and how much. (The only name this PC still looks up
itself is the proxy's own address, if it was typed as a name: it has to be found to be reached. The
DHT's starting servers are looked up through the proxy - see "looking up names through the proxy".)
"""
import json
import logging
import os
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_FILE_MAGIC = b"SSPROXY1\n"
_HOST_OK = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")
DEFAULT_PORT = 1080


class ProxyError(Exception):
    """The proxy is switched on but cannot be used as it is set."""


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    user: str = ""
    password: str = ""
    # Settings: if the proxy will not carry the lookup of the DHT's starting servers, may this PC's own
    # DNS do it? Part of the signature: a session started without the option does not outlive it being
    # switched on (it would sit with no starting servers), nor the other way round.
    local_dns: bool = False

    def signature(self) -> tuple:
        return (self.host, self.port, self.user, self.password, self.local_dns)


def valid_host(host) -> bool:
    return isinstance(host, str) and bool(_HOST_OK.match(host.strip()))


def valid_port(port) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535


# ── where the setting and the login live ─────────────────────────────────────

def _config():
    from core.config_manager import get_config
    try:
        return get_config()
    except RuntimeError:
        # No application yet, so no settings to read: only in a script that uses
        # this module on its own. (Any other failure is NOT swallowed: P2P then
        # does not start, which is the safe way to fail.)
        return {}


def credentials_path() -> Path:
    from core.constants import USER_DATA_DIR
    return USER_DATA_DIR / "p2p_proxy.dat"


def _default_key() -> bytes:
    from core.credentials import _derive_encryption_key
    return _derive_encryption_key()


_cred_lock = threading.Lock()
_cred_cache: dict = {}          # path -> (stat key, (user, password))


def _stat_key(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_credentials(path: Optional[Path] = None,
                     key_fn: Optional[Callable[[], bytes]] = None) -> tuple:
    """(user, password), or ("", "") when there is no login or it cannot be read
    (the proxy then simply refuses - never anything sent without it)."""
    path = Path(path) if path is not None else credentials_path()
    stat = _stat_key(path)
    if stat is None:
        return "", ""
    with _cred_lock:
        cached = _cred_cache.get(str(path))
        if cached and cached[0] == stat:
            return cached[1]
    try:
        from core.credentials import decrypt_data
        raw = path.read_bytes()
        if not raw.startswith(_FILE_MAGIC):
            return "", ""
        doc = json.loads(decrypt_data(raw[len(_FILE_MAGIC):], (key_fn or _default_key)()).decode("utf-8"))
        got = (str(doc.get("u") or ""), str(doc.get("p") or ""))
    except Exception:
        logger.warning("P2P proxy: the saved login could not be read", exc_info=True)
        got = ("", "")              # (remembered like a good one: this is asked every second)
    with _cred_lock:
        _cred_cache[str(path)] = (stat, got)
    return got


def save_credentials(user: str, password: str, path: Optional[Path] = None,
                     key_fn: Optional[Callable[[], bytes]] = None) -> bool:
    """Keep the login, encrypted for this PC. False when it could not be (no
    key to encrypt with): it is never written as plain text. No login at all
    removes the file."""
    path = Path(path) if path is not None else credentials_path()
    user, password = (user or "").strip(), password or ""
    with _cred_lock:
        _cred_cache.pop(str(path), None)
    if not user and not password:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        from core.credentials import encrypt_data
        blob = _FILE_MAGIC + encrypt_data(
            json.dumps({"u": user, "p": password}).encode("utf-8"), (key_fn or _default_key)())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        return True
    except Exception:
        logger.warning("P2P proxy: the login could not be saved", exc_info=True)
        return False


def configured() -> Optional[Proxy]:
    """The proxy to use, None when it is switched off. ProxyError when it is
    switched on but cannot be used as set - that blocks P2P, it never means
    "go direct"."""
    cfg = _config()
    if not cfg.get("p2p_proxy", False):
        return None
    host = str(cfg.get("p2p_proxy_host", "") or "").strip()
    port = cfg.get("p2p_proxy_port", 0)
    if not valid_host(host) or not valid_port(port):
        raise ProxyError("the SOCKS5 proxy is switched on in Settings but its address is not set")
    user, password = load_credentials()
    return Proxy(host, int(port), user, password, local_dns=bool(cfg.get("p2p_proxy_local_dns", False)))


def signature():
    """Something that changes whenever what ``configured()`` says changes: None
    (off), the proxy's details, or "invalid". A session started under one value
    must not outlive another (see FriendNet)."""
    try:
        px = configured()
    except ProxyError:
        return "invalid"
    return None if px is None else px.signature()


# ── what libtorrent is told ──────────────────────────────────────────────────

def lt_settings(lt, px: Proxy) -> dict:
    """libtorrent's settings for sending everything through *px*."""
    pt = lt.proxy_type_t
    out = {
        "proxy_type": int(pt.socks5_pw if px.user else pt.socks5),
        "proxy_hostname": px.host.strip("[]"),
        "proxy_port": px.port,
        # Every kind of connection, and names are the proxy's to look up.
        "proxy_peer_connections": True,
        "proxy_hostnames": True,
        "proxy_tracker_connections": True,
        # Nothing at all outside the proxy: not for a moment, not if it is down.
        "force_proxy": True,
        # Nobody connects in: no listening, nothing asked of the router.
        "enable_upnp": False,
        "enable_natpmp": False,
        "enable_lsd": False,
        "listen_interfaces": "0.0.0.0:0,[::]:0",
    }
    if px.user:
        out["proxy_username"] = px.user
        out["proxy_password"] = px.password
    return out


# ── talking to the proxy ─────────────────────────────────────────────────────

CHECK_CODES = ("ok", "unreachable", "timeout", "not_socks5", "auth_required", "auth_failed", "no_udp", "no_dns",
               "dns_local")


class _Refused(Exception):
    """The proxy cannot be used as P2P needs it; ``code`` is one of CHECK_CODES."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _read(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise ConnectionError("closed")
        buf += d
    return buf


def _associate(px: Proxy, timeout: float):
    """A SOCKS5 greeting (with the login if there is one) and a UDP association. Returns the control
    socket - which has to stay open for as long as the relay is used - and the (host, port) of the UDP
    relay. _Refused with the reason otherwise."""
    try:
        sock = socket.create_connection((px.host.strip("[]"), px.port), timeout=timeout)
    except socket.timeout:
        raise _Refused("timeout")
    except OSError:
        raise _Refused("unreachable")
    try:
        sock.settimeout(timeout)
        methods = b"\x00\x02" if px.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _read(sock, 2)
        if ver != 5:
            raise _Refused("not_socks5")
        if method == 0xFF:
            raise _Refused("auth_required" if not px.user else "auth_failed")
        if method == 2:
            if not px.user:
                raise _Refused("auth_required")
            u, p = px.user.encode("utf-8")[:255], px.password.encode("utf-8")[:255]
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _read(sock, 2)[1] != 0:
                raise _Refused("auth_failed")
        elif method != 0:
            raise _Refused("not_socks5")
        # UDP ASSOCIATE, address and port left open (0.0.0.0:0)
        sock.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + struct.pack(">H", 0))
        head = _read(sock, 4)
        if head[0] != 5:
            raise _Refused("not_socks5")
        if head[1] != 0:
            raise _Refused("no_udp")
        atyp = head[3]
        if atyp == 1:
            host = socket.inet_ntoa(_read(sock, 4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _read(sock, 16))
        elif atyp == 3:
            host = _read(sock, _read(sock, 1)[0]).decode("ascii", "replace")
        else:
            raise _Refused("not_socks5")
        port = struct.unpack(">H", _read(sock, 2))[0]
        if host in ("0.0.0.0", "::", ""):
            host = px.host.strip("[]")          # (a proxy that does not say: the one we are talking to)
        return sock, (host, port)
    except _Refused:
        sock.close()
        raise
    except socket.timeout:
        sock.close()
        raise _Refused("timeout")
    except (ConnectionError, OSError):
        sock.close()
        raise _Refused("not_socks5")


# ── looking up names through the proxy ───────────────────────────────────────
#
# libtorrent looks up the DHT's starting servers by name with THIS PC's resolver, whatever it is told
# about the proxy (measured: a name that exists nowhere fails on this PC's own DNS and the proxy is never
# asked) - which tells the PC's DNS server, and whoever watches it, that this PC runs P2P. So behind a
# proxy it is not asked to: the names are looked up here, over the proxy's UDP relay, by a public DNS
# server that sees the proxy's address and not ours, and libtorrent is given addresses only.

DNS_SERVERS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")
_DNS_TIMEOUT = 3.0                      # for each public DNS server asked, over the relay
_BOOTSTRAP_TTL = 2 * 3600
_bootstrap_cache: dict = {}             # the proxy's details -> (when, "ip:port,ip:port")
_bootstrap_failed: set = set()          # the proxies whose last lookup of the starting servers found nothing
_bootstrap_gave_up: dict = {}           # the proxies a whole round of tries found nothing through -> when
_GAVE_UP_SECONDS = 3600                 # how long that is remembered (a success forgets it at once)
_cache_lock = threading.Lock()


def allow_local_dns() -> bool:
    """Settings: if the proxy will not carry the lookup, may this PC's own DNS do it (off by default)?
    That tells the PC's DNS server, and whoever watches it, that this PC runs P2P - nothing else."""
    try:
        return bool(_config().get("p2p_proxy_local_dns", False))
    except Exception:
        return False


def note_gave_up(px: Proxy, now: Optional[float] = None) -> None:
    """A whole round of tries to look the starting servers up over *px* found nothing: it is the proxy,
    not a passing glitch (see gave_up)."""
    with _cache_lock:
        _bootstrap_gave_up[px.signature()] = time.time() if now is None else now


def gave_up(px: Proxy, now: Optional[float] = None) -> bool:
    """Did a whole round of tries over *px* find nothing lately (and nothing found since)? Then, if this
    PC's DNS may stand in, it is not worth another round before it does."""
    now = time.time() if now is None else now
    with _cache_lock:
        when = _bootstrap_gave_up.get(px.signature())
    return when is not None and now - when < _GAVE_UP_SECONDS


def lookup_budget(names: int) -> float:
    """The longest, in seconds, one lookup of *names* names over the proxy can take (a SOCKS association,
    then every public DNS server in turn for each name, each given its time)."""
    return _DNS_TIMEOUT * (1 + len(DNS_SERVERS) * max(1, int(names)))


def bootstrap_failed(sig) -> bool:
    """Did the last lookup of the DHT's starting servers over the proxy *sig* (see signature()) find
    nothing - a proxy that carries UDP but not that? P2P cannot start behind it, and nothing is looked up
    on this PC instead, so it is shown (FriendNet's network line) rather than left silent."""
    with _cache_lock:
        return sig in _bootstrap_failed


def _dns_query(name: str, qid: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split("."))
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)


def _skip_name(data: bytes, i: int) -> int:
    """The offset after the (possibly compressed) name at *i*."""
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def _dns_answers(data: bytes, qid: int):
    """The IPv4 addresses a DNS response carries, [] for a name that is not there (NXDOMAIN, or no
    address record), None when the server could not say (SERVFAIL, REFUSED...: another may). ValueError
    for anything that is not the answer to that query."""
    try:
        rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
        if rid != qid or not flags & 0x8000:
            raise ValueError("not the answer")
        rcode = flags & 0x000F
        if rcode == 3:
            return []
        if rcode:
            return None
        i = 12
        for _ in range(qd):
            i = _skip_name(data, i) + 4
        out = []
        for _ in range(an):
            i = _skip_name(data, i)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            if rtype == 1 and rdlen == 4:
                out.append(socket.inet_ntoa(data[i:i + 4]))
            i += rdlen
        return out
    except (struct.error, IndexError) as e:
        raise ValueError("malformed") from e


def _relay_lookup(udp, relay, name: str, timeout: float, servers=DNS_SERVERS):
    """One name, asked of each public DNS server in turn over the relay: the addresses (possibly none,
    for a name that is not there), or None when nobody could say - one that does not answer, or answers
    that it cannot (SERVFAIL, REFUSED), is passed over for the next."""
    qid = int.from_bytes(os.urandom(2), "big")
    query = _dns_query(name, qid)
    for server in servers:
        try:
            udp.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(server) + struct.pack(">H", 53) + query, relay)
            end = time.monotonic() + timeout
            while True:
                udp.settimeout(max(0.05, end - time.monotonic()))
                data, _ = udp.recvfrom(4096)
                if len(data) < 10 or data[3] != 1:
                    continue
                try:
                    found = _dns_answers(data[10:], qid)
                except ValueError:
                    continue                    # (not ours: keep listening till the time is up)
                if found is None:
                    break                       # (this one cannot say: the next, at once)
                return found
        except (socket.timeout, OSError):
            continue
    return None


def _lookup_over(relay, names, timeout: float) -> dict:
    """``{name: [IPv4 addresses]}`` for the names found through *relay* (the UDP relay of an association
    that the caller keeps open)."""
    out: dict = {}
    udp = None
    try:
        fam = socket.getaddrinfo(relay[0], relay[1], type=socket.SOCK_DGRAM)[0]
        udp = socket.socket(fam[0], socket.SOCK_DGRAM)
        for name in names:
            found = _relay_lookup(udp, fam[4], name, timeout)
            if found:
                out[name] = found
    except OSError:
        logger.debug("P2P proxy: a name could not be looked up over the proxy", exc_info=True)
    finally:
        try:
            if udp is not None:
                udp.close()
        except OSError:
            pass
    return out


def lookup_via_proxy(px: Proxy, names, timeout: float = 3.0) -> dict:
    """``{name: [IPv4 addresses]}`` for the names that were found, looked up over the proxy's UDP relay
    and nowhere else. {} when the proxy cannot carry it (nothing is looked up on this PC instead)."""
    names = list(dict.fromkeys(names))
    if not names:
        return {}
    try:
        ctl, relay = _associate(px, timeout)
    except _Refused:
        return {}
    try:
        return _lookup_over(relay, names, timeout)
    finally:
        try:
            ctl.close()
        except OSError:
            pass


def _split_node(entry: str):
    host, _, port = entry.strip().rpartition(":")
    if not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


def _is_ip(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return True
    except OSError:
        return False


def bootstrap_nodes(px: Proxy, names: str, now: Optional[float] = None) -> str:
    """The DHT's starting servers (*names*: libtorrent's ``host:port,host:port``) as ``ip:port`` entries
    only, so that libtorrent has no name left to look up on this PC. Looked up over the proxy; kept for
    a while, and the last answer is kept past that if the proxy cannot answer now. "" when there is none."""
    now = time.time() if now is None else now
    key = px.signature()
    with _cache_lock:
        cached = _bootstrap_cache.get(key)
    if cached is not None and now - cached[0] < _BOOTSTRAP_TTL:
        return cached[1]
    wanted = [n for n in (_split_node(e) for e in names.split(",")) if n]
    named = list(dict.fromkeys(h for h, _p in wanted if not _is_ip(h)))     # (a name listed twice is one)
    found = lookup_via_proxy(px, named, timeout=_DNS_TIMEOUT)
    nodes = []
    for host, port in wanted:
        for ip in ([host] if _is_ip(host) else found.get(host, [])[:2]):
            nodes.append(f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}")
    result = ",".join(nodes)
    if result and len(found) == len(named):
        with _cache_lock:
            _bootstrap_cache[key] = (now, result)
            _bootstrap_failed.discard(key)
            _bootstrap_gave_up.pop(key, None)
        return result
    result = result or (cached[1] if cached is not None else "")
    with _cache_lock:
        if result:
            _bootstrap_failed.discard(key)
            _bootstrap_gave_up.pop(key, None)
        else:
            _bootstrap_failed.add(key)
    return result


def check(px: Proxy, timeout: float = 6.0, allow_local: bool = False) -> str:
    """Ask the proxy what P2P needs of it: a SOCKS5 greeting (with the login if there is one), a UDP
    association (the DHT and the transfers run on UDP) and a name looked up over it (the DHT's starting
    servers are found that way). One of CHECK_CODES - "no_dns" for a proxy that carries UDP but not that,
    or "dns_local" if so and *allow_local* (this PC's DNS will do it). Nothing is sent beyond the proxy."""
    try:
        sock, relay = _associate(px, timeout)
    except _Refused as e:
        return e.code
    try:
        found = _lookup_over(relay, ["dht.libtorrent.org"], min(timeout, _DNS_TIMEOUT))     # (the same association)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if found:
        return "ok"
    return "dns_local" if allow_local else "no_dns"
