"""
SaveSync - PCGamingWiki source (MediaWiki API + store-link extraction).

Extracted verbatim from core/game_api.py. Pure move.
"""
import logging
import re
from typing import Optional

from core.game_sources.common import (GameInfo, _clean_game_name,
                                      _fetch_json, _fuzzy_score)

logger = logging.getLogger(__name__)


def _pcgw_extract_store_url(html: str) -> str:
    """Extract the first real store URL from a PCGamingWiki page's rendered HTML.

    Parses the Availability table (id=\"table-availability\") and returns the
    href of the first store row that isn't an internal/fandom link.
    """
    m = re.search(r'<table[^>]*id="table-availability"[^>]*>(.*?)</table>', html, re.DOTALL)
    if not m:
        return ""
    rows = re.findall(
        r'<tr[^>]*class="[^"]*table-availability-body-row[^"]*"[^>]*>(.*?)</tr>',
        m.group(1), re.DOTALL,
    )
    for row in rows:
        link = re.search(r'<a[^>]*href="(https?://[^"]+)"', row)
        if link:
            url = link.group(1)
            if 'pcgamingwiki' not in url and 'fandom.com' not in url:
                return url
    return ""


def search_pcgamingwiki(game_name: str) -> Optional[GameInfo]:
    """Search PCGamingWiki via MediaWiki OpenSearch + Cargo API.

    Uses the Cargo API to extract structured data (developer, release date,
    cover image, genres, publisher) from the game infobox.  Falls back to
    OpenSearch for title/page discovery.  No API key needed.
    """
    game_name = _clean_game_name(game_name) or game_name
    _PCGW_API = "https://www.pcgamingwiki.com/w/api.php"
    _PCGW_UA = "SaveSync/1.0 (PCGamingWiki integration; savesync@example.com)"

    import urllib.parse as _up2

    pcgw_headers = {"User-Agent": _PCGW_UA}
    all_hints = [game_name]

    try:
        os_url = (
            f"{_PCGW_API}?action=opensearch"
            f"&search={_up2.quote(game_name)}"
            f"&limit=5&namespace=0&redirects=resolve&format=json"
        )
        os_data = _fetch_json(os_url, headers=pcgw_headers)
        scored: list[tuple[float, str, str]] = []
        if os_data and len(os_data) >= 4:
            titles = os_data[1] if os_data[1] else []
            urls = os_data[3] if os_data[3] else []
            for title, pg_url in zip(titles, urls):
                if not pg_url or "youtube.com" in pg_url:
                    continue
                s = max(_fuzzy_score(h, title) for h in all_hints)
                scored.append((s, title, pg_url))
        scored.sort(key=lambda x: x[0], reverse=True)

        for score, title, pg_url in scored:
            if score < 40.0:
                break
            page_param = _up2.quote(title)
            cargo_url = (
                f"{_PCGW_API}?action=cargoquery"
                f"&tables=Infobox_game"
                f"&fields=Infobox_game._pageName=Page,"
                f"Infobox_game.Developers,"
                f"Infobox_game.Released,"
                f"Infobox_game.Cover_URL,"
                f"Infobox_game.Cover,"
                f"Infobox_game.Publishers,"
                f"Infobox_game.Genres,"
                f"Infobox_game.Steam_AppID,"
                f"Infobox_game.GOGcom_ID"
                f"&where=Infobox_game._pageName=%22{page_param}%22"
                f"&format=json"
            )
            data = _fetch_json(cargo_url, headers=pcgw_headers)
            if not data or "cargoquery" not in data or not data["cargoquery"]:
                continue

            row = data["cargoquery"][0]["title"]
            page_name = row.get("Page") or title

            dev = row.get("Developers") or ""
            if dev:
                if dev.startswith("Company:"):
                    dev = dev[len("Company:"):]
            else:
                pub_raw = row.get("Publishers") or ""
                if pub_raw:
                    parts = [p.strip() for p in pub_raw.split(",")]
                    dev = parts[0].replace("Company:", "", 1)

            genres_raw = row.get("Genres") or ""
            genres = [g.strip() for g in genres_raw.split(",") if g.strip()]

            released = row.get("Released") or ""
            year = ""
            if released:
                first_date = released.split(";")[0].strip()
                m = re.match(r"(\d{4})", first_date)
                if m:
                    year = m.group(1)

            # Resolve cover image: prefer Cover (file name → MD5 path on main domain
            # which is reliably accessible), fall back to Cover_URL (may be an
            # externally-hosted CDN URL, but PCGamingWiki's own CDN blocks hotlinks).
            cover_url = ""
            cover_file = row.get("Cover") or ""
            if cover_file:
                # Reconstruct via MediaWiki MD5 hashing rules — serves from
                # www.pcgamingwiki.com/images/ which works without special headers.
                import hashlib as _hashlib2
                underscore_name = cover_file.replace(" ", "_")
                _h = _hashlib2.md5(underscore_name.encode("utf-8")).hexdigest()
                cover_url = (
                    f"https://www.pcgamingwiki.com/images/"
                    f"{_h[0]}/{_h[:2]}/{underscore_name}"
                )
            else:
                # Fall back to Cover_URL (externally hosted, e.g. Steam CDN)
                cover_url = row.get("Cover URL") or row.get("Cover_URL") or ""
                if cover_url and "images.pcgamingwiki.com" in cover_url:
                    cover_url = cover_url.replace(
                        "https://images.pcgamingwiki.com/",
                        "https://www.pcgamingwiki.com/images/"
                    )

            # Build store URL from Steam/GOG IDs if available
            steam_ids_raw = row.get("Steam AppID") or row.get("Steam_AppID") or ""
            gog_ids_raw = row.get("GOGcom ID") or row.get("GOGcom_ID") or ""
            store_url = ""
            if steam_ids_raw:
                first_sid = steam_ids_raw.split(",")[0].strip()
                if first_sid.isdigit():
                    store_url = f"https://store.steampowered.com/app/{first_sid}/"
            elif gog_ids_raw:
                first_gid = gog_ids_raw.split(",")[0].strip()
                if first_gid.isdigit():
                    store_url = f"https://www.gog.com/en/game/{first_gid}"

            # Fetch description + rendered HTML (for store URLs / official site)
            description = ""
            official_site = ""
            try:
                parse_url = (
                    f"{_PCGW_API}?action=parse"
                    f"&page={_up2.quote(page_name)}"
                    f"&prop=text|wikitext&format=json"
                )
                parse_data = _fetch_json(parse_url, headers=pcgw_headers)
                if parse_data and "parse" in parse_data:
                    page_html = parse_data["parse"]["text"]["*"]
                    m = re.search(r'<div class="introduction">\s*<p>(.*?)</p>', page_html, re.DOTALL)
                    if m:
                        txt = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        txt = re.sub(r'\s+', ' ', txt)
                        description = txt[:500]
                    wt = parse_data["parse"].get("wikitext", {}).get("*", "")
                    m2 = re.search(r'\|official\s*site\s*=\s*(\S+)', wt)
                    if m2:
                        official_site = m2.group(1).strip()
            except Exception:
                pass

            if not store_url:
                store_url = official_site or pg_url
                # Try HTML-extracted store URL (from Availability table) —
                # this handles any store without needing a URL template mapping.
                if store_url == pg_url:
                    try:
                        _page_html = parse_data.get("parse", {}).get("text", {}).get("*", "")
                        if _page_html:
                            _html_store = _pcgw_extract_store_url(_page_html)
                            if _html_store:
                                store_url = _html_store
                    except Exception:
                        pass



            info = GameInfo(
                name=page_name,
                description=description,
                image_url=cover_url,
                release_date=year,
                genres=genres if genres else None,
                developer=dev,
                store_url=store_url,
                source="pcgamingwiki",
            )
            logger.info(f"PCGamingWiki (Cargo): {page_name!r} (score={score:.0f})")
            return info

    except Exception as e:
        logger.debug(f"PCGamingWiki search failed for {game_name!r}: {e}")
    return None


# Field list shared by the name search and the by-id fetch (pasted links).
