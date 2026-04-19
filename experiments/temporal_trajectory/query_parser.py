"""Layer 3 — structural query parser.

Given a query, extract a structured representation:

    QueryStructure(
        intent:         'current' | 'origin' | 'still' | 'retirement'
                        | 'cause_of' | 'none',
        subject:        str | None,      # from Layer 1 induction
        target_entity:  str | None,      # what the query is ASKING ABOUT
        state_entity:   str | None,      # "still [X]" adjective form
    )

Key distinction vs. the earlier regex-alternation approach:

* **Structural** — we identify grammatical roles (marker, verb, object,
  state) rather than string-matching entire query patterns.
* **Minimal seed vocab** — a small hand-written seed for the 5 intent
  families.  No whack-a-mole regex alternations.
* **Data-augmented** — seed vocab is extended by Layer 2's induced
  transition markers (from edge-destination content).  When the
  corpus gets new edges with new verbs, the intent classifier
  auto-expands its marker vocabulary.
* **Uses Layer 1** for subject and entity detection.

Not using spaCy.  A GLiNER-with-extra-labels call (production NCMS
integration) would do this with better semantic precision; for the
standalone experiment, a rule-based analyzer proves the architectural
thesis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from experiments.temporal_trajectory.corpus import EDGES
from experiments.temporal_trajectory.edge_markers import MARKERS as LAYER2
from experiments.temporal_trajectory.vocab_induction import (
    lookup_entity,
    lookup_subject,
)


# Issue-entity vocabulary — used by cause_of target extraction to
# prefer issue concepts over generic subject nouns.  Two sources:
#
# * **``_ISSUE_SEED``** — language-level issue words (blocker,
#   delay, issue, problem, bug, error, failure).  Irreducible English
#   vocabulary for "something went wrong"; ~10 words.  Kept small
#   and intrinsic — adding every domain-specific issue word
#   ("outage", "downtime", "breach", …) would cause the grammar to
#   confidently answer about unrelated concepts.  Domain-specific
#   issue words should reach the grammar via ``retires_entities``
#   annotations on typed edges (auto-derived below).
# * **Corpus-derived retires** — anything in any edge's
#   ``retires_entities`` is, by definition, something that got
#   retired/resolved — a candidate issue.  Self-improving: new
#   retires-annotations flow into the inventory at import time.
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


def _issue_entities() -> frozenset[str]:
    acc: set[str] = set(_ISSUE_SEED)
    for edge in EDGES:
        for ent in edge.retires_entities:
            acc.add(ent.lower())
    return frozenset(acc)


_ISSUE_ENTITIES: frozenset[str] = _issue_entities()


# ── Seed intent-marker vocabulary ──────────────────────────────────
# The ONLY hand-maintained lexicon in the system.  These are the
# query-grammar atoms — irreducible linguistic markers that signal
# an intent family.  Everything else is data-derived.
_SEED_MARKERS: dict[str, list[str]] = {
    "current": [
        "current", "currently", "now", "today", "latest",
        "present", "presently", "as of",
    ],
    "still": [
        # Include "currently in/on" — semantically "is X still Y".
        "still", "yet", "currently in", "currently on",
    ],
    "origin": [
        "original", "first", "initial", "earliest", "starting",
        "started", "start", "begin", "began", "kickoff", "onset",
    ],
    "cause_of": [
        "caused", "cause of", "reason for", "source of",
        "why", "what caused", "led to", "what led",
    ],
    "retirement": [
        # Just the retirement verbs.  "led to" is cause_of — it only
        # becomes retirement when the retirement verb is also present
        # (e.g., "led to the decision to retire JWT" matches BOTH
        # markers; retirement has higher precedence so retirement wins).
        "retired", "retire", "deprecated", "stopped using", "ended",
        "decommissioned",
    ],
    # "led to" alone is cause_of.  This is separated from cause_of's
    # seed because its precedence matters.

}

# Augment "retirement" + "supersedes" with Layer 2's induced
# transition markers.  When the corpus gets new supersedes edges,
# their destination-content verbs flow into this classifier.
_AUGMENTED_MARKERS: dict[str, set[str]] = {
    kind: set(words) for kind, words in _SEED_MARKERS.items()
}
_AUGMENTED_MARKERS["retirement"].update(
    LAYER2.markers.get("supersedes", set())
)
_AUGMENTED_MARKERS["retirement"].update(
    LAYER2.markers.get("retires", set())
)

# Action verbs that mark "still [verb] [target]" structure — the
# target after the verb is the entity of interest, not a subject noun.
_STILL_ACTION_VERBS = {
    "use", "using", "have", "having", "on", "in", "uses", "haven",
    "having",
}


@dataclass(frozen=True)
class QueryStructure:
    intent: str                    # current/origin/still/retirement/cause_of/range/sequence/predecessor/interval/before_named/transitive_cause/concurrent/none
    subject: str | None
    target_entity: str | None      # primary slot (what the query asks about)
    detected_marker: str | None    # the production that matched
    # Secondary slot — used by intents with TWO named events:
    # ``interval`` (X ∧ Y endpoints) and ``before_named`` (X vs Y order).
    secondary_entity: str | None = None
    # Range-intent extras — ISO-8601 strings so consumers don't need
    # dateparser themselves.  None when intent != "range".
    range_start: str | None = None
    range_end: str | None = None

    def has_grammar_answer(self) -> bool:
        return self.intent != "none"


def _find_marker(query: str, kind: str) -> str | None:
    """Return the first marker of ``kind`` found in ``query``, or None.

    Two-pass match:

      1. **Word-boundary prefix** — e.g. marker ``retire`` matches
         ``retire``, ``retired``, ``retirement``.  Handles morphology
         where the marker form is a prefix of the query word.
      2. **Stem equality** — Snowball-stemmed single-word markers
         match single-word query tokens with the same stem.  Handles
         the reverse case (marker ``supersedes`` matches query word
         ``supersede``).  Multi-word markers skip this pass (their
         internal structure matters).
    """
    from experiments.temporal_trajectory.vocab_induction import _stem
    q = query.lower()
    markers = _AUGMENTED_MARKERS.get(kind, set())
    # Prep: stem query word tokens once.
    q_stems = [_stem(w) for w in re.findall(r"\w+", q)]
    q_stem_set = set(q_stems)
    # Sort longest-first so multi-word markers match before prefixes.
    for marker in sorted(markers, key=len, reverse=True):
        pattern = r"\b" + re.escape(marker) + r"\w*\b"
        if re.search(pattern, q):
            return marker
        if " " in marker:
            continue  # multi-word markers only match via exact.
        marker_stem = _stem(marker)
        if marker_stem in q_stem_set:
            return marker
    return None


def _extract_still_object(query: str) -> str | None:
    """For still-intent queries, extract the target (X).

    Handles three marker shapes:
      - "still <verb> <entity>"         → X after verb
      - "still <adjective>"              → adjective (state)
      - "currently in/on <noun phrase>"  → X after "in"/"on"

    Returns the raw extracted phrase (not yet canonicalized).
    """
    q = query.lower()
    # Pattern 0: "currently in/on X"
    currently_pattern = (
        r"\bcurrently\s+(?:in|on|at|under|with)\s+(\w[\w\s-]{0,40}?)"
        r"(?:\?|\.|,|$|\s+for\s+|\s+with\s+)"
    )
    m = re.search(currently_pattern, q)
    if m:
        return m.group(1).strip()
    # Pattern 1: still + action_verb + object
    action_pattern = (
        r"\bstill\s+(?:"
        + "|".join(re.escape(v) for v in _STILL_ACTION_VERBS)
        + r")\s+(\w[\w\s-]{0,40}?)(?:\?|\.|,|$|\s+for\s+|\s+with\s+)"
    )
    m = re.search(action_pattern, q)
    if m:
        return m.group(1).strip()
    # Pattern 2: "still on/in/have X"
    prep_pattern = (
        r"\bstill\s+(?:on|in|at|have|has|had)\s+(\w[\w\s-]{0,40}?)"
        r"(?:\?|\.|,|$|\s+for\s+)"
    )
    m = re.search(prep_pattern, q)
    if m:
        return m.group(1).strip()
    # Pattern 3: still + adjective (no verb between)
    adj_pattern = r"\bstill\s+(\w+)\b"
    m = re.search(adj_pattern, q)
    if m:
        word = m.group(1).strip()
        if word not in _STILL_ACTION_VERBS:
            return word
    return None


def analyze_query(query: str) -> QueryStructure:
    """Parse the query into a structured representation.

    **Routing-as-parse.**  Each intent is represented by a matcher
    function that validates the FULL query shape — not just whether a
    marker appears, but whether the marker is embedded in the
    specific syntactic structure that intent expects.  The parser
    tries productions in specificity order; the first to accept the
    query wins.

    This replaces the earlier linear "scan for marker → assume
    intent" approach.  Benefits:

    * **No hidden precedence bias.**  Each matcher validates its own
      slots, so ambiguity between (say) ``still`` and ``retirement``
      is resolved by which production's structure the query fits —
      not by which marker happens to be tried first.
    * **Failing matchers don't propagate.**  A "still"-intent query
      missing its object returns ``None`` from ``_match_still`` and
      the parser continues to the next production, rather than
      silently returning a ``still`` with ``target_entity=None``.
    * **New intents land as ``(matcher, insertion_point)``** — no
      precedence surgery on adjacent productions.

    Order — most-specific (two-slot / compound) productions first:

      1. ``interval``          — "between X and Y"
      2. ``before_named``      — "did X (come/happen)? before Y"
      3. ``transitive_cause``  — "what eventually led to X"
      4. ``concurrent``        — "what was happening during X"
      5. ``sequence``          — "what came after X"
      6. ``predecessor``       — "what came before X"
      7. ``range``             — calendar ranges
      8. ``retirement``        — retirement verbs (induced by Layer 2)
      9. ``still``             — "still X" / "currently in X"
     10. ``cause_of``          — "what caused X"
     11. ``origin``            — "original X" / "first X"
     12. ``current``           — "current X" / "latest X"
    """
    subject = lookup_subject(query)

    # Self-improving routing — query-shape cache lookup.  If we've
    # successfully parsed a query with an equivalent skeleton before,
    # route by the cached intent without re-running productions.
    # Productions are the falling-back authority for new shapes.
    from experiments.temporal_trajectory.shape_cache import GLOBAL_CACHE
    cached_hit = GLOBAL_CACHE.lookup(query)

    for matcher in _PRODUCTIONS:
        result = matcher(query, subject)
        if result is not None:
            # Cache this shape for future matching queries.
            GLOBAL_CACHE.learn(query, result.intent)
            return result

    # Production miss — if the cache saw this shape before (learned
    # from a different surface form that DID match a production),
    # use the cached intent and re-extract slots.  Extremely useful
    # for varied phrasings once the cache warms up.
    if cached_hit is not None:
        cached_intent, cached_slots = cached_hit
        return QueryStructure(
            intent=cached_intent,
            subject=subject,
            target_entity=cached_slots.get("<X>"),
            secondary_entity=cached_slots.get("<Y>"),
            detected_marker="cache_hit",
        )

    return QueryStructure(
        intent="none",
        subject=subject,
        target_entity=None,
        detected_marker=None,
    )


# ── Production matchers ────────────────────────────────────────────
#
# Each matcher returns a populated ``QueryStructure`` when the query
# fits the production's structure, or ``None`` to let the parser
# continue to the next production.  Matchers MUST validate their own
# slots — returning a structure with ``target_entity=None`` is a
# silent bug, not a valid outcome.


def _domain_nouns() -> frozenset[str]:
    """Entities that appear in ≥60 % of ANY subject's memories.

    Used by :func:`_extract_event_name` to prefer distinctive event
    names over subject-domain nouns (e.g., "OAuth" over
    "authentication", "MRI" over "knee").  Same principle as the
    structural retires extractor — auto-derived from corpus, no
    hand list.
    """
    from collections import Counter as _Counter

    from experiments.temporal_trajectory.corpus import ADR_CORPUS as _CORPUS

    by_subject: dict[str, int] = {}
    counts_by_subject: dict[str, _Counter] = {}
    for m in _CORPUS:
        if m.subject is None:
            continue
        by_subject[m.subject] = by_subject.get(m.subject, 0) + 1
        c = counts_by_subject.setdefault(m.subject, _Counter())
        for e in m.entities:
            c[e.lower()] += 1
            # Also individual words in multi-word entities.
            for w in e.split():
                if len(w) >= 3:
                    c[w.lower()] += 1
    out: set[str] = set()
    for subj, c in counts_by_subject.items():
        size = by_subject[subj]
        if size < 3:
            continue
        threshold = max(2, int(0.6 * size))
        for tok, n in c.items():
            if n >= threshold:
                out.add(tok)
    return frozenset(out)


_DOMAIN_NOUNS = _domain_nouns()


def _extract_event_name(raw: str) -> str:
    """Trim a named-event phrase to a canonical form.

    Pipeline:

      1. Strip leading determiner ("the OAuth" → "OAuth").
      2. Strip trailing prepositional phrase ("OAuth in authentication"
         → "OAuth") — the prep phrase is usually a subject scope hint,
         not part of the event name.
      3. Try full-phrase entity lookup.  If it resolves to a domain
         noun (e.g., "authentication" from "OAuth in authentication"
         picks up the longer vocab entry), fall through to token-level
         search and prefer a non-domain match.
      4. Token-by-token entity lookup.  Return first non-domain hit.
      5. Fall back to full-phrase entity (even if domain) or raw lower.
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
    # (1) Full-phrase exact vocab match — highest confidence path.
    phrase_ent = lookup_entity(s)
    if phrase_ent is not None and phrase_ent.lower() == s.lower().strip():
        return phrase_ent
    # (2) Token-level: find non-domain entity matches for each word.
    #     If exactly ONE non-domain word resolves, return it.
    #     If MULTIPLE resolve, the user's phrase is constructed from
    #     separate entities — return the raw phrase so _find_memory
    #     can use its multi-word matchers (entity-set overlap) to
    #     decide whether the phrase as a whole resolves to a memory.
    single_matches: list[str] = []
    for word in re.findall(r"[\w-]+", s):
        if len(word) < 2:
            continue
        ent = lookup_entity(word)
        if ent is not None and ent.lower() not in _DOMAIN_NOUNS:
            single_matches.append(ent)
    if len(single_matches) == 1:
        return single_matches[0]
    # (3) Raw phrase — _find_memory applies bag-of-words matching.
    return s.lower()


def _match_interval(query: str, subject: str | None) -> QueryStructure | None:
    """'What happened between X and Y' / 'between X and Y on Z'."""
    m = re.search(
        r"\bbetween\s+(?P<x>[\w\s-]{2,40}?)\s+and\s+(?P<y>[\w\s-]{2,50}?)"
        r"(?:\?|\.|,|\s+on\s|\s+in\s|\s+for\s|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    y = _extract_event_name(m.group("y"))
    if not x or not y:
        return None
    return QueryStructure(
        intent="interval",
        subject=subject,
        target_entity=x,
        secondary_entity=y,
        detected_marker="between",
    )


def _match_before_named(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """Two-event ordering — two variants:

    * **Yes/no anchored** — ``Did X (come)? before Y?``.  Anchored
      to query start so "What was the step before surgery?" (a
      predecessor question) falls through to ``_match_predecessor``.
    * **WH-alternation** — ``Which X (did I do)? first, A or B?``.
      Explicit enumeration of two events asking which came first.
      Semantically equivalent to ``Did A (come)? before B?``.
    """
    # Variant 1: anchored yes/no.
    m = re.match(
        r"\s*(?:did|was|were|has|have|do|does)\s+"
        r"(?P<x>[\w\s-]{2,40}?)\s+"
        r"(?:(?:come|happen|occur|ship|land)\s+)?before\s+"
        r"(?P<y>[\w\s-]{2,40}?)(?:\?|\.|,|$)",
        query,
        flags=re.IGNORECASE,
    )
    marker = "before_named"

    # Variant 2: "which <noun> ... first, A or B"
    if m is None:
        m = re.search(
            r"\bwhich\s+\w+\s+[^?]*?\bfirst\b[^?]*?(?:,\s*)?"
            r"(?:the\s+)?(?P<x>[\w][\w\s-]{0,40}?)"
            r"\s+or\s+(?:the\s+)?(?P<y>[\w][\w\s-]{0,40}?)"
            r"(?:\?|\.|$)",
            query,
            flags=re.IGNORECASE,
        )
        if m is not None:
            marker = "which_first"

    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    y = _extract_event_name(m.group("y"))
    if not x or not y:
        return None
    return QueryStructure(
        intent="before_named",
        subject=subject,
        target_entity=x,
        secondary_entity=y,
        detected_marker=marker,
    )


def _match_transitive_cause(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'What (eventually|ultimately|finally) led to X' — full ancestor walk."""
    m = re.search(
        r"\bwhat\s+(?:eventually|ultimately|finally)\s+(?:led|resulted)"
        r"\s+(?:to|in)\s+(?P<x>[\w\s-]{2,50}?)(?:\?|\.|,|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    if not x:
        return None
    return QueryStructure(
        intent="transitive_cause",
        subject=subject,
        target_entity=x,
        detected_marker="eventually_led_to",
    )


def _match_concurrent(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'What was (else) happening (during|while|alongside) X' — cross-subject."""
    # Form 1: "What (else)? (was |happened )?(happening|going on|...) during X"
    m = re.search(
        r"\bwhat\s+(?:else\s+)?(?:was\s+)?(?:happening|going\s+on|"
        r"occurring|underway|ongoing|in\s+progress)\s+"
        r"(?:during|while|alongside)\s+"
        r"(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        # Form 2: "what (else) happened during X"
        m = re.search(
            r"\bwhat\s+else\s+happened\s+(?:during|while|alongside)\s+"
            r"(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
            query,
            flags=re.IGNORECASE,
        )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    if not x:
        return None
    return QueryStructure(
        intent="concurrent",
        subject=subject,
        target_entity=x,
        detected_marker="during",
    )


def _match_sequence(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """WH-question about chain successor — "what/where/who (...) after X".

    Accepts any WH-start question containing "after X" as long as X
    resolves to a named event.  Covers classic phrasings ("What came
    after X?") and verb-phrase variants ("What did I do after X?",
    "Where did Rachel move to after her relocation?").
    """
    if not re.match(r"\s*(?:what|where|who|which|how)\b", query, re.IGNORECASE):
        return None
    m = re.search(
        r"\bafter\s+(?P<x>[\w\s-]{2,60}?)(?:\?|\.|,|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    if not x:
        return None
    return QueryStructure(
        intent="sequence",
        subject=subject,
        target_entity=x,
        detected_marker="after",
    )


def _match_predecessor(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'What came/was (the step)? before X' — direct chain predecessor.

    Distinct from ``before_named`` (which compares two named events
    via yes/no question) — ``predecessor`` asks only about what
    precedes a single event.

    Matched structure: a WH-question containing "before X" where the
    "X" slot resolves.  Accepts both classic phrasings ("What came
    before X?") and verb-phrase phrasings ("What did I do before
    X?", "What happened before X?") via the WH + "before X" pattern.

    ``_match_before_named`` runs first in the production list and
    matches anchored yes/no queries ("Did X before Y?"), so reaching
    this matcher implies the query is a genuine WH-predecessor.
    """
    # Require a WH-word to start the query, then "before X" anywhere.
    if not re.match(r"\s*(?:what|who|which|how)\b", query, re.IGNORECASE):
        return None
    m = re.search(
        r"\bbefore\s+(?P<x>[\w\s-]{2,50}?)(?:\?|\.|,|$)",
        query,
        flags=re.IGNORECASE,
    )
    if m is None:
        return None
    x = _extract_event_name(m.group("x"))
    if not x:
        return None
    return QueryStructure(
        intent="predecessor",
        subject=subject,
        target_entity=x,
        detected_marker="before_single",
    )


def _match_range(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """Calendar range (in YYYY / Q<n> YYYY / during <month>)."""
    r = _detect_range(query)
    if r is None:
        return None
    range_start, range_end = r
    return QueryStructure(
        intent="range",
        subject=subject,
        target_entity=None,
        detected_marker="range",
        range_start=range_start,
        range_end=range_end,
    )


def _match_retirement(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """Retirement verb in retirement *structure*.

    A bare retirement verb isn't enough — "Rachel moved to Seattle"
    contains "moved" (Layer 2 supersedes marker, mined from
    "authentication moves from session cookies to OAuth") but isn't
    a retirement query.  Same verb, different grammatical role.

    Structural patterns that qualify as retirement context:

    * **Imperative / infinitive + object** — ``retire <X>``,
      ``deprecate <X>``, ``decommission <X>``, ``supersede <X>``,
      ``stop using <X>``.  Verb-at-start-of-clause form.
    * **Passive voice** — ``<X> is/was/has been retired``,
      ``<X> was deprecated``.
    * **"Led to" retirement** — ``led to <retirement_verb>``,
      ``decision to <retirement_verb>``.  Hybrid cause_of +
      retirement; this production takes precedence because it IS
      about the retirement event.
    * **Directional with "from"** — ``move(s) from <X>``.  Without
      "from", "move" is plain motion (not retirement).

    Bare verb presence anywhere in the query (e.g., "did Rachel
    move?") does NOT match.  Production rejects; caller falls
    through to other intents or abstains.
    """
    marker = _find_marker(query, "retirement")
    if marker is None:
        return None
    q_low = query.lower()

    # Build a stem-based verb pattern so "retire"/"retired"/
    # "retirement" all match via the same structural patterns below.
    # ``marker`` may be either surface form (came from the first
    # layer: word-boundary or stem match); normalize both sides.
    from experiments.temporal_trajectory.vocab_induction import _stem as _do_stem
    marker_stem = _do_stem(marker)
    verb_re = re.escape(marker_stem) + r"\w*"
    # Collect retirement-structure patterns.
    structure_patterns = [
        # Passive: "X is/was/has been retired"
        rf"\b(?:is|was|are|were|has\s+been|have\s+been)\s+"
        rf"(?:fully\s+|now\s+|officially\s+)?{verb_re}\b",
        # Imperative / infinitive: "retire X", "to retire X", "led to retire X"
        rf"\b(?:to\s+|should\s+|must\s+|will\s+|led\s+to\s+|decided\s+to\s+|"
        rf"decision\s+to\s+|plan\s+to\s+)?{verb_re}\s+"
        rf"(?:the\s+|our\s+|its\s+|their\s+)?\w",
        # Moves-from (directional) — "move" requires "from" nearby.
        rf"\b{verb_re}\s+from\b",
    ]
    # "move" and its variants need "from" — bare motion isn't retirement.
    if marker.startswith(("mov", "migrat")):
        pat = rf"\b{verb_re}\s+from\b"
        if not re.search(pat, q_low):
            return None
    else:
        # Other verbs: any structural pattern must match.
        if not any(re.search(p, q_low) for p in structure_patterns):
            return None

    target = lookup_entity(query)
    return QueryStructure(
        intent="retirement",
        subject=subject,
        target_entity=target,
        detected_marker=marker,
    )


def _match_still(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'still <verb> X' / 'currently in/on X' / 'still <adj>'.

    Requires a resolvable object (bare 'still' with no object falls
    through to the next production)."""
    marker = _find_marker(query, "still")
    if marker is None:
        return None
    still_obj = _extract_still_object(query)
    if not still_obj:
        return None
    target = _canonicalize_target(still_obj)
    if not target:
        return None
    return QueryStructure(
        intent="still",
        subject=subject,
        target_entity=target,
        detected_marker=marker,
    )


def _match_cause_of(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'what caused / led to / reason for X' (without retirement verb).

    **Slot validation (production rejects on failure).**  A cause_of
    production accepts the query only when the target resolves to a
    real, specific entity — either a known issue word (from the
    auto-derived issue inventory) or a non-domain corpus entity.

    Concrete effect: "What caused the outage on payments?" — "outage"
    isn't an issue entity and isn't in corpus vocab.  Target
    collapses to "payments project" (subject's domain noun).  The
    production REJECTS rather than silently returning the subject
    origin.  intent falls through to ``none`` and the caller's BM25
    handles it.

    Without this guard, the cause_of handler would fire its content-
    marker fallback on any subject and confidently return the
    subject's earliest issue memory even when the user asked about
    an unknown concept — confidently-wrong being worse than abstain.
    """
    marker = _find_marker(query, "cause_of")
    if marker is None:
        return None
    target = _prefer_issue_entity(query) or lookup_entity(query)
    # Reject when target collapsed to a domain noun — the query's
    # specific concept is unresolvable at this layer.
    if target is not None and target.lower() in _DOMAIN_NOUNS:
        return None
    return QueryStructure(
        intent="cause_of",
        subject=subject,
        target_entity=target,
        detected_marker=marker,
    )


def _match_origin(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'original / first / initial / starting X'."""
    marker = _find_marker(query, "origin")
    if marker is None:
        return None
    target = lookup_entity(query)
    return QueryStructure(
        intent="origin",
        subject=subject,
        target_entity=target,
        detected_marker=marker,
    )


def _match_current(
    query: str, subject: str | None,
) -> QueryStructure | None:
    """'current / now / latest / as of today' — bare current-state marker."""
    marker = _find_marker(query, "current")
    if marker is None:
        return None
    return QueryStructure(
        intent="current",
        subject=subject,
        target_entity=None,
        detected_marker=marker,
    )


# Production list — order is specificity (most-specific first).  Add
# new intents by inserting at the appropriate specificity level; each
# matcher is self-contained so neighbours are unaffected.
_PRODUCTIONS = [
    _match_interval,           # two-slot: X ∧ Y
    _match_before_named,       # two-slot: ordering X vs Y
    _match_transitive_cause,   # "eventually led to X"
    _match_concurrent,         # "during X"
    _match_sequence,           # "after X"
    _match_predecessor,        # "before X"
    _match_range,              # calendar range
    _match_retirement,         # retirement verb
    _match_still,              # "still X" with required object
    _match_cause_of,           # "caused X" (without retirement)
    _match_origin,             # "original X"
    _match_current,            # bare current-state marker
]


# ── Helpers ────────────────────────────────────────────────────────

# Morphological normalization lives in ``vocab_induction._stem``
# (Snowball).  No hand-maintained stem dict.


def _detect_range(query: str) -> tuple[str, str] | None:
    """Detect a range-intent query via explicit calendar references.

    Delegates to NCMS's ``temporal_parser`` but filters out hits
    that aren't true range intents:

    * ``recency_bias`` — "current"/"latest"/"now" parse to a "last
      48h" range but the intent is actually ``current``.  Exclude.
    * ``ordinal`` — "first"/"last" parse via ordinal branch, handled
      elsewhere.  Exclude.
    * **Single-day spans** (< 7 days) — "today" / "yesterday" parse
      to a day-wide range but the intent is ``current`` / immediate-
      past, not a filter.  Range intent should fire only on
      meaningful intervals (explicit year, quarter, month, "between
      X and Y" over ≥ 7 days).
    """
    from datetime import timedelta
    from ncms.domain.temporal_parser import parse_temporal_reference
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


def _prefer_issue_entity(query: str) -> str | None:
    """Scan the query for any issue-entity token (auto-derived from
    edge ``retires_entities``).  Longest match wins.  Returns None
    if no issue token is present.

    Why this matters: "What caused the delay on payments?" contains
    both "delay" (issue entity) and "payments" (subject noun).
    Generic entity lookup picks whichever is in Layer 1 vocabulary;
    for cause_of queries we want the issue.
    """
    q = query.lower()
    matches = [
        ent for ent in _ISSUE_ENTITIES
        if _word_like(ent, q)
    ]
    if not matches:
        return None
    # Longest match wins (multi-word issues like "beta launch" beat
    # single-word ones).
    return max(matches, key=len)


def _word_like(token: str, query_lower: str) -> bool:
    pattern = r"\b" + re.escape(token) + r"\w*\b"
    return re.search(pattern, query_lower) is not None


def _canonicalize_target(raw: str) -> str | None:
    """Map a raw extracted target word to a canonical entity.

    Uses Layer 1's stemmer-backed entity lookup (which handles
    morphology uniformly).  Falls through to lowercase-stripped
    input when no vocabulary match exists — downstream handlers
    can still text-match this against memory content.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    entity = lookup_entity(raw)
    if entity is not None:
        return entity
    return raw.lower()


def summary() -> str:
    """Inspection output for the seed vocabulary and augmented markers."""
    lines = ["Layer 3 — intent-marker vocabulary", "=" * 60]
    for kind in ["current", "still", "origin", "cause_of", "retirement"]:
        seed = sorted(_SEED_MARKERS[kind])
        augmented = sorted(_AUGMENTED_MARKERS[kind] - set(_SEED_MARKERS[kind]))
        lines.append(f"[{kind}]")
        lines.append(f"  seed ({len(seed)}): {seed}")
        if augmented:
            lines.append(f"  corpus-augmented ({len(augmented)}): {augmented}")
    return "\n".join(lines)
