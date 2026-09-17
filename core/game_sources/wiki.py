"""
SaveSync - PCGamingWiki source (MediaWiki API + infobox/store-link extraction).

Originally extracted verbatim from core/game_api.py, then rewritten: the
structured data used to come from the Cargo API (action=cargoquery against
the Infobox_game table), which PCGamingWiki has since locked down —
confirmed live against the real API: every request, even a minimal
one-field query with a normal User-Agent, now comes back
{"error":{"code":"permissiondenied", ...}}. That made this source silently
useless (data["cargoquery"] never present -> every candidate `continue`s ->
the function returns None for every search) despite being tried on EVERY
lookup as one of the three "primary APIs" — indistinguishable from "no
PCGamingWiki page exists for this game" since nothing raised or logged
about it.

The replacement reads the same information straight out of the page's own
rendered infobox HTML (action=parse, which is unaffected — it's a normal
page render, not a Cargo query), the same class of extraction
webscrape.py already relies on for sites with no clean API. Structure
confirmed against a real page (Portal 2): a <table id="infobox-game">
whose rows come in section blocks - a <th class="template-infobox-header"
colspan="2"> label (Developers / Publishers / Engines / Release dates /
Reception / Taxonomy / ...), followed by one or more <tr> pairs of
<td class="template-infobox-type"> (a sub-label, often blank) and
<td class="template-infobox-info"> (the value, usually one or more <a>
links) until the next header.
"""
import logging
import re
from typing import Optional

from core.game_sources.common import (GameInfo, _clean_game_name,
                                      _decode_entities, _fetch_json,
                                      _fuzzy_score)

logger = logging.getLogger(__name__)


def _pcgw_extract_store_url(html: str) -> str:
    """Extract the first real store URL from a PCGamingWiki page's rendered HTML.

    Parses the Availability table (id="table-availability") and returns the
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


def _pcgw_extract_infobox(html: str) -> dict:
    """The page's own infobox (id="infobox-game"), as {section_label:
    [(sub_label, value), ...]} — see this module's docstring for the
    confirmed row shape. *value* is the joined, deduplicated anchor text
    of every info cell's plain text, footnote markers stripped first. A
    genre/theme/etc. cell's several <a> links are already literally
    comma-separated in the source HTML (confirmed live: `<a ...>Platform
    </a>, <a ...>Puzzle</a>`), so plain-text extraction alone reads them
    correctly without reconstructing a join — the same simple extraction
    also has to work for a release-date cell, which is NOT a link at all,
    just text immediately followed by a MediaWiki footnote reference
    (`<sup class="reference"><a href="#cite_note-...">[5]</a></sup>`).
    Treating "the cell has a link in it" as "the value IS the link" (an
    earlier version of this function did) silently replaced a real date
    like "February 24, 2017" with the footnote's own label, "5" — stripping
    the footnote sup first, before taking the cell's plain text, is what
    actually fixes that instead of just working around it for one field."""
    out: dict = {}
    m = re.search(r'<table[^>]*id="infobox-game"[^>]*>(.*?)</table>', html, re.DOTALL)
    if not m:
        return out
    current = None
    for row in re.finditer(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.DOTALL):
        row_html = row.group(1)
        h = re.search(r'<th[^>]*class="template-infobox-header"[^>]*>(.*?)</th>',
                     row_html, re.DOTALL)
        if h:
            current = _decode_entities(re.sub(r'<[^>]+>', '', h.group(1))).strip()
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        info_m = re.search(r'<td[^>]*class="template-infobox-info"[^>]*>(.*?)</td>',
                           row_html, re.DOTALL)
        if info_m is None:
            continue
        type_m = re.search(r'<td[^>]*class="template-infobox-type"[^>]*>(.*?)</td>',
                           row_html, re.DOTALL)
        type_txt = _decode_entities(re.sub(r'<[^>]+>', '', type_m.group(1))).strip() \
            if type_m else ''
        info_html = re.sub(r'<sup[^>]*class="[^"]*reference[^"]*"[^>]*>.*?</sup>',
                           '', info_m.group(1), flags=re.DOTALL)
        info_txt = _decode_entities(re.sub(r'<[^>]+>', '', info_html)).strip()
        info_txt = re.sub(r'\s+', ' ', info_txt)
        out[current].append((type_txt, info_txt))
    return out


def _pcgw_extract_cover(html: str) -> str:
    """The infobox's own cover image — the 2x srcset entry (full-size,
    images.pcgamingwiki.com) when present, else the plain src (a
    thumbnails.pcgamingwiki.com resize, still perfectly usable)."""
    m = re.search(
        r'<td[^>]*class="template-infobox-cover"[^>]*>.*?<img[^>]*\bsrc="([^"]+)"'
        r'(?:[^>]*\bsrcset="([^"]*)")?',
        html, re.DOTALL,
    )
    if not m:
        return ""
    src, srcset = m.group(1), m.group(2) or ""
    hi_res = re.search(r'(\S+)\s+2x', srcset)
    return hi_res.group(1) if hi_res else src


def search_pcgamingwiki(game_name: str) -> Optional[GameInfo]:
    """Search PCGamingWiki via MediaWiki OpenSearch for the page, then read
    its own rendered infobox for the structured data (see module docstring
    for why this is no longer the Cargo API). No API key needed."""
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

            parse_url = (
                f"{_PCGW_API}?action=parse"
                f"&page={_up2.quote(title)}"
                f"&prop=text|wikitext&format=json"
            )
            parse_data = _fetch_json(parse_url, headers=pcgw_headers)
            if not parse_data or "parse" not in parse_data:
                continue
            page_html = parse_data["parse"]["text"]["*"]
            page_name = parse_data["parse"].get("title") or title

            sections = _pcgw_extract_infobox(page_html)

            dev = next((info for _typ, info in sections.get("Developers", []) if info), "")
            if not dev:
                dev = next((info for _typ, info in sections.get("Publishers", []) if info), "")

            genres = []
            for _typ, info in sections.get("Taxonomy", []):
                if _typ == "Genres" and info:
                    genres = [g.strip() for g in info.split(",") if g.strip()]
                    break

            # Prefer the Windows row (the PC platform this app cares about);
            # any platform's date is still a usable year if Windows has none.
            release_rows = sections.get("Release dates", [])
            release_raw = next((info for typ, info in release_rows
                                if typ.lower() == "windows" and info), "")
            if not release_raw and release_rows:
                release_raw = release_rows[0][1]
            year = ""
            if release_raw:
                ym = re.search(r"(\d{4})", release_raw)
                if ym:
                    year = ym.group(1)

            cover_url = _pcgw_extract_cover(page_html)

            description = ""
            official_site = ""
            m = re.search(r'<div class="introduction">\s*<p>(.*?)</p>', page_html, re.DOTALL)
            if m:
                txt = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                txt = _decode_entities(re.sub(r'\s+', ' ', txt))
                description = txt[:500]
            wt = parse_data["parse"].get("wikitext", {}).get("*", "")
            m2 = re.search(r'\|official\s*site\s*=\s*(\S+)', wt)
            if m2:
                official_site = m2.group(1).strip()

            store_url = official_site or _pcgw_extract_store_url(page_html) or pg_url

            if not (dev or genres or year or cover_url or description):
                # Page exists but this pass found nothing usable on it (an
                # unusual/stub page) — try the next candidate rather than
                # returning an all-empty result.
                continue

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
            logger.info(f"PCGamingWiki (infobox): {page_name!r} (score={score:.0f})")
            return info

    except Exception as e:
        logger.debug(f"PCGamingWiki search failed for {game_name!r}: {e}")
    return None
