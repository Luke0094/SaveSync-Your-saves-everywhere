"""
SaveSync - Game icon/image cache maintenance.

Extracted verbatim from ui/dialogs/add_game_dialog.py: per-game icon cache
folder migration on rename, and the WEBP compression pipeline that keeps
downloaded artwork small (compress on write, one-off compaction of legacy
uncompressed caches). Pure move — no behavior change.
"""
import logging
from pathlib import Path

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


