"""
SaveSync - Web-search candidate preview + same-tier enrichment merge UI.

- CandidatePreviewDialog — arrow-carousel preview of search candidates; the
  candidate the user confirms is authoritative for every field it fills.
- EnrichmentMergeDialog — chip-based "fill the EMPTY fields" panel offered
  from the OTHER candidates of the same result set (same tier, never new
  searches), grouped by source; tags/links always extend additively.
- _FlowLayout / _ChipGroup / _merge_chip — the wrapping chip machinery.
"""
import logging
import threading
import webbrowser

from PySide6.QtCore import Qt, QSize, QRect, QPoint, Signal
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QWidget, QLayout, QSizePolicy,
)

from i18n import t
from ui.styles.theme import palette

logger = logging.getLogger(__name__)


def _inspect_url(info) -> str:
    """Best page URL for opening a candidate/source in the browser."""
    u = (getattr(info, "store_url", "") or "").strip()
    if u:
        return u
    for extra in getattr(info, "extra_urls", None) or []:
        extra = (extra or "").strip()
        if extra:
            return extra
    return ""


def _open_inspect_url(url: str):
    url = (url or "").strip()
    if url:
        webbrowser.open(url)


class CandidatePreviewDialog(QDialog):
    """Unified popup for reviewing a search-result candidate — used for
    BOTH a single result and multiple distinct titles the same search
    turned up (see AddGameDialog._show_search_candidates). Confirming one
    of several then offers the remaining candidates of the same set as
    enrichment pieces (EnrichmentMergeDialog).

    Replaces what used to be two different presentations — a compact,
    name-and-thumbnail-only inline bar for multiple titles, and a
    separate plain Yes/No popup for a single title — with ONE popup that
    always shows the same concrete detail (cover image, name,
    description, developer, year, tags, source) for whichever candidate
    is currently selected, with ‹ › arrows to browse when there's more
    than one, and the same Confirm/Reject buttons either way. Anything
    the candidate would REPLACE is shown struck through next to the new
    value, so accepting a candidate is never a surprise.

    diff_fn(candidate) -> dict is called live as the user browses; see
    AddGameDialog._compute_candidate_diff for the canonical shape:
        has_existing / is_overwrite — case framing
        fields: {'name'|'description'|'developer'|'year':
                  {'old': str|None, 'new': str}}  (omitted = unchanged)
        new_tags   — tags that would be newly added (union, never removes)
        result_year — extracted release year, if any
    """

    _thumb_ready = Signal(int, object)   # load token, (url, raw_bytes)

    def __init__(self, candidates: list, diff_fn, parent=None, extra_note: str = ""):
        super().__init__(parent)
        self._candidates = list(candidates)
        self._diff_fn = diff_fn
        self._extra_note = extra_note
        self._idx = 0
        self._thumb_cache: dict[str, bytes] = {}
        self._thumb_token = 0
        self.selected = None   # set to the confirmed GameInfo on accept
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t('add_game.candidate_preview_title'))
        self.setMinimumWidth(440)
        self.setMaximumWidth(500)
        self._thumb_ready.connect(self._on_thumb_ready)
        self._build()
        self._update()

    def set_candidates(self, candidates: list):
        """Replace the browse list (e.g. soft-promote unlocked after an
        async primary-reachability probe). Keeps the current selection when
        that candidate is still present."""
        new = [c for c in (candidates or []) if c]
        if not new:
            return
        cur = None
        if self._candidates and 0 <= self._idx < len(self._candidates):
            cur = self._candidates[self._idx]
        self._candidates = new
        if cur is not None:
            try:
                self._idx = self._candidates.index(cur)
            except ValueError:
                self._idx = 0
        else:
            self._idx = 0
        self._update()

    # ── Construction ───────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(16, 14, 16, 14)

        self._counter_lbl = QLabel()
        self._counter_lbl.setWordWrap(True)
        self._counter_lbl.setStyleSheet(
            f"color:{palette('accent')};font-size:11px;font-weight:700;"
        )
        outer.addWidget(self._counter_lbl)

        # Filled triangles (◀ ▶) — thin ‹ › glyphs disappear on some fonts /
        # Windows DPI scales; these stay readable at the sidebar width.
        _arrow_css = (
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border_hover')};border-radius:4px;"
            f"font-weight:700;font-size:16px;padding:0;}}"
            f"QPushButton:hover{{background:{palette('accent')};color:{palette('accent_text')};"
            f"border-color:{palette('accent')};}}"
            f"QPushButton:disabled{{color:{palette('text_muted')};"
            f"border-color:{palette('border')};background:{palette('bg')};}}"
        )

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.setMinimumHeight(90)
        self._prev_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._prev_btn.setToolTip(t('add_game.candidate_prev'))
        self._prev_btn.setStyleSheet(_arrow_css)
        self._prev_btn.clicked.connect(self._go_prev)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(32)
        self._next_btn.setMinimumHeight(90)
        self._next_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._next_btn.setToolTip(t('add_game.candidate_next'))
        self._next_btn.setStyleSheet(_arrow_css)
        self._next_btn.clicked.connect(self._go_next)

        content = QVBoxLayout()
        content.setSpacing(6)

        _thumb_row = QHBoxLayout()
        self._thumb_lbl = QLabel("🎮")
        self._thumb_lbl.setFixedSize(112, 70)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setStyleSheet(
            f"background:{palette('bg_elevated')};border:1px solid {palette('border_hover')};"
            f"border-radius:6px;font-size:26px;"
        )
        _thumb_row.addWidget(self._thumb_lbl)
        _thumb_row.addStretch()
        content.addLayout(_thumb_row)

        self._name_lbl = QLabel()
        self._name_lbl.setWordWrap(True)
        self._name_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._name_lbl.setStyleSheet(f"color:{palette('text')};font-size:16px;font-weight:700;")
        content.addWidget(self._name_lbl)

        self._source_lbl = QLabel()
        self._source_lbl.setWordWrap(True)
        self._source_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;font-weight:600;")
        content.addWidget(self._source_lbl)

        self._inspect_btn = QPushButton()
        self._inspect_btn.setFlat(True)
        self._inspect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._inspect_btn.setToolTip(t("add_game.candidate_inspect_tooltip"))
        self._inspect_btn.setStyleSheet(
            f"QPushButton{{color:{palette('accent')};font-size:11px;text-align:left;"
            f"padding:0;background:transparent;border:none;}}"
            f"QPushButton:hover{{text-decoration:underline;}}"
            f"QPushButton:disabled{{color:{palette('text_disabled')};}}"
        )
        self._inspect_btn.clicked.connect(self._on_inspect)
        self._inspect_url = ""
        content.addWidget(self._inspect_btn)

        self._desc_lbl = QLabel()
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
        content.addWidget(self._desc_lbl)

        self._meta_lbl = QLabel()
        self._meta_lbl.setWordWrap(True)
        self._meta_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._meta_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        content.addWidget(self._meta_lbl)

        # The source's own verdict, when it has one and the form doesn't yet.
        self._review_lbl = QLabel()
        self._review_lbl.setWordWrap(True)
        self._review_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._review_lbl.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        content.addWidget(self._review_lbl)

        self._tags_lbl = QLabel()
        self._tags_lbl.setWordWrap(True)
        self._tags_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._tags_lbl.setStyleSheet(f"color:{palette('text_hint')};font-size:11px;")
        content.addWidget(self._tags_lbl)
        content.addStretch()

        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)
        mid_row.addWidget(self._prev_btn)
        mid_row.addLayout(content, 1)
        mid_row.addWidget(self._next_btn)
        outer.addLayout(mid_row)

        self._btn_hint_lbl = QLabel(t('add_game.candidate_buttons_hint'))
        self._btn_hint_lbl.setWordWrap(True)
        self._btn_hint_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._btn_hint_lbl.setStyleSheet(f"color:{palette('text_hint')};font-size:10px;")
        outer.addWidget(self._btn_hint_lbl)

        # Optional caller note — e.g. the initial search announces that
        # still-empty fields will be looked up on other sources afterwards,
        # with a single merge preview to pick what gets imported.
        if self._extra_note:
            _note = QLabel(self._extra_note)
            _note.setWordWrap(True)
            _note.setStyleSheet(
                f"color:{palette('text_muted')};font-size:10px;font-style:italic;"
            )
            outer.addWidget(_note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._reject_btn = QPushButton(t('common.no'))
        self._reject_btn.setMinimumWidth(90)
        self._reject_btn.setToolTip(t('add_game.candidate_dismiss'))
        self._reject_btn.clicked.connect(self._on_reject)
        self._confirm_btn = QPushButton(t('common.yes'))
        self._confirm_btn.setObjectName("primary_btn")
        self._confirm_btn.setMinimumWidth(90)
        self._confirm_btn.setToolTip(t('add_game.candidate_use'))
        self._confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self._reject_btn)
        btn_row.addWidget(self._confirm_btn)
        outer.addLayout(btn_row)

    # ── Rendering ────────────────────────────────────────────────────────

    def _update(self):
        import html as _h

        n = len(self._candidates)
        self._idx = max(0, min(self._idx, n - 1)) if n else 0
        c = self._candidates[self._idx] if n else None

        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(n > 0 and self._idx < n - 1)
        self._prev_btn.setVisible(n > 1)
        self._next_btn.setVisible(n > 1)

        if not c:
            self._counter_lbl.setText(t('add_game.search_not_found'))
            return

        diff = {}
        try:
            diff = self._diff_fn(c) or {}
        except Exception:
            logger.debug("CandidatePreviewDialog: diff_fn failed", exc_info=True)
        fields = diff.get('fields', {}) or {}

        src_label = getattr(c, 'source', '') or ''
        parent = self.parent()
        if parent is not None and hasattr(parent, '_source_label'):
            try:
                src_label = parent._source_label(src_label)
            except Exception:
                pass

        if n > 1:
            self._counter_lbl.setText(
                f"{t('add_game.candidates_found', n=n)} — {self._idx + 1}/{n}"
            )
        else:
            self._counter_lbl.setText(t('add_game.candidate_found_single'))

        # ── Name (strikethrough old if the result renames the title) ────
        _name_field = fields.get('name')
        if _name_field and _name_field.get('old'):
            self._name_lbl.setText(
                f"<span style='color:{palette('text_muted')};text-decoration:line-through;"
                f"font-weight:400;font-size:12px;'>{_h.escape(_name_field['old'])}</span><br>"
                f"{_h.escape(_name_field.get('new') or c.name or '?')}"
            )
        else:
            self._name_lbl.setText(_h.escape(c.name or '?'))

        self._source_lbl.setText(_h.escape(src_label))

        self._inspect_url = _inspect_url(c)
        if self._inspect_url:
            self._inspect_btn.setText(t("add_game.candidate_inspect"))
            self._inspect_btn.setToolTip(
                t("add_game.candidate_inspect_tooltip") + "\n" + self._inspect_url)
            self._inspect_btn.setEnabled(True)
            self._inspect_btn.setVisible(True)
        else:
            self._inspect_btn.clear()
            self._inspect_btn.setToolTip(t("add_game.candidate_inspect_tooltip"))
            self._inspect_btn.setEnabled(False)
            self._inspect_btn.setVisible(False)

        # ── Description ──────────────────────────────────────────────────
        # Only advertise a description the confirm path would actually write
        # (empty→fill). A rewritten page text must not look like a replace.
        _desc_field = fields.get('description')
        if _desc_field:
            _new_desc = (_desc_field.get('new') or '').strip()
        elif not diff.get('has_existing'):
            _new_desc = (c.description or '').strip()
        else:
            _new_desc = ''
        if _new_desc:
            _snip = _new_desc if len(_new_desc) <= 380 else _new_desc[:380].rstrip() + '…'
            if _desc_field and _desc_field.get('old'):
                _old_snip = _desc_field['old']
                _old_snip = _old_snip if len(_old_snip) <= 110 else _old_snip[:110].rstrip() + '…'
                self._desc_lbl.setText(
                    f"<span style='color:{palette('text_muted')};text-decoration:line-through;'>"
                    f"{_h.escape(_old_snip)}</span><br>{_h.escape(_snip)}"
                )
            else:
                self._desc_lbl.setText(_h.escape(_snip))
            self._desc_lbl.setVisible(True)
        else:
            self._desc_lbl.setVisible(False)

        # ── Developer / Year meta row ────────────────────────────────────
        def _meta_piece(label_key: str, field_key: str, fallback: str) -> str:
            _f = fields.get(field_key)
            _new = (_f['new'] if _f else fallback) or ''
            if not _new:
                return ''
            _lbl = _h.escape(t(label_key))
            if _f and _f.get('old'):
                return (
                    f"<b>{_lbl}:</b> "
                    f"<span style='color:{palette('text_muted')};text-decoration:line-through;'>"
                    f"{_h.escape(_f['old'])}</span> {_h.escape(_new)}"
                )
            return f"<b>{_lbl}:</b> {_h.escape(_new)}"

        _dev_piece = _meta_piece('add_game.developer', 'developer', getattr(c, 'developer', '') or '')
        _yr_piece  = _meta_piece('add_game.year', 'year', diff.get('result_year', '') or '')
        _meta_bits = [p for p in (_dev_piece, _yr_piece) if p]
        self._meta_lbl.setText('&nbsp;&nbsp;&nbsp;'.join(_meta_bits))
        self._meta_lbl.setVisible(bool(_meta_bits))

        # ── Reviews — count + up to 3 samples on ONE line ──────────────
        # Vertical stacking ate the room the description and tags need;
        # the reviews panel is where they are read in full.
        _new_reviews = diff.get('new_reviews') or []
        if _new_reviews:
            from core.library import reviews_display_count
            _bits = [
                f"<b>{_h.escape(t('reviews.preview'))}:</b> "
                f"{_h.escape(t('reviews.preview_count', count=reviews_display_count(_new_reviews)))}"
            ]
            for _r in _new_reviews[:3]:
                _score = float(_r.get('rating') or 0)
                _who = (_r.get('reviewer') or '').strip()
                _head = ' '.join(x for x in (
                    f"★ {_score:g}" if _score else '',
                    _h.escape(_who),
                ) if x)
                if _head:
                    _bits.append(_head)
            self._review_lbl.setText('&nbsp;&nbsp;·&nbsp;&nbsp;'.join(_bits))
            self._review_lbl.setVisible(True)
        else:
            self._review_lbl.setVisible(False)

        # ── Tags — always additive (union, never struck through) ─────────
        _all_genre_tags = list(c.genres or [])
        _new_tags = diff.get('new_tags') or []
        if diff.get('has_existing') and not diff.get('is_overwrite') and _all_genre_tags:
            _display_tags, _prefix = _new_tags, ('+ ' if _new_tags else '')
        else:
            _display_tags, _prefix = _all_genre_tags, ''
        if _display_tags:
            self._tags_lbl.setText(
                f"<b>{_h.escape(t('library.tags'))}:</b> {_prefix}"
                f"{_h.escape(', '.join(_display_tags[:10]))}"
            )
            self._tags_lbl.setVisible(True)
        else:
            self._tags_lbl.setVisible(False)

        self._thumb_lbl.setPixmap(QPixmap())
        self._thumb_lbl.setText("🎮")
        if getattr(c, 'image_url', ''):
            self._load_thumb(c.image_url)

    # ── Navigation / decision ────────────────────────────────────────────

    def _go_prev(self):
        if self._idx > 0:
            self._idx -= 1
            self._update()

    def _go_next(self):
        if self._idx < len(self._candidates) - 1:
            self._idx += 1
            self._update()

    def _on_confirm(self):
        self.selected = self._candidates[self._idx] if self._candidates else None
        self.accept()

    def _on_reject(self):
        self.selected = None
        self.reject()

    def _on_inspect(self):
        _open_inspect_url(self._inspect_url)

    # ── Cover thumbnail (lazy, cached, stale-load safe) ──────────────────

    def _load_thumb(self, url: str):
        self._thumb_token += 1
        token = self._thumb_token
        cached = self._thumb_cache.get(url)
        if cached is not None:
            self._set_thumb(cached)
            return

        def _fetch(u=url, tok=token):
            try:
                import urllib.request
                from core.net import open_url as _open_url
                req = urllib.request.Request(
                    u, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                )
                with _open_url(req, timeout=10) as r:
                    data = r.read()
            except Exception:
                return
            try:
                self._thumb_ready.emit(tok, (u, data))
            except RuntimeError:
                pass   # dialog already destroyed

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_thumb_ready(self, token: int, payload):
        url, data = payload
        self._thumb_cache[url] = data
        if token != self._thumb_token:
            return   # user browsed to another candidate meanwhile
        self._set_thumb(data)

    def _set_thumb(self, data: bytes):
        px = QPixmap()
        if px.loadFromData(data):
            from ui.helpers import scaled_for_screen
            self._thumb_lbl.setPixmap(scaled_for_screen(px, 112, 70))


class _FlowLayout(QLayout):
    """Minimal wrapping layout for chip strips (Qt has no built-in flow)."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        x, y = rect.x() + m.left(), rect.y() + m.top()
        line_h = 0
        right = rect.right() - m.right()
        for it in self._items:
            w, h = it.sizeHint().width(), it.sizeHint().height()
            if x + w > right and line_h > 0:
                x = rect.x() + m.left()
                y += line_h + self._spacing
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self._spacing
            line_h = max(line_h, h)
        return y + line_h + m.bottom() - rect.y()


class _ChipGroup:
    """Radio-like group of checkable chips where deselecting ALL is allowed
    (= keep current / skip). API mirrors QButtonGroup where used."""

    def __init__(self):
        self._chips: list[QPushButton] = []

    def add(self, chip: QPushButton):
        self._chips.append(chip)
        chip.toggled.connect(lambda on, c=chip: self._solo(c) if on else None)

    def _solo(self, chip: QPushButton):
        for c in self._chips:
            if c is not chip and c.isChecked():
                c.setChecked(False)

    def buttons(self) -> list:
        return list(self._chips)

    def checkedButton(self):
        for c in self._chips:
            if c.isChecked():
                return c
        return None


def _merge_chip(text: str, tooltip: str = "") -> QPushButton:
    """Toggle chip in the same visual language as the dialog tag/URL chips."""
    b = QPushButton(text.replace('&', '&&'))
    b.setCheckable(True)
    b.setFixedHeight(22)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    if tooltip:
        b.setToolTip(tooltip)
    b.setStyleSheet(
        f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text_secondary')};"
        f"border:1px solid {palette('border')};border-radius:10px;padding:0 8px;font-size:11px;}}"
        f"QPushButton:hover{{border-color:{palette('accent')};color:{palette('text')};}}"
        f"QPushButton:checked{{background:{palette('accent')};color:{palette('accent_text')};"
        f"border-color:{palette('accent')};}}"
    )
    return b


class EnrichmentMergeDialog(QDialog):
    """One-shot merge preview for same-tier enrichment, chip-based.

    Pieces are shown as toggle chips DIVIDED BY SOURCE — the same visual
    language as the dialog tag/URL chips. Single-value fields (description,
    developer, year, image) are exclusive across sources: selecting one chip
    deselects the same field chips elsewhere. Tag and link chips toggle
    independently. Image offers render a real thumbnail. Nothing touches the
    form until Apply. Back restores the candidate carousel (RESULT_BACK).
    """

    RESULT_BACK = 2

    _ELIDE = 60   # preview length inside a chip
    _IMG_W, _IMG_H = 96, 60

    _FIELDS = ('description', 'developer', 'year', 'image')

    _thumb_ready = Signal(str, bytes)   # image url, downloaded bytes

    def __init__(self, model: dict, source_label_fn, parent=None):
        super().__init__(parent)
        self._model = model
        self._src_label = source_label_fn
        self._field_groups: dict[str, _ChipGroup] = {}
        self._tag_boxes: list[tuple[QPushButton, str]] = []
        self._url_boxes: list[tuple[QPushButton, str]] = []
        self._review_boxes: list[tuple[QPushButton, list]] = []
        self._img_chips: dict[str, QPushButton] = {}   # image url → select chip
        self._header_thumbs: dict[str, QLabel] = {}    # cover url → header preview
        # source key → every chip offered under that source (for header toggle)
        self._source_chips: dict[str, list[QPushButton]] = {}
        self._source_headers: dict[str, QPushButton] = {}
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t('add_game.merge_title'))
        self.setMinimumWidth(540)
        self.setMinimumHeight(360)
        self.setMaximumHeight(680)
        self.resize(560, 520)
        self._thumb_ready.connect(self._on_thumb_ready)
        self._build()
        self._start_thumb_downloads()

    def _short(self, value: str, limit: int = 0) -> str:
        v = " ".join((value or "").split())
        n = limit or self._ELIDE
        return v if len(v) <= n else v[:n - 1] + "…"

    def _field_title(self, field: str) -> str:
        return {
            'description': t('library.description'),
            'developer':   t('add_game.developer'),
            'year':        t('add_game.year'),
            'image':       t('add_game.image'),
        }[field]

    @staticmethod
    def _short_inspect_host(url: str) -> str:
        """Compact host/path for the clickable inspect segment."""
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            host = (p.netloc or "").removeprefix("www.")
            path = (p.path or "").rstrip("/")
            tail = path.rsplit("/", 1)[-1] if path else ""
            short = f"{host}/{tail}" if host and tail else (host or url)
        except Exception:
            short = url
        return short if len(short) <= 42 else short[:41] + "…"

    def _source_header_row(self, source: str) -> QWidget:
        """Cover + ``source · title ·`` + clickable URL (inspect) + chip toggle."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 8, 0, 2)
        lay.setSpacing(4)

        meta = (self._model.get("source_meta") or {}).get(source) or {}
        cover = (meta.get("image_url") or "").strip()
        # Identity thumb — always shown when the peer has a cover, so two
        # VNDB titles are recognizable before reading the composite label.
        thumb = QLabel("🖼")
        thumb.setFixedSize(56, 36)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(
            f"background:{palette('bg_elevated')};border:1px solid {palette('border')};"
            f"border-radius:4px;font-size:14px;color:{palette('text_muted')};"
        )
        if cover:
            thumb.setToolTip((meta.get("name") or "") or cover)
            self._header_thumbs[cover] = thumb
        else:
            thumb.setVisible(False)
        lay.addWidget(thumb, 0, Qt.AlignmentFlag.AlignVCenter)

        src_id = meta.get("source_id") or source.split(" · ")[0] or source
        title = (meta.get("name") or "").strip()
        # Toggle target is source · title; the URL is a separate inspect control.
        head = self._src_label(src_id)
        if title:
            head = f"{head} · {title}"
        btn = QPushButton(head)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(t("add_game.merge_toggle_source"))
        btn.setStyleSheet(
            f"QPushButton{{color:{palette('text_muted')};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;text-align:left;padding:2px 0;"
            f"background:transparent;border:none;}}"
            f"QPushButton:hover{{color:{palette('accent')};}}"
        )
        btn.clicked.connect(lambda _=False, s=source: self._toggle_source(s))
        self._source_headers[source] = btn
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)

        inspect = (meta.get("inspect_url") or "").strip()
        if inspect:
            sep = QLabel("·")
            sep.setStyleSheet(
                f"color:{palette('text_muted')};font-size:10px;font-weight:700;padding:0 2px;")
            lay.addWidget(sep, 0, Qt.AlignmentFlag.AlignVCenter)
            # The URL itself is the inspect control (opens the source page).
            link = QPushButton(self._short_inspect_host(inspect))
            link.setFlat(True)
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.setToolTip(t("add_game.merge_inspect_tooltip") + "\n" + inspect)
            link.setStyleSheet(
                f"QPushButton{{color:{palette('accent')};font-size:10px;font-weight:600;"
                f"text-align:left;padding:2px 0;background:transparent;border:none;}}"
                f"QPushButton:hover{{text-decoration:underline;}}"
            )
            link.clicked.connect(lambda _=False, u=inspect: _open_inspect_url(u))
            lay.addWidget(link, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        return row

    def _toggle_source(self, source: str):
        chips = self._source_chips.get(source) or []
        if not chips:
            return
        # Any checked → turn all off (exclude source). All off → turn all on.
        any_on = any(c.isChecked() for c in chips)
        for c in chips:
            c.setChecked(not any_on)
        self._refresh_source_header(source)

    def _refresh_source_header(self, source: str):
        btn = self._source_headers.get(source)
        chips = self._source_chips.get(source) or []
        if btn is None or not chips:
            return
        any_on = any(c.isChecked() for c in chips)
        color = palette('accent') if any_on else palette('text_disabled')
        btn.setStyleSheet(
            f"QPushButton{{color:{color};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;text-align:left;padding:2px 0;"
            f"background:transparent;border:none;}}"
            f"QPushButton:hover{{color:{palette('accent')};}}"
        )

    @staticmethod
    def _reviews_chip_text(reviews: list) -> str:
        """"Reviews (n)", carrying the score when a single aggregate gives it."""
        from core.library import reviews_display_count
        label = t('reviews.merge_chip', count=reviews_display_count(reviews))
        rated = [float(r.get('rating') or 0) for r in reviews
                 if float(r.get('rating') or 0) > 0]
        if len(rated) == 1:
            return f"★ {rated[0]:g} · {label}"
        return label

    @staticmethod
    def _reviews_tooltip(reviews: list) -> str:
        lines = []
        for r in reviews:
            who = (r.get('reviewer') or '').strip()
            score = float(r.get('rating') or 0)
            head = " ".join(x for x in (who, f"★ {score:g}" if score else "") if x)
            body = (r.get('text') or '').strip()
            lines.append(f"{head}\n{body}".strip() if body else head)
        return "\n\n".join(x for x in lines if x)

    def _field_chip(self, field: str, text: str, value, tooltip: str = "",
                    checked: bool = False) -> QPushButton:
        chip = _merge_chip(text, tooltip)
        chip.setProperty('opt_value', value)
        group = self._field_groups.setdefault(field, _ChipGroup())
        group.add(chip)
        chip.setChecked(checked)
        return chip

    def _image_chip(self, url: str, checked: bool = False,
                    title: str = "") -> QPushButton:
        """Checkable cover thumbnail — readable without relying on memory."""
        chip = QPushButton("🖼")
        chip.setCheckable(True)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setFixedSize(self._IMG_W + 8, self._IMG_H + 8)
        chip.setIconSize(QSize(self._IMG_W, self._IMG_H))
        chip.setToolTip("\n".join(x for x in (title, url) if x))
        chip.setProperty("opt_value", url)
        chip.setStyleSheet(
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text_muted')};"
            f"border:1px solid {palette('border')};border-radius:6px;font-size:22px;padding:2px;}}"
            f"QPushButton:hover{{border-color:{palette('accent')};}}"
            f"QPushButton:checked{{border:2px solid {palette('accent')};"
            f"background:{palette('bg_card')};}}"
        )
        group = self._field_groups.setdefault("image", _ChipGroup())
        group.add(chip)
        chip.setChecked(checked)
        self._img_chips[url] = chip
        return chip

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(16, 14, 16, 14)

        intro = QLabel(t('add_game.merge_intro'))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{palette('accent')};font-size:11px;font-weight:600;")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content.setObjectName("transparent_bg")
        col = QVBoxLayout(content)
        col.setSpacing(4)
        col.setContentsMargins(0, 0, 8, 0)

        # ── One section per source, chips inside ──────────────────────────
        # Only fields the confirmed candidate left EMPTY reach this dialog
        # (see _build_merge_model), so every field chip is a filler offer:
        # the first source's chip starts selected, clicking it off (or all
        # off) means "leave the field empty". Filled fields never appear —
        # they stay whatever the confirmed source set.
        by_source: dict = {}
        for field in self._FIELDS:
            for opt in self._model.get(field, []):
                by_source.setdefault(opt['source'], {}).setdefault(field, []).append(opt['value'])
        for kind in ('tags', 'urls', 'reviews'):
            for opt in self._model.get(kind, []):
                by_source.setdefault(opt['source'], {}).setdefault(kind, []).append(opt['value'])

        _field_first_taken = {f: False for f in self._FIELDS}

        for source, offers in by_source.items():
            col.addWidget(self._source_header_row(source))
            flow = _FlowLayout(spacing=6)
            host = QWidget()
            host.setLayout(flow)
            src_chips: list[QPushButton] = []
            peer_title = ((self._model.get("source_meta") or {}).get(source) or {}).get("name") or ""
            # Cover select chip first — identity at a glance before text fields.
            for value in offers.get('image', []):
                checked = not _field_first_taken['image']
                _field_first_taken['image'] = True
                chip = self._image_chip(value, checked=checked, title=peer_title)
                flow.addWidget(chip)
                src_chips.append(chip)
            for field in self._FIELDS:
                if field == 'image':
                    continue
                for value in offers.get(field, []):
                    checked = not _field_first_taken[field]
                    _field_first_taken[field] = True
                    chip = self._field_chip(
                        field,
                        self._field_title(field) + ": " + self._short(value),
                        value, tooltip=value, checked=checked,
                    )
                    flow.addWidget(chip)
                    src_chips.append(chip)
            for tag in offers.get('tags', []):
                chip = _merge_chip(tag)
                chip.setChecked(True)
                flow.addWidget(chip)
                self._tag_boxes.append((chip, tag))
                src_chips.append(chip)
            for url in offers.get('urls', []):
                chip = _merge_chip(self._short(url, 44), tooltip=url)
                chip.setChecked(True)
                flow.addWidget(chip)
                self._url_boxes.append((chip, url))
                src_chips.append(chip)
            # One chip for ALL of a source's reviews: a score, who gave it and
            # what they wrote are one verdict, so they are taken or left as
            # one. Independent of the other sources' chips, the way tags are.
            for reviews in offers.get('reviews', []):
                chip = _merge_chip(self._reviews_chip_text(reviews),
                                   tooltip=self._reviews_tooltip(reviews))
                chip.setChecked(True)
                flow.addWidget(chip)
                self._review_boxes.append((chip, reviews))
                src_chips.append(chip)
            self._source_chips[source] = src_chips
            for chip in src_chips:
                chip.toggled.connect(
                    lambda _on, s=source: self._refresh_source_header(s))
            self._refresh_source_header(source)
            col.addWidget(host)

        col.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        back_btn = QPushButton(t('add_game.merge_back'))
        back_btn.setMinimumWidth(120)
        back_btn.setToolTip(t('add_game.merge_back_tooltip'))
        back_btn.clicked.connect(lambda: self.done(self.RESULT_BACK))
        btn_row.addWidget(back_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton(t('common.cancel'))
        cancel_btn.setMinimumWidth(90)
        cancel_btn.setToolTip(t('add_game.merge_cancel_tooltip'))
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton(t('common.apply'))
        apply_btn.setObjectName("primary_btn")
        apply_btn.setMinimumWidth(90)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        outer.addLayout(btn_row)

    def _start_thumb_downloads(self):
        """Fetch cover previews off the GUI thread (same opener as candidate)."""
        import urllib.request as _url_req
        from core.net import open_url as _open_url
        _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        urls = set(self._img_chips) | set(self._header_thumbs)
        for url in urls:
            def _dl(u=url):
                try:
                    req = _url_req.Request(u, headers={"User-Agent": _UA})
                    with _open_url(req, timeout=10) as r:
                        data = r.read(2_000_000)
                    if data:
                        self._thumb_ready.emit(u, data)
                except Exception:
                    pass
            threading.Thread(target=_dl, daemon=True).start()

    def _on_thumb_ready(self, url: str, data: bytes):
        try:
            px = QPixmap()
            if not px.loadFromData(data):
                return
            from ui.helpers import scaled_for_screen
            chip = self._img_chips.get(url)
            if chip is not None:
                thumb = scaled_for_screen(px, self._IMG_W, self._IMG_H)
                chip.setText("")
                chip.setIcon(QIcon(thumb))
                chip.setIconSize(QSize(self._IMG_W, self._IMG_H))
            hdr = self._header_thumbs.get(url)
            if hdr is not None:
                hdr.setPixmap(scaled_for_screen(px, 56, 36))
                hdr.setText("")
        except RuntimeError:
            pass   # dialog already closed

    def selection(self) -> dict:
        """Chosen pieces: description/developer/year/image mapped to the
        picked value or None (keep current / skip), plus tag, url and review
        lists — reviews arrive already flattened, a whole source at a time."""
        sel = {
            'tags': [v for cb, v in self._tag_boxes if cb.isChecked()],
            'urls': [v for cb, v in self._url_boxes if cb.isChecked()],
            'reviews': [r for cb, group in self._review_boxes
                        if cb.isChecked() for r in group],
        }
        for field, group in self._field_groups.items():
            btn = group.checkedButton()
            sel[field] = btn.property('opt_value') if btn is not None else None
        return sel


