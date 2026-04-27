"""Subject canonicalization registry + ingest-time resolver (Phase A).

The :class:`SubjectRegistry` maps surface forms (e.g.
``"the auth-service"``, ``"auth service"``, ``"auth-api"``) onto
canonical subject ids (e.g. ``"service:auth-api"``) so the rest
of the system can index, dedupe, and reason over subjects without
worrying about how the caller spelled them.

The module-level :func:`resolve_subjects` glues the registry to
the ingest pipeline: it implements the A.3 precedence chain
(caller subjects → caller subject string → SLM auto-suggest) so
``store_memory`` can compute the final ``list[Subject]`` to bake
into ``memory.structured["subjects"]``.

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
# Ingest-time resolver (claim A.3 precedence + A.17 SLM auto-suggest)
# ---------------------------------------------------------------------------


def _validate_primary_count(subjects: list[Subject]) -> None:
    """Raise ``ValueError`` when more than one subject has ``primary=True``.

    A.3: "Passing both with conflicting primary values raises
    ``ValueError``."  Exactly one Subject in a list should be the
    primary timeline anchor.
    """
    primaries = [s for s in subjects if s.primary]
    if len(primaries) > 1:
        raise ValueError(
            "Multiple subjects with primary=True: "
            + ", ".join(p.id for p in primaries),
        )


async def _persist_caller_subjects(
    registry: SubjectRegistry,
    subjects: list[Subject],
) -> None:
    """Idempotently persist caller-provided subjects + their aliases.

    Caller-provided ``Subject`` instances may carry already-canonical
    ids (the MSEB-backend pattern: canonicalize via the registry,
    then pass the result through ``subjects=``).  We register them
    so future surface-driven lookups via :meth:`canonicalize` find
    these aliases.  Both inserts are ``INSERT OR IGNORE`` so re-runs
    are safe.
    """
    for s in subjects:
        await registry._persist_subject(s.id, s.type)  # noqa: SLF001
        for alias in s.aliases:
            await registry._persist_alias(  # noqa: SLF001
                s.id,
                s.type,
                alias,
                normalize_surface(alias),
            )


async def _derive_subjects_from_slm(
    registry: SubjectRegistry,
    *,
    intent_slot_label: Any,
    config: Any,
    domains: list[str] | None,
) -> list[Subject]:
    """Derive subjects from the SLM ``role_head`` ``primary`` spans.

    Implements claim A.17 — the GLiNER-retirement path.  When the
    v9 SLM chain produces ``role_spans`` with role="primary" above
    the configured confidence threshold, each becomes a candidate
    subject (the first is marked primary, the rest co-subjects).

    Skipped silently when:

    * The SLM chain is not active (``config.default_adapter_domain``
      is unset → ``intent_slot_label`` is the heuristic null
      passthrough; ``role_spans`` is empty).
    * The label's overall confidence is below
      ``config.slm_confidence_threshold``.
    * No span has role="primary".
    """
    if intent_slot_label is None:
        return []
    threshold = float(getattr(config, "slm_confidence_threshold", 0.3) or 0.3)
    is_confident_fn = getattr(intent_slot_label, "is_confident", None)
    if callable(is_confident_fn) and not is_confident_fn(threshold):
        return []

    role_spans = list(getattr(intent_slot_label, "role_spans", ()) or ())
    primary_spans = [
        rs for rs in role_spans
        if (rs.get("role") if isinstance(rs, dict) else getattr(rs, "role", "")) == "primary"
    ]
    if not primary_spans:
        return []

    domain_hint = domains[0] if domains else None
    slot_confidences = dict(getattr(intent_slot_label, "slot_confidences", {}) or {})
    overall_confidence = float(
        getattr(intent_slot_label, "intent_confidence", 1.0) or 1.0,
    )

    resolved: list[Subject] = []
    for i, rs in enumerate(primary_spans):
        # rs may be dict (from structured["intent_slot"]) or RoleSpan dataclass.
        if isinstance(rs, dict):
            surface = rs.get("surface") or rs.get("canonical") or ""
            slot = rs.get("slot") or "subject"
        else:
            surface = getattr(rs, "surface", "") or getattr(rs, "canonical", "")
            slot = getattr(rs, "slot", "subject")
        if not surface:
            continue
        s = await registry.canonicalize(
            surface,
            type_hint=slot,
            domain=domain_hint,
            source="slm_role",
        )
        # SLM's confidence in this span: prefer per-slot, else overall.
        span_conf = slot_confidences.get(slot, overall_confidence)
        # A primary subject is the first; co-subjects follow.
        # Cap inherited confidence at the SLM's signal so a
        # tier-1 (1.0) registry hit doesn't overstate confidence.
        resolved.append(
            s.model_copy(
                update={
                    "primary": (i == 0),
                    "confidence": min(s.confidence, span_conf),
                },
            ),
        )
    return resolved


async def resolve_subjects(
    *,
    registry: SubjectRegistry,
    config: Any,
    domains: list[str] | None,
    subject_legacy: str | None,
    subjects_explicit: list[Subject] | None,
    intent_slot_label: Any | None,
) -> list[Subject]:
    """Compute the final subject list per claim A.3 precedence.

    Precedence (highest wins):

    1. ``subjects_explicit`` provided → use as-is (after persisting
       to the registry so aliases are usable for future lookups).
    2. ``subject_legacy`` provided → canonicalize the string,
       promote to a one-element list with ``primary=True``,
       ``source="caller"``.
    3. SLM ``role_head`` ``primary`` spans → derive per A.17.
    4. Otherwise → empty list (Memory persists with no subjects).

    Args:
        registry: The :class:`SubjectRegistry`.
        config: ``NCMSConfig`` (or any object exposing
            ``slm_confidence_threshold``).
        domains: Memory domains; first element used as the
            type-set scope hint for canonicalization.
        subject_legacy: The legacy ``subject=str`` kwarg from
            ``store_memory``.
        subjects_explicit: The new ``subjects=list[Subject]`` kwarg.
        intent_slot_label: SLM extraction output.

    Returns:
        The resolved ``list[Subject]``.  May be empty.

    Raises:
        ValueError: When the resolved list contains more than one
            ``Subject`` with ``primary=True``.
    """
    # Precedence 1: caller-provided list wins.
    if subjects_explicit is not None:
        _validate_primary_count(subjects_explicit)
        out = list(subjects_explicit)
        # When no Subject is marked primary, promote the first.
        if out and not any(s.primary for s in out):
            out[0] = out[0].model_copy(update={"primary": True})
        await _persist_caller_subjects(registry, out)
        return out

    # Precedence 2: legacy single-subject string.
    if subject_legacy:
        domain_hint = domains[0] if domains else None
        s = await registry.canonicalize(
            subject_legacy,
            type_hint=None,
            domain=domain_hint,
            source="caller",
        )
        return [s.model_copy(update={"primary": True})]

    # Precedence 3: SLM auto-suggest (A.17 — GLiNER-retirement path).
    return await _derive_subjects_from_slm(
        registry,
        intent_slot_label=intent_slot_label,
        config=config,
        domains=domains,
    )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return UTC now in ISO-8601 format (used for created_at)."""
    return datetime.now(UTC).isoformat()
