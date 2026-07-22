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


def _show_native_msgbox(title: str, message: str):
    """Show a native Windows MessageBox without requiring Qt."""
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
        msg = f"{APP_NAME} is already running.\nCheck the system tray."
        # QApplication doesn't exist yet, so get_config() would raise.
        # Skip i18n attempt — use the English fallback message above.
        _show_native_msgbox(APP_NAME, msg)
        sys.exit(1)

    # Mark instance check as done so that a later `import runtime_splash_hook`
    # (used for close_bootloader_splash) does NOT re-run _check_single_instance,
    # which would see the mutex we already own and incorrectly exit with
    # "already running".
    sys._savesync_instance_checked = True

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

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)

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

        # ── Second-launch focus ──────────────────────────────────────────
        # A new instance pings this local socket and exits; we answer by
        # bringing the existing window to the foreground.
        from PySide6.QtNetwork import QLocalServer
        _show_name = _instance_show_name()
        QLocalServer.removeServer(_show_name)   # clear a stale socket/pipe
        _show_server = QLocalServer(window)

        def _on_second_instance():
            while _show_server.hasPendingConnections():
                conn = _show_server.nextPendingConnection()
                if conn is not None:
                    conn.close()
                    conn.deleteLater()
            window.show_and_raise()

        _show_server.newConnection.connect(_on_second_instance)
        if not _show_server.listen(_show_name):
            logging.warning(
                f"Second-instance listener unavailable: {_show_server.errorString()}"
            )

        sys.exit(app.exec())
    except Exception:
        # aboutToQuit may not fire on exception, so release lock here.
        # _release_lock is idempotent (checks _lock_handle is not None).
        _release_lock()
        raise


if __name__ == "__main__":
    main()
