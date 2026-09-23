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
    """

    def __init__(self, items: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("regression_review.title"))
        self._items = list(items)
        self._rows: dict[str, QFrame] = {}
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

        return row

    def _resolve_row(self, game_id: str, resolved_text: str, ok: bool = True):
        row = self._rows.get(game_id)
        if row is None:
            return
        # Leave the row in place (context stays visible — which game, which
        # date) but replace its actions with a plain outcome label. Only
        # disabled on success — a failure must stay actionable so the user
        # can just try again instead of the row quietly going dead.
        row.setToolTip(resolved_text)
        if ok:
            row.setEnabled(False)

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
            win._backup_game(game_id, force_full=True, silent=True)
        except Exception:
            self._resolve_row(game_id, t("regression_review.action_failed"), ok=False)
            return
        # Queued, not necessarily written yet — the app's own status bar /
        # toast reports the actual outcome once the background job finishes.
        self._resolve_row(game_id, t("regression_review.kept"))

    def _on_restore(self, game_id: str):
        # Routed through MainWindow._restore_game_by_id: background thread,
        # the app's own locked-file retry flow, AND (the part a direct
        # restore_backup() call here would miss) records this backup_id in
        # _last_restored so the NEXT regression check recognizes it as the
        # intended state instead of flagging this very restore as another
        # regression.
        from core.backup import get_backup_manager
        win = self.parent()
        # Prefers newest TRUSTED — a pre_confirmation entry (this game's
        # own held-back capture, or an earlier restore's rejected safety
        # copy) makes a misleading default restore target — but falls
        # back to newest of any kind so "Restore" always has something to
        # point at. See newest_restore_target's own docstring.
        backup_id = get_backup_manager().newest_restore_target(game_id)
        if not backup_id or win is None or not hasattr(win, "_restore_game_by_id"):
            self._resolve_row(game_id, t("regression_review.action_failed"), ok=False)
            return
        # _restore_game_by_id's own locked-file retry flow can pop a
        # WindowModal QMessageBox on this same MainWindow once the
        # background restore finishes — ordering that against THIS dialog's
        # own application-modal exec() is not something to rely on across
        # platforms. Close this panel first (any other unresolved rows get
        # re-surfaced on the next backup run regardless) and defer the
        # actual call a tick, so it only fires once this dialog's own event
        # loop has fully unwound and the restore's dialogs have the screen
        # to themselves.
        from PySide6.QtCore import QTimer
        self.accept()
        # safety_provisional=True: what this overwrites is itself the
        # unverified state this whole panel exists to review — its own
        # pre-restore safety copy stays temporary, not real history, same
        # as everything else about it (see restore_backup's docstring).
        QTimer.singleShot(
            0, lambda: win._restore_game_by_id(
                game_id, backup_id, confirmed=True, safety_provisional=True))
