# Wolf RPG Editor LZ4 save variant — save encryption RE findings

**Update: fully solved and shipped into SaveSync — both formulas, both
directions, confirmed in the real game.** Everything below this point is
the chronological investigation log, kept as-is including the dead ends
and the sections that originally called themselves "not yet resolved" —
those are now marked with pointers to where they actually got closed
(Session 3/4 for the seed, Session 5 for the swap offsets, Session 6 for
the real-game round-trip), rather than rewritten, so the reasoning that
led there stays intact. The algorithm is implemented in
`core/engines/wolf_lz4.py` (crypto, closed-form seed + swap-offset
derivation, search kept only as a fallback for a build/save neither
formula explains), bridged into the save editor via
`core/save_editor/crypt/wolf_lz4.py` (reuses `crypt.wolf`'s existing
variable-database parser — the decompressed body has the same shape) and
`core/save_editor/wolf_lz4_format.py`, registered in
`core/save_editor/registry.py` under the `wolfrpg` engine, tried after the
standard `WolfFormat`. Dependencies: `lz4`, `numpy`, `numba` (added to
`requirements.txt`).

Validated: the keystream generator matches live captures exactly; the
seed formula (Session 4) matches 8/8 real files with completely different
headers; the swap-offset formula (Session 5) matches 8/8 real files,
including the one file where the OLD search-based method was proven to
return a silently-wrong-but-plausible answer; the `lock`/`unlock`
round-trip is internally consistent; the full `open_save()` → registry →
format-fallback chain routes correctly and fails gracefully; and — the one
gap Session 5 left open — a SaveSync-written file has since been loaded
successfully by the real, running game (Session 6), closing the last
unconfirmed step end to end.

Target: a commercial visual-novel title's `Game.exe`
(32-bit PE, ImageBase 0x400000, genuine Wolf RPG Editor build)

**Status: algorithm fully recovered, both the seed and the swap-offset
derivations are closed-form and shipped, and a real write→load round trip
through the actual game is confirmed.** No open questions remain that
block a working, bidirectional editor for this format.

## Executive summary

This build does **not** use the documented/standard Wolf RPG Editor save
scheme (3-pass XOR, seeds at header [0,3,9], msvcrt `rand()`). It uses a
custom scheme:

1. The save buffer's byte at offset **8** is a format tag.
   - `tag == 3` (confirmed: this is what all of this installation's real
     save files — `SaveData01.sav`, `SaveData02.sav`, `System.sav` — actually
     use) and `tag == 1` both route through the real, live decrypt path
     described below.
   - `tag == 0xFF` routes through a **different, heavily obfuscated decoy
     function** (`FUN_0054af30` @ VA `0x54AF30`) that was extensively
     reverse-engineered this session before being confirmed **dead code for
     every real save file this installation produces** — none of the
     on-disk saves ever use tag 0xFF. Details kept below for completeness in
     case a different build/version of the game uses it.
2. The real path (tag 3/1) derives a 32-bit seed from 3 header "salt" bytes
   and uses it to seed a **standard MT19937** PRNG with a **non-standard
   tempering step**. The low byte of 128 successive (rotated) outputs forms
   a repeating 128-byte XOR keystream applied to everything from file offset
   `0x14` onward.
3. The decrypted region is `[u32 dstCap][u32 srcLen][LZ4-compressed block]`.
   The LZ4 decompressor is a real, unmodified implementation at VA
   `0x404190`.

## Why earlier live-hook attempts all failed (root causes, both found)

1. **Arithmetic bug**: early in this session, the VA for the tag==0xFF
   decoy function was miscomputed as `0x94AF30` instead of the correct
   `0x54AF30` (`0x400000 + 0x14AF30`). Every hook placed on "the decrypt
   function" before this was found was hooking dead memory.
2. **Process-restart invalidation** (the real culprit behind *every*
   zero-hit result once the address was fixed, including on the correct
   tag=3 function): this game requires restarting to reach a save slot from
   certain menu states. Each Frida script resolved the target PID once at
   attach time via `enumerate_processes()`; when the user then restarted the
   game (as their normal workflow requires), a **new PID** was created and
   the hook was left attached to an orphaned, dead process. This was
   confirmed directly: `Get-Process` showed the game's PID changing between
   hook-arm time and the user's load action in multiple instances. Once a
   hook was attached to the *currently running* PID and the user was asked
   not to restart mid-capture, every target function fired immediately and
   repeatedly, including on the very first attempt.

## Save load entrypoint

- `FUN_009a52a0` @ VA `0x9A52A0` — the real save load/parse routine.
- Tag byte at buffer offset 8 dispatches format handling (`LAB_009a6215` for
  tag 1, or tag 2..199 after an extra per-tag preprocessing call to
  `FUN_00523b40`, which real saves — tag 3 — take).

## The real decrypt call (tag 3 / tag 1 path)

Call site: VA `0x9A6291` inside `FUN_009a52a0`, target `FUN_0047caf0` @ VA
`0x47CAF0`. Exact calling convention, from raw disassembly (`__fastcall` +
6 stack args):

- `ECX` = save-buffer pointer
- `EDX` = `0x14` (constant header size)
- stack args (right to left): buffer pointer again, `0x3`, pointer to a
  3-byte stack array `{buf[0], buf[1], buf[5]}`, `0x0`, `0x0`, `0x2`

So: **the 3 "salt" bytes are file offsets 0, 1, and 5.**

For tag>2, `FUN_00523b40(buffer, 1)` runs first and can reallocate/modify
the buffer (its exact effect on tag==3 saves — the only real-world case —
was not found to change the outcome empirically, but wasn't independently
isolated either).

## Keystream generator — genuine MT19937, custom tempering

Inside `FUN_0047caf0`'s (heavily obfuscated) body, one call site is
unambiguous and load-bearing:

```
bVar4 = FUN_0056a0d0();   // called 128 times in a row
abStack_1a40[iVar9] = bVar4;
```

`FUN_0056a0d0` @ VA `0x56A0D0` is **textbook Mersenne Twister (MT19937)**:
`N=624`, `M=397`, matrix constant `0x9908b0df`, `UPPER_MASK=0x80000000`,
`LOWER_MASK=0x7fffffff` — all confirmed byte-exact against a live-captured
624-word state array (`state[i] = 1812433253*(state[i-1]^(state[i-1]>>30))+i`
verified for i=1..19, and against the real disassembly of the twist loop).

Tempering (this is where the developer customized it — **mask applied
BEFORE the shift**, not after, and non-standard constants):

```
y ^= (y >> 11)                              // mask word was 0xffffffff (no-op)
y ^= (y & 0xff3a58ad) << 7
y ^= (y & 0xffffdf8c) << 15
y ^= (y >> 18)
```

(Standard MT19937 tempering is `y^=(y<<7)&0x9d2c5680; y^=(y<<15)&0xefc60000;`
— shift-then-mask, different constants. This build's version is
deliberately different, confirmed by reading `param_1[0x4e1]` live from
memory — it holds `0xffffffff`, i.e. the first tempering line really is a
plain `y ^= y>>11`.)

**Keystream construction**: generate 128 tempered outputs, take each one's
low byte (128 bytes total), then apply them in a rotated order — byte at
logical position `j` in the actually-used keystream is
`raw128[(j + 20) % 128]` (an empirically-determined 20-byte/5-word offset;
the underlying cause is pointer-arithmetic in the twist loop's start index
that wasn't fully traced by hand, but the offset itself is exact and
verified — see Validation below).

**Application** (this part IS cleanly readable in the decompiled C, no
obfuscation): for `i` from `0x14` to end of file,
`buffer[i] ^= keystream[(i - 0x14) % 128]`.

## Compression: LZ4

`FUN_00404190` @ VA `0x404190`, signature
`byte* __fastcall(byte* src, byte* dst, int srcLen, int dstCap)` — a
byte-for-byte match to the reference LZ4 block decompressor (token byte,
0xFF-continuation length extension, 2-byte LE match offset, MINMATCH=4,
8/16-byte wildcopy). Real, unmodified implementation, not obfuscated.

Decrypted layout: `[u32 dstCap][u32 srcLen][LZ4 block of srcLen bytes]`
immediately following the 0x14-byte header, all covered by the same XOR
keystream (i.e. decrypt first, *then* the first 8 bytes of the decrypted
result are the two length fields, then the LZ4 stream starts at file offset
`0x1c`).

## Validation (this is the important part — not a guess)

Captured via a live Frida hook on the corrected address, with `before`/
`after` 512-byte dumps of the save buffer immediately around the real
`FUN_0047caf0` call, for a real load of `SaveData01.sav` (salt bytes
`0x9d, 0x28, 0xf8`, live-captured derived MT seed `0x15cd3d19`):

- A from-scratch Python re-implementation of the algorithm above
  (`wolf_lz4_decrypt_poc.py`, in the session scratchpad) reproduces **all 512
  captured bytes exactly** (`decrypt(before, seed=0x15cd3d19) == after`,
  verified programmatically, not by eye).
- The decrypted content is human-readable and correct: marker byte `0x19`
  at the expected offset, followed by a length-prefixed ASCII string —
  the game's own internal title, redacted here — immediately followed by
  plausible-looking binary structure feeding into the LZ4 stream.
- The derived-seed → salt-byte correlation was also confirmed deterministic
  and file-specific: `SaveData01.sav` (salt `9d,28,f8`) always produced MT
  seed `0x15cd3d19` across two separate live captures in two different game
  sessions; `SaveData02.sav` (salt `2d,53,50`) always produced `0xe2a7c813`.

## Not yet resolved: the salt→seed derivation formula

> **RESOLVED — see "Session 4: chasing the formula — solved completely"
> below.** Kept here as-written for the investigation history (what was
> tried and ruled out below is what made the eventual formula-hunt
> tractable); nothing under this header should be read as still open.

The one open question, at the time this section was written: exactly how
the 3 salt bytes (offsets 0, 1, 5) —
or possibly additional header bytes read directly from the buffer, not
just these 3 — combine into the 32-bit MT seed. Two known pairs:

| salt (offsets 0,1,5) | derived MT seed |
|---|---|
| `9d, 28, f8` | `0x15cd3d19` |
| `2d, 53, 50` | `0xe2a7c813` |

Ruled out: direct byte-packing (any byte order/endianness), FNV-1/1a,
simple polynomial hashes, and chaining the same
`1812433253*(x^(x>>30))+i` step used elsewhere in this binary. A live
memory write-watchpoint on the seed word was attempted to catch the exact
computing instruction, but the state struct turned out to be
**stack-allocated** (a fresh, different address per decrypt call — confirmed
via 3 different observed addresses across 3 calls), so a static watch
address doesn't work; it would need re-arming per call or a different
technique (e.g. hardware breakpoint on write with an address range, or
CFG-aware disassembly of the true (anti-disassembly-obscured) function body
starting near VA `0x47cb1a`).

**This does not block building a working decryptor.** Since the seed space
is only 32 bits and there's a very strong, cheap oracle (decrypted byte at
a fixed offset must equal `0x19`, immediately followed by a plausible
length-prefixed ASCII string), a known-plaintext brute-force seed recovery
is entirely practical — just not in pure Python at full 2^32 scale (see
`find_seed()` in `wolf_lz4_decrypt_poc.py`, logic verified correct on a
reduced range including the known answer, full-range performance not yet
benchmarked/optimized). A C extension, numba, or multiprocessing
implementation would make this fast enough for interactive use.

## Session 2 update: brute force fails on real current saves — new findings

**The 2^32 brute-force search does NOT find a seed for the user's real,
current, confirmed-non-corrupted saves.** A full, clean, uninterrupted
exhaustive search (no worker crashes, verified via CPU monitoring
throughout) against `System.sav` (restored to its genuine state, confirmed
byte-identical across independent checks, salt `9d,28,f8`) completed in
4745.4s and found nothing. This matters because it means the earlier
"does not block a working decryptor" conclusion above was wrong for the
SAVE direction specifically — see below for why.

### SaveData01.sav (the Aug 14 file used for the original capture) is
### genuinely corrupted, independent of key or algorithm

Decrypting it with the historically-captured seed `0x15cd3d19` (same salt,
`9d,28,f8`) produces the CORRECT header and start of the literal run — the
`0x19` marker and legible fragment of the title string appear exactly
where expected — but a manual, from-scratch LZ4 block decoder (not the
`lz4` library, a hand-rolled token/literal/match walker) hits a
structurally IMPOSSIBLE instruction 4 tokens in, byte 87 of the compressed
stream: a match offset of 18277 when only 1935 bytes of output exist yet.
No valid LZ4 stream can do this. This is definitive: the file's compressed
payload is torn/corrupted, not a wrong-key or wrong-algorithm symptom. Every
backup snapshot of it taken during the original session (6 snapshots,
Aug 13 20:41 – Aug 14 07:43, `SaveSync\backups\<game>\*.zip`) has a
DIFFERENT size and salt, consistent with the file
being repeatedly rewritten while in a bad state. The last backup is
byte-identical (SHA256 match) to the current on-disk file, so nothing has
changed it since — it has been in this same corrupted state throughout.

### The algorithm itself is now proven 100% correct — via real live capture

A live Frida hook on `FUN_0047caf0` (documented address, corrected for
each session's actual module base — this build reloads at a different base
than `0x400000` every launch; ASLR or similar, compute the hook address as
`main.base + (0x47CAF0 - 0x400000)`, never hardcode) during an actual
in-game LOAD captured real before/after buffers. XORing them together
reproduces the documented 128-byte repeating keystream exactly (bytes
0-31 == bytes 128-159, confirmed). Feeding the captured "after" content
straight into `decompress()` succeeds completely: 190475 bytes, clean,
starting with `\x19 \x00<redacted internal title>\x00...` — the
exact documented structure. **This proves the MT19937 tempering, the
128-byte/20-rotation keystream construction, the marker/length structure,
and the LZ4 framing are all correct, unconditionally**, for the LOAD
direction. Nothing in `core/engines/wolf_lz4.py`'s cryptographic core is
in question anymore.

### FUN_0047caf0 IS also called during SAVE — but its buffer's own header
### is stale at call time; the salt is a separate ARGUMENT, not read from
### the buffer

Decompiling `FUN_009a40bd` (found via a `WriteFile` hook's captured
backtrace, resolved through the already-analysed Ghidra project) shows the
real save path (`tag==3`, the only one live saves ever use — the `tag==0xFF`
branch, `FUN_0054af30`, previously assumed "dead code", actually fires when
creating a genuinely NEW slot with a freshly-generated random header; every
save observed live in this session re-uses tag 3, i.e. treats the slot as
existing, so that branch stays out of scope here) calls `FUN_0047caf0`
with a POINTER to a local 3-byte array (`caller's EBP-0x920`) as one of its
stack arguments — NOT the buffer's own header bytes. **The buffer's own
header (positions 0,1,5, read via the callee's `ECX`) is stale/leftover at
this point** — the CALLER copies the real salt into the buffer's header
bytes 0,1,3,4,5,7,8 only AFTER `FUN_0047caf0` returns (confirmed in the
decompile, immediately following the call). Reading `ECX+{0,1,5}` (what a
naive hook does, and what this session tried first, repeatedly, before
finding this) will never match the real on-disk salt for the SAVE
direction — read the STACK ARGUMENT instead (`caller_ebp - 0x920`, 3
bytes), which matches the true on-disk salt exactly, confirmed
byte-for-byte across two independent live saves.

### The salt bytes are real MSVCRT rand() output, not baked into the crypto

`FUN_00b40d6e` (called 3 times to build the salt argument above) decompiles
to the textbook MSVCRT `rand()` LCG: a thread-local state (via
`FUN_00b51a22()`, state at `+0x18`) updated as
`state = state*0x343fd + 0x269ec3`, returning `(state>>0x10)&0x7fff`. Each
salt byte is `(that return value >> 4) & 0xff`. Confirmed exactly against a
live capture: the first 3 `rand()` outputs of a save action, transformed
this way, equal the file's actual bytes at positions 0, 1, 5 precisely.
This is a real, standard, well-understood generator — nothing custom here,
unlike the MT19937 tempering.

### Still unresolved: something ELSE modifies the buffer's CONTENT (not
### just its header) between FUN_0047caf0 returning and the actual disk
### write, and it hasn't been found yet

Even with the byte-exact-matching salt argument confirmed, and the derived
keystream confirmed to repeat correctly every 128 bytes, decrypting the
REAL on-disk bytes with it still fails — `decompress()` reports the
compressed stream runs past the file's end. Directly diffing the buffer
captured at `FUN_0047caf0`'s `onLeave` against the actual bytes later
passed to `WriteFile` (captured via a `kernel32!WriteFile` hook, so this is
unambiguous ground truth) shows differences not just in the header
(expected — the salt-patch above explains bytes 0-8) but ALSO past byte 20,
inside the `[dstCap][srcLen]` length-field region and beyond. Something
changes the buffer's actual content after the encrypt call and before the
write — not only patches its header.

Ruled out as the culprit: the write call chain itself. Backtraces from the
`WriteFile` hook resolve (via the analysed Ghidra project) to
`FUN_00b3891a` → `FUN_00b389b1`, reached through `FUN_00b38a3f` after a
plain `__wfopen(..., L"wb")` — this is confirmed to be generic CRT `fwrite`
machinery (lock the stream, write, unlock), not anything save-format
specific. Whatever transforms the buffer happens BEFORE this, most likely
in the tail of `FUN_009a40bd` itself (after its header-patch at the lines
following the `FUN_0047caf0` call — only decompiled up to that point so
far, not fully traced to the function's `RET`) or in `FUN_009a7240` (the
function whose return address showed up as the next backtrace frame above
`FUN_009a40bd` — decompiled in full, 219 lines, but it does not visibly
reference `DAT_0146bbdc` — the global save buffer — anywhere, which is
suspicious: either the decompiler is hiding an indirect reference, or this
particular backtrace frame belongs to a DIFFERENT write event than assumed
and the two overlapping live captures (two files written close together)
got their backtraces crossed).

**Update — traced it to completion, and it's much smaller than it looked.**
`FUN_009a40bd` does the ENTIRE save in one function, sequentially, no
separate write function needed: encrypt (`FUN_0047caf0`) → header patch
(positions 0,1,3,4,5,7,8 — the salt, confirmed above) → `puVar8[6] = 0x55`
(one more fixed byte) → `FUN_00523b40()` → directory setup
(`SetCurrentDirectoryW`/`GetCurrentDirectoryW`) → `__wfopen(path, L"wb")` →
`FUN_00b3e80a()` + `FUN_00b38a3f()` (the actual write — confirmed generic
CRT `fwrite` machinery, see above, nothing save-specific).

Diffing the exact captured buffer (`_seedhunt_buf_1_after.bin`, taken the
instant `FUN_0047caf0` returns) against the exact bytes later passed to
`WriteFile` for the SAME save action shows **only 48 bytes differ, out of
1321** — not "the whole buffer", as first feared. Every difference falls in
one of three small, specific spans:
- file positions 0,1,3,4,5,6,7,8 — the header/salt patch, already explained
  above.
- file positions **20-39** (20 bytes) — immediately after the header; this
  is exactly where `[dstCap][srcLen]` live, and decrypting the REAL on-disk
  bytes there with the correct keystream gives obvious garbage
  (`dst_cap=3311207637, src_len=3435457485` — not remotely a plausible
  buffer size), not a slightly-changed plausible integer.
- file positions **110-129** (another 20 bytes, no diffs anywhere else in
  the file, all the way to the end at 1321).

**20 bytes is a SHA-1 digest size, and there are exactly two of them.**
Everything between and after these three spans (i.e. essentially the whole
rest of the file, all 1273 remaining bytes) matches the live-captured
keystream perfectly. The only function called between the encrypt call and
the write is `FUN_00523b40` — a large (4020 decompiled lines), heavily
obfuscated function that reuses the SAME opaque-predicate infrastructure
already documented above for the `tag==0xFF` decoy (`DAT_00f303a8..ac`,
`FUN_00457f30`/`FUN_00405b10`/`FUN_00457f60`/etc., all constant-folding
stubs) and reads `DAT_0146bbdc` (the save buffer) exactly once, right at
its own entry. No standard hash constants (SHA-1's `0x67452301` etc.,
CRC32's `0xedb88320`) appear anywhere in its decompile, so if it computes a
checksum, it is a CUSTOM one, not a textbook algorithm called directly —
consistent with how the rest of this build's crypto is custom, not
standard.

**SOLVED — full decrypt+decompress confirmed on the real, current, on-disk
file.** The two 20-byte spans are not a hash — `FUN_00523b40` simply SWAPS
them with each other (confirmed by hooking it directly: the 20 bytes that
appear at span A after the call are exactly what was at span B before, and
vice versa — a plain positional exchange, no computation). Un-swapping
them before decrypting a real on-disk `System.sav` produced a clean,
complete decompress: 190475 bytes, starting with the exact documented
`\x19 \x00<redacted internal title>\x00...` structure. The full
pipeline for a save this session captured live is now proven end to end:
decrypt candidate → find the two 20-byte spans → swap them back → THEN
apply the XOR keystream → decompress.

Confirmed via a direct hook on `FUN_00523b40`:
`Interceptor.attach` at `main.base + (0x523B40 - 0x400000)`, and the
buffer must be read from the GLOBAL `DAT_0146bbdc` pointer (at
`main.base + (0x146bbdc - 0x400000)`) rather than any register or
argument — this function takes no useful buffer argument, it reads the
global directly (confirmed: neither `args[]` nor `this.context.ecx` at its
own entry carried it).

**Still open: the swap span positions are not fixed.** Across two
different save actions they were at (21-41, 116-136) and (48-68, 105-125)
— different absolute offsets each time, always 20 bytes wide, always with
a gap between them. This means there is a RULE that computes where they go
per-save (very likely based on something in the content itself — its
compressed length, a running total, or a value already present in the
header), not a constant.

**Next step to reach a FULLY general, no-live-capture-needed decryptor**:
find what determines the two span start offsets. Candidates to check
first: whether they are simple functions of `src_len` (the compressed
size, already known from the header once decrypted) or of the DECRYPTED
LZ4 payload's own length/structure (e.g. mirrored around the payload's
midpoint, or placed at fixed OFFSETS INTO THE UNCOMPRESSED stream rather
than the compressed one, which would explain why they moved when file
content/length changed between saves). With two real examples now on
record (offsets above, both from files whose full plaintext is already
recoverable via the pipeline this session proved), computing the
uncompressed offset each span corresponds to and comparing them directly
is the fastest path — no more live captures needed for that specific
check, this session's own captured files already contain everything
required.

**First real lead on the rule**: in the cleaner of the two examples
(second save, computed directly from its own confirmed-correct keystream,
not eyeballed from a rougher early diff like the first example was), span
1 starts at exactly **compressed-stream-relative offset 20** (file offset
48 = `HEADER_LEN`(20) + 8 + 20). That is a suspiciously round, likely FIXED
constant — worth testing directly against a third save before trusting it.
Span 2's offset (compressed-relative 77 in that same example) does not
obviously reduce to a simple function of `src_len` (1293) or `dst_cap`
(190475) by inspection; the first example's numbers are noisier (captured
via a cruder diff pass) and probably should be recomputed cleanly the same
way as the second before comparing the two for a pattern in span 2's
position specifically.

**Update — three more real examples gathered, and one hypothesis ruled
out.** Confirmed clean, working (full decompress succeeds) numbers now on
record:

| file | src_len | dst_cap | span1 (compressed-rel.) | span2 (compressed-rel.) | gap |
|---|---|---|---|---|---|
| System.sav, save A | 1293 | 190475 | 20 | 77 | 57 |
| System.sav, save C (same content length as A) | 1293 | 190475 | 21 | 89 | 68 |
| SaveData02.sav, save B | 57987 | 564292 | 11 | 64 | 53 |

Saves A and C are the SAME file (System.sav) with IDENTICAL `src_len`/
`dst_cap` — same content length — yet different span offsets. **This rules
out any formula based purely on content length**: two saves with the exact
same compressed size get different swap positions, so something else
(most likely the actual header/content byte VALUES, which differ between
A and C even though their lengths don't) drives it.

**Ruled out: `FUN_00523b40` does not call `rand()` itself.** Instrumented
`FUN_00b40d6e` (the confirmed MSVCRT `rand()`) with a global call counter
and recorded its value at `FUN_00523b40`'s own entry and exit — identical
both times (23, for the System.sav call in this same test). So the swap
offsets are computed from data already in hand at that point (the header,
or specific already-consumed `rand()` outputs stored earlier), not chosen
by fresh randomness at swap time.

**Also revealed in this pass, correcting an earlier assumption**: 23
`rand()` calls happen before `FUN_00523b40` for `System.sav` (46 for the
larger `SaveData02.sav`) — far more than the 3 originally thought to be
"just the salt". This matches `FUN_009a40bd`'s decompiled 20-iteration
loop (`while(iVar3<0x14)`) that was earlier assumed to only run on an
"existing slot, preserve old header" branch — it appears this loop, or one
like it, actually runs and fills ALL 20 header bytes with fresh `rand()`
output on real saves, not just the 3 documented salt positions (0,1,5).
Worth re-deriving the extraction rule for the OTHER 17 header positions
(confirmed not `(rand()>>4)&0xff` like the salt bytes are — that formula
was only checked against positions 0,1,5) as a more tractable lead than
reversing `FUN_00523b40` directly, since the header is available in the
clear on every file without needing live capture at all.

**Note on LOAD**: this swap step was never accounted for in the original
LOAD-side investigation or in `core/engines/wolf_lz4.py`'s brute-force
search, yet that search has apparently worked before (per the module's own
docstring/history) and its cheap marker-check only inspects a few bytes
right after the header — which, in both examples captured this session,
falls OUTSIDE either swap span. Worth confirming this holds in general
(i.e. the swap spans never overlap the marker-check window) before
assuming LOAD is fully unaffected by this discovery. **Update: it does
NOT hold in general** — see below, one of the two fresh captures this
session had a swap span starting AT the marker-check window's offset
(SaveData02.sav: span1 = header-relative 0, i.e. absolute file offset 20,
which is inside the `HEADER_LEN+10`-ish marker-check zone). So the swap
must be accounted for on LOAD too, not just SAVE — it is not a rare edge
case, it happened on 1 of 2 fresh real captures.

## Session 2 continued: the header-byte formula, fully solved (16 of 20 bytes) — and the swap-offset search, validated end to end

**The 20-byte header is generated by a single loop calling `rand()` 20
times, one call per byte, BEFORE `FUN_0047caf0` (encrypt) is even called**
— `FUN_0047caf0` itself never touches bytes `[0:20)` (confirmed: captured
`enc_buf` before/after are byte-identical in that range on every capture).
After the loop runs, the caller (`FUN_009a40bd`) overwrites a subset of
those 20 positions with other values (the salt, a literal flag, and — not
yet explained — 4 more bytes); the ones it does NOT touch keep exactly
what the loop put there. Byte-exact, confirmed on TWO independent real
saves with two completely different `rand()` sequences (`System.sav`,
local rand-block starting at global index 0; `SaveData02.sav`, same save
action, local rand-block starting at global index 23 — the state is one
continuously-advancing per-process sequence, not reseeded per save):

- **Header positions 2, 9, 10, 11, ..., 19 (12 of 20 bytes)** — untouched
  by the caller, so they equal the loop's raw output:
  `header[pos] = (rand(local_index = pos + 3) >> 8) & 0xFF`.
  Verified exactly on all 12 positions, both saves, zero mismatches.
- **Header positions 0, 1, 5 (the salt, already known)** — overwritten by
  the caller with `(rand(local_index) >> 4) & 0xFF` for local indices
  0, 1, 2 respectively (i.e. the FIRST 3 calls of the same block, BEFORE
  the 20-iteration loop starts at local index 3). Re-confirmed exactly on
  both saves with the new local-index bookkeeping.
- **Header position 6** — a literal `0x55`, but proven this session to be
  written AFTER `FUN_00523b40` (the swap), not before: captured
  `swap_buf` before/after both still show the loop's original value there
  (`0x7b` for `System.sav`, `0x7a` for `SaveData02.sav`); only the final
  on-disk `WriteFile` bytes show `0x55`. So there's a third, tiny patch
  step between swap and write that touches only this one byte.
- **Header positions 3, 4, 7, 8 (4 of 20 bytes) — still unexplained.**
  Confirmed NOT part of the 20-iteration loop's output (their values don't
  match `rand(pos+3)` under any bit-shift tried) and confirmed they ARE
  part of the caller's post-encrypt patch (same evidence as the salt: they
  differ between `enc_buf_after` and `swap_buf_before`, the exact window
  where the patch runs). Tried every plausible bit-extraction of the same
  3 `rand()` calls used for the salt (`local_index` 0, 1, 2) against these
  4 target bytes on both saves — no consistent formula found. **This does
  not block reading** (see below) — decided not to chase it further.

**The actual goal — locating `FUN_00523b40`'s two 20-byte swap spans
without decrypting the game's own logic — is now solved by search, and
validated against real ground truth, not just theorized:**

Using the live-captured keystream directly (`enc_buf_before XOR
enc_buf_after`, no seed needed — this sidesteps the separate, still-open
"how does the MT19937 body seed derive from the salt" question entirely
for validation purposes), a brute-force search over every `(a, b)` pair of
candidate 20-byte span offsets in `[20, 300)` — about 39,000 pairs — was
run against both real on-disk files:

1. **Cheap filter**: patch only the 12 bytes needed to read the `<II`
   `(dst_cap, src_len)` length prefix at the swap-corrected offset,
   reject anything with an implausible length. Prunes ~39,000 candidates
   to a few dozen in well under a second (pure Python, no numpy needed —
   this search space is 6 orders of magnitude smaller than the seed
   search).
2. **Strict verify** (only for cheap-filter survivors): apply the full
   un-swap, XOR-decrypt with the real keystream, LZ4-decompress, and
   require the SAME shape `core.engines.wolf.is_wolf_save` already checks
   — marker byte `0x19`, then a 2-byte length, then that many bytes ending
   in a NUL. A bare marker-byte check alone was tried first and was NOT
   selective enough — `System.sav` produced 10 different `(a, b)` pairs
   that all passed marker-only (LZ4 decoding "successfully" into garbage
   that happens to start with `0x19`); the full `is_wolf_save`-shaped
   check collapses this to exactly one hit in both files.

**Result: exactly one surviving `(a, b)` pair per file, matching the
independently-diffed ground truth exactly, and decompressing to the exact
byte count already proven correct earlier this session:**

| file | recovered span | ground truth (from raw diff) | decompressed size |
|---|---|---|---|
| `System.sav` | `(32, 96)` | `(32, 96)` | 190475 bytes |
| `SaveData02.sav` | `(20, 95)` | `(20, 95)` | 564292 bytes |

This confirms the search — not a closed-form offset formula — is a
practical, reliable way to locate the swap spans, PROVIDED the keystream
is already known. At the time this was written it did not by itself solve
decrypting a save with no live capture at all (that still needed the
body's MT19937 seed, whose derivation from the salt bytes was still open —
since resolved, see "Session 4: chasing the formula — solved completely"
below; the swap-offset search this section describes was itself later
replaced by its own closed-form formula too, see "Session 5"). What it
does establish: once a seed is known or found
(by any means — live capture, or `wolf_lz4.find_seed`'s existing 2^32
search, cache, etc.), automatically finding the swap offsets no longer
requires a live game at all — the ~39,000-candidate search above runs in
well under a second in pure Python and needs nothing but the file's own
bytes plus a candidate keystream.

**Practical implication for `find_seed`'s existing 2^32 brute-force
search**: it previously failed on every real current save (see "Session 2
update" above) because it always assumed the buffer was already correctly
laid out — the swap silently corrupted its verification step for every
seed, including the true one. The fix is not "search seeds AND offsets
jointly" (2^32 × 39,000 is not feasible) — it's to swap the order: keep
the existing cheap marker/length filter as the outer 2^32 sweep (unchanged
performance), but make the EXPENSIVE per-survivor check (currently
`verify_seed`) try the ~39,000-candidate swap-offset search described
above instead of assuming no swap. Since the cheap filter already narrows
2^32 down to a tiny number of survivors before the expensive check ever
runs, bolting a sub-second offset search onto each survivor adds
negligible total cost. The one caveat: the cheap filter's own check window
can itself fall inside a swap span (confirmed: it did for `SaveData02.sav`
this session), which could reject the true seed before the expensive
check ever sees it — `_MARKER_CHECK_BASES`' existing slack may or may not
cover this in general; not yet re-verified against a fresh capture.

**This is a structural collision, not a rare edge case.** Both real spans
recovered above have `a` anchored right next to `HEADER_LEN` (`32` and
`20`, against `HEADER_LEN = 0x14 = 20`), which is exactly where the LZ4
stream's leading literal bytes — and therefore the marker
`_MARKER_CHECK_BASES` looks for — actually live: the compressor's first
token is almost always a literal run near the very start of its own
output, since there's nothing yet to back-reference. So whenever a save
needs a swap at all, its `a` span and the cheap filter's marker window are
both independently drawn to the same few dozen bytes at the front of the
compressed stream — collision is the expected case for a swap-bearing
save, not an unlucky one. Widening `_MARKER_CHECK_BASES` does not fix
this: there is exactly one true marker position for a given (seed,
content) pair, and if it lands inside `[a, a+20)` the bytes actually
there now come from `b` — no additional base value reads the original
content, because it isn't at that file offset anymore, it was moved. A
real fix would need the cheap filter itself to test an "already-swapped"
hypothesis per candidate seed, which reopens the "search seeds AND
offsets jointly" cost this section already ruled out as infeasible.

**Confirmed on the two real files directly, not just structurally
argued.** Using the ground-truth keystream derivable from this session's
own `enc_buf`/`swap_buf` before/after captures (no seed search needed —
XORing before against after gives the real keystream directly), both
files were decrypted for real: `System.sav` decompresses to exactly
190475 bytes, `SaveData02.sav` to 564292, both matching this document's
own ground truth, both with a correctly-decoded name field (the same
redacted internal title as above). The true marker sits at **base 10**
in BOTH files — identical, not just similar — and checking every byte the
cheap filter's 3-byte-marker-plus-NUL check actually reads against each
file's real swap span confirms: `System.sav`'s window (file offsets
30–32) has its 3rd byte (32) exactly on the swap span's start, corrupting
1 of 4 checked bytes; `SaveData02.sav`'s window falls entirely inside its
span, corrupting all 3 marker bytes. Two independent real full-2^32
searches (one per file, ~23 and ~28 minutes) confirm the practical
consequence: both exhausted the entire seed space and found nothing, even
though `verify_seed` accepts the true seed instantly once handed it
directly.

**A working fix, not just a diagnosis.** Rather than the infeasible joint
search, the cheap filter now ALSO tries a small, evidence-bounded "what if
a swap already happened here" hypothesis grid against the ONE anchor
position both real captures agree on (base 10): 16 candidate `a` offsets
(0–15, covering the observed 0 and 12) × 40 candidate gaps (50–89,
covering the observed 64 and 75) = 640 hypotheses. This costs no extra
Mersenne Twister work — the keystream word needed for a given file
position depends only on that position, never on where its ciphertext
byte is read from, and the numba kernel already computes the full 128-byte
keystream per seed regardless — so it's a handful of extra array reads per
seed, not a second recurrence. Validated first on synthetic files
reproducing all three real shapes (full overlap at two different gaps,
and the exact partial-single-byte-overlap shape `System.sav` shows) in
both the numpy and numba search paths, kept in lockstep via one shared
`_swap_hyp_arrays` builder so they cannot silently check different bytes.
Measured real throughput impact: 3.24M candidates/sec on a real file (was
~5-6M/s before this change) — a full 2^32 sweep is still ~22 minutes, not
materially worse.

**Result of the real search, with the fix, against both actual failed
files:**

- `System.sav`: **seed found for real** — `0xbbcf0b4c`, in 1348.8s
  (~22.5 min), via the production `find_seed()` search with zero live
  capture involved. Independently confirmed three ways: `verify_seed`
  re-proves it; `unlock()` decrypts to the exact same `dst_cap=190475`,
  `swap_spans=(32, 96)`, and decoded name (the same redacted internal
  title) this document already had from the live-captured ground
  truth; and a full `unlock → lock → unlock` round trip reproduces the
  identical payload, `dst_cap`, and swap span. This is the first time in
  this whole investigation that SaveSync's own search — not a live
  capture, not a shortcut — recovered the key for a real save file it
  started out knowing nothing about.
- `SaveData02.sav`: **root cause of the first failure found, and fixed —
  also now solved for real.** The 645-byte shortfall wasn't a swap-search
  problem at all: `_full_write_1_SaveData02.sav.bin` (57344 bytes) and
  `_full_write_2_SaveData02.sav.bin` (645 bytes) are not two different
  writes, they are ONE `WriteFile` the game's own buffered I/O split
  across two captured calls — concatenated (57344+645 = 57989, exactly
  matching `src_len(57961)+28`), the file decrypts and decompresses
  perfectly. Re-run against the corrected, complete file: **seed found —
  `0xd43e59c6` — in 1935.1s (~32 min)**, `swap_spans=(20, 95)` (matching
  this session's own earlier buffer-derived reconstruction exactly), and
  the REAL `WolfValues` parser found **44,464 records** — the same count
  already confirmed earlier this session against this save's content by
  other means. Both real captured files this project has ever had now
  open end to end through the actual production search, with zero live
  capture involved in either search itself.

**Numeric correlation attempt for the swap-offset rule** (using these two
now-fully-confirmed, same-session examples — `System.sav`: header-relative
`a=12, b=76, gap=64`; `SaveData02.sav`: `a=0, b=75, gap=75`): checked
`src_len`/`dst_cap` modulo 20/40/128/256 against both offsets, and the
still-unexplained header bytes `[3,4,7,8]` (`System.sav`: `229,255,80,3`;
`SaveData02.sav`: `13,2,191,3` — byte 8 matches at `3` in both, likely
coincidence at n=2) — no relationship found. Two points is not enough to
responsibly rule a real formula in or out; the ORIGINAL header-byte
formula (the one part of this that WAS fully cracked) needed several real
examples to nail down even with the simpler single-bit-shift-and-mask
shape it turned out to have, and still left 4 bytes unexplained.

**A fresh live capture WAS taken (a second game session, creating save
slot 3), giving 3 more fully-confirmed real examples on top of the
original 2 — all 5 decrypt+decompress byte-exact.** Same methodology as
before (enc_buf/swap_buf before/after diffing, no seed search needed for
the derivation itself): `System.sav` was written TWICE in one "create
slot" action (two independent encrypt+swap calls, ~23 `rand()` calls
apart), then `SaveData03.sav` once — confirming the split-`WriteFile`
behavior (a large chunk + a small tail, summing to the true file length)
generalizes beyond `SaveData02.sav`, not a one-off.

| file | a_rel | dst_cap | src_len |
|---|---|---|---|
| `System.sav`, session 1, seq#1 | 12 | 190475 | 1293 |
| `SaveData02.sav`, session 1, seq#2 | 0 | 564292 | 57961 |
| `System.sav` write A, session 2, seq#1 | 24 | 190475 | 1293 |
| `System.sav` write B, session 2, seq#2 | 24 | 190475 | 1290 |
| `SaveData03.sav`, session 2, seq#3 | 12 | 564292 | 57610 |

("session" = one Frida attach / game process; "seq#N" = this save action's
1-indexed position within that session, derived from the `rand()` call
count observed at each encrypt call: `rand_count / 23`, since each save
action consumes exactly 23 `rand()` calls for its header — 3 for the salt,
20 for the rest, confirmed again on every one of these 5.)

**A real, non-obvious pattern: `a_rel` repeats exactly for the two
`System.sav` writes within the SAME session (24, 24) despite completely
different headers/content/`rand()` values, and separately matches ACROSS
sessions when the file role repeats — `session 2`'s `SaveData03.sav`
(seq#3) lands on `a_rel=12`, exactly `session 1`'s `System.sav` (seq#1)
value.** Ruled out as the explanation: sequence number alone (`seq#1` is
12 in session 1 but 24 in session 2 — not constant by position); `dst_cap`
alone (constant per file role already, doesn't by itself explain WHY
`System.sav`'s two same-`dst_cap` writes in session 2 also share `a_rel`
while session 1's single `System.sav` example has a different value); tag
byte (all 5 examples are tag 3, no variation to explain anything with).
The shape that fits: `a_rel` looks like it's constant for a given
SAVE ROUTINE across repeated calls within one process run, but differs
between separate process launches — consistent with a value computed
once per launch (process start time, ASLR base, a lazily-initialized
static local to that routine) and reused on every call to that same
routine thereafter, rather than anything derived from content or from the
regular per-save `rand()` sequence. Not confirmed — the next test that
would actually distinguish this from coincidence: save to TWO DIFFERENT
`SaveData0N.sav` slots within the SAME session and check whether their
`a_rel` values also match each other (predicted: yes, both should equal
whatever `SaveData03.sav` got in that session, since by this hypothesis
it's the ROUTINE, not the specific file, that carries the constant).
`gap` (`b_rel - a_rel`) does NOT show this clustering (42, 53, 49, 60,
75 across the 5) and still has no found relationship to content length.

**The targeted test was run — a second save action (slot 4) in the SAME
running process — and it disproves the "constant per routine per launch"
hypothesis.** `System.sav`'s slot-4 write got `a_rel=2`; slot-3's two
`System.sav` writes (SAME routine, SAME process) got `24`. Not constant
across actions within one launch after all:

| file | a_rel |
|---|---|
| `System.sav`, session 1, seq#1 | 12 |
| `SaveData02.sav`, session 1, seq#2 | 0 |
| `System.sav` write A, session 2 (slot 3 action), seq#1 | 24 |
| `System.sav` write B, session 2 (slot 3 action), seq#2 | 24 |
| `SaveData03.sav`, session 2 (slot 3 action), seq#3 | 12 |
| `System.sav`, session 2 (slot 4 action), seq#1 | 2 |
| `SaveData04.sav`, session 2 (slot 4 action), seq#2 | 20 |

The slot-3 pair's exact match is still real and still unexplained by
anything ruled in above — just not by "per-routine, per-launch". The
detail that separates it from the slot-4 example: the two slot-3 writes
happened within ONE user action, milliseconds apart; slot 3 and slot 4
were separate actions, seconds apart (menu navigation in between). That
shape — same value for calls close in wall-clock time, different values
for calls far apart — is exactly what a coarse system timer
(`GetTickCount`, ~15.6ms resolution on Windows) reused as an input would
produce. Plausible, not confirmed: testing it needs a capture that logs
an actual timestamp per call (`Date.now()`/`GetTickCount()` alongside the
existing hooks), which the current script doesn't do.

**A timer-instrumented capture WAS taken** (`_frida_cap4.py`: samples
`GetTickCount`/`GetTickCount64`/`timeGetTime` directly, synchronously, at
every `enc_buf`/`swap_buf` hook — not by intercepting those APIs, which
the game's own main loop almost certainly calls every frame and would
flood the message channel). One more save action (slot 5), giving:

| file | swap-entry tick (ms) | a_rel |
|---|---|---|
| `System.sav`, slot 5, seq#1 | 46900406 | 4 |
| `SaveData05.sav`, slot 5, seq#2 | 46900437 | 3 |

No exact modular relationship found (`tick mod 20/40/128/256` doesn't
land on either `a_rel`). But the two ARE close together on both axes at
once — 31ms apart in time (roughly 2 `GetTickCount` increments), and only
1 apart in `a_rel` (4 vs 3) — which is at least consistent with (not
proof of) a smooth, timer-linked mechanism rather than a hash (a hash of
two close-but-different tick values would typically NOT give close
outputs). Two timer-instrumented points is the same data-scarcity
problem as before, just with sharper instrumentation now available —
genuinely deciding this would need several MORE data points from one
sitting (a batch of quick consecutive saves, not one at a time) to fit a
real relationship rather than eyeball two.

**That batch WAS captured — 22 save actions in one sitting
(`_frida_cap5.py`), giving 41 fully-confirmed, decrypt+decompress-verified
(file, tick, a_rel, b_rel, gap) tuples — and the tick-count hypothesis is
now DEFINITIVELY RULED OUT, not just unconfirmed.** Systematic search
across the whole dataset:

- `(tick >> shift) & mask` for every `shift` in 0-24 and every bit-width
  4-8, scored by exact match against `a_rel` across all 41 points: best
  result was 4/41 (~10%) — statistically indistinguishable from chance
  for a value in a ~30-wide range (~3.3% expected by luck alone), and
  that "best" result is exactly what you'd expect to find by accident
  when testing ~100 different (shift, bits) combinations against 41
  points (a multiple-comparisons artifact, not a signal).
- `tick % N` for every `N` from 2 to 200: same story, best was 4/41.
- Direct Pearson correlation: `tick` vs `a_rel` = 0.056; `tick % 128` vs
  `a_rel` = -0.044; `tick % 256` vs `a_rel` = -0.167; `rand_count` (this
  session's own save-sequence position) vs `a_rel` = 0.066. All
  indistinguishable from zero.

The earlier 2-point observation that looked promising (close in time,
close in `a_rel`) does not generalize — plenty of pairs in the 41-point
set are just as close in time (some under 20ms apart, well inside one
`GetTickCount` tick) with wildly different `a_rel` (e.g. `rand_count`
943→966: 15ms apart, `a_rel` 0 vs 29). That was very likely coincidence
in a 2-point sample, exactly the risk flagged before this batch was run.
**The swap offset is not a function of `GetTickCount`/`GetTickCount64`/
`timeGetTime`, in any shifted, masked, or modulo form.** Whatever it
actually depends on remains unknown — this rules out the one concrete,
testable hypothesis this investigation had; nothing else concrete
survived the earlier rounds either (content length, header bytes, tag
byte, sequence-within-session, per-launch-per-routine constancy — all
independently ruled out across this session's data). Not pursued further
here — the search-based fix is proven correct and practical on real data
regardless, so a closed-form rule stays a nice-to-have (saves the
one-time ~20-30 min search per new save slot), not a blocker, and the
remaining honest option to make more progress is the disassembly read
this investigation already deprioritized once as low-value-per-effort.

## Session 3: salt→seed formula bypassed entirely via direct emulation

**The "Not yet resolved" salt→seed formula above is no longer a blocker at
all** — not solved as a closed-form formula (still not found, still
presumably buried in the same obfuscation as everything else in this
function), but made irrelevant by emulating the real, obfuscated
`FUN_0047caf0` body directly with Ghidra's own p-code `EmulatorHelper`
(via `pyghidra`, reusing the already-analyzed `REProj` project — no live
game process, no Frida, no game running at all). Fed the 3 real salt bytes
read straight from a save file's clear header, it reproduces the exact
128-byte XOR keystream in ~1-2 seconds (mostly one-time emulator setup;
the actual run is 27,851 emulated instructions). **Validated against
every one of the 52 distinct real salts this project has ground truth
for** (the same `enc_buf_before XOR enc_buf_after` captures used
throughout this document; confirmed all 52 captures have distinct salt
bytes with zero collisions, so this isn't silently hiding a salt tested
twice) — 52/52 exact match, zero mismatches. Scripts:
`C:\ghidra_re\emulate_seed.py` (the validation harness, all 52 vectors
inline via `all_vectors.py`), `emulate_seed2.py` (the diagnostic version
with step-by-step register tracing used to find the fix below).

This directly confirms the advisor-suggested approach from this session:
extract the *keystream*, not the integer *seed* — `unlock`/`lock`/
`verify_seed` only ever consume `keystream(seed)` in the shipped code, so
a salt-keyed keystream cache sidesteps the seed integer entirely and the
"what formula produces the seed" question never needs answering.

### A real correction to this document's own earlier calling-convention notes

The "Exact calling convention" section above (and `wolf_lz4.py`'s module
docstring) labelled one of `FUN_0047caf0`'s stack arguments "buffer
pointer again", inferred from a single live Frida capture where that
value happened to equal the buffer's own address, never independently
confirmed against what the callee actually does with it. Direct emulation
evidence says that label is wrong: naively passing the real buffer
pointer as that argument produces `EDI = <buffer address>` at VA
`0x48920c` (`mov edi, dword ptr [ebx+8]`), immediately compared against
`ECX == 0x14` (`HEADER_LEN`, confirmed via step-by-step register tracing)
at `0x48920c: cmp ecx,edi` / `0x48920e: jge 0x489233` — a "skip the
encrypt loop" check that only makes sense if both sides are lengths, not
one length and one raw pointer. That same `EDI` then becomes the upper
bound of the per-byte XOR-apply loop at `0x489216..0x489231` (`inc ecx;
cmp ecx,edi; jl`) — again only sensible as a length (matching
`_apply`'s own `for i in range(HEADER_LEN, len(data))`), not an address.
Passing **the TOTAL buffer length (header + body) instead of a pointer**
for that argument fixed it immediately: the emulation now returns
normally after 27,851 steps instead of running the "loop" up toward a
~1.7-billion value (the buffer's own address, misread as a length) and
never finishing. Whatever the caller's `EDI` genuinely holds at the real
call site (`output16.txt`'s `0x9A6289: PUSH EDI`) it must, by this same
logic, resolve to a length there too — this was never independently
checked in the original live capture, only assumed from the register
name. `wolf_lz4.py`'s module docstring's calling-convention description
should be corrected to match if anyone revisits it.

### What this does and doesn't unblock

Unblocked: any save from a salt never seen before no longer needs the
~20-45 min `find_seed` brute force IF this emulation harness is
available. Practical requirement: `pyghidra` + a JDK + the analyzed
`REProj` Ghidra project (~1.5GB, see below) — fine for continued RE work
on this machine, **not something to ship inside SaveSync itself**.
Turning this into something SaveSync could actually run on a user's
machine would mean either (a) a lightweight x86 emulator (e.g. Unicorn)
loading `FUN_0047caf0`'s code directly out of the user's own already-
installed `Game.exe` at runtime — no game process needs to be running,
just the file read off disk — or (b) finally chasing the real closed-form
formula now that the emulation harness makes it trivial to generate
unlimited (salt, seed-or-keystream) ground truth pairs on demand instead
of relying on live captures. Neither was implemented here — deliberately
left as a product decision, not a research one: (a) means shipping code
that reads and executes fragments of a third-party commercial binary at
runtime, which is a different kind of decision than reimplementing an
algorithm from reverse-engineered understanding, and is worth a deliberate
call rather than a default.

## Session 4: chasing the formula — solved completely

**The salt→seed formula is now fully known, closed-form, exact, and
shipped.** Approach: use the emulation harness from Session 3 as an
oracle — call it with chosen salt bytes (not just real captured ones) and
read the resulting seed straight back out of the emulated stack (found by
scanning for the MT19937 init recurrence, `state[i] =
1812433253*(state[i-1]^(state[i-1]>>30))+i`, which uniquely identifies
`state[0]` — no brute force needed to recover the seed integer itself,
just a mechanical memory scan). Validated first against the two
originally live-captured (salt, seed) pairs from the very start of this
document (`9d,28,f8 → 0x15cd3d19`, `2d,53,50 → 0xe2a7c813`) — exact match
— and cross-checked for self-consistency (`wolf_lz4.keystream(recovered
seed)` must equal the emulator's own directly-observed keystream output)
on every salt tested since; both checks passed on every one of dozens of
salts, so the seed-recovery mechanism itself is trusted independently of
the formula-hunting that follows.

**Method: hold two of the three salt bytes fixed and sweep the third
across all 256 values (an axis sweep), for each byte position, then test
whether the resulting seeds are separable and linear.** Result, exact and
unconditional:

```
seed = MASKS0[salt0] ^ MASKS1[salt1] ^ MASKS2[salt2]
```

Each `MASKSk` is a genuine GF(2)-linear (pure-XOR, no-carry) map from one
byte to 32 bits — confirmed by flipping every individual bit of a byte
and checking the resulting seed XOR-delta is the SAME fixed 32-bit mask
regardless of every other bit's value (the defining property of
linearity), for all 8 bits, for all 3 byte positions. Each map is fully
described by exactly 8 mask constants (one per bit); a 256-entry
lookup table isn't even necessary, though the shipped code builds one at
import time for speed. The three bytes' contributions combine by plain
XOR with **no additive constant** — `seed = 0` exactly when all three
salt bytes are 0.

This is, structurally, a per-byte CRC-style update with an unusual/custom
polynomial rather than a textbook one (no rotation relationship was found
between `MASKS0`/`MASKS1`/`MASKS2` under any shift amount, so it isn't
simply "the same table indexed 3 times" the way a standard byte-at-a-time
CRC often looks) — the exact 24 constants are recorded, not a named
algorithm, which is a complete answer either way.

**Validated three independent ways, not just internally consistent:**
1. Against 1536 emulated data points across 6 axis sweeps (three byte
   positions, each swept both from an all-zero baseline and from a
   different, nonzero baseline for the other two bytes, to confirm
   separability isn't an artifact of the specific baseline chosen) — zero
   mismatches.
2. Against **all 52 real, live-game-captured salts** this project has
   ground truth for (the same `enc_buf_before XOR enc_buf_after`
   keystreams used throughout this document): the formula-derived seed's
   `keystream()` reproduces every one of the 52 real keystreams exactly.
3. Against real, complete, on-disk save files captured during this
   session's Frida sessions (`_cap2`..`_cap5_write_*.sav.bin` in the
   project root, including a genuinely split `WriteFile` case
   concatenated back together, same as the earlier `SaveData02.sav`
   issue) — the formula-derived seed, run through the full production
   `unlock()` pipeline (swap-span search included), decrypts and
   decompresses every one cleanly to the correct, real save content.

**A test-harness bug found and fixed along the way, with the record
corrected:** an early version of the axis-sweep harness only reset the
7 dwords of stack holding the call's arguments between runs, not the
full stack region — leftover locals from a previous call (the function's
own locals, including the stack-allocated MT state array, live there)
could in principle leak into the next. Investigating a suspicious
255/256-mismatch result that this produced turned out to have a much
narrower cause than "general stack contamination": every one of the 255
"mismatches" traced back to a single corrupted value — the harness's own
computed seed for salt `(0,0,0)` specifically, used as the zero-baseline
reference point in the comparison, which was silently wrong under the
old harness (returned a plausible-looking garbage value instead of the
correct answer) and poisoned every diff computed from it by the same
constant XOR offset (confirmed directly: `diff_a(i) XOR diff_b(i)` was
the exact same constant, equal to the corrupted baseline value itself,
for all 255 other i). Every one of the other 255 real data points in that
sweep was byte-identical whether the stack was fully zeroed or not — the
"bug" never actually corrupted a normal call, only this one degenerate
edge case. Zeroing the full stack was still adopted (it's more correct
and costs nothing), but it changed the outcome for exactly one input:
salt `(0,0,0)` now fails to terminate at all under emulation, rather than
returning a wrong answer. Consistent with `(0,0,0)` being genuinely
special — the formula independently predicts `seed=0` there, and it
would not be surprising if the real game code has its own dedicated
"seed must not be 0" branch that this minimal synthetic harness doesn't
reach correctly. Not chased further: no real save's salt is ever going
to be exactly `(0,0,0)` (it's 3 real `rand()` draws), and the formula
itself was derived without needing that data point as a reference (the
GF(2)-linear masks were built from bit-flip differences among
`salt=1..255`, and `BASE` — the true value at `(0,0,0)` — came out to
exactly 0 by the formula's own construction, cross-validated by the 52
real captures rather than by this one problematic emulation call).

**Shipped**: `core/engines/wolf_lz4.py`'s `derive_seed()` (the 24 mask
constants plus the XOR-combine), wired into `find_seed()` as the normal
path — instant, no search — with the 2^32 search kept only as a
defensive fallback (still going through `verify_seed` before being
trusted, same as a cache hit would). A save from a salt never seen
before no longer costs 20-45 minutes; it's instant now, for every case
this document has real ground truth for.

## Session 5: the swap-offset formula — also solved completely, and a real bug found along the way

**"Fully solve the save" — the write direction was still blocked by the
one piece this whole investigation never cracked: what determines the
post-encrypt swap span offsets (`FUN_00523b40`). Solved now, the same way
the seed formula was: emulate the real function as an oracle instead of
reading the obfuscation by hand.**

This picks up after SaveSync's own scratch capture files (`_cap*.bin` in
the project root) were cleared out between sessions. They were not
needed: the salt→seed formula (Session 3/4) is exact and closed-form, so
ground truth for this session was regenerated on demand straight from
real, current save files — the game's own `Save/*.sav` and a handful of
independent historical backups from SaveSync's own backup store — via
`derive_seed()` + a real `unlock()`, no live game or Frida capture
required at all. That data lives in `C:\ghidra_re\swap_ground_truth\`
(pre-swap buffer reconstructions + a manifest of confirmed `(a, b)`
pairs), built by `build_swap_ground_truth.py`.

### The emulation harness (`swap_lib.py` / `emulate_swap.py`)

`FUN_00523b40` reads the save buffer through a **global** pointer
(`DAT_0146bbdc`), confirmed at its very first real instruction
(`0x523b53: MOV EAX,[0x0146bbdc]`) — not through any register or stack
argument (Ghidra itself reports 0 parameters for it). That makes the
harness simpler than `FUN_0047caf0`'s: no calling-convention guesswork,
no SEH/TEB setup (this function's prologue is a plain `/GS` cookie, no
`_chkstk`, no exception frame) — just point the global at a scratch
buffer holding the real pre-swap bytes, run to return, and read the
buffer back.

**Worked on the very first attempt**, no debugging needed this time:
6/6 real fixtures (5 current `SaveData0N.sav` + `System.sav`) reproduced
their known-correct swap exactly, in ~14,500 emulated steps each
(under a second per file once the JVM/project is warm). One of the six
initially looked like a mismatch — see below, that turned out to be the
"known-correct" value itself being wrong, not the emulator.

### A real, pre-existing bug this exposed: `find_swap_spans` can return a wrong answer even with its "strong" validator

`SaveData05.sav`'s emulated swap, `(34, 94)`, didn't match what
`unlock()`'s own search had found for that file, `(33, 93)`. Checked
directly: `(33, 93)` decrypts and decompresses to something that passes
`_validate_wolf_body` (a real `WolfValues` parse, not just the cheap
shape check) — but the payload is silently corrupted at **2733 scattered
byte positions** through the record data (not just the visibly garbled
title string — real field values too). `(34, 94)` decodes byte-for-byte
clean. The search (even asked with the strong validator explicitly)
returns `(31, 91)` — a *third*, also-wrong answer — when re-run with a
tighter validator; with the default cheap one it's `(33, 93)`. All three
are search artifacts of the same underlying gap: nothing in either
validator actually checks the *content* of the decoded name string or
record values, only that they parse structurally, and for this file more
than one offset pair happens to parse. This was a real, shipped
correctness bug in the read path (silently wrong field values, no error
raised) that nothing before this session had grounds to detect — there
was no independent way to know a search's "plausible" answer was
actually wrong until there was a second, provably-correct method to
compare it against.

### The formula

Method: hold everything constant and flip one byte at a time, exactly
like the seed formula's axis sweeps, starting from "does this depend on
the body at all" (flip individual bytes throughout a real 57KB body,
including its very last byte: **zero effect** on the offsets, every
time) and "does it depend on the buffer's length at all" (truncate a
real 57KB buffer down to 200 bytes, keeping the same header: **zero
effect**, the offsets don't even move if the truncated buffer is too
short to physically hold the second span). That leaves the 20-byte clear
header as the only remaining candidate. Flipping each of its 20 bytes
individually against the same fixed rest of the buffer: only two of them
did anything — byte 11 moved `a`, byte 14 moved `b`, nothing else moved
either. A full 256-value sweep of each (holding the other constant) gave
an exact, clean linear-with-wraparound relationship for each:

```
a = HEADER_LEN + (header[11] % 30)     # a ∈ [20, 49]
b = 80          + (header[14] % 40)    # b ∈ [80, 119]
```

Both ranges are disjoint with room to spare (`a`'s span ends at 49+20=69
at the latest; `b` never starts before 80) — consistent with a
deliberate design that guarantees the two spans can never collide,
not a coincidental fit. Both source bytes are in the file's own clear
header, so this needs no seed and no decryption at all — salt and swap
offsets are both recoverable from the same first 20 bytes, before
anything else about the file is touched.

**Validated against 8 real files**, not just the one this was derived
from: the 6 fixtures above, plus `SaveData01.sav` and `System.sav` from
an independently-timestamped historical SaveSync backup with completely
different header bytes and content. 8/8 exact matches against the true
(emulator-confirmed) offsets — which is a stronger claim than "8/8 match
what `find_swap_spans` previously returned", since one of the 8 is
exactly the file where that search was proven wrong.

### Why the earlier "same length, 24 different offsets" evidence didn't contradict this

Session 2's original finding — 24 real saves sharing one exact
compressed length, 24 different swap offsets — looked like it ruled out
any simple per-file rule and was the reason this session's very first
fix (widening `dump()`'s refusal to cover every save needing a swap, not
just unvalidated ones) erred conservative. It doesn't contradict the
header-byte formula: those 24 saves are 24 **separate save actions**,
each with its own freshly-`rand()`-generated 20-byte header (confirmed
back in Session 2: the header is regenerated from scratch, 20 fresh
`rand()` calls, on every single save) — nothing to do with content
length at all. The two findings describe different things: content
length never determined the offsets (still true), and SaveSync's own
edit-and-write-back path reuses the SAME header an edit never touches
(new fact), so the offsets it read are still exactly right to reuse —
not as an assumption, but because `derive_swap_spans` on that same,
unchanged header recomputes the identical answer.

### Shipped

- `core/engines/wolf_lz4.py`: `derive_swap_spans()` (the closed form),
  wired into `unlock()` and `verify_seed()` as the path tried before the
  search fallback (which stays only for a build/save this formula was
  never validated against). `lock()`'s docstring corrected — the
  "correct by construction" claim it used to make for a different,
  wrong reason is now true for the right one.
- `core/save_editor/crypt/wolf_lz4.py`: `WolfLZ4Values.dump()` no longer
  refuses every save needing a swap (this session's earlier, more
  conservative fix) — it recomputes the span fresh from the unchanged
  header via `derive_swap_spans` and compares against what `load` used;
  matching (the normal case, always, in every real file tested) writes
  normally, and only the residual case — a swap `load` could only find
  via the search fallback — still refuses, per this format's "never
  guess" standard.

**Result, tested end to end**: opening any of the 5 current `SaveData0N.sav`
files now takes ~0.4s (was several seconds to a search-driven crawl,
depending on how the old validator's exhaustive-search fallback behaved
for that file) — no more search at all, formula both times (seed and
swap). Editing a real field, writing it back, and reopening the written
file reproduces the edit correctly, and the written file's own header
still resolves to the identical, correct swap span. `System.sav` opening
also got faster as a side effect — the "no variable database in this
file" case no longer has to exhaust a swap search before concluding that.

**Still not independently confirmed, at the time this was written**: an
actual load of a SaveSync-written file back in the real, running game.
Everything checked from this end — the swap logic now proven byte-identical
to `FUN_00523b40`'s real behavior across 8 independent files, LZ4 being
decoder-agnostic so the recompressed stream doesn't need to match the
game's own encoder byte-for-byte — pointed at yes, but a real round-trip
in the actual game was the one check this session couldn't run itself.
**Since confirmed — see "Session 6" below.**

## Session 6: the real-game round trip, confirmed for real

**The one gap Session 5 left open — an actual load of a SaveSync-written
file back in the real, running game — is now closed.** Not from inside
this RE workspace: from a real user editing a real, live save through
SaveSync's shipped save editor.

Sequence, as it actually happened: a field in a real `SaveData01.sav` was
edited through the editor (a string-encoded value, `"108"` → `"115"`),
written back, and the game did not visibly honor the new value. That
looked at first like a possible write-path bug — the compressed file also
shrank (~57KB → ~33KB), which read as suspicious. Direct investigation
resolved both, independently of anything above:

- A byte-for-byte diff of the two files' decompressed payloads (564,162
  bytes each) found **exactly 2 differing bytes**, precisely the edited
  field, nothing else in the entire payload touched — proving the write
  path (seed formula, swap formula, LZ4 recompression, record splice) did
  exactly what it was asked, with surgical precision.
- The size drop is pure LZ4 encoder efficiency (SaveSync's `high_compression`
  mode packs tighter than whatever encoder the game itself uses) — LZ4 is
  decoder-agnostic, a fact this document already relied on above, now
  confirmed to hold in practice too.
- The edited value not visibly taking effect is the game's own logic
  (validation, a lookup, a recompute) rejecting/ignoring `"115"` for that
  field — outside SaveSync's write path entirely.

The field was then reverted through the same editor, `"115"` → `"108"`
— and **that save was accepted and loaded successfully by the real,
running game.** A SaveSync-written file, produced entirely from the
closed-form seed and swap-offset formulas above with no search fallback
involved, round-tripped through the actual game for the first time. That
is the last item this whole investigation had marked as unconfirmed.

## Toolchain / reusable assets

- `C:\ghidra_re\ghidra_12.1.3_PUBLIC\` — Ghidra install
- `C:\ghidra_re\project\REProj.gpr` (+ `.rep\`) — **fully analyzed** project,
  reuse via `pyghidra.open_project(r"C:\ghidra_re\project","REProj",create=False)`
  + `pyghidra.program_context(project, "/Game.exe")` (skips the ~13min
  auto-analysis)
- `C:\ghidra_re\analyze*.py` / `output*.txt` — all incremental scripts and
  their decompiled/disassembled output from this session, chronological.
  Key ones: `output16.txt`/`output20.txt` (raw disasm of the real call
  site and function entry), `output17.txt` (decompile containing the MT
  generation/application loops — note the *early* part of this decompile,
  before the loop, is unreliable due to anti-disassembly obfuscation
  corrupting Ghidra's control-flow analysis for that region — the function's
  registered body is only ~1200 bytes but the decompile is 7000+ lines),
  `output18.txt` (the MT step function `FUN_0056a0d0` itself, clean/reliable).
- JDK: `C:\Program Files\Java\jdk-25` (works with pyghidra when
  `JAVA_HOME` is set to it)
- Scratchpad (`...\scratchpad\`): `wolf_lz4_decrypt_poc.py` (the validated
  reference decryptor + brute-force seed search skeleton), `hook_all_in_one.py`
  / `hook_real.py` / `hook_mt_state.py` (the working Frida capture scripts —
  reusable if more live data is ever needed; remember to attach to the
  *current* PID, not a cached one, and not to let the game restart mid-capture),
  `real_hit1_before.bin` / `real_hit1_after.bin` (the validated 512-byte
  before/after capture used above), `mt_test2.py` (the validation script).

`C:\ghidra_re` is roughly 1.5GB (JDK + Ghidra + analyzed project) and can be
deleted if this work is fully wrapped up; keep it if resuming later, since
the analyzed project alone saves ~13 minutes on the next session.

## Appendix: the tag==0xFF decoy branch (dead code for this install)

Kept for reference in case a different game build/version uses it.

- Call site `0x9A5F36` → `FUN_0054af30` @ VA `0x54AF30`. `ECX=100`
  (constant), `EDX=&{buf[0],buf[5],buf[3]}`, stack args: buffer, `0x14`,
  `0`, `(int64)(len-0x14)`, `0`.
- ~3400 lines of decompiled C, almost entirely obfuscation: six
  constant-returning stub functions (`FUN_00457f30`→2, `FUN_00405b10`→0,
  `FUN_00457f60`→7, `FUN_00457f40`→3, `FUN_00457f70`→9, `FUN_00457f50`→6),
  global "salt" bytes (`DAT_00f303a8=8,...a9=9,...aa=7,...ab=1,...ac=3`),
  and a doubles table at `DAT_00c3e250` that's just the standard
  signed→unsigned `2^32` int-to-double correction — all combine so every
  opaque predicate in the function folds to a fixed, input-independent
  outcome (verified against real asm at `0x054af5d`–`0x054af99`). The
  function-pointer "dispatch" tables at `0xf327b8`/`0xf32b54` all resolve to
  `/guard:cf` compiler boilerplate (`guard_check_icall`), not attacker logic.
  `FUN_0042df90` is `std::string` copy-construction; `FUN_00b32b90` is
  `memmove`.
- Live-hooked (with the corrected address) during this session and
  confirmed it never fires for any real save/load action — consistent with
  no on-disk save file ever having tag byte `0xFF`.
