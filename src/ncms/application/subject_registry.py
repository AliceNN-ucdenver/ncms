"""Subject canonicalization registry (Phase A).

The :class:`SubjectRegistry` maps surface forms (e.g.
``"the auth-service"``, ``"auth service"``, ``"auth-api"``) onto
canonical subject ids (e.g. ``"service:auth-api"``) so the rest
of the system can index, dedupe, and reason over subjects without
worrying about how the caller spelled them.

**Sub-PR 2 (this commit):** real three-tier alias lookup:

1. **Exact match.**  ``subject_aliases.alias`` equals the input
   surface verbatim.  Confidence ``1.0``; no event.
2. **Normalized match.**  ``subject_aliases.alias_normalized``
   equals ``normalize_surface(surface)``.  Confidence ``0.85``;
   the new surface is persisted as an additional alias of the
   matched canonical, and a ``subject.alias_collision`` dashboard
   event is emitted so a reviewer can audit the canonicalization.
3. **Mint.**  No alias matches.  Mint ``"<type>:<slug>"`` (slug
   from :func:`slugify`), persist the new subject row + alias,
   confidence ``0.6``.

Type scoping: when ``type_hint`` is provided, lookup is restricted
to aliases whose ``subject_aliases.type`` equals the hint.  This
keeps ``"auth-api"`` from cross-pollinating between
``service:auth-api`` and ``decision:auth-api`` when both exist.
When the hint is omitted, lookup is unscoped — the first match
across all types wins (deterministic by ``created_at``).

The registry is intentionally NOT a domain-layer object: it
depends on aiosqlite (the SQLite store) and the dashboard event
log.  It lives under ``application/`` for that reason.

See ``docs/research/phases/phase-a-claims.md`` claims A.4, A.5,
A.16, and the design doc ``subject-centered-graph-design.md`` §4.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

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

    Stripping articles ("the ") and stop-words is *not* done here.
    This function only does the deterministic part — fuzzier
    canonicalization (article-stripping, stemming) lives in
    higher-level resolvers (Phase C scope).
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
# Event emission protocol
# ---------------------------------------------------------------------------


class _SupportsEmit(Protocol):
    """Structural type for the event log surface we depend on.

    The infrastructure ``EventLog`` and ``NullEventLog`` both
    expose ``emit(DashboardEvent)``.  We avoid importing the
    concrete classes here to keep the application layer free of
    infrastructure imports.
    """

    def emit(self, event: Any) -> None: ...  # pragma: no cover (protocol)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SubjectRegistry:
    """Canonicalize subject surfaces against persisted aliases.

    Args:
        db: An open ``aiosqlite.Connection``.  The registry does
            NOT manage the connection lifecycle; the caller does.
        event_log: Optional event sink with an ``emit(event)``
            method (``EventLog`` or ``NullEventLog`` from
            ``infrastructure.observability``).  When ``None`` the
            registry silently skips the alias-collision event.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        event_log: _SupportsEmit | None = None,
    ) -> None:
        self._db = db
        self._event_log = event_log

    # ── public API ───────────────────────────────────────────────────

    async def canonicalize(
        self,
        surface: str,
        *,
        type_hint: str | None = None,
        domain: str | None = None,
        source: SubjectSource = "caller",
    ) -> Subject:
        """Resolve a surface form to a canonical :class:`Subject`.

        Three-tier lookup; see module docstring.

        Args:
            surface: The raw text form to canonicalize.  Empty /
                whitespace-only surfaces mint to ``"<type>:unknown"``.
            type_hint: Restricts alias lookup to this type.  When
                provided, only aliases whose ``type`` column matches
                are considered; when omitted, lookup spans all types.
            domain: Reserved.  Phase B will use this to scope lookup
                by the loaded ``DomainSpec.subject_types``.  Today the
                argument is accepted but not consulted.
            source: Provenance tag carried on the returned
                :class:`Subject`.

        Returns:
            A :class:`Subject` with confidence reflecting the lookup
            tier (1.0 / 0.85 / 0.6).  ``aliases`` always contains the
            input surface as its first (and only-so-far in the
            returned object) element.
        """
        _ = domain  # reserved for Phase B type-set scoping
        # Tier 1: exact alias match.
        exact = await self._lookup_exact(surface, type_hint)
        if exact is not None:
            canonical_id, ctype = exact
            return Subject(
                id=canonical_id,
                type=ctype,
                primary=True,
                aliases=(surface,),
                source=source,
                confidence=1.0,
            )

        # Tier 2: normalized alias match (fuzzy).
        normalized = normalize_surface(surface)
        fuzzy = await self._lookup_normalized(
            normalized,
            type_hint,
            exclude_surface=surface,
        )
        if fuzzy is not None:
            canonical_id, ctype, alternatives = fuzzy
            await self._persist_alias(canonical_id, ctype, surface, normalized)
            self._emit_alias_collision(
                surface=surface,
                picked_canonical=canonical_id,
                confidence=0.85,
                alternatives=alternatives,
            )
            return Subject(
                id=canonical_id,
                type=ctype,
                primary=True,
                aliases=(surface,),
                source=source,
                confidence=0.85,
            )

        # Tier 3: mint a new canonical id.
        ctype = type_hint or "subject"
        canonical_id = f"{ctype}:{slugify(surface)}"
        await self._persist_subject(canonical_id, ctype)
        await self._persist_alias(canonical_id, ctype, surface, normalized)
        return Subject(
            id=canonical_id,
            type=ctype,
            primary=True,
            aliases=(surface,),
            source=source,
            confidence=0.6,
        )

    async def get(self, canonical_id: str) -> Subject | None:
        """Return the persisted :class:`Subject` for an id, or None.

        The returned ``aliases`` tuple includes every persisted
        alias for this canonical id (deterministic by ``created_at``).
        """
        cur = await self._db.execute(
            "SELECT canonical_id, type FROM subjects WHERE canonical_id = ?",
            (canonical_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        aliases = await self.list_aliases(canonical_id)
        return Subject(
            id=row[0],
            type=row[1],
            primary=True,
            aliases=aliases,
            source="caller",
            confidence=1.0,
        )

    async def list_aliases(self, canonical_id: str) -> tuple[str, ...]:
        """Return every persisted alias for a canonical id."""
        cur = await self._db.execute(
            "SELECT alias FROM subject_aliases WHERE canonical_id = ? "
            "ORDER BY created_at, alias",
            (canonical_id,),
        )
        rows = await cur.fetchall()
        return tuple(r[0] for r in rows)

    # ── internal helpers ─────────────────────────────────────────────

    async def _lookup_exact(
        self,
        surface: str,
        type_hint: str | None,
    ) -> tuple[str, str] | None:
        """Find a canonical id whose alias exactly equals ``surface``.

        Returns ``(canonical_id, type)`` or ``None``.  When multiple
        canonicals share the same exact alias (different types and
        no type_hint), returns the earliest by ``created_at``.
        """
        if type_hint is not None:
            cur = await self._db.execute(
                "SELECT canonical_id, type FROM subject_aliases "
                "WHERE alias = ? AND type = ? "
                "ORDER BY created_at LIMIT 1",
                (surface, type_hint),
            )
        else:
            cur = await self._db.execute(
                "SELECT canonical_id, type FROM subject_aliases "
                "WHERE alias = ? ORDER BY created_at LIMIT 1",
                (surface,),
            )
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def _lookup_normalized(
        self,
        normalized: str,
        type_hint: str | None,
        *,
        exclude_surface: str,
    ) -> tuple[str, str, tuple[str, ...]] | None:
        """Find a canonical id whose normalized alias matches.

        Returns ``(canonical_id, type, alternatives)`` where
        ``alternatives`` enumerates every distinct canonical id with
        the same normalized alias (used as the
        ``subject.alias_collision`` event payload so a reviewer can
        see what was passed over).  ``exclude_surface`` skips rows
        whose surface verbatim equals the input — those would have
        matched in tier 1.

        Returns ``None`` when no alias normalizes the same way.
        """
        if type_hint is not None:
            cur = await self._db.execute(
                "SELECT canonical_id, type FROM subject_aliases "
                "WHERE alias_normalized = ? AND type = ? "
                "AND alias <> ? "
                "ORDER BY created_at",
                (normalized, type_hint, exclude_surface),
            )
        else:
            cur = await self._db.execute(
                "SELECT canonical_id, type FROM subject_aliases "
                "WHERE alias_normalized = ? AND alias <> ? "
                "ORDER BY created_at",
                (normalized, exclude_surface),
            )
        rows = list(await cur.fetchall())
        if not rows:
            return None
        primary_canonical = rows[0][0]
        primary_type = rows[0][1]
        # Alternatives: every distinct canonical id (including the picked
        # one), preserved in order so reviewers see what was considered.
        seen: list[str] = []
        for r in rows:
            if r[0] not in seen:
                seen.append(r[0])
        return primary_canonical, primary_type, tuple(seen)

    async def _persist_subject(
        self,
        canonical_id: str,
        ctype: str,
    ) -> None:
        """Insert a subject row, idempotent on (canonical_id)."""
        await self._db.execute(
            "INSERT OR IGNORE INTO subjects "
            "(canonical_id, type, created_at) VALUES (?, ?, ?)",
            (canonical_id, ctype, _utcnow_iso()),
        )
        await self._db.commit()

    async def _persist_alias(
        self,
        canonical_id: str,
        ctype: str,
        surface: str,
        normalized: str,
    ) -> None:
        """Insert an alias row, idempotent on (canonical_id, alias)."""
        await self._db.execute(
            "INSERT OR IGNORE INTO subject_aliases "
            "(canonical_id, type, alias, alias_normalized, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (canonical_id, ctype, surface, normalized, _utcnow_iso()),
        )
        await self._db.commit()

    def _emit_alias_collision(
        self,
        *,
        surface: str,
        picked_canonical: str,
        confidence: float,
        alternatives: tuple[str, ...],
    ) -> None:
        """Emit a ``subject.alias_collision`` dashboard event.

        Fires only on tier-2 (fuzzy) matches.  The event payload
        carries everything a reviewer needs to assess whether the
        canonicalization was correct.  No-op when no event log is
        wired.

        See claim A.16: events are queryable via
        ``SELECT * FROM dashboard_events WHERE type='subject.alias_collision'``.
        """
        if self._event_log is None:
            return
        # Local import to avoid pulling infrastructure into application's
        # import graph at module load.
        from ncms.infrastructure.observability.event_log import DashboardEvent

        event = DashboardEvent(
            type="subject.alias_collision",
            data={
                "surface": surface,
                "picked_canonical": picked_canonical,
                "confidence": confidence,
                "alternatives": list(alternatives),
            },
        )
        self._event_log.emit(event)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return UTC now in ISO-8601 format (used for created_at)."""
    return datetime.now(UTC).isoformat()
