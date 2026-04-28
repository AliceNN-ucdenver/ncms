"""Subject canonicalization registry — three-tier alias lookup.

Phase A claims A.4 + A.5 + A.16.

The :class:`SubjectRegistry` maps surface forms (e.g.
``"the auth-service"``, ``"auth service"``) onto canonical
subject ids (e.g. ``"service:auth-api"``) backed by the
``subjects`` and ``subject_aliases`` SQLite tables.

Three-tier lookup, deterministic and reproducible:

1. **Exact** — ``subject_aliases.alias`` equals the raw surface.
   Confidence ``1.0``; no event.
2. **Normalized** — ``subject_aliases.alias_normalized`` equals
   :func:`normalize_surface(surface)`.  Confidence ``0.85``; the
   new surface is persisted as an additional alias of the matched
   canonical, and a ``subject.alias_collision`` dashboard event is
   emitted so reviewers can audit the canonicalization.
3. **Mint** — no alias matches.  Mint ``"<type>:<slug>"``,
   persist subject + alias rows, confidence ``0.6``.

Type scoping: when ``type_hint`` is provided, lookup is
restricted to aliases whose ``subject_aliases.type`` equals the
hint.  This keeps ``"auth-api"`` from cross-pollinating between
``service:auth-api`` and ``decision:auth-api`` when both exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ncms.application.subject.surface import normalize_surface, slugify
from ncms.domain.models import Subject, SubjectSource

if TYPE_CHECKING:
    import aiosqlite


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp used for ``created_at`` columns."""
    return datetime.now(UTC).isoformat()


class _SupportsEmit(Protocol):
    """Structural type for the dashboard event sink.

    The infrastructure ``EventLog`` and ``NullEventLog`` both
    expose ``emit(DashboardEvent)``.  We avoid importing the
    concrete classes here to keep the application layer free of
    infrastructure imports at module load.
    """

    def emit(self, event: Any) -> None: ...  # pragma: no cover (protocol)


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

    # ── public API ────────────────────────────────────────────────

    async def canonicalize(
        self,
        surface: str,
        *,
        type_hint: str | None = None,
        domain: str | None = None,
        source: SubjectSource = "caller",
    ) -> Subject:
        """Resolve a surface form to a canonical :class:`Subject`."""
        _ = domain  # reserved for Phase B type-set scoping

        exact = await self._lookup_exact(surface, type_hint)
        if exact is not None:
            return self._build_subject(exact, surface, source, 1.0)

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
            return self._build_subject(
                (canonical_id, ctype),
                surface,
                source,
                0.85,
            )

        ctype = type_hint or "subject"
        canonical_id = f"{ctype}:{slugify(surface)}"
        await self._persist_subject(canonical_id, ctype)
        await self._persist_alias(canonical_id, ctype, surface, normalized)
        return self._build_subject(
            (canonical_id, ctype),
            surface,
            source,
            0.6,
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

    # ── internal helpers ──────────────────────────────────────────

    @staticmethod
    def _build_subject(
        ids: tuple[str, str],
        surface: str,
        source: SubjectSource,
        confidence: float,
    ) -> Subject:
        canonical_id, ctype = ids
        return Subject(
            id=canonical_id,
            type=ctype,
            primary=True,
            aliases=(surface,),
            source=source,
            confidence=confidence,
        )

    async def _lookup_exact(
        self,
        surface: str,
        type_hint: str | None,
    ) -> tuple[str, str] | None:
        """Find a canonical id whose alias exactly equals ``surface``."""
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

        Returns ``(canonical_id, type, alternatives)``;
        ``alternatives`` is every distinct canonical id sharing the
        same normalized form, preserved in ``created_at`` order so
        the audit event payload shows what was passed over.
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
        seen: list[str] = []
        for r in rows:
            if r[0] not in seen:
                seen.append(r[0])
        return primary_canonical, primary_type, tuple(seen)

    async def _persist_subject(self, canonical_id: str, ctype: str) -> None:
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
        """Emit ``subject.alias_collision`` (claim A.16).

        Fires only on tier-2 (fuzzy) matches.  No-op when no event
        log is wired.  The event lands in ``dashboard_events`` and
        is queryable via SQL — that's the validation surface for
        Phase A (no UI required).
        """
        if self._event_log is None:
            return
        # Local import — keeps application's module-load graph free
        # of infrastructure imports.
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

    # ── package-internal hooks (used by resolver) ─────────────────

    async def register_caller_subject(
        self,
        s: Subject,
    ) -> None:
        """Idempotently persist a caller-provided Subject + its aliases.

        Used by :func:`resolver.resolve_subjects` when the caller
        passes ``subjects=[...]`` so future surface-driven lookups
        find the registered aliases.
        """
        await self._persist_subject(s.id, s.type)
        for alias in s.aliases:
            await self._persist_alias(
                s.id,
                s.type,
                alias,
                normalize_surface(alias),
            )
