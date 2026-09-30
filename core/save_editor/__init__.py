"""Save-file editor: open, edit and write game saves at rest.

Public API stays importable as ``from core.save_editor import …``.

Beside the editor itself this package holds per-format adapters
(``*_format``) and — under ``crypt/`` — the decryptors (Unreal, Easy Save 3,
Wolf unlock, UnityFS, remembered keys). Engine recognition and binary/format
readers stay in ``core.engines``.
"""
from .save_editor import (  # noqa: F401
    KIND_EDIT,
    KIND_PRE_RESTORE,
    SaveEditorError,
    backup_kind,
    backup_original,
    backup_taken_at,
    delete_backup,
    describe,
    explain,
    list_backups,
    open_save,
    prune_all,
    prune_backups,
    read_source,
    restore_backup,
)
from .save_hold import SaveHold  # noqa: F401

__all__ = [
    "KIND_EDIT",
    "KIND_PRE_RESTORE",
    "SaveEditorError",
    "SaveHold",
    "backup_kind",
    "backup_original",
    "backup_taken_at",
    "delete_backup",
    "describe",
    "explain",
    "list_backups",
    "open_save",
    "prune_all",
    "prune_backups",
    "read_source",
    "restore_backup",
]
