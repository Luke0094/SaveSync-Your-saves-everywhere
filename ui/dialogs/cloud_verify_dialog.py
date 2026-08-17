"""
SaveSync — Cloud Save Verification Dialog

Shown for an UNKNOWN game when the cloud already holds save(s) under this
game's name-derived folder. Two genuinely different games can share such a
folder (they have the same title), so this dialog shows what each cloud copy
was registered with — name + install path — and lets the user decide:

  • download a specific cloud copy (it IS this game's), or
  • declare a homonym (a same-name DIFFERENT game) → skip download; the game
    then takes its OWN cloud folder instead of contaminating the other's.

With one candidate it's a details/confirm prompt; with several it's a picker.
"""
import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QRadioButton, QButtonGroup,
)

from i18n import t
from ui.helpers import finalize_adaptive_dialog_size


def _esc(s: str) -> str:
    return html.escape(s or "")


class CloudVerifyDialog(QDialog):
    """Verify whether cloud save(s) under this name belong to this game.

    *candidates* — list of dicts ``{"folder", "name", "path"}``, one per cloud
    folder sharing the game's base name.
    """

    # (choice, folder): choice ∈ {"download", "homonym", "cancel"};
    # *folder* is the chosen cloud folder for "download", else "".
    resolution = Signal(str, str)

    # QButtonGroup treats id -1 as "auto-assign", so the homonym radio needs a
    # sentinel that can never collide with a candidate index (0..N-1).
    _HOMONYM_ID = 1_000_000

    def __init__(self, game_name: str, candidates: list, parent=None):
        super().__init__(parent)
        self._candidates = list(candidates or [])
        self._game_name = game_name
        self._radios = QButtonGroup(self)
        self.setWindowTitle(t("cloud_verify.title"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._choice = "cancel"
        self._build()
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=480, min_h=360)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel(t("cloud_verify.title"))
        title.setObjectName("dialog_heading")
        layout.addWidget(title)

        detected = QLabel(f"{t('cloud_verify.detected')}: <b>{_esc(self._game_name)}</b>")
        detected.setWordWrap(True)
        detected.setObjectName("dialog_desc")
        layout.addWidget(detected)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        multi = len(self._candidates) > 1
        prompt = QLabel(t("cloud_verify.which_copy") if multi else t("cloud_verify.registered_as"))
        prompt.setWordWrap(True)
        prompt.setObjectName("dialog_desc")
        layout.addWidget(prompt)

        for i, c in enumerate(self._candidates):
            layout.addWidget(self._make_candidate_row(c, i, multi))

        if multi:
            none_rb = QRadioButton(t("cloud_verify.none_homonym"))
            self._radios.addButton(none_rb, self._HOMONYM_ID)   # homonym sentinel
            layout.addWidget(none_rb)
            first = self._radios.button(0)
            if first is not None:
                first.setChecked(True)                   # default: first candidate

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep2)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        cancel = QPushButton(t("cloud_verify.cancel"))
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        btn_row.addStretch()

        if multi:
            dl = QPushButton(t("cloud_verify.download_selected"))
            dl.setObjectName("primary_btn")
            dl.clicked.connect(self._on_download_selected)
            btn_row.addWidget(dl)
        else:
            homonym = QPushButton(t("cloud_verify.is_homonym"))
            homonym.clicked.connect(lambda: self._resolve("homonym", ""))
            btn_row.addWidget(homonym)
            _folder = self._candidates[0].get("folder", "") if self._candidates else ""
            dl = QPushButton(t("cloud_verify.download"))
            dl.setObjectName("primary_btn")
            dl.clicked.connect(lambda: self._resolve("download", _folder))
            btn_row.addWidget(dl)

        layout.addLayout(btn_row)

    def _make_candidate_row(self, c: dict, idx: int, multi: bool) -> QFrame:
        row = QFrame()
        row.setObjectName("cloud_verify_row")
        rl = QVBoxLayout(row)
        head = QHBoxLayout()
        if multi:
            rb = QRadioButton()
            self._radios.addButton(rb, idx)
            head.addWidget(rb)
        name_lbl = QLabel(f"☁  <b>{_esc(c.get('name') or c.get('folder', ''))}</b>")
        head.addWidget(name_lbl)
        head.addStretch()
        rl.addLayout(head)
        path = c.get("path") or t("common.unknown")
        path_lbl = QLabel(f"{t('cloud_verify.path')}: {_esc(path)}")
        path_lbl.setWordWrap(True)
        path_lbl.setObjectName("dialog_desc")
        rl.addWidget(path_lbl)
        return row

    def _on_download_selected(self):
        gid = self._radios.checkedId()
        if gid == self._HOMONYM_ID:
            self._resolve("homonym", "")
        elif 0 <= gid < len(self._candidates):
            self._resolve("download", self._candidates[gid].get("folder", ""))
        else:
            self._resolve("cancel", "")

    def _resolve(self, choice: str, folder: str):
        self._choice = choice
        self.resolution.emit(choice, folder)
        self.accept()

    def reject(self):
        """Escape / window close — emit cancel so the caller never hangs."""
        self._choice = "cancel"
        self.resolution.emit("cancel", "")
        super().reject()

    def get_choice(self) -> str:
        return self._choice
