"""SaveSync - TLS for a transfer (optional: Settings > "Use TLS for transfers").

By default a transfer is protected by BitTorrent's own encryption, whose key rests
on the torrent's info-hash being secret (see core.p2p.transfer). TLS is the
stronger alternative that costs no copy of the files: libtorrent's "SSL torrents".
Every connection is a TLS connection, each side must show a certificate signed by
the transfer's own certificate authority, and a peer that is not speaking TLS is
refused outright - there is no downgrade to the other kind.

Nothing here is sent anywhere: the authority is DERIVED, on both PCs, from the
secret they already share (the receiver's token, which the offer is sealed under).

- The authority's key and certificate come out of the token, byte for byte the same
  on both sides (Ed25519 signs deterministically; the name, serial and dates are
  fixed). The sender puts that certificate in the torrent; the receiver, which knows
  what it must be, puts it back into the torrent description it is given.
- Each side makes a certificate of its own (a fresh key every time) and has it
  signed by the authority. Whoever can do that knows the token: the two of them,
  and nobody else.
- The certificates are made for the name "*": libtorrent checks that a peer's
  certificate names the torrent or is a wildcard, and the real check is the
  signature of the authority.
- TLS 1.3 key exchange is ephemeral, so a token that comes to light later opens
  nothing that was recorded before.

What TLS needs that the default does not (so it is a choice, not the default):
- The SSL sockets are not the one the DHT uses, so the address the DHT learns is
  not the SSL one. A connection can be made where a port is open (UPnP on, a
  forward, a public address) - not through a router that has to be punched
  through, which is how many pairs connect without it.
- It cannot be used behind the SOCKS5 proxy (uTP over SSL needs a listening socket),
  so a proxy that is switched on wins.
- A magnet link cannot start an SSL torrent. The receiver is given a short
  description of a small first torrent in the offer; that torrent carries the real
  one's description, and then the real transfer begins (see core.p2p.transfer).
"""
import datetime
from typing import Optional

from core.p2p import secret as _secret

# Fixed, so that the authority's certificate is the same on both PCs. Wide, so that
# no clock stands in the way (a certificate is checked against the date).
_NOT_BEFORE = datetime.datetime(2020, 1, 1)
_NOT_AFTER = datetime.datetime(2099, 12, 31)


# libtorrent wants Diffie-Hellman parameters whenever it is given a certificate
# (an empty string is refused, and stops the torrent with "[dh params]"). The
# 2048-bit group of RFC 3526 (no secret in it, and nothing slow to generate); the
# TLS 1.3 key exchange used between two SaveSyncs is elliptic-curve and does not
# touch it - it is there because libtorrent insists.
_DH_PRIME_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF")


def dh_params_pem() -> str:
    """The Diffie-Hellman parameters libtorrent is given with a certificate (PEM)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import dh
    params = dh.DHParameterNumbers(int(_DH_PRIME_HEX, 16), 2).parameters()
    return params.parameter_bytes(serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3).decode("ascii")


def available(lt) -> bool:
    """Does this libtorrent do SSL torrents?"""
    return hasattr(lt.torrent_handle, "set_ssl_certificate_buffer") and hasattr(lt.create_torrent, "set_root_cert")


def _name(common_name: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _authority(seed32: bytes):
    """(private key, certificate) of the transfer's authority - the same every time
    for the same *seed32*."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(_secret.derive(seed32, b"tls authority key", 32))
    cert = (x509.CertificateBuilder()
            .subject_name(_name("savesync transfer")).issuer_name(_name("savesync transfer"))
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, None))
    return key, cert


def root_cert_pem(seed32: bytes) -> str:
    """The authority's certificate as PEM text - what goes into the torrent."""
    from cryptography.hazmat.primitives import serialization
    return _authority(seed32)[1].public_bytes(serialization.Encoding.PEM).decode("ascii")


def leaf(seed32: bytes) -> tuple:
    """(certificate PEM, private key PEM) for one side of the transfer: a new key,
    signed by the authority (so only somebody who holds the token can make one)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    ca_key, ca_cert = _authority(seed32)
    key = Ed25519PrivateKey.generate()
    cert = (x509.CertificateBuilder()
            .subject_name(_name("*")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(2)
            .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("*")]), critical=False)
            .sign(ca_key, None))
    return (cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode("ascii"))


def with_ssl_sockets(listen_interfaces: str) -> str:
    """*listen_interfaces* (libtorrent's "host:port,host:port" list) with an SSL
    socket added beside each plain one: the same host, the next port up (the one
    below for the last port there is), or any free port where the plain one is
    "any" (0). An SSL socket is marked by an "s" after its port."""
    out = []
    for item in [x.strip() for x in listen_interfaces.split(",") if x.strip()]:
        out.append(item)
        host, _, port = item.rpartition(":")
        try:
            n = int(port)
        except ValueError:
            continue
        sp = 0 if n == 0 else (n + 1 if n < 65535 else n - 1)
        out.append(f"{host}:{sp}s")
    return ",".join(out)
