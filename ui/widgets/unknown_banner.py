"""SaveSync — the unknown-games carousel on the Overview.

The overlay's detected-games queue, in the page itself: while nothing is being
played, the banner that would say "no active game" offers each game SaveSync
noticed running that is not in the library yet — add it, or say never again —
without going through the overlay hotkey.

It is a second view of ONE queue (ui.unknown_history), not a second queue: it
re-reads that list whenever it changes, and the overlay does the same, so an
answer given in either place is gone from both.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

from i18n import t
from ui import unknown_history
from ui.exe_icon import show_exe_icon
from ui.helpers import ElidedLabel, scaled
from ui.styles.arrow_icons import chevron_button_style
from ui.styles.theme import palette, ThemedMixin


class UnknownGamesBanner(QFrame, ThemedMixin):
    add_requested = Signal(str)        # the entry's exe path
    dismiss_requested = Signal(str)    # "don't show again" for it
    entries_changed = Signal()         # the queue's size or content moved

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("active_banner")          # the page's own banner chrome
        self._entries: list = []
        self._index = 0
        self._build()
        unknown_history.signals.changed.connect(self.refresh)
        self.refresh()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(scaled(12, self, min_px=8), scaled(6, self, min_px=4),
                               scaled(12, self, min_px=8), scaled(6, self, min_px=4))
        lay.setSpacing(scaled(8, self, min_px=4))

        self._prev = QPushButton("")
        self._prev.setFixedSize(scaled(22, self), scaled(40, self))
        self._prev.setStyleSheet(chevron_button_style("left"))
        self._prev.setToolTip(t("add_game.candidate_prev"))
        self._prev.clicked.connect(lambda: self._step(-1))

        self._icon = QLabel("\U0001f3ae")
        self._icon.setObjectName("active_game_icon")

        col = QVBoxLayout()
        col.setSpacing(2)
        self._name = ElidedLabel("", own_tooltip=False)   # the tip is the exe path
        self._name.setObjectName("active_game_name")
        self._sub = QLabel(t("overlay.save_detected"))
        self._sub.setObjectName("active_game_sub")
        col.addWidget(self._name)
        col.addWidget(self._sub)

        self._counter = QLabel("")
        self._counter.setObjectName("active_game_sub")

        self._next = QPushButton("")
        self._next.setFixedSize(scaled(22, self), scaled(40, self))
        self._next.setStyleSheet(chevron_button_style("right"))
        self._next.setToolTip(t("add_game.candidate_next"))
        self._next.clicked.connect(lambda: self._step(1))

        self._add_btn = QPushButton(t("overlay.add_to_library"))
        self._add_btn.setObjectName("primary_btn")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(self._on_add)

        self._never_btn = QPushButton(t("overlay.dont_show_again"))
        self._never_btn.setFlat(True)
        self._never_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._never_btn.clicked.connect(self._on_never)
        self._sty(self._never_btn, lambda: (
            f"QPushButton{{color:{palette('text_muted')};background:transparent;border:none;"
            f"font-size:{scaled(11, self)}px;padding:2px 6px;}}"
            f"QPushButton:hover{{color:{palette('accent')};}}"))

        lay.addWidget(self._prev)
        lay.addWidget(self._icon)
        lay.addLayout(col, 1)
        lay.addWidget(self._counter)
        lay.addWidget(self._next)
        lay.addWidget(self._add_btn)
        lay.addWidget(self._never_btn)

    # ── the queue ────────────────────────────────────────────────────────────

    def has_entries(self) -> bool:
        return bool(self._entries)

    def current_exe(self) -> str:
        if 0 <= self._index < len(self._entries):
            return self._entries[self._index].get("exe", "")
        return ""

    def refresh(self):
        """Re-read the shared queue. A game that has just been noticed is
        shown; otherwise it stays on the same game when it is still there, and
        when it is not the next one takes its place."""
        before = [e.get("exe") for e in self._entries]
        stay = self.current_exe()
        self._entries = unknown_history.visible_entries()
        exes = [e.get("exe") for e in self._entries]
        fresh = [x for x in exes if x not in before]
        if fresh and before:
            self._index = exes.index(fresh[0])       # the queue is newest first
        elif stay in exes:
            self._index = exes.index(stay)
        else:
            self._index = max(0, min(self._index, len(self._entries) - 1))
        self._render()
        if exes != before:
            self.entries_changed.emit()

    def _render(self):
        n = len(self._entries)
        if not n:
            return
        entry = self._entries[self._index]
        self._name.setFullText(entry.get("name") or "?")
        self._name.setToolTip(entry.get("exe", ""))
        # The program's own icon; the controller when it has none to show.
        # Set on every render, so stepping to the next entry never leaves the
        # previous program's icon behind.
        show_exe_icon(self._icon, entry.get("exe", ""), scaled(28, self),
                      "\U0001f3ae", self.devicePixelRatioF())
        many = n > 1
        for w in (self._prev, self._next, self._counter):
            w.setVisible(many)
        if many:
            self._counter.setText(f"{self._index + 1} / {n}")
            self._prev.setEnabled(self._index > 0)
            self._next.setEnabled(self._index < n - 1)

    def _step(self, delta: int):
        n = len(self._entries)
        if n:
            self._index = max(0, min(self._index + delta, n - 1))
            self._render()

    # ── answers ──────────────────────────────────────────────────────────────

    def _on_add(self):
        exe = self.current_exe()
        if exe:
            self.add_requested.emit(exe)

    def _on_never(self):
        exe = self.current_exe()
        if exe:
            self.dismiss_requested.emit(exe)

    def update_locale(self):
        self._sub.setText(t("overlay.save_detected"))
        self._add_btn.setText(t("overlay.add_to_library"))
        self._never_btn.setText(t("overlay.dont_show_again"))
        self._prev.setToolTip(t("add_game.candidate_prev"))
        self._next.setToolTip(t("add_game.candidate_next"))

    def refresh_styles(self):
        super().refresh_styles()
        self._prev.setStyleSheet(chevron_button_style("left"))
        self._next.setStyleSheet(chevron_button_style("right"))
