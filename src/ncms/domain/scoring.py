"""ACT-R inspired activation scoring and admission scoring for memory retrieval.

Pure mathematical functions with no I/O dependencies.

ACT-R activation (Anderson 2007):
    A_i = B_i + S_i + noise
    B_i = ln(sum_j(t_j^(-d)))           base-level learning
    S_i = sum_k(W_k * S_ki)             spreading activation
    P_i = 1 / (1 + exp(-A_i / tau))     retrieval probability

Admission scoring:
    AdmissionScore = weighted sum of 4 text-heuristic features
    Route = threshold-based policy over features + score
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


def base_level_activation(
    access_ages_seconds: list[float],
    decay: float = 0.5,
) -> float:
    """Compute base-level activation from access history.

    Args:
        access_ages_seconds: Time in seconds since each access event.
            Must be positive values.
        decay: Decay parameter (d). Default 0.5 per ACT-R.

    Returns:
        Base-level activation B_i = ln(sum(t_j^-d))
    """
    if not access_ages_seconds:
        return -10.0  # Very low activation for never-accessed memories

    total = 0.0
    for t in access_ages_seconds:
        if t > 0:
            total += t ** (-decay)

    if total <= 0:
        return -10.0

    return math.log(total)


def spreading_activation(
    memory_entity_ids: list[str],
    context_entity_ids: list[str],
    association_strengths: dict[tuple[str, str], float] | None = None,
    source_activation: float = 1.0,
) -> float:
    """Compute spreading activation from context to memory via entity overlap.

    Legacy overlap model: activation spreads from context entities to memory
    entities that match. Used as the ACT-R S_i component.

    Uses Jaccard normalization: |overlap| / |union| instead of |overlap| / |context|.
    Association strengths from dream cycle PMI optionally weight connections.

    Args:
        memory_entity_ids: Entity IDs linked to the candidate memory.
        context_entity_ids: Entity IDs active in the current query context.
        association_strengths: Optional weights for (context, memory) entity pairs.
        source_activation: Total activation available to spread.

    Returns:
        Spreading activation S_i (Jaccard-normalized).
    """
    if not memory_entity_ids or not context_entity_ids:
        return 0.0

    mem_set = set(memory_entity_ids)
    ctx_set = set(context_entity_ids)
    overlap = mem_set & ctx_set
    if not overlap:
        return 0.0

    # Jaccard normalization: |overlap| / |union| (Fix #5)
    union_size = len(mem_set | ctx_set)
    if union_size == 0:
        return 0.0

    if association_strengths:
        # Use explicit association strengths (PMI from dream cycles)
        total = 0.0
        for entity_id in overlap:
            max_strength = 0.0
            for ctx_id in ctx_set:
                s = association_strengths.get((ctx_id, entity_id), 0.0)
                max_strength = max(max_strength, s)
            total += max_strength
        return source_activation * total / union_size
    else:
        # Default: Jaccard overlap
        return source_activation * len(overlap) / union_size


def graph_spreading_activation(
    memory_entity_ids: list[str],
    context_entity_ids: list[str],
    neighbor_fn: object,  # Callable[[str], list[tuple[str, float]]]
    entity_idf: dict[str, float] | None = None,
    hop_decay: float = 0.5,
    max_hops: int = 2,
    source_activation: float = 1.0,
    degree_fn: object | None = None,  # Callable[[str], int] for hub dampening
) -> float:
    """Compute graph-based spreading activation with per-hop decay and IDF weighting.

    Propagates activation from context entities through the graph to reach
    memory entities. Unlike the overlap model, this traverses edges with
    decay per hop, weights entities by IDF (rare entities contribute more),
    and dampens activation through high-degree hub nodes.

    Args:
        memory_entity_ids: Entity IDs linked to the candidate memory.
        context_entity_ids: Entity IDs active in the current query context.
        neighbor_fn: Function(entity_id) → [(neighbor_id, edge_weight), ...].
            Must return weighted neighbors for graph traversal.
        entity_idf: Optional dict mapping entity_id → IDF weight.
            If None, all entities weighted equally.
        hop_decay: Activation multiplier per hop (0.5 = halve each hop).
        max_hops: Maximum hops to propagate (default 2).
        source_activation: Total activation available to spread.
        degree_fn: Optional Function(entity_id) → degree (int).
            When provided, dampens activation through high-degree hub nodes
            by dividing by log2(degree). Prevents hub nodes from flooding
            activation to the entire graph.

    Returns:
        Graph-based spreading activation score.
    """
    if not memory_entity_ids or not context_entity_ids:
        return 0.0

    mem_set = set(memory_entity_ids)

    # Propagate activation from each context entity through the graph
    # activation_at[entity_id] = max activation reaching that entity
    activation_at: dict[str, float] = {}

    for ctx_id in context_entity_ids:
        # IDF weight for this context entity (rare entities get more activation)
        idf_weight = entity_idf.get(ctx_id, 1.0) if entity_idf else 1.0
        initial_activation = source_activation * idf_weight / len(context_entity_ids)

        # BFS with decay
        current_activation: dict[str, float] = {ctx_id: initial_activation}

        for _hop in range(max_hops):
            next_activation: dict[str, float] = {}
            for entity_id, act_val in current_activation.items():
                if act_val < 0.001:  # Prune negligible activations
                    continue
                # Get neighbors with edge weights
                neighbors = neighbor_fn(entity_id)  # type: ignore[operator]
                if not neighbors:
                    continue

                # Hub dampening: divide activation by log2(degree) for
                # high-degree nodes. A node with degree 2948 gets dampened
                # by ~11.5x, preventing it from flooding the graph.
                # Nodes with degree ≤ 4 are undampened (log2(4) = 2, min clamp).
                dampen = 1.0
                if degree_fn is not None:
                    degree = degree_fn(entity_id)  # type: ignore[operator]
                    if degree > 4:
                        dampen = 1.0 / math.log2(degree)

                for neighbor_id, edge_weight in neighbors:
                    propagated = act_val * hop_decay * edge_weight * dampen
                    if propagated > next_activation.get(neighbor_id, 0.0):
                        next_activation[neighbor_id] = propagated
            # Merge: take max activation at each node
            for nid, act_val in next_activation.items():
                if act_val > activation_at.get(nid, 0.0):
                    activation_at[nid] = act_val
            current_activation = next_activation

        # Direct match (0 hops) — context entity is in memory
        if ctx_id in mem_set:
            direct = initial_activation
            if direct > activation_at.get(ctx_id, 0.0):
                activation_at[ctx_id] = direct

    # Sum activation that reached memory entities, weighted by IDF
    total = 0.0
    for mem_id in mem_set:
        act_val = activation_at.get(mem_id, 0.0)
        if act_val > 0:
            mem_idf = entity_idf.get(mem_id, 1.0) if entity_idf else 1.0
            total += act_val * mem_idf

    # Normalize by union size (Jaccard-style) to keep scores comparable
    union_size = len(mem_set | set(context_entity_ids))
    return total / max(union_size, 1)


def ppr_graph_score(
    memory_entity_ids: list[str],
    ppr_scores: dict[str, float],
    entity_idf: dict[str, float] | None = None,
) -> float:
    """Compute graph score from Personalized PageRank entity scores.

    Maps PPR entity-level scores to a memory-level score by **mean-pooling**
    the IDF-weighted PPR scores across the memory's entities. Mean-pooling
    (vs. sum) prevents memories with many entities from getting inflated
    scores just because they have more entity links.

    PPR scores should be max-normalized to [0, 1] before calling this.

    Args:
        memory_entity_ids: Entity IDs linked to the candidate memory.
        ppr_scores: PPR result dict from personalized_pagerank().
        entity_idf: Optional IDF weights (rare entities get more weight).

    Returns:
        Graph score for the memory (higher = more relevant to query).
    """
    if not memory_entity_ids or not ppr_scores:
        return 0.0

    total = 0.0
    count = 0
    for eid in memory_entity_ids:
        ppr_val = ppr_scores.get(eid, 0.0)
        if ppr_val > 0:
            idf_w = entity_idf.get(eid, 1.0) if entity_idf else 1.0
            total += ppr_val * idf_w
            count += 1

    return total / max(count, 1)


def activation_noise(sigma: float = 0.25) -> float:
    """Generate logistic noise for activation (ACT-R :ans parameter).

    Args:
        sigma: Noise scale parameter. 0 for deterministic.
    """
    if sigma <= 0:
        return 0.0
    # Logistic noise with scale = sigma * pi / sqrt(3)
    s = sigma * math.pi / math.sqrt(3)
    u = random.random()
    # Clamp to avoid log(0)
    u = max(1e-10, min(1 - 1e-10, u))
    return s * math.log(u / (1 - u))


def total_activation(
    base_level: float,
    spreading: float = 0.0,
    noise: float = 0.0,
    mismatch_penalty: float = 0.0,
) -> float:
    """Compute total activation A_i = B_i + S_i + noise - penalty."""
    return base_level + spreading + noise - mismatch_penalty


def retrieval_probability(activation: float, threshold: float = -2.0, tau: float = 0.4) -> float:
    """Compute retrieval probability from activation level.

    Args:
        activation: Total activation A_i.
        threshold: Retrieval threshold. Memories below this are not retrieved.
        tau: Temperature parameter controlling sharpness of the cutoff.

    Returns:
        P_i = 1 / (1 + exp(-(A_i - threshold) / tau))
    """
    x = (activation - threshold) / tau if tau > 0 else activation - threshold
    # Clamp to prevent overflow
    x = max(-500, min(500, x))
    return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# Recency Scoring (temporal preference for recent memories)
# ---------------------------------------------------------------------------


def recency_score(
    created_age_seconds: float,
    half_life_days: float = 30.0,
) -> float:
    """Compute a recency score that decays over time.

    Uses exponential decay: score = exp(-lambda * age_days)
    where lambda = ln(2) / half_life_days.

    A memory created today gets score ~1.0.
    A memory half_life_days old gets score ~0.5.
    A memory 3x half_life old gets score ~0.125.

    Args:
        created_age_seconds: Time in seconds since memory was created.
            Must be non-negative.
        half_life_days: Days until the recency score halves (default 30).

    Returns:
        Recency score in (0.0, 1.0].
    """
    if created_age_seconds <= 0:
        return 1.0
    age_days = created_age_seconds / 86400.0
    decay_rate = math.log(2) / max(half_life_days, 0.01)
    return math.exp(-decay_rate * age_days)


# ---------------------------------------------------------------------------
# State Reconciliation Penalties (Phase 2C — NCMS-Next §9.4)
# ---------------------------------------------------------------------------


def supersession_penalty(is_superseded: bool, penalty: float = 0.3) -> float:
    """Return mismatch penalty for superseded memories.

    Superseded memories (is_current=False with a SUPERSEDED_BY edge)
    receive a fixed penalty that reduces their total activation score,
    making current information rank higher than stale facts.
    """
    return penalty if is_superseded else 0.0


def conflict_annotation_penalty(has_conflicts: bool, penalty: float = 0.15) -> float:
    """Return mismatch penalty for memories with unresolved conflicts.

    Memories with CONFLICTS_WITH edges receive a smaller penalty
    to signal reduced trustworthiness while keeping them visible
    for human review.
    """
    return penalty if has_conflicts else 0.0


# ---------------------------------------------------------------------------
# Intent-Aware Retrieval Bonus (Phase 4 — NCMS-Next §13)
# ---------------------------------------------------------------------------


def hierarchy_match_bonus(
    candidate_node_types: list[str],
    target_node_types: tuple[str, ...],
    bonus: float = 0.5,
) -> float:
    """Return a scoring bonus when a candidate's node types match intent targets.

    If any of the candidate's node types appear in the intent's target list,
    the full bonus is applied.  This boosts results of the "right" type for
    the classified intent without penalising unmatched types.

    Args:
        candidate_node_types: NodeType values for the candidate memory's nodes.
        target_node_types: NodeType values the classified intent targets.
        bonus: Maximum bonus value (default 0.5).

    Returns:
        The bonus if there is a match, 0.0 otherwise.
    """
    if not candidate_node_types or not target_node_types:
        return 0.0
    target_set = set(target_node_types)
    for nt in candidate_node_types:
        if nt in target_set:
            return bonus
    return 0.0


def role_grounding_bonus(
    role_spans: list[dict] | None,
    query_canonicals: set[str] | frozenset[str] | None,
    primary_bonus: float = 0.5,
) -> float:
    """Reward memories where a query entity appears as a primary-role span.

    The 5-head SLM emits a per-span role label drawn from
    {``primary``, ``alternative``, ``casual``, ``not_relevant``} for
    every gazetteer-detected span in a memory's content.  The
    semantics:

      - ``primary`` — this memory is genuinely *about* this entity
        ("I use Postgres for the database" → Postgres is primary).
      - ``alternative`` — explicit alternative ("I switched from
        MySQL" → MySQL is alternative).
      - ``casual`` — incidental mention ("while configuring Redis I
        noticed Postgres was slow" → Redis is casual).
      - ``not_relevant`` — string-matched but not about it.

    At retrieval time, when a query mentions entity X, a memory
    where X is the *primary* role is a higher-confidence match than
    a memory that merely string-matches X.  This grounds retrieval
    in the SLM's per-span semantic typing rather than relying on
    BM25 / SPLADE alone, which can't tell apart "about X" from
    "happened to mention X".

    Args:
        role_spans: The memory's ``intent_slot.role_spans`` list.
            Each entry is a dict with ``canonical`` (the canonical
            entity form) and ``role`` (one of the four role labels).
            Empty list / ``None`` → no SLM signal, return 0.0.
        query_canonicals: Lowercased canonical strings for the
            query's extracted entities.  Empty / ``None`` → no
            grounding signal, return 0.0.
        primary_bonus: Bonus value applied on a primary-role match.

    Returns:
        ``primary_bonus`` if any role_span has ``role="primary"`` and
        a canonical that appears in ``query_canonicals``; ``0.0``
        otherwise.

    Notes:
        Conservative-by-design: only PRIMARY matches earn a bonus.
        Penalising casual / not_relevant matches is a separate
        decision (see Phase H.3 ablation) — start with the positive
        signal and verify it lifts before introducing penalties.
    """
    if not role_spans or not query_canonicals:
        return 0.0
    qcanon_lower = {c.lower() for c in query_canonicals if c}
    if not qcanon_lower:
        return 0.0
    for span in role_spans:
        if not isinstance(span, dict):
            continue
        if span.get("role") != "primary":
            continue
        canonical = span.get("canonical")
        if (
            isinstance(canonical, str)
            and canonical.lower() in qcanon_lower
        ):
            return primary_bonus
    return 0.0


def intent_alignment_bonus(
    memory_intent: str | None,
    aligned_intents: frozenset[str] | set[str] | tuple[str, ...] | None,
    bonus: float = 0.5,
) -> float:
    """Return a scoring bonus when a memory's preference-intent label aligns
    with the classified query intent.

    The 5-head SLM emits a per-memory ``intent`` label drawn from the
    ``INTENT_CATEGORIES`` taxonomy (positive / negative / habitual /
    difficulty / choice / none).  At retrieval time, the BM25 exemplar
    classifier emits a ``QueryIntent``.  Some query intents are a clean
    semantic match for specific memory-intent labels — for example, a
    PATTERN_LOOKUP query ("what do I usually do?") is exactly what a
    memory labelled ``intent="habitual"`` was extracted to support.

    The mapping itself lives at the call site (``ScoringPipeline``);
    this function is the pure scoring primitive: given the memory's
    intent label and the set of labels the query intent considers
    aligned, apply the full bonus on match, zero otherwise.

    Args:
        memory_intent: The ``intent_slot.intent`` value baked into
            ``memory.structured`` at ingest time.  May be ``None``
            (heuristic fallback chain emits no label, or the SLM
            output was below the confidence floor).
        aligned_intents: The set / tuple of memory-intent labels the
            query intent considers a match.  ``None`` or empty means
            this query intent has no alignment rule, return 0.0.
        bonus: Maximum bonus value applied on match.  Default 0.5
            mirrors :func:`hierarchy_match_bonus` so the two intent-
            aware bonuses share a calibration scale.

    Returns:
        ``bonus`` if ``memory_intent in aligned_intents``, else 0.0.
    """
    if not memory_intent or not aligned_intents:
        return 0.0
    if memory_intent in aligned_intents:
        return bonus
    return 0.0


# ---------------------------------------------------------------------------
# Admission Scoring (Phase 1 — NCMS-Next §8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionFeatures:
    """Feature vector for memory admission scoring.

    Each feature is a normalized float in [0, 1].
    Pure text heuristics — no index or LLM dependency.
    """

    utility: float = 0.0
    temporal_salience: float = 0.0
    persistence: float = 0.0
    state_change_signal: float = 0.0


def score_admission(f: AdmissionFeatures) -> float:
    """Compute weighted admission score from 4 active features.

    Returns a float in approximately [0, 1].  Higher = more worthy of storage.
    Pure text heuristics — no index dependency (BM25/SPLADE not needed).

    Weights sum to ~1.0:
        utility (0.30)             — actionable content markers
        persistence (0.25)         — durable vs transient indicators
        state_change_signal (0.25) — entity state mutation verbs
        temporal_salience (0.20)   — dates, versions, temporal markers
    """
    return (
        0.30 * f.utility
        + 0.25 * f.persistence
        + 0.25 * f.state_change_signal
        + 0.20 * f.temporal_salience
    )


def route_memory(f: AdmissionFeatures, score: float) -> str:
    """Quality gate for memory persistence.

    Returns one of:
        ``"discard"``            — score too low, not worth keeping
        ``"ephemeral_cache"``    — useful but low persistence, TTL-based
        ``"persist"``            — passes quality gate, create L1 atomic node

    Routing is monotonic: higher score = more likely to persist.
    State change signal >= 0.35 promotes to persist regardless of score
    (entity state transitions must be captured for reconciliation).
    """
    # State change signal promotes to persist regardless of score —
    # entity state transitions must be captured for reconciliation.
    # (state_change_signal is a text heuristic, valid at admission time)
    if f.state_change_signal >= 0.35:
        return "persist"
    # Monotonic thresholds: low → discard, mid → ephemeral, high → persist
    # Calibrated on 4-feature scoring (utility, persistence, temporal, state_change)
    if score < 0.10:
        return "discard"
    if score < 0.25:
        return "ephemeral_cache"
    return "persist"
