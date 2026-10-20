"""SaveSync - group chats in the friends panel.

A mixin of ui.dialogs.p2p_friends_dialog.P2pFriendsPanel, which supplies everything it
leans on (the page stack, the style and language registries, _modal, _say, the file card's
words). It is only a VIEW of ui.p2p_friends_service, which keeps the groups
(core.p2p.groups) and does everything a button here asks for. It adds:

- on the LIST: a Groups block over the friends - the invitations waiting for an answer
  (Accept / Decline), each group with a badge counting what you have not read, and a way to
  make a new one (its name and how many people it can hold);
- one group's CHAT: texts both ways (each of mine says how many of the others have it), what
  members offered as cards you can accept, what you offered with a line for every member, and
  short notes where the group changed (somebody joined, was taken out, was made an
  administrator...) or some messages did not reach you. A box to write in and a + to offer
  files, which can also be dropped on it;
- the MEMBERS of a group: everyone with what they are (host, administrator, member) and, for
  whoever may decide it, the controls - make an administrator, allow to invite and how many,
  take out - and invite, rename, leave and (the host) close the group.

Members are shown by what you call them if they are friends and otherwise by their own word
with the start of their key always next to it: a name is only what its owner says.
"""
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from core.p2p import attachments as _a
from core.p2p import friends as _f
from core.p2p import groups as _g
from core.p2p import markup
from core.p2p.friends import FriendError
from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette
from ui.widgets.count_badge import CountBadge

PAGE_GROUP, PAGE_MEMBERS = 3, 4


def _dlg():
    """The friends panel's module (imported late: it imports this one)."""
    from ui.dialogs import p2p_friends_dialog
    return p2p_friends_dialog


def _delivery(entry: dict) -> tuple:
    """(how many of the others have a message of mine, how many it went to, how many it will
    never reach, whether it is in anybody's stream yet)."""
    dv = entry.get("dv") or {}
    have = sum(1 for d in dv.values() if d["ok"])
    lost = sum(1 for d in dv.values() if d["x"])
    out = any(d["w"] for d in dv.values())
    return have, len(dv), lost, out


class GroupPagesMixin:
    # ── set-up ───────────────────────────────────────────────────────────

    def _init_groups(self, open_group: str = ""):
        self._group_id = ""             # the group on show ("" = none)
        self._wanted_group = open_group
        self._attending_group = False
        self._group_new: set = set()    # entries that were unread when the page opened
        self._groups_sig = None
        self._group_sig = None
        self._members_sig = None
        self._group_rows: dict = {}
        self._group_xfer: dict = {}     # offer id -> (message, status, bar, speed, sender), for live figures
        self._group_out: dict = {}      # batch -> (entries, status label), the same for what I offered
        self._attach_warned = False
        self._grant_timers: dict = {}   # (group id, member id) -> the timer that sends a quota that was just changed
        self._grant_due: dict = {}      # ... and the quota it will send

    def _build_groups_block(self, lay: QVBoxLayout):
        """The Groups block at the top of the list page."""
        head = QHBoxLayout()
        lbl = self._bind(QLabel(), text="p2p.groups_title")
        self._sty(lbl, lambda: self._bold)
        head.addWidget(lbl, 1)
        self._new_group_btn = self._bind(QPushButton(), text="p2p.groups_new", tip="p2p.groups_new_tip")
        self._new_group_btn.clicked.connect(self._create_group_flow)
        head.addWidget(self._new_group_btn)
        lay.addLayout(head)
        self._groups_intro = self._bind(QLabel(), text="p2p.groups_intro")
        self._groups_intro.setWordWrap(True)
        self._sty(self._groups_intro, lambda: self._muted)
        lay.addWidget(self._groups_intro)
        self._invites_box = QVBoxLayout()
        self._invites_box.setSpacing(6)
        lay.addLayout(self._invites_box)
        self._groups_box = QVBoxLayout()
        self._groups_box.setSpacing(2)
        lay.addLayout(self._groups_box)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

    def _build_group_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self._group_back_btn = self._bind(QPushButton(), text="p2p.friends_back")
        self._group_back_btn.clicked.connect(self._group_back)
        head.addWidget(self._group_back_btn)
        head.addStretch(1)
        self._members_btn = self._bind(QPushButton(), text="p2p.groups_members_button",
                                       tip="p2p.groups_members_tip")
        self._members_btn.clicked.connect(lambda _=False: self.show_members(self._group_id))
        head.addWidget(self._members_btn)
        self._group_clear_btn = self._bind(QPushButton(), text="p2p.friends_clear", tip="p2p.groups_clear_tip")
        self._group_clear_btn.clicked.connect(self._clear_group_log)
        head.addWidget(self._group_clear_btn)
        outer.addLayout(head)
        self._group_title = QLabel()
        self._group_title.setTextFormat(Qt.TextFormat.PlainText)
        self._group_title.setWordWrap(True)
        self._sty(self._group_title, lambda: self._bold)
        outer.addWidget(self._group_title)
        self._group_status = QLabel()
        self._group_status.setWordWrap(True)
        self._sty(self._group_status, lambda: self._muted)
        outer.addWidget(self._group_status)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        self._group_box = QVBoxLayout()
        self._group_box.setSpacing(6)
        lay.addLayout(self._group_box)
        lay.addStretch(1)
        self._group_scroll = self._scroller(body)
        outer.addWidget(self._group_scroll, 1)

        self._group_drop_lbl = QLabel("")
        self._group_drop_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._group_drop_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._sty(self._group_drop_lbl, lambda: (
            f"border: 2px dashed {palette('accent')}; border-radius: 8px; "
            f"padding: 10px; color: {palette('accent')}; font-weight: 600;"))
        self._group_drop_lbl.setVisible(False)
        outer.addWidget(self._group_drop_lbl)
        self._group_note = QLabel("")
        self._group_note.setWordWrap(True)
        self._sty(self._group_note, lambda: f"color:{palette('warning')};")
        self._group_note.setVisible(False)
        outer.addWidget(self._group_note)
        row = QHBoxLayout()
        self._group_attach_btn = self._bind(QPushButton("+"), tip="p2p.groups_attach_tip")
        self._group_attach_btn.setFixedSize(scaled(34, self), scaled(34, self))
        self._group_attach_btn.setStyleSheet("font-size: 18px; font-weight: 700; padding: 0;")
        self._group_attach_btn.clicked.connect(self._group_attach)
        row.addWidget(self._group_attach_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self._group_compose = self._bind(_dlg()._ComposeEdit(), hint="p2p.groups_compose_placeholder",
                                         tip="p2p.friends_compose_hint")
        self._group_compose.setFixedHeight(scaled(58, self))
        self._group_compose.textChanged.connect(self._update_group_compose)
        self._group_compose.submitted.connect(self._send_group_text)
        self._group_compose.files_dropped.connect(self._on_group_dropped)
        self._group_compose.drag_state.connect(self._show_group_drop_hint)
        row.addWidget(self._group_compose, 1)
        col = QVBoxLayout()
        self._group_send_btn = self._bind(QPushButton(), text="p2p.friends_compose_send")
        self._group_send_btn.setObjectName("primary_btn")
        self._group_send_btn.clicked.connect(self._send_group_text)
        col.addWidget(self._group_send_btn)
        self._group_counter = QLabel("")
        self._group_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._group_counter)
        row.addLayout(col)
        outer.addLayout(row)
        return page

    def _build_members_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self._members_back_btn = self._bind(QPushButton(), text="p2p.groups_back_to_chat")
        self._members_back_btn.clicked.connect(lambda _=False: self.show_group(self._group_id))
        head.addWidget(self._members_back_btn)
        head.addStretch(1)
        self._invite_btn = self._bind(QPushButton(), text="p2p.groups_invite", tip="p2p.groups_invite_tip")
        self._invite_btn.clicked.connect(self._invite_menu)
        head.addWidget(self._invite_btn)
        outer.addLayout(head)
        self._members_title = QLabel()
        self._members_title.setTextFormat(Qt.TextFormat.PlainText)
        self._members_title.setWordWrap(True)
        self._sty(self._members_title, lambda: self._bold)
        outer.addWidget(self._members_title)
        self._members_info = QLabel()
        self._members_info.setWordWrap(True)
        self._sty(self._members_info, lambda: self._muted)
        outer.addWidget(self._members_info)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        self._members_box = QVBoxLayout()
        self._members_box.setSpacing(6)
        lay.addLayout(self._members_box)
        lay.addStretch(1)
        self._members_body = body
        outer.addWidget(self._scroller(body), 1)
        actions = QHBoxLayout()
        self._rename_group_btn = self._bind(QPushButton(), text="p2p.groups_rename")
        self._rename_group_btn.clicked.connect(self._rename_group)
        actions.addWidget(self._rename_group_btn)
        self._limit_btn = self._bind(QPushButton(), text="p2p.groups_limit_button", tip="p2p.groups_limit_tip")
        self._limit_btn.clicked.connect(self._raise_limit)
        actions.addWidget(self._limit_btn)
        self._leave_group_btn = self._bind(QPushButton(), text="p2p.groups_leave")
        self._leave_group_btn.clicked.connect(self._leave_group)
        actions.addWidget(self._leave_group_btn)
        self._dissolve_btn = self._bind(QPushButton(), text="p2p.groups_dissolve", tip="p2p.groups_dissolve_tip")
        self._dissolve_btn.clicked.connect(self._dissolve_group)
        actions.addWidget(self._dissolve_btn)
        outer.addLayout(actions)
        return page

    # ── names ────────────────────────────────────────────────────────────

    def _who(self, mid: str) -> str:
        """A member as a line names them: what I call a friend, "you" for me; for anybody else
        their own word AND the start of their key, because the word is only theirs."""
        return self._svc.member_label(mid, t("p2p.groups_you"))

    def _role_text(self, g, mid: str) -> str:
        role = self._svc.book.role(g, mid)
        return {"host": t("p2p.groups_role_host"), "admin": t("p2p.groups_role_admin")}.get(role, "")

    # ── the Groups block on the list ─────────────────────────────────────

    def _fill_groups(self):
        """Invitations and groups, written out again only when something they show changed."""
        svc = self._svc
        groups, invites = svc.groups(), svc.group_invites()
        self._groups_intro.setVisible(not groups and not invites)
        sig = (tuple((g.id, g.name, g.state, len(g.members), g.max_members, svc.group_unread(g.id), g.silenced)
                     for g in groups),
               tuple((r["gid"], r["name"], r["inviter"], r["count"], svc.member_info(r["inviter"])["name"])
                     for r in invites))
        if sig == self._groups_sig:
            return
        self._groups_sig = sig
        on = self._window.focusWidget()
        focused = next((gid for gid, row in self._group_rows.items()
                        if row is on or (on is not None and row.isAncestorOf(on))), None)
        _clear = _dlg()._clear
        _clear(self._invites_box)
        _clear(self._groups_box)
        self._group_rows = {}
        for rec in invites:
            self._invites_box.addWidget(self._invite_card(rec))
        for g in groups:
            self._group_rows[g.id] = row = self._group_row(g)
            self._groups_box.addWidget(row)
        if focused in self._group_rows:
            self._later(0, lambda gid=focused: self._group_rows.get(gid) and self._group_rows[gid].setFocus())

    def _invite_card(self, rec: dict) -> QWidget:
        card = QFrame()
        card.setObjectName("p2p_group_invite")
        card.setStyleSheet(
            f"#p2p_group_invite {{ border: 1px solid {palette('border_hover')}; border-radius: 6px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        text = QLabel(t("p2p.groups_invite_text", name=self._who(rec["inviter"]), group=rec["name"],
                        count=rec["count"], max=rec["mx"]))
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        text.setStyleSheet(self._ink)
        lay.addWidget(text)
        note = QLabel(t("p2p.groups_invite_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"{self._muted}font-size:{scaled(11, self)}px;")
        lay.addWidget(note)
        buttons = QHBoxLayout()
        accept = QPushButton(t("p2p.friends_file_accept"))
        accept.setObjectName("primary_btn")
        accept.clicked.connect(lambda _=False, gid=rec["gid"]: self._accept_invite(gid))
        decline = QPushButton(t("p2p.friends_file_decline"))
        decline.clicked.connect(lambda _=False, gid=rec["gid"]: self._decline_invite(gid))
        buttons.addWidget(accept)
        buttons.addWidget(decline)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        return card

    def _group_row(self, g) -> QWidget:
        """One group: click it for the chat; the ⋯ silences it, or lets it go."""
        dlg = _dlg()
        row = dlg._FriendRow()
        row.clicked.connect(lambda gid=g.id: self.show_group(gid))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(6)
        col = QVBoxLayout()
        col.setSpacing(0)
        name = QLabel("👥 " + g.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setStyleSheet(f"font-weight:600;{self._ink}")
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(name)
        if g.state == "active":
            sub = t("p2p.groups_row_members", count=len(g.members), max=g.max_members)
            if g.silenced:
                sub += "  ·  " + t("p2p.groups_row_silenced")
        else:
            sub = t(f"p2p.groups_row_{g.state}")
        status = QLabel(sub)
        status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.setStyleSheet(self._muted)
        col.addWidget(status)
        lay.addLayout(col, 1)
        unread = self._svc.group_unread(g.id)
        badge = CountBadge()
        badge.set_count(unread)
        lay.addWidget(badge)
        for w in (name, status, badge):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        mark, css = dlg._dots(self)
        more = self._icon_button(mark, t("p2p.friends_row_menu_tip"), None, css)
        more.clicked.connect(lambda _=False, gid=g.id, btn=more: self._group_menu(gid, btn))
        lay.addWidget(more)
        return row

    def _group_menu(self, gid: str, btn: QPushButton):
        g = self._svc.group(gid)
        if g is None:
            return
        menu = QMenu(self)
        menu.addAction(t("p2p.groups_menu_open"), lambda: self.show_group(gid))
        if g.state == "active":
            menu.addAction(t("p2p.groups_menu_unsilence") if g.silenced else t("p2p.groups_menu_silence"),
                           lambda: self._svc.set_group_silenced(gid, not g.silenced))
            menu.addAction(t("p2p.groups_leave"), lambda: self._leave_group(gid))
        else:
            menu.addAction(t("p2p.groups_delete"), lambda: self._delete_group(gid))
        with self._modal():
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    # ── making, answering ────────────────────────────────────────────────

    def _create_group_flow(self):
        """A name and how many people the group can hold (fixed when it is made)."""
        if self._svc.locked:
            return
        with self._modal():
            dlg = QDialog(self)
            dlg.setWindowTitle(t("p2p.groups_new_title"))
            form = QFormLayout(dlg)
            name = QLineEdit()
            name.setMaxLength(40)
            name.setPlaceholderText(t("p2p.groups_new_name"))
            size = QSpinBox()
            size.setRange(_f.MIN_GROUP_MEMBERS, _g.MAX_MEMBERS)
            size.setValue(_f.DEFAULT_GROUP_MEMBERS)
            size.setToolTip(t("p2p.groups_new_size_tip", most=_g.MAX_MEMBERS))
            form.addRow(t("p2p.groups_new_name_label"), name)
            form.addRow(t("p2p.groups_new_size_label"), size)
            note = QLabel(t("p2p.groups_new_note", most=_g.MAX_MEMBERS))
            note.setWordWrap(True)
            note.setStyleSheet(self._muted)
            form.addRow(note)
            row = QHBoxLayout()
            ok = QPushButton(t("p2p.groups_new_create"))
            ok.setObjectName("primary_btn")
            ok.setDefault(True)
            cancel = QPushButton(t("common.cancel"))
            ok.clicked.connect(dlg.accept)
            cancel.clicked.connect(dlg.reject)
            row.addStretch(1)
            row.addWidget(ok)
            row.addWidget(cancel)
            form.addRow(row)
            name.returnPressed.connect(dlg.accept)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            title, limit = name.text().strip(), size.value()
        if not title or self._svc.locked or self._closing:
            return
        try:
            g = self._svc.create_group(title, limit)
        except FriendError as e:
            self._say(self._group_error(e), warn=True)
            return
        self.show_group(g.id)

    def _group_error(self, e) -> str:
        key = f"p2p.groups_err_{str(e).replace(' ', '_')}"
        text = t(key)
        return text if text != key else t("p2p.groups_err_error")

    def _accept_invite(self, gid: str):
        try:
            self._svc.accept_group_invite(gid)
        except FriendError as e:
            self._say(self._group_error(e), warn=True)
            self._groups_sig = None
            self._fill_groups()
            return
        self.show_group(gid)

    def _decline_invite(self, gid: str):
        try:
            self._svc.decline_group_invite(gid)
        except FriendError:
            pass
        self._groups_sig = None
        self._fill_groups()

    # ── which page ───────────────────────────────────────────────────────

    def current_group(self) -> str:
        """The group on show ("" on the other pages)."""
        return self._group_id if self._stack.currentIndex() in (PAGE_GROUP, PAGE_MEMBERS) else ""

    def show_group(self, gid: str):
        """Open one group's chat (also when the panel is already up and a notification asks)."""
        if self._svc.locked:
            self._wanted_group = gid
            self._show_lock()
            return
        if self._modal_depth:
            self._wanted_group = gid
            return
        g = self._svc.group(gid)
        self._wanted_group = ""
        if g is None:
            self._stack.setCurrentIndex(0)
            return
        if gid == self._group_id and self._stack.currentIndex() == PAGE_GROUP:
            self._sync_attention()
            self._scroll_group_bottom()
            self._group_compose.setFocus()
            return
        self._close_videos()
        self._friend_id = ""
        self._group_id = gid
        self._group_new = {e["id"] for e in self._svc.group_log(gid)
                           if e["dir"] == "in" and not e["read"] and e["kind"] in ("text", "file")}
        self._group_sig = None
        self._group_compose.clear()
        self._fill_group(stick=True)
        self._stack.setCurrentIndex(PAGE_GROUP)
        self._sync_attention()
        self._sync_bar()
        self._update_group_compose()
        self._group_compose.setFocus()

    def show_members(self, gid: str):
        if self._svc.locked or self._svc.group(gid) is None:
            return
        self._group_id = gid
        self._members_sig = None
        self._fill_members()
        self._stack.setCurrentIndex(PAGE_MEMBERS)
        self._sync_attention()

    def _group_back(self):
        self._group_id = ""
        self._stack.setCurrentIndex(0)
        self._sync_attention()
        self._refresh()

    def _apply_wanted_group(self):
        if self._wanted_group and not self._svc.locked:
            self.show_group(self._wanted_group)

    # ── view of the service ──────────────────────────────────────────────

    def _refresh_groups(self):
        """Called by the panel's _refresh: the block on the list, and the group pages if they are on show."""
        self._fill_groups()
        gid = self.current_group()
        if gid:
            if self._svc.group(gid) is None:
                self._group_back()          # the group is gone (closed, or left from the other page)
                return
            if self._stack.currentIndex() == PAGE_GROUP:
                self._fill_group()
                self._update_group_compose()
            else:
                self._fill_members()

    def _scrub_groups(self):
        """Forget what the group pages show (the list was locked)."""
        dlg = _dlg()
        for box in (self._invites_box, self._groups_box, self._group_box, self._members_box):
            dlg._clear(box)
        self._group_rows, self._group_xfer, self._group_out = {}, {}, {}
        self._groups_sig = self._group_sig = self._members_sig = None
        self._group_new, self._group_id = set(), ""
        self._flush_grants(say=False)       # (locked: dropped)
        self._group_compose.clear()
        self._group_title.setText("")
        self._group_status.setText("")
        self._members_title.setText("")
        self._members_info.setText("")

    def _group_at_bottom(self) -> bool:
        bar = self._group_scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_group_bottom(self):
        bar = self._group_scroll.verticalScrollBar()
        self._later(0, lambda: bar.setValue(bar.maximum()))

    # ── the chat ─────────────────────────────────────────────────────────

    def _fill_group(self, stick: bool = False):
        """The group's log, oldest first. Rebuilt only when something about it changed."""
        svc = self._svc
        g = svc.group(self._group_id)
        if g is None:
            return
        entries = sorted(svc.group_log(g.id), key=lambda e: e["ts"])
        sig = (g.id, g.name, g.state, len(g.members), tuple(
            (e["id"], e["read"], e.get("fs", ""), e.get("fw", ""), e.get("w", ""), _delivery(e) if e["kind"] == "text" else 0,
             e["id"] in self._group_new) for e in entries))
        if sig == self._group_sig:
            return
        self._group_sig = sig
        was_at_bottom = stick or self._group_at_bottom()
        _dlg()._clear(self._group_box)
        self._group_xfer, self._group_out = {}, {}
        self._group_title.setText("👥 " + g.name)
        self._group_status.setText(self._group_status_text(g))
        self._group_clear_btn.setEnabled(bool(entries))
        batches_done: set = set()
        if not entries:
            empty = QLabel(t("p2p.groups_chat_empty"))
            empty.setWordWrap(True)
            empty.setStyleSheet(self._muted)
            self._group_box.addWidget(empty)
            return
        for e in entries:
            if e["kind"] == "sys":
                self._group_box.addWidget(self._sys_line(e))
            elif e["kind"] == "gap":
                note = QLabel(t("p2p.groups_gap", name=self._who(e["from"]), count=e["missed"]))
                note.setTextFormat(Qt.TextFormat.PlainText)
                note.setWordWrap(True)
                note.setAlignment(Qt.AlignmentFlag.AlignCenter)
                note.setStyleSheet(f"color:{palette('text_muted')};font-style:italic;")
                self._group_box.addWidget(note)
            elif e["kind"] == "file":
                if e["dir"] == "in":
                    self._group_box.addWidget(self._group_file_in(e))
                elif e["batch"] not in batches_done:
                    batches_done.add(e["batch"])
                    self._group_box.addWidget(self._group_file_out(
                        [x for x in entries if x["kind"] == "file" and x["dir"] == "out" and x["batch"] == e["batch"]]))
            else:
                self._group_box.addWidget(self._group_bubble(e))
        self._update_group_files()
        if was_at_bottom:
            self._scroll_group_bottom()

    def _group_status_text(self, g) -> str:
        if g.state == "joining":
            return t("p2p.groups_status_joining")
        if g.state == "kicked":
            return t("p2p.groups_status_kicked")
        if g.state == "failed":
            return t("p2p.groups_status_failed")
        role = self._svc.book.role(g, g.me)
        text = t("p2p.groups_row_members", count=len(g.members), max=g.max_members)
        if role in ("host", "admin"):
            text += "  ·  " + self._role_text(g, g.me)
        if g.host_gone:
            text += "  ·  " + t("p2p.groups_status_no_host")
        return text

    def _sys_line(self, e: dict) -> QWidget:
        x = e["x"]
        me, key = self._svc.my_id(), f"p2p.groups_sys_{x['e']}"
        # (a language that cannot put "you" where a name goes has its own words for it)
        for suffix, mine in (("_byme", x["by"] == me), ("_me", x["w"] == me)):
            if mine and t(key + suffix) != key + suffix:
                key += suffix
                break
        text = t(key, name=self._who(x["w"]) if x["w"] else "", by=self._who(x["by"]) if x["by"] else "",
                 max=e.get("b", ""))
        note = QLabel(text)
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet(f"color:{palette('text_muted')};font-size:{scaled(11, self)}px;")
        return note

    def _group_bubble(self, e: dict) -> QWidget:
        """One text: mine on the right (accent), the others' on the left with who said it."""
        mine = e["dir"] == "out"
        dlg = _dlg()
        card = QFrame()
        card.setObjectName("p2p_friend_bubble")
        fg = palette("accent_text") if mine else palette("text")
        bg = palette("accent") if mine else palette("bg_elevated")
        card.setStyleSheet(f"#p2p_friend_bubble {{ background-color: {bg}; border-radius: 10px; }}")
        card.setMaximumWidth(scaled(440, self))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)
        sender = self._who(e["from"]) if not mine else t("p2p.groups_you")
        if not mine:
            who = QLabel(sender)
            who.setTextFormat(Qt.TextFormat.PlainText)
            who.setStyleSheet(f"color:{palette('accent')};font-weight:700;font-size:{scaled(11, self)}px;")
            lay.addWidget(who)
        parsed = markup.parse(e["b"])
        link = fg if mine else palette("accent")
        body = QLabel(markup.to_html(parsed, fg=fg, muted=dlg._mix(bg, fg, 0.65), link=link,
                                     code_bg=dlg._mix(bg, fg, 0.18), code_fg=fg))
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(False)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        from PySide6.QtGui import QColor, QPalette
        pal = body.palette()
        pal.setColor(QPalette.ColorRole.Link, QColor(link))
        pal.setColor(QPalette.ColorRole.LinkVisited, QColor(link))
        body.setPalette(pal)
        body.setStyleSheet(f"color:{fg};")
        links = parsed.links
        body.linkActivated.connect(lambda href, links=links, name=sender: self._open_link(href, links, name))
        lay.addWidget(body)
        meta = QLabel()
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        when = time.strftime("%H:%M", time.localtime(e["ts"]))
        fresh = "  ·  " + t("p2p.friends_message_new") if e["id"] in self._group_new else ""
        mark, tip = "", ""
        if mine:
            have, total, lost, out = _delivery(e)
            if total:
                mark = ("✓✓" if have == total else "✓" if out else "⏳") + f" {have}/{total}"
                lines = []
                for pid, d in (e.get("dv") or {}).items():
                    state = ("✓✓" if d["ok"] else "✗" if d["x"] else "✓" if d["w"] else "⏳")
                    lines.append(f"{state} {self._who(pid)}")
                tip = "\n".join(lines)
                if lost:
                    tip += "\n" + t("p2p.groups_lost_tip")
        meta.setText(f"{when}{fresh}  {mark}".rstrip())
        meta.setToolTip(tip)
        lay.addWidget(meta)
        return self._aligned(card, mine)

    @staticmethod
    def _aligned(card: QWidget, mine: bool) -> QWidget:
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

    # attachments ----------------------------------------------------------

    def _group_card(self, mine: bool) -> tuple:
        card = QFrame()
        card.setObjectName("p2p_friend_file")
        fg = palette("accent_text") if mine else palette("text")
        bg = palette("accent") if mine else palette("bg_elevated")
        card.setStyleSheet(f"#p2p_friend_file {{ background-color: {bg}; border-radius: 10px; }}")
        card.setMaximumWidth(scaled(440, self))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 6)
        lay.setSpacing(3)
        return card, lay, fg

    def _file_head(self, lay, fg, info: dict, new: bool):
        title = QLabel("📎 " + str(info.get("l", "")))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title.setStyleSheet(f"font-weight:700;color:{fg};")
        lay.addWidget(title)
        count = int(info.get("c") or 1)
        size = _a.human_size(info.get("s", 0))
        sub = size if count <= 1 else t("p2p.friends_file_files", count=count) + "  ·  " + size
        if new:
            sub += "  ·  " + t("p2p.friends_message_new")
        sub_lbl = QLabel(sub)
        sub_lbl.setStyleSheet(f"color:{fg};font-size:{scaled(11, self)}px;")
        lay.addWidget(sub_lbl)

    def _group_file_in(self, e: dict) -> QWidget:
        """An attachment a member offered the group: Accept takes it from them alone, as a friend's."""
        m = self._svc.group_file_message(e)
        state = (m.attachment or {}).get("state", "failed")
        card, lay, fg = self._group_card(False)
        self._file_head(lay, fg, e["f"], e["id"] in self._group_new)
        sender = QLabel(t("p2p.groups_file_from", name=self._who(e["from"])))
        sender.setTextFormat(Qt.TextFormat.PlainText)
        sender.setStyleSheet(f"color:{fg};font-size:{scaled(11, self)}px;")
        lay.addWidget(sender)
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
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        fid, mid = m.friend_id, m.id

        def add(text, slot, primary=False):
            b = QPushButton(text)
            if primary:
                b.setObjectName("primary_btn")
            b.clicked.connect(lambda _=False: slot())
            buttons.addWidget(b)
            return b
        if state == "offered":
            add(t("p2p.friends_file_accept"), lambda: self._accept_group_file(fid, mid), primary=True).setToolTip(
                t("p2p.groups_file_accept_tip", name=self._who(fid)))
            add(t("p2p.friends_file_decline"), lambda: self._decline_group_file(fid, mid))
        elif state in ("waiting", "receiving"):
            add(t("p2p.friends_file_cancel"), lambda: self._cancel_group_file(fid, mid, state))
        elif state == "saved" and self._saved_path(m.attachment) is not None:
            add(t("p2p.friends_file_show"), lambda: self._show_in_folder(m.attachment))
        buttons.addStretch(1)
        lay.addLayout(buttons)
        meta = QLabel(time.strftime("%H:%M", time.localtime(e["ts"])))
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        lay.addWidget(meta)
        self._group_xfer[mid] = (m, status, bar, speed, fid)
        self._refresh_group_file(mid)
        return self._aligned(card, False)

    def _group_file_out(self, entries: list) -> QWidget:
        """What I offered the group: one card, a line for every member."""
        card, lay, fg = self._group_card(True)
        self._file_head(lay, fg, entries[0]["f"], False)
        status = QLabel()
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        status.setStyleSheet(f"color:{fg};")
        lay.addWidget(status)
        live = [x for x in entries if x.get("fs") in ("offered", "sending") and x.get("w")]
        if live:
            buttons = QHBoxLayout()
            b = QPushButton(t("p2p.groups_file_withdraw"))
            b.setToolTip(t("p2p.groups_file_withdraw_tip"))
            b.clicked.connect(lambda _=False, batch=entries[0]["batch"]: self._withdraw_offer(batch))
            buttons.addWidget(b)
            buttons.addStretch(1)
            lay.addLayout(buttons)
        meta = QLabel(time.strftime("%H:%M", time.localtime(entries[0]["ts"])))
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.setStyleSheet(f"color:{fg};font-size:{scaled(10, self)}px;")
        lay.addWidget(meta)
        self._group_out[entries[0]["batch"]] = (entries, status)
        self._refresh_group_out(entries[0]["batch"])
        return self._aligned(card, True)

    def _member_file_line(self, e: dict) -> str:
        """Where an attachment I offered is for one member."""
        name = self._who(e["to"])
        state = e.get("fs", "failed")
        if state == "sending":
            info = self._svc.transfer_info(e["to"], e.get("w", ""))
            if info and "prep" in info:
                return t("p2p.groups_file_m_preparing", name=name, pct=int(info["prep"]))
            if info and "pct" in info:
                return t("p2p.groups_file_m_sending", name=name, pct=int(info["pct"]))
            return t("p2p.groups_file_m_sending_wait", name=name)
        if state == "expired" and e.get("fw") == "queue":
            return t("p2p.groups_file_m_missed", name=name)
        key = f"p2p.groups_file_m_{state}"
        text = t(key, name=name)
        return text if text != key else f"{name}: {state}"

    def _refresh_group_out(self, batch: str):
        entry = self._group_out.get(batch)
        if entry is None:
            return
        entries, status = entry
        fresh = {x["id"]: x for x in self._svc.group_log(self._group_id)}
        status.setText("\n".join(self._member_file_line(fresh.get(x["id"], x)) for x in entries))

    def _refresh_group_file(self, mid: str):
        entry = self._group_xfer.get(mid)
        if entry is None:
            return
        m, status, bar, speed, fid = entry
        info = self._svc.transfer_info(fid, mid)
        fake = SimpleNamespace(display=self._who(fid))
        status.setText(self._file_status(m, fake, info))
        if info and "pct" in info:
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

    def _update_group_files(self):
        """Live words and figures for the attachments on show (every second)."""
        for mid in list(self._group_xfer):
            self._refresh_group_file(mid)
        for batch in list(self._group_out):
            self._refresh_group_out(batch)

    def _accept_group_file(self, fid: str, mid: str):
        try:
            self._svc.accept_file(fid, mid)
        except FriendError as e:
            key = f"p2p.friends_file_err_{str(e).replace(' ', '_')}"
            text = t(key)
            self._say(text if text != key else t("p2p.friends_file_err_error"), warn=True)
        self._group_sig = None
        self._fill_group()

    def _decline_group_file(self, fid: str, mid: str):
        self._svc.decline_file(fid, mid)
        self._group_sig = None
        self._fill_group()

    def _cancel_group_file(self, fid: str, mid: str, state: str):
        if state == "receiving":
            from ui.modal_helpers import question_window_modal
            with self._modal():
                reply = question_window_modal(
                    self, t("p2p.friends_title"), t("p2p.friends_file_cancel_confirm", name=self._who(fid)),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes or self._svc.locked:
                return
        self._svc.cancel_file(fid, mid)
        self._group_sig = None
        self._fill_group()

    def _withdraw_offer(self, batch: str):
        """Take back an offer from everybody it is still open to; a transfer under way is stopped
        (after asking) and the member told."""
        g = self._svc.group(self._group_id)
        if g is None:
            return
        mine = [e for e in g.log if e["kind"] == "file" and e["dir"] == "out" and e["batch"] == batch
                and e.get("fs") in ("offered", "sending") and e.get("w")]
        if any(e["fs"] == "sending" for e in mine):
            from ui.modal_helpers import question_window_modal
            with self._modal():
                reply = question_window_modal(
                    self, t("p2p.friends_title"), t("p2p.groups_file_withdraw_confirm"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes or self._svc.locked:
                return
        for e in mine:
            self._svc.cancel_file(e["to"], e["w"])
        self._group_sig = None
        self._fill_group()

    # writing ----------------------------------------------------------------

    def _update_group_compose(self):
        if self._svc.locked:
            return
        g = self._svc.group(self._group_id)
        active = g is not None and g.state == "active"
        body = self._group_compose.toPlainText()
        clean = _f.clean_text(body)
        used = _f.text_bytes(clean)
        too_long = used > _g.MAX_GROUP_TEXT_BYTES
        self._group_counter.setText(t("p2p.friends_compose_counter", used=used, max=_g.MAX_GROUP_TEXT_BYTES))
        self._group_counter.setStyleSheet(
            f"color:{palette('warning') if too_long else palette('text_secondary')};")
        note = ""
        if too_long:
            note = t("p2p.groups_compose_too_long", used=used, max=_g.MAX_GROUP_TEXT_BYTES)
        elif g is not None and not active:
            note = self._group_status_text(g)
        self._group_send_btn.setEnabled(active and bool(clean) and not too_long)
        self._group_compose.setEnabled(active)
        self._group_attach_btn.setEnabled(active)
        self._group_note.setText(note)
        self._group_note.setVisible(bool(note))

    def _send_group_text(self):
        if not self._group_send_btn.isEnabled():
            return
        try:
            self._svc.send_group_text(self._group_id, self._group_compose.toPlainText())
        except FriendError as e:
            self._group_note.setText(self._group_error(e))
            self._group_note.setVisible(True)
            return
        self._group_compose.clear()
        self._fill_group(stick=True)
        self._update_group_compose()

    def _clear_group_log(self):
        g = self._svc.group(self._group_id)
        if g is None:
            return
        from ui.modal_helpers import question_window_modal
        gid = g.id
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.groups_clear_confirm", name=g.name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and not self._svc.locked and self._svc.group(gid) is not None:
            self._svc.clear_group_log(gid)
            self._group_sig = None
            if self._group_id == gid:
                self._fill_group(stick=True)

    # attaching ---------------------------------------------------------------

    def _can_take_group_drop(self) -> bool:
        g = None if self._svc.locked else self._svc.group(self._group_id)
        return (self._stack.currentIndex() == PAGE_GROUP and g is not None and g.state == "active"
                and len(g.members) > 1)

    def _show_group_drop_hint(self, on: bool):
        g = self._svc.group(self._group_id)
        if on and g is not None:
            self._group_drop_lbl.setText(t("p2p.groups_drop_hint", name=g.name))
        self._group_drop_lbl.setVisible(bool(on))

    def _on_group_dropped(self, paths: list):
        if self._can_take_group_drop():
            gid = self._group_id
            self._later(0, lambda paths=list(paths): self._offer_group_paths(paths, gid))

    def _group_attach(self):
        """+: pick a file or folder, see exactly what would be offered, offer it to the group."""
        gid = self._group_id
        if not self._can_take_group_drop():
            return
        with self._modal():
            from ui.widgets.file_pickers import pick_file_or_folder
            path = pick_file_or_folder(self, t("p2p.friends_attach_pick"))
            if path:
                self._offer_group_paths([path], gid)

    def _offer_group_paths(self, paths: list, gid: str = ""):
        """Offer these files and folders to the group: what would go (any file can be ticked off),
        a refusal for the kinds that are never sent, a word on who will see this PC's address, then
        the offer. Nothing is sent now - each member gets a card and decides."""
        gid = gid or self._group_id
        g = self._svc.group(gid)
        if g is None or self._svc.locked or not paths or self._closing or g.state != "active":
            return
        with self._modal():
            sources = [Path(p) for p in paths]
            from ui.dialogs.p2p_send_confirm_dialog import P2pSendConfirmDialog
            confirm = P2pSendConfirmDialog(sources if len(sources) > 1 else sources[0], self)
            if confirm.exec() != QDialog.DialogCode.Accepted:
                return
            files = confirm.selected_files()
            if not files or self._svc.locked or self._svc.group(gid) is None:
                return
            files = [(str(p), str(rel)) for p, rel in files]
            blocked = _a.blocked_names(files)
            if blocked:
                shown = "\n".join(blocked[:5]) + ("\n…" if len(blocked) > 5 else "")
                self._say(t("p2p.friends_attach_blocked", names=shown), warn=True)
                return
            if not self._attach_warned:
                from ui.modal_helpers import question_window_modal
                reply = question_window_modal(
                    self, t("p2p.friends_title"), t("p2p.groups_attach_warning", name=g.name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes or self._svc.locked:
                    return
                self._attach_warned = True
            label = sources[0].name if len(sources) == 1 else t("p2p.send_confirm_items", count=len(sources))
            try:
                self._svc.offer_group_file(gid, files, label)
            except FriendError as e:
                key = f"p2p.groups_attach_err_{str(e).replace(' ', '_')}"
                text = t(key)
                if text == key:
                    key = f"p2p.friends_attach_err_{str(e).replace(' ', '_')}"
                    text = t(key)
                self._say(text if text != key else t("p2p.friends_attach_err_error"), warn=True)
                return
        self._group_sig = None
        if self._group_id == gid:
            self._fill_group(stick=True)
            self._update_group_compose()

    # ── the members ──────────────────────────────────────────────────────

    def _fill_members(self):
        svc = self._svc
        g = svc.group(self._group_id)
        if g is None:
            return
        book = svc.book
        me = g.me
        sig = (g.id, g.name, g.state, g.max_members, g.host_gone, tuple(
            (m.id, m.role, m.inviter, m.quota, m.kicker, len(g.used.get(m.id, [])), svc.member_info(m.id)["name"],
             svc.member_info(m.id)["friend"]) for m in g.members.values()),
            tuple(sorted(g.pending)), svc.my_id())
        if sig == self._members_sig:
            return
        focused = self._window.focusWidget()
        if isinstance(focused, QSpinBox) and self._members_body.isAncestorOf(focused):
            return                      # (somebody is typing a number: not under their hands)
        self._members_sig = sig
        _dlg()._clear(self._members_box)
        self._members_title.setText("👥 " + g.name)
        role = book.role(g, me)
        info = t("p2p.groups_members_info", count=len(g.members), max=g.max_members)
        if g.pending:
            info += "  ·  " + t("p2p.groups_members_waiting", count=len(g.pending))
        self._members_info.setText(info)
        # Host first, then administrators, then the rest in the order they came.
        order = {"host": 0, "admin": 1, "member": 2}
        for m in sorted(g.members.values(), key=lambda m: (order.get(book.role(g, m.id), 3), m.since)):
            self._members_box.addWidget(self._member_card(g, m))
        for pid in g.pending:
            wait = QLabel(t("p2p.groups_pending_line", name=self._who(pid)))
            wait.setTextFormat(Qt.TextFormat.PlainText)
            wait.setWordWrap(True)
            wait.setStyleSheet(f"{self._muted}font-style:italic;")
            self._members_box.addWidget(wait)
        active = g.state == "active"
        manager = role in ("host", "admin")
        self._invite_btn.setVisible(active)
        self._invite_btn.setEnabled(active and book.can_invite(g, me) and not book.is_full(g))
        self._rename_group_btn.setVisible(active and manager)
        self._limit_btn.setVisible(active and role == "host" and g.max_members < _g.MAX_MEMBERS)
        self._leave_group_btn.setVisible(True)
        self._leave_group_btn.setText(t("p2p.groups_leave") if active else t("p2p.groups_delete"))
        self._dissolve_btn.setVisible(active and role == "host")

    def _member_card(self, g, m) -> QWidget:
        svc, book, me = self._svc, self._svc.book, g.me
        card = QFrame()
        card.setObjectName("p2p_group_member")
        card.setStyleSheet(
            f"#p2p_group_member {{ border: 1px solid {palette('border_hover')}; border-radius: 6px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(3)
        head = QHBoxLayout()
        name = QLabel(self._who(m.id))
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setStyleSheet(f"font-weight:600;{self._ink}")
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(name, 1)
        role_text = self._role_text(g, m.id)
        if role_text:
            tag = QLabel(role_text)
            tag.setStyleSheet(f"color:{palette('accent')};font-weight:700;font-size:{scaled(11, self)}px;")
            head.addWidget(tag)
        lay.addLayout(head)
        role = book.role(g, m.id)
        used = len(g.used.get(m.id, []))
        if role == "member" and m.inviter:
            lay.addWidget(self._small(t("p2p.groups_member_may_invite", used=used, quota=m.quota)))
        elif role == "admin":
            lay.addWidget(self._small(t("p2p.groups_member_unlimited")))
        if role == "member" and m.kicker:
            lay.addWidget(self._small(t("p2p.groups_member_kicker")))
        if m.id == me or g.state != "active":
            return card
        # Two short rows rather than one long one: in a narrow panel, and in a language with longer
        # words, the controls of a member would not fit side by side.
        top, middle, bottom = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        top.setSpacing(8)
        middle.setSpacing(8)
        bottom.setSpacing(8)
        if book.can_appoint(g, me, m.id):
            admin = QCheckBox(t("p2p.groups_ctl_admin"))
            admin.setToolTip(t("p2p.groups_ctl_admin_tip"))
            admin.setChecked(role == "admin")
            admin.toggled.connect(lambda on, mid=m.id: self._set_admin(mid, on))
            top.addWidget(admin)
        top.addStretch(1)
        if book.can_kick(g, me, m.id):
            kick = QPushButton(t("p2p.groups_ctl_kick"))
            kick.setToolTip(t("p2p.groups_ctl_kick_tip"))
            kick.clicked.connect(lambda _=False, mid=m.id: self._kick(mid))
            top.addWidget(kick)
        if book.can_permit_kick(g, me, m.id):
            right = QCheckBox(t("p2p.groups_ctl_kickright"))
            right.setToolTip(t("p2p.groups_ctl_kickright_tip"))
            right.setChecked(bool(m.kicker))
            right.toggled.connect(lambda on, mid=m.id: self._set_kick_right(mid, on))
            middle.addWidget(right)
            middle.addStretch(1)
        if book.can_grant(g, me, m.id):
            may = QCheckBox(t("p2p.groups_ctl_invite"))
            may.setToolTip(t("p2p.groups_ctl_invite_tip"))
            may.setChecked(bool(m.inviter))
            spin = QSpinBox()
            spin.setRange(1, _f.MAX_QUOTA)
            spin.setValue(max(1, m.quota) if m.inviter else 3)
            spin.setEnabled(bool(m.inviter))
            spin.setToolTip(t("p2p.groups_ctl_quota_tip"))
            may.toggled.connect(lambda on, mid=m.id, spin=spin: (spin.setEnabled(on),
                                                                 self._grant(mid, spin.value() if on else 0)))
            spin.valueChanged.connect(lambda v, mid=m.id: self._grant_later(mid, v))
            bottom.addWidget(may)
            bottom.addWidget(spin)
            bottom.addStretch(1)
        if top.count() > 1:
            lay.addLayout(top)
        if middle.count():
            lay.addLayout(middle)
        if bottom.count():
            lay.addLayout(bottom)
        return card

    def _small(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"{self._muted}font-size:{scaled(11, self)}px;")
        return lbl

    def _act(self, fn, *args) -> bool:
        """Run a group action; a refusal is told. True when it went through."""
        try:
            fn(*args)
        except FriendError as e:
            self._say(self._group_error(e), warn=True)
            self._members_sig = None
            self._fill_members()
            return False
        return True

    def _set_admin(self, mid: str, on: bool):
        self._act(self._svc.set_group_admin, self._group_id, mid, bool(on))

    def _set_kick_right(self, mid: str, on: bool):
        self._act(self._svc.set_kick_right, self._group_id, mid, bool(on))

    def _raise_limit(self):
        """Ask for the new most (above the present one, up to what a group can hold) and tell the group."""
        g = self._svc.group(self._group_id)
        if g is None or g.max_members >= _g.MAX_MEMBERS:
            return
        gid = g.id
        with self._modal():
            n, ok = QInputDialog.getInt(self, t("p2p.groups_limit_title"),
                                        t("p2p.groups_limit_prompt", max=g.max_members, most=_g.MAX_MEMBERS),
                                        g.max_members + 1, g.max_members + 1, _g.MAX_MEMBERS)
        if ok and not self._svc.locked and self._svc.group(gid) is not None:
            self._act(self._svc.raise_group_limit, gid, n)

    def _grant(self, mid: str, quota: int, gid: str = ""):
        gid = gid or self._group_id
        timer = self._grant_timers.pop((gid, mid), None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._grant_due.pop((gid, mid), None)
        self._act(self._svc.grant_invite, gid, mid, int(quota))

    def _grant_later(self, mid: str, quota: int):
        """The number was changed: sent once it has stopped changing (the spin box is stepped
        through, and every step is told to the whole group). For the group that is on show NOW -
        leaving the page before it goes does not make it another group's."""
        from PySide6.QtCore import QTimer
        gid = self._group_id
        old = self._grant_timers.pop((gid, mid), None)
        if old is not None:
            old.stop()
            old.deleteLater()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._grant(mid, quota, gid))
        self._grant_timers[(gid, mid)] = timer
        self._grant_due[(gid, mid)] = quota
        timer.start(700)

    def _flush_grants(self, say: bool = True):
        """What was changed a moment ago and is not sent yet goes now - the panel is closing, and a
        change waiting for its 700 ms must not be lost with it. (Not while the list is locked: nothing
        can be sent then, and the change is dropped.) A refusal is told when *say*."""
        due, self._grant_due = self._grant_due, {}
        timers, self._grant_timers = self._grant_timers, {}
        for timer in timers.values():
            timer.stop()
            timer.deleteLater()
        if self._svc.locked:
            return
        for (gid, mid), quota in due.items():
            if self._svc.group(gid) is None:
                continue                    # (gone since: left, closed or taken out of)
            try:
                self._svc.grant_invite(gid, mid, int(quota))
            except FriendError as e:
                if say:
                    self._say(self._group_error(e), warn=True)

    def _kick(self, mid: str):
        gid = self._group_id
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.groups_kick_confirm", name=self._who(mid)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and not self._svc.locked:
            self._act(self._svc.kick_member, gid, mid)

    def _invite_menu(self):
        g = self._svc.group(self._group_id)
        if g is None:
            return
        gid = g.id
        menu = QMenu(self)
        people = [f for f in self._svc.friends() if f.id not in g.members and f.id not in g.pending
                  and (f.id not in g.banned or self._svc.book.role(g, g.me) in ("host", "admin"))]
        if not people:
            menu.addAction(t("p2p.groups_invite_none")).setEnabled(False)
        for f in people:
            menu.addAction(f.display, lambda fid=f.id: self._act(self._svc.invite_to_group, gid, fid))
        with self._modal():
            menu.exec(self._invite_btn.mapToGlobal(self._invite_btn.rect().bottomLeft()))
        self._members_sig = None
        self._fill_members()

    def _rename_group(self):
        g = self._svc.group(self._group_id)
        if g is None:
            return
        gid = g.id
        with self._modal():
            text, ok = QInputDialog.getText(self, t("p2p.groups_rename"), t("p2p.groups_rename_prompt"), text=g.name)
        if ok and not self._svc.locked and self._svc.group(gid) is not None:
            self._act(self._svc.rename_group, gid, text)

    def _leave_group(self, gid: str = ""):
        gid = gid if isinstance(gid, str) and gid else self._group_id
        g = self._svc.group(gid)
        if g is None:
            return
        if g.state != "active":
            self._delete_group(gid)
            return
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.groups_leave_confirm", name=g.name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and not self._svc.locked and self._svc.group(gid) is not None:
            self._act(self._svc.leave_group, gid)
            if self._group_id == gid:
                self._group_back()

    def _delete_group(self, gid: str):
        g = self._svc.group(gid)
        if g is None:
            return
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.groups_delete_confirm", name=g.name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and not self._svc.locked:
            self._act(self._svc.delete_group, gid)
            if self._group_id == gid:
                self._group_back()

    def _dissolve_group(self):
        g = self._svc.group(self._group_id)
        if g is None:
            return
        gid = g.id
        from ui.modal_helpers import question_window_modal
        with self._modal():
            reply = question_window_modal(
                self, t("p2p.friends_title"), t("p2p.groups_dissolve_confirm", name=g.name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and not self._svc.locked and self._svc.group(gid) is not None:
            if self._act(self._svc.dissolve_group, gid):
                self._group_back()
