"""SaveSync — reviews for one game.

A game can carry any number of reviews: the user's own, and whatever a web
source had to say about it. Each is a rating in quarter stars, who wrote it,
the review itself and private notes that are not part of it.

Why a window of its own rather than another row in the add/edit form: a
review is paragraphs, not a field, and there can be dozens. The form has
room for neither, and the list needs paging for the same reason every other
long list in the app does.

The reviews are edited on a COPY and handed back only when the dialog is
accepted, so the caller (ui/dialogs/add_game_dialog.py) writes them to the
library entry on its own Save and a cancel changes nothing.
"""

import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QTextEdit, QVBoxLayout, QWidget)

from core.game_sources.common import source_label
from core.library import (quantize_rating, review_rating, review_vote_count,
                          reviews_display_count)
from i18n import t
from ui.helpers import finalize_adaptive_dialog_size, scaled
from ui.modal_helpers import question_window_modal
from ui.styles.theme import palette
from ui.widgets.page_size import SCOPE_REVIEWS, PageSizeCombo, page_size
from ui.widgets.rating import StarRating, StarRatingInput

logger = logging.getLogger(__name__)

# A review is prose and gets room for it; notes are a reminder to self. The
# caps exist so one entry cannot make the library file unwieldy, and are the
# reason both fields show a counter rather than silently swallowing text.
REVIEW_MAX_CHARS = 4000
NOTES_MAX_CHARS = 600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when_text(iso: str) -> str:
    """The stored timestamp as a short local date, or "" when unreadable."""
    if not iso:
        return ""
    try:
        from dateutil.parser import parse as parse_dt
        return parse_dt(iso).astimezone().strftime("%d/%m/%Y")
    except Exception:
        return ""


class _CountedTextEdit(QTextEdit):
    """A text box with a hard character cap and a counter under it."""

    def __init__(self, limit: int, placeholder: str, height: int, parent=None):
        super().__init__(parent)
        self._limit = limit
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(height)
        self.setStyleSheet(
            f"QTextEdit{{background:{palette('bg_input')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:4px;"
            f"padding:4px;font-size:{scaled(12, self)}px;}}")
        self.counter = QLabel()
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.textChanged.connect(self._enforce)
        self._enforce()

    def _enforce(self):
        text = self.toPlainText()
        if len(text) > self._limit:
            # Truncate in place and leave the cursor at the end, so typing
            # past the cap simply stops instead of scrolling back to the top.
            cursor = self.textCursor()
            at_end = cursor.position() >= len(text)
            self.blockSignals(True)
            self.setPlainText(text[:self._limit])
            self.blockSignals(False)
            if at_end:
                self.moveCursor(cursor.MoveOperation.End)
            text = self.toPlainText()
        used = len(text)
        full = used >= self._limit
        self.counter.setText(f"{used}/{self._limit}")
        self.counter.setStyleSheet(
            f"color:{palette('warning' if full else 'text_hint')};font-size:{scaled(10, self)}px;")


class _ReviewCard(QFrame):
    """One stored review: rating, who, when, the text, the notes."""

    def __init__(self, review: dict, on_edit, on_delete, parent=None):
        super().__init__(parent)
        self.setObjectName("review_card")
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 8, 10, 8)
        col.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(StarRating(review_rating(review), star_size=12,
                                  font_size=11))
        who = (review.get("reviewer") or "").strip() or t("reviews.you")
        who_lbl = QLabel(who)
        who_lbl.setObjectName("review_who")
        head.addWidget(who_lbl)
        when = _when_text(review.get("at", ""))
        if when:
            when_lbl = QLabel(when)
            when_lbl.setObjectName("review_when")
            head.addWidget(when_lbl)
        # Where a score came from decides how much it is worth, so a review
        # that isn't the user's own says which site gave it — the reviewer
        # name alone ("Metacritic") does not distinguish the site that
        # aggregated the score from the one SaveSync actually asked.
        src = str(review.get("source") or "").strip()
        if src and src != "user":
            src_lbl = QLabel(source_label(src) or src)
            src_lbl.setToolTip(t("reviews.from_source", source=src))
            src_lbl.setObjectName("review_meta_sm")
            head.addWidget(src_lbl)
        votes = review_vote_count(review)
        # Aggregates only — an ordinary user/DLsite review is one opinion.
        if votes > 1:
            votes_lbl = QLabel(t("reviews.votes_n", count=votes))
            votes_lbl.setObjectName("review_meta_sm")
            head.addWidget(votes_lbl)
        head.addStretch()

        edit_btn = QPushButton("✏")
        del_btn = QPushButton("🗑")
        for btn, tip, cb in ((edit_btn, t("reviews.edit"), on_edit),
                             (del_btn, t("reviews.delete"), on_delete)):
            btn.setObjectName("icon_btn")
            btn.setFixedSize(scaled(24, btn), scaled(24, btn))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(cb)
            head.addWidget(btn)
        col.addLayout(head)

        text = (review.get("text") or "").strip()
        if text:
            # Body row: the prose on the left, a copy control pinned to the
            # top-right. Reviews often arrive in a language the user does not
            # read; copying the text is what lets them paste it into whatever
            # translator they already use, without SaveSync having to ship one.
            body_row = QHBoxLayout()
            body_row.setSpacing(6)
            body_row.setAlignment(Qt.AlignmentFlag.AlignTop)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setObjectName("review_body")
            body_row.addWidget(body, 1)
            copy_btn = QPushButton("⧉")
            copy_btn.setObjectName("icon_btn")
            copy_btn.setFixedSize(scaled(24, copy_btn), scaled(24, copy_btn))
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setToolTip(t("reviews.copy_tooltip"))
            copy_btn.clicked.connect(
                lambda _=False, btn=copy_btn, payload=text:
                    self._copy_text(btn, payload))
            body_row.addWidget(copy_btn, 0, Qt.AlignmentFlag.AlignTop)
            col.addLayout(body_row)

        notes = (review.get("notes") or "").strip()
        if notes:
            notes_lbl = QLabel(f"{t('reviews.notes')}: {notes}")
            notes_lbl.setWordWrap(True)
            notes_lbl.setObjectName("review_notes")
            col.addWidget(notes_lbl)

    @staticmethod
    def _copy_text(btn: QPushButton, text: str):
        QApplication.clipboard().setText(text)
        btn.setToolTip(t("reviews.copied"))
        # Put the permanent tip back after a beat so the next hover is not
        # stuck saying "Copied!" from a previous click. The button may have
        # been destroyed with the dialog by then — swallow that quietly.
        def _restore(b=btn):
            try:
                b.setToolTip(t("reviews.copy_tooltip"))
            except RuntimeError:
                pass
        QTimer.singleShot(1500, _restore)


class ReviewsDialog(QDialog):
    """Add, edit and remove the reviews of one game."""

    def __init__(self, game_name: str, reviews: list, parent=None):
        super().__init__(parent)
        self._reviews = [dict(r) for r in (reviews or [])]
        self._editing_index = -1        # -1 = the form is composing a new one
        self._page = 1
        self._review_gen = 0            # stale-pump guard for card insertion
        self._pending_reviews: list = []
        self._review_busy = None

        self.setWindowTitle(t("reviews.title_for", name=game_name)
                            if game_name else t("reviews.title"))
        # Native HWND paints COLOR_WINDOW (often white) before QSS — seed the
        # palette so the first mapped frame matches the theme background
        # (same construction as the add/edit dialog that opens this one).
        self._apply_window_chrome()
        # Form fields keep fixed heights; the review list scrolls. Size from
        # host/screen so short displays get a scrollbar instead of crush.
        # WindowModal like the add/edit dialog that opens it: the overlay and
        # the rest of the app stay usable.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._build()
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=620, min_h=780, scroll=self._scroll, list_content=True)
        if parent is not None and parent.isVisible():
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())
        self._refresh()

    def _apply_window_chrome(self):
        """Theme-coloured native window fill before the first paint.

        Without this, Windows maps a white client area for one frame while
        Qt resolves stylesheets — the white flash seen on every open.
        """
        from PySide6.QtGui import QColor, QPalette
        bg = QColor(palette("bg"))
        fg = QColor(palette("text"))
        card = QColor(palette("bg_card"))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.Base, card)
        pal.setColor(QPalette.ColorRole.AlternateBase, bg)
        pal.setColor(QPalette.ColorRole.Button, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Text, fg)
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QDialog {{ background-color: {palette('bg')}; color: {palette('text')}; }}"
            f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}"
        )
        try:
            from ui.helpers import set_dark_title_bar
            set_dark_title_bar(self)
        except Exception:
            pass

    def showEvent(self, event):
        self._apply_window_chrome()
        parent = self.parentWidget()
        if parent is not None and parent.isVisible():
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())
        super().showEvent(event)

    def show_settled(self):
        """Map the window at its already-chosen size with zero white flash (identical to AddGameDialog)."""
        self._apply_window_chrome()
        self.setWindowOpacity(0.0)
        self.setUpdatesEnabled(False)
        try:
            QDialog.show(self)
            self.ensurePolished()
        finally:
            self.setUpdatesEnabled(True)
        self.repaint()
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        self.setWindowOpacity(1.0)
        self.raise_()
        self.activateWindow()

    def exec(self) -> int:
        self.show_settled()
        return super().exec()



    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(t("reviews.title"))
        title.setObjectName("dialog_title")
        head.addWidget(title)
        head.addStretch()
        self._avg_caption = QLabel(t("reviews.average"))
        self._avg_caption.setObjectName("review_when")
        head.addWidget(self._avg_caption)
        self._avg = StarRating(0.0, star_size=13, font_size=12)
        head.addWidget(self._avg)
        root.addLayout(head)

        root.addWidget(self._build_form())
        root.addWidget(self._build_list(), 1)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = QPushButton(t("common.save_changes"))
        save.setObjectName("primary_btn")
        save.setDefault(True)
        save.clicked.connect(self._on_accept)
        footer.addWidget(save)
        root.addLayout(footer)

    def _build_form(self) -> QWidget:
        box = QFrame()
        box.setObjectName("review_form")
        col = QVBoxLayout(box)
        col.setContentsMargins(12, 10, 12, 10)
        col.setSpacing(6)

        self._form_title = QLabel(t("reviews.new"))
        self._form_title.setObjectName("review_form_title")
        col.addWidget(self._form_title)

        # Rating and who wrote it on one line: both are short, and the stars
        # are the first thing a review is.
        line = QHBoxLayout()
        line.setSpacing(10)
        self._stars = StarRatingInput()
        line.addWidget(self._stars)
        self._score = QLabel()
        self._score.setObjectName("review_score")
        self._stars.value_changed.connect(self._on_stars)
        line.addWidget(self._score)
        self._reviewer = QLineEdit()
        self._reviewer.setPlaceholderText(t("reviews.reviewer_placeholder"))
        self._reviewer.setObjectName("review_reviewer")
        line.addWidget(self._reviewer, 1)
        col.addLayout(line)

        # The review gets the room; the notes get a strip. That difference is
        # the point of having two fields rather than one.
        self._text = _CountedTextEdit(REVIEW_MAX_CHARS,
                                      t("reviews.text_placeholder"), 130)
        col.addWidget(self._text)
        col.addWidget(self._text.counter)
        self._notes = _CountedTextEdit(NOTES_MAX_CHARS,
                                       t("reviews.notes_placeholder"), 48)
        col.addWidget(self._notes)
        col.addWidget(self._notes.counter)

        actions = QHBoxLayout()
        self._form_hint = QLabel("")
        self._form_hint.setObjectName("review_form_hint")
        actions.addWidget(self._form_hint)
        actions.addStretch()
        self._cancel_edit_btn = QPushButton(t("reviews.cancel_edit"))
        self._cancel_edit_btn.clicked.connect(self._reset_form)
        self._cancel_edit_btn.setVisible(False)
        actions.addWidget(self._cancel_edit_btn)
        self._commit_btn = QPushButton(t("reviews.add"))
        self._commit_btn.setObjectName("form_primary_btn")
        self._commit_btn.clicked.connect(self._commit)
        actions.addWidget(self._commit_btn)
        col.addLayout(actions)
        self._on_stars(0.0)
        return box

    def _build_list(self) -> QWidget:
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent; border: none;")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._scroll.viewport().setAutoFillBackground(False)
        # A page of reviews is taller than the panel whatever its size, so the
        # vertical bar is expected; a horizontal one would only mean the text
        # failed to wrap.
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_host = QWidget()
        self._list_host.setObjectName("transparent_bg")
        self._list_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        self._list.addStretch()
        self._scroll.setWidget(self._list_host)
        col.addWidget(self._scroll, 1)

        self._empty = QLabel(t("reviews.empty"))
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setObjectName("dialog_empty")
        col.addWidget(self._empty)

        # Page size on the pager row, as everywhere else in the app.
        self._size_combo = PageSizeCombo(SCOPE_REVIEWS,
                                         self._on_page_size_changed)
        self._pager_host = QWidget()
        self._pager_host.setObjectName("transparent_bg")
        self._pager_row = QVBoxLayout(self._pager_host)
        self._pager_row.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self._pager_host)
        return wrap

    # ── Form behaviour ───────────────────────────────────────────────────────

    def _on_stars(self, value: float):
        value = quantize_rating(value)
        self._score.setText(f"{value:.2f}".rstrip("0").rstrip(".") if value
                            else t("rating.unrated_short"))

    def _reset_form(self):
        self._editing_index = -1
        self._stars.set_value(0.0)
        self._on_stars(0.0)
        self._reviewer.clear()
        self._text.clear()
        self._notes.clear()
        self._form_title.setText(t("reviews.new"))
        self._commit_btn.setText(t("reviews.add"))
        self._cancel_edit_btn.setVisible(False)
        self._form_hint.clear()

    def _load_into_form(self, index: int):
        review = self._reviews[index]
        self._editing_index = index
        self._stars.set_value(review_rating(review))
        self._on_stars(review_rating(review))
        self._reviewer.setText(review.get("reviewer") or "")
        self._text.setPlainText(review.get("text") or "")
        self._notes.setPlainText(review.get("notes") or "")
        self._form_title.setText(t("reviews.editing"))
        self._commit_btn.setText(t("common.save_changes"))
        self._cancel_edit_btn.setVisible(True)
        self._form_hint.clear()

    def _commit(self):
        rating = quantize_rating(self._stars.value())
        text = self._text.toPlainText().strip()
        notes = self._notes.toPlainText().strip()
        if not rating and not text:
            # A review with neither a score nor a word says nothing; refusing
            # it here is better than storing a blank row nobody can read.
            self._form_hint.setText(t("reviews.need_rating_or_text"))
            return
        review = {
            "rating": rating,
            "reviewer": self._reviewer.text().strip(),
            "text": text,
            "notes": notes,
            "at": _now_iso(),
        }
        if 0 <= self._editing_index < len(self._reviews):
            kept = self._reviews[self._editing_index]
            # Keep where it came from and when it was first written: editing
            # a web review's wording does not make it this user's own.
            review["source"] = kept.get("source", "user")
            review["at"] = kept.get("at") or review["at"]
            if kept.get("id"):
                review["id"] = kept["id"]
            if kept.get("vote_count"):
                review["vote_count"] = kept["vote_count"]
            self._reviews[self._editing_index] = review
        else:
            review["source"] = "user"
            self._reviews.insert(0, review)   # newest first
            self._page = 1
        self._reset_form()
        self._refresh()

    def _delete(self, index: int):
        from PySide6.QtWidgets import QMessageBox
        answer = question_window_modal(
            self, t("reviews.delete"), t("reviews.delete_confirm"))
        if answer != QMessageBox.StandardButton.Yes:
            return
        if 0 <= index < len(self._reviews):
            self._reviews.pop(index)
        if self._editing_index == index:
            self._reset_form()
        elif self._editing_index > index:
            self._editing_index -= 1
        self._refresh()

    def _on_page_size_changed(self, _size: int):
        self._page = 1
        self._refresh()

    # ── Rendering ────────────────────────────────────────────────────────────

    def _refresh(self):
        # Same vote-weighted mean as GameEntry.average_rating — aggregates
        # (Steam/VNDB) count by vote_count, not as a single row.
        rated = [r for r in self._reviews if review_rating(r) > 0]
        if rated:
            total_w = 0
            acc = 0.0
            for r in rated:
                w = review_vote_count(r)
                if w <= 0:
                    continue
                acc += review_rating(r) * w
                total_w += w
            avg = quantize_rating(acc / total_w) if total_w else 0.0
            self._avg.set_value(avg)
            self._avg_caption.setText(
                t("reviews.average_n", count=reviews_display_count(rated)))
        else:
            self._avg.set_value(0.0)
            self._avg_caption.setText(t("reviews.average"))

        while self._list.count():
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        per_page = page_size(SCOPE_REVIEWS)
        total_pages = max(1, -(-len(self._reviews) // per_page))
        self._page = max(1, min(self._page, total_pages))
        start = (self._page - 1) * per_page
        page_items = list(enumerate(
            self._reviews[start:start + per_page], start=start))

        self._empty.setVisible(not self._reviews)
        self._scroll.setVisible(bool(self._reviews))
        self._render_pager(total_pages)

        # Cards insert in QTimer chunks so a page of reviews never freezes the dialog;
        # generation token prevents stale callbacks from accumulating RAM or writing to destroyed trees.
        self._review_gen += 1
        gen = self._review_gen
        self._stop_review_busy()
        if self.isVisible():
            from ui.widgets.busy_overlay import DeferredBusy
            self._review_busy = DeferredBusy(self, t("common.please_wait"))
        self._pending_reviews = page_items
        QTimer.singleShot(0, lambda g=gen: self._insert_review_step(g))

    def _insert_review_step(self, gen: int):
        if gen != self._review_gen:
            return  # stale pump — a newer _refresh or close took over
        for _ in range(3):  # three cards per tick
            if not self._pending_reviews:
                break
            index, review = self._pending_reviews.pop(0)
            self._list.addWidget(_ReviewCard(
                review,
                on_edit=lambda _=False, i=index: self._load_into_form(i),
                on_delete=lambda _=False, i=index: self._delete(i)))
        if self._pending_reviews:
            QTimer.singleShot(0, lambda g=gen: self._insert_review_step(g))
        else:
            self._stop_review_busy()
            self._list.addStretch()

    def _stop_review_busy(self):
        busy = self._review_busy
        if busy is not None:
            busy.close()
            self._review_busy = None

    def _render_pager(self, total_pages: int):
        # The combo is reparented before the row is wiped: it belongs to the
        # dialog, not to a pager rebuilt on every change.
        self._size_combo.setParent(self)
        while self._pager_row.count():
            item = self._pager_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        def _go(page: int):
            self._page = page
            self._refresh()
            self._scroll.verticalScrollBar().setValue(0)

        from ui.pages.library_page import build_pager
        self._pager_row.addWidget(
            build_pager(self._page, total_pages, _go,
                        size_combo=self._size_combo))

    # ── Result & Cleanup ─────────────────────────────────────────────────────

    def _cleanup_pump(self):
        """Invalidate in-flight card insertion and release pending queue."""
        self._review_gen += 1
        self._pending_reviews.clear()
        self._stop_review_busy()

    def reject(self):
        self._cleanup_pump()
        super().reject()

    def done(self, result: int):
        self._cleanup_pump()
        super().done(result)

    def _on_accept(self):
        # Text still sitting in the form would otherwise be lost without a
        # word: commit it as if the button had been pressed.
        if (self._text.toPlainText().strip()
                or quantize_rating(self._stars.value())):
            self._commit()
        self._cleanup_pump()
        self.accept()

    def reviews(self) -> list:
        """The edited reviews — only meaningful after the dialog was accepted."""
        return [dict(r) for r in self._reviews]
