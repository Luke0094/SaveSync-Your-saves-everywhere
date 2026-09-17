"""Wolf RPG Editor saves — the LZ4 variant.

Some Wolf RPG Editor builds write a second save format alongside — or
instead of — the one ``core.engines.wolf`` implements. A byte at offset 8 tags which:
this module handles tags 1 and 3 (see ``TAG_VALUES``), reverse-engineered by
decrypting the game itself with Frida while it loaded its own saves,
verified byte-for-byte against what the game's own code actually wrote to
memory (see the project's ``intel/wolf_lz4_findings.md`` for the full derivation; this
module is the "so what" of that write-up).

The scheme, once unwound: three salt bytes from the file's own (clear)
header feed a 32-bit seed into a Mersenne Twister — the well-known MT19937,
though this build tempers its output with different constants than the
reference algorithm, which is the one place it was deliberately customised
rather than reused. 128 bytes of that generator's output, taken in a fixed
rotated order, are the repeating XOR keystream for everything from offset
0x14 on. What that unlocks is two little-endian lengths and then an
ordinary, unmodified LZ4 block — decompressing it lands on exactly the same
kind of plaintext ``core.engines.wolf`` produces: marker 0x19, the game's
own name, and the same variable-database shape ``crypt.wolf`` already knows
how to read (see ``crypt.wolf_lz4``, which does exactly that instead of
duplicating it).

How the three salt bytes become the 32-bit seed — the one piece manual
reverse-engineering never recovered from reading the obfuscated code — was
eventually found not by reading it but by emulating it: Ghidra's own
p-code emulator ran the real ``FUN_0047caf0`` body directly (no live game
needed) for chosen salt bytes, and the resulting seed was read back out of
the emulated stack. What that revealed (see ``derive_seed`` below and
intel/wolf_lz4_findings.md's "chasing the formula" section for the full derivation): each
salt byte contributes to the seed completely independently, as an exact
GF(2)-linear — pure XOR, no carries — function of that byte alone. No
byte-packing, FNV, or carried multiplier ever reproduced it because none of
those are XOR-linear; once framed that way, three tables of 8 bit-masks
each were enough. Confirmed exactly against every one of the 52 real salts
this project has live-game ground truth for, and against real save files
decrypting end to end through it — ``find_seed`` below uses it as the
normal path now, not the 2^32 search, which stays only as a fallback (see
its own docstring).

One more step some builds add on top: after encrypting, two 20-byte spans
of the buffer get swapped with each other before the file is written. That
offset pair turned out to be closed-form too, recovered the same way as
the seed — emulating the real swap function, ``FUN_00523b40``, directly —
and just as clean once found: each span's offset is a function of exactly
one clear header byte, nothing else (see ``derive_swap_spans``). Neither
depends on the encrypted body or the file's length at all — confirmed
directly, not assumed, by re-running the emulation with the body changed
and with the buffer truncated and seeing the same answer either way.
``unlock``/``lock``/``verify_seed`` all handle this transparently — a
caller only sees the two length fields and payload either way, and reading
back an edited save the caller itself wrote no longer needs a search at
all: the same header bytes read the same offsets, every time.
"""
import logging
import struct
import time

logger = logging.getLogger(__name__)

# Optional: a JIT-compiled seed-search path (see _search_numba) roughly
# 3-4x faster than the numpy+multiprocessing one below, measured on real
# hardware (see find_seed's own docstring). Not a hard dependency — numba
# pulls in llvmlite (~40MB) for a feature that only matters to the one
# save format needing a 32-bit search at all, so its absence just means
# falling back to the slower, always-available path, not an error.
import numpy as np

try:
    import numba
    from numba import njit, prange
    _HAVE_NUMBA = True
except ImportError:
    _HAVE_NUMBA = False

TAG_AT = 8
# The tag values observed taking this code path in the game's own dispatch
# (LAB_009a6215 and the ``FUN_00523b40`` pre-step before it for tag > 2).
# Only 1 and 3 have been seen in the wild — 3 on every real save this was
# checked against, 1 in the disassembly's own branch shape — the wider range
# the dispatch itself allows (up to 0xC7) is deliberately NOT claimed here:
# nothing has confirmed those tags actually produce this same body, and a
# save this cannot truly open must be left alone, not corrupted by a guess.
TAG_VALUES = (1, 3)
HEADER_LEN = 0x14
# Salt bytes, read from the file's own (never-encrypted) header.
_SALT_OFFSETS = (0, 1, 5)
_KS_LEN = 128
# The rotation between natural MT draw order and the order the game actually
# consumes them in. Empirically determined (see intel/wolf_lz4_findings.md) — the
# underlying cause is pointer arithmetic inside the obfuscated twist loop
# that was not traced by hand, but the offset itself is exact, checked
# against 512 live-captured bytes, not merely plausible.
_KS_ROTATE = 20
# Marker byte 0x19, same meaning as core.engines.wolf's _BODY_MARKER — the
# decompressed body starts with the same shape a standard Wolf save's body
# does (marker, then a 2-byte length and that many bytes of the game's own
# name), just reached by a different lock. It also shows up two bytes into
# the still-COMPRESSED LZ4 stream (the marker and the handful of bytes after
# it are too close to the start to be back-referenced, so LZ4 stores them as
# literals) — at HEADER_LEN + 10, on the ONE save this was captured live
# from. Confirmed against that capture, not assumed — but that capture is
# also the only ground truth this offset has: the LZ4 encoder's literal-run
# length (and so exactly where the marker lands among the literal bytes)
# depends on the plaintext being compressed, chiefly the game's own title
# string, which is not guaranteed identical across every build or save this
# scheme is ever met on. _MARKER_CHECK_OFFSET stays the documented, most
# likely value; find_seed's cheap filter also tries a run of nearby offsets
# (see _MARKER_CHECK_BASES) as a hedge against that one capture not
# generalising perfectly — cheaply, since the recurrence _mt_words_batch
# pays for is set by the DEEPEST word index needed across all of them, and
# offsets this close together barely move that.
_BODY_MARKER = 0x19
_MARKER_CHECK_OFFSET = 10
_MARKER_CHECK_SLACK = 24


class WolfLZ4Error(Exception):
    pass


# ── Mersenne Twister, customised tempering ──────────────────────────────────

_MASK32 = 0xFFFFFFFF
_N = 624
_M = 397
_MATRIX_A = 0x9908B0DF
_UPPER_MASK = 0x80000000
_LOWER_MASK = 0x7FFFFFFF


def _temper(y: int) -> int:
    """The customised output step. Reference MT19937 masks AFTER each shift;
    this build masks BEFORE, with its own constants — confirmed against a
    live-captured 624-word state array, not guessed."""
    y &= _MASK32
    y ^= (y >> 11)
    y &= _MASK32
    y ^= (y & 0xFF3A58AD) << 7 & _MASK32
    y &= _MASK32
    y ^= (y & 0xFFFFDF8C) << 15 & _MASK32
    y &= _MASK32
    y ^= (y >> 18)
    return y & _MASK32


class _MT19937:
    __slots__ = ("mt", "index")

    def __init__(self, seed: int):
        self.mt = [0] * _N
        self.mt[0] = seed & _MASK32
        for i in range(1, _N):
            self.mt[i] = (1812433253 * (self.mt[i - 1] ^ (self.mt[i - 1] >> 30)) + i) & _MASK32
        self.index = _N

    def _twist(self) -> None:
        mt = self.mt
        for i in range(_N):
            y = (mt[i] & _UPPER_MASK) | (mt[(i + 1) % _N] & _LOWER_MASK)
            mt[i] = mt[(i + _M) % _N] ^ (y >> 1)
            if y & 1:
                mt[i] ^= _MATRIX_A
        self.index = 0

    def next32(self) -> int:
        if self.index >= _N:
            self._twist()
        y = self.mt[self.index]
        self.index += 1
        return _temper(y)


def keystream(seed: int) -> bytes:
    """The 128-byte repeating XOR keystream a seed produces."""
    mt = _MT19937(seed)
    raw = bytes(mt.next32() & 0xFF for _ in range(_KS_LEN))
    return bytes(raw[(i + _KS_ROTATE) % _KS_LEN] for i in range(_KS_LEN))


def _apply(data: bytearray, seed: int) -> None:
    """XOR is its own inverse — this both locks and unlocks."""
    ks = keystream(seed)
    for i in range(HEADER_LEN, len(data)):
        data[i] ^= ks[(i - HEADER_LEN) % _KS_LEN]


# ── the post-encrypt swap ────────────────────────────────────────────────────
#
# Some builds run one more step after FUN_0047caf0 (encrypt) and before the
# file hits disk: FUN_00523b40 swaps two SWAP_LEN-byte spans of the
# already-encrypted buffer with each other — confirmed live, by hooking that
# function directly and diffing its argument before/after (see intel/wolf_lz4_findings.md,
# "Session 2 continued"). It is a pure positional swap, not a checksum or a
# second cipher pass — undoing it is applying the same swap again, since
# swapping twice is the identity.
#
# The two span offsets are NOT a fixed position and NOT a simple function of
# content length (two same-length real saves had different offsets — see
# intel/wolf_lz4_findings.md), and the formula that actually picks them was never
# recovered. What both real captures this was checked against DID share:
# the offsets are small, and there is a strong, cheap oracle for "did I just
# undo the right swap" — decrypting and decompressing lands on the same
# marker-byte-then-name shape core.engines.wolf.is_wolf_save requires. That
# makes a bounded search practical instead of a guess: see find_swap_spans.

_SWAP_LEN = 20
# Absolute file-offset upper bound for either span. Five real captures (see
# intel/wolf_lz4_findings.md's span table, normalised to absolute file offsets) put span1
# in [20, 49] and span2 in [92, 117] — this is 4x+ headroom over that
# spread, not an arbitrary number. A save whose real offsets fall outside
# this range will simply not be found (find_swap_spans returns None, and
# callers raise rather than guess) — widen this if that is ever seen.
_SWAP_SEARCH_MAX_OFFSET = 512

# ── swap offsets, closed form ────────────────────────────────────────────────
#
# Recovered the same way the salt->seed formula was (see derive_seed's own
# section, and intel/wolf_lz4_findings.md's "fully solve the save" session): emulating the
# real FUN_00523b40 directly and treating it as an oracle - feeding it
# real save buffers with individual bytes changed and watching what moved.
# The result is as clean as the seed formula turned out to be: both spans
# are a function of exactly ONE clear (never-encrypted) header byte each,
# nothing else - confirmed by exhaustively sweeping each byte through all
# 256 values, and separately confirmed to have ZERO dependence on the
# compressed body (flipped bytes throughout a real 57KB buffer, including
# right at the end - no effect) or on the buffer's length (truncated a real
# buffer from 57KB down to 200 bytes - no effect, span a stayed put even
# past where a shorter real save's body would have ended).
#
# a = HEADER_LEN + (header[11] % 30)   - the earlier span
# b = 80          + (header[14] % 40)  - the later span, chosen far enough
#                                         past a's own range ([20,49]) that
#                                         the two spans can never overlap
#
# Both header bytes are read from the file's own clear header, so this
# needs no seed and no decryption - salt and swap offsets are recoverable
# from the same 20 bytes, before anything else about the file is known.
#
# Validated against 8 real files (this project's 6 current saves plus 2
# from an independent, separately-captured historical backup): all 8 exact
# matches - or rather, 8/8 matches against the TRUE offsets, which is not
# quite the same claim as "8/8 matches against what the shipped
# find_swap_spans search previously returned": one of the 8 (SaveData05)
# exposed a real, pre-existing bug in that search - it returned (33, 93),
# which decrypts to a payload that still passes even the STRONG
# WolfValues-parse validator, but is silently corrupted at 2733 scattered
# byte positions (not just the visible garbling in its title string). The
# formula's answer, (34, 94), decodes byte-for-byte clean. This is now the
# primary path in unlock() specifically because it cannot make that
# mistake: it computes the one true answer rather than accepting the first
# candidate that merely looks plausible.
_SWAP_A_HEADER_OFFSET = 11
_SWAP_A_MOD = 30
_SWAP_B_HEADER_OFFSET = 14
_SWAP_B_BASE = 80
_SWAP_B_MOD = 40


def derive_swap_spans(data: bytes) -> tuple:
    """The (a, b) post-encrypt swap span offsets for *data* — closed form,
    no search, no seed needed (both source bytes are in the file's own
    clear header). See the section comment above for the derivation and
    validation. Always returns a pair; whether THIS file actually needed a
    swap at all is a separate question ``unlock`` answers by trying it and
    checking the result, not something this function can know on its own —
    formats/builds that never use it should just never see the returned
    pair confirmed by a real decode."""
    a = HEADER_LEN + (data[_SWAP_A_HEADER_OFFSET] % _SWAP_A_MOD)
    b = _SWAP_B_BASE + (data[_SWAP_B_HEADER_OFFSET] % _SWAP_B_MOD)
    return (a, b)


def _plausible_wolf_body(payload: bytes) -> bool:
    """Marker byte, then a 2-byte length, then that many bytes ending in a
    NUL — the same shape ``core.engines.wolf.is_wolf_save`` requires.
    Checked directly here rather than imported: this format's decompressed
    payload has the marker at offset 0, not wolf's own START_OFFSET, and a
    bare marker-byte check alone is not selective enough on its own — it let
    through 10 different wrong swap-offset pairs for one real file before
    this fuller shape check was added (see intel/wolf_lz4_findings.md)."""
    if len(payload) < 4 or payload[0] != _BODY_MARKER:
        return False
    length = payload[1] | (payload[2] << 8)
    return 1 <= length <= len(payload) - 3 and payload[length + 2] == 0


def _swapped(data: bytes, a: int, b: int) -> bytearray:
    buf = bytearray(data)
    span_a = bytes(buf[a:a + _SWAP_LEN])
    span_b = bytes(buf[b:b + _SWAP_LEN])
    buf[a:a + _SWAP_LEN] = span_b
    buf[b:b + _SWAP_LEN] = span_a
    return buf


if _HAVE_NUMBA:
    @njit(cache=True)
    def _numba_swap_cheap_scan(data_arr, ks, header_len, swap_len, lo, hi, n,
                                max_survivors, out_a, out_b):
        # Same cheap length-prefix check find_swap_spans's pure-Python loop
        # runs, compiled: measured at ~0.7s/call in Python for a bound-512
        # sweep (~121,000 pairs) with ZERO survivors most of the time — pure
        # interpreter/allocation overhead, not real work, which is exactly
        # what a JIT removes. This runs once per candidate seed that reaches
        # verify_seed's swap-aware fallback, so its cost (not find_seed's
        # own scan) is what actually bounds how many false positives from
        # the marker filter a full search can absorb — see find_seed's
        # docstring for the measured effect.
        ks_len = ks.shape[0]
        count = 0
        for a in range(lo, hi):
            if a + swap_len > n:
                continue
            for b in range(a + swap_len, hi):
                if b + swap_len > n:
                    continue
                dec = np.empty(8, dtype=np.uint8)
                for i in range(8):
                    pos = header_len + i
                    if a <= pos < a + swap_len:
                        src_pos = b + (pos - a)
                    elif b <= pos < b + swap_len:
                        src_pos = a + (pos - b)
                    else:
                        src_pos = pos
                    ks_idx = (pos - header_len) % ks_len
                    dec[i] = data_arr[src_pos] ^ ks[ks_idx]
                dst_cap = (numba.uint32(dec[0]) | (numba.uint32(dec[1]) << 8)
                           | (numba.uint32(dec[2]) << 16) | (numba.uint32(dec[3]) << 24))
                src_len = (numba.uint32(dec[4]) | (numba.uint32(dec[5]) << 8)
                           | (numba.uint32(dec[6]) << 16) | (numba.uint32(dec[7]) << 24))
                if dst_cap == 0 or dst_cap > 50_000_000 or src_len == 0 or src_len > n:
                    continue
                if count < max_survivors:
                    out_a[count] = a
                    out_b[count] = b
                count += 1
        return count


def find_swap_spans(data: bytes, seed: int, max_offset: int = _SWAP_SEARCH_MAX_OFFSET,
                     validator=None):
    """The two swap-span offsets (see above), by bounded search — a
    fallback now, not the normal path (see ``derive_swap_spans`` for the
    closed form ``unlock``/``verify_seed`` try first). Kept for a
    build/save the formula was never validated against, and because it is
    what found the formula's own validation data in the first place.

    Has a real, confirmed false-positive risk the closed form does not:
    for one real save (``SaveData05.sav`` — see intel/wolf_lz4_findings.md), this search
    returns ``(33, 93)`` even with the STRONG (real ``WolfValues``-parse)
    validator — a pair that decodes to a payload passing that check while
    still being silently wrong at 2733 scattered byte positions. The true
    answer, ``(34, 94)``, is what ``derive_swap_spans`` returns for that
    same file, confirmed correct by an exact, uncorrupted decode. Trust a
    result from THIS function accordingly — it is a plausible candidate
    the checks available to it could not rule out, not a proof.

    Two passes, cheapest first: a candidate pair is only worth the real
    decrypt+decompress if unswapping it produces a plausible LZ4 length
    prefix at all, so that check alone — a handful of bytes, no XOR loop
    over the whole file — runs first over every pair. At the default bound
    this is roughly 120,000 pairs, comfortably under a second in pure
    Python: six orders of magnitude smaller than ``find_seed``'s space,
    which is why this stays a plain search with no numpy/multiprocessing.

    *validator*, when given, replaces ``_plausible_wolf_body`` as the final
    accept/reject test on a fully decompressed candidate payload — see
    ``unlock``'s own docstring for why a caller with a real parser on hand
    (``crypt.wolf_lz4``, via ``crypt.wolf``'s variable-database ``_locate``)
    should always pass one: ``_plausible_wolf_body`` only proves the first
    ~15 bytes are shaped right, which real testing (not theorised — see
    intel/wolf_lz4_findings.md) found is NOT always enough to rule out a wrong offset
    pair when the swap lands past whatever LZ4 token emits those bytes.
    This module's own default stays the cheap check, both so it has no
    hard dependency on ``crypt.wolf`` and because ``find_seed``'s own
    unvalidated internal use (confirming a cheap-filter survivor is worth
    a full swap search at all) doesn't need it — only a caller intending
    to trust the RESULT for something as consequential as a rewrite does.

    Returns ``(a, b)`` or ``None`` (no in-range swap found — the file may
    genuinely have none, see ``TAG_VALUES``'s note on tag 1).
    """
    import lz4.block

    check = validator or _plausible_wolf_body

    ks = keystream(seed)
    n = len(data)
    hi = min(max_offset, n - _SWAP_LEN)
    if hi <= HEADER_LEN:
        return None
    check_upto = HEADER_LEN + 8

    def decrypt_range(buf: bytearray, start: int, stop: int) -> bytes:
        return bytes(buf[i] ^ ks[(i - HEADER_LEN) % _KS_LEN]
                     for i in range(start, stop))

    survivors = []
    if _HAVE_NUMBA:
        # ~50-100x faster than the pure-Python loop below (measured: a
        # ~121,000-pair sweep that took ~0.7s in Python — almost entirely
        # interpreter/allocation overhead for what is usually zero real
        # work — compiles down to low milliseconds). See
        # _numba_swap_cheap_scan's own docstring for why this specifically
        # is what a full search's total time now hinges on.
        #
        # max_survivors MUST cover the true worst case, not a guessed cap:
        # highly regular content (confirmed directly — sequential 4-byte
        # counters compressed by LZ4) can make the cheap length-plausibility
        # check pass for the large majority of pairs, not the rare handful
        # random data would give. A fixed cap silently dropping the real
        # answer past it is a correctness bug, not a performance one — this
        # bounds it by the actual pair count instead (a in [HEADER_LEN, hi),
        # b in [a+_SWAP_LEN, hi) — at most C(hi-HEADER_LEN, 2) pairs).
        m = max(0, hi - HEADER_LEN)
        max_survivors = m * m // 2 + 1
        out_a = np.zeros(max_survivors, dtype=np.int64)
        out_b = np.zeros(max_survivors, dtype=np.int64)
        data_arr = np.frombuffer(data, dtype=np.uint8)
        ks_arr = np.frombuffer(ks, dtype=np.uint8)
        count = _numba_swap_cheap_scan(data_arr, ks_arr, HEADER_LEN, _SWAP_LEN,
                                        HEADER_LEN, hi, n, max_survivors, out_a, out_b)
        for i in range(min(count, max_survivors)):
            a, b = int(out_a[i]), int(out_b[i])
            peek_upto = max(a + _SWAP_LEN, b + _SWAP_LEN, check_upto)
            if peek_upto > n:
                continue
            buf = _swapped(data[:peek_upto], a, b)
            dec = decrypt_range(buf, HEADER_LEN, check_upto)
            dst_cap, src_len = struct.unpack_from("<II", dec, 0)
            survivors.append((a, b, dst_cap, src_len))
    else:
        for a in range(HEADER_LEN, hi):
            for b in range(a + _SWAP_LEN, hi):
                peek_upto = max(a + _SWAP_LEN, b + _SWAP_LEN, check_upto)
                if peek_upto > n:
                    continue
                buf = _swapped(data[:peek_upto], a, b)
                dec = decrypt_range(buf, HEADER_LEN, check_upto)
                if len(dec) < 8:
                    continue
                dst_cap, src_len = struct.unpack_from("<II", dec, 0)
                if not (0 < dst_cap <= 50_000_000 and 0 < src_len <= n):
                    continue
                survivors.append((a, b, dst_cap, src_len))

    for a, b, dst_cap, src_len in survivors:
        buf = _swapped(data, a, b)
        dec = decrypt_range(buf, HEADER_LEN, n)
        compressed = dec[8:8 + src_len]
        if len(compressed) != src_len:
            continue
        try:
            payload = lz4.block.decompress(compressed, uncompressed_size=dst_cap)
        except Exception:
            continue
        if len(payload) != dst_cap:
            continue
        if check(payload):
            return (a, b)
    return None


# ── decrypt / decompress and the reverse ─────────────────────────────────────


def is_candidate(data: bytes) -> bool:
    """Cheap-only pre-check: the file is long enough and the tag byte is one
    this module claims. Does NOT decrypt — this format's true test costs a
    seed search, so callers gate that behind engine detection (this game is
    Wolf RPG Editor) rather than running it as a blind content sniff."""
    return len(data) > HEADER_LEN + 8 and data[TAG_AT] in TAG_VALUES


def salt(data: bytes) -> tuple:
    return tuple(data[i] for i in _SALT_OFFSETS)


def decrypt(data: bytes, seed: int) -> bytes:
    """Unlock the header region. Does not decompress — see ``decompress``
    for the LZ4 stream this exposes."""
    if len(data) <= HEADER_LEN + 8:
        raise WolfLZ4Error("too short to hold a length-prefixed LZ4 stream")
    out = bytearray(data)
    _apply(out, seed)
    return bytes(out)


def encrypt(data: bytes, seed: int) -> bytes:
    return decrypt(data, seed)  # XOR: identical operation either direction


def decompress(unlocked: bytes):
    """(dstCap, payload) — the two length-prefixed fields and the decoded
    LZ4 block that follows them, from an already-``decrypt``-ed buffer.

    Requires the decoded payload be EXACTLY ``dst_cap`` bytes, not merely
    within it: python-lz4's own ``uncompressed_size`` is documented as a
    maximum buffer size, not a required output length — "less data may be
    returned" — so a garbled/wrong-key compressed stream can decode
    without raising at all, just short. That gap is what let
    ``find_swap_spans`` accept wrong offset pairs on synthetic test data
    with LZ4-friendly structure (a real, found and fixed correctness bug —
    every caller of this function inherits the fix, not just that one)."""
    import lz4.block

    dst_cap, src_len = struct.unpack_from("<II", unlocked, HEADER_LEN)
    start = HEADER_LEN + 8
    compressed = unlocked[start:start + src_len]
    if len(compressed) != src_len:
        raise WolfLZ4Error("the compressed stream runs past the end of the file")
    try:
        payload = lz4.block.decompress(compressed, uncompressed_size=dst_cap)
    except Exception as e:
        raise WolfLZ4Error(f"LZ4 decompression failed: {e}") from e
    if len(payload) != dst_cap:
        raise WolfLZ4Error(
            f"decoded {len(payload)} bytes, expected exactly {dst_cap}")
    return dst_cap, payload


def compress(payload: bytes) -> bytes:
    """The reverse of ``decompress``: an LZ4 block SaveSync wrote itself.

    Does not have to match the game's own compressor byte-for-byte — LZ4 is
    only a byte-for-byte format across decoders, not encoders, and this only
    ever needs to decompress cleanly again, which any valid LZ4 block does.
    """
    import lz4.block

    return lz4.block.compress(payload, mode="high_compression", store_size=False)


def unlock(data: bytes, seed: int, validator=None):
    """(header_bytes, dstCap, payload, swap_spans) — decrypt AND decompress
    in one call, the shape ``crypt.wolf_lz4`` actually wants.

    Tries the file as-is first (cheap, and correct for any save this
    scheme was applied to before/without the post-encrypt swap — see
    ``find_swap_spans``'s module-level note). Only if that doesn't land on
    a plausible Wolf body does it search for and undo the swap.
    ``swap_spans`` is ``None`` when no swap was needed, or the ``(a, b)``
    pair that was — a caller writing this save back needs it to
    re-apply the same swap (see ``lock``).

    *validator*, forwarded to ``find_swap_spans`` (see its docstring) and
    also used for the "as-is" plausibility check right here — a caller
    that can prove a candidate payload is a REAL Wolf variable database
    (not just shaped like the start of one) should always pass one; this
    function's own accept/reject decisions are exactly what a wrong one
    would corrupt a save through.
    """
    check = validator or _plausible_wolf_body
    payload = None
    try:
        unlocked = decrypt(data, seed)
        dst_cap, payload = decompress(unlocked)
    except WolfLZ4Error:
        pass
    if payload is not None and check(payload):
        return unlocked[:HEADER_LEN + 8], dst_cap, payload, None

    # The closed-form answer (see derive_swap_spans) before any search: it
    # is the one true (a, b) FUN_00523b40 itself would compute, not merely
    # a candidate that happens to look right, so trying it first is both
    # faster AND more correct than the search below — see that function's
    # own module comment for a real case where the search's "plausible"
    # answer was silently wrong and this one was not.
    a, b = derive_swap_spans(data)
    try:
        unlocked = decrypt(bytes(_swapped(data, a, b)), seed)
        dst_cap, payload = decompress(unlocked)
        if check(payload):
            return unlocked[:HEADER_LEN + 8], dst_cap, payload, (a, b)
    except WolfLZ4Error:
        pass

    # Only reached for a build/save this formula was not validated against.
    spans = find_swap_spans(data, seed, validator=validator)
    if spans is None:
        raise WolfLZ4Error(
            "not a plausible LZ4 stream, with or without unswapping")
    a, b = spans
    unlocked = decrypt(bytes(_swapped(data, a, b)), seed)
    dst_cap, payload = decompress(unlocked)
    if not check(payload):
        raise WolfLZ4Error(
            "swap search found a length-plausible offset pair that did "
            "not actually decrypt to a real save")
    return unlocked[:HEADER_LEN + 8], dst_cap, payload, spans


def lock(header: bytes, seed: int, payload: bytes, swap_spans=None) -> bytes:
    """The reverse of ``unlock``: recompress, splice the two length fields
    back in, and re-lock. *header* is the HEADER_LEN + 8 bytes ``unlock``
    handed back — the clear header plus the (now stale) length pair, both
    about to be overwritten here with the fresh ones.

    *swap_spans*, when given the ``(a, b)`` pair ``unlock`` returned,
    re-applies the same swap at the same two offsets. This USED to be a
    real risk — an earlier version of this docstring claimed reuse was
    "correct by construction" whenever the compressed payload stayed the
    same length, which was never actually true (disproven directly: 24
    real captured saves sharing the exact same compressed length have 24
    DIFFERENT swap offsets — see intel/wolf_lz4_findings.md). What that evidence actually
    reflected, now that the real rule is known (see ``derive_swap_spans``):
    every one of those 24 was a genuinely SEPARATE save action, each with
    its OWN freshly-generated random header — nothing to do with length.
    For SaveSync's own edit-and-write-back path specifically, *header*
    (and so the two bytes the swap offsets are actually a function of)
    passes through unchanged from whatever ``unlock`` read, so the ``(a,
    b)`` it returned is still exactly correct here — reusing it isn't a
    guess, it's the same closed-form answer recomputed from the same
    unchanged input. What IS still checked here is the narrower, purely
    mechanical case: if the new compressed size is short enough that span
    *b* no longer fits, this raises rather than writing a truncated swap —
    the game's own loader unconditionally un-swaps at wherever it computes
    the spans to be, so a file whose swap doesn't fit isn't just less
    obfuscated, it's corrupted the moment the real game tries to read it
    back.
    """
    compressed = compress(payload)
    out = bytearray(header[:HEADER_LEN])
    out += struct.pack("<II", len(payload), len(compressed))
    out += compressed
    _apply(out, seed)
    if swap_spans is not None:
        a, b = swap_spans
        if b + _SWAP_LEN > len(out):
            raise WolfLZ4Error(
                f"edit shrank the compressed payload enough that this "
                f"save's swap span (ending at {b + _SWAP_LEN}) no longer "
                f"fits in the new {len(out)}-byte file — there is no way "
                f"to know where the swap belongs now, and writing without "
                f"it would corrupt the save the next time the game itself "
                f"loads it")
        out = _swapped(out, a, b)
    return bytes(out)


# ── salt -> seed, closed form ────────────────────────────────────────────────
#
# Recovered by emulation, not by reading the (anti-disassembly-obfuscated)
# code by hand: Ghidra's own p-code emulator ran the real FUN_0047caf0 body
# directly (no live game needed) for chosen salt bytes and the resulting
# seed was read back out of the emulated stack (see intel/wolf_lz4_findings.md's "Session
# 3"/"chasing the formula" sections for the full derivation). What that
# revealed: each of the 3 salt bytes contributes to the seed completely
# independently, and each contribution is an exact GF(2)-linear (pure XOR,
# no carries) function of its own byte alone -
# ``seed = MASKS0[s0] ^ MASKS1[s1] ^ MASKS2[s2]`` with no additive constant.
# Confirmed exactly (zero mismatches) against 1536 emulated data points
# across 6 independent sweeps and, more importantly, against all 52 real
# salts this project has live-game ground truth for - the derived seed's
# ``keystream()`` reproduces every one of those real, on-disk-save
# keystreams byte-for-byte, and multiple real save files decrypt and
# decompress end to end through it. This replaces the 2^32 search below for
# EVERY file this engine has ever been tested against; the search stays
# only as a defensive fallback in case some build/save this formula wasn't
# validated against ever needs it (see ``find_seed``).
#
# Each MASKS_k tuple is the 8-bit GF(2)-linear map for salt byte k: the XOR
# a given bit contributes to the seed, independent of every other bit -
# a "multiply this byte into the seed" step with no carrying, which is
# exactly what a per-byte CRC-style table update looks like. Stored as the
# 8 basis masks (not a flattened 256-entry table) because that's what was
# actually measured, one bit at a time; _byte_contribution below expands a
# real byte value from them.
_MASKS0 = (0x20231000, 0x40462021, 0x808C4042, 0x01080084,
           0x02100108, 0x04200210, 0x08400420, 0x10800840)
_MASKS1 = (0x04202310, 0x08404620, 0x10808C40, 0x21011880,
           0x42023100, 0x84046200, 0x0808C400, 0x10118800)
_MASKS2 = (0x00042021, 0x00084042, 0x00108084, 0x00210108,
           0x00420231, 0x00840462, 0x010808C4, 0x02101188)


def _byte_contribution(byte: int, masks: tuple) -> int:
    v = 0
    for k in range(8):
        if byte & (1 << k):
            v ^= masks[k]
    return v


def derive_seed(salt_bytes: tuple) -> int:
    """The MT19937 seed for these 3 salt bytes, by closed-form formula -
    no search, no cache, no game needed. See the section comment above for
    where this came from and how it was validated."""
    s0, s1, s2 = salt_bytes
    return (_byte_contribution(s0, _MASKS0)
            ^ _byte_contribution(s1, _MASKS1)
            ^ _byte_contribution(s2, _MASKS2))


# ── seed recovery ────────────────────────────────────────────────────────────
#
# derive_seed above is now the normal path (see find_seed) - this 2^32
# search is what found the formula's own validation data in the first
# place and stays as the fallback for a salt the formula ever gets wrong
# (none found in testing, but "never trust blindly" applies to a derived
# formula the same as to a cache - see verify_seed). The marker byte the
# decrypted stream always opens with, plus the two bytes right after it
# (see _CHECK_OFFSETS below), give a cheap filter selective enough that the
# real, expensive check - decrypt the whole file, decompress it - only has
# to run on a small, bounded number of survivors rather than once per false
# positive the marker alone would let through.
#
# A seed already found for a given salt is remembered - the search never
# needs to run twice for the same save slot, only once for a save from a new
# slot (or a new player's save entirely) the first time it is opened.
_SEED_CACHE: dict = {}
_SEED_CACHE_KEEP = 64


def cached_seed(salt_bytes: tuple):
    return _SEED_CACHE.get(salt_bytes)


def _remember(salt_bytes: tuple, seed: int) -> None:
    if len(_SEED_CACHE) >= _SEED_CACHE_KEEP:
        _SEED_CACHE.clear()
    _SEED_CACHE[salt_bytes] = seed


def verify_seed(data: bytes, seed: int, validator=None) -> bool:
    """The full, expensive-but-certain check: decrypt AND decompress must
    both succeed, and the decompressed body must have the marker-then-name
    shape ``_plausible_wolf_body`` checks (a bare marker byte alone let
    through false positives on real data — see that function's docstring).

    Swap-aware: if the file as-is doesn't decrypt to something plausible,
    this tries the closed-form ``derive_swap_spans`` next, and only then
    ``find_swap_spans`` (a search, and one with a known false-positive risk
    — see that function's own module comment) before giving up. Real
    current saves need this — see intel/wolf_lz4_findings.md. The search fallback only
    runs for a candidate that already passed the cheap 3/4-byte filter in
    ``_search_range``/``_numba_scan``, so it stays rare enough not to
    matter for the 2^32 search's overall cost (see find_seed's docstring
    for the measured rate) even before counting how rarely the formula
    itself needs to fall through to it.

    *validator*, forwarded to ``find_swap_spans`` and used here too instead
    of ``_plausible_wolf_body`` when given — see ``unlock``'s docstring for
    why a caller intending to TRUST a found seed (as opposed to this
    module's own internal search-survivor filtering) should always pass
    the real parser it has on hand rather than the cheap default.

    Used two ways: as the certain confirmation ``_search_range``/
    ``_search_numba`` run on a candidate that already passed its cheap
    filter, and — public for this — to re-prove a seed recalled from disk
    (``crypt.wolf_lz4``'s own game_keys-backed cache) before trusting it,
    the same way nothing here ever trusts a cache blindly.
    """
    check = validator or _plausible_wolf_body
    try:
        unlocked = decrypt(data, seed)
        _dst_cap, payload = decompress(unlocked)
        if payload and check(payload):
            return True
    except WolfLZ4Error:
        pass
    a, b = derive_swap_spans(data)
    try:
        unlocked = decrypt(bytes(_swapped(data, a, b)), seed)
        _dst_cap, payload = decompress(unlocked)
        if payload and check(payload):
            return True
    except WolfLZ4Error:
        pass
    return find_swap_spans(data, seed, validator=validator) is not None


def _physical_cpu_count() -> int:
    """PHYSICAL cores, not logical — see find_seed's own docstring for why
    that distinction matters here specifically. psutil is already a
    project dependency (core.backup, core.concurrency); os.cpu_count()
    (logical) is the fallback when it is unavailable or its physical count
    comes back unknown, since that is still better than nothing."""
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return n
    except Exception:
        pass
    import os
    return os.cpu_count() or 1


class CancelToken:
    """Lets a caller stop an in-progress search RIGHT NOW, from any thread —
    not just at its next scheduled check.

    *progress* returning False is the normal way to ask a search to stop,
    but it is only ever consulted between rounds: a Pool that is midway
    through handing a round's tasks to its workers does not notice that
    flag until every one of them returns, which on a slow chunk can be a
    long wait in its own right — long enough that a person clicking Cancel
    sees nothing happen for a while, or (worse) sees the dialog close while
    the search silently keeps running and burning every core in the
    background. Calling ``cancel()`` here instead terminates the Pool
    directly, which unblocks that wait immediately: multiprocessing.Pool's
    own methods are safe to call from a different thread than the one
    running the search, which is exactly the situation a UI Cancel button
    is in.

    Optional everywhere it is accepted (``find_seed``, ``crypt.wolf_lz4``'s
    ``loads``/``WolfLZ4Values.load``, ``WolfLZ4Format``) — nothing changes
    for a caller that never creates one.
    """

    def __init__(self):
        self._pool = None
        self.cancelled = False

    def _register(self, pool) -> None:
        self._pool = pool

    def _unregister(self) -> None:
        self._pool = None

    def cancel(self) -> None:
        self.cancelled = True
        pool = self._pool
        if pool is not None:
            try:
                pool.terminate()
            except Exception:
                pass


def find_seed(data: bytes, progress=None, workers: int = 0, cancel_token=None,
               validator=None):
    """The 32-bit seed that unlocks *data*. Tries the closed-form
    ``derive_seed`` first (instant — see that function and the section
    comment above it) and only falls back to the 2^32 search below if that
    formula, for some file this hasn't been validated against, turns out
    to be wrong — ``verify_seed`` is what actually decides that, the same
    "never trust blindly" check a cache hit gets, not a bare formula
    result. See the search's own docstring for why it remains at all.

    *progress*, when given, is called with the seconds elapsed so far;
    returning False stops the search and gives up. Same contract as
    ``crypt.es3.find_password``'s — the one other reader in SaveSync whose
    search can run long enough to need one.

    *cancel_token*, when given a ``CancelToken``, lets a caller stop the
    search immediately rather than waiting for *progress* to next be asked
    — see that class for why the two are not redundant.

    *validator*, forwarded to every ``verify_seed`` call this search makes
    — see that function's own docstring for why a caller that will trust
    the result (as opposed to calling this for its own internal purposes)
    should always pass its real parser rather than the cheap default.

    *workers* is how many processes to spread the search across; 0 uses one
    per PHYSICAL core (see _physical_cpu_count — measured on real hardware,
    hyperthreaded sibling cores actively hurt this workload rather than
    helping: it is cache-bandwidth-bound per core, see _search_range's own
    batch size, so two logical threads sharing one physical core's cache
    contend with each other instead of adding real throughput).

    Measured, not estimated, on a real 4-physical-core machine with these
    settings: roughly 1.3-1.8 million candidates/sec once past the first
    couple of warm-up rounds, versus ~510k/sec — FLAT regardless of worker
    count — before the batch size and physical-vs-logical worker fixes.
    That puts a worst-case, cold search (a salt never seen before, on no
    cache hit) at very roughly 40-55 minutes on comparable hardware, not
    the ~10 minutes this docstring used to claim — that earlier figure was
    itself only ever an estimate (see intel/wolf_lz4_findings.md), never actually
    benchmarked until this round of fixes. A faster or slower machine, or
    one with more physical cores, moves this roughly proportionally.

    Every subsequent save from the SAME slot (nearly always, in practice —
    see _remember) costs nothing further.
    """
    salt_bytes = salt(data)
    hit = cached_seed(salt_bytes)
    if hit is not None:
        return hit
    derived = derive_seed(salt_bytes)
    if verify_seed(data, derived, validator):
        _remember(salt_bytes, derived)
        return derived
    # Bounds for the WIDEST base offset the cheap filter now tries (see
    # _MARKER_CHECK_BASES), not just the one documented position — every
    # base has to have real bytes behind it before the search below can
    # read them.
    if len(data) <= HEADER_LEN + max(_MARKER_CHECK_BASES) + 3:
        return None

    started = time.monotonic()

    def _carry_on() -> bool:
        if progress is None:
            return True
        try:
            return progress(time.monotonic() - started) is not False
        except Exception as e:
            logger.debug(f"Wolf LZ4: the progress callback raised ({e})")
            return True

    n_workers = workers or _physical_cpu_count()
    found = _search(data, n_workers, _carry_on, cancel_token, validator)
    if found is not None:
        _remember(salt_bytes, found)
    return found


# Every base position tried for the marker+2-follow-up-bytes check — see
# _MARKER_CHECK_OFFSET's own comment for why more than the one documented
# base is worth trying. 0 through 39 comfortably covers a marker landing a
# couple dozen bytes later than the one captured example, at barely any
# extra recurrence cost (see _mt_words_batch: what it pays for is set by
# the DEEPEST word index needed, and these are all close together).
_MARKER_CHECK_BASES = tuple(range(40))
# Three of the compressed stream's leading bytes per base — checked
# together so the cheap filter's false-positive rate stays low enough that
# verifying every survivor for real (decrypt the whole file, decompress
# it) is a rounding error rather than the bulk of the search, same as it
# was with a single base. Only the marker (0x19) and the shape of a Wolf
# name field (a small length, then that many bytes) are assumed — nothing
# about what the name actually says, which is specific to whichever game
# this runs against.
_CHECK_OFFSET_SETS = tuple((b, b + 1, b + 2) for b in _MARKER_CHECK_BASES)
_NAME_LEN_MAX = 127  # generous for a game's own title; see core.engines.wolf

# The cheap filter above only ever reads the RAW file bytes at each base —
# which is wrong, not just imprecise, when the true marker's own position
# falls inside this save's post-encrypt swap span (see the swap section
# above): the byte physically sitting there came from the OTHER span, and
# no amount of re-reading the same wrong position recovers it. Confirmed
# empirically, not theorised — decrypting the two real captured files this
# project has ground truth for (intel/wolf_lz4_findings.md) with their REAL keystreams
# shows the true marker at base 10 in BOTH, and BOTH real swap spans
# overlap that exact window: System.sav's span (32,96) clips the window's
# 3rd byte, SaveData02.sav's span (20,95) covers all three. That is why a
# full 2^32 real search finds nothing on either file (measured: ~23 and
# ~28 minutes, exhausted, no hit) even though the seed is genuinely there
# and verify_seed confirms it the moment it's handed the answer directly.
#
# The fix tried here is deliberately narrow, not a general joint
# seed+offset search (intel/wolf_lz4_findings.md already rules that out as infeasible:
# 2^32 x ~39,000 is not a real number). It only re-checks the ONE
# documented, twice-confirmed anchor position (_MARKER_CHECK_OFFSET=10)
# against a SMALL grid of "what if a swap already happened here" read
# positions — not a blind search over the full swap-offset range. The
# keystream WORD INDEX needed for this doesn't change under any hypothesis
# (it depends on the file position being solved FOR, never on where its
# ciphertext byte is read FROM — see _apply), so this costs a handful of
# extra array reads per seed, not extra Mersenne Twister work.
#
# Grid bounds: re-derived from 52 real swap events recovered by directly
# diffing the project's own captured before/after buffers (no seed or
# search needed for that — see intel/wolf_lz4_findings.md), not guessed or grown from
# just the original two. Of those 52, only 18 actually have the anchor
# base (10) landing inside span a — the true trigger condition this hedge
# exists for, since a scan whose anchor DOESN'T collide with the swap
# already succeeds without any hedge at all. Across those 18: a_rel is 0-8
# (comfortably inside the existing 0-15 range, so that bound is left as
# is), but gap ranges 54-94 — two values (90, 94) fall outside the
# previous 50-89 window, which was sized from only 2 examples and, when
# checked against the full 52, silently failed find_seed on real,
# genuinely-findable saves (a ~20-45 min search returning None, identical
# in effect to "unsupported file"). Widened with headroom on both sides of
# the observed 54-94 span, not trimmed exactly to it — the 52-event sample
# is real ground truth but still not exhaustive.
_SWAP_HYP_ANCHOR_BASE = _MARKER_CHECK_OFFSET  # == 10; the base all 52
                                                # real captures agree on
_SWAP_HYP_A_REL = tuple(range(0, 16))   # covers observed 0-8 (18 events)
_SWAP_HYP_GAP = tuple(range(35, 115))   # covers observed 54-94 (18 events)
# Every (a_rel, gap) pair this hedge tries, flattened once.
_SWAP_HYP_PAIRS = tuple((a, g) for a in _SWAP_HYP_A_REL for g in _SWAP_HYP_GAP)

# Flattened word indices for every (wi0, wi1, wi2) triple _CHECK_OFFSET_SETS
# needs, in the same rotated order _mt_words_batch computes them in — used
# by the numba path below, kept in lockstep with _CHECK_OFFSET_SETS rather
# than recomputed differently, so the two search paths can never silently
# check different bytes.
_NUMBA_WORD_INDICES = tuple(
    (off + _KS_ROTATE) % _KS_LEN for offs in _CHECK_OFFSET_SETS for off in offs)
# The numba path checks a 4th byte per base too (see _numba_scan) at a
# position that depends on the candidate's own decrypted length byte, which
# can land anywhere in [0, _KS_LEN) after rotation — so, unlike the fixed
# 3-byte check above, its word index isn't knowable ahead of time and the
# recurrence has to cover every possible one, not just the ~42 actually
# used by the fixed checks.
_NUMBA_NEED = max(max(wi, wi + 1, (wi + _M) % _N) for wi in range(_KS_LEN)) + 1

if _HAVE_NUMBA:
    @njit(cache=True, inline="always")
    def _numba_temper_byte(mt, wi):
        # Same customised tempering as _temper() above, inlined against a
        # precomputed recurrence array instead of a Python int — see that
        # function's own docstring for where the constants come from.
        a = mt[wi]
        b = mt[wi + 1]
        partner = mt[(wi + 397) % 624]
        y = (a & numba.uint32(0x80000000)) | (b & numba.uint32(0x7FFFFFFF))
        mt_new = partner ^ (y >> numba.uint32(1))
        if y & numba.uint32(1):
            mt_new ^= numba.uint32(0x9908B0DF)
        y2 = mt_new
        y2 ^= (y2 >> numba.uint32(11))
        y2 ^= (y2 & numba.uint32(0xFF3A58AD)) << numba.uint32(7)
        y2 ^= (y2 & numba.uint32(0xFFFFDF8C)) << numba.uint32(15)
        y2 ^= (y2 >> numba.uint32(18))
        return numba.uint8(y2 & numba.uint32(0xFF))

    @njit(parallel=True, cache=True)
    def _numba_scan(lo, hi, word_indices, need, targets0, targets1, targets2,
                     bases, n_bases, data_arr, header_len, ks_rotate, ks_len,
                     out_hits, anchor_bi, hyp_t0, hyp_t1, hyp_t2, hyp_a_lo,
                     hyp_a_hi, hyp_b_lo, hyp_b_hi, n_hyp):
        # One MT19937-seeding recurrence per seed (need steps — see
        # _NUMBA_NEED), shared across all n_bases checks by indexing into
        # it rather than recomputing per base: the recurrence, not the
        # per-base byte compare, is what actually costs anything here.
        #
        # A 4th check beyond the original 3-byte one: real data showed the
        # marker+length-only filter passes roughly 1 in 3,300 wrong seeds —
        # harmless when rejecting one was microseconds, but that stopped
        # being true once a wrong-seed rejection could mean a real
        # find_swap_spans search (see that function) instead of an instant
        # decrypt failure. Requiring the NUL this format's own name field is
        # supposed to end with, AT THE POSITION THE CANDIDATE'S OWN DECODED
        # LENGTH SAYS IT SHOULD BE, adds a genuine independent byte of
        # selectivity (~256x) without assuming anything about what the name
        # itself contains — unlike, say, requiring printable ASCII, which
        # would reject a real name in a non-Latin encoding.
        n = hi - lo
        n_data = data_arr.shape[0]
        for k in prange(n):
            seed = lo + k
            mt = np.empty(need, dtype=np.uint32)
            mt[0] = numba.uint32(seed)
            for i in range(1, need):
                prev = mt[i - 1]
                mt[i] = (numba.uint32(1812433253)
                          * (prev ^ (prev >> numba.uint32(30))) + numba.uint32(i))
            for bi in range(n_bases):
                p0 = _numba_temper_byte(mt, word_indices[bi * 3])
                p1 = _numba_temper_byte(mt, word_indices[bi * 3 + 1])
                p2 = _numba_temper_byte(mt, word_indices[bi * 3 + 2])
                plain0 = p0 ^ targets0[bi]
                plain1 = p1 ^ targets1[bi]
                plain2 = p2 ^ targets2[bi]
                if plain0 != _BODY_MARKER or plain2 != 0 or plain1 < 1 or plain1 > _NAME_LEN_MAX:
                    continue
                nul_off = bases[bi] + 2 + numba.int64(plain1)
                nul_pos = header_len + nul_off
                if nul_pos >= n_data:
                    continue
                nul_wi = (nul_off + ks_rotate) % ks_len
                nul_plain = _numba_temper_byte(mt, nul_wi) ^ data_arr[nul_pos]
                if nul_plain == 0:
                    out_hits[k] = 1
                    break

            # Swap-hypothesis hedge (see _SWAP_HYP_ANCHOR_BASE's own
            # comment): only reached when the raw-byte scan above found
            # nothing. Reuses this SAME seed's already-computed mt[] — no
            # extra Mersenne Twister work, just extra array reads — and the
            # anchor base's own word indices (word_indices[anchor_bi*3:...])
            # rather than new ones, because the keystream index for a
            # position depends only on the position being solved FOR, never
            # on where its ciphertext byte is read from (see _apply).
            if out_hits[k] == 0:
                awi0 = word_indices[anchor_bi * 3]
                awi1 = word_indices[anchor_bi * 3 + 1]
                awi2 = word_indices[anchor_bi * 3 + 2]
                ap0 = _numba_temper_byte(mt, awi0)
                ap1 = _numba_temper_byte(mt, awi1)
                ap2 = _numba_temper_byte(mt, awi2)
                anchor_base_val = bases[anchor_bi]
                for hj in range(n_hyp):
                    plain0 = ap0 ^ hyp_t0[hj]
                    plain1 = ap1 ^ hyp_t1[hj]
                    plain2 = ap2 ^ hyp_t2[hj]
                    if plain0 != _BODY_MARKER or plain2 != 0 or plain1 < 1 or plain1 > _NAME_LEN_MAX:
                        continue
                    nul_off = anchor_base_val + 2 + numba.int64(plain1)
                    nul_pos_raw = header_len + nul_off
                    if nul_pos_raw >= n_data:
                        continue
                    a_lo = hyp_a_lo[hj]
                    a_hi = hyp_a_hi[hj]
                    b_lo = hyp_b_lo[hj]
                    b_hi = hyp_b_hi[hj]
                    if a_lo <= nul_pos_raw < a_hi:
                        nul_read = b_lo + (nul_pos_raw - a_lo)
                    elif b_lo <= nul_pos_raw < b_hi:
                        nul_read = a_lo + (nul_pos_raw - b_lo)
                    else:
                        nul_read = nul_pos_raw
                    if nul_read < 0 or nul_read >= n_data:
                        continue
                    nul_wi = (nul_off + ks_rotate) % ks_len
                    nul_plain = _numba_temper_byte(mt, nul_wi) ^ data_arr[nul_read]
                    if nul_plain == 0:
                        out_hits[k] = 1
                        break

    # Chunk size for one _numba_scan call: large enough that per-call
    # (JIT dispatch, thread-pool wake-up) overhead is negligible, small
    # enough that progress/cancellation are noticed within a couple of
    # seconds at the measured ~5-6M candidates/sec (see find_seed's
    # docstring) rather than only between much longer spans.
    _NUMBA_CHUNK = 1 << 24  # ~16.8M

    def _search_numba(data: bytes, carry_on, cancel_token, validator=None) -> "int | None":
        """The same search _search below runs, compiled instead of
        numpy-vectorised — see find_seed's docstring for the measured
        speedup. Single-process: numba's own thread pool (parallel=True,
        via prange) already spans every core, so this does NOT also use
        multiprocessing.Pool the way the fallback path does — running both
        at once would over-subscribe every core rather than add throughput.
        Because of that there is also no worker-crash/stall recovery here
        (see _search's own comment on that): there are no separate worker
        processes that can wedge, only this one, in-process compiled loop.
        """
        targets0 = np.array([data[HEADER_LEN + offs[0]] for offs in _CHECK_OFFSET_SETS], dtype=np.uint8)
        targets1 = np.array([data[HEADER_LEN + offs[1]] for offs in _CHECK_OFFSET_SETS], dtype=np.uint8)
        targets2 = np.array([data[HEADER_LEN + offs[2]] for offs in _CHECK_OFFSET_SETS], dtype=np.uint8)
        word_indices = np.array(_NUMBA_WORD_INDICES, dtype=np.int64)
        bases = np.array(_MARKER_CHECK_BASES, dtype=np.int64)
        n_bases = len(_CHECK_OFFSET_SETS)
        data_arr = np.frombuffer(data, dtype=np.uint8)
        n_data = len(data)
        hyp_t0, hyp_t1, hyp_t2, hyp_a_lo, hyp_a_hi, hyp_b_lo, hyp_b_hi = _swap_hyp_arrays(data, n_data)
        anchor_bi = _SWAP_HYP_ANCHOR_BASE
        n_hyp = len(hyp_t0)

        lo = 0
        while lo < (1 << 32):
            if cancel_token is not None and cancel_token.cancelled:
                return None
            if not carry_on():
                return None
            hi = min(lo + _NUMBA_CHUNK, 1 << 32)
            out = np.zeros(hi - lo, dtype=np.uint8)
            _numba_scan(np.int64(lo), np.int64(hi), word_indices, _NUMBA_NEED,
                        targets0, targets1, targets2, bases, n_bases, data_arr,
                        HEADER_LEN, _KS_ROTATE, _KS_LEN, out, anchor_bi,
                        hyp_t0, hyp_t1, hyp_t2, hyp_a_lo, hyp_a_hi, hyp_b_lo,
                        hyp_b_hi, n_hyp)
            for k in np.nonzero(out)[0]:
                candidate = int(lo + k)
                if verify_seed(data, candidate, validator):
                    return candidate
            lo = hi
        return None


def _mt_words_batch(seeds, word_indices):
    """The tempered MT output at each of *word_indices* for a numpy array
    of candidate seeds, batched — the vectorised form of ``_MT19937``
    restricted to exactly the words a caller needs, sharing ONE init
    recurrence (computed only as deep as the deepest word or its M=397
    twist partner requires) across all of them rather than paying for it
    once per word.

    Only the SPECIFIC steps a caller ends up needing (each word index,
    each one's "+1" neighbour, each one's M=397 partner — a handful) are
    kept; the recurrence between them still has to run in full (it is
    sequential, nothing here can skip ahead), but every intermediate step
    is discarded the moment it is not one of those few. Keeping all of
    them, one per index up to the deepest word needed, is not merely
    wasteful: for a check whose partner sits ~400 words in (M=397 puts
    every word this scheme ever checks there — see find_seed's own
    docstring), keeping every step retains on the order of 400 full-size
    arrays at once per batch, one per WORKER, all concurrently — enough to
    exhaust memory on an ordinary machine over a long search, which is
    exactly the failure this was written to stop.

    Returns ``{word_index: uint8 array}``.
    """
    import numpy as np

    mask32 = np.uint32(_MASK32)
    partner_indices = {wi: (wi + _M) % _N for wi in word_indices}
    needed = set()
    for wi in word_indices:
        needed.add(wi)
        needed.add(wi + 1)          # "b" below — the twist step's other word
        needed.add(partner_indices[wi])
    need = max(needed) + 1

    prev = seeds.astype(np.uint32)
    keep = {}
    if 0 in needed:
        keep[0] = prev
    tmp = np.empty_like(prev)
    mul = np.uint32(1812433253)
    for i in range(1, need):
        np.right_shift(prev, np.uint32(30), out=tmp)
        np.bitwise_xor(prev, tmp, out=tmp)
        nxt = (mul * tmp + np.uint32(i)) & mask32
        if i in needed:
            keep[i] = nxt
        prev = nxt

    out = {}
    for wi in word_indices:
        a = keep[wi]
        b = keep[wi + 1]
        partner = keep[partner_indices[wi]]
        y = (a & np.uint32(_UPPER_MASK)) | (b & np.uint32(_LOWER_MASK))
        mt_new = partner ^ (y >> np.uint32(1))
        odd = (y & np.uint32(1)).astype(bool)
        mt_new = np.where(odd, mt_new ^ np.uint32(_MATRIX_A), mt_new)

        y2 = mt_new & mask32
        y2 = y2 ^ (y2 >> np.uint32(11))
        y2 = y2 ^ ((y2 & np.uint32(0xFF3A58AD)) << np.uint32(7))
        y2 = y2 & mask32
        y2 = y2 ^ ((y2 & np.uint32(0xFFFFDF8C)) << np.uint32(15))
        y2 = y2 & mask32
        y2 = y2 ^ (y2 >> np.uint32(18))
        out[wi] = (y2 & np.uint32(0xFF)).astype(np.uint8)
    return out


# Set once per worker process by _init_worker (a multiprocessing.Pool
# initializer), never carried in a task's own arguments — see _search_range
# and _search for why. Also set directly (no Pool involved) by _search's
# own single-worker path, so _search_range's signature stays one shape
# either way.
_worker_data: bytes = b""
_worker_validator = None


def _init_worker(data: bytes, validator=None) -> None:
    global _worker_data, _worker_validator
    _worker_data = data
    _worker_validator = validator


def _search_range(args):
    """One numpy-batched slice of the seed space: the cheap 4-byte filter —
    marker + length shape (see ``_numba_scan``'s docstring for why a 3rd,
    data-dependent NUL-terminator check was added on top of the original
    3-byte one) tried at every base offset in _CHECK_OFFSET_SETS, a
    candidate passing ANY of them survives — and a real decrypt-and-
    decompress the instant one does.

    *args* is ``(lo, hi, batch)`` — deliberately NOT ``(lo, hi, data,
    batch)``. The save being searched (see _worker_data) is set ONCE per
    worker process via the Pool's initializer, not carried in every task's
    own arguments: with ~4,000+ tasks over a full search, sending a copy of
    it (tens of KB) on every single one was the dominant real cost, not the
    actual candidate computation — measured at roughly 5-6x slower through
    the pool than the identical work run in-process, entirely IPC pickling,
    not CPU.
    """
    lo, hi, batch = args
    data = _worker_data
    validator = _worker_validator
    import numpy as np

    data_arr = np.frombuffer(data, dtype=np.uint8)
    n_data = len(data)
    # Every rotated word position, not just the ~42 the fixed 3-byte check
    # needs: the NUL-terminator check's position depends on each
    # candidate's own decrypted length byte, so it can land anywhere in
    # [0, _KS_LEN) after rotation — see _numba_scan's docstring, this is
    # the numpy-batched equivalent of the same fix.
    word_indices = list(range(_KS_LEN))
    targets_by_base = [tuple(data[HEADER_LEN + off] for off in offs)
                       for offs in _CHECK_OFFSET_SETS]

    hyp_wi0, hyp_wi1, hyp_wi2 = (
        (off + _KS_ROTATE) % _KS_LEN for off in _CHECK_OFFSET_SETS[_SWAP_HYP_ANCHOR_BASE])
    hyp_t0, hyp_t1, hyp_t2, hyp_a_lo, hyp_a_hi, hyp_b_lo, hyp_b_hi = _swap_hyp_arrays(data, n_data)

    seed = lo
    while seed < hi:
        top = min(seed + batch, hi)
        seeds = np.arange(seed, top, dtype=np.uint32)
        n = len(seeds)
        words = _mt_words_batch(seeds, word_indices)
        stacked = np.stack([words[i] for i in range(_KS_LEN)])  # (KS_LEN, n)
        arange_n = np.arange(n)

        combined = np.zeros(n, dtype=bool)
        for bi, (offs, (t0, t1, t2)) in enumerate(zip(_CHECK_OFFSET_SETS, targets_by_base)):
            wi0, wi1, wi2 = ((off + _KS_ROTATE) % _KS_LEN for off in offs)
            plain0 = stacked[wi0] ^ np.uint8(t0)
            plain1 = stacked[wi1] ^ np.uint8(t1)
            plain2 = stacked[wi2] ^ np.uint8(t2)
            base_mask = ((plain0 == np.uint8(_BODY_MARKER))
                         & (plain2 == np.uint8(0))
                         & (plain1 >= np.uint8(1)) & (plain1 <= np.uint8(_NAME_LEN_MAX)))
            if not base_mask.any():
                continue
            b0 = _MARKER_CHECK_BASES[bi]
            nul_off = b0 + 2 + plain1.astype(np.int64)
            nul_pos = HEADER_LEN + nul_off
            in_bounds = nul_pos < n_data
            safe_pos = np.where(in_bounds, nul_pos, 0)
            nul_wi = (nul_off + _KS_ROTATE) % _KS_LEN
            nul_word = stacked[nul_wi, arange_n]
            nul_plain = nul_word ^ data_arr[safe_pos]
            base_mask = base_mask & in_bounds & (nul_plain == 0)
            combined |= base_mask

        # The swap-hypothesis hedge (see _SWAP_HYP_ANCHOR_BASE's own
        # comment): re-run the SAME anchor-base check, but reading each
        # hypothesis's own precomputed "what if a swap already happened
        # here" bytes instead of the raw file. wi0/wi1/wi2 stay the ones
        # already computed above (the keystream index depends only on the
        # destination position being solved for, never on where its
        # ciphertext byte is read from).
        for t0, t1, t2, a_lo, a_hi, b_lo, b_hi in zip(
                hyp_t0, hyp_t1, hyp_t2, hyp_a_lo, hyp_a_hi, hyp_b_lo, hyp_b_hi):
            plain0 = stacked[hyp_wi0] ^ np.uint8(t0)
            plain1 = stacked[hyp_wi1] ^ np.uint8(t1)
            plain2 = stacked[hyp_wi2] ^ np.uint8(t2)
            base_mask = ((plain0 == np.uint8(_BODY_MARKER))
                         & (plain2 == np.uint8(0))
                         & (plain1 >= np.uint8(1)) & (plain1 <= np.uint8(_NAME_LEN_MAX)))
            if not base_mask.any():
                continue
            nul_off = _SWAP_HYP_ANCHOR_BASE + 2 + plain1.astype(np.int64)
            nul_pos = HEADER_LEN + nul_off
            in_a = (a_lo <= nul_pos) & (nul_pos < a_hi)
            in_b = (b_lo <= nul_pos) & (nul_pos < b_hi)
            nul_read = np.where(in_a, b_lo + (nul_pos - a_lo),
                        np.where(in_b, a_lo + (nul_pos - b_lo), nul_pos))
            in_bounds = (nul_pos < n_data) & (nul_read >= 0) & (nul_read < n_data)
            safe_pos = np.where(in_bounds, nul_read, 0)
            nul_wi = (nul_off + _KS_ROTATE) % _KS_LEN
            nul_word = stacked[nul_wi, arange_n]
            nul_plain = nul_word ^ data_arr[safe_pos]
            base_mask = base_mask & in_bounds & (nul_plain == 0)
            combined |= base_mask

        for candidate in seeds[combined]:
            if verify_seed(data, int(candidate), validator):
                return int(candidate)
        seed = top
    return None


def _swap_hyp_arrays(data: bytes, n_data: int):
    """Precompute, once per search (not per candidate seed — these depend
    only on the file's own bytes and the hypothesis grid, never on the
    seed being tested), the "what would _SWAP_HYP_ANCHOR_BASE's 3-byte
    window read as if THIS (a_rel, gap) swap had already happened"
    targets — see _SWAP_HYP_ANCHOR_BASE's own comment for why this,
    specifically, is worth checking.

    Returns 7 same-length numpy arrays — ``t0, t1, t2`` (uint8) and
    ``a_lo, a_hi, b_lo, b_hi`` (int64) — one entry per hypothesis whose 3
    window positions all land inside the file (a hypothesis that doesn't
    is simply dropped, not kept as a guaranteed non-match). Array-shaped,
    not a list of tuples, so both the numpy (_search_range) and numba
    (_numba_scan) cheap-filter paths can consume the exact same values
    rather than risk drifting apart — see _SWAP_HYP_ANCHOR_BASE's comment
    on why the two must never silently check different bytes.
    """
    offs = _CHECK_OFFSET_SETS[_SWAP_HYP_ANCHOR_BASE]
    positions = tuple(HEADER_LEN + off for off in offs)
    t0s, t1s, t2s, a_los, a_his, b_los, b_his = [], [], [], [], [], [], []
    for a_rel, gap in _SWAP_HYP_PAIRS:
        a_lo = HEADER_LEN + a_rel
        a_hi = a_lo + _SWAP_LEN
        b_lo = a_lo + gap
        b_hi = b_lo + _SWAP_LEN
        reads = []
        ok = True
        for pos in positions:
            if a_lo <= pos < a_hi:
                read_pos = b_lo + (pos - a_lo)
            elif b_lo <= pos < b_hi:
                read_pos = a_lo + (pos - b_lo)
            else:
                read_pos = pos
            if not (0 <= read_pos < n_data):
                ok = False
                break
            reads.append(data[read_pos])
        if ok:
            t0s.append(reads[0]); t1s.append(reads[1]); t2s.append(reads[2])
            a_los.append(a_lo); a_his.append(a_hi); b_los.append(b_lo); b_his.append(b_hi)
    return (np.array(t0s, dtype=np.uint8), np.array(t1s, dtype=np.uint8),
            np.array(t2s, dtype=np.uint8), np.array(a_los, dtype=np.int64),
            np.array(a_his, dtype=np.int64), np.array(b_los, dtype=np.int64),
            np.array(b_his, dtype=np.int64))


def _search(data: bytes, n_workers: int, carry_on, cancel_token=None, validator=None) -> "int | None":
    if _HAVE_NUMBA:
        return _search_numba(data, carry_on, cancel_token, validator)
    # 131,072 candidates per numpy pass — small enough that the working
    # set (candidates plus the handful of MT-recurrence arrays kept per
    # step, see _mt_words_batch) stays resident in a core's own cache for
    # the whole batch instead of round-tripping through RAM on every one
    # of the ~400 recurrence steps. Measured, not assumed: on real
    # hardware this batch size raised throughput roughly 2.5-3.5x over the
    # original 1<<20 (4 MiB/array — well past typical L2) once workers
    # stopped being memory-bandwidth-bound on each other, which is also
    # why _physical_cpu_count exists — see find_seed's docstring.
    batch = 1 << 17
    span = (1 << 32) // max(n_workers, 1)
    ranges = []
    lo = 0
    for w in range(n_workers):
        hi = (lo + span) if w < n_workers - 1 else (1 << 32)
        ranges.append((lo, hi))
        lo = hi

    if n_workers <= 1:
        _init_worker(data, validator)
        lo, hi = ranges[0]
        seed = lo
        while seed < hi:
            if not carry_on():
                return None
            top = min(seed + batch, hi)
            found = _search_range((seed, top, batch))
            if found is not None:
                return found
            seed = top
        return None

    import multiprocessing as mp

    # Each worker covers its own slice in ``batch``-sized steps, so the pool
    # can be polled for cancellation between rounds rather than only once
    # every worker's entire slice finishes. Task tuples carry only the
    # range and batch size, never the save's own bytes — see
    # _search_range's docstring for why that used to dominate real cost.
    per_round = []
    for lo, hi in ranges:
        chunks = []
        s = lo
        while s < hi:
            top = min(s + batch, hi)
            chunks.append((s, top, batch))
            s = top
        per_round.append(chunks)
    max_rounds = max(len(c) for c in per_round)

    # Polling, not iterating imap_unordered() directly: terminate() on a
    # pool a consumer thread is blocked inside next() on does NOT reliably
    # unblock that thread — confirmed directly, not assumed (it can hang
    # well past 30s). apply_async() + a short, bounded wait per AsyncResult
    # means this loop is never blocked for more than _POLL_S at a time
    # REGARDLESS of whether terminate() behaves, so a cancellation is
    # noticed on the next poll no matter what the pool itself is doing.
    _POLL_S = 0.05
    # A worker that dies mid-task (observed directly: a segfault-class
    # crash) gets auto-replaced by Pool itself — but on Windows that
    # replacement can come up wedged: alive, but stuck before it ever pulls
    # a task from the shared queue (seen pinned at 1 thread / ~0 CPU for
    # 10+ minutes while the surviving original workers kept working
    # normally). That is an OS/runtime-level hang outside this module's
    # control, not something retrying the task itself fixes — the fix is
    # noticing the round has stopped making ANY progress and rebuilding
    # the whole pool, which also replaces every worker including the
    # wedged one. Only chunks still unresolved at that point are
    # resubmitted; anything already confirmed done is never redone.
    _STALL_TIMEOUT_S = 45.0
    ctx = mp.get_context("spawn")

    def _new_pool():
        p = ctx.Pool(n_workers, initializer=_init_worker, initargs=(data, validator))
        if cancel_token is not None:
            # terminate() still gets called — it is what actually frees
            # the CPU these workers would otherwise keep using — it is
            # just no longer what this loop's own responsiveness depends
            # on; that is cancel_token.cancelled, checked directly below.
            cancel_token._register(p)
        return p

    pool = _new_pool()
    try:
        for r in range(max_rounds):
            if not carry_on() or (cancel_token is not None and cancel_token.cancelled):
                return None
            round_args = [chunks[r] for chunks in per_round if r < len(chunks)]
            pending = [(pool.apply_async(_search_range, (args,)), args)
                       for args in round_args]
            last_progress = time.monotonic()
            while pending:
                if not carry_on() or (cancel_token is not None and cancel_token.cancelled):
                    return None
                still_pending = []
                progressed = False
                for ar, args in pending:
                    if not ar.ready():
                        still_pending.append((ar, args))
                        continue
                    progressed = True
                    try:
                        result = ar.get()
                    except Exception:
                        # A worker that DIES outright (as opposed to
                        # hanging — see the stall-timeout path below, for
                        # that case) has its AsyncResult marked ready with
                        # this exception almost immediately by Pool
                        # itself, well under the stall timeout. Dropping
                        # it here — confirmed as a real, not hypothetical,
                        # gap — would leave this chunk's whole slice of
                        # keyspace silently never checked, turning a
                        # crashed worker into a false "not found". The
                        # pool is still alive (only this one task's
                        # worker died); resubmit the SAME chunk to it
                        # immediately rather than waiting on the stall
                        # timeout for something that already failed.
                        logger.warning(
                            "wolf_lz4 seed search: a worker died mid-chunk, "
                            "resubmitting it")
                        still_pending.append(
                            (pool.apply_async(_search_range, (args,)), args))
                        continue
                    if result is not None:
                        return result
                pending = still_pending
                if progressed:
                    last_progress = time.monotonic()
                elif pending and (time.monotonic() - last_progress) > _STALL_TIMEOUT_S:
                    logger.warning(
                        "wolf_lz4 seed search: no progress in %.0fs, "
                        "rebuilding worker pool (%d chunk(s) re-queued)",
                        _STALL_TIMEOUT_S, len(pending))
                    old_pool = pool
                    if cancel_token is not None:
                        cancel_token._unregister()
                    try:
                        old_pool.terminate()
                    except Exception:
                        pass
                    pool = _new_pool()
                    pending = [(pool.apply_async(_search_range, (args,)), args)
                               for _, args in pending]
                    last_progress = time.monotonic()
                if pending:
                    time.sleep(_POLL_S)
    finally:
        if cancel_token is not None:
            cancel_token._unregister()
        try:
            pool.terminate()
        except Exception:
            pass
    return None
