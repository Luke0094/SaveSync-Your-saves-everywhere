# SaveSync

> **Your saves, everywhere.** — Game save manager with cloud sync, versioned backups, and an in-game overlay.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Qt](https://img.shields.io/badge/UI-PySide6%20(Qt6)-41cd52)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

<!-- Drop a screenshot into screenshot/ and update the path below -->
<img width="964" height="638" alt="Immagine 2026-08-10 182638" src="https://github.com/user-attachments/assets/243d76b3-32a4-41f6-ab45-b3f7ded85e97" />

SaveSync watches the games you play, finds their save folders on its own, keeps
versioned local backups, and mirrors everything to the cloud provider of your
choice — with an always-on-top overlay so you never have to leave the game.

---

## Features

### Detection & tracking
- **Auto-detection of running games** via process monitoring, with a cached
  process snapshot (per-process verdicts memoized across polls) and gentler
  polling while a game session is active — near-zero background cost during
  CPU-bound gameplay
- **Heuristic save-folder detection** — scored candidates from folder keywords,
  game-name similarity (`match_slug` keeps letters and digits in any script —
  Japanese, Chinese, Korean, Cyrillic, Greek — not only ASCII), file analysis,
  engine-specific paths, and registry saves
- **Real-time filesystem watcher** — reacts to save changes instantly, even for
  paths that don't exist yet
- **Unknown-game queue** — every unrecognized game is remembered; the overlay
  badge counts pending detections and the notification itself is browsable
  (carousel arrows), no separate window
- **Proton/Wine on Linux** — Windows games save inside their compatibility
  prefix, and SaveSync looks there too
- **Launcher URL support** — games launched through `steam://`-style URLs are
  resolved to their real executable
- **Playtime tracking** per game, with per-session detail on hover

### Backups & sync
- **Versioned local backups** with retention (max count, days, minimum kept,
  size cap) and content-dedup (unchanged saves are skipped)
- **Mtime preflight** — Backup All / Sync skip games whose saves clearly have
  not changed (newest mtime + file count vs the last backup), before rebuilding
  content hashes
- **Adaptive batch queues** — Backup All / Sync All run with a concurrency
  limit derived from CPU and RAM, with sidebar progress (`N/M — name`) and
  resume after an app restart
- **Pre-restore safety backups** — automatic backup before any restore
- **Integrity checks** — each backup is opened and confirmed readable, on
  demand or on a schedule, so a damaged archive is found before you need it;
  sweeps throttle only on weaker machines, skip archives still marked OK
  recently, and flush index files once per game
- **Interference alerts** — when something outside SaveSync puts an older save
  state back (a launcher's automatic sync, say), it says so, and can force the
  restore with the game frozen
- **Periodic in-game backups** — configurable per-game interval while playing
- **Provisional backups** — pre-confirmation saves are protected from the very
  first session, before you've even confirmed the paths
- **Multi-provider cloud sync**: Google Drive (OAuth, Service Account, Desktop
  folder), OneDrive (MSAL device flow), Dropbox (OAuth PKCE), WebDAV
  (Nextcloud, ownCloud, Box…), rclone (MEGA, S3, SFTP, B2, pCloud, 40+ remotes),
  local/NAS folder
- **Conflict resolution** with per-machine detection: cross-machine divergence
  always asks (keep local / keep cloud / keep both)
- **Quick restore from the overlay** — browse local *and* cloud backups without
  leaving the game; cloud entries download transparently

### Library
- **Card and list views** with search (by title or developer), folder tree with
  colors, star ratings from reviews, and three-state filters for tags and
  engines (include / exclude)
- **Paged lists** — only the current page of cards/rows is built; on capable
  machines the page is filled in one go, on weaker ones in small chunks so the
  UI stays responsive
- **Smart tag merging** — case- and separator-insensitive ("2D Game", "2d-game"
  and "2DCG"/"2dcg" converge to one canonical tag, self-healing on startup)
- **Web metadata search** — name, description, cover, developer, release date,
  tags and reviews/ratings scraped from store pages, wikis and forum threads
  (spoiler-wrapped tag lists included), with a merge dialog to pick what to keep
- **Per-game context menu** — backup, restore, sync, open save folder, edit,
  web search, remove

### Experience
- **In-game overlay** — frameless always-on-top card with fade animations,
  notification carousel, cloud-save prompts, running game with engine label,
  and exclusive-fullscreen protection (never breaks a game's display mode)
- **Save editor** — open a game's save and change the values in it, with the
  original kept aside first; reachable from the sidebar, the library's context
  menu, straight to the running game, or by dropping a save file on it
- **Pinned notes and images** — keep a text file, a map, or a piece of the
  screen on top of the game, drag it anywhere, edit the text in place; each
  game's pins come back when it starts
- **Global hotkeys** via pynput (no root needed on Linux) — default
  `Ctrl+Alt+S` toggles the overlay
- **Live language switching** (English / Italian, no restart)
- **Dark / Light theme**, instant switch
- **Secure credential storage** via OS keyring (Windows Credential Manager,
  macOS Keychain, Linux Secret Service) with AES-256 encrypted fallback
- **System tray** integration, minimize-to-tray, single-instance lock with
  second-launch focus, animated bootloader splash

---

## Installation

### From source

**Requirements:** Python 3.10+

```bash
git clone https://github.com/Luke0094/SaveSync-Your-saves-everywhere.git
cd SaveSync
pip install -r requirements.txt
python main.py
```

### Offline installation

To install on a machine without internet access (or to keep a local copy
of every dependency in case a package ever disappears from PyPI):

```bat
:: Windows
download_offline_deps.bat   :: once, on an ONLINE machine — fills offline_deps\
install_offline_deps.bat    :: on the target machine — installs with --no-index
```

```bash
# Linux / macOS
chmod +x *.sh                  # once, after cloning
./download_offline_deps.sh     # once, on an ONLINE machine
./install_offline_deps.sh      # on the target machine
```

The `offline_deps\` folder contains wheels for the **same OS and Python
version** it was generated on — regenerate it when either changes. It is
git-ignored (hundreds of MB); carry it alongside the project folder.

### Building the Windows executable

```bash
pip install pyinstaller
pyinstaller --clean savesync.spec
```

Output: `dist/SaveSync.exe` (single file, animated splash, no console).
Always pass `--clean` — the spec injects custom Tcl into the splash and stale
build caches would ship the old version.

### Dependencies

| Package | Purpose |
|---------|---------|
| PySide6 | Qt6 UI framework |
| psutil | Process monitoring |
| watchdog | Filesystem event watching |
| requests | HTTP client (OneDrive, WebDAV, web search) |
| google-auth, google-auth-oauthlib, google-api-python-client | Google Drive API |
| msal | Microsoft authentication (OneDrive) |
| dropbox | Dropbox SDK |
| webdavclient3 | WebDAV protocol |
| pynput | Global hotkeys (user-level on Linux, no root) |
| cryptography | AES-256 credential encryption |
| keyring | OS-level credential storage |
| Pillow + pillow-avif-plugin | Cover images (AVIF/WebP included) |

---

## Architecture

```
savesync/
├── main.py                        # Entry point, single-instance lock, startup tracing
├── runtime_splash_hook.py         # Bootloader-time single-instance mutex
├── savesync.spec                  # PyInstaller build (animated Tcl splash)
├── core/
│   ├── constants.py               # App identity, paths, folder-name rules
│   ├── config_manager.py          # JSON config with debounced writes and validation
│   ├── config_transfer.py         # Config export / import, cloud config history
│   ├── library.py                 # Game library CRUD, tag canonicalization
│   ├── monitor.py                 # Process monitor (cached snapshots, adaptive polling)
│   ├── watcher.py                 # Real-time filesystem watcher, debounced events
│   ├── save_detector.py           # Heuristic save folder detection and scoring
│   ├── engines/                   # Engine recognition + binary/format readers
│   │   ├── game_engine.py         # Which engine a game was built with
│   │   ├── gvas.py                # Unreal Engine .sav (GVAS)
│   │   ├── renpy.py               # Ren'Py .save (pickle, read never run)
│   │   ├── lcf.py                 # RPG Maker 2000/2003 .lsd (LCF chunks)
│   │   ├── rpgmaker.py            # RPG Maker MV/MZ save packing
│   │   ├── lzstring.py            # LZString codec (used by MV / HTML games)
│   │   ├── rubymarshal.py         # Ruby Marshal 4.8 (XP/VX/VX Ace)
│   │   ├── naninovel.py           # Naninovel .nson (raw deflate)
│   │   ├── sol.py                 # Flash shared objects (.sol, AMF0/AMF3)
│   │   ├── qsp.py                 # QSP saves (line-based, obfuscated)
│   │   ├── kirikiri.py            # KiriKiri .ksd (TJS dictionary in UTF-16)
│   │   ├── tyrano.py              # TyranoScript .sav (JSON behind JS escape())
│   │   ├── alicesoft.py           # AliceSoft System 4 globals and slots
│   │   ├── artemis.py             # Artemis Engine settings (BOWX container)
│   │   ├── rags.py                # RAGS .rsv (.NET objects behind fixed AES)
│   │   ├── wolf.py                # Wolf RPG obfuscation and checksum
│   │   ├── sqlite_db.py           # SQLite save databases (Room / Java)
│   │   ├── playerprefs.py         # Unity PlayerPrefs registry export
│   │   ├── tads.py                # TADS system.rec slots
│   │   ├── keyvalue.py            # key = value text configs
│   │   └── xml_save.py            # Plain XML saves
│   ├── save_editor/               # Save-file editor: orchestration + adapters
│   │   ├── save_editor.py         # open_save, detection, backups of edits
│   │   ├── save_hold.py           # Holds chosen values against the game
│   │   ├── base.py                # SaveField, errors, shared walk helpers
│   │   ├── json_format.py         # Plain JSON
│   │   ├── xml_format.py          # Plain XML
│   │   ├── playerprefs_format.py  # Unity PlayerPrefs (registry export)
│   │   ├── naninovel_format.py    # Naninovel .nson
│   │   ├── lzstring_json_format.py # LZString base64 JSON (shared base)
│   │   ├── rpgmaker_mv_format.py  # RPG Maker MV (.rpgsave)
│   │   ├── rpgmaker_mz_format.py  # RPG Maker MZ (.rmmzsave, zlib wrap)
│   │   ├── sugarcube_format.py    # Twine / SugarCube
│   │   ├── lcf_format.py          # RPG Maker 2000/2003 .lsd
│   │   ├── rubymarshal_format.py  # RPG Maker XP/VX/VX Ace
│   │   ├── keyvalue_format.py     # key = value text (.ini, .properties, …)
│   │   ├── gvas_format.py         # Unreal GVAS (+ encrypted wrapper)
│   │   ├── renpy_format.py        # Ren'Py .save
│   │   ├── sol_format.py          # Flash .sol
│   │   ├── qsp_format.py          # QSP
│   │   ├── es3_format.py          # Easy Save 3
│   │   ├── rags_format.py         # RAGS .rsv
│   │   ├── kirikiri_format.py     # KiriKiri .ksd
│   │   ├── wolf_format.py         # Wolf RPG Editor
│   │   ├── alicesoft_format.py    # AliceSoft System 4
│   │   ├── artemis_format.py      # Artemis
│   │   ├── tyrano_format.py       # TyranoScript
│   │   ├── tads_rec_format.py     # TADS TAD-kit system.rec
│   │   ├── sqlite_format.py       # SQLite (Room / Java desktop)
│   │   └── crypt/                 # Decryptors used only by the editor
│   │       ├── unreal_crypt.py    # Unreal saves locked with the game's own key
│   │       ├── es3.py             # Unity Easy Save 3, including encrypted
│   │       ├── wolf.py            # Wolf unlock + variable database
│   │       ├── game_keys.py       # Remembered decrypt keys, per game
│   │       └── unityfs.py         # Unity asset bundles, unpacked to find keys
│   ├── manual_paths.py            # Hand-registered save folders, single or in bulk
│   ├── registry_saves.py          # Windows-registry save locations
│   ├── skip_dirs.py               # Shared skip-list of noise directories
│   ├── backup.py                  # Versioned zip backups, retention, dedup
│   ├── pending_batch_jobs.py      # Persisted multi-operation batch queues
│   ├── resolvers.py               # Launcher URLs, executable resolution
│   ├── exe_scan.py                # Folder scan for installed game executables
│   ├── game_api.py                # Web metadata search orchestration
│   ├── game_sources/              # Steam, VNDB, wikis, store/forum scrapers
│   ├── enrichment.py              # Fills missing metadata on found titles
│   ├── net.py                     # Shared HTTP session and retry policy
│   ├── credentials.py             # Secure credential store (keyring + AES fallback)
│   ├── machine.py                 # Machine fingerprint for cross-machine detection
│   └── startup.py                 # Autostart, directory setup, migrations
├── sync/
│   ├── __init__.py                # Provider registry, orchestrator, conflict detection
│   ├── base.py                    # Abstract SyncProvider
│   ├── local_provider.py          # Local / NAS folder
│   ├── google_drive.py            # Google Drive (OAuth, Service Account, Desktop)
│   ├── onedrive_provider.py       # OneDrive (MSAL device flow)
│   ├── dropbox_provider.py        # Dropbox (OAuth PKCE)
│   ├── webdav_provider.py         # WebDAV (Nextcloud, ownCloud, any DAV server)
│   └── rclone_provider.py         # rclone wrapper (any configured remote)
├── i18n/                          # Live-switching translation engine (en, it)
├── hotkeys/                       # Global hotkey manager (pynput)
└── ui/
    ├── main_window.py             # Main window, tray, monitor integration
    ├── main_window_cloud.py       # Cloud prompts and sync flows of the main window
    ├── overlay.py                 # In-game overlay (notifications, prompts, restore)
    ├── unknown_history.py         # Pending unknown-game queue (persistence)
    ├── blur_modal.py              # Fullscreen blur backdrop for modal flows
    ├── modal_helpers.py           # Window-modal message boxes with the app's chrome
    ├── helpers.py                 # Elided labels/checkboxes, file manager, window flags
    ├── image_cache.py             # Cover download, conversion and disk cache
    ├── backup_labels.py           # Human-readable backup names and timestamps
    ├── game_search_runner.py      # Background web-search worker for the UI
    ├── splash_screen.py           # Startup splash
    ├── styles/theme.py            # Dark/Light QSS theme manager
    ├── pages/                     # Overview, Library, Sync, Backups, Settings,
    │                              # Save editor
    ├── widgets/                   # Game cards/rows, path rows, folder tree, file
    │                              # list, cover editor, hotkey editor, pickers,
    │                              # busy overlay, pinned notes, screen capture
    └── dialogs/                   # Add/edit game, auto-scan, restore, conflicts,
                                   # exe scan, manual paths, game search, cloud
                                   # verify, config import, credits
```

---

## Sync providers

| Provider | Auth methods | Notes |
|----------|-------------|-------|
| **Google Drive** | OAuth browser flow, Service Account JSON, Desktop app folder | Folder-level caching, chunked upload |
| **OneDrive** | MSAL device flow, personal token, local folder | Resumable upload sessions, auto token refresh |
| **Dropbox** | OAuth PKCE browser flow, personal token, local folder | Chunked upload for large files |
| **WebDAV** | Username/password | Nextcloud, ownCloud, Box, any DAV server |
| **rclone** | Any rclone-configured remote | MEGA, S3, SFTP, B2, pCloud, and 40+ others |
| **Local** | Folder path | USB, NAS, network share, any mounted path |

### Adding a new provider

1. Create `sync/my_provider.py` extending `sync.base.SyncProvider`
2. Set `PROVIDER_ID` and `DISPLAY_NAME_KEY`
3. Implement the abstract methods (`connect`, `disconnect`, `upload`,
   `download`, `list_files`, `delete_remote`, `remote_exists`,
   `get_remote_metadata`)
4. Add `credential_fields()` for the UI form
5. Register it in `sync/__init__.py` inside `_register_all()`

The UI picks it up automatically.

---

## Conflict resolution

Every decision is asked through the overlay notification, with the alternatives
under the primary button's `▾` — never through a modal that could open behind a
fullscreen game.

| Condition | Result |
|-----------|--------|
| Only local modified since last sync | Auto-upload |
| Only cloud modified | Auto-download |
| Both modified, same machine | Auto-backup, then ask |
| Both modified, different machine | **Always ask** (even with auto-backup enabled) |
| Conflict left unresolved in an earlier session | Asked again at the next launch |
| Local saves never synced but a cloud copy exists | Asked to reconcile (same options, plus "it's a different game") |
| No local data, cloud has saves | Ask to download |
| Unknown game, cloud folder with same name | Download & add, keep local, or "it's a different game" (own folder) |

Every "ask" above offers **keep local**, **keep cloud** and **keep both** (which
backs up locally, downloads, then re-uploads), plus a dated local-vs-cloud
comparison. Choosing any of them syncs, so the question doesn't come back. The
per-game *don't show again* silences the launch prompts for that game.

A never-synced game can also answer **"it's a different game"**: the cloud
folder its title resolves to belongs to a same-titled game from another
machine, so this one moves to its own folder (`Alpha_2`) with its backups, and
the two stop sharing a destination. Not offered once the two sides have synced
together — that already settles whose folder it is.

A save that goes *backwards* without SaveSync doing it (a launcher's own cloud
sync, another tool) is reported separately: it isn't a conflict, so the prompt
offers to restore the newest backup, with acknowledgement in the dropdown.

---

## Save detection

Candidate folders are scored from multiple signals:

- Keyword match in the folder name (`save`, `savegame`, `checkpoint`, `userdata`, …)
- Game-name similarity in path components — `match_slug()` compares letters and
  digits in **any** Unicode script (not ASCII-only), so titles in Japanese,
  Chinese, Korean, Cyrillic or Greek still match their save folders
- File analysis (non-binary, non-empty, known save extensions)
- Common save naming patterns (slot numbering, autosave, player profiles)
- Engine-specific locations (RenPy, RPG Maker, Unity, Unreal, Godot, …)
- Windows registry save locations
- Process working directory and known install roots

Custom keywords can be added in **Settings → Save Folder Hints**.

**Write-time correlation** (optional, off by default) adds one more signal: a
save-like file written at the same moment as a path already known for the
running game is claimed for it too — which is how folders named after a game's
*internal* title get found. Enable it and set the window in
**Settings → Save Detection**.

---


Some extensions are engine data in one engine and a save in another, so the
engine is read from the game's executable before they are judged:

| Engine | Also treated as saves |
|---|---|
| Unity, Godot, Java, WebGL | `.dat`, `.bin` |
| GameMaker | `.dat` |
| TADS | `.t3v` (classic MJR state files) |
| RPG Maker, Unreal, Ren'Py, unknown | none — `.dat`/`.bin` stay engine data |
| TyranoScript, Bakin, SRPG Studio, AliceSoft, Artemis, Wolf RPG | none — they save into extensions nothing skips |
| NW.js, Electron | none — a wrapper says nothing about what the game inside saves into |

The engines it knows are Ren'Py, RPG Maker (2000 through MZ), Unity, Unreal,
Godot, GameMaker, TyranoScript, RPG Developer Bakin, SRPG Studio, AliceSoft
System, Artemis, Wolf RPG Editor, TADS, Java and WebGL. NW.js and Electron
are recognised too, but only after every one of those has been ruled out:
they are a Chromium runtime with somebody's game inside, and RPG Maker MV,
TyranoScript and HTML5/WebGL shells all ship as one — answering "NW.js" for
those would be naming the box instead of what is in it.

A game added with only a save folder has no executable to read, so it is
treated as unknown: the conservative side, where a save that is merely not
proposed can still be added by hand.

## Save editor

Open a game's save and change what is inside it — money, stats, flags. Reach it
from the sidebar, from a game's context menu in the library, or just open the
page while playing: it lands on the running game.

Two rules it is built around:

- **Never guess.** A format is either understood well enough to rebuild it
  byte-for-byte, or it is named and left alone. Before any file is offered for
  editing it is decoded, re-encoded, and checked against the original — a file
  that does not match is read-only.
- **The original comes first.** A dated copy is put aside before anything is
  written, and every copy can be put back from the same screen. How many to
  keep of one save, and how long to keep them, are in Settings — three copies
  and seven days to start with. The newest is never dropped for age, so there
  is always something to undo with, and the age rule is applied at startup as
  well, so it reaches saves nobody has opened since.
- **The game need not be in the library.** Drop a save file onto the editor,
  or pick one, and it opens on its own. Some games are not worth adding — a
  RAGS game needs a pile of scripts around it just to start — and the save is
  the only part anyone wanted.

| Editable now | Recognised, not editable yet |
|---|---|
| JSON — Unity, Naninovel, Godot, HTML / WebGL games, whatever the extension | Saves a game encrypts with a key of its own |
| Naninovel (`.nson`), deflated or plain, down to the game's own variables | |
| XML — .NET's serializer, so Unity and Godot games saving through it | |
| Unity PlayerPrefs — a registry key rather than a file | |
| RPG Maker MV (`.rpgsave`) and MZ (`.rmmzsave`) — they compress differently | One-off containers a single studio uses and nothing else does |
| RPG Maker XP / VX / VX Ace (`.rxdata`, `.rvdata`, `.rvdata2`) | |
| Unreal Engine (`.sav`, GVAS) — UE4 and UE5, including the 5.4 property tag | An Unreal save whose game encrypted it and does not keep the key plainly in its own files |
| Ren'Py (`.save`) | |
| RPG Maker 2000/2003 (`.lsd`) — switches, variables, steps | |
| Adobe Flash shared objects (`.sol`), AMF0 and AMF3 | |
| QSP (Quest Soft Player) | |
| Wolf RPG (`.sav`) | AliceSoft gallery lists — numbers with nothing naming what they unlock |
| KiriKiri / KAG (`.ksd`) | RPG Developer Bakin (`.sgs`) — an object stream with nothing naming or typing it |
| TyranoScript / TyranoBuilder (`.sav`) | SRPG Studio — the engine encrypts its saves with a key kept in the game |
| AliceSoft System 4 global data and numbered slots (`.asd`, `.sav`) | Artemis save slots and across-playthrough data — a tagged tree this cannot follow safely |
| Artemis settings (`system.dat`) | |
| Unity Easy Save 3 (`.es3`), encrypted or not | |
| Twine / SugarCube and other LZString HTML games (`.save`) | |
| RAGS (`.rsv`) — variables, objects, rooms, the player | |
| Any Ruby Marshal file, including engine `.dat` saves | |
| `key = value` text (`.ini`, `.cfg`, `.conf`, `.properties`) | |
| TADS record (`system.rec`) — whitespace tokens, NUL-padded | Classic MJR TADS 3 VM state (`.t3v`) — a snapshot, not a named value list |
| SQLite (`.db`, `.sqlite`) — Room / Compose Desktop Java progress | |

<summary><strong>Some Info about Saves system</strong></summary>
<details>

Two notes on Ren'Py, because they are unusual. Its saves are Python pickles,
and unpickling one runs code from the file — so SaveSync reads the pickle
opcode by opcode and never builds anything out of it. And Ren'Py 8 refuses a
save whose signature does not match its contents, so an edited save is
re-signed with the key Ren'Py generated on this machine; if that key cannot be
found, the save is not written rather than handed back in a state the game
would reject.

**Values by category.** A save is not one long list — an engine keeps its
switches, its variables, the party and the actors separately, and the editor
shows them that way, one group at a time with the filter and the pager working
inside it. The groups come from the file itself, so a format nobody planned
for still gets a working selector. It matters for more than tidiness: RPG
Maker keeps switches and variables in containers with the same internal name,
so without this, switch 12 and variable 12 look like the same value and
neither can be locked.

**Easy Save 3** files are JSON, and encryption is something the developer
turns on. When it is on, the password is not in the save — it is in the game,
baked into the build as plain text, and SaveSync reads it from there. That is
still a file on disk: nothing attaches to a running game, which is how the
published tools for this work and is the one thing this program will not do.
A password is only accepted once it has actually opened the save, so a wrong
guess fails instead of producing rubbish. If a game hides its password
somewhere unusual, put the key in an `es3.key` file beside the save.

The right column is by extension: a `.sav` or `.dat` that turns out to hold
JSON or plain text is read and edited like any other. The engine of a game is
read from its executable, so a title added with only a save folder is treated
as unknown.

**Locking a value.** Set a value, press the lock next to it, and SaveSync
watches the file: every time the game writes a lower number over it, the
locked one goes back in. That is how a file editor gets you something like
unlimited health. It only bites when the game actually *writes* the save — a
game that saves at checkpoints gets its values held at checkpoints — and
values the game keeps only in memory are out of reach by design.

Values are held by NAME, never by position: a game that rewrites its save can
move a value, and a positional key would then hold whatever moved into the
slot instead. A name that turns up twice, or not at all, is skipped rather
than guessed at.

The file is read only once it has stopped changing, SaveSync's own writes are
recognised and ignored, and one copy of the original is kept when the lock
starts, not once per cycle. A value comes back about half a second after the
game overwrote it (measured: 558 ms median), which costs 0.06% of one core to
watch for — a game that reads its save back in that half second reads its own
value, not yours.

**RAGS** saves are a .NET object graph behind a fixed key, and they are big:
the one this was built against holds three million values, nearly all of them
the colours, fonts and command lists that make up the game's own logic. So the
whole save is read — the format is sequential, there is no skipping — but what
is offered is the part that is game state: variables, objects, rooms, timers
and the player, each under its own name. Fifteen thousand values instead of
three million.

**KiriKiri** saves are not a binary format at all: the game writes its state
out as a readable dictionary in UTF-16, and SaveSync edits it in place. It
arrives in three wrappers — the text on its own, deflated, or behind the
thumbnail the game shows in its load menu — and all three are written back
exactly as they came.

**TyranoScript** saves are JSON that the engine ran through JavaScript's
`escape()`, so the file is readable text with most of its punctuation written
as `%XX`. The catch is that `escape()` counts in UTF-16 code units rather than
characters, which makes an emoji a *pair* of sequences — get that wrong and
every save holding one is quietly refused. A save also holds far more than the
player's state: the label map, the macro map, the script buffer, the line
currently on screen. One of the files this was built against is 11 MB and
holds 774 values worth editing, so what is offered is the game's own
variables, one group per save slot, and not the engine's bookkeeping.

**AliceSoft** puts several different things in the same container, and they
are only told apart once it is unpacked. The game's *global data* — the flags,
counters and text a game keeps across playthroughs — is named and typed, and
is read, edited and written back. The *numbered save slots* are a dump of the
engine's virtual machine: its stack, its call frames, and a heap of tens of
thousands of objects. All of it is read and written back byte for byte, and
one part of it is offered for editing — the frame holding the game's own
global variables. The rest is the engine's own bookkeeping, where a changed
value is likelier to break the save than to help.

Those variables are stored as a list of values and a list of types, in the
order the game declared them, and with no names at all: the names live in the
`.ain` file the game runs. So a slot behaves like a save whose key is in the
game. On its own it offers every value by number; with the game in the library
the names come from the game's own code, and are used only when it lists
exactly as many globals with exactly the same types, which is what says the
two are talking about the same build.

The gallery and music-room lists arrive in a third container. They are a run
of numbers saying which pictures and tracks have been unlocked, with nothing
in the file to say what any one of them is, so they are named and left alone.

The slots come in two series, which matters when picking one to edit. The low
numbers are the save slots offered in the game, each written when the player
asked for it. The high ones are the engine's own history for stepping back
through the story: they are written together in a single moment, and the
points inside them run backwards in game time. Both open the same way — the
difference is that editing a high-numbered one changes a step in the history
rather than a save anybody chose to keep.

None of that layout was worked out by staring at bytes: it is written from
nunuhara's libsys4, the engine reimplementation behind `alice-tools`, which is
where the format is actually described. The file's own five section offsets
are then checked as it is read, so a walk that goes wrong is refused instead
of quietly producing values from the wrong places.

**Artemis** writes three files into one container, and the same split applies:
the engine's settings are a flat list of named values and are edited, while
the save slots and the data kept across playthroughs are a tagged tree whose
nesting this was not able to establish. Reading the settings ends exactly on
the last of the entries the file says it has, which is what says the walk was
right; the other two are named instead.

**Naninovel** hides its saves twice over. A `.nson` is usually not text at all
but a raw deflate stream — no zlib header, no gzip header, nothing announcing
it — so anything expecting one of those refuses the file. Unpacked, it is a
map from a .NET type name to that part of the engine's state, and each state
is not an object but a *string* with the object written inside it. The game's
own variables are down there, so a reader that stops at the outer layer offers
the file's plumbing and none of its contents. Both layers are opened, and the
states are shown by what is in them rather than as the thousands of characters
they are stored as.

An inner text is kept exactly as it arrived unless something in it was
changed, because re-encoding one nobody touched risks spelling it differently
from the way the game did. That, plus matching the deflate settings, is what
lets most saves come back out byte for byte. A save packed by a build that
compressed it differently cannot be, and says so rather than being refused:
it opens, and is checked by reading back what was written and comparing the
values.

Naninovel also shows why dictionaries need care. Unity's own JSON writer
cannot express one, so every dictionary in a Unity game arrives as two
parallel lists of keys and values. Read literally that gives rows called
"values.0" and "values.1"; read as the pairing it is, it gives rows called
`money` and `day`. SaveSync reads it the second way, wherever it appears.

**Unity PlayerPrefs** are a save with no file behind them. On Windows they
live in the registry under the company and product the game was built with,
and SaveSync has always backed them up, exporting the key as JSON. That same
export is what the editor opens, so the two cannot disagree about what a key
contains. Unity hides each preference's name behind a checksum of it, and the
checksum cannot be turned back into anything — but it does not need to be,
since the name is written in front of it. Only the tail is dropped, and only
for display: a value goes back under the name it had, as the kind it was, or
the game would not find it.

**An encrypted Unreal save** is recognised by where it sits — `Saved/SaveGames`
is the engine's own folder, and identifies the save even when the file will
not, since the game's encryption covers the `GVAS` marker along with
everything else.

The key is looked for the way Easy Save's password is, with one difference
that decides the method: Easy Save writes its password as a plain string
beside a marker anyone can search for, while a game encrypting an Unreal save
does it in its own code, with nothing naming it. So it cannot be looked up —
it is looked for, in the game's own binaries, as a key written as text and
then as one compiled in as a plain array of bytes. A key can also be given by
hand in a `unreal.key` file beside the save.

However it arrives, it is accepted only if what comes out actually starts with
`GVAS`. That is what makes searching honest rather than guessing: millions of
candidates can be tried because the save itself says which one was right, and
a search that finds nothing says nothing rather than something wrong. The
search reports how long it has run and can be called off; once a key works it
is remembered against that save, so the next time costs nothing and the game
need not even be installed. The save is locked again exactly as it was found,
so opening one and changing nothing leaves the file untouched.

Not every such game will give its key up. One whose key is assembled at run
time, or kept anywhere but plainly in its own code, will simply not be found —
and is reported as not found, rather than opened with something that looked
close enough.

**Bakin** and **SRPG Studio** saves are named but not edited. Bakin's is an
object stream with nothing in it naming or typing the values. SRPG Studio
encrypts its saves outright — every byte of one is as likely as every other,
so nothing in the file can identify it, and the only thing that can is the
game it sits in. That is what the engine detector is asked for, and when the
game is not in the library SaveSync says so, since the key to such a save
lives in the game's own program and there is no reaching it without one.

**Wolf RPG** saves are scrambled on disk, so SaveSync unlocks one, reads the
values out and locks it back. Names come from the game's own database when it
sits beside the save — level, HP, attack, in the game's own words. A game that
packs that database away still gets every value, numbered instead of named. A
Wolf file with no values in it, such as some games' `System.sav`, is reported
as unreadable rather than opened and guessed at.

**TADS** covers two layouts. The TAD-kit style (picture packs as `.tad` under
`pic/`, plus `system/system.rec`) is recognised from the install folder; the
record file is a line of whitespace-separated tokens padded with NULs to a
fixed size, and SaveSync edits those tokens and writes the same byte length
back — open with no changes is byte-identical. Classic MJR TADS 2/3 image
files (`.gam` / `.t3`) are recognised the same way; their `.t3v` VM state
snapshots are named but not edited, because they are a machine dump rather
than a list of named values.

**Java** desktop titles are recognised from a bundled JVM (`java.dll` with
`jvm.dll` / `awt.dll` next to the launcher), from jars beside a private JRE,
or from the Compose Desktop / Conveyor layout (`bin/` launcher + sibling
`app/*.jar`), including MSIX installs under WindowsApps. Progress is often a
SQLite database (Android Room on the desktop): tables the game owns are
offered cell by cell, Room's own schema tables are skipped, and a no-edit
open that does not touch a page stays bit-identical — after a real edit the
gate checks that every value still round-trips, because SQLite rewrites
pages when it writes.

**WebGL** is the answer for HTML5 / WebGL shells: Unity WebGL exports
(`Build/*.wasm` and friends), plain `index.html` + wasm/data, and NW.js or
Electron packages whose main page is HTML — those are named WebGL rather than
"NW.js", for the same reason Tyrano is named before the wrapper. Saves are
normally JSON beside the game (and LZString / SugarCube when that is what the
page wrote); indent and line endings are kept when they are unambiguous, so an
untouched open/close is byte-for-byte.

It edits files at rest. Nothing is injected into a running game and nothing
attaches to one.

</details>

## Hotkeys

| Key | Action |
|-----|--------|
| `Ctrl+Alt+S` | Toggle the overlay — or, out of game with pending unknown-game detections, browse that queue (configurable in Settings) |

Hotkeys are registered with **pynput**: on Linux they work as a regular user
(X11 or uinput session); on macOS the Accessibility permission is required.

---

## Configuration

Settings live in `%APPDATA%/SaveSync/config.json` (Linux/macOS:
`~/.local/share/SaveSync/`) with debounced writes and validation. Most of what
you tune is in **Settings**; a few behaviours scale automatically from the
machine so capable PCs are not artificially slowed and weaker ones stay usable.

### How adaptive limits work

At runtime SaveSync classifies the host into a coarse tier from **logical CPU
count** and **total RAM** (via `psutil` when present). That tier is **cached
~45 s** so a brief free-RAM dip (game launch, antivirus) does not flip library
chunking / verify pacing on and off. **Currently available RAM** is still read
live for Backup All / Sync All: under ~1.5 GB free, those queues drop to one
job so they do not fight the game for memory.

| Tier | Rough signal | What changes |
|------|--------------|--------------|
| **high** | ≥ 8 logical CPUs and ≥ ~12 GB total RAM | No pause between integrity checks; library page built in one shot; short config/library write debounce (~0.5 s); Backup All / Sync All may run more jobs in parallel (up to 8 / 4) |
| **mid** | typical desktop (≥ 4 CPUs, comfortable total RAM) | Light verify pause (~20 ms); library inserts in chunks of ~16; debounce ~1 s; moderate batch concurrency |
| **low** | few CPUs, or ≤ ~8 GB total RAM | Stronger verify pause (~80 ms); smaller library chunks (~6); debounce ~2 s; Backup All / Sync All capped tightly (often 1–2) |

These are **not** Settings toggles. Batch jobs (Backup All, Sync All,
multiple-add) also **persist progress** and resume after a restart; the sidebar
shows `N/M — name` while they run.

Other automatic I/O habits worth knowing:

- **Filesystem watcher** coalesces save bursts (~5 s, ~8 s when many files are
  pending) before triggering a backup
- **Backup / sync “already current”** uses an mtime + entry-count preflight so
  unchanged games are skipped without rebuilding zip content hashes
- **Config export history** (snapshots created on export, cloud upload,
  pre-import / pre-restore) keeps at most **5** local folders under
  `config_history/`; older ones are rotated out. A sandbox self-check after
  startup verifies that restore still works when the history is full (same
  notification path as backup integrity failures)
- **Pending batch jobs persistence** (`core/pending_batch_jobs.py` / `pending_batch_jobs.json`) —
  tracks active multi-operation batch queues (Backup All, Sync All, Multi-Add)
  and safely restores remaining items after an unexpected restart
- **Page-size crash guard** for oversized custom page sizes uses a tiny sidecar
  file (`page_size_render_guard.json`), not a full rewrite of `config.json`

### Settings reference

| Setting | Default | Description |
|---------|---------|-------------|
| `max_local_backups` | 6 | Max backups per game |
| `backup_retention_days` | 30 | Days before old backups expire |
| `min_kept_backups` | 3 | Always keep at least N newest backups |
| `max_backup_size_mb` | 512 | Max single backup size |
| `save_edit_copies` | 3 | Copies the save editor keeps of one save before writing to it |
| `save_edit_copy_days` | 7 | Days before those copies are dropped. The newest is never dropped for age, and the rule also runs at startup, so it reaches saves nobody has opened since |
| `process_poll_interval` | 1s | Base scan interval (auto-slows in game / when idle) |
| `backup_on_exit` | true | Auto-backup when a game closes |
| `backup_during_game` | false | Periodic in-game backups while playing (interval in Settings) |
| `auto_scan_on_exit` | true | Scan for save paths when a game exits |
| `auto_sync_after_backup` | false | Sync to cloud after each backup |
| `save_correlation_enabled` | false | Claim saves by write-time correlation (see above) |
| `save_correlation_window_ms` | 1000 | How far apart the two writes may be. Weaker candidates get 40% of it |
| `backup_verify_enabled` | true | Check backup archives on a schedule |
| `backup_verify_interval_days` | 7 | How often that check runs |
| `auto_export_config_enabled` | false | Periodically upload an encrypted config pack to the connected sync provider |
| `auto_export_config_interval_days` | 7 | How often that cloud config export runs |
| `page_sizes` | per list | Items per page for library, backups, save editor, reviews (presets 10 / 20 / 50, or custom) |

### Config export & history

From **Settings → Transfer** you can export / import an encrypted
`.savesync` pack (settings, library, optional credentials) and browse
**Configuration History**. Each successful export or cloud upload also writes a
local snapshot; at most five are kept. Restoring or importing first snapshots
the current state (`pre_restore` / `pre_import`) so you can undo.

### Diagnostics

Run with `SAVESYNC_TRACE=1` to log every spawned child process and every
top-level window shown, each with its call site — useful for hunting startup
flashes or stray console windows. Off by default (zero overhead).

The same switch makes the pins report their z-order: every time one is put
back on top, the log says why it was tried, whether the pin still carries the
always-on-top flag, whether it really ended up above the window in front, and
what that window is. That distinguishes "nothing tried to raise it" from "it
was raised and something put it back down".

$env:SAVESYNC_TRACE = "1"
python main.py

---

## License

Released under the [PolyForm Noncommercial License 1.0.0](LICENSE) —
© 2026 [Luke0094](https://github.com/Luke0094).

You may use, modify and share SaveSync freely **for noncommercial
purposes** (personal use, hobby, research, education, nonprofits), keeping
the copyright notice with every copy. Selling this software, or
distributing it as part of a paid product or service, is not permitted.

> Required Notice: Copyright (c) 2026 Luke0094
> (https://github.com/Luke0094/SaveSync-Your-saves-everywhere)
