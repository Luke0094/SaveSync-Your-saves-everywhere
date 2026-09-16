"""JIT-compiled AES-ECB/CBC decrypt, for scanning a game binary for an
Unreal save's key (see ``unreal_crypt.find_key``).

That scan tries every offset in a binary — up to tens of millions of them,
at strides down to 4 bytes, for three key sizes — as a candidate AES key,
decrypting just enough to check for the ``GVAS`` magic. Each candidate
today goes through ``cryptography``'s ``Cipher`` object: real per-call
Python/OpenSSL-boundary overhead multiplied by millions of candidates,
the same shape of cost wolf_lz4's seed search had before numba (see that
module's own docstring for the measured effect there). AES itself —
table lookups, XORs, a fixed round count — is exactly what a JIT compiles
well, so the same fix applies here.

Not a general-purpose AES implementation: only single/double-block
decryption, only what the scan needs (first block for ECB, first+second
for CBC using the first as IV — matching ``unreal_crypt._oracle``
exactly). Optional the same way numba is everywhere else in this
project — ``_HAVE_NUMBA`` is False and this module is simply not
imported when it is unavailable; ``find_key`` falls back to the
``cryptography``-based path either way.

Correctness matters more than usual here: a wrong decrypt that happens to
produce ``GVAS`` by coincidence would hand back a fake key. Validated in
``tests/`` against ``cryptography``'s own AES-ECB/CBC output across
random keys and plaintexts for all three key sizes, not just against
FIPS-197's published test vectors (also checked) — see
``test_aes_numba.py``.
"""
import numpy as np

try:
    from numba import njit, prange
    _HAVE_NUMBA = True
except ImportError:
    _HAVE_NUMBA = False

# ── FIPS-197 tables ──────────────────────────────────────────────────────────

_SBOX = np.array([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
], dtype=np.uint8)

_INV_SBOX = np.zeros(256, dtype=np.uint8)
for _i in range(256):
    _INV_SBOX[_SBOX[_i]] = _i

_RCON = np.array([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
                   0x6C, 0xD8, 0xAB, 0x4D], dtype=np.uint8)


def _xtime(a: int) -> int:
    a &= 0xFF
    hi = a & 0x80
    a = (a << 1) & 0xFF
    return a ^ 0x1B if hi else a


def _gmul(a: int, b: int) -> int:
    p = 0
    a &= 0xFF
    b &= 0xFF
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p & 0xFF


# Precomputed GF(2^8) multiply-by-constant tables for InvMixColumns.
_MUL9 = np.array([_gmul(x, 0x09) for x in range(256)], dtype=np.uint8)
_MUL11 = np.array([_gmul(x, 0x0B) for x in range(256)], dtype=np.uint8)
_MUL13 = np.array([_gmul(x, 0x0D) for x in range(256)], dtype=np.uint8)
_MUL14 = np.array([_gmul(x, 0x0E) for x in range(256)], dtype=np.uint8)

if _HAVE_NUMBA:
    @njit(cache=True)
    def _key_expansion(key, nk, nr):
        """Round keys as a flat (nr+1)*16 uint8 array - the standard
        FIPS-197 schedule (Nk words of the key, then Nk..4*(Nr+1) more,
        with SubWord+RotWord+Rcon every Nk words, plus an extra SubWord
        when Nk==8 / AES-256)."""
        nw = 4 * (nr + 1)
        w = np.zeros((nw, 4), dtype=np.uint8)
        for i in range(nk):
            w[i, 0] = key[4 * i]
            w[i, 1] = key[4 * i + 1]
            w[i, 2] = key[4 * i + 2]
            w[i, 3] = key[4 * i + 3]
        for i in range(nk, nw):
            temp0, temp1, temp2, temp3 = w[i - 1, 0], w[i - 1, 1], w[i - 1, 2], w[i - 1, 3]
            if i % nk == 0:
                temp0, temp1, temp2, temp3 = temp1, temp2, temp3, temp0  # RotWord
                temp0 = _SBOX[temp0]
                temp1 = _SBOX[temp1]
                temp2 = _SBOX[temp2]
                temp3 = _SBOX[temp3]
                temp0 ^= _RCON[i // nk - 1]
            elif nk > 6 and i % nk == 4:
                temp0 = _SBOX[temp0]
                temp1 = _SBOX[temp1]
                temp2 = _SBOX[temp2]
                temp3 = _SBOX[temp3]
            w[i, 0] = w[i - nk, 0] ^ temp0
            w[i, 1] = w[i - nk, 1] ^ temp1
            w[i, 2] = w[i - nk, 2] ^ temp2
            w[i, 3] = w[i - nk, 3] ^ temp3
        out = np.empty(nw * 4, dtype=np.uint8)
        for i in range(nw):
            out[4 * i:4 * i + 4] = w[i]
        return out

    @njit(cache=True, inline="always")
    def _inv_mix_columns(s):
        for c in range(4):
            a0, a1, a2, a3 = s[c, 0], s[c, 1], s[c, 2], s[c, 3]
            s[c, 0] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
            s[c, 1] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
            s[c, 2] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
            s[c, 3] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]

    @njit(cache=True, inline="always")
    def _decrypt_block(block, round_keys, nr, out):
        # State laid out column-major, matching FIPS-197 (block[4*c+r]).
        s = np.empty((4, 4), dtype=np.uint8)
        for c in range(4):
            for r in range(4):
                s[c, r] = block[4 * c + r] ^ round_keys[nr * 16 + 4 * c + r]

        for rnd in range(nr - 1, 0, -1):
            # InvShiftRows
            t = np.empty((4, 4), dtype=np.uint8)
            for c in range(4):
                for r in range(4):
                    t[c, r] = s[(c - r) % 4, r]
            # InvSubBytes
            for c in range(4):
                for r in range(4):
                    t[c, r] = _INV_SBOX[t[c, r]]
            # AddRoundKey
            for c in range(4):
                for r in range(4):
                    t[c, r] ^= round_keys[rnd * 16 + 4 * c + r]
            _inv_mix_columns(t)
            s = t

        # final round: InvShiftRows, InvSubBytes, AddRoundKey (no InvMixColumns)
        t = np.empty((4, 4), dtype=np.uint8)
        for c in range(4):
            for r in range(4):
                t[c, r] = s[(c - r) % 4, r]
        for c in range(4):
            for r in range(4):
                t[c, r] = _INV_SBOX[t[c, r]]
        for c in range(4):
            for r in range(4):
                out[4 * c + r] = t[c, r] ^ round_keys[4 * c + r]

    @njit(cache=True)
    def decrypt_ecb_ref(data, key, nk, nr):
        """Whole-buffer ECB decrypt (test/reference use — the scan below
        only ever needs one or two blocks, but correctness is validated
        against this against ``cryptography`` a whole ciphertext at a
        time)."""
        round_keys = _key_expansion(key, nk, nr)
        n = data.shape[0]
        out = np.empty(n, dtype=np.uint8)
        block_out = np.empty(16, dtype=np.uint8)
        for off in range(0, n, 16):
            _decrypt_block(data[off:off + 16], round_keys, nr, block_out)
            out[off:off + 16] = block_out
        return out

    @njit(cache=True)
    def _scan_chunk(blob, lo, hi, stride, size, nk, nr, first, second,
                     have_second, magic, out_off, out_mode):
        """One bounded slice of offsets - see find_key_numba. Stops at the
        first hit (mode 1 = ECB, 2 = CBC) or exhausts [lo, hi)."""
        rk = np.empty((nr + 1) * 16, dtype=np.uint8)
        block_out = np.empty(16, dtype=np.uint8)
        n = blob.shape[0]
        off = lo
        while off < hi:
            if off + size > n:
                break
            key = blob[off:off + size]
            rk[:] = _key_expansion(key, nk, nr)
            _decrypt_block(first, rk, nr, block_out)
            match = True
            for i in range(4):
                if block_out[i] != magic[i]:
                    match = False
                    break
            if match:
                out_off[0] = off
                out_mode[0] = 1
                return
            if have_second:
                # CBC: first ciphertext block is the IV, XORed in after
                # decrypting the second block - see unreal_crypt._oracle.
                _decrypt_block(second, rk, nr, block_out)
                match = True
                for i in range(4):
                    if (block_out[i] ^ first[i]) != magic[i]:
                        match = False
                        break
                if match:
                    out_off[0] = off
                    out_mode[0] = 2
                    return
            off += stride
        out_off[0] = -1
        out_mode[0] = 0


def find_key_numba(blob: bytes, size: int, stride: int, first: bytes,
                    second: bytes, magic: bytes, carry_on=None,
                    chunk: int = 1 << 16):
    """(offset, mode) for the first *size*-byte key at a *stride*-aligned
    offset in *blob* that decrypts *first* (ECB) or *first*+*second* (CBC,
    *first* as IV) to something starting with *magic* - or (-1, 0).

    *carry_on*, called between chunks, stops the scan early exactly like
    ``unreal_crypt.find_key``'s own ``on_tick`` - this is the JIT
    equivalent of that same loop, not a different search.
    """
    if not _HAVE_NUMBA:
        raise RuntimeError("numba is not available")
    nk = size // 4
    nr = {4: 10, 6: 12, 8: 14}[nk]
    blob_arr = np.frombuffer(blob, dtype=np.uint8)
    first_arr = np.frombuffer(first, dtype=np.uint8)
    have_second = len(second) >= 16
    second_arr = (np.frombuffer(second[:16], dtype=np.uint8) if have_second
                  else np.zeros(16, dtype=np.uint8))
    magic_arr = np.frombuffer(magic[:4].ljust(4, b"\0"), dtype=np.uint8)
    n = len(blob)
    hi_bound = max(0, n - size + 1)
    out_off = np.zeros(1, dtype=np.int64)
    out_mode = np.zeros(1, dtype=np.int64)
    lo = 0
    while lo < hi_bound:
        if carry_on is not None and not carry_on():
            return -1, 0
        hi = min(lo + chunk, hi_bound)
        _scan_chunk(blob_arr, lo, hi, stride, size, nk, nr, first_arr,
                    second_arr, have_second, magic_arr, out_off, out_mode)
        if out_off[0] >= 0:
            return int(out_off[0]), int(out_mode[0])
        lo = hi
    return -1, 0
