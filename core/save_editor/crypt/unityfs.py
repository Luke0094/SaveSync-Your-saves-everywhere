"""Reading the strings out of a Unity asset bundle.

A built Unity game keeps its data either in plain ``.assets`` files, which
are readable as they are, or in a ``UnityFS`` bundle, which is the same thing
squeezed. Only the first kind can be searched by looking at the bytes; the
second has to be unsqueezed first, and that is all this module does.

It does NOT parse the bundle's structure beyond what unsqueezing needs: no
object table, no type tree, no assets. The caller is looking for one string,
so the blocks are decompressed, joined and handed back for searching. That
keeps this a hundred lines instead of a library.

Three compressions appear in the wild and all three are handled: none, LZMA
(the standard library does it) and LZ4 (the ``lz4`` package does it — a
project dependency already, for wolf_lz4, so there is no longer a reason to
hand-roll this one the way an earlier version of this module did: that was
~60 lines of a literal/back-reference walker in pure Python, replaced with
one call into the same C decoder, measured 5x+ faster on top of being less
code to maintain — see git history / intel/wolf_lz4_findings.md if the old
version is ever wanted for reference). Anything else is declined rather
than guessed at.
"""
import logging
import lzma
import struct

import lz4.block

logger = logging.getLogger(__name__)

SIGNATURE = b"UnityFS"

_COMPRESSION_MASK = 0x3F
_NONE, _LZMA, _LZ4, _LZ4HC = 0, 1, 2, 3
# The block list can be written at the END of the file rather than after the
# header; this flag in the header says so.
_INFO_AT_END = 0x80
# …and this one says the blocks themselves start on a sixteen-byte boundary,
# which every bundle this was checked against sets. Reading from where the
# list happened to end instead lands a few bytes early, and the compressed
# data then decodes into nonsense rather than failing outright — so it must
# be honoured, not assumed away.
_PAD_BEFORE_BLOCKS = 0x200
# Bundles are big. These bound what one file may cost before it is declined —
# the point is to find a short string, not to unpack a game.
_MAX_BUNDLE = 512 << 20
_MAX_UNPACKED = 256 << 20


class UnityFsError(Exception):
    pass


def _lz4_block(src: bytes, out_size: int) -> bytes:
    """Unity's raw LZ4 block — the same shape ``core.engines.wolf_lz4``
    already depends on the ``lz4`` package for, so this is a thin wrapper
    rather than a second decoder: ``uncompressed_size`` is documented as a
    MAXIMUM, not a guarantee ("less data may be returned" on a short/wrong
    stream — the same gap wolf_lz4.decompress's own docstring already
    guards against), so the exact-length check below is load-bearing, not
    a formality.
    """
    try:
        out = lz4.block.decompress(src, uncompressed_size=out_size or -1)
    except lz4.block.LZ4BlockError as e:
        raise UnityFsError(f"the LZ4 block will not unpack: {e}") from e
    if out_size and len(out) != out_size:
        raise UnityFsError(f"unpacked to {len(out)} bytes, not {out_size}")
    return out


def _decompress(data: bytes, kind: int, out_size: int) -> bytes:
    if kind == _NONE:
        return data
    if kind in (_LZ4, _LZ4HC):
        return _lz4_block(data, out_size)
    if kind == _LZMA:
        # Unity writes the five property bytes and then the stream, with the
        # length known from outside — which is lzma's "alone" format with an
        # unknown size field.
        if len(data) < 5:
            raise UnityFsError("the LZMA header is too short")
        dec = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[lzma._decode_filter_properties(lzma.FILTER_LZMA1, data[:5])])
        return dec.decompress(data[5:], out_size)
    raise UnityFsError(f"compression {kind} is not one this reads")


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def cstring(self) -> bytes:
        end = self.d.index(b"\0", self.i)
        out = self.d[self.i:end]
        self.i = end + 1
        return out

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.d, self.i)[0]
        self.i += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from(">Q", self.d, self.i)[0]
        self.i += 8
        return v

    def align(self, n: int) -> None:
        self.i = (self.i + n - 1) & ~(n - 1)


def unpack(data: bytes, stop_after: bytes = b"", on_tick=None) -> bytes:
    """The bundle's contents, joined. Raises UnityFsError if it cannot be.

    Unpacking is not free — the LZ4 decoder here is Python, and a bundle can
    be hundreds of megabytes — so a caller that is hunting for one string
    should say so:

    *stop_after* is a marker to look for as the blocks come out. Once it has
    been seen, one more block is taken (so whatever follows it is present
    too) and the rest of the bundle is left alone.
    *on_tick* is called between blocks and stops the unpacking if it returns
    False. That is where a person waiting gets to call it off — there is no
    limit chosen here, because stopping early means answering "no password"
    when the answer was a few seconds further on.

    Neither changes what is returned when the marker is absent and no tick is
    given: the whole thing.
    """
    if not data.startswith(SIGNATURE) or len(data) > _MAX_BUNDLE:
        raise UnityFsError("not a UnityFS bundle")
    r = _Reader(data)
    r.cstring()                      # signature
    version = r.u32()
    r.cstring()                      # the Unity version it was built with
    r.cstring()                      # and its revision
    r.u64()                          # total size, unused here
    info_packed = r.u32()
    info_unpacked = r.u32()
    flags = r.u32()
    if version >= 7:
        r.align(16)

    if flags & _INFO_AT_END:
        # info_packed == 0 here would make data[-0:] return the WHOLE
        # buffer instead of an empty slice (Python quirk: -0 == 0) — a
        # degenerate bundle would then feed the entire file to _decompress
        # as "info", which fails there anyway (read-only path: worst case
        # is key search failing on that bundle, not data loss), but there's
        # no reason to rely on that when an explicit empty slice is exact.
        raw_info = data[-info_packed:] if info_packed else b""
    else:
        raw_info = data[r.i:r.i + info_packed]
        r.i += info_packed
    info = _decompress(raw_info, flags & _COMPRESSION_MASK, info_unpacked)

    ir = _Reader(info)
    ir.i += 16                       # a hash of the contents
    count = ir.u32()
    blocks = []
    for _ in range(count):
        unpacked = ir.u32()
        packed = ir.u32()
        kind = struct.unpack_from(">H", ir.d, ir.i)[0]
        ir.i += 2
        blocks.append((unpacked, packed, kind & _COMPRESSION_MASK))

    total = sum(b[0] for b in blocks)
    if total > _MAX_UNPACKED:
        raise UnityFsError("the bundle unpacks to more than we will hold")

    if flags & _PAD_BEFORE_BLOCKS:
        r.align(16)
    out = bytearray()
    pos = r.i
    seen_marker = False
    for unpacked, packed, kind in blocks:
        chunk = data[pos:pos + packed]
        pos += packed
        out += _decompress(chunk, kind, unpacked)
        if seen_marker:
            break                    # the one extra block has now been taken
        if stop_after and stop_after in out:
            seen_marker = True
        elif on_tick is not None:
            import time
            time.sleep(0.001)        # release GIL to keep GUI thread at 60 FPS
            if on_tick() is False:
                break
    return bytes(out)
