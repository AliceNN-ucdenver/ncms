"""CTLG causal heuristics — pure-function trajectory rankers.

Classical Stilman Linguistic Geometry uses **min-max** to rank
trajectories in adversarial game search.  NCMS is non-adversarial —
memories evolve in time, not against an opponent — so the ranking
primitive needed a different formal shape.  CTLG replaces min-max
with five typed causal heuristics that score trajectories by how
well they explain / fit / cover the target query.

See :doc:`../../../docs/research/ctlg-grammar.md` §4 for the full
design.  This module implements the five heuristics as pure
functions plus the weighted composition rule.

The heuristics are:

  * ``h_explanatory``      — how much of the observed state does the
                             trajectory explain?  Used by causal
                             targets (cause_of, chain_cause_of,
                             contributing_factor).
  * ``h_parsimony``        — prefer shorter trajectories (Occam).
  * ``h_recency``          — exponential decay on the terminal's
                             age.  Used by interval / range /
                             concurrent targets.  NOT used by
                             origin / before_named (anti-recent).
  * ``h_robustness``       — density of SUPPORTS edges
                             corroborating the trajectory.
  * ``h_counterfactual_dist``
                           — minimize # of edge-skips to reach the
                             requested scenario.  Only active on
                             modal / counterfactual targets.

Each heuristic returns a float in [0, 1] (higher is better).  The
composition rule :func:`rank_trajectories` applies a per-intent
weighted sum to produce the final ranking.

Pure functions — no torch, no LLM, no I/O.  Fast unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Typed Trajectory
# ---------------------------------------------------------------------------


#: Trajectory kinds (ctlg-grammar.md §2.4).  Each kind is generated
#: by a specific subgrammar; the ``kind`` field is what
#: ``rank_trajectories`` keys the per-intent weight lookup by.
TrajectoryKind = Literal[
    "refines_chain",      # G_tr,s linear refinement
    "supersedes_chain",   # G_tr,s linear supersession
    "retirement_arc",     # G_tr,s terminates in retires(M)
    "causal_chain",       # G_tr,c chain of caused_by edges
    "enables_arc",        # G_tr,c terminates in enables(M)
    "mixed",              # G_tr,m composed causal + state-evolution
]


@dataclass(frozen=True)
class Trajectory:
    """A typed walk through the zone graph.

    Replaces the implicit ``Zone.memory_ids`` chain with an explicit
    typed structure that names which subgrammar generated it and
    preserves per-heuristic scores for explainability.

    Attributes
    ----------
    kind
        Which trajectory subgrammar produced this walk.
    memory_ids
        Ordered walk through memory nodes.
    edge_types
        Parallel to ``memory_ids``; names each edge in the walk.
        ``edge_types[i]`` is the edge FROM memory_ids[i] TO
        memory_ids[i+1].
    subject
        Subject whose state the trajectory tracks.  ``None`` for
        cross-subject causal walks.
    terminal_observed_at
        ``observed_at`` of the trajectory's terminal node — feeds
        ``h_recency``.  ``None`` when no temporal metadata
        available (treats as "ancient").
    supports_edge_counts
        Per-node count of SUPPORTS edges entering that node.  Feeds
        ``h_robustness``.  Length matches memory_ids; 0 when the
        node has no supports.
    explained_state_keys
        The ``(entity_id, state_key)`` pairs this trajectory
        explains — feeds ``h_explanatory``.  Populated by the
        dispatcher from the subject's state space at walk time.
    skipped_edges
        For modal / counterfactual trajectories: the edges skipped
        to construct the counterfactual scenario.  Feeds
        ``h_counterfactual_dist``.
    heuristic_scores
        Per-heuristic scalar scores, filled by
        :func:`score_trajectory` and read by
        :func:`rank_trajectories`.
    """

    kind: TrajectoryKind
    memory_ids: tuple[str, ...]
    edge_types: tuple[str, ...]
    subject: str | None = None
    terminal_observed_at: datetime | None = None
    supports_edge_counts: tuple[int, ...] = ()
    explained_state_keys: frozenset[tuple[str, str]] = frozenset()
    skipped_edges: tuple[str, ...] = ()
    heuristic_scores: dict[str, float] = field(default_factory=dict)

    @property
    def length(self) -> int:
        """Number of edges in the walk.  Zero for single-node walks."""
        return len(self.edge_types)

    @property
    def combined_score(self) -> float:
        """Sum of populated heuristic scores.  See
        :func:`rank_trajectories` for weighted composition."""
        return sum(self.heuristic_scores.values())


# ---------------------------------------------------------------------------
# Context — what the trajectory is being ranked against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeuristicContext:
    """Shared context passed to every heuristic.

    Carries the query-side facts the heuristics need: the total
    state-key universe (so ``h_explanatory`` can normalize), the
    evaluation timestamp (so ``h_recency`` can compute age), and
    the counterfactual scenario (so ``h_counterfactual_dist``
    knows which edges were skipped).
    """

    total_state_keys: int = 1
    #: Typically ``datetime.now(UTC)`` at query time.  Exposed so
    #: tests can inject a deterministic clock.
    evaluated_at: datetime | None = None
    #: Domain-specific decay rate for ``h_recency``.  Units: per
    #: day.  Higher = faster decay.  Tuned per domain; default 0.01
    #: (software_dev's ADR cadence).
    recency_lambda_per_day: float = 0.01
    #: Parsimony falloff alpha.  h_parsimony(T) = 1 / (1 + α*(len - min_len))
    parsimony_alpha: float = 0.2
    #: Minimum admissible length for the target intent.  Prevents
    #: depth=1 trajectories from dominating when depth>1 is
    #: required (e.g. chain_cause_of).
    min_length: int = 1
    #: Scenario name (for modal queries) — passed through to
    #: h_counterfactual_dist.  ``None`` for non-modal queries.
    scenario: str | None = None


# ---------------------------------------------------------------------------
# The five heuristics
# ---------------------------------------------------------------------------


def h_explanatory(t: Trajectory, ctx: HeuristicContext) -> float:
    """Explanation coverage — how many of the subject's state keys
    does this trajectory touch?

    .. math::
        h_{explanatory}(T) = \\frac{|\\text{explained}(T)|}{|\\text{total}|}

    Higher values mean the trajectory explains more of the observed
    state.  Returns 0.0 when the trajectory explains nothing (the
    worst outcome for a causal query).  Returns 1.0 when the
    trajectory covers every state key in the subject (best).
    """
    if ctx.total_state_keys <= 0:
        return 0.0
    explained = len(t.explained_state_keys)
    if explained <= 0:
        return 0.0
    return min(1.0, explained / ctx.total_state_keys)


def h_parsimony(t: Trajectory, ctx: HeuristicContext) -> float:
    """Occam's razor — prefer shorter trajectories.

    .. math::
        h_{parsimony}(T) = \\frac{1}{1 + \\alpha \\cdot \\max(0, |T| - L_{min})}

    ``min_length`` comes from the target intent (e.g. 1 for direct
    cause_of, 2 for chain_cause_of).  Excess length over min_length
    degrades the score multiplicatively.
    """
    excess = max(0, t.length - ctx.min_length)
    return 1.0 / (1.0 + ctx.parsimony_alpha * excess)


def h_recency(t: Trajectory, ctx: HeuristicContext) -> float:
    """Temporal recency — exponential decay on terminal age.

    .. math::
        h_{recency}(T) = e^{-\\lambda \\cdot \\Delta\\text{days}}

    Returns 1.0 when the terminal is "now", falling toward 0 as
    age grows.  Returns 0.0 when the trajectory has no
    ``terminal_observed_at`` (treated as infinitely old — absence
    of evidence is treated as the worst case for recency).

    NOT used by origin / before_named / ordinal_first queries
    (those want older nodes) — caller excludes this heuristic via
    the weight config for those intents.
    """
    if t.terminal_observed_at is None:
        return 0.0
    now = ctx.evaluated_at or datetime.now(UTC)
    # Guard against tz-naive terminal; assume UTC.
    terminal = t.terminal_observed_at
    if terminal.tzinfo is None:
        terminal = terminal.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    delta_days = max(0.0, (now - terminal).total_seconds() / 86400.0)
    return math.exp(-ctx.recency_lambda_per_day * delta_days)


def h_robustness(t: Trajectory, ctx: HeuristicContext) -> float:
    """Supporting-evidence density — SUPPORTS edges per node.

    .. math::
        h_{robustness}(T) = \\min(1, \\frac{\\sum \\text{supports}(m_i)}{|T|})

    Trajectories whose nodes are corroborated by many SUPPORTS
    edges outrank trajectories based on a single observation.
    Capped at 1.0 so extraordinarily-supported trajectories don't
    dominate the other heuristics.
    """
    n = len(t.memory_ids)
    if n == 0:
        return 0.0
    if not t.supports_edge_counts:
        return 0.0
    total = sum(t.supports_edge_counts)
    return min(1.0, total / n)


def h_counterfactual_dist(t: Trajectory, ctx: HeuristicContext) -> float:
    """Counterfactual minimality — inverse of # edges skipped.

    .. math::
        h_{CF}(T) = 1 - \\frac{|\\text{skipped}(T)|}{\\max(1, |T|)}

    A counterfactual answer that requires skipping ONE edge is
    stronger than one requiring many skips.  Returns 1.0 when no
    edges were skipped (the actual history) and approaches 0 as
    the scenario requires more skips.

    Only meaningful for modal / counterfactual queries; other
    targets receive weight 0 on this heuristic.
    """
    if t.length == 0:
        return 1.0
    n_skipped = len(t.skipped_edges)
    if n_skipped <= 0:
        return 1.0
    # Normalize skip count by trajectory length; cap at 1.0 skip-
    # per-edge (fully-skipped path → 0).
    return max(0.0, 1.0 - (n_skipped / t.length))


#: Map from heuristic name to the implementation function.  Public
#: so callers can introspect + build custom compositions.
HEURISTIC_FUNCS = {
    "h_explanatory": h_explanatory,
    "h_parsimony": h_parsimony,
    "h_recency": h_recency,
    "h_robustness": h_robustness,
    "h_counterfactual_dist": h_counterfactual_dist,
}


# ---------------------------------------------------------------------------
# Scoring + ranking
# ---------------------------------------------------------------------------


def score_trajectory(
    t: Trajectory, ctx: HeuristicContext,
    *, heuristics: list[str] | None = None,
) -> Trajectory:
    """Compute the named heuristics on ``t`` and return a NEW
    :class:`Trajectory` with ``heuristic_scores`` populated.

    ``heuristics`` defaults to all five — callers typically pass
    the subset relevant to the target intent (per the weight
    config in ctlg-grammar.md §4.6).

    Pure function — doesn't mutate ``t``.
    """
    if heuristics is None:
        heuristics = list(HEURISTIC_FUNCS.keys())
    new_scores = dict(t.heuristic_scores)
    for h_name in heuristics:
        fn = HEURISTIC_FUNCS.get(h_name)
        if fn is None:
            continue
        new_scores[h_name] = float(fn(t, ctx))
    # Dataclass is frozen; make a copy with updated scores.
    from dataclasses import replace
    return replace(t, heuristic_scores=new_scores)


def rank_trajectories(
    candidates: list[Trajectory],
    weights: dict[str, float],
    *,
    context: HeuristicContext,
) -> list[Trajectory]:
    """Rank candidates by weighted heuristic sum.

    Each candidate's ``heuristic_scores`` must already have been
    populated by :func:`score_trajectory` for the heuristics
    named in ``weights``.  Weights with no matching score
    contribute 0.

    Deterministic tie-break: when two trajectories score
    identically, the one with the longer ``memory_ids[0]`` (i.e.
    lexicographically larger first memory id) wins.  This keeps
    ranking stable across runs for the same input.
    """

    def weighted_score(t: Trajectory) -> float:
        return sum(
            weights.get(h_name, 0.0) * t.heuristic_scores.get(h_name, 0.0)
            for h_name in weights
        )

    # Sort primarily by weighted score (desc), secondarily by the
    # first memory id (desc) for a deterministic tie-break.
    return sorted(
        candidates,
        key=lambda t: (
            weighted_score(t),
            t.memory_ids[0] if t.memory_ids else "",
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Default weight configurations per target intent
# ---------------------------------------------------------------------------


#: Per-target-intent default heuristic weights.  Maps the concrete
#: ``TLGRelation`` value (from ``semantic_parser.TLGQuery.relation``)
#: to the heuristic-weight dict.  Operators can override these via
#: ``NCMS_TLG_HEURISTIC_WEIGHTS_PATH`` YAML at runtime.
#:
#: Design rationale (ctlg-grammar.md §4.6):
#:
#:  * Causal queries lean on ``h_explanatory`` — the trajectory
#:    that explains more observed state is preferred.
#:  * Interval/temporal queries lean on ``h_recency`` because
#:    operational relevance decays with age.
#:  * Ordinal queries (first / last) lean on ``h_parsimony`` —
#:    the most direct chain wins.
#:  * Modal queries weight ``h_counterfactual_dist`` heavily —
#:    minimal scenario-skipping wins.
#:  * Origin / before_named INVERT recency implicitly by omitting
#:    ``h_recency`` from their weight dict; older nodes aren't
#:    penalized.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    # Causal axis
    "cause_of": {
        "h_explanatory": 0.5,
        "h_parsimony":   0.3,
        "h_robustness":  0.2,
    },
    "chain_cause_of": {
        "h_explanatory": 0.6,
        "h_parsimony":   0.4,
    },
    "contributing_factor": {
        "h_explanatory": 0.5,
        "h_robustness":  0.5,
    },
    "effect_of": {
        "h_explanatory": 0.6,
        "h_parsimony":   0.4,
    },
    "trigger_of": {
        "h_parsimony":   0.6,
        "h_robustness":  0.4,
    },
    # Temporal axis
    "before_named": {
        "h_parsimony":   0.7,
        "h_robustness":  0.3,
        # NOTE: no h_recency — older = better for "before"
    },
    "after_named": {
        "h_parsimony":   0.6,
        "h_recency":     0.4,
    },
    "during_interval": {
        "h_recency":     0.7,
        "h_parsimony":   0.3,
    },
    "concurrent_with": {
        "h_recency":     0.5,
        "h_parsimony":   0.5,
    },
    "between": {
        "h_parsimony":   0.6,
        "h_robustness":  0.4,
    },
    "state_at": {
        "h_robustness":  0.6,
        "h_recency":     0.4,
    },
    # Ordinal axis
    "first": {
        "h_parsimony":   0.8,
        "h_robustness":  0.2,
        # NOTE: no h_recency — first is OLDEST
    },
    "last": {
        "h_recency":     0.7,
        "h_parsimony":   0.3,
    },
    "nth": {
        "h_parsimony":   1.0,
    },
    # Modal axis — counterfactual dominates
    "would_be_current_if": {
        "h_counterfactual_dist": 0.7,
        "h_parsimony":           0.3,
    },
    "could_have_been": {
        "h_counterfactual_dist": 0.6,
        "h_explanatory":         0.4,
    },
    # State axis
    "current": {
        "h_recency":     0.8,
        "h_robustness":  0.2,
    },
    "retired": {
        "h_parsimony":   0.6,
        "h_robustness":  0.4,
    },
    "declared": {
        "h_parsimony":   0.6,
        "h_robustness":  0.4,
    },
}


def weights_for_relation(relation: str) -> dict[str, float]:
    """Look up the default weight config for a TLGRelation.

    Falls back to an explanatory+parsimony blend when the relation
    isn't in the registered defaults — ensures every walker has
    SOMETHING to rank with, even for relations we haven't tuned yet.
    """
    return DEFAULT_WEIGHTS.get(relation, {
        "h_explanatory": 0.5,
        "h_parsimony":   0.5,
    })


__all__ = [
    "DEFAULT_WEIGHTS",
    "HEURISTIC_FUNCS",
    "HeuristicContext",
    "Trajectory",
    "TrajectoryKind",
    "h_counterfactual_dist",
    "h_explanatory",
    "h_parsimony",
    "h_recency",
    "h_robustness",
    "rank_trajectories",
    "score_trajectory",
    "weights_for_relation",
]
