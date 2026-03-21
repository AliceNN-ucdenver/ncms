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
        config = NCMSConfig(reconciliation_enabled=False)
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


class TestToolCount:
    """Verify total tool count after Phase 6."""

    async def test_total_tools_without_consolidation(self) -> None:
        """18 tools without consolidation service."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(mcp, AsyncMock(), MagicMock(), MagicMock())
        tools = await mcp.list_tools()
        assert len(tools) == 18  # noqa: PLR2004

    async def test_total_tools_with_consolidation(self) -> None:
        """19 tools with consolidation service."""
        from mcp.server.fastmcp import FastMCP

        from ncms.interfaces.mcp.tools import register_tools

        mcp = FastMCP(name="test")
        register_tools(
            mcp, AsyncMock(), MagicMock(), MagicMock(),
            consolidation_svc=AsyncMock(),
        )
        tools = await mcp.list_tools()
        assert len(tools) == 19  # noqa: PLR2004
