"""SaveSync — a long list that only builds the rows you can see.

Both bulk dialogs show a list that can run to several hundred entries, and a
row in either is half a dozen widgets with editable fields in it. Keeping one
alive per entry is what made those dialogs crawl: Qt lays out and paints every
row on a window resize whether it is on screen or not, so the cost grew with
the scan and no amount of building them faster removed it.

So the rows that exist are the ones in view, and two spacers stand in for
everything above and below at exactly the height those rows would have taken.
The scrollbar is therefore the size it should be and the list scrolls from end
to end — it is ONE continuous list, not pages.

**Rows are views, not the data.** The entry list is the truth; a row shows one
entry and writes edits back to it. That is what makes it safe to destroy a row
the moment it leaves the screen, and it is also the thing that goes wrong if
forgotten: anything reading the batch off the widgets sees only the visible
handful.

**Only the difference is rebuilt.** Scrolling quickly used to destroy and
recreate the whole window every time the view moved past its edge, which cost
more than the rows saved. Rows that are still wanted are kept exactly as they
are — including a half-typed edit and the cursor in it — and only the ones that
came into view are built.

A user mixes this in and provides ``_wl_entries()`` and ``_wl_make_row()``.
"""
import logging

from PySide6.QtWidgets import QVBoxLayout, QWidget

logger = logging.getLogger(__name__)


class WindowedListMixin:
    """Shows a window of rows over a much longer list of entries."""

    # Extra rows kept built above and below the viewport, so a scroll does
    # not arrive somewhere blank before the window catches up.
    WL_BUFFER = 6
    # How long the view must stop moving before a JUMP is drawn. Short enough
    # to feel immediate when the user lets go, long enough that a drag across
    # the whole scrollbar builds one window rather than a hundred.
    WL_SETTLE_MS = 45

    # ── what the host supplies ──────────────────────────────────────────────

    def _wl_entries(self) -> list:
        """``[(key, entry)]`` for everything still in the list, in order."""
        raise NotImplementedError

    def _wl_make_row(self, key, entry) -> QWidget:
        """Build the row widget showing *entry*."""
        raise NotImplementedError

    def _wl_sync_row(self, row) -> None:
        """Write a row's current state back to its entry. Optional."""

    # ── the machinery ───────────────────────────────────────────────────────

    def _wl_sync_visible(self) -> None:
        for row in list(getattr(self, "_rows", []) or []):
            try:
                if not row.isHidden():
                    self._wl_sync_row(row)
            except RuntimeError:
                continue

    def _wl_row_height(self) -> int:
        """Uniform row height, measured once from a real row.

        The arithmetic needs every row to be the same height, so they are
        given this one rather than each asking for its own.
        """
        raise NotImplementedError

    def _wl_render(self, scroll, empty_label=None) -> None:
        """Put a fresh, empty window into *scroll* and fill its first screen."""
        self._wl_sync_visible()
        self._wl_kept = self._wl_entries()

        holder = QWidget()
        holder.setObjectName("transparent_bg")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._wl_empty = empty_label
        if empty_label is not None:
            empty_label.setParent(None)
            layout.addWidget(empty_label)
            empty_label.setVisible(not self._wl_kept)

        self._wl_top_pad = QWidget()
        self._wl_top_pad.setObjectName("transparent_bg")
        self._wl_top_pad.setFixedHeight(0)
        layout.addWidget(self._wl_top_pad)
        self._wl_bottom_pad = QWidget()
        self._wl_bottom_pad.setObjectName("transparent_bg")
        self._wl_bottom_pad.setFixedHeight(0)
        layout.addWidget(self._wl_bottom_pad)
        layout.addStretch()

        self._rows = []
        self._wl_built = {}          # position in _wl_kept -> row widget
        self._wl_layout = layout
        self._wl_scroll = scroll
        self._wl_window = (0, 0)

        old = scroll.takeWidget()
        scroll.setWidget(holder)
        if old is not None:
            old.deleteLater()

        if not getattr(self, "_wl_hooked", False):
            # Connected once: the scrollbar belongs to the scroll area, which
            # outlives the holders swapped in and out of it.
            from PySide6.QtCore import QTimer
            self._wl_settle = QTimer(scroll)
            self._wl_settle.setSingleShot(True)
            self._wl_settle.setInterval(self.WL_SETTLE_MS)
            # force: this IS the settled position, so it must draw rather
            # than take the deferral branch again — which, without this,
            # deferred forever and left the view showing wherever it had
            # been before the jump.
            self._wl_settle.timeout.connect(lambda: self._wl_update(force=True))
            scroll.verticalScrollBar().valueChanged.connect(
                lambda _v: self._wl_update())
            self._wl_hooked = True
        self._wl_update(force=True)

    def _wl_update(self, force: bool = False) -> None:
        kept = getattr(self, "_wl_kept", None)
        if kept is None:
            return
        layout = self._wl_layout
        if not kept:
            self._wl_top_pad.setFixedHeight(0)
            self._wl_bottom_pad.setFixedHeight(0)
            return

        step = self._wl_row_height() + layout.spacing()
        viewport = max(1, self._wl_scroll.viewport().height())
        top = self._wl_scroll.verticalScrollBar().value()

        # What the viewport genuinely needs right now...
        need_first = max(0, top // step)
        need_last = min(len(kept), -(-(top + viewport) // step) + 1)
        built_first, built_last = self._wl_window
        # ...and if what is built still covers it, there is nothing to do.
        # The buffer exists to be spent: a rebuild only happens when the view
        # reaches the edge of what was built for it.
        if not force and built_first <= need_first and need_last <= built_last:
            return

        first = max(0, need_first - self.WL_BUFFER)
        last = min(len(kept), first + viewport // step + 2 * self.WL_BUFFER + 2)
        if not force and (first, last) == self._wl_window:
            return

        # A jump that lands clear of what is built — dragging the scrollbar,
        # or a page key — replaces the whole window rather than extending it,
        # and doing that for every position the drag passes through is work
        # nobody sees the result of. Those are left until the movement
        # settles; one rebuild at the place the user actually stopped.
        #
        # An ordinary wheel scroll DOES overlap, costs only the few rows that
        # came into view, and is done immediately — waiting there would show
        # a gap for no reason.
        overlaps = built_first < last and first < built_last
        if not force and not overlaps:
            settle = getattr(self, "_wl_settle", None)
            if settle is not None:
                settle.start()
                return

        settle = getattr(self, "_wl_settle", None)
        if settle is not None:
            settle.stop()

        wanted = range(first, last)
        built = self._wl_built

        # Rows leaving the window: sync, then drop.
        for key in [k for k in built if k not in wanted]:
            row = built.pop(key)
            try:
                self._wl_sync_row(row)
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass

        self._wl_top_pad.setFixedHeight(first * step)
        self._wl_bottom_pad.setFixedHeight(max(0, len(kept) - last) * step)

        # Rows entering it: build only those. Everything already on screen is
        # left exactly as it is, cursor and half-typed edit included.
        base = 1 if self._wl_empty is not None else 0     # empty label
        base += 1                                          # top pad
        for pos in wanted:
            if pos in built:
                continue
            key, entry = kept[pos]
            try:
                row = self._wl_make_row(key, entry)
            except Exception:
                logger.debug("could not build list row %s", pos, exc_info=True)
                continue
            row.setFixedHeight(self._wl_row_height())
            built[pos] = row
            layout.insertWidget(base + sum(1 for k in built if k < pos), row)

        self._rows = [built[k] for k in sorted(built)]
        self._wl_window = (first, last)
        if self._wl_empty is not None:
            self._wl_empty.setVisible(False)

    def _wl_clear(self) -> None:
        """Forget the window and every row in it."""
        for row in list((getattr(self, "_wl_built", None) or {}).values()):
            try:
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass
        self._wl_built = {}
        self._rows = []
        self._wl_kept = []
        self._wl_window = (0, 0)
