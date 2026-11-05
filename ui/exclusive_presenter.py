"""
SaveSync - Presenting the overlay over a game in exclusive fullscreen (Windows).

A game in exclusive fullscreen owns the screen output. Showing an ORDINARY window over it takes the
game back to the desktop within about half a second - measured on a Direct3D 11 title, in the
ordinary band and in the higher "system tools" band alike, so the band alone is not what decides it.

A LAYERED window in that higher band is different, and measured on the same title: it stayed in the
front for the whole run, shown as a picture with per-pixel alpha - click-through, and also when it
accepted the mouse (moves and presses reached it; it never took the focus). The same layered window
in the ordinary band is "shown" but never displayed above the game, so the band is needed too. That
is what this module creates: one window whose pixels come from an image.

It knows nothing about Qt: it is handed a width, a height and premultiplied BGRA bytes (which is
what QImage.Format_ARGB32_Premultiplied holds in memory on Windows), so the caller renders the
notification however it likes. With ``click_through=False`` the window takes the mouse and reports it
to a callback - which window the caller then maps to its own buttons; it never takes the focus.

``band`` picks the window API: an int uses the undocumented ``CreateWindowInBand`` (16 =
ZBID_SYSTEM_TOOLS, the one higher band an ordinary process may use) and CHECKS that the window really
landed there; ``None`` uses the documented ``CreateWindowExW``. Whatever cannot be done - not Windows,
the API missing or refused, the window placed in another band - makes present() return False, and
the caller does what it did before this existed.
"""
import ctypes
import logging
import platform
import threading
from ctypes import wintypes
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

BAND_SYSTEM_TOOLS = 16

_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE, _SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
_HWND_TOPMOST = -1
_ULW_ALPHA = 0x2
_SW_HIDE = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


_CLASS_NAME = "SaveSyncExclusivePicture"
_class_state = {"registered": False, "proc": None}
_mouse_callbacks: dict = {}                     # hwnd -> callable(kind, x, y)       left button, moves, leave only
_MOUSE_KINDS = {0x0200: "move", 0x0201: "down", 0x0202: "up", 0x02A3: "leave"}   # WM_MOUSEMOVE/LBUTTONDOWN/LBUTTONUP/MOUSELEAVE
_tracking: set = set()                          # windows asked to report the mouse leaving


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND),
                ("dwHoverTime", wintypes.DWORD)]


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE),
                ("ptScreenPos", wintypes.POINT)]


_libs = None


def _load():
    """(user32, gdi32) with argument types set. Library objects of their own, so the types set
    here cannot leak into the shared ctypes.windll the rest of the program calls."""
    global _libs
    if _libs is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        ex_args = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.argtypes = ex_args
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowInBand.argtypes = ex_args + [wintypes.DWORD]
        u.CreateWindowInBand.restype = wintypes.HWND
        u.GetWindowBand.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowBand.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HANDLE
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]
        u.EnumDisplaySettingsW.restype = wintypes.BOOL
        u.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
        u.TrackMouseEvent.restype = wintypes.BOOL
        u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.ScreenToClient.restype = wintypes.BOOL
        u.SetCursor.argtypes = [wintypes.HANDLE]
        u.SetCursor.restype = wintypes.HANDLE
        u.ClipCursor.argtypes = [ctypes.c_void_p]
        u.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
        u.GetCursorInfo.restype = wintypes.BOOL
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
            ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]
        u.UpdateLayeredWindow.restype = wintypes.BOOL
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        _libs = (u, g)
    return _libs


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitor_rect(hwnd):
    """(left, top, right, bottom) of the monitor *hwnd* is on, in physical pixels, or None."""
    try:
        u, _g = _load()
        hmon = u.MonitorFromWindow(hwnd, 2)               # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def display_modes(hwnd):
    """((current_w, current_h), (desktop_w, desktop_h)) of the monitor *hwnd* is on, or None.

    "Desktop" is the mode the person chose in Windows' settings for that monitor (the registry
    mode); "current" is the one in force right now. A game in exclusive fullscreen that changed the
    resolution makes them differ, and the exclusive mode ending makes them equal again. Read per
    monitor and in physical pixels, so a game on a second screen, or a process that is not DPI
    aware, is judged correctly - GetSystemMetrics would answer for the primary screen only."""
    try:
        u, _g = _load()
        hmon = u.MonitorFromWindow(hwnd, 2)               # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        cur, reg = _DEVMODEW(), _DEVMODEW()
        cur.dmSize = reg.dmSize = ctypes.sizeof(_DEVMODEW)
        if not u.EnumDisplaySettingsW(info.szDevice, 0xFFFFFFFF, ctypes.byref(cur)):     # ENUM_CURRENT_SETTINGS
            return None
        if not u.EnumDisplaySettingsW(info.szDevice, 0xFFFFFFFE, ctypes.byref(reg)):     # ENUM_REGISTRY_SETTINGS
            return None
        return (cur.dmPelsWidth, cur.dmPelsHeight), (reg.dmPelsWidth, reg.dmPelsHeight)
    except Exception:
        return None


def display_mode_switch(hwnd) -> str:
    """"1920x1080 on a 3840x2160 desktop" when a game has switched the display mode of its monitor (the mode in force differs from
    the one in Windows' settings), else "". A game in exclusive fullscreen that picks its own resolution does this; a borderless one
    never does - which tells the two apart when the shell cannot: it said QUNS_BUSY ("not exclusive") for a game that had just switched
    its monitor to 1920x1080 and went on saying so (the log of a real game: sixteen screen changes at 01:15:50, state 2 at 01:15:51, and
    the overlay shown as an ordinary window - invisible - from then on)."""
    modes = display_modes(hwnd)
    if modes and modes[0] != modes[1]:
        return f"{modes[0][0]}x{modes[0][1]} on a {modes[1][0]}x{modes[1][1]} desktop"
    return ""


# ── Is a game in EXCLUSIVE fullscreen in front? (the overlay and the pins ask the same thing) ────────────
# SHQueryUserNotificationState: what the shell itself believes about the foreground. QUNS_RUNNING_D3D_FULL_SCREEN
# is its flag for "a full-screen EXCLUSIVE-mode Direct3D application is running" - the only case where showing a
# window takes the game out of its display mode. A borderless window (SDL/OpenGL, NW.js, Electron, a game set to
# "borderless" or "windowed fullscreen") reports QUNS_BUSY instead and composes with other windows like any other.
QUNS_RUNNING_D3D_FULL_SCREEN = 3


_qstate = {"value": None, "thread": None}
_qstate_lock = threading.Lock()


def _ask_the_shell() -> None:
    """(On a thread of its own.) Records the shell's answer."""
    value = None
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            value = int(state.value)
    except Exception:
        value = None
    with _qstate_lock:
        _qstate["value"] = value


def shell_notification_state():
    """The shell's QUNS_* state as an int, or None when it cannot be asked.

    The question is put to the shell on a thread of its own and waited for a quarter of a second at most: while a game is switching
    its display mode the shell does not answer for seconds (the stall watchdog of a real game: 5.6 s and 7 s inside this very call, on
    the GUI thread - the application dead meanwhile, hotkey and notices included). A question still unanswered is not asked again;
    the last answer stands until it comes."""
    if platform.system() != "Windows":
        return None
    with _qstate_lock:
        running = _qstate["thread"]
        if running is not None and running.is_alive():
            return _qstate["value"]
        worker = threading.Thread(target=_ask_the_shell, name="shell-state", daemon=True)
        _qstate["thread"] = worker
    worker.start()
    worker.join(0.25)
    with _qstate_lock:
        return _qstate["value"]


def window_fills_monitor(hwnd: int) -> bool:
    """True when *hwnd* covers the whole monitor it is on (to 2 px).

    Both rectangles come from Win32, in the same units. Qt's screen geometry is in device-independent units, so
    comparing a window's real size with it only agrees at 100 % scaling: on a 3840x2160 display at 150 % the
    desktop window is 3840 wide and Qt's screen 2560, and no window at the native resolution ever matched.
    """
    if platform.system() != "Windows" or not hwnd:
        return False
    try:
        u, _g = _load()
        hmon = u.MonitorFromWindow(hwnd, 2)             # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        win = wintypes.RECT()
        if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(info)) or not u.GetWindowRect(hwnd, ctypes.byref(win)):
            return False
        mon = info.rcMonitor
        return (abs((win.right - win.left) - (mon.right - mon.left)) <= 2
                and abs((win.bottom - win.top) - (mon.bottom - mon.top)) <= 2
                and mon.left <= (win.left + win.right) // 2 < mon.right
                and mon.top <= (win.top + win.bottom) // 2 < mon.bottom)
    except Exception:
        return False


def is_exclusive_hwnd(hwnd: int) -> bool:
    """Is *hwnd* a game in exclusive fullscreen: chrome-less, the size of its monitor, WHILE the shell reports an
    exclusive-mode Direct3D application. When the shell cannot be asked the cautious answer is yes."""
    if platform.system() != "Windows" or not hwnd:
        return False
    try:
        u, _g = _load()
        if not u.IsWindow(hwnd):
            return False
        if u.GetWindowLongW(hwnd, -16) & (0x00C00000 | 0x00800000):        # WS_CAPTION | WS_BORDER: borderless at most
            return False
        if not window_fills_monitor(hwnd):
            return False
        state = shell_notification_state()
        if state is None or state == QUNS_RUNNING_D3D_FULL_SCREEN:
            if state is not None:
                _exclusive_seen["hwnd"] = hwnd
            return True
        if state == 2:
            # QUNS_BUSY for a game the shell called an exclusive Direct3D application a moment ago: the shell says so, every time,
            # half a second after a window of ours comes up over it (the log of a real game: 3, then 2). Taken as "borderless" the
            # pins gave up their layered windows and were remade in the ordinary way - the game was taken to the desktop. Or for a game
            # that has switched the display mode of its monitor, which no borderless game does (see display_mode_switch).
            from ui import win_band_hook
            if win_band_hook.installed() and (hwnd == _exclusive_seen["hwnd"] or display_mode_switch(hwnd)):
                _exclusive_seen["hwnd"] = hwnd
                return True
        if state != 2:
            _exclusive_seen["hwnd"] = 0
        return False
    except Exception:
        return False


_exclusive_seen = {"hwnd": 0, "last": False}


def foreground_is_exclusive() -> bool:
    """Is the window in front a game in exclusive fullscreen? With no window in front at all - a game switching its display mode, for
    a moment - the last answer stands."""
    try:
        hwnd = int(_load()[0].GetForegroundWindow() or 0)
        if not hwnd:
            return bool(_exclusive_seen["last"])
        verdict = is_exclusive_hwnd(hwnd)
        _exclusive_seen["last"] = verdict
        return verdict
    except Exception:
        return False


def monitors_physical():
    """Every monitor as (left, top, right, bottom, gdi_device_name) in physical pixels. Empty when it cannot be
    asked. How a position in Qt's units is turned into a position on the screen: see ui.pin_mirror."""
    if platform.system() != "Windows":
        return []
    found = []
    try:
        u, _g = _load()
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       wintypes.LPARAM)

        def cb(hmon, _hdc, _rect, _lp):
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if u.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                found.append((r.left, r.top, r.right, r.bottom, info.szDevice))
            return True

        lib = ctypes.WinDLL("user32")
        lib.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, proc_type, wintypes.LPARAM]
        lib.EnumDisplayMonitors(None, None, proc_type(cb), 0)
    except Exception:
        return []
    return found


def foreground_hwnd() -> int:
    """The window in front, or 0."""
    try:
        return int(_load()[0].GetForegroundWindow() or 0)
    except Exception:
        return 0


def cursor_position():
    """(x, y) of the pointer in physical pixels, or None."""
    try:
        pt = wintypes.POINT()
        if _load()[0].GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:
        pass
    return None


def free_cursor() -> None:
    """Undo the pointer clip a game puts on the mouse, so the pointer can reach a clickable picture.
    A game that clips every frame needs this repeated (a short timer); that is the caller's job."""
    try:
        _load()[0].ClipCursor(None)
    except Exception:
        pass


def cursor_state():
    """(showing, x, y) of the system cursor in physical pixels, or None. *showing* is False when a
    game has hidden the pointer - by counting it away (ShowCursor) or by giving the window under it no
    cursor at all (SetCursor(NULL), what most engines do) - then the caller draws its own, the same
    safe way. Both were measured from another process: the flag drops in each case and the handle is 0."""
    try:
        info = _CURSORINFO()
        info.cbSize = ctypes.sizeof(_CURSORINFO)
        if _load()[0].GetCursorInfo(ctypes.byref(info)):
            shown = bool(info.flags & 1) and bool(info.hCursor)                              # CURSOR_SHOWING
            return shown, int(info.ptScreenPos.x), int(info.ptScreenPos.y)
    except Exception:
        pass
    return None


def process_name(hwnd) -> str:
    """The executable name (e.g. "Game-Win64-Shipping.exe") of the process that owns *hwnd*, or ""."""
    try:
        import psutil
        pid = wintypes.DWORD(0)
        _load()[0].GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name() if pid.value else ""
    except Exception:
        return ""


def _register_class() -> bool:
    """Register the window class of a CLICKABLE picture: its window procedure hands mouse moves and
    left-button presses to the callback of the window they happened in, and refuses to be activated -
    a click on it must never take the front from the game. Windows only; once per process."""
    if _class_state["registered"]:
        return True
    u, _g = _load()
    wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", wndproc_type),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON)]

    def proc(hwnd, msg, wparam, lparam):
        try:
            if msg == 0x0021:                                   # WM_MOUSEACTIVATE
                return 3                                        # MA_NOACTIVATE
            kind = _MOUSE_KINDS.get(msg)
            if kind is not None:
                h = int(hwnd or 0)
                if kind == "move" and h not in _tracking:        # ask for the WM_MOUSELEAVE of this hover
                    _tracking.add(h)
                    u.TrackMouseEvent(ctypes.byref(_TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), 2, hwnd, 0)))
                elif kind == "leave":
                    _tracking.discard(h)
                cb = _mouse_callbacks.get(h)
                if cb is not None:
                    cb(kind, ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
                    return 0
        except Exception:
            logger.debug("exclusive picture: window procedure error", exc_info=True)
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    k = ctypes.WinDLL("kernel32")
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HINSTANCE
    u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    u.RegisterClassExW.restype = wintypes.ATOM
    u.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    u.LoadCursorW.restype = wintypes.HANDLE
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = wndproc_type(proc)
    wc.hInstance = k.GetModuleHandleW(None)
    wc.hCursor = u.LoadCursorW(None, 32512)                     # IDC_ARROW
    wc.lpszClassName = _CLASS_NAME
    if not u.RegisterClassExW(ctypes.byref(wc)):
        logger.info("exclusive picture: could not register the window class (error %s)", ctypes.get_last_error())
        return False
    _class_state["proc"] = wc.lpfnWndProc                       # kept alive for as long as the process runs
    _class_state["registered"] = True
    return True


class ExclusivePresenter:
    """One reusable window. present() shows an image in it (or replaces the one on
    show); hide() takes it off screen; destroy() releases it. Never more than one window."""

    def __init__(self, band=BAND_SYSTEM_TOOLS, click_through=True, on_mouse=None):
        """*click_through* False makes the window take the mouse: *on_mouse*(kind, x, y) is called -
        on the thread that pumps messages - with kind "move", "down" or "up" (the left button) and the
        position inside the picture, in its own pixels - or "leave" when the pointer goes.
        The window never takes the front either way."""
        self._band = band
        self._hwnd = None
        self._click_through = click_through
        self._on_mouse = on_mouse

    @staticmethod
    def supported() -> bool:
        return platform.system() == "Windows"

    @property
    def hwnd(self):
        return int(self._hwnd) if self._hwnd else 0

    @property
    def visible(self) -> bool:
        if not self._hwnd:
            return False
        try:
            return bool(_load()[0].IsWindowVisible(self._hwnd))
        except Exception:
            return False

    def present(self, width: int, height: int, bgra, x: int, y: int) -> bool:
        """Show *bgra* (premultiplied, top row first, width*height*4 bytes) with its top-left
        corner at (x, y) in physical screen pixels. False when it could not be done."""
        if not self.supported() or width <= 0 or height <= 0 or len(bgra) < width * height * 4:
            return False
        try:
            u, _g = _load()
            if not self._ensure_window():
                return False
            if not self._paint(width, height, bgra, x, y):
                return False
            # topmost among the topmost, without taking the focus
            u.SetWindowPos(self._hwnd, wintypes.HWND(_HWND_TOPMOST), 0, 0, 0, 0,
                           _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
            return True
        except Exception:
            logger.debug("Could not present the notification over exclusive fullscreen", exc_info=True)
            return False

    def hide(self) -> None:
        if self._hwnd:
            try:
                _load()[0].ShowWindow(self._hwnd, _SW_HIDE)
            except Exception:
                pass

    def destroy(self) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd:
            _mouse_callbacks.pop(int(hwnd), None)
            _tracking.discard(int(hwnd))
            try:
                _load()[0].DestroyWindow(hwnd)
            except Exception:
                pass

    # ── internals ────────────────────────────────────────────────────────────

    def _ensure_window(self) -> bool:
        u, _g = _load()
        if self._hwnd and u.IsWindow(self._hwnd):
            return True
        self._hwnd = None
        ex = _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE | _WS_EX_LAYERED
        cls = "Static"
        if self._click_through:
            ex |= _WS_EX_TRANSPARENT
        elif _register_class():
            cls = _CLASS_NAME
        else:
            return False
        # "Static" is a system window class: no window procedure of our own to keep alive, and
        # nothing it paints matters - the pixels come from UpdateLayeredWindow.
        if self._band is None:
            hwnd = u.CreateWindowExW(ex, cls, "", _WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        else:
            hwnd = u.CreateWindowInBand(ex, cls, "", _WS_POPUP, 0, 0, 1, 1,
                                        None, None, None, None, self._band)
        if not hwnd:
            logger.info("Exclusive-fullscreen window refused (band %s, error %s)",
                        self._band, ctypes.get_last_error())
            return False
        if self._band is not None:
            actual = wintypes.DWORD(0xFFFF)
            if not u.GetWindowBand(hwnd, ctypes.byref(actual)) or actual.value != self._band:
                logger.info("Exclusive-fullscreen window landed in band %s, not %s - not used",
                            actual.value, self._band)
                u.DestroyWindow(hwnd)
                return False
        self._hwnd = hwnd
        if self._on_mouse is not None:
            _mouse_callbacks[int(hwnd)] = self._on_mouse
        return True

    def _paint(self, w: int, h: int, bgra, x: int, y: int) -> bool:
        u, g = _load()
        screen_dc = mem_dc = bitmap = old = None
        try:
            screen_dc = u.GetDC(None)
            mem_dc = g.CreateCompatibleDC(screen_dc)
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = w
            info.bmiHeader.biHeight = -h              # top-down: row 0 is the top of the image
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            bits = ctypes.c_void_p()
            bitmap = g.CreateDIBSection(mem_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                return False
            ctypes.memmove(bits, bytes(bgra[:w * h * 4]), w * h * 4)
            old = g.SelectObject(mem_dc, bitmap)
            blend = _BLENDFUNCTION(0, 0, 255, 1)                  # AC_SRC_OVER, per-pixel alpha
            ok = u.UpdateLayeredWindow(
                self._hwnd, screen_dc, ctypes.byref(wintypes.POINT(x, y)),
                ctypes.byref(wintypes.SIZE(w, h)), mem_dc, ctypes.byref(wintypes.POINT(0, 0)),
                0, ctypes.byref(blend), _ULW_ALPHA)
            return bool(ok)
        finally:
            # Every call builds a bitmap and a device context; leaving them behind would leak GDI
            # handles for as long as the program runs (the limit is 10000 per process).
            if old:
                g.SelectObject(mem_dc, old)
            if bitmap:
                g.DeleteObject(bitmap)
            if mem_dc:
                g.DeleteDC(mem_dc)
            if screen_dc:
                u.ReleaseDC(None, screen_dc)


class Front(NamedTuple):
    """What is observable about a game and the screen at one moment."""
    fg: int                      # the window in front (0: none)
    alive: bool                  # the game's window still exists
    iconic: bool                 # ... and is minimised
    mode: Optional[tuple]        # the display mode in force on the game's monitor
    desktop: Optional[tuple]     # that monitor's desktop mode


def classify_front_change(game: int, base: Front, now: Front) -> Optional[str]:
    """What happened to a game a notification picture was shown over, judged from the FIRST poll
    that differs from *base* (the state just before the picture appeared):

    - ``None``      nothing that matters has changed.
    - ``"kick"``    the game is STILL the window in front, and its monitor went from the game's
                    own display mode back to the desktop mode: the exclusive mode was taken
                    away from it and nothing else took the front first. That is what showing an
                    ordinary window over it did, every time it was measured. The only case that
                    is held against the picture on its own.
    - ``"left"``    another window took the front first - the Alt+Tab switcher, the Start menu,
                    another program, a popup, the game being closed. Somebody else's doing: the
                    mode returns to the desktop afterwards in that case too, so only the ORDER
                    tells it apart from a kick. Never held against the picture.
    - ``"suspect"`` the front went to no window at all (0). A kick of a game that runs at the
                    desktop's own resolution, which has no mode to lose, can look like that - and
                    so can a UAC prompt or Ctrl+Alt+Del. Counted, not decided; see the caller.

    A game still in front whose mode moved somewhere ELSE than the desktop mode (its own
    resolution setting) is not a loss either.
    """
    if not now.alive:
        return "left"
    if now.fg == game:
        if now.iconic:
            return "suspect"
        if now.mode is None or base.mode is None or now.mode == base.mode:
            return None
        if base.desktop is not None and base.mode != base.desktop and now.mode == base.desktop:
            return "kick"
        return None
    if now.fg == 0:
        return "suspect"
    return "left"


class FrontWatch:
    """Remembers which window is in front, and the display mode of its monitor, when it is created;
    check() then answers what classify_front_change would for the present moment."""

    def __init__(self):
        u, _g = _load()
        self._hwnd = u.GetForegroundWindow()
        self._base = self._snapshot()

    @property
    def hwnd(self) -> int:
        return int(self._hwnd) if self._hwnd else 0

    def _snapshot(self) -> Front:
        u, _g = _load()
        game = self._hwnd
        fg = u.GetForegroundWindow()
        modes = display_modes(game) if game else None
        return Front(fg=int(fg) if fg else 0,
                     alive=bool(game and u.IsWindow(game)),
                     iconic=bool(game and u.IsIconic(game)),
                     mode=modes[0] if modes else None,
                     desktop=modes[1] if modes else None)

    def check(self) -> Optional[str]:
        """None, "kick", "left" or "suspect" - see classify_front_change. Always None when no
        window was in front to begin with: there is nothing to judge a change against."""
        if not self._hwnd:
            return None
        return classify_front_change(self.hwnd, self._base, self._snapshot())

    def describe(self) -> str:
        """The baseline and the present moment, for the log: a complaint should be answerable
        from it."""
        now = self._snapshot()
        return (f"front {self._base.fg}->{now.fg} (game {self.hwnd}, minimised={now.iconic}, "
                f"exists={now.alive}), display {self._base.mode}->{now.mode}, desktop mode {now.desktop}")  # i18n-ignore (log text)
