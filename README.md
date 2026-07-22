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
- **Launcher URL support** — games launched through `steam://`-style URLs are
  resolved to their real executable
- **Playtime tracking** per game, with per-session detail on hover

### Backups & sync
- **Versioned local backups** with retention (max count, days, minimum kept,
  size cap) and content-dedup (unchanged saves are skipped)
- **Pre-restore safety backups** — automatic backup before any restore
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
│   ├── library.py                 # Game library CRUD, tag canonicalization
│   ├── monitor.py                 # Process monitor (cached snapshots, adaptive polling)
│   ├── watcher.py                 # Real-time filesystem watcher, debounced events
│   ├── save_detector.py           # Heuristic save folder detection and scoring
│   ├── registry_saves.py          # Windows-registry save locations
│   ├── backup.py                  # Versioned zip backups, retention, dedup
│   ├── resolvers.py               # Launcher URLs, executable resolution
│   ├── game_api.py                # Web metadata search orchestration
│   ├── game_sources/              # Steam, VNDB, wikis, store/forum scrapers
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
    ├── overlay.py                 # In-game overlay (notifications, prompts, restore)
    ├── unknown_history.py         # Pending unknown-game queue (persistence)
    ├── blur_modal.py              # Fullscreen blur backdrop for modal flows
    ├── styles/theme.py            # Dark/Light QSS theme manager
    ├── pages/                     # Overview, Library, Sync, Backups, Settings
    ├── widgets/                   # Game cards/rows, folder tree, hotkey editor, …
    └── dialogs/                   # Add/edit game, auto-scan, restore, conflicts,
                                   # cloud verify, config import, credits
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

---

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
| `process_poll_interval` | 1s | Base scan interval (auto-slows in game / when idle) |
| `backup_on_exit` | true | Auto-backup when a game closes |
| `auto_scan_on_exit` | true | Scan for save paths when a game exits |
| `auto_sync_after_backup` | false | Sync to cloud after each backup |

### Diagnostics

Run with `SAVESYNC_TRACE=1` to log every spawned child process and every
top-level window shown, each with its call site — useful for hunting startup
flashes or stray console windows. Off by default (zero overhead).

---

## License

Released under the [PolyForm Noncommercial License 1.0.0](LICENSE) —
© 2026 [Luke0094](https://github.com/Luke0094).

You may use, modify and share SaveSync freely **for noncommercial
purposes** (personal use, hobby, research, education, nonprofits), keeping
the copyright notice with every copy. Selling this software, or
distributing it as part of a paid product or service, is not permitted.

> Required Notice: Copyright (c) 2026 Luke0094
> (https://github.com/Luke0094/SaveSync)
