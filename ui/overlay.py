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
import os
import platform
import re
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, Slot, QPoint, QObject, QEvent
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QApplication,
                               QStyle, QStyleOptionButton, QStylePainter)

from i18n import t
from ui.backup_labels import origin_badge
from ui.helpers import scaled
from ui.styles.theme import palette
from ui import win_band_hook

if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

logger = logging.getLogger(__name__)


def _xwayland_session() -> bool:
    """True when X clients here are bridged by a Wayland compositor.

    Not "is Qt on the Wayland plugin" — this is about the X server on the
    other end of the connection. SaveSync deliberately asks for xcb even in
    a Wayland session (positioning and the global hotkey need it), so the
    plugin says nothing about who manages the windows.
    """
    if platform.system() == "Windows":
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY")
                or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")

# What the shell believes about the foreground, and whether a window fills its monitor, are answered in
# ui.exclusive_presenter - the pins ask the same questions. Kept here under their old names.
from ui.exclusive_presenter import (QUNS_RUNNING_D3D_FULL_SCREEN as _QUNS_RUNNING_D3D_FULL_SCREEN,
                                    shell_notification_state as _shell_notification_state,
                                    window_fills_monitor as _window_fills_monitor)


_XP_WATCH_S = 3.0        # how long a picture shown over an exclusive-fullscreen game is watched for a kick
_AUTO_HIDE_MS  = 5000    # default timeout if user doesn't hover
# A message from a friend has a button to read it: it stays a little longer.
_FRIEND_AUTO_HIDE_MS = 12000
_FADE_IN_MS    = 200
_FADE_OUT_MS   = 280


from ui.helpers import (format_clock, force_topmost as _force_topmost, popup_is_open,
                        ScreenSignalMixin, SystemCursor, TRACE_Z, z_report)


def _engine_badge_html(engine: str, widget=None) -> str:
    """Muted label for an engine name, or '' when there is nothing to show.

    *widget* is whoever the text is for: without one the size is measured on
    the primary screen, which is not where the overlay necessarily appears."""
    engine = (engine or "").strip()
    if not engine:
        return ""
    return (
        f" <span style='color:{palette('text_muted')};font-size:{scaled(11, widget)}px;"
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


def _main_line_first(backups) -> list:
    """The restore list's order: the real history, then the side branch
    (provisional / pre-restore copies) — the same split the Backups page
    makes. Stable, so each group stays newest-first."""
    from core.backup import BackupManager
    return sorted(backups, key=BackupManager.is_side_backup)


# Recent pins beyond this many scroll instead of growing the menu.
_PIN_MENU_ROWS = 5


class _PaintAsHovered(QObject):
    """Makes one push button paint the way its stylesheet draws it with the pointer on it (and, when asked, pressed).

    The picture over an exclusive-fullscreen game is a still taken of the widget, and there is no pointer on the
    widget to make its :hover rules apply. Setting the "under the mouse" flag by hand does not change how a
    stylesheet-styled button is drawn; asking the style to draw it with the state set does - and it is the very
    rule the live window uses, so every button looks the same in both."""

    def __init__(self, button, pressed: bool = False):
        super().__init__(button)
        self._button = button
        self._pressed = pressed

    def eventFilter(self, obj, event):
        if obj is self._button and event.type() == QEvent.Type.Paint:
            opt = QStyleOptionButton()
            self._button.initStyleOption(opt)
            opt.state |= QStyle.StateFlag.State_MouseOver
            if self._pressed:
                opt.state |= QStyle.StateFlag.State_Sunken
                opt.state &= ~QStyle.StateFlag.State_Raised
            painter = QStylePainter(self._button)
            painter.drawControl(QStyle.ControlElement.CE_PushButton, opt)
            return True
        return False


class OverlayWidget(QWidget, ScreenSignalMixin):
    action_requested  = Signal(str, str)   # action, context
    dismissed         = Signal()
    dont_show_again   = Signal(str)        # exe_path
    # A show_path_changed prompt timed out with no click — see
    # _on_expire_action / _auto_hide_due. Carries the same "id|exe_path"
    # context an explicit action would; unlike action_requested, this is
    # NEVER emitted for a button click, only a genuine unattended expiry.
    path_changed_expired = Signal(str)
    # Emitted when exclusive fullscreen prevents the overlay from showing.
    # Carries (title, message) so the caller can route it through another channel.
    exclusive_blocked = Signal(str, str)
    # "Don't show again" on a friend's message (the friend's id): the friend is silenced
    # in the sealed friend file, not in config.json - which would list the friends.
    friend_silenced = Signal(str)
    # The same for a group's messages (the group's id): silenced in the sealed friend file too.
    group_silenced = Signal(str)
    # The bin on the unknown-game queue (the exe path): out of the queue, NOT banned - the main window
    # takes it out of the shared queue and nothing is added to the suppress list.
    unknown_trashed = Signal(str)
    # Provider restore-list fetch completed (payload: entries, game_id,
    # local_ids, seq). Emitted FROM THE WORKER THREAD — a queued signal is
    # the only safe hop back to the GUI thread; the old QTimer.singleShot
    # from the plain thread never fired (no Qt event loop there), which
    # left the list stuck on "loading" forever.
    _restore_fetch_done = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Scale bookkeeping (see _sync_scale_to_screen). Set before anything
        # is built: building asks scale_screen() for every scaled() size.
        self._applied_scale: float | None = None   # scale the chrome was last sized for
        self._own_sheet_key = None                 # (theme, scale) of this window's own stylesheet
        self._last_scale_log = None                # (screen, scale) last written to the log
        self._resyncing = False
        # Monitors being added, removed or renegotiating fire a burst of
        # signals; the work waits for them to stop.
        self._screen_settle_timer = QTimer(self)
        self._screen_settle_timer.setSingleShot(True)
        self._screen_settle_timer.setInterval(250)
        self._screen_settle_timer.timeout.connect(self._resync_visible)
        self._context_exe         = ""
        self._hide_anim_connected = False
        self._hover_active        = False   # True while mouse is inside overlay
        self._auto_hide_ms        = 0       # the countdown in force, in full
        # Set by show_path_changed right after it starts a REAL countdown;
        # fired by _auto_hide_due on a genuine unattended expiry only, then
        # cleared. show_animated() also clears it on every call, so a stale
        # callback from a preempted prompt can never fire for whatever the
        # overlay has moved on to showing.
        self._on_expire_action    = None

        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self._auto_hide_due)

        # Timer to periodically re-assert topmost status for fullscreen games
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_topmost)
        self._topmost_timer.setInterval(400)   # Check every moment: an ordinary window left over a game that has just gone exclusive is invisible but still takes the mouse

        # A picture of the overlay in a click-through window, for a game in exclusive
        # fullscreen (see _present_over_exclusive). The window is made on first use.
        self._xp = None
        self._xp_watch = None
        self._xp_exe = ""
        self._xp_watch_until = 0.0
        self._xp_suspects: dict[str, int] = {}     # exe -> consecutive unclear losses (see _presenter_lost_front)
        self._xp_mon = None                        # the monitor the picture is on, in physical pixels
        self._xp_game = 0                          # the game window the picture is shown over
        self._xp_own: set = set()                  # the app's own windows that were up when it appeared
        self._xp_hits: list = []                   # (rect in picture pixels, button): what a click can reach
        self._xp_hover = False
        self._xp_pressed = None
        self._xp_hover_btn = None                  # the button the pointer is on, in the picture
        # True while the overlay's own window is up over a game in exclusive fullscreen (made in band 16, see
        # ui.win_band_hook) - the picture is only the fallback for when that cannot be done.
        self._rw_exclusive = False
        self._capturing = False                    # a screen capture is under way (it holds the exclusive session)
        self.setProperty("xs_keep", True)          # its band-16 window is managed here, not swept (ui.exclusive_session)
        self._tool_menu = None                     # the menu that is open as a non-activating tool window
        self._tool_menu_outside = 0
        self._tool_menu_timer = QTimer(self)
        self._tool_menu_timer.setInterval(500)
        self._tool_menu_timer.timeout.connect(self._tool_menu_tick)
        self._xp_arrow = None                      # a second picture: the mouse arrow, when the game hid the real one
        self._xp_arrow_data = None
        self._xp_cursor_timer = QTimer(self)
        self._xp_cursor_timer.setInterval(30)
        self._xp_cursor_timer.timeout.connect(self._xp_cursor_tick)
        self._session_start = 0.0                  # epoch seconds the running game's session began; 0 = none
        self._x11_bypass = False                   # X11: override-redirect while a fullscreen window is in front
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self._session_tick)
        self._xp_watch_timer = QTimer(self)
        self._xp_watch_timer.setInterval(40)    # the front can be empty for only ~150 ms in a real kick
        self._xp_watch_timer.timeout.connect(self._check_presenter_front)

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
        # Per queue entry: (game_id, notif_type, action). *action* is None or
        # (button label, action name, context): a notification with a primary
        # button of its own (a friend's message: open it).
        self._notif_meta: list[tuple] = []
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
        from ui.helpers import ui_scale
        self._applied_scale = ui_scale(self)
        # Worker-thread → GUI-thread hop for the provider restore-list fetch
        # (queued automatically because the emit happens off-thread).
        self._restore_fetch_done.connect(self._on_restore_fetch_done)

    # ── Window ────────────────────────────────────────────────────────────────

    def _setup_window(self):
        flags = (
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool                 |
            Qt.WindowType.NoDropShadowWindowHint
        )
        if platform.system() == "Windows":
            # WS_EX_NOACTIVATE: the panel never takes keyboard focus from
            # the game, and mouse input reaches it perfectly well.
            #
            # Not set elsewhere, and that is the point. X11 implements this
            # by clearing the window's ICCCM input hint, which is a much
            # stronger statement — a window that tells the manager it takes
            # no input is one several managers then decline to route clicks
            # to. The overlay is a dashboard with buttons on it. On X11 the
            # WA_ShowWithoutActivating below already gives the half that
            # matters: it appears without stealing focus.
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        elif _xwayland_session():
            # Under a Wayland compositor's X window manager, a client does
            # not get to say where its windows go — placement belongs to
            # the compositor, and the X bridge honours the protocol by
            # ignoring the request. Measured on WSLg: the panel is mapped,
            # viewable, correctly sized, and parked at -32768,-32768. Off
            # screen, so the pointer never reaches it: no hover, no clicks,
            # no way to close it, and an auto-hide that cannot see a cursor
            # it is nowhere near.
            #
            # Override-redirect takes the window manager out of the loop
            # entirely: the panel is placed exactly where it asks to be and
            # nothing reparents or reframes it. That is what this flag is
            # for, and the panel gives up nothing by using it — it is
            # frameless, always on top, driven with the mouse, and asks for
            # no keyboard focus in the first place.
            #
            # Only here. On a plain X11 desktop the window manager places
            # the panel correctly (verified under Openbox: positioned top
            # right, enter/move/press/release all delivered), and a managed
            # window is the better neighbour.
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # WA_X11DoNotAcceptFocus is deliberately NOT set, despite the name
        # reading like Linux support. WindowDoesNotAcceptFocus above already
        # says "do not take keyboard focus" on every platform, and does it
        # in the way that keeps the panel CLICKABLE — on Windows it becomes
        # WS_EX_NOACTIVATE, which passes mouse input through perfectly well.
        # The X11 attribute is the stronger statement: it clears the ICCCM
        # input hint, and a window that tells the window manager it accepts
        # no input at all is one several managers then decline to interact
        # with. The overlay is a dashboard with buttons, not a toast.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Explicit arrow so immersive games that set a NULL cursor still get
        # a pointer shape while the mouse is over this panel.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setObjectName("overlay")
        self.setFixedWidth(scaled(340, self))

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
        self._unknown_badge.setFixedHeight(scaled(20, self))
        self._unknown_badge.setToolTip(t("unknown_history.badge_tooltip"))
        self._unknown_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._unknown_badge.clicked.connect(self._on_badge_clicked)
        from ui import unknown_history
        unknown_history.signals.changed.connect(self._on_unknown_queue_changed)
        self._unknown_badge.setVisible(False)
        self._refresh_badge_interactivity()
        self._title = QLabel(t("app.name"))
        self._title.setObjectName("overlay_title")
        # [i]: opens the automatic save-path scan panel so detected files
        # can be reviewed/accepted WITHOUT leaving the game. Shown only on
        # the manual (hotkey) overlay — see show_manual/_clear_buttons.
        self._info_btn = QPushButton("i")
        _ib = scaled(20, self)
        self._info_btn.setFixedSize(_ib, _ib)
        self._info_btn.setToolTip(t("overlay.open_auto_scan"))
        self._info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._info_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._info_btn.setObjectName("overlay_info_btn")
        self._info_btn.clicked.connect(lambda: self._on_action("open_auto_scan"))
        self._info_btn.setVisible(False)
        # 📌: the recently pinned notes/images, plus a way to pin a new one,
        # reachable without leaving the game. Same visibility rule as [i] —
        # only on the manual (hotkey) overlay, see _clear_buttons.
        self._pin_btn = QPushButton("📌")
        self._pin_btn.setFixedSize(_ib, _ib)
        self._pin_btn.setToolTip(t("pin.menu_tooltip"))
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pin_btn.setObjectName("icon_btn")
        self._pin_btn.clicked.connect(self._show_pin_menu)
        self._pin_btn.setVisible(False)
        # The bin on the unknown-game queue: this game leaves the list WITHOUT being banned (it is asked about
        # again the next time it runs). In the header beside the close button, away from "Add to library", so a
        # press meant for one cannot land on the other. Shown only on the queue view - see _render_unknown_entry.
        self._trash_btn = QPushButton("\U0001f5d1")
        self._trash_btn.setObjectName("icon_btn")
        self._trash_btn.setFixedSize(_ib, _ib)
        self._trash_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._trash_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._trash_btn.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._trash_btn.clicked.connect(self._on_trash_unknown)
        self._trash_btn.setVisible(False)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("icon_btn")
        close_btn.setFixedSize(_ib, _ib)
        close_btn.clicked.connect(self.hide_animated)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header.addWidget(self._unknown_badge)
        header.addWidget(self._title, 1)
        header.addWidget(self._pin_btn)
        header.addWidget(self._info_btn)
        header.addWidget(self._trash_btn)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # ── Message area with flanking carousel arrows ────────────────────────
        # Layout: [‹ prev] [icon + message area] [next ›]
        # Arrows are hidden when only one notification is queued.
        msg_row = QHBoxLayout()
        msg_row.setSpacing(4)
        msg_row.setContentsMargins(0, 0, 0, 0)

        self._carousel_prev = QPushButton("‹")
        self._carousel_prev.setFixedSize(scaled(18, self), scaled(48, self))
        self._carousel_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self._carousel_prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._carousel_prev.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._carousel_prev.setVisible(False)
        self._carousel_prev.clicked.connect(self._carousel_go_prev)

        self._carousel_next = QPushButton("›")
        self._carousel_next.setFixedSize(scaled(18, self), scaled(48, self))
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
        self._message.setFixedHeight(scaled(52, self))
        centre_col.addWidget(self._message)

        # Counter (e.g. "2 / 5") below message, centred
        self._carousel_counter = QLabel("")
        self._carousel_counter.setObjectName("overlay_carousel_counter")
        self._carousel_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        # The session counter: a row of its own (so that hiding it leaves no gap), shown only while a
        # tracked game is running. Fed by show_manual, ticked by _session_tick.
        self._session_row = QWidget()
        self._session_row.setObjectName("transparent_bg")
        srow = QHBoxLayout(self._session_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(8)
        sk = QLabel(); sk.setObjectName("dash_key")
        sv = QLabel(); sv.setObjectName("dash_value")
        srow.addWidget(sk); srow.addWidget(sv, 1)
        dl.addWidget(self._session_row)
        self._session_row.setVisible(False)
        self._dash_rows.append((sk, sv))
        layout.addWidget(self._dashboard)

        self._btn_area     = QHBoxLayout(); self._btn_area.setSpacing(6)
        self._quick_area   = QHBoxLayout(); self._quick_area.setSpacing(6)
        layout.addLayout(self._btn_area)
        layout.addLayout(self._quick_area)

        # Quick-restore backup list (hidden by default)
        self._restore_area = QVBoxLayout()
        self._restore_area.setSpacing(3)
        layout.addLayout(self._restore_area)

        # A list of choices shown inside the card in place of a popup menu (the picture over an exclusive-fullscreen
        # game). A layout of its own: the restore list above can be open on the same card.
        self._inline_area = QVBoxLayout()
        self._inline_area.setSpacing(3)
        layout.addLayout(self._inline_area)

        self._suppress_btn = QPushButton()
        self._suppress_btn.setObjectName("overlay_suppress_btn")
        self._suppress_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._suppress_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._suppress_btn.clicked.connect(self._on_suppress)
        layout.addWidget(self._suppress_btn)

    def _setup_animation(self):
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ── Mouse hover — pauses auto-hide timer ─────────────────────────────────

    def _make_clickable_off_windows(self):
        """Take focus on X11 when there is no game to take it from.

        The pointer already reaches this panel — the cursor changes shape
        over it — so the events arrive; what does not arrive is the CLICK.
        Under a click-to-focus window manager the first press on a window
        that is not the active one is swallowed to focus it, and a window
        that never becomes active swallows every press, for ever. The panel
        has buttons on it, so that makes it furniture.

        Only where the trade does not cost anything. On Windows the panel
        deliberately never activates: it is shown over a running game and
        taking focus would minimise it. With no game running there is
        nothing to take focus from, and being the active window is the
        difference between a dashboard and a picture of one.
        """
        if platform.system() == "Windows":
            return
        if _xwayland_session():
            # The bypass flag took the window manager out of the loop, so
            # there is no activation to ask for and nothing that would
            # grant it. Measured: press and release arrive on their own.
            return
        try:
            if self._game_is_running():
                return
            self.raise_()
            self.activateWindow()
        except Exception:
            logger.debug("Could not activate the overlay", exc_info=True)

    def _auto_hide_due(self):
        """The countdown ran out. Hide, unless the pointer is really on it.

        Asked here rather than kept as a paused timer, and that is the
        whole point. The old shape stopped the timer on enterEvent and
        restarted it on leaveEvent — so a window that OPENS under the
        pointer gets the enter, never gets the leave, and stays up for
        ever. On Windows the overlay is placed in a screen corner away
        from the cursor and this never showed; on a Wayland compositor,
        which places the window where it likes, it happens on the first
        notification and the overlay is then stuck open for the session.

        Checking the cursor when the timer fires cannot wedge, even though
        a hover now buys the whole countdown back rather than a 1.5 s
        scrap: the timer always runs, the pointer is re-read every time it
        fires, and the moment the pointer is elsewhere the next expiry
        hides the panel. Nothing is ever paused indefinitely — the
        countdown is restarted, and restarting it is a decision taken with
        the cursor's real position in hand.
        """
        try:
            from PySide6.QtGui import QCursor
            # mapFromGlobal, not geometry().contains: geometry() is where
            # this process ASKED to be, and a compositor that places the
            # window itself leaves that number stale — the pointer then
            # never counts as inside and the panel closes under the user's
            # hand. mapFromGlobal asks the platform where the window really
            # is. underMouse() is checked too, for the case where Qt has
            # tracked the enter but the coordinates disagree.
            presented = self._presenting()
            inside = self._xp_hover if presented else self.underMouse()
            if not inside and not presented:
                inside = self.rect().contains(
                    self.mapFromGlobal(QCursor.pos()))
            if (self.isVisible() or presented) and inside:
                self._hover_active = True
                # The FULL countdown, not a 1.5 s stub. Granting a fixed
                # scrap each time the timer expired meant the panel lived
                # in 1.5 s instalments for as long as the pointer was on
                # it — a countdown that never restarted and never quite
                # ran out. Restarting it whole is what "the timer resets
                # while you are reading it" means.
                self._auto_hide_timer.start(self._auto_hide_ms or _AUTO_HIDE_MS)
                return
        except Exception:
            pass
        self._hover_active = False
        cb, self._on_expire_action = self._on_expire_action, None
        if cb:
            try:
                cb()
            except Exception:
                logger.exception("path-changed expire action failed")
        self.hide_animated()

    def enterEvent(self, event):
        """Mouse entered the overlay — noted, but the countdown keeps running.

        Stopping it here is what used to leave the overlay open for ever
        when no matching leaveEvent arrived. _auto_hide_due re-checks the
        pointer instead, which needs no leave event at all.
        """
        super().enterEvent(event)
        self._hover_active = True
        # The reset itself. Waiting for the countdown to expire before
        # noticing the pointer leaves the panel closing under a hand that
        # arrived a moment too late.
        if self.isVisible() and self._auto_hide_ms > 0:
            self._auto_hide_timer.start(self._auto_hide_ms)

    def leaveEvent(self, event):
        """Mouse left the overlay — the countdown, already running, runs out.

        Nothing is restarted here. There is exactly one timer and one rule:
        it is set when the panel appears, reset whenever the pointer
        arrives, and otherwise left alone.
        """
        super().leaveEvent(event)
        self._hover_active = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_buttons(self):
        # The [i] scan shortcut belongs to the manual overlay only —
        # show_manual re-enables it after this reset.
        if hasattr(self, "_info_btn"):
            self._info_btn.setVisible(False)
        if hasattr(self, "_pin_btn"):
            self._pin_btn.setVisible(False)
        if hasattr(self, "_trash_btn"):
            self._trash_btn.setVisible(False)
        for layout in (self._btn_area, self._quick_area, self._restore_area, self._inline_area):
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
        arrow_btn.setFixedWidth(scaled(24, self))

        def _open_menu():
            if self._presenting() and not self._rw_exclusive:
                # Over an exclusive-fullscreen game a popup menu is an ordinary window, which takes the game back
                # to the desktop: the alternatives are listed inside the card instead.
                self._toggle_inline(lambda: [
                    self._inline_btn(label, lambda a=act: self._on_action(a)) for label, act in menu_items])
                return
            menu = QMenu(self)
            menu.setStyleSheet(
                f"QMenu{{background:{palette('bg_card')};color:{palette('text')};"
                f"border:1px solid {palette('border_hover')};border-radius:6px;padding:4px;}}"
                f"QMenu::item{{padding:5px 14px;border-radius:4px;font-size:{scaled(11, self)}px;}}"
                f"QMenu::item:selected{{background:{palette('accent')};color:{palette('accent_text')};}}"
            )
            for label, act in menu_items:
                menu.addAction(label, lambda a=act: self._on_action(a))
            self._popup_menu(menu, arrow_btn.mapToGlobal(arrow_btn.rect().bottomLeft()))

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
        if accent:
            fs = scaled(11, self)
            v.setStyleSheet(f"color:{accent};font-size:{fs}px;font-weight:600;")
        else:
            v.setStyleSheet("")

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
                b.setFixedHeight(scaled(20, self))
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
                        f"QPushButton{{font-size:{scaled(9, self)}px;color:{palette('accent_text')};"
                        f"background:{palette('accent')};border:1px solid {palette('accent')};"
                        f"border-radius:4px;padding:0 8px;font-weight:700;}}"
                    )
                else:
                    b.setStyleSheet(
                        f"QPushButton{{font-size:{scaled(9, self)}px;color:{palette('text_muted')};"
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
                lbl = QLabel(f"<span style='color:{palette('text_hint')};font-size:{scaled(10, self)}px;'>{t('overlay.no_backups_available')}</span>")
                lay.insertWidget(lay.count() - 1, lbl)
            for bk in _main_line_first(local_backups):
                lay.insertWidget(lay.count() - 1,
                                 self._make_restore_btn(bk, game_id, cloud_only=False))
            self._resize_restore_scroll(max(1, len(local_backups)))
            return

        # Provider source: loading placeholder + background fetch
        loading = QLabel(f"<span style='color:{palette('text_hint')};font-size:{scaled(10, self)}px;'>⟳ {t('overlay.loading')}</span>")
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
                lbl = QLabel(f"<span style='color:{palette('text_hint')};font-size:{scaled(10, self)}px;'>{t('overlay.no_backups_available')}</span>")
                lay.insertWidget(lay.count() - 1, lbl)
            for bk in _main_line_first(entries):
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
        # A pre-restore copy is the other kind of side-branch entry (see
        # BackupManager.is_side_backup) and says so, rather than reading as
        # an ordinary backup among the real ones.
        from core.backup import BackupManager
        _meta = getattr(bk, "cloud_metadata", None) or {}
        # A version added by hand is beside the history too, but it is nothing
        # to be careful about: no warning colour, and it says what it is.
        added = bool(_meta.get(BackupManager.MANUAL_VERSION_KEY))
        is_temp = BackupManager.is_side_backup(bk) and not added
        if _meta.get("pre_restore_safety"):
            temp_tag = f"  ↶ {t('backup.pre_restore_safety')}"
        elif added:
            temp_tag = f"  ＋ {t('backup.manual_version')}"
        else:
            temp_tag = f"  ⏳ {t('overlay.temp_backup_tag')}" if is_temp else ""
        btn = QPushButton(f"  ↩  {date_str}  —  {badge}  —  {bk.size_human}{temp_tag}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        border_col = palette('warning') if is_temp else palette('border_hover')
        btn.setStyleSheet(
            f"QPushButton{{text-align:left;padding:4px 8px;font-size:{scaled(10, self)}px;"
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
            from ui.unknown_history import visible_entries
            n = len(visible_entries())
        except Exception:
            n = 0
        self._unknown_badge.setText(f"🎮 {n}")
        self._unknown_badge.setVisible(n > 0)
        self._refresh_badge_interactivity()

    def _refresh_badge_interactivity(self):
        """Badge affordance follows the overlay mode: clickable everywhere
        except the unknown view itself — where the arrows already browse
        the queue — so the user can always reach the queue from any
        notification."""
        clickable = self._overlay_mode != "unknown"
        base = (
            f"QPushButton{{color:{palette('accent_text')};background:{palette('accent')};"
            f"border:none;border-radius:10px;font-size:{scaled(11, self)}px;font-weight:700;"
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

    def _on_unknown_queue_changed(self):
        """The shared queue moved (a detection, an add, a "don't show again" —
        here or on the Overview). The badge follows; the carousel, when it is
        what is on screen, re-reads the list and stays on the same game if it
        is still there, or hands over to its neighbour — and closes when the
        queue has emptied."""
        self.refresh_unknown_badge()
        if self._unknown_queue is None or not self._on_screen():
            return
        entries = self._pending_unknown_entries()
        if not entries:
            self.hide_animated()
            return
        stay = self._context_exe
        exes = [e.get("exe") for e in entries]
        self._unknown_index = (exes.index(stay) if stay in exes
                               else min(self._unknown_index, len(entries) - 1))
        self._unknown_queue = entries
        self._render_unknown_entry()
        self._rerender_in_place()
        self._refresh_presenter()

    def _on_badge_clicked(self):
        if self._overlay_mode == "unknown":
            return
        self.note_user_summon()
        self.show_unknown_queue()

    def _set_mode(self, mode: str):
        """Track what the overlay is currently showing; leaving the unknown
        view drops its browse queue so the carousel arrows fall back to the
        notification queue."""
        # Every render starts here, before any text is written: the sizes baked
        # into it must be the ones for the monitor it is about to appear on.
        if mode:
            self._sync_scale_to_screen("render")
        self._overlay_mode = mode
        if mode != "unknown":
            self._unknown_queue = None
        self._refresh_badge_interactivity()

    def _ensure_band_window(self) -> None:
        """The overlay's native window must be a band-16 window (see ui.win_band_hook). Qt makes it on first need, and
        anything that needs the handle before the first show (a z-order helper) would make it in the ordinary band
        for good - so one that already exists in the wrong band is dropped and made again here."""
        if platform.system() != "Windows" or not win_band_hook.installed():
            return
        try:
            if self.windowHandle() is not None:
                if win_band_hook.window_band(int(self.winId())) == win_band_hook.BAND_SYSTEM_TOOLS:
                    return
                super().setVisible(False)
                self.destroy()
            with win_band_hook.band_windows():
                self.create()
        except Exception:
            logger.debug("Overlay: could not make its window in the higher band", exc_info=True)

    def _ensure_ordinary_window(self) -> None:
        """Band 16 is for the overlay only while a game is in exclusive fullscreen: a band-16 window is above every window
        of the ordinary band (the pins, the overlay's own menus, the app), whatever their topmost flags. So a native window
        made for that is dropped as soon as the overlay is shown for any other reason, and the next one is an ordinary one."""
        if platform.system() != "Windows" or not win_band_hook.installed() or self.windowHandle() is None:
            return
        try:
            if win_band_hook.window_band(int(self.winId())) == win_band_hook.BAND_SYSTEM_TOOLS:
                super().setVisible(False)
                self.destroy()
        except Exception:
            logger.debug("Overlay: could not drop its band-16 window", exc_info=True)

    def setVisible(self, visible):
        if visible and win_band_hook.installed():
            if getattr(self, "_rw_exclusive", False):
                self._ensure_band_window()
                with win_band_hook.band_windows():
                    super().setVisible(visible)
                return
            self._ensure_ordinary_window()
        super().setVisible(visible)

    def setWindowFlags(self, flags):
        if getattr(self, "_rw_exclusive", False):
            with win_band_hook.band_windows():          # a change of flags makes the native window again
                super().setWindowFlags(flags)
        else:
            super().setWindowFlags(flags)

    def _real_window_ok(self) -> bool:
        """May the overlay's own window be shown over a game in exclusive fullscreen? Yes when it can be made in the
        higher band, the setting allows it and the game is not on the deny-list."""
        if platform.system() != "Windows" or not win_band_hook.installed():
            return False
        try:
            from core.config_manager import get_config
            cfg = get_config()
            if not cfg.get("overlay_over_exclusive", True):
                return False
            from ui.exclusive_presenter import foreground_hwnd, process_name
            exe = process_name(foreground_hwnd())
            denied = {str(e).lower() for e in (cfg.get("overlay_window_deny", []) or [])}
            return not (exe and exe.lower() in denied)
        except Exception:
            return False

    def _begin_exclusive_session(self) -> None:
        """While the overlay's window is up over the game, what it opens (a dialog, a menu, a text field) is made for that game
        too (ui.exclusive_session). It ends by itself when the window is gone."""
        try:
            from ui.exclusive_session import get_session
            get_session().begin("overlay", lambda: self._rw_exclusive and self.isVisible())
        except Exception:
            logger.debug("Overlay: could not begin the exclusive session", exc_info=True)

    def _start_real_window_watch(self) -> None:
        """For the first seconds after the window appears over an exclusive game, the same watch as for the picture: if
        the game is taken out of its exclusive mode while it still has the front it is remembered (and not tried
        again); the player pressing Alt+Tab never counts."""
        try:
            from ui.exclusive_presenter import FrontWatch, process_name
            watch = FrontWatch()
            self._xp_watch, self._xp_exe, self._xp_game = watch, process_name(watch.hwnd), watch.hwnd
            self._xp_own = self._own_windows()
            self._xp_watch_until = time.monotonic() + _XP_WATCH_S
            self._xp_watch_timer.start()
        except Exception:
            logger.debug("Overlay: could not start watching the game", exc_info=True)

    # ── A menu of the overlay over a game in exclusive fullscreen ─────────────────────────────────────

    def _popup_menu(self, menu, pos, on_closed=None) -> None:
        """Show *menu* at *pos* (global). Normally it is the usual blocking popup. Over a game in exclusive fullscreen a
        Qt popup takes the foreground when it opens (measured), and that takes the game out of exclusive mode, so the
        very same menu is shown as a non-activating tool window in the higher band instead: it highlights, takes the
        mouse and runs its actions as it always did. Being no popup, Qt does not close it: it closes when an action
        runs, when something else of ours is pressed, when the overlay goes, or after the pointer has been away."""
        if not self._rw_exclusive:
            menu.exec(pos)
            if on_closed:
                on_closed()
            return
        self._close_tool_menu("another menu was opened")
        self._tool_menu_why = ""
        menu.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setProperty("xs_keep", True)      # the overlay closes it itself (_close_tool_menu): the end of a session must not sweep it away
        menu.triggered.connect(lambda *_: (setattr(self, "_tool_menu_why", "an entry was chosen"), menu.hide()))
        menu.aboutToHide.connect(lambda: self._tool_menu_closed(menu, on_closed))
        self._tool_menu, self._tool_menu_outside = menu, 0
        with win_band_hook.band_windows():
            menu.popup(pos)
        # Qt rewrites the extended style when it shows the window and drops the non-activating bit, which is put back here, before
        # anything can be pressed: a press on a window that can be activated takes the game out of exclusive fullscreen.
        win_band_hook.ensure_noactivate(int(menu.winId()), layered=True)
        # The overlay raises itself every moment (_ensure_topmost) and the menu opens inside the overlay's own rectangle: unless the
        # menu is known to be open (popup_is_open), the overlay climbs back over it and it is gone before anything can be chosen.
        from ui.helpers import note_tool_menu
        note_tool_menu(menu)
        self._tool_menu_timer.start()
        QApplication.instance().installEventFilter(self)

    def _tool_menu_closed(self, menu, on_closed) -> None:
        logger.debug("Overlay: the menu closed - %s",
                     getattr(self, "_tool_menu_why", "") or "nothing of ours closed it (Qt or the system hid it)")
        self._tool_menu_why = ""
        if self._tool_menu is menu:
            self._tool_menu = None
            self._tool_menu_timer.stop()
            try:
                QApplication.instance().removeEventFilter(self)
            except Exception:
                pass
        if on_closed:
            on_closed()

    def _close_tool_menu(self, why: str = "the overlay went") -> None:
        menu, self._tool_menu = self._tool_menu, None
        self._tool_menu_timer.stop()
        if menu is not None:
            self._tool_menu_why = why
            try:
                menu.hide()
                menu.deleteLater()
            except RuntimeError:
                pass

    def _tool_menu_tick(self) -> None:
        """The pointer has been away from both the menu and the overlay for a while (a click in the game cannot be seen
        from here): the menu goes."""
        menu = self._tool_menu
        if menu is None or not menu.isVisible():
            self._tool_menu_timer.stop()
            return
        pos = QCursor.pos()
        if menu.geometry().contains(pos) or self.geometry().contains(pos):
            self._tool_menu_outside = 0
            return
        self._tool_menu_outside += 1
        if self._tool_menu_outside >= 6:
            self._tool_menu_why = "the pointer was away from it and from the overlay for a while"
            menu.hide()

    def eventFilter(self, obj, event):
        menu = self._tool_menu
        if (menu is not None and event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget)
                and obj.window() is not menu and obj.window() is not menu.window()):
            self._tool_menu_why = f"something else of ours was pressed ({type(obj).__name__})"
            menu.hide()                              # something else of ours was pressed
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        self.refresh_unknown_badge()
        self._install_dpi_change_watch()
        if self._rw_exclusive:
            # Whichever way the window came back (the capture selector was cancelled and the panel is shown again, ...), what it
            # opens is made for the game again. (Starting it twice is harmless.)
            self._begin_exclusive_session()
        super().showEvent(event)

    def _install_dpi_change_watch(self):
        """Wire this window's OWN screen/DPI signals.

        MainWindow has the equivalent (_install_dpi_change_watch there) but
        it only reacts to ITS OWN screenChanged/DPI signals — and the
        overlay is a separate top-level window that follows the ACTIVE
        screen (see _position_top_right / _get_active_screen_geometry),
        which is very often the game's monitor, not wherever the main
        window happens to sit. Moving between two screens of different DPI
        (or scale) with the main window untouched fires nothing there,
        leaving this window's fixed-size chrome — built with scaled() calls
        for whatever screen it was FIRST shown on — stuck at the wrong size
        even though _position_top_right correctly follows it to the new
        screen. That is the "shrunk overlay after a monitor switch" report.

        windowHandle() only exists once the native window is created, so
        this runs from showEvent (idempotent — connects once per native
        window) rather than __init__.
        """
        wh = self.windowHandle()
        if wh is None:
            return
        # Keyed on the window itself, not on a flag: the native window is dropped and made again (the higher band over a
        # game in exclusive fullscreen, the ordinary one afterwards, the X11 bypass flag), and the signals of the old
        # QWindow die with it.
        if getattr(self, "_dpi_watch_handle", None) is wh:
            return
        self._dpi_watch_handle = wh
        try:
            wh.screenChanged.connect(self._on_overlay_screen_changed)
        except Exception:
            pass
        try:
            if hasattr(wh, "devicePixelRatioChanged"):
                wh.devicePixelRatioChanged.connect(self._on_overlay_screen_changed)
        except Exception:
            pass

    def _on_overlay_screen_changed(self, *_args):
        """This window's own screen / DPI changed: re-scale for the new one."""
        logger.info("Overlay: own screen/DPI change detected — re-scaling")
        self._resync_visible(force=True)

    # ── Scale follows the monitor the overlay appears on ─────────────────────
    #
    # The overlay is shown where the player's attention is — the screen under
    # the cursor — which is often not the monitor it was last on, and a hidden
    # window stays bound to that last monitor. Everything measured while it is
    # hidden (the font sizes written into labels, the fixed widths) used to be
    # measured for the WRONG one, and nothing re-did it once the window had
    # moved: the box followed the new monitor's scale and the text inside kept
    # the old one. With a monitor turned off and back on that is exactly the
    # sequence — the overlay sits bound to the stand-in monitor, then appears
    # on the real one.
    #
    # So the scale is read from the screen the overlay is ABOUT to be on
    # (scale_screen, consulted by ui.helpers for this window and all its
    # children), and _sync_scale_to_screen brings the fixed sizes and the
    # stylesheet in line with it before anything is rendered.

    def _get_active_screen(self):
        """The screen the overlay is placed on: the one under the cursor."""
        screen = QApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen

    def scale_screen(self):
        """The screen this window's sizes are measured on (see
        ui.helpers._scale_screen_of): where it IS while it is on screen, where
        it is about to appear while it is not."""
        if self.isVisible():
            try:
                screen = QApplication.screenAt(self.frameGeometry().center())
            except Exception:
                screen = None
            if screen is not None:
                return screen
        return self._get_active_screen()

    def _scale_dpr(self) -> float:
        """Device pixel ratio of scale_screen(), for pixmaps."""
        try:
            return float(self.scale_screen().devicePixelRatio() or 1.0)
        except Exception:
            return 1.0

    def _apply_own_stylesheet(self, force: bool = False) -> None:
        """Give this window its own stylesheet when its monitor's scale is not
        the one the application stylesheet was built for.

        The application sheet is scaled once, for the primary screen. A widget
        sheet outranks it, so a copy built for THIS monitor's scale makes the
        text and padding agree with the box. When the two scales agree (the
        usual case: one monitor, or monitors alike) the window has no sheet of
        its own and inherits the application's exactly as before.
        """
        from ui.helpers import ui_scale
        from ui.styles.theme import get_theme_manager
        tm = get_theme_manager()
        cur = ui_scale(self)
        applied = tm.applied_scale
        key = (None if applied is None or abs(cur - applied) < 0.02
               else (tm.current, round(cur, 2)))
        if key == self._own_sheet_key and not force:
            return
        self._own_sheet_key = key
        try:
            self.setStyleSheet("" if key is None else tm.qss_for_scale(cur))
        except Exception:
            logger.debug("Overlay: could not apply its own stylesheet", exc_info=True)

    def _sync_scale_to_screen(self, why: str = "", force: bool = False) -> bool:
        """Bring the fixed sizes and the stylesheet in line with the screen the
        overlay is on or about to be on. True when the scale had to change.

        Cheap when nothing changed (one scale lookup and a compare), so it runs
        at the start of every render. Only this window's own registered
        dimensions are recomputed — never the whole app's.
        """
        from ui.helpers import ui_scale, _recalculate_all_scaled_dimensions
        cur = ui_scale(self)
        prev = self._applied_scale
        changed = force or prev is None or abs(cur - prev) >= 0.02
        try:
            name = self.scale_screen().name()
        except Exception:
            name = "?"
        seen = (name, round(cur, 2))
        if seen != self._last_scale_log:
            # Only when the screen or the scale moved: one line per switch is
            # what makes "which monitor was it sized for" answerable from the
            # log, and one per notification would bury it.
            logger.info("Overlay scale: screen=%s scale=%.2f%s (%s)", name, cur,
                        f" (was {prev:.2f})" if prev is not None and changed else "",
                        why or "render")
            self._last_scale_log = seen
        if changed:
            self._applied_scale = cur
            if prev:
                self._rescale_queued_text(cur / prev)
            try:
                _recalculate_all_scaled_dimensions(only_within=self)
            except Exception:
                logger.debug("Overlay: scaled-dimension recalculation failed", exc_info=True)
            self.refresh_styles()          # also re-applies the own stylesheet
        else:
            self._apply_own_stylesheet()
        return changed

    def _rescale_queued_text(self, factor: float) -> None:
        """The text of a queued notification has its font sizes written into it (as HTML) when it is queued, for the scale of
        that moment, and drawing it again after a scale change reads that very text: the card came out with the new frame
        and the old text. The sizes are brought to the new scale here, before anything is drawn again."""
        if abs(factor - 1.0) < 0.01 or not self._notif_queue:
            return
        from ui.helpers import scale_stylesheet_fonts
        for i, item in enumerate(self._notif_queue):
            try:
                self._notif_queue[i] = item[:2] + (scale_stylesheet_fonts(item[2], factor),) + item[3:]
            except Exception:
                logger.debug("Overlay: could not rescale a queued notification", exc_info=True)

    def _rerender_current(self) -> None:
        """Draw what is on screen again at the current scale.

        Text is written with the scale of the moment it was rendered, so a
        scale change while a notification is up leaves it stale until
        re-rendered. Notifications and the unknown-game card know how to redraw
        themselves; the rest are rebuilt by their next show.
        """
        try:
            if self._overlay_mode == "notif" and self._notif_queue:
                self._render_current_notification(in_place=True)
            elif self._overlay_mode == "unknown" and self._unknown_queue is not None:
                self._render_unknown_entry()
                self._rerender_in_place()
            elif self._overlay_mode == "tracking" and getattr(self, "_tracking_args", None):
                self._build_tracking_card(*self._tracking_args)
                self._rerender_in_place()
            else:
                self.adjustSize()
        except RuntimeError:
            pass

    def _resync_visible(self, force: bool = False) -> None:
        """The monitors changed under a visible overlay (or it moved to another
        one): re-scale it, redraw it, put it back in the corner."""
        if self._resyncing or getattr(self, "_cleaned_up", False) or not self.isVisible():
            return
        self._resyncing = True
        try:
            if self._sync_scale_to_screen("screen change", force=force):
                self._rerender_current()
            self._position_top_right()
        finally:
            self._resyncing = False

    def on_app_scale_changed(self) -> None:
        """The application stylesheet was rebuilt (MainWindow's scale re-apply):
        the overlay's own sheet and fixed sizes are relative to it."""
        self._sync_scale_to_screen("application scale changed", force=True)  # i18n-ignore (log text)
        if self.isVisible():
            self._rerender_current()
            self._position_top_right()

    def _on_action(self, action: str):
        self._last_user_action_at = time.monotonic()       # (the player pressed a button of the overlay: see _dashboard_in_use)
        self.action_requested.emit(action, self._context_exe)
        # Always hide overlay after any action, including open_app
        self.hide_animated()

    def _on_trash_unknown(self):
        """The bin on the unknown-game queue: take THIS game out of the list, without a ban. The main
        window removes it from the shared queue (nothing is added to the suppress list); the queue view
        then shows the next game, or closes when the queue is empty (_on_unknown_queue_changed)."""
        exe = self._context_exe
        if exe:
            self.unknown_trashed.emit(exe)

    def _on_suppress(self):
        exe = self._context_exe
        in_unknown_queue = self._unknown_queue is not None
        # Direct connection: by the time emit returns, the main window has
        # already suppressed the app AND pruned it from the pending queue.
        self.dont_show_again.emit(exe)
        if in_unknown_queue:
            # Stay ON the queue: the main window has removed the entry from the
            # shared queue, and _on_unknown_queue_changed has already re-read
            # it — the next entry slides into this slot (no jump back to the
            # first one), in-place, so the auto-hide countdown and the
            # hover-pause state are NOT reset — or the card closed itself
            # when nothing was left.
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
        if self._presenting() and not self._rw_exclusive:
            self._toggle_inline(self._fill_inline_pin_menu)       # picture fallback: a popup would take the game to the desktop
            return
        self._auto_hide_timer.stop()
        menu = self._build_pin_menu()

        def _again():
            # Reading a menu is not idling: the countdown starts again whole.
            if self._auto_hide_ms > 0:
                self._auto_hide_timer.start(self._auto_hide_ms)

        self._popup_menu(menu, self._pin_btn.mapToGlobal(QPoint(0, self._pin_btn.height())), on_closed=_again)

    # ── Menus inside the card (the picture over an exclusive-fullscreen game) ─────────────────────

    def _clear_inline(self) -> None:
        area = self._inline_area
        while area.count():
            item = area.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

    def _inline_btn(self, text: str, fn) -> None:
        """One entry of a menu shown inside the card: choosing it closes the list, does its thing on the next
        turn, and draws the card again."""
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _go():
            self._clear_inline()
            QTimer.singleShot(0, fn)
            QTimer.singleShot(90, self._refresh_presenter)
            if self._auto_hide_ms > 0:
                self._auto_hide_timer.start(self._auto_hide_ms)

        btn.clicked.connect(_go)
        self._inline_area.addWidget(btn)

    def _toggle_inline(self, fill) -> None:
        """Open the list *fill* builds, or close it when it is already open. Opening it gives the card a fresh
        countdown (hovering holds it, as everywhere on the card); it is not stopped, so a list nobody answers
        does not keep the card over the game for good."""
        if self._inline_area.count():
            self._clear_inline()
        else:
            fill()
        if self._auto_hide_ms > 0:
            self._auto_hide_timer.start(self._auto_hide_ms)
        self._refresh_presenter()

    def _fill_inline_pin_menu(self) -> None:
        """The 📌 menu as a list in the card: what was pinned recently (a pin marks what is on screen), a new
        note, close all. "Pin a file" and "capture a region" open real windows - a file picker, a screen-wide
        selector - which would take the game to the desktop, so they are left out here."""
        from ui.widgets.pins import get_pin_manager
        mgr = get_pin_manager()
        for path in mgr.recent():
            label, _tip, _unsaved = mgr.menu_entry(path)
            self._inline_btn(f"\U0001f4cc {label}" if mgr.is_open(path) else label, lambda p=path: mgr.toggle(p))
        self._inline_btn(t("pin.new_note"), mgr.new_note)
        if mgr.open_paths() or mgr.unsaved_pins():
            self._inline_btn(t("pin.close_all"), mgr.close_all)

    def _pin_capture(self) -> None:
        """Grab a piece of the screen and pin it.

        SaveSync's own overlay goes first: the shot is taken of what the
        player is looking at, and the panel they opened this from is not part
        of that.
        """
        from ui.widgets.pins import get_pin_manager
        from ui.widgets.screen_capture import capture_region

        was_visible = self.isVisible()
        exclusive = bool(self._rw_exclusive)
        # The panel is down while the selector is up, and its countdown must not run out meanwhile: it would hide the panel for
        # good and bring it back, when the selector is cancelled, as a window at opacity 0.
        self._auto_hide_timer.stop()
        screen = None
        if exclusive:
            # Over a game in exclusive fullscreen the picker is a window of the higher band on the game's monitor; the session
            # (ui.exclusive_session) is held for the capture from here, before the overlay goes: the overlay is what holds it
            # now, and the sweep at the end of a session would take the picker with it.
            from ui import exclusive_app
            from ui.exclusive_session import get_session
            self._capturing = True
            get_session().begin("capture", lambda: self._capturing)
            screen = exclusive_app.screen_of_game(self.presented_game())
        self.hide()
        QApplication.processEvents()
        piece = None
        blank = False
        try:
            from ui.widgets import screen_capture
            piece = capture_region(None, screen=screen, exclusive=exclusive)
            blank = piece is None and screen_capture.last_grab_blank
        finally:
            if exclusive:
                self._capturing = False
            # Only when nothing was captured — cancelled, or a display mode we
            # cannot read. On success the player got what they opened this for,
            # and bringing the panel back would just cover the new pin.
            if piece is None and was_visible and not self.isVisible():
                if blank:
                    self.show_notice(t("pin.capture_blank"))      # said instead of the panel: the frame could not be read
                else:
                    self.setWindowOpacity(1.0)
                    self.show()
                    if self._auto_hide_ms > 0:
                        self._auto_hide_timer.start(self._auto_hide_ms)
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

        # (Over a game in exclusive fullscreen the picker has no parent: the overlay's own window is the one that is dropped when
        # it hides, and a dialog made for the game must not depend on it.)
        path = pick_file(None if self._rw_exclusive else self, t("pin.add_title"), pin_name_filter())
        if path:
            get_pin_manager().pin(path)

    def _hide_suppress_btn(self) -> None:
        self._suppress_btn.setVisible(False)
        self._suppress_btn.setMaximumHeight(0)

    def _hide_dashboard(self) -> None:
        self._dashboard.setVisible(False)
        self._dashboard.setMaximumHeight(0)

    def _begin_cloud_prompt(self, game_name: str, exe_path: str, hint_key: str,
                            hint_args: "dict | None" = None) -> None:
        """Shared header for the cloud-save decision prompts: context, cloud
        icon, game name + overlay.*hint_key* message (formatted with
        *hint_args* when the message carries placeholders — escaped by the
        caller), clean button areas."""
        self._set_mode("cloud")
        self._context_exe = exe_path
        self._priority_context = exe_path
        self._icon_label.setText("☁")
        self._title.setText(t("app.name"))
        self._message.setText(
            f"<b>{game_name}</b><br>"
            f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
            f"{t(f'overlay.{hint_key}', **(hint_args or {}))}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()

    def _show_priority_prompt(self, auto_hide_ms: int = 0) -> None:
        """Show a decision-required prompt: holds priority (later
        notifications defer to it). Never auto-hides by default; a caller
        with a safe, correctable default for "nobody answered" (see
        show_path_changed) can pass a real timeout instead — the caller is
        responsible for setting self._on_expire_action beforehand if it
        wants to react to that expiry."""
        self._position_top_right()
        self._priority_active = True
        self.show_animated(auto_hide=auto_hide_ms)

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
        of the old dedicated 'detected games (not in library)' panel.

        The notification queue is COVERED, not cleared: it stays in place,
        and a notification that arrives while the unknown view is active
        (e.g. the aggregated Backup/Sync Tutti toast) drops the unknown
        view and falls back to the queue, so no pending toast is lost.
        hide_animated() still clears everything on manual dismiss.
        """
        from ui.unknown_history import feature_enabled
        if not feature_enabled():
            self.refresh_unknown_badge()
            return
        if self._defer_if_priority(self.show_unknown_queue):
            return
        entries = self._pending_unknown_entries()
        if not entries:
            self.refresh_unknown_badge()
            return
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
        # The program's own icon — it is the only thing that tells two
        # unfamiliar names apart — with the controller when it has none.
        from ui.exe_icon import show_exe_icon
        show_exe_icon(self._icon_label, self._context_exe, scaled(24, self), "🎮",
                      self._scale_dpr())
        self._title.setText(t("app.name"))
        # The install folder's name, beside the detected one (a program a list of known names has not met reads as a game of its own name)
        from ui.unknown_history import folder_reference
        _ref = folder_reference(h)
        _ref_txt = f" · {t('unknown_history.folder_ref', folder=html.escape(_ref))}" if _ref else ""
        self._message.setText(
            f"<b>{game_name}</b> {t('overlay.detected_msg')}<br>"
            f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>{t('overlay.save_detected')}{_ref_txt}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        self._add_btn(self._btn_area, t("overlay.add_to_library"), "add_game", primary=True)
        # The bin (header, beside the close button): this game leaves the queue WITHOUT being banned. Not
        # "don't show again", which is the link below. A hover that can be seen: the stock one is a shade
        # off the card's own colour and an emoji ignores a change of text colour.
        self._trash_btn.setToolTip(t("unknown_history.trash_tooltip"))
        self._trash_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;border-radius:4px;padding:0;"
            f"font-size:{scaled(13, self)}px;}}"
            f"QPushButton:hover{{background:{palette('error')};}}")
        self._trash_btn.setVisible(True)
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
            f"<b>{html.escape(game_name)}</b>{_engine_badge_html(eng, self)}"
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
        self._build_tracking_card(game_name, exe_path, engine)
        self._position_top_right()
        self.show_animated(auto_hide=_AUTO_HIDE_MS)

    def _build_tracking_card(self, game_name: str, exe_path: str = "", engine: str = "") -> None:
        """The content of the "now tracking" card, measured for the screen it is on. Kept apart from showing it so it can be
        drawn again when the scale changes under it (a display mode switch, another monitor): its text carries font sizes
        written at the scale of the moment."""
        self._tracking_args = (game_name, exe_path, engine)
        self._set_mode("tracking")
        self._context_exe = exe_path
        # The game's own icon, as on the detection card; the controller when
        # its program has none (or there is no program to look at).
        from ui.exe_icon import show_exe_icon
        show_exe_icon(self._icon_label, exe_path, scaled(24, self), "🎮",
                      self._scale_dpr())
        self._title.setText(t("app.name"))
        eng = (engine or "").strip() or _engine_label_for_exe(exe_path)
        # Game name + engine on one line; status on the line below.
        game_html = (
            f"<b>{html.escape(game_name)}</b>{_engine_badge_html(eng, self)}"
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
            f"<span style='color:{palette('text_muted')};font-size:{scaled(10, self)}px;'>"
            f"{t('overlay.open_with_hotkey', hotkey=hotkey)}</span>"
        )
        hotkey_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_area.addWidget(hotkey_lbl)

        self._hide_suppress_btn()

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

    def show_cloud_saves(self, game_name: str, exe_path: str, compare: bool = False):
        """Cloud saves exist for a library game whose local copy isn't synced. *compare*: both versions exist and differ, so the comparison
        window (both dated, and every way to settle it) is offered too."""
        if self._defer_if_priority(lambda: self.show_cloud_saves(game_name, exe_path, compare),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_available")
        # Same shape as the other cloud prompts: a primary download plus a
        # dropdown that says what the alternative actually DOES. A bare
        # "Dismiss" here suggested nothing — the player could not tell
        # whether closing it meant keeping local saves, postponing, or
        # losing something.
        # "Continue with local saves" here means: not THAT cloud copy - a different game with the same name, or a new playthrough - so the game
        # takes a cloud folder of its own (see main_window "continue_local_cloud_available"); the way to say "same game, local wins" is the comparison.
        _items = [(t("overlay.continue_local_fork"), "continue_local_cloud_available")]
        if compare:
            _items.insert(0, (t("overlay.compare_versions"), "resolve_conflict_details"))
        self._add_split_btn(
            self._btn_area, t("overlay.download_saves"), "download_saves",
            menu_items=_items,
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
            menu_items=[(t("overlay.continue_local_fork"), "continue_local_no_local")],
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("suppress_cloud_no_local"))
        self._show_priority_prompt()

    def show_cloud_saves_no_local_copies(self, game_name: str, exe_path: str, several: bool = True):
        """The no-saves-here prompt for a LIBRARY game when the data cannot say which cloud copy is its own: SEVERAL folders share its name
        (or a copy and an archive on this PC that are not the same data), or the one copy is not its folder - so there is no plain "download"
        (it would pick one for the player). Primary opens the picker (the one unknown games get); the dropdown keeps this PC's saves, as on
        the single-copy prompt (a cloud folder of its own where a same-name copy is already there)."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_no_local_copies(game_name, exe_path, several),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_conflict" if several else "cloud_no_local")
        self._add_split_btn(
            self._btn_area,
            t("overlay.verify_conflicts") if several else t("overlay.verify_details"),
            "verify_conflicts_known_game" if several else "verify_details_known_game",
            menu_items=[(t("overlay.continue_local_fork"), "continue_local_no_local")],
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("suppress_cloud_no_local"))
        self._show_priority_prompt()

    def show_remote_renamed(self, game_name: str, exe_path: str, new_name: str):
        """Another machine renamed this game's cloud folder, so this machine's
        old one is gone. Primary: take the new name here too; dropdown: keep
        both names (this machine carries on under its own); bottom link: same
        as keep both, and never ask again for this game."""
        if self._defer_if_priority(lambda: self.show_remote_renamed(game_name, exe_path, new_name),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_renamed",
                                 hint_args={"name": html.escape(new_name)})
        self._add_split_btn(
            self._btn_area, t("overlay.update_renamed"), "update_renamed_game",
            menu_items=[(t("sync.keep_both"), "keep_both_renamed_game")],
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("never_renamed_game"))
        self._show_priority_prompt()

    def show_cloud_saves_different_machine(self, game_name: str, exe_path: str, compare: bool = False):
        """Cloud saves were last uploaded by a DIFFERENT machine — local saves
        here may be older. Download & replace, or keep local. *compare*: there are
        local saves too, so the comparison window is offered to choose by the dates."""
        if self._defer_if_priority(lambda: self.show_cloud_saves_different_machine(game_name, exe_path, compare),
                                   context=exe_path, is_priority=True):
            return
        self._begin_cloud_prompt(game_name, exe_path, "cloud_different_machine")
        self._add_btn(self._btn_area, t("overlay.download_and_replace"), "download_saves_different_machine", primary=True)
        if compare:
            self._add_btn(self._btn_area, t("overlay.compare_versions"), "resolve_conflict_details")
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

    def show_path_changed(self, game_name: str, game_id: str, new_exe_path: str,
                          alternates: list | None = None, same_install: bool = False):
        """A stem/name match found *game_name* running from a path that
        differs from the one on record — its stored exe is presumably gone
        (a reinstall, an update, a moved folder). Tracking already proceeded
        on the stem match; this only asks how the new path should be filed.

        A decision-required prompt like the others: it does not expire (it
        used to apply the primary action by itself after a few seconds);
        it stays up until it is answered, and the hotkey calls it up
        again. Tracking has already proceeded on the stem match, so
        nothing is withheld while it waits - only the filing of the new
        path waits for the answer.

        *alternates* are other library entries the match was ambiguous
        against — (game_id, game_name) pairs, offered in the dropdown as
        "it's actually …" corrections alongside Overwrite/Different game,
        for the not-unusual case of more than one entry sharing a common
        name. Also decides what an unattended expiry is allowed to do:
        actually copying the old save DATA into the new location waits
        for the match to be unambiguous first (no alternates, or an
        explicit choice) — seeing alternates, expiry moves only the
        tracking pointer and leaves the physical data copy for later.

        *same_install*: the program is NOT at a different location - it is another file of the install the game already has (a 64-bit build beside the 32-bit
        one), and the wording says so. It was recognised as the game by its NAME; a program found only by where it sits is an ordinary detection (the queue)."""
        context = f"{game_id}|{new_exe_path}"
        if self._defer_if_priority(
                lambda: self.show_path_changed(game_name, game_id, new_exe_path, alternates, same_install),
                context=context, is_priority=True):
            return
        self._set_mode("cloud")          # same decision-prompt chrome
        self._context_exe = context
        self._priority_context = context
        self._icon_label.setText("📁")
        self._title.setText(t("app.name"))
        _kind = "path_sibling" if same_install else "path_changed"
        self._message.setText(
            f"<b>{t(f'overlay.{_kind}_msg', game=game_name)}</b><br>"
            f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
            f"{t(f'overlay.{_kind}_hint')}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        menu_items = [
            (t("overlay.path_overwrite"), "path_overwrite"),
            (t("overlay.path_new_game"),  "path_new_game"),
        ]
        for alt_id, alt_name in (alternates or []):
            menu_items.append(
                (t("overlay.path_reassign", game=alt_name), f"path_reassign:{alt_id}"))
        self._add_split_btn(
            self._btn_area, t("overlay.path_add_same"), "path_add_version",
            menu_items=menu_items,
            primary=True,
        )
        self._set_suppress_link(
            "dont_show_again", lambda: self._on_action("path_add_version"))
        self._show_priority_prompt()

    def show_overwrite_saves_conflict(self, game_name: str, game_id: str, new_exe_path: str,
                                      is_data_copy: bool = False):
        """Overwrite was chosen, but the path it would rebase saves onto
        already has its OWN content — a second, independent save history,
        not an empty reinstall target. Ask before relocating tracking onto
        it. Unanswered defaults to NOT rebasing (the old location stays
        tracked as it was) — the safe outcome, never the one that could
        silently start treating two different saves as the same.

        *is_data_copy* tells apart the two calling situations, which do
        DIFFERENT things on "Overwrite" despite sharing this one prompt
        (see _resolve_overwrite_saves_conflict in main_window.py for how
        the answer itself is told apart): False (_apply_path_overwrite)
        moves which location is TRACKED, no bytes touched — the default
        hint describes exactly that, "track the new one from now on".
        True (_carry_rebased_save_data) is asked AFTER tracking already
        moved — "Overwrite" here means shutil.copytree the OLD save data
        into the new location, merging it with what's already there, a
        real file copy the tracking-pointer wording never mentions and
        would otherwise leave the player agreeing to blind."""
        context = f"{game_id}|{new_exe_path}"
        if self._defer_if_priority(
                lambda: self.show_overwrite_saves_conflict(
                    game_name, game_id, new_exe_path, is_data_copy),
                context=context, is_priority=True):
            return
        self._set_mode("cloud")
        self._context_exe = context
        self._priority_context = context
        self._icon_label.setText("⚠")
        self._title.setText(t("app.name"))
        _hint_key = "overlay.overwrite_conflict_hint_copy" if is_data_copy \
            else "overlay.overwrite_conflict_hint"
        self._message.setText(
            f"<b>{t('overlay.overwrite_conflict_msg', game=game_name)}</b><br>"
            f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
            f"{t(_hint_key)}</span>"
        )
        self._hide_dashboard()
        self._clear_buttons()
        self._add_btn(self._btn_area, t("sync.keep_both"), "path_overwrite_keep_both",
                      primary=True)
        self._add_btn(self._btn_area, t("overlay.path_overwrite"), "path_overwrite_confirm")
        self._hide_suppress_btn()
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
            f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
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
                           newest_backup_id: str = "", after_restore: bool = False,
                           unbacked: bool = False):
        """The current save state needs the player's attention: either it
        went BACK to a state already recorded (a regression), or it is
        content this game's history has never seen at all (unbacked).

        Both readings come from one comparison (see
        core.backup.BackupManager.detect_regression) and share this same
        prompt shape rather than two separate ones — decision chrome, an
        icon, title/message/hint, a primary action, a dropdown alternative,
        a mute link — with only the wording and the primary action differing:

        - regression at launch: state the fact, offer to put the newest
          state back (or to back the current one up instead);
        - regression right after a restore: name the likely cause (a
          launcher's automatic sync racing the restore) and offer to force
          it with the game frozen, which is the thing that actually wins
          that race;
        - unbacked: state that nothing on record matches this. The current
          state is unverified — it could be ordinary offline play, or it
          could be something else entirely — so the primary action is the
          cautious one, restoring the previous state (the newest TRUSTED
          backup when one exists, else simply the newest on record — see
          core.backup.BackupManager.newest_restore_target), same as a
          regression. Backing up this unverified state instead (enshrining
          it as a new backup) is one dropdown click away, not the default.
        """
        context = f"{game_id}|unbacked" if unbacked else f"{game_id}|reverted"
        if self._defer_if_priority(
                lambda: self.show_save_reverted(game_name, game_id,
                                                newest_backup_id, after_restore,
                                                unbacked),
                context=context, is_priority=True):
            return
        self._set_mode("cloud")          # decision-prompt chrome
        # Same "game_id|backup_id" shape either way (a bare id when there is
        # no backup to point at) — every button below, unbacked or
        # regression, reads it with the same partition("|").
        self._context_exe = f"{game_id}|{newest_backup_id}"
        self._priority_context = context
        if unbacked:
            self._icon_label.setText("💾")
            self._title.setText(t("overlay.save_unbacked_title"))
            self._message.setText(
                f"{t('overlay.save_unbacked_msg', game=game_name)}<br>"
                f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
                f"{t('overlay.save_unbacked_hint')}</span>"
            )
        else:
            self._icon_label.setText("↩")
            self._title.setText(t("overlay.save_reverted_title"))
            msg_key = "overlay.restore_undone_msg" if after_restore else "overlay.save_reverted_msg"
            hint_key = "overlay.restore_undone_hint" if after_restore else "overlay.save_reverted_hint"
            self._message.setText(
                f"{t(msg_key, game=game_name)}<br>"
                f"<span style='color:{palette('text_hint')};font-size:{scaled(11, self)}px;'>"
                f"{t(hint_key)}</span>"
            )
        self._hide_dashboard()
        self._clear_buttons()
        # Two answers, the same for a regression and for unbacked content:
        # put the last backup back (the cautious primary — the point is
        # avoiding changes nobody vouched for), or make what is on disk the
        # new backup (the dropdown, for when the player knows it is genuine —
        # played offline, or on another PC). There is deliberately no third
        # "dismiss": the ✕ closes the alert and the warning stays pending, so
        # the game keeps being backed up as temporary copies (see
        # _launch_notification_pending) that only one of the two answers
        # above ever settles. The hotkey brings the alert back.
        #
        # Not offered right after a restore: keeping the state the player just
        # restored away from would contradict what they just did — there the
        # one answer is forcing the restore, a plain button.
        backup_now = (t("overlay.save_unbacked_backup"), "backup_now_unbacked")
        if not newest_backup_id:
            # Defense-in-depth only: detect_regression needs a backup on
            # record to fire at all, so this game should always have one.
            self._add_btn(self._btn_area, backup_now[0], backup_now[1], primary=True)
        elif after_restore:
            self._add_btn(self._btn_area, t("overlay.restore_force"),
                          "force_restore", primary=True)
        else:
            self._add_split_btn(
                self._btn_area,
                t("overlay.save_unbacked_restore") if unbacked
                else t("overlay.save_reverted_restore"),
                "restore_newest",
                menu_items=[backup_now],
                primary=True)
        self._set_suppress_link(
            "dont_show_ingame",
            lambda _checked=False, gid=game_id:
                self._suppress_unbacked_notif(gid) if unbacked
                else self._suppress_regression_notif(gid))
        self._show_priority_prompt()

    def _suppress_unbacked_notif(self, game_id: str) -> None:
        """Mute the unbacked-save alert for this game — own handler for the
        same reason _suppress_regression_notif has one: a priority prompt,
        not something in _notif_queue, so the queue-popping shared handler
        would leave this alert on screen with nothing to release its lock.
        """
        from core.config_manager import get_config
        config = get_config()
        suppressed: dict = dict(config.get("suppressed_ingame_notifs", {}))
        kinds = list(suppressed.get(game_id, []))
        if "unbacked" not in kinds:
            kinds.append("unbacked")
        suppressed[game_id] = kinds
        config.set("suppressed_ingame_notifs", suppressed)
        logger.info(f"Muted unbacked-save alerts for game {game_id}")
        self.hide_animated()

    def dismiss_unverified_match(self, proc_name: str, game_id: str) -> None:
        """Withdraw an unanswered unverified-match prompt whose process has
        exited. Without this the prompt sits there asking about something that
        is gone AND keeps holding priority, deferring every later
        notification behind a question that can no longer be answered."""
        context = f"{proc_name}|{game_id}"
        if self._priority_context != context:
            return
        if self._presenting() or (self.isVisible() and self.windowOpacity() > 0.1):
            self.hide_animated()      # drops priority and replays deferred
            self._priority_context = ""
            return
        self.clear_priority_for(context)

    # ── Priority prompt gating ───────────────────────────────────────────────

    def _dashboard_in_use(self) -> bool:
        """The hotkey dashboard is on screen and the player is using it: the pointer is on it, or a menu of it (the 📌 one) is open."""
        try:
            now = time.monotonic()
            # What the player just ASKED for is not a notice that came by itself, and must never wait behind the dashboard it was asked from:
            # the unknown-game badge (note_user_summon) held back its own click - nothing seemed to happen - and the answer to a dashboard
            # button (a backup that finishes a few seconds after "Backup" was pressed) is the answer to a request.
            if now - getattr(self, "_user_summon_at", -10.0) < 1.5 or now - getattr(self, "_last_user_action_at", -100.0) < 30.0:
                return False
            if self._overlay_mode != "manual" or not self._on_screen():
                return False
            if not self._presenting() and self.windowOpacity() <= 0.3:
                return False                                   # (on its way out)
            hovered = self._xp_hover if self._presenting() else (self._hover_active or self.underMouse())
            return bool(hovered or popup_is_open())
        except RuntimeError:
            return False

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
        if self._priority_active and self._on_screen():
            if is_priority and context and context == self._priority_context:
                return False
        elif is_priority or not self._dashboard_in_use():
            return False
        # (else: a notice that came by itself, while the dashboard the player asked for is being used - the pointer is on it, or its pin
        # menu is open. It replaced the dashboard, and the menu with it: "the tracking notification closed my overlay". It is shown once
        # the dashboard has gone, like one that comes behind a prompt.)
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

    def clear_priority_for(self, exe_path: str, game_id: str = "") -> None:
        """Release a decision-required priority prompt when ITS subject process
        has exited, so later notifications (including hotkey-invoked ones) stop
        being deferred behind a prompt that can never be answered — the wedge
        that silently blocked all further detection. Replays anything queued
        behind it. A blank *exe_path* clears unconditionally.

        Several priority prompts (show_path_changed, show_overwrite_saves_
        conflict, show_save_reverted) key _priority_context on a composite
        "a|b" string rather than a bare exe path, so an exact match against
        *exe_path* alone would never release them from a plain exit handler
        that only knows the exe path (or, for an orphan process, has no
        game_id at all). Matching either value against any "|"-delimited
        component of the context — not just the whole string — covers both.
        """
        if not self._priority_active:
            return
        if not exe_path and not game_id:
            matched = True
        else:
            parts = self._priority_context.split("|") if self._priority_context else []
            matched = (
                exe_path == self._priority_context
                or game_id == self._priority_context
                or (exe_path and exe_path in parts)
                or (game_id and game_id in parts)
            )
        if matched:
            logger.debug(f"Releasing priority prompt — subject exited: "
                        f"exe_path={exe_path!r} game_id={game_id!r}")
            self._priority_active = False
            self._priority_context = ""
            self._flush_deferred_notifs()

    # ── Notification carousel ────────────────────────────────────────────────

    def _push_notification(self, icon: str, title: str, message: str,
                           auto_hide_ms: int = _AUTO_HIDE_MS,
                           game_id: str = "", notif_type: str = "",
                           action: "tuple | None" = None) -> None:
        """Add a notification to the queue and display it.

        game_id + notif_type: when both set, a "don't show again" link is
        offered for that specific game × notification type combination.
        If this game+type pair is already suppressed in config, skip silently.
        (The "game" can be anything with a stable id: a friend is
        "friend:<id>", and shows in Settings' list of what was silenced like
        any game does.)

        *action*: (button label, action name, context) adds a primary button
        that raises ``action_requested(name, context)``.
        """
        if self._defer_if_priority(lambda: self._push_notification(
                icon, title, message, auto_hide_ms, game_id, notif_type, action)):
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
        self._notif_meta.append((game_id, notif_type, action))
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
        meta = self._notif_meta[idx] if idx < len(self._notif_meta) else ("", "", None)
        game_id, notif_type = meta[0], meta[1]
        action = meta[2] if len(meta) > 2 else None

        self._icon_label.setText(icon)
        self._title.setText(title)
        self._message.setText(message)
        self._hide_dashboard()
        self._clear_buttons()
        if action:
            label, act, ctx = action
            self._context_exe = ctx
            self._add_btn(self._btn_area, label, act, primary=True)

        # Per-game suppress link when this notif carries an identity
        if game_id and notif_type:
            self._set_suppress_link(
                "dont_show_friend" if game_id.startswith("friend:")
                else "dont_show_group" if game_id.startswith("group:") else "dont_show_ingame",
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
        elif in_place and self._presenting():
            self._refresh_presenter()
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
        if game_id.startswith("friend:"):
            self.friend_silenced.emit(game_id[len("friend:"):])
            logger.info("Silenced a friend's message notifications")
        elif game_id.startswith("group:"):
            self.group_silenced.emit(game_id[len("group:"):])
            logger.info("Silenced a group's message notifications")
        else:
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

    def _clear_notification_queue(self, hide_carousel: bool = True) -> None:
        """Reset the queue. Called when user manually dismisses or a non-queued
        show method is called (e.g. game detected, save done in-game).

        *hide_carousel* exists for the one caller that is on its way out.
        Hiding a child widget is instant; fading the window is not. Doing
        both from hide_animated made the arrows and the counter blink out
        of a panel that was still fully opaque, and only then did the rest
        fade — the queue appearing to close before the overlay it lives in.
        The data is dropped either way; only the widgets wait.
        """
        self._notif_queue.clear()
        self._notif_meta.clear()
        self._notif_index = 0
        self._unknown_queue = None
        if hide_carousel:
            self._hide_carousel_controls()

    def _hide_carousel_controls(self) -> None:
        """Take down the browse arrows and the counter."""
        for widget in (self._carousel_prev, self._carousel_next,
                       self._carousel_counter):
            try:
                widget.setVisible(False)
            except RuntimeError:
                pass

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

    def show_batch_done(self, kind: str, done: int, name: str = ""):
        """Queue ONE aggregated notification at the end of Backup Tutti /
        Sync Tutti. Per-game notifications stay suppressed during a batch
        (spam froze the UI), but the summary is still delivered through
        the same already-built queue — a plain append, no rebuild.
        kind: "backup" or "sync". A single finished title reports its name,
        matching the sidebar notice.
        """
        if kind == "backup":
            icon = "✓"
            key = "batch.backup_done_one" if (done == 1 and name) else "batch.backup_done"
            text = t(key, name=name) if done == 1 and name else t(key, done=done)
        else:
            icon = "☁"
            key = "batch.sync_done_one" if (done == 1 and name) else "batch.sync_done"
            text = t(key, name=name) if done == 1 and name else t(key, done=done)
        msg = (
            f"<span style='color:{palette('accent')};font-weight:700;'>"
            f"{text}</span>"
        )
        self._push_notification(icon, t("app.name"), msg, _AUTO_HIDE_MS)

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

    def show_friend_message(self, friend_id: str, name: str, preview: str) -> None:
        """A friend's message arrived (see ui.p2p_friends_service): who it is
        from and a line of it, a primary button that opens it, and a "don't
        show again" that silences THIS friend only (it is listed in Settings,
        where it can be taken off). Messages from several friends stack in the
        carousel like any other notification."""
        msg = (f"<b>{html.escape(name)}</b> — "
               f"<span style='color:{palette('text_secondary')};'>{html.escape(preview)}</span>")
        self._push_notification(
            "👥", t("p2p.friends_notice_title"), msg, _FRIEND_AUTO_HIDE_MS,
            game_id=f"friend:{friend_id}", notif_type="friend_message",
            action=(t("p2p.friends_notice_open"), "open_friend_messages", friend_id))

    def show_p2p_offer(self, text: str) -> None:
        """Somebody used the person's token: files (or a save) are waiting for a yes or a no.
        A button opens the page; ignoring it leaves the page in the sidebar, where it can be
        opened from at any time. Nothing is put in front of what the person is doing."""
        self._push_notification(
            "📥", t("p2p.receive_notice_title"), text, _FRIEND_AUTO_HIDE_MS,
            action=(t("p2p.receive_notice_open"), "open_p2p_receive", ""))

    def show_group_message(self, group_id: str, group: str, sender: str, preview: str) -> None:
        """A message (or an attachment offer) arrived in a group: which group, who said it and a line
        of it, a button that opens the group, and a "don't show again" that silences THIS group."""
        msg = (f"<b>{html.escape(group)}</b> · {html.escape(sender)} — "
               f"<span style='color:{palette('text_secondary')};'>{html.escape(preview)}</span>")
        self._push_notification(
            "👥", t("p2p.groups_notice_title"), msg, _FRIEND_AUTO_HIDE_MS,
            game_id=f"group:{group_id}", notif_type="group_message",
            action=(t("p2p.groups_notice_open"), "open_group_messages", group_id))

    def show_group_invite(self, group_id: str, text: str) -> None:
        """Somebody asked the person into a group: the button opens the list, where the invitation waits."""
        msg = html.escape(text)
        self._push_notification(
            "👥", t("p2p.groups_notice_invite_title"), msg, _FRIEND_AUTO_HIDE_MS,
            action=(t("p2p.groups_notice_invite_open"), "open_group_messages", ""))

    def show_group_kicked(self, group_id: str, text: str) -> None:
        self._push_notification(
            "👥", t("p2p.groups_notice_title"), html.escape(text), _FRIEND_AUTO_HIDE_MS,
            action=(t("p2p.groups_notice_open"), "open_group_messages", group_id))

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

    def show_notice(self, text: str, icon: str = "ℹ"):
        """Show a generic one-line informational toast (no game association).

        Used for cooldown messages, sync-no-changes, and other ephemeral
        status notifications that don't belong to a specific backup event.
        """
        msg = (
            f"<span style='color:{palette('text_secondary')};'>"
            f"{text}</span>"
        )
        self._push_notification(icon, t("app.name"), msg, _AUTO_HIDE_MS)

    def show_manual(self, stats: dict | None = None):
        """Toggle dashboard. If visible → hide; else → show with live stats."""
        # Over a game in exclusive fullscreen the dashboard is a picture (see
        # _present_over_exclusive): the second press closes that one.
        if self._xp is not None and self._xp.visible:
            self._end_presenter()
            return
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
                    f"{_engine_badge_html(eng, self)}"
                )
                self._message.setText(t("overlay.game_launched", game=game_html))
                from ui.exe_icon import show_exe_icon
                show_exe_icon(self._icon_label, stats.get("active_exe", ""),
                              scaled(24, self), "🎮", self._scale_dpr())
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
            start = float(stats.get("session_start") or 0)
            self._session_start = start if (stats.get("active_game") and start > 0) else 0.0
            self._session_row.setVisible(self._session_start > 0)
            if self._session_start > 0:
                self._set_dash(4, t('overlay.session'), format_clock(time.time() - self._session_start))
        else:
            self._message.setText(f"<span style='color:{palette('text_hint')};'>{t('app.tagline')}</span>")
            self._hide_dashboard()
            self._session_start = 0.0

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
                        f"QPushButton{{font-size:{scaled(11, self)}px;color:{palette('warning')};background:transparent;"
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
        # Hotkey dashboard only: notifications must not steal / redraw the pointer.
        self.show_animated(auto_hide=_AUTO_HIDE_MS, pointer=True)
        if self._session_start > 0:
            self._session_timer.start()
        else:
            self._session_timer.stop()

    # ── Animation ─────────────────────────────────────────────────────────────

    # The views a player has to click in: a question to answer ("cloud": a conflict, a restore, a verification ...), a game to add or
    # bin ("unknown"), the dashboard. They bring the pointer with them when a game has hidden its own: the question does not go
    # away by itself, and a question that cannot be answered is no question. Passive notices (a backup was made, a game is being
    # watched, a restore is done) leave the player's pointer alone.
    _POINTER_MODES = frozenset({"manual", "cloud", "unknown"})

    def note_user_summon(self) -> None:
        """The player asked for what is about to be shown (the hotkey, the badge): it gets the pointer. A notice that
        appears by itself leaves the player's mouse alone - as it does over a borderless game."""
        self._user_summon_at = time.monotonic()

    def _needs_pointer(self) -> bool:
        return (self._overlay_mode in self._POINTER_MODES
                or time.monotonic() - getattr(self, "_user_summon_at", -10.0) < 1.5)

    def _fullscreen_game_in_front(self) -> bool:
        """A game is being played: SaveSync is tracking one, or a fullscreen window is in front (a game that is
        not in the library yet is a game all the same, and the one the queue asks about)."""
        return self._game_is_running() or self._is_fullscreen_active()

    def show_animated(self, auto_hide: int = _AUTO_HIDE_MS, *,
                      pointer: bool = False, over_exclusive: bool = False):
        """Show the overlay above any window — safely handles fullscreen games.

        Fullscreen handling:
        - **Borderless fullscreen**: safe — show with TOPMOST + NOACTIVATE.
        - **Exclusive fullscreen** (DirectX/Vulkan): showing ANY window forces
          the DWM to composite, kicking the game out of exclusive mode (black
          flicker, fps drop, possible crash).  So the overlay window itself
          is never shown.  Instead what it holds is shown as a PICTURE in a
          layered, non-activating window that was measured not to take the game
          out of exclusive mode, and that takes the mouse so its buttons work
          (_present_over_exclusive).  When that cannot be done - the game is on
          the deny-list, the setting is off, the API is refused - the overlay is
          skipped and ``exclusive_blocked(title, message)`` is emitted so the
          caller can route it through another channel.  No defer — by the time
          exclusive mode ends the notification is stale.
        *pointer*: when True (hotkey / ``show_manual`` only) **and** a game is
        running, free clip/capture so the overlay is reachable. The software
        arrow appears only if the game has hidden the OS cursor — titles that
        already draw their own pointer are left alone. Outside a session, and
        for toasts/notifications, the player's cursor is untouched.
        """
        pointer = pointer or self._needs_pointer()
        self._user_summon_at = -10.0          # a request applies to the one view it produced
        # ── Exclusive fullscreen guard ──────────────────────────────────────
        self._rw_exclusive = False
        # (*over_exclusive*: the caller has just judged that the game in front is exclusive. Asked again here, with the foreground at
        # that moment being some window of ours, the answer can be "no" and an ordinary window is shown over an exclusive game: invisible,
        # and still taking the mouse.)
        exclusive_now = platform.system() == "Windows" and (over_exclusive or self._is_exclusive_fullscreen())
        if exclusive_now and self._real_window_ok():
            # The overlay's own window, made in the higher band: the mouse, the hover, the menus and the pointer are
            # the window's own. No picture.
            self._rw_exclusive = True
            logger.debug("Overlay shown over exclusive fullscreen as a window")
        elif exclusive_now:
            if self._present_over_exclusive(auto_hide, pointer):
                logger.debug("Overlay shown over exclusive fullscreen as a click-through picture")
                return
            title = self._title.text() if hasattr(self, '_title') else t("app.name")
            msg = self._message.text() if hasattr(self, '_message') else ""
            msg_plain = re.sub(r'<[^>]+>', '', msg).strip()
            # Nothing is on screen — a hidden overlay must never hold priority.
            self._priority_active = False
            self._flush_deferred_notifs()
            self.exclusive_blocked.emit(title, msg_plain)
            logger.info("Overlay skipped — exclusive fullscreen active")
            return

        # An ordinary screen again: a picture left over from the exclusive-fullscreen case has
        # no place next to the real overlay.
        self._hide_presenter()
        if self._rw_exclusive:
            self._start_real_window_watch()       # after the line above, which ends any watch
            self._begin_exclusive_session()
        if not self.isVisible():
            self._apply_fullscreen_stacking()

        # Views that are clicked in (dashboard, questions, the queue) while a game is being played. Desktop /
        # passive toasts: no second cursor.
        if pointer and self._fullscreen_game_in_front():
            SystemCursor.hold("overlay")
            self._cursor_timer.start()
        else:
            self._cursor_timer.stop()
            SystemCursor.release("overlay")

        # Cancel any pending hide first
        self._auto_hide_timer.stop()
        self._on_expire_action = None
        self._anim.stop()
        if self._hide_anim_connected:
            try:
                self._anim.finished.disconnect(self._do_hide)
            except RuntimeError:
                pass
            self._hide_anim_connected = False
        self._hover_active        = False
        self.setWindowOpacity(0.0)
        # Over a game a notice that appears by itself leaves the player's mouse alone - the cursor already did (no pointer, below),
        # but its rectangle still took every click and every hover: the player moves the mouse, or clicks in the game, and a notice
        # that is not even asked for gets it. Only a view that was asked for (the hotkey, the badge) takes the mouse.
        # (And only while the game has hidden its cursor: a game that shows its own has a player who can see the notice and the pointer
        # and reach it - see _click_through_now.)
        self._interactive_view = bool(pointer)

        # ── Capture current foreground ──────────────────────────────────────
        _saved_foreground = None
        if platform.system() == "Windows":
            try:
                _saved_foreground = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                pass

        # Normally a no-op: the render that led here already did this (see
        # _set_mode). It is the net for a path that did not, and sizes the
        # frame for the screen it is about to be moved to.
        self._sync_scale_to_screen("show")
        self._position_top_right()

        # ── Show + topmost ──────────────────────────────────────────────────
        self.show()
        self._take_the_front()
        self._ensure_visible_on_screen()
        self._make_clickable_off_windows()

        if pointer and self._game_is_running():
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

        self._auto_hide_ms = max(0, int(auto_hide))
        if auto_hide > 0:
            self._auto_hide_timer.start(auto_hide)
        self._apply_click_through()

    def _click_through_now(self) -> bool:
        """Does the mouse go through the overlay to the game right now? Only for a notice nobody asked for, as a window over a game,
        while the game has HIDDEN its cursor - the player has no pointer to aim with, and moving the mouse in the game must not
        land on a notice. A game that shows its own cursor is another matter: the player sees the notice and the pointer, and
        can reach it. A cursor held by us (a view that was asked for, a pin being dragged) is not the game's to judge."""
        if not self._rw_exclusive or getattr(self, "_interactive_view", True):
            return False
        try:
            return SystemCursor.game_hides_cursor()
        except Exception:
            return False

    def _apply_click_through(self) -> None:
        """WS_EX_TRANSPARENT on the overlay's window while it is a notice nobody asked for (see show_animated). Qt rewrites the
        extended style now and then, and the game may hide or show its cursor, so it is put right again whenever the overlay
        re-orders itself."""
        if platform.system() != "Windows" or not self.isVisible():
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            ex = user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
            want = (ex | 0x00000020) if self._click_through_now() else (ex & ~0x00000020)
            if want != ex:
                user32.SetWindowLongW(hwnd, -20, ctypes.c_long(want - (1 << 32) if want >= (1 << 31) else want))
        except Exception:
            logger.debug("Overlay: could not set whether the mouse goes through", exc_info=True)

    def hide_animated(self):
        self._hide_presenter()
        self._clear_inline()
        self._auto_hide_timer.stop()
        self._topmost_timer.stop()
        self._uninstall_foreground_hook()
        self._auto_hide_ms = 0
        self._set_mode("")
        # When the user explicitly closes the overlay, reset the notification
        # queue so stale notifications don't reappear on the next show_animated.
        # The arrows and the counter stay on screen until the fade is over —
        # see _clear_notification_queue.
        self._clear_notification_queue(hide_carousel=False)
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

    # ── A picture over a game in exclusive fullscreen ────────────────────────

    def _apply_fullscreen_stacking(self) -> None:
        """Plain X11 only (ui.x11_fullscreen has the measurements): a managed window is stacked UNDER a
        fullscreen one, so while a fullscreen window is in front the overlay is an override-redirect window,
        and a managed one again when it is not. Changing the flag recreates the native window, so it is done
        only when the answer changes - hidden first, and shown again if it was on screen."""
        try:
            from ui.x11_fullscreen import dynamic_bypass_applies, fullscreen_window_in_front
            if not dynamic_bypass_applies():
                return
            want = fullscreen_window_in_front()
        except Exception:
            return
        if want == self._x11_bypass:
            return
        self._x11_bypass = want
        was_visible = self.isVisible()
        self.hide()
        self.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, want)
        logger.info("Overlay: %s a fullscreen window in front - %s", "there is" if want else "there is no longer",
                    "now an override-redirect window" if want else "a managed window again")
        if was_visible:
            # Not in the same turn of the event loop: a window hidden, re-flagged and shown within one turn is
            # left unmapped by the xcb plugin (measured).
            QTimer.singleShot(40, self._reshow_after_stacking)

    def _reshow_after_stacking(self) -> None:
        if not getattr(self, "_cleaned_up", False) and self._overlay_mode:
            self.show()
            self._take_the_front()

    def _presenting(self) -> bool:
        """The picture over an exclusive-fullscreen game is up."""
        return self._xp is not None and self._xp.visible

    def _on_screen(self) -> bool:
        """The overlay is showing something: its own window, or the picture."""
        return self.isVisible() or self._presenting()

    def _present_over_exclusive(self, auto_hide: int, pointer: bool = False) -> bool:
        """Show what the overlay holds as a PICTURE over a game in exclusive fullscreen, and make it
        work like the window does. True when it is on screen; False leaves the caller to skip, as
        before this existed.

        Not a window the game would have to share the screen with: an ordinary window takes the
        game back to the desktop within half a second. A layered, non-activating window in the one
        higher band an ordinary process may use is shown by Windows without taking it out of
        exclusive mode - measured on a Direct3D 11 title, including with the mouse reaching it
        (ui.exclusive_presenter has the details). The picture is the overlay as it looks, buttons
        included; a click is mapped back to the button under it and that button's own slot runs, so
        prompts are answered, the carousel steps and "don't show again" works as in the window. The
        countdown, the hover pause and the way it is closed are the window's own.

        *pointer* (the hotkey dashboard) frees the mouse from the game's clip and, when the game
        has hidden the pointer, draws an arrow the same way - what SystemCursor does for the window.

        For the first seconds the game is watched (_check_presenter_front). It is held against the
        picture only when the exclusive mode was taken away from the game while nothing else had
        the front - never because the player pressed Alt+Tab - and then that game is not tried
        again (overlay_exclusive_deny).
        """
        try:
            from core.config_manager import get_config
            cfg = get_config()
            if not cfg.get("overlay_over_exclusive", True):
                return False
            from ui.exclusive_presenter import (ExclusivePresenter, FrontWatch,
                                                monitor_rect, process_name)
            if not ExclusivePresenter.supported():
                return False
            watch = FrontWatch()
            exe = process_name(watch.hwnd)
            denied = {str(e).lower() for e in (cfg.get("overlay_exclusive_deny", []) or [])}
            if exe and exe.lower() in denied:
                return False
            self._xp_mon = monitor_rect(watch.hwnd)
            if self._xp_mon is None:
                return False
            self._xp_game = watch.hwnd
            self._xp_own = self._own_windows()
            if self._xp is None:
                self._xp = ExclusivePresenter(click_through=False, on_mouse=self._on_presenter_mouse_raw)
            if not self._paint_presenter():
                return False
        except Exception:
            logger.debug("Overlay: could not present over exclusive fullscreen", exc_info=True)
            return False
        self._xp_watch, self._xp_exe = watch, exe
        self._xp_watch_until = time.monotonic() + _XP_WATCH_S
        self._xp_watch_timer.start()
        # The window's own countdown and expiry action (a path-changed prompt's, for one).
        self._on_expire_action = None
        self._auto_hide_ms = max(0, int(auto_hide))
        if self._auto_hide_ms > 0:
            self._auto_hide_timer.start(self._auto_hide_ms)
        else:
            self._auto_hide_timer.stop()
        if pointer:
            # Over a game in exclusive fullscreen by definition: no need for it to be one SaveSync tracks.
            logger.info("Overlay pointer: freeing the mouse from the game's clip; an arrow of our own is drawn "
                        "whenever the game hides the system pointer")
            self._xp_cursor_timer.start()
            self._xp_cursor_tick()
        else:
            self._xp_cursor_timer.stop()
        return True

    def _render_for_presenter(self):
        """The overlay as it would look, as a premultiplied-alpha QImage, and what a click can reach:
        [(rect in the picture's own pixels, QPushButton)]. (None, []) on failure."""
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QImage
        # What showEvent does for the window and a widget that is only grabbed never gets: the
        # pending-games badge. Without it the first picture lacked a badge the window has.
        self.refresh_unknown_badge()
        self._sync_scale_to_screen("show")
        # A hidden widget does not re-lay itself out when children change: without this the
        # size comes from whatever the layout last computed (a render that depended on the
        # one before it).
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.adjustSize()
        lit = self._xp_hover_btn if self._presenting() else None
        painters = []
        for b, pressed in ((lit, self._xp_pressed is lit and lit is not None),):
            if b is not None:
                try:
                    p = _PaintAsHovered(b, pressed)
                    b.installEventFilter(p)
                    painters.append((b, p))
                except RuntimeError:
                    pass                  # deleted meanwhile
        try:
            pixmap = self.grab()
        finally:
            for b, p in painters:
                try:
                    b.removeEventFilter(p)
                    p.deleteLater()
                except RuntimeError:
                    pass
        if pixmap.isNull():
            return None, []
        dpr = pixmap.devicePixelRatio()
        hits = []
        for b in self.findChildren(QPushButton):
            try:
                if b.isVisibleTo(self) and b.isEnabled():
                    corner = b.mapTo(self, QPoint(0, 0))
                    hits.append((QRect(round(corner.x() * dpr), round(corner.y() * dpr),
                                       round(b.width() * dpr), round(b.height() * dpr)), b))
            except RuntimeError:
                pass          # deleted meanwhile
        return pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), hits

    def _paint_presenter(self) -> bool:
        """Render the overlay and put it in the top-right of the monitor the game is on."""
        image, hits = self._render_for_presenter()
        if image is None or self._xp_mon is None:
            return False
        w, h = image.width(), image.height()
        margin = max(2, round(10 * image.devicePixelRatio()))
        x, y = self._xp_mon[2] - w - margin, self._xp_mon[1] + margin       # the top-right, as the window is
        if not self._xp.present(w, h, bytes(image.constBits())[:w * h * 4], x, y):
            return False
        self._xp_hits = hits
        return True

    def _refresh_presenter(self) -> None:
        """The overlay's content changed while the picture is up (a button was pressed, a notification
        stepped): draw it again."""
        if self._presenting():
            self._paint_presenter()

    def _on_presenter_mouse_raw(self, kind: str, x: int, y: int) -> None:
        """From the picture's window procedure: handed on at once, so nothing slow happens there."""
        QTimer.singleShot(0, lambda: self._on_presenter_mouse(kind, x, y))

    def _presenter_hit(self, x: int, y: int):
        for rect, button in self._xp_hits:
            if rect.contains(x, y):
                return button
        return None

    def _on_presenter_mouse(self, kind: str, x: int, y: int) -> None:
        """The mouse over the picture: hover holds the countdown, a press and release on the same
        button presses it."""
        if not self._presenting():
            return
        if kind == "move":
            if not self._xp_hover:
                self._xp_hover = True
                if self._auto_hide_ms > 0:               # the window restarts it whole when the pointer arrives
                    self._auto_hide_timer.start(self._auto_hide_ms)
            self._set_picture_hover(self._presenter_hit(x, y))
        elif kind == "leave":
            self._xp_hover = False
            self._set_picture_hover(None)
        elif kind == "down":
            self._xp_pressed = self._presenter_hit(x, y)
            if self._xp_pressed is not None:
                self._xp_hover_btn = self._xp_pressed
                self._refresh_presenter()
        elif kind == "up":
            pressed, self._xp_pressed = self._xp_pressed, None
            if pressed is not None and pressed is self._presenter_hit(x, y):
                self._xp_watch_timer.stop()          # what a button opens is not the picture's doing
                self._xp_watch = None
                try:
                    pressed.click()
                except RuntimeError:
                    pass          # deleted by an earlier press
                QTimer.singleShot(60, self._refresh_presenter)

    def _set_picture_hover(self, button) -> None:
        """The picture is a still, so a button under the pointer is shown highlighted the way the window shows it
        (see _PaintAsHovered), and the picture is drawn again."""
        if button is self._xp_hover_btn:
            return
        self._xp_hover_btn = button
        self._refresh_presenter()

    def _xp_cursor_tick(self) -> None:
        """While the picture is up and the mouse was asked for: keep the pointer free of the game's
        clip, and draw an arrow of our own - the same safe way - when the game has hidden the real one."""
        if not self._presenting():
            self._xp_cursor_timer.stop()
            return
        try:
            from ui.exclusive_presenter import ExclusivePresenter, free_cursor, cursor_state
            free_cursor()
            state = cursor_state()
            if state is not None and not state[0]:
                if self._xp_arrow is None:
                    logger.info("Overlay pointer: the game has hidden the system pointer - drawing an arrow")
                    self._xp_arrow = ExclusivePresenter()                  # click-through
                self._xp_arrow.present(32, 32, self._arrow_picture(), state[1], state[2])
            elif self._xp_arrow is not None:
                self._xp_arrow.hide()
        except Exception:
            logger.debug("Overlay: pointer handling over exclusive fullscreen failed", exc_info=True)

    def _arrow_picture(self) -> bytes:
        """A mouse arrow, 32x32, premultiplied BGRA."""
        if self._xp_arrow_data is None:
            from PySide6.QtCore import QPointF
            from PySide6.QtGui import QImage, QPainter, QColor, QPen, QPolygonF
            img = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(0, 0, 0), 2))
            painter.setBrush(QColor(255, 255, 255))
            painter.drawPolygon(QPolygonF([QPointF(2, 2), QPointF(2, 24), QPointF(8, 18), QPointF(12, 28),
                                           QPointF(16, 26), QPointF(12, 17), QPointF(20, 17)]))
            painter.end()
            self._xp_arrow_data = bytes(img.constBits())[:32 * 32 * 4]
        return self._xp_arrow_data

    def _session_tick(self) -> None:
        """The session counter on the hotkey dashboard: once a second, while that view is on screen."""
        if self._overlay_mode != "manual" or self._session_start <= 0 or not self._on_screen():
            self._session_timer.stop()
            return
        self._dash_rows[4][1].setText(format_clock(time.time() - self._session_start))
        if self._presenting():
            self._refresh_presenter()          # the picture over an exclusive game is a still: draw it again

    def _hide_presenter(self):
        """Take the picture down (or do nothing when there is none)."""
        self._xp_watch_timer.stop()
        self._xp_cursor_timer.stop()
        self._xp_watch = None
        self._xp_hits = []
        self._xp_hover = False
        self._xp_pressed = None
        self._xp_hover_btn = None
        if self._xp_arrow is not None:
            self._xp_arrow.hide()
        if self._xp is not None:
            self._xp.hide()

    def _end_presenter(self):
        """Take the picture away the way the window is taken away: hide_animated. That also empties the
        notification queue (which drops a notification identical to one it still holds, so without
        it the second "Saved - <game>" of a session would never be shown), releases a prompt's
        priority and replays what was deferred behind it."""
        if self._presenting() or self._rw_exclusive:
            self.hide_animated()
        else:
            self._hide_presenter()

    def _check_presenter_front(self):
        """While the picture is young: did something happen to the game? Judged by what happened
        FIRST (ui.exclusive_presenter.classify_front_change): the player pressing Alt+Tab, opening
        the Start menu or answering a popup moves the front to another window before anything
        else, and must never count against the picture."""
        watch = self._xp_watch
        if watch is None:
            self._xp_watch_timer.stop()
            return
        if time.monotonic() > self._xp_watch_until:
            # It stayed up its whole watch with the game in front: a clean presentation, so
            # whatever doubts earlier ones left about this game are over.
            self._xp_suspects.pop(self._xp_exe, None)
            self._xp_watch_timer.stop()
            return
        if self._own_windows() - self._xp_own:
            # The app opened a window of its own (a button that opens the app, a dialog): that takes
            # the front from the game just as surely, and is not the picture's doing.
            logger.debug("Overlay: the app opened a window - no longer judging the picture")
            self._xp_watch_timer.stop()
            self._xp_watch = None
            return
        verdict = watch.check()
        if verdict is not None:
            self._presenter_lost_front(verdict, watch)

    def _own_windows(self) -> set:
        """The app's visible top-level windows other than this one (the main window, a dialog, the
        blur backdrop, a pin)."""
        try:
            return {id(w) for w in QApplication.topLevelWidgets() if w is not self and w.isVisible()}
        except Exception:
            return set()

    def presented_game(self) -> int:
        """The window of the game the picture is up over, or 0 when there is no picture. For a button's
        handler: a window it opens takes the game out of exclusive mode, and the game has to be
        given the screen back afterwards."""
        return self._xp_game if (self._presenting() or (self._rw_exclusive and self.isVisible())) else 0

    def _presenter_lost_front(self, verdict: str, watch) -> None:
        """The game is no longer as it was when the picture appeared. The picture always comes
        down; what is REMEMBERED depends on what happened:

        - "left"     somebody else took the front first (Alt+Tab, the Start menu, another
                     program): nothing is remembered.
        - "suspect"  the front went to no window: counted, and remembered only when it happens
                     twice in a row for this game with no clean presentation in between. A UAC
                     prompt or Ctrl+Alt+Del does this once; a game that really cannot take the
                     picture does it every time.
        - "kick"     the game was taken out of its exclusive mode while it still had the front:
                     remembered at once.

        What triggered it is written to the log, so a report can be answered from it.
        """
        exe = self._xp_exe
        detail = watch.describe()
        real = bool(self._rw_exclusive)              # the overlay's own window was what was shown, not the picture
        self._end_presenter()
        record = verdict == "kick"
        if verdict == "suspect":
            self._xp_suspects[exe] = self._xp_suspects.get(exe, 0) + 1
            record = self._xp_suspects[exe] >= 2
        logger.info("Overlay: %s left its place after the %s was shown over it: %s (%s)%s",
                    exe or "a game", "window" if real else "picture", verdict, detail,
                    " - not tried on it again" if record else "")
        if not record:
            return
        if self._xp is not None and not real:
            self._xp.destroy()
        self._xp_suspects.pop(exe, None)
        try:
            from core.config_manager import get_config
            cfg = get_config()
            key = "overlay_window_deny" if real else "overlay_exclusive_deny"
            if exe:
                deny = list(cfg.get(key, []) or [])
                if exe.lower() not in {str(d).lower() for d in deny}:
                    deny.append(exe)
                    cfg.set(key, deny)
            elif real:
                logger.warning("Overlay: the game could not be named - its window cannot be remembered as refused")
            else:
                cfg.set("overlay_over_exclusive", False)
                logger.warning("Overlay: the game could not be named - the picture is turned off")
        except Exception:
            logger.debug("Overlay: could not record the deny-list entry", exc_info=True)

    def _re_show_over_exclusive(self):
        """The overlay is up as an ordinary window and the game in front has just gone exclusive (a notice shown while the
        game was still starting, the display mode switching under it). The ordinary window goes, and what it held is shown
        again the way it is shown over such a game - its own window in the higher band, or the picture - which also
        measures it again for the screen it is on now. Only when neither is possible is it hidden, as before."""
        pointer = self._cursor_timer.isActive()
        # What was left of the time it was given. A countdown that is not running (paused while the pointer is over the notice, or
        # not started yet) is the whole time again - not "no end": that left the notice on screen for good.
        if self._auto_hide_timer.isActive():
            left = max(self._auto_hide_timer.remainingTime(), 1500)
        else:
            left = self._auto_hide_ms
        self._auto_hide_timer.stop()
        self._topmost_timer.stop()
        self._uninstall_foreground_hook()
        self._anim.stop()
        if self._hide_anim_connected:
            try:
                self._anim.finished.disconnect(self._do_hide)
            except (RuntimeError, TypeError):
                pass
            self._hide_anim_connected = False
        self.hide()
        self._cursor_timer.stop()
        SystemCursor.release("overlay")
        self.show_animated(auto_hide=max(0, left), pointer=pointer, over_exclusive=True)
        if self._rw_exclusive:
            # The display mode is still settling: measure once more when it has.
            QTimer.singleShot(700, lambda: self._resync_visible(force=True))
        logger.info("Overlay: the game went exclusive while it was up - shown again over it (as a window of the higher band: %s, "
                    "goes by itself in %s ms)", self._rw_exclusive, max(0, left) or "never")

    def _hide_for_exclusive(self):
        """Hide the overlay to protect a game's exclusive fullscreen mode.

        Centralised helper so every code path (hook, poll) behaves identically:
        stops timers, unhooks, hides, and emits the exclusive_blocked signal.
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
        self._flush_deferred_notifs()
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

        if self._is_exclusive_fullscreen_hwnd(fg_hwnd) and not self._rw_exclusive:
            self._re_show_over_exclusive()
        elif not popup_is_open():
            self._take_the_front()
            self._trace_z("the window in front changed")  # i18n-ignore (log text)

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
        self._apply_click_through()

        if platform.system() != "Windows":
            # On Linux/macOS: unconditional raise_() via _force_topmost.
            # This is the only mechanism we have to recover z-order.
            self._apply_fullscreen_stacking()
            self._take_the_front()
            return

        if self._is_fullscreen_active():
            if self._is_exclusive_fullscreen() and not self._rw_exclusive:
                self._re_show_over_exclusive()
            else:
                self._take_the_front()
                self._trace_z("borderless fullscreen")
        elif self._is_overlay_obscured():
            self._take_the_front()
            self._trace_z("obscured by a window")  # i18n-ignore (log text)

    def _take_the_front(self):
        """Raise the overlay — then hand the front straight back to the pins.

        A pin is something the user put on screen to keep reading; the overlay
        announces things and goes away. So the pins go above it, always, and
        the only place that can be guaranteed is here: every raise the overlay
        does passes through this.
        """
        _force_topmost(self)
        self._keep_menu_on_top()
        try:
            from ui.widgets.pins import get_pin_manager
            get_pin_manager().assert_topmost_all("the overlay came forward")  # i18n-ignore (log text)
        except Exception:
            logger.debug("overlay: could not put the pins back on top",
                         exc_info=True)

    def _keep_menu_on_top(self) -> None:
        """Raising the overlay does not carry the windows it owns with it (the raise is made so that it does not), and the 📌 menu is
        one that opens inside the overlay's own rectangle: whenever the overlay comes forward, an open menu goes back over it."""
        menu = self._tool_menu
        if menu is None:
            return
        try:
            if menu.isVisible():
                _force_topmost(menu)
        except RuntimeError:
            pass

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
        self._close_tool_menu()
        self._rw_exclusive = False
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
        # Now that nothing is on screen, the queue's controls can go.
        self._hide_carousel_controls()
        if platform.system() == "Windows" and win_band_hook.installed():
            from ui.exclusive_session import log_windows
            QTimer.singleShot(1200, lambda: log_windows("1.2 s after the overlay was hidden"))

    def _get_active_screen_geometry(self):
        """Get the available geometry of the screen where the active window / cursor is."""
        # Use the screen under the cursor — most reliable when resolution changes
        return self._get_active_screen().availableGeometry()

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
                return _window_fills_monitor(int(hwnd))
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
            if not hwnd:
                return bool(getattr(self, "_last_exclusive", False))     # (no window in front for a moment: the last answer stands)
            verdict = self._is_exclusive_fullscreen_hwnd(int(hwnd))
            self._last_exclusive = verdict
            return verdict
        except Exception:
            return False

    def _is_exclusive_fullscreen_hwnd(self, hwnd: int) -> bool:
        """Check if *hwnd* is in exclusive fullscreen mode: a chrome-less window the
        size of its screen WHILE the shell reports an exclusive-mode Direct3D
        application (see _shell_notification_state). A borderless fullscreen game
        is not exclusive and gets the overlay.

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

            # Check if the window covers the whole monitor
            if _window_fills_monitor(hwnd):
                # Chrome-less and monitor-sized is what a borderless game looks
                # like too - the shape cannot tell the two apart, and treating
                # it as proof kept the overlay off every fullscreen game. Ask the
                # shell whether an exclusive-mode Direct3D application is really
                # running; only that is a reason to stay away. When it cannot be
                # asked, keep the cautious answer.
                qstate = _shell_notification_state()
                if qstate is None or qstate == _QUNS_RUNNING_D3D_FULL_SCREEN:
                    verdict = True
                    if qstate is not None:
                        self._exclusive_seen_hwnd = hwnd
                elif qstate == 2 and win_band_hook.installed() and (
                        hwnd == getattr(self, "_exclusive_seen_hwnd", 0) or self._switched_mode(hwnd)):
                    # QUNS_BUSY, for the game that the shell called an exclusive Direct3D application a moment ago: it said so
                    # every time, half a second after a window of ours came up over the game (the log shows 3, then 2). Taken as
                    # "a borderless game" the overlay was shown as an ordinary window - invisible over the game, and still
                    # taking the mouse and the cursor. Or a game that has switched the display mode of its monitor, which no
                    # borderless game does: the log of a real game shows it start as a window, switch its monitor to 1920x1080 (sixteen
                    # screen changes), and the shell say "busy" ever after.
                    verdict = True
                    self._exclusive_seen_hwnd = hwnd
                else:
                    verdict = False
                    if qstate != 2:
                        self._exclusive_seen_hwnd = 0
                self._log_fullscreen_verdict(hwnd, style, qstate, verdict)
                return verdict
        except Exception:
            pass
        return False

    def _switched_mode(self, hwnd: int) -> str:
        """"1920x1080 on a 3840x2160 desktop" when the game has switched the display mode of its monitor, else ""."""
        try:
            from ui.exclusive_presenter import display_mode_switch
            self._mode_switch_seen = display_mode_switch(hwnd)
        except Exception:
            self._mode_switch_seen = ""
        return self._mode_switch_seen

    def _log_fullscreen_verdict(self, hwnd: int, style: int, qstate, exclusive: bool) -> None:
        """Say why a fullscreen window was (not) treated as exclusive - once per change
        of answer, not per check: this runs for every foreground change and hotkey.
        The inputs are what is needed to tell, from a log, a game that really is in
        exclusive mode from one the shell reports otherwise."""
        switched = getattr(self, "_mode_switch_seen", "") if exclusive and qstate == 2 else ""
        key = (qstate, exclusive, bool(switched))
        if getattr(self, "_fs_verdict_logged", None) == key:
            return
        self._fs_verdict_logged = key
        exe = ""
        try:
            import psutil
            pid = wintypes.DWORD(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = psutil.Process(pid.value).name()
        except Exception:
            pass
        logger.info("Fullscreen window %r: shell state=%s, style=0x%08X -> %s%s", exe, qstate,
                    style & 0xFFFFFFFF,
                    "exclusive (overlay kept off)" if exclusive
                    else "not exclusive (overlay may show over it)",
                    f" - the shell says busy, but the game has switched its monitor's display mode ({switched})" if switched else "")

    # Screen add/remove/geometry wiring comes from ScreenSignalMixin;
    # only the reaction is overlay-specific:
    def _on_screen_changed(self, *_args):
        """A monitor was added, removed or changed geometry.

        Hidden, there is nothing to fix: the next render reads the scale of the
        screen it appears on (see _sync_scale_to_screen). Visible, the work
        waits for the burst of signals a monitor change produces to stop, then
        re-scales, redraws and re-positions in one go.
        """
        if self.isVisible():
            logger.debug("Screen geometry changed — re-fitting overlay")
            self._screen_settle_timer.start()

    def deleteLater(self):
        """Ensure hooks and screen signals are disconnected before destruction."""
        self.cleanup()
        super().deleteLater()

    def cleanup(self):
        """Disconnect screen signals and hooks to prevent crashes after widget destruction."""
        self._cleaned_up = True
        self._hide_presenter()
        if self._xp is not None:
            self._xp.destroy()
            self._xp = None
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
        self._carousel_prev.setObjectName("overlay_carousel_arrow")
        self._carousel_next.setObjectName("overlay_carousel_arrow")

    def refresh_styles(self):
        """Re-apply all inline styles after theme change."""
        from ui.styles.theme import get_theme_manager
        self._is_dark = get_theme_manager().is_dark()
        # The own stylesheet is a copy of the theme: a new theme (or a new
        # application scale) makes it out of date, so it is rebuilt here.
        self._apply_own_stylesheet(force=True)
        self._refresh_badge_interactivity()
        # #overlay_icon / #overlay_separator / #overlay_suppress_btn are theme-owned.
        self._style_carousel_arrows()
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
