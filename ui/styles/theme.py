"""
SaveSync - Theme System
Dark/Light themes inspired by NVIDIA App aesthetics.

QSS + palettes live in per-theme modules (``dark.py``, ``light.py``).
``theme.py`` is the registry and runtime API — callers keep importing
``palette``, ``get_theme_manager``, ``ThemedMixin``, ``DARK_THEME`` /
``LIGHT_THEME`` from here.
"""
import logging
import threading
from types import ModuleType

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from ui.styles import dark as _dark
from ui.styles import light as _light

logger = logging.getLogger(__name__)

# Registry: add a module with THEME / PALETTE / ID / IS_DARK, then entry here.
THEMES: dict[str, ModuleType] = {
    _dark.ID: _dark,
    _light.ID: _light,
}

# Back-compat aliases (tests / docs / rare direct QSS access).
DARK_THEME = _dark.THEME
LIGHT_THEME = _light.THEME
_PALETTE_DARK = _dark.PALETTE
_PALETTE_LIGHT = _light.PALETTE


def get_theme_module(theme: str | None = None) -> ModuleType:
    """Return the theme module for *theme* (or the active one if omitted)."""
    if theme is None:
        theme = get_theme_manager().current
    mod = THEMES.get(theme)
    if mod is None:
        logger.warning("Unknown theme %r, falling back to dark", theme)
        return THEMES["dark"]
    return mod


def palette(key: str) -> str:
    """Return the color hex for *key* based on the active theme.

    Usage in widgets::

        from ui.styles.theme import palette
        label.setStyleSheet(f"color: {palette('text_muted')}; font-size: 11px;")

    This replaces hardcoded dark-only colors with theme-aware values.
    """
    if not isinstance(key, str) or not key:
        logger.warning(f"Invalid palette key: {key!r}, returning generic fallback")
        return "#888888"
    pal = get_theme_module().PALETTE
    value = pal.get(key)
    if value is not None:
        return value
    logger.warning(f"Unknown palette key: {key!r}, returning generic fallback")
    return "#888888"


class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self._current = "dark"

    def apply(self, theme: str, app: QApplication):
        # Swapping the application stylesheet makes Qt re-resolve style rules
        # for EVERY live widget, and that cost is linear in how many there
        # are (measured at roughly 0.4 ms each) — on a large library it is a
        # visible pause with the event loop blocked throughout. Nothing about
        # it can be deferred or batched from here (hiding the windows or
        # disabling updates around it measured slower, not faster), so at
        # least say the app is busy rather than looking hung.
        # A "please wait" sheet over the window, not just a wait cursor: the
        # pause is long enough to look like a hang, and a cursor shape is easy
        # to miss. Falls back to plain execution when there is no visible
        # window to cover (startup, headless).
        target = None
        for w in app.topLevelWidgets():
            if w.isVisible() and w.isWindow() and w.width() > 200:
                target = w
                break
        if target is None:
            self._apply_inner(theme, app)
            return
        from ui.widgets.busy_overlay import busy_over
        # Event loop is blocked for the whole stylesheet swap — show at once.
        with busy_over(target, delay_ms=0) as overlay:
            self._apply_inner(theme, app, overlay=overlay)

    def _apply_inner(self, theme: str, app: QApplication, overlay=None):
        mod = get_theme_module(theme)
        self._current = mod.ID
        qss = mod.THEME
        # SVG chevrons — CSS border triangles do not paint reliably on Windows.
        from ui.styles.arrow_icons import ensure_arrow_icons
        for key, url in ensure_arrow_icons(self._current).items():
            qss = qss.replace(f"__ICON_{key.upper()}__", url)

        # Accessibility UI scale: design-time font-size:Npx → scaled px.
        from ui.helpers import scale_stylesheet_fonts, ui_scale
        qss = scale_stylesheet_fonts(qss, ui_scale())

        # Override Qt's built-in QPalette so Fusion doesn't paint
        # its default gray on view viewports and scroll areas.
        colors = mod.PALETTE
        bg = QColor(colors["bg"])
        fg = QColor(colors["text"])
        base = QColor(colors["bg"])
        alt = QColor(colors["bg_hover"])
        pal = app.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Base, base)
        pal.setColor(QPalette.ColorRole.AlternateBase, alt)
        pal.setColor(QPalette.ColorRole.Text, fg)
        pal.setColor(QPalette.ColorRole.Button, bg)
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        app.setPalette(pal)
        if overlay is not None:
            overlay.pump()

        app.setStyleSheet(qss)
        if overlay is not None:
            overlay.pump()
        self.theme_changed.emit(self._current)
        if overlay is not None:
            overlay.pump()

    @property
    def current(self) -> str:
        return self._current

    def is_dark(self) -> bool:
        return bool(getattr(get_theme_module(self._current), "IS_DARK", True))


class ThemedMixin:
    """Mixin for widgets whose inline, palette-dependent styles must survive a
    light/dark switch WITHOUT rebuilding the widget tree (the rebuild is what
    made theme changes freeze on large libraries).

    Route every palette-dependent ``setStyleSheet`` through
    ``self._sty(widget, lambda: f"...{palette('key')}...")``: the style is
    applied immediately AND remembered, so ``refresh_styles()`` re-applies it
    with the now-current palette. Because applying and registering are the SAME
    call, a converted site can never be silently dropped from a separate list.

    Note on loops: the ``lambda`` is evaluated at refresh time, so any
    loop-local variable it references (e.g. a per-item colour) MUST be captured
    with a default argument — ``lambda c=c: f"...{c}..."`` — or every registered
    entry would see the final loop value.

    Widgets that create their own themed children (e.g. per-game cards) override
    ``refresh_styles`` to also cascade into the current children — see
    LibraryPage.refresh_styles.
    """

    def _sty(self, widget, style_fn):
        try:
            reg = self._themed_styles
        except AttributeError:
            reg = self._themed_styles = {}
        from ui.helpers import scale_stylesheet_fonts
        widget.setStyleSheet(scale_stylesheet_fonts(style_fn()))
        # Keyed by widget, not appended: a page that re-registers the same
        # widget (a row restyled on selection, a card re-themed on hover)
        # would otherwise accumulate one entry per call, and refresh_styles
        # would replay them all on every theme switch.
        reg[widget] = style_fn
        return widget

    def refresh_styles(self):
        reg = getattr(self, "_themed_styles", None)
        if not reg:
            return
        from ui.helpers import scale_stylesheet_fonts
        dead = []
        for widget, style_fn in list(reg.items()):
            try:
                widget.setStyleSheet(scale_stylesheet_fonts(style_fn()))
            except RuntimeError:
                # Underlying C++ widget already deleted. DROP it — leaving it
                # behind grew the registry for the whole session (every page
                # rebuild added its replaced widgets), so each theme switch
                # replayed an ever-longer list of entries that only raise.
                dead.append(widget)
        for widget in dead:
            reg.pop(widget, None)

    def prune_themed_styles(self):
        """Forget entries whose widget is already gone.

        refresh_styles() prunes as it goes, but a page that rebuilds a whole
        block of rows knows right then that the old ones are dead — calling
        this keeps the registry the size of what is actually on screen
        instead of leaving it to the next theme switch to discover.
        """
        reg = getattr(self, "_themed_styles", None)
        if not reg:
            return
        import shiboken6 as sip
        for widget in [w for w in reg if not sip.isValid(w)]:
            reg.pop(widget, None)


_theme_mgr: ThemeManager | None = None
_theme_lock = threading.Lock()


def get_theme_manager() -> ThemeManager:
    """Return the singleton ThemeManager.

    Must be called from the main thread (ThemeManager is a QObject and
    creating QObjects on background threads causes signal delivery issues).
    """
    global _theme_mgr
    if _theme_mgr is None:
        with _theme_lock:
            if _theme_mgr is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("ThemeManager requires QApplication")
                _theme_mgr = ThemeManager()
    return _theme_mgr
