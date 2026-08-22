"""SaveSync — an archive whose folder is not where it was.

An archive is identified by the destination its saves belong to and the name
it carries: a relative chain under the game, or an absolute one when it lands
in the user's profile. That identity does not move when the FOLDER does — a
drive back under another letter, a collection reorganised — and it does not
move when the files change either.

The files change constantly. An archive is a save folder handed over WITHOUT
a game in the library, which is the point of it: the user goes on playing out
of that folder, deleting a save here and starting a new one there. Nothing
read off disk can be trusted to say which folder this is, so nothing here
reads any.

What is left is one honest question, and it is only ever asked when the
origin in front of us is not the origin on record: the same saves from a new
place, or a different game that happens to share the name? Two paths, side by
side, and the user answers it — the way a sync conflict is answered. What
follows either way is ordinary: a refresh keeps versioning inside the one
archive, and keeping them apart gives the newcomer its own.

"Answer the same for the rest" is not a convenience. Re-adding a collection
from a new drive letter puts every folder in it in this position at once, and
a few hundred of these in a row is not a question, it is an obstruction.
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout)

from i18n import t
from ui.helpers import (apply_game_friendly_flags, center_dialog,
                        finalize_adaptive_dialog_size, scaled)
from ui.styles.theme import palette

# What the caller gets back.
UPDATE = "update"
SEPARATE = "separate"
CANCEL = "cancel"


class ArchiveChoiceDialog(QDialog):
    """Same title, two paths: one archive or two?"""

    def __init__(self, title: str, folder: str, archive: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("manual_path.same_name_title"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        apply_game_friendly_flags(self)
        self._choice = CANCEL
        self._all = False
        self._build(title, folder, archive)
        finalize_adaptive_dialog_size(self, min_w=520, min_h=340)
        center_dialog(self)

    # ── the question ────────────────────────────────────────────────────────

    def _build(self, title: str, folder: str, archive: dict):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        head = QLabel(t("manual_path.same_name_title"))
        head.setObjectName("dialog_heading")
        layout.addWidget(head)

        desc = QLabel(t("manual_path.same_name_desc", name=title))
        desc.setWordWrap(True)
        desc.setObjectName("dialog_desc")
        layout.addWidget(desc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        cards.addWidget(self._card(
            "📦", t("manual_path.same_name_archive"),
            archive.get("where") or t("manual_path.same_name_never"),
            palette("cloud") or "#9b8bd8"))
        cards.addWidget(self._card(
            "📁", t("manual_path.same_name_folder"), folder, "",
            palette("info") or "#5a8fd6"))
        layout.addLayout(cards)

        self._all_cb = QCheckBox(t("manual_path.same_name_all"))
        layout.addWidget(self._all_cb)

        row = QHBoxLayout()
        row.setSpacing(8)
        keep = QPushButton(t("manual_path.same_name_separate"))
        keep.clicked.connect(lambda: self._resolve(SEPARATE))
        update = QPushButton(t("manual_path.same_name_update"))
        update.setObjectName("primary_btn")
        update.clicked.connect(lambda: self._resolve(UPDATE))
        row.addWidget(keep)
        row.addStretch()
        row.addWidget(update)
        layout.addLayout(row)

    def _card(self, icon: str, heading: str, path: str,
              colour: str) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.NoFrame)
        card.setStyleSheet(
            "QFrame { background: %s; border: 1px solid %s40;"
            " border-radius: 8px; padding: 12px; }"
            % (palette("bg_card"), colour))
        col = QVBoxLayout(card)
        col.setSpacing(4)
        glyph = QLabel(icon)
        glyph.setStyleSheet("font-size: %dpx; color: %s;"
                            % (scaled(22, self), colour))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(glyph)
        name = QLabel(heading)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setObjectName("dialog_desc")
        col.addWidget(name)
        # The path is the thing being compared, so it wraps rather than
        # eliding: a folder that moved differs from the one recorded in one
        # component, and hiding the middle hides exactly that component.
        where = QLabel(path)
        where.setWordWrap(True)
        where.setAlignment(Qt.AlignmentFlag.AlignCenter)
        where.setObjectName("backup_row_meta_sm")
        col.addWidget(where)
        return card

    # ── the answer ──────────────────────────────────────────────────────────

    def _resolve(self, choice: str):
        self._choice = choice
        self._all = self._all_cb.isChecked()
        self.accept()

    def reject(self):
        # Escape closes it as a cancel, and the caller abandons the whole
        # batch rather than picking for the user — nothing has been written
        # at this point, so there is nothing half-done to leave behind.
        self._choice = CANCEL
        self._all = False
        super().reject()

    def choice(self) -> str:
        return self._choice

    def applies_to_all(self) -> bool:
        return self._all


def archive_card(entry, manager) -> dict:
    """The bits of an archive this question needs, read from the index."""
    where = ""
    for p in manager.orphan_source_paths(entry):
        if p:
            where = p
            break
    if not where:
        where = next((p for p in (entry.save_paths or []) if p), "")
    return {"game_id": entry.game_id, "where": where,
            "folder": Path(where).name if where else ""}
