"""API Agent - knows about REST endpoints and API contracts."""

from __future__ import annotations

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent


class ApiAgent(KnowledgeAgent):
    """Agent responsible for building and maintaining API endpoints."""

    def declare_expertise(self) -> list[str]:
        return ["api", "api:user-service", "api:auth-service"]

    def declare_subscriptions(self) -> list[str]:
        return ["db", "db:user-schema", "config"]

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        # Search our memory for relevant knowledge
        results = await self._memory.search(
            query=ask.question,
            domain="api",
            limit=3,
            agent_id=self.agent_id,
        )

        if results:
            best = results[0]
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent=self.agent_id,
                confidence=min(0.95, best.total_activation / 10 + 0.5),
                knowledge=KnowledgePayload(
                    type="interface-spec",
                    content=best.memory.content,
                    structured=best.memory.structured,
                    references=[f"memory:{best.memory.id}"],
                ),
                provenance=KnowledgeProvenance(
                    source="memory-store",
                    trust_level="authoritative",
                ),
                source_mode="live",
            )

        return None

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        """Export current API knowledge for snapshot."""
        memories = await self._memory.list_memories(domain="api", agent_id=self.agent_id)
        return [
            SnapshotEntry(
                domain="api",
                knowledge=KnowledgePayload(
                    type=m.type,
                    content=m.content,
                    structured=m.structured,
                ),
                confidence=0.9,
                volatility="changing",
            )
            for m in memories[:20]
        ]
