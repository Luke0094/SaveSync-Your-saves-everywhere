"""Diagnose — and, only when told to, repair — damaged archive indexes.

Read-only by default. ``--dry-run`` is the default and ``--apply`` is the only
way to write anything; every write is preceded by a backup of the index file
it touches.

What it looks for, in the order the damage happened:

  injected-source   an archive whose recorded ORIGIN list contains its own
                    restore DESTINATION — the pre-sync backup bug. This is
                    what made a re-backup read the same saves twice.
  lost-chain        an orphan entry whose save_chains are empty while an
                    older entry of the same archive still carries them, so
                    restore has nothing to rebuild the destination from.
  doubled-zip       an entry whose zip holds the same saves under two roots.
  empty-folder      a backup folder with no zip and no index: debris from a
                    run that decided not to write, which then pushed the
                    next attempt onto a "_2" / "~tag" name.
  orphan-dup        two archives with the same identity (chain + stripped
                    title) built from different source folders.
  index-drift       local archives missing from the provider's master index,
                    and master-index entries with nothing local behind them.

Usage:
    python maintenance/repair_archives.py                  # diagnose everything
    python maintenance/repair_archives.py --game TomieWGM  # one archive
    python maintenance/repair_archives.py --apply --fix injected-source,lost-chain
    python maintenance/repair_archives.py --apply --fix empty-folder
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

FIXES = ("injected-source", "lost-chain", "empty-folder")


def _out(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        print(str(text).encode("ascii", "replace").decode("ascii"))


def _casefold_set(values):
    return {str(v).casefold() for v in (values or []) if v}


def _roots(manifest):
    out = defaultdict(int)
    for key in manifest or {}:
        out[str(key).replace("\\", "/").split("/")[0]] += 1
    return dict(out)


class Archive:
    """One backup folder, loaded straight off disk."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.index_path = folder / "index.json"
        self.entries: list[dict] = []
        self.zips = sorted(p for p in folder.glob("*.zip"))
        self.error = ""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                self.entries = data if isinstance(data, list) else []
            except Exception as exc:                       # noqa: BLE001
                self.error = f"unreadable index.json: {exc}"

    @property
    def name(self):
        return self.folder.name

    @property
    def is_empty(self):
        """Nothing in the folder at all — not even an unreadable index.

        Deliberately stricter than "no usable entries": a folder holding an
        index.json that failed to parse still holds the user's only record
        of those backups, and is reported as unreadable instead.
        """
        try:
            return not any(self.folder.iterdir())
        except OSError:
            return False

    def newest_first(self):
        return sorted(self.entries,
                      key=lambda e: str(e.get("created_at") or ""),
                      reverse=True)


def _load(backup_dir: Path, only: str = "") -> list[Archive]:
    if not backup_dir.is_dir():
        raise SystemExit(f"backup folder not found: {backup_dir}")
    out = []
    for sub in sorted(backup_dir.iterdir()):
        if not sub.is_dir():
            continue
        if only and only.casefold() not in sub.name.casefold():
            continue
        out.append(Archive(sub))
    return out


# ── checks ──────────────────────────────────────────────────────────────
def _trusted_destinations(arc: Archive) -> set:
    """Destinations from entries that were never clobbered.

    An entry whose save_chains still carry a chain was written with the
    destination the user's folder actually rebuilds to. An entry with empty
    chains had its save_paths overwritten with the SOURCE list, so reading
    destinations off it would flag the user's own archive folder as
    injected — and "repairing" that would delete the one path the archive
    can be read from. Only chain-backed entries are believed.
    """
    out = set()
    for entry in arc.entries:
        chains = entry.get("save_chains") or []
        if not any(c for c in chains):
            continue
        out |= _casefold_set(entry.get("save_paths"))
    return out


def check_injected_source(arc: Archive):
    """Origins that are really this archive's own destination."""
    trusted = _trusted_destinations(arc)
    if not trusted:
        return []                    # nothing reliable to compare against
    found = []
    for entry in arc.entries:
        meta = entry.get("cloud_metadata") or {}
        if not meta.get("orphan"):
            continue
        sources = [str(p) for p in (meta.get("orphan_source_paths") or []) if p]
        if len(sources) < 2:
            continue
        bad = [p for p in sources if p.casefold() in trusted]
        # Never propose emptying the list: if every recorded origin looks
        # like a destination the reading is wrong, not the data.
        if bad and len(bad) < len(sources):
            found.append((entry, bad))
    return found


def check_clobbered_dest(arc: Archive):
    """Recorded destinations that are really one of the archive's SOURCES.

    Per PATH, not per entry. The first version asked whether the whole
    save_paths list was a subset of the sources and was disjoint from the
    trusted destinations — and an entry holding one of each (a real
    destination beside a source that had been written over it) satisfied
    the subset test but not the disjoint one, so it reported clean. That is
    exactly the shape rebackup_archive's clobber produces once the sync bug
    has injected the destination into the source list, i.e. the very entry
    this is meant to find.
    """
    trusted = _trusted_destinations(arc)
    if not trusted:
        # No chain-backed entry to compare against. An archive of a LIVE
        # save folder legitimately records the same path as source and as
        # destination, and without a baseline there is no way to tell that
        # apart from a clobber — so nothing is claimed.
        return []
    found = []
    for entry in arc.entries:
        meta = entry.get("cloud_metadata") or {}
        if not meta.get("orphan"):
            continue
        if any(c for c in (entry.get("save_chains") or [])):
            continue          # chain-backed: this entry is believed
        sources = _casefold_set(meta.get("orphan_source_paths"))
        bad = [p for p in (entry.get("save_paths") or [])
               if p and p.casefold() in sources
               and p.casefold() not in trusted]
        if bad:
            found.append((entry, bad))
    return found


def check_lost_chain(arc: Archive):
    """Orphan entries whose chains went empty while older ones still have them."""
    known = {}
    for entry in arc.newest_first()[::-1]:
        for path, chain in zip(entry.get("save_paths") or [],
                               entry.get("save_chains") or []):
            if chain:
                known[str(path).casefold()] = chain
    found = []
    for entry in arc.entries:
        meta = entry.get("cloud_metadata") or {}
        if not meta.get("orphan"):
            continue
        chains = entry.get("save_chains") or []
        if chains and any(c for c in chains):
            continue
        repair = [known.get(str(p).casefold(), "")
                  for p in (entry.get("save_paths") or [])]
        if any(repair):
            found.append((entry, repair))
    return found


def check_doubled_zip(arc: Archive):
    """Entries whose zip holds a root that came from the DESTINATION.

    More than one root is normal and usually correct: an archive can be
    read from several folders (``Alvein 14c`` and ``Alvein 44b``), and a
    game can keep its saves in two places at once. What is NOT normal is a
    root named after a folder this archive RESTORES to and never reads
    from — that root can only have got there by the pre-sync backup zipping
    the destination, and it sits beside the real one holding the same saves
    a second time.
    """
    found = []
    for entry in arc.entries:
        meta = entry.get("cloud_metadata") or {}
        if not meta.get("orphan"):
            continue           # a library game's save_paths ARE its sources
        roots = _roots(meta.get("file_manifest"))
        if len(roots) < 2:
            continue
        trusted = _trusted_destinations(arc)
        # The injected path is itself in the source list by now, so reading
        # source names off it would make the stray root look legitimate.
        src_names = {Path(p).name.casefold()
                     for p in (meta.get("orphan_source_paths") or [])
                     if p and str(p).casefold() not in trusted}
        if not src_names:
            continue
        dest_names = {Path(p).name.casefold() for p in trusted}
        strays = [r for r in roots
                  if r.casefold() not in src_names
                  and r.casefold() in dest_names]
        # At least one root has to line up with a real source, or this is a
        # legacy archive whose roots simply cannot be matched and guessing
        # would only produce noise.
        if strays and any(r.casefold() in src_names for r in roots):
            found.append((entry, roots, strays))
    return found


def check_not_published(arc: Archive, remote_root: Path):
    """Entries the provider genuinely never received.

    NOT "missing from the remote index": the provider enforces the same
    retention as the local store (max_local_backups / retention_days /
    min_kept_backups), so an old backup that WAS uploaded and later pruned
    up there is absent by design. Counting those reported 18 unpublished
    entries where exactly one had never gone anywhere.

    The real signal is the entry's own bookkeeping: no ``synced_to`` at all,
    or a pending ``index_needs_publish`` that the provider's index does not
    reflect.
    """
    if not remote_root:
        return []
    remote_index = remote_root / arc.name / "index.json"
    remote_ids = set()
    remote_readable = remote_index.exists()
    if remote_readable:
        try:
            remote = json.loads(remote_index.read_text(encoding="utf-8"))
            remote_ids = {e.get("backup_id") for e in (remote or [])}
        except Exception:                                   # noqa: BLE001
            remote_readable = False
    # The NEWEST entry only. index_needs_publish is written onto every entry
    # of an archive so the answer survives retention pruning the newest one,
    # which means an old entry the provider pruned years ago carries the flag
    # for ever — reporting those said "13 unpublished" about one archive that
    # was actually behind.
    out = []
    newest = arc.newest_first()
    if not newest:
        return out
    entry = newest[0]
    meta = entry.get("cloud_metadata") or {}
    if entry.get("backup_id") not in remote_ids:
        if not (meta.get("synced_to") or []):
            out.append((entry, "newest backup was never uploaded"))
        elif remote_readable:
            out.append((entry, "newest backup is not in the provider's index"))
    elif meta.get("index_needs_publish") and remote_readable:
        out.append((entry, "index change pending publication"))
    return out


def _identity(entry):
    chain = next((c for c in (entry.get("save_chains") or []) if c), "") \
        or next((c for c in (entry.get("content_chains") or []) if c), "")
    return (chain.replace("\\", "/").strip("/").casefold(),
            str(entry.get("game_name") or "").strip().casefold())


# ── repairs ─────────────────────────────────────────────────────────────
def _save(arc: Archive, apply: bool):
    if not apply:
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = arc.index_path.with_suffix(f".json.bak_{stamp}")
    if arc.index_path.exists() and not backup.exists():
        shutil.copy2(arc.index_path, backup)
    arc.index_path.write_text(
        json.dumps(arc.entries, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--remote-dir", default="")
    ap.add_argument("--game", default="", help="only folders matching this")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: report only)")
    ap.add_argument("--fix", default="",
                    help=f"comma-separated: {', '.join(FIXES)}")
    args = ap.parse_args()

    backup_dir = Path(args.backup_dir) if args.backup_dir else \
        Path(os.environ.get("APPDATA", "")) / "SaveSync" / "backups"
    remote_root = Path(args.remote_dir) if args.remote_dir else None
    wanted = {f.strip() for f in args.fix.split(",") if f.strip()}
    unknown = wanted - set(FIXES)
    if unknown:
        raise SystemExit(f"unknown --fix value(s): {', '.join(sorted(unknown))}")
    if args.apply and not wanted:
        raise SystemExit("--apply needs --fix to say what to repair")

    archives = _load(backup_dir, args.game)
    _out(f"backup folder : {backup_dir}")
    if remote_root:
        _out(f"provider folder: {remote_root}")
    _out(f"archives       : {len(archives)}")
    _out(f"mode           : {'APPLY' if args.apply else 'dry-run (no writes)'}")
    _out()

    totals = defaultdict(int)
    empty_folders = []

    for arc in archives:
        notes = []
        if arc.error:
            notes.append(("unreadable", arc.error))
        if arc.is_empty:
            empty_folders.append(arc)
            totals["empty-folder"] += 1
            continue

        injected = check_injected_source(arc)
        for entry, bad in injected:
            notes.append(("injected-source",
                          f"{entry.get('backup_id')}: origin list contains its own "
                          f"destination -> {bad}"))
        totals["injected-source"] += len(injected)

        lost = check_lost_chain(arc)
        for entry, repair in lost:
            notes.append(("lost-chain",
                          f"{entry.get('backup_id')}: chains empty, recoverable "
                          f"as {repair}"))
        totals["lost-chain"] += len(lost)

        clobbered = check_clobbered_dest(arc)
        for entry, bad in clobbered:
            notes.append(("clobbered-dest",
                          f"{entry.get('backup_id')}: save_paths records "
                          f"{bad} — a SOURCE folder, not a restore "
                          f"destination"))
        totals["clobbered-dest"] += len(clobbered)

        doubled = check_doubled_zip(arc)
        for entry, roots, strays in doubled:
            notes.append(("doubled-zip",
                          f"{entry.get('backup_id')}: roots {roots}; "
                          f"{strays} came from the destination, not from a "
                          f"recorded source — the saves are in there twice. "
                          f"Not repairable in place: delete this entry and "
                          f"take a fresh backup."))
        totals["doubled-zip"] += len(doubled)

        if remote_root:
            missing = check_not_published(arc, remote_root)
            for entry, why in missing:
                if why == "index change pending publication":
                    # Counted apart: this is the FLAG being set, which the
                    # deferred batch flush used to re-set on every archive
                    # right after a sync had cleared it. It says a sweep
                    # will visit the archive again, not that anything is
                    # missing up there — and it clears itself on the next
                    # successful sync now that the flush no longer re-marks.
                    totals["publish-flag-pending"] += 1
                    continue
                notes.append(("not-published",
                              f"{entry.get('backup_id')}: {why}"))
                totals["not-published"] += 1

        if notes:
            _out(f"--- {arc.name}")
            for kind, text in notes:
                _out(f"    [{kind}] {text}")

        # repairs
        changed = False
        if "injected-source" in wanted and injected:
            for entry, bad in injected:
                meta = entry.setdefault("cloud_metadata", {})
                drop = _casefold_set(bad)
                meta["orphan_source_paths"] = [
                    p for p in (meta.get("orphan_source_paths") or [])
                    if str(p).casefold() not in drop]
                meta["index_needs_publish"] = True
                changed = True
                _out(f"    -> dropped {len(bad)} injected origin(s) from "
                     f"{entry.get('backup_id')}")
        if "lost-chain" in wanted and lost:
            _clobbered_ids = {row[0].get("backup_id") for row in clobbered}
            _doubled_ids = {row[0].get("backup_id") for row in doubled}
            for entry, repair in lost:
                if entry.get("backup_id") in _clobbered_ids | _doubled_ids:
                    _out(f"    -> SKIPPED {entry.get('backup_id')}: its "
                         f"save_paths are sources, so a chain would describe "
                         f"the wrong folder")
                    continue
                entry["save_chains"] = list(repair)
                if not any(entry.get("content_chains") or []):
                    entry["content_chains"] = list(repair)
                (entry.setdefault("cloud_metadata", {}))["index_needs_publish"] = True
                changed = True
                _out(f"    -> restored chains on {entry.get('backup_id')}")
        if changed:
            _save(arc, args.apply)

    if empty_folders:
        _out()
        _out(f"--- empty backup folders ({len(empty_folders)})")
        for arc in empty_folders:
            _out(f"    {arc.name}")
        if "empty-folder" in wanted:
            for arc in empty_folders:
                if args.apply:
                    try:
                        arc.folder.rmdir()
                        _out(f"    -> removed {arc.name}")
                    except OSError as exc:
                        _out(f"    !! could not remove {arc.name}: {exc}")
                else:
                    _out(f"    -> would remove {arc.name}")

    # same identity, different source folder
    by_identity = defaultdict(list)
    for arc in archives:
        for entry in arc.newest_first()[:1]:
            meta = entry.get("cloud_metadata") or {}
            if not meta.get("orphan"):
                continue
            by_identity[_identity(entry)].append(
                (arc.name, list(meta.get("orphan_source_paths") or [])))
    dups = {k: v for k, v in by_identity.items() if len(v) > 1 and k[1]}
    if dups:
        _out()
        _out(f"--- archives sharing an identity ({len(dups)})")
        for (chain, title), rows in sorted(dups.items()):
            _out(f"    {title!r} chain={chain!r}")
            for folder, sources in rows:
                _out(f"        {folder}  <- {sources}")
        totals["orphan-dup"] = len(dups)

    if remote_root:
        master = remote_root / "index.json"
        if master.exists():
            try:
                games = (json.loads(master.read_text(encoding="utf-8"))
                         or {}).get("games") or {}
            except Exception as exc:                        # noqa: BLE001
                games = {}
                _out(f"\nmaster index unreadable: {exc}")
            local_names = {a.name for a in archives}
            if not args.game:
                missing = sorted(n for n in local_names if n not in games)
                extra = sorted(n for n in games if n not in local_names)
                _out()
                _out("--- master index drift")
                _out(f"    local but not in master : {len(missing)}")
                for n in missing:
                    _out(f"        {n}")
                _out(f"    master but not local    : {len(extra)}")
                for n in extra:
                    _out(f"        {n}")
                totals["index-drift"] = len(missing) + len(extra)

    _out()
    _out("=== summary ===")
    for key in sorted(totals):
        _out(f"  {key:22s} {totals[key]}")
    if totals.get("publish-flag-pending"):
        _out()
        _out("  publish-flag-pending is not damage: it is the index_needs_publish")
        _out("  flag still set from before the flush_orphan_indexes fix. It means")
        _out("  the next sweep will re-publish these archives once, after which it")
        _out("  clears and stays clear.")
    if not args.apply and any(totals.values()):
        _out()
        _out("Nothing was written. Re-run with --apply --fix <kinds> to repair.")


if __name__ == "__main__":
    main()
