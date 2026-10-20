# SaveSync

> **Your saves, everywhere.** — Game save manager with cloud sync, versioned backups, saves editor and an in-game overlay.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Qt](https://img.shields.io/badge/UI-PySide6%20(Qt6)-41cd52)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

<!-- Drop a screenshot into screenshot/ and update the path below -->
<img width="964" height="638" alt="Immagine 2026-08-10 182638" src="https://github.com/user-attachments/assets/243d76b3-32a4-41f6-ab45-b3f7ded85e97" />

SaveSync watches the games you play, finds their save folders on its own, keeps
versioned local backups, and mirrors everything to the cloud provider of your
choice, you can also edit the save the integrated save editor 
— with an always-on-top overlay so you never have to leave the game.
Saves can also go straight to a friend, P2P, with no server or cloud provider
involved at all.

---

## Contents

**New here? Start with what you want to do:**

| I want to… | Go to |
|---|---|
| Keep my saves safe: backups, cloud, restore | [Backups & sync](#backups--sync) · [Sync providers](#sync-providers) · [Conflict resolution](#conflict-resolution) |
| Send a save, a file or a folder to a friend | [P2P save transfer](#p2p-save-transfer) → [Sending](#sending) · [Receiving](#receiving) · [Friends](#friends) |
| Chat with several friends at once | [Group chats](#group-chats) |
| Hide my address when I send | [Hiding your address](#hiding-your-address) · [Private mode (Tor)](#private-mode-tor) |
| Change what is inside a save | [Save editor](#save-editor) |
| Know how a folder is decided to be a save | [Save detection](#save-detection) |
| Install it, or build it myself | [Installation](#installation) |
| Change a setting | [Settings reference](#settings-reference) · [Configuration](#configuration) |
| Read how it works inside | [Architecture](#architecture) · [How the P2P transfer works](#how-it-works) · [Privacy: what the network sees](#privacy-what-the-network-sees) |
| Fix something | [If it doesn't connect](#if-it-doesnt-connect) · [Data-integrity checks](#data-integrity-checks) · [Offline repair](#offline-repair) · [Diagnostics](#diagnostics) |

**Every section:**

| Section | What's in it |
|---|---|
| [Features](#features) | [Detection & tracking](#detection--tracking) · [Backups & sync](#backups--sync) · [Library](#library) · [Experience](#experience) |
| [Installation](#installation) | From source, offline, building the Windows .exe and the Linux AppImage, Linux and macOS, dependencies |
| [Architecture](#architecture) | The project layout |
| [Sync providers](#sync-providers) | Google Drive, OneDrive, Dropbox, WebDAV, rclone and a local folder - and how to add one |
| [Conflict resolution](#conflict-resolution) | What happens when the cloud and this PC disagree |
| [P2P save transfer](#p2p-save-transfer) | Sending, receiving, settings, friends, group chats, speed, TLS, hiding your address, how it works, privacy |
| [Save detection](#save-detection) | How candidate folders are scored |
| [Save editor](#save-editor) | Editing the values inside a save, and the formats it understands |
| [Hotkeys](#hotkeys) | The global shortcuts |
| [Configuration](#configuration) | Adaptive limits, the settings reference, export, integrity checks, offline repair, diagnostics |
| [License](#license) | PolyForm Noncommercial 1.0.0 |

> Long sections open with the short version and keep the detail in **collapsible blocks**: click a block's title to open it.

---

## Features

Four groups. Each one lists what it covers; open it for the details.

| Group | What it covers |
|---|---|
| [Detection & tracking](#detection--tracking) | Spotting running games, finding their save folders, playtime, several exe-path versions per game |
| [Backups & sync](#backups--sync) | Versioned backups, integrity checks, cloud providers, conflicts, restore, P2P transfer |
| [Library](#library) | Cards and lists, search and tags, web metadata and pictures, the context menu |
| [Experience](#experience) | The overlay, the save editor, pinned notes, hotkeys, language and theme, the tray |

### Detection & tracking

**In this group:** Auto-detection of running games · Cheap process-exit watchdog · Heuristic save-folder detection · Real-time filesystem watcher · Unknown-game queue · Proton/Wine on Linux · Launcher URL support · Playtime tracking · Multiple exe-path versions per game

<details>
<summary><strong>Show all 9 features</strong></summary>

- **Auto-detection of running games** via process monitoring, with a cached
  process snapshot (per-process verdicts memoized across polls) and gentler
  polling while a game session is active — near-zero background cost during
  CPU-bound gameplay. A confirmed game in your library is the only thing
  that speeds polling back up; a merely plausible-looking process (anything
  outside a system directory) no longer does, and a candidate that didn't
  pan out — a name-only match, or an unknown process that exited before
  being confirmed — gets a 3-minute cooldown per executable before it's
  evaluated again, so a program that keeps respawning its own short-lived
  helper process isn't re-asked about on every single spawn
- **Cheap process-exit watchdog** — while a game is tracked, a lightweight
  timer (derived from `process_poll_interval`, 0.5–5 s) checks only that
  game's own pid instead of waiting for the next full scan, so closing it
  (final backup, save scan, window restore) starts immediately rather than
  after the poll's session-long backoff. Pressing Play does the same in the
  other direction — it doesn't wait for the next scheduled poll either
- **Heuristic save-folder detection** — scored candidates from folder keywords,
  game-name similarity (`match_slug` keeps letters and digits in any script —
  Japanese, Chinese, Korean, Cyrillic, Greek — not only ASCII), file analysis,
  engine-specific paths, and registry saves
- **Real-time filesystem watcher** — reacts to save changes instantly, even for
  paths that don't exist yet
- **Unknown-game queue** — every unrecognized game is remembered; the overlay
  badge counts pending detections and the notification itself is browsable
  (carousel arrows), no separate window. While nothing is being played the
  same queue is a carousel on the Overview too — add a game or say "don't show
  again" without the overlay hotkey. It is one queue with two views: an answer
  given in either is gone from both
- **Proton/Wine on Linux** — Windows games save inside their compatibility
  prefix, and SaveSync looks there too
- **Launcher URL support** — games launched through `steam://`-style URLs are
  resolved to their real executable
- **Playtime tracking** per game, with per-session detail on hover
- **Multiple exe-path versions per game** — a reinstall, an update, or a
  second copy kept side by side can all be tracked under the same library
  entry instead of forking into a duplicate. Add, switch between, or remove
  versions from Add/Edit Game, or answer the in-game prompt raised the
  moment a tracked game turns up running from a path it hasn't seen before
  (add it as another version, overwrite the primary, or split it into its
  own entry). A version added by mistake can be split off afterwards with 🔀
  in the version menu: on Save it becomes its own card with only its exe, its
  own save location if it has one, and a name taken fresh from the exe. Save
  paths kept inside the install folder are rebased onto a
  new version automatically; if the destination already has its own saves,
  SaveSync asks before either gets overwritten. When the new location has no
  saves yet, replacing the executable brings the saves across (copied, the
  originals stay put) — or offers the latest backup when the old version's
  folder is gone. Switching between tracked versions does the same into an
  empty folder, does nothing when the target already holds every save of the
  version you leave (compared by hash), and when it has different saves of its
  own it asks **as soon as you pick the version** (Overwrite = **Merge saves**:
  every save of the version you leave is brought across, same-named files are
  replaced after a copy of them is set aside, and nothing else is removed, so a
  game's own config can never take the real saves with it). The folders under
  *Your save folders* say what Save will do with each - copied, merged, kept - and
  the copy itself happens on Save, so Cancel leaves everything as it was; an
  unticked folder is never carried. Adding a version beside the current one seeds
  its empty folder

</details>

### Backups & sync

**In this group:** Versioned local backups · Mtime preflight · Adaptive batch queues · Pre-restore safety backups · Verified on arrival · Duplicates view · Integrity checks · Interference alerts · Periodic in-game backups · Provisional backups · Multi-provider cloud sync · Renames and edited notes reach the provider · Conflict resolution · Quick restore from the overlay · Direct P2P transfer

<details>
<summary><strong>Show all 15 features</strong></summary>

- **Versioned local backups** with retention (max count, days, minimum kept,
  size cap) and content-dedup (unchanged saves are skipped)
- **Mtime preflight** — Backup All / Sync skip games whose saves clearly have
  not changed (newest mtime + file count vs the last backup), before rebuilding
  content hashes
- **Adaptive batch queues** — Backup All / Sync All run with a concurrency
  limit derived from CPU and RAM, with sidebar progress (`N/M — name`) and
  resume after an app restart
- **Pre-restore safety backups** — automatic backup before any restore. Like
  provisional (unconfirmed) backups they form a side branch: their own
  retention, never counted against the main history, never picked as the
  newest restore target, never uploaded
- **Verified on arrival** — every new or downloaded backup is integrity-checked
  as it lands (green dot from the start); one scheduled sweep covers the rest,
  skipping archives already verified within its interval
- **Duplicates view** — the 🔁 button at the far left of the Backups pager
  narrows the list to titles that appear more than once (a library game and an
  archive of it, a tagged twin), side by side, so the extra one can be found and
  deleted. Adding a folder whose backup folder name is already taken compares
  its files with that history by hash and, if they are not in it, lists what is
  stored under the name (each with its last backup) and asks: add to it (another
  version, restorable by hand, kept beside a library game's own history), keep
  both (its own tagged archive) or don't ask again — with an extra "which one?"
  step when several are stored. A shared name is never taken as proof of one
  game; only "don't ask again" is remembered, and it is listed per game in Settings
- **Integrity checks** — each backup is opened and confirmed readable, on
  demand or on a schedule, so a damaged archive is found before you need it;
  sweeps throttle only on weaker machines, skip archives still marked OK
  recently, and flush index files once per game
- **Interference alerts** — when something outside SaveSync puts an older save
  state back (a launcher's automatic sync, say), it says so, and can force the
  restore with the game frozen. A save written by the game's own process since
  it started — some engines rewrite their own settings file just from opening —
  is never mistaken for that; only a difference that already existed before
  this session counts
- **Periodic in-game backups** — configurable per-game interval while playing
- **Provisional backups** — pre-confirmation saves are protected from the very
  first session, before you've even confirmed the paths
- **Multi-provider cloud sync**: Google Drive (OAuth, Service Account, Desktop
  folder), OneDrive (MSAL device flow), Dropbox (OAuth PKCE), WebDAV
  (Nextcloud, ownCloud, Box…), rclone (MEGA, S3, SFTP, B2, pCloud, 40+ remotes),
  local/NAS folder
- **Renames and edited notes reach the provider** — changing a game's title
  moves its backup folder (locally and on every connected provider, index and
  zips together, the old folder removed) on the next sync, and a sync that
  finds the saves unchanged still republishes an index that changed, so a
  rename or an edited note no longer waits for a new backup. Another machine
  that still uses the old name is told when it launches the game: update the
  name there too, or keep both (its own cloud folder is then started over)
- **Conflict resolution** with per-machine detection: cross-machine divergence
  always asks (keep local / keep cloud / keep both)
- **Quick restore from the overlay** — browse local *and* cloud backups without
  leaving the game; cloud entries download transparently
- **Direct P2P transfer** — send a specific backup, or any file or folder,
  straight to another SaveSync install, no server or cloud provider involved,
  encrypted between the two of you. Backups arrive in the Backups page; files go
  straight into a folder you choose, with OK / Move to… at the end. A friend
  list hands your one-time token over in a click, writes with a little
  formatting, and offers files, folders and videos that download like any other
  transfer (see
  [P2P save transfer](#p2p-save-transfer))

</details>

### Library

**In this group:** Card and list views · Paged lists · Smart tag merging · Web metadata search · More pictures · Languages · Per-game context menu

<details>
<summary><strong>Show all 7 features</strong></summary>

- **Card and list views** with search (by title or developer), folder tree with
  colors, star ratings from reviews, and three-state filters (include /
  exclude) for tags, engine and languages, picked from one dropdown that
  shows how many chips each holds. Languages: include needs all of the ones
  chosen, exclude drops a game that has any, Unknown covers games listing none
- **Paged lists** — only the current page of cards/rows is built; on capable
  machines the page is filled in one go, on weaker ones in small chunks so the
  UI stays responsive
- **Smart tag merging** — case- and separator-insensitive ("2D Game", "2d-game"
  and "2DCG"/"2dcg" converge to one canonical tag, self-healing on startup)
- **Web metadata search** — name, description, cover, developer, release date,
  tags, languages and reviews/ratings scraped from store pages, wikis and forum
  threads (spoiler-wrapped tag lists included). Nothing is applied on its own: a
  chip dialog lists every source (the one you confirmed included). Fields with a
  value start on "keep current" (an empty one on the fetched value); new tags,
  languages, covers, reviews and page links start selected, and each source's
  header clears or restores its whole selection. Only what is selected is written
- **More pictures** — Steam, VNDB, itch.io, DLsite and forum threads (the
  pictures posted in the first message) also list screenshots.
  The chip dialog shows them beside the cover, selected like the rest (untick
  what you do not want); the ticked ones, covers included, are downloaded in the
  background and added to the carousel. The panel stays usable and Save does not
  wait (rename a new game meanwhile and what came in moves to its new folder, the
  rest goes straight there): what is still coming goes on into the saved game, without the panel (a
  cover that arrives after Save becomes its cover, unless you picked another or
  changed it since; a picture that fails is reported in the status bar), and
  Cancel before Save drops it.
  A screenshot never replaces the cover, and Cancel removes only what that session added
  A source applied before still shows its preview while it has pictures the game
  lacks (worked out from the cache's file names, nothing is downloaded to know)
- **Languages** — DLsite, Steam and VNDB say which languages a game is offered
  in. The candidate card shows up to three (+N more); the chip dialog offers a
  source's languages as one chip with the count; the add/edit panel shows the
  result beside the version — English when the game has it, with the full list
  one per line on hover. Clicking it lists them with a bin on each (shown on
  hover), for the ones a regional store offers and your copy lacks, and an **Add a
  language…** row at the bottom: pick one from the list (the ones the sources name, and every
  language any of your games lists) or type your own - a language no source knows. A game with
  none shows an **Add language** chip that opens the same menu. Kept on Save only; the library's
  language filter lists what you add
- **Per-game context menu** — backup, restore, sync, open save folder, edit,
  web search, remove

</details>

### Experience

**In this group:** In-game overlay · Save editor · Pinned notes and images · Global hotkeys · Live language switching · Dark / Light theme · Secure credential storage · System tray

<details>
<summary><strong>Show all 8 features</strong></summary>

- **In-game overlay** — frameless always-on-top card with fade animations,
  notification carousel, cloud-save prompts, running game with engine label,
  and exclusive-fullscreen protection (never breaks a game's display mode)
- **Save editor** — open a game's save and change the values in it, with the
  original kept aside first; reachable from the sidebar, the library's context
  menu, straight to the running game, or by dropping a save file on it. Kept
  copies have a restore panel of their own: each is tagged (before an edit, or
  the file as it stood just before a restore — a restore keeps the current
  state first, so it can be undone from the same list)
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

</details>

---

## Installation

Run it from source, or build an executable. Each part below is its own block:

| Part | What it covers |
|---|---|
| [From source](#from-source) | Run it from the repository (Python 3.10+) |
| [Offline installation](#offline-installation) | Install on a machine without internet access |
| [Building the Windows executable](#building-the-windows-executable) | Build the .exe with PyInstaller |
| [Building the Linux AppImage](#building-the-linux-appimage) | Build the AppImage |
| [Linux and macOS](#linux-and-macos) | Running it on those systems |
| [What Linux needs installed](#what-linux-needs-installed) | The three packages that decide whether it looks right |
| [Dependencies](#dependencies) | Every package and what it is for |

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

<details>
<summary><strong>Offline installation steps</strong></summary>


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

</details>

### Building the Windows executable

<details>
<summary><strong>Build the Windows .exe</strong></summary>


```bash
pip install pyinstaller
python tools/fetch_tor.py            # once: the Tor program private mode runs (into vendor/tor)
pyinstaller --clean savesync.spec
```

`tools/fetch_tor.py` downloads the Tor Project's own program (about 10 MB), refuses it unless its
SHA-256 is the one pinned in the script, and the spec packs `vendor/tor` into the build. A build made
without it still works; private mode then says the Tor program is missing and sends nothing.

Output: `dist/SaveSync-<version>-win-<arch>.exe` (e.g.
`SaveSync-1.4.1-win-AMD64.exe`) — single file, animated splash, no console.
Always pass `--clean` — the spec injects custom Tcl into the splash and stale
build caches would ship the old version.

</details>

### Building the Linux AppImage

<details>
<summary><strong>Build the Linux AppImage</strong></summary>


```bash
pip install pyinstaller
./packaging/build_appimage.sh
```

Output: `dist/SaveSync-<version>-<arch>.AppImage` — one file, executable,
no installation. `appimagetool` is fetched into `build/` if it is not on
PATH; nothing is installed system-wide. The script also fetches the Tor program
for private mode (`tools/fetch_tor.py linux-x86_64`) if `vendor/tor` lacks it.

**Why the two end up different sizes:** both are "one file you double-click",
but they get there differently. The Windows build is PyInstaller's *onefile*
mode — UPX (LZMA) compresses each DLL individually, then everything is
packed into one `.exe` that unpacks itself into a temp folder at launch. The
Linux build is deliberately *onedir*, not onefile — an AppImage already
extracts/mounts itself when it runs, so a onefile build inside one would mean
extracting twice on every launch. PyInstaller instead lays out a plain,
uncompressed directory tree, and `squashfs` (zstd) compresses that whole tree
into the single `.AppImage` as the last packaging step. Per-binary UPX vs.
whole-tree squashfs, over what can also be differently-sized upstream
binaries between platforms (e.g. numba's bundled LLVM), is why the two
outputs aren't a fair byte-for-byte comparison even when both carry the same
dependencies.

</details>

### Linux and macOS

The application itself is portable: nothing imports a Windows module at
module level, the data directory follows XDG (`$XDG_DATA_HOME/SaveSync`,
`~/Library/Application Support/SaveSync` on macOS), single-instance uses
`flock` where there is no named mutex, launch-at-login writes an XDG
`.desktop` or a LaunchAgent, folders open through `xdg-open` / `open`, and
the global hotkey goes through pynput rather than a Windows hook.

<details>
<summary><strong>Linux and macOS in detail</strong></summary>


Two things are Windows-only **by nature**, and degrade rather than fail:

| | On Windows | Elsewhere |
|---|---|---|
| Credential protection | DPAPI, tied to the account | the OS keyring when one answers, otherwise a key derived from a salt file — with the salt, the credential blob and its copy all written **0600** |
| Token file hardening | `icacls`, ACL narrowed to the user | `chmod 0600` |

**Windows games run through Wine or Proton.** A `.exe` is not something a
Linux kernel can execute, so off Windows SaveSync launches it through the
runtime it belongs to: Proton when the executable lives inside a Proton
prefix (`steamapps/compatdata/<appid>/pfx`), with `STEAM_COMPAT_DATA_PATH`
pointed at that prefix; Wine otherwise, with `WINEPREFIX` set to whichever
prefix the executable sits in. That last part decides where the game's
saves go — run with the wrong prefix it finds no registry, no runtime, and
writes its saves somewhere new.

**No system tray?** GNOME ships without one, and so does a bare
compositor. SaveSync notices at startup and stops pretending: closing the
window **asks** whether to minimise or quit instead of deciding (with
minimize-to-tray off it still quits without asking), **Ctrl+Q** quits
(everywhere — it is simply the only way out where there is no tray menu), and starting a game minimises
the window rather than hiding it into a tray that is not there — the same
setting, the same restore when the game exits.

**Save locations searched on Linux:** `$XDG_DATA_HOME`, `$XDG_CONFIG_HOME`,
`~/.renpy` (Ren'Py writes there, outside XDG entirely), `~/Documents`,
`~/snap`, `~/.var/app` (Flatpak), Steam's `userdata` under both `~/.steam`
and `~/.local/share/Steam`, and the user directories inside every Wine and
Proton prefix it can find — plain `~/.wine`, `$WINEPREFIX`, Bottles,
Heroic, Lutris.

</details>

### What Linux needs installed

Three packages decide whether SaveSync looks right, and none of them is a
hard dependency — the application starts without them and misbehaves in
ways that look like bugs. The AppImage bundles the fonts; from source they
have to be there.

| Package (Debian/Ubuntu names) | Without it |
|---|---|
| `libxcb-cursor0` | Qt cannot load its xcb platform plugin and the app does not start at all |
| `fonts-noto-color-emoji` | every emoji in the interface — folder, disk, bin, refresh — is an empty box |
| `fonts-noto-cjk` | Japanese, Chinese and Korean titles and paths are empty boxes |
| `xdg-utils` | every "open folder" button does nothing (SaveSync falls back to `gio`, `kde-open` or `exo-open` when one of those is present) |

### Dependencies

<details>
<summary><strong>The dependency table</strong></summary>


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
| lz4 | Wolf RPG's LZ4-compressed save variant |
| numpy | Vectorised key search for that same variant |

</details>

---

## Architecture

For contributors: where the code lives.

<details>
<summary><strong>Project layout</strong></summary>


```
savesync/
├── main.py                        # Entry point, single-instance lock, startup tracing
├── runtime_splash_hook.py         # Bootloader-time single-instance mutex
├── savesync.spec                  # PyInstaller build (animated Tcl splash)
├── requirements.txt               # Pinned runtime dependencies
├── install_offline_deps.bat/.sh   # Install from a downloaded wheel cache
├── download_offline_deps.bat/.sh  # Fetch that cache on a connected machine
├── core/
│   ├── constants.py               # App identity, paths, folder-name rules
│   ├── concurrency.py             # Adaptive limits (poll rates, chunk sizes, sweeps)
│   ├── config_manager.py          # JSON config with debounced writes and validation
│   ├── config_transfer.py         # Config export / import, cloud config history
│   ├── library.py                 # Game library CRUD, tag canonicalization
│   ├── monitor.py                 # Process monitor (cached snapshots, adaptive polling)
│   ├── watcher.py                 # Real-time filesystem watcher, debounced events
│   ├── save_detector.py           # Heuristic save folder detection and scoring
│   ├── exe_stems.py               # Shared executable-name vocabulary (one list, three passes)
│   ├── engines/                   # Engine recognition + binary/format readers
│   │   ├── game_engine.py         # Which engine a game was built with
│   │   ├── gvas.py                # Unreal Engine .sav (GVAS)
│   │   ├── renpy.py               # Ren'Py .save (pickle, read never run)
│   │   ├── renpy_save.py          # Back-compat shim re-exporting renpy.py
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
│   │   ├── alicesoft_vsf.py       # AliceSoft VSF save-slot summary (date, scene, playtime)
│   │   ├── artemis.py             # Artemis Engine settings (BOWX container)
│   │   ├── rags.py                # RAGS .rsv (.NET objects behind fixed AES)
│   │   ├── wolf.py                # Wolf RPG obfuscation and checksum
│   │   ├── wolf_lz4.py            # Wolf RPG's LZ4 variant — key + swap, closed-form
│   │   ├── sqlite_db.py           # SQLite save databases (Room / Java)
│   │   ├── playerprefs.py         # Unity PlayerPrefs registry export
│   │   ├── tads.py                # TADS system.rec slots
│   │   ├── keyvalue.py            # key = value text configs
│   │   ├── xml_save.py            # Plain XML saves
│   │   ├── protobuf_raw.py        # Schema-less Protocol Buffers (generic, no magic bytes)
│   │   ├── struct_header.py       # Generic reader for a fixed, unlabelled binary header
│   │   └── known_headers.py       # Known fixed-header layouts, one per game (data for struct_header.py)
│   ├── save_editor/               # Save-file editor: orchestration + adapters
│   │   ├── save_editor.py         # open_save, detection, backups of edits
│   │   ├── registry.py            # One description per format: extension, magic, engine
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
│   │   ├── wolf_lz4_format.py     # Wolf RPG Editor, the LZ4-compressed variant
│   │   ├── alicesoft_format.py    # AliceSoft System 4
│   │   ├── alicesoft_vsf_format.py # AliceSoft VSF save-slot summary
│   │   ├── artemis_format.py      # Artemis
│   │   ├── tyrano_format.py       # TyranoScript
│   │   ├── tads_rec_format.py     # TADS TAD-kit system.rec
│   │   ├── sqlite_format.py       # SQLite (Room / Java desktop)
│   │   ├── protobuf_format.py     # Schema-less Protocol Buffers, reached via "Open as…"
│   │   ├── struct_header_format.py # Known fixed-header save, auto-detected by layout match
│   │   ├── steamid_aes_format.py  # Steam saves keyed by the player's own SteamID64
│   │   ├── recipe_format.py       # Generic unwrap recipes for unrecognised saves
│   │   └── crypt/                 # Decryptors used only by the editor
│   │       ├── unreal_crypt.py    # Unreal saves locked with the game's own key
│   │       ├── es3.py             # Unity Easy Save 3, including encrypted
│   │       ├── wolf.py            # Wolf unlock + variable database
│   │       ├── wolf_lz4.py        # Same database, reached through the LZ4 variant's lock
│   │       ├── steamid_aes.py     # AES-256 keyed from a SteamID64 (generic reader)
│   │       ├── steamid_aes_recipes.py # Per-game recipe table for the reader above
│   │       ├── recipes.py         # XOR/LCG keystreams x decompression, tried and gated cheaply
│   │       ├── game_keys.py       # Remembered decrypt keys, per game
│   │       └── unityfs.py         # Unity asset bundles, unpacked to find keys
│   ├── manual_paths.py            # Hand-registered save folders, single or in bulk
│   ├── registry_saves.py          # Registry save locations (Windows & Wine/Proton on Linux)
│   ├── skip_dirs.py               # Shared skip-list of noise directories
│   ├── backup.py                  # Versioned zip backups, retention, dedup
│   ├── pending_batch_jobs.py      # Persisted multi-operation batch queues
│   ├── p2p/                       # Direct P2P save transfer (BitTorrent + DHT mailbox)
│   │   ├── __init__.py
│   │   ├── transfer.py            # ReceiveSession, send_backup, send_files — see "P2P save transfer" below
│   │   ├── secret.py              # Keys and sealing derived from a token or a friendship
│   │   ├── friends.py             # Friend codes, per-direction mailboxes, the encrypted friend file
│   │   ├── friend_net.py          # DHT session that reads/writes the friends' mailboxes
│   │   ├── groups.py              # Group chats: members, roles, invitations, the queue - over the same pairwise mailboxes
│   │   ├── tls.py                 # Optional TLS for transfers: the certificate authority derived from the token
│   │   └── paths.py               # Folder received files are downloaded into (Settings)
│   ├── resolvers.py               # Launcher URLs, executable resolution
│   ├── exe_scan.py                # Folder scan for installed game executables
│   ├── game_api.py                # Web metadata search orchestration
│   ├── game_sources/              # Steam, VNDB, wikis, store/forum scrapers
│   ├── enrichment.py              # Fills missing metadata on found titles
│   ├── net.py                     # Shared HTTP session and retry policy
│   ├── credentials.py             # Secure credential store (keyring + AES fallback)
│   ├── machine.py                 # Machine fingerprint for cross-machine detection
│   ├── self_checks.py             # Data-integrity checks (one list, two callers)
│   ├── stall_watchdog.py          # Logs where the GUI thread is stuck when it stops answering
│   ├── update_check.py            # GitHub Releases check for a newer build
│   └── startup.py                 # Autostart, directory setup, migrations
├── sync/
│   ├── __init__.py                # Provider registry, orchestrator, conflict detection
│   ├── base.py                    # Abstract SyncProvider
│   ├── app_credentials.py         # Default OAuth client IDs for zero-config setup
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
    ├── styles/                    # Theming
    │   ├── theme.py               # Dark/Light QSS theme manager
    │   ├── dark.py                # Dark theme (QSS + palette)
    │   ├── light.py               # Light theme (QSS + palette)
    │   └── arrow_icons.py         # SVG chevrons for stylesheets (Fusion-safe)
    ├── pages/                     # Overview, Library, Sync, Backups, Settings,
    │                              # Save editor
    ├── widgets/                   # Game cards/rows, path rows, folder tree, file
    │                              # list, cover editor, hotkey editor, pickers,
    │                              # busy overlay, pinned notes, screen capture
    └── dialogs/                   # Add/edit game, auto-scan, restore, conflicts,
                                   # exe scan, manual paths, game search, cloud
                                   # verify, config import, P2P send/receive, credits
├── tools/                        # Build-time asset generation, nothing else
│   ├── generate_icon.py           # Build the multi-size app .ico
│   ├── generate_splash.py         # Build the static splash PNG
│   ├── splash_animated.py         # Build the animated splash GIF
│   └── signature.py               # Animated splash variant with the 3D flip reveal
└── maintenance/                  # Offline repair of stored data — see below
    ├── repair_archives.py         # Diagnose, and only on request repair, archive indexes
    └── test_recipe_format.py      # Standalone verification for the unwrap recipe battery
```

The four directories shown as a single line — `core/game_sources/`, `ui/pages/`,
`ui/widgets/` and `ui/dialogs/` — hold one module per source, page, widget and
dialog respectively; they are summarised rather than listed because their
contents are exactly what their names say and the list would be longer than
the rest of the tree put together.

</details>

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

<details>
<summary><strong>The decision table and what each option does</strong></summary>


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
machine, so this one moves to its own folder (`Alpha~7f31c0`) with its backups, and
the two stop sharing a destination. Not offered once the two sides have synced
together — that already settles whose folder it is.

A save that goes *backwards* without SaveSync doing it (a launcher's own cloud
sync, another tool) is reported separately: it isn't a conflict, so the prompt
offers to restore the newest backup, or to back up the current state from the dropdown.
Closing it unanswered leaves the game on temporary backups until you choose.

</details>

---

## P2P save transfer

Sends a save — or any file or folder — straight to another SaveSync install,
peer to peer. No server either side has to run, and no cloud provider involved
at all; the two people need nothing in common but SaveSync (1.4.6 or later, on
both sides). It is encrypted between the two of you — see
[Privacy](#privacy-what-the-network-sees).

There are two things you can send, and they end up in different places:

| You send | It arrives | Tracked by SaveSync? |
|---|---|---|
| **A backup** — ➡️ on a row of the Backups page | In the receiver's **Backups** page, as an archive with a `📥 P2P` badge, ready to restore | Yes, like any other backup, including the retention below |
| **A file or folder** — **Send a file…** in the Receive panel | Straight into a folder the receiver picks in Settings | No. Never in Backups, never listed, never deleted by SaveSync |

**In this section:**

| Part | What it covers |
|---|---|
| [Sending](#sending) | Give the receiver's token, pick what to send |
| [Receiving](#receiving) | Get a token, accept an offer, where the files go |
| [Settings](#settings) | The P2P settings and what each one does |
| [Friends](#friends) | Add people by code, chat, hand over tokens, send files, lock the list, link another PC |
| [Group chats](#group-chats) | Chat with several friends: roles, invitations, taking people out, attachments, what it does not promise |
| [If it doesn't connect](#if-it-doesnt-connect) | Why two PCs fail to meet |
| [Speed](#speed) | What decides the speed of a transfer |
| [TLS (optional)](#tls-optional) | Swapping BitTorrent's own encryption for TLS |
| [Hiding your address](#hiding-your-address) | Going through a SOCKS5 proxy |
| [Private mode (Tor)](#private-mode-tor) | Going through Tor, so nobody sees your address |
| [How it works](#how-it-works) | The mailbox, the meeting point, the connection |
| [Privacy: what the network sees](#privacy-what-the-network-sees) | What stays hidden and what does not |

### Sending

1. The receiver opens 📥 (bottom left of the sidebar) and gives you the token
   it shows — pasted, sent to you in one click if you are friends, or, if you are
   friends, **asked for from the send window itself** with 👥 (see
   [Friends](#friends)).
2. Pick what to send:
   - **A backup:** click ➡️ on it in the Backups page.
   - **A file or folder:** open 📥, press **Send a file…** and pick it with the
     app's own picker. A page lists every file inside; untick the ones that
     shouldn't leave (at least one stays on). It goes as it is — not zipped,
     with no game or backup entry attached.
3. Paste the token and press **Send**.

SaveSync then seeds the transfer until it's picked up. The window shows how
many peers are connected, the upload speed (MB/s, with Mbps on hover) and a
percentage where 100% means one full copy has gone out. Closing it before then
asks "Close anyway?", and closing it ends the transfer.

**Nothing is copied to be sent.** The file is *hard-linked* under a temporary
name, not duplicated, so sending 50 GB doesn't need another 50 GB of disk. That
needs a drive that supports links (NTFS, ext4, APFS…); on one that doesn't
(exFAT, a network share) SaveSync says so rather than quietly making a copy. The
one catch: a link is not a snapshot, so a file that is still being written
*while* it's sent can fail the receiver's integrity check. A backup archive
never changes; for your own files, send ones that are at rest.

### Receiving

Open 📥 to get a one-time token and give it to whoever is sending. When they
send, the panel shows who and what — for a backup the game, its folder and the
file (`"<name> wants to send you save.zip"`), for files the folder and how many
— and **nothing downloads until you accept**. You can also decline.

<details>
<summary><strong>Receiving in detail</strong></summary>


- A token works **once**, and a used one is no longer shown or copyable.
- A token is good for **10 minutes from the moment it was made**; the panel
  counts the time down and, if it runs out unused, shows a new token and says
  so. Close the panel and the same token carries on until then; the panel
  reopens by itself when something arrives. A download you've started keeps
  running with the panel closed, and a tray message tells you when it's done.
- While downloading you see a percentage and the speed; a transfer of several
  files also lists each file with its own percentage and speed. It doesn't
  read 100% until the last byte is in and written to disk.

**A backup** is filed into the Backups page as an archive — the same
no-library-game shape "Aggiungi percorso" already produces — tagged with its
own `📥 P2P` badge, restorable immediately. The downloaded file is *moved* into
your backup folder, not read into memory and copied, so it never exists twice,
and the emptied download folder is removed. Filing it doesn't freeze the
window. No need to add the game to the library or match it to one up front: the
same automatic name-matching that already links an unrecognised cloud archive
to a game handles this identically.

**Files and folders** are downloaded *directly into the folder set in
Settings* — by default SaveSync's own `p2p_files` folder next to its config.
Before a byte is fetched SaveSync works out where every file will go and tells
the download to write it there, so there is no staging area and no copy
afterwards: 50 GB takes 50 GB of space, not 100.

- The sender's layout is kept (`Slot1/data/save.bin` arrives as
  `<your folder>/Slot1/data/save.bin`), and folders that already exist are
  merged into.
- **Nothing is ever overwritten.** A name that's taken keeps both files; the new
  one becomes `name (2).ext`.
- A name that tries to climb out of the folder (`..`) is refused.

When it's done the panel says *Your files have been downloaded in:* with the
path, a 📁 button that opens it, and two buttons under it:

- **OK** — dismisses the note. The files stay where they are.
- **Move to…** — moves them to another folder: a real move (a rename on the same
  drive; across drives each file is copied and the original removed), with the
  same layout and the same "(2)" rule there. Only folders the transfer itself
  created are tidied away; ones that were already there are left alone.

That note lives in memory only. SaveSync keeps no list of received files and
never deletes them.

</details>

### Settings

| Setting | Where | Default | What it does |
|---|---|---|---|
| `p2p_username` | Behaviour → *P2P display name* | empty | The name the receiver sees in your offer |
| `p2p_max_per_game` | P2P → Received saves → *Archives to keep per game* | 3 | Received **saves** beyond this are pruned, oldest first |
| `p2p_retention_days` | P2P → Received saves → *Delete after* | 7 | Received **saves** older than this are pruned (the newest is always kept) |
| `p2p_files_dir` | P2P → Received saves → *Folder for received files* | empty = `<SaveSync data>/p2p_files` | Where files and folders sent by hand are downloaded. *Browse…* picks one, *Default* goes back |
| `p2p_port` | P2P → Connection → Direct → *Connection port* | 0 (*Auto*) | The port used to send and receive. Auto picks a free one each time; otherwise 1024–65535 |
| `p2p_upnp` | P2P → Connection → Direct → *UPnP* | on | Asks the router to open that port by itself (UPnP / NAT-PMP) |
| `p2p_friends_online` | P2P → Friends → *Check friends for tokens in the background* | on | Keeps SaveSync listening for the tokens friends send while it runs; off = only while the Friends panel or the Receive window is open |
| `p2p_friends_autolock_minutes` | P2P → Friends → *Lock friends page after* | 10 | Minutes the friend list may sit unused before it locks itself (0 = never); only applies when a lock password is set |
| `p2p_friends_media` | P2P → Friends → *Play pictures and videos from friends inside the chat* | on | Whether **Show picture**, **Play video** and **Watch while downloading** exist on a received attachment. Off = SaveSync never reads a file somebody else made, in any way |
| `p2p_tls` | P2P → Connection → Direct → *Use TLS for transfers* | off | Sends over TLS instead of BitTorrent's RC4, with no fallback; needs a reachable port, and is not used while the proxy is on; see [TLS](#tls-optional) |
| `p2p_proxy`, `p2p_proxy_host`, `p2p_proxy_port` | P2P → Connection → *SOCKS5 proxy* | off (the chosen connection is *Direct*) | Sends every P2P connection (friends and transfers) through that proxy, or none at all; see [Hiding your address](#hiding-your-address). The proxy's login is kept encrypted on this PC, not in `config.json` |
| `p2p_proxy_local_dns` | P2P → Connection → *If the proxy cannot look up names, let this PC's DNS do it* | off | Behind a proxy that carries UDP but not a name lookup: off = P2P waits (nothing is looked up on this PC); on = this PC's DNS looks up the DHT's starting server's name, and so learns you use P2P |
| `p2p_tor` | P2P → Connection → *Tor (private)* | off | Friends and transfers go through Tor (the program comes with SaveSync) and the DHT is not used at all; both ends need it. One connection at a time (Tor wins if a config has both set), and not with TLS; see [Private mode](#private-mode-tor) |

The port and UPnP apply from the next transfer; a session that's already open
keeps what it started with. The proxy applies at once to the friends' chat and to
a token that is waiting (it is replaced by a new one); a transfer that is already
running finishes the way it began.

**Retention** is a separate cap from the regular backup policy, since a save
someone keeps sending you isn't the same kind of history as your own games'
backups. It is shared across whoever sends that game rather than counted per
sender, and runs on the same scheduled sweep the regular backup retention does.
It only ever touches received *saves* — files and folders sent by hand are
outside it.

### Friends

Instead of pasting a token into another app, add the people you send things to
as friends: write to each other (with a little formatting), offer each other
files, and hand each other tokens in a click. 👥 in the sidebar, right after 📥,
slides the list out of the sidebar, over the page beside it (click 👥 again, ✕ or
press Esc to put it away). It stays up when the window is minimized, put away to the
tray, or another program (a game, say) has the focus; the chat is then not being
read, so a new message is announced like any other and stays unread until you are
back. Switching the theme or the language changes it in place: a draft and the
picture you opened stay as they were.

**Covers:** Your friend code · Handing over a token · Asking a friend for a token · Messages · Formatting and links · Attachments · Announcing · Nothing lost · Badges · Stopping a transfer · A token's time · Staying reachable · Removing · Locking the list · Forgot the lock password? · Another PC

<details>
<summary><strong>Friends in detail</strong></summary>


- **Your friend code** is a 56-character string that identifies you to your
  friends; the Friends panel shows it with a **Copy** button. Give it to a
  friend and add theirs with **Add** (a name is optional). **Each of you has to
  add the other**: until they do, the entry reads *waiting for them to add you*,
  and it turns *connected* as soon as something of theirs is seen. Your name
  for friends is the P2P display name in Settings.
- **Who is there.** A **green** dot on each friend (and under their name in the chat, with the
  word *online*) means their SaveSync has been heard from lately; **red** means it has not
  (the tooltip says when they were last seen). While SaveSync runs it writes each friend's mailbox
  again every 3 minutes whether or not anything changed, and a friend who has not been seen to
  write for 7 minutes (two beats missed) is shown as gone - so closing SaveSync turns the dot red
  within a few minutes, not at once, and a friend on a version before this one (which writes every
  45 minutes) reads red most of the time. In private mode (Tor) the dot is the live link itself.
  Nothing is shown as online while your own network is not running, is blocked, or cannot reach the
  DHT, and never while the list is locked. The same dot is on the friends in the Receive and Send
  menus. People you only know through a group get no dot and no extra writes.
- **Handing over a token.** In the Receive panel, **👥 Friends** on the right of
  *Copy* lists your friends; pick one and the token goes to them directly. It
  arrives as a card in that friend's chat, with a countdown. **Press *Send…* on it,
  pick what to send, and the transfer starts** — nothing to paste. For a backup, the send
  dialog (➡️ on a backup) offers the tokens friends have sent in a drop-down.
- **Asking a friend for a token.** The other way round: in the send window (➡️ on a
  backup, or **Send a file…**), **👥** next to the token box lists your friends;
  pick one and they get a *request* in their chat — "Alice wants to send you
  Game — 2026-10-19" — with **Accept** and **Decline**. Nothing is sent before
  they press Accept. Accept makes a new token for that request and hands it to you; it
  fills in your send window, where the send starts at once, with no further click; they then accept the incoming save in their Receive panel as usual. They get the
  request while their SaveSync is listening (*Check friends for tokens in the background* is on
  by default; with it off, only while their Friends panel or Receive window is open) and the
  list is not locked. The
  usual rules hold: the token is good for 10 minutes from when it was made, for the
  first contact only, and the request itself lapses after 10 minutes too. The token is
  made *for that friend*: it is tied to your friendship by a key only the two of you have,
  so someone who gets hold of the token - or a friend who passes it on - can neither find
  the transfer nor read it nor answer it, and a request never gets a token that was
  already out. (If a token they gave another friend is still waiting for them, Accept says
  so rather than take it away.) A token you copy from the Receive panel, or hand over with
  *Send to friend*, is not tied like that: it is a secret that whoever holds it can use
  once. You can ask a friend once a minute; they
  turn away what comes faster, with a little allowance for how often they look, so at
  worst about one and a half a minute gets through. Closing the
  send window takes a request back (their card goes); Decline, a request that is not
  answered, and a friend who is busy receiving something else are each told in the
  window. Only the name shown on the request (what you are sending, cut to 64
  bytes) is shown to them. Both of you need this version: an older one ignores
  requests, and the window then says there was no answer. One request per friend
  is live at a time; a newer one replaces the last.
- **Messages.** Click a friend to open their chat and write in
  the box under it: Enter sends, Shift+Enter starts a new line. (The **⋯** on a
  friend's row renames or removes them.) A message is up
  to 400 bytes (about 400 letters; fewer in scripts that need more bytes per
  character, the counter says). Tokens they hand you sit in the same conversation
  as cards, with their countdown and a **Send…** button: each friend's tokens are
  in that friend's chat, there is no separate list of them. Each of your texts shows how far it got: ⏳ waiting to go out, ✓ stored
  in the network for them to pick up, ✓✓ **delivered** (their SaveSync has it).
  The conversation is kept on this PC, in the friend file (encrypted like the rest
  of it), up to 300 entries per friend; **Clear** deletes it here — your friend's
  copy is theirs.
- **Formatting and links.** A message may use a few marks and nothing else:
  `**bold**`, `*italic*` (or `_italic_`), `~~struck~~`, `` `code` ``, a line that
  starts with `> ` for a quote, and three backticks around a block of code. No
  headings, tables, images or `[label](address)` links (a label that says one
  thing and opens another is how links get faked). The marks travel as ordinary
  text, so a friend on an older SaveSync just sees them, and *your PC* reads them
  when it shows the message; whatever a friend writes is never taken for HTML.
  A web address (`http://` or `https://` with a real domain name — not an IP
  address, `localhost`, a name that only exists on a local network, or one with a
  user name in it) becomes a link, always **shown as the address it opens**; an
  international name is shown in the plain ASCII form the browser will use, so
  look-alike letters cannot pass for others. **Clicking a link asks first**: the
  host on its own line, a warning for look-alike names and plain `http`, Cancel as
  the default. At most five links per message are live. **Nothing is fetched** for
  a link — no preview, no icon, no image or video from the address — because every
  one of those would tell whoever runs that address that you read the message,
  when, and from where.
- **Attachments.** The **+** next to the box offers a file or folder — or drag
  files and folders onto the conversation (or the box), as many at once as you like
  — and you get the same list as when sending by hand, to untick any file. It is an **offer**: your
  friend gets a card with the name, how many files and the size, and *nothing
  moves* until they press **Accept**. Accepting makes a fresh transfer token on
  their PC and answers with it; your PC — which has to be running, within a day of
  the offer — then sends to it with the usual sealed transfer: links, never copies,
  mandatory encryption. **Size does not matter**: it is the same transfer as
  sending by hand, with no limit on how big a file is or how long it takes (see *A
  token's time* below), and a big one shows how far it has got while it is being
  got ready. **Receiving is a download, always**: whatever the file is — a save, a
  picture, a video — it comes whole into their *Folder for received files*
  (Settings) and stays there. A video can also be **watched while it is still
  arriving**, as an extra that changes nothing about the download. Both cards show
  progress and speed, then *sent* / *saved*; a saved card has **Show in folder**
  first and, as extras that work on the saved copy, **Show picture** for a plain
  picture and **Play video** for a video. **Either end can stop a transfer at any
  time** — see *Stopping a transfer* below. The rules, on both sides:
  - what arrives must be **exactly what was offered** — the same number of files
    and the same total size — or it is refused before a byte is fetched. A
    different offer that turns up on that token is shown as an ordinary offer for
    you to decide on, never taken by itself;
  - **programs and scripts are never sent or taken**: `.exe .scr .bat .cmd .ps1
    .vbs .js .jar .lnk .msi .dll .reg .iso` and the like, macro-enabled Office
    files, judged by the real last extension (`photo.jpg.exe` is a program, and so
    is `a.exe.`). Put it in a `.zip` if it must go. Names Windows treats specially
    (`CON`, `x:stream`, direction-override characters), more than 1000 files, and
    files that changed after the offer (the size no longer matches) are refused too;
  - received files are marked **as coming from the Internet**, the same mark a
    browser leaves, so Windows's own checks apply when you open one;
  - SaveSync **never opens or runs** a received file. The only things it ever
    decodes are a picture or a video you ask to see, on that click and no other
    (Settings can turn both off):
    - a **picture** — PNG, JPEG, GIF, WebP or BMP, under 8 MB and 16 megapixels,
      first frame only, header checked before a pixel is read, with a memory cap;
    - a **video** — MP4, M4V, MOV, 3GP, MKV, WebM, AVI, WMV/ASF, OGV, FLV, MPEG or
      MPEG-TS, in a small player inside the card (play/pause, seek, volume, close;
      no full screen): **Play video** plays the downloaded copy, and **Watch while
      downloading** (on a single-file video that is still arriving, once its start is
      in) plays it as it comes. The download is not touched either way: the file
      still lands whole in your folder, and whether or not you ever press Play. A
      video that is still arriving is read through a device that hands the player
      only pieces that have fully arrived **and match the torrent's own hash**, so
      the player sees exactly the bytes the finished file will have, never a
      half-written piece; the end of the file (where an MP4 keeps its index) and any
      place you seek to are fetched first, and a part that has not arrived yet shows
      *waiting for the download*. What plays depends on the codecs of this PC's media
      library: the container is not the codec, and a video it cannot play says so.
      The file must really be that container (its first bytes are checked, a
      playlist or a program wearing a video's name is refused), a plain file and not
      a link, still exactly the size that was offered; the player is handed the
      file's *bytes* — no path, no other file, no network address — and only one
      plays at a time, with no autoplay. It is dropped if it has not loaded in 20
      seconds or the library reports an error, and closed when you leave the
      conversation. **This is the riskiest thing in the chat:** a media library
      parsing a file somebody else made runs inside SaveSync, and a flaw in a decoder
      is a flaw in SaveSync. Play videos from people you trust, or turn it off. A
      link to a video elsewhere (a video site) is never embedded: that would contact
      it;
  - there must be room in the folder (the size plus 64 MB), or Accept says so; an
    offer nobody takes up lapses after a day, and several offers waiting at once
    fill the mailbox like texts do (the + then says so).
- **Announcing.** A message from a friend is announced like any other SaveSync
  notification: who it is from, a preview (the first words of the text, without
  its marks; *"offers you holiday.zip"*; or *"Marco sent you a token - 9:41 left"*), a primary **Open message** button that opens
  that friend's conversation, and **Don't show again**, which silences *that
  friend only*. A silenced friend is listed in Settings, in the same list as the
  per-game ones; remove the entry there and their messages are announced again.
  The silence is kept in the friend file (encrypted, see *Locking the list*), not in
  `config.json`: a line there would have spelled out who your friends are to anything
  that read the settings (and to a config export).
  Nothing is announced while you already have that friend's conversation open
  (what arrives there is read as it comes).
- **Nothing lost.** A mailbox can only hold a little, so texts are numbered and
  *acknowledged*: yours stay on your PC, and go back into the mailbox every time it
  is written, until your friend's SaveSync says it has them — also after a restart.
  If so many are still waiting that one more would not fit (about ten short ones, or
  two or three long ones), the box says so and Send is off, instead of an older
  message being pushed out. The network keeps what you wrote for about two hours,
  refreshed while SaveSync runs; so a message reaches a friend who is offline as soon
  as they are back, provided your SaveSync is running or has not yet been closed for
  longer than that — otherwise it waits (⏳) on your PC and goes out the next time you
  start it. If their numbers skip (they wrote before you added them, say) a note
  reads *N message(s) did not reach you*.
- **Badges.** The 👥 button in the sidebar shows how many *different* friends have
  something unread (two messages from one friend count once). In the Friends
  panel each friend has a badge with the exact number of unread messages (texts,
  attachment offers and tokens alike); opening their conversation reads them and the badges come
  down. The button's tooltip also gives the total number of unread messages. A token card says
  whether the token is usable (with its countdown), used,
  replaced by a newer one, or expired.
- **Stopping a transfer.** Either end can call one off at any time, and the other
  end is told. **Receiving:** *Cancel* on the card (or *Cancel transfer* in the
  Receive panel) asks first, then stops the download: the files that had arrived
  *whole* stay in the folder (the panel's usual *OK / Move to…* note lists them),
  whatever was only *partly* received is removed together with the folders this
  transfer made — nothing else is touched, not even a file of yours with the same
  name — and the card goes back to *offered*, so you can accept again from scratch;
  the sender's card says you stopped it. **Sending:** *Cancel* on the card (or
  closing the send window and answering *Close anyway*) stops seeding and tells the
  receiving SaveSync, which stops and cleans up the same way; the card there reads
  *cancelled*. An offer taken back before anyone accepted it turns the card at the
  other end into *cancelled* too, and so does a cancel while the files are still
  being got ready. Both ends are told two ways — a short message between you, and
  (for a send) a sealed notice in the transfer's own mailbox that the receiving
  session keeps reading while it downloads — so one lost message does not leave
  the other end waiting. If the sending PC is switched off, or SaveSync is simply
  closed there, nothing can be sent and no notice is: a download that has had
  nothing arrive for a minute says so (on the card and in the Receive panel) and that
  the sender may be offline — it is **never given up on by itself**, since a
  connection has no time limit, so you can keep waiting or cancel.
- **A token's time** — **10 minutes from the moment it was made, wherever it ends
  up, for the sender to make FIRST CONTACT; nothing else is limited by it**: one
  pasted later is refused as *expired*, the Receive panel replaces a token that
  runs out unused, and a friend's row disappears at zero. The two clocks need not
  agree: the deadline shown is the earlier of what the token's own stamp and its
  sender's "seconds left" say. An offer that arrived in time stays until you
  answer it, and once it is accepted the transfer runs for as long as it takes —
  there is no time limit on a connection and none on a file's size. Getting a big
  offer ready (every byte of it is hashed) can take longer than ten minutes, so a
  sender that is still busy after 45 seconds leaves a small sealed *hold* in the
  mailbox and renews it every 30: the receiver reads it as the sender having
  arrived, stops counting the token down (the panel says the sender is getting the
  files ready) and keeps waiting for as long as the sender keeps saying it is
  working — five minutes without a word and it gives up. A sender that never
  makes contact in time, or whose first connection does not happen within 12
  minutes of publishing the offer, is the only way a send is dropped for time.
- **Staying reachable.** While SaveSync runs it looks in your friends' mailboxes
  every few seconds when a window is open and about every 20 seconds otherwise,
  so a token arrives while you play. Turn *Check friends for tokens in the
  background* off and it only looks while the Friends panel or the Receive window is open.
  With no friends it uses no network at all for this.
- **Removing** a friend stops SaveSync listening to them (and drops any token
  of theirs); they are not told.
- **Locking the list.** The ⋯ menu next to *Lock* sets a **lock password**, and it
  is a real encryption, not only a screen: the friend file is sealed under a random
  data key that the file keeps wrapped by your password (scrypt, 128 MiB, about a
  third of a second per try; ChaCha20-Poly1305). Without a password the file is
  sealed under a key this PC makes for itself - which any program running as your
  Windows user can derive, so it only keeps the file from being read off the disk
  elsewhere. With one, **nothing on this PC can open the file without the password**,
  and the file does not need this PC's key at all.
  The list is closed when SaveSync starts, after the minutes you choose without
  using it (*Lock friends page after*, Settings, 10 by default, *Never* = only the button;
  a Friends panel left open and not touched does not count as using it)
  and whenever you press **🔒 Lock**. Closing it saves it, stops the friends'
  network, and forgets the key and everything decrypted (the friends, the
  conversations, your identity, the mailbox keys): the program holds nothing it
  could be made to show. If a transfer is still going on it is closed on screen
  at once and sealed when the transfer is over. Python cannot wipe memory, only let
  go of it, so this keeps the list from a program that reads the file, not from one
  that reads this process while the list is open.
  **While it is locked nothing is listened for**, so nothing arrives, is announced
  or counted on the badge; what friends send waits for you - their texts stay in
  their outbox until you acknowledge them, and the mailbox keeps them in the DHT -
  and is taken the moment you unlock. (After a start, listening begins the first
  time you open the list.) Because the key only exists behind the password, a
  forgotten one cannot be recovered - see the next point. After five wrong passwords in
  a row it waits 30 seconds (that slows a person typing; against someone who has
  only the file, the cost of each try is the scrypt work).
  Nothing is generated for you and nothing is set until you do it: you choose the
  password (any text, at least 8 characters now that it protects a file offline),
  type it twice to confirm it, and changing or removing it asks for the current one
  first.
- **Forgot the lock password?** The only way back in is to delete the list: press
  **Forgot the password?** on the lock screen, or **Reset…** next to *Friend list* in
  Settings (which is never locked, and keeps working while the list is). After one
  plain question it deletes, for good, your friend code and every friend, all
  conversations and unsent messages, the lock password, anything being sent or
  received through friends (and the token waiting in the Receive panel), and what
  Settings remembers about friends. You get a new friend code; nobody on the old list
  can reach you, or be reached, until you add each other again. Files you already
  received, link files you made and all other settings stay. The files are
  deleted, not shredded - and a list that had a lock password was sealed under it,
  which is what keeps its leftovers unreadable.
- **Another PC.** The friend file is tied to this machine and login, so it cannot
  simply be copied. **Link another PC…** (⋯ menu) writes a link file protected by a
  password you choose (at least 8 characters, stretched with scrypt); on the other
  PC use **Import from another PC…** and type it. Both then are *you* to your
  friends — but two PCs on one identity overwrite each other's mailbox, so after
  writing the file SaveSync offers to remove the friends from this PC. What an
  import or a removal replaces is kept next to the file. Anyone holding the file
  *and* the password can read your friends and write as you: keep both safe.

</details>

### Group chats

A group is a chat for several of you, kept **between the members themselves**: there is no server, no
group key to leak and no place the conversation lives, so it goes on when someone - even the person who made it -
is away. Open 👥, press **New group**, give it a name and say how many people it can hold, then invite your
friends from the group's **Members** page. Messages are text (up to 270 bytes) and attachments, sealed end to
end the same way a direct message is.

**Covers:** Making one, and raising its limit · Inviting, and letting others invite · Roles: host, administrator, member · Taking people out, and who may · Closing the group · When the host goes · Who sees what · Attachments · What is not promised · Limits

<details>
<summary><strong>Group chats in detail</strong></summary>

- **Making one.** *New group* asks for a name and the **most people it can hold** (2 to 12, you included -
  8 by default). Every member talks to every other member directly, so a bigger group costs everybody more;
  the host can **raise** the limit later (*Members* → **Raise limit…**, up to 12) and never lower it, and every
  member is told. Your friends are asked from the group's *Members* page (**Invite…**); they see an invitation in their list (and a
  notification) and **Accept** or **Decline**. An invitation lapses after a day. Accepting shows your
  name and friend key to every member of the group, and theirs to you - that is how a group works.
- **Letting others invite.** The host (and administrators) can allow an ordinary member to invite their own
  friends: tick **Can invite** on that member and choose how many people **in all** they may ask. The member
  asks one of *their* friends; when that friend accepts, the member tells them who is in the group and tells
  the others who joined. Somebody asked back after they left is the same person, not another one of the number.
  What is waiting for an answer counts against the number until it is answered.
- **Roles.** The **host** made the group and can do everything: take anybody out, rename the group, allow members
  to invite and how many, make administrators (and take that away), and close the group. An **administrator**
  - made so by the host with the **Administrator** box - can do what the host can **against ordinary members**:
  take them out, allow them to invite, rename the group, and invite without any number. They cannot do it to
  the host, who cannot be touched, nor to another administrator: only the host removes an administrator. The host
  or an administrator can also tick **Can take out members** on an ordinary member, which lets that member take
  out the ordinary members **below** them - never an administrator, the host, or anybody else who holds that right
  (so power only ever goes down: host, administrators, those allowed to, everybody else) - and take it back at any
  time. An ordinary **member** can write, offer files, and leave. A member who is taken out, or who is made an
  administrator, loses the single rights they were given.
- **Taking people out, and closing the group.** **Take out** removes a member: they are told, they lose what they
  were allowed, and only the host or an administrator can ask them back (an invitation from anyone else is
  refused). Who may take whom out is decided by each member for themselves from who sent it, so a claim from
  somebody who may not is ignored. Because there is no one place a group lives, **there is no "delete for
  everybody"**: the only way to end one is to take everybody out, which the host's **Close group** button does in
  one go - each member is told and the group is removed from the host's PC. A member who was away when it was
  closed is told as soon as they are back.
- **When the host goes.** Nothing in a group needs the host to be there: the others keep talking, administrators keep
  their powers, and members keep the right to invite they were given. What goes with the host is the host's own
  power - nobody can appoint administrators any more - and a group with no administrator is simply
  not run by anyone: nobody can take people out or allow them to invite.
- **Who sees what.** Every message is sealed separately for each other member, with the key the two of you share -
  exactly like a direct message - so only the members can read it, and what the DHT stores for a mailbox looks like
  any other. A member you are not friends with is a **hidden friend**: never in your friend list, only able to send
  you this group's traffic (and the answer to a file you offered them), shown with the start of their key next to
  their own name, because a name is only what its owner says. **Every member can see every member's name and key**,
  whoever invited them. In the DHT, the nodes that keep a mailbox see an address write and an address read it, as
  with a friend - for every pair of members; in private mode (Tor) every member is one more onion link.
- **Attachments.** The **+** (or dropping files on the chat) offers a file or folder to the group: nothing moves
  until a member presses **Accept**, and then the files go from the PC that offered them to that member alone, over
  the usual sealed transfer, one member at a time - so **everybody who accepts connects to the PC that offered, and
  both see each other's network address**, as friends do (you are asked once, when you offer). You see one card
  with a line for every member (offered, sending 45%, sent, taken back, not taken in time) and can **Take back**
  what is still open. Pictures and videos are not shown inside a group's chat: open the received file from its
  folder (*Show in folder*). Programs and scripts are never sent or taken, as in a direct chat.
- **What is not promised.** A member who joins takes their inviter's word for who is in the group (everyone else
  then speaks for themselves): an inviter who lies can show a newcomer the wrong members. An inviter who lies
  selectively can let in one person more than their number to some of the members, each of whom counts for
  themselves. There is no history: a newcomer sees only what is said after they joined. A name is not proof of
  anyone. Nothing here has been tried over a live DHT or Tor - it was tested over simulated mailboxes.
- **If somebody is away.** What you say waits for them and goes when they are back. When more than twenty
  messages are waiting for somebody who has been away that long, the oldest chat is dropped from the queue, but never a message that changes the group (a
  join, a removal, a permission): the log shows a ✗ for that member and they get a note that some did not reach
  them. Each of your messages shows how many of the others have it (✓✓ 3/3); hover for who. A group's messages
  go out one or two at a time per member, so a direct message to a friend can briefly say the mailbox is full
  while a group message waits to be acknowledged.
- **Limits.** At most 12 members (chosen per group, raised by the host later), 20 groups, 20 invitations waiting for an answer, texts up to
  270 bytes. "Don't show again" on a group's notification (or the ⋯ menu) silences that group only; it is listed in
  Settings and counted by the badge all the same. Locking the friend list closes the groups with it.

</details>

### If it doesn't connect

The two machines find each other through the public BitTorrent network — at a
meeting point only they know, see below — and then connect directly. That
usually takes a few seconds, longer on a slow network. If the sender sits at 0
peers:

- Keep **UPnP** on (the default) so the router opens the port itself.
- If your router has UPnP switched off, set a fixed **Connection port** in
  Settings and forward it (TCP and UDP) to your PC. Only one of the two sides
  needs to be reachable. A fixed port is what makes a hand-made forward
  possible; with *Auto* the port changes every time.
- A fixed port that's already in use (say, a second transfer in the same app)
  moves up to the next free one rather than failing.
- Turn UPnP off only if you forward by hand and don't want the router asked.
- With *Use TLS for transfers* on, one of the two PCs needs a port that can be
  reached, and nothing falls back to the plain kind: see [TLS](#tls-optional).
- Both sides need the same generation of SaveSync: an older build can't read the
  offer, and the sender just keeps waiting.

### Speed

A transfer is meant to use all of your line. What decides that, measured on this PC
(one gigabyte, both ends on loopback, so the line itself is not in it):

- **TCP first.** The connection is tried over TCP, which runs at the speed of the
  line (about 220 MB/s here), and over uTP - the fallback that gets through a router
  with no open port - only if nobody has connected after 6 seconds. uTP is
  user-space and tops out at about 47 MB/s here whatever its settings, so it is the
  difference between a gigabit line being full and a fifth used. Opening a port
  (UPnP on, the default, or a forward) lets the first one work. Behind the SOCKS5
  proxy uTP is the only way there is.
- **The disk cache.** Received data now goes through the operating system's cache.
  The old default wrote every piece straight through to the disk, which held a fast
  transfer to 90-150 MB/s here.
- **Finding each other sooner.** The first announce at the meeting point is made
  before the DHT has a single node and is lost; the next few now follow after 2, 3,
  5 and 8 seconds, instead of one after 20 - the two PCs meet in seconds instead of
  tens of seconds.
- **What it did not need.** A longer request queue, bigger socket buffers and a
  bigger piece size were tried and changed nothing measurable (the queue also on an
  emulated path of 100-600 ms at 300 Mbit/s); a very small piece size (16 KB) is far
  slower, so the automatic size is kept. Switching encryption off made no
  difference either: RC4 and the piece checks are not what limits a transfer.
- **What cannot be measured here:** your real line. A real internet path has
  slow-start and other traffic of its own, and a file goes the speed of the slower of
  the two PCs' lines.

### TLS (optional)

*Use TLS for transfers* (Settings) swaps BitTorrent's own encryption (RC4 under a
secret info-hash, see *Privacy* below) for TLS - an authenticated modern cipher -
through libtorrent's "SSL torrents". Nothing is copied; the files stay links.

<details>
<summary><strong>TLS in detail</strong></summary>


- **A transfer is TLS or it does not happen.** Every connection is TLS and each
  side must show a certificate signed by that transfer's own authority; a peer that
  is not speaking TLS is refused. There is no fallback to the other kind. The
  setting is the *sender's* choice (the offer says so, and the Receive panel shows
  a 🔒); a PC with it on also opens an SSL socket while it waits to receive. A PC
  with it off can still take a TLS offer, but only from a sender whose port can be
  reached.
- **No certificate travels.** The authority is derived, on both PCs, from the token
  they share (the offer is sealed under it): the same key and certificate byte for
  byte, because Ed25519 signs deterministically and every field is fixed. Each side
  makes a certificate of its own, a fresh key every time, and has it signed by the
  authority - which only somebody holding the token can do. TLS 1.3 key exchange
  is ephemeral, so a token that comes to light later opens nothing that was
  recorded. A stranger who somehow got the torrent's description, but not the token,
  is turned away at the handshake (tested), as is a plain connection to the plain port.
- **Two steps instead of a magnet link**, because a magnet link cannot start an SSL
  torrent: the receiver has to be given the torrent's description. The offer carries
  the description of a tiny first torrent (about 140 bytes, a version-2 torrent so
  that it stays that size), that torrent carries the real one's description, and then
  the real transfer begins - the same one as ever, so watching while downloading,
  cancelling from either end and the checks on arrival work as they do without TLS.
  It adds a second or two at the start.
- **It needs a port that can be reached** on at least one of the two PCs (UPnP is on
  by default). The SSL sockets are not the one the DHT uses, so the address the DHT
  learns for them is the port named outright, not the one a router mapped for the
  DHT: where a pair connects only by punching through routers, which plain
  transfers do, TLS may not. When it cannot connect, nothing falls back; the cure is
  a forwarded port (set the *Connection port*; the SSL port is the next one up) or
  turning TLS off. This part was tested on loopback only - UPnP and real routers are
  not something this PC could try.
- **Not with the proxy.** TLS over uTP needs a socket that listens, which a proxy
  forbids, so with the proxy on TLS is not used (the Settings box says so) and
  transfers are the plain, proxied kind.
- **Cost:** about 40% slower than RC4 in the same loopback test (136 against 220 MB/s),
  still above what a gigabit line carries.
- **Who sees what:** the SNI of a TLS connection is the torrent's info-hash in the
  clear - random to anyone else, and useless without a certificate.

</details>

### Hiding your address

Peer to peer cannot hide an address by itself: the friend on the other end
connects to your PC (for a transfer; chat only goes through the DHT), and the DHT nodes
that keep the mailboxes see who asks them.
The only way out is to let something else be seen instead - a **SOCKS5 proxy**
(many VPN services offer one). In Settings, under *P2P → Connection*, choose
*SOCKS5 proxy*, give its address and port (and a login if it wants one) and press
**Test**.

<details>
<summary><strong>The proxy in detail</strong></summary>


- **Through it, or not at all.** Every P2P packet and connection - the DHT, the
  mailboxes, the transfers, both ways - goes through the proxy. If it is down,
  refuses, or has no UDP, nothing is sent: never "directly instead". Switched on
  but not filled in, P2P is paused the same way, and the Friends panel says so.
- **Nobody connects in.** SaveSync stops listening and stops asking the router for
  a port (UPnP / NAT-PMP). The two PCs meet the way two PCs behind routers do: each
  reaches for the address the other showed in the DHT, which is the proxy's.
- **It needs a proxy that carries UDP** (SOCKS5 "UDP ASSOCIATE") and gives you one
  address for all your traffic. **Test** says if UDP is missing; Tor's SOCKS port
  has none, so it cannot be used. Hiding one side is the safest; hiding both
  works when the proxy lets the other's packets in, as a router would - if a
  transfer never connects, that is the first thing to suspect.
- **Changing it.** The friends' network restarts by itself; a waiting token (or an
  offer you have not accepted yet) is dropped for a new one; a transfer already
  running finishes the way it began.
- **The login** is kept in `p2p_proxy.dat`, encrypted for this PC - not in
  `config.json`, and never in a config export.
- **What it does not hide:** the proxy's operator (and the line up to it) still
  knows your PC and sees that you use P2P, when and how much. The DHT's starting
  servers are looked up **through the proxy** too - over its UDP, by a public DNS server that sees
  the proxy's address - and libtorrent is given addresses only: left to itself it asks *this PC's*
  DNS for the name whatever the proxy settings say (checked: a name that exists nowhere fails on
  this PC's resolver and the proxy is never asked), which tells that DNS server you run P2P. The only
  name still looked up on this PC is the proxy's own address, if you typed it as a name. **Test** also
  checks that the proxy carries such a lookup; one that carries UDP but not that cannot be used for
  P2P - nothing is looked up locally instead - unless you tick **If the proxy cannot look up names, let this
  PC's DNS do it** (off by default): then only that one name goes to this PC's DNS, which learns you use P2P.
  A passing glitch does not send it there: the lookup is tried twice first (once, for a proxy that has just
  failed a whole round), and a public DNS server that answers with an error is passed over for the next.
  Switching the option restarts the friends' network (and drops a waiting token, as any change of the proxy
  does). Without it, a friends' network that started before the proxy could carry the lookup looks again
  every minute and starts its DHT by itself when it can.
- **Why not I2P.** I2P was designed and tried (libtorrent left exactly as it is,
  talking only to 127.0.0.1, small bridges carrying its connections over I2P
  streams: a transfer, cancel and all, worked against a pretend router) but dropped: its
  router needs 10-20 minutes before it is usable the first time, and offline messages
  would need roughly 20-50 PCs of SaveSync users on I2P. Tor is what private mode
  uses instead.
- **Why not Hyphanet (Freenet).** Asked for, and not offered, because it cannot be
  built in small. It is a Java node (a recent build needs Java 17; the download page still
  says 9) with a datastore of its own, which a program talks to over its FCP protocol by
  inserting and fetching content-addressed blocks - not a connection two PCs can run
  BitTorrent over, so it would replace the transfer rather than carry it, and would have to
  be installed beside SaveSync (the Tor copy in `vendor/tor` is about 20 MB for both
  platforms). Hiding an address always costs at least one hop; the fast one is the proxy
  above, and Tor is the private one.

</details>

### Private mode (Tor)

A proxy puts somebody else's address where yours was, and needs one. **Private mode**
(Settings, *P2P → Connection → Tor (private)*) needs nothing: SaveSync carries
a copy of Tor (the Tor Project's own program, kept in `vendor/tor`, fetched and checked by
`tools/fetch_tor.py`), starts it when it is first needed and ends it with the application.
The two PCs meet inside Tor, at *onion addresses*, and neither ever sees the other's
address - nor do the relays, nor the DHT, which is not used at all while this is on.

<details>
<summary><strong>Private mode in detail</strong></summary>


- **Transfers.** The receiver's token says it is a private one (it is one byte longer) and
  leads to an onion address that both sides work out from it; the receiver waits there, the
  sender reaches it, and the offer, the "I am still preparing" notes and a cancel cross on a
  control stream, sealed under the token as ever. libtorrent is left exactly as it is -
  hardlinked staging, pieces, cancel, watching a video while it arrives - but listens on
  127.0.0.1 only, makes its connections from there only, and has no DHT, no router
  port-mapping, no local discovery and no uTP (`core/p2p/transfer.py`,
  `_tor_session_settings`). Small loopback bridges carry its connections over Tor
  (`core/p2p/tor_link.py`).
- **Friends.** Each friendship has two onion addresses, one for each end, that both
  friends work out from the secret they share; this PC serves its own and reaches for the
  friend's. The very same sealed slot a mailbox would hold - hello, tokens, numbered
  texts, the acknowledgement - goes down that stream whenever it changes
  (`core/p2p/tor_friends.py`). The DHT mailboxes are not touched, so there is no longer
  anything for a DHT node to match: a mailbox that one address writes and another reads is
  what let two addresses be tied together there, and Tor carries no UDP to hide it.
- **Both ends need it.** A private token cannot be used by a PC that is not in private mode
  and a plain token is refused by one that is - it would show that PC's address - each with a
  message saying so; friends talk to friends in the same mode.
- **What it costs.** Nothing waits in the network for a friend who is off: a text stays in
  your outbox (numbered, kept until acknowledged, as always) and goes the moment you are both
  on; a token needs the friend on while it is good, and is reported as not delivered after three
  minutes otherwise. Transfers run at a few hundred kilobytes a second, not megabytes. The
  first start on a PC downloads the list of relays (measured: 42, 56 and 126 seconds); every
  later one takes about three.
- **Speed.** A Tor circuit carries about 0.06-0.18 MB/s on the PC this was measured on, so each
  transfer uses six, side by side, each its own peer: the bridges give a connection a peer id of
  its own and take the listen port and the "upload only" notices out of the extended
  handshake - without that libtorrent keeps one connection of the six and closes the rest, and
  a receiver that has not yet put the files in their places makes it drop the one it has and never
  call the sender back. BitTorrent's own encryption is off (Tor is end to end already). The six
  circuits are made while the receiver is still reading the offer, so the transfer starts at
  once when it accepts. Measured through two real Tor programs, 25 MB with sub-folders arrived
  bit-identical in 78 seconds - 0.32 MB/s on average, 0.35-0.43 once under way - where one
  circuit gave 0.17; more than six circuits added nothing (8 and 12 gave the same, and three
  entry guards instead of one did not help either). A circuit that fails is replaced by one on a
  new circuit and the transfer goes on. The ceiling is the Tor network's, not this program's.
- **Getting there.** Reaching an onion service takes seconds and now and then minutes (a slow
  relay; a service so new that the directories do not have it yet - seen: 61 s, and once 300 s of
  time-outs). So a try that is slow is not waited for alone: after 15 seconds a second goes out on
  other circuits, then a third, and the first to answer is used; a try that FAILS is not raced
  (the service is not up). And a token made in private mode is handed to a friend only once its
  own address can be found.
- **Measured.** An onion service is found about 3 seconds after it starts; the offer arrived
  11-17 seconds after the sender began (and once 61 s, before the racing existed), the first bytes
  2.5 seconds after accepting; a link between two friends was up in 9 seconds, a text crossed in
  under a second.
- **Safety.** If Tor cannot start - no program, no network - nothing is sent: never
  "directly instead". libtorrent is told to connect from 127.0.0.1 only, so a connection to
  the internet could not be made even by mistake; the receiver never connects out and the
  sender takes none in, because a peer says which port it listens on and here that would be
  a port of this very PC, chosen by the other side. What arrives on the control stream is
  believed only if it opens under the token (or the friendship's key); a stream that does
  not begin with the byte that says what it is, or whose BitTorrent handshake is not
  BitTorrent's, is dropped; at most 24 connections are served at once.
- **What it does not hide.** That this PC uses Tor (your provider sees a Tor connection, not
  where it goes), and the size and timing of what crosses. Tor is a network run by volunteers:
  a transfer of hundreds of megabytes is a real load on it, so keep private mode for what needs
  it.
- **Two things learned the hard way, for whoever works on it.** Whatever sits behind an onion
  service must never speak first - tor 0.4.9 on Windows then leaves the first bytes on its
  buffer for good (BitTorrent's connecting side always speaks first, so libtorrent is fine; a
  test server that greets is not). And a circuit takes 3-60 seconds to set up, so libtorrent's
  handshake timeout is raised from 10 s to two minutes.

</details>

### How it works

**Covers:** The mailbox · The meeting point · Friends · The token's time · Tokens made for a friend · Asking for a token · Stopping, and watching meanwhile · Chat formatting and attachments · The connection

<details>
<summary><strong>How the transfer works, in detail</strong></summary>


**The mailbox.** The receiver's token is 15 random bytes (shown in base32);
hashing it gives the seed for a keypair generated fresh for that transfer. The
sender derives the same keypair from the token and writes the offer — magnet
link, their identity, what is being sent — into a BitTorrent DHT "mutable item"
(BEP44) addressed by that keypair; the receiver, already watching that address,
reads it back. The offer is **sealed** first (ChaCha20-Poly1305, with a key
derived from the token), so the DHT nodes that store it hold noise: only the two
of you can open it, and an altered or foreign item is rejected. If the DHT
hasn't taken the item yet, the sender publishes it again before giving up -
and again if no answer comes at all. Every mailbox item, of any kind, is padded to
the same size before it is sealed (the padding is trailing spaces in the JSON, which
any reader ignores), so a node cannot tell a short text from a long one, or a hold
from an offer, by its size.

**The meeting point.** The magnet link carries the torrent's info-hash, and
that is what a stranger would need to find the download. So it is never
announced: the torrent has no tracker, and the DHT, local-network discovery and
peer exchange are all switched off for it. Instead both sides announce their
address under a "rendezvous" hash derived from the token — it has no connection
to the info-hash — look that up, and connect to whatever the other left there.

**Friends.** Each person has an X25519 identity key that stays on their machine;
the friend code is its public half plus a checksum, so a typo is caught. From
the two keys two friends derive — separately for each *direction* — a key pair
for a DHT mutable item (the friend's mailbox) and a sealing key. To the DHT that
is two random-looking keys per friendship: no names, no contact list, nothing
that ties a mailbox to a person. **They change every six hours:** the keys also
come from the number of the six-hour period (an "epoch") the clock is in, so a node
that stores or sees a mailbox cannot recognise it later. Each friendship's periods
begin at an offset taken from what the two share (not all at once, which would be a
burst from every SaveSync there is). A mailbox is written under the period my clock
is in; a friend whose clock is up to ten minutes off still finds it, because the
period before is read for the first 2 hours 10 minutes of a period (an item can
live that long in the DHT, so a friend who was away still gets it) and the next
one for the last ten minutes. What is waiting is written again under the new keys
when a period begins, and a message seen twice is taken once. What the mailbox holds, sealed
(ChaCha20-Poly1305), is a "hello" with the sender's name, the tokens sent in the
last few minutes, the texts the friend has not acknowledged yet and the sender's
own acknowledgement of theirs, at most about 900 bytes; each message has an id and
is taken once, and the sender writes it again every 3 minutes (45 for somebody only known through a
group) so it does not expire from the DHT - and so that the friend can tell it is there. A text carries a number inside a random "numbering" of its
own (an epoch, made with the first one): the friend answers with "I have up to n
of that epoch", the sender drops what is covered and marks it delivered, and a
skipped number shows up as a gap rather than going unnoticed. A text is never
dropped to make room: when the mailbox is full the next one is refused. Only the two friends can derive the sealing key, which is also what
tells the receiver a message really is from that friend. The identity and the
list are kept in one file, `p2p_friends.dat` in SaveSync's data folder - sealed
under the lock password when there is one (see *Locking the list*), otherwise under
the same machine-bound key as the cloud credentials - and are never part of a
config export. If the file can't be read it is set aside, not
overwritten, and a new identity is made. A link file is the identity and the list
sealed (ChaCha20-Poly1305) under a key from your password (scrypt).

**The token's time.** The ten minutes are for the sender reaching the mailbox, not
for what comes after. Before the torrent exists every piece of the offer has to be
hashed, which takes as long as the offer is big; if that outlasts 45 seconds the
sender writes `{"hold": n}` (sealed like the offer) into the same mailbox, again
every 30 seconds, and writes the real offer only after the last hold has been
stored (a mailbox keeps the highest sequence number). The receiver reads a hold as
first contact: the token stops counting down, and the wait ends only when the
offer comes, the person cancels, or five minutes pass without a renewal. After the
offer is accepted nothing has a clock — not the download, not the seeding, not
the connection.

**Tokens made for a friend.** A token has one flag byte (the one private mode already used),
and a token made for a friend - the answer to a request, or to an attachment offer - sets a second
bit in it. Its seed is not just the hash of the token but that mixed with a key derived from the
secret the two friends' identity keys give (`FriendStore.bind_key`, the same on both sides, and
under a label of its own). Everything a transfer is made from comes from that one seed - the DHT
mailbox and the key it is signed with, the key the offer, the "hold" and the "cancel" are sealed
under, the rendezvous hash, the TLS authority, the onion address - so a holder of the token alone
derives none of them: it is not their token. The send entry points refuse a bound token that
comes without its key rather than send to the wrong mailbox, and the key is read on the GUI thread
before the sending work starts. Verified at the level of the derivation and the services, not
over a live DHT or Tor.

**Group chats.** A group is no new kind of channel: a group message is a numbered, acknowledged text with a
`g` field - the group, what it is (a message, an offered attachment, an invitation, the roster, a member added,
taken out, allowed, made an administrator, renamed, left) and a few validated fields - sent to each member over the
pair's own sealed mailbox, so what it adds to a direct conversation is only the rules. **Who may do what is decided by
who a message came from**, which the pairwise sealing proves: the host's or an administrator's word counts for
taking out, renaming and allowing (an administrator's not against the host or another administrator; one allowed
to take members out only against ordinary members who are not themselves allowed to), the host's alone for
making administrators and for raising the limit, an inviter's for who joined (within the number they were given, counted by
each member for themselves), anyone's for what they say; a message that claims more is ignored. There is no group
key, so a removal means "stop sending to them and refuse them" rather than a re-key. The members a group needs
and you are not friends with are **hidden friends** in the friend file: they get a stream like any friend, but
every other kind of message from them (a text of their own, a request) is dropped, and a token only as the
answer to an attachment offered them. Streams are separate, so a message can arrive before the roster or the "add"
that explains it: it is held (30 per group, a day) and acted on when they come, never lost, since the stream has
already counted it as received. Group traffic waits in a queue of its own and goes into a pair's stream one or two at
a time, so that somebody away does not fill it; chat that waits too long is dropped (and noted), what changes the
group never. Each message carries its sender's own number for the group, so a gap is told. A group attachment
reuses the direct one's offer, accept and send, one per member, keyed by the id of the offer in that member's stream.

**Asking for a token.** A request is a small "ask" message — a label for what is to be sent
(cut to 64 bytes) and the seconds it is good for, at most the token's ten minutes — kept
in the mailbox like a token and dropped before any text when it is crowded. The friend's
answer is an ordinary token message whose "re" is the request's id (the same field that
answers an attachment offer), so what the asker's send window takes up is a token that
passed the same checks as any other; a token for a request that was declined or taken
back is just an ordinary token. A request that does not fit the friend's mailbox next to
the texts waiting there is refused, never squeezed in. An older SaveSync skips the
unknown message, so there the request goes unanswered.

**Stopping, and watching meanwhile.** A *cancel* between friends is a small
message naming the offer (or the request for a token) and saying who stops (*withdraw*: the
sender took it back; *stop*: the receiver stopped an attempt, or declined the request) —
dropped before any text when the mailbox is crowded, like a token. For a send in progress the sender also writes a
sealed `{"cancel": 1}` into the token's mailbox and keeps its session alive a few
seconds so that it is stored; the receiving session looks at that mailbox every
five seconds while it downloads. Stopping a download removes the torrent, waits
until libtorrent has let go of the files, notes which were whole, and deletes the
rest on a worker thread, retrying where Windows still holds a file. Watching
while downloading (`core/p2p/transfer.py`, `TorrentStream`) asks the torrent for
the pieces under the read position first and the next few behind them, switches
the download to in-order from the first real read, and before handing out a byte
reads the piece back from the disk and compares its SHA-1 with the torrent's — a
piece the torrent has but the disk does not yet is waited for, never guessed.

**Chat formatting and attachments.** The marks in a message are read when it is
shown (`core/p2p/markup.py`): the text is cut into plain text, code, links and
emphasis, every character is HTML-escaped, and the label is given only the few
tags that module writes itself, with a link as an index into the list it checked
— so a message has no way to bring a tag, an address or a resource of its own. An
attachment offer is a numbered text with a small description (name, number of
files, size), so it is as reliable as a text and an older SaveSync shows it as one;
the accepting token is an ordinary token message that names the offer it answers.
The sender sends with the same sealed transfer as everything else, naming the
offer inside the sealed part; the receiving PC takes the arrival by itself only
when it is that offer (same number of files, same size), and before anything is
fetched it checks the file list the torrent holds — names, kinds, counts, sizes
(`core/p2p/attachments.py`).

**The connection.** BitTorrent's protocol encryption is mandatory both ways (RC4;
a plain peer is refused), and its key includes the info-hash, which only the two
of you know. A client that doesn't know it can't join the transfer or read it. The
info-hash is in the sealed offer and nowhere else, and the torrent's name carries
128 random bits, so it cannot be worked out even for a file everybody has.

| What travels where | Why |
|---|---|
| Sealed DHT mailbox: magnet link, sender name and machine id, game name, filename, size — and for files, how many and the folder they came from | BEP44 items cap out around 1000 bytes: room only for what's needed to decide *before* downloading. Long names are trimmed to fit |
| `manifest.json` inside a **backup's** torrent | The full backup entry (save paths, exe path, chains) and the archive's real file name — no size cap, read only after accepting |
| **Files and folders:** nothing extra | The torrent holds just the files, laid out as they are: no manifest, no wrapper folder |

For files, the receiver fetches the file list first, gives each file its final
path, and only then starts the download — so nothing is written anywhere else
and an existing file can't be hit by accident. A download counts as finished
only once the torrent has been released and the files are closed; until then the
last bytes may still be on their way to the disk.

</details>

### Privacy: what the network sees

**Covers:** Hidden · Not hidden · The token is the secret · Friends · Formatting, links, attachments · The cipher is BitTorrent's · TLS is the stronger, optional choice

<details>
<summary><strong>What stays hidden, what does not, and the trade-offs</strong></summary>


- **Hidden:** the offer (who sends, what, how big, the magnet link), the
  torrent's info-hash, the file names and the contents. The public network sees
  a sealed blob in the mailbox and two machines announcing and looking up a hash
  that means nothing to anyone else. No tracker is contacted.
- **Not hidden:** that two IP addresses met at that hash and then exchanged
  traffic — as in any peer-to-peer connection — and that they use the BitTorrent
  network. Every packet to the DHT carries the sender's address, sealed contents or
  not, so the nodes that keep the mailboxes see it, as one more DHT node. Chat alone
  never connects the two PCs (messages only go through those mailboxes): your friend
  sees your address only while a transfer connects. A SOCKS5 proxy puts its address there instead of yours; see
  [Hiding your address](#hiding-your-address). **Private mode** hides it from the
  other side and from the DHT alike, which it does not use; see
  [Private mode](#private-mode-tor).
- **The token is the secret.** Whoever has it can read an offer meant for you or
  send you one (you still have to accept). Hand it over through a channel you
  trust — or to a friend, which sends it sealed; it works once, for ten minutes,
  and a spent one disappears from the panel.
- **Friends:** the network sees mailboxes that belong to nobody in particular, not
  who your friends are — and not what is in them or how long it is (every item is padded to
  one size), only that a sealed item changed, and when. A mailbox's key changes every six hours, so
  one node cannot follow the same pair for long (the two addresses that write and read it
  can still be matched within a period, by timing; only a proxy or private mode changes that - in
  private mode there is no mailbox at all). Your conversations are kept on this PC, in the friend file, and
  nowhere else but your friend's own PC. The lock password and the link password are two different
  things: the first encrypts the friend file on this PC and closes the list, the second
  is what protects a link file if it is copied or lost. Your identity key never leaves your machine; whoever got
  hold of it could read what your friends still have stored for you (tokens that
  have long run out) and pose as you to them — there is no forward secrecy for
  these short-lived messages.
- **Formatting, links, attachments:** none of it makes SaveSync contact anything
  but your friend. A link is opened by your browser only after you confirm it, and
  no preview is fetched; a file is offered, not pushed, and checked against its
  offer; see *Attachments* above for what is refused.
- **The cipher is BitTorrent's** (RC4 with a secret key, not a modern
  authenticated cipher), and the files are not encrypted a second time inside:
  that would mean writing an encrypted copy of everything sent, which a
  link-only send avoids on purpose (50 GB must not cost 100 GB). Integrity is
  BitTorrent's piece hashes, taken from the sealed offer. What makes this hold is
  the secrecy of the info-hash, not RC4 itself: RC4 is old, but its known weaknesses
  need many encryptions under one key or a guessable one, and here every transfer
  has its own key (made from a 160-bit secret) with the first 1024 bytes of the
  stream thrown away. BitTorrent's key exchange is small by today's standards, but
  the info-hash is mixed into the key, so breaking the exchange alone gives
  nothing. It is not what a modern cipher would give; that would need the encrypted
  copy.
- **TLS is the stronger, optional choice** (see [TLS](#tls-optional)): an
  authenticated modern cipher with forward secrecy, still without a copy of the
  files. It is not the default because it needs a reachable port and cannot be
  hole-punched or proxied the way the plain transfer can; RC4 under the secret
  info-hash works everywhere.
- The name an offer shows is whatever the sender typed as their display name, so
  accept only what you're expecting.

P2P needs the `libtorrent` package, which is in `requirements.txt` and bundled in
the release builds.

Relevant files: `core/p2p/transfer.py` (the DHT-mailbox + BitTorrent
mechanics, sending and receiving, the token's 10-minute clock),
`core/p2p/proxy.py` (the SOCKS5 option: settings, the login, the Test),
`core/p2p/tls.py` (the optional TLS transfer: certificates derived from the token),
`core/p2p/tor.py` (private mode: the Tor program, its control port, onion addresses made
from a secret), `core/p2p/tor_link.py` (the loopback plumbing between libtorrent and Tor:
control stream, circuits), `core/p2p/tor_friends.py` (the friends' direct link over Tor),
`tools/fetch_tor.py` (fetches and checks the Tor program into `vendor/tor`),
`core/p2p/secret.py` (the keys derived from the token or a friendship),
`core/p2p/friends.py` (friend codes, per-direction mailboxes, the encrypted
friend file), `core/p2p/friend_net.py` (the DHT session that reads and writes the
friends' mailboxes), `core/p2p/markup.py` (the marks and links a message may
use), `core/p2p/attachments.py` (what an attachment may be, and the checks on its
arrival), `ui/dialogs/p2p_link_dialog.py` (the "open this link?" question),
`ui/widgets/safe_image.py` (the one place a picture is decoded),
`ui/widgets/safe_video.py` (the one place a video is played),
`ui/widgets/stream_device.py` (what lets it read a file that is still arriving), `ui/p2p_friends_service.py` (the friend list, groups, received
tokens and their countdowns), `ui/dialogs/p2p_friends_dialog.py` (the Friends
panel that slides out of the sidebar), `ui/dialogs/p2p_friends_reset.py` (the question before the friend list is deleted), `ui/dialogs/p2p_send_flow.py` (pick, confirm, send), `core/p2p/paths.py`
(where received files go), `ui/p2p_receive_service.py` (keeps the receive session alive with the panel
closed, files the archive, does the move), `ui/dialogs/p2p_send_dialog.py`,
`ui/dialogs/p2p_send_confirm_dialog.py` (the list of files to send),
`ui/dialogs/p2p_receive_dialog.py`.

</details>

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
| Wolf RPG (`.sav`), including the LZ4-compressed variant some builds use | AliceSoft gallery lists — numbers with nothing naming what they unlock |
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

### Where the format knowledge comes from

Two things keep that table from turning into a maintenance treadmill, and both
are worth stating because they are what a hand-written parser per engine would
have cost.

**The game is the schema.** Where a save's own layout or its key lives in the
game rather than in a table here, SaveSync reads it out of the game at the
moment the save is opened, instead of shipping a constant that a patch can
invalidate:

- **Unity Easy Save 3.** Encryption is AES-128-CBC with a password the
  developer chose and the build carries. Easy Save leaves it in an
  `ES3Defaults` object, so the password is read from the game's own asset
  files — `resources.assets`, `globalgamemanagers.assets`, the shared and
  level assets, and, when those do not answer, inside `.unity3d` / `.bundle`
  archives, which are unpacked for the purpose (`core/save_editor/crypt/`,
  `es3.py` and `unityfs.py`).
- **Encrypted Unreal saves.** Nothing in the engine names the key, so it is
  not looked up but looked *for*: the game's own binaries are scanned at
  16-, 8- and 4-byte strides for AES key material (`unreal_crypt.py`).
- **Wolf RPG.** The field layout of the variable database is stored *in the
  save* — a run of field codes — so the values are read from the file's own
  structure. The game's `CDataBase.project` is consulted only to put names on
  them, and is optional (`crypt/wolf.py`).
- **Wolf RPG, the LZ4 variant.** Some builds lock the save with a custom
  Mersenne Twister keystream instead of (or alongside) the documented
  scheme — reverse-engineered by instrumenting the game itself with Frida
  while it loaded its own saves, then verified byte-for-byte against what
  the game's own code produced in memory. Two separate closed-form formulas
  — both recovered by emulating the game's own obfuscated code as an
  oracle, both confirmed against real saves — cover it end to end: one
  derives the keystream's seed from 3 salt bytes in the file's clear
  header, the other derives where the write path's post-encrypt byte-swap
  lands from 2 other header bytes (the old search-based way to find that
  swap had a real, confirmed correctness bug — more than one offset could
  look valid, and the wrong one silently corrupted field values). Both run
  instantly, no search, no game needed. The 2^32 brute-force search that
  originally validated the seed formula is kept only as a defensive
  fallback, for a file neither formula's output verifies against — none
  seen yet — and even then, a slot's own search result is cached, so a
  repeat is never needed either (`engines/wolf_lz4.py`, `crypt/wolf_lz4.py`).

Nothing is guessed in any of these. A candidate key is accepted only when what
comes out decrypts to the format's own magic — `GVAS` for Unreal, valid JSON
for Easy Save — so a wrong answer fails instead of quietly producing rubbish.
Whatever is worked out is written down against that game (`crypt/game_keys.py`)
and never worked out twice. All of it reads **files at rest**; SaveSync never
attaches to a running game.

**Detection is layered, not a lookup.** `open_save` does not ask an extension
what a file is and stop there. It builds an ordered list of candidate readers:
the engine detected from the *install* folder first (`core/engines/game_engine.py`
reads the fingerprints an engine leaves beside its executable), then the
extension, then magic bytes and container markers — and each candidate must
decode *and* re-encode the file before it is accepted. A file whose extension
lies about it is still recognised.

All four of those tables come from one description per format in
`core/save_editor/registry.py`: which extensions point at a reader, how it
recognises a file from its own bytes, which engines should try it first, and
whether the engine's name is the better label for it. Adding a format is a
module plus one `FormatSpec` — it used to mean editing three separate places
in `save_editor.py`, where missing one made the format work for files named
the way you tested and silently not for the rest. (Two tables stay out of the
registry on purpose: the lists of formats that are *recognised and not
editable*, which answer "why will this not open" rather than "what is this".)

**A point release must not take your saves away.** Some formats decide their
layout from an engine version number in the file. Those thresholds are written
down from the engine's history, so they are right about every build that
existed when they were written and can be wrong about the next one — and being
wrong that way is indistinguishable from "this is not that format". A game
updating could put its saves out of reach with nothing actually wrong.

So a format that can be wrong that way offers alternative readings, and
SaveSync tries them **after** reading the file as written has failed — never
before, so nothing that opens today is affected. An alternative reading has to
earn acceptance twice:

1. the byte-exact round trip every reader here must pass, and
2. proof that it actually *parsed* what it rebuilt. Byte-equality alone is not
   enough: a reader that carries what it could not parse as an opaque tail
   rebuilds the file perfectly while having understood almost none of it. A
   reading that found no values, or left most of the file unaccounted for, is
   refused however exact its output.

This runs in two passes, and the second is what makes it general:

1. **Other readings of the formats that were tried.** For layouts chosen by a
   version number. Unreal (GVAS) enumerates the four switches its header
   drives — UE5 package version, custom version block, property GUID, and the
   5.4 property-tag shape — so a build that moves one of them is one of
   sixteen shapes rather than an unknown file.
2. **Every other reader there is.** Detection works from what a file says
   about itself: extension, magic, the engine of the game it belongs to. An
   engine update can change any of those, and then a save stops *looking* like
   itself while the reader that understands it is still sitting right there,
   never offered because nothing pointed at it. Trying them all is safe for
   exactly one reason: the gate does not care how a reader was reached. This
   is what makes version tolerance apply to **every** format, including ones
   added later, with nothing to write down for each.

The two readers that *search* for a key — unpacking a game's archives, or
scanning its binaries — are left out of the second pass. They are worth
minutes when something points at them, never on the off-chance.

**The engine the library already knows beats guessing again.** The
first-pass engine detection re-reads the install folder from scratch by
default, and when the game is in the library its own confirmed answer is
trusted instead — re-deriving it and getting a different answer used to mean
a game the library already had right could still open its own saves as
"unsupported". Re-derivation, when it does run for this one purpose, also
scans the install folder without the entry-count cap the same lookup uses
everywhere else (rendering a whole library of cards is not the moment to
risk turning into a directory walk; opening one save, deliberately, is).

**"Unsupported" is not the end of it.** Three more live options sit right
there: check the two searching readers after all — worth the wait now that
it is a single, deliberate file rather than a blind cost paid on every
failure — name the engine directly and skip guessing altogether, or ask the
editor to try breaking the file's obfuscation (see
[Files nothing recognises](#files-nothing-recognises) below). Whichever of
the first two works gets written back to the game's own engine field, so
the same detour is never needed twice — the third never claims to have
identified an engine, so there is nothing to remember.

The same obfuscation-breaking offer also appears over a save that DID open
but only read-only: a reader understood it well enough to show its values
and not well enough to trust writing them back, and a recipe occasionally
does better. Asking never makes things worse — a recipe that finds nothing
leaves the read-only reading exactly as it was.

When auto-resolution fires it says so in the log, naming the reader and the
layout it used.

What this deliberately is **not**: there is no inspection of a running process
and no execution of game code, and nothing is ever written back on a reading
that did not reproduce the original exactly. Formats that hide their layout
somewhere neither the save nor the game's files expose plainly stay in the
right-hand column of the table above, named rather than mangled.

<details>
<summary><strong>How the editor reads and writes these formats</strong></summary>

#### At a glance

| | What it covers |
|---|---|
| [Ren'Py](#renpy) | Pickles read opcode by opcode; edited saves re-signed |
| [Values by category](#values-by-category) | Why the groups come from the file itself |
| [Easy Save 3](#easy-save-3) | Encryption is the developer's choice, and where the key comes from |
| [Locking a value](#locking-a-value) | Holding a value against the game's own writes |
| [RAGS](#rags) | Three million values in the file, fifteen thousand offered |
| [KiriKiri](#kirikiri) | A readable UTF-16 dictionary, in three wrappers |
| [TyranoScript](#tyranoscript) | `escape()` counted in UTF-16 code units, not characters |
| [AliceSoft](#alicesoft) | Several formats in one container; the names live in the game |
| [Artemis](#artemis) | Three files in one container, split by what can be edited |
| [Naninovel](#naninovel) | A raw deflate stream with nothing announcing it |
| [Unity PlayerPrefs](#unity-playerprefs) | A save with no file behind it: the Windows registry |
| [Encrypted Unreal saves](#encrypted-unreal-saves) | Recognised by the folder they sit in, not by their contents |
| [Bakin and SRPG Studio](#bakin-and-srpg-studio) | Named but not edited, and why |
| [Wolf RPG](#wolf-rpg) | Unlocked, read, locked back; names from the game's database |
| [Wolf RPG, the LZ4 variant](#wolf-rpg-the-lz4-variant) | A second lock some builds use; both its key and its write-time byte-swap are now closed-form formulas, search only as a fallback |
| [TADS](#tads) | Two layouts, one a NUL-padded line of tokens |
| [Java](#java) | Bundled-JVM layouts; SQLite progress offered cell by cell |
| [WebGL](#webgl) | HTML5 shells, and why the wrapper is not the name |
| [Files nothing recognises](#files-nothing-recognises) | A bounded battery of generic unwrap recipes, tried only when asked |

##### Ren'Py

Two notes on Ren'Py, because they are unusual. Its saves are Python pickles,
and unpickling one runs code from the file — so SaveSync reads the pickle
opcode by opcode and never builds anything out of it. And Ren'Py 8 refuses a
save whose signature does not match its contents, so an edited save is
re-signed with the key Ren'Py generated on this machine; if that key cannot be
found, the save is not written rather than handed back in a state the game
would reject.

##### Values by category

A save is not one long list — an engine keeps its
switches, its variables, the party and the actors separately, and the editor
shows them that way, one group at a time with the filter and the pager working
inside it. The groups come from the file itself, so a format nobody planned
for still gets a working selector. It matters for more than tidiness: RPG
Maker keeps switches and variables in containers with the same internal name,
so without this, switch 12 and variable 12 look like the same value and
neither can be locked.

##### Easy Save 3

Easy Save 3 files are JSON, and encryption is something the developer
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

##### Locking a value

Set a value, press the lock next to it, and SaveSync
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

##### RAGS

RAGS saves are a .NET object graph behind a fixed key, and they are big:
the one this was built against holds three million values, nearly all of them
the colours, fonts and command lists that make up the game's own logic. So the
whole save is read — the format is sequential, there is no skipping — but what
is offered is the part that is game state: variables, objects, rooms, timers
and the player, each under its own name. Fifteen thousand values instead of
three million.

##### KiriKiri

KiriKiri saves are not a binary format at all: the game writes its state
out as a readable dictionary in UTF-16, and SaveSync edits it in place. It
arrives in three wrappers — the text on its own, deflated, or behind the
thumbnail the game shows in its load menu — and all three are written back
exactly as they came.

##### TyranoScript

TyranoScript saves are JSON that the engine ran through JavaScript's
`escape()`, so the file is readable text with most of its punctuation written
as `%XX`. The catch is that `escape()` counts in UTF-16 code units rather than
characters, which makes an emoji a *pair* of sequences — get that wrong and
every save holding one is quietly refused. A save also holds far more than the
player's state: the label map, the macro map, the script buffer, the line
currently on screen. One of the files this was built against is 11 MB and
holds 774 values worth editing, so what is offered is the game's own
variables, one group per save slot, and not the engine's bookkeeping.

##### AliceSoft

AliceSoft puts several different things in the same container, and they
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

##### Artemis

Artemis writes three files into one container, and the same split applies:
the engine's settings are a flat list of named values and are edited, while
the save slots and the data kept across playthroughs are a tagged tree whose
nesting this was not able to establish. Reading the settings ends exactly on
the last of the entries the file says it has, which is what says the walk was
right; the other two are named instead.

##### Naninovel

Naninovel hides its saves twice over. A `.nson` is usually not text at all
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

##### Unity PlayerPrefs

Unity PlayerPrefs are a save with no file behind them. On Windows they
live in the Windows registry (and on Linux under Proton/Wine inside the
prefix's `user.reg`), under the company and product the game was built with,
and SaveSync backs them up by exporting the key tree as JSON. That same
export is what the editor opens, so the two cannot disagree about what a key
contains. Unity hides each preference's name behind a checksum of it, and the
checksum cannot be turned back into anything — but it does not need to be,
since the name is written in front of it. Only the tail is dropped, and only
for display: a value goes back under the name it had, as the kind it was, or
the game would not find it.

##### Encrypted Unreal saves

An encrypted Unreal save is recognised by where it sits — `Saved/SaveGames`
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

##### Bakin and SRPG Studio

Bakin and SRPG Studio saves are named but not edited. Bakin's is an
object stream with nothing in it naming or typing the values. SRPG Studio
encrypts its saves outright — every byte of one is as likely as every other,
so nothing in the file can identify it, and the only thing that can is the
game it sits in. That is what the engine detector is asked for, and when the
game is not in the library SaveSync says so, since the key to such a save
lives in the game's own program and there is no reaching it without one.

##### Wolf RPG

Wolf RPG saves are scrambled on disk, so SaveSync unlocks one, reads the
values out and locks it back. Names come from the game's own database when it
sits beside the save — level, HP, attack, in the game's own words. A game that
packs that database away still gets every value, numbered instead of named. A
Wolf file with no values in it, such as some games' `System.sav`, is reported
as unreadable rather than opened and guessed at.

##### Wolf RPG, the LZ4 variant

Some Wolf RPG Editor builds lock their saves a second way instead of (or
alongside) the documented scheme: a byte in the header says which, and files
carrying the newer one are unlocked with a custom Mersenne Twister keystream
rather than the standard obfuscation. Once unlocked and decompressed the
result is the exact same variable-database shape the standard reader already
knows — this one only replaces how the bytes get unlocked, not how they are
read afterward.

Two separate closed-form formulas make this run without ever touching the
game, both recovered the same way: running the game's own
(anti-disassembly-obfuscated) code through an emulator as an oracle rather
than reading it by hand, then confirmed exactly against real saves, not
just internally consistent.

The keystream's own seed comes from three salt bytes in the file's clear
header. Each contributes to the seed on its own, as a pure XOR function of
its own byte with no carries — confirmed against thousands of emulated data
points and every real save this project has ground truth for, zero
mismatches — so the closed form runs instantly, no search, no cache, no
game needed, and covers every build tested so far.

Separately, the write path also applies a post-encrypt byte-swap whose two
20-byte span positions used to only be locatable by search — which turned
out, on one real file, to have a genuine correctness bug: more than one
offset pair can pass even the search's strongest validator, and the wrong
one silently corrupts thousands of scattered field values with no error
raised. Those two span positions turn out to be a plain, disjoint,
wraparound function of two OTHER bytes already sitting in the same clear
header — no seed, no decryption, no search needed to find them either, and
confirmed against the exact file the old search got wrong.

The 2^32 brute-force search that originally found the seed formula's own
validation data (and the older, occasionally-wrong swap search) is kept
only as a defensive fallback: on a salt or header neither formula's output
verifies against the file's own decrypted shape — none seen yet — SaveSync
falls back to searching, shown with a progress bar and cancellable like an
Easy Save 3 password hunt, and remembers the answer so a repeat search
(same slot) is never needed either.

##### TADS

TADS covers two layouts. The TAD-kit style (picture packs as `.tad` under
`pic/`, plus `system/system.rec`) is recognised from the install folder; the
record file is a line of whitespace-separated tokens padded with NULs to a
fixed size, and SaveSync edits those tokens and writes the same byte length
back — open with no changes is byte-identical. Classic MJR TADS 2/3 image
files (`.gam` / `.t3`) are recognised the same way; their `.t3v` VM state
snapshots are named but not edited, because they are a machine dump rather
than a list of named values.

##### Java

Java desktop titles are recognised from a bundled JVM (`java.dll` with
`jvm.dll` / `awt.dll` next to the launcher), from jars beside a private JRE,
or from the Compose Desktop / Conveyor layout (`bin/` launcher + sibling
`app/*.jar`), including MSIX installs under WindowsApps. Progress is often a
SQLite database (Android Room on the desktop): tables the game owns are
offered cell by cell, Room's own schema tables are skipped, and a no-edit
open that does not touch a page stays bit-identical — after a real edit the
gate checks that every value still round-trips, because SQLite rewrites
pages when it writes.

##### WebGL

WebGL is the answer for HTML5 / WebGL shells: Unity WebGL exports
(`Build/*.wasm` and friends), plain `index.html` + wasm/data, and NW.js or
Electron packages whose main page is HTML — those are named WebGL rather than
"NW.js", for the same reason Tyrano is named before the wrapper. Saves are
normally JSON beside the game (and LZString / SugarCube when that is what the
page wrote); indent and line endings are kept when they are unambiguous, so an
untouched open/close is byte-for-byte.

It edits files at rest. Nothing is injected into a running game and nothing
attaches to one.

##### Files nothing recognises

Cracking the Wolf RPG LZ4 variant above by hand — reverse-engineering the
game's own obfuscation from scratch — is not something SaveSync tries to
automate generally: that took live instrumentation of a running game, which
is exactly what this project's own rule (see above) rules out doing
automatically. What generalises is the *shape* the result turned out to
have, and several other readers already share: a short, reversible byte
transform — a repeating XOR key, sometimes an old-fashioned `rand()`-style
keystream seeded from a byte or two of the file's own header — optionally
followed by ordinary compression, wrapped around content a normal reader
would already recognise once it is undone.

Asked for explicitly (see above — never on an already-openable file, and
never as part of "check every format"), the editor tries a small, bounded
battery of exactly those recipes: a handful of transforms, each tried at a
few plausible header lengths, each optionally followed by zlib, raw
deflate, or LZ4. A transform's result is checked cheaply first — does the
start of it look like JSON, XML, a zip, SQLite, or plain text — and only a
real candidate pays for being handed to every registered reader in full,
which is what decides whether it actually opens: the same byte-exact-or-
value round trip every save in this editor is held to, proven in both
directions before anything is offered for editing. Nothing is guessed at;
a search that finds nothing changes nothing.

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

**In this section:**

| Part | What it covers |
|---|---|
| [How adaptive limits work](#how-adaptive-limits-work) | How the machine is sized up and what scales with it |
| [Settings reference](#settings-reference) | Every setting, its default and what it does |
| [Config export & history](#config-export--history) | Exporting and importing settings, and the history |
| [Data-integrity checks](#data-integrity-checks) | The checks that run on a schedule and on demand |
| [Offline repair](#offline-repair) | The one tool that can rewrite an archive index |
| [Diagnostics](#diagnostics) | Tracing child processes and windows |

### How adaptive limits work

At runtime SaveSync classifies the host into a coarse tier from **logical CPU
count**, **nominal CPU clock** and **total RAM** (via `psutil` when present).
That tier is **cached ~45 s** so a brief free-RAM dip (game launch, antivirus)
does not flip library chunking / verify pacing on and off. **Currently
available RAM** is still read live for Backup All / Sync All: under ~1.5 GB
free, those queues drop to one job so they do not fight the game for memory.

<details>
<summary><strong>How the tiers and limits work</strong></summary>


**Cores are not the whole story, in either direction.**

*How big the machine is.* Core count alone over-rates an old CPU: a 2013
eight-core is not the machine a modern eight-core is, and it is the one that
stutters when eight things start at once. A nominal clock below ~2.6 GHz
therefore **demotes** the tier. Only ever downwards, and an unknown clock
(some platforms do not report one) changes nothing.

*How busy it is right now.* Core count says how many things **can** run, not
whether they already are — and a PC with a game on it looks identical to an
idle one through `os.cpu_count()`. So Backup All and Sync All also read
**live CPU utilisation** and cut their ceiling back:

| Recent CPU load | Parallel backup / sync jobs |
|-----------------|------------------------------|
| under ~55 % | the tier's full ceiling |
| ~55 – 70 % | two thirds |
| ~70 – 85 % | half |
| over ~85 % | one at a time |
| unknown | unchanged |

The reading is sampled from `cpu_times` deltas over at least 0.8 s and cached
~3 s — deliberately **not** `cpu_percent(interval=None)`, which measures since
its own last call *anywhere in the process*, so a second caller would silently
shorten the window. It only ever **lowers** the ceiling, so a bad reading
costs throughput and never correctness, and both queues re-ask on **every**
job: a sweep over a large library outlives the conditions it began in, and a
ceiling fixed at the start never came down when a game launched half-way
through.

| Tier | Rough signal | Throughput | CPU & memory upkeep |
|------|--------------|------------|---------------------|
| **high** | ≥ 8 logical CPUs and ≥ ~12 GB total RAM | No pause between integrity checks; library page built in one shot; write debounce ~0.5 s; Backup All / Sync All up to 8 / 4 in parallel | Process poll at the interval you set (×1); memory sweep every 60 s, deep every 10th; a loaded save released after 15 min idle |
| **mid** | typical desktop (≥ 4 CPUs, comfortable total RAM) | Light verify pause (~20 ms); library inserts in chunks of ~16; debounce ~1 s; moderate batch concurrency | Poll ×1.5; sweep every 45 s, deep every 12th; save released after 10 min |
| **low** | few CPUs, or ≤ ~8 GB total RAM | Stronger verify pause (~80 ms); smaller library chunks (~6); debounce ~2 s; Backup All / Sync All capped tightly (often 1–2) | Poll ×2.5 — fewer wake-ups; sweep every 30 s but deep only every 16th; save released after 5 min |

The upkeep column deliberately moves in **different directions**. A weak
machine should poll *less* often (spend less CPU), reclaim memory *more*
often, and let go of a loaded save *sooner* — but it should defer the
*expensive* sweep, not hurry it, because that one throws away decoded cover
art and hands the working set back, and re-decoding is precisely what a slow
machine can least afford. One shared multiplier would have got at least one
of those backwards.

**Three kinds of sweep.** The routine tick only drops what is already dead —
registry rows whose widget is gone, watcher file indices once nothing is being
watched. It costs about 12 ms including the next process poll. A medium pass
hands the working set back to the OS but keeps decoded covers and the loaded
save, so it's safe to run while you're actively using the app — it's what
keeps RSS from just climbing during a long session, roughly every
`memory sweep` interval below. The full trim (also purge cover/image caches,
full GC) costs roughly 100 ms and runs on a long counter, or immediately when
free RAM is genuinely short — but never at the cost of a visible repaint: if
the window is on screen and you're merely not focused on it (not genuinely
away from the PC, and memory isn't actually tight), the full trim holds back
to the medium pass for whichever page you're looking at specifically, while
still fully cleaning every other, off-screen page.

**It paces itself by what the app is doing**, not by the clock:

- **Real memory pressure wins** over everything and forces a full trim now
- **Heavy work is left alone** — a backup walking save trees, an archive
  verify, a sync, a folder scan minimised into the sidebar. They want the
  memory and CPU they are using; the cadence snaps back to its floor so the
  first sweep *after* that work lands quickly, which is when there is most to
  reclaim
- **A barren sweep doubles the wait**, up to a per-tier ceiling (15 / 10 / 6
  min). Repeating a cleanup that frees nothing costs a wake-up and buys
  nothing; one that *does* reclaim something returns to the floor cadence

The tier multiplier scales the `process_poll_interval` you set — it never
replaces it. None of the rest are Settings toggles. Batch jobs (Backup All, Sync All,
multiple-add) also **persist progress** and resume after a restart; the sidebar
shows `N/M — name` while they run.

Other automatic I/O habits worth knowing:

- **Filesystem watcher** coalesces save bursts (~5 s, ~8 s when many files are
  pending) before acting — discovery only (surfacing the folder for
  confirmation, and starting the in-game backup timer the first time it
  finds something) unless `backup_during_game` is explicitly turned on, in
  which case it also backs up reactively on every settled burst. The actual
  periodic backup while playing always runs on the per-game interval set in
  Add/Edit Game, independent of this
- **Backup / sync “already current”** uses an mtime + entry-count preflight so
  unchanged games are skipped without rebuilding zip content hashes
- **Config export history** (snapshots created on export, cloud upload,
  pre-import / pre-restore) keeps at most **5** local folders under
  `config_history/`; older ones are rotated out. A sandbox check verifies
  that restore still works when the history is full — one of the
  data-integrity checks below, not a separate startup pass
- **Pending batch jobs persistence** (`core/pending_batch_jobs.py` / `pending_batch_jobs.json`) —
  tracks active multi-operation batch queues (Backup All, Sync All, Multi-Add)
  and safely restores remaining items after an unexpected restart
- **Page-size crash guard** for oversized custom page sizes uses a tiny sidecar
  file (`page_size_render_guard.json`), not a full rewrite of `config.json`

</details>

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
| `idle_after_minutes` | 10 | How long SaveSync must go **unused** before its heavier upkeep may run — releasing the loaded save and the decoded cover art back to the system. A setting rather than a constant because the cost of getting it wrong falls on you: called idle too eagerly, putting SaveSync aside for a minute means coming back to a page that has to build itself again |
| `save_scan_debounce` | 5s | How long the filesystem watcher waits for a save burst to settle before it triggers a backup. Raised by 3s while many files are still pending, and clamped to 1–30s |
| `backup_on_exit` | true | Auto-backup when a game closes |
| `backup_during_game` | false | Periodic in-game backups while playing (interval in Settings) |
| `auto_scan_on_exit` | true | Scan for save paths when a game exits |
| `auto_sync_after_backup` | false | Sync to cloud after each backup |
| `save_correlation_enabled` | false | Claim saves by write-time correlation (see above) |
| `save_correlation_window_ms` | 1000 | How far apart the two writes may be. Weaker candidates get 40% of it |
| `backup_verify_enabled` | true | Check backup archives on a schedule |
| `backup_verify_interval_days` | 7 | How often that check runs |
| `self_checks` | true | Run the data-integrity checks automatically |
| `self_checks_frequency` | 7 | Days between automatic runs (the manual ⚕️ on the Backups page runs the same list on demand) |
| `auto_export_config_enabled` | false | Periodically upload an encrypted config pack to the connected sync provider |
| `auto_export_config_interval_days` | 7 | How often that cloud config export runs |
| `page_sizes` | per list | Items per page for library, backups, save editor, reviews (presets 10 / 20 / 50, or custom) |
| `backup_archives_too` | true | Include archives — save folders added by hand, with no game in the library — in Backup All and Sync All |
| `sync_timeout` | 120s | How long a single cloud operation may take before it is abandoned (10–600) |
| `auto_backup` | false | Back up on a timer regardless of what is running |
| `minimize_to_tray` | true | Don't close SaveSync when you click ✕ — keeps it running in the tray instead of quitting |
| `hide_to_tray_on_game_launch` | true | Get out of the way when a game starts |
| `launch_on_startup` | true | Register for launch at login — HKCU `Run` on Windows, an XDG `.desktop` in `~/.config/autostart` on Linux, a LaunchAgent on macOS |
| `start_minimized_on_startup` | false | That autostart launch skips the window and goes straight to the tray; a manual double-click always opens it normally |
| `overlay_hotkey` | `alt+ctrl+s` | Global shortcut that opens the in-game overlay |
| `show_overlay_on_launch` / `_backup` / `_cloud` / `_unknown` | true | Which events the overlay speaks up for |
| `check_for_updates` | true | Look for a newer release (`update_check_interval_sec`, 0 = at startup only) |
| `extra_watch_paths` | [] | Additional folders the filesystem watcher should keep an eye on |
| `theme` / `language` | `dark` / `en` | Appearance and tool language |
| `p2p_username`, `p2p_max_per_game`, `p2p_retention_days`, `p2p_files_dir`, `p2p_port`, `p2p_upnp`, `p2p_friends_online`, `p2p_friends_autolock_minutes`, `p2p_proxy`, `p2p_proxy_host`, `p2p_proxy_port`, `p2p_proxy_local_dns` | see the P2P section | P2P display name, how many received saves to keep and for how long, the folder files sent by hand are downloaded into, the connection port and UPnP, whether friends are checked in the background, and the optional SOCKS5 proxy — described under *Settings* in [P2P save transfer](#p2p-save-transfer) |
| `ui_scale_auto` / `ui_scale_factor` | true / 1.0 | Follow the screen's DPI, or pin a factor by hand |

Everything else in `config.json` is remembered state rather than a
setting — window geometry, which pins are open, the machine id, the
timestamps that say when a scheduled job last ran. It is listed by
`--vocab-help`-style validation rather than here: all 80 keys are
shape-checked on load, and one that fails goes back to its default
instead of taking a page down with it.

### Config export & history

From **Settings → Transfer** you can export / import an encrypted
`.savesync` pack (settings, library, optional credentials) and browse
**Configuration History**. Each successful export or cloud upload also writes a
local snapshot; at most five are kept. Restoring or importing first snapshots
the current state (`pre_restore` / `pre_import`) so you can undo.

### Data-integrity checks

One ordered list of checks, run from exactly two places — automatically on
the schedule you set (`self_checks_frequency`, default 7 days) and on demand
from the ⚕️ button on the Backups page. Both run the same six:

| Check | What it does |
|-------|--------------|
| index zip-existence | drops index rows whose archive is gone (deleted outside SaveSync, a failed sync) |
| legacy metadata repair | rebuilds the manifest / save hash on backups made before per-file manifests, so the dedup preflight stops treating those games as permanently changed |
| archive verification | opens every zip and CRC-checks its contents — the only check that proves a backup can actually be restored |
| config snapshots | a sandbox restore over a full rotated history |
| settings integrity | the OS autostart registration against what `launch_on_startup` wants — repairs a stale entry left by an old build (its path/filename baked in changes every update) or removes one left behind after the setting was turned off |
| picture cache | deletes the hidden temporary files (`.<hex>.part`) a picture download leaves in a game's icon folder if the app is killed while writing one; a file a download could still own (this run, under 10 minutes old) is left alone |

Progress appears in the sidebar, and past 30 seconds the notice offers a
**Cancel**: cancellation is cooperative and lands between archives, so a
sweep over hundreds of them can be stopped without waiting it out. Per-backup
results are written into the index, which is where the health dot on each
backup row gets its colour.

The sweep does **not** run at startup on its own — it is scheduled work, so a
launch never pays for it.

### Offline repair

`maintenance/repair_archives.py` is the one thing that can rewrite an archive
index, and it is deliberately outside the app: nothing in SaveSync repairs
these on its own, because each finding has more than one plausible repair and
the wrong one deletes the only path an archive can be read from.

It is **read-only by default** — `--apply` is the only way to write anything,
`--fix` names which kinds to touch, and every write is preceded by a
timestamped backup of the index file it changes.

```
python maintenance/repair_archives.py                  # diagnose everything
python maintenance/repair_archives.py --game GameName # one archive
python maintenance/repair_archives.py --apply --fix injected-source,lost-chain
```

| Finding | What it means |
|---------|---------------|
| `injected-source` | the recorded **origin** list contains the archive's own restore **destination** — a re-backup then reads the same saves twice |
| `clobbered-dest` | the recorded destination is really one of the sources, written over the real one |
| `lost-chain` | chains went empty while an older entry still carries them, so a restore has nothing to rebuild the destination from |
| `doubled-zip` | the zip holds a root that came from the destination: the same saves stored a second time. Not repairable in place — delete the entry and take a fresh backup |
| `empty-folder` | a backup folder with no zip and no index, debris that pushes the next attempt onto a tagged name (`Name~7f31c0`) |
| `orphan-dup` | two archives with the same identity built from different source folders |
| `not-published` / `index-drift` | with `--remote-dir`: entries the provider never received, and drift between the local folders and the master index |

Only `injected-source`, `lost-chain` and `empty-folder` have a repair; the
rest are reported so the decision stays with you.

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
> (https://github.com/Luke0094/SaveSync-Your-saves-everywhere) — SaveSync - Your saves everywhere
