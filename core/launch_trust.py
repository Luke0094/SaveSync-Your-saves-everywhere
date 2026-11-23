"""
SaveSync - Asking before Play starts something only the card vouches for.

A card's launch target - its executable, or its launcher link - is data, and
it can come from a library imported from a file (whose key is the same in every
install), from another PC, from a friend. Play hands it to the system as it is.
For a plain program, or a link of a launcher SaveSync knows, that is what
anyone expects. For a script, a shortcut (which can hold any command), a
program on a network share, or a link of a kind no game launcher SaveSync
knows uses, the player is asked first - once per card, and again only if the
target changes.

Nothing is refused. A genuine launch script, a game started through Proton or
Wine, a launcher SaveSync has never heard of: all still start, after one
answer. What the player sets themselves in Add/Edit on this PC is theirs and is
never asked about. The answers stay on this PC: they are a machine-specific
setting, so an imported file cannot carry approvals with it.
"""
import re

from core.zip_guard import NEVER_RESTORED_EXTENSIONS, suffix_of

# Saved per PC (see config_transfer._MACHINE_SPECIFIC_KEYS):
# {game_id: [approved target, casefolded]}.
APPROVED_KEY = "launch_approved_targets"

# Launchers whose links Play starts without asking. Lutris links have no "//"
# ("lutris:rungameid/12"), so the scheme is read up to the first colon.
KNOWN_LAUNCHER_SCHEMES = frozenset({
    "steam", "com.epicgames.launcher", "epic", "gog", "goggalaxy", "ubisoft",
    "uplay", "battlenet", "battle.net", "origin", "origin2", "ea", "eadm",
    "heroic", "itch", "amazon-games", "bethesdanet", "rockstar", "playnite",
    "lutris", "legendary", "savesync",
})
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]+):")

# A plain program: what a game's executable is, on any system.
_PROGRAM_SUFFIXES = frozenset({"", ".exe", ".app", ".appimage", ".x86_64", ".x86"})
_SCRIPT_SUFFIXES = frozenset({
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".py", ".pyw", ".jar", ".sh", ".command",
})
_SHORTCUT_SUFFIXES = frozenset({
    ".lnk", ".url", ".website", ".desktop", ".appref-ms", ".scf",
})


def looks_like_link(target: str) -> bool:
    """A launcher link rather than a file: "scheme:..." with a scheme longer
    than a drive letter."""
    return bool(_SCHEME_RE.match((target or "").strip()))


def launch_concern(target: str) -> str:
    """Why Play should ask before starting *target*, or "" when it need not.

    "link"     a link of a kind no game launcher SaveSync knows uses
    "network"  a program on another computer (a share)
    "script"   a script: it runs whatever commands it holds
    "shortcut" a shortcut: it can start any program with any command
    "program"  a file that runs code but is not a plain program
    """
    t = (target or "").strip()
    if not t:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in t):
        return "link" if looks_like_link(t) else "program"
    if looks_like_link(t):
        scheme = _SCHEME_RE.match(t).group(1).lower()
        if scheme in KNOWN_LAUNCHER_SCHEMES or scheme.startswith("ms-xbl-"):
            return ""       # (ms-xbl-<title>: an Xbox / Game Pass game's own link)
        return "link"
    if t.startswith(("\\\\", "//")):
        return "network"
    suffix = suffix_of(t)
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _SHORTCUT_SUFFIXES:
        return "shortcut"
    if suffix in _PROGRAM_SUFFIXES:
        return ""
    if suffix in NEVER_RESTORED_EXTENSIONS:
        return "program"
    return ""               # a file opened by its own program (a page, a ROM)


def _norm(target: str) -> str:
    return (target or "").strip().casefold()


def _store() -> dict:
    from core.config_manager import get_config
    raw = get_config().get(APPROVED_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def is_approved(game_id: str, target: str) -> bool:
    """True when the player already said yes to *target* for this card here."""
    if not game_id or not target:
        return False
    return _norm(target) in {_norm(x) for x in (_store().get(game_id) or [])
                             if isinstance(x, str)}


def needs_asking(game_id: str, target: str) -> str:
    """The concern (see launch_concern) when Play must ask first, else ""."""
    concern = launch_concern(target)
    if not concern or is_approved(game_id, target):
        return ""
    return concern


def approve(game_id: str, target: str) -> None:
    """Remember a yes for *target* on this card - only for a target that is
    asked about at all, and replacing what this card had: a changed target is
    asked about again."""
    if not game_id or not launch_concern(target):
        return
    from core.config_manager import get_config
    store = dict(_store())
    kept = [x for x in (store.get(game_id) or []) if isinstance(x, str)]
    if _norm(target) in {_norm(x) for x in kept}:
        return
    # A card has two targets at most (its link and its executable): keep the
    # newest few, not a history.
    store[game_id] = (kept + [target.strip()])[-4:]
    get_config().set(APPROVED_KEY, store)


def shown(target: str) -> str:
    """*target* as the question shows it: control characters spelled out, so
    nothing in it is hidden from the player."""
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else f"\\x{ord(c):02x}"
                   for c in (target or "").strip())
