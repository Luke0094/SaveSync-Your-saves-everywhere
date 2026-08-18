"""
SaveSync - Shared file/folder pickers.

One picker for the whole app, because Qt's native dialog gets shortcuts wrong
in a way that matters here: with ``*.lnk`` in the filter it stops
dereferencing them (FOS_NODEREFERENCELINKS), so a FOLDER shortcut becomes a
returnable item that closes the window instead of a navigation step — and the
native window offers no hook to intercept it.

The widget dialog does virtualise accept(), so both pickers below share the
same treatment: a .lnk pointing at a directory is entered in place (same
window, history preserved), a .lnk pointing at a file comes back as the raw
.lnk path so callers can still derive a name from the shortcut.
"""
import os
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QEvent, QTimer, QStandardPaths, QUrl

from PySide6.QtWidgets import QFileDialog

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette

logger = logging.getLogger(__name__)





# Folder shortcuts the user pinned into the picker's sidebar. Persisted so a
# pinned folder is there on every open, like the Desktop/Home default places.
_PINS_CONFIG_KEY = "browse_sidebar_pins"


def _load_sidebar_pins() -> list[str]:
    """Pinned sidebar folders (absolute paths), only the ones still existing."""
    try:
        from core.config_manager import get_config
        return [p for p in (get_config().get(_PINS_CONFIG_KEY) or [])
                if p and Path(p).is_dir()]
    except Exception:
        return []


def _save_sidebar_pins(pins: list[str]) -> None:
    try:
        from core.config_manager import get_config
        get_config().set(_PINS_CONFIG_KEY, list(pins))
    except Exception:
        logger.debug("could not persist browse sidebar pins", exc_info=True)


def _translate_file_type(raw: str) -> str:
    if not raw:
        return ""
    low = raw.lower()
    if "folder" in low or "directory" in low or "cartella" in low:
        return t("file_picker.type_folder")
    if "application" in low or "executable" in low or "eseguibile" in low or "applicazion" in low or "exe" in low:
        return t("file_picker.type_app")
    if "text" in low or "plain" in low or "testo" in low:
        return t("file_picker.type_text")
    if "pdf" in low or "portable document" in low:
        return t("file_picker.type_pdf")
    if "archive" in low or "zip" in low or "compressed" in low or "rar" in low or "7z" in low or "archivio" in low:
        return t("file_picker.type_archive")
    if "image" in low or "png" in low or "jpeg" in low or "jpg" in low or "immagine" in low or "bitmap" in low:
        return t("file_picker.type_image")
    if "audio" in low or "sound" in low or "mp3" in low or "wav" in low or "flac" in low:
        return t("file_picker.type_audio")
    if "video" in low or "mp4" in low or "mkv" in low or "avi" in low:
        return t("file_picker.type_video")
    if "shortcut" in low or "link" in low or "collegamento" in low:
        return t("file_picker.type_shortcut")
    return raw


def _translate_system_name(raw: str) -> str:
    if not raw:
        return ""
    low = raw.lower().strip()
    if "my computer" in low or "this pc" in low or "questo pc" in low or low == "computer":
        return t("file_picker.my_computer")
    if low == "desktop":
        return t("file_picker.desktop")
    if "documents" in low or "documenti" in low:
        return t("file_picker.documents")
    if "downloads" in low or "download" in low:
        return t("file_picker.downloads")
    home_p = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
    user_name = Path(home_p).name.lower() if home_p else ""
    user_env = os.environ.get("USERNAME", "").lower()
    if low in (user_name, user_env, "home", "user", "cartella utente") and low:
        return t("file_picker.user_home")
    return ""


from PySide6.QtWidgets import QStyledItemDelegate, QHeaderView, QStyleOptionHeader


class _LocalizedHeader(QHeaderView):
    _COL_KEYS = {
        0: "file_picker.col_name",
        1: "file_picker.col_size",
        2: "file_picker.col_type",
        3: "file_picker.col_date_modified",
    }

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(True)

    def initStyleOption(self, option: QStyleOptionHeader) -> None:
        super().initStyleOption(option)
        col = option.section
        if col in self._COL_KEYS:
            option.text = t(self._COL_KEYS[col])


class _SidebarItemDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        raw = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        tr = _translate_system_name(raw)
        if tr:
            option.text = tr


class _FileDetailsDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        col = index.column()
        if col == 0:
            raw = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            tr = _translate_system_name(raw)
            if tr:
                option.text = tr
        elif col == 2:
            raw = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            if raw:
                option.text = _translate_file_type(raw)


def _is_system_pin(url: QUrl, system_urls: list[QUrl] = None) -> bool:
    """Identify if a URL is a permanent protected system pin:
    - My Computer / root / special URLs
    - User Home folder
    - Desktop
    - Documents
    - Downloads
    - Drive roots (C:\\, D:\\, /)
    - Any URL recorded in system_urls
    """
    if not url.isValid() or not url.toString() or url.toString() in ("file:", "file:///", "", "computer:"):
        return True

    path_str = url.toLocalFile() if url.isLocalFile() else url.toString()
    if not path_str:
        return True

    # Normalize incoming path for robust case/separator-agnostic comparison
    try:
        norm_incoming = os.path.normcase(os.path.normpath(os.path.abspath(path_str)))
    except Exception:
        norm_incoming = os.path.normcase(path_str.replace("/", "\\").rstrip("\\"))

    # Drive root (e.g. C:\ or /)
    try:
        p = Path(path_str).resolve()
        if len(p.parts) <= 1 or str(p).rstrip("/\\") == str(p.drive).rstrip("/\\") or norm_incoming.endswith(":\\"):
            return True
    except Exception:
        pass

    # Standard system locations & user folders
    system_paths = set()
    for env_var in ("USERPROFILE", "HOMEPATH", "HOME", "PUBLIC", "OneDrive"):
        val = os.environ.get(env_var)
        if val:
            system_paths.add(val)

    user_home = os.path.expanduser("~")
    if user_home:
        system_paths.add(user_home)
        system_paths.add(os.path.join(user_home, "Desktop"))
        system_paths.add(os.path.join(user_home, "Documents"))
        system_paths.add(os.path.join(user_home, "Downloads"))
        system_paths.add(os.path.join(user_home, "Pictures"))
        system_paths.add(os.path.join(user_home, "Music"))
        system_paths.add(os.path.join(user_home, "Videos"))

    for loc in (
        QStandardPaths.StandardLocation.DesktopLocation,
        QStandardPaths.StandardLocation.HomeLocation,
        QStandardPaths.StandardLocation.DocumentsLocation,
        QStandardPaths.StandardLocation.DownloadLocation,
        QStandardPaths.StandardLocation.MusicLocation,
        QStandardPaths.StandardLocation.PicturesLocation,
        QStandardPaths.StandardLocation.MoviesLocation,
    ):
        p_str = QStandardPaths.writableLocation(loc)
        if p_str:
            system_paths.add(p_str)

    for sp in system_paths:
        try:
            norm_sp = os.path.normcase(os.path.normpath(os.path.abspath(sp)))
            if norm_incoming == norm_sp:
                return True
        except Exception:
            if norm_incoming == os.path.normcase(sp.replace("/", "\\").rstrip("\\")):
                return True

    if system_urls:
        for su in system_urls:
            if su == url:
                return True
            if su.isLocalFile():
                try:
                    norm_su = os.path.normcase(os.path.normpath(os.path.abspath(su.toLocalFile())))
                    if norm_incoming == norm_su:
                        return True
                except Exception:
                    pass

    return False



class _LnkAwareDialog(QFileDialog):
    """QFileDialog that treats a folder shortcut as a navigation step.

    Also provides:
    - Full Italian/English localisation for all visible labels, columns, tooltips, and actions.
    - Protected system sidebar pins (My Computer, User Home, Desktop, Drives, Documents, Downloads).
    - Right-click on any folder in the view with "Aggiungi alla barra laterale".
    """

    def __init__(self, parent, caption: str):
        super().__init__(parent, caption)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setOption(QFileDialog.Option.DontResolveSymlinks, True)

        self._apply_window_chrome()

        self._system_sidebar_urls: list[QUrl] = []
        self._add_common_places()
        self._load_pinned_places()
        self._hook_folder_context_menu()
        self._localize_labels()

        from PySide6.QtWidgets import QFileSystemModel
        model = self.findChild(QFileSystemModel)
        if model is not None:
            model.rootPathChanged.connect(self._redirect_lnk_directory)
            model.directoryLoaded.connect(lambda *_: self._localize_labels())

    def eventFilter(self, watched, event):
        """Block Delete/Backspace key from removing protected system pins and manage context menu on sidebar."""
        if watched is getattr(self, "_sidebar_view", None):
            if event.type() == QEvent.Type.ContextMenu:
                self._on_sidebar_context_menu(event.pos())
                return True
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                    idx = self._sidebar_view.currentIndex()
                    if idx.isValid():
                        urls = list(self.sidebarUrls())
                        if idx.row() < len(urls) and _is_system_pin(urls[idx.row()], self._system_sidebar_urls):
                            return True
        return super().eventFilter(watched, event)

    def _apply_window_chrome(self):
        """Apply theme palette and stylesheet before mapping to prevent color flash in both light and dark modes."""
        from PySide6.QtGui import QColor, QPalette
        from ui.styles.theme import palette, get_theme_manager
        is_dark = get_theme_manager().is_dark()

        bg = QColor(palette("bg"))
        fg = QColor(palette("text"))
        card = QColor(palette("bg_card"))
        input_bg = QColor(palette("bg_input"))
        border = palette("border")
        accent = palette("accent")
        accent_text = palette("accent_text")

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.Base, input_bg)
        pal.setColor(QPalette.ColorRole.AlternateBase, bg)
        pal.setColor(QPalette.ColorRole.Button, card)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Text, fg)
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        pal.setColor(QPalette.ColorRole.Highlight, QColor(accent))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(accent_text))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QFileDialog, QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}"
            f"QListView, QTreeView {{ background-color: {palette('bg_input')}; color: {palette('text')}; border: 1px solid {border}; border-radius: 4px; }}"
            f"QLineEdit, QComboBox {{ background-color: {palette('bg_input')}; color: {palette('text')}; border: 1px solid {border}; border-radius: 4px; padding: 2px 4px; }}"
            f"QToolButton, QPushButton {{ background-color: {palette('bg_card')}; color: {palette('text')}; border: 1px solid {border}; border-radius: 4px; padding: 4px 8px; }}"
            f"QToolButton:hover, QPushButton:hover {{ background-color: {palette('bg_input')}; border-color: {accent}; }}"
            f"QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; }}"
        )
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self, dark=is_dark)
        except Exception:
            pass

    def exec(self):
        self._apply_window_chrome()
        self._localize_labels()
        return super().exec()

    def open(self):
        self._apply_window_chrome()
        self._localize_labels()
        super().open()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_window_chrome()
        self._localize_labels()
        if getattr(self, "_sidebar_view", None) is not None:
            sw = scaled(185, self, min_px=165)
            self._sidebar_view.setMinimumWidth(sw)
        parent = self.parentWidget()
        if parent is not None and parent.isVisible():
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())

    # ── Localisation ──────────────────────────────────────────────────────────

    def _localize_labels(self):
        """Set all visible QFileDialog labels, headers, buttons, and actions to the active locale strings."""
        try:
            self.setLabelText(QFileDialog.DialogLabel.LookIn,
                              t("file_picker.look_in"))
            self.setLabelText(QFileDialog.DialogLabel.FileName,
                              t("file_picker.file_name"))
            self.setLabelText(QFileDialog.DialogLabel.FileType,
                              t("file_picker.file_type"))
            self.setLabelText(QFileDialog.DialogLabel.Accept,
                              t("common.open"))
            self.setLabelText(QFileDialog.DialogLabel.Reject,
                              t("common.cancel"))

            from PySide6.QtWidgets import QToolButton, QTreeView, QListView, QComboBox, QHeaderView
            from PySide6.QtGui import QAction

            # Header columns
            tree = self.findChild(QTreeView, "treeView")
            if tree is not None:
                if not isinstance(tree.header(), _LocalizedHeader):
                    hdr = _LocalizedHeader(Qt.Orientation.Horizontal, tree)
                    tree.setHeader(hdr)
                tree.setItemDelegate(_FileDetailsDelegate(tree))

            list_v = self.findChild(QListView, "listView")
            if list_v is not None:
                list_v.setItemDelegate(_FileDetailsDelegate(list_v))

            sidebar = self.findChild(QListView, "sidebar")
            if sidebar is not None:
                sidebar.setItemDelegate(_SidebarItemDelegate(sidebar))

            combo = self.findChild(QComboBox, "lookInCombo")
            if combo is not None:
                combo.setItemDelegate(_SidebarItemDelegate(combo))

            # Toolbuttons tooltips
            btn_tooltips = {
                "backButton": t("file_picker.back"),
                "forwardButton": t("file_picker.forward"),
                "toParentButton": t("file_picker.parent_directory"),
                "newFolderButton": t("file_picker.new_folder"),
                "listModeButton": t("file_picker.list_view"),
                "detailModeButton": t("file_picker.detail_view"),
            }
            for btn_name, tip in btn_tooltips.items():
                b = self.findChild(QToolButton, btn_name)
                if b is not None:
                    b.setToolTip(tip)

            # Actions text
            action_texts = {
                "qt_rename_action": t("file_picker.rename"),
                "qt_delete_action": t("file_picker.delete"),
                "qt_show_hidden_action": t("file_picker.show_hidden"),
                "qt_new_folder_action": t("file_picker.new_folder"),
                "qt_goto_parent_action": t("file_picker.parent_directory"),
            }
            for act_name, txt in action_texts.items():
                a = self.findChild(QAction, act_name)
                if a is not None:
                    a.setText(txt)
                    a.setToolTip(txt)

            for act in self.findChildren(QAction):
                txt = act.text().replace('&', '').strip().lower()
                if txt in ("remove", "delete from sidebar", "rimuovi"):
                    act.setText(t("file_picker.remove_from_sidebar"))
                    act.setToolTip(t("file_picker.remove_from_sidebar"))
        except Exception:
            pass

    # ── Sidebar places ────────────────────────────────────────────────────────

    def _add_common_places(self):
        """Desktop and Home in the sidebar."""
        urls = list(self.sidebarUrls())
        for location in (QStandardPaths.StandardLocation.DesktopLocation,
                         QStandardPaths.StandardLocation.HomeLocation):
            path = QStandardPaths.writableLocation(location)
            if path:
                url = QUrl.fromLocalFile(path)
                if url not in urls:
                    urls.append(url)
        self.setSidebarUrls(urls)
        self._system_sidebar_urls = [u for u in self.sidebarUrls() if _is_system_pin(u)]

    def _load_pinned_places(self):
        """Re-apply the user's pinned folders onto the sidebar."""
        urls = list(self.sidebarUrls())
        for p in _load_sidebar_pins():
            url = QUrl.fromLocalFile(p)
            if url not in urls:
                urls.append(url)
        self.setSidebarUrls(urls)

    def _hook_folder_context_menu(self):
        """Right-click on a FOLDER in the listing offers to pin it into the sidebar."""
        from PySide6.QtWidgets import QListView, QTreeView
        self._pins_views = []
        for view_type in (QListView, QTreeView):
            for v in self.findChildren(view_type):
                if v.objectName() in ("listView", "treeView"):
                    self._pins_views.append(v)
                    v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    v.customContextMenuRequested.connect(
                        lambda pos, target=v: self._on_file_view_context_menu(pos, target))
        self._sidebar_view = self.findChild(QListView, "sidebar")
        if self._sidebar_view is not None:
            sw = scaled(185, self, min_px=165)
            self._sidebar_view.setMinimumWidth(sw)
            self._sidebar_view.setMaximumWidth(scaled(280, self))
            self._sidebar_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._sidebar_view.customContextMenuRequested.connect(self._on_sidebar_context_menu)
            self._sidebar_view.installEventFilter(self)

    def _on_sidebar_context_menu(self, pos) -> None:
        """Right-click on the sidebar: protect system pins, allow removing custom pins."""
        sidebar = getattr(self, "_sidebar_view", None)
        if sidebar is None:
            return
        idx = sidebar.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        urls = list(self.sidebarUrls())
        if row >= len(urls):
            return
        url = urls[row]
        system_list = getattr(self, "_system_sidebar_urls", [])
        global_pos = sidebar.mapToGlobal(pos)
        if _is_system_pin(url, system_list):
            from PySide6.QtWidgets import QMenu
            menu = QMenu(sidebar)
            act = menu.addAction(t("file_picker.cannot_remove_system_pin"))
            act.setEnabled(False)
            menu.exec(global_pos)
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(sidebar)
        act = menu.addAction(t("file_picker.remove_from_sidebar"))
        chosen = menu.exec(global_pos)
        if chosen is act:
            urls.pop(row)
            self.setSidebarUrls(urls)
            customs = [u.toLocalFile() for u in urls
                       if not _is_system_pin(u, system_list) and u.isLocalFile() and u.toLocalFile()]
            _save_sidebar_pins(customs)

    def _on_file_view_context_menu(self, pos, view) -> None:
        """Right-click in file list/tree view: add pin action + standard localized actions."""
        from PySide6.QtWidgets import QFileSystemModel, QMenu
        from PySide6.QtGui import QAction
        idx = view.indexAt(pos)
        model = self.findChild(QFileSystemModel) or view.model()
        global_pos = view.mapToGlobal(pos)

        new_folder_act = self.findChild(QAction, "qt_new_folder_action")
        rename_act = self.findChild(QAction, "qt_rename_action")
        delete_act = self.findChild(QAction, "qt_delete_action")
        hidden_act = self.findChild(QAction, "qt_show_hidden_action")

        menu = QMenu(view)

        if idx.isValid() and model is not None and model.isDir(idx):
            path = model.filePath(idx)
            if path and Path(path).is_dir():
                pins = _load_sidebar_pins()
                pinned = path in pins
                pin_act = menu.addAction(
                    t("file_picker.remove_from_sidebar" if pinned
                      else "file_picker.add_to_sidebar"))
                menu.addSeparator()
                if new_folder_act:
                    menu.addAction(new_folder_act)
                if rename_act:
                    menu.addAction(rename_act)
                if delete_act:
                    menu.addAction(delete_act)
                if hidden_act:
                    menu.addSeparator()
                    menu.addAction(hidden_act)
                chosen = menu.exec(global_pos)
                if chosen is pin_act:
                    if pinned:
                        pins.remove(path)
                    else:
                        if path not in pins:
                            pins.append(path)
                    _save_sidebar_pins(pins)
                    self._load_pinned_places()
                return
        elif idx.isValid():
            if rename_act:
                menu.addAction(rename_act)
            if delete_act:
                menu.addAction(delete_act)
            if hidden_act:
                menu.addSeparator()
                menu.addAction(hidden_act)
            menu.exec(global_pos)
            return
        else:
            if new_folder_act:
                menu.addAction(new_folder_act)
            if hidden_act:
                menu.addSeparator()
                menu.addAction(hidden_act)
            menu.exec(global_pos)
            return


    def done(self, result):
        """Persist sidebar state, but NEVER persist the removal of a system pin.

        Qt's own sidebar context menu can remove any URL including the system
        ones (Desktop, Home, Drives). Re-adding them before saving ensures they
        are always there on the next open, regardless of what the user did.
        """
        try:
            current_urls = set(self.sidebarUrls())
            system_urls = getattr(self, "_system_sidebar_urls", [])
            # Restore any system URLs the user may have removed via Qt's menu.
            restored = list(current_urls | set(system_urls))
            self.setSidebarUrls(restored)
            # Persist only custom (non-system) URLs as user pins.
            customs = [u.toLocalFile() for u in restored
                       if not _is_system_pin(u, system_urls) and u.isLocalFile()
                       and u.toLocalFile()]
            _save_sidebar_pins(customs)
        except Exception:
            logger.debug("could not persist sidebar pins on close", exc_info=True)
        super().done(result)


    # ── .lnk redirect ─────────────────────────────────────────────────────────

    def _redirect_lnk_directory(self, path: str):
        if not path.lower().endswith('.lnk'):
            return

        from core.resolvers import resolve_lnk_target
        target = resolve_lnk_target(path).strip().strip('"')
        if not (target and target != path and Path(target).is_dir()):
            # Unresolvable link-as-directory: bounce back to its folder
            # instead of stranding the view on an empty file-root.
            target = str(Path(path).parent)

        def _go(t=target):
            try:
                self.setDirectory(t)
                self._clear_name_box()
            except RuntimeError:
                pass
        QTimer.singleShot(0, _go)

    def _clear_name_box(self):
        try:
            from PySide6.QtWidgets import QLineEdit
            edit = self.findChild(QLineEdit, "fileNameEdit")
            if edit is not None:
                edit.clear()
        except RuntimeError:
            pass

    def accept(self):
        files = self.selectedFiles()
        if len(files) == 1 and files[0].lower().endswith('.lnk'):
            from core.resolvers import resolve_lnk_target
            target = resolve_lnk_target(files[0]).strip().strip('"')
            if target and target != files[0] and Path(target).is_dir():
                self.setDirectory(target)
                # The filename box still holds "Folder.lnk" after the hop —
                # clear it immediately AND on the next loop turn: the
                # click/selection handlers that invoked accept() may still
                # write the old name back after we return.
                self._clear_name_box()
                QTimer.singleShot(0, self._clear_name_box)
                return
        super().accept()


class ExePickerDialog(_LnkAwareDialog):
    """Pick an executable; folder shortcuts navigate, app shortcuts return."""

    def __init__(self, parent, caption: str, name_filter: str):
        super().__init__(parent, caption)
        self.setFileMode(QFileDialog.FileMode.ExistingFile)
        self.setNameFilter(name_filter)


class FolderPickerDialog(_LnkAwareDialog):
    """Pick a directory, with the same shortcut behaviour."""

    def __init__(self, parent, caption: str):
        super().__init__(parent, caption)
        self.setFileMode(QFileDialog.FileMode.Directory)
        self.setOption(QFileDialog.Option.ShowDirsOnly, True)
        # "Seleziona" is more appropriate than "Apri" for folder selection.
        try:
            self.setLabelText(QFileDialog.DialogLabel.Accept,
                              t("file_picker.select"))
        except Exception:
            pass


class SavePathPickerDialog(_LnkAwareDialog):
    """Pick a save-file path that may be either a folder OR a file.

    Used by the manual-path section of Add Game / Edit when the user
    wants to point at a specific save file rather than a directory. The
    "Files of type" filter has two entries:

      - Cartella (Any directory)
      - Tutti i file (*.*)

    Accepting a directory returns the directory path; accepting a file
    returns the file path.
    """

    def __init__(self, parent, caption: str, mode: str = "folder"):
        """*mode* is ``"folder"`` (directories only) or ``"file"`` (any file)."""
        super().__init__(parent, caption)
        if mode == "file":
            self.setFileMode(QFileDialog.FileMode.ExistingFile)
            self.setNameFilters([
                t("file_picker.filter_all_files"),   # Tutti i file (*.*)
            ])
        else:
            self.setFileMode(QFileDialog.FileMode.Directory)
            self.setOption(QFileDialog.Option.ShowDirsOnly, False)
            self.setNameFilters([
                t("file_picker.filter_folders"),      # Cartella
                t("file_picker.filter_all_files"),
            ])
        try:
            self.setLabelText(QFileDialog.DialogLabel.Accept,
                              t("file_picker.select"))
        except Exception:
            pass


def pick_executable(parent, caption: str, name_filter: str = "",
                    start_dir: str = "") -> str:
    """Shared executable picker. Returns "" when cancelled."""
    if not name_filter:
        from core.resolvers import executable_name_filter
        name_filter = executable_name_filter()
    dialog = ExePickerDialog(parent, caption, name_filter)
    if start_dir:
        try:
            if Path(start_dir).is_dir():
                dialog.setDirectory(start_dir)
        except OSError:
            pass
    if dialog.exec() != QFileDialog.DialogCode.Accepted:
        return ""
    selected = dialog.selectedFiles()
    return selected[0] if selected else ""


def pick_folder(parent, caption: str, start_dir: str = "") -> str:
    """Shared folder picker. Returns "" when cancelled."""
    dialog = FolderPickerDialog(parent, caption)
    if start_dir:
        try:
            if Path(start_dir).is_dir():
                dialog.setDirectory(start_dir)
        except OSError:
            pass
    if dialog.exec() != QFileDialog.DialogCode.Accepted:
        return ""
    selected = dialog.selectedFiles()
    if not selected:
        return ""
    chosen = selected[0]
    # A folder shortcut selected outright (rather than entered) still has to
    # come back as its target.
    if chosen.lower().endswith('.lnk'):
        from core.resolvers import resolve_lnk_target
        target = resolve_lnk_target(chosen).strip().strip('"')
        if target and Path(target).is_dir():
            return target
    return chosen


def pick_save_path_entry(parent, caption: str, mode: str = "folder",
                         start_dir: str = "") -> str:
    """Pick a save-file path (folder or file) for Add Game / Edit.

    *mode* is ``"folder"`` (default) or ``"file"``.  Returns ``""`` when
    cancelled.
    """
    return _run_picker(SavePathPickerDialog(parent, caption, mode), start_dir)


class FilePickerDialog(_LnkAwareDialog):
    """Pick any existing file, with the same shortcut behaviour."""

    def __init__(self, parent, caption: str, name_filter: str):
        super().__init__(parent, caption)
        self.setFileMode(QFileDialog.FileMode.ExistingFile)
        if name_filter:
            self.setNameFilter(name_filter)


class SavePickerDialog(_LnkAwareDialog):
    """Choose where to save a new file."""

    def __init__(self, parent, caption: str, name_filter: str):
        super().__init__(parent, caption)
        self.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        self.setFileMode(QFileDialog.FileMode.AnyFile)
        # Explicit rather than relying on the default: picking a name that
        # already exists must ask before it replaces someone's file.
        self.setOption(QFileDialog.Option.DontConfirmOverwrite, False)
        if name_filter:
            self.setNameFilter(name_filter)
        try:
            self.setLabelText(QFileDialog.DialogLabel.Accept,
                              t("file_picker.save"))
        except Exception:
            pass


def _run_picker(dialog, start_dir: str) -> str:
    if start_dir:
        try:
            if Path(start_dir).is_dir():
                dialog.setDirectory(start_dir)
        except OSError:
            pass
    if dialog.exec() != QFileDialog.DialogCode.Accepted:
        return ""
    selected = dialog.selectedFiles()
    return selected[0] if selected else ""


def pick_file(parent, caption: str, name_filter: str = "",
              start_dir: str = "") -> str:
    """Shared any-file picker. Returns "" when cancelled."""
    return _run_picker(FilePickerDialog(parent, caption, name_filter), start_dir)


def pick_save_path(parent, caption: str, name_filter: str = "",
                   default_name: str = "", start_dir: str = "") -> str:
    """Shared "save as" picker. Returns "" when cancelled."""
    dialog = SavePickerDialog(parent, caption, name_filter)
    if default_name:
        dialog.selectFile(default_name)
        # Typing a bare name must not produce an extension-less file: it would
        # then be filtered out of the very dialog used to open it again.
        suffix = Path(default_name).suffix.lstrip(".")
        if suffix:
            dialog.setDefaultSuffix(suffix)
    return _run_picker(dialog, start_dir)
