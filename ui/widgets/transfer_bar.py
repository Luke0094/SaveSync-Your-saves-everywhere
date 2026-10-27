"""SaveSync - a progress bar that can be read.

The application style gives every QProgressBar a four-pixel height and a
transparent text colour: the thin bars the rest of the app wants. A transfer's
bar is the other kind: the number on it is the point, and ``setTextVisible(True)``
on a bar styled that way shows nothing at all. A transfer bar is styled here
instead — taller, with the percentage drawn on it in a colour that can be read
against both the track and the fill.
"""
from PySide6.QtWidgets import QProgressBar

from ui.helpers import scaled
from ui.styles.theme import palette


def transfer_bar(height: int = 18, text_color: str = "", parent=None) -> QProgressBar:
    """A 0-100 bar with its percentage on it. *text_color* is for a bar that sits
    on something other than the page (a coloured chat bubble)."""
    bar = QProgressBar(parent)
    bar.setRange(0, 100)
    style_transfer_bar(bar, height, text_color)
    return bar


def style_transfer_bar(bar: QProgressBar, height: int = 18, text_color: str = "") -> None:
    """Give an existing bar the look of transfer_bar."""
    bar.setTextVisible(True)
    bar.setFormat("%p%")
    bar.setFixedHeight(scaled(height, bar))
    color = text_color or palette("text")
    bar.setStyleSheet(
        f"QProgressBar{{color:{color};font-size:{scaled(11, bar)}px;font-weight:600;"
        f"text-align:center;}}")
