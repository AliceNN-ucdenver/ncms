"""Query-side grammar correctness trace on a handful of queries.

Ingests softwaredev mini; then for a set of target qids it runs the
full search path and emits a per-query JSONL record with every
grammar decision step instrumented.  Regex / heuristic / fallback
call sites are instrumented with monkey-patch counters so we can
prove they fire (or don't) on a v6 adapter + subject-kwarg corpus.

Target queries include:
  - regressions (old hit, new miss)
  - stable hits
  - shape categories that dropped to 0% (origin, causal_chain,
    sequence, ordinal_last)

Dumps both human-readable per-query blocks and a JSONL file.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from collections import Counter
from pathlib import Path

import yaml

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus

ROOT = Path("/Users/shawnmccarthy/ncms")
OUT_JSONL = ROOT / "benchmarks/results/audit/softwaredev_query_trace.jsonl"
OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)

TARGET_QIDS = [
    # Regressions (old hit top-1, new missed) — one per broken shape
    "softwaredev-causal_chain-001",
    "softwaredev-origin-001",
    "softwaredev-sequence-001",
    "softwaredev-ordinal_last-001",
    "softwaredev-current_state-001",
    "softwaredev-ordinal_first-001",
    "softwaredev-concurrent-001",
    "softwaredev-before_named-002",
    # Stable hits (both old + new hit)
    "softwaredev-retirement-001",
    "softwaredev-predecessor-001",
    "softwaredev-transitive_cause-001",
]


class FallbackCounter:
    """Counts every call to regex/heuristic/fallback code paths."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def record(self, label: str) -> None:
        self.calls[label] += 1

    def reset(self) -> None:
        self.calls = Counter()

    def snapshot(self) -> dict[str, int]:
        return dict(self.calls)


def install_instrumentation(counter: FallbackCounter) -> None:
    """Monkey-patch known regex/fallback entry points to record calls.

    We intentionally DO NOT change behaviour — we only count.  Each
    patch wraps the original function and calls it through.
    """
    # 1. extract_entity_state_meta (regex zoo for L2 extraction)
    from ncms.application.ingestion import pipeline as ingest_pipeline

    orig_extract = ingest_pipeline.IngestionPipeline.extract_entity_state_meta

    @staticmethod
    def patched_extract(content, entities):
        counter.record("extract_entity_state_meta")
        return orig_extract.__func__(content, entities)

    ingest_pipeline.IngestionPipeline.extract_entity_state_meta = patched_extract

    # 2. HeuristicFallbackExtractor.extract (SLM chain tail)
    try:
        from ncms.infrastructure.extraction.intent_slot import heuristic_fallback

        orig_heur = heuristic_fallback.HeuristicFallbackExtractor.extract

        def patched_heur(self, text, *, domain):
            counter.record("heuristic_fallback.extract")
            return orig_heur(self, text, domain=domain)

        heuristic_fallback.HeuristicFallbackExtractor.extract = patched_heur
    except Exception:
        pass

    # 3. E5ZeroShotExtractor.extract (SLM chain middle)
    try:
        from ncms.infrastructure.extraction.intent_slot import e5_zero_shot

        orig_e5 = e5_zero_shot.E5ZeroShotExtractor.extract

        def patched_e5(self, text, *, domain):
            counter.record("e5_zero_shot.extract")
            return orig_e5(self, text, domain=domain)

        e5_zero_shot.E5ZeroShotExtractor.extract = patched_e5
    except Exception:
        pass

    # 4. Intent classifier (BM25 exemplar)
    try:
        from ncms.infrastructure.indexing import exemplar_intent_index as exempl

        if hasattr(exempl, "ExemplarIntentIndex"):
            orig_classify = exempl.ExemplarIntentIndex.classify

            def patched_classify(self, query):
                counter.record("exemplar_intent_index.classify")
                return orig_classify(self, query)

            exempl.ExemplarIntentIndex.classify = patched_classify
    except Exception:
        pass

    # 5. TLG query_parser — analyze_query (regex-driven grammar parsing)
    try:
        from ncms.domain.tlg import query_parser

        orig_analyze = query_parser.analyze_query

        def patched_analyze(*args, **kwargs):
            counter.record("tlg.analyze_query")
            return orig_analyze(*args, **kwargs)

        query_parser.analyze_query = patched_analyze
        # Also re-export at package level if referenced via ncms.domain.tlg
        try:
            from ncms.domain import tlg as tlg_pkg

            if hasattr(tlg_pkg, "analyze_query"):
                tlg_pkg.analyze_query = patched_analyze
        except Exception:
            pass
    except Exception:
        pass


async def main() -> None:
    build = ROOT / "benchmarks/mseb_softwaredev/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    queries = yaml.safe_load((ROOT / "benchmarks/mseb_softwaredev/gold_locked.yaml").read_text())
    gold_by_qid = {g["qid"]: g for g in queries}
    print(f"[query_trace] corpus={len(corpus)} queries={len(queries)}", flush=True)

    counter = FallbackCounter()
    install_instrumentation(counter)

    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain="software_dev",
    )
    await backend.setup()
    try:
        print("[query_trace] ingesting...", flush=True)
        await backend.ingest(corpus)
        ingest_fallback_snapshot = counter.snapshot()
        counter.reset()  # clear ingest-time counts; query pass starts fresh

        print(f"[query_trace] INGEST fallback snapshot: {ingest_fallback_snapshot}", flush=True)

        svc = backend._svc
        store = svc._store

        records = []
        for qid in TARGET_QIDS:
            g = gold_by_qid.get(qid)
            if g is None:
                print(f"  [warn] qid {qid} not in gold", flush=True)
                continue
            qtext = g["text"]
            shape = g["shape"]
            gold_mid = g["gold_mid"]

            # Per-query fallback audit
            counter.reset()

            # ── SLM heads on query ────────────────────────────────────
            head = backend.classify_query(qtext)

            # ── Full search path (as harness uses) ────────────────────
            results = await svc.search(
                query=qtext,
                limit=10,
            )
            ranked_mids: list[str] = []
            top_meta = []
            for r in results:
                mem = getattr(r, "memory", None)
                if mem is None:
                    continue
                mid = None
                for tag in mem.tags or []:
                    if tag.startswith("mid:"):
                        mid = tag.split(":", 1)[1]
                        break
                if mid is None:
                    continue
                ranked_mids.append(mid)
                top_meta.append(
                    {
                        "mid": mid,
                        "score": getattr(r, "total_score", None),
                        "bm25": getattr(r, "bm25_score", None),
                        "splade": getattr(r, "splade_score", None),
                        "graph": getattr(r, "graph_score", None),
                        "actr": getattr(r, "actr_score", None),
                    }
                )

            # ── retrieve_lg (grammar walker) — direct call ────────────
            grammar_answer = None
            grammar_conf = None
            grammar_proof = None
            grammar_intent = None
            grammar_subject = None
            grammar_entity = None
            try:
                trace = await svc.retrieve_lg(
                    qtext,
                )
                grammar_conf = trace.confidence.value if trace.confidence else None
                grammar_proof = trace.proof
                if trace.grammar_answer:
                    gmem = await store.get_memory(trace.grammar_answer)
                    if gmem is not None:
                        for tag in gmem.tags or []:
                            if tag.startswith("mid:"):
                                grammar_answer = tag.split(":", 1)[1]
                                break
                if trace.intent is not None:
                    grammar_intent = trace.intent.kind
                    grammar_subject = trace.intent.subject
                    grammar_entity = trace.intent.entity
            except Exception as exc:
                grammar_proof = f"retrieve_lg_raised: {exc!r}"

            rank_of_gold = None
            for i, m in enumerate(ranked_mids, start=1):
                if m == gold_mid:
                    rank_of_gold = i
                    break

            fallbacks = counter.snapshot()
            rec = {
                "qid": qid,
                "shape": shape,
                "text": qtext,
                "gold_mid": gold_mid,
                "slm_heads": head,
                "search_top1": ranked_mids[0] if ranked_mids else None,
                "search_top10": ranked_mids[:10],
                "rank_of_gold_in_search": rank_of_gold,
                "top_meta": top_meta,
                "grammar_intent": grammar_intent,
                "grammar_subject": grammar_subject,
                "grammar_entity": grammar_entity,
                "grammar_answer_mid": grammar_answer,
                "grammar_conf": grammar_conf,
                "grammar_proof": grammar_proof,
                "grammar_hit": grammar_answer == gold_mid if grammar_answer else False,
                "fallbacks_during_query": fallbacks,
            }
            records.append(rec)

            print()
            print("=" * 86)
            print(f"  qid={qid}  shape={shape}")
            print(f"  text: {qtext[:110]}")
            print(f"  gold_mid: {gold_mid}")
            print(
                f"  SLM: shape_intent={head.get('shape_intent')!r} "
                f"({head.get('shape_intent_confidence')})  "
                f"intent={head.get('intent')!r} "
                f"({head.get('intent_confidence')})"
            )
            print(
                f"       topic={head.get('topic')!r}  "
                f"admission={head.get('admission')!r}  "
                f"state_change={head.get('state_change')!r}"
            )
            print(
                f"  Grammar: intent={grammar_intent!r}  "
                f"subject={grammar_subject!r}  entity={grammar_entity!r}  "
                f"conf={grammar_conf!r}"
            )
            print(
                f"  Grammar answer: {grammar_answer!r}  "
                f"(gold={gold_mid!r})  match={rec['grammar_hit']}"
            )
            print(f"  Grammar proof: {grammar_proof}")
            print(f"  Search top-1: {ranked_mids[0] if ranked_mids else '-'}")
            print(f"  Search top-3: {ranked_mids[:3]}")
            print(f"  Rank of gold in search: {rank_of_gold}")
            print(f"  Fallbacks fired during this query: {fallbacks or '{}'}")

        with OUT_JSONL.open("w") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
        print()
        print(f"[query_trace] wrote {len(records)} records -> {OUT_JSONL}", flush=True)

        # Aggregate fallbacks across all queries
        total_q_fb: Counter = Counter()
        for r in records:
            total_q_fb.update(r["fallbacks_during_query"])
        print(
            f"[query_trace] AGGREGATE query-time fallbacks: {dict(total_q_fb) or '{}'}", flush=True
        )
        print(
            f"[query_trace] ingest-time fallbacks (earlier): {ingest_fallback_snapshot}", flush=True
        )

    finally:
        await backend.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
