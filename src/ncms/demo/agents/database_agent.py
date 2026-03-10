"""Database Agent - knows about schemas, migrations, and data models."""

from __future__ import annotations

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent


class DatabaseAgent(KnowledgeAgent):
    """Agent responsible for database schemas and migrations."""

    def declare_expertise(self) -> list[str]:
        return ["db", "db:user-schema", "db:auth-schema", "db:migrations"]

    def declare_subscriptions(self) -> list[str]:
        return ["api", "config"]

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        results = await self._memory.search(
            query=ask.question,
            domain="db",
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
                ),
                provenance=KnowledgeProvenance(
                    source="memory-store",
                    trust_level="authoritative",
                ),
                source_mode="live",
            )

        return None

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        memories = await self._memory.list_memories(domain="db", agent_id=self.agent_id)
        return [
            SnapshotEntry(
                domain="db",
                knowledge=KnowledgePayload(type=m.type, content=m.content),
                confidence=0.95,
                volatility="stable",
            )
            for m in memories[:20]
        ]
