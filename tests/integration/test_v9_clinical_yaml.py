"""v9 Phase B'.0b.5d: clinical YAML domain integration test.

Verifies the clinical domain loads cleanly, has expected gazetteer
coverage, and every diversity node partitions correctly by slot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DOMAIN = _REPO / "adapters/domains/clinical"


@pytest.fixture(scope="module")
def spec():
    from ncms.application.adapters.domain_loader import load_domain

    if not _DOMAIN.is_dir():
        pytest.skip(f"clinical domain not present at {_DOMAIN}")
    return load_domain(_DOMAIN)


class TestClinicalYAMLDomain:
    def test_loads_cleanly(self, spec):
        assert spec.name == "clinical"
        assert "medication" in spec.slots
        assert "procedure" in spec.slots
        assert "symptom" in spec.slots
        assert "severity" in spec.slots

    def test_gazetteer_has_sufficient_coverage(self, spec):
        """Starter gazetteer should cover common outpatient vocabulary."""
        assert len(spec.gazetteer) >= 400, f"clinical gazetteer is too small: {len(spec.gazetteer)}"
        by_slot = spec.gazetteer_by_slot
        assert len(by_slot.get("medication", ())) >= 150, (
            f"medication count too low: {len(by_slot.get('medication', ()))}"
        )
        assert len(by_slot.get("procedure", ())) >= 75
        assert len(by_slot.get("symptom", ())) >= 75
        assert len(by_slot.get("severity", ())) >= 10
        assert len(by_slot.get("frequency", ())) >= 10

    def test_diversity_partitions_by_slot(self, spec):
        """Diversity design principle: one node per slot for breadth,
        specialty context moves to archetypes."""
        slots_covered = set()
        for n in spec.diversity.nodes:
            assert n.source == "gazetteer"
            # Each node should filter on a single slot (cleanest design).
            assert len(n.filter_slots) == 1, (
                f"node {n.qualified_name!r} filters multiple slots "
                f"{n.filter_slots} — specialty partitioning belongs in "
                "archetypes, not diversity nodes"
            )
            slots_covered.add(n.filter_slots[0])
        # All six clinical slots represented.
        assert slots_covered == {
            "medication",
            "procedure",
            "symptom",
            "severity",
            "frequency",
            "alternative",
        }

    def test_medication_pool_is_large(self, spec):
        """The medication diversity node should return the full pool;
        specialty filtering is an archetype concern, not a filter."""
        med_node = next(n for n in spec.diversity.nodes if n.filter_slots == ("medication",))
        pool = spec.diversity.resolve_examples(med_node, spec.gazetteer)
        assert len(pool) >= 150, f"medication pool too small: {len(pool)}"

    def test_common_medications_present(self, spec):
        """Spot check — sentinel entries should be in the gazetteer."""
        canonicals = {e.canonical for e in spec.gazetteer}
        # A mix of acute / chronic / common meds.
        for sentinel in [
            "metformin",
            "atorvastatin",
            "lisinopril",
            "sertraline",
            "amoxicillin",
            "ibuprofen",
            "levothyroxine",
            "albuterol",
            "omeprazole",
        ]:
            assert sentinel in canonicals, (
                f"expected common medication {sentinel!r} missing from gazetteer"
            )

    def test_common_procedures_present(self, spec):
        canonicals = {e.canonical for e in spec.gazetteer}
        for sentinel in [
            "ekg",
            "colonoscopy",
            "mammogram",
            "mri brain",
            "chest x-ray",
            "physical therapy",
        ]:
            assert sentinel in canonicals, f"expected procedure {sentinel!r} missing"

    def test_common_symptoms_present(self, spec):
        canonicals = {e.canonical for e in spec.gazetteer}
        for sentinel in [
            "headache",
            "chest pain",
            "nausea",
            "cough",
            "shortness of breath",
            "dizziness",
        ]:
            assert sentinel in canonicals, f"expected symptom {sentinel!r} missing"

    def test_archetypes_starter_set_present(self, spec):
        names = {a.name for a in spec.archetypes}
        expected = {
            "positive_medication_start",
            "choice_medication_switch",
            "habitual_medication_regimen",
            "negative_medication_discontinuation",
            "neutral_procedure_outcome",
            "difficulty_symptom_persistence",
        }
        assert expected.issubset(names), f"missing starter archetypes: {expected - names}"

    def test_archetype_role_spans_reference_valid_slots(self, spec):
        slot_set = set(spec.slots)
        for a in spec.archetypes:
            for rs in a.role_spans:
                assert rs.slot in slot_set
