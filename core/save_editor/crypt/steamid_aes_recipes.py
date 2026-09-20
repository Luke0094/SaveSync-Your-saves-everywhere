"""Known SteamID-keyed save recipes, one entry per game — the data half of
``core.save_editor.crypt.steamid_aes``'s generic reader. Each recipe below
is taken from a real, working community decryptor for that ONE game, never
inferred from staring at bytes; where it came from is in the recipe's own
comment. Add the next documented recipe here, not as a new module.
"""
from .steamid_aes import SteamIdRecipe

# Borderlands 4's own base key and key-derivation steps, read verbatim from
# glacierpiece/borderlands-4-save-utility's blcrypt.py (derive_key/BASE_KEY),
# and cross-checked against a second, independent implementation —
# monokrome/bl4 (Rust) — whose README describes the identical algorithm
# ("Steam ID XOR'd with hardcoded base key") while crediting the same
# original reverse-engineering. Saves live under each player's own
# ``Saved/SaveGames/<SteamID64>/`` folder, which is also where
# steamid_aes.steamid64_in_path reads the id straight off the path.
BORDERLANDS_4 = SteamIdRecipe(
    name="Borderlands 4",
    folder_marker="Borderlands 4",
    base_key=bytes((
        0x35, 0xEC, 0x33, 0x77, 0xF3, 0x5D, 0xB0, 0xEA,
        0xBE, 0x6B, 0x83, 0x11, 0x54, 0x03, 0xEB, 0xFB,
        0x27, 0x25, 0x64, 0x2E, 0xD5, 0x49, 0x06, 0x29,
        0x05, 0x78, 0xBD, 0x60, 0xBA, 0x4A, 0xA7, 0x87,
    )),
    source="glacierpiece/borderlands-4-save-utility (blcrypt.py), cross-checked against monokrome/bl4",
)

# Tried in this order — see steamid_aes_format.SteamIdAesFormat.load().
RECIPES = (BORDERLANDS_4,)
