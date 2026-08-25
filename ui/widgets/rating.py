"""SaveSync — star ratings, drawn rather than spelled out.

Five stars filled to a quarter of a star, plus the number beside them: the
stars are read at a glance, the number is what says 3.75 rather than
"nearly four". Both come from the same value, so they can never disagree.

Two widgets, the same painting code:

- ``StarRating``      — read-only, for the library card and row;
- ``StarRatingInput`` — click or drag to set, for the reviews panel.

The stars are painted, not text: the ★ glyph is drawn by whichever font the
system hands over, at a size and weight nobody here chose, and a half-filled
one cannot be written at all.
"""

import logging

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from core.library import RATING_MAX, RATING_STEP, quantize_rating
from i18n import t
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

_STARS = int(RATING_MAX)


def _star_polygon(rect: QRectF) -> QPolygonF:
    """A five-pointed star inscribed in *rect*."""
    import math
    cx, cy = rect.center().x(), rect.center().y()
    outer = min(rect.width(), rect.height()) / 2
    inner = outer * 0.42
    points = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        # -90° so a point faces up rather than right.
        angle = math.radians(-90 + i * 36)
        points.append(QPointF(cx + r * math.cos(angle),
                              cy + r * math.sin(angle)))
    return QPolygonF(points)


def _paint_stars(widget: QWidget, painter: QPainter, value: float,
                 size: int, spacing: int):
    """Draw five stars filled to *value*, left-aligned and vertically centred."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    filled = QColor(palette("warning"))
    empty = QColor(palette("text_disabled"))
    top = (widget.height() - size) / 2
    for i in range(_STARS):
        rect = QRectF(i * (size + spacing), top, size, size)
        star = QPainterPath()
        star.addPolygon(_star_polygon(rect))
        star.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(empty)
        painter.drawPath(star)
        # How much of THIS star is filled: 1 for the ones below the value,
        # the remainder for the one it falls inside, 0 above.
        fill = max(0.0, min(1.0, value - i))
        if fill <= 0:
            continue
        painter.save()
        # Clipping the star to a fraction of its width is what makes a
        # quarter-star readable — a scaled-down star would just look smaller.
        painter.setClipRect(QRectF(rect.left(), rect.top(),
                                   rect.width() * fill, rect.height()))
        painter.setBrush(filled)
        painter.drawPath(star)
        painter.restore()


class StarRating(QWidget):
    """Read-only stars plus the numeric score, e.g. ★★★★☆ 3.75.

    An unrated game shows empty stars and a dash, not "0" — zero is a score
    somebody gave, absent is not.
    """

    def __init__(self, value: float = 0.0, star_size: int = 11,
                 spacing: int = 1, font_size: int = 11, parent=None):
        """Sizes are DESIGN units at 100%, scaled here like every other one.

        They used to be taken as literal pixels. Every other measurement in
        a card goes through ``scaled()`` — the row height, the paddings, the
        stylesheet's font-size — so at any UI scale other than 1.0 the stars
        and their number stayed at their design size inside a row that had
        grown around them, which is why the rating read as tiny next to the
        playtime beside it. Scaling here rather than at each call site so a
        new one cannot forget.
        """
        super().__init__(parent)
        from ui.helpers import scaled as _scaled
        star_size = max(6, _scaled(star_size, parent))
        spacing = max(0, _scaled(spacing, parent)) if spacing else spacing
        font_size = max(7, _scaled(font_size, parent))
        self._value = quantize_rating(value)
        self._star_size = star_size
        self._spacing = spacing
        self._stars_width = _STARS * (star_size + spacing)

        row = QHBoxLayout(self)
        row.setContentsMargins(self._stars_width + 4, 0, 0, 0)
        row.setSpacing(0)
        self._num = QLabel()
        self._num.setObjectName("rating_value")
        self._font_size = font_size
        row.addWidget(self._num)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(max(star_size + 2, font_size + 6))
        self._apply()

    def set_value(self, value: float):
        self._value = quantize_rating(value)
        self._apply()

    def value(self) -> float:
        return self._value

    def refresh_styles(self):
        """Re-read the palette: stars on paint, the number on its stylesheet."""
        self._apply()

    def _apply(self):
        rated = self._value > 0
        # Two decimals would show 3.50 where 3.5 is meant; one keeps 3.25
        # honest and 4 short.
        text = (f"{self._value:.2f}".rstrip("0").rstrip(".") if rated
                else t("rating.unrated_short"))
        self._num.setText(text)
        self._num.setStyleSheet(
            f"color:{palette('text_secondary' if rated else 'text_disabled')};"
            f"font-size:{self._font_size}px;")
        self.setToolTip(t("rating.average_tooltip", value=text) if rated
                        else t("rating.unrated"))
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        _paint_stars(self, painter, self._value, self._star_size, self._spacing)


class StarRatingInput(QWidget):
    """Stars the user sets: click or drag, quarter-star steps.

    Clicking the star already selected clears the rating — the only way back
    to "no rating" once one has been given, and less surprising than a
    separate button nobody would look for.
    """

    value_changed = Signal(float)

    def __init__(self, value: float = 0.0, star_size: int = 22,
                 spacing: int = 4, parent=None):
        # Design units, scaled — see StarRating.__init__.
        super().__init__(parent)
        from ui.helpers import scaled as _scaled
        star_size = max(10, _scaled(star_size, parent))
        spacing = max(1, _scaled(spacing, parent))
        self._value = quantize_rating(value)
        self._preview = None       # what the pointer is over, while hovering
        self._star_size = star_size
        self._spacing = spacing
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_STARS * (star_size + spacing), star_size + 4)
        self.setToolTip(t("rating.pick_tooltip"))

    def value(self) -> float:
        return self._value

    def set_value(self, value: float):
        self._value = quantize_rating(value)
        self.update()

    def refresh_styles(self):
        self.update()

    def _value_at(self, x: float) -> float:
        """The rating the pointer at *x* is asking for, snapped to the grid."""
        step_px = (self._star_size + self._spacing)
        raw = (x + self._spacing) / step_px
        # Round UP to the next quarter so the leftmost sliver of the first
        # star is 0.25 rather than 0 — every position inside the strip has to
        # mean some rating, or the first star could not be given at all.
        import math
        snapped = math.ceil(raw / RATING_STEP) * RATING_STEP
        return max(RATING_STEP, min(RATING_MAX, round(snapped, 2)))

    def mouseMoveEvent(self, event):
        self._preview = self._value_at(event.position().x())
        self.update()

    def leaveEvent(self, event):
        self._preview = None
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        picked = self._value_at(event.position().x())
        self._value = 0.0 if picked == self._value else picked
        self._preview = None
        self.update()
        self.value_changed.emit(self._value)

    def paintEvent(self, event):
        painter = QPainter(self)
        shown = self._preview if self._preview is not None else self._value
        _paint_stars(self, painter, shown, self._star_size, self._spacing)
