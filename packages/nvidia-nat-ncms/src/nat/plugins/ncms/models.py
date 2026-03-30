# SPDX-License-Identifier: Apache-2.0
"""Mapping between NCMS API responses and NAT MemoryItem."""

from __future__ import annotations

from typing import Any

from nat.memory.models import MemoryItem


def ncms_recall_to_memory_item(result: dict[str, Any], user_id: str) -> MemoryItem:
    """Convert an NCMS /memories/recall response item to a NAT MemoryItem.

    The recall endpoint returns enriched results with episode context,
    entity states, and causal chains. These are preserved in metadata
    so the auto_memory_wrapper can inject them as context.
    """
    memory = result.get("memory", result)
    return MemoryItem(
        user_id=user_id,
        memory=memory.get("content", ""),
        tags=memory.get("domains", []),
        metadata={
            "memory_id": memory.get("memory_id", ""),
            "type": memory.get("type"),
            "domains": memory.get("domains", []),
            "source_agent": memory.get("source_agent"),
            "importance": memory.get("importance"),
            "episode": result.get("episode"),
            "entity_states": result.get("entity_states"),
            "causal_chain": result.get("causal_chain"),
            "retrieval_path": result.get("retrieval_path"),
        },
        similarity_score=memory.get("total_activation") or memory.get("bm25_score"),
    )


def ncms_search_to_memory_item(result: dict[str, Any], user_id: str) -> MemoryItem:
    """Convert an NCMS /memories/search response item to a NAT MemoryItem."""
    return MemoryItem(
        user_id=user_id,
        memory=result.get("content", ""),
        tags=result.get("domains", []),
        metadata={
            "memory_id": result.get("memory_id", ""),
            "type": result.get("type"),
            "domains": result.get("domains", []),
            "bm25_score": result.get("bm25_score"),
            "combined_score": result.get("combined_score"),
        },
        similarity_score=result.get("combined_score") or result.get("bm25_score"),
    )


def memory_item_to_ncms_store(item: MemoryItem) -> dict[str, Any]:
    """Convert a NAT MemoryItem to an NCMS POST /memories request body."""
    content = item.memory
    if not content and item.conversation:
        content = _format_conversation(item.conversation)
    if not content:
        content = ""

    return {
        "content": content,
        "type": item.metadata.get("type", "fact"),
        "domains": item.tags or item.metadata.get("domains", []),
        "source_agent": item.user_id,
        "importance": item.metadata.get("importance", 5.0),
        "tags": item.metadata.get("tags", []),
    }


def _format_conversation(conversation: list[dict[str, str]] | None) -> str:
    """Format a conversation list into a single string for storage."""
    if not conversation:
        return ""
    parts = []
    for msg in conversation:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)
