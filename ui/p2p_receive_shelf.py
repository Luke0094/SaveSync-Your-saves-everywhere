"""SaveSync - the sidebar's face of a download that goes on with its panel closed.

The receive session lives in ui.p2p_receive_service, not in the panel, so closing the panel
never stops it. This is what the sidebar's put-away entries (the same ones the send window
has) ask of it: whether something is happening, how far it has got - the overall percentage and
nothing else - what to say, and how to stop it. Clicking the entry opens the panel again, which
shows the same page as before (see ui.widgets.transfer_files_view).
"""
from i18n import t
from ui.p2p_receive_service import DOWNLOADING, DONE, OFFER


class P2pReceiveShelf:
    def __init__(self, service):
        self._svc = service

    def wanted(self) -> bool:
        """Is there something for the sidebar to show: files waiting for an answer, a download
        under way, or one that ended with files left out that can still be asked for
        (clicking opens the page)."""
        return self._svc.state in (OFFER, DOWNLOADING) or self._svc.resumable

    def shelve_attention(self) -> bool:
        """Is it waiting for the person (an offer nobody has answered)?"""
        return self._svc.state == OFFER

    # ── what the sidebar's entries ask of anything they stand for ────────

    def has_shelvable_work(self) -> bool:
        return self._svc.state == DOWNLOADING and not self._svc.cancelling

    def shelve_working(self) -> bool:
        return self.has_shelvable_work()

    def shelve_progress(self) -> tuple:
        if self._svc.state == DOWNLOADING:
            return (int(self._svc.pct), 100, "")
        return (0, 0, "")

    def shelve_nav_label(self) -> str:
        if self._svc.state == OFFER:
            return t("p2p.receive_shelved_offer")
        if self._svc.state == DOWNLOADING:
            return t("p2p.receive_shelved_pct", pct=int(self._svc.pct))
        return t("p2p.receive_shelved_done")

    def shelve_nav_tooltip(self) -> str:
        offer = self._svc.offer
        lines = [offer.filename] if offer is not None and offer.filename else []
        if self._svc.state == OFFER:
            lines.append(t("p2p.receive_waiting_decision"))
        if self._svc.state == DONE and self._svc.skipped_files:
            lines.append(t("p2p.receive_skipped_note", count=len(self._svc.skipped_files),
                           names=", ".join(self._svc.skipped_files[:5])))
        return "\n".join(lines)

    def cancel_background_work(self) -> None:
        self._svc.cancel_download()
