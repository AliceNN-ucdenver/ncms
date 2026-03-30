# SPDX-License-Identifier: Apache-2.0
"""Tests for NCMSMemoryEditor."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nat.memory.models import MemoryItem
from nat.plugins.ncms.config import NCMSMemoryConfig
from nat.plugins.ncms.editor import NCMSMemoryEditor


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.recall_memory = AsyncMock(return_value=[
        {
            "memory": {
                "memory_id": "mem-1",
                "content": "Use JWT with RS256",
                "type": "architecture-decision",
                "domains": ["architecture"],
                "total_activation": 0.9,
            },
        },
        {
            "memory": {
                "memory_id": "mem-2",
                "content": "PostgreSQL for persistence",
                "type": "architecture-decision",
                "domains": ["architecture"],
                "total_activation": 0.7,
            },
        },
    ])
    client.search_memory = AsyncMock(return_value=[
        {
            "memory_id": "mem-3",
            "content": "Fallback result",
            "domains": ["architecture"],
            "bm25_score": 5.0,
        },
    ])
    client.store_memory = AsyncMock(return_value={"memory_id": "new-1"})
    client.delete_memory = AsyncMock(return_value={"deleted": True})
    return client


@pytest.fixture
def config():
    return NCMSMemoryConfig(
        hub_url="http://localhost:9080",
        agent_id="test-agent",
        domains=["test"],
        recall_limit=10,
    )


@pytest.fixture
def editor(mock_client, config):
    return NCMSMemoryEditor(client=mock_client, config=config)


class TestSearch:
    async def test_returns_memory_items(self, editor, mock_client):
        results = await editor.search("What auth pattern?", top_k=5, user_id="builder")
        assert len(results) == 2
        assert results[0].memory == "Use JWT with RS256"
        assert results[0].similarity_score == 0.9
        assert results[1].memory == "PostgreSQL for persistence"
        mock_client.recall_memory.assert_awaited_once_with(
            query="What auth pattern?", domain=None, limit=5,
        )

    async def test_falls_back_to_search(self, editor, mock_client):
        mock_client.recall_memory.side_effect = Exception("recall failed")
        results = await editor.search("test query", top_k=3)
        assert len(results) == 1
        assert results[0].memory == "Fallback result"
        mock_client.search_memory.assert_awaited_once()

    async def test_returns_empty_on_total_failure(self, editor, mock_client):
        mock_client.recall_memory.side_effect = Exception("fail")
        mock_client.search_memory.side_effect = Exception("also fail")
        results = await editor.search("broken query")
        assert results == []

    async def test_passes_domain(self, editor, mock_client):
        await editor.search("test", domain="security")
        mock_client.recall_memory.assert_awaited_once_with(
            query="test", domain="security", limit=10,
        )


class TestAddItems:
    async def test_stores_single_item(self, editor, mock_client):
        item = MemoryItem(
            user_id="builder",
            memory="Decided on JWT",
            tags=["architecture"],
            metadata={"type": "architecture-decision"},
        )
        await editor.add_items([item])
        mock_client.store_memory.assert_awaited_once()
        call_kwargs = mock_client.store_memory.call_args
        assert call_kwargs.kwargs["content"] == "Decided on JWT"

    async def test_stores_multiple_items(self, editor, mock_client):
        items = [
            MemoryItem(user_id="b", memory="Fact 1", metadata={}),
            MemoryItem(user_id="b", memory="Fact 2", metadata={}),
        ]
        await editor.add_items(items)
        assert mock_client.store_memory.await_count == 2

    async def test_continues_on_failure(self, editor, mock_client):
        mock_client.store_memory.side_effect = [Exception("fail"), {"memory_id": "ok"}]
        items = [
            MemoryItem(user_id="b", memory="Will fail", metadata={}),
            MemoryItem(user_id="b", memory="Will succeed", metadata={}),
        ]
        await editor.add_items(items)
        assert mock_client.store_memory.await_count == 2


class TestRemoveItems:
    async def test_deletes_by_memory_id(self, editor, mock_client):
        await editor.remove_items(memory_id="mem-1")
        mock_client.delete_memory.assert_awaited_once_with("mem-1")

    async def test_noop_without_memory_id(self, editor, mock_client):
        await editor.remove_items(user_id="someone")
        mock_client.delete_memory.assert_not_awaited()
