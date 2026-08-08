"""Save-file editor: open, edit and write game saves at rest.

Public API stays importable as ``from core.save_editor import …``.

Beside the editor itself this package holds per-format adapters
(``*_format``) and — under ``crypt/`` — the decryptors (Unreal, Easy Save 3,
Wolf unlock, UnityFS, remembered keys). Engine recognition and binary/format
readers stay in ``core.engines``.
"""
from .save_editor import (  # noqa: F401
    SaveEditorError,
    backup_original,
    describe,
    explain,
    list_backups,
    open_save,
    prune_all,
    prune_backups,
    restore_backup,
)
from .save_hold import SaveHold  # noqa: F401

__all__ = [
    "SaveEditorError",
    "SaveHold",
    "backup_original",
    "describe",
    "explain",
    "list_backups",
    "open_save",
    "prune_all",
    "prune_backups",
    "restore_backup",
]
