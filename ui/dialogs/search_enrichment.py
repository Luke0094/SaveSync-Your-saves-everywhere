"""
SaveSync - Web-search candidate preview + same-tier enrichment merge UI.

Extracted verbatim from ui/dialogs/add_game_dialog.py:

- CandidatePreviewDialog — arrow-carousel preview of search candidates; the
  candidate the user confirms is authoritative for every field it fills.
- EnrichmentMergeDialog — chip-based "fill the EMPTY fields" panel offered
  from the OTHER candidates of the same result set (same tier, never new
  searches), grouped by source; tags/links always extend additively.
- _FlowLayout / _ChipGroup / _merge_chip — the wrapping chip machinery.

Pure move — no behavior change.
"""
import logging
import threading

from PySide6.QtCore import Qt, QSize, QRect, QPoint, Signal
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QWidget, QLayout, QSizePolicy,
)

from i18n import t
from ui.styles.theme import palette

logger = logging.getLogger(__name__)


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

        _arrow_css = (
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:4px;"
            f"font-weight:bold;font-size:18px;}}"
            f"QPushButton:hover{{background:{palette('accent')};color:{palette('accent_text')};}}"
            f"QPushButton:disabled{{color:{palette('text_muted')};border-color:{palette('border')};}}"
        )

        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedWidth(30)
        self._prev_btn.setMinimumHeight(90)
        self._prev_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._prev_btn.setToolTip(t('add_game.candidate_prev'))
        self._prev_btn.setStyleSheet(_arrow_css)
        self._prev_btn.clicked.connect(self._go_prev)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedWidth(30)
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

        # ── Description ──────────────────────────────────────────────────
        _desc_field = fields.get('description')
        _new_desc = (_desc_field['new'] if _desc_field else (c.description or '')) or ''
        _new_desc = _new_desc.strip()
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
            self._thumb_lbl.setPixmap(px.scaled(
                112, 70,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


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
    deselects the same field chips elsewhere, and the leading "current /
    do not import" chip makes keeping the existing value (or skipping) an
    explicit choice. Tag and link chips toggle independently. Nothing touches
    the form until Apply."""

    _ELIDE = 60   # preview length inside a chip

    _FIELDS = ('description', 'developer', 'year', 'image')

    _thumb_ready = Signal(str, bytes)   # image url, downloaded bytes

    def __init__(self, model: dict, source_label_fn, parent=None):
        super().__init__(parent)
        self._model = model
        self._src_label = source_label_fn
        self._field_groups: dict[str, _ChipGroup] = {}
        self._tag_boxes: list[tuple[QPushButton, str]] = []
        self._url_boxes: list[tuple[QPushButton, str]] = []
        self._img_chips: dict[str, QPushButton] = {}   # image url → its chip
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(t('add_game.merge_title'))
        self.setMinimumWidth(520)
        self.setMaximumHeight(640)
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

    def _section_lbl(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{palette('text_muted')};font-size:10px;font-weight:700;"
            f"letter-spacing:0.5px;margin-top:6px;"
        )
        return lbl

    def _field_chip(self, field: str, text: str, value, tooltip: str = "",
                    checked: bool = False) -> QPushButton:
        chip = _merge_chip(text, tooltip)
        chip.setProperty('opt_value', value)
        group = self._field_groups.setdefault(field, _ChipGroup())
        group.add(chip)
        chip.setChecked(checked)
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
        content = QWidget()
        content.setStyleSheet("background:transparent;")
        col = QVBoxLayout(content)
        col.setSpacing(4)
        col.setContentsMargins(0, 0, 6, 0)

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
        for kind in ('tags', 'urls'):
            for opt in self._model.get(kind, []):
                by_source.setdefault(opt['source'], {}).setdefault(kind, []).append(opt['value'])

        _field_first_taken = {f: False for f in self._FIELDS}

        for source, offers in by_source.items():
            col.addWidget(self._section_lbl(self._src_label(source)))
            flow = _FlowLayout(spacing=6)
            host = QWidget()
            host.setLayout(flow)
            for field in self._FIELDS:
                for value in offers.get(field, []):
                    if field == 'image':
                        text = t('add_game.image')
                    else:
                        text = self._field_title(field) + ": " + self._short(value)
                    checked = not _field_first_taken[field]
                    _field_first_taken[field] = True
                    chip = self._field_chip(field, text, value,
                                            tooltip=value, checked=checked)
                    if field == 'image':
                        # Async thumbnail preview lands on the chip icon
                        self._img_chips[value] = chip
                    flow.addWidget(chip)
            for tag in offers.get('tags', []):
                chip = _merge_chip(tag)
                chip.setChecked(True)
                flow.addWidget(chip)
                self._tag_boxes.append((chip, tag))
            for url in offers.get('urls', []):
                chip = _merge_chip(self._short(url, 44), tooltip=url)
                chip.setChecked(True)
                flow.addWidget(chip)
                self._url_boxes.append((chip, url))
            col.addWidget(host)

        col.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t('common.cancel'))
        cancel_btn.setMinimumWidth(90)
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton(t('common.apply'))
        apply_btn.setObjectName("primary_btn")
        apply_btn.setMinimumWidth(90)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        outer.addLayout(btn_row)

    def _start_thumb_downloads(self):
        """Fetch a small preview for every offered image, off the GUI thread;
        _on_thumb_ready puts it on the chip as its icon."""
        import urllib.request as _url_req
        _UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        for url in list(self._img_chips):
            def _dl(u=url):
                try:
                    req = _url_req.Request(u, headers={"User-Agent": _UA})
                    with _url_req.urlopen(req, timeout=8) as r:
                        data = r.read(2_000_000)
                    if data:
                        self._thumb_ready.emit(u, data)
                except Exception:
                    pass   # no preview — the chip stays text-only
            threading.Thread(target=_dl, daemon=True).start()

    def _on_thumb_ready(self, url: str, data: bytes):
        chip = self._img_chips.get(url)
        if chip is None:
            return
        try:
            px = QPixmap()
            if px.loadFromData(data):
                chip.setIcon(QIcon(px.scaled(
                    48, 30,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )))
                chip.setIconSize(QSize(48, 30))
                chip.setFixedHeight(38)
        except RuntimeError:
            pass   # dialog already closed

    def selection(self) -> dict:
        """Chosen pieces: description/developer/year/image mapped to the
        picked value or None (keep current / skip), plus tag and url lists."""
        sel = {
            'tags': [v for cb, v in self._tag_boxes if cb.isChecked()],
            'urls': [v for cb, v in self._url_boxes if cb.isChecked()],
        }
        for field, group in self._field_groups.items():
            btn = group.checkedButton()
            sel[field] = btn.property('opt_value') if btn is not None else None
        return sel


