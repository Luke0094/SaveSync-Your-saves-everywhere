"""
SaveSync - Steam store source (appdetails + storesearch).

Extracted verbatim from core/game_api.py. Pure move.
"""
import logging
import re
import urllib.parse
from typing import Optional

from core.game_sources.common import (GameInfo, _build_search_queries,
                                      _clean_game_name, _fetch_json,
                                      _find_best_match)

logger = logging.getLogger(__name__)

# Kana and CJK ideographs — the scripts whose titles Steam files under its
# Japanese catalogue.
_JAPANESE_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


# The divider Steam publishers use when they put BOTH of a game's names in
# one title — its romanized name, this character, then its original. Only
# this one: a dash or a tilde is an ordinary subtitle, and splitting on those
# would cut real titles in half.
_TITLE_DIVIDERS = ("|", "｜")


def _preferred_title(title: str) -> str:
    """The Latin half of a title that carries both of a game's names.

    Returned unchanged when the title is not of that shape — one name, or two
    written in the same script. The full original is kept as an alternative
    name by the caller, so nothing is lost for matching.
    """
    for sep in _TITLE_DIVIDERS:
        if sep not in title:
            continue
        parts = [p.strip() for p in title.split(sep) if p.strip()]
        latin = [p for p in parts if not _JAPANESE_RE.search(p)]
        # Only when the two halves genuinely differ in script: a title split
        # into two Latin halves is one name with a divider in it.
        if latin and len(latin) < len(parts):
            return latin[0]
    return title


def _appdetails(appid: str, prefer_region: str = "us") -> dict:
    """One game's store details, under its international name where it has one.

    Two independent choices, and only one of them decides the name:

    - The LANGUAGE is always English, and that is what picks the name. Asking
      the Japanese store in English still answers with a game's international
      title wherever it has one; a game sold only in Japan answers with its
      Japanese title, because that is the only name it has.
    - The STORE is asked where the game was actually FOUND. A title sold only
      in Japan is absent from the US catalogue and comes back as a plain
      failure, so asking there first is a request that can only fail for
      exactly the games that needed the other one. The other store is still
      tried afterwards, for an appid that arrived without a catalogue behind
      it — a launcher, or the library.
    """
    regions = [prefer_region] + [r for r in ("us", "jp") if r != prefer_region]
    for region in regions:
        url = ("https://store.steampowered.com/api/appdetails"
               f"?appids={appid}&cc={region}&l=english")
        data = _fetch_json(url) or {}
        entry = data.get(str(appid)) or {}
        if entry.get("success") and entry.get("data"):
            return entry["data"]
    return {}


def _storesearch_urls(term: str) -> list[tuple[str, str]]:
    """The (region, url) store searches to try for one search term.

    Steam's store search is answered per LANGUAGE, not per title: a game
    released only under a Japanese name is absent from the English catalogue
    and the search returns nothing at all, while the same term against the
    Japanese one returns it as the first hit. Asking only in English is
    therefore not a preference, it is a game that cannot be found — so a term
    carrying Japanese characters is asked for in both.
    """
    quoted = urllib.parse.quote(term)
    base = "https://store.steampowered.com/api/storesearch/?term="
    urls = [("us", f"{base}{quoted}&l=en&cc=US&limit=10")]
    if _JAPANESE_RE.search(term):
        urls.append(("jp", f"{base}{quoted}&l=japanese&cc=JP&limit=10"))
    return urls


def search_steam(game_name: str, appid: Optional[str] = None,
                 region: str = "us") -> Optional[GameInfo]:
    """Search Steam Store API.

    Free, no API key required.
    Tries multiple search term variations and picks the best fuzzy match.

    *region* is the store to ask for the details of a known *appid* — the one
    the game was found in, when the caller knows. It never changes the name's
    language, only which catalogue is asked first; see _appdetails.
    """
    if appid:
        app_data = _appdetails(str(appid), region)
        if app_data:
            full_name = app_data.get("name", "")
            shown = _preferred_title(full_name)
            return GameInfo(
                name=shown,
                description=app_data.get("short_description", ""),
                image_url=app_data.get("header_image", ""),
                release_date=app_data.get("release_date", {}).get("date", ""),
                genres=[g["description"] for g in app_data.get("genres", [])],
                developer=app_data.get("developers", [""])[0] if app_data.get("developers") else "",
                publisher=app_data.get("publishers", [""])[0] if app_data.get("publishers") else "",
                store_url=f"https://store.steampowered.com/app/{appid}/",
                source="steam",
                # The title as Steam writes it, kept whenever it is not what
                # is shown: it holds the game's other name, and that is what
                # a search written in that name has to match against.
                alt_names=[full_name] if full_name != shown else [],
            )

    # Search by name — build meaningful queries, collect candidates then pick best
    search_terms = _build_search_queries(game_name)
    if not search_terms:
        logger.info(f"Steam: no meaningful search terms from {game_name!r}")
        return None
    all_items: list[dict] = []
    seen_ids: set = set()

    # Which catalogue each candidate came out of, so its details can be asked
    # for where it is actually sold — see _appdetails.
    item_region: dict = {}

    for term in search_terms:
        for region, url in _storesearch_urls(term):
            logger.info(f"Steam: trying {url}")
            data = _fetch_json(url)

            if data and data.get("items"):
                items = data.get("items", [])
                logger.info(f"Steam found {len(items)} items with term '{term}'")
                for item in items:
                    item_id = item.get("id")
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        item_region[item_id] = region
                        all_items.append(item)

    if not all_items:
        logger.info("Steam: no results for any search term")
        return None

    # Pick best match by name
    # Judged against the name that was SEARCHED for, not the raw one. The
    # queries above are built from the cleaned name, so the raw one may still
    # carry a release marker or a product code — words that cannot appear in
    # any store title, and which the scorer treats as words the result is
    # missing. Requiring them is requiring a title nobody publishes.
    scored_as = _clean_game_name(game_name) or game_name
    best = _find_best_match(scored_as, all_items, "name")
    if best:
        best_appid = best.get("id")
        if best_appid:
            logger.info(f"Steam best match: {best.get('name')} (appid={best_appid})")
            info = search_steam(game_name, str(best_appid),
                                region=item_region.get(best_appid, "us"))
            # The name the game was FOUND under, kept alongside the one the
            # store details return. Those differ whenever the search matched
            # a localised catalogue — a Japanese title is found under it and
            # then described in English — and without carrying it over, the
            # caller has no way to see that this entry is the one asked for.
            matched = best.get("name") or ""
            known = [info.name] + list(info.alt_names or []) if info else []
            if info and matched and matched not in known:
                info.alt_names = list(info.alt_names or []) + [matched]
            return info

    return None


