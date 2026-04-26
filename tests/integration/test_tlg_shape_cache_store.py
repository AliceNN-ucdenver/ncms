"""TLG persistent shape cache — end-to-end integration.

Verifies the ``grammar_shape_cache`` table is populated by the
``ShapeCacheStore`` after a successful parse, survives restart, and
short-circuits the production list on subsequent lookups.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.tlg import ShapeCacheStore
from ncms.domain.tlg import SubjectMemory, induce_vocabulary
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


def _vocab():
    return induce_vocabulary(
        [
            SubjectMemory(
                subject="auth",
                entities=frozenset({"OAuth", "session cookies", "authentication"}),
            ),
        ]
    )


class TestShapeCacheStore:
    async def test_warm_empty_store(self, store: SQLiteStore) -> None:
        cache = ShapeCacheStore()
        await cache.warm(store)
        assert cache.size() == 0

    async def test_learn_persists_to_store(
        self,
        store: SQLiteStore,
    ) -> None:
        cache = ShapeCacheStore()
        v = _vocab()
        await cache.learn(store, "What came after OAuth?", "sequence", v)
        # Round-trip via a fresh cache.
        cache2 = ShapeCacheStore()
        await cache2.warm(store)
        hit = cache2.lookup("What came after OAuth?", v)
        assert hit is not None
        intent, _ = hit
        assert intent == "sequence"

    async def test_abstain_not_persisted(
        self,
        store: SQLiteStore,
    ) -> None:
        cache = ShapeCacheStore()
        v = _vocab()
        await cache.learn(store, "What is OAuth?", "none", v)
        await cache.learn(store, "What is OAuth?", "abstain", v)
        # Nothing should be in the backing table.
        snapshot = await store.load_shape_cache()
        assert snapshot == {}

    async def test_repeat_learn_idempotent(
        self,
        store: SQLiteStore,
    ) -> None:
        cache = ShapeCacheStore()
        v = _vocab()
        await cache.learn(store, "What came after OAuth?", "sequence", v)
        await cache.learn(store, "What came after OAuth?", "sequence", v)
        snapshot = await store.load_shape_cache()
        # Only one entry; hit_count tracked.
        assert len(snapshot) == 1
        sole = next(iter(snapshot.values()))
        assert sole["intent"] == "sequence"
        assert sole["hit_count"] == 2

    async def test_conflict_does_not_overwrite_intent(
        self,
        store: SQLiteStore,
    ) -> None:
        cache = ShapeCacheStore()
        v = _vocab()
        await cache.learn(store, "What came after OAuth?", "sequence", v)
        # Attempting to reassign the same skeleton to a different
        # intent should be a no-op — productions remain authoritative.
        await cache.learn(
            store,
            "What came after OAuth?",
            "predecessor",
            v,
        )
        snapshot = await store.load_shape_cache()
        sole = next(iter(snapshot.values()))
        assert sole["intent"] == "sequence"
