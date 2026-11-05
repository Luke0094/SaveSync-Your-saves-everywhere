"""
SaveSync - the launch URL a game was started with.

A launcher URL (``steam://rungameid/<appid>``, ``com.epicgames.launcher://apps/...``, ``uplay://launch/<id>/0``…) from a desktop icon, a Start-menu entry or a
link is not handed to the game. Windows gives it to the program registered for the URL's scheme - Steam, Epic, GOG, Ubisoft, Battle.net and EA all register
theirs the same way - which starts, passes the URL to the launcher already running, and exits. The launcher then starts the game ITSELF: the game's parent is
the long-running launcher, whose command line holds no URL.

Recorded on real launches of a Steam game (the process table sampled every 13 ms, and Steam's own log), then read off the Epic launcher's:

    desktop icon   handler  steam.exe -- steam://rungameid/<appid>   parent: explorer.exe     2.2 s before the game
                   Steam log  ExecuteSteamURL: "steam://rungameid/<appid>"  ->  Game process added ... ProcID <the game>
    Play button    no URL in any process, and none in the log: the launcher hands nothing over

So a launch URL is looked for in this order (url_for_process / url_for_exe):

  1. The handler itself, while it lives (LaunchUrlWatcher). General: the handlers are DISCOVERED from the registry (every scheme that names a program, see
     HandlerTable), so a launcher needs no entry here. Steam's handler lives a few milliseconds and a 50 ms check missed it twice; the others are heavier
     programs. Only launch URLs are kept, from the program registered for their scheme, and only for a game that descends from that launcher.
  2. What the launcher wrote down about the process: Steam's console log and Epic's launcher log both record the URL they executed and the game that followed.
     Per launcher by nature (each writes its own format), so only these two are read.
  3. (the caller's) what a manifest says: the game its install folder belongs to - see core.resolvers.launcher_url_for_exe.

Nothing but the URL is kept of any command line (Epic's log lines carry a session code, which is never read). The check is a difference of process ids
(about 0.05 ms).
"""
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:                                      # pragma: no cover
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)

POLL_S = 0.05
# A URL older than this, set against the game that started after it, is not that game's. The real gap measured was 2 s; a launcher that has to update or log
# in first takes longer.
WINDOW_S = 120.0
_KEEP_S = 300.0
_MAX_KEPT = 32
_REDISCOVER_S = 1800.0

# ── what counts as a URL that STARTS a game ─────────────────────────────────────────────────────────────────────────
# The ones known for certain (with what an Epic icon, a Steam icon, a Ubisoft Connect one… hold)…
_STRONG = (
    ("steam", re.compile(r"^steam://(?:rungameid|run)/\d+", re.IGNORECASE)),
    ("epic", re.compile(r"^(?:com\.epicgames\.launcher://apps/|epic://launch/)", re.IGNORECASE)),
    ("gog", re.compile(r"^(?:goggalaxy://(?:openGameView|runGame)/|gog://game/)", re.IGNORECASE)),
    ("ubisoft", re.compile(r"^(?:uplay|ubisoft)://(?:launch|play)/", re.IGNORECASE)),
)
# …and, for a launcher nobody listed, any URL for a scheme a launcher registered that SAYS it launches and does not say it navigates.
_LAUNCH_VERB = re.compile(r"(?:^|[/?&=_.\-])(?:launch|run|rungameid|play|start|openGameView|runGame)(?:[/?&=_.\-]|$)", re.IGNORECASE)
_NAV_VERB = re.compile(r"(?:^|[/?&=_.\-])(?:nav|open|store|install|uninstall|friends|settings|library|community|browse|support|news|chat|profile)(?:[/?&=_.\-]|$)",
                       re.IGNORECASE)
_URL_IN_TOKEN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://[^\s\"']+")

# Only for a machine with no registry to ask (and so the pure checks): the programs that receive those URLs, by a fragment of their name.
_STATIC_HANDLERS = {"steam": ("steam",), "epic": ("epicgameslauncher",), "gog": ("galaxyclient", "goggalaxy"), "ubisoft": ("upc", "ubisoftconnect", "uplay")}
_STATIC_SCHEME_FAMILY = {"steam": "steam", "com.epicgames.launcher": "epic", "epic": "epic", "goggalaxy": "gog", "gog": "gog", "uplay": "ubisoft", "ubisoft": "ubisoft"}

# Programs that are registered for some scheme but start everything (the shell) or show pages (browsers): a game "descending" from one says nothing.
_NOT_LAUNCHERS = frozenset({
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "rundll32.exe", "wscript.exe", "cscript.exe", "mshta.exe", "svchost.exe",
    "chrome.exe", "firefox.exe", "msedge.exe", "brave.exe", "iexplore.exe", "opera.exe", "vivaldi.exe",
})
_OS_SCHEMES = re.compile(r"^(?:http|https|file|ftp|mailto|tel|sms|callto|search|ms-|windows|microsoft|ie$|read$|bingmaps|zune|webcal|mms|msnim|wsman|wpd|ldap|res$"
                         r"|its$|mk$|feed|cortana|outlookmail|about|javascript|vbscript|gopher|nntp|news|snews|telnet|tn3270|rlogin|shell|sip|swlauncher|onenote"
                         r"|osb|grvopen|limitless|explorer\.)", re.IGNORECASE)


class HandlerTable:
    """``{program name (lower): {schemes it is registered for}}`` - which program receives which kind of URL. Discovered from the registry on Windows
    (HKEY_CLASSES_ROOT: a key with a ``URL Protocol`` value and a ``shell\\open\\command``); empty elsewhere and until discovered."""

    def __init__(self, mapping: Optional[dict] = None):
        self._lock = threading.Lock()
        self._by_name: dict = {k.lower(): {s.lower() for s in v} for k, v in (mapping or {}).items()}
        self._loaded_at = 0.0

    def schemes_for(self, process_name: str) -> set:
        with self._lock:
            return set(self._by_name.get((process_name or "").lower(), ()))

    def names(self) -> set:
        with self._lock:
            return set(self._by_name)

    def discover(self) -> int:
        """Read the registry (Windows). About 2 s for ~7000 keys, so never on a thread that has something else to do. Returns how many programs."""
        if os.name != "nt":
            return 0
        try:
            import winreg
        except ImportError:                              # pragma: no cover
            return 0
        found: dict = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                i = 0
                while True:
                    try:
                        scheme = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if scheme.startswith(".") or _OS_SCHEMES.match(scheme):
                        continue
                    try:
                        with winreg.OpenKey(root, scheme) as k:
                            winreg.QueryValueEx(k, "URL Protocol")
                        with winreg.OpenKey(root, scheme + r"\shell\open\command") as c:
                            command = winreg.QueryValueEx(c, "")[0]
                    except OSError:
                        continue
                    exe = _program_of(command)
                    if exe and exe not in _NOT_LAUNCHERS:
                        found.setdefault(exe, set()).add(scheme.lower())
        except OSError:
            return 0
        with self._lock:
            self._by_name = found
            self._loaded_at = time.time()
        logger.info(f"URL-scheme handlers discovered: {len(found)} programs")
        return len(found)

    def stale(self) -> bool:
        return time.time() - self._loaded_at > _REDISCOVER_S


def _program_of(command: str) -> str:
    """The program name (lower) in a registered command line: quoted or not, environment variables expanded."""
    try:
        cmd = os.path.expandvars(str(command or "").strip())
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            exe = cmd[1:end] if end > 0 else ""
        else:
            m = re.match(r"(.+?\.exe)\b", cmd, re.IGNORECASE)
            exe = m.group(1) if m else ""
        return Path(exe).name.lower() if exe else ""
    except (ValueError, OSError):
        return ""


_handlers = HandlerTable()


def get_handlers() -> HandlerTable:
    return _handlers


def _static_family_of(process_name: str) -> str:
    low = (process_name or "").lower()
    for family, frags in _STATIC_HANDLERS.items():
        if any(f in low for f in frags):
            return family
    return ""


def is_launch_url(url: str, registered: bool = False) -> bool:
    """True when *url* starts a game rather than navigates: one of the known forms, or - for a scheme a launcher REGISTERED - one that says it launches and
    does not say it navigates. Pure."""
    for _family, rx in _STRONG:
        if rx.match(url):
            return True
    if not registered:
        return False
    rest = url.split("://", 1)[-1]
    return bool(_LAUNCH_VERB.search(rest)) and not _NAV_VERB.search(rest)


def extract_launch_url(process_name: str, cmdline, table: Optional[HandlerTable] = None) -> Optional[tuple]:
    """``(family, url)`` when *cmdline* - of the program called *process_name* - carries a URL that starts a game, else None. The URL may be a whole argument or
    sit inside one (``--uri="battlenet://…"``, ``/urlProtocol="goggalaxy://…"``); it is returned as written, launch options included
    (``steam://rungameid/<appid>//-windowed``). *family* is the URL's scheme, or the launcher's name for the known ones. Pure."""
    table = table or _handlers
    name = (process_name or "").lower()
    registered = table.schemes_for(name)
    static_family = _static_family_of(name)
    if not registered and not static_family:
        return None
    for token in cmdline or ():
        for m in _URL_IN_TOKEN.finditer(str(token)):
            scheme = m.group("scheme").lower()
            # an argument that IS the URL is taken whole (its launch options may hold spaces); one that merely contains it (--uri="…") up to its quote
            url = str(token).strip().strip("\"'") if m.start() == 0 else m.group(0).rstrip("\"'")
            if scheme in registered:
                if is_launch_url(url, registered=True):
                    return _STATIC_SCHEME_FAMILY.get(scheme, scheme), url
            elif static_family and _STATIC_SCHEME_FAMILY.get(scheme) == static_family and is_launch_url(url):
                return static_family, url
    return None


class LaunchUrlLog:
    """The launch URLs seen lately, newest last. Thread-safe and bounded."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=_MAX_KEPT)

    def note(self, family: str, url: str, handler_pid: int = 0, when: Optional[float] = None, handler: str = "", handler_dir: str = "") -> None:
        with self._lock:
            self._items.append({"family": family, "url": url, "pid": handler_pid, "ts": time.time() if when is None else when,
                                "handler": (handler or "").lower(), "dir": handler_dir})

    def recent(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        with self._lock:
            return [dict(i) for i in self._items if now - i["ts"] <= _KEEP_S]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_log = LaunchUrlLog()


def get_log() -> LaunchUrlLog:
    return _log


def _steam_app_id(url: str) -> str:
    m = re.search(r"(?:rungameid|run)/(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


def _epic_app_name(url: str) -> str:
    """The AppName (third part) of ``com.epicgames.launcher://apps/<ns>%3A<item>%3A<AppName>?action=launch``."""
    m = re.search(r"apps/([^?\s]+)", url, re.IGNORECASE)
    if not m:
        return ""
    parts = re.split(r"%3A|:", m.group(1), flags=re.IGNORECASE)
    return parts[-1].lower() if len(parts) >= 3 else ""


def _identity(url: str) -> tuple:
    """(family, key) of a launch URL: what two URLs for the SAME game share, whatever else is written after it. ("", "") when that cannot be said."""
    low = url.lower()
    if low.startswith("steam://"):
        return "steam", _steam_app_id(url)
    if low.startswith(("com.epicgames.launcher://", "epic://")):
        return "epic", _epic_app_name(url)
    return "", ""


def _descends_from(item: dict, ancestors: list) -> bool:
    """Is the launcher that received this URL among the game's parents? Same program name (the usual case - the handler IS the launcher, started a second
    time), or a program from the same folder (a launcher whose handler is not the process that stays running), or - with no registry to say - the same launcher
    by the name fragments."""
    handler, hdir = item.get("handler", ""), item.get("dir", "")
    frags = _STATIC_HANDLERS.get(item.get("family", ""), ())
    for a in ancestors:
        name, path = (a if isinstance(a, tuple) else (a, ""))
        low = str(name).lower()
        if handler and low == handler:
            return True
        if hdir and path and os.path.normcase(os.path.dirname(path)) == os.path.normcase(hdir):
            return True
        if frags and any(f in low for f in frags):
            return True
    return False


def url_for_game(game_started: float, ancestor_names, exe_path: str = "", log: Optional[LaunchUrlLog] = None, window: float = WINDOW_S) -> Optional[str]:
    """The launch URL that started the game which came up at *game_started* (epoch seconds), or None.

    A candidate is a URL seen up to *window* seconds BEFORE the game (a few seconds after it are allowed for clock granularity), received by a launcher the
    game descends from (*ancestor_names*: its parents - names, or (name, path) pairs) - so a link opened for another launcher never lands on this game -
    and, where the install says which game that is (Steam's and Epic's manifests), the URL must be for THAT game: two games started a minute apart each get
    their own. The newest candidate wins."""
    log = log or _log
    ancestors = list(ancestor_names or ())
    own = ("", "")
    if exe_path:
        try:
            from core.resolvers import launcher_url_for_exe
            own = _identity(launcher_url_for_exe(exe_path) or "")
        except Exception:
            own = ("", "")
    for c in sorted(log.recent(now=game_started + 5.0), key=lambda i: i["ts"], reverse=True):
        if not (game_started - window <= c["ts"] <= game_started + 3.0):
            continue
        if not _descends_from(c, ancestors):
            continue
        seen = _identity(c["url"])
        if own[1] and seen[0] == own[0] and seen[1] != own[1]:
            continue                                 # (a URL for another game of the same launcher)
        return c["url"]
    return None


# ── Steam writes down the URL it executed ─────────────────────────────────────────────────────────────────────────
# The handler of a steam:// link lives a few milliseconds, so catching it is a race. Steam itself keeps the record, in logs/console_log.txt (local time),
# in an order that ties it to the game:
#
#     [18:37:06] ExecuteSteamURL: "steam://rungameid/<appid>"
#     [18:37:08] Game process added : AppID <appid> ""D:\...\<game>.exe"", ProcID <pid>, IP 0.0.0.0:0
#     [18:37:55] GameAction [AppID <appid>, ActionID <n>] : LaunchApp ...        (Play button: NO URL line before it)
#     [18:37:56] Game process added : AppID <appid> ""D:\...\<game>.exe"", ProcID <pid>, IP 0.0.0.0:0
#
# A URL belongs to the first game process added after it, and to that one only. It is the URL as it was really written - `steam://run/<appid>//` is another
# form the same log shows - launch options included.
_LOG_TS = "%Y-%m-%d %H:%M:%S"
_RX_ADDED = re.compile(r'^\[(?P<ts>[\d\- :]+)\] Game process added : AppID (?P<app>\d+) "+(?P<exe>.*?)"+, ProcID (?P<pid>\d+)')
_RX_EXEC = re.compile(r'^\[(?P<ts>[\d\- :]+)\] ExecuteSteamURL: "(?P<url>[^"]*)"')
_RX_LAUNCH = re.compile(r"^steam://(?:rungameid|run)/(?P<app>\d+)", re.IGNORECASE)
_URL_MAX_AGE_S = 120.0
_LOG_TAIL_BYTES = 400_000


def parse_steam_console_log(text: str, pid: int, exe_path: str = "") -> Optional[dict]:
    """What Steam's console log says about the game process *pid* (or, failing a pid, the last one started from *exe_path*):
    ``{"app": "<appid>", "url": "<the URL it was started with, or ''>"}``; None when the log does not mention it. Pure."""
    import datetime as _dt

    def _ts(s):
        try:
            return _dt.datetime.strptime(s, _LOG_TS)
        except ValueError:
            return None

    want_exe = (exe_path or "").replace("/", "\\").casefold()
    pending = None                                         # the launch URL waiting for the next game process
    by_pid = by_exe = None
    for line in text.splitlines():
        m = _RX_EXEC.match(line)
        if m:
            if _RX_LAUNCH.match(m.group("url")):          # (a navigation, an install... is not what starts a game)
                pending = (_ts(m.group("ts")), m.group("url"))
            continue
        m = _RX_ADDED.match(line)
        if not m:
            continue
        url = ""
        if pending is not None:
            at, cand = pending
            added = _ts(m.group("ts"))
            app_in_url = _RX_LAUNCH.match(cand).group("app")
            if (at is not None and added is not None and 0 <= (added - at).total_seconds() <= _URL_MAX_AGE_S
                    and app_in_url == m.group("app")):
                url = cand
        pending = None                                     # (that game process has consumed it, whether it matched or not)
        found = {"app": m.group("app"), "url": url}
        if int(m.group("pid")) == pid:
            by_pid = found
        if want_exe and m.group("exe").replace("/", "\\").casefold() == want_exe:
            by_exe = found
    return by_pid or by_exe


def steam_log_launch(pid: int, exe_path: str = "") -> Optional[dict]:
    """parse_steam_console_log on the tail of this machine's Steam log; None when Steam keeps none here or it does not mention the process."""
    try:
        from core.resolvers import _get_launcher_install_paths
        roots = list(_get_launcher_install_paths().get("steam", []))
    except Exception:
        roots = []
    for root in roots:
        path = root / "logs" / "console_log.txt"
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > _LOG_TAIL_BYTES:
                    f.seek(size - _LOG_TAIL_BYTES)
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        hit = parse_steam_console_log(text, pid, exe_path)
        if hit:
            return hit
    return None


# ── Epic writes down the URL it was given, too ─────────────────────────────────────────────────────────────────────
# %LOCALAPPDATA%/EpicGamesLauncher/Saved/Logs/EpicGamesLauncher.log, times in UTC (Steam's are local):
#
#     [2026.11.01-19.25.11:709][  1]LogUriHandler: Started processing URI [com.epicgames.launcher://apps/<ns>%3A<item>%3A<app>?action=launch&silent=true -AllowSoftwareRendering]
#     [2026.11.01-19.25.16:160][ 85]FCommunityPortalLaunchAppTask: Launching app 'D:/.../Game.exe' with commandline ' ... '
#
# The URL belongs to the first game launched after it. The "Launching app" line carries the program (no pid) - and a command line with a session exchange code in
# it, which is why only the program's path is ever read from that line, never the rest, and nothing of it is logged or kept.
_RX_EPIC_URI = re.compile(r"^\[(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}):\d+\]\[\s*\d+\]LogUriHandler: Started processing URI \[(?P<uri>[^\s\]]+)")
_RX_EPIC_LAUNCH = re.compile(r"^\[(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}):\d+\]\[\s*\d+\]FCommunityPortalLaunchAppTask: Launching app '(?P<exe>[^']+)'")
_RX_EPIC_LAUNCH_URL = re.compile(r"^(?:com\.epicgames\.launcher://apps/|epic://launch/)", re.IGNORECASE)
_EPIC_LOG_TAIL_BYTES = 3_000_000


def parse_epic_launcher_log(text: str, exe_path: str, now: Optional[float] = None, max_age: Optional[float] = None) -> Optional[dict]:
    """What Epic's launcher log says about the LAST time it launched *exe_path*: ``{"url": "<the URL it was given, or ''>"}``; None when it never did (or, with
    *max_age*, not within that many seconds of *now*). A URL is the one for THIS game: its AppName must be the game's own when the manifest can say which that
    is. Pure."""
    import calendar

    def _epoch(s):
        try:
            return calendar.timegm(time.strptime(s, "%Y.%m.%d-%H.%M.%S"))
        except ValueError:
            return None

    want = (exe_path or "").replace("\\", "/").casefold()
    if not want:
        return None
    own_app = ""
    try:
        from core.resolvers import epic_manifest_for_exe
        own_app = ((epic_manifest_for_exe(exe_path) or {}).get("app") or "").lower()
    except Exception:
        own_app = ""
    pending = None
    found = None
    for line in text.splitlines():
        m = _RX_EPIC_URI.match(line)
        if m:
            if _RX_EPIC_LAUNCH_URL.match(m.group("uri")):
                pending = (_epoch(m.group("ts")), m.group("uri"))
            continue
        m = _RX_EPIC_LAUNCH.match(line)
        if not m:
            continue
        at = _epoch(m.group("ts"))
        url = ""
        if pending is not None and pending[0] is not None and at is not None and 0 <= at - pending[0] <= _URL_MAX_AGE_S:
            if not own_app or _epic_app_name(pending[1]) in ("", own_app):
                url = pending[1]
        pending = None                                      # (that launch has consumed it, whether it matched or not)
        if m.group("exe").replace("\\", "/").casefold() == want:
            found = {"url": url, "at": at}
    if found is None:
        return None
    if max_age is not None and (found["at"] is None or abs((now if now is not None else time.time()) - found["at"]) > max_age):
        return None
    return {"url": found["url"]}


def epic_log_launch(exe_path: str, max_age: Optional[float] = None) -> Optional[dict]:
    """parse_epic_launcher_log on this machine's Epic launcher log; None when there is none or it does not mention the program."""
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        return None
    path = Path(base) / "EpicGamesLauncher" / "Saved" / "Logs" / "EpicGamesLauncher.log"
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > _EPIC_LOG_TAIL_BYTES:
                f.seek(size - _EPIC_LOG_TAIL_BYTES)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return parse_epic_launcher_log(text, exe_path, max_age=max_age)


def url_for_exe(exe_path: str) -> Optional[str]:
    """The launch URL of a game that is NOT running now (it is being added from the queue, after the fact): what the launcher's log says about the last time this
    program was started - the URL, or for Steam's Play button the canonical URL of its app. None when no launcher log knows it."""
    hit = steam_log_launch(0, exe_path)
    if hit:
        return hit["url"] or f"steam://rungameid/{hit['app']}"
    hit = epic_log_launch(exe_path)
    if hit and hit["url"]:
        return hit["url"]
    return None


def learn_launch_url(game_id: str, pid: int, exe_path: str, log: Optional[LaunchUrlLog] = None) -> str:
    """A game the library already has was just started: if its card holds no URL to start it with, take the one this launch shows.

    The player may have added it from a launcher's Play button (which hands over no URL) and then started it from a desktop icon, or the other way round;
    whenever a launch brings the URL the card lacks, the card gets it. An entry that has a launcher URL is never changed - it may be the player's own - while an
    empty one, or a bare id (just the number cannot start anything), is replaced. The library announces the change, so the card redraws by itself. Returns the URL
    set, or "" when nothing was changed."""
    try:
        from core.library import get_library
        from core.resolvers import is_launcher_url, launcher_url_for_exe
        entry = get_library().get_by_id(game_id)
        if entry is None or (entry.appid and is_launcher_url(entry.appid)):
            return ""
        url = url_for_process(pid, exe_path, log)
        if not url:
            try:
                from core.monitor import _get_launcher_appid
                url = _get_launcher_appid(pid)
            except Exception:
                url = None
        if not url:
            url = launcher_url_for_exe(exe_path)
        if not url:
            return ""
        get_library().update_game_fields(game_id, appid=url)
        logger.info(f"{entry.name!r}: its launch URL, which the card lacked, is now {url!r}")
        return url
    except Exception:
        logger.debug("Could not learn a launch URL", exc_info=True)
        return ""


def url_for_process(pid: int, exe_path: str = "", log: Optional[LaunchUrlLog] = None) -> Optional[str]:
    """The launch URL for a running game, from the most exact source there is: the URL caught on its way to a launcher (url_for_game), then what a launcher's log
    wrote down about this very program - its URL if it was started by one; for Steam's Play button, the canonical URL of the app the log says the process is."""
    if not PSUTIL_AVAILABLE:
        return None
    try:
        proc = psutil.Process(pid)
        started = proc.create_time()
        ancestors, cur = [], proc
        for _ in range(8):
            cur = cur.parent()
            if cur is None:
                break
            try:
                ancestors.append((cur.name(), cur.exe()))
            except psutil.Error:
                ancestors.append((cur.name(), ""))
        caught = url_for_game(started, ancestors, exe_path, log)
    except Exception:
        caught = None
    if caught:
        return caught
    hit = steam_log_launch(pid, exe_path)
    if hit:
        return hit["url"] or f"steam://rungameid/{hit['app']}"
    hit = epic_log_launch(exe_path, max_age=300.0)         # (Epic's log names the program, not the process: only a launch of the last minutes is this one)
    if hit and hit["url"]:
        return hit["url"]
    return None


class LaunchUrlWatcher(threading.Thread):
    """Notices each new process and, for the programs registered to receive launcher URLs, keeps the launch URL on their command line."""

    def __init__(self, interval: float = POLL_S, log: Optional[LaunchUrlLog] = None, table: Optional[HandlerTable] = None, discover: bool = False):
        super().__init__(name="launch-url-watcher", daemon=True)
        self._interval = interval
        self._log = log or _log
        self._table = table or _handlers
        self._discover = discover
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def _rediscover(self) -> None:
        # on a thread of its own: the scan takes about two seconds, and the check below must not stop for them
        threading.Thread(target=self._table.discover, name="launch-url-handlers", daemon=True).start()

    def run(self) -> None:
        if not PSUTIL_AVAILABLE:
            return
        if self._discover:
            self._rediscover()
        seen = set(psutil.pids())
        while not self._stop_evt.wait(self._interval):
            if self._discover and self._table.stale():
                self._table._loaded_at = time.time()          # (not again until the scan has had its half hour)
                self._rediscover()
            try:
                cur = set(psutil.pids())
            except Exception:
                continue
            for pid in cur - seen:
                self._look(pid)
            seen = cur

    def _look(self, pid: int) -> None:
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            low = name.lower()
            if not (self._table.schemes_for(low) or _static_family_of(low)):
                return                                   # (the name first: it is cheap, and most new programs have nothing to do with launchers)
            hit = extract_launch_url(name, proc.cmdline(), self._table)
            try:
                folder = os.path.dirname(proc.exe())
            except Exception:
                folder = ""
        except Exception:
            return                                       # (gone already, or not ours to inspect)
        if hit:
            self._log.note(hit[0], hit[1], pid, handler=low, handler_dir=folder)
            logger.info(f"Launch URL seen: {hit[1]} (received by {name}, pid {pid})")


_watcher: Optional[LaunchUrlWatcher] = None
_watcher_lock = threading.Lock()


def start() -> None:
    """Start watching (once). Safe to call again. The handlers are discovered from the registry in the background."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher.is_alive():
            return
        _watcher = LaunchUrlWatcher(discover=True)
        _watcher.start()


def stop() -> None:
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


if __name__ == "__main__":                               # a standalone run, to see what is caught: python -m core.launch_urls 120
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start()
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    t0 = time.time()
    try:
        while time.time() - t0 < secs:
            time.sleep(0.5)
    finally:
        stop()
    for item in get_log().recent():
        print(f"caught {item['url']}  ({item['family']}, handler {item['handler']} pid {item['pid']}, {time.strftime('%H:%M:%S', time.localtime(item['ts']))})")
    print("done")
