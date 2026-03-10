"""Integration tests for Knowledge Bus ask/respond/announce flow."""

import pytest

from ncms.domain.models import (
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    ImpactAssessment,
    SubscriptionFilter,
)


class TestBusRouting:
    @pytest.mark.asyncio
    async def test_ask_routes_to_provider(self, bus_service):
        """An ask should route to a registered provider for the domain."""
        await bus_service.register_provider("api-agent", ["api"])

        expected_content = "the answer from api-agent"

        async def handler(ask):
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent="api-agent",
                knowledge=KnowledgePayload(content=expected_content),
                provenance=KnowledgeProvenance(),
            )

        bus_service.set_ask_handler("api-agent", handler)

        await bus_service.register_provider("frontend", ["frontend"])
        ask = KnowledgeAsk(
            from_agent="frontend",
            question="What is the API?",
            domains=["api"],
        )
        response = await bus_service.ask_sync(ask, timeout_ms=2000)

        assert response is not None
        assert response.knowledge.content == expected_content
        assert response.from_agent == "api-agent"

    @pytest.mark.asyncio
    async def test_ask_with_no_provider_returns_none(self, bus_service):
        """An ask with no matching provider should return None."""
        await bus_service.register_provider("asker", ["frontend"])

        ask = KnowledgeAsk(
            from_agent="asker",
            question="Anything?",
            domains=["nonexistent-domain"],
        )
        response = await bus_service.ask_sync(ask, timeout_ms=500)
        assert response is None

    @pytest.mark.asyncio
    async def test_ask_handler_receives_correct_question(self, bus_service):
        """The ask handler should receive the question from the caller."""
        await bus_service.register_provider("responder", ["info"])
        received_questions = []

        async def handler(ask):
            received_questions.append(ask.question)
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent="responder",
                knowledge=KnowledgePayload(content="response"),
            )

        bus_service.set_ask_handler("responder", handler)

        question_text = "What info do you have?"
        await bus_service.register_provider("caller", ["other"])
        ask = KnowledgeAsk(
            from_agent="caller",
            question=question_text,
            domains=["info"],
        )
        await bus_service.ask_sync(ask, timeout_ms=2000)

        assert len(received_questions) == 1
        assert received_questions[0] == question_text

    @pytest.mark.asyncio
    async def test_ask_response_carries_ask_id(self, bus_service):
        """Response should reference the original ask_id."""
        await bus_service.register_provider("provider", ["test-domain"])

        async def handler(ask):
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent="provider",
                knowledge=KnowledgePayload(content="answer"),
            )

        bus_service.set_ask_handler("provider", handler)

        await bus_service.register_provider("asker", ["other"])
        ask = KnowledgeAsk(from_agent="asker", question="?", domains=["test-domain"])
        response = await bus_service.ask_sync(ask, timeout_ms=2000)

        assert response is not None
        assert response.ask_id == ask.ask_id


class TestAnnouncements:
    @pytest.mark.asyncio
    async def test_announcement_delivered_to_subscriber(self, bus_service):
        """Announcements should be delivered to subscribed agents."""
        await bus_service.register_provider("sender", ["db"])
        await bus_service.register_provider("receiver", ["api"])
        await bus_service.subscribe("receiver", ["db"])

        ann_content = "users table schema changed"
        announcement = KnowledgeAnnounce(
            from_agent="sender",
            event="breaking-change",
            domains=["db"],
            knowledge=KnowledgePayload(content=ann_content),
            impact=ImpactAssessment(
                breaking_change=True,
                affected_domains=["db"],
                severity="critical",
                description=ann_content,
            ),
        )
        await bus_service.announce(announcement)

        announcements = await bus_service.get_announcements("receiver")
        assert len(announcements) >= 1
        assert any(a.knowledge.content == ann_content for a in announcements)

    @pytest.mark.asyncio
    async def test_announcement_not_delivered_to_unsubscribed(self, bus_service):
        """Announcements should not reach agents not subscribed to the domain."""
        await bus_service.register_provider("sender", ["db"])
        await bus_service.register_provider("bystander", ["frontend"])
        await bus_service.subscribe("bystander", ["frontend"])  # not subscribed to db

        announcement = KnowledgeAnnounce(
            from_agent="sender",
            event="updated",
            domains=["db"],
            knowledge=KnowledgePayload(content="db update"),
        )
        await bus_service.announce(announcement)

        announcements = await bus_service.get_announcements("bystander")
        assert len(announcements) == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers_receive_announcement(self, bus_service):
        """All subscribers to a domain should receive the announcement."""
        await bus_service.register_provider("sender", ["db"])
        await bus_service.register_provider("sub1", ["api"])
        await bus_service.register_provider("sub2", ["frontend"])
        await bus_service.subscribe("sub1", ["db"])
        await bus_service.subscribe("sub2", ["db"])

        announcement = KnowledgeAnnounce(
            from_agent="sender",
            event="updated",
            domains=["db"],
            knowledge=KnowledgePayload(content="broadcast update"),
        )
        await bus_service.announce(announcement)

        ann1 = await bus_service.get_announcements("sub1")
        ann2 = await bus_service.get_announcements("sub2")
        assert len(ann1) >= 1
        assert len(ann2) >= 1

    @pytest.mark.asyncio
    async def test_drain_announcements_clears_queue(self, bus_service):
        """Draining should return and clear all pending announcements."""
        await bus_service.register_provider("sender", ["db"])
        await bus_service.register_provider("receiver", ["api"])
        await bus_service.subscribe("receiver", ["db"])

        for i in range(3):
            await bus_service.announce(KnowledgeAnnounce(
                from_agent="sender",
                event="updated",
                domains=["db"],
                knowledge=KnowledgePayload(content=f"update {i}"),
            ))

        drained = await bus_service.drain_announcements("receiver")
        assert len(drained) == 3

        # After draining, queue should be empty
        remaining = await bus_service.get_announcements("receiver")
        assert len(remaining) == 0


class TestAgentLifecycle:
    @pytest.mark.asyncio
    async def test_deregister_removes_agent(self, bus_service):
        """Deregistering should remove the agent from the bus."""
        await bus_service.register_provider("temp", ["temp"])
        assert bus_service.is_agent_online("temp")

        await bus_service.deregister_provider("temp")
        assert not bus_service.is_agent_online("temp")

    @pytest.mark.asyncio
    async def test_list_domains(self, bus_service):
        """list_domains should return all registered domains."""
        await bus_service.register_provider("a", ["api", "auth"])
        await bus_service.register_provider("b", ["db"])

        domains = bus_service.list_domains()
        assert {"api", "auth", "db"}.issubset(set(domains))

    @pytest.mark.asyncio
    async def test_get_all_agents(self, bus_service):
        """get_all_agents should return info about all registered agents."""
        await bus_service.register_provider("agent-x", ["x"])
        await bus_service.register_provider("agent-y", ["y"])

        agents = bus_service.get_all_agents()
        agent_ids = {a.agent_id for a in agents}
        assert {"agent-x", "agent-y"}.issubset(agent_ids)

    @pytest.mark.asyncio
    async def test_update_availability(self, bus_service):
        """Changing availability should update agent status."""
        await bus_service.register_provider("sleeper", ["domain"])
        assert bus_service.is_agent_online("sleeper")

        await bus_service.update_availability("sleeper", "sleeping")
        assert not bus_service.is_agent_online("sleeper")

        await bus_service.update_availability("sleeper", "online")
        assert bus_service.is_agent_online("sleeper")
