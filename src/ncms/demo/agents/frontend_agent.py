"""Frontend Agent - knows about UI components and client-side patterns."""

from __future__ import annotations

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent


class FrontendAgent(KnowledgeAgent):
    """Agent responsible for building UI components."""

    def declare_expertise(self) -> list[str]:
        return ["frontend", "ui:components", "ui:pages"]

    def declare_subscriptions(self) -> list[str]:
        return ["api", "api:user-service", "api:auth-service"]

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        results = await self._memory.search(
            query=ask.question,
            domain="frontend",
            limit=3,
            agent_id=self.agent_id,
        )

        if results:
            best = results[0]
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent=self.agent_id,
                confidence=min(0.90, best.total_activation / 10 + 0.5),
                knowledge=KnowledgePayload(
                    type="code-snippet",
                    content=best.memory.content,
                    structured=best.memory.structured,
                ),
                provenance=KnowledgeProvenance(
                    source="memory-store",
                    trust_level="observed",
                ),
                source_mode="live",
            )

        return None

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        memories = await self._memory.list_memories(domain="frontend", agent_id=self.agent_id)
        return [
            SnapshotEntry(
                domain="frontend",
                knowledge=KnowledgePayload(type=m.type, content=m.content),
                confidence=0.85,
                volatility="changing",
            )
            for m in memories[:20]
        ]
