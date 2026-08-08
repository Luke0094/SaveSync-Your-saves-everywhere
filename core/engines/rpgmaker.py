"""RPG Maker MV / MZ save packing.

Detection of an RPG Maker install lives in ``game_engine``. This module is
the on-disk save wrappers:

- **MV** (``.rpgsave``): JSON text squeezed with LZString and written as
  base64 — see ``lzstring``.
- **MZ** (``.rmmzsave``): JSON deflated with zlib, then often handed to the
  file writer as a *string*, so every byte above 0x7f lands on disk as the
  two bytes UTF-8 spells it with.

Older RPG Maker generations have their own readers: ``lcf`` (2000/2003) and
``rubymarshal`` (XP / VX / VX Ace).
"""
import zlib

from .lzstring import compress_to_base64, decompress_from_base64


class RpgMakerError(ValueError):
    pass


# Deflate level MZ itself uses; matching it keeps an untouched save identical.
_MZ_LEVEL = 1


def mv_decompress(data: bytes) -> str:
    """JSON text from an MV ``.rpgsave`` (or any LZString-base64 MV payload)."""
    try:
        text = decompress_from_base64(
            data.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as e:
        raise RpgMakerError("not an LZString payload") from e
    if not text:
        raise RpgMakerError("not an LZString payload")
    return text


def mv_compress(text: str) -> bytes:
    return compress_to_base64(text).encode("ascii")


def mz_decompress(data: bytes) -> tuple[bytes, bool]:
    """Plain JSON bytes and whether the file used the UTF-8 string wrap.

    The wrap flag must be remembered for writing back: some builds skip it
    and store the zlib bytes as-is.
    """
    wrapped = True
    try:
        binary = data.decode("utf-8").encode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        binary, wrapped = data, False
    try:
        plain = zlib.decompress(binary)
    except zlib.error as e:
        raise RpgMakerError(f"not a deflated RPG Maker MZ save: {e}") from e
    return plain, wrapped


def mz_compress(plain: bytes, wrapped: bool = True) -> bytes:
    packed = zlib.compress(plain, _MZ_LEVEL)
    if not wrapped:
        return packed
    return packed.decode("latin-1").encode("utf-8")
