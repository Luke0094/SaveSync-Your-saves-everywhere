# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SaveSync.

Build command:
    pyinstaller savesync.spec

Output: dist/SaveSync.exe

La limitazione della singola istanza viene gestita su TRE livelli indipendenti (Defense in Depth):
  1. Livello Tcl (nello script di Splash) — Eseguito DURANTE l'estrazione dei file temporanei, prima che Python esista.
     Utilizza un file di lock con controllo PID (evitando l'apertura di porte TCP).
  2. Livello runtime_splash_hook.py — Eseguito subito dopo l'estrazione completa e immediatamente prima di caricare main.py.
     Utilizza un Named Mutex nativo su Windows e flock su sistemi Unix (meccanismo autoritativo definitivo).
  3. Livello main.py (_acquire_lock) — Meccanismo di fallback per quando l'applicazione viene eseguita direttamente dai sorgenti.
"""
import sys
import sys as _sys
from pathlib import Path
import PyInstaller.building.splash_templates as _splash_tpl

block_cipher = None
ROOT = Path(SPECPATH)

# ── Controllo Tcl Singola Istanza (Iniettato direttamente nello splash script) ──
# Il processo Python principale (tramite runtime_splash_hook) mantiene aperto un handle sul file sentinella.
# Su sistemi Windows, un file aperto in modalità esclusiva non può essere eliminato: usiamo questa proprietà
# tentando un comando `file delete`. Se fallisce, un'altra istanza è attiva -> mostra l'errore e termina.
# Questo approccio è puramente Tcl, non alloca porte di rete ed è istantaneo.

_single_instance_tcl = r"""
set _sentinel ""
if {$::tcl_platform(platform) eq "windows"} {
    catch {set _sentinel [file join $::env(APPDATA) "SaveSync" ".savesync.running"]}
} else {
    catch {
        if {[info exists ::env(XDG_DATA_HOME)] && $::env(XDG_DATA_HOME) ne ""} {
            set _sentinel [file join $::env(XDG_DATA_HOME) "SaveSync" ".savesync.running"]
        } else {
            set _sentinel [file join $::env(HOME) ".local" "share" "SaveSync" ".savesync.running"]
        }
    }
}

if {$_sentinel ne ""} {
    set _already_running 0

    if {[file exists $_sentinel]} {
        # Tenta l'eliminazione: fallisce su Windows se l'handle è bloccato da un'altra istanza attiva
        if {[catch {file delete $_sentinel}]} {
            set _already_running 1
        }
    }

    if {$_already_running} {
        package require Tk
        wm withdraw .

        set _msg "SaveSync is already running.\nCheck the system tray."
        set _lang "en"
        set _cfg ""

        if {$::tcl_platform(platform) eq "windows"} {
            catch {set _cfg [file join $::env(APPDATA) "SaveSync" "config.json"]}
        } else {
            catch {set _cfg [file join $::env(HOME) ".local" "share" "SaveSync" "config.json"]}
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

    # Crea il file sentinella e mantiene l'handle aperto per tutta la vita del processo.
    # Impedisce la cancellazione o la sovrascrittura da parte di processi paralleli.
    catch {
        file mkdir [file dirname $_sentinel]
        set ::_savesync_sentinel_fh [open $_sentinel w]
        puts $::_savesync_sentinel_fh [pid]
        flush $::_savesync_sentinel_fh
        # NOTA: Non chiudere l'handle, deve rimanere allocato fino alla chiusura dell'app
    }
}
"""

_datas = [
    (str(ROOT / 'i18n' / 'locales'), 'i18n/locales'),
    (str(ROOT / 'sync' / 'app_credentials.py'), 'sync'),
    (str(ROOT / 'assets' / 'splash.png'), 'assets'),
    (str(ROOT / 'assets' / 'splash_animated.gif'), 'assets'),
    (str(ROOT / 'assets' / 'icon.ico'), 'assets'),
    (str(ROOT / 'assets' / 'icon.png'), 'assets'),
]

from PyInstaller.utils.hooks import collect_submodules as _collect_submodules

# Themes are found by scanning ui.styles at runtime (see
# ui/styles/theme.py::_discover_themes), so nothing imports a new theme
# module by name and PyInstaller's analysis would never see it. Collecting
# the whole package is what lets "drop a module in ui/styles" keep working
# in a frozen build instead of only from source.
_THEME_MODULES = _collect_submodules('ui.styles')

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_datas,
    hiddenimports=_THEME_MODULES + [
        'core', 'core.config_transfer', 'core.config_manager', 'core.library',
        'core.backup', 'core.monitor', 'core.watcher', 'core.credentials',
        'core.machine', 'core.save_detector', 'core.startup', 'core.constants',
        'core.resolvers', 'core.game_api',
        # Save editor + engine codecs (lazy-imported inside format handlers —
        # listed so a frozen build cannot drop one that analysis skipped).
        'core.save_editor', 'core.save_editor.save_editor',
        'core.save_editor.save_hold', 'core.save_editor.base',
        'core.save_editor.json_format', 'core.save_editor.xml_format',
        'core.save_editor.playerprefs_format', 'core.save_editor.naninovel_format',
        'core.save_editor.lzstring_json_format',
        'core.save_editor.rpgmaker_mv_format',
        'core.save_editor.rpgmaker_mz_format',
        'core.save_editor.sugarcube_format',
        'core.save_editor.lcf_format',
        'core.save_editor.rubymarshal_format', 'core.save_editor.keyvalue_format',
        'core.save_editor.gvas_format', 'core.save_editor.renpy_format',
        'core.save_editor.sol_format', 'core.save_editor.qsp_format',
        'core.save_editor.es3_format', 'core.save_editor.rags_format',
        'core.save_editor.kirikiri_format',
        'core.save_editor.wolf_format', 'core.save_editor.alicesoft_format',
        'core.save_editor.artemis_format', 'core.save_editor.tyrano_format',
        'core.save_editor.tads_rec_format', 'core.save_editor.sqlite_format',
        # Decryptors (moved under crypt/)
        'core.save_editor.crypt', 'core.save_editor.crypt.es3',
        'core.save_editor.crypt.game_keys', 'core.save_editor.crypt.unityfs',
        'core.save_editor.crypt.unreal_crypt', 'core.save_editor.crypt.wolf',
        'core.engines', 'core.engines.game_engine',
        'core.engines.alicesoft', 'core.engines.artemis',
        'core.engines.gvas', 'core.engines.kirikiri',
        'core.engines.lcf', 'core.engines.rpgmaker', 'core.engines.lzstring',
        'core.engines.naninovel', 'core.engines.qsp',
        'core.engines.rags', 'core.engines.renpy', 'core.engines.renpy_save',
        'core.engines.rubymarshal', 'core.engines.sol', 'core.engines.tyrano',
        'core.engines.wolf', 'core.engines.sqlite_db',
        'core.engines.playerprefs', 'core.engines.tads',
        'core.engines.keyvalue', 'core.engines.xml_save',
        'ui.pages.cheats_page',
        'sync.local_provider',
        'sync.google_drive', 'sync.onedrive_provider', 'sync.dropbox_provider',
        'sync.webdav_provider', 'sync.rclone_provider', 'sync.app_credentials',
        'ui.main_window', 'ui.overlay', 'ui.dialogs.add_game_dialog',
        'ui.dialogs.auto_scan_dialog', 'ui.dialogs.conflict_dialog',
        'ui.dialogs.cloud_verify_dialog', 'ui.dialogs.restore_dialog',
        'ui.dialogs.config_import_dialog', 'ui.dialogs.credits_dialog',
        'ui.pages.overview_page',
        'ui.pages.library_page', 'ui.pages.sync_page', 'ui.pages.backups_page',
        'ui.pages.settings_page', 'ui.widgets.hotkey_edit',
        'ui.widgets.file_list_widget', 'ui.styles.theme',
        'ui.styles.arrow_icons', 'ui.splash_screen',
        'hotkeys', 'pynput', 'pynput.keyboard._win32', 'pynput.mouse._win32',
        'i18n', 'dateutil', 'dateutil.parser',
        'keyring.backends', 'keyring.backends.Windows',
        'google.auth.transport.requests', 'google_auth_oauthlib.flow',
        'googleapiclient.discovery', 'cryptography.hazmat.primitives.ciphers.aead',
        'PIL', 'pillow_avif', 'jaraco.functools', 'jaraco.context', 'jaraco.text',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / 'runtime_splash_hook.py')],
    excludes=['matplotlib', 'numpy', 'scipy', 'cv2', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ─────────────────────────────────────────────────────────────────────────────
# Splash Screen: animato e dimensionato in base al DPI dello schermo
# ─────────────────────────────────────────────────────────────────────────────
# La logica vive in packaging/splash_frames.py, condivisa con lo spec Linux:
# stessa GIF, stessa tecnica Tcl, stessa regola di scala. Qui restano solo
# il controllo di istanza singola (sopra) e la configurazione di Splash().
#
# I fotogrammi vengono preparati a piu` scale in fase di build e quella
# giusta viene scelta a runtime da `larghezza monitor / 2560` — la stessa
# regola con cui l'applicazione scala il resto dell'interfaccia. Prima la
# dimensione era fissa a 480x300: corretta a 4K con scaling 150% (dove Tk
# non e` DPI-aware e Windows la stira di 1.5), troppo piccola a 4K al 100%
# e troppo grande a 1080p.
_sys.path.insert(0, str(ROOT / 'packaging'))
import splash_frames as _splash_frames               # noqa: E402

_frame0_path = ROOT / 'assets' / 'splash.png'
_gif_animation_tcl = ""

_sets, _delay_ms, _frame0 = _splash_frames.build_frames(
    ROOT / 'assets' / 'splash_animated.gif',
    ROOT / 'build' / '_splash_frame0_runtime.png',
)
if _sets:
    _frame0_path = _frame0
    _gif_animation_tcl = _splash_frames.build_tcl(_sets, _delay_ms)
    for _line in _splash_frames.describe(_sets):
        print(_line)
else:
    print("[spec] WARNING: nessun fotogramma animato — solo splash.png statico")

_original_build_script = _splash_tpl.build_script


def _patched_build_script(**kwargs):
    _script = (_single_instance_tcl
               + _splash_frames.build_pre_tcl()
               + _original_build_script(**kwargs)
               + _gif_animation_tcl)
    _anim = "animated, DPI-sized" if _gif_animation_tcl else "static PNG only"
    print(f"[spec] Tcl splash script assembled: {len(_script) // 1024} KB ({_anim})")
    return _script


_splash_tpl.build_script = _patched_build_script

# Configurazione dell'istanza Splash.
# minify_script=True applica la compressione degli spazi bianchi riducendo il peso dello script finale.
# full_tk=True include i pacchetti Tk essenziali per evitare problemi di inizializzazione dell'immagine su alcune build.
splash = Splash(
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

# Ripristino immediato della funzione originale per evitare conflitti o effetti collaterali su build successive
_splash_tpl.build_script = _original_build_script

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    splash,
    splash.binaries,
    [],
    name='SaveSync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / 'assets' / 'icon.ico'),
)

# ─────────────────────────────────────────────────────────────────────────────
# NOTA DI COMPILAZIONE FONDAMENTALE
# ─────────────────────────────────────────────────────────────────────────────
# Durante lo sviluppo e i test sullo splash screen animato, compila SEMPRE usando il flag `--clean`:
#
#     pyinstaller --clean savesync.spec
#
# PyInstaller tende ad archiviare i file intermedi (.tcl e .res) all'interno della cartella temporanea `build/`.
# Senza l'opzione `--clean`, le modifiche apportate alla logica Tcl o alle stringhe dei commenti qui sopra
# verrebbero ignorate dall'esecutore, continuando a mostrare il vecchio comportamento memorizzato in cache.