"""L1 subject-vocabulary cache (application layer).

Composes the pure domain helpers in :mod:`ncms.domain.tlg.vocabulary`
with the MemoryStore.  Builds an :class:`InducedVocabulary` from the
corpus of ``ENTITY_STATE`` nodes and caches it in-memory; exposes
subject / entity lookups for the retrieval path.

Subject mapping for NCMS
------------------------

The research code marks every memory with a static ``subject`` field.
NCMS doesn't have that column — we derive it instead: every
``MemoryNode`` with ``node_type = ENTITY_STATE`` has an ``entity_id``
in its metadata, and that entity IS the subject the memory pertains
to.  Linked entities on the backing Memory row form the entity set.

Rebuild policy
--------------

The cache is lazy: first :meth:`get_vocabulary` call scans the store
and memoises the result.  :meth:`invalidate` clears the cache (for
explicit-refresh flows: a maintenance pass, a bulk import, or the
TLG-enabled toggle flipping).  A future enhancement can layer
staleness tracking (rebuild when N new ENTITY_STATE nodes have
landed since the last build) — for Phase 3b the simple lazy +
invalidate-on-demand policy is enough.

See ``docs/p1-plan.md`` §3 and
``docs/temporal-linguistic-geometry.md`` §4.
"""

from __future__ import annotations

import logging

from ncms.domain.models import NodeType
from ncms.domain.tlg import (
    InducedVocabulary,
    SubjectMemory,
    induce_vocabulary,
    lookup_entity,
    lookup_subject,
)

logger = logging.getLogger(__name__)


class VocabularyCache:
    """Lazy, in-memory cache of the L1 ``InducedVocabulary``.

    Thread-safety: NCMS is asyncio-single-threaded on the hot path,
    so a plain attribute-based cache is sufficient.  If we ever move
    induction off the event loop, add an ``asyncio.Lock`` around
    :meth:`_rebuild`.
    """

    def __init__(self) -> None:
        self._vocab: InducedVocabulary | None = None

    def invalidate(self) -> None:
        """Clear the cache — next :meth:`get_vocabulary` call rebuilds."""
        self._vocab = None

    async def get_vocabulary(self, store: object) -> InducedVocabulary:
        """Return the cached vocabulary, rebuilding on first call / after
        :meth:`invalidate`.
        """
        if self._vocab is None:
            self._vocab = await self._rebuild(store)
        return self._vocab

    async def lookup_subject(
        self, query: str, store: object,
    ) -> str | None:
        """Return the subject most strongly implied by ``query`` per the
        induced vocabulary, or ``None``.  Rebuilds the cache on first
        call.
        """
        vocab = await self.get_vocabulary(store)
        return lookup_subject(query, vocab)

    async def lookup_entity(
        self, query: str, store: object,
    ) -> str | None:
        """Return the canonical form of the longest matching entity in
        ``query``, or ``None``.  Rebuilds the cache on first call.
        """
        vocab = await self.get_vocabulary(store)
        return lookup_entity(query, vocab)

    # ── Rebuild ──────────────────────────────────────────────────────

    async def _rebuild(self, store: object) -> InducedVocabulary:
        """Scan the store's ENTITY_STATE nodes and induce a fresh vocab.

        Pure read — does not mutate the store.  A memory without an
        ``entity_id`` in its node metadata is skipped (no subject to
        anchor induction to).  A memory with no linked entities is
        also skipped (no vocabulary to contribute).
        """
        nodes = await store.get_memory_nodes_by_type(  # type: ignore[attr-defined]
            NodeType.ENTITY_STATE.value
        )
        memories: list[SubjectMemory] = []
        for node in nodes:
            subject = node.metadata.get("entity_id") if node.metadata else None
            if not subject:
                continue
            try:
                linked = await store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover — defensive guard
                logger.warning(
                    "TLG vocabulary: could not load entities for memory %s: %s",
                    node.memory_id,
                    exc,
                )
                continue
            if not linked:
                continue
            memories.append(
                SubjectMemory(subject=subject, entities=frozenset(linked))
            )
        vocab = induce_vocabulary(memories)
        logger.info(
            "TLG L1 induction: %d subjects, %d entity tokens",
            len({m.subject for m in memories}),
            len(vocab.entity_lookup),
        )
        return vocab
