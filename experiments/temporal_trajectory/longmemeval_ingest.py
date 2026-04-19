"""LongMemEval mock-ingest pipeline.

Given a LongMemEval question + its haystack (conversation sessions
with timestamps), construct a typed-edge corpus that the grammar
can query.  Zero LLM calls; all deterministic heuristics.

Pipeline:

1. **Session → memory**: each haystack session becomes one memory.
   Content = concatenation of turn texts; ``observed_at`` parsed
   from session date; ``mid`` = session ID.
2. **Entity extraction**: regex-based NER for proper nouns,
   time expressions, numeric measurements, and topic nouns lifted
   from the question.
3. **Subject clustering**: memories with ≥ ``min_overlap`` shared
   entities get merged into the same subject.  Union-find across
   all sessions.  Isolated memories get their own subject.
4. **Mock reconciliation**: the existing ``mock_reconciliation``
   module produces typed edges from adjacent same-subject pairs.
5. **Grammar query**: route the LongMemEval question through the
   production grammar; grammar's grammar_answer (if any) is a
   session ID that can be compared to ``answer_session_ids``.

This mirrors what NCMS's `ReconciliationService` + entity graph
would do at ingest time, minus the LLM-assisted entity linking.
For taxonomy validation it's sufficient; for production readiness
we'd swap the regex NER for GLiNER and the subject clusterer for
NCMS's existing entity-graph clustering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from experiments.temporal_trajectory.corpus import Edge, Memory


# ── Date parsing ───────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*\(\w+\)\s*(\d{1,2}):(\d{2})")


def parse_lme_date(raw: str) -> datetime:
    m = _DATE_RE.search(raw)
    if m is None:
        raise ValueError(f"unparseable LongMemEval date: {raw!r}")
    from datetime import UTC
    year, month, day, hour, minute = (int(x) for x in m.groups())
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── Entity extraction ──────────────────────────────────────────────

_PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
_PROPER_ABBR_RE = re.compile(r"\b([A-Z]{2,6})\b")
_NUMERIC_MEASURE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?(?:\s*[:]\s*\d+)?)\s*"
    r"(minutes?|hours?|seconds?|miles?|km|kilometers?|lbs?|pounds?|"
    r"kilos?|kg|percent|%|k|pm|am|years?|months?|weeks?|days?)?\b"
)


def extract_entities(text: str, topic_nouns: set[str]) -> frozenset[str]:
    """Regex-based entity extraction.

    Combines proper-noun detection, numeric measurement patterns, and
    the question's topic-noun seed.  Returns a deduplicated frozenset.
    """
    ents: set[str] = set()
    # Proper nouns (sentence-start single-capital words filtered below).
    for m in _PROPER_NOUN_RE.finditer(text):
        candidate = m.group(1).strip()
        # Single capitalized word at sentence start is usually just
        # "I" / "The" / generic — keep multi-word proper nouns and
        # single caps that appear mid-sentence.
        if " " in candidate or _PROPER_ABBR_RE.fullmatch(candidate):
            ents.add(candidate)
        else:
            # Single capitalized — keep only if not at string start
            # and not a generic filler.
            idx = m.start()
            if idx > 0 and not text[:idx].rstrip().endswith("."):
                # Mid-sentence — likely a name/entity.
                if candidate not in {"I", "The", "A", "An", "My", "Our"}:
                    ents.add(candidate)
    for m in _PROPER_ABBR_RE.finditer(text):
        ents.add(m.group(1))
    # Numeric measurements — useful for "how much"/"how long" queries.
    for m in _NUMERIC_MEASURE_RE.finditer(text):
        value = m.group(0).strip()
        if m.group(2):  # has unit → keep
            ents.add(value)
    # Topic nouns from the question — guaranteed to appear in haystack
    # if the session is answer-relevant.
    text_lower = text.lower()
    for t in topic_nouns:
        if re.search(rf"\b{re.escape(t.lower())}\w*\b", text_lower):
            ents.add(t)
    return frozenset(ents)


_QUESTION_STOPWORDS = frozenset({
    "what", "when", "where", "why", "how", "who", "which",
    "was", "were", "is", "are", "do", "does", "did", "had", "have", "has",
    "my", "your", "his", "her", "our", "their", "its",
    "the", "a", "an", "in", "on", "at", "to", "of", "for", "with",
    "and", "or", "but", "that", "this", "it", "i",
})


def extract_question_topics(question: str) -> set[str]:
    """Content words from the question — used to seed entity match
    across the haystack.  Filters common function words; keeps
    domain-specific nouns and verbs."""
    words = re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", question)
    return {w for w in words if w.lower() not in _QUESTION_STOPWORDS}


# ── Memory construction ───────────────────────────────────────────

def build_memories(
    question: str,
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    haystack_dates: list[str],
    answer_session_ids: list[str] | None = None,
) -> list[Memory]:
    """Convert a LongMemEval question's haystack into Memory objects.

    Each session becomes one memory; content = joined turn texts
    (truncated per-turn to keep memory objects reasonable).
    """
    topics = extract_question_topics(question)
    mems: list[Memory] = []
    for i, (sess, sess_id, sess_date) in enumerate(
        zip(haystack_sessions, haystack_session_ids, haystack_dates, strict=True)
    ):
        # Concatenate turn contents, truncate each to 400 chars to
        # keep memory objects bounded.
        turn_texts = [
            f"{t.get('role', 'user')}: {t.get('content', '')[:400]}"
            for t in sess
        ]
        content = " | ".join(turn_texts)[:2000]
        observed_at = parse_lme_date(sess_date)
        entities = extract_entities(content, topics)
        mems.append(Memory(
            mid=sess_id,
            content=content,
            observed_at=observed_at,
            entities=entities,
            subject=None,  # filled in by clustering
        ))
    return mems


# ── Subject clustering ────────────────────────────────────────────

def cluster_subjects(
    mems: list[Memory], min_overlap: int = 2,
) -> dict[str, str]:
    """Union-find cluster memories into subjects by entity overlap.

    Args:
        mems: memories to cluster (subject=None initially).
        min_overlap: minimum shared-entity count to merge two memories
            into the same subject.

    Returns ``{mid → subject_name}``.  Subject names are synthesized
    as ``subject_<i>`` — integration would replace with canonical
    subject labels from NCMS's entity graph.
    """
    parent: dict[str, str] = {m.mid: m.mid for m in mems}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Pairwise overlap check.
    for i, a in enumerate(mems):
        for b in mems[i + 1:]:
            shared = {e.lower() for e in a.entities} & {e.lower() for e in b.entities}
            if len(shared) >= min_overlap:
                union(a.mid, b.mid)

    # Name each root cluster.
    roots = {}
    idx = 0
    for m in mems:
        r = find(m.mid)
        if r not in roots:
            roots[r] = f"subject_{idx}"
            idx += 1

    return {m.mid: roots[find(m.mid)] for m in mems}


def assign_subjects(mems: list[Memory], min_overlap: int = 2) -> list[Memory]:
    """Return a new list of Memory objects with subjects assigned."""
    assignment = cluster_subjects(mems, min_overlap=min_overlap)
    return [
        Memory(
            mid=m.mid,
            content=m.content,
            observed_at=m.observed_at,
            entities=m.entities,
            subject=assignment[m.mid],
        )
        for m in mems
    ]


# ── Ingest + reconcile orchestrator ────────────────────────────────

@dataclass
class IngestedCorpus:
    memories: list[Memory]
    edges: list[Edge]
    aliases_induced: int = 0
    layer2_markers: dict[str, frozenset[str]] | None = None

    def summary(self) -> str:
        subj_counts: dict[str, int] = {}
        for m in self.memories:
            subj_counts[m.subject or "(none)"] = (
                subj_counts.get(m.subject or "(none)", 0) + 1
            )
        total_ents = sum(len(m.entities) for m in self.memories)
        lines = [
            f"memories:   {len(self.memories)}",
            f"subjects:   {len(subj_counts)}",
            f"entities:   {total_ents} total, "
            f"{len({e for m in self.memories for e in m.entities})} unique",
            f"edges:      {len(self.edges)}",
            "",
            "Per-subject memory count:",
        ]
        for subj, cnt in sorted(subj_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {subj}: {cnt}")
        return "\n".join(lines)


def ingest_question(
    question: dict, min_overlap: int = 2,
) -> IngestedCorpus:
    """Full ingest pipeline for one LongMemEval question.

    Swaps the global ``corpus.ADR_CORPUS`` and ``corpus.EDGES`` to
    the ingested values, reloads dependent modules, and returns
    the constructed corpus.  Caller is responsible for restoring
    the original corpus after use.
    """
    mems = build_memories(
        question=question["question"],
        haystack_sessions=question["haystack_sessions"],
        haystack_session_ids=question["haystack_session_ids"],
        haystack_dates=question["haystack_dates"],
        answer_session_ids=question.get("answer_session_ids", []),
    )
    mems = assign_subjects(mems, min_overlap=min_overlap)

    # Swap corpus, reload dependent modules so induction re-runs.
    # IMPORTANT: every module that did ``from corpus import ADR_CORPUS``
    # or ``from corpus import EDGES`` captured a STALE reference at
    # import time.  Reassigning ``corpus.ADR_CORPUS = mems`` doesn't
    # propagate to those bindings — we MUST reload every dependent
    # module for the new corpus to take effect.
    from experiments.temporal_trajectory import corpus as _corpus
    _corpus.ADR_CORPUS = mems
    _corpus.EDGES = []

    import importlib
    from experiments.temporal_trajectory import (
        aliases,
        edge_markers,
        grammar,
        lg_retriever,
        marker_induction,
        mock_reconciliation,
        properties,
        query_parser,
        retirement_extractor,
        shape_cache,
        vocab_induction,
    )
    # First pass: reload induction-level modules (no edges yet).
    for mod in (
        vocab_induction, edge_markers, grammar, aliases,
        retirement_extractor, mock_reconciliation,
    ):
        importlib.reload(mod)
    edges = mock_reconciliation.reconcile_corpus()
    _corpus.EDGES = edges
    # Second pass: reload EVERY module that captured ADR_CORPUS or
    # EDGES via ``from`` import, so their bindings refresh.
    for mod in (
        vocab_induction, edge_markers, grammar, aliases,
        retirement_extractor, marker_induction, mock_reconciliation,
        query_parser, shape_cache, properties, lg_retriever,
    ):
        importlib.reload(mod)

    return IngestedCorpus(
        memories=mems,
        edges=edges,
        aliases_induced=len(aliases.ALIASES),
        layer2_markers=dict(edge_markers.MARKERS.markers),
    )
