"""
SaveSync - Shared building blocks for the game-info sources.

Extracted verbatim from core/game_api.py: fuzzy matching + numeral
normalization, description cleaners, the GameInfo dataclass, JSON fetch,
query expansion/cleaning and the shared noise filters. Every source module
builds on this; it imports NOTHING from its siblings.
"""
import html
import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from core.constants import CAMEL_SPLIT_RE
from core.net import open_url as _open_url
from core.save_detector import GENERIC_EXE_STEMS as _GENERIC_EXE_STEMS_LIST

logger = logging.getLogger(__name__)


def _fuzzy_slug(s: str) -> str:
    """Normalize string for fuzzy matching.

    Was NFKD followed by ``encode("ascii", "ignore")``, which folded accents
    correctly and then threw away every character that was not Latin. A
    Japanese title came out empty, and an empty slug scores zero against
    everything — including against ITSELF, so a game named in Japanese could
    not be matched to its own store entry however exactly the two agreed.
    match_slug folds the accents the same way and keeps the rest.
    """
    from core.constants import match_slug
    return match_slug(s)


def _has_latin(slug: str) -> bool:
    """Whether a slug holds anything the word/character scores can read."""
    return any("a" <= ch <= "z" or "0" <= ch <= "9" for ch in slug)


def can_score(query: str) -> bool:
    """Whether _fuzzy_score can say anything useful about *query* at all.

    False for a query written entirely in a script the scorer declines to
    judge. That distinction matters to callers: a zero from _fuzzy_score
    normally means "a poor match", but for such a query it means "no
    opinion", and treating the two the same throws away results the source
    itself was confident about.
    """
    return _has_latin(_fuzzy_slug(query))


def _fuzzy_words(s: str) -> set[str]:
    """Extract normalized words for word-level matching.

    Also splits camelCase/PascalCase compounds to improve matching
    when the exe stem or folder name concatenates words (e.g.
    "SuperGameStory" → {"super", "game", "story"}).
    """
    # First, insert spaces at camelCase boundaries in the original string,
    # then lower-case and extract alphanumeric tokens.
    spaced = re.sub(CAMEL_SPLIT_RE, ' ', s)
    words: set[str] = set(re.findall(r'[a-z0-9]+', spaced.lower()))
    return words


# Roman ↔ Arabic sequel numerals. Multi-character forms only (II…XX): single
# letters "I"/"V"/"X" are left alone — in titles they are far more often real
# words/initials ("I Robot", "X-Men", "V") than the numbers 1/5/10. Titles are
# canonicalised to Arabic so "Example Game VII" and "Example Game 7" match.
_ROMAN_TO_ARABIC = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
}
_ROMAN_WORD_RE = re.compile(r'(?<![a-z0-9])[ivxlcdm]{2,}(?![a-z0-9])', re.IGNORECASE)


def _normalize_numerals(text: str) -> str:
    """Canonicalise multi-char Roman sequel numerals to Arabic ("VII" → "7")
    so a title using either form matches the other. Only exact entries in
    _ROMAN_TO_ARABIC are converted, so ordinary words made of Roman letters
    ("mix", "civil") are left untouched; single-letter I/V/X are never
    converted (too often real words)."""
    if not text:
        return text
    return _ROMAN_WORD_RE.sub(
        lambda m: _ROMAN_TO_ARABIC.get(m.group(0).lower(), m.group(0)), text)

# Matches version/build strings where the number is NOT a sequel identifier.
# e.g. "v1.0.1", "ver2", "version 3.0.1", "b12", "build 15" — the digits
# inside are version/build numbers and must NOT be treated as mandatory
# sequel numbers.
_VER_NUM_RE = re.compile(
    r'\b(?:v(?:er(?:sion)?)?|b(?:uild)?)\s*(?P<num>\d+(?:[._]\d+)*)',
    re.IGNORECASE,
)


def _fuzzy_score(query: str, target: str) -> float:
    """Calculate fuzzy match score between query and target.

    Mandatory-word rule:
      EVERY query word must appear in the target — no stop-word list and no
      length threshold, so even 1-char words count. A candidate that drops a
      leading article, preposition or single letter is a DIFFERENT title and
      is rejected ("The Example" ≠ "Example", "X Example" ≠ "Example"). Both
      sides are first version-stripped (a "v0.4.8" / "build 2" marker is not a
      word) and apostrophe-normalised (a possessive "Name's" → the single word
      "names", never a stray "s"). A bare sequel number is mandatory too
      ("Example 3" ≠ "Example 2"), and a Roman sequel numeral is canonicalised
      to Arabic, so "Example VII" matches "Example 7" (single letters I/V/X are
      left alone, being real words more often than 1/5/10).

    Missing mandatory words halve the score per absent word, floored at 5,
    always landing below MIN_ACCEPT (45).

    Returns a score from 0–100.
    """
    # Canonicalise Roman sequel numerals to Arabic so "…VII" and "…7" match.
    query = _normalize_numerals(query)
    target = _normalize_numerals(target)
    query_slug = _fuzzy_slug(query)
    target_slug = _fuzzy_slug(target)

    if not query_slug or not target_slug:
        return 0.0

    if query_slug == target_slug:
        return 100.0

    # Past this point every measure assumes a script written in separate
    # words out of a small alphabet. Japanese is neither: it has no spaces to
    # split on, and two unrelated titles routinely share kana.
    #
    # This was measured against the live database rather than reasoned about,
    # and the reasoning lost. Allowing only the exact-substring tests through
    # — which look strong, being exact — still picked a spin-off over the
    # game asked for in three lookups out of five, because a short title is
    # contained in every longer one built on it. Nothing partial is reliable
    # here. An exact match above still counts in any script; anything less is
    # left to the search engine's own ranking rather than overruled by a
    # number that does not mean what it says.
    if not (_has_latin(query_slug) and _has_latin(target_slug)):
        return 0.0

    query_words = _fuzzy_words(query)
    target_words = _fuzzy_words(target)

    # ── Mandatory-word gate ──────────────────────────────────────────────
    # EVERY word must appear in the target — no stop-word list, no length
    # threshold, so 1-char words count too (a leading article or single letter
    # a candidate drops makes it a different title). Two normalisations,
    # applied to BOTH sides so they compare on equal footing: version/build
    # markers are stripped (a folder's "v0.4.8" must not force the store title
    # to carry it), and apostrophes are removed so a possessive does not split
    # into a stray "s" ("Name's" → one word "names"). Bare sequel numbers /
    # Roman numerals are ordinary words and so stay mandatory ("Example 3" ≠
    # "Example 2").
    from core.constants import strip_version_tokens

    def _mandatory_tokens(text: str) -> set[str]:
        cleaned = strip_version_tokens(text).replace("'", "").replace("’", "")
        return _fuzzy_words(cleaned)

    missing = _mandatory_tokens(query) - _mandatory_tokens(target)
    if missing:
        penalty = 0.5 ** len(missing)
        return max(5.0, 40.0 * penalty)

    # ── Normal Jaccard + character scoring ───────────────────────────────
    word_score = 0.0
    if query_words and target_words:
        intersection = query_words & target_words
        union = query_words | target_words
        word_score = (len(intersection) / len(union)) * 100.0
        query_recall = len(intersection) / len(query_words)
        word_score *= (0.5 + 0.5 * query_recall)

    char_score = 0.0
    if query_slug in target_slug:
        char_score = 80.0 * len(query_slug) / len(target_slug)
    elif target_slug in query_slug:
        char_score = 70.0
    else:
        common_chars = set(query_slug) & set(target_slug)
        if common_chars:
            overlap_ratio = len(common_chars) / max(len(query_slug), len(target_slug))
            char_score = 50.0 * overlap_ratio

    return max(word_score, char_score)



def _find_best_match(query: str, candidates: list[dict], name_field: str = "name") -> Optional[dict]:
    """Find best matching result from API candidates using fuzzy matching."""
    if not candidates:
        return None
    
    best_score = 0
    best_match = None
    
    for candidate in candidates:
        name = candidate.get(name_field, "")
        if not name:
            continue
        
        score = _fuzzy_score(query, name)
        logger.debug(f"Fuzzy score for '{name}': {score}")
        
        if score > best_score:
            best_score = score
            best_match = candidate
    
    logger.debug(f"Best match score: {best_score}")
    
    # Only return if score is reasonable (above 25 for better recall)
    if best_score >= 25:
        return best_match

    # Nothing scored — but for a query the scorer declines to judge, that is
    # not the same statement. The source searched in the query's own script
    # and returned these in its own order; its first hit is the only opinion
    # anyone has, and discarding it means finding nothing for a game that was
    # in fact found. Only reached when the score cannot mean what it says.
    if not can_score(query):
        first = next((c for c in candidates if c.get(name_field)), None)
        if first is not None:
            logger.debug(f"Unscoreable query {query!r} — deferring to the "
                         f"source's own first hit: {first.get(name_field)!r}")
            return first

    logger.debug("No candidate scored >= 25 — returning None instead of first result")
    return None


# ── Web-text sanitisation ────────────────────────────────────────────────
# Web sources (Steam JSON, Wikipedia/DLsite/itch scrapes, generic results)
# routinely leave HTML entities in text fields, so a game's name, developer
# or description arrives as "N&amp;R" or "you&#039;re". Everything is decoded
# once, at GameInfo construction, so every source is covered uniformly and
# the stored entry is clean.
_DLSITE_SIGNATURE_RE = re.compile(
    r'[\s"\'?]*DLsite[^"\n]{0,40}["\']?\s+is a download shop\b.*?DLsite\s*!',
    re.IGNORECASE | re.DOTALL,
)
# Conservative HTML-tag strip — only well-known tags, never a broad
# ``<[^>]+>`` which would eat "<3"-style text. Relevant because decoding an
# entity-encoded "&lt;br&gt;" turns it into a real "<br>".
_HTML_TAG_RE = re.compile(
    r'</?(?:br|p|b|i|u|s|strong|em|div|span|a|ul|ol|li|h[1-6]|hr|blockquote|small)\b[^>]*>',
    re.IGNORECASE,
)


def _decode_entities(text: str) -> str:
    """Decode HTML entities and trim. Plain ``html.unescape`` (no aggressive
    ``#``-repair) — covers the reported cases (``&amp;``, ``&#039;``) and is
    safe for free-text descriptions."""
    if not text:
        return text
    return html.unescape(text).replace('\xa0', ' ').strip()


def _clean_description(text: str) -> str:
    """Decode entities, then remove DLsite's site-wide signature boilerplate
    and any leaked HTML tags, collapsing runs of whitespace."""
    if not text:
        return text
    text = html.unescape(text).replace('\xa0', ' ')
    text = _DLSITE_SIGNATURE_RE.sub(' ', text)
    text = _HTML_TAG_RE.sub(' ', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


@dataclass
class GameInfo:
    name: str
    description: str = ""
    image_url: str = ""
    release_date: str = ""
    genres: Optional[list[str]] = None
    developer: str = ""
    publisher: str = ""
    store_url: str = ""          # link to store page / official site
    source: str = ""             # which API provided the info
    extra_urls: Optional[list[str]] = None   # additional site pages (e.g. the VNDB entry)
    # The other titles this same game is known by — its original title, its
    # romanization, the name it was released under elsewhere.
    #
    # A source searches ALL of them and hands back the one entry that matched;
    # the caller then scores what came back against what was asked for, and
    # without this it can only score the DISPLAY name. That threw away
    # perfectly good answers: a game asked for by the name it was released
    # under is found by a source that lists it under its original title, and
    # the score between those two strings can be 10 out of 100 — so the game
    # the source had already identified was rejected. Carrying the titles it
    # matched on is what lets the answer be recognised as the right one.
    alt_names: Optional[list[str]] = None

    def __post_init__(self):
        if self.genres is None:
            self.genres = []
        if self.extra_urls is None:
            self.extra_urls = []
        if self.alt_names is None:
            self.alt_names = []
        # Deduplicated, and never repeating the display name: several steps
        # can arrive at the same alternative — the store search matched on it
        # and the store details returned it — and one name listed twice is
        # scored twice for no gain.
        seen, uniq = {self.name}, []
        for n in self.alt_names:
            n = _decode_entities(n)
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        self.alt_names = uniq
        # Sanitise web-sourced text so every source (Steam, Wikipedia,
        # DLsite, itch, generic web…) stores clean values — decode entities
        # for name/developer/publisher, and additionally strip the DLsite
        # signature + leaked tags from the description.
        self.name = _decode_entities(self.name)
        self.developer = _decode_entities(self.developer)
        self.publisher = _decode_entities(self.publisher)
        self.description = _clean_description(self.description)


def _fetch_json(url: str, timeout: int = 10, headers: Optional[dict] = None) -> Optional[dict]:
    """Fetch JSON from URL."""
    try:
        if headers is None:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        req = urllib.request.Request(url, headers=headers)
        with _open_url(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


def _expand_search_terms(name: str) -> list[str]:
    """Generate search term variations, including word subsets.

    Progressive subsets (dropping trailing/leading words one at a time)
    help find games whose official name differs slightly from the query
    — e.g. "Super Story Online Idle RPG" can match "Super Story – Idle
    Clicker Online RPG" via the subset "Super Story Idle RPG".
    """
    terms = [name]
    # Add spaces between lowercase/uppercase transitions
    expanded = ""
    for char in name:
        if char.isupper() and expanded and expanded[-1].islower():
            expanded += " " + char
        else:
            expanded += char
    if expanded != name:
        terms.append(expanded)
    # Add underscore variations
    terms.append(name.replace(" ", "_"))
    # Progressive word subsets — only for multi-word queries
    words = name.split()
    if len(words) > 2:
        used = set(terms)
        # Drop trailing words one at a time (down to 1 word)
        for i in range(len(words) - 1, 0, -1):
            subset = " ".join(words[:i])
            if subset not in used:
                used.add(subset)
                terms.append(subset)
        # Drop leading words one at a time (minimum 2 words)
        for i in range(1, len(words) - 1):
            subset = " ".join(words[i:])
            if len(subset.split()) >= 2 and subset not in used:
                used.add(subset)
                terms.append(subset)
    return terms


# Generic executable stems — shared with save_detector.
# When a game name IS exactly one of these (after cleaning), the name is
# uninformative (e.g. auto-filled from "game.exe") and should not be sent
# to any API. The check is exact — "game" is filtered but "Game Breaker" is not.
_GENERIC_EXE_STEMS = frozenset(_GENERIC_EXE_STEMS_LIST)

# Parenthetical disambiguators that mark a NON-game work — a film, TV series,
# band/album/song, novel, etc. Encyclopedic sources (Wikipedia, Fandom) and
# generic web results use these to distinguish same-named works, so a game
# metadata search must reject them: searching a title should never return the
# movie/TV/band of the same name. Genuine game markers — "(video game)",
# "(visual novel)" — are deliberately NOT listed, so they still pass. An
# optional leading year is tolerated ("(2009 film)"), while a bare year with
# no media word ("(2009)") is left alone (it may be a game's release year).
_NON_GAME_MEDIA_RE = re.compile(
    r'\('
    r'(?:\d{4}\s+)?'
    r'(?:'
    r'film|movie|motion picture|short film|film series|'
    r'tv series|television series|tv program(?:me)?|mini[\s-]?series|web series|'
    r'band|musical group|album|song|single|soundtrack|ep|opera|musical|'
    r'novel|light novel|book|manga|manhwa|anime|comics?|graphic novel|play'
    r')'
    r'[^)]*\)',
    re.IGNORECASE,
)


def _is_non_game_media_title(title: str) -> bool:
    """True if *title* carries a parenthetical film/TV/band/album/novel
    disambiguator — a non-game work a game search must reject. Genuine game
    markers such as "(video game)"/"(visual novel)" are NOT matched."""
    return bool(title and _NON_GAME_MEDIA_RE.search(title))


# A favicon / site-logo / tiny site-icon must never be used as a game cover:
# forum/thread pages often set og:image to their favicon, so a scrape
# would otherwise "extract" a 32x32 site icon. Matches favicon/apple-touch-icon
# style names and tiny WxH markers (≤64px, e.g. "favicon-32x32").
_FAVICON_IMG_RE = re.compile(
    r'(?:favicon|apple-touch-icon|mstile|android-chrome|site[-_]?icon|/icons?/'
    r'|[-_/](?:16|24|32|48|64)x(?:16|24|32|48|64)(?:[-_.]|$))',
    re.IGNORECASE,
)


def _is_favicon_like(image_url: str) -> bool:
    """True if *image_url* looks like a favicon / site logo / tiny icon rather
    than a real cover image."""
    return bool(image_url and _FAVICON_IMG_RE.search(image_url))


# Forum thread pages (game-forum threads in general) cram everything
# into one labelled og:description — "Overview: … Developer: … Release Date: …
# Tags: …" — with CONSTANT labels. Split it into the proper fields instead of
# dumping the whole (truncated) blob into the description. The label set is
# the EXPLICIT reference for what fills which field — parsing is by these
# labels, never by character position alone.
_FORUM_LABEL_RE = re.compile(
    r'\b(overview|description|story|synopsis|plot'
    r'|developer\s*/\s*publisher|developers?|publishers?|artist|author|circle|dev'
    r'|release\s*date|released|thread\s*updated|updated'
    r'|original\s+title|game\s+name|version|status|engine|platform|os'
    r'|censorship|censored|language|translator|resolution|voiced?'
    r'|tags?|genres?|installation|change-?log)\s*:',
    re.IGNORECASE,
)

# Labels of the overview/description family — the segment that IS the
# description (and, as a prefix, must never leak into it).
_FORUM_OVERVIEW_KEYS = ('overview', 'description', 'story', 'synopsis', 'plot')
# Labels that mark a STRUCTURED info block (a forum thread header) as opposed
# to words that can plausibly appear with a colon inside ordinary prose.
# Deliberately excludes the overview family, 'updated' and 'story'-like words:
# at least one of THESE must be present before a label-less description is
# reinterpreted as a forum blob.
_FORUM_STRUCT_KEYS = frozenset((
    'developer/publisher', 'developer', 'developers', 'dev', 'publisher',
    'publishers', 'artist', 'author', 'circle', 'version', 'release date',
    'released', 'thread updated', 'censorship', 'censored', 'language', 'os',
    'engine', 'platform', 'status', 'tags', 'tag', 'genres', 'genre',
    'installation', 'translator', 'original title', 'game name', 'changelog',
    'change-log', 'resolution', 'voice', 'voiced',
))


# Link-service words a thread header appends after the developer name
# ("DevName Patreon - Discord - Itch.io ..."). Matched as a whole word; the
# cut only applies when the word is NOT the first (see usage below).
_FORUM_DEV_LINKS_RE = re.compile(
    r'\b(patreon|discord|subscribestar|substar|itch\.io|itch|steam|twitter'
    r'|x\.com|website|wiki|boosty|ci-en|fanbox|pixiv|linktr\.ee|linktree)\b',
    re.IGNORECASE,
)

# Placeholder text a forum injects where registration-gated links live
# ("You must be registered to see the links"). After tag-stripping it lands
# INSIDE field values ("Developer: PixelForge You must be registered …") and
# the overview, so it is scrubbed from the whole text before parsing.
# Matched with explicit phrase endings, NEVER an open-ended tail — a greedy
# tail would swallow the following field label ("… the links Version: 0.8"
# must lose only the placeholder, not "Version").
_FORUM_GATED_RE = re.compile(
    r'(you must be (?:registered|logged\s?in)'
    r'(?:\s+(?:in\s+order\s+)?to\s+(?:see|view|access|download))?'
    r'(?:\s+(?:the|this|these|that))?'
    r'(?:\s+(?:links?|contents?|images?|media|spoilers?|attachments?|hidden\s+content))?'
    r'|log\s?in\s+or\s+register'
    r'(?:\s+now)?'
    r'(?:\s+to\s+(?:see|view|reply|access|download))?'
    r'(?:\s+(?:the|this|these))?'
    r'(?:\s+(?:links?|contents?|images?|media|attachments?|hidden\s+content))?'
    r')',
    re.IGNORECASE,
)

# Explicit date shapes seen in thread headers: 2025-10-30 / 30.10.2025 /
# "30 Oct 2025" / "Oct 30, 2025" / bare year.
_FORUM_DATE_RE = re.compile(
    r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}'
    r'|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}'
    r'|\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}'
    r'|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}'
    r'|(?:19|20)\d{2})'
)


def _parse_forum_description(text: str) -> dict:
    """Split a forum-thread og:description into fields by its inline labels.

    Returns {overview, developer?, release_date?, tags?} when the text is a
    labelled forum blob, {} otherwise (ordinary prose is left untouched).

    Recognised shapes:
      - "Overview: … Developer: … Tags: …"  — classic labelled blob;
      - "Overview: …" alone at the START     — the label is stripped, the rest
        is the description (a lone label mid-prose never triggers);
      - "<description text> Developer: … Version: …" — the description
        PRECEDES the labels: the preamble becomes the overview, provided at
        least one structural label (developer/version/tags/…) confirms the
        text is a thread-header blob and not prose;
      - "Developer: … Version: …" with no description at all — fields are
        extracted and overview comes back EMPTY, so a labels-only blob stops
        masquerading as a description."""
    if not text:
        return {}
    # Scrub registration-gate placeholders BEFORE locating labels, so they
    # pollute neither the overview nor any field value.
    text = re.sub(r'\s{2,}', ' ', _FORUM_GATED_RE.sub(' ', text)).strip()
    labels = list(_FORUM_LABEL_RE.finditer(text))
    if not labels:
        return {}

    def _norm_key(raw: str) -> str:
        return re.sub(r'\s*/\s*', '/', re.sub(r'\s+', ' ', raw.lower())).strip()

    preamble = text[:labels[0].start()].strip().strip('.,;·|-–— ')
    # A single label buried inside ordinary prose is not a forum blob —
    # leave the text untouched. A single LEADING label still parses
    # ("Overview: …" alone must lose its label).
    if len(labels) == 1 and preamble:
        return {}

    seg: dict = {}
    for i, m in enumerate(labels):
        key = _norm_key(m.group(1))
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(text)
        seg[key] = text[start:end].strip().strip('.,;·|-–— ')

    has_struct = any(k in seg for k in _FORUM_STRUCT_KEYS)
    overview = next((seg[k] for k in _FORUM_OVERVIEW_KEYS if seg.get(k)), None)
    if overview is not None and not has_struct and preamble:
        # Overview-family + weak labels ("story:", "updated:") buried after
        # prose — that's a sentence, not a thread header. A blob is only
        # trusted without structural labels when it STARTS with the label.
        return {}
    if overview is None:
        if not has_struct:
            return {}          # colon-words in prose, not a thread header
        if len(preamble) >= 30:
            # The real description precedes the labels — use it.
            overview = preamble
        elif len(labels) >= 2 and not preamble:
            # Labels-only blob: extract the fields, description stays empty.
            overview = ''
        else:
            return {}

    out: dict = {'overview': overview}
    dev = next((seg[k] for k in ('developer/publisher', 'developer',
                                 'developers', 'dev', 'artist', 'author',
                                 'circle', 'publisher', 'publishers')
                if seg.get(k)), None)
    if dev:
        # Thread headers routinely append the developer's link labels to the
        # name ("PixelForge Patreon - Discord - Itch.io") — cut at the first
        # such word, unless it IS the first word (a studio actually named so).
        m = _FORUM_DEV_LINKS_RE.search(dev)
        if m and m.start() > 0:
            dev = dev[:m.start()]
        out['developer'] = dev.strip(' -–—|·,;')[:80].strip()
    rel = next((seg[k] for k in ('release date', 'released', 'thread updated',
                                 'updated') if seg.get(k)), None)
    if rel:
        # Prefer an explicit date token over the raw segment — the segment
        # runs to the next label (or end of text) and can drag prose along.
        m = _FORUM_DATE_RE.search(rel)
        out['release_date'] = m.group(1) if m else rel[:40].strip()
    tagstr = next((seg[k] for k in ('tags', 'tag', 'genres', 'genre') if seg.get(k)), None)
    if tagstr:
        # Individual tags are short — an overlong "tag" is the post's trailing
        # prose swallowed after the last comma, never a real tag. Stray
        # quotes around the list (spoiler text nodes) are shed per token.
        _toks = (t.strip(' \'"“”‘’') for t in re.split(r'[,;/|]', tagstr))
        out['tags'] = [t for t in _toks if t and len(t) <= 40][:16]
    return out


# Words a release folder carries that are never part of a game's title: what
# language the files are in, whether they were machine-translated, whether
# the art was uncensored. The same class as the version markers stripped
# below, and left in they do real damage — they are searched for as if they
# were words of the title, and a stray Latin one on an otherwise Japanese
# name also makes it look like a name this module can score, when it cannot.
#
# Spelled out rather than pattern-matched, and three letters or more: "EN",
# "JP" and "CN" are too short to strike out of somebody's title on suspicion.
#
# Several of these are also ordinary English words that begin real titles —
# a game may genuinely be called "Censored …", "Crack …", "Patched …". They
# are safe to list because a marker is never stripped from the FIRST word of
# a name: a release folder carries them as suffixes, and a title carries them
# as its opening word. Position tells the two apart, which is better than
# dropping the word from the list and never stripping it at all.
#
# "DLC" is deliberately absent: it does not describe the same files in
# another language, it describes different content.
_RELEASE_MARKERS = frozenset({
    "jap", "jpn", "eng", "chs", "cht", "kor", "rus",
    "mtl", "unc", "uncen", "uncensored", "decensored", "censored",
    "repack", "cracked", "patched", "crack", "hotfix",
})

# Words that are release noise only in company. A release folder writes one
# of these beside something that is already noise — a version, a language, a
# publisher — while a game genuinely named with the word has ordinary words
# either side of it. Struck out on its own, each would take a word off some
# real title, so each is struck out only when a neighbour is noise too.
#
# "Steam" is here rather than among the trailing markers for exactly that
# reason: it names the store, but it is also an ordinary English word, and a
# title can plausibly end on it. "Kagura" and "GOG" cannot, so they need no
# such care and are taken off the end unconditionally.
_CONTEXTUAL_MARKERS = frozenset({
    "fix", "fixed", "steam",
})

# Who a release came from — the store it was bought from, or the publisher
# who localised it. Stripped ONLY from the end of a name, and that
# restriction is the whole point: these are ordinary words that appear in the
# middle of real titles, and stripping one there would cut a game's name in
# half. Where the word sits is what separates the title from the label stuck
# on the end of it.
_TRAILING_MARKERS = frozenset({
    "kagura", "gog",
    # Concatenated store label (MangaGamer / mangagamer). The spaced form is
    # handled by _TWO_WORD_TRAILING — "manga" or "gamer" alone must never
    # come off a title that happens to end on either word.
    "mangagamer",
})

# "Hot fix" written as two words, folded into the one word the marker list
# already knows. Only ever as that pair: "hot" on its own opens plenty of
# real titles and is never touched.
_TWO_WORD_MARKERS = re.compile(r"\bhot[\s._-]+fix\b", re.IGNORECASE)

# Publisher / store labels that are two words and must be stripped ONLY as
# that pair, and ONLY from the end. "Some Game Manga Gamer" loses the store;
# "Manga Quest" and "The Last Gamer" keep their titles.
_TWO_WORD_TRAILING = re.compile(
    r"(?:^|[\s._-]+)manga[\s._-]+gamer\s*$",
    re.IGNORECASE,
)


def _is_punctuation(w: str) -> bool:
    """A token that is only a separator — a dash, a bullet, a bar."""
    return not any(ch.isalnum() for ch in w)


def _is_version_token(w: str) -> bool:
    """A version string — needs a v/b prefix or a decimal point, so the bare
    sequel number in "Example Game 1" survives."""
    return bool(re.match(r'^(?:[vb]\d+(?:\.\d+)*|\d+\.\d+)$', w, re.IGNORECASE))


def _is_product_code(w: str) -> bool:
    """A shop's product code — letters then digits, or a long bare number."""
    return bool(re.match(r'^(?:[A-Z]{2,4}\d{4,15}|\d{6,15})$', w))


def _drop_contextual_markers(text: str) -> str:
    """Remove the words that are release noise only in company.

    Run BEFORE the version markers are taken out, because a version beside a
    word is one of the things that identifies it: "<title> v1.0 fix" is a
    fixed release, while "<title> Fix" is a game with that word in its name,
    and by the time the version has been removed the two look alike.

    A word qualifies when a neighbour is itself noise — a version, a product
    code, a language marker, a store. Repeated to a fixed point, so a run
    resolves from the outside in: the one beside the version settles first,
    and only then can it settle the next.
    """
    words = [w for w in text.split() if w]
    noise = [False] * len(words)
    for i, w in enumerate(words):
        bare = w.strip("-_.").lower()
        noise[i] = bool(
            _is_version_token(w) or _is_product_code(w)
            or (i > 0 and bare in _RELEASE_MARKERS)
            or (i > 0 and bare in _TRAILING_MARKERS))

    def neighbour(i: int, step: int):
        """The nearest word either side that is a word at all.

        A lone dash between two of these is punctuation, not a neighbour, and
        treating it as one would hide a version sitting right behind it. What
        counts as punctuation is "carries no letter or digit", rather than a
        list of the characters people separate things with — that list would
        have to be right about every dash there is.
        """
        j = i + step
        while 0 <= j < len(words):
            if not _is_punctuation(words[j]):
                return j
            j += step
        return None

    changed = True
    while changed:
        changed = False
        for i, w in enumerate(words):
            if noise[i] or i == 0:
                continue
            if w.strip("-_.").lower() not in _CONTEXTUAL_MARKERS:
                continue
            before, after = neighbour(i, -1), neighbour(i, 1)
            if (before is not None and noise[before]) or \
                    (after is not None and noise[after]):
                noise[i] = True
                changed = True

    kept = [w for i, w in enumerate(words)
            if not (noise[i]
                    and w.strip("-_.").lower() in _CONTEXTUAL_MARKERS)]
    return " ".join(kept)


def _clean_game_name(game_name: str) -> str:
    """Strip brackets, version strings and product codes from a game name.
    
    Returns a single cleaned name suitable for API queries.  The caller
    may further expand it via _expand_search_terms if multiple query
    variations are desired.
    """
    stripped = re.sub(r'[\[\]\(\)\{\}]', ' ', game_name).strip()
    stripped = _TWO_WORD_MARKERS.sub("hotfix", stripped)
    # Shared normalizer handles multi-word markers too ("version 2",
    # "Build 15") and the b/build family — bare sequel numbers survive.
    from core.constants import strip_version_tokens
    # Before the version markers are taken out, because a version is exactly
    # the sort of neighbour that identifies one of these — see _in_company.
    stripped = _drop_contextual_markers(stripped)
    stripped = strip_version_tokens(stripped)
    filtered = []
    for w in stripped.split():
        w_clean = w.strip()
        if not w_clean:
            continue
        if _is_version_token(w_clean) or _is_product_code(w_clean):
            continue
        # Release markers, but never the opening word — see _RELEASE_MARKERS.
        if filtered and w_clean.strip("-_.").lower() in _RELEASE_MARKERS:
            continue
        filtered.append(w_clean)
    # Store and publisher labels come off the END, and keep coming off while
    # the end is one: a name can carry two of them, or a label followed by a
    # language marker the pass above has just removed. Never the first word,
    # which is the title itself.
    while len(filtered) > 1 and (
            filtered[-1].strip("-_.").lower() in _TRAILING_MARKERS
            # …and the separator a removed label leaves behind with it.
            or _is_punctuation(filtered[-1])):
        filtered.pop()
    cleaned = ' '.join(filtered).strip(" ._-")
    # Two-word trailing labels (Manga Gamer): only the whole pair, never the
    # individual words. Applied after the one-word pass so a concatenated
    # "mangagamer" token is already gone and this only sees the spaced form.
    # An empty result means the whole name WAS the label — keep it, same as
    # the one-word fallback below.
    _tw = _TWO_WORD_TRAILING.sub("", cleaned).strip(" ._-")
    if _tw:
        cleaned = _tw
    # Never strip a name down to nothing: a game genuinely called by one of
    # these words keeps it, since having the wrong name is still better than
    # having none to search with.
    return cleaned or stripped.strip()


def _build_search_queries(game_name: str) -> list[str]:
    """Build expanded search query variations.

    Strips bracket characters, filters version strings and product codes
    (RJ#####, etc.), then builds expanded search term variations.
    Whole-name stop-word filtering is handled at a higher level (in
    search_game_info) so "game" alone never reaches here.
    """
    clean = _clean_game_name(game_name)
    if not clean:
        return []
    return _expand_search_terms(clean)




def _release_year(info: "GameInfo") -> str:
    """Extract a 4-digit year from a GameInfo.release_date string, if any."""
    m = re.search(r'(19|20)\d{2}', info.release_date or '')
    return m.group(0) if m else ''


_NOISE_LANG = frozenset({
    "eng", "en", "english", "ita", "it", "italian", "jap", "jp", "jpn", "ja",
    "japanese", "fr", "fra", "fre", "french", "de", "deu", "ger", "german",
    "es", "esp", "spa", "spanish", "ru", "rus", "russian", "cn", "chn", "zh",
    "chs", "cht", "chinese", "kr", "kor", "ko", "korean", "pt", "ptbr", "br",
    "portuguese", "multi", "multilang", "multilanguage",
})
_NOISE_PLATFORM = frozenset({
    "pc", "win", "win32", "win64", "windows", "mac", "macos", "osx",
    "android", "apk", "linux", "ios", "x86", "x64",
})
_NOISE_TAG = frozenset({
    "uncensored", "censored", "decensored", "premium", "complete", "completed",
    "full", "demo", "trial", "test", "beta", "alpha", "final", "deluxe",
    "goty", "remaster", "remastered", "repack", "cracked", "crack", "patched",
    "dlc", "edition", "standalone", "portable", "rip",
})
_RELEASE_NOISE = _NOISE_LANG | _NOISE_PLATFORM | _NOISE_TAG


def _strip_release_noise(name: str, drop_version: bool = False) -> str:
    """Strip release/packaging decorations from a title.

    Removes ``[...]`` scene-tag groups ("[ENG]", "[Uncensored]"), treats
    ``-``/``_`` and remaining brackets as word separators, then drops a
    TRAILING run of language/platform/edition-tag tokens (ENG, PC,
    Uncensored, …). Only the trailing run is removed, so a leading real word
    that collides with the noise vocabulary survives (a title that merely
    starts with a noise word such as "PC", "Test" or "Final"). With
    *drop_version* the
    software version/build markers are removed too — targeted-site queries
    want the bare title, while identity de-dup keeps the version as a
    disambiguator between different games that share a name.

    Falls back to the original name when stripping would empty it.
    """
    if not name:
        return name
    s = re.sub(r'\[[^\]]*\]', ' ', name)              # scene-tag groups
    if drop_version:
        from core.constants import strip_version_tokens
        s = strip_version_tokens(s)
    s = re.sub(r'[\(\)\{\}\[\]\-_]', ' ', s)          # brackets + separators
    tokens = s.split()
    while tokens and tokens[-1].lower() in _RELEASE_NOISE:
        tokens.pop()
    return ' '.join(tokens).strip() or name


def _dedupe_slug(name: str) -> str:
    """Identity slug for de-dup: title with release noise stripped."""
    return _fuzzy_slug(_strip_release_noise(name))


def _dedupe_candidates(cands: list[tuple["GameInfo", float]],
                       limit: int = 3) -> list[tuple["GameInfo", float]]:
    """Drop duplicate hits from a best-first candidate list.

    A shared NAME alone is never enough to hide a candidate. Two rules:

    * Exact same store URL → the same entry, collapsed regardless of source.
    * Otherwise de-dup only ACROSS DIFFERENT sources, and only with positive
      corroboration: the titles must normalize to the same identity slug
      (release noise — language / platform / edition tags — stripped) AND at
      least one of developer / release-year must be present on both sides and
      agree. One field may be absent (identity degrades gracefully), but
      name-alone never merges, and a present field that disagrees keeps both.
      Two same-named results from the SAME source are always both kept — a
      store legitimately lists different games under one name.

    Capped at *limit* — the result feeds a user-facing picker, not another
    ranking pass: the input is already sorted best-first, so the cap keeps
    the top-scored titles and discards the lower-ranked tail.
    """
    kept: list[tuple[GameInfo, float]] = []
    # (name_slug, dev_slug, year, source, url) per kept entry
    kept_meta: list[tuple[str, str, str, str, str]] = []
    for info, score in cands:
        slug = _dedupe_slug(info.name)
        dev_slug = _fuzzy_slug(info.developer or '')
        year = _release_year(info)
        src = (info.source or '').strip().lower()
        url = (info.store_url or '').strip().rstrip('/').lower()
        is_dup = False
        for k_slug, k_dev, k_year, k_src, k_url in kept_meta:
            if url and k_url and url == k_url:
                is_dup = True          # identical link — same entry
                break
            if src and k_src and src == k_src:
                continue               # same source: always keep both
            if not slug or slug != k_slug:
                continue               # different title: not a duplicate
            dev_match = bool(dev_slug and k_dev and dev_slug == k_dev)
            year_match = bool(year and k_year and year == k_year)
            if dev_match or year_match:
                is_dup = True          # cross-source, positively identified
                break
        if is_dup:
            continue
        kept.append((info, score))
        kept_meta.append((slug, dev_slug, year, src, url))
        if len(kept) >= limit:
            break
    return kept


