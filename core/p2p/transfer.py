"""SaveSync - P2P save transfer over BitTorrent's public DHT.

Sends one backup archive directly from one SaveSync instance to another,
peer to peer, with no server either side has to run: the sender creates a
torrent for the file and seeds it, the receiver downloads it once they
accept — ordinary, heavily-used BitTorrent machinery, not something built
from scratch. The one unusual piece is how the receiver's app learns the
sender's magnet link at all: a DHT "mutable item" (BEP44) acts as a
one-time mailbox, addressed by a keypair the receiver generates fresh for
this transfer. Its 32-byte SEED — never the keys themselves — is what gets
handed to the sender as the token, short enough to copy/paste. The sender
derives the same keypair from it, signs the magnet link plus who they are,
and writes that into the mailbox; the receiver, already watching that same
address, reads it back. No salt is used — a fresh keypair per transfer
already makes the address unique, so there is nothing a salt would add.

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

Two small classes are the public surface:
- ReceiveSession — generate a token, poll for an incoming offer, accept or
  decline it, track the download, then finalize() it.
- send_backup — create a torrent for one file, seed it, and publish the
  offer under a token the receiver already generated.

What actually travels where is split in two, because BEP44 mutable items
are capped at 1000 bytes total — nowhere near enough for a full
core.backup.BackupEntry (save_paths, exe_path, save/content chains can add
up well past that for a game with several tracked folders):

- The DHT mailbox carries only what the receiver needs to show a decision
  BEFORE downloading anything — the magnet link, the sender's name/machine
  id, the game name, the filename and size. All short strings, always
  comfortably under the limit.
- The torrent itself carries a small ``manifest.json`` alongside the save
  archive, holding the sender's full BackupEntry (as a plain dict). Nothing
  caps a torrent's payload size, so the receiver only ever reads this
  AFTER accepting — by then the transfer is already happening and reading
  one more small file costs nothing extra.
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from base64 import b32decode, b32encode
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ~120 bits of entropy — plenty for an address that exists for one transfer
# and is thrown away right after, not a long-lived secret.
_TOKEN_BYTES = 15
_DHT_SALT = b""
# How often accept-side and sender-side re-issue their DHT get/put while
# waiting — frequent enough to feel responsive, rare enough not to hammer
# the network while a person is off copy-pasting the token to someone.
_REPOLL_SECONDS = 3.0


class P2pError(Exception):
    pass


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
    libtorrent's DHT signing expects — see module docstring."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.from_private_bytes(seed32)
    pub32 = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    h = bytearray(hashlib.sha512(seed32).digest())
    h[0] &= 248
    h[31] &= 63
    h[31] |= 64
    return pub32, bytes(h)


def generate_token() -> tuple:
    """(token_str, seed32) for a fresh, single-use transfer address.
    token_str is what gets handed to the other side; seed32 is what THIS
    side keeps to derive its own keys from."""
    token_bytes = os.urandom(_TOKEN_BYTES)
    token_str = b32encode(token_bytes).decode("ascii").rstrip("=")
    return token_str, hashlib.sha256(token_bytes).digest()


def seed_from_token(token_str: str) -> bytes:
    cleaned = "".join((token_str or "").split()).upper()
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        token_bytes = b32decode(padded)
    except Exception as e:
        raise P2pError("that token is not valid") from e
    if len(token_bytes) != _TOKEN_BYTES:
        raise P2pError("that token is not valid")
    return hashlib.sha256(token_bytes).digest()


def _new_session():
    lt = _require_libtorrent()
    settings = {
        "enable_dht": True,
        # Ephemeral port: this is a one-off transfer session, not a
        # long-lived seedbox with a port worth remembering across runs.
        "listen_interfaces": "0.0.0.0:0,[::]:0",
        "alert_mask": (lt.alert.category_t.error_notification
                      | lt.alert.category_t.status_notification
                      | lt.alert.category_t.dht_notification),
    }
    return lt.session(settings)


_MANIFEST_NAME = "manifest.json"


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


class ReceiveSession:
    """One "waiting to receive" session: a token, a DHT mailbox being
    watched under it, and — once accepted — the download itself.

    Call pump() periodically (a UI timer, roughly once a second) to drive
    both the DHT lookup and the torrent download; nothing here blocks.
    """

    def __init__(self):
        self.token, self._seed32 = generate_token()
        self._pub32, _sk64 = _expanded_keys(self._seed32)
        self._ses = _new_session()
        self._last_get_at = 0.0
        self.offer: Optional[TransferOffer] = None
        self._offer_raw: bytes = b""
        self._handle = None
        self._save_dir: Optional[Path] = None
        self.download_error: str = ""
        self.download_done = False
        self._closed = False

    def pump(self) -> None:
        if self._closed:
            return
        now = time.time()
        if self.offer is None and now - self._last_get_at > _REPOLL_SECONDS:
            self._last_get_at = now
            try:
                self._ses.dht_get_mutable_item(self._pub32, _DHT_SALT)
            except Exception:
                logger.debug("P2P receive: dht_get_mutable_item failed", exc_info=True)
        for a in self._ses.pop_alerts():
            self._on_alert(a)

    def _on_alert(self, a) -> None:
        name = type(a).__name__
        if name == "dht_mutable_item_alert" and self.offer is None:
            item = bytes(getattr(a, "item", b"") or b"")
            if not item or item == self._offer_raw:
                return
            self._offer_raw = item
            try:
                data = json.loads(item.decode("utf-8"))
                self.offer = TransferOffer(
                    magnet=str(data["magnet"]),
                    filename=_safe_filename(data.get("filename")),
                    size=int(data.get("size") or 0),
                    game_name=str(data.get("game_name") or ""),
                    sender_username=str(data.get("sender_username") or ""),
                    sender_machine_id=str(data.get("sender_machine_id") or ""),
                )
            except Exception:
                logger.debug("P2P receive: mailbox item did not parse", exc_info=True)
        elif name == "torrent_finished_alert":
            self.download_done = True
        elif name in ("torrent_error_alert", "file_error_alert"):
            self.download_error = str(getattr(a, "error", "") or a.message())

    def accept(self, save_dir: Path) -> None:
        """Start downloading the accepted offer's file into *save_dir*."""
        if self.offer is None:
            raise P2pError("no transfer offer to accept yet")
        lt = _require_libtorrent()
        self._save_dir = Path(save_dir)
        self._save_dir.mkdir(parents=True, exist_ok=True)
        atp = lt.parse_magnet_uri(self.offer.magnet)
        atp.save_path = str(self._save_dir)
        self._handle = self._ses.add_torrent(atp)

    def finalize(self) -> tuple:
        """(backup_entry dict, path to the downloaded save archive) — call
        once download_done is True. Reads the manifest that travelled
        alongside the archive inside the torrent (see module docstring);
        backup_entry is {} if the sender didn't include one (an older
        SaveSync build), in which case only the file itself is usable.
        """
        if self._handle is None or self._save_dir is None:
            raise P2pError("nothing has been downloaded yet")
        root_name = self._handle.status().name
        transfer_dir = self._save_dir / root_name
        manifest_path = transfer_dir / _MANIFEST_NAME
        backup_entry: dict = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest.get("backup_entry"), dict):
                    backup_entry = manifest["backup_entry"]
            except Exception:
                logger.debug("P2P receive: manifest did not parse", exc_info=True)
        save_path = transfer_dir / _safe_filename(self.offer.filename)
        try:
            save_path.resolve().relative_to(transfer_dir.resolve())
        except ValueError:
            raise P2pError("received filename escaped the transfer directory")
        return backup_entry, save_path

    def progress(self) -> Optional[dict]:
        """None before accept(); a dict of live download stats after."""
        if self._handle is None:
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
        try:
            if self._handle is not None:
                self._ses.remove_torrent(self._handle)
        except Exception:
            pass


@dataclass
class SendHandle:
    """A file being seeded out to whoever accepts it. put_confirmed is
    None until the DHT publish's own result comes back (see pump())."""
    _session: object
    _handle: object
    _staging_root: Optional[Path] = None
    put_confirmed: Optional[bool] = None

    def pump(self) -> None:
        for a in self._session.pop_alerts():
            if type(a).__name__ == "dht_put_alert" and self.put_confirmed is None:
                self.put_confirmed = bool(getattr(a, "num_success", 0) > 0)

    def progress(self) -> dict:
        st = self._handle.status()
        return {
            "num_peers": st.num_peers,
            "upload_rate": st.upload_rate,
            "all_time_upload": st.all_time_upload,
        }

    def stop(self) -> None:
        try:
            self._session.remove_torrent(self._handle)
        except Exception:
            pass
        if self._staging_root is not None:
            shutil.rmtree(self._staging_root, ignore_errors=True)


def send_backup(token_str: str, file_path, sender_username: str,
                sender_machine_id: str, game_name: str = "",
                backup_entry: Optional[dict] = None) -> SendHandle:
    """Create a torrent for *file_path*, start seeding it, and publish the
    offer into the DHT mailbox *token_str* (the RECEIVER's token) points to.

    *backup_entry* is the sender's own core.backup.BackupEntry, already
    turned into a plain dict (entry.to_dict()) by the caller — this module
    stays independent of core.backup and just carries it through unread. It
    travels inside the torrent, not the DHT mailbox (see module docstring
    for why), alongside a hardlink (falling back to a copy) of *file_path*
    rather than the original in place — a multi-file torrent needs every
    member under one shared root, and the original save folder is not that.

    Returns a handle to keep seeding from — poll its pump() for whether the
    DHT publish itself succeeded, and keep the handle alive (do not stop())
    until the transfer is known to be picked up: stopping it removes the
    only seed AND cleans up the staged copy.
    """
    lt = _require_libtorrent()
    seed32 = seed_from_token(token_str)
    pub32, sk64 = _expanded_keys(seed32)

    file_path = Path(file_path)
    if not file_path.is_file():
        raise P2pError(f"{file_path} is not a file")

    staging_root = Path(tempfile.mkdtemp(prefix="savesync_p2p_send_"))
    transfer_dir = staging_root / f"savesync_{uuid.uuid4().hex[:8]}"
    transfer_dir.mkdir(parents=True)
    staged_file = transfer_dir / file_path.name
    try:
        os.link(file_path, staged_file)
    except OSError:
        shutil.copy2(file_path, staged_file)
    (transfer_dir / _MANIFEST_NAME).write_text(
        json.dumps({"backup_entry": backup_entry or {}}), encoding="utf-8")

    ses = _new_session()
    fs = lt.file_storage()
    lt.add_files(fs, str(transfer_dir))
    ct = lt.create_torrent(fs)
    ct.add_tracker("udp://tracker.opentrackr.org:1337/announce")
    ct.set_creator("SaveSync")
    lt.set_piece_hashes(ct, str(staging_root))
    info = lt.torrent_info(ct.generate())

    atp = lt.add_torrent_params()
    atp.ti = info
    atp.save_path = str(staging_root)
    atp.flags |= lt.torrent_flags.seed_mode
    handle = ses.add_torrent(atp)
    magnet = lt.make_magnet_uri(handle)

    payload = json.dumps({
        "magnet": magnet,
        "filename": file_path.name,
        "size": file_path.stat().st_size,
        "game_name": game_name,
        "sender_username": sender_username,
        "sender_machine_id": sender_machine_id,
    }).encode("utf-8")
    ses.dht_put_mutable_item(sk64, pub32, payload, _DHT_SALT)

    return SendHandle(_session=ses, _handle=handle, _staging_root=staging_root)
