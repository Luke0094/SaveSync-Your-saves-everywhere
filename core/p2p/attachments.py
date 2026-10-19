"""SaveSync - what a file a friend sends in a chat may be, and what is done
with it when it arrives.

An attachment is not a new way of moving bytes: it is the sealed transfer
SaveSync already has (core.p2p.transfer - the token, the secret info-hash, the
mandatory encryption, links instead of copies), asked for from the chat. What
this module adds is the rules that make a file from another person's PC safe to
have on yours:

- it is OFFERED first (a name, how many files, how big) and only fetched when
  the person clicks Accept; nothing arrives by itself;
- what arrives must be exactly what was offered: the same number of files and
  the same total size, or the transfer is refused;
- some kinds of file are never taken (programs, scripts, shortcuts, installers,
  disk images, office files that carry macros...): they run code the moment they
  are opened. Put them in a .zip if they really must go. The same goes for
  names Windows would treat specially (``CON``, ``a.exe.``, ``x:stream``, a
  direction override that makes ``txt.exe`` read as ``exe.txt``);
- the files are marked as coming from the Internet (the same "Zone.Identifier"
  mark a browser leaves), so Windows's own checks apply when one is opened;
- SaveSync never opens, runs or previews a received file by itself. The one
  exception is an image the person asks to see, which is checked first (type,
  size, dimensions) and decoded only on that click.
"""
import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# More files than this in one offer is refused outright.
MAX_FILES = 1000
# Length of the name shown in a card / the chat (bytes of UTF-8).
MAX_LABEL_BYTES = 80
# Free space that has to be left on top of the size of what is received.
SPACE_MARGIN = 64 * 1024 * 1024
# An offer that nobody has taken up in this long is no longer good.
OFFER_SECONDS = 24 * 3600

# Opening one of these runs code (or mounts a disk, or loads a macro).
BLOCKED_EXTENSIONS = frozenset("""
exe scr com pif bat cmd msi msp mst ps1 ps1xml psc1 psd1 psm1 vbs vbe js jse wsf wsh wsc hta
jar lnk url reg dll cpl msc gadget application appref-ms scf inf sys drv ocx appx appxbundle
msix msixbundle xll xlam docm dotm xlsm xltm pptm potm ppam sldm iso img vhd vhdx chm py pyw
pyz pyc sh bash command apk dmg pkg deb rpm lib ade adp mda mdb mde mdt mdw mdz ws
""".split())

_BIDI = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
# Characters that show nothing, or turn the text around: no use in a file name
# except to make one look like another. (The joiners emoji are built with stay.)
_HIDDEN = _BIDI | frozenset("​⁠﻿­")
_RESERVED = frozenset(["con", "prn", "aux", "nul", "conin$", "conout$"]
                      + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)])
_BAD_CHARS = set('<>:"|?*\\')


def extension(name: str) -> str:
    """The last extension of *name*, the way Windows reads it (trailing dots
    and spaces do not count), lower case, without the dot."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].rstrip(" .")
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def is_blocked_name(name: str) -> bool:
    """A file of a kind that is never sent or taken in a chat."""
    return extension(name) in BLOCKED_EXTENSIONS


def name_problem(rel: str) -> str:
    """Why a received path (relative, "sub/a.txt") is refused, or "" when it is
    fine. Looks at every part of it."""
    rel = str(rel)
    if not rel or rel.startswith(("/", "\\")) or len(rel) > 240:
        return "name"
    parts = rel.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", "..") or part != part.strip() or part.endswith("."):
            return "name"
        if any(ch in _BAD_CHARS or ch in _HIDDEN or unicodedata.category(ch) in ("Cc", "Cs")
               for ch in part):
            return "name"
        if part.split(".", 1)[0].strip().lower() in _RESERVED:
            return "name"
    if is_blocked_name(parts[-1]):
        return "type"
    return ""


def check_incoming(rels, sizes, count: int, total: int) -> str:
    """Why the files an accepted transfer turns out to hold are refused, or "".
    *rels*/*sizes* are what its file list says, *count*/*total* what the
    friend's message promised."""
    if len(rels) != len(sizes) or not rels:
        return "empty"
    if len(rels) != count or sum(int(s) for s in sizes) != total:
        return "mismatch"
    if len(rels) > MAX_FILES:
        return "too_many"
    seen = set()
    for rel in rels:
        why = name_problem(rel)
        if why:
            return why
        key = str(rel).casefold()
        if key in seen:
            return "name"
        seen.add(key)
    return ""


def check_outgoing(files) -> str:
    """Why *files* (``[(path, relative path)]``) cannot be offered, or ""."""
    if not files:
        return "empty"
    if len(files) > MAX_FILES:
        return "too_many"
    for src, rel in files:
        why = name_problem(rel)
        if why == "type":
            return "type"
        if why:
            return "name"
        if is_blocked_name(Path(src).name):
            return "type"
    return ""


def blocked_names(files) -> list:
    """The names in *files* that are of a blocked kind (for telling the person)."""
    return [str(rel) for _src, rel in files if is_blocked_name(rel)]


def clean_label(text, limit: int = MAX_LABEL_BYTES) -> str:
    """A name as it is shown in a card: no path, no control, invisible or
    direction characters, one line, at most *limit* bytes."""
    s = str(text or "").replace("\\", "/").rsplit("/", 1)[-1]
    s = "".join(ch for ch in s if ch not in _HIDDEN and unicodedata.category(ch) not in ("Cc", "Cs"))
    s = " ".join(s.split())
    raw = s.encode("utf-8", "replace")
    return s if len(raw) <= limit else raw[:limit - 3].decode("utf-8", "ignore").rstrip() + "…"


def human_size(n) -> str:
    """"950 B", "2.1 MB", "48 GB" - decimal units, like the speeds shown."""
    n = max(0, int(n or 0))
    if n < 1000:
        return f"{n} B"
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1000.0
        if v < 1000 or unit == "TB":
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{v:.0f} TB"


def free_space_ok(folder, needed: int) -> bool:
    """Is there room in *folder* (or the nearest folder above it that exists)
    for *needed* bytes and a margin?"""
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free >= int(needed) + SPACE_MARGIN
    except OSError:
        return True             # cannot tell: do not refuse on a guess


# ── the Internet mark ────────────────────────────────────────────────────

def mark_from_internet(path) -> bool:
    """Leave the "this came from the Internet" mark Windows (and browsers) use
    on *path*. Best effort: it needs NTFS and does nothing elsewhere. True when
    the mark was written."""
    if os.name != "nt":
        return False
    try:
        with open(str(path) + ":Zone.Identifier", "w", encoding="ascii", newline="") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\n")
        return True
    except OSError:
        return False


def has_internet_mark(path) -> bool:
    try:
        with open(str(path) + ":Zone.Identifier", "r", encoding="ascii") as f:
            return "ZoneId=3" in f.read()
    except OSError:
        return False


# ── images ───────────────────────────────────────────────────────────────

IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_PIXELS = 16_000_000
IMAGE_MAX_SIDE = 8192
_IMAGE_KINDS = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),
    "bmp": (b"BM",),
}


def image_problem(path) -> str:
    """Why the file at *path* is not shown as a picture, or "" when it may be:
    a plain image type, small, whose first bytes really are that type's (and,
    for WebP, whose RIFF header says WEBP). The decoding itself is the caller's,
    and has its own limits (see image_limits_ok)."""
    p = Path(path)
    ext = extension(p.name)
    if ext not in _IMAGE_KINDS:
        return "type"
    try:
        size = p.stat().st_size
        if not p.is_file() or p.is_symlink():
            return "type"
        if size == 0 or size > IMAGE_MAX_BYTES:
            return "size"
        with open(p, "rb") as f:
            head = f.read(16)
    except OSError:
        return "missing"
    if not any(head.startswith(m) for m in _IMAGE_KINDS[ext]):
        return "type"
    if ext == "webp" and head[8:12] != b"WEBP":
        return "type"
    return ""


# ── videos ───────────────────────────────────────────────────────────────

# Containers the chat will play, by extension, each with the bytes a real one
# starts with. A text playlist (.m3u8), an SDP file or anything else that makes
# a media library go and open other things is not on this list, and a file that
# merely carries one of these names without the signature is not played.
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot")
VIDEO_EXTENSIONS = frozenset("mp4 m4v mov qt 3gp 3g2 mkv webm avi wmv asf ogv flv mpg mpeg ts mts m2ts".split())


def _video_signature(ext: str, head: bytes) -> bool:
    if ext in ("mp4", "m4v", "mov", "qt", "3gp", "3g2"):
        return len(head) >= 12 and head[4:8] in _MP4_BOXES
    if ext in ("mkv", "webm"):
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext == "avi":
        return head[:4] == b"RIFF" and head[8:12] == b"AVI "
    if ext in ("wmv", "asf"):
        return head[:16] == _ASF_GUID
    if ext == "ogv":
        return head[:4] == b"OggS"
    if ext == "flv":
        return head[:3] == b"FLV" and head[3:4] == b"\x01"
    if ext in ("mpg", "mpeg"):
        return head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")
    if ext == "ts":
        return len(head) > 188 and head[0] == 0x47 and head[188] == 0x47
    if ext in ("mts", "m2ts"):
        return len(head) > 196 and head[4] == 0x47 and head[196] == 0x47
    return False


def video_name_ok(name: str) -> bool:
    """Does *name* end the way a video the chat plays does?"""
    return extension(name) in VIDEO_EXTENSIONS


def video_signature_ok(name: str, head: bytes) -> bool:
    """Do the first bytes of a file called *name* (a few hundred are enough)
    really belong to the video container its extension says?"""
    return _video_signature(extension(name), bytes(head))


def video_problem(path) -> str:
    """Why the file at *path* is not played as a video, or "" when it may be: a
    plain file (not a link) with a video extension whose first bytes really are
    that container's. What is inside it - and whether this PC can play it - is
    the player's business (ui.widgets.safe_video)."""
    p = Path(path)
    ext = extension(p.name)
    if ext not in VIDEO_EXTENSIONS:
        return "type"
    try:
        if p.is_symlink() or not p.is_file():
            return "type"
        if p.stat().st_size < 16:
            return "size"
        with open(p, "rb") as f:
            head = f.read(400)
    except OSError:
        return "missing"
    return "" if _video_signature(ext, head) else "type"


def image_limits_ok(width: int, height: int) -> bool:
    """Is a picture of this size small enough to decode? (Checked from the
    header, before a single pixel is read.)"""
    return (0 < width <= IMAGE_MAX_SIDE and 0 < height <= IMAGE_MAX_SIDE
            and width * height <= IMAGE_MAX_PIXELS)
