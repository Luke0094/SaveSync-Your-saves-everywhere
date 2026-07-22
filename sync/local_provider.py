"""
SaveSync - Local Folder Provider
Syncs saves to any local/network path (USB, NAS, etc.)
"""
import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sync.base import SyncProvider, RemoteFile
import i18n

logger = logging.getLogger(__name__)


class LocalProvider(SyncProvider):
    PROVIDER_ID = "local"
    DISPLAY_NAME_KEY = 'local_provider.display_name'

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._root: Optional[Path] = None

    def connect(self) -> bool:
        path_str = self._credentials.get("root_path", "")
        if not path_str:
            return False
        root = Path(path_str)
        if not root.exists():
            try:
                root.mkdir(parents=True)
            except Exception as e:
                logger.error(f"Local provider: cannot create root {root}: {e}")
                return False
        self._root = root
        self._connected = True
        self._user_info = {"name": str(root)}
        return True

    def disconnect(self):
        self._root = None
        self._connected = False

    def _remote_to_local(self, remote_path: str) -> Path:
        if self._root is None:
            raise RuntimeError("LocalProvider not connected")
        result = (self._root / remote_path.replace("/", os.sep)).resolve()
        root_resolved = self._root.resolve()
        result_str = str(result)
        root_str = str(root_resolved)
        if os.name == "nt":
            result_str = result_str.lower()
            root_str = root_str.lower()
        if result_str != root_str and not result_str.startswith(root_str + os.sep):
            raise ValueError(f"Path traversal detected: {remote_path!r}")
        return result

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        self.last_list_error = None   # I/O errors here RAISE (caller decides)
        folder = self._remote_to_local(remote_folder)
        if not folder.exists():
            return []
        results = []
        for f in folder.rglob("*"):
            if f.is_file():
                stat = f.stat()
                rel = str(f.relative_to(self._root)).replace(os.sep, "/")
                # Compute MD5 checksum for reliable change detection
                # across machines (mtime alone is unreliable for local sync).
                try:
                    h = hashlib.md5()
                    with open(f, "rb") as fh:
                        for chunk in iter(lambda: fh.read(65536), b""):
                            h.update(chunk)
                    md5 = h.hexdigest()
                except OSError:
                    md5 = None
                results.append(RemoteFile(
                    path=rel,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(tzinfo=None),
                    size_bytes=stat.st_size,
                    checksum=md5,
                ))
        return results

    def upload(self, local_path: Path, remote_path: str) -> bool:
        try:
            dest = self._remote_to_local(remote_path)
        except ValueError:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write to .tmp sibling then atomic rename - prevents corrupt partial files
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            shutil.copy2(local_path, tmp)
            tmp.replace(dest)   # atomic on same filesystem
            return True
        except Exception as e:
            logger.error(f"Local upload error: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def download(self, remote_path: str, local_path: Path) -> bool:
        try:
            src = self._remote_to_local(remote_path)
        except ValueError:
            return False
        if not src.exists():
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".tmp")
        try:
            shutil.copy2(src, tmp)
            tmp.replace(local_path)  # atomic on same filesystem
            return True
        except Exception as e:
            logger.error(f"Local download error: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def delete_remote(self, remote_path: str) -> bool:
        try:
            p = self._remote_to_local(remote_path)
        except ValueError:
            return False
        try:
            if p.exists():
                p.unlink()
            return True
        except Exception:
            return False

    def remote_exists(self, remote_path: str) -> bool:
        try:
            return self._remote_to_local(remote_path).exists()
        except ValueError:
            return False

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        try:
            p = self._remote_to_local(remote_path)
        except ValueError:
            return None
        if not p.exists():
            return None
        stat = p.stat()
        try:
            h = hashlib.md5()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            md5 = h.hexdigest()
        except OSError:
            md5 = None
        return RemoteFile(
            path=remote_path,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(tzinfo=None),
            size_bytes=stat.st_size,
            checksum=md5,
        )

    @classmethod
    def credential_fields(cls) -> list[dict]:
        return [
            {"id": "root_path", "label": i18n.t('local_provider.sync_folder_path'), "type": "folder", "required": True},
        ]
