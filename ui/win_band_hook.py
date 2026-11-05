"""
SaveSync - Qt windows that can be shown over a game in exclusive fullscreen (Windows).

Showing an ordinary window over a game in exclusive fullscreen takes the game back to the desktop
(see ui.exclusive_presenter). A LAYERED, non-activating window in the one higher z-order band an
ordinary process may use (band 16, "system tools") does not - measured, mouse included. The overlay's
notifications are drawn into such a window by hand; the application's own windows (the app itself, its
dialogs, menus, the dimmed backdrop behind them) are Qt windows, and Qt creates every native window
with CreateWindowExW - in the ordinary band.

This module changes that for as long as it is switched on. The Qt platform plugin (qwindows.dll) calls
CreateWindowExW through its import table; the one entry for that function is pointed at a small function
of ours which, while the mode is on, creates a top-level window with CreateWindowInBand in band 16, adds
WS_EX_LAYERED and WS_EX_NOACTIVATE (and TOOLWINDOW/TOPMOST), and otherwise does exactly what the original
did. A window created that way is a real Qt window: Qt draws it, and the mouse reaches it the way it reaches
any other window - no copy of its picture, no input of our own. It never takes the focus, so the keyboard
is a separate matter (see ui.exclusive_keys).

Qt asks for the foreground explicitly for some windows - a popup (a menu, the list of a combo box, a completer) calls
SetForegroundWindow on itself as it opens (measured), and that alone takes a game out of exclusive fullscreen even when
the popup is a band-16, layered, non-activating window. So the same import table gets a second entry: SetForegroundWindow
called on a window of the higher band does nothing; for any other window it is the original call.

Only this process's own import table is touched; nothing is injected into any other process, and the
table entry is put back by uninstall().

Qt itself decides how a window is painted, and that matters: a FRAMELESS window with an alpha channel
(Qt.FramelessWindowHint + WA_TranslucentBackground) is painted with UpdateLayeredWindow, the way the overlay is.
A window with a native frame is painted by GDI into a layered window, which is only visible once
SetLayeredWindowAttributes has been called - so for those this hook calls it (alpha 255) right after creating
the window.

Everything that cannot be done (not Windows, the plugin not loaded, the import not found, the band refused)
makes install() return False / creates the window the ordinary way: the caller does what it did before.
"""
import ctypes
import logging
import platform
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

BAND_SYSTEM_TOOLS = 16

_WS_CHILD = 0x40000000
_WS_CAPTION = 0x00C00000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_LWA_ALPHA = 0x2
_PAGE_READWRITE = 0x04

_state = {
    "installed": False,
    "mode": False,
    "slot": 0,            # address of the import-table entry
    "orig_ptr": 0,        # what the entry held
    "callback": None,     # kept alive
    "orig": None,         # callable for the original function
    "band_fn": None,
    "created": [],        # windows created in band 16 (the most recent ones)
    "refused": 0,
    "sfw_slot": 0,        # the import-table entry for SetForegroundWindow
    "sfw_orig_ptr": 0,
    "sfw_callback": None,
    "sfw_orig": None,
    "sfw_skipped": 0,     # activations of higher-band windows that were not carried out
    "gwb": None,
}


def supported() -> bool:
    return platform.system() == "Windows"


def _module_base(name: str) -> int:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = ctypes.c_void_p
    return int(k.GetModuleHandleW(name) or 0)


def _find_import_slot(base: int, dll: str, func: str) -> int:
    """Address of the import-table entry for *func* of *dll* in the module loaded at *base*, or 0.
    Read from the module's own headers in memory."""
    def rd(addr, n):
        return ctypes.string_at(addr, n)

    def u32(addr):
        return int.from_bytes(rd(addr, 4), "little")

    if rd(base, 2) != b"MZ":
        return 0
    nt = base + u32(base + 0x3C)
    if rd(nt, 4) != b"PE\0\0":
        return 0
    opt = nt + 24
    magic = int.from_bytes(rd(opt, 2), "little")
    if magic != 0x20B:                       # PE32+ (64-bit) only
        return 0
    imp_rva = u32(opt + 112 + 8)             # data directory 1: imports
    if not imp_rva:
        return 0
    desc = base + imp_rva
    while True:
        oft, _ts, _fwd, name_rva, ft = (u32(desc + i * 4) for i in range(5))
        if not name_rva:
            return 0
        if rd(base + name_rva, len(dll) + 1).lower() == dll.lower().encode() + b"\0":
            names = base + (oft or ft)
            idx = 0
            while True:
                entry = int.from_bytes(rd(names + idx * 8, 8), "little")
                if not entry:
                    return 0
                if not entry >> 63:                           # by name (not by ordinal)
                    hint_name = base + (entry & 0x7FFFFFFF)
                    n = ctypes.string_at(hint_name + 2).decode("ascii", "replace")
                    if n == func:
                        return base + ft + idx * 8
                idx += 1
        desc += 20


def _write_pointer(slot: int, value: int) -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k.VirtualProtect.restype = wintypes.BOOL
    old = wintypes.DWORD(0)
    if not k.VirtualProtect(slot, 8, _PAGE_READWRITE, ctypes.byref(old)):
        return False
    try:
        ctypes.c_void_p.from_address(slot).value = value
    finally:
        k.VirtualProtect(slot, 8, old.value, ctypes.byref(wintypes.DWORD(0)))
    return True


def _proto():
    """The signature of CreateWindowExW (built when needed: ctypes.WINFUNCTYPE exists only on Windows)."""
    return ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _band_of(hwnd) -> Optional[int]:
    """The band of a window, through a function looked up once (this runs on every SetForegroundWindow of Qt's)."""
    fn = _state["gwb"]
    if fn is None:
        fn = ctypes.WinDLL("user32", use_last_error=True).GetWindowBand
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL
        _state["gwb"] = fn
    band = wintypes.DWORD(0xFFFF)
    return int(band.value) if fn(hwnd, ctypes.byref(band)) else None


def _install_activation_guard(base: int) -> None:
    """Point the plugin's SetForegroundWindow import at a function that leaves windows of the higher band alone."""
    slot = _find_import_slot(base, "USER32.dll", "SetForegroundWindow")
    if not slot:
        logger.info("Band hook: SetForegroundWindow is not in the plugin's import table - popups will take the foreground")
        return
    orig_ptr = int.from_bytes(ctypes.string_at(slot, 8), "little")
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
    orig = proto(orig_ptr)

    def guarded(hwnd):
        try:
            if hwnd and _band_of(hwnd) == BAND_SYSTEM_TOOLS:
                _state["sfw_skipped"] += 1
                return 1
        except Exception:
            pass
        return orig(hwnd)

    cb = proto(guarded)
    if _write_pointer(slot, ctypes.cast(cb, ctypes.c_void_p).value):
        _state.update(sfw_slot=slot, sfw_orig_ptr=orig_ptr, sfw_callback=cb, sfw_orig=orig)
    else:
        logger.info("Band hook: the SetForegroundWindow entry could not be written")


def install() -> bool:
    """Point Qt's window creation at our function. Idempotent. False when it cannot be done - the plugin is not
    loaded (a headless platform), the import is not there, or the table cannot be written."""
    if _state["installed"]:
        return True
    if not supported():
        return False
    try:
        base = _module_base("qwindows.dll")
        if not base:
            logger.info("Band hook: the Qt Windows platform plugin is not loaded - not installed")
            return False
        slot = _find_import_slot(base, "USER32.dll", "CreateWindowExW")
        if not slot:
            logger.info("Band hook: CreateWindowExW is not in the plugin's import table - not installed")
            return False
        orig_ptr = int.from_bytes(ctypes.string_at(slot, 8), "little")
        u = ctypes.WinDLL("user32", use_last_error=True)
        band_fn = u.CreateWindowInBand
        band_fn.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        band_fn.restype = ctypes.c_void_p
        u.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
        u.SetLayeredWindowAttributes.restype = wintypes.BOOL
        proto = _proto()
        orig = proto(orig_ptr)

        def hooked(ex, cls, name, style, x, y, w, h, parent, menu, inst, param):
            if _state["mode"] and not parent and not (style & _WS_CHILD):
                try:
                    ex2 = ex | _WS_EX_LAYERED | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST
                    hwnd = band_fn(ex2, cls, name, style, x, y, w, h, parent, menu, inst, param, BAND_SYSTEM_TOOLS)
                    if hwnd:
                        if (style & _WS_CAPTION) == _WS_CAPTION:
                            # a framed window is painted by GDI, and a layered window shows nothing until this
                            u.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)
                        _state["created"].append(int(hwnd))
                        del _state["created"][:-64]
                        return hwnd
                    _state["refused"] += 1
                except Exception:
                    logger.debug("Band hook: could not create the window in the higher band", exc_info=True)
            return orig(ex, cls, name, style, x, y, w, h, parent, menu, inst, param)

        cb = proto(hooked)
        if not _write_pointer(slot, ctypes.cast(cb, ctypes.c_void_p).value):
            logger.info("Band hook: the import table could not be written - not installed")
            return False
        _state.update(installed=True, slot=slot, orig_ptr=orig_ptr, callback=cb, orig=orig, band_fn=band_fn)
        try:
            _install_activation_guard(base)
        except Exception:
            logger.info("Band hook: the activation guard is not installed", exc_info=True)
        logger.info("Band hook installed%s", "" if _state["sfw_slot"] else " (without the activation guard)")
        return True
    except Exception:
        logger.info("Band hook: not installed", exc_info=True)
        return False


def uninstall() -> None:
    """Put Qt's import entry back. Windows already created in the higher band stay what they are."""
    set_mode(False)
    if _state["installed"] and _state["slot"]:
        try:
            _write_pointer(_state["slot"], _state["orig_ptr"])
        except Exception:
            logger.debug("Band hook: could not restore the import table", exc_info=True)
    if _state["sfw_slot"]:
        try:
            _write_pointer(_state["sfw_slot"], _state["sfw_orig_ptr"])
        except Exception:
            logger.debug("Band hook: could not restore the activation guard", exc_info=True)
    _state.update(installed=False, callback=None, orig=None, sfw_slot=0, sfw_callback=None, sfw_orig=None)


def installed() -> bool:
    return bool(_state["installed"])


def set_mode(on: bool) -> bool:
    """While on, every top-level window Qt creates is created in band 16 (layered, non-activating). Returns the
    previous setting. Does nothing (and returns False) unless the hook is installed."""
    prev = bool(_state["mode"])
    _state["mode"] = bool(on) and _state["installed"]
    return prev


def mode() -> bool:
    return bool(_state["mode"])


class band_windows:
    """``with band_windows(): widget.show()`` - the windows Qt creates inside are created in band 16."""

    def __enter__(self):
        self._prev = set_mode(True)
        return self

    def __exit__(self, *exc):
        set_mode(self._prev)
        return False


def window_band(hwnd: int) -> Optional[int]:
    """The z-order band of a window (1 = ordinary, 16 = system tools), or None."""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetWindowBand.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowBand.restype = wintypes.BOOL
        band = wintypes.DWORD(0xFFFF)
        return int(band.value) if u.GetWindowBand(hwnd, ctypes.byref(band)) else None
    except Exception:
        return None


def ensure_band(widget) -> None:
    """Make sure *widget*'s native window is a band-16 window. One made earlier in the ordinary band (a z-order helper asked
    for its handle, it was shown before the hook was on) is dropped and made again; one not made yet is made now."""
    if not _state["installed"]:
        return
    try:
        if widget.windowHandle() is not None:
            if window_band(int(widget.winId())) == BAND_SYSTEM_TOOLS:
                return
            widget.hide()
            widget.destroy()
        with band_windows():
            widget.create()
    except Exception:
        logger.debug("Band hook: could not make the window in the higher band", exc_info=True)


def ensure_noactivate(hwnd: int, layered: bool = False) -> bool:
    """A window of the higher band that a click can activate takes a game out of exclusive fullscreen. Qt rewrites the
    extended style of a window whose flags change while it is being shown (a dialog adopted inside show()) and the
    non-activating bit can be lost; this puts it back. With *layered* the layered bit is set too, WITHOUT the attributes call
    ensure_layered makes: for a translucent window that Qt paints with UpdateLayeredWindow, which starts doing so at its
    first paint - until then the window would not be layered at all. True when the window had to be changed."""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        ex = u.GetWindowLongPtrW(hwnd, -20) & 0xFFFFFFFF
        want = ex | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST | (_WS_EX_LAYERED if layered else 0)
        if want == ex:
            return False
        u.SetWindowLongPtrW(hwnd, -20, want)
        return True
    except Exception:
        return False


def refused_count() -> int:
    return int(_state["refused"])


def skipped_activations() -> int:
    """How many times Qt asked for the foreground for a window of the higher band and was not obeyed."""
    return int(_state["sfw_skipped"])


def ensure_layered(hwnd: int) -> bool:
    """Qt removes the layered style from a window it paints with GDI (any framed window) once it has no use for it, and a
    window in band 16 that is not layered does take a game out of exclusive fullscreen. This puts it back (and makes the
    window visible with SetLayeredWindowAttributes, alpha 255) when it has gone. True when the window is layered now."""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
        u.SetLayeredWindowAttributes.restype = wintypes.BOOL
        ex = u.GetWindowLongPtrW(hwnd, -20) & 0xFFFFFFFF
        if ex & _WS_EX_LAYERED:
            return True
        u.SetWindowLongPtrW(hwnd, -20, ex | _WS_EX_LAYERED)
        return bool(u.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA))
    except Exception:
        return False
