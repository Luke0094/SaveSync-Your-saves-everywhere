"""
Etichette di provenienza backup (locale / cloud) condivise tra UI dialog, tab Backup e overlay.
"""
from typing import Any

ORIGIN_LABELS = {
    "local": "💻 Local",
    "onedrive": "☁ OneDrive",
    "google_drive": "☁ Google Drive",
    "dropbox": "☁ Dropbox",
    "webdav": "☁ WebDAV",
    "rclone": "☁ rclone",
    "local_provider": "📁 Local Folder",
}


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
        parts = [ORIGIN_LABELS.get("local", "💻 Local")]
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
