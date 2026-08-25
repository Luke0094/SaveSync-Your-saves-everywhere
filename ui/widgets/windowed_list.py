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
    # The tallest the spacers are allowed to make the holder.
    #
    # Widget coordinates on Windows are 16-bit: nothing can be laid out past
    # 32767 px. The spacers stand in for EVERY row not built, so a list of
    # 570 folders at 210 px wants 119 700 — three and a half times the
    # limit. The platform clamps the holder and the layout carries on
    # placing rows at their real offsets, so everything below the clamp is
    # drawn where its clicks never arrive: rows that are visible, and dead.
    #     Unable to set geometry 1028x116130 ... Resulting: 1028x32767
    # Under this cap a spacer pixel is a row pixel and scrolling is 1:1;
    # over it, one spacer pixel stands for more than one row. Room is left
    # for the built rows themselves, which keep their real height.
    # The platform's own ceiling, in DEVICE pixels. Widget coordinates on
    # Windows are 16-bit, and the conversion from logical to device happens
    # below Qt's API: on a 4K screen at 150% every logical pixel is 1.5
    # device pixels, so a holder measuring 26 380 logical arrives as 39 570
    # and is clamped. A fixed LOGICAL cap looked safe and was not — the
    # holder never even reached it.
    WL_DEVICE_LIMIT_PX = 32767
    # Headroom under that ceiling: the platform also has to fit the frame,
    # and a rounding error here costs the bottom of the list.
    WL_DEVICE_SAFETY_PX = 1500
    # A viewport is at most a screen tall. With setWidgetResizable(True) a
    # scroll area that has not been laid out yet can report a viewport as
    # tall as its CONTENT, and every size derived from it then explodes: a
    # reserve computed from a 8 856px "viewport" put the holder at 35 796
    # and straight back over the limit the cap exists to stay under.
    WL_MAX_VIEWPORT_PX = 4000

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

    def _wl_max_content_px(self) -> int:
        """The tallest the holder may be, in LOGICAL pixels on this screen.

        Derived from the device limit and the screen's pixel ratio rather
        than fixed, because the two differ by exactly that ratio and it is
        the DEVICE number the platform enforces.
        """
        dpr = 1.0
        try:
            scroll = getattr(self, "_wl_scroll", None)
            if scroll is not None:
                dpr = float(scroll.devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        dpr = max(1.0, dpr)
        usable = self.WL_DEVICE_LIMIT_PX - self.WL_DEVICE_SAFETY_PX
        return max(4000, int(usable / dpr))

    def _wl_vstep(self, count: int, step: int, budget: int = 0) -> float:
        """Spacer height standing in for ONE un-built row.

        The real step whenever the whole list fits under WL_MAX_VIRTUAL_PX —
        which is every ordinary list, so nothing changes for them. Past it,
        the list is represented proportionally instead: the scrollbar still
        reaches every row, it just covers more of them per pixel. Coarser
        scrolling on a very long list is the price; the alternative is a
        list whose lower two thirds cannot be touched.
        """
        if count <= 0:
            return float(step)
        if budget <= 0:
            budget = max(step, self._wl_max_content_px() // 2)
        total = count * step
        if total <= budget:
            return float(step)
        return max(1.0, budget / float(count))

    def wl_scroll_value_for_row(self, row: int) -> int:
        """The scrollbar value that puts *row* at the top of the viewport.

        The inverse of the mapping _wl_update reads, and the only honest way
        to ask for a row by number: under the cap a scroll value is a pixel
        offset, over it it is a proportion, and the caller should not have
        to know which.
        """
        kept = getattr(self, "_wl_kept", None) or []
        n = len(kept)
        if n <= 0:
            return 0
        layout = getattr(self, "_wl_layout", None)
        spacing = layout.spacing() if layout is not None else 0
        row_h = max(1, int(self._wl_row_height() or 0))
        step = row_h + spacing
        viewport = max(1, min(int(self._wl_scroll.viewport().height()),
                              self.WL_MAX_VIEWPORT_PX))
        fit = max(1, int((viewport + spacing) // step))
        window_rows = fit + 2 + 2 * self.WL_BUFFER
        max_content = self._wl_max_content_px()
        reserve = window_rows * step
        budget = max(step, max_content - reserve)
        vstep = self._wl_vstep(n, step, budget)
        row = max(0, min(n - 1, int(row)))
        if vstep >= step:
            return row * step
        content = min(max_content, int(round(n * vstep)) + reserve)
        span = max(1, content - viewport)
        frac = row / float(max(1, n - fit))
        return int(round(min(1.0, frac) * span))

    def _wl_render(self, scroll, empty_label=None) -> None:
        """Put a fresh, empty window into *scroll* and fill its first screen."""
        self._wl_sync_visible()
        self._wl_kept = self._wl_entries()

        # Parented from the start, every one of them. A QWidget built with no
        # parent is a TOP-LEVEL widget until something reparents it, and Qt
        # will give it a native window if it is touched in the meantime —
        # which these are, immediately, by setFixedHeight. A window carries
        # the platform's coordinate limits: on Windows 32767, while a spacer
        # standing in for a thousand rows is 100 000 px tall. The result was
        # a log full of
        #     QWindowsWindow::setGeometry: Unable to set geometry
        #     1002x101010 ... on QWidgetWindow/"transparent_bgWindow"
        # once per relayout, for a widget that was never meant to be a window
        # at all. Inside the scroll area's viewport it is just a child and
        # the limit does not apply.
        holder = QWidget(scroll)
        holder.setObjectName("transparent_bg")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._wl_empty = empty_label
        if empty_label is not None:
            empty_label.setParent(holder)
            layout.addWidget(empty_label)
            empty_label.setVisible(not self._wl_kept)

        self._wl_top_pad = QWidget(holder)
        self._wl_top_pad.setObjectName("transparent_bg")
        self._wl_top_pad.setFixedHeight(0)
        layout.addWidget(self._wl_top_pad)
        self._wl_bottom_pad = QWidget(holder)
        self._wl_bottom_pad.setObjectName("transparent_bg")
        self._wl_bottom_pad.setFixedHeight(0)
        layout.addWidget(self._wl_bottom_pad)
        layout.addStretch()

        self._rows = []
        self._wl_built = {}          # position in _wl_kept -> row widget
        self._wl_layout = layout
        self._wl_scroll = scroll
        self._wl_window = (0, 0)

        # Never leaves the previous holder unparented — see the helper.
        from ui.helpers import swap_scroll_widget
        swap_scroll_widget(scroll, holder)

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

        # A row height of zero would make every arithmetic below divide the
        # list into infinitely many rows, and setFixedHeight(0) below would
        # collapse every row it built into an invisible, unclickable strip.
        # A host that cannot measure a row yet is told to try again rather
        # than allowed to draw a broken window.
        row_h = max(1, int(self._wl_row_height() or 0))
        step = row_h + layout.spacing()
        viewport = max(1, min(int(self._wl_scroll.viewport().height()),
                              self.WL_MAX_VIEWPORT_PX))
        top = self._wl_scroll.verticalScrollBar().value()

        # Rows are built at their REAL height; only the spacers are scaled.
        # So "how many rows fit on screen" stays a question about step, while
        # where the scroll IS becomes a question of proportion.
        n = len(kept)
        # How many rows genuinely FIT — the last row of a block carries no
        # trailing spacing, hence the +spacing. on_screen adds slack for the
        # partial rows at either edge and is what gets BUILT; fit is what the
        # viewport can actually hold and is what the end of the list is
        # measured against. Using on_screen for both put the final screenful
        # two rows too high, and those two could not be scrolled to.
        fit = max(1, int((viewport + layout.spacing()) // step))
        on_screen = fit + 2
        window_rows = on_screen + 2 * self.WL_BUFFER

        # The built block keeps its REAL height, so it comes off the ceiling
        # before the spacers get what is left. Computed in this order the
        # total cannot exceed the ceiling however tall the rows are.
        max_content = self._wl_max_content_px()
        reserve = window_rows * step
        budget = max(step, max_content - reserve)
        vstep = self._wl_vstep(n, step, budget)

        # The content height the holder is asked to be. Constant for a given
        # list, whatever is scrolled to: the room for the built block is
        # always reserved, so the scrollbar's range does not shift under the
        # user's hand as rows come and go.
        compressed = vstep < step
        if compressed:
            # Room for the built block is reserved on top of the compressed
            # list, so the scrollbar's range does not shift under the user's
            # hand as rows come and go. Only when compressed: for a list
            # that fits, the block IS the rows at their real height and a
            # reserve would be scrollable emptiness at the end.
            content = min(max_content, int(round(n * vstep)) + reserve)
            span = max(1, content - viewport)
            # Which row belongs at the top, read as a FRACTION of the
            # scroll. Dividing the scroll value by vstep looked simpler and
            # could not reach the end: the built block is taller than the
            # spacer height it displaces, so the arithmetic ran out of
            # scrollbar before it ran out of rows and the last few dozen
            # rows could not be scrolled to at all.
            frac = min(1.0, max(0.0, float(top) / float(span)))
            need_first = int(round(frac * max(0, n - fit)))
        else:
            content = n * step
            need_first = max(0, min(max(0, n - 1), int(top // step)))
        need_last = min(n, need_first + on_screen)
        built_first, built_last = self._wl_window

        # The spacers follow the scroll on EVERY pass, rebuild or not: they
        # are two setFixedHeight calls, and without them the block stays
        # where the last rebuild left it while the view moves on.
        holder = layout.parentWidget()

        def _place(w_first: int, w_last: int) -> None:
            count = max(0, w_last - w_first)
            block = count * row_h + max(0, count - 1) * layout.spacing()
            if holder is not None:
                # The holder's height is PINNED when compressed, not left to
                # the sum of its children. Derived from them it changed
                # every time the built block did — a few rows more at the
                # top of the list than at the bottom — so the scrollbar's
                # range moved as the user scrolled, and the value needed to
                # reach the last rows was clamped away before it could be
                # set: the end of the list became a fixed point the scroll
                # could not pass.
                try:
                    if compressed:
                        holder.setFixedHeight(content)
                    else:
                        holder.setMinimumHeight(0)
                        holder.setMaximumHeight(16777215)
                except RuntimeError:
                    pass
            if compressed:
                # Put the block so the row that belongs at the top IS at the
                # top — the spacers no longer measure rows, so the block has
                # to be aimed at the scroll position rather than derived
                # from an index.
                #
                # head + block + tail + the layout's own gaps must come to
                # the SAME total wherever the list is scrolled, or the
                # holder changes height as it scrolls, the scrollbar's range
                # changes with it, and the value needed to reach the end is
                # clamped away before it can ever be set — the end of the
                # list becomes a fixed point it cannot pass.
                gaps = layout.spacing() * (count + 1)
                room = max(0, content - block - gaps)
                head = min(room,
                           max(0, int(top) - max(0, need_first - w_first) * step))
                tail = max(0, room - head)
            else:
                head = w_first * step
                tail = max(0, (n - w_last) * step)
            self._wl_top_pad.setFixedHeight(head)
            self._wl_bottom_pad.setFixedHeight(tail)

        # ...and if what is built still covers what is needed, placing it is
        # all there was to do. The buffer exists to be spent.
        if not force and built_first <= need_first and need_last <= built_last:
            _place(built_first, built_last)
            return

        first = max(0, need_first - self.WL_BUFFER)
        last = min(n, first + window_rows)
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
        rate_limit = False
        if not force and not overlaps:
            settle = getattr(self, "_wl_settle", None)
            if settle is not None:
                # The timer RATE-LIMITS the draws; it does not replace the
                # first one. Deferring every jump — which is what this used
                # to do — left the viewport over a region with no rows built
                # in it for as long as the movement lasted: bands of bare
                # background, and a field clicked in one of them was not
                # there to receive it. Restarting the timer on each scroll
                # event made that last the whole drag, because an event
                # every few milliseconds pushed the deadline back forever.
                #
                # So: nothing pending → draw this one NOW and arm the timer.
                # Something pending → the movement is still going, skip this
                # position and let the armed draw land. Either way there is
                # at most one rebuild per WL_SETTLE_MS, and the user always
                # has rows under the cursor.
                if settle.isActive():
                    return
                rate_limit = True

        settle = getattr(self, "_wl_settle", None)
        if settle is not None and not rate_limit:
            # A draw that keeps up on its own needs no follow-up.
            settle.stop()

        wanted = range(first, last)
        built = self._wl_built

        # Rows leaving the window: sync, then drop.
        #
        # hide() + deleteLater(), never setParent(None). A row scrolled out
        # of view sits deep inside the holder — y = 38 442 on a list of 570 —
        # and setParent(None) makes it a TOP-LEVEL widget that KEEPS that
        # position. Qt then asks the platform for a window there, which
        # Windows refuses:
        #     Unable to set geometry 1028x198+0+38442 on
        #     QWidgetWindow/"QWidgetClassWindow" ... Resulting: +0+32767
        # Once per row per scroll. Removing it from the layout takes it off
        # screen just as well, and deleteLater does the rest — while it
        # still has a parent, so it is never a window at any point.
        for key in [k for k in built if k not in wanted]:
            row = built.pop(key)
            try:
                self._wl_sync_row(row)
                row.hide()
                layout.removeWidget(row)
                row.deleteLater()
            except RuntimeError:
                pass

        _place(first, last)

        # Rows entering it: build only those. Everything already on screen is
        # left exactly as it is, cursor and half-typed edit included.
        for pos in wanted:
            if pos in built:
                continue
            key, entry = kept[pos]
            try:
                row = self._wl_make_row(key, entry)
            except Exception:
                logger.debug("could not build list row %s", pos, exc_info=True)
                continue
            # Same rule as the spacers above: never size a widget that has
            # no parent. insertWidget below would adopt it anyway, but by
            # then setFixedHeight has already touched a top-level widget —
            # which Qt may back with a real window, shown for a frame.
            if row.parent() is None:
                row.setParent(layout.parentWidget())
            row.setFixedHeight(row_h)

            # WHERE it goes is read off the layout, never counted up from
            # the built dict. The count assumed every key in `built` has a
            # widget in this layout, and on the way down — a panel closing,
            # a holder being replaced — that stops being true: Qt empties
            # the layout while the dict still holds the keys, and the sum
            # runs past the end.
            #     QBoxLayout::insert: index 12 out of range (max: 6)
            # Before the next row already on screen, or before the bottom
            # spacer when this is the last one. Both are positions the
            # layout itself reports, so neither can be out of range.
            anchor = None
            for k in sorted(built):
                if k > pos:
                    anchor = built[k]
                    break
            index = layout.indexOf(anchor) if anchor is not None else -1
            if index < 0:
                index = layout.indexOf(self._wl_bottom_pad)
            if index < 0:
                index = layout.count()
            built[pos] = row
            layout.insertWidget(index, row)

        self._rows = [built[k] for k in sorted(built)]
        self._wl_window = (first, last)
        if self._wl_empty is not None:
            self._wl_empty.setVisible(False)

        # Armed AFTER the draw, never before: started earlier it would be
        # cancelled by the stop() above on this very pass. While it runs,
        # further jumps are skipped; when it fires it draws wherever the
        # view has got to, which is the settled position if the user has
        # stopped and the next frame of the drag if they have not.
        if rate_limit and settle is not None:
            settle.start()

    def _wl_clear(self) -> None:
        """Forget the window and every row in it."""
        layout = getattr(self, "_wl_layout", None)
        for row in list((getattr(self, "_wl_built", None) or {}).values()):
            try:
                # Same rule as the drop above: hidden and removed from the
                # layout, never unparented — see the note there.
                row.hide()
                if layout is not None:
                    layout.removeWidget(row)
                row.deleteLater()
            except RuntimeError:
                pass
        self._wl_built = {}
        self._rows = []
        self._wl_kept = []
        self._wl_window = (0, 0)
