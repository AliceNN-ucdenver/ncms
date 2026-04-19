"""TLG alias expansion — integration test.

Verifies that a ``still`` query using the **short form** of an
entity (``"JWT"``) hits a SUPERSEDES edge that recorded the
**long form** (``"JSON Web Tokens"``) in its ``retires_entities``
set.  Without alias expansion this lookup would miss and fall back
to BM25.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.application.tlg import VocabularyCache, retrieve_lg
from ncms.config import NCMSConfig
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.domain.tlg import Confidence
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _ensure_entity(store: SQLiteStore, eid: str) -> None:
    if await store.get_entity(eid) is not None:
        return
    ent = Entity(name=eid, type="concept")
    ent.id = eid
    await store.save_entity(ent)


async def _seed_state(
    store: SQLiteStore,
    *,
    content: str,
    entity_id: str,
    state_key: str,
    state_value: str,
    linked_entity_ids: list[str],
) -> MemoryNode:
    mem = Memory(content=content, domains=["tlg-alias-test"])
    await store.save_memory(mem)
    for eid in linked_entity_ids:
        await _ensure_entity(store, eid)
        await store.link_memory_entity(mem.id, eid)
    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await store.save_memory_node(node)
    return node


class TestStillWithAlias:
    async def test_short_form_query_matches_long_form_retirement(
        self, store: SQLiteStore
    ) -> None:
        config = NCMSConfig(
            db_path=":memory:",
            reconciliation_enabled=True,
            tlg_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        # Old state mentions both the long form and the short form.
        # The induction universe must see both surface forms for the
        # abbreviation rule to register them as aliases.
        await _seed_state(
            store,
            content="Auth uses JSON Web Tokens (JWT).",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="JSON Web Tokens",
            linked_entity_ids=["JSON Web Tokens", "JWT"],
        )
        # New state retires the long form.
        new_node = await _seed_state(
            store,
            content="Retire JSON Web Tokens; adopt short-lived session tokens.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session tokens",
            linked_entity_ids=["session tokens"],
        )
        await service.reconcile(new_node)

        cache = VocabularyCache()
        # Confirm the alias was induced.
        aliases = await cache.get_aliases(store)
        assert aliases, "alias induction produced no results"
        # Either direction of the pair should include the other.
        has_pair = any(
            "JWT" in als or "JSON Web Tokens" in als
            for als in aliases.values()
        )
        assert has_pair, f"expected JWT↔JSON Web Tokens alias, got {aliases!r}"

        # Query uses the SHORT form.  Without alias expansion the
        # retirement-edge scan would miss (retires_entities recorded
        # the long form).
        trace = await retrieve_lg(
            "are we still using JWT for auth?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "still"
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == new_node.id

    async def test_long_form_query_matches_short_form_retirement(
        self, store: SQLiteStore
    ) -> None:
        """Symmetric — query uses long form, edge recorded short form."""
        config = NCMSConfig(
            db_path=":memory:",
            reconciliation_enabled=True,
            tlg_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        # Old state mentions both forms so alias induction can pair them.
        await _seed_state(
            store,
            content="Auth requires MFA (multi-factor authentication).",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="MFA",
            linked_entity_ids=["MFA", "multi-factor authentication"],
        )
        # New state retires the short form.
        new_node = await _seed_state(
            store,
            content="Retire MFA; adopt passkeys.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="passkeys",
            linked_entity_ids=["passkeys"],
        )
        await service.reconcile(new_node)

        cache = VocabularyCache()
        trace = await retrieve_lg(
            "are we still using multi-factor authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "still"
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == new_node.id

    async def test_no_alias_available_still_works_direct_match(
        self, store: SQLiteStore
    ) -> None:
        """Entity without a short/long partner still dispatches directly."""
        config = NCMSConfig(
            db_path=":memory:",
            reconciliation_enabled=True,
            tlg_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        await _seed_state(
            store,
            content="Backend uses polling-config.",
            entity_id="backend",
            state_key="config",
            state_value="polling-config",
            linked_entity_ids=["polling-config"],
        )
        new_node = await _seed_state(
            store,
            content="Retire polling-config; adopt push notifications.",
            entity_id="backend",
            state_key="config",
            state_value="push notifications",
            linked_entity_ids=["push notifications"],
        )
        await service.reconcile(new_node)

        cache = VocabularyCache()
        trace = await retrieve_lg(
            "are we still using polling-config for backend?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == new_node.id


class TestAliasCacheInvalidation:
    async def test_aliases_cleared_on_invalidate(
        self, store: SQLiteStore
    ) -> None:
        # Empty store
        cache = VocabularyCache()
        first = await cache.get_aliases(store)
        assert first == {}

        # Seed entities that form an alias pair.
        for eid in ["JWT", "JSON Web Tokens"]:
            await _ensure_entity(store, eid)
        mem = Memory(content="seed", domains=["t"])
        await store.save_memory(mem)
        await store.link_memory_entity(mem.id, "JWT")
        await store.link_memory_entity(mem.id, "JSON Web Tokens")
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "svc", "state_key": "k", "state_value": "v"},
        )
        await store.save_memory_node(node)

        # Without invalidate, returns the cached empty result.
        still_empty = await cache.get_aliases(store)
        assert still_empty == {}

        # After invalidate, rebuild sees the new data.
        cache.invalidate()
        rebuilt = await cache.get_aliases(store)
        assert rebuilt, "cache rebuild did not pick up new aliases"
