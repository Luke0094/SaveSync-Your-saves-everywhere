"""Save-file editor: open, edit and write game saves at rest.

Public API stays importable as ``from core.save_editor import …``.

Beside the editor itself this package holds tools used *only* for editing
— decryptors (Unreal, Easy Save 3), remembered keys, UnityFS unpack for
key search, and Wolf value extraction. Engine recognition, format readers
and Wolf's obfuscation layer stay in ``core.engines``.
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
