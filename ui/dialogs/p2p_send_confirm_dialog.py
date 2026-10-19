"""SaveSync - "Choose what to send" page for a file or folder sent by hand.

What is sent here may have nothing to do with saves, and it is NOT zipped —
every file goes as it is — so before anything is published the person sees
exactly which files would leave this machine and can turn any of them off.
At least one has to stay selected to continue.

The listing is capped: a torrent over tens of thousands of files is slow to
hash and unreadable as a checklist, so a folder with more than MAX_FILES asks
for a smaller one instead.
"""
import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem,
)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

MAX_FILES = 5000
# Directories visited before giving up: a link loop that holds no files would
# otherwise never reach MAX_FILES.
_MAX_DIRS = 50_000


def collect_files(source: Path) -> tuple:
    """(files, too_many) for *source*: ``[(path on disk, relative path)]``
    sorted by name. A single file is itself; a folder is walked (links are not
    followed) and keeps its own name as the top of every relative path, so the
    receiver gets the folder back, not its loose contents. *too_many* is True
    when the listing was cut off at MAX_FILES."""
    source = Path(source)
    if source.is_file():
        return [(source, source.name)], False
    found: list = []
    dirs = 0
    too_many = False
    for root, subdirs, names in os.walk(source, followlinks=False):
        dirs += 1
        if dirs > _MAX_DIRS:
            too_many = True
            break
        for name in names:
            full = Path(root) / name
            try:
                if full.is_symlink() or not full.is_file():
                    continue
            except OSError:
                continue
            rel = (Path(source.name) / full.relative_to(source)).as_posix()
            found.append((full, rel))
            if len(found) > MAX_FILES:
                too_many = True
                break
        if too_many:
            break
    if too_many:
        return [], True
    found.sort(key=lambda p: p[1].casefold())
    return found, False


def collect_many(sources) -> tuple:
    """(files, too_many) for several sources at once (files and folders dropped
    together): each is collected as collect_files does, and a top-level name two
    of them share becomes "name (2)" so that nothing is staged on top of
    anything else."""
    files: list = []
    used: set = set()
    too_many = False
    for src in sources:
        src = Path(src)
        part, many = collect_files(src)
        too_many = too_many or many
        if not part:
            continue
        top = Path(part[0][1]).parts[0]
        stem, suffix = (top, "") if src.is_dir() else os.path.splitext(top)
        new_top, n = top, 2
        while new_top.casefold() in used:
            new_top = f"{stem} ({n}){suffix}"
            n += 1
        used.add(new_top.casefold())
        for path, rel in part:
            files.append((path, "/".join((new_top,) + Path(rel).parts[1:])))
        if len(files) > MAX_FILES:
            too_many = True
            break
    if too_many:
        return [], True
    files.sort(key=lambda p: p[1].casefold())
    return files, False


class P2pSendConfirmDialog(QDialog):
    """Lists the files of *source* as checkboxes (*source* may also be a list of
    several files and folders). ``selected_files()`` is the answer once the
    dialog is accepted."""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        many = isinstance(source, (list, tuple))
        self._sources = [Path(s) for s in source] if many else [Path(source)]
        self._source = self._sources[0]
        # What the offer is called at the top: its name, or how many things it is.
        self._title_text = (t("p2p.send_confirm_items", count=len(self._sources))
                            if len(self._sources) > 1 else (self._source.name or str(self._source)))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t("p2p.send_confirm_title"))
        self._apply_window_chrome()
        self._files, self._too_many = (collect_many(self._sources) if len(self._sources) > 1
                                       else collect_files(self._source))
        self._sizes: list = []
        for path, _rel in self._files:
            try:
                self._sizes.append(path.stat().st_size)
            except OSError:
                self._sizes.append(0)
        self._build()
        self._update_summary()

    def _apply_window_chrome(self):
        bg = QColor(palette("bg"))
        fg = QColor(palette("text"))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            f"QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}")
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self)
        except Exception:
            pass

    def _build(self):
        from core import fmt_size
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        self.setMinimumWidth(scaled(460, self))
        self.setMinimumHeight(scaled(380, self))

        name_lbl = QLabel(self._title_text)
        name_lbl.setStyleSheet(f"font-weight:700;font-size:{scaled(13, self)}px;")
        name_lbl.setWordWrap(True)
        outer.addWidget(name_lbl)

        intro = QLabel(t("p2p.send_confirm_intro"))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(intro)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setUniformItemSizes(True)
        self._list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        for (path, rel), size in zip(self._files, self._sizes):
            item = QListWidgetItem(f"{rel}    ({fmt_size(size)})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setToolTip(str(path))
            self._list.addItem(item)
        self._list.itemChanged.connect(lambda _i: self._update_summary())
        outer.addWidget(self._list, 1)

        self._note_lbl = QLabel("")
        self._note_lbl.setWordWrap(True)
        self._note_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        outer.addWidget(self._note_lbl)
        if self._too_many:
            self._list.setVisible(False)
            self._note_lbl.setText(
                t("p2p.send_confirm_too_many", max=MAX_FILES, name=self._title_text))
        elif not self._files:
            self._list.setVisible(False)
            self._note_lbl.setText(
                t("p2p.send_confirm_empty", name=self._title_text))

        pick_row = QHBoxLayout()
        self._all_btn = QPushButton(t("p2p.send_confirm_all"))
        self._all_btn.clicked.connect(lambda: self._set_all(True))
        pick_row.addWidget(self._all_btn)
        self._none_btn = QPushButton(t("p2p.send_confirm_none"))
        self._none_btn.clicked.connect(lambda: self._set_all(False))
        pick_row.addWidget(self._none_btn)
        pick_row.addStretch()
        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
        pick_row.addWidget(self._summary_lbl)
        outer.addLayout(pick_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        self._send_btn = QPushButton(t("p2p.send_confirm_send"))
        self._send_btn.setObjectName("primary_btn")
        self._send_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._send_btn)
        outer.addLayout(btn_row)

    def _checked(self) -> list:
        return [i for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.CheckState.Checked]

    def _set_all(self, on: bool):
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._list.blockSignals(True)
        try:
            for i in range(self._list.count()):
                self._list.item(i).setCheckState(state)
        finally:
            self._list.blockSignals(False)
        self._update_summary()

    def _update_summary(self):
        from core import fmt_size
        picked = self._checked()
        total = sum(self._sizes[i] for i in picked)
        self._summary_lbl.setText(
            t("p2p.send_confirm_summary", selected=len(picked),
              total=len(self._files), size=fmt_size(total)))
        ok = bool(picked)
        self._send_btn.setEnabled(ok)
        self._send_btn.setToolTip("" if ok else t("p2p.send_confirm_need_one"))
        self._all_btn.setEnabled(len(picked) < len(self._files))
        self._none_btn.setEnabled(bool(picked))

    def selected_files(self) -> list:
        """``[(path on disk, relative path)]`` of the files left ticked."""
        return [self._files[i] for i in self._checked()]

    def accept(self):
        # The button is disabled with nothing ticked; Enter must not slip past it.
        if not self._checked():
            return
        super().accept()
