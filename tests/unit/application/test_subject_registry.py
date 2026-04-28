"""Phase A — SubjectRegistry canonicalization (sub-PR 2).

Covers claim A.5: three-tier alias lookup (exact / normalized /
mint) with deterministic confidence and alias persistence.

Alias-collision audit-event coverage lives in
``tests/integration/test_subject_alias_collision.py`` because the
event has to round-trip through the EventLog persistence task and
the ``dashboard_events`` table — that's an integration concern.
"""

from __future__ import annotations

import aiosqlite
import pytest

from ncms.application.subject import (
    SubjectRegistry,
    normalize_surface,
    slugify,
)
from ncms.domain.models import Subject
from ncms.infrastructure.storage.migrations import create_schema

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNormalizeSurface:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Auth-Service", "auth-service"),
            ("  Auth-Service  ", "auth-service"),
            ("the   AUTH service", "the auth service"),
            ("ADR-004", "adr-004"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize_surface(raw) == expected


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Auth Service", "auth-service"),
            ("ADR-004: Authentication", "adr-004-authentication"),
            ("my/weird name!", "my-weird-name"),
            ("", "unknown"),
            ("---", "unknown"),
        ],
    )
    def test_slugify(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        await create_schema(conn)
        yield conn


@pytest.fixture
def registry(db: aiosqlite.Connection) -> SubjectRegistry:
    return SubjectRegistry(db)


# ---------------------------------------------------------------------------
# Tier 3 — minting (the only path with no prior aliases)
# ---------------------------------------------------------------------------


class TestCanonicalizeTier3Mint:
    """No alias → mint, confidence 0.6, both rows persisted."""

    async def test_minted_subject_has_confidence_0_6(
        self, registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize("auth service", type_hint="service")
        assert s.confidence == 0.6
        assert s.id == "service:auth-service"
        assert s.type == "service"

    async def test_minted_id_format_is_type_colon_slug(
        self, registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize(
            "ADR-004: Authentication",
            type_hint="decision",
        )
        assert s.id == "decision:adr-004-authentication"

    async def test_default_type_when_hint_missing(
        self, registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize("foo bar")
        assert s.type == "subject"
        assert s.id == "subject:foo-bar"

    async def test_minted_subject_persisted_in_subjects_table(
        self,
        registry: SubjectRegistry,
        db: aiosqlite.Connection,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        cur = await db.execute(
            "SELECT canonical_id, type FROM subjects "
            "WHERE canonical_id = 'service:auth-service'",
        )
        row = await cur.fetchone()
        assert row == ("service:auth-service", "service")

    async def test_minted_alias_persisted(
        self,
        registry: SubjectRegistry,
        db: aiosqlite.Connection,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        cur = await db.execute(
            "SELECT alias, alias_normalized FROM subject_aliases "
            "WHERE canonical_id = 'service:auth-service'",
        )
        rows = await cur.fetchall()
        assert ("auth service", "auth service") in rows


# ---------------------------------------------------------------------------
# Tier 1 — exact alias match (existing canonical, identical surface)
# ---------------------------------------------------------------------------


class TestCanonicalizeTier1Exact:
    """Re-canonicalizing the same surface returns confidence 1.0."""

    async def test_exact_repeat_returns_confidence_1_0(
        self, registry: SubjectRegistry,
    ) -> None:
        first = await registry.canonicalize("auth service", type_hint="service")
        assert first.confidence == 0.6  # tier 3 first time

        second = await registry.canonicalize("auth service", type_hint="service")
        assert second.confidence == 1.0
        assert second.id == first.id
        assert second.type == "service"

    async def test_exact_match_idempotent_no_dup_subject_row(
        self,
        registry: SubjectRegistry,
        db: aiosqlite.Connection,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        await registry.canonicalize("auth service", type_hint="service")
        cur = await db.execute(
            "SELECT COUNT(*) FROM subjects "
            "WHERE canonical_id = 'service:auth-service'",
        )
        row = await cur.fetchone()
        assert row[0] == 1

    async def test_exact_match_idempotent_no_dup_alias_row(
        self,
        registry: SubjectRegistry,
        db: aiosqlite.Connection,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        await registry.canonicalize("auth service", type_hint="service")
        cur = await db.execute(
            "SELECT COUNT(*) FROM subject_aliases "
            "WHERE canonical_id = 'service:auth-service' "
            "AND alias = 'auth service'",
        )
        row = await cur.fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------------
# Tier 2 — normalized (fuzzy) match
# ---------------------------------------------------------------------------


class TestCanonicalizeTier2Fuzzy:
    """Different surface, same normalized form → existing canonical."""

    async def test_alias_variants_resolve_to_one_canonical(
        self,
        registry: SubjectRegistry,
    ) -> None:
        """A.5 headline test: surface variants converge."""
        first = await registry.canonicalize("auth service", type_hint="service")
        second = await registry.canonicalize("AUTH SERVICE", type_hint="service")
        third = await registry.canonicalize("  auth   service  ", type_hint="service")
        assert second.id == first.id
        assert third.id == first.id
        # Tier-2 confidences (the second and third surfaces).
        assert second.confidence == 0.85
        assert third.confidence == 0.85

    async def test_fuzzy_match_persists_new_alias(
        self,
        registry: SubjectRegistry,
        db: aiosqlite.Connection,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        await registry.canonicalize("AUTH SERVICE", type_hint="service")
        cur = await db.execute(
            "SELECT COUNT(*) FROM subject_aliases "
            "WHERE canonical_id = 'service:auth-service'",
        )
        row = await cur.fetchone()
        # Two aliases now: "auth service" + "AUTH SERVICE".
        assert row[0] == 2

    async def test_exact_higher_confidence_than_fuzzy(
        self,
        registry: SubjectRegistry,
    ) -> None:
        # Mint via "auth service".
        await registry.canonicalize("auth service", type_hint="service")
        # Fuzzy match via "AUTH SERVICE" — confidence 0.85, persists alias.
        await registry.canonicalize("AUTH SERVICE", type_hint="service")
        # Now "AUTH SERVICE" is itself a stored alias — should hit tier 1.
        third = await registry.canonicalize("AUTH SERVICE", type_hint="service")
        assert third.confidence == 1.0


# ---------------------------------------------------------------------------
# Type scoping (claim A.5: type_hint scopes lookup)
# ---------------------------------------------------------------------------


class TestTypeScoping:
    async def test_type_hint_isolates_lookup(
        self,
        registry: SubjectRegistry,
    ) -> None:
        """Same surface in two types → two distinct canonicals."""
        svc = await registry.canonicalize("auth-api", type_hint="service")
        adr = await registry.canonicalize("auth-api", type_hint="decision")
        assert svc.id == "service:auth-api"
        assert adr.id == "decision:auth-api"
        assert svc.id != adr.id

    async def test_no_type_hint_picks_first_by_created_at(
        self,
        registry: SubjectRegistry,
    ) -> None:
        # First mint: service:auth-api.
        first = await registry.canonicalize("auth-api", type_hint="service")
        # Then mint: decision:auth-api.
        await registry.canonicalize("auth-api", type_hint="decision")
        # Untyped lookup of "auth-api" — picks the first by created_at.
        ambiguous = await registry.canonicalize("auth-api")
        assert ambiguous.id == first.id


# ---------------------------------------------------------------------------
# Source / domain pass-through
# ---------------------------------------------------------------------------


class TestSourceAndDomain:
    async def test_source_propagates_to_returned_subject(
        self,
        registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize(
            "auth service",
            type_hint="service",
            source="slm_role",
        )
        assert s.source == "slm_role"

    async def test_domain_kwarg_accepted_and_ignored_in_phase_a(
        self,
        registry: SubjectRegistry,
    ) -> None:
        # Phase A: domain kwarg is reserved; passing it is a no-op.
        s = await registry.canonicalize(
            "auth service",
            type_hint="service",
            domain="software_dev",
        )
        assert isinstance(s, Subject)


# ---------------------------------------------------------------------------
# get() and list_aliases()
# ---------------------------------------------------------------------------


class TestGetAndListAliases:
    async def test_get_returns_none_for_unknown_id(
        self, registry: SubjectRegistry,
    ) -> None:
        assert await registry.get("service:does-not-exist") is None

    async def test_get_returns_subject_with_all_aliases(
        self,
        registry: SubjectRegistry,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        await registry.canonicalize("AUTH SERVICE", type_hint="service")
        s = await registry.get("service:auth-service")
        assert s is not None
        assert s.id == "service:auth-service"
        assert set(s.aliases) == {"auth service", "AUTH SERVICE"}

    async def test_list_aliases_returns_empty_for_unknown_id(
        self, registry: SubjectRegistry,
    ) -> None:
        assert await registry.list_aliases("service:nope") == ()

    async def test_list_aliases_returns_persisted_set(
        self,
        registry: SubjectRegistry,
    ) -> None:
        await registry.canonicalize("auth service", type_hint="service")
        await registry.canonicalize("Auth Service", type_hint="service")
        await registry.canonicalize("AUTH SERVICE", type_hint="service")
        aliases = await registry.list_aliases("service:auth-service")
        assert set(aliases) == {"auth service", "Auth Service", "AUTH SERVICE"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_empty_surface_mints_unknown_slug(
        self, registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize("", type_hint="service")
        assert s.id == "service:unknown"

    async def test_only_punctuation_mints_unknown_slug(
        self, registry: SubjectRegistry,
    ) -> None:
        s = await registry.canonicalize("---", type_hint="service")
        assert s.id == "service:unknown"
