"""SaveSync - the formatting a friend's message may use, and the links in it.

A message on the wire is still PLAIN TEXT (core.p2p.friends). The formatting is
a handful of marks inside that text which each PC reads for itself when it shows
the message, so nothing about the protocol changes, an older SaveSync simply
shows the marks as they are, and nothing a friend sends is ever HTML:

    **bold**   *italic*  _italic_   ~~struck~~   `code`   ```block```   > quote

Only these. No headings, no images, no tables, no ``[label](address)`` links -
a label that says one thing and opens another is how links are faked - and
nothing is fetched for a link: no preview, no icon, no "embed" loaded from the
address, because every one of those would tell whoever runs that address that
you read the message, when, and from where.

A web address in a message is a link only if it is plainly one (``http://`` or
``https://``, a real domain name, no user name inside it, no local or numeric
address; see check_url) and it is always SHOWN as the address it opens, with an
international name written out in its ASCII form so that look-alike letters
cannot pass for others. Clicking it is a question first (the dialog is the
caller's), never a jump.

parse() turns a message into blocks of spans; to_html() writes them as the
small piece of HTML a Qt label shows. Every character a friend wrote goes
through html.escape and the only tags in the result are the fixed ones this
module writes itself, so there is no way for a message to bring its own.
"""
import html
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

# At most this many links in one message become clickable (the rest stay text),
# and at most this many emphasis marks are looked at.
MAX_LINKS = 5
MAX_DELIMS = 60
MAX_URL_CHARS = 300

_ESCAPABLE = frozenset("\\*_~`>")
_FENCE = re.compile(r"^```[A-Za-z0-9_+.#-]{0,20}$")
# A quote needs the ">" to stand alone or be followed by a space, so that
# emoticons (">_<") and the like are not taken for one.
_QUOTE = re.compile(r"^>( |$)")
_URL_START = re.compile(r"https?://", re.IGNORECASE)
_URL_STOP = frozenset(" \t\n\r\x0b\x0c<>\"'`\xa0\u2028\u2029")
_TRAIL = ".,;:!?)]}*_~"
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
# Names that only mean something on a network of your own.
_LOCAL_TLDS = frozenset({"localhost", "local", "localdomain", "internal", "lan", "home",
                         "corp", "intranet", "private", "arpa", "onion", "i2p", "test",
                         "invalid"})


@dataclass(frozen=True)
class SafeUrl:
    """A web address that passed check_url."""
    url: str            # what is opened (and shown): ASCII only
    host: str           # the host as the browser will see it (ASCII)
    shown_host: str     # the host as a person reads it (may be international)
    secure: bool        # https
    international: bool  # the host was written with non-ASCII letters


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: int = -1      # index into Parsed.links; -1 when not a link


@dataclass(frozen=True)
class Block:
    kind: str           # "p", "quote" or "code"
    spans: tuple = ()


@dataclass
class Parsed:
    blocks: list = field(default_factory=list)
    links: list = field(default_factory=list)

    @property
    def plain(self) -> str:
        """The message without its marks (for a preview or a notification)."""
        return "\n".join("".join(s.text for s in b.spans) for b in self.blocks)


# ── addresses ────────────────────────────────────────────────────────────

def check_url(raw) -> Optional[SafeUrl]:
    """*raw* as a SafeUrl, or None when it is not a web address this app will
    make clickable: not http(s), a user name or password in it, no domain
    name (an IP address, ``localhost``, a name that only exists on a local
    network, a single word), a port that is not a port, control or direction
    characters, backslashes, or simply too long."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_CHARS:
        return None
    import unicodedata
    for ch in raw:
        if ch.isspace() or ch == "\\" or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return None
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.netloc:
            return None
        if "@" in parts.netloc:
            return None
        host = (parts.hostname or "").rstrip(".")
        port = parts.port                      # ValueError for a bad one
    except ValueError:
        return None
    if not host or port == 0 or (port is not None and not 1 <= port <= 65535):
        return None
    international = not host.isascii()
    try:
        ascii_host = host.encode("idna").decode("ascii") if international else host
    except UnicodeError:
        return None
    ascii_host = ascii_host.lower()
    labels = ascii_host.split(".")
    if len(labels) < 2 or len(ascii_host) > 253:
        return None
    if not all(_LABEL.match(x) for x in labels):
        return None
    if not _TLD.match(labels[-1]) or labels[-1] in _LOCAL_TLDS:
        return None
    netloc = ascii_host + (f":{port}" if port is not None else "")
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~[]")
    frag = quote(parts.fragment, safe="/?%:@!$&'()*+,;=-._~[]")
    url = urlunsplit((scheme, netloc, path, query, frag))
    if len(url) > MAX_URL_CHARS * 2 or not url.isascii():
        return None
    return SafeUrl(url=url, host=ascii_host, shown_host=host.lower(),
                   secure=scheme == "https", international=international)


# ── inline marks ─────────────────────────────────────────────────────────

def _find_url(s: str, i: int):
    """(the address written at s[i:], its SafeUrl) or None."""
    if i and s[i - 1].isalnum():
        return None
    m = _URL_START.match(s, i)
    if m is None:
        return None
    j = m.end()
    while j < len(s) and s[j] not in _URL_STOP:
        j += 1
    cand = s[i:j]
    while cand and cand[-1] in _TRAIL:
        # A closing bracket belongs to the address when it has its own opening one.
        if cand[-1] in ")]}" and cand.count({")": "(", "]": "[", "}": "{"}[cand[-1]]) >= cand.count(cand[-1]):
            break
        cand = cand[:-1]
    safe = check_url(cand)
    return (cand, safe) if safe is not None else None


def _run(s: str, i: int) -> int:
    """How many times the character at s[i] repeats from there."""
    n = 1
    while i + n < len(s) and s[i + n] == s[i]:
        n += 1
    return n


def _code_span(s: str, i: int):
    """(code text, index after the closing run) for a backtick run at s[i], or
    None when it is never closed (or would hold nothing). The closing run has
    to be exactly as long as the opening one."""
    n = _run(s, i)
    j = i + n
    while j < len(s):
        k = s.find("`", j)
        if k < 0:
            return None
        end = k + _run(s, k)
        if end - k == n:
            body = s[i + n:k]
            return (body, end) if body.strip() else None
        j = end
    return None


def _tokens(s: str, links: list) -> list:
    """Cut one run of text into plain text, code, links and the marks that may
    open or close emphasis."""
    toks: list = []
    buf: list = []
    ndelim = 0

    def flush():
        if buf:
            toks.append(["t", "".join(buf)])
            buf.clear()

    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
            buf.append(s[i + 1])
            i += 2
            continue
        if ch == "`":
            found = _code_span(s, i)
            if found is not None:
                flush()
                toks.append(["a", Span(found[0], code=True)])
                i = found[1]
                continue
            run = _run(s, i)
            buf.append(s[i:i + run])
            i += run
            continue
        if ch in "hH" and len(links) < MAX_LINKS:
            found = _find_url(s, i)
            if found is not None:
                cand, safe = found
                flush()
                links.append(safe)
                toks.append(["a", Span(safe.url, link=len(links) - 1)])
                i += len(cand)
                continue
        if ch in "*~_" and ndelim < MAX_DELIMS:
            run = _run(s, i)
            markers = {("*", 1): ["*"], ("*", 2): ["**"], ("*", 3): ["**", "*"],
                       ("~", 2): ["~~"], ("_", 1): ["_"]}.get((ch, run))
            if markers is not None:
                prev = s[i - 1] if i > 0 else None
                nxt = s[i + run] if i + run < n else None
                left = nxt is not None and not nxt.isspace()
                right = prev is not None and not prev.isspace()
                if ch == "_":
                    # Never inside a word: snake_case_names stay as they are.
                    left = left and (prev is None or not prev.isalnum())
                    right = right and (nxt is None or not nxt.isalnum())
                if len(markers) == 2 and left and not right:
                    markers.reverse()               # "***" opening: italic around bold
                flush()
                for marker in markers:
                    toks.append(["d", marker, left, right, None])
                ndelim += len(markers)
                i += run
                continue
            buf.append(s[i:i + run])
            i += run
            continue
        buf.append(ch)
        i += 1
    flush()
    return toks


def _pair(toks: list) -> None:
    """Match each closing mark with the nearest opening one of its own kind;
    what does not match, or would enclose nothing, stays literal."""
    stack: list = []                    # indexes of open delimiters
    for idx, tk in enumerate(toks):
        if tk[0] != "d":
            continue
        marker, can_open, can_close = tk[1], tk[2], tk[3]
        paired = False
        if can_close:
            for depth in range(len(stack) - 1, -1, -1):
                o = stack[depth]
                if toks[o][1] == marker:
                    if idx != o + 1:        # (right next to it: nothing to emphasise)
                        del stack[depth:]
                        toks[o][4], tk[4] = "open", "close"
                        paired = True
                    break
        if can_open and not paired:
            stack.append(idx)


def _spans(toks: list) -> list:
    out: list = []
    bold = italic = strike = 0

    def add(text, **kw):
        sp = Span(text, bold=bold > 0, italic=italic > 0, strike=strike > 0, **kw)
        if (out and not kw and not out[-1].code and out[-1].link < 0
                and (out[-1].bold, out[-1].italic, out[-1].strike) == (sp.bold, sp.italic, sp.strike)):
            out[-1] = Span(out[-1].text + text, sp.bold, sp.italic, sp.strike)
        elif text:
            out.append(sp)

    for tk in toks:
        if tk[0] == "t":
            add(tk[1])
        elif tk[0] == "a":
            a = tk[1]
            add(a.text, code=a.code, link=a.link)
        else:
            marker, state = tk[1], tk[4]
            if state is None:
                add(marker)
                continue
            step = 1 if state == "open" else -1
            if marker == "**":
                bold += step
            elif marker == "~~":
                strike += step
            else:
                italic += step
    return out


def _inline(s: str, links: list) -> list:
    toks = _tokens(s, links)
    _pair(toks)
    return _spans(toks)


# ── blocks ───────────────────────────────────────────────────────────────

def parse(text) -> Parsed:
    """A message as blocks of spans. Never raises, whatever *text* is."""
    result = Parsed()
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    para: list = []

    def flush_para():
        while para and not para[0].strip():
            para.pop(0)
        while para and not para[-1].strip():
            para.pop()
        if para:
            spans = _inline("\n".join(para), result.links)
            if spans:
                result.blocks.append(Block("p", tuple(spans)))
        para.clear()

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if _FENCE.match(stripped):
            flush_para()
            j, code = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            body = "\n".join(code).strip("\n")
            if body.strip():
                result.blocks.append(Block("code", (Span(body, code=True),)))
            i = j + 1
            continue
        if _QUOTE.match(stripped):
            flush_para()
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i].strip()):
                body = lines[i].strip()[1:]
                quoted.append(body[1:] if body.startswith(" ") else body)
                i += 1
            spans = _inline("\n".join(quoted).strip("\n"), result.links)
            if spans:
                result.blocks.append(Block("quote", tuple(spans)))
            continue
        para.append(lines[i])
        i += 1
    flush_para()
    return result


def plain(text) -> str:
    """*text* without its marks."""
    return parse(text).plain


# ── HTML for a Qt label ──────────────────────────────────────────────────

_COLOR = re.compile(r"^[#0-9A-Za-z(),.% ]{1,40}$")
_MONO = "Consolas, 'Cascadia Mono', monospace"  # i18n-ignore (markup and CSS)


def _color(value, fallback: str) -> str:
    return value if isinstance(value, str) and _COLOR.match(value) else fallback


def _esc(s: str) -> str:
    return html.escape(s, quote=True).replace("\n", "<br>")


def _span_html(sp: Span, c: dict) -> str:
    out = _esc(sp.text)
    if sp.link >= 0:
        out = f'<a href="l{int(sp.link)}"><span style="color:{c["link"]};text-decoration:underline">{out}</span></a>'  # i18n-ignore (markup and CSS)
    if sp.code:
        out = (f'<span style="font-family:{_MONO};background-color:{c["code_bg"]};'
               f'color:{c["code_fg"]}">{out}</span>')
    if sp.bold:
        out = f"<b>{out}</b>"
    if sp.italic:
        out = f"<i>{out}</i>"
    if sp.strike:
        out = f"<s>{out}</s>"
    return out


def to_html(parsed: Parsed, *, fg: str = "inherit", muted: str = "inherit", link: str = "inherit",
            code_bg: str = "rgba(128,128,128,0.25)", code_fg: str = "inherit") -> str:
    """The message as the small HTML a QLabel shows. Links are ``href="l<n>"``
    (an index into ``parsed.links``) - never an address - so a click can only
    ever ask about one of the links this module checked."""
    c = {"fg": _color(fg, "inherit"), "muted": _color(muted, "inherit"),
         "link": _color(link, "inherit"), "code_bg": _color(code_bg, "rgba(128,128,128,0.25)"),
         "code_fg": _color(code_fg, "inherit")}
    parts = []
    for b in parsed.blocks:
        if b.kind == "code":
            body = _esc(b.spans[0].text)
            parts.append(f'<div style="font-family:{_MONO};background-color:{c["code_bg"]};'
                         f'color:{c["code_fg"]};white-space:pre;margin:3px 0">{body}</div>')
        elif b.kind == "quote":
            inner = "".join(_span_html(s, c) for s in b.spans).replace("<br>", "<br>▎ ")
            parts.append(f'<div style="color:{c["muted"]};margin:2px 0">▎ {inner}</div>')  # i18n-ignore (markup and CSS)
        else:
            inner = "".join(_span_html(s, c) for s in b.spans)
            parts.append(f"<div>{inner}</div>")
    body = "".join(parts)
    return f'<div style="white-space:pre-wrap;color:{c["fg"]}">{body}</div>'
