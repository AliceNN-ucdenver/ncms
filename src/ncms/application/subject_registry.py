"""Subject canonicalization registry (Phase A foundation).

The :class:`SubjectRegistry` maps surface forms (e.g.
``"the auth-service"``, ``"auth service"``, ``"auth-api"``) onto
canonical subject ids (e.g. ``"service:auth-api"``) so the rest
of the system can index, dedupe, and reason over subjects without
worrying about how the caller spelled them.

This module ships in **two stages**:

1. **Sub-PR 1 (this commit) — skeleton.**  The class exists, holds
   a reference to the SQLite store, and exposes the public method
   surface that downstream code will call:
   :meth:`canonicalize`, :meth:`get`, :meth:`list_aliases`.  All
   three return passthrough / empty results — no real
   canonicalization happens yet.  This lets sub-PRs 3+ start
   wiring the API surface (``store_memory(subjects=…)``) without
   blocking on the canonicalization implementation.

2. **Sub-PR 2 — real logic.**  Three-tier alias lookup (exact →
   normalized → fuzzy/minted), confidence assignment, and the
   ``subject.alias_collision`` audit event for fuzzy matches.

The registry is intentionally NOT a domain-layer object: it
depends on aiosqlite (the SQLite store) and the dashboard event
log.  It lives under ``application/`` for that reason.

See ``docs/research/phases/phase-a-claims.md`` claims A.4, A.5,
A.16, and the design doc ``subject-centered-graph-design.md`` §4.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ncms.domain.models import Subject, SubjectSource

if TYPE_CHECKING:
    import aiosqlite


# ---------------------------------------------------------------------------
# Surface normalization
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_surface(surface: str) -> str:
    """Collapse whitespace and lowercase a surface form.

    Used as the lookup key in ``subject_aliases.alias_normalized``.
    Two surfaces that normalize to the same string are treated as
    equivalent for canonicalization purposes.

    Examples:
        ``"  Auth-Service  "`` → ``"auth-service"``
        ``"the   AUTH service"`` → ``"the auth service"``
        ``"adr-004"`` → ``"adr-004"``

    Stripping articles ("the ") and stop-words is *not* done here
    — that's a fuzzy-match concern handled by Sub-PR 2.  This
    function only does the deterministic part.
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
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s or "unknown"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SubjectRegistry:
    """Canonicalize subject surfaces against persisted aliases.

    Sub-PR 1 ships the skeleton: methods exist with the documented
    signatures, return shapes match what callers expect, but the
    actual lookup / minting logic is a stub that always mints a
    new canonical id from the surface (no alias reuse).  Sub-PR 2
    fills in the three-tier lookup.

    Args:
        db: An open ``aiosqlite.Connection``.  The registry does
            NOT manage the connection lifecycle; the caller does.
        event_emit: Optional callable invoked with a single dict
            argument when an alias-collision audit event should
            be emitted.  When ``None``, events are dropped.
            Sub-PR 2 wires this to the dashboard event log.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        event_emit: object | None = None,
    ) -> None:
        self._db = db
        self._event_emit = event_emit

    async def canonicalize(
        self,
        surface: str,
        *,
        type_hint: str | None = None,
        domain: str | None = None,
        source: SubjectSource = "caller",
    ) -> Subject:
        """Resolve a surface form to a canonical :class:`Subject`.

        Sub-PR 1 stub: always mints a new canonical id from the
        surface using ``slugify()`` and the type hint.  No alias
        lookup yet.  Confidence is fixed at 0.6 (the "minted" tier
        in the spec).

        Sub-PR 2 will replace this body with:

        1. Exact-match lookup against ``subject_aliases.alias`` →
           confidence 1.0.
        2. Normalized lookup against
           ``subject_aliases.alias_normalized`` → confidence 0.85,
           emit alias_collision event.
        3. Mint new canonical id, persist alias and subject row →
           confidence 0.6.

        The ``domain`` argument is reserved for sub-PR 2: the
        canonicalizer will scope alias lookup by the loaded
        DomainSpec.subject_types so that "auth-service" doesn't
        collide across software_dev / clinical when they happen
        to share a surface.  Acknowledged here with a no-op
        binding to keep the parameter on the public surface.
        """
        _ = domain  # sub-PR 2 will use this for type-set scoping
        canonical_type = type_hint or "subject"
        canonical_id = f"{canonical_type}:{slugify(surface)}"
        return Subject(
            id=canonical_id,
            type=canonical_type,
            primary=True,
            aliases=(surface,),
            source=source,
            confidence=0.6,
        )

    async def get(self, canonical_id: str) -> Subject | None:
        """Look up a canonical subject by id.

        Returns ``None`` if no subject with this id has been
        canonicalized yet.  Sub-PR 1 stub always returns ``None``.
        """
        return None

    async def list_aliases(self, canonical_id: str) -> tuple[str, ...]:
        """Return every persisted alias for a canonical id.

        Sub-PR 1 stub always returns ``()``.
        """
        return ()


# ---------------------------------------------------------------------------
# Helpers reserved for Sub-PR 2 — kept here so the import surface is
# stable from sub-PR 1.
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return UTC now in ISO-8601 format (used for created_at)."""
    return datetime.now(UTC).isoformat()
