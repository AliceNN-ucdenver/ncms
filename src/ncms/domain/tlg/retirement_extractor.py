"""Structural extractor for ``retires_entities``.

Pure port of ``experiments/temporal_trajectory/retirement_extractor.py``,
adapted for NCMS.  Two changes from the research version:

* The Layer-2 retirement-verb inventory is passed in as a parameter
  instead of being read from a global ``MARKERS`` constant.
  ``ReconciliationService`` is responsible for loading the inventory
  (from ``grammar_transition_markers`` once Phase 2 induction lands;
  from :data:`SEED_RETIREMENT_VERBS` in the meantime).
* The subject's domain-entity set (topical nouns appearing in ≥80 %
  of the subject's memories) is also a parameter.  The caller
  computes it from the MemoryStore and passes it in, keeping this
  module free of infrastructure dependencies.

The algorithm itself is unchanged — regex-based pattern matching
against retirement-verb sentences in ``dst_content`` (active / passive
/ directional patterns), union with filtered set-diff, and a
``dst_new`` filter to prevent the new state's introductions from
being marked as retirements.

See ``docs/p1-plan.md`` §3 and ``docs/temporal-linguistic-geometry.md``
§5 for the theory.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Seed retirement-verb inventory
# ---------------------------------------------------------------------------
#
# Phase 1 uses this constant set because ``grammar_transition_markers``
# is empty until Phase 2 induction runs.  These verbs were observed in
# the research corpus as *distinctively* associated with ``supersedes``
# or ``retires`` transitions (see ``experiments/temporal_trajectory/
# edge_markers.py``).  A production deployment with populated L2
# markers passes the table's contents to :func:`extract_retired`
# instead; this seed is the cold-start default.

SEED_RETIREMENT_VERBS: frozenset[str] = frozenset(
    {
        "supersedes",
        "superseded",
        "retire",
        "retired",
        "retires",
        "replace",
        "replaced",
        "replaces",
        "deprecate",
        "deprecated",
        "deprecates",
        "deprecating",
        "move",
        "moved",
        "moves",
        "migrate",
        "migrated",
        "migrates",
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MID_LIKE = re.compile(r"^[A-Z]{2,5}-\w*$", re.IGNORECASE)


def _sentences(content: str) -> list[str]:
    """Split content into sentences on punctuation or newlines."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", content)
    return [p.strip() for p in parts if p.strip()]


def _is_excluded(ent_low: str, domain: frozenset[str]) -> bool:
    """Skip doc cross-references (``ADR-021``) and topical domain nouns."""
    if _MID_LIKE.match(ent_low):
        return True
    return ent_low in domain


def _match_in_window(
    window: str,
    candidates: dict[str, str],
    domain: frozenset[str],
) -> set[str]:
    """Return canonical entity names that appear as whole words in ``window``."""
    out: set[str] = set()
    for ent_low, ent_canon in candidates.items():
        if _is_excluded(ent_low, domain):
            continue
        if re.search(rf"\b{re.escape(ent_low)}\w*\b", window, re.IGNORECASE):
            out.add(ent_canon)
    return out


# ---------------------------------------------------------------------------
# Per-sentence extraction helpers
# ---------------------------------------------------------------------------


def _find_verb_hits(
    sent_low: str,
    retirement_verbs: frozenset[str],
) -> list[tuple[str, int, int]]:
    """Every ``(verb, start, end)`` occurrence in the sentence."""
    hits: list[tuple[str, int, int]] = []
    for verb in retirement_verbs:
        for match in re.finditer(rf"\b{re.escape(verb)}\w*\b", sent_low):
            hits.append((verb, match.start(), match.end()))
    return hits


def _is_directional(verb_hits: list[tuple[str, int, int]]) -> bool:
    """``moves`` / ``migrates`` — verbs that require a ``from`` anchor."""
    return any(v.startswith(("mov", "migrat")) for v, _, _ in verb_hits)


def _directional_retired(
    sent_low: str,
    candidates: dict[str, str],
    domain: frozenset[str],
) -> set[str]:
    """Extract the ``from``-side of a ``moves from X to Y`` sentence.

    Returns the empty set when the sentence has no ``from`` anchor
    (``moves to Y`` alone names only the new state).
    """
    from_m = re.search(r"\bfrom\b", sent_low)
    if from_m is None:
        return set()
    from_end = from_m.end()
    to_m = re.search(
        r"\b(?:to|into|toward|towards)\b",
        sent_low[from_end:],
    )
    to_start = from_end + to_m.start() if to_m else len(sent_low)
    return _match_in_window(sent_low[from_end:to_start], candidates, domain)


def _pre_post_window_retired(
    verb_hits: list[tuple[str, int, int]],
    sent_low: str,
    candidates: dict[str, str],
    domain: frozenset[str],
) -> set[str]:
    """Non-directional verbs — scan both pre- and post-verb windows.

    Rationale for scanning BOTH sides:
      * Passive ``<NP> (is|was|are|were) <verb>`` — pre-verb NP retired.
      * Active nominal ``<NP> <verb> <OBJ>`` — either side potentially
        retired; the ``dst_new`` filter drops the new-state object.
      * Imperative ``<verb> <NP>`` — post-verb NP retired.
    """
    retired: set[str] = set()
    for _, vstart, vend in verb_hits:
        pre = sent_low[max(0, vstart - 60) : vstart]
        post = sent_low[vend : vend + 80]
        retired |= _match_in_window(pre, candidates, domain)
        retired |= _match_in_window(post, candidates, domain)
    return retired


def _retired_from_sentence(
    sentence: str,
    retirement_verbs: frozenset[str],
    candidates: dict[str, str],
    domain: frozenset[str],
) -> set[str]:
    """Structural extraction for a single sentence.

    Dispatches to the directional or non-directional handler based on
    the verbs that fire.  Returns ``set()`` when no retirement verb
    matches the sentence at all.
    """
    sent_low = sentence.lower()
    verb_hits = _find_verb_hits(sent_low, retirement_verbs)
    if not verb_hits:
        return set()
    if _is_directional(verb_hits):
        if "from" not in sent_low:
            # ``moves to Y`` alone names only the new state — no
            # retirement can be inferred.
            return set()
        return _directional_retired(sent_low, candidates, domain)
    return _pre_post_window_retired(verb_hits, sent_low, candidates, domain)


# ---------------------------------------------------------------------------
# Filters composed after the sentence-level pass
# ---------------------------------------------------------------------------


def _setdiff_retired(
    src_entities: frozenset[str],
    dst_entities: frozenset[str],
    domain: frozenset[str],
) -> set[str]:
    """Entities that dropped out silently (in src, not in dst).

    Safety net for silent disappearances — content-scan catches
    in-content retirements, set-diff catches entities that simply
    stop being mentioned.  Respects the domain + MID-reference
    exclusions so topical nouns don't leak in.
    """
    return {
        ent
        for ent in src_entities
        if ent not in dst_entities and not _is_excluded(ent.lower(), domain)
    }


def _drop_dst_new(
    retired: set[str],
    src_entities: frozenset[str],
    dst_entities: frozenset[str],
) -> set[str]:
    """Drop entities that appear only in dst (introduced, not retired).

    Fixes the failure mode where "Arthroscopic surgery scheduled" would
    mark ``arthroscopic surgery`` as retired — it's the NEW state, not
    the old one.
    """
    dst_new = {e.lower() for e in dst_entities} - {e.lower() for e in src_entities}
    return {e for e in retired if e.lower() not in dst_new}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_retired(
    dst_content: str,
    src_entities: frozenset[str],
    dst_entities: frozenset[str],
    *,
    retirement_verbs: frozenset[str] = SEED_RETIREMENT_VERBS,
    domain_entities: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Structurally extract the set of entities retired by a SUPERSEDES edge.

    Pipeline:

    1. For every sentence in ``dst_content``, run
       :func:`_retired_from_sentence` — dispatches to the directional
       or non-directional handler based on which retirement verbs fire.
    2. Union the structural result with :func:`_setdiff_retired` — the
       silent-disappearance safety net (entities in src, not in dst).
    3. Drop any entity that appears only in dst via
       :func:`_drop_dst_new` — protects against the new state's
       introductions being tagged as retired.

    Args:
      dst_content: content of the **new** (superseding) memory — the
        announcement of the state change.  Retirement phrases are
        mined from here.
      src_entities: entities on the **old** (superseded) memory.
      dst_entities: entities on the **new** memory.
      retirement_verbs: Layer-2 inventory of verbs that signal
        supersession / retirement.  Pass the content of
        ``grammar_transition_markers`` once Phase 2 induction is
        populated; falls back to :data:`SEED_RETIREMENT_VERBS`.
      domain_entities: topical nouns appearing in ≥80 % of the
        subject's memories.  Passed empty when the subject is unknown
        or the corpus is too cold to compute.
    """
    candidates = {e.lower(): e for e in (src_entities | dst_entities)}
    retired: set[str] = set()
    for sentence in _sentences(dst_content):
        retired |= _retired_from_sentence(
            sentence,
            retirement_verbs,
            candidates,
            domain_entities,
        )
    retired |= _setdiff_retired(src_entities, dst_entities, domain_entities)
    retired = _drop_dst_new(retired, src_entities, dst_entities)
    return frozenset(retired)
