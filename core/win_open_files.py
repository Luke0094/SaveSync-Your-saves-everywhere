"""Windows-only fast replacement for ``psutil.Process.open_files()``.

Why this exists
---------------
psutil's ``open_files()`` on Windows is expensive in three ways that all
land at once, every call:

* it enumerates **every handle on the whole system** via
  ``NtQuerySystemInformation(SystemExtendedHandleInformation)`` just to
  find the target process's handles — 100k+ entries on a normal desktop,
  and it takes a kernel handle-table lock while doing it;
* it **holds the GIL for the entire call** (measured 85-370 ms), so a
  background thread calling it freezes SaveSync's GUI thread for that
  whole time;
* it resolves handle names one at a time, each on its own short-lived
  thread with a timeout.

The live-tracking loop calls this once a minute for the length of a play
session. On the measured repro a single-process RPG Maker game with 33
open files cost 450-550 ms per poll — a periodic hitch.

What this does instead
----------------------
* ``NtQueryInformationProcess(ProcessHandleInformation)`` — the target
  process's handle table **only** (Win8+), ~0.5 ms, no system-wide walk;
* every ctypes call releases the GIL, so the cost is paid off-thread for
  real;
* names are resolved concurrently, each on a daemon thread that owns and
  closes its own duplicated handle, with **one** overall deadline
  (``_DEADLINE_S``) rather than a per-handle timeout.

Measured against psutil across 8 processes (incl. a pathological OneDrive
service) and a synthetic "files being written right now" case: identical
path sets, 2-10 ms typical vs psutil's 85-370 ms.

This is best-effort and Windows-only. ``AVAILABLE`` is False elsewhere or
if the ctypes surface can't be set up; callers must keep the psutil path
as a fallback and compare the two for a few polls before trusting this
(see ``core.save_detector._open_files_safe``).
"""
from __future__ import annotations

import logging
import os
import platform
import stat as _stat
import sys
import threading
import queue as _queue
import time
from collections import namedtuple
from typing import Optional

logger = logging.getLogger(__name__)

popenfile = namedtuple("popenfile", ["path", "fd"])

AVAILABLE = False
_DEADLINE_S = 0.5          # whole-call budget for name resolution
_FILE_TYPE_DISK = 0x0001   # GetFileType()

if platform.system() == "Windows":
    try:
        import ctypes
        from ctypes import wintypes as _wt

        _ntdll = ctypes.WinDLL("ntdll")
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _NTSTATUS = ctypes.c_long
        _HANDLE = _wt.HANDLE
        _ULONG = _wt.ULONG
        _PVOID = ctypes.c_void_p

        _k32.GetCurrentProcess.restype = _HANDLE
        _k32.GetCurrentProcess.argtypes = []
        _k32.OpenProcess.restype = _HANDLE
        _k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
        _k32.CloseHandle.restype = _wt.BOOL
        _k32.CloseHandle.argtypes = [_HANDLE]
        _k32.DuplicateHandle.restype = _wt.BOOL
        _k32.DuplicateHandle.argtypes = [
            _HANDLE, _HANDLE, _HANDLE, ctypes.POINTER(_HANDLE),
            _wt.DWORD, _wt.BOOL, _wt.DWORD,
        ]
        _k32.GetFileType.restype = _wt.DWORD
        _k32.GetFileType.argtypes = [_HANDLE]
        _k32.QueryDosDeviceW.restype = _wt.DWORD
        _k32.QueryDosDeviceW.argtypes = [_wt.LPCWSTR, _wt.LPWSTR, _wt.DWORD]

        _ntdll.NtQueryInformationProcess.restype = _NTSTATUS
        _ntdll.NtQueryInformationProcess.argtypes = [
            _HANDLE, ctypes.c_int, _PVOID, _ULONG, ctypes.POINTER(_ULONG),
        ]
        _ntdll.NtQueryObject.restype = _NTSTATUS
        _ntdll.NtQueryObject.argtypes = [
            _HANDLE, ctypes.c_int, _PVOID, _ULONG, ctypes.POINTER(_ULONG),
        ]

        _STATUS_SUCCESS = 0
        _STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
        _ProcessHandleInformation = 51
        _ObjectNameInformation = 1
        _PROCESS_QUERY_INFORMATION = 0x0400
        _PROCESS_DUP_HANDLE = 0x0040
        _DUPLICATE_SAME_ACCESS = 0x0002

        class _PROCESS_HANDLE_TABLE_ENTRY_INFO(ctypes.Structure):
            _fields_ = [
                ("HandleValue", _PVOID),
                ("HandleCount", ctypes.c_size_t),
                ("PointerCount", ctypes.c_size_t),
                ("GrantedAccess", _ULONG),
                ("ObjectTypeIndex", _ULONG),
                ("HandleAttributes", _ULONG),
                ("Reserved", _ULONG),
            ]

        class _UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", _PVOID),
            ]

        AVAILABLE = True
    except Exception as _e:  # pragma: no cover - defensive
        logger.debug("win_open_files unavailable: %r", _e)
        AVAILABLE = False


# ── internals ────────────────────────────────────────────────────────────

_devmap: dict[str, str] = {}
_devmap_lock = threading.Lock()
_file_type_index: Optional[int] = None
_file_type_index_tried = False


def _build_devmap() -> None:
    with _devmap_lock:
        if _devmap:
            return
        buf = ctypes.create_unicode_buffer(2048)
        for i in range(26):
            drive = f"{chr(65 + i)}:"
            n = _k32.QueryDosDeviceW(drive, buf, 2048)
            if not n:
                continue
            # QueryDosDeviceW writes a NUL-separated, double-NUL-terminated
            # list; the first entry is the live mapping. buf[:n] keeps them
            # all rather than stopping at the first NUL like buf.value.
            for target in buf[:n].split("\x00"):
                if target and target.lower() not in _devmap:
                    _devmap[target.lower()] = drive


def _nt_to_dos(path: str) -> str:
    """``\\Device\\HarddiskVolume3\\x`` → ``D:\\x`` (best effort)."""
    if not path or not path.startswith("\\Device\\"):
        return path
    if not _devmap:
        _build_devmap()
    low = path.lower()
    for dev, drive in _devmap.items():
        if low.startswith(dev + "\\"):
            return drive + path[len(dev):]
    return path


def _isfile_strict(path: str) -> bool:
    """True only for a confirmed regular file — same as what psutil's
    open_files() actually returns.

    No exemption for a failed stat: ``os.stat`` on Windows succeeds even
    for an exclusively-locked file (it never opens the data stream), so a
    save file being written right now still stats fine; the cases where it
    *does* raise (ACCESS_DENIED, gone) are ambiguous enough — a
    non-traversable directory raises ACCESS_DENIED too — that treating
    them as files would risk feeding a directory into the save scan.
    """
    try:
        return _stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _discover_file_type_index() -> Optional[int]:
    """The kernel's ObjectTypeIndex for ``File`` objects. Stable for the
    life of a boot and identical across processes, so probe it once from
    our own handle table via a handle we know names a file.

    Purely an optimisation — with it, non-File handles are skipped before
    the DuplicateHandle/GetFileType work; without it (returns None) the
    call still produces correct results, just slower. So any failure here
    is swallowed.
    """
    global _file_type_index, _file_type_index_tried
    if _file_type_index_tried:
        return _file_type_index
    _file_type_index_tried = True
    # sys.executable is always a real, readable file on disk — unlike
    # __file__, which under PyInstaller can point inside the frozen bundle.
    probe_path = sys.executable or __file__
    try:
        import msvcrt
        f = open(probe_path, "rb")
        try:
            osfh = msvcrt.get_osfhandle(f.fileno()) & (2 ** 64 - 1)
            me = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, os.getpid())
            if not me:
                return None
            try:
                for e in _query_process_handles(me):
                    if e.HandleValue == osfh:
                        _file_type_index = int(e.ObjectTypeIndex)
                        logger.debug("win_open_files: File ObjectTypeIndex=%d",
                                     _file_type_index)
                        return _file_type_index
            finally:
                _k32.CloseHandle(me)
        finally:
            f.close()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("file type index probe failed: %r", e)
    return None


def _query_process_handles(hproc) -> list:
    """``NtQueryInformationProcess(ProcessHandleInformation)`` → list of
    ``_PROCESS_HANDLE_TABLE_ENTRY_INFO`` for that process only."""
    length = _ULONG(0x4000)
    for _ in range(12):
        buf = ctypes.create_string_buffer(length.value)
        status = _ntdll.NtQueryInformationProcess(
            hproc, _ProcessHandleInformation, buf, length.value,
            ctypes.byref(length),
        ) & 0xFFFFFFFF
        if status == _STATUS_SUCCESS:
            break
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            length = _ULONG(int(length.value * 1.5) + 0x2000)
            continue
        raise OSError(f"NtQueryInformationProcess: {hex(status)}")
    else:
        raise OSError("NtQueryInformationProcess: buffer never large enough")
    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0]
    header = ctypes.sizeof(ctypes.c_size_t) * 2   # NumberOfHandles, Reserved
    arr_t = _PROCESS_HANDLE_TABLE_ENTRY_INFO * count
    span = ctypes.sizeof(arr_t)
    return list(arr_t.from_buffer_copy(buf.raw[header:header + span]))


def _handle_name(dup) -> Optional[str]:
    """``NtQueryObject(ObjectNameInformation)`` on a duplicated handle.
    Can block for a long time on some handle types — always call on a
    thread that can be abandoned."""
    try:
        buf = ctypes.create_string_buffer(0x2000)
        rl = _ULONG()
        status = _ntdll.NtQueryObject(
            dup, _ObjectNameInformation, buf, 0x2000, ctypes.byref(rl),
        ) & 0xFFFFFFFF
        if status != _STATUS_SUCCESS:
            return None
        us = _UNICODE_STRING.from_buffer_copy(
            buf.raw[: ctypes.sizeof(_UNICODE_STRING)])
        if not us.Length or not us.Buffer:
            return None
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return None


def open_files(pid: int, deadline_s: float = _DEADLINE_S,
               stats: Optional[dict] = None) -> list:
    """Regular on-disk files the process has open, as ``popenfile(path, -1)``.

    Mirrors ``psutil.Process(pid).open_files()`` semantics (directories and
    pipes excluded; sharing-violation files kept). Raises ``OSError`` if
    the process can't be opened — callers fall back to psutil.

    ``stats`` (optional dict) is filled with diagnostic counters.
    """
    if not AVAILABLE:
        raise OSError("win_open_files not available")
    if pid in (0, 4):
        return []

    hproc = _k32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        raise ctypes.WinError(ctypes.get_last_error())

    fti = _discover_file_type_index()
    cur = _k32.GetCurrentProcess()
    seen = typed = dup_ok = disk = named = kept = 0
    dups: list = []
    try:
        try:
            for e in _query_process_handles(hproc):
                seen += 1
                if fti is not None and e.ObjectTypeIndex != fti:
                    continue
                typed += 1
                dup = _HANDLE()
                if not _k32.DuplicateHandle(
                    hproc, _HANDLE(e.HandleValue), cur, ctypes.byref(dup),
                    0, False, _DUPLICATE_SAME_ACCESS,
                ):
                    continue
                dup_ok += 1
                if _k32.GetFileType(dup) != _FILE_TYPE_DISK:
                    _k32.CloseHandle(dup)
                    continue
                disk += 1
                dups.append(dup)
        except Exception:
            for h in dups:            # don't leak on a mid-enumeration error
                _k32.CloseHandle(h)
            raise
    finally:
        _k32.CloseHandle(hproc)

    # Resolve names concurrently. Each worker owns its handle and closes
    # it — a worker still stuck in NtQueryObject past the deadline is
    # abandoned (daemon) and cleans up if/when the call ever returns.
    results: "_queue.Queue[Optional[str]]" = _queue.Queue()

    def _worker(h):
        try:
            results.put(_handle_name(h))
        finally:
            _k32.CloseHandle(h)

    started = 0
    for h in dups:
        try:
            threading.Thread(target=_worker, args=(h,), daemon=True,
                             name="win-open-files").start()
            started += 1
        except RuntimeError:          # can't spawn more threads — close the rest
            for leftover in dups[started:]:
                _k32.CloseHandle(leftover)
            break

    raw_names: list[str] = []
    got = 0
    timed_out = 0
    deadline = time.monotonic() + max(0.05, deadline_s)
    for _ in range(started):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = started - got
            break
        try:
            nm = results.get(timeout=remaining)
        except _queue.Empty:
            timed_out = started - got
            break
        got += 1
        if nm:
            raw_names.append(nm)

    paths: set[str] = set()
    for nm in raw_names:
        named += 1
        p = _nt_to_dos(nm)
        if _isfile_strict(p):
            paths.add(p)
    kept = len(paths)

    if stats is not None:
        stats.update(seen=seen, typed=typed, dup_ok=dup_ok, disk=disk,
                     named=named, kept=kept, timed_out=timed_out,
                     type_index=fti)
    if timed_out:
        logger.debug("win_open_files pid=%s: %d handle(s) unresolved within "
                     "%.0fms", pid, timed_out, deadline_s * 1000)

    return [popenfile(path=p, fd=-1) for p in sorted(paths)]
