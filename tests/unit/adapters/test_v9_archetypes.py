"""v9 ArchetypeSpec + coverage-validator unit tests.

Exercises the schema invariants + the registry-level class-floor
check that must pass before Phase B'.2 generation runs.
"""

from __future__ import annotations

import pytest

from ncms.application.adapters.sdg.v9.archetypes import (
    ArchetypeSpec,
    CoverageGap,
    RoleSpec,
    validate_archetype_coverage,
)


def _make_arch(**overrides) -> ArchetypeSpec:
    """Helper to build a minimal valid archetype, overriding any field."""
    defaults = {
        "name": "test_arch",
        "domain": "software_dev",
        "intent": "positive",
        "admission": "persist",
        "state_change": "none",
        "description": "test archetype",
        "role_spans": (RoleSpec("primary", "framework", 1),),
    }
    defaults.update(overrides)
    return ArchetypeSpec(**defaults)


class TestRoleSpec:
    def test_valid_construction(self):
        rs = RoleSpec(role="primary", slot="framework", count=1)
        assert rs.role == "primary"
        assert rs.slot == "framework"
        assert rs.count == 1

    def test_rejects_unknown_role(self):
        with pytest.raises(ValueError, match="not in"):
            RoleSpec(role="bogus", slot="framework")  # type: ignore[arg-type]

    def test_rejects_negative_count(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            RoleSpec(role="primary", slot="framework", count=-1)


class TestArchetypeSpec:
    def test_valid_construction(self):
        a = _make_arch()
        assert a.name == "test_arch"
        assert a.intent == "positive"
        assert a.total_role_count == 1

    def test_rejects_unknown_intent(self):
        with pytest.raises(ValueError, match="unknown intent"):
            _make_arch(intent="bogus")

    def test_rejects_unknown_admission(self):
        with pytest.raises(ValueError, match="unknown admission"):
            _make_arch(admission="bogus")

    def test_rejects_unknown_state_change(self):
        with pytest.raises(ValueError, match="unknown state_change"):
            _make_arch(state_change="bogus")

    def test_rejects_empty_description(self):
        with pytest.raises(ValueError, match="empty description"):
            _make_arch(description="")

    def test_rejects_inverted_length_range(self):
        with pytest.raises(ValueError, match="target_max_chars"):
            _make_arch(target_min_chars=100, target_max_chars=50)

    def test_rejects_nonpositive_batch(self):
        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            _make_arch(batch_size=0)

    def test_rejects_negative_target(self):
        with pytest.raises(ValueError, match="negative target"):
            _make_arch(n_gold=-5)

    def test_total_role_count_sums_counts(self):
        a = _make_arch(
            role_spans=(
                RoleSpec("primary", "framework", 1),
                RoleSpec("alternative", "framework", 2),
                RoleSpec("casual", "library", 3),
            )
        )
        assert a.total_role_count == 6

    def test_role_spec_views(self):
        a = _make_arch(
            role_spans=(
                RoleSpec("primary", "framework", 1),
                RoleSpec("alternative", "framework", 1),
                RoleSpec("alternative", "library", 1),
                RoleSpec("casual", "tool", 1),
            )
        )
        assert len(a.primary_role_specs) == 1
        assert len(a.alternative_role_specs) == 2


class TestCoverage:
    def test_empty_registry_returns_all_gaps(self):
        gaps = validate_archetype_coverage(
            [],
            intent_floor=10,
            admission_floor=10,
            state_change_floor=10,
            role_floor=10,
        )
        # Every intent class + admission class + state_change class
        # + role class should be reported as a gap.
        heads = {g.head for g in gaps}
        assert heads == {"intent", "admission", "state_change", "role"}

    def test_balanced_registry_has_no_gaps(self):
        """One archetype per intent + admission combo = full coverage."""
        from ncms.application.adapters.schemas import (
            ADMISSION_DECISIONS,
            INTENT_CATEGORIES,
            ROLE_LABELS,
            STATE_CHANGES,
        )

        archs = []
        for i, intent in enumerate(INTENT_CATEGORIES):
            for j, admission in enumerate(ADMISSION_DECISIONS):
                for k, state in enumerate(STATE_CHANGES):
                    # Rotate roles across archetypes so every role
                    # accumulates plenty of spans.
                    role = ROLE_LABELS[(i + j + k) % len(ROLE_LABELS)]
                    archs.append(
                        _make_arch(
                            name=f"a{i}_{j}_{k}",
                            intent=intent,
                            admission=admission,
                            state_change=state,
                            role_spans=(RoleSpec(role, "framework", 1),),
                            n_gold=50,
                        )
                    )
        gaps = validate_archetype_coverage(
            archs,
            split="gold",
            intent_floor=50,
            admission_floor=50,
            state_change_floor=50,
            role_floor=50,
        )
        assert gaps == []

    def test_under_floor_reports_gap(self):
        archs = [
            _make_arch(
                intent="positive",
                admission="persist",
                state_change="none",
                n_gold=20,  # < 50 floor
                role_spans=(RoleSpec("primary", "framework", 1),),
            )
        ]
        gaps = validate_archetype_coverage(
            archs,
            split="gold",
            intent_floor=50,
            admission_floor=50,
            state_change_floor=50,
            role_floor=50,
        )
        # Should report intent=positive (only 20 rows), and every
        # other class of every head (zero rows).  Exactly one gap
        # for intent=positive at 20/50.
        positive_gaps = [g for g in gaps if g.head == "intent" and g.cls == "positive"]
        assert len(positive_gaps) == 1
        assert positive_gaps[0].found == 20
        assert positive_gaps[0].floor == 50

    def test_rejects_unknown_split(self):
        with pytest.raises(ValueError, match="unknown split"):
            validate_archetype_coverage([], split="adversarial")  # type: ignore[arg-type]

    def test_coverage_gap_str(self):
        g = CoverageGap(head="intent", cls="difficulty", found=12, floor=50)
        s = str(g)
        assert "intent" in s
        assert "difficulty" in s
        assert "12" in s
        assert "50" in s
