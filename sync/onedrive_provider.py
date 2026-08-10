"""
SaveSync - Microsoft OneDrive Provider
Supports: OneDrive client (local folder), MSAL device-code OAuth, personal Graph token.

Device-code OAuth design:
  Because device-code flow requires showing a URL+code to the user (UI thread),
  then blocking while polling (must run in worker thread), sync_page performs the
  pre-auth step (initiate_device_flow) in the main thread, shows a dialog, then
  passes the msal app/flow objects as private creds keys (_msal_app, _msal_flow).
  These are stripped before persisting to the credential store.
"""
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sync.base import SyncProvider, RemoteFile, restrict_file_acl as _restrict_file_acl
import i18n

logger = logging.getLogger(__name__)


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# NOTE: never list "offline_access" here — MSAL reserves it (adds it by
# itself for refresh tokens) and raises ValueError on any reserved scope,
# which would kill every device-flow/silent call before the user even
# sees a sign-in code.
_SCOPES     = ["Files.ReadWrite", "User.Read"]


class OneDriveProvider(SyncProvider):
    PROVIDER_ID  = "onedrive"
    DISPLAY_NAME_KEY = 'onedrive.display_name'

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._token: Optional[str] = None
        self._token_expires_at: Optional[float] = None
        # Separate from expiry: after a failed MSAL refresh, stay False until
        # this time so callers do not treat the dead token as "session ready".
        self._refresh_backoff_until: Optional[float] = None
        self._msal_app = None
        self._msal_cache = None
        self._msal_cache_path: str = ""
        self._session = None
        self._refresh_lock = threading.Lock()

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        method = self._credentials.get("method", "local_folder")
        try:
            if method == "local_folder":
                return self._connect_local()
            elif method == "personal_token":
                return self._connect_token()
            else:  # "oauth" or "oauth_simple"
                return self._connect_oauth()
        except ImportError as e:
            logger.error(f"Missing dependency for OneDrive OAuth: {e} — run: pip install msal requests")
            self.last_error = "msal/requests not installed"
            return False
        except Exception as e:
            logger.error(f"OneDrive connect error: {e}")
            self.last_error = str(e)[:120]
            return False

    def _connect_local(self) -> bool:
        from sync.local_provider import LocalProvider
        from pathlib import Path
        path = self._credentials.get("onedrive_folder_path", "")
        if not path:
            raise RuntimeError(i18n.t('onedrive.no_folder_path'))
        if not Path(path).is_dir():
            raise RuntimeError(i18n.t('onedrive.folder_not_found', path=path))
        delegate = LocalProvider({"root_path": path})
        ok = delegate.connect()
        if ok:
            self._local_delegate = delegate
            self._connected = True
            self._user_info = {"name": f"OneDrive Folder ({path})"}
        return ok

    def _connect_token(self) -> bool:
        self._token = self._credentials.get("access_token", "")
        if not self._token:
            logger.error("No OneDrive access token provided")
            self.last_error = "no access token provided"
            return False
        self._setup_session()
        # Set a default expiry so _ensure_session() can detect when the token
        # is stale (personal tokens typically expire in 1 hour).
        import time
        self._token_expires_at = time.time() + 3600
        self._refresh_backoff_until = None
        # Validate token by fetching user info
        if not self._validate_token():
            logger.error("OneDrive access token is invalid or expired")
            # Close the session created by _setup_session to avoid resource leak
            if hasattr(self, '_session') and self._session:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            self._token_expires_at = None
            return False
        self._connected = True
        return True

    def _validate_token(self) -> bool:
        """Validate the current token by making a test API call."""
        try:
            r = self._session.get(f"{_GRAPH_BASE}/me", timeout=self._get_timeout())
            if r.ok:
                data = r.json()
                self._user_info = {
                    "name": data.get("displayName", ""),
                    "email": data.get("userPrincipalName", ""),
                }
                return True
            logger.warning(f"Token validation failed: HTTP {r.status_code}")
            return False
        except Exception as e:
            logger.warning(f"Token validation error: {e}")
            return False

    def _connect_oauth(self) -> bool:
        """
        Complete the MSAL device-code flow.

        Expects _msal_app and _msal_flow pre-set by sync_page (main thread).
        If a fresh token is already available via cache (_msal_token), uses it directly.
        Falls back to a new device-code flow if no pre-auth state is present.
        """
        import msal

        preauth_token      = self._credentials.get("_msal_token")
        preauth_app        = self._credentials.get("_msal_app")
        preauth_flow       = self._credentials.get("_msal_flow")
        preauth_cache      = self._credentials.get("_msal_cache")
        preauth_cache_path = self._credentials.get("_msal_cache_path", "")

        def _save_cache(cache):
            if cache and preauth_cache_path:
                try:
                    # Validate cache path is within user data directories
                    cache_dir = os.path.dirname(os.path.abspath(preauth_cache_path))
                    appdata = os.getenv("APPDATA", "")
                    home = str(Path.home())
                    # Normalize case on Windows, guard against empty strings
                    cache_dir_n = os.path.normcase(cache_dir)
                    if not ((appdata and cache_dir_n.startswith(os.path.normcase(appdata))) or
                            (home and cache_dir_n.startswith(os.path.normcase(home)))):
                        logger.warning(f"MSAL cache path outside user directory, skipping: {cache_dir}")
                        return
                    os.makedirs(cache_dir, exist_ok=True)
                    fd = os.open(preauth_cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as _f:
                        _f.write(cache.serialize())
                    _restrict_file_acl(preauth_cache_path)
                except Exception as e:
                    logger.warning(f"Failed to save MSAL token cache: {e}")

        def _store_msal_state(app, cache, cache_path):
            """Store MSAL app/cache so token can be refreshed later."""
            self._msal_app = app
            self._msal_cache = cache
            self._msal_cache_path = cache_path

        def _store_token_expiry(result_dict):
            """Store token expiry from MSAL result (expires_in seconds)."""
            import time
            expires_in = result_dict.get("expires_in")
            if expires_in:
                self._token_expires_at = time.time() + int(expires_in)
            self._refresh_backoff_until = None

        # Case 1: cached token already refreshed by sync_page
        if preauth_token:
            import time
            if isinstance(preauth_token, dict):
                # Full MSAL result: honor the REAL remaining lifetime. A
                # silently-served cached token may have far less than 1h
                # left — assuming 3600 opened a 401 window between the true
                # expiry and the refresh trigger.
                self._token = preauth_token.get("access_token", "")
                _store_token_expiry(preauth_token)
                if not self._token_expires_at:
                    self._token_expires_at = time.time() + 3600
            else:
                self._token = preauth_token
                self._token_expires_at = time.time() + 3600
                self._refresh_backoff_until = None
            _save_cache(preauth_cache)
            _store_msal_state(preauth_app, preauth_cache, preauth_cache_path)
            self._setup_session()
            self._fetch_user_info()
            self._connected = True
            return True

        # Case 2: device flow already initiated by sync_page — just poll (blocks)
        if preauth_app and preauth_flow:
            result = preauth_app.acquire_token_by_device_flow(preauth_flow)
            if "access_token" not in result:
                logger.error(f"OneDrive device flow failed: {result.get('error_description')}")
                return False
            self._token = result["access_token"]
            _store_token_expiry(result)
            _save_cache(preauth_cache)
            _store_msal_state(preauth_app, preauth_cache, preauth_cache_path)
            self._setup_session()
            self._fetch_user_info()
            self._connected = True
            return True

        # Case 3: legacy fallback (no preauth — only reached if flow changed)
        logger.warning("OneDrive OAuth: no pre-auth state — attempting token cache only")
        client_id = self._credentials.get("client_id", "")
        tenant    = self._credentials.get("tenant", "consumers")
        authority = f"https://login.microsoftonline.com/{tenant}"
        _default_cache_dir = os.getenv("APPDATA", "") or str(Path.home() / ".config")
        token_cache_path = preauth_cache_path or os.path.join(
            _default_cache_dir, "SaveSync", "onedrive_cache.bin"
        )
        cache = msal.SerializableTokenCache()
        if os.path.exists(token_cache_path):
            with open(token_cache_path) as _f:
                cache.deserialize(_f.read())
        app      = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
        accounts = app.get_accounts()
        result   = None
        if accounts:
            result = app.acquire_token_silent(_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._token = result["access_token"]
            _store_token_expiry(result)
            _save_cache(cache)
            _store_msal_state(app, cache, token_cache_path)
            self._setup_session()
            self._fetch_user_info()
            self._connected = True
            return True

        logger.error("OneDrive OAuth: no cached token available and no device flow initiated")
        self.last_error = "no cached token and no device flow"
        return False

    # ── Pre-auth helper (called from main thread by sync_page) ───────────────

    @staticmethod
    def start_device_flow(client_id: str, tenant: str = "consumers"):
        """
        Initiate the MSAL device-code flow in the main thread.
        Returns (message, msal_app, flow_or_none, cache, cache_path,
        result_dict_or_none) — the last element is the full MSAL result
        (with access_token + expires_in) when a cached token was served.
        Call this in the UI thread; then pass app/flow to the connect worker.
        """
        import msal
        authority = f"https://login.microsoftonline.com/{tenant}"
        _default_cache_dir = os.getenv("APPDATA", "") or str(Path.home() / ".config")
        token_cache_path = os.path.join(
            _default_cache_dir, "SaveSync", "onedrive_cache.bin"
        )
        cache = msal.SerializableTokenCache()
        if os.path.exists(token_cache_path):
            with open(token_cache_path) as _f:
                cache.deserialize(_f.read())

        app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)

        # Try silent refresh first. Return the FULL result dict (not just the
        # token string) so connect() can honor the real expires_in of a
        # cached token instead of assuming a fresh 1-hour lifetime.
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(_SCOPES, account=accounts[0])
            if result and "access_token" in result:
                return "cached", app, None, cache, token_cache_path, result

        flow = app.initiate_device_flow(scopes=_SCOPES)
        if "error" in flow:
            raise RuntimeError(flow.get("error_description", "device flow initiation failed"))

        return flow.get("message", ""), app, flow, cache, token_cache_path, None

    # ── Session helpers ──────────────────────────────────────────────────────

    def _setup_session(self):
        import requests
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self._token}"})

    def _fetch_user_info(self):
        try:
            r = self._session.get(f"{_GRAPH_BASE}/me", timeout=self._get_timeout())
            if r.ok:
                data = r.json()
                self._user_info = {
                    "name":  data.get("displayName", ""),
                    "email": data.get("userPrincipalName", ""),
                }
        except Exception:
            pass

    def _graph_path(self, remote_path: str) -> str:
        from urllib.parse import quote
        clean = remote_path.strip("/")
        # URL-encode each path component individually to preserve "/" separators
        encoded = "/".join(quote(part, safe="") for part in clean.split("/"))
        return f"{_GRAPH_BASE}/me/drive/root:/{encoded}"

    # ── Disconnect ───────────────────────────────────────────────────────────

    def disconnect(self):
        # Set _connected = False first so in-flight operations on other
        # threads will fail gracefully via _ensure_session().
        self._connected = False
        # Acquire _refresh_lock to avoid racing with _ensure_session which
        # reads/writes _session and _token under this lock.
        with self._refresh_lock:
            self._token = None
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
            self._session = None
        if hasattr(self, "_local_delegate"):
            try:
                self._local_delegate.disconnect()
            except Exception:
                pass
            del self._local_delegate

    # ── File operations ──────────────────────────────────────────────────────

    def _ensure_session(self) -> bool:
        if self._session is None and not hasattr(self, "_local_delegate"):
            logger.error("OneDrive session not connected")
            return False
        # personal_token mode has NO refresh path (a raw Graph token carries
        # no refresh token): once expired, fail fast with a clear reason
        # instead of letting every operation silently 401 forever.
        if (self._token_expires_at is not None and self._msal_app is None
                and not hasattr(self, "_local_delegate")):
            import time as _t
            if _t.time() >= self._token_expires_at:
                if self._connected:   # log/flag once per expiry
                    logger.error(
                        "OneDrive personal access token expired (~1h lifetime, "
                        "no refresh possible) — re-authenticate from Settings")
                    self.last_error = "access token expired — re-authenticate"
                    self._connected = False
                return False
        # Refresh token if expired (thread-safe)
        if self._token_expires_at is not None and self._msal_app is not None:
            with self._refresh_lock:
                import time
                now = time.time()
                # Failed refresh → refuse the session until backoff ends.
                # Do NOT push _token_expires_at forward: that made the
                # pre-refresh window look healthy and returned True with a
                # token that had just failed.
                if (self._refresh_backoff_until is not None
                        and now < self._refresh_backoff_until):
                    return False
                if now >= self._token_expires_at - 300:  # refresh 5 min before expiry
                    try:
                        accounts = self._msal_app.get_accounts()
                        if accounts:
                            result = self._msal_app.acquire_token_silent(
                                _SCOPES, account=accounts[0])
                            if result and "access_token" in result:
                                new_token = result["access_token"]
                                expires_in = result.get("expires_in")
                                # Build new session BEFORE swapping references
                                # so in-flight requests on the old session are
                                # not affected by the token update.
                                import requests as _req
                                new_session = _req.Session()
                                new_session.headers.update(
                                    {"Authorization": f"Bearer {new_token}"})
                                old_session = self._session
                                self._token = new_token
                                self._session = new_session
                                if expires_in:
                                    self._token_expires_at = time.time() + int(expires_in)
                                self._refresh_backoff_until = None
                                if old_session is not None:
                                    try:
                                        old_session.close()
                                    except Exception:
                                        pass
                                if self._msal_cache and self._msal_cache_path:
                                    try:
                                        cache_dir = os.path.dirname(
                                            os.path.abspath(self._msal_cache_path))
                                        os.makedirs(cache_dir, exist_ok=True)
                                        fd = os.open(
                                            self._msal_cache_path,
                                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                            0o600)
                                        with os.fdopen(fd, "w") as _f:
                                            _f.write(self._msal_cache.serialize())
                                        _restrict_file_acl(self._msal_cache_path)
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to save refreshed MSAL cache: {e}")
                                logger.info("OneDrive token refreshed successfully")
                            else:
                                logger.error(
                                    "OneDrive token refresh failed: "
                                    "no access token in response")
                                self._refresh_backoff_until = time.time() + 60
                                self.last_error = "token refresh failed"
                                return False
                        else:
                            logger.error(
                                "OneDrive token refresh failed: no accounts found")
                            self._refresh_backoff_until = time.time() + 60
                            self.last_error = "token refresh failed — no accounts"
                            return False
                    except Exception as e:
                        logger.error(f"OneDrive token refresh failed: {e}")
                        self._refresh_backoff_until = time.time() + 60
                        self.last_error = f"token refresh failed: {e}"
                        return False
        return True

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        self.last_list_error = None
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.list_files(remote_folder)
        if not self._ensure_session():
            self.last_list_error = "not connected"
            return []
        try:
            return self._list_recursive(remote_folder)
        except Exception as e:
            logger.error(f"OneDrive list error: {e}")
            # 404 on the folder itself is a legitimate empty, not an error
            if "404" not in str(e) and "itemNotFound" not in str(e):
                self.last_list_error = str(e)[:120]
            return []

    def _list_recursive(self, remote_folder: str, depth: int = 0, max_depth: int = 15) -> list[RemoteFile]:
        """Recursively list all files under a OneDrive folder.

        Re-validates the session before each page request to handle token
        expiry during large directory traversals.
        """
        if depth >= max_depth:
            logger.warning(f"OneDrive _list_recursive: max depth {max_depth} reached at {remote_folder}")
            return []
        url = f"{self._graph_path(remote_folder)}:/children"
        files = []
        while url:
            # Re-validate session before each page to handle token expiry
            if not self._ensure_session():
                logger.error("OneDrive session lost during listing")
                break
            r = self._session.get(url, timeout=self._get_timeout())
            if not r.ok:
                break
            data = r.json()
            for item in data.get("value", []):
                child_path = f"{remote_folder}/{item['name']}"
                if "folder" in item:
                    files.extend(self._list_recursive(child_path, depth + 1, max_depth))
                    continue
                dt = datetime.fromisoformat(
                    item["lastModifiedDateTime"].replace("Z", "+00:00")
                )
                if dt.tzinfo is not None:
                    from datetime import timezone
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                files.append(RemoteFile(
                    path=child_path,
                    modified_at=dt,
                    size_bytes=item.get("size", 0),
                    checksum=None,
                ))
            url = data.get("@odata.nextLink")
        return files

    _UPLOAD_SESSION_THRESHOLD = 4 * 1024 * 1024   # 4 MB — Graph API simple upload limit

    def upload(self, local_path: Path, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.upload(local_path, remote_path)
        if not self._ensure_session():
            return False
        try:
            file_size = local_path.stat().st_size
            if file_size <= self._UPLOAD_SESSION_THRESHOLD:
                return self._upload_simple(local_path, remote_path)
            else:
                return self._upload_resumable(local_path, remote_path, file_size)
        except Exception as e:
            logger.error(f"OneDrive upload error: {e}")
            return False

    def _upload_simple(self, local_path: Path, remote_path: str) -> bool:
        """PUT upload — works for files up to 4 MB."""
        url = f"{self._graph_path(remote_path)}:/content"
        with open(local_path, "rb") as f:
            r = self._session.put(url, data=f, timeout=self._get_timeout())
        if not r.ok:
            logger.error(f"OneDrive simple upload failed ({r.status_code}): {r.text[:200]}")
        return r.ok

    def _upload_resumable(self, local_path: Path, remote_path: str, file_size: int) -> bool:
        """Create an upload session for large files (> 4 MB)."""
        # 1. Create upload session
        session_url = f"{self._graph_path(remote_path)}:/createUploadSession"
        r = self._session.post(session_url, json={
            "item": {"@microsoft.graph.conflictBehavior": "replace"}
        }, timeout=self._get_timeout())
        if not r.ok:
            logger.error(f"OneDrive upload session failed ({r.status_code}): {r.text[:200]}")
            return False

        upload_url = r.json().get("uploadUrl")
        if not upload_url:
            return False

        # 2. Upload in 10 MB chunks
        chunk_size = 10 * 1024 * 1024
        offset = 0
        # Use a dedicated session for chunk uploads WITHOUT the Authorization
        # header. Microsoft Graph API explicitly forbids sending Bearer tokens
        # to upload session URLs (causes 401 Unauthorized).
        import requests as _requests
        upload_session = _requests.Session()
        try:
            with open(local_path, "rb") as f:
                while offset < file_size:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    end = offset + len(chunk) - 1
                    headers = {
                        "Content-Range": f"bytes {offset}-{end}/{file_size}",
                        "Content-Length": str(len(chunk)),
                    }
                    from core.config_manager import get_config
                    _sync_timeout = int(get_config().get("sync_timeout", 120))
                    cr = upload_session.put(upload_url, data=chunk, headers=headers, timeout=_sync_timeout)
                    if cr.status_code not in (200, 201, 202):
                        logger.error(f"OneDrive chunk upload failed ({cr.status_code}): {cr.text[:200]}")
                        # Cancel the upload session to free server resources
                        try:
                            upload_session.delete(upload_url, timeout=10)
                        except Exception:
                            pass
                        return False
                    offset += len(chunk)
            return True
        except Exception as e:
            logger.error(f"OneDrive resumable upload error: {e}")
            # Cancel the upload session to free server resources
            try:
                upload_session.delete(upload_url, timeout=10)
            except Exception:
                pass
            return False
        finally:
            upload_session.close()

    def download(self, remote_path: str, local_path: Path) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.download(remote_path, local_path)
        if not self._ensure_session():
            return False
        try:
            url = f"{self._graph_path(remote_path)}:/content"
            r   = self._session.get(url, stream=True, timeout=self._get_timeout())
            try:
                if not r.ok:
                    return False
                local_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic download: write to tmp then rename
                tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    tmp_path.replace(local_path)
                except Exception:
                    if tmp_path.exists():
                        tmp_path.unlink()
                    raise
                return True
            finally:
                r.close()
        except Exception as e:
            logger.error(f"OneDrive download error: {e}")
            return False

    def delete_remote(self, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.delete_remote(remote_path)
        if not self._ensure_session():
            return False
        try:
            r = self._session.delete(self._graph_path(remote_path), timeout=self._get_timeout())
            return r.ok or r.status_code == 404
        except Exception:
            return False

    def remote_exists(self, remote_path: str) -> bool:
        return self.get_remote_metadata(remote_path) is not None

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.get_remote_metadata(remote_path)
        if not self._ensure_session():
            return None
        try:
            r = self._session.get(self._graph_path(remote_path), timeout=self._get_timeout())
            if not r.ok:
                return None
            item = r.json()
            try:
                dt = datetime.fromisoformat(
                    item["lastModifiedDateTime"].replace("Z", "+00:00")
                )
                if dt.tzinfo is not None:
                    from datetime import timezone
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except (KeyError, ValueError):
                dt = datetime.min
            return RemoteFile(
                path=remote_path,
                modified_at=dt,
                size_bytes=item.get("size", 0),
                checksum=None,  # OneDrive SHA1/quickXorHash is incompatible with MD5
            )
        except Exception:
            return None

    # ── Credential schema ────────────────────────────────────────────────────

    @classmethod
    def credential_fields(cls) -> list[dict]:
        from sync.app_credentials import ONEDRIVE_CLIENT_ID
        methods = [
            {"value": "local_folder",   "label": i18n.t('onedrive.local_folder_recommended')},
            {"value": "personal_token", "label": i18n.t('onedrive.graph_api_token')},
        ]
        # Only show simple OAuth if default credentials are configured
        if ONEDRIVE_CLIENT_ID:
            methods.insert(1, {"value": "oauth_simple", "label": i18n.t('onedrive.signin_simple')})
        methods.append({"value": "oauth", "label": i18n.t('onedrive.signin_advanced')})

        return [
            {
                "id": "method",
                "label": i18n.t('onedrive.connection_method'),
                "type": "select",
                "options": methods,
                "required": True,
                "hint": i18n.t('onedrive.local_folder_hint'),
            },
            {
                "id": "onedrive_folder_path",
                "label": i18n.t('onedrive.local_folder'),
                "type": "folder",
                "required": True,
                "depends_on": {"method": "local_folder"},
                "hint": i18n.t('onedrive.select_folder_hint'),
            },
            {
                "id": "_oauth_guide",
                "type": "guide",
                "depends_on": {"method": "oauth"},
                "steps": [
                    i18n.t('onedrive.guide_step1'),
                    i18n.t('onedrive.guide_step2'),
                    i18n.t('onedrive.guide_step3'),
                    i18n.t('onedrive.guide_step4'),
                ],
                "portal_url": "https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
                "portal_label": i18n.t('onedrive.open_azure_portal'),
            },
            {
                "id": "client_id",
                "label": i18n.t('onedrive.azure_app_client_id'),
                "type": "text",
                "required": False,
                "depends_on": {"method": "oauth"},
                "hint": i18n.t('onedrive.azure_app_hint'),
            },
            {
                "id": "_token_guide",
                "type": "guide",
                "depends_on": {"method": "personal_token"},
                "steps": [
                    i18n.t('onedrive.token_step1'),
                    i18n.t('onedrive.token_step2'),
                    i18n.t('onedrive.token_step3'),
                ],
                "portal_url": "https://developer.microsoft.com/en-us/graph/graph-explorer",
                "portal_label": i18n.t('onedrive.open_graph_explorer'),
            },
            {
                "id": "access_token",
                "label": i18n.t('onedrive.graph_access_token'),
                "type": "password",
                "required": False,
                "depends_on": {"method": "personal_token"},
                "hint": i18n.t('onedrive.graph_token_hint'),
            },
        ]
