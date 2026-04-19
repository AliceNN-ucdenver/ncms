"""Four retrieval strategies to compare on the trajectory experiment.

* **A — BM25 only.** Pure baseline.  Uses the same Tantivy engine NCMS
  uses in production, in-memory.
* **B — BM25 + observed_at sort (naive ordinal rerank).** The retired
  P1b-v1.  Included to prove it regresses here too.
* **C — BM25 + entity-scoped ordinal (Phase B primitive).** Ported
  inline so the experiment doesn't depend on importing NCMS
  internals.  Picks "end of the chain" = latest subject-linked memory
  when query has ordinal-last intent; otherwise BM25 order.
* **D — BM25 + path rerank.** The new concept.  Builds a DAG of
  candidates ordered by ``observed_at``, connects nodes with an edge
  when they share ≥1 entity within an N-memory window, and scores
  each candidate by its position in the longest path through the
  graph.

All four return ``list[tuple[mid, score]]`` in descending rank order.
They share the same corpus + same pre-labeled entities, so the
comparison isolates the rerank strategy from extraction noise.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass

from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.domain.models import Memory as NcmsMemory

from experiments.temporal_trajectory.corpus import ADR_CORPUS, Memory


# ── Shared BM25 infrastructure ──────────────────────────────────────

def _build_bm25_index() -> TantivyEngine:
    """Index the corpus into a fresh Tantivy in-memory engine.

    Returns an engine ready for ``.search(query, top_k)`` calls.  The
    engine is shared across all retrievers that need BM25.
    """
    # Tantivy wants a temp dir for the in-memory (it writes files
    # under an ephemeral path).  The experiment is stateless — we
    # build a fresh engine per call.
    path = tempfile.mkdtemp(prefix="trajectory_exp_")
    engine = TantivyEngine(path=path)
    engine.initialize(path=path)
    for m in ADR_CORPUS:
        ncms_mem = NcmsMemory(
            id=m.mid,
            content=m.content,
            observed_at=m.observed_at,
        )
        engine.index_memory(ncms_mem)
    return engine


def _bm25_scores(engine: TantivyEngine, query: str) -> list[tuple[str, float]]:
    """Return the full BM25 ranking over the corpus (all 10 ADRs)."""
    return engine.search(query, limit=len(ADR_CORPUS))


# ── Retriever A: BM25 only ──────────────────────────────────────────

def retrieve_bm25(query: str, engine: TantivyEngine) -> list[tuple[str, float]]:
    return _bm25_scores(engine, query)


# ── Retriever B: BM25 + naive date sort ─────────────────────────────

def retrieve_bm25_date(
    query: str,
    engine: TantivyEngine,
    direction: str = "desc",
) -> list[tuple[str, float]]:
    """Retire-mode primitive: sort all BM25 candidates by observed_at.

    ``direction='desc'`` = newest first (models 'latest X' intent).
    Included so the experiment reproduces the retired P1b-v1 failure
    mode on its home turf (ADR evolution).
    """
    ranked = _bm25_scores(engine, query)
    by_id = {m.mid: m for m in ADR_CORPUS}
    sorted_mids = sorted(
        (mid for mid, _ in ranked),
        key=lambda mid: by_id[mid].observed_at,
        reverse=(direction == "desc"),
    )
    return [(mid, 1.0 / (i + 1)) for i, mid in enumerate(sorted_mids)]


# ── Retriever C: entity-scoped ordinal (Phase B) ────────────────────

def retrieve_entity_scoped(
    query: str,
    engine: TantivyEngine,
    subject_entities: frozenset[str],
    ordinal: str = "last",
) -> list[tuple[str, float]]:
    """Phase B ordinal-single primitive, ported inline.

    Candidates that share ≥1 entity with ``subject_entities`` are
    partitioned from the rest.  The subject slice is sorted by
    ``observed_at`` (desc for ``last``, asc for ``first``).  Non-
    subject candidates stay in BM25 order behind.
    """
    ranked = _bm25_scores(engine, query)
    by_id = {m.mid: m for m in ADR_CORPUS}
    subject_slice: list[tuple[str, float]] = []
    other: list[tuple[str, float]] = []
    for mid, score in ranked:
        mem = by_id[mid]
        if mem.entities & subject_entities:
            subject_slice.append((mid, score))
        else:
            other.append((mid, score))
    subject_slice.sort(
        key=lambda x: by_id[x[0]].observed_at,
        reverse=(ordinal == "last"),
    )
    return subject_slice + other


# ── Retriever D: Path rerank ────────────────────────────────────────

@dataclass(frozen=True)
class PathRerankParams:
    """Hyperparameters for the path-rerank.  Defaults chosen to keep
    the experiment honest (no tuning on the test queries)."""

    alpha: float = 0.40       # weight on path length (end-of-chain signal)
    beta: float = 0.10        # weight on path coherence
    gamma: float = 0.30       # penalty on successor count (not end-of-chain)
    min_overlap: int = 2      # stricter: require ≥2 entities for an edge
    window_days: int = 730    # max temporal gap for an edge (2 years)
    # Use rank-based BM25 normalization so path signal has comparable
    # magnitude regardless of absolute BM25 score spread.
    use_rank_bm25: bool = True


def retrieve_path_rerank(
    query: str,
    engine: TantivyEngine,
    subject_entities: frozenset[str],
    params: PathRerankParams = PathRerankParams(),
) -> list[tuple[str, float]]:
    """Path-integrity rerank — the experimental strategy.

    Algorithm:

    1. Take full BM25 ranking.
    2. Partition subject-linked candidates (like Retriever C).  Non-
       subject candidates skip the path computation and stay in BM25
       order at the tail.
    3. Build a DAG over subject-linked candidates:
       * Node per candidate.
       * Edge ``m_i → m_j`` iff
         ``m_i.observed_at < m_j.observed_at`` AND
         ``|m_j.observed_at - m_i.observed_at| <= window_days`` AND
         ``|entities_i ∩ entities_j| >= min_overlap``.
       * Edge weight = ``|entities_i ∩ entities_j| / |entities_i ∪ entities_j|``
         (Jaccard).
    4. For each node, compute:
       * ``path_length`` = longest predecessor-chain length (number
         of hops).
       * ``coherence`` = mean edge weight along that chain.
    5. Final score = ``bm25 + alpha * normalized_path_length +
       beta * coherence``.
    6. Sort subject-linked by final score desc.  Concatenate non-
       subject tail.

    Intuition: a memory that sits at the end of a long entity-
    coherent chronological chain is likely the current state of an
    evolving subject.
    """
    ranked = _bm25_scores(engine, query)
    by_id = {m.mid: m for m in ADR_CORPUS}

    subject: list[tuple[str, float]] = []
    other: list[tuple[str, float]] = []
    for mid, score in ranked:
        mem = by_id[mid]
        if mem.entities & subject_entities:
            subject.append((mid, score))
        else:
            other.append((mid, score))

    if len(subject) < 2:
        return subject + other  # nothing to rerank

    # Build DAG edges.
    edges: dict[str, list[tuple[str, float]]] = {}
    for i_mid, _ in subject:
        m_i = by_id[i_mid]
        for j_mid, _ in subject:
            if i_mid == j_mid:
                continue
            m_j = by_id[j_mid]
            if m_i.observed_at >= m_j.observed_at:
                continue
            if (m_j.observed_at - m_i.observed_at).days > params.window_days:
                continue
            overlap = m_i.entities & m_j.entities
            if len(overlap) < params.min_overlap:
                continue
            union = m_i.entities | m_j.entities
            jaccard = len(overlap) / len(union) if union else 0.0
            edges.setdefault(i_mid, []).append((j_mid, jaccard))

    # Invert: for each node, who are its predecessors?
    predecessors: dict[str, list[tuple[str, float]]] = {}
    for src, succs in edges.items():
        for dst, weight in succs:
            predecessors.setdefault(dst, []).append((src, weight))

    # Longest-path + mean-coherence for each node via topological DP.
    chronological = sorted(
        [mid for mid, _ in subject],
        key=lambda mid: by_id[mid].observed_at,
    )
    best_length: dict[str, int] = {mid: 0 for mid in chronological}
    best_coherence: dict[str, float] = {mid: 0.0 for mid in chronological}
    for mid in chronological:
        preds = predecessors.get(mid, [])
        if not preds:
            continue
        # Best predecessor = the one giving the longest chain.  Tie-
        # break on higher edge weight for coherence.
        best_len_here = 0
        best_coh_here = 0.0
        for pred_mid, w in preds:
            chain_len = best_length[pred_mid] + 1
            coh_sum = (
                best_coherence[pred_mid] * best_length[pred_mid] + w
            )
            coh_mean = (
                coh_sum / chain_len if chain_len > 0 else 0.0
            )
            if chain_len > best_len_here or (
                chain_len == best_len_here and coh_mean > best_coh_here
            ):
                best_len_here = chain_len
                best_coh_here = coh_mean
        best_length[mid] = best_len_here
        best_coherence[mid] = best_coh_here

    # Also reconstruct the best predecessor chain per node so we can
    # print it during analysis (was this the invariant we expected?).
    best_prev: dict[str, str | None] = {mid: None for mid in chronological}
    for mid in chronological:
        preds = predecessors.get(mid, [])
        if not preds:
            continue
        best_len_here = 0
        best_coh_here = 0.0
        best_prev_here: str | None = None
        for pred_mid, w in preds:
            chain_len = best_length[pred_mid] + 1
            coh_sum = (
                best_coherence[pred_mid] * best_length[pred_mid] + w
            )
            coh_mean = (
                coh_sum / chain_len if chain_len > 0 else 0.0
            )
            if chain_len > best_len_here or (
                chain_len == best_len_here and coh_mean > best_coh_here
            ):
                best_len_here = chain_len
                best_coh_here = coh_mean
                best_prev_here = pred_mid
        best_prev[mid] = best_prev_here

    # Count successors for each node — a true end-of-chain has zero.
    successors: dict[str, int] = {mid: 0 for mid in chronological}
    for src, succs in edges.items():
        successors[src] = len(succs)

    max_len = max(best_length.values()) if best_length else 1
    max_len = max(max_len, 1)  # avoid div/0
    max_succ = max(successors.values()) if successors else 1
    max_succ = max(max_succ, 1)

    # Rescore.
    rescored = []
    debug: list[dict] = []  # stashed for introspection
    max_bm25 = max(v for _, v in subject) or 1.0
    subject_rank = {mid: i for i, (mid, _) in enumerate(subject)}
    n_subject = max(len(subject), 1)
    for mid, bm25 in subject:
        length_norm = best_length[mid] / max_len
        coh = best_coherence[mid]
        if params.use_rank_bm25:
            # Linear rank normalization: BM25 rank 1 → 1.0, last → ~0.
            bm25_norm = (n_subject - subject_rank[mid]) / n_subject
        else:
            bm25_norm = bm25 / max_bm25
        succ_penalty = params.gamma * (successors[mid] / max_succ)
        final = (
            bm25_norm
            + params.alpha * length_norm
            + params.beta * coh
            - succ_penalty
        )
        # Reconstruct chain by walking best_prev backward.
        chain: list[str] = [mid]
        cur = mid
        while best_prev[cur] is not None:
            cur = best_prev[cur]  # type: ignore[assignment]
            chain.append(cur)
        chain.reverse()
        rescored.append((mid, final))
        debug.append({
            "mid": mid,
            "bm25": round(bm25, 3),
            "bm25_norm": round(bm25_norm, 3),
            "path_length": best_length[mid],
            "coherence": round(coh, 3),
            "successors": successors[mid],
            "chain": chain,
            "final_score": round(final, 3),
        })

    rescored.sort(key=lambda x: x[1], reverse=True)
    # Stash debug on the function call so run.py can access it.
    retrieve_path_rerank.last_debug = debug  # type: ignore[attr-defined]
    return rescored + other


# ── Common interface ────────────────────────────────────────────────

@dataclass(frozen=True)
class RetrievalTrace:
    strategy: str
    query: str
    top_k: list[str]
    full_ranking: list[tuple[str, float]]


def run_all(
    query: str,
    subject_entities: frozenset[str],
    engine: TantivyEngine,
) -> dict[str, RetrievalTrace]:
    """Run all five strategies on one query.  Returns trace per
    strategy."""
    from experiments.temporal_trajectory.lg_retriever import retrieve_lg

    bm25_full = retrieve_bm25(query, engine)
    lg_ranking, _lg_trace = retrieve_lg(query, bm25_full)
    results = {
        "A_bm25": bm25_full,
        "B_bm25_date": retrieve_bm25_date(query, engine, direction="desc"),
        "C_entity_scoped": retrieve_entity_scoped(
            query, engine, subject_entities, ordinal="last",
        ),
        "D_path_rerank": retrieve_path_rerank(
            query, engine, subject_entities,
        ),
        "E_lg_grammar": lg_ranking,
    }
    return {
        name: RetrievalTrace(
            strategy=name,
            query=query,
            top_k=[mid for mid, _ in ranking[:5]],
            full_ranking=ranking,
        )
        for name, ranking in results.items()
    }
