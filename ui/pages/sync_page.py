"""
SaveSync - Sync Page
Provider selection, credential form, and connection management.

OAuth pre-auth flows (Dropbox, OneDrive) run on the main thread before the
connect worker starts — worker threads must never touch the UI. The provider
is instantiated with full creds (incl. preauth objects) and handed to the
worker directly; disconnecting clears the credential store and sync_provider
config so the app does not auto-reconnect on the next launch.
"""
import os
import webbrowser
import logging

from PySide6.QtCore import Qt, QThread, Signal as QSignal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFrame, QScrollArea, QLineEdit, QFileDialog,
    QCheckBox, QGroupBox, QFormLayout, QMessageBox,
)

from i18n import t
from sync import available_providers, get_provider_class, get_provider_fields, get_orchestrator
from core.config_manager import get_config
from core.library import get_library
from ui.modal_helpers import (
    information_window_modal,
    input_text_window_modal,
    question_window_modal,
    warning_window_modal,
)
from ui.styles.theme import palette, ThemedMixin

logger = logging.getLogger(__name__)


# ── Dynamic credential form ──────────────────────────────────────────────────

class ProviderCredentialForm(QWidget, ThemedMixin):
    """
    Dynamically built form based on provider credential_fields().

    Two widget dictionaries:
      _widgets      — fid → data-bearing widget (QLineEdit, QComboBox, QCheckBox)
                      used by get_values() / set_values()
      _form_widgets — fid → widget actually added to QFormLayout (may be a wrapper)
                      used by _on_select_changed() for show/hide
    """

    def __init__(self, fields: list[dict], parent=None, hint_callback=None, status_callback=None):
        super().__init__(parent)
        self._fields:       list[dict]        = fields
        self._widgets:      dict[str, QWidget] = {}   # data widgets
        self._form_widgets: dict[str, QWidget] = {}   # layout widgets
        self._hint_callback = hint_callback    # called with hint text when method changes
        self._status_callback = status_callback  # called to clear/set status on method change
        self._build()

    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self):
        form = QFormLayout(self)
        form.setSpacing(12)
        form.setContentsMargins(0, 0, 0, 0)

        for field in self._fields:
            fid   = field["id"]
            label = field.get("label", fid)
            ftype = field.get("type", "text")
            hint  = field.get("hint", "")

            # ── select ──────────────────────────────────────────────────────
            if ftype == "select":
                data_w = QComboBox()
                for opt in field.get("options", []):
                    data_w.addItem(opt["label"], opt["value"])
                data_w.currentIndexChanged.connect(self._on_select_changed)
                self._widgets[fid] = data_w
                # Skip hint for select fields — the green provider hint covers this
                form.addRow(label, data_w)
                self._form_widgets[fid] = data_w

            # ── text / password ──────────────────────────────────────────────
            elif ftype in ("text", "password"):
                data_w = QLineEdit()
                data_w.setEchoMode(
                    QLineEdit.EchoMode.Password if ftype == "password"
                    else QLineEdit.EchoMode.Normal
                )
                data_w.setPlaceholderText(field.get("placeholder", ""))
                self._widgets[fid] = data_w
                form_w = self._wrap_hint(data_w, hint)
                form.addRow(label, form_w)
                self._form_widgets[fid] = form_w

            # ── file / folder ────────────────────────────────────────────────
            elif ftype in ("file", "folder"):
                data_w = QLineEdit()
                data_w.setPlaceholderText(field.get("placeholder", ""))
                browse_btn = QPushButton(t("add_game.browse"))
                browse_btn.setFixedWidth(80)
                if ftype == "folder":
                    browse_btn.clicked.connect(
                        lambda _, w=data_w: w.setText(
                            QFileDialog.getExistingDirectory(self, t("sync.select_folder")) or w.text()
                        )
                    )
                else:
                    browse_btn.clicked.connect(
                        lambda _, w=data_w: w.setText(
                            QFileDialog.getOpenFileName(self, t("sync.select_file"))[0] or w.text()
                        )
                    )
                row_w = QWidget()
                row   = QHBoxLayout(row_w)
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(data_w, 1)
                row.addWidget(browse_btn)

                self._widgets[fid] = data_w
                form_w = self._wrap_hint(row_w, hint)
                form.addRow(label, form_w)
                self._form_widgets[fid] = form_w

            # ── bool (checkbox) ──────────────────────────────────────────────
            elif ftype == "bool":
                data_w = QCheckBox(label)
                # Honor the field's declared default — security-relevant for
                # webdav.verify_ssl: an unchecked default would silently
                # disable TLS certificate verification on a fresh connect.
                data_w.setChecked(bool(field.get("default", False)))
                self._widgets[fid] = data_w
                form_w = self._wrap_hint(data_w, hint)
                form.addRow("", form_w)
                self._form_widgets[fid] = form_w

            # ── guide (step-by-step setup instructions) ──────────────────────
            elif ftype == "guide":
                guide_w = QFrame()
                self._sty(guide_w, lambda: (
                    f"QFrame {{ background:{palette('bg_card')}; border:1px solid {palette('border')};"
                    f"border-radius:6px; padding:10px 12px; }}"
                ))
                guide_layout = QVBoxLayout(guide_w)
                guide_layout.setContentsMargins(0, 0, 0, 0)
                guide_layout.setSpacing(6)

                steps = field.get("steps", [])
                for i, step in enumerate(steps, 1):
                    step_lbl = QLabel(f"<b>{i}.</b> {step}")
                    step_lbl.setWordWrap(True)
                    self._sty(step_lbl, lambda: f"color:{palette('text_secondary')};font-size:11px;"
                                                f"background:transparent;")
                    step_lbl.setOpenExternalLinks(True)
                    step_lbl.setTextFormat(Qt.TextFormat.RichText)
                    guide_layout.addWidget(step_lbl)

                portal_url = field.get("portal_url", "")
                portal_label = field.get("portal_label", "")
                if portal_url and portal_label:
                    import webbrowser as _wb
                    open_btn = QPushButton(f"\U0001f310  {portal_label}")
                    open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    self._sty(open_btn, lambda: (
                        f"QPushButton {{ background:{palette('accent')}; color:white; border:none;"
                        f"border-radius:4px; padding:6px 12px; font-size:11px; font-weight:600; }}"
                        f"QPushButton:hover {{ background:{palette('accent_hover')}; }}"
                    ))
                    open_btn.clicked.connect(lambda _, url=portal_url: _wb.open(url))
                    guide_layout.addWidget(open_btn)

                self._widgets[fid] = guide_w  # no data value, just for visibility
                form.addRow("", guide_w)
                self._form_widgets[fid] = guide_w

        self._on_select_changed()

    def _wrap_hint(self, widget: QWidget, hint: str) -> QWidget:
        """Wrap a widget with an optional hint label below it."""
        if not hint:
            return widget
        wrapper = QWidget()
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(widget)
        lbl = QLabel(hint)
        lbl.setWordWrap(True)
        self._sty(lbl, lambda: f"color:{palette('text_hint')};font-size:10px;")
        col.addWidget(lbl)
        return wrapper

    # ── Visibility ───────────────────────────────────────────────────────────

    def _on_select_changed(self):
        """Show/hide fields based on their depends_on rule."""
        # Collect current values of all select (combo) widgets
        current = {}
        for field in self._fields:
            fid = field["id"]
            w   = self._widgets.get(fid)
            if isinstance(w, QComboBox):
                current[fid] = w.currentData()

        form = self.layout()
        for field in self._fields:
            fid     = field["id"]
            depends = field.get("depends_on", {})
            if not depends:
                continue

            form_w = self._form_widgets.get(fid)
            if form_w is None:
                continue

            visible = all(current.get(k) == v for k, v in depends.items())
            form_w.setVisible(visible)

            # Also hide the corresponding label in the form layout
            if form:
                idx = form.indexOf(form_w)
                if idx >= 0:
                    row, _ = form.getItemPosition(idx)
                    label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                    if label_item and label_item.widget():
                        label_item.widget().setVisible(visible)

        # Handle WebDAV preset selection — auto-fill URL template
        preset_w = self._widgets.get("preset")
        url_w = self._widgets.get("url")
        if isinstance(preset_w, QComboBox) and isinstance(url_w, QLineEdit):
            preset = preset_w.currentData()
            _DEFAULT_PLACEHOLDER = "https://nextcloud.example.com/remote.php/dav/files/username/"
            _WEBDAV_TEMPLATES = {
                "nextcloud": "https://{server}/remote.php/dav/files/{username}/",
                "owncloud": "https://{server}/remote.php/dav/files/{username}/",
                "box": "https://dav.box.com/dav",
                "4shared": "https://webdav.4shared.com",
            }
            if preset == "custom":
                url_w.setPlaceholderText(_DEFAULT_PLACEHOLDER)
                if url_w.text() in _WEBDAV_TEMPLATES.values():
                    url_w.clear()  # Clear auto-filled URL from previous preset
            elif preset in ("box", "4shared"):
                url_w.setText(_WEBDAV_TEMPLATES[preset])
            else:
                template = _WEBDAV_TEMPLATES.get(preset, "")
                if template:
                    url_w.setPlaceholderText(template)
                    if url_w.text() in _WEBDAV_TEMPLATES.values():
                        url_w.clear()

        # Update parent hint box and status based on selected method
        method_w = self._widgets.get("method")
        if isinstance(method_w, QComboBox):
            method_val = method_w.currentData() or ""
            # Show the green provider hint only for "local_folder" method
            if self._hint_callback:
                if method_val == "local_folder":
                    for field in self._fields:
                        if field["id"] == "method":
                            hint = field.get("hint", "")
                            self._hint_callback(f"💡 {hint}" if hint else "")
                            break
                else:
                    self._hint_callback("")
            if self._status_callback:
                if method_val != "local_folder":
                    # Clear "folder detected" status when switching away from local_folder
                    self._status_callback("")
                else:
                    # Restore "folder detected" status when switching back to local_folder
                    folder = next(
                        (self._widgets[f["id"]].text()
                         for f in self._fields
                         if f["id"].endswith("_path")
                         and f["id"] in self._widgets
                         and hasattr(self._widgets[f["id"]], "text")
                         and self._widgets[f["id"]].text()),
                        "",
                    )
                    if folder:
                        self._status_callback(
                            f"✓  {t('sync.folder_auto_detected', path=folder)}",
                            palette('success'),
                        )

    # ── Data access ──────────────────────────────────────────────────────────

    def get_values(self) -> dict:
        result = {}
        for field in self._fields:
            fid = field["id"]
            w   = self._widgets.get(fid)
            if w is None:
                continue
            if isinstance(w, QComboBox):
                result[fid] = w.currentData()
            elif isinstance(w, QLineEdit):
                result[fid] = w.text()
            elif isinstance(w, QCheckBox):
                result[fid] = w.isChecked()
        return result

    def set_values(self, values: dict):
        for field in self._fields:
            fid = field["id"]
            w   = self._widgets.get(fid)
            if w is None or fid not in values:
                continue
            val = values[fid]
            if isinstance(w, QComboBox):
                for i in range(w.count()):
                    if w.itemData(i) == val:
                        w.setCurrentIndex(i)
                        break
            elif isinstance(w, QLineEdit):
                w.setText(str(val))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(val))
        self._on_select_changed()


# ── Quick-connect card ───────────────────────────────────────────────────────

class QuickConnectCard(QFrame, ThemedMixin):
    """A single auto-detected quick-connect card (a cloud service the user can
    connect to with one click).

    Themed via ThemedMixin so its palette-dependent inline styles can be
    re-applied IN PLACE on a light/dark switch — SyncPage.refresh_styles()
    cascades into the current cards instead of rebuilding them. The connect
    button reflects a transient connection *state*: its style_fn reads
    ``self._connected`` (and the live palette) at apply time, so refresh_styles
    always re-applies the correct variant for the active theme.
    """

    def __init__(self, pid: str, icon: str, name: str, path: str,
                 method: str, on_connect, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._connected = False
        self._build(icon, name, path, method)
        self.button.clicked.connect(lambda _=False: on_connect())

    def _build(self, icon: str, name: str, path: str, method: str):
        self.setObjectName("quick_card")
        self._sty(self, lambda: (
            f"QFrame#quick_card {{ background:{palette('bg_card')}; border:1px solid {palette('border')};"
            f"border-radius:8px; }}"
            f"QFrame#quick_card:hover {{ border-color:{palette('accent')}; }}"
        ))
        card_layout = QHBoxLayout(self)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(12)

        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet("font-size:20px; background:transparent;")  # static — no palette()
        icon_lbl.setFixedWidth(28)
        card_layout.addWidget(icon_lbl)

        info = QVBoxLayout()
        info.setSpacing(2)
        # Show provider name with method indicator
        method_tag = f"  ({t('sync.local_folder_tag')})" if method == "local_folder" else ""
        name_lbl = QLabel(f"{name}{method_tag}")
        self._sty(name_lbl, lambda: f"color:{palette('text')};font-size:13px;font-weight:600;"
                                    f"background:transparent;")
        info.addWidget(name_lbl)
        path_lbl = QLabel(path)
        self._sty(path_lbl, lambda: f"color:{palette('text_hint')};font-size:10px;background:transparent;")
        info.addWidget(path_lbl)
        card_layout.addLayout(info, 1)

        self.button = QPushButton(t("sync.one_click_connect"))
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setFixedWidth(120)
        # Registered once; the fn reads self._connected so refresh_styles picks
        # the right variant with the currently-active palette.
        self._sty(self.button, lambda: self._button_style())
        card_layout.addWidget(self.button)

    def _button_style(self) -> str:
        if self._connected:
            # Connected: solid success fill, no hover rule (matches original).
            return (
                f"QPushButton {{ background:{palette('success')}; color:{palette('accent_text')}; border:none;"
                f"border-radius:4px; padding:6px 12px; font-size:12px; font-weight:600; }}"
            )
        return (
            f"QPushButton {{ background:{palette('accent')}; color:{palette('accent_text')}; border:none;"
            f"border-radius:4px; padding:6px 12px; font-size:12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{palette('accent_hover')}; }}"
        )

    def set_connected(self, connected: bool):
        """Reflect the current connection state on the button (text + enabled +
        style). The style is applied live here; the registration made in
        ``_build`` re-reads ``self._connected`` on the next refresh_styles()."""
        self._connected = connected
        if connected:
            self.button.setText(t("sync.connected_label"))
            self.button.setEnabled(False)
        else:
            self.button.setText(t("sync.one_click_connect"))
            self.button.setEnabled(True)
        self.button.setStyleSheet(self._button_style())


# ── Sync Page ────────────────────────────────────────────────────────────────

class SyncPage(QWidget, ThemedMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_form: ProviderCredentialForm | None = None
        self._connect_worker = None
        self._abandoned_workers: list = []  # prevent GC of still-running threads
        self._build()
        self._load_saved_config()
        # Populate persisted sync history right away (survives restarts)
        self._refresh_history()

        # Connect sync progress and provider state changes
        orch = get_orchestrator()
        orch.sync_started.connect(self._on_sync_started)
        orch.sync_finished.connect(self._on_sync_done)
        orch.provider_changed.connect(self._on_orchestrator_provider_changed)
        orch.providers_updated.connect(self._on_orchestrator_provider_changed)

    def _on_orchestrator_provider_changed(self, _pid: str = ""):
        """Refresh UI when provider connects/disconnects (e.g. on startup)."""
        self._on_provider_changed()
        self._update_quick_card_states()

    def disconnect_signals(self):
        """Disconnect orchestrator signals to prevent crashes when widget is destroyed."""
        try:
            orch = get_orchestrator()
            orch.sync_started.disconnect(self._on_sync_started)
            orch.sync_finished.disconnect(self._on_sync_done)
            orch.provider_changed.disconnect(self._on_orchestrator_provider_changed)
            orch.providers_updated.disconnect(self._on_orchestrator_provider_changed)
        except (RuntimeError, TypeError):
            pass

    def deleteLater(self):
        """Ensure orchestrator signals are disconnected before destruction."""
        self.disconnect_signals()
        super().deleteLater()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(24)

        self._header = QLabel(t("sync.title"))
        self._header.setObjectName("page_header")
        root.addWidget(self._header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout  = QVBoxLayout(content)
        layout.setSpacing(20)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Quick-connect cards (auto-detected cloud services) ────────────────
        self._quick_cards_widget = QWidget()
        self._quick_cards_layout = QVBoxLayout(self._quick_cards_widget)
        self._quick_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._quick_cards_layout.setSpacing(8)
        self._build_quick_connect_cards()
        layout.addWidget(self._quick_cards_widget)

        # ── Provider selection ────────────────────────────────────────────────
        self._prov_group = prov_group = self._make_group(t("sync.provider"))
        prov_layout = QVBoxLayout(prov_group)

        prov_row = QHBoxLayout()
        self._provider_combo = QComboBox()
        self._provider_combo.addItem(t("sync.no_provider"), None)
        for p in available_providers():
            self._provider_combo.addItem(p["name"], p["id"])
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        prov_row.addWidget(self._provider_combo, 1)

        self._connect_btn = QPushButton(t("sync.connect"))
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._connect_btn.setFixedWidth(120)
        self._sty(self._connect_btn, lambda: (
            f"QPushButton {{ background:{palette('accent')}; color:{palette('accent_text')}; border:none;"
            f"border-radius:4px; padding:6px 12px; font-size:12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{palette('accent_hover')}; }}"
        ))
        self._connect_btn.clicked.connect(self._on_connect_toggle)
        self._connect_btn.setVisible(False)  # Hidden until a provider is selected
        prov_row.addWidget(self._connect_btn)
        prov_layout.addLayout(prov_row)

        self._conn_status = QLabel()
        self._sty(self._conn_status, lambda: f"color: {palette('text_hint')}; font-size: 12px;")
        prov_layout.addWidget(self._conn_status)

        # Dynamic credential form area
        self._form_container = QWidget()
        self._form_layout    = QVBoxLayout(self._form_container)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setSpacing(6)

        self._provider_hint = QLabel()
        self._provider_hint.setWordWrap(True)
        self._sty(self._provider_hint, lambda: (
            f"color:{palette('success')};font-size:11px;padding:6px 8px;"
            f"background:{palette('bg_card')};border-radius:4px;border:1px solid {palette('border')};"
        ))
        self._provider_hint.setVisible(False)
        self._provider_hint.setMaximumHeight(0)  # No space when hidden
        self._form_layout.addWidget(self._provider_hint)

        prov_layout.addWidget(self._form_container)
        layout.addWidget(prov_group)

        # ── Auto-backup ───────────────────────────────────────────────────────
        self._auto_group = auto_group = self._make_group(t("sync.auto_backup"))
        QVBoxLayout(auto_group)   # parented on creation — keeps the group's frame laid out
        # Backup/sync automation toggles live in Settings, not here.

        # ── Sync all button ───────────────────────────────────────────────────
        self._sync_all_btn = QPushButton(t("sync.sync_now"))
        self._sync_all_btn.setObjectName("primary_btn")
        self._sync_all_btn.clicked.connect(self._sync_all)
        layout.addWidget(self._sync_all_btn)

        self._sync_progress = QLabel()
        self._sty(self._sync_progress, lambda: f"color: {palette('text_hint')}; font-size: 11px;")
        self._sync_progress.setVisible(False)
        layout.addWidget(self._sync_progress)

        # ── Sync History ─────────────────────────────────────────────────────
        self._history_group = self._make_group(t("sync.history"))
        history_layout = QVBoxLayout(self._history_group)
        self._history_list = QLabel(t("sync.no_history"))
        self._sty(self._history_list, lambda: f"color: {palette('text_hint')}; font-size: 11px;")
        self._history_list.setWordWrap(True)
        history_layout.addWidget(self._history_list)
        layout.addWidget(self._history_group)

        layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)

    def _make_group(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        self._sty(g, lambda: f"""
            QGroupBox {{ color: {palette('text_muted')}; font-size: 11px; font-weight: 600;
                        letter-spacing: 0.5px; border: 1px solid {palette('border')};
                        border-radius: 8px; margin-top: 8px; padding: 16px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 12px; top: -6px;
                               background: {palette('bg')}; padding: 0 4px; }}
        """)
        return g

    # ── Quick-connect cards ──────────────────────────────────────────────────

    def _build_quick_connect_cards(self):
        """Scan for installed cloud apps and show one-click connect cards."""
        # Current QuickConnectCard instances — refresh_styles() cascades into
        # these so a theme switch restyles them in place instead of rebuilding.
        self._quick_cards: list[QuickConnectCard] = []

        detected = []
        for pid, icon, label_key in [
            ("onedrive",     "☁",  "providers.onedrive"),
            ("google_drive", "📁", "providers.google_drive"),
            ("dropbox",      "📦", "providers.dropbox"),
        ]:
            folder_info = self._detect_cloud_folder(pid)
            if folder_info:
                path = next((v for k, v in folder_info.items() if k.endswith("_path")), "")
                detected.append((pid, icon, t(label_key), path, folder_info))

        # Also detect rclone remotes
        rclone_remotes = self._detect_rclone_remotes()
        for remote_name in rclone_remotes[:3]:  # Show max 3 remotes
            detected.append((
                "rclone", "⚙", f"rclone: {remote_name}", remote_name,
                {"remote": remote_name, "base_path": "SaveSync"},
            ))

        if not detected:
            self._quick_cards_widget.setVisible(False)
            return

        header = QLabel(t("sync.quick_connect_header"))
        self._sty(header, lambda: f"color:{palette('text_muted')};font-size:11px;font-weight:600;"
                                  f"letter-spacing:0.5px;")
        self._quick_cards_layout.addWidget(header)

        for pid, icon, name, path, creds_dict in detected:
            card = QuickConnectCard(
                pid, icon, name, path,
                creds_dict.get("method", ""),
                on_connect=lambda p=pid, c=creds_dict: self._quick_connect(p, c),
            )
            self._quick_cards.append(card)
            self._quick_cards_layout.addWidget(card)

        # Reflect current connection state in cards
        self._update_quick_card_states()

    def _update_quick_card_states(self):
        """Update quick-connect cards to reflect the current connection state."""
        orch = get_orchestrator()
        connected_pids = set(orch.get_connected_provider_ids())
        for card in self._quick_cards:
            card.set_connected(card.pid in connected_pids)

    # ── Theme refresh ─────────────────────────────────────────────────────────

    def refresh_styles(self):
        """Re-apply every recorded palette-dependent inline style with the
        now-current palette, then cascade into the dynamic children that are
        created fresh on provider/locale changes (the quick-connect cards and
        the active credential form). This lets a light/dark switch restyle the
        page IN PLACE — no widget-tree rebuild.

        Transient runtime styles are not registered as themed templates: the
        connect-button variant is handled inside QuickConnectCard (its style_fn
        reads self._connected, so the cascade re-applies the right one), while
        _show_status() paints a runtime colour on _conn_status and simply falls
        back to that label's registered resting style on a switch.
        """
        super().refresh_styles()
        for card in list(getattr(self, "_quick_cards", ())):
            try:
                card.refresh_styles()
            except RuntimeError:
                pass  # underlying C++ card already deleted
        form = getattr(self, "_current_form", None)
        if form is not None:
            try:
                form.refresh_styles()
            except RuntimeError:
                pass

    @staticmethod
    def _get_method_label(provider_id: str, method: str) -> str:
        """Get human-readable label for a connection method from the provider's credential fields."""
        fields = get_provider_fields(provider_id)
        for field in fields:
            if field.get("id") == "method" and field.get("type") == "select":
                for opt in field.get("options", []):
                    if opt.get("value") == method:
                        return opt.get("label", method)
        return method

    def _quick_connect(self, provider_id: str, creds: dict):
        """One-click connect using auto-detected credentials."""
        # Check if this provider is already connected with the same method
        orch = get_orchestrator()
        existing = orch.get_provider(provider_id)
        if existing and existing.is_connected:
            current_method = getattr(existing, '_credentials', {}).get("method", "")
            new_method = creds.get("method", "")
            if current_method == new_method:
                label = self._get_method_label(provider_id, current_method)
                user = existing.user_display
                self._show_status(
                    f"✓  {t('sync.already_connected', method=label, user=user)}",
                    palette('success'),
                )
                return

        # Store pending info — saved to config only on success
        self._pending_quick_pid = provider_id
        self._pending_quick_creds = creds

        # Select provider in combo and build the form immediately so the UI
        # reflects the switch before the connection starts.
        self._provider_combo.blockSignals(True)
        for i in range(self._provider_combo.count()):
            if self._provider_combo.itemData(i) == provider_id:
                self._provider_combo.setCurrentIndex(i)
                break
        self._provider_combo.blockSignals(False)
        self._on_provider_changed()

        # Instantiate provider
        cls = get_provider_class(provider_id)
        if not cls:
            return
        provider = cls(creds)

        # Local-folder methods connect instantly (just a path check) —
        # no background thread needed.
        if creds.get("method") == "local_folder":
            try:
                ok = provider.connect()
                # On failure, surface the provider's own reason ("rclone not
                # found in PATH", "no URL provided", …) instead of collapsing
                # every returns-False connect into a generic "auth failed".
                user = (getattr(provider, "user_display", "") if ok
                        else getattr(provider, "last_error", ""))
            except Exception as e:
                ok, user = False, str(e)[:120]
            self._on_quick_connect_result(ok, user, provider if ok else None)
            return

        # Remote / OAuth methods need a worker thread
        self._connect_btn.setEnabled(False)
        self._connect_btn.setText(t("sync.connecting"))
        self._show_status(f"⟳  {t('sync.connecting')}", palette('warning'))

        class _QuickWorker(QThread):
            done = QSignal(bool, str, object)
            def run(self_w):
                try:
                    ok = provider.connect()
                    user = (getattr(provider, "user_display", "") if ok
                            else getattr(provider, "last_error", ""))
                    self_w.done.emit(ok, user, provider if ok else None)
                except Exception as e:
                    self_w.done.emit(False, str(e)[:120], None)

        # Stop any existing worker before starting a new one
        self._cancel_connect_worker()
        self._connect_worker = _QuickWorker()
        self._connect_worker.done.connect(self._on_quick_connect_result)
        self._connect_worker.finished.connect(lambda: self._clear_connect_worker_ref())
        self._connect_worker.start()

    def _on_quick_connect_result(self, ok: bool, user_display: str, provider):
        """Handle quick-connect result — update card states, keep cards visible."""
        self._connect_btn.setEnabled(True)
        if ok and provider is not None:
            # Persist credentials only after successful connection
            pid = getattr(self, '_pending_quick_pid', None)
            creds = getattr(self, '_pending_quick_creds', None)
            if pid and creds:
                from core.credentials import get_credential_store
                persistent = {k: v for k, v in creds.items() if not k.startswith("_")}
                get_credential_store().save(pid, persistent)
            # set_provider handles sync_providers list and providers_connected
            get_orchestrator().set_provider(provider)
            msg = t("sync.connected_as", user=user_display) if user_display else t("sync.connected")
            self._show_status(f"✓  {msg}", palette('success'))
            self._connect_btn.setText(t("sync.disconnect"))
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, lambda p=provider: self._check_cloud_config(p))
        else:
            err = user_display or t("errors.auth_failed")
            self._show_status(f"✗  {err}", palette('error'))
            self._connect_btn.setText(t("sync.connect"))
        self._update_quick_card_states()

    @staticmethod
    def _detect_rclone_remotes() -> list[str]:
        """List configured rclone remotes, or empty list if rclone not available."""
        import shutil
        import subprocess
        rclone = shutil.which("rclone")
        if not rclone:
            return []
        try:
            # CREATE_NO_WINDOW: this runs during SyncPage construction at
            # STARTUP — without it the windowless app flashes a console
            # for an instant whenever rclone is installed.
            result = subprocess.run(
                [rclone, "listremotes"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return [r.rstrip(":").strip() for r in result.stdout.splitlines() if r.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return []

    # ── Provider selection ────────────────────────────────────────────────────

    @staticmethod
    def _get_provider_hint(provider_id: str) -> str:
        key = f"sync.provider_hint_{provider_id}"
        hint = t(key)
        return f"💡 {hint}" if hint != key else ""

    def _show_hint(self, text: str):
        """Show provider hint box with text, or hide it completely if empty."""
        if text:
            self._provider_hint.setText(text)
            self._provider_hint.setVisible(True)
            self._provider_hint.setMaximumHeight(16777215)
        else:
            self._provider_hint.setText("")
            self._provider_hint.setVisible(False)
            self._provider_hint.setMaximumHeight(0)

    def _show_status(self, text: str, color: str = ""):
        """Show or hide the connection status label. Hides completely when empty."""
        if text:
            self._conn_status.setText(text)
            self._conn_status.setStyleSheet(f"color:{color or palette('text_hint')};font-size:12px;")
            self._conn_status.setVisible(True)
        else:
            self._conn_status.setText("")
            self._conn_status.setVisible(False)

    def _clear_connect_worker_ref(self):
        """Clear reference and schedule C++ deletion after worker finishes."""
        worker = self._connect_worker
        self._connect_worker = None
        if worker is not None:
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        # Clean up any abandoned workers that have since finished
        self._abandoned_workers = [
            w for w in self._abandoned_workers
            if self._is_worker_running(w)
        ]

    @staticmethod
    def _is_worker_running(worker) -> bool:
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _cancel_connect_worker(self):
        """Cancel any running connect worker and reset button state."""
        if hasattr(self, '_connect_worker') and self._connect_worker is not None:
            worker = self._connect_worker
            self._connect_worker = None
            try:
                # Disconnect the done signal so stale results are ignored
                try:
                    worker.done.disconnect()
                except RuntimeError:
                    pass
                if worker.isRunning():
                    # Ask the thread to stop (cooperative cancellation)
                    worker.requestInterruption()
                    # Don't block the UI — park the worker and let it finish
                    # on its own. deleteLater fires once the thread ends.
                    worker.finished.connect(worker.deleteLater)
                    self._abandoned_workers.append(worker)
                else:
                    try:
                        worker.deleteLater()
                    except RuntimeError:
                        pass
            except RuntimeError:
                # C++ object already deleted
                pass
        # Clean up finished abandoned workers
        self._abandoned_workers = [
            w for w in self._abandoned_workers
            if self._is_worker_running(w)
        ]
        self._connect_btn.setEnabled(True)

    def _on_provider_changed(self):
        # Cancel any running connect worker so changing provider doesn't leave
        # the button disabled or fire stale callbacks.
        self._cancel_connect_worker()

        pid = self._provider_combo.currentData()

        # Update connection status and button based on selected vs connected provider
        orch = get_orchestrator()
        existing = orch.get_provider(pid) if pid else None
        if existing and existing.is_connected:
            user = getattr(existing, "user_display", "")
            msg = t("sync.connected_as", user=user) if user else t("sync.connected")
            self._show_status(f"✓  {msg}", palette('success'))
            self._connect_btn.setText(t("sync.disconnect"))
            self._connect_btn.setVisible(True)
        elif pid:
            self._show_status("")
            self._connect_btn.setText(t("sync.connect"))
            self._connect_btn.setVisible(True)
        else:
            self._show_status("")
            self._connect_btn.setVisible(False)

        # Remove old form widget (keep hint widget in layout)
        while self._form_layout.count() > 1:
            item = self._form_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self._current_form = None

        if not pid:
            self._show_hint("")
            return

        # Show provider hint
        self._show_hint(self._get_provider_hint(pid))

        # Build credential form
        fields = get_provider_fields(pid)
        if fields:
            self._current_form = ProviderCredentialForm(fields, hint_callback=self._show_hint, status_callback=self._show_status)
            self._form_layout.addWidget(self._current_form)

            # Auto-detect cloud provider local folders (only if not already connected to this provider)
            if not (existing and existing.is_connected):
                detected = self._detect_cloud_folder(pid)
                if detected:
                    self._current_form.set_values(detected)
                    # Show "folder detected" only if method is local_folder after set_values
                    creds = self._current_form.get_values()
                    if creds.get("method") == "local_folder":
                        folder = next((v for k, v in detected.items() if k.endswith("_path")), "")
                        if folder:
                            self._show_status(f"✓  {t('sync.folder_auto_detected', path=folder)}", palette('success'))

    # ── Cloud folder auto-detection ──────────────────────────────────────────

    @staticmethod
    def _detect_cloud_folder(provider_id: str) -> dict:
        """Auto-detect local sync folders for cloud providers.

        Returns a dict of {field_id: value} to pre-fill in the form,
        or empty dict if nothing was detected.
        """
        import platform
        from pathlib import Path

        home = Path.home()
        system = platform.system()

        if provider_id == "onedrive":
            # OneDrive — check registry on Windows, common paths otherwise
            candidates = []
            if system == "Windows":
                # Try Windows registry for the actual OneDrive path
                try:
                    import winreg
                    for key_path in (
                        r"Software\Microsoft\OneDrive",
                        r"Software\Microsoft\OneDrive\Accounts\Personal",
                    ):
                        try:
                            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                                val, _ = winreg.QueryValueEx(key, "UserFolder")
                                if val and Path(val).is_dir():
                                    return {"method": "local_folder", "onedrive_folder_path": val}
                        except (FileNotFoundError, OSError):
                            continue
                except ImportError:
                    pass
                # Environment variable set by OneDrive
                env_path = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
                if env_path and Path(env_path).is_dir():
                    candidates.append(Path(env_path))
                candidates += [
                    home / "OneDrive",
                    home / "OneDrive - Personal",
                ]
            elif system == "Darwin":
                candidates = [
                    home / "OneDrive",
                    home / "OneDrive - Personal",
                    home / "Library" / "CloudStorage" / "OneDrive-Personal",
                ]
            else:
                candidates = [home / "OneDrive"]

            for p in candidates:
                if p.is_dir():
                    return {"method": "local_folder", "onedrive_folder_path": str(p)}

        elif provider_id == "google_drive":
            candidates = []
            if system == "Windows":
                # Google Drive for Desktop uses a virtual drive letter or stream path
                # Check common locations
                candidates = [
                    home / "Google Drive",
                    home / "My Drive",
                    Path("G:\\My Drive"),
                    Path("G:\\"),
                ]
                # Also check all drive letters for "My Drive" folder
                for letter in "GHIJKLMNOP":
                    gp = Path(f"{letter}:\\My Drive")
                    if gp.is_dir():
                        candidates.insert(0, gp)
                        break
                    gp2 = Path(f"{letter}:\\")
                    # Check if this looks like a Google Drive mount
                    if gp2.is_dir():
                        marker = gp2 / ".shortcut-targets-by-id"
                        if marker.exists():
                            candidates.insert(0, gp2)
                            break
            elif system == "Darwin":
                candidates = [
                    home / "Google Drive",
                    home / "Google Drive" / "My Drive",
                    home / "Library" / "CloudStorage" / "GoogleDrive-you@gmail.com" / "My Drive",
                ]
                # Scan CloudStorage for any GoogleDrive-* folder
                cloud_storage = home / "Library" / "CloudStorage"
                if cloud_storage.is_dir():
                    try:
                        for d in cloud_storage.iterdir():
                            if d.name.startswith("GoogleDrive") and d.is_dir():
                                my_drive = d / "My Drive"
                                candidates.insert(0, my_drive if my_drive.is_dir() else d)
                                break
                    except OSError:
                        pass
            else:
                candidates = [home / "Google Drive"]

            for p in candidates:
                if p.is_dir():
                    return {"method": "local_folder", "drive_folder_path": str(p)}

        elif provider_id == "dropbox":
            candidates = []
            if system == "Windows":
                # Dropbox stores its path in a JSON info file
                info_paths = [
                    Path(os.environ.get("LOCALAPPDATA", "")) / "Dropbox" / "info.json",
                    Path(os.environ.get("APPDATA", "")) / "Dropbox" / "info.json",
                ]
                for info_path in info_paths:
                    if info_path.is_file():
                        try:
                            import json
                            with open(info_path, encoding="utf-8") as f:
                                info = json.load(f)
                            for acct in ("personal", "business"):
                                acct_info = info.get(acct, {})
                                db_path = acct_info.get("path", "")
                                if db_path and Path(db_path).is_dir():
                                    return {"method": "local_folder", "dropbox_folder_path": db_path}
                        except (json.JSONDecodeError, OSError):
                            continue
                candidates = [
                    home / "Dropbox",
                    home / "Dropbox (Personal)",
                ]
            elif system == "Darwin":
                # macOS also uses info.json
                info_path = home / ".dropbox" / "info.json"
                if info_path.is_file():
                    try:
                        import json
                        with open(info_path, encoding="utf-8") as f:
                            info = json.load(f)
                        for acct in ("personal", "business"):
                            db_path = info.get(acct, {}).get("path", "")
                            if db_path and Path(db_path).is_dir():
                                return {"method": "local_folder", "dropbox_folder_path": db_path}
                    except (json.JSONDecodeError, OSError):
                        pass
                candidates = [
                    home / "Dropbox",
                    home / "Dropbox (Personal)",
                    home / "Library" / "CloudStorage" / "Dropbox",
                ]
            else:
                candidates = [home / "Dropbox"]

            for p in candidates:
                if p.is_dir():
                    return {"method": "local_folder", "dropbox_folder_path": str(p)}

        return {}

    # ── Connect / Disconnect ──────────────────────────────────────────────────

    def _on_connect_toggle(self):
        orch = get_orchestrator()
        pid = self._provider_combo.currentData()
        connected_pids = orch.get_connected_provider_ids()
        if pid in connected_pids:
            self._on_disconnect(pid)
        else:
            self._on_connect()

    def _on_connect(self):
        pid = self._provider_combo.currentData()
        if not pid:
            return
        creds = self._current_form.get_values() if self._current_form else {}

        # ── Check if already connected with the same method ──────────────────
        orch = get_orchestrator()
        existing = orch.get_provider(pid)
        if existing and existing.is_connected:
            current_method = getattr(existing, '_credentials', {}).get("method", "")
            new_method = creds.get("method", "")
            if current_method == new_method:
                label = self._get_method_label(pid, current_method)
                user = existing.user_display
                self._show_status(
                    f"✓  {t('sync.already_connected', method=label, user=user)}",
                    palette('success'),
                )
                return

        # ── Validate required fields ──────────────────────────────────────────
        fields = get_provider_fields(pid)
        for field in fields:
            fid = field["id"]
            if not field.get("required", False):
                continue
            # Skip fields hidden by depends_on
            depends = field.get("depends_on", {})
            if depends and not all(creds.get(k) == v for k, v in depends.items()):
                continue
            val = creds.get(fid, "")
            if isinstance(val, str) and not val.strip():
                label = field.get("label", fid)
                self._set_status_error(t("sync.field_required", field=label))
                return

        # ── Pre-auth flows (must run in main/UI thread) ───────────────────────
        if pid == "dropbox" and creds.get("method") in ("oauth", "oauth_simple"):
            if creds.get("method") == "oauth_simple":
                from sync.app_credentials import DROPBOX_APP_KEY
                creds["app_key"] = DROPBOX_APP_KEY
            if not self._preauth_dropbox(creds):
                return

        elif pid == "onedrive" and creds.get("method") in ("oauth", "oauth_simple"):
            if creds.get("method") == "oauth_simple":
                from sync.app_credentials import ONEDRIVE_CLIENT_ID, ONEDRIVE_TENANT
                creds["client_id"] = ONEDRIVE_CLIENT_ID
                creds["tenant"] = ONEDRIVE_TENANT
            if not self._preauth_onedrive(creds):
                return

        elif pid == "google_drive" and creds.get("method") in ("oauth", "oauth_simple"):
            if not self._preauth_google(creds):
                return

        # ── Store pending credentials — persisted only on successful connect ──
        self._pending_connect_pid = pid
        self._pending_connect_creds = creds

        # ── Instantiate provider with full creds (incl. preauth objects) ───────
        cls = get_provider_class(pid)
        if not cls:
            return
        provider = cls(creds)

        # ── Start connect worker ──────────────────────────────────────────────
        self._connect_btn.setEnabled(False)
        self._connect_btn.setText(t("sync.connecting"))
        self._show_status(f"⟳  {t('sync.connecting')}", palette('warning'))

        class _ConnectWorker(QThread):
            done = QSignal(bool, str, object)   # ok, user_display, provider_or_None

            def run(self_w):
                try:
                    ok = provider.connect()
                    user = (getattr(provider, "user_display", "") if ok
                            else getattr(provider, "last_error", ""))
                    self_w.done.emit(ok, user, provider if ok else None)
                except Exception as e:
                    self_w.done.emit(False, str(e)[:120], None)

        # Stop any existing connect worker before starting a new one
        self._cancel_connect_worker()
        self._connect_btn.setEnabled(False)  # re-disable after cancel reset it
        self._connect_worker = _ConnectWorker()
        self._connect_worker.done.connect(self._on_connect_result)
        self._connect_worker.finished.connect(lambda: self._clear_connect_worker_ref())
        self._connect_worker.start()

    def _on_connect_result(self, ok: bool, user_display: str, provider):
        """Called on main thread — safe to mutate orchestrator here."""
        self._connect_btn.setEnabled(True)
        if ok and provider is not None:
            # Persist credentials only after successful connection
            pid = getattr(self, '_pending_connect_pid', None)
            creds = getattr(self, '_pending_connect_creds', None)
            if pid and creds:
                from core.credentials import get_credential_store
                persistent = {k: v for k, v in creds.items() if not k.startswith("_")}
                get_credential_store().save(pid, persistent)
            # set_provider handles sync_providers list and providers_connected
            get_orchestrator().set_provider(provider)
            msg = t("sync.connected_as", user=user_display) if user_display else t("sync.connected")
            self._show_status(f"✓  {msg}", palette('success'))
            self._connect_btn.setText(t("sync.disconnect"))
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, lambda p=provider: self._check_cloud_config(p))
        else:
            err = user_display or t("errors.auth_failed")
            self._show_status(f"✗  {err}", palette('error'))
            self._connect_btn.setText(t("sync.connect"))
        self._update_quick_card_states()

    def _check_cloud_config(self, provider):
        """After connecting, check if a config from another machine exists on cloud.

        Runs the network check in a background thread to avoid freezing the GUI.
        """
        import threading
        from collections import deque

        if not hasattr(self, '_cloud_check_results'):
            self._cloud_check_results = deque()

        def _bg_check():
            try:
                from core.config_transfer import check_cloud_config, should_prompt_cloud_import
                info = check_cloud_config(provider)
                if not info or not info.get("exists"):
                    return
                if not should_prompt_cloud_import(info["remote_meta"]):
                    return
                # Append to thread-safe deque instead of writing attributes directly
                self._cloud_check_results.append((info, provider))
                from PySide6.QtCore import QMetaObject, Qt as QtC
                QMetaObject.invokeMethod(
                    self, "_show_cloud_config_prompt",
                    QtC.ConnectionType.QueuedConnection,
                )
            except Exception as e:
                logger.debug(f"Cloud config check failed: {e}")

        threading.Thread(target=_bg_check, daemon=True).start()

    @Slot()
    def _show_cloud_config_prompt(self):
        """Show cloud config prompt on the GUI thread."""
        if not hasattr(self, '_cloud_check_results') or not self._cloud_check_results:
            return
        info, provider = self._cloud_check_results.popleft()
        if not info or not provider:
            return
        provider_name = getattr(provider, "DISPLAY_NAME", "cloud")
        from ui.dialogs.config_import_dialog import CloudConfigPromptDialog
        dlg = CloudConfigPromptDialog(info, provider_name, self)
        dlg.import_requested.connect(lambda _pw="": self._do_cloud_import(provider, info))
        dlg.skipped.connect(self._on_cloud_config_skipped)
        dlg.exec()

    def _do_cloud_import(self, provider, info: dict):
        """Download and import cloud config in a background thread."""
        import threading
        from collections import deque

        if not hasattr(self, '_cloud_import_results'):
            self._cloud_import_results = deque()

        def _bg_download():
            try:
                from core.config_transfer import download_and_parse_cloud_config
                parsed = download_and_parse_cloud_config(provider)
                # Append to thread-safe deque instead of writing attributes directly
                self._cloud_import_results.append((parsed, info))
                from PySide6.QtCore import QMetaObject, Qt as QtC
                QMetaObject.invokeMethod(
                    self, "_show_cloud_import_preview",
                    QtC.ConnectionType.QueuedConnection,
                )
            except ValueError:
                from PySide6.QtCore import QMetaObject, Qt as QtC
                QMetaObject.invokeMethod(
                    self, "_show_cloud_import_error",
                    QtC.ConnectionType.QueuedConnection,
                )
            except Exception as e:
                logger.error(f"Cloud config download failed: {e}")
                from PySide6.QtCore import QMetaObject, Qt as QtC
                QMetaObject.invokeMethod(
                    self, "_show_cloud_import_error",
                    QtC.ConnectionType.QueuedConnection,
                )

        threading.Thread(target=_bg_download, daemon=True).start()

    @Slot()
    def _show_cloud_import_preview(self):
        """Show import preview dialog on the GUI thread."""
        if not hasattr(self, '_cloud_import_results') or not self._cloud_import_results:
            return
        parsed, info = self._cloud_import_results.popleft()
        if not parsed:
            return
        from core.config_transfer import preview_import, apply_import, mark_cloud_config_imported
        preview = preview_import(parsed)

        if preview.get("is_identical"):
            # Config on cloud is identical to local — nothing to import
            if info:
                mark_cloud_config_imported(info["remote_meta"])
            return

        from ui.dialogs.config_import_dialog import ConfigImportPreviewDialog
        dlg = ConfigImportPreviewDialog(preview, preview["has_credentials"], self)

        def _do_apply(settings, library, creds, strategy):
            try:
                result = apply_import(parsed, settings, library, creds, strategy)
                if info:
                    mark_cloud_config_imported(info["remote_meta"])
                if result.get("credentials_skipped_machine"):
                    information_window_modal(
                        self,
                        t("settings.import_config"),
                        t("settings.credentials_skipped_machine"),
                    )
            except Exception as e:
                logger.error(f"Cloud config apply failed: {e}")

        dlg.import_confirmed.connect(_do_apply)
        dlg.exec()

    @Slot()
    def _show_cloud_import_error(self):
        warning_window_modal(self, t("config_transfer.cloud_config_found"),
                            t("settings.import_corrupt"))

    def _on_cloud_config_skipped(self, never_ask: bool):
        if never_ask:
            get_config().set("suppress_cloud_config_prompt", True)
            get_config().save()

    def _on_disconnect(self, provider_id: str = None):
        """Disconnect a specific provider and clear its stored credentials."""
        pid = provider_id or self._provider_combo.currentData()
        if not pid:
            return
        reply = question_window_modal(
            self,
            t("sync.disconnect_title"),
            t("sync.disconnect_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        from core.credentials import get_credential_store
        orch = get_orchestrator()
        orch.disconnect_provider(pid)
        get_credential_store().delete_provider(pid)
        self._show_status("")
        self._connect_btn.setText(t("sync.connect"))
        self._update_quick_card_states()

    # ── Provider-specific pre-auth helpers ────────────────────────────────────

    def _preauth_dropbox(self, creds: dict) -> bool:
        """
        Dropbox OAuth pre-auth. Two modes:
        - oauth_simple: localhost redirect (browser handles everything automatically)
        - oauth (advanced): manual code copy-paste (NoRedirect flow)
        """
        app_key = creds.get("app_key", "").strip()
        if not app_key:
            self._set_status_error(t("sync.enter_dropbox_key"))
            return False

        try:
            from sync.dropbox_provider import DropboxProvider
        except ImportError:
            self._set_status_error(t("sync.dropbox_not_installed"))
            return False

        # Simple mode: localhost redirect — runs in the connect worker (non-blocking)
        # Capture theme colors here (main thread) so the worker doesn't call palette()
        if creds.get("method") == "oauth_simple":
            creds["_use_localhost_oauth"] = True
            creds["_theme_bg"] = palette('bg')
            creds["_theme_fg"] = palette('text')
            creds["_theme_accent"] = palette('accent')
            creds["_lbl_success"] = t('dropbox.oauth_callback_success')
            creds["_lbl_close"] = t('dropbox.oauth_callback_close')
            creds["_lbl_failed"] = t('dropbox.oauth_callback_failed')
            creds["_lbl_retry"] = t('dropbox.oauth_callback_retry')
            return True

        # Advanced mode: manual code copy-paste
        try:
            url, auth_flow = DropboxProvider.start_oauth_flow(app_key)
        except Exception as e:
            self._set_status_error(t("sync.could_not_start_oauth", error=e))
            return False

        webbrowser.open(url)

        code, ok = input_text_window_modal(
            self,
            t("sync.dropbox_auth_title"),
            t("sync.dropbox_auth_text"),
        )
        if not ok or not code.strip():
            self._set_status_error(t("sync.auth_cancelled"))
            return False

        creds["_oauth_code"] = code.strip()
        creds["_oauth_flow"] = auth_flow
        return True

    def _preauth_onedrive(self, creds: dict) -> bool:
        """
        Initiate OneDrive MSAL device-code flow in the main thread, show instructions,
        then inject preauth objects into creds for the worker thread.
        """
        client_id = creds.get("client_id", "").strip()
        if not client_id:
            self._set_status_error(t("sync.enter_azure_client_id"))
            return False
        try:
            from sync.onedrive_provider import OneDriveProvider
            result = OneDriveProvider.start_device_flow(
                client_id, creds.get("tenant", "consumers")
            )
        except ImportError:
            self._set_status_error(t("sync.msal_not_installed"))
            return False
        except Exception as e:
            self._set_status_error(t("sync.could_not_start_onedrive", error=e))
            return False

        message, app, flow, cache, cache_path, token = result

        creds["_msal_cache"]      = cache
        creds["_msal_cache_path"] = cache_path

        if message == "cached":
            # Token refreshed silently — no dialog needed
            creds["_msal_token"] = token
            creds["_msal_app"]   = app  # needed for future token refresh
            return True

        # Try to extract the URL from the message and open it
        import re
        url_match = re.search(r'https://\S+', message)
        if url_match:
            try:
                webbrowser.open(url_match.group())
            except Exception:
                pass

        creds["_msal_app"]  = app
        creds["_msal_flow"] = flow

        msg_box = QMessageBox(self)
        msg_box.setWindowModality(Qt.WindowModality.ApplicationModal)
        msg_box.setWindowTitle(t("sync.onedrive_signin_title"))
        msg_box.setIcon(QMessageBox.Icon.Information)
        msg_box.setText(t("sync.onedrive_signin_text", message=message))
        msg_box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if msg_box.exec() != QMessageBox.StandardButton.Ok:
            self._set_status_error(t("sync.onedrive_auth_cancelled"))
            return False

        return True

    def _preauth_google(self, creds: dict) -> bool:
        """Google OAuth pre-auth. Two modes:
        - oauth_simple: embedded credentials — no pre-auth needed, worker does everything
        - oauth (advanced): user provides client_secret.json — validate file exists
        """
        # Check required packages first — fail fast with a clear message
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
            from googleapiclient.discovery import build  # noqa: F401
        except ImportError:
            self._set_status_error(t("sync.google_packages_missing"))
            return False

        from pathlib import Path
        _default_cfg = os.getenv("APPDATA", "") or str(Path.home() / ".config")
        token_path = Path(creds.get("token_path", os.path.join(
            _default_cfg, "SaveSync", "gdrive_token.json"
        )))
        scopes = ["https://www.googleapis.com/auth/drive.file"]

        # Check if we already have a valid token (no browser needed)
        try:
            from google.oauth2.credentials import Credentials as OAuthCredentials
            from google.auth.transport.requests import Request
            if token_path.exists():
                existing_creds = OAuthCredentials.from_authorized_user_file(str(token_path), scopes)
                if existing_creds and existing_creds.valid:
                    creds["_google_creds"] = existing_creds
                    return True
                if existing_creds and existing_creds.expired and existing_creds.refresh_token:
                    existing_creds.refresh(Request())
                    creds["_google_creds"] = existing_creds
                    return True
        except Exception:
            pass

        if creds.get("method") == "oauth_simple":
            # Simple mode — worker thread handles everything (run_local_server)
            # No pre-auth needed; _connect_oauth() uses embedded credentials
            return True

        # Advanced mode — require client_secret.json
        client_secret = creds.get("client_secret_path", "").strip()
        if not client_secret or not os.path.isfile(client_secret):
            self._set_status_error(t("sync.enter_google_secret"))
            return False
        return True

    def _set_status_error(self, msg: str):
        self._show_status(f"✗  {msg}", palette('error'))

    # ── Sync all ──────────────────────────────────────────────────────────────

    def _sync_all(self):
        orch = get_orchestrator()
        if not orch.is_online():
            self._show_status(t("sync.no_provider"), palette('warning'))
            return
        from core.backup import get_backup_manager
        from core.machine import get_machine_id
        from PySide6.QtWidgets import QMessageBox
        bm = get_backup_manager()
        machine_id = get_machine_id()

        games_to_sync = []
        download_candidates = []  # games with cloud data that differ from local (other machine)

        for entry in get_library().all_games():
            if not entry.save_paths:
                continue
            # Skip truly unchanged synced games
            if entry.sync_status == "synced":
                recents = bm.get_backups_for_game(entry.id)
                if recents:
                    current_hash = (recents[0].cloud_metadata or {}).get("save_hash", "")
                    synced_hash  = entry.cloud_metadata.get("last_synced_hash", "")
                    if current_hash and current_hash == synced_hash:
                        logger.debug(f"sync_all: skipping {entry.name!r} (hash unchanged)")
                        continue
                elif not entry._saves_changed_since_sync():
                    logger.debug(f"sync_all: skipping {entry.name!r} (no changes)")
                    continue

            # Detect if this game was last synced from a *different* machine
            # and we haven't yet confirmed this machine's download for it
            cloud_meta = entry.cloud_metadata or {}
            last_machine = cloud_meta.get("last_sync_machine", "")
            confirmed_machines: list = cloud_meta.get("download_confirmed_machines", [])
            if (last_machine and last_machine != machine_id
                    and machine_id not in confirmed_machines):
                download_candidates.append(entry)
            games_to_sync.append(entry)

        if not games_to_sync:
            # Nothing changed — the status line says so instead of staying silent
            self._show_status(t("sync.nothing_to_sync"), palette('success'))
            return

        # If some games have cloud saves from another machine, ask for confirmation
        if download_candidates:
            names = ", ".join(e.name for e in download_candidates[:4])
            if len(download_candidates) > 4:
                names += t("sync.and_others", count=len(download_candidates) - 4)
            msg = QMessageBox(self)
            msg.setWindowTitle(t("sync.cloud_diff_title"))
            msg.setText(t("sync.cloud_diff_body",
                          count=len(download_candidates), names=names))
            msg.setIcon(QMessageBox.Icon.Question)
            yes_btn = msg.addButton(t("sync.cloud_diff_yes"), QMessageBox.ButtonRole.AcceptRole)
            msg.addButton(t("sync.cloud_diff_no"), QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() == yes_btn:
                # User confirmed: mark this machine as having accepted the download
                for entry in download_candidates:
                    cloud_meta = dict(entry.cloud_metadata or {})
                    confirmed = list(cloud_meta.get("download_confirmed_machines", []))
                    if machine_id not in confirmed:
                        confirmed.append(machine_id)
                    cloud_meta["download_confirmed_machines"] = confirmed
                    from core.library import get_library as _gl
                    _gl().update_game_fields(entry.id, cloud_metadata=cloud_meta)
            else:
                # Only upload; remove download candidates from the download direction
                # by syncing remaining without the download-candidate set
                games_to_sync = [e for e in games_to_sync if e not in download_candidates]
                for entry in download_candidates:
                    orch.sync_game(
                        entry.id, entry.name, entry.save_paths,
                        direction="up",
                        exe_path=entry.exe_path,
                        computed_folder_name=entry.computed_folder_name,
                        name_history=list(entry.name_history),
                    )

        for entry in games_to_sync:
            orch.sync_game(
                entry.id, entry.name, entry.save_paths,
                exe_path=entry.exe_path,
                computed_folder_name=entry.computed_folder_name,
                name_history=list(entry.name_history),
            )

    def _on_sync_started(self, game_id: str):
        entry = get_library().get_by_id(game_id)
        name = entry.name if entry else game_id
        self._sync_progress.setText(f"⟳ {t('sync.syncing')} {name}...")
        self._sync_progress.setVisible(True)
        self._sync_all_btn.setEnabled(False)

    def _on_sync_done(self, game_id: str, result):
        self._sync_progress.setVisible(False)
        self._sync_all_btn.setEnabled(True)
        self._refresh_history()

    def _refresh_history(self):
        """One line PER SYNC RUN — never aggregated per game. The orchestrator
        persists the history to disk, so entries survive app restarts."""
        orch = get_orchestrator()
        history = orch.sync_history
        if not history:
            self._history_list.setText(t("sync.no_history"))
            return
        lines = []
        for h in history[:15]:
            entry = get_library().get_by_id(h.get("game_id", ""))
            name = (entry.name if entry
                    else h.get("game_name") or h.get("game_id", "")[:8])
            icon = "✓" if h.get("success") else "✗"
            raw_time = h.get("time", "")
            from core import to_local_dt
            dt = to_local_dt(raw_time)
            if dt is not None:
                time_str = dt.strftime("%d/%m %H:%M")
            else:
                time_str = raw_time[11:16] if len(raw_time) > 16 else raw_time
            up = h.get("files_uploaded", 0)
            down = h.get("files_downloaded", 0)
            detail = f"↑{up} ↓{down}"
            if h.get("success") and not up and not down:
                detail += f"  ·  {t('sync.nothing_to_sync')}"
            lines.append(f"{icon}  {time_str}  {name}  {detail}")
        self._history_list.setText("\n".join(lines))

    # ── Restore saved config on startup ──────────────────────────────────────

    def _load_saved_config(self):
        config = get_config()
        # Select first configured provider in the combo
        pids = config.get("sync_providers", [])
        if pids:
            first_pid = pids[0]
            for i in range(self._provider_combo.count()):
                if self._provider_combo.itemData(i) == first_pid:
                    self._provider_combo.setCurrentIndex(i)
                    break
            # Load and pre-fill credential fields from secure store
            from core.credentials import get_credential_store
            creds = get_credential_store().load_provider(first_pid)
            if creds and self._current_form:
                self._current_form.set_values(creds)

        # Reflect current connection state
        orch = get_orchestrator()
        if orch.is_online():
            provider = orch.provider
            user = getattr(provider, "user_display", "") if provider else ""
            msg  = t("sync.connected_as", user=user) if user else t("sync.connected")
            self._show_status(f"✓  {msg}", palette('success'))
            self._connect_btn.setText(t("sync.disconnect"))
        elif pids:
            # Providers configured but not connected yet — startup load in progress
            self._show_status(f"⟳  {t('sync.connecting')}", palette('warning'))
            self._connect_btn.setEnabled(False)
            self._connect_btn.setText(t("sync.connecting"))

    # ── i18n ──────────────────────────────────────────────────────────────────

    def update_locale(self):
        self._header.setText(t("sync.title"))
        self._prov_group.setTitle(t("sync.provider"))
        self._auto_group.setTitle(t("sync.auto_backup"))
        self._sync_all_btn.setText(t("sync.sync_now"))
        self._history_group.setTitle(t("sync.history"))
        orch = get_orchestrator()
        self._connect_btn.setText(
            t("sync.disconnect") if orch.is_online() else t("sync.connect")
        )
        # Rebuild quick-connect cards with new language
        while self._quick_cards_layout.count():
            item = self._quick_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_quick_connect_cards()
        # Rebuild provider combo with translated names
        current_data = self._provider_combo.currentData()
        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        self._provider_combo.addItem(t("sync.no_provider"), None)
        for p in available_providers():
            self._provider_combo.addItem(p["name"], p["id"])
        for i in range(self._provider_combo.count()):
            if self._provider_combo.itemData(i) == current_data:
                self._provider_combo.setCurrentIndex(i)
                break
        self._provider_combo.blockSignals(False)
        # Rebuild credential form with translated labels, preserving entered values
        pid = self._provider_combo.currentData()
        if pid:
            old_values = self._current_form.get_values() if hasattr(self, '_current_form') and self._current_form and hasattr(self._current_form, 'get_values') else {}
            self._on_provider_changed()
            if old_values and hasattr(self, '_current_form') and self._current_form and hasattr(self._current_form, 'set_values'):
                self._current_form.set_values(old_values)
        # Refresh sync history with new language
        self._refresh_history()
