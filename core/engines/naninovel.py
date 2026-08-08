"""Naninovel ``.nson`` — JSON behind a raw deflate stream.

Naninovel can write saves as plain text or as binary. The binary form is
raw deflate: no zlib header, no gzip header. The window / level / memory
settings below are the ones that reproduce files byte for byte on the
builds checked; a save packed differently still decompresses, but will not
round-trip exactly.
"""
import zlib


class NaninovelError(ValueError):
    pass


# Raw deflate: a negative window size is what says "no header".
_WINDOW = -15
_LEVEL = 6
_MEMORY = 8


def decompress(data: bytes) -> bytes:
    try:
        plain = zlib.decompressobj(_WINDOW).decompress(data)
    except zlib.error as e:
        raise NaninovelError(f"not a deflated Naninovel save: {e}") from e
    if not plain:
        raise NaninovelError("the save unpacks to nothing")
    return plain


def compress(plain: bytes) -> bytes:
    packer = zlib.compressobj(_LEVEL, zlib.DEFLATED, _WINDOW, _MEMORY)
    return packer.compress(plain) + packer.flush()
