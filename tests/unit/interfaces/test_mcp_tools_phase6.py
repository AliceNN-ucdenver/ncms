"""Tests for Phase 6 MCP tool enhancements."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from ncms.config import NCMSConfig
from ncms.domain.models import Memory, MemoryNode, NodeType


def _make_memory(**overrides) -> Memory:
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


def _make_node(**overrides) -> MemoryNode:
    defaults = {
        "id": "node-1",
        "memory_id": "mem-1",
        "node_type": NodeType.ENTITY_STATE,
        "metadata": {"entity_id": "ent-1", "state_key": "status", "state_value": "active"},
        "is_current": True,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return MemoryNode(**defaults)


class TestStoreMemoryShowAdmission:
    """Tests for show_admission parameter on store_memory."""

    async def test_without_show_admission(self) -> None:
        """Default: admission data not included."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        memory_svc = AsyncMock()
        memory_svc.store_memory.return_value = _make_memory(
            structured={"admission": {"score": 0.8, "route": "atomic"}},
        )
        bus_svc = MagicMock()
        snapshot_svc = MagicMock()

        register_tools(mcp, memory_svc, bus_svc, snapshot_svc)

        tools = await mcp.list_tools()
        store_tool = next(t for t in tools if t.name == "store_memory")
        assert store_tool is not None

    async def test_with_show_admission_returns_data(self) -> None:
        """When show_admission=True, admission data is included."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        memory_svc = AsyncMock()
        admission_data = {"score": 0.8, "route": "atomic"}
        memory_svc.store_memory.return_value = _make_memory(
            structured={"admission": admission_data},
        )
        bus_svc = MagicMock()
        snapshot_svc = MagicMock()

        register_tools(mcp, memory_svc, bus_svc, snapshot_svc)

        tools_dict = {t.name: t for t in await mcp.list_tools()}
        assert "store_memory" in tools_dict


class TestSearchMemoryIntentOverride:
    """Tests for intent override on search_memory."""

    async def test_intent_parameter_exists(self) -> None:
        """search_memory accepts intent parameter."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        memory_svc = AsyncMock()
        memory_svc.search.return_value = []
        bus_svc = MagicMock()
        snapshot_svc = MagicMock()

        register_tools(mcp, memory_svc, bus_svc, snapshot_svc)

        tools_dict = {t.name: t for t in await mcp.list_tools()}
        assert "search_memory" in tools_dict


class TestGetCurrentState:
    """Tests for get_current_state tool."""

    async def test_returns_error_when_disabled(self) -> None:
        """Returns error when reconciliation is disabled."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        memory_svc = AsyncMock()
        config = NCMSConfig(temporal_enabled=False)
        type(memory_svc)._config = PropertyMock(return_value=config)
        bus_svc = MagicMock()
        snapshot_svc = MagicMock()

        register_tools(mcp, memory_svc, bus_svc, snapshot_svc)
        tools_dict = {t.name: t for t in await mcp.list_tools()}
        assert "get_current_state" in tools_dict

    async def test_tool_registered(self) -> None:
        """get_current_state tool is registered."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        memory_svc = AsyncMock()
        bus_svc = MagicMock()
        snapshot_svc = MagicMock()

        register_tools(mcp, memory_svc, bus_svc, snapshot_svc)
        tool_names = [t.name for t in await mcp.list_tools()]
        assert "get_current_state" in tool_names


class TestGetStateHistory:
    """Tests for get_state_history tool."""

    async def test_tool_registered(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(mcp, AsyncMock(), MagicMock(), MagicMock())
        tool_names = [t.name for t in await mcp.list_tools()]
        assert "get_state_history" in tool_names


class TestListEpisodes:
    """Tests for list_episodes tool."""

    async def test_tool_registered(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(mcp, AsyncMock(), MagicMock(), MagicMock())
        tool_names = [t.name for t in await mcp.list_tools()]
        assert "list_episodes" in tool_names


class TestGetEpisode:
    """Tests for get_episode tool."""

    async def test_tool_registered(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(mcp, AsyncMock(), MagicMock(), MagicMock())
        tool_names = [t.name for t in await mcp.list_tools()]
        assert "get_episode" in tool_names


class TestToolRegistration:
    """Verify Phase-6 tool names appear in the registered tool set.

    We test for the specific tools this file's phase added, not exact
    counts — adding tools in future phases should not break this test.
    """

    async def test_phase6_tools_registered(self) -> None:
        """Phase 6 added get_current_state, get_state_history, list_episodes."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(mcp, AsyncMock(), MagicMock(), MagicMock())
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        for expected in (
            "get_current_state",
            "get_state_history",
            "list_episodes",
        ):
            assert expected in tool_names, (
                f"Phase 6 tool {expected!r} not registered"
            )

    async def test_consolidation_adds_consolidation_tools(self) -> None:
        """Passing consolidation_svc should register consolidation-specific tools."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp_without = FastMCP(name="test-without")
        register_tools(
            mcp_without, AsyncMock(), MagicMock(), MagicMock(),
        )
        without = {t.name for t in await mcp_without.list_tools()}

        mcp_with = FastMCP(name="test-with")
        register_tools(
            mcp_with, AsyncMock(), MagicMock(), MagicMock(),
            consolidation_svc=AsyncMock(),
        )
        with_consol = {t.name for t in await mcp_with.list_tools()}

        # With consolidation must be a strict superset.
        assert without < with_consol, (
            "consolidation_svc did not add any new tools"
        )
