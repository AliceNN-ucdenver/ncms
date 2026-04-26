"""Minimal diagnostic: ingest MSEB softwaredev mini, dump L1 vocabulary.

Verifies the three production bugs from docs/slm-entity-extraction-design.md:

  Part 1 — UUID → name resolution.  The induced vocab should contain
           entity NAMES (e.g. "react", "frontend framework"), not UUIDs.

  Part 2 — SLM slot-head → memory_entities.  Slot-extracted typed
           entities should appear in the vocab's entity list.

  Part 4 — subject kwarg.  The induced subjects should be MSEB's
           subject IDs (e.g. "adr-0001"), not regex-parsed noise.

Does NOT run retrieve_lg — the full trajectory trace deadlocks on
an unrelated issue we'll triage separately.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from pathlib import Path

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus

ROOT = Path("/Users/shawnmccarthy/ncms")


async def main() -> None:
    build = ROOT / "benchmarks/mseb_softwaredev/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    print(f"loaded {len(corpus)} memories")

    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain="software_dev",
    )
    await backend.setup()
    try:
        print("ingesting...")
        import time

        t0 = time.perf_counter()
        await backend.ingest(corpus)
        print(f"ingest done in {time.perf_counter() - t0:.1f}s")

        svc = backend._svc
        store = svc._store

        # Count ENTITY_STATE nodes
        from ncms.domain.models import NodeType

        nodes = await store.get_memory_nodes_by_type(
            NodeType.ENTITY_STATE.value,
        )
        print(f"\nENTITY_STATE nodes: {len(nodes)}")
        sample = nodes[:5]
        for n in sample:
            entity_id = n.metadata.get("entity_id") if n.metadata else None
            state_key = n.metadata.get("state_key") if n.metadata else None
            state_value = (n.metadata.get("state_value") or "")[:80] if n.metadata else ""
            source = n.metadata.get("source") if n.metadata else None
            print(f"  entity_id={entity_id!r:30}  state_key={state_key!r:15}  source={source!r}")
            print(f"    state_value: {state_value!r}")

        # Dump induced vocab
        vocab_ctx = await svc._tlg_vocab_cache.get_parser_context(store)
        vocab = vocab_ctx.vocabulary
        subjects = sorted(set(vocab.subject_lookup.values()))
        entity_names = list(vocab.entity_lookup.values())

        print("\nInduced L1 vocabulary:")
        print(f"  subjects ({len(subjects)}):")
        for s in subjects[:20]:
            print(f"    {s!r}")
        if len(subjects) > 20:
            print(f"    … and {len(subjects) - 20} more")

        print(f"\n  entities ({len(entity_names)}):")
        for e in entity_names[:25]:
            print(f"    {e!r}")
        if len(entity_names) > 25:
            print(f"    … and {len(entity_names) - 25} more")

        # UUID smell test — should be ZERO in entity names.
        import re

        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        uuid_entities = [e for e in entity_names if uuid_re.match(e)]
        uuid_subjects = [s for s in subjects if uuid_re.match(s)]
        print("\n  UUID smell test:")
        print(f"    entities that look like UUIDs: {len(uuid_entities)} (expected: 0 — Part 1 fix)")
        print(f"    subjects that look like UUIDs: {len(uuid_subjects)} (expected: 0 — Part 1 fix)")

        # Subject-lookup spot check
        print("\n  Subject-lookup spot check:")
        for q in [
            "what is the current status of adr-0001?",
            "which frontend framework did we pick?",
            "why did we choose react?",
        ]:
            from ncms.domain.tlg import lookup_subject

            subj = lookup_subject(q, vocab)
            print(f"    {q!r} -> subject={subj!r}")

    finally:
        await backend.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
