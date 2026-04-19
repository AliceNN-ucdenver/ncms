"""Synthetic corpus scaling test for TLG.

Generates synthetic corpora at increasing sizes and measures:

* Layer 1 / Layer 2 / alias / domain-noun induction time
* Zone computation time (per subject)
* Property invariant validation time
* Query response time (grammar dispatch)
* Mock reconciliation time

Identifies algorithmic bottlenecks before NCMS integration.
Expected scale ceilings:

* **Alias induction** is O(|entities|²) — worst case.
* **Subject clustering** (mock-ingest only) is O(|memories|²).
* **Zone computation** is O(|M_S| + |E_S|) per subject.
* **Query dispatch** is O(|productions| × avg-regex-cost).

Run::

    uv run python -m experiments.temporal_trajectory.run_scale_test \\
        --scales 100,500,1000,2500,5000

Results go to stdout + JSON at ``--json-out``.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime, timedelta

from experiments.temporal_trajectory.corpus import Memory


_RNG = random.Random(42)


# ── Synthetic corpus generator ─────────────────────────────────────

_VERBS_INTRO = [
    "introduced", "added", "initiated", "launched", "started",
    "kicked off", "began", "performed", "created", "built",
]
_VERBS_SUPER = [
    "retired", "superseded", "replaced", "deprecated", "moved from",
    "decommissioned", "ended", "concluded",
]
_VERBS_REFINE = [
    "extended", "enhanced", "updated", "improved", "refined",
]
_NOUNS = [
    "authentication", "payment", "database", "logging", "caching",
    "rate limiting", "session", "token", "identity", "routing",
    "ingest", "reranking", "indexing", "synthesis", "pipeline",
    "reconciliation", "admission", "storage", "retrieval", "query",
    "scoring", "embedding", "classifier", "extractor", "planner",
]


def _synth_entity() -> str:
    """Random multi-word noun-phrase entity."""
    n = _RNG.randint(1, 2)
    return " ".join(_RNG.choice(_NOUNS) for _ in range(n))


def _synth_content(kind: str, entities: list[str]) -> str:
    """Random plausible content with the chosen verb kind."""
    if kind == "intro":
        verb = _RNG.choice(_VERBS_INTRO)
    elif kind == "super":
        verb = _RNG.choice(_VERBS_SUPER)
    else:
        verb = _RNG.choice(_VERBS_REFINE)
    e = entities[0] if entities else "system"
    return f"System {verb} {e}.  Follow-up details on {' and '.join(entities[:3])}."


def generate_corpus(
    n_memories: int,
    n_subjects: int | None = None,
    edge_density: float = 0.8,
) -> tuple[list[Memory], list]:
    """Generate (memories, edges) of a given size.

    Args:
        n_memories: target memory count.
        n_subjects: number of distinct subjects.  Default = sqrt(n/5)
            so each subject holds ~√(5n) memories on average.
        edge_density: fraction of adjacent same-subject pairs that
            get an edge (supersedes vs refines chosen at random).
    """
    from experiments.temporal_trajectory.corpus import Edge
    if n_subjects is None:
        n_subjects = max(3, int((n_memories / 5) ** 0.5))

    # Generate subjects with unique seed entities.
    subject_entities: dict[str, list[str]] = {}
    for i in range(n_subjects):
        subj = f"subj_{i}"
        seeds = [_synth_entity() for _ in range(_RNG.randint(2, 5))]
        subject_entities[subj] = seeds

    # Distribute memories across subjects.
    memories: list[Memory] = []
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(n_memories):
        subj = f"subj_{i % n_subjects}"
        # entities: 2-4 from subject seed pool + 0-1 random
        seed_pool = subject_entities[subj]
        k = min(_RNG.randint(2, 4), len(seed_pool))
        ents = _RNG.sample(seed_pool, k)
        if _RNG.random() < 0.3:
            ents.append(_synth_entity())
        mid = f"MEM-{i:05d}"
        ents.append(mid)  # include mid for structural correctness
        kind = _RNG.choice(["intro", "super", "refine"])
        content = _synth_content(kind, ents)
        observed = t0 + timedelta(days=i, hours=_RNG.randint(0, 23))
        memories.append(Memory(
            mid=mid,
            content=content,
            observed_at=observed,
            entities=frozenset(ents),
            subject=subj,
        ))

    # Edges: adjacent same-subject memories get typed edges.
    by_subject: dict[str, list[Memory]] = {}
    for m in memories:
        by_subject.setdefault(m.subject, []).append(m)
    edges: list = []
    for subj, mems in by_subject.items():
        mems.sort(key=lambda m: m.observed_at)
        for a, b in zip(mems[:-1], mems[1:], strict=False):
            if _RNG.random() > edge_density:
                continue
            trans = _RNG.choice(["refines", "refines", "supersedes"])
            retires = (
                frozenset(_RNG.sample(
                    list(a.entities - {a.mid} - b.entities),
                    k=min(2, len(a.entities - {a.mid} - b.entities)),
                ))
                if trans == "supersedes" and (a.entities - b.entities)
                else frozenset()
            )
            edges.append(Edge(
                src=a.mid, dst=b.mid,
                transition=trans,
                retires_entities=retires,
            ))

    return memories, edges


# ── Scale measurement ──────────────────────────────────────────────

@dataclass
class ScaleMetrics:
    n_memories: int
    n_subjects: int
    n_edges: int
    layer1_ms: float = 0.0
    layer2_ms: float = 0.0
    aliases_ms: float = 0.0
    domain_nouns_ms: float = 0.0
    zone_compute_ms_per_subject: float = 0.0
    properties_ms: float = 0.0
    mock_reconcile_ms: float = 0.0
    query_dispatch_ms: float = 0.0
    total_unique_entities: int = 0
    aliases_found: int = 0
    layer2_markers: int = 0
    domain_nouns_count: int = 0


def _time_ms(fn):
    """Time a function call, return (result, ms)."""
    t = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t) * 1000


def measure_scale(n_memories: int) -> ScaleMetrics:
    """Measure TLG induction and dispatch times on a synthetic corpus."""
    mems, edges = generate_corpus(n_memories)
    subjects = {m.subject for m in mems}
    metrics = ScaleMetrics(
        n_memories=len(mems),
        n_subjects=len(subjects),
        n_edges=len(edges),
        total_unique_entities=len({e for m in mems for e in m.entities}),
    )

    # Swap global corpus, reload dependent modules.
    from experiments.temporal_trajectory import corpus as _corpus
    orig_mems, orig_edges = list(_corpus.ADR_CORPUS), list(_corpus.EDGES)
    _corpus.ADR_CORPUS = mems
    _corpus.EDGES = edges
    import importlib
    from experiments.temporal_trajectory import (
        aliases,
        edge_markers,
        grammar,
        lg_retriever,
        mock_reconciliation,
        properties,
        query_parser,
        retirement_extractor,
        shape_cache,
        vocab_induction,
    )
    try:
        # ── Layer 1 ────────────────────────────────────────────────
        _, metrics.layer1_ms = _time_ms(
            lambda: importlib.reload(vocab_induction)
        )

        # ── Layer 2 ────────────────────────────────────────────────
        _, metrics.layer2_ms = _time_ms(
            lambda: importlib.reload(edge_markers)
        )
        metrics.layer2_markers = sum(
            len(s) for s in edge_markers.MARKERS.markers.values()
        )

        # ── Aliases ────────────────────────────────────────────────
        _, metrics.aliases_ms = _time_ms(
            lambda: importlib.reload(aliases)
        )
        metrics.aliases_found = sum(len(v) for v in aliases.ALIASES.values())

        # ── Grammar (zones cached lazily) ─────────────────────────
        importlib.reload(grammar)

        # ── Domain nouns (computed in query_parser import) ─────────
        _, metrics.domain_nouns_ms = _time_ms(
            lambda: importlib.reload(query_parser)
        )
        metrics.domain_nouns_count = len(query_parser._DOMAIN_NOUNS)

        # ── Retirement extractor ───────────────────────────────────
        importlib.reload(retirement_extractor)

        # ── Mock reconciliation ────────────────────────────────────
        importlib.reload(mock_reconciliation)
        _, metrics.mock_reconcile_ms = _time_ms(
            lambda: mock_reconciliation.reconcile_corpus()
        )

        # ── Zone computation (per subject) ─────────────────────────
        subj_list = list(subjects)
        t = time.perf_counter()
        for s in subj_list:
            grammar.compute_zones(s)
        total_ms = (time.perf_counter() - t) * 1000
        metrics.zone_compute_ms_per_subject = (
            total_ms / len(subj_list) if subj_list else 0
        )

        # ── Property invariants ────────────────────────────────────
        importlib.reload(properties)
        _, metrics.properties_ms = _time_ms(
            lambda: properties.validate_all()
        )

        # ── Query dispatch (one sample query per subject) ──────────
        importlib.reload(lg_retriever)
        importlib.reload(shape_cache)
        sample_queries = [
            f"What is the current state of {s}?"
            for s in list(subjects)[:10]
        ]
        bm25_stub = [(m.mid, 1.0) for m in mems[:20]]
        t = time.perf_counter()
        for q in sample_queries:
            lg_retriever.retrieve_lg(q, bm25_stub)
        n = max(1, len(sample_queries))
        metrics.query_dispatch_ms = (
            (time.perf_counter() - t) * 1000 / n
        )

    finally:
        _corpus.ADR_CORPUS = orig_mems
        _corpus.EDGES = orig_edges
        for mod in (
            vocab_induction, edge_markers, grammar, aliases,
            retirement_extractor, mock_reconciliation, query_parser,
            shape_cache, properties, lg_retriever,
        ):
            importlib.reload(mod)

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scales", default="100,500,1000,2500,5000",
        help="Comma-separated memory counts to test.",
    )
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args()

    scales = [int(s) for s in args.scales.split(",")]
    results: list[ScaleMetrics] = []

    print("TLG synthetic-corpus scaling test")
    print("=" * 90)
    header = (
        f"{'N':>6}  {'subj':>5}  {'ents':>6}  {'edges':>6}  "
        f"{'L1 ms':>7}  {'L2 ms':>7}  {'alias ms':>9}  "
        f"{'domN ms':>9}  {'zone ms':>8}  {'props ms':>9}  "
        f"{'mock ms':>9}  {'query ms':>9}"
    )
    print(header)
    print("-" * 90)
    for n in scales:
        print(f"  measuring N={n} ...", flush=True)
        m = measure_scale(n)
        results.append(m)
        print(
            f"{m.n_memories:>6}  {m.n_subjects:>5}  "
            f"{m.total_unique_entities:>6}  {m.n_edges:>6}  "
            f"{m.layer1_ms:>7.1f}  {m.layer2_ms:>7.1f}  "
            f"{m.aliases_ms:>9.1f}  {m.domain_nouns_ms:>9.1f}  "
            f"{m.zone_compute_ms_per_subject:>8.2f}  "
            f"{m.properties_ms:>9.1f}  {m.mock_reconcile_ms:>9.1f}  "
            f"{m.query_dispatch_ms:>9.2f}"
        )

    # Derived: check algorithmic scaling (fit to quadratic growth).
    print()
    print("Scaling ratios (O(n²) would show steady growth in ms/n²)")
    print("-" * 60)
    for m in results:
        n = m.n_memories
        sqrt_n = n**0.5
        print(
            f"  N={n:>5}: alias ms/n² = {m.aliases_ms / (n*n) * 1e6:.2f} µs·n⁻²  "
            f"L2 ms/n = {m.layer2_ms / n:.3f}  "
            f"mock ms/edge = {m.mock_reconcile_ms / max(1, m.n_edges):.3f}"
        )

    if args.json_out:
        from pathlib import Path
        Path(args.json_out).write_text(json.dumps(
            [asdict(m) for m in results], indent=2,
        ))
        print(f"\nWrote JSON to {args.json_out}")


if __name__ == "__main__":
    main()
