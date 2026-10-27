"""SaveSync - "send a file or folder" as one step for the callers: pick it with
the app's own picker (or take what was dropped), let the person untick what
should not leave, then open the send window. Used by the Receive panel (token
pasted by the other person) and by the Friends panel (token a friend sent: the
send starts at once)."""
from pathlib import Path

from PySide6.QtWidgets import QDialog

from i18n import t


def send_files_flow(parent, token: str = "", paths=None) -> bool:
    """Returns True when the send window was shown. With *token* the window
    starts sending as soon as it opens. *paths* are files and folders already
    chosen (dropped, say): the picker is skipped and they are all offered."""
    from ui.dialogs.p2p_send_confirm_dialog import P2pSendConfirmDialog
    if paths:
        sources = [Path(p) for p in paths]
    else:
        # One panel for the choice - files, a folder, or both, by hand or by dropping them
        from ui.dialogs.p2p_add_files_dialog import pick_paths
        picked = pick_paths(parent, t("p2p.send_file_pick"))
        if not picked:
            return False
        sources = [Path(p) for p in picked]
    # What is sent may have nothing to do with saves and goes as it is: the
    # person sees every file and ticks off what should not leave.
    source = sources[0]
    confirm = P2pSendConfirmDialog(sources if len(sources) > 1 else source, parent)
    if confirm.exec() != QDialog.DialogCode.Accepted:
        return False
    files = confirm.selected_files()
    if not files:
        return False
    folder = source.name if source.is_dir() else source.parent.name
    if len(files) == 1:
        # One file left ticked, even inside a picked folder: it goes out under
        # its own name, with the folder shown on its own line beside it.
        label = Path(files[0][1]).name
    elif len(sources) == 1:
        label = source.name
    else:
        label = t("p2p.send_confirm_items", count=len(sources))
    # Through the main window when there is one: its send window can be put away
    # in the sidebar and outlives whichever panel started it.
    window = parent.window() if parent is not None else None
    opener = getattr(window, "_open_p2p_send_dialog", None)
    if callable(opener):
        opener(None, files=files, label=label, folder_name=folder,
               token=token, auto_start=bool(token))
        return True
    from ui.dialogs.p2p_send_dialog import P2pSendDialog
    P2pSendDialog(None, parent, files=files, label=label, folder_name=folder,
                  token=token, auto_start=bool(token)).exec()
    return True
