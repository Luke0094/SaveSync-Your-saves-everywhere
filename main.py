"""
SaveSync - Entry Point
"""
import sys
import logging
import os
from pathlib import Path

if sys.platform == "win32":
    import ctypes
else:
    try:
        import fcntl
    except ImportError:
        fcntl = None

# Same mutex name used by runtime_splash_hook.py
_MUTEX_NAME = "Global\\SaveSyncSingleInstance"

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.constants import LOGS_DIR, USER_DATA_DIR, APP_NAME, APP_VERSION


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "savesync.log"
    # Rotating handler: savesync.log is capped at 2 MB with 3 rotated
    # copies (savesync.log.1..3) — a plain FileHandler grew unbounded.
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file, encoding="utf-8",
        maxBytes=2 * 1024 * 1024, backupCount=3,
    )
    # Start every run in a FRESH savesync.log: if the previous run left a
    # non-empty log, roll it to savesync.log.1 (shifting .1→.2→.3) so the
    # CURRENT session is always a clean, self-contained file — quick for the
    # user to open and attach to a bug report. Mid-run size rotation (maxBytes)
    # still applies on top. A first run (no/empty log) is left as-is.
    try:
        if log_file.exists() and log_file.stat().st_size > 0:
            file_handler.doRollover()
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            file_handler,
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info(f"=== {APP_NAME} v{APP_VERSION} starting ===")
    if _tracing_enabled():
        _install_spawn_tracer()


def _tracing_enabled() -> bool:
    """Diagnostic tracing (spawn + window-show) is OPT-IN via
    SAVESYNC_TRACE=1: the app-wide event filter costs a Python call per
    Qt event, which measurably slows startup — with tracing off (the
    default) neither tracer is installed and the cost is zero."""
    return os.environ.get("SAVESYNC_TRACE") == "1"


def _install_spawn_tracer():
    """Log every child-process creation WITH its caller.

    The app is windowless (console=False): any subprocess started without
    CREATE_NO_WINDOW flashes a console for an instant. When that happens,
    this trace names the exact call site — no more guessing which library
    spawned it.
    """
    import traceback

    def _hook(event, args):
        if event not in ("subprocess.Popen", "os.system", "os.spawn",
                         "os.posix_spawn", "os.exec"):
            return
        try:
            # Innermost non-stdlib frame = the actual call site.
            stack = traceback.extract_stack()
            site = next(
                (f"{fr.filename}:{fr.lineno} in {fr.name}"
                 for fr in reversed(stack)
                 if "lib" not in fr.filename.lower()
                 or "site-packages" in fr.filename.lower()),
                "?",
            )
            logging.getLogger("spawn").info(
                f"child process via {event}: {args[0] if args else '?'} "
                f"— spawned from {site}")
        except Exception:
            pass

    sys.addaudithook(_hook)


# ── Single-instance lock ─────────────────────────────────────────────────────

_lock_file = None
_lock_handle = None    # file handle (Unix) or mutex handle (Windows)


def _acquire_lock() -> bool:
    """Acquire a single-instance lock. Returns False if another instance owns it.

    Only called when running from source (no runtime_splash_hook).
    In frozen builds the hook already acquired the lock and main() picks it up
    directly — this function is never reached.
    """
    global _lock_file, _lock_handle

    if sys.platform == "win32":
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = _kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        last_err = ctypes.get_last_error()
        if last_err == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                _kernel32.CloseHandle(handle)
            return False
        _lock_handle = handle
        return True

    # Unix: flock
    lock_path = USER_DATA_DIR / ".savesync.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_handle = open(lock_path, "a+")
        if fcntl is not None:
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.seek(0)
        _lock_handle.truncate()
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        _lock_file = lock_path
        return True
    except (OSError, IOError):
        if _lock_handle:
            try:
                _lock_handle.close()
            except OSError:
                pass
            _lock_handle = None
        return False


def _release_sentinel():
    """Close and delete the sentinel file used by the Tcl-level check."""
    fh = getattr(sys, '_savesync_sentinel_fh', None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass
        del sys._savesync_sentinel_fh
    path = getattr(sys, '_savesync_sentinel_path', None)
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass
        del sys._savesync_sentinel_path


def _release_lock():
    global _lock_file, _lock_handle

    _release_sentinel()

    if not _lock_handle:
        return

    if sys.platform == "win32":
        # Release Named Mutex (whether from runtime hook or local fallback)
        try:
            _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            _kernel32.ReleaseMutex(_lock_handle)
            _kernel32.CloseHandle(_lock_handle)
        except Exception:
            pass
        _lock_handle = None
        if hasattr(sys, '_savesync_mutex'):
            del sys._savesync_mutex
        return

    # Unix: release flock (whether from runtime hook or local fallback)
    try:
        if fcntl is not None:
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
        _lock_handle.close()
    except Exception:
        pass
    _lock_handle = None
    for attr in ('_savesync_lockfh', '_savesync_lockpath'):
        if hasattr(sys, attr):
            delattr(sys, attr)
    if _lock_file:
        try:
            _lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        _lock_file = None


def _instance_show_name() -> str:
    """Name of the local pipe/socket a second launch pings to focus the
    running instance. Per-user, so two Windows sessions never collide.
    Must stay in sync with the copy in runtime_splash_hook.py (frozen
    second instances ping from there, before any project import exists)."""
    import re
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "SaveSyncShow-" + re.sub(r"\W+", "_", user)


def _ping_running_instance() -> bool:
    """Ask the already-running instance to bring itself to the foreground.
    The CONNECTION itself is the signal — the listener acts on newConnection
    and never reads, so nothing is written (a write would block on Qt's
    zero-buffer pipe until the peer reads). Returns True when the ping was
    delivered — the caller exits silently."""
    name = _instance_show_name()
    try:
        if sys.platform == "win32":
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
                # All pipe instances momentarily taken — wait briefly, retry once
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
    except Exception as e:
        logging.debug(f"Running-instance ping failed: {e}")
        return False


# On Windows a QLocalServer does NOT see a raw CreateFileW client (Qt's
# QLocalSocket protocol needs an exchange the ctypes ping never performs), so
# the listener is a plain Win32 named pipe served from a thread. The ping
# above connects, we get the connection, the callback brings the window up.
def _start_show_pipe(callback) -> None:
    """Serve ``\\\\.\\pipe\\<show-name>`` on a daemon thread (Windows only)."""
    import threading
    import time

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = ctypes.c_void_p
    k32.CreateNamedPipeW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_void_p]
    k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    NMPWAIT_USE_DEFAULT_WAIT = 0x00000000
    INVALID = ctypes.c_void_p(-1).value
    ERROR_PIPE_CONNECTED = 535
    path = "\\\\.\\pipe\\" + _instance_show_name()

    def _serve():
        while True:
            try:
                handle = k32.CreateNamedPipeW(
                    path, PIPE_ACCESS_DUPLEX,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    1, 0, 0, NMPWAIT_USE_DEFAULT_WAIT, None)
            except Exception:
                time.sleep(0.5)
                continue
            if handle in (None, INVALID):
                time.sleep(0.5)
                continue
            try:
                ok = k32.ConnectNamedPipe(handle, None)
                if not ok and ctypes.get_last_error() == ERROR_PIPE_CONNECTED:
                    ok = True   # client connected between Create and Connect
                if ok:
                    callback()
            finally:
                k32.CloseHandle(handle)

    t = threading.Thread(target=_serve, name="show-pipe", daemon=True)
    t.start()


def _already_running_message() -> str:
    """The "already running" sentence, in the language on file.

    Qt is not up yet, so i18n proper is out of reach — but the locale files
    are plain JSON and the chosen language is one field in config.json.
    Falling back to English on any error keeps a failure here from being
    the reason the user sees nothing at all.
    """
    fallback = f"{APP_NAME} is already running.\nCheck the system tray."
    try:
        import json as _json
        cfg = USER_DATA_DIR / "config.json"
        lang = "en"
        if cfg.is_file():
            lang = _json.loads(cfg.read_text(encoding="utf-8-sig")).get(
                "language", "en") or "en"
        loc = Path(__file__).resolve().parent / "i18n" / "locales" / f"{lang}.json"
        if loc.is_file():
            data = _json.loads(loc.read_text(encoding="utf-8"))
            # Written whole rather than as ("app", "already_running"): the
            # key is reached without going through t(), and a tool looking
            # for unreferenced keys can only see the ones spelled out.
            section, _, leaf = "app.already_running".partition(".")
            msg = (data.get(section) or {}).get(leaf)
            if isinstance(msg, str) and msg.strip():
                return msg
    except Exception:
        pass
    return fallback


def _x_resource(rm: str, name: str):
    """One value out of an X resource-manager string, or None."""
    import re
    match = re.search(rf"^\s*{re.escape(name)}:\s*(\S+)\s*$", rm,
                      re.MULTILINE)
    return match.group(1) if match else None


def _seed_cursor_size():
    """Set XCURSOR_SIZE when no part of the desktop has set one.

    Nothing here overrides a choice. A session with a settings daemon, an
    Xcursor.size resource or XCURSOR_SIZE in the environment already knows
    what it wants and is left alone; this only fills the gap left by a
    session that says nothing — a bare compositor, a login without a
    desktop environment, WSLg — where the desktop draws its own 24 px
    pointer and never tells X clients about it.

    In that gap libXcursor falls back to a size derived from the display,
    and Qt to a bitmap of its own. Measured on a 3840-wide XWayland
    session: 64x64 inside SaveSync against a 24x24 pointer everywhere
    else, which is what "the cursor is enormous" looks like.

    The size follows the same rule the rest of the chrome does — 24 px at
    the 2560-DIP baseline, scaled by the app's own UI scale — so the
    pointer grows with the buttons instead of against them. It has to be
    decided BEFORE QApplication: the xcb plugin builds its cursor context
    while the platform integration starts, and reads none of this again.
    """
    if sys.platform in ("win32", "darwin"):
        return
    if os.environ.get("XCURSOR_SIZE") or not os.environ.get("DISPLAY"):
        return
    try:
        import ctypes                  # not imported at module level off Windows
        x11 = ctypes.CDLL("libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return
        try:
            # A settings daemon owns cursor appearance for the whole
            # session; anything set here would fight it.
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                        ctypes.c_int]
            x11.XGetSelectionOwner.restype = ctypes.c_ulong
            x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            atom = x11.XInternAtom(ctypes.c_void_p(dpy), b"_XSETTINGS_S0", 1)
            if atom and x11.XGetSelectionOwner(ctypes.c_void_p(dpy), atom):
                return

            x11.XResourceManagerString.restype = ctypes.c_char_p
            x11.XResourceManagerString.argtypes = [ctypes.c_void_p]
            rm = x11.XResourceManagerString(ctypes.c_void_p(dpy)) or b""
            rm = rm.decode("utf-8", "replace")
            if _x_resource(rm, "Xcursor.size"):
                return

            x11.XDefaultScreen.restype = ctypes.c_int
            x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
            x11.XDisplayWidth.restype = ctypes.c_int
            x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
            screen = x11.XDefaultScreen(ctypes.c_void_p(dpy))
            width = x11.XDisplayWidth(ctypes.c_void_p(dpy), screen)
            # An X screen is the whole desk, monitors and all: two 1080p
            # panels side by side report 3840 and would scale the pointer
            # as if it were 4K. ui_scale() works from one screen's work
            # area, so this asks RandR for the same thing.
            monitor = _primary_monitor_rect(x11, dpy, screen)
            if monitor:
                width = monitor[2]
        finally:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(ctypes.c_void_p(dpy))

        if width <= 0:
            return

        # Pixels to DIPs, the way Qt will do it a moment from now.
        dpi = 96.0
        raw = _x_resource(rm, "Xft.dpi")
        if raw:
            try:
                dpi = max(48.0, min(480.0, float(raw)))
            except ValueError:
                dpi = 96.0
        logical = width / (dpi / 96.0)

        scale = _configured_ui_scale()
        if scale is None:
            # ui_scale()'s auto rule and its readability clamp, without
            # importing Qt to get them.
            scale = max(0.50, min(4.00, logical / 2560.0))

        size = int(round(24 * scale))
        os.environ["XCURSOR_SIZE"] = str(max(16, min(96, size)))
        logging.info(f"No cursor size configured on this session — using "
                     f"{os.environ['XCURSOR_SIZE']}px "
                     f"({logical:.0f} DIP wide, UI scale {scale:.2f}).")
    except Exception as e:
        logging.debug(f"Could not seed the cursor size: {e}")


def _primary_monitor_rect(x11, dpy, screen):
    """``(x, y, width, height)`` of the primary monitor, or None.

    Falls back to the widest monitor when nothing is marked primary, which
    is what a session with one output and no desktop environment looks
    like. Any failure returns None and the caller keeps the X screen
    width — wrong on a multi-head desk, but never worse than not running.
    """
    import ctypes

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_ulong), ("primary", ctypes.c_int),
            ("automatic", ctypes.c_int), ("noutput", ctypes.c_int),
            ("x", ctypes.c_int), ("y", ctypes.c_int),
            ("width", ctypes.c_int), ("height", ctypes.c_int),
            ("mwidth", ctypes.c_int), ("mheight", ctypes.c_int),
            ("outputs", ctypes.c_void_p),
        ]

    try:
        xrandr = ctypes.CDLL("libXrandr.so.2")
        x11.XRootWindow.restype = ctypes.c_ulong
        x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        root = x11.XRootWindow(ctypes.c_void_p(dpy), screen)

        xrandr.XRRGetMonitors.restype = ctypes.POINTER(_MonitorInfo)
        xrandr.XRRGetMonitors.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_int)]
        count = ctypes.c_int(0)
        monitors = xrandr.XRRGetMonitors(ctypes.c_void_p(dpy), root, 1,
                                         ctypes.byref(count))
        if not monitors or count.value <= 0:
            return None
        try:
            widest = None
            for i in range(count.value):
                m = monitors[i]
                if m.width <= 0:
                    continue
                rect = (int(m.x), int(m.y), int(m.width), int(m.height))
                if m.primary:
                    return rect
                if widest is None or rect[2] > widest[2]:
                    widest = rect
            return widest
        finally:
            xrandr.XRRFreeMonitors.argtypes = [ctypes.POINTER(_MonitorInfo)]
            xrandr.XRRFreeMonitors(monitors)
    except Exception:
        return None


def _remember_primary_monitor():
    """Write the primary monitor's rectangle where the splash can find it.

    The bootloader splash is Tcl, drawn before Python exists, and Tk knows
    only the size of the whole X SCREEN. On a desk with two monitors that
    is both of them, so "centre of the screen" lands on the seam — or, on
    a session whose second output is virtual, well off to one side.

    Tcl cannot ask RandR, so the answer is left for it: the app writes the
    rectangle each time it starts, and the next launch's splash centres on
    it. The first launch on a new arrangement still uses the screen, which
    is exactly what it did before.
    """
    if sys.platform in ("win32", "darwin") or not os.environ.get("DISPLAY"):
        return
    try:
        import ctypes
        from core.constants import USER_DATA_DIR
        x11 = ctypes.CDLL("libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return
        try:
            x11.XDefaultScreen.restype = ctypes.c_int
            x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
            rect = _primary_monitor_rect(x11, dpy,
                                         x11.XDefaultScreen(ctypes.c_void_p(dpy)))
        finally:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(ctypes.c_void_p(dpy))
        if not rect:
            return
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        (USER_DATA_DIR / ".screen").write_text(
            "{} {} {} {}".format(*rect), encoding="utf-8")
    except Exception as e:
        logging.debug(f"Could not record the primary monitor: {e}")


def _configured_ui_scale():
    """The manual UI scale from config.json, or None when it is automatic.

    Read as plain JSON: get_config() wants a QApplication, and this runs
    before there is one.
    """
    try:
        import json
        from core.constants import CONFIG_FILE
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("ui_scale_auto", True):
            return None
        return max(0.50, min(1.50, float(data.get("ui_scale_factor", 1.0))))
    except Exception:
        return None


def _choose_qt_platform() -> None:
    """On Linux, prefer X11 unless the user has said otherwise.

    Not a preference about Wayland — a consequence of two things it does
    not allow, both of which SaveSync is built on:

      * a client cannot POSITION its own window. The overlay and the pins
        are placed against the game's window and the screen edges; under
        Wayland the compositor decides instead, and they land wherever it
        likes. That is the "overlay in a random place".
      * a client cannot GRAB a global shortcut. pynput registers the
        hotkey and reports success, and it never fires, because only the
        compositor sees keys that are not addressed to a focused window.

    XWayland gives both back, at the cost of nothing a user would notice,
    so it is chosen when an X display is there to be had. Anyone who wants
    Wayland regardless sets QT_QPA_PLATFORM themselves — this only ever
    fills in a value nobody chose.
    """
    if sys.platform in ("win32", "darwin"):
        return
    if os.environ.get("QT_QPA_PLATFORM"):
        return                      # an explicit choice is left alone
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY")
                      or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")
    if not on_wayland:
        return
    if os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        logging.info(
            "Wayland session with an X display available — using xcb, because "
            "Wayland allows neither window positioning (overlay, pins) nor a "
            "global hotkey. Set QT_QPA_PLATFORM=wayland to override."
        )
    else:
        logging.warning(
            "Wayland session with no X display: the overlay and the pins will "
            "be placed by the compositor rather than where SaveSync asks, and "
            "the global hotkey cannot be grabbed. Installing XWayland restores "
            "both."
        )


def _show_unix_msgbox(title: str, message: str) -> bool:
    """Put *message* on screen without Qt. True when something showed it.

    Qt is not up yet when this is needed — the single-instance check runs
    before QApplication exists — so this goes through whatever the desktop
    provides. Printing to stderr, which is what used to happen, is
    invisible to anyone who started SaveSync from an icon or a menu: a
    second launch simply appeared to do nothing.
    """
    import shutil
    import subprocess
    for name, args in (
        ("zenity", ["--warning", f"--title={title}", f"--text={message}"]),
        ("kdialog", [f"--title={title}", "--sorry", message]),
        ("xmessage", ["-center", message]),
        ("notify-send", [title, message]),
    ):
        path = shutil.which(name)
        if not path:
            continue
        try:
            subprocess.Popen([path, *args])
            return True
        except Exception:
            continue
    return False


def _show_native_msgbox(title: str, message: str):
    """Show a native message box without requiring Qt."""
    if sys.platform != "win32":
        if _show_unix_msgbox(title, message):
            return
    if sys.platform == "win32":
        import ctypes
        MB_OK = 0x00000000
        MB_ICONWARNING = 0x00000030
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_OK | MB_ICONWARNING)
    else:
        # Fallback: print to stderr on non-Windows platforms
        print(f"{title}: {message}", file=sys.stderr)


def _hook_already_acquired() -> bool:
    """Check if runtime_splash_hook already acquired the single-instance lock."""
    if sys.platform == "win32":
        return getattr(sys, '_savesync_mutex', None) is not None
    return getattr(sys, '_savesync_lockfh', None) is not None


def main():
    setup_logging()

    # ── Single-instance check (before ANY heavy import) ─────────────────────
    # In frozen builds the runtime_splash_hook already acquired the lock
    # and handled the "already running" case — skip the duplicate check.
    if _hook_already_acquired():
        global _lock_handle, _lock_file
        if sys.platform == "win32":
            _lock_handle = sys._savesync_mutex
        else:
            _lock_handle = sys._savesync_lockfh
            _lock_file = Path(getattr(sys, '_savesync_lockpath', ''))
    elif not _acquire_lock():
        # A second launch means "show me the app": ping the running instance
        # so it comes to the foreground, then exit silently. The message box
        # remains only as a fallback when the ping cannot be delivered
        # (instance older than this feature, pipe error).
        if _ping_running_instance():
            logging.info("Another instance is running — asked it to come to the foreground.")
            sys.exit(0)
        logging.warning("Another instance is already running — exiting.")
        # get_config() needs a QApplication that does not exist yet, but
        # the language does not: it is a field in a JSON file, and reading
        # it directly is what runtime_splash_hook already does for this
        # very message. Read here rather than importing that module, which
        # would run its own single-instance check on import.
        _show_native_msgbox(APP_NAME, _already_running_message())
        sys.exit(1)

    # Mark instance check as done so that a later `import runtime_splash_hook`
    # (used for close_bootloader_splash) does NOT re-run _check_single_instance,
    # which would see the mutex we already own and incorrectly exit with
    # "already running".
    sys._savesync_instance_checked = True

    # ── Second-launch focus listener, as early as possible ──────────────
    # A new instance pings a local pipe/socket and exits; we answer by
    # bringing the existing window to the foreground. Started BEFORE the
    # heavy imports so a relaunch during startup already finds the pipe.
    # The callback resolves `window` lazily: the pipe may receive a ping
    # before MainWindow exists, so the raise happens on the first call that
    # follows window creation.
    if sys.platform == "win32":
        from PySide6.QtCore import QObject, Signal
        _window_ref = {}

        class _ShowSignal(QObject):
            requested = Signal()

        _show_signal = _ShowSignal()

        def _raise_window():
            w = _window_ref.get("w")
            if w is not None:
                try:
                    w.show_and_raise()
                except Exception:
                    pass

        # The pipe runs on a daemon thread: emit is queued into the main
        # thread's event loop (receiver lives there), so Qt widget calls
        # always happen on the GUI thread.
        _show_signal.requested.connect(_raise_window)
        _start_show_pipe(_show_signal.requested.emit)
        _window_ref["w"] = None  # filled in below once MainWindow is created
    else:
        # QLocalServer serves AF_UNIX sockets on Linux/macOS; the plain
        # socket client matches here. newConnection fires on the GUI thread,
        # so a direct call is safe. The client (main._ping_running_instance
        # and runtime_splash_hook._ping_primary_instance) connects to
        # tempfile.gettempdir()/name, so the server must listen on that same
        # absolute path: QLocalServer's own temp path would not always be
        # the same directory tempfile picks.
        import tempfile
        from PySide6.QtNetwork import QLocalServer
        _window_ref = {}
        _show_name = os.path.join(tempfile.gettempdir(), _instance_show_name())

        def _on_second_instance():
            w = _window_ref.get("w")
            if w is not None:
                try:
                    w.show_and_raise()
                except Exception:
                    pass

        QLocalServer.removeServer(_show_name)
        _show_server = QLocalServer()

        def _on_qt_second_instance():
            while _show_server.hasPendingConnections():
                conn = _show_server.nextPendingConnection()
                if conn is not None:
                    conn.close()
                    conn.deleteLater()
            _on_second_instance()

        _show_server.newConnection.connect(_on_qt_second_instance)
        if not _show_server.listen(_show_name):
            logging.warning(
                f"Second-instance listener unavailable: {_show_server.errorString()}"
            )

    # Phase 1: filesystem-only startup (native splash already visible)
    from core.startup import ensure_data_directory, validate_directories, cleanup_temp_files, check_dependencies
    try:
        check_dependencies()
        ensure_data_directory()
        validate_directories()
        cleanup_temp_files()
    except Exception as e:
        logging.warning(f"Pre-Qt startup issues: {e}")

    # HighDPI policy must be set before QApplication is created
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    _choose_qt_platform()
    _seed_cursor_size()
    _remember_primary_monitor()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)

    # Qt paint-warning tracer (SAVESYNC_TRACE_PAINT=1 only). A
    # "QPainter::begin: Paint device returned engine == 0" storm names the
    # failure but not the caller, and the cascade of "Painter not active"
    # lines that follows is the same one call still trying to draw. Qt
    # writes these through its own message handler, so the Python stack that
    # produced them is only reachable from inside one — which is what this
    # installs. Off by default: it turns every Qt warning into a stack walk.
    if os.environ.get("SAVESYNC_TRACE_PAINT") == "1":
        import traceback as _tb
        from PySide6.QtCore import qInstallMessageHandler

        _paint_seen: set = set()

        def _paint_handler(mode, ctx, message):
            text = str(message)
            if "QPainter::begin" in text or "Paint device" in text:
                stack = "".join(_tb.format_stack()[:-1])
                key = stack[-400:]
                if key not in _paint_seen:      # one report per call site
                    _paint_seen.add(key)
                    logging.warning("Qt paint failure: %s\n%s", text, stack)
            else:
                logging.debug("Qt: %s", text)

        qInstallMessageHandler(_paint_handler)
        logging.info("Qt paint tracing enabled (SAVESYNC_TRACE_PAINT=1)")

    # Window-show tracer (SAVESYNC_TRACE=1 only): names every top-level
    # window the moment it is shown, WITH the Python call stack that
    # showed it — the way the startup "flash" (a parentless QLabel shown
    # for one frame) was pinpointed. Off by default: an app-wide event
    # filter costs a Python call per Qt event and slows startup.
    if _tracing_enabled():
        import traceback
        from PySide6.QtCore import QObject, QEvent
        from PySide6.QtWidgets import QWidget as QWidgetType

        class _ShowTracer(QObject):
            def eventFilter(self, obj, ev):
                try:
                    if (ev.type() == QEvent.Type.Show
                            and isinstance(obj, QWidgetType) and obj.isWindow()):
                        g = obj.geometry()
                        site = " <- ".join(
                            f"{fr.filename.rsplit('savesync', 1)[-1]}:{fr.lineno}"
                            for fr in traceback.extract_stack()[-6:-2]
                            if "savesync" in fr.filename)
                        logging.getLogger("ui-trace").info(
                            f"window shown: {obj.__class__.__name__}"
                            f" name={obj.objectName()!r}"
                            f" geom={g.x()},{g.y()} {g.width()}x{g.height()}"
                            f" flags={int(obj.windowFlags()):#x}"
                            f" from {site or '?'}")
                except Exception:
                    pass
                return False

        app.installEventFilter(_ShowTracer(app))

    app.processEvents()

    # Set app icon (.ico for proper Windows taskbar support)
    from PySide6.QtGui import QIcon
    _icon_candidates = [
        Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / "assets" / "icon.ico",
        Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / "assets" / "icon.png",
    ]
    for _ic in _icon_candidates:
        if _ic.exists():
            app.setWindowIcon(QIcon(str(_ic)))
            break

    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("SaveSync")
    app.setQuitOnLastWindowClosed(False)  # keep running in tray

    def _handle_savesync_url(url_str: str):
        from urllib.parse import unquote
        from core.resolvers import parse_launcher_url, launch_with_url
        url_str = unquote(url_str)
        parsed = parse_launcher_url(url_str)
        if parsed:
            appid = parsed.get("appid")
            if appid:
                logging.info(f"Launching game via URL: appid={appid}")
                launch_with_url(url_str)

    for arg in app.arguments():
        if arg.startswith("savesync://"):
            logging.info(f"Startup URL argument: {arg}")
            _handle_savesync_url(arg)

    try:
        from core.config_manager import get_config
        from i18n import get_engine
        from ui.styles.theme import get_theme_manager
        from ui.main_window import MainWindow

        # Phase 2: config + migrations (requires QApplication)
        from core.startup import setup_default_config, migrate_old_settings
        setup_default_config()
        migrate_old_settings()

        # Load config and apply initial settings
        config = get_config()

        # Apply language (with fallback to English if configured locale is invalid)
        lang = config.get("language", "en")
        engine = get_engine()
        engine.set_locale(lang)
        if engine.locale != lang:
            logging.warning(f"Configured language '{lang}' unavailable, using '{engine.locale}'")
            config.set("language", engine.locale)

        # Force Fusion style BEFORE theme to avoid overriding theme properties
        app.setStyle("Fusion")

        # Apply theme
        theme = config.get("theme", "dark")
        get_theme_manager().apply(theme, app)

        # Release the single-instance lock early in the shutdown sequence
        # so that a quick relaunch doesn't see "already running" while
        # _on_quit is still tearing down monitors.  Connected BEFORE
        # MainWindow (which connects _on_quit in _setup_cleanup), so Qt
        # fires _release_lock first.  The remaining cleanup (_on_quit) is
        # in-memory only (stop timers, disconnect signals) + an atomic
        # config.save(), so no conflict with the new process which still
        # needs extraction + Python load (seconds) before touching any
        # shared file.
        app.aboutToQuit.connect(_release_lock)

        # Undo a per-list page size that took the app down while rendering,
        # BEFORE any page is built with it — otherwise the same size crashes
        # the same list again and the setting can never be reached to change.
        from ui.widgets.page_size import recover_page_sizes
        recover_page_sizes()

        # Create main window (splash still visible during construction)
        window = MainWindow()

        # Dismiss bootloader splash BEFORE showing the main window, so
        # Windows creates the taskbar entry for the PyQt window (with icon)
        # rather than inheriting the no-icon state from the Tcl/Tk splash.
        try:
            import runtime_splash_hook
            runtime_splash_hook.close_bootloader_splash()
        except (ImportError, AttributeError):
            pass

        window.show()
        # Re-apply window icon after show() as a final refresh for Windows.
        _wi = app.windowIcon()
        if not _wi.isNull():
            window.setWindowIcon(_wi)

        from PySide6.QtGui import QDesktopServices
        QDesktopServices.setUrlHandler("savesync", window, "handleSavesyncUrl")

        # Hand the created window to the early second-launch listener.
        _window_ref["w"] = window

        sys.exit(app.exec())
    except Exception:
        # aboutToQuit may not fire on exception, so release lock here.
        # _release_lock is idempotent (checks _lock_handle is not None).
        _release_lock()
        raise


if __name__ == "__main__":
    main()
