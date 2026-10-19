"""SaveSync - the secrets a P2P transfer is built on.

The receiver's token is the one thing the two people share, and it never
touches the network (the receiver hands it over by hand). Everything that has
to stay between them is derived from it:

- the key the offer in the DHT mailbox is sealed with, so the nodes that store
  that item see noise - not the sender's name, the file names or the magnet
  link (which holds the torrent's info-hash, the one thing a stranger would
  need to find the swarm);
- a *rendezvous hash*, the address where the two sides find each other in the
  DHT. It is NOT the info-hash (that is never announced anywhere) and nothing
  ties it to the torrent: a stranger sees that two machines looked a hash up,
  not what for.

Sealing is ChaCha20-Poly1305: a tampered or foreign item fails to open instead
of being half-believed. The mailbox key, the rendezvous hash and the keypair
the mailbox is signed with (see core.p2p.transfer) all come from the same
token but through different one-way derivations, so none reveals another.
"""
import os

_VERSION = b"\x01"
_NONCE_LEN = 12
_TAG_LEN = 16
# What sealing adds to a message: version byte + nonce + authentication tag.
OVERHEAD = len(_VERSION) + _NONCE_LEN + _TAG_LEN
_CONTEXT = b"savesync-p2p/1 "


class SealError(ValueError):
    """A sealed item that is not for this token, was altered, or is not one."""


def _derive(seed32: bytes, label: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None,
                info=_CONTEXT + label).derive(seed32)


def derive(secret: bytes, label: bytes, length: int = 32) -> bytes:
    """*length* bytes derived from *secret* for the purpose *label*: another
    label gives bytes that have nothing to do with these."""
    return _derive(secret, label, length)


def seal_with(key32: bytes, plain: bytes, aad: bytes) -> bytes:
    """*plain* sealed under *key32*; *aad* is authenticated, not stored (the
    opener must give the same). A fresh random nonce each time."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    nonce = os.urandom(_NONCE_LEN)
    return _VERSION + nonce + ChaCha20Poly1305(key32).encrypt(nonce, bytes(plain), _CONTEXT + aad)


def open_with(key32: bytes, blob: bytes, aad: bytes) -> bytes:
    """The message inside *blob*; SealError when it was not sealed under
    *key32* with this *aad* (or was altered, truncated, or is another version)."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    blob = bytes(blob or b"")
    if len(blob) < OVERHEAD or blob[:1] != _VERSION:
        raise SealError("not a sealed message")
    nonce = blob[1:1 + _NONCE_LEN]
    try:
        return ChaCha20Poly1305(key32).decrypt(nonce, blob[1 + _NONCE_LEN:], _CONTEXT + aad)
    except Exception as e:
        raise SealError("could not open the message") from e


# Everything left in a DHT mailbox is made this long before it is sealed, so a
# node that stores it cannot tell a short text from a long one, or a "hold" from
# an offer, by its size (a mailbox item may be 1000 bytes; this leaves room for the
# seal and the framing). It is JSON text, so the padding is trailing spaces, which
# any JSON reader ignores: an older SaveSync reads a padded item as it always did.
PAD_TO = 940


def pad(plain: bytes) -> bytes:
    """*plain* (JSON text) with trailing spaces up to PAD_TO bytes; unchanged
    when it is already that long."""
    plain = bytes(plain)
    return plain if len(plain) >= PAD_TO else plain + b" " * (PAD_TO - len(plain))


def seal(seed32: bytes, plain: bytes) -> bytes:
    """*plain* (JSON text, padded: see PAD_TO), sealed with the key derived from
    *seed32* (see transfer.seed_from_token). A fresh random nonce each time."""
    return seal_with(_derive(seed32, b"offer key", 32), pad(plain), b"offer")


def open_sealed(seed32: bytes, blob: bytes) -> bytes:
    """The message inside *blob*; SealError when it is not a sealed offer for
    this token (wrong key, altered, truncated, or from another version)."""
    return open_with(_derive(seed32, b"offer key", 32), blob, b"offer")


def signing_keys(seed32: bytes) -> tuple:
    """(public key 32B, private key 64B) in the exact byte layout
    libtorrent's DHT signing expects.

    The 64-byte "expanded" Ed25519 private key is NOT the libsodium/NaCl
    secret-key format (seed + public key) - it is RFC 8032's own expanded
    form, clamp(SHA512(seed)). Confirmed against libtorrent's own vendored
    ed25519 source (src/ed25519/keypair.cpp). Getting the layout wrong does
    not error: the write is simply rejected by every node that verifies it,
    which reads like "the network doesn't support this"."""
    import hashlib
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(seed32)
    pub32 = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    h = bytearray(hashlib.sha512(seed32).digest())
    h[0] &= 248
    h[31] &= 63
    h[31] |= 64
    return pub32, bytes(h)


def rendezvous_hash(seed32: bytes, label: bytes = b"rendezvous") -> bytes:
    """The 20 bytes (the size of a DHT key) both sides announce and look up. A
    TLS transfer meets at another one (label b"rendezvous tls"), since the port
    it is reached at is another."""
    return _derive(seed32, label, 20)
