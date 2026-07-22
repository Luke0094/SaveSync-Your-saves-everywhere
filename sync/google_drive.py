"""
SaveSync - Google Drive Provider
Supports: Drive for Desktop (local folder), OAuth browser login, Service Account JSON.
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


_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_APP_FOLDER = "SaveSync"


def _escape_drive_query(name: str) -> str:
    """Escape a value for use INSIDE single quotes in a Drive API query.

    Within a quoted string the query language treats only backslash and
    single-quote as special — escaping those is sufficient AND necessary:
    the old extra stripping of ``(){}`` made a file uploaded as
    ``save (1).zip`` unfindable by every subsequent query (the query
    searched for a name that didn't exist)."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveProvider(SyncProvider):
    PROVIDER_ID = "google_drive"
    DISPLAY_NAME_KEY = 'google_drive.display_name'

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._service = None
        self._folder_cache: dict[str, str] = {}  # path -> file_id
        self._cache_lock = threading.Lock()  # protects _folder_cache
        self._auth_lock = threading.Lock()   # protects credential refresh

    def _build_service(self, creds):
        """Drive client with a real socket timeout.

        googleapiclient's default httplib2 transport has NO timeout, so a
        half-open connection would hang the sync worker forever. Passing an
        AuthorizedHttp built on httplib2.Http(timeout=…) closes that hole;
        if google_auth_httplib2 is unavailable, fall back to the default
        transport rather than failing the connect.
        """
        from googleapiclient.discovery import build
        try:
            import httplib2
            import google_auth_httplib2
            authed = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=self._get_timeout()))
            return build("drive", "v3", http=authed, cache_discovery=False)
        except ImportError:
            logger.warning("google_auth_httplib2 missing — Drive calls run without timeout")
            return build("drive", "v3", credentials=creds, cache_discovery=False)

    def connect(self) -> bool:
        method = self._credentials.get("method", "oauth")
        try:
            if method == "local_folder":
                return self._connect_local_folder()
            elif method == "service_account":
                return self._connect_service_account()
            else:
                return self._connect_oauth()
        except ImportError:
            logger.error("google-api-python-client not installed")
            self.last_error = "google-api-python-client not installed"
            return False
        except Exception as e:
            logger.error(f"Google Drive connect error: {e}")
            self.last_error = str(e)[:120]
            return False

    def _connect_oauth(self) -> bool:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials as OAuthCredentials

        # Case 0: pre-authenticated credentials from sync_page
        preauth_creds = self._credentials.get("_google_creds")
        if preauth_creds:
            if preauth_creds.expired and preauth_creds.refresh_token:
                preauth_creds.refresh(Request())
            self._service = self._build_service(preauth_creds)
            self._oauth_creds = preauth_creds
            profile = self._service.about().get(fields="user").execute()
            self._user_info = profile.get("user", {})
            self._user_info["name"] = self._user_info.get("displayName", "")
            self._connected = True
            return True

        cred_path_str = self._credentials.get("client_secret_path", "").strip()
        _default_cfg = os.getenv("APPDATA", "") or str(Path.home() / ".config")
        token_path = Path(self._credentials.get("token_path", os.path.join(
            _default_cfg, "SaveSync", "gdrive_token.json"
        )))

        creds = None
        if token_path.exists():
            try:
                creds = OAuthCredentials.from_authorized_user_file(str(token_path), _SCOPES)
            except Exception as e:
                logger.warning(f"Could not load token file: {e}")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Google OAuth token refresh failed: {e}")
                    self.last_error = "token refresh failed — please re-authenticate"
                    if token_path.exists():
                        try:
                            token_path.unlink()
                            logger.info("Deleted stale token file. Please re-authenticate.")
                        except OSError as ue:
                            logger.warning(f"Could not delete stale token: {ue}")
                    return False
            elif cred_path_str and Path(cred_path_str).is_file():
                flow = InstalledAppFlow.from_client_secrets_file(cred_path_str, _SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                # Try embedded credentials (oauth_simple mode)
                from sync.app_credentials import GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET
                if GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET:
                    client_config = {
                        "installed": {
                            "client_id": GOOGLE_DRIVE_CLIENT_ID,
                            "client_secret": GOOGLE_DRIVE_CLIENT_SECRET,
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "redirect_uris": ["http://localhost"],
                        }
                    }
                    flow = InstalledAppFlow.from_client_config(client_config, _SCOPES)
                    creds = flow.run_local_server(port=0)
                else:
                    logger.error("No OAuth credentials available")
                    self.last_error = "no OAuth credentials available"
                    return False
            token_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(creds.to_json())
            _restrict_file_acl(token_path)

        self._service = self._build_service(creds)
        self._oauth_creds = creds
        profile = self._service.about().get(fields="user").execute()
        self._user_info = profile.get("user", {})
        self._user_info["name"] = self._user_info.get("displayName", "")
        self._connected = True
        return True

    def _connect_service_account(self) -> bool:
        from google.oauth2.service_account import Credentials

        json_path = self._credentials.get("service_account_json", "")
        if not json_path or not Path(json_path).exists():
            self.last_error = "service account JSON file not found"
            return False
        creds = Credentials.from_service_account_file(json_path, scopes=_SCOPES)
        self._service = self._build_service(creds)
        # Validate with a real API call — a bad key or disabled Drive API
        # must fail HERE, not silently on the first upload. (Caveat that
        # can't be checked from here: a service account has no personal
        # Drive quota, so uploads to "root" may still 403 unless a Shared
        # Drive is used.)
        self._service.about().get(fields="user").execute()
        self._user_info = {"name": i18n.t('google_drive.service_account_connected', path=json_path)}
        self._connected = True
        return True

    def _connect_local_folder(self) -> bool:
        """Google Drive for Desktop - treat as local folder."""
        from sync.local_provider import LocalProvider
        drive_path = self._credentials.get("drive_folder_path", "")
        if not drive_path:
            raise RuntimeError(i18n.t('google_drive.no_folder_path'))
        if not Path(drive_path).is_dir():
            raise RuntimeError(i18n.t('google_drive.folder_not_found', path=drive_path))
        delegate = LocalProvider({"root_path": drive_path})
        result = delegate.connect()
        if result:
            self._local_delegate = delegate
            self._connected = True
            self._user_info = {"name": i18n.t('google_drive.desktop_connected', path=drive_path)}
        return result

    def disconnect(self):
        """Disconnect from Google Drive. Does NOT delete the token file;
        re-authentication requires manual token deletion or calling logout().

        Sets _connected = False first so in-flight operations on other threads
        will fail gracefully via _ensure_service() rather than crashing on a
        None service object.
        """
        self._connected = False
        self._service = None
        with self._cache_lock:
            self._folder_cache.clear()
        if hasattr(self, "_local_delegate"):
            try:
                self._local_delegate.disconnect()
            except Exception:
                pass
            del self._local_delegate

    def _ensure_service(self) -> bool:
        """Ensure the Drive service is connected and token is valid.

        Thread-safe: uses _auth_lock to prevent concurrent token refreshes
        from corrupting the credential state.
        """
        if self._service is None and not hasattr(self, "_local_delegate"):
            logger.error("Google Drive service not connected")
            return False
        # Proactive token refresh for OAuth credentials (serialised via lock)
        if self._service and hasattr(self, '_oauth_creds'):
            with self._auth_lock:
                try:
                    if self._oauth_creds.expired and self._oauth_creds.refresh_token:
                        from google.auth.transport.requests import Request
                        self._oauth_creds.refresh(Request())
                        # Rebuild the service with refreshed credentials so
                        # subsequent API calls use the new access token.
                        self._service = self._build_service(self._oauth_creds)
                        # Re-save token
                        _default_cfg = os.getenv("APPDATA", "") or str(Path.home() / ".config")
                        token_path = Path(self._credentials.get("token_path", os.path.join(
                            _default_cfg, "SaveSync", "gdrive_token.json"
                        )))
                        try:
                            token_path.parent.mkdir(parents=True, exist_ok=True)
                            fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                            with os.fdopen(fd, "w") as f:
                                f.write(self._oauth_creds.to_json())
                            _restrict_file_acl(token_path)
                        except Exception as e:
                            logger.warning(f"Could not save refreshed Google token: {e}")
                        logger.info("Google Drive token refreshed proactively")
                except Exception as e:
                    logger.error(f"Google Drive token refresh failed: {e}")
                    return False
        return True

    def logout(self):
        """Fully log out: disconnect and delete the cached OAuth token file.
        Call this only when the user explicitly wants to re-authenticate."""
        self.disconnect()
        token_path_str = self._credentials.get("token_path", "")
        if token_path_str:
            token_path = Path(token_path_str)
            if token_path.is_file():
                try:
                    token_path.unlink()
                except OSError:
                    pass
        else:
            _default_cfg = os.getenv("APPDATA", "") or str(Path.home() / ".config")
            default_token = Path(os.path.join(_default_cfg, "SaveSync", "gdrive_token.json"))
            if default_token.is_file():
                try:
                    default_token.unlink()
                except OSError:
                    pass

    def _find_folder(self, name: str, parent_id: Optional[str] = None) -> Optional[str]:
        """Look up a folder by name — READ ONLY, never creates. Returns None if missing."""
        cache_key = f"{parent_id}/{name}"
        with self._cache_lock:
            if cache_key in self._folder_cache:
                return self._folder_cache[cache_key]
        q = f"name='{_escape_drive_query(name)}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        results = self._service.files().list(q=q, fields="files(id)").execute()
        files = results.get("files", [])
        if not files:
            return None
        fid = files[0]["id"]
        with self._cache_lock:
            self._folder_cache[cache_key] = fid
        return fid

    def _get_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """Get or create a folder — used only during write operations (upload).

        The entire lookup-then-create sequence runs under _cache_lock so that
        concurrent threads cannot race past the cache check and each issue
        their own Drive API create call, which would produce duplicate folders.
        """
        cache_key = f"{parent_id}/{name}"
        with self._cache_lock:
            if cache_key in self._folder_cache:
                return self._folder_cache[cache_key]

            # API lookup while holding the lock — prevents the TOCTOU race
            # where two threads both see "not found" and both create.
            q = (
                f"name='{_escape_drive_query(name)}' and "
                f"mimeType='application/vnd.google-apps.folder' and trashed=false"
            )
            if parent_id:
                q += f" and '{parent_id}' in parents"
            results = self._service.files().list(q=q, fields="files(id)").execute()
            files = results.get("files", [])
            if files:
                fid = files[0]["id"]
                self._folder_cache[cache_key] = fid
                return fid

            # Folder does not exist — create it
            meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if parent_id:
                meta["parents"] = [parent_id]
            folder = self._service.files().create(body=meta, fields="id").execute()
            self._folder_cache[cache_key] = folder["id"]
            return folder["id"]

    def _resolve_folder_id(self, remote_folder: str, create: bool = False) -> Optional[str]:
        """Walk path components, creating folders only when create=True."""
        parts = remote_folder.strip("/").split("/")
        folder_id: Optional[str] = None
        for part in parts:
            if create:
                folder_id = self._get_or_create_folder(part, folder_id)
            else:
                folder_id = self._find_folder(part, folder_id)
                if folder_id is None:
                    return None   # path doesn't exist
        return folder_id

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        """Read-only folder resolution + recursive listing with md5Checksum."""
        self.last_list_error = None
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.list_files(remote_folder)
        if not self._ensure_service():
            self.last_list_error = "not connected"
            return []
        folder_id = self._resolve_folder_id(remote_folder, create=False)
        if folder_id is None:
            return []   # folder doesn't exist — no files, don't create it
        # API errors during the walk RAISE (caller decides — fail-open)
        return self._list_recursive(remote_folder, folder_id)

    def _list_recursive(self, base_path: str, folder_id: str, depth: int = 0, max_depth: int = 15) -> list[RemoteFile]:
        """Recursively list all files under folder_id with pagination."""
        if depth >= max_depth:
            logger.warning(f"Google Drive _list_recursive: max depth {max_depth} reached at {base_path}")
            return []
        files = []
        page_token = None
        while True:
            kwargs = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,modifiedTime,size,md5Checksum,mimeType)",
                "pageSize": 1000,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            results = self._service.files().list(**kwargs).execute()
            for f in results.get("files", []):
                is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
                child_path = f"{base_path}/{f['name']}"
                if is_folder:
                    with self._cache_lock:
                        self._folder_cache[f"{folder_id}/{f['name']}"] = f["id"]
                    files.extend(self._list_recursive(child_path, f["id"], depth + 1, max_depth))
                else:
                    dt = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
                    files.append(RemoteFile(
                        path=child_path,
                        modified_at=dt.replace(tzinfo=None),
                        size_bytes=int(f.get("size", 0)),
                        checksum=f.get("md5Checksum"),
                    ))
            page_token = results.get("nextPageToken")
            if not page_token:
                break
        return files

    def upload(self, local_path: Path, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.upload(local_path, remote_path)
        if not self._ensure_service():
            return False
        try:
            from googleapiclient.http import MediaFileUpload

            parts = remote_path.strip("/").split("/")
            folder_id = None
            for part in parts[:-1]:
                folder_id = self._get_or_create_folder(part, folder_id)

            filename = parts[-1]
            q = f"name='{_escape_drive_query(filename)}' and trashed=false"
            if folder_id:
                q += f" and '{folder_id}' in parents"
            else:
                q += " and 'root' in parents"
            existing = self._service.files().list(q=q, fields="files(id)").execute().get("files", [])

            media = MediaFileUpload(str(local_path), resumable=True)
            if existing:
                # Update the first match; clean up duplicates left by prior races
                self._service.files().update(fileId=existing[0]["id"], media_body=media).execute()
                for dup in existing[1:]:
                    try:
                        self._service.files().delete(fileId=dup["id"]).execute()
                    except Exception:
                        pass
            else:
                meta = {"name": filename, "parents": [folder_id] if folder_id else ["root"]}
                self._service.files().create(body=meta, media_body=media).execute()
            return True
        except Exception as e:
            logger.error(f"GDrive upload error: {e}")
            return False

    def download(self, remote_path: str, local_path: Path) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.download(remote_path, local_path)
        if not self._ensure_service():
            return False
        try:
            from googleapiclient.http import MediaIoBaseDownload

            parts = remote_path.strip("/").split("/")
            filename = parts[-1]
            parent_folder = "/".join(parts[:-1])
            folder_id = self._resolve_folder_id(parent_folder, create=False) if parent_folder else None

            q = f"name='{_escape_drive_query(filename)}' and trashed=false"
            if folder_id:
                q += f" and '{folder_id}' in parents"
            else:
                q += " and 'root' in parents"
            files = self._service.files().list(q=q, fields="files(id)").execute().get("files", [])
            if not files:
                return False

            request = self._service.files().get_media(fileId=files[0]["id"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            # Download to temp file first, then atomic rename
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            try:
                with open(tmp_path, "wb") as f:
                    downloader = MediaIoBaseDownload(f, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                tmp_path.replace(local_path)
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            return True
        except Exception as e:
            logger.error(f"GDrive download error: {e}")
            return False

    def delete_remote(self, remote_path: str) -> bool:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.delete_remote(remote_path)
        if not self._ensure_service():
            return False
        try:
            parts    = remote_path.strip("/").split("/")
            filename = parts[-1]
            parent_folder = "/".join(parts[:-1])
            folder_id = self._resolve_folder_id(parent_folder, create=False) if parent_folder else None
            q = f"name='{_escape_drive_query(filename)}' and trashed=false"
            if folder_id:
                q += f" and '{folder_id}' in parents"
            else:
                q += " and 'root' in parents"
            files = self._service.files().list(q=q, fields="files(id)").execute().get("files", [])
            if not files:
                return True
            self._service.files().delete(fileId=files[0]["id"]).execute()
            # Invalidate folder cache entries for this path
            with self._cache_lock:
                self._folder_cache = {k: v for k, v in self._folder_cache.items()
                                      if v != files[0]["id"]}
            return True
        except Exception as e:
            logger.error(f"GDrive delete error: {e}")
            return False

    def remote_exists(self, remote_path: str) -> bool:
        """Check for both files and folders (check_cloud_saves passes folder paths)."""
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.remote_exists(remote_path)
        if not self._ensure_service():
            return False
        # Check folder first (cheaper ��� uses cache), then file
        try:
            folder_id = self._resolve_folder_id(remote_path, create=False)
            if folder_id is not None:
                return True
        except Exception as e:
            logger.debug(f"remote_exists folder check failed for {remote_path}: {e}")
        return self.get_remote_metadata(remote_path) is not None

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        if hasattr(self, "_local_delegate"):
            return self._local_delegate.get_remote_metadata(remote_path)
        if not self._ensure_service():
            return None
        try:
            parts = remote_path.strip("/").split("/")
            filename = parts[-1]
            parent_folder = "/".join(parts[:-1])
            folder_id = self._resolve_folder_id(parent_folder, create=False) if parent_folder else None
            if folder_id is None and parent_folder:
                return None  # folder doesn't exist — file definitely doesn't
            q = f"name='{_escape_drive_query(filename)}' and trashed=false"
            if folder_id:
                q += f" and '{folder_id}' in parents"
            elif not parent_folder:
                q += " and 'root' in parents"
            files = self._service.files().list(
                q=q, fields="files(id,modifiedTime,size,md5Checksum)"
            ).execute().get("files", [])
            if not files:
                return None
            f = files[0]
            dt = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
            return RemoteFile(
                path=remote_path,
                modified_at=dt.replace(tzinfo=None),
                size_bytes=int(f.get("size", 0)),
                checksum=f.get("md5Checksum"),
            )
        except Exception:
            return None

    @classmethod
    def credential_fields(cls) -> list[dict]:
        from sync.app_credentials import GOOGLE_DRIVE_CLIENT_ID
        methods = [
            {"value": "local_folder",    "label": i18n.t('google_drive.local_folder_recommended')},
        ]
        if GOOGLE_DRIVE_CLIENT_ID:
            methods.append({"value": "oauth_simple", "label": i18n.t('google_drive.signin_simple')})
        methods.append({"value": "oauth",           "label": i18n.t('google_drive.oauth_advanced')})
        methods.append({"value": "service_account", "label": i18n.t('google_drive.service_account_json')})
        return [
            {"id": "method", "label": i18n.t('google_drive.connection_method'), "type": "select",
             "options": methods, "required": True,
             "hint": i18n.t('google_drive.local_folder_hint')},
            {"id": "drive_folder_path", "label": i18n.t('google_drive.local_folder'), "type": "folder",
             "required": True, "depends_on": {"method": "local_folder"},
             "hint": i18n.t('google_drive.select_folder_hint')},
            {"id": "_oauth_guide", "type": "guide",
             "depends_on": {"method": "oauth"},
             "steps": [
                 i18n.t('google_drive.guide_step1'),
                 i18n.t('google_drive.guide_step2'),
                 i18n.t('google_drive.guide_step3'),
                 i18n.t('google_drive.guide_step4'),
             ],
             "portal_url": "https://console.cloud.google.com/apis/credentials",
             "portal_label": i18n.t('google_drive.open_gcp_console'),
            },
            {"id": "client_secret_path", "label": i18n.t('google_drive.client_secret_json'), "type": "file",
             "required": False, "depends_on": {"method": "oauth"},
             "hint": i18n.t('google_drive.client_secret_hint')},
            {"id": "_sa_guide", "type": "guide",
             "depends_on": {"method": "service_account"},
             "steps": [
                 i18n.t('google_drive.sa_step1'),
                 i18n.t('google_drive.sa_step2'),
                 i18n.t('google_drive.sa_step3'),
             ],
             "portal_url": "https://console.cloud.google.com/iam-admin/serviceaccounts",
             "portal_label": i18n.t('google_drive.open_gcp_console'),
            },
            {"id": "service_account_json", "label": i18n.t('google_drive.service_account_json_label'), "type": "file",
             "required": False, "depends_on": {"method": "service_account"},
             "hint": i18n.t('google_drive.service_account_hint')},
        ]
