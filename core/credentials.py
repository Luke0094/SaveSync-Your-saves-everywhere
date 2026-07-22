"""
SaveSync - Credential Store
Securely stores sync provider credentials using the OS keyring (Windows Credential Manager,
macOS Keychain, Linux Secret Service). Falls back to AES-256 encrypted local file if keyring
is unavailable.
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
_FALLBACK_PATH = USER_DATA_DIR / "credentials.enc"
_BACKUP_PATH = USER_DATA_DIR / "credentials.backup.enc"
_SALT_PATH = USER_DATA_DIR / "salt"


_salt_lock = threading.Lock()
_cached_salt: str | None = None

def _get_or_create_salt() -> str:
    """Get or create a per-installation random salt.
    Thread-safe: uses a lock to prevent two threads from generating
    different salts simultaneously.  Caches the salt in memory so that
    even if disk writes fail, the same salt is used within the session."""
    global _cached_salt
    with _salt_lock:
        if _cached_salt is not None:
            return _cached_salt
        if _SALT_PATH.exists():
            try:
                existing = _SALT_PATH.read_text(encoding="utf-8").strip()
                if existing:
                    _cached_salt = existing
                    return existing
            except Exception:
                pass
        import secrets
        salt = secrets.token_hex(16)
        try:
            from core import atomic_replace as _atomic_replace
            tmp_path = _SALT_PATH.with_name(_SALT_PATH.name + ".tmp")
            tmp_path.write_text(salt, encoding="utf-8")
            _atomic_replace(tmp_path, _SALT_PATH)
        except Exception:
            # If atomic write fails, try direct write as fallback
            try:
                _SALT_PATH.write_text(salt, encoding="utf-8")
            except Exception:
                logger.warning(
                    "Could not persist encryption salt to disk; "
                    "using ephemeral in-memory salt for this session"
                )
        _cached_salt = salt
        return salt

# Generate strong encryption key derived from machine ID and additional entropy
def _derive_encryption_key() -> bytes:
    """Derive a 256-bit encryption key from machine ID and system entropy."""
    from core.machine import get_machine_id
    import hashlib
    
    machine_id = get_machine_id()
    # Add system-specific entropy sources (no PID - it changes every run)
    entropy_sources = [
        machine_id,
        os.getenv("COMPUTERNAME", ""),
        os.getenv("USERNAME", ""),
        _get_or_create_salt()  # Per-installation random salt
    ]
    
    # Always include all sources (empty string instead of filtering)
    # to keep key derivation deterministic regardless of env var presence
    combined = "|".join(s or "" for s in entropy_sources).encode("utf-8")
    return hashlib.sha256(combined).digest()

def _derive_flexible_key() -> bytes:
    """Derive encryption key with fallback options for machine migration.

    Uses a different domain separator ("flexible") so the derived key is
    always distinct from the primary key, providing genuine migration
    resilience.  Falls back to more stable system identifiers when the
    primary sources are mostly empty.
    """
    from core.machine import get_machine_id
    import hashlib

    salt = _get_or_create_salt()

    primary_sources = [
        get_machine_id(),
        os.getenv("COMPUTERNAME", ""),
        os.getenv("USERNAME", ""),
        salt
    ]

    filtered = [s or "" for s in primary_sources]
    if sum(1 for s in filtered if s) >= 2:
        # Use a distinct domain separator so this key differs from _derive_encryption_key
        combined = ("flexible|" + "|".join(filtered)).encode("utf-8")
        return hashlib.sha256(combined).digest()

    # Fallback to more stable identifiers
    fallback_sources = [
        os.getenv("USERPROFILE", "") or os.getenv("HOME", ""),
        os.getenv("USERDOMAIN", ""),
        platform.node(),
        "SaveSync2024"
    ]
    combined = ("flexible|" + "|".join(filter(None, fallback_sources))).encode("utf-8")
    return hashlib.sha256(combined).digest()

def _create_backup_key() -> bytes:
    """Create a backup key using only stable system identifiers."""
    import hashlib
    
    # Use only very stable identifiers that rarely change
    stable_sources = [
        platform.node(),  # Computer name
        os.getenv("USERNAME", ""),  # Username
        "SaveSync2024"  # App salt
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
    """Try to decrypt with multiple keys for migration support."""
    keys_to_try = [
        (_derive_encryption_key(), "primary"),
        (_derive_flexible_key(), "flexible"),
        (_create_backup_key(), "backup")
    ]
    
    last_error = None
    for key, key_type in keys_to_try:
        try:
            decrypted = decrypt_data(encrypted_data, key)
            logger.debug(f"Successfully decrypted with {key_type} key")
            return decrypted, key_type
        except (ValueError, Exception) as e:
            # Log each attempt's failure for diagnostics
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
            logger.info(f"OS keyring unavailable - using obfuscated local fallback. Error: {type(e).__name__}: {e}")
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

                    if key_type != "primary" and file_path == _FALLBACK_PATH:
                        try:
                            new_key = _derive_encryption_key()
                            enc = encrypt_data(raw, new_key)
                            enc_b64 = base64.b64encode(enc)
                            from core import atomic_replace as _atomic_replace
                            tmp = _FALLBACK_PATH.with_suffix(".tmp")
                            tmp.write_bytes(enc_b64)
                            _atomic_replace(tmp, _FALLBACK_PATH)
                            logger.info("Re-encryption with primary key succeeded")
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
            raw = payload.encode("utf-8")
            key = _derive_encryption_key()
            encrypted = encrypt_data(raw, key)
            encoded = base64.b64encode(encrypted)
            from core import atomic_replace as _atomic_replace
            tmp_fallback = _FALLBACK_PATH.with_suffix(".tmp")
            tmp_fallback.write_bytes(encoded)
            _atomic_replace(tmp_fallback, _FALLBACK_PATH)

            try:
                backup_key = _create_backup_key()
                backup_encrypted = encrypt_data(raw, backup_key)
                backup_encoded = base64.b64encode(backup_encrypted)
                tmp_backup = _BACKUP_PATH.with_suffix(".tmp")
                tmp_backup.write_bytes(backup_encoded)
                _atomic_replace(tmp_backup, _BACKUP_PATH)
            except Exception as e:
                logger.debug(f"Could not create backup: {e}")

            return True
        except Exception as e:
            logger.error(f"Credential save failed: {e}")
            return False

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
