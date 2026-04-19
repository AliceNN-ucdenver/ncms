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
        subject's memories.  Passed empty when the subject is
        unknown or the corpus is too cold to compute.

    Returns:
      Frozenset of entity names retired by this transition.  Empty
      when no retirement signal fires.
    """
    candidates = {e.lower(): e for e in (src_entities | dst_entities)}
    retired: set[str] = set()

    for sent in _sentences(dst_content):
        sent_low = sent.lower()

        # Find all retirement-verb occurrences in the sentence.
        verb_hits: list[tuple[str, int, int]] = []
        for verb in retirement_verbs:
            for match in re.finditer(rf"\b{re.escape(verb)}\w*\b", sent_low):
                verb_hits.append((verb, match.start(), match.end()))
        if not verb_hits:
            continue

        # Directional verbs (``moves``/``migrates``): require ``from`` in
        # the sentence.  ``moves to Y`` alone names only the new state
        # — the source state isn't named, so no retirement can be
        # inferred.
        directional = any(
            v.startswith(("mov", "migrat")) for v, _, _ in verb_hits
        )
        if directional and "from" not in sent_low:
            continue

        # ``moves from X to Y`` → extract X (between ``from`` and
        # ``to``/end-of-sentence).
        if directional and "from" in sent_low:
            from_m = re.search(r"\bfrom\b", sent_low)
            if from_m is not None:
                from_end = from_m.end()
                to_m = re.search(
                    r"\b(?:to|into|toward|towards)\b",
                    sent_low[from_end:],
                )
                to_start = from_end + to_m.start() if to_m else len(sent_low)
                window = sent_low[from_end:to_start]
                retired |= _match_in_window(window, candidates, domain_entities)
                continue

        # Non-directional verbs — scan both pre- and post-verb windows.
        # Rationale:
        #   * ``<NP> (is|was|are|were) <verb>`` — passive — pre-verb
        #     NP retired ("session cookies are fully retired").
        #   * ``<NP> <verb> <OBJ>`` — active nominal — either side
        #     potentially retired (dst_new filter below drops the
        #     verb's new-state object).
        #   * ``<verb> <NP>`` — imperative — post-verb NP retired
        #     ("Retire long-lived JWTs").
        for _, vstart, vend in verb_hits:
            pre = sent_low[max(0, vstart - 60):vstart]
            post = sent_low[vend:vend + 80]
            retired |= _match_in_window(pre, candidates, domain_entities)
            retired |= _match_in_window(post, candidates, domain_entities)

    # Union with filtered set-diff — structural catches in-content
    # retirements, set-diff catches silent disappearances (entities
    # that simply stop being mentioned).
    for ent in src_entities:
        if ent in dst_entities:
            continue
        if _is_excluded(ent.lower(), domain_entities):
            continue
        retired.add(ent)

    # ``dst_new`` filter: entities appearing only in dst (introduced
    # by the new state) can't be retired by this edge.  Prevents
    # "Arthroscopic surgery scheduled" marking ``arthroscopic surgery``
    # as retired when it's being introduced, not replaced.
    dst_new = {e.lower() for e in dst_entities} - {e.lower() for e in src_entities}
    retired = {e for e in retired if e.lower() not in dst_new}

    return frozenset(retired)
