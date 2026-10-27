"""SaveSync - the friend list, a drawer that slides out of the sidebar: add people
by their friend code, write to each of them, hand them your transfer token from the
Receive panel, and use the tokens they send.

It is only a VIEW of ui.p2p_friends_service, which keeps the friend list, looks
after the DHT mailboxes (core.p2p.friend_net) and holds the messages friends
have sent, each with its countdown. It lies over the page beside the sidebar,
the way the sidebar itself does - the window is never made wider or narrower by
it - and has three pages:

- the LIST: each friend with a badge counting the messages of theirs you have not
  read (click one to open the chat) and a ⋯ to rename or remove them; under it,
  your own code and adding a friend;
- one friend's CHAT: the conversation - texts both ways (each of mine
  marked as waiting, sent or delivered), tokens they sent as cards you can use
  (Send, pick what to send, and it goes), attachments as cards with their own
  buttons, requests for a token as cards too (a friend who has a save to send asks for
  the token to send it to: Accept hands them one, Decline says no - and mine, which say
  whether they answered), and a note where some did not arrive - with a box to write in and a +
  to attach a file. Opened from the list, or straight from the notification of a
  new message. What is on show is read, which brings the badges down; what arrives
  while it is open is read as it comes, and is not announced again - but only
  while somebody is looking: when the window is minimized, put away to the tray, or
  another program has the focus, the chat is not "viewed" (see _sync_attention), so a
  message is announced like any other and stays new until the person is back;
- the LOCK screen, when the list has been locked with a password - with a way
  back in for a forgotten one that deletes the list (ui.dialogs.p2p_friends_reset);
- GROUP CHATS, over the friends on the list and on two pages of their own - a group's chat and
  its members (all of it in ui.dialogs.p2p_group_pages).

A token is good for 10 minutes. The ⋯ next to Lock holds the lock password
and linking another PC (a password-protected file, since the friend file is
bound to this machine).

What a friend writes is shown through core.p2p.markup: a handful of marks (bold,
italic, struck, code, a quote, a code block) and web addresses, written out as
HTML that module builds itself from escaped text. A link is never opened by a
click alone - it asks first (ui.dialogs.p2p_link_dialog) - and nothing in a
message is ever fetched. An attachment is an OFFER until the person presses
Accept; the arrival is checked against it, and the only things ever decoded from
a received file are a picture or a video, on request (ui.widgets.safe_image,
ui.widgets.safe_video; Settings can turn both off). SaveSync never opens or runs
a received file. Files and folders can be dragged onto the conversation (or the
box) to offer them, exactly as + does.
"""
import contextlib
import logging
import os
import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QFontMetrics, QGuiApplication, QKeySequence, QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from core.p2p import attachments as _a
from core.p2p import friends as _f
from core.p2p import markup
from core.p2p.friends import FriendError
from i18n import t
from ui.helpers import scaled
from ui.dialogs.p2p_group_pages import PAGE_GROUP, PAGE_MEMBERS, GroupPagesMixin
from ui.p2p_friends_service import format_remaining
from ui.styles.theme import ThemedMixin, palette
from ui.widgets.count_badge import CountBadge
from ui.widgets.media_fullscreen import ClickablePicture, fit_pixmap
from ui.widgets.presence_dot import PresenceDot
from ui.widgets.transfer_bar import style_transfer_bar

logger = logging.getLogger(__name__)

_PAGE_LIST, _PAGE_MESSAGES, _PAGE_LOCK = 0, 1, 2
# What counts as the person using the list (for the lock timer): a press, a key, the wheel.
_INPUT_EVENTS = frozenset((QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel))
# What changes whether the window is being looked at.
_WINDOW_EVENTS = frozenset((QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowStateChange))
_LINK_SUFFIX = ".ssfriends"
_LINK_HREF = re.compile(r"^l(\d{1,2})$")
# A download that has had nothing arrive for this long is said to be stuck (it is not
# ended - a connection has no time limit - and can be cancelled).
_IDLE_HINT_SECONDS = 60
_NO_CONNECTION_HINT_SECONDS = 45


def _video_available() -> bool:
    """Qt's multimedia part may be missing from a build: then there is no Play."""
    try:
        from ui.widgets.safe_video import available
        return available()
    except Exception:
        return False


def _mix(a: str, b: str, ratio: float) -> str:
    """The colour *ratio* of the way from *a* to *b*, as #rrggbb."""
    ca, cb = QColor(a), QColor(b)
    mix = [round(x + (y - x) * ratio) for x, y in
           ((ca.red(), cb.red()), (ca.green(), cb.green()), (ca.blue(), cb.blue()))]
    return "#{:02x}{:02x}{:02x}".format(*mix)


def _clear(layout) -> None:
    """Empty *layout*. hide() BEFORE deleteLater(): until the deferred delete
    runs, a widget taken out of a layout stays on screen at its last size and
    paints over the rows that replace it."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


MAX_DROPPED = 200


def dropped_paths(mime) -> list:
    """The local files and folders in what is being dragged (nothing from a web
    page, nothing virtual, nothing without a name, a drive root included), at
    most MAX_DROPPED, each once."""
    out: list = []
    if mime is None or not mime.hasUrls():
        return out
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        try:
            if path and Path(path).name and Path(path).exists() and path not in out:
                out.append(path)
        except OSError:
            continue
        if len(out) >= MAX_DROPPED:
            break
    return out


def _breakable(path) -> str:
    """A path a label may break after each of its folders. A line is only broken at a space, a path
    has few, and one long folder name wider than the page pushed the whole card out of the drawer."""
    return str(path).replace("\\", "\\\u200b").replace("/", "/\u200b")


class _ComposeEdit(QPlainTextEdit):
    """The box a message is written in: Enter sends, Shift+Enter is a new line.
    Files dragged onto it are not pasted as text: they are offered (see the
    dialog), exactly as if they had been dropped anywhere else on the page."""
    submitted = Signal()
    files_dropped = Signal(list)
    drag_state = Signal(bool)

    def dragEnterEvent(self, event):
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()
            self.drag_state.emit(True)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self.drag_state.emit(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        paths = dropped_paths(event.mimeData())
        self.drag_state.emit(False)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


def _dots(widget) -> tuple:
    """The "more" mark for a button, and the style that makes it show: ⋯, or ••• where
    the font has no such glyph (it would be an empty box). Both are drawn as small
    specks at the size of the sidebar's icons, so both are made larger and bolder."""
    if QFontMetrics(widget.font()).inFontUcs4(0x22EF):
        return "⋯", f"font-size:{scaled(22, widget)}px;font-weight:800;"
    return "•••", f"font-size:{scaled(13, widget)}px;font-weight:800;"


class _FriendRow(QFrame):
    """One friend in the list. The whole row opens their chat (Enter and Space
    too); what sits on it that can be clicked - the ⋯ - takes its own clicks."""
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("p2p_friend_row")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pressed = False
        self.setStyleSheet(
            "#p2p_friend_row { background: transparent; border-radius: 8px; }"
            f"#p2p_friend_row:hover, #p2p_friend_row:focus {{ background: {palette('bg_elevated')}; }}")

    def mousePressEvent(self, event):
        # (a press that is not taken goes to the parent, and the release with it)
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # A click is a press AND a release on this row: a release alone - the one that
        # closes a menu opened over the row, say - is not one.
        pressed, self._pressed = self._pressed, False
        if (pressed and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


class P2pFriendsPanel(GroupPagesMixin, QFrame, ThemedMixin):
    """The friend list as a drawer: it slides out of *anchor* (the sidebar) over the
    page beside it, and slides back in when closed. *host* is the widget both sit in.
    Built for one opening - closing deletes it, but never while a dialog it opened is
    still up (see _modal)."""
    closed = Signal()

    _WIDTH = 380            # design pixels (scaled), as much as the window can spare
    _MIN_WIDTH = 280
    _KEEP_VISIBLE = 120     # of the page beside it, in a window too narrow for all of it

    def __init__(self, service, host, anchor, *, open_friend: str = "", open_group: str = ""):
        super().__init__(host)
        self._svc = service
        self._host = host
        self._anchor = anchor
        self._window = host.window()
        self._slide = 0.0               # 0: tucked behind the sidebar, 1: all the way out
        self._anim = None
        self._closing = False
        self._finished = False          # hidden and announced as closed
        self._modal_depth = 0           # flows of ours that are inside a dialog's event loop
        self._discard_pending = False   # to be deleted as soon as no dialog of ours is up
        self._attending = False         # is a friend's chat on show AND somebody looking at it
        self._away = False              # the window is minimized or put away (see _sync_window)
        self._holding = False           # holds the network on the service (see acquire)
        self._friend_id = ""            # whose messages are on show ("" = the list)
        self._wanted_friend = open_friend   # to show once the list is open
        self._new_ids: set = set()      # messages that were unread when the page opened
        self._msg_labels: dict = {}         # message id -> (label, message)
        self._msgs_sig = None               # what the page shows now (to rebuild only on a change)
        self._list_sig = None               # what the list shows now (same)
        self._rows: dict = {}               # friend id -> its row on the list
        self._xfer: dict = {}               # attachment id -> (message, status, bar, speed), for live figures
        self._previews: dict = {}           # attachment id -> the picture the person asked to see
        self._videos: dict = {}             # attachment id -> the video player the person opened
        self._fs_picture = None             # the picture on show full screen, if any
        self._placed_width = 0              # the width the panel was last laid out at (see _place)
        self._retexts: list = []            # what sets the words of the fixed widgets (see update_locale)
        self._init_groups(open_group)
        self.setObjectName("p2p_friends_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)     # (so Esc works before anything in it is clicked)
        self.setAcceptDrops(True)           # files and folders dropped here are offered
        self._apply_chrome()
        self._build()
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close_panel)
        host.installEventFilter(self)
        anchor.installEventFilter(self)
        self._window.installEventFilter(self)
        QApplication.instance().installEventFilter(self)     # (only to see the person use it)
        QGuiApplication.instance().applicationStateChanged.connect(self._sync_attention)
        self._svc.changed.connect(self._refresh)
        self._svc.lock_changed.connect(self._on_lock_changed)
        # Open and left alone, this must not keep the list from locking itself: it holds
        # the network, and the lock timer runs from the last time it was touched.
        self._svc.acquire(blocks_lock=False)
        self._holding = True
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(
            lambda: self._copy_btn.setText(t("p2p.friends_copy_code")))
        self._update_net_status()
        self._refresh()
        self._enter()
        self._place()

    def _apply_chrome(self):
        # The sidebar's own colours, so it reads as the sidebar growing sideways.
        self._sty(self, lambda: (
            f"#p2p_friends_panel {{ background-color: {palette('bg')}; "
            f"border-right: 1px solid {palette('border')}; }}"))

    # The text colours the pages are written in. Read when used, not kept: they have to
    # follow the theme (see refresh_styles).
    @property
    def _ink(self) -> str:
        return f"color:{palette('text')};"

    @property
    def _muted(self) -> str:
        return f"color:{palette('text_secondary')};"

    @property
    def _bold(self) -> str:
        return f"font-weight:700;font-size:13px;{self._ink}"

    # ── Theme and language, in place ─────────────────────────────────────

    def refresh_styles(self):
        """The theme (or the UI scale) changed: restyle what is on show without
        rebuilding the panel - the draft, the scroll and the pictures stay."""
        if self._finished:
            return
        super().refresh_styles()
        self._list_sig = self._msgs_sig = self._groups_sig = self._group_sig = self._members_sig = None
        self._update_net_status()
        if not self._svc.locked:
            self._refresh()
            self._update_compose()

    def update_locale(self):
        """The language changed: the fixed widgets get their words again, and the
        friends and the conversation are written out again."""
        if self._finished:
            return
        for apply in self._retexts:
            apply()
        self._list_sig = self._msgs_sig = self._groups_sig = self._group_sig = self._members_sig = None
        self._update_net_status()
        if not self._svc.locked:
            self._refresh()
            self._update_compose()

    def _bind(self, widget, *, text: str = "", tip: str = "", hint: str = ""):
        """Give *widget* its words (message keys) and remember how, so that
        update_locale() can say them again in another language."""
        def apply():
            if text:
                widget.setText(t(text))
            if tip:
                widget.setToolTip(t(tip))
            if hint:
                widget.setPlaceholderText(t(hint))
        self._retexts.append(apply)
        apply()
        return widget

    def _later(self, ms: int, fn):
        """Run *fn* once, *ms* from now - unless the panel has gone by then. (A bare
        QTimer.singleShot would call into widgets that are deleted by then; this timer
        is the panel's child, so it goes with it.)"""
        timer = QTimer(self)
        timer.setSingleShot(True)

        def fire():
            timer.deleteLater()
            if not self._closing:
                fn()
        timer.timeout.connect(fire)
        timer.start(ms)

    # ── Sliding in and out ───────────────────────────────────────────────

    def _anchor_right(self) -> int:
        return self._anchor.mapTo(self._host, QPoint(self._anchor.width(), 0)).x()

    def _place(self):
        """Where the panel is for the current slide: its right edge on the sidebar's
        when out, wholly behind it when in (the sidebar is stacked over it)."""
        right = self._anchor_right()
        room = max(0, self._host.width() - right)
        width = min(room, scaled(self._WIDTH, self),
                    max(scaled(self._MIN_WIDTH, self), room - scaled(self._KEEP_VISIBLE, self)))
        x = round(right - width * (1.0 - self._slide))
        self.setGeometry(x, 0, width, self._host.height())
        if width != self._placed_width:
            # (the slide only moves x: this is a window made wider or narrower)
            self._placed_width = width
            if self._previews or self._videos:
                self._later(0, self._refit_media)

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype in _INPUT_EVENTS:
            # (the application's filter: every press, key and wheel turn of the program)
            if isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)):
                self._svc.touch()
        elif etype == QEvent.Type.Resize and obj in (self._host, self._anchor):
            self._place()
        elif etype in _WINDOW_EVENTS and obj is self._window:
            self._sync_window()
            self._sync_attention()
        return False

    def _set_slide(self, value):
        self._slide = float(value)
        self._place()

    def _animate(self, to: float, then=None):
        # One slide at a time: a second one started over the first (a double click, Esc
        # during the opening) would have both moving the panel, and the stale one would
        # go on after the panel is gone.
        old, self._anim = self._anim, QVariantAnimation(self)
        if old is not None:
            old.stop()
            old.deleteLater()
        anim = self._anim
        anim.setStartValue(float(self._slide))
        anim.setEndValue(float(to))
        anim.setDuration(190 if to else 140)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic if to else QEasingCurve.Type.InCubic)
        anim.valueChanged.connect(self._set_slide)
        if then is not None:
            anim.finished.connect(then)
        anim.start()

    def open_panel(self):
        """Slide out of the sidebar."""
        self._place()
        self.raise_()
        self._anchor.raise_()           # over the panel, so that it comes out from behind it
        self.show()
        self._animate(1.0)
        if self._stack.currentIndex() == _PAGE_MESSAGES:
            self._compose.setFocus()
        elif self._stack.currentIndex() == PAGE_GROUP:
            self._group_compose.setFocus()
        else:
            self.setFocus()
        self._sync_attention()

    @property
    def closing(self) -> bool:
        return self._closing

    def close_panel(self, animated: bool = True):
        """Slide back into the sidebar and go. The service is let go of at once, so a
        message that arrives while the panel is on its way out is announced as usual."""
        if self._closing:
            return
        self._closing = True
        try:
            self._teardown()
        except Exception:
            # (whatever fails, the panel still goes: half closed it could not be closed)
            logger.warning("P2P friends: the panel did not tidy up cleanly", exc_info=True)
        if animated and self.isVisible():
            self._animate(0.0, self._finish)
        else:
            self._finish()

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        try:
            if self._anim is not None:
                self._anim.stop()
            self._host.removeEventFilter(self)
            self._anchor.removeEventFilter(self)
            self.hide()
        finally:
            self.closed.emit()
            self._discard()

    def _discard(self):
        """Delete the panel - once no dialog of its own is up. Deleting it under one
        would take the dialog with it (it is its child) in the middle of exec()."""
        if self._modal_depth:
            self._discard_pending = True
        else:
            self.deleteLater()

    @contextlib.contextmanager
    def _modal(self):
        """Around everything that runs a dialog (or a menu) of the panel's, and around
        the whole of a flow that runs several: the panel may be closed meanwhile - the
        window was minimized, a game started, the theme changed - and goes only when the
        last of them is over. Code after the dialog can still find the panel closed."""
        self._modal_depth += 1
        try:
            yield
        finally:
            self._modal_depth -= 1
            if not self._modal_depth:
                if self._discard_pending:
                    self._discard_pending = False
                    QTimer.singleShot(0, self._discard)
                elif not self._closing:
                    self._apply_wanted_friend()

    def _apply_wanted_friend(self):
        """A friend's chat was asked for while a dialog was up (a notification's
        "Open message"): it switches the page under the dialog's flow, so it waited."""
        if self._wanted_friend and not self._svc.locked:
            self.show_friend(self._wanted_friend)
        else:
            self._apply_wanted_group()

    # ── Layout ───────────────────────────────────────────────────────────

    def _icon_button(self, text: str, tip: str, slot, css: str = "") -> QPushButton:
        """A small square button in the sidebar's own icon-button style. That style has
        no padding: with the ordinary button's 16 px each side, a button this narrow
        has no room left for its mark and shows nothing."""
        btn = QPushButton(text)
        btn.setObjectName("toolbar_icon_btn")
        btn.setFixedSize(scaled(28, self), scaled(28, self))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tip)
        if css:
            btn.setStyleSheet(css)
        if slot is not None:
            btn.clicked.connect(lambda _=False: slot())
        return btn

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 14, 14)
        outer.setSpacing(10)

        # The head, over every page: the title and the close; with them, except on the
        # lock screen, lock now and the menu.
        head = QHBoxLayout()
        head.setSpacing(6)
        title = self._bind(QLabel(), text="p2p.friends_title")
        self._sty(title, lambda: f"font-weight:700;font-size:15px;{self._ink}")
        head.addWidget(title, 1)
        self._tools = QWidget()
        tools = QHBoxLayout(self._tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self._lock_btn = self._bind(QPushButton(), text="p2p.friends_lock",
                                    tip="p2p.friends_lock_tooltip")
        self._lock_btn.clicked.connect(self._svc.lock)
        tools.addWidget(self._lock_btn)
        mark, css = _dots(self)
        self._menu_btn = self._bind(
            self._icon_button(mark, "", self._show_menu, css), tip="p2p.friends_menu_tooltip")
        tools.addWidget(self._menu_btn)
        head.addWidget(self._tools)
        head.addWidget(self._bind(self._icon_button("✕", "", self.close_panel),
                                  tip="p2p.receive_close"))
        outer.addLayout(head)
        # Whether the network is going through the proxy (or stopped for want of one).
        # On a line of its own: what it says can be long, and the head is narrow.
        self._net_lbl = QLabel("")
        self._net_lbl.setWordWrap(True)
        self._net_lbl.setVisible(False)
        outer.addWidget(self._net_lbl)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_list_page())
        self._stack.addWidget(self._build_messages_page())
        self._stack.addWidget(self._build_lock_page())
        self._stack.addWidget(self._build_group_page())
        self._stack.addWidget(self._build_members_page())
        outer.addWidget(self._stack, 1)

    def _scroller(self, body: QWidget) -> QScrollArea:
        """The lists sit in a scrolling area, on the dialog's own background."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.setObjectName("p2p_friends_body")
        body.setStyleSheet("#p2p_friends_body { background: transparent; }")
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(body)
        body.setAutoFillBackground(False)          # (setWidget turns it on)
        return scroll

    def _build_list_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        self._reset_lbl = self._bind(QLabel(), text="p2p.friends_reset_note")
        self._reset_lbl.setWordWrap(True)
        self._sty(self._reset_lbl, lambda: f"color:{palette('warning')};")
        self._reset_lbl.setVisible(bool(getattr(self._svc.store, "reset_reason", "")))
        outer.addWidget(self._reset_lbl)

        # The friends, which is what the list is for, come first and take the room.
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 4, 0)
        body_lay.setSpacing(6)
        self._build_groups_block(body_lay)
        lbl = self._bind(QLabel(), text="p2p.friends_list_title")
        self._sty(lbl, lambda: self._bold)
        body_lay.addWidget(lbl)
        # How it works is for whoever has nobody in the list yet.
        self._intro_lbl = self._bind(QLabel(), text="p2p.friends_intro")
        self._intro_lbl.setWordWrap(True)
        self._sty(self._intro_lbl, lambda: self._muted)
        body_lay.addWidget(self._intro_lbl)
        self._friends_box = QVBoxLayout()
        self._friends_box.setSpacing(2)
        body_lay.addLayout(self._friends_box)
        body_lay.addStretch(1)
        outer.addWidget(self._scroller(body), 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        lbl = self._bind(QLabel(), text="p2p.friends_my_code")
        self._sty(lbl, lambda: self._bold)
        outer.addWidget(lbl)
        row = QHBoxLayout()
        self._code_edit = QLineEdit(self._svc.code if not self._svc.locked else "")
        self._code_edit.setReadOnly(True)
        self._code_edit.setStyleSheet(f"font-family: monospace; font-size: {scaled(12, self)}px;")
        row.addWidget(self._code_edit, 1)
        self._copy_btn = self._bind(QPushButton(), text="p2p.friends_copy_code")
        self._copy_btn.clicked.connect(self._copy_code)
        row.addWidget(self._copy_btn)
        outer.addLayout(row)
        self._name_lbl = QLabel()
        self._name_lbl.setWordWrap(True)
        self._sty(self._name_lbl, lambda: self._muted)
        outer.addWidget(self._name_lbl)

        lbl = self._bind(QLabel(), text="p2p.friends_add_title")
        self._sty(lbl, lambda: self._bold + "padding-top:4px;")
        outer.addWidget(lbl)
        self._add_code = self._bind(QLineEdit(), hint="p2p.friends_add_code_placeholder")
        self._add_code.returnPressed.connect(self._add_friend)
        outer.addWidget(self._add_code)
        add = QHBoxLayout()
        self._add_name = self._bind(QLineEdit(), hint="p2p.friends_add_name_placeholder")
        self._add_name.returnPressed.connect(self._add_friend)
        add.addWidget(self._add_name, 1)
        self._add_btn = self._bind(QPushButton(), text="p2p.friends_add_button")
        self._add_btn.setObjectName("primary_btn")
        self._add_btn.clicked.connect(self._add_friend)
        add.addWidget(self._add_btn)
        outer.addLayout(add)
        self._add_msg = QLabel("")
        self._add_msg.setWordWrap(True)
        self._sty(self._add_msg, lambda: self._muted)
        outer.addWidget(self._add_msg)
        return page

    def _build_messages_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self._back_btn = self._bind(QPushButton(), text="p2p.friends_back")
        self._back_btn.clicked.connect(self._back)
        head.addWidget(self._back_btn)
        head.addStretch(1)
        self._clear_btn = self._bind(QPushButton(), text="p2p.friends_clear",
                                     tip="p2p.friends_clear_tip")
        self._clear_btn.clicked.connect(self._clear_conversation)
        head.addWidget(self._clear_btn)
        outer.addLayout(head)
        # Under the buttons, not between them: a long name has the whole width.
        self._msgs_title = QLabel()
        self._msgs_title.setTextFormat(Qt.TextFormat.PlainText)
        self._msgs_title.setWordWrap(True)
        self._sty(self._msgs_title, lambda: self._bold)
        outer.addWidget(self._msgs_title)
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._msgs_dot = PresenceDot(False, scaled(10, self))
        status_row.addWidget(self._msgs_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._msgs_status = QLabel()
        self._sty(self._msgs_status, lambda: self._muted)
        status_row.addWidget(self._msgs_status, 1)
        outer.addLayout(status_row)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        self._msgs_box = QVBoxLayout()
        self._msgs_box.setSpacing(6)
        lay.addLayout(self._msgs_box)
        lay.addStretch(1)
        self._msgs_scroll = self._scroller(body)
        outer.addWidget(self._msgs_scroll, 1)

        # Writing: Enter sends. The counter is in bytes (what the mailbox has
        # room for), and sending is off while the text is too long or the
        # mailbox is full of messages still waiting for the friend.
        self._drop_lbl = QLabel(t("p2p.friends_drop_hint", name=""))
        self._drop_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._sty(self._drop_lbl, lambda: (
            f"border: 2px dashed {palette('accent')}; border-radius: 8px; "
            f"padding: 10px; color: {palette('accent')}; font-weight: 600;"))
        self._drop_lbl.setVisible(False)
        outer.addWidget(self._drop_lbl)
        self._compose_msg = QLabel("")
        self._compose_msg.setWordWrap(True)
        self._sty(self._compose_msg, lambda: f"color:{palette('warning')};")
        self._compose_msg.setVisible(False)
        outer.addWidget(self._compose_msg)
        row = QHBoxLayout()
        self._attach_btn = self._bind(QPushButton("+"), tip="p2p.friends_attach_tip")
        self._attach_btn.setFixedSize(scaled(34, self), scaled(34, self))
        self._attach_btn.setStyleSheet("font-size: 18px; font-weight: 700; padding: 0;")
        self._attach_btn.clicked.connect(self._attach)
        row.addWidget(self._attach_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self._compose = self._bind(_ComposeEdit(), hint="p2p.friends_compose_placeholder",
                                   tip="p2p.friends_compose_hint")
        self._compose.setFixedHeight(scaled(58, self))
        self._compose.textChanged.connect(self._update_compose)
        self._compose.submitted.connect(self._send_text)
        self._compose.files_dropped.connect(self._on_dropped)
        self._compose.drag_state.connect(self._show_drop_hint)
        row.addWidget(self._compose, 1)
        col = QVBoxLayout()
        self._send_btn = self._bind(QPushButton(), text="p2p.friends_compose_send")
        self._send_btn.setObjectName("primary_btn")
        self._send_btn.clicked.connect(self._send_text)
        col.addWidget(self._send_btn)
        self._counter = QLabel("")
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._counter)
        row.addLayout(col)
        outer.addLayout(row)
        return page

    def _build_lock_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 40, 0, 0)
        lay.setSpacing(10)
        icon = QLabel("🔒")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"font-size:{scaled(40, self)}px;")
        lay.addWidget(icon)
        title = self._bind(QLabel(), text="p2p.friends_locked_title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sty(title, lambda: self._bold)
        lay.addWidget(title)
        hint = self._bind(QLabel(), text="p2p.friends_locked_hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        self._sty(hint, lambda: self._muted)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch()
        self._unlock_edit = self._bind(QLineEdit(), hint="p2p.friends_unlock_placeholder")
        self._unlock_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._unlock_edit.setMinimumWidth(scaled(240, self))
        self._unlock_edit.returnPressed.connect(self._unlock)
        row.addWidget(self._unlock_edit)
        self._unlock_btn = self._bind(QPushButton(), text="p2p.friends_unlock")
        self._unlock_btn.setObjectName("primary_btn")
        self._unlock_btn.clicked.connect(self._unlock)
        row.addWidget(self._unlock_btn)
        row.addStretch()
        lay.addLayout(row)
        self._unlock_msg = QLabel("")
        self._unlock_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unlock_msg.setWordWrap(True)
        self._sty(self._unlock_msg, lambda: f"color:{palette('warning')};")
        lay.addWidget(self._unlock_msg)
        # The way back in when the password is forgotten: delete the list and start
        # again (Settings has the same button - it is never locked).
        forgot = QHBoxLayout()
        forgot.addStretch()
        self._forgot_btn = self._bind(QPushButton(), text="p2p.friends_forgot",
                                      tip="p2p.friends_forgot_tooltip")
        self._forgot_btn.setFlat(True)
        self._forgot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sty(self._forgot_btn,
                  lambda: f"color:{palette('text_secondary')};text-decoration:underline;")
        self._forgot_btn.clicked.connect(self._forgot)
        forgot.addWidget(self._forgot_btn)
        forgot.addStretch()
        lay.addLayout(forgot)
        lay.addStretch(1)
        return page

    # ── Which page ───────────────────────────────────────────────────────

    def _enter(self):
        """Show where the person should land: the lock screen, one friend's
        messages (when opened from a notification), or the list."""
        if self._svc.locked:
            self._show_lock()
        elif self._wanted_friend:
            self.show_friend(self._wanted_friend)
        elif self._wanted_group:
            self.show_group(self._wanted_group)
        else:
            self._stack.setCurrentIndex(_PAGE_LIST)
        self._sync_bar()

    def _scrub(self):
        """Forget what the pages show - the friends, the conversation and the draft,
        pictures and videos that were opened, the friend code. Locking seals the list
        and forgets what was decrypted; a window must not go on holding what it was
        built from."""
        self._close_videos()
        for box in (self._friends_box, self._msgs_box):
            _clear(box)
        self._msg_labels, self._xfer, self._previews, self._rows = {}, {}, {}, {}
        self._svc.forget_previews()
        self._msgs_sig, self._list_sig, self._new_ids, self._friend_id = None, None, set(), ""
        self._scrub_groups()
        self._compose.clear()
        self._code_edit.clear()
        self._msgs_title.setText("")
        self._msgs_status.setText("")

    def _show_lock(self):
        self._scrub()
        self._unlock_msg.setText("")
        self._unlock_edit.clear()
        self._stack.setCurrentIndex(_PAGE_LOCK)
        self._sync_attention()
        self._sync_bar()
        self._unlock_edit.setFocus()

    def _sync_bar(self):
        locked = self._stack.currentIndex() == _PAGE_LOCK
        self._tools.setVisible(not locked)
        self._lock_btn.setVisible(self._svc.has_lock_password)
        self._sync_net_visibility()

    def _sync_net_visibility(self):
        """The network line shows when it has something to say, except on the lock screen."""
        self._net_lbl.setVisible(bool(self._net_lbl.text())
                                 and self._stack.currentIndex() != _PAGE_LOCK)

    def current_friend(self) -> str:
        """The friend whose messages are on show ("" on the list / lock page)."""
        return self._friend_id if self._stack.currentIndex() == _PAGE_MESSAGES else ""

    # ── Who is looking ───────────────────────────────────────────────────

    def _sync_window(self):
        """The window was put away (minimized, to the tray) or brought back. The panel
        stays up - closing it would lose the draft and the place in the chat - but while
        the window is away it costs nothing: its timer is stopped and the network is no
        longer held open for it, so what Settings say about listening in the background
        is all that is left."""
        if self._closing:
            return
        win = self._window
        away = not win.isVisible() or win.isMinimized()
        if away == self._away:
            return
        self._away = away
        if away:
            self._tick.stop()
            if self._holding:
                self._holding = False
                self._svc.release(blocks_lock=False)
        else:
            if not self._holding:
                self._holding = True
                self._svc.acquire(blocks_lock=False)
            self._tick.start()
            self._on_tick()

    def _is_attended(self) -> bool:
        """Is a friend's chat on show, in a window somebody can see and is using?
        Another program in front (a game) or a window put away is not: the panel
        stays up, but what arrives is not read for them."""
        if self._closing or self._svc.locked or self._stack.currentIndex() not in (_PAGE_MESSAGES, PAGE_GROUP):
            return False
        win = self._window
        return (QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive
                and win.isVisible() and not win.isMinimized())

    def _sync_attention(self, _state=None):
        """Tell the service whose messages are being looked at - or none - so that what
        arrives is read as it comes when somebody is reading, and announced when not.
        Called whenever the page, the lock or the window's state changes."""
        attended, page = self._is_attended(), self._stack.currentIndex()
        fid = self._friend_id if attended and page == _PAGE_MESSAGES else None
        gid = self._group_id if attended and page == PAGE_GROUP else None
        if fid and not self._attending:
            # Back after being away: what came meanwhile is new, as when opening the chat.
            self._new_ids |= {m.id for m in self._svc.messages(fid) if not m.read}
            self._msgs_sig = None
        if gid and not self._attending_group:
            self._group_new |= {e["id"] for e in self._svc.group_log(gid)
                                if e["dir"] == "in" and not e["read"] and e["kind"] in ("text", "file")}
            self._group_sig = None
        self._attending = bool(fid)
        self._attending_group = bool(gid)
        self._svc.set_viewing(fid)
        self._svc.set_viewing_group(gid)

    def show_friend(self, fid: str):
        """Open one friend's messages (also when the panel is already up and a
        notification asks for them)."""
        if self._svc.locked:
            self._wanted_friend = fid
            self._show_lock()
            return
        if self._modal_depth:
            # A dialog of ours is up and the flow behind it is about this conversation.
            self._wanted_friend = fid
            return
        f = self._svc.friend(fid)
        self._wanted_friend = ""
        if f is None:
            self._stack.setCurrentIndex(_PAGE_LIST)
            return
        if fid == self._friend_id and self._stack.currentIndex() == _PAGE_MESSAGES:
            # Already there (a notification for the chat on show): what is typed, the
            # pictures opened and the "new" marks stay; it is only looked at again.
            self._sync_attention()
            self._scroll_bottom()
            self._compose.setFocus()
            return
        self._friend_id = fid
        # Which were new when the person got here: they are read from now on,
        # but still marked new on the page itself.
        self._new_ids = {m.id for m in self._svc.messages(fid) if not m.read}
        self._msgs_sig = None
        self._close_videos()
        self._restore_previews(fid)
        self._compose.clear()
        self._fill_messages(stick=True)
        self._stack.setCurrentIndex(_PAGE_MESSAGES)
        self._sync_attention()
        self._sync_bar()
        self._update_compose()
        self._compose.setFocus()

    def _back(self):
        self._close_videos()
        self._friend_id = ""
        self._stack.setCurrentIndex(_PAGE_LIST)
        self._sync_attention()
        self._refresh()

    def _on_lock_changed(self, locked: bool):
        if locked:
            self._friend_id = ""
            self._show_lock()
        else:
            self._enter()
            self._refresh()

    # ── View of the service ──────────────────────────────────────────────

    def _refresh(self):
        locked = self._svc.locked
        self._sync_bar()
        if locked:
            if self._stack.currentIndex() != _PAGE_LOCK:
                self._show_lock()
            return
        self._code_edit.setText(self._svc.code)
        name = self._svc.my_name
        self._name_lbl.setText(t("p2p.friends_my_name", name=name) if name
                               else t("p2p.friends_no_name"))
        self._fill_friends()
        self._refresh_groups()
        if self._stack.currentIndex() == _PAGE_MESSAGES:
            if self._svc.friend(self._friend_id) is None:
                self._back()            # the friend was removed under it
            else:
                self._fill_messages()
                self._update_compose()

    def _fill_friends(self):
        """The list, written out again only when something it shows changed (a message
        or any other service signal that leaves it as it was would otherwise rebuild
        every row and drop the keyboard focus from the one the person is on)."""
        people = self._svc.friends()
        self._intro_lbl.setVisible(not people)
        sig = tuple((f.id, f.display, f.connected, self._svc.unread(f.id), self._svc.is_online(f.id))
                    for f in people)
        if sig == self._list_sig:
            return
        self._list_sig = sig
        # (the window's focus widget, not hasFocus(): it is the same whether or not the
        # window is the active one at this moment)
        on = self._window.focusWidget()
        focused = next((fid for fid, row in self._rows.items()
                        if row is on or (on is not None and row.isAncestorOf(on))), None)
        _clear(self._friends_box)
        self._rows = {}
        if not people:
            empty = QLabel(t("p2p.friends_empty"))
            empty.setStyleSheet(self._muted)
            empty.setWordWrap(True)
            self._friends_box.addWidget(empty)
            return
        for f in people:
            self._rows[f.id] = row = self._friend_row(f)
            self._friends_box.addWidget(row)
        if focused in self._rows:
            # (once the new row is on screen: focus asked of a widget not yet shown is dropped)
            self._later(0, lambda fid=focused: self._focus_row(fid))

    def _focus_row(self, fid: str):
        row = self._rows.get(fid)
        if row is not None:
            row.setFocus()

    def _friend_row(self, f) -> QWidget:
        """One friend: click it for the chat; the ⋯ on it renames or removes them.
        Tokens they send are in the chat, as cards, with the rest of what they say."""
        row = _FriendRow()
        row.clicked.connect(lambda fid=f.id: self.show_friend(fid))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(6)
        # Green when they are there, red when not (their words are in the tooltip and the accessible name)
        online = self._svc.is_online(f.id)
        presence = self._svc.presence_tip(f.id)
        dot = PresenceDot(online, scaled(10, self))
        dot.setAccessibleName(presence)
        lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        name = QLabel(f.display)
        name.setTextFormat(Qt.TextFormat.PlainText)         # (their words, whatever they are)
        name.setStyleSheet(f"font-weight:600;{self._ink}")
        # Takes the width it is given, however long the name: a label's text would
        # otherwise set the width of the whole list.
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(name)
        status = QLabel(("✓ " + t("p2p.friends_status_connected")) if f.connected
                        else ("⏳ " + t("p2p.friends_status_waiting")))
        status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.setStyleSheet(self._muted)
        col.addWidget(status)
        lay.addLayout(col, 1)
        unread = self._svc.unread(f.id)
        badge = CountBadge()
        badge.set_count(unread)
        lay.addWidget(badge)
        # (the labels and the badge let a click through to the row, so the row is where
        # their tooltips are)
        tip = presence + "\n" + (t("p2p.friends_status_tip_connected") if f.connected
                                 else t("p2p.friends_status_tip_waiting"))
        if unread:
            tip += "\n" + t("p2p.friends_unread_tip", count=unread)
        row.setToolTip(tip)
        for w in (name, status, badge):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        mark, css = _dots(self)
        more = self._icon_button(mark, t("p2p.friends_row_menu_tip"), None, css)
        more.clicked.connect(lambda _=False, fid=f.id, btn=more: self._show_friend_menu(fid, btn))
        lay.addWidget(more)
        return row

    def _show_friend_menu(self, fid: str, btn: QPushButton):
        menu = QMenu(self)
        menu.addAction(t("p2p.friends_rename"), lambda: self._rename(fid))
        menu.addAction(t("p2p.friends_remove"), lambda: self._remove(fid))
        with self._modal():
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _at_bottom(self) -> bool:
        bar = self._msgs_scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_bottom(self):
        bar = self._msgs_scroll.verticalScrollBar()
        self._later(0, lambda: bar.setValue(bar.maximum()))

    def _fill_messages(self, stick: bool = False):
        """The conversation, oldest first like any chat. Rebuilt only when
        something about it changed (a message, how far one of mine got, a token
        running out); stays at the bottom when it was there, or when *stick*."""
        f = self._svc.friend(self._friend_id)
        if f is None:
            return
        msgs = sorted(self._svc.messages(self._friend_id), key=lambda m: m.received_at)
        sig = (self._friend_id, tuple((m.id, m.state(), m.read, m.delivery,
                                       (m.attachment or {}).get("why", ""), m.id in self._previews,
                                       self._stream_ready(m))
                                      for m in msgs), f.display, f.connected, self._svc.is_online(f.id))
        if sig == self._msgs_sig:
            return
        self._msgs_sig = sig
        was_at_bottom = stick or self._at_bottom()
        for player in self._videos.values():        # (taken out so the rebuild does not delete it)
            player.hide()
            player.setParent(None)
        _clear(self._msgs_box)
        self._msg_labels = {}
        self._xfer = {}
        self._msgs_title.setText(t("p2p.friends_messages_title", name=f.display))
        online = self._svc.is_online(f.id)
        self._msgs_dot.set_online(online)
        self._msgs_dot.setToolTip(self._svc.presence_tip(f.id))
        self._msgs_dot.setAccessibleName(self._svc.presence_tip(f.id))
        self._msgs_status.setText(
            t("p2p.friends_online" if online else "p2p.friends_offline") + "  ·  "
            + (("✓ " + t("p2p.friends_status_connected")) if f.connected
               else ("⏳ " + t("p2p.friends_status_waiting"))))
        self._clear_btn.setEnabled(bool(msgs))
        if not msgs:
            empty = QLabel(t("p2p.friends_messages_empty", name=f.display))
            empty.setWordWrap(True)
            empty.setStyleSheet(self._muted)
            self._msgs_box.addWidget(empty)
            self._reap_videos()
            return
        for m in msgs:
            if m.kind == "token":
                self._msgs_box.addWidget(self._token_card(m, f))
            elif m.kind == "file":
                self._msgs_box.addWidget(self._file_card(m, f))
            elif m.kind == "ask":
                self._msgs_box.addWidget(self._ask_card(m, f))
            elif m.kind == "gap":
                note = QLabel(t("p2p.friends_gap", count=m.missed))
                note.setAlignment(Qt.AlignmentFlag.AlignCenter)
                note.setStyleSheet(f"color:{palette('text_muted')};font-style:italic;")
                self._msgs_box.addWidget(note)
            else:
                self._msgs_box.addWidget(self._bubble(m, f))
        self._reap_videos()
        self._update_countdowns()
        if was_at_bottom:
            self._scroll_bottom()

    def _stream_ready(self, m) -> bool:
        """Is this a video attachment that is arriving and whose start is in, so
        that it can be watched meanwhile?"""
        if m.kind != "file" or m.direction != "in" or m.state() != "receiving":
            return False
        st = self._svc.stream_for(m.friend_id, m.id)
        return bool(st is not None and st.ready())

    def _reap_videos(self):
        """A player whose card is gone from the page (the conversation was
        cleared, the attachment dropped out of the history), whose attachment is
        no longer arriving or saved (it was called off), or that Settings now
        forbid, is closed - which lets go of the file it was reading."""
        allowed = self._svc.media_allowed()         # (Settings may have been turned off meanwhile)
        for mid, player in list(self._videos.items()):
            entry = self._xfer.get(mid)
            live = entry is not None and entry[0].state() in ("receiving", "saved")
            if player.parent() is None or not allowed or not live:
                self._close_video(mid)

    def _bubble(self, m, f) -> QWidget:
        """One text: mine on the right (accent), theirs on the left. The words
        are read for the few marks core.p2p.markup knows and shown as the HTML it
        writes from them - never as anything the friend wrote - and an address in
        them is a link that asks before it opens."""
        mine = m.direction == "out"
        card = QFrame()
        card.setObjectName("p2p_friend_bubble")
        fg = palette("accent_text") if mine else palette("text")
        bg = palette("accent") if mine else palette("bg_elevated")
        card.setStyleSheet(f"#p2p_friend_bubble {{ background-color: {bg}; border-radius: 10px; }}")
        card.setMaximumWidth(scaled(440, self))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)
        parsed = markup.parse(m.body)
        link = fg if mine else palette("accent")
        body = QLabel(markup.to_html(parsed, fg=fg, muted=_mix(bg, fg, 0.65), link=link,
                                     code_bg=_mix(bg, fg, 0.18), code_fg=fg))
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(False)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        pal = body.palette()
        pal.setColor(QPalette.ColorRole.Link, QColor(link))
        pal.setColor(QPalette.ColorRole.LinkVisited, QColor(link))
        body.setPalette(pal)
        body.setStyleSheet(f"color:{fg};")
        links, name = parsed.links, f.display
        body.linkActivated.connect(lambda href, links=links, name=name: self._open_link(href, links, name))
        lay.addWidget(body)
        meta = QLabel()
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        when = time.strftime("%H:%M", time.localtime(m.received_at))
        mark, tip = "", ""
        if mine:
            state = m.state()
            mark = {"pending": "⏳", "sent": "✓", "delivered": "✓✓"}.get(state, "")
            tip = t(f"p2p.friends_delivery_{state}", name=f.display) if state in (
                "pending", "sent", "delivered") else ""
        fresh = "  ·  " + t("p2p.friends_message_new") if m.id in self._new_ids else ""
        meta.setText(f"{when}{fresh}  {mark}".rstrip())
        meta.setToolTip(tip)
        lay.addWidget(meta)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if mine:
            row.addStretch(1)
            row.addWidget(card)
        else:
            row.addWidget(card)
            row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _token_card(self, m, f) -> QWidget:
        card = QFrame()
        card.setObjectName("p2p_friend_msg")
        card.setStyleSheet(
            f"#p2p_friend_msg {{ border: 1px solid {palette('border_hover')}; border-radius: 6px; }}")
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 8, 10, 8)
        col = QVBoxLayout()
        col.setSpacing(0)
        title = QLabel("🎫 " + t("p2p.friends_message_token", name=f.display)
                       + (("  ·  " + t("p2p.friends_message_new")) if m.id in self._new_ids else ""))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setStyleSheet(f"font-weight:600;{self._ink}")
        col.addWidget(title)
        sub_lbl = QLabel()
        sub_lbl.setStyleSheet(self._muted)
        col.addWidget(sub_lbl)
        row.addLayout(col, 1)
        self._msg_labels[m.id] = (sub_lbl, m)
        if m.state() == "active":
            send = QPushButton(t("p2p.friends_message_send"))
            send.setObjectName("primary_btn")
            send.clicked.connect(lambda _=False, token=m.token: self._use_token(token))
            row.addWidget(send)
        return card

    # ── Requests for a token ─────────────────────────────────────────────

    def _ask_card(self, m, f) -> QWidget:
        """A request for a token, either way. Theirs ("X wants to send you Y"): Accept
        hands them a token - nothing is sent before - and Decline tells them no. Mine
        ("you asked X for a token to send Y"): where it is, and Cancel takes it back."""
        incoming = m.direction == "in"
        state = m.state()
        card = QFrame()
        card.setObjectName("p2p_friend_msg")
        card.setStyleSheet(
            f"#p2p_friend_msg {{ border: 1px solid {palette('border_hover')}; border-radius: 6px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        key = "p2p.friends_ask_in_title" if incoming else "p2p.friends_ask_out_title"
        title = QLabel(("🎫 " if incoming else "📨 ") + t(key, name=f.display, label=m.body)
                       + (("  ·  " + t("p2p.friends_message_new")) if m.id in self._new_ids else ""))
        title.setTextFormat(Qt.TextFormat.PlainText)            # (their words: never markup)
        title.setWordWrap(True)
        title.setStyleSheet(f"font-weight:600;{self._ink}")
        lay.addWidget(title)
        sub_lbl = QLabel()
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(self._muted)
        lay.addWidget(sub_lbl)
        self._msg_labels[m.id] = (sub_lbl, m)
        mid, fid = m.id, m.friend_id
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        if incoming and state == "active":
            accept = QPushButton(t("p2p.friends_file_accept"))
            accept.setObjectName("primary_btn")
            accept.setToolTip(t("p2p.friends_ask_accept_tip", name=f.display))
            accept.clicked.connect(lambda _=False: self._accept_request(fid, mid))
            buttons.addWidget(accept)
            decline = QPushButton(t("p2p.friends_file_decline"))
            decline.clicked.connect(lambda _=False: self._decline_request(fid, mid))
            buttons.addWidget(decline)
        elif not incoming and state == "waiting":
            cancel = QPushButton(t("p2p.friends_file_cancel"))
            cancel.setToolTip(t("p2p.friends_ask_cancel_tip"))
            cancel.clicked.connect(lambda _=False: self._withdraw_request(fid, mid))
            buttons.addWidget(cancel)
        buttons.addStretch(1)
        if buttons.count() > 1:
            lay.addLayout(buttons)
        return card

    def _ask_tail(self, m) -> str:
        """Where a request is, in words (the countdown while it can still be answered)."""
        state = m.state()
        key = f"p2p.friends_ask_{'in' if m.direction == 'in' else 'out'}_state_{state}"
        return t(key, time=format_remaining(m.remaining()))

    def _request_failed(self, e) -> None:
        """Say why a request could not be accepted (the reasons the service gives)."""
        code = str(e).replace(" ", "_")
        for prefix in ("p2p.friends_ask_err_", "p2p.friends_file_err_"):
            text = t(prefix + code)
            if text != prefix + code:
                break
        else:
            text = t("p2p.friends_ask_err_error")
        self._say(text, warn=True)

    def _accept_request(self, fid: str, rid: str):
        try:
            self._svc.accept_request(fid, rid)
        except FriendError as e:
            self._request_failed(e)
        self._request_done(fid)

    def _decline_request(self, fid: str, rid: str):
        self._svc.decline_request(fid, rid)
        self._request_done(fid)

    def _withdraw_request(self, fid: str, rid: str):
        self._svc.withdraw_request(fid, rid)
        self._request_done(fid)

    def _request_done(self, fid: str):
        self._msgs_sig = None
        if self._friend_id == fid and not self._svc.locked:
            self._fill_messages()

    # ── Attachments ──────────────────────────────────────────────────────

    def _file_card(self, m, f) -> QWidget:
        """An attachment, either way: its name and size, where it is, and the
        buttons that make sense for that (Accept / Decline, Cancel, Show in
        folder, Show picture). Mine on the right, theirs on the left."""
        att = m.attachment or {}
        mine = m.direction == "out"
        state = att.get("state", "failed")
        fg = palette("accent_text") if mine else palette("text")
        bg = palette("accent") if mine else palette("bg_elevated")
        card = QFrame()
        card.setObjectName("p2p_friend_file")
        card.setStyleSheet(f"#p2p_friend_file {{ background-color: {bg}; border-radius: 10px; }}")
        card.setMaximumWidth(scaled(440, self))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 6)
        lay.setSpacing(3)

        count = int(att.get("count") or 1)
        # (a name is only ever text: never markup, never a path)
        title = QLabel("📎 " + str(att.get("label", "")))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title.setStyleSheet(f"font-weight:700;color:{fg};")
        lay.addWidget(title)
        size = _a.human_size(att.get("size", 0))
        sub_text = size if count <= 1 else t("p2p.friends_file_files", count=count) + "  ·  " + size
        if m.id in self._new_ids:
            sub_text += "  ·  " + t("p2p.friends_message_new")
        sub_lbl = QLabel(sub_text)
        sub_lbl.setStyleSheet(f"color:{fg};font-size:{scaled(11, self)}px;")
        lay.addWidget(sub_lbl)

        status = QLabel()
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        status.setStyleSheet(f"color:{fg};")
        lay.addWidget(status)
        bar = QProgressBar()
        bar.setRange(0, 100)
        # (the application style draws a bar four pixels high with no readable text:
        # a transfer's percentage is the point of it)
        style_transfer_bar(bar, 16, fg)
        bar.setVisible(False)
        lay.addWidget(bar)
        speed = QLabel()
        speed.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        speed.setVisible(False)
        lay.addWidget(speed)

        pixmap = self._previews.get(m.id)
        if pixmap is not None and not self._preview_still_there(att):
            self._previews.pop(m.id, None)              # (the file is gone, or is not the one that came)
            pixmap = None
        room = self._media_width()
        if pixmap is not None:
            pic = ClickablePicture(fit_pixmap(pixmap, room))
            pic.clicked.connect(lambda mid=m.id, att=att: self._fullscreen_picture(mid, att))
            lay.addWidget(pic)
        player = self._videos.get(m.id)
        if player is not None:
            player.fit_to(room)
            lay.addWidget(player)
            player.show()

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        def add(text, slot, primary=False):
            b = QPushButton(text)
            if primary:
                b.setObjectName("primary_btn")
            b.clicked.connect(lambda _=False: slot())
            buttons.addWidget(b)
            return b
        mid, fid = m.id, m.friend_id
        if not mine:
            if state == "offered":
                accept = add(t("p2p.friends_file_accept"), lambda: self._accept_file(mid), primary=True)
                accept.setToolTip(t("p2p.friends_file_accept_tip", name=f.display))
                add(t("p2p.friends_file_decline"), lambda: self._decline_file(mid))
            elif state == "waiting":
                add(t("p2p.friends_file_cancel"), lambda: self._cancel_file(mid))
            elif state == "receiving":
                add(t("p2p.friends_file_cancel"), lambda: self._cancel_file(mid))
                if count == 1 and mid not in self._videos and _video_available():
                    stream = self._svc.stream_for(fid, mid)
                    if stream is not None and stream.ready():
                        watch = add(t("p2p.friends_file_stream"), lambda: self._play_stream(mid))
                        watch.setToolTip(t("p2p.friends_file_stream_tip"))
            elif state == "saved":
                where = self._saved_path(att)
                if where is not None:
                    add(t("p2p.friends_file_show"), lambda: self._show_in_folder(att))
                    if count == 1 and self._media_ok(where, att):
                        if mid not in self._previews and not _a.image_problem(where):
                            add(t("p2p.friends_file_picture"), lambda: self._show_picture(mid, att))
                        if mid not in self._videos and not _a.video_problem(where) and _video_available():
                            play = add(t("p2p.friends_file_video"), lambda: self._play_video(mid, att))
                            play.setToolTip(t("p2p.friends_file_video_tip"))
        elif state in ("offered", "sending"):
            add(t("p2p.friends_file_cancel"), lambda: self._cancel_file(mid))
        if mine and count == 1:
            # What I offered can be looked at too, not only what I was sent: the
            # one picture or video, from where it is on this PC, under the same
            # checks (Settings, still exactly the size offered, a plain file).
            where = self._source_path(att)
            if where is not None and self._media_ok(where, att):
                if mid not in self._previews and not _a.image_problem(where):
                    add(t("p2p.friends_file_picture"), lambda: self._show_picture(mid, att))
                if mid not in self._videos and not _a.video_problem(where) and _video_available():
                    play = add(t("p2p.friends_file_video"), lambda: self._play_video(mid, att))
                    play.setToolTip(t("p2p.friends_file_video_tip"))
        if pixmap is not None:
            add(t("p2p.friends_fullscreen"), lambda: self._fullscreen_picture(mid, att))
        buttons.addStretch(1)
        lay.addLayout(buttons)

        meta = QLabel()
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        when = time.strftime("%H:%M", time.localtime(m.received_at))
        mark, tip = "", ""
        if mine:
            mark = {"pending": "⏳", "sent": "✓", "delivered": "✓✓"}.get(m.delivery, "")
            tip = t(f"p2p.friends_delivery_{m.delivery}", name=f.display) if m.delivery in (
                "pending", "sent", "delivered") else ""
        meta.setText(f"{when}  {mark}".rstrip())
        meta.setToolTip(tip)
        lay.addWidget(meta)
        self._xfer[mid] = (m, status, bar, speed)
        self._refresh_file(mid)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if mine:
            row.addStretch(1)
            row.addWidget(card)
        else:
            row.addWidget(card)
            row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _file_status(self, m, f, info) -> str:
        """The line of words under an attachment: where it is and why."""
        att = m.attachment or {}
        state, why, name = att.get("state", "failed"), att.get("why", ""), f.display
        side = "out" if m.direction == "out" else "in"
        if info and "prep" in info:             # (mine: hashing the files, however big they are)
            return t("p2p.friends_file_out_preparing", name=name, pct=int(info["prep"]))
        if info and info.get("preparing"):      # (theirs: the sender said they are working on it)
            return t("p2p.friends_file_in_preparing", name=name)
        key = f"p2p.friends_file_{side}_{state}"
        text = t(key, name=name, path=_breakable(att.get("dir", "")),
                 time=format_remaining((info or {}).get("wait", 0)))
        if text == key:
            text = ""                       # (a combination that has no words)
        idle = float((info or {}).get("idle", 0) or 0)
        if state == "receiving" and idle >= _IDLE_HINT_SECONDS:
            # (nothing is cut off for this - there is no time limit on a connection)
            text += "\n" + t("p2p.friends_file_idle", name=name, minutes=max(1, int(idle // 60)))
            if (info or {}).get("tls"):
                text += " " + t("p2p.receive_idle_tls")
        # Nothing has ever come across: say why that can be, and what to try.
        never_connected = ((state == "receiving" and idle >= _NO_CONNECTION_HINT_SECONDS
                            and not (info or {}).get("pct"))
                           or (state == "sending"
                               and float((info or {}).get("unconnected", 0) or 0) >= _NO_CONNECTION_HINT_SECONDS))
        if never_connected:
            from core.p2p.transfer import tor_enabled
            hint = (info or {}).get("hint")
            if tor_enabled():
                text += "\n" + t("p2p.no_connection_tor")
            elif isinstance(hint, tuple) and len(hint) == 2:
                text += "\n" + t(hint[0], **hint[1])      # (which of the things that can be wrong it is)
            else:
                text += "\n" + t("p2p.no_connection_direct")
        if state in ("failed", "offered", "cancelled") and why:
            extra_key = f"p2p.friends_file_why_{why}"
            extra = t(extra_key, name=name, count=len(att.get("files") or []))
            if extra != extra_key:
                text += "\n" + extra
        return text

    def _refresh_file(self, mid: str):
        """Live words and figures for one attachment (called every second)."""
        entry = self._xfer.get(mid)
        if entry is None:
            return
        m, status, bar, speed = entry
        f = self._svc.friend(self._friend_id)
        if f is None:
            return
        info = self._svc.transfer_info(m.friend_id, mid)
        status.setText(self._file_status(m, f, info))
        if info and "prep" in info:
            bar.setValue(int(info["prep"]))
            bar.setVisible(True)
            speed.setVisible(False)
        elif info and "pct" in info:
            bar.setValue(int(info["pct"]))
            bar.setVisible(True)
            from core.p2p.transfer import format_rate
            mbs, mbps = format_rate(info.get("rate", 0))
            speed.setText(mbs)
            speed.setVisible(True)
            # Megabits (as the line is quoted) on whichever figure the pointer
            # rests on, not only on the small speed text.
            for w in (speed, bar, status):
                w.setToolTip(mbps)
        else:
            bar.setVisible(False)
            speed.setVisible(False)

    @staticmethod
    def _source_path(att) -> "Path | None":
        """Where the one picture or video I offered is on this PC, if it is
        still there as a plain file (a link, or something else now, is not)."""
        src = att.get("src") if isinstance(att, dict) else ""
        if not src:
            return None
        try:
            path = Path(src)
            return path if path.is_file() and not path.is_symlink() else None
        except (OSError, ValueError):
            return None

    def _preview_path(self, att) -> "Path | None":
        """The file a preview or a player is shown for: the received copy, or
        for an offer of mine the file I offered."""
        return self._saved_path(att) or self._source_path(att)

    def _saved_path(self, att) -> "Path | None":
        """The first received file of an attachment, if it is still there and
        really is inside the folder it was saved to."""
        folder, files = att.get("dir"), att.get("files") or []
        if not folder or not files:
            return None
        try:
            base = Path(folder).resolve()
            path = (base / files[0]).resolve()
            path.relative_to(base)
            return path if path.is_file() else None
        except (OSError, ValueError):
            return None

    def _accept_file(self, mid: str):
        try:
            self._svc.accept_file(self._friend_id, mid)
        except FriendError as e:
            code = str(e)
            key = f"p2p.friends_file_err_{code.replace(' ', '_')}"
            text = t(key)
            self._say(text if text != key else t("p2p.friends_file_err_error"), warn=True)
        self._msgs_sig = None
        self._fill_messages()

    def _decline_file(self, mid: str):
        self._svc.decline_file(self._friend_id, mid)
        self._msgs_sig = None
        self._fill_messages()

    def _cancel_file(self, mid: str):
        """Cancel on a card. Stopping something that is arriving or leaving asks
        first (it is called off for the other end too); giving up waiting or taking
        back an offer does not."""
        entry = self._xfer.get(mid)
        fid = self._friend_id           # (fixed now: the page may be another friend's once it is answered)
        f = self._svc.friend(fid)
        if entry is not None and f is not None:
            m = entry[0]
            state, incoming = m.state(), m.direction == "in"
            ask = None
            if state == "receiving" and incoming:
                ask = t("p2p.friends_file_cancel_confirm", name=f.display)
            elif state == "sending" and not incoming:
                ask = t("p2p.friends_file_cancel_send_confirm", name=f.display)
            if ask is not None:
                from ui.modal_helpers import question_window_modal
                with self._modal():
                    reply = question_window_modal(
                        self, t("p2p.friends_title"), ask,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes or self._svc.locked:
                    return
            self._close_video(mid)              # (a file being watched is let go of first)
        self._svc.cancel_file(fid, mid)
        self._msgs_sig = None
        if self._friend_id == fid:
            self._fill_messages()

    def _saved_top(self, att) -> "Path | None":
        """What an attachment arrived as, for "Show in folder": the file itself, or - for a folder
        or several files in one - the top folder it came in, not the deepest one the first file
        happens to be in. None when it is gone or is not inside the folder it was saved to."""
        folder, files = att.get("dir"), att.get("files") or []
        if not folder or not files:
            return None
        try:
            base = Path(folder).resolve()
            parts = Path(files[0]).parts
            if not parts:
                return None
            top = (base / parts[0]).resolve()
            top.relative_to(base)
            return top if top.exists() else None
        except (OSError, ValueError):
            return None

    def _show_in_folder(self, att):
        """Open the folder a received file is in, with the file (or the folder it came in)
        selected. The file itself is never opened or run from here."""
        path = self._saved_top(att) or self._saved_path(att)
        if path is None:
            return
        from ui.helpers import reveal_in_file_manager
        reveal_in_file_manager(path)

    def _media_ok(self, path, att) -> bool:
        """May this received file be shown or played here: Settings allow it, and
        it is still exactly the size that was offered (a file swapped for another
        since it arrived is left alone)."""
        if not self._svc.media_allowed():
            return False
        try:
            return Path(path).stat().st_size == int(att.get("size", -1))
        except OSError:
            return False

    def _play_video(self, mid: str, att):
        """The person pressed Play video: one player, for this one file, under
        the rules in ui.widgets.safe_video."""
        path = self._preview_path(att)
        if path is None or not self._media_ok(path, att) or _a.video_problem(path):
            self._say(t("p2p.friends_video_failed"), warn=True)
            return
        try:
            from ui.widgets.safe_video import SafeVideoPlayer
            player = SafeVideoPlayer(path)
        except Exception:
            logger.debug("P2P friends: a video player could not start", exc_info=True)
            self._say(t("p2p.friends_video_failed"), warn=True)
            return
        self._close_videos()                    # one at a time
        self._videos[mid] = player
        player.closed.connect(lambda mid=mid: self._video_closed(mid))
        self._msgs_sig = None
        self._fill_messages()

    def _play_stream(self, mid: str):
        """Watch a video that is still arriving: the player is given a stream
        that answers only with pieces that have arrived and match the torrent
        (see ui.widgets.stream_device); the download carries on behind it."""
        stream = self._svc.stream_for(self._friend_id, mid)
        if stream is None or not stream.ready():
            self._say(t("p2p.friends_video_failed"), warn=True)
            return
        try:
            from ui.widgets.safe_video import SafeVideoPlayer
            player = SafeVideoPlayer(None, stream=stream)
        except Exception:
            logger.debug("P2P friends: a stream player could not start", exc_info=True)
            self._say(t("p2p.friends_video_failed"), warn=True)
            return
        self._close_videos()
        self._videos[mid] = player
        player.closed.connect(lambda mid=mid: self._video_closed(mid))
        self._msgs_sig = None
        self._fill_messages()

    def _video_closed(self, mid: str):
        self._videos.pop(mid, None)
        self._msgs_sig = None
        self._fill_messages()

    def _close_video(self, mid: str):
        player = self._videos.pop(mid, None)
        if player is not None:
            try:
                player.closed.disconnect()
            except (RuntimeError, TypeError):
                pass
            player.close_player()

    def _close_videos(self):
        # (a picture on show full screen is let go of at the same moments: the list locking, another
        # friend's page, the panel going - it shows what was in the chat)
        self._close_fullscreen_picture()
        for mid in list(self._videos):
            self._close_video(mid)

    def _media_width(self) -> int:
        """The room a picture or a video has on a card: the page's width less the scroll bar (always
        counted, so that it appearing does not push the picture out) and the card's own margins,
        and never more than a card is allowed to be."""
        view = self._msgs_scroll.viewport().width()
        by_panel = self.width() - 31                    # (the panel's own margins and edge)
        if view <= 0 or view > by_panel:                # (not laid out yet: what the panel itself says)
            view = by_panel
        bar = self._msgs_scroll.verticalScrollBar().sizeHint().width() or scaled(14, self)
        card = min(view - bar - 4, scaled(440, self))
        return max(scaled(100, self), card - 20)

    def _refit_media(self):
        """The panel's width changed: the pictures and players are made to the new room."""
        if self._friend_id and self._stack.currentIndex() == _PAGE_MESSAGES and not self._svc.locked:
            self._msgs_sig = None
            self._fill_messages()

    def _fullscreen_picture(self, mid: str, att):
        """The person asked for this picture full screen: decoded again at the size of the screen,
        under the same limits as the preview."""
        path = self._preview_path(att)
        if path is not None and not self._media_ok(path, att):
            path = None
        self._close_fullscreen_picture()
        win = None
        if path is not None:
            from ui.widgets.media_fullscreen import open_picture_fullscreen
            win = open_picture_fullscreen(path, self)
        if win is None:
            self._say(t("p2p.friends_file_picture_failed"), warn=True)
            return
        self._fs_picture = win
        win.closed.connect(lambda w=win: self._fullscreen_closed(w))

    def _fullscreen_closed(self, win):
        if self._fs_picture is win:
            self._fs_picture = None

    def _close_fullscreen_picture(self):
        win, self._fs_picture = self._fs_picture, None
        if win is not None:
            try:
                win.close()
            except RuntimeError:
                pass                    # (already gone)

    def _preview_still_there(self, att) -> bool:
        """Is the file a picture was opened for still where it was, and the one it was? (A
        preview stays for as long as it is: through the end of the transfer, and when the chat is
        left and come back to.)"""
        path = self._preview_path(att)
        return path is not None and self._media_ok(path, att)

    def _restore_previews(self, fid: str):
        """The pictures that were opened in this chat are opened again (decoded again, under the same
        limits), the ones whose file is still where it was; the latest few."""
        self._previews = {}
        wanted = self._svc.previewed()
        if not wanted:
            return
        from ui.widgets.safe_image import load_preview
        for m in reversed(list(self._svc.messages(fid))):
            if len(self._previews) >= 12:
                break
            if m.kind != "file" or m.id not in wanted:
                continue
            att = m.attachment or {}
            path = self._preview_path(att)
            if path is None or not self._media_ok(path, att):
                continue
            pixmap = load_preview(path)
            if pixmap is not None:
                self._previews[m.id] = pixmap

    def _show_picture(self, mid: str, att):
        """The person asked to see this picture: decode it (and only it) under
        the limits of ui.widgets.safe_image."""
        from ui.widgets.safe_image import load_preview
        path = self._preview_path(att)
        if path is not None and not self._media_ok(path, att):
            path = None
        pixmap = load_preview(path) if path is not None else None
        if pixmap is None:
            self._say(t("p2p.friends_file_picture_failed"), warn=True)
            return
        self._previews[mid] = pixmap
        self._svc.note_preview(mid)
        self._msgs_sig = None
        self._fill_messages()

    def _attach(self):
        """+: pick a file or folder, see exactly what would be offered, offer it.
        Nothing is sent now - the friend gets a card and decides."""
        fid = self._friend_id
        f = self._svc.friend(fid)
        if f is None:
            return
        with self._modal():
            if not self._svc.can_offer(fid):
                self._say(t("p2p.friends_compose_full", name=f.display), warn=True)
                return
            from ui.dialogs.p2p_add_files_dialog import pick_paths
            paths = pick_paths(self, t("p2p.friends_attach_pick"))
            if paths:
                self._offer_paths(paths, fid)

    # ── Dropping files ───────────────────────────────────────────────────

    def _can_take_drop(self) -> bool:
        """Only a friend's open conversation takes files: not the list, not the
        lock screen."""
        return (self._can_take_group_drop()
                or (not self._svc.locked and self._stack.currentIndex() == _PAGE_MESSAGES
                    and self._svc.friend(self._friend_id) is not None))

    def _show_drop_hint(self, on: bool):
        if self._stack.currentIndex() == PAGE_GROUP:
            self._show_group_drop_hint(on)
            return
        f = self._svc.friend(self._friend_id)
        if on and f is not None:
            self._drop_lbl.setText(t("p2p.friends_drop_hint", name=f.display))
        self._drop_lbl.setVisible(bool(on))

    def _refuse_drop(self, event):
        """Taken and answered with "no": a drop that is only IGNORED goes on to the
        parent, and this panel lies over a page that does its own thing with files."""
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()

    def dragEnterEvent(self, event):
        if self._can_take_drop() and dropped_paths(event.mimeData()):
            event.acceptProposedAction()
            self._show_drop_hint(True)
        else:
            self._refuse_drop(event)

    def dragMoveEvent(self, event):
        if self._can_take_drop() and dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            self._refuse_drop(event)

    def dragLeaveEvent(self, event):
        self._show_drop_hint(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._show_drop_hint(False)
        paths = dropped_paths(event.mimeData())
        if paths and self._can_take_drop():
            event.acceptProposedAction()
            self._on_dropped(paths)
        else:
            self._refuse_drop(event)

    def _on_dropped(self, paths: list):
        """Something was dropped: ask about it once the drop itself is over (a
        window opened inside a drop would hold the other program up)."""
        if self._stack.currentIndex() == PAGE_GROUP:
            self._on_group_dropped(paths)
        elif self._can_take_drop():
            fid = self._friend_id
            self._later(0, lambda paths=list(paths): self._offer_paths(paths, fid))

    def _offer_paths(self, paths: list, fid: str = ""):
        """Offer these files and folders to *fid* (default: the friend whose
        conversation is open): the list of what would go (any file can be ticked off),
        a refusal for the kinds that are never sent, then the offer. Nothing is sent
        now - the friend gets a card and decides. The same whether they were picked with
        + or dropped. *fid* is fixed before the dialogs: by the time they are answered
        the page may be another friend's."""
        fid = fid or self._friend_id
        f = self._svc.friend(fid)
        if f is None or self._svc.locked or not paths or self._closing:
            return
        with self._modal():
            if not self._svc.can_offer(fid):
                self._say(t("p2p.friends_compose_full", name=f.display), warn=True)
                return
            sources = [Path(p) for p in paths]
            from ui.dialogs.p2p_send_confirm_dialog import P2pSendConfirmDialog
            confirm = P2pSendConfirmDialog(sources if len(sources) > 1 else sources[0], self)
            if confirm.exec() != QDialog.DialogCode.Accepted:
                return
            files = confirm.selected_files()
            if not files or self._svc.locked or self._svc.friend(fid) is None:
                return          # (nothing ticked, or the list was locked / the friend removed meanwhile)
            files = [(str(p), str(rel)) for p, rel in files]
            blocked = _a.blocked_names(files)
            if blocked:
                shown = "\n".join(blocked[:5]) + ("\n…" if len(blocked) > 5 else "")
                self._say(t("p2p.friends_attach_blocked", names=shown), warn=True)
                return
            label = sources[0].name if len(sources) == 1 else t("p2p.send_confirm_items", count=len(sources))
            try:
                self._svc.offer_file(fid, files, label)
            except FriendError as e:
                key = f"p2p.friends_attach_err_{str(e).replace(' ', '_')}"
                text = t(key, name=f.display)
                self._say(text if text != key else t("p2p.friends_attach_err_error"), warn=True)
                return
        self._msgs_sig = None
        if self._friend_id == fid:
            self._fill_messages(stick=True)
            self._update_compose()

    def _open_link(self, href: str, links: list, name: str):
        """A link in a message was clicked: ask first. *href* is only an index
        into the links core.p2p.markup checked - never an address."""
        m = _LINK_HREF.match(str(href))
        if m is None or int(m.group(1)) >= len(links):
            return
        from ui.dialogs.p2p_link_dialog import P2pLinkDialog
        with self._modal():
            P2pLinkDialog(links[int(m.group(1))], name, self).exec()

    def _update_net_status(self):
        status = self._svc.network_status()
        self._net_lbl.setToolTip("")
        if status == "proxy":
            self._net_lbl.setText("🛡 " + t("p2p.friends_net_proxy"))
            self._net_lbl.setStyleSheet(f"color:{palette('success')};")
        elif status == "blocked":
            self._net_lbl.setText("⚠ " + t("p2p.friends_net_blocked"))
            self._net_lbl.setStyleSheet(f"color:{palette('warning')};")
        elif status.startswith("private"):
            self._net_lbl.setToolTip(t("p2p.friends_net_private_tip"))
            info = self._svc.private_info()
            if status == "private_blocked":
                err = info.get("error") or ""
                self._net_lbl.setText("⚠ " + t("p2p.friends_net_private_blocked", error=err))
                self._net_lbl.setStyleSheet(f"color:{palette('warning')};")
            elif status == "private_starting":
                self._net_lbl.setText("⏳ " + t("p2p.friends_net_private_starting", percent=int(info.get("progress", 0))))
                self._net_lbl.setStyleSheet(f"color:{palette('text_secondary')};")
            else:
                links = int(info.get("links", 0))
                text = (t("p2p.friends_net_private", online=int(info.get("online", 0)), links=links)
                        if links else t("p2p.friends_net_private_idle"))
                self._net_lbl.setText("🛡 " + text)
                self._net_lbl.setStyleSheet(f"color:{palette('success')};")
        else:
            self._net_lbl.setText("")
        self._sync_net_visibility()

    def _on_tick(self):
        if self._modal_depth:
            self._svc.touch()           # (a dialog of ours is up: the person is in the middle of something)
        self._update_net_status()
        self._update_countdowns()
        page = self._stack.currentIndex()
        if page == _PAGE_MESSAGES:
            self._fill_messages()               # (rebuilt only if something about it changed)
        elif page == PAGE_GROUP:
            self._fill_group()
            self._update_group_files()
        elif page == PAGE_MEMBERS:
            self._fill_members()

    def _update_countdowns(self):
        for mid, (sub_lbl, m) in self._msg_labels.items():
            state = m.state()
            when = time.strftime("%H:%M:%S", time.localtime(m.received_at))
            if m.kind == "ask":
                tail = self._ask_tail(m)
            else:
                tail = (t("p2p.friends_message_state_active", time=format_remaining(m.remaining()))
                        if state == "active" else t(f"p2p.friends_message_state_{state}"))
            sub_lbl.setText(t("p2p.friends_message_received", time=when) + "  ·  " + tail)
        for mid in list(self._xfer):
            self._refresh_file(mid)

    # ── Writing ──────────────────────────────────────────────────────────

    def _update_compose(self):
        """The counter, whether Send is on, and what is in the way if not."""
        if self._svc.locked:
            return
        f = self._svc.friend(self._friend_id)
        body = self._compose.toPlainText()
        clean = _f.clean_text(body)
        used = _f.text_bytes(clean)
        too_long = used > _f.MAX_TEXT_BYTES
        self._counter.setText(t("p2p.friends_compose_counter", used=used, max=_f.MAX_TEXT_BYTES))
        self._counter.setStyleSheet(f"color:{palette('warning') if too_long else palette('text_secondary')};")
        room = bool(clean) and not too_long and self._svc.text_room(self._friend_id, clean)
        note = ""
        if too_long:
            note = t("p2p.friends_compose_too_long", used=used, max=_f.MAX_TEXT_BYTES)
        elif clean and not room:
            note = t("p2p.friends_compose_full", name=f.display if f is not None else "?")
        elif f is not None and not f.connected:
            note = t("p2p.friends_compose_waiting", name=f.display)
        self._send_btn.setEnabled(room)
        self._compose_msg.setText(note)
        self._compose_msg.setVisible(bool(note))

    def _send_text(self):
        if not self._send_btn.isEnabled():
            return
        try:
            self._svc.send_text(self._friend_id, self._compose.toPlainText())
        except FriendError as e:
            full = "full" in str(e)
            f = self._svc.friend(self._friend_id)
            self._compose_msg.setText(
                t("p2p.friends_compose_full", name=f.display if f is not None else "?") if full
                else t("p2p.friends_compose_too_long", used=_f.text_bytes(self._compose.toPlainText()),
                       max=_f.MAX_TEXT_BYTES))
            self._compose_msg.setVisible(True)
            return
        self._compose.clear()
        self._fill_messages(stick=True)
        self._update_compose()

    def _clear_conversation(self):
        fid = self._friend_id
        f = self._svc.friend(fid)
        if f is None:
            return
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.friends_clear_confirm", name=f.display),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if (reply == QMessageBox.StandardButton.Yes and not self._svc.locked
                and self._svc.friend(fid) is not None):
            self._svc.clear_conversation(fid)
            self._msgs_sig = None
            if self._friend_id == fid:
                self._fill_messages(stick=True)

    # ── Actions ──────────────────────────────────────────────────────────

    def _copy_code(self):
        QApplication.clipboard().setText(self._code_edit.text())
        self._copy_btn.setText(t("p2p.friends_copied"))
        self._copy_timer.start(1500)            # (again if pressed again; gone with the panel)

    def _add_friend(self):
        code = self._add_code.text().strip()
        if not code:
            return
        try:
            f = self._svc.add_friend(code, self._add_name.text().strip())
        except FriendError as e:
            msg = str(e)
            key = ("p2p.friends_add_own" if "own" in msg
                   else "p2p.friends_add_again" if "already" in msg
                   else "p2p.friends_add_invalid")
            self._add_msg.setText(t(key))
            return
        self._add_code.clear()
        self._add_name.clear()
        self._add_msg.setText(t("p2p.friends_added", name=f.display))

    def _rename(self, fid: str):
        f = self._svc.friend(fid)
        if f is None:
            return
        with self._modal():
            text, ok = QInputDialog.getText(self, t("p2p.friends_rename"),
                                            t("p2p.friends_rename_prompt"), text=f.alias or f.name)
        if ok and not self._svc.locked and self._svc.friend(fid) is not None:
            self._svc.rename_friend(fid, text)

    def _remove(self, fid: str):
        f = self._svc.friend(fid)
        if f is None:
            return
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.friends_remove_confirm", name=f.display),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if (reply == QMessageBox.StandardButton.Yes and not self._svc.locked
                and self._svc.friend(fid) is not None):
            self._svc.remove_friend(fid)

    def _use_token(self, token: str):
        """The person clicked a token a friend sent: pick what to send, go."""
        live = next((tk for tk in self._svc.tokens() if tk.token == token), None)
        if live is None:
            self._refresh()
            return
        self._svc.mark_read(live.friend_id)
        from ui.dialogs.p2p_send_flow import send_files_flow
        with self._modal():
            send_files_flow(self, token=token)
        if not self._closing and not self._svc.locked:
            self._refresh()

    # ── Lock ─────────────────────────────────────────────────────────────

    def _unlock(self):
        wait = self._svc.unlock_wait()
        if wait > 0:
            self._unlock_msg.setText(t("p2p.friends_unlock_wait", seconds=int(wait + 0.999)))
            return
        if self._svc.unlock(self._unlock_edit.text()):
            self._unlock_edit.clear()
            return          # lock_changed(False) brings the list up
        wait = self._svc.unlock_wait()
        self._unlock_msg.setText(t("p2p.friends_unlock_wait", seconds=int(wait + 0.999)) if wait > 0
                                 else t("p2p.friends_lock_wrong"))
        self._unlock_edit.selectAll()

    def _forgot(self):
        """Forgotten password: delete the list (after asking) and start again."""
        from ui.dialogs.p2p_friends_reset import reset_friend_list
        with self._modal():
            reset_friend_list(self, self._svc)      # (lock_changed brings the empty list up)

    def _password(self, title: str, label: str) -> tuple:
        with self._modal():
            text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Password)
        return (text if ok else None)

    def _say(self, text: str, warn: bool = False):
        from ui.modal_helpers import information_window_modal, warning_window_modal
        with self._modal():
            (warning_window_modal if warn else information_window_modal)(
                self, t("p2p.friends_title"), text)

    def _new_password(self, label_key: str, minimum: int):
        """Ask for a new password twice; None when cancelled or not usable."""
        with self._modal():
            first = self._password(t("p2p.friends_title"), t(label_key, n=minimum))
            if first is None:
                return None
            if len(first) < minimum:
                self._say(t("p2p.friends_password_short", n=minimum), warn=True)
                return None
            again = self._password(t("p2p.friends_title"), t("p2p.friends_password_again"))
            if again is None:
                return None
            if again != first:
                self._say(t("p2p.friends_password_mismatch"), warn=True)
                return None
            return first

    def _lock_password_flow(self):
        svc = self._svc
        with self._modal():
            current = ""
            if svc.has_lock_password:
                current = self._password(t("p2p.friends_title"), t("p2p.friends_lock_prompt_current"))
                if current is None or svc.locked:
                    return
                if not svc.store.check_lock_password(current):
                    self._say(t("p2p.friends_lock_wrong"), warn=True)
                    return
            new = self._new_password("p2p.friends_lock_prompt_new", _f.MIN_LOCK_PASSWORD)
            if new is None or svc.locked:       # (locked meanwhile: nothing may be written to it)
                return
            try:
                svc.set_lock_password(new, current)
            except FriendError as e:
                self._say(str(e), warn=True)
                return
            minutes = svc.autolock_minutes()
            self._say(t("p2p.friends_lock_set_done", minutes=minutes) if minutes
                      else t("p2p.friends_lock_set_done_manual"))
        self._sync_bar()

    def _remove_lock_flow(self):
        svc = self._svc
        with self._modal():
            current = self._password(t("p2p.friends_title"), t("p2p.friends_lock_prompt_current"))
            if current is None or svc.locked:
                return
            try:
                svc.remove_lock_password(current)
            except FriendError:
                self._say(t("p2p.friends_lock_wrong"), warn=True)
                return
        self._sync_bar()

    # ── Linking another PC ───────────────────────────────────────────────

    def _link_flow(self):
        """Write a link file the other PC can adopt."""
        from ui.widgets.file_pickers import pick_save_path
        from ui.modal_helpers import question_window_modal
        with self._modal():
            password = self._new_password("p2p.friends_link_prompt", _f.MIN_LINK_PASSWORD)
            if password is None:
                return
            path = pick_save_path(self, t("p2p.friends_link_title"), t("p2p.friends_link_filter"),
                                  default_name="savesync-friends" + _LINK_SUFFIX)
            if not path or self._svc.locked:
                return
            if not path.lower().endswith(_LINK_SUFFIX):
                path += _LINK_SUFFIX
            try:
                Path(path).write_bytes(self._svc.export_link(password))
            except (OSError, FriendError) as e:
                self._say(t("p2p.friends_link_failed", error=str(e)), warn=True)
                return
            reply = question_window_modal(
                self, t("p2p.friends_link_title"), t("p2p.friends_link_saved", path=path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes and not self._svc.locked:
                self._svc.retire_here()
                self._say(t("p2p.friends_link_retired"))

    def _import_flow(self):
        """Adopt the identity and friends of a link file made on another PC."""
        from ui.widgets.file_pickers import pick_file
        from ui.modal_helpers import question_window_modal
        with self._modal():
            path = pick_file(self, t("p2p.friends_import_pick"), t("p2p.friends_link_filter"))
            if not path:
                return
            try:
                blob = Path(path).read_bytes()
            except OSError as e:
                self._say(t("p2p.friends_link_failed", error=str(e)), warn=True)
                return
            password = self._password(t("p2p.friends_title"), t("p2p.friends_import_password"))
            if password is None:
                return
            try:
                _f.parse_link(blob, password)           # before anything is replaced
            except FriendError:
                self._say(t("p2p.friends_import_failed"), warn=True)
                return
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.friends_import_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes or self._svc.locked:
                return
            try:
                self._svc.import_link(blob, password)
            except FriendError:
                self._say(t("p2p.friends_import_failed"), warn=True)
                return
            self._say(t("p2p.friends_import_done", count=len(self._svc.friends())))
        self._back()            # (the list: whoever was on show is a friend of the old list)

    def _show_menu(self):
        menu = QMenu(self)
        svc = self._svc
        menu.addAction(t("p2p.friends_lock_change") if svc.has_lock_password
                       else t("p2p.friends_lock_set"), self._lock_password_flow)
        if svc.has_lock_password:
            menu.addAction(t("p2p.friends_lock_remove"), self._remove_lock_flow)
        menu.addSeparator()
        menu.addAction(t("p2p.friends_link_menu"), self._link_flow)
        menu.addAction(t("p2p.friends_import_menu"), self._import_flow)
        with self._modal():
            menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _teardown(self):
        """Let go of everything the panel held on the service for: balanced with what
        __init__ took."""
        try:
            self._tick.stop()
            self._copy_timer.stop()
            self._close_videos()
            for sig, slot in ((self._svc.changed, self._refresh),
                              (self._svc.lock_changed, self._on_lock_changed),
                              (QGuiApplication.instance().applicationStateChanged,
                               self._sync_attention)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._window.removeEventFilter(self)
            QApplication.instance().removeEventFilter(self)
            self._attending = False
            self._attending_group = False
            self._svc.set_viewing(None)
            self._svc.set_viewing_group(None)
            self._flush_grants(say=False)
        finally:
            if self._holding:                           # (balances acquire whatever else failed)
                self._holding = False
                self._svc.release(blocks_lock=False)
