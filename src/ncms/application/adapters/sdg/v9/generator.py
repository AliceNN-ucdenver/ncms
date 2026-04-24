"""v9 stratified archetype corpus generator.

Consumes a :class:`DomainSpec` (loaded from a YAML plugin directory),
samples entities per archetype, prompts an :class:`LLMBackend` for
raw rows, and validates + labels each row into a :class:`GoldExample`.

Top-level contract:

* :func:`generate_for_archetype` — one archetype, ``n`` rows.
* :func:`generate_domain` — every archetype in a spec, respecting
  its ``n_gold`` / ``n_sdg`` target for the requested split.

The generator is deterministic for a given seed + backend choice —
``TemplateBackend`` is fully deterministic; ``SparkBackend`` is
deterministic only up to the LLM's own non-determinism (use
``temperature=0`` for reproducibility).

Entity sourcing strategy:

* **Gazetteer-backed slot** (slot appears in ``spec.gazetteer``) —
  sample from matching entries.  For alternative-role slots the
  sampler also de-duplicates against the primary pick so
  "medication switch" archetypes get two distinct medications.
* **Open-vocab slot** (no matching gazetteer entries) — sample
  from inline diversity nodes whose ``topic_hint`` matches the
  archetype's topic, falling back to any inline node if no topic
  match exists.

Validation rejections are logged via the returned
:class:`GenerationStats` but don't abort the run — the caller
decides whether the yield rate is acceptable.  A typical
TemplateBackend run hits > 95% yield; Spark live runs should
target > 70%.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ncms.application.adapters.schemas import GoldExample

if TYPE_CHECKING:
    # Deferred imports break the cycle:
    #   domain_loader → v9/__init__ → v9/generator → domain_loader.
    # The generator accesses DomainSpec / DiversityNode attributes
    # purely via duck-typing at runtime, so we only need them as
    # type hints.
    from ncms.application.adapters.domain_loader import (
        DiversityNode,
        DomainSpec,
    )
from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec
from ncms.application.adapters.sdg.v9.backends import (
    LLMBackend,
    TemplateBackend,
)
from ncms.application.adapters.sdg.v9.prompts import build_archetype_prompt
from ncms.application.adapters.sdg.v9.validation import (
    RejectionReason,
    validate_and_label,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stats + batch result
# ---------------------------------------------------------------------------


@dataclass
class GenerationStats:
    """Aggregate counters for one generation run (per archetype or domain).

    Attributes are cumulative across batches.  The generator mutates
    them in place; callers read after the run completes.
    """

    requested: int = 0            # how many rows were asked for
    generated: int = 0            # how many raw rows the backend produced
    accepted: int = 0             # rows that passed validation
    rejections: dict[RejectionReason, int] = field(default_factory=dict)
    duplicates: int = 0           # text-level dedup rejections

    def note_rejection(self, reason: RejectionReason) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    @property
    def yield_rate(self) -> float:
        if self.generated == 0:
            return 0.0
        return self.accepted / self.generated


# ---------------------------------------------------------------------------
# Entity sampling
# ---------------------------------------------------------------------------


def _sample_entities(
    spec: DomainSpec,
    archetype: ArchetypeSpec,
    rng: random.Random,
) -> dict[tuple[str, str], str]:
    """Pick one surface per ``(role, slot)`` required by the archetype.

    Returns a mapping ``{(role, slot): surface, ...}``.  Keys are
    unique because each ``RoleSpec(role, slot, count)`` with ``count > 1``
    would need multiple surfaces — we handle that by appending
    ``#i`` disambiguators per extra count.  (Practical archetypes
    ship with ``count=1`` for each role_span; multi-count is a
    forward-looking extension.)

    Sampling rules:

    * Gazetteer-backed slot → uniform pick from
      ``spec.gazetteer_by_slot[slot]``.
    * Open-vocab slot → pick from inline diversity nodes whose
      ``topic_hint`` matches the archetype's topic; fall back to
      any inline node if no topic match.

    When the same slot is requested for both primary and alternative
    roles (e.g. ``choice_medication_switch``), the alternative pick
    is drawn from the remaining pool so the two surfaces differ.
    """
    gaz_by_slot = spec.gazetteer_by_slot
    inline_nodes = tuple(
        n for n in spec.diversity.nodes if n.source == "inline"
    )

    out: dict[tuple[str, str], str] = {}
    used_per_slot: dict[str, set[str]] = {}
    # Iterate in a stable order so seed determinism holds across
    # reruns; role_spans is a tuple so its order is already stable.
    for rs in archetype.role_spans:
        if rs.count <= 0:
            continue
        for i in range(rs.count):
            key_suffix = "" if rs.count == 1 else f"#{i}"
            key = (rs.role, rs.slot + key_suffix)
            surface = _draw_one(
                slot=rs.slot,
                gaz_by_slot=gaz_by_slot,
                inline_nodes=inline_nodes,
                archetype=archetype,
                already_used=used_per_slot.setdefault(rs.slot, set()),
                rng=rng,
            )
            if surface is None:
                # No entity available for this (role, slot) pair —
                # signal via an empty value so the caller can skip
                # this archetype for this run.  We prefer a crisp
                # skip over silently emitting unlabeled rows.
                return {}
            used_per_slot[rs.slot].add(surface.lower())
            out[key] = surface
    return out


def _draw_one(
    *,
    slot: str,
    gaz_by_slot: dict[str, tuple],
    inline_nodes: tuple[DiversityNode, ...],
    archetype: ArchetypeSpec,
    already_used: set[str],
    rng: random.Random,
) -> str | None:
    """Return one surface for ``slot`` not in ``already_used``; None if
    the pool is exhausted or nonexistent.
    """
    # Gazetteer path.
    gaz_entries = gaz_by_slot.get(slot, ())
    if gaz_entries:
        candidates = [
            e.canonical for e in gaz_entries
            if e.canonical.lower() not in already_used
        ]
        if candidates:
            return rng.choice(candidates)
        return None

    # Open-vocab path: filter inline nodes by slot compatibility
    # FIRST, then by topic_hint.  The slot filter uses
    # ``DiversityNode.filter_slots``: a node participates when it
    # either declares no filter (universal) or explicitly lists
    # this slot.  Without this gate, a node whose vocabulary is
    # time-phrases ("in the afternoon") can be sampled as an
    # ``object`` slot entity, which is exactly the failure mode
    # caught in B'.4 probing.
    slot_compatible = [
        n for n in inline_nodes
        if not n.filter_slots or slot in n.filter_slots
    ]
    if not slot_compatible:
        return None

    # Prefer nodes whose topic_hint matches the archetype's topic
    # (when declared); fall back to all slot-compatible nodes so
    # archetypes with topic=None still get entities.
    matching = [
        n for n in slot_compatible
        if archetype.topic is not None and n.topic_hint == archetype.topic
    ]
    if not matching:
        matching = slot_compatible

    # Sample uniformly across candidate nodes (not weighted by size —
    # avoids dominant-category bias).
    node = rng.choice(matching)
    candidates = [
        ex for ex in node.examples if ex.lower() not in already_used
    ]
    if not candidates:
        # Try a different slot-compatible node if this one was exhausted.
        for n in matching:
            if n is node:
                continue
            candidates = [
                ex for ex in n.examples if ex.lower() not in already_used
            ]
            if candidates:
                return rng.choice(candidates)
        return None
    return rng.choice(candidates)


# ---------------------------------------------------------------------------
# Per-archetype loop
# ---------------------------------------------------------------------------


def generate_for_archetype(
    spec: DomainSpec,
    archetype: ArchetypeSpec,
    *,
    n: int,
    backend: LLMBackend,
    split: Literal["gold", "llm", "sdg", "adversarial"] = "sdg",
    seed: int = 17,
    stats: GenerationStats | None = None,
) -> tuple[list[GoldExample], GenerationStats]:
    """Produce ``n`` validated :class:`GoldExample` rows for one archetype.

    Retries + batching:

    * Rows are drawn in batches of ``archetype.batch_size``.  Each
      batch picks a fresh entity set (so repeated archetypes
      rotate across the pool) and calls ``backend.generate`` once.
    * Accepted rows accumulate until we reach ``n`` or we've
      generated ``3 * n`` raw rows without meeting the target
      (cap protects against stuck archetypes — e.g. a gazetteer
      slot with fewer entries than ``n``).
    """
    rng = random.Random(seed)
    stats = stats or GenerationStats()
    stats.requested += n

    accepted: list[GoldExample] = []
    seen_texts: set[str] = set()
    max_raw = max(n * 3, archetype.batch_size * 2)
    raw_count = 0

    # For TemplateBackend, we bypass the LLM-prompt path entirely and
    # fill phrasings directly — template yield doesn't benefit from
    # per-row entity variety (phrasings are already limited).  The
    # LLM path builds a per-row-entity prompt so a single batch call
    # produces len(entity_rows) distinct surface sentences without
    # collapsing to paraphrases of one entity pick.
    is_template_backend = isinstance(backend, TemplateBackend)

    while len(accepted) < n and raw_count < max_raw:
        batch_n = min(archetype.batch_size, n - len(accepted))
        if batch_n <= 0:
            break

        # Sample ONE entity set per row in this batch.  Per-row
        # sampling is the key diversity fix — previously we picked
        # a single entity set per batch and asked the LLM to produce
        # batch_n rows all using that one entity, which collapsed
        # to eight paraphrases of the same sentence.
        entity_rows: list[dict[tuple[str, str], str]] = []
        for _ in range(batch_n):
            entities = _sample_entities(spec, archetype, rng)
            if not entities and archetype.role_spans:
                # Domain has no entity source for this archetype; we
                # still need SOMETHING to hand the validator.  An
                # empty dict means "no entity constraint" — validation
                # will skip the entity-presence check, and role-span
                # composition will pass only if the archetype
                # declared zero role_spans.
                logger.warning(
                    "archetype %r: no entities available at row sample; "
                    "emitting empty-entity row (validator may reject)",
                    archetype.name,
                )
            entity_rows.append(entities)

        # Abort this archetype if every row sampled empty — typically
        # means the domain has no gazetteer coverage AND no inline
        # diversity nodes.  Logging once beats spamming the log per
        # sample.
        if all(not e for e in entity_rows) and archetype.role_spans:
            logger.warning(
                "archetype %r: batch produced zero entity sets "
                "(domain=%s); stopping this archetype",
                archetype.name, spec.name,
            )
            break

        if is_template_backend:
            # Template backend renders per-row phrasings using each
            # row's own entity set, so surface diversity matches the
            # LLM path.
            raw_rows = []
            for row_entities in entity_rows:
                phrasings = _prefill_phrasings(archetype, row_entities)
                raw_rows.extend(
                    TemplateBackend(phrasings=phrasings).generate(
                        prompt="", n=1, rng=rng,
                    ),
                )
        else:
            prompt = build_archetype_prompt(
                archetype,
                entity_rows=entity_rows,
                domain_description=spec.description,
            )
            raw_rows = backend.generate(
                prompt=prompt, n=batch_n, rng=rng,
            )

        stats.generated += len(raw_rows)
        raw_count += len(raw_rows)

        # Walk each returned row against the matching entity set.
        # The LLM returns rows in the order requested; short-count
        # responses (len(raw_rows) < batch_n) naturally map to the
        # first entity_rows entries.
        for i, text in enumerate(raw_rows):
            if i >= len(entity_rows):
                # LLM overshoot past the requested count — drop.
                break
            row_entities = entity_rows[i]
            norm = text.strip()
            if norm in seen_texts:
                stats.duplicates += 1
                continue
            outcome = validate_and_label(
                norm,
                archetype=archetype,
                entities=row_entities,
                domain=spec.name,  # type: ignore[arg-type]
            )
            if not outcome.ok:
                assert outcome.reason is not None
                stats.note_rejection(outcome.reason)
                continue
            seen_texts.add(norm)
            accepted.append(GoldExample(
                text=norm,
                domain=spec.name,  # type: ignore[arg-type]
                intent=archetype.intent,
                slots=_build_slots_from_entities(row_entities),
                topic=archetype.topic,
                admission=archetype.admission,
                state_change=archetype.state_change,
                role_spans=list(outcome.role_spans),
                split=split,
                source=f"sdg-v9 archetype={archetype.name} seed={seed}",
            ))
            stats.accepted += 1
            if len(accepted) >= n:
                break

    return accepted, stats


def _build_slots_from_entities(
    entities: dict[tuple[str, str], str],
) -> dict[str, str]:
    """Flatten ``{(role, slot): surface}`` to ``{slot: surface}``.

    Keeps the PRIMARY-role surface when multiple roles touch the same
    slot (e.g. ``choice_medication_switch`` has both primary and
    alternative medications — slots["medication"] gets the primary).
    Alternative surfaces land in the ``alternative`` slot by
    convention so downstream consumers can read them out.
    """
    out: dict[str, str] = {}
    # First pass: primary wins the slot.
    for (role, slot_key), surface in entities.items():
        base_slot = slot_key.split("#", 1)[0]
        if role == "primary":
            out[base_slot] = surface
    # Second pass: alternatives fill "alternative" if not already set.
    for (role, _slot_key), surface in entities.items():
        if role == "alternative" and "alternative" not in out:
            out["alternative"] = surface
    # Third pass: casual slots fill in only if primary didn't already.
    for (role, slot_key), surface in entities.items():
        base_slot = slot_key.split("#", 1)[0]
        if role == "casual" and base_slot not in out:
            out[base_slot] = surface
    return out


def _prefill_phrasings(
    archetype: ArchetypeSpec,
    entities: dict[tuple[str, str], str],
) -> tuple[str, ...]:
    """Replace ``{primary}`` / ``{alternative}`` / ``{casual}`` /
    ``{<slot>}`` placeholders in the archetype's phrasings with
    sampled entity surfaces.

    Keeps free-text placeholders (``{condition}``, ``{rationale}``
    etc.) intact — TemplateBackend fills those with canned fillers.
    """
    # Build a lookup: primary/alternative/casual role → surface,
    # plus per-slot surfaces keyed by slot name.
    role_map: dict[str, str] = {}
    slot_map: dict[str, str] = {}
    for (role, slot_key), surface in entities.items():
        base_slot = slot_key.split("#", 1)[0]
        role_map.setdefault(role, surface)
        slot_map.setdefault(base_slot, surface)

    def sub(template: str) -> str:
        for role, surface in role_map.items():
            template = template.replace("{" + role + "}", surface)
        for slot, surface in slot_map.items():
            template = template.replace("{" + slot + "}", surface)
        return template

    return tuple(sub(p) for p in archetype.phrasings)


# ---------------------------------------------------------------------------
# Domain-level driver
# ---------------------------------------------------------------------------


def generate_domain(
    spec: DomainSpec,
    *,
    backend: LLMBackend,
    split: Literal["gold", "sdg"] = "sdg",
    seed: int = 17,
) -> tuple[list[GoldExample], dict[str, GenerationStats]]:
    """Walk every archetype in ``spec`` and produce a combined corpus.

    Returns ``(rows, per_archetype_stats)``.  Rows are emitted in
    archetype-order so downstream shuffling / stratified splits work.
    Per-archetype stats let callers see which archetypes are
    under-yielding without digging through logs.

    ``split="gold"`` uses each archetype's ``n_gold`` target;
    ``split="sdg"`` uses ``n_sdg``.  Rows are stamped with the
    matching ``split`` label so downstream trainers can filter.
    """
    all_rows: list[GoldExample] = []
    stats_by_arch: dict[str, GenerationStats] = {}
    for i, archetype in enumerate(spec.archetypes):
        n = archetype.n_gold if split == "gold" else archetype.n_sdg
        if n <= 0:
            continue
        # Stable per-archetype seed so adding archetypes doesn't
        # reshuffle earlier ones.
        arch_seed = seed + i * 101
        rows, stats = generate_for_archetype(
            spec, archetype,
            n=n, backend=backend,
            split=split,  # type: ignore[arg-type]
            seed=arch_seed,
        )
        all_rows.extend(rows)
        stats_by_arch[archetype.name] = stats
        logger.info(
            "v9-gen domain=%s archetype=%s split=%s "
            "requested=%d accepted=%d yield=%.1f%% rejections=%s",
            spec.name, archetype.name, split,
            stats.requested, stats.accepted, stats.yield_rate * 100.0,
            stats.rejections,
        )
    return all_rows, stats_by_arch


__all__ = [
    "GenerationStats",
    "generate_domain",
    "generate_for_archetype",
]
