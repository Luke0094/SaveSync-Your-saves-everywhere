# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Linux build that goes inside the AppImage.

Separate from savesync.spec on purpose. That one is the Windows build and
is full of things Linux has no use for — a .ico, and hidden imports naming
``pynput.*._win32`` and ``keyring.backends.Windows``, none of which exist
here. Bending it into shape would have put the working Windows build one
edit away from breaking, for no gain.

The splash below is the one exception, and it is a deliberate copy rather
than a shared import: both files are build recipes that have to keep
working on their own, and the Windows one is a release path that should
not gain a dependency on this one.

The modules are collected by PACKAGE rather than listed by hand. The
Windows spec enumerates roughly a hundred of them because several are
imported lazily inside format handlers and analysis never sees them;
collecting the packages whole reaches the same modules and cannot drift
out of date when one is added.

Build (on Linux):
    pyinstaller --clean --noconfirm packaging/savesync-linux.spec

Always pass --clean when the splash has been touched: the Tcl script is
assembled at build time and a stale one is cached otherwise.
"""
import sys as _sys
from pathlib import Path

import PyInstaller.building.splash_templates as _splash_tpl
from PyInstaller.utils.hooks import collect_submodules

# packaging/ is not on the path when PyInstaller execs a spec.
_sys.path.insert(0, str(Path(SPECPATH).resolve()))   # noqa: F821
import splash_frames as _splash_frames               # noqa: E402

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 — PyInstaller global

# The splash the bootloader shows while it unpacks — the same thing the
# Windows build has, and for the same reason: an AppImage of this size
# spends a few seconds mounting and starting Python, and without it the
# user gets nothing at all after a double click. PyInstaller draws it with
# Tk, so it exists only where Tk does; a build host without python3-tk
# still produces a working AppImage, just a silent start.
try:
    import tkinter as _tk           # noqa: F401
    _HAVE_TK = True
except Exception:
    _HAVE_TK = False

datas = [
    (str(ROOT / 'i18n' / 'locales'), 'i18n/locales'),
    (str(ROOT / 'sync' / 'app_credentials.py'), 'sync'),
    (str(ROOT / 'assets' / 'splash.png'), 'assets'),
    (str(ROOT / 'assets' / 'splash_animated.gif'), 'assets'),
    (str(ROOT / 'assets' / 'icon.png'), 'assets'),
    # The .ico as well, and not as a Windows leftover: Qt reads it here too,
    # and it carries seven sizes where the PNG carries one. Measured on
    # Linux — QIcon('icon.ico').availableSizes() returns 16 through 256,
    # QIcon('icon.png') returns 256 alone, so dropping it would leave every
    # small chrome (window list, tray, task switcher) scaling one big image.
    (str(ROOT / 'assets' / 'icon.ico'), 'assets'),
]

hiddenimports = []
for package in ('core', 'ui', 'sync', 'hotkeys', 'i18n'):
    hiddenimports += collect_submodules(package)

hiddenimports += [
    # pynput and keyring pick their backend at run time, so analysis sees
    # neither. The X11 ones are the Linux halves of what the Windows spec
    # names; SecretService is what a desktop keyring answers on.
    'pynput.keyboard._xorg', 'pynput.mouse._xorg',
    'pynput.keyboard._uinput',
    'keyring.backends', 'keyring.backends.SecretService',
    'keyring.backends.chainer', 'keyring.backends.fail',
    'secretstorage', 'jeepney',
    # Same third-party leaves the Windows spec lists.
    'google.auth.transport.requests', 'google_auth_oauthlib.flow',
    'googleapiclient.discovery',
    'cryptography.hazmat.primitives.ciphers.aead',
    'PIL', 'pillow_avif',
    'jaraco.functools', 'jaraco.context', 'jaraco.text',
    'dateutil', 'dateutil.parser',
]

a = Analysis(                                    # noqa: F821
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # No runtime hook. The Tcl block above already answers a second
    # launch, and main.py imports runtime_splash_hook itself when it
    # needs close_bootloader_splash() — which takes the sentinel over
    # from the Tcl interpreter on its way past.
    runtime_hooks=[],
    # Qt ships every platform plugin and every translation; an AppImage
    # that carries the Wayland and XCB ones plus the Qt translations is
    # already large enough without the rest.
    excludes=[
        # tkinter is NOT excluded when the splash is built below: PyInstaller
        # draws the bootloader splash with Tk, and excluding it would leave
        # the AppImage silent for the seconds it spends unpacking itself.
        *([] if _HAVE_TK else ['tkinter']), 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtCharts',
        'PySide6.QtDataVisualization', 'PySide6.QtQuick3D',
        'PySide6.QtMultimediaWidgets', 'PySide6.QtBluetooth',
        'PySide6.QtNfc', 'PySide6.QtSerialPort', 'PySide6.QtWebSockets',
        'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtTest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)                                # noqa: F821


# ── Single instance, decided before the splash is even drawn ─────────────
# The Windows build answers a second launch with a message box out of the
# Tcl splash script, which runs before Python exists. Linux got the same
# question answered several seconds later, by the flock in
# runtime_splash_hook — so a second double click still put a splash on
# screen and only then went quiet.
#
# The Windows test is "can this file be deleted": a handle held open there
# makes delete fail. It is not portable, and worse than useless here —
# POSIX unlinks a file that is open, so on Linux the check would always
# say "not running" AND would delete the live instance's sentinel on the
# way past. The portable half of the same idea is the pid inside it: the
# sentinel names the process that wrote it, /proc says whether that
# process is still there, and comm says whether it is still ours rather
# than a stranger who inherited the number.
_single_instance_tcl = r"""
set _sentinel ""
catch {
    if {[info exists ::env(XDG_DATA_HOME)] && $::env(XDG_DATA_HOME) ne ""} {
        set _sentinel [file join $::env(XDG_DATA_HOME) "SaveSync" ".savesync.running"]
    } else {
        set _sentinel [file join $::env(HOME) ".local" "share" "SaveSync" ".savesync.running"]
    }
}

if {$_sentinel ne ""} {
    set _already_running 0

    if {[file exists $_sentinel]} {
        set _pid ""
        catch {
            set _sfh [open $_sentinel r]
            set _pid [string trim [read $_sfh]]
            close $_sfh
        }
        if {[string is integer -strict $_pid] && $_pid > 0
            && [file isdirectory "/proc/$_pid"]} {
            set _comm ""
            catch {
                set _cfh [open "/proc/$_pid/comm" r]
                set _comm [string tolower [string trim [read $_cfh]]]
                close $_cfh
            }
            if {$_comm eq "" || [string match "*savesync*" $_comm]} {
                set _already_running 1
            }
        }
    }

    if {$_already_running} {
        package require Tk
        wm withdraw .

        set _msg "SaveSync is already running.\nCheck the system tray."
        set _lang "en"
        set _cfg ""
        catch {
            if {[info exists ::env(XDG_DATA_HOME)] && $::env(XDG_DATA_HOME) ne ""} {
                set _cfg [file join $::env(XDG_DATA_HOME) "SaveSync" "config.json"]
            } else {
                set _cfg [file join $::env(HOME) ".local" "share" "SaveSync" "config.json"]
            }
        }

        if {$_cfg ne "" && [file exists $_cfg]} {
            catch {
                set _fh [open $_cfg r]
                set _data [read $_fh]
                close $_fh
                if {[regexp {"language"\s*:\s*"([^"]*)"} $_data -> _lang]} {}
            }
        }

        switch -exact -- $_lang {
            it  {set _msg "SaveSync è già in esecuzione.\nControlla la system tray."}
            es  {set _msg "SaveSync ya está en ejecución.\nRevisa la bandeja del sistema."}
            fr  {set _msg "SaveSync est déjà en cours d'exécution.\nVérifiez la barre des tâches."}
            de  {set _msg "SaveSync läuft bereits.\nÜberprüfen Sie die Taskleiste."}
            pt  {set _msg "SaveSync já está em execução.\nVerifique a bandeja do sistema."}
        }

        tk_messageBox -type ok -icon warning -title "SaveSync" -message $_msg
        exit 1
    }

    # Ours now. The handle stays open for the life of the process so the
    # pid inside always belongs to something alive.
    catch {
        file mkdir [file dirname $_sentinel]
        set ::_savesync_sentinel_fh [open $_sentinel w]
        puts $::_savesync_sentinel_fh [pid]
        flush $::_savesync_sentinel_fh
    }
}
"""


# ── The splash: animated, and sized for the display it lands on ─────────
# Both halves live in packaging/splash_frames.py, which the Windows spec
# imports too — same GIF, same Tcl trick, same sizing rule, one copy.
_frame0_path = ROOT / 'assets' / 'splash.png'
_splash_tcl = ""

if _HAVE_TK:
    _sets, _delay_ms, _frame0 = _splash_frames.build_frames(
        ROOT / 'assets' / 'splash_animated.gif',
        ROOT / 'build' / '_splash_frame0_linux.png',
    )
    if _sets:
        _frame0_path = _frame0
        _splash_tcl = _splash_frames.build_tcl(_sets, _delay_ms)
        for _line in _splash_frames.describe(_sets):
            print(_line)
    else:
        print("[spec] WARNING: no animated frames — static splash.png only")

splash = None
if _HAVE_TK:
    _original_build_script = _splash_tpl.build_script

    def _patched_build_script(**kwargs):
        script = (_single_instance_tcl
                  + _splash_frames.build_pre_tcl()
                  + _original_build_script(**kwargs)
                  + _splash_tcl)
        kind = "animated, DPI-sized" if _splash_tcl else "static PNG only"
        print(f"[spec] Tcl splash script assembled: {len(script) // 1024} KB ({kind})")
        return script

    _splash_tpl.build_script = _patched_build_script
    try:
        splash = Splash(                         # noqa: F821
            str(_frame0_path),
            binaries=a.binaries,
            datas=a.datas,
            text_pos=None,
            text_size=12,
            text_color='#76b900',
            minify_script=True,
            full_tk=True,
            always_on_top=False,
        )
    finally:
        # Restore at once: a later build in the same process must not
        # inherit the patch.
        _splash_tpl.build_script = _original_build_script

# ONEDIR, not onefile. A onefile build unpacks itself into /tmp on every
# launch, which an AppImage then does a second time — two extractions and
# two copies of a 300 MB tree for one start. The AppImage IS the single
# file the user gets; what goes inside it should be a plain directory.
exe = EXE(                                       # noqa: F821
    pyz,
    a.scripts,
    *([splash] if splash is not None else []),
    [],
    exclude_binaries=True,
    name='savesync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # AppImage compresses the whole payload anyway
    console=False,
)

coll = COLLECT(                                  # noqa: F821
    exe,
    a.binaries,
    *([splash.binaries] if splash is not None else []),
    a.datas,
    strip=False,
    upx=False,
    name='savesync',
)
