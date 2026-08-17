"""
SaveSync - Game Info APIs
Fetches game information from public APIs (Steam, RAWG, VNDB).
No configuration required - uses free public endpoints.
"""
import logging
import re
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional

from core.constants import CAMEL_SPLIT_RE

logger = logging.getLogger(__name__)


# ── Source modules (facade re-exports) ──────────────────────────────
# game_api stays the single entry point: every name that used to live
# here is importable (and monkeypatchable) exactly as before, while the
# implementations live in core/game_sources/*.
from core.game_sources.common import (   # noqa: F401
    GameInfo, _fuzzy_slug, _fuzzy_words, _fuzzy_score, _find_best_match, can_score,
    _normalize_numerals, _decode_entities, _clean_description,
    _fetch_json, _expand_search_terms, _clean_game_name,
    _build_search_queries, _is_non_game_media_title, _is_favicon_like,
    _parse_forum_description, _GENERIC_EXE_STEMS,
    _strip_release_noise, _dedupe_slug, _dedupe_candidates, _release_year,
    _NOISE_LANG, _NOISE_PLATFORM, _NOISE_TAG, _RELEASE_NOISE,
)
from core.game_sources.steam import search_steam   # noqa: F401
from core.game_sources.wiki import (   # noqa: F401
    search_pcgamingwiki, _pcgw_extract_store_url,
)
from core.game_sources.vndb import (   # noqa: F401
    search_vndb, fetch_vndb_by_id, _parse_vndb_entry, _VNDB_FIELDS,
)
from core.game_sources.webscrape import (   # noqa: F401
    _fetch_html, _fetch_html_ex, _mediawiki_search, _scrape_opengraph,
    _search_targeted_sites, _web_search_urls, _web_search_urls_single,
    _find_itch_url_via_search, _find_dlsite_url_via_search,
    _scrape_itch_title, engines_blocked_status, _engine_new_search_phase,
)


class UrlFetchError(Exception):
    """A pasted-link fetch failed for a REPORTABLE reason (as opposed to
    "the page had no usable metadata", which is a plain None result).

    kind:   'blocked' — the page answered with an anti-bot wall/challenge
                        (403/429/503, Cloudflare "Just a moment…", …);
            'network' — the page could not be reached at all (DNS, timeout,
                        refused connection).
    status: the HTTP status code when one was received, else 0."""

    def __init__(self, kind: str, status: int = 0):
        self.kind = kind
        self.status = status
        super().__init__(f"{kind} (HTTP {status})" if status else kind)


# Strong anti-bot interstitial markers. Only checked near the top of the
# page (title/head area) so a game page that merely MENTIONS a captcha in
# its body text never false-positives.
_PROTECTION_RE = re.compile(
    r"(just a moment|cf-chl|attention required|checking your browser"
    r"|ddos-guard|verify you are human|are you a robot)",
    re.IGNORECASE,
)


def fetch_info_from_url(url: str) -> Optional[GameInfo]:
    """Fetch game metadata for a pasted store/database link.

    The link itself is the search: the page is read directly via the
    OpenGraph scraper. Only vndb.org entries (Kana API by id) and Steam
    store pages (appdetails by appid) go through their API first — and
    when that API call fails, the same URL still falls back to the
    direct page read instead of giving up. The source is labeled by
    domain so the dialog's tier logic treats it correctly. Returns None
    when nothing usable.

    Raises UrlFetchError when the direct read failed for a reportable
    reason — anti-bot wall ('blocked') or unreachable page ('network') —
    so the UI can say WHY instead of a generic "not found"."""
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    m = re.search(r'(?:^|//|\.)vndb\.org/(v\d+)', u)
    if m:
        try:
            info = fetch_vndb_by_id(m.group(1))
        except Exception as e:
            logger.debug(f"VNDB API failed for {u!r}: {e}")
            info = None
        if info:
            return info
        logger.info(f"VNDB API gave nothing for {u!r} — reading the page directly")
    m = re.search(r'store\.steampowered\.com/app/(\d+)', u)
    if m:
        try:
            info = search_steam("", m.group(1))
        except Exception as e:
            logger.debug(f"Steam API failed for {u!r}: {e}")
            info = None
        if info:
            return info
        logger.info(f"Steam API gave nothing for {u!r} — reading the page directly")
    status, html = _fetch_html_ex(u)
    if status == 0:
        raise UrlFetchError('network')
    if status in (401, 403, 429, 503) or (
            html and _PROTECTION_RE.search(html[:4000])):
        logger.info(f"Page {u!r} is behind an anti-bot wall (HTTP {status})")
        raise UrlFetchError('blocked', status)
    if not (200 <= status < 300) or not html:
        raise UrlFetchError('network', status)
    info = _scrape_opengraph(u, html=html)
    if not info or not info.name:
        return None
    host = urllib.parse.urlsplit(u).netloc.lower()
    if "steampowered.com" in host:
        info.source = "steam"
    elif "vndb.org" in host:
        info.source = "vndb"
    elif "itch.io" in host:
        info.source = "itch"
    elif "dlsite.com" in host:
        info.source = "dlsite"
    elif "mobygames.com" in host:
        info.source = "mobygames"
    elif "wikipedia.org" in host:
        info.source = "wikipedia"
    else:
        info.source = info.source or "web"
    info.store_url = info.store_url or u
    return info


def _result_names(result) -> list[str]:
    """Every name a result may legitimately be recognised by.

    A source searches a game's original title, its romanization and its
    release titles, and hands back the entry that matched. Judging that entry
    on its DISPLAY name alone rejected answers the source had already got
    right — see GameInfo.alt_names.
    """
    names = [getattr(result, "name", "") or ""]
    names += list(getattr(result, "alt_names", None) or [])
    return [n for n in names if n]


def _best_over_names(score_fn, result) -> float:
    """The best a result scores under *score_fn*, over all its names."""
    return max((score_fn(n) for n in _result_names(result)), default=0.0)


def _score_against_hints(result_name: str, primary: str, secondary_hints: list[str]) -> float:
    """Score a result name against the primary query and secondary hints.

    Rules:
    - The primary name (user-given game name) drives the score.
    - Secondary hints (exe stem, folder name) can only *raise* the score if
      they produce a result that also matches the primary reasonably well.
    - A result found only via a short exe stem (e.g. "sol") must score ≥ 60
      against the primary before it's accepted, preventing "sol" → "Nine Sols".
    """
    primary_score = _fuzzy_score(primary, result_name)
    # If the primary already gives a good match, use that
    if primary_score >= 55:
        return primary_score
    # Try secondary hints — they may describe the same game differently
    best_secondary = max((_fuzzy_score(h, result_name) for h in secondary_hints), default=0.0)
    # A result that scores well against a secondary hint but poorly against the
    # primary is likely a false positive.  Apply a penalty.
    if best_secondary >= 55 and primary_score >= 20:
        # Secondary hit with plausible primary linkage
        return max(primary_score, best_secondary * 0.75)
    # Otherwise return the primary score only
    return primary_score


# Release/packaging decorations stripped when comparing candidate titles for
# identity (de-dup) and when building targeted-site queries. These are
# distribution wrappers a store or release folder adds around the real title
# — spoken language, target platform, and edition/build tags — never part of
# the game's identity. Kept small on purpose and matched only as a TRAILING
# run (see _strip_release_noise) so a leading real word that collides with
# the vocabulary is preserved (a title that merely starts with a noise word
# such as "PC", "Test" or "Final"). The software VERSION number is
# deliberately NOT here: it is the
# disambiguator between two different games sharing a title, so identity
# comparisons keep it (targeted-site queries drop it via drop_version=True).
def search_game_info_multi(game_name: str, appid: Optional[str] = None,
                           enable_web_fallback: bool = False,
                           exe_path: str = "",
                           folder_name: str = "",
                           skip_primary_apis: bool = False,
                           enable_targeted_fallback: Optional[bool] = None,
                           enable_generic_fallback: Optional[bool] = None,
                           skip_api_sources: list[str] | None = None,
                           skip_targeted_sources: list[str] | None = None) -> list[GameInfo]:
    """Search for game info across Steam, PCGamingWiki and VNDB.

    Strategy:
    1. Collect *all* candidates from all APIs using all available hints.
    2. Score every candidate against the **primary** name (game_name) — the
       name the user typed.  Secondary hints (exe stem, folder name) help
       retrieve the right results but must not override a poor primary match.
    3. Return every distinct-title result that clears the minimum threshold,
       best first — the caller lets the user pick (or takes the first).
    4. If no API succeeds and enable_web_fallback is True, try a web scrape
       (may be inaccurate — caller must warn the user, offer only once).

    Args:
        game_name:                User-given display name — primary matching key.
        appid:                    Steam/launcher appid if known.
        enable_web_fallback:      Activate both targeted + generic web fallback.
        exe_path:                 Executable path; stem used as secondary search hint.
        folder_name:              Install-folder name; used as secondary search hint.
        skip_primary_apis:        If True, skip Steam/PCGamingWiki/VNDB entirely and
                                  go straight to web fallback (targeted sites, then
                                  generic web search if needed).
        enable_targeted_fallback: Try targeted sites only (itch.io, DLSite, MobyGames,
                                  Wikipedia).  Overrides enable_web_fallback when set.
        enable_generic_fallback:  Try generic web search only (Brave/Bing/SearXNG/DDG).
                                  Overrides enable_web_fallback when set.
    """
    # ── Resolve fallback flags ────────────────────────────────────────────────
    if enable_targeted_fallback is None:
        enable_targeted_fallback = enable_web_fallback
    if enable_generic_fallback is None:
        enable_generic_fallback = enable_web_fallback

    # ── Build search hints ────────────────────────────────────────────────────
    primary = game_name.strip()
    primary_clean = _clean_game_name(primary) or primary
    secondary: list[str] = []

    for raw in [folder_name, Path(exe_path).stem if exe_path else ""]:
        term = raw.strip() if raw else ""
        if not term or term == primary:
            continue
        # Filter out generic exe stems — they provide no useful search signal
        if term.lower() in _GENERIC_EXE_STEMS:
            logger.debug(f"Skipping generic exe stem as secondary hint: {term!r}")
            continue
        # Filter out very short hints (≤ 3 alphanum chars): "ps", "pro", "run" etc.
        # all match too many unrelated results (e.g. "ps" in "apocalypse").
        from core.constants import match_slug, slug_weight
        _term_clean = match_slug(term)
        if slug_weight(_term_clean) <= 3:
            logger.debug(f"Skipping too-short secondary hint "
                         f"({slug_weight(_term_clean)}): {term!r}")
            continue
        spaced = re.sub(CAMEL_SPLIT_RE, ' ', term).strip()
        for t in [term, spaced]:
            if t and t not in secondary and t != primary:
                secondary.append(t)

    # ── Early-exit: generic exe name check ───────────────────────────────────
    # If the primary name (after cleaning) is exactly a generic exe stem,
    # replace it with the first non-generic secondary hint (folder name, exe
    # stem) so all downstream search and scoring work naturally.
    _check_name = re.sub(r'[\[\]\(\)\{\}]', '', primary).strip().lower()
    _is_generic = _check_name in _GENERIC_EXE_STEMS
    if _is_generic:
        logger.info(
            f"Primary name {primary!r} resolves to generic stem {_check_name!r}"
        )
        # Find first non-generic secondary hint to use as effective primary
        _replacement = next((h for h in secondary if h.lower() not in _GENERIC_EXE_STEMS), None)
        if _replacement:
            logger.info(f"Replacing generic primary with folder hint: {_replacement!r}")
            primary = _replacement
            primary_clean = _clean_game_name(primary) or primary
        else:
            logger.info("No non-generic secondary hints — will skip API search")

    # CamelCase-split variant of the primary name ("SuperGameStory" →
    # "Super Game Story") as the top-priority extra hint. Fuzzy SCORING
    # already splits camelCase, but the QUERIES sent to every tier did
    # not — real pages write the spaced form, so the compound form
    # retrieves nothing outside Steam (which expands terms itself).
    _prim_spaced = re.sub(
        CAMEL_SPLIT_RE, ' ', primary_clean
    ).strip()
    if _prim_spaced and _prim_spaced not in (primary, primary_clean) \
            and _prim_spaced not in secondary:
        secondary.insert(0, _prim_spaced)

    all_hints = [primary] + secondary
    logger.info(f"API search: primary={primary!r} clean={primary_clean!r} secondary={secondary}")

    _has_useful_hints = _is_generic and bool(
        next((h for h in secondary if h.lower() not in _GENERIC_EXE_STEMS), None)
    )
    # True when primary was generic and no non-generic hint was found to
    # replace it — all downstream searches should be skipped entirely.
    _skip_search = _is_generic and not _has_useful_hints

    # ── Collect all raw GameInfo results ──────────────────────────────────────
    # Each entry: (GameInfo, score_vs_primary)
    candidates: list[tuple[GameInfo, float]] = []
    MIN_ACCEPT = 45.0   # minimum score against primary to even consider a result
    MIN_PRIMARY = 30.0  # minimum primary score even for secondary-hint hits

    def _collect(search_fn, hint: str, is_secondary: bool = False):
        """Run search_fn(hint), score result vs primary, add to candidates."""
        try:
            r = search_fn(hint)
            if not r:
                return
            # Scored over every name the game is known by, not just the one
            # it is displayed under: the source matched on one of the others
            # and the display title may share nothing with what was asked.
            score = _best_over_names(
                lambda n: _score_against_hints(n, primary_clean, secondary), r)
            # Secondary-hint results need a minimum primary linkage
            if is_secondary:
                primary_score = _best_over_names(
                    lambda n: _fuzzy_score(primary_clean, n), r)
                if primary_score < MIN_PRIMARY:
                    logger.debug(
                        f"Rejected secondary hit '{r.name}' (hint={hint!r}): "
                        f"primary score {primary_score:.0f} < {MIN_PRIMARY}")
                    return
            # A query written in a script the scorer will not judge scores
            # zero against everything, and zero here would be read as "a bad
            # match" and dropped — even though the source searched that very
            # script and picked this entry out of it. Its own ranking is the
            # only opinion available, so it is taken at exactly the threshold:
            # good enough to be offered, never enough to outrank a result that
            # genuinely matched what was asked.
            if score <= 0 and not can_score(primary_clean):
                logger.debug(
                    f"Candidate '{r.name}' via {search_fn.__name__}({hint!r}): "
                    f"unscoreable query — deferring to the source's own ranking")
                candidates.append((r, MIN_ACCEPT))
                return
            logger.debug(f"Candidate '{r.name}' via {search_fn.__name__}({hint!r}): score={score:.0f}")
            candidates.append((r, score))
        except Exception as e:
            logger.debug(f"{search_fn.__name__}({hint!r}) failed: {e}")

    # ── Steam appid (most accurate — always try if available) ─────────────────
    _skip_api = {s.lower() for s in (skip_api_sources or [])}
    if appid and str(appid).isdigit() and not _skip_search and not skip_primary_apis \
            and 'steam' not in _skip_api:
        try:
            r = search_steam(primary_clean, str(appid))
            if r:
                score = _best_over_names(
                    lambda n: _score_against_hints(n, primary_clean, secondary), r)
                candidates.append((r, score + 20))  # bonus for known appid
        except Exception as e:
            logger.debug(f"Steam appid failed: {e}")

    # ── Primary APIs × all hints ──────────────────────────────────────────────
    if not skip_primary_apis and not _skip_search:
        # Build list of api search functions, excluding any in skip_api_sources
        _api_fns = []
        if 'steam' not in _skip_api:
            _api_fns.append(search_steam)
        if 'pcgamingwiki' not in _skip_api:
            _api_fns.append(search_pcgamingwiki)
        if 'vndb' not in _skip_api:
            _api_fns.append(search_vndb)
        for fn in _api_fns:
            # One query per API — secondary folder/exe hints only boost
            # scoring via _score_against_hints, they do not fire extra searches.
            _collect(fn, primary, is_secondary=False)

    # ── Pick accepted candidates ──────────────────────────────────────────────
    if candidates:
        # Sort by score descending, break ties by source priority (steam > itch > vndb)
        source_priority = {"steam": 3, "pcgamingwiki": 2, "vndb": 1, "web": 0}
        candidates.sort(key=lambda x: (x[1], source_priority.get(x[0].source, 0)), reverse=True)
        accepted = _dedupe_candidates(
            [c for c in candidates if c[1] >= MIN_ACCEPT]
        )
        if accepted:
            logger.info(
                "API matches: " + ", ".join(
                    f"'{i.name}' via {i.source} ({s:.0f})" for i, s in accepted
                )
            )
            # Enrichment from trusted / generic-web sources is handled by
            # the dialog's enrichment chain (_run_next_enrichment_tier).
            # Results are distinct titles, best first; fields from multiple
            # sources are never merged here.
            return [info for info, _ in accepted]
        best_result, best_score = candidates[0]
        logger.info(
            f"Best candidate '{best_result.name}' score={best_score:.0f} "
            f"below threshold {MIN_ACCEPT} — rejecting"
        )

    # ── Web fallback (opt-in, targeted first, then generic) ───────────────────
    # When primary name is a generic stem with no non-generic secondary hints
    # to guide the search, skip all web fallback — the query is uninformative.
    if enable_targeted_fallback or enable_generic_fallback:
        apis_tried = "Steam, PCGamingWiki, VNDB"
        logger.info(
            f"No result from primary APIs ({apis_tried}) for {primary!r}"
        )
        if _skip_search:
            logger.info(
                f"Primary name is generic with no non-generic hints — "
                f"skipping targeted and generic web fallback"
            )
            return []
        # Step 1: trusted targeted sites (tier 2). Guarded on its OWN so a
        # failure here can never suppress the generic tier below — previously a
        # single try/except wrapped both tiers, so any tier-2 exception (hint
        # building, dedup, one engine) returned [] and tier-3 never ran.
        if enable_targeted_fallback:
            try:
                logger.info("Falling back to targeted site search.")
                # Pass the RAW primary (version/platform still intact) so
                # tier-2 can build both bare and title+version itch queries.
                # Passing primary_clean here used to drop the version token
                # before _title_keep_version could see it.
                targeted = _search_targeted_sites(primary, secondary,
                                                   skip_sources=skip_targeted_sources,
                                                   return_all=True)
                if targeted:
                    logger.info(
                        "Found via targeted sites: "
                        + ", ".join(i.name for i in targeted)
                    )
                    return targeted
            except Exception as e:
                logger.error(f"Targeted site search failed (continuing to generic web): {e}")
        # Step 2: generic web as last resort (tier 3) — runs even if tier 2 raised.
        if enable_generic_fallback:
            if _skip_search:
                logger.info(
                    f"Primary name {primary!r} is generic — "
                    "skipping generic web fallback"
                )
                return []
            try:
                logger.info("Targeted returned nothing, trying generic web...")
                r = _web_search_urls_single(primary, all_hints, return_all=True)
                if r:
                    logger.info(
                        "Found via generic web: " + ", ".join(i.name for i in r)
                    )
                    return r
            except Exception as e:
                logger.error(f"Generic web fallback failed: {e}")

    logger.info(f"No API result cleared threshold for: {primary!r}")
    return []

# ── Web enrichment merge ──────────────────────────────────────────────────────

# ── HTTP request helper ───────────────────────────────────────────────────────

