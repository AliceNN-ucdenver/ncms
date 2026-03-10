"""Shared test fixtures for NCMS."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.config import NCMSConfig
from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0)


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index() -> TantivyEngine:
    engine = TantivyEngine()
    engine.initialize()
    return engine


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


@pytest_asyncio.fixture
async def memory_service(store, index, graph, config):
    return MemoryService(store=store, index=index, graph=graph, config=config)


@pytest_asyncio.fixture
async def snapshot_service(store):
    return SnapshotService(store=store)


@pytest_asyncio.fixture
async def bus():
    b = AsyncKnowledgeBus(ask_timeout_ms=2000)
    yield b
    await b.stop()


@pytest_asyncio.fixture
async def bus_service(bus, snapshot_service):
    return BusService(bus=bus, snapshot_service=snapshot_service)
