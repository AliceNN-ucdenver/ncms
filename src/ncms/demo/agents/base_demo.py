"""Base class for demo agents with memory-backed ask/snapshot handling.

Provides default implementations of ``on_ask()`` and
``collect_working_knowledge()`` that search/list memories by domain.
Concrete demo agents only need to declare their domain configuration.
"""

from __future__ import annotations

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent


class DemoAgent(KnowledgeAgent):
    """Base class for demo agents with configurable domain behaviour.

    Subclasses set class attributes to customise response and snapshot
    parameters.  Override ``on_ask`` or ``collect_working_knowledge``
    for truly custom behaviour.
    """

    # ── Override in subclass ──────────────────────────────────────────────

    primary_domain: str = "general"
    """Domain to search/list when answering asks and collecting snapshots."""

    knowledge_type: str = "fact"
    """Knowledge payload type for ask responses."""

    trust_level: str = "authoritative"
    """Provenance trust level for ask responses."""

    max_confidence: float = 0.95
    """Confidence cap for ask responses (``min(max_confidence, computed)``). """

    snapshot_confidence: float = 0.9
    """Confidence value for snapshot entries."""

    snapshot_volatility: str = "changing"
    """Volatility marker for snapshot entries."""

    include_structured_in_snapshot: bool = False
    """Whether to include ``memory.structured`` in snapshot entries."""

    include_references_in_response: bool = False
    """Whether to include ``memory:`` references in ask response payloads."""

    # ── Default implementations ───────────────────────────────────────────

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Search memory for relevant knowledge and return the best match."""
        results = await self._memory.search(
            query=ask.question,
            domain=self.primary_domain,
            limit=3,
            agent_id=self.agent_id,
        )

        if not results:
            return None

        best = results[0]

        payload_kwargs: dict[str, object] = {
            "type": self.knowledge_type,
            "content": best.memory.content,
            "structured": best.memory.structured,
        }
        if self.include_references_in_response:
            payload_kwargs["references"] = [f"memory:{best.memory.id}"]

        return KnowledgeResponse(
            ask_id=ask.ask_id,
            from_agent=self.agent_id,
            confidence=min(self.max_confidence, best.total_activation / 10 + 0.5),
            knowledge=KnowledgePayload(**payload_kwargs),  # type: ignore[arg-type]
            provenance=KnowledgeProvenance(
                source="memory-store",
                trust_level=self.trust_level,
            ),
            source_mode="live",
        )

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        """Export current domain knowledge for snapshot publication."""
        memories = await self._memory.list_memories(
            domain=self.primary_domain, agent_id=self.agent_id,
        )

        entries: list[SnapshotEntry] = []
        for m in memories[:20]:
            payload_kwargs: dict[str, object] = {
                "type": m.type,
                "content": m.content,
            }
            if self.include_structured_in_snapshot:
                payload_kwargs["structured"] = m.structured

            entries.append(
                SnapshotEntry(
                    domain=self.primary_domain,
                    knowledge=KnowledgePayload(**payload_kwargs),  # type: ignore[arg-type]
                    confidence=self.snapshot_confidence,
                    volatility=self.snapshot_volatility,
                )
            )
        return entries
