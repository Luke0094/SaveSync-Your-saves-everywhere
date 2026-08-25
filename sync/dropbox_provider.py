"""
SaveSync - Dropbox Provider
Supports: Dropbox Client (local folder), OAuth PKCE browser, personal token.

OAuth flow design:
  Because the OAuth code must be collected in the UI thread (via QInputDialog),
  sync_page calls start_oauth_flow() before creating the worker, then passes
  the completed code/flow as private creds keys (_oauth_code, _oauth_flow).
  These keys are stripped before persisting to the credential store.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sync.base import SyncProvider, RemoteFile
import i18n

logger = logging.getLogger(__name__)


class DropboxProvider(SyncProvider):
    PROVIDER_ID  = "dropbox"
    DISPLAY_NAME_KEY = 'providers.dropbox'

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._dbx = None

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        # Try to reconnect using stored refresh token
        stored_refresh = self._credentials.get("_refresh_token")
        if stored_refresh:
            app_key = self._credentials.get("app_key", "")
            if app_key:
                try:
                    import dropbox as dbx_module
                    client = dbx_module.Dropbox(
                        oauth2_refresh_token=stored_refresh,
                        app_key=app_key,
                        timeout=self._get_timeout(),
                    )
                    # Validate BEFORE adopting the client: a revoked token
                    # must not leave self._dbx pointing at a dead client
                    # (_ensure_client only checks "is not None").
                    acct = client.users_get_current_account()
                    self._dbx = client
                    self._user_info = {"name": acct.name.display_name, "email": acct.email}
                    self._connected = True
                    return True
                except Exception as e:
                    logger.warning(f"Dropbox refresh token reconnect failed: {e}")

        method = self._credentials.get("method", "local_folder")
        try:
            if method == "local_folder":
                return self._connect_local()
            elif method in ("oauth", "oauth_simple"):
                # Localhost OAuth flow (runs in worker thread — non-blocking for UI)
                if self._credentials.get("_use_localhost_oauth"):
                    return self._connect_oauth_localhost()
                return self._connect_oauth()
            else:
                return self._connect_token()
        except ImportError:
            logger.error("dropbox package not installed — run: pip install dropbox")
            self.last_error = "dropbox package not installed"
            return False
        except Exception as e:
            logger.error(f"Dropbox connect error: {e}")
            self.last_error = str(e)[:120]
            return False

    def _connect_local(self) -> bool:
        from sync.local_provider import LocalProvider
        from pathlib import Path
        path = self._credentials.get("dropbox_folder_path", "")
        if not path:
            raise RuntimeError(i18n.t('dropbox.no_folder_path'))
        if not Path(path).is_dir():
            raise RuntimeError(i18n.t('dropbox.folder_not_found', path=path))
        delegate = LocalProvider({"root_path": path})
        ok = delegate.connect()
        if ok:
            self._local_delegate = delegate
            self._connected = True
            self._user_info = {"name": i18n.t('dropbox.folder_name', path=path)}
        return ok

    def _connect_token(self) -> bool:
        import dropbox
        token = self._credentials.get("access_token", "")
        if not token:
            logger.error("No Dropbox access token provided")
            return False
        client = dropbox.Dropbox(token, timeout=self._get_timeout())
        try:
            acct = client.users_get_current_account()
        except Exception as e:
            # Don't leave self._dbx pointing at an invalid client
            logger.error(f"Dropbox token validation failed: {e}")
            return False
        self._dbx = client
        self._user_info = {"name": acct.name.display_name, "email": acct.email}
        self._connected = True
        return True

    def _connect_oauth(self) -> bool:
        """
        Complete the OAuth PKCE flow.
        Expects _oauth_code and _oauth_flow to be pre-set by sync_page
        (those are populated by the UI dialog before the worker starts).
        """
        import dropbox as dbx_module
        code      = self._credentials.get("_oauth_code", "")
        auth_flow = self._credentials.get("_oauth_flow")

        if not code or auth_flow is None:
            logger.error("Dropbox OAuth: code or flow not provided (UI pre-auth missing)")
            return False

        result = auth_flow.finish(code.strip())
        app_key = self._credentials.get("app_key", "")
        if result.refresh_token and app_key:
            # Use refresh token so the client can auto-refresh when the access token expires
            self._dbx = dbx_module.Dropbox(
                oauth2_refresh_token=result.refresh_token,
                app_key=app_key,
                timeout=self._get_timeout(),
            )
        else:
            self._dbx = dbx_module.Dropbox(oauth2_access_token=result.access_token, timeout=self._get_timeout())
        acct = self._dbx.users_get_current_account()
        self._user_info = {"name": acct.name.display_name, "email": acct.email}
        self._connected = True
        self._persist_refresh_token(result.refresh_token, app_key, "oauth")
        return True

    def _persist_refresh_token(self, refresh_token: str, app_key: str,
                               default_method: str):
        """Persist the OAuth refresh token so reconnection survives a
        restart — shared by both OAuth completion paths."""
        if not refresh_token:
            return
        from core.credentials import get_credential_store
        get_credential_store().save("dropbox", {
            "method": self._credentials.get("method", default_method),
            "app_key": app_key,
            "_refresh_token": refresh_token,
        })
        logger.info("Dropbox refresh token persisted to credential store")

    def _connect_oauth_localhost(self) -> bool:
        """Complete OAuth via localhost redirect (runs in worker thread)."""
        app_key = self._credentials.get("app_key", "")
        if not app_key:
            logger.error("Dropbox OAuth: no app_key provided")
            return False
        try:
            result = self.start_oauth_flow_localhost(
                app_key,
                theme_bg=self._credentials.get("_theme_bg", "#111114"),
                theme_fg=self._credentials.get("_theme_fg", "#e8e8ea"),
                theme_accent=self._credentials.get("_theme_accent", "#76b900"),
                label_success=self._credentials.get("_lbl_success", ""),
                label_close=self._credentials.get("_lbl_close", ""),
                label_failed=self._credentials.get("_lbl_failed", ""),
                label_retry=self._credentials.get("_lbl_retry", ""),
            )
            import dropbox as dbx_module
            if result.refresh_token and app_key:
                self._dbx = dbx_module.Dropbox(
                    oauth2_refresh_token=result.refresh_token,
                    app_key=app_key,
                    timeout=self._get_timeout(),
                )
            else:
                self._dbx = dbx_module.Dropbox(
                    oauth2_access_token=result.access_token,
                    timeout=self._get_timeout(),
                )
            acct = self._dbx.users_get_current_account()
            self._user_info = {"name": acct.name.display_name, "email": acct.email}
            self._connected = True
            self._persist_refresh_token(
                result.refresh_token, self._credentials.get("app_key", ""),
                "oauth_simple")
            return True
        except Exception as e:
            logger.error(f"Dropbox localhost OAuth failed: {e}")
            return False

    # ── Pre-auth helper (called from main thread by sync_page) ───────────────

    @staticmethod
    def start_oauth_flow(app_key: str):
        """
        Start the PKCE OAuth flow and return (authorize_url, flow_object).
        Call this from the main (UI) thread before launching the connect worker.
        """
        import dropbox
        auth_flow = dropbox.DropboxOAuth2FlowNoRedirect(
            app_key, use_pkce=True, token_access_type="offline",
            timeout=DropboxProvider._get_timeout(),
        )
        url = auth_flow.start()
        return url, auth_flow

    @staticmethod
    def start_oauth_flow_localhost(app_key: str, port: int = 53682,
                                   theme_bg: str = "#111114",
                                   theme_fg: str = "#e8e8ea",
                                   theme_accent: str = "#76b900",
                                   label_success: str = "",
                                   label_close: str = "",
                                   label_failed: str = "",
                                   label_retry: str = ""):
        """Start PKCE OAuth with localhost redirect — browser handles everything.

        Spins up a temporary HTTP server on 127.0.0.1:{port}, opens the browser,
        and waits for Dropbox to redirect back with the auth code. Returns the
        completed OAuth result directly, or raises on failure.

        Dropbox enforces EXACT redirect-URI matching against the URIs
        registered on the app key. The default DROPBOX_APP_KEY is rclone's
        public key, whose registered redirect is http://127.0.0.1:53682/ —
        so both the port and the root path here must stay exactly that (an
        arbitrary port like the old 18923 gets rejected at the authorize
        step before the user can even approve).

        Theme colors and labels should be captured in the main thread and passed here.
        """
        import dropbox
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading

        _bg, _fg, _accent = theme_bg, theme_fg, theme_accent
        # These reach a page in the user's browser. The caller passes them
        # already translated; when it does not — a direct call, a test — the
        # fallback goes through i18n rather than being English on the way
        # out, which is what made this the only user-visible text in the
        # project written into the code.
        def _lbl(passed: str, key: str, last_resort: str) -> str:
            if passed:
                return passed
            try:
                from i18n import t as _t
                return _t(key) or last_resort
            except Exception:
                return last_resort      # no app around it: source language

        _lbl_success = _lbl(label_success, "dropbox.oauth_callback_success",
                            "Authorization successful")
        _lbl_close = _lbl(label_close, "dropbox.oauth_callback_close",
                          "You can close this tab.")
        _lbl_failed = _lbl(label_failed, "dropbox.oauth_callback_failed",
                           "Authorization failed")
        _lbl_retry = _lbl(label_retry, "dropbox.oauth_callback_retry",
                          "Please try again.")
        import urllib.parse
        import webbrowser

        redirect_uri = f"http://127.0.0.1:{port}/"
        auth_flow = dropbox.DropboxOAuth2Flow(
            consumer_key=app_key,
            redirect_uri=redirect_uri,
            session={},  # in-memory session
            csrf_token_session_key="dropbox-auth-csrf-token",
            use_pkce=True,
            token_access_type="offline",
            # Bounds the final code→token exchange too — without this a
            # hung token endpoint stalled the connect worker indefinitely.
            timeout=DropboxProvider._get_timeout(),
        )
        auth_url = auth_flow.start()

        # Container for the result captured by the callback handler
        captured = {"code": None, "state": None, "error": None}
        server_ref = {"server": None}

        class _CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                # The redirect lands on the root path ("/?code=…"). Ignore
                # anything without OAuth params (favicon requests, etc.).
                if "code" not in params and "error" not in params:
                    self.send_response(204)
                    self.end_headers()
                    return
                captured["code"] = params.get("code", [None])[0]
                captured["state"] = params.get("state", [None])[0]
                captured["error"] = params.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                from html import escape as _esc
                if captured["code"]:
                    title = _esc(_lbl_success)
                    msg = _esc(_lbl_close)
                else:
                    title = _esc(_lbl_failed)
                    msg = _esc(_lbl_retry)
                # Escape CSS values to prevent injection via theme colors
                safe_bg = _esc(_bg)
                safe_fg = _esc(_fg)
                safe_accent = _esc(_accent)
                html = (f"<html><head><style>"
                        f"body{{font-family:sans-serif;text-align:center;padding:60px 20px;"
                        f"background:{safe_bg};color:{safe_fg};}}"
                        f"h2{{color:{safe_accent};}}"
                        f"</style></head><body>"
                        f"<h2>{title}</h2><p>{msg}</p></body></html>")
                self.wfile.write(html.encode("utf-8"))
                threading.Thread(target=lambda: server_ref["server"].shutdown(), daemon=True).start()

            def log_message(self, *args):
                pass

        # No port fallback: the redirect URI is pinned by the app key's
        # registration, so a different port could never receive the code —
        # better to fail fast with a clear message.
        try:
            server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
        except OSError as e:
            raise RuntimeError(
                f"Port {port} is busy (needed for the Dropbox sign-in "
                f"redirect — it cannot be changed). Close the application "
                f"using it and retry. [{e}]"
            )
        server_ref["server"] = server
        server.timeout = 120

        webbrowser.open(auth_url)

        # Block until callback or timeout (runs in worker thread — UI stays responsive)
        # Auto-shutdown after timeout
        timeout_timer = threading.Timer(120, lambda: server.shutdown())
        timeout_timer.daemon = True
        timeout_timer.start()

        try:
            server.serve_forever()
        finally:
            server.server_close()
            timeout_timer.cancel()

        if captured["error"]:
            raise RuntimeError(f"Dropbox OAuth error: {captured['error']}")
        if not captured["code"]:
            raise RuntimeError(
                "No authorization code received. "
                "The OAuth flow timed out after 120 seconds or was cancelled by the user."
            )

        # Complete the OAuth flow
        query_params = {"code": captured["code"], "state": captured["state"] or ""}
        result = auth_flow.finish(query_params)
        return result

    # ── Disconnect ───────────────────────────────────────────────────────────

    def disconnect(self):
        # Set _connected = False first so in-flight operations on other
        # threads will fail gracefully via _ensure_client().
        self._connected = False
        self._dbx = None
        if hasattr(self, "_local_delegate"):
            try:
                self._local_delegate.disconnect()
            except Exception:
                pass
            del self._local_delegate

    def _ensure_client(self) -> bool:
        """Ensure Dropbox client is connected."""
        if self._dbx is None and not hasattr(self, "_local_delegate"):
            logger.error("Dropbox client not connected")
            return False
        return True

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _to_dbx(self, path: str) -> str:
        return "/" + path.strip("/")

    # ── File operations ──────────────────────────────────────────────────────

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        self.last_list_error = None
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.list_files(remote_folder)
        if not self._ensure_client():
            self.last_list_error = "not connected"
            return []
        try:
            result = self._dbx.files_list_folder(self._to_dbx(remote_folder), recursive=True)
            files = []
            while True:
                for entry in result.entries:
                    if hasattr(entry, "server_modified"):
                        # Strip leading "/" and re-prefix with remote_folder
                        rel = entry.path_display.lstrip("/")
                        # Normalize datetime to naive for comparison
                        mod_at = entry.server_modified
                        if hasattr(mod_at, 'tzinfo') and mod_at.tzinfo is not None:
                            from datetime import timezone
                            mod_at = mod_at.astimezone(timezone.utc).replace(tzinfo=None)
                        files.append(RemoteFile(
                            path=rel,
                            modified_at=mod_at,
                            size_bytes=entry.size,
                            checksum=None,  # Dropbox content_hash is incompatible with MD5
                        ))
                if not result.has_more:
                    break
                result = self._dbx.files_list_folder_continue(result.cursor)
            return files
        except Exception as e:
            logger.error(f"Dropbox list error: {e}")
            # A missing folder ("not_found") is a legitimate empty listing
            if "not_found" not in str(e):
                self.last_list_error = str(e)[:120]
            return []

    _UPLOAD_CHUNK_SIZE = 128 * 1024 * 1024  # 128 MB — Dropbox chunk limit
    _UPLOAD_SESSION_THRESHOLD = 4 * 1024 * 1024  # 4 MB — use upload sessions for larger files

    def upload(self, local_path: Path, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.upload(local_path, remote_path)
        if not self._ensure_client():
            return False
        try:
            import dropbox
            file_size = local_path.stat().st_size
            dest = self._to_dbx(remote_path)
            mode = dropbox.files.WriteMode("overwrite")

            if file_size <= self._UPLOAD_SESSION_THRESHOLD:
                # Small file (<=4MB): single upload
                with open(local_path, "rb") as f:
                    self._dbx.files_upload(
                        f.read(),
                        dest,
                        mode=mode,
                    )
            else:
                # Large file: chunked upload session
                session = None
                with open(local_path, "rb") as f:
                    chunk = f.read(self._UPLOAD_CHUNK_SIZE)
                    session = self._dbx.files_upload_session_start(chunk)
                    cursor = dropbox.files.UploadSessionCursor(
                        session_id=session.session_id, offset=f.tell()
                    )
                    commit = dropbox.files.CommitInfo(path=dest, mode=mode)
                    try:
                        while True:
                            chunk = f.read(self._UPLOAD_CHUNK_SIZE)
                            if not chunk:
                                # No more data — finish the session with empty bytes
                                self._dbx.files_upload_session_finish(b"", cursor, commit)
                                break
                            # Check if this is the last chunk (we've read to or past EOF)
                            is_last = f.tell() >= file_size
                            if is_last:
                                # Last chunk — finish the session with its data
                                self._dbx.files_upload_session_finish(chunk, cursor, commit)
                                break
                            # Intermediate chunk — append and continue
                            self._dbx.files_upload_session_append_v2(chunk, cursor)
                            cursor.offset = f.tell()
                        session = None  # Mark as finished AFTER successful completion
                    except Exception:
                        # Abandon the session: it expires server-side on its
                        # own. NEVER "close" it via files_upload_session_finish
                        # — finish COMMITS, so it would overwrite the remote
                        # file with the truncated partial upload.
                        session = None
                        raise
            return True
        except Exception as e:
            logger.error(f"Dropbox upload error: {e}")
            return False

    def download(self, remote_path: str, local_path: Path) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.download(remote_path, local_path)
        if not self._ensure_client():
            return False
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic download: write to tmp then rename
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            try:
                self._dbx.files_download_to_file(str(tmp_path), self._to_dbx(remote_path))
                tmp_path.replace(local_path)
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            return True
        except Exception as e:
            logger.error(f"Dropbox download error: {e}")
            return False

    def delete_remote(self, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.delete_remote(remote_path)
        if not self._ensure_client():
            return False
        try:
            self._dbx.files_delete_v2(self._to_dbx(remote_path))
            return True
        except Exception as e:
            # If the file doesn't exist, treat deletion as successful
            err_str = str(e).lower()
            if "not_found" in err_str or "path/not_found" in err_str:
                return True
            logger.error(f"Dropbox delete error for {remote_path}: {e}")
            return False

    def remote_exists(self, remote_path: str) -> bool:
        return self.get_remote_metadata(remote_path) is not None

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.get_remote_metadata(remote_path)
        if not self._ensure_client():
            return None
        try:
            import dropbox as dbx_module
            meta = self._dbx.files_get_metadata(self._to_dbx(remote_path))
            # Handle FolderMetadata: return a valid RemoteFile with sensible defaults
            if isinstance(meta, dbx_module.files.FolderMetadata):
                return RemoteFile(
                    path=remote_path,
                    modified_at=datetime.min,
                    size_bytes=0,
                    checksum=None,
                )
            mod_at = meta.server_modified
            if hasattr(mod_at, 'tzinfo') and mod_at.tzinfo is not None:
                from datetime import timezone
                mod_at = mod_at.astimezone(timezone.utc).replace(tzinfo=None)
            return RemoteFile(
                path=remote_path,
                modified_at=mod_at,
                size_bytes=meta.size,
                checksum=None,  # Dropbox content_hash is incompatible with MD5
            )
        except Exception:
            return None

    # ── Credential schema ────────────────────────────────────────────────────

    @classmethod
    def credential_fields(cls) -> list[dict]:
        from sync.app_credentials import DROPBOX_APP_KEY
        methods = [
            {"value": "local_folder", "label": i18n.t('dropbox.local_folder_recommended')},
            {"value": "token",        "label": i18n.t('dropbox.personal_access_token')},
        ]
        if DROPBOX_APP_KEY:
            methods.insert(1, {"value": "oauth_simple", "label": i18n.t('dropbox.signin_simple')})
        methods.append({"value": "oauth", "label": i18n.t('dropbox.oauth_advanced')})

        return [
            {
                "id": "method",
                "label": i18n.t('dropbox.connection_method'),
                "type": "select",
                "options": methods,
                "required": True,
                "hint": i18n.t('dropbox.local_folder_hint'),
            },
            {
                "id": "dropbox_folder_path",
                "label": i18n.t('dropbox.local_folder'),
                "type": "folder",
                "required": True,
                "depends_on": {"method": "local_folder"},
                "hint": i18n.t('dropbox.select_folder_hint'),
            },
            {
                "id": "_oauth_guide",
                "type": "guide",
                "depends_on": {"method": "oauth"},
                "steps": [
                    i18n.t('dropbox.guide_step1'),
                    i18n.t('dropbox.guide_step2'),
                    i18n.t('dropbox.guide_step3'),
                ],
                "portal_url": "https://www.dropbox.com/developers/apps",
                "portal_label": i18n.t('dropbox.open_dev_console'),
            },
            {
                "id": "app_key",
                "label": i18n.t('dropbox.app_key'),
                "type": "text",
                "required": False,
                "depends_on": {"method": "oauth"},
                "hint": i18n.t('dropbox.app_key_hint'),
            },
            {
                "id": "_token_guide",
                "type": "guide",
                "depends_on": {"method": "token"},
                "steps": [
                    i18n.t('dropbox.token_step1'),
                    i18n.t('dropbox.token_step2'),
                    i18n.t('dropbox.token_step3'),
                ],
                "portal_url": "https://www.dropbox.com/developers/apps",
                "portal_label": i18n.t('dropbox.open_dev_console'),
            },
            {
                "id": "access_token",
                "label": i18n.t('dropbox.access_token'),
                "type": "password",
                "required": False,
                "depends_on": {"method": "token"},
                "hint": i18n.t('dropbox.token_hint'),
            },
        ]
