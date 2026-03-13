"""ACT-R inspired activation scoring and admission scoring for memory retrieval.

Pure mathematical functions with no I/O dependencies.

ACT-R activation (Anderson 2007):
    A_i = B_i + S_i + noise
    B_i = ln(sum_j(t_j^(-d)))           base-level learning
    S_i = sum_k(W_k * S_ki)             spreading activation
    P_i = 1 / (1 + exp(-A_i / tau))     retrieval probability

Admission scoring (NCMS-Next §8):
    AdmissionScore = weighted sum of 8 feature dimensions
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
    """Compute spreading activation from context to memory via shared entities.

    Simple overlap model: activation spreads from context entities to memory
    entities that match. Association strengths can optionally weight connections.

    Args:
        memory_entity_ids: Entity IDs linked to the candidate memory.
        context_entity_ids: Entity IDs active in the current query context.
        association_strengths: Optional weights for (context, memory) entity pairs.
        source_activation: Total activation available to spread.

    Returns:
        Spreading activation S_i.
    """
    if not memory_entity_ids or not context_entity_ids:
        return 0.0

    overlap = set(memory_entity_ids) & set(context_entity_ids)
    if not overlap:
        return 0.0

    # Distribute source activation equally across context elements
    w_j = source_activation / len(context_entity_ids)

    total = 0.0
    for entity_id in overlap:
        if association_strengths:
            # Use explicit association strength if available
            for ctx_id in context_entity_ids:
                s = association_strengths.get((ctx_id, entity_id), 0.0)
                total += w_j * s
        else:
            # Default: each overlap contributes proportionally
            total += w_j

    return total


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
# Admission Scoring (Phase 1 — NCMS-Next §8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionFeatures:
    """Feature vector for memory admission scoring.

    Each feature is a normalized float in [0, 1].
    """

    novelty: float = 0.0
    utility: float = 0.0
    reliability: float = 0.0
    temporal_salience: float = 0.0
    persistence: float = 0.0
    redundancy: float = 0.0
    episode_affinity: float = 0.0
    state_change_signal: float = 0.0


# Weights from NCMS-Next design spec §8.3
ADMISSION_WEIGHTS: dict[str, float] = {
    "novelty": 0.20,
    "utility": 0.18,
    "reliability": 0.12,
    "temporal_salience": 0.12,
    "persistence": 0.15,
    "redundancy": -0.15,  # negative — penalizes redundancy
    "episode_affinity": 0.04,
    "state_change_signal": 0.14,
}


def score_admission(f: AdmissionFeatures) -> float:
    """Compute weighted admission score from feature vector.

    Returns a float in approximately [0, 1].  Higher = more worthy of storage.
    The formula directly implements NCMS-Next §8.3.
    """
    return (
        0.20 * f.novelty
        + 0.18 * f.utility
        + 0.12 * f.reliability
        + 0.12 * f.temporal_salience
        + 0.15 * f.persistence
        - 0.15 * f.redundancy
        + 0.04 * f.episode_affinity
        + 0.14 * f.state_change_signal
    )


def route_memory(f: AdmissionFeatures, score: float) -> str:
    """Determine storage route from features and admission score.

    Returns one of:
        ``"discard"``            — score too low, not worth keeping
        ``"ephemeral_cache"``    — useful but low persistence, TTL-based
        ``"atomic_memory"``      — standard persistent memory
        ``"entity_state_update"``— state change for a tracked entity
        ``"episode_fragment"``   — fragment belonging to an open episode

    Routing policy from NCMS-Next §8.4.
    """
    if score < 0.25 and f.persistence < 0.20 and f.state_change_signal < 0.20:
        return "discard"
    if f.state_change_signal >= 0.50:
        return "entity_state_update"
    if f.episode_affinity >= 0.55:
        return "episode_fragment"
    if 0.25 <= score < 0.45:
        return "ephemeral_cache"
    return "atomic_memory"
