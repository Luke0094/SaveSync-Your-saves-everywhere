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
import os
import platform
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

from core.constants import match_slug

logger = logging.getLogger(__name__)

REG_PREFIX = "registry:"

# Standard Windows Registry Types
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_MULTI_SZ = 7
REG_QWORD = 11

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


# ── Wine / Proton user.reg Support (Linux & macOS) ──────────────────────────

def _find_wine_user_reg_files() -> List[Path]:
    """Find user.reg files from Wine and Steam Proton prefixes."""
    found: List[Path] = []
    seen: set[str] = set()

    # 1. Custom WINEPREFIX
    wp = os.environ.get("WINEPREFIX")
    if wp:
        p = Path(wp) / "user.reg"
        if p.is_file():
            found.append(p)
            seen.add(str(p.resolve()))

    home = Path.home()
    # 2. Standard ~/.wine
    std_wine = home / ".wine" / "user.reg"
    if std_wine.is_file() and str(std_wine.resolve()) not in seen:
        found.append(std_wine)
        seen.add(str(std_wine.resolve()))

    # 3. Steam Proton prefixes from Steam library roots
    candidates = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        home / "Library" / "Application Support" / "Steam",
    ]
    all_steam_roots: List[Path] = []
    for s_root in candidates:
        if not s_root.is_dir():
            continue
        all_steam_roots.append(s_root)
        vdf = s_root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                    lp = Path(m.group(1))
                    if lp.is_dir() and lp not in all_steam_roots:
                        all_steam_roots.append(lp)
            except Exception:
                pass

    for s_root in all_steam_roots:
        compat_dir = s_root / "steamapps" / "compatdata"
        if compat_dir.is_dir():
            try:
                for app_entry in compat_dir.iterdir():
                    if app_entry.is_dir():
                        pfx_reg = app_entry / "pfx" / "user.reg"
                        if pfx_reg.is_file():
                            r_str = str(pfx_reg.resolve())
                            if r_str not in seen:
                                found.append(pfx_reg)
                                seen.add(r_str)
            except Exception:
                pass

    return found


def _parse_wine_value(raw_val: str) -> Tuple[Any, int]:
    """Parse a Wine .reg value line into (Python value, REG_* type)."""
    raw_val = raw_val.strip()
    if raw_val.startswith('"') and raw_val.endswith('"'):
        s = raw_val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        return s, REG_SZ
    elif raw_val.startswith('dword:'):
        hex_s = raw_val[6:].strip()
        try:
            return int(hex_s, 16), REG_DWORD
        except ValueError:
            return 0, REG_DWORD
    elif raw_val.startswith('hex(b):'):
        hex_bytes = raw_val[7:].replace('\\', '').replace('\n', '').replace(' ', '').split(',')
        try:
            b = bytes(int(x, 16) for x in hex_bytes if x)
            val = int.from_bytes(b, "little", signed=False)
            return val, REG_QWORD
        except Exception:
            return 0, REG_QWORD
    elif raw_val.startswith('hex(7):'):
        hex_bytes = raw_val[7:].replace('\\', '').replace('\n', '').replace(' ', '').split(',')
        try:
            b = bytes(int(x, 16) for x in hex_bytes if x)
            decoded = b.decode("utf-16-le", errors="ignore")
            parts = [p for p in decoded.split('\x00') if p]
            return parts, REG_MULTI_SZ
        except Exception:
            return [], REG_MULTI_SZ
    elif raw_val.startswith('hex(2):'):
        hex_bytes = raw_val[7:].replace('\\', '').replace('\n', '').replace(' ', '').split(',')
        try:
            b = bytes(int(x, 16) for x in hex_bytes if x)
            s = b.decode("utf-16-le", errors="ignore").rstrip('\x00')
            return s, REG_EXPAND_SZ
        except Exception:
            return "", REG_EXPAND_SZ
    elif raw_val.startswith('hex:'):
        hex_bytes = raw_val[4:].replace('\\', '').replace('\n', '').replace(' ', '').split(',')
        try:
            b = bytes(int(x, 16) for x in hex_bytes if x)
            return b, REG_BINARY
        except ValueError:
            return b"", REG_BINARY
    elif raw_val.startswith('str(2):"') and raw_val.endswith('"'):
        s = raw_val[7:-1].replace('\\"', '"').replace('\\\\', '\\')
        return s, REG_EXPAND_SZ
    return raw_val, REG_SZ


def _format_wine_value(vname: str, encoded_spec: dict) -> str:
    """Format a value spec back into Wine .reg line syntax."""
    vtype = encoded_spec.get("t", REG_BINARY)
    prefix = f'"{vname}"' if vname else "@"
    if vtype == REG_SZ:
        s = encoded_spec.get("s", "")
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'{prefix}="{escaped}"'
    elif vtype == REG_DWORD:
        i = int(encoded_spec.get("i", 0)) & 0xFFFFFFFF
        return f'{prefix}=dword:{i:08x}'
    elif vtype == REG_QWORD:
        i = int(encoded_spec.get("i", 0))
        b = i.to_bytes(8, "little", signed=False)
        hex_str = ",".join(f"{x:02x}" for x in b)
        return f'{prefix}=hex(b):{hex_str}'
    elif vtype == REG_MULTI_SZ:
        m = encoded_spec.get("m", [])
        raw = ("\x00".join(m) + "\x00\x00").encode("utf-16-le")
        hex_str = ",".join(f"{x:02x}" for x in raw)
        return f'{prefix}=hex(7):{hex_str}'
    elif vtype == REG_EXPAND_SZ:
        s = encoded_spec.get("s", "")
        raw = (s + "\x00").encode("utf-16-le")
        hex_str = ",".join(f"{x:02x}" for x in raw)
        return f'{prefix}=hex(2):{hex_str}'
    else:  # REG_BINARY
        raw = base64.b64decode(encoded_spec.get("b", ""))
        hex_str = ",".join(f"{x:02x}" for x in raw)
        return f'{prefix}=hex:{hex_str}'


def _parse_wine_user_reg(reg_file: Path) -> Dict[str, Dict[str, Any]]:
    """Parse user.reg sections and values into a dictionary structure."""
    sections: Dict[str, Dict[str, Any]] = {}
    current_sec = None
    current_vals = None
    buffer = ""

    def _flush_buffer(buf: str, target_dict: dict):
        eq = buf.find("=")
        if eq < 0:
            return
        left = buf[:eq].strip()
        right = buf[eq + 1:].strip()
        vname = ""
        if left.startswith('"') and left.endswith('"'):
            vname = left[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        elif left == "@":
            vname = ""
        else:
            vname = left
        val, vtype = _parse_wine_value(right)
        target_dict[vname] = (val, vtype)

    try:
        with open(reg_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_s = line.strip()
                if not line_s or line_s.startswith(";;"):
                    continue
                if line_s.startswith("[") and "]" in line_s:
                    if buffer and current_vals is not None:
                        _flush_buffer(buffer, current_vals)
                        buffer = ""
                    sec_raw = line_s[1:line_s.index("]")].replace("/", "\\")
                    current_sec = re.sub(r"\\+", r"\\", sec_raw).strip("\\")
                    current_vals = {}
                    sections[current_sec.lower()] = {
                        "orig_name": current_sec,
                        "values": current_vals,
                        "time": 0.0,
                    }
                    continue

                if current_vals is None:
                    continue

                if line_s.startswith("#time="):
                    try:
                        hex_time = line_s[6:].strip()
                        ft = int(hex_time, 16)
                        sections[current_sec.lower()]["time"] = (ft - _FILETIME_EPOCH_DELTA) / 1e7
                    except Exception:
                        pass
                    continue
                elif line_s.startswith("#"):
                    continue

                if buffer:
                    buffer += line_s
                else:
                    buffer = line_s

                if buffer.endswith("\\"):
                    buffer = buffer[:-1].strip()
                    continue
                else:
                    _flush_buffer(buffer, current_vals)
                    buffer = ""

            if buffer and current_vals is not None:
                _flush_buffer(buffer, current_vals)
    except Exception as e:
        logger.debug(f"Could not parse Wine user.reg {reg_file}: {e}")

    return sections


def _wine_build_tree(sections: dict, subkey_lower: str, depth: int = 0) -> Optional[dict]:
    if depth > _MAX_DEPTH:
        return None
    node = {"values": {}, "subkeys": {}}
    sec_data = sections.get(subkey_lower)
    if sec_data:
        for vname, (val, vtype) in sec_data["values"].items():
            enc = _encode_value(val, vtype)
            node["values"][vname] = enc

    prefix = subkey_lower + "\\"
    for k_low, sdata in sections.items():
        if k_low.startswith(prefix):
            rel = k_low[len(prefix):]
            if "\\" not in rel:
                orig_child = sdata["orig_name"].split("\\")[-1]
                child_tree = _wine_build_tree(sections, k_low, depth + 1)
                if child_tree is not None:
                    node["subkeys"][orig_child] = child_tree
    return node


# ── Public Registry Inspection & Backup APIs ───────────────────────────────

def registry_key_exists(path_str: str) -> bool:
    subkey = _parse(path_str)
    if subkey is None:
        return False
    if platform.system() == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ):
                return True
        except OSError:
            return False
    else:
        sk_low = subkey.lower()
        for reg_file in _find_wine_user_reg_files():
            sections = _parse_wine_user_reg(reg_file)
            if sk_low in sections or any(k.startswith(sk_low + "\\") for k in sections):
                return True
        return False


def registry_has_values(path_str: str) -> bool:
    """True when the key tree holds at least one value (something to save)."""
    subkey = _parse(path_str)
    if subkey is None:
        return False
    if platform.system() == "Windows":
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
    else:
        sk_low = subkey.lower()
        for reg_file in _find_wine_user_reg_files():
            sections = _parse_wine_user_reg(reg_file)
            for k_low, sdata in sections.items():
                if k_low == sk_low or k_low.startswith(sk_low + "\\"):
                    if len(sdata["values"]) > 0:
                        return True
        return False


def registry_value_count(path_str: str) -> int:
    """Total number of values across the key tree (bounded walk)."""
    subkey = _parse(path_str)
    if subkey is None:
        return 0
    if platform.system() == "Windows":
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
    else:
        sk_low = subkey.lower()
        total_count = 0
        for reg_file in _find_wine_user_reg_files():
            sections = _parse_wine_user_reg(reg_file)
            c = 0
            for k_low, sdata in sections.items():
                if k_low == sk_low or k_low.startswith(sk_low + "\\"):
                    c += len(sdata["values"])
            if c > total_count:
                total_count = c
        return total_count


def registry_last_write(path_str: str) -> float:
    """Most recent last-write epoch across the key tree (0.0 on failure)."""
    subkey = _parse(path_str)
    if subkey is None:
        return 0.0
    if platform.system() == "Windows":
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
    else:
        sk_low = subkey.lower()
        latest = 0.0
        for reg_file in _find_wine_user_reg_files():
            sections = _parse_wine_user_reg(reg_file)
            for k_low, sdata in sections.items():
                if k_low == sk_low or k_low.startswith(sk_low + "\\"):
                    t = sdata.get("time", 0.0) or reg_file.stat().st_mtime
                    if t > latest:
                        latest = t
        return latest


def _encode_value(vdata, vtype) -> dict:
    if vtype in (REG_SZ, REG_EXPAND_SZ) and isinstance(vdata, str):
        return {"t": vtype, "s": vdata}
    if vtype == REG_MULTI_SZ and isinstance(vdata, list):
        return {"t": vtype, "m": [str(x) for x in vdata]}
    if vtype in (REG_DWORD, REG_QWORD) and isinstance(vdata, int):
        return {"t": vtype, "i": vdata}
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
    vtype = int(spec.get("t", REG_BINARY))
    if "s" in spec:
        return spec["s"], vtype
    if "m" in spec:
        return list(spec["m"]), vtype
    if "i" in spec:
        return int(spec["i"]), vtype
    return base64.b64decode(spec.get("b", "")), vtype


def export_registry_key(path_str: str) -> Optional[bytes]:
    """Serialize the key tree to canonical JSON bytes, or None."""
    subkey = _parse(path_str)
    if subkey is None:
        logger.warning(f"Registry export refused (outside HKCU\\Software): {path_str}")
        return None

    if platform.system() == "Windows":
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
    else:
        tree = _wine_export_registry_key(subkey)

    if tree is None:
        return None
    doc = {"format": 1, "key": f"{_ALLOWED_ROOT}\\{subkey}", "tree": tree}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _wine_export_registry_key(subkey: str) -> Optional[dict]:
    sk_low = subkey.lower()
    tree = None
    for reg_file in _find_wine_user_reg_files():
        sections = _parse_wine_user_reg(reg_file)
        if sk_low in sections or any(k.startswith(sk_low + "\\") for k in sections):
            tree = _wine_build_tree(sections, sk_low, 0)
            if tree:
                break
    return tree


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


def _wine_import_registry_tree(subkey: str, tree: dict) -> bool:
    target_files = _find_wine_user_reg_files()
    if not target_files:
        logger.warning(f"No Wine/Proton user.reg found to import {subkey}")
        return False

    def _collect_sections(prefix_key: str, node: dict, out_dict: dict):
        vals = []
        for vname in sorted((node.get("values") or {}).keys()):
            vals.append(_format_wine_value(vname, node["values"][vname]))
        out_dict[prefix_key] = vals
        for child_name, child_node in (node.get("subkeys") or {}).items():
            if re.fullmatch(r"[^\x00-\x1f\\]+", child_name):
                _collect_sections(f"{prefix_key}\\{child_name}", child_node, out_dict)

    new_sections = {}
    _collect_sections(subkey, tree, new_sections)

    success = False
    target_prefix = subkey.lower()
    for reg_file in target_files:
        try:
            lines = []
            with open(reg_file, "r", encoding="utf-8", errors="ignore") as f:
                skipping = False
                for line in f:
                    line_s = line.strip()
                    if line_s.startswith("[") and "]" in line_s:
                        sec_raw = line_s[1:line_s.index("]")].replace("/", "\\")
                        sec = re.sub(r"\\+", r"\\", sec_raw).strip("\\").lower()
                        if sec == target_prefix or sec.startswith(target_prefix + "\\"):
                            skipping = True
                            continue
                        else:
                            skipping = False
                    if not skipping:
                        lines.append(line)

            # Append new sections with Wine-standard double backslash header
            lines.append("\n")
            for sec_name, vals in new_sections.items():
                wine_sec_header = sec_name.replace("\\", "\\\\")
                lines.append(f"[{wine_sec_header}]\n")
                for val_line in vals:
                    lines.append(f"{val_line}\n")
                lines.append("\n")

            tmp_path = reg_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            tmp_path.replace(reg_file)
            success = True
            logger.info(f"Wine registry restore completed in {reg_file} for {subkey}")
        except Exception as e:
            logger.error(f"Failed writing to Wine user.reg {reg_file}: {e}")

    return success


def import_registry_tree(path_str: str, data: bytes) -> bool:
    """Restore an exported tree, replacing the current subtree."""
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

    if platform.system() == "Windows":
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
    else:
        return _wine_import_registry_tree(subkey, tree)



def open_in_regedit(path_str: str) -> bool:
    """Open regedit positioned at the virtual path's key."""
    subkey = _parse(path_str)
    if subkey is None:
        return False
    if platform.system() == "Windows":
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
    else:
        # On Linux, open the folder containing the user.reg file
        for reg_file in _find_wine_user_reg_files():
            from ui.helpers import open_in_file_manager
            open_in_file_manager(reg_file.parent)
            return True
        return False


def registry_arc_name(path_str: str) -> str:
    """Zip member name for a registry export (reserved __registry__/ area)."""
    body = registry_display(path_str)
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", body)[:180]
    return f"__registry__/{sanitized}.json"


def arc_name_is_registry(arc_name: str) -> bool:
    return arc_name.replace("\\", "/").startswith("__registry__/")


def find_registry_value_keys(terms: list[str]) -> list[str]:
    """Detect Unity-PlayerPrefs-style keys for a game."""
    slugs = set()
    for t in terms or []:
        s = match_slug((t or "").lower())
        if len(s) >= 4:
            slugs.add(s)
    if not slugs:
        return []

    def _slug(name: str) -> str:
        return match_slug(name)

    found: list[str] = []
    seen: set[str] = set()

    if platform.system() == "Windows":
        import winreg

        def _qualifies(sk: str) -> bool:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sk, 0, winreg.KEY_READ) as k:
                    i = 0
                    while True:
                        try:
                            _vn, _vd, vtype = winreg.EnumValue(k, i)
                        except OSError:
                            return False
                        if vtype in (REG_BINARY, REG_DWORD, REG_QWORD):
                            return True
                        i += 1
            except OSError:
                return False

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
                        rp = make_registry_path(vpath)
                        if rp not in seen:
                            found.append(rp)
                            seen.add(rp)
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
                                    rp = make_registry_path(ppath)
                                    if rp not in seen:
                                        found.append(rp)
                                        seen.add(rp)
                    except OSError:
                        continue
        except OSError:
            pass
    else:
        for reg_file in _find_wine_user_reg_files():
            sections = _parse_wine_user_reg(reg_file)
            for k_low, sdata in sections.items():
                if not k_low.startswith("software\\"):
                    continue
                parts = sdata["orig_name"].split("\\")
                if len(parts) < 2:
                    continue
                vendor = parts[1]
                if vendor.lower() in ("microsoft", "classes", "policies", "wow6432node"):
                    continue

                # Check if values contain binary/dword types
                has_non_string = any(vtype in (REG_BINARY, REG_DWORD, REG_QWORD)
                                     for _val, vtype in sdata["values"].values())
                if not has_non_string:
                    continue

                if len(parts) == 2:
                    if _slug(vendor) in slugs:
                        rp = make_registry_path(sdata["orig_name"])
                        if rp not in seen:
                            found.append(rp)
                            seen.add(rp)
                elif len(parts) == 3:
                    prod = parts[2]
                    if _slug(prod) in slugs or _slug(vendor) in slugs:
                        rp = make_registry_path(sdata["orig_name"])
                        if rp not in seen:
                            found.append(rp)
                            seen.add(rp)

    return found


def registry_export_fingerprint(data: bytes) -> str:
    """Manifest fingerprint for an export blob."""
    import hashlib
    return f"{len(data)}|reg|{hashlib.sha256(data).hexdigest()}"
