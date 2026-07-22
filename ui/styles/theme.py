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

/* ── GroupBox (Settings) ──────────────────────────────────────────── */
QGroupBox {
    color: #4a4a5a;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    border: 1px solid #1e1e24;
    border-radius: 8px;
    margin-top: 10px;
    padding: 18px 16px 16px 16px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    top: -6px;
    background: #111114;
    padding: 0 6px;
}

/* ── Overview – stat cards ────────────────────────────────────────── */
#stat_card {
    background: #111114;
    border: 1px solid #1e1e24;
    border-radius: 8px;
    min-width: 110px;
}

/* ── Overview – active game banner ───────────────────────────────── */
#active_banner {
    background: #0d140a;
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

/* ── GroupBox (Settings) ──────────────────────────────────────────── */
QGroupBox {
    color: #1a1a2e;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    margin-top: 10px;
    padding: 18px 16px 16px 16px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    top: -6px;
    background: #ffffff;
    padding: 0 6px;
}

/* ── Overview – stat cards ────────────────────────────────────────── */
#stat_card {
    background: #ffffff;
    border: 1px solid #e0e0ea;
    border-radius: 8px;
    min-width: 110px;
}

#stat_label {
    color: #1a1a2e;
}

/* ── Overview – active game banner ───────────────────────────────── */
#active_banner {
    background: #f0f8e0;
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
            reg = self._themed_styles = []
        widget.setStyleSheet(style_fn())
        reg.append((widget, style_fn))
        return widget

    def refresh_styles(self):
        for widget, style_fn in list(getattr(self, "_themed_styles", ())):
            try:
                widget.setStyleSheet(style_fn())
            except RuntimeError:
                pass   # underlying C++ widget already deleted — drop silently


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
