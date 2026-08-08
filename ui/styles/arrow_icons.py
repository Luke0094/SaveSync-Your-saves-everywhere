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
