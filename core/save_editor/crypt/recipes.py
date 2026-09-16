"""Generic unwrap recipes for a save nothing in the registry recognises.

Grew out of reverse-engineering ``core.engines.wolf_lz4`` by hand: a short,
reversible byte transform (an XOR keystream, sometimes seeded from a header
byte) wraps content a normal reader would already understand once the
transform is undone, optionally with ordinary compression in between. This
module is the mechanical shape of that generalised — a small, bounded
battery of transforms to try, not a live attack on a running game (see
``core.save_editor.save_editor``'s own module docstring for why that line is
never crossed).

Two stages, composed:

- **Stage A** — a reversible whole-buffer transform: identity, a repeating
  single-byte XOR (solved from the data itself where the target is a plain
  magic byte, brute-forced over all 256 keys otherwise), or an MSVCRT
  ``rand()``-style LCG keystream seeded from one of the file's own early
  bytes (same recurrence ``core.engines.wolf`` uses, offered with both a
  Wolf-shaped narrow extraction and a generic full-byte one — see
  ``_lcg_extract_narrow``/``_lcg_extract_full``).
- **Stage B** — decompression: identity, zlib, raw deflate, or LZ4 *frame*
  (never block — a block decoder needs an out-of-band output size no
  generic recipe has), tried at a few small offsets into Stage A's output.

Every Stage A candidate is streaming/position-local (a repeating key, or a
keystream generated from a fixed start point), so transforming a small
prefix equals the prefix of transforming the whole file. ``find_candidates``
uses that: a cheap shape check (``_looks_structured``) runs against just
that prefix for every (Stage A x Stage B) combination, and only a survivor
pays for materialising the full buffer and the real decompressor.
"""
import json
import struct
import zlib
from collections import Counter

# A prefix this long is enough for every magic-byte test below, and enough
# bytes for the printable-ratio test to mean something, while staying cheap
# to transform ~11,500 times over.
PREFIX_LEN = 4096
# A save needing more than this is a small blob, not an archive — matches
# the cap RecipeFormat holds the whole search to.
MAX_RECIPE_BYTES = 4 * 1024 * 1024
# Bound on what the CHEAP prefix-only decompression attempts are allowed to
# produce — these run on untrusted, possibly-adversarial small inputs, and
# nothing about them should be allowed to allocate more than a small
# multiple of the prefix itself.
_CHEAP_MAX_OUT = 256 * 1024

# The printable-ratio arm of _looks_structured is the one non-deterministic
# knob in an otherwise all-magic-bytes gate. At least this many bytes must be
# available before it is trusted at all — a short buffer trivially passes
# any ratio test — and of those, this fraction must be plain ASCII text.
_MIN_PRINTABLE_PREFIX = 64
_PRINTABLE_RATIO = 0.95
_PRINTABLE_BYTES = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
# Real text has some variety of bytes even when it clears the printable-ratio
# bar above. Degenerate content — a buffer that's mostly one padding byte
# (NUL, a run of spaces, ...) — can still clear that bar after an XOR: e.g.
# XORing a mostly-NUL buffer with a key that maps NUL to TAB or space is
# "95% printable" while carrying zero real information. Reject a prefix
# where a single byte value accounts for more than this fraction of it.
_MAX_DOMINANT_BYTE_FRACTION = 0.5

# Plausible clear-header lengths to try starting a transform from: no header,
# one tag byte (the shape core.engines.wolf_lz4's own tag byte takes), a
# 4-byte length/version field, a 16-byte magic/GUID-sized header.
_START_OFFSETS = (0, 1, 4, 16)
# Magic bytes a plaintext container opens with — see registry.py's own sniff
# tests for JSON/XML/zip/SQLite/RubyMarshal, mirrored here so a solved XOR
# key can be computed in closed form instead of brute-forced.
_SOLVED_TARGETS = (0x7B, 0x5B, 0x3C, 0x50, 0x53, 0x04)  # { [ < P S \x04

# The Microsoft C runtime's LCG — same constants as core.engines.wolf, but
# NOT imported from there: that module's own keystream cache and narrow,
# Wolf-specific extraction are unsuited to being hammered by a generic
# search (see the module docstring above).
_LCG_MUL, _LCG_ADD = 214013, 2531011
_LCG_SEED_BYTES = 16

# Small offsets into Stage A's output to look for a compressed stream at —
# 0 (right up against the transform), 4 and 8 (past one or two little-endian
# length fields, the shape core.engines.wolf_lz4's own scheme uses).
_STAGE_B_OFFSETS = (0, 4, 8)


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    """*data* XORed with an equal-length *key*, done as one big integer op
    rather than a byte at a time — the only way this stays cheap at the
    survivor stage, where *data* can be the whole (up to 4 MiB) buffer."""
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(len(data), "big")


def _xor_keystream(key_byte: int):
    def ks(n: int) -> bytes:
        return bytes([key_byte]) * n
    return ks


def _lcg_extract_narrow(state: int) -> int:
    """core.engines.wolf's own extraction — 3 bits, values 0-7. Offered so a
    Wolf-shaped save that somehow reached this search (rather than
    WolfFormat) is not silently unreachable, but it only ever rediscovers
    saves that format already owns."""
    return (state >> 28) & 7


def _lcg_extract_full(state: int) -> int:
    """A generic full-byte extraction — the one worth trying for anything
    that is NOT specifically Wolf RPG Editor's own scheme."""
    return (state >> 16) & 0xFF


def _lcg_keystream(seed_byte: int, extract):
    def ks(n: int) -> bytes:
        state = seed_byte & 0xFFFFFFFF
        out = bytearray(n)
        for i in range(n):
            state = (state * _LCG_MUL + _LCG_ADD) & 0xFFFFFFFF
            out[i] = extract(state)
        return bytes(out)
    return ks


def _keystream_for(ks_kind: str, ks_param: int):
    if ks_kind == "xor":
        return _xor_keystream(ks_param)
    if ks_kind == "lcg_narrow":
        return _lcg_keystream(ks_param, _lcg_extract_narrow)
    if ks_kind == "lcg_full":
        return _lcg_keystream(ks_param, _lcg_extract_full)
    raise ValueError(f"unknown Stage A kind {ks_kind!r}")


def stage_a_candidates(data: bytes):
    """Yield ``(label, offset, keystream_fn, spec)``.

    *keystream_fn(n)* returns exactly *n* keystream bytes, deterministically
    — calling it again with a larger *n* reproduces the same leading bytes,
    which is what lets a prefix computed for the cheap gate be trusted as
    the prefix of the full transform. *spec* is ``(offset, kind, param)``, a
    plain, re-derivable identity for this same candidate — see
    ``apply_recipe``, which is what a cache hit uses to redo one specific
    recipe without repeating the search that first found it. Ordered
    cheapest/most-likely first: identity, solved XOR keys, the full XOR
    brute sweep, then the LCG.
    """
    seen = set()

    yield ("identity", 0, _xor_keystream(0), (0, "xor", 0))
    seen.add((0, 0))

    for offset in _START_OFFSETS:
        if offset >= len(data):
            continue
        first = data[offset]
        for target in _SOLVED_TARGETS:
            key = first ^ target
            if (offset, key) in seen:
                continue
            seen.add((offset, key))
            yield (f"xor_solved_key=0x{key:02x}_off={offset}", offset,
                   _xor_keystream(key), (offset, "xor", key))

    for offset in _START_OFFSETS:
        if offset >= len(data):
            continue
        for key in range(1, 256):
            if (offset, key) in seen:
                continue
            seen.add((offset, key))
            yield (f"xor_key=0x{key:02x}_off={offset}", offset,
                   _xor_keystream(key), (offset, "xor", key))

    for seed_index in range(min(_LCG_SEED_BYTES, len(data))):
        seed_byte = data[seed_index]
        for offset in _START_OFFSETS:
            if offset >= len(data):
                continue
            for ext_label, ks_kind in (("narrow", "lcg_narrow"), ("full", "lcg_full")):
                extract = _lcg_extract_narrow if ks_kind == "lcg_narrow" else _lcg_extract_full
                yield (f"lcg_seed=data[{seed_index}]_off={offset}_{ext_label}", offset,
                       _lcg_keystream(seed_byte, extract), (offset, ks_kind, seed_byte))


def _zlib_decompress(payload: bytes, max_size: int) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(payload, max_size)
    if d.unconsumed_tail:
        raise ValueError("zlib output exceeds cap")
    return out


def _raw_deflate_decompress(payload: bytes, max_size: int) -> bytes:
    d = zlib.decompressobj(-15)
    out = d.decompress(payload, max_size)
    if d.unconsumed_tail:
        raise ValueError("deflate output exceeds cap")
    return out


def _lz4_frame_decompress(payload: bytes, max_size: int) -> bytes:
    import lz4.frame
    out = lz4.frame.decompress(payload)
    if len(out) > max_size:
        raise ValueError("lz4 frame output exceeds cap")
    return out


# label -> decode(payload, max_size) -> bytes, raises on failure. Identity is
# handled directly by find_candidates, not through this table.
_STAGE_B_CODECS = (
    ("zlib", _zlib_decompress),
    ("raw-deflate", _raw_deflate_decompress),
    ("lz4-frame", _lz4_frame_decompress),
)


def _looks_like_zlib(prefix: bytes) -> bool:
    """A valid zlib header — cheap enough to skip an actual decompression
    attempt on data that obviously is not zlib at all."""
    if len(prefix) < 2:
        return False
    cmf, flg = prefix[0], prefix[1]
    return (cmf & 0x0F) == 8 and ((cmf << 8) | flg) % 31 == 0


def _looks_like_lz4_frame(prefix: bytes) -> bool:
    return prefix[:4] == b"\x04\x22\x4D\x18"


def _looks_structured(prefix: bytes, complete: bool) -> bool:
    """Cheap shape test for a FINAL candidate output's prefix — magic bytes
    for the containers a registered reader would recognise (JSON, XML, zip,
    SQLite, RubyMarshal — mirroring registry.py's own sniff tests), or, only
    once enough of the prefix is available to mean something, a printable-
    ASCII ratio. Never applied to a still-compressed buffer as a proxy for
    "is this worth decompressing" — see _looks_like_zlib/_looks_like_lz4_frame
    for that question; this one is only ever asked of what a recipe would
    actually hand to the registry.

    *complete* is True when *prefix* is the WHOLE candidate output, not a
    slice truncated for cost reasons (see find_candidates) — a solved XOR
    key (see stage_a_candidates) guarantees the FIRST byte of a JSON/XML
    magic by construction, so accepting on that byte alone would accept
    pure noise every time a solved key happens to be tried; a real parse of
    the complete content is cheap (the prefix cap is generous relative to
    the small blobs this is for) and has no such tautology. A JSON/XML
    magic seen on a TRUNCATED prefix falls back to the printable-ratio bar
    below instead, same as content with no recognised magic at all — zip,
    SQLite and RubyMarshal's own magics need 2-16 bytes a solved key does
    not also guarantee, which is a different, much stronger bar already.
    """
    if not prefix:
        return False
    if prefix[:4] == b"PK\x03\x04":
        return True
    if prefix[:16] == b"SQLite format 3\x00":
        return True
    if prefix[:2] == b"\x04\x08":
        return True
    if complete:
        head = prefix.lstrip()[:1]
        if head in (b"{", b"["):
            try:
                json.loads(prefix)
                return True
            except Exception:
                pass
        if prefix[:200].lstrip()[:1] == b"<":
            try:
                import xml.etree.ElementTree as ET
                ET.fromstring(prefix)
                return True
            except Exception:
                pass
    if len(prefix) >= _MIN_PRINTABLE_PREFIX:
        printable = sum(1 for b in prefix if b in _PRINTABLE_BYTES)
        if printable / len(prefix) >= _PRINTABLE_RATIO:
            dominant = max(Counter(prefix).values())
            if dominant / len(prefix) <= _MAX_DOMINANT_BYTE_FRACTION:
                return True
    return False


def stage_b_candidates():
    """``(label, offset, codec)`` for every non-identity Stage B — identity
    is handled directly by find_candidates, since it needs no offset
    stepping or decode attempt."""
    for offset in _STAGE_B_OFFSETS:
        for label, decode in _STAGE_B_CODECS:
            yield (label, offset, decode)


def find_candidates(data: bytes):
    """Yield ``(description, spec, payload)`` for every (Stage A, Stage B)
    combination whose output prefix passes the cheap shape gate — in the
    order Stage A/Stage B candidates are generated, so the caller trying
    each in turn and stopping at the first real hit already tries the most
    likely recipes first.

    *spec* is ``(offset_a, ks_kind, ks_param, label_b, offset_b)`` — a
    plain, re-derivable identity for the recipe that produced *payload*; see
    ``apply_recipe``. *payload* is the fully materialised buffer — only
    ever built for a combination that already passed the gate on its own
    small prefix, which is what keeps this cheap over roughly 10⁴
    combinations.
    """
    if not data or len(data) > MAX_RECIPE_BYTES:
        return
    prefix_len = min(PREFIX_LEN, len(data))

    for label_a, offset_a, ks_fn, spec_a in stage_a_candidates(data):
        src_prefix = data[offset_a:offset_a + prefix_len]
        if not src_prefix:
            continue
        prefix_a = _xor_bytes(src_prefix, ks_fn(len(src_prefix)))
        # Whether prefix_a is the WHOLE remaining buffer or a slice
        # truncated for cost reasons — see _looks_structured's docstring
        # for why that distinction matters.
        complete_a = (offset_a + prefix_len) >= len(data)

        if _looks_structured(prefix_a, complete_a):
            full_src = data[offset_a:]
            full_a = _xor_bytes(full_src, ks_fn(len(full_src)))
            spec = spec_a + ("identity", 0)
            yield (f"{label_a}::identity", spec, full_a)

        for offset_b in _STAGE_B_OFFSETS:
            sub_prefix = prefix_a[offset_b:]
            if not sub_prefix:
                continue
            for label_b, decode in _STAGE_B_CODECS:
                if label_b == "zlib" and not _looks_like_zlib(sub_prefix):
                    continue
                if label_b == "lz4-frame" and not _looks_like_lz4_frame(sub_prefix):
                    continue
                try:
                    out_prefix = decode(sub_prefix, _CHEAP_MAX_OUT)
                except Exception:
                    continue
                # A successful decode already proves out_prefix was not cut
                # short BY THE DECODE (see _zlib_decompress et al raising on
                # unconsumed_tail) — the only remaining way it could be
                # incomplete is sub_prefix itself having been truncated.
                if not _looks_structured(out_prefix, complete_a):
                    continue
                full_src = data[offset_a:]
                full_a = _xor_bytes(full_src, ks_fn(len(full_src)))
                sub_full = full_a[offset_b:]
                try:
                    out_full = decode(sub_full, MAX_RECIPE_BYTES)
                except Exception:
                    continue
                spec = spec_a + (label_b, offset_b)
                yield (f"{label_a}::{label_b}_off={offset_b}", spec, out_full)


def _decode_by_label(label_b: str):
    for label, decode in _STAGE_B_CODECS:
        if label == label_b:
            return decode
    return None


def _compress_for(label_b: str, payload: bytes) -> bytes:
    if label_b == "identity":
        return payload
    if label_b == "zlib":
        return zlib.compress(payload, 9)
    if label_b == "raw-deflate":
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        return co.compress(payload) + co.flush()
    if label_b == "lz4-frame":
        import lz4.frame
        return lz4.frame.compress(payload)
    raise ValueError(f"unknown Stage B label {label_b!r}")


def _pack_gap(offset_b: int, compressed_len: int, uncompressed_len: int) -> bytes:
    """The small field, if any, ``_STAGE_B_OFFSETS`` reserves before the
    compressed stream: none at 0, a 4-byte compressed length at 4, or the
    (uncompressed, compressed) length pair at 8 — the exact shape
    core.engines.wolf_lz4's own format uses. A generic recipe cannot know
    for certain that a game reads this field as a length rather than, say, a
    checksum — but a length is what every known real-world case of this
    shape (wolf_lz4 included) turns out to be, and writing a self-consistent
    one is strictly more honest than leaving the ORIGINAL file's now-stale
    value sitting under new content of a different size.
    """
    if offset_b == 0:
        return b""
    if offset_b == 4:
        return struct.pack("<I", compressed_len)
    if offset_b == 8:
        return struct.pack("<II", uncompressed_len, compressed_len)
    raise ValueError(f"unsupported Stage B offset {offset_b}")


def lock_recipe(payload: bytes, spec: tuple, header: bytes) -> bytes:
    """The reverse of the recipe *spec* identifies: recompress *payload*,
    rebuild the small gap (see ``_pack_gap``) that sits before it, then
    re-apply Stage A's keystream (XOR is its own inverse) over the whole
    thing. *header* is the untouched clear-header bytes Stage A never
    reaches, exactly as they were read off the original file — never
    recomputed, since a generic recipe does not know what they mean.
    """
    offset_a, ks_kind, ks_param, label_b, offset_b = spec
    ks_fn = _keystream_for(ks_kind, ks_param)
    compressed = _compress_for(label_b, payload)
    gap = _pack_gap(offset_b, len(compressed), len(payload))
    full_a_new = gap + compressed
    full_data_new = _xor_bytes(full_a_new, ks_fn(len(full_a_new)))
    return header + full_data_new


def apply_recipe(data: bytes, spec: tuple):
    """Re-apply one recipe ``find_candidates`` found earlier to fresh
    *data* sharing the same header — a game's obfuscation shows up in its
    header and is constant across save slots, so a recipe that unwrapped
    one save from a slot unwraps every other one the same way, without
    repeating the O(10⁴) search that first found it. Returns ``None`` on
    any failure — a changed or too-short file falls back to the caller
    re-running the full search, never trusted blindly (see RecipeFormat).
    """
    offset_a, ks_kind, ks_param, label_b, offset_b = spec
    if offset_a >= len(data):
        return None
    try:
        ks_fn = _keystream_for(ks_kind, ks_param)
    except ValueError:
        return None
    full_src = data[offset_a:]
    full_a = _xor_bytes(full_src, ks_fn(len(full_src)))
    if label_b == "identity":
        return full_a
    sub_full = full_a[offset_b:]
    decode = _decode_by_label(label_b)
    if decode is None or not sub_full:
        return None
    try:
        return decode(sub_full, MAX_RECIPE_BYTES)
    except Exception:
        return None
