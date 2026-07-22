"""
PyInstaller runtime hook — runs AFTER extraction, BEFORE main.py.

Single-instance enforcement via OS-native guards:
  - Windows: Named Mutex
  - Linux/macOS: flock on a lock file

Also exposes close_bootloader_splash() for main.py.
"""
import sys
import os
import json


def _close_splash():
    """Close the bootloader splash if still alive."""
    try:
        import pyi_splash
        if pyi_splash.is_alive():
            pyi_splash.close()
    except ImportError:
        pass


def _get_config_path() -> str:
    """Return the path to config.json (no heavy imports)."""
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "SaveSync", "config.json")
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "SaveSync", "config.json")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
        return os.path.join(xdg, "SaveSync", "config.json")


def _get_localised_msg() -> str:
    """Return a localised 'already running' message (best-effort)."""
    msg = "SaveSync is already running.\nCheck the system tray."
    try:
        cfg = _get_config_path()
        if os.path.isfile(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                lang = json.load(f).get("language", "en")
            msgs = {
                "it": "SaveSync è già in esecuzione.\nControlla la system tray.",
                "es": "SaveSync ya está en ejecución.\nRevisa la bandeja del sistema.",
                "fr": "SaveSync est déjà en cours d'exécution.\nVérifiez la barre des tâches.",
                "de": "SaveSync läuft bereits.\nÜberprüfen Sie die Taskleiste.",
                "pt": "SaveSync já está em execução.\nVerifique a bandeja do sistema.",
            }
            msg = msgs.get(lang, msg)
    except Exception:
        pass
    return msg


def _ping_primary_instance() -> bool:
    """Ask the already-running instance to bring itself to the foreground.
    Mirrors main._ping_running_instance — the pipe/socket name must match
    main._instance_show_name (per-user, non-word chars collapsed). The
    CONNECTION itself is the signal: the listener acts on newConnection and
    never reads, so nothing is written."""
    try:
        import re
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        name = "SaveSyncShow-" + re.sub(r"\W+", "_", user)
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # Explicit 64-bit-safe types: the default c_int restype truncates
            # INVALID_HANDLE_VALUE to -1 and valid handles could truncate too.
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            ERROR_PIPE_BUSY = 231
            INVALID = ctypes.c_void_p(-1).value
            path = "\\\\.\\pipe\\" + name
            handle = k32.CreateFileW(path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if handle in (None, INVALID) and ctypes.get_last_error() == ERROR_PIPE_BUSY:
                k32.WaitNamedPipeW(path, 1000)
                handle = k32.CreateFileW(path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if handle in (None, INVALID):
                return False
            k32.CloseHandle(handle)
            return True
        import socket
        import tempfile
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(1.0)
            s.connect(os.path.join(tempfile.gettempdir(), name))
        finally:
            s.close()
        return True
    except Exception:
        return False


def _show_and_exit(msg: str):
    """Close splash, show a native message, and exit."""
    _close_splash()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "SaveSync", 0x00000030)
    else:
        # stderr fallback — on Linux/macOS the Tcl level already showed tk_messageBox
        print(f"SaveSync: {msg}", file=sys.stderr)
    sys.exit(1)


def _check_single_instance():
    """Acquire an OS-native single-instance lock before main.py loads."""

    if sys.platform == "win32":
        # Windows: Named Mutex
        # Use WinDLL with use_last_error=True so ctypes atomically saves the
        # error code right after the FFI call.  Reading via the plain
        # ctypes.windll.kernel32.GetLastError() is unreliable because ctypes
        # may internally call other Windows APIs (e.g. GetProcAddress) that
        # overwrite the thread-local error before we can read it.
        import ctypes
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        mutex_name = "Global\\SaveSyncSingleInstance"
        handle = _kernel32.CreateMutexW(None, True, mutex_name)
        last_error = ctypes.get_last_error()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                _kernel32.CloseHandle(handle)
            # Second launch = "show me the app": ping the running instance's
            # focus pipe (served by main.py via QLocalServer) and exit
            # silently. Falls back to the message box when the ping can't be
            # delivered. Name must stay in sync with main._instance_show_name.
            if _ping_primary_instance():
                _close_splash()
                sys.exit(0)
            _show_and_exit(_get_localised_msg())
        sys._savesync_mutex = handle

    else:
        # Linux / macOS: flock
        try:
            import fcntl
        except ImportError:
            return  # platform without flock — rely on Tcl level only

        data_dir = os.path.dirname(_get_config_path())
        lock_path = os.path.join(data_dir, ".savesync.lock")
        try:
            os.makedirs(data_dir, exist_ok=True)
            fh = open(lock_path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            # Keep file handle alive for the whole process
            sys._savesync_lockfh = fh
            sys._savesync_lockpath = lock_path
        except (OSError, IOError):
            if _ping_primary_instance():
                _close_splash()
                sys.exit(0)
            _show_and_exit(_get_localised_msg())


# Execute only once — guard against re-import from main.py
if not getattr(sys, '_savesync_instance_checked', False):
    _check_single_instance()
    sys._savesync_instance_checked = True


# ── Public API for main.py ──────────────────────────────────────────────────

def _take_over_sentinel():
    """Re-open the sentinel file from Python so it stays locked after
    the Tcl interpreter (and its file handle) is shut down by pyi_splash.close().
    """
    data_dir = os.path.dirname(_get_config_path())
    sentinel = os.path.join(data_dir, ".savesync.running")
    try:
        # Keep handle alive — prevents file delete from other processes
        sys._savesync_sentinel_fh = open(sentinel, "a")
        sys._savesync_sentinel_path = sentinel
    except OSError:
        pass


def close_bootloader_splash():
    """Dismiss the bootloader splash screen (called by main.py).

    Before closing the Tcl interpreter, re-open the sentinel from Python
    so the file lock is never released.
    """
    _take_over_sentinel()
    _close_splash()
