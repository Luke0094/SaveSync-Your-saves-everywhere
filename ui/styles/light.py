"""
SaveSync — light theme (QSS + palette).

Imported by ``ui.styles.theme``. New themes: same exports
(``THEME``, ``PALETTE``, ``ID``, ``IS_DARK``) and register in
``theme.THEMES``.
"""

ID = "light"
IS_DARK = False

THEME = """

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
/* Width is set in code via scaled(200, min_px=190) so DPI wins over QSS. */
#sidebar {
    background-color: #ffffff;
    border-right: 0px solid transparent;
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

#credits_logo {
    background: transparent;
    border: none;
}
#credits_github_btn {
    color: #1a1a2e;
    background: #f0f0f5;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
}
#credits_github_btn:hover {
    background: #5a9400;
    border-color: #5a9400;
    color: #ffffff;
}
#credits_heading {
    color: #1a1a2e;
    background: transparent;
}
#credits_muted {
    color: #4a4a5a;
    font-size: 12px;
    background: transparent;
}
#credits_coin {
    color: #4a4a5a;
    font-size: 11px;
    background: transparent;
}
#credits_sep {
    background: #e0e0ea;
    border: none;
    max-height: 1px;
}
QLineEdit#credits_wallet_field {
    color: #4a4a5a;
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 10px;
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
#nav_btn[notice="true"] {
    padding: 10px 28px 10px 20px;
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
    image: __ICON_DOWN__;
    width: 10px;
    height: 10px;
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
QSpinBox, QDoubleSpinBox {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 6px 10px;
}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 18px;
    background: #f0f0f5;
    border: none;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #d8d8e8;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    width: 8px; height: 8px;
    image: __ICON_UP__;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    width: 8px; height: 8px;
    image: __ICON_DOWN__;
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
    width: 12px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #c0c0d0;
    border-radius: 3px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover { background: #a8a8b8; }
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
    background: #c0c0d0;
    border-radius: 3px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover { background: #a8a8b8; }
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

QPushButton#form_muted_btn {
    color: #8a8a9a;
    border: 1px solid #e8e8f0;
    background: transparent;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 11px;
}
QPushButton#form_muted_btn:hover {
    border-color: #8a8a9a;
    color: #4a4a5a;
}

/* Shared dialog chrome — mirror of DARK_THEME */
#dialog_title {
    color: #1a1a2e;
    font-size: 16px;
    font-weight: 600;
    background: transparent;
}
#dialog_desc {
    color: #4a4a5a;
    font-size: 11px;
    background: transparent;
}
#dialog_status {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#dialog_empty {
    color: #8a8a9a;
    font-size: 12px;
    padding: 24px;
    background: transparent;
}
#update_title {
    color: #1a1a2e;
    font-size: 15px;
    font-weight: 700;
    background: transparent;
    border: none;
}
#update_summary {
    color: #4a4a5a;
    font-size: 12px;
    font-weight: 500;
    background: transparent;
    border: none;
}
#update_note {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
    border: none;
}
QTextEdit#update_notes {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 8px;
    font-size: 12px;
    font-weight: 400;
}
QPushButton#update_later_btn {
    color: #1a1a2e;
    background: #f4f4f8;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#update_later_btn:hover {
    border-color: #c8c8d0;
}
#review_form {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
#review_card {
    background: #f4f4f8;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
#review_who {
    color: #4a4a5a;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
#review_when {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#review_form_title {
    color: #4a4a5a;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
QListWidget#game_search_list {
    background: #f4f4f8;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    font-size: 11px;
}
#dialog_heading {
    color: #1a1a2e;
    font-size: 18px;
    font-weight: 700;
    background: transparent;
}
#review_body {
    color: #1a1a2e;
    font-size: 12px;
    background: transparent;
}
#sidebar_machine {
    color: #9a9aaa;
    font-size: 10px;
    padding: 0 16px 12px;
    background: transparent;
}
#cloud_verify_row {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    padding: 10px;
}
#auto_scan_game_header {
    color: #5a9400;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
}
#auto_scan_muted {
    color: #9a9aaa;
    font-size: 10px;
    background: transparent;
}
#enrich_name {
    color: #1a1a2e;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}
#enrich_source {
    color: #8a8a9a;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#enrich_desc {
    color: #4a4a5a;
    font-size: 12px;
    background: transparent;
}
#enrich_meta {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#enrich_hint {
    color: #9a9aaa;
    font-size: 11px;
    background: transparent;
}
#enrich_btn_hint {
    color: #9a9aaa;
    font-size: 10px;
    background: transparent;
}
#enrich_thumb {
    background: #f4f4f8;
    border: 1px solid #c8c8d0;
    border-radius: 6px;
    font-size: 26px;
}
QPushButton#enrich_inspect_btn {
    color: #5a9400;
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
    color: #b0b0b8;
}
#enrich_intro {
    color: #5a9400;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
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

#quick_card {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
}
#quick_card:hover {
    border-color: #5a9400;
}
#quick_card_name {
    color: #1a1a2e;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}
#sync_muted {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#sync_status {
    color: #8a8a9a;
    font-size: 12px;
    background: transparent;
}
#sync_section_header {
    color: #8a8a9a;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    background: transparent;
}
#sync_setup_guide {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
    padding: 10px 12px;
}
QPushButton#sync_portal_btn {
    background: #5a9400;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#sync_portal_btn:hover {
    background: #6aad00;
}
#sync_provider_hint {
    color: #5a9400;
    font-size: 11px;
    padding: 6px 8px;
    background: #ffffff;
    border-radius: 4px;
    border: 1px solid #e0e0ea;
}
#backup_summary {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#backup_empty {
    color: #b0b0b8;
    font-size: 14px;
    padding: 32px;
    background: transparent;
}
QFrame#path_row {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
QFrame#path_row:hover {
    border-color: #c8c8d0;
}
#path_row_path {
    color: #4a4a5a;
    font-size: 11px;
    background: transparent;
}
#path_row_info {
    color: #9a9aaa;
    font-size: 10px;
    min-width: 110px;
    background: transparent;
}
#settings_section_lbl {
    color: #8a8a9a;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#form_field_lbl {
    color: #8a8a9a;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}
#form_section_lbl {
    color: #8a8a9a;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 0;
    background: transparent;
}
#form_section_lbl_faint {
    color: #b0b0b8;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 0;
    background: transparent;
}
#form_empty_lbl {
    color: #b0b0b8;
    font-size: 11px;
    padding: 8px;
    background: transparent;
}
#form_muted_sm {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#form_secondary_lbl {
    color: #4a4a5a;
    font-size: 12px;
    background: transparent;
}
#img_counter {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#img_preview_frame {
    background: #f4f4f8;
    border: 1px solid #c8c8d0;
    border-radius: 6px;
}
QFrame#url_strip, QFrame#tag_strip {
    background: #ffffff;
    border: 1px solid #e0e0ea;
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
    background: #f4f4f8;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    font-size: 15px;
    font-weight: bold;
    padding: 0;
}
QPushButton#overlay_carousel_arrow:hover {
    background: #5a9400;
    border-color: #5a9400;
    color: #ffffff;
}
QPushButton#overlay_carousel_arrow:disabled {
    color: #9a9aaa;
    border-color: #e0e0ea;
    background: #f4f4f8;
}
QPushButton#enrich_merge_chip {
    background: #f4f4f8;
    color: #4a4a5a;
    border: 1px solid #e0e0ea;
    border-radius: 10px;
    padding: 0 8px;
    font-size: 11px;
}
QPushButton#enrich_merge_chip:hover {
    border-color: #5a9400;
    color: #1a1a2e;
}
QPushButton#enrich_merge_chip:checked {
    background: #5a9400;
    color: #ffffff;
    border-color: #5a9400;
}
QPushButton#img_tool_btn {
    background: #ffffff;
    color: #4a4a5a;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    font-size: 14px;
    padding: 0;
}
QPushButton#img_tool_btn:hover {
    border-color: #5a9400;
    color: #1a1a2e;
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
    background: #f4f4f8;
    color: #4a4a5a;
    border: 1px solid #e0e0ea;
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
    background: #f4f4f8;
    border: 1px solid #c8c8d0;
    border-radius: 10px;
}
QPushButton#url_chip_link {
    background: transparent;
    color: #5a9400;
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
    color: #8a8a9a;
    border: none;
    font-size: 11px;
    padding: 0;
}
QPushButton#url_chip_remove:hover {
    color: #c0392b;
}
QPushButton#overlay_info_btn {
    color: #8a8a9a;
    background: transparent;
    border: 1px solid #c8c8d0;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    font-style: italic;
    padding: 0;
}
QPushButton#overlay_info_btn:hover {
    color: #5a9400;
    border-color: #5a9400;
}
QPushButton#overlay_suppress_btn {
    font-size: 10px;
    color: #8a8a9a;
    padding: 4px 8px;
    border: 1px solid #c8c8d0;
    border-radius: 4px;
    background: transparent;
}
QPushButton#overlay_suppress_btn:hover {
    color: #1a1a2e;
    border-color: #5a9400;
    background: #f4f4f8;
}
#overlay_carousel_counter {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#overlay_icon {
    font-size: 15px;
    min-width: 20px;
    background: transparent;
}
#overlay_separator {
    background: #c8c8d0;
    border: none;
    max-height: 1px;
}
QPushButton#credits_nav_btn {
    color: #4a4a5a;
    background: transparent;
    border: none;
    font-size: 12px;
    font-weight: 600;
    padding: 9px 16px;
    text-align: left;
}
QPushButton#credits_nav_btn:hover {
    color: #1a1a2e;
    background: #f4f4f8;
}
#credits_toast {
    color: #1a1a2e;
    background: #ffffff;
    border: 1px solid #5a9400;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 11px;
    font-weight: 600;
}
QFrame#settings_panel {
    background: #f8f8fc;
    border: 1px solid #e0e0ea;
    border-radius: 10px;
}
QWidget#settings_ui_scale_row {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
}
QPushButton#settings_reset_btn {
    color: #a08000;
    border: 1px solid #a08000;
    background: transparent;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
}
QPushButton#settings_reset_btn:hover {
    background: #a08000;
    color: #ffffff;
}
QListWidget#settings_pref_list {
    background: #ffffff;
    border: 1px solid #5a9400;
    border-radius: 6px;
}
QListWidget#settings_pref_list::item {
    padding: 4px 8px;
    color: #4a4a5a;
    font-size: 11px;
}
QListWidget#settings_pref_list::item:selected {
    background: #f4f4f8;
    color: #c0392b;
}
#deleted_tag {
    color: #9a9aaa;
    font-size: 10px;
    border: 1px solid #e0e0ea;
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
    color: #4a4a5a;
    font-size: 12px;
    background: transparent;
}
#path_entry_meta {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#review_meta_sm {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#review_notes {
    color: #8a8a9a;
    font-size: 11px;
    font-style: italic;
    background: transparent;
}
#review_score {
    color: #4a4a5a;
    font-size: 12px;
    font-weight: 600;
    min-width: 34px;
    background: transparent;
}
#review_form_hint {
    color: #a08000;
    font-size: 11px;
    background: transparent;
}
QLineEdit#review_reviewer {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 12px;
}
QCheckBox#path_row_cb {
    spacing: 4px;
}
#add_dialog_title {
    color: #1a1a2e;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}

#settings_section_hint {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
QFrame#backup_suggest_popup {
    background: #ffffff;
    border: 1px solid #c8c8d0;
    border-radius: 6px;
}
QFrame#backup_suggest_popup QListWidget {
    background: transparent;
    border: none;
    font-size: 12px;
    color: #4a4a5a;
}
QFrame#backup_suggest_popup QListWidget::item {
    padding: 5px 8px;
    border-radius: 4px;
}
QFrame#backup_suggest_popup QListWidget::item:selected {
    background: #5a9400;
    color: #ffffff;
}
QPushButton#folder_row_add {
    color: #8a8a9a;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
    border: 1px solid #e0e0ea;
    border-radius: 9px;
    padding: 0;
}
QPushButton#folder_row_add:hover {
    color: #5a9400;
    border-color: #5a9400;
}
#file_list_meta {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#file_list_meta_italic {
    color: #8a8a9a;
    font-size: 10px;
    font-style: italic;
    background: transparent;
}
#file_list_hint {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
QCheckBox#file_list_cb {
    color: #4a4a5a;
    font-size: 10px;
}
#file_list_error {
    color: #d03030;
    font-size: 10px;
    background: transparent;
}
#cover_editor_hint {
    color: #4a4a5a;
    font-size: 11px;
    background: transparent;
}
QPushButton#file_list_toggle {
    color: #8a8a9a;
    font-size: 10px;
    text-align: left;
    padding: 0;
    border: none;
    background: transparent;
}
QPushButton#file_list_toggle:hover {
    color: #4a4a5a;
}
#library_empty {
    color: #8a8a9a;
    font-size: 13px;
    padding: 20px 16px;
}
QToolButton#library_sort_dir {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    font-size: 14px;
    color: #1a1a2e;
}

/* Library folder sidebar + tag filter chrome (static) — mirror of DARK */
#folder_tree {
    background: #ffffff;
    border-right: 1px solid #e0e0ea;
}
#folder_filter_header {
    color: #8a8a9a;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
}
#folder_filter_active {
    color: #8a8a9a;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
    margin-top: 2px;
}
QLineEdit#folder_filter_search {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 4px;
    padding: 0 6px;
    font-size: 11px;
    color: #1a1a2e;
}
QPushButton#folder_filter_clear {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
    border: none;
    padding: 2px;
}
QPushButton#folder_filter_clear:hover {
    color: #5a9400;
}
#folder_filter_by {
    color: #8a8a9a;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
}
QSplitter#folder_tree_splitter::handle {
    background: #e8e8f0;
}
QSplitter#folder_tree_splitter::handle:hover {
    background: #5a9400;
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
#backup_row_date {
    color: #4a4a5a;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}
#backup_row_meta {
    color: #8a8a9a;
    font-size: 11px;
    background: transparent;
}
#backup_row_meta_sm {
    color: #8a8a9a;
    font-size: 10px;
    background: transparent;
}
#backup_row_note {
    color: #8a8a9a;
    font-size: 10px;
    font-style: italic;
    background: transparent;
}
#backup_row_playing {
    color: #c88a00;
    font-size: 10px;
    font-weight: 600;
    background: transparent;
}

/* ── Sidebar batch progress ──────────────────────────────────────── */
#batch_progress_notice {
    background: #f4f4f8;
    border: 1px solid #e0e0ea;
    border-radius: 6px;
}
#batch_progress_notice QLabel#batch_progress_label {
    color: #4a4a5a;
    font-size: 11px;
    background: transparent;
    border: none;
}
#batch_progress_notice QLabel#batch_progress_label_done {
    color: #1a1a2e;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
    border: none;
}
#batch_progress_notice QProgressBar {
    background: #ffffff;
    border: none;
    border-radius: 2px;
}
#batch_progress_notice QProgressBar::chunk {
    background: #5a9400;
    border-radius: 2px;
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
    padding: 0px;
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

/* Inner labels of the header button — mirror of the DARK_THEME block. */
QLabel#backup_group_title {
    background: transparent;
    color: #1a1a2e;
    font-size: 12px;
    font-weight: 700;
}

QLabel#backup_group_meta {
    background: transparent;
    color: #6a6a76;
    font-size: 11px;
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

/* ── Sidebar "filter by" tabs — mirror of the DARK_THEME block ────── */
QPushButton#filter_tab {
    background: #f2f2f7;
    color: #1a1a2e;
    border: 1px solid #c8c8d4;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    min-width: 0;
}

QPushButton#filter_tab:hover {
    color: #1a1a2e;
    border-color: #5a9400;
    background: #e8e8f0;
}

QPushButton#filter_tab[active="1"],
QPushButton#filter_tab[active="1"]:hover,
QPushButton#filter_tab[active="1"]:pressed,
QPushButton#filter_tab[active="1"]:focus {
    background: #5a9400;
    color: #ffffff;
    border: 1px solid #5a9400;
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
#cheats_row_engine { color: #6a6a7e; font-size: 11px; font-weight: 500; }
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

QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #a8a8b4;
    background-color: #f4f4f8;
    border-color: #e8e8f0;
}

QSpinBox::up-button:disabled, QSpinBox::down-button:disabled,
QDoubleSpinBox::up-button:disabled, QDoubleSpinBox::down-button:disabled {
    background: #f4f4f8;
}

"""

PALETTE = {
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
    "archive":         "#1a9e94",
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
