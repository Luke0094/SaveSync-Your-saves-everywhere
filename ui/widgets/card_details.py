"""SaveSync - what a library card's info box shows while the pointer is over its picture.

The picture of a card is the part you point at to look at a game; the box under it is the part you
click (Play, backup, sync). While the pointer rests on the picture the box has nothing to be clicked
for, so it turns the pages of what the game carries - a page of its description, then its tags -
round and round, while the picture's own slideshow goes on. The moment the pointer leaves
the picture, or goes onto the box, the box is as it was and the buttons are there to be clicked.

``build_pages`` is the part that decides what the pages are (pure: a font and a width in, the pages
out); ``CardDetails`` is the box's side of it: the label laid over the buttons, the turning, the
coming and going.
"""
import html
import re

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel

# How long a page stays, and how long the change from one to the next takes.
PAGE_MS = 2800
FADE_MS = 240
# How much of a long description is shown (one page, then the tags), and how much of the text is even
# looked at - the rest could not be reached anyway.
MAX_DESC_PAGES = 1
MAX_CHARS = 900


def clean_text(raw) -> str:
    """A description as plain text on one line: markup out, entities decoded, runs of space one."""
    text = html.unescape(re.sub(r"<[^>]*>", " ", str(raw or "")))
    return re.sub(r"\s+", " ", text).strip()


def wrap_lines(text: str, fm, width: int) -> list:
    """*text* broken into lines no wider than *width* under the font metrics *fm* (a word wider than
    a line is cut where it stops fitting)."""
    lines, line = [], ""
    for word in text.split(" "):
        if not word:
            continue
        trial = word if not line else f"{line} {word}"
        if fm.horizontalAdvance(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        while len(word) > 1 and fm.horizontalAdvance(word) > width:
            cut = len(word) - 1
            while cut > 1 and fm.horizontalAdvance(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def _description_pages(text: str, fm, width: int, per_page: int) -> list:
    if not text:
        return []
    cut = len(text) > MAX_CHARS
    lines = wrap_lines(text[:MAX_CHARS], fm, width)
    room = per_page * MAX_DESC_PAGES
    if len(lines) > room:
        lines, cut = lines[:room], True
    if cut and lines:
        last = lines[-1].rstrip(" .,;:!?-") + "…"
        while len(last) > 1 and fm.horizontalAdvance(last) > width:
            last = last[:-2].rstrip(" .,;:!?-") + "…"
        lines[-1] = last
    return ["<br>".join(html.escape(ln) for ln in lines[i:i + per_page])
            for i in range(0, len(lines), per_page)]


def _tags_page(tags, fm, width: int, per_page: int, accent: str) -> list:
    """The tags as one page: "#tag" in the accent colour, as many to a line as fit, and "+3" when
    some do not fit on the page."""
    names, seen = [], set()
    for tag in tags or []:
        name = clean_text(tag)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append("#" + name)
    if not names:
        return []
    gap = fm.horizontalAdvance("  ")
    lines, line, used = [], [], 0
    for i, name in enumerate(names):
        w = fm.horizontalAdvance(name)
        if line and used + gap + w > width:
            lines.append(line)
            line, used = [], 0
        if len(lines) == per_page:
            break
        line.append(name)
        used += w + (gap if len(line) > 1 else 0)
    else:
        lines.append(line)
    shown = sum(len(ln) for ln in lines)
    hidden = len(names) - shown
    html_lines = ["&nbsp;&nbsp;".join(f'<span style="color:{accent}">{html.escape(n)}</span>' for n in ln)
                  for ln in lines if ln]
    if hidden > 0 and html_lines:
        html_lines[-1] += f"&nbsp;&nbsp;+{hidden}"
    return ["<br>".join(html_lines)] if html_lines else []


def build_pages(description, tags, fm, width: int, per_page: int, accent: str) -> list:
    """The pages (rich text, lines joined with <br>) a card turns through: the description, a few
    lines at a time and not the whole of a long one, then the tags. Empty when the game carries
    neither."""
    per_page = max(1, per_page)
    return (_description_pages(clean_text(description), fm, width, per_page)
            + _tags_page(tags, fm, width, per_page, accent))


class CardDetails(QObject):
    """The turning pages over a card's info box. Made when the card is first pointed at (a card
    with nothing to show never makes one); ``start`` when the pointer is on the picture, ``stop`` when
    it is not. It watches the box itself: the pointer going onto the box stops it, and coming back
    off the box onto the picture starts it again."""

    def __init__(self, card, bottom, area_top, area_bottom, hidden_while_shown, entry_of, pointer_pos,
                 accent_of):
        """*area_top* / *area_bottom*: the first and last widget of what is covered (the label takes
        the space between them); *hidden_while_shown*: the widgets of the box that go (their space is
        kept, so the box does not move); *entry_of*: the card's current game; *pointer_pos*: the
        pointer in the card's own coordinates; *accent_of*: the colour tags are drawn in."""
        super().__init__(card)
        self._card, self._bottom = card, bottom
        self._top, self._last = area_top, area_bottom
        self._hideable = list(hidden_while_shown)
        self._entry_of, self._pointer_pos, self._accent_of = entry_of, pointer_pos, accent_of
        for w in self._hideable:
            policy = w.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            w.setSizePolicy(policy)
        self._hidden: list = []
        self._pages: list = []
        self._empty_key = None          # what was read to nothing (see start)
        self._idx = 0
        self._swapped = True
        self.active = False

        self._label = QLabel(bottom)
        self._label.setObjectName("game_card_details")
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # (the pointer going onto the box must reach the box, not stop on this)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._effect = QGraphicsOpacityEffect(self._label)
        self._effect.setOpacity(1.0)
        self._label.setGraphicsEffect(self._effect)
        self._label.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(PAGE_MS)
        self._timer.timeout.connect(self._turn)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(lambda: self._effect.setOpacity(1.0))
        bottom.installEventFilter(self)

    # ── what is shown ─────────────────────────────────────────────────────

    def _place(self):
        layout = self._bottom.layout()
        if layout is not None:
            layout.activate()
            m = layout.contentsMargins()
        else:
            m = None
        left = m.left() if m else 0
        right = m.right() if m else 0
        top = self._top.geometry().top()
        bottom = self._last.geometry().bottom()
        self._label.setGeometry(left, top, max(20, self._bottom.width() - left - right),
                                max(12, bottom - top + 1))

    def _make_pages(self) -> list:
        entry = self._entry_of()
        if not (getattr(entry, "description", "") or getattr(entry, "tags", None)):
            return []
        self._label.ensurePolished()
        fm = self._label.fontMetrics()
        per_page = max(2, self._label.height() // max(1, fm.lineSpacing()))
        return build_pages(entry.description, entry.tags, fm, self._label.width() - 2, per_page,
                           self._accent_of())

    # ── coming and going ──────────────────────────────────────────────────

    def start(self):
        """The pointer is on the picture: the box turns to the pages (if there are any)."""
        if self.active:
            return
        entry = self._entry_of()
        key = (getattr(entry, "description", ""), tuple(getattr(entry, "tags", None) or ()))
        if key == self._empty_key:
            return                      # (read already: there is nothing to turn through)
        self._place()
        pages = self._make_pages()
        if not pages:
            self._empty_key = key
            return
        self._pages, self._idx = pages, 0
        self._hidden = [w for w in self._hideable if not w.isHidden()]
        for w in self._hidden:
            w.setVisible(False)
        self._label.setText(pages[0])
        self._effect.setOpacity(1.0)
        self._label.show()
        self._label.raise_()
        self.active = True
        if len(pages) > 1:
            self._timer.start()

    def stop(self):
        """Back to the box as it was."""
        if not self.active:
            return
        self.active = False
        self._timer.stop()
        self._fade.stop()
        self._effect.setOpacity(1.0)
        self._label.hide()
        for w in self._hidden:
            w.setVisible(True)
        self._hidden = []

    def start_if_on_picture(self):
        """Start unless the pointer is on the box (a card entered from below, say)."""
        pos = self._pointer_pos()
        if self._card.rect().contains(pos) and not self._bottom.geometry().contains(pos):
            self.start()

    # ── turning ───────────────────────────────────────────────────────────

    def _turn(self):
        if not self.active or len(self._pages) < 2 or self._fade.state() == QVariantAnimation.State.Running:
            return
        self._idx = (self._idx + 1) % len(self._pages)
        self._swapped = False
        self._fade.start()

    def _on_fade(self, value):
        # down to nothing and up again, the text changing at the bottom
        v = float(value)
        if v >= 0.5 and not self._swapped:
            self._swapped = True
            self._label.setText(self._pages[self._idx % len(self._pages)])
        self._effect.setOpacity(abs(1.0 - 2.0 * v))

    # ── the box under watch ───────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._bottom:
            kind = event.type()
            if kind == QEvent.Type.Enter:
                self.stop()
            elif kind == QEvent.Type.Leave:
                pos = self._pointer_pos()
                if self._card.rect().contains(pos):
                    self.start_if_on_picture()
                else:
                    self.stop()             # (out of the card altogether)
        return False
