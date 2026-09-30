"""SaveSync - Regression Review Dialog

Shown after a backup run (single or batch, via Backup Tutti) finds one or
more games where create_backup held back a new backup instead of writing
it, for one of two distinct reasons:

- "regression": current save state exactly matches an OLDER backup instead
  of the newest one — something put an earlier state back (a launcher's
  cloud sync, a manual copy, an external tool).
- "unbacked": current save state matches no known backup at all — new,
  untracked content. Most of the time that is just ordinary offline play,
  but nothing tracked it happening, which is exactly the gap this whole
  panel exists to close.

Either way nothing is silently written over or absorbed as if it were
normal progress, so this is where the player actually resolves each one:
keep the current state after all (force the backup through), or restore
the previous state back onto disk instead (the newest TRUSTED backup when
one exists, else simply the newest on record — see
core.backup.BackupManager.newest_restore_target).
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QWidget, QFrame
)

from i18n import t, format_dt
from ui.helpers import finalize_adaptive_dialog_size, scaled


class RegressionReviewDialog(QDialog):
    """One row per held-back game. *items* is
    ``[(game_id, game_name, kind, older_backup_entry), ...]`` — *kind* is
    ``"regression"`` (matched an older backup — *older_backup_entry* is that
    entry, used only for its date) or ``"unbacked"`` (matched nothing at
    all — *older_backup_entry* is ``None``). Either way the "restore" action
    targets newest_restore_target (the newest TRUSTED backup when one
    exists, else the newest of any kind), never that older matched one.

    Every row is answered once, and answering it is final for the run: its
    buttons give way to the outcome, and the panel closes by itself as soon as
    the last row is answered — there is nothing left to hold the run up for.
    Restores are not started here but collected in ``pending_restores`` and
    run by the caller once this modal has unwound: a restore can raise its own
    dialogs on the main window (locked files), and ordering those against this
    dialog's own exec() is not something to rely on.
    """

    # How long the last outcome stays readable before the panel closes itself.
    _AUTO_CLOSE_MS = 1100

    def __init__(self, items: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("regression_review.title"))
        self._items = list(items)
        self._rows: dict[str, QFrame] = {}
        self._actions: dict[str, list] = {}     # game_id -> the row's buttons
        self._outcomes: dict[str, QLabel] = {}
        self._resolved: set[str] = set()
        # [(game_id, backup_id)] answered "restore" — see the class docstring.
        self.pending_restores: list[tuple[str, str]] = []
        self._build()
        finalize_adaptive_dialog_size(self, min_w=520, min_h=340)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel(t("regression_review.title"))
        title.setObjectName("dialog_heading")
        layout.addWidget(title)

        # Regression and unbacked are different situations with different
        # explanations — shown as their own line each, only for whichever
        # kind(s) actually turned up in this run, rather than one sentence
        # trying to describe both at once.
        kinds_present = {kind for _, _, kind, _ in self._items}
        if "regression" in kinds_present:
            desc_regression = QLabel(t("regression_review.desc_regression"))
            desc_regression.setWordWrap(True)
            desc_regression.setObjectName("dialog_desc")
            layout.addWidget(desc_regression)
        if "unbacked" in kinds_present:
            desc_unbacked = QLabel(t("regression_review.desc_unbacked"))
            desc_unbacked.setWordWrap(True)
            desc_unbacked.setObjectName("dialog_desc")
            layout.addWidget(desc_unbacked)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        for game_id, game_name, kind, older in self._items:
            row = self._make_row(game_id, game_name, kind, older)
            self._rows[game_id] = row
            list_layout.addWidget(row)
        list_layout.addStretch()
        scroll.setWidget(list_widget)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(t("common.close"))
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _make_row(self, game_id: str, game_name: str, kind: str, older) -> QFrame:
        row = QFrame()
        row.setObjectName("regression_review_row")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(10)

        if kind == "unbacked":
            text = t("regression_review.row_unbacked", game=game_name)
        else:
            when = ""
            try:
                when = format_dt(older.created_dt, "%d %b %Y, %H:%M")
            except Exception:
                pass
            text = t("regression_review.row_regression", game=game_name, when=when)
        label = QLabel(text)
        label.setWordWrap(True)
        h.addWidget(label, 1)

        # Shown in place of the buttons once the row is answered.
        outcome = QLabel("")
        outcome.setWordWrap(True)
        outcome.setVisible(False)
        h.addWidget(outcome)
        self._outcomes[game_id] = outcome

        keep_btn = QPushButton(t(f"regression_review.keep_{kind}"))
        keep_btn.setToolTip(t(f"regression_review.keep_{kind}_tip"))
        keep_btn.clicked.connect(
            lambda _=False, gid=game_id: self._on_keep(gid))
        h.addWidget(keep_btn)

        restore_btn = QPushButton(t(f"regression_review.restore_{kind}"))
        restore_btn.setObjectName("primary_btn")
        restore_btn.setToolTip(t(f"regression_review.restore_{kind}_tip"))
        restore_btn.clicked.connect(
            lambda _=False, gid=game_id: self._on_restore(gid))
        h.addWidget(restore_btn)
        self._actions[game_id] = [keep_btn, restore_btn]

        return row

    def _resolve_row(self, game_id: str, resolved_text: str, ok: bool = True):
        """Answer a row: its buttons give way to *resolved_text* (context — which
        game, which date — stays visible). Only on success: a failure keeps the
        buttons so the person can simply try again instead of the row quietly
        going dead. Closes the panel once every row has been answered."""
        row = self._rows.get(game_id)
        if row is None:
            return
        row.setToolTip(resolved_text)
        if not ok:
            return
        for btn in self._actions.get(game_id, []):
            btn.setVisible(False)
        out = self._outcomes.get(game_id)
        if out is not None:
            out.setText(f"\u2713 {resolved_text}")
            out.setVisible(True)
        self._resolved.add(game_id)
        if len(self._resolved) >= len(self._rows):
            QTimer.singleShot(self._AUTO_CLOSE_MS, self.accept)

    def _on_keep(self, game_id: str):
        # Routed through MainWindow's own queue (background thread, adaptive
        # concurrency cap) rather than calling create_backup directly here —
        # same path every other "Backup Now" button uses, so a big save
        # doesn't freeze this modal. force=True bypasses every dedup gate,
        # so this can't loop back into another held-back "unbacked"/
        # regression result and re-open this same panel.
        win = self.parent()
        if win is None or not hasattr(win, "_backup_game"):
            self._resolve_row(game_id, t("regression_review.action_failed"), ok=False)
            return
        try:
            # Not silent: the row promises "see the status bar", and a silent
            # job never writes there.
            win._backup_game(game_id, force_full=True, silent=False)
            if hasattr(win, "_settle_held_back"):
                win._settle_held_back(game_id, keep=True)
        except Exception:
            self._resolve_row(game_id, t("regression_review.action_failed"), ok=False)
            return
        # Queued, not necessarily written yet — the app's own status bar /
        # toast reports the actual outcome once the background job finishes.
        self._resolve_row(game_id, t("regression_review.kept"))

    def _on_restore(self, game_id: str):
        # Recorded, not started: MainWindow._restore_game_by_id (background
        # thread, the app's own locked-file retry flow, AND — the part a
        # direct restore_backup() call would miss — the note in _last_restored
        # so the NEXT regression check recognises this state as intended
        # instead of flagging the restore itself) runs once this modal has
        # closed, see MainWindow._maybe_show_regression_review. Until then the
        # other rows stay answerable; closing the panel used to drop them.
        from core.backup import get_backup_manager
        # Prefers newest TRUSTED — a pre_confirmation entry (this game's
        # own held-back capture, or an earlier restore's rejected safety
        # copy) makes a misleading default restore target — but falls
        # back to newest of any kind so "Restore" always has something to
        # point at. See newest_restore_target's own docstring.
        backup_id = get_backup_manager().newest_restore_target(game_id)
        win = self.parent()
        if not backup_id or win is None or not hasattr(win, "_restore_game_by_id"):
            self._resolve_row(game_id, t("regression_review.action_failed"), ok=False)
            return
        self.pending_restores.append((game_id, backup_id))
        if hasattr(win, "_settle_held_back"):
            win._settle_held_back(game_id, keep=False)
        self._resolve_row(game_id, t("regression_review.restored"))
