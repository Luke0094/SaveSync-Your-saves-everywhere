"""
SaveSync - Process Monitor
Detects ONLY new processes (baseline captured silently on first poll).
Uses (pid, create_time) as identity to disambiguate same-name executables.
Self-excludes via own PID comparison.
"""
import concurrent.futures
import copy
import logging
import os
import platform
import re
import signal
import sys
import threading
import time
import atexit
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logging.warning("psutil not available - process monitoring disabled")

from core.config_manager import get_config
from core.exe_stems import NEVER_A_GAME_PROCESS_STEMS as _NEVER_A_GAME_PROCESS_STEMS
from core.library import get_library, GameEntry

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"
_OWN_PID    = os.getpid()

# Process name stems (lower, no extension) that are always ignored.
#
# Two halves. The vendor/system half is written out below. The
# "ships beside an application, never a game" half — installers, updaters,
# crash handlers — comes from core.exe_stems, shared with the folder scan
# and the save detector's generic-stem list: those two already rejected
# GameUpdate.exe while the monitor still announced it as a game the user
# had launched, which is exactly the drift a shared vocabulary removes.
_SYSTEM_STEMS: frozenset[str] = _NEVER_A_GAME_PROCESS_STEMS | frozenset({
    "system", "svchost", "explorer", "taskmgr", "dwm", "winlogon",
    "lsass", "services", "smss", "csrss", "wininit", "spoolsv",
    "runtimebroker", "searchindexer", "sihost", "ctfmon", "audiodg",
    "fontdrvhost", "dllhost", "msiexec", "wuauclt", "securityhealthsystray",
    "smartscreen", "shellexperiencehost", "startmenuexperiencehost",
    "textinputhost", "searchapp", "microsoftedge", "msedge", "brave", "msedgewebview2",
    "savesync", "main", "__main__",
    "python", "python3", "pythonw", "python3w", "pyside6",
    "cmd", "powershell", "pwsh", "conhost", "wt", "windowsterminal",
    "bash", "sh", "zsh", "fish",
    "steam", "epicgameslauncher", "goggalaxy", "origin",
    "upc", "battlenet", "playnite", "xboxgamebar", "gamebar",
    "eadesktop", "ubisoftconnect", "rockstargameslauncher",
    "bethesdalauncher", "itchio", "taskhostw", "werfault", "wermgr",
    "nvsphelper64", "nvcontainer", "nvdisplay.container",  # NVIDIA
    "amdrsserv", "amddvr", "radeonsoft",  # AMD
    "gamebarpresencewriter", "elevation_service", "identity_helper",  # Xbox Game Bar
    "servicehost", "hostservice", "comppkgsrv",  # Windows service hosts
    "searchhost", "widgetservice", "widgets",  # Windows 11 widgets
    "phoneexperiencehost", "yourphone", "lockapp",
    "applicationframehost", "systemsettings", "settingsynchost",
    "backgroundtaskhost", "backgroundtransferhost",
    "msedgeupdate", "googleupdate",  # Browser updaters
    "microsoftedgeupdate", "bravesoftware", "brave-browser",
    "egui", "eeclnt", "eservicehost", "ekrn",  # ESET antivirus
    "securityhealthservice", "sgrmbroker", "mpcmdrun",  # Windows Security
    "onedrive", "filesync", "filesyncshell64",  # OneDrive client (not a game)
    "onedrivestandaloneupdater", "filecoauth", "microsoft.sharepoint",  # OneDrive satellites
    "msmpeng", "mpdefender", "mpdefendercoreservice",  # Defender engine
    "epiconlineservices", "adguard",  # families, satellites covered by suffix
    "acrobat", "acrord32", "acrotray", "acrocef",  # Adobe Acrobat family
    "adobecollabsync", "adobearm", "armsvc", "adobeipcbroker",  # Adobe services
    "epicwebhelper", "epiconlineserviceshost", "crashreport",  # Epic launcher helpers
    "epiconlineservicesuihelper", "epiconlineservicesinstallhelper",

    "discord", "discordptb", "discordcanary",  # Chat apps
    "spotify", "slack", "teams", "telegram",
    "winrar", "7z", "7za", "7zfm", "7zg", "7zip",  # Archives
    "chrome", "firefox",  # Common utilities and browsers
    # Only essential system processes - let runtime filter handle the rest
    # "rundll32", "conhost",
})

_MIN_EXE_BYTES = 64 * 1024    # < 64 KB → almost certainly not a game
_MIN_RUNTIME_SECONDS = 6    # Process must run at least 6 seconds to be considered a game (avoid borderline 10s cases)

# Bounds for the tracked-process watchdog (see _tracked_watchdog). The
# interval itself is DERIVED from the user's own "Process scan interval"
# rather than fixed, for the reason process_poll_multiplier documents about
# that setting: it is applied on top of their choice, never instead of it.
# Somebody who sets 60 seconds is asking SaveSync to be quiet, and a fixed
# 700 ms timer would be waking the process eighty times more often than they
# asked, however little each wake does.
#
# The floor keeps it useful (there is no point being slower than the poll it
# exists to pre-empt) and the ceiling keeps a very slow setting from turning
# exit detection back into minutes.
_WATCHDOG_MIN_MS = 500
_WATCHDOG_MAX_MS = 5000


def _watchdog_interval_ms() -> int:
    """How often to ask whether the tracked pids are still alive."""
    try:
        base = float(get_config().get("process_poll_interval", 1)) * 1000.0
    except Exception:
        base = 1000.0
    return int(max(_WATCHDOG_MIN_MS, min(_WATCHDOG_MAX_MS, base / 2.0)))

# Identity of a process = (pid, create_time_rounded)
ProcessKey = tuple[int, float]


def _stem(name: str) -> str:
    return Path(name).stem.lower().strip()


# Words a program appends to its OWN name for the satellites it ships. Used
# ONLY to strip a tail off a stem and test what is left against the ignore
# list, so "onedrivelauncher" can be recognised as OneDrive's while
# "steamworlddig" stays a game: "worlddig" is not one of these.
_HELPER_SUFFIXES: frozenset[str] = frozenset({
    "update", "updater", "standaloneupdater", "autoupdate", "setup",
    "installer", "install", "uninstall", "bootstrapper", "launcher",
    "helper", "webhelper", "userhelper", "uihelper", "installhelper",
    "service", "services", "svc", "host", "agent", "tray", "sync",
    "broker", "monitor", "watcher", "daemon", "server", "client",
    "crashhandler", "crashreporter", "crashpad", "reporter", "notifier",
    "elevation", "elevationservice", "core", "coreservice", "config",
    "shell", "ui", "gui", "cli", "console", "engine",
    "delta", "patch", "runtime",
    "browser", "extension",  # Adguard.BrowserExtensionHost and kin
    "x64", "x86", "64", "32",
})
# Below this, a stem is too short to be a safe FAMILY name for the two
# fuzzy rules: "sh", "wt", "main" and friends would start matching games.
# They still work as exact matches, which is how they got on the list.
_MIN_FAMILY_LEN = 5
# A separator-cut prefix only counts when EVERY piece after it looks like a
# satellite (helper word / version / install id). Without that, "brave-souls"
# and "steam-hunters" would be swallowed by "brave"/"steam" — game titles,
# not browser/launcher helpers. Edge Runner is already safe (the list has
# msedge/microsoftedge, never bare "edge"), but the same shape of collision
# is real for every short brand on the list.
_SATELLITE_PART = re.compile(
    r"^(?:"
    r"[0-9]+(?:\.[0-9]+)*"   # 150 / 150.0.4078.99
    r"|[0-9a-f]{8,}"         # install GUIDs, hex blobs
    r")$",
    re.IGNORECASE,
)


def _part_is_satellite(part: str) -> bool:
    """One dotted/hyphenated piece of a satellite name.

    Helpers are often glued together without a separator
    (BrowserExtensionHost); peel known helper suffixes from the right until
    nothing is left. A leftover that is not itself a helper ("souls",
    "hunters") means this is a title, not a satellite.
    """
    pl = (part or "").lower()
    if not pl:
        return True
    if pl in _HELPER_SUFFIXES or _SATELLITE_PART.fullmatch(pl):
        return True
    # Longest suffix first so "webhelper" wins over "helper".
    suffixes = sorted(_HELPER_SUFFIXES, key=len, reverse=True)
    while pl:
        if pl in _HELPER_SUFFIXES or _SATELLITE_PART.fullmatch(pl):
            return True
        for suffix in suffixes:
            if len(pl) > len(suffix) and pl.endswith(suffix):
                pl = pl[:-len(suffix)]
                break
        else:
            return False
    return True


def _tail_is_satellite(tail: str) -> bool:
    """True when *tail* is only helper/version tokens — not a game title."""
    parts = [p for p in re.split(r"[._\-]+", tail) if p]
    return all(_part_is_satellite(p) for p in parts) if parts else True


def _stem_ignored(stem: str, *stem_sets) -> bool:
    """True when *stem* names a listed program, or a satellite of one.

    An exact match is not enough on Windows, where a program ships a small
    fleet under its own name: OneDrive alone runs OneDrive.Sync.Service,
    OneDriveLauncher, OneDriveSetup, FileSyncHelper. With "onedrive" listed
    and only whole-stem equality tested, every one of those was announced as
    a possible game — the ignore list looked ignored.

    Two more rules, both anchored so a listed name can never swallow an
    unrelated one that merely starts the same way:
      • a leading part on a separator boundary whose REMAINDER is only
        helper/version tokens ("microsoftedge_x64_151.0…" → microsoftedge,
        "brave_installer-delta-x64" → brave) — "brave-souls" does NOT match;
      • the name minus one of the suffixes programs give their own helpers
        ("onedrivelauncher" → onedrive, "steamwebhelper" → steam).
    """
    if not stem:
        return False

    def _listed(value: str) -> bool:
        return any(value in s for s in stem_sets)

    if _listed(stem):
        return True
    # Leading segments, longest first. A SPACE is not a separator here —
    # service executables don't use one, game titles do, and treating it as
    # one made "System Shock 2.exe" a satellite of "system".
    marks = [i for i, ch in enumerate(stem) if ch in "._-"]
    for cut in reversed(marks):
        head, tail = stem[:cut], stem[cut + 1:]
        if (len(head) >= _MIN_FAMILY_LEN and _listed(head)
                and _tail_is_satellite(tail)):
            return True
    # Whole name minus a helper suffix.
    for suffix in _HELPER_SUFFIXES:
        if len(stem) > len(suffix) and stem.endswith(suffix):
            head = stem[:-len(suffix)].rstrip("._-")
            if len(head) >= _MIN_FAMILY_LEN and _listed(head):
                return True
    return False


def _get_launcher_appid(pid: int) -> Optional[str]:
    """Get launcher appid from parent process command line.
    
    When a game is launched via Steam/Epic/etc., the launcher spawns the game
    process with the appid in the command line or as a URL.
    
    Returns:
        appid string if found, None otherwise
    """
    if not PSUTIL_AVAILABLE:
        return None
    
    try:
        proc = psutil.Process(pid)
        
        # Check parent process (the launcher)
        try:
            parent = proc.parent()
            if parent:
                cmdline = parent.cmdline()
                cmdline_str = " ".join(cmdline) if cmdline else ""

                # Steam: steam://rungameid/<appid> already present in command line
                if "steam://rungameid/" in cmdline_str.lower():
                    for part in cmdline:
                        if "steam://rungameid/" in part.lower():
                            return part  # full URL — use as-is
                    # Fallback: numeric id only → build canonical URL
                    for part in cmdline:
                        if part.isdigit():
                            return f"steam://rungameid/{part}"

                # Steam: -applaunch <appid> (launched from Steam client)
                if "-applaunch" in cmdline_str:
                    for i, part in enumerate(cmdline):
                        if part == "-applaunch" and i + 1 < len(cmdline):
                            appid = cmdline[i + 1]
                            if appid.isdigit():
                                return f"steam://rungameid/{appid}"

                # Epic Games Launcher
                if "epicgameslauncher" in parent.name().lower():
                    for part in cmdline:
                        if part.startswith("com.epicgames."):
                            # Return the full com.epicgames.launcher URL so the
                            # launcher can be invoked directly via appid later.
                            return f"com.epicgames.launcher://apps/{part}?action=launch"

                # GOG Galaxy: -gameId <id>
                if "goggalaxy" in parent.name().lower():
                    for i, part in enumerate(cmdline):
                        if part in ("-gameId", "--gameId") and i + 1 < len(cmdline):
                            return f"gog://game/{cmdline[i + 1]}"

                # Ubisoft Connect: numeric game id in command line
                if "ubisoft" in parent.name().lower():
                    for part in cmdline:
                        if part.isdigit() and len(part) > 5:
                            return f"ubisoft://play/{part}"

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        
    except Exception:
        pass
    
    return None


# Cache for pending appid lookups (exe_path -> appid).
#
# Read-once by design (get_pending_appid pops), which means an entry whose
# exe is never matched to a library game is never collected — the only
# structure here with no bound at all. Capped rather than cleared on a timer:
# the gap between a launcher recording an appid and the game process being
# matched is seconds, and a sweep that happened to land inside it would drop
# the appid the match was waiting for. Oldest-out can't, since a fresh entry
# is by definition the newest.
_MAX_PENDING_APPIDS = 64
_pending_launcher_appids: "OrderedDict[str, str]" = OrderedDict()
_pending_appids_lock = threading.Lock()


def _remember_pending_appid(exe_path: str, appid: str) -> None:
    with _pending_appids_lock:
        _pending_launcher_appids[exe_path] = appid
        _pending_launcher_appids.move_to_end(exe_path)
        while len(_pending_launcher_appids) > _MAX_PENDING_APPIDS:
            _pending_launcher_appids.popitem(last=False)


def get_pending_appid(exe_path: str) -> Optional[str]:
    """Get and clear pending appid for an exe path."""
    with _pending_appids_lock:
        return _pending_launcher_appids.pop(exe_path, None)


class ProcessMonitor(QObject):
    """Emits only for processes that appear AFTER the baseline poll."""
    game_launched         = Signal(object, str)   # GameEntry, exe_path
    game_exited           = Signal(object)         # GameEntry
    unknown_game_detected = Signal(str, str)       # display_name, exe_path
    unknown_game_exited   = Signal(str)            # exe_path
    # A process matched a library entry by NAME ONLY (its own path was
    # unreadable, so nothing could confirm or contradict it). NOT tracked —
    # the user is asked first. (GameEntry, process_name)
    game_match_unverified = Signal(object, str)
    # …and that process exited before the question was answered: take the
    # prompt down, it now asks about something that no longer exists.
    # (process_name, game_id)
    game_match_unverified_gone = Signal(str, str)
    # Worker-thread → GUI-thread hop for the process snapshot (see _poll)
    _snapshot_ready       = Signal(object)         # dict[ProcessKey, dict]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        # Cheap liveness probe over the tracked pids only — see
        # _tracked_watchdog. Separate from _timer because the whole point is
        # that it runs at its own fast rate while _timer is throttled down.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(_watchdog_interval_ms())
        self._watchdog.timeout.connect(self._tracked_watchdog)
        self._snapshot_in_flight = False
        self._snapshot_ready.connect(self._on_snapshot_ready)
        # Process→entry matching caches (see _find_entry): library lookups
        # built once per library change, per-process answers memoized.
        self._exe_lookup: Optional[dict] = None
        self._stem_lookup: Optional[dict] = None
        self._proc_match_cache: dict = {}
        self._proc_resolved_cache: dict = {}
        # (process_name, game_id) pairs already put to the user this session,
        # so an unverified match is queried once, not on every launch.
        self._unverified_prompted: set = set()
        # Prompts still awaiting an answer: ProcessKey → (process_name, game_id).
        # Lets an unanswered prompt be withdrawn when its process exits.
        self._unverified_pending: dict = {}
        self._suppressed_raw: set = set()
        self._suppressed_resolved: set = set()
        # Cross-poll snapshot cache: (pid, create_time) → verdict dict
        # ({"name","exe"}) or None (filtered out). (pid, create_time) is a
        # stable process identity, so for every ALREADY-SEEN process the
        # poll skips both the exe fetch (an OpenProcess per process — the
        # expensive part of process_iter) and the system-process filter;
        # steady-state polls read only pid/name/create_time. Invalidated
        # by _refresh_ignored_cache (the filter's inputs changed);
        # _snap_gen discards a snapshot raced by such a refresh.
        self._snap_verdicts: dict = {}
        self._snap_gen = 0
        try:
            lib = get_library()
            lib.game_added.connect(self._invalidate_entry_lookup)
            lib.game_updated.connect(self._invalidate_entry_lookup)
            lib.game_removed.connect(self._invalidate_entry_lookup)
        except Exception:
            pass
        try:
            get_config().config_changed.connect(self._on_monitor_cfg_changed)
        except Exception:
            pass

        # Lock to protect shared dicts (_tracked, _running, _game_sessions, etc.)
        self._data_lock = threading.Lock()

        # ProcessKey → {name, exe}
        self._running: dict[ProcessKey, dict] = {}
        # ProcessKey → GameEntry|None
        self._tracked: dict[ProcessKey, Optional[GameEntry]] = {}
        # game_id → {"start", "last_save", "procs": set[ProcessKey]}
        # ONE session per game, no matter how many processes it spawns.
        # Games that fork worker/plugin processes (RPG Maker MZ/NW.js,
        # Chromium-based engines, launchers) would otherwise get their
        # playtime counted once per child process on exit.
        self._game_sessions: dict[str, dict] = {}
        # ProcessKey → first_seen_time (for minimum runtime check)
        self._first_seen: dict[ProcessKey, float] = {}
        # Exes already shown overlay for (per session)
        self._seen_unknown_exes: set[str] = set()

        # Flag set by signal handler, checked in next poll cycle
        self._emergency_flag = False

        self._active        = False
        self._baseline_done = False
        self._ignored_cache: set[str] = set()
        self._own_exe       = self._get_own_exe()
        self._refresh_ignored_cache()
        self._snapshot_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="monitor-snap"
        )
        
        # Initialize adaptive polling variables
        self._fast_poll_count = 0
        self._last_activity_time = time.time()
        
        logger.info(f"ProcessMonitor initialized, psutil available: {PSUTIL_AVAILABLE}")
        
        # Register emergency save handlers
        self._register_emergency_handlers()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_monitor_cfg_changed(self, key: str, _value):
        if key in ("ignored_processes", "suppressed_overlay_apps"):
            self._refresh_ignored_cache()

    def _register_tracked_locked(self, key: ProcessKey, entry: GameEntry) -> None:
        """Track *key* as a process of *entry*, joining the game's existing
        session if one is already active (launcher/child processes of the
        same game share ONE playtime session). Caller must hold _data_lock."""
        self._tracked[key] = entry
        now = time.time()
        self._first_seen[key] = now
        sess = self._game_sessions.get(entry.id)
        if sess is None:
            self._game_sessions[entry.id] = {
                "start": now, "last_save": now, "procs": {key},
            }
        else:
            sess["procs"].add(key)

    def _get_own_exe(self) -> str:
        try:
            return str(Path(psutil.Process(_OWN_PID).exe()).resolve()).lower() if PSUTIL_AVAILABLE else ""
        except Exception:
            return sys.executable.lower()

    def _refresh_ignored_cache(self):
        """Build the ignored-process lookup from config.

        Each entry may be:
        - A full exe path (e.g. 'C:/Games/launcher.exe') -> match by resolved path
        - A bare stem (e.g. 'launcher') -> legacy, match by stem only (less safe)

        Entries saved by the UI always use full paths from suppressed_overlay_apps.
        The legacy ignored_processes list is kept for backward compatibility.
        """
        raw = get_config().get("ignored_processes", [])
        # Filter inputs are changing: cached per-process verdicts computed
        # under the OLD rules must not be replayed, and a snapshot running
        # right now must not publish its cache (see _snap_gen in _snapshot).
        self._snap_gen = getattr(self, "_snap_gen", 0) + 1
        self._snap_verdicts = {}
        self._ignored_stems: set[str] = set()   # bare stem matches (legacy)
        self._ignored_paths: set[str] = set()   # full resolved-path matches
        for entry in raw:
            e = entry.strip()
            if not e:
                continue
            # If the entry contains a path separator or looks like an absolute path,
            # treat it as a full path.
            if "/" in e or "\\" in e or os.sep in e:
                try:
                    self._ignored_paths.add(str(Path(e).resolve()).lower())
                except Exception:
                    self._ignored_stems.add(_stem(e))
            else:
                self._ignored_stems.add(_stem(e))
        # _ignored_cache (stem set) kept for backward-compat callers
        self._ignored_cache = self._ignored_stems
        # suppressed_overlay_apps precomputed for _is_system_process: raw
        # strings + resolved-lowered paths, so the per-process check is two
        # set lookups instead of resolving the whole list per process.
        suppressed = get_config().get("suppressed_overlay_apps", [])
        self._suppressed_raw = {s for s in suppressed if s}
        self._suppressed_resolved = set()
        for s_ in self._suppressed_raw:
            try:
                self._suppressed_resolved.add(str(Path(s_).resolve()).lower())
            except (OSError, RuntimeError, RecursionError):
                self._suppressed_resolved.add(s_.lower())

    def _register_emergency_handlers(self):
        """Register signal handlers for emergency save on app termination.

        Saves the previous signal handlers so they can be called after the
        emergency save completes (e.g. Qt's default SIGINT handler).
        """
        self._emergency_saved = False
        self._prev_signal_handlers: dict[int, Any] = {}
        # Register for normal exit
        atexit.register(self._emergency_save_all)

        # Register for signals only from main thread
        if threading.current_thread() is not threading.main_thread():
            return

        sigs = [signal.SIGINT, signal.SIGTERM]
        if _IS_WINDOWS and hasattr(signal, "SIGBREAK"):
            sigs.append(signal.SIGBREAK)
        elif not _IS_WINDOWS:
            if hasattr(signal, "SIGUSR1"):
                sigs.append(signal.SIGUSR1)

        for sig in sigs:
            prev = signal.signal(sig, self._signal_handler)
            self._prev_signal_handlers[sig] = prev

    def _signal_handler(self, signum, frame):
        """Handle system signals for emergency save.
        Sets the flag so the next poll cycle performs the save, then
        re-invokes the previous handler (e.g. Qt's SIGINT handler) so
        the application can still shut down normally."""
        self._emergency_flag = True
        # Re-invoke the previous handler so Qt (or other frameworks) can
        # perform its own shutdown logic (e.g. quit on Ctrl+C).
        prev = getattr(self, '_prev_signal_handlers', {}).get(signum)
        if callable(prev):
            prev(signum, frame)

    def _emergency_save_all(self):
        """Emergency save all running games playtime.

        Calls _flush_save directly instead of update_game (which uses Qt signals/timers).
        """
        if getattr(self, '_emergency_saved', False):
            return
        self._emergency_saved = True
        try:
            current_time = time.time()
            lib = get_library()

            # Acquire lock to safely read tracked entries
            acquired = self._data_lock.acquire(timeout=2)
            if not acquired:
                # Lock not acquired — cannot read tracked entries, so playtime
                # accumulated since the last periodic save will be lost.
                try:
                    logger.warning(
                        "Emergency save: could not acquire data lock within 2s. "
                        "Playtime since last periodic save may be lost. "
                        "Flushing current library state only."
                    )
                except Exception:
                    pass
                if hasattr(lib, '_flush_save'):
                    lib._flush_save()
                return
            try:
                # One entry per GAME (not per process) so multi-process games
                # never save their pending playtime more than once.
                sessions_snapshot = {
                    gid: sess["last_save"]
                    for gid, sess in self._game_sessions.items()
                }
            finally:
                self._data_lock.release()

            # Update playtime directly and flush without Qt signals.
            # During shutdown the event loop may be torn down, so update_game()
            # (which emits signals and schedules timers) is unsafe.
            any_updated = False
            for gid, last_save in sessions_snapshot.items():
                playtime_seconds = int(current_time - last_save)
                if playtime_seconds > 0:
                    # Atomically merge playtime into the live entry to
                    # avoid overwriting concurrent updates (e.g. save
                    # path changes) with stale data.
                    # Use a timeout to avoid deadlocking during shutdown
                    # if lib._lock is already held by another thread.
                    if lib._lock.acquire(timeout=2):
                        try:
                            live = lib._games.get(gid)
                            if live is not None:
                                live.add_playtime(playtime_seconds)
                                any_updated = True
                        finally:
                            lib._lock.release()
            # Mark the saved slice as consumed so a later normal exit does
            # not add the same seconds again (emergency save can fire on a
            # stray SIGINT while the app keeps running).
            if any_updated and self._data_lock.acquire(timeout=2):
                try:
                    for gid in sessions_snapshot:
                        sess = self._game_sessions.get(gid)
                        if sess is not None:
                            sess["last_save"] = current_time
                finally:
                    self._data_lock.release()
            # Flush to disk directly (no Qt signals/timers)
            if any_updated and hasattr(lib, '_flush_save'):
                lib._flush_save()
        except Exception as e:
            # During interpreter shutdown, objects may be destroyed
            try:
                logger.error(f"Emergency save error: {e}")
            except Exception:
                pass

    # Cached system directories — built once, reused every poll cycle.
    _CACHED_SYSTEM_DIRS: tuple[str, ...] | None = None

    def _is_plausible_game(self, exe: str) -> bool:
        try:
            p = Path(exe)

            if not p.exists():
                return False
            if p.stat().st_size < _MIN_EXE_BYTES:
                return False

            # Only exclude system directories - everything else is fair game
            el = str(p).lower().replace("\\", "/")
            if ProcessMonitor._CACHED_SYSTEM_DIRS is None:
                _sys_root = os.environ.get("SYSTEMROOT", "C:\\Windows").replace("\\", "/").lower()
                _prog_files = os.environ.get("PROGRAMFILES", "C:\\Program Files").replace("\\", "/").lower()
                ProcessMonitor._CACHED_SYSTEM_DIRS = (
                    f"{_sys_root}/",
                    f"{_prog_files}/windowsapps/",
                    f"{_sys_root}/system32/",
                    f"{_sys_root}/syswow64/",
                    "/usr/lib/",
                    "/usr/bin/",
                    "/bin/",
                    "/sbin/",
                    "/lib/",
                    "/system/",
                    "/usr/libexec/",
                    # The rest of the Unix system tree. Without these, a
                    # daemon starting after SaveSync did looks exactly like
                    # a game that has just been launched: measured on a
                    # Linux run, /init was picked up as a game, live
                    # tracking ran for it, and /usr/share/icons/hicolor was
                    # offered as its save folder.
                    "/usr/sbin/",
                    "/usr/local/sbin/",
                    "/proc/",
                    "/sys/",
                    "/dev/",
                    "/run/",
                    "/etc/",
                    "/init",
                    "/lib64/",
                    "/usr/lib64/",
                    "/snap/core",
                    "/snap/snapd",
                )
            if any(el.startswith(d) for d in ProcessMonitor._CACHED_SYSTEM_DIRS):
                return False

            # If it's not in system directories and has reasonable size, it's plausible
            return True

        except (OSError, ValueError):
            return False

    def _snapshot(self) -> dict[ProcessKey, dict]:
        """One pass over the process table, CACHED across polls with zero-allocation steady state.

        Uses fast process table enumeration (CreateToolhelp32Snapshot on Windows / psutil on Linux)
        to inspect running PIDs and process names in ~1-10 ms without per-process syscall overhead.

        PID recycling is fully protected against:
        - If a PID is reused by a different executable, name mismatch immediately invalidates the entry.
        - Candidate game processes verify create_time on each poll.
        - Result is keyed by (pid, create_time) ProcessKey identity.
        """
        result: dict[ProcessKey, dict] = {}
        if not PSUTIL_AVAILABLE:
            return result

        live_procs: dict[int, str] = {}
        if _IS_WINDOWS:
            try:
                import ctypes
                import ctypes.wintypes as wt

                class _PROCESSENTRY32(ctypes.Structure):
                    _fields_ = [
                        ('dwSize', wt.DWORD),
                        ('cntUsage', wt.DWORD),
                        ('th32ProcessID', wt.DWORD),
                        ('th32DefaultHeapID', ctypes.c_void_p),
                        ('th32ModuleID', wt.DWORD),
                        ('cntThreads', wt.DWORD),
                        ('th32ParentProcessID', wt.DWORD),
                        ('pcPriClassBase', wt.LONG),
                        ('dwFlags', wt.DWORD),
                        ('szExeFile', ctypes.c_char * 260)
                    ]

                k32 = ctypes.windll.kernel32
                hSnap = k32.CreateToolhelp32Snapshot(2, 0)
                if hSnap != -1:
                    entry = _PROCESSENTRY32()
                    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
                    if k32.Process32First(hSnap, ctypes.byref(entry)):
                        while True:
                            pid = entry.th32ProcessID
                            if pid != _OWN_PID and pid > 0:
                                live_procs[pid] = entry.szExeFile.decode('latin1', 'ignore').strip()
                            if not k32.Process32Next(hSnap, ctypes.byref(entry)):
                                break
                    k32.CloseHandle(hSnap)
            except Exception as e:
                logger.debug(f"Toolhelp snapshot error: {e}")
                live_procs.clear()

        # Fallback if Windows toolhelp snapshot was empty or on Linux/other OS
        if not live_procs:
            try:
                for p in psutil.process_iter(['pid', 'name']):
                    pid = p.info['pid']
                    if pid != _OWN_PID and pid > 0:
                        live_procs[pid] = (p.info.get('name') or '').strip()
            except Exception as e:
                logger.debug(f"process_iter error: {e}")
                return result

        # 1. Evict terminated PIDs from cache
        dead_pids = set(self._snap_verdicts.keys()) - set(live_procs.keys())
        for dead_pid in dead_pids:
            self._snap_verdicts.pop(dead_pid, None)

        # 2. Evaluate processes (verifying identity and catching PID recycling)
        tracked_pids = set()
        with self._data_lock:
            tracked_pids = {k[0] for k in self._tracked}

        for pid, name in live_procs.items():
            cached = self._snap_verdicts.get(pid)
            if cached is not None:
                cached_name, cached_ctime, verdict = cached
                # Protection 1: If executable name changed, PID was recycled by OS!
                if name.lower() != cached_name.lower():
                    cached = None
                # Protection 2: For actively tracked games, verify create_time has not changed (same-name relaunch)
                elif pid in tracked_pids:
                    try:
                        cur_proc = psutil.Process(pid)
                        cur_ctime = round(cur_proc.create_time(), 1)
                        if cur_ctime != cached_ctime:
                            cached = None  # PID was recycled by another instance of the game
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        cached = None


            if cached is None:
                # Newly spawned or recycled PID: evaluate full pipeline once
                ctime = 0.0
                verdict = None
                try:
                    proc = psutil.Process(pid)
                    ctime = round(proc.create_time(), 1)
                    if name and not (_IS_WINDOWS and not name.lower().endswith(".exe")):
                        s = _stem(name)
                        if not _stem_ignored(s, _SYSTEM_STEMS, self._ignored_cache):
                            try:
                                exe = (proc.exe() or "").strip()
                            except (psutil.AccessDenied, psutil.ZombieProcess):
                                exe = ""
                            if not self._is_system_process(name, exe):
                                verdict = {"name": name, "exe": exe}

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
                except Exception:
                    pass
                self._snap_verdicts[pid] = (name, ctime, verdict)

        # 3. Assemble snapshot output for candidate processes
        for pid, (name, ctime, verdict) in self._snap_verdicts.items():
            if verdict is not None and ctime > 0:
                result[(pid, ctime)] = verdict
        return result



    def _is_system_process(self, name: str, exe: str) -> bool:
        """Check if process is a known system process (not game).

        Runs per PROCESS per POLL on the GUI thread, so everything here is
        precomputed: the suppressed_overlay_apps set (raw + resolved) is
        built once in _refresh_ignored_cache (re-run on config change) and
        the process exe resolve goes through the cross-poll cache — the old
        version resolved every suppressed entry against every process on
        every poll.
        """
        s = _stem(name)
        if _stem_ignored(s, _SYSTEM_STEMS, self._ignored_cache):
            return True
        if not exe:
            return False
        if exe in self._suppressed_raw:
            return True
        resolved_exe = self._resolve_proc_exe(exe)
        if resolved_exe in getattr(self, "_ignored_paths", ()):
            return True
        if resolved_exe in self._suppressed_resolved:
            return True
        return resolved_exe == self._own_exe

    def _has_tracked_ancestor(self, pid: int, game_id: str) -> bool:
        """True if any ancestor (parent, grandparent, ...) of *pid* is
        already being tracked in self._tracked for the SAME *game_id*.

        Used to decide whether to emit game_launched for a process that
        just matched a library entry: many games run through a launcher
        first (Steam, a game's own updater, etc.) before spawning the real
        game executable as a child process. Both can independently match
        the same library entry — the launcher typically via _find_entry's
        exact exe_path (if the library recorded the launcher itself,
        which is usually necessary for the "▶ Play" button to actually
        work later) and the real game exe via its own match. Emitting
        game_launched for BOTH means every subscriber (watcher, in-game
        backup timer, live tracking, the tracking overlay) runs twice for
        what is, from the player's side, a single session.

        The fix keeps the FIRST (parent) emission — not the child's — as
        the authoritative one: the parent is what "▶ Play" relies on to
        correctly (re)launch the game later (a launcher's own exe_path,
        not its child's, since the child is usually not directly
        launchable on its own — it depends on the launcher for DRM checks,
        overlay injection, environment setup, etc.), and a game with NO
        launcher at all (a single standalone exe) is unaffected either way
        since it has no ancestor to find here. When a child DOES later
        appear for the same game, this lets that existing (parent-rooted)
        session keep covering it instead of starting a redundant second,
        duplicate one.
        """
        if not PSUTIL_AVAILABLE:
            return False
        try:
            proc = psutil.Process(pid)
            for _ in range(6):   # bounded walk — avoid any pathological loop
                parent = proc.parent()
                if parent is None:
                    return False
                with self._data_lock:
                    for (tracked_pid, _ct), tracked_entry in self._tracked.items():
                        if tracked_pid == parent.pid and tracked_entry is not None \
                                and tracked_entry.id == game_id:
                            return True
                proc = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    # ── Library lookup (exact exe match first, then fuzzy) ───────────────────

    # ── Fast process→entry matching ─────────────────────────────────────
    # The poll runs on the GUI thread and used to re-resolve EVERY library
    # exe path for EVERY running process (N_proc × N_games filesystem
    # resolves): the first poll froze the UI for seconds right when the
    # user is waiting for the online/offline status. The lookups below are
    # built ONCE per library change, and each (name, exe) pair is answered
    # from a per-process cache after its first evaluation — same matching
    # semantics (exact path → resolved path → exact stem with path
    # disambiguation → ≤2-char substring stem), collapsed to dict work.

    def _invalidate_entry_lookup(self, *_args):
        self._exe_lookup = None
        self._stem_lookup = None
        self._proc_match_cache.clear()

    def prune_caches(self, full: bool = False):
        """Trim memory held by cached lookup maps without discarding process verdicts.

        Snapshot verdicts for running processes are preserved so that subsequent polls take the
        instant zero-I/O cached path for known system processes without re-opening psutil handles.
        Dead processes are naturally evicted on each snapshot pass.
        """
        with self._data_lock:
            self._proc_resolved_cache.clear()
            self._proc_match_cache.clear()
            if full:
                self._invalidate_entry_lookup()


    def _build_entry_lookup(self):
        from core.resolvers import fuzzy_slug as _slug
        exe_lookup: dict[str, str] = {}      # lowered / resolved path → id
        stem_lookup: dict[str, list] = {}    # slug(stem) → [(id, resolved)]
        for g in get_library().all_games():
            if not g.exe_path:
                continue
            exe_lookup.setdefault(g.exe_path.lower(), g.id)
            try:
                resolved = str(Path(g.exe_path).resolve()).lower()
            except OSError:
                resolved = g.exe_path.lower()
            exe_lookup.setdefault(resolved, g.id)
            stem = _slug(Path(g.exe_path).stem)
            if stem:
                stem_lookup.setdefault(stem, []).append((g.id, resolved))
        self._exe_lookup = exe_lookup
        self._stem_lookup = stem_lookup

    def _resolve_proc_exe(self, exe: str) -> str:
        cached = self._proc_resolved_cache.get(exe)
        if cached is None:
            try:
                cached = str(Path(exe).resolve()).lower()
            except OSError:
                cached = exe.lower()
            if len(self._proc_resolved_cache) > 512:
                self._proc_resolved_cache.clear()
            self._proc_resolved_cache[exe] = cached
        return cached

    @staticmethod
    def _proc_match_key(name: str) -> str:
        """Key for the confirmed/rejected stores: the process filename. The
        path is precisely what's missing in these cases, so it can't be used."""
        return Path(name or "").name.lower()

    @staticmethod
    def _match_confirmed(proc_name: str, game_id: str) -> bool:
        """User already said "yes, that process IS this game"."""
        return get_config().get("confirmed_process_matches", {}).get(proc_name) == game_id

    @staticmethod
    def _match_rejected(proc_name: str, game_id: str) -> bool:
        """User already said "no, that process is NOT this game". Stored per
        (process, game) pair on purpose: rejecting launcher.exe as Alpha must
        not stop it from ever matching Beta."""
        return game_id in (get_config().get("rejected_process_matches", {}).get(proc_name) or [])

    def _find_entry(self, name: str, exe: str) -> Optional[GameEntry]:
        """The matched entry, regardless of how confidently it was matched."""
        return self._match_process(name, exe)[0]

    def _match_process(self, name: str, exe: str) -> tuple[Optional[GameEntry], bool]:
        """Match a running process to a library entry.

        Returns ``(entry, verified)``. *verified* is False when the match
        rests on the process NAME alone because the process's own path was
        unreadable — typically an elevated game, where psutil raises
        AccessDenied on proc.exe(). A name is not an identity (plenty of
        games ship launcher.exe), so an unverified match must NOT be acted
        on silently: callers ask the user instead, and the answer is
        remembered so the question is asked once per process name.
        """
        from core.resolvers import fuzzy_slug as _slug
        lib = get_library()
        cache_key = (name.lower(), exe.lower())
        cached = self._proc_match_cache.get(cache_key)
        if cached is not None:
            gid, verified = cached
            return (lib.get_by_id(gid) if gid else None), verified
        if self._exe_lookup is None or self._stem_lookup is None:
            self._build_entry_lookup()

        def _remember(gid, verified: bool = True):
            if len(self._proc_match_cache) > 1024:
                self._proc_match_cache.clear()
            self._proc_match_cache[cache_key] = (gid, verified)
            return (lib.get_by_id(gid) if gid else None), verified

        # 1-2) Exact / resolved exe path — the path IS the proof.
        gid = self._exe_lookup.get(exe.lower())
        if gid is None:
            gid = self._exe_lookup.get(self._resolve_proc_exe(exe))
        if gid is not None:
            return _remember(gid)

        # 3) Stem matching, only for stems long enough to be meaningful
        s = _stem(name)
        if len(s) >= 4:
            from core.resolvers import is_different_program
            pname = _slug(Path(name).stem)
            exe_resolved = self._resolve_proc_exe(exe) if exe else ""
            # No readable process path → nothing can contradict a name match,
            # and nothing can confirm it either.
            has_path_evidence = bool(exe_resolved)
            proc_key = self._proc_match_key(name)

            def _consider(gid_c: str, g_resolved: str):
                """(entry, verified) for a candidate, or None to skip it."""
                if is_different_program(g_resolved, exe_resolved):
                    return None
                if not has_path_evidence:
                    if self._match_rejected(proc_key, gid_c):
                        return None
                    return _remember(gid_c, self._match_confirmed(proc_key, gid_c))
                return _remember(gid_c, True)

            # Exact stem — same stem but a DIFFERENT (still existing) path
            # means a different game, so skip those.
            for gid_c, g_resolved in self._stem_lookup.get(pname, []):
                hit = _consider(gid_c, g_resolved)
                if hit is not None:
                    return hit
            # Substring stem with ≤2 chars of difference. The SAME path rule
            # applies here — without it this loop silently handed back the
            # very candidates the exact-stem loop had just rejected:
            # _stem_lookup is keyed by stem, so `pname` also matches itself
            # here (length difference 0) and cands[0] is that same
            # different-path game. A second game's launcher.exe was being
            # attributed to the first one, backups included.
            for stem, cands in self._stem_lookup.items():
                if not stem or not pname:
                    continue
                shorter, longer = ((stem, pname) if len(stem) <= len(pname)
                                   else (pname, stem))
                if shorter in longer and (len(longer) - len(shorter)) <= 2:
                    for gid_c, g_resolved in cands:
                        hit = _consider(gid_c, g_resolved)
                        if hit is not None:
                            return hit
        return _remember(None)

    def _prompt_unverified_match(self, entry: GameEntry, proc_name: str,
                                 proc_key: Optional[tuple] = None):
        """Ask the user whether this name-only match is really that game.

        Asked once per (process, game) per session; the answer is persisted,
        so in practice it is once, ever. Until it comes, the process stays
        untracked — attributing another game's saves to this entry is a worse
        outcome than missing a session.
        """
        name_key = self._proc_match_key(proc_name)
        pair = (name_key, entry.id)
        if pair in self._unverified_prompted:
            return
        self._unverified_prompted.add(pair)
        if proc_key is not None:
            with self._data_lock:
                self._unverified_pending[proc_key] = pair
        logger.info(
            f"Unverified match: process {proc_name!r} looks like {entry.name} "
            f"but its path is unreadable — not tracking until confirmed")
        self.game_match_unverified.emit(entry, Path(proc_name or "").name)

    def _prompt_unverified_after_runtime(self, entry: GameEntry, name: str, key: tuple):
        """Defer the prompt by the same runtime threshold every other launch
        notification respects, so a short-lived updater/installer that happens
        to share a game's executable name never raises the question."""
        game_id = entry.id

        def _check():
            try:
                if time.time() - psutil.Process(key[0]).create_time() < _MIN_RUNTIME_SECONDS:
                    return
            except Exception:
                return          # already gone — nothing to ask about
            fresh = get_library().get_by_id(game_id)
            if fresh is not None:
                self._prompt_unverified_match(fresh, name, key)

        QTimer.singleShot(_MIN_RUNTIME_SECONDS * 1000, _check)

    def confirm_unverified_match(self, proc_name: str, game_id: str, accept: bool):
        """Record the user's answer to an unverified-match prompt.

        On accept the live process is attached straight away — persisting the
        mapping without doing that would leave the running session untracked,
        which reads as the confirmation having done nothing.
        """
        config = get_config()
        key = self._proc_match_key(proc_name)
        if not key or not game_id:
            return
        if accept:
            store = dict(config.get("confirmed_process_matches", {}))
            store[key] = game_id
            config.set("confirmed_process_matches", store)
        else:
            store = {k: list(v) for k, v in config.get("rejected_process_matches", {}).items()}
            ids = store.setdefault(key, [])
            if game_id not in ids:
                ids.append(game_id)
            config.set("rejected_process_matches", store)
        # The verdict is cached alongside the match — drop it so the next
        # lookup reflects the answer.
        self._proc_match_cache.clear()
        logger.info(f"Unverified match for {key}: user said "
                    f"{'YES' if accept else 'NO'} to game {game_id}")
        if accept:
            self._attach_confirmed_match(key, game_id)

    def _attach_confirmed_match(self, proc_key: str, game_id: str):
        """Start tracking every still-running process the user just confirmed."""
        entry = get_library().get_by_id(game_id)
        if entry is None:
            return
        with self._data_lock:
            candidates = [
                (key, info) for key, info in self._running.items()
                if self._proc_match_key(info.get("name", "")) == proc_key
                and self._tracked.get(key) is None
            ]
            for key, _info in candidates:
                self._unverified_pending.pop(key, None)
        # _running is only as fresh as the last poll, so a process that died
        # in between is still listed — attaching it would start a session for
        # a pid that no longer exists.
        candidates = [(k, i) for k, i in candidates
                      if not PSUTIL_AVAILABLE or psutil.pid_exists(k[0])]
        for key, _info in candidates:
            entry.mark_played()
            get_library().update_game(entry)
            with self._data_lock:
                self._register_tracked_locked(key, entry)
            if not self._has_tracked_ancestor(key[0], entry.id):
                self.game_launched.emit(entry, entry.exe_path or "")
                logger.info(f"Confirmed match attached: {entry.name} (pid={key[0]})")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if not PSUTIL_AVAILABLE:
            logger.warning("Process monitoring unavailable (psutil missing)")
            return
        logger.info("Starting process monitor...")
        self._refresh_ignored_cache()
        # Use adaptive polling: start fast, then slow down if no activity
        self._fast_poll_count = 0
        self._last_activity_time = time.time()
        interval = 500  # Start with 500ms for fast detection
        self._timer.start(interval)
        self._watchdog.setInterval(_watchdog_interval_ms())
        self._watchdog.start()
        self._active = True
        logger.info(f"Process monitor started ({interval}ms interval with adaptive polling)")

    def _get_adaptive_interval(self) -> int:
        """Get adaptive polling interval based on recent activity.

        Two multipliers, both ON TOP of the user's process_poll_interval,
        never replacing it — that is a visible 1-60s setting and a silent
        override would make it look broken:

        - the machine's capability tier, so a weak PC spends less CPU on
          polling than a fast one (shared with the memory sweeps and the
          save-editor idle release, see core.concurrency);
        - recent activity, unchanged below.
        """
        from core.concurrency import process_poll_multiplier
        base = int(get_config().get("process_poll_interval", 1) * 1000
                   * process_poll_multiplier())

        # IN-GAME: a tracked game is running — the CPU belongs to it. Fast
        # polling exists to catch launches quickly; with a session already
        # active the scan only needs to notice the exit (and the rare
        # second game), so poll gently. Combined with the _snapshot verdict
        # cache this keeps the background cost negligible for CPU-bound
        # games; exit detection is delayed by a few seconds at most.
        with self._data_lock:
            in_game = bool(self._tracked)
        if in_game:
            return base * 4

        current_time = time.time()
        time_since_activity = current_time - self._last_activity_time

        # Fast polling (1x base) for first 30 seconds after recent activity
        if time_since_activity < 30:
            return base
        # Medium polling (2x base) for moderate activity
        elif time_since_activity < 120:
            return base * 2
        # Slow polling (4x base) when idle
        else:
            return base * 4

    def _tracked_watchdog(self):
        """Notice a tracked game exiting NOW, not at the next throttled poll.

        The main poll is deliberately slowed to base*4 during a session:
        it walks the entire process table and resolves executables, and that
        CPU belongs to the game. The consequence was that the one event that
        matters most during a session — the game closing — waited out that
        whole interval before anything downstream (final backup, save scan,
        restoring the window from the tray) even started.

        This costs nothing to do properly, because the tracked processes are
        already known by pid: asking the OS whether a pid still exists is a
        single syscall per tracked process, a couple of them per tick, versus
        hundreds of process inspections. When one is gone the real poll is
        armed for the next event-loop turn and does the authoritative work —
        session accounting, signals, cleanup — exactly as it always did.

        A recycled pid can only make this MISS an early trigger (the normal
        poll still catches it a moment later), never fabricate an exit: this
        method decides nothing on its own, it only asks the poll to run.
        """
        if not (self._active and PSUTIL_AVAILABLE):
            return
        with self._data_lock:
            pids = {key[0] for key in self._tracked}
        if not pids:
            return
        for pid in pids:
            try:
                alive = psutil.pid_exists(pid)
            except Exception:
                continue        # unreadable — leave it to the real poll
            if not alive:
                logger.debug(
                    f"Watchdog: tracked pid {pid} is gone — polling immediately")
                self.nudge()
                return

    def nudge(self):
        """Poll on the next event-loop turn, and treat now as fresh activity.

        For the moments the app already KNOWS something happened and should
        not have to rediscover it on a timer: the user pressing Play, a
        tracked process vanishing. Without this, launching a game from
        SaveSync after a quiet spell waited out the idle interval (base*4)
        before the process was even looked for, and only then started the
        runtime threshold — the delay was self-inflicted, not inherent.
        """
        if not self._active:
            return
        self._last_activity_time = time.time()
        # A ONE-OFF poll, deliberately not self._timer.start(0).
        #
        # _timer repeats, and _poll returns early whenever a snapshot is
        # already in flight — without ever reaching _update_polling_interval,
        # which is what would put a sane interval back. Re-arming it at zero
        # would therefore spin: fire, see the in-flight guard, return, fire
        # again, for as long as the background process walk takes (tens to
        # hundreds of milliseconds), while the watchdog kept re-nudging
        # because _tracked is not cleared until that walk lands. A singleShot
        # gets the immediate poll with no interval to restore, and the
        # scheduled timer keeps its own cadence untouched — the freshly reset
        # activity time above is what makes the NEXT interval the fast one.
        QTimer.singleShot(0, self._poll)

    def _update_polling_interval(self):
        """Update timer interval based on activity"""
        if self._active:
            new_interval = self._get_adaptive_interval()
            current_interval = self._timer.interval()
            if new_interval != current_interval:
                self._timer.setInterval(new_interval)
                logger.info(f"Adaptive polling interval changed to {new_interval}ms")

    def stop(self):
        self._timer.stop()
        self._watchdog.stop()
        self._active = False

    def restart_with_new_interval(self):
        if self._active:
            # The watchdog is derived from the same setting, so it moves with
            # it — otherwise changing "Process scan interval" would leave the
            # cheaper timer still running at the old rate.
            self._watchdog.setInterval(_watchdog_interval_ms())
            self._timer.stop()
            self._refresh_ignored_cache()
            # Reset adaptive polling so it recalculates from current activity
            self._fast_poll_count = 0
            self._last_activity_time = time.time()
            interval = self._get_adaptive_interval()
            self._timer.start(interval)

    # ── Poll ──────────────────────────────────────────────────────────────────

    def _poll(self):
        # Check emergency flag set by signal handler.
        # Reset the flag after saving so the monitor keeps running
        # (a stray SIGINT should not permanently disable polling).
        if self._emergency_flag:
            self._emergency_flag = False
            self._emergency_save_all()
            # Allow _emergency_save_all to run again if needed
            self._emergency_saved = False

        # The psutil process iteration is the one intrinsically heavy part
        # of the poll (tens/hundreds of ms with many processes) and used to
        # run on the GUI thread — the perceptible startup freeze while the
        # sidebar still said offline. It now runs on a worker thread; the
        # continuation (_process_snapshot: diffs, matching, signals) stays
        # on the GUI thread via the queued signal. Overlapping polls are
        # skipped instead of queued.
        if self._snapshot_in_flight:
            return
        self._snapshot_in_flight = True

        def _bg():
            try:
                current = self._snapshot()
            except Exception as e:
                logger.debug(f"Snapshot thread error: {e}")
                current = {}
            self._snapshot_ready.emit(current)

        try:
            self._snapshot_executor.submit(_bg)
        except Exception:
            self._snapshot_in_flight = False

    def _on_snapshot_ready(self, current: dict):
        self._snapshot_in_flight = False
        if not self._active:
            return
        self._process_snapshot(current)

    def _process_snapshot(self, current: dict):
        self._fast_poll_count += 1

        if self._baseline_done and current == self._running:
            return

        if not self._baseline_done:
            with self._data_lock:
                self._running = current
            logger.info(
                f"Process monitor baseline: {len(current)} processes — "
                "watching for NEW ones only"
            )

            # Check baseline processes against library - detect known games already running
            for key, info in current.items():
                name, exe = info["name"], info["exe"]

                # Check if this process is in our library
                entry, verified = self._match_process(name, exe)
                if entry is not None and not verified:
                    self._prompt_unverified_after_runtime(entry, name, key)
                    continue

                if entry:
                    # Known game - track it but wait for runtime threshold before showing popup
                    entry.mark_played()
                    get_library().update_game(entry)
                    with self._data_lock:
                        self._register_tracked_locked(key, entry)
                    
                    # Check runtime before emitting signal
                    def check_baseline_known_game_runtime(_pid=key[0], _game_id=entry.id, _exe=exe, _key=key):
                        with self._data_lock:
                            if _key not in self._tracked:
                                return  # process already exited
                        try:
                            proc = psutil.Process(_pid)
                            actual_create_time = proc.create_time()
                            runtime = time.time() - actual_create_time

                            if runtime >= _MIN_RUNTIME_SECONDS:
                                # Re-fetch fresh entry from library
                                fresh = get_library().get_by_id(_game_id)
                                if fresh and not self._has_tracked_ancestor(_pid, fresh.id):
                                    self.game_launched.emit(fresh, _exe)
                                    logger.info(f"Baseline: Known game launched after {runtime:.1f}s: {fresh.name} (pid={_pid})")
                        except Exception:
                            # Process might have exited, that's fine
                            pass

                    # Schedule runtime check
                    QTimer.singleShot(_MIN_RUNTIME_SECONDS * 1000, check_baseline_known_game_runtime)
                    logger.info(f"Baseline: Known game detected, waiting {_MIN_RUNTIME_SECONDS}s: {entry.name} (pid={key[0]})")
            # Set baseline flag AFTER all timers are scheduled to avoid
            # processes appearing in between being misclassified.
            self._baseline_done = True
            # Don't track unknown processes at baseline - only watch for NEW ones
            self._update_polling_interval()
            return

        # Track new processes and update activity
        new_processes_found = False
        for key, info in current.items():
            if key in self._running:
                continue           # already known

            name, exe = info["name"], info["exe"]

            # Check if this process is in our library
            entry, verified = self._match_process(name, exe)
            if entry is not None and not verified:
                # Matched on the name alone (unreadable process path). Do NOT
                # start tracking a game we can't confirm this is — ask.
                new_processes_found = True
                self._prompt_unverified_after_runtime(entry, name, key)
                continue

            if entry:
                # Known game - track it but wait for runtime threshold before showing popup
                new_processes_found = True
                entry.mark_played()
                get_library().update_game(entry)
                with self._data_lock:
                    self._register_tracked_locked(key, entry)
                
                # Check runtime before emitting signal
                def check_known_game_runtime(_pid=key[0], _game_id=entry.id, _exe=exe, _key=key):
                    with self._data_lock:
                        if _key not in self._tracked:
                            return  # process already exited
                    try:
                        proc = psutil.Process(_pid)
                        actual_create_time = proc.create_time()
                        runtime = time.time() - actual_create_time

                        if runtime >= _MIN_RUNTIME_SECONDS:
                            # Re-fetch fresh entry from library
                            fresh = get_library().get_by_id(_game_id)
                            if fresh and not self._has_tracked_ancestor(_pid, fresh.id):
                                self.game_launched.emit(fresh, _exe)
                                logger.info(f"Known game launched after {runtime:.1f}s: {fresh.name} (pid={_pid})")
                    except Exception:
                        # Process might have exited, that's fine
                        pass

                # Schedule runtime check
                QTimer.singleShot(_MIN_RUNTIME_SECONDS * 1000, check_known_game_runtime)
                logger.info(f"Known game detected, waiting {_MIN_RUNTIME_SECONDS}s: {entry.name} (pid={key[0]})")
            else:
                # Unknown process - check if it could be a game
                if self._is_plausible_game(exe):
                    new_processes_found = True

                    # Wait a bit longer to ensure process stays active before showing popup
                    def check_runtime(_runtime_check_exe=exe, _key=key):
                        try:
                            proc = psutil.Process(_key[0])
                            if not proc.is_running():
                                return
                            actual_create_time = proc.create_time()
                            runtime = time.time() - actual_create_time
                            if runtime >= _MIN_RUNTIME_SECONDS:
                                with self._data_lock:
                                    already_seen = _runtime_check_exe in self._seen_unknown_exes
                                    if not already_seen:
                                        self._seen_unknown_exes.add(_runtime_check_exe)
                                if not already_seen:
                                    # Try to get appid from parent process (launcher)
                                    appid = _get_launcher_appid(_key[0])
                                    if appid:
                                        _remember_pending_appid(_runtime_check_exe, appid)
                                        logger.info(f"Detected launcher appid={appid} for {_runtime_check_exe}")
                                    # Generic exe stems ("Game", "Launcher"…) are
                                    # replaced with the install-folder name so the
                                    # overlay/auto-add never proposes a meaningless
                                    # title (and cloud-save matching uses a real name).
                                    from core.save_detector import derive_display_name
                                    display_name = derive_display_name(_runtime_check_exe)
                                    self.unknown_game_detected.emit(display_name, _runtime_check_exe)
                                    logger.info(f"Unknown process detected after {runtime:.1f}s: {display_name} (pid={_key[0]})")
                        except Exception:
                            pass
                    
                    # Track as None so exit handler can clean up
                    with self._data_lock:
                        self._tracked[key] = None
                    # Schedule runtime check with a buffer to account for timing issues
                    QTimer.singleShot((_MIN_RUNTIME_SECONDS + 2) * 1000, check_runtime)
                else:
                    # Not a plausible game, track as None but don't show popup
                    with self._data_lock:
                        self._tracked[key] = None

        # Exited processes — compute gone set and collect data under a single lock.
        # A game's playtime session ends only when its LAST tracked process
        # exits: multi-process games (RPG Maker/NW.js spawn several "Game"
        # processes) must count the session ONCE, not once per child.
        with self._data_lock:
            running_keys = set(self._running)
            gone = running_keys - set(current)
            gone_data: list[tuple] = []
            # Unanswered "is this really that game?" prompts whose process is
            # now gone. Dropped from _unverified_prompted too, so a later
            # launch in this session asks again instead of staying silent
            # about a question that was never actually answered.
            withdrawn: list[tuple] = []
            for key in gone:
                pending = self._unverified_pending.pop(key, None)
                if pending is not None and not any(
                        v == pending for k, v in self._unverified_pending.items()):
                    self._unverified_prompted.discard(pending)
                    withdrawn.append(pending)
                entry = self._tracked.pop(key, None)
                self._first_seen.pop(key, None)
                info = self._running.get(key, {})
                exe = info.get("exe", "")
                seen = exe and exe in self._seen_unknown_exes
                finished_session = None
                if entry is not None:
                    sess = self._game_sessions.get(entry.id)
                    if sess is not None:
                        sess["procs"].discard(key)
                        if not sess["procs"]:
                            finished_session = self._game_sessions.pop(entry.id)
                gone_data.append((key, entry, finished_session, exe, seen))

        for proc_name, game_id in withdrawn:
            logger.info(f"Withdrawing unanswered match prompt for {proc_name!r} "
                        "— the process exited")
            self.game_match_unverified_gone.emit(proc_name, game_id)

        for key, entry, finished_session, exe, seen in gone_data:
            if entry is not None:
                if finished_session is None:
                    # Other processes of the same game are still running —
                    # the session (and its playtime) continues.
                    logger.debug(
                        f"Process of {entry.name} exited (pid={key[0]}), "
                        "session continues with remaining processes"
                    )
                    continue
                # Calculate playtime since last save (not total time)
                now_ts = time.time()
                playtime_seconds = int(now_ts - finished_session["last_save"])
                session_seconds = int(now_ts - finished_session["start"])
                if playtime_seconds > 0 or session_seconds > 0:
                    lib = get_library()
                    # Use atomic field-level update to avoid clobbering
                    # concurrent changes (e.g. mark_backed_up from backup thread).
                    with lib._lock:
                        live = lib._games.get(entry.id)
                        if live is not None:
                            if playtime_seconds > 0:
                                live.add_playtime(playtime_seconds)
                            if session_seconds > 0:
                                # Full session length (launch → exit),
                                # shown by the library card's playtime hover.
                                live.last_session_seconds = session_seconds
                            live.mark_played()
                        else:
                            logger.warning(f"Game {entry.name} was removed from library during play, skipping playtime update")
                    if live is not None:
                        lib._schedule_save()
                        # Tell the UI, not just the disk. The block above is
                        # an in-place mutation under lib._lock (deliberately —
                        # add_playtime ACCUMULATES and a read-modify-write via
                        # update_game would drop a concurrent writer's change),
                        # and in-place means no game_updated goes out. The card
                        # therefore kept showing the playtime and "last played"
                        # from BEFORE the session that had just ended, until
                        # some unrelated rebuild happened to refresh it.
                        lib.notify_updated(entry.id)
                    logger.info(f"Added {playtime_seconds}s playtime to {entry.name} (session: {session_seconds}s)")

                self.game_exited.emit(entry)
                logger.info(f"Game exited: {entry.name} (pid={key[0]})")
            else:
                # Unknown process (was tracked as None) — notify for post-exit proposal
                if seen:
                    self.unknown_game_exited.emit(exe)
                    logger.debug(f"Unknown process exited: {exe} (pid={key[0]})")

        with self._data_lock:
            # Preserve entries added by start_tracking() during this poll cycle.
            # Merge any keys in self._running that are not in current but ARE
            # tracked — these may have been added by start_tracking() between
            # _snapshot() and now.
            for key in list(self._running):
                if key not in current and key in self._tracked:
                    current[key] = self._running[key]
            self._running = current

        # Update activity tracking and adaptive polling
        if new_processes_found:
            self._last_activity_time = time.time()
            self._fast_poll_count = 0  # Reset fast poll counter on activity
        
        # Update polling interval based on activity
        self._update_polling_interval()

    # ── Public helpers ────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    def currently_playing(self) -> list[GameEntry]:
        """Return all known games currently being tracked (one entry per
        game, even when the game runs as multiple processes)."""
        with self._data_lock:
            by_id: dict[str, GameEntry] = {}
            for e in self._tracked.values():
                if e is not None and e.id not in by_id:
                    by_id[e.id] = copy.deepcopy(e)
            return list(by_id.values())

    def refresh_tracked(self):
        """Re-evaluate all currently-tracked-None processes against the current library.
        Call this after a game is added to the library mid-session.
        """
        lib = get_library()
        with self._data_lock:
            items = list(self._tracked.items())
        for key, entry in items:
            if entry is not None:
                continue   # already matched
            with self._data_lock:
                info = self._running.get(key, {})
            name, exe = info.get("name", ""), info.get("exe", "")
            if not name and not exe:
                continue
            # Late-match after the user added a game. An unverified (name-only)
            # match is skipped rather than prompted: a process whose path is
            # unreadable never became a tracked-unknown in the first place
            # (_is_plausible_game needs an existing path), so this branch can
            # only be reached with real path evidence.
            found, verified = self._match_process(name, exe)
            if found and verified:
                found.mark_played()
                lib.update_game(found)
                with self._data_lock:
                    self._register_tracked_locked(key, found)
                if not self._has_tracked_ancestor(key[0], found.id):
                    self.game_launched.emit(found, exe)
                    logger.info(f"Late-match: {found.name} (pid={key[0]}) now in library")


    def clear_seen_exe(self, exe_path: str):
        """Allow overlay to re-show for this exe (e.g. after game removed)."""
        with self._data_lock:
            self._seen_unknown_exes.discard(exe_path)

    def find_pid_by_exe(self, exe_path: str) -> int:
        """Find PID of a running process by exe path. Returns 0 if not found."""
        try:
            target = Path(exe_path).resolve()
        except (OSError, ValueError):
            return 0
        with self._data_lock:
            for key, info in self._running.items():
                exe = info.get("exe", "")
                if exe:
                    try:
                        if Path(exe).resolve() == target:
                            return key[0]
                    except (OSError, ValueError):
                        continue
        return 0

    def find_game_process(self, game_id: str, exe_path: str) -> int:
        """Find the actual game process PID, handling launcher→child scenarios.

        Search order:
        1. Tracked games by game_id (fastest — already matched by monitor)
        2. Running processes by exe_path (direct match)
        3. Child/sibling processes near the exe's directory (launcher→game pattern)

        Returns PID or 0 if not found.
        """
        if not PSUTIL_AVAILABLE:
            return 0

        # 1. Check tracked games
        with self._data_lock:
            for key, entry in self._tracked.items():
                if entry is not None and entry.id == game_id:
                    # Verify the process is still running
                    try:
                        proc = psutil.Process(key[0])
                        if proc.is_running():
                            return key[0]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

        # 2. Direct exe match in running dict
        pid = self.find_pid_by_exe(exe_path)
        if pid:
            return pid

        # 3. Launcher→child pattern: find the largest non-system process whose
        #    exe is in the same directory (or a subdirectory) as the registered exe.
        #    This handles cases where launcher.exe spawns game_actual.exe in the
        #    same install folder.
        try:
            exe_dir = Path(exe_path).resolve().parent
        except (OSError, ValueError):
            return 0

        # Import outside the lock to avoid potential deadlock with the
        # import machinery on first invocation.
        from core import is_relative_to

        # Snapshot running dict under lock, then do expensive I/O outside
        # to avoid holding _data_lock during psutil/Path.resolve calls.
        with self._data_lock:
            running_snapshot = list(self._running.items())

        best_pid = 0
        best_mem = 0
        for key, info in running_snapshot:
            exe = info.get("exe", "")
            if not exe:
                continue
            try:
                proc_path = Path(exe).resolve()
                # Check if the process exe is inside the game's install directory
                if proc_path.parent == exe_dir or is_relative_to(proc_path.parent, exe_dir):
                    # Prefer the process using the most memory (likely the actual game)
                    try:
                        proc = psutil.Process(key[0])
                        mem = proc.memory_info().rss
                        if mem > best_mem:
                            best_mem = mem
                            best_pid = key[0]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        if not best_pid:
                            best_pid = key[0]
            except (OSError, ValueError, RuntimeError, RecursionError):
                continue

        if best_pid:
            logger.info(f"Found game process via directory match: PID {best_pid} "
                       f"(game_id={game_id}, install_dir={exe_dir})")
        return best_pid

    def get_tracked_snapshot(self) -> dict:
        """Return a thread-safe snapshot of tracked processes."""
        with self._data_lock:
            return {k: copy.deepcopy(v) for k, v in self._tracked.items()}

    def is_playing(self, game_id: str) -> bool:
        """Whether *game_id* has a tracked process right now.

        A dict scan comparing ids, and nothing else. The callers that only
        need this answer were going through currently_playing(), which
        DEEP-COPIES every tracked GameEntry — save paths, tags, history — to
        build a list they then reduced back to a set of ids. Cheap enough to
        be asked on the GUI thread, which currently_playing() was not.
        """
        if not game_id:
            return False
        with self._data_lock:
            return any(e is not None and e.id == game_id
                       for e in self._tracked.values())

    def tracked_pid_for(self, game_id: str) -> int:
        """A pid tracked for *game_id*, or 0. No process-table walk.

        The fallback find_game_process falls back to; kept separate so a
        caller that already knows the game is tracked never pays for the
        launcher/child search.
        """
        if not game_id:
            return 0
        with self._data_lock:
            for key, entry in self._tracked.items():
                if entry is not None and entry.id == game_id:
                    return key[0]
        return 0

    def start_tracking(self, entry, exe_path: str):
        """Add a game to tracked processes (thread-safe public API).

        A game added while it is ALREADY RUNNING — the overlay's "add this
        to my library" during a session — has, from everything downstream's
        point of view, just been launched: nothing has watched its saves,
        nothing has started its in-game backup timer, and nothing has
        recorded that it is being played. This used to register the pid and
        stop there, on the assumption that refresh_tracked (fired by the
        library's game_added signal) had already done the launch half.

        That assumption holds only while the process is sitting in _tracked
        as an unknown AND _match_process verifies it — and when it does not,
        the failure is completely silent: the game is tracked, so nothing
        looks wrong, but no watcher is armed, live tracking never runs, no
        saves are discovered during the session or offered at exit, and
        last_played is never written. So the launch half is done HERE, where
        the registration actually happens, and refresh_tracked's own emit is
        left alone — whichever gets there first wins and the other sees the
        entry already registered and does nothing.
        """
        current_processes = self._snapshot()
        for key, info in current_processes.items():
            if info["exe"] != exe_path:
                continue
            with self._data_lock:
                previous = self._tracked.get(key)
                self._register_tracked_locked(key, entry)
                if key not in self._running:
                    self._running[key] = info
            logger.info(f"Started tracking game: {entry.name} (pid={key[0]})")
            already_launched = (previous is not None
                                and getattr(previous, "id", None) == entry.id)
            if not already_launched:
                try:
                    entry.mark_played()
                    get_library().update_game(entry)
                except Exception:
                    logger.debug("Could not stamp last_played on %s",
                                 entry.name, exc_info=True)
                # Same guard refresh_tracked uses: a launcher already
                # tracked for this game owns the session, and emitting for
                # its child would run every subscriber a second time.
                if not self._has_tracked_ancestor(key[0], entry.id):
                    self.game_launched.emit(entry, exe_path)
            return


_monitor: ProcessMonitor | None = None
_monitor_lock = threading.Lock()


def get_monitor() -> ProcessMonitor:
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("ProcessMonitor requires QApplication — create QApplication first")
                _monitor = ProcessMonitor()
    return _monitor