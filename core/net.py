"""
SaveSync - Shared HTTPS opener with certificate-store fallback.

Some Windows installations carry an outdated copy of a root/intermediate CA
in the system store and reject a site's perfectly fresh certificate chain
with "certificate has expired" (observed with api.vndb.org and
en.wikipedia.org on the new Let's Encrypt hierarchy, while browsers — which
ship their own trust stores — load the same sites fine).

open_url() tries the preferred SSL context first and, on a certificate
VERIFICATION failure only, retries once with the alternative store
(OS default ⇄ bundled certifi). Whichever store succeeds is promoted to
preferred for the rest of the process, so affected hosts don't pay a failed
handshake on every call. Verification itself is never disabled.
"""
import logging
import ssl
import threading
from collections import OrderedDict
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def image_fetch_request(url: str) -> tuple[urllib.request.Request, str]:
    """Build a Request for fetching a candidate/cover image with the
    headers real-world hosts actually require, plus the URL it actually
    points at (the /thumb/ rewrite below can change it — callers that
    cache by URL need the same string the bytes actually came from).

    A bare, header-less request 403s or silently serves a placeholder on
    many forum attachment CDNs, which require the parent site as Referer
    (attachments.example.com -> example.com); those hosts also often force
    a tiny thumbnail at a /thumb/ path even when the full attachment sits
    one path segment away. Used by both the confirmed-download path (which
    had this logic) and the candidate-preview thumbnail fetch (which
    didn't — a plain request there silently failed on exactly these hosts,
    so the preview stayed blank even though confirming and downloading for
    real, through the header-aware path, worked)."""
    parts = urllib.parse.urlsplit(url)
    referer = f"{parts.scheme}://{parts.netloc}/"
    host = (parts.netloc or "").lower()
    if host.startswith("attachments."):
        origin = host.split(".", 1)[-1]
        referer = f"https://{origin}/"
    if "/thumb/" in (parts.path or ""):
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc,
             (parts.path or "").replace("/thumb/", "/", 1),
             parts.query, parts.fragment)
        )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _IMAGE_UA,
            "Accept": "image/jpeg,image/png,image/webp,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
    )
    return req, url

_lock = threading.Lock()
# None = OS default trust store; an SSLContext = the certifi-backed store.
_preferred_ctx: ssl.SSLContext | None = None
_certifi_ctx: ssl.SSLContext | None = None
_certifi_unavailable = False


def _get_certifi_context() -> ssl.SSLContext | None:
    """Build (once) an SSLContext anchored on certifi's CA bundle."""
    global _certifi_ctx, _certifi_unavailable
    if _certifi_ctx is not None or _certifi_unavailable:
        return _certifi_ctx
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception as e:          # certifi missing or unreadable bundle
        logger.debug(f"certifi CA bundle unavailable: {e}")
        _certifi_unavailable = True
        return None
    with _lock:
        if _certifi_ctx is None:
            _certifi_ctx = ctx
    return _certifi_ctx


def _is_cert_verify_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError)


def open_url(url_or_req, timeout: float = 10):
    """urllib.request.urlopen with automatic trust-store fallback.

    Raises exactly what urlopen raises; the fallback only engages on
    certificate-verification failures and never weakens verification.
    """
    global _preferred_ctx
    preferred = _preferred_ctx
    try:
        return urllib.request.urlopen(url_or_req, timeout=timeout, context=preferred)
    except (urllib.error.URLError, ssl.SSLCertVerificationError) as e:
        if not _is_cert_verify_error(e):
            raise
        # Alternative store: certifi when the OS store failed, and vice versa
        alt = _get_certifi_context() if preferred is None else None
        if alt is preferred:        # no certifi available → nothing to try
            raise
        resp = urllib.request.urlopen(url_or_req, timeout=timeout, context=alt)
        with _lock:
            _preferred_ctx = alt
        which = "bundled certifi CA store" if alt is not None else "OS trust store"
        logger.info(
            f"Certificate store rejected a valid-looking chain — switched to {which} "
            f"for subsequent HTTPS requests ({e})"
        )
        return resp


# ── Pictures already downloaded this session ────────────────────────────────
#
# A game's picture used to be fetched once for the candidate preview, once more
# for the chip dialog's thumbnail and once more when Apply saved it — and the
# preview dialog is rebuilt on every Back, so even the first was repeated. The
# bytes of a picture that has arrived whole are kept here, in memory, and the
# next place that wants the same URL is handed them. Nothing is written to disk
# from here: what gets SAVED is still decided (and re-encoded) by the dialog.
#
# Only what is safe to save is admitted: a complete body, within the size cap,
# that starts like an image. A truncated read or an error page served with a 200
# must never be handed to a caller that would write it down as the cover.

IMAGE_FETCH_LIMIT = 16 * 1024 * 1024          # a single picture, bytes
_IMAGE_CACHE_BUDGET = 64 * 1024 * 1024        # all of them together
_image_cache: "OrderedDict[str, bytes]" = OrderedDict()
_image_cache_bytes = 0
_image_cache_lock = threading.Lock()


def looks_like_image(data: bytes) -> bool:
    """True when *data* starts with the signature of a picture format."""
    if not data or len(data) < 12:
        return False
    head = data[:16]
    return (head[:3] == b"\xff\xd8\xff"                       # JPEG
            or head[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
            or head[:6] in (b"GIF87a", b"GIF89a")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[:2] == b"BM"                              # BMP
            or head[:4] == b"\x00\x00\x01\x00"                # ICO
            or (head[4:8] == b"ftyp" and b"avi" in data[8:16]))   # AVIF


def _image_key(url: str) -> str:
    url = (url or "").strip()
    return "https:" + url if url.startswith("//") else url


def cached_image(url: str):
    """``(url the bytes came from, bytes)`` for a picture already downloaded
    this session, or None. Found by the URL as asked for and by the URL the
    request is really made to (the /thumb/ rewrite of image_fetch_request)."""
    url = _image_key(url)
    if not url.startswith("http"):
        return None
    keys = [url]
    try:
        resolved = image_fetch_request(url)[1]
        if resolved != url:
            keys.insert(0, resolved)
    except Exception:
        pass
    with _image_cache_lock:
        for k in keys:
            data = _image_cache.get(k)
            if data is not None:
                _image_cache.move_to_end(k)
                return k, data
    return None


def remember_image(url: str, resolved_url: str, data: bytes) -> bool:
    """Keep a picture that arrived whole. Filed under the URL it was asked for
    AND the one it came from; False (nothing kept) when it is not safe to save
    as is — too big, or not an image."""
    global _image_cache_bytes
    if not data or len(data) > IMAGE_FETCH_LIMIT or not looks_like_image(data):
        return False
    with _image_cache_lock:
        for k in dict.fromkeys((_image_key(resolved_url), _image_key(url))):
            if not k:
                continue
            old = _image_cache.pop(k, None)
            if old is not None:
                _image_cache_bytes -= len(old)
            _image_cache[k] = data
            _image_cache_bytes += len(data)
        while _image_cache_bytes > _IMAGE_CACHE_BUDGET and _image_cache:
            _k, dropped = _image_cache.popitem(last=False)
            _image_cache_bytes -= len(dropped)
    return True


def fetch_image(url: str, timeout: float = 20):
    """``(url fetched, bytes)`` for a picture — from the session's store when it
    is there, from the network otherwise (and then kept). None when it cannot
    be had or the host did not answer with a picture. Safe on a worker thread."""
    hit = cached_image(url)
    if hit is not None:
        return hit
    url = _image_key(url)
    if not url.startswith("http"):
        return None
    try:
        req, resolved = image_fetch_request(url)
        with open_url(req, timeout=timeout) as response:
            ct = response.headers.get("Content-Type", "")
            if ct and not ct.startswith("image/") and "octet-stream" not in ct:
                logger.warning(f"Not an image: Content-Type {ct!r} for {resolved!r}")
                return None
            data = response.read(IMAGE_FETCH_LIMIT + 1)
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
        return None
    if len(data) > IMAGE_FETCH_LIMIT:
        logger.warning(f"Picture over {IMAGE_FETCH_LIMIT // (1024 * 1024)} MB not used: {resolved!r}")
        return None
    logger.info(f"Downloaded {len(data)}B, Content-Type: {ct!r}")
    remember_image(url, resolved, data)
    return resolved, data
