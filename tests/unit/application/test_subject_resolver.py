"""Phase A — resolve_subjects() precedence + SLM auto-suggest unit tests.

Covers:
- claim A.3 precedence chain (caller subjects > caller subject > SLM > empty).
- claim A.17 SLM ``role_head`` ``primary`` spans auto-suggest path
  (the GLiNER-retirement wiring).
- ValueError on conflicting primaries.

Integration tests for the full ``store_memory(subjects=…)`` flow
live in ``tests/integration/test_subject_payload.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiosqlite
import pytest

from ncms.application.subject import (
    SubjectRegistry,
    resolve_subjects,
)
from ncms.config import NCMSConfig
from ncms.domain.models import Subject
from ncms.infrastructure.storage.migrations import create_schema

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


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:")


@dataclass
class _FakeRoleSpan:
    """Shape match for the v9 SLM RoleSpan dataclass."""

    surface: str
    canonical: str
    slot: str
    role: str
    char_start: int = 0
    char_end: int = 0


@dataclass
class _FakeIntentSlotLabel:
    """Minimal duck-typed ExtractedLabel for resolver tests.

    We avoid importing the real ``ExtractedLabel`` because it carries
    a heavy import surface (training schemas).  The resolver only
    cares about a few attributes.
    """

    intent: str = "positive"
    intent_confidence: float = 0.95
    role_spans: tuple = field(default_factory=tuple)
    slot_confidences: dict = field(default_factory=dict)

    def is_confident(self, threshold: float = 0.7) -> bool:
        return self.intent_confidence >= threshold


# ---------------------------------------------------------------------------
# Precedence 1: subjects=[...] wins
# ---------------------------------------------------------------------------


class TestPrecedenceCallerSubjectsList:
    async def test_caller_subjects_list_returned_unchanged(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                aliases=("auth-service",),
                source="caller",
            ),
        ]
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=["software_dev"],
            subject_legacy=None,
            subjects_explicit=explicit,
            intent_slot_label=None,
        )
        assert len(out) == 1
        assert out[0].id == "service:auth-api"
        assert out[0].source == "caller"

    async def test_caller_subjects_persisted_in_registry(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
        db: aiosqlite.Connection,
    ) -> None:
        """Caller-passed subjects should be findable on a future call."""
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                aliases=("auth-service", "Auth Service"),
                source="caller",
            ),
        ]
        await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=explicit,
            intent_slot_label=None,
        )
        # Now a surface that was an alias should resolve to the same id.
        s = await registry.canonicalize("auth-service", type_hint="service")
        assert s.id == "service:auth-api"
        assert s.confidence == 1.0  # exact alias hit

    async def test_caller_list_with_no_primary_promotes_first(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        explicit = [
            Subject(id="adr:004", type="decision", primary=False),
            Subject(id="service:auth-api", type="service", primary=False),
        ]
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=explicit,
            intent_slot_label=None,
        )
        # First subject promoted to primary.
        assert out[0].primary is True
        assert out[1].primary is False

    async def test_subjects_list_takes_precedence_over_subject_string(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """Both kwargs agree → subjects= wins (no raise; redundant is OK)."""
        # subject="auth-api" + type_hint="service" canonicalizes to
        # "service:auth-api" — same as the explicit Subject's id.
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                source="caller",
            ),
        ]
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy="auth-api",
            subjects_explicit=explicit,
            intent_slot_label=None,
        )
        assert len(out) == 1
        assert out[0].id == "service:auth-api"

    async def test_conflicting_kwargs_raises(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """Both kwargs disagree on canonical id → ValueError (A.3 cross-kwarg)."""
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                source="caller",
            ),
        ]
        with pytest.raises(ValueError, match="Conflicting primary"):
            await resolve_subjects(
                registry=registry,
                config=config,
                domains=None,
                subject_legacy="payments-service",
                subjects_explicit=explicit,
                intent_slot_label=None,
            )

    async def test_conflicting_kwargs_does_not_mutate_registry(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
        db: aiosqlite.Connection,
    ) -> None:
        """A.3: a raised conflict leaves the registry untouched.

        Codex round-1 caught that the original conflict-check
        called ``registry.canonicalize`` which idempotently mints
        + persists, leaving a row behind on a "validation failure."
        The fix uses a deterministic formula that doesn't touch
        the registry.
        """
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                source="caller",
            ),
        ]
        with pytest.raises(ValueError, match="Conflicting primary"):
            await resolve_subjects(
                registry=registry,
                config=config,
                domains=None,
                subject_legacy="payments-service",
                subjects_explicit=explicit,
                intent_slot_label=None,
            )
        # The registry should be empty — no canonical_id was minted.
        cur = await db.execute("SELECT COUNT(*) FROM subjects")
        row = await cur.fetchone()
        assert row[0] == 0, (
            "conflict raise unexpectedly minted a subject row"
        )
        cur = await db.execute("SELECT COUNT(*) FROM subject_aliases")
        row = await cur.fetchone()
        assert row[0] == 0, (
            "conflict raise unexpectedly minted an alias row"
        )

    async def test_conflicting_primaries_raises(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """Two Subjects with primary=True → ValueError."""
        explicit = [
            Subject(id="adr:004", type="decision", primary=True),
            Subject(id="adr:002", type="decision", primary=True),
        ]
        with pytest.raises(ValueError, match="primary"):
            await resolve_subjects(
                registry=registry,
                config=config,
                domains=None,
                subject_legacy=None,
                subjects_explicit=explicit,
                intent_slot_label=None,
            )


# ---------------------------------------------------------------------------
# Precedence 2: subject="..." promotes to single-element list
# ---------------------------------------------------------------------------


class TestPrecedenceLegacySubjectString:
    async def test_legacy_subject_string_promoted_to_list(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy="adr:004",
            subjects_explicit=None,
            intent_slot_label=None,
        )
        assert len(out) == 1
        assert out[0].source == "caller"
        assert out[0].primary is True
        # Canonicalized: "adr:004" → "subject:adr-004" (no type hint
        # was given to canonicalize from a bare string, so type
        # defaults to "subject").
        assert out[0].id == "subject:adr-004"


# ---------------------------------------------------------------------------
# Precedence 3: SLM auto-suggest (claim A.17)
# ---------------------------------------------------------------------------


class TestPrecedenceSLMAutoSuggest:
    async def test_primary_role_span_becomes_subject(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """A.17 headline test: SLM primary span auto-becomes a subject."""
        label = _FakeIntentSlotLabel(
            intent_confidence=0.92,
            role_spans=(
                _FakeRoleSpan(
                    surface="auth-service",
                    canonical="auth-service",
                    slot="service",
                    role="primary",
                ),
            ),
            slot_confidences={"service": 0.92},
        )
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=["software_dev"],
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=label,
        )
        assert len(out) == 1
        assert out[0].source == "slm_role"
        assert out[0].primary is True
        assert out[0].id == "service:auth-service"
        # Confidence inherits the SLM signal (≤ slot_confidence).
        assert out[0].confidence <= 0.92

    async def test_multiple_primary_spans_become_co_subjects(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        label = _FakeIntentSlotLabel(
            intent_confidence=0.9,
            role_spans=(
                _FakeRoleSpan(
                    surface="auth-service",
                    canonical="auth-service",
                    slot="service",
                    role="primary",
                ),
                _FakeRoleSpan(
                    surface="adr-004",
                    canonical="adr-004",
                    slot="decision",
                    role="primary",
                ),
            ),
        )
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=["software_dev"],
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=label,
        )
        assert len(out) == 2
        # First is primary, rest are co-subjects.
        assert out[0].primary is True
        assert out[1].primary is False
        assert {s.id for s in out} == {"service:auth-service", "decision:adr-004"}

    async def test_alternative_role_spans_skipped(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        label = _FakeIntentSlotLabel(
            role_spans=(
                _FakeRoleSpan(
                    surface="auth-service",
                    canonical="auth-service",
                    slot="service",
                    role="alternative",
                ),
                _FakeRoleSpan(
                    surface="cart-service",
                    canonical="cart-service",
                    slot="service",
                    role="casual",
                ),
            ),
        )
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=label,
        )
        assert out == []

    async def test_low_confidence_extraction_skipped(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """A.17: spans below confidence threshold are not promoted."""
        # config.slm_confidence_threshold default is 0.3 — set the
        # label's confidence below it to make sure we abstain.
        label = _FakeIntentSlotLabel(
            intent_confidence=0.05,  # below threshold
            role_spans=(
                _FakeRoleSpan(
                    surface="auth-service",
                    canonical="auth-service",
                    slot="service",
                    role="primary",
                ),
            ),
        )
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=label,
        )
        assert out == []

    async def test_no_slm_label_no_subjects(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """When SLM chain is dark, intent_slot_label is None → no subjects."""
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=None,
        )
        assert out == []

    async def test_caller_subjects_override_slm_spans(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        """Precedence: caller subjects= beats SLM primary spans."""
        label = _FakeIntentSlotLabel(
            intent_confidence=0.95,
            role_spans=(
                _FakeRoleSpan(
                    surface="payments-service",
                    canonical="payments-service",
                    slot="service",
                    role="primary",
                ),
            ),
        )
        explicit = [
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                source="caller",
            ),
        ]
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=["software_dev"],
            subject_legacy=None,
            subjects_explicit=explicit,
            intent_slot_label=label,
        )
        # Only the caller subject — SLM ignored entirely.
        assert len(out) == 1
        assert out[0].id == "service:auth-api"
        assert out[0].source == "caller"


# ---------------------------------------------------------------------------
# Precedence 4: empty (no caller, no SLM)
# ---------------------------------------------------------------------------


class TestPrecedenceEmpty:
    async def test_no_inputs_returns_empty_list(
        self,
        registry: SubjectRegistry,
        config: NCMSConfig,
    ) -> None:
        out = await resolve_subjects(
            registry=registry,
            config=config,
            domains=None,
            subject_legacy=None,
            subjects_explicit=None,
            intent_slot_label=None,
        )
        assert out == []
