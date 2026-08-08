"""
SaveSync - Apply a web-search result to a library entry, headlessly.

The Add/Edit dialog enriches one game with the user watching every field. This
does the same job unattended, for the batch search that follows a folder scan:
no widgets, no prompts, and — deliberately — no overwriting. Only fields the
entry does not already have are filled, so an auto-accepted result can never
replace something the user typed.
"""
import logging
import re
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_MIN_IMAGE_BYTES = 512


def download_cover(url: str, game_name: str, entry_id: str = "",
                   computed_folder_name: Optional[str] = None,
                   exe_path: str = "", timeout: int = 20) -> str:
    """Fetch *url* into the icon cache. Returns the local path, or "" on any
    failure — a missing cover is never a reason to fail an enrichment."""
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return ""
    try:
        from core.net import open_url
        from core.constants import get_install_folder_name
        from ui.image_cache import _ICON_CACHE_DIR, _compress_image_to_cache

        request = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "/".join(url.split("/")[:3]) + "/",
        })
        with open_url(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if content_type and not content_type.startswith("image/") \
                    and "octet-stream" not in content_type:
                logger.debug(f"Cover skipped, Content-Type {content_type!r}")
                return ""
            data = response.read()
        if len(data) < _MIN_IMAGE_BYTES:
            logger.debug(f"Cover too small ({len(data)}B), skipped")
            return ""

        folder = get_install_folder_name(exe_path or "", game_name, entry_id,
                                         computed_folder_name)
        dest_dir = Path(_ICON_CACHE_DIR) / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w\-. ]", "_", Path(url.split("?")[0]).stem)[:40] or "cover"
        saved = _compress_image_to_cache(data, dest_dir / f"{stem}.jpg")
        return str(saved) if saved else ""
    except Exception as e:
        logger.debug(f"Cover download failed for {url!r}: {e}")
        return ""


def apply_game_info(entry, info, fetch_cover: bool = True) -> list:
    """Fill in *entry* from *info*, touching only fields that are still empty.

    Returns the names of the fields that changed, so the caller can tell a
    genuinely useful result from one that added nothing. The entry is NOT
    saved here — the caller decides when to write.
    """
    changed: list = []
    if entry is None or info is None:
        return changed

    def _fill(attr: str, value):
        if not value:
            return
        current = getattr(entry, attr, "")
        if current:
            return
        setattr(entry, attr, value)
        changed.append(attr)

    _fill("description", (info.description or "").strip())
    _fill("developer", (info.developer or "").strip())
    _fill("store_url", (info.store_url or "").strip())
    _fill("info_source", (info.source or "").strip())

    year = _year_of(getattr(info, "release_date", ""))
    if year:
        _fill("release_year", year)

    if not getattr(entry, "tags", None):
        genres = [g.strip() for g in (getattr(info, "genres", None) or []) if g and g.strip()]
        if genres:
            entry.tags = genres[:6]
            changed.append("tags")

    # What the source thought of the game — one verdict (Steam/VNDB) or many
    # user reviews (DLsite). Keyed by review_identity so re-running updates
    # each entry instead of stacking duplicates, and the user's own reviews
    # (source "user") are never touched.
    from core.library import review_identity
    if hasattr(info, "as_reviews"):
        web_reviews = info.as_reviews()
    elif hasattr(info, "as_review"):
        one = info.as_review()
        web_reviews = [one] if one else []
    else:
        web_reviews = []
    if web_reviews:
        reviews = list(getattr(entry, "reviews", None) or [])
        by_key = {review_identity(r): i
                  for i, r in enumerate(reviews) if isinstance(r, dict)}
        touched = False
        for web_review in web_reviews:
            if (web_review.get("source") or "") == "user":
                continue
            key = review_identity(web_review)
            if not key:
                continue
            idx = by_key.get(key)
            if idx is not None:
                if reviews[idx] != web_review:
                    reviews[idx] = web_review
                    touched = True
            else:
                by_key[key] = len(reviews)
                reviews.append(web_review)
                touched = True
        if touched:
            entry.reviews = reviews
            changed.append("reviews")

    if fetch_cover and not getattr(entry, "icon_path", ""):
        local = download_cover(
            getattr(info, "image_url", ""), entry.name, entry.id,
            getattr(entry, "computed_folder_name", None), getattr(entry, "exe_path", ""))
        if local:
            entry.icon_path = local
            changed.append("icon_path")

    return changed


def _year_of(release_date: str) -> str:
    """First 4-digit year in a release date, whatever format it arrived in."""
    match = re.search(r"(19|20)\d{2}", str(release_date or ""))
    return match.group(0) if match else ""
