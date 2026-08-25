"""
SaveSync - Overview Page
Live dashboard: active game, library stats, recent backups, sync status.
Widget access is guarded via ui.helpers.safe_widget to avoid C++ deleted
object crashes.
"""
from datetime import datetime
import logging

import math

from PySide6.QtCore import Qt, QTimer, Signal, QRectF, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QSizePolicy, QToolTip
)

from i18n import t
from ui.helpers import PageScrollMixin, safe_widget as _safe, scaled
from ui.styles.theme import palette, ThemedMixin
from core.config_manager import get_config
from core.library import get_library
from core.backup import get_backup_manager
from core.monitor import get_monitor
from sync import get_orchestrator

logger = logging.getLogger(__name__)


class StatCard(QFrame, ThemedMixin):
    """A single overview stat tile.

    The accent is passed as a palette KEY (e.g. ``"accent"``, ``"info"``) — NOT a
    resolved hex — so the top-border and value colour re-theme in place on a
    light/dark switch. All palette-dependent styles are routed through
    ``self._sty`` so ``refresh_styles()`` re-applies them with the live palette.
    """

    def __init__(self, value: str, label: str, accent_key: str = "accent", card_key: str = ""):
        super().__init__()
        self._accent_key = accent_key or "accent"
        self.setFrameShape(QFrame.Shape.NoFrame)  # kill Fusion 3D frame
        obj_name = f"stat_card_{card_key}" if card_key else "stat_card"
        self.setObjectName(obj_name)
        self._sty(self, lambda: f"""
            QFrame#{obj_name} {{
                background: {palette('bg_card')};
                border: 1px solid {palette('border')};
                border-top: 2px solid {palette(self._accent_key)};
                border-radius: 8px;
            }}
        """)
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(2)
        self.setMinimumWidth(scaled(85, self, min_px=70))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._metrics_cache: tuple | None = None
        val_lbl = QLabel(value)
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        val_lbl.setWordWrap(False)
        self._val_lbl = val_lbl

        lbl = QLabel(label)
        lbl.setObjectName("stat_label")
        lbl.setWordWrap(True)
        self._layout.addWidget(val_lbl)
        self._layout.addWidget(lbl)
        self._lbl = lbl
        self._apply_metrics()

    # ── Responsive type ──────────────────────────────────────────────────
    # The tile stretches with the window, so its type has to stretch too, or
    # a wide overview shows the same small numbers in a much bigger box while
    # the donut beside it grows — which is the inconsistency this fixes.
    #
    # Everything is derived from WIDTH, never height. The layout decides the
    # width; the height follows the text, so sizing text from height would
    # feed back into itself and grow without end.
    _VALUE_SHARE = 0.09        # of the tile's width
    _LABEL_SHARE = 0.045
    _PAD_SHARE = 0.035
    _GROWTH_CAP = 1.6          # never more than this multiple of the floor

    def _metrics(self) -> tuple:
        """``(value px, label px, vertical padding)`` for the current width.

        The two font sizes are DESIGN-TIME, because that is what a stylesheet
        takes here: _sty runs every sheet through scale_stylesheet_fonts,
        which applies the UI scale itself. Handing it an already-scaled size
        applied the scale twice — the tile's own long-standing quirk, visible
        on any machine not at 100%. The padding is a real Qt call, so that
        one IS scaled.

        The floors are the sizes the tile has always had, so a narrow window
        looks exactly as it did; only the room above them is new.
        """
        from ui.helpers import ui_scale
        scale = max(0.1, float(ui_scale() or 1.0))
        w_design = max(1.0, self.width() / scale)

        def _grow(share: float, floor: int, cap_mult: float = None) -> int:
            cap_mult = self._GROWTH_CAP if cap_mult is None else cap_mult
            return int(max(floor, min(w_design * share, floor * cap_mult)))

        return (_grow(self._VALUE_SHARE, 20),
                _grow(self._LABEL_SHARE, 10),
                scaled(_grow(self._PAD_SHARE, 8, 2.0), self))

    def _apply_metrics(self) -> None:
        metrics = self._metrics()
        if metrics == self._metrics_cache:
            return          # a resize that changes nothing must not restyle
        self._metrics_cache = metrics
        _val_px, lbl_px, pad = metrics
        side = int(pad * 1.25)
        self._layout.setContentsMargins(side, pad, side, pad)
        self._sty(self._lbl, lambda: (
            f"color: {palette('text_muted')}; font-size: {lbl_px}px; "
            f"font-weight: 600; background: transparent;"))
        self._restyle_value()

    def _restyle_value(self) -> None:
        val_px = (self._metrics_cache or self._metrics())[0]
        # A long number in the same box needs to give some size back —
        # the same 20/16 relationship the fixed sizes used to have.
        if len(self._val_lbl.text()) > 5:
            val_px = int(val_px * 0.8)
        self._sty(self._val_lbl, lambda: (
            f"color: {palette(self._accent_key)}; font-size: {val_px}px; "
            f"font-weight: 700; background: transparent;"))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_metrics()

    def refresh_styles(self):
        # The UI-scale floors may have moved with the theme/scale change.
        self._metrics_cache = None
        super().refresh_styles()
        self._apply_metrics()

    def set_stat_value(self, val: str):
        self._val_lbl.setText(val)
        self._restyle_value()

    def set_stat_label(self, label: str):
        self._lbl.setText(label)



class ActivityRow(QFrame, ThemedMixin):
    """One line in the "recent activity" list.

    Every piece of it is named and styled by the theme (#activity_row and
    friends): none of the five looks varies with the row's content, and a
    busy overview holds ten of these, so a theme switch has nothing to
    re-apply here.
    """

    def __init__(self, icon: str, title: str, subtitle: str, time_str: str, tooltip: str = ""):
        super().__init__()
        self.setObjectName("activity_row")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(8)

        icon_lbl = QLabel(icon)
        icon_lbl.setObjectName("activity_icon")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("activity_title")
        title_lbl.setMinimumWidth(scaled(50, self))
        # Elide long titles with "…" instead of disappearing
        title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_lbl.setWordWrap(True)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("activity_sub")
        sub_lbl.setMinimumWidth(scaled(50, self))
        sub_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sub_lbl.setWordWrap(True)

        text_col.addWidget(title_lbl)
        text_col.addWidget(sub_lbl)
        row.addLayout(text_col, 1)

        time_lbl = QLabel(time_str)
        time_lbl.setObjectName("activity_time")
        time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_lbl.setMinimumWidth(scaled(30, self))
        time_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        row.addWidget(time_lbl)
        # Expose label for live timestamp refresh without full rebuild
        self._ts_lbl = time_lbl

        if tooltip:
            self.setToolTip(tooltip)
            time_lbl.setToolTip(tooltip)


class SyncDonutChart(QWidget, ThemedMixin):
    """Donut chart showing sync status distribution using QPainter.

    Slice colours are stored as palette KEYS (e.g. ``"success"``) and resolved
    live inside ``paintEvent`` via ``palette(key)``, so a theme switch re-themes
    every slice with just a repaint — ``refresh_styles()`` only needs ``update()``.
    """

    # Share of the widget's width the ring may take. A ceiling is needed —
    # a ring touching the card edges looks broken — but it has to be a
    # SHARE, not a fixed pixel count: the old cap of 220px was reached at
    # any ordinary window size, so the donut was already at its maximum when
    # the page opened and could only ever get smaller from there.
    _RING_WIDTH_SHARE = 0.85

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[tuple[str, int, str]] = []  # [(label, count, color_key), ...]
        self._total = 0
        # Ring geometry from the last paint, for hit-testing tooltips.
        # (cx, cy, r_outer, r_inner, [(label, count, start_deg, span_deg)])
        self._ring_hit: tuple | None = None
        self.setMinimumSize(scaled(130, self, min_px=100), scaled(100, self, min_px=80))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Charts are fully painted by us — skip Qt's background fill.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # ── Tooltips over the ring ────────────────────────────────────────────
    def _segment_at(self, pos) -> tuple | None:
        """(label, count) of the slice under *pos*, or None."""
        hit = self._ring_hit
        if not hit:
            return None
        cx, cy, r_out, r_in, segments = hit
        dx = pos.x() - cx
        dy = cy - pos.y()          # screen y grows downward; angles do not
        dist = math.hypot(dx, dy)
        if dist > r_out or dist < r_in:
            return None
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        for label, count, start_deg, span_deg in segments:
            # Slices are drawn clockwise from *start_deg*, so the sweep runs
            # towards DECREASING angle; measure how far round we have come.
            delta = (start_deg - angle) % 360.0
            if delta < abs(span_deg):
                return label, count
        return None

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            seg = self._segment_at(e.pos())
            if seg is not None and self._total:
                label, count = seg
                pct = round(count * 100.0 / self._total)
                QToolTip.showText(
                    e.globalPos(), f"{label}: {count} ({pct}%)", self)
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)

    def set_data(self, data: list[tuple[str, int, str]]):
        # Third element is a palette KEY (resolved in paintEvent), not a hex.
        filtered = [(l, c, col) for l, c, col in data if c > 0]
        total = sum(c for _, c, _ in filtered)
        if filtered == self._data and total == self._total:
            return
        self._data = filtered
        self._total = total
        self.update()

    def refresh_styles(self):
        # Colours are read live from palette() in paintEvent, so a repaint is
        # all that's needed to pick up the new theme.
        super().refresh_styles()
        self.setMinimumSize(scaled(130, self, min_px=100), scaled(100, self, min_px=80))
        self.update()

    def paintEvent(self, event):
        if self.width() < 2 or self.height() < 2:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.fillRect(self.rect(), QColor(palette('bg_card')))
        if not self._data or self._total == 0:
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        item_spacing = scaled(17, self, min_px=14)
        legend_cols = self._legend_columns()
        legend_h = self._legend_rows(legend_cols) * item_spacing
        # The ring and its legend read as one block when they touch;
        # a little air between them is what separates the picture from
        # the key to it. Scaled, like every other spacing here.
        gap = scaled(16, self, min_px=9)
        # Responsive in BOTH directions: the ceiling is a share of the
        # widget's own width, so the ring keeps growing as the window does
        # instead of stopping at a fixed pixel size it reaches immediately.
        avail_diam = min(w - scaled(16, self), h - legend_h - gap - scaled(6, self))
        chart_size = max(scaled(75, self, min_px=65),
                         min(avail_diam, int(w * self._RING_WIDTH_SHARE)))
        thickness = max(int(chart_size // 5.5), 7)
        ring_room = chart_size - thickness

        if ring_room < scaled(35, self) or (h - legend_h < scaled(35, self)):
            self._ring_hit = None          # no ring to point at
            self._draw_legend_only(painter, h, legend_h, item_spacing,
                                   legend_cols)
            painter.end()
            return

        total_block = chart_size + gap + legend_h
        top_y = max(2.0, (h - total_block) / 2)
        cx = w / 2
        cy = top_y + chart_size / 2
        rect = QRectF(cx - ring_room / 2, cy - ring_room / 2, ring_room, ring_room)

        start = 90 * 16  # start at top (Qt uses 1/16th degrees, clockwise negative)
        segments: list[tuple] = []
        for label, count, color_key in self._data:
            span = int(-count / self._total * 360 * 16)
            pen = QPen(QColor(palette(color_key)), thickness)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawArc(rect, start, span)
            segments.append((label, count, start / 16.0, span / 16.0))
            start += span
        # Remember where the ring landed so a hover can be resolved to the
        # slice under it — the chart is one painted widget, so this is the
        # only record of which pixels belong to which status.
        self._ring_hit = (cx, cy,
                          ring_room / 2 + thickness / 2,
                          max(0.0, ring_room / 2 - thickness / 2),
                          segments)

        # Center text
        painter.setPen(QColor(palette('text')))
        font = QFont()
        font.setPixelSize(max(int(ring_room // 5), scaled(13, self, min_px=11)))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self._total))

        font.setPixelSize(max(int(ring_room // 8), scaled(9, self, min_px=8)))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(palette('text_muted')))
        label_rect = QRectF(rect.x(), rect.y() + ring_room // 4, rect.width(), rect.height())
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, t('overview.chart_total'))

        # Legend: positioned directly below the donut ring and follows it dynamically
        self._draw_legend(painter, top_y + chart_size + gap, h, item_spacing,
                          legend_cols)
        painter.end()

    # Narrowest a legend column may get before a label is elided down to
    # nothing useful: dot, a few words, a space, the number.
    _LEGEND_MIN_COL_W = 118

    def _legend_columns(self) -> int:
        """How many columns the legend gets — 2 when they genuinely fit, else 1.

        Stacking every entry in one column wastes the whole right half of the
        card and pushes the ring smaller to make room for a tall list, so the
        entries pair up across two columns when there is width for it. The
        decision is made from the CURRENT width on every paint, which is what
        makes it reflow: narrow the window and the columns fall back to the
        single stack this always drew, with no state to keep in sync.
        """
        if len(self._data) < 2:
            return 1
        usable = self.width() - scaled(8, self) - scaled(10, self)
        return 2 if usable >= 2 * scaled(self._LEGEND_MIN_COL_W, self) else 1

    def _legend_rows(self, cols: int) -> int:
        return -(-len(self._data) // max(1, cols))     # ceil division

    def _draw_legend(self, painter, ly: float, h: int, item_spacing: int,
                     cols: int = 1):
        """Colour dot, label, then the label's OWN number right after it,
        laid out across *cols* columns and filled row by row.

        The count used to be right-aligned to the widget edge, which put it
        on the far side of the chart from the label it belonged to — reading
        a row meant crossing the whole donut to find its number. Keeping the
        pair together is the whole point of a legend, so the number follows
        its label with one space of separation.

        Row-major fill, so the reading order is the order the entries are in:
        the first goes top-left, the second top-right, the third starts the
        next row on the left again. Column-major would put the second entry
        below the first, which reads as two separate lists rather than one
        list in two columns.
        """
        w = self.width()
        font = QFont()
        font.setPixelSize(scaled(11, self, min_px=10))
        painter.setFont(font)
        fm = painter.fontMetrics()
        cols = max(1, int(cols))
        left_pad = scaled(8, self)
        right_pad = scaled(10, self)
        col_w = (w - left_pad - right_pad) / cols
        dot_to_text = scaled(14, self)
        pair_gap = scaled(6, self, min_px=4)
        for i, (label, count, color_key) in enumerate(self._data):
            row, col = divmod(i, cols)
            row_y = ly + row * item_spacing
            if row_y + scaled(12, self) > h:
                break
            dot_x = left_pad + col * col_w
            text_x = dot_x + dot_to_text
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette(color_key)))
            painter.drawEllipse(int(dot_x), int(row_y + scaled(3, self)),
                                scaled(8, self), scaled(8, self))
            count_str = str(count)
            count_w = fm.horizontalAdvance(count_str)
            # Room for label + gap + count on one line, inside THIS column —
            # a two-column legend has to elide against its own share of the
            # width, not the whole widget, or the columns overlap.
            avail_w = col_w - dot_to_text - (right_pad if cols == 1 else scaled(6, self))
            label_max_w = max(20, avail_w - count_w - pair_gap)
            elided_label = fm.elidedText(
                label, Qt.TextElideMode.ElideRight, int(label_max_w))
            baseline = int(row_y + scaled(11, self))
            painter.setPen(QColor(palette('text_secondary')))
            painter.drawText(int(text_x), baseline, elided_label)
            painter.setPen(QColor(palette('text')))
            painter.drawText(
                int(text_x + fm.horizontalAdvance(elided_label) + pair_gap),
                baseline, count_str)

    def _draw_legend_only(self, painter, h: int, legend_h: int,
                          item_spacing: int = 17, cols: int = 1):
        """Fallback when the ring cannot fit: just the legend."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if legend_h > h - 4:
            painter.setClipRect(self.rect())
        self._draw_legend(painter, max(4, h - legend_h + 4), h, item_spacing,
                          cols)


class BackupBarChart(QWidget, ThemedMixin):
    """Mini bar chart showing backup count per day (last 7 days) using QPainter.

    All bar/label/accent colours are read live from ``palette()`` inside
    ``paintEvent``, so ``refresh_styles()`` only needs to trigger a repaint.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # [(day_label, count, tooltip), ...]
        self._bars: list[tuple[str, int, str]] = []
        self._max_val = 0
        # Per-column hover hit regions built during paint: (x_left, x_right, tip)
        self._hit_regions: list[tuple[float, float, str]] = []
        self.setMinimumHeight(scaled(115, self, min_px=95))
        self.setMinimumWidth(scaled(240, self, min_px=180))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Enable per-day hover tooltips showing the backup count for that day.
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_data(self, bars: list[tuple]):
        # (label, count, tooltip); a missing tooltip defaults to "".
        normalized = [(b[0], int(b[1]), b[2] if len(b) > 2 else "") for b in bars]
        if normalized == self._bars:
            return
        self._bars = normalized
        self._max_val = max((c for _, c, _ in self._bars), default=0)
        self._hit_regions = []   # rebuilt on next paint — avoid stale tooltips
        self.update()

    def refresh_styles(self):
        # Colours are read live from palette() in paintEvent — repaint to re-theme.
        super().refresh_styles()
        self.setMinimumHeight(scaled(115, self, min_px=95))
        self.setMinimumWidth(scaled(240, self, min_px=180))
        self.update()

    def mouseMoveEvent(self, event):
        """Show a tooltip with the day's backup count while hovering a column."""
        try:
            xpos = event.position().x()
        except AttributeError:
            xpos = event.x()
        tip = ""
        for x0, x1, t_ in self._hit_regions:
            if x0 <= xpos <= x1:
                tip = t_
                break
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        if self.width() < 2 or self.height() < 2:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.fillRect(self.rect(), QColor(palette('bg_card')))
        if not self._bars:
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin_bottom = scaled(20, self, min_px=16)
        margin_top = scaled(22, self, min_px=18)
        bar_area_h = max(20.0, h - margin_bottom - margin_top)
        n = len(self._bars)
        gap = 6
        bar_w = max((w - gap * (n + 1)) / n, 8)
        accent = QColor(palette('accent'))
        muted = QColor(palette('text_hint'))

        self._hit_regions = []
        for i, (label, count, tip) in enumerate(self._bars):
            # When the window narrows the bars shrink too: keep the day labels
            # readable by abbreviating them rather than letting them overlap
            # and "eat" each other (same rule as the donut's legend fallback).
            day_label = label
            if bar_w < scaled(30, self) and len(day_label) > 3:
                day_label = day_label[:3] + "…"
            x = gap + i * (bar_w + gap)
            # Whole-column hover region (centred on the bar, spanning the gap)
            # so hovering anywhere in a day's column shows its tooltip.
            self._hit_regions.append((x - gap / 2, x + bar_w + gap / 2, tip))
            if self._max_val > 0:
                bar_h = max(count / self._max_val * bar_area_h, 2)
            else:
                bar_h = 2
            y = margin_top + bar_area_h - bar_h

            # Bar
            painter.setPen(Qt.PenStyle.NoPen)
            c = QColor(accent)
            c.setAlpha(200 if count > 0 else 40)
            painter.setBrush(c)
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 3, 3)

            # Count on top
            if count > 0:
                painter.setPen(QColor(palette('text_secondary')))
                font = QFont()
                font.setPixelSize(scaled(10, self, min_px=9))
                font.setBold(True)
                painter.setFont(font)
                cnt_h = scaled(15, self, min_px=12)
                cnt_y = max(2.0, y - cnt_h - 1)
                painter.drawText(QRectF(x - 2, cnt_y, bar_w + 4, cnt_h),
                                 Qt.AlignmentFlag.AlignCenter, str(count))
            # Day label
            painter.setPen(muted)
            font = QFont()
            font.setPixelSize(scaled(9, self, min_px=8))
            painter.setFont(font)
            painter.drawText(QRectF(x - 2, h - margin_bottom + 2, bar_w + 4, margin_bottom),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, day_label)

        painter.end()


class OverviewPage(PageScrollMixin, QWidget, ThemedMixin):
    _REFRESH_COOLDOWN_S = 60.0  # full wipe+rebuild once per minute max

    backup_requested = Signal(str)
    backup_all_requested = Signal(object)  # list[str] game ids
    open_library     = Signal()
    # "Take me where I can fix this." Raised from the two places that are a
    # dead end with no provider connected — see _sync_all and the provider
    # label. Connecting a page has nothing to do with the Overview, so the
    # page it belongs to is where the user is sent.
    open_sync        = Signal()
    refresh_all_requested = Signal()  # wipe + re-pump every open page

    def __init__(self, parent=None):
        super().__init__(parent)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        # Separate timer for relative-time labels (e.g. "2 min fa") — runs every 60s
        # so timestamps update without triggering a full data reload.
        self._ts_timer = QTimer(self)
        self._ts_timer.setInterval(60_000)
        self._ts_timer.timeout.connect(self._refresh_timestamps_only)
        # Coalesce bursty library/sync signals into one refresh.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self.refresh)
        self._dirty_while_hidden = False
        self._activity_cache_key = None  # skip activity rebuild if unchanged
        # A full wipe+rebuild while a game is running would fight the game's
        # file writes (and the overlay-based entry point makes that easy to
        # hit), so the refresh button is disabled in-game. The monitor already
        # tracks this and emits game_launched/game_exited — no polling needed;
        # the initial check in _build() covers an app opened while a game was
        # already running (e.g. from the overlay).
        self._in_game = False
        # Provider-label colour depends on online state; read by _provider_style()
        # (registered once in _build_body and re-applied on refresh/theme switch).
        self._provider_online = False
        self._build()
        self._connect_signals()
        # Warm data while hidden; the visible enter refresh is deferred so the
        # page can paint first.
        self.refresh()

    def schedule_refresh(self):
        """Debounced refresh; no-op work while the page is hidden."""
        if not self.isVisible():
            self._dirty_while_hidden = True
            return
        # Backup Tutti and Sync Tutti run under library begin_bulk /
        # orchestrator batch: per-entry signals would rebuild the activity
        # list once per game (partial rows + wasted CPU). bulk_finished /
        # batch_finished refresh once at the end instead.
        try:
            if get_library()._in_bulk() or get_orchestrator()._sync_batch:
                return
        except Exception:
            pass
        self._debounce.start()

    def refresh_on_enter(self):
        """Refresh after the page is already visible (non-blocking).

        Every enter — the first show (app open) included — runs the refresh
        silently: no please-wait, the page stays fully interactive and the
        data lands when it's ready. The refresh itself only reads lightweight
        indexes and rebuilds at most five activity rows, so there is nothing
        a busy sheet would be covering.
        """
        QTimer.singleShot(0, self.refresh)

    def _connect_signals(self):
        """Connect to ALL library, backup, sync, monitor, config, and watcher signals for immediate refresh."""
        self._on_game_added = lambda _: self.schedule_refresh()
        self._on_game_removed = lambda _: self.schedule_refresh()
        self._on_bulk_finished = lambda: self.schedule_refresh()
        self._on_backup_created = self._on_backup_created_refresh
        self._on_sync_finished = lambda *_: self.schedule_refresh()

        # 1. LibraryManager signals
        try:
            lib = get_library()
            lib.game_updated.connect(self.schedule_refresh)
            lib.game_added.connect(self._on_game_added)
            lib.game_removed.connect(self._on_game_removed)
            lib.bulk_finished.connect(self._on_bulk_finished)
            lib.library_loaded.connect(self.schedule_refresh)
        except Exception:
            pass

        # 2. BackupManager signals
        try:
            bm = get_backup_manager()
            bm.backup_created.connect(self._on_backup_created)
            bm.backup_restored.connect(lambda *_: self.schedule_refresh())
            bm.backup_deleted.connect(lambda *_: self.schedule_refresh())
            bm.index_validation_failed.connect(lambda *_: self.schedule_refresh())
            bm.index_validation_recovered.connect(lambda *_: self.schedule_refresh())
        except Exception:
            pass

        # 3. SyncOrchestrator signals
        try:
            orch = get_orchestrator()
            orch.sync_started.connect(lambda *_: self.schedule_refresh())
            orch.sync_finished.connect(self._on_sync_finished)
            orch.conflict_detected.connect(lambda *_: self.schedule_refresh())
            orch.provider_changed.connect(lambda *_: self.schedule_refresh())
            orch.providers_updated.connect(self.schedule_refresh)
            orch.batch_finished.connect(self.schedule_refresh)
        except Exception:
            pass

        # 4. ProcessMonitor signals
        try:
            mon = get_monitor()
            mon.game_launched.connect(lambda *_: self._update_in_game_state())
            mon.game_exited.connect(lambda *_: self._update_in_game_state())
            mon.unknown_game_detected.connect(lambda *_: self._update_in_game_state())
            mon.unknown_game_exited.connect(lambda *_: self._update_in_game_state())
            mon.game_match_unverified.connect(lambda *_: self._update_in_game_state())
            mon.game_match_unverified_gone.connect(lambda *_: self._update_in_game_state())
        except Exception:
            pass

        # 5. ConfigManager signals
        try:
            from core.config_manager import get_config
            get_config().config_changed.connect(lambda *_: self.schedule_refresh())
        except Exception:
            pass

        # 6. SaveWatcher signals
        try:
            from core.watcher import get_watcher
            w = get_watcher()
            if w is not None:
                w.save_changed.connect(lambda *_: self.schedule_refresh())
                w.folder_appeared.connect(lambda *_: self.schedule_refresh())
        except Exception:
            pass

    def _on_backup_created_refresh(self, _entry=None):
        # Backup Tutti runs under library begin_bulk — skip per-zip refreshes.
        try:
            if get_library()._in_bulk():
                return
        except Exception:
            pass
        self.schedule_refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(scaled(20, self, min_px=14), scaled(14, self, min_px=8), scaled(20, self, min_px=14), scaled(14, self, min_px=8))
        root.setSpacing(scaled(8, self, min_px=4))

        # Header
        header = QHBoxLayout()
        header.setSpacing(scaled(8, self, min_px=4))
        self._header = QLabel(t("overview.title"))
        self._header.setObjectName("page_header")
        header.addWidget(self._header)
        header.addStretch()
        self._refresh_btn = QPushButton(t("buttons.refresh_icon"))
        self._refresh_btn.setObjectName("icon_btn")
        self._refresh_btn.setToolTip(t("tooltips.refresh"))
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)

        # Active game banner
        self._active_banner = QFrame()
        self._active_banner.setFrameShape(QFrame.Shape.NoFrame)
        self._active_banner.setObjectName("active_banner")
        self._update_banner_style()
        bl = QHBoxLayout(self._active_banner)
        bl.setContentsMargins(scaled(12, self, min_px=8), scaled(6, self, min_px=4), scaled(12, self, min_px=8), scaled(6, self, min_px=4))
        bl.setSpacing(scaled(8, self, min_px=4))
        self._active_icon = QLabel("\U0001f3ae")
        self._active_icon.setObjectName("active_game_icon")
        self._active_name = QLabel(t("overview.no_active_game"))
        self._active_name.setObjectName("active_game_name")
        self._active_sub  = QLabel("")
        self._active_sub.setObjectName("active_game_sub")
        self._active_backup_btn = QPushButton(t("buttons.backup_now"))
        self._active_backup_btn.setObjectName("primary_btn")
        self._active_backup_btn.setVisible(False)
        self._active_backup_btn.clicked.connect(self._on_backup_active)
        left_col = QVBoxLayout()
        left_col.setSpacing(2)
        left_col.addWidget(self._active_name)
        left_col.addWidget(self._active_sub)
        bl.addWidget(self._active_icon)
        bl.addLayout(left_col, 1)
        bl.addWidget(self._active_backup_btn)
        root.addWidget(self._active_banner)

        # Body scrolls only when the stack viewport cannot fit the dashboard
        # (resize-first — same rule as dialogs).
        self._page_scroll = QScrollArea()
        self._page_scroll.setWidgetResizable(True)
        self._page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body_host = QWidget()
        body_host.setObjectName("transparent_bg")
        body_lay = QVBoxLayout(body_host)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(12)
        self._build_body(body_lay)
        self._page_scroll.setWidget(body_host)
        root.addWidget(self._page_scroll, 1)
        self._register_page_scroll(self._page_scroll)
        if hasattr(self, "_activity_scroll"):
            self._register_page_scroll(self._activity_scroll, list_content=True)

    def _update_banner_style(self):
        """Nothing to do: #active_banner (gradient, border, accent edge) is
        defined per theme in DARK_THEME/LIGHT_THEME."""

    def _on_provider_clicked(self, event):
        """Offer the Sync page, but only while there is nothing connected."""
        from PySide6.QtCore import Qt as _Qt
        if not self._provider_online and event.button() == _Qt.MouseButton.LeftButton:
            event.accept()
            self.open_sync.emit()
            return
        event.ignore()

    def _provider_style(self) -> str:
        """Provider-label style — colour depends on the current online state."""
        key = 'accent' if self._provider_online else 'text_muted'
        return f"color:{palette(key)};font-size:{scaled(11, self)}px;padding:4px;"

    def refresh_styles(self):
        """Re-apply every inline, palette-dependent style IN PLACE for the current
        theme — no widget rebuild.

        super().refresh_styles() replays this page's OWN registered styles; then
        we cascade into child widgets that keep their own ThemedMixin registries
        (the four stat cards and the two charts) and into the dynamically
        rebuilt activity rows, so after this call no widget shows a stale
        (previous-theme) colour.
        """
        super().refresh_styles()
        # Stable children with their own style registries (not covered by super).
        for card in (self._card_games, self._card_backups,
                     self._card_synced, self._card_playtime):
            if _safe(card):
                card.refresh_styles()
        # Custom-painted charts: they read palette() live in paintEvent, so their
        # refresh_styles() just repaints.
        if _safe(self._donut_chart):
            self._donut_chart.refresh_styles()
        if _safe(self._bar_chart):
            self._bar_chart.refresh_styles()
        # Dynamic children: activity rows are recreated on data refresh, so reach
        # the CURRENT instances through the layout (never a stale list).
        if _safe(self._activity_layout):
            for i in range(self._activity_layout.count()):
                item = self._activity_layout.itemAt(i)
                w = item.widget() if item else None
                if (w is not None and w is not self._activity_empty
                        and hasattr(w, "refresh_styles")):
                    try:
                        w.refresh_styles()
                    except RuntimeError:
                        pass  # underlying C++ widget deleted mid-cascade — skip

    def _remediate_page_scrolls(self):
        """Re-mediate scroll policies after DPI scale changes to maintain proportions."""
        try:
            # Update chart dimensions to maintain proportions
            if _safe(self._donut_chart):
                self._donut_chart.setMinimumSize(
                    scaled(130, self, min_px=100), scaled(100, self, min_px=80))
                self._donut_chart.updateGeometry()
            if _safe(self._bar_chart):
                self._bar_chart.setMinimumHeight(scaled(115, self, min_px=95))
                self._bar_chart.setMaximumHeight(scaled(175, self, min_px=130))
                self._bar_chart.setMinimumWidth(scaled(240, self, min_px=180))
                self._bar_chart.updateGeometry()
            if _safe(self._activity_scroll):
                self._activity_scroll.setMinimumWidth(scaled(130, self, min_px=110))
                self._activity_scroll.setMinimumHeight(scaled(100, self, min_px=80))
                self._activity_scroll.updateGeometry()
            for btn, _ in getattr(self, "_action_btns", []):
                if _safe(btn):
                    btn.setMinimumWidth(scaled(135, self, min_px=120))
                    btn.updateGeometry()
            # Update stat cards to maintain proportions
            for card in (self._card_games, self._card_backups,
                         self._card_synced, self._card_playtime):
                if _safe(card):
                    card.setMinimumWidth(scaled(65, self, min_px=55))
                    card.updateGeometry()
            
            # Trigger layout recalculation
            if hasattr(self, 'layout') and self.layout():
                self.layout().activate()
                self.layout().update()
        except Exception:
            pass

    def _build_body(self, root):
        """Continuation of _build — stat cards, charts, activity, actions."""
        # Stat cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self._card_games   = StatCard("0", t("overview.stat_games"),    "accent",  card_key="games")
        self._card_backups = StatCard("0", t("overview.stat_backups"),  "info",    card_key="backups")
        self._card_synced  = StatCard("0", t("overview.stat_synced"),   "cloud",   card_key="sync")
        self._card_playtime = StatCard("0", t("overview.stat_playtime"), "warning", card_key="playtime")
        for c in (self._card_games, self._card_backups, self._card_synced, self._card_playtime):
            cards_row.addWidget(c, 1)
        root.addLayout(cards_row, 0)

        # Body — 3 columns: activity | donut | actions
        body = QHBoxLayout()
        body.setSpacing(10)

        # Column 1: recent activity (stretches)
        activity_col = QVBoxLayout()
        activity_col.setSpacing(6)
        self._activity_header = QLabel(t("overview.recent_activity"))
        self._activity_header.setObjectName("section_header")
        activity_col.addWidget(self._activity_header)

        self._activity_frame = QFrame()
        self._activity_frame.setFrameShape(QFrame.Shape.NoFrame)
        self._activity_frame.setObjectName("panel_card")
        self._activity_layout = QVBoxLayout(self._activity_frame)
        self._activity_layout.setContentsMargins(8, 6, 8, 6)
        self._activity_layout.setSpacing(0)
        self._activity_empty = QLabel(t("overview.no_activity"))
        self._activity_empty.setObjectName("empty_hint")
        self._activity_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._activity_layout.addWidget(self._activity_empty)

        # Wrap in a QScrollArea so rows are never crushed/hidden when the window
        # is made narrow — the panel scrolls vertically and never clips content.
        self._activity_scroll = QScrollArea()
        self._activity_scroll.setWidgetResizable(True)
        self._activity_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._activity_scroll.setObjectName("activity_scroll")
        self._activity_scroll.setWidget(self._activity_frame)
        self._activity_scroll.setMinimumWidth(scaled(130, self, min_px=110))
        self._activity_scroll.setMinimumHeight(scaled(100, self, min_px=80))
        self._activity_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        activity_col.addWidget(self._activity_scroll, 1)
        body.addLayout(activity_col, 5)

        # Column 2: donut chart
        donut_col = QVBoxLayout()
        donut_col.setSpacing(6)
        donut_header = QLabel(t("overview.sync_distribution"))
        donut_header.setObjectName("section_header")
        donut_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        donut_col.addWidget(donut_header)
        self._donut_header = donut_header

        self._donut_chart = SyncDonutChart()
        self._donut_chart.setMinimumSize(scaled(130, self, min_px=100), scaled(100, self, min_px=80))
        self._donut_chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        donut_col.addWidget(self._donut_chart, 1)
        body.addLayout(donut_col, 4)

        # Column 3: quick actions
        actions_col = QVBoxLayout()
        actions_col.setSpacing(6)
        self._actions_header = QLabel(t("overview.quick_actions"))
        self._actions_header.setObjectName("section_header")
        actions_col.addWidget(self._actions_header)

        self._action_btns: list[tuple[QPushButton, str]] = []   # (button, i18n key) for retranslation
        for label_key, cb in [
            ('overview.add_game',   self.open_library.emit),
            ('overview.sync_all',   self._sync_all),
            ('overview.backup_all', self._backup_all),
        ]:
            btn = QPushButton(t(label_key))
            self._action_btns.append((btn, label_key))
            btn.setMinimumHeight(scaled(32, self))
            btn.setMinimumWidth(scaled(135, self, min_px=120))
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("quick_action_btn")
            btn.clicked.connect(cb)
            actions_col.addWidget(btn)

        self._provider_lbl = QLabel()
        self._provider_lbl.setMinimumWidth(scaled(135, self, min_px=120))
        self._sty(self._provider_lbl, self._provider_style)
        # "No provider connected" is the one line on this page that names a
        # problem the user can fix, so it is also the way to fix it. Only
        # while it says that: once something IS connected the label is a
        # status, and a status that navigates when clicked is a surprise.
        self._provider_lbl.mousePressEvent = self._on_provider_clicked

        self._provider_lbl.setWordWrap(True)
        actions_col.addWidget(self._provider_lbl)

        # Transient feedback for the quick actions ("nothing to sync" etc.):
        # appears for a few seconds under the buttons, then hides itself.
        self._sync_feedback_lbl = QLabel("")
        self._sync_feedback_lbl.setWordWrap(True)
        self._sync_feedback_lbl.setVisible(False)
        self._sty(self._sync_feedback_lbl,
                  lambda: f"color:{palette('success')};font-size:{scaled(11, self)}px;")
        actions_col.addWidget(self._sync_feedback_lbl)
        actions_col.addStretch(1)
        body.addLayout(actions_col, 3)

        root.addLayout(body, 3)

        # Backup activity bar chart (full width, below body)
        bar_header = QLabel(t("overview.backup_activity"))
        bar_header.setObjectName("section_header")
        bar_header.setContentsMargins(0, scaled(4, self, min_px=2), 0, scaled(2, self, min_px=1))
        self._bar_header = bar_header
        root.addWidget(bar_header, 0)

        self._bar_chart = BackupBarChart()
        self._bar_chart.setObjectName("panel_card")
        self._bar_chart.setMinimumHeight(scaled(115, self, min_px=95))
        self._bar_chart.setMaximumHeight(scaled(175, self, min_px=130))
        self._bar_chart.setMinimumWidth(scaled(240, self, min_px=180))
        self._bar_chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self._bar_chart, 1)

        # Initial in-game state without waiting for the first poll tick
        # (app opened from the overlay while a game is running).
        self._update_in_game_state()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _on_refresh_clicked(self):
        """Refresh the dashboard data AND ask the window to wipe every other
        page: their next visit re-runs the async chunk pump (the refresh
        button was previously wired to data-only refresh, which the library
        signals already drive). Cooldown: once per 60 s — rebuilding every
        page on every click would thrash the UI. In-game: disabled entirely
        (a wipe while the game is writing its saves could corrupt files)."""
        if self._in_game:
            return
        from time import monotonic
        now = monotonic()
        remaining = self._REFRESH_COOLDOWN_S - (now - getattr(self, "_last_refresh_mono", 0.0))
        if remaining > 0:
            self._show_cooldown_toast(int(remaining) + 1)
            return
        self._last_refresh_mono = now

        # delay_ms=0, deliberately. The refresh below is SYNCHRONOUS: both
        # refresh() and the direct-connection emit run to completion without
        # ever returning to the event loop. A deferred sheet arms a QTimer
        # that therefore cannot fire, and the finally block closes it on the
        # way out — so the please-wait was never shown at all, however long
        # the work took. That is precisely the case the module documents for
        # delay 0 ("blocks the event loop with no ticks"), and reveal() pumps
        # once so the sheet is actually painted before the work starts.
        #
        # Close any previous sheet before making a new one, like every other
        # DeferredBusy holder does. Overwriting the attribute would strand the
        # old overlay with its countdown still running — a revealed sheet that
        # nobody can reach and nobody stops.
        from ui.widgets.busy_overlay import DeferredBusy
        self._stop_refresh_busy()
        self._refresh_busy = DeferredBusy(self, t("common.please_wait"),
                                          delay_ms=0)

        try:
            self.refresh()
            # Direct connection: _on_refresh_all_pages runs to completion
            # inside emit(). Every page it wipes defers its rebuild only when
            # VISIBLE, and the pages live in a QStackedWidget with Panoramica
            # on top — so none of them are, and there is nothing still running
            # when this returns.
            self.refresh_all_requested.emit()
        finally:
            # Closed here rather than on a 400 ms timer that had no relation
            # to the work: it hid the sheet while a slow refresh was still
            # going, and kept it up after a fast one had finished.
            self._stop_refresh_busy()
            # ONE trim, and only now. There used to be three per click — one
            # before the rebuild (which threw away the covers the rebuild was
            # about to need), one inside the wipe, and one 250 ms later that
            # landed mid-repaint and threw away what had just been decoded
            # again. Each costs a full cache purge, three gc passes and the
            # working set; doing it up front was the worst of the three.
            try:
                from ui.helpers import trim_process_memory
                trim_process_memory()
            except Exception:
                pass

    def _stop_refresh_busy(self):
        if getattr(self, "_refresh_busy", None) is not None:
            try:
                self._refresh_busy.close()
            except Exception:
                pass
            self._refresh_busy = None

    def _update_in_game_state(self):
        """Poll the process monitor: while any game is running the refresh
        button stays disabled (see _on_refresh_clicked)."""
        try:
            from core.monitor import get_monitor
            playing = bool(get_monitor().currently_playing())
        except Exception:
            playing = False
        if playing == self._in_game:
            return
        self._in_game = playing
        if _safe(self._refresh_btn):
            self._refresh_btn.setEnabled(not playing)
            self._refresh_btn.setToolTip(
                t("tooltips.refresh_in_game" if playing else "tooltips.refresh"))

    def refresh(self):
        """Refresh all live data — safe to call from GUI thread only."""
        if not _safe(self._header):
            return
        self._dirty_while_hidden = False
        # Cancel a pending debounce so we don't double-refresh right after.
        if self._debounce.isActive():
            self._debounce.stop()

        lib   = get_library()
        games = lib.all_games()
        mgr   = get_backup_manager()
        orch  = get_orchestrator()
        # One lock pass — no deepcopy of the full BackupEntry index.
        snap = mgr.overview_index_snapshot()
        bk_rows = snap["rows"]
        all_bk = snap["count"]

        # Active game
        active_entries = get_monitor().currently_playing()
        if active_entries:
            e = active_entries[0]
            from core.engines.game_engine import engine_display, engine_for_game
            eng = engine_display(engine_for_game(e))
            self._active_name.setText(
                f"{e.name}  ·  {eng}" if eng else e.name)
            self._active_sub.setText(t("overview.running_saves", count=len(e.save_paths)))
            self._active_backup_btn.setVisible(True)
            self._active_backup_btn.setProperty("_game_id", e.id)
        else:
            self._active_name.setText(t("overview.no_active_game"))
            self._active_sub.setText("")
            self._active_backup_btn.setVisible(False)

        # Stat cards — guard each widget access in case of teardown during theme change
        if _safe(self._card_games):
            self._card_games.set_stat_value(str(len(games)))
        if _safe(self._card_backups):
            self._card_backups.set_stat_value(str(all_bk))
        if _safe(self._card_synced):
            self._card_synced.set_stat_value(
                str(sum(1 for g in games if g.sync_status == "synced"))
            )
        if _safe(self._card_playtime):
            total_secs = sum(g.playtime_seconds for g in games)
            hours = total_secs // 3600
            mins = (total_secs % 3600) // 60
            self._card_playtime.set_stat_value(f"{hours}h {mins}m" if hours else f"{mins}m")

        # Donut chart — sync status distribution
        if _safe(self._donut_chart):
            status_map = {"synced": 0, "pending": 0, "conflict": 0, "local_only": 0,
                          "cloud_only": 0, "no_saves": 0, "provisional": 0,
                          "archives": 0}
            # Games with no confirmed save_paths yet but that DO have at
            # least one live-tracking-discovered provisional backup get
            # their own bucket instead of being lumped in with "no saves"
            # — there IS restorable data, the user just hasn't confirmed
            # which paths to keep yet.
            _provisional_game_ids = snap["provisional_ids"]
            for g in games:
                if not g.save_paths:
                    s = "provisional" if g.id in _provisional_game_ids else "no_saves"
                else:
                    s = g.sync_status if g.sync_status in status_map else "no_saves"
                status_map[s] += 1
            # Backups with no library game: Aggiungi-percorso archives (pending
            # or already synced) AND leftovers after a game was removed. Always
            # counted — Sync Tutti still only uploads those with needs_sync.
            status_map["archives"] = int(snap.get("orphan_unit_count") or 0)
            # Third element is a palette KEY — the donut resolves it live in
            # paintEvent, so slices re-theme on a light/dark switch (via
            # refresh_styles -> update()) without needing a data refresh.
            donut_data = [
                (t("library.status_synced"),      status_map["synced"],      'success'),
                (t("library.status_pending"),     status_map["pending"],     'warning'),
                (t("library.status_conflict"),    status_map["conflict"],    'error'),
                (t("library.status_local_only"),  status_map["local_only"],  'info'),
                (t("library.status_cloud_only"),  status_map["cloud_only"],  'cloud'),
                (t("library.status_archives"),    status_map["archives"],    'archive'),
                (t("library.status_provisional"), status_map["provisional"], 'provisional'),
                (t("library.status_no_saves"),    status_map["no_saves"],    'text_hint'),
            ]
            self._donut_chart.set_data(donut_data)

        # Bar chart — backup activity last 7 days
        if _safe(self._bar_chart):
            from datetime import timedelta, timezone
            _DAY_KEYS = ["day_mon", "day_tue", "day_wed", "day_thu", "day_fri", "day_sat", "day_sun"]
            now = datetime.now(timezone.utc)
            day_counts: dict[str, int] = {}
            day_labels: list[tuple[str, str]] = []
            for i in range(6, -1, -1):
                d = now - timedelta(days=i)
                iso = d.strftime("%Y-%m-%d")
                short = t(f"overview.{_DAY_KEYS[d.weekday()]}")
                day_counts[iso] = 0
                day_labels.append((iso, short))
            from core import to_local_dt
            for _bid, _gid, created_at, _sz in bk_rows:
                # Bucket by LOCAL date (created_at is naive UTC): a backup
                # made at 00:30 local must count on the local day, not on
                # the previous UTC day.
                _bk_dt = to_local_dt(created_at)
                if _bk_dt is not None:
                    bk_date = _bk_dt.strftime("%Y-%m-%d")
                    if bk_date in day_counts:
                        day_counts[bk_date] += 1
            from i18n import format_dt
            bars = []
            for iso, label in day_labels:
                cnt = day_counts[iso]
                try:
                    _readable = format_dt(datetime.strptime(iso, "%Y-%m-%d"), "%d %b %Y")
                except ValueError:
                    _readable = iso
                bars.append((label, cnt, f"{_readable} · {t('overview.day_backups', count=cnt)}"))
            self._bar_chart.set_data(bars)

        # Provider — colour tracks online state via _provider_style (registered
        # once in _build_body). Apply it directly here so we don't re-register on
        # every refresh; refresh_styles() re-themes the current state in place.
        if orch.is_online():
            pids = ", ".join(orch.get_connected_provider_ids())
            self._provider_lbl.setText(t("overview.connected_provider", provider=pids))
            self._provider_online = True
        else:
            self._provider_lbl.setText(t('overview.no_provider_connected'))
            self._provider_online = False
        # The cursor and the tooltip follow the state, because the label is
        # only clickable in one of them — a hand over something inert is a
        # promise the page does not keep.
        self._provider_lbl.setCursor(
            Qt.CursorShape.ArrowCursor if self._provider_online
            else Qt.CursorShape.PointingHandCursor)
        self._provider_lbl.setToolTip(
            "" if self._provider_online else t("overview.open_sync_to_connect"))
        self._provider_lbl.setStyleSheet(self._provider_style())

        self._rebuild_activity(games, bk_rows)

    def _refresh_timestamps_only(self):
        """Update only the relative-time labels in the activity list.

        Called every 60 s by _ts_timer.  Avoids a full data reload when all
        that changed is "2 min fa" → "3 min fa".
        """
        if not _safe(self._activity_layout):
            return
        from core import to_local_dt
        from i18n import format_dt
        for i in range(self._activity_layout.count()):
            item = self._activity_layout.itemAt(i)
            w = item.widget() if item else None
            if w is None or w is self._activity_empty:
                continue
            raw_ts = getattr(w, '_raw_ts', None)
            ts_lbl = getattr(w, '_ts_lbl', None)
            if raw_ts and ts_lbl and _safe(ts_lbl):
                try:
                    dt = to_local_dt(raw_ts)
                    if dt is not None:
                        ts_lbl.setText(_fmt_relative(dt))
                        full_date_str = format_dt(dt, "%d %b %Y, %H:%M")
                        w.setToolTip(full_date_str)
                        ts_lbl.setToolTip(full_date_str)
                except (ValueError, TypeError):
                    pass

    def _rebuild_activity(self, games, bk_rows):
        """Rebuild activity rows from lightweight backup tuples."""
        if not _safe(self._activity_layout):
            return

        # Skip full rebuild if underlying data hasn't changed.
        # Relative timestamps are updated in place by _refresh_timestamps_only.
        from i18n import get_current_language
        cur_lang = get_current_language()
        bk_ids_hash = hash(tuple(r[0] for r in bk_rows)) if bk_rows else 0
        game_names_hash = hash(tuple(g.name for g in games)) if games else 0
        game_mod_hash = hash(tuple((g.id, g.last_played or "", g.last_synced or "", g.playtime_seconds, g.last_session_seconds) for g in games)) if games else 0
        cache_key = (len(games), len(bk_rows),
                     games[-1].id if games else "",
                     bk_ids_hash,
                     game_names_hash,
                     game_mod_hash,
                     cur_lang)
        if cache_key == self._activity_cache_key:
            return
        self._activity_cache_key = cache_key

        # Remove only non-empty-label children
        items_to_remove = []
        for i in range(self._activity_layout.count()):
            item = self._activity_layout.itemAt(i)
            w = item.widget() if item else None
            if w is not None and w is not self._activity_empty:
                items_to_remove.append(w)

        for w in items_to_remove:
            self._activity_layout.removeWidget(w)
            w.deleteLater()

        # bk_rows: (backup_id, game_id, created_at, size_human)
        bk_by_game: dict[str, list] = {}
        for _bid, gid, created_at, size_human in bk_rows:
            if not gid:
                continue
            bk_by_game.setdefault(gid, []).append((created_at, size_human))
        for gid in bk_by_game:
            bk_by_game[gid].sort(key=lambda x: x[0] or "", reverse=True)

        events = []
        for g in games:
            for created_at, size_human in bk_by_game.get(g.id, [])[:2]:
                events.append(("💾", g.name, t('overview.backup_prefix') + f" {size_human}", created_at))
            if g.last_synced:
                events.append(("☁", g.name, t('overview.synced_to_cloud'), g.last_synced))
            if g.last_played:
                # This event represents a single play session (keyed on
                # last_played), so show the LAST SESSION's duration, not the
                # lifetime total.
                session_info = (g.get_last_session_formatted()
                                if g.last_session_seconds > 0
                                else g.get_playtime_formatted())
                events.append(("🎮", g.name, t('overview.played_prefix') + f" {session_info}", g.last_played))

        # Backups whose game is not in the library are ARCHIVES — folders
        # handed over without adding the game. They were dropped here for
        # the simple reason that this loop walks the library, so backing one
        # up left the page saying nothing had happened.
        _lib_ids = {g.id for g in games}
        _archives = [gid for gid in bk_by_game if gid not in _lib_ids]
        if _archives:
            try:
                from core.backup import get_backup_manager as _bmgr
                _mgr = _bmgr()
                for _gid in _archives:
                    _name = _mgr.archive_display_name(_gid)
                    if not _name:
                        continue
                    for created_at, size_human in bk_by_game[_gid][:2]:
                        events.append(("📦", _name,
                                       t('overview.backup_prefix') + f" {size_human}",
                                       created_at))
            except Exception:
                logger.debug("could not add archive activity", exc_info=True)

        events.sort(key=lambda e: e[3] or "", reverse=True)

        if not events:
            if _safe(self._activity_empty):
                self._activity_empty.setVisible(True)
            return

        if _safe(self._activity_empty):
            self._activity_empty.setVisible(False)

        from core import to_local_dt
        from i18n import format_dt
        for icon, title, subtitle, ts in events[:5]:
            try:
                dt = to_local_dt(ts)
                if dt is not None:
                    tstr = _fmt_relative(dt)
                    full_date_str = format_dt(dt, "%d %b %Y, %H:%M")
                else:
                    tstr = ""
                    full_date_str = ""
            except (ValueError, TypeError):
                tstr = ""
                full_date_str = ""
            row = ActivityRow(icon, title, subtitle, tstr, tooltip=full_date_str)
            row._raw_ts = ts   # raw ISO timestamp for live refresh
            self._activity_layout.addWidget(row)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_backup_active(self):
        gid = self._active_backup_btn.property("_game_id")
        if gid:
            self.backup_requested.emit(gid)

    def _backup_all(self):
        """Enqueue Backup Tutti via MainWindow (adaptive queue + sidebar progress).

        Throttled to one launch per minute (same rule as the refresh button):
        a second click inside the window shows a cooldown toast.
        """
        from time import monotonic
        now = monotonic()
        remaining = self._REFRESH_COOLDOWN_S - (now - getattr(self, "_last_backup_all_mono", 0.0))
        if remaining > 0:
            self._show_cooldown_toast(int(remaining) + 1)
            return
        self._last_backup_all_mono = now
        games = [g for g in get_library().all_games() if g.save_paths]
        # Archives too. A save folder handed over without adding the game is
        # in no library listing, which is how "back up everything" quietly
        # meant "everything in the library".
        from core.backup import get_backup_manager as _bm
        ids = [g.id for g in games] + _bm().backupable_archive_ids()
        self.backup_all_requested.emit(ids)

    def _sync_all(self):
        """Sync games that may have changes — same skip rules as Sync page.

        Unchanged games are left alone so library cards / recent activity are
        not stamped for empty up/down runs. The Sync page history still logs
        any run that does go out. Orphan Aggiungi-percorso archives are
        included too (cloud folder = index game_name).

        Throttled to one launch per minute, like Backup Tutti / refresh.
        """
        orch = get_orchestrator()
        if not orch.is_online():
            # Nothing to sync TO. This used to return in silence, so the
            # button was simply inert for anyone who had not set up a
            # provider yet — pressed, nothing happened, nothing said why.
            # The answer to "sync all" when there is nowhere to sync is the
            # page where somewhere gets chosen.
            self.open_sync.emit()
            return
        from time import monotonic
        now = monotonic()
        remaining = self._REFRESH_COOLDOWN_S - (now - getattr(self, "_last_sync_all_mono", 0.0))
        if remaining > 0:
            self._show_cooldown_toast(int(remaining) + 1)
            return
        self._last_sync_all_mono = now
        # Back everything up FIRST. Syncing publishes the newest backup a
        # game has; without this, a game played since its last backup got
        # its OLD one sent up and the sync reported success — the one thing
        # "sync everything" must not quietly mean.
        mw = self.window()
        if mw is not None and hasattr(mw, "_start_backup_all"):
            self._show_sync_feedback(t("overview.backing_up_first"))
            mw._start_backup_all(source="sync_all", then_sync=True)
            return
        self._launch_sync_all()

    def _launch_sync_all(self):
        """Collect what has changed and hand it to the orchestrator."""
        orch = get_orchestrator()
        from core.backup import get_backup_manager
        bm = get_backup_manager()
        jobs = []
        for g in get_library().all_games():
            if not g.save_paths:
                continue
            if g.sync_status == "synced" and not bm.game_needs_publish(g.id):
                # Two ways to be behind: the SAVES moved, which the hashes
                # below catch, or what the index SAYS about them did — a
                # note, a rename, an archive told about another folder. The
                # second leaves every hash identical, so a sweep deciding on
                # hashes alone skipped it and the provider kept the old row.
                recents = bm.get_backups_for_game(g.id)
                if recents:
                    current_hash = (recents[0].cloud_metadata or {}).get("save_hash", "")
                    synced_hash = (g.cloud_metadata or {}).get("last_synced_hash", "")
                    if current_hash and current_hash == synced_hash:
                        continue
                elif not g._saves_changed_since_sync():
                    continue
            jobs.append({
                "game_id": g.id,
                "game_name": g.name,
                "save_paths": list(g.save_paths or []),
                "exe_path": g.exe_path or "",
                "computed_folder_name": g.computed_folder_name or "",
                "name_history": list(g.name_history or []),
            })
        try:
            jobs.extend(bm.orphan_sync_jobs())
        except Exception:
            logger.debug("orphan_sync_jobs failed", exc_info=True)
        if not jobs:
            # Nothing changed anywhere — show a toast (same style as Backup Tutti)
            # instead of the old inline label (which was unique to Sync Tutti).
            try:
                mw = self.window()
                if mw is not None and hasattr(mw, '_overlay') and mw._overlay:
                    mw._overlay.show_batch_done("sync", 0, "")
                else:
                    self._show_sync_feedback(t("sync.nothing_to_sync"))
            except Exception:
                self._show_sync_feedback(t("sync.nothing_to_sync"))
            return
        orch.enqueue_sync_batch(jobs, source="overview")

    def _show_cooldown_toast(self, seconds: int):
        """Show a toast notification when an action is on cooldown."""
        try:
            mw = self.window()
            if mw is not None and hasattr(mw, '_overlay') and mw._overlay:
                from i18n import t as _t
                msg = _t("notifications.cooldown_active", seconds=seconds)
                mw._overlay.show_notice(msg)
                return
        except Exception:
            pass
        # Fallback to inline label. Same sentence as the overlay above:
        # this branch was written in Italian, so an English or Spanish user
        # whose overlay happened to be unavailable read Italian.
        from i18n import t as _t
        self._show_sync_feedback(
            _t("notifications.cooldown_active", seconds=seconds))

    def _show_sync_feedback(self, msg: str):
        """Flash a short message under the quick actions (auto-hides)."""
        if not _safe(self._sync_feedback_lbl):
            return
        self._sync_feedback_lbl.setText(msg)
        self._sync_feedback_lbl.setVisible(True)
        try:
            self._sync_feedback_lbl.adjustSize()
        except RuntimeError:
            return
        QTimer.singleShot(4000, self._hide_sync_feedback)

    def _hide_sync_feedback(self):
        if _safe(self._sync_feedback_lbl):
            self._sync_feedback_lbl.setVisible(False)

    # ── Visibility management ────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        # Poll less often than before — signals + debounce cover live changes;
        # this is a safety net for playtime / active-game banner.
        if not self._refresh_timer.isActive():
            self._refresh_timer.start(10_000)
        if not self._ts_timer.isActive():
            self._ts_timer.start()
        # Cover the enter refresh after the page paints. Periodic timer
        # refreshes stay silent.
        self.refresh_on_enter()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._refresh_timer.stop()
        self._ts_timer.stop()
        self._debounce.stop()
        # RAM cleanup on leaving the dashboard: the full-screen decoded
        # images (view cache, up to ~192 MB) are only ever shown inside the
        # add/edit dialog's viewer — the overview itself never uses them, so
        # keeping them after any refresh/leave is pure memory. Freeing here
        # costs nothing to this page.
        from ui.helpers import clear_view_cache as _clear_view_cache
        _clear_view_cache()

    def disconnect_signals(self):
        self._refresh_timer.stop()
        self._ts_timer.stop()
        self._debounce.stop()
        try:
            self._refresh_timer.timeout.disconnect(self.refresh)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_updated.disconnect(self.schedule_refresh)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_added.disconnect(self._on_game_added)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_removed.disconnect(self._on_game_removed)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().bulk_finished.disconnect(self._on_bulk_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            get_backup_manager().backup_created.disconnect(self._on_backup_created)
        except (RuntimeError, TypeError):
            pass
        try:
            orch = get_orchestrator()
            orch.sync_finished.disconnect(self._on_sync_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            orch = get_orchestrator()
            orch.providers_updated.disconnect(self.schedule_refresh)
        except (RuntimeError, TypeError):
            pass

    def update_locale(self):
        if not _safe(self._header):
            return
        self._header.setText(t("overview.title"))
        self._activity_header.setText(t("overview.recent_activity"))
        self._actions_header.setText(t("overview.quick_actions"))
        for btn, key in self._action_btns:
            if _safe(btn):
                btn.setText(t(key))
        if _safe(self._donut_header):
            self._donut_header.setText(t("overview.sync_distribution"))
        if _safe(self._bar_header):
            self._bar_header.setText(t("overview.backup_activity"))
        if _safe(self._activity_empty):
            self._activity_empty.setText(t("overview.no_activity"))
        self._refresh_btn.setToolTip(t("tooltips.refresh"))
        # Stat card labels — use unique objectNames to find the correct label
        for card_name, key in [("stat_card_games", "overview.stat_games"),
                               ("stat_card_backups", "overview.stat_backups"),
                               ("stat_card_sync", "overview.stat_synced"),
                               ("stat_card_playtime", "overview.stat_playtime")]:
            card = self.findChild(QFrame, card_name)
            if card:
                lbl = card.findChild(QLabel, "stat_label")
                if lbl:
                    lbl.setText(t(key))
        # Refresh to update dynamic text. The activity rows' subtitles
        # ("Played •", "Backup •", …) live in widgets the rebuild skips when
        # its cache key still matches — the key tracks data, not language —
        # so drop it first or the rows keep the old locale until the next
        # minute tick or data change.
        self._activity_cache_key = None
        self.refresh()

        # Recovered from a SECOND update_locale that used to sit further up this
        # class. Python keeps the last definition, so none of the below ran: the
        # header, the refresh tooltip, the four stat-card labels, the section
        # headers and the action buttons all stayed in the language the page was
        # built in. Anything added here has to stay in this one method.
        if _safe(self._header):
            self._header.setText(t("overview.title"))
        if _safe(self._refresh_btn):
            self._refresh_btn.setToolTip(
                t("tooltips.refresh_in_game" if self._in_game else "tooltips.refresh"))
        if _safe(self._card_games):
            self._card_games.set_stat_label(t("overview.stat_games"))
        if _safe(self._card_backups):
            self._card_backups.set_stat_label(t("overview.stat_backups"))
        if _safe(self._card_synced):
            self._card_synced.set_stat_label(t("overview.stat_synced"))
        if _safe(self._card_playtime):
            self._card_playtime.set_stat_label(t("overview.stat_playtime"))
        if _safe(self._activity_header):
            self._activity_header.setText(t("overview.recent_activity"))
        if _safe(self._actions_header):
            self._actions_header.setText(t("overview.quick_actions"))
        for btn, label_key in getattr(self, "_action_btns", []):
            if _safe(btn):
                btn.setText(t(label_key))
        if _safe(self._active_backup_btn):
            self._active_backup_btn.setText(t("buttons.backup_now"))

    # ── Stats for overlay ─────────────────────────────────────────────────────

    def get_stats_for_overlay(self) -> dict:
        lib   = get_library()
        games = lib.all_games()
        mgr   = get_backup_manager()
        orch  = get_orchestrator()
        active = get_monitor().currently_playing()

        all_backups = mgr.get_all_backups()
        last_bk = None
        for bk in all_backups:
            if last_bk is None or bk.created_at > last_bk:
                last_bk = bk.created_at
        last_bk_str = t("library.never")
        if last_bk:
            try:
                last_bk_str = _fmt_relative(datetime.fromisoformat(last_bk))
            except ValueError:
                pass

        active_engine = ""
        if active:
            from core.engines.game_engine import engine_display, engine_for_game
            active_engine = engine_display(engine_for_game(active[0]))
        return {
            "active_game":   active[0].name if active else None,
            "active_engine": active_engine,
            "library_count": len(games),
            "last_backup":   last_bk_str,
            "sync_status":   t("common.online") if orch.is_online() else t("common.offline"),
            "provider":      ", ".join(get_config().get("sync_providers", [])) or t("common.none"),
        }


def _fmt_relative(dt: datetime) -> str:
    from datetime import timezone
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    delta   = datetime.now(timezone.utc).replace(tzinfo=None) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:    return t('overview.just_now')  # handle future dates (clock skew)
    if seconds < 60:   return t('overview.just_now')
    if seconds < 3600: return t('overview.minutes_ago', n=seconds // 60)
    if seconds < 86400:return t('overview.hours_ago', n=seconds // 3600)
    return t('overview.days_ago', n=max(1, delta.days))
