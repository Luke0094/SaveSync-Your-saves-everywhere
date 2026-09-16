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
