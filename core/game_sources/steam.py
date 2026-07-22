"""
SaveSync - Steam store source (appdetails + storesearch).

Extracted verbatim from core/game_api.py. Pure move.
"""
import logging
import urllib.parse
from typing import Optional

from core.game_sources.common import (GameInfo, _build_search_queries,
                                      _fetch_json, _find_best_match)

logger = logging.getLogger(__name__)


def search_steam(game_name: str, appid: Optional[str] = None) -> Optional[GameInfo]:
    """Search Steam Store API.

    Free, no API key required.
    Tries multiple search term variations and picks the best fuzzy match.
    """
    if appid:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
        data = _fetch_json(url)
        if data and str(appid) in data:
            app_data = data[str(appid)].get("data", {})
            if app_data:
                return GameInfo(
                    name=app_data.get("name", ""),
                    description=app_data.get("short_description", ""),
                    image_url=app_data.get("header_image", ""),
                    release_date=app_data.get("release_date", {}).get("date", ""),
                    genres=[g["description"] for g in app_data.get("genres", [])],
                    developer=app_data.get("developers", [""])[0] if app_data.get("developers") else "",
                    publisher=app_data.get("publishers", [""])[0] if app_data.get("publishers") else "",
                    store_url=f"https://store.steampowered.com/app/{appid}/",
                    source="steam",
                )

    # Search by name — build meaningful queries, collect candidates then pick best
    search_terms = _build_search_queries(game_name)
    if not search_terms:
        logger.info(f"Steam: no meaningful search terms from {game_name!r}")
        return None
    all_items: list[dict] = []
    seen_ids: set = set()

    for term in search_terms:
        url = f"https://store.steampowered.com/api/storesearch/?term={urllib.parse.quote(term)}&l=en&cc=US&limit=10"
        logger.info(f"Steam: trying {url}")
        data = _fetch_json(url)

        if data and data.get("items"):
            items = data.get("items", [])
            logger.info(f"Steam found {len(items)} items with term '{term}'")
            for item in items:
                item_id = item.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    all_items.append(item)

    if not all_items:
        logger.info("Steam: no results for any search term")
        return None

    # Pick best match by name
    best = _find_best_match(game_name, all_items, "name")
    if best:
        best_appid = best.get("id")
        if best_appid:
            logger.info(f"Steam best match: {best.get('name')} (appid={best_appid})")
            return search_steam(game_name, str(best_appid))

    return None


