"""Phase A — SubjectRegistry skeleton (sub-PR 1 surface only).

Sub-PR 1 ships a stub: ``canonicalize()`` always mints a new
canonical id from the surface (no alias lookup yet).  These tests
lock the API surface so sub-PR 2 can replace the body without
breaking callers.

Real canonicalization tests (alias variants resolve, exact >
fuzzy, minted persists) land in sub-PR 2 alongside the alias-
collision audit event.
"""

from __future__ import annotations

import aiosqlite
import pytest

from ncms.application.subject_registry import (
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
# Skeleton API surface (sub-PR 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonicalize_returns_subject_with_minted_confidence() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        reg = SubjectRegistry(db)
        s = await reg.canonicalize(
            "auth service",
            type_hint="service",
            domain="software_dev",
        )
        assert isinstance(s, Subject)
        # Sub-PR 1 stub: minted confidence is 0.6 per claim A.5.
        assert s.confidence == 0.6
        assert s.id == "service:auth-service"
        assert s.type == "service"
        assert s.aliases == ("auth service",)


@pytest.mark.asyncio
async def test_canonicalize_default_type_when_hint_missing() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        reg = SubjectRegistry(db)
        s = await reg.canonicalize("auth service")
        assert s.type == "subject"
        assert s.id == "subject:auth-service"


@pytest.mark.asyncio
async def test_canonicalize_propagates_source() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        reg = SubjectRegistry(db)
        s = await reg.canonicalize(
            "auth service",
            type_hint="service",
            source="slm_role",
        )
        assert s.source == "slm_role"


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_id_in_skeleton() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        reg = SubjectRegistry(db)
        # Sub-PR 1 stub: get() always returns None.
        assert await reg.get("service:auth-api") is None


@pytest.mark.asyncio
async def test_list_aliases_returns_empty_in_skeleton() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        reg = SubjectRegistry(db)
        assert await reg.list_aliases("service:auth-api") == ()
