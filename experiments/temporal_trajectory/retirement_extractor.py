"""Structural extractor for ``retires_entities``.

The mock-reconciliation run uses plain set-diff (``src.entities -
dst.entities``) to infer what a supersedes edge retired.  That fails
whenever the retired entity is still named in the successor's
content — e.g., ADR-021 says "Retire long-lived JWTs" but JWT is also
in ADR-021's own entity set, so set-diff ignores it.

This module replaces set-diff with structural extraction grounded in
Layer 2's induced verb inventory:

* **Retirement verbs** come from Layer 2's ``supersedes`` and
  ``retires`` marker sets — already mined from edge destinations, so
  the inventory grows as the corpus grows.
* **Active pattern** ``<retirement_verb> <NP>``: entities appearing
  after the verb in the same sentence are retired.
* **Passive pattern** ``<NP> (is|was|are|were) [fully|now] <verb>``:
  entities appearing before the aux are retired.
* **Directional pattern** ``moves/migrates from <X> [to <Y>]``: only
  the ``from``-side is retired.  Without a ``from``, the verb is
  skipped (the source state isn't named).

Filters applied:

* **Mid-like references** (``ADR-xxx``/``MED-xx``/``PROJ-xx``) are
  dropped — they're doc cross-references, not retired entities.
* **Domain nouns** (entities appearing in ≥80 % of the subject's
  memories, like "authentication" for the auth subject) are dropped
  — they're the subject's topic, not a retired state.

Falls back to filtered set-diff when structural extraction produces
no hits (e.g., ``refines`` transitions with no retirement verb at
all).  Final ``retires_entities`` is the union of structural
extraction + filtered set-diff — structural catches in-content
retirements, set-diff catches silent disappearances.
"""

from __future__ import annotations

import re
from collections import Counter

from experiments.temporal_trajectory.corpus import ADR_CORPUS
from experiments.temporal_trajectory.edge_markers import MARKERS as _LAYER2


_MID_LIKE = re.compile(r"^[A-Z]{2,5}-\w*$", re.IGNORECASE)


def _retirement_verbs() -> frozenset[str]:
    """Retirement verbs = Layer 2's supersedes + retires induced markers."""
    return (
        frozenset(_LAYER2.markers.get("supersedes", frozenset()))
        | frozenset(_LAYER2.markers.get("retires", frozenset()))
    )


def _sentences(content: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", content)
    return [p.strip() for p in parts if p.strip()]


_DOMAIN_CACHE: dict[str, frozenset[str]] = {}


def _domain_entities(subject: str) -> frozenset[str]:
    """Entities in ≥80% of the subject's memories — domain topic nouns."""
    cached = _DOMAIN_CACHE.get(subject)
    if cached is not None:
        return cached
    subj_mems = [m for m in ADR_CORPUS if m.subject == subject]
    if len(subj_mems) < 3:
        _DOMAIN_CACHE[subject] = frozenset()
        return frozenset()
    threshold = max(2, int(0.8 * len(subj_mems)))
    counts: Counter[str] = Counter()
    for m in subj_mems:
        for e in m.entities:
            counts[e.lower()] += 1
    domain = frozenset(e for e, c in counts.items() if c >= threshold)
    _DOMAIN_CACHE[subject] = domain
    return domain


def _is_excluded(ent_low: str, domain: frozenset[str]) -> bool:
    if _MID_LIKE.match(ent_low):
        return True
    if ent_low in domain:
        return True
    return False


def _match_in_window(
    window: str,
    candidates: dict[str, str],
    domain: frozenset[str],
) -> set[str]:
    out: set[str] = set()
    for ent_low, ent_canon in candidates.items():
        if _is_excluded(ent_low, domain):
            continue
        if re.search(rf"\b{re.escape(ent_low)}\w*\b", window, re.IGNORECASE):
            out.add(ent_canon)
    return out


def extract_retired(
    dst_content: str,
    src_entities: frozenset[str],
    dst_entities: frozenset[str],
    subject: str | None,
) -> frozenset[str]:
    """Structurally extract retires_entities from ``dst_content``.

    Returns the union of:

    * Entities named near a retirement verb in ``dst_content`` (active
      / passive / directional patterns).
    * Filtered set-diff ``src - dst`` (excluding domain nouns + mid
      references).

    Empty result when neither signal fires.
    """
    verbs = _retirement_verbs()
    domain = _domain_entities(subject) if subject else frozenset()
    candidates = {e.lower(): e for e in (src_entities | dst_entities)}

    retired: set[str] = set()

    for sent in _sentences(dst_content):
        sent_low = sent.lower()
        # Find all retirement-verb occurrences in the sentence.
        verb_hits: list[tuple[str, int, int]] = []
        for v in verbs:
            for m in re.finditer(rf"\b{re.escape(v)}\w*\b", sent_low):
                verb_hits.append((v, m.start(), m.end()))
        if not verb_hits:
            continue

        # Directional verbs (moves/migrates): require "from" in sentence.
        directional = any(
            v.startswith(("mov", "migrat")) for v, _, _ in verb_hits
        )
        if directional and "from" not in sent_low:
            # "moves to Y" alone names only the new state — source is
            # unnamed.  Skip the sentence entirely (no retired entity
            # can be inferred).
            continue

        # "moves from X to Y" → extract X (between "from" and "to"/end).
        if directional and "from" in sent_low:
            from_m = re.search(r"\bfrom\b", sent_low)
            if from_m is not None:
                from_end = from_m.end()
                to_m = re.search(
                    r"\b(?:to|into|toward|towards)\b", sent_low[from_end:],
                )
                to_start = from_end + to_m.start() if to_m else len(sent_low)
                window = sent_low[from_end:to_start]
                retired |= _match_in_window(window, candidates, domain)
                continue

        # Non-directional verbs: scan BOTH pre-verb and post-verb
        # windows for candidate entities.  Rationale:
        #
        #   * ``<NP> (is|was|are|were) <verb>`` — passive — pre-verb NP
        #     is retired ("Password authentication is fully retired").
        #   * ``<NP> <verb> <OBJ>`` — active with nominal subject — both
        #     sides potentially retired ("Blocker resolved by…").  The
        #     dst_new filter below removes the verb's new-state object.
        #   * ``<verb> <NP>`` — imperative — post-verb NP retired
        #     ("Retire long-lived JWTs").
        for _, vstart, vend in verb_hits:
            pre_window = sent_low[max(0, vstart - 60):vstart]
            post_window = sent_low[vend:vend + 80]
            retired |= _match_in_window(pre_window, candidates, domain)
            retired |= _match_in_window(post_window, candidates, domain)

    # ALWAYS union with filtered set-diff — structural catches
    # in-content retirements, set-diff catches silent disappearances.
    for ent in src_entities:
        if ent in dst_entities:
            continue
        if _is_excluded(ent.lower(), domain):
            continue
        retired.add(ent)

    # dst_new filter: entities first appearing in dst (not in
    # src_entities) can't be retired by this edge — they're the NEW
    # state the transition introduced, not what it replaced.
    # Fixes "Arthroscopic surgery scheduled" extracting "arthroscopic
    # surgery" as retired (it's being introduced, not retired).
    dst_new = {e.lower() for e in dst_entities} - {e.lower() for e in src_entities}
    retired = {e for e in retired if e.lower() not in dst_new}

    return frozenset(retired)
