"""v9 domain plugin loader — YAML parsing + validation unit tests.

Builds synthetic domain directories in a tmp_path and exercises the
full ``load_domain`` → :class:`DomainSpec` pipeline including cross-
file consistency checks (archetype slot references, diversity node
topic/slot references, gazetteer-backed node filter validity).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ncms.application.adapters.domain_loader import (
    DiversityTaxonomy,
    DomainSpec,
    DomainValidationError,
    load_all_domains,
    load_domain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _valid_software_dev_fixture(root: Path) -> Path:
    """A minimal but fully-valid software_dev-flavoured domain."""
    d = root / "software_dev"
    d.mkdir(parents=True)

    _write(
        d / "domain.yaml",
        {
            "name": "software_dev",
            "description": "test domain",
            "intended_content": "whatever",
            "slots": ["framework", "library", "tool", "alternative"],
            "topics": ["framework", "infra", "tooling"],
        },
    )
    _write(
        d / "gazetteer.yaml",
        {
            "entries": [
                {
                    "canonical": "fastapi",
                    "slot": "framework",
                    "topic": "framework",
                    "aliases": [],
                    "source": "wd:1",
                },
                {
                    "canonical": "django",
                    "slot": "framework",
                    "topic": "framework",
                    "aliases": [],
                    "source": "wd:2",
                },
                {
                    "canonical": "pytest",
                    "slot": "tool",
                    "topic": "tooling",
                    "aliases": ["py.test"],
                    "source": "wd:3",
                },
            ],
        },
    )
    _write(
        d / "diversity.yaml",
        {
            "frameworks_and_libs": {
                "description": "web frameworks",
                "topic_hint": "framework",
                "source": "gazetteer",
                "filter_slots": ["framework", "library"],
            },
            "tools": {
                "topic_hint": "tooling",
                "source": "gazetteer",
                "filter_slots": ["tool"],
            },
        },
    )
    _write(
        d / "archetypes.yaml",
        {
            "archetypes": [
                {
                    "name": "positive_framework_adoption",
                    "intent": "positive",
                    "admission": "persist",
                    "state_change": "declaration",
                    "role_spans": [
                        {"role": "primary", "slot": "framework", "count": 1},
                    ],
                    "n_gold": 30,
                    "n_sdg": 100,
                    "description": "User adopted a new framework.",
                    "example_utterances": [
                        "We switched to FastAPI last sprint.",
                    ],
                },
                {
                    "name": "neutral_tool_note",
                    "intent": "none",
                    "admission": "persist",
                    "state_change": "none",
                    "role_spans": [
                        {"role": "casual", "slot": "tool", "count": 1},
                    ],
                    "n_gold": 20,
                    "n_sdg": 80,
                    "description": "Passing mention of a tool.",
                },
            ],
        },
    )
    return d


def _valid_conversational_fixture(root: Path) -> Path:
    """Open-vocab domain: no gazetteer, inline diversity only."""
    d = root / "conversational"
    d.mkdir(parents=True)
    _write(
        d / "domain.yaml",
        {
            "name": "conversational",
            "slots": ["object", "alternative", "frequency"],
            "topics": ["food_pref", "activity_pref", "other"],
        },
    )
    _write(
        d / "diversity.yaml",
        {
            "foods": {
                "cuisines": {
                    "topic_hint": "food_pref",
                    "source": "inline",
                    "examples": ["Italian", "Japanese", "Thai"],
                },
            },
            "activities": {
                "sports": {
                    "topic_hint": "activity_pref",
                    "source": "inline",
                    "examples": ["bouldering", "pickleball", "cycling"],
                },
            },
        },
    )
    _write(
        d / "archetypes.yaml",
        {
            "archetypes": [
                {
                    "name": "positive_food_adoption",
                    "intent": "positive",
                    "admission": "persist",
                    "state_change": "declaration",
                    "role_spans": [
                        {"role": "primary", "slot": "object", "count": 1},
                    ],
                    "n_gold": 30,
                    "n_sdg": 100,
                    "description": "User declares they adopted a new food.",
                    "example_utterances": [
                        "I've really started loving pho lately.",
                    ],
                },
            ],
        },
    )
    return d


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadDomainHappyPath:
    def test_gazetteer_backed_domain_loads(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        spec = load_domain(d)
        assert isinstance(spec, DomainSpec)
        assert spec.name == "software_dev"
        assert spec.slots == ("framework", "library", "tool", "alternative")
        assert spec.topics == ("framework", "infra", "tooling")
        assert len(spec.gazetteer) == 3
        assert spec.has_gazetteer is True
        assert {e.canonical for e in spec.gazetteer} == {
            "fastapi",
            "django",
            "pytest",
        }
        assert isinstance(spec.diversity, DiversityTaxonomy)
        assert len(spec.diversity.nodes) == 2
        assert len(spec.archetypes) == 2

    def test_gazetteer_by_slot_groups_correctly(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        spec = load_domain(d)
        by_slot = spec.gazetteer_by_slot
        assert set(by_slot) == {"framework", "tool"}
        assert len(by_slot["framework"]) == 2
        assert len(by_slot["tool"]) == 1

    def test_inline_diversity_domain_loads(self, tmp_path: Path):
        d = _valid_conversational_fixture(tmp_path)
        spec = load_domain(d)
        assert spec.has_gazetteer is False
        assert spec.gazetteer == ()
        assert len(spec.diversity.nodes) == 2
        # Nodes are flattened to leaves; inline ones have examples.
        inline_nodes = [n for n in spec.diversity.nodes if n.source == "inline"]
        assert len(inline_nodes) == 2

    def test_resolve_examples_gazetteer(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        spec = load_domain(d)
        framework_node = next(
            n for n in spec.diversity.nodes if n.qualified_name == "frameworks_and_libs"
        )
        examples = spec.diversity.resolve_examples(
            framework_node,
            spec.gazetteer,
        )
        assert "fastapi" in examples
        assert "django" in examples
        assert "pytest" not in examples  # wrong slot

    def test_resolve_examples_inline(self, tmp_path: Path):
        d = _valid_conversational_fixture(tmp_path)
        spec = load_domain(d)
        foods_node = next(n for n in spec.diversity.nodes if n.qualified_name == "foods.cuisines")
        examples = spec.diversity.resolve_examples(
            foods_node,
            spec.gazetteer,
        )
        assert examples == ("Italian", "Japanese", "Thai")

    def test_default_paths_are_domain_scoped(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        spec = load_domain(d)
        # Paths contain the domain name somewhere.
        assert "software_dev" in str(spec.gold_jsonl_path)
        assert "software_dev" in str(spec.adapter_output_root)

    def test_default_version_is_v9(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        spec = load_domain(d)
        assert spec.default_adapter_version == "v9"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_directory_raises(self, tmp_path: Path):
        with pytest.raises(DomainValidationError, match="not found"):
            load_domain(tmp_path / "nonexistent")

    def test_name_mismatch_with_directory(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        # Rewrite domain.yaml with a different name.
        manifest = yaml.safe_load((d / "domain.yaml").read_text())
        manifest["name"] = "not_matching"
        (d / "domain.yaml").write_text(yaml.safe_dump(manifest))
        with pytest.raises(DomainValidationError, match="!= directory name"):
            load_domain(d)

    def test_gazetteer_entry_with_unknown_slot(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        manifest = yaml.safe_load((d / "gazetteer.yaml").read_text())
        manifest["entries"][0]["slot"] = "not_a_real_slot"
        (d / "gazetteer.yaml").write_text(yaml.safe_dump(manifest))
        with pytest.raises(DomainValidationError, match="not in domain.slots"):
            load_domain(d)

    def test_gazetteer_duplicate_canonical_raises(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        manifest = yaml.safe_load((d / "gazetteer.yaml").read_text())
        manifest["entries"][0]["canonical"] = "duplicate"
        manifest["entries"][1]["canonical"] = "duplicate"
        (d / "gazetteer.yaml").write_text(yaml.safe_dump(manifest))
        with pytest.raises(DomainValidationError, match="duplicate canonical"):
            load_domain(d)

    def test_gazetteer_entry_with_unknown_topic_raises(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        manifest = yaml.safe_load((d / "gazetteer.yaml").read_text())
        manifest["entries"][0]["topic"] = "nonexistent_topic"
        (d / "gazetteer.yaml").write_text(yaml.safe_dump(manifest))
        with pytest.raises(DomainValidationError, match="not in domain.topics"):
            load_domain(d)

    def test_diversity_node_references_unknown_topic(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        div = yaml.safe_load((d / "diversity.yaml").read_text())
        div["frameworks_and_libs"]["topic_hint"] = "phantom"
        (d / "diversity.yaml").write_text(yaml.safe_dump(div))
        with pytest.raises(DomainValidationError, match="not in domain.topics"):
            load_domain(d)

    def test_diversity_gazetteer_node_with_unknown_slot(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        div = yaml.safe_load((d / "diversity.yaml").read_text())
        div["frameworks_and_libs"]["filter_slots"] = ["no_such_slot"]
        (d / "diversity.yaml").write_text(yaml.safe_dump(div))
        with pytest.raises(DomainValidationError, match="not in domain.slots"):
            load_domain(d)

    def test_diversity_gazetteer_node_with_no_matching_entries(
        self,
        tmp_path: Path,
    ):
        """filter_slots references a real slot but no entries match."""
        d = _valid_software_dev_fixture(tmp_path)
        div = yaml.safe_load((d / "diversity.yaml").read_text())
        # All gazetteer entries are in framework/tool — library slot exists
        # in domain.yaml but no entries use it.
        div["frameworks_and_libs"]["filter_slots"] = ["library"]
        (d / "diversity.yaml").write_text(yaml.safe_dump(div))
        with pytest.raises(
            DomainValidationError,
            match="no gazetteer entries match",
        ):
            load_domain(d)

    def test_archetype_with_unknown_intent(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        archs = yaml.safe_load((d / "archetypes.yaml").read_text())
        archs["archetypes"][0]["intent"] = "bogus"
        (d / "archetypes.yaml").write_text(yaml.safe_dump(archs))
        with pytest.raises(DomainValidationError, match="unknown intent"):
            load_domain(d)

    def test_archetype_with_unknown_slot_in_role_span(
        self,
        tmp_path: Path,
    ):
        d = _valid_software_dev_fixture(tmp_path)
        archs = yaml.safe_load((d / "archetypes.yaml").read_text())
        archs["archetypes"][0]["role_spans"][0]["slot"] = "nonexistent"
        (d / "archetypes.yaml").write_text(yaml.safe_dump(archs))
        with pytest.raises(DomainValidationError, match="not in domain.slots"):
            load_domain(d)

    def test_archetype_slot_has_no_entity_source(self, tmp_path: Path):
        """Archetype wants role=primary slot=library; no gazetteer + no inline diversity."""
        d = _valid_software_dev_fixture(tmp_path)
        # Change archetype to need a library-slot entity (gazetteer has
        # none).  No inline diversity nodes exist, so generator can't
        # source the entity.
        archs = yaml.safe_load((d / "archetypes.yaml").read_text())
        archs["archetypes"][0]["role_spans"][0]["slot"] = "library"
        (d / "archetypes.yaml").write_text(yaml.safe_dump(archs))
        # Also need to clear the library-referencing diversity node so
        # validation hits archetype check, not diversity check.
        div = yaml.safe_load((d / "diversity.yaml").read_text())
        div["frameworks_and_libs"]["filter_slots"] = ["framework"]
        (d / "diversity.yaml").write_text(yaml.safe_dump(div))
        with pytest.raises(
            DomainValidationError,
            match="no entity source",
        ):
            load_domain(d)

    def test_duplicate_archetype_name_raises(self, tmp_path: Path):
        d = _valid_software_dev_fixture(tmp_path)
        archs = yaml.safe_load((d / "archetypes.yaml").read_text())
        archs["archetypes"][1]["name"] = archs["archetypes"][0]["name"]
        (d / "archetypes.yaml").write_text(yaml.safe_dump(archs))
        with pytest.raises(
            DomainValidationError,
            match="duplicate archetype",
        ):
            load_domain(d)


# ---------------------------------------------------------------------------
# load_all_domains
# ---------------------------------------------------------------------------


class TestLoadAllDomains:
    def test_loads_multiple(self, tmp_path: Path):
        _valid_software_dev_fixture(tmp_path)
        _valid_conversational_fixture(tmp_path)
        specs = load_all_domains(tmp_path)
        assert set(specs) == {"software_dev", "conversational"}

    def test_skips_directories_without_domain_yaml(self, tmp_path: Path):
        _valid_software_dev_fixture(tmp_path)
        (tmp_path / "not_a_domain").mkdir()
        specs = load_all_domains(tmp_path)
        assert set(specs) == {"software_dev"}

    def test_empty_root_returns_empty_dict(self, tmp_path: Path):
        specs = load_all_domains(tmp_path)
        assert specs == {}

    def test_missing_root_raises(self, tmp_path: Path):
        with pytest.raises(
            DomainValidationError,
            match="domains root",
        ):
            load_all_domains(tmp_path / "does_not_exist")
