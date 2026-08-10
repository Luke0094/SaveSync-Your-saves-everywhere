"""Check GitHub Releases for a newer SaveSync build.

The shipped Windows build is a onefile executable, so there is no in-place
updater — a newer release is named and the person is pointed at the download
page. Polling is roughly every twelve hours, with jitter on the interval, so
installs do not all hit the API on the same clock (and a steady exact cadence
is harder to mistake for abusive traffic).
"""
from __future__ import annotations

import json
import logging
import random
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.constants import (
    APP_NAME,
    APP_VERSION,
    GITHUB_RELEASES_API,
    GITHUB_RELEASES_URL,
)

logger = logging.getLogger(__name__)

# ~12 h centre, ±2 h → between 10 h and 14 h between attempts.
_BASE_INTERVAL_S = 12 * 60 * 60
_JITTER_S = 2 * 60 * 60
# First look after startup is delayed and jittered so it stays clear of the
# launch burst and is not the same second on every machine.
_FIRST_DELAY_MIN_MS = 90 * 1000
_FIRST_DELAY_MAX_MS = 4 * 60 * 1000

_UA = f"{APP_NAME}/{APP_VERSION} (+{GITHUB_RELEASES_URL})"


@dataclass(frozen=True)
class ReleaseInfo:
    """One published GitHub release that is newer than the running build."""
    version: str
    tag: str
    name: str
    body: str
    html_url: str


def next_interval_seconds() -> int:
    """Seconds until the next check — ~12 h with a couple of hours of jitter."""
    return random.randint(_BASE_INTERVAL_S - _JITTER_S,
                          _BASE_INTERVAL_S + _JITTER_S)


def first_delay_ms() -> int:
    """How long after startup before the first due check may run."""
    return random.randint(_FIRST_DELAY_MIN_MS, _FIRST_DELAY_MAX_MS)


def normalize_version(value: str) -> tuple:
    """Comparable version tuple from a tag or APP_VERSION string."""
    text = (value or "").strip().lstrip("vV")
    parts = [int(p) for p in re.split(r"[^\d]+", text) if p.isdigit()]
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return normalize_version(remote) > normalize_version(local)


def fetch_latest_release(timeout: float = 15.0) -> Optional[ReleaseInfo]:
    """Latest non-draft, non-prerelease release, or None on any failure."""
    req = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={
            "User-Agent": _UA,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug(f"Update check could not reach GitHub: {e}")
        return None
    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV").strip() or tag
    return ReleaseInfo(
        version=version,
        tag=tag,
        name=str(data.get("name") or f"{APP_NAME} {tag}").strip(),
        body=str(data.get("body") or "").strip(),
        html_url=str(data.get("html_url") or GITHUB_RELEASES_URL).strip()
                 or GITHUB_RELEASES_URL,
    )


def is_check_due(config) -> bool:
    """Whether enough jittered time has passed since the last attempt."""
    last = (config.get("update_check_last", "") or "").strip()
    if not last:
        return True
    interval = int(config.get("update_check_interval_sec") or _BASE_INTERVAL_S)
    interval = max(_BASE_INTERVAL_S - _JITTER_S, interval)
    try:
        then = datetime.fromisoformat(last)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - then).total_seconds()
        return elapsed >= interval
    except (ValueError, TypeError):
        return True


def mark_check_attempted(config) -> None:
    """Record that a check ran (success or quiet failure) and roll the jitter."""
    config.set("update_check_last",
               datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    config.set("update_check_interval_sec", next_interval_seconds())


def should_notify(info: ReleaseInfo, config, local: str = APP_VERSION) -> bool:
    """True when *info* is newer than this build and not already shown."""
    if not is_newer(info.version, local):
        return False
    seen = (config.get("update_notified_version", "") or "").strip()
    if seen and not is_newer(info.version, seen):
        return False
    return True


def mark_notified(config, version: str) -> None:
    config.set("update_notified_version",
               (version or "").strip().lstrip("vV"))
