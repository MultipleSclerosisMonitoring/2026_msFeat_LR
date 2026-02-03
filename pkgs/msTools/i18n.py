# msTools/i18n.py
# -*- coding: utf-8 -*-
"""Lightweight i18n utilities using ``gettext`` and OS locales.

This module centralizes internationalization (i18n) setup for command-line
scripts and libraries in the project. It provides:

- Language detection based on explicit preference, environment variables,
  and system locale.
- Loading of compiled gettext catalogs (``.mo``) from a standard path
  (``locales/<lang>/LC_MESSAGES/<domain>.mo``).
- Global aliases ``_`` and ``ngettext`` after initialization so you can
  mark translatable strings throughout your code.
- Optional process locale configuration to influence date/number formatting
  (independent from gettext message language).

The code follows **Google-style docstrings**, making it easy to generate
API documentation with **Sphinx** + **napoleon**.

Example:
    >>> from msTools import i18n
    >>> i18n.init_translation("es", domain="msgait", localedir="locales")
    <gettext.GNUTranslations ...>
    >>> _ = i18n._
    >>> print(_("HELLO"))
    Hola

Notes:
    - ``init_translation`` installs the alias ``_`` into Python builtins.
    - If no ``.mo`` is found, translations gracefully fallback to a
      ``NullTranslations`` (no exception, messages returned as-is).
"""
from __future__ import annotations

import gettext as _gettext
import locale
import os
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------- Configuration ---------------------------------
DOMAIN: str = "msGait"
BASE_DIR = Path(__file__).resolve().parent
LOCALES_DIR = (BASE_DIR / ".." / "locales").resolve()

# Store the active translation object. Exposed by init_translation().
_translation: Optional[_gettext.NullTranslations] = None

# Public aliases; rebound in init_translation().
_: callable = lambda s: s  # noqa: E731 - will be replaced by gettext after init
ngettext: callable = lambda s, p, n: s if n == 1 else p  # noqa: E731


# ------------------------------ Public API ----------------------------------

def detect_language(preferred: Optional[str] = None) -> str:
    """Detect a reasonable language code to use for message translation.

    The function tries, in order: an explicit ``preferred`` value, environment
    variables (``LC_ALL``, ``LC_MESSAGES``, ``LANG``), and the system locale.
    The return value is normalized to a short language code (e.g., ``"es"``)
    whenever possible.

    Args:
        preferred: Explicit language preference such as ``"es"``, ``"es_ES"``,
            or ``"en_US"``. If provided, it takes precedence over environment
            and system settings.

    Returns:
        A language code string (e.g., ``"es"`` or ``"en"``). If no suitable
        value can be determined, ``"en"`` is returned.

    Example:
        >>> detect_language("es_ES")
        'es'
        >>> os.environ["LANG"] = "en_US.UTF-8"
        >>> detect_language(None)
        'en'
    """
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    try:
        loc = locale.getlocale()
        if loc and loc[0]:
            candidates.append(loc[0])
    except Exception:
        # Ignore locale probing errors and fallback later.
        pass

    # Normalize candidates and pick the first usable short code.
    for cand in candidates:
        if not cand:
            continue
        lang = cand.split(".")[0]  # strip encoding
        if "_" in lang:
            # Prefer short form ("es_ES" -> "es").
            return lang.split("_")[0]
        return lang
    return "en"


def available_languages(localedir: str | os.PathLike = LOCALES_DIR, *, domain: str = DOMAIN) -> list[str]:
    """List all languages for which a compiled catalog exists.

    It scans ``<localedir>/<lang>/LC_MESSAGES/<domain>.mo`` and returns the
    ``<lang>`` parts found.

    Args:
        localedir: Base directory where locale folders live.
        domain: Gettext domain (catalog name without extension).

    Returns:
        Sorted list of language directory names (e.g., ``["en", "es"]``).

    Example:
        >>> available_languages("locales", domain="msgait")
        ['en', 'es']
    """
    base = Path(localedir)
    langs: list[str] = []
    for lang_dir in base.iterdir() if base.exists() else []:
        mo = lang_dir / "LC_MESSAGES" / f"{domain}.mo"
        if mo.exists():
            langs.append(lang_dir.name)
    return sorted(langs)


def init_translation(
    lang: Optional[str] = None,
    *,
    domain: str = DOMAIN,
    localedir: str | os.PathLike = LOCALES_DIR,
) -> _gettext.NullTranslations:
    """Initialize gettext and expose ``_``/``ngettext``.

    This function configures gettext for the given language and domain. It also
    installs the global alias ``_`` into builtins (via ``trans.install()``),
    and updates the module-level ``_`` and ``ngettext`` references so that
    other modules importing ``msTools.i18n`` can immediately use them.

    Args:
        lang: Explicit language preference (``"es"``, ``"en"``, ``"es_ES"``, ...).
            If ``None``, a language will be detected via :func:`detect_language`.
        domain: Gettext domain (catalog base name, without extension).
        localedir: Base directory where locale folders live.

    Returns:
        The loaded gettext translations object. If no ``.mo`` is found, a
        ``NullTranslations`` object is returned (strings remain unchanged).

    Example:
        >>> trans = init_translation("es", domain="msgait", localedir="locales")
        >>> _ = _  # alias provided by this module
        >>> print(_("HELLO"))
        Hola
    """
    global _translation, _, ngettext

    selected = detect_language(lang)
    # Try both the full form and the short form (e.g., es_ES then es)
    langs_to_try: list[str] = [selected]
    short = selected.split("_")[0]
    if short != selected:
        langs_to_try.append(short)

    # fallback=True prevents exceptions if the catalog is missing
    trans = _gettext.translation(
        domain=domain, localedir=str(localedir), languages=langs_to_try, fallback=True
    )

    _translation = trans
    trans.install()         # installs _ into builtins
    _ = trans.gettext       # refresh local module alias
    ngettext = trans.ngettext

    return trans


def set_locale_for_formatting(lang: Optional[str]) -> None:
    """Attempt to set the process locale for regional formatting.

    This affects functions that rely on the C locale (e.g., number and date
    formatting via ``locale`` or ``datetime.strftime``). It **does not** affect
    gettext translations; use :func:`init_translation` for message language.

    Args:
        lang: Locale name such as ``"es_ES"`` or ``"en_US"``. If ``None`` or
            the desired locale is not available on the system, this function
            does nothing.

    Example:
        >>> set_locale_for_formatting("es_ES")
        >>> import datetime
        >>> datetime.datetime(2025, 1, 2).strftime("%x")  # may print in Spanish format
        '02/01/25'
    """
    if not lang:
        return
    for cand in (f"{lang}.UTF-8", f"{lang}.utf8", lang):
        try:
            locale.setlocale(locale.LC_ALL, cand)
            return
        except Exception:
            continue


def gettext(message: str) -> str:
    """Translate a single message using the active catalog.

    This is a small helper that proxies to the active translations object if
    available; otherwise it returns ``message`` unchanged. Most code should use
    the global alias ``_`` installed by :func:`init_translation`.

    Args:
        message: The source message string to translate.

    Returns:
        The translated string if a catalog is active; otherwise, ``message``.

    Example:
        >>> init_translation("en")
        <gettext.GNUTranslations ...>
        >>> gettext("HELLO")
        'HELLO'
    """
    if _translation:
        return _translation.gettext(message)
    return message


# Keep module-level alias for compatibility with code that imported `_` early
_ = gettext

