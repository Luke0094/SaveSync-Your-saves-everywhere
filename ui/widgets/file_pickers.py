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
import logging
from pathlib import Path

from PySide6.QtCore import QTimer, QStandardPaths, QUrl
from PySide6.QtWidgets import QFileDialog

logger = logging.getLogger(__name__)


class _LnkAwareDialog(QFileDialog):
    """QFileDialog that treats a folder shortcut as a navigation step."""

    def __init__(self, parent, caption: str):
        super().__init__(parent, caption)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        # Hand back the RAW .lnk path: the widget dialog otherwise resolves
        # shortcuts itself, and callers need the .lnk (display name from the
        # shortcut filename, its folder as a search hint).
        self.setOption(QFileDialog.Option.DontResolveSymlinks, True)
        self._add_common_places()
        # Qt treats .lnk files as symlinks, so double-clicking a FOLDER
        # shortcut makes isDir() true and the dialog "enters" the .lnk FILE
        # itself — Look-in shows "...\Folder.lnk", the listing is empty, and
        # accept() is never reached. rootPathChanged fires for EVERY
        # directory change (directoryEntered only covers UI navigation), so
        # hook that and redirect on the next loop turn.
        from PySide6.QtWidgets import QFileSystemModel
        model = self.findChild(QFileSystemModel)
        if model is not None:
            model.rootPathChanged.connect(self._redirect_lnk_directory)

    def _add_common_places(self):
        """Desktop and Home in the sidebar — the widget dialog ships only
        Home, and game shortcuts overwhelmingly live on the Desktop."""
        urls = list(self.sidebarUrls())
        for location in (QStandardPaths.StandardLocation.DesktopLocation,
                         QStandardPaths.StandardLocation.HomeLocation):
            path = QStandardPaths.writableLocation(location)
            if path:
                url = QUrl.fromLocalFile(path)
                if url not in urls:
                    urls.append(url)
        self.setSidebarUrls(urls)

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
