"""
SaveSync - Internationalization Engine
Supports live language switching without application restart.
"""
import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

_I18N_DIR = Path(__file__).parent / "locales"


class I18nEngine(QObject):
    """Central i18n manager. Emits language_changed when locale is switched."""
    language_changed = Signal(str)  # new locale code

    def __init__(self):
        super().__init__()
        self._locale: str = "en"
        self._strings: dict = {}
        self._fallback: dict = {}
        self._observers: list[Callable] = []
        self._lock = threading.RLock()
        self._initializing = True  # suppress signal during construction
        self._load_fallback()
        self.set_locale(self._locale)
        self._initializing = False

    def _load_fallback(self):
        path = _I18N_DIR / "en.json"
        if not path.exists():
            logger.error(f"Fallback locale file missing: {path}")
            return
        try:
            with open(path, encoding="utf-8") as f:
                self._fallback = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load fallback locale: {e}")

    def set_locale(self, locale: str):
        path = _I18N_DIR / f"{locale}.json"
        if not path.exists():
            logger.warning(f"Locale file not found: {path}, falling back to 'en'")
            if locale != "en":
                self.set_locale("en")
            return
        # Load file outside lock to minimize lock hold time
        try:
            with open(path, encoding="utf-8") as f:
                new_strings = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load locale '{locale}': {e}, falling back to 'en'")
            if locale != "en":
                self.set_locale("en")
            return
        # Update state under lock, emit signal OUTSIDE lock to avoid deadlock.
        # During __init__, skip the emit to prevent deadlock if a connected
        # slot calls t() → get_engine() before the singleton is assigned.
        with self._lock:
            self._strings = new_strings
            self._locale = locale
        if not getattr(self, '_initializing', False):
            self.language_changed.emit(locale)

    def t(self, key: str, **kwargs) -> str:
        """Translate a key with optional format arguments."""
        parts = key.split(".")
        with self._lock:
            val = self._strings
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    val = None
                    break
            if val is None:
                val = self._fallback
                for p in parts:
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
        if val is None:
            logger.debug(f"Missing translation key: {key}")
            return key
        if kwargs:
            try:
                return str(val).format(**kwargs)
            except (KeyError, ValueError) as e:
                logger.debug(f"Translation format error for key '{key}': {e}")
                return str(val)
        return str(val)

    @property
    def locale(self) -> str:
        with self._lock:
            return self._locale

    def available_locales(self) -> list[str]:
        try:
            return [p.stem for p in _I18N_DIR.glob("*.json")]
        except OSError:
            logger.warning("Could not list locale files")
            return ["en"]


# Singleton
_engine: Optional[I18nEngine] = None
_engine_lock = threading.Lock()

def get_engine() -> I18nEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    raise RuntimeError("I18nEngine requires QApplication — create QApplication first")
                # Assign to _engine BEFORE the constructor emits
                # language_changed, because a connected slot might call t()
                # which calls get_engine() — that would deadlock on
                # _engine_lock if _engine is still None.
                engine = I18nEngine()
                _engine = engine
    return _engine


def t(key: str, **kwargs) -> str:
    return get_engine().t(key, **kwargs)


def get_current_language() -> str:
    """Return the currently active locale code (e.g. 'it', 'en')."""
    return get_engine().locale


def get_locale() -> str:
    """Return the currently active locale code (e.g. 'it', 'en')."""
    return get_engine().locale


_MONTH_KEYS = ("jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")


def month_abbr(month: int) -> str:
    """Localized month abbreviation (dates.jan … dates.dec)."""
    if 1 <= month <= 12:
        return t(f"dates.{_MONTH_KEYS[month - 1]}")
    return str(month)


def format_dt(dt, fmt: str) -> str:
    """strftime with a locale-aware %b: the month abbreviation comes from the
    active dictionary instead of the C locale (always English otherwise)."""
    # '%%b' survives strftime as a literal '%b', replaced afterwards.
    return dt.strftime(fmt.replace("%b", "%%b")).replace("%b", month_abbr(dt.month))
