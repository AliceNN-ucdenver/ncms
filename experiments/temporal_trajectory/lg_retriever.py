"""LG-proper retrieval: intent classification + bidirectional search.

Given a query and a corpus with typed edges, this retriever:

1. **Classifies the query's temporal intent** into an LG primitive
   (``current``, ``origin``, ``retirement``, ``none``).  Each
   primitive has a grammar-defined answer:

   * ``current(subject)``    → terminal memory of the subject's
                                currently-open zone
   * ``origin(subject)``     → start memory of the subject's
                                earliest zone
   * ``retirement(entity, subject)``
                              → memory whose edge ended the entity's
                                last valid zone
   * ``none``                 → no grammar-derivable answer; fall
                                through to BM25 order

2. **Bidirectional search** collapses for zone-terminal queries:
   because the grammar is well-formed, there's usually exactly one
   grammar-admissible answer.  The "intersection" of
   backward-from-intent and forward-from-context is the single
   terminal.  When the grammar is ambiguous, we fall through.

3. **Structural exclusion of non-subject memories.**  Any memory
   whose ``subject`` doesn't match (or doesn't appear in any
   admissible path from the query's subject) is ranked AFTER all
   grammatically-admitted memories — even if BM25 loves them.  This
   is how ADR-033 (subject='auth_ops') gets pushed to the bottom
   for authentication queries.

Explainability: each call returns not just a ranking but a
``LGTrace`` with the query intent, the admitted trajectory, and the
grammar-proof for the top answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from experiments.temporal_trajectory.corpus import ADR_CORPUS, Memory
from experiments.temporal_trajectory.grammar import (
    compute_zones,
    current_zone,
    origin_memory,
    retirement_memory,
)


# ── Intent classifier ───────────────────────────────────────────────

# Intent-marker regexes are retired — Layer 3 (``query_parser``) now
# owns classification.  See ``query_parser._SEED_MARKERS`` for the
# minimal seed vocabulary and ``edge_markers`` for corpus-augmented
# transition markers.


@dataclass(frozen=True)
class LGIntent:
    kind: str                    # see query_parser.QueryStructure for kinds
    subject: str | None          # zone/subject to query
    entity: str | None = None    # primary entity slot
    secondary: str | None = None  # secondary entity (interval / before_named)


@dataclass
class LGTrace:
    """Explainable result: shows what the grammar produced.

    ``confidence`` levels (production-grade integration hint):

    * ``high``    — grammar path is deterministic and slots resolved
                    exactly (zone terminal, direct edge lookup, alias
                    match in retires_entities).  Safe to trust as
                    rank-1 without BM25 fallback.
    * ``medium``  — grammar path is well-defined but uses a minor
                    approximation (content-marker fallback,
                    entity-in-current-zone heuristic, concurrent
                    window).  Good rank-1 candidate but BM25 ordering
                    should be preserved below.
    * ``low``     — grammar path used a loose fallback (generic
                    entity-mention).  Answer is a hint; BM25 rank-1
                    typically still right.
    * ``abstain`` — intent matched but slots couldn't be resolved
                    (unknown entity, no matching edge, empty
                    interval).  ``grammar_answer`` is ``None`` and the
                    retriever falls back to pure BM25.
    * ``none``    — no intent matched; grammar didn't apply.
    """

    query: str
    intent: LGIntent
    admitted_zones: list[str] = field(default_factory=list)
    grammar_answer: str | None = None   # mid deterministically identified
    proof: str = ""                      # syntactic justification
    confidence: str = "none"             # high | medium | low | abstain | none

    def has_confident_answer(self) -> bool:
        """Integration predicate — true iff grammar answer is safe to
        prepend at rank-1 without falling back to BM25.

        Integration pattern::

            trace = retrieve_lg(query, bm25_full)
            if trace.has_confident_answer():
                return trace.full_ranking   # grammar wins
            else:
                return bm25_full           # let BM25 + SPLADE handle it

        Low/abstain answers still appear in ``full_ranking`` but NOT
        at the top — BM25 ordering is preserved.
        """
        return (
            self.grammar_answer is not None
            and self.confidence in ("high", "medium")
        )


# Subject + entity lookups are corpus-derived (see
# ``vocab_induction.py``).  Snowball stemming handles morphology so
# "authenticate"/"authentication" and "blocked"/"blocker" collapse
# automatically — no hand-maintained stem dict.
from experiments.temporal_trajectory.vocab_induction import (
    lookup_entity as _induced_entity,
    lookup_subject as _induced_subject,
)


def _infer_subject(query: str) -> str | None:
    return _induced_subject(query)


def _infer_entity(query: str) -> str | None:
    return _induced_entity(query)


def classify_lg_intent(query: str) -> LGIntent:
    """Determine the LG primitive for a query.

    Uses the structural query parser (``query_parser.analyze_query``)
    which combines Layer 1 (corpus-induced subject/entity vocab),
    Layer 2 (edge-content-induced transition markers), and a minimal
    seed of query-grammar atoms.  The seed is the only hand-maintained
    lexicon and it's tiny (~35 words across 5 intent families).

    See ``docs/p1-experiment-diary.md`` Phase-E entries for the
    design narrative.
    """
    from experiments.temporal_trajectory.query_parser import analyze_query

    qs = analyze_query(query)
    return LGIntent(
        kind=qs.intent,
        subject=qs.subject,
        entity=qs.target_entity,
        secondary=qs.secondary_entity,
    )


# ── Bidirectional search ────────────────────────────────────────────

def _bm25_tiebreaker(
    mids: list[str],
    bm25_full: list[tuple[str, float]],
) -> list[str]:
    """Order ``mids`` by their BM25 rank; unranked at the end."""
    rank = {mid: i for i, (mid, _) in enumerate(bm25_full)}
    return sorted(mids, key=lambda m: rank.get(m, 10**9))


def _stem_subsequence(
    needle: tuple[str, ...], haystack: tuple[str, ...],
) -> bool:
    """True iff ``needle`` appears as a contiguous subsequence of
    ``haystack`` (stem-equality).  Zero-length needle returns False."""
    if not needle or len(needle) > len(haystack):
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i:i + len(needle)] == needle:
            return True
    return False


def _find_memory(subject: str | None, name: str) -> Memory | None:
    """Find the earliest memory naming ``name`` in its entities/content.

    Used by the new memory-return intents (sequence / predecessor /
    interval / before_named / transitive_cause / concurrent) to
    resolve the named event(s) in the query to specific memories.

    Order of checks:
      1. Exact entity match (case-insensitive equality).
      2. Substring in any entity (case-insensitive).
      3. Word-boundary match in content.
      4. Stem-sequence match in content (Snowball-stemmed) — handles
         morphology uniformly, same mechanism as vocab_induction.

    When ``subject`` is ``None``, searches all memories in ``ADR_CORPUS``
    (used by ``concurrent``, which may resolve a named event from any
    subject).
    """
    from experiments.temporal_trajectory.vocab_induction import _stem

    name_low = name.strip().lower()
    if not name_low:
        return None
    name_stems = [_stem(w) for w in re.findall(r"\w+", name_low) if w]

    pool = [
        m for m in ADR_CORPUS
        if subject is None or m.subject == subject
    ]
    pool.sort(key=lambda m: m.observed_at)

    for m in pool:
        ents_low = [e.lower() for e in m.entities]
        # (1) exact
        if any(name_low == e for e in ents_low):
            return m
        # (2) substring in entity
        if any(name_low in e for e in ents_low):
            return m
        # (3) word-boundary in content
        content_low = m.content.lower()
        if re.search(rf"\b{re.escape(name_low)}\b", content_low):
            return m
        # (4) stem-sequence in content
        if name_stems:
            content_stems = [_stem(w) for w in re.findall(r"\w+", content_low)]
            k = len(name_stems)
            for i in range(len(content_stems) - k + 1):
                if content_stems[i:i + k] == name_stems:
                    return m
    # (5) Bag-of-words fallback — for multi-word queries ("Stripe
    # blocker") where neither literal substring nor stem-sequence
    # matches, accept a memory whose entity set covers ALL of the
    # query's content words (stem-matched).  This resolves
    # user-constructed phrases like "Stripe blocker" → PROJ-03
    # (entities contain both "Stripe" and "blocker") without
    # matching unrelated phrases like "sprint planning" (no single
    # memory has both "sprint" and "planning").
    if len(name_stems) >= 2:
        query_stems = set(name_stems)
        for m in pool:
            mem_stems: set[str] = set()
            for e in m.entities:
                for w in re.findall(r"\w+", e.lower()):
                    mem_stems.add(_stem(w))
            if query_stems.issubset(mem_stems):
                return m
    return None


def retrieve_lg(
    query: str,
    bm25_full: list[tuple[str, float]],
) -> tuple[list[tuple[str, float]], LGTrace]:
    """LG-grammar retrieval over the ADR corpus.

    Returns (ranked_list, trace) where ranked_list has the grammar
    answer at rank 1 (when derivable), zone-neighbouring memories
    next, then all other subject-admissible memories, then non-subject
    memories in BM25 order.

    ``bm25_full`` is the complete BM25 ranking — used as a tiebreaker
    when the grammar admits multiple memories, and as the fallback
    order for non-subject memories.
    """
    intent = classify_lg_intent(query)
    trace = LGTrace(query=query, intent=intent)
    by_id = {m.mid: m for m in ADR_CORPUS}

    # Non-subject memories — ranked last in BM25 order.
    subject = intent.subject
    if subject is None:
        # No subject inferred → fall through to BM25.
        trace.proof = "no subject inferred; using BM25 order"
        trace.confidence = "abstain"
        return bm25_full, trace

    subject_mems = {m.mid for m in ADR_CORPUS if m.subject == subject}
    non_subject_ordered = [
        (mid, score) for mid, score in bm25_full
        if mid not in subject_mems
    ]

    # Dispatch by intent.
    grammar_answer: str | None = None
    zone_context: list[str] = []

    if intent.kind == "current":
        zone = current_zone(subject)
        if zone is not None:
            grammar_answer = zone.terminal_mid
            zone_context = list(zone.memory_ids)
            trace.proof = (
                f"current(subject={subject}): terminal of zone "
                f"{zone.zone_id} "
                f"(chain: {' → '.join(zone.memory_ids)})"
            )
            trace.admitted_zones = [f"zone{zone.zone_id}"]
            trace.confidence = "high"

    elif intent.kind == "origin":
        # Two variants:
        #   1. Entity-scoped origin: "initial DIAGNOSIS for the knee" →
        #      earliest memory in subject whose entities include
        #      "diagnosis".  Different from the zone root, which is the
        #      earliest memory in the subject regardless of entity.
        #   2. Subject-scoped origin: "original auth" → zone root.
        resolved_origin: str | None = None
        if intent.entity is not None:
            # Scan subject memories chronologically; return the first
            # whose entities set (case-insensitive) contains the entity.
            subj_mems = [m for m in ADR_CORPUS if m.subject == subject]
            subj_mems.sort(key=lambda m: m.observed_at)
            ent_low = intent.entity.lower()
            for m in subj_mems:
                ents_low = {e.lower() for e in m.entities}
                if ent_low in ents_low or ent_low in m.content.lower():
                    resolved_origin = m.mid
                    trace.proof = (
                        f"origin(subject={subject}, entity="
                        f"{intent.entity}): first memory mentioning "
                        f"'{intent.entity}' is {m.mid}"
                    )
                    break
        if resolved_origin is None:
            resolved_origin = origin_memory(subject)
            if resolved_origin is not None:
                trace.proof = (
                    f"origin(subject={subject}): root of earliest zone "
                    f"= {resolved_origin}"
                )
        if resolved_origin is not None:
            grammar_answer = resolved_origin
            for z in compute_zones(subject):
                if z.start_mid == resolved_origin:
                    zone_context = list(z.memory_ids)
                    break
            trace.confidence = "high"

    elif intent.kind == "still":
        # "Still using X?" — if X's zone has been superseded, the
        # answer is the superseding memory (which tells you it's
        # NOT still in use).  If X is still in the current zone,
        # the answer is the terminal of the current zone.
        if intent.entity is None:
            trace.proof = "still(): no entity inferred; BM25 fallback"
        else:
            retire_mid = retirement_memory(subject, intent.entity)
            current = current_zone(subject)
            if retire_mid is not None:
                grammar_answer = retire_mid
                trace.proof = (
                    f"still({intent.entity}): retirement memory "
                    f"{retire_mid} ended {intent.entity} within "
                    f"subject={subject}"
                )
                trace.confidence = "high"
            elif current is not None:
                # Check whether the entity is in the current zone.
                current_entities: set[str] = set()
                for mid in current.memory_ids:
                    current_entities |= by_id[mid].entities
                if intent.entity.lower() in {
                    e.lower() for e in current_entities
                }:
                    grammar_answer = current.terminal_mid
                    zone_context = list(current.memory_ids)
                    trace.proof = (
                        f"still({intent.entity}): entity is in the "
                        f"current zone, terminal = "
                        f"{current.terminal_mid}"
                    )
                    trace.confidence = "medium"

    elif intent.kind == "range":
        # Range-filter: return subject memories whose observed_at
        # falls in the classifier-supplied interval, in chronological
        # order.  No numeric scoring; grammar-only.
        from experiments.temporal_trajectory.query_parser import (
            analyze_query as _analyze,
        )
        qs = _analyze(query)
        if qs.range_start and qs.range_end:
            r_start = qs.range_start
            r_end = qs.range_end
            subj_mems = [
                m for m in ADR_CORPUS
                if m.subject == subject
                and r_start <= m.observed_at.isoformat() < r_end
            ]
            subj_mems.sort(key=lambda m: m.observed_at)
            if subj_mems:
                grammar_answer = subj_mems[0].mid
                zone_context = [m.mid for m in subj_mems[1:]]
                trace.proof = (
                    f"range(subject={subject}, "
                    f"[{r_start[:10]}, {r_end[:10]})): "
                    f"{len(subj_mems)} memories; "
                    f"earliest = {grammar_answer}"
                )
                trace.confidence = "high"

    elif intent.kind == "sequence":
        # "What came right after X?"  — direct chain successor of X.
        from experiments.temporal_trajectory.corpus import EDGES as _EDGES
        if intent.entity is not None:
            x_mem = _find_memory(subject, intent.entity)
            if x_mem is not None:
                for edge in _EDGES:
                    if edge.src == x_mem.mid:
                        grammar_answer = edge.dst
                        trace.proof = (
                            f"sequence(subject={subject}, after={intent.entity}"
                            f"@{x_mem.mid}): successor = {edge.dst} "
                            f"({edge.transition})"
                        )
                        trace.confidence = "high"
                        break

    elif intent.kind == "predecessor":
        # "What came before X?" — direct chain predecessor of X.
        from experiments.temporal_trajectory.corpus import EDGES as _EDGES
        if intent.entity is not None:
            x_mem = _find_memory(subject, intent.entity)
            if x_mem is not None:
                for edge in _EDGES:
                    if edge.dst == x_mem.mid:
                        grammar_answer = edge.src
                        trace.proof = (
                            f"predecessor(subject={subject}, "
                            f"before={intent.entity}@{x_mem.mid}): "
                            f"predecessor = {edge.src} ({edge.transition})"
                        )
                        trace.confidence = "high"
                        break

    elif intent.kind == "interval":
        # "What happened between X and Y?" — memories in same subject
        # with observed_at strictly between X's and Y's observed_at.
        # Returns the earliest as rank-1; remaining as zone_context.
        if intent.entity is not None and intent.secondary is not None:
            x_mem = _find_memory(subject, intent.entity)
            y_mem = _find_memory(subject, intent.secondary)
            if x_mem is not None and y_mem is not None:
                lo_t = min(x_mem.observed_at, y_mem.observed_at)
                hi_t = max(x_mem.observed_at, y_mem.observed_at)
                between = [
                    m for m in ADR_CORPUS
                    if m.subject == subject
                    and lo_t < m.observed_at < hi_t
                ]
                between.sort(key=lambda m: m.observed_at)
                if between:
                    grammar_answer = between[0].mid
                    zone_context = [m.mid for m in between[1:]]
                    trace.proof = (
                        f"interval(subject={subject}, "
                        f"{x_mem.mid}↔{y_mem.mid}): "
                        f"{len(between)} memories strictly between; "
                        f"earliest = {grammar_answer}"
                    )
                    trace.confidence = "high"

    elif intent.kind == "before_named":
        # "Did X happen before Y?" — compares observed_at ordering of
        # two named events.  Answer memory = X's memory (user's
        # subject of question).  Proof states whether X ≺ Y or not.
        if intent.entity is not None and intent.secondary is not None:
            x_mem = _find_memory(subject, intent.entity)
            y_mem = _find_memory(subject, intent.secondary)
            if x_mem is not None and y_mem is not None:
                grammar_answer = x_mem.mid
                if x_mem.observed_at < y_mem.observed_at:
                    verdict = f"yes — {x_mem.mid} before {y_mem.mid}"
                elif x_mem.observed_at > y_mem.observed_at:
                    verdict = f"no — {x_mem.mid} after {y_mem.mid}"
                else:
                    verdict = f"same time — {x_mem.mid} ≡ {y_mem.mid}"
                trace.proof = (
                    f"before_named({intent.entity}→{x_mem.mid}, "
                    f"{intent.secondary}→{y_mem.mid}, subject={subject}): "
                    f"{verdict}"
                )
                trace.confidence = "high"

    elif intent.kind == "transitive_cause":
        # "What eventually led to X?" — full predecessor walk through
        # admissible edges (supersedes + refines) in the subject.
        # Answer = root ancestor (memory with no admissible incoming
        # edge in the walked chain).
        from collections import defaultdict as _dd
        from experiments.temporal_trajectory.corpus import EDGES as _EDGES
        if intent.entity is not None:
            x_mem = _find_memory(subject, intent.entity)
            if x_mem is not None:
                by_dst: dict[str, list] = _dd(list)
                for edge in _EDGES:
                    if edge.transition not in ("supersedes", "refines"):
                        continue
                    src_s = by_id[edge.src].subject if edge.src in by_id else None
                    dst_s = by_id[edge.dst].subject if edge.dst in by_id else None
                    if src_s != subject or dst_s != subject:
                        continue
                    by_dst[edge.dst].append(edge)
                chain = [x_mem.mid]
                cur = x_mem.mid
                visited = {cur}
                while cur in by_dst:
                    pred = by_dst[cur][0].src
                    if pred in visited:
                        break
                    visited.add(pred)
                    chain.append(pred)
                    cur = pred
                grammar_answer = chain[-1]   # root ancestor
                zone_context = list(reversed(chain[:-1]))
                trace.proof = (
                    f"transitive_cause(subject={subject}, "
                    f"to={intent.entity}@{x_mem.mid}): "
                    f"walked {len(chain)} predecessors; root = "
                    f"{grammar_answer}"
                )
                trace.confidence = "high"

    elif intent.kind == "concurrent":
        # "What else was happening during X?" — cross-subject query.
        # Find X in ANY subject; return the closest memory from a
        # DIFFERENT subject whose observed_at is within a 30-day
        # window of X's observed_at.  Rank-1 = closest by days.
        from datetime import timedelta
        if intent.entity is not None:
            x_mem = _find_memory(None, intent.entity)
            if x_mem is not None:
                lo_t = x_mem.observed_at - timedelta(days=30)
                hi_t = x_mem.observed_at + timedelta(days=30)
                concurrent_mems = [
                    m for m in ADR_CORPUS
                    if m.mid != x_mem.mid
                    and m.subject is not None
                    and m.subject != x_mem.subject
                    and lo_t <= m.observed_at <= hi_t
                ]
                concurrent_mems.sort(
                    key=lambda m: (
                        abs((m.observed_at - x_mem.observed_at).days),
                        m.observed_at,
                    ),
                )
                if concurrent_mems:
                    grammar_answer = concurrent_mems[0].mid
                    zone_context = [m.mid for m in concurrent_mems[1:5]]
                    trace.proof = (
                        f"concurrent(during={intent.entity}"
                        f"@{x_mem.mid} [{x_mem.subject}]): "
                        f"{len(concurrent_mems)} cross-subject memories "
                        f"within ±30d; closest = {grammar_answer}"
                    )
                    trace.confidence = "medium"

    elif intent.kind == "retirement":
        if intent.entity is not None:
            retire_mid = retirement_memory(subject, intent.entity)
            if retire_mid is not None:
                grammar_answer = retire_mid
                trace.proof = (
                    f"retirement({intent.entity}): grammar "
                    f"identifies retiring memory {retire_mid}"
                )
                trace.confidence = "high"

    elif intent.kind == "cause_of":
        # "What led to/caused X" = the memory that introduced X into
        # the subject's trajectory.  Grammar steps (most-specific first):
        #
        #   (a) If X appears in a supersedes edge's retires_entities,
        #       return the SOURCE of that edge.  Highest-confidence:
        #       the edge explicitly names X as what got resolved.
        #   (c) Earliest memory in subject whose CONTENT matches a
        #       grammatical issue-marker (blocker/delay/issue/problem/
        #       incident).  Fires when (a) misses — typical when the
        #       user's surface form ("delay") doesn't match the
        #       reconciled retires_entities ("blocker").  Content-
        #       marker search is robust to alias gaps.
        #   (b) Earliest memory whose entities/content mention X.
        #       LAST resort — loose match; prone to returning the
        #       subject-opening memory when X is a domain noun.
        from experiments.temporal_trajectory.corpus import EDGES as _EDGES
        target = intent.entity

        # (a) Look up X in edge retires_entities — alias-expanded
        # exact match.  Intentionally NOT stem-subsequence matching:
        # "surgery" stem-subsequence-matches "arthroscopic surgery"
        # but they're semantically different ("surgery" = the event
        # of having surgery; "arthroscopic surgery" = an ongoing
        # treatment state that recovery replaces).  When the user
        # uses a looser surface form, fall through to (c)/(b).
        if target is not None:
            from experiments.temporal_trajectory.aliases import expand_aliases
            target_surfaces = {s.lower() for s in expand_aliases(target)}
            for edge in _EDGES:
                if edge.transition not in ("supersedes", "retires"):
                    continue
                src_subj = by_id[edge.src].subject if edge.src in by_id else None
                if src_subj != subject:
                    continue
                retired_low = {e.lower() for e in edge.retires_entities}
                overlap = target_surfaces & retired_low
                if overlap:
                    grammar_answer = edge.src
                    trace.proof = (
                        f"cause_of(subject={subject}, entity={target}): "
                        f"edge {edge.src}→{edge.dst} retires "
                        f"{sorted(overlap)!r}, so cause = {edge.src}"
                    )
                    trace.confidence = "high"
                    break

        # (c) Content-marker fallback — earliest issue memory in the
        # subject.  Fires BEFORE the loose entity-mention lookup
        # because when the user asks about an issue ("delay"), the
        # first memory NAMING an issue is usually the right answer —
        # regardless of whether the specific surface form matches.
        #
        # Gated on ``_ISSUE_SEED`` (intrinsic English issue-words):
        # only fires when the user's concept IS a language-level
        # issue concept (blocker/delay/issue/problem/bug/error/
        # failure/incident).  Corpus-derived retires entities
        # ("arthroscopic surgery", "access tokens") are NOT in the
        # seed — they're concrete things, not issue concepts, and
        # their cause isn't the "first issue memory" of the subject.
        #
        # Structural guard: issue nouns (blocker/issue/incident/bug
        # etc.) are polysemous ("jogging incident" != "security
        # incident").  Require the noun to co-occur with a report/
        # resolution verb so we match problem-reporting memories only.
        from experiments.temporal_trajectory.query_parser import _ISSUE_SEED
        target_is_issue_word = (
            target is None
            or target.lower() in _ISSUE_SEED
        )
        if grammar_answer is None and target_is_issue_word:
            import re as _re
            # Issue noun + problem verb in either order, within ~20 chars.
            issue_noun = (
                r"(?:blocker|blocked|blockers|bug|bugs|failure|failures"
                r"|issue|issues|problem|problems|incident|incidents"
                r"|error|errors|delay|delays|delayed)"
            )
            problem_verb = (
                r"(?:identified|reported|detected|raised|found|discovered"
                r"|resolved|hit|encountered)"
            )
            issue_re = _re.compile(
                rf"\b{issue_noun}\b[\s\w\-—,]{{0,30}}?\b{problem_verb}\b"
                rf"|\b{problem_verb}\b[\s\w\-—,]{{0,30}}?\b{issue_noun}\b",
                _re.IGNORECASE,
            )
            issue_mids: list[str] = []
            for m in ADR_CORPUS:
                if m.subject != subject:
                    continue
                if issue_re.search(m.content):
                    issue_mids.append(m.mid)
            if issue_mids:
                issue_mids.sort(key=lambda mid: by_id[mid].observed_at)
                grammar_answer = issue_mids[0]
                trace.proof = (
                    f"cause_of(subject={subject}): content-marker "
                    f"fallback, earliest issue memory = {grammar_answer}"
                )
                trace.confidence = "medium"

        # (b) Generic entity-mention fallback — LAST resort.
        # Confidence scaled by match quality:
        #   * exact in entity set         → medium
        #   * substring in a multi-word
        #     entity ("surgery" in
        #     "arthroscopic surgery")     → medium
        #   * word-boundary in content    → low
        if grammar_answer is None and target is not None:
            target_low = target.lower()
            subj_mems = [m for m in ADR_CORPUS if m.subject == subject]
            subj_mems.sort(key=lambda m: m.observed_at)
            for m in subj_mems:
                ents_low = [e.lower() for e in m.entities]
                match_kind: str | None = None
                if target_low in ents_low:
                    match_kind = "entity_exact"
                elif any(target_low in e for e in ents_low):
                    match_kind = "entity_substring"
                elif re.search(rf"\b{re.escape(target_low)}\b", m.content.lower()):
                    match_kind = "content"
                if match_kind is not None:
                    grammar_answer = m.mid
                    trace.proof = (
                        f"cause_of(subject={subject}, entity={target}): "
                        f"first memory mentioning '{target}' via "
                        f"{match_kind} = {m.mid}"
                    )
                    trace.confidence = (
                        "medium" if match_kind.startswith("entity")
                        else "low"
                    )
                    break

    # Subject-only fallback — fires ONLY for single-memory subjects.
    # Rationale: when the corpus has a subject with exactly one
    # memory (e.g., ``identity_project`` = PROJ-99 alone), ANY query
    # whose subject resolves to it has an unambiguous answer (that
    # one memory).  This is a DATA property, not a query heuristic.
    #
    # Explicitly NOT firing for multi-memory subjects with
    # single-memory current zones (e.g., mature `authentication`) —
    # returning the current state for every vague query would be
    # confident-wrong when the user's intent wasn't "current".  For
    # those cases, intent=none correctly signals "grammar can't
    # parse this, BM25 handles it."
    if grammar_answer is None and intent.kind == "none" and subject is not None:
        subj_mems = [m for m in ADR_CORPUS if m.subject == subject]
        if len(subj_mems) == 1:
            grammar_answer = subj_mems[0].mid
            trace.proof = (
                f"subject-only fallback: {subject} has exactly one "
                f"memory ({grammar_answer}) — unambiguous"
            )
            trace.confidence = "medium"

    # Abstention: intent matched but slots didn't resolve → signal
    # low confidence so callers can fall back to pure BM25.
    if grammar_answer is None and intent.kind != "none":
        trace.confidence = "abstain"
        if not trace.proof:
            trace.proof = (
                f"{intent.kind}: intent matched but slots did not "
                f"resolve — abstaining (fall back to BM25)"
            )

    trace.grammar_answer = grammar_answer

    # Assemble final ranking.  Prepend grammar_answer as rank-1 ONLY
    # when confidence is high or medium.  Low-confidence / abstain
    # paths preserve BM25 ordering (grammar answer still appears in
    # full_ranking but without a rank-1 override).
    ordered_mids: list[str] = []
    placed: set[str] = set()
    if trace.has_confident_answer():
        ordered_mids.append(grammar_answer)
        placed.add(grammar_answer)
    for mid in zone_context:
        if mid not in placed:
            ordered_mids.append(mid)
            placed.add(mid)
    # Other subject-admissible memories — in BM25 order.
    for mid, _ in bm25_full:
        if mid in subject_mems and mid not in placed:
            ordered_mids.append(mid)
            placed.add(mid)
    # Non-subject memories at the tail.
    for mid, _ in non_subject_ordered:
        ordered_mids.append(mid)

    # Rebuild (mid, score) — use position-based synthetic scores so
    # downstream code (run.py's top-5 extractor) sees a sensible
    # monotone ranking.
    n = len(ordered_mids)
    ranked = [
        (mid, float(n - i))
        for i, mid in enumerate(ordered_mids)
    ]
    return ranked, trace
