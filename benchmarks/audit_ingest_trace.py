"""Ingest-side forensic trace: what the SLM produced, what entities got
linked, and whether any regex fallback fired.

Ingests softwaredev mini with ``subject=`` kwarg + SLM enabled, then
scans every persisted memory and emits one JSONL record per memory
with:

  raw_content, subject, mid, memory_id,
  slm_head_outputs (all 6 + confidences + method),
  slm_slot_entities (what slm_slots_to_entity_dicts produced),
  linked_entity_names (from the memory_entities JOIN),
  linked_entity_types (per-entity 'type' column),
  l1_node present?, l2_node present?, l2_source=(caller_subject|regex|slm),
  l2_entity_id, l2_state_key, l2_state_value[:100],
  memory_slots (raw SLM slot output persisted to memory_slots table)

Special focus: ``sdev-adr_jph-high-trust-teamwork-sec-01`` — the memory
that squats at rank 1 across many unrelated queries in the new run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from pathlib import Path

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus

ROOT = Path("/Users/shawnmccarthy/ncms")
OUT = ROOT / "benchmarks/results/audit/softwaredev_ingest_trace.jsonl"
OUT.parent.mkdir(parents=True, exist_ok=True)

SPOTLIGHT_PREFIXES = [
    "sdev-adr_jph-high-trust-teamwork",  # squatter
    "sdev-adr_jph-api-using-json-v-grpc",  # one that used to win
    "sdev-adr_jph-python-django-framework",
    "sdev-adr_jph-tailwind-css",
]


async def main() -> None:
    build = ROOT / "benchmarks/mseb_softwaredev/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    print(f"[trace] corpus loaded: {len(corpus)} memories", flush=True)

    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain="software_dev",
    )
    await backend.setup()
    try:
        # Build mid -> corpus_memory map BEFORE ingest (so we keep
        # the original subject + content).
        by_mid = {m.mid: m for m in corpus}

        print("[trace] ingesting...", flush=True)
        import time

        t0 = time.perf_counter()
        mid_map = await backend.ingest(corpus)
        print(f"[trace] ingest done in {time.perf_counter() - t0:.1f}s", flush=True)

        svc = backend._svc
        store = svc._store
        from ncms.domain.models import NodeType

        # Preload all ENTITY_STATE nodes keyed by memory_id
        l2_nodes = await store.get_memory_nodes_by_type(
            NodeType.ENTITY_STATE.value,
        )
        l2_by_mem = {n.memory_id: n for n in l2_nodes}

        records = []
        spotlight_records = []

        for mid, mem_id in mid_map.items():
            corpus_mem = by_mid.get(mid)
            if corpus_mem is None:
                continue
            mem = await store.get_memory(mem_id)
            if mem is None:
                continue

            # memory_entities JOIN for names + types
            entity_ids = await store.get_memory_entities(mem_id)
            entity_records = []
            for eid in entity_ids:
                ent = await store.get_entity(eid)
                if ent is not None:
                    entity_records.append(
                        {
                            "name": ent.name,
                            "type": ent.type,
                            # attributes may contain source=slm_slot marker
                            "attributes": ent.attributes,
                        }
                    )

            # memory_slots (raw slot surface forms from SLM)
            memory_slots = []
            if hasattr(store, "get_memory_slots"):
                with contextlib.suppress(Exception):
                    memory_slots = await store.get_memory_slots(mem_id)

            # SLM head outputs were baked into structured['intent_slot']
            # by store_memory before save_memory.
            slm = (mem.structured or {}).get("intent_slot", {})

            # L2 node, if any
            l2 = l2_by_mem.get(mem_id)
            l2_info = None
            if l2 is not None:
                md = l2.metadata or {}
                l2_info = {
                    "source": md.get("source"),
                    "entity_id": md.get("entity_id"),
                    "state_key": md.get("state_key"),
                    "state_value": (md.get("state_value") or "")[:100],
                }

            rec = {
                "mid": mid,
                "memory_id": mem_id,
                "corpus_subject": corpus_mem.subject,
                "content_head": corpus_mem.content[:200],
                "slm": {
                    "method": slm.get("method"),
                    "intent": slm.get("intent"),
                    "intent_conf": slm.get("intent_confidence"),
                    "topic": slm.get("topic"),
                    "topic_conf": slm.get("topic_confidence"),
                    "admission": slm.get("admission"),
                    "admission_conf": slm.get("admission_confidence"),
                    "state_change": slm.get("state_change"),
                    "state_change_conf": slm.get("state_change_confidence"),
                    # shape_intent is not baked into structured (query-side
                    # only) but let's record what we have
                },
                "entities_linked": entity_records,
                "n_entities_linked": len(entity_records),
                "memory_slots": memory_slots,
                "l2": l2_info,
                "has_l1": True,  # L1 always created if we got a mem_id
                "has_l2": l2 is not None,
            }
            records.append(rec)
            if any(mid.startswith(p) for p in SPOTLIGHT_PREFIXES):
                spotlight_records.append(rec)

        # Write full JSONL
        with OUT.open("w") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"[trace] wrote {len(records)} records -> {OUT}", flush=True)

        # ── Print spotlight memories in human-readable form ─────────────
        print()
        print("=" * 86)
        print("  SPOTLIGHT: memories of special interest")
        print("=" * 86)
        for r in spotlight_records:
            print(f"\n  mid={r['mid']}")
            print(f"    subject (corpus): {r['corpus_subject']}")
            print(f"    content[:160]:    {r['content_head'][:160]!r}")
            print(f"    SLM method:       {r['slm']['method']}")
            print(f"    SLM intent:       {r['slm']['intent']} (conf={r['slm']['intent_conf']})")
            print(f"    SLM topic:        {r['slm']['topic']} (conf={r['slm']['topic_conf']})")
            print(
                f"    SLM admission:    {r['slm']['admission']} (conf={r['slm']['admission_conf']})"
            )
            print(
                f"    SLM state_change: {r['slm']['state_change']} "
                f"(conf={r['slm']['state_change_conf']})"
            )
            print(f"    memory_slots:     {r['memory_slots']}")
            print(f"    n_entities_linked: {r['n_entities_linked']}")
            for e in r["entities_linked"]:
                src = ""
                if isinstance(e.get("attributes"), dict):
                    src = e["attributes"].get("source", "")
                print(f"      - name={e['name']!r:45}  type={e['type']!r:20}  src={src!r}")
            print(f"    L2: {r['l2']}")

        # ── Aggregate stats ─────────────────────────────────────────────
        print()
        print("=" * 86)
        print("  AGGREGATE STATS")
        print("=" * 86)
        slm_src = {}
        entity_src = {"slm_slot": 0, "gliner": 0, "caller_subject": 0, "other": 0}
        l2_src = {}
        for r in records:
            slm_src[r["slm"].get("method")] = slm_src.get(r["slm"].get("method"), 0) + 1
            for e in r["entities_linked"]:
                a = e.get("attributes") or {}
                s = a.get("source", "other")
                entity_src[s] = entity_src.get(s, 0) + 1
            if r["l2"]:
                l2_src[r["l2"]["source"]] = l2_src.get(r["l2"]["source"], 0) + 1
        print(f"  SLM method (ingest-time): {slm_src}")
        print(f"  Entity source histogram:  {entity_src}")
        print(f"  L2 node source histogram: {l2_src}")
        print(f"  Memories total:           {len(records)}")
        print(f"  Memories with L2:         {sum(1 for r in records if r['has_l2'])}")
        print(
            f"  Memories with 0 linked entities:  "
            f"{sum(1 for r in records if r['n_entities_linked'] == 0)}"
        )

        # Entity-count distribution
        from collections import Counter

        counts = Counter(r["n_entities_linked"] for r in records)
        print("  Entities-linked distribution (count -> num memories):")
        for n in sorted(counts):
            print(f"    {n:3d} entities -> {counts[n]:4d} memories")

    finally:
        await backend.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
