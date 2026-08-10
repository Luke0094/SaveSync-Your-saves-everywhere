"""
SaveSync - Add/Edit Game Dialog
- Image upload/remove with preview
- Save path list with size info, open folder, content preview
- Duplicate exe detection
- Paths are always saved (selected or not selected = saved to library)
"""
import os
import threading
import logging
import urllib.request
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QPoint
from PySide6.QtGui import QPixmap, QColor, QIcon, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFileDialog, QProgressBar, QFrame,
    QWidget, QScrollArea, QSizePolicy, QMessageBox,
    QSpinBox, QCheckBox, QComboBox, QTextEdit,
)

from i18n import t
from ui.helpers import (ElidedCheckBox, display_scale, load_pixmap_any,
                        open_in_file_manager, scaled_for_screen,
                        thumbnail_pixmap, viewer_pixmap)
from ui.modal_helpers import question_window_modal
from ui.styles.theme import palette
from core.library import GameEntry, get_library
from core.machine import get_machine_id
from core.constants import get_folder_name_for_save, get_install_folder_name

logger = logging.getLogger(__name__)

from ui.image_cache import (_ICON_CACHE_DIR, migrate_icon_cache,
                            _ensure_cache_compressed)
from ui.widgets.search_inputs import _GhostLineEdit, _SuggestPopup
from ui.widgets.path_row import PathRow
from ui.dialogs.detect_worker import DetectWorker


class IgnoredPathsDialog(QDialog):
    """Lets the user review and restore save paths that were permanently
    deleted (trash icon) during a post-game-exit save confirmation
    (ui/dialogs/auto_scan_dialog.py).

    Deletion is deliberately permanent (the scanner is told to never
    propose the path again) so a single accidental click used to have no
    way back. This dialog is that way back: it lists every path currently
    on the per-game deleted-paths list and lets the user restore the ones
    they pick, so the next save-confirmation scan considers them again.

    Simple deselection (checkbox left unchecked, but not deleted) is a
    separate, much softer action — it stays as an ordinary, visible entry
    in the game's own save-path list (GameEntry.excluded_save_paths), still
    re-includable any time, and deliberately does NOT appear here.
    """

    def __init__(self, game_id: str, game_name: str, parent=None,
                 extra_paths: Optional[list] = None,
                 session_only: bool = False):
        """*extra_paths*: session-only deletions not yet persisted (the
        confirmation panel deletes locally until Apply) — so a just-trashed
        path can be recovered immediately. Restored paths (both kinds) are
        exposed to the caller via ``self.restored_paths``.

        *session_only*: list ONLY those, leaving the persisted store out.
        The save-confirmation panel opens the dialog this way — there, the
        subject is the handful of paths just proposed, and entries ignored in
        earlier runs were noise between the user and the one row they wanted
        back. Those stay reachable from Settings → excluded paths.
        """
        super().__init__(parent)
        self.game_id = game_id
        self._extra_paths: list = list(extra_paths or [])
        self._session_only = session_only
        self.restored_paths: list = []
        self.setWindowTitle(t("add_game.ignored_paths_dialog_title"))
        self.setMinimumWidth(480)
        self._checkboxes: list[tuple[QCheckBox, str]] = []  # (cb, path)
        self._build()

    def _build(self):
        from core.config_manager import get_config
        layout = QVBoxLayout(self)

        desc = QLabel(t("add_game.ignored_paths_dialog_desc_session"
                        if self._session_only
                        else "add_game.ignored_paths_dialog_desc"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        layout.addWidget(desc)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 8, 0, 8)

        if self._session_only:
            deleted = list(self._extra_paths)
        else:
            config = get_config()
            deleted = list(config.get("auto_scan_deleted_paths", {}).get(self.game_id, []))
            for p in self._extra_paths:      # session-only deletions
                if p not in deleted:
                    deleted.append(p)

        if not deleted:
            empty_lbl = QLabel(t("add_game.session_ignored_none" if self._session_only
                                 else "add_game.ignored_paths_none"))
            empty_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:12px;")
            inner_layout.addWidget(empty_lbl)
        else:
            for path in deleted:
                row = self._make_row(path)
                inner_layout.addLayout(row)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(t("add_game.close"))
        close_btn.clicked.connect(self.reject)
        restore_btn = QPushButton(t("add_game.restore_selected_paths"))
        restore_btn.setObjectName("primary_btn")
        restore_btn.clicked.connect(self._restore_selected)
        restore_btn.setEnabled(bool(self._checkboxes))
        btn_row.addWidget(close_btn)
        btn_row.addWidget(restore_btn)
        layout.addLayout(btn_row)

    def _make_row(self, path: str) -> QHBoxLayout:
        row = QHBoxLayout()
        # Elided: the dialog is a fixed-width review list, and a path longer
        # than it would otherwise widen the dialog until the "Deleted" tag
        # fell off the right edge. Full path stays in the tooltip, and the
        # restore logic reads it from self._checkboxes, not from the label.
        cb = ElidedCheckBox()
        cb.setStyleSheet("QCheckBox { font-size: 11px; }")
        cb.setFullText(path)
        tag = QLabel(t("add_game.ignored_paths_deleted_label"))
        tag.setStyleSheet(
            f"color:{palette('text_hint')};font-size:10px;"
            f"border:1px solid {palette('border')};border-radius:3px;padding:1px 5px;"
        )
        row.addWidget(cb, 1)
        row.addWidget(tag)
        self._checkboxes.append((cb, path))
        return row

    def _restore_selected(self):
        """Remove checked paths from the deleted-paths list so the next
        scan considers them again."""
        to_restore = {p for cb, p in self._checkboxes if cb.isChecked()}

        if not to_restore:
            self.reject()
            return

        from core.config_manager import get_config
        config = get_config()

        # Touch the store only when there IS something stored to un-ignore:
        # a game still being added has no id (and nothing persisted), and
        # session-only restores would otherwise write a junk "" key and
        # trigger a pointless config write + Settings refresh.
        deleted_cfg = dict(config.get("auto_scan_deleted_paths", {}))
        stored = list(deleted_cfg.get(self.game_id, [])) if self.game_id else []
        if stored:
            remaining = [p for p in stored if p not in to_restore]
            if remaining != stored:
                deleted_cfg[self.game_id] = remaining
                config.set("auto_scan_deleted_paths", deleted_cfg)

        # Session-only entries aren't in the store — the caller reads this
        # to un-delete them locally and re-propose the rows.
        self.restored_paths = sorted(to_restore)
        logger.info(f"Restored {len(to_restore)} previously deleted path(s) for game {self.game_id}")
        self.accept()


from ui.dialogs.search_flow import SearchFlowMixin


# The executable picker moved to ui/widgets/file_pickers so the scan and
# manual-path dialogs get the identical shortcut-aware behaviour.
from ui.widgets.file_pickers import ExePickerDialog as _ExePickerDialog  # noqa: E402


class AddGameDialog(SearchFlowMixin, QDialog):
    game_added = Signal(object)   # GameEntry
    search_finished = Signal(object, object)  # list[GameInfo] | GameInfo | None, error
    _placeholder_signal = Signal(str)

    def __init__(self, name: str = "", exe_path: str = "",
                 entry: Optional[GameEntry] = None, parent=None):
        super().__init__(parent)
        self._detect_worker: Optional[DetectWorker] = None
        self._detection_in_progress = False
        self._cancel_event = threading.Event()
        self._save_paths: list[str] = []   # current path list
        # Rows the user trashed (✕) in this dialog session. Kept locally and
        # written to the ignored-paths store on Save, so Cancel discards them
        # and a delete-then-re-add nets out to nothing.
        self._removed_paths: list[str] = []
        self._image_path: Optional[str] = None
        self._image_url_cache: dict[str, str] = {}  # API image URL → local cached path
        self._image_path_to_url: dict[str, str] = {}  # reverse: local cached path → URL
        self._editing_entry: Optional[GameEntry] = entry
        self._created_icon_dirs: set[Path] = set()  # Track icon dirs created during this session
        self._session_initial_image_path: Optional[str] = None  # Image at dialog open (set on first search)
        self._session_image_captured: bool = False               # Guards the one-time capture above
        self._shortcut_name: Optional[str] = None  # Name extracted from shortcut filename
        self._shortcut_dir: Optional[str] = None  # Directory containing the shortcut
        # Pre-search form snapshot; refreshed by _web_search on every search.
        # Initialized here because _process_search_result (which reads them)
        # can also run from the candidate picker.
        self._original_name: str = ""
        self._original_desc: str = ""
        self._original_image_path: Optional[str] = None
        self._original_image_url: Optional[str] = None
        # Reviews are edited in their own window and only written back on
        # Save, so a cancelled dialog leaves the stored ones untouched.
        self._reviews: list[dict] = [dict(r) for r in
                                     (getattr(entry, "reviews", None) or [])]

        self.setWindowTitle(t("add_game.title") if not entry else t("library.edit"))
        self.setMinimumWidth(540)
        # WindowModal, NOT ApplicationModal: the dialog must block only the
        # main window. ApplicationModal froze input on EVERY app window —
        # including the in-game overlay, so an unknown-game notification
        # arriving while add/edit was open couldn't be clicked (see the
        # same rule in ui/modal_helpers.py).
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAcceptDrops(True)
        
        self._build()
        self._update_reviews_btn()
        # Live locale: placeholder "Unknown"/"Sconosciuto" changes width.
        from i18n import get_engine as _get_i18n
        _get_i18n().language_changed.connect(self._on_language_changed)

        if entry:
            # Block signals during init population to prevent auto-detect firing
            self._exe_edit.blockSignals(True)
            # _clean_tag (html.unescape) heals name/developer/description saved
            # with HTML entities by earlier versions ("N&amp;R", "you&#039;re"),
            # exactly as it already does for tags — so the dialog shows clean
            # text and re-saving persists it clean.
            self._name_edit.setText(self._clean_tag(entry.name))
            self._exe_edit.setText(entry.exe_path or "")
            self._exe_edit.blockSignals(False)
            # Load appid if present
            if entry.appid:
                self._appid_edit.setText(entry.appid)
            # Load backup interval (convert seconds to minutes)
            backup_interval_min = round(entry.backup_interval_sec / 60)
            self._backup_interval_spin.setValue(max(1, backup_interval_min))
            # Load auto backup enabled setting
            self._auto_backup_cb.setChecked(entry.auto_backup_enabled)
            for p in entry.save_paths:
                self._add_path(p, detected=False)
            # Compress any legacy uncompressed images in the cache folder
            # (must happen before icon_path check so stale refs are resolved)
            if entry.exe_path:
                _gf = get_install_folder_name(entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)
                _ensure_cache_compressed(_ICON_CACHE_DIR / _gf)
            # Resolve icon_path: if original was compressed, update to the .jpg
            _icon = entry.icon_path
            if _icon and not Path(_icon).exists():
                _jpg_alt = Path(_icon).with_suffix(".jpg")
                if _jpg_alt.exists():
                    _icon = str(_jpg_alt)
            if _icon and Path(_icon).exists():
                self._image_path = _icon
                self._update_image_preview(_icon)
            # Detect all available images for navigation (including current)
            all_imgs = []
            # Always detect from exe path - _find_all_game_images handles icon_path properly
            if entry.exe_path:
                from ui.widgets.game_items import _find_all_game_images
                all_imgs = _find_all_game_images(entry)
            # Remove duplicates while preserving order
            seen = set()
            all_imgs = [x for x in all_imgs if not (x in seen or seen.add(x))]
            if all_imgs:
                self._detected_images = all_imgs
                # Always show the saved icon_path as current (only if it exists)
                # Use the resolved _icon (may have been updated to .jpg above)
                if _icon and Path(_icon).exists():
                    self._image_path = _icon
                    self._current_image_idx = 0
                elif self._image_path and self._image_path in all_imgs:
                    self._current_image_idx = all_imgs.index(self._image_path)
                else:
                    self._current_image_idx = 0
                    self._image_path = all_imgs[0]
                if self._image_path:
                    self._update_image_preview(self._image_path)
                    self._update_nav_buttons()
        
            # Pre-fill description and category
            if entry.description:
                self._desc_edit.setPlainText(self._clean_tag(entry.description))
            if entry.category:
                for i in range(self._category_combo.count()):
                    if self._category_combo.itemData(i) == entry.category:
                        self._category_combo.setCurrentIndex(i)
                        break
            if entry.tags:
                # _split_tag_text heals tags saved with HTML entities
                # (&#039; …) AND comma-joined tags stored as one ("avventura
                # , azione") by earlier versions/web sources; the healed
                # list is what gets saved.
                self._tags = self._split_tag_text(entry.tags)
                # Render the tag chips immediately. This MUST NOT be nested in
                # the store_url branch below (the old bug) — editing a game
                # with tags but no store URL would then open with an empty
                # chip strip while add mode rendered fine, which read as the
                # add/edit tag-area inconsistency.
                self._rebuild_tag_chips()
            # A stored engine is the answer even when it came from detection:
            # re-detecting here would need the install folder, which may be
            # gone, and would overwrite a hand-typed value.
            _stored_engine = (getattr(entry, "engine", "") or "").strip()
            if _stored_engine:
                self._set_engine(_stored_engine, from_user=True)
            elif entry.exe_path:
                self._detect_engine_from_exe(entry.exe_path)
            # New metadata fields
            if hasattr(entry, 'developer') and entry.developer:
                self._developer_edit.setText(self._clean_tag(entry.developer))
            if hasattr(entry, 'release_year') and entry.release_year:
                self._year_edit.setText(entry.release_year)
            if hasattr(entry, 'store_url') and entry.store_url:
                # store_url may be a single URL or comma-separated list
                urls = [u.strip() for u in entry.store_url.split(',') if u.strip()]
                self._store_urls = urls
                self._rebuild_url_chips()
            # Seed source fingerprint so already-applied sources are not
            # re-offered without material news, and so a dead primary can be
            # soft-promoted without wiping saved fields.
            _saved_src = getattr(entry, 'info_source', '') or ''
            _applied: list[str] = []
            _base = (_saved_src or '').split('+')[0]
            if _base:
                _applied.append(_base)
            for _r in (getattr(entry, 'reviews', None) or []):
                if not isinstance(_r, dict):
                    continue
                _rs = (_r.get('source') or '').split('+')[0]
                if _rs and _rs not in ('user', 'web') and _rs not in _applied:
                    _applied.append(_rs)
            for _u in (getattr(self, '_store_urls', None) or []):
                _us = self._source_from_url(_u) if hasattr(self, '_source_from_url') else ''
                if _us and _us not in _applied:
                    _applied.append(_us)
            if _saved_src or _applied:
                self._enrichment_source_fingerprint = {
                    'source': _saved_src,
                    'content': (
                        (entry.description or '') + ' ' +
                        (getattr(entry, 'developer', '') or '') + ' ' +
                        (getattr(entry, 'release_year', '') or '')
                    ).strip(),
                    'applied': _applied,
                }
            self._add_btn.setText(t("common.save_changes"))
        else:
            if name:
                # A caller may hand us a generic exe stem ("nw", "game"…) as the
                # name; with a real exe path, walk up to the install-folder name
                # instead so the game is never proposed as "nw".
                if exe_path:
                    try:
                        from core.save_detector import derive_display_name, GENERIC_EXE_STEMS
                        from core.resolvers import is_launcher_url as _is_lurl
                        if name.strip().lower() in GENERIC_EXE_STEMS and not _is_lurl(exe_path):
                            _better = derive_display_name(exe_path)
                            if _better:
                                name = _better
                    except Exception:
                        pass
                self._name_edit.setText(name)
            if exe_path:
                from core.resolvers import is_launcher_url
                if is_launcher_url(exe_path):
                    # Store the full launcher URL in appid field
                    self._appid_edit.setText(exe_path)
                    if name:
                        self._shortcut_name = name
                    # Defer exe resolution to after the dialog is shown so the UI
                    # never freezes on drag-and-drop (the sync scan ran before exec()).
                    # _resolve_exe_from_url_async already runs in a background thread.
                    QTimer.singleShot(0, lambda _url=exe_path: self._resolve_exe_from_url_async(_url))
                    if name:
                        self._detect_btn.setEnabled(True)
                else:
                    self._exe_edit.setText(exe_path)
                    self._detect_btn.setEnabled(True)
                    self._auto_detect_image(exe_path)

        self._placeholder_signal.connect(self._exe_edit.setPlaceholderText)
        self.search_finished.connect(self._on_search_finished)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel(self.windowTitle())
        title.setObjectName("page_header")
        title.setStyleSheet("font-size:17px;")
        layout.addWidget(title)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # ── Image + name/exe row ──────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        # Cover image selector with left/right navigation
        img_col = QVBoxLayout()
        img_col.setSpacing(6)

        # Image preview with nav arrows overlay
        img_nav_row = QHBoxLayout()
        img_nav_row.setSpacing(2)

        _img_arrow_css = (
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;"
            f"font-weight:700;font-size:12px;padding:0;}}"
            f"QPushButton:hover{{background:{palette('accent')};color:{palette('accent_text')};}}"
            f"QPushButton:disabled{{color:{palette('text_muted')};border-color:{palette('border')};}}"
        )
        self._img_prev_btn = QPushButton("◀")
        self._img_prev_btn.setFixedSize(22, 56)
        self._img_prev_btn.setToolTip(t('add_game.previous_image'))
        self._img_prev_btn.setStyleSheet(_img_arrow_css)
        self._img_prev_btn.setEnabled(False)
        self._img_prev_btn.clicked.connect(self._prev_image)

        self._img_preview_container = QFrame()
        self._img_preview_container.setFixedSize(90, 56)
        self._img_preview_container.setStyleSheet(
            f"background:{palette('bg_elevated')};border:1px solid {palette('border_hover')};border-radius:6px;"
        )
        # Use a QStackedLayout so the trash icon overlays the preview
        from PySide6.QtWidgets import QStackedLayout
        img_preview_layout = QStackedLayout(self._img_preview_container)
        img_preview_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        img_preview_layout.setContentsMargins(0, 0, 0, 0)

        self._img_preview = QLabel()
        self._img_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_preview.setText("🎮")
        self._img_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self._img_preview.setToolTip(t('add_game.image_click_expand'))
        img_preview_layout.addWidget(self._img_preview)
        self._img_preview_container.setCursor(Qt.CursorShape.PointingHandCursor)
        self._img_preview_container.mousePressEvent = lambda e: self._show_image_modal()

        # Trash overlay — small icon at top-right of preview, cache images only
        self._img_trash_overlay = QPushButton("🗑", self._img_preview_container)
        self._img_trash_overlay.setFixedSize(20, 20)
        self._img_trash_overlay.move(68, 2)   # top-right corner of the 90×56 container
        self._img_trash_overlay.setCursor(Qt.CursorShape.PointingHandCursor)
        self._img_trash_overlay.setToolTip(t('add_game.remove_image_tooltip'))
        self._img_trash_overlay.setStyleSheet(
            "QPushButton{"
            "background:rgba(0,0,0,0.45);color:rgba(255,255,255,0.6);"
            "border:none;border-radius:4px;font-size:11px;padding:0;}"
            "QPushButton:hover{"
            "background:#c0392b;color:#fff;}"
        )
        self._img_trash_overlay.setVisible(False)
        self._img_trash_overlay.clicked.connect(self._remove_current_image_from_carousel)
        # Keep the preview on top of the stacked widget
        img_preview_layout.setCurrentIndex(1)

        self._img_next_btn = QPushButton("▶")
        self._img_next_btn.setFixedSize(22, 56)
        self._img_next_btn.setToolTip(t('add_game.next_image') if t('add_game.next_image') != 'add_game.next_image' else "Next image")
        self._img_next_btn.setStyleSheet(_img_arrow_css)
        self._img_next_btn.setEnabled(False)
        self._img_next_btn.clicked.connect(self._next_image)

        img_nav_row.addWidget(self._img_prev_btn)
        img_nav_row.addWidget(self._img_preview_container)
        img_nav_row.addWidget(self._img_next_btn)

        # Image counter label (e.g. "2 / 5")
        self._img_counter = QLabel()
        self._img_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_counter.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;")

        img_btn_row = QHBoxLayout()
        img_btn_row.setSpacing(4)
        self._img_add_btn = QPushButton("📷")
        self._img_add_btn.setFixedSize(40, 26)
        self._img_add_btn.setToolTip(t('add_game.set_custom_image'))
        self._img_add_btn.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};border:1px solid {palette('border')};border-radius:4px;font-size:14px;padding:0px;}}"
            f"QPushButton:hover{{background:{palette('accent')};}}"
        )
        self._img_add_btn.clicked.connect(self._browse_image)
        self._img_folder_btn = QPushButton("📂")
        self._img_folder_btn.setFixedSize(40, 26)
        self._img_folder_btn.setToolTip(t('add_game.open_cache_folder'))
        self._img_folder_btn.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};border:1px solid {palette('border')};border-radius:4px;font-size:14px;padding:0px;}}"
            f"QPushButton:hover{{background:{palette('accent')};}}"
        )
        self._img_folder_btn.clicked.connect(self._open_image_cache_folder)
        img_btn_row.addWidget(self._img_add_btn)
        img_btn_row.addWidget(self._img_folder_btn)
        img_col.addLayout(img_nav_row)
        img_col.addWidget(self._img_counter)
        img_col.addLayout(img_btn_row)
        top_row.addLayout(img_col)

        # Detected images list for navigation
        self._detected_images: list[str] = []
        self._current_image_idx: int = -1

        # Name + exe
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        name_lbl = QLabel(t("add_game.name"))
        name_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        name_row = QHBoxLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(t('add_game.name_placeholder'))
        self._name_edit.textChanged.connect(self._on_name_changed)
        self._web_search_btn = QPushButton("🌐")
        self._web_search_btn.setToolTip(t('add_game.web_search'))
        self._web_search_btn.setFixedWidth(48)
        self._web_search_btn.setEnabled(False)
        self._web_search_btn.clicked.connect(self._web_search)
        name_row.addWidget(self._name_edit, 1)
        name_row.addSpacing(2)
        name_row.addWidget(self._web_search_btn)
        right_col.addWidget(name_lbl)
        right_col.addLayout(name_row)

        # Web-search candidates (one distinct title or several) are now
        # reviewed through CandidatePreviewDialog, a popup opened by
        # _show_search_candidates() — see that method. No inline bar to
        # build here any more.

        # Engine sits beside the "Game Executable" label (not on the path
        # row): the path line stays path + Browse, full width.
        exe_col = QVBoxLayout()
        exe_col.setSpacing(3)
        self._exe_lbl = QLabel(t("add_game.exe_path"))
        self._exe_lbl.setStyleSheet(
            f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        self._engine_user_edited = False
        self._engine_fit_deferred = False
        self._engine_edit = QLineEdit()
        self._engine_edit.setPlaceholderText(t("common.unknown"))
        self._engine_edit.setToolTip(t("add_game.engine_tooltip"))
        self._engine_edit.setFixedHeight(22)
        self._engine_edit.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        # Pixel size (not point size) so metrics match the QSS font-size:11px.
        _eng_font = self._engine_edit.font()
        _eng_font.setPixelSize(11)
        _eng_font.setWeight(QFont.Weight.DemiBold)
        self._engine_edit.setFont(_eng_font)
        self._engine_edit.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._engine_edit.setStyleSheet(
            f"QLineEdit{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;"
            f"padding:1px 8px;font-size:11px;font-weight:600;}}"
            f"QLineEdit:focus{{border-color:{palette('accent')};}}"
        )
        self._engine_edit.textChanged.connect(self._fit_engine_width)
        self._engine_edit.textEdited.connect(self._on_engine_edited)
        self._fit_engine_width()
        exe_lbl_row = QHBoxLayout()
        exe_lbl_row.setContentsMargins(0, 0, 0, 0)
        exe_lbl_row.setSpacing(8)
        # Engine immediately to the RIGHT of the label — no stretch between.
        exe_lbl_row.addWidget(self._exe_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        exe_lbl_row.addWidget(self._engine_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        exe_lbl_row.addStretch(1)
        exe_col.addLayout(exe_lbl_row)
        exe_row = QHBoxLayout()
        exe_row.setSpacing(6)
        self._exe_edit = QLineEdit()
        self._exe_edit.setPlaceholderText(
            "C:\\Games\\game.exe" if os.name == 'nt' else "/opt/games/game")
        self._exe_edit.textChanged.connect(self._on_exe_changed)
        exe_row.addWidget(self._exe_edit, 1)
        browse_exe = QPushButton(t("add_game.browse"))
        browse_exe.setFixedWidth(80)
        browse_exe.clicked.connect(lambda: self._browse_exe())
        exe_row.addWidget(browse_exe)
        exe_col.addLayout(exe_row)
        right_col.addLayout(exe_col)
        
        # Game ID (appid from launcher URL)
        appid_lbl = QLabel(t("add_game.game_id"))
        appid_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        appid_row = QHBoxLayout()
        self._appid_edit = QLineEdit()
        self._appid_edit.setPlaceholderText(t('add_game.appid_placeholder'))
        self._appid_edit.setReadOnly(False)
        appid_row.addWidget(self._appid_edit, 1)
        right_col.addWidget(appid_lbl)
        right_col.addLayout(appid_row)
        
        top_row.addLayout(right_col, 1)
        layout.addLayout(top_row)

        # ── Developer · Year · Store URL ─────────────────────────────────────
        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)

        def _meta_col(label_key: str, placeholder_key: str):
            col = QVBoxLayout()
            col.setSpacing(3)
            lbl = QLabel(t(label_key))
            lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
            ed = QLineEdit()
            ed.setPlaceholderText(t(placeholder_key))
            ed.setStyleSheet(
                f"QLineEdit{{background:{palette('bg_input')};color:{palette('text')};"
                f"border:1px solid {palette('border')};border-radius:4px;padding:4px 6px;"
                f"font-size:12px;}}"
            )
            col.addWidget(lbl)
            col.addWidget(ed)
            return col, ed

        dev_col,   self._developer_edit  = _meta_col("add_game.developer", "add_game.developer_placeholder")
        year_col,  self._year_edit       = _meta_col("add_game.year",      "add_game.year_placeholder")

        # ── Store URLs as bubbles ─────────────────────────────────────────────
        store_col = QVBoxLayout()
        store_col.setSpacing(3)
        store_lbl = QLabel(t("add_game.store_url"))
        store_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        store_col.addWidget(store_lbl)

        # Bubble row (scrollable)
        self._url_chips_frame = QFrame()
        self._url_chips_frame.setFixedHeight(28)
        self._url_chips_frame.setStyleSheet(
            f"background:{palette('bg_input')};border:1px solid {palette('border')};"
            f"border-radius:4px;"
        )
        url_chips_outer = QHBoxLayout(self._url_chips_frame)
        url_chips_outer.setContentsMargins(4, 2, 4, 2)
        url_chips_outer.setSpacing(4)
        self._url_chips_layout = QHBoxLayout()
        self._url_chips_layout.setSpacing(4)
        self._url_chips_layout.setContentsMargins(0, 0, 0, 0)

        # Text input for adding new URL
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(t("add_game.store_url_placeholder"))
        self._url_input.setStyleSheet(
            "QLineEdit{background:transparent;border:none;padding:0 2px;font-size:11px;"
            f"color:{palette('text')};}}"
        )
        self._url_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._url_input.returnPressed.connect(self._add_url_from_input)

        # Fetch-from-link: pull metadata for a pasted store/database URL
        # (vndb.org / Steam via API, anything else via OpenGraph scrape)
        self._url_fetch_btn = QPushButton("🔗")
        self._url_fetch_btn.setObjectName("icon_btn")
        self._url_fetch_btn.setFixedSize(24, 24)
        self._url_fetch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._url_fetch_btn.setToolTip(t('add_game.fetch_from_url'))
        self._url_fetch_btn.clicked.connect(self._fetch_from_url_input)

        url_chips_outer.addLayout(self._url_chips_layout)
        url_chips_outer.addWidget(self._url_input, 1)
        url_chips_outer.addWidget(self._url_fetch_btn)
        store_col.addWidget(self._url_chips_frame)

        # Internal list of URL strings
        self._store_urls: list[str] = []

        meta_row.addLayout(dev_col,   2)
        meta_row.addLayout(year_col,  1)
        meta_row.addLayout(store_col, 2)
        layout.addLayout(meta_row)

        # ── Description + Category ────────────────────────────────────────────
        desc_cat_row = QHBoxLayout()
        desc_cat_row.setSpacing(12)

        # Description
        desc_col = QVBoxLayout()
        desc_col.setSpacing(4)
        desc_lbl = QLabel(t("library.description"))
        desc_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        self._desc_edit = QTextEdit()
        self._desc_edit.setPlaceholderText(t("library.description_placeholder"))
        self._desc_edit.setFixedHeight(60)
        self._desc_edit.setStyleSheet(
            f"QTextEdit{{background:{palette('bg_input')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:4px;padding:4px;font-size:12px;}}"
        )
        desc_col.addWidget(desc_lbl)
        desc_col.addWidget(self._desc_edit)
        desc_cat_row.addLayout(desc_col, 2)

        # Reviews and folder, in that order: the button sits level with the
        # description beside it, and the folder picker — which carries its own
        # label — reads better underneath than squeezed between the two.
        cat_col = QVBoxLayout()
        cat_col.setSpacing(4)

        # Reviews live in their own window: a rating, who wrote it and the
        # text of it need far more room than this form has, and there can be
        # any number of them. The button carries the count so the panel does
        # not have to be opened to find out there is nothing in it.
        self._reviews_btn = QPushButton()
        self._reviews_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reviews_btn.setToolTip(t("reviews.button_tooltip"))
        self._reviews_btn.setFixedHeight(28)
        self._reviews_btn.setStyleSheet(
            f"QPushButton{{font-size:11px;font-weight:600;padding:3px 10px;"
            f"background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;}}"
            f"QPushButton:hover{{background:{palette('bg_button')};"
            f"border-color:{palette('accent')};color:{palette('accent')};}}"
        )
        self._reviews_btn.clicked.connect(self._open_reviews)
        cat_col.addWidget(self._reviews_btn)

        cat_lbl = QLabel(t("library.category"))
        cat_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        self._category_combo = QComboBox()
        self._populate_category_combo()
        cat_col.addWidget(cat_lbl)
        cat_col.addWidget(self._category_combo)
        cat_col.addStretch()
        desc_cat_row.addLayout(cat_col, 1)

        layout.addLayout(desc_cat_row)

        # ── Tags ──────────────────────────────────────────────────────────────
        tag_lbl = QLabel(t("library.tags"))
        tag_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        layout.addWidget(tag_lbl)

        tag_widget = QWidget()
        tag_layout = QHBoxLayout(tag_widget)
        tag_layout.setContentsMargins(0, 0, 0, 0)
        tag_layout.setSpacing(4)

        # Filled triangles — ASCII "<"/">" (and thin ‹ ›) vanish on some fonts /
        # Windows DPI scales; same fix as the image nav arrows above.
        self._tag_left_btn = QPushButton("◀")
        self._tag_left_btn.setFixedWidth(24)
        self._tag_left_btn.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};border:1px solid {palette('border')};border-radius:4px;font-weight:bold;font-size:11px;}}"
            f"QPushButton:hover{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        self._tag_left_btn.setVisible(False)
        self._tag_left_btn.clicked.connect(self._scroll_tags_left)

        # The strip CHROME (background + border) lives on a wrapper frame,
        # NOT on the scroll area: the wrapper's inner margins push the
        # scroll viewport — where chips get clipped — INWARD, so a cut tag
        # ends 10 px BEFORE the strip edge and its arrow, on visible strip
        # background, never flush under the button.
        self._tag_strip_frame = QFrame()
        self._tag_strip_frame.setObjectName("tag_strip")
        self._tag_strip_frame.setStyleSheet(
            f"QFrame#tag_strip{{background:{palette('bg_input')};"
            f"border:1px solid {palette('border')};border-radius:4px;}}"
        )
        # FIXED strip: with the horizontal scrollbar AlwaysOff, a QScrollArea's
        # minimumSizeHint grows with the content, so overflowing/long chips
        # were widening the whole dialog (games with many long tags stretched
        # far beyond the others). A fixed viewport keeps the layout identical
        # for every game (~5 typical chips visible); the rest scrolls via the
        # arrow buttons.
        self._tag_strip_frame.setFixedHeight(32)
        self._tag_strip_frame.setFixedWidth(340)
        _strip_lay = QHBoxLayout(self._tag_strip_frame)
        # Clip gaps: chips are cut 10 px inside the LEFT edge and 44 px
        # inside the RIGHT edge — the cut lands on visible strip
        # background well BEFORE the > arrow, never flush under it.
        _strip_lay.setContentsMargins(10, 1, 44, 1)
        _strip_lay.setSpacing(0)

        self._tag_scroll = QScrollArea()
        self._tag_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}")
        self._tag_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tag_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # setWidgetResizable(True) is REQUIRED so the inner container is always
        # at least as wide as the viewport and chips are visible.
        self._tag_scroll.setWidgetResizable(True)
        _strip_lay.addWidget(self._tag_scroll)

        # CONTINUOUS strip: the chips fill the whole width and the one
        # crossing the strip edge is simply clipped — no whole-chip
        # pagination (it left large empty gaps at the end of each page).
        # The contract is on the ARROWS instead: whenever any part of a
        # chip is beyond the edge, the arrow stays clickable and each
        # click scrolls further, snapping onto the clipped chip's start
        # (left) or its closing ✕ (right) so the walk never strands a
        # half-hidden tag. Arrow state follows the real scroll range.
        self._tag_scroll.horizontalScrollBar().rangeChanged.connect(
            lambda _mn, _mx: self._update_tag_arrow_states())
        self._tag_scroll.horizontalScrollBar().valueChanged.connect(
            lambda _v: self._update_tag_arrow_states())

        self._tag_container = QFrame()
        self._tag_container.setStyleSheet("background:transparent;border:none;")
        self._tag_chips_layout = QHBoxLayout(self._tag_container)
        # Trailing margin 12: at full scroll the last chip ends 12 px
        # BEFORE the clip edge, so its ✕ AND its rounded border are fully
        # inside the strip, never resting on the cut line.
        self._tag_chips_layout.setContentsMargins(0, 3, 12, 3)
        self._tag_chips_layout.setSpacing(4)
        self._tag_chips_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._tag_scroll.setWidget(self._tag_container)

        self._tag_right_btn = QPushButton("▶")
        self._tag_right_btn.setFixedWidth(24)
        self._tag_right_btn.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};border:1px solid {palette('border')};border-radius:4px;font-weight:bold;font-size:11px;}}"
            f"QPushButton:hover{{background:{palette('accent')};color:{palette('accent_text')};}}"
        )
        self._tag_right_btn.setVisible(False)
        self._tag_right_btn.clicked.connect(self._scroll_tags_right)

        tag_layout.addWidget(self._tag_left_btn)
        tag_layout.addWidget(self._tag_strip_frame)
        tag_layout.addWidget(self._tag_right_btn)

        self._tag_input = _GhostLineEdit()
        self._tag_input.setPlaceholderText(t("library.tags_placeholder"))
        self._tag_input.setStyleSheet(
            f"QLineEdit{{border:1px solid {palette('border')};background:{palette('bg_input')};"
            f"border-radius:4px;color:{palette('text')};font-size:12px;padding:4px 8px;"
            f"min-width:120px;}}"
        )
        self._tag_input.returnPressed.connect(self._add_tag_from_input)
        # Registered-tag suggestions, backups-search style: typing opens a
        # popup list that refilters live; ↓/↑ move the highlight and mirror
        # it as a paint-only ghost (navigation NEVER adds anything by
        # itself). Confirming the highlight — Enter, a row click, or a
        # click on the painted ghost — COMPLETES THE TEXT with that tag
        # (no chip yet): the user can then type ", " and continue with the
        # next tag, or press Enter again to commit everything as chips.
        # Enter with no highlight commits the text as typed, so a new tag
        # that prefixes a registered one ("av" vs "Avventura") is always
        # enterable. The list starts with NO highlight (select_first=False).
        self._tag_suggest_matches: list[str] = []
        self._tag_suggest = _SuggestPopup(self)
        self._tag_suggest.item_activated.connect(self._on_tag_suggest_clicked)
        self._tag_input.textChanged.connect(self._update_tag_suggest)
        self._tag_input.ghost_accepted.connect(
            lambda: self._complete_tag_text(self._tag_suggest_current()))
        self._tag_input.installEventFilter(self)

        # The strip is fixed-width: any extra row width goes to the input.
        tag_row = QHBoxLayout()
        tag_row.addWidget(tag_widget)
        tag_row.addWidget(self._tag_input, 1)
        self._tags: list[str] = []

        layout.addLayout(tag_row)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep2)

        # ── Save paths ────────────────────────────────────────────────────────
        paths_header = QHBoxLayout()
        paths_lbl = QLabel(t("add_game.save_path"))
        paths_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        paths_header.addWidget(paths_lbl, 1)

        detect_row = QHBoxLayout()
        self._detect_btn = QPushButton(t("add_game.detect"))
        self._detect_btn.setFixedHeight(28)
        self._detect_btn.clicked.connect(self._start_detect)
        self._detect_progress = QProgressBar()
        self._detect_progress.setRange(0, 0)
        self._detect_progress.setFixedHeight(4)
        self._detect_progress.setVisible(False)

        # Extended scan button — re-runs detection with broad filesystem scan
        self._extended_scan_btn = QPushButton(t("auto_scan.extended_scan_btn"))
        self._extended_scan_btn.setFixedHeight(28)
        self._extended_scan_btn.setToolTip(t("auto_scan.general_scan_hint"))
        self._extended_scan_btn.setEnabled(False)  # enabled after normal scan completes
        self._extended_scan_btn.clicked.connect(self._start_extended_detect)
        self._extended_scan_btn.setStyleSheet(
            f"QPushButton{{border:1px solid {palette('border')};background:{palette('bg_elevated')};"
            f"color:{palette('text_muted')};border-radius:4px;font-size:11px;padding:0 8px;}}"
            f"QPushButton:enabled{{color:{palette('text')};border-color:{palette('accent')};}}"
            f"QPushButton:hover:enabled{{background:{palette('bg_card')};}}"
        )

        detect_row.addWidget(self._detect_btn)
        detect_row.addWidget(self._detect_progress, 1)
        detect_row.addWidget(self._extended_scan_btn)
        paths_header.addLayout(detect_row)
        layout.addLayout(paths_header)

        # Path rows container (scroll)
        self._paths_scroll = QScrollArea()
        self._paths_scroll.setWidgetResizable(True)
        self._paths_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._paths_scroll.setMinimumHeight(120)
        self._paths_scroll.setMaximumHeight(200)
        self._paths_container = QWidget()
        self._paths_container.setObjectName("transparent_bg")
        self._paths_layout = QVBoxLayout(self._paths_container)
        self._paths_layout.setContentsMargins(0, 0, 0, 0)
        self._paths_layout.setSpacing(4)
        # Section: "Your paths" (always above detected)
        self._manual_section_lbl = QLabel(t('add_game.your_save_folders'))
        self._manual_section_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:10px;font-weight:700;padding:2px 0;")
        self._paths_layout.addWidget(self._manual_section_lbl)
        self._paths_empty_lbl = QLabel(t('add_game.no_paths_added'))
        self._paths_empty_lbl.setStyleSheet(f"color:{palette('text_disabled')};font-size:11px;padding:8px;")
        self._paths_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._paths_layout.addWidget(self._paths_empty_lbl)
        # Separator and "Detected" section header — hidden until detection runs
        self._detected_sep = QFrame(); self._detected_sep.setFrameShape(QFrame.Shape.HLine)
        self._detected_sep.setVisible(False)
        self._detected_section_lbl = QLabel(t('add_game.auto_detected'))
        self._detected_section_lbl.setStyleSheet(f"color:{palette('text_faint')};font-size:10px;font-weight:700;padding:2px 0;")
        self._detected_section_lbl.setVisible(False)
        self._paths_layout.addWidget(self._detected_sep)
        self._paths_layout.addWidget(self._detected_section_lbl)
        self._paths_layout.addStretch()
        self._paths_scroll.setWidget(self._paths_container)
        layout.addWidget(self._paths_scroll)

        # Manual add row
        manual_row = QHBoxLayout()
        self._manual_path = QLineEdit()
        self._manual_path.setPlaceholderText(t('add_game.manual_path_placeholder'))
        add_path_btn = QPushButton("+")
        add_path_btn.setFixedWidth(36)
        add_path_btn.setToolTip(t('add_game.add_path_manually'))
        add_path_btn.clicked.connect(self._add_manual_path)
        browse_save = QPushButton(t("add_game.browse"))
        browse_save.setFixedWidth(80)
        browse_save.clicked.connect(self._browse_save)
        manual_row.addWidget(self._manual_path, 1)
        manual_row.addWidget(add_path_btn)
        manual_row.addWidget(browse_save)
        layout.addLayout(manual_row)

        # ── Ignored paths — right below the manual path row ──────────────────
        # Paths deleted here or in a post-game-exit save confirmation are
        # excluded from future scans permanently — this is the way back from
        # an accidental delete.
        # Present when ADDING a game too: a ✕ there is just as permanent
        # (it's recorded against the entry's id the moment it's created), so
        # it needs the same way back. Edit mode shows the box always — it can
        # hold entries from earlier sessions; add mode has nothing stored yet,
        # so the box appears as soon as the first row is deleted.
        self._ignored_paths_box = QWidget()
        _ig_vbox = QVBoxLayout(self._ignored_paths_box)
        _ig_vbox.setContentsMargins(0, 0, 0, 0)
        _ig_vbox.setSpacing(2)

        ignored_lbl = QLabel(t("add_game.ignored_paths_section"))
        ignored_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        _ig_vbox.addWidget(ignored_lbl)

        ignored_row = QHBoxLayout()
        self._ignored_paths_count_lbl = QLabel()
        self._ignored_paths_count_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        manage_ignored_btn = QPushButton(t("add_game.manage_ignored_paths_btn"))
        manage_ignored_btn.clicked.connect(self._open_ignored_paths_dialog)
        ignored_row.addWidget(self._ignored_paths_count_lbl, 1)
        ignored_row.addWidget(manage_ignored_btn)
        _ig_vbox.addLayout(ignored_row)

        layout.addWidget(self._ignored_paths_box)
        self._ignored_paths_box.setVisible(bool(self._editing_entry))
        self._refresh_ignored_paths_count()

        self._status_lbl = QLabel()
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")
        self._search_progress = QProgressBar()
        self._search_progress.setRange(0, 0)
        self._search_progress.setFixedHeight(4)
        self._search_progress.setFixedWidth(120)
        self._search_progress.setVisible(False)
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_lbl, 1)
        status_row.addWidget(self._search_progress)
        layout.addLayout(status_row)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep3)

        # ── Backup settings ───────────────────────────────────────────────────
        backup_header = QHBoxLayout()
        backup_lbl = QLabel(t("add_game.backup_settings"))
        backup_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        backup_header.addWidget(backup_lbl)
        layout.addLayout(backup_header)

        # Auto backup switch
        switch_row = QHBoxLayout()
        self._auto_backup_cb = QCheckBox(t("add_game.auto_backup_enabled"))
        self._auto_backup_cb.setChecked(True)  # Default enabled
        self._auto_backup_cb.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
        switch_row.addWidget(self._auto_backup_cb)
        switch_row.addStretch()
        layout.addLayout(switch_row)

        backup_row = QHBoxLayout()
        self._backup_interval_spin = QSpinBox()
        self._backup_interval_spin.setRange(1, 120)
        self._backup_interval_spin.setSuffix(" min")
        self._backup_interval_spin.setValue(10)  # Default 10 minutes
        self._backup_interval_spin.setToolTip(t("add_game.backup_interval_tooltip"))
        backup_interval_lbl = QLabel(t("add_game.backup_interval_tooltip"))
        backup_interval_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        backup_row.addWidget(backup_interval_lbl)
        backup_row.addWidget(self._backup_interval_spin)
        backup_row.addStretch()
        layout.addLayout(backup_row)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t("add_game.cancel"))
        cancel_btn.clicked.connect(self.reject)
        self._add_btn = QPushButton(t("add_game.add"))
        self._add_btn.setObjectName("primary_btn")
        self._add_btn.clicked.connect(self._add_game)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._add_btn)
        layout.addLayout(btn_row)

    def _pending_removed_paths(self) -> list[str]:
        """This session's trashed rows that are still gone from the list —
        a path deleted and then re-added (manually, re-detected or restored)
        must NOT be ignored."""
        return [p for p in self._removed_paths if p not in self._save_paths]

    def _refresh_ignored_paths_count(self):
        """Update the 'N paths excluded from future scans' label."""
        if not hasattr(self, '_ignored_paths_count_lbl'):
            return
        from core.config_manager import get_config
        # No id before the entry exists — a new game can only have this
        # session's deletions, never a stored list.
        gid = self._editing_entry.id if self._editing_entry else ""
        stored = get_config().get("auto_scan_deleted_paths", {}).get(gid, []) if gid else []
        # Session deletions count too: they're already gone from the list, so
        # the label would otherwise under-report until Save.
        n = len(set(stored) | set(self._pending_removed_paths()))
        if n:
            self._ignored_paths_count_lbl.setText(t("add_game.ignored_paths_count", count=n))
        else:
            self._ignored_paths_count_lbl.setText(t("add_game.ignored_paths_none"))
        if not self._editing_entry and hasattr(self, '_ignored_paths_box'):
            self._ignored_paths_box.setVisible(n > 0)

    def _open_ignored_paths_dialog(self):
        gid = self._editing_entry.id if self._editing_entry else ""
        gname = (self._editing_entry.name if self._editing_entry
                 else self._name_edit.text().strip())
        dlg = IgnoredPathsDialog(gid, gname, parent=self,
                                 extra_paths=self._pending_removed_paths())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Restoring means "I want this path back" — so besides removing
            # it from the ignored store, insert it STRAIGHT into the
            # dialog's save-path list as a user path (saved with the entry
            # on confirm), instead of waiting for a future scan to
            # re-propose it.
            for p in getattr(dlg, "restored_paths", []) or []:
                # Undo a not-yet-persisted deletion from this same session.
                if p in self._removed_paths:
                    self._removed_paths.remove(p)
                self._add_path(p)
            self._refresh_ignored_paths_count()

    # ── Image management ──────────────────────────────────────────────────────

    def _show_image_modal(self):
        """Full-size image viewer modal.

        Features:
        - Semi-transparent dark backdrop; clicking outside the rendered image
          (not merely outside the label) closes the viewer
        - ✕ button top-right with visible label
        - ⛶ fullscreen toggle anchored to the image's top-right corner; in
          fullscreen every control fades out while the mouse is idle so the
          image can be viewed without distractions (move the mouse to bring
          the controls back, Esc leaves fullscreen first, then closes)
        - Prev / Next navigation buttons and ←/→ keys
        - Thumbnail strip at the bottom (between the nav arrows) with active highlight
        """
        if not self._detected_images:
            return
        from PySide6.QtWidgets import (
            QDialog, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QScrollArea
        )
        from PySide6.QtGui import QPixmap
        from PySide6.QtCore import Qt, QEvent, QObject, QRect

        dlg = QDialog(self)
        dlg.setWindowTitle(t('add_game.image_viewer_title'))
        dlg.setModal(True)
        dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        dlg.setStyleSheet("QDialog{background:transparent;}")

        outer = QWidget(dlg)
        outer.setStyleSheet("background:rgba(0,0,0,0.85);border-radius:12px;")
        outer_layout = QVBoxLayout(dlg)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

        dlg.resize(self.width(), self.height())
        dlg.move(self.mapToGlobal(self.rect().topLeft()))

        v = QVBoxLayout(outer)
        v.setContentsMargins(24, 14, 24, 16)
        v.setSpacing(8)

        _fs = {"on": False}   # fullscreen state (closure-mutable)

        # ── Main image ────────────────────────────────────────────────────────
        # Only the image lives in the layout; every control (close button,
        # nav/thumbnail bar, ⛶) FLOATS on top of it. Hiding the controls in
        # fullscreen therefore never re-layouts, so the image doesn't resize.
        self._modal_idx = self._current_image_idx
        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_lbl.setMinimumSize(400, 240)
        img_lbl.setObjectName("transparent_bg")
        v.addWidget(img_lbl, 1)

        # ── Top bar: X button — floats over the top edge of the viewer ───────
        top_bar = QWidget(outer)
        top_bar.setObjectName("transparent_bg")
        top_row = QHBoxLayout(top_bar)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addStretch()
        close_btn = QPushButton(t('add_game.close_modal'))
        close_btn.setFixedHeight(28)
        close_btn.setStyleSheet(
            "QPushButton{background:rgba(0,0,0,0.55);color:#fff;border:none;"
            "border-radius:5px;font-size:12px;font-weight:700;padding:0 14px;}"
            "QPushButton:hover{background:#c0392b;color:#fff;}"
        )
        close_btn.clicked.connect(dlg.accept)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        top_row.addWidget(close_btn)

        # ⛶ fullscreen toggle — floats over the image's top-right corner
        fs_btn = QPushButton("⛶", img_lbl)
        fs_btn.setFixedSize(32, 32)
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setToolTip(t('add_game.fullscreen_enter'))
        fs_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        fs_btn.setStyleSheet(
            "QPushButton{background:rgba(0,0,0,0.55);color:rgba(255,255,255,0.85);"
            "border:1px solid rgba(255,255,255,0.25);border-radius:6px;"
            "font-size:16px;padding:0;}"
            "QPushButton:hover{background:rgba(255,255,255,0.2);color:#fff;}"
        )

        def _pixmap_rect_in_label() -> QRect:
            """Rect of the rendered pixmap inside img_lbl (label coords)."""
            pm = img_lbl.pixmap()
            if pm is None or pm.isNull():
                return QRect(0, 0, img_lbl.width(), img_lbl.height())
            dpr = pm.devicePixelRatio() or 1.0
            pw, ph = int(pm.width() / dpr), int(pm.height() / dpr)
            return QRect((img_lbl.width() - pw) // 2,
                         (img_lbl.height() - ph) // 2, pw, ph)

        def _position_fs_btn():
            r = _pixmap_rect_in_label()
            fs_btn.move(r.right() - fs_btn.width() - 8, r.top() + 8)
            fs_btn.raise_()

        # thumbnail strip + nav (built after img_lbl so _update_thumbs can reference them)
        thumb_labels: list[QLabel] = []

        # Source pixmap for the current image, kept unscaled so the view can
        # be re-fitted to whatever space it actually gets.
        _src: dict = {"px": None, "fitted": None}

        def _fit_to_label():
            """Scale the source to the LABEL's real size.

            Deriving the size from dlg.width()/height() was the bug: on open,
            and on entering or leaving fullscreen, the window manager grants
            the new geometry asynchronously, so those numbers were still the
            previous ones when the image was scaled — the picture came up at
            the wrong size and only snapped into place if something later
            happened to reload it. The label's own resize event IS the moment
            the available space is known, so that is what drives this.
            """
            px = _src["px"]
            if px is None or px.isNull():
                return
            avail = img_lbl.size()
            if avail.width() < 40 or avail.height() < 40:
                return
            # Scaled to REAL pixels, not to Qt's. On a display that magnifies
            # everything, fitting to Qt's coordinates throws away the detail
            # that magnification then has to invent back — which showed as a
            # soft, blocky picture in the window and not at full screen,
            # where almost nothing had been thrown away to begin with.
            dpr = display_scale()
            if _src["fitted"] == (avail.width(), avail.height(), dpr):
                return                      # already fitted to this size
            _src["fitted"] = (avail.width(), avail.height(), dpr)
            shown = px.scaled(int(round(avail.width() * dpr)),
                              int(round(avail.height() * dpr)),
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            shown.setDevicePixelRatio(dpr)
            img_lbl.setPixmap(shown)
            _position_fs_btn()

        def _load_modal_img():
            if 0 <= self._modal_idx < len(self._detected_images):
                # Through the cache: this runs on every arrow AND on every
                # fullscreen toggle, and decoding a large picture again is
                # the whole of the pause that used to come with both.
                px = viewer_pixmap(self._detected_images[self._modal_idx])
                _src["px"] = None if px.isNull() else px
                _src["fitted"] = None
                _fit_to_label()
            _update_thumbs()
            prev_btn.setEnabled(self._modal_idx > 0)
            next_btn.setEnabled(self._modal_idx < len(self._detected_images) - 1)
            _position_fs_btn()

        def _update_thumbs():
            for i, lbl in enumerate(thumb_labels):
                active = (i == self._modal_idx)
                lbl.setStyleSheet(
                    f"border:2px solid {'#fff' if active else 'rgba(255,255,255,0.25)'};"
                    f"border-radius:3px;background:{'rgba(255,255,255,0.15)' if active else 'transparent'};"
                    f"opacity:{'1' if active else '0.6'};"
                )

        # ── Nav row with thumbnail strip in the middle ────────────────────────
        # Floats over the bottom edge of the image (not a layout row) with a
        # translucent backdrop so hiding it never re-layouts the image.
        nav_bar = QWidget(outer)
        nav_bar.setObjectName("modal_nav_bar")
        nav_bar.setStyleSheet(
            "QWidget#modal_nav_bar{background:rgba(0,0,0,0.45);border-radius:8px;}"
        )
        nav_row = QHBoxLayout(nav_bar)
        nav_row.setContentsMargins(8, 4, 8, 4)
        nav_row.setSpacing(8)

        _btn_style = (
            f"QPushButton{{background:{palette('accent')};color:{palette('accent_text')};"
            "border:none;border-radius:5px;padding:6px 14px;font-size:13px;font-weight:700;}"
            f"QPushButton:hover{{background:{palette('accent_hover')};}}"
            "QPushButton:disabled{background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.25);}"
        )
        prev_btn = QPushButton(f"◀  {t('add_game.previous_image')}")
        prev_btn.setFixedHeight(36)
        prev_btn.setStyleSheet(_btn_style)
        prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        next_btn = QPushButton(f"{t('add_game.next_image')}  ▶")
        next_btn.setFixedHeight(36)
        next_btn.setStyleSheet(_btn_style)
        next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Thumbnail strip
        thumb_scroll = QScrollArea()
        thumb_scroll.setFixedHeight(52)
        thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        thumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        thumb_scroll.setFrameShape(thumb_scroll.Shape.NoFrame)
        thumb_scroll.setStyleSheet("background:transparent;border:none;")
        thumb_container = QWidget()
        thumb_container.setObjectName("transparent_bg")
        thumb_row = QHBoxLayout(thumb_container)
        thumb_row.setContentsMargins(4, 2, 4, 2)
        thumb_row.setSpacing(6)
        thumb_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        for i, img_path in enumerate(self._detected_images):
            lbl = QLabel()
            lbl.setFixedSize(60, 40)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("border:2px solid rgba(255,255,255,0.25);border-radius:3px;")
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            # Capture index for click
            def _make_click(idx):
                def _click(event):
                    self._modal_idx = idx
                    _load_modal_img()
                return _click
            lbl.mousePressEvent = _make_click(i)
            thumb_labels.append(lbl)
            thumb_row.addWidget(lbl)

        def _fill_thumbs(i: int = 0):
            """Draw the strip one picture at a time, after the viewer is up.

            Decoding them all before showing anything is what kept the viewer
            waiting on a folder of large images — and the picture the player
            actually asked for was behind all of it. The frames are laid out
            immediately, at their real size so nothing moves, and each fills
            in as it is read. They are cached, so this is only ever paid once.
            """
            if i >= len(thumb_labels):
                return
            try:
                px_t = thumbnail_pixmap(self._detected_images[i], 60, 40)
                if not px_t.isNull():
                    thumb_labels[i].setPixmap(px_t)
            except Exception as e:
                logger.debug(f"Thumbnail {i} failed: {e}")
            QTimer.singleShot(0, lambda: _fill_thumbs(i + 1))

        QTimer.singleShot(0, _fill_thumbs)

        thumb_scroll.setWidget(thumb_container)
        thumb_container.adjustSize()
        thumb_scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        # QScrollArea defaults to StrongFocus and uses ←/→ itself to scroll
        # its content once focused (e.g. after clicking a thumbnail) — that
        # silently swallows the arrow keys before they ever reach the
        # dialog's own keyPressEvent below. This viewer has no need for
        # keyboard focus on the strip itself, so keyboard nav should always
        # go to the image, never to scrolling the thumbnails.
        thumb_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        def _go_prev():
            if self._modal_idx > 0:
                self._modal_idx -= 1
                _load_modal_img()

        def _go_next():
            if self._modal_idx < len(self._detected_images) - 1:
                self._modal_idx += 1
                _load_modal_img()

        prev_btn.clicked.connect(_go_prev)
        next_btn.clicked.connect(_go_next)

        nav_row.addWidget(prev_btn)
        nav_row.addWidget(thumb_scroll, 1)
        nav_row.addWidget(next_btn)

        # ── Floating-chrome placement (top bar + nav bar over the image) ─────
        def _layout_chrome():
            m = 14
            top_bar.setGeometry(m, m, max(outer.width() - 2 * m, 50),
                                close_btn.height())
            nh = nav_bar.sizeHint().height()
            nav_bar.setGeometry(m, max(outer.height() - nh - m, 0),
                                max(outer.width() - 2 * m, 50), nh)
            top_bar.raise_()
            nav_bar.raise_()
            fs_btn.raise_()

        # ── Fullscreen: toggle + idle auto-hide of every control ─────────────
        hide_timer = QTimer(dlg)
        hide_timer.setSingleShot(True)
        hide_timer.setInterval(1500)

        def _set_chrome_visible(visible: bool):
            # Controls are overlays on the image: toggling them never
            # re-layouts, so the image keeps its exact size and position.
            top_bar.setVisible(visible)
            nav_bar.setVisible(visible)
            fs_btn.setVisible(visible)
            if visible:
                dlg.unsetCursor()
                _layout_chrome()
            else:
                dlg.setCursor(Qt.CursorShape.BlankCursor)

        def _on_idle():
            if _fs["on"]:
                _set_chrome_visible(False)

        hide_timer.timeout.connect(_on_idle)

        def _wake_chrome():
            """Mouse activity: show controls; re-arm the idle timer in fullscreen."""
            if _fs["on"]:
                if not nav_bar.isVisible():
                    _set_chrome_visible(True)
                hide_timer.start()

        def _toggle_fs():
            _fs["on"] = not _fs["on"]
            if _fs["on"]:
                fs_btn.setText("🗗")
                fs_btn.setToolTip(t('add_game.fullscreen_exit'))
                outer.setStyleSheet("background:rgba(0,0,0,0.97);border-radius:0px;")
                dlg.showFullScreen()
                hide_timer.start()
            else:
                hide_timer.stop()
                fs_btn.setText("⛶")
                fs_btn.setToolTip(t('add_game.fullscreen_enter'))
                outer.setStyleSheet("background:rgba(0,0,0,0.85);border-radius:12px;")
                _set_chrome_visible(True)
                dlg.showNormal()
                dlg.resize(self.width(), self.height())
                dlg.move(self.mapToGlobal(self.rect().topLeft()))
            QTimer.singleShot(0, _load_modal_img)

        fs_btn.clicked.connect(_toggle_fs)

        # Wake the controls on any mouse move over the viewer (children too)
        class _MoveWatch(QObject):
            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Type.MouseMove:
                    _wake_chrome()
                elif ev.type() == QEvent.Type.Resize:
                    if obj is img_lbl:
                        # Re-fit HERE, not after showFullScreen()/showNormal():
                        # this is the first moment the granted geometry is real.
                        _fit_to_label()
                        _position_fs_btn()
                    elif obj is outer:
                        _layout_chrome()
                return False

        _watch = _MoveWatch(dlg)
        for _w in (dlg, outer, img_lbl):
            _w.setMouseTracking(True)
            _w.installEventFilter(_watch)

        # Esc leaves fullscreen first (then the default reject closes);
        # ←/→ navigate between images.
        _orig_key = dlg.keyPressEvent

        def _key(ev):
            if ev.key() == Qt.Key.Key_Escape and _fs["on"]:
                _toggle_fs()
                return
            if ev.key() == Qt.Key.Key_Left:
                _go_prev()
                return
            if ev.key() == Qt.Key.Key_Right:
                _go_next()
                return
            _orig_key(ev)

        dlg.keyPressEvent = _key

        _load_modal_img()
        _layout_chrome()
        dlg.setFocus(Qt.FocusReason.OtherFocusReason)

        # Click on the dark area OUTSIDE the rendered image closes the viewer
        # (windowed mode only — QLabel ignores presses, so clicks anywhere on
        # the label propagate here; hit-test the actual pixmap, not the whole
        # stretched label, or the empty bands beside the image feel dead).
        def _backdrop_press(event):
            if _fs["on"]:
                _wake_chrome()
                return
            pt = event.position().toPoint()
            px_rect = _pixmap_rect_in_label().translated(img_lbl.geometry().topLeft())
            if not px_rect.contains(pt):
                dlg.accept()
        outer.mousePressEvent = _backdrop_press

        dlg.exec()

    def _open_image_cache_folder(self):
        """Open the icon cache folder for this game in the system file manager."""
        try:
            # Build the game-specific cache path
            from core.constants import get_install_folder_name
            if self._editing_entry:
                game_folder = get_install_folder_name(
                    self._editing_entry.exe_path or "",
                    self._editing_entry.name,
                    self._editing_entry.id,
                    self._editing_entry.computed_folder_name,
                )
            else:
                exe = self._exe_edit.text().strip() if hasattr(self, '_exe_edit') else ""
                name = self._name_edit.text().strip() if hasattr(self, '_name_edit') else ""
                game_folder = get_install_folder_name(exe, name, "", None)
            cache_path = _ICON_CACHE_DIR / game_folder
            # Track a folder we are about to create (e.g. "Unknown" while the
            # name field is still empty) so closing without saving removes it.
            if not cache_path.exists():
                self._created_icon_dirs.add(cache_path)
            cache_path.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(cache_path)
        except Exception as e:
            logger.debug(f"Failed to open cache folder: {e}")

    def _browse_image(self):
        from ui.widgets.file_pickers import pick_file
        path = pick_file(self, t('add_game.select_cover_image'),
                         "Images (*.png *.jpg *.jpeg *.webp *.bmp *.avif)")
        if path:
            # Use original path directly (no copy)
            self._image_path = path
            # Add to existing detected images (don't replace)
            if path not in self._detected_images:
                self._detected_images.append(path)
            self._current_image_idx = self._detected_images.index(path)
            self._update_image_preview(path)
            self._update_nav_buttons()
    
    def _prev_image(self):
        if not self._detected_images or self._current_image_idx <= 0:
            return
        self._current_image_idx -= 1
        path = self._detected_images[self._current_image_idx]
        self._image_path = path
        self._update_image_preview(path)
        self._update_nav_buttons()

    def _next_image(self):
        if not self._detected_images or self._current_image_idx >= len(self._detected_images) - 1:
            return
        self._current_image_idx += 1
        path = self._detected_images[self._current_image_idx]
        self._image_path = path
        self._update_image_preview(path)
        self._update_nav_buttons()

    def _is_downloaded_image(self, path: str) -> bool:
        """Return True if *path* is a cached/downloaded image (not from install dir)."""
        if not path:
            return False
        return str(path).startswith(str(_ICON_CACHE_DIR))

    def _remove_current_image_from_carousel(self):
        """Remove current image from carousel.

        Cache/downloaded images are physically deleted.
        Custom images set via Browse are only removed from the list — the
        file on disk is never deleted because it belongs to the user.
        """
        if not self._image_path:
            return
        is_cache = self._is_downloaded_image(self._image_path)
        if is_cache:
            try:
                Path(self._image_path).unlink(missing_ok=True)
            except Exception:
                pass
        # Always remove from carousel list
        if self._image_path in self._detected_images:
            self._detected_images.remove(self._image_path)
        n = len(self._detected_images)
        self._current_image_idx = max(0, min(self._current_image_idx, n - 1)) if n else -1
        if n:
            new_path = self._detected_images[self._current_image_idx]
            self._image_path = new_path
            self._update_image_preview(new_path)
        else:
            self._image_path = None
            self._img_preview.setPixmap(QPixmap())
            self._img_preview.setText("🎮")
        self._update_nav_buttons()


    def _update_nav_buttons(self):
        """Update left/right nav buttons, counter, and trash overlay based on current state."""
        n = len(self._detected_images)
        idx = self._current_image_idx
        self._img_prev_btn.setEnabled(idx > 0)
        self._img_next_btn.setEnabled(idx < n - 1)
        if n > 1:
            self._img_counter.setText(f"{idx + 1} / {n}")
        elif n == 1:
            self._img_counter.setText("1 / 1")
        # Show trash overlay only when the current image is a downloaded/cached one
        current_path = self._detected_images[idx] if 0 <= idx < n else ""
        show_trash = self._is_downloaded_image(current_path)
        if hasattr(self, '_img_trash_overlay'):
            self._img_trash_overlay.setVisible(show_trash)
        else:
            self._img_counter.setText("")

    def _update_image_preview(self, path: str):
        try:
            px = viewer_pixmap(path)
            if not px.isNull():
                px = scaled_for_screen(px, 90, 56)
                self._img_preview.setPixmap(px)
                self._img_preview.setText("")
                return
        except Exception:
            pass
        self._img_preview.setText("🎮")

    def _ensure_cached_icon(
        self, image_path: str | None,
        exe_path: str, game_name: str, game_id: str,
        computed_folder_name: str | None,
    ) -> str | None:
        """Return the icon path to persist in the library.

        Images that already live inside the icon cache are returned as-is.
        External images (browsed from the filesystem or detected near the
        exe) are **not** copied or modified — the original path is stored
        directly so the user's files are never touched.
        """
        if not image_path:
            return None
        if not Path(image_path).exists():
            return None
        return image_path

    # ── Browse ────────────────────────────────────────────────────────────────

    def _resolve_launcher_url(self, url_or_path: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a launcher URL to executable path and appid.
        
        Returns:
            (resolved_exe_path, appid) or (None, None) if not a valid URL
        """
        url_or_path = url_or_path.strip()
        if not url_or_path:
            return None, None
        
        try:
            from core.resolvers import parse_launcher_url, get_appid_from_url, is_launcher_url
            if not is_launcher_url(url_or_path):
                return None, None
            
            parsed = parse_launcher_url(url_or_path)
            if not parsed:
                return None, None
            
            appid = get_appid_from_url(url_or_path)
            user_game_name = self._name_edit.text().strip()
            
            game_name = self._shortcut_name if self._shortcut_name else user_game_name
            game_name = game_name if game_name else None
            
            from core.resolvers import find_executable_by_fuzzy_name, _get_suggested_exe_search_paths
            import os
            import time as _time
            # This path is SYNCHRONOUS on the GUI thread — bound it hard:
            # better a partial answer after 20s than a frozen window.
            _deadline = _time.monotonic() + 20

            # Build paths the same way as async version
            paths = _get_suggested_exe_search_paths()
            if self._shortcut_dir:
                sd = Path(self._shortcut_dir)
                if sd.exists() and sd not in paths:
                    paths.insert(0, sd)
            if os.name == 'nt':
                added = {str(p) for p in paths}
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    drive = f"{letter}:/"
                    if not Path(drive).exists():
                        continue
                    for sub in ["Program Files", "Program Files (x86)", "ProgramData"]:
                        p = Path(drive) / sub
                        if p.exists() and str(p) not in added:
                            paths.append(p)
                            added.add(str(p))
                    users = Path(drive) / "Users"
                    if users.exists():
                        for user_dir in users.iterdir():
                            games_dir = user_dir / "Games"
                            if games_dir.exists() and str(games_dir) not in added:
                                paths.append(games_dir)
                                added.add(str(games_dir))
            
            exe_path = None
            if game_name:
                exe_path = find_executable_by_fuzzy_name(game_name, paths,
                                                         deadline=_deadline)

            if not exe_path and appid:
                exe_path = find_executable_by_fuzzy_name(appid, paths,
                                                         deadline=_deadline)

            if not exe_path:
                # No executable found, but still return the parsed appid
                # so launcher URLs (battlenet://, etc.) can be saved for launching
                return None, appid
            
            return str(exe_path), appid
        except Exception as e:
            logger.error(f"Error resolving launcher URL: {e}")
            return None, None

    def _browse_exe(self, start_dir: str = ""):
        # Qt widget dialog, ONE window: a folder shortcut navigates in place
        # (see _ExePickerDialog) — reopening a fresh native dialog per hop
        # lost the navigation history ("back" stopped working).
        from core.resolvers import executable_name_filter
        dlg = _ExePickerDialog(
            self, t('add_game.select_executable'),
            executable_name_filter(),
        )
        if start_dir:
            dlg.setDirectory(start_dir)
        if dlg.exec() != QFileDialog.DialogCode.Accepted:
            return
        _sel = dlg.selectedFiles()
        path = _sel[0] if _sel else ""
        if path:
            # Extract name and directory from shortcut filename (e.g., "My Game.url" -> "My Game").
            # For plain executables, a generic stem ("game", "launcher"…) is
            # replaced with the install-folder name so the game is never
            # proposed as "Game" or "Launcher".
            self._shortcut_name = None
            self._shortcut_dir = None
            from core.save_detector import display_name_for_added_file
            filename = display_name_for_added_file(path)
            if filename:
                self._shortcut_name = filename
                # Store the shortcut's directory for exe search
                self._shortcut_dir = str(Path(path).parent)
                # Auto-fill name field if empty
                if not self._name_edit.text().strip():
                    self._name_edit.setText(filename)
            
            # If .url file, read the URL from it
            if path.lower().endswith('.url'):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    for line in content.splitlines():
                        if line.lower().startswith('url='):
                            url = line[4:].strip()
                            if url:
                                self._appid_edit.setText(url)
                                self._seed_fingerprint_from_path(url)   # infer source from launcher URL
                                self._resolve_exe_from_url_async(url)
                                return
                except Exception as e:
                    logger.warning(f"Failed to read .url file: {e}")
            elif path.lower().endswith('.desktop'):
                # Linux launcher: same role as .lnk — resolve to what it starts.
                from core.resolvers import resolve_desktop_entry
                target = resolve_desktop_entry(path)
                if target and target != path:
                    self._exe_edit.setText(target)
                    self._detect_btn.setEnabled(True)
                    self._seed_fingerprint_from_path(target)
                    self._auto_detect_image(target)
                    return
            elif path.lower().endswith('.lnk'):
                from core.resolvers import resolve_lnk_target
                target = resolve_lnk_target(path)
                if target and target != path:
                    self._exe_edit.setText(target)
                    self._detect_btn.setEnabled(True)
                    self._seed_fingerprint_from_path(target)   # infer source from resolved exe
                    self._auto_detect_image(target)
                    return
            self._exe_edit.setText(path)
            self._detect_btn.setEnabled(True)
            self._seed_fingerprint_from_path(path)             # infer source from exe path
            self._auto_detect_image(path)

    # ── Drag & drop from Desktop ──────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        path = urls[0].toLocalFile()
        p = Path(path)
        # Platform-aware (see core.resolvers.is_addable_file): Windows
        # extensions, or on Unix the exec-bit binaries and .sh/.AppImage/
        # .x86_64/.desktop equivalents.
        from core.resolvers import is_addable_file, resolve_desktop_entry
        if not is_addable_file(path):
            return
        event.acceptProposedAction()
        # Generic exe stems ("nw", "game", "launcher"…) fall back to the
        # install-folder name; shortcut filenames are kept as-is (shared helper).
        from core.save_detector import display_name_for_added_file
        _display = display_name_for_added_file(path)
        self._shortcut_name = _display
        self._shortcut_dir = str(p.parent)
        if not self._name_edit.text().strip():
            self._name_edit.setText(_display)
        if p.suffix.lower() == '.url':
            try:
                content = p.read_text(encoding='utf-8')
                for line in content.splitlines():
                    if line.lower().startswith('url='):
                        url = line[4:].strip()
                        if url:
                            self._appid_edit.setText(url)
                            self._seed_fingerprint_from_path(url)   # infer source from launcher URL
                            self._resolve_exe_from_url_async(url)
                            return
            except Exception as e:
                logger.warning(f"Failed to read .url file: {e}")
        elif p.suffix.lower() == '.desktop':
            # Linux launcher: same role as .lnk — resolve to what it starts.
            target = resolve_desktop_entry(path)
            if target and target != path:
                self._exe_edit.setText(target)
                self._detect_btn.setEnabled(True)
                self._seed_fingerprint_from_path(target)
                self._auto_detect_image(target)
                return
        elif p.suffix.lower() == '.lnk':
            from core.resolvers import resolve_lnk_target
            target = resolve_lnk_target(path)
            _t_clean = (target or "").strip().strip('"')
            if _t_clean and _t_clean != path and Path(_t_clean).is_dir():
                # Folder shortcut: not a game — the target folder becomes the
                # search hint and the file picker opens inside it, mirroring
                # the browse flow (a directory must never land in the exe
                # field).
                self._shortcut_dir = _t_clean
                self._browse_exe(start_dir=_t_clean)
                return
            if target and target != path:
                self._exe_edit.setText(target)
                self._detect_btn.setEnabled(True)
                self._seed_fingerprint_from_path(target)   # infer source from resolved exe path
                self._auto_detect_image(target)
                return
        self._exe_edit.setText(path)
        self._detect_btn.setEnabled(True)
        self._seed_fingerprint_from_path(path)             # infer source from exe path
        self._auto_detect_image(path)

    def _on_name_changed(self, text: str):
        """Enable/disable web search button based on name field."""
        self._web_search_btn.setEnabled(bool(text.strip()))

    def _find_game_by_launcher(self, url: str = "", appid: str = ""):
        """Library entry already registered for this launcher URL / appid.

        Deliberately a STRING comparison, not get_by_exe(): a launcher URL is
        not a filesystem path, and Path("steam://…").resolve() turns it into
        nonsense relative to the cwd. An auto-added launcher game keeps the
        raw URL in `appid` (see MainWindow._auto_add_game_from_overlay) while
        one added through this dialog before its URL could be resolved keeps
        it in `exe_path` — so both fields are checked, plus the bare appid.
        """
        from core.resolvers import is_launcher_url
        url = (url or "").strip()
        # Only a real launcher URL may be compared against exe_path below —
        # otherwise passing a plain path here would "match" any game that
        # simply has that exe, which is get_by_exe's job, not this one.
        url_cf = url.casefold() if (url and is_launcher_url(url)) else ""
        appid_cf = (appid or "").strip().casefold()
        if not url_cf and not appid_cf:
            return None
        try:
            games = get_library().all_games()
        except Exception:
            return None
        for g in games:
            if self._editing_entry and g.id == self._editing_entry.id:
                continue
            g_appid = (g.appid or "").strip().casefold()
            g_exe = (g.exe_path or "").strip().casefold()
            if url_cf and (g_appid == url_cf or g_exe == url_cf):
                return g
            # The bare id ("413150") is only meaningful in the appid field —
            # matching it against exe_path would be a coincidence, not a game.
            if appid_cf and g_appid == appid_cf:
                return g
        return None

    def _report_already_in_library(self, existing) -> None:
        """Tell the user this game is already there and stop the add flow."""
        self._status_lbl.setText(t('add_game.game_exists_with_appid', name=existing.name))
        self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
        logger.info(f"Launcher target already in library: {existing.name} ({existing.id})")

    def _resolve_exe_from_url_async(self, url: str, timeout: int = 30):
        """Start async resolution of URL to exe path.

        Search strategy (same for all launchers):
        1. Suggested paths: extra_watch_paths + launcher directories
        2. Shortcut's own directory (where the .url file was selected)
        3. Common game directories: all Program Files/Program Files (x86) across drives

        Args:
            url: Launcher URL to resolve.
            timeout: Maximum seconds before resolution is abandoned.
        """
        import time
        from core.resolvers import find_executable_by_fuzzy_name
        from core.resolvers import parse_launcher_url, get_appid_from_url

        # Already in the library? Say so NOW, before the fuzzy search: the
        # search can take up to 30s of disk walking to land on an exe the
        # duplicate check would reject anyway — and on a URL it can't resolve
        # it may land on an unrelated exe, which is precisely how a second
        # entry for the same game used to get created.
        if not self._editing_entry:
            existing = self._find_game_by_launcher(url, get_appid_from_url(url) or "")
            if existing is not None:
                self._report_already_in_library(existing)
                self._exe_edit.setPlaceholderText("")
                return

        # Disable save button during resolution
        self._add_btn.setEnabled(False)
        self._detect_btn.setEnabled(False)
        self._search_progress.setVisible(True)
        
        def update_placeholder(text: str):
            self._placeholder_signal.emit(text)
        
        def _re_enable_buttons():
            self._add_btn.setEnabled(True)
            self._detect_btn.setEnabled(True)
            from PySide6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(
                self._search_progress, "hide",
                Qt.ConnectionType.QueuedConnection,
            )
        
        # Update placeholder immediately from main thread
        self._exe_edit.setPlaceholderText(t('add_game.exe_searching'))
        
        def _build_search_paths() -> list[Path]:
            from core.resolvers import _get_suggested_exe_search_paths
            import os
            
            paths = _get_suggested_exe_search_paths()
            
            if self._shortcut_dir:
                sd = Path(self._shortcut_dir)
                if sd.exists() and sd not in paths:
                    paths.insert(0, sd)
            
            if os.name == 'nt':
                added = {str(p) for p in paths}
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    drive = f"{letter}:/"
                    if not Path(drive).exists():
                        continue
                    for sub in ["Program Files", "Program Files (x86)", "ProgramData"]:
                        p = Path(drive) / sub
                        if p.exists() and str(p) not in added:
                            paths.append(p)
                            added.add(str(p))
                    users = Path(drive) / "Users"
                    if users.exists():
                        for user_dir in users.iterdir():
                            games_dir = user_dir / "Games"
                            if games_dir.exists() and str(games_dir) not in added:
                                paths.append(games_dir)
                                added.add(str(games_dir))
            
            return paths
        
        def resolve():
            start_time = time.time()
            # Hard deadline INSIDE the fuzzy search too: the old code only
            # checked the clock BETWEEN phases, so a single cold-cache disk
            # walk could blow past the whole budget and time out with
            # nothing — now the walk stops at the deadline and returns the
            # best candidate found so far.
            deadline_m = time.monotonic() + timeout
            try:
                parsed = parse_launcher_url(url)
                if not parsed:
                    logger.warning(f"Could not parse URL: {url}")
                    update_placeholder("")
                    _re_enable_buttons()
                    return
                
                if self._cancel_event.is_set():
                    update_placeholder(t('add_game.exe_search_cancelled'))
                    _re_enable_buttons()
                    return
                
                appid = get_appid_from_url(url)
                game_name = self._shortcut_name if self._shortcut_name else self._name_edit.text().strip()
                game_name = game_name if game_name else None
                
                search_paths = _build_search_paths()
                
                exe_path = None
                
                if game_name and time.time() - start_time < timeout and not self._cancel_event.is_set():
                    exe_path = find_executable_by_fuzzy_name(
                        game_name, search_paths,
                        deadline=deadline_m, cancel_event=self._cancel_event)

                if not exe_path and appid and time.time() - start_time < timeout and not self._cancel_event.is_set():
                    exe_path = find_executable_by_fuzzy_name(
                        appid, search_paths,
                        deadline=deadline_m, cancel_event=self._cancel_event)

                if not exe_path and self._shortcut_dir and time.time() - start_time < timeout and not self._cancel_event.is_set():
                    update_placeholder(t('add_game.exe_full_scan'))
                    focused = [Path(self._shortcut_dir)]
                    if game_name:
                        exe_path = find_executable_by_fuzzy_name(
                            game_name, focused,
                            deadline=deadline_m, cancel_event=self._cancel_event)
                    if not exe_path and appid:
                        exe_path = find_executable_by_fuzzy_name(
                            appid, focused,
                            deadline=deadline_m, cancel_event=self._cancel_event)
                
                if self._cancel_event.is_set():
                    update_placeholder(t('add_game.exe_search_cancelled'))
                    _re_enable_buttons()
                    return
                
                if time.time() - start_time >= timeout:
                    update_placeholder(t('add_game.exe_timed_out'))
                    _re_enable_buttons()
                    return
                
                if exe_path:
                    update_placeholder("")
                    from PySide6.QtCore import QMetaObject, Qt, Q_ARG
                    QMetaObject.invokeMethod(
                        self._exe_edit, "setText",
                        Qt.ConnectionType.QueuedConnection,
                        Q_ARG(str, str(exe_path)),
                    )
                else:
                    update_placeholder(t('add_game.exe_not_found'))
            except Exception as e:
                logger.error(f"Async URL resolution failed: {e}")
                update_placeholder(f"Error: {str(e)[:30]}")
            finally:
                _re_enable_buttons()
        
        threading.Thread(target=resolve, daemon=True).start()
    
    def _on_exe_changed(self):
        """Handle exe path text changes for auto image detection."""
        exe_path = self._exe_edit.text().strip()
        if exe_path and Path(exe_path).exists():
            self._detect_btn.setEnabled(True)
            self._auto_detect_image(exe_path)
            self._detect_engine_from_exe(exe_path)
        else:
            self._detect_btn.setEnabled(False)

    # ── Engine ───────────────────────────────────────────────────────────────

    def _on_engine_edited(self, _text: str):
        """Anything the user types here wins over later auto-detection."""
        self._engine_user_edited = True

    def _on_language_changed(self, _locale: str = ""):
        """Placeholder / labels follow the new locale; width follows them."""
        self._refresh_engine_locale()

    def _refresh_engine_locale(self):
        if not hasattr(self, "_engine_edit"):
            return
        self._exe_lbl.setText(t("add_game.exe_path"))
        self._engine_edit.setPlaceholderText(t("common.unknown"))
        self._engine_edit.setToolTip(t("add_game.engine_tooltip"))
        # Empty field → width tracks Unknown/Sconosciuto; filled → engine name.
        self._fit_engine_width()

    def _fit_engine_width(self, _text: str = ""):
        """Width follows the visible string: engine text, or placeholder if empty.

        Runs once immediately and once deferred so a post-layout / post-locale
        font polish cannot leave the field sized for the previous string.
        """
        self._apply_engine_width()
        if not self._engine_fit_deferred:
            self._engine_fit_deferred = True
            QTimer.singleShot(0, self._fit_engine_width_deferred)

    def _fit_engine_width_deferred(self):
        self._engine_fit_deferred = False
        self._apply_engine_width()

    def _apply_engine_width(self):
        if not hasattr(self, "_engine_edit"):
            return
        fm = self._engine_edit.fontMetrics()
        raw = self._engine_edit.text().strip()
        placeholder = self._engine_edit.placeholderText() or "?"
        sample = raw or placeholder
        text_w = max(fm.horizontalAdvance(sample), fm.boundingRect(sample).width())
        # padding 8×2 + border 1×2 + slack for bold glyphs / focus frame.
        chrome = 8 + 8 + 1 + 1 + 14
        self._engine_edit.setFixedWidth(min(max(text_w + chrome, 48), 280))
        # setText leaves the cursor at the end; keep the start of the name
        # visible if the field was briefly too narrow.
        if not self._engine_edit.hasFocus():
            self._engine_edit.setCursorPosition(0)

    def _set_engine(self, engine: str, from_user: bool = False):
        """Show *engine* in the compact field, by label when it is a known one."""
        from core.engines.game_engine import engine_display
        text = engine_display(engine) if engine else ""
        self._engine_edit.blockSignals(True)
        self._engine_edit.setText(text)
        self._engine_edit.setCursorPosition(0)
        self._engine_edit.blockSignals(False)
        self._fit_engine_width()
        if from_user:
            self._engine_user_edited = True

    def _detect_engine_from_exe(self, exe_path: str):
        """Fill the engine field from the executable, unless it was typed in."""
        if self._engine_user_edited:
            return
        try:
            from core.engines.game_engine import detect_engine
            self._set_engine(detect_engine(exe_path=exe_path) or "")
        except Exception as e:
            logger.debug(f"Engine detection failed for {exe_path!r}: {e}")

    def _engine_value(self) -> str:
        """What to store: the engine id for a known one, else the typed text.

        Empty / "Unknown" is stored as empty, so a later detection run (a game
        moved, an executable finally pointed at) can still fill it in.
        """
        typed = self._engine_edit.text().strip()
        if not typed or typed == t("common.unknown"):
            return ""
        # Typed text that happens to name a known engine is stored as its id,
        # so the sidebar filter groups it with the detected ones.
        from core.engines.game_engine import known_engines, label as engine_label
        for eng in known_engines():
            if typed.casefold() in (eng.casefold(), engine_label(eng).casefold()):
                return eng
        return typed

    # ── Reviews ──────────────────────────────────────────────────────────────

    def _update_reviews_btn(self):
        from core.library import reviews_display_count
        count = reviews_display_count(self._reviews)
        self._reviews_btn.setText(
            t("reviews.button_n", count=count) if count
            else t("reviews.button"))

    def _open_reviews(self):
        from ui.dialogs.reviews_dialog import ReviewsDialog
        dlg = ReviewsDialog(self._name_edit.text().strip(),
                            self._reviews, self)
        if dlg.exec():
            self._reviews = dlg.reviews()
            self._update_reviews_btn()

    def _get_missing_fields(self) -> list[str]:
        """Return list of human-readable field names that are still empty."""
        missing = []
        if not self._developer_edit.text().strip():
            missing.append(t('add_game.developer'))
        if not self._year_edit.text().strip():
            missing.append(t('add_game.year'))
        if not self._store_urls:
            missing.append(t('add_game.store_url'))
        if not self._desc_edit.toPlainText().strip():
            missing.append(t('library.description'))
        if not getattr(self, '_image_path', None):
            missing.append(t('add_game.image'))
        return missing

    def _download_and_set_image(self, url: str):
        """Download image from URL and set it.

        Uses a realistic browser User-Agent and follows redirects.
        Handles relative protocol-relative URLs (//example.com/img.jpg).
        """
        import logging
        logger = logging.getLogger(__name__)

        if not url:
            return

        # Normalise protocol-relative URLs
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            logger.debug(f"Skipping non-http image URL: {url!r}")
            return

        try:
            import uuid

            _UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": _UA,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "/".join(url.split("/")[:3]) + "/",
                }
            )
            from core.net import open_url as _open_url
            with _open_url(req, timeout=20) as response:
                # Verify Content-Type is an image before reading
                ct = response.headers.get("Content-Type", "")
                if ct and not ct.startswith("image/") and "octet-stream" not in ct:
                    logger.debug(f"Skipping non-image Content-Type {ct!r} for {url!r}")
                    return
                image_data = response.read()

            if len(image_data) < 512:
                logger.debug(f"Downloaded image too small ({len(image_data)} bytes), skipping")
                return

            magic = image_data[:16].hex().upper()
            logger.info(f"Downloaded {len(image_data)}B, Content-Type: {ct!r}, magic bytes: {magic}")

            # Save directly to cache in a subfolder named after the game
            _ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            
            # Get game folder using get_install_folder_name for consistency
            exe_path = self._exe_edit.text().strip() if hasattr(self, '_exe_edit') else ""
            game_name = self._name_edit.text().strip() or "unknown"
            game_id = self._editing_entry.id if self._editing_entry else ""
            game_folder = get_install_folder_name(exe_path, game_name, game_id, self._editing_entry.computed_folder_name if self._editing_entry else None)
            game_icon_dir = _ICON_CACHE_DIR / game_folder
            game_icon_dir.mkdir(parents=True, exist_ok=True)
            self._created_icon_dirs.add(game_icon_dir)
            
            # Get original filename from URL
            clean_url = url.split("?")[0]
            original_name = Path(clean_url).stem

            # Sanitize filename
            safe_name = "".join(c for c in original_name if c.isalnum() or c in "._- ")[:50]
            # Always save as .jpg (compression converts to JPEG)
            filename = f"{safe_name}.jpg"
            cache_path = game_icon_dir / filename

            # If file exists, check if it's the same (by hash) or rename
            if cache_path.exists():
                import hashlib

                new_hash = hashlib.md5(image_data).hexdigest()
                try:
                    existing_hash = hashlib.md5(cache_path.read_bytes()).hexdigest()
                except (OSError, IOError):
                    existing_hash = None

                if existing_hash == new_hash:
                    pass  # Same file — reuse
                else:
                    filename = f"{safe_name}_{uuid.uuid4().hex[:8]}.jpg"
                    cache_path = game_icon_dir / filename

            # Try to decode with Qt first (supports JPEG/PNG/WebP/AVIF via system plugins)
            _qt_ok = False
            _px = QPixmap()
            if _px.loadFromData(image_data) and not _px.isNull():
                self._pending_pixmap = _px
                _qt_ok = True

            def _encode_jpeg(pil_img, dest: str):
                """Re-encode for the cache: clamp huge sources to 1280px on the
                long edge (LANCZOS, shrink-only) and save as high-quality JPEG.
                quality=88 + 4:4:4 chroma (subsampling=0) instead of the old
                quality=80 + default 4:2:0, which visibly grained flat-color
                art at card/modal sizes for a negligible size difference."""
                from PIL import Image as _PILImg
                if pil_img.mode != "RGB":
                    pil_img = pil_img.convert("RGB")
                _lanczos = getattr(_PILImg, "Resampling", _PILImg).LANCZOS
                pil_img.thumbnail((1280, 1280), _lanczos)
                pil_img.save(dest, "JPEG", quality=88, optimize=True, subsampling=0)

            # Save with correct extension based on actual content
            if _qt_ok:
                try:
                    # Imported INSIDE the try on purpose: a broken/absent PIL
                    # (a frozen build missing PIL._imaging raises ImportError
                    # here) must fall through to the Qt encoder below, not
                    # abort the whole download — Qt has already decoded the
                    # image at this point, so the only thing PIL adds is the
                    # JPEG re-encode.
                    from PIL import Image as _PILImage
                    import io as _io
                    _pil_img = _PILImage.open(_io.BytesIO(image_data))
                    _encode_jpeg(_pil_img, str(cache_path))
                    logger.info(f"Saved as JPEG via PIL: {cache_path.name}")
                except Exception as _pil_err:
                    logger.info(f"PIL re-encode unavailable ({_pil_err}) — using Qt")
                    # Qt encodes the already-decoded pixmap instead, applying
                    # the same 1280px clamp so the cache stays comparable.
                    _px_save = self._pending_pixmap
                    if max(_px_save.width(), _px_save.height()) > 1280:
                        _px_save = _px_save.scaled(
                            1280, 1280,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    if _px_save.save(str(cache_path), "JPEG", 88):
                        logger.info(f"Saved as JPEG via Qt: {cache_path.name}")
                    else:
                        png_path = cache_path.with_suffix(".png")
                        if _px_save.save(str(png_path), "PNG"):
                            cache_path = png_path
                            logger.info(f"Saved as PNG via Qt: {png_path.name}")
                        else:
                            cache_path.write_bytes(image_data)
                            logger.warning("Qt/PIL both failed to encode, saved raw bytes")
            else:
                logger.warning(f"Qt could not decode (magic: {magic}) — trying PIL")
                _saved = False
                try:
                    from PIL import Image as _PILImage
                    import io as _io
                    try:
                        import pillow_avif
                    except ImportError:
                        pass
                    _pil_img = _PILImage.open(_io.BytesIO(image_data))
                    _encode_jpeg(_pil_img, str(cache_path))
                    self._pending_pixmap = QPixmap(str(cache_path))
                    logger.info(f"PIL decoded (AVIF plugin?) and saved as JPEG: {cache_path.name}")
                    _saved = True
                except Exception:
                    pass
                if not _saved:
                    # Try system AVIF codec via file (Windows 11 + AVIF Extension)
                    _avif_p = cache_path.with_suffix(".avif")
                    _avif_p.write_bytes(image_data)
                    _px2 = QPixmap(str(_avif_p))
                    if not _px2.isNull():
                        self._pending_pixmap = _px2
                        cache_path = _avif_p
                        logger.info(f"Loaded via system AVIF codec: {_avif_p.name}")
                        _saved = True
                    else:
                        _avif_p.unlink(missing_ok=True)
                if not _saved:
                    cache_path.write_bytes(image_data)
                    logger.warning(f"No decoder available for format (magic: {magic}) — saved raw")

            # Apply the downloaded image to the carousel immediately
            self._set_web_image(str(cache_path))
            # Track URL ↔ local path mapping for subsequent unchanged checks
            self._image_url_cache[url] = str(cache_path)
            self._image_path_to_url[str(cache_path)] = url
        except Exception as e:
            logger.error(f"Failed to download image: {e}")

    def _set_web_image(self, image_path: str):
        """Set the downloaded web image - adds to existing detected images."""
        self._image_path = image_path
        # Add to existing detected images (don't replace)
        if not hasattr(self, '_detected_images') or not self._detected_images:
            self._detected_images = [image_path]
            self._current_image_idx = 0
        elif image_path not in self._detected_images:
            self._detected_images.append(image_path)
            self._current_image_idx = len(self._detected_images) - 1
        else:
            self._current_image_idx = self._detected_images.index(image_path)

        # Use pending pixmap pre-loaded from original bytes if available
        pending = getattr(self, '_pending_pixmap', None)
        if pending and not pending.isNull():
            scaled = scaled_for_screen(pending, 90, 56)
            self._img_preview.setPixmap(scaled)
            self._img_preview.setText("")
            self._pending_pixmap = None
        else:
            self._update_image_preview(image_path)
        self._update_nav_buttons()

    @staticmethod
    def _clean_tag(text: str) -> str:
        """Normalize a tag coming from web APIs: HTML entities (&#039; → ')
        decoded, surrounding whitespace stripped."""
        import html
        return html.unescape(text or "").strip()

    @classmethod
    def _split_tag_text(cls, values) -> list[str]:
        """Normalize free-form tag text into a clean tag list.

        *values* is a string or an iterable of strings; each is split on
        commas (whitespace around the comma is irrelevant — "avventura ,
        azione" is two tags, never one), cleaned via _clean_tag and deduped
        case-insensitively, preserving order and the first-seen casing.
        Single entry point for every tag ingress (typed input, saved
        entries, web genres), so no path can let a comma-joined blob
        through as one tag."""
        from core.library import tag_merge_key
        if isinstance(values, str):
            values = [values]
        seen: set[str] = set()
        out: list[str] = []
        for v in values or []:
            for piece in str(v).split(','):
                ct = cls._clean_tag(piece)
                if ct and tag_merge_key(ct) not in seen:
                    seen.add(tag_merge_key(ct))
                    out.append(ct)
        return out

    def _apply_web_tags(self, genres: list[str]):
        """Apply genres as tags from web search."""
        if not genres:
            return

        if not hasattr(self, '_tags'):
            self._tags = []

        from core.library import tag_merge_key
        _existing = {tag_merge_key(x) for x in self._tags}
        # Web genres join the catalog's established spelling too (see the
        # same canonicalization in _add_tag_from_input).
        try:
            _canon = {tag_merge_key(p): p for p in self._known_tags_pool()}
        except Exception:
            _canon = {}
        for genre in self._split_tag_text(genres[:5]):  # Limit to 5 raw entries
            genre = _canon.get(tag_merge_key(genre), genre)
            if tag_merge_key(genre) not in _existing:
                _existing.add(tag_merge_key(genre))
                self._tags.append(genre)
        
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()
        
        logger.debug(f"Applied web search tags: {genres}")

    def _auto_detect_image(self, exe_path: str):
        """Auto-detect ALL game images when exe is selected, enabling navigation."""
        try:
            # Only auto-detect if no image is already set by user
            if hasattr(self, '_image_path') and self._image_path:
                logger.debug(f"Image already set, skipping auto-detection: {self._image_path}")
                return

            from core.library import GameEntry
            from ui.widgets.game_items import _find_all_game_images

            # Use the game name from the name field if available
            from core.save_detector import derive_display_name
            game_name = self._name_edit.text().strip() or derive_display_name(exe_path)

            temp_entry = GameEntry(
                id="temp",
                name=game_name,
                exe_path=exe_path,
                save_paths=[],
                auto_added=True
            )

            # Compress any legacy uncompressed images in the cache folder
            game_id = self._editing_entry.id if self._editing_entry else ""
            cfn = self._editing_entry.computed_folder_name if self._editing_entry else None
            _gf = get_install_folder_name(exe_path, game_name, game_id, cfn)
            _ensure_cache_compressed(_ICON_CACHE_DIR / _gf)

            all_images = _find_all_game_images(temp_entry)
            if all_images:
                # Only update if we don't already have a selected image
                if not self._image_path:
                    self._detected_images = all_images
                    self._current_image_idx = 0
                    self._image_path = all_images[0]
                else:
                    # Merge with existing, avoid duplicates
                    for img in all_images:
                        if img not in self._detected_images:
                            self._detected_images.append(img)
                self._update_image_preview(self._image_path)
                self._update_nav_buttons()
                logger.info(f"Auto-detected {len(all_images)} game image(s): {[Path(p).name for p in all_images]}")
            else:
                if not self._detected_images:
                    self._current_image_idx = -1
                    self._update_nav_buttons()
        except Exception as e:
            logger.debug(f"Auto image detection failed: {e}")

    def _browse_save(self):
        from ui.widgets.file_pickers import pick_folder
        path = pick_folder(self, t('add_game.select_save_folder'))
        if path:
            self._add_path_with_validation(path)

    def _add_manual_path(self):
        p = self._manual_path.text().strip()
        if p:
            self._add_path_with_validation(p)
            self._manual_path.clear()

    # ── Path list management ──────────────────────────────────────────────────

    def _add_path_with_validation(self, path_str: str):
        """Validate a manually-added path and warn if it looks suspicious."""
        from core.save_detector import validate_save_path
        result = validate_save_path(path_str)
        if not result.ok:
            reply = question_window_modal(
                self, t('add_game.path_warning'),
                t('add_game.add_anyway_question', warning=result.warning),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._add_path(path_str)

    def _add_path(self, path_str: str, detected: bool = False):
        """Add a save path row. detected=True places it in the detected section."""
        if path_str in self._save_paths:
            return
        # A row deleted earlier in THIS session must not be resurrected by a
        # re-run of auto-detect (same rule the confirmation panel applies in
        # add_paths). Manual add / Browse / an explicit restore still get
        # through: those are the user asking for the path back.
        if detected and path_str in self._removed_paths:
            return
        self._save_paths.append(path_str)
        self._paths_empty_lbl.setVisible(False)

        game_id = self._editing_entry.id if self._editing_entry else ""
        row = PathRow(path_str, game_id=game_id)
        if self._editing_entry and path_str in (self._editing_entry.excluded_save_paths or []):
            row.set_checked(False)
        row.remove_requested.connect(self._remove_path)
        row.setProperty("detected", detected)

        # Manual paths go before the separator; detected go after it
        sep_idx = self._paths_layout.indexOf(self._detected_sep)
        if detected:
            # Only show separator when BOTH manual and detected paths exist
            has_manual = any(
                isinstance(self._paths_layout.itemAt(i).widget() if self._paths_layout.itemAt(i) else None, PathRow)
                and not (self._paths_layout.itemAt(i).widget().property("detected") if self._paths_layout.itemAt(i).widget() else False)
                for i in range(self._paths_layout.count())
            )
            self._detected_sep.setVisible(has_manual)
            self._detected_section_lbl.setVisible(True)
            # Insert before stretch (last item)
            insert_at = self._paths_layout.count() - 1
        else:
            # Insert before the separator
            insert_at = sep_idx if sep_idx >= 0 else self._paths_layout.count() - 1

        self._paths_layout.insertWidget(insert_at, row)
        # Recompute net sizes for all rows (exclude child-paths from parent sizes)
        self._refresh_path_sizes()

    def _refresh_path_sizes(self):
        """Update size labels for all PathRows, excluding files in child paths."""
        all_paths = list(self._save_paths)
        for i in range(self._paths_layout.count()):
            item = self._paths_layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, PathRow):
                continue
            parent_path = Path(w._path)
            # Find sibling paths that are strict sub-paths of this one
            excluded = set()
            for other in all_paths:
                if other == w._path:
                    continue
                try:
                    other_p = Path(other)
                    other_p.relative_to(parent_path)  # raises if not child
                    excluded.add(other)
                except ValueError:
                    pass
            w.set_excluded_subdirs(excluded)

    def _remove_path(self, path_str: str):
        if path_str in self._save_paths:
            self._save_paths.remove(path_str)
        # Deleting a row here is the same act as the trash icon in the save
        # confirmation panel: the path must stop being proposed by future
        # scans. Recorded now, persisted to the ignored-paths store on Save
        # (see _persist_removed_paths) — so it lands in both "Ignored paths"
        # here and "Excluded save paths" in Settings.
        if path_str not in self._removed_paths:
            self._removed_paths.append(path_str)
        self._refresh_ignored_paths_count()
        # Find and remove the PathRow widget
        for i in range(self._paths_layout.count()):
            item = self._paths_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, PathRow) and w._path == path_str:
                self._paths_layout.removeWidget(w)
                w.deleteLater()
                break
        # Hide detected section markers if no detected rows remain
        has_detected = any(
            isinstance(self._paths_layout.itemAt(i).widget() if self._paths_layout.itemAt(i) else None, PathRow)
            and (self._paths_layout.itemAt(i).widget().property("detected") if self._paths_layout.itemAt(i).widget() else False)
            for i in range(self._paths_layout.count())
        )
        has_manual = any(
            isinstance(self._paths_layout.itemAt(i).widget() if self._paths_layout.itemAt(i) else None, PathRow)
            and not (self._paths_layout.itemAt(i).widget().property("detected") if self._paths_layout.itemAt(i).widget() else False)
            for i in range(self._paths_layout.count())
        )
        # Only show separator when BOTH manual and detected paths exist
        self._detected_sep.setVisible(has_detected and has_manual)
        self._detected_section_lbl.setVisible(has_detected)
        if not self._save_paths:
            self._paths_empty_lbl.setVisible(True)

    # ── Auto-detect ───────────────────────────────────────────────────────────

    def _start_detect(self, general_scan: bool = False):
        if self._detection_in_progress:
            return
        from core.save_detector import reset_cancel
        reset_cancel()
        name = self._name_edit.text().strip()
        exe  = self._exe_edit.text().strip()
        if not name and not exe:
            return
        from core.resolvers import is_launcher_url
        if exe and is_launcher_url(exe):
            return
        self._detection_in_progress = True
        self._add_btn.setEnabled(False)
        self._detect_btn.setEnabled(False)
        self._detect_btn.setText(t("add_game.detecting"))
        self._detect_progress.setVisible(True)
        self._extended_scan_btn.setEnabled(False)  # disable while scanning

        if self._detect_worker and self._detect_worker.isRunning():
            try:
                self._detect_worker.found.disconnect(self._on_detected)
            except RuntimeError:
                pass
            self._detect_worker.stop()
            self._detect_worker.wait(1500)

        tracked_snapshot = {}
        try:
            from core.monitor import get_monitor
            monitor = get_monitor()
            if monitor:
                tracked_snapshot = monitor.get_tracked_snapshot()
        except Exception:
            pass
        appid = ""
        if hasattr(self, '_appid_edit') and self._appid_edit.text():
            appid = self._appid_edit.text().strip()
        from core.save_detector import derive_display_name
        self._detect_worker = DetectWorker(name or derive_display_name(exe), exe, general_scan, tracked_snapshot=tracked_snapshot, appid=appid)
        self._detect_worker.found.connect(self._on_detected)
        self._detect_worker.start()

    def _start_extended_detect(self):
        """Re-run detection with broad filesystem scan enabled."""
        self._start_detect(general_scan=True)

    def _on_detected(self, paths: list[str], is_live_tracking: bool = False):
        self._detection_in_progress = False
        self._add_btn.setEnabled(True)
        self._detect_btn.setEnabled(True)
        self._detect_btn.setText(t("add_game.detect"))
        self._detect_progress.setVisible(False)
        # Re-enable extended scan button now that the normal scan is done
        self._extended_scan_btn.setEnabled(True)

        # Store the live tracking info for status display
        self._is_live_tracking = is_live_tracking
        self._detection_method = "live_tracking" if is_live_tracking else "filesystem"
        
        # Path-normalized membership: a detected path differing only by
        # case/trailing separator from an already-listed one is a duplicate,
        # not a new row.
        def _norm(p: str) -> str:
            return os.path.normcase(os.path.normpath(p))
        existing = {_norm(x) for x in self._save_paths}

        added = 0
        for p in paths:
            if _norm(p) not in existing:
                self._add_path(p, detected=True)   # below separator
                existing.add(_norm(p))
                added += 1

        if added:
            if is_live_tracking:
                status_msg = t('add_game.paths_detected', count=added)
                style = f"color:{palette('info')};font-size:11px;"  # Blue for live tracking
            elif getattr(self._detect_worker, '_general_scan', False):
                status_msg = t('add_game.paths_found_general', count=added)
                style = f"color:{palette('warning')};font-size:11px;"  # Orange for general scan
            else:
                status_msg = t('add_game.paths_detected', count=added)
                style = f"color:{palette('success')};font-size:11px;"  # Green for filesystem scan

            self._status_lbl.setText(status_msg)
            self._status_lbl.setStyleSheet(style)

            # If we found paths via live tracking, we can be more confident
            if is_live_tracking and added > 0:
                self._auto_select_detected_paths()
        elif paths:
            # Detection DID find the save location — it just matches what is
            # already listed. That's a confirmation, not a failure.
            self._status_lbl.setText(t("add_game.paths_already_saved"))
            self._status_lbl.setStyleSheet(f"color:{palette('success')};font-size:12px;")
        else:
            self._status_lbl.setText(t("add_game.not_detected"))
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")

    def _auto_select_detected_paths(self):
        """Auto-select paths that were found via live tracking since they're 100% accurate"""
        # Auto-check all detected paths since they're 100% accurate via live tracking
        for i in range(self._paths_layout.count()):
            item = self._paths_layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, PathRow) and widget.property("detected"):
                # This is a detected path - check it (should already be checked by default)
                widget.set_checked(True)
                logger.info(f"Auto-selected live tracking path: {widget.get_path()}")
        
        # Update status to indicate auto-selection
        if hasattr(self, '_status_lbl'):
            current_text = self._status_lbl.text()
            if "100% accurate" in current_text:
                self._status_lbl.setText(current_text + " (auto-selected)")
                logger.info("Live tracking paths auto-selected due to 100% accuracy")

    def _save_file_exclusions(self, game_id: str):
        """Persist per-path file exclusions from all PathRow file browsers to config."""
        from core.config_manager import get_config as _gc
        _cfg = _gc()
        excluded_files: dict[str, list[str]] = {}
        for i in range(self._paths_layout.count()):
            item = self._paths_layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, PathRow):
                continue
            exc = w.file_list.get_excluded_files() if w.file_list else set()
            if exc:
                excluded_files[w._path] = sorted(exc)
        if excluded_files:
            all_excl = dict(_cfg.get("auto_scan_excluded_files", {}))
            if game_id not in all_excl:
                all_excl[game_id] = {}
            for path_key, files in excluded_files.items():
                existing = set(all_excl[game_id].get(path_key, []))
                all_excl[game_id][path_key] = sorted(existing | set(files))
            _cfg.set("auto_scan_excluded_files", all_excl)

    def _prune_stale_icon_dirs(self, keep_dir: "Path"):
        """On save, remove session-created icon-cache folders EXCEPT *keep_dir*
        (the final game's folder) — folders created under earlier names during a
        web-search rename would otherwise be orphaned. Only touches folders
        directly under the icon cache root; *keep_dir* is never removed."""
        import shutil
        for _stale_dir in list(self._created_icon_dirs):
            try:
                if (_stale_dir != keep_dir
                        and _stale_dir.exists()
                        and _stale_dir.is_dir()
                        and _stale_dir.parent == _ICON_CACHE_DIR):
                    shutil.rmtree(_stale_dir, ignore_errors=True)
                    logger.debug(f"Removed stale icon dir at save: {_stale_dir.name}")
            except Exception as _se:
                logger.debug(f"Could not remove stale icon dir {_stale_dir}: {_se}")
        self._created_icon_dirs.clear()

    def _cleanup_session_icon_dirs(self):
        """Discard icon-cache material created during this (unsaved) session.

        - New game: the whole tracked folder is temp — remove it (this is what
          deletes the "Unknown" folder created before a name was typed).
        - Editing: keep only the image that was on disk BEFORE this session's
          first search ran; session downloads are temp.
        """
        for icon_dir in self._created_icon_dirs:
            if not icon_dir.exists():
                continue
            if self._editing_entry:
                initial_path = self._session_initial_image_path
                for f in icon_dir.iterdir():
                    if f.is_file() and str(f) != initial_path:
                        try:
                            f.unlink()
                        except Exception:
                            pass
            else:
                import shutil
                shutil.rmtree(icon_dir, ignore_errors=True)
        self._created_icon_dirs.clear()

    def reject(self):
        """Override reject to cancel in-flight detection and clean up icon cache before closing."""
        self._cancel_detection()
        self._cleanup_session_icon_dirs()
        try:
            from i18n import get_engine as _get_i18n
            _get_i18n().language_changed.disconnect(self._on_language_changed)
        except (RuntimeError, TypeError):
            pass
        super().reject()

    def _cancel_detection(self):
        """Stop any running detection worker and signal scans to abort."""
        from core.save_detector import cancel_detection
        cancel_detection()
        if self._detect_worker and self._detect_worker.isRunning():
            try:
                self._detect_worker.found.disconnect(self._on_detected)
            except RuntimeError:
                pass
            self._detect_worker.stop()
            if not self._detect_worker.wait(5000):
                logger.warning("Detection worker did not stop within 5s — detaching")
            self._detect_worker.deleteLater()
            self._detect_worker = None

    def closeEvent(self, event):
        self._cancel_detection()
        self._cleanup_session_icon_dirs()
        try:
            from i18n import get_engine as _get_i18n
            _get_i18n().language_changed.disconnect(self._on_language_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    # ── Save ──────────────────────────────────────────────────────────────────


    def _get_all_and_excluded_paths(self) -> tuple[list[str], list[str]]:
        """Return (all_paths, excluded_paths) from the current path-row list.

        Unchecking a row here — whether it was manually added or
        auto-detected — is a soft, reversible exclusion: the path stays in
        save_paths (still shown, still re-includable any time by checking
        it again) and is simply skipped when an actual backup runs. It is
        NOT removed from the list; only the trash icon in the save-scan
        confirmation dialog does that (and that removal is a separate,
        harder action recorded for restoration under that game's
        Settings/Preferences).
        """
        all_paths: list[str] = []
        excluded: list[str] = []
        for i in range(self._paths_layout.count()):
            item = self._paths_layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, PathRow):
                p = widget.get_path()
                all_paths.append(p)
                if not widget.is_checked():
                    excluded.append(p)
        return all_paths, excluded

    def _persist_removed_paths(self, game_id: str, final_paths: list[str]):
        """Record this session's trashed rows in the ignored-paths store.

        Same store the save-confirmation panel writes to, so the paths show
        up under "Ignored paths" for this game AND under "Excluded save
        paths" in Settings, and stop being proposed by future scans. Called
        on Save only — Cancel must leave the store untouched.

        *final_paths* is the list actually being written to the entry: a row
        deleted and then re-added during the same session is back in it, and
        must not be ignored (it would otherwise be filtered out of every
        future scan while still sitting in save_paths).
        """
        removed = [p for p in self._removed_paths if p not in final_paths]
        if not game_id or not removed:
            return
        try:
            from core.config_manager import get_config
            config = get_config()
            store = {k: list(v) for k, v in config.get("auto_scan_deleted_paths", {}).items()}
            existing = store.get(game_id, [])
            merged = existing + [p for p in removed if p not in existing]
            if merged != existing:
                store[game_id] = merged
                config.set("auto_scan_deleted_paths", store)
                logger.info(
                    f"Ignored {len(merged) - len(existing)} deleted save path(s) for game {game_id}")
        except Exception as e:
            logger.warning(f"Could not record deleted save paths for {game_id}: {e}")

    # ── Tag input: ghost completion + commit ─────────────────────────────────

    def _known_tags_pool(self) -> list[str]:
        """All tags registered across the library (cleaned, case-insensitively
        deduped, alphabetical) — the ghost-completion vocabulary. Built once
        per dialog: the library doesn't change while the dialog is open."""
        pool = getattr(self, '_known_tags_cache', None)
        if pool is None:
            from core.library import tag_merge_key
            seen: dict[str, str] = {}
            try:
                for g in get_library().all_games():
                    for x in (g.tags or []):
                        for ct in self._split_tag_text(x):
                            seen.setdefault(tag_merge_key(ct), ct)
            except Exception:
                logger.debug("Tag pool build failed", exc_info=True)
            pool = sorted(seen.values(), key=str.casefold)
            self._known_tags_cache = pool
        return pool

    def _current_tag_segment(self) -> str:
        """The text after the last comma (the tag being typed right now)."""
        return self._tag_input.text().rpartition(',')[2].lstrip()

    def _tag_suggestions(self) -> list[str]:
        """Registered tags matching the current segment (contains-match,
        case-insensitive; prefix matches first so ↓ starts on the best
        completion). Tags already on the entry are never offered."""
        seg = self._current_tag_segment()
        if not seg:
            return []
        from core.library import tag_merge_key
        seg_cf = seg.casefold()
        existing = {tag_merge_key(x) for x in getattr(self, '_tags', [])}
        matches = [c for c in self._known_tags_pool()
                   if seg_cf in c.casefold() and tag_merge_key(c) not in existing]
        matches.sort(key=lambda c: (not c.casefold().startswith(seg_cf), c.casefold()))
        return matches[:50]

    def _update_tag_suggest(self):
        """Refresh the popup while typing. EVERY text edit returns to the
        NULL selection (keep_selection=False): only explicit ↓/↑
        navigation may highlight a row, so after browsing with the arrows
        a keystroke always gives the ghost-free state back and Enter means
        "commit my text" again."""
        matches = self._tag_suggestions()
        self._tag_suggest_matches = matches
        if not matches:
            self._hide_tag_suggest()
            return
        if not self._tag_input.hasFocus():
            return          # never OPEN unfocused; FocusOut hides
        self._tag_suggest.set_items(matches, select_first=False,
                                    keep_selection=False)
        self._tag_input.set_ghost("")
        pos = self._tag_input.mapTo(self, QPoint(0, self._tag_input.height()))
        self._tag_suggest.move(pos)
        self._tag_suggest.setFixedWidth(max(self._tag_input.width(), 200))
        self._tag_suggest.show()
        self._tag_suggest.raise_()
        self._apply_tag_ghost()

    def _hide_tag_suggest(self):
        if self._tag_suggest.isVisible():
            self._tag_suggest.hide()
        self._tag_input.set_ghost("")

    def _apply_tag_ghost(self):
        """Mirror the HIGHLIGHTED popup tag into the input as a paint-only
        hint: typed 'av' + highlighted 'Avventura' paints 'ventura'; a
        non-prefix match is shown as '  —  name'. No highlight, no ghost."""
        picked = self._tag_suggest_current()
        seg = self._current_tag_segment()
        if not picked or not seg:
            self._tag_input.set_ghost("")
            return
        if picked.casefold().startswith(seg.casefold()) and len(picked) > len(seg):
            self._tag_input.set_ghost(picked[len(seg):])
        else:
            self._tag_input.set_ghost(f"  —  {picked}")

    def _tag_suggest_current(self) -> str:
        """The highlighted popup tag, or "" when the popup is closed or the
        user hasn't navigated to a row yet."""
        if not self._tag_suggest.isVisible():
            return ""
        row = self._tag_suggest.current_row()
        m = self._tag_suggest_matches
        return m[row] if 0 <= row < len(m) else ""

    def _on_tag_suggest_clicked(self, row: int):
        m = self._tag_suggest_matches
        if 0 <= row < len(m):
            self._complete_tag_text(m[row])

    def _complete_tag_text(self, match: str):
        """Confirming the highlighted suggestion (Enter on it, a row click,
        or a click on the painted ghost) COMPLETES THE TEXT: the current
        segment becomes the registered tag, as physical text — NO chip is
        added yet. The user then either types ", " and continues with the
        next tag, or presses Enter again (no highlight anymore) to commit
        everything as chips."""
        if not match:
            return
        head, sep, _seg = self._tag_input.text().rpartition(',')
        self._tag_input.setText((head + sep + ' ' if sep else '') + match)
        self._hide_tag_suggest()   # highlight consumed — next Enter commits

    def _add_tag_from_input(self):
        # Enter COMPLETES the highlighted suggestion when the user
        # navigated the popup (↓/↑) — see _complete_tag_text; with no
        # highlight it commits the text AS TYPED, so a brand-new tag that
        # happens to prefix a registered one is always enterable. A comma
        # separates several tags entered in one go — "action, rpg, indie"
        # becomes three chips (whitespace around each comma is irrelevant);
        # each piece is cleaned and de-duplicated.
        picked = self._tag_suggest_current()
        if picked:
            self._complete_tag_text(picked)
            return
        added = False
        from core.library import tag_merge_key
        _existing = {tag_merge_key(x) for x in self._tags}
        # A typed tag that matches a CATALOG tag joins the established
        # spelling ("adventure" → "Adventure", "2d-game" → "2D Game"):
        # case/separator variants must merge, never branch a duplicate in
        # the library.
        _canon = {tag_merge_key(p): p for p in self._known_tags_pool()}
        for tag in self._split_tag_text(self._tag_input.text()):
            tag = _canon.get(tag_merge_key(tag), tag)
            if tag_merge_key(tag) not in _existing:
                _existing.add(tag_merge_key(tag))
                self._tags.append(tag)
                added = True
        if added:
            self._rebuild_tag_chips()
        self._tag_input.clear()

    def eventFilter(self, obj, event):
        """↓/↑/Esc routing for the tag input's suggestion popup. ↓ opens
        the popup when hidden (first press lands on the first row); with it
        open, arrows move the highlight and the ghost follows — and ↑ from
        the FIRST row returns to the null selection (no highlight, no
        ghost), so the arrows can always walk back out of the list. Esc
        closes the popup WITHOUT closing the dialog. Focus loss hides
        it."""
        if obj is getattr(self, '_tag_input', None):
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    if not self._tag_suggest.isVisible() and key == Qt.Key.Key_Down:
                        self._update_tag_suggest()
                    if self._tag_suggest.isVisible():
                        if key == Qt.Key.Key_Up and self._tag_suggest.current_row() == 0:
                            self._tag_suggest.clear_selection()
                        else:
                            self._tag_suggest.move_selection(
                                1 if key == Qt.Key.Key_Down else -1)
                        self._apply_tag_ghost()
                        return True
                    return False
                if key == Qt.Key.Key_Escape and self._tag_suggest.isVisible():
                    self._hide_tag_suggest()
                    return True
            elif event.type() == QEvent.Type.FocusOut:
                self._hide_tag_suggest()
        return super().eventFilter(obj, event)

    def _remove_tag(self, tag: str):
        if tag in self._tags:
            self._tags.remove(tag)
            self._rebuild_tag_chips()

    def _rebuild_tag_chips(self):
        # Remove old chips
        while self._tag_chips_layout.count():
            item = self._tag_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Insert chips — each chip is individually visible, layout left-aligned
        for i, tag in enumerate(self._tags):
            # '&' is QPushButton's mnemonic marker: escape it or a tag like
            # "rock & roll" renders as "rock _roll" (and HTML leftovers like
            # "&#039;" show up as "#039;").
            chip = QPushButton(f"{tag.replace('&', '&&')}  ✕")
            chip.setFixedHeight(22)
            chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            chip.setToolTip(tag)   # full text at a glance even while clipped
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text_secondary')};"
                f"border:1px solid {palette('border')};border-radius:10px;padding:0 8px;"
                f"font-size:11px;}}"
                f"QPushButton:hover{{background:{palette('error')};color:{palette('accent_text')};"
                f"border-color:{palette('error')};}}"
            )
            chip.clicked.connect(lambda _, t=tag: self._remove_tag(t))
            self._tag_chips_layout.insertWidget(i, chip)

        # Pin the container's minimum width to the chips' REAL total extent:
        # widgetResizable sizes the container from its minimumSizeHint, and
        # any under-estimation (DPI/font-metric quirks) silently truncates
        # the scroll range — the arrows then can't reach the last chip's ✕
        # no matter what the scroll math does. An explicit minimum makes
        # the range exact by construction, so scrolling to sb.maximum()
        # ALWAYS ends with the last chip's closing ✕ (plus the trailing
        # margin) in view.
        chips = self._tag_chip_widgets()
        _m = self._tag_chips_layout.contentsMargins()
        _total = _m.left() + _m.right()
        for _w in chips:
            _total += self._tag_chip_width(_w)
        if len(chips) > 1:
            _total += self._tag_chips_layout.spacing() * (len(chips) - 1)
        self._tag_container.setMinimumWidth(_total)
        self._tag_scroll.horizontalScrollBar().setValue(0)

        # Deferred: the scroll range only settles after this rebuild's
        # layout pass (the rangeChanged hook covers later changes too).
        # _repin corrects the estimate with REAL geometry: sizeHint-based
        # totals drift a few px under DPI scaling, and a short range means
        # the last chip's rounded end stays clipped even at sb.maximum().
        QTimer.singleShot(0, self._repin_tag_strip_width)

    def _repin_tag_strip_width(self):
        """Post-layout pass: pin the container to the LAYOUT's own minimum
        width. The rebuild-time estimate can come out a few px short
        (style/DPI rounding); a short container makes the layout compress
        chip spacing, so measuring chip positions would under-correct —
        the layout's minimumSize is the exact uncompressed extent, and
        with it scrolling to maximum() always shows the last chip WHOLE:
        ✕, rounded border, plus the 12 px trailing margin."""
        try:
            real = self._tag_chips_layout.minimumSize().width()
            if real > self._tag_container.minimumWidth():
                self._tag_container.setMinimumWidth(real)
            self._update_tag_arrow_states()
        except RuntimeError:
            pass

    def _tag_chip_widgets(self) -> list:
        out = []
        for i in range(self._tag_chips_layout.count()):
            w = self._tag_chips_layout.itemAt(i).widget()
            if w:
                out.append(w)
        return out

    @staticmethod
    def _tag_chip_width(w) -> int:
        """Effective chip width: sizeHint for the normal (Fixed-policy)
        chips, minimumWidth when a fixed width was forced on the widget."""
        return max(w.sizeHint().width(), w.minimumWidth())

    # Below this residue the strip edge counts as ON a chip boundary — it
    # swallows the 1-2px layout rounding that would otherwise offer a
    # useless micro-scroll step.
    _TAG_SNAP_PX = 8

    def _update_tag_arrow_states(self):
        """Arrows follow the real scroll range: visible only when the chips
        overflow the strip, and each side stays ENABLED exactly while that
        side still hides content — so a tag cut at the arrow's edge can
        always be scrolled the rest of the way into view."""
        try:
            sb = self._tag_scroll.horizontalScrollBar()
            overflow = sb.maximum() > self._TAG_SNAP_PX
            self._tag_left_btn.setVisible(overflow)
            self._tag_right_btn.setVisible(overflow)
            self._tag_left_btn.setEnabled(sb.value() > 0)
            self._tag_right_btn.setEnabled(sb.value() < sb.maximum())
        except RuntimeError:
            pass

    # ── URL bubble helpers ───────────────────────────────────────────────────
    def _add_url_from_input(self):
        url = self._url_input.text().strip()
        if url and url not in self._store_urls:
            # Auto-prefix http if missing
            if url and "://" not in url:
                url = "https://" + url
            self._store_urls.append(url)
            self._url_input.clear()
            self._rebuild_url_chips()

    def _remove_url(self, url: str):
        if url in self._store_urls:
            self._store_urls.remove(url)
        self._rebuild_url_chips()

    def _rebuild_url_chips(self):
        while self._url_chips_layout.count():
            item = self._url_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, url in enumerate(self._store_urls):
            # Label part (clickable to open)
            label = url.replace("https://", "").replace("http://", "").split("/")[0]
            chip_frame = QFrame()
            chip_frame.setFixedHeight(22)
            chip_frame.setStyleSheet(
                f"QFrame{{background:{palette('bg_elevated')};border:1px solid {palette('border')};"
                f"border-radius:10px;}}"
                f"QFrame:hover{{border-color:{palette('accent')};}}"
            )
            chip_inner = QHBoxLayout(chip_frame)
            chip_inner.setContentsMargins(6, 0, 2, 0)
            chip_inner.setSpacing(2)

            lbl_btn = QPushButton(label)
            lbl_btn.setFlat(True)
            lbl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl_btn.setStyleSheet(
                f"QPushButton{{color:{palette('accent')};background:transparent;border:none;"
                f"font-size:10px;padding:0;text-decoration:underline;}}"
                f"QPushButton:hover{{color:{palette('accent_hover')};}}"
            )
            lbl_btn.clicked.connect(lambda _, u=url: __import__('webbrowser').open(u))
            lbl_btn.setToolTip(url)

            rm_btn = QPushButton("✕")
            rm_btn.setFlat(True)
            rm_btn.setFixedSize(16, 16)
            rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            rm_btn.setToolTip(t('add_game.remove_url_tooltip'))
            rm_btn.setStyleSheet(
                f"QPushButton{{color:{palette('text_muted')};background:transparent;border:none;"
                f"font-size:9px;font-weight:700;border-radius:8px;padding:0;}}"
                f"QPushButton:hover{{color:{palette('accent_text')};background:{palette('error')};}}"
            )
            rm_btn.clicked.connect(lambda _, u=url: self._remove_url(u))

            chip_inner.addWidget(lbl_btn)
            chip_inner.addWidget(rm_btn)
            self._url_chips_layout.insertWidget(i, chip_frame)

    def _scroll_tags_left(self):
        """Step the strip LEFT. Each click moves by a fixed fraction of the
        viewport; when the START of the nearest left-clipped chip is within
        reach of one step, the scroll SNAPS exactly onto it, and the final
        step snaps onto 0 — the walk always terminates at the first chip.
        Long chips take several clicks, mirroring _scroll_tags_right."""
        sb = self._tag_scroll.horizontalScrollBar()
        vw = self._tag_scroll.viewport().width()
        step = max(40, int(vw * 0.6))
        target = sb.value() - step
        left_edge = sb.value()
        for w in reversed(self._tag_chip_widgets()):
            if w.x() < left_edge:
                start_target = w.x() - 8
                if start_target >= target:
                    target = start_target   # snap onto the chip's start
                break
        if target <= self._TAG_SNAP_PX:
            target = 0
        sb.setValue(max(target, 0))
        self._update_tag_arrow_states()

    def _scroll_tags_right(self):
        """Step the strip RIGHT until the clipped chip's closing ✕ comes
        into view. Each click advances by a fixed fraction of the viewport;
        when the END of the chip cut at the arrow's edge (its ✕, the chip's
        delimiter) is within reach of one step, the scroll SNAPS exactly
        onto it — a long tag is walked through click by click and never
        left stranded half-hidden (the pinned container width guarantees
        sb.maximum() reaches the last chip's ✕)."""
        sb = self._tag_scroll.horizontalScrollBar()
        vw = self._tag_scroll.viewport().width()
        step = max(40, int(vw * 0.6))
        target = sb.value() + step
        right_edge = sb.value() + vw
        for w in self._tag_chip_widgets():
            if w.x() + w.width() > right_edge:
                end_target = w.x() + w.width() + 8 - vw
                if end_target <= target:
                    target = end_target     # snap onto the chip's ✕
                break
        if sb.maximum() - target <= self._TAG_SNAP_PX:
            target = sb.maximum()
        sb.setValue(min(target, sb.maximum()))
        self._update_tag_arrow_states()

    def _populate_category_combo(self):
        """Fill the category combo with the folder tree (indented)."""
        from core.config_manager import get_config
        from ui.widgets.library_folders import _flatten_folders, _ensure_children_field
        self._category_combo.clear()
        self._category_combo.addItem(t("library.no_folder"), "")
        folders = get_config().get("library_folders", [])
        _ensure_children_field(folders)
        for path, color_key, depth in _flatten_folders(folders):
            indent = "  " * depth
            name = path.split("/")[-1]
            px = QPixmap(12, 12)
            px.fill(QColor(palette(color_key)))
            self._category_combo.addItem(QIcon(px), f"{indent}{name}", path)

    def _add_game(self):
        exe = self._exe_edit.text().strip()
        lib = get_library()
        
        from core.resolvers import is_launcher_url
        
        # If exe is empty but appid contains a launcher URL, promote it to exe
        appid_from_field = self._appid_edit.text().strip()
        if not exe and appid_from_field and is_launcher_url(appid_from_field):
            exe = appid_from_field
            self._exe_edit.setText(exe)
        
        appid = None
        # Capture the launcher URL BEFORE resolution replaces `exe` with a
        # filesystem path — it is the only reliable identity for a game whose
        # exe can't be (or is wrongly) resolved, and the duplicate check below
        # needs it.
        _launcher_url = exe if (exe and is_launcher_url(exe)) else (
            appid_from_field if is_launcher_url(appid_from_field) else "")
        if exe and is_launcher_url(exe):
            resolved_exe, appid = self._resolve_launcher_url(exe)
            if resolved_exe:
                exe = resolved_exe
                self._exe_edit.setText(exe)
                if appid:
                    self._appid_edit.setText(appid)
        
        if not appid:
            appid = appid_from_field

        # Auto-fill game name from launcher URL (the parsed launcher entry
        # carries the game's display name)
        name = self._name_edit.text().strip()
        if not name and appid_from_field and is_launcher_url(appid_from_field):
            from core.resolvers import parse_launcher_url
            parsed = parse_launcher_url(appid_from_field)
            if parsed and parsed.get("game_name"):
                name = parsed["game_name"]
                self._name_edit.setText(name)

        # Now validate name
        if not name:
            self._status_lbl.setText(t('add_game.field_required', field=t('add_game.name')))
            self._status_lbl.setStyleSheet(f"color:{palette('error')};")
            return

        # Duplicate check (only for new games, not edits). Two identities,
        # because a launcher game may have neither field in common with the
        # stored entry: the resolved exe, and the launcher URL/appid — the
        # latter matters when resolution failed or picked a different exe
        # than the one recorded when the game was first added.
        if not self._editing_entry:
            existing = lib.get_by_exe(exe) if exe else None
            if existing is None:
                existing = self._find_game_by_launcher(_launcher_url, appid or "")
            if existing:
                if appid and not existing.appid:
                    existing.appid = appid
                    lib.update_game(existing)
                    self._status_lbl.setText(t('add_game.game_updated_appid', name=existing.name))
                    self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")
                else:
                    self._status_lbl.setText(t('add_game.game_exists_with_appid', name=existing.name))
                    self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
                self.game_added.emit(existing)
                self._created_icon_dirs.clear()  # Keep icon folders on successful save
                self.accept()
                return

        # Skip exe existence check for URLs (they launch via launcher, not directly)
        is_url = exe and is_launcher_url(exe)
        if exe and not is_url and not Path(exe).exists():
            self._status_lbl.setText(t('add_game.executable_not_found', exe=exe))
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
            return

        if self._editing_entry:
            entry = self._editing_entry
            # Capture the path list BEFORE we overwrite it, so rows the user
            # hard-deleted (via _remove_path) can be detected and their
            # provisional backups pruned on save (see the resolve call below).
            _prev_save_paths = set(entry.save_paths or [])
            entry.name       = name
            entry.exe_path   = exe
            # Keep every row's path in save_paths — unchecking a path
            # (manual or auto-detected) is a soft exclusion, not a removal;
            # only excluded_save_paths tracks which ones are currently
            # skipped at backup time.
            all_paths, excluded_paths = self._get_all_and_excluded_paths()
            entry.save_paths = all_paths
            entry.excluded_save_paths = excluded_paths
            entry.save_paths_confirmed = len(all_paths) > 0
            checked_paths = [p for p in all_paths if p not in excluded_paths]
            
            # Update detection method if auto-detected paths were used
            if hasattr(self, '_detection_method') and checked_paths:
                entry.detection_method = self._detection_method
                # ALL auto-detection methods require confirmation at exit
                if self._detection_method in ['live_tracking', 'general_scan', 'filesystem']:
                    entry.requires_confirmation = True
                    entry.save_paths_confirmed = False  # Reset to require confirmation
            
            # Save description, category and tags
            entry.description = self._desc_edit.toPlainText().strip()
            entry.category = self._category_combo.currentData() or ""
            entry.tags = list(self._tags)
            entry.developer  = self._developer_edit.text().strip()
            entry.release_year = self._year_edit.text().strip()
            entry.store_url  = ', '.join(self._store_urls)
            entry.engine     = self._engine_value()
            entry.reviews    = [dict(r) for r in self._reviews]
            # Persist which API source the metadata came from so replacement-tier
            # logic is correct the next time this dialog is opened.
            _fp = getattr(self, '_enrichment_source_fingerprint', {}) or {}
            if _fp.get('source'):
                entry.info_source = _fp['source']
            # Save launcher appid (from URL resolution or manual field)
            if not appid:
                appid = self._appid_edit.text().strip()
            if appid:
                entry.appid = appid
            # Save backup settings
            entry.auto_backup_enabled = self._auto_backup_cb.isChecked()
            entry.backup_interval_sec = self._backup_interval_spin.value() * 60

            # record_name() updates computed_folder_name AND name_history so
            # the sync/backup folder tracks the current title while history
            # preserves every past name for migration.
            old_folder_name = get_install_folder_name(
                entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name
            )
            entry.record_name(name)
            # Disambiguate against any other game already occupying this folder
            # so two entries that share a title can't cross-contaminate their
            # saves/backups. Done BEFORE the icon-cache migration below so the
            # icon and the saves both land in the same (possibly suffixed) folder.
            entry.computed_folder_name = get_library().unique_folder_name(
                entry.computed_folder_name, entry.id
            )
            new_folder_name = entry.computed_folder_name or get_install_folder_name(
                entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name
            )

            # Migrate icon cache folder if the name changed. This must never
            # lose the current icon — see migrate_icon_cache for the cases
            # (the old both-exist branch deleted the old folder wholesale,
            # icon included, when enrichment renamed without a new image).
            self._image_path = migrate_icon_cache(
                old_folder_name, new_folder_name, self._image_path,
            )

            # Compress external images into cache; cached images are already compressed.
            _prev_icon = entry.icon_path
            entry.icon_path = self._ensure_cached_icon(
                self._image_path, entry.exe_path, entry.name, entry.id, entry.computed_folder_name,
            )
            if not entry.icon_path and _prev_icon:
                # Last-resort: keep a previously stored icon that still
                # exists (possibly under the renamed cache folder) instead
                # of silently dropping it.
                _cand = _prev_icon
                if not Path(_cand).exists() and old_folder_name and new_folder_name:
                    _cand = _cand.replace(
                        str(_ICON_CACHE_DIR / old_folder_name),
                        str(_ICON_CACHE_DIR / new_folder_name),
                    )
                if Path(_cand).exists():
                    entry.icon_path = _cand

            lib.update_game(entry)

            # Saving the edit dialog is a genuine confirmation (every path is
            # visible/checkable here), so temporary (pre-confirmation) session
            # backups are resolved PER PATH — a backup covering a row the user
            # deleted here is discarded, the rest are promoted to definitive.
            # Blanket-promoting would keep (and cloud-expose) a session backup
            # for a path that no longer exists, and blanket-discarding would
            # throw away backups for paths the user kept.
            _removed_paths = list(_prev_save_paths - set(all_paths))
            try:
                from core.backup import get_backup_manager
                _bm = get_backup_manager()
                if entry.save_paths_confirmed and not entry.requires_confirmation:
                    # Confirmed: discard removed rows' session backups, promote
                    # the rest (same net effect the auto-scan dialog produces).
                    _bm.resolve_pre_confirmation_backups(
                        entry.id, discarded_paths=_removed_paths,
                        note=t('main.auto_in_game'))
                elif _removed_paths:
                    # Not a full confirmation, but rows were deleted — drop only
                    # those paths' provisional backups; the rest stay provisional
                    # until the user confirms them (at exit or via the panel).
                    _bm.resolve_pre_confirmation_backups(
                        entry.id, discarded_paths=_removed_paths, promote_rest=False)
            except Exception as e:
                logger.warning(
                    f"Could not resolve pre-confirmation backups for {entry.name}: {e}")

            # Rows trashed here are ignored from now on — same treatment the
            # save-confirmation panel gives its trash icon.
            self._persist_removed_paths(entry.id, all_paths)

            self.game_added.emit(entry)

            # Save per-file exclusions from file browsers
            self._save_file_exclusions(entry.id)

        else:
            # Keep every row's path — unchecking is a soft exclusion, not a
            # removal (see _get_all_and_excluded_paths docstring).
            all_paths, excluded_paths = self._get_all_and_excluded_paths()
            checked_paths = [p for p in all_paths if p not in excluded_paths]
            
            # Determine detection method
            detection_method = getattr(self, '_detection_method', 'manual')
            
            entry = GameEntry(
                name=name,
                exe_path=exe,
                save_paths=all_paths,
                excluded_save_paths=excluded_paths,
                auto_added=False,
                save_paths_confirmed=len(all_paths) > 0,
                detection_method=detection_method,
                requires_confirmation=(
                    not checked_paths
                    or detection_method in ['live_tracking', 'general_scan', 'filesystem']
                ),
                machine_id=get_machine_id(),
                auto_backup_enabled=self._auto_backup_cb.isChecked(),
                backup_interval_sec=self._backup_interval_spin.value() * 60,
                description=self._desc_edit.toPlainText().strip(),
                category=self._category_combo.currentData() or "",
                tags=list(self._tags),
                appid=appid,
                developer=self._developer_edit.text().strip(),
                release_year=self._year_edit.text().strip(),
                store_url=', '.join(self._store_urls),
                info_source=(getattr(self, '_enrichment_source_fingerprint', {}) or {}).get('source', ''),
                computed_folder_name=get_folder_name_for_save(name, exe or "", ""),
                engine=self._engine_value(),
                reviews=[dict(r) for r in self._reviews],
            )
            # Initialise name_history for new entries so rename tracking works
            # from day one (record_name also sets computed_folder_name)
            entry.record_name(name)
            # …and the names as they are on disk. The title above has been
            # tidied for the library; the release folder still carries the
            # code and version, which is what a folder of saves kept under
            # the same release name will match on.
            entry.record_exe_hints(exe)
            # Isolate this game's folder from any other library entry sharing the
            # same title (add_game re-checks too, but the icon cache below keys off
            # computed_folder_name, so resolve it here first).
            entry.computed_folder_name = get_library().unique_folder_name(
                entry.computed_folder_name, entry.id
            )
            # Compress external images into cache; cached images are already compressed.
            entry.icon_path = self._ensure_cached_icon(
                self._image_path, entry.exe_path, entry.name, entry.id, entry.computed_folder_name,
            )
            lib.add_game(entry)
            # Deferred until here on purpose: the ignored-paths store is keyed
            # by game id, which only exists once the entry has been built.
            self._persist_removed_paths(entry.id, all_paths)
            self.game_added.emit(entry)

            # Save per-file exclusions from file browsers
            self._save_file_exclusions(entry.id)

        # At save time, remove icon dirs created under OLD game names during this
        # session (a web-search rename creates a new folder; the original must
        # not linger). Keep only the final game's folder. Runs even when the
        # game was saved WITHOUT an icon — previously gated on having one, so a
        # renamed-but-imageless game left its original folder orphaned.
        if entry.icon_path:
            _keep_dir = Path(entry.icon_path).parent
        else:
            _keep_folder = get_install_folder_name(
                entry.exe_path or "", entry.name, entry.id, entry.computed_folder_name)
            _keep_dir = _ICON_CACHE_DIR / _keep_folder
        self._prune_stale_icon_dirs(_keep_dir)

        self.accept()