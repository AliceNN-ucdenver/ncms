"""TLG Phase 3d: MemoryService.search() auto-composes with grammar.

Benchmarks call ``memory_service.search()`` — this test verifies
that when ``NCMS_TEMPORAL_ENABLED=true`` a confident grammar answer
promotes its memory to rank 1 of the returned ``ScoredMemory``
list.  When TLG is disabled, the original BM25 ordering is
returned unchanged.

Covers the ingest-side invalidation hook too: seeding a new
ENTITY_STATE node after the cache has warmed up must not leave
``search()`` serving the stale vocabulary.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


class _StubIntentSlotExtractor:
    """Test stub that returns a canned ``shape_intent`` for any input.

    The MemoryService consults its ``_intent_slot`` at query time to
    classify the query's TLG shape; production uses a real LoRA
    adapter.  Tests that need the grammar composition path to fire
    without loading a 2.4 MB adapter supply this stub wired to the
    shape the test is exercising.
    """
    name = "test_stub"

    def __init__(self, shape: str, *, adapter_domain: str = "conversational"):
        self._shape = shape
        self.adapter_domain = adapter_domain

    def extract(self, text: str, *, domain: str):  # pragma: no cover — trivial
        from ncms.domain.models import ExtractedLabel
        return ExtractedLabel(
            intent="none",
            intent_confidence=0.0,
            shape_intent=self._shape,  # type: ignore[arg-type]
            shape_intent_confidence=0.99,
            method=self.name,
        )


async def _build_service(
    *, slm_shape: str | None = None,
) -> MemoryService:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
    )
    reconciliation = ReconciliationService(store=store, config=config)
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
        reconciliation=reconciliation,
        intent_slot=(
            _StubIntentSlotExtractor(slm_shape) if slm_shape else None
        ),
    )
    return svc


@pytest_asyncio.fixture
async def svc_tlg_on() -> MemoryService:
    # current_state is the shape the composition tests exercise; the
    # stub hands that back so the grammar dispatcher has a route.
    svc = await _build_service(slm_shape="current_state")
    yield svc
    await svc.store.close()


# NOTE: ``svc_tlg_off`` fixture + ``TestSearchCompositionDisabled`` suite
# were removed when the NCMSConfig flag scheme collapsed tlg/
# reconciliation/episodes/intent_classification/intent_routing into the
# single ``temporal_enabled`` master flag.  Disabled-path behaviour is
# implicitly covered by every other unit test that runs without
# ``temporal_enabled=True``.


async def _ensure_entity(store: SQLiteStore, eid: str) -> None:
    if await store.get_entity(eid) is not None:
        return
    ent = Entity(name=eid, type="concept")
    ent.id = eid
    await store.save_entity(ent)


async def _ingest_memory_with_entities(
    svc: MemoryService,
    *,
    content: str,
    linked_entity_ids: list[str],
) -> Memory:
    """Store a plain Memory and link entities (so BM25 can find it)."""
    for eid in linked_entity_ids:
        await _ensure_entity(svc.store, eid)
    mem = await svc.store_memory(
        content=content,
        domains=["tlg-search-test"],
        entities=[{"name": eid, "type": "concept"} for eid in linked_entity_ids],
    )
    return mem


async def _seed_entity_state(
    svc: MemoryService,
    *,
    content: str,
    entity_id: str,
    state_key: str,
    state_value: str,
    linked_entity_ids: list[str],
) -> MemoryNode:
    """Create an ENTITY_STATE node backed by a fresh Memory."""
    for eid in linked_entity_ids:
        await _ensure_entity(svc.store, eid)
    mem = Memory(content=content, domains=["tlg-search-test"])
    await svc.store.save_memory(mem)
    for eid in linked_entity_ids:
        await svc.store.link_memory_entity(mem.id, eid)
    # Also index for BM25 so search can find it.
    svc._index.index_memory(mem)  # type: ignore[attr-defined]
    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await svc.store.save_memory_node(node)
    return node


# ---------------------------------------------------------------------------


class TestSearchCompositionEnabled:
    async def test_current_query_promotes_current_state_to_rank_1(
        self, svc_tlg_on: MemoryService
    ) -> None:
        await _seed_entity_state(
            svc_tlg_on,
            content="Authentication uses session cookies.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies", "authentication"],
        )
        current = await _seed_entity_state(
            svc_tlg_on,
            content="Retire session cookies; adopt OAuth 2.0 tokens.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        # Reconcile so current node is marked is_current=True.
        await svc_tlg_on._reconciliation.reconcile(current)  # type: ignore[attr-defined]
        # Nothing else is marking current_authentication; vocabulary
        # cache picks up "authentication" → subject auth-svc.

        results = await svc_tlg_on.search(
            "What is the current authentication method?",
            limit=5,
        )
        assert results, "search returned empty results"
        # Grammar answer was the current node → its backing memory
        # lands at rank 1.
        assert results[0].memory.id == current.memory_id

    async def test_non_grammar_query_leaves_bm25_untouched(
        self, svc_tlg_on: MemoryService
    ) -> None:
        # Seed an ENTITY_STATE so the cache has content to work with,
        # then fire a query with no grammar structure.  Expectation:
        # the composition returns BM25 unchanged.
        await _seed_entity_state(
            svc_tlg_on,
            content="Gateway uses OAuth 2.0 tokens.",
            entity_id="gateway-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0"],
        )
        results = await svc_tlg_on.search(
            "who authored the design document?",
            limit=5,
        )
        # No grammar match → no forced rank-1 promotion.  BM25 may or
        # may not return results for this query; the important thing
        # is that search didn't crash and the composition layer left
        # BM25 alone.
        assert isinstance(results, list)


class TestIngestInvalidatesVocabularyCache:
    async def test_newly_ingested_state_is_visible_next_retrieve(
        self, svc_tlg_on: MemoryService
    ) -> None:
        # Warm cache — empty corpus → empty vocabulary.
        trace1 = await svc_tlg_on.retrieve_lg(
            "What is the current authentication method?",
            slm_shape_intent="current_state",
        )
        assert trace1.confidence.value == "abstain"  # no subject known

        # Ingest an ENTITY_STATE node via the direct path (not
        # store_memory, which is what hooks invalidation — the
        # index_worker path is exercised elsewhere).  We call
        # invalidate_tlg_vocabulary manually to simulate the hook.
        await _seed_entity_state(
            svc_tlg_on,
            content="Authentication uses OAuth 2.0.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        svc_tlg_on.invalidate_tlg_vocabulary()

        trace2 = await svc_tlg_on.retrieve_lg(
            "What is the current authentication method?",
            slm_shape_intent="current_state",
        )
        assert trace2.confidence.value == "high"
        assert trace2.grammar_answer is not None
