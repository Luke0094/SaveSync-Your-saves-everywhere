"""Unreal saves a game locked with a key of its own.

Unreal writes ``.sav`` files that open with ``GVAS`` — see core/gvas, which
reads them. A game is free to encrypt that file before writing it, and when
it does the magic goes under the encryption with everything else: the file is
high-entropy from its first byte and nothing in it says what it is. Its
length is a whole number of AES blocks, and that is the only clue it gives.

The key is in the game, not in the save. Unlike Easy Save there is no place
the engine puts it — Easy Save writes its password as a plain string beside a
marker anyone can search for, while a game encrypting an Unreal save does it
in its own code, with nothing naming it. So it is not looked up; it is looked
FOR, in the game's own binaries, and every candidate is tried.

**The proof is the magic.** A key is accepted only when what comes out starts
with ``GVAS`` — the same rule the rest of the editor is built on. A wrong key
produces noise, noise does not start with GVAS, and the file is left alone.
That is what makes searching honest rather than guessing: millions of
candidates can be tried because the save itself says which one was right, and
a search that finds nothing says nothing rather than something wrong.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAGIC = b"GVAS"
# Where in a binary a constant of this size sits. Sixteen first because that
# is where a compiler aligns one; the finer steps are the fallback, and each
# is twice the work of the one before. It stops at four: a compiler aligns a
# constant to at least its word, so a key at an odd address is not a case
# worth the hours that stepping one byte at a time would cost.
_STRIDES = (16, 8, 4)
# How often to ask whether to carry on — every 64 KB of positions, which is
# often enough to feel responsive and rare enough to cost nothing.
_TICK_MASK = 0xFFFF
# A binary bigger than this is not searched: at around sixty thousand keys a
# second, a hundred megabytes is most of an hour at the finest step, and a
# game's own module is far smaller than that.
_MAX_BINARY = 128 << 20
_MAX_BINARIES = 24
_BLOCK = 16
# Unreal's own FAES uses a 32-byte key; games rolling their own commonly use
# 16 or 24. All three are standard AES sizes and are accepted as given.
_KEY_SIZES = (32, 24, 16)
# A file smaller than this is not a save worth trying, and one not a whole
# number of blocks was never AES to begin with.
_MIN_SIZE = 32
# What a key file may be called, beside the save or in the game's folder.
KEY_FILE = "unreal.key"


class UnrealCryptError(Exception):
    pass


def looks_encrypted(raw: bytes) -> bool:
    """Whether *raw* could be an Unreal save with its magic under encryption.

    Deliberately weak: this says "not ruled out", not "is". It is only ever
    asked about a file that nothing else claimed, and being wrong costs one
    failed decryption rather than a bad answer.
    """
    return (len(raw) >= _MIN_SIZE
            and len(raw) % _BLOCK == 0
            and not raw.startswith(MAGIC))


def parse_key(text: str) -> list:
    """The byte forms a written key might mean, best first.

    A key is published as hex, as base64, or as the plain characters someone
    typed. Which one it is can be told by trying: the wrong reading gives the
    wrong bytes, and the wrong bytes fail the GVAS test below.
    """
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return []
    out = []
    cleaned = text.removeprefix("0x").removeprefix("0X").replace(" ", "")
    try:
        if len(cleaned) % 2 == 0 and all(
                c in "0123456789abcdefABCDEF" for c in cleaned):
            out.append(bytes.fromhex(cleaned))
    except ValueError:
        pass
    try:
        import base64
        raw = base64.b64decode(text, validate=True)
        if raw:
            out.append(raw)
    except Exception:
        pass
    out.append(text.encode("utf-8"))
    # Only real AES key lengths, and each shape once.
    seen, keys = set(), []
    for k in out:
        if len(k) in _KEY_SIZES and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _decrypt_ecb(raw: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(raw) + dec.finalize()


def _decrypt_cbc(raw: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    iv, body = raw[:_BLOCK], raw[_BLOCK:]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(body) + dec.finalize()


def decrypt(raw: bytes, key_text: str) -> tuple:
    """(plain bytes, how it was locked) for a key that works, or ("", "").

    *how* is kept so the file can be locked again exactly as it was found —
    a save written back any other way is one the game would refuse.
    """
    if not looks_encrypted(raw):
        return b"", ""
    for key in parse_key(key_text):
        # ECB first: it is what Unreal's own FAES does, so it is the likelier
        # of the two and costs nothing to rule out.
        for how, fn in (("ecb", _decrypt_ecb), ("cbc", _decrypt_cbc)):
            if how == "cbc" and len(raw) < _BLOCK * 2:
                continue
            try:
                plain = fn(raw, key)
            except Exception as e:
                logger.debug(f"Unreal: {how} with a {len(key)}-byte key "
                             f"failed ({e})")
                continue
            if plain.startswith(MAGIC):
                logger.info(f"Unreal: opened with a {len(key)}-byte key "
                            f"({how.upper()})")
                return plain, how
    return b"", ""


def _oracle(raw: bytes):
    """A test for one key: does it turn THIS save into a GVAS file?

    Built once and reused, because it is run millions of times. Only the
    first block is decrypted — the magic is in it, and a key that produces
    the magic is then used on the whole file and checked again.
    """
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    first = raw[:_BLOCK]
    second = raw[_BLOCK:_BLOCK * 2]

    def test(key: bytes) -> str:
        try:
            if Cipher(algorithms.AES(key), modes.ECB()).decryptor() \
                    .update(first).startswith(MAGIC):
                return "ecb"
            if second and Cipher(algorithms.AES(key), modes.CBC(first)) \
                    .decryptor().update(second).startswith(MAGIC):
                return "cbc"
        except Exception:
            pass
        return ""
    return test


def _string_keys(blob: bytes):
    """Keys written as text: the characters themselves, hex, or base64."""
    import base64
    import re
    for m in re.finditer(rb"[\x20-\x7e]{16,90}", blob):
        s = m.group(0)
        for n in _KEY_SIZES:
            if len(s) >= n:
                yield s[:n]
        text = s.decode("ascii", "ignore")
        for n in (32, 48, 64):
            if len(text) >= n and all(c in "0123456789abcdefABCDEF"
                                      for c in text[:n]):
                try:
                    yield bytes.fromhex(text[:n])
                except ValueError:
                    pass
        if 20 <= len(text) <= 90 and re.fullmatch(r"[A-Za-z0-9+/=]+", text):
            try:
                decoded = base64.b64decode(text, validate=True)
                if len(decoded) in _KEY_SIZES:
                    yield decoded
            except Exception:
                pass


def game_binaries(game_dir) -> list:
    """The files in a game worth searching for its key, cheapest first.

    The game's own compiled code, and nothing else: the engine's own DLLs are
    shipped identically with every Unreal game and cannot hold what this
    particular one chose. Sorted smallest first so the quick answers come
    first — a key is as likely to be in a small module as in the main
    executable, and the main executable is tens of megabytes.
    """
    root = Path(game_dir)
    if not root.is_dir():
        return []
    out = []
    for pattern in ("*.exe", "*.dll"):
        try:
            for p in root.rglob(pattern):
                parts = [q.lower() for q in p.parts]
                if "engine" in parts or "thirdparty" in parts:
                    continue          # shipped with the engine, not the game
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if 0 < size <= _MAX_BINARY:
                    out.append((size, p))
        except OSError:
            continue
    out.sort()
    return [p for _s, p in out[:_MAX_BINARIES]]


def find_key(raw: bytes, binaries, on_tick=None) -> tuple:
    """Hunt for the key in a game's own binaries. Returns (key hex, how).

    *binaries* are the files to look in, cheapest first. Each is searched two
    ways: for a key written as text, which is quick and often enough, and
    then for one compiled in as a plain array of bytes, which is millions of
    positions and is why *on_tick* exists — it is called as the search goes
    and stops it by returning False.

    A byte array is looked for at sixteen-byte steps before finer ones. That
    is where a compiler puts a constant of this size, and starting anywhere
    else would spend the first several minutes in the least likely places.
    """
    if not looks_encrypted(raw):
        return "", ""
    test = _oracle(raw)
    seen = set()

    def carry_on() -> bool:
        return on_tick is None or on_tick() is not False

    for path in binaries:
        try:
            blob = Path(path).read_bytes()
        except OSError:
            continue
        if not carry_on():
            return "", ""
        for key in _string_keys(blob):
            if key in seen:
                continue
            seen.add(key)
            how = test(key)
            if how:
                logger.info(f"Unreal: key found written in {Path(path).name}")
                return key.hex(), how
        for stride in _STRIDES:
            for size in _KEY_SIZES:
                # Inclusive end: last valid window starts at len(blob) - size.
                for off in range(0, max(0, len(blob) - size + 1), stride):
                    if not (off & _TICK_MASK) and not carry_on():
                        return "", ""
                    how = test(blob[off:off + size])
                    if how:
                        logger.info(f"Unreal: {size}-byte key found in "
                                    f"{Path(path).name} at 0x{off:x}")
                        return blob[off:off + size].hex(), how
    return "", ""


def encrypt(plain: bytes, key_text: str, how: str) -> bytes:
    """Lock it again the way it was found, so an unchanged save is unchanged.

    The padding is whatever the game itself left: the plain text is written
    back to a whole number of blocks with zero bytes, which is what produces
    the identical file when nothing was edited.
    """
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    keys = parse_key(key_text)
    if not keys:
        raise UnrealCryptError("that key is not a usable length")
    key = keys[0]
    if len(plain) % _BLOCK:
        plain = plain + b"\0" * (_BLOCK - len(plain) % _BLOCK)
    if how == "ecb":
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(plain) + enc.finalize()
    raise UnrealCryptError(f"{how} cannot be written back")
