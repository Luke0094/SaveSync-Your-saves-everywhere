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
(the standard library does it) and LZ4, whose block format is simple enough
to decode here — see _lz4_block. Anything else is declined rather than
guessed at.
"""
import logging
import lzma
import struct

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
    """LZ4 block format — literals and back-references, nothing else.

    Written out here because the format is small and the alternative is a
    dependency: a token byte splits into how many literal bytes follow and
    how many bytes to copy from what has already been produced, with 0xf in
    either half meaning "read more length bytes until one is not 0xff".
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i:i + lit]
            i += lit
        if i >= n:
            break
        if i + 2 > n:
            raise UnityFsError("a back-reference runs past the block")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise UnityFsError("a back-reference points nowhere")
        length = token & 0x0F
        if length == 15:
            while i < n:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4                       # the format's minimum match
        start = len(out) - offset
        if start < 0:
            raise UnityFsError("a back-reference points before the start")
        # Copied one byte at a time on purpose: a match may overlap what it
        # is still producing, which is how the format encodes a run.
        for k in range(length):
            out.append(out[start + k])
        if len(out) > _MAX_UNPACKED:
            raise UnityFsError("the block unpacks to more than we will hold")
    if out_size and len(out) != out_size:
        raise UnityFsError(f"unpacked to {len(out)} bytes, not {out_size}")
    return bytes(out)


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
        raw_info = data[-info_packed:]
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
        elif on_tick is not None and on_tick() is False:
            break
    return bytes(out)
