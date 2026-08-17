"""
SaveSync — dark theme (QSS + palette).

Imported by ``ui.styles.theme``. New themes: same exports
(``THEME``, ``PALETTE``, ``ID``, ``IS_DARK``) and register in
``theme.THEMES``.
"""

ID = "dark"
IS_DARK = True

THEME = """

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
/* Width is set in code via scaled(200, min_px=190) so DPI wins over QSS. */
#sidebar {
    background-color: #111114;
    border-right: 1px solid #1e1e24;
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

#credits_logo {
    background: transparent;
    border: none;
}
#credits_github_btn {
    color: #e8e8ea;
    background: #1a1a22;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
}
#credits_github_btn:hover {
    background: #76b900;
    border-color: #76b900;
    color: #000000;
}
#credits_heading {
    color: #e8e8ea;
    background: transparent;
}
#credits_muted {
    color: #b0b0b8;
    font-size: 12px;
    background: transparent;
}
#credits_coin {
    color: #b0b0b8;
    font-size: 11px;
    background: transparent;
}
#credits_sep {
    background: #1e1e24;
    border: none;
    max-height: 1px;
}
QLineEdit#credits_wallet_field {
    color: #b0b0b8;
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 10px;
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
#nav_btn[notice="true"] {
    padding: 10px 28px 10px 20px;
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
    /* Placeholder filled at apply-time with a real SVG (CSS triangles
       often fail to paint on Windows Fusion). */
    image: __ICON_DOWN__;
    width: 10px;
    height: 10px;
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
    max-width: 58px;
    border-radius: 4px;
}
QComboBox#page_size_combo::drop-down {
    width: 16px;
}
QComboBox#page_size_combo::down-arrow {
    margin-right: 4px;
}

/* ── Sliders & Spinboxes ──────────────────────────────────────────── */
/* QDoubleSpinBox too: float fields (e.g. Ren'Py BINFLOAT) must match ints. */
QSpinBox, QDoubleSpinBox {
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
    width: 12px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #2a2a3a;
    border-radius: 3px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover { background: #3a3a50; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar::sub-line:vertical {
    height: 12px;
    background: transparent;
    subcontrol-position: top;
    subcontrol-origin: margin;
}
QScrollBar::add-line:vertical {
    height: 12px;
    background: transparent;
    subcontrol-position: bottom;
    subcontrol-origin: margin;
}
QScrollBar::up-arrow:vertical {
    width: 8px; height: 8px;
    image: __ICON_UP__;
}
QScrollBar::down-arrow:vertical {
    width: 8px; height: 8px;
    image: __ICON_DOWN__;
}

QScrollBar:horizontal {
    background: transparent;
    height: 12px;
}

QScrollBar::handle:horizontal {
    background: #2a2a3a;
    border-radius: 3px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover { background: #3a3a50; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QScrollBar::sub-line:horizontal {
    width: 12px;
    background: transparent;
    subcontrol-position: left;
    subcontrol-origin: margin;
}
QScrollBar::add-line:horizontal {
    width: 12px;
    background: transparent;
    subcontrol-position: right;
    subcontrol-origin: margin;
}
QScrollBar::left-arrow:horizontal {
    width: 8px; height: 8px;
    image: __ICON_LEFT__;
}
QScrollBar::right-arrow:horizontal {
    width: 8px; height: 8px;
    image: __ICON_RIGHT__;
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
#backup_row_date {
    color: #b0b0b8;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
#backup_row_meta {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#backup_row_meta_sm {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#backup_row_note {
    color: #6a6a78;
    font-size: 10px;
    font-style: italic;
    background: transparent;
}
#backup_row_playing {
    color: #e6a817;
    font-size: 10px;
    font-weight: 600;
    background: transparent;
}

/* ── Sidebar batch progress ──────────────────────────────────────── */
#batch_progress_notice {
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 6px;
}
#batch_progress_notice QLabel#batch_progress_label {
    color: #b0b0b8;
    font-size: 11px;
    background: transparent;
    border: none;
}
#batch_progress_notice QLabel#batch_progress_label_done {
    color: #e8e8ea;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
    border: none;
}
#batch_progress_notice QProgressBar {
    background: #111114;
    border: none;
    border-radius: 2px;
}
#batch_progress_notice QProgressBar::chunk {
    background: #76b900;
    border-radius: 2px;
}

/* ── Spinbox up/down arrows ──────────────────────────────────────── */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 18px;
    background: #1e1e28;
    border: none;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #2a2a38;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    width: 8px; height: 8px;
    image: __ICON_UP__;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    width: 8px; height: 8px;
    image: __ICON_DOWN__;
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

/* Card playtime: total normally, last session while hovered. Transparent —
   overlap with stars is masked in _PlayRatingStrip (no backdrop). Hover
   colour matches the sync-status accent. */
#playtime_lbl {
    color: #6b6b7a;
    font-size: 9px;
    background: transparent;
    padding: 0px;
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

QPushButton#form_muted_btn {
    color: #6a6a78;
    border: 1px solid #1a1a22;
    background: transparent;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 11px;
}
QPushButton#form_muted_btn:hover {
    border-color: #6a6a78;
    color: #b0b0b8;
}

/* Shared dialog chrome */
#dialog_title {
    color: #e8e8ea;
    font-size: 16px;
    font-weight: 600;
    background: transparent;
}
#dialog_desc {
    color: #b0b0b8;
    font-size: 11px;
    background: transparent;
}
#dialog_status {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#dialog_empty {
    color: #6a6a78;
    font-size: 12px;
    padding: 24px;
    background: transparent;
}
#update_title {
    color: #e8e8ea;
    font-size: 15px;
    font-weight: 700;
    background: transparent;
    border: none;
}
#update_summary {
    color: #b0b0b8;
    font-size: 12px;
    font-weight: 500;
    background: transparent;
    border: none;
}
#update_note {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
    border: none;
}
QTextEdit#update_notes {
    background: #111114;
    color: #e8e8ea;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    padding: 8px;
    font-size: 12px;
    font-weight: 400;
}
QPushButton#update_later_btn {
    color: #e8e8ea;
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#update_later_btn:hover {
    border-color: #2a2a38;
}
#review_form {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 6px;
}
#review_card {
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 6px;
}
#review_who {
    color: #b0b0b8;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
#review_when {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#review_form_title {
    color: #b0b0b8;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
QListWidget#game_search_list {
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    font-size: 11px;
}
#dialog_heading {
    color: #e8e8ea;
    font-size: 18px;
    font-weight: 700;
    background: transparent;
}
#review_body {
    color: #e8e8ea;
    font-size: 12px;
    background: transparent;
}
#sidebar_machine {
    color: #5a5a68;
    font-size: 10px;
    padding: 0 16px 12px;
    background: transparent;
}
#cloud_verify_row {
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 8px;
    padding: 10px;
}
#auto_scan_game_header {
    color: #76b900;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
}
#auto_scan_muted {
    color: #5a5a68;
    font-size: 10px;
    background: transparent;
}
#enrich_name {
    color: #e8e8ea;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}
#enrich_source {
    color: #6a6a78;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#enrich_desc {
    color: #b0b0b8;
    font-size: 12px;
    background: transparent;
}
#enrich_meta {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#enrich_hint {
    color: #5a5a68;
    font-size: 11px;
    background: transparent;
}
#enrich_btn_hint {
    color: #5a5a68;
    font-size: 10px;
    background: transparent;
}
#enrich_thumb {
    background: #111114;
    border: 1px solid #2a2a38;
    border-radius: 6px;
    font-size: 26px;
}
QPushButton#enrich_inspect_btn {
    color: #76b900;
    font-size: 11px;
    text-align: left;
    padding: 0;
    background: transparent;
    border: none;
}
QPushButton#enrich_inspect_btn:hover {
    text-decoration: underline;
}
QPushButton#enrich_inspect_btn:disabled {
    color: #4a4a5a;
}
#enrich_intro {
    color: #76b900;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
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

#quick_card {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 8px;
}
#quick_card:hover {
    border-color: #76b900;
}
#quick_card_name {
    color: #e8e8ea;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}
#sync_muted {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#sync_status {
    color: #6a6a78;
    font-size: 12px;
    background: transparent;
}
#sync_section_header {
    color: #6a6a78;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    background: transparent;
}
#sync_setup_guide {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 6px;
    padding: 10px 12px;
}
QPushButton#sync_portal_btn {
    background: #76b900;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#sync_portal_btn:hover {
    background: #8ad414;
}
#sync_provider_hint {
    color: #76b900;
    font-size: 11px;
    padding: 6px 8px;
    background: #111114;
    border-radius: 4px;
    border: 1px solid #1e1e24;
}
#backup_summary {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#backup_empty {
    color: #4a4a5a;
    font-size: 14px;
    padding: 32px;
    background: transparent;
}
QFrame#path_row {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 6px;
}
QFrame#path_row:hover {
    border-color: #2a2a38;
}
#path_row_path {
    color: #b0b0b8;
    font-size: 11px;
    background: transparent;
}
#path_row_info {
    color: #5a5a68;
    font-size: 10px;
    min-width: 110px;
    background: transparent;
}
#settings_section_lbl {
    color: #6a6a78;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#form_field_lbl {
    color: #6a6a78;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#form_section_lbl {
    color: #6a6a78;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 0;
    background: transparent;
}
#form_section_lbl_faint {
    color: #4a4a5a;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 0;
    background: transparent;
}
#form_empty_lbl {
    color: #4a4a5a;
    font-size: 11px;
    padding: 8px;
    background: transparent;
}
#form_muted_sm {
    color: #6a6a78;
    font-size: 11px;
    background: transparent;
}
#form_secondary_lbl {
    color: #b0b0b8;
    font-size: 12px;
    background: transparent;
}
#img_counter {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#img_preview_frame {
    background: #111114;
    border: 1px solid #2a2a38;
    border-radius: 6px;
}
QFrame#url_strip, QFrame#tag_strip {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 4px;
}
QScrollArea#strip_scroll {
    background: transparent;
    border: none;
}
QFrame#strip_host {
    background: transparent;
    border: none;
}
QPushButton#overlay_carousel_arrow {
    background: #111114;
    color: #e8e8ea;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    font-size: 15px;
    font-weight: bold;
    padding: 0;
}
QPushButton#overlay_carousel_arrow:hover {
    background: #76b900;
    border-color: #76b900;
    color: #000000;
}
QPushButton#overlay_carousel_arrow:disabled {
    color: #6a6a78;
    border-color: #1e1e24;
    background: #111114;
}
QPushButton#enrich_merge_chip {
    background: #111114;
    color: #b0b0b8;
    border: 1px solid #1e1e24;
    border-radius: 10px;
    padding: 0 8px;
    font-size: 11px;
}
QPushButton#enrich_merge_chip:hover {
    border-color: #76b900;
    color: #e8e8ea;
}
QPushButton#enrich_merge_chip:checked {
    background: #76b900;
    color: #111114;
    border-color: #76b900;
}
QPushButton#img_tool_btn {
    background: #161619;
    color: #b0b0b8;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    font-size: 14px;
    padding: 0;
}
QPushButton#img_tool_btn:hover {
    border-color: #76b900;
    color: #e8e8ea;
}
QPushButton#img_trash_btn {
    background: rgba(0, 0, 0, 0.45);
    color: rgba(255, 255, 255, 0.6);
    border: none;
    border-radius: 4px;
    font-size: 11px;
    padding: 0;
}
QPushButton#img_trash_btn:hover {
    background: #c0392b;
    color: #ffffff;
}
QPushButton#add_tag_chip {
    background: #111114;
    color: #b0b0b8;
    border: 1px solid #1e1e24;
    border-radius: 10px;
    padding: 0 8px;
    font-size: 11px;
}
QPushButton#add_tag_chip:hover {
    background: #c0392b;
    color: #ffffff;
    border-color: #c0392b;
}
QFrame#url_chip {
    background: #1a1a22;
    border: 1px solid #2a2a38;
    border-radius: 10px;
}
QPushButton#url_chip_link {
    background: transparent;
    color: #76b900;
    border: none;
    font-size: 11px;
    text-align: left;
    padding: 0 4px;
}
QPushButton#url_chip_link:hover {
    text-decoration: underline;
}
QPushButton#url_chip_remove {
    background: transparent;
    color: #6a6a78;
    border: none;
    font-size: 11px;
    padding: 0;
}
QPushButton#url_chip_remove:hover {
    color: #c0392b;
}
QPushButton#overlay_info_btn {
    color: #6a6a78;
    background: transparent;
    border: 1px solid #2a2a38;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    font-style: italic;
    padding: 0;
}
QPushButton#overlay_info_btn:hover {
    color: #76b900;
    border-color: #76b900;
}
QPushButton#overlay_suppress_btn {
    font-size: 10px;
    color: #6a6a78;
    padding: 4px 8px;
    border: 1px solid #2a2a38;
    border-radius: 4px;
    background: transparent;
}
QPushButton#overlay_suppress_btn:hover {
    color: #e8e8ea;
    border-color: #76b900;
    background: #111114;
}
#overlay_carousel_counter {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#overlay_icon {
    font-size: 15px;
    min-width: 20px;
    background: transparent;
}
#overlay_separator {
    background: #2a2a38;
    border: none;
    max-height: 1px;
}
QPushButton#credits_nav_btn {
    color: #b0b0b8;
    background: transparent;
    border: none;
    font-size: 12px;
    font-weight: 600;
    padding: 9px 16px;
    text-align: left;
}
QPushButton#credits_nav_btn:hover {
    color: #e8e8ea;
    background: #111114;
}
#credits_toast {
    color: #e8e8ea;
    background: #1e1e28;
    border: 1px solid #76b900;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 11px;
    font-weight: 600;
}
QFrame#settings_panel {
    background: #0c0c0e;
    border: 1px solid #1e1e24;
    border-radius: 10px;
}
QWidget#settings_ui_scale_row {
    background: #161619;
    border: 1px solid #1e1e24;
    border-radius: 8px;
}
QPushButton#settings_reset_btn {
    color: #c8a000;
    border: 1px solid #c8a000;
    background: transparent;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
}
QPushButton#settings_reset_btn:hover {
    background: #c8a000;
    color: #111114;
}
QListWidget#settings_pref_list {
    background: #161619;
    border: 1px solid #76b900;
    border-radius: 6px;
}
QListWidget#settings_pref_list::item {
    padding: 4px 8px;
    color: #b0b0b8;
    font-size: 11px;
}
QListWidget#settings_pref_list::item:selected {
    background: #111114;
    color: #c0392b;
}
#deleted_tag {
    color: #5a5a68;
    font-size: 10px;
    border: 1px solid #1e1e24;
    border-radius: 3px;
    padding: 1px 5px;
    background: transparent;
}
QCheckBox#list_cb_sm {
    font-size: 11px;
    spacing: 4px;
}
QPushButton#auto_scan_sm_btn {
    font-size: 10px;
    padding: 2px 8px;
}
QPushButton#auto_scan_icon_btn {
    font-size: 10px;
    padding: 0px;
}
#emoji_icon {
    font-size: 20px;
    background: transparent;
}
#folder_row_icon {
    font-size: 12px;
    background: transparent;
}
#dialog_intro {
    color: #b0b0b8;
    font-size: 12px;
    background: transparent;
}
#path_entry_meta {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#review_meta_sm {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#review_notes {
    color: #6a6a78;
    font-size: 11px;
    font-style: italic;
    background: transparent;
}
#review_score {
    color: #b0b0b8;
    font-size: 12px;
    font-weight: 600;
    min-width: 34px;
    background: transparent;
}
#review_form_hint {
    color: #c8a000;
    font-size: 11px;
    background: transparent;
}
QLineEdit#review_reviewer {
    background: #111114;
    color: #e8e8ea;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 12px;
}
QCheckBox#path_row_cb {
    spacing: 4px;
}
#add_dialog_title {
    color: #f0f0f2;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}

#settings_section_hint {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
QFrame#backup_suggest_popup {
    background: #111114;
    border: 1px solid #2a2a38;
    border-radius: 6px;
}
QFrame#backup_suggest_popup QListWidget {
    background: transparent;
    border: none;
    font-size: 12px;
    color: #b0b0b8;
}
QFrame#backup_suggest_popup QListWidget::item {
    padding: 5px 8px;
    border-radius: 4px;
}
QFrame#backup_suggest_popup QListWidget::item:selected {
    background: #76b900;
    color: #111114;
}
QPushButton#folder_row_add {
    color: #6a6a78;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
    border: 1px solid #1e1e24;
    border-radius: 9px;
    padding: 0;
}
QPushButton#folder_row_add:hover {
    color: #76b900;
    border-color: #76b900;
}
#file_list_meta {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
#file_list_meta_italic {
    color: #6a6a78;
    font-size: 10px;
    font-style: italic;
    background: transparent;
}
#file_list_hint {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
}
QCheckBox#file_list_cb {
    color: #b0b0b8;
    font-size: 10px;
}
#file_list_error {
    color: #e05555;
    font-size: 10px;
    background: transparent;
}
#cover_editor_hint {
    color: #b0b0b8;
    font-size: 11px;
    background: transparent;
}
QPushButton#file_list_toggle {
    color: #6a6a78;
    font-size: 10px;
    text-align: left;
    padding: 0;
    border: none;
    background: transparent;
}
QPushButton#file_list_toggle:hover {
    color: #b0b0b8;
}
#library_empty {
    color: #6a6a78;
    font-size: 13px;
    padding: 20px 16px;
}
QToolButton#library_sort_dir {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    font-size: 14px;
    color: #e8e8ea;
}

/* Library folder sidebar + tag filter chrome (static) */
#folder_tree {
    background: #111114;
    border-right: 1px solid #1e1e24;
}
#folder_filter_header {
    color: #6a6a78;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
}
#folder_filter_active {
    color: #6a6a78;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
    margin-top: 2px;
}
QLineEdit#folder_filter_search {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 4px;
    padding: 0 6px;
    font-size: 11px;
    color: #e8e8ea;
}
QPushButton#folder_filter_clear {
    color: #6a6a78;
    font-size: 10px;
    background: transparent;
    border: none;
    padding: 2px;
}
QPushButton#folder_filter_clear:hover {
    color: #76b900;
}
#folder_filter_by {
    color: #6a6a78;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
}
QSplitter#folder_tree_splitter::handle {
    background: #2a2a38;
}
QSplitter#folder_tree_splitter::handle:hover {
    background: #76b900;
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

/* Inner labels of the header button: transparent so the button chrome
   shows; the title inherits the button's white weight, the meta (count ·
   size) is muted and right-aligned. */
QLabel#backup_group_title {
    background: transparent;
    color: #e8e8ea;
    font-size: 12px;
    font-weight: 700;
}

QLabel#backup_group_meta {
    background: transparent;
    color: #9a9aa2;
    font-size: 11px;
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

/* ── Sidebar "filter by" tabs (tags | engine) ─────────────────────── */
/* Named and keyed on an "active" property for the same reason as the chips
   above: the two buttons are re-polished on every switch, and a per-button
   stylesheet would have to be rewritten on a theme change. */
QPushButton#filter_tab {
    background: #161619;
    color: #c8c8d0;
    border: 1px solid #2a2a38;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    min-width: 0;
}

QPushButton#filter_tab:hover {
    color: #e8e8ea;
    border-color: #3a3a50;
    background: #1a1a22;
}

/* Active tab: white on accent. Also pinned for :hover/:focus — the global
   QPushButton:hover colour was winning and painting light-on-light / green-
   on-green so the selected label looked blank. */
QPushButton#filter_tab[active="1"],
QPushButton#filter_tab[active="1"]:hover,
QPushButton#filter_tab[active="1"]:pressed,
QPushButton#filter_tab[active="1"]:focus {
    background: #76b900;
    color: #ffffff;
    border: 1px solid #76b900;
    padding: 2px 6px;
    font-weight: 700;
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
#cheats_row_engine { color: #8a8a9a; font-size: 11px; font-weight: 500; }
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

QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #55555f;
    background-color: #16161c;
    border-color: #1e1e24;
}

QSpinBox::up-button:disabled, QSpinBox::down-button:disabled,
QDoubleSpinBox::up-button:disabled, QDoubleSpinBox::down-button:disabled {
    background: #16161c;
}

"""

PALETTE = {
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
    "archive":         "#2ec4b6",
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
