"""SaveSync - "Reset the friend list": the way back in when the lock password is
forgotten.

The lock keeps whoever sits down at this PC out of the list, so there is
deliberately no way past it that leaves the list in place: the way back is to
delete the list for good (ui.p2p_friends_service.reset_all) and start again.
That is asked for plainly, once, from two places - the lock screen of the Friends
window and Settings, which is never locked - and is the same for both.
"""
import logging

from PySide6.QtWidgets import QMessageBox

from core.p2p.friends import FriendError
from i18n import t

logger = logging.getLogger(__name__)


def reset_friend_list(parent, svc) -> bool:
    """Ask, then delete the friend list. True when it was deleted."""
    from ui.modal_helpers import information_window_modal, question_window_modal, warning_window_modal
    Yes, Cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
    reply = question_window_modal(
        parent, t("p2p.friends_reset_title"), t("p2p.friends_reset_ask"), Yes | Cancel,
        default_button=Cancel, button_texts={Yes: t("p2p.friends_reset_do")})
    if reply != Yes:
        return False
    try:
        result = svc.reset_all()
    except FriendError:
        logger.warning("P2P friends: the friend list could not be deleted", exc_info=True)
        warning_window_modal(parent, t("p2p.friends_reset_title"), t("p2p.friends_reset_failed"))
        return False
    text = t("p2p.friends_reset_done")
    if result.get("failed"):
        text += "\n\n" + t("p2p.friends_reset_leftover", count=result["failed"])
    information_window_modal(parent, t("p2p.friends_reset_title"), text)
    return True
