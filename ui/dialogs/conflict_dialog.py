"""
SaveSync - Sync Conflict Dialog
Shows local vs cloud save metadata and lets user choose resolution.
"""
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)

from i18n import t
from ui.styles.theme import palette


class ConflictDialog(QDialog):
    """Shown when local and remote saves were both modified since last sync."""

    resolution = Signal(str)  # "local" | "cloud" | "both"

    def __init__(
        self,
        game_name: str,
        local_mtime: Optional[datetime],
        cloud_mtime: Optional[datetime],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(t("sync.conflict_title"))
        self.setMinimumWidth(440)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._choice: str = "local"
        self._build(game_name, local_mtime, cloud_mtime)

    def _build(self, game_name: str, local_dt: Optional[datetime], cloud_dt: Optional[datetime]):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel(t("sync.conflict_title"))
        title.setObjectName("page_header")
        title.setStyleSheet("font-size: 18px;")
        layout.addWidget(title)

        desc = QLabel(f"<b>{game_name}</b><br>{t('sync.conflict_desc')}")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {palette('text_secondary')}; line-height: 1.5;")
        layout.addWidget(desc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        def _fmt(dt: Optional[datetime]) -> str:
            if not dt:
                return t("common.unknown")
            from i18n import format_dt
            return format_dt(dt, "%d %b %Y, %H:%M:%S")

        # Version cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)

        local_color = palette("info") or "#5a8fd6"
        cloud_color = palette("cloud") or "#9b8bd8"

        local_card = self._make_card(
            "💾", t("sync.local_version", date=_fmt(local_dt)), local_color
        )
        cloud_card = self._make_card(
            "☁", t("sync.cloud_version", date=_fmt(cloud_dt)), cloud_color
        )
        cards_row.addWidget(local_card)
        cards_row.addWidget(cloud_card)
        layout.addLayout(cards_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep2)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        keep_local = QPushButton(t("sync.keep_local"))
        keep_local.clicked.connect(lambda: self._resolve("local"))

        keep_cloud = QPushButton(t("sync.keep_cloud"))
        keep_cloud.setObjectName("primary_btn")
        keep_cloud.clicked.connect(lambda: self._resolve("cloud"))

        keep_both = QPushButton(t("sync.keep_both"))
        keep_both.clicked.connect(lambda: self._resolve("both"))

        btn_row.addWidget(keep_local)
        btn_row.addWidget(keep_both)
        btn_row.addStretch()
        btn_row.addWidget(keep_cloud)
        layout.addLayout(btn_row)

    def _make_card(self, icon: str, text: str, color: str) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.NoFrame)
        card.setStyleSheet(f"""
            QFrame {{
                background: {palette('bg_card')};
                border: 1px solid {color}40;
                border-radius: 8px;
                padding: 12px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet(f"font-size: 24px; color: {color};")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_lbl = QLabel(text)
        text_lbl.setStyleSheet(f"color: {palette('text_secondary')}; font-size: 12px;")
        text_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_lbl.setWordWrap(True)
        card_layout.addWidget(icon_lbl)
        card_layout.addWidget(text_lbl)
        return card

    def _resolve(self, choice: str):
        self._choice = choice
        self.resolution.emit(choice)
        self.accept()

    def reject(self):
        """Escape key / window close — emit cancelled so caller doesn't hang."""
        self._choice = "cancel"
        self.resolution.emit("cancel")
        super().reject()

    def get_choice(self) -> str:
        return self._choice
