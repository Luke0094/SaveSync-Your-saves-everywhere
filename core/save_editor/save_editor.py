"""SaveSync — save-file editor.

Opens a save file, exposes the values inside it as an editable list, and
writes it back — with the original kept aside first, so any edit can be
undone.

This deliberately does NOT touch running processes: nothing is injected into
game memory, nothing is attached to, nothing is patched at runtime. It reads
and writes files at rest, the same thing a text editor does, which is both
the honest way to do it and the reason SaveSync cannot be mistaken for
something that tampers with programs.

Two rules the whole module is built around:

- **Never guess.** A format is either understood well enough to rebuild it
  byte-for-byte, or it is named and left alone (read-only). A half-understood
  binary format that is written back anyway produces a save the game refuses
  to load — and the player finds out hours later.
- **Prove the round trip.** Before any file is offered for editing, it is
  decoded and re-encoded, and the result must match the original exactly.
  If it does not, the file is still opened read-only when the format was
  recognised: values can be inspected, writing is refused. This catches
  every parsing gap without having to enumerate them.
"""
import logging
import shutil
from dataclasses import dataclass, field as _field
from datetime import datetime
from pathlib import Path

from core.constants import USER_DATA_DIR

from .base import SaveEditorError, explain  # noqa: F401 — public via this module
from . import registry as registry_module
# Only the three this module names directly. Every other reader reaches it
# through the registry, which is the point of having one: adding a format
# does not add a line here.
from .alicesoft_format import AliceSoftFormat
from .json_format import JsonFormat
from .playerprefs_format import PlayerPrefsFormat

logger = logging.getLogger(__name__)

# Engines we can read AND rebuild exactly.
# Anything else is named for the player rather than silently mangled.
# Named for the player, and with what is actually missing for each — saying
# which is the difference between a limitation and a shrug.
_RECOGNISED_ONLY = {
    ".es3": ("Unity (Easy Save 3)",
             "it is encrypted and its password is not in the game's files "
             "where Easy Save usually leaves it — put the key in an "
             "es3.key file beside the save and it will open"),
    # AliceSoft .asd carries several things in the same container, and all
    # the ones with values in them open (see AliceSoftFormat). What reaches
    # here is one that has none to offer, or one that is encrypted.
    ".asd": ("AliceSoft System 4",
             "it carries no values that can be named — the gallery lists are "
             "a run of numbers with nothing saying what they unlock, and the "
             "engine scrambles some of the rest"),
    ".vsf": ("AliceSoft System 4",
             "it is the flag file beside the save, and it carries no names to "
             "show a value under"),
    # RPG Developer Bakin — "YUKRDATA", then its Yukar runtime's own object
    # stream. Same reason: read, not described.
    ".sgs": ("RPG Developer Bakin",
             "its values are written as a plain object stream with nothing "
             "naming or typing them"),
    # Classic MJR TADS 3 machine state — binary VM snapshot, not a value list.
    ".t3v": ("TADS",
             "it is a TADS 3 VM state snapshot; the values inside are not "
             "named for editing — TAD-kit system.rec and JSON saves open "
             "normally"),
}


def _is_alicesoft(data: bytes) -> bool:
    """An AliceSoft container this reader could not open.

    Reached only once AliceSoftFormat has already declined the file, so what
    is left is a numbered save slot, an encrypted one, or the smaller "CSD"
    container the engine keeps its common settings in. All of them are worth
    naming rather than calling a mystery. The CSD check carries the deflate
    marker that has to follow it, because three bytes alone would claim files
    that are nothing of the sort.
    """
    if data[:4] == b"GD\x01\x01":
        return True
    return data[:4] == b"CSD\x00" and data[16:17] == b"\x78"

_BACKUP_DIR = USER_DATA_DIR / "save_edits"
# How many copies of one save to keep before the oldest is dropped, and how
# long to keep them at all. Both are in Settings; these are what they start
# at, and what is used when there is no configuration to ask (the editor runs
# headless in tests, where reading the config would need a running app).
_DEFAULT_COPIES = 3
_DEFAULT_COPY_DAYS = 7


# ── Detection ────────────────────────────────────────────────────────────────
#
# What each format is, which extensions point at it, how it recognises itself
# from its own bytes and which engines prefer it all live in ONE description
# per format — see core.save_editor.registry. This module consumes that; it
# no longer carries a table of its own for any of it.

_BY_EXTENSION = registry_module.by_extension()


# Both live in the registry now — the content tests there need them, and
# importing them from where they are described keeps one definition. Re-bound
# here because open_save's error paths below still ask these questions.
_in_unreal_save_folder = registry_module.in_unreal_save_folder
_looks_encrypted_unreal = registry_module.looks_encrypted_unreal


def _candidates(path: Path, data: bytes, game_dir=None) -> list:
    """Formats worth trying for this file, best guess first.

    Four passes, narrowest evidence first, and the order between them is the
    whole point:

    1. the ENGINE the library detected for the game, when it knows one — a
       Ren'Py install must not spend the open path on Easy Save / JSON probes;
    2. the file's EXTENSION;
    3. what the BYTES say, in registry order (most particular test first), so
       a file whose extension lies about it is still recognised;
    4. the generic shapes, which would otherwise claim files that belong to
       something more specific.

    Every one of those comes from core.save_editor.registry. Adding a format
    means describing it there — nothing in this function needs to know it
    exists.
    """
    out = []
    ext = path.suffix.lower()

    engine = ""
    if game_dir:
        try:
            from core.engines.game_engine import detect_engine
            engine = detect_engine(game_dir=str(game_dir)) or ""
        except Exception:
            engine = ""
    if engine:
        out.extend(registry_module.engine_preferences(engine))

    if ext in _BY_EXTENSION:
        out.append(_BY_EXTENSION[ext])

    out.extend(registry_module.sniffed(path, data, ext))
    out.extend(registry_module.lzstring_readers(data))
    out.extend(registry_module.fallback_readers())

    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


@dataclass
class SaveDocument:
    """A save file opened for viewing — and editing when the round trip holds.

    ``read_only`` is True when the format was recognised and values could be
    listed, but re-encoding does not rebuild the original bytes (or the
    equivalent value check for formats that never claim byte identity). The
    file is named and shown; writing is refused. That is the principle in
    the module docstring: reconstruct byte-for-byte, or name it and leave
    it alone — never a silent mangled write.
    """
    path: Path
    format_name: str
    engine: str
    fields: list = _field(default_factory=list)
    read_only: bool = False
    _fmt: object = None
    _original: bytes = b""
    # Set when the save is a registry key rather than a file — see open_save.
    _registry: str = ""
    # The values as they stand in the file, for the formats whose bytes
    # cannot be compared — see dirty_against_disk.
    _baseline: tuple = ()

    def set_value(self, path: tuple, value) -> None:
        if self.read_only:
            raise SaveEditorError(
                "this save can be read but not rewritten safely",
                "cheats.err_read_only")
        self._fmt.set_field(path, value)
        for f in self.fields:
            if f.path == path:
                f.value = value

    def dirty_against_disk(self) -> bool:
        """Whether anything here differs from what is in the file.

        Comparing the bytes is right only for a format that rebuilds them
        exactly. The others re-encode — LZString picks a different packing,
        a zip is written afresh, JSON is spelled without its spacing — so an
        untouched save comes out as different bytes carrying identical
        values, and comparing bytes would call it modified when nobody
        modified it. For those the values are what is compared, which is
        what the question is actually asking.
        """
        if self.read_only:
            return False
        if getattr(self._fmt, "verify_exact", True):
            return self._fmt.dump() != self._original
        return self._value_snapshot() != self._baseline

    def _value_snapshot(self) -> tuple:
        return tuple((f.path, f.value) for f in self._fmt.fields())

    def save(self) -> Path:
        """Write the edits back, keeping the original first.

        Returns the path of the copy that was set aside — the thing "undo"
        needs. Writing happens only after that copy exists.
        """
        if self.read_only:
            raise SaveEditorError(
                "this save can be read but not rewritten safely",
                "cheats.err_read_only")
        if self._registry:
            # Nothing to copy aside on disk, so the copy IS the export: the
            # key exactly as it stands, written where a file's backup would
            # go. Restoring it is the same import that writing uses.
            backup = backup_original(self.path, self._original)
            self.write_without_backup()
            return backup
        backup = backup_original(self.path)
        self.write_without_backup()
        return backup

    def write_without_backup(self) -> None:
        """Write the edits with no copy taken.

        For the value-hold loop, which takes ONE copy when it starts and then
        rewrites the same file repeatedly — a copy per cycle would bury the
        original under near-identical files. Everything else must go through
        save(), which keeps the original first.

        Written to a temporary file and moved into place, so a game reading
        the save mid-write sees either the old file or the new one, never
        half of each.
        """
        if self.read_only:
            raise SaveEditorError(
                "this save can be read but not rewritten safely",
                "cheats.err_read_only")
        data = self._fmt.dump()
        if self._registry:
            from core.registry_saves import import_registry_tree
            if not import_registry_tree(self._registry, data):
                raise SaveEditorError(
                    "the changes could not be written back to the registry")
            self._original = data
            self._baseline = self._value_snapshot()
            return
        tmp = self.path.with_suffix(self.path.suffix + ".savesync-tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        self._original = data
        self._baseline = self._value_snapshot()


def describe(path) -> str:
    """The engine this file looks like, for a file we cannot edit."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[0] if known else ""


def why_not(path) -> str:
    """Why that file cannot be opened, in a few words."""
    known = _RECOGNISED_ONLY.get(Path(path).suffix.lower())
    return known[1] if known else ""


def open_save(path, game_dir=None, progress=None) -> SaveDocument:
    """Open *path* for editing, or explain why it cannot be.

    *game_dir* is where the game itself is installed, when that is known. A
    save does not always sit with its game — Unity puts them under the user's
    profile — and one format needs to look in the game's own files.

    *progress* is for the one format whose search can run long: it is called
    with the seconds elapsed and stops the search by returning False. Every
    other format ignores it.
    """
    # Unity's PlayerPrefs are a save that is not a file: SaveSync proposes
    # them as "registry:HKCU\..." and backs them up already, so the same
    # export is what gets edited here. Everything downstream then works on
    # bytes exactly as it does for a file.
    from core.registry_saves import (export_registry_key, is_registry_path,
                                     registry_display)
    registry = str(path) if is_registry_path(str(path)) else ""
    if registry:
        p = Path(registry_display(registry).replace("\\", "/"))
        data = export_registry_key(registry)
        if not data:
            raise SaveEditorError(
                "that registry key could not be read, or holds nothing")
    else:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise SaveEditorError(f"cannot read {p.name}: {e}",
                                  "cheats.err_cannot_read", name=p.name,
                                  reason=str(e)) from e
        if not data:
            raise SaveEditorError("the file is empty", "cheats.err_empty")

    def prepare(cls):
        """A reader, told where the file came from.

        A couple of formats need that: Wolf looks beside the save for the
        game's database, Easy Save 3 looks inside the game for its password.
        The verification below builds a SECOND reader, and it has to be given
        the same context — without it that reader cannot open what the first
        one just wrote, and a perfectly good save is rejected.
        """
        fmt = cls()
        if hasattr(fmt, "source_path"):
            fmt.source_path = p
        if game_dir and hasattr(fmt, "game_dir"):
            fmt.game_dir = game_dir
        if progress is not None and hasattr(fmt, "progress"):
            fmt.progress = progress
        return fmt

    def engine_label_for(cls):
        label = cls.engine
        # When the library knows the game's engine, prefer that label for
        # formats that are shared across engines (plain JSON, key/value text)
        # so a WebGL or Java title is not shown as a generic "JSON" save.
        if game_dir and registry_module.shared_across_engines(cls):
            try:
                from core.engines.game_engine import detect_engine, label as eng_label
                detected = detect_engine(game_dir=str(game_dir))
                if detected in ("webgl", "tads", "java"):
                    label = eng_label(detected) or label
            except Exception:
                pass
        return label

    def make_doc(cls, fmt, *, read_only=False):
        fields = fmt.fields()
        # Plain JSON (and WebGL shells that write it) can be a valid save with
        # no leaf values yet — e.g. {"saves": []}. Rejecting that made empty
        # but real save files look "unreadable".
        if not fields and cls is not JsonFormat and not issubclass(cls, JsonFormat):
            return None
        doc = SaveDocument(
            path=p, format_name=cls.name, engine=engine_label_for(cls),
            fields=fields, read_only=read_only, _fmt=fmt, _original=data,
            _registry=registry)
        doc._baseline = doc._value_snapshot()
        return doc

    # A candidate that loaded but failed the round trip is kept aside while
    # later readers are still tried — a writable match must win. Only if
    # nothing writes safely do we return the named read-only document, so
    # the file is not demoted to "unrecognised" after we already understood it.
    readonly_doc = None

    def remember_readonly(cls, fmt, why: str):
        nonlocal readonly_doc
        if readonly_doc is not None:
            return
        doc = make_doc(cls, fmt, read_only=True)
        if doc is None:
            return
        logger.info(f"{p.name}: {cls.name} {why} — read-only")
        readonly_doc = doc

    tried = [PlayerPrefsFormat] if registry else _candidates(p, data, game_dir)

    for cls in tried:
        fmt = prepare(cls)
        try:
            fmt.load(data)
        except Exception as e:
            # Every format is tried in turn, so a file that is not this one's
            # is entirely normal. Worth a line all the same: without it, a
            # mistake inside a reader is indistinguishable from a file that
            # simply was not that format.
            logger.debug(f"{p.name}: not {cls.name} ({type(e).__name__}: {e})")
            continue
        # A format that proved byte-exactness during load (json_format,
        # naninovel, tads_rec: they compared dump()==data on the file they
        # just saw) can skip a second dump. A class-level verify_exact=True
        # claim is NOT trusted on sight: the dump is proven byte-exact here
        # and now. When the claim does not hold on this file the format is
        # downgraded to the generic value round trip below instead of being
        # offered for editing unproven.
        exact = getattr(fmt, "verify_exact", False)
        if exact and "verify_exact" in fmt.__dict__:
            doc = make_doc(cls, fmt, read_only=False)
            if doc is not None:
                return doc
            continue
        if exact:
            try:
                if fmt.dump() == data:
                    doc = make_doc(cls, fmt, read_only=False)
                    if doc is not None:
                        return doc
            except Exception:
                pass
            fmt.verify_exact = False
            exact = False
        # Format-specific fast verify (e.g. Ren'Py: zip rebuild + log bytes,
        # without a second full pickle walk on tens of thousands of values).
        if hasattr(fmt, "verify_value_round_trip"):
            try:
                if not fmt.verify_value_round_trip():
                    remember_readonly(cls, fmt, "value round trip differs")
                    continue
            except Exception:
                remember_readonly(cls, fmt, "value round trip failed")
                continue
            doc = make_doc(cls, fmt, read_only=False)
            if doc is None:
                continue
            return doc
        # The round trip is the whole safety argument: if we cannot rebuild
        # what we just read, we do not understand the file well enough to
        # write to it, whatever the extension says.
        try:
            rebuilt = fmt.dump()
        except Exception:
            remember_readonly(cls, fmt, "dump failed after load")
            continue
        # Asked of the reader, not of its class: a format can only know
        # which guarantee it can offer once it has seen the file. Naninovel
        # is the case — most of its saves are rebuilt byte for byte, and the
        # odd one written by a different build of the game is not.
        if not fmt.verify_exact:
            # Re-serialised formats differ in whitespace or compression, so
            # equality is checked where it means something: reading the
            # rebuilt bytes must give back the same values.
            try:
                probe = prepare(cls)
                # Reuse secrets the first reader already paid for (Easy Save
                # password hunt can unpack game archives).
                for attr in ("_password", "_iv"):
                    if hasattr(fmt, attr):
                        setattr(probe, attr, getattr(fmt, attr))
                probe.load(rebuilt)
                if ([(f.label, f.value) for f in probe.fields()]
                        != [(f.label, f.value) for f in fmt.fields()]):
                    remember_readonly(cls, fmt, "value round trip differs")
                    continue
            except Exception:
                remember_readonly(cls, fmt, "value round trip failed")
                continue
        elif rebuilt != data:
            remember_readonly(cls, fmt, "round trip differs")
            continue
        doc = make_doc(cls, fmt, read_only=False)
        if doc is None:
            continue
        return doc

    # ── Auto-resolution ──────────────────────────────────────────────────
    # Everything above read each format the way it is written TODAY. A format
    # whose layout is chosen by an engine version number can be wrong about a
    # build it has never seen — Unreal has moved the property tag, the GUID
    # and the version block between releases — and being wrong that way looks
    # exactly like "this file is not that format". So a game shipping a point
    # release could put its saves out of reach with nothing actually wrong,
    # and nothing to do about it until somebody wrote down a new threshold.
    #
    # The formats that can be wrong that way offer their other readings here
    # (see _Format.variants). This runs LAST, only where the answer would
    # otherwise have been read-only or unreadable, so no file that opens today
    # can be affected by it.
    #
    # Two gates, not one. The byte-exact round trip is the same standard every
    # reader in this module has to meet — but it is not by itself proof of
    # understanding, because a reader that carries what it could not parse as
    # an opaque tail rebuilds the file perfectly while having read almost none
    # of it. So a variant must ALSO say it genuinely parsed what it rebuilt.
    # A reading that passes both is as safe as one whose threshold happened to
    # be right, and is offered for editing on the same terms.
    def accepted(cls, fmt) -> bool:
        """The gate an auto-resolved reading has to pass. Two things, not one.

        The byte-exact round trip is the same standard every reader in this
        module meets — but on its own it is not proof of UNDERSTANDING: a
        reader that carries what it could not parse as an opaque tail
        reproduces the file perfectly while having read almost none of it. So
        the reader is also asked whether it genuinely parsed what it rebuilt.
        """
        try:
            if fmt.dump() != data:
                return False
            return bool(fmt.parse_is_plausible())
        except Exception:
            return False

    # Pass 1 — the formats already tried, read another way. For layouts chosen
    # by a version NUMBER: the thresholds are written from an engine's history,
    # so they are right about every build that existed when they were written
    # and can be wrong about the next one, and being wrong that way is
    # indistinguishable from "not that format".
    costly = registry_module.expensive_readers()
    for cls in tried:
        make_variants = getattr(cls, "variants", None)
        if not callable(make_variants):
            continue
        if cls in costly:
            # A reader that SEARCHES for a key pays that cost on every load,
            # and a variant list is a load per combination. UnrealEncryptedFormat
            # inherits GvasFormat's fifteen, and its own load() re-runs the
            # binary scan each time — one failed search over a game's modules
            # already runs into minutes, so the set of them would hang for the
            # best part of an hour. The layout question these variants answer
            # only arises once the bytes are readable anyway, and reaching that
            # point means the key was found.
            logger.debug(f"{p.name}: not retrying {cls.name} layouts — its "
                         f"reader searches for a key on every attempt")
            continue
        try:
            offered = list(make_variants())
        except Exception:
            continue
        for label, tweak in offered:
            fmt = prepare(cls)
            try:
                tweak(fmt)
                fmt.load(data)
            except Exception:
                continue
            if not accepted(cls, fmt):
                continue
            doc = make_doc(cls, fmt, read_only=False)
            if doc is None:
                continue
            logger.warning(
                f"{p.name}: read as {cls.name} with a layout its version did "
                f"not predict ({label}) — rebuilt byte-for-byte, so it is "
                f"offered for editing. A newer engine build is the usual cause.")
            return doc

    # Pass 2 — every OTHER reader there is, whatever pointed anywhere.
    #
    # Detection above works from what a file says about itself: its extension,
    # its magic, the engine of the game it belongs to. An engine update can
    # change any of those — a new container wrapper, a moved header field, a
    # magic that gained a byte — and then a save stops LOOKING like itself
    # while the reader that understands it is still sitting right here,
    # never offered because nothing pointed at it.
    #
    # Trying them all is only defensible because the gate does not care how a
    # reader was reached: it still has to rebuild the file byte-for-byte and
    # still has to have parsed it, and make_doc still refuses a reading with
    # no values in it. A reader reached this way is exactly as proven as one
    # reached by its own magic — which is the whole reason the round trip is
    # the standard rather than the extension.
    #
    # This is what makes version tolerance general rather than a per-format
    # favour: it applies to every reader in the registry, including ones added
    # later, with nothing to write down for each. The two that SEARCH for a key
    # are left out — worth minutes when something points at them, never on the
    # off-chance (see FormatSpec.expensive).
    already = set(tried)
    sweep_limit = registry_module.max_sweep_bytes()
    sweep = registry_module.all_readers() if len(data) <= sweep_limit else ()
    if not sweep:
        logger.debug(
            f"{p.name}: {len(data) >> 20} MB is past the backup size limit "
            f"({sweep_limit >> 20} MB) — not trying every reader on it")
    for cls in sweep:
        if cls in already:
            continue
        fmt = prepare(cls)
        try:
            fmt.load(data)
        except Exception:
            continue
        if not accepted(cls, fmt):
            continue
        doc = make_doc(cls, fmt, read_only=False)
        if doc is None:
            continue
        logger.warning(
            f"{p.name}: nothing about this file pointed at {cls.name}, but "
            f"that reader rebuilt it byte-for-byte and parsed it — offering it "
            f"for editing. An engine changing its container or header is the "
            f"usual cause.")
        return doc

    if readonly_doc is not None:
        return readonly_doc

    known = describe(p)
    if known:
        reason = why_not(p)
        # The missing piece for some of these is not in the save at all — it
        # is in the GAME, and a save dropped in on its own arrives with no
        # game behind it. Then "cannot be opened" is not the whole answer:
        # adding the game is a step that gets somewhere, and leaving it out
        # hides it. Once the game IS known, the reader has already looked, so
        # the offer is dropped rather than repeated at someone who took it.
        if not game_dir:
            reason = _KEY_LIVES_IN_THE_GAME.get(p.suffix.lower(), reason)
        raise SaveEditorError(
            f"{p.name} looks like a {known} save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=known,
            reason=reason)
    # An Unreal save that GVAS could not read is worth saying so about, rather
    # than calling it unrecognised: the file IS one, and what stopped the
    # reader is something inside it rather than the format being a mystery.
    if data[:4] == b"GVAS":
        raise SaveEditorError(
            f"{p.name} is an Unreal Engine save, but SaveSync could not read "
            f"all the way through it",
            "cheats.err_unreal_new", name=p.name)
    # An encrypted Unreal save, which by then is one whose key was not found:
    # saying so is the difference between a file nobody can identify and one
    # that only needs its key.
    if _in_unreal_save_folder(p) and _looks_encrypted_unreal(data):
        from core.save_editor.crypt.unreal_crypt import KEY_FILE
        reason = (f"the game encrypted it with a key of its own, and none was "
                  f"found — put the key in a {KEY_FILE} file beside the save")
        if not game_dir:
            reason += ", or add the game to the library"
        raise SaveEditorError(
            f"{p.name} is an Unreal Engine save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name,
            engine="Unreal Engine", reason=reason)
    # Artemis puts its settings, its across-playthroughs data and its slots
    # all into a .dat. Only the first opens; the other two are worth naming.
    if data[:3] == b"BOW":
        reason = ("its values sit in a tagged tree, and following one wrongly "
                  "would write a number into the wrong place")
        raise SaveEditorError(
            f"{p.name} looks like an Artemis save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine="Artemis",
            reason=reason)
    # AliceSoft puts the same container behind a .sav as often as behind a
    # .asd, and the extension map cannot see that. Its own header can.
    if _is_alicesoft(data):
        engine, reason = _RECOGNISED_ONLY[".asd"]
        raise SaveEditorError(
            f"{p.name} looks like a {engine} save, which SaveSync cannot edit "
            f"yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=engine,
            reason=reason)
    # Some engines encrypt their saves outright, and an encrypted file has
    # nothing in it to recognise — every byte is as likely as every other. The
    # only thing that can name one is the game it belongs to, which is what
    # the engine detector is for. Asked last, and only about a file nothing
    # else claimed, so it cannot take a save away from a reader that works.
    engine_said, have_game = _engine_that_encrypts(p, game_dir)
    if engine_said:
        reason = ("the engine encrypts it, and the key is inside the game's "
                  "own program rather than in the save")
        if not have_game:
            reason += (" — add the game to the library so SaveSync has its "
                       "executable to look in")
        raise SaveEditorError(
            f"{p.name} looks like a {engine_said} save, which SaveSync cannot "
            f"edit yet: {reason}",
            "cheats.err_known_not_editable", name=p.name, engine=engine_said,
            reason=reason)
    raise SaveEditorError(f"{p.name} is not a save format SaveSync can read",
                          "cheats.err_unreadable", name=p.name)


# Engines whose saves are encrypted with a key that lives in the game, so
# that no amount of looking at the file will identify or open it.
_ENCRYPTING_ENGINES = ("srpgstudio",)

# What to say instead, for formats whose missing piece is inside the GAME,
# when SaveSync does not know where the game is.
#
# A separate sentence rather than a line tacked onto the usual one, because
# the usual one is not true in this case: _RECOGNISED_ONLY says the password
# "is not in the game's files", and with no game to look in, nothing looked.
# Saying so, and naming the step that would fix it, is the difference between
# a dead end and an instruction.
_KEY_LIVES_IN_THE_GAME = {
    ".es3": ("it is encrypted, and the password is baked into the game's own "
             "build rather than kept in the save — add the game to the "
             "library and SaveSync will read it out of there, or put the key "
             "in an es3.key file beside the save"),
}


def _engine_that_encrypts(path: Path, game_dir=None) -> tuple:
    """(engine name, whether the game itself was found) for an encrypted save.

    ("", False) for every other file.

    The game folder is used when it is known, and the save's own folder is
    tried when it is not — these engines keep their saves under the game, so
    the detector's walk upward usually reaches it either way. Which of the two
    answered matters: the key to one of these saves is inside the game's own
    program file, so a game SaveSync cannot see is a game whose saves it could
    not open even once it knows how. Saying which case it is turns a dead end
    into something the player can act on — add the game, and the executable
    comes with it.
    """
    from core.engines.game_engine import detect_engine, label
    if game_dir:
        engine = detect_engine(game_dir=str(game_dir))
        if engine in _ENCRYPTING_ENGINES:
            return label(engine), True
    engine = detect_engine(game_dir=str(path.parent))
    if engine in _ENCRYPTING_ENGINES:
        return label(engine), bool(game_dir)
    return "", False


# ── Keeping the original ─────────────────────────────────────────────────────

def _slot_dir(path: Path, create: bool = True) -> Path:
    """Where copies of *path* are kept.

    Only creating it when something is about to be WRITTEN there. Reading is
    the common case — the editor asks about every save it lists — and a read
    that creates a folder left one empty directory behind per save file ever
    looked at, hundreds of them, none of which pruning could ever tidy.
    """
    # Keyed by the full path so two games' "save1.json" never collide.
    import hashlib
    key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    d = _BACKUP_DIR / key
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def backup_original(path, contents: bytes = None) -> Path:
    """Put a dated copy of *path* aside and return where it went.

    *contents* is for a save that is not a file — a registry key, whose
    "original" is the export taken when it was opened. Everything else about
    keeping copies, naming them and pruning them is the same either way.
    """
    p = Path(path)
    d = _slot_dir(p)
    # Milliseconds AND a collision guard: saving and then undoing happen
    # within the same second, and a second-resolution name would have the
    # undo's copy overwrite the pristine original it exists to protect.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    dest = d / f"{stamp}__{p.name}"
    n = 1
    while dest.exists():
        dest = d / f"{stamp}-{n}__{p.name}"
        n += 1
    if contents is None:
        shutil.copy2(p, dest)
    else:
        dest.write_bytes(contents)
    (d / "origin.txt").write_text(str(p), encoding="utf-8")
    prune_backups(p)
    logger.info(f"Kept the original of {p.name} at {dest.name}")
    return dest


def copy_policy() -> tuple:
    """How many copies to keep of one save, and for how many days."""
    try:
        from core.config_manager import get_config
        cfg = get_config()
        return (max(1, int(cfg.get("save_edit_copies", _DEFAULT_COPIES))),
                max(1, int(cfg.get("save_edit_copy_days", _DEFAULT_COPY_DAYS))))
    except Exception:
        # No configuration to ask — headless, or before the app is up.
        return _DEFAULT_COPIES, _DEFAULT_COPY_DAYS


def prune_backups(path) -> int:
    """Apply both rules to the copies kept of *path*; returns how many went.

    The newest copy is never dropped, whatever its age. Age alone could
    otherwise clear the lot — leaving an edit with nothing to undo it with,
    which is the one thing these copies exist to prevent.
    """
    keep, days = copy_policy()
    p = Path(path)
    try:
        # By time, not by name: two copies taken in the same millisecond get a
        # collision suffix, and "...-1__name" sorts BEFORE "...__name", which
        # would make the newest look like the oldest.
        kept = [f for f, _ in reversed(list_backups(p))]
    except OSError:
        return 0
    cutoff = datetime.now().timestamp() - days * 86400
    gone = 0
    # Oldest first, and never the last one standing.
    for old in kept[:-1]:
        too_many = len(kept) - gone > keep
        try:
            too_old = old.stat().st_mtime < cutoff
        except OSError:
            continue
        if not (too_many or too_old):
            continue
        try:
            old.unlink()
            gone += 1
        except OSError:
            pass
    if gone:
        logger.info(f"Dropped {gone} old copies of {p.name} "
                    f"(keeping {keep}, for {days} days)")
    return gone


def prune_all() -> int:
    """Apply the rules to every save the editor has kept copies of.

    Run once when the app starts. Without it the age rule would only hold for
    saves somebody happens to open again — edit a file today, never look at
    that game again, and its copies would sit there for good, which is not
    what "delete after seven days" says. Each slot folder remembers the file
    it belongs to, so they can all be found from here.
    """
    gone = 0
    try:
        slots = list(_BACKUP_DIR.iterdir())
    except OSError:
        return 0
    for slot in slots:
        if not slot.is_dir():
            continue
        try:
            origin = (slot / "origin.txt").read_text(encoding="utf-8").strip()
        except OSError:
            # A folder with nothing in it and no origin: left behind by an
            # older version, which made one for every save it merely LOOKED
            # at. rmdir refuses a folder holding anything, so this can only
            # ever remove the empty ones.
            try:
                slot.rmdir()
            except OSError:
                pass
            continue
        if origin:
            try:
                gone += prune_backups(Path(origin))
            except OSError:
                continue
    if gone:
        logger.info(f"Cleared {gone} old save-editor copies at startup")
    return gone


def list_backups(path) -> list:
    """Copies kept for *path*, newest first, as (file, modified)."""
    p = Path(path)
    out = []
    d = _slot_dir(p, create=False)
    if not d.is_dir():
        return []
    try:
        for f in d.glob(f"*__{p.name}"):
            try:
                out.append((f, datetime.fromtimestamp(f.stat().st_mtime)))
            except OSError:
                continue
    except OSError:
        return []
    return sorted(out, key=lambda t: t[1], reverse=True)


def restore_backup(backup, target) -> None:
    """Put a kept copy back. The file being replaced is itself kept first, so
    an undo can be undone."""
    b, t = Path(backup), Path(target)
    if not b.is_file():
        raise SaveEditorError("that copy is no longer there",
                              "cheats.err_copy_gone")
    if t.exists():
        backup_original(t)
    shutil.copy2(b, t)
    logger.info(f"Restored {t.name} from {b.name}")
