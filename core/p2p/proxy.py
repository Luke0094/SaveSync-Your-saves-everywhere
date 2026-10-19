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
this PC, and sees that it runs P2P, when, and how much. The name of the DHT's
starting server is looked up by this PC's own resolver, not through the proxy.
"""
import json
import logging
import os
import re
import socket
import struct
import threading
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

    def signature(self) -> tuple:
        return (self.host, self.port, self.user, self.password)


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
    return Proxy(host, int(port), user, password)


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


# ── "does this proxy work?" - for the Test button ────────────────────────────

CHECK_CODES = ("ok", "unreachable", "timeout", "not_socks5", "auth_required", "auth_failed", "no_udp")


def _read(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise ConnectionError("closed")
        buf += d
    return buf


def check(px: Proxy, timeout: float = 6.0) -> str:
    """Ask the proxy what P2P needs of it: a SOCKS5 greeting (with the login if
    there is one) and a UDP association (the DHT and the transfers run on UDP).
    One of CHECK_CODES; nothing is sent beyond the proxy."""
    try:
        sock = socket.create_connection((px.host.strip("[]"), px.port), timeout=timeout)
    except socket.timeout:
        return "timeout"
    except OSError:
        return "unreachable"
    try:
        sock.settimeout(timeout)
        try:
            methods = b"\x00\x02" if px.user else b"\x00"
            sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
            ver, method = _read(sock, 2)
            if ver != 5:
                return "not_socks5"
            if method == 0xFF:
                return "auth_required" if not px.user else "auth_failed"
            if method == 2:
                if not px.user:
                    return "auth_required"
                u, p = px.user.encode("utf-8")[:255], px.password.encode("utf-8")[:255]
                sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                if _read(sock, 2)[1] != 0:
                    return "auth_failed"
            elif method != 0:
                return "not_socks5"
            # UDP ASSOCIATE, address and port left open (0.0.0.0:0)
            sock.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + struct.pack(">H", 0))
            head = _read(sock, 4)
            if head[0] != 5:
                return "not_socks5"
            if head[1] != 0:
                return "no_udp"
            return "ok"
        except socket.timeout:
            return "timeout"
        except (ConnectionError, OSError):
            return "not_socks5"
    finally:
        try:
            sock.close()
        except OSError:
            pass
