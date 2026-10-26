"""Překlady: jazyk podle Windows (nebo z nastavení), texty v locales/<jazyk>.json.

Klíče „ui.*“ používá okno (JavaScript), ostatní Python. Chybějící text se vezme
z angličtiny, chybějící klíč se zobrazí, jak je (ať je chyba vidět).
"""

import ctypes
import json
import locale
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LOCALES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "tvcast" / "locales"
LANGUAGES = {"cs": "Čeština", "en": "English", "sk": "Slovenčina", "de": "Deutsch", "pl": "Polski",
             "fr": "Français", "es": "Español", "it": "Italiano"}
FALLBACK = "en"

_lang = FALLBACK
_strings: dict[str, str] = {}
_fallback: dict[str, str] = {}


def system_language() -> str:
    """Jazyk rozhraní Windows (např. cs), pokud ho aplikace umí, jinak angličtina."""
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        code = locale.windows_locale.get(lcid, "")[:2]
    except (AttributeError, OSError):
        code = (locale.getlocale()[0] or "")[:2]
    return code if code in LANGUAGES else FALLBACK


def resolve(setting: str | None) -> str:
    """Jazyk z nastavení („auto“ nebo kód) → kód jazyka, který se použije."""
    return setting if setting in LANGUAGES else system_language()


def _load(code: str) -> dict[str, str]:
    try:
        return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Překlad %s nejde načíst: %s", code, e)
        return {}


def set_language(code: str) -> None:
    global _lang, _strings, _fallback
    _lang = code if code in LANGUAGES else FALLBACK
    _fallback = _load(FALLBACK)
    _strings = _fallback if _lang == FALLBACK else _load(_lang)


def language() -> str:
    return _lang


def t(key: str, **params) -> str:
    text = _strings.get(key) or _fallback.get(key) or key
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError, ValueError):
        return text


def ui_strings() -> dict[str, str]:
    """Texty pro okno: „ui.*“ v aktuálním jazyce, doplněné angličtinou."""
    return {k[3:]: v for k, v in (_fallback | _strings).items() if k.startswith("ui.")}


# Preferované jazyky zvuku a titulků podle jazyka aplikace (Češi a Slováci si rozumí).
DEFAULT_LANGS = {"cs": ["cs", "sk", "en"], "sk": ["sk", "cs", "en"], "en": ["en"]}


def default_langs(code: str | None = None) -> list[str]:
    """Výchozí preferované jazyky zvuku a titulků pro jazyk aplikace (nebo daný)."""
    code = code or _lang
    return DEFAULT_LANGS.get(code, [code, "en"])


def preferred_langs(cfg: dict) -> list[str]:
    """Jazyky zvuku a titulků: vlastní seznam z nastavení, jinak podle jazyka aplikace."""
    return cfg["langs"] if cfg.get("langs_custom") and cfg.get("langs") else default_langs()


set_language(system_language())
