"""Tiny SVG chevrons for Qt stylesheets.

Border-triangle CSS arrows often fail to paint on Windows Fusion + QSS.
Real ``image: url(...)`` icons do not. Written under the user data dir so a
frozen build can refresh them even when the install folder is read-only.
"""
from pathlib import Path

# viewBox 10×10; chevrons fill most of it so they stay readable at 8–12 px.
_SVGS = {
    "up":    '<path d="M5 2.2 L9.2 7.8 H0.8 Z"/>',
    "down":  '<path d="M5 7.8 L0.8 2.2 H9.2 Z"/>',
    "left":  '<path d="M2.2 5 L7.8 0.8 V9.2 Z"/>',
    "right": '<path d="M7.8 5 L2.2 9.2 V0.8 Z"/>',
}


def _svg(body: str, fill: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        f'viewBox="0 0 10 10"><g fill="{fill}">{body}</g></svg>'
    )


def ensure_arrow_icons(theme: str) -> dict[str, str]:
    """Return QSS-ready ``url("…")`` values for up/down/left/right chevrons."""
    from core.constants import USER_DATA_DIR

    fill = "#c0c0cc" if theme == "dark" else "#4a4a5a"
    out_dir = Path(USER_DATA_DIR) / "ui_icons" / theme
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = {}
    for name, body in _SVGS.items():
        path = out_dir / f"{name}.svg"
        text = _svg(body, fill)
        try:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        except OSError:
            # Still point at the path; Qt will simply omit the image.
            pass
        # Forward slashes + quotes: Windows paths with spaces break unquoted url().
        urls[name] = f'url("{path.resolve().as_posix()}")'
    return urls


def chevron_button_style(
    direction: str,
    *,
    theme: str | None = None,
    size_px: int = 10,
    extra: str = "",
) -> str:
    """QSS for a text-less QPushButton showing an SVG chevron.

    *direction* is one of ``left`` / ``right`` / ``up`` / ``down``. Unicode
    glyphs (◀ ▶) vanish on some Windows fonts/DPI scales — these icons are
    the same assets the global theme injects into scrollbars.
    """
    if theme is None:
        try:
            from ui.styles.theme import get_theme_manager
            theme = get_theme_manager().current
        except Exception:
            theme = "dark"
    urls = ensure_arrow_icons(theme)
    icon = urls.get(direction) or urls.get("right")
    from ui.styles.theme import palette
    return (
        f"QPushButton{{background:{palette('bg_elevated')};color:{palette('text')};"
        f"border:1px solid {palette('border')};border-radius:4px;"
        f"padding:0;image:{icon};image-position:center;{extra}}}"
        f"QPushButton:hover{{background:{palette('accent')};"
        f"border-color:{palette('accent')};}}"
        f"QPushButton:disabled{{background:{palette('bg')};"
        f"border-color:{palette('border')};}}"
    )
