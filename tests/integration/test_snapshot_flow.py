"""Integration tests for the sleep/wake/surrogate response cycle."""

import pytest

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent


class SimpleAgent(KnowledgeAgent):
    """Minimal agent for testing."""

    def __init__(self, agent_id, domains, bus_svc, memory_svc, snapshot_svc):
        super().__init__(agent_id, bus_svc, memory_svc, snapshot_svc)
        self._domains = domains

    def declare_expertise(self) -> list[str]:
        return self._domains

    def declare_subscriptions(self) -> list[str]:
        return []

    async def on_ask(self, ask):
        results = await self._memory.search(ask.question, limit=1)
        if results:
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent=self.agent_id,
                confidence=0.9,
                knowledge=KnowledgePayload(content=results[0].memory.content),
                provenance=KnowledgeProvenance(),
            )
        return None

    async def collect_working_knowledge(self):
        memories = await self._memory.list_memories(agent_id=self.agent_id)
        return [
            SnapshotEntry(
                domain=self._domains[0] if self._domains else "general",
                knowledge=KnowledgePayload(type=m.type, content=m.content),
            )
            for m in memories
        ]


class TestSnapshotFlow:
    @pytest.mark.asyncio
    async def test_sleep_publishes_snapshot(
        self, bus_service, memory_service, snapshot_service
    ):
        """Sleeping should publish a snapshot containing the agent's knowledge."""
        agent = SimpleAgent("test", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()

        knowledge_content = "GET /users returns list"
        await agent.store_knowledge(knowledge_content, domains=["api"])

        snapshot = await agent.sleep()
        assert snapshot is not None
        assert snapshot.agent_id == agent.agent_id
        assert len(snapshot.entries) >= 1
        # Snapshot should contain the knowledge we stored
        entry_contents = [e.knowledge.content for e in snapshot.entries]
        assert any(knowledge_content in c for c in entry_contents)

    @pytest.mark.asyncio
    async def test_surrogate_response_from_snapshot(
        self, bus_service, memory_service, snapshot_service
    ):
        """After an agent sleeps, its snapshot should answer questions (warm mode)."""
        knowledge_content = "GET /users returns paginated list"
        agent = SimpleAgent("api-agent", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()
        await agent.store_knowledge(knowledge_content, domains=["api"])

        await agent.sleep()

        requester = SimpleAgent(
            "requester", ["frontend"], bus_service, memory_service, snapshot_service
        )
        await requester.start()

        response = await requester.ask_knowledge(
            "What does the users endpoint return?",
            domains=["api"],
        )

        assert response is not None
        assert response.source_mode == "warm"
        # Response should contain content from the sleeping agent's snapshot
        assert knowledge_content.lower() in response.knowledge.content.lower() or \
            "users" in response.knowledge.content.lower()

    @pytest.mark.asyncio
    async def test_wake_restores_agent(
        self, bus_service, memory_service, snapshot_service
    ):
        """Waking should restore the agent to online status."""
        agent = SimpleAgent("test", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()
        await agent.store_knowledge("some knowledge", domains=["api"])

        await agent.sleep()
        assert not bus_service.is_agent_online(agent.agent_id)

        await agent.wake()
        assert bus_service.is_agent_online(agent.agent_id)

    @pytest.mark.asyncio
    async def test_live_response_after_wake(
        self, bus_service, memory_service, snapshot_service
    ):
        """After waking, agent should respond live (not surrogate)."""
        knowledge_content = "GET /users returns list"
        agent = SimpleAgent("api", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()
        await agent.store_knowledge(knowledge_content, domains=["api"])

        await agent.sleep()
        await agent.wake()

        requester = SimpleAgent(
            "req", ["frontend"], bus_service, memory_service, snapshot_service
        )
        await requester.start()

        response = await requester.ask_knowledge(
            "What does users return?",
            domains=["api"],
        )

        assert response is not None
        assert response.source_mode == "live"

    @pytest.mark.asyncio
    async def test_snapshot_persists_multiple_entries(
        self, bus_service, memory_service, snapshot_service
    ):
        """An agent with multiple memories should snapshot all of them."""
        agent = SimpleAgent("multi", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()

        entries_stored = 3
        for i in range(entries_stored):
            await agent.store_knowledge(
                f"API endpoint specification number {i}",
                domains=["api"],
            )

        snapshot = await agent.sleep()
        assert snapshot is not None
        assert len(snapshot.entries) == entries_stored

    @pytest.mark.asyncio
    async def test_surrogate_response_has_discounted_confidence(
        self, bus_service, memory_service, snapshot_service
    ):
        """Surrogate responses should apply the 0.8 discount factor."""
        agent = SimpleAgent("conf-test", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()
        await agent.store_knowledge(
            "GET /users returns user list data",
            domains=["api"],
        )

        snapshot = await agent.sleep()

        requester = SimpleAgent(
            "requester", ["frontend"], bus_service, memory_service, snapshot_service
        )
        await requester.start()

        response = await requester.ask_knowledge(
            "What does the users endpoint return?",
            domains=["api"],
        )

        assert response is not None
        # Surrogate confidence = entry_confidence × 0.8 (discount factor)
        # The entry confidence comes from collect_working_knowledge (default 0.5)
        snapshot_entry_conf = SnapshotEntry.model_fields["confidence"].default
        surrogate_discount = 0.8
        expected_confidence = snapshot_entry_conf * surrogate_discount
        assert response.confidence == pytest.approx(expected_confidence, abs=0.01)

    @pytest.mark.asyncio
    async def test_sleep_makes_agent_offline(
        self, bus_service, memory_service, snapshot_service
    ):
        """After sleeping, agent should no longer be online."""
        agent = SimpleAgent("offline-test", ["api"], bus_service, memory_service, snapshot_service)
        await agent.start()
        assert bus_service.is_agent_online(agent.agent_id)

        await agent.store_knowledge("data", domains=["api"])
        await agent.sleep()
        assert not bus_service.is_agent_online(agent.agent_id)
