"""SaveSync - "add files": what to send, chosen in one small panel.

Sending starts from a choice of files and folders, and the one picker the three places that send
(Send a file…, a chat's +, a group's +) shared made that awkward: a folder could only be picked by
walking into it and pressing Select, and there was nowhere to say "files" or "a folder" at all. This
panel is a place to drop files and folders - clicking it opens the one picker, whose type box switches
between files (as many as wanted) and a folder (ui.widgets.file_pickers.FilesOrFolderPickerDialog) - and
the list of what has been chosen so far, so several of each can be put together, and one taken out again.

``pick_paths`` returns the paths (files and folders as chosen) or [] when the person backed out.
What would actually go, and the unticking of single files, is the next step's: see
ui.dialogs.p2p_send_confirm_dialog.
"""
import logging
import os
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette

logger = logging.getLogger(__name__)


def paths_from_mime(mime) -> list:
    """The existing local files and folders a drop carries, in the order given."""
    out = []
    for url in mime.urls() if mime.hasUrls() else ():
        local = url.toLocalFile()
        if local and Path(local).exists():
            out.append(os.path.normpath(local))      # (a URL gives "C:/x/y": the system's own form)
    return out


class _DropZone(QFrame):
    """Somewhere to drop files and folders; clicking it opens the picker."""
    dropped = Signal(list)
    browse = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("p2p_add_drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(scaled(86, self))
        self._hot = False
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        self._label = QLabel(t("p2p.add_files_hint"))
        self._label.setObjectName("p2p_add_drop_label")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._label, 1)
        self._restyle()

    def _restyle(self):
        edge = palette("accent") if self._hot else palette("border")
        fill = palette("bg_input") if self._hot else "transparent"
        self.setStyleSheet(
            f"QFrame#p2p_add_drop{{border:1px dashed {edge};border-radius:6px;background:{fill};}}"
            f"QLabel#p2p_add_drop_label{{color:{palette('text_muted')};"
            f"font-size:{scaled(12, self)}px;background:transparent;border:none;}}")

    def _glow(self, on: bool):
        if self._hot != on:
            self._hot = on
            self._restyle()

    def dragEnterEvent(self, event):
        if paths_from_mime(event.mimeData()):
            self._glow(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._glow(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._glow(False)
        paths = paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths)
        else:
            event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.browse.emit()
            return
        super().mousePressEvent(event)


class P2pAddFilesDialog(QDialog):
    def __init__(self, parent=None, title: str = ""):
        super().__init__(parent)
        self._paths: list = []
        self.setWindowTitle(title or t("p2p.add_files_title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(scaled(520, self))
        self.setAcceptDrops(True)       # (a drop anywhere on the panel counts, not only on the zone)
        self._chrome()
        self._build(title)

    def _chrome(self):
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(palette("bg")))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(palette("text")))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}")

    def _build(self, title: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        if title:
            head = QLabel(title)
            head.setStyleSheet(f"font-weight:700;font-size:{scaled(13, self)}px;")
            head.setWordWrap(True)
            outer.addWidget(head)
        self._zone = _DropZone()
        self._zone.dropped.connect(self.add)
        self._zone.browse.connect(self._choose)
        outer.addWidget(self._zone)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._count_lbl)
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setMinimumHeight(scaled(120, self))
        self._list.setMaximumHeight(scaled(220, self))
        self._list.itemSelectionChanged.connect(self._refresh)
        outer.addWidget(self._list)

        row = QHBoxLayout()
        self._remove_btn = QPushButton(t("p2p.add_files_remove"))
        self._remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self._remove_btn)
        row.addStretch()
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._ok_btn = QPushButton(t("p2p.add_files_continue"))
        self._ok_btn.setObjectName("primary_btn")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self.accept)
        row.addWidget(self._ok_btn)
        outer.addLayout(row)
        self._refresh()

    # ── what has been chosen ─────────────────────────────────────────────

    def paths(self) -> list:
        return list(self._paths)

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    def add(self, paths: Iterable) -> int:
        """Put these files and folders in the list: ones that do not exist (any more) and ones
        already chosen are left out. How many were added."""
        added = 0
        have = {self._key(p) for p in self._paths}
        for p in paths or []:
            p = str(p)
            if not p or not Path(p).exists() or self._key(p) in have:
                continue
            self._paths.append(p)
            have.add(self._key(p))
            icon = "📁" if Path(p).is_dir() else "📄"
            item = QListWidgetItem(f"{icon}  {Path(p).name or p}")
            item.setToolTip(p)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._list.addItem(item)
            added += 1
        self._refresh()
        return added

    def _remove_selected(self):
        for item in self._list.selectedItems():
            p = item.data(Qt.ItemDataRole.UserRole)
            self._paths = [q for q in self._paths if self._key(q) != self._key(p)]
            self._list.takeItem(self._list.row(item))
        self._refresh()

    def _refresh(self):
        n = len(self._paths)
        self._count_lbl.setText(t("p2p.add_files_count", count=n) if n else t("p2p.add_files_none"))
        self._ok_btn.setEnabled(n > 0)
        self._remove_btn.setEnabled(bool(self._list.selectedItems()))

    # ── ways of choosing ─────────────────────────────────────────────────

    def _choose(self):
        from ui.widgets.file_pickers import pick_files_or_folder
        self.add(pick_files_or_folder(self, t("p2p.add_files_pick")))

    def dragEnterEvent(self, event):
        if paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.add(paths)
        else:
            event.ignore()


def pick_paths(parent, title: str = "") -> list:
    """Open the panel; the files and folders chosen, or [] when the person backed out."""
    dlg = P2pAddFilesDialog(parent, title)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    return dlg.paths()
