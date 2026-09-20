"""Generic reader for a save a game locks with a key derived from the
player's own SteamID64 — no password, no key file, nothing the game ships
that names it, because the game does not need to: it already knows whose
save it is. Borderlands 4 is the one documented recipe today (see
steamid_aes_recipes.RECIPES), reverse-engineered by the community rather
than looked up anywhere official — Gearbox documents none of this — but the
shape (a constant baked into the game, XORed against the player's SteamID64
to make a key unique per player) is a known, recurring pattern, not
something this one game happens to do. Add the next documented recipe to
that table, not a new module: nothing in HERE is specific to any one game.

Two ways to learn the SteamID64 a save needs, tried in order:

1. Read straight out of the save's own path, when the game puts its saves
   in a per-player folder named after it (Borderlands 4's own
   ``Saved/SaveGames/<SteamID64>/`` is exactly this) — exact, no guessing.
2. Otherwise, every SteamID64 that has ever used Steam on THIS machine
   (read from Steam's own ``userdata/<AccountID>/`` folders, Windows and
   Unix both — see local_steam_ids). Usually just one account, sometimes a
   handful; each is a real candidate, never a search of the address space,
   and — same proof-not-guess rule crypt/unreal_crypt uses for its own key
   hunt — a candidate is accepted only once it actually decrypts the save
   to valid compressed data. A wrong SteamID produces noise, and noise does
   not zlib-decompress.

Found once, the answer is remembered per save FOLDER (crypt/game_keys),
so this only ever runs the multi-candidate search the first time a given
save is opened.
"""
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

# SteamID64 = AccountID (Steam's own per-account 32-bit number, what
# userdata/<N>/ is named after) + this fixed offset — the individual-account
# universe/type/instance bits are the same for every regular Steam account,
# so the only part that varies is the account id itself.
_STEAMID64_INDIVIDUAL_OFFSET = 76561197960265728

_KEY_KIND = "steamid_aes"


class SteamIdAesError(ValueError):
    pass


@dataclass(frozen=True)
class SteamIdRecipe:
    """One game's "base key XORed against the player's SteamID64" recipe.

    *folder_marker* is the game's own folder name under wherever it saves
    (Documents/My Games, AppData, ...) — the cheap pre-filter before ever
    attempting a decrypt; *base_key* is that game's own 32-byte constant.
    """
    name: str
    folder_marker: str
    base_key: bytes
    source: str = ""


# ── SteamID discovery ────────────────────────────────────────────────────

def steamid64_in_path(path) -> str:
    """The SteamID64-shaped folder segment of *path*, or "".

    Every real SteamID64 starts with 7656119 (Valve's fixed
    universe/type/instance prefix for an individual account) and is 17
    digits long — distinctive enough that matching it against each path
    segment does not need to know anything about a particular game's
    folder layout.
    """
    for part in Path(path).parts:
        if len(part) == 17 and part.isdigit() and part.startswith("7656119"):
            return part
    return ""


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _steam_userdata_dirs() -> list:
    """Steam's own ``userdata`` folder(s) on this machine, Windows and
    Unix. More than one can legitimately exist on Linux (a native install
    and a Flatpak one each keep their own), so every plausible location is
    checked rather than just the first that exists."""
    import platform
    bases = []
    if platform.system() == "Windows":
        try:
            import winreg
            for hive, key_path, value in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key_path) as k:
                        raw, _ = winreg.QueryValueEx(k, value)
                        if raw:
                            bases.append(Path(raw))
                except OSError:
                    continue
        except ImportError:
            pass
        bases.append(Path(r"C:\Program Files (x86)\Steam"))
    else:
        home = Path.home()
        bases.extend([
            home / ".steam" / "steam",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ])
    out, seen = [], set()
    for base in bases:
        userdata = base / "userdata"
        if not userdata.is_dir():
            continue
        key = str(userdata).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(userdata)
    return out


def local_steam_ids() -> list:
    """Every SteamID64 that has used Steam on this machine, most recently
    active first — candidate profile ids for a save whose own path does not
    carry it directly (unlike the games steamid64_in_path handles)."""
    out, seen = [], set()
    for userdata in _steam_userdata_dirs():
        try:
            entries = [e for e in userdata.iterdir() if e.is_dir() and e.name.isdigit()]
        except OSError:
            continue
        entries.sort(key=_safe_mtime, reverse=True)
        for entry in entries:
            accountid = int(entry.name)
            if accountid <= 0:
                continue
            steamid64 = str(accountid + _STEAMID64_INDIVIDUAL_OFFSET)
            if steamid64 in seen:
                continue
            seen.add(steamid64)
            out.append(steamid64)
    return out


def find_save_plaintext(data: bytes, recipe: SteamIdRecipe, path=None) -> tuple:
    """(steamid, decrypted bytes) using whichever candidate SteamID64
    actually opens this save, or ("", b"") when none does.

    Tried in order: the id embedded in *path* itself (exact, when the game
    puts it there), the one remembered from the last time THIS save folder
    was opened, then every Steam account known to have used this machine.
    Nothing is guessed at — a candidate is accepted only once decrypt()
    proves it right by actually producing valid compressed data.
    """
    from core.save_editor.crypt.game_keys import stored_key, store_key
    place = Path(path).parent if path else None
    candidates, seen = [], set()

    def _offer(steamid):
        if steamid and steamid not in seen:
            seen.add(steamid)
            candidates.append(steamid)

    _offer(steamid64_in_path(path) if path else "")
    if place is not None:
        _offer(stored_key(_KEY_KIND, place))
    for steamid in local_steam_ids():
        _offer(steamid)

    for steamid in candidates:
        try:
            plain = decrypt(data, steamid, recipe.base_key)
        except SteamIdAesError:
            continue
        if place is not None:
            store_key(_KEY_KIND, place, steamid)
        return steamid, plain
    return "", b""


# ── The recipe's own crypto/container shape ─────────────────────────────
# (Verified against Borderlands 4's own recipe; a future recipe whose
# container differs — a different compression, a different trailer — would
# need this section to grow a per-recipe hook rather than assuming every
# recipe looks exactly like this one, but there is only the one documented
# recipe to go on today — see steamid_aes_recipes.)

def steamid_xor_key(base_key: bytes, steamid: str) -> bytes:
    """*base_key*, its leading 8 bytes XORed against *steamid* packed as an
    8-byte little-endian integer — the recipe every game in RECIPES shares.
    """
    digits = "".join(ch for ch in steamid if ch.isdigit())
    if not digits:
        raise SteamIdAesError("no SteamID to derive this save's key from")
    sid_le = int(digits).to_bytes(8, "little", signed=False)
    key = bytearray(base_key)
    for i in range(min(8, len(key))):
        key[i] ^= sid_le[i]
    return bytes(key)


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(data) + dec.finalize()


def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def _unpad_pkcs7(data: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(data) + unpadder.finalize()
    except ValueError:
        # Not validly padded — handed back as-is; zlib.decompress below is
        # the real judge of whether the key was actually right.
        return data


def _pad_pkcs7(data: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()


def looks_like_save(data: bytes) -> bool:
    """Weak, deliberately: a whole number of AES blocks and nothing more —
    the file is fully encrypted from its first byte, so nothing about its
    CONTENT can confirm it. Only actually decrypting it (with the right
    SteamID) proves anything; see decrypt.
    """
    return len(data) >= 16 and len(data) % 16 == 0


def decrypt(data: bytes, steamid: str, base_key: bytes) -> bytes:
    """The save's own (compressed-container) plaintext, decrypted and
    decompressed.

    Raises SteamIdAesError when *steamid* is not the one this save was
    encrypted for (or *data* is not this shape at all) — AES-ECB with the
    wrong key produces noise, and noise does not zlib-decompress.
    """
    if not looks_like_save(data):
        raise SteamIdAesError(
            f"not a SteamID-keyed save (size {len(data)} is not a whole "
            f"number of AES blocks)")
    key = steamid_xor_key(base_key, steamid)
    padded = _aes_ecb_decrypt(data, key)
    body = _unpad_pkcs7(padded)
    try:
        return zlib.decompress(body)
    except zlib.error as e:
        raise SteamIdAesError(
            "decrypted, but the result is not compressed data — wrong "
            "SteamID for this save?") from e


def encrypt(plain: bytes, steamid: str, base_key: bytes) -> bytes:
    """The reverse of decrypt: compress, trail an Adler-32 + original
    length (both little-endian) the way the game itself writes them, pad,
    encrypt."""
    comp = zlib.compress(plain, 9)
    adler32 = zlib.adler32(plain) & 0xFFFFFFFF
    packed = comp + struct.pack("<I", adler32) + struct.pack("<I", len(plain))
    padded = _pad_pkcs7(packed)
    key = steamid_xor_key(base_key, steamid)
    return _aes_ecb_encrypt(padded, key)
