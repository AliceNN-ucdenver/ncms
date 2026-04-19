"""State-evolution grammar (Option β from the design discussion).

Generic enough to apply beyond ADRs (medical timelines, project
state, agent memory, audit logs) but small enough to reason about.

Four primitive transitions:

* ``introduces`` — a state begins.  The root of a zone.  Implicit
  for memories with no incoming edge in the subject's graph.
* ``refines`` — same-zone continuation.  Extends the current state
  without replacing it (e.g., "add MFA on top of existing tokens").
* ``supersedes`` — cross-zone transition.  Ends the previous state,
  begins a new one (e.g., "retire JWT, use short-lived tokens").
* ``retires`` — ends a zone without starting a new one in the same
  subject.  Rare but possible (e.g., "discontinue the feature").

Zones are inferred by walking the typed-edge graph:

* A memory with no incoming edge starts a zone.
* ``refines`` edges stay in the zone.
* ``supersedes`` / ``retires`` edges end the zone.  The destination
  of a ``supersedes`` starts a new zone.

"Current" in a subject = the last memory in the most recent zone
that has no outgoing ``supersedes`` or ``retires`` edge.

Production rules (admissibility):

    S          → introduces(M)                       [zone start]
    introduces(M)  → refines(M', M) | supersedes(M, M') | retires(M)
    refines(M, M') → refines(M'', M') | supersedes(M', M'') | retires(M')
    supersedes(M, M')  → refines(M'', M') | supersedes(M', M'') | retires(M')
    retires(M) → ε                                   [terminal]

The LG retriever's bidirectional search only traverses edges
admitted by these rules.  Any edge not matching a production is
inadmissible (e.g., an edge from the auth zone to ADR-033 wouldn't
exist because ADR-033 is in a different subject).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from experiments.temporal_trajectory.corpus import ADR_CORPUS, EDGES, Edge


ADMISSIBLE_TRANSITIONS = frozenset({
    "introduces", "refines", "supersedes", "retires",
})


@dataclass(frozen=True)
class Zone:
    """A grammar-computed state zone — contiguous ``refines`` chain
    rooted at an ``introduces`` or ``supersedes``-target memory."""

    zone_id: int
    subject: str
    memory_ids: tuple[str, ...]  # in chronological order
    start_mid: str               # the zone-opening memory
    terminal_mid: str            # last memory of the zone
    ended_by: str | None         # mid of the memory that supersedes/retires
    ended_transition: str | None  # 'supersedes' | 'retires' | None


def build_subject_graph(subject: str) -> dict[str, list[Edge]]:
    """Return adjacency list of outgoing edges per memory, filtered to
    a single subject's zone chain.

    Only memories whose ``subject`` matches (or whose edge endpoints
    both belong to the subject) are included.
    """
    mem_subjects = {m.mid: m.subject for m in ADR_CORPUS}
    out: dict[str, list[Edge]] = defaultdict(list)
    for edge in EDGES:
        src_subj = mem_subjects.get(edge.src)
        dst_subj = mem_subjects.get(edge.dst)
        if src_subj != subject or dst_subj != subject:
            continue
        if edge.transition not in ADMISSIBLE_TRANSITIONS:
            continue
        out[edge.src].append(edge)
    return dict(out)


def compute_zones(subject: str) -> list[Zone]:
    """Walk the typed-edge graph for ``subject`` and produce the zone
    list in chronological order.

    Algorithm:
      1. Every memory with no incoming admissible edge is a zone root.
      2. From each root, follow ``refines`` until exhausted.  The
         chain is one zone.
      3. When a ``supersedes`` edge is hit, close the zone and mark
         the destination as a new zone root.
      4. When a ``retires`` edge is hit, close the zone; no new zone
         begins.

    Returns zones in the order their roots are first observed.
    """
    out_edges = build_subject_graph(subject)
    incoming: dict[str, list[Edge]] = defaultdict(list)
    for srcs in out_edges.values():
        for e in srcs:
            incoming[e.dst].append(e)

    subj_mems = [m for m in ADR_CORPUS if m.subject == subject]
    subj_mems.sort(key=lambda m: m.observed_at)

    # Roots = memories with no incoming admissible edge.
    root_mids = [m.mid for m in subj_mems if not incoming.get(m.mid)]

    zones: list[Zone] = []
    visited: set[str] = set()

    def _walk_zone(root_mid: str, zid: int) -> Zone:
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
            break  # no outgoing transitions: terminal zone
        for mid in chain:
            visited.add(mid)
        return Zone(
            zone_id=zid,
            subject=subject,
            memory_ids=tuple(chain),
            start_mid=root_mid,
            terminal_mid=chain[-1],
            ended_by=ended_by,
            ended_transition=ended_transition,
        )

    next_roots: list[str] = list(root_mids)
    zid = 0
    while next_roots:
        r = next_roots.pop(0)
        if r in visited:
            continue
        zone = _walk_zone(r, zid)
        zones.append(zone)
        zid += 1
        if (
            zone.ended_transition == "supersedes"
            and zone.ended_by
            and zone.ended_by not in visited
        ):
            next_roots.append(zone.ended_by)

    return zones


def current_zone(subject: str) -> Zone | None:
    """Return the zone with no successor — i.e. the 'current state'.

    In a well-formed grammar, exactly one zone per subject is
    current (no ``supersedes`` or ``retires`` closing it).  If the
    subject's graph has no zones or multiple ungrounded chains,
    return None.
    """
    zones = compute_zones(subject)
    if not zones:
        return None
    # A current zone is one whose terminal memory has no outgoing
    # admissible transition (i.e., not closed by supersedes or retires).
    terminals = [z for z in zones if z.ended_transition is None]
    if len(terminals) == 1:
        return terminals[0]
    # Multiple ungrounded zones — pick the latest by terminal
    # observed_at.
    if terminals:
        by_id = {m.mid: m for m in ADR_CORPUS}
        return max(
            terminals,
            key=lambda z: by_id[z.terminal_mid].observed_at,
        )
    return None


def origin_memory(subject: str) -> str | None:
    """Return the mid of the subject's very first memory — root of the
    earliest zone."""
    zones = compute_zones(subject)
    if not zones:
        return None
    # Earliest zone by start_mid's observed_at.
    by_id = {m.mid: m for m in ADR_CORPUS}
    earliest = min(zones, key=lambda z: by_id[z.start_mid].observed_at)
    return earliest.start_mid


def retirement_memory(subject: str, entity: str) -> str | None:
    """Find the memory that ended an entity's state within the subject.

    Match pipeline (most-specific first):

      1. **Alias expansion** — include the entity's known aliases
         (auto-derived abbreviations: JWT ↔ JSON Web Tokens, PT ↔
         physical therapy, …).  See :mod:`aliases`.
      2. **Stem equality** — Snowball-stemmed word sequences.
      3. **Prefix tolerance** — agentive-noun handling (blocker ↔
         blocked), length ≥ 4, scoped to the narrow retires set per
         edge.

    Returns ``None`` when no admissible edge retires the entity or
    any of its aliases.
    """
    from experiments.temporal_trajectory.aliases import expand_aliases
    from experiments.temporal_trajectory.vocab_induction import _stem

    # Build candidate stem sequences: entity + all aliases.
    query_stems: set[str] = set()
    for surface in expand_aliases(entity):
        stems = [_stem(w) for w in surface.lower().split() if w]
        stem_seq = " ".join(stems)
        if stem_seq:
            query_stems.add(stem_seq)
    if not query_stems:
        return None

    mem_subjects = {m.mid: m.subject for m in ADR_CORPUS}
    for edge in EDGES:
        if mem_subjects.get(edge.src) != subject:
            continue
        if mem_subjects.get(edge.dst) != subject:
            continue
        if edge.transition not in {"supersedes", "retires"}:
            continue
        for retired in edge.retires_entities:
            retired_stems = [_stem(w) for w in retired.lower().split() if w]
            retired_stem_str = " ".join(retired_stems)
            if not retired_stem_str:
                continue
            if retired_stem_str in query_stems:
                return edge.dst
            # Agentive-noun tolerance — prefix match ≥ 4 chars.
            for qs in query_stems:
                if (
                    len(qs) >= 4
                    and len(retired_stem_str) >= 4
                    and (
                        retired_stem_str.startswith(qs)
                        or qs.startswith(retired_stem_str)
                    )
                ):
                    return edge.dst
    return None
