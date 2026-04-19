"""Ablation study — measure the value of each TLG component.

Runs the full test matrix (positive + adversarial) with each
component independently disabled.  Reports the delta in rank-1 and
confidently-wrong rate from the full-TLG baseline.

Components studied:

* **-aliases**: alias table empty (no initials-based expansion).
  Tests JWT ↔ JSON Web Tokens robustness.
* **-distinctiveness**: Layer 2 marker filter removed (accept all
  mined verbs even when they appear in multiple transition buckets).
  Tests whether ambiguous verbs cause misrouting.
* **-slot_rejection**: cause_of matcher stops rejecting domain-noun
  targets.  Tests whether unknown-entity queries become confidently-
  wrong.
* **-cache**: shape cache disabled (every query runs productions
  from scratch).  Tests cache's contribution to consistency.
* **-alias_retirement**: retirement_memory ignores aliases.  Tests
  alias-dependent retirement lookups.

Each ablation runs the three suites (positive-hand, adversarial,
LongMemEval-taxonomy) and reports delta from baseline.
"""

from __future__ import annotations

import contextlib


@contextlib.contextmanager
def ablate_aliases():
    """Temporarily empty the alias table."""
    from experiments.temporal_trajectory import aliases
    orig = aliases.ALIASES
    aliases.ALIASES = {}
    try:
        yield
    finally:
        aliases.ALIASES = orig


@contextlib.contextmanager
def ablate_distinctiveness():
    """Disable Layer 2 distinctiveness filter — keep ALL mined verbs
    in both buckets."""
    from experiments.temporal_trajectory import edge_markers
    orig = edge_markers.MARKERS
    from collections import Counter, defaultdict
    from experiments.temporal_trajectory.corpus import ADR_CORPUS, EDGES
    # Rebuild markers WITHOUT distinctiveness filter.
    by_id = {m.mid: m for m in ADR_CORPUS}
    accum: dict[str, Counter] = defaultdict(Counter)
    for edge in EDGES:
        dst_mem = by_id.get(edge.dst)
        if dst_mem is None:
            continue
        for h in edge_markers._extract_shape_matches(dst_mem.content):
            accum[edge.transition][h] += 1
    markers_no_filter = {
        t: frozenset(c.keys()) for t, c in accum.items()
    }
    edge_markers.MARKERS = edge_markers.EdgeMarkers(markers=markers_no_filter)
    try:
        yield
    finally:
        edge_markers.MARKERS = orig


@contextlib.contextmanager
def ablate_slot_rejection():
    """Force cause_of matcher to accept ALL targets including domain
    nouns (baseline before our guard was added)."""
    from experiments.temporal_trajectory import query_parser
    orig = query_parser._DOMAIN_NOUNS
    query_parser._DOMAIN_NOUNS = frozenset()
    try:
        yield
    finally:
        query_parser._DOMAIN_NOUNS = orig


@contextlib.contextmanager
def ablate_cache():
    """Clear shape cache; every query runs productions from scratch."""
    from experiments.temporal_trajectory import shape_cache
    orig = shape_cache.GLOBAL_CACHE
    shape_cache.GLOBAL_CACHE = shape_cache.QueryShapeCache()
    try:
        yield
    finally:
        shape_cache.GLOBAL_CACHE = orig


def _run_positive_hand() -> tuple[int, int, int]:
    """Return (rank1, top5, wrong) over the 32-query hand corpus."""
    from experiments.temporal_trajectory.queries import QUERIES
    from experiments.temporal_trajectory.retrievers import (
        _build_bm25_index,
    )
    from experiments.temporal_trajectory.lg_retriever import retrieve_lg

    engine = _build_bm25_index()
    rank1 = top5 = wrong = 0
    for q in QUERIES:
        bm25 = list(engine.search(q.text, limit=20))
        ranked, trace = retrieve_lg(q.text, bm25)
        top_mids = [mid for mid, _ in ranked[:5]]
        top1_mid = top_mids[0] if top_mids else None
        if top1_mid == q.gold_mid:
            rank1 += 1
        if q.gold_mid in top_mids:
            top5 += 1
        # confidently-wrong = grammar asserted rank-1 but got it wrong
        if trace.has_confident_answer() and trace.grammar_answer != q.gold_mid:
            wrong += 1
    return rank1, top5, wrong


def _run_adversarial() -> tuple[int, int]:
    """Return (correct, confidently_wrong_on_abstain) for the 15-query adversarial suite.

    correct = expected-answer got + expected-abstain abstained.
    confidently_wrong_on_abstain = grammar returned a confident answer
    on a query that was meant to abstain.
    """
    from experiments.temporal_trajectory.adversarial import ADVERSARIAL
    from experiments.temporal_trajectory.retrievers import (
        _build_bm25_index,
    )
    from experiments.temporal_trajectory.lg_retriever import retrieve_lg

    engine = _build_bm25_index()
    correct = cw = 0
    for aq in ADVERSARIAL:
        bm25 = list(engine.search(aq.text, limit=20))
        _, trace = retrieve_lg(aq.text, bm25)
        confident = trace.has_confident_answer()
        if aq.expected_mode in ("answer", "alias"):
            if confident and trace.grammar_answer == aq.gold_mid:
                correct += 1
        elif aq.expected_mode == "abstain":
            if not confident:
                correct += 1
            else:
                cw += 1     # confidently-wrong — critical failure
    return correct, cw


def _run_taxonomy() -> int:
    """Return taxonomy coverage count out of 15."""
    from experiments.temporal_trajectory.longmemeval_subset import (
        CURATED,
    )
    from experiments.temporal_trajectory.query_parser import analyze_query

    correct = 0
    for q in CURATED:
        qs = analyze_query(q.text)
        ok = (
            qs.intent in q.expected_intents
            or (q.acceptable_abstain and qs.intent == "none")
        )
        if ok:
            correct += 1
    return correct


def _baseline() -> dict:
    r, t, w = _run_positive_hand()
    adv_ok, adv_cw = _run_adversarial()
    tax = _run_taxonomy()
    return {
        "positive_rank1": r, "positive_top5": t, "positive_wrong": w,
        "adversarial_correct": adv_ok,
        "adversarial_confidently_wrong": adv_cw,
        "taxonomy_correct": tax,
    }


def _delta(base: dict, abl: dict) -> dict:
    return {k: abl[k] - base[k] for k in base}


def main() -> None:
    # Fresh baseline.
    base = _baseline()
    print("TLG ablation study")
    print("=" * 70)
    print(f"Baseline (full TLG): {base}")
    print()
    print(f"{'Ablation':<22} {'Δpos_r1':>9} {'Δpos_t5':>9} "
          f"{'Δpos_cw':>9} {'Δadv_cor':>10} {'Δadv_cw':>10} {'Δtax':>6}")
    print("-" * 80)

    # Each ablation: yield within the context manager, measure.
    ablations = [
        ("-aliases", ablate_aliases),
        ("-distinctiveness", ablate_distinctiveness),
        ("-slot_rejection", ablate_slot_rejection),
        ("-cache", ablate_cache),
    ]

    for name, ctx in ablations:
        with ctx():
            abl = _baseline()
        delta = _delta(base, abl)
        print(
            f"{name:<22} "
            f"{delta['positive_rank1']:>+9} "
            f"{delta['positive_top5']:>+9} "
            f"{delta['positive_wrong']:>+9} "
            f"{delta['adversarial_correct']:>+10} "
            f"{delta['adversarial_confidently_wrong']:>+10} "
            f"{delta['taxonomy_correct']:>+6}"
        )

    print()
    print("Legend")
    print("  Δpos_r1  = Δ positive-suite rank-1 (−32 worst, +0 neutral)")
    print("  Δpos_t5  = Δ positive-suite top-5")
    print("  Δpos_cw  = Δ confidently-wrong rank-1 on positive suite")
    print("              (should stay ≤ 0 — ablations CAN'T be better-than-baseline)")
    print("  Δadv_cor = Δ adversarial correct (out of 15)")
    print("  Δadv_cw  = Δ adversarial confidently-wrong "
          "(→ integration risk)")
    print("  Δtax     = Δ taxonomy coverage (out of 15)")


if __name__ == "__main__":
    main()
