"""v9 archetype schema.

An ``ArchetypeSpec`` is one stratified generation archetype: a fixed
joint label combination (intent × admission × state_change) with
fixed role-span composition + generation parameters + prompt surface.

Per-domain archetype registries import this module to declare their
16 archetypes; :func:`validate_archetype_coverage` checks that every
head's class meets its minimum-rows floor across the registry before
generation runs.

Design rationale: ``docs/research/v9-corpus-generation-design.md``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from ncms.application.adapters.schemas import (
    ADMISSION_DECISIONS,
    INTENT_CATEGORIES,
    ROLE_LABELS,
    STATE_CHANGES,
    AdmissionDecision,
    Domain,
    Intent,
    Role,
    StateChange,
)


@dataclass(frozen=True)
class RoleSpec:
    """One role-span slot declaration in an archetype.

    Example: ``RoleSpec(role="primary", slot="framework", count=1)``
    means every row emitted by this archetype must have exactly one
    primary-role span in the ``framework`` slot.

    The generator uses this to:

    1. Pre-sample entities from the per-domain catalog when the
       domain has one (software_dev) — or ask the LLM to generate
       + annotate entities inline when the slot is open-vocabulary
       (conversational ``object``, clinical ``symptom``).
    2. Validate generated rows: if an archetype declares
       ``role="primary", slot="framework", count=1`` and the returned
       row has zero primary spans (or a primary span in a different
       slot), the row is dropped.
    """

    role: Role
    slot: str
    count: int = 1

    def __post_init__(self) -> None:
        if self.role not in ROLE_LABELS:
            raise ValueError(
                f"RoleSpec.role {self.role!r} not in {ROLE_LABELS}",
            )
        if self.count < 0:
            raise ValueError(f"RoleSpec.count must be >= 0, got {self.count}")


_HEAD_NAMES = ("intent", "admission", "state_change")


@dataclass(frozen=True)
class ArchetypeSpec:
    """A stratified generation archetype.

    Parameters
    ----------
    name
        Stable identifier, used in logs + filenames.  Convention:
        ``<intent>_<state_change>[_<notable_modifier>]`` e.g.
        ``positive_declaration_with_alternative``.
    domain
        Which domain this archetype produces rows for.
    intent, admission, state_change
        **Fixed** labels — every row emitted by this archetype gets
        exactly these three labels.  The generator asserts this at
        validation time.
    topic
        Fixed topic if the archetype has one (e.g. "food_pref" on
        conversational food archetypes).  ``None`` = topic is sampled
        per-row from the domain's topic taxonomy.
    role_spans
        Tuple of :class:`RoleSpec` declaring the role-span
        composition.  A row passes validation iff it contains
        exactly these role-span counts.
    n_gold
        Target row count for the ``gold`` split.
    n_sdg
        Target row count for the ``sdg`` split (larger bulk augmentation).
    target_min_chars / target_max_chars
        Text-length envelope.  Rows outside this range are dropped.
        Archetypes should overlap ranges so the full corpus has
        length diversity.
    batch_size
        Rows requested per Spark call.
    description
        One-liner used in the prompt + audit reports.
    example_utterances
        3-8 few-shot examples embedded in the prompt.  Each should
        match the archetype's joint label + role-span composition.
    phrasings
        Surface templates with ``{primary}``, ``{alternative}``,
        ``{casual}``, ``{not_relevant}`` placeholders.  Generator
        rotates through them to keep surface diversity high.

    Label balance is enforced at the registry level via
    :func:`validate_archetype_coverage` — every head's class must
    appear in enough archetypes that the corpus-level total
    satisfies the per-head floor (default 50 rows / class).
    """

    name: str
    domain: Domain

    intent: Intent
    admission: AdmissionDecision
    state_change: StateChange
    topic: str | None = None

    role_spans: tuple[RoleSpec, ...] = ()

    n_gold: int = 30
    n_sdg: int = 150

    target_min_chars: int = 20
    target_max_chars: int = 200
    batch_size: int = 10

    description: str = ""
    example_utterances: tuple[str, ...] = ()
    phrasings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Label vocabulary checks — catch typos early.
        if self.intent not in INTENT_CATEGORIES:
            raise ValueError(
                f"archetype {self.name!r}: unknown intent {self.intent!r}",
            )
        if self.admission not in ADMISSION_DECISIONS:
            raise ValueError(
                f"archetype {self.name!r}: "
                f"unknown admission {self.admission!r}",
            )
        if self.state_change not in STATE_CHANGES:
            raise ValueError(
                f"archetype {self.name!r}: "
                f"unknown state_change {self.state_change!r}",
            )

        # Size sanity.
        if self.n_gold < 0 or self.n_sdg < 0:
            raise ValueError(f"archetype {self.name!r}: negative target count")
        if self.target_min_chars <= 0:
            raise ValueError(
                f"archetype {self.name!r}: "
                f"target_min_chars must be > 0",
            )
        if self.target_max_chars < self.target_min_chars:
            raise ValueError(
                f"archetype {self.name!r}: target_max_chars "
                f"({self.target_max_chars}) < target_min_chars "
                f"({self.target_min_chars})",
            )
        if self.batch_size < 1:
            raise ValueError(f"archetype {self.name!r}: batch_size must be >= 1")

        # Prompt surface is required — we don't generate from empty prompts.
        if not self.description.strip():
            raise ValueError(
                f"archetype {self.name!r}: empty description "
                "(required for prompt + audit reports)",
            )

    # ── Convenience views ─────────────────────────────────────────

    @property
    def primary_role_specs(self) -> tuple[RoleSpec, ...]:
        return tuple(rs for rs in self.role_spans if rs.role == "primary")

    @property
    def alternative_role_specs(self) -> tuple[RoleSpec, ...]:
        return tuple(rs for rs in self.role_spans if rs.role == "alternative")

    @property
    def total_role_count(self) -> int:
        return sum(rs.count for rs in self.role_spans)


# ---------------------------------------------------------------------------
# Registry-level coverage check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageGap:
    """A single class-floor violation found by coverage validation."""

    head: Literal["intent", "admission", "state_change", "role"]
    cls: str
    found: int
    floor: int

    def __str__(self) -> str:
        return (
            f"{self.head}={self.cls!r}: "
            f"archetype sum = {self.found} < floor {self.floor}"
        )


def validate_archetype_coverage(
    archetypes: list[ArchetypeSpec],
    *,
    split: Literal["gold", "sdg"] = "gold",
    intent_floor: int = 50,
    admission_floor: int = 50,
    state_change_floor: int = 50,
    role_floor: int = 100,
) -> list[CoverageGap]:
    """Verify the archetype registry produces enough rows per class.

    Sums the per-archetype target counts grouped by each head's
    label.  Any (head, class) pair below its floor is returned as a
    :class:`CoverageGap`.  Empty return list means the registry
    satisfies every head's class balance at the requested split.

    Parameters
    ----------
    archetypes
        Per-domain archetype list.
    split
        Which target count to use: ``"gold"`` reads ``n_gold``,
        ``"sdg"`` reads ``n_sdg``.
    intent_floor / admission_floor / state_change_floor
        Minimum row count per class for those scalar heads.
    role_floor
        Minimum total role-span count per role label across the
        registry.  Note this is **span count**, not row count —
        a single row can contribute multiple role spans.

    Raises
    ------
    ValueError
        If ``split`` is not one of ``"gold"`` or ``"sdg"``.
    """
    if split not in ("gold", "sdg"):
        raise ValueError(f"unknown split {split!r}")

    per_intent: dict[str, int] = defaultdict(int)
    per_admission: dict[str, int] = defaultdict(int)
    per_state_change: dict[str, int] = defaultdict(int)
    per_role: dict[str, int] = defaultdict(int)

    for a in archetypes:
        n = a.n_gold if split == "gold" else a.n_sdg
        per_intent[a.intent] += n
        per_admission[a.admission] += n
        per_state_change[a.state_change] += n
        for rs in a.role_spans:
            per_role[rs.role] += n * rs.count

    gaps: list[CoverageGap] = []

    for cls in INTENT_CATEGORIES:
        found = per_intent.get(cls, 0)
        if found < intent_floor:
            gaps.append(
                CoverageGap("intent", cls, found, intent_floor),
            )
    for cls in ADMISSION_DECISIONS:
        found = per_admission.get(cls, 0)
        if found < admission_floor:
            gaps.append(
                CoverageGap("admission", cls, found, admission_floor),
            )
    for cls in STATE_CHANGES:
        found = per_state_change.get(cls, 0)
        if found < state_change_floor:
            gaps.append(
                CoverageGap(
                    "state_change", cls, found, state_change_floor,
                ),
            )
    for cls in ROLE_LABELS:
        found = per_role.get(cls, 0)
        if found < role_floor:
            gaps.append(
                CoverageGap("role", cls, found, role_floor),
            )

    return gaps


__all__ = [
    "ArchetypeSpec",
    "CoverageGap",
    "RoleSpec",
    "validate_archetype_coverage",
]
