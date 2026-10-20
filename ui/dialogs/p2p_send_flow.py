"""SaveSync - "send a file or folder" as one step for the callers: pick it with
the app's own picker, let the person untick what should not leave, then open
the send dialog. Used by the Receive panel (token pasted by the other person)
and by the Friends panel (token a friend sent: the send starts at once)."""
from pathlib import Path

from PySide6.QtWidgets import QDialog

from i18n import t


def send_files_flow(parent, token: str = "") -> bool:
    """Returns True when the send dialog was shown. With *token* the dialog
    starts sending as soon as it opens."""
    from ui.widgets.file_pickers import pick_file_or_folder
    path = pick_file_or_folder(parent, t("p2p.send_file_pick"))
    if not path:
        return False
    # What is sent may have nothing to do with saves and goes as it is: the
    # person sees every file and ticks off what should not leave.
    from ui.dialogs.p2p_send_confirm_dialog import P2pSendConfirmDialog
    from ui.dialogs.p2p_send_dialog import P2pSendDialog
    source = Path(path)
    confirm = P2pSendConfirmDialog(source, parent)
    if confirm.exec() != QDialog.DialogCode.Accepted:
        return False
    files = confirm.selected_files()
    if not files:
        return False
    folder = source.name if source.is_dir() else source.parent.name
    P2pSendDialog(None, parent, files=files, label=source.name, folder_name=folder,
                  token=token, auto_start=bool(token)).exec()
    return True
