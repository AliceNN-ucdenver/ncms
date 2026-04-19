"""L3 — structural query parser (routing-as-parse).

Port of ``experiments/temporal_trajectory/query_parser.py`` adapted
for NCMS: all globals lifted to a :class:`ParserContext` argument.
The context bundles the four corpus-derived inputs the parser
needs — induced L1 vocabulary, induced L2 markers, the auto-mined
issue inventory, and the per-subject domain nouns — so callers
build them once (from the MemoryStore via
``application/tlg/vocabulary_cache``) and reuse across queries.

Why structural parsing
----------------------

The production-rule matchers validate the FULL query shape: a
marker appearing anywhere is not enough — the marker must live
inside the specific syntactic structure the intent expects.
"Rachel moved to Seattle" contains the L2 ``moves`` marker but is
not a retirement query, so the retirement production rejects and
the parser falls through.

Each matcher is self-contained; neighbours don't interact via
shared precedence flags.  Order in :data:`_PRODUCTIONS` is
specificity — most-specific first.  Adding new intents = inserting
a matcher at the right specificity level.

Returned shape
--------------

:class:`QueryStructure` carries every slot downstream dispatch
needs (``intent`` / ``subject`` / ``target_entity`` /
``secondary_entity`` / ``range_start`` / ``range_end``).  A miss
returns ``intent="none"``.

See ``docs/temporal-linguistic-geometry.md`` §6 for the theory and
``docs/p1-plan.md`` for phase context.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from ncms.domain.temporal.parser import parse_temporal_reference
from ncms.domain.tlg.aliases import expand_aliases
from ncms.domain.tlg.markers import InducedEdgeMarkers
from ncms.domain.tlg.vocabulary import (
    InducedVocabulary,
    _stem,
    lookup_entity,
    lookup_subject,
)

# ---------------------------------------------------------------------------
# Seed intent-marker vocabulary — the ONLY hand-maintained lexicon.
# ~35 words across 5 intent families.
# ---------------------------------------------------------------------------

SEED_INTENT_MARKERS: dict[str, tuple[str, ...]] = {
    "current": (
        "current", "currently", "now", "today", "latest",
        "present", "presently", "as of",
    ),
    "still": (
        "still", "yet", "currently in", "currently on",
    ),
    "origin": (
        "original", "first", "initial", "earliest", "starting",
        "started", "start", "begin", "began", "kickoff", "onset",
    ),
    "cause_of": (
        "caused", "cause of", "reason for", "source of",
        "why", "what caused", "led to", "what led",
    ),
    "retirement": (
        "retired", "retire", "deprecated", "stopped using", "ended",
        "decommissioned",
    ),
}

# Action verbs that mark "still <verb> <target>" structure — the
# target after the verb is the entity of interest, not a subject noun.
_STILL_ACTION_VERBS: frozenset[str] = frozenset({
    "use", "using", "have", "having", "on", "in", "uses", "haven",
    "do", "doing", "does",
})

# Irreducible English issue-seed vocabulary for cause_of target
# preference ("something went wrong").  Domain-specific issue words
# reach the parser via the ``issue_entities`` context field.
ISSUE_SEED: frozenset[str] = frozenset({
    "blocker", "blockers", "blocked",
    "delay", "delays", "delayed",
    "issue", "issues",
    "problem", "problems",
    "incident", "incidents",
    "bug", "bugs",
    "error", "errors",
    "failure", "failures",
})


# ---------------------------------------------------------------------------
# Parser inputs / outputs
# ---------------------------------------------------------------------------


class _MemoryLike(Protocol):
    """Minimal shape the parser needs from a memory.

    The parser doesn't inspect content; it only needs entity lists
    to compute per-subject domain nouns.  Caller decides which
    records participate — typically ENTITY_STATE nodes' backing
    Memory objects.
    """

    subject: str | None
    entities: frozenset[str]


@dataclass(frozen=True)
class ParserContext:
    """Everything the parser reads besides the query itself.

    Built once per batch of queries (e.g. at search time) from the
    :class:`VocabularyCache`.  Immutable so callers can share it
    freely.
    """

    vocabulary: InducedVocabulary
    induced_markers: InducedEdgeMarkers
    aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    issue_entities: frozenset[str] = ISSUE_SEED
    domain_nouns: frozenset[str] = frozenset()

    def augmented_markers(self) -> dict[str, frozenset[str]]:
        """Seed + L2-induced markers for each intent family.

        Retirement verbs are a union of the seed and the
        ``supersedes`` + ``retires`` L2 buckets — so the parser
        auto-expands its vocabulary as new supersession edges land.
        """
        out: dict[str, set[str]] = {
            k: set(v) for k, v in SEED_INTENT_MARKERS.items()
        }
        induced = self.induced_markers.markers
        out["retirement"].update(induced.get("supersedes", frozenset()))
        out["retirement"].update(induced.get("retires", frozenset()))
        return {k: frozenset(v) for k, v in out.items()}


@dataclass(frozen=True)
class QueryStructure:
    """Structured query analysis.

    ``intent`` is always populated; ``"none"`` when no production
    matched.  Which slots matter depends on the intent — see the
    per-matcher docstrings.  Consumers should read ``intent`` first
    and branch on it.
    """

    intent: str
    subject: str | None
    target_entity: str | None
    detected_marker: str | None
    secondary_entity: str | None = None
    range_start: str | None = None
    range_end: str | None = None

    def has_grammar_answer(self) -> bool:
        return self.intent != "none"


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
    memories.  Used to prefer distinctive event names over the
    subject's topical nouns in :func:`_extract_event_name`.

    Ported from the research ``_domain_nouns`` helper, with the
    subject mapping lifted to the caller (``_MemoryLike.subject``).
    """
    by_subject_count: dict[str, int] = {}
    counts_by_subject: dict[str, Counter[str]] = {}
    for mem in memories:
        subj = mem.subject
        if subj is None:
            continue
        by_subject_count[subj] = by_subject_count.get(subj, 0) + 1
        c = counts_by_subject.setdefault(subj, Counter())
        for ent in mem.entities:
            c[ent.lower()] += 1
            for w in ent.split():
                if len(w) >= 3:
                    c[w.lower()] += 1

    out: set[str] = set()
    for subj, counter in counts_by_subject.items():
        size = by_subject_count[subj]
        if size < min_memories_per_subject:
            continue
        threshold = max(2, int(threshold_fraction * size))
        for token, n in counter.items():
            if n >= threshold:
                out.add(token)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Marker + slot helpers
# ---------------------------------------------------------------------------


def _find_marker(
    query: str, kind: str, augmented: dict[str, frozenset[str]],
) -> str | None:
    """Return the first marker of ``kind`` in ``query``, or None.

    Two-pass match preserved from the research code:

    1. Word-boundary prefix — marker ``retire`` matches ``retire``,
       ``retired``, ``retirement``.
    2. Stem equality (Snowball) — handles the reverse case
       (marker ``supersedes`` matches query word ``supersede``).
    """
    q = query.lower()
    markers = augmented.get(kind, frozenset())
    q_stems = [_stem(w) for w in re.findall(r"\w+", q)]
    q_stem_set = set(q_stems)
    for marker in sorted(markers, key=len, reverse=True):
        pattern = r"\b" + re.escape(marker) + r"\w*\b"
        if re.search(pattern, q):
            return marker
        if " " in marker:
            continue
        if _stem(marker) in q_stem_set:
            return marker
    return None


def _word_like(token: str, query_lower: str) -> bool:
    pattern = r"\b" + re.escape(token) + r"\w*\b"
    return re.search(pattern, query_lower) is not None


def _prefer_issue_entity(
    query: str, issue_entities: frozenset[str],
) -> str | None:
    q = query.lower()
    matches = [ent for ent in issue_entities if _word_like(ent, q)]
    if not matches:
        return None
    return max(matches, key=len)


def _extract_still_object(query: str) -> str | None:
    """Extract the X slot from a still-intent query."""
    q = query.lower()
    # "currently in/on X"
    m = re.search(
        r"\bcurrently\s+(?:in|on|at|under|with)\s+(\w[\w\s-]{0,40}?)"
        r"(?:\?|\.|,|$|\s+for\s+|\s+with\s+)",
        q,
    )
    if m:
        return m.group(1).strip()
    # "still <action_verb> X"
    action_pattern = (
        r"\bstill\s+(?:"
        + "|".join(re.escape(v) for v in _STILL_ACTION_VERBS)
        + r")\s+(\w[\w\s-]{0,40}?)(?:\?|\.|,|$|\s+for\s+|\s+with\s+)"
    )
    m = re.search(action_pattern, q)
    if m:
        return m.group(1).strip()
    # "still on/in/have X"
    m = re.search(
        r"\bstill\s+(?:on|in|at|have|has|had)\s+(\w[\w\s-]{0,40}?)"
        r"(?:\?|\.|,|$|\s+for\s+)",
        q,
    )
    if m:
        return m.group(1).strip()
    # "still <adjective>"
    m = re.search(r"\bstill\s+(\w+)\b", q)
    if m:
        word = m.group(1).strip()
        if word not in _STILL_ACTION_VERBS:
            return word
    return None


def _extract_event_name(raw: str, ctx: ParserContext) -> str:
    """Canonicalize a named-event phrase via vocab lookup.

    Pipeline matches the research version:

    1. Strip leading determiner.
    2. Strip trailing prepositional phrase.
    3. Try full-phrase entity lookup.  If it resolves to a domain
       noun, fall through.
    4. Token-level lookup — return the unique non-domain match.
    5. Fall back to the stripped lowercase phrase.
    """
    s = raw.strip().strip(".,?!").rstrip()
    s = re.sub(
        r"^(?:the|a|an|our|their|its|his|her)\s+",
        "", s, flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\s+(?:in|on|for|at|during|with|by|from)\s+.*$",
        "", s, flags=re.IGNORECASE,
    )
    phrase_ent = lookup_entity(s, ctx.vocabulary)
    if phrase_ent is not None and phrase_ent.lower() == s.lower().strip():
        return phrase_ent

    single_matches: list[str] = []
    for word in re.findall(r"[\w-]+", s):
        if len(word) < 2:
            continue
        ent = lookup_entity(word, ctx.vocabulary)
        if ent is not None and ent.lower() not in ctx.domain_nouns:
            single_matches.append(ent)
    if len(single_matches) == 1:
        return single_matches[0]
    return s.lower()


def _canonicalize_target(
    raw: str | None, ctx: ParserContext,
) -> str | None:
    """Map a raw extracted target to a canonical entity or lowercase
    fallback.  Preserves research behavior: entity lookup first, then
    raw-lower fallback."""
    if not raw:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    ent = lookup_entity(stripped, ctx.vocabulary)
    if ent is not None:
        return ent
    return stripped.lower()


# ---------------------------------------------------------------------------
# Range detection — wraps ``parse_temporal_reference``
# ---------------------------------------------------------------------------


def _detect_range(query: str) -> tuple[str, str] | None:
    """Extract a calendar range when the query carries one.

    Filters out hits that aren't true range intents (recency,
    ordinal, single-day spans).  ``range`` fires only on meaningful
    intervals (explicit year, quarter, month, ≥ 7 day window).
    """
    ref = parse_temporal_reference(query)
    if ref is None:
        return None
    if getattr(ref, "recency_bias", False):
        return None
    if getattr(ref, "ordinal", None):
        return None
    if ref.range_start is None or ref.range_end is None:
        return None
    span = ref.range_end - ref.range_start
    if span < timedelta(days=7):
        return None
    return ref.range_start.isoformat(), ref.range_end.isoformat()


# ---------------------------------------------------------------------------
# Production matchers
# ---------------------------------------------------------------------------


def _match_interval(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """``between X and Y``."""
    m = re.search(
        r"\bbetween\s+(?P<x>[\w\s-]{2,40}?)\s+and\s+"
        r"(?P<y>[\w\s-]{2,50}?)"
        r"(?:\?|\.|,|\s+on\s|\s+in\s|\s+for\s|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    y = _extract_event_name(m.group("y"), ctx)
    if not x or not y:
        return None
    return QueryStructure(
        intent="interval", subject=subject,
        target_entity=x, secondary_entity=y,
        detected_marker="between",
    )


def _match_before_named(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """Two-event ordering.

    Two variants:

    * **Anchored yes/no** — ``did X (come)? before Y``.
    * **WH-alternation** — ``which X (did I do)? first, A or B``.
    """
    m = re.match(
        r"\s*(?:did|was|were|has|have|do|does)\s+"
        r"(?P<x>[\w\s-]{2,40}?)\s+"
        r"(?:(?:come|happen|occur|ship|land)\s+)?before\s+"
        r"(?P<y>[\w\s-]{2,40}?)(?:\?|\.|,|$)",
        query, flags=re.IGNORECASE,
    )
    marker = "before_named"
    if m is None:
        m = re.search(
            r"\bwhich\s+\w+\s+[^?]*?\bfirst\b[^?]*?(?:,\s*)?"
            r"(?:the\s+)?(?P<x>[\w][\w\s-]{0,40}?)"
            r"\s+or\s+(?:the\s+)?(?P<y>[\w][\w\s-]{0,40}?)"
            r"(?:\?|\.|$)",
            query, flags=re.IGNORECASE,
        )
        if m is not None:
            marker = "which_first"
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    y = _extract_event_name(m.group("y"), ctx)
    if not x or not y:
        return None
    return QueryStructure(
        intent="before_named", subject=subject,
        target_entity=x, secondary_entity=y,
        detected_marker=marker,
    )


def _match_transitive_cause(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """``what (eventually|ultimately|finally) led to X``."""
    m = re.search(
        r"\bwhat\s+(?:eventually|ultimately|finally)\s+(?:led|resulted)"
        r"\s+(?:to|in)\s+(?P<x>[\w\s-]{2,50}?)(?:\?|\.|,|$)",
        query, flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    if not x:
        return None
    return QueryStructure(
        intent="transitive_cause", subject=subject,
        target_entity=x, detected_marker="eventually_led_to",
    )


def _match_concurrent(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """``what was happening during X`` — cross-subject."""
    m = re.search(
        r"\bwhat\s+(?:else\s+)?(?:was\s+)?(?:happening|going\s+on|"
        r"occurring|underway|ongoing|in\s+progress)\s+"
        r"(?:during|while|alongside)\s+"
        r"(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
        query, flags=re.IGNORECASE,
    )
    if m is None:
        m = re.search(
            r"\bwhat\s+else\s+happened\s+(?:during|while|alongside)\s+"
            r"(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
            query, flags=re.IGNORECASE,
        )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    if not x:
        return None
    return QueryStructure(
        intent="concurrent", subject=subject,
        target_entity=x, detected_marker="during",
    )


def _match_sequence(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """WH-question about chain successor — ``what came after X``."""
    if not re.match(r"\s*(?:what|where|who|which|how)\b", query, re.IGNORECASE):
        return None
    m = re.search(
        r"\bafter\s+(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
        query, flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    if not x:
        return None
    return QueryStructure(
        intent="sequence", subject=subject,
        target_entity=x, detected_marker="after",
    )


def _match_predecessor(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """WH-question chain predecessor — ``what came before X``."""
    if not re.match(r"\s*(?:what|who|which|how)\b", query, re.IGNORECASE):
        return None
    m = re.search(
        r"\bbefore\s+(?P<x>[\w\s-]{2,50}?)(?:\?|\.|,|$)",
        query, flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"), ctx)
    if not x:
        return None
    return QueryStructure(
        intent="predecessor", subject=subject,
        target_entity=x, detected_marker="before_single",
    )


def _match_range(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    r = _detect_range(query)
    if r is None:
        return None
    range_start, range_end = r
    return QueryStructure(
        intent="range", subject=subject,
        target_entity=None, detected_marker="range",
        range_start=range_start, range_end=range_end,
    )


def _match_retirement(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """Retirement verb in retirement *structure*.

    Bare marker presence isn't enough: ``Rachel moved to Seattle``
    contains ``moved`` but isn't a retirement query.  Structural
    patterns are imperative/infinitive + object, passive voice,
    ``led to <verb>``, and directional ``move(s) from <X>``.
    """
    augmented = ctx.augmented_markers()
    marker = _find_marker(query, "retirement", augmented)
    if marker is None:
        return None
    q_low = query.lower()

    verb_re = re.escape(_stem(marker)) + r"\w*"
    structure_patterns = [
        rf"\b(?:is|was|are|were|has\s+been|have\s+been)\s+"
        rf"(?:fully\s+|now\s+|officially\s+)?{verb_re}\b",
        rf"\b(?:to\s+|should\s+|must\s+|will\s+|led\s+to\s+|decided\s+to\s+|"
        rf"decision\s+to\s+|plan\s+to\s+)?{verb_re}\s+"
        rf"(?:the\s+|our\s+|its\s+|their\s+)?\w",
        rf"\b{verb_re}\s+from\b",
    ]
    if marker.startswith(("mov", "migrat")):
        if not re.search(rf"\b{verb_re}\s+from\b", q_low):
            return None
    elif not any(re.search(p, q_low) for p in structure_patterns):
        return None

    target = lookup_entity(query, ctx.vocabulary)
    return QueryStructure(
        intent="retirement", subject=subject,
        target_entity=target, detected_marker=marker,
    )


def _match_still(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """``still <verb> X`` / ``currently in X``.  Requires a resolvable object."""
    augmented = ctx.augmented_markers()
    marker = _find_marker(query, "still", augmented)
    if marker is None:
        return None
    still_obj = _extract_still_object(query)
    if not still_obj:
        return None
    target = _canonicalize_target(still_obj, ctx)
    if not target:
        return None
    return QueryStructure(
        intent="still", subject=subject,
        target_entity=target, detected_marker=marker,
    )


def _match_cause_of(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    """``what caused / reason for X`` (without retirement verb).

    Rejects when the target collapses to a subject-domain noun —
    prevents confidently-wrong answers on unresolvable concepts.
    """
    augmented = ctx.augmented_markers()
    marker = _find_marker(query, "cause_of", augmented)
    if marker is None:
        return None
    target = (
        _prefer_issue_entity(query, ctx.issue_entities)
        or lookup_entity(query, ctx.vocabulary)
    )
    if target is not None and target.lower() in ctx.domain_nouns:
        return None
    return QueryStructure(
        intent="cause_of", subject=subject,
        target_entity=target, detected_marker=marker,
    )


def _match_origin(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    augmented = ctx.augmented_markers()
    marker = _find_marker(query, "origin", augmented)
    if marker is None:
        return None
    target = lookup_entity(query, ctx.vocabulary)
    return QueryStructure(
        intent="origin", subject=subject,
        target_entity=target, detected_marker=marker,
    )


def _match_current(
    query: str, subject: str | None, ctx: ParserContext,
) -> QueryStructure | None:
    augmented = ctx.augmented_markers()
    marker = _find_marker(query, "current", augmented)
    if marker is None:
        return None
    return QueryStructure(
        intent="current", subject=subject,
        target_entity=None, detected_marker=marker,
    )


#: Ordered production list — most-specific first.  Adding new intents
#: means inserting a matcher at the right specificity level; each
#: matcher is self-contained so neighbours are unaffected.
_PRODUCTIONS: tuple[
    Callable[[str, str | None, ParserContext], QueryStructure | None], ...,
] = (
    _match_interval,
    _match_before_named,
    _match_transitive_cause,
    _match_concurrent,
    _match_sequence,
    _match_predecessor,
    _match_range,
    _match_retirement,
    _match_still,
    _match_cause_of,
    _match_origin,
    _match_current,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_query(
    query: str, ctx: ParserContext,
) -> QueryStructure:
    """Parse ``query`` into a :class:`QueryStructure`.

    Runs each production in specificity order; the first matcher to
    accept the query wins.  Returns ``intent="none"`` when no
    production fires — callers should fall back to BM25.

    Subject resolution uses the context's L1 vocabulary (null when
    the corpus is too cold to infer one).  Aliases aren't applied
    here — they enter the pipeline at dispatch time where the
    grammar checks ``retires_entities``.
    """
    subject = lookup_subject(query, ctx.vocabulary)
    for matcher in _PRODUCTIONS:
        result = matcher(query, subject, ctx)
        if result is not None:
            return result
    return QueryStructure(
        intent="none",
        subject=subject,
        target_entity=None,
        detected_marker=None,
    )


# ---------------------------------------------------------------------------
# Introspection — same spirit as the research summary()
# ---------------------------------------------------------------------------


def summary(ctx: ParserContext) -> str:
    lines = ["L3 query-parser vocabulary", "=" * 60]
    augmented = ctx.augmented_markers()
    for kind in ("current", "still", "origin", "cause_of", "retirement"):
        seed = set(SEED_INTENT_MARKERS.get(kind, ()))
        extras = sorted(augmented.get(kind, frozenset()) - seed)
        lines.append(
            f"[{kind}] seed={sorted(seed)}  "
            f"L2-augmented=+{len(extras)}"
        )
        if extras:
            lines.append(f"    induced: {extras}")
    lines.append(f"issue entities: {len(ctx.issue_entities)}")
    lines.append(f"domain nouns:   {len(ctx.domain_nouns)}")
    return "\n".join(lines)


# ``expand_aliases`` re-exported for callers that want to apply
# alias expansion on a parsed ``target_entity`` before hitting
# dispatch.  Pure convenience passthrough.
__all__ = [
    "ISSUE_SEED",
    "ParserContext",
    "QueryStructure",
    "SEED_INTENT_MARKERS",
    "analyze_query",
    "compute_domain_nouns",
    "expand_aliases",
    "summary",
]
