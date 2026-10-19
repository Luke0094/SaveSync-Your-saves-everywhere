"""SaveSync - Tor, built in (optional): the address stays hidden from the other PC
and from the DHT.

Peer to peer cannot hide an address by itself: the friend on the other end connects
to this PC, and the DHT nodes that keep the mailboxes see who asks them (see
core.p2p.proxy for the way out with a SOCKS5 proxy of your own). With *private mode*
switched on (Settings, P2P) SaveSync carries a copy of Tor and runs it in the
background, and the two PCs meet INSIDE Tor instead:

- a PC that waits for something (a transfer's receiver, a friend) hosts an *onion
  service*: an address (``xxxxxxxx.onion``) that anyone who knows it can connect to
  through Tor, without learning where the PC is. Nothing connects in from the
  internet; no port is opened, UPnP is not asked for;
- the other side connects to that address through Tor's SOCKS port, which is also
  the only way anything of this PC reaches the network: libtorrent itself listens
  on 127.0.0.1 only, has no DHT, no UPnP, no uTP, and is bridged to Tor by small
  loopback sockets (core.p2p.tor_link). Both ends are hidden - neither side's
  address is ever seen by the other, by the relays, or by anyone watching the DHT
  (it is not used at all while private mode is on).

The onion addresses are not chosen: they come from a secret the two share (the
transfer token; a friendship's shared secret), so the other side can work the address
out by itself and nothing has to be published to find it. Only a PC that knows the
secret can open the connection, as the first thing said over it is sealed under it.

Tor itself is the ``tor`` program of the Tor Project (BSD licence), kept in
``vendor/tor`` (``tools/fetch_tor.py`` fetches and checks it) and packed with the
application. It is started when it is first needed and ends with the application
(``__OwningControllerProcess``: it goes by itself if SaveSync is killed). The first
start on a PC takes 15-60 seconds - Tor downloads the list of relays - every later
one about three seconds, since the list is kept in the application's data folder.

What it does NOT do: it does not make the transfer fast (a Tor circuit carries about
a fifth of a megabyte per second here; several are used side by side, see
core.p2p.tor_link), and it cannot carry UDP, which is why the DHT mailboxes are not
used in private mode. A PC that cannot start Tor (no binary, no network) does nothing:
never "directly instead".

Everything here is one shared service (``service()``), driven from threads; a test
can put a pretend one in its place (``set_service``).
"""
import atexit
import base64
import hashlib
import logging
import os
import platform
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core.p2p import secret as _secret

logger = logging.getLogger(__name__)

# SOCKS5 reply codes Tor gives for an onion service that cannot be reached.
SOCKS_ERRORS = {
    0x01: "general failure", 0x02: "not allowed", 0x03: "network unreachable",
    0x04: "host unreachable", 0x05: "connection refused", 0x06: "timed out",
    0xF0: "descriptor not found", 0xF1: "descriptor invalid", 0xF2: "introduction failed",
    0xF3: "rendezvous failed", 0xF4: "client authorization needed",  # i18n-ignore (tor diagnostics, log text)
    0xF5: "client authorization wrong", 0xF6: "bad address", 0xF7: "introduction timed out",  # i18n-ignore (tor diagnostics, log text)
}
ONION_PORT = 80                 # the one port every service of ours answers on
START_TIMEOUT = 240.0           # the first start downloads the relay list: 15-125 s measured
RESTART_DELAYS = (5.0, 15.0, 30.0, 60.0)


class TorError(Exception):
    """Tor could not do what was asked. ``code`` is the SOCKS5 reply code when the
    failure came from one (0 otherwise)."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class TorUnavailable(TorError):
    """There is no Tor to run (the program is not there, or it would not start)."""


# ── the setting ──────────────────────────────────────────────────────────────

def _config():
    from core.p2p import proxy as _proxy
    return _proxy._config()


def enabled() -> bool:
    """Is private mode (Tor) switched on in Settings?"""
    return bool(_config().get("p2p_tor", False))


# ── where the program is ─────────────────────────────────────────────────────

def _platform_dir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"{'windows' if system == 'windows' else 'macos' if system == 'darwin' else 'linux'}-{arch}"


def _exe_name() -> str:
    return "tor.exe" if platform.system() == "Windows" else "tor"


def find_tor() -> Optional[Path]:
    """The tor program to run: ``SAVESYNC_TOR`` if set (a test, a different build),
    else the one packed with the application, else ``vendor/tor`` of a source
    checkout. None when there is none."""
    override = os.environ.get("SAVESYNC_TOR")
    if override:
        p = Path(override)
        return p if p.is_file() else None
    name = _exe_name()
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "tor" / name)
    root = Path(__file__).resolve().parents[2]
    candidates.append(root / "vendor" / "tor" / _platform_dir() / name)
    return next((c for c in candidates if c.is_file()), None)


def data_dir() -> Path:
    from core.constants import USER_DATA_DIR
    return USER_DATA_DIR / "tor"


# ── onion addresses made from a secret ───────────────────────────────────────

def onion_address(pub32: bytes) -> str:
    """The v3 onion address (56 characters, no ".onion") of an Ed25519 public key."""
    checksum = hashlib.sha3_256(b".onion checksum" + pub32 + b"\x03").digest()[:2]
    return base64.b32encode(pub32 + checksum + b"\x03").decode("ascii").lower()


def identity_from_seed(seed32: bytes) -> tuple:
    """(address, key blob) of the onion service this secret stands for. The blob is
    what tor's ADD_ONION wants: the 64-byte expanded Ed25519 key, base64 (the same
    layout libtorrent's DHT signing uses, see core.p2p.secret.signing_keys). The
    same secret gives the same address on both sides of a friendship or a transfer
    - and a different label, a different, unrelated one."""
    pub32, sk64 = _secret.signing_keys(_secret.derive(seed32, b"onion service", 32))
    return onion_address(pub32), "ED25519-V3:" + base64.b64encode(sk64).decode("ascii")


# ── the control port ─────────────────────────────────────────────────────────

class _Control:
    """A bare tor control-port client: commands with their replies, and the events
    tor sends meanwhile handed to a callback (from the reading thread: keep it short)."""

    def __init__(self, port: int, cookie_path: Path, on_event: Callable[[str], None],
                 deadline: float = 30.0):
        end = time.monotonic() + deadline
        while True:
            try:
                self._s = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                if time.monotonic() > end:
                    raise TorError("tor's control port did not open")
                time.sleep(0.05)
        self._s.settimeout(None)
        while not cookie_path.exists():
            if time.monotonic() > end:
                raise TorError("tor did not write its control cookie")
            time.sleep(0.05)
        self._on_event = on_event
        self._replies: "queue.Queue" = queue.Queue()
        self._cmd_lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._read, name="tor-control", daemon=True).start()
        self.cmd("AUTHENTICATE " + cookie_path.read_bytes().hex())

    def _read(self) -> None:
        buf, block = b"", []
        try:
            while True:
                data = self._s.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    text = line.decode("utf-8", "replace")
                    if text.startswith("650"):
                        try:
                            self._on_event(text)
                        except Exception:
                            logger.debug("Tor: an event handler failed", exc_info=True)
                        continue
                    block.append(text)
                    if len(text) >= 4 and text[3] == " ":
                        self._replies.put(block)
                        block = []
        except OSError:
            pass
        self.closed = True
        self._replies.put(None)

    def cmd(self, text: str, timeout: float = 60.0) -> list:
        """The reply lines to one command; TorError for anything but "250"."""
        with self._cmd_lock:
            if self.closed:
                raise TorError("tor's control port is closed")
            try:
                self._s.sendall(text.encode("utf-8") + b"\r\n")
                block = self._replies.get(timeout=timeout)
            except (OSError, queue.Empty) as e:
                raise TorError(f"tor did not answer {text.split()[0]}") from e
        if block is None:
            raise TorError("tor's control port closed")
        if not block[-1].startswith("250"):
            raise TorError(f"{text.split()[0]}: {block[-1]}")
        return block

    def close(self) -> None:
        self.closed = True
        try:
            self._s.close()
        except OSError:
            pass


class _Onion:
    def __init__(self, address: str, key_blob: str, ports: dict):
        self.address, self.key_blob, self.ports = address, key_blob, dict(ports)
        self.added = False              # ADD_ONION has been answered on the current tor
        self.published = False          # a directory has taken the descriptor
        self.published_at = 0.0


# ── the service ──────────────────────────────────────────────────────────────

class TorService:
    """One running tor, started by ``start()`` and kept up (restarted if it dies)
    until ``stop()``. ``state``: "stopped", "starting" (``progress`` 0-100),
    "ready" or "failed" (``error`` says why; it is tried again)."""

    def __init__(self, directory: Optional[Path] = None, exe: Optional[Path] = None):
        self._dir = Path(directory) if directory is not None else None
        self._exe = Path(exe) if exe is not None else None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._ctl: Optional[_Control] = None
        self._onions: dict = {}
        self._stopping = False
        self.state = "stopped"
        self.progress = 0
        self.error = ""
        self.socks_port = 0
        self.started_at = 0.0
        self.ready_at = 0.0

    # life ------------------------------------------------------------------

    def available(self) -> bool:
        """Is there a tor program to run?"""
        exe = self._exe or find_tor()
        return exe is not None and Path(exe).is_file()

    def start(self) -> None:
        """Begin starting tor, unless that is under way or done. Does not wait."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stopping:
                return
            self._stopping = False
            self.state, self.progress, self.error = "starting", 0, ""
            self._thread = threading.Thread(target=self._run, name="tor-service", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            thread = self._thread
        self._wake.set()
        self._kill()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._lock:
            self.state, self.progress, self.socks_port = "stopped", 0, 0
            self._ready.clear()
            for o in self._onions.values():
                o.added = o.published = False

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self._ready.is_set()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout) and self.state == "ready"

    def status(self) -> dict:
        return {"state": self.state, "progress": self.progress, "error": self.error,
                "since": self.started_at}

    # the supervising thread --------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                self._start_once()
                attempt = 0
                self._watch()
            except TorUnavailable as e:
                self._fail(str(e))
                return                  # (nothing to retry: there is no program)
            except TorError as e:
                self._fail(str(e))
            except Exception as e:
                logger.warning("Tor: the service failed", exc_info=True)
                self._fail(str(e))
            finally:
                self._kill()
            if self._stopping:
                break
            delay = RESTART_DELAYS[min(attempt, len(RESTART_DELAYS) - 1)]
            attempt += 1
            self._wake.wait(delay)
            self._wake.clear()

    def _fail(self, why: str) -> None:
        with self._lock:
            if self._stopping:
                return
            self.state, self.error = "failed", why
            self._ready.clear()
            self.socks_port = 0
            for o in self._onions.values():
                o.added = o.published = False
        logger.warning("Tor: %s", why)

    def _kill(self) -> None:
        ctl, proc = self._ctl, self._proc
        self._ctl = self._proc = None
        if ctl is not None:
            ctl.close()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _directory(self) -> Path:
        return self._dir if self._dir is not None else data_dir()

    def _start_once(self) -> None:
        exe = self._exe or find_tor()
        if exe is None or not Path(exe).is_file():
            raise TorUnavailable("private mode needs the Tor program, which is not in this installation")
        d = self._directory()
        d.mkdir(parents=True, exist_ok=True)
        for stale in ("control.port", "control_auth_cookie"):
            try:
                (d / stale).unlink()
            except OSError:
                pass
        # Every path in it is relative to the folder tor is started in (the data folder): a
        # user name with accents or another alphabet in the full path would not be read right
        # by tor's own file handling on Windows, and none of that is in the file this way.
        torrc = d / "torrc"
        torrc.write_text("\n".join([
            "DataDirectory .",
            "SocksPort 127.0.0.1:auto",
            "ControlPort 127.0.0.1:auto",
            "ControlPortWriteToFile control.port",
            "CookieAuthentication 1",
            "CookieAuthFile control_auth_cookie",
            "ClientOnly 1",
            "AvoidDiskWrites 1",
            "Log notice file tor.log",
        ]) + "\n", encoding="utf-8")
        flags = 0x08000000 if platform.system() == "Windows" else 0     # CREATE_NO_WINDOW
        with self._lock:
            self.state, self.progress, self.error = "starting", 0, ""
            self.started_at = time.time()
            self._ready.clear()
        env = dict(os.environ)
        if platform.system() != "Windows":
            # (the libraries that come with the program sit beside it: libevent and the like)
            var = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
            env[var] = str(Path(exe).parent) + (os.pathsep + env[var] if env.get(var) else "")
        # (what it says before it has a log of its own - a setting it refuses - goes to a file)
        with open(d / "tor.stderr", "wb") as err:
            self._proc = subprocess.Popen(
                [str(exe), "-f", "torrc", "--__OwningControllerProcess", str(os.getpid())],
                stdout=err, stderr=err, creationflags=flags, cwd=str(d), env=env)
        deadline = time.monotonic() + 30
        port = 0
        while not port:
            if self._proc.poll() is not None:
                raise TorError("tor stopped at once: " + self._log_tail())
            try:
                line = (d / "control.port").read_text().strip()
                port = int(line.split(":")[-1])
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise TorError("tor did not open its control port")
                time.sleep(0.05)
        self._ctl = _Control(port, d / "control_auth_cookie", self._on_event)
        self._ctl.cmd("SETEVENTS STATUS_CLIENT HS_DESC")
        self._bootstrap_line(self._ctl.cmd("GETINFO status/bootstrap-phase")[0])  # i18n-ignore (tor diagnostics, log text)
        end = time.monotonic() + START_TIMEOUT
        while self.progress < 100:
            if self._stopping:
                return
            if self._proc.poll() is not None or self._ctl.closed:
                raise TorError("tor stopped while starting: " + self._log_tail())
            if time.monotonic() > end:
                raise TorError("tor did not finish starting in time")
            time.sleep(0.2)
        listeners = self._ctl.cmd("GETINFO net/listeners/socks")[0]
        try:
            self.socks_port = int(listeners.split('"')[1].split()[0].split(":")[-1])
        except (IndexError, ValueError):
            raise TorError("tor did not say where its SOCKS port is")
        with self._lock:
            self.state, self.ready_at = "ready", time.time()
            self._ready.set()
        logger.info("Tor: ready in %.1fs", self.ready_at - self.started_at)
        self._add_pending()

    def _log_tail(self) -> str:
        """The last complaint tor made (its log, or what it said on starting)."""
        for name in ("tor.log", "tor.stderr"):
            try:
                lines = (self._directory() / name).read_text(encoding="utf-8", errors="replace").strip().splitlines()
            except OSError:
                continue
            errs = [l for l in lines if "[err]" in l or "[warn]" in l] or lines
            if errs:
                return errs[-1][:200]
        return "no reason given"

    def _watch(self) -> None:
        """Keep up while tor runs: put onions that were asked for on it."""
        while not self._stopping:
            if self._proc is None or self._proc.poll() is not None:
                raise TorError("tor stopped: " + self._log_tail())
            if self._ctl is None or self._ctl.closed:
                raise TorError("tor's control port closed")
            self._add_pending()
            self._wake.wait(0.5)
            self._wake.clear()

    def _on_event(self, line: str) -> None:
        if "BOOTSTRAP" in line and "PROGRESS=" in line:
            self._bootstrap_line(line)
        elif line.startswith("650 HS_DESC UPLOADED "):
            address = line.split()[3]
            with self._lock:
                o = self._onions.get(address)
                if o is not None and not o.published:
                    o.published, o.published_at = True, time.time()

    def _bootstrap_line(self, line: str) -> None:
        try:
            self.progress = max(self.progress, int(line.split("PROGRESS=")[1].split()[0]))
        except (IndexError, ValueError):
            pass

    # onion services -----------------------------------------------------------

    def host(self, address: str, key_blob: str, ports: dict) -> None:
        """Serve *address* (see identity_from_seed): connections to its virtual port
        K go to 127.0.0.1:ports[K]. Asked now, done as soon as tor is up (and again
        if it is restarted); safe to call again."""
        with self._lock:
            old = self._onions.get(address)
            if old is not None and old.ports == dict(ports):
                return
            self._onions[address] = _Onion(address, key_blob, ports)
        if old is not None and old.added:
            self._del(address)
        self._wake.set()

    def unhost(self, address: str) -> None:
        with self._lock:
            o = self._onions.pop(address, None)
        if o is not None and o.added:
            self._del(address)

    def hosted(self, address: str) -> bool:
        """Has the service's descriptor been taken by a directory (it can be found)?"""
        with self._lock:
            o = self._onions.get(address)
            return bool(o and o.published)

    def _add_pending(self) -> None:
        ctl = self._ctl
        if ctl is None or ctl.closed or not self.ready:
            return
        with self._lock:
            todo = [o for o in self._onions.values() if not o.added]
        for o in todo:
            targets = " ".join(f"Port={v},127.0.0.1:{p}" for v, p in sorted(o.ports.items()))
            try:
                ctl.cmd(f"ADD_ONION {o.key_blob} {targets}")
            except TorError as e:
                if self._stopping or ctl.closed:
                    return                      # (going down: nothing to say)
                if "collision" in str(e).lower() or "550" in str(e):
                    try:
                        ctl.cmd(f"DEL_ONION {o.address}")
                        ctl.cmd(f"ADD_ONION {o.key_blob} {targets}")
                    except TorError:
                        logger.warning("Tor: could not host an onion service: %s", e)
                        continue
                else:
                    logger.warning("Tor: could not host an onion service: %s", e)
                    continue
            with self._lock:
                o.added = True

    def _del(self, address: str) -> None:
        ctl = self._ctl
        if ctl is None or ctl.closed:
            return
        try:
            ctl.cmd(f"DEL_ONION {address}", timeout=5.0)        # (asked from the window's thread: not for long)
        except TorError:
            pass

    # streams -------------------------------------------------------------------

    def open_stream(self, address: str, port: int = ONION_PORT, isolation: str = "",
                    timeout: float = 90.0) -> socket.socket:
        """A connected socket to the onion service *address* (with or without
        ".onion"), through tor. Streams with the same *isolation* share circuits, those
        with another get circuits of their own (more circuits carry more). TorError when
        it cannot be reached; ``code`` then says why. Blocks while the rendezvous is made
        (a few seconds; tens when the service is new)."""
        if not self.ready:
            raise TorError("tor is not ready")
        host = (address if address.endswith(".onion") else address + ".onion").encode("ascii")
        s = socket.create_connection(("127.0.0.1", self.socks_port), timeout=min(10.0, timeout))
        try:
            s.settimeout(timeout)
            user = (isolation or "x").encode("utf-8")[:255]
            s.sendall(b"\x05\x01\x02")
            if _recv(s, 2) != b"\x05\x02":
                raise TorError("tor's SOCKS port did not take the login")
            s.sendall(b"\x01" + bytes([len(user)]) + user + b"\x01x")
            if _recv(s, 2) != b"\x01\x00":
                raise TorError("tor's SOCKS port refused the login")
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", port))
            head = _recv(s, 4)
            if head[1] != 0:
                raise TorError("the onion service could not be reached: "
                               + SOCKS_ERRORS.get(head[1], f"error {head[1]:#x}"), head[1])
            kind = head[3]
            _recv(s, {1: 4, 4: 16}.get(kind, 0) + 2 if kind != 3 else _recv(s, 1)[0] + 2)
            s.settimeout(None)
            return s
        except BaseException:
            try:
                s.close()
            except OSError:
                pass
            raise


def _recv(s: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        try:
            chunk = s.recv(n - len(out))
        except socket.timeout as e:
            raise TorError("tor did not answer in time", 0x06) from e
        if not chunk:
            raise TorError("tor closed the connection")
        out += chunk
    return out


# ── the one shared service ───────────────────────────────────────────────────

_service: Optional[TorService] = None
_service_lock = threading.Lock()


def service() -> TorService:
    """The shared tor (made on first use, not yet started)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = TorService()
        return _service


def set_service(svc: Optional[TorService]) -> Optional[TorService]:
    """Put another in its place (a test's pretend one); returns the old."""
    global _service
    with _service_lock:
        old, _service = _service, svc
    return old


def ensure_started() -> TorService:
    svc = service()
    svc.start()
    return svc


def shutdown() -> None:
    with _service_lock:
        svc = _service
    if svc is not None:
        try:
            svc.stop()
        except Exception:
            logger.debug("Tor: stopping failed", exc_info=True)


atexit.register(shutdown)
