"""v9 domain plugin loader.

A domain is one directory::

    adapters/domains/<name>/
      domain.yaml         # required — slots, topics, metadata, file refs
      gazetteer.yaml      # optional — inference-time lookup catalog
      diversity.yaml      # required — generator-time type taxonomy
      archetypes.yaml     # required — stratified generation archetypes

This module parses that directory into a validated :class:`DomainSpec`.
All downstream v9 code (generator, coverage audit, trainer) reads
through :class:`DomainSpec` rather than hardcoded per-domain
branches.

Design: ``docs/research/v9-domain-plugin-architecture.md``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover — yaml is a direct dep
    raise RuntimeError(
        "domain_loader requires PyYAML (`uv sync`)",
    ) from exc

from ncms.application.adapters.schemas import (
    ADMISSION_DECISIONS,
    INTENT_CATEGORIES,
    ROLE_LABELS,
    STATE_CHANGES,
)
from ncms.application.adapters.sdg.catalog.primitives import CatalogEntry
from ncms.application.adapters.sdg.v9.archetypes import (
    ArchetypeSpec,
    RoleSpec,
)


class DomainValidationError(ValueError):
    """Raised when a domain directory fails schema validation."""


# ---------------------------------------------------------------------------
# Diversity taxonomy primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiversityNode:
    """One leaf node of a domain's diversity taxonomy.

    A diversity node produces entities the generator rotates through
    when building training rows.  Two sourcing modes:

    * ``source == "inline"`` — examples listed directly in the YAML.
      Used for open-vocabulary domains (conversational) where a
      closed catalog would mis-represent the space.
    * ``source == "gazetteer"`` — examples pulled from the domain's
      gazetteer, filtered by slot.  Used for catalog-backed domains
      (software_dev, clinical).

    The node's ``topic_hint`` is the topic-head label the generator
    attaches to rows produced from this node.  Must be one of the
    domain's declared topics.
    """

    path: tuple[str, ...]               # ("foods", "cuisines")
    description: str
    topic_hint: str
    source: str                         # "inline" | "gazetteer"
    examples: tuple[str, ...] = ()      # populated when source=inline
    filter_slots: tuple[str, ...] = ()  # populated when source=gazetteer
    n_examples_per_batch: int = 8

    @property
    def qualified_name(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class DiversityTaxonomy:
    """Hierarchical diversity taxonomy for one domain.

    Leaf nodes are flattened into :attr:`nodes` for uniform iteration
    by the generator; the original hierarchy is preserved for
    presentation via :meth:`top_level_groups`.
    """

    nodes: tuple[DiversityNode, ...]

    def top_level_groups(self) -> dict[str, list[DiversityNode]]:
        """Group nodes by their top-level taxonomy key.

        Useful for coverage reports: "did we sample from every food
        subcategory?"
        """
        groups: dict[str, list[DiversityNode]] = {}
        for node in self.nodes:
            top = node.path[0]
            groups.setdefault(top, []).append(node)
        return groups

    def resolve_examples(
        self,
        node: DiversityNode,
        gazetteer: tuple[CatalogEntry, ...],
    ) -> tuple[str, ...]:
        """Return the example pool the generator should sample from.

        For inline nodes, returns the YAML-declared examples verbatim.
        For gazetteer-backed nodes, filters the domain's gazetteer by
        the node's ``filter_slots`` and returns the canonical forms.
        """
        if node.source == "inline":
            return node.examples
        if node.source == "gazetteer":
            return tuple(
                e.canonical for e in gazetteer
                if e.slot in node.filter_slots
            )
        raise DomainValidationError(
            f"diversity node {node.qualified_name!r}: "
            f"unknown source {node.source!r}",
        )


# ---------------------------------------------------------------------------
# Domain spec (the loaded, validated whole-domain artifact)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainSpec:
    """Validated v9 domain specification loaded from a directory.

    Every downstream component that needs per-domain configuration
    reads through this dataclass:

    * generator — :attr:`archetypes` + :attr:`diversity` + :attr:`gazetteer`
    * inference — :attr:`gazetteer`
    * trainer — :attr:`gold_jsonl_path`, :attr:`sdg_jsonl_path`,
      :attr:`adversarial_jsonl_path`
    * coverage audit — :attr:`gazetteer` (catalog-backed domains),
      :attr:`diversity` (all domains), benchmark corpora (registered
      elsewhere)
    """

    name: str                                 # e.g. "software_dev"
    description: str
    intended_content: str

    slots: tuple[str, ...]                    # domain's slot taxonomy
    topics: tuple[str, ...]                   # topic head vocabulary

    gazetteer: tuple[CatalogEntry, ...]       # empty when no gazetteer
    diversity: DiversityTaxonomy
    archetypes: tuple[ArchetypeSpec, ...]

    # Training paths — filled from the domain YAML or defaulted by
    # ``load_domain`` when absent.
    gold_jsonl_path: Path
    sdg_jsonl_path: Path
    adversarial_jsonl_path: Path

    adapter_output_root: Path
    deployed_adapter_root: Path
    default_adapter_version: str

    # Preserved for audit: where this spec was loaded from.
    source_dir: Path

    @property
    def has_gazetteer(self) -> bool:
        return bool(self.gazetteer)

    @property
    def gazetteer_by_slot(self) -> dict[str, tuple[CatalogEntry, ...]]:
        out: dict[str, list[CatalogEntry]] = {}
        for e in self.gazetteer:
            out.setdefault(e.slot, []).append(e)
        return {slot: tuple(entries) for slot, entries in out.items()}


# ---------------------------------------------------------------------------
# YAML parsing (internal helpers)
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise DomainValidationError(f"expected YAML file at {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DomainValidationError(
            f"invalid YAML at {path}: {exc}",
        ) from exc


def _require_str(data: Any, field_name: str, path: Path) -> str:
    val = data.get(field_name) if isinstance(data, dict) else None
    if not isinstance(val, str) or not val.strip():
        raise DomainValidationError(
            f"{path}: missing/empty required string field {field_name!r}",
        )
    return val


def _opt_str(data: dict, field_name: str, default: str = "") -> str:
    val = data.get(field_name)
    return val if isinstance(val, str) else default


def _require_list_of_str(
    data: Any, field_name: str, path: Path,
) -> tuple[str, ...]:
    val = data.get(field_name) if isinstance(data, dict) else None
    if not isinstance(val, list) or not val:
        raise DomainValidationError(
            f"{path}: {field_name!r} must be a non-empty list of strings",
        )
    for item in val:
        if not isinstance(item, str) or not item.strip():
            raise DomainValidationError(
                f"{path}: {field_name!r} entries must be non-empty strings "
                f"(got {item!r})",
            )
    return tuple(val)


# ---------------------------------------------------------------------------
# Gazetteer loader
# ---------------------------------------------------------------------------


def _load_gazetteer(
    path: Path, allowed_slots: tuple[str, ...],
) -> tuple[CatalogEntry, ...]:
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise DomainValidationError(
            f"{path}: gazetteer root must be a mapping",
        )
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise DomainValidationError(
            f"{path}: 'entries' must be a list",
        )
    allowed_slot_set = set(allowed_slots)
    seen_canonicals: set[str] = set()
    entries: list[CatalogEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise DomainValidationError(
                f"{path}: entries[{i}] must be a mapping, got {type(raw).__name__}",
            )
        canonical = _require_str(raw, "canonical", path)
        slot = _require_str(raw, "slot", path)
        topic = _require_str(raw, "topic", path)
        if slot not in allowed_slot_set:
            raise DomainValidationError(
                f"{path}: entry[{i}] {canonical!r} references slot "
                f"{slot!r} not in domain.slots {sorted(allowed_slot_set)}",
            )
        if canonical in seen_canonicals:
            raise DomainValidationError(
                f"{path}: duplicate canonical {canonical!r}",
            )
        seen_canonicals.add(canonical)
        aliases_raw = raw.get("aliases") or []
        if not isinstance(aliases_raw, list):
            raise DomainValidationError(
                f"{path}: entry[{i}] {canonical!r}: "
                f"aliases must be a list",
            )
        aliases: tuple[str, ...] = tuple(
            str(a).strip() for a in aliases_raw if str(a).strip()
        )
        entries.append(CatalogEntry(
            canonical=canonical,
            slot=slot,
            topic=topic,
            aliases=aliases,
            source=_opt_str(raw, "source"),
            notes=_opt_str(raw, "notes"),
        ))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Diversity taxonomy loader
# ---------------------------------------------------------------------------


def _walk_diversity(
    node: Any,
    *,
    path: tuple[str, ...],
    allowed_topics: set[str],
    allowed_slots: set[str],
    yaml_path: Path,
    out: list[DiversityNode],
) -> None:
    """Recursively walk the diversity YAML, emitting leaf nodes.

    A leaf is a mapping carrying a ``source`` key.  Intermediate
    nodes are mappings whose children are further mappings without a
    ``source`` key — they act purely as grouping.
    """
    if not isinstance(node, dict):
        raise DomainValidationError(
            f"{yaml_path}: node at {'.'.join(path) or '<root>'} "
            "must be a mapping",
        )
    if "source" in node:
        # Leaf node.
        source = node["source"]
        if source not in ("inline", "gazetteer"):
            raise DomainValidationError(
                f"{yaml_path}: {'.'.join(path)} has source={source!r}; "
                "must be 'inline' or 'gazetteer'",
            )
        topic_hint = _require_str(node, "topic_hint", yaml_path)
        if topic_hint not in allowed_topics:
            raise DomainValidationError(
                f"{yaml_path}: {'.'.join(path)} topic_hint "
                f"{topic_hint!r} not in domain.topics "
                f"{sorted(allowed_topics)}",
            )
        description = _opt_str(node, "description")
        n_batch = node.get("n_examples_per_batch", 8)
        if not isinstance(n_batch, int) or n_batch < 1:
            raise DomainValidationError(
                f"{yaml_path}: {'.'.join(path)} "
                f"n_examples_per_batch must be a positive int",
            )
        if source == "inline":
            examples_raw = node.get("examples")
            if not isinstance(examples_raw, list) or not examples_raw:
                raise DomainValidationError(
                    f"{yaml_path}: {'.'.join(path)} "
                    "source=inline requires non-empty 'examples' list",
                )
            examples = tuple(
                str(x).strip() for x in examples_raw
                if isinstance(x, (str, int, float)) and str(x).strip()
            )
            out.append(DiversityNode(
                path=path,
                description=description,
                topic_hint=topic_hint,
                source="inline",
                examples=examples,
                n_examples_per_batch=n_batch,
            ))
            return
        # source == "gazetteer"
        fs_raw = node.get("filter_slots") or []
        if not isinstance(fs_raw, list) or not fs_raw:
            raise DomainValidationError(
                f"{yaml_path}: {'.'.join(path)} "
                "source=gazetteer requires non-empty 'filter_slots'",
            )
        for s in fs_raw:
            if not isinstance(s, str) or s not in allowed_slots:
                raise DomainValidationError(
                    f"{yaml_path}: {'.'.join(path)} filter_slots "
                    f"{s!r} not in domain.slots {sorted(allowed_slots)}",
                )
        out.append(DiversityNode(
            path=path,
            description=description,
            topic_hint=topic_hint,
            source="gazetteer",
            filter_slots=tuple(fs_raw),
            n_examples_per_batch=n_batch,
        ))
        return
    # Non-leaf: recurse.
    for key, child in node.items():
        if not isinstance(key, str):
            raise DomainValidationError(
                f"{yaml_path}: non-string key {key!r} at "
                f"{'.'.join(path) or '<root>'}",
            )
        _walk_diversity(
            child, path=path + (key,),
            allowed_topics=allowed_topics,
            allowed_slots=allowed_slots,
            yaml_path=yaml_path, out=out,
        )


def _load_diversity(
    path: Path,
    allowed_topics: tuple[str, ...],
    allowed_slots: tuple[str, ...],
) -> DiversityTaxonomy:
    data = _read_yaml(path)
    out: list[DiversityNode] = []
    _walk_diversity(
        data, path=(),
        allowed_topics=set(allowed_topics),
        allowed_slots=set(allowed_slots),
        yaml_path=path, out=out,
    )
    if not out:
        raise DomainValidationError(
            f"{path}: produced zero leaf diversity nodes — need at "
            "least one source=inline or source=gazetteer entry",
        )
    return DiversityTaxonomy(nodes=tuple(out))


# ---------------------------------------------------------------------------
# Archetypes loader
# ---------------------------------------------------------------------------


def _load_archetypes(
    path: Path,
    *,
    domain: str,
    allowed_slots: tuple[str, ...],
    allowed_topics: tuple[str, ...],
) -> tuple[ArchetypeSpec, ...]:
    data = _read_yaml(path)
    raw_list = (data or {}).get("archetypes") if isinstance(data, dict) else None
    if not isinstance(raw_list, list) or not raw_list:
        raise DomainValidationError(
            f"{path}: 'archetypes' must be a non-empty list",
        )
    slot_set = set(allowed_slots)
    topic_set = set(allowed_topics)
    seen_names: set[str] = set()
    out: list[ArchetypeSpec] = []
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise DomainValidationError(
                f"{path}: archetypes[{i}] must be a mapping",
            )
        name = _require_str(raw, "name", path)
        if name in seen_names:
            raise DomainValidationError(
                f"{path}: duplicate archetype name {name!r}",
            )
        seen_names.add(name)
        intent = _require_str(raw, "intent", path)
        admission = _require_str(raw, "admission", path)
        state_change = _require_str(raw, "state_change", path)
        description = _require_str(raw, "description", path)

        topic = raw.get("topic")
        if topic is not None and (
            not isinstance(topic, str) or topic not in topic_set
        ):
            raise DomainValidationError(
                f"{path}: archetype {name!r} topic {topic!r} not in "
                f"domain.topics {sorted(topic_set)}",
            )

        role_specs_raw = raw.get("role_spans") or []
        if not isinstance(role_specs_raw, list):
            raise DomainValidationError(
                f"{path}: archetype {name!r}: role_spans must be a list",
            )
        role_specs: list[RoleSpec] = []
        for j, rs in enumerate(role_specs_raw):
            if not isinstance(rs, dict):
                raise DomainValidationError(
                    f"{path}: archetype {name!r} "
                    f"role_spans[{j}] must be a mapping",
                )
            role = _require_str(rs, "role", path)
            slot = _require_str(rs, "slot", path)
            count = rs.get("count", 1)
            if role not in ROLE_LABELS:
                raise DomainValidationError(
                    f"{path}: archetype {name!r}: unknown role {role!r}",
                )
            if slot not in slot_set:
                raise DomainValidationError(
                    f"{path}: archetype {name!r}: slot {slot!r} not "
                    f"in domain.slots {sorted(slot_set)}",
                )
            if not isinstance(count, int) or count < 0:
                raise DomainValidationError(
                    f"{path}: archetype {name!r}: "
                    f"role_spans[{j}].count must be a non-negative int",
                )
            role_specs.append(RoleSpec(role=role, slot=slot, count=count))  # type: ignore[arg-type]

        example_utterances = raw.get("example_utterances") or []
        if not isinstance(example_utterances, list):
            raise DomainValidationError(
                f"{path}: archetype {name!r}: example_utterances must be a list",
            )
        example_utterances_t = tuple(
            str(x) for x in example_utterances if isinstance(x, str)
        )
        phrasings_raw = raw.get("phrasings") or []
        if not isinstance(phrasings_raw, list):
            raise DomainValidationError(
                f"{path}: archetype {name!r}: phrasings must be a list",
            )
        phrasings = tuple(
            str(x) for x in phrasings_raw if isinstance(x, str)
        )
        # Allow phrasings_path override for external files:
        phrasings_path = raw.get("phrasings_path")
        if isinstance(phrasings_path, str) and phrasings_path.strip():
            ppath = path.parent / phrasings_path
            if ppath.is_file():
                lines = tuple(
                    line.strip()
                    for line in ppath.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
                phrasings = phrasings + lines

        try:
            spec = ArchetypeSpec(
                name=name,
                domain=domain,  # type: ignore[arg-type]
                intent=intent,  # type: ignore[arg-type]
                admission=admission,  # type: ignore[arg-type]
                state_change=state_change,  # type: ignore[arg-type]
                topic=topic,
                role_spans=tuple(role_specs),
                n_gold=int(raw.get("n_gold", 30)),
                n_sdg=int(raw.get("n_sdg", 150)),
                target_min_chars=int(raw.get("target_min_chars", 20)),
                target_max_chars=int(raw.get("target_max_chars", 200)),
                batch_size=int(raw.get("batch_size", 10)),
                description=description,
                example_utterances=example_utterances_t,
                phrasings=phrasings,
            )
        except ValueError as exc:
            raise DomainValidationError(
                f"{path}: archetype {name!r}: {exc}",
            ) from exc
        out.append(spec)
    return tuple(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_domain(
    domain_dir: Path,
    *,
    corpora_root: Path | None = None,
    adapter_checkpoint_root: Path | None = None,
    adapter_deployed_root: Path | None = None,
) -> DomainSpec:
    """Parse + validate one domain directory into a :class:`DomainSpec`.

    Expected layout::

        domain_dir/
          domain.yaml       # required
          gazetteer.yaml    # optional
          diversity.yaml    # required (referenced from domain.yaml)
          archetypes.yaml   # required (referenced from domain.yaml)

    ``corpora_root`` / ``adapter_checkpoint_root`` / ``adapter_deployed_root``
    are used to fill in default paths when the domain.yaml doesn't
    declare them explicitly.  Defaults mirror the pre-v9 layout:

    * corpora:   ``adapters/corpora/v9/<name>/{gold,sdg,adv}.jsonl``
    * checkpoint: ``adapters/checkpoints/<name>/``
    * deployed:   ``~/.ncms/adapters/<name>/``
    """
    domain_dir = Path(domain_dir).resolve()
    if not domain_dir.is_dir():
        raise DomainValidationError(f"domain directory not found: {domain_dir}")

    # ── domain.yaml ───────────────────────────────────────────────
    manifest_path = domain_dir / "domain.yaml"
    manifest = _read_yaml(manifest_path)
    if not isinstance(manifest, dict):
        raise DomainValidationError(
            f"{manifest_path}: root must be a mapping",
        )
    name = _require_str(manifest, "name", manifest_path)
    if name != domain_dir.name:
        raise DomainValidationError(
            f"{manifest_path}: name={name!r} != directory name "
            f"{domain_dir.name!r}",
        )
    description = _opt_str(manifest, "description")
    intended_content = _opt_str(manifest, "intended_content")
    slots = _require_list_of_str(manifest, "slots", manifest_path)
    topics = _require_list_of_str(manifest, "topics", manifest_path)

    # ── gazetteer (optional) ──────────────────────────────────────
    gaz_path_name = _opt_str(manifest, "gazetteer_path", "gazetteer.yaml")
    gaz_path = domain_dir / gaz_path_name
    if gaz_path.is_file():
        gazetteer = _load_gazetteer(gaz_path, slots)
    else:
        gazetteer = ()

    # Every gazetteer entry's topic should appear in the topic vocab
    # — check now so the error references the gazetteer location.
    topic_set = set(topics)
    for e in gazetteer:
        if e.topic not in topic_set:
            raise DomainValidationError(
                f"{gaz_path}: entry {e.canonical!r} topic {e.topic!r} "
                f"not in domain.topics {sorted(topic_set)}",
            )

    # ── diversity (required) ──────────────────────────────────────
    div_path_name = _opt_str(manifest, "diversity_path", "diversity.yaml")
    div_path = domain_dir / div_path_name
    diversity = _load_diversity(div_path, topics, slots)

    # Every gazetteer-backed diversity node must have at least one
    # catalog entry that matches its filter_slots — otherwise its
    # example pool is empty and generation for that node will stall.
    for node in diversity.nodes:
        if node.source != "gazetteer":
            continue
        matching = [
            e for e in gazetteer if e.slot in node.filter_slots
        ]
        if not matching:
            raise DomainValidationError(
                f"{div_path}: diversity node {node.qualified_name!r} "
                f"filters gazetteer slots {list(node.filter_slots)} "
                "but no gazetteer entries match (domain has no "
                "gazetteer, or no entries in those slots)",
            )

    # ── archetypes (required) ─────────────────────────────────────
    arch_path_name = _opt_str(
        manifest, "archetypes_path", "archetypes.yaml",
    )
    arch_path = domain_dir / arch_path_name
    archetypes = _load_archetypes(
        arch_path, domain=name, allowed_slots=slots, allowed_topics=topics,
    )

    # ── Paths (with defaults) ─────────────────────────────────────
    _repo_root = _locate_repo_root(domain_dir)
    corpora_root = corpora_root or (_repo_root / "adapters/corpora/v9" / name)
    adapter_checkpoint_root = adapter_checkpoint_root or (
        _repo_root / "adapters/checkpoints" / name
    )
    adapter_deployed_root = adapter_deployed_root or (
        Path.home() / ".ncms/adapters" / name
    )

    paths_block = manifest.get("paths") or {}
    if not isinstance(paths_block, dict):
        raise DomainValidationError(
            f"{manifest_path}: 'paths' must be a mapping if present",
        )
    gold_path = _resolve_path(
        paths_block.get("gold_jsonl"),
        corpora_root / "gold.jsonl",
    )
    sdg_path = _resolve_path(
        paths_block.get("sdg_jsonl"),
        corpora_root / "sdg.jsonl",
    )
    adv_path = _resolve_path(
        paths_block.get("adversarial_jsonl"),
        corpora_root / "adv.jsonl",
    )

    adapter_block = manifest.get("adapter") or {}
    if not isinstance(adapter_block, dict):
        raise DomainValidationError(
            f"{manifest_path}: 'adapter' must be a mapping if present",
        )
    out_root = _resolve_path(
        adapter_block.get("output_root"), adapter_checkpoint_root,
    )
    deployed_root = _resolve_path(
        adapter_block.get("deployed_root"), adapter_deployed_root,
    )
    default_version = _opt_str(adapter_block, "default_version", "v9") or _opt_str(
        manifest, "default_adapter_version", "v9",
    )

    spec = DomainSpec(
        name=name,
        description=description,
        intended_content=intended_content,
        slots=slots,
        topics=topics,
        gazetteer=gazetteer,
        diversity=diversity,
        archetypes=archetypes,
        gold_jsonl_path=gold_path,
        sdg_jsonl_path=sdg_path,
        adversarial_jsonl_path=adv_path,
        adapter_output_root=out_root,
        deployed_adapter_root=deployed_root,
        default_adapter_version=default_version,
        source_dir=domain_dir,
    )

    # Final cross-file consistency: every archetype's role_spans
    # references a slot that has a matching gazetteer (for catalog-
    # backed domains) OR a matching inline diversity node (for
    # open-vocab domains).  When neither is present the generator
    # has no way to pick entities for that archetype.
    _validate_archetype_entity_sources(spec)

    return spec


def load_all_domains(
    domains_root: Path,
    *,
    corpora_root: Path | None = None,
    adapter_checkpoint_root: Path | None = None,
    adapter_deployed_root: Path | None = None,
) -> dict[str, DomainSpec]:
    """Load every subdirectory of ``domains_root`` that has a domain.yaml.

    Missing domain.yaml → directory skipped silently.  Any YAML error →
    ``DomainValidationError`` surfaces with the offending path.
    """
    domains_root = Path(domains_root).resolve()
    if not domains_root.is_dir():
        raise DomainValidationError(
            f"domains root directory not found: {domains_root}",
        )
    out: dict[str, DomainSpec] = {}
    for sub in sorted(domains_root.iterdir()):
        if not sub.is_dir():
            continue
        if not (sub / "domain.yaml").is_file():
            continue
        spec = load_domain(
            sub,
            corpora_root=corpora_root,
            adapter_checkpoint_root=adapter_checkpoint_root,
            adapter_deployed_root=adapter_deployed_root,
        )
        out[spec.name] = spec
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_path(raw: Any, default: Path) -> Path:
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise DomainValidationError(
            f"path override must be a string, got {type(raw).__name__}",
        )
    p = Path(raw).expanduser()
    return p


def _locate_repo_root(domain_dir: Path) -> Path:
    """Walk up from a domain directory to find the repo root.

    Heuristic: the first ancestor containing a ``pyproject.toml``.
    Falls back to ``domain_dir.parents[2]`` (the adapters/domains
    layout convention).
    """
    for p in domain_dir.parents:
        if (p / "pyproject.toml").is_file():
            return p
    # Fallback to the layout convention: adapters/domains/<name>/ → repo
    if len(domain_dir.parents) >= 3:
        return domain_dir.parents[2]
    return domain_dir.parent


def _validate_archetype_entity_sources(spec: DomainSpec) -> None:
    """Every archetype must have SOME way to source entities at gen time.

    For each archetype slot required by its ``role_spans``, the
    domain must offer at least one source:

    * a gazetteer entry with that slot, OR
    * an inline diversity node whose topic_hint isn't gazetteer-backed
      (we treat inline nodes as universal — they produce entities
      regardless of slot vocabulary)

    Without that, the generator would request a slot-bound entity
    it can't fulfill.
    """
    slots_with_gaz = {e.slot for e in spec.gazetteer}
    has_inline = any(
        n.source == "inline" for n in spec.diversity.nodes
    )
    gaps: list[str] = []
    for a in spec.archetypes:
        for rs in a.role_spans:
            if rs.count == 0:
                continue
            if rs.slot in slots_with_gaz:
                continue
            if has_inline:
                # Inline nodes aren't slot-bound — the generator can
                # pick examples from them regardless of archetype
                # slot.  Good enough for open-vocab domains.
                continue
            gaps.append(
                f"archetype {a.name!r} needs role={rs.role!r} "
                f"slot={rs.slot!r}",
            )
    if gaps:
        raise DomainValidationError(
            f"domain {spec.name!r}: archetype role_spans reference "
            "slots with no entity source (no gazetteer entries + no "
            f"inline diversity nodes):\n  " + "\n  ".join(gaps),
        )


__all__ = [
    "DiversityNode",
    "DiversityTaxonomy",
    "DomainSpec",
    "DomainValidationError",
    "load_all_domains",
    "load_domain",
]
