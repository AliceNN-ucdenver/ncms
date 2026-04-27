from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import ExtractedLabel
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


class _FakeIntentSlot:
    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        return ExtractedLabel(
            intent="none",
            intent_confidence=0.95,
            slots={"framework": "FastAPI"},
            slot_confidences={"framework": 0.95},
            topic="framework",
            topic_confidence=0.95,
            admission="persist",
            admission_confidence=0.99,
            state_change="declaration",
            state_change_confidence=0.98,
            role_spans=[
                {
                    "char_start": text.index("FastAPI"),
                    "char_end": text.index("FastAPI") + len("FastAPI"),
                    "surface": "FastAPI",
                    "canonical": "FastAPI",
                    "slot": "framework",
                    "role": "primary",
                    "source": "test",
                }
            ],
            method="joint_bert_lora",
        )


async def _build_service() -> tuple[MemoryService, SQLiteStore]:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    config = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
        entity_extraction_mode="slm_only",
        admission_enabled=False,
        splade_enabled=False,
        scoring_weight_splade=0.0,
        index_workers=1,
    )
    svc = MemoryService(
        store=store,
        index=index,
        graph=NetworkXGraph(),
        config=config,
        splade=None,
        intent_slot=_FakeIntentSlot(),
    )
    return svc, store


async def _ingest(async_indexing: bool) -> tuple[list[str], list[tuple[str, dict]]]:
    svc, store = await _build_service()
    try:
        if async_indexing:
            await svc.start_index_pool()
        memory = await svc.store_memory(
            "svc-api now uses FastAPI as the primary framework.",
            domains=["software_dev"],
            source_agent="test",
            subject="svc-api",
        )
        if async_indexing:
            await svc.flush_indexing(poll_interval=0.01)

        entity_names = sorted(await store.get_memory_entity_names(memory.id))
        nodes = await store.get_memory_nodes_for_memory(memory.id)
        node_summary = sorted((node.node_type.value, node.metadata) for node in nodes)
        return entity_names, node_summary
    finally:
        await svc.stop_index_pool()
        await store.close()


@pytest.mark.asyncio
async def test_inline_and_async_indexing_have_entity_and_l2_parity() -> None:
    inline_entities, inline_nodes = await _ingest(async_indexing=False)
    async_entities, async_nodes = await _ingest(async_indexing=True)

    assert async_entities == inline_entities == ["FastAPI", "svc-api"]
    assert async_nodes == inline_nodes
    assert [node_type for node_type, _ in inline_nodes] == ["atomic", "entity_state"]
