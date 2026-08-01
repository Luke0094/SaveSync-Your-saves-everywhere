"""Wolf RPG Editor save files.

Taken from WolfSave (github.com/Sinflower/WolfSave). A Wolf save is
obfuscated: three XOR passes over everything from offset 0x14 on, each driven
by the Microsoft C runtime's ``rand()`` seeded from three bytes of the file's
own plaintext header. XOR is its own inverse, so the same three passes in the
opposite order put it back.

What this module does and does not do is worth being exact about.

It **unlocks** the file: decrypt, re-encrypt, and recompute the checksum, all
verified byte-for-byte against real saves. That is the part that is hard to
get right and impossible to guess.

It does **not** name the values inside yet. Wolf saves carry no variable
names — a Wolf save editor gets them from the GAME's own database
(``Data/BasicData/CDataBase.project``), which says how many numbers and
strings each record holds. Without that, the bytes are a structure with no
labels, and offering them by position would be inviting someone to write into
whatever happens to sit there. Comparing two saves does not rescue it either:
two real saves of the same game differ in a fifth of their words and are not
even the same length, so nothing lines up.
"""
import logging

logger = logging.getLogger(__name__)

# Everything before this is the plaintext header the seeds come from.
START_OFFSET = 0x14
# The XOR passes: seed byte index, and how far apart the bytes they touch are.
_PASSES = ((0, 1), (3, 2), (9, 5))
# The Microsoft C runtime's linear congruential generator.
_LCG_MUL, _LCG_ADD = 214013, 2531011
# Keystreams, kept by seed byte. A save is decrypted and re-encrypted on every
# open, and again on every round of a held value, always with the same three
# seeds — generating them once instead of six times a second is the whole
# difference between this being usable and not.
_KEYSTREAMS = {}
# Only ever three seeds per file; a few files' worth is plenty.
_KEYSTREAM_KEEP = 24
# Byte 0 of the decrypted body. WolfSave refuses anything else, and so do we:
# it is the one cheap proof that the three passes ran with the right seeds.
_BODY_MARKER = 0x19
_UTF8_FLAG_AT = 6
_UTF8_FLAG = 0x55
_CHECKSUM_AT = 0x02


class WolfError(Exception):
    pass


def _keystream(seed: int, count: int) -> bytearray:
    """The bytes a pass seeded with *seed* XORs with, generated once.

    Wolf XORs with ``(rand() >> 12) & 0xFF``, where ``rand`` is not "a"
    pseudo-random generator but the Microsoft C runtime's — whatever it
    produced on the machine that wrote the save. Its ``rand()`` is bits 16-30
    of the state, so shifting down by 12 leaves bits 28-30: three bits, values
    0 to 7. Taking them straight off the state saves a shift and a mask per
    byte, a million times over.
    """
    entry = _KEYSTREAMS.get(seed)
    if entry is None:
        if len(_KEYSTREAMS) >= _KEYSTREAM_KEEP:
            _KEYSTREAMS.clear()
        entry = _KEYSTREAMS[seed] = [bytearray(), seed & 0xFFFFFFFF]
    buf, state = entry
    if len(buf) < count:
        extra = bytearray(count - len(buf))
        for j in range(len(extra)):
            state = (state * _LCG_MUL + _LCG_ADD) & 0xFFFFFFFF
            extra[j] = (state >> 28) & 7
        buf += extra
        entry[1] = state
    return buf


def _apply(data: bytearray, passes) -> None:
    """XOR each pass's keystream over the bytes that pass touches.

    A strided slice of a bytearray is a copy, so the XOR is done on the whole
    slice at once as one big integer — C speed — rather than a byte at a time
    in Python. The three passes commute: each only ever XORs, the keystreams
    do not depend on the data, and the seed bytes sit below START_OFFSET where
    no pass reaches them. That is why locking and unlocking are the same work
    in the opposite order, and why both can share these keystreams.
    """
    for seed_index, step in passes:
        segment = data[START_OFFSET::step]
        count = len(segment)
        if not count:
            continue
        key = _keystream(data[seed_index], count)
        mixed = (int.from_bytes(segment, "big")
                 ^ int.from_bytes(key[:count], "big"))
        data[START_OFFSET::step] = mixed.to_bytes(count, "big")


def decrypt(data: bytes) -> bytes:
    """Unlock a Wolf save. The seeds are read from the file's own header,
    which is left in the clear."""
    if len(data) <= START_OFFSET:
        raise WolfError("too short to be a Wolf RPG save")
    out = bytearray(data)
    _apply(out, _PASSES)
    return bytes(out)


def encrypt(data: bytes) -> bytes:
    """Lock it again. The same passes in the opposite order — and the seeds
    still come from the header, which decryption never touched."""
    if len(data) <= START_OFFSET:
        raise WolfError("too short to be a Wolf RPG save")
    out = bytearray(data)
    _apply(out, tuple(reversed(_PASSES)))
    return bytes(out)


def checksum(decrypted: bytes) -> int:
    """The single byte at 0x02: every byte from 0x14 on, added up."""
    total = 0
    for i in range(START_OFFSET, len(decrypted)):
        total = (total + decrypted[i]) & 0xFF
    return total


def fix_checksum(decrypted: bytes) -> bytes:
    out = bytearray(decrypted)
    out[_CHECKSUM_AT] = checksum(out)
    return bytes(out)


def _head(data: bytes, count: int) -> bytes:
    """The first *count* bytes of the body, unlocked without the rest.

    A pass touches position i only when i - START_OFFSET is a multiple of its
    step, and takes the keystream byte at that multiple, so any prefix can be
    worked out on its own. Which matters: this runs over every save file
    SaveSync looks at, and a megabyte to answer a yes/no question is a
    megabyte too many.
    """
    count = max(0, min(count, len(data) - START_OFFSET))
    out = bytearray(data[START_OFFSET:START_OFFSET + count])
    for seed_index, step in _PASSES:
        key = _keystream(data[seed_index], -(-count // step))
        for j in range(0, count, step):
            out[j] ^= key[j // step]
    return bytes(out)


# The marker, the two-byte length of the game's name, and the name itself.
_HEADER_PEEK = 272


def is_wolf_save(data: bytes) -> bool:
    """True when unlocking the file produces a Wolf save's opening.

    The marker alone is thin evidence: it is one byte XORed with three
    three-bit values, so it only pins the low three bits and about one file in
    thirty-two would pass by chance. What follows it is far more particular —
    the game's own name, stored as a two-byte length and then that many bytes
    ending in a nul. Requiring the whole shape to hold, rather than just the
    marker, is what makes this safe to run on files that are not Wolf's.
    """
    if len(data) <= START_OFFSET + 4:
        return False
    head = _head(data, _HEADER_PEEK)
    if head[0] != _BODY_MARKER:
        return False
    length = head[1] | (head[2] << 8)
    return 1 <= length <= len(head) - 3 and head[length + 2] == 0


def game_name(decrypted: bytes) -> str:
    """The game's own name, which Wolf writes right after the marker."""
    raw = decrypted[START_OFFSET + 3:START_OFFSET + 64]
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    encoding = ("utf-8" if len(decrypted) > _UTF8_FLAG_AT
                and decrypted[_UTF8_FLAG_AT] == _UTF8_FLAG else "cp932")
    return raw.decode(encoding, errors="replace").strip()
