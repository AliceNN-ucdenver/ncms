"""Behavioral tests for MCP tools.

This file intentionally avoids exact tool-count assertions.  The
important contract is that public tools forward their inputs to the
application layer and return useful data; adding unrelated tools should
not make these tests fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from ncms.config import NCMSConfig
from ncms.domain.models import Memory, MemoryNode, NodeType
from ncms.interfaces.mcp.tools import register_tools


def _make_memory(**overrides: Any) -> Memory:
    defaults = {
        "id": "mem-1",
        "content": "test content",
        "type": "fact",
        "domains": ["api"],
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Memory(**defaults)


def _make_node(**overrides: Any) -> MemoryNode:
    defaults = {
        "id": "node-1",
        "memory_id": "mem-1",
        "node_type": NodeType.ENTITY_STATE,
        "metadata": {
            "entity_id": "ent-1",
            "state_key": "status",
            "state_value": "active",
        },
        "is_current": True,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return MemoryNode(**defaults)


def _registered_tools(
    *,
    memory_svc: Any | None = None,
    bus_svc: Any | None = None,
    snapshot_svc: Any | None = None,
    consolidation_svc: Any | None = None,
):
    mcp = FastMCP(name="test")
    register_tools(
        mcp,
        memory_svc or AsyncMock(),
        bus_svc or MagicMock(),
        snapshot_svc or MagicMock(),
        consolidation_svc=consolidation_svc,
    )
    return mcp._tool_manager._tools  # noqa: SLF001 - tests call registered tool bodies.


class TestStoreMemoryTool:
    async def test_forwards_subject_payload_and_admission_output(self) -> None:
        memory_svc = AsyncMock()
        admission = {"score": 0.8, "route": "atomic"}
        memory_svc.store_memory.return_value = _make_memory(
            structured={"admission": admission},
        )
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["store_memory"].fn(
            content="ADR-004 picked Postgres",
            type="decision",
            domains=["software_dev"],
            tags=["adr"],
            project="ncms",
            structured={"source": "test"},
            importance=7.0,
            show_admission=True,
            subject="adr-004",
            subjects=[
                {
                    "id": "decision:adr-004",
                    "type": "decision",
                    "primary": True,
                    "aliases": ["ADR-004"],
                    "source": "caller",
                    "confidence": 1.0,
                }
            ],
            parent_doc_id="doc-parent",
        )

        memory_svc.store_memory.assert_awaited_once()
        kwargs = memory_svc.store_memory.await_args.kwargs
        assert kwargs["memory_type"] == "decision"
        assert kwargs["subject"] == "adr-004"
        assert kwargs["parent_doc_id"] == "doc-parent"
        assert kwargs["subjects"][0].id == "decision:adr-004"
        assert kwargs["subjects"][0].aliases == ("ADR-004",)
        assert result["admission"] == admission

    async def test_omits_admission_by_default(self) -> None:
        memory_svc = AsyncMock()
        memory_svc.store_memory.return_value = _make_memory(
            structured={"admission": {"score": 0.8}},
        )
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["store_memory"].fn(content="plain fact")

        assert "admission" not in result


class TestCommitKnowledgeTool:
    async def test_forwards_session_and_subject_payload(self) -> None:
        memory_svc = AsyncMock()
        memory_svc.store_memory.return_value = _make_memory(domains=["software_dev"])
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["commit_knowledge"].fn(
            content="learned something",
            domains=["software_dev"],
            type="decision",
            structured={"kind": "adr"},
            project="ncms",
            tags=["phase-a"],
            session_id="sess-1",
            subjects=[
                {
                    "id": "decision:adr-004",
                    "type": "decision",
                    "primary": True,
                    "aliases": [],
                    "source": "caller",
                    "confidence": 1.0,
                }
            ],
            parent_doc_id="doc-parent",
        )

        kwargs = memory_svc.store_memory.await_args.kwargs
        assert kwargs["memory_type"] == "decision"
        assert kwargs["tags"] == ["phase-a", "session:sess-1"]
        assert kwargs["subjects"][0].id == "decision:adr-004"
        assert kwargs["parent_doc_id"] == "doc-parent"
        assert result == {
            "memory_id": "mem-1",
            "domains_detected": ["software_dev"],
            "stored": True,
        }


class TestSearchMemoryTool:
    async def test_forwards_intent_override(self) -> None:
        memory_svc = AsyncMock()
        memory_svc.search.return_value = []
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["search_memory"].fn(
            query="current database",
            domain="software_dev",
            limit=3,
            intent="current_state_lookup",
        )

        assert result == []
        memory_svc.search.assert_awaited_once_with(
            "current database",
            domain="software_dev",
            limit=3,
            intent_override="current_state_lookup",
        )


class TestStateAndEpisodeTools:
    async def test_get_current_state_reports_disabled_temporal_feature(self) -> None:
        memory_svc = MagicMock()
        memory_svc._config = NCMSConfig(temporal_enabled=False)  # noqa: SLF001
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["get_current_state"].fn("svc-A", "status")

        assert "not enabled" in result["error"]

    async def test_get_current_state_returns_node_when_enabled(self) -> None:
        memory_svc = MagicMock()
        memory_svc._config = NCMSConfig(temporal_enabled=True)  # noqa: SLF001
        memory_svc.store.get_current_state = AsyncMock(return_value=_make_node())
        tools = _registered_tools(memory_svc=memory_svc)

        result = await tools["get_current_state"].fn("ent-1", "status")

        assert result["found"] is True
        assert result["node"]["metadata"]["state_value"] == "active"
        memory_svc.store.get_current_state.assert_awaited_once_with("ent-1", "status")

    async def test_required_public_tools_are_registered(self) -> None:
        tools = _registered_tools()

        for expected in (
            "store_memory",
            "commit_knowledge",
            "search_memory",
            "get_current_state",
            "get_state_history",
            "list_episodes",
            "get_episode",
        ):
            assert expected in tools

    async def test_consolidation_service_adds_tools(self) -> None:
        without = set(_registered_tools())
        with_consol = set(_registered_tools(consolidation_svc=AsyncMock()))

        assert without < with_consol
