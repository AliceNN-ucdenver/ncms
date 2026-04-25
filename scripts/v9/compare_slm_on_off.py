"""Phase E.2 — SLM-on vs SLM-off ingestion comparison.

Ingests a small corpus of representative inputs through ``store_memory``
twice each: once with the v9 SLM enabled, once with it disabled.  Diffs
the resulting Memory's structured labels (intent, topic, admission,
state_change, slots, role_spans) and Memory.domains.

The SLM-off baseline is what NCMS currently ships at runtime since
``NCMS_SLM_ENABLED=false`` is the default.  Output of this script is
the concrete evidence that turning SLM on actually changes ingestion
behavior — used in the v9 ship-readiness writeup.

Usage::

    uv run python scripts/v9/compare_slm_on_off.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.extraction.intent_slot import build_extractor_chain
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.observability.event_log import EventLog
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


# Representative inputs — three per domain, one per archetype family.
INPUTS: list[tuple[str, str]] = [
    ("clinical",
     "Started patient on metformin 500mg twice daily for type 2 diabetes."),
    ("clinical",
     "Discontinued amoxicillin — rash developed after second dose."),
    ("clinical",
     "Patient reports moderate back pain, persistent for 3 weeks."),
    ("conversational",
     "I really love sushi and ramen, especially with tempura"),
    ("conversational",
     "Switched from coffee to matcha this year."),
    ("conversational",
     "I go bouldering twice a week at the gym downtown."),
    ("software_dev",
     "We've adopted React Native for the mobile app."),
    ("software_dev",
     "Decision: drop Express in favor of Fastify for the API."),
    ("software_dev",
     "We use pytest before every commit for the test suite."),
]


async def _ingest_one(text: str, domain: str, *, slm_enabled: bool) -> dict:
    """Ingest ``text`` once via a fresh in-memory MemoryService and
    return the structured intent_slot labels + domain expansion."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    event_log = EventLog()

    config = NCMSConfig(
        db_path=":memory:",
        slm_enabled=slm_enabled,
        slm_populate_domains=True,
        slm_confidence_threshold=0.3,  # v9 default — see config.py
    )
    chain = None
    if slm_enabled:
        adapter_dir = Path.home() / ".ncms/adapters" / domain / "v9"
        if not adapter_dir.is_dir():
            raise SystemExit(
                f"v9 {domain} adapter not deployed at {adapter_dir}. "
                "Run `ncms adapters train --domain X --version v9` "
                "+ `ncms adapters deploy --domain X --version v9` first.",
            )
        chain = build_extractor_chain(
            checkpoint_dir=adapter_dir,
            confidence_threshold=0.3,
            include_e5_fallback=False,
        )
    svc = MemoryService(
        store=store, index=index, graph=graph,
        config=config, event_log=event_log,
        intent_slot=chain,
    )
    mem = await svc.store_memory(content=text, domains=[domain])
    saved = await store.get_memory(mem.id)
    intent_slot = (saved.structured or {}).get("intent_slot", {}) or {}
    return {
        "intent": intent_slot.get("intent"),
        "topic": intent_slot.get("topic"),
        "admission": intent_slot.get("admission"),
        "state_change": intent_slot.get("state_change"),
        "method": intent_slot.get("method"),
        "domains": list(saved.domains),
        "slot_keys": sorted(intent_slot.get("slots", {}).keys()),
        "n_role_spans": len(intent_slot.get("role_spans", [])),
    }


async def main() -> None:
    print(f"{'domain':14s}  text")
    print("=" * 100)
    n_diverged = 0
    for domain, text in INPUTS:
        on = await _ingest_one(text, domain, slm_enabled=True)
        off = await _ingest_one(text, domain, slm_enabled=False)
        diff: list[str] = []
        for k in ("intent", "topic", "admission", "state_change", "method"):
            if on[k] != off[k]:
                diff.append(f"{k}: {off[k]!r} → {on[k]!r}")
        if on["domains"] != off["domains"]:
            diff.append(f"domains: {off['domains']} → {on['domains']}")
        if on["slot_keys"] != off["slot_keys"]:
            diff.append(f"slots: {off['slot_keys']} → {on['slot_keys']}")
        if on["n_role_spans"] != off["n_role_spans"]:
            diff.append(f"role_spans: {off['n_role_spans']} → {on['n_role_spans']}")
        print(f"{domain:14s}  {text[:80]}")
        for d in diff:
            print(f"   Δ  {d}")
        if not diff:
            print("   (no diff — SLM agreed with regex/heuristic)")
        else:
            n_diverged += 1
        print()
    print("=" * 100)
    print(
        f"summary: {n_diverged}/{len(INPUTS)} inputs showed SLM-vs-regex "
        "divergence (every divergence above is the SLM populating a "
        "label that the regex/heuristic path left None).",
    )


if __name__ == "__main__":
    asyncio.run(main())
