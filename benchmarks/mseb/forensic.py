"""MSEB forensic tool — what is TLG actually doing per query?

Runs a handful of gold queries through the NCMS pipeline with a
live EventLog attached, captures every pipeline stage event
(intent_classification, intent_miss, hierarchy_bonus, temporal
signal, reconciliation penalties, scoring breakdown), and prints
a per-query trace so we can answer questions like:

- Is the intent classifier firing, or falling back to fact_lookup
  on these queries?  What confidence did it report?
- What did the SLM's topic / state_change heads say about each
  corpus memory at ingest?  Were patches tagged as retirement?
- On "tlg-needed" queries (where gold is at lexical rank 2-3):
  did TLG mechanisms lift the gold to rank 1 or not?

Usage::

    uv run python -m benchmarks.mseb.forensic \\
        --build-dir benchmarks/mseb_swe/build_mini \\
        --adapter-domain software_dev \\
        --validation benchmarks/mseb_swe/gold_validation.json \\
        --difficulty tlg-needed \\
        --n 20 \\
        --out benchmarks/mseb/run-logs/forensic-swe.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Load HF_TOKEN before any transformer imports.
try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("mseb.forensic")


async def run_forensic(
    build_dir: Path,
    adapter_domain: str,
    validation_path: Path,
    difficulty: str,
    n: int,
    out_path: Path,
) -> None:
    from benchmarks.intent_slot_adapter import get_intent_slot_chain
    from benchmarks.mseb.schema import load_corpus, load_queries
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.observability.event_log import (
        EventLog,
    )
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    corpus = load_corpus(build_dir / "corpus.jsonl")
    queries = load_queries(build_dir / "queries.jsonl")
    logger.info("loaded corpus=%d queries=%d", len(corpus), len(queries))

    # Filter queries to the target difficulty class (from validation output).
    val = json.loads(validation_path.read_text())
    diff_by_qid = {r["qid"]: r.get("difficulty", "?") for r in val["per_query"]}
    candidates = [q for q in queries if diff_by_qid.get(q.qid) == difficulty]
    logger.info(
        "found %d queries with difficulty=%s (of %d total)",
        len(candidates), difficulty, len(queries),
    )
    if not candidates:
        logger.warning("no queries match difficulty filter; nothing to analyse")
        return
    picked = candidates[:n]

    # ----- Event log setup -----
    event_log = EventLog(max_events=10000)
    events_per_query: dict[str, list[dict]] = defaultdict(list)
    current_qid: list[str | None] = [None]

    # Subscribe via the event_log's internal buffer; we read events
    # after each query from the ring buffer.
    def drain_events() -> list[dict]:
        out: list[dict] = []
        for ev in list(event_log._events):  # type: ignore[attr-defined]
            out.append({
                "type": ev.type,
                "data": ev.data,
                "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
            })
        # Clear the buffer so each query's events are isolated.
        event_log._events.clear()  # type: ignore[attr-defined]
        return out

    # ----- NCMS setup (full TLG-on config) -----
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine(); index.initialize()
    graph = NetworkXGraph()
    splade = SpladeEngine()
    intent_slot = get_intent_slot_chain(
        domain=adapter_domain, include_e5_fallback=False,
    )

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_splade=0.3,
        scoring_weight_graph=0.3,
        contradiction_detection_enabled=False,
        temporal_enabled=True,
        scoring_weight_hierarchy=0.5,
        slm_enabled=True,
        slm_populate_domains=True,
        pipeline_debug=True,
    )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, intent_slot=intent_slot, event_log=event_log,
    )
    await svc.start_index_pool()

    # ----- Ingest -----
    from datetime import UTC, datetime
    logger.info("ingesting %d memories …", len(corpus))
    for m in sorted(corpus, key=lambda x: (x.subject, x.observed_at, x.mid)):
        try:
            observed_at = datetime.fromisoformat(
                m.observed_at.replace("Z", "+00:00"),
            ).astimezone(UTC)
        except ValueError:
            observed_at = None
        await svc.store_memory(
            content=m.content, memory_type="fact",
            source_agent=m.metadata.get("source_agent", "mseb"),
            domains=m.metadata.get("domains") or [],
            tags=["mseb", f"subject:{m.subject}", f"mid:{m.mid}"],
            observed_at=observed_at,
        )
    await svc.flush_indexing()
    logger.info("ingest drained; starting forensic search")

    # Drain ingest-side events (we don't care about these right now).
    _ = drain_events()

    # ----- Per-query search + event capture -----
    traces: list[dict] = []
    for q in picked:
        current_qid[0] = q.qid
        results = await svc.search(query=q.text, limit=10)
        events = drain_events()

        ranked_mids: list[str] = []
        for r in results:
            memory = getattr(r, "memory", r)
            tags = getattr(memory, "tags", []) or []
            mid = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("mid:")),
                None,
            )
            if mid is not None:
                ranked_mids.append(mid)

        gold_rank = next(
            (i + 1 for i, m in enumerate(ranked_mids) if m == q.gold_mid),
            None,
        )

        # Pick out the KEY events.
        intent_events = [e for e in events
                         if "intent" in e["type"].lower()]
        miss_events = [e for e in events
                       if e["type"].endswith(".intent_miss")]
        scoring_events = [e for e in events
                          if "scor" in e["type"].lower()]

        trace = {
            "qid": q.qid,
            "shape": q.shape,
            "text": q.text,
            "subject": q.subject,
            "gold_mid": q.gold_mid,
            "gold_rank_actual": gold_rank,
            "ranked_mids": ranked_mids[:5],
            "intent_events": intent_events,
            "intent_miss_events": miss_events,
            "scoring_events": scoring_events[:3],  # cap
            "all_event_types": sorted({e["type"] for e in events}),
        }
        traces.append(trace)

    # ----- Summarize -----
    n_intent_miss = sum(1 for t in traces if t["intent_miss_events"])
    gold_rank_1 = sum(1 for t in traces
                      if t["gold_rank_actual"] == 1)
    gold_rank_top5 = sum(1 for t in traces
                         if t["gold_rank_actual"] and t["gold_rank_actual"] <= 5)
    summary = {
        "n_queries_analyzed": len(traces),
        "difficulty_filter": difficulty,
        "gold_rank_1_count": gold_rank_1,
        "gold_rank_top5_count": gold_rank_top5,
        "intent_miss_count": n_intent_miss,
        "intent_miss_rate": n_intent_miss / max(len(traces), 1),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "summary": summary, "traces": traces,
    }, indent=2, default=str))
    logger.info("wrote %d traces to %s", len(traces), out_path)
    print(json.dumps(summary, indent=2, sort_keys=True))

    import os
    os._exit(0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", type=Path, required=True)
    ap.add_argument("--adapter-domain", required=True)
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--difficulty", default="tlg-needed",
                    choices=["easy", "tlg-needed", "hard", "impossible"])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    asyncio.run(run_forensic(
        build_dir=args.build_dir,
        adapter_domain=args.adapter_domain,
        validation_path=args.validation,
        difficulty=args.difficulty,
        n=args.n,
        out_path=args.out,
    ))


if __name__ == "__main__":
    sys.exit(main())
