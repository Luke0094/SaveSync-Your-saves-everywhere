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
from pathlib import Path
import PyInstaller.building.splash_templates as _splash_tpl
import base64 as _b64

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
                set _data [read _fh]
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

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'i18n' / 'locales'), 'i18n/locales'),
        (str(ROOT / 'sync' / 'app_credentials.py'), 'sync'),
        (str(ROOT / 'assets' / 'splash.png'), 'assets'),
        (str(ROOT / 'assets' / 'splash_animated.gif'), 'assets'),
        (str(ROOT / 'assets' / 'icon.ico'), 'assets'),
        (str(ROOT / 'assets' / 'icon.png'), 'assets'),
    ],
    hiddenimports=[
        'core', 'core.config_transfer', 'core.config_manager', 'core.library',
        'core.backup', 'core.monitor', 'core.watcher', 'core.credentials',
        'core.machine', 'core.save_detector', 'core.startup', 'core.constants',
        'core.resolvers', 'core.game_api', 'sync.local_provider',
        'sync.google_drive', 'sync.onedrive_provider', 'sync.dropbox_provider',
        'sync.webdav_provider', 'sync.rclone_provider', 'sync.app_credentials',
        'ui.main_window', 'ui.overlay', 'ui.dialogs.add_game_dialog',
        'ui.dialogs.auto_scan_dialog', 'ui.dialogs.conflict_dialog',
        'ui.dialogs.cloud_verify_dialog', 'ui.dialogs.restore_dialog',
        'ui.dialogs.config_import_dialog', 'ui.dialogs.credits_dialog',
        'ui.pages.overview_page',
        'ui.pages.library_page', 'ui.pages.sync_page', 'ui.pages.backups_page',
        'ui.pages.settings_page', 'ui.widgets.hotkey_edit',
        'ui.widgets.file_list_widget', 'ui.styles.theme', 'ui.splash_screen',
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
# Animazione dello Splash Screen a livello Bootloader (Tcl/Tk Patched)
# ─────────────────────────────────────────────────────────────────────────────
# Strategia Logica:
# -----------------
# La classe standard `Splash()` di PyInstaller genera internamente uno script Tcl
# basato su dei template rigidi. Noi andiamo ad applicare una monkey-patch a
# `build_script()` per accodare la nostra logica personalizzata SUBITO DOPO il setup
# standard. Questo garantisce due requisiti critici:
#   1. L'oggetto canvas e il widget `splash_image` sono già stati inizializzati.
#   2. Il nostro codice carica ogni singolo frame della GIF partendo da stringhe
#      in formato base64 convertite al volo in PNG (`image create photo -data $b64`),
#      evitando di scrivere file temporanei su disco ad ogni frame.
#
# Risoluzione del problema del Delta Encoding delle GIF in Tcl/Tk:
# -----------------------------------------------------------------
# I file GIF animati ottimizzati memorizzano solo i pixel modificati rispetto al
# frame precedente (Delta Encoding). Se usassimo il selettore nativo di Tk
# `gif -index N`, Tcl estrarrebbe solo la porzione di delta modificata (es. un quadrato
# di 10x10 pixel disallineato) anziché il frame intero a 480x300.
# Per bypassare questo limite, usiamo la libreria Pillow (PIL) in fase di build:
# scansioniamo la GIF, usiamo `Image.seek()` che applica automaticamente i delta ripristinando
# l'immagine completa, ed esportiamo ogni frame risultante come una PNG standalone compressa
# in base64. A runtime, Tcl riceve delle PNG complete, azzerando i problemi di rendering.
#
# Architettura dell'Event Loop (`after 0`):
# ----------------------------------------
# Il caricamento iniziale e l'avvio dell'animazione avvengono dentro il blocco `after 0`.
# Questo sposta l'esecuzione all'interno del ciclo degli eventi (event loop) nativo di C,
# impedendo che il parsing massivo di stringhe base64 blocchi o provochi la terminazione
# silenziosa dello script durante la fase bloccante di `Tcl_EvalEx`.

_original_build_script = _splash_tpl.build_script
_gif_path = ROOT / 'assets' / 'splash_animated.gif'

# Generazione preventiva del Frame 0 per la stabilità del bootloader
_frame0_path = ROOT / 'build' / '_splash_frame0_runtime.png'

if _gif_path.exists():
    _frame_delay_ms = 90
    _frames_b64 = []
    try:
        import io as _io
        from PIL import Image as _PILImg

        _pil_gif = _PILImg.open(str(_gif_path))
        _frame_delay_ms = max(20, int(_pil_gif.info.get('duration', 90)))

        # Estrae e salva temporaneamente il frame 0 come PNG statico per l'inizializzazione di Splash()
        _frame0_path.parent.mkdir(parents=True, exist_ok=True)
        _pil_gif.seek(0)
        _pil_gif.copy().convert('RGB').save(str(_frame0_path), 'PNG')
        print(f"[spec] Extracted GIF frame 0 -> {_frame0_path}")

        # Estrazione sequenziale e conversione in memoria dei frame completi
        _pil_gif.seek(0)
        try:
            while True:
                _buf = _io.BytesIO()
                # Usiamo il convertitore RGBA e l'ottimizzazione PNG per ridurre l'impronta della stringa finale
                _pil_gif.copy().convert('RGBA').save(_buf, format='PNG', optimize=True)
                _frames_b64.append(_b64.b64encode(_buf.getvalue()).decode('ascii'))
                _pil_gif.seek(_pil_gif.tell() + 1)
        except EOFError:
            pass

        _n_frames  = len(_frames_b64)
        _total_kb  = sum(len(b) for b in _frames_b64) // 1024
        _anim_delay_ms = _frame_delay_ms
        print(f"[spec] {_n_frames} composed PNG frames embedded ({_total_kb} KB base64, {_anim_delay_ms} ms/frame)")

    except Exception as _e:
        print(f"[spec] WARNING: frame extraction failed ({_e}); falling back to splash.png")
        _frame0_path   = ROOT / 'assets' / 'splash.png'
        _anim_delay_ms = 450

    # ── Blocco di Animazione Tcl con Tracciamento Coda Integrato ──────────────────
    # Nota fondamentale: Il sistema di debug `_dbg` e la sua sequenza numerica NON devono
    # essere rimossi o ridotti. La loro presenza garantisce la corretta temporizzazione e
    # l'ordinamento dei messaggi all'interno della coda dei thread del bootloader Tcl/Tk.
    _gif_animation_tcl_template = r"""
set _dbg_seq 0
set _dbg_file ""
catch {
    if {$::tcl_platform(platform) eq "windows"} {
        set _dbg_file [file join $::env(TEMP) "savesync_splash_debug.log"]
    } else {
        set _dbg_file "/tmp/savesync_splash_debug.log"
    }
    file delete -force $_dbg_file
}
proc _dbg {msg} {
    global _dbg_file _dbg_seq
    incr _dbg_seq
    if {$_dbg_file eq ""} { return }
    catch {
        set _fh [open $_dbg_file a]
        puts $_fh "#${_dbg_seq} ${msg}"
        close $_fh
    }
}

_dbg "main-script-start"

after 0 {
    global _gif_frames_b64_list

    _dbg "after0-start"

    set _gif_frames {}
    set _idx 0
    foreach _b $_gif_frames_b64_list {
        if {[catch {
            image create photo "_gf_${_idx}" -data $_b -format png
        } _e]} {
            _dbg "frame-err idx=$_idx err=$_e"
            break
        }
        lappend _gif_frames "_gf_${_idx}"
        incr _idx
    }
    unset -nocomplain _gif_frames_b64_list _b _idx
    _dbg "frames=[llength $_gif_frames]"

    if {[llength $_gif_frames] < 1} { _dbg "ERROR-no-frames"; return }

    set _cid 1
    catch {
        set _r [.root.canvas find all]
        if {$_r ne ""} { set _cid [lindex $_r 0] }
    }
    _dbg "cid=$_cid"

    catch { .root.canvas itemconfigure $_cid -image [lindex $_gif_frames 0] } _e0
    _dbg "frame0-err=$_e0"

    if {[llength $_gif_frames] < 2} { _dbg "single-frame-only"; return }

    set _gif_cur 0
    proc _gif_tick {} {
        global _gif_frames _gif_cur _cid
        set _gif_cur [expr {($_gif_cur + 1) % [llength $_gif_frames]}]
        catch { .root.canvas itemconfigure $_cid -image [lindex $_gif_frames $_gif_cur] }
        after __DELAY__ _gif_tick
    }
    after __DELAY__ _gif_tick
    _dbg "loop-started"
}

_dbg "after0-scheduled"
"""

    if _frames_b64:
        # Concatenazione pulita delle stringhe base64 per non appesantire la memoria dell'interprete
        _gif_animation_tcl = (
            "set _gif_frames_b64_list {\n" +
            "\n".join(_frames_b64) +
            "\n}\n" +
            _gif_animation_tcl_template.replace("__DELAY__", str(_anim_delay_ms))
        )
    else:
        _gif_animation_tcl = ""

else:
    _frame0_path = ROOT / 'assets' / 'splash.png'
    print(f"[spec] WARNING: {_gif_path} not found — static splash.png only")
    _gif_animation_tcl = ""


def _patched_build_script(**kwargs):
    _script = _single_instance_tcl + _original_build_script(**kwargs) + _gif_animation_tcl
    _anim = "animated GIF after-loop" if _gif_animation_tcl else "static PNG only"
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