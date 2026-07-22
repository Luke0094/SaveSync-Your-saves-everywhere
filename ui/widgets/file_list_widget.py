"""
SaveSync - File List Widget
Collapsible file browser for save path directories.
Shows individual files with checkboxes so users can exclude specific files from backups.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox,
)

from i18n import t
from ui.styles.theme import palette
from core import fmt_size as _fmt_size
from core.backup import _is_skip_file, _BACKUP_SKIP_DIRS

logger = logging.getLogger(__name__)

# Max files to show per directory (avoid UI freeze on huge dirs)
_MAX_DISPLAY_FILES = 200


class FileListWidget(QWidget):
    """Collapsible file list for a save path directory.

    Shows a toggle button that expands to reveal all files in the path
    with individual checkboxes. Files matched by _is_skip_file() (skip
    extension or skip filename stem, e.g. "log"/"logs") are hidden by
    default (they won't be backed up anyway).

    Emits ``selection_changed`` whenever the user checks/unchecks a file.
    """

    selection_changed = Signal()  # emitted when any file checkbox changes

    def __init__(self, path_str: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = path_str
        self._expanded = False
        self._file_checkboxes: list[tuple[QCheckBox, str]] = []  # (checkbox, rel_path)
        self._built = False  # lazy build on first expand
        self._excluded_files: set[str] = set()  # relative paths excluded by user
        self._cancel_event = threading.Event()  # thread-safe cancellation flag
        self._bg_thread: Optional[threading.Thread] = None  # track background count thread

        self._build_toggle()

    def cleanup(self):
        """Cancel any running background thread before destruction."""
        self._cancel_event.set()
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=5.0)
            self._bg_thread = None

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_toggle(self):
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(20, 0, 0, 0)
        self._root_layout.setSpacing(2)

        # Toggle button row
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(6)

        self._arrow = QLabel("▶")
        self._arrow.setStyleSheet(f"color:{palette('text_muted')}; font-size:10px;")
        self._arrow.setFixedWidth(12)

        self._toggle_btn = QPushButton(t("file_list.show_files"))
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setStyleSheet(
            f"QPushButton {{ color:{palette('text_muted')}; font-size:10px; text-align:left; padding:0; border:none; }}"
            f"QPushButton:hover {{ color:{palette('text_secondary')}; }}"
        )
        self._toggle_btn.clicked.connect(self._toggle)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color:{palette('text_hint')}; font-size:10px;")

        toggle_row.addWidget(self._arrow)
        toggle_row.addWidget(self._toggle_btn)
        toggle_row.addWidget(self._count_lbl)
        toggle_row.addStretch()

        self._root_layout.addLayout(toggle_row)

        # Container for file list (hidden initially)
        self._file_container = QWidget()
        self._file_container.setVisible(False)
        self._file_layout = QVBoxLayout(self._file_container)
        self._file_layout.setContentsMargins(0, 4, 0, 4)
        self._file_layout.setSpacing(1)
        self._root_layout.addWidget(self._file_container)

        # Defer count update to avoid blocking the constructor
        QTimer.singleShot(0, self._update_count_label)

    def _update_count_label(self):
        """Count files asynchronously to avoid blocking the GUI."""

        # Reset cancellation for new count
        self._cancel_event.clear()

        # Virtual registry entries: show the value count instead of a
        # misleading "path not found" — there is no per-value exclusion,
        # so the expandable file list stays hidden.
        from core.registry_saves import is_registry_path, registry_value_count
        if is_registry_path(self._path):
            n = registry_value_count(self._path)
            self._count_lbl.setText(f"({n} {t('file_list.registry_values')})")
            self._toggle_btn.setVisible(False)
            self._arrow.setVisible(False)
            return

        p = Path(self._path)
        if p.is_file():
            self._count_lbl.setText(f"(1 {t('file_list.file')})")
            self._toggle_btn.setVisible(False)
            self._arrow.setVisible(False)
            return
        if not p.is_dir():
            self._count_lbl.setText(f"({t('file_list.not_found')})")
            self._toggle_btn.setVisible(False)
            self._arrow.setVisible(False)
            return

        path = self._path
        count_lbl = self._count_lbl
        # Local reference to event — safe even after widget destruction
        cancel_event = self._cancel_event

        def _count():
            count = 0
            total_size = 0
            _FILE_COUNT_LIMIT = 10000
            _TIME_LIMIT = 2.0
            import time
            _start = time.monotonic()
            _timed_out = False
            try:
                for f in Path(path).rglob("*"):
                    if cancel_event.is_set():
                        return  # Widget destroyed — stop early
                    if f.is_file() and not _is_skip_file(f):
                        count += 1
                        try:
                            total_size += f.stat().st_size
                        except OSError:
                            pass
                        if count >= _FILE_COUNT_LIMIT:
                            break
                    if count % 200 == 0 and time.monotonic() - _start > _TIME_LIMIT:
                        _timed_out = True
                        break
            except (PermissionError, OSError):
                pass
            if cancel_event.is_set():
                return  # Widget destroyed — don't update UI

            suffix = "+" if count >= _FILE_COUNT_LIMIT or _timed_out else ""
            text = f"({count}{suffix} {t('file_list.files')}, {_fmt_size(total_size)})"

            # Double-check cancellation right before touching the Qt object
            # to minimise the race window between the event check above and
            # the invokeMethod call (which can crash if the C++ object was
            # deleted in between).
            if cancel_event.is_set():
                return
            from PySide6.QtCore import QMetaObject, Qt as QtConst, Q_ARG
            try:
                import shiboken6
                if not shiboken6.isValid(count_lbl):
                    return
                QMetaObject.invokeMethod(
                    count_lbl, "setText",
                    QtConst.ConnectionType.QueuedConnection,
                    Q_ARG(str, text),
                )
            except (RuntimeError, OSError):
                pass  # Widget was destroyed before thread completed

        # Cancel previous background thread if still running
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._cancel_event.set()
            self._bg_thread.join(timeout=5.0)
            self._cancel_event.clear()

        import threading as _threading
        bg = _threading.Thread(target=_count, daemon=True)
        self._bg_thread = bg
        bg.start()

    def _toggle(self):
        self._expanded = not self._expanded
        if self._expanded and not self._built:
            self._build_file_list()
            self._built = True
        self._file_container.setVisible(self._expanded)
        self._arrow.setText("▼" if self._expanded else "▶")
        self._toggle_btn.setText(
            t("file_list.hide_files") if self._expanded else t("file_list.show_files")
        )

    def _build_file_list(self):
        """Populate the file list from the directory."""
        p = Path(self._path)
        if not p.exists():
            lbl = QLabel(t("file_list.path_not_found"))
            lbl.setStyleSheet(f"color:{palette('text_muted')}; font-size:10px; font-style:italic;")
            self._file_layout.addWidget(lbl)
            return

        if p.is_file():
            # Single file — just show it
            cb = QCheckBox(p.name)
            cb.setChecked(True)
            cb.setStyleSheet(f"QCheckBox {{ color:{palette('text_secondary')}; font-size:10px; }}")
            cb.toggled.connect(lambda: self.selection_changed.emit())
            self._file_checkboxes.append((cb, p.name))
            self._file_layout.addWidget(cb)
            return

        # Directory — list files with filtering
        files_shown = 0
        try:
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                # Skip non-save extensions/filenames (same filter as backup)
                if _is_skip_file(f):
                    continue
                # Skip asset subdirectories
                try:
                    rel_parts = f.relative_to(p).parts
                    if any(part.lower() in _BACKUP_SKIP_DIRS for part in rel_parts[:-1]):
                        continue
                    rel_path = str(f.relative_to(p))
                except ValueError:
                    continue

                if files_shown >= _MAX_DISPLAY_FILES:
                    more_lbl = QLabel(f"  ... {t('file_list.and_more')}")
                    more_lbl.setStyleSheet(f"color:{palette('text_muted')}; font-size:10px; font-style:italic;")
                    self._file_layout.addWidget(more_lbl)
                    break

                # File size
                try:
                    size = _fmt_size(f.stat().st_size)
                except OSError:
                    size = "?"

                cb = QCheckBox(f"{rel_path}  ({size})")
                cb.setChecked(rel_path not in self._excluded_files)
                cb.setStyleSheet(f"QCheckBox {{ color:{palette('text_secondary')}; font-size:10px; }}")
                cb.setToolTip(str(f))
                cb.toggled.connect(lambda checked, rp=rel_path: self._on_file_toggled(rp, checked))
                self._file_checkboxes.append((cb, rel_path))
                self._file_layout.addWidget(cb)
                files_shown += 1

        except (PermissionError, OSError) as e:
            err_lbl = QLabel(f"{t('file_list.scan_error')}: {e}")
            err_lbl.setStyleSheet(f"color:{palette('error')}; font-size:10px;")
            self._file_layout.addWidget(err_lbl)

        if files_shown == 0:
            empty_lbl = QLabel(t("file_list.no_save_files"))
            empty_lbl.setStyleSheet(f"color:{palette('text_muted')}; font-size:10px; font-style:italic;")
            self._file_layout.addWidget(empty_lbl)

    def _on_file_toggled(self, rel_path: str, checked: bool):
        if checked:
            self._excluded_files.discard(rel_path)
        else:
            self._excluded_files.add(rel_path)
        self.selection_changed.emit()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_excluded_files(self) -> set[str]:
        """Return set of relative paths the user has excluded."""
        return set(self._excluded_files)

    def set_excluded_files(self, excluded: set[str]):
        """Set excluded files (e.g., from saved preferences)."""
        self._excluded_files = set(excluded)
        # Update checkboxes if already built
        for cb, rel_path in self._file_checkboxes:
            cb.setChecked(rel_path not in self._excluded_files)

    def get_path(self) -> str:
        return self._path
