"""Integration tests for Phase 3: Keyword Bridge Nodes.

Tests that keyword extraction creates Entity(type="keyword") nodes,
links them to memories, and enables graph expansion to discover
related memories via shared keywords.

All tests mock litellm.acompletion to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

_PATCH_TARGET = "litellm.acompletion"


def _mock_llm_response(keywords: list[dict]) -> MagicMock:
    """Create a mock litellm acompletion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(keywords)
    return response


async def _create_keyword_service(**overrides) -> tuple[MemoryService, SQLiteStore]:
    """Create a MemoryService with keyword bridge nodes enabled.

    Returns (service, store) — caller should ``await store.close()`` when done.
    """
    defaults: dict = dict(
        db_path=":memory:",
        actr_noise=0.0,
        keyword_bridge_enabled=True,
        keyword_max_per_memory=8,
        keyword_llm_model="gpt-4o-mini",
        graph_expansion_enabled=True,
        graph_expansion_depth=1,
        graph_expansion_max=10,
    )
    defaults.update(overrides)
    config = NCMSConfig(**defaults)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    return MemoryService(store=store, index=index, graph=graph, config=config), store


class TestKeywordBridges:
    """Tests for keyword bridge node integration."""

    @pytest.mark.asyncio
    async def test_keywords_stored_as_entities(self):
        """Keyword extraction should create Entity(type='keyword') nodes."""
        svc, store = await _create_keyword_service()
        try:
            mock_keywords = [
                {"name": "security", "domain": "auth"},
                {"name": "access control", "domain": "auth"},
            ]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(mock_keywords),
            ):
                await svc.store_memory(
                    content="JWT authentication with role-based access control",
                    domains=["auth"],
                )

            # Verify keyword entities were created
            entities = await svc.list_entities(entity_type="keyword")
            keyword_names = {e.name.lower() for e in entities}
            assert "security" in keyword_names
            assert "access control" in keyword_names
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_keywords_linked_to_memory(self):
        """Keyword entities should be linked to the source memory via memory_entities."""
        svc, store = await _create_keyword_service()
        try:
            mock_keywords = [
                {"name": "data persistence", "domain": "storage"},
            ]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(mock_keywords),
            ):
                memory = await svc.store_memory(
                    content="PostgreSQL database stores user profiles",
                    domains=["db"],
                )

            # Verify the keyword entity is linked to the memory in the graph
            entity_ids = svc.graph.get_entity_ids_for_memory(memory.id)
            assert len(entity_ids) >= 1, "Memory should have at least one linked entity"

            # Find the keyword entity and confirm it's linked
            kw_entity = await store.find_entity_by_name("data persistence")
            assert kw_entity is not None
            assert kw_entity.id in entity_ids
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_shared_keyword_bridges_memories(self):
        """Two memories sharing a keyword should be discoverable via graph expansion."""
        svc, store = await _create_keyword_service()
        try:
            # Memory A: about JWT authentication — keyword "security"
            keywords_a = [{"name": "security", "domain": "auth"}]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(keywords_a),
            ):
                mem_a = await svc.store_memory(
                    content="JWT token validation for API endpoints",
                    domains=["auth"],
                )

            # Memory B: about RBAC — same keyword "security"
            keywords_b = [{"name": "security", "domain": "auth"}]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(keywords_b),
            ):
                mem_b = await svc.store_memory(
                    content="Role-based access control for admin dashboard",
                    domains=["auth"],
                )

            # Query that BM25-matches mem_a (JWT token validation)
            # Graph expansion should discover mem_b via shared "security" keyword
            results = await svc.search("JWT token validation")
            result_ids = [r.memory.id for r in results]

            assert mem_a.id in result_ids, "BM25 hit should be in results"
            assert mem_b.id in result_ids, (
                "Memory sharing keyword 'security' should be discovered via graph expansion"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_keywords_disabled_no_extraction(self):
        """With keyword_bridge_enabled=False, no keyword entities should be created."""
        svc, store = await _create_keyword_service(keyword_bridge_enabled=False)
        try:
            await svc.store_memory(
                content="JWT authentication with role-based access control",
                domains=["auth"],
            )

            # No keyword entities should exist
            keyword_entities = await svc.list_entities(entity_type="keyword")
            assert len(keyword_entities) == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_keywords_dedup_with_entities(self):
        """Keywords should not duplicate existing auto-extracted entities."""
        svc, store = await _create_keyword_service()
        try:
            # LLM returns "JWT" which is already auto-extracted as a technology entity
            mock_keywords = [
                {"name": "JWT", "domain": "auth"},
                {"name": "token management", "domain": "auth"},
            ]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(mock_keywords),
            ):
                await svc.store_memory(
                    content="JWT authentication for API security",
                    domains=["auth"],
                )

            # "JWT" should exist as a technology entity (from regex), not duplicated
            jwt_entity = await store.find_entity_by_name("JWT")
            assert jwt_entity is not None
            assert jwt_entity.type == "technology"  # Original type preserved

            # "token management" should exist as a keyword entity
            tm_entity = await store.find_entity_by_name("token management")
            assert tm_entity is not None
            assert tm_entity.type == "keyword"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_keyword_max_cap(self):
        """Should respect keyword_max_per_memory configuration."""
        svc, store = await _create_keyword_service(keyword_max_per_memory=3)
        try:
            mock_keywords = [
                {"name": f"concept_{i}", "domain": "test"} for i in range(10)
            ]
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_mock_llm_response(mock_keywords),
            ):
                await svc.store_memory(
                    content="A complex memory with many potential keywords",
                    domains=["test"],
                )

            keyword_entities = await svc.list_entities(entity_type="keyword")
            assert len(keyword_entities) <= 3
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_keyword_extraction_failure_non_fatal(self):
        """LLM error during keyword extraction should not break store_memory."""
        svc, store = await _create_keyword_service()
        try:
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM API unavailable"),
            ):
                # This should NOT raise — keyword failure is non-fatal
                memory = await svc.store_memory(
                    content="PostgreSQL database with Redis caching",
                    domains=["db"],
                )

            # Memory should still be stored successfully
            assert memory.id is not None
            retrieved = await svc.get_memory(memory.id)
            assert retrieved is not None
            assert retrieved.content == "PostgreSQL database with Redis caching"

            # Regular entities should still be extracted (regex works independently)
            entities = await svc.list_entities()
            entity_names = {e.name.lower() for e in entities}
            assert "postgresql" in entity_names or "redis" in entity_names
        finally:
            await store.close()
