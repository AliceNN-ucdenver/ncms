"""NVIDIA Agent Toolkit memory provider backed by NCMS.

Replaces Zep/Mem0/Redis as the NAT memory backend, providing:
- Hybrid retrieval (BM25 + SPLADE + Graph) instead of vector search
- Entity extraction and knowledge graph
- Episode formation and state reconciliation
- Structured recall with context enrichment

Installation:
    pip install ncms nvidia-nat

Registration:
    from ncms.integrations.nat_memory import NCMSMemoryEditor
    builder.register_memory("ncms", NCMSMemoryEditor)

Usage:
    memory = builder.get_memory_client("ncms")
    await memory.add(items=[MemoryItem(text="API uses OAuth2", metadata={...})])
    results = await memory.search(query="authentication method", limit=5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """A memory item compatible with NAT's MemoryItem interface.

    This is a standalone dataclass so NCMS can be used without
    installing nvidia-nat. When nvidia-nat is installed, NAT's
    own MemoryItem can be used interchangeably.
    """

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    score: float = 0.0


class NCMSMemoryEditor:
    """NVIDIA Agent Toolkit MemoryEditor backed by NCMS.

    Implements the NAT MemoryEditor interface (add/search/remove)
    delegating to NCMS MemoryService for cognitive memory operations.

    Can be used standalone or registered as a NAT memory provider:

        # Standalone
        editor = NCMSMemoryEditor()
        await editor.initialize()
        await editor.add([MemoryItem(text="fact")])
        results = await editor.search("query")

        # As NAT provider
        builder.register_memory("ncms", NCMSMemoryEditor)
        memory = builder.get_memory_client("ncms")
    """

    def __init__(
        self,
        db_path: str | None = None,
        index_path: str | None = None,
        config_overrides: dict[str, Any] | None = None,
    ):
        self._db_path = db_path
        self._index_path = index_path
        self._config_overrides = config_overrides or {}
        self._memory_svc: Any = None
        self._bus_svc: Any = None
        self._snapshot_svc: Any = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize NCMS services. Called automatically on first use."""
        if self._initialized:
            return

        from ncms.config import NCMSConfig
        from ncms.interfaces.mcp.server import create_ncms_services

        kwargs: dict[str, Any] = {}
        if self._db_path:
            kwargs["db_path"] = self._db_path
        if self._index_path:
            kwargs["index_path"] = self._index_path
        kwargs.update(self._config_overrides)

        config = NCMSConfig(**kwargs)
        services = await create_ncms_services(config)
        self._memory_svc = services[0]
        self._bus_svc = services[1]
        self._snapshot_svc = services[2]
        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def add(
        self,
        items: list[MemoryItem] | list[Any],
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[str]:
        """Store memory items into NCMS.

        Args:
            items: List of MemoryItem (or NAT MemoryItem) objects.
            conversation_id: Maps to NCMS domain for scoping.
            agent_id: Source agent identifier.

        Returns:
            List of created memory IDs.
        """
        await self._ensure_initialized()

        memory_ids = []
        for item in items:
            text = item.text if hasattr(item, "text") else str(item)
            metadata = item.metadata if hasattr(item, "metadata") else {}

            domains: list[str] = []
            if conversation_id:
                domains.append(conversation_id)
            if "domain" in metadata:
                d = metadata["domain"]
                if isinstance(d, list):
                    domains.extend(d)
                else:
                    domains.append(str(d))

            memory = await self._memory_svc.store_memory(
                content=text,
                memory_type=metadata.get("type", "fact"),
                domains=domains or None,
                tags=metadata.get("tags"),
                importance=metadata.get("importance", 5.0),
                source_agent=agent_id or metadata.get("source_agent"),
                structured=metadata.get("structured"),
            )
            memory_ids.append(memory.id)

        return memory_ids

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        conversation_id: str | None = None,
        use_recall: bool = True,
    ) -> list[MemoryItem]:
        """Search NCMS memory using hybrid retrieval.

        Args:
            query: Search query text.
            limit: Maximum results to return.
            conversation_id: Filter by domain (maps from NAT conversation_id).
            use_recall: If True, use structured recall (richer context).
                       If False, use flat search.

        Returns:
            List of MemoryItem results with scores.
        """
        await self._ensure_initialized()

        domain = conversation_id

        if use_recall:
            results = await self._memory_svc.recall(
                query=query, domain=domain, limit=limit,
            )
            return [
                MemoryItem(
                    text=r.memory.memory.content,
                    metadata={
                        "memory_id": r.memory.memory.id,
                        "domains": r.memory.memory.domains,
                        "retrieval_path": r.retrieval_path,
                        "episode": {
                            "id": r.context.episode.episode_id,
                            "title": r.context.episode.episode_title,
                        } if r.context.episode else None,
                        "entity_states": [
                            {
                                "entity": s.entity_name,
                                "key": s.state_key,
                                "value": s.state_value,
                            }
                            for s in r.context.entity_states
                        ] if r.context.entity_states else [],
                    },
                    id=r.memory.memory.id,
                    score=r.memory.total_activation,
                )
                for r in results
            ]
        else:
            results = await self._memory_svc.search(
                query=query, domain=domain, limit=limit,
            )
            return [
                MemoryItem(
                    text=r.memory.content,
                    metadata={
                        "memory_id": r.memory.id,
                        "domains": r.memory.domains,
                    },
                    id=r.memory.id,
                    score=r.total_activation,
                )
                for r in results
            ]

    async def remove(self, memory_ids: list[str]) -> int:
        """Remove memories from NCMS.

        Args:
            memory_ids: List of memory IDs to delete.

        Returns:
            Number of memories successfully deleted.
        """
        await self._ensure_initialized()

        deleted = 0
        for mid in memory_ids:
            if await self._memory_svc.delete(mid):
                deleted += 1
        return deleted

    # -- Knowledge Bus integration -----------------------------------------

    async def ask(
        self,
        question: str,
        *,
        domains: list[str] | None = None,
        from_agent: str = "nat-client",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """Ask a question on the NCMS Knowledge Bus.

        Routes to live agents or falls back to surrogate responses.
        """
        await self._ensure_initialized()

        from ncms.domain.models import KnowledgeAsk

        ask_obj = KnowledgeAsk(
            question=question,
            domains=domains or [],
            from_agent=from_agent,
        )
        response = await self._bus_svc.ask_sync(ask_obj, timeout_ms=timeout_ms)
        if response is None:
            return {"answered": False}

        return {
            "answered": True,
            "content": response.knowledge.content,
            "from_agent": response.from_agent,
            "source_mode": response.source_mode,
            "confidence": response.confidence,
        }

    async def announce(
        self,
        content: str,
        *,
        domains: list[str],
        from_agent: str = "nat-client",
        event: Literal["created", "updated", "deprecated", "breaking-change"] = "updated",
    ) -> None:
        """Broadcast an announcement on the NCMS Knowledge Bus."""
        await self._ensure_initialized()

        from ncms.domain.models import KnowledgeAnnounce, KnowledgePayload

        announcement = KnowledgeAnnounce(
            knowledge=KnowledgePayload(content=content),
            domains=domains,
            from_agent=from_agent,
            event=event,
        )
        await self._bus_svc.announce(announcement)
