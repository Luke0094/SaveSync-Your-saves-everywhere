"""
Etichette di provenienza backup (locale / cloud) condivise tra UI dialog, tab Backup e overlay.
"""
from typing import Any

from i18n import t


class _OriginLabels(dict):
    """Origin id -> badge text. The cloud providers are brand names and stay as
    they are; "Local" and "Local Folder" are words, so they are said in the
    language in use at the moment they are asked for (every reader goes
    through ``.get``)."""

    _WORDS = {
        "local": ("💻 ", "backups.origin_local"),
        "local_provider": ("📁 ", "backups.origin_local_folder"),
    }

    def get(self, key, default=None):
        word = self._WORDS.get(key)
        if word is not None:
            return word[0] + t(word[1])
        return super().get(key, default)


ORIGIN_LABELS = _OriginLabels({
    "p2p": "📥 P2P",
    "onedrive": "☁ OneDrive",  # i18n-ignore (brand)
    "google_drive": "☁ Google Drive",  # i18n-ignore (brand)
    "dropbox": "☁ Dropbox",  # i18n-ignore (brand)
    "webdav": "☁ WebDAV",  # i18n-ignore (brand)
    "rclone": "☁ rclone",  # i18n-ignore (brand)
})

# Origins whose zip is a file on THIS machine: its own backups, and what a P2P
# transfer put there. Everything else is a cloud provider's id.
LOCAL_ORIGINS = ("local", "p2p")


def origin_badge(entry: Any) -> str:
    """Testo badge: copia locale + provider cloud quando synced_to è valorizzato."""
    origin = getattr(entry, "origin", "local")
    synced_to = []
    try:
        synced_to = entry.cloud_metadata.get("synced_to", [])
    except AttributeError:
        pass

    if not synced_to:
        return ORIGIN_LABELS.get(origin, f"☁ {origin}")

    if origin == "local":
        parts = [ORIGIN_LABELS.get("local")]
        seen = set(parts)
        for p in synced_to:
            pl = ORIGIN_LABELS.get(p, f"☁ {p}")
            if pl not in seen:
                parts.append(pl)
                seen.add(pl)
        return " · ".join(parts)

    parts = []
    seen = set()
    first = ORIGIN_LABELS.get(origin, f"☁ {origin}")
    parts.append(first)
    seen.add(first)
    for p in synced_to:
        pl = ORIGIN_LABELS.get(p, f"☁ {p}")
        if pl not in seen:
            parts.append(pl)
            seen.add(pl)
    return " · ".join(parts)
