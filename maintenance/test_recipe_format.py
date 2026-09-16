"""Standalone verification for the generic unwrap recipe battery.

No pytest here — run directly:

    python maintenance/test_recipe_format.py

Plain asserts, corpus built in code. See core/save_editor/crypt/recipes.py
and core/save_editor/recipe_format.py for what is under test, and
save_editor.py's open_save()/_detect() split for the structural guarantee
the "already openable" tests below are pinning.
"""
import inspect
import json
import os
import random
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.save_editor import save_editor
from core.save_editor.base import SaveEditorError, SaveField
from core.save_editor.save_editor import SaveDocument, open_save
from core.save_editor.recipe_format import RecipeFormat
from core.save_editor.crypt import recipes as recipes_module

_LCG_MUL, _LCG_ADD = 214013, 2531011
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _fail(msg: str):
    raise AssertionError(msg)


def _xor(data: bytes, key: int, offset: int = 0) -> bytes:
    out = bytearray(data)
    for i in range(offset, len(out)):
        out[i] ^= key
    return bytes(out)


def _lcg_full_keystream(seed_byte: int, n: int) -> bytes:
    state = seed_byte & 0xFFFFFFFF
    out = bytearray(n)
    for i in range(n):
        state = (state * _LCG_MUL + _LCG_ADD) & 0xFFFFFFFF
        out[i] = (state >> 16) & 0xFF
    return bytes(out)


def _lcg_xor(data: bytes, seed_byte: int, offset: int) -> bytes:
    out = bytearray(data)
    n = len(out) - offset
    ks = _lcg_full_keystream(seed_byte, n)
    for i in range(n):
        out[offset + i] ^= ks[i]
    return bytes(out)


def _plain_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _write_temp(data: bytes, suffix: str = ".bin") -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def _field(doc, label):
    for f in doc.fields:
        if f.label == label:
            return f
    _fail(f"no field named {label!r} in {[f.label for f in doc.fields]}")


def _cleanup(*paths):
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


# ── Positive: every recipe shape actually round-trips end to end ───────────

def test_xor_offset0_no_header():
    payload = _plain_json({"hp": 100, "name": "Ada"})
    data = _xor(payload, 0x5A, 0)
    p = _write_temp(data)
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("xor offset0: expected a writable doc")
        if not isinstance(doc._fmt, RecipeFormat):
            _fail(f"expected RecipeFormat, opened via {type(doc._fmt).__name__}")
        doc.set_value(_field(doc, "hp").path, 999)
        doc.write_without_backup()

        doc2 = open_save(p, try_recipes=True)
        if doc2.read_only or _field(doc2, "hp").value != 999:
            _fail("xor offset0: edit did not survive a lock/unlock round trip")
    finally:
        _cleanup(p)


def test_xor_behind_junk_header():
    payload = _plain_json({"hp": 100, "name": "Ada"})
    header = bytes(range(16))
    data = header + _xor(payload, 0x37, 0)
    data = _xor(data, 0x37, 16)  # key only applies from offset 16 on
    p = _write_temp(data)
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("xor+header: expected a writable doc")
        if not isinstance(doc._fmt, RecipeFormat):
            _fail(f"expected RecipeFormat, opened via {type(doc._fmt).__name__}")
        doc.set_value(_field(doc, "name").path, "Zoe")
        doc.write_without_backup()

        doc2 = open_save(p, try_recipes=True)
        if doc2.read_only or _field(doc2, "name").value != "Zoe":
            _fail("xor+header: edit did not survive a lock/unlock round trip")
        # The clear header itself must still read back untouched.
        if p.read_bytes()[:16] != header:
            _fail("xor+header: the clear header was not preserved on write-back")
    finally:
        _cleanup(p)


def test_zlib_then_xor():
    payload = _plain_json({"level": 7, "gold": 250})
    compressed = zlib.compress(payload, 9)
    data = _xor(compressed, 0x11, 0)
    p = _write_temp(data)
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("zlib+xor: expected a writable doc")
        if not isinstance(doc._fmt, RecipeFormat):
            _fail(f"expected RecipeFormat, opened via {type(doc._fmt).__name__}")
        doc.set_value(_field(doc, "gold").path, 12345)
        doc.write_without_backup()

        doc2 = open_save(p, try_recipes=True)
        if doc2.read_only or _field(doc2, "gold").value != 12345:
            _fail("zlib+xor: edit did not survive a lock/unlock round trip")
    finally:
        _cleanup(p)


def test_raw_deflate_no_xor():
    # Long/repetitive enough that a level-9 raw-deflate encode provably
    # differs, byte for byte, from core.engines.naninovel's own level-6
    # compressor — otherwise this is ambiguous with a real Naninovel save
    # (also raw-deflate JSON) and _detect's full sweep would open it via
    # NaninovelFormat before the recipe battery ever got a turn, testing
    # the wrong code path despite still passing.
    payload = _plain_json({
        "chapter": 3, "flag": True,
        "notes": "lorem ipsum lorem ipsum lorem ipsum lorem ipsum dolor sit amet " * 5,
    })
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = co.compress(payload) + co.flush()
    p = _write_temp(compressed)
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("raw-deflate: expected a writable doc")
        if not isinstance(doc._fmt, RecipeFormat):
            _fail(f"expected RecipeFormat, opened via {type(doc._fmt).__name__}")
        doc.set_value(_field(doc, "chapter").path, 9)
        doc.write_without_backup()

        doc2 = open_save(p, try_recipes=True)
        if doc2.read_only or _field(doc2, "chapter").value != 9:
            _fail("raw-deflate: edit did not survive a lock/unlock round trip")
    finally:
        _cleanup(p)


def test_lcg_full_byte_from_header_seed():
    payload = _plain_json({"score": 42})
    seed_byte = 0x99
    header = bytes([seed_byte])  # offset 0's own byte IS the seed
    data = header + _lcg_xor(payload, seed_byte, 0)
    p = _write_temp(data)
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("lcg: expected a writable doc")
        if not isinstance(doc._fmt, RecipeFormat):
            _fail(f"expected RecipeFormat, opened via {type(doc._fmt).__name__}")
        doc.set_value(_field(doc, "score").path, 777)
        doc.write_without_backup()

        doc2 = open_save(p, try_recipes=True)
        if doc2.read_only or _field(doc2, "score").value != 777:
            _fail("lcg: edit did not survive a lock/unlock round trip")
    finally:
        _cleanup(p)


# ── Negative: noise must never be recovered into a false positive ──────────

def test_negative_icon_png():
    icon = _REPO_ROOT / "assets" / "icon.png"
    if not icon.exists():
        _fail(f"fixture missing: {icon}")
    try:
        open_save(icon, try_recipes=True)
        _fail("icon.png: expected SaveEditorError, got a doc")
    except SaveEditorError:
        pass


def test_negative_random_4mib():
    data = random.Random(20240914).randbytes(4 * 1024 * 1024)
    p = _write_temp(data)
    try:
        started = time.monotonic()
        try:
            open_save(p, try_recipes=True)
            _fail("random 4MiB: expected SaveEditorError, got a doc")
        except SaveEditorError:
            pass
        elapsed = time.monotonic() - started
        print(f"[test_recipe_format] 4 MiB random, worst case: {elapsed:.2f}s")
    finally:
        _cleanup(p)


def test_negative_plain_text():
    data = ("This is an ordinary paragraph of plain prose, not a save file "
            "of any kind, and nothing in it looks like JSON, XML, a zip, a "
            "SQLite header, or a Ruby Marshal stream.\n").encode("utf-8") * 4
    p = _write_temp(data)
    try:
        open_save(p, try_recipes=True)
        _fail("plain text: expected SaveEditorError, got a doc")
    except SaveEditorError:
        pass
    finally:
        _cleanup(p)


def test_negative_small_random_never_escalates_readonly():
    """The read-only escalation path (see open_save) must also stay a no-op
    on a file no recipe can improve — not just the top-level unreadable
    path the other negative tests already cover."""
    # Search for a small random buffer no recipe finds anything in, rather
    # than trusting one fixed seed to stay a true negative forever — a
    # printable-ratio false positive over ~10^4 trials is rare per seed but
    # not rare enough to hardcode blindly.
    data = None
    for seed in range(50):
        candidate = random.Random(seed).randbytes(256)
        if not list(recipes_module.find_candidates(candidate)):
            data = candidate
            break
    if data is None:
        _fail("could not find a clean random-noise fixture in 50 tries")

    class _FakeReadOnly:
        name = "Fake read-only"
        engine = "Fake"
        verify_exact = False

        def __init__(self):
            self._data = b""

        def load(self, raw: bytes) -> None:
            self._data = raw

        def dump(self) -> bytes:
            return self._data + b"!"

        def fields(self) -> list:
            return [SaveField(("x",), "x", "int", len(self._data))]

        def set_field(self, path, value) -> None:
            pass

        def parse_is_plausible(self) -> bool:
            return True

    from core.save_editor import registry as registry_module
    original = registry_module.fallback_readers
    registry_module.fallback_readers = lambda: original() + (_FakeReadOnly,)
    p = _write_temp(data)
    try:
        doc = open_save(p, try_recipes=True)
        if not doc.read_only:
            _fail("expected the fake reader's read-only reading back unchanged")
        if doc.format_name != _FakeReadOnly.name:
            _fail(f"expected the fake reader's own reading, got {doc.format_name!r}")
    finally:
        registry_module.fallback_readers = original
        _cleanup(p)


def test_readonly_escalation_succeeds():
    """A file a fake reader only half-understands (read-only today) that
    ALSO happens to be a recipe away from real JSON — try_recipes=True must
    replace the read-only reading with the writable one; try_recipes=False
    must leave the read-only reading exactly as it is today."""
    payload = _plain_json({"coins": 5})
    data = _xor(payload, 0x2C, 0)

    class _FakeReadOnly:
        name = "Fake read-only"
        engine = "Fake"
        verify_exact = False

        def __init__(self):
            self._data = b""

        def load(self, raw: bytes) -> None:
            self._data = raw

        def dump(self) -> bytes:
            return self._data + b"!"

        def fields(self) -> list:
            return [SaveField(("x",), "x", "int", len(self._data))]

        def set_field(self, path, value) -> None:
            pass

        def parse_is_plausible(self) -> bool:
            return True

    from core.save_editor import registry as registry_module
    original = registry_module.fallback_readers
    registry_module.fallback_readers = lambda: original() + (_FakeReadOnly,)
    p = _write_temp(data)
    try:
        doc_off = open_save(p, try_recipes=False)
        if not doc_off.read_only or doc_off.format_name != _FakeReadOnly.name:
            _fail("try_recipes=False must leave the read-only reading untouched")

        doc_on = open_save(p, try_recipes=True)
        if doc_on.read_only:
            _fail("try_recipes=True must escalate a read-only reading a recipe "
                  "can improve on")
        if _field(doc_on, "coins").value != 5:
            _fail("escalated reading did not expose the right value")
    finally:
        registry_module.fallback_readers = original
        _cleanup(p)


def test_already_openable_json_never_tries_recipes():
    payload = _plain_json({"gold": 10, "name": "hero"})
    p = _write_temp(payload, suffix=".json")

    def _forbidden(self, data):
        _fail("RecipeFormat.load must never be called for an already-openable save")

    original_load = RecipeFormat.load
    RecipeFormat.load = _forbidden
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("plain JSON should open writable")
    finally:
        RecipeFormat.load = original_load
        _cleanup(p)


def test_already_openable_keyvalue_never_tries_recipes():
    data = b"hp = 100\nname = Ada\n"
    p = _write_temp(data, suffix=".ini")

    def _forbidden(self, raw):
        _fail("RecipeFormat.load must never be called for an already-openable save")

    original_load = RecipeFormat.load
    RecipeFormat.load = _forbidden
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only:
            _fail("plain key/value text should open writable")
    finally:
        RecipeFormat.load = original_load
        _cleanup(p)


def test_try_recipes_false_never_calls_battery():
    called = []
    original = save_editor.try_recipe_battery

    def _spy(*a, **kw):
        called.append(True)
        return original(*a, **kw)

    save_editor.try_recipe_battery = _spy
    data = random.Random(3).randbytes(64)
    p = _write_temp(data)
    try:
        try:
            open_save(p, try_recipes=False)
            _fail("expected SaveEditorError for unrecognisable noise")
        except SaveEditorError as e:
            if e.key != "cheats.err_unreadable":
                _fail(f"expected cheats.err_unreadable, got {e.key!r}")
        if called:
            _fail("try_recipes=False must never call try_recipe_battery")
    finally:
        save_editor.try_recipe_battery = original
        _cleanup(p)


# ── The two wrapper gates, in isolation from any real format ────────────────

def test_wrapper_skips_known_not_editable():
    original_detect = save_editor._detect
    original_battery = save_editor.try_recipe_battery

    def _stub_detect(*a, **kw):
        raise SaveEditorError("known but not editable",
                              "cheats.err_known_not_editable", name="x")

    def _forbidden_battery(*a, **kw):
        _fail("try_recipe_battery must not run for a non-generic raise")

    save_editor._detect = _stub_detect
    save_editor.try_recipe_battery = _forbidden_battery
    p = _write_temp(b"anything")
    try:
        try:
            open_save(p, try_recipes=True)
            _fail("expected the stubbed SaveEditorError to propagate")
        except SaveEditorError as e:
            if e.key != "cheats.err_known_not_editable":
                _fail(f"wrong exception propagated: {e.key!r}")
    finally:
        save_editor._detect = original_detect
        save_editor.try_recipe_battery = original_battery
        _cleanup(p)


def test_wrapper_skips_already_writable():
    original_detect = save_editor._detect
    original_battery = save_editor.try_recipe_battery
    sentinel = object()

    def _stub_detect(p, data, registry, game_dir, progress, engine, full_sweep,
                     cancel_token=None):
        return SaveDocument(path=p, format_name="Stub", engine="Stub",
                            fields=[SaveField(("x",), "x", "int", 1)],
                            read_only=False, _fmt=sentinel, _original=data,
                            _registry=registry)

    def _forbidden_battery(*a, **kw):
        _fail("try_recipe_battery must not run for an already-writable doc")

    save_editor._detect = _stub_detect
    save_editor.try_recipe_battery = _forbidden_battery
    p = _write_temp(b"anything")
    try:
        doc = open_save(p, try_recipes=True)
        if doc.read_only or doc._fmt is not sentinel:
            _fail("expected the stub's own writable doc back, untouched")
    finally:
        save_editor._detect = original_detect
        save_editor.try_recipe_battery = original_battery
        _cleanup(p)


def test_detect_source_never_mentions_recipes():
    src = inspect.getsource(save_editor._detect)
    for banned in ("RecipeFormat", "try_recipe_battery", "recipe_format", "recipes"):
        if banned in src:
            _fail(f"_detect's source mentions {banned!r} — recipe logic must "
                  f"stay outside it, see try_recipe_battery")


def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
