"""CTLG cue taxonomy — linguistic cue labels for causal-temporal parsing.

This module defines the vocabulary of typed linguistic cues used by
the CTLG 6th head (BIO sequence tagger) to mark tokens in a query or
memory content.  The cue vocabulary and annotator contract is
documented in :doc:`../../../docs/research/ctlg-cue-guidelines.md`;
the grammar-level consumer (trajectory grammar :math:`G_{tr,c}` and
target grammar productions) is documented in
:doc:`../../../docs/research/ctlg-grammar.md`.

The key abstractions:

  * :data:`CueLabel` — a ``Literal`` enumerating the 29 BIO cue
    labels (14 cue families × {B, I} plus ``"O"``).
  * :class:`TaggedToken` — one token tagged with a cue label plus
    character offsets + confidence.
  * :data:`CUE_FAMILIES` — convenience mapping from cue family name
    (``"causal"``, ``"temporal"``, ``"ordinal"``, ``"modal"``,
    ``"referent"``) to the set of BIO labels that belong to it.

CTLG replaces the classification-style ``shape_intent_head`` from v6/v7.
See :doc:`../../../docs/completed/failed-experiments/shape-intent-classification.md`
for the retirement rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

#: All BIO cue labels.  Match to the guidelines doc — 14 typed
#: families × {B, I} + ``O``.  Order is stable for index-based
#: tensor operations in the training loop (don't reorder without
#: updating the manifest schema version).
CueLabel = Literal[
    "O",
    # Causal
    "B-CAUSAL_EXPLICIT",
    "I-CAUSAL_EXPLICIT",
    "B-CAUSAL_ALTLEX",
    "I-CAUSAL_ALTLEX",
    # Temporal — relative markers
    "B-TEMPORAL_BEFORE",
    "I-TEMPORAL_BEFORE",
    "B-TEMPORAL_AFTER",
    "I-TEMPORAL_AFTER",
    "B-TEMPORAL_DURING",
    "I-TEMPORAL_DURING",
    "B-TEMPORAL_SINCE",
    "I-TEMPORAL_SINCE",
    # Temporal — concrete anchors (dates, quarters, named periods)
    "B-TEMPORAL_ANCHOR",
    "I-TEMPORAL_ANCHOR",
    # Ordinal
    "B-ORDINAL_FIRST",
    "I-ORDINAL_FIRST",
    "B-ORDINAL_LAST",
    "I-ORDINAL_LAST",
    "B-ORDINAL_NTH",
    "I-ORDINAL_NTH",
    # Modal / counterfactual
    "B-MODAL_HYPOTHETICAL",
    "I-MODAL_HYPOTHETICAL",
    # Query-specific markers
    "B-ASK_CHANGE",
    "I-ASK_CHANGE",
    "B-ASK_CURRENT",
    "I-ASK_CURRENT",
    # Entity references
    "B-REFERENT",
    "I-REFERENT",
    "B-SUBJECT",
    "I-SUBJECT",
    "B-SCOPE",
    "I-SCOPE",
]

#: Frozen ordered tuple of all labels — feeds the adapter manifest's
#: ``cue_labels`` field and the BIO→id lookup during training.
CUE_LABELS: tuple[CueLabel, ...] = (
    "O",
    "B-CAUSAL_EXPLICIT",
    "I-CAUSAL_EXPLICIT",
    "B-CAUSAL_ALTLEX",
    "I-CAUSAL_ALTLEX",
    "B-TEMPORAL_BEFORE",
    "I-TEMPORAL_BEFORE",
    "B-TEMPORAL_AFTER",
    "I-TEMPORAL_AFTER",
    "B-TEMPORAL_DURING",
    "I-TEMPORAL_DURING",
    "B-TEMPORAL_SINCE",
    "I-TEMPORAL_SINCE",
    "B-TEMPORAL_ANCHOR",
    "I-TEMPORAL_ANCHOR",
    "B-ORDINAL_FIRST",
    "I-ORDINAL_FIRST",
    "B-ORDINAL_LAST",
    "I-ORDINAL_LAST",
    "B-ORDINAL_NTH",
    "I-ORDINAL_NTH",
    "B-MODAL_HYPOTHETICAL",
    "I-MODAL_HYPOTHETICAL",
    "B-ASK_CHANGE",
    "I-ASK_CHANGE",
    "B-ASK_CURRENT",
    "I-ASK_CURRENT",
    "B-REFERENT",
    "I-REFERENT",
    "B-SUBJECT",
    "I-SUBJECT",
    "B-SCOPE",
    "I-SCOPE",
)

#: Lookup: label → row index in the head's logit tensor.  Stable
#: across adapter versions that share a cue vocabulary.
CUE_LABEL_TO_INDEX: dict[CueLabel, int] = {label: idx for idx, label in enumerate(CUE_LABELS)}


#: Cue family name → set of BIO labels belonging to that family.
#: Used by the compositional synthesizer to group a tagged sequence
#: into family buckets before matching grammar productions.
CueFamily = Literal[
    "causal",
    "temporal",
    "temporal_anchor",
    "ordinal",
    "modal",
    "ask",
    "referent",
    "subject",
    "scope",
]

CUE_FAMILIES: dict[CueFamily, frozenset[CueLabel]] = {
    "causal": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-CAUSAL_EXPLICIT",
                "I-CAUSAL_EXPLICIT",
                "B-CAUSAL_ALTLEX",
                "I-CAUSAL_ALTLEX",
            ],
        )
    ),
    "temporal": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-TEMPORAL_BEFORE",
                "I-TEMPORAL_BEFORE",
                "B-TEMPORAL_AFTER",
                "I-TEMPORAL_AFTER",
                "B-TEMPORAL_DURING",
                "I-TEMPORAL_DURING",
                "B-TEMPORAL_SINCE",
                "I-TEMPORAL_SINCE",
            ],
        )
    ),
    "temporal_anchor": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-TEMPORAL_ANCHOR",
                "I-TEMPORAL_ANCHOR",
            ],
        )
    ),
    "ordinal": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-ORDINAL_FIRST",
                "I-ORDINAL_FIRST",
                "B-ORDINAL_LAST",
                "I-ORDINAL_LAST",
                "B-ORDINAL_NTH",
                "I-ORDINAL_NTH",
            ],
        )
    ),
    "modal": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-MODAL_HYPOTHETICAL",
                "I-MODAL_HYPOTHETICAL",
            ],
        )
    ),
    "ask": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-ASK_CHANGE",
                "I-ASK_CHANGE",
                "B-ASK_CURRENT",
                "I-ASK_CURRENT",
            ],
        )
    ),
    "referent": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-REFERENT",
                "I-REFERENT",
            ],
        )
    ),
    "subject": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-SUBJECT",
                "I-SUBJECT",
            ],
        )
    ),
    "scope": frozenset(
        cast(
            "list[CueLabel]",
            [
                "B-SCOPE",
                "I-SCOPE",
            ],
        )
    ),
}


@dataclass(frozen=True)
class TaggedToken:
    """One token marked with a cue label.

    Produced by the CTLG shape_cue_head at inference time and by
    annotators (LLM or human) at training time.  The offsets are
    character-level into the original text so downstream
    consumers can slice the source or align with gazetteer
    :class:`ncms.application.adapters.schemas.DetectedSpan` instances.

    Attributes
    ----------
    char_start, char_end
        Slice of the original text that the token covers.  Note:
        model inputs are BERT wordpieces; tokens here are ALWAYS
        projected up to whole surface words (the training loop
        aggregates subwords by majority-label vote before writing
        a TaggedToken).
    surface
        The token text as it appears in the source.
    cue_label
        One of :data:`CUE_LABELS`.
    confidence
        Softmax probability on the winning class in [0, 1].  For
        annotator-produced tags, annotators set ``1.0``.
    """

    char_start: int
    char_end: int
    surface: str
    cue_label: CueLabel
    confidence: float = 1.0


def group_bio_spans(
    tokens: list[TaggedToken] | tuple[TaggedToken, ...],
) -> list[tuple[str, list[TaggedToken]]]:
    """Collapse B-/I- BIO tags into typed spans.

    Returns a list of ``(cue_type, tokens_in_span)`` pairs, where
    ``cue_type`` is the portion after the ``B-`` / ``I-`` prefix
    (e.g. ``"CAUSAL_EXPLICIT"``).  ``O``-labeled tokens are
    dropped.  A new span starts on every ``B-`` regardless of the
    previous tag (lenient — handles model predictions that emit
    ``I-`` without a preceding ``B-`` by starting a span anyway;
    BIO cleanup is a downstream concern).

    Example
    -------
    >>> tokens = [
    ...   TaggedToken(0, 4, "What", "O", 1.0),
    ...   TaggedToken(5, 8, "did", "O", 1.0),
    ...   TaggedToken(9, 11, "we", "O", 1.0),
    ...   TaggedToken(12, 15, "use", "O", 1.0),
    ...   TaggedToken(16, 22, "before", "B-TEMPORAL_BEFORE", 0.95),
    ...   TaggedToken(23, 31, "Postgres", "B-REFERENT", 0.99),
    ... ]
    >>> group_bio_spans(tokens)
    [('TEMPORAL_BEFORE', [...]), ('REFERENT', [...])]
    """
    spans: list[tuple[str, list[TaggedToken]]] = []
    current_type: str | None = None
    current_tokens: list[TaggedToken] = []
    for tok in tokens:
        if tok.cue_label == "O":
            if current_type is not None:
                spans.append((current_type, current_tokens))
            current_type = None
            current_tokens = []
            continue
        prefix, _, cue_type = tok.cue_label.partition("-")
        if prefix == "B":
            if current_type is not None:
                spans.append((current_type, current_tokens))
            current_type = cue_type
            current_tokens = [tok]
        elif prefix == "I":
            if current_type == cue_type:
                current_tokens.append(tok)
            else:
                # Lenient start — handle orphan I-tags by starting a new span.
                if current_type is not None:
                    spans.append((current_type, current_tokens))
                current_type = cue_type
                current_tokens = [tok]
    if current_type is not None:
        spans.append((current_type, current_tokens))
    return spans


def span_text(tokens: list[TaggedToken]) -> str:
    """Reconstruct the source text of a span from its constituent tokens.

    Callers that have the original text should slice it directly
    using ``(tokens[0].char_start, tokens[-1].char_end)`` — that
    preserves any interior whitespace or punctuation.  This helper
    returns the token surfaces joined by whitespace as a best-effort
    fallback when the original text isn't in scope.
    """
    if not tokens:
        return ""
    return " ".join(t.surface for t in tokens)


__all__ = [
    "CUE_FAMILIES",
    "CUE_LABELS",
    "CUE_LABEL_TO_INDEX",
    "CueFamily",
    "CueLabel",
    "TaggedToken",
    "group_bio_spans",
    "span_text",
]
