"""E5: End-to-end CTLG pipeline smoke test with hand-authored cue_tags.

Bypasses the (not-yet-trained) cue head by hand-writing cue_tags
onto the memory's structured dict, then runs the memory through
the full ingest pipeline:

  store_memory()
    → admission / inline indexing
    → L1 atomic
    → L2 entity_state
    → reconcile
    → episode formation
    → _extract_and_persist_causal_edges()  (← the CTLG step)
    → SQLite save_graph_edge(CAUSED_BY)

Then queries the dispatcher via retrieve_lg() with a hand-crafted
shape_intent to verify the causal walker finds the new edge.

No SLM, no LLM — pure integration plumbing test.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.application.memory_service import MemoryService  # noqa: E402
from ncms.config import NCMSConfig  # noqa: E402
from ncms.infrastructure.graph.networkx_store import NetworkXGraph  # noqa: E402
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine  # noqa: E402
from ncms.infrastructure.storage.sqlite_store import SQLiteStore  # noqa: E402


def _cue(char_start, char_end, surface, label, conf=0.95):
    """Factory for cue_tag dicts (the serialized TaggedToken shape
    the ingestion pipeline reads)."""
    return {
        "char_start": char_start,
        "char_end": char_end,
        "surface": surface,
        "cue_label": label,
        "confidence": conf,
    }


async def build_service() -> MemoryService:
    cfg = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
        slm_enabled=False,  # we're hand-authoring cue_tags, SLM off
    )
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    svc = MemoryService(
        store=store, index=index, graph=graph, config=cfg,
        intent_slot=None,  # no SLM
    )
    return svc


# ── Narrative: auth-service database driven by a compliance audit ──
# Two memories — one establishes the AUDIT state, the other the
# POSTGRES state with a causal cue linking them.  Both carry a
# realistic SLM payload (state_change + role_spans) so L2 nodes
# get created, then the causal extractor can resolve both REFERENTs
# to existing L2 memory_ids.
#
# When v8 trains, the real SLM produces ALL these fields in one
# forward pass.  This smoke test injects them by hand so we can
# exercise the integration BEFORE v8 lands.
NARRATIVE = [
    # M1: Establish AUDIT as a state-value so the REFERENT surface
    # "audit" resolves in the causal-pair lookup.  Using "audit"
    # (canonical) instead of "Q2 compliance audit" keeps the surface
    # match exact — this is what v8's catalog-canonicalized output
    # would produce in production.
    {
        "content": "The audit recommended encryption-at-rest as a Q2 standard.",
        "slm": {
            "intent": "none", "intent_confidence": 0.95,
            "state_change": "declaration", "state_change_confidence": 0.9,
            "admission": "persist", "admission_confidence": 0.95,
            "topic": "infra", "topic_confidence": 0.8,
            "method": "hand_authored_smoke",
            # role_spans with primary "audit" so L2's state_value
            # becomes the canonical "audit" surface.
            "role_spans": [{
                "char_start": 4, "char_end": 9,
                "surface": "audit", "canonical": "audit",
                "slot": "event", "role": "primary",
            }],
            "slots": {"event": "audit"},
            "cue_tags": [
                _cue(4, 9, "audit", "B-REFERENT"),
            ],
        },
        "subject": "compliance",
    },
    # M2: Establish POSTGRES as the auth-service state + causal
    # cue linking postgres (effect) to audit (cause).
    {
        "content": (
            "The auth-service uses postgres because of the audit "
            "requirement."
        ),
        "slm": {
            "intent": "none", "intent_confidence": 0.95,
            "state_change": "declaration", "state_change_confidence": 0.92,
            "admission": "persist", "admission_confidence": 0.95,
            "topic": "infra", "topic_confidence": 0.8,
            "method": "hand_authored_smoke",
            "role_spans": [{
                "char_start": 22, "char_end": 30,
                "surface": "postgres", "canonical": "postgres",
                "slot": "database", "role": "primary",
            }],
            "slots": {"database": "postgres"},
            "cue_tags": [
                _cue(4, 16, "auth-service", "B-SUBJECT"),
                _cue(22, 30, "postgres", "B-REFERENT"),
                _cue(31, 38, "because", "B-CAUSAL_EXPLICIT"),
                _cue(39, 41, "of", "I-CAUSAL_EXPLICIT"),
                _cue(46, 51, "audit", "B-REFERENT"),
            ],
        },
        "subject": "auth-service",
    },
]


async def main() -> None:
    svc = await build_service()

    print("=" * 72)
    print("E5 — ingest")
    print("=" * 72)

    stored_ids = []
    for i, m in enumerate(NARRATIVE):
        # Inject the full SLM payload into structured so the L2
        # builder + causal extractor see what v8 will eventually
        # produce.
        structured = {"intent_slot": m["slm"]}
        mem = await svc.store_memory(
            content=m["content"],
            domains=["software_dev"],
            importance=9.0,  # bypass admission
            structured=structured,
            subject=m.get("subject"),
        )
        stored_ids.append(mem.id)
        print(f"  stored[{i}] id={mem.id[:8]} text={m['content'][:60]!r}")

    print()
    print("=" * 72)
    print("E5 — inspect zone graph")
    print("=" * 72)

    # Load L1 + L2 + edges
    l1 = await svc._store.get_memory_nodes_by_type("atomic")
    l2 = await svc._store.get_memory_nodes_by_type("entity_state")
    caused_by = await svc._store.list_graph_edges_by_type(["caused_by"])
    enables = await svc._store.list_graph_edges_by_type(["enables"])

    print(f"  L1 atomic nodes:    {len(l1)}")
    print(f"  L2 entity_state:    {len(l2)}")
    print(f"  CAUSED_BY edges:    {len(caused_by)}")
    print(f"  ENABLES edges:      {len(enables)}")
    print()

    for e in caused_by:
        print("  CAUSED_BY edge:")
        print(f"    src={e.source_id[:8]} (effect)")
        print(f"    dst={e.target_id[:8]} (cause)")
        print(f"    metadata: {e.metadata}")

    print()
    print("=" * 72)
    print("E5 — dispatcher causal walker")
    print("=" * 72)

    # Exercise the causal walker by calling retrieve_lg with a
    # hand-set shape_intent.  The walker should find the CAUSED_BY
    # edge we just persisted.
    try:
        trace = await svc.retrieve_lg(
            query="what caused postgres for the auth-service?",
            slm_shape_intent="transitive_cause",
        )
    except Exception as exc:
        print(f"  dispatcher raised: {exc!r}")
        return

    conf = getattr(trace.confidence, "name", str(trace.confidence))
    print(f"  confidence: {conf}")
    print(f"  trace.intent: {trace.intent}")
    print(f"  trace.grammar_answer: {trace.grammar_answer}")
    print(f"  trace.proof: {trace.proof}")

    # Assertions
    print()
    print("=" * 72)
    print("E5 — acceptance checks")
    print("=" * 72)

    ok_ingest = len(caused_by) >= 1
    print(f"  {'✓' if ok_ingest else '✗'} "
          f"CAUSED_BY edge created at ingest ({len(caused_by)} edge(s))")

    ok_walker = "CTLG causal chain" in (trace.proof or "")
    print(f"  {'✓' if ok_walker else '✗'} "
          f"Dispatcher used CTLG path (proof names CTLG)")

    ok_ancestor = trace.grammar_answer is not None
    print(f"  {'✓' if ok_ancestor else '✗'} "
          f"Grammar answer populated: {trace.grammar_answer!r}")

    all_ok = ok_ingest and ok_walker and ok_ancestor
    print()
    print(f"  OVERALL: {'PASS' if all_ok else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
