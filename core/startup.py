"""
SaveSync - Startup Manager
Registers/unregisters the application for launch at system boot.
Supports: Windows (registry), Linux (XDG autostart), macOS (LaunchAgent).
"""
import logging
import os
import platform
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from core.constants import APP_NAME, APP_ID

logger = logging.getLogger(__name__)


def _get_exe() -> str:
    """Return the path of the running executable (or python main.py fallback).

    On Windows, quotes both paths so registry Run keys work with spaces.
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if platform.system() == "Windows" and " " in exe:
            return f'"{exe}"'
        return exe
    # Running from source: use python + main.py path
    main_py = str(Path(sys.argv[0]).resolve())
    py = sys.executable
    if platform.system() == "Windows":
        # Registry Run keys need quoted paths when they contain spaces
        if " " in py:
            py = f'"{py}"'
        if " " in main_py:
            main_py = f'"{main_py}"'
    return f'{py} {main_py}'


# ── Windows ──────────────────────────────────────────────────────────────────

def _win_set(enable: bool) -> bool:
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path,
            0, winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _get_exe())
                logger.info(f"Startup entry created in HKCU Run: {_get_exe()}")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    logger.info("Startup entry removed from HKCU Run")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        logger.error(f"Windows startup registry error: {e}")
        return False


def _win_get() -> bool:
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            try:
                winreg.QueryValueEx(key, APP_NAME)
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


# ── Linux (XDG autostart) ─────────────────────────────────────────────────────

def _linux_autostart_path() -> Path:
    autostart_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    return autostart_dir / f"{APP_ID}.desktop"


def _linux_set(enable: bool) -> bool:
    try:
        path = _linux_autostart_path()
        if enable:
            exe = _get_exe()
            import i18n
            content = (
                "[Desktop Entry]\n"
                f"Name={APP_NAME}\n"
                f"Exec={exe}\n"
                "Type=Application\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                f"Comment={APP_NAME} - {i18n.t('app.game_save_manager')}\n"
            )
            path.write_text(content, encoding="utf-8")
            logger.info(f"Autostart .desktop created: {path}")
        else:
            if path.exists():
                path.unlink()
                logger.info(f"Autostart .desktop removed: {path}")
        return True
    except Exception as e:
        logger.error(f"Linux autostart error: {e}")
        return False


def _linux_get() -> bool:
    return _linux_autostart_path().exists()


# ── macOS (LaunchAgent) ───────────────────────────────────────────────────────

def _macos_plist_path() -> Path:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    return launch_agents / f"{APP_ID}.plist"


def _macos_set(enable: bool) -> bool:
    try:
        import subprocess
        path = _macos_plist_path()
        if enable:
            import shlex
            exe_parts = shlex.split(_get_exe())
            program_args = "".join(f"        <string>{xml_escape(p.strip('\"'))}</string>\n" for p in exe_parts)
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                f'    <key>Label</key><string>{APP_ID}</string>\n'
                f'    <key>ProgramArguments</key>\n    <array>\n{program_args}    </array>\n'
                '    <key>RunAtLoad</key><true/>\n'
                '    <key>KeepAlive</key><false/>\n'
                '</dict>\n</plist>\n'
            )
            path.write_text(plist, encoding="utf-8")
            subprocess.run(["launchctl", "load", str(path)], check=False)
            logger.info(f"LaunchAgent plist created: {path}")
        else:
            if path.exists():
                subprocess.run(["launchctl", "unload", str(path)], check=False)
                path.unlink()
                logger.info(f"LaunchAgent plist removed: {path}")
        return True
    except Exception as e:
        logger.error(f"macOS LaunchAgent error: {e}")
        return False


def _macos_get() -> bool:
    return _macos_plist_path().exists()


# ── Public API ────────────────────────────────────────────────────────────────

def set_launch_on_startup(enable: bool) -> bool:
    """Enable or disable launching SaveSync at system boot. Returns True on success."""
    system = platform.system()
    if system == "Windows":
        return _win_set(enable)
    elif system == "Linux":
        return _linux_set(enable)
    elif system == "Darwin":
        return _macos_set(enable)
    else:
        logger.warning(f"Launch on startup not supported on {system}")
        return False


def get_launch_on_startup() -> bool:
    """Return True if SaveSync is registered to launch on startup."""
    system = platform.system()
    if system == "Windows":
        return _win_get()
    elif system == "Linux":
        return _linux_get()
    elif system == "Darwin":
        return _macos_get()
    return False


# ── Additional Startup Functions ─────────────────────────────────────────────────────

def ensure_data_directory() -> Path:
    """Ensure the application data directory exists and return its path."""
    from core.constants import _user_data_dir
    data_dir = _user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def setup_default_config() -> None:
    """Setup default configuration if it doesn't exist.
    ConfigManager already loads defaults on init, so just trigger a load."""
    from core.config_manager import get_config
    from core.constants import _user_data_dir

    try:
        # Loading the config is sufficient — ConfigManager merges _DEFAULTS
        # with any existing config.json, so all keys are always present.
        get_config()
    except Exception as e:
        logger.error(f"Failed to setup default config: {e}")
        # Create basic config file manually as fallback
        try:
            data_dir = _user_data_dir()
            config_file = data_dir / "config.json"

            if not config_file.exists():
                import json
                default_config = {
                    "language": "en",
                    "theme": "dark",
                }
                config_file.write_text(json.dumps(default_config, indent=2))

        except Exception as fallback_error:
            logger.error(f"Failed to create fallback config: {fallback_error}")


def migrate_old_settings() -> None:
    """Migrate settings from older versions based on schema_version."""
    from core.config_manager import get_config
    config = get_config()
    version = config.get("schema_version", 0)
    if version >= 1:
        return  # already at latest version, nothing to migrate
    # Migrate from v0 (pre-schema) to v1
    config.set("schema_version", 1)


def validate_directories() -> bool:
    """Validate that required directories exist and are accessible."""
    try:
        from core.constants import _user_data_dir
        
        data_dir = _user_data_dir()
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
        
        # Check write permissions
        test_file = data_dir / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
        
        return True
    except Exception as e:
        logger.error(f"Directory validation failed: {e}")
        return False


def cleanup_temp_files() -> None:
    """Clean up temporary files and old logs."""
    from core.constants import _user_data_dir
    
    try:
        data_dir = _user_data_dir()
        temp_dir = data_dir / "temp"
        
        if temp_dir.exists():
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir.mkdir(exist_ok=True)
        
        # Clean old log files (keep last 5)
        log_dir = data_dir / "logs"
        if log_dir.exists():
            # Exclude the currently active log file so we never try to
            # delete a file held open by the logging FileHandler (which
            # would fail with PermissionError on Windows and skew the
            # intended retention count).
            active_log = log_dir / "savesync.log"
            log_files = []
            for f in log_dir.glob("*.log"):
                if f == active_log:
                    continue
                try:
                    log_files.append((f.stat().st_mtime, f))
                except OSError:
                    continue
            log_files.sort()
            log_files = [f for _, f in log_files]
            # Keep at most 4 rotated logs (+ the active one = 5 total)
            if len(log_files) > 4:
                for old_log in log_files[:-4]:
                    try:
                        old_log.unlink()
                    except (PermissionError, OSError) as e:
                        logger.debug(f"Could not delete old log {old_log.name}: {e}")
                    
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")


def check_dependencies() -> dict:
    """Check if optional dependencies are available.

    Returns dict with ``missing_optional`` list. Logs a warning for each
    missing optional module so the user knows which features are degraded.
    """
    import importlib

    optional_modules = {
        'psutil':    'process monitoring',
        'watchdog':  'real-time save file detection',
        'keyring':   'secure credential storage',
    }

    missing_optional = []
    for module, feature in optional_modules.items():
        try:
            importlib.import_module(module)
        except (ImportError, AttributeError, OSError):
            missing_optional.append(module)
            logger.warning(f"Optional module '{module}' not available — {feature} disabled")

    return {"missing_optional": missing_optional}
