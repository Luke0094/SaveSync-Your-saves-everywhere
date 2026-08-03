"""
SaveSync - VNDB source (Kana API): search, by-id fetch, entry parsing.

Extracted verbatim from core/game_api.py. Pure move.
"""
import json
import logging
import re
import urllib.request
from typing import Optional

from core.game_sources.common import (GameInfo, _clean_game_name,
                                      _fuzzy_score)
from core.net import open_url as _open_url

logger = logging.getLogger(__name__)


_VNDB_FIELDS = (
    "id, title, alttitle, "
    "titles{lang,title,latin,official,main}, "
    "image{url,thumbnail,sexual,violence}, "
    "description, "
    "tags{id,name,rating,spoiler}, "
    "developers{name}, "
    "released, platforms, languages, "
    "extlinks{url,label}"
)


def _title_variants(entry: dict) -> list[str]:
    """Every title one VNDB entry is known by, in every script it carries.

    A Japanese game is listed under its original title, its romanization and
    often an English release title, and a search may legitimately match any
    of them. Both the search and the result use this, so the titles a match
    was FOUND on are the same ones the caller gets to see it by.
    """
    out: list[str] = [entry.get("title", ""), entry.get("alttitle", "")]
    for t in entry.get("titles", []) or []:
        out.append(t.get("title", ""))
        out.append(t.get("latin", ""))
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def search_vndb(game_name: str) -> Optional[GameInfo]:
    """Search VNDB Kana API v2.

    Ref: https://api.vndb.org/kana
    Free, no API key. Searches titles, aliases and release titles.

    Fields requested per the API docs:
    - title / alttitle: main display title + romanized alternative
    - titles{lang,title,latin}: full multi-language title list
    - image.url / image.thumbnail: cover image
    - description: freetext description (may contain formatting codes)
    - tags{id,name,rating,spoiler}: directly applied tags (genres etc.)
    - developers{name}: developer list
    """
    game_name = _clean_game_name(game_name) or game_name
    url = "https://api.vndb.org/kana/vn"
    query = {
        "filters": ["search", "=", game_name],
        "fields": _VNDB_FIELDS,
        "results": 15,
        "sort": "searchrank",
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(query).encode("utf-8"),
            headers={"User-Agent": "SaveSync/1.0", "Content-Type": "application/json"},
            method="POST",
        )
        with _open_url(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            logger.debug(f"VNDB: no results for {game_name!r}")
            return None

        best_entry = None
        best_score = -1.0
        for entry in results:
            score = max(_fuzzy_score(game_name, t)
                        for t in _title_variants(entry))
            # Strictly greater, so equal scores keep the EARLIER entry — and
            # that is load-bearing rather than incidental. A query written in
            # a script the scorer will not judge (see common._fuzzy_score)
            # leaves every candidate on zero, and the first of them is VNDB's
            # own top hit, which measured better than anything computed here.
            # Relaxing this to >= would silently hand every such query to the
            # LAST result instead.
            if score > best_score:
                best_score = score
                best_entry = entry

        if not best_entry:
            return None

        logger.info(f"VNDB best: {best_entry.get('title')!r} score={best_score:.0f}")
        return _parse_vndb_entry(best_entry)

    except Exception as e:
        logger.debug(f"VNDB search failed for {game_name!r}: {e}")
    return None


def _parse_vndb_entry(entry: dict) -> GameInfo:
    """Build a GameInfo from one Kana-API vn entry (shared by the name
    search above and the pasted-link fetch below)."""
    # Choose best display title: prefer romanized/latin over kanji
    display_title = entry.get("title", "")
    for t in entry.get("titles", []):
        if t.get("main") and t.get("latin"):
            display_title = t["latin"]
            break
    if not display_title:
        display_title = entry.get("alttitle", "") or entry.get("title", "")

    # Image — prefer the FULL-size url: the ~256px thumbnail looks grainy
    # everywhere it's shown upscaled (library card 186×240, image modal).
    # The saved copy is re-encoded/clamped locally, so file size stays low.
    img_obj = entry.get("image") or {}
    image_url = img_obj.get("url") or img_obj.get("thumbnail") or ""

    # Description — strip VNDB markup codes like [spoiler]...[/spoiler]
    raw_desc = entry.get("description") or ""
    clean_desc = re.sub(r'\[/?[a-z]+[^\]]*\]', '', raw_desc).strip()[:800]

    # Tags — only include non-spoiler tags with rating >= 1.5
    genres: list[str] = []
    for tag in entry.get("tags", []):
        if (tag.get("spoiler", 0) == 0
                and tag.get("rating", 0) >= 1.5
                and tag.get("name")):
            genres.append(tag["name"])
    genres = genres[:16]

    # Developer
    devs = entry.get("developers", [])
    developer = devs[0].get("name", "") if devs else ""

    # Release date — VNDB returns "YYYY-MM-DD" or partial ("YYYY", "YYYY-MM")
    release_date = entry.get("released") or ""

    # Store URL — prefer Steam link, then any first extlink
    store_url = ""
    extlinks = entry.get("extlinks") or []
    for link in extlinks:
        u = link.get("url", "")
        lbl = (link.get("label") or "").lower()
        if "steam" in lbl or "steampowered.com" in u:
            store_url = u
            break
    if not store_url and extlinks:
        store_url = extlinks[0].get("url", "")

    # The VNDB entry page itself is always offered as a site link, so
    # the game keeps a reference to its VNDB page alongside any store.
    vndb_url = f"https://vndb.org/{entry['id']}" if entry.get("id") else ""
    extra_urls: list[str] = []
    if vndb_url:
        if not store_url:
            store_url = vndb_url
        elif vndb_url != store_url:
            extra_urls.append(vndb_url)

    # Every other name this game is known by. The search above already looks
    # at all of them; carrying them on the result is what lets the caller see
    # WHY this entry was chosen, instead of judging it on a display title the
    # query never mentioned. See GameInfo.alt_names.
    alt_names = [n for n in _title_variants(entry) if n != display_title]

    return GameInfo(
        name=display_title,
        description=clean_desc,
        image_url=image_url,
        genres=genres,
        developer=developer,
        release_date=release_date,
        store_url=store_url,
        source="vndb",
        extra_urls=extra_urls,
        alt_names=alt_names,
    )


def fetch_vndb_by_id(vn_id: str) -> Optional[GameInfo]:
    """Fetch one VNDB entry by its id ("v123") via the Kana API — the
    pasted-link counterpart of search_vndb, same fields and parsing."""
    query = {"filters": ["id", "=", vn_id], "fields": _VNDB_FIELDS, "results": 1}
    try:
        req = urllib.request.Request(
            "https://api.vndb.org/kana/vn",
            data=json.dumps(query).encode("utf-8"),
            headers={"User-Agent": "SaveSync/1.0", "Content-Type": "application/json"},
            method="POST",
        )
        with _open_url(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        results = data.get("results", [])
        if results:
            return _parse_vndb_entry(results[0])
    except Exception as e:
        logger.debug(f"VNDB by-id fetch failed for {vn_id!r}: {e}")
    return None


