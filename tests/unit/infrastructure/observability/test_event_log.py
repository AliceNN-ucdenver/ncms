"""Tests for the EventLog observability infrastructure."""

from __future__ import annotations

import json

import pytest

from ncms.infrastructure.observability.event_log import DashboardEvent, EventLog


class TestDashboardEvent:
    def test_event_has_id_and_timestamp(self):
        event = DashboardEvent(type="test.event")
        assert event.id
        assert event.timestamp
        assert event.type == "test.event"

    def test_event_to_sse_format(self):
        event = DashboardEvent(
            id="abc123",
            type="bus.ask",
            agent_id="api-agent",
            data={"question": "hello"},
        )
        sse = event.to_sse()
        assert "id: abc123" in sse
        assert "event: bus.ask" in sse
        assert "data: " in sse
        # Verify JSON payload is valid
        data_line = [line for line in sse.split("\n") if line.startswith("data: ")][0]
        payload = json.loads(data_line.removeprefix("data: "))
        assert payload["agent_id"] == "api-agent"
        assert payload["data"]["question"] == "hello"

    def test_event_immutable(self):
        event = DashboardEvent(type="test")
        with pytest.raises(AttributeError):
            event.type = "other"  # type: ignore[misc]


class TestEventLog:
    def test_emit_and_recent(self):
        log = EventLog(max_events=100)
        log.emit(DashboardEvent(type="a"))
        log.emit(DashboardEvent(type="b"))
        log.emit(DashboardEvent(type="c"))

        recent = log.recent(10)
        assert len(recent) == 3
        # Most recent first
        assert recent[0].type == "c"
        assert recent[2].type == "a"

    def test_ring_buffer_overflow(self):
        log = EventLog(max_events=3)
        for i in range(5):
            log.emit(DashboardEvent(type=f"event-{i}"))

        assert log.count() == 3
        recent = log.recent(10)
        # Only last 3 should remain
        assert recent[0].type == "event-4"
        assert recent[2].type == "event-2"

    def test_convenience_emitters(self):
        log = EventLog()

        log.agent_registered("api-agent", ["api", "api:users"])
        log.agent_deregistered("api-agent")
        log.agent_status("api-agent", "sleeping")
        log.bus_ask("ask-1", "frontend", "What is JWT?", ["api"], ["api-agent"])
        log.bus_response("ask-1", "api-agent", "live", 0.85)
        log.bus_announce("ann-1", "db-agent", "breaking-change", ["db"], "critical", ["api-agent"])
        log.bus_surrogate("ask-2", "api-agent", 0.65, 3600.0)
        log.memory_stored("mem-1", "Hello world", "fact", ["api"], 3, "api-agent")
        log.memory_searched("JWT auth", 5, 0.92, "frontend")

        assert log.count() == 9

    def test_query_diagnostic_emits_full_payload(self):
        """Per-query diagnostic event must include all signal fields.

        Phase I — operators rely on this event to verify retrieval
        behaviour as we retire regex/heuristic/GLiNER fallbacks.
        Adding new signal fields to the payload (e.g. CTLG cue
        coverage) should extend this test rather than break it.
        """
        log = EventLog()
        log.query_diagnostic(
            query="what database do we use",
            intent="fact_lookup",
            intent_confidence=0.82,
            query_entities=["database"],
            resolved_entity_ids=["ent-db"],
            temporal_ref=None,
            grammar_composed=False,
            grammar_confidence=None,
            candidate_counts={
                "bm25": 50, "splade": 50, "rrf_fused": 60,
                "expanded": 75, "scored": 75, "returned": 5,
            },
            signal_coverage={
                "intent_alignment": 0,
                "state_change_alignment": 0,
                "role_grounding": 12,
                "hierarchy_bonus": 0,
                "temporal": 0,
                "graph": 30,
                "reconciliation_penalty": 0,
            },
            htmg_subject_stats={
                "l2_entity_states": 3,
                "supersession_chain_size": 1,
                "causal_edges": 0,
            },
            top_breakdown={
                "memory_id": "mem-top",
                "content_preview": "Postgres is the database",
                "node_types": ["atomic"],
                "bm25_raw": 5.2, "splade_raw": 0.0,
                "graph_raw": 1.5, "h_bonus": 0.0,
                "ia_contrib": 0.0, "sc_contrib": 0.0,
                "rg_contrib": 0.25, "temporal": 0.0,
                "penalty": 0.0, "total": 6.7,
                "is_superseded": False, "has_conflicts": False,
            },
            result_count=5,
            total_ms=42.3,
        )

        recent = log.recent(2)
        assert len(recent) == 1
        evt = recent[0]
        assert evt.type == "query.diagnostic"
        d = evt.data
        # Core query info
        assert d["query"] == "what database do we use"
        assert d["intent"] == "fact_lookup"
        assert d["intent_confidence"] == 0.82
        assert d["query_entities"] == ["database"]
        assert d["resolved_entity_ids"] == ["ent-db"]
        # TLG composition
        assert d["grammar_composed"] is False
        assert d["grammar_confidence"] is None
        # Per-stage funnel
        assert d["candidate_counts"]["bm25"] == 50
        assert d["candidate_counts"]["returned"] == 5
        # Signal coverage — the H-series + CTLG-extension surface
        assert d["signal_coverage"]["role_grounding"] == 12
        assert d["signal_coverage"]["graph"] == 30
        assert d["signal_coverage"]["intent_alignment"] == 0
        # HTMG stats
        assert d["htmg_subject_stats"]["l2_entity_states"] == 3
        assert d["htmg_subject_stats"]["supersession_chain_size"] == 1
        # Top breakdown — full signal vector for rank-1
        top = d["top_breakdown"]
        assert top["memory_id"] == "mem-top"
        assert top["bm25_raw"] == 5.2
        assert top["rg_contrib"] == 0.25
        assert top["total"] == 6.7
        assert top["is_superseded"] is False
        assert d["result_count"] == 5
        assert d["total_ms"] == 42.3

    def test_query_diagnostic_truncates_long_query(self):
        """Long queries should be truncated to 200 chars."""
        log = EventLog()
        long_q = "x" * 500
        log.query_diagnostic(
            query=long_q, intent=None, intent_confidence=None,
            query_entities=[], resolved_entity_ids=[],
            temporal_ref=None, grammar_composed=False,
            grammar_confidence=None,
            candidate_counts={}, signal_coverage={},
            htmg_subject_stats={}, top_breakdown=None,
            result_count=0, total_ms=1.0,
        )
        evt = log.recent(1)[0]
        assert len(evt.data["query"]) == 200

    def test_query_diagnostic_caps_entity_lists(self):
        """Large entity lists capped at 20 to keep the event payload sane."""
        log = EventLog()
        many = [f"e-{i}" for i in range(50)]
        log.query_diagnostic(
            query="q", intent=None, intent_confidence=None,
            query_entities=many, resolved_entity_ids=many,
            temporal_ref=None, grammar_composed=False,
            grammar_confidence=None,
            candidate_counts={}, signal_coverage={},
            htmg_subject_stats={}, top_breakdown=None,
            result_count=0, total_ms=1.0,
        )
        evt = log.recent(1)[0]
        assert len(evt.data["query_entities"]) == 20
        assert len(evt.data["resolved_entity_ids"]) == 20

    def test_convenience_emitters_event_types(self):
        """Tail of the convenience-emitters test — verifies that all
        emitted convenience events show up in ``recent()``.  Split off
        from ``test_convenience_emitters`` so this assertion-block
        survives reorderings of the new diagnostic-event tests above.
        """
        log = EventLog()
        log.agent_registered("api-agent", ["api"])
        log.bus_ask("ask-1", "frontend", "q", ["api"], ["api-agent"])
        log.bus_response("ask-1", "api-agent", "live", 0.85)
        log.bus_announce(
            "ann-1", "db-agent", "evt", ["db"], "info", ["api-agent"],
        )
        log.bus_surrogate("ask-2", "api-agent", 0.65, 3600.0)
        log.memory_stored(
            "mem-1", "Hello", "fact", ["api"], 3, "api-agent",
        )
        log.memory_searched("q", 5, 0.92, "frontend")

        recent = log.recent(20)
        types = [e.type for e in recent]
        assert "agent.registered" in types
        assert "bus.ask" in types
        assert "bus.response" in types
        assert "bus.announce" in types
        assert "bus.surrogate" in types
        assert "memory.stored" in types
        assert "memory.searched" in types

    @pytest.mark.asyncio
    async def test_subscriber_receives_events(self):
        log = EventLog()
        queue = await log.subscribe()

        log.emit(DashboardEvent(type="test.event", data={"key": "value"}))

        # Subscriber should have the event
        event = queue.get_nowait()
        assert event.type == "test.event"
        assert event.data["key"] == "value"

        log.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        log = EventLog()
        q1 = await log.subscribe()
        q2 = await log.subscribe()

        log.emit(DashboardEvent(type="shared"))

        assert q1.get_nowait().type == "shared"
        assert q2.get_nowait().type == "shared"

        log.unsubscribe(q1)
        log.unsubscribe(q2)

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        log = EventLog()
        queue = await log.subscribe()
        log.unsubscribe(queue)

        log.emit(DashboardEvent(type="after_unsub"))

        assert queue.empty()

    @pytest.mark.asyncio
    async def test_overflowed_subscriber_removed(self):
        log = EventLog()
        queue = await log.subscribe()

        # Fill the queue to capacity (500)
        for i in range(501):
            log.emit(DashboardEvent(type=f"flood-{i}"))

        # Subscriber should have been removed due to overflow
        assert queue not in log._subscribers


class TestPipelineStageEmitter:
    """Test the pipeline_stage() convenience emitter."""

    def test_pipeline_stage_emits_correct_type(self):
        log = EventLog()
        log.pipeline_stage(
            pipeline_id="abc123",
            pipeline_type="store",
            stage="persist",
            duration_ms=1.5,
            memory_id="mem-1",
        )
        event = log.recent(1)[0]
        assert event.type == "pipeline.store.persist"
        assert event.data["pipeline_id"] == "abc123"
        assert event.data["duration_ms"] == 1.5
        assert event.data["memory_id"] == "mem-1"

    def test_pipeline_stage_search_type(self):
        log = EventLog()
        log.pipeline_stage(
            pipeline_id="def456",
            pipeline_type="search",
            stage="bm25",
            duration_ms=3.2,
            data={"candidate_count": 42},
        )
        event = log.recent(1)[0]
        assert event.type == "pipeline.search.bm25"
        assert event.data["candidate_count"] == 42
        assert event.data["pipeline_type"] == "search"

    def test_pipeline_stage_with_agent_id(self):
        log = EventLog()
        log.pipeline_stage(
            pipeline_id="ghi789",
            pipeline_type="store",
            stage="start",
            duration_ms=0.0,
            agent_id="api-agent",
        )
        event = log.recent(1)[0]
        assert event.agent_id == "api-agent"

    def test_pipeline_stage_extra_data_merged(self):
        log = EventLog()
        log.pipeline_stage(
            pipeline_id="jkl012",
            pipeline_type="store",
            stage="entity_extraction",
            duration_ms=5.0,
            data={"auto_count": 3, "entity_names": ["JWT", "FastAPI"]},
        )
        event = log.recent(1)[0]
        assert event.data["auto_count"] == 3
        assert event.data["entity_names"] == ["JWT", "FastAPI"]
        assert event.data["pipeline_id"] == "jkl012"

    def test_pipeline_stage_rounds_duration(self):
        log = EventLog()
        log.pipeline_stage(
            pipeline_id="mno345",
            pipeline_type="search",
            stage="actr_scoring",
            duration_ms=1.23456789,
        )
        event = log.recent(1)[0]
        assert event.data["duration_ms"] == 1.23


class TestEventLogBusIntegration:
    """Test that bus emits events when event_log is provided."""

    @pytest.mark.asyncio
    async def test_bus_emits_agent_registered(self):
        from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus

        log = EventLog()
        bus = AsyncKnowledgeBus(event_log=log)

        await bus.register_provider("test-agent", ["test-domain"])

        assert log.count() == 1
        event = log.recent(1)[0]
        assert event.type == "agent.registered"
        assert event.agent_id == "test-agent"
        assert event.data["domains"] == ["test-domain"]

    @pytest.mark.asyncio
    async def test_bus_emits_agent_deregistered(self):
        from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus

        log = EventLog()
        bus = AsyncKnowledgeBus(event_log=log)

        await bus.register_provider("test-agent", ["test"])
        await bus.deregister_provider("test-agent")

        events = log.recent(10)
        assert events[0].type == "agent.deregistered"

    @pytest.mark.asyncio
    async def test_bus_emits_status_change(self):
        from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus

        log = EventLog()
        bus = AsyncKnowledgeBus(event_log=log)

        await bus.register_provider("test-agent", ["test"])
        await bus.update_availability("test-agent", "sleeping")

        events = log.recent(10)
        assert events[0].type == "agent.status"
        assert events[0].data["status"] == "sleeping"

    @pytest.mark.asyncio
    async def test_bus_without_event_log_works(self):
        """Bus should work normally without event_log (backwards compatible)."""
        from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus

        bus = AsyncKnowledgeBus()  # No event_log
        await bus.register_provider("test-agent", ["test"])
        await bus.deregister_provider("test-agent")
        # No crash = success


class TestEventLogMemoryIntegration:
    """Test that MemoryService emits events when event_log is provided."""

    @pytest.mark.asyncio
    async def test_memory_service_emits_store_event(self):
        from ncms.application.memory_service import MemoryService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        log = EventLog()
        config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
            event_log=log,
        )

        await svc.store_memory("Test content about Flask", domains=["api"])

        events = [e for e in log.recent(10) if e.type == "memory.stored"]
        assert len(events) == 1
        assert "Flask" in events[0].data["content"]
        assert events[0].data["domains"] == ["api"]
        assert events[0].data["entity_count"] >= 1  # "Flask" extracted

        await store.close()

    @pytest.mark.asyncio
    async def test_memory_service_emits_search_event(self):
        from ncms.application.memory_service import MemoryService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        log = EventLog()
        config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
            event_log=log,
        )

        await svc.store_memory("Flask web framework for Python REST APIs")
        await svc.search("Flask API")

        events = [e for e in log.recent(10) if e.type == "memory.searched"]
        assert len(events) == 1
        assert events[0].data["query"] == "Flask API"
        assert events[0].data["result_count"] >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_bus_response_includes_answer_text(self):
        log = EventLog()
        log.bus_response("ask-1", "api-agent", "live", 0.85, answer="JWT uses RS256 signing")

        event = log.recent(1)[0]
        assert event.type == "bus.response"
        assert event.data["answer"] == "JWT uses RS256 signing"

    @pytest.mark.asyncio
    async def test_bus_announce_includes_content_text(self):
        log = EventLog()
        log.bus_announce(
            "ann-1", "db-agent", "breaking-change", ["db"], "critical",
            ["api-agent"], content="ALTER TABLE users ADD COLUMN role",
        )

        event = log.recent(1)[0]
        assert event.type == "bus.announce"
        assert event.data["content"] == "ALTER TABLE users ADD COLUMN role"

    @pytest.mark.asyncio
    async def test_bus_surrogate_includes_answer_text(self):
        log = EventLog()
        log.bus_surrogate("ask-2", "api-agent", 0.65, 3600.0, answer="Rate limit is 100/min")

        event = log.recent(1)[0]
        assert event.type == "bus.surrogate"
        assert event.data["answer"] == "Rate limit is 100/min"

    @pytest.mark.asyncio
    async def test_answer_text_truncated_at_200_chars(self):
        log = EventLog()
        long_answer = "x" * 300
        log.bus_response("ask-1", "api-agent", "live", 0.85, answer=long_answer)

        event = log.recent(1)[0]
        assert len(event.data["answer"]) == 200

    @pytest.mark.asyncio
    async def test_memory_service_without_event_log_works(self):
        """MemoryService should work normally without event_log."""
        from ncms.application.memory_service import MemoryService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(store=store, index=index, graph=graph, config=config)
        await svc.store_memory("No event log, still works")
        results = await svc.search("event log")
        assert len(results) >= 1

        await store.close()
