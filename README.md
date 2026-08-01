# SaveSync

> **Your saves, everywhere.** — Game save manager with cloud sync, versioned backups, and an in-game overlay.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Qt](https://img.shields.io/badge/UI-PySide6%20(Qt6)-41cd52)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

<!-- Drop a screenshot into screenshot/ and update the path below -->
<img width="957" height="636" alt="Immagine 2026-07-22 130446" src="https://github.com/user-attachments/assets/436c34d7-9615-4f62-b9cd-12fed7544932" />

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
  game-name similarity, file analysis, engine-specific paths, and registry saves
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
- **Pre-restore safety backups** — automatic backup before any restore
- **Integrity checks** — each backup is opened and confirmed readable, on
  demand or on a schedule, so a damaged archive is found before you need it
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
  colors, and a three-state tag filter (include / exclude)
- **Smart tag merging** — case- and separator-insensitive ("2D Game", "2d-game"
  and "2DCG"/"2dcg" converge to one canonical tag, self-healing on startup)
- **Web metadata search** — name, description, cover, developer, release date
  and tags scraped from store pages, wikis and forum threads (spoiler-wrapped
  tag lists included), with a merge dialog to pick what to keep
- **Per-game context menu** — backup, restore, sync, open save folder, edit,
  web search, remove

### Experience
- **In-game overlay** — frameless always-on-top card with fade animations,
  notification carousel, cloud-save prompts, and exclusive-fullscreen
  protection (never breaks a game's display mode)
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
│   ├── game_engine.py             # Which engine a game was built with
│   ├── save_editor.py             # Reads/writes save files, keeps the original
│   ├── save_hold.py               # Holds chosen values against the game
│   ├── lzstring.py                # LZString codec (RPG Maker MV/MZ saves)
│   ├── rubymarshal.py             # Ruby Marshal 4.8 (RPG Maker XP/VX/VX Ace)
│   ├── gvas.py                    # Unreal Engine .sav (GVAS)
│   ├── renpy_save.py              # Ren'Py .save (pickle, read never run)
│   ├── lcf.py                     # RPG Maker 2000/2003 .lsd (LCF chunks)
│   ├── sol.py                     # Flash shared objects (.sol, AMF0/AMF3)
│   ├── qsp.py                     # QSP saves (line-based, obfuscated)
│   ├── wolf.py                    # Wolf RPG obfuscation and checksum
│   ├── wolf_save.py               # Wolf RPG values, named from the game's database
│   ├── kirikiri.py                # KiriKiri .ksd (TJS dictionary in UTF-16)
│   ├── es3.py                     # Unity Easy Save 3, including encrypted
│   ├── rags.py                    # RAGS .rsv (.NET objects behind fixed AES)
│   ├── manual_paths.py            # Hand-registered save folders, single or in bulk
│   ├── registry_saves.py          # Windows-registry save locations
│   ├── skip_dirs.py               # Shared skip-list of noise directories
│   ├── backup.py                  # Versioned zip backups, retention, dedup
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

| Condition | Result |
|-----------|--------|
| Only local modified since last sync | Auto-upload |
| Only cloud modified | Auto-download |
| Both modified, same machine | Auto-backup, ask user |
| Both modified, different machine | **Always ask** (even with auto-backup enabled) |
| No local data, cloud has saves | Ask to download (overlay prompt) |
| Unknown game, cloud folder with same name | Overlay prompt: download & add, keep local, or "it's a different game" (own folder) |

---

## Save detection

Candidate folders are scored from multiple signals:

- Keyword match in the folder name (`save`, `savegame`, `checkpoint`, `userdata`, …)
- Game-name similarity in path components
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
| Unity, Godot | `.dat`, `.bin` |
| GameMaker | `.dat` |
| RPG Maker, Unreal, Ren'Py, unknown | none — `.dat`/`.bin` stay engine data |

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
| JSON — Unity, Naninovel, Godot, HTML games, whatever the extension | Saves a game encrypts with a key of its own |
| RPG Maker MV (`.rpgsave`) and MZ (`.rmmzsave`) — they compress differently | One-off containers a single studio uses and nothing else does |
| RPG Maker XP / VX / VX Ace (`.rxdata`, `.rvdata`, `.rvdata2`) | |
| Unreal Engine (`.sav`, GVAS) — UE4 and UE5, including the 5.4 property tag | |
| Ren'Py (`.save`) | |
| RPG Maker 2000/2003 (`.lsd`) — switches, variables, steps | |
| Adobe Flash shared objects (`.sol`), AMF0 and AMF3 | |
| QSP (Quest Soft Player) | |
| Wolf RPG (`.sav`) | |
| KiriKiri / KAG (`.ksd`) | |
| Unity Easy Save 3 (`.es3`), encrypted or not | |
| Twine / SugarCube and other LZString HTML games (`.save`) | |
| RAGS (`.rsv`) — variables, objects, rooms, the player | |
| Any Ruby Marshal file, including engine `.dat` saves | |
| `key = value` text (`.ini`, `.cfg`, `.conf`) | |

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

**Wolf RPG** saves are scrambled on disk, so SaveSync unlocks one, reads the
values out and locks it back. Names come from the game's own database when it
sits beside the save — level, HP, attack, in the game's own words. A game that
packs that database away still gets every value, numbered instead of named. A
Wolf file with no values in it, such as some games' `System.sav`, is reported
as unreadable rather than opened and guessed at.

It edits files at rest. Nothing is injected into a running game and nothing
attaches to one.

## Hotkeys

| Key | Action |
|-----|--------|
| `Ctrl+Alt+S` | Toggle the overlay — or, out of game with pending unknown-game detections, browse that queue (configurable in Settings) |

Hotkeys are registered with **pynput**: on Linux they work as a regular user
(X11 or uinput session); on macOS the Accessibility permission is required.

---

## Configuration

Settings live in `%APPDATA%/SaveSync/config.json` (Linux/macOS:
`~/.local/share/SaveSync/`) with debounced writes and validation.

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
| `auto_scan_on_exit` | true | Scan for save paths when a game exits |
| `auto_sync_after_backup` | false | Sync to cloud after each backup |
| `save_correlation_enabled` | false | Claim saves by write-time correlation (see above) |
| `save_correlation_window_ms` | 1000 | How far apart the two writes may be. Weaker candidates get 40% of it |
| `backup_verify_enabled` | true | Check backup archives on a schedule |
| `backup_verify_interval_days` | 7 | How often that check runs |

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
