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

ADMISSIBLE_TRANSITIONS: frozenset[str] = frozenset(
    {
        "introduces",
        "refines",
        "supersedes",
        "retires",
    }
)


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


@dataclass(frozen=True)
class CausalZone:
    """CTLG v8+: a connected component under ``CAUSED_BY`` + ``ENABLES``.

    Dual to :class:`Zone` (which is a refines-connected component).
    Causation is many-to-many — a single event can both cause and
    be caused by multiple others — so causal zones can span
    multiple subjects and merge into larger DAGs than refines
    zones.

    The trajectory grammar's causal subgrammar ``G_{tr,c}``
    generates walks within a causal zone; the dispatcher uses the
    zone's ``root_causes`` as starting points for backward walks
    (``cause_of`` / ``chain_cause_of``) and ``leaf_effects`` for
    forward walks (``effect_of``).

    Attributes
    ----------
    zone_id
        Stable id within the domain's graph.
    member_ids
        Memory ids in this causal zone.  Unordered frozenset because
        a causal DAG doesn't have a single linear ordering (unlike
        the refines chain).
    root_causes
        Nodes with no incoming ``CAUSED_BY`` edges within the zone
        — the backward-walk starting points.
    leaf_effects
        Nodes with no outgoing ``CAUSED_BY`` edges within the zone
        — the forward-walk starting points.
    subject_coverage
        Subjects touched by this zone.  A causal zone may span
        multiple subjects (``"audit"`` → ``"auth-service"``,
        ``"billing-service"``).
    """

    zone_id: int
    member_ids: frozenset[str]
    root_causes: tuple[str, ...]
    leaf_effects: tuple[str, ...]
    subject_coverage: frozenset[str] = frozenset()


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
            (e for e in outs if e.transition == "refines"),
            None,
        )
        next_super = next(
            (e for e in outs if e.transition == "supersedes"),
            None,
        )
        next_retire = next(
            (e for e in outs if e.transition == "retires"),
            None,
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
        if zone.ended_transition == "supersedes" and zone.ended_by and zone.ended_by not in visited:
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
    return " ".join(_stem(w) for w in surface.lower().split() if w)


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
                    and (retired_seq.startswith(qs) or qs.startswith(retired_seq))
                ):
                    return edge.dst
    return None


# ---------------------------------------------------------------------------
# Causal zones (CTLG v8+)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalEdge:
    """A single ``CAUSED_BY`` or ``ENABLES`` edge in the causal graph.

    Distinct from :class:`ZoneEdge` (refines/supersedes/retires), which
    only admits state-evolution transitions.  Causal edges cross
    subject boundaries, don't participate in zone closure, and carry
    cue provenance in ``metadata`` for explainability.
    """

    src: str  # effect memory id
    dst: str  # cause memory id
    edge_type: str  # "caused_by" or "enables"
    cue_type: str = ""  # "CAUSAL_EXPLICIT" / "CAUSAL_ALTLEX"
    confidence: float = 1.0


def build_causal_zones(
    causal_edges: Iterable[CausalEdge],
    memory_subjects: Mapping[str, str] | None = None,
) -> list[CausalZone]:
    """Compute causal zones from a set of ``CausalEdge``s.

    A causal zone is a weakly-connected component in the causal
    graph (``CAUSED_BY`` + ``ENABLES``).  Since causation is
    many-to-many, zones can span subjects and merge on shared
    cause/effect nodes.

    The returned zones identify:
      * ``root_causes`` — nodes with no incoming causal edge
      * ``leaf_effects`` — nodes with no outgoing causal edge
      * ``subject_coverage`` — set of subjects touched (from
        ``memory_subjects`` if provided; empty set otherwise)

    Dispatcher callers use ``root_causes`` as starting points for
    ``cause_of`` / ``chain_cause_of`` backward walks, and
    ``leaf_effects`` for ``effect_of`` forward walks.
    """
    edges_list = list(causal_edges)
    if not edges_list:
        return []

    # Build undirected adjacency for weakly-connected-component BFS.
    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges_list:
        adj[e.src].add(e.dst)
        adj[e.dst].add(e.src)

    # Track directed in/out degree for root/leaf identification.
    in_count: dict[str, int] = defaultdict(int)
    out_count: dict[str, int] = defaultdict(int)
    for e in edges_list:
        out_count[e.src] += 1  # src has outgoing edge
        in_count[e.dst] += 1  # dst has incoming edge

    visited: set[str] = set()
    zones: list[CausalZone] = []
    zone_counter = 0
    for start in adj:
        if start in visited:
            continue
        # BFS to find all nodes in this weakly-connected component.
        component: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            stack.extend(adj[node] - visited)

        # Within component:
        #   root_cause = node with no OUTGOING caused_by edge, i.e.
        #     no cause of its own — it originates the chain.
        #   leaf_effect = node with no INCOMING caused_by edge, i.e.
        #     nothing is caused by it — it's a pure terminal effect.
        # (Recall: CAUSED_BY points effect→cause, so having an
        # outgoing caused_by means "I have a cause".)
        roots = tuple(sorted(m for m in component if out_count.get(m, 0) == 0))
        leaves = tuple(sorted(m for m in component if in_count.get(m, 0) == 0))
        subjects: frozenset[str]
        if memory_subjects is not None:
            subjects = frozenset(s for m in component if (s := memory_subjects.get(m)) is not None)
        else:
            subjects = frozenset()

        zones.append(
            CausalZone(
                zone_id=zone_counter,
                member_ids=frozenset(component),
                root_causes=roots,
                leaf_effects=leaves,
                subject_coverage=subjects,
            )
        )
        zone_counter += 1
    return zones
