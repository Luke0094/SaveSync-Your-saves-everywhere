"""Engine recognition and per-engine save *formats*.

Which engine built a game, and how that engine writes its saves (GVAS,
Marshal, LCF, Ren'Py, RPG Maker MV/MZ, SQLite, PlayerPrefs, key/value, XML,
…). Cryptography used only by the save editor — decryptors, key stores,
UnityFS key search — lives under ``core.save_editor.crypt``. Format adapters
in ``core.save_editor`` stay thin.
"""
