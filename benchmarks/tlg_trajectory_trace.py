"""Deep TLG trajectory trace.

For each MSEB query in a build_mini corpus, ingest the corpus then
run the full retrieve_lg pipeline and report:

  1. SLM shape_intent classification (from v6 adapter taxonomy)
  2. Dispatcher intent (after _SLM_SHAPE_TO_DISPATCH_INTENT map)
  3. Subject inferred from vocabulary lookup
  4. Target entity extracted from query
  5. Zone load (how many zones exist for that subject?)
  6. Grammar answer (the memory_id the walker proposed)
  7. Walker confidence (HIGH / MEDIUM / ABSTAIN / NONE)
  8. Human-readable proof trace
  9. Whether the grammar_answer matches MSEB gold_mid

Prints per-shape accuracy + the first few examples where the
grammar answer differs from gold, so we can see where the walk
drifts.
"""
from __future__ import annotations

import asyncio
import os
os.environ.setdefault("HF_HUB_OFFLINE", "1")
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus, load_queries

ROOT = Path("/Users/shawnmccarthy/ncms")


async def trace_domain(
    mseb_domain: str, adapter_domain: str, limit_queries: int | None = None,
) -> None:
    print(f"[{mseb_domain}] loading corpus + queries", flush=True)
    build = ROOT / f"benchmarks/mseb_{mseb_domain}/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    queries = load_queries(build / "queries.jsonl")
    if limit_queries:
        queries = queries[:limit_queries]
    print(f"[{mseb_domain}] loaded {len(corpus)} mems + "
          f"{len(queries)} queries", flush=True)

    # MSEB gold for correctness check
    gold_yaml = yaml.safe_load(
        (ROOT / f"benchmarks/mseb_{mseb_domain}/gold_locked.yaml").read_text(),
    )
    gold_by_qid = {g["qid"]: g for g in gold_yaml}

    print(f"[{mseb_domain}] constructing backend...", flush=True)
    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain=adapter_domain,
    )
    print(f"[{mseb_domain}] calling backend.setup()...", flush=True)
    await backend.setup()
    try:
        print(f"[{mseb_domain}] ingesting {len(corpus)} memories...", flush=True)
        import time as _t
        t0 = _t.perf_counter()
        await backend.ingest(corpus)
        print(f"[{mseb_domain}] ingest done in {_t.perf_counter()-t0:.1f}s",
              flush=True)
        svc = backend._svc
        print(f"[{mseb_domain}] starting trace over {len(queries)} queries...",
              flush=True)

        print(f"\n{'='*86}")
        print(f"  {mseb_domain} → {adapter_domain}/v6  "
              f"(corpus={len(corpus)} queries={len(queries)})")
        print(f"{'='*86}")

        per_shape_stats: dict[str, Counter] = defaultdict(Counter)
        mismatches: list[dict] = []
        abstain_samples: list[dict] = []  # for forensic review

        for q in queries:
            gold = gold_by_qid.get(q.qid, {})
            # Classify via SLM (same path the harness uses)
            head = backend.classify_query(q.text)
            slm_shape = head.get("shape_intent")

            # Call retrieve_lg with the SLM-classified shape
            # (matches the production MemoryService path).
            trace = await svc.retrieve_lg(
                q.text,
                slm_shape_intent=slm_shape if slm_shape else None,
            )

            gold_shape = gold.get("shape", "noise")
            # Normalize MSEB's "noise" to SLM's "none"
            expected_shape = "none" if gold_shape == "noise" else gold_shape
            confidence = trace.confidence.value
            dispatch_intent = trace.intent.kind if trace.intent else ""

            # For gold_mid correctness: if the query's gold_mid
            # corresponds to a memory we ingested, does the grammar
            # answer match?
            gold_mid = gold.get("gold_mid", "")
            # Grammar answer is a memory_id; gold_mid is the MSEB
            # corpus mid.  The ingest path puts "mid:<corpus_mid>"
            # in the memory's tags, so we look up the memory.
            grammar_correct = None
            if trace.grammar_answer and gold_mid:
                try:
                    mem = await svc._store.get_memory(trace.grammar_answer)
                except Exception:
                    mem = None
                if mem is not None:
                    mid_tag = next(
                        (t.split(":", 1)[1] for t in (mem.tags or [])
                         if t.startswith("mid:")), None,
                    )
                    grammar_correct = (mid_tag == gold_mid)

            # Accumulate per-shape stats
            per_shape_stats[expected_shape]["total"] += 1
            per_shape_stats[expected_shape][f"conf_{confidence}"] += 1
            if grammar_correct is True:
                per_shape_stats[expected_shape]["grammar_correct"] += 1
            elif grammar_correct is False:
                per_shape_stats[expected_shape]["grammar_wrong"] += 1

            # Record mismatches for review
            if (confidence in ("high", "medium") and grammar_correct is False):
                mismatches.append({
                    "qid": q.qid,
                    "text": q.text,
                    "expected_shape": expected_shape,
                    "slm_shape": slm_shape,
                    "dispatch_intent": dispatch_intent,
                    "grammar_answer": trace.grammar_answer,
                    "gold_mid": gold_mid,
                    "confidence": confidence,
                    "proof": trace.proof,
                    "subject": trace.intent.subject if trace.intent else None,
                    "entity": trace.intent.entity if trace.intent else None,
                })

            # Sample first 3 abstains per shape — need this when
            # ALL queries abstain (no mismatches to print).
            shape_abstains = sum(
                1 for a in abstain_samples
                if a["expected_shape"] == expected_shape
            )
            if confidence in ("abstain", "none") and shape_abstains < 3:
                abstain_samples.append({
                    "qid": q.qid,
                    "text": q.text,
                    "expected_shape": expected_shape,
                    "slm_shape": slm_shape,
                    "dispatch_intent": dispatch_intent,
                    "confidence": confidence,
                    "proof": trace.proof,
                    "subject": trace.intent.subject if trace.intent else None,
                    "entity": trace.intent.entity if trace.intent else None,
                })

        # Per-shape summary
        print(f"\n{'shape':20} {'n':>4} {'high':>5} {'med':>4} "
              f"{'abs':>4} {'none':>5} {'✓':>4} {'✗':>4}")
        print("-" * 72)
        for shape in sorted(per_shape_stats):
            s = per_shape_stats[shape]
            print(
                f"{shape:20} {s['total']:4d} "
                f"{s.get('conf_high', 0):>5} {s.get('conf_medium', 0):>4} "
                f"{s.get('conf_abstain', 0):>4} {s.get('conf_none', 0):>5} "
                f"{s.get('grammar_correct', 0):>4} {s.get('grammar_wrong', 0):>4}"
            )

        # Show first N mismatches for forensic review
        if mismatches:
            print(f"\nFirst {min(8, len(mismatches))} grammar mismatches "
                  f"(confident but wrong) — {len(mismatches)} total:\n")
            for m in mismatches[:8]:
                print(f"  [{m['confidence']}] qid={m['qid']}")
                print(f"     expected_shape={m['expected_shape']:18} "
                      f"slm_shape={m['slm_shape']:18} "
                      f"dispatch={m['dispatch_intent']}")
                print(f"     query: {m['text'][:110]}")
                print(f"     subject={m['subject']!r} entity={m['entity']!r}")
                print(f"     grammar_answer={m['grammar_answer']}")
                print(f"     gold_mid=        {m['gold_mid']}")
                print(f"     proof: {m['proof']}")
                print()

        # Show abstain samples — useful when ALL queries abstain
        if abstain_samples:
            print(f"\n{len(abstain_samples)} abstain samples "
                  f"(up to 3 per shape):\n")
            for m in abstain_samples:
                print(f"  [{m['confidence']}] qid={m['qid']}")
                print(f"     expected_shape={m['expected_shape']:18} "
                      f"slm_shape={m['slm_shape']:18} "
                      f"dispatch={m['dispatch_intent']}")
                print(f"     query: {m['text'][:110]}")
                print(f"     subject={m['subject']!r} entity={m['entity']!r}")
                print(f"     proof: {m['proof']}")
                print()

        # Vocabulary diagnostic — what subjects + entities did the
        # ingest pipeline induce? A 0-subject vocabulary explains why
        # every subject resolution fails.
        try:
            vocab_ctx = await svc._tlg_vocab_cache.get_parser_context(  # type: ignore[attr-defined]
                svc._store,
            )
            subjects = sorted(set(vocab_ctx.vocabulary.subject_lookup.values()))
            entity_names = list(vocab_ctx.vocabulary.entity_lookup.keys())
            print(f"\nInduced L1 vocabulary after ingest:")
            print(f"  subjects ({len(subjects)}): {subjects[:10]}"
                  f"{'…' if len(subjects) > 10 else ''}")
            print(f"  entities ({len(entity_names)}): "
                  f"{entity_names[:12]}"
                  f"{'…' if len(entity_names) > 12 else ''}")
        except Exception as exc:
            print(f"\nvocabulary diagnostic failed: {exc!r}")

    finally:
        await backend.shutdown()


async def main() -> None:
    # Start with softwaredev (most legible — template-generated queries)
    await trace_domain("softwaredev", "software_dev", limit_queries=60)


if __name__ == "__main__":
    asyncio.run(main())
