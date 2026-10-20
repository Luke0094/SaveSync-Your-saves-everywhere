"""SaveSync - group chats, kept between the members themselves.

A group is a set of people who each hold the others' keys. There is no server and no
single place the conversation lives: every member keeps the group's state and log on
their own PC, and a message to the group is a message to each of the others, sent over
the same pairwise mailboxes friends use (core.p2p.friends, core.p2p.friend_net) - sealed
for that one member, exactly as a direct message is, so what a member says is readable by
the members and nobody else. A member who is not a friend of yours is a "hidden" friend:
in no list, and never able to send you anything but this group's traffic (and the answer
to an attachment you offered them). So the chat goes on whoever is away, the host included.

WHO MAY DO WHAT is decided by who a message came from, which the pairwise sealing proves
(only the two friends can make a message that opens). There are three roles:

- the HOST (whoever made the group) can do everything: kick, rename, allow members to
  invite (and how many), make members administrators and take that away, and close the
  group (dissolve: everybody is taken out);
- an ADMINISTRATOR (made so by the host) can do all of that against ordinary members -
  kick, allow to invite, rename, invite without a number - but not against the host, who
  cannot be touched, and not against another administrator: only the host removes one. An
  administrator (and the host) can also give an ordinary member the right to TAKE OUT other
  ordinary members - not administrators, not the host, and not anybody else who holds that
  right: it works downwards only;
- an INVITER (an ordinary member the host or an administrator allowed, for so many people in
  all) asks a friend of theirs; when the friend accepts, the inviter tells the friend the
  roster and tells every member who joined. A member believes that only from somebody they
  were told may invite, and only within that number;
- a MEMBER can say things, offer attachments, and leave.

The members and the roles are told to everybody directly by whoever has the power; nobody
acts on a relayed claim of anyone's word. A kicked person (or one who left) can be asked
back; a kicked one only by the host or an administrator. A group needs the host only for
the host's own powers: when the host leaves, administrators go on, and without any the group
runs on as it is, nobody to kick or to allow.

A group holds at most as many members as it was made for (its host chooses at creation, up to
MAX_MEMBERS - each member is a mailbox everybody reads), and the host can raise that later, never
lower it. It carries text and
attachments. An attachment is offered to each member separately, on their own stream, and
goes - as in a direct conversation - from the PC that offered it to each PC that accepts it,
over a direct connection: both then see each other's address, as friends do.

What this does not do, on purpose: a new member takes the inviter's word for the roster
(everyone else then speaks for themselves), an inviter who lies selectively can let one
more in than their number to some of the members, every member sees every member's key
and name (a name is a member's own word: it is shown with the start of their key), and
there is no history for newcomers.

EARLY MESSAGES. Streams are separate: a member can hear from somebody before they have
been told who that somebody is (the roster, an "add"). A message about or from somebody not
known yet is HELD (a small list per group, bounded, a day at most) and acted on once the
roster or the "add" that makes it make sense has come - it is never simply lost, because
the stream has already counted it as received.

THE QUEUE. A pair's message stream is numbered and acknowledged and never drops a text,
which suits a conversation of two and not a group: one member away for a week would fill
the stream and block the rest. So group traffic waits in a queue of its own
(store.group_outq) and goes into a pair's stream a couple at a time (MAX_INFLIGHT), leaving
room for the direct conversation. What waits too long for a member who is away is dropped
from the queue - chat and offers only, never a message that changes the group (a join, a kick,
a permission): those wait for as long as it takes. Every group message carries its sender's
own number (per group, per time they joined), so the receiver can say "3 messages from X
did not reach you".
"""
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from core.p2p import friends as _f
from core.p2p.friends import (DEFAULT_GROUP_MEMBERS, FriendError, MAX_GROUP_MEMBERS, MAX_QUOTA,
                              MAX_ROSTER_CHUNK, MAX_TEXT_BYTES, MIN_GROUP_MEMBERS, _clean_name,
                              clean_text, is_member_id, text_bytes)

logger = logging.getLogger(__name__)

MAX_MEMBERS = MAX_GROUP_MEMBERS
MAX_GROUPS = 20
INVITE_SECONDS = 24 * 3600          # an invitation left unanswered is over after this
MAX_INVITES_IN = 20                 # invitations waiting for my answer
MAX_LOG = 300
# What a member can say at once. A slot holds 900 bytes, the hello (with a name of up to 40
# characters) and the acknowledgement go in every one, and room is kept for a token: this is
# what still fits with the longest names there can be. (A direct text may be 400.)
MAX_GROUP_TEXT_BYTES = 270
MAX_INFLIGHT = 2                    # group messages unacknowledged on one pair's stream (a friend's: 1)
HOLD_MAX = 30                       # early messages kept per group
_HOLD_CHAT = ("m", "fo")            # what goes first when there is no room: talk, not who may do what
HOLD_SECONDS = 24 * 3600
JOIN_SECONDS = INVITE_SECONDS       # the roster has this long to come after I say yes
MAX_OUT_CHAT = 20                   # chat messages and offers waiting for one member
MAX_OUT = 80                        # everything waiting for one member
ROSTER_WINDOW = 3600                # more of the roster is taken from the inviter for this long after the first
CTRL_BODY = "👥"                    # what a message that is not chat says (a text needs a body)
ROLES = ("host", "admin", "member")


class GroupError(FriendError):
    """Why a group action cannot be done ("not allowed", "full", "no name"...)."""


@dataclass
class Member:
    id: str
    role: str = "member"            # "host" | "admin" | "member"
    inviter: bool = False           # may invite (the host and administrators always may)
    quota: int = 0                  # how many people in all (the host and administrators: no limit)
    by: str = ""                    # who invited them
    since: float = 0.0
    kicker: bool = False            # may take out ordinary members (an ordinary member allowed to; others are above that)


@dataclass
class Group:
    id: str
    name: str
    host: str                       # the host's id - kept after they have left (host_gone)
    me: str
    created: float
    members: dict = field(default_factory=dict)     # id -> Member (me included)
    # "joining" (accepted, roster not here yet) | "active" | "kicked" | "failed" (the roster never came)
    state: str = "active"
    host_gone: bool = False
    max_members: int = DEFAULT_GROUP_MEMBERS        # chosen when the group was made
    banned: list = field(default_factory=list)
    used: dict = field(default_factory=dict)        # inviter id -> [ids they let in, as I know]
    pending: dict = field(default_factory=dict)     # invitee id -> when I invited them
    log: list = field(default_factory=list)
    my_epoch: str = ""              # my numbering starts afresh each time I join
    my_seq: int = 0
    rx: dict = field(default_factory=dict)          # sender id -> [their epoch, the last number of theirs I have]
    joined_by: str = ""             # who invited me (for the roster)
    roster_until: float = 0.0       # 0: until the first part of the roster has come
    silenced: bool = False
    held: list = field(default_factory=list)        # early messages: {"f": sender, "g": info, "b": text, "t": when, "x": extra}


# ── what is kept ─────────────────────────────────────────────────────────────

def _num(x, default=0):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _member_to_dict(m: Member) -> dict:
    return {"id": m.id, "r": m.role, "iv": 1 if m.inviter else 0, "q": m.quota, "kk": 1 if m.kicker else 0,
            "by": m.by, "t": m.since}


def _member_from_dict(d) -> Optional[Member]:
    if not (isinstance(d, dict) and is_member_id(d.get("id"))):
        return None
    by = d.get("by")
    return Member(id=d["id"], role=d["r"] if d.get("r") in ROLES else "member",
                  inviter=bool(d.get("iv")), quota=max(0, min(MAX_QUOTA, int(_num(d.get("q"))))),
                  kicker=bool(d.get("kk")) and d.get("r") == "member",
                  by=by if is_member_id(by) else "", since=float(_num(d.get("t"))))


_EVENTS = ("create", "join", "left", "kick", "grant", "rename", "host_left", "admin", "unadmin",
           "kick_right", "kick_right_off", "limit")


def _entry_from_dict(e) -> Optional[dict]:
    if not (isinstance(e, dict) and e.get("dir") in ("in", "out", "sys")
            and e.get("kind") in ("text", "gap", "sys", "file") and isinstance(e.get("id"), str)):
        return None
    sender = e.get("from")
    entry = {"id": e["id"][:60], "from": sender if is_member_id(sender) else "", "dir": e["dir"],
             "kind": e["kind"], "b": str(e.get("b") or "")[:2000], "ts": float(_num(e.get("ts"))),
             "read": bool(e.get("read", True)), "seq": int(_num(e.get("seq"))),
             "missed": int(_num(e.get("missed")))}
    dv = e.get("dv")
    if isinstance(dv, dict):
        entry["dv"] = {p: {"w": str(v.get("w") or "")[:40], "ok": bool(v.get("ok")), "x": bool(v.get("x"))}
                       for p, v in list(dv.items())[:MAX_MEMBERS] if is_member_id(p) and isinstance(v, dict)}
    if e["kind"] == "sys":
        x = e.get("x")
        if not (isinstance(x, dict) and x.get("e") in _EVENTS):
            return None
        entry["x"] = {"e": x["e"], "w": x["w"] if is_member_id(x.get("w")) else "",
                      "by": x["by"] if is_member_id(x.get("by")) else ""}
    if e["kind"] == "file":
        # An attachment, as in a direct conversation (core.p2p.friends._chat_entry_from_dict), with
        # who it is with ("from" for one offered to me, "to" for one of mine, one per member), the
        # id of the offer in THEIR stream ("w") and which offer of mine it is part of ("batch").
        info = _f.clean_file_info(e.get("f"))
        if info is None or e["dir"] == "sys":
            return None
        to = e.get("to")
        state = e.get("fs") if e.get("fs") in _f.FILE_STATES else "failed"
        entry.update(f=info, fs=state, fw=str(e.get("fw") or "")[:40], fp=_f._file_paths(e.get("fp")),
                     sd=str(e.get("sd") or "")[:600],
                     sf=[str(x)[:240] for x in (e.get("sf") or [])[:50]] if isinstance(e.get("sf"), list) else [],
                     w=str(e.get("w") or "")[:40], to=to if is_member_id(to) else "",
                     batch=str(e.get("batch") or "")[:16])
        if e["dir"] == "out" and state in ("offered", "waiting", "sending") and not entry["fp"]:
            entry["fs"] = "expired"             # the files it was made of are not on this PC
        if e["dir"] == "in":
            entry["fp"] = []
        if state in ("waiting", "receiving", "sending"):
            entry["fs"] = "offered" if entry["fs"] != "expired" else "expired"
    return entry


def _hold_from_dict(d) -> Optional[dict]:
    if not (isinstance(d, dict) and is_member_id(d.get("f")) and isinstance(d.get("b"), str)):
        return None
    info = _f.clean_group_info(d.get("g"))
    if info is None or info["k"] in ("inv", "ro", "ok", "no"):
        return None
    x = d.get("x") if isinstance(d.get("x"), dict) else {}
    extra = {"f": _f.clean_file_info(x.get("f")), "w": str(x.get("w") or "")[:40]}
    if (info["k"] == "fo") != (extra["f"] is not None):
        return None
    return {"f": d["f"], "g": info, "b": d["b"][:MAX_TEXT_BYTES], "t": float(_num(d.get("t"))), "x": extra}


def group_to_dict(g: Group) -> dict:
    return {"id": g.id, "name": g.name, "host": g.host, "me": g.me, "created": g.created,
            "members": [_member_to_dict(m) for m in g.members.values()], "st": g.state,
            "hg": 1 if g.host_gone else 0, "mx": g.max_members, "banned": list(g.banned), "used": g.used,
            "pending": g.pending, "log": g.log[-MAX_LOG:], "ep": g.my_epoch, "seq": g.my_seq,
            "rx": g.rx, "jb": g.joined_by, "ru": g.roster_until, "sl": 1 if g.silenced else 0,
            "hold": g.held[-HOLD_MAX:]}


def _clamp_members(x) -> int:
    n = x if isinstance(x, int) and not isinstance(x, bool) else DEFAULT_GROUP_MEMBERS
    return max(MIN_GROUP_MEMBERS, min(MAX_MEMBERS, n))


def group_from_dict(d) -> Optional[Group]:
    """A group from what was kept; None when it is not one (whatever else is malformed
    takes its default)."""
    try:
        if not (isinstance(d, dict) and isinstance(d.get("id"), str) and _f._GROUP_ID.fullmatch(d["id"])
                and is_member_id(d.get("host")) and is_member_id(d.get("me"))):
            return None
        name = _clean_name(d.get("name"))
        if not name:
            return None
        members = {}
        for md in (d.get("members") or [])[:MAX_MEMBERS]:
            m = _member_from_dict(md)
            if m is not None:
                members[m.id] = m
        g = Group(id=d["id"], name=name, host=d["host"], me=d["me"], created=float(_num(d.get("created"))),
                  members=members,
                  state=d.get("st") if d.get("st") in ("joining", "active", "kicked", "failed") else "active",
                  host_gone=bool(d.get("hg")), max_members=_clamp_members(d.get("mx")))
        g.banned = [b for b in (d.get("banned") or [])[:200] if is_member_id(b)]
        g.used = {k: [x for x in v if is_member_id(x)][:MAX_QUOTA + MAX_MEMBERS]
                  for k, v in (d.get("used") or {}).items()
                  if is_member_id(k) and isinstance(v, list)}
        g.pending = {k: float(_num(v)) for k, v in (d.get("pending") or {}).items() if is_member_id(k)}
        g.log = [e for e in (_entry_from_dict(x) for x in (d.get("log") or [])[-MAX_LOG:]) if e is not None]
        ep = d.get("ep")
        g.my_epoch = ep if isinstance(ep, str) and _f._GROUP_EPOCH.fullmatch(ep) else os.urandom(3).hex()
        g.my_seq = max(0, int(_num(d.get("seq"))))
        g.rx = {k: [v[0], max(0, int(_num(v[1])))] for k, v in (d.get("rx") or {}).items()
                if is_member_id(k) and isinstance(v, list) and len(v) == 2
                and isinstance(v[0], str) and _f._GROUP_EPOCH.fullmatch(v[0])}
        jb = d.get("jb")
        g.joined_by = jb if is_member_id(jb) else ""
        g.roster_until = float(_num(d.get("ru")))
        g.silenced = bool(d.get("sl"))
        g.held = [h for h in (_hold_from_dict(x) for x in (d.get("hold") or [])[-HOLD_MAX:]) if h is not None]
        return g
    except Exception:
        logger.debug("P2P groups: a stored group could not be read", exc_info=True)
        return None


def invite_from_dict(d) -> Optional[dict]:
    if not (isinstance(d, dict) and isinstance(d.get("gid"), str) and _f._GROUP_ID.fullmatch(d["gid"])
            and is_member_id(d.get("inviter")) and is_member_id(d.get("host"))):
        return None
    name = _clean_name(d.get("name"))
    if not name:
        return None
    return {"gid": d["gid"], "name": name, "host": d["host"], "inviter": d["inviter"],
            "count": max(1, min(MAX_MEMBERS, int(_num(d.get("count"), 1)))), "mx": _clamp_members(d.get("mx")),
            "hg": 1 if d.get("hg") else 0, "ts": float(_num(d.get("ts")))}


def outq_item_from_dict(d) -> Optional[dict]:
    if not (isinstance(d, dict) and is_member_id(d.get("p"))):
        return None
    g = _f.clean_group_info(d.get("g"))
    body = d.get("b")
    info = _f.clean_file_info(d.get("f")) if d.get("f") else None
    if g is None or not isinstance(body, str) or not body or (g["k"] == "fo") != (info is not None):
        return None
    item = {"p": d["p"], "g": g, "b": body[:MAX_TEXT_BYTES], "c": 1 if d.get("c") else 0,
            "e": str(d.get("e") or "")[:60], "t": float(_num(d.get("t")))}
    if info is not None:
        item["f"] = info
    return item


# ── the book ─────────────────────────────────────────────────────────────────

@dataclass
class Effects:
    """What an incoming message did that the rest of the app should hear about:
    ("invite", invitation) - an invitation to answer; ("message", group id, entry id) - a
    message or an attachment; ("changed", group id) - the members, permissions or name;
    ("kicked", group id)."""
    events: list = field(default_factory=list)


class GroupBook:
    """The groups of a FriendStore, and everything that can happen to them. It changes the
    store's lists (groups, group_invites, group_outq) and queues what is to be sent; sending
    it is the service's job (ui.p2p_friends_service) - this part knows nothing of the network."""

    def __init__(self, store):
        self.store = store

    # access ---------------------------------------------------------------
    @property
    def me(self) -> str:
        return _f.friend_id(self.store.pub)

    def groups(self) -> list:
        return list(self.store.groups)

    def get(self, gid: str) -> Optional[Group]:
        return next((g for g in self.store.groups if g.id == gid), None)

    def invites(self) -> list:
        return list(self.store.group_invites)

    def _save(self) -> None:
        self.store.dirty = True
        self.store.save()

    # who may do what ----------------------------------------------------
    @staticmethod
    def _dead(g: Group) -> bool:
        """I am not in it any more (kicked, or it never came to be)."""
        return g.state in ("kicked", "failed")

    @staticmethod
    def _is_host(g: Group, who: str) -> bool:
        return who == g.host and not g.host_gone and who in g.members

    @staticmethod
    def _is_admin(g: Group, who: str) -> bool:
        m = g.members.get(who)
        return m is not None and m.role == "admin"

    def _manager(self, g: Group, who: str) -> bool:
        """The host (while there is one) or an administrator."""
        return self._is_host(g, who) or self._is_admin(g, who)

    def _is_kicker(self, g: Group, who: str) -> bool:
        m = g.members.get(who)
        return m is not None and m.role == "member" and m.kicker

    def role(self, g: Group, who: str) -> str:
        """"host", "admin" or "member" - "" for somebody who is not in the group."""
        m = g.members.get(who)
        if m is None:
            return ""
        return "host" if self._is_host(g, who) else ("admin" if m.role == "admin" else "member")

    def remaining(self, g: Group, who: str) -> Optional[int]:
        """How many more people *who* may let in (None: no limit - the host and administrators);
        0 if none, or they may not invite. For me, what is waiting for an answer counts as let in."""
        m = g.members.get(who)
        if m is None or g.state != "active":
            return 0
        if self._manager(g, who):
            return None
        if not m.inviter:
            return 0
        counted = set(g.used.get(who, []))
        if who == g.me:
            counted |= set(g.pending)           # (somebody asked back is not another person)
        return max(0, m.quota - len(counted))

    def can_invite(self, g: Group, who: str) -> bool:
        """Whether *who* may still ask somebody in (the size of the group aside)."""
        r = self.remaining(g, who)
        return g.state == "active" and (r is None or r > 0)

    def can_kick(self, g: Group, who: str, target: str) -> bool:
        """Only downwards: the host can take out anybody but themselves; an administrator any ordinary
        member (those allowed to take members out included); an ordinary member who was allowed to,
        another ordinary member who was not."""
        if g.state != "active" or who == target or target not in g.members or target == g.host:
            return False
        t = g.members[target]
        if self._is_host(g, who):
            return True
        if self._is_admin(g, who):
            return t.role == "member"
        return self._is_kicker(g, who) and t.role == "member" and not t.kicker

    def can_permit_kick(self, g: Group, who: str, target: str) -> bool:
        """Whether *who* may decide if *target* can take members out: the host or an administrator, and
        an ordinary member as target (administrators always can, below them)."""
        return self.can_grant(g, who, target)

    def can_grant(self, g: Group, who: str, target: str) -> bool:
        """Whether *who* may decide if *target* can invite (an ordinary member: administrators
        always can)."""
        if g.state != "active" or who == target or target not in g.members:
            return False
        return self._manager(g, who) and g.members[target].role == "member"

    def can_appoint(self, g: Group, who: str, target: str) -> bool:
        """Only the host makes (or unmakes) administrators."""
        return (g.state == "active" and self._is_host(g, who) and who != target
                and target in g.members and target != g.host)

    def is_full(self, g: Group) -> bool:
        return len(g.members) + len(g.pending) >= g.max_members

    def _log(self, g: Group, entry: dict) -> None:
        g.log.append(entry)
        del g.log[:-MAX_LOG]

    def _sys(self, g: Group, event: str, who: str = "", by: str = "", now: Optional[float] = None,
             note: str = "") -> None:
        now = time.time() if now is None else now
        self._log(g, {"id": f"sys-{os.urandom(4).hex()}", "from": "", "dir": "sys", "kind": "sys", "b": note,
                      "ts": now, "read": True, "seq": 0, "missed": 0, "x": {"e": event, "w": who, "by": by}})

    def _queue(self, peer: str, g: dict, body: str = CTRL_BODY, chat: bool = False, entry: str = "",
               now: Optional[float] = None, file: Optional[dict] = None) -> None:
        """Leave a message for *peer* to go into their stream when there is room."""
        q = self.store.group_outq
        item = {"p": peer, "g": g, "b": body, "c": 1 if chat else 0, "e": entry,
                "t": time.time() if now is None else now}
        if file is not None:
            item["f"] = file
        q.append(item)
        # What waits too long for somebody who is away: chat and offers go, oldest first; a
        # message that changes the group never.
        while True:
            mine = [i for i, x in enumerate(q) if x["p"] == peer]
            chat_i = [i for i in mine if q[i]["c"]]
            if len(chat_i) > MAX_OUT_CHAT or (len(mine) > MAX_OUT and chat_i):
                self._dropped(q.pop(chat_i[0]))
            else:
                break

    def _dropped(self, item: dict) -> None:
        """A chat message or offer that will not reach *item["p"]*: the log says so."""
        g = self.get(item["g"]["i"])
        if g is None or not item["e"]:
            return
        e = next((x for x in g.log if x["id"] == item["e"]), None)
        if e is None:
            return
        if e["kind"] == "file":
            e["fs"], e["fw"], e["fp"] = "expired", "queue", []
        else:
            e.setdefault("dv", {})[item["p"]] = {"w": "", "ok": False, "x": True}

    def _tell(self, g: Group, kind: str, to, **fields) -> None:
        for peer in to:
            self._queue(peer, {"i": g.id, "k": kind, **fields})

    def _others(self, g: Group, *skip) -> list:
        return [i for i in g.members if i != g.me and i not in skip]

    # making and joining ---------------------------------------------------
    def create(self, name: str, max_members: int = DEFAULT_GROUP_MEMBERS, now: Optional[float] = None) -> Group:
        """Make a group that holds at most *max_members* people (MIN_GROUP_MEMBERS to MAX_MEMBERS),
        me the host. GroupError: "no name", "too many groups"."""
        now = time.time() if now is None else now
        name = _clean_name(name)
        if not name:
            raise GroupError("no name")
        if len(self.store.groups) >= MAX_GROUPS:
            raise GroupError("too many groups")
        me = self.me
        g = Group(id=os.urandom(8).hex(), name=name, host=me, me=me, created=now,
                  members={me: Member(me, "host", True, 0, "", now)}, my_epoch=os.urandom(3).hex(),
                  max_members=_clamp_members(max_members))
        self._sys(g, "create", me, me, now)
        self.store.groups.append(g)
        self._save()
        return g

    def invite(self, gid: str, fid: str, now: Optional[float] = None) -> None:
        """Ask the friend *fid* to join. GroupError: "no such group", "not a friend", "already in",
        "already asked", "full", "not allowed" (I may not invite, have no more to invite, or they
        were kicked and I am not the host or an administrator)."""
        now = time.time() if now is None else now
        g = self.get(gid)
        if g is None or g.state != "active":
            raise GroupError("no such group")
        f = self.store.get(fid)
        if f is None or f.hidden or fid == g.me:
            raise GroupError("not a friend")
        if fid in g.members:
            raise GroupError("already in")
        if fid in g.pending and now - g.pending[fid] < INVITE_SECONDS:
            raise GroupError("already asked")
        if self.is_full(g):
            raise GroupError("full")
        if not self.can_invite(g, g.me) or (fid in g.banned and not self._manager(g, g.me)):
            raise GroupError("not allowed")
        g.pending[fid] = now
        self._queue(fid, {"i": g.id, "k": "inv", "n": g.name, "h": g.host, "c": len(g.members),
                          "mx": g.max_members, "hg": 1 if g.host_gone else 0}, now=now)
        self._save()

    def accept_invite(self, gid: str, now: Optional[float] = None) -> Group:
        """Say yes to the invitation to *gid*: the group is here (joining) until the inviter has
        sent the roster. GroupError: "expired", "already in", "too many groups"."""
        now = time.time() if now is None else now
        rec = next((r for r in self.store.group_invites if r["gid"] == gid), None)
        if rec is None or now - rec["ts"] > INVITE_SECONDS:
            self.store.group_invites = [r for r in self.store.group_invites if r["gid"] != gid]
            raise GroupError("expired")
        old = self.get(gid)
        if old is not None and not self._dead(old):
            self.store.group_invites = [r for r in self.store.group_invites if r["gid"] != gid]
            raise GroupError("already in")
        if old is None and len(self.store.groups) >= MAX_GROUPS:
            raise GroupError("too many groups")
        me = self.me
        g = Group(id=gid, name=rec["name"], host=rec["host"], me=me, created=now, state="joining",
                  host_gone=bool(rec.get("hg")), max_members=_clamp_members(rec.get("mx")),
                  joined_by=rec["inviter"], my_epoch=os.urandom(3).hex())
        g.members[me] = Member(me, "member", False, 0, rec["inviter"], now)
        g.members[rec["inviter"]] = Member(rec["inviter"], "member", False, 0, "", 0.0)
        if not g.host_gone:
            host = g.members.setdefault(rec["host"], Member(rec["host"], "host", True, 0, "", 0.0))
            host.role, host.inviter, host.quota = "host", True, 0
        self._ensure_peer(rec["inviter"])
        if not g.host_gone:
            self._ensure_peer(rec["host"])
        if old is not None:                         # (asked back after being kicked: the log stays)
            g.log = [e for e in old.log[-MAX_LOG:] if not (e["kind"] == "file" and e.get("fs") == "offered")]
            self.store.groups = [x for x in self.store.groups if x is not old]
        self.store.groups.append(g)
        self.store.group_invites = [r for r in self.store.group_invites if r["gid"] != gid]
        self._queue(rec["inviter"], {"i": gid, "k": "ok"}, now=now)
        self._save()
        return g

    def decline_invite(self, gid: str) -> None:
        rec = next((r for r in self.store.group_invites if r["gid"] == gid), None)
        self.store.group_invites = [r for r in self.store.group_invites if r["gid"] != gid]
        if rec is not None:
            self._queue(rec["inviter"], {"i": gid, "k": "no"})
        self._save()

    def _ensure_peer(self, member_id: str) -> None:
        if member_id != self.me and is_member_id(member_id):
            try:
                self.store.ensure_peer(_f.pub_from_id(member_id))
            except FriendError:
                logger.debug("P2P groups: a member's key could not be used", exc_info=True)

    # saying things ----------------------------------------------------------
    def _speak(self, gid: str) -> Group:
        g = self.get(gid)
        if g is None:
            raise GroupError("no such group")
        if g.state != "active":
            raise GroupError("not active")
        return g

    def send(self, gid: str, text: str, now: Optional[float] = None) -> str:
        """Say *text* to the group; returns the id of the entry in the log. GroupError:
        "no such group", "not active" (kicked, or the roster has not come yet), "empty", "too long"."""
        now = time.time() if now is None else now
        g = self._speak(gid)
        body = clean_text(text)
        if not body:
            raise GroupError("empty")
        if text_bytes(body) > MAX_GROUP_TEXT_BYTES:
            raise GroupError("too long")
        g.my_seq += 1
        eid = f"{g.my_epoch}.{g.my_seq}"
        others = self._others(g)
        self._log(g, {"id": eid, "from": g.me, "dir": "out", "kind": "text", "b": body, "ts": now,
                      "read": True, "seq": g.my_seq, "missed": 0,
                      "dv": {p: {"w": "", "ok": False, "x": False} for p in others}})
        for peer in others:
            self._queue(peer, {"i": g.id, "k": "m", "e": g.my_epoch, "s": g.my_seq}, body,
                        chat=True, entry=eid, now=now)
        self._save()
        return eid

    def offer_file(self, gid: str, info: dict, paths: list, now: Optional[float] = None) -> str:
        """Offer an attachment to every member: *info* is ``{"l", "c", "s"}`` (core.p2p.friends.
        clean_file_info), *paths* ``[(path, relative path, size)]`` of the files on this PC.
        Nothing moves now: each member gets a card of their own and the files go to those who
        accept, one at a time, from this PC. Returns the id of the offer (``batch``). GroupError:
        "no such group", "not active", "not an attachment", "nobody"."""
        now = time.time() if now is None else now
        g = self._speak(gid)
        info = _f.clean_file_info(info)
        if info is None:
            raise GroupError("not an attachment")
        others = self._others(g)
        if not others:
            raise GroupError("nobody")
        g.my_seq += 1
        batch = os.urandom(4).hex()
        fallback = _f.file_fallback(info)
        fp = _f._file_paths([list(p) for p in paths or []])
        for peer in others:
            eid = f"fo.{batch}.{peer[:8]}"
            self._log(g, {"id": eid, "from": "", "to": peer, "dir": "out", "kind": "file", "b": fallback,
                          "ts": now, "read": True, "seq": g.my_seq, "missed": 0, "batch": batch, "w": "",
                          "f": info, "fs": "offered", "fw": "", "fp": [list(p) for p in fp], "sd": "", "sf": []})
            self._queue(peer, {"i": g.id, "k": "fo", "e": g.my_epoch, "s": g.my_seq}, fallback,
                        chat=True, entry=eid, now=now, file=info)
        self._save()
        return batch

    # the powers of the host and the administrators --------------------------------
    def _need_manager(self, gid: str) -> Group:
        g = self.get(gid)
        if g is None:
            raise GroupError("no such group")
        if g.state != "active" or not self._manager(g, g.me):
            raise GroupError("not allowed")
        return g

    def _need_host(self, gid: str) -> Group:
        g = self.get(gid)
        if g is None:
            raise GroupError("no such group")
        if g.state != "active" or not self._is_host(g, g.me):
            raise GroupError("not allowed")
        return g

    def grant(self, gid: str, member_id: str, quota: int, now: Optional[float] = None) -> None:
        """Let the ordinary member *member_id* invite *quota* people in all (0: no longer).
        Host or administrator. (Administrators invite without a number.)"""
        g = self._need_manager(gid)
        if member_id not in g.members:
            raise GroupError("no such member")
        if not self.can_grant(g, g.me, member_id):
            raise GroupError("not allowed")
        m = g.members[member_id]
        quota = max(0, min(MAX_QUOTA, int(quota)))
        m.inviter, m.quota = quota > 0, quota
        self._tell(g, "gr", self._others(g), w=member_id, q=quota)
        self._sys(g, "grant", member_id, g.me, now)
        self._save()

    def set_admin(self, gid: str, member_id: str, on: bool, now: Optional[float] = None) -> None:
        """Make *member_id* an administrator (or take it away). Host only."""
        g = self._need_host(gid)
        if member_id not in g.members:
            raise GroupError("no such member")
        if not self.can_appoint(g, g.me, member_id):
            raise GroupError("not allowed")
        self._set_role(g.members[member_id], bool(on))
        self._tell(g, "ad", self._others(g), w=member_id, a=1 if on else 0)
        self._sys(g, "admin" if on else "unadmin", member_id, g.me, now)
        self._save()

    @staticmethod
    def _set_role(m: Member, admin: bool) -> None:
        m.role, m.inviter, m.quota, m.kicker = ("admin", True, 0, False) if admin else ("member", False, 0, False)

    def permit_kick(self, gid: str, member_id: str, on: bool, now: Optional[float] = None) -> None:
        """Let the ordinary member *member_id* take out other ordinary members (or not any more).
        Host or administrator."""
        g = self._need_manager(gid)
        if member_id not in g.members:
            raise GroupError("no such member")
        if not self.can_permit_kick(g, g.me, member_id):
            raise GroupError("not allowed")
        g.members[member_id].kicker = bool(on)
        self._tell(g, "kp", self._others(g), w=member_id, a=1 if on else 0)
        self._sys(g, "kick_right" if on else "kick_right_off", member_id, g.me, now)
        self._save()

    def raise_limit(self, gid: str, n: int, now: Optional[float] = None) -> None:
        """Let the group hold *n* people (more than it does now, at most MAX_MEMBERS). Host only; the
        limit is never lowered. GroupError "limit" for a number that is not a rise."""
        g = self._need_host(gid)
        if not isinstance(n, int) or isinstance(n, bool) or not g.max_members < n <= MAX_MEMBERS:
            raise GroupError("limit")
        g.max_members = n
        self._tell(g, "lim", self._others(g), n=n)
        self._sys(g, "limit", "", g.me, now, note=str(n))
        self._save()

    def kick(self, gid: str, member_id: str, now: Optional[float] = None) -> None:
        """Take *member_id* out, and refuse them if anybody but the host or an administrator asks them
        in again. Only downwards (see can_kick)."""
        g = self.get(gid)
        if g is None:
            raise GroupError("no such group")
        if member_id not in g.members:
            raise GroupError("no such member")
        if not self.can_kick(g, g.me, member_id):
            raise GroupError("not allowed")
        others = self._others(g)                    # (the one going is told as well)
        self._remove_member(g, member_id, ban=True)
        self._tell(g, "kick", others, w=member_id)
        self._sys(g, "kick", member_id, g.me, now)
        self._save()

    def rename(self, gid: str, name: str, now: Optional[float] = None) -> None:
        g = self._need_manager(gid)
        name = _clean_name(name)
        if not name:
            raise GroupError("no name")
        g.name = name
        self._tell(g, "nm", self._others(g), n=name)
        self._sys(g, "rename", "", g.me, now)
        self._save()

    def dissolve(self, gid: str) -> None:
        """Close the group: everybody is taken out (each is told so, and refused if asked back
        by anyone but the host or an administrator), and it is gone from here. There is no one
        place the group lives, so this is all closing a group can mean. Host only."""
        g = self._need_host(gid)
        for peer in self._others(g):
            self._queue(peer, {"i": g.id, "k": "kick", "w": peer})
        self.store.groups = [x for x in self.store.groups if x.id != gid]
        self.cleanup_peers()
        self._save()

    def leave(self, gid: str) -> None:
        """Go out of the group: the others are told, and it is gone from here (its log with it)."""
        g = self.get(gid)
        if g is None:
            raise GroupError("no such group")
        if g.state == "active":
            self._tell(g, "lv", self._others(g))
        self.store.groups = [x for x in self.store.groups if x.id != gid]
        self.cleanup_peers()
        self._save()

    def delete(self, gid: str) -> None:
        """Throw away a group I was kicked out of."""
        self.store.groups = [x for x in self.store.groups if x.id != gid]
        self.cleanup_peers()
        self._save()

    def set_silenced(self, gid: str, on: bool) -> None:
        g = self.get(gid)
        if g is not None and g.silenced != bool(on):
            g.silenced = bool(on)
            self._save()

    def _remove_member(self, g: Group, member_id: str, ban: bool = False) -> None:
        g.members.pop(member_id, None)
        g.pending.pop(member_id, None)
        if ban and member_id not in g.banned:
            g.banned.append(member_id)
        # (what was waiting to go to them for this group will not, and nor will the files)
        self.store.group_outq = [x for x in self.store.group_outq
                                 if not (x["p"] == member_id and x["g"]["i"] == g.id)]
        for e in g.log:
            if e["kind"] == "file" and e["dir"] == "out" and e.get("to") == member_id and e.get("fs") == "offered":
                e["fs"], e["fw"], e["fp"] = "cancelled", "", []

    # what comes in --------------------------------------------------------------
    def handle(self, fid: str, g: dict, body: str, now: Optional[float] = None,
               file: Optional[dict] = None, wire: str = "") -> Effects:
        """A group message from *fid* (a key, not necessarily a friend): act on it as far as
        *fid* has the right to say it, and ignore it otherwise. *file* and *wire* are the
        attachment and the id of the message in *fid*'s stream (for an offer)."""
        now = time.time() if now is None else now
        fx = Effects()
        f = self.store.get(fid)
        if f is None:
            return fx
        extra = {"f": file, "w": wire}
        if g["k"] == "inv":
            self._on_invite(f, g, now, fx)
        else:
            grp = self.get(g["i"])
            if grp is not None:
                if self._not_yet(grp, f, g):
                    self._park(grp, f.id, g, body, now, extra)
                else:
                    self._run(grp, f, g, body, now, fx, extra)
                    if g["k"] in ("ro", "add", "lim"):
                        self._replay(grp, now, fx)      # (somebody is known now who was not, or there is room)
        self.store.dirty = True
        if fx.events:
            self.store.save()
        return fx

    def _run(self, grp, f, g, body, now, fx, extra) -> None:
        {"ok": self._on_ok, "no": self._on_no, "ro": self._on_roster, "add": self._on_add,
         "kick": self._on_kick, "gr": self._on_grant, "ad": self._on_admin, "kp": self._on_kick_right,
         "lim": self._on_limit, "lv": self._on_leave, "nm": self._on_rename, "m": self._on_message,
         "fo": self._on_file}[g["k"]](grp, f, g, body, now, fx, extra)

    def _not_yet(self, grp: Group, f, g: dict) -> bool:
        """Cannot be understood yet: the group has not come to be for me (the roster is on its
        way), or it is from, or about, somebody I have not been told of. (The host's kick is
        always acted on - a ban is a ban, whenever it is heard - and so is the rest of the
        joining itself.)"""
        kind = g["k"]
        if kind in ("ro", "ok", "no") or self._dead(grp):
            return False
        if kind == "kick":
            if self._is_host(grp, f.id):
                return False
            # (an administrator's depends on who is being taken out)
            return grp.state == "joining" or f.id not in grp.members or g["w"] not in grp.members
        if grp.state == "joining" or f.id not in grp.members:
            return True
        if kind == "add" and len(grp.members) >= grp.max_members:
            return True                         # (the host's raising of the limit may be on its way)
        return kind in ("gr", "ad", "kp") and g["w"] not in grp.members

    def _park(self, grp: Group, fid: str, g: dict, body: str, now: float, extra: dict) -> None:
        """Keep an early message till it can be understood (everything that comes while the roster is
        still on its way is early, from every member alike: nothing is dropped while there is room). When
        HOLD_MAX runs out the oldest talk goes first, from the friend who holds most - so that somebody's
        chatter cannot push out a kick, a permission or a change of the limit that is waiting."""
        grp.held.append({"f": fid, "g": g, "b": body, "t": now, "x": extra})
        while len(grp.held) > HOLD_MAX:
            counts: dict = {}
            for h in grp.held:
                counts[h["f"]] = counts.get(h["f"], 0) + 1
            top = max(counts.values())
            # (the one who holds most; of equals, the one whose oldest message is the oldest)
            who = next(h["f"] for h in grp.held if counts[h["f"]] == top)
            self._drop_held(grp, [h for h in grp.held if h["f"] == who])

    @staticmethod
    def _drop_held(grp: Group, among: list) -> None:
        """Let go of one of *among* (a friend's early messages, oldest first): the oldest talk, else the oldest."""
        victim = next((h for h in among if h["g"]["k"] in _HOLD_CHAT), among[0])
        grp.held = [h for h in grp.held if h is not victim]

    def _replay(self, grp: Group, now: float, fx) -> None:
        """Act on the early messages that can be understood now (in the order they came)."""
        for _ in range(3):
            held, grp.held = [h for h in grp.held if now - h["t"] <= HOLD_SECONDS], []
            progressed = False
            for h in held:
                f = self.store.get(h["f"])
                if f is None:
                    continue
                if self._not_yet(grp, f, h["g"]):
                    grp.held.append(h)
                    continue
                self._run(grp, f, h["g"], h["b"], now, fx, h.get("x") or {})
                progressed = True
            if not progressed:
                break

    def _on_invite(self, f, g, now, fx) -> None:
        if f.hidden or g["h"] == self.me:
            return                                  # (only a friend asks me into a group)
        gid = g["i"]
        old = self.get(gid)
        if old is not None and not self._dead(old):
            return
        recs = [r for r in self.store.group_invites if r["gid"] != gid]
        if len(recs) >= MAX_INVITES_IN:
            return
        rec = {"gid": gid, "name": g["n"], "host": g["h"], "inviter": f.id, "count": g["c"],
               "mx": g["mx"], "hg": g["hg"], "ts": now}
        self.store.group_invites = recs + [rec]
        fx.events.append(("invite", rec))

    def _on_ok(self, grp, f, g, body, now, fx, extra) -> None:
        """The friend I invited said yes: they are in, and told the roster; the rest are told they are."""
        if grp.state != "active" or f.id not in grp.pending:
            return
        asked = grp.pending.pop(f.id)
        if f.id in grp.members:
            return
        r = self.remaining(grp, grp.me)             # (my right may have gone while they thought)
        if (now - asked > INVITE_SECONDS or len(grp.members) >= grp.max_members
                or (r is not None and r <= 0 and f.id not in grp.used.get(grp.me, []))
                or (f.id in grp.banned and not self._manager(grp, grp.me))):
            self._queue(f.id, {"i": grp.id, "k": "no"})     # (so that they do not wait for ever)
            return
        grp.banned = [b for b in grp.banned if b != f.id]   # (a ban is lifted by those who can set one)
        grp.members[f.id] = Member(f.id, "member", False, 0, grp.me, now)
        if f.id not in grp.used.setdefault(grp.me, []):
            grp.used[grp.me].append(f.id)
        self._sys(grp, "join", f.id, grp.me, now)
        rows = [[m.id, "h" if self._is_host(grp, m.id) else ("a" if m.role == "admin" else "m"),
                 1 if (m.inviter or m.role == "admin") else 0, m.quota, 1 if m.kicker else 0]
                for m in grp.members.values()]
        for i in range(0, len(rows), MAX_ROSTER_CHUNK):
            self._queue(f.id, {"i": grp.id, "k": "ro", "r": rows[i:i + MAX_ROSTER_CHUNK], "mx": grp.max_members})
        self._tell(grp, "add", self._others(grp, f.id), w=f.id)
        fx.events.append(("changed", grp.id))

    def _on_no(self, grp, f, g, body, now, fx, extra) -> None:
        """Either the friend I asked said no, or the one who asked me cannot take me in after all."""
        grp.pending.pop(f.id, None)
        if grp.state == "joining" and f.id == grp.joined_by:
            grp.state = "failed"
            fx.events.append(("changed", grp.id))

    def _on_roster(self, grp, f, g, body, now, fx, extra) -> None:
        """The roster from the one who invited me (and nobody else): the first part opens the
        group, the rest is taken for a while after it."""
        if f.id != grp.joined_by or grp.state == "kicked":
            return
        if grp.state == "active" and now > grp.roster_until:
            return
        for mid, role, inviter, quota, kicker in g["r"]:
            if mid in grp.banned or (grp.host_gone and mid == grp.host):
                continue
            m = grp.members.get(mid)
            if m is None:
                if len(grp.members) >= MAX_MEMBERS:
                    continue
                m = grp.members[mid] = Member(mid, "member", False, 0, "", now)
            if mid == grp.host:
                m.role, m.inviter, m.quota, m.kicker = "host", True, 0, False
            elif role == "a":
                self._set_role(m, True)
            else:
                m.role, m.inviter, m.quota, m.kicker = "member", bool(inviter), quota, bool(kicker)
            self._ensure_peer(mid)
        # (the inviter vouches for who is in the group, and so for the limit it has now: the host may have
        # raised it since the invitation was made, and has not met the newcomer yet to say so)
        grp.max_members = max(grp.max_members, g["mx"], len(grp.members))
        if grp.state == "joining":
            grp.state = "active"
            grp.roster_until = now + ROSTER_WINDOW
            self._sys(grp, "join", grp.me, grp.joined_by, now)
        fx.events.append(("changed", grp.id))

    def _on_add(self, grp, f, g, body, now, fx, extra) -> None:
        """A member says they let somebody in: believed if they had the right, and within their number."""
        w = g["w"]
        if grp.state != "active" or f.id not in grp.members or w == grp.me:
            return
        if w in grp.members or len(grp.members) >= grp.max_members:
            return
        m = grp.members[f.id]
        manager = self._manager(grp, f.id)
        used = grp.used.setdefault(f.id, [])
        if not manager and not (m.inviter and (w in used or len(used) < m.quota)):
            return
        if w in grp.banned:
            if not manager:
                return
            grp.banned.remove(w)                    # (only those who can set a ban lift it)
        grp.members[w] = Member(w, "member", False, 0, f.id, now)
        if w not in used:
            used.append(w)
        self._ensure_peer(w)
        self._sys(grp, "join", w, f.id, now)
        fx.events.append(("changed", grp.id))

    def _on_kick(self, grp, f, g, body, now, fx, extra) -> None:
        """Taking out goes downwards only: the host takes out anybody but themselves, an administrator
        an ordinary member, one allowed to an ordinary member who is not allowed to as well."""
        w = g["w"]
        by_host = self._is_host(grp, f.id)
        by_admin = self._is_admin(grp, f.id)
        if not (by_host or by_admin or self._is_kicker(grp, f.id)) or w == grp.host:
            return
        target = grp.members.get(w)
        if not by_host and target is not None:
            if target.role != "member" or (not by_admin and target.kicker):
                return
        if w == grp.me:
            if grp.state != "kicked":
                grp.state = "kicked"
                self._sys(grp, "kick", w, f.id, now)
                fx.events.append(("kicked", grp.id))
            return
        if target is not None or w not in grp.banned:
            self._remove_member(grp, w, ban=True)   # (even somebody not heard of yet: their "add" may be on its way)
            if target is not None:
                self._sys(grp, "kick", w, f.id, now)
            fx.events.append(("changed", grp.id))

    def _on_grant(self, grp, f, g, body, now, fx, extra) -> None:
        m = grp.members.get(g["w"])
        if m is None or m.id == grp.host or not self._manager(grp, f.id) or m.role != "member":
            return
        m.inviter, m.quota = g["q"] > 0, g["q"]
        self._sys(grp, "grant", m.id, f.id, now)
        fx.events.append(("changed", grp.id))

    def _on_admin(self, grp, f, g, body, now, fx, extra) -> None:
        """Only the host makes administrators."""
        m = grp.members.get(g["w"])
        if m is None or m.id == grp.host or not self._is_host(grp, f.id):
            return
        self._set_role(m, bool(g["a"]))
        self._sys(grp, "admin" if g["a"] else "unadmin", m.id, f.id, now)
        fx.events.append(("changed", grp.id))

    def _on_kick_right(self, grp, f, g, body, now, fx, extra) -> None:
        """The host or an administrator lets an ordinary member take out other ordinary members."""
        m = grp.members.get(g["w"])
        if m is None or m.id == grp.host or not self._manager(grp, f.id) or m.role != "member":
            return
        m.kicker = bool(g["a"])
        self._sys(grp, "kick_right" if g["a"] else "kick_right_off", m.id, f.id, now)
        fx.events.append(("changed", grp.id))

    def _on_limit(self, grp, f, g, body, now, fx, extra) -> None:
        """Only the host raises the limit, and only raises it."""
        if not self._is_host(grp, f.id) or g["n"] <= grp.max_members:
            return
        grp.max_members = g["n"]
        self._sys(grp, "limit", "", f.id, now, note=str(g["n"]))
        fx.events.append(("changed", grp.id))

    def _on_leave(self, grp, f, g, body, now, fx, extra) -> None:
        if f.id not in grp.members or f.id == grp.me:
            return
        was_host = f.id == grp.host
        self._remove_member(grp, f.id)
        if was_host:
            grp.host_gone = True
            self._sys(grp, "host_left", f.id, f.id, now)
        else:
            self._sys(grp, "left", f.id, f.id, now)
        fx.events.append(("changed", grp.id))

    def _on_rename(self, grp, f, g, body, now, fx, extra) -> None:
        if not self._manager(grp, f.id) or g["n"] == grp.name:
            return
        grp.name = g["n"]
        self._sys(grp, "rename", "", f.id, now)
        fx.events.append(("changed", grp.id))

    def _sequence(self, grp: Group, f, g: dict, now: float) -> bool:
        """Take the number of a message from a member: False for one already had; a gap in the
        numbers is noted. (A new epoch is somebody who joined again: it starts afresh.)"""
        epoch, s = g["e"], g["s"]
        had = grp.rx.get(f.id)
        last = had[1] if had and had[0] == epoch else 0
        if s <= last:
            return False
        if had and had[0] == epoch and s > last + 1:
            self._log(grp, {"id": f"gap-{f.id[:8]}.{epoch}.{s}", "from": f.id, "dir": "sys", "kind": "gap",
                            "b": "", "ts": now, "read": True, "seq": s, "missed": s - last - 1})
        grp.rx[f.id] = [epoch, s]
        return True

    def _on_message(self, grp, f, g, body, now, fx, extra) -> None:
        """What a member said (somebody who is not one - kicked, never in - is not heard)."""
        if grp.state != "active" or f.id not in grp.members or f.id == grp.me:
            return
        if not self._sequence(grp, f, g, now):
            return
        eid = f"{f.id[:8]}.{g['e']}.{g['s']}"
        self._log(grp, {"id": eid, "from": f.id, "dir": "in", "kind": "text", "b": body, "ts": now,
                        "read": False, "seq": g["s"], "missed": 0})
        fx.events.append(("message", grp.id, eid))

    def _on_file(self, grp, f, g, body, now, fx, extra) -> None:
        """A member offers the group an attachment: a card for me, until I accept or decline."""
        info, wire = extra.get("f"), extra.get("w")
        if grp.state != "active" or f.id not in grp.members or f.id == grp.me or not info or not wire:
            return
        if not self._sequence(grp, f, g, now):
            return
        from core.p2p import attachments as _a
        blocked = info["c"] == 1 and _a.is_blocked_name(info["l"])
        eid = f"{f.id[:8]}.{wire}"
        self._log(grp, {"id": eid, "from": f.id, "to": "", "dir": "in", "kind": "file",
                        "b": _f.file_fallback(info), "ts": now, "read": False, "seq": g["s"], "missed": 0,
                        "batch": "", "w": wire, "f": dict(info), "fs": "blocked" if blocked else "offered",
                        "fw": "type" if blocked else "", "fp": [], "sd": "", "sf": []})
        fx.events.append(("message", grp.id, eid))

    # the queue, for whoever sends --------------------------------------------------
    def waiting_peers(self) -> list:
        seen = []
        for x in self.store.group_outq:
            if x["p"] not in seen:
                seen.append(x["p"])
        return seen

    def next_out(self, peer: str, inflight: int, shared: bool = False) -> Optional[dict]:
        """The next thing waiting for *peer*, if their stream has room for another: MAX_INFLIGHT
        group messages at a time, and - when the stream is a friend's, which a conversation
        shares - chat and offers one at a time (what changes the group is not held back by that)."""
        item = next((x for x in self.store.group_outq if x["p"] == peer), None)
        if item is None:
            return None
        return item if inflight < (1 if shared and item["c"] else MAX_INFLIGHT) else None

    def refresh(self, item: dict) -> None:
        """Bring a queued message up to date as it is sent: a roster says the limit the group has by
        then, not the one it had when the roster was made."""
        if item["g"]["k"] == "ro":
            g = self.get(item["g"]["i"])
            if g is not None:
                item["g"]["mx"] = g.max_members

    def discard_item(self, item: dict) -> None:
        """Give up on one queued message (it cannot be sent at all)."""
        try:
            self.store.group_outq.remove(item)
        except ValueError:
            return
        self._dropped(item)
        self.store.dirty = True

    def discard_peer(self, peer: str) -> None:
        """Give up on everything queued for somebody who is no longer here."""
        for item in [x for x in self.store.group_outq if x["p"] == peer]:
            self.discard_item(item)

    def sent(self, item: dict, wire_id: str) -> None:
        """*item* went into the peer's stream as message *wire_id*."""
        try:
            self.store.group_outq.remove(item)
        except ValueError:
            return
        g = self.get(item["g"]["i"])
        if g is not None and item["e"]:
            e = next((x for x in g.log if x["id"] == item["e"]), None)
            if e is not None:
                if e["kind"] == "file":
                    e["w"] = wire_id            # (the id the member answers an offer by)
                else:
                    e.setdefault("dv", {})[item["p"]] = {"w": wire_id, "ok": False, "x": False}
        self.store.dirty = True

    def delivered(self, peer: str, wire_ids) -> list:
        """The peer has these messages of mine: the log says so. Returns (group id, entry id)."""
        ids, out = set(wire_ids), []
        for g in self.store.groups:
            for e in g.log:
                d = e.get("dv", {}).get(peer)
                if d and d["w"] and not d["ok"] and d["w"] in ids:
                    d["ok"] = True
                    out.append((g.id, e["id"]))
        if out:
            self.store.dirty = True
        return out

    # tidying -----------------------------------------------------------------------
    def lapse(self, now: Optional[float] = None) -> bool:
        """Invitations nobody answered are over, a group whose roster never came is given up,
        and early messages that were never understood are let go. True when something changed."""
        now = time.time() if now is None else now
        changed = False
        for g in self.store.groups:
            gone = [p for p, ts in g.pending.items() if now - ts > INVITE_SECONDS]
            for p in gone:
                del g.pending[p]
                changed = True
            if g.state == "joining" and now - g.created > JOIN_SECONDS:
                g.state = "failed"                  # (the roster is not coming)
                changed = True
            old = [h for h in g.held if now - h["t"] > HOLD_SECONDS]
            if old:
                g.held = [h for h in g.held if h not in old]
                changed = True
        keep = [r for r in self.store.group_invites if now - r["ts"] <= INVITE_SECONDS]
        if len(keep) != len(self.store.group_invites):
            self.store.group_invites = keep
            changed = True
        if changed:
            self.store.dirty = True
        return changed

    def peer_ids(self) -> set:
        """Everybody a group still needs a mailbox with."""
        out = set()
        for g in self.store.groups:
            if not self._dead(g):
                out |= {i for i in g.members if i != g.me}
        return out

    def cleanup_peers(self, now: Optional[float] = None) -> list:
        """Let go of the hidden friends no group needs - once what was left for them has gone.
        Returns the ids that went."""
        busy = {x["p"] for x in self.store.group_outq}
        return self.store.drop_unused_peers(self.peer_ids() | busy, now)

    # reading --------------------------------------------------------------------------
    def unread(self, gid: str) -> int:
        g = self.get(gid)
        return sum(1 for e in g.log if e["dir"] == "in" and e["kind"] in ("text", "file") and not e["read"]) if g else 0

    def mark_read(self, gid: str) -> bool:
        g = self.get(gid)
        changed = False
        for e in (g.log if g else []):
            if e["dir"] == "in" and not e["read"]:
                e["read"] = True
                changed = True
        if changed:
            self.store.dirty = True
        return changed

    def unread_groups(self) -> int:
        return sum(1 for g in self.store.groups if self.unread(g.id))

    def clear_log(self, gid: str) -> None:
        """Forget what was said here (an attachment still on its way stays)."""
        g = self.get(gid)
        if g is not None:
            g.log = [e for e in g.log if e["kind"] == "file" and e.get("fs") in ("waiting", "receiving", "sending")]
            self._save()
