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
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame,
                               QHBoxLayout, QLabel, QPushButton, QRadioButton,
                               QVBoxLayout)

from i18n import t
from ui.helpers import (apply_game_friendly_flags, center_dialog,
                        finalize_adaptive_dialog_size, scaled)
from ui.styles.theme import palette

# What the caller gets back.
UPDATE = "update"
SEPARATE = "separate"
CANCEL = "cancel"
ADD = "add"
SKIP = "skip"


class ArchiveChoiceDialog(QDialog):
    """Same title, two paths: one archive or two?"""

    def __init__(self, title: str, folder: str, archive: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self._window_title())
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        apply_game_friendly_flags(self)
        self._choice = CANCEL
        self._all = False
        self._build(title, folder, archive)
        finalize_adaptive_dialog_size(self, min_w=520, min_h=340)
        center_dialog(self)

    def _window_title(self) -> str:
        return t("manual_path.same_name_title")

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
            "📁", t("manual_path.same_name_folder"), folder,
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

    def _frame(self, colour: str) -> QFrame:
        card = QFrame()
        card.setObjectName("archive_choice_card")
        card.setFrameShape(QFrame.Shape.NoFrame)
        # By name, not "QFrame": a QLabel is a QFrame, and every label inside
        # the card picked up its border and padding too — a box in a box.
        card.setStyleSheet(
            "QFrame#archive_choice_card { background: %s; border: 1px solid %s40;"
            " border-radius: 8px; padding: 12px; }"
            % (palette("bg_card"), colour))
        return card

    def _card(self, icon: str, heading: str, path: str,
              colour: str) -> QFrame:
        card = self._frame(colour)
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
        cb = getattr(self, "_all_cb", None)
        self._all = bool(cb is not None and cb.isChecked())
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


def holder_line(rec: dict) -> tuple:
    """``(title, detail)`` for one entry stored under a name (see
    BackupManager.name_holders): what it is, and when it was last backed up —
    the way to tell two games of one name apart is the last time each was
    played, and a tagged folder name says which one this is."""
    from core import to_local_dt
    from i18n import format_dt
    game = rec.get("kind") == "game"
    kind = t("manual_path.name_collision_row_game" if game
             else "manual_path.name_collision_row_archive")
    title = "%s  %s  ·  %s" % ("🎮" if game else "📦", rec.get("name", ""), kind)
    folder = rec.get("folder") or ""
    if folder and folder != rec.get("name"):
        title += "  (%s)" % folder
    dt = to_local_dt(rec["last"]) if rec.get("last") else None
    if dt is not None:
        detail = t("manual_path.name_collision_last",
                   when=format_dt(dt, "%d %b %Y  %H:%M"), count=rec.get("count", 0))
    else:
        detail = t("manual_path.name_collision_no_backup")
    if rec.get("where"):
        detail += "\n" + rec["where"]
    return title, detail


class NameCollisionDialog(ArchiveChoiceDialog):
    """A backup folder with this name exists, and what is being added is not
    in it. Whether these are other saves of one of the entries stored under
    the name or a different game that shares it is for the user to say —
    nothing on disk can — so it lists what is stored, each with its last
    backup (the last time that game was played), and asks: add it to one of
    them as another version, keep both (its own archive, its name given a
    tag), or don't ask again about this folder.

    The window's own close button is "ask me later": nothing is stored and
    nothing is written for this folder, and the question comes back the next
    time it is added. It is not a fourth button because it is not an answer.

    Opened with the folder name as *title*, the folder being added as
    *folder* and *data* = ``{"rows": [{"title", "detail", "can_add", "why"}],
    "changed": "…"}`` — one row per entry stored under the name."""

    def _window_title(self) -> str:
        return t("manual_path.name_collision_title")

    def _build(self, title: str, folder: str, data: dict):
        rows = data.get("rows", [])
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        head = QLabel(t("manual_path.name_collision_title"))
        head.setObjectName("dialog_heading")
        layout.addWidget(head)

        desc = QLabel(t("manual_path.name_collision_desc", folder=title))
        desc.setWordWrap(True)
        desc.setObjectName("dialog_desc")
        layout.addWidget(desc)

        if rows:
            stored = QLabel(t("manual_path.name_collision_stored"))
            stored.setObjectName("dialog_desc")
            layout.addWidget(stored)
            for r in rows:
                layout.addWidget(self._entry(r["title"], r["detail"],
                                             palette("cloud") or "#9b8bd8"))

        adding = self._frame(palette("info") or "#5a8fd6")
        col = QVBoxLayout(adding)
        col.setSpacing(4)
        name = QLabel("📁  " + t("manual_path.same_name_folder"))
        name.setObjectName("dialog_desc")
        col.addWidget(name)
        where = QLabel(folder + ("\n" + data["changed"] if data.get("changed") else ""))
        where.setWordWrap(True)
        where.setObjectName("backup_row_meta_sm")
        col.addWidget(where)
        layout.addWidget(adding)

        self._all_cb = QCheckBox(t("manual_path.name_collision_all"))
        layout.addWidget(self._all_cb)

        row = QHBoxLayout()
        row.setSpacing(8)
        dont = QPushButton(t("manual_path.name_collision_dont_ask"))
        dont.setToolTip(t("manual_path.name_collision_dont_ask_tip"))
        dont.clicked.connect(lambda: self._resolve(SKIP))
        keep = QPushButton(t("manual_path.name_collision_keep"))
        # A folder an archive already reads is that archive's: a second archive
        # of it would be two entries for one folder.
        known = bool(data.get("known"))
        keep.setEnabled(not known)
        keep.setToolTip(t("manual_path.name_collision_keep_known") if known
                        else t("manual_path.name_collision_keep_tip"))
        keep.clicked.connect(lambda: self._resolve(SEPARATE))
        add = QPushButton(t("manual_path.name_collision_add"))
        add.setObjectName("primary_btn")
        can_add = any(r.get("can_add") for r in rows)
        add.setEnabled(can_add)
        add.setToolTip(t("manual_path.name_collision_add_tip") if can_add
                       else ((rows[0].get("why") if rows else "")
                             or t("manual_path.name_collision_add_none")))
        add.clicked.connect(lambda: self._resolve(ADD))

        # "Don't ask again for the rest" only carries over that answer; add and
        # keep-both each pick a target. Ticked, they are not offered — rather
        # than clicked and quietly not applied to the rest.
        def _blanket(on: bool):
            keep.setEnabled(not on and not known)
            add.setEnabled(can_add and not on)
        self._all_cb.toggled.connect(_blanket)
        row.addWidget(dont)
        row.addStretch()
        row.addWidget(keep)
        row.addWidget(add)
        layout.addLayout(row)

    def _entry(self, title: str, detail: str, colour: str) -> QFrame:
        card = self._frame(colour)
        col = QVBoxLayout(card)
        col.setSpacing(2)
        head = QLabel(title)
        head.setWordWrap(True)
        col.addWidget(head)
        meta = QLabel(detail)
        meta.setWordWrap(True)
        meta.setObjectName("backup_row_meta_sm")
        col.addWidget(meta)
        return card


class NameTargetDialog(NameCollisionDialog):
    """The extra step, for when more than one entry is stored under the name —
    a second game kept apart last time, say. Which one are these saves for?
    Each is listed with its last backup so the user can tell them apart.

    Opened for *mode* ADD (pick the entry to add them to) or SEPARATE (keep
    both: they may be a new archive of their own, the last row, or belong to
    one already kept). ``selected()`` is the entry's game_id, or "" for a new
    archive; the X is "ask me later" here too."""

    def _window_title(self) -> str:
        return t("manual_path.name_target_title")

    def _build(self, title: str, folder: str, data: dict):
        mode, rows = data["mode"], data["rows"]
        self._selected = ""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        head = QLabel(t("manual_path.name_target_title"))
        head.setObjectName("dialog_heading")
        layout.addWidget(head)
        desc = QLabel(t("manual_path.name_target_desc_add" if mode == ADD
                        else "manual_path.name_target_desc_keep", folder=title))
        desc.setWordWrap(True)
        desc.setObjectName("dialog_desc")
        layout.addWidget(desc)

        self._group = QButtonGroup(self)
        self._radios: dict = {}
        # Keep both says they are a different game: the library game is listed
        # (it is one of the entries stored under the name) but is not a place
        # to put them — that is what Add to it is for.
        cards = [(r["id"], r["title"], r["detail"],
                  r.get("can_add", True) and not (mode == SEPARATE and r.get("kind") == "game"),
                  t("manual_path.name_target_game_not_here")
                  if (mode == SEPARATE and r.get("kind") == "game") else r.get("why", ""))
                 for r in rows]
        if mode == SEPARATE:
            cards.append(("", t("manual_path.name_target_new"),
                          t("manual_path.name_target_new_detail"), True, ""))
        for rid, rtitle, rdetail, ok, why in cards:
            frame = self._frame(palette("cloud") or "#9b8bd8")
            col = QVBoxLayout(frame)
            col.setSpacing(2)
            radio = QRadioButton(rtitle)
            radio.setEnabled(ok)
            if not ok:
                radio.setToolTip(why)
            self._group.addButton(radio)
            self._radios[rid] = radio
            col.addWidget(radio)
            meta = QLabel(rdetail)
            meta.setWordWrap(True)
            meta.setObjectName("backup_row_meta_sm")
            meta.mousePressEvent = (lambda _e, rb=radio: rb.setChecked(True) if rb.isEnabled() else None)
            col.addWidget(meta)
            layout.addWidget(frame)
            radio.toggled.connect(lambda _c: self._sync())
        if mode == SEPARATE:
            self._radios[""].setChecked(True)
        else:
            # The archive that reads the folder is the natural target.
            reads = next((r["id"] for r in rows
                          if r.get("reads") and self._radios[r["id"]].isEnabled()), None)
            if reads is not None:
                self._radios[reads].setChecked(True)

        row = QHBoxLayout()
        row.addStretch()
        self._ok = QPushButton(t("manual_path.name_target_ok"))
        self._ok.setObjectName("primary_btn")
        self._ok.clicked.connect(self._confirm)
        row.addWidget(self._ok)
        layout.addLayout(row)
        self._sync()

    def _sync(self):
        ok = getattr(self, "_ok", None)      # not there yet while a row is being pre-selected
        if ok is not None:
            ok.setEnabled(any(rb.isChecked() for rb in self._radios.values()))

    def _confirm(self):
        self._selected = next((rid for rid, rb in self._radios.items() if rb.isChecked()), "")
        self._resolve(ADD)

    def selected(self) -> str:
        return getattr(self, "_selected", "")


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
