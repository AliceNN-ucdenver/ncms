"""Forensics B+C+D: TLG end-to-end smoke test.

Builds an in-memory MemoryService with temporal_enabled=True and SLM
chain pointing at v7.1.  Ingests a narrative with known state changes,
then exercises:

  * C) state reconciliation — does ingest create SUPERSEDES edges +
    flip is_current correctly?
  * B) dispatcher wiring — does retrieve_lg() with a manually-supplied
    shape_intent route to the right walker?  (Bypasses the broken
    shape_intent head so we can isolate dispatcher correctness.)
  * D) end-to-end — does a natural query resolve through the full
    pipeline (ingest \u2192 reconciliation \u2192 retrieve_lg) to a
    sensible grammar_answer?

Output: JSONL trace per step so we can diff before/after changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("forensics_tlg")
log.setLevel(logging.INFO)


# A clean narrative with 3 state transitions on the 'database' subject.
# Each line is one memory we'll ingest.
NARRATIVE = [
    # t0 — declaration
    "Decision: the auth-service uses PostgreSQL for the primary user store.",
    # t1 — refinement (same subject, adds detail, should NOT supersede)
    "For auth-service PostgreSQL, we'll use Patroni for HA and pgBouncer in front.",
    # t2 — first supersession (PostgreSQL -> CockroachDB)
    "Migrated the auth-service away from PostgreSQL to CockroachDB for multi-region.",
    # t3 — second supersession (CockroachDB -> Yugabyte)
    "We have deprecated CockroachDB in favor of YugabyteDB for stronger Postgres compatibility.",
    # t4 — current state
    "The auth-service now uses YugabyteDB in all three regions as of the latest rollout.",
]


async def build_service() -> MemoryService:
    from benchmarks.intent_slot_adapter import get_intent_slot_chain

    # Always-on temporal stack for forensics.
    cfg = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
        slm_enabled=True,
        # inline indexing only when no pool is started (default)
    )
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    try:
        chain = get_intent_slot_chain(domain="software_dev", version="v7.1")
    except Exception as exc:
        log.warning("SLM chain load failed: %s — running without SLM", exc)
        chain = None
    svc = MemoryService(
        store=store, index=index, graph=graph, config=cfg,
        intent_slot=chain,
    )
    return svc


async def ingest_narrative(svc: MemoryService) -> list:
    stored = []
    for i, text in enumerate(NARRATIVE):
        mem = await svc.store_memory(
            content=text, domains=["software_dev"],
            importance=9.0,  # bypass admission so every line persists
        )
        log.info("stored[%d] id=%s type=%s",
                 i, mem.id[:8], getattr(mem, "type", "?"))
        stored.append(mem)
    return stored


async def audit_reconciliation(svc: MemoryService) -> dict:
    """Forensics C: did reconciliation create SUPERSEDES edges?"""
    store = svc._store  # noqa: SLF001
    nodes = await store.get_memory_nodes_by_type("atomic")
    nodes += await store.get_memory_nodes_by_type("entity_state")
    edges = []
    for et in ("supersedes","derived_from","refines","conflicts","superseded_by"):
        edges += await store.list_graph_edges_by_type(et)
    l1 = [n for n in nodes if n.node_type == "atomic"]
    l2 = [n for n in nodes if n.node_type == "entity_state"]
    supersedes = [e for e in edges if e.edge_type == "supersedes"]
    derived = [e for e in edges if e.edge_type == "derived_from"]
    refines = [e for e in edges if e.edge_type == "refines"]
    conflicts = [e for e in edges if e.edge_type == "conflicts"]
    current_l2 = [n for n in l2 if n.is_current]
    retired_l2 = [n for n in l2 if not n.is_current]
    return {
        "l1_atomic_count": len(l1),
        "l2_entity_state_count": len(l2),
        "supersedes_edges": len(supersedes),
        "derived_from_edges": len(derived),
        "refines_edges": len(refines),
        "conflicts_edges": len(conflicts),
        "l2_current": len(current_l2),
        "l2_retired": len(retired_l2),
        "l2_current_values": [
            {"id": n.id[:8], "memory_id": n.memory_id[:8], "is_current": n.is_current, "metadata": n.metadata}
            for n in current_l2
        ],
        "l2_retired_values": [
            {"id": n.id[:8], "memory_id": n.memory_id[:8], "metadata": n.metadata,
             "valid_to": n.valid_to.isoformat() if n.valid_to else None}
            for n in retired_l2
        ],
    }


# Query per dispatcher intent.  Using override so shape_intent head is
# bypassed — this tests the dispatcher, not the SLM classifier.
DISPATCH_PROBES: list[tuple[str, str]] = [
    ("current",          "What does the auth-service use for its database?"),
    ("before_named",     "What did the auth-service use before YugabyteDB?"),
    ("origin",           "What was the first database the auth-service adopted?"),
    ("retirement",       "What databases did the auth-service retire?"),
    ("sequence",         "Walk through the auth-service database history."),
    ("predecessor",      "What was considered before CockroachDB?"),
    ("transitive_cause", "Why did we leave PostgreSQL?"),
    ("cause_of",         "What chain of decisions led to YugabyteDB?"),
    ("concurrent",       "What ran alongside PostgreSQL in auth-service?"),
    ("interval",         "What was in use during the CockroachDB era?"),
]


async def exercise_dispatcher(svc: MemoryService) -> list[dict]:
    """Forensics B: call retrieve_lg with explicit shape_intent overrides."""
    out = []
    for dispatch_intent, query in DISPATCH_PROBES:
        # Reverse-map dispatch_intent back to a shape_intent that would
        # route there.  Use the most natural mapping.
        intent_to_shape = {
            "current": "current_state",
            "before_named": "before_named",
            "origin": "origin",
            "retirement": "retirement",
            "sequence": "sequence",
            "predecessor": "predecessor",
            "transitive_cause": "transitive_cause",
            "cause_of": "causal_chain",
            "concurrent": "concurrent",
            "interval": "interval",
        }
        shape = intent_to_shape[dispatch_intent]
        try:
            trace = await svc.retrieve_lg(
                query=query,
                slm_shape_intent=shape,
            )
            conf = getattr(trace.confidence, "name", str(trace.confidence))
            out.append({
                "dispatch_intent": dispatch_intent,
                "shape_intent": shape,
                "query": query,
                "trace_intent": trace.intent,
                "confidence": conf,
                "grammar_answer": str(trace.grammar_answer)[:200]
                if trace.grammar_answer else None,
                "zone_hits": len(trace.zones) if hasattr(trace, "zones") else None,
            })
        except Exception as exc:
            out.append({
                "dispatch_intent": dispatch_intent,
                "shape_intent": shape,
                "query": query,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return out


async def e2e_auto_classify(svc: MemoryService) -> list[dict]:
    """Forensics D: full pipeline — SLM classifies + dispatcher routes."""
    queries = [
        "What does the auth-service use for its database?",
        "What did the auth-service use before YugabyteDB?",
        "Which database did we retire last?",
        "What was the original database of the auth-service?",
    ]
    out = []
    for q in queries:
        trace = await svc.retrieve_lg(query=q)
        conf = getattr(trace.confidence, "name", str(trace.confidence))
        out.append({
            "query": q,
            "trace_intent": trace.intent,
            "confidence": conf,
            "grammar_answer": str(trace.grammar_answer)[:200]
            if trace.grammar_answer else None,
        })
    return out


async def main() -> None:
    svc = await build_service()
    print("=" * 72)
    print("INGEST")
    print("=" * 72)
    await ingest_narrative(svc)

    print()
    print("=" * 72)
    print("FORENSICS C — state reconciliation audit")
    print("=" * 72)
    audit = await audit_reconciliation(svc)
    print(json.dumps(audit, indent=2))

    print()
    print("=" * 72)
    print("FORENSICS B — dispatcher wiring (shape_intent override)")
    print("=" * 72)
    b = await exercise_dispatcher(svc)
    for row in b:
        print(f"  [{row.get('dispatch_intent'):<17}] conf={row.get('confidence')}"
              f" intent={row.get('trace_intent')}")
        if row.get("error"):
            print(f"    ERROR: {row['error']}")
        elif row.get("grammar_answer"):
            print(f"    answer: {row['grammar_answer']!r}")
        print(f"    query:  {row['query']!r}")

    print()
    print("=" * 72)
    print("FORENSICS D — end-to-end auto-classify")
    print("=" * 72)
    d = await e2e_auto_classify(svc)
    for row in d:
        print(f"  intent={str(row['trace_intent'])[:60]} conf={row['confidence']:<8}"
              f" query={row['query']!r}")
        if row["grammar_answer"]:
            print(f"    answer: {row['grammar_answer']!r}")


if __name__ == "__main__":
    asyncio.run(main())
