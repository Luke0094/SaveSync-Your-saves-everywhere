"""SaveSync - friends: people you can hand a transfer token to directly.

A friend is a long-lived public key. Each person has one X25519 identity
(created the first time it is needed, kept on this machine only); the
"friend code" you give someone is its public half plus a short check so a typo
is caught. Both people add each other's code.

What two friends share is derived from their two keys (X25519, then HKDF - see
core.p2p.secret), and from that, for each DIRECTION of the friendship:

- a mailbox - a keypair for a BitTorrent DHT "mutable item" (BEP44), written by
  the sender and read by the recipient. It is not either person's identity key:
  to anyone else it is one more random-looking key, unlinked to either of them,
  so the DHT sees no names, no contact list, nobody to follow - and it is a new
  one every EPOCH_SECONDS (see "mailboxes that change" below), so it cannot be
  followed for long either;
- a sealing key (ChaCha20-Poly1305), so what the mailbox holds is noise to the
  nodes that store it. Only the two friends can derive it, which is also what
  tells the recipient the message is from that friend and nobody else.

(In private mode - core.p2p.tor - there are no mailboxes: the same sealed slot goes down a
direct link over Tor, see core.p2p.tor_friends, under a key of its own and for good.)

A mailbox item is at most 1000 bytes and holds only the LATEST value, so it
carries a small list of recent messages (a "hello" with the sender's name, the
tokens sent in the last minutes, and text messages); each has an id so the
recipient takes every message once. See core.p2p.friend_net for the part that
talks to the DHT.

TEXT messages must not get lost, which a one-slot mailbox does not promise by
itself, so they are numbered and acknowledged:

- the sender numbers them per friend (an "epoch" - random, made with the first
  one - and 1, 2, 3...) and KEEPS each in its outbox, and in the mailbox, until
  the friend says it has it;
- the friend's own mailbox carries that acknowledgement ("received up to n of
  epoch e") next to whatever else it holds, which also tells the sender it is
  "delivered", not merely stored in the DHT;
- if the window is full - too many still unacknowledged to fit the mailbox -
  a new message is REFUSED rather than an old one dropped (pack_slot never
  drops a text), and they all stay on the sender's PC while the friend is away;
- both sides keep the conversation in the friend file; what is received is
  plain text, cleaned (no control characters). Any formatting is read by the
  receiving PC from the marks in that text (core.p2p.markup), never sent as
  markup.

An ATTACHMENT is offered in the same numbered stream - a text whose "f" says
what it is (a name, how many files, how big) - so it is as reliable as a text;
the files themselves never travel in a mailbox. When the friend accepts, their
SaveSync makes a transfer token (core.p2p.transfer) and answers with it (a
token message whose "re" is the offer's id), and mine sends to that token with
the ordinary sealed transfer. Either side can call it off: a "cancel" message
names the offer ("re") and says whether it is the sender WITHDRAWING it or the
receiver STOPPING an attempt ("k"); like a token it is not worth keeping and
goes first when the mailbox is crowded. What is refused, and how the arrival is checked
against the offer, is core.p2p.attachments.

Everything that is kept - the identity and the friend list - lives in one file
(FriendStore), encrypted with the same machine-bound key the app's cloud
credentials use, and is never part of a config export. Because of that binding
the file cannot simply be copied to another PC: a LINK file (export_link /
parse_link) carries the identity and the list under a password the person
chooses, and the other PC adopts it.

A separate, optional LOCK password does two things. It closes the list to whoever
sits down at an unlocked PC - and it encrypts the file with a key made from it.
The machine-bound key above is only as private as this PC's user account: any
program that runs as the same user can derive it and read the file. So with a lock
password the file is sealed under a random data key, and that key is kept only
wrapped by the password (scrypt, 128 MiB): nothing on this PC, and no program
running as its user, can open the file without it. Locking forgets the key and
everything decrypted (FriendStore.seal); the file stays as it is on disk. Since the
password is the only way in, a forgotten one is answered by FriendStore.wipe(),
which deletes the list for good - there is no recovery, and none is meant.
"""
import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable, Optional

from core.p2p import secret as _secret

logger = logging.getLogger(__name__)

_CHECK_BYTES = 3
_FILE_MAGIC = b"SSFRIENDS1\n"            # no lock password: sealed under this PC's own key
_FILE_MAGIC_PW = b"SSFRIENDS2\n"         # with one: sealed under a data key that only the password unwraps
_LINK_MAGIC = b"SSLINK1\n"
# Passwords are stretched with scrypt (about 0.1 s, 32 MB): fine for a person
# typing one, and a real cost to someone trying a list of them against a file.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
# The lock password protects the file itself, offline, so it is stretched harder
# (about a third of a second and 128 MiB per try) and has to be longer than when it
# only closed a window.
_LOCK_SCRYPT = (2 ** 17, 8, 1)
_LOCK_SCRYPT_MAX = (2 ** 20, 16, 4)        # what a file may ask for (a damaged one must not ask for the moon)
MIN_LOCK_PASSWORD = 8
MIN_LINK_PASSWORD = 8
# What goes into a mailbox item before it is sealed: the item may be 1000 bytes
# bencoded, and sealing adds _secret.OVERHEAD.
MAX_SLOT_PLAIN = 900
_MAX_NAME_CHARS = 40
# Tokens older than this are dropped from the mailbox (they have run out anyway).
TOKEN_KEEP_SECONDS = 15 * 60
_SEEN_KEEP = 64
_KINDS = ("hello", "token", "text", "cancel")
# A text message: at most this many bytes (UTF-8) - what a slot has room for.
MAX_TEXT_BYTES = 400
# How much of a conversation is kept per friend.
MAX_CHAT = 300
# Room always left in my mailbox for one token: a text that would crowd it out
# is refused instead.
TOKEN_RESERVE = 170


class FriendError(Exception):
    """A friend code that cannot be used, or a friend that cannot be found."""


# ── identity and friend codes ────────────────────────────────────────────

def new_identity() -> bytes:
    """A fresh private key (32 random bytes). Never leaves this machine."""
    return os.urandom(32)


def public_key(priv32: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    return X25519PrivateKey.from_private_bytes(priv32).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def friend_id(pub32: bytes) -> str:
    """The stable name of a friend inside the app (their key, in base32)."""
    return base64.b32encode(pub32).decode("ascii").rstrip("=")


def pub_from_id(fid: str) -> bytes:
    try:
        raw = base64.b32decode(fid + "=" * (-len(fid) % 8))
    except Exception as e:
        raise FriendError("not a friend") from e
    if len(raw) != 32:
        raise FriendError("not a friend")
    return raw


def fingerprint(pub32: bytes) -> str:
    """Eight characters that tell two keys apart at a glance ("1A2B-3C4D")."""
    h = hashlib.sha256(b"savesync-friend-fingerprint" + pub32).hexdigest().upper()
    return f"{h[:4]}-{h[4:8]}"


def _code_check(pub32: bytes) -> bytes:
    return hashlib.sha256(b"savesync-friend-code" + pub32).digest()[:_CHECK_BYTES]


def encode_code(pub32: bytes) -> str:
    """What you give a friend: the key and a check, in groups of eight."""
    raw = base64.b32encode(pub32 + _code_check(pub32)).decode("ascii").rstrip("=")
    return "-".join(raw[i:i + 8] for i in range(0, len(raw), 8))


def decode_code(text: str) -> bytes:
    """The public key inside a friend code; FriendError for anything else
    (a typo, a token, a half-copied code)."""
    cleaned = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except Exception as e:
        raise FriendError("not a friend code") from e
    if len(raw) != 32 + _CHECK_BYTES:
        raise FriendError("not a friend code")
    pub32, check = raw[:32], raw[32:]
    if check != _code_check(pub32):
        raise FriendError("not a friend code")
    return pub32


def shared_secret(priv32: bytes, pub32: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    try:
        s = X25519PrivateKey.from_private_bytes(priv32).exchange(
            X25519PublicKey.from_public_bytes(pub32))
    except ValueError as e:
        raise FriendError("not a usable friend code") from e
    if s == bytes(32):
        raise FriendError("not a usable friend code")
    return s


# ── one direction of a friendship ────────────────────────────────────────

@dataclass(frozen=True)
class Channel:
    """What the sender leaves for the recipient: the DHT key it is written
    under (``mailbox_pub``/``mailbox_sk``, the latter in libtorrent's layout)
    and the key it is sealed with. Both friends derive the same one."""
    mailbox_pub: bytes
    mailbox_sk: bytes
    key: bytes
    aad: bytes

    def seal(self, plain: bytes) -> bytes:
        """*plain* (JSON text) padded to the size every mailbox item has (see
        secret.PAD_TO), then sealed."""
        return _secret.seal_with(self.key, _secret.pad(plain), self.aad)

    def open(self, blob: bytes) -> bytes:
        return _secret.open_with(self.key, blob, self.aad)


def channel(shared: bytes, sender_pub: bytes, recipient_pub: bytes, epoch: int) -> Channel:
    info = sender_pub + recipient_pub + int(epoch).to_bytes(8, "big", signed=True)
    seed = _secret.derive(shared, b"friends mailbox " + info, 32)
    pub, sk = _secret.signing_keys(seed)
    return Channel(pub, sk, _secret.derive(shared, b"friends seal " + info, 32),
                   b"friends " + info)


# ── mailboxes that change ────────────────────────────────────────────────
#
# A mailbox that never changed would be one more fixed name in the DHT: the nodes
# that store it, or just see it asked for, could recognise it day after day and tell
# which two addresses keep meeting at it. So every friendship's mailboxes are
# re-derived every EPOCH_SECONDS: a new key, a new place in the DHT, a new sealing
# key, nothing in common with the last but the two friends' shared secret.
#
# The two sides agree on which epoch it is from the clock alone, so a little
# care is needed at the edges:
# - the epochs of one friendship begin at an offset taken from the shared secret
#   (not at the same moment for everybody: that would be a burst of writes, all
#   at once, from every SaveSync there is);
# - I write only under the epoch my clock says. A friend whose clock differs by
#   up to CLOCK_SKEW_SECONDS reads the epoch before for that long after the edge,
#   and the one after for that long before it, so nothing is missed;
# - an item stays in the DHT for up to two hours after it was written, and a friend
#   who was away when I wrote it still finds it then: the epoch before is read for
#   that long too (the same reach a fixed mailbox always had).
# Whatever is waiting is written again under the new epoch when it comes, and a
# message that is read twice is taken once (ids, numbers).
EPOCH_SECONDS = 6 * 3600
CLOCK_SKEW_SECONDS = 10 * 60
ITEM_LIFETIME_SECONDS = 2 * 3600 + 10 * 60
# The "epoch" of the direct link (private mode): not one that a clock can ever give.
LINK_EPOCH = -(2 ** 40)


def epoch_offset(shared: bytes) -> int:
    """Where this friendship's epochs begin, in seconds: the same for both friends."""
    return int.from_bytes(_secret.derive(shared, b"friends epoch offset", 4), "big") % EPOCH_SECONDS


def epoch_of(now: float, offset: int = 0) -> int:
    return int((now - offset) // EPOCH_SECONDS)


def epochs_to_read(now: float, offset: int = 0) -> list:
    """The epochs whose mailbox to look at now: this one, the one before while an
    item written under it can still be alive (or a slow clock still writes under
    it), and the next one just before it begins (a fast clock already does)."""
    e = epoch_of(now, offset)
    into = (now - offset) - e * EPOCH_SECONDS
    out = [e]
    if into < ITEM_LIFETIME_SECONDS + CLOCK_SKEW_SECONDS:
        out.append(e - 1)
    if EPOCH_SECONDS - into < CLOCK_SKEW_SECONDS:
        out.append(e + 1)
    return out


class Mailboxes:
    """The two mailboxes of one friendship, whichever epoch is asked for."""

    def __init__(self, my_priv: bytes, friend_pub: bytes):
        self._me = public_key(my_priv)
        self._friend = friend_pub
        self._shared = shared_secret(my_priv, friend_pub)   # (FriendError for a key that cannot be used)
        self.offset = epoch_offset(self._shared)
        self._out: dict = {}
        self._inc: dict = {}

    def epoch(self, now: float) -> int:
        return epoch_of(now, self.offset)

    def read_epochs(self, now: float) -> list:
        return epochs_to_read(now, self.offset)

    def out(self, epoch: int) -> Channel:
        """What I leave for the friend under *epoch*."""
        ch = self._out.get(epoch)
        if ch is None:
            ch = self._out[epoch] = channel(self._shared, self._me, self._friend, epoch)
        return ch

    def inc(self, epoch: int) -> Channel:
        """What the friend leaves for me under *epoch*."""
        ch = self._inc.get(epoch)
        if ch is None:
            ch = self._inc[epoch] = channel(self._shared, self._friend, self._me, epoch)
        return ch

    def link_out(self) -> Channel:
        """What I send the friend on the direct link of private mode (core.p2p.tor):
        sealed like a mailbox item, but under a key of its own and for good - nothing is
        stored anywhere, so there is no epoch to change."""
        ch = self._out.get(LINK_EPOCH)
        if ch is None:
            ch = self._out[LINK_EPOCH] = channel(self._shared, self._me, self._friend, LINK_EPOCH)
        return ch

    def link_in(self) -> Channel:
        """What the friend sends me on the direct link."""
        ch = self._inc.get(LINK_EPOCH)
        if ch is None:
            ch = self._inc[LINK_EPOCH] = channel(self._shared, self._friend, self._me, LINK_EPOCH)
        return ch

    def onion_seed(self, mine: bool) -> bytes:
        """The secret the onion address of the direct link comes from: one for each of
        the two ends, the same on both PCs - so each can work out the other's address
        by itself, and nobody else can (see core.p2p.tor.identity_from_seed)."""
        owner = self._me if mine else self._friend
        return _secret.derive(self._shared, b"friends onion " + owner, 32)

    def incoming_with_key(self, key: bytes) -> Optional[Channel]:
        """The incoming mailbox (of an epoch asked for lately) that has this DHT key."""
        return next((c for c in self._inc.values() if c.mailbox_pub == key), None)

    def outgoing_with_key(self, key: bytes) -> Optional[Channel]:
        return next((c for c in self._out.values() if c.mailbox_pub == key), None)

    def forget_before(self, epoch: int) -> None:
        """Let go of the channels of epochs long gone (keys are kept in memory only
        as long as they can matter)."""
        for cache in (self._out, self._inc):
            for e in [e for e in cache if e < epoch - 3 and e != LINK_EPOCH]:
                del cache[e]


def channels(my_priv: bytes, friend_pub: bytes, epoch: Optional[int] = None,
             now: Optional[float] = None) -> tuple:
    """(outgoing, incoming) of *epoch* - the one *now* (the present, if not
    given) falls in, if that is not given: what I leave for the friend, and what
    they leave for me."""
    mb = Mailboxes(my_priv, friend_pub)
    e = mb.epoch(time.time() if now is None else now) if epoch is None else epoch
    return mb.out(e), mb.inc(e)


# ── messages ─────────────────────────────────────────────────────────────

def _new_id() -> str:
    return os.urandom(8).hex()


def _clean_name(name) -> str:
    """A name as it may be kept and shown: one line, no control or direction
    characters, and no angle brackets - a name is somebody else's text that ends
    up in labels and tooltips, and ``<b>`` or ``<img ...>`` in one must not be
    taken for markup by any of them."""
    import unicodedata
    s = "".join(ch for ch in str(name or "")
                if ch not in "<>" and ch not in _BIDI_CONTROLS and unicodedata.category(ch) not in ("Cc", "Cs"))
    return " ".join(s.split())[:_MAX_NAME_CHARS]


def make_hello(name: str, now: Optional[float] = None) -> dict:
    return {"id": _new_id(), "t": "hello", "name": _clean_name(name),
            "at": int(time.time() if now is None else now)}


def make_token(token: str, ttl: float, name: str, now: Optional[float] = None,
               reply_to: str = "") -> dict:
    """A token for the friend to use, with the seconds it has left. *reply_to*
    is the id of the attachment offer it answers ("Accept"), if it does."""
    msg = {"id": _new_id(), "t": "token", "token": str(token), "ttl": max(0, int(ttl)),
           "name": _clean_name(name), "at": int(time.time() if now is None else now)}
    if reply_to:
        msg["re"] = str(reply_to)
    return msg


_TEXT_ID = re.compile(r"^[0-9a-f]{1,12}\.[0-9]{1,9}$")
CANCEL_KINDS = ("withdraw", "stop")
# Bidirectional overrides can make a line read differently from what it says.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean_text(body) -> str:
    """A message as it will be sent or shown: line breaks normalised, control
    and direction-override characters gone, no run of blank lines, trimmed."""
    import unicodedata
    s = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s
                if ch == "\n" or (ch not in _BIDI_CONTROLS
                                  and unicodedata.category(ch) not in ("Cc", "Cs")))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def text_bytes(body: str) -> int:
    return len(str(body).encode("utf-8", "replace"))


def truncate_bytes(body: str, limit: int) -> str:
    """*body* cut to *limit* UTF-8 bytes without splitting a character."""
    raw = str(body).encode("utf-8", "replace")
    return str(body) if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore")


def make_cancel(reply_to: str, kind: str, name: str, now: Optional[float] = None) -> dict:
    """A friend is told an attachment offer is off: *kind* "withdraw" (I took my
    offer back, or stopped sending it) or "stop" (I stopped receiving it)."""
    return {"id": _new_id(), "t": "cancel", "re": str(reply_to), "k": str(kind),
            "name": _clean_name(name), "at": int(time.time() if now is None else now)}


def clean_file_info(raw) -> Optional[dict]:
    """The description of an attachment offer - ``{"l": name, "c": how many
    files, "s": total bytes}`` - as it will be sent or shown, or None when it
    is not a usable one. The name is only a label (the files themselves are
    named by the transfer, and checked against this when they arrive)."""
    from core.p2p import attachments as _a
    if not isinstance(raw, dict):
        return None
    label, count, size = raw.get("l"), raw.get("c"), raw.get("s")
    if (not isinstance(label, str) or isinstance(count, bool) or not isinstance(count, int)
            or isinstance(size, bool) or not isinstance(size, int)
            or not 1 <= count <= _a.MAX_FILES or not 0 <= size <= 2 ** 50):
        return None
    label = _a.clean_label(label)
    return {"l": label, "c": count, "s": size} if label else None


def file_fallback(info: dict) -> str:
    """The words an attachment offer carries as its text, so that an older
    SaveSync (which does not know attachments) still shows something sensible."""
    from core.p2p import attachments as _a
    more = f" ({info['c']} files)" if info["c"] > 1 else ""
    return truncate_bytes(f"📎 {info['l']}{more} · {_a.human_size(info['s'])}", MAX_TEXT_BYTES)


def make_text(epoch: str, n: int, body: str, now: Optional[float] = None,
              file: Optional[dict] = None) -> dict:
    """Text message number *n* of the numbering *epoch* (its id says both). With
    *file* it is an attachment offer: the text is what an older version shows."""
    msg = {"id": f"{epoch}.{n}", "t": "text", "n": int(n), "b": body,
           "at": int(time.time() if now is None else now)}
    if file is not None:
        msg["f"] = {"l": file["l"], "c": file["c"], "s": file["s"]}
    return msg


def payload(msgs: list, ack: Optional[dict] = None) -> bytes:
    """The mailbox payload for *msgs* exactly as given (and the
    acknowledgement *ack* of what I have received, if any)."""
    doc: dict = {"v": 1, "m": list(msgs)}
    if ack:
        doc["a"] = ack
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pack_slot(msgs: list, ack: Optional[dict] = None) -> bytes:
    """The mailbox payload for *msgs*, trimmed to fit: the oldest tokens go
    first, then the hello. A text is NEVER dropped - the caller keeps them to
    what fits (see payload), because dropping one would lose it for good."""
    msgs = list(msgs)
    while True:
        out = payload(msgs, ack)
        if len(out) <= MAX_SLOT_PLAIN:
            return out
        victim = next((i for i, m in enumerate(msgs) if m.get("t") in ("token", "cancel")), None)
        if victim is None:
            victim = next((i for i, m in enumerate(msgs) if m.get("t") == "hello"), None)
        if victim is None:
            return out
        del msgs[victim]


def _parse_ack(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    e, n = raw.get("e"), raw.get("n")
    if (isinstance(e, str) and re.fullmatch(r"[0-9a-f]{1,12}", e)
            and isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 10 ** 9):
        return {"e": e, "n": n}
    return None


def unpack_slot(plain: bytes) -> list:
    """The messages in a mailbox payload (see parse_slot)."""
    return parse_slot(plain)[0]


def parse_slot(plain: bytes) -> tuple:
    """(messages, acknowledgement) in a mailbox payload, each message reduced
    to the fields we use. Anything malformed is skipped rather than believed;
    a text is cleaned and cut to MAX_TEXT_BYTES."""
    try:
        data = json.loads(bytes(plain).decode("utf-8"))
        raw = data["m"]
    except Exception:
        return [], None
    ack = _parse_ack(data.get("a")) if isinstance(data, dict) else None
    out = []
    if not isinstance(raw, list):
        return out, ack
    for m in raw[:30]:
        if not isinstance(m, dict):
            continue
        kind, mid = m.get("t"), m.get("id")
        if kind not in _KINDS or not isinstance(mid, str) or not (0 < len(mid) <= 32):
            continue
        item = {"id": mid, "t": kind, "name": _clean_name(m.get("name")),
                "at": int(m["at"]) if isinstance(m.get("at"), (int, float)) else 0}
        if kind == "token":
            if not isinstance(m.get("token"), str) or len(m["token"]) > 64:
                continue
            ttl = m.get("ttl")
            item["token"] = m["token"]
            item["ttl"] = int(ttl) if isinstance(ttl, (int, float)) and ttl >= 0 else 0
            if isinstance(m.get("re"), str) and _TEXT_ID.fullmatch(m["re"]):
                item["re"] = m["re"]            # the attachment offer this answers
        elif kind == "cancel":
            if (not isinstance(m.get("re"), str) or not _TEXT_ID.fullmatch(m["re"])
                    or m.get("k") not in CANCEL_KINDS):
                continue
            item["re"], item["k"] = m["re"], m["k"]
        elif kind == "text":
            n, body = m.get("n"), m.get("b")
            if (not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10 ** 9
                    or not isinstance(body, str) or not _TEXT_ID.fullmatch(mid)
                    or int(mid.rpartition(".")[2]) != n):
                continue
            body = truncate_bytes(clean_text(body), MAX_TEXT_BYTES)
            info = clean_file_info(m.get("f")) if "f" in m else None
            if info is not None:
                item["f"] = info
                body = body or file_fallback(info)
            if not body:
                continue
            item["n"], item["b"] = n, body
        out.append(item)
    return out, ack


# ── the file ─────────────────────────────────────────────────────────────

@dataclass
class Friend:
    pub: bytes
    alias: str = ""             # what I call them
    name: str = ""              # what they call themselves (from their hello)
    added: float = 0.0
    last_seen: float = 0.0      # the last time something of theirs was opened
    seen: list = field(default_factory=list)      # message ids already handled
    # The conversation (see ChatEntry in FriendStore): what was said, both ways.
    chat: list = field(default_factory=list)
    # My numbering of texts to them, and the ones they have not acknowledged.
    tx_epoch: str = ""
    tx_next: int = 1
    outbox: list = field(default_factory=list)
    # How far I have got through THEIR numbering.
    rx_epoch: str = ""
    rx_n: int = 0
    # "Don't show again": their messages are not announced. Kept here, in the sealed
    # file - it used to be a "friend:<their key>" line in config.json, which spelled
    # out who the friends are to anything that read the settings.
    silenced: bool = False

    @property
    def id(self) -> str:
        return friend_id(self.pub)

    @property
    def display(self) -> str:
        return self.alias or self.name or fingerprint(self.pub)

    @property
    def connected(self) -> bool:
        """They have added me too: something of theirs has been opened."""
        return self.last_seen > 0


# ── passwords ────────────────────────────────────────────────────────────

def _scrypt(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R,
                  p=_SCRYPT_P).derive(password.encode("utf-8"))


def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _unb32(text: str) -> bytes:
    return base64.b32decode(text + "=" * (-len(text) % 8))


def new_lock(password: str, dek: bytes) -> dict:
    """The header that keeps *dek* (the key the file is sealed under) behind
    *password*: the scrypt settings, a salt, and the key wrapped by what they make.
    Not the password, and nothing that tells it apart from a wrong one but opening
    the wrap (a wrong password fails there, authenticated)."""
    if len(password or "") < MIN_LOCK_PASSWORD:
        raise FriendError("password too short")
    n, r, p = _LOCK_SCRYPT
    salt = os.urandom(16)
    kek = _lock_key(password, salt, n, r, p)
    return {"v": 2, "kdf": "scrypt", "n": n, "r": r, "p": p, "salt": _b32(salt),
            "wrap": _b32(_secret.seal_with(kek, bytes(dek), b"friends dek" + salt))}


def _lock_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _valid_lock(lock) -> bool:
    try:
        return (isinstance(lock, dict) and lock.get("v") == 2 and lock.get("kdf") == "scrypt"
                and all(isinstance(lock.get(k), int) and not isinstance(lock.get(k), bool) and 1 <= lock[k] <= m
                        for k, m in zip(("n", "r", "p"), _LOCK_SCRYPT_MAX))
                and len(_unb32(lock["salt"])) == 16 and len(_unb32(lock["wrap"])) > _secret.OVERHEAD)
    except Exception:
        return False


def open_lock(lock: Optional[dict], password: str) -> Optional[bytes]:
    """The data key *lock* keeps, or None for a wrong password (or an empty one)."""
    if not password or not _valid_lock(lock):
        return None
    try:
        salt = _unb32(lock["salt"])
        kek = _lock_key(password, salt, lock["n"], lock["r"], lock["p"])
        return _secret.open_with(kek, _unb32(lock["wrap"]), b"friends dek" + salt)
    except Exception:
        return None


def check_lock(lock: Optional[dict], password: str) -> bool:
    return open_lock(lock, password) is not None


def export_link(store: "FriendStore", password: str) -> bytes:
    """The identity and the friend list as a file another PC can adopt, sealed
    under *password* (FriendError if it is too short)."""
    if len(password or "") < MIN_LINK_PASSWORD:
        raise FriendError("password too short")
    doc = {"v": 1, "identity": _b32(store.priv),
           "friends": [_friend_to_dict(f, local=False) for f in store.friends]}
    salt = os.urandom(16)
    sealed = _secret.seal_with(_scrypt(password, salt), json.dumps(doc).encode("utf-8"),
                               b"friends link")
    return _LINK_MAGIC + salt + sealed


def parse_link(blob: bytes, password: str) -> dict:
    """The document inside a link file. FriendError for a wrong password, a
    damaged file, or something that is not a link file (the two cases cannot
    be told apart, on purpose)."""
    blob = bytes(blob or b"")
    if not blob.startswith(_LINK_MAGIC) or len(blob) < len(_LINK_MAGIC) + 16 + _secret.OVERHEAD:
        raise FriendError("not a link file")
    salt = blob[len(_LINK_MAGIC):len(_LINK_MAGIC) + 16]
    try:
        doc = json.loads(_secret.open_with(_scrypt(password or "", salt),
                                           blob[len(_LINK_MAGIC) + 16:], b"friends link"))
        if len(_unb32(doc["identity"])) != 32:
            raise ValueError("identity")
        for d in doc.get("friends", []):
            pub_from_id(d["id"])
        return doc
    except _secret.SealError as e:
        raise FriendError("wrong password or damaged file") from e
    except Exception as e:
        raise FriendError("wrong password or damaged file") from e


# Where an attachment is: the offer made / received ("offered"), the friend has
# accepted and the files are on their way ("waiting"/"receiving"/"sending"),
# and how it ended.
FILE_STATES = ("offered", "waiting", "receiving", "sending", "saved", "sent", "failed",
               "declined", "cancelled", "expired", "blocked")
# What belongs to THIS PC only (where a file is, where one was put): it does not
# travel in a link file.
_LOCAL_FILE_KEYS = ("fp", "sd", "sf")


def _friend_to_dict(f: "Friend", local: bool = True) -> dict:
    chat = f.chat[-MAX_CHAT:]
    if not local:
        chat = [{k: v for k, v in e.items() if k not in _LOCAL_FILE_KEYS} for e in chat]
    return {"id": f.id, "alias": f.alias, "name": f.name, "added": f.added,
            "last_seen": f.last_seen, "seen": f.seen[-_SEEN_KEEP:],
            "chat": chat, "tx": {"e": f.tx_epoch, "n": f.tx_next},
            "outbox": f.outbox, "rx": {"e": f.rx_epoch, "n": f.rx_n}, "sl": bool(f.silenced)}


def _num(x, default=0):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _file_paths(raw) -> list:
    """The ``[path, relative path, size]`` of each file of an offer of mine, as
    far as they are well formed."""
    out = []
    for p in (raw or [])[:1000] if isinstance(raw, list) else []:
        if (isinstance(p, (list, tuple)) and len(p) == 3 and isinstance(p[0], str)
                and isinstance(p[1], str) and isinstance(p[2], int) and not isinstance(p[2], bool)):
            out.append([p[0][:600], p[1][:240], p[2]])
    return out


def _chat_entry_from_dict(e) -> Optional[dict]:
    if not (isinstance(e, dict) and e.get("dir") in ("in", "out", "sys")
            and e.get("kind") in ("text", "gap", "file") and isinstance(e.get("id"), str)):
        return None
    entry = {"id": e["id"][:40], "dir": e["dir"], "kind": e["kind"],
             "b": str(e.get("b") or "")[:2000], "ts": float(_num(e.get("ts"))),
             "n": int(_num(e.get("n"))), "read": bool(e.get("read", True)),
             "state": e.get("state") if e.get("state") in ("pending", "sent", "delivered") else "",
             "missed": int(_num(e.get("missed")))}
    if e["kind"] == "file":
        info = clean_file_info(e.get("f"))
        if info is None or e["dir"] == "sys":
            return None
        state = e.get("fs") if e.get("fs") in FILE_STATES else "failed"
        entry.update(f=info, fs=state, fw=str(e.get("fw") or "")[:40], fp=_file_paths(e.get("fp")),
                     sd=str(e.get("sd") or "")[:600],
                     sf=[str(x)[:240] for x in (e.get("sf") or [])[:50]] if isinstance(e.get("sf"), list) else [])
        if e["dir"] == "out" and state in ("offered", "waiting", "sending") and not entry["fp"]:
            entry["fs"] = "expired"         # the files it was made of are not on this PC
        if e["dir"] == "in":
            entry["fp"] = []
        if state in ("waiting", "receiving", "sending"):
            entry["fs"] = "offered" if entry["fs"] != "expired" else "expired"
    return entry


def _friend_from_dict(d: dict) -> "Friend":
    """A friend from a stored dict; whatever is missing or malformed takes its
    default (older files have no conversation at all)."""
    chat = []
    for e in (d.get("chat") or [])[-MAX_CHAT:]:
        entry = _chat_entry_from_dict(e)
        if entry is not None:
            chat.append(entry)
    outbox = []
    for o in (d.get("outbox") or [])[:50]:
        if (isinstance(o, dict) and isinstance(o.get("id"), str) and _TEXT_ID.fullmatch(o["id"])
                and isinstance(o.get("b"), str) and isinstance(o.get("n"), int)):
            wire = {"id": o["id"], "t": "text", "n": o["n"], "b": o["b"], "at": int(_num(o.get("at")))}
            info = clean_file_info(o.get("f")) if "f" in o else None
            if info is not None:
                wire["f"] = info
            outbox.append(wire)
    tx, rx = d.get("tx") or {}, d.get("rx") or {}
    return Friend(
        pub=pub_from_id(d["id"]), alias=_clean_name(d.get("alias")), name=_clean_name(d.get("name")),
        added=float(d.get("added") or 0), last_seen=float(d.get("last_seen") or 0),
        seen=[str(x) for x in (d.get("seen") or [])][-_SEEN_KEEP:], chat=chat,
        tx_epoch=str(tx.get("e") or "") if isinstance(tx, dict) else "",
        tx_next=max(1, int(_num(tx.get("n"), 1))) if isinstance(tx, dict) else 1,
        outbox=outbox,
        rx_epoch=str(rx.get("e") or "") if isinstance(rx, dict) else "",
        rx_n=max(0, int(_num(rx.get("n")))) if isinstance(rx, dict) else 0,
        silenced=bool(d.get("sl")))


def default_path() -> Path:
    from core.constants import USER_DATA_DIR
    return USER_DATA_DIR / "p2p_friends.dat"


def _default_key() -> bytes:
    from core.credentials import _derive_encryption_key
    return _derive_encryption_key()


def _lock_bytes(lock: dict) -> bytes:
    """The lock header as written (and as the file's seal is bound to it)."""
    return json.dumps(lock, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _split_password_file(data: bytes) -> tuple:
    """(lock header, sealed body) of a file sealed under the lock password; ValueError
    for anything that is not one."""
    head = len(_FILE_MAGIC_PW)
    if not data.startswith(_FILE_MAGIC_PW) or len(data) < head + 4:
        raise ValueError("not a sealed friend file")
    n = int.from_bytes(data[head:head + 4], "big")
    if not 0 < n <= 4096 or len(data) < head + 4 + n + _secret.OVERHEAD:
        raise ValueError("damaged")
    header = json.loads(data[head + 4:head + 4 + n].decode("utf-8"))
    if not _valid_lock(header):
        raise ValueError("damaged header")
    return header, data[head + 4 + n:]


class FriendStore:
    """The identity and the friend list, on disk. Not thread-safe: the GUI
    thread owns it. ``key_fn`` gives the 32-byte key the file is encrypted
    with (default: the credential store's machine-bound key); if there is none
    the file is written as plain JSON, which is no worse than config.json.

    Two kinds of file. Without a lock password: sealed under that machine key.
    With one: sealed under a random data key (``_dek``) that the file keeps wrapped
    by the password (``lock``, see new_lock) - the machine key is not involved at
    all, so the file opens with the password alone, on any PC, and not without it.

    A store that has a lock password starts SEALED: it knows only that, and holds
    nothing - no identity, no friends. ``unseal(password)`` opens it; ``seal()``
    saves and forgets everything again. While sealed nothing is ever written (and a
    new identity is never made: that would overwrite the file)."""

    def __init__(self, path: Optional[Path] = None,
                 key_fn: Optional[Callable[[], bytes]] = None):
        self.path = Path(path) if path is not None else default_path()
        self._key_fn = key_fn if key_fn is not None else _default_key
        self._priv: Optional[bytes] = None
        self.friends: list = []
        self.lock: Optional[dict] = None        # see new_lock; None = no lock password
        self._dek: Optional[bytes] = None       # the key the file is sealed under (with a lock password)
        self._sealed = False
        self.dirty = False
        self.reset_reason = ""
        self._load()

    # identity -----------------------------------------------------------
    @property
    def sealed(self) -> bool:
        """The list is closed: its file is there, protected by the lock password,
        and nothing of it is in memory."""
        return self._sealed

    @property
    def priv(self) -> bytes:
        if self._sealed:
            raise FriendError("the friend list is locked")
        if self._priv is None:
            self._priv = new_identity()
            self.dirty = True
            self.save()
        return self._priv

    @property
    def pub(self) -> bytes:
        return public_key(self.priv)

    @property
    def code(self) -> str:
        return encode_code(self.pub)

    # lock ---------------------------------------------------------------
    @property
    def has_lock_password(self) -> bool:
        return self.lock is not None

    def set_lock_password(self, password: str, current: str = "") -> None:
        """Set the lock password - or change it, which needs the current one. The
        file is sealed under it from now on. FriendError: too short, wrong current
        password, or the list is closed."""
        if self._sealed:
            raise FriendError("the friend list is locked")
        if self.lock is not None and not self.check_lock_password(current):
            raise FriendError("wrong password")
        _ = self.priv                           # (an identity to protect)
        dek = self._dek or os.urandom(32)
        lock = new_lock(password, dek)          # (FriendError when too short)
        self.lock, self._dek = lock, dek
        self.dirty = True
        self.save()

    def check_lock_password(self, password: str) -> bool:
        return check_lock(self.lock, password)

    def clear_lock_password(self, current: str) -> None:
        """Take the lock password off: the file goes back to being sealed under
        this PC's own key. Needs the current password (and an open list)."""
        if self._sealed:
            raise FriendError("the friend list is locked")
        if self.lock is not None and not self.check_lock_password(current):
            raise FriendError("wrong password")
        self.lock, self._dek = None, None
        self.dirty = True
        self.save()

    def unseal(self, password: str) -> bool:
        """Open a sealed list with the lock password; False for a wrong one. A
        file that cannot be read once its key is open (damaged) is set aside and
        replaced by a new, empty list, with ``reset_reason`` saying so."""
        if not self._sealed:
            return self.check_lock_password(password)
        dek = open_lock(self.lock, password)
        if dek is None:
            return False
        try:
            data = self.path.read_bytes()
            header, body = _split_password_file(data)
            if header != self.lock:
                raise ValueError("the file changed under us")
            doc = json.loads(_secret.open_with(dek, body, b"friends file" + _lock_bytes(header)).decode("utf-8"))
            priv = _unb32(doc["identity"])
            if len(priv) != 32:
                raise ValueError("identity")
            friends = [_friend_from_dict(d) for d in doc.get("friends", [])]
        except Exception:
            logger.warning("P2P friends: the sealed friend list could not be read", exc_info=True)
            self._set_aside("unreadable")
            self.lock, self._dek, self._sealed = None, None, False
            self.reset_reason = "unreadable"
            return True
        self._priv, self.friends, self._dek, self._sealed = priv, friends, dek, False
        self.dirty = False
        return True

    def seal(self) -> None:
        """Close the list: save what is unsaved, then forget the key and
        everything that was decrypted (the file on disk is already sealed). The
        memory is let go of, not wiped - Python cannot promise more; this keeps the
        list from a program that reads the file, not from one that reads this
        process. FriendError (and nothing forgotten) when there is no lock password
        or the list could not be saved."""
        if self.lock is None:
            raise FriendError("no lock password")
        if self._sealed:
            return
        if self._priv is not None and self.dirty:
            self.save()
            if self.dirty:
                raise FriendError("could not save the friend list")
        self._priv, self.friends, self._dek, self._sealed = None, [], None, True
        self.dirty = False

    def set_silenced(self, fid: str, on: bool = True) -> None:
        f = self.get(fid)
        if f is not None and f.silenced != bool(on):
            f.silenced = bool(on)
            self.dirty = True
            self.save()

    # linking another PC -------------------------------------------------
    def adopt(self, doc: dict) -> None:
        """Take over the identity and friends of a link file (parse_link). What
        was here is kept next to the file, not thrown away."""
        if self._sealed:
            raise FriendError("the friend list is locked")
        priv = _unb32(doc["identity"])
        friends = [_friend_from_dict(d) for d in doc.get("friends", [])]
        self._set_aside("replaced")
        self._priv, self.friends = priv, friends
        self.dirty = True
        self.save()

    def retire(self) -> None:
        """Forget the identity and the friends here (kept next to the file): a
        new identity is made the next time one is needed. For the PC a link
        file was made on, once the person has moved to the other one."""
        if self._sealed:
            raise FriendError("the friend list is locked")
        self._set_aside("retired")
        self._priv, self.friends, self.lock, self._dek = None, [], None, None

    def wipe(self) -> tuple:
        """Delete the friend list for good - the one thing to do when the lock
        password is forgotten, since the lock is what keeps other people at this
        PC out of it. The file goes, and so does every copy set aside next to it
        (what retire / adopt / an unreadable file leave); in memory, the identity,
        the friends, their conversations and the lock password are gone, and a new
        identity (a new friend code) is made the next time one is needed.

        Returns (files deleted, [names that could not be]). FriendError when the
        list's own file cannot be deleted - nothing is cleared then, so what is
        on screen stays what is on disk. Files are deleted, not shredded: on a
        modern disk nothing a program does can promise the bytes are gone (they
        are encrypted for this PC, which is what keeps them from anyone else)."""
        deleted, failed = 0, []
        if self.path.exists():
            try:
                self.path.unlink()
                deleted += 1
            except OSError as e:
                raise FriendError("could not delete the friend list") from e
        # (the temp file of a save that was cut short, and the copies set aside)
        try:
            extras = sorted(self.path.parent.glob(glob_escape(self.path.name) + ".*"))
        except OSError:
            extras = []
        for p in extras:
            try:
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError:
                failed.append(p.name)
        self._priv, self.friends, self.lock, self._dek = None, [], None, None
        self._sealed = False
        self.dirty = False
        self.reset_reason = ""
        return deleted, failed

    def _set_aside(self, why: str) -> None:
        if not self.path.exists():
            return
        try:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.{why}-{int(time.time())}"))
        except OSError:
            logger.warning("P2P friends: could not set the old friend list aside", exc_info=True)

    # friends ------------------------------------------------------------
    def get(self, fid: str) -> Optional[Friend]:
        return next((f for f in self.friends if f.id == fid), None)

    def add(self, code: str, alias: str = "") -> Friend:
        pub = decode_code(code)
        if pub == self.pub:
            raise FriendError("that is your own code")
        if self.get(friend_id(pub)) is not None:
            raise FriendError("already a friend")
        shared_secret(self.priv, pub)       # refuses a key that cannot be used
        f = Friend(pub=pub, alias=_clean_name(alias), added=time.time())
        self.friends.append(f)
        self.dirty = True
        self.save()
        return f

    def remove(self, fid: str) -> None:
        self.friends = [f for f in self.friends if f.id != fid]
        self.dirty = True
        self.save()

    def set_alias(self, fid: str, alias: str) -> None:
        f = self.get(fid)
        if f is not None:
            f.alias = _clean_name(alias)
            self.dirty = True
            self.save()

    def note_seen(self, fid: str, name: str = "", now: Optional[float] = None) -> None:
        f = self.get(fid)
        if f is None:
            return
        f.last_seen = time.time() if now is None else now
        if name and name != f.name:
            f.name = _clean_name(name)
        self.dirty = True

    # the conversation ----------------------------------------------------
    # Entries are dicts: {"id", "dir": "in"|"out"|"sys", "kind": "text"|"gap",
    # "b": the text, "ts": when it was recorded here, "n": its number, "read",
    # "state": for "out": "pending" (not stored in the DHT yet) | "sent"
    # (stored) | "delivered" (the friend acknowledged it), "missed": for a gap}.
    def chat(self, fid: str) -> list:
        f = self.get(fid)
        return list(f.chat) if f is not None else []

    def _chat_add(self, f: "Friend", entry: dict) -> None:
        f.chat.append(entry)
        del f.chat[:-MAX_CHAT]
        self.dirty = True

    def queue_text(self, fid: str, body: str, now: Optional[float] = None, *,
                   file: Optional[dict] = None, paths: Optional[list] = None) -> dict:
        """Number a text for the friend and keep it (outbox and conversation)
        until they acknowledge it. Returns the message as it goes on the wire.
        FriendError when empty or too long. Whether it FITS the mailbox is the
        sender's business (core.p2p.friend_net.FriendNet.text_room).

        With *file* (see clean_file_info) it is an attachment OFFER instead:
        *paths* is ``[(path, relative path, size)]`` of the files on this PC,
        kept here (never sent) for when the friend accepts."""
        f = self.get(fid)
        if f is None:
            raise FriendError("not a friend")
        info = None
        if file is not None:
            info = clean_file_info(file)
            if info is None:
                raise FriendError("not an attachment")
            body = file_fallback(info)
        else:
            body = clean_text(body)
            if not body:
                raise FriendError("empty message")
            if text_bytes(body) > MAX_TEXT_BYTES:
                raise FriendError("message too long")
        if not f.tx_epoch:
            f.tx_epoch, f.tx_next = os.urandom(3).hex(), 1
        now = time.time() if now is None else now
        wire = make_text(f.tx_epoch, f.tx_next, body, now, file=info)
        f.tx_next += 1
        f.outbox.append(wire)
        entry = {"id": wire["id"], "dir": "out", "kind": "file" if info else "text", "b": body,
                 "ts": now, "n": wire["n"], "read": True, "state": "pending", "missed": 0}
        if info:
            entry.update(f=info, fs="offered", fw="", fp=_file_paths([list(p) for p in paths or []]),
                         sd="", sf=[])
        self._chat_add(f, entry)
        self.save()
        return wire

    def receive_text(self, fid: str, msg: dict, now: Optional[float] = None,
                     read: bool = False) -> Optional[dict]:
        """Take a text from the friend (a parse_slot message). The entry added
        to the conversation, or None when it is one already had. If numbers
        were skipped, a "did not reach you" marker goes in first."""
        f = self.get(fid)
        if f is None:
            return None
        epoch, n = msg["id"].rpartition(".")[0], msg["n"]
        if epoch != f.rx_epoch:
            f.rx_epoch, f.rx_n = epoch, 0          # they started numbering afresh
        if n <= f.rx_n:
            return None
        now = time.time() if now is None else now
        missed = n - f.rx_n - 1
        if missed > 0:
            self._chat_add(f, {"id": f"gap-{epoch}.{n}", "dir": "sys", "kind": "gap", "b": "",
                               "ts": now, "n": n, "read": True, "state": "", "missed": missed})
        f.rx_n = n
        entry = {"id": msg["id"], "dir": "in", "kind": "text", "b": msg["b"], "ts": now,
                 "n": n, "read": bool(read), "state": "", "missed": 0}
        info = msg.get("f")
        if info:
            from core.p2p import attachments as _a
            # What the friend's own words said about it do not matter: the card
            # is made from the checked description alone.
            blocked = info["c"] == 1 and _a.is_blocked_name(info["l"])
            entry.update(kind="file", b=file_fallback(info), f=dict(info),
                         fs="blocked" if blocked else "offered", fw="type" if blocked else "",
                         fp=[], sd="", sf=[])
        self._chat_add(f, entry)
        return entry

    # attachments ---------------------------------------------------------
    def file_entry(self, fid: str, mid: str) -> Optional[dict]:
        """The conversation entry (live: change it through set_file_state) of
        an attachment offer, or None."""
        f = self.get(fid)
        return next((e for e in f.chat if e["id"] == mid and e["kind"] == "file"), None) if f else None

    def set_file_state(self, fid: str, mid: str, state: str, why: str = "", **extra) -> bool:
        """Move an attachment on to *state* (one of FILE_STATES). *extra* may
        set ``sd`` / ``sf`` (where a received attachment was put). The paths of
        files I offered are forgotten once the offer is over. Returns False for
        an unknown one."""
        e = self.file_entry(fid, mid)
        if e is None or state not in FILE_STATES:
            return False
        e["fs"], e["fw"] = state, str(why or "")[:40]
        for k in ("sd", "sf"):
            if k in extra:
                e[k] = extra[k]
        if state not in ("offered", "waiting", "sending"):
            e["fp"] = []
        self.dirty = True
        return True

    def lapse_files(self, now: Optional[float] = None) -> list:
        """Offers nobody took up in OFFER_SECONDS are over. Returns their ids."""
        from core.p2p import attachments as _a
        now = time.time() if now is None else now
        done = []
        for f in self.friends:
            for e in f.chat:
                if (e["kind"] == "file" and e.get("fs") == "offered"
                        and now - e["ts"] > _a.OFFER_SECONDS):
                    e["fs"], e["fw"], e["fp"] = "expired", "", []
                    done.append(e["id"])
        if done:
            self.dirty = True
        return done

    def ack_for(self, fid: str) -> Optional[dict]:
        """What to tell the friend I have received (None until they sent a text)."""
        f = self.get(fid)
        return {"e": f.rx_epoch, "n": f.rx_n} if f is not None and f.rx_epoch else None

    def apply_ack(self, fid: str, ack: Optional[dict]) -> list:
        """The friend says they have my texts up to ack["n"]: those leave the
        outbox and read "delivered". Returns their ids."""
        f = self.get(fid)
        if f is None or not ack or ack.get("e") != f.tx_epoch:
            return []
        done = [o["id"] for o in f.outbox if o["n"] <= ack["n"]]
        if not done:
            return []
        f.outbox = [o for o in f.outbox if o["n"] > ack["n"]]
        for e in f.chat:
            if e["id"] in done:
                e["state"] = "delivered"
        self.dirty = True
        return done

    def mark_sent(self, fid: str, ids: list) -> list:
        """The DHT stored these: "pending" becomes "sent". Returns what changed."""
        f = self.get(fid)
        if f is None:
            return []
        changed = []
        for e in f.chat:
            if e["id"] in ids and e.get("state") == "pending":
                e["state"] = "sent"
                changed.append(e["id"])
        if changed:
            self.dirty = True
        return changed

    def unread_chat(self, fid: str) -> int:
        f = self.get(fid)
        return sum(1 for e in f.chat if e["dir"] == "in" and e["kind"] in ("text", "file")
                   and not e["read"]) if f is not None else 0

    def mark_chat_read(self, fid: str) -> bool:
        f = self.get(fid)
        changed = False
        for e in (f.chat if f is not None else []):
            if e["dir"] == "in" and not e["read"]:
                e["read"] = True
                changed = True
        if changed:
            self.dirty = True
        return changed

    def clear_chat(self, fid: str) -> None:
        """Forget the conversation here, texts still waiting to be delivered
        included. The numbering carries on, so nothing is mistaken for old."""
        f = self.get(fid)
        if f is not None:
            f.chat, f.outbox = [], []
            self.dirty = True
            self.save()

    def remember(self, fid: str, msg_id: str) -> bool:
        """True the first time *msg_id* is seen from this friend."""
        f = self.get(fid)
        if f is None or msg_id in f.seen:
            return False
        f.seen.append(msg_id)
        del f.seen[:-_SEEN_KEEP]
        self.dirty = True
        return True

    # file ---------------------------------------------------------------
    def _encode(self, raw: bytes) -> bytes:
        try:
            key = self._key_fn()
        except Exception:
            logger.warning("P2P friends: no key to encrypt the friend list with; "
                           "it is kept as plain text", exc_info=True)
            return raw
        from core.credentials import encrypt_data
        return _FILE_MAGIC + encrypt_data(raw, key)

    def _decode(self, data: bytes) -> bytes:
        if not data.startswith(_FILE_MAGIC):
            return data
        from core.credentials import decrypt_data
        return decrypt_data(data[len(_FILE_MAGIC):], self._key_fn())

    def _encode_password(self, raw: bytes) -> bytes:
        header = _lock_bytes(self.lock)
        return (_FILE_MAGIC_PW + len(header).to_bytes(4, "big") + header
                + _secret.seal_with(self._dek, raw, b"friends file" + header))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = self.path.read_bytes()
            if data.startswith(_FILE_MAGIC_PW):
                # Sealed under the lock password: only the header is read, which
                # holds nothing but how to check a password.
                header, _body = _split_password_file(data)
                self.lock, self._sealed = header, True
                return
            doc = json.loads(self._decode(data).decode("utf-8"))
            priv = base64.b32decode(doc["identity"] + "=" * (-len(doc["identity"]) % 8))
            if len(priv) != 32:
                raise ValueError("identity")
            friends = [_friend_from_dict(d) for d in doc.get("friends", [])]
            self._priv, self.friends = priv, friends
        except Exception:
            # Unreadable (another login, a damaged file): keep it aside rather
            # than overwrite it, and start with a new identity.
            logger.warning("P2P friends: the friend list could not be read", exc_info=True)
            try:
                aside = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
                os.replace(self.path, aside)
                self.reset_reason = str(aside)
            except OSError:
                self.reset_reason = "unreadable"

    def save(self) -> None:
        if self._sealed or self._priv is None:
            return              # sealed: the file is not ours to touch; no identity: nothing to keep
        doc = {"v": 1,
               "identity": base64.b32encode(self._priv).decode("ascii").rstrip("="),
               "friends": [_friend_to_dict(f) for f in self.friends]}
        raw = json.dumps(doc).encode("utf-8")
        try:
            blob = self._encode_password(raw) if self.lock is not None else self._encode(raw)
        except Exception:
            logger.warning("P2P friends: could not encrypt the friend list", exc_info=True)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            logger.warning("P2P friends: could not save the friend list", exc_info=True)

    def flush(self) -> None:
        if self.dirty and self._priv is not None:
            self.save()
