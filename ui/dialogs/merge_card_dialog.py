"""SaveSync — which library cards to merge into the one being edited.

The list is every other card in the library, searchable (a library can hold hundreds). A card a game is
running for right now cannot be picked: deleting a card in the middle of a session would break the
playtime and the backup taken when it ends. Cards already staged come pre-ticked, so unticking one
takes it back.

Nothing is merged here: the dialog only answers WHICH cards. The merge happens when the edit dialog is
saved (core.merge_cards).
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout)

from i18n import t
from ui.helpers import (apply_game_friendly_flags, center_dialog,
                        finalize_adaptive_dialog_size)

_ID_ROLE = Qt.ItemDataRole.UserRole


class MergeCardDialog(QDialog):
    """Pick the cards that will be merged into *keep_name*'s card."""

    def __init__(self, keep_id: str, keep_name: str, staged=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("add_game.merge_cards_title"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        apply_game_friendly_flags(self)
        self._keep_id = keep_id
        self._staged = set(staged or ())
        self._build(keep_name)
        finalize_adaptive_dialog_size(self, min_w=560, min_h=460)
        center_dialog(self)

    def _build(self, keep_name: str):
        from core.library import get_library
        from core.merge_cards import playing_ids
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        head = QLabel(t("add_game.merge_cards_title"))
        head.setObjectName("dialog_heading")
        layout.addWidget(head)
        desc = QLabel(t("add_game.merge_desc", name=keep_name))
        desc.setWordWrap(True)
        desc.setObjectName("dialog_desc")
        layout.addWidget(desc)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText(t("add_game.merge_filter"))
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        playing = playing_ids()
        games = sorted((g for g in get_library().all_games() if g.id != self._keep_id),
                       key=lambda g: (g.name or "").casefold())
        for g in games:
            where = g.exe_path or (g.save_paths[0] if g.save_paths else "")
            item = QListWidgetItem(f"{g.name}    —    {where}" if where else g.name)
            item.setData(_ID_ROLE, g.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, f"{g.name} {where}".casefold())
            flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            if g.id in playing:
                flags &= ~Qt.ItemFlag.ItemIsEnabled
                item.setToolTip(t("add_game.merge_playing"))
            else:
                item.setToolTip(where)
            item.setFlags(flags)
            item.setCheckState(Qt.CheckState.Checked if g.id in self._staged else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.currentItemChanged.connect(self._show_info)
        layout.addWidget(self._list, 1)

        self._info = QLabel("")
        self._info.setObjectName("dialog_desc")
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        row = QHBoxLayout()
        row.setSpacing(8)
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        ok = QPushButton(t("add_game.merge_stage"))
        ok.setObjectName("primary_btn")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addStretch()
        row.addWidget(ok)
        layout.addLayout(row)

    def _apply_filter(self, text: str):
        needle = (text or "").strip().casefold()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(needle) and needle not in (item.data(Qt.ItemDataRole.UserRole + 1) or ""))

    def _show_info(self, item, _previous=None):
        if item is None:
            self._info.setText("")
            return
        from core.merge_cards import describe_card
        d = describe_card(item.data(_ID_ROLE))
        self._info.setText(t("add_game.merge_info", exes=d["exes"], paths=d["paths"], backups=d["backups"]))

    def chosen_ids(self) -> list:
        """The ticked cards, in list order."""
        return [self._list.item(i).data(_ID_ROLE) for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.CheckState.Checked]
