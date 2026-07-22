"""
SaveSync - WebDAV Provider
Works with Nextcloud, ownCloud, Box WebDAV, and any WebDAV server.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sync.base import SyncProvider, RemoteFile
import i18n

logger = logging.getLogger(__name__)


class WebDAVProvider(SyncProvider):
    PROVIDER_ID  = "webdav"
    DISPLAY_NAME_KEY = 'webdav.display_name'

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._client = None

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            from webdav3.client import Client
            url      = self._credentials.get("url", "").strip()
            username = self._credentials.get("username", "").strip()
            password = self._credentials.get("password", "")
            if not url:
                logger.error("WebDAV: no URL provided")
                self.last_error = "no URL provided"
                return False
            if url.startswith("http://"):
                logger.warning(
                    "\u26a0 WARNING: WebDAV URL uses plain HTTP (not HTTPS) \u2014 "
                    "credentials will be sent in plaintext over the network. "
                    "Switch to an https:// URL to protect your login credentials."
                )
            options = {
                "webdav_hostname": url,
                "webdav_login":    username,
                "webdav_password": password,
                # Without this, webdavclient3 uses its library default and the
                # user's sync_timeout setting is silently ignored.
                "webdav_timeout":  self._get_timeout(),
            }
            self._client = Client(options)
            verify_ssl_val = self._credentials.get("verify_ssl", True)
            if isinstance(verify_ssl_val, str):
                verify_ssl_val = verify_ssl_val.lower() in ("true", "1", "yes")
            self._client.verify = bool(verify_ssl_val)
            self._client.check("/")   # test connection
            self._connected = True
            self._user_info = {"name": f"{username}@{url}"}
            return True
        except ImportError:
            logger.error("webdavclient3 not installed — run: pip install webdavclient3")
            self.last_error = "webdavclient3 not installed"
            return False
        except Exception as e:
            logger.error(f"WebDAV connect error: {e}")
            self.last_error = str(e)[:120]
            return False

    def disconnect(self):
        self._connected = False
        self._client = None

    # ── File operations ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(date_str: str) -> datetime:
        """Parse WebDAV date strings robustly (ISO 8601 + RFC 2822).
        Falls back to datetime.min instead of datetime.now() to avoid treating
        unparseable timestamps as 'just modified'."""
        if not date_str:
            return datetime.min
        # Try ISO 8601 first
        try:
            dt = datetime.fromisoformat(date_str)
            # Convert to UTC if aware, then strip tzinfo for consistency
            if dt.tzinfo is not None:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            pass
        # Try RFC 2822 (common WebDAV format)
        try:
            from email.utils import parsedate_to_datetime
            from datetime import timezone
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.replace(tzinfo=None)
        except Exception:
            pass
        logger.warning(f"Could not parse WebDAV date: {date_str!r}, treating as unknown")
        return datetime.min

    def _ensure_client(self) -> bool:
        if self._client is None:
            logger.error("WebDAV client not connected")
            return False
        return True

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        self.last_list_error = None
        if not self._ensure_client():
            self.last_list_error = "not connected"
            return []
        try:
            return self._list_recursive(remote_folder)
        except Exception as e:
            logger.error(f"WebDAV list error: {e}")
            # RemoteResourceNotFound = the folder simply doesn't exist —
            # a legitimate empty listing, not a verification failure.
            if type(e).__name__ != "RemoteResourceNotFound":
                self.last_list_error = str(e)[:120]
            return []

    def _list_recursive(self, remote_folder: str, max_depth: int = 15, _visited: set = None) -> list[RemoteFile]:
        """Recursively list all files under remote_folder, up to max_depth levels.

        Uses a _visited set to prevent infinite loops when the server returns
        paths with different casing, URL-encoding, or normalisation.
        """
        if max_depth <= 0:
            logger.warning(f"WebDAV _list_recursive: max depth reached at {remote_folder}, stopping")
            return []
        if _visited is None:
            _visited = set()
        # Normalise for cycle detection: lowercase, strip trailing slashes
        norm_folder = remote_folder.rstrip("/").lower()
        if norm_folder in _visited:
            return []
        _visited.add(norm_folder)

        items = self._client.list(remote_folder, get_info=True)
        files = []
        for item in items:
            item_path = item.get("path", "")
            if item.get("isdir"):
                # Recurse into subdirectories (skip self-reference)
                if item_path:
                    norm_item = item_path.rstrip("/").lower()
                    if norm_item != norm_folder and norm_item not in _visited:
                        files.extend(self._list_recursive(item_path, max_depth - 1, _visited))
                continue
            # Normalize server-absolute paths to remote_folder-prefixed paths
            # so the base-class sync logic can strip remote_base consistently
            # across all providers.
            if item_path:
                from pathlib import PurePosixPath
                stripped_folder = remote_folder.strip("/")
                folder_parts = PurePosixPath(stripped_folder).parts if stripped_folder else ()
                path_parts = PurePosixPath(item_path.lstrip("/")).parts
                folder_parts_lower = tuple(p.lower() for p in folder_parts) if folder_parts else ()
                path_parts_lower = tuple(p.lower() for p in path_parts)
                if folder_parts_lower and len(path_parts_lower) > len(folder_parts_lower) and path_parts_lower[:len(folder_parts_lower)] == folder_parts_lower:
                    rel = "/".join(path_parts[len(folder_parts):])
                    item_path = f"{stripped_folder}/{rel}"
                elif not folder_parts and path_parts:
                    # When remote_folder is empty/root, use the raw path as-is
                    # but ensure it's prefixed consistently (no leading slash)
                    item_path = "/".join(path_parts)
            mod_dt = self._parse_datetime(item.get("modified", ""))
            files.append(RemoteFile(
                path=item_path,
                modified_at=mod_dt,
                size_bytes=int(item.get("size", 0) or 0),
            ))
        return files

    def upload(self, local_path: Path, remote_path: str) -> bool:
        if not self._ensure_client():
            return False
        try:
            # Ensure parent directories exist (create intermediate dirs)
            remote_dir = "/".join(remote_path.split("/")[:-1])
            if remote_dir:
                parts = remote_dir.strip("/").split("/")
                current = ""
                for part in parts:
                    current = f"{current}/{part}" if current else part
                    try:
                        if not self._client.check(current):
                            self._client.mkdir(current)
                    except Exception as e:
                        # check() may fail if parent doesn't exist yet;
                        # try mkdir directly — it's idempotent if dir already exists.
                        try:
                            self._client.mkdir(current)
                        except Exception:
                            # Only warn if both check and mkdir fail (e.g. permission denied)
                            logger.warning(f"WebDAV mkdir failed for '{current}': {e}")
            # Atomic upload: send to a temp name, then server-side MOVE onto
            # the final path — an interrupted transfer must never leave a
            # truncated index.json/zip at the real location (download() below
            # already has the mirror-image tmp+replace).
            tmp_remote = remote_path + ".tmp"
            self._client.upload_sync(remote_path=tmp_remote, local_path=str(local_path))
            try:
                self._client.move(remote_path_from=tmp_remote,
                                  remote_path_to=remote_path, overwrite=True)
            except Exception:
                try:
                    self._client.clean(tmp_remote)
                except Exception:
                    pass
                raise
            return True
        except Exception as e:
            logger.error(f"WebDAV upload error: {e}")
            return False

    def download(self, remote_path: str, local_path: Path) -> bool:
        """Atomic download — write to .tmp then rename."""
        if not self._ensure_client():
            return False
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            try:
                self._client.download_sync(remote_path=remote_path, local_path=str(tmp_path))
                tmp_path.replace(local_path)
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            return True
        except Exception as e:
            logger.error(f"WebDAV download error: {e}")
            return False

    def delete_remote(self, remote_path: str) -> bool:
        if not self._ensure_client():
            return False
        try:
            self._client.clean(remote_path)
            return True
        except Exception:
            # If the file doesn't exist, treat deletion as successful
            try:
                if not self._client.check(remote_path):
                    return True
            except Exception:
                pass
            return False

    def remote_exists(self, remote_path: str) -> bool:
        if not self._ensure_client():
            return False
        try:
            return self._client.check(remote_path)
        except Exception:
            return False

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        if not self._ensure_client():
            return None
        try:
            info = self._client.info(remote_path)
            mod_dt = self._parse_datetime(info.get("modified", ""))
            return RemoteFile(
                path=remote_path,
                modified_at=mod_dt,
                size_bytes=int(info.get("size", 0) or 0),
            )
        except Exception:
            return None

    # ── Credential schema ────────────────────────────────────────────────────

    @classmethod
    def credential_fields(cls) -> list[dict]:
        return [
            {
                "id": "preset",
                "label": i18n.t('webdav.server_type'),
                "type": "select",
                "options": [
                    {"value": "custom",     "label": i18n.t('webdav.custom_server')},
                    {"value": "nextcloud",  "label": "Nextcloud"},
                    {"value": "owncloud",   "label": "ownCloud"},
                    {"value": "box",        "label": "Box.com"},
                    {"value": "4shared",    "label": "4shared"},
                ],
                "required": False,
            },
            {
                "id": "url",
                "label": i18n.t('webdav.url'),
                "type": "text",
                "required": True,
                "placeholder": "https://nextcloud.example.com/remote.php/dav/files/username/",
            },
            {
                "id": "username",
                "label": i18n.t('webdav.username'),
                "type": "text",
                "required": True,
                "hint": i18n.t('webdav.username_hint'),
            },
            {
                "id": "password",
                "label": i18n.t('webdav.password'),
                "type": "password",
                "required": True,
                "hint": i18n.t('webdav.password_hint'),
            },
            {
                "id": "verify_ssl",
                "label": i18n.t('webdav.verify_ssl'),
                "type": "bool",
                "default": True,   # NEVER default to skipping TLS verification
                "required": False,
                "hint": i18n.t('webdav.verify_ssl_hint'),
            },
        ]
