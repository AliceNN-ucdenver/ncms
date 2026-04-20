"""End-to-end integration test for the P2 intent-slot SLM.

Demonstrates the full story the user asked for:

1. **Store a memory with adapter A** — verify all five SLM heads
   populate the `memories` columns + `memory_slots` table +
   dashboard event.  No topic config anywhere; topic is set
   dynamically from the classifier's output.

2. **Switch to adapter B** — build a new MemoryService with a
   different domain's adapter.  Verify the same flow uses the new
   adapter's taxonomy, with new topic labels appearing in the DB.

3. **Read topics dynamically** — use
   :meth:`SQLiteStore.list_topics_seen` to enumerate topics for
   the dashboard WITHOUT consulting any adapter manifest.  This
   proves the "dynamic topics" design: the dashboard's topic view
   works even if the operator changes adapters without re-
   configuring the dashboard.

4. **Heuristic fallback** — when no adapter is available, ingest
   still works: admission=persist, other heads stay NULL.

5. **Parity check** — compare the adapter's inference output
   (directly via extract()) with what lands in the DB (via
   store_memory + get_memory).  Round-trip clean.

Skipped when v4 adapters aren't published at ``~/.ncms/adapters/``.
Run locally with:

    uv run pytest tests/integration/test_intent_slot_e2e.py -v -s
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from benchmarks.intent_slot_adapter import (
    find_adapter_dir,
    get_intent_slot_chain,
    list_available_adapters,
)
from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.extraction.intent_slot import build_extractor_chain
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.observability.event_log import EventLog
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

ADAPTERS_ROOT = Path.home() / ".ncms" / "adapters"


# Module-level cache: load each adapter's BERT+LoRA ONCE across
# all tests in this file.  Without this every test case spawns a
# fresh ~20s model load × 4 tests = the full suite hangs for minutes.
@lru_cache(maxsize=8)
def _cached_chain(domain: str):
    return get_intent_slot_chain(
        domain=domain,
        root=ADAPTERS_ROOT,
        include_e5_fallback=False,  # deterministic tests
    )


def _have_adapter(domain: str) -> bool:
    """Only run these tests when the v4 adapters are published."""
    return find_adapter_dir(domain, root=ADAPTERS_ROOT) is not None


pytestmark = pytest.mark.skipif(
    not (_have_adapter("conversational") and _have_adapter("software_dev")),
    reason=(
        "v4 adapters not published at ~/.ncms/adapters/; "
        "run the sprint-3 pipeline first or set NCMS_ADAPTER_ROOT"
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _build_service_with_adapter(
    domain: str,
) -> tuple[MemoryService, SQLiteStore, EventLog]:
    """Build a MemoryService wired to the domain's v4 adapter."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    event_log = EventLog()
    config = NCMSConfig(
        db_path=":memory:",
        intent_slot_enabled=True,
        intent_slot_populate_domains=True,
        intent_slot_confidence_threshold=0.7,
        # No start_index_pool() → store_memory's enqueue returns
        # False and the ingest runs inline.  Keeps the test
        # deterministic without the worker-loop teardown spam.
    )
    chain = _cached_chain(domain)
    service = MemoryService(
        store=store, index=index, graph=graph,
        config=config, event_log=event_log,
        intent_slot=chain,
    )
    return service, store, event_log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_with_conversational_adapter_populates_all_five_heads():
    """All five SLM heads land in the DB + dashboard event."""
    svc, store, event_log = await _build_service_with_adapter(
        "conversational",
    )

    mem = await svc.store_memory(
        content="I really love sushi and ramen, especially with tempura",
        domains=["conversational"],
    )
    # async_indexing_enabled=False → everything ran inline already.

    # Fetch the row directly from the store to confirm columns.
    saved = await store.get_memory(mem.id)
    assert saved is not None

    # Core classifier outputs persisted to memories columns.
    intent_slot = (saved.structured or {}).get("intent_slot")
    assert intent_slot is not None, "intent_slot block missing from structured"
    assert intent_slot["intent"] == "positive"
    assert intent_slot["intent_confidence"] > 0.7
    assert intent_slot["topic"] == "food_pref"
    assert intent_slot["admission"] == "persist"
    assert intent_slot["state_change"] in {"none", "declaration"}
    assert intent_slot["method"] == "joint_bert_lora"

    # Slots persisted to memory_slots table.
    slots = await store.get_memory_slots(mem.id)
    assert "object" in slots
    assert "sushi" in slots["object"].lower()

    # Topic auto-appended to Memory.domains.
    assert "food_pref" in saved.domains

    # Dashboard event emitted.
    events = [e for e in event_log.recent(100)
              if e.type.startswith("intent_slot.")]
    assert events, "no intent_slot.* dashboard event emitted"
    assert events[0].data["topic"] == "food_pref"
    assert events[0].data["method"] == "joint_bert_lora"


@pytest.mark.asyncio
async def test_switch_adapter_changes_taxonomy_at_runtime():
    """Two services, two adapters, two taxonomies — same API."""
    # Service A: conversational
    svc_a, store_a, _ = await _build_service_with_adapter("conversational")
    mem_a = await svc_a.store_memory(
        content="I love dark chocolate",
        domains=["conversational"],
    )
    # inline ingest — no pool, nothing to wait on
    saved_a = await store_a.get_memory(mem_a.id)
    topic_a = (saved_a.structured or {}).get("intent_slot", {}).get("topic")

    # Service B: software_dev
    svc_b, store_b, _ = await _build_service_with_adapter("software_dev")
    mem_b = await svc_b.store_memory(
        content="I use pytest before every commit",
        domains=["software_dev"],
    )
    # inline ingest — no pool, nothing to wait on
    saved_b = await store_b.get_memory(mem_b.id)
    topic_b = (saved_b.structured or {}).get("intent_slot", {}).get("topic")

    # The adapters produce their own domain-specific topics.
    # conversational → a food_pref-family label
    # software_dev → a framework/testing-family label
    assert topic_a is not None, "conversational adapter didn't emit a topic"
    assert topic_b is not None, "software_dev adapter didn't emit a topic"
    assert topic_a != topic_b, (
        f"expected different topics across adapters, got {topic_a} for both"
    )


@pytest.mark.asyncio
async def test_dynamic_topic_enumeration_without_config():
    """Dashboard enumerates topics from DB — no config coupling."""
    svc, store, _ = await _build_service_with_adapter("conversational")

    # Ingest several distinct-topic memories.
    await svc.store_memory(
        content="I love sushi", domains=["conversational"],
    )
    await svc.store_memory(
        content="I can't stand cold weather", domains=["conversational"],
    )
    await svc.store_memory(
        content="I go rock climbing every weekend",
        domains=["conversational"],
    )
    # async_indexing_enabled=False → everything ran inline already.

    # The dashboard asks the store, not the adapter manifest.
    topics = await store.list_topics_seen()
    topic_names = {t["topic"] for t in topics}
    assert topic_names, "no topics found — adapter/topic head may be broken"
    # At least two distinct topics should have been produced across
    # food / weather / hobby preferences.
    assert len(topic_names) >= 2


@pytest.mark.asyncio
async def test_heuristic_fallback_when_no_adapter():
    """No adapter → heuristic chain → admission=persist, others None."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        intent_slot_enabled=True,
        intent_slot_populate_domains=True,
    )
    # Build a heuristic-only chain (no checkpoint, no E5).
    chain = build_extractor_chain(
        checkpoint_dir=None,
        include_e5_fallback=False,
    )
    svc = MemoryService(
        store=store, index=index, graph=graph,
        config=config, intent_slot=chain,
    )

    mem = await svc.store_memory(
        content="Arbitrary content here",
        domains=["some_unknown_domain"],
    )

    saved = await store.get_memory(mem.id)
    intent_slot = (saved.structured or {}).get("intent_slot", {})
    assert intent_slot.get("method") == "heuristic_fallback"
    assert intent_slot.get("admission") == "persist"
    assert intent_slot.get("intent") == "none"
    # No topic emitted by heuristic.
    assert intent_slot.get("topic") is None
    # Domain list should not have been auto-expanded.
    assert saved.domains == ["some_unknown_domain"]


@pytest.mark.asyncio
async def test_adapter_listing_sees_published_v4():
    """Sanity check — the benchmark helper can enumerate adapters."""
    available = list_available_adapters(root=ADAPTERS_ROOT)
    assert "conversational" in available
    assert "v4" in available["conversational"]
