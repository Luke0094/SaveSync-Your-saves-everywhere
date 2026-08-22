"""SaveSync — "Your user paths", for an archive.

An archive is a save folder handed over without a game in the library, so
nothing else in the app holds its settings: this panel IS where it says which
folders it is made of and which files inside them count. That is the same
question Add/Edit Game asks of a library game, and it is asked here with the
same widgets — a row per folder with an include tick, a ✕, and a collapsible
file list whose ticks and bins work exactly as they do there.

Three answers, and they are not the same answer:

  **unticked**  the folder stays on the archive and is skipped by the next
                backup. "Not this time."
  **✕**         the folder was never this archive's. It is gone, and so is
                anything remembered about the files inside it.
  **not here**  no answer at all — a drive off its cable. Skipped this time,
                back in the next backup on its own, and never dropped by the
                backup that could not reach it.

The way back from a ✕ on a FILE is the ignored-paths list at the bottom, the
same one Add/Edit Game carries: a bin is permanent, so it needs somewhere
that undoes it.
"""
import logging
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from core.backup import get_backup_manager
from i18n import t
from ui.helpers import (center_dialog, finalize_adaptive_dialog_size,
                        lock_min_size, scaled)
from ui.styles.theme import palette
from ui.widgets.path_row import PathRow

logger = logging.getLogger(__name__)


class ArchivePathsDialog(QDialog):
    """The folders an archive is backed up from, and the files inside them."""

    def __init__(self, game_id: str, game_name: str, parent=None):
        super().__init__(parent)
        self._game_id = game_id
        self._game_name = game_name
        self._mgr = get_backup_manager()
        self._rows: dict = {}
        self.setWindowTitle(t("backups.archive_paths_title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._build()
        finalize_adaptive_dialog_size(self, min_w=620, min_h=480)
        center_dialog(self)
        self._reload()

    # ── mapping and unmapping ──────────────────────────────────

    def _apply_window_chrome(self):
        """Theme-coloured fill before the first paint.

        Without it Windows maps a white client area for one frame while Qt
        resolves the stylesheet — the flash Add/Edit Game already deals with
        this way. Read from the live palette, so it is right in either theme
        rather than right in the one it was written in.
        """
        bg = QColor(palette("bg"))
        fg = QColor(palette("text"))
        card = QColor(palette("bg_card"))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.Base, card)
        pal.setColor(QPalette.ColorRole.AlternateBase, bg)
        pal.setColor(QPalette.ColorRole.Button, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Text, fg)
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QDialog { background-color: %s; color: %s; }"
            "QScrollArea { background: transparent; }"
            % (palette("bg"), palette("text")))
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self)
        except Exception:
            logger.debug("could not match the title bar to the theme",
                         exc_info=True)

    def exec(self):
        """Map it settled, then run — nothing un-themed is ever on screen."""
        self._apply_window_chrome()
        self.setWindowOpacity(0.0)
        self.setUpdatesEnabled(False)
        try:
            QDialog.show(self)
            self.ensurePolished()
        finally:
            self.setUpdatesEnabled(True)
        self.repaint()
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        self.setWindowOpacity(1.0)
        self.raise_()
        self.activateWindow()
        return super().exec()

    def done(self, result):
        """Let the rows go on the way out.

        A row per folder, each with a file list under it, is not something to
        leave to whenever the collector next runs: this panel is opened from
        a list where every archive has one, so what it holds is held once per
        archive the user looks at.
        """
        self._release()
        super().done(result)
        self.deleteLater()

    def _release(self):
        for row in list(self._rows.values()):
            try:
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass
        self._rows = {}
        try:
            old = self._scroll.takeWidget()
            if old is not None:
                old.setParent(None)
                old.deleteLater()
        except RuntimeError:
            pass

    # ── layout ──────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(10)

        head = QLabel(t("backups.archive_paths_title"))
        head.setObjectName("dialog_heading")
        outer.addWidget(head)

        name = QLabel(self._game_name)
        name.setObjectName("form_section_lbl")
        outer.addWidget(name)

        desc = QLabel(t("backups.archive_paths_desc"))
        desc.setWordWrap(True)
        desc.setObjectName("dialog_desc")
        outer.addWidget(desc)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        outer.addWidget(self._scroll, 1)

        self._empty = QLabel(t("backups.archive_paths_empty"))
        self._empty.setObjectName("form_empty_lbl")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        add_row = QHBoxLayout()
        self._manual = QLineEdit()
        self._manual.setPlaceholderText(t("add_game.manual_path_placeholder"))
        self._manual.setMinimumWidth(scaled(160, self, min_px=120))
        self._manual.returnPressed.connect(self._add_typed)
        plus = QPushButton("+")
        _w = scaled(36, self, min_px=32)
        plus.setFixedWidth(_w)
        lock_min_size(plus, _w, scaled(28, self, min_px=26),
                      policy_h=QSizePolicy.Policy.Fixed,
                      policy_v=QSizePolicy.Policy.Fixed)
        plus.setToolTip(t("add_game.add_path_manually"))
        plus.clicked.connect(self._add_typed)
        browse = QPushButton(t("add_game.browse"))
        _b = scaled(80, self, min_px=72)
        browse.setFixedWidth(_b)
        lock_min_size(browse, _b, scaled(28, self, min_px=26),
                      policy_h=QSizePolicy.Policy.Fixed,
                      policy_v=QSizePolicy.Policy.Fixed)
        browse.clicked.connect(self._browse)
        add_row.addWidget(self._manual, 1)
        add_row.addWidget(plus)
        add_row.addWidget(browse)
        outer.addLayout(add_row)

        # The way back from a ✕ on a file — the same store, and the same
        # dialog, Add/Edit Game uses.
        ig = QVBoxLayout()
        ig.setSpacing(2)
        ig_lbl = QLabel(t("add_game.ignored_paths_section"))
        ig_lbl.setObjectName("form_field_lbl")
        ig.addWidget(ig_lbl)
        ig_row = QHBoxLayout()
        self._ignored_lbl = QLabel()
        self._ignored_lbl.setObjectName("form_muted_sm")
        manage = QPushButton(t("add_game.manage_ignored_paths_btn"))
        lock_min_size(manage, scaled(88, self, min_px=72),
                      scaled(28, self, min_px=26),
                      policy_h=QSizePolicy.Policy.Minimum,
                      policy_v=QSizePolicy.Policy.Fixed)
        manage.clicked.connect(self._open_ignored)
        ig_row.addWidget(self._ignored_lbl, 1)
        ig_row.addWidget(manage)
        ig.addLayout(ig_row)
        outer.addLayout(ig)

        close_row = QHBoxLayout()
        close_row.addStretch()
        done = QPushButton(t("common.close"))
        done.setObjectName("primary_btn")
        done.clicked.connect(self.accept)
        close_row.addWidget(done)
        outer.addLayout(close_row)

    # ── the list ────────────────────────────────────────────────────────────

    def _reload(self):
        """Rebuild every row from the index. Cheap: no file is read here."""
        recorded, skipped = self._mgr.orphan_sources_view(self._game_id)
        off = {p.casefold() for p in skipped}

        holder = QWidget()
        holder.setObjectName("transparent_bg")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._rows = {}
        for path in recorded:
            row = PathRow(path, game_id=self._game_id)
            row.set_checked(path.casefold() not in off)
            if not Path(path).exists():
                # Said out loud rather than left looking like an empty
                # folder: nothing is wrong with it, it is simply not here.
                row.setToolTip("%s — %s" % (path, t("backups.archive_source_gone")))
            row.checkbox.toggled.connect(
                lambda on, p=path: self._mgr.set_orphan_source_skipped(
                    self._game_id, p, not on))
            row.remove_requested.connect(self._forget)
            self._rows[path] = row
            lay.addWidget(row)
        self._empty.setParent(None)
        if not recorded:
            lay.addWidget(self._empty)
        lay.addStretch()
        old = self._scroll.takeWidget()
        self._scroll.setWidget(holder)
        if old is not None:
            old.deleteLater()
        self._refresh_ignored_count()

    def _forget(self, path: str):
        self._mgr.forget_orphan_source(self._game_id, path)
        self._reload()

    def _add(self, path: str):
        path = (path or "").strip().strip('"')
        if not path:
            return
        if self._mgr.add_orphan_sources(self._game_id, [path]):
            self._reload()
        self._manual.clear()

    def _add_typed(self):
        self._add(self._manual.text())

    def _browse(self):
        from ui.widgets.file_pickers import pick_folder
        start = next((p for p in self._rows if Path(p).is_dir()), "")
        chosen = pick_folder(self, t("add_game.select_save_folder"),
                             start_dir=start)
        # Taken as given. Comparing it against the files the archive holds
        # was tempting and wrong: these folders are played out of, so a save
        # deleted and a new one started is enough to make the right folder
        # look like the wrong one — and choosing it here IS the statement.
        self._add(chosen)

    # ── the way back from a bin ─────────────────────────────────────────────

    def _refresh_ignored_count(self):
        try:
            from core.config_manager import get_config
            n = len((get_config().get("auto_scan_deleted_paths", {}) or {}
                     ).get(self._game_id, []) or [])
        except Exception:
            n = 0
        self._ignored_lbl.setText(t("add_game.ignored_paths_count", count=n)
                                  if n else t("add_game.ignored_paths_none"))

    def _open_ignored(self):
        from ui.dialogs.add_game_dialog import IgnoredPathsDialog
        dlg = IgnoredPathsDialog(self._game_id, self._game_name, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for p in getattr(dlg, "restored_paths", []) or []:
            # A binned FILE belongs back in the list it was binned from, not
            # beside it as a folder of its own.
            owner = self._owning_row(p)
            if owner is not None:
                owner.refresh_files()
                owner.set_checked(True)
                continue
            self._mgr.add_orphan_sources(self._game_id, [p])
        self._reload()

    def _owning_row(self, path: str):
        """The row whose folder contains *path*, if any."""
        try:
            target = Path(path).resolve()
        except OSError:
            return None
        best, best_len = None, -1
        for p, row in self._rows.items():
            try:
                root = Path(p).resolve()
                target.relative_to(root)
            except (OSError, ValueError):
                continue
            if len(str(root)) > best_len:
                best, best_len = row, len(str(root))
        return best
