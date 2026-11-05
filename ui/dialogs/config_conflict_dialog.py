"""SaveSync — the configuration on the provider is not the one this PC last read.

Asked BEFORE anything is uploaded (and before a game that a merge made on the other PC concerns is synced),
because both would otherwise send this PC's picture of the library over the other PC's: a merge made there
would be undone, and the backups of the merged titles cut off from the folder that holds them.

What can be answered depends on what was found (core.config_sync.Preflight):

- another PC's library differs (titles this one lacks, merges it made): import it - the titles are added
  and the merges made here too, one by one as ticked - or keep this PC's library apart (a configuration
  file of its own, and cloud folders of their own for the titles in conflict), or decide later;
- the file cannot be read: replace it with this PC's, keep apart, or later;
- this PC's library is much smaller than the one last uploaded: upload anyway, or later.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

from i18n import t
from ui.helpers import apply_game_friendly_flags, center_dialog, finalize_adaptive_dialog_size

IMPORT = "import"
SEPARATE = "separate"
OVERWRITE = "overwrite"
LATER = "later"

_SHOWN_TITLES = 8


class ConfigConflictDialog(QDialog):
    """One question, a few answers; ``choice`` and ``merge_pairs`` say which."""

    def __init__(self, pre, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("config_sync.title"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        apply_game_friendly_flags(self)
        self.choice = LATER
        self._pre = pre
        self._merge_boxes: list = []
        self._build(pre)
        finalize_adaptive_dialog_size(self, min_w=540, min_h=300)
        center_dialog(self)

    @property
    def merge_pairs(self) -> list:
        """[(keep_id, absorb_id)] of the merges left ticked."""
        return [(m["keep_id"], m["absorb_id"]) for cb, m in self._merge_boxes if cb.isChecked()]

    def _label(self, text: str, name: str = "dialog_desc") -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setObjectName(name)
        return lbl

    def _build(self, pre):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(self._label(t("config_sync.title"), "dialog_heading"))

        buttons = []        # (text, choice, primary)
        if pre.kind == "foreign":
            layout.addWidget(self._label(t("config_sync.foreign_msg", machine=pre.machine_name or "?", when=(pre.exported_at or "")[:16].replace("T", " "))))
            if pre.new_games:
                shown = ", ".join(f"«{n}»" for n in pre.new_games[:_SHOWN_TITLES])
                more = len(pre.new_games) - _SHOWN_TITLES
                layout.addWidget(self._label(t("config_sync.foreign_new", count=len(pre.new_games), names=shown)
                                             + (" " + t("config_sync.foreign_more", count=more) if more > 0 else "")))
            if pre.merges:
                layout.addWidget(self._label(t("config_sync.merges_head")))
                for m in pre.merges:
                    cb = QCheckBox(t("config_sync.merge_item", absorb=m["absorb_name"], keep=m["keep_name"], backups=m["backups"]))
                    cb.setChecked(True)
                    layout.addWidget(cb)
                    self._merge_boxes.append((cb, m))
                layout.addWidget(self._label(t("config_sync.hold_hint")))
            buttons = [(t("config_sync.btn_separate"), SEPARATE, False), (t("config_sync.btn_later"), LATER, False),
                       (t("config_sync.btn_import"), IMPORT, True)]
        elif pre.kind == "unreadable":
            key = "config_sync.unreadable_newer" if pre.detail == "newer" else "config_sync.unreadable_msg"
            layout.addWidget(self._label(t(key)))
            buttons = [(t("config_sync.btn_separate"), SEPARATE, False), (t("config_sync.btn_later"), LATER, False),
                       (t("config_sync.btn_overwrite"), OVERWRITE, True)]
        else:   # shrunk
            layout.addWidget(self._label(t("config_sync.shrunk_msg", now=pre.local_games, before=pre.last_games)))
            buttons = [(t("config_sync.btn_later"), LATER, False), (t("config_sync.btn_upload_anyway"), OVERWRITE, True)]

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, choice, primary in buttons:
            b = QPushButton(text)
            if primary:
                b.setObjectName("primary_btn")
            b.clicked.connect(lambda _=False, c=choice: self._answer(c))
            if primary and len(buttons) > 1:
                row.addStretch()
            row.addWidget(b)
        layout.addLayout(row)

    def _answer(self, choice: str):
        self.choice = choice
        self.accept()

    def reject(self):
        self.choice = LATER
        super().reject()
