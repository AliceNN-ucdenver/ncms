"""Schema integration tests.

Verifies that the clean-create migration produces:

* ``graph_edges.retires_entities`` column with a ``'[]'`` default
* ``grammar_shape_cache`` table with ``idx_gsc_hit_count`` index
* ``grammar_transition_markers`` table with the expected PK

Plus round-trip coverage for ``GraphEdge.retires_entities`` through
``SQLiteStore.save_graph_edge`` / ``get_graph_edges``.

These are pure schema tests. They pass with ``retires_entities`` empty
on every edge, which is the clean-create steady state.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.domain.models import EdgeType, GraphEdge
from ncms.infrastructure.storage.migrations import SCHEMA_VERSION
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestSchemaVersion:
    async def test_persisted_version_matches_code_constant(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION


class TestGraphEdgesRetiresColumn:
    async def test_column_exists(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute("PRAGMA table_info(graph_edges)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "retires_entities" in cols
        # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
        assert cols["retires_entities"][2].upper() == "TEXT"
        assert cols["retires_entities"][3] == 1  # NOT NULL
        assert cols["retires_entities"][4] == "'[]'"  # DEFAULT '[]'

    async def test_existing_indexes_preserved(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='graph_edges'"
        )
        names = {row[0] for row in await cursor.fetchall()}
        assert "idx_gedges_source" in names
        assert "idx_gedges_target" in names
        assert "idx_gedges_type" in names


class TestGrammarShapeCache:
    async def test_table_exists_with_expected_columns(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute("PRAGMA table_info(grammar_shape_cache)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert set(cols.keys()) == {
            "skeleton",
            "intent",
            "slot_names",
            "hit_count",
            "last_used",
        }
        # skeleton is PK
        assert cols["skeleton"][5] == 1
        # intent NOT NULL
        assert cols["intent"][3] == 1
        # hit_count defaults to 0
        assert cols["hit_count"][4] == "0"

    async def test_hit_count_index_exists(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_gsc_hit_count'"
        )
        assert await cursor.fetchone() is not None


class TestGrammarTransitionMarkers:
    async def test_table_exists_with_composite_pk(self, store: SQLiteStore) -> None:
        cursor = await store.db.execute("PRAGMA table_info(grammar_transition_markers)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert set(cols.keys()) == {"transition_type", "marker_head", "count"}
        # Composite PK: both transition_type and marker_head participate
        pk_cols = {name for name, row in cols.items() if row[5] > 0}
        assert pk_cols == {"transition_type", "marker_head"}


class TestGraphEdgeRetiresRoundTrip:
    async def test_default_empty_list(self, store: SQLiteStore) -> None:
        edge = GraphEdge(
            source_id="a",
            target_id="b",
            edge_type=EdgeType.SUPPORTS,
        )
        assert edge.retires_entities == []
        await store.save_graph_edge(edge)
        loaded = await store.get_graph_edges("a")
        assert len(loaded) == 1
        assert loaded[0].retires_entities == []

    async def test_populated_round_trip(self, store: SQLiteStore) -> None:
        edge = GraphEdge(
            source_id="new_memory",
            target_id="old_memory",
            edge_type=EdgeType.SUPERSEDES,
            retires_entities=["entity-1", "entity-2", "entity-3"],
        )
        await store.save_graph_edge(edge)
        loaded = await store.get_graph_edges("new_memory")
        assert len(loaded) == 1
        assert loaded[0].retires_entities == ["entity-1", "entity-2", "entity-3"]
        assert loaded[0].edge_type == EdgeType.SUPERSEDES

    async def test_filtering_by_type_preserves_retires(self, store: SQLiteStore) -> None:
        supersedes = GraphEdge(
            source_id="src",
            target_id="tgt1",
            edge_type=EdgeType.SUPERSEDES,
            retires_entities=["x"],
        )
        supports = GraphEdge(
            source_id="src",
            target_id="tgt2",
            edge_type=EdgeType.SUPPORTS,
        )
        await store.save_graph_edge(supersedes)
        await store.save_graph_edge(supports)
        filtered = await store.get_graph_edges("src", edge_type="supersedes")
        assert len(filtered) == 1
        assert filtered[0].retires_entities == ["x"]
