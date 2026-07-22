"""
SaveSync - rclone Provider
Uses rclone as a universal sync backend.
Supports any rclone-configured remote: MEGA, B2, S3, SFTP, Box, pCloud, etc.
"""
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sync.base import SyncProvider, RemoteFile
import i18n

logger = logging.getLogger(__name__)

# Without this every rclone invocation flashes a black console window when
# SaveSync runs as a windowed app (pythonw / packaged exe).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _rclone_path() -> Optional[str]:
    """Find rclone binary in PATH."""
    return shutil.which("rclone")


class RcloneProvider(SyncProvider):
    PROVIDER_ID   = "rclone"
    DISPLAY_NAME_KEY = 'rclone.display_name'
    REQUIRES_AUTH = False  # auth handled by rclone config

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        import re as _re
        # Tolerate the "gdrive:" form users copy from rclone docs — the
        # colon is added back by _remote_path, so strip it instead of
        # silently discarding the whole remote name.
        remote = credentials.get("remote", "").strip().rstrip(":")
        # Validate remote name to prevent path injection
        if remote and not _re.match(r'^[a-zA-Z0-9_-]+$', remote):
            logger.warning(f"Invalid rclone remote name: {remote}")
            remote = ""
        self._remote    = remote
        # Optional EXTRA prefix on the remote. Every caller already passes
        # paths rooted at "SaveSync/…", so defaulting this to "SaveSync"
        # used to double-nest everything at remote:SaveSync/SaveSync/….
        base = credentials.get("base_path", "").strip()
        # Sanitize base_path to prevent path traversal using pathlib
        from pathlib import PurePosixPath
        base_parts = PurePosixPath(base).parts
        # Reject any component that is ".."
        base_parts = [p for p in base_parts if p not in ("..", "/")]
        self._base_path = "/".join(base_parts)
        self._rclone    = _rclone_path()

    @staticmethod
    def _get_timeout(default: int = 30) -> int:
        """Read sync timeout from config, with a minimum floor."""
        try:
            from core.config_manager import get_config
            return max(default, int(get_config().get("sync_timeout", 120)))
        except Exception:
            return default

    # ── Display ──────────────────────────────────────────────────────────────

    @property
    def user_display(self) -> str:
        if self._remote:
            return f"rclone:{self._remote}"
        return i18n.t('rclone.not_configured')

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if not self._rclone:
            logger.error("rclone not found in PATH")
            self.last_error = "rclone not found in PATH"
            return False
        if not self._remote:
            logger.error("No rclone remote specified")
            self.last_error = "no rclone remote specified"
            return False
        try:
            result = self._run(["lsd", f"{self._remote}:", "--max-depth", "1"],
                               timeout=self._get_timeout(15))
            if result.returncode == 0:
                logger.info(f"rclone connected to remote: {self._remote}")
                self._connected = True
                self._user_info = {"name": f"rclone:{self._remote}"}
                return True
            logger.error(f"rclone lsd failed: {result.stderr.strip()}")
            self.last_error = result.stderr.strip()[:120]
            return False
        except subprocess.TimeoutExpired:
            logger.error("rclone connect timed out")
            self.last_error = "rclone connect timed out"
            return False
        except Exception as e:
            # PermissionError on a non-executable binary, decode errors on
            # exotic locales, … — none of these may escape into the caller
            # (the auto-reconnect timer calls connect() from a Qt slot).
            logger.error(f"rclone connect error: {e}")
            self.last_error = str(e)[:120]
            return False

    def disconnect(self):
        self._connected = False

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _remote_path(self, rel: str) -> str:
        """Build full rclone remote path string."""
        rel = rel.strip("/")
        parts = [p for p in (self._base_path, rel) if p]
        return f"{self._remote}:" + "/".join(parts)

    def _run(self, args: list, timeout: int = None) -> subprocess.CompletedProcess:
        if timeout is None:
            timeout = self._get_timeout(120)
        return subprocess.run(
            [self._rclone] + args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )

    @staticmethod
    def _parse_modtime(mod_str: str) -> datetime:
        try:
            cleaned = mod_str.replace("Z", "+00:00")
            # Truncate fractional seconds to 6 digits (microsecond precision)
            # so fromisoformat can parse them, instead of discarding entirely
            # which loses sub-second precision and causes false sync equality.
            import re
            cleaned = re.sub(r'\.(\d{1,6})\d*', r'.\1', cleaned)
            dt = datetime.fromisoformat(cleaned)
            # Convert to UTC before stripping tzinfo so that non-UTC offsets
            # (e.g. +05:30) are normalised consistently with other providers.
            if dt.tzinfo is not None:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc)
            return dt.replace(tzinfo=None)
        except Exception:
            return datetime.min  # Treats unknown as old, not new

    # ── File operations ──────────────────────────────────────────────────────

    def upload(self, local_path: Path, remote_path: str) -> bool:
        """Upload a single file using rclone copyto."""
        if not self._rclone or not self._remote:
            return False
        try:
            result = self._run(["copyto", str(local_path), self._remote_path(remote_path)])
            if result.returncode != 0:
                logger.error(f"rclone upload failed: {result.stderr[:300]}")
            return result.returncode == 0
        except Exception as e:
            logger.error(f"rclone upload error: {e}")
            return False

    def download(self, remote_path: str, local_path: Path) -> bool:
        """Download a single file using rclone copyto. Atomic download via temp file."""
        if not self._rclone or not self._remote:
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
        try:
            result = self._run(["copyto", self._remote_path(remote_path), str(tmp_path)])
            if result.returncode != 0:
                logger.error(f"rclone download failed: {result.stderr[:300]}")
                if tmp_path.exists():
                    tmp_path.unlink()
                return False
            tmp_path.replace(local_path)
            return True
        except Exception as e:
            logger.error(f"rclone download error: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return False

    def list_files(self, remote_folder: str) -> list[RemoteFile]:
        """List files at remote_folder, returning RemoteFile objects."""
        self.last_list_error = None
        if not self._rclone or not self._remote:
            self.last_list_error = "rclone not configured"
            return []
        try:
            result = self._run(
                ["lsjson", "--recursive", self._remote_path(remote_folder)], timeout=self._get_timeout(30)
            )
            if result.returncode != 0:
                # "directory not found" = legitimate empty; anything else
                # means the listing could not be verified.
                if "directory not found" not in result.stderr.lower():
                    self.last_list_error = result.stderr.strip()[:120]
                return []
            if not result.stdout.strip():
                return []
            try:
                items = json.loads(result.stdout)
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"rclone lsjson parse error: {e}")
                self.last_list_error = f"lsjson parse error: {e}"[:120]
                return []
            files = []
            for item in items:
                if item.get("IsDir"):
                    continue
                rel_path = item.get("Path", item.get("Name", ""))
                files.append(RemoteFile(
                    path=f"{remote_folder}/{rel_path}",
                    modified_at=self._parse_modtime(item.get("ModTime", "")),
                    size_bytes=int(item.get("Size", 0)),
                    checksum=item.get("Hashes", {}).get("md5") if isinstance(item.get("Hashes"), dict) else None,
                ))
            return files
        except Exception as e:
            logger.error(f"rclone list_files error: {e}")
            self.last_list_error = str(e)[:120]
            return []

    def delete_remote(self, remote_path: str) -> bool:
        """Delete a single remote file."""
        if not self._rclone or not self._remote:
            return False
        try:
            result = self._run(["deletefile", self._remote_path(remote_path)])
            if result.returncode != 0:
                # If deletion failed, check if the file already doesn't exist
                if not self.remote_exists(remote_path):
                    return True
                logger.error(f"rclone deletefile failed for {remote_path}: {result.stderr[:300]}")
                return False
            return True
        except Exception as e:
            logger.error(f"rclone delete_remote error: {e}")
            return False

    def remote_exists(self, remote_path: str) -> bool:
        """Check if remote path exists (file or folder, even empty)."""
        if not self._rclone or not self._remote:
            return False
        try:
            # Use lsjson on the path itself — returns metadata if it exists
            rp = self._remote_path(remote_path)
            result = self._run(["lsjson", rp, "--max-depth", "0"],
                               timeout=self._get_timeout())
            if result.returncode == 0:
                return True
            # Fallback: check if it's a directory by listing parent
            parent = "/".join(remote_path.strip("/").split("/")[:-1])
            name = remote_path.strip("/").split("/")[-1]
            rp_parent = self._remote_path(parent) if parent else f"{self._remote}:"
            result = self._run(["lsjson", rp_parent, "--max-depth", "1"],
                               timeout=self._get_timeout())
            if result.returncode == 0 and result.stdout.strip():
                import json
                items = json.loads(result.stdout)
                return any(item.get("Name") == name for item in items)
            return False
        except Exception:
            return False

    def get_remote_metadata(self, remote_path: str) -> Optional[RemoteFile]:
        """Return metadata for a single remote file, or None if not found."""
        if not self._rclone or not self._remote:
            return None
        try:
            result = self._run(
                ["lsjson", self._remote_path(remote_path)], timeout=self._get_timeout(20)
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            items = json.loads(result.stdout)
            if not items:
                return None
            item = items[0]
            return RemoteFile(
                path=remote_path,
                modified_at=self._parse_modtime(item.get("ModTime", "")),
                size_bytes=int(item.get("Size", 0)),
                checksum=item.get("Hashes", {}).get("md5") if isinstance(item.get("Hashes"), dict) else None,
            )
        except Exception as e:
            logger.error(f"rclone get_remote_metadata error: {e}")
            return None

    # ── Credential schema ────────────────────────────────────────────────────

    @classmethod
    def credential_fields(cls) -> list[dict]:
        return [
            {
                "id": "remote",
                "label": i18n.t('rclone.remote_name'),
                "type": "text",
                "required": True,
                "placeholder": i18n.t('rclone.remote_placeholder'),
            },
            {
                "id": "base_path",
                "label": i18n.t('rclone.base_folder'),
                "type": "text",
                "hint": i18n.t('rclone.base_folder_hint'),
            },
        ]
