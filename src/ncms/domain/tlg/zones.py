"""State-zone computation (L3 grammar).

Port of ``experiments/temporal_trajectory/grammar.py`` adapted for
NCMS: inputs (memories + edges + content-lookup) are passed as
parameters instead of reading globals.  Application-layer callers
(``application/tlg/dispatch.py``) compose the input from the
MemoryStore; this module is pure.

Zones formalise the "current state" of a subject's entity:

* **introduces** — the subject's first memory (implicit root of a zone).
* **refines** — same-zone continuation.
* **supersedes** — cross-zone transition (ends old zone, opens new).
* **retires** — ends a zone without opening a new one.

Zone semantics derived from the production-rule system in
``docs/temporal-linguistic-geometry.md`` §5:

    S              -> introduces(M)
    introduces(M)  -> refines(M', M) | supersedes(M, M') | retires(M)
    refines(M, M') -> refines(M'', M') | supersedes(M', M'') | retires(M')
    supersedes(M, M') -> refines(M'', M') | supersedes(M', M'') | retires(M')
    retires(M)     -> epsilon

The dispatcher uses ``current_zone`` for "current-state" queries,
``origin_memory`` for "first/original" queries, and
``retirement_memory`` for "still-using?" queries.  All three work on
the same zone-list produced by :func:`compute_zones`.

NCMS edge-type mapping:

* SUPERSEDES -> supersedes
* REFINES    -> refines
* (no NCMS edge type corresponds to ``retires``) — reconciliation
  doesn't emit explicit retirement edges; ``retirement_memory``
  falls back to the structural ``retires_entities`` set on
  SUPERSEDES edges, which Phase 1 already populates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import snowballstemmer

from ncms.domain.tlg.aliases import expand_aliases

_STEMMER = snowballstemmer.stemmer("english")


# ---------------------------------------------------------------------------
# Admissible transitions for zone walks
# ---------------------------------------------------------------------------

ADMISSIBLE_TRANSITIONS: frozenset[str] = frozenset({
    "introduces", "refines", "supersedes", "retires",
})


# ---------------------------------------------------------------------------
# Inputs + outputs
# ---------------------------------------------------------------------------


class _HasIdAndTime(Protocol):
    """Structural protocol for memory-like records used in zone walks.

    Zone computation only needs two things from each record: its
    identifier (``id``) and a chronological ordering key
    (``observed_at``).  ``retirement_memory`` reads retirement sets
    off the edges, not the memories themselves, so we don't need
    content here.
    """

    id: str
    observed_at: datetime | None


@dataclass(frozen=True)
class ZoneEdge:
    """One admissible transition between two memories in a subject's graph.

    ``transition`` is the normalised verb (``supersedes`` /
    ``refines``); callers translate from ``EdgeType`` before handing
    us the input.  ``retires_entities`` echoes
    :attr:`GraphEdge.retires_entities` so :func:`retirement_memory`
    can match without re-fetching the edge.
    """

    src: str
    dst: str
    transition: str
    retires_entities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Zone:
    """A contiguous ``refines`` chain rooted at an ``introduces`` or
    ``supersedes``-target memory."""

    zone_id: int
    subject: str
    memory_ids: tuple[str, ...]
    start_mid: str
    terminal_mid: str
    ended_by: str | None
    ended_transition: str | None


# ---------------------------------------------------------------------------
# Graph shaping
# ---------------------------------------------------------------------------


def build_subject_graph(
    edges: Iterable[ZoneEdge],
    subject_memory_ids: set[str],
) -> dict[str, list[ZoneEdge]]:
    """Adjacency list of admissible edges scoped to a subject.

    Keeps only edges whose endpoints are both in ``subject_memory_ids``
    — a subject's zone chain is isolated from other subjects.
    """
    out: dict[str, list[ZoneEdge]] = defaultdict(list)
    for edge in edges:
        if edge.transition not in ADMISSIBLE_TRANSITIONS:
            continue
        if edge.src not in subject_memory_ids or edge.dst not in subject_memory_ids:
            continue
        out[edge.src].append(edge)
    return dict(out)


def _walk_zone(
    root_mid: str,
    zone_id: int,
    subject: str,
    out_edges: Mapping[str, list[ZoneEdge]],
    visited: set[str],
) -> Zone:
    """Follow ``refines`` from ``root_mid`` until a closer fires."""
    chain: list[str] = [root_mid]
    cur = root_mid
    ended_by: str | None = None
    ended_transition: str | None = None
    while True:
        outs = out_edges.get(cur, [])
        next_refine = next(
            (e for e in outs if e.transition == "refines"), None,
        )
        next_super = next(
            (e for e in outs if e.transition == "supersedes"), None,
        )
        next_retire = next(
            (e for e in outs if e.transition == "retires"), None,
        )
        if next_refine is not None:
            chain.append(next_refine.dst)
            cur = next_refine.dst
            continue
        if next_super is not None:
            ended_by = next_super.dst
            ended_transition = "supersedes"
            break
        if next_retire is not None:
            ended_by = next_retire.dst or None
            ended_transition = "retires"
            break
        break
    visited.update(chain)
    return Zone(
        zone_id=zone_id,
        subject=subject,
        memory_ids=tuple(chain),
        start_mid=root_mid,
        terminal_mid=chain[-1],
        ended_by=ended_by,
        ended_transition=ended_transition,
    )


def compute_zones(
    subject: str,
    subject_memories: list[_HasIdAndTime],
    edges: Iterable[ZoneEdge],
) -> list[Zone]:
    """Walk the admissible-edge graph for ``subject`` and produce the
    zone list in chronological root order.

    Zone-root rule: a memory with no incoming admissible edge.  From
    each root, follow ``refines`` until a ``supersedes`` or
    ``retires`` closer fires.  A ``supersedes`` closer promotes its
    destination to a new zone root.  A ``retires`` closer ends the
    chain without opening a new zone.
    """
    subject_memory_ids = {m.id for m in subject_memories}
    out_edges = build_subject_graph(edges, subject_memory_ids)

    incoming: dict[str, list[ZoneEdge]] = defaultdict(list)
    for srcs in out_edges.values():
        for edge in srcs:
            incoming[edge.dst].append(edge)

    ordered = sorted(
        subject_memories,
        key=lambda m: m.observed_at or datetime.min,
    )
    root_mids = [m.id for m in ordered if not incoming.get(m.id)]

    zones: list[Zone] = []
    visited: set[str] = set()
    pending: list[str] = list(root_mids)
    zone_id = 0
    while pending:
        root = pending.pop(0)
        if root in visited:
            continue
        zone = _walk_zone(root, zone_id, subject, out_edges, visited)
        zones.append(zone)
        zone_id += 1
        if (
            zone.ended_transition == "supersedes"
            and zone.ended_by
            and zone.ended_by not in visited
        ):
            pending.append(zone.ended_by)
    return zones


# ---------------------------------------------------------------------------
# Zone queries
# ---------------------------------------------------------------------------


def current_zone(
    zones: list[Zone],
    memory_index: Mapping[str, _HasIdAndTime],
) -> Zone | None:
    """Return the zone whose terminal has no successor, i.e. the
    current state.

    A well-formed grammar has exactly one such zone per subject.  If
    there are multiple ungrounded chains we pick the one whose
    terminal is newest (``observed_at``).  If there are none, return
    None.
    """
    if not zones:
        return None
    terminals = [z for z in zones if z.ended_transition is None]
    if len(terminals) == 1:
        return terminals[0]
    if terminals:
        def _term_time(zone: Zone) -> datetime:
            mem = memory_index.get(zone.terminal_mid)
            return (mem.observed_at if mem else None) or datetime.min
        return max(terminals, key=_term_time)
    return None


def origin_memory(
    zones: list[Zone],
    memory_index: Mapping[str, _HasIdAndTime],
) -> str | None:
    """Return the ID of the root of the earliest zone (the subject's
    first observed memory).  ``None`` for empty input.
    """
    if not zones:
        return None
    def _start_time(zone: Zone) -> datetime:
        mem = memory_index.get(zone.start_mid)
        return (mem.observed_at if mem else None) or datetime.min
    return min(zones, key=_start_time).start_mid


# ---------------------------------------------------------------------------
# Retirement lookup — stem + alias + prefix matching
# ---------------------------------------------------------------------------


def _stem(word: str) -> str:
    return _STEMMER.stemWord(word.lower())


def _stem_sequence(surface: str) -> str:
    return " ".join(
        _stem(w) for w in surface.lower().split() if w
    )


def retirement_memory(
    entity: str,
    edges: Iterable[ZoneEdge],
    subject_memory_ids: set[str],
    *,
    aliases: Mapping[str, frozenset[str]] | None = None,
) -> str | None:
    """Find the memory that ended ``entity``'s state within a subject.

    Three-tier match:

    1. **Alias expansion** — stem the query entity and all its aliases,
       build a candidate set of stem sequences.
    2. **Stem equality** — any ``retires_entities`` entry whose stems
       match any candidate stem sequence.
    3. **Prefix tolerance** — agentive-noun handling (``blocker`` /
       ``blocked``): ≥4-char stems, scoped to the retires set so we
       don't over-match.

    Returns the ``dst`` MemoryNode ID of the first admissible edge
    that fires, or ``None`` when no retirement edge covers ``entity``.
    Admissible transitions: ``supersedes`` and ``retires``.
    """
    # Build candidate stem sequences: entity + all aliases.
    candidate_surfaces: set[str] = {entity}
    if aliases is not None:
        candidate_surfaces |= expand_aliases(entity, dict(aliases))
    query_stems: set[str] = set()
    for surface in candidate_surfaces:
        seq = _stem_sequence(surface)
        if seq:
            query_stems.add(seq)
    if not query_stems:
        return None

    for edge in edges:
        if edge.src not in subject_memory_ids or edge.dst not in subject_memory_ids:
            continue
        if edge.transition not in {"supersedes", "retires"}:
            continue
        for retired in edge.retires_entities:
            retired_seq = _stem_sequence(retired)
            if not retired_seq:
                continue
            # Tier 2 — stem equality.
            if retired_seq in query_stems:
                return edge.dst
            # Tier 3 — prefix tolerance for agentive nouns.
            for qs in query_stems:
                if (
                    len(qs) >= 4
                    and len(retired_seq) >= 4
                    and (
                        retired_seq.startswith(qs)
                        or qs.startswith(retired_seq)
                    )
                ):
                    return edge.dst
    return None
