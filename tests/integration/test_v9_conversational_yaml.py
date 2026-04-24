"""v9 Phase B'.0b.5c: conversational YAML domain integration test.

conversational is the first fully open-vocabulary domain — no
gazetteer, diversity taxonomy entirely inline.  Validates the
domain loads + every diversity node has a usable example pool.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_DOMAIN = _REPO / "adapters/domains/conversational"


@pytest.fixture(scope="module")
def spec():
    from ncms.application.adapters.domain_loader import load_domain
    if not _DOMAIN.is_dir():
        pytest.skip(f"conversational domain not present at {_DOMAIN}")
    return load_domain(_DOMAIN)


class TestConversationalYAMLDomain:
    def test_loads_cleanly(self, spec):
        assert spec.name == "conversational"
        assert spec.slots == ("object", "alternative", "frequency")
        assert "food_pref" in spec.topics
        assert "activity_pref" in spec.topics

    def test_no_gazetteer(self, spec):
        """Open-vocab domain — gazetteer intentionally empty."""
        assert spec.has_gazetteer is False
        assert spec.gazetteer == ()

    def test_diversity_has_multiple_topics(self, spec):
        """Need coverage across >=4 distinct topic_hints so the
        topic head gets training signal per label."""
        distinct_topics = {n.topic_hint for n in spec.diversity.nodes}
        assert len(distinct_topics) >= 4, (
            f"only {len(distinct_topics)} topics represented: {distinct_topics}"
        )

    def test_per_topic_member_floor(self, spec):
        """Every topic appearing in diversity should have >=30 members
        so the topic head can learn it."""
        from collections import Counter
        counts: Counter = Counter()
        for n in spec.diversity.nodes:
            pool = spec.diversity.resolve_examples(n, spec.gazetteer)
            counts[n.topic_hint] += len(pool)
        for topic, n_members in counts.items():
            assert n_members >= 30, (
                f"topic {topic!r} has only {n_members} members "
                "(floor 30 for reasonable topic-head training signal)"
            )

    def test_all_inline_nodes(self, spec):
        """Every node should be source=inline since there's no gazetteer."""
        for n in spec.diversity.nodes:
            assert n.source == "inline", (
                f"node {n.qualified_name!r} has source={n.source!r} "
                "but the domain has no gazetteer"
            )

    def test_no_duplicate_members_within_node(self, spec):
        """Each diversity node's example list should be unique."""
        for n in spec.diversity.nodes:
            examples = n.examples
            assert len(examples) == len(set(examples)), (
                f"node {n.qualified_name!r} has duplicate examples"
            )

    def test_archetypes_starter_set_present(self, spec):
        """Six starter archetypes cover the major joint labels."""
        names = {a.name for a in spec.archetypes}
        expected = {
            "positive_object_adoption",
            "choice_object_vs_alternative",
            "habitual_routine",
            "negative_object_retirement",
            "neutral_object_casual",
            "difficulty_temporary",
        }
        assert expected.issubset(names), (
            f"missing starter archetypes: {expected - names}"
        )

    def test_archetype_role_spans_reference_valid_slots(self, spec):
        slot_set = set(spec.slots)
        for a in spec.archetypes:
            for rs in a.role_spans:
                assert rs.slot in slot_set, (
                    f"archetype {a.name!r} uses slot {rs.slot!r} "
                    f"not in domain.slots {sorted(slot_set)}"
                )

    def test_total_inline_member_count_reasonable(self, spec):
        """Ballpark: ~500-1000 total members across all inline nodes."""
        total = sum(
            len(spec.diversity.resolve_examples(n, spec.gazetteer))
            for n in spec.diversity.nodes
        )
        assert 400 <= total <= 1500, (
            f"unexpected total inline member count: {total} "
            "(expected 400-1500 for conversational)"
        )
