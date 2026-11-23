"""
SaveSync - Reading a backup archive without letting it unpack without bound,
or write what no save is.

A backup is not necessarily one this machine made: it can come down from the
cloud, arrive from a friend, or be an archive added by hand. A ZIP declares how
big each member unpacks to, and a crafted one - a "zip bomb" - packs gigabytes,
or petabytes, into a few kilobytes. Read whole into memory, the way a restore
read every member, it takes the program down, and the machine with it.

What SaveSync itself writes is bounded: create_backup refuses a backup over
``max_backup_size_mb``, and Settings caps that at 4096 MB. An archive that
unpacks to more is not a backup this program made, whatever the setting is
today - and the limit does not move with it, so lowering the setting never
locks out a backup made before.

check_archive() is asked before anything is unpacked; read_member() and
hash_member() then read in pieces and stop at what the member declared (the
zipfile module already does, failing the CRC after; this does not rely on it).

What is inside matters as much as how big it is: member_refusal() names the
members a restore must never write (see NEVER_RESTORED_EXTENSIONS).
"""
import hashlib
import os
import zipfile

# Files a restore never writes, whatever archive they come in, and a backup
# never takes: programs, and what Windows or a game engine runs as code. A save
# is data. One of these in a restore is how a crafted archive would run code of
# its own: a DLL the game loads from its own folder (winmm.dll, version.dll), a
# plugin RPG Maker runs (.js), a script Ren'Py compiles at launch (.rpy), a
# shortcut the player opens.
NEVER_RESTORED_EXTENSIONS = frozenset({
    # programs and libraries
    ".exe", ".dll", ".sys", ".drv", ".ocx", ".cpl", ".scr", ".com", ".pif",
    ".so", ".dylib", ".asi", ".xll", ".ax",
    # installers
    ".msi", ".msp", ".mst", ".appx", ".appxbundle", ".msix", ".msixbundle",
    ".appinstaller",
    # scripts
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse",
    ".mjs", ".wsf", ".wsh", ".wsc", ".hta", ".sct", ".jar", ".class", ".sh",
    ".command", ".py", ".pyw", ".pyc", ".pyo", ".pyd",
    # engine code, and the archives that carry it
    ".rpy", ".rpyc", ".rpym", ".rpymc", ".rpyb", ".rpa", ".pck", ".pak",
    ".asar", ".love",
    # shortcuts and shell files that open something else
    ".lnk", ".url", ".website", ".scf", ".library-ms", ".searchconnector-ms",
    ".search-ms", ".settingcontent-ms", ".appref-ms", ".application",
    ".desktop", ".reg", ".inf", ".chm", ".msc",
    # documents that carry macros
    ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".potm", ".ppam",
})

# Names Windows maps to devices, whatever extension follows ("nul.sav").
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$"}
                          | {f"com{i}" for i in range(10)}
                          | {f"lpt{i}" for i in range(10)})


def suffix_of(name: str) -> str:
    """The extension Windows sees in *name*, lowercased. Windows drops trailing
    dots and spaces when it writes a name, so "winmm.dll." and "winmm.dll "
    are winmm.dll on disk - and a leading dot is an extension too (".lnk")."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].rstrip(" .")
    return "." + base.rsplit(".", 1)[1].lower() if "." in base else ""


def is_never_restored(name: str) -> bool:
    """True for a file name of a type no backup takes and no restore writes."""
    return suffix_of(name) in NEVER_RESTORED_EXTENSIONS


def member_refusal(arc_name: str) -> str:
    """Why a restore must not write the archive member *arc_name*; "" when it may.
    Said for the log. Where the member would land is the caller's to check."""
    raw = (arc_name or "").replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return "no name"  # i18n-ignore (log text)
    if raw.startswith("/") or ".." in parts:
        return "a path out of the folder"  # i18n-ignore (log text)
    for p in parts:
        if ":" in p:
            # A drive ("C:") or an NTFS stream ("save.sav:evil.exe" - the
            # stream, or with "::$DATA" the file itself, written under a name
            # that hides it).
            return "a drive or a hidden stream in its name"  # i18n-ignore (log text)
        if any(ord(c) < 32 for c in p):
            return "control characters in its name"  # i18n-ignore (log text)
        base = p.rstrip(" .")
        if not base:
            return "a name Windows cannot keep"  # i18n-ignore (log text)
        if base.split(".", 1)[0].strip().lower() in _DEVICE_NAMES:
            return "a device name"  # i18n-ignore (log text)
    suffix = suffix_of(parts[-1])
    if suffix in NEVER_RESTORED_EXTENSIONS:
        return f"a program file ({suffix}), not a save"  # i18n-ignore (log text)
    if [p.lower() for p in parts[-2:]] == ["tokens", "security_keys.txt"]:
        # Ren'Py's list of the save signers it trusts: a key added there makes
        # a save made by anyone load without Ren'Py's own warning about it.
        return "Ren'Py's list of trusted save signers"  # i18n-ignore (log text)
    return ""


def forbidden_destination(dest) -> bool:
    """True when *dest* is somewhere no save belongs and a written file does
    something on its own: Windows' folder, and the Start Menu (its Startup
    folder opens whatever is in it at every sign-in). Archives can say where
    their files go (a folder handed over by hand carries its chain), so this is
    asked of the place itself, after it is worked out."""
    roots = []
    for var in ("SystemRoot", "WINDIR"):
        if os.environ.get(var):
            roots.append(os.environ[var])
    for var in ("APPDATA", "PROGRAMDATA"):
        if os.environ.get(var):
            roots.append(os.path.join(os.environ[var], "Microsoft", "Windows", "Start Menu"))
    try:
        d = os.path.normcase(os.path.abspath(str(dest)))
    except (OSError, ValueError, TypeError):
        return True
    for r in roots:
        r = os.path.normcase(os.path.abspath(r))
        if d == r or d.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False

# The largest backup Settings allows (4096 MB), with room for the registry
# exports a backup carries beside the files.
MAX_TOTAL_BYTES = (4096 + 64) * 1024 * 1024
# Save folders hold hundreds of files, rarely thousands. Millions of tiny
# members are a bomb of another kind: one file created on disk per entry.
MAX_MEMBERS = 100_000
# How much bigger than its packed size an archive may unpack. Deflate packs at
# most about 1032:1 and real saves a few times to a few dozen; past 64 MB, over
# 200 times the packed size is not saves (it is what a bomb under the size cap
# looks like). Small archives are exempt: a few zero-filled slots can do it.
RATIO_FLOOR_BYTES = 64 * 1024 * 1024
MAX_RATIO = 200

_CHUNK = 1024 * 1024


class UnsafeArchive(zipfile.BadZipFile):
    """An archive refused before anything was unpacked from it (see the module notes)."""


def check_archive(zf: zipfile.ZipFile) -> None:
    """Raise UnsafeArchive when *zf* would unpack to more than a backup can hold,
    has more entries than one could, or expands far past what saves compress to.
    Reads only the archive's directory - nothing is decompressed."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise UnsafeArchive(f"{len(infos)} entries, more than any backup holds ({MAX_MEMBERS})")  # i18n-ignore (log text)
    total = 0
    packed = 0
    for info in infos:
        if info.is_dir():
            continue
        if info.file_size < 0 or info.compress_size < 0:
            raise UnsafeArchive(f"{info.filename}: impossible sizes")  # i18n-ignore (log text)
        total += info.file_size
        packed += info.compress_size
        if total > MAX_TOTAL_BYTES:
            raise UnsafeArchive(
                f"unpacks to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB, "  # i18n-ignore (log text)
                f"more than any backup holds")  # i18n-ignore (log text)
    if total > RATIO_FLOOR_BYTES and total > MAX_RATIO * max(packed, 1):
        raise UnsafeArchive(
            f"unpacks to {total // (1024 * 1024)} MB from {max(packed, 1) // 1024} KB packed, "  # i18n-ignore (log text)
            f"over {MAX_RATIO} times: not saves")  # i18n-ignore (log text)


def _bounded_chunks(zf: zipfile.ZipFile, name):
    info = name if isinstance(name, zipfile.ZipInfo) else zf.getinfo(name)
    limit = min(info.file_size, MAX_TOTAL_BYTES)
    got = 0
    with zf.open(info) as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                return
            got += len(chunk)
            if got > limit:
                raise UnsafeArchive(f"{info.filename}: unpacks past its declared size")  # i18n-ignore (log text)
            yield chunk


def read_member(zf: zipfile.ZipFile, name) -> bytes:
    """One member's bytes, read in pieces and refused past its declared size.
    Use after check_archive(), in place of ``zf.read(name)``."""
    out = bytearray()
    for chunk in _bounded_chunks(zf, name):
        out += chunk
    return bytes(out)


def write_member(zf: zipfile.ZipFile, name, dest) -> int:
    """Stream one member into the file *dest* (a path), in bounded pieces - for a
    caller that only writes it out. Returns the bytes written. A refused member
    leaves a partial file behind: write to a temporary name and move it after."""
    written = 0
    with open(dest, "wb") as out:
        for chunk in _bounded_chunks(zf, name):
            out.write(chunk)
            written += len(chunk)
    return written


def hash_member(zf: zipfile.ZipFile, name) -> str:
    """SHA-256 of one member, streamed - never the whole member in memory."""
    h = hashlib.sha256()
    for chunk in _bounded_chunks(zf, name):
        h.update(chunk)
    return h.hexdigest()
