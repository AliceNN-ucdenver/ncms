"""Mock of NCMS's ``ReconciliationService`` edge-creation behaviour.

In production, NCMS's ``ReconciliationService`` observes each new
memory and, when it matches an existing entity-state, emits a typed
edge (``SUPPORTS``/``REFINES``/``SUPERSEDES``/``CONFLICTS_WITH``).
Today this uses a combination of heuristic features + an optional
LLM classifier.  For the LG experiment we don't need any LLM — but
we DO need to validate that the grammar works on edges a realistic
reconciliator would produce, not only on hand-curated ones.

This module simulates what that ingest-time edge creation would look
like using only deterministic heuristics:

* **Same-subject** — candidate edges only between memories sharing
  a subject (pre-assigned at ingest).
* **Temporal order** — ``src.observed_at < dst.observed_at``.
* **Entity overlap** — memories must share at least one entity.
* **Transition-type inference** from destination content:
    * ``supersedes`` — content matches replacement/retire verbs
      (*retire*, *supersede*, *replace*, *moves from X to Y*, …).
      These verbs are auto-derived from Layer 2 (``edge_markers``).
    * ``refines``    — content matches extension verbs (*add*,
      *extend*, *initiate*, *perform*, …).
    * Unclassified pairs are left out (no edge emitted).
* **``retires_entities``** — for each candidate ``supersedes`` edge,
  compute the set diff:  entities present in src's zone but absent
  from dst's entity set.  Tag supersedes edge accordingly.  This
  mirrors what NCMS would do when a new memory ends a prior state.

The mock is intentionally *lossier* than the hand-labels so we can
see which edges the grammar requires that reconciliation wouldn't
produce.  That diff is the gap a production integration has to
close — either by extending reconciliation or by making LG robust
to missing edges.

Usage::

    from experiments.temporal_trajectory.mock_reconciliation import (
        reconcile_corpus, diff_against_hand_labels,
    )

    mock_edges = reconcile_corpus()
    added, missing, mismatched = diff_against_hand_labels(mock_edges)
    print(f"mock produced {len(mock_edges)} edges")
    print(f"missing vs hand: {missing}")
    print(f"mismatched transitions: {mismatched}")

Not imported by the main retrieval path — just a validation tool.
"""

from __future__ import annotations

from dataclasses import replace

from experiments.temporal_trajectory.corpus import (
    ADR_CORPUS,
    EDGES as HAND_EDGES,
    Edge,
)
from experiments.temporal_trajectory.edge_markers import MARKERS as LAYER2


import re as _re


_NEGATION_BEFORE = _re.compile(
    r"\b(not|didn'?t|doesn'?t|hasn'?t|haven'?t|isn'?t|aren'?t|wasn'?t|"
    r"weren'?t|never|no|without)\s+\w{0,15}\s*$",
)


def _marker_hit(marker: str, content_lower: str) -> bool:
    """True iff ``marker`` appears in ``content_lower`` without a
    negation cue in the preceding ~25 chars.  Ignoring negated hits
    stops "not resolved" / "has not been confirmed" from being
    counted as a resolution/confirmation signal."""
    for m in _re.finditer(rf"\b{_re.escape(marker)}\w*\b", content_lower):
        before = content_lower[max(0, m.start() - 25):m.start()]
        if _NEGATION_BEFORE.search(before):
            continue
        return True
    return False


def _detect_transition(src_content: str, dst_content: str) -> str | None:
    """Scan destination content against Layer 2's induced markers to
    guess the transition type.

    Returns ``None`` when no strong signal is present — reconciliation
    wouldn't emit an edge in that case.  We deliberately don't
    guess: the point is to see which edges the grammar needs that
    heuristic reconciliation would miss.

    Negated marker occurrences ("not resolved", "has not been
    confirmed") are skipped — they flip the marker's polarity and
    would otherwise cause "Mid-PT check-in.  Symptoms improving but
    not resolved" to register as a supersedes signal.
    """
    dst_lower = dst_content.lower()
    supersedes_hits = sum(
        1 for m in LAYER2.markers.get("supersedes", ())
        if _marker_hit(m, dst_lower)
    )
    refines_hits = sum(
        1 for m in LAYER2.markers.get("refines", ())
        if _marker_hit(m, dst_lower)
    )
    # Require a meaningful lead — no edge at a tie.
    if supersedes_hits > refines_hits:
        return "supersedes"
    if refines_hits > supersedes_hits:
        return "refines"
    return None


def _infer_retires_entities(
    src_entities: frozenset[str],
    dst_entities: frozenset[str],
    dst_content: str = "",
    subject: str | None = None,
) -> frozenset[str]:
    """Infer what a supersedes transition retired.

    Uses :mod:`retirement_extractor` to pull entities from the
    destination content via Layer 2's induced retirement verbs
    (active / passive / directional shapes), unioned with filtered
    set-diff for entities that disappeared silently.

    Upgrade over pure set-diff: catches entities that remain in the
    destination's entity list BUT are explicitly named as retired in
    the destination's content (e.g., ADR-021 content "Retire
    long-lived JWTs" with JWT still in ADR-021's entity set).
    """
    from experiments.temporal_trajectory.retirement_extractor import (
        extract_retired,
    )
    return extract_retired(
        dst_content=dst_content,
        src_entities=src_entities,
        dst_entities=dst_entities,
        subject=subject,
    )


def reconcile_corpus() -> list[Edge]:
    """Produce the set of typed edges a deterministic reconciliator
    would emit for the corpus.  Emits one edge per same-subject
    consecutive-in-time memory pair with entity overlap."""
    mems = sorted(
        [m for m in ADR_CORPUS if m.subject is not None],
        key=lambda m: (m.subject or "", m.observed_at),
    )
    edges: list[Edge] = []

    # Walk same-subject memories in observed_at order; emit edges
    # between pairs with entity overlap.
    by_subject: dict[str, list] = {}
    for m in mems:
        by_subject.setdefault(m.subject, []).append(m)

    for subject, subj_mems in by_subject.items():
        for i, src in enumerate(subj_mems):
            for dst in subj_mems[i + 1:i + 2]:  # adjacent only
                if not (src.entities & dst.entities):
                    continue
                transition = _detect_transition(src.content, dst.content)
                if transition is None:
                    # Conservative default: when same-subject adjacency +
                    # entity overlap exists but no verb signal fires
                    # (e.g., negated-only content like "Symptoms not
                    # resolved", or signal-free content like "Stripe v1
                    # deprecation notice received"), treat as a refines
                    # continuation.  This mirrors what NCMS's reconciler
                    # does when it can't decide — default to SUPPORTS/
                    # REFINES rather than drop the edge entirely.
                    transition = "refines"
                # ``retires_entities`` is computed against the CUMULATIVE
                # state of the subject, not just the direct predecessor.
                # In NCMS this is what the L2 entity_state table tracks:
                # when a supersedes fires, reconciliation compares the
                # new memory to the FULL active state of the subject,
                # not the single previous memory.  Mock it here by
                # unioning entities across all ancestors ≤ src.
                ancestor_entities: set[str] = set()
                for ancestor in subj_mems[:i + 1]:
                    ancestor_entities |= ancestor.entities
                retires = (
                    _infer_retires_entities(
                        frozenset(ancestor_entities),
                        dst.entities,
                        dst_content=dst.content,
                        subject=dst.subject,
                    )
                    if transition == "supersedes"
                    else frozenset()
                )
                edges.append(Edge(
                    src=src.mid,
                    dst=dst.mid,
                    transition=transition,
                    retires_entities=retires,
                ))
    return edges


def diff_against_hand_labels(
    mock_edges: list[Edge],
) -> tuple[list[Edge], list[Edge], list[tuple[Edge, Edge]]]:
    """Compare mock edges to hand-labeled ``EDGES``.

    Returns ``(extra, missing, mismatched)``:

    * ``extra``       — edges the mock produced that aren't in the
      hand list.  These are false positives.
    * ``missing``     — edges in the hand list the mock didn't
      produce.  These are the gaps reconciliation would need to
      close.
    * ``mismatched``  — edges present in both lists but labelled
      with different transition types.  Each entry is
      ``(hand_edge, mock_edge)``.
    """
    key = lambda e: (e.src, e.dst)  # noqa: E731
    hand_by_key = {key(e): e for e in HAND_EDGES}
    mock_by_key = {key(e): e for e in mock_edges}

    extra = [e for k, e in mock_by_key.items() if k not in hand_by_key]
    missing = [e for k, e in hand_by_key.items() if k not in mock_by_key]
    mismatched: list[tuple[Edge, Edge]] = []
    for k, hand_e in hand_by_key.items():
        mock_e = mock_by_key.get(k)
        if mock_e is None:
            continue
        if mock_e.transition != hand_e.transition:
            mismatched.append((hand_e, mock_e))

    return extra, missing, mismatched


def summary() -> str:
    mock = reconcile_corpus()
    extra, missing, mismatched = diff_against_hand_labels(mock)
    lines = ["Mock ReconciliationService diff vs hand-labeled EDGES",
             "=" * 68]
    lines.append(
        f"Hand-labeled edges:  {len(HAND_EDGES)}"
    )
    lines.append(
        f"Mock-generated edges: {len(mock)}"
    )
    lines.append("")
    lines.append("Agreement breakdown")
    matched = len(mock) - len(extra)
    lines.append(f"  matched (same src/dst):  {matched}")
    lines.append(f"  extra (mock-only):       {len(extra)}")
    lines.append(f"  missing (hand-only):     {len(missing)}")
    lines.append(f"  mismatched transitions:  {len(mismatched)}")
    lines.append("")

    if extra:
        lines.append("Extra (mock produced, hand didn't):")
        for e in extra:
            lines.append(
                f"  {e.src} --[{e.transition}]--> {e.dst}  "
                f"retires={sorted(e.retires_entities)}"
            )
        lines.append("")
    if missing:
        lines.append("Missing (hand has, mock doesn't):")
        for e in missing:
            lines.append(
                f"  {e.src} --[{e.transition}]--> {e.dst}  "
                f"retires={sorted(e.retires_entities)}"
            )
        lines.append("")
    if mismatched:
        lines.append("Mismatched transitions:")
        for hand_e, mock_e in mismatched:
            lines.append(
                f"  {hand_e.src}→{hand_e.dst}  "
                f"hand={hand_e.transition}  mock={mock_e.transition}"
            )
        lines.append("")

    # Check retires_entities accuracy on matched edges.
    retires_matched = 0
    retires_different = 0
    for e in mock:
        hand_e = next(
            (h for h in HAND_EDGES if h.src == e.src and h.dst == e.dst),
            None,
        )
        if hand_e is None or hand_e.transition != e.transition:
            continue
        if hand_e.retires_entities == e.retires_entities:
            retires_matched += 1
        else:
            retires_different += 1
    lines.append(
        f"Retires_entities on matched supersedes edges: "
        f"{retires_matched} exact, {retires_different} differ"
    )

    return "\n".join(lines)
