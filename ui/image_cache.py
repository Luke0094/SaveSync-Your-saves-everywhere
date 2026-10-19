"""
SaveSync - Game icon/image cache maintenance.

Extracted verbatim from ui/dialogs/add_game_dialog.py: per-game icon cache
folder migration on rename, and the WEBP compression pipeline that keeps
downloaded artwork small (compress on write, one-off compaction of legacy
uncompressed caches). Pure move — no behavior change.

Also what a picture downloaded for a game becomes on disk (store_picture): no
GUI in it, so the dialog's background job and the one that outlives a Save can
both run it on a worker thread.
"""
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from core.constants import USER_DATA_DIR

logger = logging.getLogger(__name__)

_ICON_CACHE_DIR = USER_DATA_DIR / "icons"


def _file_md5(path: "Path") -> "str | None":
    """MD5 of a file's bytes, or None if it can't be read."""
    import hashlib
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def migrate_icon_cache(old_folder: str, new_folder: str,
                       current_image_path: "str | None",
                       cache_root: "Path | None" = None) -> "str | None":
    """Migrate a game's icon cache folder after a rename and return the
    (possibly remapped) current image path.

    Cases handled — the icon must survive ALL of them:
    - new folder absent  → plain rename of the old folder;
    - new folder present → MERGE old files into it (never delete the old
      folder wholesale: the currently selected icon may still live there,
      e.g. when web enrichment renamed the game but brought no image —
      deleting it is exactly how previously saved icons got lost);
    - same-name collision → the existing destination file wins and the
      current path is remapped onto it;
    - image outside the cache (install-dir art) → untouched;
    - old folder absent → no-op.
    """
    import shutil as _shutil
    root = cache_root or _ICON_CACHE_DIR
    result_path = current_image_path
    if not old_folder or not new_folder or old_folder == new_folder:
        return result_path
    old_cache = root / old_folder
    new_cache = root / new_folder
    if not old_cache.exists():
        return result_path

    def _remap(p: "str | None") -> "str | None":
        if not p:
            return p
        try:
            rel = Path(p).relative_to(old_cache)
        except ValueError:
            return p          # outside the old cache → leave untouched
        return str(new_cache / rel)

    try:
        if not new_cache.exists():
            old_cache.rename(new_cache)
            logger.info(f"Renamed icon cache: {old_folder!r} → {new_folder!r}")
            return _remap(result_path)

        # Merge every file into new_cache — a same-NAME collision is never
        # assumed to be the same IMAGE (two unrelated downloads can easily
        # land on the same safe-name/stem). Hash-compare like
        # _compress_existing_file does: identical bytes → the destination
        # already has it, drop the source; different bytes → keep BOTH,
        # renaming the incoming one. This guarantees old_cache is fully
        # drained before it's ever removed below — the previous version
        # moved only non-colliding files and then rmtree'd whatever
        # collisions were left behind "because the current icon survived",
        # silently destroying any OTHER image (not the one tracked path)
        # that happened to share a filename. That is exactly how a rename
        # could quietly drop every cover except the current one.
        for f in list(old_cache.iterdir()):
            if not f.is_file():
                continue
            dest = new_cache / f.name
            if not dest.exists():
                try:
                    _shutil.move(str(f), str(dest))
                except OSError as e:
                    logger.warning(f"Icon merge failed for {f.name}: {e}")
                continue
            try:
                same = (dest.stat().st_size == f.stat().st_size
                        and _file_md5(dest) == _file_md5(f))
            except OSError:
                same = False
            if same:
                try:
                    f.unlink()
                except OSError:
                    pass
            else:
                import uuid
                alt = new_cache / f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                try:
                    _shutil.move(str(f), str(alt))
                except OSError as e:
                    logger.warning(f"Icon merge collision move failed for {f.name}: {e}")
        result_path = _remap(result_path)
        try:
            if not any(old_cache.iterdir()):
                old_cache.rmdir()
            # Anything still left is a file that failed to move/unlink above
            # (permissions, in use, …) — leave the folder rather than risk
            # deleting content that was never accounted for.
        except OSError:
            pass
        logger.info(f"Merged icon cache {old_folder!r} into {new_folder!r}")
    except OSError as e:
        logger.warning(f"Could not migrate icon cache folder: {e}")
    # Never return a dangling path: fall back to any surviving cached image
    if result_path and not Path(result_path).exists():
        try:
            for cand in sorted(new_cache.iterdir()):
                if cand.is_file():
                    return str(cand)
        except OSError:
            pass
        return current_image_path if current_image_path and Path(current_image_path).exists() else None
    return result_path

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico", ".avif"}

_COMPRESS_QUALITY = 80  # JPEG quality for cached images


def _compress_image_to_cache(image_data: bytes, dest_path: Path) -> Path:
    """Save image data to *dest_path* as a compressed JPEG (80% quality).

    Converts any supported format (PNG, BMP, WebP, AVIF, etc.) to JPEG —
    on success the returned path has a ``.jpg`` extension. If the image
    has an alpha channel it is composited onto a white background before
    saving.

    Decode order: PIL (with the AVIF plugin when available), then Qt as a
    second decoder (its system/plugin codecs can read formats this PIL
    build can't). Only when BOTH fail are the raw bytes written — and then
    to *dest_path* AS GIVEN, so callers must pass a path with the SOURCE's
    real extension: a failed conversion must never label undecodable bytes
    ``.jpg`` (that both corrupts the cache entry and, in the compaction
    path, used to get the original deleted as "converted")."""
    jpg_path = dest_path.with_suffix(".jpg")
    try:
        from PIL import Image
        import io
        try:
            import pillow_avif  # noqa: F401 — registers the AVIF codec in PIL
        except ImportError:
            pass
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.convert("RGBA").split()[3])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(str(jpg_path), "JPEG", quality=_COMPRESS_QUALITY, optimize=True)
        return jpg_path
    except Exception as exc:
        logger.debug(f"PIL could not convert image ({exc}) — trying Qt")
    # Second decoder: Qt (QImage) — reads AVIF via qtimageformats/system codecs
    try:
        from PySide6.QtGui import QImage
        qimg = QImage.fromData(image_data)
        if not qimg.isNull() and qimg.save(str(jpg_path), "JPG", _COMPRESS_QUALITY):
            return jpg_path
    except Exception as exc:
        logger.debug(f"Qt could not convert image either: {exc}")
    logger.warning(
        f"Image conversion failed — keeping original bytes as {dest_path.name}")
    dest_path.write_bytes(image_data)
    return dest_path


def _compress_existing_file(src_path: Path, dest_dir: Path) -> Path:
    """Compress an existing image file into *dest_dir* as JPEG 80%.

    If the source is already a JPEG under 500 KB it is copied as-is
    (or returned directly when already inside *dest_dir*).
    Non-JPEG originals inside *dest_dir* are deleted after successful
    conversion to avoid leaving duplicates.
    """
    data = src_path.read_bytes()
    is_inside_dest = False
    try:
        is_inside_dest = src_path.resolve().parent == dest_dir.resolve()
    except (OSError, ValueError):
        pass

    # Small JPEGs are fine as-is
    if src_path.suffix.lower() in (".jpg", ".jpeg") and len(data) < 500_000:
        if is_inside_dest:
            return src_path  # already in place, no copy needed
        dest = dest_dir / src_path.name
        dest.write_bytes(data)
        return dest

    dest = dest_dir / (src_path.stem + ".jpg")
    # Avoid overwriting a different image that already has the .jpg name
    if dest.exists() and dest != src_path:
        import hashlib
        existing_hash = hashlib.md5(dest.read_bytes()).hexdigest()
        new_hash = hashlib.md5(data).hexdigest()
        if existing_hash != new_hash:
            # Different image — keep both, suffix the new one
            import uuid
            dest = dest_dir / (src_path.stem + f"_{uuid.uuid4().hex[:6]}.jpg")
        else:
            # Same content — just remove the duplicate original
            if is_inside_dest and src_path.exists() and src_path != dest:
                try:
                    src_path.unlink()
                except OSError:
                    pass
            return dest
    # Pass the target with the SOURCE's extension: on success the converter
    # writes <stem>.jpg anyway, while on decode failure the raw bytes keep
    # their true extension (an undecodable .avif must stay .avif — never be
    # mislabelled .jpg) and, in-place, result == src so nothing is deleted.
    result = _compress_image_to_cache(data, dest.with_suffix(src_path.suffix))
    # Remove the original only when conversion actually produced a NEW file
    # in the cache dir (never after a failed conversion left it in place).
    if is_inside_dest and result != src_path and result.exists() and src_path.exists():
        try:
            src_path.unlink()
        except OSError:
            pass
    return result


# Maps cache dir path → directory mtime at last successful scan.
# If the dir mtime hasn't changed, all files are already compressed
# and the scan is skipped.  When the user (or a download) adds a new
# file the OS bumps the directory mtime, so the next call rescans.
_compressed_dir_mtime: dict[str, float] = {}


def _ensure_cache_compressed(cache_dir: Path) -> None:
    """Compress all uncompressed images already present in *cache_dir*.

    Converts PNG, BMP, WebP, etc. to JPEG 80% and removes the originals.
    Already-compressed JPEGs (< 500 KB) are left untouched.

    Skips entirely when the directory's mtime hasn't changed since the
    last scan — a single stat() call instead of iterating every file.
    When the user drops new files into the folder (or a web download
    lands there), the OS updates the directory mtime and the next call
    will pick them up.
    """
    if not cache_dir.exists():
        return
    dir_key = str(cache_dir)
    try:
        current_mtime = cache_dir.stat().st_mtime
    except OSError:
        return
    if _compressed_dir_mtime.get(dir_key) == current_mtime:
        return  # nothing changed since last scan

    needed_work = False
    for f in list(cache_dir.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _IMG_EXTS:
            continue
        # Small JPEGs are already fine
        if f.suffix.lower() in (".jpg", ".jpeg"):
            try:
                if f.stat().st_size < 500_000:
                    continue
            except OSError:
                continue
        # Found a file that needs compression
        needed_work = True
        try:
            _compress_existing_file(f, cache_dir)
        except Exception as exc:
            logger.debug(f"Cache compression skipped for {f.name}: {exc}")

    # Record the mtime *after* compression.  Compression itself changes
    # the dir mtime (new .jpg created, old file deleted), so re-stat.
    try:
        _compressed_dir_mtime[dir_key] = cache_dir.stat().st_mtime
    except OSError:
        _compressed_dir_mtime.pop(dir_key, None)
    if needed_work:
        logger.info(f"Compressed cached images in {cache_dir.name}")


# ── Pictures downloaded for a game ──────────────────────────────────────────
#
# What a picture fetched from the web (a cover, a screenshot) ends up as on disk:
# one JPEG in the game's cache folder. Nothing here touches the GUI, so a worker
# thread can run it — the re-encode is the slow part, and it used to hold the
# panel still for as long as it took.

PICTURE_MAX_EDGE = 1280        # px, long edge; larger sources are shrunk to this
_PICTURE_MIN_BYTES = 512       # below this it is an error page or a pixel, not art
_store_lock = threading.Lock()
# The name a picture is written under until it is complete (and its Qt probe, which
# is that name + ".avif"): hidden from every folder scan, found again by the sweep.
_PART_SUFFIXES = (".part", ".part.avif")
# No writer of this process can predate it: a temp file older than this run is the
# leftover of one that died.
_PROCESS_START = time.time()


def picture_stem(url: str) -> str:
    """The file name (without extension) a picture from *url* is saved under."""
    original_name = Path(url.split("?")[0]).stem
    return "".join(c for c in original_name if c.isalnum() or c in "._- ")[:50]


def _encode_picture(data: bytes, dest: Path) -> bool:
    """Write *data* to *dest* as a JPEG, at most PICTURE_MAX_EDGE on the long edge.

    Quality 88 with 4:4:4 chroma (the old 80 with 4:2:0 visibly grained flat-colour
    art at card and modal sizes, for a negligible size difference). PIL first, with
    the AVIF plugin when it is there — attachment CDNs serve AVIF under a .png/.jpg
    URL — then Qt, whose system codecs read formats this PIL build cannot. False
    when neither can decode it."""
    try:
        # Imported inside the try on purpose: a frozen build missing PIL._imaging
        # raises ImportError here, and must fall through to Qt, not abort.
        from PIL import Image
        import io
        try:
            import pillow_avif  # noqa: F401 — registers the AVIF codec in PIL
        except ImportError:
            pass
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img.thumbnail((PICTURE_MAX_EDGE, PICTURE_MAX_EDGE), resample)
        img.save(str(dest), "JPEG", quality=88, optimize=True, subsampling=0)
        return True
    except Exception as exc:
        logger.info(f"PIL could not encode the picture ({exc}) — trying Qt")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        qimg = QImage.fromData(data)
        if qimg.isNull():
            # Some system codecs (AVIF on Windows) only read from a file.
            probe = dest.parent / f"{dest.name}.avif"
            try:
                probe.write_bytes(data)
                qimg = QImage(str(probe))
            finally:
                probe.unlink(missing_ok=True)
        if qimg.isNull():
            return False
        if max(qimg.width(), qimg.height()) > PICTURE_MAX_EDGE:
            qimg = qimg.scaled(PICTURE_MAX_EDGE, PICTURE_MAX_EDGE,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        return bool(qimg.save(str(dest), "JPEG", 88))
    except Exception as exc:
        logger.info(f"Qt could not encode the picture either: {exc}")
        return False


def _identical_file(candidate: Path, folder: Path,
                    extra: "Iterable[str | Path]" = ()) -> "Path | None":
    """A file in *folder* (or among *extra*) with exactly the bytes of *candidate*.

    Compared by size first, so only the files that could match are read."""
    try:
        size = candidate.stat().st_size
    except OSError:
        return None
    digest = _file_md5(candidate)
    if digest is None:
        return None
    others: list[Path] = []
    try:
        others = [p for p in folder.iterdir()
                  if p.is_file() and not p.name.startswith(".")]
    except OSError:
        pass
    others += [Path(p) for p in extra]
    seen: set[Path] = set()
    for other in others:
        if other == candidate or other in seen:
            continue
        seen.add(other)
        try:
            if other.stat().st_size == size and _file_md5(other) == digest:
                return other
        except OSError:
            continue
    return None


def store_picture(url: str, data: bytes, dest_dir: Path,
                  same_as: "Iterable[str | Path]" = ()) -> "tuple[Path, bool] | None":
    """Re-encode *data* (the picture at *url*) into *dest_dir* as a JPEG.

    Returns ``(path, created)``: where the picture now is, and whether this call
    wrote that file. A picture identical to one *dest_dir* already holds, or
    that is among *same_as* (the files already in the carousel), is not written
    twice — that file is returned, ``created`` False. None when *data* is not a
    picture any decoder here can read. Safe on a worker thread."""
    if len(data) < _PICTURE_MIN_BYTES:
        logger.warning(f"Downloaded picture too small ({len(data)} bytes): {url!r}")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = picture_stem(url) or "picture"
    # Written under a name the folder scans ignore and renamed into place, so a
    # reader never meets a half-written picture and two threads never share a name.
    part = dest_dir / f".{uuid.uuid4().hex}{_PART_SUFFIXES[0]}"
    try:
        if not _encode_picture(data, part):
            logger.warning(f"No decoder here can read the picture: {url!r}")
            return None
        with _store_lock:
            same = _identical_file(part, dest_dir, same_as)
            if same is not None:
                return same, False
            final = dest_dir / f"{stem}.jpg"
            if final.exists():
                # A different picture under the same name: both are kept.
                final = dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}.jpg"
            os.replace(part, final)
            return final, True
    finally:
        part.unlink(missing_ok=True)


def sweep_picture_leftovers(cache_root: "Path | None" = None,
                            min_age: float = 600.0) -> "tuple[int, int]":
    """Delete the temporary files store_picture leaves behind when the process is
    killed, or crashes, in the middle of one. Returns ``(removed, could not remove)``.

    Only what no writer can still own: older than this run, or untouched for
    *min_age* seconds (an encode takes seconds, never minutes). A picture being
    written right now is left alone."""
    root = cache_root or _ICON_CACHE_DIR
    now = time.time()
    removed = failed = 0
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return 0, 0                      # no cache yet: nothing to sweep
    for folder in folders:
        try:
            files = [f for f in folder.iterdir()
                     if f.name.startswith(".") and f.name.endswith(_PART_SUFFIXES)]
        except OSError:
            continue
        for f in files:
            try:
                if not f.is_file():
                    continue
                mtime = f.stat().st_mtime
                if mtime >= _PROCESS_START and now - mtime < min_age:
                    continue
                f.unlink()
                removed += 1
            except FileNotFoundError:
                continue                 # gone meanwhile: the writer finished
            except OSError as exc:
                failed += 1
                logger.warning(f"Could not remove leftover {f.name} in {folder.name}: {exc}")
    if removed:
        logger.info(f"Removed {removed} leftover temporary picture file(s) from the icon cache")
    return removed, failed


def move_picture_into(path: "str | Path", folder: Path) -> Path:
    """Move a picture into *folder* unless it is already there; a copy that
    *folder* holds already is used instead, and this one dropped. Returns where
    the picture is now. The folder it leaves is removed if that empties it."""
    import shutil
    src = Path(path)
    if src.parent == folder:
        return src
    folder.mkdir(parents=True, exist_ok=True)
    with _store_lock:
        same = _identical_file(src, folder)
        if same is not None:
            src.unlink(missing_ok=True)
            dest = same
        else:
            dest = folder / src.name
            if dest.exists():
                dest = folder / f"{src.stem}_{uuid.uuid4().hex[:8]}{src.suffix}"
            shutil.move(str(src), str(dest))
    try:
        src.parent.rmdir()
    except OSError:
        pass            # not empty (or already gone): nothing to tidy
    return dest


def discard_picture(path: "str | Path") -> None:
    """Delete a picture this process wrote and no one holds a reference to, and
    the folder it was in if that leaves it empty."""
    p = Path(path)
    try:
        p.unlink(missing_ok=True)
        p.parent.rmdir()
    except OSError:
        pass            # in use, or the folder holds more: leave it be


