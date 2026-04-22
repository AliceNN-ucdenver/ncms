"""L3 — minimal query entity extraction (post-v6).

Historical context
------------------

Before v6 this module hosted a hand-coded regex classifier: ~15
production rules that matched seed-marker vocabularies
(``SEED_INTENT_MARKERS``) against the query text to pick a TLG
grammar intent (``current`` / ``origin`` / ``before_named`` / etc.).

The P2 SLM's 6th head (``shape_intent_head``, trained on 747 MSEB
gold queries with 100% accuracy on the clean-gold domains) now
owns intent classification.  The production-rule matchers and the
seed-marker vocabulary have been deleted — see
``ncms.application.tlg.vocabulary_cache`` for the two small seed
sets that survived (used only for L2 marker induction at ingest
time, not for query parsing).

What this module still does
---------------------------

1. **Subject resolution** — ``lookup_subject`` against the L1
   ``InducedVocabulary``.  Deterministic vocabulary match, not
   regex; maps query tokens to corpus subject IDs.

2. **Target-entity extraction** — ``_extract_event_name`` finds
   named entities (issue-vocabulary or L1-vocabulary hits) in the
   query so dispatchers that need a specific target (sequence,
   predecessor, before_named, …) can reference it.  This is a
   surface-form NER task and will move to a ``target_entity``
   slot head in v7; until then it's a compact regex-adjacent
   helper kept in-process.

3. **Data types** — ``ParserContext`` + ``QueryStructure`` + the
   ``compute_domain_nouns`` helper are schema containers the
   dispatcher consumes.

The exported ``analyze_query`` returns a ``QueryStructure`` with
``intent=None``.  The caller (``retrieve_lg``) fills ``intent``
from the SLM's ``shape_intent_head`` output before dispatching
to a walker.

See ``docs/completed/p2-plan.md`` for the SLM design and
``docs/mseb-results.md`` §5 for per-head evidence.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ncms.domain.tlg.aliases import expand_aliases
from ncms.domain.tlg.markers import InducedEdgeMarkers
from ncms.domain.tlg.vocabulary import (
    InducedVocabulary,
    _stem,
    lookup_entity,
    lookup_subject,
)


# ---------------------------------------------------------------------------
# Parser inputs / outputs
# ---------------------------------------------------------------------------


class _MemoryLike(Protocol):
    """Minimal shape the domain-noun helper needs from a memory.

    Reads only ``subject`` + ``entities``; used for
    ``compute_domain_nouns`` so the caller can supply mocks /
    trimmed views without pulling the full Memory class.
    """

    subject: str | None
    entities: frozenset[str]


@dataclass(frozen=True)
class ParserContext:
    """Everything the parser reads besides the query itself.

    Built once per batch of queries (typically from
    :class:`VocabularyCache`).  Immutable so callers can share
    freely across threads / coroutines.

    Post-v6, the L3 parser only uses ``vocabulary``, ``issue_entities``,
    and ``domain_nouns``.  The remaining fields (``induced_markers``,
    ``aliases``, ``content_*_markers``) are carried on the context
    for downstream consumers — zone dispatch, aliased-entity
    expansion, and L2 induction — that still read them.
    """

    vocabulary: InducedVocabulary
    induced_markers: InducedEdgeMarkers | None = None
    aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    issue_entities: frozenset[str] = field(default_factory=frozenset)
    domain_nouns: frozenset[str] = field(default_factory=frozenset)
    content_current_markers: frozenset[str] = field(
        default_factory=frozenset,
    )
    content_origin_markers: frozenset[str] = field(
        default_factory=frozenset,
    )


@dataclass(frozen=True)
class QueryStructure:
    """Structured query analysis — subject + target_entity only.

    Post-v6, ``intent`` is always ``None`` on output from
    :func:`analyze_query`.  The grammar dispatcher fills it in
    from the SLM ``shape_intent_head`` classification before
    routing to a walker.

    Consumers should branch on the dispatcher-assigned intent, not
    on this struct's ``intent`` field.  Kept here for
    compatibility with the dispatcher's data model.
    """

    intent: str | None
    subject: str | None
    target_entity: str | None
    detected_marker: str | None = None
    secondary_entity: str | None = None
    range_start: str | None = None
    range_end: str | None = None

    def has_grammar_answer(self) -> bool:
        """Deprecated — kept for call-site compatibility.

        The post-v6 dispatcher reads ``intent`` directly and checks
        for ``None``; this method will be removed after the
        ``intent`` field itself can be dropped from the struct.
        """
        return self.intent is not None and self.intent != "none"


# ---------------------------------------------------------------------------
# Domain-noun helper (computed once per corpus; lifted to context)
# ---------------------------------------------------------------------------


def compute_domain_nouns(
    memories: Iterable[_MemoryLike],
    *,
    min_memories_per_subject: int = 3,
    threshold_fraction: float = 0.6,
) -> frozenset[str]:
    """Entities that appear in ≥``threshold_fraction`` of any subject's
    memories.  Used by target-entity extraction to prefer distinctive
    event names over the subject's topical nouns.

    Historical note: lived alongside the regex production rules
    before v6; remains here because it's used by
    :func:`_extract_event_name`.
    """
    by_subject_count: dict[str, int] = {}
    counts_by_subject: dict[str, Counter[str]] = {}
    for mem in memories:
        subj = mem.subject
        if subj is None:
            continue
        by_subject_count[subj] = by_subject_count.get(subj, 0) + 1
        counts = counts_by_subject.setdefault(subj, Counter())
        for ent in mem.entities:
            counts[ent] += 1

    domain: set[str] = set()
    for subj, size in by_subject_count.items():
        if size < min_memories_per_subject:
            continue
        # Round UP so 60% of 3 = 2, not 1.  Previously used
        # int(0.6*3) = 1 which made every entity in any memory a
        # domain-noun whenever size >= 2 — far too loose.
        floor = max(1, math.ceil(threshold_fraction * size))
        per_subject = counts_by_subject.get(subj) or Counter()
        for ent, n in per_subject.items():
            if n >= floor:
                domain.add(ent)
    return frozenset(domain)


# ---------------------------------------------------------------------------
# Target-entity extraction helpers
# ---------------------------------------------------------------------------


def _word_like(token: str, query_lower: str) -> bool:
    """True if ``token`` appears as a whole word in ``query_lower``."""
    pattern = r"\b" + re.escape(token.lower()) + r"\b"
    return re.search(pattern, query_lower) is not None


def _prefer_issue_entity(
    candidates: Iterable[str], ctx: ParserContext,
) -> str | None:
    """Pick the issue-like entity first; fall back to first candidate."""
    for cand in candidates:
        if cand.lower() in ctx.issue_entities:
            return cand
    # No issue hit — defer to caller's ordering.
    return None


def _extract_event_names(
    raw: str, ctx: ParserContext, *, max_entities: int = 2,
) -> list[str]:
    """Find up to ``max_entities`` named entities in the query.

    Returns entities in order of appearance (after skipping
    domain-topical nouns like "authentication").  Two-entity
    dispatchers (before_named, interval) use the first as
    ``target_entity`` and the second as ``secondary_entity``.

    Strategy:

    1. Walk L1 vocabulary entities longest-first so multi-word
       matches ("session cookies") preempt single-word substring
       matches.
    2. Track the START POSITION of each match so we can order them
       by their position in the query (left-to-right).
    3. Skip entities in ``domain_nouns`` (subject-topical nouns
       like "authentication" that are too general).
    4. When no L1 entity matches, fall back to a single issue-seed
       word (``blocker`` / ``bug`` / ``problem`` / …).
    """
    query_lower = raw.lower()

    # (position, canonical) pairs — use a longest-first walk so
    # multi-word entities are detected before their sub-tokens;
    # but de-dupe by covered character range so sub-token matches
    # inside a larger match don't double-count.
    found: list[tuple[int, str]] = []
    covered_ranges: list[tuple[int, int]] = []

    entity_names = sorted(
        ctx.vocabulary.entity_lookup.keys(),
        key=len, reverse=True,
    )
    for ent in entity_names:
        if ent in ctx.domain_nouns:
            continue
        pattern = r"\b" + re.escape(ent.lower()) + r"\b"
        for m in re.finditer(pattern, query_lower):
            start, end = m.start(), m.end()
            # Skip if this match is inside a longer entity we
            # already captured.
            if any(cs <= start and end <= ce for cs, ce in covered_ranges):
                continue
            canonical = ctx.vocabulary.entity_lookup.get(ent)
            if canonical:
                found.append((start, canonical))
                covered_ranges.append((start, end))

    # Left-to-right, de-duped order.
    found.sort(key=lambda t: t[0])
    seen: set[str] = set()
    ordered: list[str] = []
    for _, canonical in found:
        if canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(canonical)
        if len(ordered) >= max_entities:
            break

    if ordered:
        return ordered

    # No L1 match — issue-seed fallback, single entity.
    for token in re.findall(r"\b\w+\b", query_lower):
        if token in ctx.issue_entities:
            return [token]

    return []


def _extract_event_name(raw: str, ctx: ParserContext) -> str | None:
    """Back-compat single-entity extraction.  See
    :func:`_extract_event_names` for the multi-entity form used by
    before_named / interval dispatchers.
    """
    entities = _extract_event_names(raw, ctx, max_entities=1)
    return entities[0] if entities else None


def _canonicalize_target(
    raw: str | None, ctx: ParserContext,
) -> str | None:
    """Map a raw entity surface form to the L1 canonical name."""
    if raw is None:
        return None
    hit = ctx.vocabulary.entity_lookup.get(raw.lower())
    if hit is not None:
        return hit
    return raw


# ---------------------------------------------------------------------------
# Public entry point — subject + target_entity only (post-v6)
# ---------------------------------------------------------------------------


def analyze_query(
    query: str, ctx: ParserContext,
) -> QueryStructure:
    """Extract subject + target entity + secondary entity from a query.

    Post-v6 this function does NOT classify intent — the SLM's
    ``shape_intent_head`` owns that.  Returns ``intent=None``;
    the grammar dispatcher overrides it from the SLM output
    before routing.

    When the query mentions two L1-vocabulary entities (e.g.
    "Did session cookies come before OAuth?"), the first is
    returned as ``target_entity`` and the second as
    ``secondary_entity`` — left-to-right order.  Two-entity
    dispatchers (before_named, interval) consume both slots.
    """
    subject = lookup_subject(query, ctx.vocabulary)
    entities = _extract_event_names(query, ctx, max_entities=2)
    target_entity = entities[0] if entities else None
    secondary_entity = entities[1] if len(entities) > 1 else None
    return QueryStructure(
        intent=None,
        subject=subject,
        target_entity=target_entity,
        secondary_entity=secondary_entity,
        detected_marker=None,
    )


__all__ = [
    "ParserContext",
    "QueryStructure",
    "analyze_query",
    "compute_domain_nouns",
    "expand_aliases",
]
