# SPDX-License-Identifier: Apache-2.0
"""Tests for NAT MemoryItem ↔ NCMS Memory mapping."""

from __future__ import annotations

from nat.memory.models import MemoryItem
from nat.plugins.ncms.models import (
    _format_conversation,
    memory_item_to_ncms_store,
    ncms_recall_to_memory_item,
    ncms_search_to_memory_item,
)


class TestNcmsRecallToMemoryItem:
    def test_basic_mapping(self):
        result = {
            "memory": {
                "memory_id": "mem-1",
                "content": "JWT with RS256 for token signing",
                "type": "architecture-decision",
                "domains": ["architecture", "security"],
                "source_agent": "architect",
                "importance": 8.0,
                "total_activation": 0.85,
            },
            "episode": {"id": "ep-1", "title": "Auth design"},
            "entity_states": [{"entity": "jwt", "state": "RS256"}],
            "causal_chain": {"supersedes": ["mem-0"]},
            "retrieval_path": "fact_lookup",
        }
        item = ncms_recall_to_memory_item(result, user_id="builder")

        assert item.user_id == "builder"
        assert item.memory == "JWT with RS256 for token signing"
        assert "architecture" in item.tags
        assert item.metadata["memory_id"] == "mem-1"
        assert item.metadata["type"] == "architecture-decision"
        assert item.metadata["episode"] == {"id": "ep-1", "title": "Auth design"}
        assert item.similarity_score == 0.85

    def test_flat_result_without_memory_wrapper(self):
        result = {
            "memory_id": "mem-2",
            "content": "Use bcrypt for password hashing",
            "domains": ["security"],
            "bm25_score": 12.5,
        }
        item = ncms_recall_to_memory_item(result, user_id="test")
        assert item.memory == "Use bcrypt for password hashing"
        assert item.similarity_score == 12.5


class TestNcmsSearchToMemoryItem:
    def test_basic_mapping(self):
        result = {
            "memory_id": "mem-3",
            "content": "Express.js API gateway pattern",
            "type": "architecture-decision",
            "domains": ["architecture"],
            "bm25_score": 10.2,
            "score": 15.8,
        }
        item = ncms_search_to_memory_item(result, user_id="builder")
        assert item.memory == "Express.js API gateway pattern"
        assert item.similarity_score == 15.8
        assert item.metadata["bm25_score"] == 10.2


class TestMemoryItemToNcmsStore:
    def test_from_memory_field(self):
        item = MemoryItem(
            user_id="builder",
            memory="Decided to use PostgreSQL for users table",
            tags=["identity-service", "implementation"],
            metadata={"type": "architecture-decision", "importance": 7.0},
        )
        payload = memory_item_to_ncms_store(item)
        assert payload["content"] == "Decided to use PostgreSQL for users table"
        assert payload["type"] == "architecture-decision"
        assert payload["domains"] == ["identity-service", "implementation"]
        assert payload["source_agent"] == "builder"
        assert payload["importance"] == 7.0

    def test_from_conversation(self):
        item = MemoryItem(
            user_id="builder",
            conversation=[
                {"role": "user", "content": "What auth pattern?"},
                {"role": "assistant", "content": "Use JWT with RBAC"},
            ],
            tags=["architecture"],
            metadata={},
        )
        payload = memory_item_to_ncms_store(item)
        assert "user: What auth pattern?" in payload["content"]
        assert "assistant: Use JWT with RBAC" in payload["content"]

    def test_defaults(self):
        item = MemoryItem(user_id="test", memory="Some fact", metadata={})
        payload = memory_item_to_ncms_store(item)
        assert payload["type"] == "fact"
        assert payload["importance"] == 5.0


class TestFormatConversation:
    def test_empty(self):
        assert _format_conversation(None) == ""
        assert _format_conversation([]) == ""

    def test_single_message(self):
        result = _format_conversation([{"role": "user", "content": "Hello"}])
        assert result == "user: Hello"

    def test_multi_turn(self):
        msgs = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
        result = _format_conversation(msgs)
        assert "user: Question" in result
        assert "assistant: Answer" in result
