"""
SaveSync - Theme System
Dark/Light themes inspired by NVIDIA App aesthetics.
"""
import logging
import threading
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

DARK_THEME = """
/* ── Base ─────────────────────────────────────────────────────────── */
QWidget {
    background-color: #111114;
    color: #e8e8ea;
    font-family: "Segoe UI", "SF Pro Display", sans-serif;
    font-size: 13px;
    border: none;
    outline: none;
}

/* Labels & child frames inherit parent bg — prevents colored rectangles on gradients/cards */
QLabel, QCheckBox, QRadioButton {
    background: transparent;
}

QMainWindow, QDialog {
    background-color: #111114;
}

/* ── Sidebar ───────────────────────────────────────────────────────── */
#sidebar {
    background-color: #111114;
    border-right: 1px solid #1e1e24;
    min-width: 220px;
    max-width: 220px;
}

#sidebar_logo {
    color: #76b900;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 24px 20px 8px 20px;
}

#sidebar_tagline {
    color: #4a4a5a;
    font-size: 10px;
    letter-spacing: 0.5px;
    padding: 0 20px 20px 20px;
}

/* ── Nav buttons ───────────────────────────────────────────────────── */
#nav_btn {
    background: transparent;
    color: #6b6b7a;
    font-size: 13px;
    font-weight: 500;
    text-align: left;
    padding: 10px 20px;
    border-radius: 0;
    border-left: 3px solid transparent;
}

#nav_btn:hover {
    background-color: #161619;
    color: #c8c8d0;
    border-left-color: #333340;
}

#nav_btn[active="true"] {
    background-color: #1a1a20;
    color: #e8e8ea;
    border-left-color: #76b900;
}

/* ── Content area ─────────────────────────────────────────────────── */
#content_area {
    background-color: #111114;
}

#page_header {
    color: #f0f0f2;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
}

/* ── Cards ────────────────────────────────────────────────────────── */
#game_card {
    background-color: #111114;
    border: 1px solid #1a1a22;
    border-radius: 8px;
    padding: 16px;
}

#game_card:hover {
    border-color: #2a2a38;
    background-color: #131318;
}

#game_name {
    color: #e8e8ea;
    font-size: 14px;
    font-weight: 600;
}

#game_meta {
    color: #4a4a5a;
    font-size: 11px;
}

/* ── Status badges ────────────────────────────────────────────────── */
#status_synced    { color: #76b900; }
#status_pending   { color: #f5a623; }
#status_conflict  { color: #e84d4d; }
#status_local     { color: #5a8fd6; }
#status_cloud     { color: #9b8bd8; }
#status_no_saves  { color: #4a4a5a; }

/* ── Buttons ──────────────────────────────────────────────────────── */
QPushButton {
    background-color: #1e1e28;
    color: #c8c8d0;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #252530;
    color: #e8e8ea;
    border-color: #3a3a50;
}

QPushButton:pressed {
    background-color: #1a1a22;
}

QPushButton#primary_btn {
    background-color: #76b900;
    color: #000;
    border: none;
    font-weight: 600;
}

QPushButton#primary_btn:hover {
    background-color: #88d000;
}

QPushButton#primary_btn:pressed {
    background-color: #629900;
}

QPushButton#danger_btn {
    background-color: transparent;
    color: #e84d4d;
    border-color: #3a1a1a;
}

QPushButton#danger_btn:hover {
    background-color: #1e0f0f;
    border-color: #e84d4d;
}

QPushButton#icon_btn {
    background: transparent;
    border: none;
    color: #8a8a9a;
    padding: 4px;
    font-size: 16px;
    border-radius: 4px;
}

QPushButton#icon_btn:hover {
    background-color: #1e1e28;
    color: #e8e8ea;
}

/* A glyph-only button that sits in a toolbar NEXT TO ordinary text buttons
   (the library's 🔍, the backups page's ➕). It carries the same chrome as
   QPushButton above so it doesn't read as a bare symbol floating on the
   page; only the padding differs, because the default 7px/16px leaves a
   fixed 30px button no room to draw the glyph. */
QPushButton#toolbar_icon_btn {
    background-color: #1e1e28;
    color: #c8c8d0;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    padding: 0;
    font-size: 15px;
}

QPushButton#toolbar_icon_btn:hover {
    background-color: #252530;
    color: #e8e8ea;
    border-color: #3a3a50;
}

QPushButton#toolbar_icon_btn:pressed {
    background-color: #1a1a22;
}

/* ── Inputs ───────────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #111114;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 13px;
    selection-background-color: #76b900;
    selection-color: #000;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #76b900;
}

QLineEdit::placeholder {
    color: #3a3a4a;
}

/* ── ComboBox ─────────────────────────────────────────────────────── */
QComboBox {
    background-color: #111114;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    padding: 7px 32px 7px 12px;
    min-width: 120px;
}

QComboBox:hover { border-color: #3a3a50; }
QComboBox:focus { border-color: #76b900; }

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #6b6b7a;
    margin-right: 8px;
}

QComboBox QAbstractItemView {
    background-color: #161619;
    border: 1px solid #2a2a38;
    selection-background-color: #76b900;
    selection-color: #000;
    border-radius: 6px;
}

/* Compact page-size control on pager rows — closed box shows a number;
   the popup is widened in code so "Personalizzato…" / "Custom…" fits. */
QComboBox#page_size_combo {
    padding: 2px 18px 2px 6px;
    min-width: 0px;
    max-width: 52px;
    border-radius: 4px;
}
QComboBox#page_size_combo::drop-down {
    width: 16px;
}
QComboBox#page_size_combo::down-arrow {
    margin-right: 4px;
}

/* ── Sliders & Spinboxes ──────────────────────────────────────────── */
QSpinBox {
    background-color: #111114;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    padding: 6px 10px;
}

/* ── CheckBox ─────────────────────────────────────────────────────── */
QCheckBox {
    color: #c8c8d0;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #3a3a50;
    border-radius: 4px;
    background: #111114;
}

QCheckBox::indicator:checked {
    background-color: #76b900;
    border-color: #76b900;
}

/* ── ScrollBar ────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #2a2a3a;
    border-radius: 3px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover { background: #3a3a50; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QScrollBar:horizontal {
    background: transparent;
    height: 6px;
}

QScrollBar::handle:horizontal {
    background: #2a2a3a;
    border-radius: 3px;
    min-width: 30px;
}

/* ── Tab widget ───────────────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid #1e1e24;
    border-radius: 8px;
    background: #111114;
}

QTabBar::tab {
    background: transparent;
    color: #6b6b7a;
    padding: 8px 18px;
    font-size: 12px;
    border-bottom: 2px solid transparent;
}

QTabBar::tab:selected {
    color: #e8e8ea;
    border-bottom-color: #76b900;
}

QTabBar::tab:hover { color: #c8c8d0; }

/* ── ToolTip ──────────────────────────────────────────────────────── */
QToolTip {
    background-color: #1e1e28;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}

/* ── Separator ────────────────────────────────────────────────────── */
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    background: #1e1e24;
    border: none;
    max-height: 1px;
    color: #1e1e24;
}

/* ── Overlay ──────────────────────────────────────────────────────── */
#overlay {
    background-color: rgba(13, 13, 15, 220);
    border: 1px solid #2a2a38;
    border-radius: 12px;
}

#overlay_title {
    color: #76b900;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

#overlay_message {
    color: #c8c8d0;
    font-size: 12px;
}

/* ── Status bar ───────────────────────────────────────────────────── */
QStatusBar {
    background-color: #111114;
    color: #4a4a5a;
    font-size: 11px;
    border-top: 1px solid #1e1e24;
}

/* ── System tray ──────────────────────────────────────────────────── */
QMenu {
    background-color: #161619;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 7px 20px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #1e1e28;
    color: #76b900;
}

QMenu::separator { height: 1px; background: #2a2a38; margin: 4px 8px; }

/* ── Progress bar ─────────────────────────────────────────────────── */
QProgressBar {
    background-color: #1a1a22;
    border-radius: 4px;
    height: 4px;
    text-align: center;
    color: transparent;
}

QProgressBar::chunk {
    background-color: #76b900;
    border-radius: 4px;
}

/* ── GroupBox (Settings, Sync) ────────────────────────────────────── */
/* These values used to live inline in settings_page._group and
   sync_page._make_group, overriding what was here — so this rule never
   actually painted anything. Merged into one definition; the type selector
   means a new section needs no objectName and no styling code at all. */
QGroupBox {
    color: #6b6b7a;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    border: 1px solid #1e1e24;
    border-radius: 8px;
    margin-top: 8px;
    padding: 16px;
}
QGroupBox::title {
    /* NO negative "top": with subcontrol-origin:margin the title's box starts
       at the widget's own top edge, so shifting it up pushed all but the last
       row of the glyphs outside the widget and Qt clipped them away. Sitting
       at the top with margin-top: 8px leaves the border running through the
       middle of the text, which is the look this was reaching for. */
    subcontrol-origin: margin;
    left: 12px;
    background: #111114;
    padding: 0 4px;
}

/* ── Overview – stat cards ────────────────────────────────────────── */
#stat_card {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 8px;
    min-width: 110px;
}

/* ── Overview – active game banner ───────────────────────────────── */
/* The gradient used to be assembled in overview_page._update_banner_style
   only so the start colour could differ per theme — which is what these two
   blocks already do. */
#active_banner {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #0f1a06, stop:1 #111114);
    border: 1px solid #1e3a0a;
    border-left: 3px solid #76b900;
    border-radius: 8px;
}

/* ── Backups – backup row ────────────────────────────────────────── */
#backup_row {
    background: #111114;
    border: 1px solid #1a1a20;
    border-radius: 6px;
}
#backup_row:hover {
    border-color: #2a2a38;
    background: #131318;
}

/* ── Spinbox up/down arrows ──────────────────────────────────────── */
QSpinBox::up-button, QSpinBox::down-button {
    width: 18px;
    background: #1e1e28;
    border: none;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: #2a2a38;
}
QSpinBox::up-arrow {
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid #6b6b7a;
}
QSpinBox::down-arrow {
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid #6b6b7a;
}

/* ── Views & scroll areas (prevent Qt default gray) ──────────────── */
QAbstractItemView, QListWidget, QListView, QTreeView, QTreeWidget,
QTableView, QTableWidget, QHeaderView {
    background-color: #111114;
    color: #e8e8ea;
    border: none;
}

QAbstractScrollArea, QScrollArea {
    background-color: #111114;
    border: none;
}

QAbstractScrollArea > QWidget {
    background-color: #111114;
}

/* ── Dialog ───────────────────────────────────────────────────────── */
QDialog {
    background-color: #111114;
    border: 1px solid #1e1e24;
}

/* ── Library – game card internals ────────────────────────────────── */
/* Same reasoning as the tag chips below: eight styles that never vary
   between cards, once handed to every child of every card. The card FRAME
   itself keeps its inline style — its left border carries the folder
   colour, which is per-card state the QSS can't know. */
#game_card_cover {
    background: #1a1a22;
    font-size: 42px;
    border-radius: 10px;
}

#playing_badge {
    background: #76b900;
    color: #000000;
    font-size: 9px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
}

#game_card_dots {
    background: rgba(0, 0, 0, 0.55);
    border-radius: 8px;
}

#game_card_bottom {
    background: rgba(17, 17, 20, 0.88);
    border-radius: 0 0 10px 10px;
}

/* The card FRAME, for the common case: no folder colour. A card that is
   filed under a coloured folder still gets an inline sheet, because the
   left border carries that colour and the QSS can't know it. Most games
   aren't in a folder, so most cards take this path and carry no sheet.
   Note the geometry differs from #game_card (the list-row frame): radius
   10 and no padding, since the cover fills the card edge to edge. */
QFrame#game_card_grid {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 10px;
}

QFrame#game_card_grid:hover {
    border-color: #2a2a38;
    background: #161619;
}

/* Card playtime: total normally, last session while hovered. Only the TEXT
   swap needs Python; the two colours are constant per theme. The hover
   colour is gated on hasHover, because a game with no recorded session
   keeps its text on hover and so must keep its colour too. */
#playtime_lbl {
    color: #6b6b7a;
    font-size: 9px;
    background: transparent;
}

#playtime_lbl[hasHover="1"]:hover {
    color: #76b900;
}

/* List view: the same pieces, one row instead of one card. */
#game_row_thumb {
    background: #1a1a22;
    border-radius: 6px;
    font-size: 22px;
}

/* Zero padding is REQUIRED: the global QPushButton rule pads 7px 16px, and
   on a fixed 28px button that leaves no content area — the ▶ glyph was
   clipped away entirely. :pressed deliberately repeats the base colour,
   cancelling #primary_btn:pressed exactly as the inline sheet used to. */
QPushButton#row_play_btn {
    background: #76b900;
    color: #000000;
    border: none;
    border-radius: 4px;
    padding: 0;
    font-size: 12px;
    font-weight: 700;
}

QPushButton#row_play_btn:hover {
    background: #88d000;
}

QPushButton#row_play_btn:pressed {
    background: #76b900;
}

#game_card_name {
    color: #e8e8ea;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}

/* Flat card buttons: no background of their own, because the bottom panel
   propagates its rgba fill to children and painted them as darker boxes. */
QPushButton#card_flat_btn, QPushButton#card_flat_btn_lg {
    background: transparent;
    border: none;
    padding: 0;
    color: #6b6b7a;
    font-size: 12px;
}

QPushButton#card_flat_btn_lg {
    font-size: 16px;
}

/* The 💾 and the ⋯ get a backdrop on hover, not just a colour change: they
   sit on a translucent panel over the cover art, where a bare colour shift is
   easy to miss. The 💾 gets the stronger one — it is the action people reach
   for. The ⟳ keeps its colour-only hover. */
QPushButton#card_flat_btn:hover {
    background: transparent;
    color: #76b900;
}

QPushButton#card_flat_btn_lg:hover {
    background: #2f2f3c;
    color: #88d000;
}

/* The border mirrors #primary_btn, whose objectName this button used to
   carry: it inherited the border from there, so leaving it out would have
   quietly changed the card in the light theme.
   The :pressed rule is NEW. The card's inline sheet used to override the
   background in every state, so this was the one primary button in the app
   that stayed flat when clicked; the value matches #primary_btn:pressed. */
QPushButton#card_play_btn {
    background: #76b900;
    color: #000000;
    font-size: 11px;
    font-weight: 700;
    border: none;
    border-radius: 4px;
    padding: 0 8px;
}

QPushButton#card_play_btn:hover {
    background: #88d000;
}

QPushButton#card_play_btn:pressed {
    background: #629900;
}

/* The ⋯ button keeps a solid backdrop on purpose: the glyph is small and
   the info panel below it is translucent over the cover art, so without
   one the dots disappear into whatever the artwork happens to be. It used
   to get this by accident — the panel's own stylesheet propagated its fill
   onto the button, painting the tint twice — and an #objectName rule does
   not propagate, so it is spelled out here instead. Hover deliberately
   changes nothing, matching what the accident produced. */
QPushButton#card_more_btn {
    background: #111114;
    border: none;
    border-radius: 4px;
    padding: 4px;
    color: #8a8a9a;
    font-size: 16px;
}

QPushButton#card_more_btn:hover {
    background: #1e1e28;
    color: #e8e8ea;
}

/* ── Overlay – dashboard key/value rows ───────────────────────────── */
/* Four rows, always the same shape. The value keeps an inline colour ONLY
   when a row is highlighted with an accent; the ordinary case is here. */
#dash_key {
    color: #4a4a5a;
    font-size: 11px;
    min-width: 90px;
}

#dash_value {
    color: #c8c8d0;
    font-size: 11px;
    font-weight: 600;
}

/* ── Settings – form buttons and list search boxes ────────────────── */
/* Used for save/export/import (primary) and cancel/history (secondary),
   plus the small search field above each list. The :disabled variants are
   spelled out because these buttons genuinely spend time disabled. */
QPushButton#form_primary_btn {
    background: #76b900;
    color: #000000;
    border: 1px solid #76b900;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 600;
}

QPushButton#form_primary_btn:hover {
    background: #88d000;
}

QPushButton#form_primary_btn:disabled {
    background: #1a1a22;
    color: #2a2a38;
    border-color: #1e1e24;
}

QPushButton#form_secondary_btn {
    background: #111114;
    color: #c8c8d0;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
}

QPushButton#form_secondary_btn:hover {
    border-color: #76b900;
    color: #76b900;
}

QPushButton#form_secondary_btn:disabled {
    color: #2a2a38;
    border-color: #1e1e24;
}

QLineEdit#list_search {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 0 6px;
    font-size: 11px;
    color: #e8e8ea;
}

/* ── Small repeated text roles ────────────────────────────────────── */
/* Column headers on the overview, stat-card captions, form hints and
   setup-step lines. Each of these appears several times and never varies,
   so naming the ROLE beats repeating the same f-string at each site. */
#section_header {
    color: #6b6b7a;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

#stat_label {
    color: #c8c8d0;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
    background: transparent;
}

#form_hint {
    color: #4a4a5a;
    font-size: 10px;
    background: transparent;
}

#setup_step {
    color: #c8c8d0;
    font-size: 11px;
    background: transparent;
}

/* ── Overview – active-game banner, panels, quick actions ─────────── */
/* All fixed per theme. #active_banner used to be built in Python purely to
   pick the gradient's start colour per theme — which is what having two
   theme blocks is for. */
#active_game_icon {
    font-size: 22px;
    background: transparent;
}

#active_game_name {
    color: #c8c8d0;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}

#active_game_sub {
    color: #c8c8d0;
    font-size: 11px;
    background: transparent;
}

/* Plain bordered panel — the activity list and the bar chart sit in one. */
#panel_card {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 8px;
}

/* Empty states carry instructions ("add a save folder from the library
   menu"), so they read as secondary text, not as a disabled control: the
   old #2a2a38 was the panel-border tone on a #111114 card. */
#empty_hint {
    color: #c8c8d0;
    font-size: 12px;
    padding: 16px;
}

#activity_scroll {
    background: transparent;
    border: none;
}

QPushButton#quick_action_btn {
    text-align: left;
    padding-left: 14px;
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    color: #c8c8d0;
    font-size: 12px;
}

QPushButton#quick_action_btn:hover {
    background: #1a1a22;
    border-color: #76b900;
    color: #76b900;
}

/* ── Overview – recent activity rows ──────────────────────────────── */
/* Five fixed looks repeated once per row. Nothing here varies with the
   row's content, so none of it belongs on the instances. */
QFrame#activity_row {
    background: transparent;
    border-bottom: 1px solid #1e1e24;
}

QFrame#activity_row:hover {
    background: #1a1a22;
}

#activity_icon {
    font-size: 18px;
    min-width: 24px;
    background: transparent;
}

#activity_title {
    color: #c8c8d0;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}

#activity_sub {
    color: #c8c8d0;
    font-size: 11px;
    background: transparent;
}

#activity_time {
    color: #6b6b7a;
    font-size: 10px;
    background: transparent;
}

/* ── Pager buttons (library and backups) ──────────────────────────── */
/* Two fixed looks, current page and the rest, rebuilt on every page change
   in two different pages. */
QPushButton#pager_btn {
    background: #111114;
    color: #c8c8d0;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    font-size: 11px;
    padding: 0 8px;
}

QPushButton#pager_btn:hover {
    border-color: #76b900;
    color: #76b900;
}

QPushButton#pager_btn_active {
    background: #76b900;
    color: #000000;
    border: none;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    padding: 0 8px;
}

/* ── Backups – collapsible per-title group header ─────────────────── */
/* One per title in the list, so a busy backups page holds a couple of dozen
   identical copies. */
QPushButton#backup_group_header {
    text-align: left;
    background: #111114;
    color: #e8e8ea;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 700;
}

QPushButton#backup_group_header:hover {
    border-color: #2a2a38;
    background: #1a1a22;
}

/* ── Library – tag filter chips ───────────────────────────────────── */
/* Here rather than inline: the panel builds one button per tag and a real
   library reaches ~200 of them. A widget carrying its own stylesheet costs
   roughly 3x more to re-polish on a theme switch, so those 200 alone were
   about a quarter of a second every time. The three filter states ride on
   the `tagState` property — the buttons are rebuilt on every state change,
   so nothing needs re-polishing by hand. */
QPushButton#tag_chip {
    background: transparent;
    color: #c8c8d0;
    border: 1px solid transparent;
    border-radius: 3px;
    font-size: 11px;
    padding: 1px 6px;
    text-align: left;
}

QPushButton#tag_chip:hover {
    border-color: #2a2a38;
    color: #e8e8ea;
}

QPushButton#tag_chip[tagState="1"] {
    background: #1e5c2e;
    color: #6fcf7f;
    border-color: #2e8b57;
}

QPushButton#tag_chip[tagState="1"]:hover {
    background: #2e7d45;
}

QPushButton#tag_chip[tagState="2"] {
    background: #5c1e1e;
    color: #cf6f6f;
    border-color: #8b2e2e;
}

QPushButton#tag_chip[tagState="2"]:hover {
    background: #7d2e2e;
}

/* The chips' two scroll bodies. These MUST be NAMED rather than carrying
   "background: transparent" as their own stylesheet: a widget's stylesheet
   outranks the application one across its whole subtree, so a transparent
   parent overrode the include/exclude fills above and selected chips came
   out unpainted. The chips set their own background either way, so nothing
   here needs to reach them. */
/* ── "Please wait" toast ──────────────────────────────────────────── */
/* Shown over a page while the GUI thread is blocked (theme swap, library
   build). The sheet behind it is painted by BusyOverlay itself, because a
   translucent fill has to composite over whatever it covers. */
/* ── Save editor page ─────────────────────────────────────────────────── */
#cheats_back {
    background: #1e1e28;
    border: 1px solid #2a2a38;
    color: #c8c8d0;
    border-radius: 6px;
    padding: 0px;
    font-size: 14px;
}
#cheats_back:hover { background: #33334a; border-color: #6c5ce7; color: #e8e8ea; }
#cheats_subtitle { color: #8a8a9a; font-size: 11px; }
#cheats_row {
    background: #1a1a24;
    border: 1px solid #24242e;
    border-radius: 6px;
}
#cheats_row:hover { background: #24243a; border-color: #6c5ce7; }
#cheats_row_title { color: #e8e8ea; font-size: 12px; }
#cheats_row_where { color: #8a8a9a; font-size: 10px; }
#cheats_row_detail { color: #8a8a9a; font-size: 11px; }
#cheats_row_btn {
    background: #33334a; border: 1px solid #3d3d55; color: #e8e8ea;
    border-radius: 4px; padding: 3px 10px; font-size: 11px;
}
#cheats_row_btn:hover { background: #6c5ce7; border-color: #6c5ce7; }
#cheats_field { background: #1a1a24; border-radius: 5px; }
#cheats_field:hover { background: #22222e; }
#cheats_field_name { color: #c8c8d0; font-size: 11px; }
#cheats_scroll { background: transparent; border: none; }
#cheats_hold_btn {
    background: #1e1e28; border: 1px solid #2a2a38; color: #8a8a9a;
    border-radius: 4px; padding: 0px; font-size: 11px;
}
#cheats_hold_btn:hover { border-color: #6c5ce7; color: #e8e8ea; }
#cheats_hold_btn:checked {
    background: #6c5ce7; border-color: #6c5ce7; color: #ffffff;
}
#cheats_pager {
    background: #1e1e28; border: 1px solid #2a2a38; color: #c8c8d0;
    border-radius: 4px; padding: 0px; font-size: 12px;
}
#cheats_pager:hover { background: #33334a; border-color: #6c5ce7; }
#cheats_pager:disabled { color: #4a4a5e; border-color: #24242e; }
#cheats_page_lbl { color: #8a8a9a; font-size: 11px; }
#cheats_holding { color: #6c5ce7; font-size: 11px; font-weight: 600; }


#busy_toast {
    background: #1e1e28;
    color: #e8e8ea;
    border: 1px solid #2a2a38;
    border-radius: 8px;
    padding: 14px 28px;
    font-size: 13px;
    font-weight: 600;
}

/* ── Pinned notes and images ──────────────────────────────────────── */
/* Frameless always-on-top windows that outlive the page that opened them.
   Named rules, never inline sheets: a pin left on screen across a
   light/dark switch has to follow the switch like everything else. */
#pin_item {
    background: #1e1e28;
    border: 1px solid #2a2a38;
    border-radius: 6px;
}
#pin_header {
    background: #252533;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}
#pin_title {
    color: #a8a8b8;
    font-size: 11px;
    font-weight: 600;
}
/* The base QPushButton rule sets padding 7px/16px. On these fixed 18x18
   buttons that left 4x4 for the glyph, so Qt elided the text away and they
   looked EMPTY until hover painted a background behind nothing. Padding is
   reset explicitly, and they carry a resting fill: a bare glyph on a pin
   header reads as decoration, not as something to press. */
#pin_icon_btn {
    color: #c8c8d0;
    background: #33334a;
    border: 1px solid #3d3d55;
    border-radius: 4px;
    padding: 0px;
    font-size: 12px;
}
#pin_icon_btn:hover {
    background: #6c5ce7;
    border-color: #6c5ce7;
    color: #ffffff;
}
#pin_icon_btn:pressed {
    background: #2a2a38;
}
#pin_text {
    background: transparent;
    color: #e8e8ea;
    border: none;
    font-size: 12px;
    padding: 4px 6px;
}
#pin_image {
    background: transparent;
    color: #a8a8b8;
}
/* Opacity slider — floats over the bottom of a pin, only while hovered. */
#pin_fade::groove:horizontal {
    background: #2a2a38;
    height: 3px;
    border-radius: 2px;
}
#pin_fade::sub-page:horizontal {
    background: #6c5ce7;
    height: 3px;
    border-radius: 2px;
}
#pin_fade::handle:horizontal {
    background: #e8e8ea;
    width: 9px;
    height: 9px;
    margin: -3px 0;
    border-radius: 4px;
}
/* One line of the 📌 menu. */
#pin_row {
    background: transparent;
}
/* The recent list scrolls past five entries; neither it nor its
   viewport may paint over the menu behind them. */
#pin_menu_scroll, #pin_menu_body {
    background: transparent;
    border: none;
}
#pin_row:hover {
    background: #2a2a38;
}
#pin_row_mark {
    color: #6c5ce7;
    font-size: 11px;
}
#pin_row_name {
    color: #e8e8ea;
    font-size: 12px;
}

/* Plain transparent wrapper. MUST be a name, never a widget stylesheet: a
   widget's own sheet outranks the application one for its whole subtree, so
   a wrapper that merely wanted to be see-through was erasing the background
   of every named widget inside it. */
#transparent_bg {
    background: transparent;
}

#tag_scroll_body {
    background: transparent;
}

/* ── Disabled ─────────────────────────────────────────────────────── */
/* Needed explicitly: the rules above set an unconditional `color`, and a
   stylesheet colour beats QPalette's Disabled group — so without these a
   greyed-out control looked exactly like a live one and simply ignored
   clicks. Kept deliberately SHORT and mirrored in LIGHT_THEME: every extra
   selector is re-resolved against every live widget on a theme switch, and
   this block measured +11% on that switch when it covered all the input
   types instead of just the ones the app actually disables. */
QLabel:disabled, QCheckBox:disabled {
    color: #55555f;
}

QCheckBox::indicator:disabled {
    border-color: #2a2a38;
    background: #16161c;
}

QSpinBox:disabled {
    color: #55555f;
    background-color: #16161c;
    border-color: #1e1e24;
}

QSpinBox::up-button:disabled, QSpinBox::down-button:disabled {
    background: #16161c;
}
"""

LIGHT_THEME = """
/* ── Base ─────────────────────────────────────────────────────────── */
QWidget {
    background-color: #ffffff;
    color: #1a1a2e;
    font-family: "Segoe UI", "SF Pro Display", sans-serif;
    font-size: 13px;
    border: 0px solid transparent;
    outline: none;
}

/* Labels & child frames inherit parent bg — prevents white rectangles on gradients/cards */
QLabel, QCheckBox, QRadioButton {
    background: transparent;
}

QMainWindow, QDialog {
    background-color: #ffffff;
}

/* ── Sidebar ───────────────────────────────────────────────────────── */
#sidebar {
    background-color: #ffffff;
    border-right: 0px solid transparent;
    min-width: 220px;
    max-width: 220px;
}

#sidebar_logo {
    color: #5a9400;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 24px 20px 8px 20px;
}

#sidebar_tagline {
    color: #9a9aaa;
    font-size: 10px;
    letter-spacing: 0.5px;
    padding: 0 20px 20px 20px;
}

/* ── Nav buttons ───────────────────────────────────────────────────── */
#nav_btn {
    background: transparent;
    color: #6a6a7a;
    font-size: 13px;
    font-weight: 500;
    text-align: left;
    padding: 10px 20px;
    border-radius: 0;
    border-left: 3px solid transparent;
}

#nav_btn:hover {
    background-color: #f0f0f5;
    color: #2a2a3a;
    border-left-color: transparent;
}

#nav_btn[active="true"] {
    background-color: #eef5e0;
    color: #1a1a2e;
    border-left-color: #5a9400;
}

/* ── Content area ─────────────────────────────────────────────────── */
#content_area {
    background-color: #ffffff;
}

#page_header {
    color: #1a1a2e;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
}

/* ── Cards ────────────────────────────────────────────────────────── */
#game_card {
    background-color: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    padding: 16px;
}

#game_card:hover {
    border: 1px solid #5a9400;
    padding: 16px;
    background-color: #fafafe;
}

#game_name {
    color: #1a1a2e;
    font-size: 14px;
    font-weight: 600;
}

#game_meta {
    color: #1a1a2e;
    font-size: 11px;
}

/* ── Status badges ────────────────────────────────────────────────── */
#status_synced    { color: #5a9400; }
#status_pending   { color: #c88a00; }
#status_conflict  { color: #d03030; }
#status_local     { color: #4080c0; }
#status_cloud     { color: #7868b8; }
#status_no_saves  { color: #9a9aaa; }

/* ── Buttons ──────────────────────────────────────────────────────── */
QPushButton {
    background-color: #ffffff;
    color: #2a2a3a;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #fafafe;
    color: #1a1a2e;
    border: 1px solid #5a9400;
    padding: 7px 16px;
}

QPushButton:pressed {
    background-color: #f0f0f5;
}

QPushButton#primary_btn {
    background-color: #5a9400;
    color: #fff;
    border: 1px solid #4a7a00;
    font-weight: 600;
}

QPushButton#primary_btn:hover {
    background-color: #6ab000;
    border-color: #5a9400;
}

QPushButton#primary_btn:pressed {
    background-color: #4a7a00;
}

QPushButton#danger_btn {
    background-color: transparent;
    color: #d03030;
    border: 1px solid #e0e0ea;
}

QPushButton#danger_btn:hover {
    background-color: #fef0f0;
    border: 1px solid #d03030;
    padding: 7px 16px;
}

QPushButton#icon_btn {
    background: transparent;
    border: 0px solid transparent;
    color: #6a6a7a;
    padding: 4px;
    font-size: 16px;
    border-radius: 4px;
}

QPushButton#icon_btn:hover {
    background-color: #e8e8f0;
    color: #1a1a2e;
}

/* Glyph-only toolbar button — see the note in DARK_THEME. */
QPushButton#toolbar_icon_btn {
    background-color: #ffffff;
    color: #2a2a3a;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 0;
    font-size: 15px;
}

QPushButton#toolbar_icon_btn:hover {
    background-color: #fafafe;
    color: #1a1a2e;
    border-color: #5a9400;
}

QPushButton#toolbar_icon_btn:pressed {
    background-color: #f0f0f5;
}

/* ── Inputs ───────────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 7px 11px;
    font-size: 13px;
    selection-background-color: #5a9400;
    selection-color: #fff;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #5a9400;
}

QLineEdit::placeholder {
    color: #b0b0c0;
}

/* ── ComboBox ─────────────────────────────────────────────────────── */
QComboBox {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 7px 32px 7px 12px;
    min-width: 120px;
}

QComboBox:hover { border-color: #5a9400; }
QComboBox:focus { border-color: #5a9400; }

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #1a1a2e;
    margin-right: 8px;
}

QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    selection-background-color: #5a9400;
    selection-color: #ffffff;
    border-radius: 6px;
}

/* Compact page-size control — mirror of DARK_THEME. */
QComboBox#page_size_combo {
    padding: 2px 18px 2px 6px;
    min-width: 0px;
    max-width: 52px;
    border-radius: 4px;
}
QComboBox#page_size_combo::drop-down {
    width: 16px;
}
QComboBox#page_size_combo::down-arrow {
    margin-right: 4px;
}

/* ── Sliders & Spinboxes ──────────────────────────────────────────── */
QSpinBox {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 6px 10px;
}

QSpinBox::up-button, QSpinBox::down-button {
    width: 18px;
    background: #f0f0f5;
    border: none;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: #d8d8e8;
}
QSpinBox::up-arrow {
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid #6a6a7a;
}
QSpinBox::down-arrow {
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid #6a6a7a;
}

/* ── CheckBox ─────────────────────────────────────────────────────── */
QCheckBox {
    color: #2a2a3a;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #d0d0e0;
    border-radius: 4px;
    background: #ffffff;
}

QCheckBox::indicator:checked {
    background-color: #5a9400;
    border-color: #5a9400;
}

/* ── ScrollBar ────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #c0c0d0;
    border-radius: 3px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover { background: #a8a8b8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QScrollBar:horizontal {
    background: transparent;
    height: 6px;
}

QScrollBar::handle:horizontal {
    background: #c0c0d0;
    border-radius: 3px;
    min-width: 30px;
}

/* ── Tab widget ───────────────────────────────────────────────────── */
QTabWidget::pane {
    border: 0px solid transparent;
    border-radius: 8px;
    background: #ffffff;
}

QTabBar::tab {
    background: transparent;
    color: #6a6a7a;
    padding: 8px 18px;
    font-size: 12px;
    border-bottom: 2px solid transparent;
}

QTabBar::tab:selected {
    color: #1a1a2e;
    border-bottom-color: #5a9400;
}

QTabBar::tab:hover { color: #2a2a3a; }

/* ── ToolTip ──────────────────────────────────────────────────────── */
QToolTip {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 0px solid transparent;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}

/* ── Separator ────────────────────────────────────────────────────── */
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    background: #e0e0ea;
    border: none;
    max-height: 1px;
    color: #e0e0ea;
}

/* ── Overlay ──────────────────────────────────────────────────────── */
#overlay {
    background-color: rgba(255, 255, 255, 230);
    border: 0px solid transparent;
    border-radius: 12px;
}

#overlay_title {
    color: #5a9400;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

#overlay_message {
    color: #2a2a3a;
    font-size: 12px;
}

/* ── Status bar ───────────────────────────────────────────────────── */
QStatusBar {
    background-color: #ffffff;
    color: #3a3a4a;
    font-size: 11px;
    border: 0px solid transparent;
}

/* ── System tray ──────────────────────────────────────────────────── */
QMenu {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 0px solid transparent;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 7px 20px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #eef5e0;
    color: #5a9400;
}

QMenu::separator { height: 1px; background: #e8e8f0; margin: 4px 8px; }

/* ── Progress bar ─────────────────────────────────────────────────── */
QProgressBar {
    background-color: #e8e8f0;
    border-radius: 4px;
    height: 4px;
    text-align: center;
    color: transparent;
}

QProgressBar::chunk {
    background-color: #5a9400;
    border-radius: 4px;
}

/* ── GroupBox (Settings, Sync) ────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QGroupBox {
    color: #2a2a3a;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    margin-top: 8px;
    padding: 16px;
}
QGroupBox::title {
    /* No negative "top" — see the note in DARK_THEME. */
    subcontrol-origin: margin;
    left: 12px;
    background: #ffffff;
    padding: 0 4px;
}

/* ── Overview – stat cards ────────────────────────────────────────── */
#stat_card {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    min-width: 110px;
}

/* ── Overlay – dashboard key/value rows ───────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#dash_key {
    color: #2a2a3a;
    font-size: 11px;
    min-width: 90px;
}

#dash_value {
    color: #1a1a2e;
    font-size: 11px;
    font-weight: 600;
}

/* ── Settings – form buttons and list search boxes ────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QPushButton#form_primary_btn {
    background: #5a9400;
    color: #ffffff;
    border: 1px solid #5a9400;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 600;
}

QPushButton#form_primary_btn:hover {
    background: #6ab000;
}

QPushButton#form_primary_btn:disabled {
    background: #f0f0f5;
    color: #b0b0c0;
    border-color: #e0e0ea;
}

QPushButton#form_secondary_btn {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
}

QPushButton#form_secondary_btn:hover {
    border-color: #5a9400;
    color: #5a9400;
}

QPushButton#form_secondary_btn:disabled {
    color: #b0b0c0;
    border-color: #e0e0ea;
}

QLineEdit#list_search {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 0 6px;
    font-size: 11px;
    color: #1a1a2e;
}

/* ── Small repeated text roles ────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#section_header {
    color: #2a2a3a;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

#stat_label {
    color: #1a1a2e;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
    background: transparent;
}

#form_hint {
    color: #2a2a3a;
    font-size: 10px;
    background: transparent;
}

#setup_step {
    color: #1a1a2e;
    font-size: 11px;
    background: transparent;
}

/* ── Overview – active game banner ───────────────────────────────── */
/* Gradient banner — see the note in DARK_THEME. */
#active_banner {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #f0f8e0, stop:1 #ffffff);
    border: 1px solid #d0e8b0;
    border-left: 3px solid #5a9400;
    border-radius: 8px;
}

/* ── Backups – backup row ────────────────────────────────────────── */
#backup_row {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
#backup_row:hover {
    border: 1px solid #5a9400;
    background: #fafafe;
}

/* ── Views & scroll areas (prevent Qt default gray) ──────────────── */
QAbstractItemView, QListWidget, QListView, QTreeView, QTreeWidget,
QTableView, QTableWidget, QHeaderView {
    background-color: #ffffff;
    color: #1a1a2e;
    border: none;
}

QAbstractScrollArea, QScrollArea {
    background-color: #ffffff;
    border: none;
}

QAbstractScrollArea > QWidget {
    background-color: #ffffff;
}

/* ── Dialog ───────────────────────────────────────────────────────── */
QDialog {
    background-color: #ffffff;
    border: 0px solid transparent;
}

/* ── Library – game card internals ────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#game_card_cover {
    background: #f0f0f5;
    font-size: 42px;
    border-radius: 10px;
}

#playing_badge {
    background: #5a9400;
    color: #ffffff;
    font-size: 9px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
}

#game_card_dots {
    background: rgba(0, 0, 0, 0.55);
    border-radius: 8px;
}

#game_card_bottom {
    background: rgba(255, 255, 255, 0.88);
    border-radius: 0 0 10px 10px;
}

/* The card FRAME without a folder colour — see the note in DARK_THEME. */
QFrame#game_card_grid {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 10px;
}

QFrame#game_card_grid:hover {
    border-color: #5a9400;
    background: #fafafe;
}

/* Card playtime — see the note in DARK_THEME. */
#playtime_lbl {
    color: #2a2a3a;
    font-size: 9px;
    background: transparent;
}

#playtime_lbl[hasHover="1"]:hover {
    color: #5a9400;
}

/* List view: the same pieces, one row instead of one card. */
#game_row_thumb {
    background: #f0f0f5;
    border-radius: 6px;
    font-size: 22px;
}

QPushButton#row_play_btn {
    background: #5a9400;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 0;
    font-size: 12px;
    font-weight: 700;
}

QPushButton#row_play_btn:hover {
    background: #6ab000;
}

QPushButton#row_play_btn:pressed {
    background: #5a9400;
}

#game_card_name {
    color: #1a1a2e;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}

QPushButton#card_flat_btn, QPushButton#card_flat_btn_lg {
    background: transparent;
    border: none;
    padding: 0;
    color: #2a2a3a;
    font-size: 12px;
}

QPushButton#card_flat_btn_lg {
    font-size: 16px;
}

/* Hover backdrops — see the note in DARK_THEME. */
QPushButton#card_flat_btn:hover {
    background: transparent;
    color: #5a9400;
}

QPushButton#card_flat_btn_lg:hover {
    background: #d6d6e4;
    color: #4a7a00;
}

/* Border inherited from #primary_btn, :pressed newly added — see the note
   in DARK_THEME. */
QPushButton#card_play_btn {
    background: #5a9400;
    color: #ffffff;
    font-size: 11px;
    font-weight: 700;
    border: 1px solid #4a7a00;
    border-radius: 4px;
    padding: 0 8px;
}

QPushButton#card_play_btn:hover {
    background: #6ab000;
    border-color: #5a9400;
}

QPushButton#card_play_btn:pressed {
    background: #4a7a00;
}

/* Solid backdrop for the ⋯ glyph — see the note in DARK_THEME. */
QPushButton#card_more_btn {
    background: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 4px;
    color: #6a6a7a;
    font-size: 16px;
}

QPushButton#card_more_btn:hover {
    background: #e8e8f0;
    color: #1a1a2e;
}

/* ── Overview – active-game banner, panels, quick actions ─────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#active_game_icon {
    font-size: 22px;
    background: transparent;
}

#active_game_name {
    color: #1a1a2e;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}

#active_game_sub {
    color: #1a1a2e;
    font-size: 11px;
    background: transparent;
}

#panel_card {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
}

/* Mirror of the dark rule: #b0b0c0 was the same disabled tone, and just as
   washed out against a white card. */
#empty_hint {
    color: #3a3a4a;
    font-size: 12px;
    padding: 16px;
}

#activity_scroll {
    background: transparent;
    border: none;
}

QPushButton#quick_action_btn {
    text-align: left;
    padding-left: 14px;
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    color: #1a1a2e;
    font-size: 12px;
}

QPushButton#quick_action_btn:hover {
    background: #f0f0f5;
    border-color: #5a9400;
    color: #5a9400;
}

/* ── Overview – recent activity rows ──────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QFrame#activity_row {
    background: transparent;
    border-bottom: 1px solid #e0e0ea;
}

QFrame#activity_row:hover {
    background: #f0f0f5;
}

#activity_icon {
    font-size: 18px;
    min-width: 24px;
    background: transparent;
}

#activity_title {
    color: #1a1a2e;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}

#activity_sub {
    color: #1a1a2e;
    font-size: 11px;
    background: transparent;
}

#activity_time {
    color: #2a2a3a;
    font-size: 10px;
    background: transparent;
}

/* ── Pager buttons (library and backups) ──────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QPushButton#pager_btn {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    font-size: 11px;
    padding: 0 8px;
}

QPushButton#pager_btn:hover {
    border-color: #5a9400;
    color: #5a9400;
}

QPushButton#pager_btn_active {
    background: #5a9400;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    padding: 0 8px;
}

/* ── Backups – collapsible per-title group header ─────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QPushButton#backup_group_header {
    text-align: left;
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 700;
}

QPushButton#backup_group_header:hover {
    border-color: #5a9400;
    background: #f0f0f5;
}

/* ── Library – tag filter chips ───────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. The include/exclude
   colours are deliberately identical in both themes: they were hardcoded
   when the style was inline, and green/red read the same either way. */
QPushButton#tag_chip {
    background: transparent;
    color: #1a1a2e;
    border: 1px solid transparent;
    border-radius: 3px;
    font-size: 11px;
    padding: 1px 6px;
    text-align: left;
}

QPushButton#tag_chip:hover {
    border-color: #5a9400;
    color: #1a1a2e;
}

QPushButton#tag_chip[tagState="1"] {
    background: #1e5c2e;
    color: #6fcf7f;
    border-color: #2e8b57;
}

QPushButton#tag_chip[tagState="1"]:hover {
    background: #2e7d45;
}

QPushButton#tag_chip[tagState="2"] {
    background: #5c1e1e;
    color: #cf6f6f;
    border-color: #8b2e2e;
}

QPushButton#tag_chip[tagState="2"]:hover {
    background: #7d2e2e;
}

/* The chips' two scroll bodies. These MUST be NAMED rather than carrying
   "background: transparent" as their own stylesheet: a widget's stylesheet
   outranks the application one across its whole subtree, so a transparent
   parent overrode the include/exclude fills above and selected chips came
   out unpainted. The chips set their own background either way, so nothing
   here needs to reach them. */
/* ── "Please wait" toast ──────────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
/* ── Save editor page ─────────────────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#cheats_back {
    background: #f2f2f7;
    border: 1px solid #e0e0ea;
    color: #3a3a4e;
    border-radius: 6px;
    padding: 0px;
    font-size: 14px;
}
#cheats_back:hover { background: #e4e4ee; border-color: #6c5ce7; color: #1a1a2e; }
#cheats_subtitle { color: #6a6a7e; font-size: 11px; }
#cheats_row {
    background: #ffffff;
    border: 1px solid #e8e8f0;
    border-radius: 6px;
}
#cheats_row:hover { background: #f4f2ff; border-color: #6c5ce7; }
#cheats_row_title { color: #1a1a2e; font-size: 12px; }
#cheats_row_where { color: #6a6a7e; font-size: 10px; }
#cheats_row_detail { color: #6a6a7e; font-size: 11px; }
#cheats_row_btn {
    background: #e4e4ee; border: 1px solid #d2d2e0; color: #1a1a2e;
    border-radius: 4px; padding: 3px 10px; font-size: 11px;
}
#cheats_row_btn:hover { background: #6c5ce7; border-color: #6c5ce7; color: #ffffff; }
#cheats_field { background: #ffffff; border-radius: 5px; }
#cheats_field:hover { background: #f4f2ff; }
#cheats_field_name { color: #3a3a4e; font-size: 11px; }
#cheats_scroll { background: transparent; border: none; }
#cheats_hold_btn {
    background: #f2f2f7; border: 1px solid #e0e0ea; color: #5a5a6e;
    border-radius: 4px; padding: 0px; font-size: 11px;
}
#cheats_hold_btn:hover { border-color: #6c5ce7; color: #1a1a2e; }
#cheats_hold_btn:checked {
    background: #6c5ce7; border-color: #6c5ce7; color: #ffffff;
}
#cheats_pager {
    background: #f2f2f7; border: 1px solid #e0e0ea; color: #3a3a4e;
    border-radius: 4px; padding: 0px; font-size: 12px;
}
#cheats_pager:hover { background: #e4e4ee; border-color: #6c5ce7; }
#cheats_pager:disabled { color: #b8b8c8; border-color: #ececf4; }
#cheats_page_lbl { color: #6a6a7e; font-size: 11px; }
#cheats_holding { color: #6c5ce7; font-size: 11px; font-weight: 600; }


#busy_toast {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    padding: 14px 28px;
    font-size: 13px;
    font-weight: 600;
}

/* ── Pinned notes and images ──────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
#pin_item {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
#pin_header {
    background: #f2f2f7;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}
#pin_title {
    color: #5a5a6e;
    font-size: 11px;
    font-weight: 600;
}
/* Mirror of the DARK_THEME block — see the note there. */
#pin_icon_btn {
    color: #3a3a4e;
    background: #e4e4ee;
    border: 1px solid #d2d2e0;
    border-radius: 4px;
    padding: 0px;
    font-size: 12px;
}
#pin_icon_btn:hover {
    background: #6c5ce7;
    border-color: #6c5ce7;
    color: #ffffff;
}
#pin_icon_btn:pressed {
    background: #d2d2e0;
}
#pin_text {
    background: transparent;
    color: #1a1a2e;
    border: none;
    font-size: 12px;
    padding: 4px 6px;
}
#pin_image {
    background: transparent;
    color: #5a5a6e;
}
/* Opacity slider — floats over the bottom of a pin, only while hovered. */
#pin_fade::groove:horizontal {
    background: #e0e0ea;
    height: 3px;
    border-radius: 2px;
}
#pin_fade::sub-page:horizontal {
    background: #6c5ce7;
    height: 3px;
    border-radius: 2px;
}
#pin_fade::handle:horizontal {
    background: #4a4a5e;
    width: 9px;
    height: 9px;
    margin: -3px 0;
    border-radius: 4px;
}
/* One line of the 📌 menu. */
#pin_row {
    background: transparent;
}
/* The recent list scrolls past five entries; neither it nor its
   viewport may paint over the menu behind them. */
#pin_menu_scroll, #pin_menu_body {
    background: transparent;
    border: none;
}
#pin_row:hover {
    background: #ececf4;
}
#pin_row_mark {
    color: #6c5ce7;
    font-size: 11px;
}
#pin_row_name {
    color: #1a1a2e;
    font-size: 12px;
}

/* Plain transparent wrapper. MUST be a name, never a widget stylesheet: a
   widget's own sheet outranks the application one for its whole subtree, so
   a wrapper that merely wanted to be see-through was erasing the background
   of every named widget inside it. */
#transparent_bg {
    background: transparent;
}

#tag_scroll_body {
    background: transparent;
}

/* ── Disabled ─────────────────────────────────────────────────────── */
/* Mirror of the DARK_THEME block — see the note there. */
QLabel:disabled, QCheckBox:disabled {
    color: #a8a8b4;
}

QCheckBox::indicator:disabled {
    border-color: #e0e0ea;
    background: #f4f4f8;
}

QSpinBox:disabled {
    color: #a8a8b4;
    background-color: #f4f4f8;
    border-color: #e8e8f0;
}

QSpinBox::up-button:disabled, QSpinBox::down-button:disabled {
    background: #f4f4f8;
}
"""


# ── Color palette for inline styles ─────────────────────────────────────────
# Widgets that use setStyleSheet() at runtime should call palette("key")
# instead of hardcoding hex colors, so they adapt to the active theme.

_PALETTE_DARK = {
    # Text
    "text":            "#e8e8ea",   # primary text
    "text_secondary":  "#c8c8d0",   # secondary / subtitle
    "text_muted":      "#6b6b7a",   # muted labels
    "text_hint":       "#4a4a5a",   # hints, metadata
    "text_faint":      "#3a3a48",   # barely visible (timestamps, IDs)
    "text_disabled":   "#2a2a38",   # disabled / empty-state

    # Backgrounds
    "bg":              "#111114",   # main background
    "bg_card":         "#111114",   # card / panel background
    "bg_hover":        "#161619",   # card hover
    "bg_input":        "#111114",   # input fields
    "bg_elevated":     "#1a1a22",   # elevated panels
    "bg_button":       "#1e1e28",   # button background

    # Borders
    "border":          "#1e1e24",   # default border
    "border_hover":    "#2a2a38",   # hover border
    "border_focus":    "#76b900",   # focus border (accent)
    "border_subtle":   "#1a1a22",   # subtle separator

    # UI Elements
    "tag_arrow":       "#3a3a48",   # tag scroll arrows

    # Accent
    "accent":          "#76b900",   # primary accent (green)
    "accent_hover":    "#88d000",   # accent hover
    "accent_text":     "#000000",   # text on accent bg

    # Semantic
    "success":         "#76b900",
    "warning":         "#f5a623",
    "provisional":     "#e0703a",
    "error":           "#e84d4d",
    "info":            "#7ab8f5",
    "cloud":           "#9b8bd8",

    # Overlay
    "overlay_bg":      "rgba(13, 13, 15, 220)",
    "separator":       "#1e1e24",

    # Folder colors
    "folder_red":      "#e84d4d",
    "folder_orange":   "#f5a623",
    "folder_yellow":   "#f5d623",
    "folder_green":    "#76b900",
    "folder_blue":     "#7ab8f5",
    "folder_purple":   "#9b8bd8",
    "folder_pink":     "#e88bd8",
    "folder_gray":     "#6b6b7a",
}

_PALETTE_LIGHT = {
    # Text — on light theme all content text stays dark on white cards
    "text":            "#1a1a2e",
    "text_secondary":  "#1a1a2e",
    "text_muted":      "#2a2a3a",
    "text_hint":       "#2a2a3a",
    "text_faint":      "#3a3a4a",
    "text_disabled":   "#b0b0c0",

    # Backgrounds
    "bg":              "#ffffff",
    "bg_card":         "#ffffff",
    "bg_hover":        "#fafafe",
    "bg_input":        "#ffffff",
    "bg_elevated":     "#f0f0f5",
    "bg_button":       "#ffffff",

    # Borders — subtle visible borders to separate boxes on white background
    "border":          "#e0e0ea",
    "border_hover":    "#5a9400",
    "border_focus":    "#5a9400",
    "border_subtle":   "#ebebf0",

    # UI Elements
    "tag_arrow":       "#b0b0c0",   # tag scroll arrows

    # Accent
    "accent":          "#5a9400",
    "accent_hover":    "#6ab000",
    "accent_text":     "#ffffff",

    # Semantic
    "success":         "#5a9400",
    "warning":         "#c88a00",
    "provisional":     "#c05a1a",
    "error":           "#d03030",
    "info":            "#4080c0",
    "cloud":           "#7868b8",

    # Overlay
    "overlay_bg":      "rgba(255, 255, 255, 230)",
    "separator":       "#e8e8f0",

    # Folder colors
    "folder_red":      "#d03030",
    "folder_orange":   "#c88a00",
    "folder_yellow":   "#c8a800",
    "folder_green":    "#5a9400",
    "folder_blue":     "#4080c0",
    "folder_purple":   "#7868b8",
    "folder_pink":     "#c868b8",
    "folder_gray":     "#4a4a5a",
}


def palette(key: str) -> str:
    """Return the color hex for *key* based on the active theme.

    Usage in widgets::

        from ui.styles.theme import palette
        label.setStyleSheet(f"color: {palette('text_muted')}; font-size: 11px;")

    This replaces hardcoded dark-only colors with theme-aware values.
    """
    if not isinstance(key, str) or not key:
        logger.warning(f"Invalid palette key: {key!r}, returning generic fallback")
        return "#888888"
    mgr = get_theme_manager()
    pal = _PALETTE_DARK if mgr.is_dark() else _PALETTE_LIGHT
    value = pal.get(key)
    if value is not None:
        return value
    logger.warning(f"Unknown palette key: {key!r}, returning generic fallback")
    return "#888888"


class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self._current = "dark"

    def apply(self, theme: str, app: QApplication):
        # Swapping the application stylesheet makes Qt re-resolve style rules
        # for EVERY live widget, and that cost is linear in how many there
        # are (measured at roughly 0.4 ms each) — on a large library it is a
        # visible pause with the event loop blocked throughout. Nothing about
        # it can be deferred or batched from here (hiding the windows or
        # disabling updates around it measured slower, not faster), so at
        # least say the app is busy rather than looking hung.
        # A "please wait" sheet over the window, not just a wait cursor: the
        # pause is long enough to look like a hang, and a cursor shape is easy
        # to miss. Falls back to plain execution when there is no visible
        # window to cover (startup, headless).
        target = None
        for w in app.topLevelWidgets():
            if w.isVisible() and w.isWindow() and w.width() > 200:
                target = w
                break
        if target is None:
            self._apply_inner(theme, app)
            return
        from ui.widgets.busy_overlay import busy_over
        with busy_over(target):
            self._apply_inner(theme, app)

    def _apply_inner(self, theme: str, app: QApplication):
        self._current = theme
        qss = DARK_THEME if theme == "dark" else LIGHT_THEME

        # Override Qt's built-in QPalette so Fusion doesn't paint
        # its default gray on view viewports and scroll areas.
        pal = app.palette()
        if theme == "dark":
            bg = QColor("#111114")
            fg = QColor("#e8e8ea")
            base = QColor("#111114")
            alt = QColor("#161619")
        else:
            bg = QColor("#ffffff")
            fg = QColor("#1a1a2e")
            base = QColor("#ffffff")
            alt = QColor("#fafafe")
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Base, base)
        pal.setColor(QPalette.ColorRole.AlternateBase, alt)
        pal.setColor(QPalette.ColorRole.Text, fg)
        pal.setColor(QPalette.ColorRole.Button, bg)
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        app.setPalette(pal)

        app.setStyleSheet(qss)
        self.theme_changed.emit(theme)

    @property
    def current(self) -> str:
        return self._current

    def is_dark(self) -> bool:
        return self._current == "dark"


class ThemedMixin:
    """Mixin for widgets whose inline, palette-dependent styles must survive a
    light/dark switch WITHOUT rebuilding the widget tree (the rebuild is what
    made theme changes freeze on large libraries).

    Route every palette-dependent ``setStyleSheet`` through
    ``self._sty(widget, lambda: f"...{palette('key')}...")``: the style is
    applied immediately AND remembered, so ``refresh_styles()`` re-applies it
    with the now-current palette. Because applying and registering are the SAME
    call, a converted site can never be silently dropped from a separate list.

    Note on loops: the ``lambda`` is evaluated at refresh time, so any
    loop-local variable it references (e.g. a per-item colour) MUST be captured
    with a default argument — ``lambda c=c: f"...{c}..."`` — or every registered
    entry would see the final loop value.

    Widgets that create their own themed children (e.g. per-game cards) override
    ``refresh_styles`` to also cascade into the current children — see
    LibraryPage.refresh_styles.
    """

    def _sty(self, widget, style_fn):
        try:
            reg = self._themed_styles
        except AttributeError:
            reg = self._themed_styles = {}
        widget.setStyleSheet(style_fn())
        # Keyed by widget, not appended: a page that re-registers the same
        # widget (a row restyled on selection, a card re-themed on hover)
        # would otherwise accumulate one entry per call, and refresh_styles
        # would replay them all on every theme switch.
        reg[widget] = style_fn
        return widget

    def refresh_styles(self):
        reg = getattr(self, "_themed_styles", None)
        if not reg:
            return
        dead = []
        for widget, style_fn in list(reg.items()):
            try:
                widget.setStyleSheet(style_fn())
            except RuntimeError:
                # Underlying C++ widget already deleted. DROP it — leaving it
                # behind grew the registry for the whole session (every page
                # rebuild added its replaced widgets), so each theme switch
                # replayed an ever-longer list of entries that only raise.
                dead.append(widget)
        for widget in dead:
            reg.pop(widget, None)

    def prune_themed_styles(self):
        """Forget entries whose widget is already gone.

        refresh_styles() prunes as it goes, but a page that rebuilds a whole
        block of rows knows right then that the old ones are dead — calling
        this keeps the registry the size of what is actually on screen
        instead of leaving it to the next theme switch to discover.
        """
        reg = getattr(self, "_themed_styles", None)
        if not reg:
            return
        import shiboken6 as sip
        for widget in [w for w in reg if not sip.isValid(w)]:
            reg.pop(widget, None)


_theme_mgr: ThemeManager | None = None
_theme_lock = threading.Lock()


def get_theme_manager() -> ThemeManager:
    """Return the singleton ThemeManager.

    Must be called from the main thread (ThemeManager is a QObject and
    creating QObjects on background threads causes signal delivery issues).
    """
    global _theme_mgr
    if _theme_mgr is None:
        with _theme_lock:
            if _theme_mgr is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("ThemeManager requires QApplication")
                _theme_mgr = ThemeManager()
    return _theme_mgr
