"""Deterministic surface-form normalization (Phase A subject layer).

Pure functions, no I/O.  Used by :mod:`registry` for alias
lookup keys and by :mod:`resolver` for the A.3 cross-kwarg
conflict check (which compares formula-built canonical ids
without touching the registry).

Article-stripping, stemming, and other fuzzier matching are out
of scope here — those live in higher-level resolvers (Phase C).
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def normalize_surface(surface: str) -> str:
    """Collapse whitespace and lowercase a surface form.

    Used as the lookup key in
    ``subject_aliases.alias_normalized``.  Two surfaces that
    normalize to the same string are treated as equivalent for
    canonicalization purposes.

    Examples:
        ``"  Auth-Service  "`` → ``"auth-service"``
        ``"the   AUTH service"`` → ``"the auth service"``
        ``"adr-004"`` → ``"adr-004"``
    """
    return _WHITESPACE_RE.sub(" ", surface.strip()).lower()


def slugify(text: str) -> str:
    """Slugify a surface into a canonical-id-safe slug.

    Used when minting a new canonical id.  Aggressive: keeps
    alphanumerics + ``-``, replaces other runs with ``-``, and
    collapses leading/trailing dashes.

    Examples:
        ``"Auth Service"`` → ``"auth-service"``
        ``"ADR-004: Authentication"`` → ``"adr-004-authentication"``
        ``"my/weird name!"`` → ``"my-weird-name"``
    """
    s = _SLUG_RE.sub("-", text).strip("-").lower()
    return s or "unknown"
