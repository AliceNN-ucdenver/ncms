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
    SparkBackend,
    TemplateBackend,
    build_archetype_prompt,
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
        from ncms.application.adapters.domain_loader import DiversityTaxonomy, DomainSpec
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
        ), self._patch_call_once(return_value=["only one"]):
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
        ), pytest.raises(RuntimeError, match="malformed"):
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
        ), pytest.raises(RuntimeError, match="all 2 attempts failed"):
            be.generate(prompt="ignored", n=2, rng=self._rng())


# ---------------------------------------------------------------------------
# build_archetype_prompt — label-leakage + per-row assignment
# ---------------------------------------------------------------------------


class TestBuildArchetypePrompt:
    """Guard-rail tests for the LLM prompt.

    The two critical invariants:

    1. Literal joint-label tokens (``persist``, ``declaration``,
       ``positive``) must NOT appear in the prompt — otherwise the
       LLM echoes them into surface text (caught in B'.4 probing).
    2. Each row in ``entity_rows`` must be presented as its own
       row-numbered line so the LLM knows to produce DIFFERENT
       rows (not N paraphrases of one sentence).
    """

    def _archetype(self) -> ArchetypeSpec:
        return ArchetypeSpec(
            name="test_positive_declaration",
            domain="clinical",
            intent="positive",
            admission="persist",
            state_change="declaration",
            role_spans=(
                RoleSpec(role="primary", slot="medication", count=1),
            ),
            description="Clinician starts a patient on a new medication.",
            example_utterances=(
                "Started metformin 500mg BID.",
            ),
            phrasings=("Started patient on {primary}.",),
        )

    def test_no_literal_label_tokens_in_prompt(self):
        """The three joint-label values must not appear verbatim."""
        arch = self._archetype()
        prompt = build_archetype_prompt(
            arch,
            entity_rows=[
                {("primary", "medication"): "metformin"},
            ],
        )
        # Case-sensitive check — the label vocabulary is all
        # lowercase so a case-insensitive check would catch
        # legitimate English uses of "positive" in descriptions.
        for forbidden in ("intent: positive", "admission: persist",
                          "state_change: declaration"):
            assert forbidden not in prompt, (
                f"literal label token {forbidden!r} leaked into prompt"
            )

    def test_behavioral_cues_present(self):
        """The three heads must be described, not labelled."""
        arch = self._archetype()
        prompt = build_archetype_prompt(
            arch,
            entity_rows=[
                {("primary", "medication"): "metformin"},
            ],
        )
        # Behavioural-description anchors from prompts.py.
        assert "Speaker stance" in prompt
        assert "Persistence" in prompt
        assert "State transition" in prompt
        # And the actual description text for this archetype's labels.
        assert "approval" in prompt or "enthusiasm" in prompt
        assert "long-term" in prompt or "facts" in prompt
        assert "NEW state" in prompt or "adoption" in prompt

    def test_row_specific_entity_assignments(self):
        """Each entity_rows entry gets its own 'Row N: ...' line."""
        arch = self._archetype()
        prompt = build_archetype_prompt(
            arch,
            entity_rows=[
                {("primary", "medication"): "metformin"},
                {("primary", "medication"): "atorvastatin"},
                {("primary", "medication"): "lisinopril"},
            ],
        )
        assert "Row 1:" in prompt
        assert "Row 2:" in prompt
        assert "Row 3:" in prompt
        assert "metformin" in prompt
        assert "atorvastatin" in prompt
        assert "lisinopril" in prompt

    def test_output_format_asks_for_exact_count(self):
        """The output instruction must name the correct row count."""
        arch = self._archetype()
        prompt = build_archetype_prompt(
            arch,
            entity_rows=[{("primary", "medication"): "x"}] * 5,
        )
        assert "exactly 5 strings" in prompt

    def test_empty_entity_rows_raises(self):
        """Zero rows is a caller bug — fail fast."""
        arch = self._archetype()
        with pytest.raises(ValueError):
            build_archetype_prompt(arch, entity_rows=[])


# ---------------------------------------------------------------------------
# Per-row entity diversity in generate_for_archetype
# ---------------------------------------------------------------------------


class TestPerRowEntitySampling:
    """After the B'.4 fix, a batch of N rows must produce N DISTINCT
    entity assignments when the source pool is large enough.

    The regression we're guarding against: in B'.2 the generator
    sampled ONE entity set per batch and asked the LLM for N rows
    using that set — the result was N paraphrases of one sentence.
    """

    @pytest.fixture(scope="class")
    def clinical_spec(self):
        d = _DOMAINS_ROOT / "clinical"
        if not d.is_dir():
            pytest.skip(f"clinical domain not present at {d}")
        return load_domain(d)

    def test_row_topic_inherits_from_gazetteer_entry(self, clinical_spec):
        """Emitted GoldExample.topic must match the primary entity's
        gazetteer-declared topic — NOT None.

        Regression guard for the B'.4 full-run bug where every
        emitted row carried ``topic=None`` because the generator
        only looked at ``archetype.topic`` (unset on all three
        shipped domains) and ignored gazetteer-entry topics.
        """
        arch = next(
            a for a in clinical_spec.archetypes
            if a.name == "habitual_medication_regimen"
        )
        rows, _ = generate_for_archetype(
            clinical_spec, arch,
            n=5, backend=TemplateBackend(), seed=17,
        )
        topics = [r.topic for r in rows]
        assert all(t is not None for t in topics), (
            f"expected non-None topics, got {topics}"
        )
        # Topic must be a valid clinical-domain topic drawn from the
        # gazetteer entry (not None, not an unknown string).
        allowed = set(clinical_spec.topics)
        for t in topics:
            assert t in allowed, (
                f"topic {t!r} not in clinical topic vocab {sorted(allowed)}"
            )

    def test_row_topic_inherits_from_inline_node(self, conversational_spec):
        """Open-vocab domain: topic must come from the sampled
        inline node's ``topic_hint``.
        """
        arch = next(
            a for a in conversational_spec.archetypes
            if a.name == "positive_object_adoption"
        )
        rows, _ = generate_for_archetype(
            conversational_spec, arch,
            n=10, backend=TemplateBackend(), seed=17,
        )
        # Every row's topic must be in the conversational topic vocab
        # (not None, not "habit_pref" — since frequency nodes are
        # filter_slots-scoped, they can't supply object-slot entities).
        allowed_object_topics = set(conversational_spec.topics) - {"habit_pref"}
        for r in rows:
            assert r.topic is not None, (
                f"row had None topic: {r.text}"
            )
            assert r.topic in allowed_object_topics, (
                f"row topic {r.topic!r} not in "
                f"{sorted(allowed_object_topics)}: {r.text}"
            )

    def test_batch_uses_multiple_medications(self, clinical_spec):
        """Across a 10-row batch from habitual_medication_regimen,
        more than one medication must appear in slots."""
        arch = next(
            a for a in clinical_spec.archetypes
            if a.name == "habitual_medication_regimen"
        )
        rows, _ = generate_for_archetype(
            clinical_spec, arch,
            n=10, backend=TemplateBackend(), seed=42,
        )
        meds = {r.slots.get("medication") for r in rows}
        # The medication gazetteer has > 150 entries; sampling 10
        # with replacement should yield > 1 distinct med almost
        # always.  We use >= 3 as a robust floor that still
        # guarantees we're not reusing a single pick.
        assert len(meds) >= 3, (
            f"expected diverse medications; got {meds} across "
            f"{len(rows)} rows"
        )


# ---------------------------------------------------------------------------
# DiversityNode.filter_slots scoping inline nodes to specific slots
# ---------------------------------------------------------------------------


class TestInlineNodeSlotScoping:
    """Regression test for the B'.4 "in the afternoon" sampled as an
    object-slot entity bug.

    Time-of-day / frequency / period inline nodes in conversational
    diversity are now tagged ``filter_slots: [frequency]`` so the
    generator can't pick them when sampling for ``slot: object``.
    """

    @pytest.fixture(scope="class")
    def conversational_spec(self):
        d = _DOMAINS_ROOT / "conversational"
        if not d.is_dir():
            pytest.skip(f"conversational domain not present at {d}")
        return load_domain(d)

    def test_times_nodes_declare_frequency_only(self, conversational_spec):
        """The YAML change is honoured by the loader."""
        times_nodes = [
            n for n in conversational_spec.diversity.nodes
            if n.path and n.path[0] == "times"
        ]
        assert times_nodes, "expected times.* nodes in conversational diversity"
        for n in times_nodes:
            assert n.filter_slots == ("frequency",), (
                f"times.{'.'.join(n.path[1:])}: filter_slots="
                f"{n.filter_slots} (expected ('frequency',))"
            )

    def test_object_slot_sampling_skips_frequency_nodes(
        self, conversational_spec,
    ):
        """Direct test on the generator: asking for ``slot=object``
        must never return a frequency-phrase example.
        """
        from ncms.application.adapters.sdg.v9.generator import _draw_one

        arch = next(
            a for a in conversational_spec.archetypes
            if a.name == "positive_object_adoption"
        )
        inline_nodes = tuple(
            n for n in conversational_spec.diversity.nodes
            if n.source == "inline"
        )
        # Collect every frequency-only example so we can assert
        # absence across many draws.
        frequency_only_examples = {
            ex.lower()
            for n in inline_nodes
            if n.filter_slots == ("frequency",)
            for ex in n.examples
        }
        assert frequency_only_examples, "test setup: no frequency nodes found"

        # Draw 50 times — with uniform node sampling previously,
        # ~3/31 ≈ 10% of draws would have hit frequency nodes.
        # Post-fix, the probability is zero.
        drawn = []
        rng_state = random.Random(123)
        for _ in range(50):
            draw = _draw_one(
                slot="object",
                gaz_by_slot={},  # no gazetteer for conversational
                inline_nodes=inline_nodes,
                archetype=arch,
                already_used=set(),
                rng=rng_state,
            )
            if draw is not None:
                surface, _topic = draw
                drawn.append(surface.lower())

        assert drawn, "expected _draw_one to return SOMETHING"
        leaks = [s for s in drawn if s in frequency_only_examples]
        assert leaks == [], (
            f"object-slot draw returned frequency-scoped phrases: {leaks}"
        )

    def test_frequency_slot_still_reaches_frequency_nodes(
        self, conversational_spec,
    ):
        """Sanity: the filter_slots change must not cut off the
        frequency slot from its legitimate sources.
        """
        from ncms.application.adapters.sdg.v9.generator import _draw_one

        arch = next(
            a for a in conversational_spec.archetypes
            if a.name == "habitual_routine"
        )
        inline_nodes = tuple(
            n for n in conversational_spec.diversity.nodes
            if n.source == "inline"
        )
        rng_state = random.Random(99)
        drawn: list[str] = []
        for _ in range(30):
            draw = _draw_one(
                slot="frequency",
                gaz_by_slot={},
                inline_nodes=inline_nodes,
                archetype=arch,
                already_used=set(),
                rng=rng_state,
            )
            if draw is not None:
                surface, _topic = draw
                drawn.append(surface.lower())
        assert drawn, "frequency-slot sampling returned nothing"
        # At least one draw must come from the frequency-scoped pool
        # — if we never hit it, the filter went the wrong way.
        frequency_examples = {
            ex.lower()
            for n in inline_nodes
            if "frequency" in n.filter_slots
            for ex in n.examples
        }
        hits = sum(1 for s in drawn if s in frequency_examples)
        assert hits > 0, (
            f"frequency-slot draws never hit scoped frequency pool; "
            f"drew: {drawn[:5]}..."
        )
