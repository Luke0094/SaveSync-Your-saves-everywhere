"""
SaveSync - Registry-based save support (Windows).

Some games — Unity titles via PlayerPrefs above all — keep their progress
INSIDE registry values (HKCU\\Software\\<Company>\\<Product>) instead of, or
in addition to, files on disk. This module makes such saves first-class:

- Representation: a virtual save path ``registry:HKCU\\Software\\Co\\Game``
  stored alongside normal folder paths in GameEntry.save_paths. Everything
  that treats paths as plain strings (dedupe, containment, persistence,
  cloud sync of the backup zip) keeps working untouched; filesystem
  consumers guard with :func:`is_registry_path`.
- Backup: :func:`export_registry_key` serializes the key tree to canonical
  JSON (sorted, binary as base64) that the backup engine stores inside the
  zip under ``__registry__/``; identical trees produce identical bytes, so
  the per-file change detection keeps working for registry state too.
- Restore: :func:`import_registry_tree` re-creates the exported tree,
  REPLACING the current subtree so deleted prefs don't linger. Imports are
  hard-restricted to HKCU\\Software (never HKLM, never Microsoft/Classes/
  Policies subtrees) — a malformed or malicious backup must not be able to
  touch anything but a game's own key.
- Live tracking: :func:`registry_last_write` exposes the key tree's most
  recent last-write timestamp so the 60s poll can gate on real activity,
  exactly like the mtime gate for folders.
"""
import base64
import json
import logging
import platform
import re

from core.constants import match_slug
from typing import Optional

logger = logging.getLogger(__name__)

REG_PREFIX = "registry:"

# Only per-user software keys may ever be written (or exported — exporting
# elsewhere would just create backups that can never be restored).
_ALLOWED_ROOT = "HKCU"
_ALLOWED_SUBKEY_PREFIX = "software\\"
# Never touch these even under HKCU\Software: OS/shell state, not game saves.
_DENY_PREFIXES = (
    "software\\microsoft",
    "software\\classes",
    "software\\policies",
    "software\\wow6432node\\microsoft",
    "software\\wow6432node\\classes",
    "software\\wow6432node\\policies",
)

# Export sanity caps: a game prefs key is small; anything beyond this is a
# mis-detection (or registry vandalism) and must not balloon the backup.
_MAX_DEPTH = 5
_MAX_KEYS = 200
_MAX_VALUES = 2000
_MAX_TOTAL_BYTES = 5 * 1024 * 1024

_FILETIME_EPOCH_DELTA = 116444736000000000  # 100ns units, 1601→1970


def is_registry_path(path_str: str) -> bool:
    """True for virtual registry save paths (``registry:HKCU\\...``)."""
    return isinstance(path_str, str) and path_str.lower().startswith(REG_PREFIX)


def registry_display(path_str: str) -> str:
    """Human form without the scheme prefix (``HKCU\\Software\\Co\\Game``)."""
    return path_str[len(REG_PREFIX):] if is_registry_path(path_str) else path_str


def make_registry_path(subkey: str) -> str:
    """Build the canonical virtual path for an HKCU subkey."""
    return f"{REG_PREFIX}{_ALLOWED_ROOT}\\{subkey}"


def _parse(path_str: str) -> Optional[str]:
    """Validate + normalize a virtual path → HKCU subkey, or None.

    Enforces the HKCU\\Software restriction and the deny list for BOTH
    read and write operations — uniform is simpler and safer.
    """
    if not is_registry_path(path_str):
        return None
    body = path_str[len(REG_PREFIX):].strip().strip("\\")
    body = body.replace("/", "\\")
    parts = body.split("\\")
    if len(parts) < 3:          # need at least HKCU\Software\<key>
        return None
    root = parts[0].upper()
    if root in ("HKEY_CURRENT_USER", "HKCU"):
        subkey = "\\".join(parts[1:])
    else:
        return None
    low = subkey.lower()
    if not low.startswith(_ALLOWED_SUBKEY_PREFIX):
        return None
    if any(low == d or low.startswith(d + "\\") for d in _DENY_PREFIXES):
        return None
    if ".." in subkey or not re.fullmatch(r"[^\x00-\x1f]+", subkey):
        return None
    return subkey


def registry_key_exists(path_str: str) -> bool:
    if platform.system() != "Windows":
        return False
    subkey = _parse(path_str)
    if subkey is None:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def registry_has_values(path_str: str) -> bool:
    """True when the key tree holds at least one value (something to save)."""
    if platform.system() != "Windows":
        return False
    subkey = _parse(path_str)
    if subkey is None:
        return False
    import winreg

    def _walk(sk: str, depth: int) -> bool:
        if depth > _MAX_DEPTH:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
                n_sub, n_val, _ = winreg.QueryInfoKey(k)
                if n_val > 0:
                    return True
                for i in range(n_sub):
                    try:
                        child = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    if _walk(f"{sk}\\{child}", depth + 1):
                        return True
        except OSError:
            pass
        return False

    return _walk(subkey, 0)


def registry_value_count(path_str: str) -> int:
    """Total number of values across the key tree (bounded walk)."""
    if platform.system() != "Windows":
        return 0
    subkey = _parse(path_str)
    if subkey is None:
        return 0
    import winreg
    count = 0
    visited = 0

    def _walk(sk: str, depth: int):
        nonlocal count, visited
        if depth > _MAX_DEPTH or visited > _MAX_KEYS:
            return
        visited += 1
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
                n_sub, n_val, _ = winreg.QueryInfoKey(k)
                count += n_val
                for i in range(n_sub):
                    try:
                        child = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    _walk(f"{sk}\\{child}", depth + 1)
        except OSError:
            pass

    _walk(subkey, 0)
    return count


def registry_last_write(path_str: str) -> float:
    """Most recent last-write epoch across the key tree (0.0 on failure).

    Registry semantics: writing a value bumps only ITS key, not the
    parents — so the whole (bounded) tree is inspected, mirroring the
    one-level descent the folder recency gate does for files.
    """
    if platform.system() != "Windows":
        return 0.0
    subkey = _parse(path_str)
    if subkey is None:
        return 0.0
    import winreg
    latest = 0.0
    visited = 0

    def _walk(sk: str, depth: int):
        nonlocal latest, visited
        if depth > _MAX_DEPTH or visited > _MAX_KEYS:
            return
        visited += 1
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
                n_sub, _n_val, ft = winreg.QueryInfoKey(k)
                ts = (ft - _FILETIME_EPOCH_DELTA) / 1e7
                if ts > latest:
                    latest = ts
                for i in range(n_sub):
                    try:
                        child = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    _walk(f"{sk}\\{child}", depth + 1)
        except OSError:
            pass

    _walk(subkey, 0)
    return latest


def _encode_value(vdata, vtype) -> dict:
    import winreg
    if vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(vdata, str):
        return {"t": vtype, "s": vdata}
    if vtype == winreg.REG_MULTI_SZ and isinstance(vdata, list):
        return {"t": vtype, "m": [str(x) for x in vdata]}
    if vtype in (winreg.REG_DWORD, winreg.REG_QWORD) and isinstance(vdata, int):
        return {"t": vtype, "i": vdata}
    # REG_BINARY and anything exotic: raw bytes, base64
    if isinstance(vdata, bytes):
        raw = vdata
    elif isinstance(vdata, str):
        raw = vdata.encode("utf-16-le")
    elif isinstance(vdata, int):
        raw = vdata.to_bytes(8, "little", signed=False)
    elif vdata is None:
        raw = b""
    else:
        raw = str(vdata).encode("utf-8")
    return {"t": vtype, "b": base64.b64encode(raw).decode("ascii")}


def _decode_value(spec: dict):
    import winreg
    vtype = int(spec.get("t", winreg.REG_BINARY))
    if "s" in spec:
        return spec["s"], vtype
    if "m" in spec:
        return list(spec["m"]), vtype
    if "i" in spec:
        return int(spec["i"]), vtype
    return base64.b64decode(spec.get("b", "")), vtype


def export_registry_key(path_str: str) -> Optional[bytes]:
    """Serialize the key tree to canonical JSON bytes, or None.

    Canonical = sorted keys/values + no timestamps in the payload body, so
    identical registry state ⇒ identical bytes ⇒ the backup engine's
    content hashing detects real changes and skips no-op backups.
    """
    if platform.system() != "Windows":
        return None
    subkey = _parse(path_str)
    if subkey is None:
        logger.warning(f"Registry export refused (outside HKCU\\Software): {path_str}")
        return None
    import winreg
    total = {"bytes": 0, "values": 0, "keys": 0}

    def _walk(sk: str, depth: int) -> Optional[dict]:
        if depth > _MAX_DEPTH:
            logger.warning(f"Registry export: depth cap hit at {sk}")
            return None
        total["keys"] += 1
        if total["keys"] > _MAX_KEYS:
            raise ValueError("too many keys")
        node = {"values": {}, "subkeys": {}}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    vname, vdata, vtype = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                total["values"] += 1
                if total["values"] > _MAX_VALUES:
                    raise ValueError("too many values")
                enc = _encode_value(vdata, vtype)
                total["bytes"] += len(json.dumps(enc))
                if total["bytes"] > _MAX_TOTAL_BYTES:
                    raise ValueError("registry data too large")
                node["values"][vname] = enc
            i = 0
            while True:
                try:
                    child = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                sub = _walk(f"{sk}\\{child}", depth + 1)
                if sub is not None:
                    node["subkeys"][child] = sub
        return node

    try:
        tree = _walk(subkey, 0)
    except (OSError, ValueError) as e:
        logger.warning(f"Registry export failed for {path_str}: {e}")
        return None
    if tree is None:
        return None
    doc = {"format": 1, "key": f"{_ALLOWED_ROOT}\\{subkey}", "tree": tree}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _delete_tree(winreg, sk: str, depth: int = 0):
    if depth > _MAX_DEPTH + 2:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0,
                            winreg.KEY_READ) as k:
            children = []
            i = 0
            while True:
                try:
                    children.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        return
    for child in children:
        _delete_tree(winreg, f"{sk}\\{child}", depth + 1)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sk)
    except OSError:
        pass


def import_registry_tree(path_str: str, data: bytes) -> bool:
    """Restore an exported tree, replacing the current subtree.

    Replace (not merge) is what makes the restore FAITHFUL: prefs the game
    deleted after the backup would otherwise survive the rollback. The
    write target is re-validated from BOTH the virtual path and the
    payload's own recorded key — they must agree, and both must pass the
    HKCU\\Software + denylist gate.
    """
    if platform.system() != "Windows":
        return False
    subkey = _parse(path_str)
    if subkey is None:
        logger.error(f"Registry import refused (target outside HKCU\\Software): {path_str}")
        return False
    try:
        doc = json.loads(data.decode("utf-8"))
        recorded = str(doc.get("key", ""))
        tree = doc.get("tree")
    except Exception as e:
        logger.error(f"Registry import: malformed payload: {e}")
        return False
    rec_sub = _parse(REG_PREFIX + recorded)
    if rec_sub is None or rec_sub.lower() != subkey.lower():
        logger.error(
            f"Registry import refused: payload key {recorded!r} does not "
            f"match target {path_str!r}")
        return False
    if not isinstance(tree, dict):
        logger.error("Registry import: missing tree")
        return False
    import winreg

    def _write(sk: str, node: dict, depth: int):
        if depth > _MAX_DEPTH:
            return
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sk, 0,
                               winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY)
        try:
            for vname in sorted((node.get("values") or {}).keys()):
                try:
                    vdata, vtype = _decode_value(node["values"][vname])
                    winreg.SetValueEx(k, vname, 0, vtype, vdata)
                except OSError as e:
                    logger.warning(f"Registry import: value {vname!r} failed: {e}")
        finally:
            winreg.CloseKey(k)
        for child in sorted((node.get("subkeys") or {}).keys()):
            if re.fullmatch(r"[^\x00-\x1f\\]+", child):
                _write(f"{sk}\\{child}", node["subkeys"][child], depth + 1)

    try:
        _delete_tree(winreg, subkey)
        _write(subkey, tree, 0)
        logger.info(f"Registry restore completed for {registry_display(path_str)}")
        return True
    except OSError as e:
        logger.error(f"Registry import failed for {path_str}: {e}")
        return False


def open_in_regedit(path_str: str) -> bool:
    """Open regedit positioned at the virtual path's key (the standard
    Regedit LastKey mechanism). Returns False when the path is invalid."""
    if platform.system() != "Windows":
        return False
    subkey = _parse(path_str)
    if subkey is None:
        return False
    import subprocess
    import winreg
    try:
        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ,
                              f"Computer\\HKEY_CURRENT_USER\\{subkey}")
        subprocess.Popen(["regedit.exe"])
        return True
    except OSError as e:
        logger.warning(f"Could not open regedit at {path_str}: {e}")
        return False


def registry_arc_name(path_str: str) -> str:
    """Zip member name for a registry export (reserved __registry__/ area)."""
    body = registry_display(path_str)
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", body)[:180]
    return f"__registry__/{sanitized}.json"


def arc_name_is_registry(arc_name: str) -> bool:
    return arc_name.replace("\\", "/").startswith("__registry__/")


def find_registry_value_keys(terms: list[str]) -> list[str]:
    """Detect Unity-PlayerPrefs-style keys for a game: HKCU\\Software\\<X>
    or HKCU\\Software\\<Vendor>\\<Product> whose name slug matches a term
    EXACTLY and whose values include at least one non-string type (Unity
    stores every pref as REG_BINARY / REG_DWORD — a key with only string
    values is almost always install metadata, not save state).
    """
    if platform.system() != "Windows":
        return []
    import winreg
    slugs = set()
    for t in terms or []:
        s = match_slug((t or "").lower())
        if len(s) >= 4:
            slugs.add(s)
    if not slugs:
        return []

    def _slug(name: str) -> str:
        return match_slug(name)

    def _qualifies(sk: str) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
                i = 0
                while True:
                    try:
                        _vn, _vd, vtype = winreg.EnumValue(k, i)
                    except OSError:
                        return False
                    if vtype in (winreg.REG_BINARY, winreg.REG_DWORD, winreg.REG_QWORD):
                        return True
                    i += 1
        except OSError:
            return False

    found: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software", 0, winreg.KEY_READ) as soft:
            i = 0
            while True:
                try:
                    vendor = winreg.EnumKey(soft, i)
                except OSError:
                    break
                i += 1
                vlow = vendor.lower()
                if vlow in ("microsoft", "classes", "policies", "wow6432node"):
                    continue
                vpath = f"Software\\{vendor}"
                if _slug(vendor) in slugs and _qualifies(vpath):
                    found.append(make_registry_path(vpath))
                # one level deeper: Vendor\Product
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, vpath, 0,
                                        winreg.KEY_READ) as vk:
                        j = 0
                        while True:
                            try:
                                prod = winreg.EnumKey(vk, j)
                            except OSError:
                                break
                            j += 1
                            ppath = f"{vpath}\\{prod}"
                            if _slug(prod) in slugs and _qualifies(ppath):
                                found.append(make_registry_path(ppath))
                except OSError:
                    continue
    except OSError:
        pass
    return found


def registry_export_fingerprint(data: bytes) -> str:
    """Manifest fingerprint for an export blob — same shape the backup
    engine uses for files (size|mtime|hash); mtime slot is a constant
    because only content matters for registry state."""
    import hashlib
    return f"{len(data)}|reg|{hashlib.sha256(data).hexdigest()}"
