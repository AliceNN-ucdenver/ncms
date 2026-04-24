"""Unit tests for the v9 stratified archetype generator.

Covers:

* :class:`TemplateBackend` — deterministic, phrasings-driven filler.
* :class:`SparkBackend` — mock-based tests for retry / short-count
  / malformed-response handling.  The live-endpoint test path is
  in the Phase B'.4 integration harness, not here.
* :func:`validate_and_label` — length / placeholder / entity / role
  composition checks.
* :func:`generate_for_archetype` and :func:`generate_domain` — the
  end-to-end orchestrator, using TemplateBackend + the shipped
  clinical / conversational / software_dev YAML plugins.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ncms.application.adapters.domain_loader import load_domain
from ncms.application.adapters.sdg.v9 import (
    GenerationStats,
    SparkBackend,
    TemplateBackend,
    generate_domain,
    generate_for_archetype,
    validate_and_label,
)
from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec, RoleSpec


_REPO = Path(__file__).resolve().parents[3]
_DOMAINS_ROOT = _REPO / "adapters/domains"


@pytest.fixture(scope="module")
def clinical_spec():
    d = _DOMAINS_ROOT / "clinical"
    if not d.is_dir():
        pytest.skip(f"clinical domain not present at {d}")
    return load_domain(d)


@pytest.fixture(scope="module")
def conversational_spec():
    d = _DOMAINS_ROOT / "conversational"
    if not d.is_dir():
        pytest.skip(f"conversational domain not present at {d}")
    return load_domain(d)


@pytest.fixture(scope="module")
def software_dev_spec():
    d = _DOMAINS_ROOT / "software_dev"
    if not d.is_dir():
        pytest.skip(f"software_dev domain not present at {d}")
    return load_domain(d)


# ---------------------------------------------------------------------------
# TemplateBackend
# ---------------------------------------------------------------------------


class TestTemplateBackend:
    def test_fills_free_text_placeholders(self):
        be = TemplateBackend(phrasings=(
            "Started patient on metformin {frequency} for {condition}.",
        ))
        rng = random.Random(17)
        rows = be.generate(prompt="", n=3, rng=rng)
        assert len(rows) == 3
        for r in rows:
            assert "{" not in r, f"placeholder leaked: {r}"
            assert "metformin" in r

    def test_empty_phrasings_returns_empty(self):
        be = TemplateBackend(phrasings=())
        rows = be.generate(prompt="", n=5, rng=random.Random(0))
        assert rows == []

    def test_rotates_through_phrasings(self):
        be = TemplateBackend(phrasings=(
            "alpha {condition}.",
            "beta {condition}.",
        ))
        rng = random.Random(17)
        rows = be.generate(prompt="", n=4, rng=rng)
        assert sum("alpha" in r for r in rows) == 2
        assert sum("beta" in r for r in rows) == 2

    def test_deterministic_with_seed(self):
        phrasings = ("Took {condition} today with {rationale}.",)
        r1 = TemplateBackend(phrasings=phrasings).generate(
            prompt="", n=3, rng=random.Random(42),
        )
        r2 = TemplateBackend(phrasings=phrasings).generate(
            prompt="", n=3, rng=random.Random(42),
        )
        assert r1 == r2

    def test_unknown_placeholder_falls_back(self):
        be = TemplateBackend(phrasings=("Weird {totally_unknown_thing}.",))
        rows = be.generate(prompt="", n=1, rng=random.Random(0))
        assert rows == ["Weird ….".replace("….", "….")]  # literal "…" filler


# ---------------------------------------------------------------------------
# validate_and_label
# ---------------------------------------------------------------------------


def _archetype_for_validation(
    *, min_chars: int = 20, max_chars: int = 160,
    role_spans: tuple[RoleSpec, ...] = (),
) -> ArchetypeSpec:
    return ArchetypeSpec(
        name="test_arch",
        domain="clinical",
        intent="positive",
        admission="persist",
        state_change="none",
        role_spans=role_spans,
        target_min_chars=min_chars,
        target_max_chars=max_chars,
        description="test",
    )


class TestValidateAndLabel:
    def test_empty_text_rejected(self):
        out = validate_and_label(
            "   ",
            archetype=_archetype_for_validation(),
            entities={},
            domain="clinical",
        )
        assert not out.ok
        assert out.reason == "empty_text"

    def test_too_short_rejected(self):
        out = validate_and_label(
            "short",
            archetype=_archetype_for_validation(min_chars=20),
            entities={},
            domain="clinical",
        )
        assert not out.ok
        assert out.reason == "too_short"

    def test_too_long_rejected(self):
        out = validate_and_label(
            "x" * 300,
            archetype=_archetype_for_validation(max_chars=100),
            entities={},
            domain="clinical",
        )
        assert not out.ok
        assert out.reason == "too_long"

    def test_placeholder_leak_rejected(self):
        out = validate_and_label(
            "Patient started on {primary} every morning for control.",
            archetype=_archetype_for_validation(),
            entities={},
            domain="clinical",
        )
        assert not out.ok
        assert out.reason == "placeholder_leak"

    def test_missing_entity_rejected(self):
        out = validate_and_label(
            "Patient reports improvement on therapy for several weeks.",
            archetype=_archetype_for_validation(),
            entities={("primary", "medication"): "metformin"},
            domain="clinical",
        )
        assert not out.ok
        assert out.reason == "missing_entity"
        assert "metformin" in out.detail

    def test_valid_clinical_row_labels_correctly(self):
        arch = _archetype_for_validation(
            role_spans=(RoleSpec(role="primary", slot="medication", count=1),),
        )
        out = validate_and_label(
            "Started patient on metformin for newly diagnosed diabetes.",
            archetype=arch,
            entities={("primary", "medication"): "metformin"},
            domain="clinical",
        )
        assert out.ok, out.detail
        assert len(out.role_spans) == 1
        span = out.role_spans[0]
        assert span.canonical == "metformin"
        assert span.role == "primary"
        assert span.slot == "medication"

    def test_wrong_role_composition_rejected(self):
        # Archetype asks for 1 primary medication, but the row
        # contains zero medications matching the signature.
        arch = _archetype_for_validation(
            role_spans=(RoleSpec(role="primary", slot="medication", count=1),),
        )
        out = validate_and_label(
            # "lisinopril" is a medication but we passed metformin as
            # the sampled entity — so the gazetteer will detect
            # lisinopril as not_relevant and find zero primary spans.
            # But we also have to include metformin for the entity
            # presence check. So craft a test that passes entity
            # presence but fails role match: use an entity with the
            # WRONG slot.
            "Patient mentioned atorvastatin at the visit after reviewing labs.",
            archetype=arch,
            entities={("primary", "medication"): "atorvastatin"},
            domain="clinical",
        )
        # This actually SUCCEEDS — atorvastatin IS in the gazetteer
        # as a medication, and it's the primary.  So we need a
        # harder test: entity mentioned but with wrong role
        # declaration.
        assert out.ok  # baseline success
        # Now force a failure: archetype wants 2 medications but
        # only one was sampled / mentioned.
        arch_two = _archetype_for_validation(
            role_spans=(
                RoleSpec(role="primary", slot="medication", count=1),
                RoleSpec(role="alternative", slot="medication", count=1),
            ),
        )
        out2 = validate_and_label(
            "Patient mentioned atorvastatin at the visit after reviewing labs.",
            archetype=arch_two,
            entities={("primary", "medication"): "atorvastatin"},
            domain="clinical",
        )
        # Entity presence passes (atorvastatin is there), but
        # role composition check should fail — archetype wants both
        # a primary and an alternative, only one is supplied.
        assert not out2.ok
        assert out2.reason == "wrong_role_spans"

    def test_open_vocab_labels_synthesized(self):
        arch = _archetype_for_validation(
            role_spans=(RoleSpec(role="primary", slot="object", count=1),),
        )
        out = validate_and_label(
            "Really getting into sourdough baking these days.",
            archetype=arch,
            entities={("primary", "object"): "sourdough baking"},
            domain="conversational",
        )
        assert out.ok, out.detail
        assert len(out.role_spans) == 1
        assert out.role_spans[0].surface == "sourdough baking"
        assert out.role_spans[0].role == "primary"

    def test_gazetteer_hit_not_in_entities_labeled_not_relevant(self):
        # Row mentions TWO medications but archetype only asks for
        # one (metformin).  The second (lisinopril) should land as
        # not_relevant, and overall validation should still pass.
        arch = _archetype_for_validation(
            role_spans=(RoleSpec(role="primary", slot="medication", count=1),),
        )
        out = validate_and_label(
            "Started patient on metformin; holding lisinopril for now.",
            archetype=arch,
            entities={("primary", "medication"): "metformin"},
            domain="clinical",
        )
        assert out.ok, out.detail
        roles = sorted(rs.role for rs in out.role_spans)
        assert "primary" in roles
        assert "not_relevant" in roles


# ---------------------------------------------------------------------------
# generate_for_archetype  (TemplateBackend)
# ---------------------------------------------------------------------------


class TestGenerateForArchetype:
    def test_clinical_positive_medication_start(self, clinical_spec):
        arch = next(
            a for a in clinical_spec.archetypes
            if a.name == "positive_medication_start"
        )
        be = TemplateBackend()  # empty — generator fills phrasings per-batch
        rows, stats = generate_for_archetype(
            clinical_spec, arch, n=10, backend=be, seed=7,
        )
        assert len(rows) == 10, f"yield too low: stats={stats}"
        assert stats.accepted == 10
        assert stats.yield_rate > 0.0
        # Every row should carry the fixed joint labels.
        for r in rows:
            assert r.intent == "positive"
            assert r.admission == "persist"
            assert r.state_change == "declaration"
            assert r.domain == "clinical"
            # Medication + frequency should be populated.
            assert "medication" in r.slots
            assert "frequency" in r.slots
            # Role spans should have at least the two required.
            roles = [rs.role for rs in r.role_spans]
            assert roles.count("primary") >= 2

    def test_conversational_open_vocab_archetype(self, conversational_spec):
        arch = next(
            a for a in conversational_spec.archetypes
            if a.name == "positive_object_adoption"
        )
        be = TemplateBackend()
        rows, stats = generate_for_archetype(
            conversational_spec, arch, n=8, backend=be, seed=5,
        )
        assert len(rows) >= 1, f"open-vocab zero yield: stats={stats}"
        for r in rows:
            assert r.domain == "conversational"
            assert r.intent == "positive"
            assert "object" in r.slots
            # Open-vocab role spans come from synthetic labels.
            assert any(rs.role == "primary" for rs in r.role_spans)

    def test_choice_archetype_samples_two_distinct_entities(self, clinical_spec):
        arch = next(
            a for a in clinical_spec.archetypes
            if a.name == "choice_medication_switch"
        )
        be = TemplateBackend()
        rows, _ = generate_for_archetype(
            clinical_spec, arch, n=5, backend=be, seed=11,
        )
        assert rows
        for r in rows:
            primary = r.slots.get("medication")
            alt = r.slots.get("alternative")
            assert primary and alt
            assert primary.lower() != alt.lower(), (
                f"choice row has identical primary + alternative: {primary!r}"
            )

    def test_stats_reflect_rejections(self):
        # Construct an archetype whose phrasings leak placeholders the
        # filler pool doesn't know — validation should reject many.
        arch = ArchetypeSpec(
            name="leaky_test", domain="clinical",
            intent="positive", admission="persist", state_change="none",
            role_spans=(RoleSpec(role="primary", slot="medication", count=1),),
            description="test", phrasings=("Short on {primary}.",),
            target_min_chars=100,  # too short envelope → forces rejects
        )
        # Use a minimal spec that has the slot.
        from ncms.application.adapters.domain_loader import DomainSpec, DiversityTaxonomy
        from ncms.application.adapters.sdg.catalog.primitives import CatalogEntry

        spec = DomainSpec(
            name="clinical",
            description="", intended_content="",
            slots=("medication",),
            topics=("medication_mgmt",),
            gazetteer=(
                CatalogEntry(
                    canonical="metformin", slot="medication",
                    topic="medication_mgmt",
                ),
            ),
            diversity=DiversityTaxonomy(nodes=()),
            archetypes=(arch,),
            gold_jsonl_path=Path("/tmp/x"),
            sdg_jsonl_path=Path("/tmp/x"),
            adversarial_jsonl_path=Path("/tmp/x"),
            adapter_output_root=Path("/tmp/x"),
            deployed_adapter_root=Path("/tmp/x"),
            default_adapter_version="v9",
            source_dir=Path("/tmp/x"),
        )
        rows, stats = generate_for_archetype(
            spec, arch, n=5, backend=TemplateBackend(), seed=3,
        )
        # Short-envelope target should produce 0 accepted + many rejects.
        assert stats.accepted == 0
        assert stats.rejections.get("too_short", 0) > 0
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# generate_domain
# ---------------------------------------------------------------------------


class TestGenerateDomain:
    def test_smoke_clinical_gold(self, clinical_spec):
        # Use a tiny override so the test runs fast — override n_gold
        # by monkeying the spec's archetypes in-place isn't easy
        # (dataclasses are frozen), so just take the real spec and
        # check we get >= 100 rows in total.
        be = TemplateBackend()
        rows, stats_by = generate_domain(
            clinical_spec, backend=be, split="gold", seed=1,
        )
        assert len(rows) >= 100, (
            f"combined gold yield too low: {len(rows)}"
        )
        # Every shipped archetype should have at least one stats entry.
        assert set(stats_by.keys()) == {a.name for a in clinical_spec.archetypes}
        # Spot check: every row is tagged gold + belongs to a
        # shipped archetype.
        arch_names = {a.name for a in clinical_spec.archetypes}
        for r in rows:
            assert r.split == "gold"
            src = r.source
            assert any(an in src for an in arch_names)

    def test_deterministic_with_seed(self, clinical_spec):
        be = TemplateBackend()
        rows1, _ = generate_domain(clinical_spec, backend=be, split="gold", seed=99)
        rows2, _ = generate_domain(clinical_spec, backend=be, split="gold", seed=99)
        assert [r.text for r in rows1] == [r.text for r in rows2]

    def test_different_seeds_diverge(self, clinical_spec):
        be = TemplateBackend()
        rows1, _ = generate_domain(clinical_spec, backend=be, split="gold", seed=1)
        rows2, _ = generate_domain(clinical_spec, backend=be, split="gold", seed=2)
        # Not bitwise-identical, but overlap should be < 100%.
        t1 = {r.text for r in rows1}
        t2 = {r.text for r in rows2}
        overlap = len(t1 & t2) / max(len(t1 | t2), 1)
        assert overlap < 0.95, f"seeds produced near-identical corpora: {overlap}"


# ---------------------------------------------------------------------------
# SparkBackend (mocked — no live LLM spend)
# ---------------------------------------------------------------------------


class TestSparkBackend:
    """Mock-based tests for SparkBackend.

    We patch :meth:`SparkBackend._call_once` with an
    :class:`unittest.mock.AsyncMock` — its ``return_value`` /
    ``side_effect`` appear AFTER the await, so the backend's
    ``asyncio.run(self._call_once(...))`` path receives the raw
    value without any coroutine plumbing in the test.  This is
    critical because :class:`MagicMock` with a coroutine in
    ``return_value`` breaks on the first retry (coroutines can
    only be awaited once).
    """

    def _rng(self) -> random.Random:
        return random.Random(0)

    def _patch_call_once(self, **async_mock_kwargs):
        """Shorthand: replace ``SparkBackend._call_once`` with an AsyncMock."""
        return patch.object(
            SparkBackend, "_call_once",
            new_callable=AsyncMock, **async_mock_kwargs,
        )

    def test_happy_path_returns_rows(self):
        be = SparkBackend(model="openai/test", api_base="http://x")
        with self._patch_call_once(return_value=["row one", "row two", "row three"]):
            rows = be.generate(prompt="ignored", n=3, rng=self._rng())
        assert rows == ["row one", "row two", "row three"]

    def test_cap_to_requested_n(self):
        """Model overshoots; we cap silently to the requested count."""
        be = SparkBackend(model="openai/test", api_base="http://x")
        with self._patch_call_once(return_value=["a", "b", "c", "d", "e"]):
            rows = be.generate(prompt="ignored", n=3, rng=self._rng())
        assert rows == ["a", "b", "c"]

    def test_short_count_logged_not_raised(self, caplog):
        """Model returns fewer rows than asked — WARNING logged, no exception."""
        import logging
        be = SparkBackend(model="openai/test", api_base="http://x")
        with caplog.at_level(
            logging.WARNING,
            logger="ncms.application.adapters.sdg.v9.backends",
        ):
            with self._patch_call_once(return_value=["only one"]):
                rows = be.generate(prompt="ignored", n=5, rng=self._rng())
        assert rows == ["only one"]
        assert any("short-count" in rec.message for rec in caplog.records)

    def test_strips_blank_and_nonstring(self):
        be = SparkBackend(model="openai/test", api_base="http://x")
        with self._patch_call_once(
            return_value=["  row one  ", "", None, 42, "row two"],
        ):
            rows = be.generate(prompt="ignored", n=5, rng=self._rng())
        # "" and None drop; integer 42 coerces to "42"; rows trim.
        assert rows == ["row one", "42", "row two"]

    def test_non_list_response_retries_then_raises(self):
        """call_llm_json returned a dict — malformed; retry then fail."""
        be = SparkBackend(
            model="openai/test", api_base="http://x",
            max_attempts=2, backoff_base_seconds=0.0,
        )
        with self._patch_call_once(
            side_effect=[{"wrong": "shape"}, {"still": "wrong"}],
        ):
            with pytest.raises(RuntimeError, match="malformed"):
                be.generate(prompt="ignored", n=3, rng=self._rng())

    def test_transient_error_retried_then_succeeds(self):
        """First call raises a network-style error; second succeeds."""
        be = SparkBackend(
            model="openai/test", api_base="http://x",
            max_attempts=3, backoff_base_seconds=0.0,
        )
        with self._patch_call_once(
            side_effect=[
                ConnectionError("temporary 503"),
                ["recovered", "row"],
            ],
        ):
            rows = be.generate(prompt="ignored", n=2, rng=self._rng())
        assert rows == ["recovered", "row"]

    def test_all_attempts_fail_raises_with_last_error(self):
        be = SparkBackend(
            model="openai/test", api_base="http://x",
            max_attempts=2, backoff_base_seconds=0.0,
        )
        with self._patch_call_once(
            side_effect=[
                TimeoutError("attempt 1"),
                TimeoutError("attempt 2"),
            ],
        ):
            with pytest.raises(RuntimeError, match="all 2 attempts failed"):
                be.generate(prompt="ignored", n=2, rng=self._rng())
