# SPDX-License-Identifier: Apache-2.0
"""NCMSMemoryEditor — implements NAT MemoryEditor backed by NCMS Hub API."""

from __future__ import annotations

import logging
from typing import Any

from nat.memory.interfaces import MemoryEditor
from nat.memory.models import MemoryItem

from .config import NCMSMemoryConfig
from .http_client import NCMSHttpClient
from .models import (
    memory_item_to_ncms_store,
    ncms_recall_to_memory_item,
    ncms_search_to_memory_item,
)

logger = logging.getLogger(__name__)


class NCMSMemoryEditor(MemoryEditor):
    """NAT MemoryEditor that delegates to an NCMS Hub via HTTP.

    Uses NCMS's ``/memories/recall`` endpoint for search — this gives
    BM25 + SPLADE + Graph spreading activation retrieval with episode
    context, entity states, and causal chains.  No dense vectors needed.

    The ``search()`` method receives raw query text (not embeddings),
    which maps directly to NCMS's text-based retrieval pipeline.
    """

    def __init__(self, client: NCMSHttpClient, config: NCMSMemoryConfig) -> None:
        self._client = client
        self._config = config

    async def add_items(self, items: list[MemoryItem], **kwargs: Any) -> None:
        """Store memories in the NCMS Hub."""
        for item in items:
            payload = memory_item_to_ncms_store(item)
            try:
                await self._client.store_memory(**payload)
            except Exception:
                logger.exception("Failed to store memory in NCMS Hub")

    async def search(
        self, query: str, top_k: int = 5, **kwargs: Any
    ) -> list[MemoryItem]:
        """Search NCMS Hub using BM25+SPLADE+Graph retrieval.

        Tries ``/memories/recall`` first (rich context), falls back to
        ``/memories/search`` (flat results).
        """
        user_id = kwargs.get("user_id", "default")
        domain = kwargs.get("domain")
        limit = top_k or self._config.recall_limit

        # Truncate to prevent 431 errors from auto_memory passing large texts
        if len(query) > 2000:
            logger.info("[editor] Search query truncated: %d → 2000 chars", len(query))
        truncated = query[:2000]

        try:
            results = await self._client.recall_memory(
                query=truncated, domain=domain, limit=limit,
            )
            return [ncms_recall_to_memory_item(r, user_id) for r in results]
        except Exception:
            logger.debug("recall_memory failed, falling back to search", exc_info=True)
            try:
                results = await self._client.search_memory(
                    query=truncated, domain=domain, limit=limit,
                )
                return [ncms_search_to_memory_item(r, user_id) for r in results]
            except Exception:
                logger.exception("search_memory also failed")
                return []

    async def remove_items(self, **kwargs: Any) -> None:
        """Remove items from NCMS Hub by memory_id."""
        memory_id = kwargs.get("memory_id")
        if memory_id:
            try:
                await self._client.delete_memory(memory_id)
            except Exception:
                logger.exception("Failed to delete memory %s", memory_id)
