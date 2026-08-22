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
    QCheckBox, QToolButton,
)

from i18n import t
from ui.helpers import scaled
from core import fmt_size as _fmt_size
from core.backup import _is_skip_file, _BACKUP_SKIP_DIRS

logger = logging.getLogger(__name__)

# Max files to show per directory (avoid UI freeze on huge dirs)
_MAX_DISPLAY_FILES = 200


class FileListWidget(QWidget):
    """Collapsible file list for a save path directory.

    Shows a toggle button that expands to reveal all files in the path
    with individual checkboxes and delete/restore action buttons so users
    can exclude specific files from backups or restore them.
    """

    selection_changed = Signal()  # emitted when any file checkbox changes

    def __init__(self, path_str: str, game_id: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = path_str
        self._game_id = game_id
        self._expanded = False
        self._file_checkboxes: list[tuple[QCheckBox, str]] = []  # (checkbox, rel_path)
        self._file_rows: dict[str, tuple[QCheckBox, QToolButton]] = {}
        self._built = False  # lazy build on first expand
        self._excluded_files: set[str] = set()  # relative paths excluded by user
        # Files the user BINNED. Deselecting and deleting are different acts
        # and were being recorded as one: unticking a file wrote it to the
        # deleted-paths store as well, so it turned up in "restore deleted"
        # having never been deleted, and the restore list filled with things
        # the user had merely chosen not to back up this time.
        #
        #   unticked -> stays in the list, not backed up, NOT restorable
        #               (there is nothing to restore: it is right there)
        #   binned   -> leaves the list, and IS restorable
        self._deleted_files: set[str] = set()
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
        self._arrow.setObjectName("file_list_meta")
        self._arrow.setFixedWidth(scaled(12, self))

        self._toggle_btn = QPushButton(t("file_list.show_files"))
        self._toggle_btn.setObjectName("file_list_toggle")
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle)

        self._count_lbl = QLabel("")
        self._count_lbl.setObjectName("file_list_hint")

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
        """Populate the file list from the directory.

        Idempotent. It used to be called exactly once — lazily, the first
        time the list was expanded — so it appended to an empty layout and
        the question never came up. Calling it a second time (to show a
        restored file back in its place) appended a whole second copy of
        every row beside the first, and a third call a third copy. A build
        that cannot be repeated is a trap for the next caller, so it clears
        what it is about to rebuild.
        """
        self._files_built = True
        self._file_checkboxes = []
        self._file_rows = {}
        while self._file_layout.count():
            item = self._file_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        p = Path(self._path)
        if not p.exists():
            lbl = QLabel(t("file_list.path_not_found"))
            lbl.setObjectName("file_list_meta_italic")
            self._file_layout.addWidget(lbl)
            return

        if p.is_file():
            # Single file — just show it
            cb = QCheckBox(p.name)
            cb.setObjectName("file_list_cb")
            cb.setChecked(True)
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
                    more_lbl.setObjectName("file_list_meta_italic")
                    self._file_layout.addWidget(more_lbl)
                    break

                # File size
                try:
                    size = _fmt_size(f.stat().st_size)
                except OSError:
                    size = "?"

                row_w = QWidget()
                row_lay = QHBoxLayout(row_w)
                row_lay.setContentsMargins(0, 1, 0, 1)
                row_lay.setSpacing(6)

                if rel_path in self._deleted_files:
                    continue          # binned: out of the list, not greyed out
                is_excluded = rel_path in self._excluded_files

                cb = QCheckBox(rel_path)
                cb.setObjectName("file_list_cb")
                cb.setChecked(not is_excluded)
                cb.setToolTip(str(f))

                size_lbl = QLabel(f"({size})")
                size_lbl.setObjectName("file_list_meta")

                act_btn = QToolButton()
                act_btn.setObjectName("file_list_action")
                _btn_sz = scaled(18, self, min_px=16)
                act_btn.setFixedSize(_btn_sz, _btn_sz)
                act_btn.setCursor(Qt.CursorShape.PointingHandCursor)

                # The bin DELETES. The tick-box beside it is what says "not
                # this time" — it used to do the same thing as this button,
                # which left no way to actually take a file out of the list.
                act_btn.setText("🗑")
                act_btn.setToolTip(t("file_list.delete_file"))
                _live = getattr(self, "_selectable", True)
                cb.setEnabled(_live)
                act_btn.setEnabled(_live)
                cb.setStyleSheet(
                    "" if not is_excluded
                    else "color: #888888; text-decoration: line-through;")

                act_btn.clicked.connect(lambda _=False, rp=rel_path, w=row_w: (
                    self._on_delete_clicked(rp, w)
                ))
                cb.toggled.connect(lambda checked, rp=rel_path, c=cb: (
                    self._on_cb_toggled(rp, checked, c)
                ))

                row_lay.addWidget(cb, 1)
                row_lay.addWidget(size_lbl)
                row_lay.addWidget(act_btn)

                self._file_checkboxes.append((cb, rel_path))
                self._file_rows[rel_path] = (cb, act_btn)
                self._file_layout.addWidget(row_w)
                files_shown += 1

        except (PermissionError, OSError) as e:
            err_lbl = QLabel(f"{t('file_list.scan_error')}: {e}")
            err_lbl.setObjectName("file_list_error")
            self._file_layout.addWidget(err_lbl)

        if files_shown == 0:
            empty_lbl = QLabel(t("file_list.no_save_files"))
            empty_lbl.setObjectName("file_list_meta_italic")
            self._file_layout.addWidget(empty_lbl)

    def _on_delete_clicked(self, rel_path: str, row_w):
        """Bin a file: out of the list, and restorable from Ignored Paths."""
        if not getattr(self, "_selectable", True):
            return
        self._deleted_files.add(rel_path)
        self._set_file_excluded_state(rel_path, True, deleted=True)
        self._file_rows.pop(rel_path, None)
        self._file_checkboxes = [(c, r) for c, r in self._file_checkboxes
                                 if r != rel_path]
        try:
            row_w.setParent(None)
            row_w.deleteLater()
        except RuntimeError:
            pass

    def _on_cb_toggled(self, rel_path: str, checked: bool, cb: QCheckBox):
        """Tick / untick: whether this file goes into the backup. Nothing
        leaves the list and nothing becomes restorable — it is still here."""
        if not getattr(self, "_selectable", True):
            return
        self._set_file_excluded_state(rel_path, not checked)
        cb.setStyleSheet("" if checked
                         else "color: #888888; text-decoration: line-through;")

    def _set_file_excluded_state(self, rel_path: str, excluded: bool,
                                 deleted: bool = False):
        full_p = str(Path(self._path) / rel_path)
        if excluded:
            self._excluded_files.add(rel_path)
        else:
            self._excluded_files.discard(rel_path)

        if self._game_id:
            try:
                from core.config_manager import get_config
                cfg = get_config()
                # 1. auto_scan_excluded_files
                excl_files = dict(cfg.get("auto_scan_excluded_files", {}))
                game_excl = dict(excl_files.get(self._game_id, {}))
                path_excl = list(game_excl.get(self._path, []))
                if excluded and rel_path not in path_excl:
                    path_excl.append(rel_path)
                elif not excluded and rel_path in path_excl:
                    path_excl.remove(rel_path)
                if path_excl:
                    game_excl[self._path] = path_excl
                else:
                    game_excl.pop(self._path, None)
                if game_excl:
                    excl_files[self._game_id] = game_excl
                else:
                    excl_files.pop(self._game_id, None)
                cfg.set("auto_scan_excluded_files", excl_files)

                # 2. auto_scan_deleted_paths — what "restore deleted" offers.
                # Only a DELETE belongs here. An unticked file is still on
                # screen with its box empty, so listing it as something to
                # restore asks the user to bring back what never went away.
                del_paths = dict(cfg.get("auto_scan_deleted_paths", {}))
                game_del = list(del_paths.get(self._game_id, []))
                if deleted and full_p not in game_del:
                    game_del.append(full_p)
                elif not excluded and full_p in game_del:
                    game_del.remove(full_p)
                if game_del:
                    del_paths[self._game_id] = game_del
                else:
                    del_paths.pop(self._game_id, None)
                cfg.set("auto_scan_deleted_paths", del_paths)
            except Exception as e:
                logger.debug(f"Could not persist excluded file state: {e}")

        self.selection_changed.emit()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_selectable(self, on: bool) -> None:
        """Enable or disable every file control, remembering the ticks.

        A file inside a path nobody is backing up is not a choice anyone can
        usefully make: the path decides. Leaving the boxes live let the user
        tick files under an unticked path and reasonably expect something to
        come of it. So the whole list follows its path — and the state the
        user had chosen is put back when the path is ticked again rather than
        being flattened to "all on", which is what would make cascading
        destructive instead of merely obedient.
        """
        if on and getattr(self, "_ticks_before_disable", None) is not None:
            self._excluded_files = set(self._ticks_before_disable)
            self._ticks_before_disable = None
            for rel, (cb, _btn) in list(self._file_rows.items()):
                try:
                    cb.blockSignals(True)
                    cb.setChecked(rel not in self._excluded_files)
                    cb.setStyleSheet(
                        "" if rel not in self._excluded_files
                        else "color: #888888; text-decoration: line-through;")
                    cb.blockSignals(False)
                except RuntimeError:
                    continue
        elif not on and getattr(self, "_ticks_before_disable", None) is None:
            self._ticks_before_disable = set(self._excluded_files)

        self._selectable = bool(on)
        for _rel, (cb, btn) in list(self._file_rows.items()):
            try:
                cb.setEnabled(on)
                btn.setEnabled(on)
            except RuntimeError:
                continue
        try:
            self._toggle_btn.setEnabled(True)      # looking is always allowed
        except (AttributeError, RuntimeError):
            pass

    def refresh_from_store(self) -> None:
        """Re-read this path's exclusions and rebuild the visible list.

        Used after a restore: the file comes back into THIS list, where it
        was binned from, instead of arriving as a save path of its own.
        """
        if self._game_id:
            try:
                from core.config_manager import get_config
                saved = (get_config().get("auto_scan_excluded_files", {})
                         or {}).get(self._game_id, {}) or {}
                self._excluded_files = set(saved.get(self._path, []))
            except Exception:
                pass
        self._deleted_files.clear()
        if getattr(self, "_files_built", False):
            self._build_file_list()

    def get_excluded_files(self) -> set[str]:
        """Return set of relative paths the user has excluded."""
        return set(self._excluded_files)

    def set_excluded_files(self, excluded: set[str]):
        """Set excluded files (e.g., from saved preferences)."""
        self._excluded_files = set(excluded)
        for rel_path, (cb, btn) in self._file_rows.items():
            excl = rel_path in self._excluded_files
            cb.blockSignals(True)
            cb.setChecked(not excl)
            cb.blockSignals(False)
            if excl:
                btn.setText("↺")
                btn.setToolTip(t("file_list.restore_file"))
                cb.setStyleSheet("color: #888888; text-decoration: line-through;")
            else:
                btn.setText("✕")
                btn.setToolTip(t("file_list.exclude_file"))
                cb.setStyleSheet("")

    def get_path(self) -> str:
        return self._path

