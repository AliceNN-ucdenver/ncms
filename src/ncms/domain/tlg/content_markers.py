"""Content-derived current/origin marker induction.

Port of ``experiments/temporal_trajectory/marker_induction.py`` with
the corpus lifted to parameters.  Extends the parser's current /
origin marker vocabulary with verb heads that discriminate between
"current state" memories, "origin" memories, and the middle of the
supersession chain.

Two families mined from memory content:

* **Current-state markers** — verb heads in the content of memories
  that *terminate* a zone with no closer (``current_zone``
  terminals).  Seed: ``current`` / ``now`` / ``latest`` / … ; induced
  candidates extend the inventory as the corpus grows.
* **Origin markers** — verb heads in the content of the subject's
  *first* memory (by ``observed_at``).  Seed: ``original`` / ``first``
  / ``initial`` / ``started`` / … ; induced candidates extend.

Retirement markers are NOT mined here — those come from
:func:`ncms.domain.tlg.markers.induce_edge_markers` (typed-edge-
destination mining).  Two induction paths, two vocabularies,
composed by the parser's ``augmented_markers`` method.

Discovery heuristic (from the research code):

1. A word qualifies in a bucket iff it appears in ≥ ``min_support``
   target memories AND at least ``purity_ratio`` more often there
   than in the opposing set.
2. Only verb heads that match the fixed grammatical shapes in
   :data:`ncms.domain.tlg.markers.VERB_PHRASE_SHAPES` are considered
   — the grammar structure stays hand-coded; only the vocabulary
   inside it grows.
3. Stopwords + digits are excluded.
4. Words already in the seed intent markers are dropped (avoids
   echoing known vocabulary back to the parser).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ncms.domain.tlg.markers import VERB_PHRASE_SHAPES


_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "for", "with", "of", "to",
    "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "this", "that", "these", "those", "i", "we", "you", "they", "he",
    "she", "it", "our", "their", "its", "his", "her", "my", "your",
    "not", "no", "now", "also", "then", "than", "so", "such", "more",
    "most", "other", "some", "any", "all", "each", "both", "will",
    "would", "could", "should", "may", "might", "must", "shall",
    "can", "first", "second", "third", "new", "old", "same",
})


_WORD_RE = re.compile(r"\b[a-z][a-z'-]{2,}\b")


class _MemoryLike(Protocol):
    """Minimal memory shape the induction needs.

    Caller hands over only what we actually read — ID, content, an
    ``observed_at`` ordering key, and a subject tag.  Typed-edge
    information enters via the ``terminal_ids`` / ``root_ids``
    arguments, not the memory itself.
    """

    id: str
    content: str
    observed_at: object  # datetime | None — we only need ordering


@dataclass(frozen=True)
class InducedContentMarkers:
    """Current + origin marker candidates produced by induction."""

    current_candidates: frozenset[str] = field(default_factory=frozenset)
    origin_candidates: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verb_heads(content: str) -> set[str]:
    """Extract verb-phrase heads in ``content`` (same shapes as
    :data:`VERB_PHRASE_SHAPES`, lowercased first word of each match).

    Drops stopwords and pure-digit tokens so we surface only
    discriminative heads.
    """
    heads: set[str] = set()
    for pat in VERB_PHRASE_SHAPES:
        for m in pat.finditer(content):
            phrase = m.group(1).lower()
            head = phrase.split()[0]
            if head and head not in _STOPWORDS and not head.isdigit():
                heads.add(head)
    return heads


def _word_counts(
    memories: Iterable[_MemoryLike],
    memory_ids: set[str],
) -> Counter[str]:
    """Aggregate verb-head counts across a given subset of memories.

    Counts each memory once (set-ify heads per memory) — avoids
    over-counting when the same head appears repeatedly in one long
    document.
    """
    counts: Counter[str] = Counter()
    wanted = set(memory_ids)
    for mem in memories:
        if mem.id not in wanted:
            continue
        counts.update(set(_verb_heads(mem.content)))
    return counts


def _filter_by_purity(
    target: Counter[str],
    opposing: Counter[str],
    *,
    min_support: int,
    purity_ratio: float,
) -> frozenset[str]:
    """Keep target verb heads that pass the support + purity gate."""
    keep: set[str] = set()
    for word, cnt in target.items():
        if cnt < min_support:
            continue
        opp = opposing.get(word, 0)
        if opp == 0 or cnt >= purity_ratio * opp:
            keep.add(word)
    return frozenset(keep)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def induce_content_markers(
    memories: Iterable[_MemoryLike],
    *,
    terminal_ids: set[str],
    root_ids: set[str],
    seed_current: frozenset[str] = frozenset(),
    seed_origin: frozenset[str] = frozenset(),
    min_support: int = 2,
    purity_ratio: float = 2.0,
) -> InducedContentMarkers:
    """Mine current + origin content-marker candidates.

    Args:
      memories: every subject-linked memory in the corpus.  Caller
        decides which records participate; induction doesn't filter.
      terminal_ids: memory IDs that are the terminal of a *current*
        zone (no outgoing supersedes / retires).  Passed in by the
        application layer — zone computation lives in
        :mod:`ncms.domain.tlg.zones` and the caller already knows
        these IDs.
      root_ids: memory IDs that are a subject's first memory
        (earliest by observed_at).  Not every zone root — only the
        subject-first memory.  See the research module docstring
        for why: later zone roots carry retirement verbs, not
        origin verbs.
      seed_current / seed_origin: the parser's existing seed
        markers — induction won't re-emit them (reduces noise).
      min_support: minimum target-set count for admission.
      purity_ratio: target count must exceed opposing count by at
        least this factor.

    Returns:
      :class:`InducedContentMarkers`.  Empty when no candidates
      survive the gates — safe on cold corpora.
    """
    memories_list = list(memories)
    all_ids = {m.id for m in memories_list}
    middle_ids = all_ids - terminal_ids - root_ids

    term_counts = _word_counts(memories_list, terminal_ids)
    root_counts = _word_counts(memories_list, root_ids)
    mid_counts = _word_counts(memories_list, middle_ids)

    current_cands = _filter_by_purity(
        term_counts, mid_counts + root_counts,
        min_support=min_support, purity_ratio=purity_ratio,
    )
    origin_cands = _filter_by_purity(
        root_counts, mid_counts + term_counts,
        min_support=min_support, purity_ratio=purity_ratio,
    )
    # Strip seed markers — we only surface what the corpus TAUGHT us.
    current_cands = frozenset(current_cands - seed_current)
    origin_cands = frozenset(origin_cands - seed_origin)
    return InducedContentMarkers(
        current_candidates=current_cands,
        origin_candidates=origin_cands,
    )


def summary(induced: InducedContentMarkers) -> str:
    lines = ["Content-induced markers", "=" * 40]
    lines.append(
        f"[current]  ({len(induced.current_candidates)}): "
        f"{sorted(induced.current_candidates)}"
    )
    lines.append(
        f"[origin]   ({len(induced.origin_candidates)}): "
        f"{sorted(induced.origin_candidates)}"
    )
    return "\n".join(lines)
