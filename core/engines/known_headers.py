"""Known fixed-header save layouts, one entry per game — the data half of
``core.engines.struct_header``'s generic reader. Each layout below is taken
from a real, working reader for that ONE game, never inferred from staring
at bytes; where it came from is in the layout's own comment. Add the next
documented header here, not as a new module.
"""
from .struct_header import HeaderField, HeaderLayout

# Just Cause 2's PC save header. Taken from the JC2.Save library
# (x-cubed/JC2MapViewer, LGPL 2.1) — SaveFileHeader.cs and SaveFile.cs, a
# real, working C# parser written for a save/map viewer, not inferred from
# staring at bytes. Its own comment says it best: "lazy check... pc
# savegames are exactly 891904 bytes" — the Xbox 360 build uses a
# different, big-endian layout at a different offset, which is out of
# scope here; only the PC layout (offset 0, little-endian) is read.
#
# The five NAMED fields (total_chaos, total_cash, percent_done, play_time_s,
# time_saved_epoch) are exactly what SaveFileHeader.cs itself names; the
# rest are fields it reads but never names ("unk1", "unk2", ...), kept here
# the same way — offered by position rather than invented a meaning for.
# Everything past the 56-byte header — a much larger chunk-based property
# system (settlements, missions, statistics, ...) — is out of scope and
# carried through unchanged, see struct_header.StructHeaderSave.
_JUST_CAUSE_2_PC_SIZE = 891904

JUST_CAUSE_2 = HeaderLayout(
    name="Just Cause 2",
    engine="Avalanche Engine (Just Cause 2)",
    source="JC2.Save (x-cubed/JC2MapViewer), SaveFileHeader.cs",
    fields=[
        HeaderField("unk1", "Unknown (unk1)", "<i"),
        HeaderField("unk2", "Unknown (unk2)", "<i"),
        HeaderField("total_chaos", "Total Chaos", "<i"),
        HeaderField("total_cash", "Total Cash", "<i"),
        HeaderField("percent_done", "Percent Done", "<i"),
        HeaderField("unk6", "Unknown (unk6)", "<i"),
        HeaderField("play_time_s", "Play Time (seconds)", "<Q"),
        HeaderField("time_saved_epoch", "Time Saved (Unix seconds)", "<Q"),
        HeaderField("unk8", "Unknown (unk8)", "<i"),
        HeaderField("unk9_1", "Unknown (unk9_1)", "<h"),
        HeaderField("unk9_2", "Unknown (unk9_2)", "<h"),
        HeaderField("unk10", "Unknown (unk10)", "<i"),
        HeaderField("unk11_1", "Unknown (unk11_1)", "<h"),
        HeaderField("unk11_2", "Unknown (unk11_2)", "<h"),
    ],
    matches=lambda data, ext: (ext == ".sav"
                               and len(data) == _JUST_CAUSE_2_PC_SIZE),
)

# Tried in this order — see struct_header.find_layout.
KNOWN_LAYOUTS = (JUST_CAUSE_2,)
