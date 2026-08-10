"""
SaveSync - Web scraping + search-engine layer.

Extracted verbatim from core/game_api.py: HTML fetch with browser headers,
MediaWiki search helper, the OpenGraph scraper, the search-engine rotation
(Brave/Bing/SearXNG/DuckDuckGo) with per-engine throttling and cooldowns,
the trusted-sites targeted search (itch/DLSite/MobyGames/Wikipedia) and the
generic single-query web search. Pure move.
"""
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from typing import Optional

from core.constants import CAMEL_SPLIT_RE
from core.game_sources.common import (GameInfo, _clean_description,
                                      _clean_game_name,
                                      _decode_entities, _dedupe_candidates,
                                      _earliest_forum_date,
                                      _fetch_json, _fuzzy_score, _fuzzy_slug,
                                      _is_dlsite_shop_blurb,
                                      _is_favicon_like,
                                      _is_non_game_media_title,
                                      _parse_forum_description,
                                      _strip_release_noise, _title_keep_version,
                                      source_label,
                                      _GENERIC_EXE_STEMS, _VER_NUM_RE)
from core.net import open_url as _open_url

logger = logging.getLogger(__name__)


def _normalize_itch_game_url(url: str) -> str | None:
    """Return a canonical ``https://author.itch.io/game`` URL, or None.

    Unwraps Wayback embeds (``web.archive.org/web/…/https://…itch.io/…``)
    and drops non-game itch paths (search/profile/jam/…).
    """
    if not url:
        return None
    u = urllib.parse.unquote(url.replace("&amp;", "&")).strip()
    m = re.search(
        r'(https?://[a-zA-Z0-9-]+\.itch\.io/[a-z0-9][a-z0-9-]*)',
        u, re.IGNORECASE,
    )
    if not m:
        return None
    u = m.group(1).rstrip("/")
    domain = u.split("://", 1)[-1].split("/", 1)[0].lower()
    if domain in {"static.itch.io", "api.itch.io", "img.itch.io", "itch.io"}:
        return None
    path = u.split("itch.io", 1)[-1]
    if any(path.startswith(s) for s in (
        "/search", "/games", "/jam", "/profile", "/login", "/register",
        "/docs", "/t/", "/devlogs/", "/main", "/community",
    )):
        return None
    return u


def _itch_url_live(url: str, timeout: int = 10) -> tuple[bool, str]:
    """True when *url* is a reachable itch game page (not a 404 shell).

    Prefers the lightweight ``/data.json`` probe (``invalid game`` ⇒ gone);
    falls back to a full GET and rejects the generic itch.io 404 title.
    Returns ``(ok, html_or_empty)`` — html is filled only on the GET path
    so a subsequent OG scrape can reuse it.
    """
    if not url:
        return False, ""
    try:
        st, body = _fetch_html_ex(url.rstrip("/") + "/data.json", timeout=timeout)
        if st == 200 and body.lstrip().startswith("{"):
            # Official "gone" marker only — do not treat any JSON that
            # merely contains an "errors" key as dead.
            if "invalid game" in body:
                return False, ""
            # Valid JSON payload ⇒ game exists; still need HTML for OG.
    except Exception:
        pass
    st, html = _fetch_html_ex(url, timeout=timeout)
    if not (200 <= st < 300) or not html:
        return False, ""
    m = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
    title = re.sub(r'\s+', ' ', (m.group(1) if m else "")).strip()
    # The itch 404/soft-delete shell titles itself just "itch.io".
    if not title or title.lower() in {"itch.io", "itch"}:
        return False, ""
    return True, html


def _pick_live_itch_url(
    candidates: list[str],
    game_name: str,
    *,
    max_probe: int = 8,
) -> Optional[str]:
    """Pick the best *live* itch game URL from *candidates*.

    Ranks by slug similarity to *game_name*, probes live pages (skips
    404 / ``invalid game``), and requires a title fuzzy-score ≥ 40 so a
    SERP full of unrelated 200-OK itch links does not win.
    """
    name = (game_name or "").strip()
    name_slug = _fuzzy_slug(name)
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        u = _normalize_itch_game_url(raw)
        if not u or u in seen:
            continue
        seen.add(u)
        slug = _fuzzy_slug(u.rstrip("/").rsplit("/", 1)[-1])
        # Exact slug match first; otherwise keep a mild slug score so we
        # still probe near-misses (versioned slugs like title-v0891).
        if name_slug and slug == name_slug:
            pri = 200.0
        elif name_slug and name_slug in slug:
            pri = 120.0
        else:
            pri = _fuzzy_score(name, slug.replace("-", " ")) if name else 0.0
        ranked.append((pri, u))
    ranked.sort(key=lambda x: x[0], reverse=True)

    for pri, u in ranked[:max_probe]:
        ok, html = _itch_url_live(u)
        if not ok:
            logger.info(f"itch.io candidate dead/unavailable: {u}")
            continue
        title = _scrape_itch_title(u) if not html else None
        if html and not title:
            m = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
            if m:
                title = re.sub(r'\s+by\s+\S+\s*$', '', m.group(1).strip())
                title = re.sub(r'\s+', ' ', title).strip()
        title = title or u
        score = _fuzzy_score(name, title) if name else 100.0
        if score < 40.0 and pri < 120.0:
            logger.debug(
                f"itch.io candidate rejected (score={score:.0f}): {u} title={title!r}"
            )
            continue
        # Exact/near slug match: accept even when the live title is noisy
        # ("… PC Free Game by …") as long as the page is real.
        if score < 40.0 and pri >= 120.0:
            logger.info(
                f"itch.io: accepting slug-matched live page despite "
                f"noisy title (score={score:.0f}): {u}"
            )
        else:
            logger.info(
                f"itch.io: live page {u} (title={title!r}, score={score:.0f})"
            )
        return u
    return None


def _find_itch_url_via_search(
    query: str,
    game_name: str | None = None,
    headers: dict | None = None,
) -> Optional[str]:
    """Find an itch.io game page URL via web search then direct itch.io search.

    *query* is the full search string (e.g. ``'"Example Game" site:itch.io'``).
    *game_name* is the plain game name used for fuzzy scoring on direct itch.io results
    (falls back to the query string if not given).

    Engine hits are collected and *probed* before returning: returning the
    first SERP itch URL used to silently fail when the indexed page is a
    404 / removed game (common for adult titles whose official page is
    gone but still ranks in search).
    """
    _ITCH_GAME_RE = re.compile(
        r'https?://[a-zA-Z0-9-]+\.itch\.io/[a-z0-9][a-z0-9-]*'
    )

    def _extract(html: str) -> list[str]:
        seen: dict[str, None] = {}
        for m in _ITCH_GAME_RE.finditer(html):
            u = _normalize_itch_game_url(m.group(0))
            if u and u not in seen:
                seen[u] = None
        return list(seen)

    name = game_name or query.replace('"', "").replace("site:itch.io", "").strip()
    candidates: list[str] = []

    # 1. Web search engines — gather itch URLs from the WHOLE chain, then
    #    pick a live one. Early-returning the first hit skipped every other
    #    engine and never noticed that the top result was a 404 shell.
    try:
        for engine, page_html in _iter_search_engine_html(query):
            urls = _extract(urllib.parse.unquote(page_html))
            if urls:
                logger.debug(f"{engine} found itch URLs: {urls[:3]}")
                for u in urls:
                    if u not in candidates:
                        candidates.append(u)
        hit = _pick_live_itch_url(candidates, name)
        if hit:
            return hit
        if candidates:
            logger.info(
                f"itch.io: {len(candidates)} SERP candidate(s) for {name!r} "
                f"but none reachable — trying direct itch search"
            )
    except Exception as e:
        logger.debug(f"Engine search failed for {query!r}: {e}")

    # 2. Direct itch.io search — try progressively shorter query terms.
    # Bounded on purpose: at most 4 subquery lengths, and at most 5 titles
    # scraped per subquery — the unbounded version could quietly spend a
    # minute+ fetching pages when nothing matches, with no log trace.
    name_slug = _fuzzy_slug(name)
    terms = name.split()
    for n_words in range(min(len(terms), 4), 0, -1):
        sub = " ".join(terms[:n_words])
        try:
            itch_url = f"https://itch.io/search?q={urllib.parse.quote(sub)}"
            html = _fetch_html(itch_url)
            if html:
                urls = _extract(html)
                if not urls:
                    continue
                # Prefer slug match among *live* pages.
                ordered = sorted(
                    urls,
                    key=lambda u: (
                        0 if _fuzzy_slug(u.rsplit("/", 1)[-1]) == name_slug else 1,
                        u,
                    ),
                )
                hit = _pick_live_itch_url(ordered, name, max_probe=5)
                if hit:
                    return hit
        except Exception as e:
            logger.debug(f"itch.io search for {sub!r} failed: {e}")

    return None


def _scrape_itch_title(url: str) -> str | None:
    """Quick scrape of an itch.io page title."""
    html = _fetch_html(url)
    if html:
        m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
        if m:
            t = m.group(1).strip()
            # Strip trailing " by AuthorName"
            t = re.sub(r'\s+by\s+\S+\s*$', '', t)
            return t.strip()
    return None


# Product-work URL pattern for DLSite (maniax / soft / pro / books / etc.)
_DLSITE_WORK_RE = re.compile(
    r'https?://(?:www\.)?dlsite\.com/\w+/work/=[^\s"\'<>]*\.html',
    re.IGNORECASE,
)


# Purchase geo-block banner on DLsite work pages. Metadata (OG title, …) may
# still be present underneath; a shell with only this message is unusable.
_DLSITE_REGION_LOCK_RE = re.compile(
    r'you cannot buy this product from the country/region you live in',
    re.IGNORECASE,
)
_DLSITE_SORRY_TITLE_RE = re.compile(r'^\s*sorry\b', re.IGNORECASE)


def _dlsite_region_locked(html: str) -> bool:
    return bool(html and _DLSITE_REGION_LOCK_RE.search(html))


def _dlsite_finish(url: str, html: str) -> Optional[GameInfo]:
    """Scrape a DLsite work page, logging region locks.

    A region-locked purchase banner means the work is not buyable here, but
    title/cover (and sometimes developer/genres/…) usually remain in the
    markup — propose whatever is recoverable. The site-wide shop footer is
    never kept as a description (see ``_clean_description``).
    """
    locked = _dlsite_region_locked(html)
    info = _scrape_opengraph(url, html=html)
    if locked:
        base = (url or "").split("?")[0]
        if info and (info.name or "").strip():
            logger.info(
                f"DLSite: region-locked — proposing available metadata: {base}"
            )
        else:
            logger.info(
                f"DLSite: region-locked — no usable metadata: {base}"
            )
    return info


def _scrape_dlsite_en(product_url: str) -> Optional[GameInfo]:
    """Scrape a DLsite work page in the ENGLISH locale.

    Only the section a work actually belongs to honours ``?locale=en_US``.
    Asking any other section for the same product code serves that work
    from its canonical page with the parameter IGNORED — Japanese title,
    Japanese circle name, Japanese UI. Since the product-code lookup tries
    several sections, that is how one product came back as several
    identical Japanese-titled candidates.

    So: request with the locale, then follow ``<link rel=canonical>`` when
    it points elsewhere and ask THAT page for the English locale. Whichever
    section is tried first, the data comes from the localized page.

    A work with no English title on DLsite still returns its Japanese one —
    that is the only title the site has for it, not a locale failure.
    """
    base = (product_url or "").split("?")[0]
    if not base:
        return None
    url = base + "?locale=en_US"
    html = _fetch_html(url)
    if not html:
        return None
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html)
    if m:
        canon = m.group(1)
        canon_base = canon.split("?")[0].rstrip("/")
        # "locale=" already on the canonical link means this page IS the
        # localized one; a canonical pointing at another section without it
        # means we were served that section's Japanese page.
        if "locale=" not in canon and canon_base != base.rstrip("/"):
            canon_url = canon_base + "?locale=en_US"
            canon_html = _fetch_html(canon_url)
            if canon_html:
                logger.debug(f"DLSite: followed canonical to {canon_url}")
                # Reviews are attached inside _scrape_opengraph for every
                # dlsite.com URL, so the locale redirect does not have to.
                return _dlsite_finish(canon_url, canon_html)
    return _dlsite_finish(url, html)


# How many DLsite user reviews to pull in one go. Matches the reviews panel's
# page size: more than that and the import would bury the pager under a
# scroll of unread text.
_DLSITE_REVIEW_LIMIT = 10

_DLSITE_SECTION_RE = re.compile(
    r'dlsite\.com/(maniax|soft|pro|books|girls|bl|home|comic|appx)/',
    re.IGNORECASE,
)
_DLSITE_CODE_RE = re.compile(r'product_id/([A-Z]{2}\d+)', re.IGNORECASE)


def _attach_dlsite_reviews(info: GameInfo, product_url: str,
                           html: str = "") -> None:
    """Fill *info.reviews* from DLsite's user-review list.

    The work page only mounts a Vue ``product-review-list`` stub inside
    ``#work_review`` — the ``.review_contents`` blocks the browser shows are
    rendered client-side. The JSON the component fetches
    (``/{section}/api/review``) is what we ask for. HTML parsing of
    ``.review_contents`` is kept as a fallback for pages that already have
    them embedded (the dedicated review-list view).
    """
    reviews = _fetch_dlsite_reviews_api(product_url)
    if not reviews and html:
        reviews = _parse_dlsite_review_contents(html)
    if not reviews:
        return
    info.reviews = reviews
    # A single score for the candidate chip / average display: the mean of
    # what users actually rated, not a zero that would look like a damning
    # empty verdict.
    from core.library import quantize_rating
    rated = [float(r.get("rating") or 0) for r in reviews
             if float(r.get("rating") or 0) > 0]
    if rated and not info.rating:
        info.rating = quantize_rating(sum(rated) / len(rated))
    if not info.reviewer:
        info.reviewer = "DLsite"


def _fetch_dlsite_reviews_api(product_url: str) -> list[dict]:
    """``/{section}/api/review`` → list of GameEntry-shaped review dicts."""
    sm = _DLSITE_SECTION_RE.search(product_url or "")
    cm = _DLSITE_CODE_RE.search(product_url or "")
    if not cm:
        return []
    section = (sm.group(1) if sm else "maniax").lower()
    code = cm.group(1).upper()
    api = (
        f"https://www.dlsite.com/{section}/api/review"
        f"?product_id={code}&limit={_DLSITE_REVIEW_LIMIT}"
        f"&mix_pickup=true&page=1&order=top&locale=en_US"
    )
    data = _fetch_json(api, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Referer": (product_url or "").split("?")[0] or (
            f"https://www.dlsite.com/{section}/work/=/product_id/{code}.html"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    if not isinstance(data, dict) or not data.get("is_success"):
        return []
    out: list[dict] = []
    for raw in data.get("review_list") or []:
        if not isinstance(raw, dict):
            continue
        mapped = _dlsite_api_review_to_dict(raw)
        if mapped:
            out.append(mapped)
    return out


def _dlsite_api_review_to_dict(raw: dict) -> Optional[dict]:
    """One DLsite API review → GameEntry.reviews entry."""
    from core.library import quantize_rating
    try:
        rate = float(raw.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    # Prefer a translation when the API actually shipped one; most reviews
    # stay in Japanese either way (title/text null under en_US).
    title = (raw.get("review_title") or "").strip()
    text = (raw.get("review_text") or "").strip()
    for tr in raw.get("translations") or []:
        if not isinstance(tr, dict):
            continue
        if tr.get("locale") != "en_US":
            continue
        if tr.get("title"):
            title = str(tr["title"]).strip()
        if tr.get("text"):
            text = str(tr["text"]).strip()
        break
    body = text
    if title and text:
        body = f"{title}\n\n{text}"
    elif title:
        body = title
    if rate <= 0 and not body:
        return None
    when = (raw.get("regist_date") or raw.get("entry_date") or "").strip()
    if when and "T" not in when:
        when = when.replace(" ", "T", 1)
    return {
        "id": str(raw.get("member_review_id") or "").strip(),
        "rating": quantize_rating(rate),
        "reviewer": (raw.get("nick_name") or "").strip() or "DLsite",
        "text": body,
        "notes": "",
        "source": "dlsite",
        "at": when,
    }


def _parse_dlsite_review_contents(html: str) -> list[dict]:
    """Fallback: scrape schema.org Review blocks already in the HTML.

    Used when ``#work_review`` was server-rendered with ``.review_contents``
    (the dedicated review-list page). The work page itself does not embed
    them — that path goes through ``_fetch_dlsite_reviews_api``.
    """
    from core.library import quantize_rating
    # Bound the search to #work_review when present, so a stray schema.org
    # Review elsewhere on the page cannot be mistaken for a user review.
    scope = html
    wm = re.search(
        r'<div[^>]*id=["\']work_review["\'][^>]*>([\s\S]*?)</div>\s*<div',
        html, re.I,
    )
    if wm:
        scope = wm.group(1)
    out: list[dict] = []
    for block in re.finditer(
            r'<div[^>]*class=["\'][^"\']*\breview_contents\b[^"\']*["\'][^>]*'
            r'itemprop=["\']review["\'][^>]*>([\s\S]*?)'
            r'(?=<div[^>]*class=["\'][^"\']*\breview_contents\b|$)',
            scope, re.I):
        chunk = block.group(1)
        who = ""
        am = re.search(
            r'itemprop=["\']author["\'][\s\S]*?itemprop=["\']name["\'][^>]*>\s*([^<]+)',
            chunk, re.I,
        )
        if not am:
            am = re.search(
                r'class=["\'][^"\']*reveiw_author[^"\']*["\'][^>]*>\s*<a[^>]*>\s*([^<]+)',
                chunk, re.I,
            )
        if am:
            who = am.group(1).strip()
        rate = 0.0
        rm = re.search(
            r'itemprop=["\']ratingValue["\'][^>]*content=["\']([^"\']+)',
            chunk, re.I,
        )
        if not rm:
            rm = re.search(
                r'itemprop=["\']ratingValue["\'][^>]*>\s*([^<]+)',
                chunk, re.I,
            )
        if rm:
            try:
                rate = float(rm.group(1).strip())
            except ValueError:
                rate = 0.0
        body = ""
        bm = re.search(
            r'itemprop=["\']reviewBody["\'][^>]*>([\s\S]*?)</(?:div|p|span)>',
            chunk, re.I,
        )
        if bm:
            body = re.sub(r'<[^>]+>', ' ', bm.group(1))
            body = re.sub(r'\s+', ' ', body).strip()
        when = ""
        dm = re.search(
            r'itemprop=["\']datePublished["\'][^>]*(?:content=["\']([^"\']+)'
            r'|>\s*([^<]+))',
            chunk, re.I,
        )
        if dm:
            when = (dm.group(1) or dm.group(2) or "").strip()
        if rate <= 0 and not body:
            continue
        out.append({
            "id": "",
            "rating": quantize_rating(rate),
            "reviewer": who or "DLsite",
            "text": body,
            "notes": "",
            "source": "dlsite",
            "at": when,
        })
        if len(out) >= _DLSITE_REVIEW_LIMIT:
            break
    return out


def _find_dlsite_url_via_search(keyword: str) -> Optional[str]:
    """Find a DLSite product work URL via web search (shared engine layer).

    Mirrors _find_itch_url_via_search: prefers search-engine discovery over
    DLSite's own /fsr/ endpoint, which is frequently rate-limited or returns
    JavaScript-only pages that prevent reliable HTML parsing.
    Returns the first product URL found, or None.
    """
    def _extract(html: str) -> Optional[str]:
        m = _DLSITE_WORK_RE.search(html)
        if m:
            # Strip any trailing quote / fragment that may have been consumed
            return re.split(r'["\'<>\s]', m.group(0))[0]
        return None

    queries = [
        f'"{keyword}" site:dlsite.com',
        f'{keyword} site:dlsite.com',
    ]

    for query in queries:
        try:
            for engine, page_html in _iter_search_engine_html(query):
                # Decode DDG's percent-encoded redirect URLs before matching
                url = _extract(urllib.parse.unquote(page_html))
                if url:
                    logger.debug(f"{engine} found DLSite URL for {keyword!r}: {url}")
                    return url
        except Exception as exc:
            logger.debug(f"Engine DLSite search failed for {query!r}: {exc}")

    return None


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _fetch_html_ex(url: str, timeout: int = 12,
                   extra_headers: dict | None = None) -> tuple[int, str]:
    """Fetch *url* and return (http_status, decoded_html).

    Status 0 means a transport failure (DNS, timeout, refused connection);
    an HTTPError becomes its status code with the (decoded) error body, so
    callers can tell a 403/challenge wall apart from an empty page."""
    def _decode(raw, headers) -> str:
        enc = (headers.get_content_charset("utf-8") if headers else None) or "utf-8"
        # Some sites (e.g. DLSite) claim UTF-8 but serve cp932/Shift-JIS.
        # Try strict decode first; if that fails, try common JP encodings.
        for e in (enc, "cp932", "utf-8"):
            try:
                return raw.decode(e, errors="strict")
            except (LookupError, UnicodeDecodeError, ValueError):
                continue
        # Last resort: UTF-8 with replace (garbled but survivable)
        return raw.decode("utf-8", errors="replace")

    hdrs = dict(_DEFAULT_HEADERS)
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with _open_url(req, timeout=timeout) as r:
            return r.status, _decode(r.read(), r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode(e.read(), e.headers)
        except Exception:
            return e.code, ""
    except Exception as e:
        logger.debug(f"_fetch_html {url[:60]}: {e}")
        return 0, ""


def _fetch_html(url: str, timeout: int = 12,
                extra_headers: dict | None = None) -> str:
    """Fetch *url* and return the decoded HTML.  Returns empty string on any error."""
    status, html = _fetch_html_ex(url, timeout=timeout, extra_headers=extra_headers)
    return html if 200 <= status < 300 else ""


# ── Targeted site search ──────────────────────────────────────────────────────
#
# Instead of scraping generic search engine results (which block bots),
# we search directly against trusted, structured game databases.
# Each source is tried in order; the first that returns a plausible match wins.
#
    # Sources covered (no API key required):
    #   1. PCGamingWiki     – MediaWiki OpenSearch API  → title + URL → scrape OG
    #   2. MobyGames HTML   – /search/quick?q=         → extract game page URL
    #   3. GameFAQs search  – /search?game=            → first result URL
    #   4. itch.io          – via web search (SearXNG/DDG) with site: filter
    #   5. DLSite           – for Japanese/indie games
    #   6. Wikipedia        – MediaWiki OpenSearch API  → title + URL → scrape OG (last resort)
    #
    # YouTube is explicitly excluded from all queries.
    # ─────────────────────────────────────────────────────────────────────────────

def _mediawiki_search(base_url: str, query: str,
                      namespace: int = 0) -> list[tuple[str, str]]:
    """OpenSearch on a MediaWiki instance.  Returns list of (title, page_url)."""
    api = (
        f"{base_url}?action=opensearch&search={urllib.parse.quote(query)}"
        f"&limit=5&namespace={namespace}&format=json"
    )
    try:
        html = _fetch_html(api)
        if not html:
            return []
        data = json.loads(html)
        # OpenSearch returns [query, [titles], [descriptions], [urls]]
        titles = data[1] if len(data) > 1 else []
        urls   = data[3] if len(data) > 3 else []
        return list(zip(titles, urls))
    except Exception as e:
        logger.debug(f"MediaWiki OpenSearch failed ({base_url}): {e}")
        return []


# Store-link triggers for forum first posts: an anchor whose TEXT says it's
# the shop ("Store"/"Shop"/"Buy"/…) or whose HOST is a known store domain —
# the latter catches store links whose visible text is the registration-gate
# placeholder instead of a real label.
_STORE_LINK_TEXT_RE = re.compile(
    r'\b(store|shop|buy|purchase|official\s+(?:site|page|website)'
    r'|website|homepage)\b', re.IGNORECASE)
_STORE_LINK_HOST_RE = re.compile(
    r'(store\.steampowered\.com|\bitch\.io|dlsite\.com|\bgog\.com'
    r'|gamejolt\.com|nutaku\.net)', re.IGNORECASE)


def _ld_aggregate_rating(ld: dict) -> tuple[float, str, str, int]:
    """Pull a 5-star score out of a JSON-LD node's aggregateRating.

    Returns (stars, reviewer_label, summary_text, vote_count), or
    (0, "", "", 0) when the node has no usable score. Scales whatever
    bestRating the site declared (itch uses 5, some Product pages use 100)
    onto SaveSync's five stars. *vote_count* is the underlying ratingCount
    / reviewCount so a store average is never treated as one opinion.
    """
    agg = ld.get("aggregateRating")
    if not isinstance(agg, dict):
        return 0.0, "", "", 0
    try:
        value = float(agg.get("ratingValue") or 0)
    except (TypeError, ValueError):
        return 0.0, "", "", 0
    if value <= 0:
        return 0.0, "", "", 0
    try:
        best = float(agg.get("bestRating") or 5)
    except (TypeError, ValueError):
        best = 5.0
    if best <= 0:
        best = 5.0
    stars = value * (5.0 / best)
    # Cap before quantize: a site that put bestRating below the value would
    # otherwise produce a 6-star score that collapses to "unrated".
    stars = min(stars, 5.0)
    count = agg.get("ratingCount") or agg.get("reviewCount") or ""
    try:
        count_n = int(float(count)) if count != "" else 0
    except (TypeError, ValueError):
        count_n = 0
    # Reviewer label is filled by the caller from the page's source id
    # (itch / mobygames / …); leave it blank here so the source wins.
    # Display shape matches Steam/VNDB aggregates: "4.6/5 (1575)".
    summary = f"{value:g}/{best:g}"
    if count_n:
        summary = f"{summary} ({count_n})"
    return stars, "", summary, count_n


# How many forum user reviews to pull from the Reviews tab's first page.
# These boards list twenty per page; taking fewer would throw away reviews
# already sitting in the HTML we fetched. The reviews panel still pages
# them ten at a time for display — that is independent of how many we keep.
_FORUM_REVIEW_LIMIT = 20

# XenForo threads that split the product post from the user-review list put
# the latter on a sibling path (…/br-reviews/, optionally …/br-reviews/page-N).
# Matched generically — the addon name is not a source label we ever store.
_FORUM_REVIEWS_PATH_RE = re.compile(
    r'/br-reviews(?:/page-\d+)?/?$', re.IGNORECASE,
)
_FORUM_REVIEWS_HREF_RE = re.compile(
    r'/br-reviews(?:/|$|\?)', re.IGNORECASE,
)
_FORUM_REVIEWS_TAB_TEXT_RE = re.compile(
    r'^\s*reviews?\s*(?:\(\d+\))?\s*$', re.IGNORECASE,
)


def _is_forum_reviews_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url or "").path or ""
    return bool(_FORUM_REVIEWS_PATH_RE.search(path))


def _forum_thread_base_url(url: str) -> str:
    """Product-thread URL for a reviews-tab URL, otherwise the URL itself."""
    base = (url or "").split("?")[0]
    trimmed = _FORUM_REVIEWS_PATH_RE.sub("/", base)
    if not trimmed.endswith("/"):
        # XenForo thread canonicals keep the trailing slash; without it the
        # reviews-tab join below would produce …/287854br-reviews.
        trimmed += "/"
    return trimmed


def _looks_like_forum_reviews_html(html: str) -> bool:
    """Whether *html* is a reviews-tab listing rather than the product post."""
    return bool(html and re.search(
        r'class=["\'][^"\']*\bmessage--review\b', html, re.IGNORECASE))


def _find_forum_reviews_tab(html: str, page_url: str) -> str:
    """Absolute URL of the Reviews tab linked from a product thread, or ""."""
    if not html:
        return ""
    for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            html, re.IGNORECASE):
        href = m.group(1).strip()
        text = re.sub(r'<[^>]+>', '', m.group(2))
        text = re.sub(r'\s+', ' ', text).strip()
        if _FORUM_REVIEWS_HREF_RE.search(href) or (
                _FORUM_REVIEWS_TAB_TEXT_RE.match(text)
                and re.search(r'review', href, re.IGNORECASE)):
            return urllib.parse.urljoin(page_url, href)
    return ""


def _web_reviewer_label() -> str:
    """Fallback reviewer label for an anonymous web review."""
    try:
        from i18n import t
        return t("add_game.web_source_generic")
    except Exception:
        return "web"


def _parse_forum_message_reviews(html: str) -> list[dict]:
    """User reviews from a XenForo-style reviews tab.

    Shape observed on threads that park reviews on a sibling tab:
      ``.message.message--review.js-review`` with ``data-author``,
      ``data-content="review-…"``, a ``.ratingStars`` title of
      ``"N.NN star(s)"``, body in ``.bbWrapper``, date in ``<time datetime>``.
    Source is left as generic ``web`` — the forum is not a named catalogue.
    """
    from core.library import quantize_rating
    if not html:
        return []
    out: list[dict] = []
    # Match ONLY the opening tag so finditer advances past each card's
    # start, not past an 8 KB window that would swallow the next opens.
    for m in re.finditer(
            r'<div([^>]*class=["\'][^"\']*\bmessage--review\b[^"\']*["\'][^>]*)>',
            html, re.IGNORECASE):
        attrs = m.group(1)
        chunk = html[m.end():m.end() + 8000]
        nxt = re.search(
            r'<div[^>]*class=["\'][^"\']*\bmessage--review\b',
            chunk, re.IGNORECASE,
        )
        if nxt:
            chunk = chunk[:nxt.start()]
        who = ""
        am = re.search(r'data-author=["\']([^"\']+)["\']', attrs, re.I)
        if am:
            who = _decode_entities(am.group(1)).strip()
        if not who:
            um = re.search(
                r'class=["\'][^"\']*\busername\b[^"\']*["\'][^>]*>\s*'
                r'(?:<span[^>]*>)?\s*([^<]+)',
                chunk, re.I,
            )
            if um:
                who = _decode_entities(um.group(1)).strip()
        rid = ""
        im = re.search(r'data-content=["\']([^"\']+)["\']', attrs, re.I)
        if im:
            rid = im.group(1).strip()
        rate = 0.0
        rm = re.search(
            r'class=["\'][^"\']*\bratingStars\b[^"\']*["\'][^>]*'
            r'title=["\']\s*([\d.]+)\s*star',
            chunk, re.I,
        ) or re.search(
            r'class=["\'][^"\']*\bu-srOnly\b[^"\']*["\'][^>]*>\s*'
            r'([\d.]+)\s*star',
            chunk, re.I,
        )
        if rm:
            try:
                rate = float(rm.group(1))
            except ValueError:
                rate = 0.0
        body = ""
        bm = re.search(
            r'class=["\'][^"\']*\bbbWrapper\b[^"\']*["\'][^>]*>([\s\S]*?)</div>',
            chunk, re.I,
        ) or re.search(
            r'<article[^>]*class=["\'][^"\']*\bmessage-body\b[^"\']*["\'][^>]*>'
            r'([\s\S]*?)</article>',
            chunk, re.I,
        )
        if bm:
            raw = bm.group(1)
            raw = re.sub(r'<br\s*/?>', '\n', raw, flags=re.I)
            raw = re.sub(r'</p\s*>', '\n', raw, flags=re.I)
            raw = re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>',
                         ' ', raw, flags=re.I)
            raw = re.sub(r'<[^>]+>', ' ', raw)
            body = re.sub(r'[ \t]+\n', '\n', _decode_entities(raw))
            body = re.sub(r'\n{3,}', '\n\n', body).strip()
            body = re.sub(r'[^\S\n]{2,}', ' ', body)
        when = ""
        dm = re.search(
            r'<time[^>]+datetime=["\']([^"\']+)["\']',
            chunk, re.I,
        )
        if dm:
            when = dm.group(1).strip()
        if rate <= 0 and not body:
            continue
        out.append({
            "id": rid,
            "rating": quantize_rating(rate),
            "reviewer": who or _web_reviewer_label(),
            "text": body,
            "notes": "",
            "source": "web",
            "at": when,
        })
        if len(out) >= _FORUM_REVIEW_LIMIT:
            break
    return out


def _attach_forum_reviews(info: GameInfo, page_url: str, html: str,
                          reviews_html: Optional[str] = None) -> None:
    """Pull the Reviews-tab listing into *info.reviews* when the thread has one.

    *reviews_html* is the already-fetched tab body (caller pasted the tab
    URL). Otherwise the product page's Reviews link is followed. No-op when
    the page is not a forum thread with that split.
    """
    reviews: list[dict] = []
    if reviews_html and _looks_like_forum_reviews_html(reviews_html):
        reviews = _parse_forum_message_reviews(reviews_html)
    elif _looks_like_forum_reviews_html(html):
        reviews = _parse_forum_message_reviews(html)
    else:
        tab = _find_forum_reviews_tab(html, page_url)
        if tab:
            tab_html = _fetch_html(tab)
            if tab_html:
                reviews = _parse_forum_message_reviews(tab_html)
    if not reviews:
        return
    # Forum reviews are additive to whatever a store link on the same page
    # already contributed; identity is per review id, so duplicates collapse
    # later in the merge path.
    existing = list(info.reviews or [])
    have = {str(r.get("id") or "") for r in existing if r.get("id")}
    for r in reviews:
        rid = str(r.get("id") or "")
        if rid and rid in have:
            continue
        existing.append(r)
        if rid:
            have.add(rid)
    info.reviews = existing
    from core.library import quantize_rating
    rated = [float(r.get("rating") or 0) for r in existing
             if float(r.get("rating") or 0) > 0]
    if rated and not info.rating:
        info.rating = quantize_rating(sum(rated) / len(rated))
    if not info.reviewer:
        info.reviewer = _web_reviewer_label()


def _scrape_opengraph(url: str, html: Optional[str] = None) -> Optional[GameInfo]:
    """Fetch *url* and extract Open Graph / JSON-LD / meta tag info.

    Works for any modern site: PCGamingWiki, Wikipedia, itch.io, GOG,
    Epic store pages, DLSite, GameFAQs, etc.
    Extracts genres from keywords, og:video:tag, and JSON-LD.
    YouTube URLs are never fetched.

    *html*, when given, is used as the already-fetched page body (the
    caller wanted the HTTP status too — see _fetch_html_ex) so the page
    is not downloaded twice.
    """
    if not url or "youtube.com" in url or "youtu.be" in url:
        return None

    # XenForo-style product threads sometimes park user reviews on a sibling
    # tab (…/br-reviews/). Pasting that tab should still fill the product
    # fields from the main thread, and keep this page's body for the list.
    _forum_reviews_html: Optional[str] = None
    _forum_reviews_url = ""
    if _is_forum_reviews_url(url) or (html and _looks_like_forum_reviews_html(html)):
        _forum_reviews_url = url
        _forum_reviews_html = html
        parent = _forum_thread_base_url(url)
        parent_norm = parent.rstrip("/")
        here_norm = (url or "").split("?")[0].rstrip("/")
        if parent_norm and parent_norm != here_norm:
            url = parent
            html = None

    if html is None:
        html = _fetch_html(url)
    if not html:
        return None

    if _forum_reviews_url and _forum_reviews_html is None:
        _forum_reviews_html = _fetch_html(_forum_reviews_url)

    def _meta(*names: str) -> str:
        for name in names:
            for m in [
                re.search(
                    rf'<meta\s+(?:property|name)=["\'](?:og:|twitter:)?{re.escape(name)}["\']\s+content=["\']([^"\']+)["\']',
                    html, re.IGNORECASE,
                ),
                re.search(
                    rf'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\'](?:og:|twitter:)?{re.escape(name)}["\']',
                    html, re.IGNORECASE,
                ),
            ]:
                if m:
                    return m.group(1).strip()
        return ""

    # Title
    title = _meta("og:title", "title", "twitter:title")
    if not title:
        m = re.search(r"<title>([^<]{2,120})</title>", html)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Strip platform suffixes
    title = re.sub(
        r'\s*[\|\-–—]\s*(itch\.io|gog\.com|epic games|steam|gog|itch'
        r'|gamejolt\.com|humble bundle|pcgamingwiki|wikipedia'
        r'|dlsite\.com|gamefaqs\.gamespot\.com|moby ?games?).*$',
        '', title, flags=re.IGNORECASE,
    ).strip()
    # DLsite region-lock shells sometimes surface "SORRY..." as og:title —
    # clear it and keep scraping; itemprop="name" / JSON-LD often still
    # carry the real work title. Non-DLsite pages still require a title now.
    _url_is_dlsite = "dlsite.com" in (url or "").lower()
    if title and _url_is_dlsite and _DLSITE_SORRY_TITLE_RE.match(title):
        logger.info(
            f"DLSite: placeholder title on {url} — trying page markup"
        )
        title = ""
    if not title and not _url_is_dlsite:
        return None

    _raw_description = _meta("og:description", "description", "twitter:description")
    description = _raw_description[:600]
    # DLsite OG description is often the site-wide shop blurb — drop it
    # early so the work body / JSON-LD can fill a real synopsis instead.
    if _url_is_dlsite and description and (
            _is_dlsite_shop_blurb(description)
            or not _clean_description(description)):
        description = ""
    image_url   = _meta("og:image:secure_url", "og:image", "twitter:image")

    # Resolve relative image URLs to absolute
    if image_url:
        from urllib.parse import urljoin as _urljoin
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("/"):
            from urllib.parse import urlparse as _up
            p = _up(url)
            image_url = f"{p.scheme}://{p.netloc}{image_url}"
        elif not image_url.startswith(("http://", "https://")):
            image_url = _urljoin(url, image_url)

    # Also try itemprop="image" and link[rel="image_src"]
    if not image_url:
        for m in [
            re.search(r'<meta\s+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE),
            re.search(r'<meta\s+content=["\']([^"\']+)["\'][^>]+itemprop=["\']image["\']', html, re.IGNORECASE),
            re.search(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE),
            re.search(r'<link[^>]+href=["\']([^"\']+)["\'](?:[^>]*)rel=["\']image_src["\']', html, re.IGNORECASE),
        ]:
            if m:
                img_raw = m.group(1).strip()
                if img_raw.startswith("//"):
                    image_url = "https:" + img_raw
                elif img_raw.startswith("/"):
                    from urllib.parse import urlparse as _up
                    p = _up(url)
                    image_url = f"{p.scheme}://{p.netloc}{img_raw}"
                elif img_raw.startswith(("http://", "https://")):
                    image_url = img_raw
                else:
                    from urllib.parse import urljoin as _urljoin
                    image_url = _urljoin(url, img_raw)
                break

    # A favicon/site-logo/tiny icon set as og:image (or itemprop/image_src) is
    # not a cover — drop it so the HTML image scan below (which already skips
    # these) or "no image" takes over, instead of using the site's favicon.
    if _is_favicon_like(image_url):
        logger.debug(f"Rejecting favicon/icon as cover: {image_url}")
        image_url = ""

    # Fallback: find first plausible image tag in HTML when OG/twitter image absent
    if not image_url:
        import re as _re
        _SKIP_IMG = _re.compile(
            r'(logo|icon|avatar|favicon|spinner|loader|pixel|badge|rating|star|ads?[/_])',
            _re.IGNORECASE,
        )

        def _resolve_src(src: str) -> str | None:
            src = src.strip()
            if _SKIP_IMG.search(src):
                return None
            if src.startswith("//"):
                return "https:" + src
            if src.startswith("/"):
                from urllib.parse import urlparse as _up
                p = _up(url)
                return f"{p.scheme}://{p.netloc}{src}"
            if src.startswith(("http://", "https://")):
                return src
            from urllib.parse import urljoin as _urljoin
            resolved = _urljoin(url, src)
            return resolved if resolved.startswith("http") else None

        # Forum threads: prefer the first full-size attachment linked from
        # the OP body (<a href=…attachments…/file.jpg>) over later thumbs
        # or avatars elsewhere on the page.
        _op = (
            _re.search(
                r'<article[^>]+class=["\'][^"\']*message-body[^"\']*["\'][^>]*>'
                r'([\s\S]*?)</article>',
                html, _re.IGNORECASE,
            )
            or _re.search(
                r'<div[^>]+class=["\'][^"\']*\bbbWrapper\b[^"\']*["\'][^>]*>'
                r'([\s\S]{0,30000})',
                html, _re.IGNORECASE,
            )
        )
        _op_html = _op.group(1) if _op else ""
        if _op_html:
            for _am in _re.finditer(
                r'<a[^>]+href=(["\'])(https?://[^"\']*attachments\.[^"\']+\.'
                r'(?:png|jpg|jpeg|webp|gif|avif|bmp)(?:\?[^"\']*)?)\1',
                _op_html, _re.IGNORECASE,
            ):
                _href = _am.group(2)
                if "/thumb/" in _href:
                    _href = _href.replace("/thumb/", "/", 1)
                if not _SKIP_IMG.search(_href):
                    image_url = _href
                    break
            if not image_url:
                for _im in _re.finditer(
                    r'<img[^>]+src=(["\'])(https?://[^"\']+\.'
                    r'(?:png|jpg|jpeg|webp|gif|avif|bmp)(?:\?[^"\']*)?)\1',
                    _op_html, _re.IGNORECASE,
                ):
                    src = _resolve_src(_im.group(2))
                    if src:
                        image_url = src
                        break

        # Pass 1: URLs ending in known image extensions (whole page)
        if not image_url:
            for m in _re.finditer(
                r'<img[^>]+src=([\'"])(.*?\.(?:png|jpg|jpeg|webp|gif|avif|bmp))(?:\?[^\1]*)?\1',
                html, _re.IGNORECASE,
            ):
                src = _resolve_src(m.group(2))
                if src:
                    image_url = src
                    break

        # Pass 2: any img src with a path that looks like an image (no extension needed, CDNs)
        if not image_url:
            _IMG_ANY_RE = _re.compile(
                r'<img[^>]+src=([\'"])((?:https?:)?//[^\1]{10,})?\1',
                _re.IGNORECASE,
            )
            for m in _IMG_ANY_RE.finditer(html):
                raw = m.group(2)
                if not raw:
                    continue
                path = raw.split("?")[0].rstrip("/")
                if not path or path.endswith(("/", ".html", ".php", ".aspx", ".css", ".js")):
                    continue
                if re.search(r'(logo|icon|avatar|favicon|spinner|loader|pixel|badge|star)', raw, re.IGNORECASE):
                    continue
                src = _resolve_src(raw)
                if src:
                    image_url = src
                    break

        # Pass 3: last resort — first img with an absolute http(s) src that isn't tiny
        if not image_url:
            for m in _re.finditer(
                r'<img[^>]+src=([\'"])(https?://[^\'"]+?\.\w{2,5}(?:[?#]\S*?)?)\1',
                html, _re.IGNORECASE
            ):
                raw = m.group(2).strip()
                if re.search(r'(logo|icon|avatar|favicon|spinner|loader|pixel|badge|star|sprite)', raw, re.IGNORECASE):
                    continue
                if re.search(r'[\d]+x[\d]+(?:\.[a-z]+)?$', raw.split("/")[-1].split("?")[0]):
                    continue
                src = _resolve_src(raw)
                if src:
                    image_url = src
                    break

    # Attachment CDNs often serve a /thumb/ preview in <img src> while the
    # parent <a href> points at the full file — prefer the full URL.
    if image_url and "/thumb/" in image_url:
        image_url = image_url.replace("/thumb/", "/", 1)
    if image_url and _is_favicon_like(image_url):
        logger.debug(f"Rejecting favicon/icon as cover: {image_url}")
        image_url = ""

    # Genres from meta keywords / tags
    genres: list[str] = []
    kw = _meta("keywords", "og:video:tag", "genre")
    if kw:
        genres = [k.strip() for k in re.split(r'[,;|]', kw) if k.strip()][:8]

    # Game-related JSON-LD type keywords (ignore Article/BreadcrumbList/WebSite)
    _GAME_LD_TYPES = ("game", "videogame", "softwareapplication", "product", "creativework")
    # Forum thread JSON-LD still carries the original post date — useful when
    # the labelled "Release Date" is just the bump stamp.
    _FORUM_LD_TYPES = ("discussionforumposting", "discussionforum", "socialmediaposting")

    def _ld_is_game_related(ld: dict) -> bool:
        t = (ld.get("@type") or "").lower().replace(" ", "")
        return any(gt in t for gt in _GAME_LD_TYPES)

    def _ld_is_forum_post(ld: dict) -> bool:
        t = (ld.get("@type") or "").lower().replace(" ", "")
        return any(ft in t for ft in _FORUM_LD_TYPES)

    ld_dates: list[str] = []
    ld_forum_dates: list[str] = []
    ld_publishers: list[str] = []
    ld_developers: list[str] = []
    # Aggregate score from JSON-LD (itch.io, MobyGames, Product pages…).
    # Kept as (stars_0_to_5, reviewer_label, summary_text) and applied to
    # GameInfo at the end so Steam/VNDB/DLsite-specific paths can still
    # overwrite with a richer verdict.
    ld_rating: float = 0.0
    ld_reviewer: str = ""
    ld_review_text: str = ""
    ld_vote_count: int = 0
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(m.group(1))
            for ld in ([data] if isinstance(data, list) else [data]):
                if not isinstance(ld, dict):
                    continue
                is_game = _ld_is_game_related(ld)
                is_forum = _ld_is_forum_post(ld)
                if not title and ld.get("name"):
                    _ld_name = str(ld["name"]).strip()
                    if not (_url_is_dlsite
                            and _DLSITE_SORRY_TITLE_RE.match(_ld_name)):
                        title = _ld_name
                if not description and ld.get("description"):
                    _ld_desc = str(ld["description"])[:600]
                    if not (_url_is_dlsite and (
                            _is_dlsite_shop_blurb(_ld_desc)
                            or not _clean_description(_ld_desc))):
                        description = _ld_desc
                if not image_url and isinstance(ld.get("image"), str):
                    img = ld["image"]
                    if img.startswith("//"):
                        img = "https:" + img
                    elif img.startswith("/"):
                        from urllib.parse import urlparse as _up
                        p = _up(url)
                        img = f"{p.scheme}://{p.netloc}{img}"
                    elif not img.startswith(("http://", "https://")):
                        from urllib.parse import urljoin as _urljoin
                        img = _urljoin(url, img)
                    if not _is_favicon_like(img):
                        image_url = img
                # Forum posts: datePublished is the original thread date —
                # compared later with the labelled Release Date (earlier wins).
                # Never use the forum JSON-LD image (usually an avatar).
                if is_forum:
                    for dk in ("datePublished", "dateCreated"):
                        dv = ld.get(dk)
                        if dv:
                            ld_forum_dates.append(str(dv))
                            break
                # Only extract game-specific fields from game-related types
                if is_game:
                    for dk in ("datePublished", "releaseDate"):
                        dv = ld.get(dk)
                        if dv:
                            ld_dates.append(str(dv))
                            break
                    pub = ld.get("publisher")
                    if pub:
                        if isinstance(pub, str):
                            ld_publishers.append(pub)
                        elif isinstance(pub, list):
                            for p in pub:
                                ld_publishers.append(str(p) if isinstance(p, str) else (p.get("name", str(p)) if isinstance(p, dict) else str(p)))
                        elif isinstance(pub, dict) and pub.get("name"):
                            ld_publishers.append(str(pub["name"]))
                    for ak in ("author", "creator", "brand"):
                        val = ld.get(ak)
                        if val:
                            if isinstance(val, str):
                                ld_developers.append(val)
                            elif isinstance(val, list):
                                for v in val:
                                    ld_developers.append(str(v) if isinstance(v, str) else (v.get("name", str(v)) if isinstance(v, dict) else str(v)))
                            elif isinstance(val, dict) and val.get("name"):
                                ld_developers.append(str(val["name"]))
                    for gk in ("genre", "gameCategory", "applicationCategory"):
                        val = ld.get(gk)
                        if val:
                            for g in ([val] if isinstance(val, str) else val):
                                if str(g) not in genres:
                                    genres.append(str(g))
                    if not ld_rating:
                        stars, who, summary, votes = _ld_aggregate_rating(ld)
                        if stars:
                            ld_rating, ld_reviewer, ld_review_text = stars, who, summary
                            ld_vote_count = votes
        except (json.JSONDecodeError, TypeError):
            pass

    release_date = ld_dates[0] if ld_dates else ""
    publisher = "; ".join(dict.fromkeys(ld_publishers)) if ld_publishers else ""
    developer = "; ".join(dict.fromkeys(ld_developers)) if ld_developers else ""

    # Site-specific HTML extraction for fields OG/JSON-LD missed
    if "dlsite.com" in url:
        # DLSite: extract release, developer, genre from HTML tables

        # Developer: Brand (pro) / Circle (maniax), then itemprop.
        # Stay inside the <td> — a loose [\s\S]*?<a> on region-locked shells
        # walks into the page footer and captures "Back to Top".
        def _dlsite_dev_ok(name: str) -> bool:
            n = (name or "").strip()
            if not n or len(n) > 80:
                return False
            return not re.search(
                r'^(back\s*to\s*top|top\s*of\s*(?:the\s*)?page'
                r'|ページの先頭|ページトップ|ページ上部へ)',
                n, re.IGNORECASE,
            )

        for _pat in (
            r'<th>(?:ブランド名|サークル名|Brand|Circle)</th>\s*'
            r'<td\b[^>]*>([\s\S]*?)</td>',
            r'itemprop=["\']brand["\'][^>]*>\s*<a[^>]*>([^<]+)</a>',
        ):
            if developer:
                break
            _dm = re.search(_pat, html, re.IGNORECASE)
            if not _dm:
                continue
            _chunk = _dm.group(1)
            _am = re.search(r'<a[^>]*>([^<]+)</a>', _chunk) if '<' in _chunk else None
            _cand = (_am.group(1) if _am else _chunk).strip()
            _cand = re.sub(r'<[^>]+>', '', _cand).strip()
            if _dlsite_dev_ok(_cand):
                developer = _cand
        if developer and not _dlsite_dev_ok(developer):
            developer = ""

        # Release date (販売日 / Release)
        dm = re.search(r'<th>(?:販売日|Release\s*date)</th>\s*<td>(?:<a[^>]*>)?([^<]+)(?:</a>)?', html)
        if dm and not release_date:
            release_date = dm.group(1).strip().replace("年", "-").replace("月", "-").replace("日", "")
            # The cell can continue into a sale time ("Aug/02/2026 05:00"),
            # and the markup splits it so the tail arrives truncated
            # ("Aug/02/2026 0"). Keep the leading date and drop the rest;
            # an unrecognised format is left untouched.
            _dm = re.match(
                r'\s*([A-Za-z]{3,9}[/\s.\-]\d{1,2}[/\s.\-]\d{4}'
                r'|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}'
                r'|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})',
                release_date,
            )
            if _dm:
                release_date = _dm.group(1)

        # Category type (作品形式 / Product format)
        cm = re.search(r'<th>(?:作品形式|Product\s*format|Category)</th>[\s\S]*?<span[^>]*title="([^"]+)"', html)
        if cm:
            cat = cm.group(1).strip()
            if cat not in genres:
                genres.append(cat)

        # Genre tags (ジャンル / Genre)
        gm = re.search(r'<th>(?:ジャンル|Genre)</th>\s*<td>\s*<div class="main_genre">([\s\S]*?)</div>', html)
        if gm:
            for a_match in re.finditer(r'<a[^>]*>([^<]+)</a>', gm.group(1)):
                g = a_match.group(1).strip()
                if g and g not in genres:
                    genres.append(g)

        # Cleaner title: itemprop="name" has clean name without "| DLsite" suffix
        im = re.search(r'itemprop=["\']name["\'][^>]*>\s*([^<]+)\s*<', html)
        if im and not im.group(1).strip().endswith("DLsite"):
            title_clean = im.group(1).strip()
            if title_clean and len(title_clean) > 2:
                title = title_clean

        # Description from work_parts_container (more detailed than og:description).
        # Prefer the work body whenever present — OG is frequently only the
        # shop footer, which would otherwise block this path.
        _wpc = re.search(
            r'<div\s+itemprop=["\']description["\']\s+class=["\']work_parts_container["\']>'
            r'\s*<div\s+class=["\']work_parts_body["\']>(.*?)</div>\s*</div>',
            html, re.S | re.I
        )
        if _wpc:
            _d_desc = re.sub(r'<[^>]+>', '', _wpc.group(1)).strip()
            _d_desc = re.sub(r'\s+', ' ', _d_desc).strip()
            if (_d_desc and len(_d_desc) > 20
                    and not _is_dlsite_shop_blurb(_d_desc)):
                description = _d_desc[:600]
        if description and (_is_dlsite_shop_blurb(description)
                            or not _clean_description(description)):
            description = ""

    elif "wikipedia.org" in url:
        # Wikipedia: extract description from first substantial paragraph
        if not description:
            for pm in re.finditer(r'<p[^>]*>(.*?)</p>', html, re.S):
                desc_raw = re.sub(r'<[^>]+>', '', pm.group(1)).strip()
                desc_raw = re.sub(r'\[\d+\]', '', desc_raw).strip()
                desc_raw = re.sub(r'\.mw-parser-output[^}]*\}', '', desc_raw).strip()
                desc_raw = re.sub(r'\s+', ' ', desc_raw).strip()
                if desc_raw and len(desc_raw) > 20:
                    description = desc_raw[:600]
                    break

        # Wikipedia: extract developer, publisher, release, genre from infobox
        ib = re.search(r'<table[^>]*class="[^"]*infobox[^"]*"[^>]*>(.*?)</table>', html, re.I | re.S)
        if ib:
            ib_html = ib.group(1)
            # Strip <style> blocks and <sup> tags before parsing
            ib_html = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', ib_html)
            ib_html = re.sub(r'<sup[^>]*>[\s\S]*?</sup>', '', ib_html)
            for row in re.finditer(r'<th[^>]*scope=["\']row["\'][^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', ib_html, re.I | re.S):
                label = re.sub(r'<[^>]+>', '', row.group(1)).strip().lower()
                value_raw = re.sub(r'<[^>]+>', ' ', row.group(2))
                value = re.sub(r'\s+', ' ', value_raw).strip()
                if label == "developer" and not developer:
                    developer = value
                elif "publisher" in label and not publisher:
                    publisher = value
                elif label == "release" and not release_date:
                    # Clean up multi-region to just take the earliest year
                    year_m = re.search(r'(\d{4})', value)
                    if year_m:
                        release_date = year_m.group(1)
                    else:
                        release_date = value[:80]
                elif label in ("genre", "genres") and not genres:
                    for g in re.split(r'[,;]', value):
                        gs = g.strip().strip('.')
                        if gs and gs not in genres:
                            genres.append(gs)

    elif "mobygames.com" in url:
        # MobyGames: extract Developers from static Vue-rendered <dt>Developers</dt>
        if not developer:
            dm = re.search(r'<dt>[Dd]evelopers?</dt>\s*<dd>[\s\S]*?<a[^>]*>([^<]+)</a>', html)
            if dm:
                developer = dm.group(1).strip()

    elif "vndb.org" in url:
        # VNDB entry page: developer + release date live in the infobox table
        # (<tr><td>Developer</td><td><a …>Name</a></td></tr>) — no JSON-LD,
        # so without this the generic web search would drop the developer.
        if not developer:
            dm = re.search(
                r'<td>Developers?</td>\s*<td>[\s\S]*?<a[^>]*>([^<]+)</a>', html)
            if dm:
                developer = dm.group(1).strip()
        if not release_date:
            rm = re.search(r'<td>Released</td>\s*<td[^>]*>[\s\S]*?((?:19|20)\d{2})', html)
            if rm:
                release_date = rm.group(1)

    elif "itch.io" in url:
        # itch.io: prefer the full description from formatted_description user_formatted
        # over the short OG/description meta tagline.
        fd = re.search(
            r'<div\s+class="formatted_description\s+user_formatted"[^>]*>\s*(.*?)\s*</div>\s*</div>\s*</div>',
            html, re.I | re.S,
        )
        if fd:
            desc_raw = re.sub(r'<[^>]+>', ' ', fd.group(1))
            desc_raw = re.sub(r'&nbsp;', ' ', desc_raw)
            desc_raw = re.sub(r'&#x27;', "'", desc_raw)
            desc_raw = re.sub(r'\s+', ' ', desc_raw).strip()
            if desc_raw and len(desc_raw) > 20:
                description = desc_raw[:600]

        # itch.io: extract developer from "Title by DeveloperName" pattern
        if not developer:
            im = re.search(r' \b[bB]y\s+([A-Za-z0-9][A-Za-z0-9_. -]{1,40})$', title)
            if im:
                developer = im.group(1).strip()
                # Remove " by Developer" from title
                title = re.sub(r'\s+by\s+[A-Za-z0-9][A-Za-z0-9_. -]{1,40}$', '', title).strip()

        # itch.io: extract genre(s) + tags from the game info panel
        ip = re.search(
            r'<div[^>]*class="[^"]*game_info_panel_widget[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            html, re.I | re.S,
        )
        if ip:
            panel = ip.group(1)
            # Genre row
            gm = re.search(r'<tr>\s*<td>\s*Genre\s*</td>\s*<td>(.*?)</td>', panel, re.I | re.S)
            if gm:
                g = re.sub(r'<[^>]+>', ' ', gm.group(1)).strip()
                g = re.sub(r'\s+', ' ', g).strip()
                if g and g not in genres:
                    genres.append(g)
            # Tags row — comma-separated values from <a> tags
            tm = re.search(r'<tr>\s*<td>\s*Tags?\s*</td>\s*<td>(.*?)</td>', panel, re.I | re.S)
            if tm:
                tag_html = tm.group(1)
                for a in re.finditer(r'<a[^>]*>([^<]+)</a>', tag_html):
                    t = a.group(1).strip()
                    if t and t not in genres:
                        genres.append(t)
            # Author row as fallback for developer
            if not developer:
                am = re.search(r'<tr>\s*<td>\s*Author\s*</td>\s*<td>\s*<a[^>]*>\s*([^<]+)\s*</a>', panel, re.I)
                if am:
                    developer = am.group(1).strip()
            # Release date from "Published" or "Updated" row
            if not release_date:
                dm = re.search(r'<tr>\s*<td>\s*(?:Published|Updated)\s*</td>\s*<td[^>]*>\s*<abbr[^>]*title="([^"]+)"', panel, re.I)
                if dm:
                    rd = dm.group(1).strip()
                    ym = re.search(r'(\d{4})', rd)
                    if ym:
                        release_date = ym.group(1)

    # Publisher is only a fallback for developer (we don't expose it separately)
    if not developer and publisher:
        developer = publisher

    # Forum thread pages label everything inside the first post — split it
    # into proper fields. Description = the Overview segment only (text after
    # Overview: until the next labelled field such as Release Date /
    # Developer / Version), never the field block itself.
    #
    # The labelled header lives IN FULL in the first post's BODY; the
    # og:description is only a truncated preview of it, so labels that fall
    # beyond the preview cut (often Tags:/Developer:) never reach the parser
    # from the meta tag alone. Read the first message body on the page, tags
    # stripped, entities decoded. Fallback order: full post body →
    # raw og:description → whatever description another origin (JSON-LD,
    # site-specific extractor) produced. Only fills fields that a
    # higher-quality source didn't already provide.
    _post_text = ""
    _post_html = ""
    _pm = re.search(
        r'<article[^>]+class=["\'][^"\']*message-body[^"\']*["\'][^>]*>([\s\S]*?)</article>',
        html, re.IGNORECASE,
    ) or re.search(
        r'<div[^>]+class=["\'][^"\']*\bbbWrapper\b[^"\']*["\'][^>]*>([\s\S]{0,20000})',
        html, re.IGNORECASE,
    )
    if _pm:
        _post_html = _pm.group(1)
        # Spoiler-aware flattening. The usual thread shape is
        #     <b>Genre</b>: <div class=bbCodeSpoiler>[button "Spoiler"]
        #     <div class=bbCodeSpoiler-content>2D Game, 2DCG, …</div></div>
        # — the label is real text and the list is in the served HTML
        # (hidden only by CSS/JS), but the spoiler BUTTON's own caption
        # ("Spoiler") lands between them after tag-stripping ("Genre :
        # Spoiler 2D Game, …") and pollutes the first tag. Replace each
        # spoiler button with nothing — or with its title as a "Title: "
        # label when it carries one (bbCodeSpoiler-button-title) — so the
        # labelled parse reads straight into the spoiler's content and
        # extracts EXACTLY the labelled fields, nothing else.
        # Same treatment for HTML5 <details><summary> spoilers.
        def _spoiler_btn(m):
            _tm = re.search(
                r'bbCodeSpoiler-button-title[^>]*>\s*([^<]{1,60}?)\s*</span>',
                m.group(0), re.IGNORECASE)
            if _tm:
                return f' {_tm.group(1).strip().rstrip(":")}: '
            return ' '
        _ph = re.sub(
            r'<button[^>]+class=["\'][^"\']*bbCodeSpoiler-button[^"\']*["\']'
            r'[\s\S]*?</button>',
            _spoiler_btn, _post_html, flags=re.IGNORECASE)
        _ph = re.sub(r'<summary[^>]*>\s*([^<]{1,60}?)\s*</summary>',
                     lambda m: f' {m.group(1).strip().rstrip(":")}: ',
                     _ph, flags=re.IGNORECASE)
        _pt = re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>', ' ', _ph)
        _pt = re.sub(r'<[^>]+>', ' ', _pt)
        _post_text = re.sub(r'\s+', ' ', _decode_entities(_pt)).strip()

    _forum = (_parse_forum_description(_post_text)
              or _parse_forum_description(_raw_description))
    if not _forum and description and description != _raw_description[:600]:
        _forum = _parse_forum_description(description)
    if _forum:
        # Overview is already bounded by the next labelled field — keep it
        # intact (do not clip to the generic OG 600-char preview budget).
        _ov = (_forum.get("overview") or "").strip()
        # Drop zero-width leftovers from &#8203; / spoiler markers.
        _ov = re.sub(r'[\u200b\u200c\u200d\ufeff]+', '', _ov).strip()
        if _ov:
            description = _ov
        developer = developer or _forum.get("developer", "")
        _forum_rd = _forum.get("release_date", "") or ""
        # Release Date on these threads can mean commercial launch OR the
        # current-build publish date; datePublished is the thread's original
        # post. Whichever comes first chronologically is the year we keep.
        _picked = _earliest_forum_date(_forum_rd, *(ld_forum_dates or []))
        if _picked:
            release_date = release_date or _picked
        if not genres and _forum.get("tags"):
            genres = _forum["tags"]
    elif ld_forum_dates and not release_date:
        release_date = ld_forum_dates[0]

    # Last resort for forum threads: the thread's native tag list (XenForo
    # "tagItem" anchors under the title). It lives OUTSIDE the post body —
    # never inside a spoiler — so it survives even when the header keeps
    # its Genre list in a shape the labelled parse can't read.
    if not genres:
        _seen_t: set[str] = set()
        for _tm in re.finditer(
                r'<a[^>]+class=["\'][^"\']*\btagItem\b[^"\']*["\'][^>]*>([^<]{1,40})</a>',
                html, re.IGNORECASE):
            _tag = _decode_entities(_tm.group(1)).strip()
            if _tag and _tag.casefold() not in _seen_t:
                _seen_t.add(_tag.casefold())
                genres.append(_tag)
            if len(genres) >= 16:
                break

    # Safety net: whatever path produced the description, a leftover leading
    # "Overview:" / "Description:"-style label is never part of the text.
    if description:
        description = re.sub(
            r'^\s*(?:overview|description|story|synopsis|plot)\s*:\s*',
            '', description, flags=re.IGNORECASE)

    # Forum threads: the game's real store page is usually linked from the
    # first post ("Store"/"Shop"/"Buy"/… anchors, or a bare link to a known
    # store domain). Use that as the store_url — the thread URL itself only
    # as fallback when no such link is present — and keep the thread URL as
    # an extra site page so its chip survives in the dialog. Only EXTERNAL
    # links count: forum-internal/masked (registration-gated) hrefs point
    # back at the forum host and are never a store page.
    _store_link = ""
    _store_extra: list[str] = []
    if _post_html and _forum:
        _page_host = urllib.parse.urlsplit(url).netloc.lower()
        _seen_links: set[str] = set()
        for _am in re.finditer(
                r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                _post_html, re.IGNORECASE):
            _href = _am.group(1).strip()
            _atext = re.sub(r'<[^>]+>', ' ', _am.group(2))
            _host = urllib.parse.urlsplit(_href).netloc.lower()
            if not _host or _host == _page_host or _href in _seen_links:
                continue
            if _STORE_LINK_HOST_RE.search(_host) or _STORE_LINK_TEXT_RE.search(_atext):
                _seen_links.add(_href)
                if not _store_link:
                    _store_link = _href
                elif len(_store_extra) < 4:
                    _store_extra.append(_href)

    # Derive source from URL for known sites
    _source = "web"
    _url_lower = url.lower()
    if "dlsite.com" in _url_lower:
        _source = "dlsite"
    elif "itch.io" in _url_lower:
        _source = "itch"
    elif "mobygames.com" in _url_lower:
        _source = "mobygames"
    elif "wikipedia.org" in _url_lower:
        _source = "wikipedia"
    elif "vndb.org" in _url_lower:
        _source = "vndb"

    # DLsite: still no real title after OG/JSON-LD/itemprop → not a candidate.
    if _source == "dlsite" and not (title or "").strip():
        logger.info(f"DLSite: no usable title for {url}")
        return None

    # JSON-LD aggregate → a single-verdict review for sites that publish one
    # (itch ratings, MobyScore, …). DLsite's user-review list is richer and
    # is attached just below; the aggregate is only the fallback there.
    # itch.io: "itch.io: 4.6/5 (1575)" with vote_count — not one fake entry.
    _rating = 0.0
    _reviewer = ""
    _review_text = ""
    _vote_count = 0
    if ld_rating and _source != "dlsite":
        from core.library import quantize_rating
        _rating = quantize_rating(ld_rating)
        _reviewer = ld_reviewer or source_label(_source) or _source
        _vote_count = int(ld_vote_count or 0)
        if _source == "itch":
            _review_text = f"{_reviewer}: {_rating:g}/5"
            if _vote_count:
                _review_text = f"{_review_text} ({_vote_count})"
        else:
            _review_text = (f"{_reviewer}: {ld_review_text}"
                            if ld_review_text else f"{_reviewer}: {_rating:g}/5")

    info = GameInfo(
        name=title,
        description=description,
        image_url=image_url,
        release_date=release_date,
        genres=genres[:16],
        developer=developer,
        publisher='',
        store_url=_store_link or url,
        source=_source,
        extra_urls=([url] + _store_extra) if _store_link else [],
        rating=_rating,
        reviewer=_reviewer,
        review_text=_review_text,
        vote_count=_vote_count if _rating else 0,
    )
    # DLsite user reviews live behind a Vue stub on the work page; every
    # dlsite.com scrape (locale redirect, direct opengraph, pasted link)
    # has to ask the review API, or the score never reaches the form.
    if _source == "dlsite":
        _attach_dlsite_reviews(info, url, html=html)
    elif (_forum_reviews_html or _forum
          or _FORUM_REVIEWS_HREF_RE.search(html or "")):
        # XenForo product threads that split Reviews onto a sibling tab:
        # follow that tab (or use the pasted tab body) and import the
        # user reviews as generic "web" entries — never as a named source.
        _attach_forum_reviews(info, url, html,
                              reviews_html=_forum_reviews_html)
    return info


# ── Search-engine access layer ────────────────────────────────────────────────
# Every scraped engine eventually rate-limits or challenges an IP that sends
# bursts of queries (DuckDuckGo returns its HTTP 202 "anomaly" page, most
# commercial engines serve captchas). All engine traffic therefore goes
# through one shared layer that (a) spaces requests to the same engine,
# (b) puts an engine that signalled a block into a cooldown instead of
# hammering it, and (c) logs every outcome.
#
# Engine lineup, tried in this order per query (consumers break as soon as
# they extracted what they need, so a healthy earlier engine short-circuits
# the rest):
#   1. Brave  — best recall, and the only engine that reliably surfaces niche
#               (indie/adult, versioned) titles. Answers residential clients
#               but returns 429/challenge to datacenter/CI IPs, so it can look
#               dead from a build server; its 429 is a stochastic burst limit,
#               not a durable block (see the branch — it is retried, never
#               cooled down on a 429).
#   2. Bing   — classic server-rendered SERP (via a minimal, non-Chrome UA —
#               see _bing_http_get); result links are /ck/a?…u=a1<b64>, decoded
#               downstream by _unwrap_redirect. Reliable and captcha-free, but
#               its index is weak on niche titles, so it mainly backs up Brave
#               for mainstream queries.
#   3. SearXNG— best-effort meta-search over a small pool of public instances,
#               each response health-checked (_searx_page_relevant rejects the
#               homepage/decoy pages these instances often serve). No hardcoded
#               instance list stays healthy for long — this is a fallback, and
#               Bing carries the mainstream load that used to rest here.
#   4. DDG    — LAST: largely redundant with Bing (whose index it draws on) and
#               it 202-blocks aggressively, so it is deprioritized but kept for
#               the IPs where it is the one engine still answering. On IPs DDG
#               has classified as bot traffic ("cc=botnet" in its challenge
#               form) EVERY variant — html, lite, the SPA's d.js API — serves
#               the duck captcha, so there is no endpoint fallback; repeated
#               blocks escalate to the long hard cooldown instead.
#
# Mojeek was removed: it serves an anti-bot captcha (HTTP 200) from every IP
# tried, so it only ever cost a throttled request and contributed nothing.
#
# Brave and Bing were dropped in an earlier build after they 429'd "nearly
# every scripted request" — but that was measured from a datacenter/CI IP,
# which they block far more aggressively than the residential IPs this app
# actually runs on, where they answer normally. They are back for that reason;
# the request shapes (UA-only for Brave, minimal-UA for Bing) match what worked
# before. Each engine has its OWN throttle/cooldown bucket, so one engine's
# block never sidelines the others — combined with the one-probe-per-phase rule
# below, that is what keeps a later tier searchable after an earlier tier
# tripped a block on some engines.

_ENGINE_MIN_INTERVAL = {          # seconds between requests to the same engine
    "brave": 3.0,
    "bing": 2.0,
    "searxng": 2.0,
    "ddg": 3.0,
}
_ENGINE_NAMES = tuple(_ENGINE_MIN_INTERVAL)
# Sit out after a block signal (429/captcha/anomaly). In practice this now
# bounds the block WITHIN a tier only: _engine_new_search_phase() lifts every
# cooldown at each tier boundary, so a block never carries into the next tier.
# The value just has to outlast a single tier's queries (seconds); 300s does.
_ENGINE_COOLDOWN_S = 300.0
# Durable-wall escalation: an engine that serves a block signal on EVERY
# attempt (observed 2026-07: DuckDuckGo answering 202 + duck-captcha with a
# "cc=botnet" IP classification on every endpoint variant — html, lite,
# d.js — regardless of UA/method) will never recover within a session, yet
# the per-phase probe kept spending one request + min-interval sleep on it
# every tier, forever. After _ENGINE_HARD_BLOCK_STRIKES consecutive blocks
# with no success in between, the engine sits out _ENGINE_HARD_COOLDOWN_S —
# a window that survives phase resets. Any real answer clears the strikes,
# so an engine that was merely rate-limited still comes back on its own.
_ENGINE_HARD_BLOCK_STRIKES = 3
_ENGINE_HARD_COOLDOWN_S = 3600.0
_engine_lock = threading.Lock()
_engine_state: dict[str, dict] = {}   # engine → {"last","blocked_until","probe_gen","strikes","hard_until"}
_probe_generation = 0             # bumped by _engine_new_search_phase()


def _engine_entry(engine: str) -> dict:
    return _engine_state.setdefault(
        engine, {"last": 0.0, "blocked_until": 0.0, "probe_gen": -1,
                 "strikes": 0, "hard_until": 0.0}
    )


def _engine_new_search_phase():
    """Mark the start of a user-facing search phase (targeted / generic tier).

    Every engine's cooldown is LIFTED here, so a block picked up in an earlier
    tier never sidelines that engine for the next tier: each tier retries all
    engines from scratch, matching the pre-cooldown build's per-tier freshness.
    Within a tier the block still applies (an engine that signalled a block is
    skipped for the rest of THAT tier — see _engine_wait_slot), so this frees
    later tiers without re-hammering an engine inside one. The throttle
    (min-interval spacing) is untouched — that is the "queue" protection.

    This is the mechanism behind "don't block all tiers": the pure-engine
    generic tier (tier 3) always gets a fresh shot even when the targeted tier
    (tier 2) just blocked every engine.
    """
    global _probe_generation
    with _engine_lock:
        _probe_generation += 1
        for st in _engine_state.values():
            st["blocked_until"] = 0.0
        # NB: hard_until (the durable-wall sit-out) is deliberately NOT
        # cleared — an engine that blocked every attempt stays out across
        # phases until its long cooldown expires or it answers again.
        # NB: SearXNG per-instance cooldowns are deliberately NOT cleared here.
        # Lifting the "searxng" engine cooldown already gives a later tier a
        # fresh probe; keeping the per-instance blocks lets that probe advance
        # to UNTRIED instances in the (large searx.space) pool instead of
        # re-hitting the same dead ones each tier.


def _engine_wait_slot(engine: str) -> bool:
    """Reserve a request slot for *engine*: False while it cools down after a
    block (except for the phase's single probe), otherwise sleeps out the
    remaining min-interval and returns True."""
    import time as _time
    min_iv = _ENGINE_MIN_INTERVAL.get(engine, 2.0)
    with _engine_lock:
        st = _engine_entry(engine)
        now = _time.time()
        if now < st["hard_until"]:
            return False              # durably walled — no per-phase probe either
        if now < st["blocked_until"]:
            if st["probe_gen"] == _probe_generation:
                return False          # already probed (and re-blocked) this phase
            st["probe_gen"] = _probe_generation
            logger.info(f"Search engine {engine} in cooldown — probing once this phase")
        wait = max(0.0, st["last"] + min_iv - now)
        # Reserve the slot before sleeping so concurrent searches space out too.
        st["last"] = now + wait
    if wait > 0:
        _time.sleep(wait)
    return True


def _engine_mark_ok(engine: str):
    """The engine answered with a real page: lift any active cooldown."""
    import time as _time
    with _engine_lock:
        st = _engine_entry(engine)
        if st["blocked_until"] > _time.time() or st["hard_until"] > _time.time():
            logger.info(f"Search engine {engine} recovered — cooldown lifted")
        st["blocked_until"] = 0.0
        st["hard_until"] = 0.0
        st["strikes"] = 0


def _engine_block(engine: str, reason: str):
    """Put *engine* into cooldown after it signalled rate-limiting/challenge.
    Consecutive blocks with no success in between escalate to the long
    hard cooldown (see the durable-wall note by _ENGINE_HARD_COOLDOWN_S)."""
    import time as _time
    with _engine_lock:
        st = _engine_entry(engine)
        st["blocked_until"] = _time.time() + _ENGINE_COOLDOWN_S
        # The block consumes this phase's probe allowance too: later queries
        # of the SAME phase skip the engine outright; the next phase probes.
        st["probe_gen"] = _probe_generation
        st["strikes"] += 1
        strikes = st["strikes"]
        hard = strikes >= _ENGINE_HARD_BLOCK_STRIKES
        if hard:
            st["hard_until"] = _time.time() + _ENGINE_HARD_COOLDOWN_S
    if hard:
        logger.info(
            f"Search engine {engine} blocked us ({reason}) — "
            f"{strikes} blocks in a row with no success, sitting out "
            f"for {_ENGINE_HARD_COOLDOWN_S / 60:.0f} min"
        )
    else:
        logger.info(
            f"Search engine {engine} blocked us ({reason}) — "
            f"cooling down for {_ENGINE_COOLDOWN_S / 60:.0f} min"
        )


def engines_blocked_status() -> tuple[int, int, int]:
    """(engines currently in cooldown, total engines, minutes until the
    soonest blocked one may be retried). Lets the UI say "the search
    engines are rate-limiting us, retry in ~N min" instead of a generic
    "no results" when a search found nothing because of a total blackout."""
    import math
    import time as _time
    now = _time.time()
    blocked = 0
    soonest = 0.0
    with _engine_lock:
        for name in _ENGINE_NAMES:
            st = _engine_entry(name)
            remaining = max(st["blocked_until"], st["hard_until"]) - now
            if remaining > 0:
                blocked += 1
                soonest = min(soonest, remaining) if blocked > 1 else remaining
    minutes = max(1, math.ceil(soonest / 60.0)) if blocked else 0
    return blocked, len(_ENGINE_NAMES), minutes


def _http_get(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET *url* with browser-like headers. Returns (status, html).
    HTTPError is returned as its status code (not raised); other errors bubble."""
    req = urllib.request.Request(url, headers=dict(_DEFAULT_HEADERS))
    try:
        with _open_url(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body


def _brave_http_get(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET Brave Search with a UA-only header set. Returns (status, html).

    Mirrors the request shape the prior build used successfully — the default
    Accept-* headers are omitted deliberately to match it. Brave answers
    residential clients but returns 429/challenge pages to datacenter/CI IPs;
    a block here just cools Brave down and the chain falls through to Bing.
    HTTPError is returned as its status code (not raised)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": _DEFAULT_HEADERS["User-Agent"]})
    try:
        with _open_url(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body


# Bing serves a JS-rendered SPA shell (no result links in the static HTML) to
# modern-Chrome User-Agents, but the classic server-rendered SERP — organic
# results in <li class="b_algo"> with bing.com/ck/a?…u=a1<b64> links that
# _unwrap_redirect decodes — to a plain UA WITHOUT the "Chrome" token. So Bing
# is fetched with a deliberately minimal UA (this is the approach an earlier
# build used; the current default Chrome UA silently got the linkless shell).
_BING_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Retry UA: Firefox also gets the classic server-rendered SERP (verified
# 2026-07: 200 + 10 b_algo blocks). When the first attempt draws the linkless
# shell / consent wall, redrawing with the SAME UA tends to hit the same
# variant — the retry switches UA to get an independent draw instead.
# (The format=rss endpoint is NOT a fallback: it silently truncates the query
# to roughly its first word and returns dictionary/unrelated links.)
_BING_UA_RETRY = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                  "Gecko/20100101 Firefox/128.0")


def _bing_http_get(url: str, timeout: int = 12, ua: str = _BING_UA) -> tuple[int, str]:
    """GET Bing with a UA that yields the classic (non-JS) SERP.
    Returns (status, html); HTTPError becomes its status code."""
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with _open_url(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception:
            return e.code, ""


# SearXNG instances are IMPORTED from searx.space (the public instance tracker)
# rather than hardcoded, so the pool self-updates instead of rotting. A tiny
# seed is kept only as the offline fallback when searx.space is unreachable.
# IMPORTANT: searx.space's own health grade is NOT our client's experience — an
# instance it rates 100% may still serve US its homepage or a decoy SERP — so
# _searx_page_relevant() vets every response regardless (it rejects any page
# without the query's own tokens). The import only picks a live, search-capable,
# non-Tor starting pool; the guard does the trusting. SearXNG stays best-effort:
# Bing now carries the mainstream load that used to rest here.
_SEARX_SPACE_URL = "https://searx.space/data/instances.json"
_SEARX_SEED = (
    "https://search.rhscz.eu",
    "https://etsi.me",
    "https://ooglester.com",
)
# Cap per query — NOT "try the whole pool". Public instances are mostly
# bot-walled (homepage decoy / Anubis / 403); a live probe (2026-08) found
# ~2/47 usable from this IP. With a low cap the unlucky shuffle never
# reaches those two and the old code then engine-blocked SearXNG for 5 min,
# freezing the remaining pool mid-tier. Cap is high enough to usually hit a
# live instance; untried instances stay available for the next query (see
# _iter_search_engine_html — engine cooldown only when the pool is exhausted).
_SEARX_MAX_TRY = 16
_searx_pool_cache: Optional[list[str]] = None


def _searx_instances() -> list[str]:
    """Healthy public SearXNG instances from searx.space, cached per process.

    Filters to normal-network (non-Tor), HTTP-200, search-capable instances and
    shuffles them to spread load; falls back to _SEARX_SEED if searx.space is
    unreachable. Callers MUST still validate each response — searx.space's grade
    is not our client's success."""
    global _searx_pool_cache
    if _searx_pool_cache is not None:
        return _searx_pool_cache
    pool: list[str] = []
    try:
        status, body = _http_get(_SEARX_SPACE_URL, timeout=10)
        if status == 200 and body:
            for url, s in (json.loads(body).get("instances") or {}).items():
                if not url.startswith("https://"):     # scheme guard
                    continue
                if not isinstance(s, dict) or s.get("network_type") != "normal":
                    continue
                if (s.get("http") or {}).get("status_code") != 200:
                    continue
                search = (s.get("timing") or {}).get("search") or {}
                if (search.get("success_percentage") or 0) < 95:
                    continue
                pool.append(url.rstrip("/"))
    except Exception as e:
        logger.debug(f"searx.space instance list unavailable: {e}")
    if pool:
        import random
        random.shuffle(pool)                 # spread load across the pool
        src = "searx.space"
    else:
        pool = list(_SEARX_SEED)
        src = "seed"
    _searx_pool_cache = pool
    logger.info(f"SearXNG pool: {len(pool)} instances ({src})")
    return _searx_pool_cache
_SEARX_INSTANCE_COOLDOWN_S = 1800.0   # failed instance sit-out
_searx_instance_blocked: dict[str, float] = {}   # base_url → blocked_until
_searx_sticky: list = [None]   # last instance that worked this process — tried first
# link_token ping cache: base → unix time until which the CSS handshake is
# still valid (SearXNG PING_LIVE_TIME is 3600s; renew a bit sooner).
_searx_warmed_until: dict[str, float] = {}
_SEARX_WARM_TTL_S = 3000.0

# SearXNG botdetection (docs.searxng.org/src/searx.botdetection.html):
#   • http_accept_encoding — needs gzip AND deflate (urllib defaults to
#     identity → bot)
#   • http_connection — Connection: close → 429 (urllib adds close unless
#     we override with keep-alive)
#   • http_sec_fetch — Mode navigate|cors, Dest document|empty, Site
#     same-origin|same-site|none; invalid → 302 to index
#   • link_token — without a prior GET of /client<token>.css the IP is
#     "suspicious"; after a few hits /search is 302'd to the homepage
#     (the decoy we kept seeing). Browser loads that CSS from the index
#     page automatically; we must do the same handshake.
# gzip/deflate is decompressed manually (urllib won't).
_SEARX_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Markers of anti-bot interstitials some instances put in front of SearXNG
# (Anubis proof-of-work, "checking request" pages) — no organic links inside.
_SEARX_CHALLENGE_RE = re.compile(
    r"(making sure you're not a bot|checking request|anubis|proof-of-work"
    r"|verification could not run)", re.IGNORECASE)

# link_token stylesheet embedded in the instance index page.
_SEARX_CLIENT_CSS_RE = re.compile(
    r"""(?:href|src)=["']([^"']*client[^"']*\.css[^"']*)["']""",
    re.IGNORECASE,
)


def _searx_http_get(
    url: str,
    timeout: int = 12,
    extra_headers: dict | None = None,
) -> tuple[int, str]:
    """GET *url* with the headers SearXNG's bot detection requires.
    Returns (status, html); HTTPError becomes its status code."""
    import gzip
    import zlib

    def _decode(resp) -> str:
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in enc:
                data = gzip.decompress(data)
            elif "deflate" in enc:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        except Exception:
            pass
        return data.decode("utf-8", errors="replace")

    hdrs = {**_DEFAULT_HEADERS, **_SEARX_HEADERS}
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with _open_url(req, timeout=timeout) as r:
            return r.status, _decode(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode(e)
        except Exception:
            return e.code, ""


def _searx_link_token_warmup(base: str, timeout: int = 8) -> bool:
    """Homepage + /client<token>.css ping so link_token stops decoying /search.

    Returns True when the instance looks usable afterward (or has no
    link_token at all). False on hard failure to even load the index.
    Cached per base for ``_SEARX_WARM_TTL_S`` so a sticky instance does not
    re-handshake on every query.
    """
    import time as _time
    now = _time.time()
    with _engine_lock:
        if now < _searx_warmed_until.get(base, 0.0):
            return True
    try:
        status, body = _searx_http_get(
            base + "/",
            timeout=timeout,
            extra_headers={"Sec-Fetch-Site": "none"},
        )
    except Exception:
        return False
    if status != 200 or not body:
        return False
    if _SEARX_CHALLENGE_RE.search(body[:4000]):
        return False
    m = _SEARX_CLIENT_CSS_RE.search(body)
    if m:
        css_url = urllib.parse.urljoin(base + "/", m.group(1))
        try:
            # Dest must be document|empty for http_sec_fetch; browsers use
            # style, but that value is rejected and 302'd back to index.
            _searx_http_get(
                css_url,
                timeout=timeout,
                extra_headers={
                    "Accept": "text/css,*/*;q=0.1",
                    "Referer": base + "/",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
        except Exception:
            pass  # ping best-effort; search will tell us if it worked
    with _engine_lock:
        _searx_warmed_until[base] = _time.time() + _SEARX_WARM_TTL_S
    return True


def _searx_page_relevant(html: str, query: str) -> bool:
    """True when the organic results actually relate to *query*.

    Several public instances anti-scrape by serving a real-looking results
    page whose entries are random cached SERPs for OTHER queries (observed
    2026-07: a game-title query answered with car-dealer and tax-office links).
    Accepting those would poison downstream metadata scraping, so require at
    least one significant query token inside the result <article> blocks.
    The check is scoped to the articles because the search box echoes the
    query, making a whole-page substring test always pass.
    """
    articles = re.findall(r'<article[\s\S]*?</article>', html)
    if not articles:
        return False                  # zero organic results → nothing usable
    tokens = {
        t for t in re.findall(r'[a-z0-9]{4,}', query.lower())
        if t not in ("site", "game", "https", "http")
    }
    if not tokens:
        return True                   # query too short to judge — accept
    joined = " ".join(articles).lower()
    return any(t in joined for t in tokens)


def _searx_fetch_one(base: str, q: str, query: str, timeout: int = 8) -> tuple[str, Optional[str]]:
    """Warm link_token, then ONE HTML search against *base*.

    Returns (kind, html):
      ('ok', html)    — usable results page (query tokens in <article>s).
      ('empty', None) — instance answered a real results page with zero hits
                        (``no results`` / no articles after a non-decoy SERP).
                        Callers must NOT punish it — that used to cool the
                        instance for 30 min and starve the pool.
      ('bad', None)   — error / challenge / homepage decoy / irrelevant SERP.

    ``format=json`` is intentionally NOT used: ip_limit.API_MAX is ~4/hour
    per IP on many public instances, so JSON burns the quota across a pool
    walk. HTML after the link_token handshake is what browsers do and what
    clears the homepage-decoy redirect.
    """
    if not _searx_link_token_warmup(base, timeout=timeout):
        return "bad", None
    try:
        # same-origin + Referer: we "navigated" from the index after the
        # CSS ping — matches a real browser session on this instance.
        status, body = _searx_http_get(
            f"{base}/search?q={q}",
            timeout=timeout,
            extra_headers={
                "Referer": base + "/",
                "Sec-Fetch-Site": "same-origin",
            },
        )
    except Exception:
        return "bad", None
    if status != 200 or not body:
        return "bad", None
    if _SEARX_CHALLENGE_RE.search(body[:4000]):
        return "bad", None
    # Homepage decoy: link_token redirect lands on index with no <article>.
    if _searx_page_relevant(body, query):
        return "ok", body
    # Real empty SERP (search UI present, zero organic hits) vs decoy.
    if re.search(r"no results|nothing found|0 results", body[:8000], re.I):
        return "empty", None
    articles = re.findall(r'<article[\s\S]*?</article>', body)
    if not articles and ('name="q"' in body or "search_url" in body):
        return "empty", None
    return "bad", None


def _searx_pool_available() -> tuple[int, int]:
    """Return ``(available_now, pool_size)`` — instances not in per-instance cooldown."""
    import time as _time
    pool = _searx_instances()
    now = _time.time()
    with _engine_lock:
        avail = sum(
            1 for b in pool
            if now >= _searx_instance_blocked.get(b, 0.0)
        )
    return avail, len(pool)


def _searx_fetch(q: str, query: str):
    """Return (instance_base, html) from ONE working SearXNG instance,
    the string "empty" when at least one instance answered validly but had
    no results for the query, or None when every probed instance was
    unusable (error/challenge/decoy).

    "empty" vs None matters to the caller: a valid empty answer means the
    engine is HEALTHY and must not be put in the 5-min engine cooldown —
    the query simply has no hits.

    Politeness is the point — public instances challenge/ban clients that probe
    hard, so a multi-query search must not hammer the pool. Therefore:
      1. reuse the last instance that worked this process (sticky) FIRST, so a
         whole search rides one instance instead of probing fresh ones per query;
      2. exactly ONE request per instance (see _searx_fetch_one);
      3. at most _SEARX_MAX_TRY non-cooled instances before giving up
         (failed ones stay cooled; the next query walks further into the pool)."""
    import time as _time
    order: list[str] = []
    _s = _searx_sticky[0]
    if _s:
        order.append(_s)
    for base in _searx_instances():
        if base not in order:
            order.append(base)
    tried = 0
    n_bad = 0
    first_empty: Optional[str] = None
    for base in order:
        if tried >= _SEARX_MAX_TRY:
            break
        with _engine_lock:
            if _time.time() < _searx_instance_blocked.get(base, 0.0):
                continue
        tried += 1
        kind, html = _searx_fetch_one(base, q, query)
        if kind == "ok":
            _searx_sticky[0] = base          # remember the winner for next query
            return base, html
        if kind == "empty":
            # The instance works — a no-results answer is not a failure, so
            # no instance cooldown and no sticky drop. Keep probing the
            # remaining budget: instances enable different upstream engines,
            # so another one may still have hits for this query.
            logger.debug(f"searxng {base} answered empty for {query!r}")
            first_empty = first_empty or base
            continue
        n_bad += 1
        logger.debug(f"searxng {base} unusable for {query!r}")
        with _engine_lock:
            _searx_instance_blocked[base] = _time.time() + _SEARX_INSTANCE_COOLDOWN_S
        if _searx_sticky[0] == base:
            _searx_sticky[0] = None          # sticky went bad — drop it
    if first_empty:
        if not _searx_sticky[0]:
            _searx_sticky[0] = first_empty   # a healthy instance beats none
        return "empty"
    if tried:
        avail, pool_n = _searx_pool_available()
        logger.info(
            f"searxng: probed {tried} instance(s) for {query!r} "
            f"({n_bad} unusable); {avail}/{pool_n} still available in pool"
        )
    return None


def _iter_search_engine_html(query: str):
    """Yield (engine_name, html) for *query* from each usable engine in turn.

    Consumers loop and break as soon as they extracted what they need, so
    healthy engines cost one request and blocked ones are skipped via their
    cooldown. Order: Brave, then Bing (classic SERP via a minimal UA), then
    the SearXNG instance pool (meta-search — keeps working through a Brave/Bing
    cooldown), then DuckDuckGo last. See the lineup note above.
    """
    q = urllib.parse.quote(query)

    # Brave Search first — short-circuits the rest when it answers. Brave's
    # 429 is a stochastic short-window burst limit, NOT a durable block:
    # observed on the same IP, a 200 with full results and a 429 two seconds
    # apart. So a 429 never cools Brave down — every query re-tries it (one
    # quick in-query retry after 2s, then the min-interval throttle paces the
    # cross-query attempts), which is exactly how the prior build kept
    # recovering on IPs where every other engine is walled off. A 403 (a real
    # wall) does cool it down for the rest of the tier.
    if _engine_wait_slot("brave"):
        import time as _time
        for _attempt in (0, 1):
            try:
                status, html = _brave_http_get(f"https://search.brave.com/search?q={q}")
            except Exception as e:
                logger.debug(f"brave fetch failed for {query!r}: {e}")
                break
            if status == 200 and html:
                logger.debug(f"brave answered {query!r} ({len(html)}B)")
                _engine_mark_ok("brave")
                yield "brave", html
                break
            if status == 403:
                _engine_block("brave", "HTTP 403")
                break
            if status == 429 and _attempt == 0:
                _time.sleep(2)          # burst limit — one quick fresh draw
                continue
            if status == 429:
                logger.info(f"brave 429 for {query!r} — will retry on the next query")
            break

    # Bing — classic server-rendered SERP via the minimal UA (see the note by
    # _bing_http_get). Result links are bing.com/ck/a?…u=a1<b64>, decoded
    # downstream by _unwrap_redirect. Own cooldown bucket, separate from Brave.
    if _engine_wait_slot("bing"):
        try:
            # mkt/setlang nudge Bing to the en-US market so it serves the
            # classic SERP and skips the EU region-consent interstitial more
            # often (games are searched in English anyway).
            bing_url = f"https://www.bing.com/search?q={q}&mkt=en-US&setlang=en-US"
            status, html = _bing_http_get(bing_url)
            # Bing occasionally A/Bs the linkless JS shell even for this UA;
            # the classic SERP always carries "b_algo" result blocks. Retry
            # once with the Firefox UA — a same-UA redraw tends to serve the
            # same shell again (see _BING_UA_RETRY).
            if status == 200 and html and "b_algo" not in html:
                status, html = _bing_http_get(bing_url, ua=_BING_UA_RETRY)
            if status == 200 and html and "b_algo" in html:
                logger.debug(f"bing answered {query!r} ({len(html)}B)")
                _engine_mark_ok("bing")
                yield "bing", html
            elif status in (403, 429):
                _engine_block("bing", f"HTTP {status}")
            elif status == 200 and html:
                # 200 but no organic-results block — a consent/region wall or
                # the linkless JS shell. This was silent before; surface it so
                # "Bing returned nothing" is visible in the run log.
                logger.info(f"bing served a non-results page for {query!r} "
                            f"(no b_algo: consent/region wall or JS shell)")
        except Exception as e:
            logger.debug(f"bing fetch failed for {query!r}: {e}")

    # SearXNG instance pool (one engine bucket, per-instance failover)
    if _engine_wait_slot("searxng"):
        hit = _searx_fetch(q, query)
        if hit == "empty":
            # A healthy instance answered "no results" — the engine works,
            # so no cooldown; blocking here made a hitless query look like
            # a ban and silenced SearXNG for 5 min despite a live pool.
            logger.debug(f"searxng answered empty for {query!r}")
            _engine_mark_ok("searxng")
        elif hit is not None:
            base, html = hit
            logger.debug(f"searxng ({base}) answered {query!r} ({len(html)}B)")
            _engine_mark_ok("searxng")
            yield "searxng", html
        else:
            # Most of the searx.space list is bot-walled from any given IP;
            # a single query only probes _SEARX_MAX_TRY. Cooling the whole
            # engine here used to freeze the remaining (still-untried)
            # instances for 5 min mid-tier. Only engine-block when the pool
            # is actually exhausted — otherwise the next query walks on.
            avail, pool_n = _searx_pool_available()
            if avail <= 0:
                _engine_block(
                    "searxng",
                    f"all {pool_n} instances failed/challenged",
                )
            else:
                logger.info(
                    f"searxng: no usable answer for {query!r} this round "
                    f"({avail}/{pool_n} instances still untried) — "
                    f"not cooling engine"
                )

    # DuckDuckGo static HTML endpoints, LAST: largely redundant with Bing (whose
    # index it draws on) and it 202-blocks aggressively, so a healthy engine
    # above short-circuits it — but it still answers on some IPs where the
    # others are walled, so it is kept rather than dropped.
    if _engine_wait_slot("ddg"):
        for endpoint in (f"https://html.duckduckgo.com/html/?q={q}",
                         f"https://lite.duckduckgo.com/lite/?q={q}"):
            try:
                status, html = _http_get(endpoint)
            except Exception as e:
                logger.debug(f"ddg fetch failed for {query!r}: {e}")
                break
            if status == 202:
                # 202 is DDG's rate-limit/anomaly answer, not a real result
                # page. Detect by status ONLY — the page <title> echoes the
                # query, so text markers would false-positive on games whose
                # name contains words like "anomaly".
                _engine_block("ddg", "HTTP 202 anomaly page")
                break
            if status == 200 and html:
                logger.debug(f"ddg answered {query!r} ({len(html)}B)")
                _engine_mark_ok("ddg")
                yield "ddg", html
                break


def _unwrap_redirect(url: str) -> str:
    """Decode engine redirect links (DuckDuckGo /l/?uddg=…, Bing /ck/a?…u=a1…)."""
    m = re.search(r'[?&]uddg=([^&]+)', url)
    if m:
        return urllib.parse.unquote(m.group(1))
    # Bing wraps results as /ck/a?...&u=a1<base64url> — decode when present
    m = re.search(r'bing\.com/ck/a\?.*?[?&]u=a1([^&]+)', url)
    if m:
        try:
            import base64
            token = m.group(1)
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
    return url


def _web_search_urls(query: str, max_results: int = 6) -> list[str]:
    """Return result URLs from web search engines, excluding YouTube.

    Iterates the shared engine layer (Brave → Bing → SearXNG → DDG)
    and stops as soon as enough *query-relevant* links were extracted.

    Relevance gate: an engine that returns a full SERP of unrelated links
    (common when Bing serves a soft wall / A-B junk for scripted clients)
    must NOT short-circuit the chain — that used to make tier-3 look
    "done" in ~10s after a Brave 429, never reaching SearXNG, while tier-2
    spent minutes on the same engines because ``site:`` queries kept
    Bing on the no-b_algo path.
    """
    # Engine self-links: SearXNG pages link to their own preferences/about
    # pages, searx.space, the SearXNG GitHub/docs, and per-result cache links.
    # Only the seed hosts are listed statically (the dynamic pool's instances
    # mostly use relative nav links); searx\.space/searxng\.org below cover the
    # rest.
    _searx_hosts = "|".join(
        re.escape(urllib.parse.urlsplit(b).netloc) for b in _SEARX_SEED
    )
    _SKIP = re.compile(
        r'(' + _searx_hosts + r'|searx\.space|searxng\.org|github\.com/searxng'
        r'|cache\.google\.com|mojeek\.com|duckduckgo\.com'
        r'|search\.brave\.com|brave\.com'
        r'|google\.com|microsoft\.com|bing\.com'
        r'|w3\.org|schema\.org|wikipedia\.org/wiki/Special'
        r'|web\.archive\.org|translate\.google|msn\.com'
        r'|ad\.doubleclick|cloudflare\.com/cdn'
        r'|buttondown\.email'          # Mojeek footer newsletter link
        r'|youtube\.com|youtu\.be)',
        re.IGNORECASE,
    )
    # Significant query tokens that should appear in a useful result URL
    # (slug/path). Packaging noise and tiny words are ignored — same spirit
    # as _searx_page_relevant / _title_tokens.
    _q_tokens = {
        t for t in re.findall(r'[a-z0-9]{4,}', query.lower())
        if t not in ("site", "game", "https", "http", "html", "www",
                     "com", "org", "net")
    }

    def _resolve(url: str) -> str | None:
        # Unescape &amp; first: Bing wraps results as ck/a?…&amp;u=a1<b64>, and
        # the entity would otherwise break _unwrap_redirect's &u= match.
        u = _unwrap_redirect(url.replace("&amp;", "&"))
        u = urllib.parse.unquote(u).split("&")[0].rstrip("/")
        if u.startswith("http") and not _SKIP.search(u):
            return u
        return None

    def _url_match_strength(url: str) -> int:
        """0 = reject; else count of query tokens present (all required)."""
        if not _q_tokens:
            return 1
        low = url.lower()
        # EVERY significant token must appear — "Super Adventure" must match
        # super+adventure, not super alone (unrelated pages) or adventure alone.
        n = sum(1 for t in _q_tokens if t in low)
        return n if n == len(_q_tokens) else 0

    def _extract_links(html: str, limit: int) -> list[str]:
        seen: dict[str, None] = {}
        # href may be protocol-relative (DuckDuckGo redirect links are
        # //duckduckgo.com/l/?uddg=…) — normalize before resolving.
        for m in re.finditer(
            r'<a[^>]+href="((?:https?:)?//(?:[^/"]+\.)+[a-z]{2,}[^"]*)"',
            html,
        ):
            raw = m.group(1)
            if raw.startswith("//"):
                raw = "https:" + raw
            u = _resolve(raw)
            if u:
                seen[u] = None
                if len(seen) >= limit:
                    break
        return list(seen)

    urls: list[str] = []
    for engine, page_html in _iter_search_engine_html(query):
        # Pull a wider href pool before the token filter — with a strict
        # all-tokens gate the first N raw links are often partial matches.
        found = _extract_links(page_html, max_results * 4)
        ranked = sorted(
            ((_url_match_strength(u), u) for u in found),
            key=lambda x: x[0],
            reverse=True,
        )
        matched = [u for strength, u in ranked if strength > 0]
        if found and not matched:
            # Full SERP, no URL carries every query token → soft miss; keep
            # walking the engine chain (esp. toward SearXNG).
            logger.info(
                f"_web_search_urls {query!r}: {engine} returned {len(found)} "
                f"link(s) with no all-token match — trying next engine"
            )
            continue
        new = 0
        for u in matched:
            if u not in urls:
                urls.append(u)
                new += 1
            if len(urls) >= max_results:
                break
        if new:
            logger.info(
                f"_web_search_urls {query!r}: {engine} contributed {new} "
                f"relevant link(s) (total {len(urls)})"
            )
        if len(urls) >= max_results:
            break

    (logger.info if not urls else logger.debug)(
        f'_web_search_urls {query!r}: {len(urls)} URLs'
    )
    return urls[:max_results]



def _search_targeted_sites(primary: str,
                            secondary: list[str],
                            skip_sources: list[str] | None = None,
                            return_all: bool = False):
    """Web-fallback step 1: query trusted game databases directly.

    Called only when the primary APIs (Steam, PCGamingWiki, VNDB) return
    no result above threshold.  Tries ALL sources in order:

    1. itch.io       (web search + direct itch.io search → scrape OG)
    2. DLSite        (for Japanese/indie titles, HTML scrape)
    3. MobyGames     (HTML search → first game URL → scrape OG)
    4. Wikipedia     (MediaWiki OpenSearch → scrape OG, last resort)

    ALL sources are always tried.  Returns the best-scored GameInfo (or
    None), or — with *return_all* — every distinct-title qualifying result
    as a best-first list.

    *skip_sources* is an optional list of source keys (e.g. ['itch']) to
    exclude — used when the caller already has data from that source and wants
    to try alternative sources within the same tier.
    """
    _engine_new_search_phase()   # blocked engines get one fresh probe per tier
    _skip = {s.lower() for s in (skip_sources or [])}
    _raw_all_hints = [primary] + list(secondary)   # before cleaning — may carry version info
    primary = _clean_game_name(primary) or primary
    secondary = [_clean_game_name(h) or h for h in secondary]
    all_hints = [primary] + secondary
    # Scoring uses every hint (folder/exe/code) as a BONUS only — they must
    # not fire extra SERP round-trips. Engines already index the title.
    _scoring_hints = [h for h in all_hints if h.lower() not in _GENERIC_EXE_STEMS] or all_hints[:1]
    for _h in list(_scoring_hints):
        _stripped = _strip_release_noise(_h, drop_version=True)
        if _stripped and _stripped not in _scoring_hints:
            _scoring_hints.append(_stripped)
        _spaced_h = re.sub(CAMEL_SPLIT_RE, ' ', _stripped or _h).strip()
        if _spaced_h and _spaced_h not in _scoring_hints:
            _scoring_hints.append(_spaced_h)
    # ONE title form for site: queries (spaced + noise-stripped). Optional
    # same-title+version variant when the raw folder still carries it.
    _q_best = re.sub(CAMEL_SPLIT_RE, ' ', primary).replace('_', ' ').strip() or primary
    _q_best = _strip_release_noise(_q_best, drop_version=True) or _q_best
    _q_ver = ""
    _prim_slug = _fuzzy_slug(_q_best)
    for _rh in _raw_all_hints:
        _rh = (_rh or '').strip()
        if not _rh or _rh.lower() in _GENERIC_EXE_STEMS:
            continue
        if re.match(r'^(RJ|RE|VJ)\d{4,10}$', _rh, re.IGNORECASE):
            continue
        _with_ver = _title_keep_version(_rh)
        _bare = _strip_release_noise(_with_ver, drop_version=True) or _with_ver
        if (not _with_ver or not _bare
                or _with_ver.casefold() == _bare.casefold()):
            continue
        if _prim_slug and _fuzzy_slug(_bare) == _prim_slug:
            _q_ver = re.sub(CAMEL_SPLIT_RE, ' ', _with_ver).replace('_', ' ')
            _q_ver = re.sub(r'\s+', ' ', _q_ver).strip()
            break
    _q_forms = [_q_best]
    if _q_ver and _q_ver.casefold() != _q_best.casefold():
        _q_forms.append(_q_ver)
    MIN_SCORE = 40.0
    results: list[tuple[GameInfo, float]] = []

    def _collect(source_name: str, info: GameInfo | None, score_bonus: float = 0.0) -> None:
        """Collect result from source. *score_bonus* is added for high-precision matches."""
        if info and info.name:
            score = max(_fuzzy_score(h, info.name) for h in _scoring_hints)
            score = min(100.0, score + score_bonus)
            bonus_str = f", base+{score_bonus:.0f}bonus" if score_bonus else ""
            accepted = score >= MIN_SCORE
            logger.info(
                f"Web fallback via {source_name}: {info.name!r} "
                f"(score={score:.0f}{bonus_str}) "
                f"{'✓' if accepted else f'✗ below MIN={MIN_SCORE}'}"
            )
            if accepted:
                results.append((info, score))

    # 1. itch.io — one title (bare), then same title+version if available.
    # Secondary hints score hits; they do not add more site: queries.
    if 'itch' not in _skip:
        try:
            _itch_seen: set[str] = set()
            _itch_hit = False
            for _q in _q_forms:
                _q = (_q or "").strip()
                _key = _q.casefold()
                if not _q or _key in _itch_seen:
                    continue
                _itch_seen.add(_key)
                url = _find_itch_url_via_search(
                    f'"{_q}" site:itch.io',
                    game_name=_q,
                )
                if not url:
                    url = _find_itch_url_via_search(
                        f'{_q} site:itch.io',
                        game_name=_q,
                    )
                if url:
                    info = _scrape_opengraph(url)
                    if info:
                        _collect("itch.io", info)
                        _itch_hit = True
                        break
                    logger.info(
                        f"Targeted itch.io: reached {url} but "
                        f"metadata scrape failed"
                    )
            if not _itch_hit:
                logger.info(f"Targeted itch.io: no page for {_q_best!r}")
        except Exception as e:
            logger.debug(f"itch.io targeted search failed: {e}")

    # 2. DLSite direct (product code RJ/RE/VJ in hints — precise match).
    # Read codes from RAW hints: _clean_game_name / strip_version_tokens
    # already remove RJ/RE/VJ, so cleaned all_hints would never see them.
    _dl_codes: list[str] = []
    for h in _raw_all_hints:
        for m in re.finditer(r'(?:^|[\s\[\(\{])(RJ|RE|VJ)(\d{4,10})(?:[\s\]\)\}]|$)', h, re.IGNORECASE):
            code = (m.group(1) + m.group(2)).upper()
            if code not in _dl_codes:
                _dl_codes.append(code)
    # URLs safe to attach onto OTHER candidates — only when the URL's
    # product_id matches a search-hint code (source of truth). Keyword
    # hits without a matching hint code are never attached.
    _dlsite_attach_urls: list[str] = []

    def _dlsite_url_code(u: str) -> str:
        m = re.search(r'product_id/((?:RJ|RE|VJ)\d{4,10})', u or '', re.I)
        return m.group(1).upper() if m else ""

    def _remember_dlsite_attach(u: str) -> None:
        base = (u or "").split("?")[0].strip()
        if not base:
            return
        code = _dlsite_url_code(base)
        if not code or code not in _dl_codes:
            return
        if base not in _dlsite_attach_urls:
            _dlsite_attach_urls.append(base)

    if _dl_codes and 'dlsite' not in _skip:
        # One candidate per product code. The section list is a fallback, not
        # a set of distinct sources: a code is only listed in the one section
        # that carries it, but ANY section answers for it by serving that
        # same work, so carrying on after a hit produced two extra identical
        # candidates. Which section is asked first no longer matters either —
        # _scrape_dlsite_en follows the canonical link, so a work living in a
        # section not even listed here (girls, bl, home…) still resolves.
        # Region-locked pages still scrape title/cover/… and are proposed;
        # the product code guarantees the same work even if the user later
        # rejects the candidate title as "wrong".
        for code in _dl_codes:
            info = None
            _tried_url = ""
            for _dl_sub in ("maniax", "soft", "pro"):
                _tried_url = (
                    f"https://www.dlsite.com/{_dl_sub}/work/=/product_id/{code}.html"
                )
                try:
                    info = _scrape_dlsite_en(_tried_url)
                except Exception as e:
                    logger.debug(f"DLSite product direct ({_dl_sub}) failed: {e}")
                    info = None
                if info and info.name:
                    break
            # Code hint → URL is known truth: attach even when metadata
            # scrape fails (region shell with no title, fetch error, …).
            _remember_dlsite_attach(
                (info.store_url if info and info.store_url else _tried_url)
            )
            if info and info.name:
                # Product code ensures we found the right game, but score
                # by name so better-matched sources (itch.io with English
                # name) win.  Floor at 40 so a code-only hit (Japanese
                # title vs English folder) is included but not dominant.
                name_score = max(_fuzzy_score(h, info.name) for h in _scoring_hints)
                _score = max(name_score, 40.0)
                logger.info(
                    f"Web fallback via DLSite product code {code}: "
                    f"{info.name!r} (name_score={name_score:.0f}, final={_score:.0f})"
                )
                results.append((info, _score))
            else:
                logger.info(
                    f"DLSite product code {code}: no usable page "
                    f"(URL still attachable)"
                )

    # 3. DLSite keyword search (by name, no RJ code needed)
    #
    #    Two paths for DLSite:
    #      A) RJ/RE/VJ code present  -> direct product URL (section 2 above)
    #      B) No code                -> web-indexed search via _find_dlsite_url_via_search
    #                                   (shared engine layer, filtered with site:dlsite.com)
    #    No /fsr/ direct endpoint - unreliable, no structured parsing needed.
    #    Keyword hits are proposed (including region-locked). Attach to other
    #    sources ONLY when the hit's product_id matches a hint code.
    _dl_keyword = _q_best or primary
    if 'dlsite' not in _skip:
        _dl_url = _find_dlsite_url_via_search(_dl_keyword)
        if _dl_url:
            # No score_bonus here: web-indexed search may surface any DLSite page
            # that matches keywords.  The 40-pt bonus is reserved for direct
            # product-code hits (section 2 above) where the match is guaranteed.
            # Same English-locale handling as the code lookup — a search hit
            # can land on any section, including one that ignores the locale.
            _dl_info = _scrape_dlsite_en(_dl_url)
            if _dl_info and _dl_info.name:
                _collect("DLSite (web)", _dl_info)
                _remember_dlsite_attach(_dl_info.store_url or _dl_url)
            else:
                # No candidate, but a code-matching URL may still attach.
                _remember_dlsite_attach(_dl_url)
                logger.info(f"Targeted DLSite: no product for {_dl_keyword!r}")
        else:
            logger.info(f"Targeted DLSite: no product for {_dl_keyword!r}")

    # 4. MobyGames (mainstream / commercial games) — use first non-generic hint
    _mg_keyword = _q_best or primary
    _mg_found = False
    if 'mobygames' not in _skip:
        try:
            mg_url = (
                f"https://www.mobygames.com/search/quick"
                f"?q={urllib.parse.quote(_mg_keyword)}&type=game"
            )
            html = _fetch_html(mg_url)
            if html:
                m = re.search(r'href="(https?://www\.mobygames\.com/game/[^"]+)"', html)
                if m:
                    _collect("MobyGames", _scrape_opengraph(m.group(1)))
                    _mg_found = True
        except Exception as e:
            logger.debug(f"MobyGames failed: {e}")
        # Web-engine fallback for MobyGames when direct search is blocked
        if not _mg_found:
            try:
                # Deeper pool: the /game/ filter below runs AFTER this slice,
                # so with only 4 URLs a run of non-game mobygames links
                # (search/company/person pages) could hide a /game/ result
                # ranked just past the cut. Same single engine query — a
                # wider pool only extracts more links from the same page.
                _mg_urls = _web_search_urls(
                    f'{_mg_keyword} site:mobygames.com', max_results=8
                )
                _mgm = next(
                    (u for u in _mg_urls
                     if re.match(r'https?://www\.mobygames\.com/game/', u)),
                    None,
                )
                if _mgm:
                    info = _scrape_opengraph(_mgm)
                    if info:
                        _collect("MobyGames (web)", info)
                    else:
                        logger.info(
                            f"Targeted MobyGames: reached {_mgm} but "
                            f"metadata scrape failed"
                        )
                else:
                    logger.info(f"Targeted MobyGames: nothing for {_mg_keyword!r}")
            except Exception as _be:
                logger.debug(f"MobyGames web fallback failed: {_be}")

    # 5. Wikipedia — try with and without "video game" suffix (last resort).
    #    Uses a higher threshold for bare-name results to avoid
    #    false matches between a short game title and an unrelated
    #    near-homograph encyclopedia article.
    if 'wikipedia' not in _skip:
        try:
            wiki_hits: list[tuple[str, str, float]] = []
            for suffix, min_score in [("", 55.0), (" video game", MIN_SCORE)]:
                _wiki_query = (_q_best or primary) + suffix
                hits = _mediawiki_search(
                    "https://en.wikipedia.org/w/api.php", _wiki_query
                )
                if hits:
                    for title, url in hits:
                        if not url or "youtube.com" in url:
                            continue
                        # Reject film/TV/band/etc. articles of the same name.
                        if _is_non_game_media_title(title):
                            logger.debug(f"Wikipedia skip (non-game media): {title!r}")
                            continue
                        score = max(_fuzzy_score(h, title) for h in _scoring_hints)
                        if score >= min_score:
                            wiki_hits.append((score, title, url))
            if wiki_hits:
                wiki_hits.sort(key=lambda x: x[0], reverse=True)
                for score, title, url in wiki_hits:
                    logger.debug(f"Trying Wikipedia {url[:60]} (score={score:.0f})")
                    info = _scrape_opengraph(url)
                    if info:
                        _collect("Wikipedia", info)
            else:
                logger.info("Targeted Wikipedia: no qualifying article")
        except Exception as e:
            logger.debug(f"Wikipedia failed: {e}")

    # Attach DLsite product URLs onto other candidates ONLY when the URL
    # matches a search-hint product code (source of truth). No hint code →
    # no attach in any case (keyword-only hits stay separate candidates).
    if _dlsite_attach_urls:
        if results:
            for _info, _ in results:
                _src = (getattr(_info, "source", "") or "").split("+")[0].lower()
                if _src == "dlsite":
                    continue
                _extras = list(_info.extra_urls or [])
                for _u in _dlsite_attach_urls:
                    if _u and _u not in _extras and _u != (_info.store_url or ""):
                        _extras.append(_u)
                if not (_info.store_url or "").strip() and _dlsite_attach_urls:
                    _info.store_url = _dlsite_attach_urls[0]
                    _extras = [u for u in _extras if u != _info.store_url]
                _info.extra_urls = _extras
            logger.info(
                "DLSite: attached code-hint URL(s) to non-DLsite candidates: "
                + ", ".join(_dlsite_attach_urls)
            )
        else:
            # Code is enough to propose the product link alone.
            results.append((GameInfo(
                name=primary,
                store_url=_dlsite_attach_urls[0],
                source="dlsite",
                extra_urls=list(_dlsite_attach_urls[1:]),
            ), 40.0))
            logger.info(
                "DLSite: proposing code-hint link under search title "
                f"{primary!r}"
            )

    # No merging of fields from multiple sources — enrichment from
    # additional sources is handled separately by the caller
    # (add_game_dialog enrichment chain).
    if not results:
        return [] if return_all else None
    results.sort(key=lambda x: x[1], reverse=True)
    if return_all:
        deduped = _dedupe_candidates(results)
        logger.info(
            "Trusted results: " + ", ".join(
                f"{i.name!r} ({i.source}, {s:.0f})" for i, s in deduped
            )
        )
        return [info for info, _ in deduped]
    best_info, best_score = results[0]
    logger.info(
        f"Best trusted result: {best_info.name!r} "
        f"(source={getattr(best_info,'source','?')}, score={best_score:.0f})"
    )
    return best_info


# A version/build string in the folder name (e.g. "v0.4.8") is the strongest
# signal that splits two same-named games apart — an early-access build is a
# different release from a later version of a same-titled game. So the
# version-bearing hint is queried verbatim (the '"…" game' form returns nothing
# for a version string) and a candidate whose title or URL carries that SAME
# version is lifted over clean-titled namesakes. Only high-entropy versions
# (≥3 numeric parts) count — a bare "1.0" would match unrelated pages. No
# site/domain is ever preferred: the folder's own version, not the kind of
# site, disambiguates.
_BARE_VERSION_RE = re.compile(r'(?<![\w.])(\d+(?:\.\d+){2,}[a-z]?)(?![\w.])')


def _hint_version(text: str) -> Optional[str]:
    """The version number in *text*, only when specific enough (≥3 parts)."""
    m = _VER_NUM_RE.search(text or "")
    if m and len(re.findall(r'\d+', m.group("num"))) >= 3:
        return m.group("num")
    m2 = _BARE_VERSION_RE.search(text or "")
    return m2.group(1) if m2 else None


def _version_match_re(num: str):
    """Match *num* with flexible separators: '0.4.8' also matches '0-4-8',
    '0_4_8' and '048' as they appear in result titles and URLs."""
    parts = re.findall(r'\d+', num)
    return re.compile(r'(?<!\d)' + r'[._\-]?'.join(parts) + r'(?!\d)')


def _web_search_urls_single(query: str,
                            all_hints: list[str] | None = None,
                            return_all: bool = False):
    """Generic web-engine search only, no targeted-site lookups.

    Returns the single best GameInfo (or None) — or, with *return_all*,
    every distinct-title candidate scoring ≥ 30 as a best-first list. Used
    as a last-resort fallback when primary APIs and trusted targeted sites
    have all been exhausted.

    Note: version / release markers stay on the query (they help recall), but
    packed folder spellings (``name_game``, ``NameGame``) are spaced out —
    search engines index the readable form, not the filesystem packing.
    """
    raw_hints = [h for h in (all_hints or [query]) if h]
    # Product codes are HINTS only — never a standalone SERP query. Tier 3
    # keeps version markers on the title; when a code is present the lead
    # query is "[CODE] <title>" (title spaced from _ / CamelCase).
    _DL_CODE_ONLY_RE = re.compile(r'^(RJ|RE|VJ)\d{4,10}$', re.IGNORECASE)
    _DL_CODE_FIND_RE = re.compile(r'(RJ|RE|VJ)\d{4,10}', re.IGNORECASE)

    def _strip_leading_dl_code(text: str) -> str:
        return re.sub(
            r'^[\s\[\(\{]*(?:RJ|RE|VJ)\d{4,10}[\s\]\)\}\-_–—|,.;]*',
            '', (text or '').strip(), flags=re.IGNORECASE,
        ).strip(' [](){}-_–—|.,;')

    def _space_packed_title(text: str) -> str:
        """Turn ``name_game`` / ``NameGame`` into readable SERP words.

        Underscores and CamelCase are filesystem packing — always spaced.
        Hyphens inside words (``yama-san``) and version dots stay as-is.
        """
        t = (text or "").replace("_", " ")
        t = re.sub(CAMEL_SPLIT_RE, " ", t)
        return re.sub(r"\s+", " ", t).strip()

    _dl_codes: list[str] = []
    for _h in raw_hints:
        for _m in _DL_CODE_FIND_RE.finditer(_h or ''):
            _c = _m.group(0).upper()
            if _c not in _dl_codes:
                _dl_codes.append(_c)

    # Title hints (not bare codes): keep version, space out packed spellings.
    _title_hints: list[str] = []
    for _h in raw_hints:
        if not _h or _DL_CODE_ONLY_RE.match(_h.strip()):
            continue
        _t = _space_packed_title(_strip_leading_dl_code(_h) or _h.strip())
        if _t and _t not in _title_hints:
            _title_hints.append(_t)
    _main_title = ""
    if query and not _DL_CODE_ONLY_RE.match(query.strip()):
        _main_title = _space_packed_title(
            _strip_leading_dl_code(query) or query.strip())
    if not _main_title and _title_hints:
        _main_title = _title_hints[0]
    # Prefer a longer hint that still carries the same title (folder often
    # keeps "Title v1.01" when the primary lost the version).
    if _main_title:
        _prim_cf = _main_title.casefold()
        _prim_words = [w for w in _prim_cf.split() if len(w) >= 3]
        for _t in _title_hints:
            _tcf = _t.casefold()
            if len(_t) <= len(_main_title):
                continue
            if _prim_cf in _tcf or (
                _prim_words and all(w in _tcf for w in _prim_words)
            ):
                _main_title = _t

    hints: list[str] = []
    if _main_title and _dl_codes:
        hints.append(f"[{_dl_codes[0]}] {_main_title}")
    if _main_title and _main_title not in hints:
        hints.append(_main_title)
    for _t in _title_hints:
        if _t not in hints:
            hints.append(_t)
    if not hints:
        # Absolute last resort: bare codes only (no title anywhere).
        hints = list(_dl_codes) or list(raw_hints)

    # Scoring: unclean titles + bare codes (a page that cites the code matches).
    _scoring_hints = [
        h for h in (hints + _dl_codes)
        if h and h.lower() not in _GENERIC_EXE_STEMS
    ] or hints[:1]
    # Scoring only: add bare-title variants so a folder like
    # "My Game v0.9 - Win" does not force mandatory "win" against a
    # clean store title (score ~20 < MIN 30 → false "not found").
    for _h in list(_scoring_hints):
        _stripped = _strip_release_noise(_h, drop_version=True)
        if _stripped and _stripped not in _scoring_hints:
            _scoring_hints.append(_stripped)
        _spaced = re.sub(CAMEL_SPLIT_RE, ' ', _stripped or _h).strip()
        if _spaced and _spaced not in _scoring_hints:
            _scoring_hints.append(_spaced)
    # Early-exit: if every hint is a generic stem, nothing useful to search for.
    if not any(h.lower() not in _GENERIC_EXE_STEMS for h in hints):
        logger.debug(
            f"_web_search_urls_single: all {len(hints)} hint(s) are "
            f"generic stems — skipping"
        )
        return [] if return_all else None
    _engine_new_search_phase()   # blocked engines get one fresh probe per tier
    # Query with the CamelCase-split form and drop slug-duplicates: pages
    # write the spaced form ("Super Game Story"), not the compound one
    # ("SuperGameStory"), and querying both spellings of the same title
    # would just burn engine quota. When two hints share a slug the
    # more-worded (spaced) variant wins. Capped to bound the number of
    # throttled engine round-trips.
    # ONE SERP query: lead hint is already "[CODE] title" or the spaced
    # title. Extra folder/exe strings stay in _scoring_hints only — the
    # engine index does the heavy matching; we do not re-query per hint.
    _lead = next(
        (h for h in hints if h and h.lower() not in _GENERIC_EXE_STEMS),
        "",
    )
    if not _lead:
        logger.info("Generic web: no usable title query")
        return [] if return_all else None
    _folder_ver = _hint_version(_lead) or next(
        (v for h in hints if (v := _hint_version(h))), None)
    _ver_re = _version_match_re(_folder_ver) if _folder_ver else None
    # Unquoted first (broader recall), then quoted if needed — not both
    # bare/spaced/version permutations of every secondary hint.
    _forms = [_lead]
    _bare_lead = _strip_release_noise(_lead, drop_version=True)
    if _bare_lead and _bare_lead.casefold() != _lead.casefold():
        # Drop the code prefix from a bare retry when present.
        _bare_nocode = re.sub(
            r'^\[(?:RJ|RE|VJ)\d{4,10}\]\s*', '', _bare_lead, flags=re.I,
        ).strip()
        if _bare_nocode and _bare_nocode.casefold() != _lead.casefold():
            _forms.append(_bare_nocode)

    MAX_GENERIC_RESULTS = 6
    candidates: list[tuple[GameInfo, float]] = []
    seen_urls: set[str] = set()
    stop = False
    _target = 8 if _folder_ver else 6

    for q in _forms:
        if stop or len([1 for _, s in candidates if s >= 30.0]) >= MAX_GENERIC_RESULTS:
            break
        urls = _web_search_urls(q, max_results=_target * 2)
        _query_valid = 0
        for url in urls:
            if (_query_valid >= _target
                    or len([1 for _, s in candidates if s >= 30.0]) >= MAX_GENERIC_RESULTS):
                break
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if "dlsite.com" in (url or "").lower() and "/work/=" in (url or "").lower():
                info = _scrape_dlsite_en(url)
            else:
                info = _scrape_opengraph(url)
            if not info or not info.name:
                continue
            if _is_non_game_media_title(info.name):
                logger.debug(f"Generic web skip (non-game media): {info.name!r}")
                continue
            info.source = "web"
            info.store_url = info.store_url or url
            base_score = max(_fuzzy_score(h, info.name) for h in _scoring_hints)
            score = base_score
            if _ver_re and (_ver_re.search(info.name or "") or _ver_re.search(url)):
                score = max(score, 90.0)
                logger.debug(
                    f"Generic web: version {_folder_ver} matched in "
                    f"{url[:45]} → boosted to {score:.0f}")
            logger.debug(f"Generic web: {url[:55]} → {info.name!r} (score={score:.0f})")
            candidates.append((info, score))
            _query_valid += 1
            if base_score >= 60:
                logger.info(f"Generic web early hit: {info.name!r} (score={score:.0f})")
                stop = True
                break

    if not candidates:
        logger.info("Generic web: no results found")
        return [] if return_all else None

    candidates.sort(key=lambda x: x[1], reverse=True)
    if return_all:
        kept = _dedupe_candidates([c for c in candidates if c[1] >= 30.0])
        logger.info(
            "Generic web results: " + (
                ", ".join(f"{i.name!r} ({s:.0f})" for i, s in kept) or "none ≥ 30"
            )
        )
        return [info for info, _ in kept]
    best, best_score = candidates[0]
    logger.info(f"Generic web best: {best.name!r} (score={best_score:.0f})")
    return best if best_score >= 30 else None
