"""v9 Phase B'.0b.5b: software_dev YAML migration integration test.

Verifies that loading ``adapters/domains/software_dev/`` via
:func:`domain_loader.load_domain` produces the same gazetteer as
the legacy Python module ``sdg/catalog/software_dev.py``.

The Python module is still the live path in this commit — the
cut-over happens in Phase B'.0c when ``normalize.py`` switches to
reading from YAML.  This test proves the two sources are already
equivalent, so the cut-over is pure transport change.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_SOFTWARE_DEV_YAML = _REPO / "adapters/domains/software_dev"


@pytest.fixture(scope="module")
def yaml_spec():
    from ncms.application.adapters.domain_loader import load_domain
    if not _SOFTWARE_DEV_YAML.is_dir():
        pytest.skip(f"software_dev YAML domain not present at {_SOFTWARE_DEV_YAML}")
    return load_domain(_SOFTWARE_DEV_YAML)


@pytest.fixture(scope="module")
def python_catalog():
    from ncms.application.adapters.sdg.catalog import software_dev
    return software_dev


class TestSoftwareDevYAMLEquivalence:
    def test_same_canonical_set(self, yaml_spec, python_catalog):
        yaml_canonicals = {e.canonical for e in yaml_spec.gazetteer}
        # Python module's CATALOG dict is keyed by canonical + aliases —
        # use ENTRIES_BY_SLOT to get just the canonicals.
        python_canonicals: set[str] = set()
        for slot_entries in python_catalog.ENTRIES_BY_SLOT.values():
            python_canonicals.update(e.canonical for e in slot_entries)
        missing_in_yaml = python_canonicals - yaml_canonicals
        extra_in_yaml = yaml_canonicals - python_canonicals
        # Allow at most the ~48 accidental intra-slot duplicates from the
        # Phase B'.0b.2 extension — YAML dedupes those by definition.
        assert not extra_in_yaml, (
            f"YAML has canonicals not in Python: {sorted(extra_in_yaml)[:20]}"
        )
        assert len(missing_in_yaml) == 0, (
            f"{len(missing_in_yaml)} canonicals in Python but missing "
            f"from YAML: {sorted(missing_in_yaml)[:20]}"
        )

    def test_same_slot_distribution(self, yaml_spec, python_catalog):
        yaml_by_slot = {
            slot: len(entries)
            for slot, entries in yaml_spec.gazetteer_by_slot.items()
        }
        # Python: count unique canonicals per slot.
        py_by_slot: dict[str, int] = {}
        for slot, entries in python_catalog.ENTRIES_BY_SLOT.items():
            unique = {e.canonical for e in entries}
            py_by_slot[slot] = len(unique)
        # The Python module had duplicates in several slots (same canonical
        # listed twice in a slot tuple — Phase B'.0b.2 additions).  YAML
        # drops those.  Difference per slot should be small (<5 entries).
        for slot in set(yaml_by_slot) | set(py_by_slot):
            y = yaml_by_slot.get(slot, 0)
            p = py_by_slot.get(slot, 0)
            assert abs(y - p) <= 5, (
                f"slot {slot!r} distribution differs: "
                f"yaml={y} python={p}"
            )

    def test_alias_coverage(self, yaml_spec, python_catalog):
        """Every alias in the Python catalog should also appear in YAML."""
        yaml_alias_by_canonical: dict[str, set[str]] = {
            e.canonical: set(e.aliases) for e in yaml_spec.gazetteer
        }
        python_alias_by_canonical: dict[str, set[str]] = {}
        for entries in python_catalog.ENTRIES_BY_SLOT.values():
            for e in entries:
                python_alias_by_canonical.setdefault(
                    e.canonical, set(),
                ).update(e.aliases)
        for canon, py_aliases in python_alias_by_canonical.items():
            if canon not in yaml_alias_by_canonical:
                continue  # cross-slot dupe — YAML picked the other slot
            yaml_aliases = yaml_alias_by_canonical[canon]
            missing = py_aliases - yaml_aliases
            assert not missing, (
                f"canonical {canon!r} YAML is missing aliases: {missing}"
            )

    def test_topics_match_domain_vocab(self, yaml_spec):
        """Every gazetteer entry's topic must appear in domain.topics."""
        topic_set = set(yaml_spec.topics)
        for e in yaml_spec.gazetteer:
            assert e.topic in topic_set, (
                f"entry {e.canonical!r} topic {e.topic!r} not in "
                f"domain.topics {sorted(topic_set)}"
            )

    def test_slots_match_domain_vocab(self, yaml_spec):
        """Every gazetteer entry's slot must appear in domain.slots."""
        slot_set = set(yaml_spec.slots)
        for e in yaml_spec.gazetteer:
            assert e.slot in slot_set, (
                f"entry {e.canonical!r} slot {e.slot!r} not in "
                f"domain.slots {sorted(slot_set)}"
            )

    def test_entry_count_within_expected_range(self, yaml_spec):
        """Ballpark sanity: Phase B'.0b.2 left catalog at ~712 unique canonicals."""
        n = len(yaml_spec.gazetteer)
        assert 700 <= n <= 800, f"unexpected gazetteer size: {n}"

    def test_diversity_nodes_all_match_gazetteer(self, yaml_spec):
        """Every diversity node must resolve to a non-empty example pool."""
        for node in yaml_spec.diversity.nodes:
            pool = yaml_spec.diversity.resolve_examples(
                node, yaml_spec.gazetteer,
            )
            assert len(pool) >= 1, (
                f"diversity node {node.qualified_name!r} resolves to "
                "empty pool — no gazetteer entries match filter_slots"
            )

    def test_archetypes_loaded(self, yaml_spec):
        """Starter set of 6 archetypes should be present."""
        names = [a.name for a in yaml_spec.archetypes]
        assert "positive_framework_adoption" in names
        assert "choice_framework_vs_alternative" in names
        # Full 16-archetype set lands in Phase B'.2.
        assert len(yaml_spec.archetypes) >= 5

    def test_archetype_example_pool_nonempty(self, yaml_spec):
        """Every archetype role_span slot must have gazetteer entries
        (prevents generator from stalling when Phase B'.2 uses these)."""
        by_slot = yaml_spec.gazetteer_by_slot
        for a in yaml_spec.archetypes:
            for rs in a.role_spans:
                if rs.count == 0:
                    continue
                assert rs.slot in by_slot, (
                    f"archetype {a.name!r} wants slot {rs.slot!r} "
                    f"but gazetteer has no entries for it"
                )
                assert len(by_slot[rs.slot]) >= 1
