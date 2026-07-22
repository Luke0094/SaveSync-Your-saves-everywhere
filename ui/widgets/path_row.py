"""
SaveSync - Save-path row widget for the Add/Edit Game dialog.

Extracted verbatim from ui/dialogs/add_game_dialog.py: the per-path row
(checkbox, size label, open/remove buttons, collapsible file browser) plus
its size-computation helpers. Registry-aware: virtual registry entries show
value counts and open in regedit. Pure move — no behavior change.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QToolButton,
)

from i18n import t
from core import fmt_size as _fmt_size
from ui.helpers import open_in_file_manager
from ui.modal_helpers import information_window_modal
from ui.styles.theme import palette

logger = logging.getLogger(__name__)


def _folder_size(path_str: str, exclude_subdirs: Optional[set] = None) -> tuple[int, int]:
    """Return (total_bytes, file_count) for a directory.

    Files under any path in *exclude_subdirs* are skipped so that when a
    parent path (e.g. ``game/``) and a child path (e.g. ``game/saves/``) are
    both tracked, the parent's size does not double-count the child's files.
    """
    try:
        p = Path(path_str)
        if not p.is_dir():
            return (0, 0)
        total, count = 0, 0
        excl_resolved: list[Path] = []
        if exclude_subdirs:
            for ep in exclude_subdirs:
                try:
                    excl_resolved.append(Path(ep).resolve())
                except Exception:
                    pass
        for f in p.rglob("*"):
            if f.is_file():
                if excl_resolved:
                    skip = False
                    try:
                        f_res = f.resolve()
                        for excl in excl_resolved:
                            try:
                                f_res.relative_to(excl)
                                skip = True
                                break
                            except ValueError:
                                pass
                    except Exception:
                        pass
                    if skip:
                        continue
                try:
                    total += f.stat().st_size
                    count += 1
                except OSError:
                    pass
        return (total, count)
    except Exception:
        return (0, 0)


def _fmt_size_and_count(path_str: str, exclude_subdirs: Optional[set] = None) -> str:
    """Format size and count, optionally excluding sub-paths already tracked.

    Callers should run this in a background thread for large directories.
    """
    from core.registry_saves import is_registry_path, registry_value_count
    if is_registry_path(path_str):
        return f"({registry_value_count(path_str)} {t('file_list.registry_values')})"
    s, c = _folder_size(path_str, exclude_subdirs)
    if c == 0:
        return f"({t('common.empty')})"
    file_word = t('file_list.files') if c != 1 else t('file_list.file')
    return f"{_fmt_size(s)}  •  {c} {file_word}"


class PathRow(QFrame):
    """One save-path row with size, open, preview, remove, checkbox, and file browser."""
    remove_requested = Signal(str)

    def __init__(self, path_str: str, game_id: str = "", parent=None):
        super().__init__(parent)
        self._path = path_str
        self._game_id = game_id
        self._excluded_subdirs: set = set()  # child paths to exclude from size
        self.file_list = None  # FileListWidget (lazy)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            QFrame {{ background:{palette('bg_card')}; border:1px solid {palette('border')}; border-radius:6px; }}
            QFrame:hover {{ border-color:{palette('border_hover')}; }}
        """)
        self._build()

    def set_excluded_subdirs(self, excluded: set):
        """Set child paths whose files should be excluded from size calculation."""
        self._excluded_subdirs = excluded
        self._refresh_size()

    def _refresh_size(self):
        """Recompute size label in background thread."""
        _path = self._path
        _excl = set(self._excluded_subdirs)
        info_lbl = self._info_lbl

        def _compute():
            text = _fmt_size_and_count(_path, _excl if _excl else None)
            try:
                from PySide6.QtCore import QMetaObject, Qt as QtConst, Q_ARG
                QMetaObject.invokeMethod(
                    info_lbl, "setText",
                    QtConst.ConnectionType.QueuedConnection,
                    Q_ARG(str, text),
                )
            except (RuntimeError, OSError):
                pass
        threading.Thread(target=_compute, daemon=True).start()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)

        # Checkbox for path selection
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)  # Default to checked
        self.checkbox.setToolTip(t('add_game.include_path'))
        self.checkbox.setStyleSheet("QCheckBox { spacing: 4px; }")

        # Truncated path label (registry entries show 🗝 + key, no scheme)
        from core.registry_saves import is_registry_path as _is_reg
        from core.registry_saves import registry_display as _reg_disp
        _disp = (f"🗝 {_reg_disp(self._path)}" if _is_reg(self._path)
                 else self._path)
        path_lbl = QLabel(_disp)
        path_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:11px;")
        path_lbl.setToolTip(_disp if not _is_reg(self._path)
                            else t('auto_scan.registry_key_tooltip'))
        path_lbl.setMinimumWidth(60)
        path_lbl.setWordWrap(False)

        # Size info — computed asynchronously to avoid blocking the GUI
        # thread with rglob on large save directories.
        info_lbl = QLabel("...")
        info_lbl.setStyleSheet(f"color:{palette('text_faint')};font-size:10px;min-width:110px;")
        self._info_lbl = info_lbl
        # Trigger initial size computation (no exclusions yet; updated by set_excluded_subdirs)
        self._refresh_size()

        # Buttons
        open_btn = QToolButton()
        open_btn.setText("\U0001f4c2")
        open_btn.setToolTip(t('add_game.open_folder'))
        open_btn.setFixedSize(24, 24)
        open_btn.clicked.connect(self._open_folder)

        rm_btn = QToolButton()
        rm_btn.setText("\u2715")
        rm_btn.setToolTip(t('add_game.remove_path'))
        rm_btn.setFixedSize(24, 24)
        rm_btn.clicked.connect(lambda: self.remove_requested.emit(self._path))

        row.addWidget(self.checkbox)
        row.addWidget(path_lbl, 1)
        row.addWidget(info_lbl)
        row.addWidget(open_btn)
        row.addWidget(rm_btn)
        outer.addLayout(row)

        # File browser (collapsible) — shows individual files for exclusion
        from ui.widgets.file_list_widget import FileListWidget
        self.file_list = FileListWidget(self._path)
        # Restore any previously saved file exclusions for this path
        if self._game_id:
            try:
                from core.config_manager import get_config as _gc
                saved = _gc().get("auto_scan_excluded_files", {}).get(self._game_id, {})
                if self._path in saved:
                    self.file_list.set_excluded_files(set(saved[self._path]))
            except Exception:
                pass
        outer.addWidget(self.file_list)

    def is_checked(self) -> bool:
        """Return whether this path is checked."""
        return self.checkbox.isChecked()

    def set_checked(self, checked: bool):
        """Set the checkbox state."""
        self.checkbox.setChecked(checked)

    def get_path(self) -> str:
        """Get the path string."""
        return self._path

    def _open_folder(self):
        from core.registry_saves import is_registry_path, open_in_regedit
        if is_registry_path(self._path):
            open_in_regedit(self._path)
            return
        target = Path(self._path)
        if not target.exists():
            information_window_modal(self, t('add_game.folder_not_found'),
                t('add_game.folder_not_exist', path=self._path))
            return
        open_in_file_manager(target)


