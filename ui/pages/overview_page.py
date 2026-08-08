"""
SaveSync - Overview Page
Live dashboard: active game, library stats, recent backups, sync status.
Widget access is guarded via ui.helpers.safe_widget to avoid C++ deleted
object crashes.
"""
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QSizePolicy, QToolTip
)

from i18n import t
from ui.helpers import safe_widget as _safe
from ui.styles.theme import palette, ThemedMixin
from core.config_manager import get_config
from core.library import get_library
from core.backup import get_backup_manager
from core.monitor import get_monitor
from sync import get_orchestrator


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
                min-width: 120px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        val_lbl = QLabel(value)
        self._sty(val_lbl, lambda: f"color: {palette(self._accent_key)}; font-size: 26px; font-weight: 700; background: transparent;")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        lbl = QLabel(label)
        lbl.setObjectName("stat_label")
        layout.addWidget(val_lbl)
        layout.addWidget(lbl)
        self._val_lbl = val_lbl
        self._lbl     = lbl


class ActivityRow(QFrame, ThemedMixin):
    """One line in the "recent activity" list.

    Every piece of it is named and styled by the theme (#activity_row and
    friends): none of the five looks varies with the row's content, and a
    busy overview holds ten of these, so a theme switch has nothing to
    re-apply here.
    """

    def __init__(self, icon: str, title: str, subtitle: str, time_str: str):
        super().__init__()
        self.setObjectName("activity_row")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(12)

        icon_lbl = QLabel(icon)
        icon_lbl.setObjectName("activity_icon")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("activity_title")
        title_lbl.setMinimumWidth(60)
        # Elide long titles with "…" instead of disappearing
        title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("activity_sub")
        sub_lbl.setMinimumWidth(60)
        sub_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        text_col.addWidget(title_lbl)
        text_col.addWidget(sub_lbl)
        row.addLayout(text_col, 1)

        time_lbl = QLabel(time_str)
        time_lbl.setObjectName("activity_time")
        time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_lbl.setMinimumWidth(40)
        time_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        row.addWidget(time_lbl)
        # Expose label for live timestamp refresh without full rebuild
        self._ts_lbl = time_lbl


class SyncDonutChart(QWidget, ThemedMixin):
    """Donut chart showing sync status distribution using QPainter.

    Slice colours are stored as palette KEYS (e.g. ``"success"``) and resolved
    live inside ``paintEvent`` via ``palette(key)``, so a theme switch re-themes
    every slice with just a repaint — ``refresh_styles()`` only needs ``update()``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[tuple[str, int, str]] = []  # [(label, count, color_key), ...]
        self._total = 0
        self.setMinimumSize(180, 200)

    def set_data(self, data: list[tuple[str, int, str]]):
        # Third element is a palette KEY (resolved in paintEvent), not a hex.
        self._data = [(l, c, col) for l, c, col in data if c > 0]
        self._total = sum(c for _, c, _ in self._data)
        self.update()

    def refresh_styles(self):
        # Colours are read live from palette() in paintEvent, so a repaint is
        # all that's needed to pick up the new theme.
        super().refresh_styles()
        self.update()

    def paintEvent(self, event):
        if not self._data or self._total == 0:
            return
        if self.width() < 2 or self.height() < 2:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        legend_h = len(self._data) * 18 + 8
        chart_size = min(w - 16, h - legend_h - 8, 160)
        if chart_size < 40:
            painter.end()
            return
        thickness = max(chart_size // 5, 10)
        cx = w / 2
        cy = (h - legend_h) / 2
        rect = QRectF(cx - chart_size / 2, cy - chart_size / 2, chart_size, chart_size)

        start = 90 * 16  # start at top (Qt uses 1/16th degrees, clockwise negative)
        for _, count, color_key in self._data:
            span = int(-count / self._total * 360 * 16)
            pen = QPen(QColor(palette(color_key)), thickness)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawArc(rect, start, span)
            start += span

        # Center text
        painter.setPen(QColor(palette('text')))
        font = QFont()
        font.setPixelSize(max(chart_size // 5, 14))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self._total))

        font.setPixelSize(max(chart_size // 8, 9))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(palette('text_muted')))
        label_rect = QRectF(rect.x(), rect.y() + chart_size // 4, rect.width(), rect.height())
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, t('overview.chart_total'))

        # Legend
        ly = h - legend_h + 4
        font.setPixelSize(11)
        painter.setFont(font)
        for label, count, color_key in self._data:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette(color_key)))
            painter.drawEllipse(int(8), int(ly + 2), 8, 8)
            painter.setPen(QColor(palette('text_secondary')))
            painter.drawText(22, int(ly + 11), f"{label}  {count}")
            ly += 18

        painter.end()


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
        self.setMinimumHeight(110)
        self.setMaximumHeight(130)
        # Enable per-day hover tooltips showing the backup count for that day.
        self.setMouseTracking(True)

    def set_data(self, bars: list[tuple]):
        # (label, count, tooltip); a missing tooltip defaults to "".
        self._bars = [(b[0], int(b[1]), b[2] if len(b) > 2 else "") for b in bars]
        self._max_val = max((c for _, c, _ in self._bars), default=0)
        self._hit_regions = []   # rebuilt on next paint — avoid stale tooltips
        self.update()

    def refresh_styles(self):
        # Colours are read live from palette() in paintEvent — repaint to re-theme.
        super().refresh_styles()
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
        if not self._bars:
            return
        if self.width() < 2 or self.height() < 2:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin_bottom = 22
        margin_top = 8
        bar_area_h = h - margin_bottom - margin_top
        n = len(self._bars)
        gap = 6
        bar_w = max((w - gap * (n + 1)) / n, 8)
        accent = QColor(palette('accent'))
        muted = QColor(palette('text_hint'))

        self._hit_regions = []
        for i, (label, count, tip) in enumerate(self._bars):
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
                font.setPixelSize(9)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(QRectF(x, y - 14, bar_w, 14),
                                 Qt.AlignmentFlag.AlignCenter, str(count))

            # Day label
            painter.setPen(muted)
            font = QFont()
            font.setPixelSize(9)
            painter.setFont(font)
            painter.drawText(QRectF(x, h - margin_bottom + 2, bar_w, margin_bottom),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)

        painter.end()


class OverviewPage(QWidget, ThemedMixin):
    backup_requested = Signal(str)
    open_library     = Signal()
    open_sync        = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        # Separate timer for relative-time labels (e.g. "2 min fa") — runs every 60s
        # so timestamps update without triggering a full data reload.
        self._ts_timer = QTimer(self)
        self._ts_timer.setInterval(60_000)
        self._ts_timer.timeout.connect(self._refresh_timestamps_only)
        self._activity_cache_key = None  # (game_count, backup_count) — skip re-sort if unchanged
        # Provider-label colour depends on online state; read by _provider_style()
        # (registered once in _build_body and re-applied on refresh/theme switch).
        self._provider_online = False
        self._build()
        self._connect_signals()
        self.refresh()

    def _connect_signals(self):
        """Connect to library and sync signals for immediate refresh."""
        self._on_game_added = lambda _: self.refresh()
        self._on_game_removed = lambda _: self.refresh()
        self._on_backup_created = lambda _: self.refresh()
        self._on_sync_finished = lambda *_: self.refresh()
        try:
            get_library().game_updated.connect(self.refresh)
            get_library().game_added.connect(self._on_game_added)
            get_library().game_removed.connect(self._on_game_removed)
        except Exception:
            pass
        try:
            get_backup_manager().backup_created.connect(self._on_backup_created)
        except Exception:
            pass
        try:
            orch = get_orchestrator()
            orch.sync_finished.connect(self._on_sync_finished)
            orch.providers_updated.connect(self.refresh)
        except Exception:
            pass

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(24)

        # Header
        header = QHBoxLayout()
        self._header = QLabel(t("overview.title"))
        self._header.setObjectName("page_header")
        header.addWidget(self._header)
        header.addStretch()
        self._refresh_btn = QPushButton(t("buttons.refresh_icon"))
        self._refresh_btn.setObjectName("icon_btn")
        self._refresh_btn.setToolTip(t("tooltips.refresh"))
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)

        # Active game banner
        self._active_banner = QFrame()
        self._active_banner.setFrameShape(QFrame.Shape.NoFrame)
        self._active_banner.setObjectName("active_banner")
        self._update_banner_style()
        bl = QHBoxLayout(self._active_banner)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(12)
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

        self._build_body(root)

    def _update_banner_style(self):
        """Nothing to do: #active_banner (gradient, border, accent edge) is
        defined per theme in DARK_THEME/LIGHT_THEME."""

    def _provider_style(self) -> str:
        """Provider-label style — colour depends on the current online state."""
        key = 'accent' if self._provider_online else 'text_muted'
        return f"color:{palette(key)};font-size:11px;padding:4px;"

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

    def _build_body(self, root):
        """Continuation of _build — stat cards, charts, activity, actions."""
        # Stat cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self._card_games   = StatCard("0", t("overview.stat_games"),    "accent",  card_key="games")
        self._card_backups = StatCard("0", t("overview.stat_backups"),  "info",    card_key="backups")
        self._card_synced  = StatCard("0", t("overview.stat_synced"),   "cloud",   card_key="sync")
        self._card_playtime = StatCard("0", t("overview.stat_playtime"), "warning", card_key="playtime")
        for c in (self._card_games, self._card_backups, self._card_synced, self._card_playtime):
            cards_row.addWidget(c, 1)
        root.addLayout(cards_row)

        # Body — 3 columns: activity | donut | actions
        body = QHBoxLayout()
        body.setSpacing(16)

        # Column 1: recent activity (stretches)
        activity_col = QVBoxLayout()
        activity_col.setSpacing(8)
        self._activity_header = QLabel(t("overview.recent_activity"))
        self._activity_header.setObjectName("section_header")
        activity_col.addWidget(self._activity_header)

        self._activity_frame = QFrame()
        self._activity_frame.setFrameShape(QFrame.Shape.NoFrame)
        self._activity_frame.setObjectName("panel_card")
        self._activity_layout = QVBoxLayout(self._activity_frame)
        self._activity_layout.setContentsMargins(12, 8, 12, 8)
        self._activity_layout.setSpacing(0)
        self._activity_empty = QLabel(t("overview.no_activity"))
        self._activity_empty.setObjectName("empty_hint")
        self._activity_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # NO setVisible(True) here: the label has no parent yet, so it
        # would be SHOWN AS A TOP-LEVEL WINDOW for one frame (the startup
        # flash at screen centre). Once added to the layout below it is
        # visible with its parent anyway; refresh() manages it from there.
        self._activity_layout.addWidget(self._activity_empty)

        # Wrap in a QScrollArea so rows are never crushed/hidden when the window
        # is made narrow — the panel scrolls vertically and never clips content.
        activity_scroll = QScrollArea()
        activity_scroll.setWidgetResizable(True)
        activity_scroll.setFrameShape(QFrame.Shape.NoFrame)
        activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        activity_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        activity_scroll.setObjectName("activity_scroll")
        activity_scroll.setWidget(self._activity_frame)
        # Minimum width keeps the column readable at small window sizes
        activity_scroll.setMinimumWidth(260)

        activity_col.addWidget(activity_scroll, 1)
        body.addLayout(activity_col, 2)   # weight 2: activity gets more space than donut/actions

        # Column 2: donut chart (fixed width)
        donut_col = QVBoxLayout()
        donut_col.setSpacing(4)
        donut_header = QLabel(t("overview.sync_distribution"))
        donut_header.setObjectName("section_header")
        donut_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        donut_col.addWidget(donut_header)
        self._donut_header = donut_header

        self._donut_chart = SyncDonutChart()
        self._donut_chart.setMinimumWidth(160)
        donut_col.addWidget(self._donut_chart, 1)
        body.addLayout(donut_col, 1)

        # Column 3: quick actions (fixed width)
        actions_col = QVBoxLayout()
        actions_col.setSpacing(8)
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
            btn.setMinimumHeight(34)
            btn.setFixedWidth(160)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("quick_action_btn")
            btn.clicked.connect(cb)
            actions_col.addWidget(btn)

        self._provider_lbl = QLabel()
        self._provider_lbl.setFixedWidth(160)
        # Colour tracks online/offline state (self._provider_online); registered
        # once so refresh_styles() re-applies the CURRENT state with the new theme.
        self._sty(self._provider_lbl, self._provider_style)
        self._provider_lbl.setWordWrap(True)
        actions_col.addWidget(self._provider_lbl)
        actions_col.addStretch()
        body.addLayout(actions_col)

        root.addLayout(body, 1)

        # Backup activity bar chart (full width, below body)
        bar_header = QLabel(t("overview.backup_activity"))
        bar_header.setObjectName("section_header")
        self._bar_header = bar_header
        root.addWidget(bar_header)

        self._bar_chart = BackupBarChart()
        self._bar_chart.setObjectName("panel_card")
        root.addWidget(self._bar_chart)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh(self):
        """Refresh all live data — safe to call from GUI thread only."""
        if not _safe(self._header):
            return

        lib   = get_library()
        games = lib.all_games()
        mgr   = get_backup_manager()
        orch  = get_orchestrator()

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
        all_bk_flat = mgr.get_all_backups()
        all_bk = len(all_bk_flat)
        if _safe(self._card_games):
            self._card_games._val_lbl.setText(str(len(games)))
        if _safe(self._card_backups):
            self._card_backups._val_lbl.setText(str(all_bk))
        if _safe(self._card_synced):
            self._card_synced._val_lbl.setText(
                str(sum(1 for g in games if g.sync_status == "synced"))
            )
        if _safe(self._card_playtime):
            total_secs = sum(g.playtime_seconds for g in games)
            hours = total_secs // 3600
            mins = (total_secs % 3600) // 60
            self._card_playtime._val_lbl.setText(f"{hours}h {mins}m" if hours else f"{mins}m")

        # Donut chart — sync status distribution
        if _safe(self._donut_chart):
            status_map = {"synced": 0, "pending": 0, "conflict": 0, "local_only": 0,
                          "cloud_only": 0, "no_saves": 0, "provisional": 0}
            # Games with no confirmed save_paths yet but that DO have at
            # least one live-tracking-discovered provisional backup get
            # their own bucket instead of being lumped in with "no saves"
            # — there IS restorable data, the user just hasn't confirmed
            # which paths to keep yet.
            _provisional_game_ids = {
                b.game_id for b in get_backup_manager().get_all_backups()
                if (b.cloud_metadata or {}).get("pre_confirmation")
            }
            for g in games:
                if not g.save_paths:
                    s = "provisional" if g.id in _provisional_game_ids else "no_saves"
                else:
                    s = g.sync_status if g.sync_status in status_map else "no_saves"
                status_map[s] += 1
            # Third element is a palette KEY — the donut resolves it live in
            # paintEvent, so slices re-theme on a light/dark switch (via
            # refresh_styles -> update()) without needing a data refresh.
            donut_data = [
                (t("library.status_synced"),      status_map["synced"],      'success'),
                (t("library.status_pending"),     status_map["pending"],     'warning'),
                (t("library.status_conflict"),    status_map["conflict"],    'error'),
                (t("library.status_local_only"),  status_map["local_only"],  'info'),
                (t("library.status_cloud_only"),  status_map["cloud_only"],  'cloud'),
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
            for bk in all_bk_flat:
                # Bucket by LOCAL date (created_at is naive UTC): a backup
                # made at 00:30 local must count on the local day, not on
                # the previous UTC day.
                from core import to_local_dt
                _bk_dt = to_local_dt(bk.created_at)
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
        self._provider_lbl.setStyleSheet(self._provider_style())

        self._rebuild_activity(games, mgr, all_bk_flat)

    def _refresh_timestamps_only(self):
        """Update only the relative-time labels in the activity list.

        Called every 60 s by _ts_timer.  Avoids a full data reload when all
        that changed is "2 min fa" → "3 min fa".  Also invalidates the
        activity cache key so the next full refresh sees the change.
        """
        self._activity_cache_key = None   # force rebuild on next refresh()
        # Walk existing ActivityRow widgets and update only their timestamp label
        for i in range(self._activity_layout.count()):
            item = self._activity_layout.itemAt(i)
            w = item.widget() if item else None
            if w is None or w is self._activity_empty:
                continue
            # ActivityRow stores its raw timestamp in _raw_ts
            raw_ts = getattr(w, '_raw_ts', None)
            ts_lbl = getattr(w, '_ts_lbl', None)
            if raw_ts and ts_lbl and _safe(ts_lbl):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(raw_ts)
                    ts_lbl.setText(_fmt_relative(dt))
                except (ValueError, TypeError):
                    pass

    def _rebuild_activity(self, games, mgr, all_backups=None):
        """Rebuild activity rows."""
        if not _safe(self._activity_layout):
            return

        # Skip full rebuild if underlying data hasn't changed.
        # Hash all backup IDs so mid-list deletions are detected
        # (previously only the last backup was checked).
        bk_list = all_backups if all_backups is not None else mgr.get_all_backups()
        bk_ids_hash = hash(tuple(b.backup_id for b in bk_list)) if bk_list else 0
        game_names_hash = hash(tuple(g.name for g in games)) if games else 0
        game_mod_hash = hash(tuple((g.id, g.last_played or "", g.last_synced or "") for g in games)) if games else 0
        from datetime import datetime as _dt2
        # Include current minute in cache key so relative timestamps ("2 min fa")
        # are recalculated every minute even when library data hasn't changed.
        _now_minute = _dt2.now().strftime("%Y-%m-%dT%H:%M")
        cache_key = (len(games), len(bk_list),
                     games[-1].id if games else "",
                     bk_ids_hash,
                     game_names_hash,
                     game_mod_hash,
                     _now_minute)
        if cache_key == self._activity_cache_key:
            return
        self._activity_cache_key = cache_key
        all_backups = bk_list

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

        # Build events list — use pre-fetched backups
        # all_backups is guaranteed non-None here: resolved at line 316 via bk_list
        bk_by_game: dict[str, list] = {}
        for bk in all_backups:
            bk_by_game.setdefault(bk.game_id, []).append(bk)
        # Sort each game's backups by date desc and take top 2
        for gid in bk_by_game:
            bk_by_game[gid].sort(key=lambda b: b.created_at, reverse=True)

        events = []
        for g in games:
            for bk in bk_by_game.get(g.id, [])[:2]:
                events.append(("💾", g.name, t('overview.backup_prefix') + f" {bk.size_human}", bk.created_at))
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

        events.sort(key=lambda e: e[3] or "", reverse=True)

        if not events:
            if _safe(self._activity_empty):
                self._activity_empty.setVisible(True)
            return

        if _safe(self._activity_empty):
            self._activity_empty.setVisible(False)

        for icon, title, subtitle, ts in events[:5]:
            try:
                dt   = datetime.fromisoformat(ts)
                tstr = _fmt_relative(dt)
            except (ValueError, TypeError):
                tstr = ""
            row = ActivityRow(icon, title, subtitle, tstr)
            row._raw_ts = ts   # raw ISO timestamp for live refresh
            self._activity_layout.addWidget(row)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_backup_active(self):
        gid = self._active_backup_btn.property("_game_id")
        if gid:
            self.backup_requested.emit(gid)

    def _backup_all(self):
        """Emit backup requests with staggered delays to avoid UI freeze."""
        games = [g for g in get_library().all_games() if g.save_paths]
        for i, g in enumerate(games):
            QTimer.singleShot(i * 100, lambda gid=g.id: self.backup_requested.emit(gid))

    def _sync_all(self):
        """Sync games that may have changes — same skip rules as Sync page.

        Unchanged games are left alone so library cards / recent activity are
        not stamped for empty up/down runs. The Sync page history still logs
        any run that does go out.
        """
        orch = get_orchestrator()
        if not orch.is_online():
            return
        from core.backup import get_backup_manager
        bm = get_backup_manager()
        for g in get_library().all_games():
            if not g.save_paths:
                continue
            if g.sync_status == "synced":
                recents = bm.get_backups_for_game(g.id)
                if recents:
                    current_hash = (recents[0].cloud_metadata or {}).get("save_hash", "")
                    synced_hash = (g.cloud_metadata or {}).get("last_synced_hash", "")
                    if current_hash and current_hash == synced_hash:
                        continue
                elif not g._saves_changed_since_sync():
                    continue
            orch.sync_game(
                g.id, g.name, g.save_paths,
                exe_path=g.exe_path,
                computed_folder_name=g.computed_folder_name,
            )

    # ── Visibility management ────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start(5000)
        if not self._ts_timer.isActive():
            self._ts_timer.start()
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._refresh_timer.stop()
        self._ts_timer.stop()

    def disconnect_signals(self):
        self._refresh_timer.stop()
        self._ts_timer.stop()
        try:
            self._refresh_timer.timeout.disconnect(self.refresh)
        except (RuntimeError, TypeError):
            pass
        try:
            get_library().game_updated.disconnect(self.refresh)
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
            orch.providers_updated.disconnect(self.refresh)
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
