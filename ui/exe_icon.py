"""
SaveSync - Executable icons.

The icon a program carries inside its .exe, for the places that stand in for a
game's cover: the overlay's detection and tracking cards, the Overview's
banners, the sidebar's "now playing" line, and a library entry that has no
cover image. A game SaveSync has only just noticed has nothing else to show,
and for any game the program's own icon is the one thing that tells two
unfamiliar names apart at a glance. A real cover always wins where there is one.

Every failure answers None, and the caller keeps its controller glyph: a
missing file, a program with no icon resource, a platform where the shell has
no per-executable icon, a drive that is not there any more.
"""
import logging
import os
import platform
import time
from collections import OrderedDict

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileIconProvider

logger = logging.getLogger(__name__)

# (normalised path, device px) -> pixmap, or None for "has no usable icon".
# Bounded, but sized for a library: every cover-less entry asks for its icon,
# and an eviction costs a shell round-trip on the GUI thread to get it back.
_CACHE: "OrderedDict[tuple[str, int], QPixmap | None]" = OrderedDict()
_CACHE_MAX = 512
_provider: "QFileIconProvider | None" = None

# normalised path -> time.monotonic() when it was found missing. A missing file
# is not remembered for good (a drive that is not mounted yet may be next
# time), but it is remembered briefly: a library asks again on every hover and
# refresh, and on an unreachable network drive each ask is a stall.
_MISSING: "dict[str, float]" = {}
_MISSING_TTL = 30.0


def _has_icon_resource(path: str) -> bool:
    """True when the executable embeds at least one icon.

    QFileIconProvider never answers "none": for a file with no icon of its own
    the shell hands back the generic application icon, which would be shown as
    though it were the game's. Counting the icons in the file is what tells the
    two apart.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.shell32.ExtractIconExW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.UINT]
        fn.restype = wintypes.UINT
        # Index -1 with no output buffers: "how many icons are in this file".
        return int(fn(path, -1, None, None, 0)) > 0
    except Exception:
        logger.debug("Could not count the icons in %s", path, exc_info=True)
        return False


def exe_icon_pixmap(exe_path: str, size: int, dpr: float = 1.0) -> "QPixmap | None":
    """The icon of *exe_path* as a *size*-px (logical) square pixmap, or None.

    Must be called on the GUI thread. *dpr* is the target screen's device pixel
    ratio: the pixmap is made that much larger and tagged with it, so the label
    that shows it stays *size* wide and the icon stays sharp.
    """
    if not exe_path or size <= 0:
        return None
    dpr = max(1.0, float(dpr or 1.0))
    device_px = max(1, int(round(size * dpr)))
    norm = os.path.normcase(exe_path)
    key = (norm, device_px)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    gone_at = _MISSING.get(norm)
    if gone_at is not None:
        if time.monotonic() - gone_at < _MISSING_TTL:
            return None
        del _MISSING[norm]

    pixmap = None
    try:
        if not os.path.isfile(exe_path):
            # Only briefly remembered: a drive that is not mounted yet may be
            # there next time.
            if len(_MISSING) >= _CACHE_MAX:
                _MISSING.clear()
            _MISSING[norm] = time.monotonic()
            return None
        if _has_icon_resource(exe_path):
            global _provider
            if _provider is None:
                _provider = QFileIconProvider()
            icon = _provider.icon(QFileInfo(exe_path))
            if not icon.isNull():
                raw = icon.pixmap(QSize(device_px, device_px))
                if not raw.isNull():
                    pixmap = raw.scaled(device_px, device_px,
                                        Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
                    pixmap.setDevicePixelRatio(dpr)
    except Exception:
        logger.debug("Could not load the icon of %s", exe_path, exc_info=True)
        pixmap = None

    _CACHE[key] = pixmap
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return pixmap


def show_exe_icon(label, exe_path: str, size: int, fallback_text: str,
                  dpr: float = 1.0) -> bool:
    """Put the executable's icon on *label*, or *fallback_text* when it has none.

    QLabel keeps one kind of content at a time, so each call replaces whatever
    the label showed before (text or pixmap) — a label reused for the next
    queue entry never carries the previous program's icon. Returns True when an
    icon was shown.
    """
    pixmap = exe_icon_pixmap(exe_path, size, dpr)
    if pixmap is not None:
        label.setPixmap(pixmap)
        return True
    label.setText(fallback_text)
    return False
