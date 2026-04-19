"""Marker induction — grammar structure grows with ingest.

Layer 2 already mines transition markers from edge-destination
content.  This module extends induction to more query-intent
families by scanning memory content patterns:

* **Current-state markers** — words appearing in content of memories
  that terminate a zone (``z.terminal_mid``) correlate with
  current-state.  Already-known markers (current/now/latest/…) are
  the seed; new corpus-derived markers extend it.
* **Origin markers** — words appearing in content of zone-root
  memories (``z.start_mid``) correlate with origination.  Seed
  (original/first/initial/started/…) is extended from corpus.
* **Retirement markers** — already handled by Layer 2.

Result: a corpus-derived extension of the query-intent marker set
that grows with ingestion.  Applied at import time like Layer 2.

Importantly, this does NOT change production regex shapes.  English
question structure ("what X after Y") is an English-grammar
invariant, not a domain-specific grammar feature.  What grows is
the VOCABULARY inside those structures.

### Discovery heuristic

For each intent family, candidate words are:

1. Common verbs / nouns in the target memory content.
2. **Rank-filtered**: a word qualifies as a new marker iff it
   appears in ≥ N target memories (configurable, default 2) AND
   does NOT appear in the "opposing" memory set at higher frequency.
3. **Stopword-filtered**: English function words + numbers excluded.
4. **Seed-intersection excluded**: words already in the hand seed
   are not re-emitted.

Tuned to prefer high-precision over recall — false-positive markers
would bleed into the production classifier and cause misrouting.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from experiments.temporal_trajectory.corpus import ADR_CORPUS
from experiments.temporal_trajectory.grammar import compute_zones


_STOPWORDS = frozenset({
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


@dataclass(frozen=True)
class InducedMarkers:
    current_candidates: frozenset[str]
    origin_candidates: frozenset[str]

    def summary(self) -> str:
        lines = ["Content-derived marker candidates", "=" * 50]
        lines.append(
            f"[current]  ({len(self.current_candidates)}): "
            f"{sorted(self.current_candidates)}"
        )
        lines.append(
            f"[origin]   ({len(self.origin_candidates)}): "
            f"{sorted(self.origin_candidates)}"
        )
        return "\n".join(lines)


def _tokens(text: str) -> list[str]:
    """Lowercase verb-like content words (minus stopwords / numbers).

    Restricted to verb heads identified by Layer 2's verb-phrase
    shape regexes.  Without this restriction, marker induction
    would surface topic nouns ("knee", "project") that happen to
    appear in root memories but aren't grammatical markers.
    """
    from experiments.temporal_trajectory.edge_markers import (
        _VERB_PHRASE_SHAPES,
    )
    heads: set[str] = set()
    for pat in _VERB_PHRASE_SHAPES:
        for m in pat.finditer(text):
            phrase = m.group(1).lower()
            head = phrase.split()[0]
            if head not in _STOPWORDS and not head.isdigit():
                heads.add(head)
    return list(heads)


def _word_counts_for_memories(mids: set[str]) -> Counter:
    by_id = {m.mid: m for m in ADR_CORPUS}
    c: Counter = Counter()
    for mid in mids:
        mem = by_id.get(mid)
        if mem is None:
            continue
        # Unique words per memory (avoid over-counting in long content).
        c.update(set(_tokens(mem.content)))
    return c


def induce_markers(
    min_support: int = 2, purity_ratio: float = 2.0,
) -> InducedMarkers:
    """Mine current/origin marker candidates from memory content.

    Args:
        min_support: minimum number of target memories a word must
            appear in to qualify.
        purity_ratio: a word in the target set must appear at least
            this many times more often there than in the opposing set.
    """
    # Collect memory-id sets for each intent family's target group.
    # - "Origin" memories = FIRST memory per subject (earliest by
    #   observed_at).  Not "every zone root" — zone roots of later
    #   zones contain retirement verbs (the supersession narrative),
    #   not origin verbs.
    # - "Current" memories = TERMINAL of the current zone only.
    all_subjects = {
        m.subject for m in ADR_CORPUS if m.subject is not None
    }
    terminal_mids: set[str] = set()
    root_mids: set[str] = set()
    for subj in all_subjects:
        subj_mems = [m for m in ADR_CORPUS if m.subject == subj]
        if not subj_mems:
            continue
        subj_mems.sort(key=lambda m: m.observed_at)
        root_mids.add(subj_mems[0].mid)  # subject-first
        # Current terminal: zone with no outgoing supersedes/retires.
        zones = compute_zones(subj)
        for z in zones:
            if z.ended_transition is None:
                terminal_mids.add(z.terminal_mid)

    # "Opposing" sets: middle memories (neither root nor current terminal)
    all_mids = {m.mid for m in ADR_CORPUS if m.subject is not None}
    middle_mids = all_mids - terminal_mids - root_mids

    term_counts = _word_counts_for_memories(terminal_mids)
    root_counts = _word_counts_for_memories(root_mids)
    mid_counts = _word_counts_for_memories(middle_mids)

    def _filter(
        target: Counter, opposing: Counter, min_sup: int, ratio: float,
    ) -> frozenset[str]:
        keep: set[str] = set()
        for word, cnt in target.items():
            if cnt < min_sup:
                continue
            opp_cnt = opposing.get(word, 0)
            if opp_cnt == 0 or cnt >= ratio * opp_cnt:
                keep.add(word)
        return frozenset(keep)

    current_cands = _filter(term_counts, mid_counts + root_counts, min_support, purity_ratio)
    origin_cands = _filter(root_counts, mid_counts + term_counts, min_support, purity_ratio)

    # Remove words already in seed markers (import late to avoid cycle).
    from experiments.temporal_trajectory.query_parser import _SEED_MARKERS
    seed_current = set(_SEED_MARKERS.get("current", []))
    seed_origin = set(_SEED_MARKERS.get("origin", []))
    current_cands = frozenset(current_cands - seed_current)
    origin_cands = frozenset(origin_cands - seed_origin)

    return InducedMarkers(
        current_candidates=current_cands,
        origin_candidates=origin_cands,
    )


INDUCED = induce_markers()


if __name__ == "__main__":
    print(INDUCED.summary())
