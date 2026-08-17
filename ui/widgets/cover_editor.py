"""
SaveSync - Custom cover framing editor.

The 3x3 preset grid answers "which ninth of the image do I want"; this answers
"exactly how do I want it framed": pan freely, zoom, and pick how the image
meets the frame (fill / fit / stretch). The result is written back into
GameEntry.cover_focus as a ``custom:…`` string, so every existing consumer of
that field keeps working unchanged (see game_items.parse_cover_focus).

The preview is the card's real size, and the info-panel toggle reproduces the
library card's own bottom overlay — same geometry and same palette-derived
tint — so what the user frames is what the card shows.
"""
import logging

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QSlider, QCheckBox,
    QSizePolicy,
)

from i18n import t
from ui.helpers import scaled
from ui.styles.theme import palette

logger = logging.getLogger(__name__)

CARD_W, CARD_H = 186, 240          # the library card's real cover size
_INFO_TOP, _INFO_H = 130, 110      # its bottom info overlay (GameCard._build)


class _PreviewLabel(QLabel):
    """Cover preview that pans on drag and zooms on wheel."""
    dragged = Signal(int, int)     # dx, dy in pixels
    zoomed = Signal(int)           # wheel steps, +in / -out

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last: QPoint | None = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(CARD_W, CARD_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._last = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._last is None:
            return
        pos = event.position().toPoint()
        delta = pos - self._last
        self._last = pos
        self.dragged.emit(delta.x(), delta.y())

    def mouseReleaseEvent(self, event):
        self._last = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        steps = event.angleDelta().y()
        if steps:
            self.zoomed.emit(1 if steps > 0 else -1)
            event.accept()


class CoverCustomEditor(QWidget):
    """Interactive framing editor. Read the result from focus_string()."""

    changed = Signal()

    _ZOOM_MIN, _ZOOM_MAX = 100, 400        # slider units = percent
    _ZOOM_STEP = 10

    def __init__(self, source: QPixmap, game_name: str = "",
                 focus: str = "center", parent=None):
        super().__init__(parent)
        from ui.widgets.game_items import parse_cover_focus
        self._src = source
        self._game_name = game_name
        mode, zoom, x, y = parse_cover_focus(focus)
        self._mode, self._zoom, self._x, self._y = mode, zoom, x, y
        self._build()
        self._refresh()

    # ── Public ───────────────────────────────────────────────────────────────

    def focus_string(self) -> str:
        from ui.widgets.game_items import format_cover_focus
        return format_cover_focus(self._mode, self._zoom, self._x, self._y)

    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        hint = QLabel(t("library.cover_custom_hint"))
        hint.setObjectName("cover_editor_hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ── Preview, framed exactly like a card ──────────────────────────────
        preview_row = QHBoxLayout()
        preview_row.addStretch()

        self._stage = QWidget()
        self._stage.setFixedSize(CARD_W, CARD_H)
        self._preview = _PreviewLabel(self._stage)
        self._preview.move(0, 0)
        self._preview.setStyleSheet(
            f"background:{palette('bg_elevated')};border-radius:10px;")
        self._preview.dragged.connect(self._on_drag)
        self._preview.zoomed.connect(self._on_wheel)

        # The card's own bottom overlay, reproduced at its real geometry so the
        # toggle shows how much of the framing the panel actually covers.
        self._info_panel = QWidget(self._stage)
        self._info_panel.setGeometry(0, _INFO_TOP, CARD_W, _INFO_H)
        self._info_name = QLabel(self._game_name or t("common.unknown"), self._info_panel)
        self._info_name.setGeometry(10, 6, CARD_W - 20, 18)
        self._info_panel.setVisible(False)
        self._info_panel.raise_()

        preview_row.addWidget(self._stage)
        preview_row.addStretch()
        root.addLayout(preview_row)

        # ── Fit mode ─────────────────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self._mode_btns: dict[str, QPushButton] = {}
        for key, label_key in (("cover", "library.cover_mode_cover"),
                               ("fit", "library.cover_mode_fit"),
                               ("stretch", "library.cover_mode_stretch")):
            btn = QPushButton(t(label_key))
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _c=False, k=key: self._set_mode(k))
            self._mode_btns[key] = btn
            mode_row.addWidget(btn)
        root.addLayout(mode_row)

        # ── Zoom ─────────────────────────────────────────────────────────────
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(8)
        self._zoom_lbl = QLabel()
        self._zoom_lbl.setFixedWidth(scaled(74, self))
        # Kept as attributes and styled in apply_theme: an unstyled
        # QPushButton inherits the app's default 7px/16px padding, which in a
        # 26px box leaves no room for the glyph — it renders as a bare filled
        # square, which is exactly how these two looked.
        self._zoom_out_btn = QPushButton("−")
        self._zoom_out_btn.setFixedSize(scaled(26, self), scaled(26, self))
        self._zoom_out_btn.setToolTip(t("library.cover_zoom_out"))
        self._zoom_out_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zoom_out_btn.clicked.connect(lambda: self._on_wheel(-1))
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(self._ZOOM_MIN, self._ZOOM_MAX)
        self._zoom_slider.setSingleStep(self._ZOOM_STEP)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self._zoom_in_btn = QPushButton("+")
        self._zoom_in_btn.setFixedSize(scaled(26, self), scaled(26, self))
        self._zoom_in_btn.setToolTip(t("library.cover_zoom_in"))
        self._zoom_in_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zoom_in_btn.clicked.connect(lambda: self._on_wheel(+1))
        zoom_row.addWidget(self._zoom_lbl)
        zoom_row.addWidget(self._zoom_out_btn)
        zoom_row.addWidget(self._zoom_slider, 1)
        zoom_row.addWidget(self._zoom_in_btn)
        root.addLayout(zoom_row)

        # ── Info-card toggle + reset ─────────────────────────────────────────
        bottom_row = QHBoxLayout()
        self._info_cb = QCheckBox(t("library.cover_show_info"))
        self._info_cb.setToolTip(t("library.cover_show_info_tooltip"))
        self._info_cb.toggled.connect(self._info_panel.setVisible)
        bottom_row.addWidget(self._info_cb)
        bottom_row.addStretch()
        self._reset_btn = QPushButton(t("library.cover_reset"))
        self._reset_btn.clicked.connect(self._reset)
        bottom_row.addWidget(self._reset_btn)
        root.addLayout(bottom_row)

        self.apply_theme()

    def apply_theme(self):
        """(Re-)apply palette-derived styling — called on build and whenever
        the theme changes under an open dialog."""
        from ui.widgets.game_items import _hex_to_rgb
        self._preview.setStyleSheet(
            f"background:{palette('bg_elevated')};border-radius:10px;")
        self._info_panel.setStyleSheet(
            f"background:rgba({_hex_to_rgb(palette('bg_card'))},0.88);"
            f"border-radius:0 0 10px 10px;")
        self._info_name.setStyleSheet(
            f"color:{palette('text')};font-size:{scaled(12, self)}px;font-weight:600;"
            f"background:transparent;")
        self._zoom_lbl.setObjectName("cover_editor_hint")
        self._zoom_lbl.setStyleSheet("")
        normal = (
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:6px;padding:5px 8px;font-size:{scaled(11, self)}px;}}"
            f"QPushButton:hover{{border-color:{palette('accent')};}}"
            f"QPushButton:checked{{background:{palette('accent')};color:{palette('accent_text')};"
            f"border-color:{palette('accent')};}}"
        )
        for btn in self._mode_btns.values():
            btn.setStyleSheet(normal)
        self._reset_btn.setStyleSheet(normal)
        # Square icon buttons need their own rule: no padding, centred glyph.
        stepper = (
            f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
            f"border:1px solid {palette('border')};border-radius:4px;padding:0px;"
            f"font-size:{scaled(15, self)}px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{palette('accent')};color:{palette('accent')};}}"
            f"QPushButton:pressed{{background:{palette('bg_hover')};}}"
        )
        for btn in (self._zoom_out_btn, self._zoom_in_btn):
            btn.setStyleSheet(stepper)

    # ── Interaction ──────────────────────────────────────────────────────────

    def _set_mode(self, mode: str):
        self._mode = mode
        self._refresh()

    def _on_zoom_slider(self, value: int):
        zoom = value / 100.0
        if abs(zoom - self._zoom) > 1e-6:
            self._zoom = zoom
            self._refresh()

    def _on_wheel(self, direction: int):
        value = self._zoom_slider.value() + direction * self._ZOOM_STEP
        self._zoom_slider.setValue(
            max(self._ZOOM_MIN, min(self._ZOOM_MAX, value)))

    def _on_drag(self, dx: int, dy: int):
        """Pan. One formula covers both directions because the offset is
        ``(frame - scaled) * position``: with the image larger than the frame
        that term is negative, so dragging right lowers the position — with it
        smaller (fit mode) it is positive and dragging right raises it."""
        sw, sh = self._scaled_size()
        denom_x, denom_y = CARD_W - sw, CARD_H - sh
        if denom_x:
            self._x = max(0.0, min(1.0, self._x + dx / denom_x))
        if denom_y:
            self._y = max(0.0, min(1.0, self._y + dy / denom_y))
        self._refresh()

    def _reset(self):
        self._mode, self._zoom, self._x, self._y = "cover", 1.0, 0.5, 0.5
        self._refresh()

    # ── Render ───────────────────────────────────────────────────────────────

    def _scaled_size(self) -> tuple[int, int]:
        """Size the source takes at the current mode/zoom — the same scaling
        render_cover does, needed here to turn a pixel drag into a position."""
        tw = max(1, int(round(CARD_W * self._zoom)))
        th = max(1, int(round(CARD_H * self._zoom)))
        if self._src.isNull():
            return tw, th
        if self._mode == "stretch":
            return tw, th
        sw, sh = self._src.width(), self._src.height()
        if not sw or not sh:
            return tw, th
        ratio_w, ratio_h = tw / sw, th / sh
        ratio = max(ratio_w, ratio_h) if self._mode == "cover" else min(ratio_w, ratio_h)
        return max(1, int(round(sw * ratio))), max(1, int(round(sh * ratio)))

    def _refresh(self):
        from ui.helpers import display_scale
        from ui.widgets.game_items import render_cover
        for key, btn in self._mode_btns.items():
            btn.setChecked(key == self._mode)
        slider_value = int(round(self._zoom * 100))
        if self._zoom_slider.value() != slider_value:
            self._zoom_slider.blockSignals(True)
            self._zoom_slider.setValue(
                max(self._ZOOM_MIN, min(self._ZOOM_MAX, slider_value)))
            self._zoom_slider.blockSignals(False)
        self._zoom_lbl.setText(t("library.cover_zoom", percent=slider_value))
        if not self._src.isNull():
            # At the screen's own scale, like the card this is previewing —
            # a preview drawn at fewer pixels than the thing it stands for
            # would show framing that looks rougher than the result.
            self._preview.setPixmap(
                render_cover(self._src, CARD_W, CARD_H, self.focus_string(),
                             display_scale()))
        else:
            self._preview.setText("🖼️")
        self.changed.emit()
