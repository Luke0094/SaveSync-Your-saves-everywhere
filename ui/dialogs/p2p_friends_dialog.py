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
  buttons, and a note where some did not arrive - with a box to write in and a +
  to attach a file. Opened from the list, or straight from the notification of a
  new message. What is on show is read, which brings the badges down; what arrives
  while it is open is read as it comes, and is not announced again - which is why
  the panel closes itself when the window is minimized or put away to the tray: a
  message must not arrive unannounced into a chat nobody is looking at;
- the LOCK screen, when the list has been locked with a password - with a way
  back in for a forgotten one that deletes the list (ui.dialogs.p2p_friends_reset).

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
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, Qt, QTimer, QUrl, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFontMetrics, QKeySequence, QPalette, QShortcut,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.p2p import attachments as _a
from core.p2p import friends as _f
from core.p2p import markup
from core.p2p.friends import FriendError
from i18n import t
from ui.helpers import scaled
from ui.p2p_friends_service import format_remaining
from ui.styles.theme import palette
from ui.widgets.count_badge import CountBadge

logger = logging.getLogger(__name__)

_PAGE_LIST, _PAGE_MESSAGES, _PAGE_LOCK = 0, 1, 2
_LINK_SUFFIX = ".ssfriends"
_LINK_HREF = re.compile(r"^l(\d{1,2})$")
# A download that has had nothing arrive for this long is said to be stuck (it is not
# ended - a connection has no time limit - and can be cancelled).
_IDLE_HINT_SECONDS = 60


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
        self.setStyleSheet(
            "#p2p_friend_row { background: transparent; border-radius: 8px; }"
            f"#p2p_friend_row:hover, #p2p_friend_row:focus {{ background: {palette('bg_elevated')}; }}")

    def mousePressEvent(self, event):
        # (a press that is not taken goes to the parent, and the release with it)
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


class P2pFriendsPanel(QFrame):
    """The friend list as a drawer: it slides out of *anchor* (the sidebar) over the
    page beside it, and slides back in when closed. *host* is the widget both sit in.
    Built for one opening - closing deletes it."""
    closed = Signal()

    _WIDTH = 380            # design pixels (scaled), as much as the window can spare
    _MIN_WIDTH = 280
    _KEEP_VISIBLE = 120     # of the page beside it, in a window too narrow for all of it

    def __init__(self, service, host, anchor, *, open_friend: str = ""):
        super().__init__(host)
        self._svc = service
        self._host = host
        self._anchor = anchor
        self._slide = 0.0               # 0: tucked behind the sidebar, 1: all the way out
        self._anim = None
        self._closing = False
        self._friend_id = ""            # whose messages are on show ("" = the list)
        self._wanted_friend = open_friend   # to show once the list is open
        self._new_ids: set = set()      # messages that were unread when the page opened
        self._msg_labels: dict = {}         # message id -> (label, message)
        self._msgs_sig = None               # what the page shows now (to rebuild only on a change)
        self._xfer: dict = {}               # attachment id -> (message, status, bar, speed), for live figures
        self._previews: dict = {}           # attachment id -> the picture the person asked to see
        self._videos: dict = {}             # attachment id -> the video player the person opened
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
        self._svc.changed.connect(self._refresh)
        self._svc.lock_changed.connect(self._on_lock_changed)
        self._svc.acquire()
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._update_net_status()
        self._refresh()
        self._enter()
        self._place()

    def _apply_chrome(self):
        # The sidebar's own colours, so it reads as the sidebar growing sideways.
        self.setStyleSheet(
            f"#p2p_friends_panel {{ background-color: {palette('bg')}; "
            f"border-right: 1px solid {palette('border')}; }}")

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

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize and obj in (self._host, self._anchor):
            self._place()
        return False

    def _set_slide(self, value):
        self._slide = float(value)
        self._place()

    def _animate(self, to: float, then=None):
        anim = QVariantAnimation(self)
        anim.setStartValue(float(self._slide))
        anim.setEndValue(float(to))
        anim.setDuration(190 if to else 140)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic if to else QEasingCurve.Type.InCubic)
        anim.valueChanged.connect(self._set_slide)
        if then is not None:
            anim.finished.connect(then)
        self._anim = anim
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
        else:
            self.setFocus()

    @property
    def closing(self) -> bool:
        return self._closing

    def close_panel(self, animated: bool = True):
        """Slide back into the sidebar and go. The service is let go of at once, so a
        message that arrives while the panel is on its way out is announced as usual."""
        if self._closing:
            return
        self._closing = True
        self._teardown()
        if animated and self.isVisible():
            self._animate(0.0, self._finish)
        else:
            self._finish()

    def _finish(self):
        self._host.removeEventFilter(self)
        self._anchor.removeEventFilter(self)
        self.hide()
        self.closed.emit()
        self.deleteLater()

    def hideEvent(self, event):
        super().hideEvent(event)
        # The window went away under it (put away to the tray, say): nobody is
        # reading, so nothing may be marked read or kept quiet on their behalf.
        if not self._closing:
            win = self.window()
            if win is not None and (not win.isVisible() or win.isMinimized()):
                self.close_panel(animated=False)

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
        self._ink = f"color:{palette('text')};"
        self._muted = f"color:{palette('text_secondary')};"
        self._bold = f"font-weight:700;font-size:{scaled(13, self)}px;{self._ink}"

        # The head, over every page: the title and the close; with them, except on the
        # lock screen, lock now and the menu.
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel(t("p2p.friends_title"))
        title.setStyleSheet(f"font-weight:700;font-size:{scaled(15, self)}px;{self._ink}")
        head.addWidget(title, 1)
        self._tools = QWidget()
        tools = QHBoxLayout(self._tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self._lock_btn = QPushButton(t("p2p.friends_lock"))
        self._lock_btn.setToolTip(t("p2p.friends_lock_tooltip"))
        self._lock_btn.clicked.connect(self._svc.lock)
        tools.addWidget(self._lock_btn)
        mark, css = _dots(self)
        self._menu_btn = self._icon_button(mark, t("p2p.friends_menu_tooltip"), self._show_menu, css)
        tools.addWidget(self._menu_btn)
        head.addWidget(self._tools)
        head.addWidget(self._icon_button("✕", t("p2p.receive_close"), self.close_panel))
        outer.addLayout(head)
        # Whether the network is going through the proxy (or stopped for want of one).
        # On a line of its own: what it says can be long, and the head is narrow.
        self._net_lbl = QLabel("")
        self._net_lbl.setWordWrap(True)
        self._net_lbl.setStyleSheet(self._muted)
        self._net_lbl.setVisible(False)
        outer.addWidget(self._net_lbl)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_list_page())
        self._stack.addWidget(self._build_messages_page())
        self._stack.addWidget(self._build_lock_page())
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

        self._reset_lbl = QLabel(t("p2p.friends_reset_note"))
        self._reset_lbl.setWordWrap(True)
        self._reset_lbl.setStyleSheet(f"color:{palette('warning')};")
        self._reset_lbl.setVisible(bool(getattr(self._svc.store, "reset_reason", "")))
        outer.addWidget(self._reset_lbl)

        # The friends, which is what the list is for, come first and take the room.
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 4, 0)
        body_lay.setSpacing(6)
        lbl = QLabel(t("p2p.friends_list_title"))
        lbl.setStyleSheet(self._bold)
        body_lay.addWidget(lbl)
        # How it works is for whoever has nobody in the list yet.
        self._intro_lbl = QLabel(t("p2p.friends_intro"))
        self._intro_lbl.setWordWrap(True)
        self._intro_lbl.setStyleSheet(self._muted)
        body_lay.addWidget(self._intro_lbl)
        self._friends_box = QVBoxLayout()
        self._friends_box.setSpacing(2)
        body_lay.addLayout(self._friends_box)
        body_lay.addStretch(1)
        outer.addWidget(self._scroller(body), 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        lbl = QLabel(t("p2p.friends_my_code"))
        lbl.setStyleSheet(self._bold)
        outer.addWidget(lbl)
        row = QHBoxLayout()
        self._code_edit = QLineEdit(self._svc.code if not self._svc.locked else "")
        self._code_edit.setReadOnly(True)
        self._code_edit.setStyleSheet(f"font-family: monospace; font-size: {scaled(12, self)}px;")
        row.addWidget(self._code_edit, 1)
        self._copy_btn = QPushButton(t("p2p.friends_copy_code"))
        self._copy_btn.clicked.connect(self._copy_code)
        row.addWidget(self._copy_btn)
        outer.addLayout(row)
        self._name_lbl = QLabel()
        self._name_lbl.setWordWrap(True)
        self._name_lbl.setStyleSheet(self._muted)
        outer.addWidget(self._name_lbl)

        lbl = QLabel(t("p2p.friends_add_title"))
        lbl.setStyleSheet(self._bold + "padding-top:4px;")
        outer.addWidget(lbl)
        self._add_code = QLineEdit()
        self._add_code.setPlaceholderText(t("p2p.friends_add_code_placeholder"))
        self._add_code.returnPressed.connect(self._add_friend)
        outer.addWidget(self._add_code)
        add = QHBoxLayout()
        self._add_name = QLineEdit()
        self._add_name.setPlaceholderText(t("p2p.friends_add_name_placeholder"))
        self._add_name.returnPressed.connect(self._add_friend)
        add.addWidget(self._add_name, 1)
        self._add_btn = QPushButton(t("p2p.friends_add_button"))
        self._add_btn.setObjectName("primary_btn")
        self._add_btn.clicked.connect(self._add_friend)
        add.addWidget(self._add_btn)
        outer.addLayout(add)
        self._add_msg = QLabel("")
        self._add_msg.setWordWrap(True)
        self._add_msg.setStyleSheet(self._muted)
        outer.addWidget(self._add_msg)
        return page

    def _build_messages_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self._back_btn = QPushButton(t("p2p.friends_back"))
        self._back_btn.clicked.connect(self._back)
        head.addWidget(self._back_btn)
        head.addStretch(1)
        self._clear_btn = QPushButton(t("p2p.friends_clear"))
        self._clear_btn.setToolTip(t("p2p.friends_clear_tip"))
        self._clear_btn.clicked.connect(self._clear_conversation)
        head.addWidget(self._clear_btn)
        outer.addLayout(head)
        # Under the buttons, not between them: a long name has the whole width.
        self._msgs_title = QLabel()
        self._msgs_title.setTextFormat(Qt.TextFormat.PlainText)
        self._msgs_title.setWordWrap(True)
        self._msgs_title.setStyleSheet(self._bold)
        outer.addWidget(self._msgs_title)
        self._msgs_status = QLabel()
        self._msgs_status.setStyleSheet(self._muted)
        outer.addWidget(self._msgs_status)
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
        self._drop_lbl.setStyleSheet(f"border: 2px dashed {palette('accent')}; border-radius: 8px; "
                                     f"padding: 10px; color: {palette('accent')}; font-weight: 600;")
        self._drop_lbl.setVisible(False)
        outer.addWidget(self._drop_lbl)
        self._compose_msg = QLabel("")
        self._compose_msg.setWordWrap(True)
        self._compose_msg.setStyleSheet(f"color:{palette('warning')};")
        self._compose_msg.setVisible(False)
        outer.addWidget(self._compose_msg)
        row = QHBoxLayout()
        self._attach_btn = QPushButton("+")
        self._attach_btn.setFixedSize(scaled(34, self), scaled(34, self))
        self._attach_btn.setToolTip(t("p2p.friends_attach_tip"))
        self._attach_btn.setStyleSheet("font-size: 18px; font-weight: 700; padding: 0;")
        self._attach_btn.clicked.connect(self._attach)
        row.addWidget(self._attach_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self._compose = _ComposeEdit()
        self._compose.setPlaceholderText(t("p2p.friends_compose_placeholder"))
        self._compose.setToolTip(t("p2p.friends_compose_hint"))
        self._compose.setFixedHeight(scaled(58, self))
        self._compose.textChanged.connect(self._update_compose)
        self._compose.submitted.connect(self._send_text)
        self._compose.files_dropped.connect(self._on_dropped)
        self._compose.drag_state.connect(self._show_drop_hint)
        row.addWidget(self._compose, 1)
        col = QVBoxLayout()
        self._send_btn = QPushButton(t("p2p.friends_compose_send"))
        self._send_btn.setObjectName("primary_btn")
        self._send_btn.clicked.connect(self._send_text)
        col.addWidget(self._send_btn)
        self._counter = QLabel("")
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter.setStyleSheet(self._muted)
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
        title = QLabel(t("p2p.friends_locked_title"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(self._bold)
        lay.addWidget(title)
        hint = QLabel(t("p2p.friends_locked_hint"))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(self._muted)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch()
        self._unlock_edit = QLineEdit()
        self._unlock_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._unlock_edit.setPlaceholderText(t("p2p.friends_unlock_placeholder"))
        self._unlock_edit.setMinimumWidth(scaled(240, self))
        self._unlock_edit.returnPressed.connect(self._unlock)
        row.addWidget(self._unlock_edit)
        self._unlock_btn = QPushButton(t("p2p.friends_unlock"))
        self._unlock_btn.setObjectName("primary_btn")
        self._unlock_btn.clicked.connect(self._unlock)
        row.addWidget(self._unlock_btn)
        row.addStretch()
        lay.addLayout(row)
        self._unlock_msg = QLabel("")
        self._unlock_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unlock_msg.setWordWrap(True)
        self._unlock_msg.setStyleSheet(f"color:{palette('warning')};")
        lay.addWidget(self._unlock_msg)
        # The way back in when the password is forgotten: delete the list and start
        # again (Settings has the same button - it is never locked).
        forgot = QHBoxLayout()
        forgot.addStretch()
        self._forgot_btn = QPushButton(t("p2p.friends_forgot"))
        self._forgot_btn.setFlat(True)
        self._forgot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._forgot_btn.setToolTip(t("p2p.friends_forgot_tooltip"))
        self._forgot_btn.setStyleSheet(f"color:{palette('text_secondary')};text-decoration:underline;")
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
        self._msg_labels, self._xfer, self._previews = {}, {}, {}
        self._msgs_sig, self._new_ids, self._friend_id = None, set(), ""
        self._compose.clear()
        self._code_edit.clear()
        self._msgs_title.setText("")
        self._msgs_status.setText("")

    def _show_lock(self):
        self._svc.set_viewing(None)
        self._scrub()
        self._unlock_msg.setText("")
        self._unlock_edit.clear()
        self._stack.setCurrentIndex(_PAGE_LOCK)
        self._sync_bar()
        self._unlock_edit.setFocus()

    def _sync_bar(self):
        locked = self._stack.currentIndex() == _PAGE_LOCK
        self._tools.setVisible(not locked)
        self._lock_btn.setVisible(self._svc.has_lock_password)
        self._net_lbl.setVisible(bool(self._net_lbl.text()) and not locked)

    def current_friend(self) -> str:
        """The friend whose messages are on show ("" on the list / lock page)."""
        return self._friend_id if self._stack.currentIndex() == _PAGE_MESSAGES else ""

    def show_friend(self, fid: str):
        """Open one friend's messages (also when the dialog is already up and a
        notification asks for them)."""
        if self._svc.locked:
            self._wanted_friend = fid
            self._show_lock()
            return
        f = self._svc.friend(fid)
        if f is None:
            self._stack.setCurrentIndex(_PAGE_LIST)
            return
        self._wanted_friend = ""
        self._friend_id = fid
        # Which were new when the person got here: they are read from now on,
        # but still marked new on the page itself.
        self._new_ids = {m.id for m in self._svc.messages(fid) if not m.read}
        self._svc.set_viewing(fid)
        self._msgs_sig = None
        self._previews = {}
        self._close_videos()
        self._compose.clear()
        self._fill_messages(stick=True)
        self._stack.setCurrentIndex(_PAGE_MESSAGES)
        self._sync_bar()
        self._update_compose()
        self._compose.setFocus()

    def _back(self):
        self._close_videos()
        self._svc.set_viewing(None)
        self._friend_id = ""
        self._stack.setCurrentIndex(_PAGE_LIST)
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
        if self._stack.currentIndex() == _PAGE_MESSAGES:
            if self._svc.friend(self._friend_id) is None:
                self._back()            # the friend was removed under it
            else:
                self._fill_messages()
                self._update_compose()

    def _fill_friends(self):
        _clear(self._friends_box)
        people = self._svc.friends()
        self._intro_lbl.setVisible(not people)
        if not people:
            empty = QLabel(t("p2p.friends_empty"))
            empty.setStyleSheet(self._muted)
            empty.setWordWrap(True)
            self._friends_box.addWidget(empty)
            return
        for f in people:
            self._friends_box.addWidget(self._friend_row(f))

    def _friend_row(self, f) -> QWidget:
        """One friend: click it for the chat; the ⋯ on it renames or removes them.
        Tokens they send are in the chat, as cards, with the rest of what they say."""
        row = _FriendRow()
        row.clicked.connect(lambda fid=f.id: self.show_friend(fid))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(6)
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
        tip = (t("p2p.friends_status_tip_connected") if f.connected
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
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _at_bottom(self) -> bool:
        bar = self._msgs_scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_bottom(self):
        QTimer.singleShot(0, lambda: self._msgs_scroll.verticalScrollBar().setValue(
            self._msgs_scroll.verticalScrollBar().maximum()))

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
                                      for m in msgs), f.display, f.connected)
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
        self._msgs_status.setText(("✓ " + t("p2p.friends_status_connected")) if f.connected
                                  else ("⏳ " + t("p2p.friends_status_waiting")))
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
        bar.setFixedHeight(scaled(14, self))
        bar.setVisible(False)
        lay.addWidget(bar)
        speed = QLabel()
        speed.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        speed.setVisible(False)
        lay.addWidget(speed)

        pixmap = self._previews.get(m.id)
        if pixmap is not None:
            pic = QLabel()
            pic.setPixmap(pixmap)
            pic.setAlignment(Qt.AlignmentFlag.AlignLeft)
            lay.addWidget(pic)
        player = self._videos.get(m.id)
        if player is not None:
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
        text = t(key, name=name, path=att.get("dir", ""), time=format_remaining((info or {}).get("wait", 0)))
        if text == key:
            text = ""                       # (a combination that has no words)
        idle = float((info or {}).get("idle", 0) or 0)
        if state == "receiving" and idle >= _IDLE_HINT_SECONDS:
            # (nothing is cut off for this - there is no time limit on a connection)
            text += "\n" + t("p2p.friends_file_idle", name=name, minutes=max(1, int(idle // 60)))
            if (info or {}).get("tls"):
                text += " " + t("p2p.receive_idle_tls")
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
            speed.setToolTip(mbps)
            speed.setVisible(True)
        else:
            bar.setVisible(False)
            speed.setVisible(False)

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
        f = self._svc.friend(self._friend_id)
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
                reply = question_window_modal(
                    self, t("p2p.friends_title"), ask,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self._close_video(mid)              # (a file being watched is let go of first)
        self._svc.cancel_file(self._friend_id, mid)
        self._msgs_sig = None
        self._fill_messages()

    def _show_in_folder(self, att):
        """Open the folder a received file is in, with the file selected. The
        file itself is never opened or run from here."""
        path = self._saved_path(att)
        if path is None:
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        except OSError:
            logger.debug("P2P friends: could not show a received file", exc_info=True)

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
        path = self._saved_path(att)
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
        for mid in list(self._videos):
            self._close_video(mid)

    def _show_picture(self, mid: str, att):
        """The person asked to see this picture: decode it (and only it) under
        the limits of ui.widgets.safe_image."""
        from ui.widgets.safe_image import load_preview
        path = self._saved_path(att)
        if path is not None and not self._media_ok(path, att):
            path = None
        pixmap = load_preview(path) if path is not None else None
        if pixmap is None:
            self._say(t("p2p.friends_file_picture_failed"), warn=True)
            return
        self._previews[mid] = pixmap
        self._msgs_sig = None
        self._fill_messages()

    def _attach(self):
        """+: pick a file or folder, see exactly what would be offered, offer it.
        Nothing is sent now - the friend gets a card and decides."""
        f = self._svc.friend(self._friend_id)
        if f is None:
            return
        if not self._svc.can_offer(self._friend_id):
            self._say(t("p2p.friends_compose_full", name=f.display), warn=True)
            return
        from ui.widgets.file_pickers import pick_file_or_folder
        path = pick_file_or_folder(self, t("p2p.friends_attach_pick"))
        if path:
            self._offer_paths([path])

    # ── Dropping files ───────────────────────────────────────────────────

    def _can_take_drop(self) -> bool:
        """Only a friend's open conversation takes files: not the list, not the
        lock screen."""
        return (not self._svc.locked and self._stack.currentIndex() == _PAGE_MESSAGES
                and self._svc.friend(self._friend_id) is not None)

    def _show_drop_hint(self, on: bool):
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
        if self._can_take_drop():
            QTimer.singleShot(0, lambda paths=list(paths): self._offer_paths(paths))

    def _offer_paths(self, paths: list):
        """Offer these files and folders to the friend whose conversation is open:
        the list of what would go (any file can be ticked off), a refusal for the
        kinds that are never sent, then the offer. Nothing is sent now - the friend
        gets a card and decides. The same whether they were picked with + or dropped."""
        f = self._svc.friend(self._friend_id)
        if f is None or self._svc.locked or not paths:
            return
        if not self._svc.can_offer(self._friend_id):
            self._say(t("p2p.friends_compose_full", name=f.display), warn=True)
            return
        sources = [Path(p) for p in paths]
        from ui.dialogs.p2p_send_confirm_dialog import P2pSendConfirmDialog
        confirm = P2pSendConfirmDialog(sources if len(sources) > 1 else sources[0], self)
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        files = confirm.selected_files()
        if not files:
            return
        files = [(str(p), str(rel)) for p, rel in files]
        blocked = _a.blocked_names(files)
        if blocked:
            shown = "\n".join(blocked[:5]) + ("\n…" if len(blocked) > 5 else "")
            self._say(t("p2p.friends_attach_blocked", names=shown), warn=True)
            return
        label = sources[0].name if len(sources) == 1 else t("p2p.send_confirm_items", count=len(sources))
        try:
            self._svc.offer_file(self._friend_id, files, label)
        except FriendError as e:
            key = f"p2p.friends_attach_err_{str(e).replace(' ', '_')}"
            text = t(key, name=f.display)
            self._say(text if text != key else t("p2p.friends_attach_err_error"), warn=True)
            return
        self._msgs_sig = None
        self._fill_messages(stick=True)
        self._update_compose()

    def _open_link(self, href: str, links: list, name: str):
        """A link in a message was clicked: ask first. *href* is only an index
        into the links core.p2p.markup checked - never an address."""
        m = _LINK_HREF.match(str(href))
        if m is None or int(m.group(1)) >= len(links):
            return
        from ui.dialogs.p2p_link_dialog import P2pLinkDialog
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
        self._net_lbl.setVisible(bool(self._net_lbl.text())
                                 and self._stack.currentIndex() != _PAGE_LOCK)

    def _on_tick(self):
        self._update_net_status()
        self._update_countdowns()
        if self._stack.currentIndex() == _PAGE_MESSAGES:
            self._fill_messages()               # (rebuilt only if something about it changed)

    def _update_countdowns(self):
        for mid, (sub_lbl, m) in self._msg_labels.items():
            state = m.state()
            when = time.strftime("%H:%M:%S", time.localtime(m.received_at))
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
        f = self._svc.friend(self._friend_id)
        if f is None:
            return
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("p2p.friends_title"), t("p2p.friends_clear_confirm", name=f.display),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._svc.clear_conversation(self._friend_id)
            self._msgs_sig = None
            self._fill_messages(stick=True)

    # ── Actions ──────────────────────────────────────────────────────────

    def _copy_code(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._code_edit.text())
        self._copy_btn.setText(t("p2p.friends_copied"))
        QTimer.singleShot(1500, lambda: self._copy_btn.setText(t("p2p.friends_copy_code")))

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
        text, ok = QInputDialog.getText(self, t("p2p.friends_rename"),
                                        t("p2p.friends_rename_prompt"), text=f.alias or f.name)
        if ok:
            self._svc.rename_friend(fid, text)

    def _remove(self, fid: str):
        f = self._svc.friend(fid)
        if f is None:
            return
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("p2p.friends_title"), t("p2p.friends_remove_confirm", name=f.display),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._svc.remove_friend(fid)

    def _use_token(self, token: str):
        """The person clicked a token a friend sent: pick what to send, go."""
        live = next((tk for tk in self._svc.tokens() if tk.token == token), None)
        if live is None:
            self._refresh()
            return
        self._svc.mark_read(live.friend_id)
        from ui.dialogs.p2p_send_flow import send_files_flow
        send_files_flow(self, token=token)
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
        reset_friend_list(self, self._svc)      # (lock_changed brings the empty list up)

    def _password(self, title: str, label: str) -> tuple:
        text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Password)
        return (text if ok else None)

    def _say(self, text: str, warn: bool = False):
        from ui.modal_helpers import information_window_modal, warning_window_modal
        (warning_window_modal if warn else information_window_modal)(self, t("p2p.friends_title"), text)

    def _new_password(self, label_key: str, minimum: int):
        """Ask for a new password twice; None when cancelled or not usable."""
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
        current = ""
        if svc.has_lock_password:
            current = self._password(t("p2p.friends_title"), t("p2p.friends_lock_prompt_current"))
            if current is None:
                return
            if not svc.store.check_lock_password(current):
                self._say(t("p2p.friends_lock_wrong"), warn=True)
                return
        new = self._new_password("p2p.friends_lock_prompt_new", _f.MIN_LOCK_PASSWORD)
        if new is None:
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
        current = self._password(t("p2p.friends_title"), t("p2p.friends_lock_prompt_current"))
        if current is None:
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
        password = self._new_password("p2p.friends_link_prompt", _f.MIN_LINK_PASSWORD)
        if password is None:
            return
        path = pick_save_path(self, t("p2p.friends_link_title"), t("p2p.friends_link_filter"),
                              default_name="savesync-friends" + _LINK_SUFFIX)
        if not path:
            return
        if not path.lower().endswith(_LINK_SUFFIX):
            path += _LINK_SUFFIX
        try:
            Path(path).write_bytes(self._svc.export_link(password))
        except (OSError, FriendError) as e:
            self._say(t("p2p.friends_link_failed", error=str(e)), warn=True)
            return
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("p2p.friends_link_title"), t("p2p.friends_link_saved", path=path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._svc.retire_here()
            self._say(t("p2p.friends_link_retired"))

    def _import_flow(self):
        """Adopt the identity and friends of a link file made on another PC."""
        from ui.widgets.file_pickers import pick_file
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
        from ui.modal_helpers import question_window_modal
        reply = question_window_modal(
            self, t("p2p.friends_title"), t("p2p.friends_import_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._svc.import_link(blob, password)
        except FriendError:
            self._say(t("p2p.friends_import_failed"), warn=True)
            return
        self._say(t("p2p.friends_import_done", count=len(self._svc.store.friends)))
        self._stack.setCurrentIndex(_PAGE_LIST)
        self._refresh()

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
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _teardown(self):
        """Let go of everything the panel held on the service for: balanced with what
        __init__ took."""
        self._tick.stop()
        self._close_videos()
        for sig, slot in ((self._svc.changed, self._refresh),
                          (self._svc.lock_changed, self._on_lock_changed)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._svc.set_viewing(None)
        self._svc.release()
