"""Bootloader-splash frames, and the Tcl that plays them at the right size.

Imported by both build recipes — ``savesync.spec`` (Windows) and
``packaging/savesync-linux.spec`` — because the splash is the one thing the
two builds genuinely share: the same GIF, the same Tcl trick, the same
sizing rule. Everything else in those files is platform-specific and stays
where it is.

Two problems are solved here.

**Animation.** PyInstaller's ``Splash()`` draws one still image. The
bootloader runs a Tcl script to do it, and that script can be appended to
— so every frame of the GIF is decoded at build time, embedded as base64
PNG, and swapped onto the canvas by an ``after`` loop. Pillow does the
decoding rather than Tk's own ``-index N`` because an optimised GIF stores
each frame as a delta on the one before: Tk hands back the delta, a small
misaligned patch, where PIL's ``seek()`` composes the frames and returns
whole images.

**Size.** A splash measured in pixels is a different object on every
display: 480x300 is a fifth of a 2560 desktop and a eighth of a 4K one.
So the frames are prepared at several scales and the right set is chosen
when the splash actually runs, by the same rule the rest of the UI uses —
``width / 2560``. Tk could scale at runtime instead, but only by whole
numbers and only by duplicating pixels; resampling here keeps the edges.

The scales stop at 1.5. Every frame of the chosen set becomes a live Tk
photo, and 55 frames at 720x450 is already about 70 MB of image data —
twice that for a 2.0 set, to make a splash slightly larger on a 5K screen.
"""
import base64
import io
from pathlib import Path

BASE_WIDTH = 2560           # the DIP width ui_scale() measures against
SCALES = (0.75, 1.0, 1.5)
_FALLBACK_DELAY_MS = 90


def _permille(scale: float) -> int:
    """Tcl array keys have to be integers, so scales travel as thousandths."""
    return int(round(scale * 1000))


def build_frames(gif_path, frame0_path):
    """Decode the GIF once per scale.

    Returns ``(sets, delay_ms, frame0_path)`` where *sets* maps a permille
    scale to its list of base64 PNGs. ``sets`` is empty when the GIF is
    missing or Pillow cannot read it — the caller then keeps its static
    PNG and the splash simply does not animate.
    """
    gif_path = Path(gif_path)
    frame0_path = Path(frame0_path)
    if not gif_path.exists():
        return {}, _FALLBACK_DELAY_MS, None

    try:
        from PIL import Image
    except Exception:
        return {}, _FALLBACK_DELAY_MS, None

    try:
        gif = Image.open(str(gif_path))
        delay_ms = max(20, int(gif.info.get('duration', _FALLBACK_DELAY_MS)))

        # The still the bootloader shows before the loop starts, and the
        # size the canvas is built at: the baseline, so the window only
        # ever grows from it.
        frame0_path.parent.mkdir(parents=True, exist_ok=True)
        gif.seek(0)
        gif.copy().convert('RGB').save(str(frame0_path), 'PNG')

        sets = {}
        for scale in SCALES:
            size = (round(gif.width * scale), round(gif.height * scale))
            frames = []
            gif.seek(0)
            while True:
                frame = gif.copy().convert('RGBA')
                if size != frame.size:
                    frame = frame.resize(size, Image.LANCZOS)
                # A palette PNG, not RGBA: the art is flat colour and this
                # is a third of the bytes, which matters when three sets
                # of 55 frames live inside the Tcl script as text.
                frame = frame.convert('RGB').quantize(colors=256)
                buf = io.BytesIO()
                frame.save(buf, format='PNG', optimize=True)
                frames.append(base64.b64encode(buf.getvalue()).decode('ascii'))
                try:
                    gif.seek(gif.tell() + 1)
                except EOFError:
                    break
            sets[_permille(scale)] = frames
        return sets, delay_ms, frame0_path
    except Exception:
        return {}, _FALLBACK_DELAY_MS, None


# The runtime half. Everything is wrapped in catch: a splash that fails is
# a splash that does not appear, and that must never stop the app starting.
_TCL_TEMPLATE = r"""
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

# The monitor SaveSync last ran on, if it has ever run. Tk knows only the
# size of the whole X screen, which on a desk with two monitors is both of
# them: centring on it lands on the seam, and scaling from it makes the
# splash as large as the pair. The app leaves the rectangle behind on each
# start for exactly this.
proc _savesync_monitor {} {
    set path ""
    catch {
        if {$::tcl_platform(platform) eq "windows"} {
            set path [file join $::env(APPDATA) "SaveSync" ".screen"]
        } elseif {[info exists ::env(XDG_DATA_HOME)] && $::env(XDG_DATA_HOME) ne ""} {
            set path [file join $::env(XDG_DATA_HOME) "SaveSync" ".screen"]
        } else {
            set path [file join $::env(HOME) ".local" "share" "SaveSync" ".screen"]
        }
    }
    if {$path eq "" || ![file exists $path]} { return {} }
    set rect {}
    catch {
        set fh [open $path r]
        set rect [string trim [read $fh]]
        close $fh
    }
    if {[llength $rect] != 4} { return {} }
    lassign $rect mx my mw mh
    if {![string is integer -strict $mw] || $mw <= 0} { return {} }
    if {![string is integer -strict $mh] || $mh <= 0} { return {} }
    return [list $mx $my $mw $mh]
}

after 0 {
    global _gif_sets _gif_delay

    _dbg "after0-start"

    set _mon [_savesync_monitor]
    if {[llength $_mon] == 4} {
        lassign $_mon _mx _my _mw _mh
    } else {
        set _mx 0
        set _my 0
        set _mw [winfo screenwidth .]
        set _mh [winfo screenheight .]
    }
    set _byp 0
    catch { if {[info exists ::_savesync_bypass]} { set _byp $::_savesync_bypass } }
    _dbg "monitor=${_mx},${_my} ${_mw}x${_mh} bypass=${_byp}"

    # The same rule the rest of the chrome scales by.
    set _want [expr {double($_mw) / __BASEW__}]
    set _pick 0
    foreach _key [lsort -integer [array names _gif_sets]] {
        if {$_pick == 0} { set _pick $_key ; continue }
        if {abs($_key / 1000.0 - $_want) < abs($_pick / 1000.0 - $_want)} {
            set _pick $_key
        }
    }
    _dbg "want=[format %.2f $_want] pick=$_pick"
    if {$_pick == 0} { _dbg "ERROR-no-sets" ; return }

    set _frames {}
    set _idx 0
    foreach _b $_gif_sets($_pick) {
        if {[catch {
            image create photo "_gf_${_idx}" -data $_b -format png
        } _e]} {
            _dbg "frame-err idx=$_idx err=$_e"
            break
        }
        lappend _frames "_gf_${_idx}"
        incr _idx
    }
    unset -nocomplain _gif_sets
    _dbg "frames=[llength $_frames]"
    if {[llength $_frames] < 1} { _dbg "ERROR-no-frames" ; return }

    set _first [lindex $_frames 0]
    set _iw [image width $_first]
    set _ih [image height $_first]

    set _cid 1
    catch {
        set _r [.root.canvas find all]
        if {$_r ne ""} { set _cid [lindex $_r 0] }
    }

    # The canvas was built around the baseline still; grow it, move the
    # image to the new centre, and put the window back in the middle of
    # the monitor at its new size.
    catch {
        .root.canvas configure -width $_iw -height $_ih
        .root.canvas coords $_cid [expr {$_iw / 2}] [expr {$_ih / 2}]
    }
    catch {
        set _px [expr {int($_mx + 0.5 * ($_mw - $_iw))}]
        set _py [expr {int($_my + 0.5 * ($_mh - $_ih))}]
        wm geometry . ${_iw}x${_ih}+${_px}+${_py}
        raise .
        _dbg "geometry=${_iw}x${_ih}+${_px}+${_py}"
    }

    catch { .root.canvas itemconfigure $_cid -image $_first } _e0
    _dbg "frame0-err=$_e0"

    if {[llength $_frames] < 2} { _dbg "single-frame-only" ; return }

    set _gif_cur 0
    set _gif_frames $_frames
    proc _gif_tick {} {
        global _gif_frames _gif_cur _cid _gif_delay
        set _gif_cur [expr {($_gif_cur + 1) % [llength $_gif_frames]}]
        catch { .root.canvas itemconfigure $_cid -image [lindex $_gif_frames $_gif_cur] }
        after $_gif_delay _gif_tick
    }
    after $_gif_delay _gif_tick
    _dbg "loop-started"
}

_dbg "after0-scheduled"
"""


# Runs BEFORE PyInstaller's own splash script, which is the whole point —
# see build_pre_tcl.
_PRE_TCL = r"""
catch {
    if {[info exists ::env(WAYLAND_DISPLAY)] && $::env(WAYLAND_DISPLAY) ne ""} {
        package require Tk
        wm overrideredirect . 1
        set ::_savesync_bypass 1
    }
}
"""


def build_pre_tcl():
    """Tcl to put in FRONT of PyInstaller's splash script.

    Under a Wayland compositor's X bridge, where a client asks to be is a
    suggestion and the answer is no: measured on WSLg, a splash asked to
    sit at +1040+570 was reparented into a frame and ended up at
    +2326+1316. On Linux PyInstaller deliberately leaves the window managed
    — it sets the splash window type rather than overrideredirect — which
    is right on a real X11 desktop and useless here.

    Order is everything. The flag decides whether the window manager takes
    the window at all, and it is read once, when the window is first
    mapped. PyInstaller's script ends with ``raise .``, which creates and
    maps it — so anything appended AFTER that script is already too late,
    as measured: the flag was set, the window stayed in its frame, and
    withdrawing and remapping did not persuade Weston to let go.

    Loading Tk here costs nothing: the script that follows does it anyway.
    """
    return _PRE_TCL


def build_tcl(sets, delay_ms):
    """The Tcl to append to PyInstaller's own splash script.

    Empty string when there are no frames, so the caller can append it
    unconditionally.
    """
    if not sets:
        return ""
    blocks = [f"set _gif_delay {int(delay_ms)}"]
    for key in sorted(sets):
        blocks.append("set _gif_sets(%d) {\n%s\n}" % (key, "\n".join(sets[key])))
    body = _TCL_TEMPLATE.replace("__BASEW__", str(BASE_WIDTH))
    return "\n".join(blocks) + "\n" + body


def describe(sets):
    """One line per prepared set, for the build log."""
    lines = []
    for key in sorted(sets):
        frames = sets[key]
        kb = sum(len(f) for f in frames) // 1024
        lines.append(f"[spec] splash x{key / 1000:.2f}: {len(frames)} frames, "
                     f"{kb} KB base64")
    return lines
