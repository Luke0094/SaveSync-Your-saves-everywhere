"""
SaveSync - Credential Store

Primary store: OS keyring (Windows Credential Manager, macOS Keychain,
Linux Secret Service).

File fallback (when keyring is unavailable): AES-256-GCM of the blob under
USER_DATA_DIR. The per-install salt that feeds key derivation is NOT kept as
plaintext next to credentials.enc — it is wrapped with the OS user secret
store when possible (Windows DPAPI; keyring for salt on other platforms).
A same-key redundancy copy may exist as credentials.backup.enc; the old
hardcoded-string "backup key" is read-only for migration and never written.
"""
import base64
import json
import logging
import os
import platform
import threading
import traceback
from typing import Optional

from core.constants import USER_DATA_DIR

logger = logging.getLogger(__name__)

_SERVICE = "SaveSync"
_ACCOUNT = "sync_credentials"
_SALT_ACCOUNT = "encryption_salt"
_FALLBACK_PATH = USER_DATA_DIR / "credentials.enc"
_BACKUP_PATH = USER_DATA_DIR / "credentials.backup.enc"
_SALT_PATH = USER_DATA_DIR / "salt"
# Wrapped salt on disk (DPAPI / opaque blob). Legacy plaintext lives at _SALT_PATH.
_SALT_PROTECTED_PATH = USER_DATA_DIR / "salt.protected"
_SALT_FILE_MAGIC = b"SSALT1\n"


_salt_lock = threading.Lock()
_cached_salt: str | None = None


def _dpapi_protect(data: bytes) -> bytes:
    """Windows DPAPI (user scope) — ciphertext only decrypts for this login."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _wrap_secret(plaintext: str) -> bytes:
    """Wrap *plaintext* so a stolen data-dir alone is not enough to recover it."""
    raw = plaintext.encode("utf-8")
    if platform.system() == "Windows":
        return _SALT_FILE_MAGIC + _dpapi_protect(raw)
    # Prefer OS keyring for the salt even when the credential *blob* uses
    # the file fallback — keyring often still works for a short secret.
    try:
        if not os.environ.get("SAVESYNC_DISABLE_KEYRING"):
            import keyring
            keyring.set_password(_SERVICE, _SALT_ACCOUNT, plaintext)
            return _SALT_FILE_MAGIC + b"KEYRING"
    except Exception as e:
        logger.debug(f"Keyring salt wrap unavailable: {e}")
    # Last resort: plaintext marker — honest obfuscation only.
    return _SALT_FILE_MAGIC + b"PLAIN:" + raw


def _unwrap_secret(blob: bytes) -> str:
    if not blob.startswith(_SALT_FILE_MAGIC):
        # Legacy plaintext salt file (no magic).
        return blob.decode("utf-8").strip()
    body = blob[len(_SALT_FILE_MAGIC):]
    if body == b"KEYRING":
        import keyring
        val = keyring.get_password(_SERVICE, _SALT_ACCOUNT)
        if not val:
            raise ValueError("Salt marked KEYRING but missing from OS store")
        return val.strip()
    if body.startswith(b"PLAIN:"):
        return body[len(b"PLAIN:"):].decode("utf-8").strip()
    if platform.system() == "Windows":
        return _dpapi_unprotect(body).decode("utf-8").strip()
    raise ValueError("Unsupported protected-salt payload")


def _persist_salt(salt: str) -> None:
    """Write salt wrapped; migrate away from legacy plaintext when possible."""
    from core import atomic_replace as _atomic_replace
    from core import restrict_to_owner as _restrict_to_owner
    wrapped = _wrap_secret(salt)
    tmp = _SALT_PROTECTED_PATH.with_name(_SALT_PROTECTED_PATH.name + ".tmp")
    tmp.write_bytes(wrapped)
    _atomic_replace(tmp, _SALT_PROTECTED_PATH)
    _restrict_to_owner(_SALT_PROTECTED_PATH)
    # Drop legacy plaintext salt once the protected copy is in place —
    # unless wrap fell back to PLAIN (then the plaintext file is the store).
    if wrapped.startswith(_SALT_FILE_MAGIC + b"PLAIN:"):
        return
    if _SALT_PATH.exists():
        try:
            _SALT_PATH.unlink()
        except OSError:
            pass


def _get_or_create_salt() -> str:
    """Get or create a per-installation random salt (OS-wrapped on disk)."""
    global _cached_salt
    with _salt_lock:
        if _cached_salt is not None:
            return _cached_salt

        # 1) Protected file
        if _SALT_PROTECTED_PATH.exists():
            try:
                existing = _unwrap_secret(_SALT_PROTECTED_PATH.read_bytes())
                if existing:
                    _cached_salt = existing
                    return existing
            except Exception as e:
                logger.warning(f"Could not unwrap protected salt: {e}")

        # 2) Keyring-only salt (no local file yet)
        if not os.environ.get("SAVESYNC_DISABLE_KEYRING"):
            try:
                import keyring
                existing = keyring.get_password(_SERVICE, _SALT_ACCOUNT)
                if existing:
                    _cached_salt = existing.strip()
                    try:
                        _persist_salt(_cached_salt)
                    except Exception:
                        pass
                    return _cached_salt
            except Exception:
                pass

        # 3) Legacy plaintext salt — migrate into protected storage
        if _SALT_PATH.exists():
            try:
                existing = _SALT_PATH.read_text(encoding="utf-8").strip()
                if existing:
                    _cached_salt = existing
                    try:
                        _persist_salt(existing)
                        logger.info("Migrated credential salt into OS-protected storage")
                    except Exception as e:
                        logger.warning(f"Salt migration to protected storage failed: {e}")
                    return existing
            except Exception:
                pass

        import secrets
        from core import restrict_to_owner as _restrict_to_owner
        salt = secrets.token_hex(16)
        try:
            _persist_salt(salt)
        except Exception:
            try:
                _SALT_PATH.write_text(salt, encoding="utf-8")
                _restrict_to_owner(_SALT_PATH)
                logger.warning(
                    "Could not OS-protect encryption salt; wrote plaintext salt "
                    "(file-fallback credentials remain obfuscation-only if the "
                    "whole data directory is copied)"
                )
            except Exception:
                logger.warning(
                    "Could not persist encryption salt to disk; "
                    "using ephemeral in-memory salt for this session"
                )
        _cached_salt = salt
        return salt


def _derive_encryption_key() -> bytes:
    """Derive a 256-bit encryption key from machine ID and system entropy."""
    from core.machine import get_machine_id
    import hashlib

    machine_id = get_machine_id()
    entropy_sources = [
        machine_id,
        os.getenv("COMPUTERNAME", ""),
        os.getenv("USERNAME", ""),
        _get_or_create_salt(),
    ]
    combined = "|".join(s or "" for s in entropy_sources).encode("utf-8")
    return hashlib.sha256(combined).digest()


def _derive_flexible_key() -> bytes:
    """Legacy migration key (domain-separated). Used only for decrypt attempts."""
    from core.machine import get_machine_id
    import hashlib

    salt = _get_or_create_salt()
    primary_sources = [
        get_machine_id(),
        os.getenv("COMPUTERNAME", ""),
        os.getenv("USERNAME", ""),
        salt,
    ]
    filtered = [s or "" for s in primary_sources]
    if sum(1 for s in filtered if s) >= 2:
        combined = ("flexible|" + "|".join(filtered)).encode("utf-8")
        return hashlib.sha256(combined).digest()

    fallback_sources = [
        os.getenv("USERPROFILE", "") or os.getenv("HOME", ""),
        os.getenv("USERDOMAIN", ""),
        platform.node(),
        "SaveSync2024",  # legacy only — never used to encrypt new blobs
    ]
    combined = ("flexible|" + "|".join(filter(None, fallback_sources))).encode("utf-8")
    return hashlib.sha256(combined).digest()


def _create_backup_key() -> bytes:
    """Legacy weak backup key (hardcoded app string). Decrypt-only for migration."""
    import hashlib
    stable_sources = [
        platform.node(),
        os.getenv("USERNAME", ""),
        "SaveSync2024",
    ]
    combined = "|".join(s or "" for s in stable_sources).encode("utf-8")
    return hashlib.sha256(combined).digest()

def encrypt_data(data: bytes, key: bytes) -> bytes:
    """Encrypt data using AES-256 in GCM mode."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import secrets
    
    # Generate random nonce
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    
    # Return nonce + ciphertext
    return nonce + ciphertext

def decrypt_data(encrypted_data: bytes, key: bytes) -> bytes:
    """Decrypt data using AES-256 in GCM mode."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    
    if len(encrypted_data) < 12:
        raise ValueError("Invalid encrypted data")
    
    nonce = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)

def _try_decrypt_with_multiple_keys(encrypted_data: bytes) -> tuple[bytes, str]:
    """Try primary key first; legacy flexible/weak-backup keys only for migration."""
    keys_to_try = [
        (_derive_encryption_key(), "primary"),
        (_derive_flexible_key(), "flexible"),
        (_create_backup_key(), "legacy_backup"),
    ]

    last_error = None
    for key, key_type in keys_to_try:
        try:
            decrypted = decrypt_data(encrypted_data, key)
            logger.debug(f"Successfully decrypted with {key_type} key")
            return decrypted, key_type
        except (ValueError, Exception) as e:
            logger.debug(f"Decryption with {key_type} key failed: {type(e).__name__}: {e}")
            last_error = e
            continue

    raise ValueError(f"All decryption attempts failed (last error: {last_error})")


class CredentialStore:
    """
    Secure credential storage with OS keyring primary, AES-256 encrypted file fallback.
    Never stores credentials in plaintext config.json.

    Storage format v2 (multi-provider):
        {"version": 2, "providers": {"google_drive": {...}, "dropbox": {...}}}
    Legacy v1 format is auto-migrated on first load:
        {"provider": "google_drive", "creds": {...}}
    """

    def __init__(self):
        self._keyring_available = self._check_keyring()
        self._io_lock = threading.Lock()

    def _check_keyring(self) -> bool:
        """Check if the OS keyring is functional without writing to it.

        Reads a known-absent key — get_password returns None without
        side-effects if keyring is available but the key doesn't exist.

        SAVESYNC_DISABLE_KEYRING (env): forces the file fallback under
        USER_DATA_DIR. The OS keyring entry is GLOBAL ("SaveSync") and does
        NOT follow an APPDATA sandbox — a test that saved credentials
        through the keyring once clobbered the user's real provider
        configuration. Every sandboxed test MUST set this.
        """
        if os.environ.get("SAVESYNC_DISABLE_KEYRING"):
            logger.info("Keyring disabled via SAVESYNC_DISABLE_KEYRING — file fallback")
            return False
        try:
            import keyring
            logger.debug(f"Keyring backend: {keyring.get_keyring()}")
            
            # Test reading a non-existent key
            result = keyring.get_password(_SERVICE, "__availability_check__")
            logger.debug(f"Keyring test result: {result}")
            
            # Additional diagnostics
            try:
                import keyring.backend
                backends = keyring.backend.get_all_keyring()
                logger.debug(f"Available backends: {[type(b).__name__ for b in backends]}")
            except Exception as e:
                logger.debug(f"Could not list backends: {e}")
            
            # Test Windows Credential Manager specifically
            if platform.system() == "Windows":
                try:
                    import win32cred
                    logger.debug("Windows Credential Manager available")
                except ImportError:
                    logger.debug("win32cred not available - pywin32 missing?")
                except Exception as e:
                    logger.debug(f"Windows Credential Manager error: {e}")
            
            return True
        except Exception as e:
            logger.info(
                f"OS keyring unavailable — using local file fallback "
                f"(salt OS-wrapped when possible). Error: {type(e).__name__}: {e}"
            )
            logger.debug(f"Keyring failure traceback: {traceback.format_exc()}")
            return False

    # ── Internal blob helpers ──────────────────────────────────────────────────

    def _load_blob(self) -> dict:
        """Load the raw JSON blob from keyring or fallback file.
        Returns a v2 dict: {"version": 2, "providers": {...}}.
        Auto-migrates v1 format on load."""
        data = self._load_blob_raw()
        if not data:
            return {"version": 2, "providers": {}}
        # v1 migration: {"provider": ..., "creds": ...} -> v2
        if "provider" in data and "version" not in data:
            pid = data.get("provider")
            creds = data.get("creds", {})
            v2 = {"version": 2, "providers": {pid: creds} if pid and creds else {}}
            logger.info(f"Migrated credential store from v1 to v2 (provider: {pid})")
            self._save_blob(v2)
            return v2
        return data

    def _load_blob_raw(self) -> dict:
        """Load raw JSON from keyring or fallback files. Returns dict or {}."""
        if self._keyring_available:
            try:
                import keyring
                payload = keyring.get_password(_SERVICE, _ACCOUNT)
                if payload:
                    return json.loads(payload)
            except Exception as e:
                logger.warning(f"Keyring load failed ({e}), trying fallback")

        credential_files = [
            (_FALLBACK_PATH, "primary"),
            (_BACKUP_PATH, "backup")
        ]
        for file_path, file_type in credential_files:
            if file_path.exists():
                try:
                    encoded = file_path.read_bytes()
                    encrypted = base64.b64decode(encoded)
                    raw, key_type = _try_decrypt_with_multiple_keys(encrypted)
                    data = json.loads(raw.decode("utf-8"))
                    logger.info(f"Loaded credentials from {file_type} file using {key_type} key")

                    # Anything opened with a legacy key is rewritten under the
                    # primary key (and the same-key backup copy) so the weak
                    # hardcoded backup scheme is retired on disk.
                    if key_type != "primary":
                        try:
                            self._write_fallback_files(raw)
                            logger.info(
                                f"Re-encrypted credentials with primary key "
                                f"(was {key_type})"
                            )
                        except Exception as e:
                            logger.warning(f"Re-encryption failed: {e}")

                    return data
                except Exception as e:
                    logger.warning(f"{file_type} credential file could not be loaded: {type(e).__name__}: {e}")
                    logger.debug(f"Full traceback: {traceback.format_exc()}")
                    should_delete = False
                    try:
                        file_size = file_path.stat().st_size
                        if file_size == 0 or file_size < 50:
                            should_delete = True
                        elif isinstance(e, (ValueError, json.JSONDecodeError)):
                            should_delete = True
                    except Exception:
                        pass
                    if should_delete:
                        try:
                            file_path.unlink()
                            logger.info(f"Removed corrupted {file_type} credential file")
                        except Exception:
                            pass
        return {}

    def _save_blob(self, data: dict) -> bool:
        """Persist the v2 JSON blob to keyring and/or fallback file."""
        payload = json.dumps(data)
        if self._keyring_available:
            try:
                import keyring
                keyring.set_password(_SERVICE, _ACCOUNT, payload)
                logger.debug("Credentials saved to OS keyring")
                return True
            except Exception as e:
                logger.warning(f"Keyring save failed ({e}), using fallback")

        try:
            self._write_fallback_files(payload.encode("utf-8"))
            return True
        except Exception as e:
            logger.error(f"Credential save failed: {e}")
            return False

    def _write_fallback_files(self, raw: bytes) -> None:
        """Write credentials.enc + same-key redundancy copy (not a weaker key)."""
        from core import atomic_replace as _atomic_replace
        from core import restrict_to_owner as _restrict_to_owner
        key = _derive_encryption_key()
        encoded = base64.b64encode(encrypt_data(raw, key))
        tmp_fallback = _FALLBACK_PATH.with_suffix(".tmp")
        tmp_fallback.write_bytes(encoded)
        _atomic_replace(tmp_fallback, _FALLBACK_PATH)
        _restrict_to_owner(_FALLBACK_PATH)
        try:
            # Same ciphertext material / same primary key — survives a
            # half-written primary without opening the old weak-key shortcut.
            tmp_backup = _BACKUP_PATH.with_suffix(".tmp")
            tmp_backup.write_bytes(encoded)
            _atomic_replace(tmp_backup, _BACKUP_PATH)
            _restrict_to_owner(_BACKUP_PATH)
        except Exception as e:
            logger.debug(f"Could not create same-key credential backup: {e}")

    # ── Public API ───────────────────────────────────────────────────────────

    def save(self, provider_id: str, credentials: dict) -> bool:
        """Save credentials for a provider (additive — other providers are preserved)."""
        with self._io_lock:
            blob = self._load_blob()
            blob["providers"][provider_id] = credentials
            ok = self._save_blob(blob)
            if ok:
                logger.info(f"Credentials saved for provider: {provider_id}")
            return ok

    def load_all(self) -> dict[str, dict]:
        """Return all stored credentials: {provider_id: creds_dict, ...}."""
        with self._io_lock:
            blob = self._load_blob()
        return dict(blob.get("providers", {}))

    def load_provider(self, provider_id: str) -> Optional[dict]:
        """Return credentials for a specific provider, or None."""
        with self._io_lock:
            blob = self._load_blob()
        return blob.get("providers", {}).get(provider_id)

    def delete_provider(self, provider_id: str) -> bool:
        """Remove credentials for a single provider. Others are preserved."""
        with self._io_lock:
            blob = self._load_blob()
            providers = blob.get("providers", {})
            if provider_id not in providers:
                return True
            del providers[provider_id]
            ok = self._save_blob(blob)
            if ok:
                logger.info(f"Credentials deleted for provider: {provider_id}")
            return ok

    _EXPORT_VERSION_MARKER = b"SSv3"  # v3 format: machine-derived key

    def _export_key(self) -> bytes:
        """Derive a per-machine export key from the machine ID + installation salt.

        Not based on a static constant — each machine + installation
        produces a unique key, so exports are only importable on the
        same machine (which is the intended behavior, since credentials
        are machine-bound anyway).
        """
        import hashlib
        from core.machine import get_machine_id
        mid = get_machine_id()
        salt = _get_or_create_salt()
        return hashlib.sha256(f"SaveSync_Export_{mid}_{salt}".encode()).digest()

    _LEGACY_EXPORT_KEY_MATERIAL = "SaveSync_Export_v2"  # for importing old exports

    def export_credentials(self) -> Optional[str]:
        """Export all provider credentials as encrypted base64 string.

        The encryption key is derived from the machine ID + installation
        salt, so the export can only be decrypted on the same machine.
        """
        from datetime import datetime, timezone
        from core.machine import get_machine_id

        all_creds = self.load_all()
        if not all_creds:
            return None

        try:
            export_data = {
                "providers": all_creds,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "version": "3.0",
                "machine_id": get_machine_id()
            }

            raw = json.dumps(export_data).encode("utf-8")
            encrypted = encrypt_data(raw, self._export_key())
            blob = self._EXPORT_VERSION_MARKER + encrypted

            return base64.b64encode(blob).decode("utf-8")
        except Exception as e:
            logger.error(f"Credential export failed: {e}")
            return None

    def import_credentials(self, export_string: str) -> bool:
        """Import credentials from encrypted export string.

        Supports v3 (machine-derived key), v2 (legacy static key), and
        v1 formats for backward compatibility.
        """
        import hashlib

        try:
            blob = base64.b64decode(export_string)

            marker_v3 = self._EXPORT_VERSION_MARKER
            if blob[:len(marker_v3)] == marker_v3:
                # v3 format: machine-derived key
                encrypted = blob[len(marker_v3):]
                export_key = self._export_key()
            else:
                # Legacy v2/v1: static hardcoded key
                encrypted = blob
                export_key = hashlib.sha256(
                    self._LEGACY_EXPORT_KEY_MATERIAL.encode()
                ).digest()

            raw = decrypt_data(encrypted, export_key)
            data = json.loads(raw.decode("utf-8"))

            # v2/v3 export: {"providers": {...}, "version": "..."}
            if "providers" in data and isinstance(data["providers"], dict):
                ok = True
                for pid, creds in data["providers"].items():
                    if not self.save(pid, creds):
                        ok = False
                return ok

            # v1 export: {"provider": ..., "credentials": ..., "version": "1.1"}
            if not all(k in data for k in ["provider", "credentials", "version"]):
                raise ValueError("Invalid export format")
            return self.save(data["provider"], data["credentials"])
        except Exception as e:
            logger.error(f"Credential import failed: {e}")
            return False


_store: CredentialStore | None = None
_store_lock = threading.Lock()


def get_credential_store() -> CredentialStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CredentialStore()
    return _store
