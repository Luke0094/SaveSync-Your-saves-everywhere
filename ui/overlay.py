"""
SaveSync - Overlay Widget
Frameless always-on-top overlay.
- Auto-hides after 5 s with fade animation; timer pauses on hover
- Above fullscreen (Windows HWND_TOPMOST)
- Thread-safe: all public methods must be called from GUI thread
- hide_animated uses flag to avoid RuntimeWarning
"""
import html
import logging
import platform
import re
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, Slot, QPoint
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QCursor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QApplication

from i18n import t
from ui.backup_labels import origin_badge
from ui.styles.theme import palette

if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

logger = logging.getLogger(__name__)

_AUTO_HIDE_MS  = 5000    # default timeout if user doesn't hover
_FADE_IN_MS    = 200
_FADE_OUT_MS   = 280


from ui.helpers import (force_topmost as _force_topmost, popup_is_open,
                        ScreenSignalMixin, SystemCursor, TRACE_Z, z_report)


def _engine_badge_html(engine: str) -> str:
    """Muted label for an engine name, or '' when there is nothing to show."""
    engine = (engine or "").strip()
    if not engine:
        return ""
    return (
        f" <span style='color:{palette('text_muted')};font-size:11px;"
        f"font-weight:500;'>· {html.escape(engine)}</span>"
    )


def _engine_label_for_exe(exe_path: str = "") -> str:
    """Display label for the engine of the running / named game, or ''."""
    from core.engines.game_engine import engine_display, engine_for_game
    from core.library import get_library
    from core.monitor import get_monitor

    entry = None
    if exe_path:
        entry = get_library().get_by_exe(exe_path)
    if entry is None:
        playing = get_monitor().currently_playing()
        if playing:
            entry = playing[0]
    return engine_display(engine_for_game(entry)) if entry else ""


def _remote_game_folder(orch, provider, entry, game_id: str) -> str:
    """Version/build-insensitive remote folder for a game: the current
    install-folder name plus every historical name/folder, resolved against
    the provider's actual remote folders. Shared by the restore-list fetch
    and the quick-restore download."""
    from core.constants import get_install_folder_name, get_folder_name_for_save
    candidates = [get_install_folder_name(
        entry.exe_path, entry.name, game_id, entry.computed_folder_name)]
    for hn in entry.name_history:
        fn = get_folder_name_for_save(hn, entry.exe_path or "", game_id)
        if fn not in candidates:
            candidates.append(fn)
    for fn in (entry.folder_history or []):
        if fn and fn not in candidates:
            candidates.append(fn)
    return orch.resolve_remote_game_folder(provider, candidates) or candidates[0]


# Recent pins beyond this many scroll instead of growing the menu.
_PIN_MENU_ROWS = 5


class OverlayWidget(QWidget, ScreenSignalMixin):
    action_requested  = Signal(str, str)   # action, context
    dismissed         = Signal()
    dont_show_again   = Signal(str)        # exe_path
    # Emitted when exclusive fullscreen prevents the overlay from showing.
    # Carries (title, message) so the caller can provide audio/toast feedback.
    exclusive_blocked = Signal(str, str)
    # Provider restore-list fetch completed (payload: entries, game_id,
    # local_ids, seq). Emitted FROM THE WORKER THREAD — a queued signal is
    # the only safe hop back to the GUI thread; the old QTimer.singleShot
    # from the plain thread never fired (no Qt event loop there), which
    # left the list stuck on "loading" forever.
    _restore_fetch_done = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context_exe         = ""
        self._hide_anim_connected = False
        self._hover_active        = False   # True while mouse is inside overlay
        self._pending_auto_hide   = 0       # ms to resume after mouse leaves

        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.hide_animated)

        # Timer to periodically re-assert topmost status for fullscreen games
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_topmost)
        self._topmost_timer.setInterval(1000)  # Check every second

        # Unity (and similar) re-clip the pointer every frame in fullscreen.
        # Releasing ClipCursor once at show is not enough — keep undoing it
        # for as long as the overlay holds the system cursor.
        # ~60 Hz: Unity re-hides/re-clips every frame; 100 ms left a gap.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.timeout.connect(SystemCursor.reassert)
        self._cursor_timer.setInterval(16)

        # Foreground change hook — reacts instantly when another window
        # covers the overlay (e.g. game entering fullscreen while visible)
        self._fg_hook = None          # WinEventHook handle
        self._fg_hook_proc = None     # prevent GC of the ctypes callback

        # Notification queue: list of (icon, title, message, auto_hide_ms)
        # plus parallel (game_id, notif_type) metadata for the suppress link.
        self._notif_queue: list[tuple[str, str, str, int]] = []
        self._notif_meta: list[tuple[str, str]] = []
        self._notif_index: int = 0

        # Unknown-game queue browsing: when non-None, the overlay is showing
        # the pending unknown-game detections and the carousel arrows step
        # through THIS list instead of the notification queue. The badge is
        # clickable only while a known game's tracking toast is on screen
        # (mode "tracking") — everywhere else it's a passive counter; on the
        # unknown view itself the arrows already browse the queue.
        self._unknown_queue: list[dict] | None = None
        self._unknown_index: int = 0
        self._overlay_mode: str = ""

        # Priority prompts (cloud-save decisions) must not be overwritten by
        # later notifications: while one is showing, other show_* calls are
        # deferred here and replayed once the prompt is resolved (user action
        # or the overlay being closed). See _defer_if_priority / hide_animated.
        self._priority_active: bool = False
        self._priority_context: str = ""   # exe_path of the active priority prompt
        self._deferred_notifs: list = []   # which queued item is displayed

        self._setup_window()
        self._build_ui()
        self._setup_animation()
        self._connect_screen_changes()
        # Worker-thread → GUI-thread hop for the provider restore-list fetch
        # (queued automatically because the emit happens off-thread).
        self._restore_fetch_done.connect(self._on_restore_fetch_done)

    # ── Window ────────────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool                 |
            Qt.WindowType.NoDropShadowWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)  # Linux support
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Explicit arrow so immersive games that set a NULL cursor still get
        # a pointer shape while the mouse is over this panel.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setObjectName("overlay")
        self.setFixedWidth(340)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Header: [pending-unknown badge] title + [i] save-scan shortcut +
        # close button
        header = QHBoxLayout()
        # Top-left badge: dynamic count of PENDING unknown-game detections
        # (the recallable queue — see ui/unknown_history.py). Visible only
        # when the queue is non-empty. Clickable ONLY over the tracking
        # toast, where it swaps the notification for the unknown queue
        # in place; on the unknown view itself the carousel arrows already
        # browse the queue, so there the badge is a passive counter.
        self._unknown_badge = QPushButton("")
        self._unknown_badge.setFixedHeight(20)
        self._unknown_badge.setToolTip(t("unknown_history.badge_tooltip"))
        self._unknown_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._unknown_badge.clicked.connect(self._on_badge_clicked)
        self._unknown_badge.setVisible(False)
        self._refresh_badge_interactivity()
        self._title = QLabel(t("app.name"))
        self._title.setObjectName("overlay_title")
        # [i]: opens the automatic save-path scan panel so detected files
        # can be reviewed/accepted WITHOUT leaving the game. Shown only on
        # the manual (hotkey) overlay — see show_manual/_clear_buttons.
        self._info_btn = QPushButton("i")
        self._info_btn.setFixedSize(20, 20)
        self._info_btn.setToolTip(t("overlay.open_auto_scan"))
        self._info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._info_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._info_btn.setStyleSheet(
            f"QPushButton{{color:{palette('text_muted')};background:transparent;"
            f"border:1px solid {palette('border_hover')};border-radius:10px;"
            f"font-size:11px;font-weight:700;font-style:italic;padding:0;}}"
            f"QPushButton:hover{{color:{palette('accent')};border-color:{palette('accent')};}}"
        )
        self._info_btn.clicked.connect(lambda: self._on_action("open_auto_scan"))
        self._info_btn.setVisible(False)
        # 📌: the recently pinned notes/images, plus a way to pin a new one,
        # reachable without leaving the game. Same visibility rule as [i] —
        # only on the manual (hotkey) overlay, see _clear_buttons.
        self._pin_btn = QPushButton("📌")
        self._pin_btn.setFixedSize(20, 20)
        self._pin_btn.setToolTip(t("pin.menu_tooltip"))
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pin_btn.setObjectName("icon_btn")
        self._pin_btn.clicked.connect(self._show_pin_menu)
        self._pin_btn.setVisible(False)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("icon_btn")
        close_btn.setFixedSize(20, 20)
        close_btn.clicked.connect(self.hide_animated)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header.addWidget(self._unknown_badge)
        header.addWidget(self._title, 1)
        header.addWidget(self._pin_btn)
        header.addWidget(self._info_btn)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # ── Message area with flanking carousel arrows ────────────────────────
        # Layout: [‹ prev] [icon + message area] [next ›]
        # Arrows are hidden when only one notification is queued.
        msg_row = QHBoxLayout()
        msg_row.setSpacing(4)
        msg_row.setContentsMargins(0, 0, 0, 0)

        self._carousel_prev = QPushButton("‹")
        self._carousel_prev.setFixedSize(18, 48)
        self._carousel_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self._carousel_prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._carousel_prev.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._carousel_prev.setVisible(False)
        self._carousel_prev.clicked.connect(self._carousel_go_prev)

        self._carousel_next = QPushButton("›")
        self._carousel_next.setFixedSize(18, 48)
        self._carousel_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._carousel_next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._carousel_next.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._carousel_next.setVisible(False)
        self._carousel_next.clicked.connect(self._carousel_go_next)
        self._style_carousel_arrows()

        # Centre column: icon + message stack
        centre_col = QVBoxLayout()
        centre_col.setSpacing(4)
        centre_col.setContentsMargins(0, 0, 0, 0)

        # Icon label
        self._icon_label = QLabel("💾")
        self._icon_label.setObjectName("overlay_icon")
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        centre_col.addWidget(self._icon_label)

        # Message label
        self._message = QLabel()
        self._message.setObjectName("overlay_message")
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.TextFormat.RichText)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Two lines for "in esecuzione / monitoraggio attivo" (and similar).
        self._message.setFixedHeight(52)
        centre_col.addWidget(self._message)

        # Counter (e.g. "2 / 5") below message, centred
        self._carousel_counter = QLabel("")
        self._carousel_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._carousel_counter.setStyleSheet(
            f"color:{palette('text_muted')};font-size:9px;"
        )
        self._carousel_counter.setVisible(False)
        centre_col.addWidget(self._carousel_counter)

        msg_row.addWidget(self._carousel_prev, 0)
        msg_row.addLayout(centre_col, 1)
        msg_row.addWidget(self._carousel_next, 0)
        layout.addLayout(msg_row)

        # Dashboard widget (for quick-restore list etc.)
        self._dashboard = QWidget()
        self._dashboard.setObjectName("transparent_bg")
        self._dashboard.setVisible(False)
        self._dashboard.setMaximumHeight(0)
        dl = QVBoxLayout(self._dashboard)
        dl.setContentsMargins(0, 4, 0, 0)
        dl.setSpacing(5)
        self._dash_rows: list[tuple] = []
        for _ in range(4):
            row = QHBoxLayout()
            row.setSpacing(8)
            # Named, not styled here: the look is fixed (#dash_key /
            # #dash_value in the theme). A highlighted row still overrides the
            # value's colour inline — see set_dash_row below.
            k = QLabel(); k.setObjectName("dash_key")
            v = QLabel(); v.setObjectName("dash_value")
            row.addWidget(k); row.addWidget(v, 1)
            dl.addLayout(row)
            self._dash_rows.append((k, v))
        layout.addWidget(self._dashboard)

        self._btn_area     = QHBoxLayout(); self._btn_area.setSpacing(6)
        self._quick_area   = QHBoxLayout(); self._quick_area.setSpacing(6)
        layout.addLayout(self._btn_area)
        layout.addLayout(self._quick_area)

        # Quick-restore backup list (hidden by default)
        self._restore_area = QVBoxLayout()
        self._restore_area.setSpacing(3)
        layout.addLayout(self._restore_area)

        self._suppress_btn = QPushButton()
        self._suppress_btn.setObjectName("icon_btn")
        self._suppress_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._suppress_btn.setStyleSheet(
            f"QPushButton{{font-size:10px;color:{palette('text_muted')};padding:4px 8px;"
            f"border:1px solid {palette('border_hover')};border-radius:4px;background:transparent;}}"
            f"QPushButton:hover{{color:{palette('text')};border-color:{palette('accent')};background:{palette('bg_elevated')};}}"
        )
        self._suppress_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._suppress_btn.clicked.connect(self._on_suppress)
        layout.addWidget(self._suppress_btn)

    def _setup_animation(self):
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ── Mouse hover — pauses auto-hide timer ─────────────────────────────────

    def enterEvent(self, event):
        """Mouse entered overlay — pause auto-hide countdown.

        Reads remainingTime() only when the timer is active, and clamps
        the value to 0 in case a TOCTOU race causes the timer to fire
        between isActive() and remainingTime() (which returns -1).
        """
        super().enterEvent(event)
        if self._auto_hide_timer.isActive():
            remaining = self._auto_hide_timer.remainingTime()
            self._pending_auto_hide = max(remaining, 0)
            self._auto_hide_timer.stop()
        self._hover_active = True

    def leaveEvent(self, event):
        """Mouse left overlay — resume auto-hide countdown."""
        super().leaveEvent(event)
        self._hover_active = False
        remaining = self._pending_auto_hide
        self._pending_auto_hide = 0
        if remaining > 0:
            self._auto_hide_timer.start(max(remaining, 1500))  # at least 1.5 s

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_buttons(self):
        # The [i] scan shortcut belongs to the manual overlay only —
        # show_manual re-enables it after this reset.
        if hasattr(self, "_info_btn"):
            self._info_btn.setVisible(False)
        if hasattr(self, "_pin_btn"):
            self._pin_btn.setVisible(False)
        for layout in (self._btn_area, self._quick_area, self._restore_area):
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w:
                    w.hide()        # keep childrenRect honest until deletion
                    w.deleteLater()
                # For spacer items, just let them be garbage collected

    def _add_btn(self, layout, text: str, action: str, primary=False):
        btn = QPushButton(text)
        if primary:
            btn.setObjectName("primary_btn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._on_action(action))
        layout.addWidget(btn)
        return btn

    def _add_split_btn(self, layout, text: str, action: str,
                       menu_items: list[tuple[str, str]], primary=True):
        """Primary button with an attached ▾ dropdown of alternative actions.

        *menu_items* is a list of (label, action) shown in a QMenu anchored
        to the arrow half; picking one routes through the same _on_action
        path as a plain button click.
        """
        from PySide6.QtWidgets import QMenu, QWidget as _QW

        wrap = _QW()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)

        main_btn = QPushButton(text)
        if primary:
            main_btn.setObjectName("primary_btn")
        main_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        main_btn.clicked.connect(lambda: self._on_action(action))

        arrow_btn = QPushButton("▾")
        if primary:
            arrow_btn.setObjectName("primary_btn")
        arrow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        arrow_btn.setFixedWidth(24)

        def _open_menu():
            menu = QMenu(self)
            menu.setStyleSheet(
                f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
                f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
                f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:11px;}}"
                f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
            )
            for label, act in menu_items:
                menu.addAction(label, lambda a=act: self._on_action(a))
            menu.exec(arrow_btn.mapToGlobal(arrow_btn.rect().bottomLeft()))

        arrow_btn.clicked.connect(_open_menu)

        row.addWidget(main_btn, 1)
        row.addWidget(arrow_btn)
        layout.addWidget(wrap)
        return main_btn

    def _set_dash(self, idx: int, key: str, val: str, accent: str = ""):
        k, v = self._dash_rows[idx]
        k.setText(key)
        v.setText(val)
        # Only a highlighted row needs a sheet of its own; clearing it lets
        # the theme's #dash_value take over again.
        v.setStyleSheet(f"color:{accent};font-size:11px;font-weight:600;" if accent else "")

    # Restore list sizing: cap the scrollable area so the overlay stays compact
    _RESTORE_ROW_H = 28
    _RESTORE_MAX_H = 190

    @Slot(str)
    def _show_restore_list(self, game_id: str):
        """Quick in-game restore with a SOURCE selector: one button for the
        local backups plus one per connected provider. The chosen source's
        backups are listed in a capped scrollable area — cloud entries are
        downloaded transparently on restore."""
        from PySide6.QtWidgets import QScrollArea

        # Clear previous entries. hide() BEFORE deleteLater: until the
        # deferred delete runs, a still-visible orphan child inflates
        # adjustSize()'s childrenRect and the card keeps the old height.
        while self._restore_area.count():
            item = self._restore_area.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self._restore_list_lay = None
        self._restore_game_id = game_id
        # Generation counter: invalidates in-flight provider fetches when
        # the user switches source (or the list is rebuilt).
        self._restore_load_seq = getattr(self, "_restore_load_seq", 0) + 1

        # ── Source selector: local + each connected provider ──────────────
        sources: list[tuple[str, str]] = [("local", f"💾 {t('backups.source_local')}")]
        try:
            from sync import get_orchestrator
            from ui.backup_labels import ORIGIN_LABELS
            for p in get_orchestrator().get_connected_providers():
                label = ORIGIN_LABELS.get(p.PROVIDER_ID, f"☁ {p.PROVIDER_ID}")
                sources.append((p.PROVIDER_ID, label))
        except Exception:
            pass

        self._restore_source_btns = {}
        if len(sources) > 1:
            sel_w = QWidget()
            sel_w.setObjectName("transparent_bg")
            sel_row = QHBoxLayout(sel_w)
            sel_row.setContentsMargins(0, 0, 0, 0)
            sel_row.setSpacing(4)
            for src_id, label in sources:
                b = QPushButton(label)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setFixedHeight(20)
                b.clicked.connect(lambda _=False, s=src_id: self._load_restore_source(s))
                sel_row.addWidget(b)
                self._restore_source_btns[src_id] = b
            sel_row.addStretch()
            self._restore_area.addWidget(sel_w)

        # ── Scrollable list container ──────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setObjectName("transparent_bg")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("transparent_bg")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 0, 0)
        inner_lay.setSpacing(3)
        inner_lay.addStretch()
        scroll.setWidget(inner)
        self._restore_list_lay = inner_lay
        self._restore_scroll = scroll
        self._restore_area.addWidget(scroll)

        self._load_restore_source("local")

    def _style_restore_source_btns(self, active: str):
        for src_id, b in getattr(self, "_restore_source_btns", {}).items():
            try:
                if src_id == active:
                    b.setStyleSheet(
                        f"QPushButton{{font-size:9px;color:{palette('accent_text')};"
                        f"background:{palette('accent')};border:1px solid {palette('accent')};"
                        f"border-radius:4px;padding:0 8px;font-weight:700;}}"
                    )
                else:
                    b.setStyleSheet(
                        f"QPushButton{{font-size:9px;color:{palette('text_muted')};"
                        f"background:transparent;border:1px solid {palette('border_hover')};"
                        f"border-radius:4px;padding:0 8px;}}"
                        f"QPushButton:hover{{color:{palette('accent')};border-color:{palette('accent')};}}"
                    )
            except RuntimeError:
                pass

    def _clear_restore_rows(self):
        """Remove all rows from the restore list, keeping the trailing stretch."""
        lay = getattr(self, "_restore_list_lay", None)
        if lay is None:
            return
        while lay.count() > 1:      # last item is the stretch
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()            # keep childrenRect honest until deletion
                w.deleteLater()

    def _load_restore_source(self, source_id: str):
        """Populate the restore list from *source_id*: 'local' or a
        connected provider id (fetched in a background thread)."""
        from core.backup import get_backup_manager, BackupEntry

        game_id = getattr(self, "_restore_game_id", "")
        lay = getattr(self, "_restore_list_lay", None)
        if not game_id or lay is None:
            return
        self._restore_load_seq += 1
        seq = self._restore_load_seq
        self._style_restore_source_btns(source_id)
        self._clear_restore_rows()

        bm = get_backup_manager()
        local_backups = bm.get_backups_for_game(game_id)

        if source_id == "local":
            if not local_backups:
                lbl = QLabel(f"<span style='color:{palette('text_hint')};font-size:10px;'>{t('overlay.no_backups_available')}</span>")
                lay.insertWidget(lay.count() - 1, lbl)
            for bk in local_backups:
                lay.insertWidget(lay.count() - 1,
                                 self._make_restore_btn(bk, game_id, cloud_only=False))
            self._resize_restore_scroll(max(1, len(local_backups)))
            return

        # Provider source: loading placeholder + background fetch
        loading = QLabel(f"<span style='color:{palette('text_hint')};font-size:10px;'>⟳ {t('overlay.loading')}</span>")
        lay.insertWidget(lay.count() - 1, loading)
        self._resize_restore_scroll(1)

        local_ids = {b.backup_id for b in local_backups}

        import threading

        def _bg_fetch(gid=game_id, pid=source_id, my_seq=seq):
            entries: list = []
            try:
                from sync import get_orchestrator
                from core.library import get_library
                orch = get_orchestrator()
                provider = orch.get_provider(pid)
                lib_entry = get_library().get_by_id(gid)
                if provider and provider.is_connected and lib_entry:
                    # Version/build-insensitive folder resolution: the remote
                    # folder may carry a version suffix from before an update.
                    game_folder = _remote_game_folder(orch, provider, lib_entry, gid)
                    for rd in provider.list_cloud_backups(game_folder):
                        bid = rd.get("backup_id", "")
                        if not bid:
                            continue
                        rd["origin"] = pid
                        # Record where the zip actually lives so the restore
                        # download targets the right remote folder.
                        rd.setdefault("cloud_metadata", {})["remote_folder"] = game_folder
                        try:
                            entries.append(BackupEntry.from_dict(rd))
                        except Exception:
                            pass
            except Exception:
                pass
            entries.sort(key=lambda b: b.created_dt, reverse=True)
            # NEVER QTimer.singleShot from here: this runs in a plain
            # thread with no Qt event loop, so the timer would never fire
            # and the list would stay on "loading" forever. A cross-thread
            # signal emit is queued to the GUI thread by Qt itself.
            self._restore_fetch_done.emit((entries, gid, local_ids, my_seq))

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _on_restore_fetch_done(self, payload):
        """GUI thread: unpack a finished provider fetch and fill the list."""
        try:
            entries, gid, local_ids, seq = payload
        except (TypeError, ValueError):
            return
        self._populate_provider_restore(entries, gid, local_ids, seq)

    def _populate_provider_restore(self, entries: list, game_id: str,
                                   local_ids: set, seq: int):
        """GUI thread: fill the restore list with a provider's backups.
        Stale results (source switched meanwhile) are discarded via *seq*."""
        try:
            if seq != getattr(self, "_restore_load_seq", -1):
                return
            lay = getattr(self, "_restore_list_lay", None)
            if lay is None:
                return
            self._clear_restore_rows()
            if not entries:
                lbl = QLabel(f"<span style='color:{palette('text_hint')};font-size:10px;'>{t('overlay.no_backups_available')}</span>")
                lay.insertWidget(lay.count() - 1, lbl)
            for bk in entries:
                # Already-downloaded backups restore from the local zip
                # directly; the rest download transparently first.
                is_cloud_only = bk.backup_id not in local_ids
                lay.insertWidget(lay.count() - 1,
                                 self._make_restore_btn(bk, game_id, cloud_only=is_cloud_only))
            self._resize_restore_scroll(max(1, len(entries)))
        except RuntimeError:
            pass  # Widget destroyed

    def _resize_restore_scroll(self, row_count: int):
        """Grow the restore scroll with its content up to the cap.

        Anchor contract: the card's TOP edge is frozen — however many
        backups the list holds, the card only ever extends DOWNWARD from
        where it already is (default position or wherever the user dragged
        it), never re-centering or drifting. If the expansion would cross
        the bottom of the screen, the excess is taken back from the scroll
        (which scrolls anyway) instead of moving the card.
        """
        scroll = getattr(self, "_restore_scroll", None)
        if scroll is None:
            return
        try:
            anchor = self.pos()
            h = min(max(1, row_count) * (self._RESTORE_ROW_H + 3) + 6, self._RESTORE_MAX_H)
            scroll.setFixedHeight(h)
            self.adjustSize()
            self.move(anchor)
            # Real geometry only exists after the deferred LayoutRequest
            # settles (sizeHint caches make synchronous readback stale), so
            # re-assert the anchor and clamp to the screen on the next
            # event-loop turn. This runs on the GUI thread, so the timer is
            # legitimate here.
            QTimer.singleShot(0, lambda a=anchor, hh=h: self._anchor_restore_card(a, hh))
        except RuntimeError:
            pass

    def _anchor_restore_card(self, anchor, h: int):
        """Deferred pass: freeze the card's top edge and keep it on-screen.

        By now the first resize has really been applied, so geometry() is
        trustworthy; the shrink that keeps the bottom edge on screen is
        derived arithmetically from that one measurement.
        """
        scroll = getattr(self, "_restore_scroll", None)
        if scroll is None or not self.isVisible():
            return
        try:
            self.adjustSize()
            self.move(anchor)
            screen = self._get_active_screen_geometry()
            cur_h = self.geometry().height()
            min_h = self._RESTORE_ROW_H + 9   # never below one visible row
            overflow = (anchor.y() + cur_h + 10) - screen.bottom()
            if overflow > 0:
                new_h = max(min_h, h - overflow)
                scroll.setFixedHeight(new_h)
                # Card dragged so low that even the one-row list can't fit:
                # only then lift the top edge, by the minimum necessary.
                final_h = cur_h - (h - new_h)
                still = (anchor.y() + final_h + 10) - screen.bottom()
                y = anchor.y() if still <= 0 else max(screen.top() + 10,
                                                      anchor.y() - still)
                final_pos = QPoint(anchor.x(), y)

                def _settle(pos=final_pos):
                    try:
                        self.adjustSize()
                        self.move(pos)
                    except RuntimeError:
                        pass

                _settle()
                # The shrink's LayoutRequest lands on the next loop turn;
                # only then does adjustSize see the reduced sizeHint.
                QTimer.singleShot(0, _settle)
        except Exception:
            pass

    def _make_restore_btn(self, bk, game_id: str, cloud_only: bool) -> QPushButton:
        # Local-time display: created_at is stored as naive UTC, so it MUST
        # go through to_local_dt (a plain fromisoformat showed raw UTC here).
        from core import to_local_dt
        from i18n import format_dt
        dt = to_local_dt(bk.created_at)
        if dt is not None:
            date_str = format_dt(dt, "%d %b  %H:%M")
        else:
            date_str = bk.created_at[:16] if bk.created_at else "?"

        badge = origin_badge(bk)
        # Temporary (pre-confirmation) backups — created during the first
        # session of a game whose save paths aren't confirmed yet — are
        # exactly what this in-game list exists to restore, so flag them.
        is_temp = bool((getattr(bk, "cloud_metadata", None) or {}).get("pre_confirmation"))
        temp_tag = f"  ⏳ {t('overlay.temp_backup_tag')}" if is_temp else ""
        btn = QPushButton(f"  ↩  {date_str}  —  {badge}  —  {bk.size_human}{temp_tag}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        border_col = palette('warning') if is_temp else palette('border_hover')
        btn.setStyleSheet(
            f"QPushButton{{text-align:left;padding:4px 8px;font-size:10px;"
            f"color:{palette('text_secondary')};background:{palette('bg_elevated')};border:1px solid {border_col};border-radius:4px;}}"
            f"QPushButton:hover{{background:{palette('bg_elevated')};border-color:{palette('accent')};color:{palette('accent')};}}"
        )
        bid = bk.backup_id
        _origin = getattr(bk, "origin", "") or ""
        btn.clicked.connect(lambda _, b=bid, co=cloud_only, gid=game_id, org=_origin:
                            self._do_quick_restore(b, cloud_only=co, game_id=gid,
                                                   provider_id=org))
        return btn

    def _do_quick_restore(self, backup_id: str, cloud_only: bool = False, game_id: str = "",
                          provider_id: str = ""):
        """Execute a quick restore from the overlay.

        If *cloud_only*, download the backup — from the provider the entry
        came from (*provider_id*, i.e. the source selected in the restore
        list), falling back to the first connected one — then emit the
        restore action.
        """
        if cloud_only and game_id:
            import threading
            _bid = backup_id
            _gid = game_id
            _pid = provider_id

            def _bg_download():
                try:
                    from sync import get_orchestrator
                    from core.backup import get_backup_manager, BackupEntry
                    from core.library import get_library
                    import tempfile

                    orch = get_orchestrator()
                    entry = get_library().get_by_id(_gid)
                    provider = (orch.get_provider(_pid) if _pid else None) or orch.provider
                    if provider and not provider.is_connected:
                        provider = orch.provider
                    if provider and entry:
                        # Version/build-insensitive remote folder resolution
                        # (same rule as the restore-list fetch).
                        game_folder = _remote_game_folder(orch, provider, entry, _gid)
                        remote_zip = f"SaveSync/backup/{game_folder}/{_bid}.zip"
                        for rd in provider.list_cloud_backups(game_folder):
                            if rd.get("backup_id") == _bid:
                                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                                    tmp_path = Path(tmp.name)
                                ok = provider.download(remote_zip, tmp_path)
                                if ok and tmp_path.exists():
                                    rd["origin"] = provider.PROVIDER_ID
                                    be = BackupEntry.from_dict(rd)
                                    get_backup_manager().import_backup(be, tmp_path.read_bytes())
                                tmp_path.unlink(missing_ok=True)
                                break
                except Exception:
                    pass
                # Signal emission is thread-safe (queued to the receiver's
                # thread); QTimer.singleShot from this plain thread never
                # fired, so the cloud restore silently never happened.
                self.action_requested.emit("quick_restore", _bid)

            threading.Thread(target=_bg_download, daemon=True).start()
            self.hide_animated()
            return

        self.action_requested.emit("quick_restore", backup_id)
        self.hide_animated()

    def refresh_unknown_badge(self):
        """Sync the top-left badge with the pending unknown-game queue —
        called on every show and whenever a new detection is recorded
        while the overlay is already visible. Hidden entirely when the
        unknown-process feature is off: the queue is not offered then,
        so a counter for it would only confuse."""
        try:
            from core.config_manager import get_config
            if not get_config().get("show_overlay_on_unknown", True):
                n = 0
            else:
                from ui.unknown_history import pending_unknown_count
                n = pending_unknown_count()
        except Exception:
            n = 0
        self._unknown_badge.setText(f"🎮 {n}")
        self._unknown_badge.setVisible(n > 0)
        self._refresh_badge_interactivity()

    def _refresh_badge_interactivity(self):
        """Badge affordance follows the overlay mode: an active button only
        over the tracking toast (clicking swaps in the unknown queue); a
        flat counter everywhere else — on the unknown view the arrows
        already browse the queue, so a click would be a no-op there."""
        clickable = self._overlay_mode == "tracking"
        base = (
            f"QPushButton{{color:{palette('accent_text')};background:{palette('accent')};"
            f"border:none;border-radius:10px;font-size:11px;font-weight:700;"
            f"padding:0 8px;}}"
        )
        if clickable:
            self._unknown_badge.setStyleSheet(
                base + f"QPushButton:hover{{background:{palette('accent_hover')};}}")
            self._unknown_badge.setCursor(Qt.CursorShape.PointingHandCursor)
            self._unknown_badge.setToolTip(t("unknown_history.badge_tooltip"))
        else:
            self._unknown_badge.setStyleSheet(base)
            self._unknown_badge.setCursor(Qt.CursorShape.ArrowCursor)
            self._unknown_badge.setToolTip(t("unknown_history.badge_pending"))

    def _on_badge_clicked(self):
        if self._overlay_mode != "tracking":
            return
        self.show_unknown_queue()

    def _set_mode(self, mode: str):
        """Track what the overlay is currently showing; leaving the unknown
        view drops its browse queue so the carousel arrows fall back to the
        notification queue."""
        self._overlay_mode = mode
        if mode != "unknown":
            self._unknown_queue = None
        self._refresh_badge_interactivity()

    def showEvent(self, event):
        self.refresh_unknown_badge()
        super().showEvent(event)

    def _on_action(self, action: str):
        self.action_requested.emit(action, self._context_exe)
        # Always hide overlay after any action, including open_app
        self.hide_animated()

    def _on_suppress(self):
        exe = self._context_exe
        in_unknown_queue = self._unknown_queue is not None
        idx = self._unknown_index
        # Direct connection: by the time emit returns, the main window has
        # already suppressed the app AND pruned it from the pending queue.
        self.dont_show_again.emit(exe)
        if in_unknown_queue:
            # Stay ON the queue: re-read it (the suppressed entry is gone)
            # and keep the same position — the next entry slides into this
            # slot, no jump back to the first one, and the render is
            # in-place so the auto-hide countdown and the hover-pause
            # state are NOT reset (with the cursor still on the card the
            # countdown stays paused exactly as before the click).
            entries = self._pending_unknown_entries()
            if entries:
                self._unknown_queue = entries
                self._unknown_index = min(idx, len(entries) - 1)
                self._render_unknown_entry()
                self._rerender_in_place()
                self.refresh_unknown_badge()
                return
        self.hide_animated()

    def _set_suppress_link(self, text_key: str, handler) -> None:
        """Show the bottom suppress link labelled overlay.*text_key* and wire
        its click to *handler*. Always disconnect-then-connect: the button is
        shared across notification types, so relying on a previous connection
        would fire a stale handler."""
        self._suppress_btn.setText(t(f"overlay.{text_key}"))
        self._suppress_btn.setVisible(True)
        self._suppress_btn.setMaximumHeight(16777215)
        try:
            self._suppress_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._suppress_btn.clicked.connect(handler)

    def _suppress_regression_notif(self, game_id: str) -> None:
        """Mute the save-interference alert for this game.

        Its own handler, not _suppress_ingame_notif: that one assumes the
        thing being muted is the notification currently rendered FROM
        _notif_queue, and pops-and-re-renders that queue. This alert is a
        priority prompt and is not in the queue, so sharing the handler would
        dismiss an unrelated notification, leave the alert on screen, and
        keep the priority lock held with nothing to release it.
        """
        from core.config_manager import get_config
        config = get_config()
        suppressed: dict = dict(config.get("suppressed_ingame_notifs", {}))
        kinds = list(suppressed.get(game_id, []))
        if "regression" not in kinds:
            kinds.append("regression")
        suppressed[game_id] = kinds
        config.set("suppressed_ingame_notifs", suppressed)
        logger.info(f"Muted save-interference alerts for game {game_id}")
        self.hide_animated()

    def _build_pin_menu(self):
        """Build the 📌 menu. Separate from showing it so its contents can be
        checked without opening a modal menu loop.

        The recent entries are widget rows rather than plain actions: each
        carries a pin marker on the left for what is already on screen, and a
        bin on the right that drops it from the list without pinning it first.
        """
        from PySide6.QtWidgets import QMenu, QScrollArea, QWidgetAction
        from ui.widgets.pins import get_pin_manager, PinMenuRow

        mgr = get_pin_manager()
        menu = QMenu(self)
        recent = mgr.recent()          # this game's list, not everyone's
        if recent:
            rows = []
            body = QWidget()
            body.setObjectName("pin_menu_body")
            col = QVBoxLayout(body)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            for path in recent:
                label, tip, unsaved = mgr.menu_entry(path)
                row = PinMenuRow(path, mgr.is_open(path), body, label, tip,
                                 unsaved)
                # Every one of these closes the menu FIRST and acts after.
                # All three can take a pin off the screen or put a dialog up,
                # and doing that from inside a menu's own event loop — while
                # it holds the mouse — is what makes the menu misbehave
                # rather than simply close.
                row.activated.connect(lambda p, m=menu: (
                    m.close(), QTimer.singleShot(0, lambda q=p: mgr.toggle(q))))
                row.removed.connect(lambda p, m=menu: (
                    m.close(), QTimer.singleShot(0, lambda q=p: mgr.forget(q))))
                row.save.connect(lambda p, m=menu: (
                    m.close(), QTimer.singleShot(0, lambda q=p: mgr.save_now(q))))
                col.addWidget(row)
                rows.append(row)
            holder = body
            # Past a handful the list stops being a menu and becomes a wall.
            if len(rows) > _PIN_MENU_ROWS:
                area = QScrollArea()
                area.setObjectName("pin_menu_scroll")
                area.setWidgetResizable(True)
                area.setWidget(body)
                area.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                area.setFixedHeight(rows[0].sizeHint().height() * _PIN_MENU_ROWS + 4)
                area.setFixedWidth(max(r.sizeHint().width() for r in rows) + 20)
                holder = area
            act = QWidgetAction(menu)
            act.setDefaultWidget(holder)
            menu.addAction(act)
        if not recent:
            empty = menu.addAction(t("pin.none"))
            empty.setEnabled(False)
        menu.addSeparator()
        menu.addAction(t("pin.new_note"), lambda: mgr.new_note())
        menu.addAction(t("pin.capture_action"), self._pin_capture)
        menu.addAction(t("pin.add"), self._pin_add_file)
        if mgr.open_paths() or mgr.unsaved_pins():
            menu.addAction(t("pin.close_all"), mgr.close_all)
        return menu

    def _show_pin_menu(self) -> None:
        """The 📌 menu: what was pinned recently, and a way to pin something
        new. Ticking an entry pins it, unticking closes it."""
        # The overlay fades itself out on a timer. A player reading this menu
        # must not have the window under it disappear mid-choice.
        self._auto_hide_timer.stop()
        menu = self._build_pin_menu()
        menu.exec(self._pin_btn.mapToGlobal(QPoint(0, self._pin_btn.height())))
        if self._pending_auto_hide:
            self._auto_hide_timer.start(self._pending_auto_hide)

    def _pin_capture(self) -> None:
        """Grab a piece of the screen and pin it.

        SaveSync's own overlay goes first: the shot is taken of what the
        player is looking at, and the panel they opened this from is not part
        of that.
        """
        from ui.widgets.pins import get_pin_manager
        from ui.widgets.screen_capture import capture_region

        was_visible = self.isVisible()
        self.hide()
        QApplication.processEvents()
        piece = None
        try:
            piece = capture_region(None)
        finally:
            # Only when nothing was captured — cancelled, or a display mode we
            # cannot read. On success the player got what they opened this for,
            # and bringing the panel back would just cover the new pin.
            if piece is None and was_visible and not self.isVisible():
                self.show()
        if piece is not None:
            # The panel stays down, so _do_hide never runs: let go here, or
            # the overlay's hold would outlive the overlay itself.
            self._cursor_timer.stop()
            SystemCursor.release("overlay")
            get_pin_manager().new_capture(piece)

    def _pin_add_file(self) -> None:
        # The app's own picker, not the plain Qt one: same shortcut handling
        # and same sidebar places as everywhere else in SaveSync.
        from ui.widgets.file_pickers import pick_file
        from ui.widgets.pins import get_pin_manager, pin_name_filter

        path = pick_file(self, t("pin.add_title"), pin_name_filter())
        if path:
            get_pin_manager().pin(path)

    def _hide_suppress_btn(self) -> None:
        self._suppress_btn.setVisible(False)
        self._suppress_btn.setMaximumHeight(0)

    def _hide_dashboard(self) -> None:
        self._dashboard.setVisible(False)
        self._dashboard.setMaximumHeight(0)

    def _begin_cloud_prompt(self, game_name: str, exe_path: str, hint_key: str) -> None:
        """Shared header for the cloud-save decision prompts: context, cloud
        icon, game name + overlay.*hint_key* message, clean button areas."""
        self._set_mode("cloud")
        self._context_exe = exe_path
        self._priority_context = exe_path
        self._icon_label.setText("☁")
        self._title.setText(t("app.name"))
        self._message.setText(
            f"<b>{game_name}</b><br>"
            f"<span style='color:{palette('text_hint')};font-size:11px;'>{t(f'overlay.{hint_key}')}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()

    def _show_priority_prompt(self) -> None:
        """Show a decision-required prompt: holds priority (later
        notifications defer to it) and never auto-hides."""
        self._position_top_right()
        self._priority_active = True
        self.show_animated(auto_hide=0)

    # ── Public API ────────────────────────────────────────────────────────────

    def show_game_detected(self, game_name: str, exe_path: str):
        """Unknown-game detection prompt. When other detections are still
        pending, the same view exposes them via the carousel arrows — the
        whole queue is browsable in place, no separate panel."""
        if self._defer_if_priority(lambda: self.show_game_detected(game_name, exe_path)):
            return
        self._clear_notification_queue()
        entries = self._pending_unknown_entries()
        idx = next((i for i, h in enumerate(entries)
                    if h.get("exe") == exe_path), None)
        if idx is None:
            # Live detection not (yet) recorded in the history — still show it.
            entries.insert(0, {"name": game_name, "exe": exe_path})
            idx = 0
        self._set_mode("unknown")
        self._unknown_queue = entries
        self._unknown_index = idx
        self._render_unknown_entry()
        self._position_relative_to_active_window()
        self.show_animated(auto_hide=_AUTO_HIDE_MS)

    def show_unknown_queue(self):
        """REPLACE the current notification with the pending unknown-game
        queue, browsable via the carousel arrows: the in-overlay successor
        of the old dedicated 'detected games (not in library)' panel."""
        from core.config_manager import get_config
        if not get_config().get("show_overlay_on_unknown", True):
            self.refresh_unknown_badge()
            return
        if self._defer_if_priority(self.show_unknown_queue):
            return
        entries = self._pending_unknown_entries()
        if not entries:
            self.refresh_unknown_badge()
            return
        self._clear_notification_queue()
        self._set_mode("unknown")
        self._unknown_queue = entries
        self._unknown_index = 0
        self._render_unknown_entry()
        self._position_top_right()
        self.show_animated(auto_hide=_AUTO_HIDE_MS)

    @staticmethod
    def _pending_unknown_entries() -> list[dict]:
        try:
            from ui.unknown_history import pending_entries
            return list(pending_entries())
        except Exception:
            return []

    def _render_unknown_entry(self):
        """Render unknown-queue entry _unknown_index: detection message,
        Add-to-library button and suppress link all target THIS entry; the
        flanking arrows step through the rest of the queue."""
        entries = self._unknown_queue or []
        n = len(entries)
        if not n:
            return
        idx = max(0, min(self._unknown_index, n - 1))
        self._unknown_index = idx
        h = entries[idx]
        game_name = h.get("name") or "?"
        self._context_exe = h.get("exe", "")
        self._icon_label.setText("🎮")
        self._title.setText(t("app.name"))
        self._message.setText(
            f"<b>{game_name}</b> {t('overlay.detected_msg')}<br>"
            f"<span style='color:{palette('text_hint')};font-size:11px;'>{t('overlay.save_detected')}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        self._add_btn(self._btn_area, t("overlay.add_to_library"), "add_game", primary=True)
        # Rewire explicitly: a previous notification may have pointed the
        # shared suppress button at a different handler.
        self._set_suppress_link("dont_show_again", self._on_suppress)
        show_arrows = n > 1
        self._carousel_prev.setVisible(show_arrows)
        self._carousel_next.setVisible(show_arrows)
        self._carousel_counter.setVisible(show_arrows)
        if show_arrows:
            self._carousel_counter.setText(f"{idx + 1} / {n}")
            self._carousel_prev.setEnabled(idx > 0)
            self._carousel_next.setEnabled(idx < n - 1)

    def show_game_added(self, game_name: str, exe_path: str = "",
                        then_track: bool = False, engine: str = ""):
        """Confirmation that a game was added to library.
        If then_track=True, automatically transitions to tracking overlay after delay."""
        if self._defer_if_priority(
                lambda: self.show_game_added(
                    game_name, exe_path, then_track, engine)):
            return
        self._set_mode("added")
        self._context_exe = exe_path
        self._icon_label.setText("✓")
        self._title.setText(t("app.name"))
        eng = engine or _engine_label_for_exe(exe_path)
        self._message.setText(
            f"<span style='color:{palette('accent')};font-weight:700;'>"
            f"{t('overlay.add_to_library')}</span><br>"
            f"<b>{html.escape(game_name)}</b>{_engine_badge_html(eng)}"
        )
        self._hide_dashboard()
        self._clear_buttons()
        self._hide_suppress_btn()
        self._position_relative_to_active_window()
        self.show_animated(auto_hide=2500)

        if then_track:
            # Wait for auto_hide + fade_out + buffer before showing next overlay
            QTimer.singleShot(
                2500 + _FADE_OUT_MS + 50,
                lambda n=game_name, e=exe_path, eng=eng:
                    self.show_game_launched(n, e, eng))

    def show_game_launched(self, game_name: str, exe_path: str = "",
                           engine: str = ""):
        """Notification that a known game is being monitored.

        Shows the overlay hotkey hint so the user knows how to open the app
        in-game.  The Restore button is deliberately omitted here — the user
        has not asked to restore anything; it would appear as a false alarm.
        Clears stale backup/sync notifications from previous sessions.
        """
        if self._defer_if_priority(
                lambda: self.show_game_launched(game_name, exe_path, engine)):
            return
        self._clear_notification_queue()
        self._set_mode("tracking")
        self._context_exe = exe_path
        self._icon_label.setText("🎮")
        self._title.setText(t("app.name"))
        eng = (engine or "").strip() or _engine_label_for_exe(exe_path)
        # Game name + engine on one line; status on the line below.
        game_html = (
            f"<b>{html.escape(game_name)}</b>{_engine_badge_html(eng)}"
        )
        self._message.setText(t("overlay.game_launched", game=game_html))
        self._hide_dashboard()
        self._clear_buttons()

        # Show the overlay hotkey so the user knows how to open SaveSync mid-game
        from core.config_manager import get_config
        # Same fallback as the registration in MainWindow._setup_hotkeys —
        # a different one here told the user a shortcut that never worked.
        hotkey = get_config().get("overlay_hotkey", "alt+ctrl+s").upper()
        hotkey_lbl = QLabel(
            f"<span style='color:{palette('text_muted')};font-size:10px;'>"
            f"{t('overlay.open_with_hotkey', hotkey=hotkey)}</span>"
        )
        hotkey_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_area.addWidget(hotkey_lbl)

        self._hide_suppress_btn()
        self._position_top_right()
        self.show_animated(auto_hide=_AUTO_HIDE_MS)

    def show_restore_result(self, success: bool, game_name: str):
        """Brief notification showing restore result."""
        if self._defer_if_priority(lambda: self.show_restore_result(success, game_name)):
            return
        self._set_mode("restore")
        self._context_exe = ""
        if success:
            self._icon_label.setText("✓")
            self._message.setText(
                f"<span style='color:{palette('success')};font-weight:700;'>"
                f"{t('overlay.restore_success')}</span>"
                + (f"<br><b>{game_name}</b>" if game_name else "")
            )
        else:
            self._icon_label.setText("✗")
            self._message.setText(
                f"<span style='color:{palette('error')};font-weight:700;'>"
                f"{t('overlay.restore_failed')}</span>"
            )
        self._hide_dashboard()
        self._clear_buttons()
        self._hide_suppress_btn()
        self._position_top_right()
        self.show_animated(auto_hide=3000)

    # The four cloud-save prompts below are DECISION-REQUIRED: they hold
    # priority while on screen (later notifications defer via
    # _defer_if_priority) and never auto-hide. Closing without choosing means
    # "later" — no state is popped, so the hotkey can re-summon them.

    def show_cloud_saves(self, game_name: str, exe_path: str):
        """Cloud saves exist for a library game whose local copy isn't synced."""
        if self._defer_if_priority(lambda: self.show_cloud_saves(game_name, exe_path),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_available")
        # Same shape as the other cloud prompts: a primary download plus a
        # dropdown that says what the alternative actually DOES. A bare
        # "Dismiss" here suggested nothing — the player could not tell
        # whether closing it meant keeping local saves, postponing, or
        # losing something.
        self._add_split_btn(
            self._btn_area, t("overlay.download_saves"), "download_saves",
            menu_items=[(t("overlay.continue_local"), "dismiss")],
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("suppress_cloud_no_local"))
        self._show_priority_prompt()

    def show_cloud_saves_unknown(self, game_name: str, exe_path: str):
        """Cloud saves for an UNKNOWN game, exactly ONE cloud folder with this
        name. Primary downloads & adds; dropdown offers keep-local or a details
        check (in case the cloud copy is a same-name different game)."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_unknown(game_name, exe_path),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_available")
        self._add_split_btn(
            self._btn_area, t("overlay.download_and_add"), "download_saves_unknown_game",
            menu_items=[
                (t("overlay.keep_local_saves"), "add_game_no_download"),
                (t("overlay.is_homonym"), "homonym_unknown_game"),
                (t("overlay.verify_details"), "verify_details_unknown_game"),
            ],
            primary=True,
        )
        self._set_suppress_link("dont_show_again", self._on_suppress)
        self._show_priority_prompt()

    def show_cloud_saves_conflict(self, game_name: str, exe_path: str):
        """Cloud saves for an UNKNOWN game, but SEVERAL cloud folders share this
        name — a genuine conflict (a prior homonym created a second folder).
        Primary opens the verify-conflicts dialog to pick the right copy."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_conflict(game_name, exe_path),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_conflict")
        # No plain "keep local" here: with 2+ same-named cloud folders the
        # destination is ambiguous. Offer the primary verify-conflicts picker
        # and a direct "it's a homonym" (own new folder) shortcut.
        self._add_split_btn(
            self._btn_area, t("overlay.verify_conflicts"), "verify_conflicts_unknown_game",
            menu_items=[(t("overlay.is_homonym"), "homonym_unknown_game")],
            primary=True,
        )
        self._set_suppress_link("dont_show_again", self._on_suppress)
        self._show_priority_prompt()

    def show_cloud_saves_no_local(self, game_name: str, exe_path: str):
        """Cloud saves found for a LIBRARY game that has no local backups
        (split-button dropdown: continue with local saves; bottom link:
        permanent per-game suppression)."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_no_local(game_name, exe_path),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_no_local")
        self._add_split_btn(
            self._btn_area, t("overlay.download_saves"), "download_saves_no_local",
            menu_items=[(t("overlay.continue_local"), "continue_local_no_local")],
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("suppress_cloud_no_local"))
        self._show_priority_prompt()

    def show_cloud_saves_different_machine(self, game_name: str, exe_path: str):
        """Cloud saves were last uploaded by a DIFFERENT machine — local saves
        here may be older. Download & replace, or keep local."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_different_machine(game_name, exe_path),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_different_machine")
        self._add_btn(self._btn_area, t("overlay.download_and_replace"), "download_saves_different_machine", primary=True)
        self._add_btn(self._btn_area, t("overlay.keep_local"),           "decline_cloud_different_machine")
        self._hide_suppress_btn()
        self._show_priority_prompt()

    def show_cloud_conflict_resolve(self, game_name: str, exe_path: str,
                                    diverged: bool = True):
        """Local and cloud saves need reconciling — the notification IS the
        entry point for resolving it, with every resolution in the dropdown.

        The comparison window (ConflictDialog, with both dated versions) is
        the PRIMARY action rather than one of the resolutions: this can pop
        up mid-game, and a one-click "keep cloud" on an overlay would
        overwrite local progress without ever showing the two dates the
        decision rests on. The one-click paths are still there, one menu
        away.

        *diverged* tells the two situations apart: both sides changed since
        the last sync (a real conflict), or a cloud copy exists that this
        library entry has simply never reconciled with — same resolutions,
        different explanation. Only the second one can be a HOMONYM (a cloud
        folder named after a same-titled different game, from another
        machine), so it alone offers that way out; a diverged game has
        already synced with that folder, which settles whose it is.
        """
        if self._defer_if_priority(
                lambda: self.show_cloud_conflict_resolve(game_name, exe_path, diverged),
                context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(
            game_name, exe_path,
            "cloud_conflict_diverged" if diverged else "cloud_conflict_unreconciled",
        )
        _items = [
            (t("sync.keep_both"),  "conflict_keep_both"),
            (t("sync.keep_local"), "conflict_keep_local"),
            (t("sync.keep_cloud"), "conflict_keep_cloud"),
        ]
        if not diverged:
            _items.append((t("overlay.is_homonym"), "homonym_library_game"))
        self._add_split_btn(
            self._btn_area, t("overlay.verify_conflicts"), "resolve_conflict_details",
            menu_items=_items,
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("suppress_cloud_no_local"))
        self._show_priority_prompt()

    def show_unverified_match(self, game_name: str, proc_name: str, game_id: str):
        """A running process matched a library game by NAME ONLY — its own
        path could not be read (typically an elevated game), so nothing
        confirms it really is that game rather than another one shipping an
        executable of the same name.

        A decision-required prompt on purpose: until the user answers, the
        process is not tracked and no save of it is touched. The answer is
        remembered, so this is asked once per executable name.
        """
        context = f"{proc_name}|{game_id}"
        if self._defer_if_priority(
                lambda: self.show_unverified_match(game_name, proc_name, game_id),
                context=context, is_priority=True):
            return
        self._set_mode("cloud")          # same decision-prompt chrome
        self._context_exe = context
        self._priority_context = context
        self._icon_label.setText("❓")
        self._title.setText(t("app.name"))
        self._message.setText(
            f"<b>{proc_name}</b> {t('overlay.unverified_match_msg', game=game_name)}<br>"
            f"<span style='color:{palette('text_hint')};font-size:11px;'>"
            f"{t('overlay.unverified_match_hint')}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        self._add_btn(self._btn_area, t("overlay.unverified_match_yes", game=game_name),
                      "confirm_process_match", primary=True)
        self._add_btn(self._btn_area, t("overlay.unverified_match_no"),
                      "reject_process_match")
        self._hide_suppress_btn()
        self._show_priority_prompt()

    def show_save_reverted(self, game_name: str, game_id: str,
                           newest_backup_id: str = "", after_restore: bool = False):
        """The game's saves went back to a state we had already recorded.

        Playing produces content that never existed before, so landing exactly
        on an older backup's state is not something play can do — something
        put an earlier state back. Two wordings, because the two moments call
        for different advice:

        - at launch: state the fact, offer to put the newest state back;
        - right after a restore: name the likely cause (a launcher's automatic
          sync racing the restore) and offer to force it with the game frozen,
          which is the thing that actually wins that race.
        """
        context = f"{game_id}|reverted"
        if self._defer_if_priority(
                lambda: self.show_save_reverted(game_name, game_id,
                                                newest_backup_id, after_restore),
                context=context, is_priority=True):
            return
        self._set_mode("cloud")          # decision-prompt chrome
        self._context_exe = f"{game_id}|{newest_backup_id}"
        self._priority_context = context
        self._icon_label.setText("↩")
        self._title.setText(t("overlay.save_reverted_title"))
        msg_key = "overlay.restore_undone_msg" if after_restore else "overlay.save_reverted_msg"
        hint_key = "overlay.restore_undone_hint" if after_restore else "overlay.save_reverted_hint"
        self._message.setText(
            f"{t(msg_key, game=game_name)}<br>"
            f"<span style='color:{palette('text_hint')};font-size:11px;'>"
            f"{t(hint_key)}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        # An alert holds priority and never auto-hides, so it MUST carry a way
        # out: without it nothing behind it would ever be shown again.
        # "regression_ack" is its own action, not the shared "dismiss":
        # losing this alert must not mean losing the warning, so it stays
        # re-summonable by the hotkey until the player actually acknowledges
        # it — and THAT is what this action says. Worded as an
        # acknowledgement for the same reason.
        #
        # Same shape as the cloud prompts: the repair is the primary button
        # and the acknowledgement sits in its dropdown. With no backup to
        # restore there is no primary to attach a menu to, so the
        # acknowledgement stays a plain button — it is the only way out.
        if newest_backup_id:
            self._add_split_btn(
                self._btn_area,
                t("overlay.restore_force") if after_restore
                else t("overlay.save_reverted_restore"),
                "force_restore" if after_restore else "restore_newest",
                menu_items=[(t("overlay.got_it"), "regression_ack")],
                primary=True)
        else:
            self._add_btn(self._btn_area, t("overlay.got_it"), "regression_ack")
        self._set_suppress_link(
            "dont_show_ingame",
            lambda _checked=False, gid=game_id:
                self._suppress_regression_notif(gid))
        self._show_priority_prompt()

    def dismiss_unverified_match(self, proc_name: str, game_id: str) -> None:
        """Withdraw an unanswered unverified-match prompt whose process has
        exited. Without this the prompt sits there asking about something that
        is gone AND keeps holding priority, deferring every later
        notification behind a question that can no longer be answered."""
        context = f"{proc_name}|{game_id}"
        if self._priority_context != context:
            return
        if self.isVisible() and self.windowOpacity() > 0.1:
            self.hide_animated()      # drops priority and replays deferred
            self._priority_context = ""
            return
        self.clear_priority_for(context)

    # ── Priority prompt gating ───────────────────────────────────────────────

    def _defer_if_priority(self, fn, context: str = "", is_priority: bool = False) -> bool:
        """If a decision-required priority prompt (cloud-save decision) is
        currently on screen, queue *fn* to replay once it resolves and return
        True — the caller must then return without showing anything. Returns
        False when there is no active priority prompt (show normally).

        *is_priority*/*context*: a priority prompt re-showing ITSELF (same
        game/context — e.g. the hotkey re-summoning the same cloud prompt) is
        NOT deferred against itself; it refreshes in place. A DIFFERENT
        priority prompt, and every interruptible notification, is deferred.
        """
        # Opacity is deliberately NOT part of this test. A prompt is still
        # fading in for the first fraction of its animation, and a
        # notification landing in that window used to REPLACE it instead of
        # queueing behind it — the one moment a prompt is most likely to be
        # interrupted is right after it was raised. _priority_active is the
        # honest signal: hide_animated drops it before the fade-out starts,
        # so a prompt on its way out never holds the lock here.
        if not (self._priority_active and self.isVisible()):
            return False
        if is_priority and context and context == self._priority_context:
            return False
        # Bounded: a long-lived prompt must not accumulate stale toasts.
        if len(self._deferred_notifs) >= 20:
            self._deferred_notifs.pop(0)
        self._deferred_notifs.append(fn)
        logger.debug("Deferred a notification behind an active priority prompt")
        return True

    def _flush_deferred_notifs(self) -> None:
        """Replay notifications that were deferred while a priority prompt was
        active. Scheduled after the prompt's fade-out (hide_animated). If a new
        priority prompt appeared meanwhile, keep waiting — its own resolution
        will re-schedule this."""
        if self._priority_active:
            return
        if not self._deferred_notifs:
            return
        pending = self._deferred_notifs
        self._deferred_notifs = []
        for fn in pending:
            try:
                fn()
            except Exception as e:
                logger.debug(f"Deferred notification replay failed: {e}")

    def clear_priority_for(self, exe_path: str) -> None:
        """Release a decision-required priority prompt when ITS subject process
        has exited, so later notifications (including hotkey-invoked ones) stop
        being deferred behind a prompt that can never be answered — the wedge
        that silently blocked all further detection. Replays anything queued
        behind it. A blank *exe_path* clears unconditionally."""
        if self._priority_active and (not exe_path or exe_path == self._priority_context):
            logger.debug(f"Releasing priority prompt — subject exited: {exe_path!r}")
            self._priority_active = False
            self._priority_context = ""
            self._flush_deferred_notifs()

    # ── Notification carousel ────────────────────────────────────────────────

    def _push_notification(self, icon: str, title: str, message: str,
                           auto_hide_ms: int = _AUTO_HIDE_MS,
                           game_id: str = "", notif_type: str = "") -> None:
        """Add a notification to the queue and display it.

        game_id + notif_type: when both set, a "don't show again" link is
        offered for that specific game × notification type combination.
        If this game+type pair is already suppressed in config, skip silently.
        """
        if self._defer_if_priority(lambda: self._push_notification(
                icon, title, message, auto_hide_ms, game_id, notif_type)):
            return
        # Cleared only AFTER the defer guard: while a priority prompt is on
        # screen its buttons rely on _context_exe — wiping it here would
        # break them (the toast is deferred, the prompt stays visible).
        self._context_exe = ""
        # Check per-game suppression
        if game_id and notif_type:
            from core.config_manager import get_config
            suppressed = get_config().get("suppressed_ingame_notifs", {})
            if notif_type in suppressed.get(game_id, []):
                return

        entry = (icon, title, message, auto_hide_ms)
        if entry in self._notif_queue:
            return

        self._notif_queue.append(entry)
        self._notif_meta.append((game_id, notif_type))
        self._notif_index = len(self._notif_queue) - 1
        self._render_current_notification()

    def _render_current_notification(self, in_place: bool = False) -> None:
        """Render the notification at _notif_index.

        *in_place*: the overlay is already on screen and only its content
        changes (arrow step, suppress-and-stay) — swap without the fade /
        reposition / countdown-reset of a full show_animated pass."""
        if not self._notif_queue:
            return
        self._set_mode("notif")
        idx = self._notif_index
        icon, title, message, auto_hide_ms = self._notif_queue[idx]
        game_id, notif_type = (self._notif_meta[idx]
                               if idx < len(self._notif_meta) else ("", ""))

        self._icon_label.setText(icon)
        self._title.setText(title)
        self._message.setText(message)
        self._hide_dashboard()
        self._clear_buttons()

        # Per-game suppress link when this notif carries an identity
        if game_id and notif_type:
            self._set_suppress_link(
                "dont_show_ingame",
                lambda _checked=False, gid=game_id, nt=notif_type:
                    self._suppress_ingame_notif(gid, nt))
        else:
            self._hide_suppress_btn()

        # Update carousel arrows (shown flanking message only when >1 notification)
        n = len(self._notif_queue)
        show_arrows = n > 1
        self._carousel_prev.setVisible(show_arrows)
        self._carousel_next.setVisible(show_arrows)
        self._carousel_counter.setVisible(show_arrows)
        if show_arrows:
            self._carousel_counter.setText(f"{idx + 1} / {n}")
            self._carousel_prev.setEnabled(idx > 0)
            self._carousel_next.setEnabled(idx < n - 1)

        # Visible is enough for the in-place path — checking opacity too
        # would bounce a click landing during the fade-in back through the
        # full show_animated (blink + countdown reset), which is exactly
        # what in-place exists to avoid; a still-running fade simply
        # continues over the swapped content.
        if in_place and self.isVisible():
            self._rerender_in_place()
        else:
            self._position_top_right()
            self.show_animated(auto_hide=auto_hide_ms)

    def _rerender_in_place(self) -> None:
        """Content swapped while the overlay is ON SCREEN (carousel step,
        suppress-and-stay): adjust size around the frozen top-left anchor
        and nothing else — no fade restart, no reposition to the corner,
        and the auto-hide countdown / hover-pause state stay UNTOUCHED
        (show_animated resets all three, which made every arrow click
        blink the card, teleport the buttons and restart the timer)."""
        anchor = self.pos()
        self.adjustSize()
        self.move(anchor)
        # Hover states are only correct after the new layout has settled.
        QTimer.singleShot(0, self._resync_hover)

    def _resync_hover(self) -> None:
        """Qt re-evaluates :hover only on real mouse MOVEMENT: after an
        in-place re-render the button under the (stationary) cursor may
        have moved or been rebuilt, so the arrow just clicked sat there
        'unlit' until the user wiggled the mouse — and a shifted layout
        could light a NEIGHBOUR instead. Re-derive under-mouse from the
        actual cursor position and repolish the buttons that changed."""
        try:
            if not self.isVisible():
                return
            pos = QCursor.pos()
            for btn in self.findChildren(QPushButton):
                under = (btn.isVisible() and btn.isEnabled()
                         and btn.rect().contains(btn.mapFromGlobal(pos)))
                if under != btn.testAttribute(Qt.WidgetAttribute.WA_UnderMouse):
                    btn.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, under)
                    btn.style().unpolish(btn)
                    btn.style().polish(btn)
                    btn.update()
        except RuntimeError:
            pass

    def _suppress_ingame_notif(self, game_id: str, notif_type: str) -> None:
        """Persist a per-game notification suppression. With more items in
        the carousel, drop the suppressed one and STAY on the queue at the
        same position (in place: countdown and hover untouched); only an
        emptied queue hides the overlay."""
        from core.config_manager import get_config
        config = get_config()
        suppressed: dict = dict(config.get("suppressed_ingame_notifs", {}))
        if game_id not in suppressed:
            suppressed[game_id] = []
        if notif_type not in suppressed[game_id]:
            suppressed[game_id].append(notif_type)
        config.set("suppressed_ingame_notifs", suppressed)
        logger.info(f"Suppressed in-game {notif_type!r} notifications for game {game_id}")
        if len(self._notif_queue) > 1 and 0 <= self._notif_index < len(self._notif_queue):
            idx = self._notif_index
            self._notif_queue.pop(idx)
            self._notif_meta.pop(idx)
            self._notif_index = min(idx, len(self._notif_queue) - 1)
            self._render_current_notification(in_place=True)
            return
        self.hide_animated()

    def _carousel_go_prev(self) -> None:
        # Unknown-queue view: the arrows browse THAT queue, not the notifs.
        if self._unknown_queue is not None:
            if self._unknown_index > 0:
                self._unknown_index -= 1
                self._render_unknown_entry()
                self._rerender_in_place()
            return
        if self._notif_index > 0:
            self._notif_index -= 1
            self._render_current_notification(in_place=True)

    def _carousel_go_next(self) -> None:
        if self._unknown_queue is not None:
            if self._unknown_index < len(self._unknown_queue) - 1:
                self._unknown_index += 1
                self._render_unknown_entry()
                self._rerender_in_place()
            return
        if self._notif_index < len(self._notif_queue) - 1:
            self._notif_index += 1
            self._render_current_notification(in_place=True)

    def _clear_notification_queue(self) -> None:
        """Reset the queue. Called when user manually dismisses or a non-queued
        show method is called (e.g. game detected, save done in-game)."""
        self._notif_queue.clear()
        self._notif_meta.clear()
        self._notif_index = 0
        self._unknown_queue = None
        self._carousel_prev.setVisible(False)
        self._carousel_next.setVisible(False)
        self._carousel_counter.setVisible(False)

    # ── Public notification helpers ───────────────────────────────────────────

    def show_backup_done(self, game_name: str, skipped: bool = False,
                         game_id: str = ""):
        """Queue a backup-result notification. Multiple results stack in the carousel.

        game_id: when set, offers a per-game "don't show again" suppress link.
        """
        icon = "—" if skipped else "✓"
        if skipped:
            msg = (
                f"<span style='color:{palette('text_muted')};'>"
                f"{t('notifications.backup_unchanged', game=game_name)}</span>"
            )
        else:
            msg = (
                f"<span style='color:{palette('accent')};font-weight:700;'>"
                f"{t('overlay.saved_msg')}</span> — <b>{game_name}</b>"
            )
        self._push_notification(icon, t("app.name"), msg, _AUTO_HIDE_MS,
                                 game_id=game_id, notif_type="backup")

    def show_provisional_backup_done(self, game_name: str, game_id: str = ""):
        """Queue a notification for a TEMPORARY (pre-confirmation) backup —
        distinct notif_type from show_backup_done, so "don't show again"
        silences provisional notifications independently from normal ones.
        """
        msg = (
            f"<span style='color:{palette('provisional')};font-weight:700;'>"
            f"{t('overlay.provisional_saved_msg')}</span> — <b>{game_name}</b>"
        )
        self._push_notification("◌", t("app.name"), msg, _AUTO_HIDE_MS,
                                 game_id=game_id, notif_type="provisional_backup")

    def show_sync_done(self, game_name: str, game_id: str = ""):
        """Queue a sync-result notification. Multiple results stack in the carousel.

        game_id: when set, offers a per-game "don't show again" suppress link.
        """
        msg = (
            f"<span style='color:{palette('accent')};font-weight:700;'>"
            f"{t('overlay.synced_msg')}</span> — <b>{game_name}</b>"
        )
        self._push_notification("☁", t("app.name"), msg, _AUTO_HIDE_MS,
                                 game_id=game_id, notif_type="sync")

    def show_manual(self, stats: dict | None = None):
        """Toggle dashboard. If visible → hide; else → show with live stats."""
        if self.isVisible() and self.windowOpacity() > 0.3:
            self.hide_animated()
            return

        self._set_mode("manual")
        self._context_exe = ""
        self._icon_label.setText("⚡")
        self._title.setText(t("app.name"))

        if stats:
            active = stats.get("active_game")
            if active:
                eng = stats.get("active_engine") or _engine_label_for_exe()
                game_html = (
                    f"<b>{html.escape(str(active))}</b>"
                    f"{_engine_badge_html(eng)}"
                )
                self._message.setText(t("overlay.game_launched", game=game_html))
                self._icon_label.setText("🎮")
            else:
                self._message.setText(
                    f"<span style='color:{palette('text_hint')};'>"
                    f"{t('app.tagline')}</span>"
                )
            self._dashboard.setVisible(True); self._dashboard.setMaximumHeight(16777215)
            self._set_dash(0, t('overlay.library'), t('overview.stat_games_count', count=stats.get('library_count', 0)))
            self._set_dash(1, t('overlay.last_backup'),  stats.get("last_backup", t("library.never")))
            sync_status = stats.get("sync_status", t("common.offline"))
            is_online = sync_status == t("common.online")
            self._set_dash(2, t('overlay.sync'),
                sync_status,
                palette("accent") if is_online else palette("text_hint")
            )
            self._set_dash(3, t('overlay.provider'),    stats.get("provider", t("common.none")))
        else:
            self._message.setText(f"<span style='color:{palette('text_hint')};'>{t('app.tagline')}</span>")
            self._hide_dashboard()

        self._clear_buttons()
        # [i] shortcut to the save-path scan panel (accept detected files
        # without leaving the game)
        # Both header shortcuts belong to a game SESSION, and both are
        # meaningless without one:
        #
        # - [i] opens the save-path panel for the running game. With no game
        #   it opens with no game context at all, so an Extended Scan from
        #   there has nothing to attribute its results to — a confusing
        #   dead end, especially next to the general search.
        # - 📌 pins are per game, put away when it ends, and would otherwise
        #   be filed under a bucket no game ever reads back.
        in_session = self._game_is_running()
        self._info_btn.setVisible(in_session)
        self._pin_btn.setVisible(in_session)
        self._add_btn(self._quick_area, t('overlay.open_app'),   "open_app", primary=True)
        # In-game (a tracked game is running): a plain "Backup" of the
        # current game. Outside a session: "Backup all".
        if stats and stats.get("active_game"):
            self._add_btn(self._quick_area, t('overlay.backup'), "backup_current")
        else:
            self._add_btn(self._quick_area, t('overlay.backup_all'), "backup_all")

        # Add quick-restore if a game is actively running
        if stats and stats.get("active_game"):
            from core.monitor import get_monitor
            from core.backup import get_backup_manager
            playing = get_monitor().currently_playing()
            if playing:
                entry = playing[0]
                backups = get_backup_manager().get_backups_for_game(entry.id)
                if backups:
                    restore_btn = self._add_btn(self._quick_area, t('overlay.quick_restore'), "")
                    restore_btn.setStyleSheet(
                        f"QPushButton{{font-size:11px;color:{palette('warning')};background:transparent;"
                        f"border:1px solid {palette('warning')};border-radius:4px;padding:4px 10px;}}"
                        f"QPushButton:hover{{background:{palette('bg_elevated')};}}"
                    )
                    try:
                        restore_btn.clicked.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                    try:
                        restore_btn.clicked.connect(lambda _, gid=entry.id: self._show_restore_list(gid))
                    except RuntimeError:
                        pass  # Button was deleted between disconnect and reconnect

        self._hide_suppress_btn()
        self._position_top_right()
        self.show_animated(auto_hide=_AUTO_HIDE_MS)

    # ── Animation ─────────────────────────────────────────────────────────────

    def show_animated(self, auto_hide: int = _AUTO_HIDE_MS):
        """Show the overlay above any window — safely handles fullscreen games.

        Fullscreen handling:
        - **Borderless fullscreen**: safe — show with TOPMOST + NOACTIVATE.
        - **Exclusive fullscreen** (DirectX/Vulkan): showing ANY window forces
          the DWM to composite, kicking the game out of exclusive mode (black
          flicker, fps drop, possible crash).  We skip the overlay entirely
          and emit ``exclusive_blocked(title, message)`` so the caller can
          give audio feedback (a notification sound).  No defer — by the time
          exclusive mode ends the notification is stale.
        """
        # ── Exclusive fullscreen guard ──────────────────────────────────────
        if platform.system() == "Windows" and self._is_exclusive_fullscreen():
            title = self._title.text() if hasattr(self, '_title') else t("app.name")
            msg = self._message.text() if hasattr(self, '_message') else ""
            msg_plain = re.sub(r'<[^>]+>', '', msg).strip()
            # Nothing is on screen — a hidden overlay must never hold priority.
            self._priority_active = False
            self.exclusive_blocked.emit(title, msg_plain)
            logger.info("Overlay skipped — exclusive fullscreen active")
            return

        # A game may be hiding the mouse pointer and confining it to its own
        # window; a panel that cannot be pointed at cannot be used. Undone in
        # _do_hide, so the pointer goes away when this does.
        if self._game_is_running():
            SystemCursor.hold("overlay")
            self._cursor_timer.start()
        else:
            self._cursor_timer.stop()

        # Cancel any pending hide first
        self._auto_hide_timer.stop()
        self._anim.stop()
        if self._hide_anim_connected:
            try:
                self._anim.finished.disconnect(self._do_hide)
            except RuntimeError:
                pass
            self._hide_anim_connected = False
        self._hover_active        = False
        self._pending_auto_hide   = 0
        self.setWindowOpacity(0.0)

        # ── Capture current foreground ──────────────────────────────────────
        _saved_foreground = None
        if platform.system() == "Windows":
            try:
                _saved_foreground = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                pass

        self._position_top_right()

        # ── Show + topmost ──────────────────────────────────────────────────
        self.show()
        self._take_the_front()
        self._ensure_visible_on_screen()

        # Immersive games leave the pointer trapped/invisible in their HWND.
        # Drop the OS pointer onto this panel so the software arrow (and clicks)
        # land where the user can actually use the overlay.
        if self._game_is_running():
            c = self.frameGeometry().center()
            SystemCursor.warp_to(c.x(), c.y())
            SystemCursor.reassert()

        # Periodic re-assert for z-order recovery.
        # On Windows, also install an instant foreground-change hook.
        self._topmost_timer.start()
        if platform.system() == "Windows":
            self._install_foreground_hook()

        # ── Restore the game's foreground ───────────────────────────────────
        if _saved_foreground and platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                if user32.IsWindow(_saved_foreground):
                    current = user32.GetForegroundWindow()
                    our_hwnd = int(self.winId())
                    if current == our_hwnd and _saved_foreground != our_hwnd:
                        user32.AllowSetForegroundWindow(-1)
                        user32.SetForegroundWindow(_saved_foreground)
            except Exception:
                pass

        # Fade-in
        self._anim.setDuration(_FADE_IN_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

        if auto_hide > 0:
            self._auto_hide_timer.start(auto_hide)

    def hide_animated(self):
        self._auto_hide_timer.stop()
        self._topmost_timer.stop()
        self._uninstall_foreground_hook()
        self._pending_auto_hide = 0
        self._set_mode("")
        # When the user explicitly closes the overlay, reset the notification
        # queue so stale notifications don't reappear on the next show_animated.
        self._clear_notification_queue()
        # Any priority prompt is now resolved/closed: drop the lock and replay
        # deferred notifications AFTER the fade-out (an inline replay would be
        # cancelled by the fade-out started below).
        self._priority_active = False
        if self._deferred_notifs:
            QTimer.singleShot(_FADE_OUT_MS + 30, self._flush_deferred_notifs)
        self._anim.stop()
        if self._hide_anim_connected:
            try:
                self._anim.finished.disconnect(self._do_hide)
            except (RuntimeError, TypeError):
                pass
        self._hide_anim_connected = False
        self._anim.setDuration(_FADE_OUT_MS)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._do_hide)
        self._hide_anim_connected = True
        self._anim.start()

    # ── Foreground change hook (Windows) ────────────────────────────────────

    def _install_foreground_hook(self):
        """Install a WinEvent hook that fires when ANY window becomes foreground.

        This lets us react instantly when a game enters fullscreen (or any
        window covers us) instead of waiting up to 1 s for the polling timer.
        """
        if platform.system() != "Windows" or self._fg_hook is not None:
            return
        try:
            # EVENT_SYSTEM_FOREGROUND = 0x0003
            # WINEVENT_OUTOFCONTEXT  = 0x0000  (callback in our own process)
            EVENT_SYSTEM_FOREGROUND = 0x0003
            WINEVENT_OUTOFCONTEXT   = 0x0000

            # WinEventProc signature:
            #   void callback(HWINEVENTHOOK, DWORD, HWND, LONG, LONG, DWORD, DWORD)
            WINEVENTPROC = ctypes.WINFUNCTYPE(
                None,
                ctypes.c_void_p,   # hWinEventHook
                wintypes.DWORD,    # event
                wintypes.HWND,     # hwnd
                ctypes.c_long,     # idObject
                ctypes.c_long,     # idChild
                wintypes.DWORD,    # idEventThread
                wintypes.DWORD,    # dwmsEventTime
            )

            def _on_foreground_change(_hook, _event, hwnd, _obj, _child, _tid, _time):
                try:
                    if getattr(self, '_cleaned_up', False):
                        return
                    try:
                        if hwnd is None:
                            return
                        our_hwnd = int(self.winId())
                        if hwnd == our_hwnd:
                            return
                    except (RuntimeError, Exception):
                        return
                    new_fg = int(hwnd) if hwnd else 0
                    QTimer.singleShot(0, lambda fg=new_fg: self._on_foreground_lost(fg))
                except Exception:
                    pass

            # prevent GC of the callback
            self._fg_hook_proc = WINEVENTPROC(_on_foreground_change)

            user32 = ctypes.windll.user32
            self._fg_hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,   # eventMin
                EVENT_SYSTEM_FOREGROUND,   # eventMax
                None,                      # hmodWinEventProc (None = in-process)
                self._fg_hook_proc,
                0,                         # idProcess (0 = all)
                0,                         # idThread  (0 = all)
                WINEVENT_OUTOFCONTEXT,
            )
            if self._fg_hook:
                logger.debug("Foreground change hook installed")
            else:
                logger.debug("Failed to install foreground change hook")
                self._fg_hook_proc = None
        except Exception as e:
            logger.debug(f"Could not install foreground hook: {e}")

    def _uninstall_foreground_hook(self):
        """Remove the WinEvent hook."""
        if self._fg_hook is not None:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._fg_hook)
            except Exception:
                pass
            self._fg_hook = None
            self._fg_hook_proc = None
            logger.debug("Foreground change hook removed")

    def _hide_for_exclusive(self):
        """Hide the overlay to protect a game's exclusive fullscreen mode.

        Centralised helper so every code path (hook, poll) behaves identically:
        stops timers, unhooks, hides, and emits the signal for audio feedback.
        """
        # Collect info before hiding (widget may not be queryable after hide)
        title = self._title.text() if hasattr(self, '_title') else t("app.name")
        msg = self._message.text() if hasattr(self, '_message') else ""
        msg_plain = re.sub(r'<[^>]+>', '', msg).strip()

        # Stop everything first, then hide
        self._auto_hide_timer.stop()
        self._topmost_timer.stop()
        self._uninstall_foreground_hook()
        # Reset animation state to avoid stale _hide_anim_connected flag
        self._anim.stop()
        if self._hide_anim_connected:
            try:
                self._anim.finished.disconnect(self._do_hide)
            except (RuntimeError, TypeError):
                pass
            self._hide_anim_connected = False
        self.hide()
        # Nothing is on screen — a hidden overlay must never hold priority,
        # nor keep a pointer raised for a panel that is no longer there.
        self._priority_active = False
        self._cursor_timer.stop()
        SystemCursor.release("overlay")
        self.exclusive_blocked.emit(title, msg_plain)
        logger.info("Overlay hidden — exclusive fullscreen protected")

    def _on_foreground_lost(self, fg_hwnd: int = 0):
        """Called (on Qt thread) when another window becomes foreground.

        *fg_hwnd* is the HWND captured at callback time (avoids a race where
        GetForegroundWindow() returns a different window by the time we run).

        If the overlay is visible:
        - Exclusive fullscreen → hide immediately (protect the game's display mode)
        - Borderless / normal  → re-assert TOPMOST (safe)
        """
        if not self.isVisible() or self.windowOpacity() < 0.1:
            return

        if self._is_exclusive_fullscreen_hwnd(fg_hwnd):
            self._hide_for_exclusive()
        elif not popup_is_open():
            self._take_the_front()
            self._trace_z("the window in front changed")

    # ── Topmost helpers ──────────────────────────────────────────────────────

    def _is_overlay_obscured(self) -> bool:
        """Check if our overlay is behind the foreground window (z-order lost)."""
        if platform.system() != "Windows":
            return False
        try:
            user32 = ctypes.windll.user32
            our_hwnd = int(self.winId())
            fg_hwnd = user32.GetForegroundWindow()
            if not fg_hwnd or fg_hwnd == our_hwnd:
                return False
            fg_rect = wintypes.RECT()
            user32.GetWindowRect(fg_hwnd, ctypes.byref(fg_rect))
            our_rect = wintypes.RECT()
            user32.GetWindowRect(our_hwnd, ctypes.byref(our_rect))
            # Check if the foreground window's rect fully contains ours
            return (fg_rect.left <= our_rect.left and fg_rect.top <= our_rect.top
                    and fg_rect.right >= our_rect.right and fg_rect.bottom >= our_rect.bottom)
        except Exception:
            return False

    def _ensure_topmost(self):
        """Periodically ensure overlay stays on top (backup for the hook).

        Windows scenarios:
        1. Game entered exclusive fullscreen → hide to protect display mode
        2. Borderless fullscreen → safe re-assert TOPMOST
        3. Non-fullscreen window covers us → simple re-assert

        Linux/macOS:
        - No reliable fullscreen detection; just call raise_() every tick
          to re-assert z-order. This is lightweight (no Win32 API calls).
        """
        if not self.isVisible():
            return
        # The 📌 menu opens inside the overlay's own rectangle. Putting the
        # overlay back on top once a second while that menu is up is what
        # buries it, so nothing is raised while a menu of ours is open.
        if popup_is_open():
            return

        if platform.system() != "Windows":
            # On Linux/macOS: unconditional raise_() via _force_topmost.
            # This is the only mechanism we have to recover z-order.
            self._take_the_front()
            return

        if self._is_fullscreen_active():
            if self._is_exclusive_fullscreen():
                self._hide_for_exclusive()
            else:
                self._take_the_front()
                self._trace_z("borderless fullscreen")
        elif self._is_overlay_obscured():
            self._take_the_front()
            self._trace_z("obscured by a window")

    def _take_the_front(self):
        """Raise the overlay — then hand the front straight back to the pins.

        A pin is something the user put on screen to keep reading; the overlay
        announces things and goes away. So the pins go above it, always, and
        the only place that can be guaranteed is here: every raise the overlay
        does passes through this.
        """
        _force_topmost(self)
        try:
            from ui.widgets.pins import get_pin_manager
            get_pin_manager().assert_topmost_all("the overlay came forward")
        except Exception:
            logger.debug("overlay: could not put the pins back on top",
                         exc_info=True)

    def _trace_z(self, why: str):
        """Say where the overlay ended up, on the same switch as the pins.

        The overlay and the pins share the raising mechanism, so they share
        its failures; having both report in the same words is what makes a
        log answer "which of the two was underneath" without guessing.
        """
        if TRACE_Z:
            logger.info(f"overlay: put back on top ({why}) — {z_report(self)}")
        else:
            logger.debug(f"Re-asserted topmost ({why})")

    def _game_is_running(self) -> bool:
        from ui.helpers import game_is_running
        return game_is_running()

    def _do_hide(self):
        """Slot called when hide animation finishes — resets the connection flag."""
        # Let go of the pointer. It only actually goes down if nothing else
        # still needs it — a note being typed into, or a pin mid-drag.
        self._cursor_timer.stop()
        SystemCursor.release("overlay")
        self._hide_anim_connected = False
        self._topmost_timer.stop()
        self._uninstall_foreground_hook()
        try:
            self._anim.finished.disconnect(self._do_hide)
        except RuntimeError:
            pass   # already disconnected — harmless
        self.hide()

    def _get_active_screen_geometry(self):
        """Get the available geometry of the screen where the active window / cursor is."""
        # Use the screen under the cursor — most reliable when resolution changes
        screen = QApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry()

    def _position_top_right(self):
        screen = self._get_active_screen_geometry()
        self.adjustSize()

        # Check if any window is in fullscreen mode
        if self._is_fullscreen_active():
            # Position more conservatively in fullscreen
            self.move(screen.right() - self.width() - 10, screen.top() + 10)
        else:
            # Normal positioning
            self.move(screen.right() - self.width() - 20, screen.top() + 20)

    def _position_relative_to_active_window(self):
        """Position overlay relative to the currently active window/game."""
        try:
            if platform.system() == "Windows":
                # Get foreground window handle
                user32 = ctypes.windll.user32
                foreground_hwnd = user32.GetForegroundWindow()
                
                if foreground_hwnd:
                    # Get window rect
                    rect = wintypes.RECT()
                    user32.GetWindowRect(foreground_hwnd, ctypes.byref(rect))
                    
                    # Get screen geometry for the screen containing the active window
                    screen = self._get_active_screen_geometry()
                    self.adjustSize()
                    
                    # Position overlay in top-right corner of the active window
                    # but ensure it stays within screen bounds
                    overlay_x = min(rect.right - self.width() - 10, screen.right() - self.width() - 10)
                    overlay_y = max(rect.top + 10, screen.top() + 10)

                    self.move(overlay_x, overlay_y)
                    logger.info(f"Positioned overlay relative to active window at ({overlay_x}, {overlay_y})")
                    return
        except Exception as e:
            logger.error(f"Failed to position relative to active window: {e}")
        
        # Fallback to standard positioning
        self._position_top_right()

    def _is_fullscreen_active(self) -> bool:
        """Check if the foreground window is running in fullscreen (borderless or exclusive)."""
        try:
            if platform.system() == "Windows":
                user32 = ctypes.windll.user32

                hwnd = user32.GetForegroundWindow()
                if not hwnd:
                    return False

                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                win_w = rect.right - rect.left
                win_h = rect.bottom - rect.top

                center = QPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
                screen = QApplication.screenAt(center)
                if screen:
                    sg = screen.geometry()
                    if abs(win_w - sg.width()) <= 2 and abs(win_h - sg.height()) <= 2:
                        return True
            else:
                # On Linux/macOS, Qt doesn't expose a reliable fullscreen-window query.
                # Comparing screen.geometry() == screen.availableGeometry() gives false
                # positives on desktops without taskbars. Return False as conservative default.
                return False
        except Exception:
            pass
        return False

    def _is_exclusive_fullscreen(self) -> bool:
        """Detect DirectX/Vulkan exclusive fullscreen (display mode changed).
        In exclusive fullscreen, showing ANY window risks minimizing the game.
        Returns True only for true exclusive mode, not borderless fullscreen."""
        if platform.system() != "Windows":
            return False
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return self._is_exclusive_fullscreen_hwnd(int(hwnd) if hwnd else 0)
        except Exception:
            return False

    def _is_exclusive_fullscreen_hwnd(self, hwnd: int) -> bool:
        """Check if *hwnd* is in exclusive fullscreen mode.

        Separated from _is_exclusive_fullscreen so the hook callback can pass
        the HWND captured at event time (avoids race with GetForegroundWindow).
        """
        if platform.system() != "Windows" or not hwnd:
            return False
        try:
            user32 = ctypes.windll.user32

            if not user32.IsWindow(hwnd):
                return False

            # Exclusive fullscreen windows typically have NO border, NO caption,
            # and cover the exact display resolution.
            GWL_STYLE = -16
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            WS_CAPTION = 0x00C00000
            WS_BORDER  = 0x00800000

            if bool(style & WS_CAPTION) or bool(style & WS_BORDER):
                return False  # has window chrome → borderless at most

            # Check if the window covers the full screen
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            win_w = rect.right - rect.left
            win_h = rect.bottom - rect.top

            center = QPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            screen = QApplication.screenAt(center)
            if screen:
                sg = screen.geometry()
                if abs(win_w - sg.width()) <= 2 and abs(win_h - sg.height()) <= 2:
                    return True
        except Exception:
            pass
        return False

    # Screen add/remove/geometry wiring comes from ScreenSignalMixin;
    # only the reaction is overlay-specific:
    def _on_screen_changed(self, *_args):
        """Re-position overlay when screen geometry changes."""
        if self.isVisible():
            logger.debug("Screen geometry changed — repositioning overlay")
            self._position_top_right()

    def deleteLater(self):
        """Ensure hooks and screen signals are disconnected before destruction."""
        self.cleanup()
        super().deleteLater()

    def cleanup(self):
        """Disconnect screen signals and hooks to prevent crashes after widget destruction."""
        self._cleaned_up = True
        self._uninstall_foreground_hook()
        self._screen_signals_cleanup()

    def _ensure_visible_on_screen(self):
        """Make sure overlay is visible on current screen configuration."""
        try:
            screen = self._get_active_screen_geometry()
            # If overlay is outside screen bounds, reposition it
            if not screen.contains(self.geometry()):
                self._position_top_right()
        except Exception:
            pass

    # ── Paint & drag ──────────────────────────────────────────────────────────

    def _style_carousel_arrows(self):
        """The look of the two browse arrows, from the current palette.

        A method rather than a local string because refresh_styles has to be
        able to say it again: built once with whatever theme happened to be on
        at the time, the arrows kept a dark box and pale glyph after a switch
        to the light theme, which on a light card reads as two black blocks.
        """
        style = (
            f"QPushButton{{background:{palette('bg_elevated')};"
            f"color:{palette('text')};border:1px solid {palette('border')};"
            f"border-radius:4px;font-size:15px;font-weight:bold;padding:0;}}"
            f"QPushButton:hover{{background:{palette('accent')};"
            f"border-color:{palette('accent')};color:#000;}}"
            f"QPushButton:disabled{{color:{palette('text_muted')};"
            f"border-color:{palette('border')};"
            f"background:{palette('bg_elevated')};}}"
        )
        self._carousel_prev.setStyleSheet(style)
        self._carousel_next.setStyleSheet(style)

    def refresh_styles(self):
        """Re-apply all inline styles after theme change."""
        from ui.styles.theme import get_theme_manager
        self._is_dark = get_theme_manager().is_dark()
        self._refresh_badge_interactivity()
        self._icon_label.setStyleSheet("font-size: 15px; min-width: 20px; background: transparent;")
        sep = self.findChild(QFrame, "overlay_separator")
        if sep:
            sep.setStyleSheet(f"background:{palette('border_hover')};border:none;max-height:1px;")
        self._message.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
        self._style_carousel_arrows()
        self._suppress_btn.setStyleSheet(
            f"QPushButton{{font-size:10px;color:{palette('text_muted')};padding:4px 8px;"
            f"border:1px solid {palette('border_hover')};border-radius:4px;background:transparent;}}"
            f"QPushButton:hover{{color:{palette('text')};border-color:{palette('accent')};background:{palette('bg_elevated')};}}"
        )
        # The dashboard rows need nothing: #dash_key / #dash_value come from
        # the theme (a highlighted value keeps its own accent colour).
        self.update()  # trigger repaint

    def paintEvent(self, event):
        from ui.styles.theme import get_theme_manager
        is_dark = getattr(self, '_is_dark', get_theme_manager().is_dark())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        if is_dark:
            painter.setBrush(QBrush(QColor(13, 13, 18, 235)))
            painter.setPen(QPen(QColor(42, 42, 56, 200), 1))
            accent = QColor(118, 185, 0, 180)
        else:
            painter.setBrush(QBrush(QColor(255, 255, 255, 245)))
            painter.setPen(QPen(QColor(210, 210, 225, 200), 1))
            accent = QColor(90, 148, 0, 200)
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(QPen(accent, 2))
        painter.drawLine(rect.left() + 12, rect.top() + 1,
                         rect.right() - 12, rect.top() + 1)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if hasattr(self, "_drag_start") and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, "_drag_start"):
            del self._drag_start
