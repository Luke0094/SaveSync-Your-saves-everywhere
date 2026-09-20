"""SaveSync — the one place a save format announces itself.

Adding support for a format used to mean editing ``save_editor`` in three
separate places: the extension table, the per-engine preference map buried
inside the detection function, and the ordered chain of magic-byte tests. Miss
one and the format works for files named the way you tested and silently not
for the rest — the same class of drift ``core.exe_stems`` exists to remove for
executable names.

So a format is described ONCE, here, as a :class:`FormatSpec`. What
``save_editor`` needs is derived from that description:

- which extensions point at it,
- whether it recognises a file from its CONTENT, whatever it is called,
- which detected engines should try it first, and in what order,
- whether it is generic enough that the engine's own name is the better
  label for it (plain JSON is not "a JSON save", it is a Godot save).

**Order is the contract.** ``SPECS`` is a sequence, not a set, and its order
is the order content tests run in — most particular first, so a file that two
formats could both claim goes to the one that knows more about it. Move an
entry and you change detection; add one at the end and you cannot.

Two things deliberately stay OUT of here, because they are about formats that
are recognised and *not* editable rather than about reading them:
``_RECOGNISED_ONLY`` and ``_KEY_LIVES_IN_THE_GAME`` in ``save_editor``. They
answer "why can this not be opened", which is a different question from "what
is this".
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .alicesoft_format import AliceSoftFormat
from .alicesoft_vsf_format import AliceSoftVsfFormat
from .artemis_format import ArtemisFormat
from .es3_format import Es3Format
from .gvas_format import GvasFormat, UnrealEncryptedFormat
from .json_format import JsonFormat
from .keyvalue_format import KeyValueFormat
from .kirikiri_format import KirikiriFormat
from .lcf_format import LcfFormat
from .lzstring_json_format import _LzStringJson
from .naninovel_format import NaninovelFormat
from .playerprefs_format import PlayerPrefsFormat
from .protobuf_format import ProtobufFormat
from .qsp_format import QspFormat
from .rags_format import RagsFormat
from .renpy_format import RenpyFormat
from .rpgmaker_mv_format import RpgMakerMvFormat
from .rpgmaker_mz_format import RpgMakerMzFormat
from .rubymarshal_format import RubyMarshalFormat
from .sol_format import SolFormat
from .sqlite_format import SqliteFormat
from .steamid_aes_format import SteamIdAesFormat
from .struct_header_format import StructHeaderFormat
from .sugarcube_format import SugarCubeFormat
from .tads_rec_format import TadsRecFormat
from .tyrano_format import TyranoFormat
from .wolf_format import WolfFormat
from .wolf_lz4_format import WolfLZ4Format
from .xml_format import XmlFormat


# ── Helpers a content test may need ─────────────────────────────────────────

def in_unreal_save_folder(path) -> bool:
    """Whether *path* sits where Unreal itself puts a game's saves.

    ``<Game>/Saved/SaveGames`` is the engine's own layout, not something a
    game chooses, so it identifies an Unreal save even when the file will not
    identify itself.
    """
    parts = [p.lower() for p in Path(path).parts]
    return "savegames" in parts and "saved" in parts


def looks_encrypted_unreal(data: bytes) -> bool:
    try:
        from core.save_editor.crypt.unreal_crypt import looks_encrypted
        return looks_encrypted(data)
    except Exception:
        return False


def in_steamid_keyed_save_folder(path) -> bool:
    """Whether *path* sits under one of the games crypt/steamid_aes_recipes
    knows a SteamID-key recipe for — the cheap pre-filter for
    SteamIdAesFormat, whose file itself is fully encrypted and says nothing
    on its own (see crypt/steamid_aes)."""
    try:
        from core.save_editor.crypt.steamid_aes_recipes import RECIPES
        haystack = str(path).lower()
        return any(r.folder_marker.lower() in haystack for r in RECIPES)
    except Exception:
        return False


def looks_like_steamid_keyed_save(data: bytes) -> bool:
    try:
        from core.save_editor.crypt.steamid_aes import looks_like_save
        return looks_like_save(data)
    except Exception:
        return False


def _is_wolf(data: bytes) -> bool:
    try:
        from core.engines.wolf import is_wolf_save
        return bool(is_wolf_save(data))
    except Exception:
        return False


def _is_wolf_lz4(data: bytes) -> bool:
    """Cheap only: a tag byte in range. Confirming this format for real
    costs a seed search (see core.engines.wolf_lz4.find_seed) — far too much
    to spend on every .sav on the chance it is this one, so this stays a
    heuristic and the search itself is what actually proves or disproves a
    file, the moment WolfLZ4Format.load() is tried."""
    try:
        from core.engines.wolf_lz4 import is_candidate
        return bool(is_candidate(data))
    except Exception:
        return False


def find_header_layout(data: bytes, ext: str):
    """The known fixed-header layout *data* matches, or None — see
    core/engines/known_headers."""
    try:
        from core.engines.known_headers import KNOWN_LAYOUTS
        from core.engines.struct_header import find_layout
        return find_layout(data, ext, KNOWN_LAYOUTS)
    except Exception:
        return None


_LZSTRING_BODY = re.compile(rb"[A-Za-z0-9+/=\s]+")


# ── The description ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FormatSpec:
    """One reader, and everything detection needs to know about it."""

    cls: type
    # Extensions that point at this reader. First match wins, so an extension
    # belongs to the reader that owns it, not to everything that could parse it.
    extensions: tuple = ()
    # Recognise the file from its bytes (and, for the two that need it, from
    # where it sits). Called as sniff(path, data, ext). None = extension only.
    sniff: Optional[Callable] = None
    # True when this reader is generic enough that the engine detected for the
    # game is the better thing to call the save — a Godot game saving JSON
    # should not be labelled "JSON".
    shared_across_engines: bool = False
    # Tried before everything else when the game's engine is known to be one
    # of these. Order between formats for the SAME engine comes from
    # ENGINE_PREFERENCES below, which is where that ordering is legible.
    engines: tuple = field(default_factory=tuple)
    # True when merely ATTEMPTING this reader costs real time — unpacking
    # game archives, or searching binaries for a key — rather than failing
    # fast the way a wrong parse does. It answers two questions in open_save,
    # and both are "not on spec": do not try this on the off-chance (the
    # last-resort sweep), and do not try it REPEATEDLY (the variant retries,
    # where a reader that searches on every load would multiply minutes by
    # the number of layouts).
    expensive: bool = False


# The order of this sequence IS the order content tests run in. Read it as a
# funnel: names itself unambiguously, then names itself with help, then merely
# looks plausible. The two catch-alls at the bottom are last on purpose.
SPECS = (
    # ── Named by extension, and by a magic no one else writes ───────────────
    FormatSpec(NaninovelFormat, extensions=(".nson",)),
    # expensive: finding the password can mean unpacking the game's archives.
    FormatSpec(Es3Format, extensions=(".es3",), engines=("unity",), expensive=True),
    FormatSpec(RpgMakerMzFormat, extensions=(".rmmzsave",), engines=("rpgmaker",),
               # A deflate stream opens with a marker whose two bytes are both
               # under 0x80, so it survives MZ's text wrapper and is
               # recognisable as it is.
               sniff=lambda path, data, ext: data[:1] == b"x" and len(data) > 8),
    FormatSpec(RpgMakerMvFormat, extensions=(".rpgsave",), engines=("rpgmaker",)),
    FormatSpec(LcfFormat, extensions=(".lsd",),
               # The LCF name is length-prefixed, so an .lsd starts with its
               # own length.
               sniff=lambda path, data, ext: (data[:1] == bytes([len(b"LcfSaveData")])
                                              and data[1:12] == b"LcfSaveData")),
    FormatSpec(SolFormat, extensions=(".sol",),
               # A .sol carries its magic six bytes in, after version and length.
               sniff=lambda path, data, ext: data[6:10] == b"TCSO"),
    FormatSpec(RagsFormat, extensions=(".rsv",)),
    FormatSpec(RubyMarshalFormat, extensions=(".rvdata2", ".rvdata", ".rxdata"),
               engines=("rpgmaker",),
               # Ruby stamps every Marshal stream with its version, which is as
               # strong a signal as a magic number — and RPG Maker also writes
               # .dat files this way, which no extension would have told us.
               sniff=lambda path, data, ext: data[:2] == b"\x04\x08"),
    FormatSpec(GvasFormat, extensions=(".sav",), engines=("unreal",),
               sniff=lambda path, data, ext: data.startswith(b"GVAS")),
    # A save with a known, documented FIXED header — see
    # core/engines/known_headers for the list of layouts and
    # core/engines/struct_header for the reader every one of them is read
    # through. One more documented header is one more entry there, not a
    # new reader class or a new line here.
    FormatSpec(StructHeaderFormat,
               sniff=lambda path, data, ext: find_header_layout(data, ext)
               is not None),
    FormatSpec(KirikiriFormat,
               # KiriKiri by its extension, and by its compressed marker
               # whatever it is called. The other two wrappers — plain UTF-16
               # text, or a thumbnail bitmap — are far too ordinary to claim a
               # file on their own.
               sniff=lambda path, data, ext: (ext == ".ksd"
                                              or data.startswith(b"\xfe\xfe\x02\xff\xfe"))),
    # expensive: locating the variable database means walking the file for an
    # offset that parses cleanly, which costs roughly a second per megabyte on
    # bytes that are not a Wolf save at all. The sniff below is the cheap gate
    # that decides whether it is worth paying — the blind sweep has none.
    FormatSpec(WolfFormat, engines=("wolfrpg",), expensive=True,
               # Wolf hides behind obfuscation, so only unlocking it can tell.
               sniff=lambda path, data, ext: (ext == ".sav" and len(data) > 0x20
                                              and _is_wolf(data))),
    # expensive: confirming this one for real costs a seed search (see
    # _is_wolf_lz4 and core.engines.wolf_lz4.find_seed) — the sniff below is
    # only the cheap tag-byte gate, ENGINE_PREFERENCES is what keeps this
    # from being tried on a file that already opened as standard Wolf.
    FormatSpec(WolfLZ4Format, engines=("wolfrpg",), expensive=True,
               sniff=lambda path, data, ext: (ext == ".sav" and len(data) > 0x20
                                              and _is_wolf_lz4(data))),
    FormatSpec(AliceSoftFormat, engines=("alicesoft",),
               # AliceSoft names itself in its first four bytes, which is just
               # as well: it puts the same container behind .asd and behind
               # .sav, and what is INSIDE decides whether it opens at all.
               sniff=lambda path, data, ext: data[:4] in (b"GD\x01\x01", b"PSR\x00")),
    # A VSF is the save-slot SUMMARY (date, scene description, playtime)
    # that sits beside the GSAVE above — different container, same engine,
    # its own 8-byte magic (see core/engines/alicesoft_vsf).
    FormatSpec(AliceSoftVsfFormat, extensions=(".vsf",), engines=("alicesoft",),
               sniff=lambda path, data, ext: data[:8] == b"VSF\x00\x00\x00\x00\x00"),
    FormatSpec(UnrealEncryptedFormat, expensive=True,
               # An Unreal save whose game encrypted it says nothing about
               # itself — the magic is under the encryption with everything
               # else. Where it SITS says it instead. Only ever tried with a
               # key that then has to produce the magic, so a file that merely
               # lives there and is something else costs one failed decryption.
               sniff=lambda path, data, ext: (in_unreal_save_folder(path)
                                              and looks_encrypted_unreal(data))),
    # A SteamID-keyed save (see crypt/steamid_aes) also says nothing about
    # itself — fully encrypted, keyed by the player's own SteamID64 rather
    # than a key the game carries — so this too is found by where it sits
    # rather than by its bytes. Not expensive: the key is DERIVED (or tried
    # against a handful of local Steam accounts), never brute-force
    # searched for, so a wrong match here costs a few deterministic decrypt
    # attempts, not a scan.
    FormatSpec(SteamIdAesFormat, extensions=(".sav",),
               sniff=lambda path, data, ext: (in_steamid_keyed_save_folder(path)
                                              and looks_like_steamid_keyed_save(data))),
    # expensive, and deliberately given no sniff at all: protobuf has no
    # magic bytes whatsoever (see core/engines/protobuf_raw), so the only
    # way to tell it apart from unrelated bytes is a real structural parse
    # of the whole file — never worth that on the off-chance, only when
    # something has already asked to check every format. Reached ONLY
    # through Pass 2's full sweep (registry.all_readers), which does not
    # consult sniff() at all — load() is the entire gate.
    FormatSpec(ProtobufFormat, expensive=True),
    FormatSpec(ArtemisFormat, engines=("artemis",),
               # Artemis writes settings, global data and slots all into a
               # .dat, and all three name themselves in the first four bytes.
               sniff=lambda path, data, ext: data[:3] == b"BOW"),
    FormatSpec(TyranoFormat, engines=("tyrano",),
               # TyranoScript also writes .sav, so the extension cannot tell it
               # from Unreal or Wolf. What can is that its JSON arrives
               # escaped: the opening brace is on disk as the three characters
               # "%7B", which nothing else here starts with.
               sniff=lambda path, data, ext: data[:3] in (b"%7B", b"%5B")),
    FormatSpec(QspFormat,
               # QSP names itself in the clear, in either encoding it uses.
               sniff=lambda path, data, ext: (
                   data.startswith(b"QSPSAVEDGAME")
                   or data.startswith("QSPSAVEDGAME".encode("utf-16-le")))),
    FormatSpec(RenpyFormat, extensions=(".save",), engines=("renpy",),
               # Ren'Py .save is a zip; magic catches Unity/Godot .save
               # fall-throughs that still happen to be zips.
               sniff=lambda path, data, ext: data[:4] == b"PK\x03\x04"),
    FormatSpec(TadsRecFormat, extensions=(".rec",), engines=("tads",),
               shared_across_engines=True,
               # TAD-kit record: ASCII integer tokens, usually NUL-padded.
               sniff=lambda path, data, ext: (
                   ext == ".rec"
                   or (len(data) >= 64 and data[:1] in b"0123456789"
                       and b"\x00" in data[-32:]))),
    FormatSpec(SqliteFormat, extensions=(".db", ".sqlite", ".sqlite3"),
               engines=("java",), shared_across_engines=True,
               sniff=lambda path, data, ext: data.startswith(b"SQLite format 3\x00")),

    # ── Generic containers: recognised by shape, so they come after ─────────
    FormatSpec(JsonFormat, extensions=(".json",),
               engines=("unity", "godot", "webgl", "java"),
               shared_across_engines=True,
               sniff=lambda path, data, ext: data[:1].lstrip()[:1] in (b"{", b"[")),
    FormatSpec(XmlFormat, extensions=(".xml",), engines=("godot",),
               # XML whatever the file is called: a game saving through .NET's
               # serializer often names the result .sav or .dat.
               sniff=lambda path, data, ext: data[:200].lstrip()[:1] == b"<"),
    FormatSpec(SugarCubeFormat, engines=("webgl",), shared_across_engines=True),
    FormatSpec(_LzStringJson, shared_across_engines=True),
    FormatSpec(KeyValueFormat, extensions=(".ini", ".cfg", ".conf", ".properties"),
               shared_across_engines=True),
    FormatSpec(PlayerPrefsFormat),
)


# Which readers to try FIRST when the library already knows the engine, and in
# what order. Kept as its own table because that order is a judgement about
# the engine ("an RPG Maker save is far more likely to be MZ than XP"), not a
# property of any one format — and it reads as a list precisely because the
# sequence is the whole content.
ENGINE_PREFERENCES = {
    "renpy":     (RenpyFormat,),
    "unity":     (Es3Format, JsonFormat),
    "godot":     (JsonFormat, XmlFormat),
    "unreal":    (GvasFormat,),
    "rpgmaker":  (RpgMakerMzFormat, RpgMakerMvFormat, RubyMarshalFormat),
    "wolfrpg":   (WolfFormat, WolfLZ4Format),
    "artemis":   (ArtemisFormat,),
    "alicesoft": (AliceSoftFormat, AliceSoftVsfFormat),
    "tyrano":    (TyranoFormat,),
    "tads":      (TadsRecFormat,),
    "webgl":     (JsonFormat, SugarCubeFormat),
    "java":      (SqliteFormat, JsonFormat),
    # Not a game engine detect_engine() ever produces — a save shape, not an
    # engine, picked only by a person naming it directly from "Open as…"
    # (see cheats_page.py). Forcing it is what lets ProtobufFormat run at
    # all outside a full sweep: see its own FormatSpec, expensive=True with
    # no sniff, for why it is otherwise unreachable on the off-chance.
    "protobuf":     (ProtobufFormat,),
    # Same reasoning, for a file whose known-layout match (see
    # find_header_layout) did not fire automatically — an unusual file size
    # for an otherwise-documented header, say.
    "known_header": (StructHeaderFormat,),
}

# LZString base64 is cheap to try and fails fast, but "looks like base64"
# matches far too much to sit in the funnel as one more content test. Which
# engine wrote it is decided by what comes out, most particular reader first.
_LZSTRING_READERS = (RpgMakerMvFormat, SugarCubeFormat, _LzStringJson)

# Tried for anything still unclaimed. Both are shapes rather than formats, so
# they are the last word rather than an early guess.
_FALLBACK_READERS = (JsonFormat, KeyValueFormat)


def by_extension() -> dict:
    """``{extension: reader}`` — first spec claiming an extension owns it."""
    table = {}
    for spec in SPECS:
        for ext in spec.extensions:
            table.setdefault(ext, spec.cls)
    return table


def engine_preferences(engine: str) -> tuple:
    """Readers to try first for a known *engine*, in order."""
    return ENGINE_PREFERENCES.get(engine or "", ())


def shared_across_engines(cls) -> bool:
    """True when the engine's own name is the better label for this reader."""
    return any(s.cls is cls and s.shared_across_engines for s in SPECS)


def engine_of(cls) -> str:
    """The single engine key *cls* means, or "" when it does not name one
    on its own — a reader shared across several (plain JSON, key=value
    text: see shared_across_engines) is read by more than that one, so
    picking its first listed engine would be a guess dressed up as an
    answer, worse than saying nothing."""
    for spec in SPECS:
        if spec.cls is cls:
            if spec.shared_across_engines or len(spec.engines) != 1:
                return ""
            return spec.engines[0]
    return ""


def sniffed(path, data: bytes, ext: str) -> list:
    """Readers whose own content test claims this file, in registry order."""
    out = []
    for spec in SPECS:
        if spec.sniff is None:
            continue
        try:
            if spec.sniff(path, data, ext):
                out.append(spec.cls)
        except Exception:
            continue        # a content test must never decide the whole open
    return out


def lzstring_readers(data: bytes) -> tuple:
    """The base64/LZString family, when the bytes could plausibly be one."""
    if data[:1].isalnum() and len(data) > 8 and _LZSTRING_BODY.fullmatch(data[:512] or b""):
        return _LZSTRING_READERS
    return ()


def fallback_readers() -> tuple:
    return _FALLBACK_READERS


def max_sweep_bytes() -> int:
    """Past this, a file is not an unrecognised save — it is something else.

    Read from the user's own ``max_backup_size_mb``, NOT from a number chosen
    here. That setting already answers "how big may a save be" and is on the
    Settings page with a range the user picked from; a second limit invented
    alongside it would quietly contradict theirs the moment they changed it,
    and a save inside the size they allow would be refused for a reason not
    written down anywhere they can see.
    """
    try:
        from core.config_manager import get_config
        mb = int(get_config().get("max_backup_size_mb", 512))
    except Exception:
        mb = 512
    return max(1, mb) * 1024 * 1024


def expensive_readers() -> frozenset:
    """Readers whose every ATTEMPT costs real time, not just a failed parse.

    They search — unpacking a game's archives, scanning its binaries — so the
    cost is paid per load() and is measured in seconds to minutes rather than
    milliseconds. Anything that would try a reader repeatedly, or try it on
    the off-chance, has to leave these out.
    """
    return frozenset(s.cls for s in SPECS if s.expensive)


def all_readers(include_expensive: bool = False) -> tuple:
    """Every registered reader worth trying blind, in registry order.

    For the last-resort pass in open_save: when an engine update changes
    something that makes a save stop LOOKING like itself — a magic byte, a
    header field, a container wrapper — no content test claims it any more,
    even though the reader that understands it is still sitting right here.
    Trying them all is only defensible because the acceptance gate does not
    care how a reader was reached: it still has to rebuild the file and prove
    it parsed it.

    The searching readers are left out by default — see expensive_readers,
    which is where that question is asked and answered rather than counted
    here — since a blind, automatic sweep should not pay minutes on the
    off-chance for every unrelated file that fails to open. *include_expensive*
    puts them back in for the one caller who is supposed to ask for that
    explicitly: a person who has already seen "unsupported" and chose to pay
    the cost rather than pick the engine by hand.
    """
    return tuple(s.cls for s in SPECS if include_expensive or not s.expensive)
