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
    InducedContentMarkers,
    InducedEdgeMarkers,
    InducedVocabulary,
    ParserContext,
    SubjectMemory,
    compute_domain_nouns,
    expand_aliases,
    induce_aliases,
    induce_content_markers,
    induce_vocabulary,
    lookup_entity,
    lookup_subject,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# L2 induction seeds — USED AT INGEST TIME ONLY.
# ---------------------------------------------------------------------------
# These are seed vocabularies for the L2 marker inducer that runs when
# memory-nodes are ingested: ``current`` seed words anchor the
# terminal-side of a state chain, ``origin`` seeds anchor the root
# side.  The inducer then expands from these seeds by scanning actual
# memory text.
#
# Historically these lived inside ``SEED_INTENT_MARKERS`` in
# ``ncms.domain.tlg.query_parser`` alongside query-parsing regex
# vocabularies (``retirement``, ``cause_of``, ``still``).  With the
# v6 SLM handling query-shape classification, the query-side regex
# vocabularies are gone and these two seed sets are the only
# survivors — relocated here so vocabulary_cache owns them directly
# and the L3 query parser can shed all its regex machinery.
_L2_SEED_CURRENT: frozenset[str] = frozenset({
    "current", "currently", "now", "today", "latest",
    "present", "presently", "as of",
})
_L2_SEED_ORIGIN: frozenset[str] = frozenset({
    "original", "first", "initial", "earliest", "starting",
    "started", "start", "begin", "began", "kickoff", "onset",
})

# Irreducible English issue-seed vocabulary used by the L3 parser
# when preferring "issue entity" over subject noun for cause_of
# target extraction.  Kept here (not in query_parser) because the
# parser itself is entity-extraction-only now — the L2 side still
# needs this seed for its domain-agnostic issue vocabulary.
_ISSUE_SEED: frozenset[str] = frozenset({
    "blocker", "blockers", "blocked",
    "delay", "delays", "delayed",
    "issue", "issues",
    "problem", "problems",
    "incident", "incidents",
    "bug", "bugs",
    "error", "errors",
    "failure", "failures",
})


class VocabularyCache:
    """Lazy, in-memory cache of the L1 ``InducedVocabulary``.

    Thread-safety: NCMS is asyncio-single-threaded on the hot path,
    so a plain attribute-based cache is sufficient.  If we ever move
    induction off the event loop, add an ``asyncio.Lock`` around
    :meth:`_rebuild`.
    """

    def __init__(self) -> None:
        self._vocab: InducedVocabulary | None = None
        self._aliases: dict[str, frozenset[str]] | None = None
        self._domain_nouns: frozenset[str] | None = None
        self._content_markers: InducedContentMarkers | None = None
        self._parser_context_key: tuple[int, ...] | None = None
        self._parser_context: ParserContext | None = None

    def invalidate(self) -> None:
        """Clear every cached artefact — next accessor rebuilds.

        Clears vocab, aliases, domain-nouns, content-markers, and
        the parser-context snapshot in one call since all five are
        derived from the same corpus scan.  Callers hook this to the
        ingest path (see :meth:`MemoryService.invalidate_tlg_vocabulary`).
        """
        self._vocab = None
        self._aliases = None
        self._domain_nouns = None
        self._content_markers = None
        self._parser_context_key = None
        self._parser_context = None

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

    async def get_aliases(
        self, store: object,
    ) -> dict[str, frozenset[str]]:
        """Return the cached alias mapping, rebuilding on first call.

        Built from the same entity universe as :meth:`get_vocabulary`:
        every entity name that appears in an ENTITY_STATE node plus
        every entry in a SUPERSEDES / SUPERSEDED_BY edge's
        ``retires_entities`` set.  That's the universe a query may
        reference — aliases over any other entities would never be
        matched.
        """
        if self._aliases is None:
            self._aliases = await self._rebuild_aliases(store)
        return self._aliases

    async def expand(
        self, entity: str, store: object,
    ) -> frozenset[str]:
        """``{entity}`` unioned with its known aliases (case-insensitive)."""
        aliases = await self.get_aliases(store)
        return expand_aliases(entity, aliases)

    async def get_parser_context(
        self,
        store: object,
        *,
        induced_markers: InducedEdgeMarkers | None = None,
    ) -> ParserContext:
        """Compose the full :class:`ParserContext` for
        :func:`ncms.domain.tlg.analyze_query`.

        Caches every derived artefact — vocabulary, aliases, domain
        nouns, content markers, and the assembled :class:`ParserContext`
        itself.  Only the ``induced_markers`` argument varies per
        call (it comes from a fresh read of
        ``grammar_transition_markers`` so callers see newly-persisted
        L2 vocabulary without an invalidate), so we cache the context
        keyed on marker-bucket identity.  A fresh marker table causes
        exactly one re-assembly; subsequent calls in the same epoch
        are zero-cost.
        """
        vocab = await self.get_vocabulary(store)
        aliases = await self.get_aliases(store)
        if self._domain_nouns is None:
            self._domain_nouns = await self._compute_domain_nouns(store)
        if self._content_markers is None:
            self._content_markers = await self._compute_content_markers(store)
        markers = induced_markers or InducedEdgeMarkers(markers={})
        marker_key = tuple(sorted(
            (t, tuple(sorted(heads)))
            for t, heads in markers.markers.items()
        ))
        if (
            self._parser_context is not None
            and self._parser_context_key == marker_key
        ):
            return self._parser_context
        ctx = ParserContext(
            vocabulary=vocab,
            induced_markers=markers,
            aliases=aliases,
            issue_entities=_ISSUE_SEED,
            domain_nouns=self._domain_nouns,
            content_current_markers=self._content_markers.current_candidates,
            content_origin_markers=self._content_markers.origin_candidates,
        )
        self._parser_context = ctx
        self._parser_context_key = marker_key
        return ctx

    # ── Rebuild ──────────────────────────────────────────────────────

    async def _rebuild_aliases(
        self, store: object,
    ) -> dict[str, frozenset[str]]:
        """Mine the alias table from the store's entity universe.

        Scans two sources:

        1. Every entity linked to an ENTITY_STATE node's backing
           Memory (matches the vocabulary-rebuild universe).
        2. Every entry in a SUPERSEDES / SUPERSEDED_BY edge's
           ``retires_entities`` set — so a query can reference
           retired entities using the short form even if the
           reconciler recorded the long form.
        """
        universe: set[str] = set()
        nodes = await store.get_memory_nodes_by_type(  # type: ignore[attr-defined]
            NodeType.ENTITY_STATE.value
        )
        for node in nodes:
            try:
                linked = await store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover — defensive guard
                logger.warning(
                    "TLG aliases: could not load entities for memory %s: %s",
                    node.memory_id,
                    exc,
                )
                continue
            universe.update(linked)

        try:
            edges = await store.list_graph_edges_by_type(  # type: ignore[attr-defined]
                ["supersedes", "superseded_by"]
            )
        except Exception as exc:  # pragma: no cover — defensive guard
            logger.warning(
                "TLG aliases: could not list supersession edges: %s", exc,
            )
            edges = []
        for edge in edges:
            universe.update(edge.retires_entities)

        aliases = induce_aliases(universe)
        if aliases:
            group_count = len({
                frozenset({k, *v}) for k, v in aliases.items()
            })
            logger.info(
                "TLG L1 aliases: %d surface forms, %d alias groups",
                len(aliases),
                group_count,
            )
        return aliases

    async def _compute_content_markers(
        self, store: object,
    ) -> InducedContentMarkers:
        """Mine content-derived current/origin markers from the
        subject graphs.

        Terminal IDs come from ``current_zone`` of each subject;
        root IDs are each subject's earliest ENTITY_STATE node.
        Uses the zone machinery already loaded elsewhere — we
        rebuild it per-subject here because induction spans the
        whole corpus, not a single dispatch call.
        """
        from ncms.application.tlg.dispatch import _load_subject_zones
        from ncms.domain.tlg.zones import current_zone

        nodes = await store.get_memory_nodes_by_type(  # type: ignore[attr-defined]
            NodeType.ENTITY_STATE.value,
        )
        subject_nodes: dict[str, list] = {}
        for node in nodes:
            subj = node.metadata.get("entity_id") if node.metadata else None
            if not subj:
                continue
            subject_nodes.setdefault(subj, []).append(node)

        terminal_ids: set[str] = set()
        root_ids: set[str] = set()
        for subject, subj_nodes in subject_nodes.items():
            if not subj_nodes:
                continue
            # Subject's earliest memory — by observed_at, then created_at.
            earliest = min(
                subj_nodes,
                key=lambda n: (n.observed_at or n.created_at),
            )
            root_ids.add(earliest.memory_id)
            # Current-zone terminal(s).
            try:
                zones, node_index, _ = await _load_subject_zones(
                    store, subject,
                )
            except Exception as exc:  # pragma: no cover — defensive guard
                logger.warning(
                    "TLG content markers: zone build failed for %s: %s",
                    subject, exc,
                )
                continue
            terminal_zone = current_zone(zones, node_index)
            if terminal_zone is not None:
                term_node = node_index.get(terminal_zone.terminal_mid)
                if term_node is not None:
                    terminal_ids.add(term_node.memory_id)

        # Load memory objects we care about (roots ∪ terminals ∪ middles).
        relevant_ids = set()
        for subj_nodes in subject_nodes.values():
            for node in subj_nodes:
                relevant_ids.add(node.memory_id)
        memories: list = []
        for memory_id in relevant_ids:
            try:
                mem = await store.get_memory(memory_id)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover — defensive guard
                continue
            if mem is not None:
                memories.append(mem)

        return induce_content_markers(
            memories,
            terminal_ids=terminal_ids,
            root_ids=root_ids,
            seed_current=_L2_SEED_CURRENT,
            seed_origin=_L2_SEED_ORIGIN,
        )

    async def _compute_domain_nouns(
        self, store: object,
    ) -> frozenset[str]:
        """Build the domain-noun frozenset by scanning the same
        ENTITY_STATE universe the vocabulary uses.

        Memory objects have a ``subject`` field only as metadata —
        we lift it from the ENTITY_STATE node's ``entity_id``.
        """
        nodes = await store.get_memory_nodes_by_type(  # type: ignore[attr-defined]
            NodeType.ENTITY_STATE.value
        )

        class _MemView:
            __slots__ = ("subject", "entities")

            def __init__(self, subject: str | None, entities: frozenset[str]):
                self.subject = subject
                self.entities = entities

        views: list[_MemView] = []
        for node in nodes:
            subject = node.metadata.get("entity_id") if node.metadata else None
            if not subject:
                continue
            try:
                linked = await store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover — defensive guard
                logger.warning(
                    "TLG domain-nouns: could not load entities for memory %s: %s",
                    node.memory_id,
                    exc,
                )
                continue
            views.append(_MemView(subject=subject, entities=frozenset(linked)))
        return compute_domain_nouns(views)

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
