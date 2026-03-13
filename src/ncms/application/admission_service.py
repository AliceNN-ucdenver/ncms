"""Admission Service — computes features and routes incoming content.

Evaluates whether new information should be discarded, cached ephemerally,
stored as atomic memory, routed as an entity state update, or attached to
an open episode.  All 8 features are heuristic-based (no LLM required).

Feature-flagged via ``config.admission_enabled`` (default False).
"""

from __future__ import annotations

import logging
import re

from ncms.config import NCMSConfig
from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
from ncms.domain.scoring import AdmissionFeatures, route_memory, score_admission

logger = logging.getLogger(__name__)

# ── Keyword Lexicons ──────────────────────────────────────────────────────

_DECISION_MARKERS: frozenset[str] = frozenset({
    "decided", "decision", "chose", "chosen", "selected", "approved",
    "rejected", "agreed", "concluded", "resolved",
})

_CHANGE_MARKERS: frozenset[str] = frozenset({
    "changed", "updated", "migrated", "deployed", "released", "reverted",
    "rolled back", "rollback", "upgraded", "downgraded", "patched",
    "hotfix", "hotfixed",
})

_INCIDENT_MARKERS: frozenset[str] = frozenset({
    "error", "bug", "incident", "outage", "failure", "failed", "crashed",
    "fix", "fixed", "broken", "regression", "alert", "exception",
})

_ARCHITECTURE_MARKERS: frozenset[str] = frozenset({
    "architecture", "architectural", "design", "pattern", "convention",
    "constraint", "principle", "standard", "guideline", "policy",
})

_UTILITY_MARKERS: frozenset[str] = (
    _DECISION_MARKERS | _CHANGE_MARKERS | _INCIDENT_MARKERS | _ARCHITECTURE_MARKERS
)

_TEMPORAL_MARKERS: frozenset[str] = frozenset({
    "now", "currently", "since", "as of", "starting", "until",
    "effective", "beginning", "from now", "going forward",
})

_TEMPORAL_VERBS: frozenset[str] = frozenset({
    "changed", "updated", "released", "deprecated", "migrated",
    "fixed", "deployed", "removed", "added", "created",
})

_PERSISTENCE_HIGH: frozenset[str] = frozenset({
    "policy", "decision", "architectural", "architecture", "principle",
    "standard", "convention", "constraint", "guideline", "requirement",
    "rule", "always", "never", "must",
})

_PERSISTENCE_LOW: frozenset[str] = frozenset({
    "todo", "wip", "draft", "temp", "temporary", "hack", "workaround",
    "quick fix", "placeholder", "experimenting", "trying",
})

_HEDGING: frozenset[str] = frozenset({
    "maybe", "might", "possibly", "perhaps", "i think", "not sure",
    "unclear", "seems like", "could be", "probably",
})

_STATE_CHANGE_VERBS: frozenset[str] = frozenset({
    "changed", "updated", "now", "switched", "moved", "set to",
    "became", "is now", "transitioned",
})

# Date patterns
_DATE_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DATE_INFORMAL = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{4}\b",
    re.IGNORECASE,
)
_VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")


def _count_matches(text_lower: str, markers: frozenset[str]) -> int:
    """Count how many markers appear in the lowered text."""
    return sum(1 for m in markers if m in text_lower)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class AdmissionService:
    """Computes admission features and routes incoming content.

    All feature extractors are synchronous heuristics except novelty/redundancy
    which use the BM25 index.
    """

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine,
        graph: GraphEngine,
        config: NCMSConfig | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()

    # ── Feature Extractors ────────────────────────────────────────────────

    def _compute_novelty(
        self, bm25_results: list[tuple[str, float]]
    ) -> float:
        """Novelty = 1 - max_similarity.  Empty results → full novelty."""
        if not bm25_results:
            return 1.0
        # BM25 scores are unbounded; normalize via sigmoid-like transform.
        # Tantivy BM25 scores are typically 0.5-3 for partial matches, 1-5 for
        # good matches. A divisor of 2.0 maps score=1→sim=0.33, 2→0.5, 5→0.71.
        best_score = max(s for _, s in bm25_results)
        if best_score <= 0:
            return 1.0
        similarity = 1.0 - 1.0 / (1.0 + best_score / 2.0)
        return _clamp(1.0 - similarity)

    def _compute_utility(self, text_lower: str) -> float:
        """Utility = density of actionable/valuable markers."""
        matches = _count_matches(text_lower, _UTILITY_MARKERS)
        if matches == 0:
            return 0.05
        # Diminishing returns: 1 match → 0.35, 2 → 0.55, 3 → 0.70, 5+ → ~0.90
        return _clamp(0.15 + 0.20 * matches / (1.0 + 0.15 * matches))

    def _compute_reliability(
        self, text_lower: str, source_type: str | None = None
    ) -> float:
        """Reliability based on source type and hedging language."""
        base = 0.60  # default "observed"
        if source_type in ("system", "authoritative"):
            base = 0.90
        elif source_type == "speculative":
            base = 0.30
        # Penalize hedging language
        hedge_count = _count_matches(text_lower, _HEDGING)
        penalty = min(0.30, hedge_count * 0.10)
        return _clamp(base - penalty)

    def _compute_temporal_salience(self, text_lower: str, text: str) -> float:
        """Temporal salience from dates, temporal markers, and change verbs."""
        score = 0.0
        # ISO dates or informal dates
        if _DATE_ISO.search(text) or _DATE_INFORMAL.search(text):
            score += 0.40
        # Temporal markers
        marker_count = _count_matches(text_lower, _TEMPORAL_MARKERS)
        score += min(0.30, marker_count * 0.15)
        # Temporal verbs
        verb_count = _count_matches(text_lower, _TEMPORAL_VERBS)
        score += min(0.30, verb_count * 0.10)
        return _clamp(score)

    def _compute_persistence(self, text_lower: str) -> float:
        """Persistence = durability of the information."""
        high_count = _count_matches(text_lower, _PERSISTENCE_HIGH)
        low_count = _count_matches(text_lower, _PERSISTENCE_LOW)
        if high_count > 0 and low_count == 0:
            return _clamp(0.60 + 0.10 * high_count)
        if low_count > 0 and high_count == 0:
            return _clamp(0.20 - 0.05 * low_count)
        # Mixed or neither
        return _clamp(0.40 + 0.05 * (high_count - low_count))

    def _compute_redundancy(
        self, bm25_results: list[tuple[str, float]]
    ) -> float:
        """Redundancy = overlap with existing memories (high BM25 = high overlap)."""
        if not bm25_results:
            return 0.0
        best_score = max(s for _, s in bm25_results)
        if best_score <= 0:
            return 0.0
        # Same sigmoid mapping as novelty but not inverted
        similarity = 1.0 - 1.0 / (1.0 + best_score / 2.0)
        return _clamp(similarity)

    def _compute_state_change_signal(self, text_lower: str) -> float:
        """State change signal from entity state mutation indicators."""
        score = 0.0
        # State change verbs
        verb_count = _count_matches(text_lower, _STATE_CHANGE_VERBS)
        score += min(0.40, verb_count * 0.15)
        # Version patterns (v1.2.3 style)
        if _VERSION_PATTERN.search(text_lower):
            score += 0.20
        # "status" or "state" words near change indicators
        if ("status" in text_lower or "state" in text_lower) and verb_count > 0:
            score += 0.25
        return _clamp(score)

    def _compute_episode_affinity(self) -> float:
        """Episode affinity — stub for Phase 1 (no episodes exist yet)."""
        # In Phase 2+, this will check for open episodes with overlapping entities
        return 0.0

    # ── Orchestrator ──────────────────────────────────────────────────────

    async def compute_features(
        self,
        content: str,
        domains: list[str] | None = None,
        source_agent: str | None = None,
        source_type: str | None = None,
    ) -> AdmissionFeatures:
        """Compute all 8 admission features for incoming content.

        Args:
            content: The text content to evaluate.
            domains: Domain tags for the content.
            source_agent: Agent that produced this content.
            source_type: Trust level indicator ("system", "authoritative", "speculative").

        Returns:
            AdmissionFeatures with all 8 normalized feature scores.
        """
        search_limit = self._config.admission_novelty_search_limit
        # BM25 search — shared by novelty + redundancy
        bm25_results = self._index.search(content, limit=search_limit)

        text_lower = content.lower()

        return AdmissionFeatures(
            novelty=self._compute_novelty(bm25_results),
            utility=self._compute_utility(text_lower),
            reliability=self._compute_reliability(text_lower, source_type),
            temporal_salience=self._compute_temporal_salience(text_lower, content),
            persistence=self._compute_persistence(text_lower),
            redundancy=self._compute_redundancy(bm25_results),
            episode_affinity=self._compute_episode_affinity(),
            state_change_signal=self._compute_state_change_signal(text_lower),
        )

    async def evaluate(
        self,
        content: str,
        domains: list[str] | None = None,
        source_agent: str | None = None,
        source_type: str | None = None,
    ) -> tuple[AdmissionFeatures, float, str]:
        """Compute features, score, and route in one call.

        Returns:
            (features, admission_score, route) tuple.
        """
        features = await self.compute_features(
            content, domains=domains, source_agent=source_agent, source_type=source_type
        )
        admission_score = score_admission(features)
        route = route_memory(features, admission_score)

        logger.debug(
            "Admission: score=%.3f route=%s features=%s",
            admission_score, route, features,
        )

        return features, admission_score, route
